"""双熔断器：`concurrency_breaker` 与 `quota_breaker`，**两者都按服务独立**。

为什么是两个而不是一个（PRD FR-401 点名 v1.0 的错误）：v1.0 只有一个 30 s 熔断器，
它对「并发超限」是对的，对「当日配额耗尽」是**错的**——`status=4` / `302` 当天不可恢复
（FAQ 2361 / 2366：次日零时归零），30 s 后重连只会每隔半分钟再烧掉一次请求，把剩下的
配额加速耗光，并且让用户在同一天里反复看到同一个错误。

为什么必须按服务分：地点检索 100 次/日、路线规划 5,000 次/日（笔记 §3.1）。全局一条熔断
会让「地点检索今天打光了」误伤成「路线也别打了」，而路线恰恰还能打 4,900 次；反过来路线
被并发墙挡住时，也不该把只有 300 次/日的逆地理一起关禁闭。

判据一律来自 `infra/errors.BAIDU_ERROR_TABLE`：本文件不写任何「if status == 4」式的分支，
新增状态码只改那张表（笔记 §3.2）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from time import monotonic
from typing import Any

from infra.config import Settings, get_settings
from infra.errors import (
    CONCURRENCY_BREAKER_HOLD_S,
    BaiduApiError,
    BreakerKind,
    BreakerLifetime,
    CircleDocError,
    ErrorSpec,
    seconds_until_next_midnight,
    spec_for,
)

__all__ = [
    "BreakerDecision",
    "BreakerRegistry",
    "BreakerState",
    "ConcurrencyBreaker",
    "QuotaBreaker",
]

MonotonicClock = Callable[[], float]
WallClock = Callable[[], datetime]


class BreakerState(StrEnum):
    """熔断器的三态。半开只对 `concurrency_breaker` 存在。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerDecision:
    """一次「能不能出网」的判定结果。"""

    allowed: bool
    service: str
    kind: BreakerKind = BreakerKind.NONE
    state: BreakerState = BreakerState.CLOSED
    retry_after_s: float | None = None
    reason: str | None = None
    consecutive_failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        """给日志与 `/api/health` 的字典形式；`kind` 落回字符串以免序列化时踩到 StrEnum。"""
        return {
            "allowed": self.allowed,
            "service": self.service,
            "breaker": self.kind.value,
            "state": self.state.value,
            "retry_after_s": self.retry_after_s,
            "reason": self.reason,
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass
class _ConcState:
    """单个服务在 `concurrency_breaker` 里的状态。"""

    failures: int = 0
    opened_at: float | None = None
    open_until: float | None = None
    probe_in_flight: bool = False
    trips: int = 0


@dataclass
class _QuotaState:
    """单个服务在 `quota_breaker` 里的状态。"""

    opened_at: datetime | None = None
    expires_at: float | None = None
    status: int | None = None
    trips: int = 0
    blocked_total: int = 0


class ConcurrencyBreaker:
    """针对 `status=401` / `1` / 网络超时：连续 N 次失败 → 打开约 30 s → 半开只放一个探测。

    为什么半开只放一个：打开 30 s 后如果一次性放行整批排队请求，等于把刚歇下来的并发墙
    重新撞一遍，第二次熔断又得等 30 s——恢复期反而比不熔断更长。
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        hold_s: int = CONCURRENCY_BREAKER_HOLD_S,
        clock: MonotonicClock | None = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.hold_s = max(1, int(hold_s))
        self._clock = clock or monotonic
        self._states: dict[str, _ConcState] = {}

    def state_of(self, service: str) -> BreakerState:
        """某服务当前处于三态中的哪一态（到点自动从 OPEN 变 HALF_OPEN）。"""
        state = self._peek(service)
        if state is None or state.open_until is None:
            return BreakerState.CLOSED
        if self._clock() < state.open_until:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def decision(self, service: str) -> BreakerDecision:
        """是否允许该服务此刻出网；半开时只允许第一个探测请求通过。"""
        current = self.state_of(service)
        if current is BreakerState.CLOSED:
            # 放行路径不建状态：读一次就把服务写进字典，会让「HTTP 500 不算限流信号」
            # 这类判据的快照断言变成「有个 0 计数的条目」，看不出真的没计数。
            return BreakerDecision(True, service, state=BreakerState.CLOSED)
        state = self._raw(service)
        if current is BreakerState.OPEN:
            assert state.open_until is not None  # 由 state_of() 的分支保证
            return BreakerDecision(
                False,
                service,
                kind=BreakerKind.CONCURRENCY,
                state=BreakerState.OPEN,
                retry_after_s=max(0.0, state.open_until - self._clock()),
                reason="concurrency_breaker_open",
                consecutive_failures=state.failures,
            )
        # 半开：已有探测在途就继续挡住，否则本次调用领取这个唯一名额。
        if state.probe_in_flight:
            return BreakerDecision(
                False,
                service,
                kind=BreakerKind.CONCURRENCY,
                state=BreakerState.HALF_OPEN,
                retry_after_s=1.0,
                reason="concurrency_breaker_probe_in_flight",
                consecutive_failures=state.failures,
            )
        state.probe_in_flight = True
        return BreakerDecision(
            True,
            service,
            kind=BreakerKind.CONCURRENCY,
            state=BreakerState.HALF_OPEN,
            reason="concurrency_breaker_probe",
            consecutive_failures=state.failures,
        )

    def record_failure(self, service: str, *, hold_s: int | None = None) -> bool:
        """记一次「限流类」失败；累计到阈值就打开熔断。返回本次是否触发了打开动作。"""
        state = self._raw(service)
        state.failures += 1
        if state.failures < self.threshold:
            return False
        self._open(service, hold_s or self.hold_s)
        return True

    def record_success(self, service: str) -> None:
        """成功即清零连败计数并关闭熔断（半开探测成功就是恢复信号）。"""
        state = self._raw(service)
        state.failures = 0
        state.opened_at = None
        state.open_until = None
        state.probe_in_flight = False

    def release_probe(self, service: str) -> None:
        """探测请求既没成功也没失败（例如被取消）时归还半开名额，否则该服务会永久卡死。"""
        self._raw(service).probe_in_flight = False

    def trip(self, service: str, *, hold_s: int | None = None) -> None:
        """不看计数、立刻打开（用于「连续多次 401 后主动收手」这类上层判断）。"""
        self._open(service, hold_s or self.hold_s)

    def remaining_s(self, service: str) -> float:
        """熔断还需多少秒才允许再试；已关闭返回 0。"""
        state = self._peek(service)
        if state is None or state.open_until is None:
            return 0.0
        return max(0.0, state.open_until - self._clock())

    def snapshot(self) -> dict[str, Any]:
        """各服务的连败次数、状态与累计打开次数。"""
        return {
            service: {
                "state": self.state_of(service).value,
                "consecutive_failures": state.failures,
                "remaining_s": round(self.remaining_s(service), 3),
                "trips": state.trips,
            }
            for service, state in sorted(self._states.items())
        }

    def _open(self, service: str, hold_s: int) -> None:
        state = self._raw(service)
        now = self._clock()
        state.opened_at = now
        state.open_until = now + max(1, int(hold_s))
        state.probe_in_flight = False
        state.trips += 1

    def _raw(self, service: str) -> _ConcState:
        state = self._states.get(service)
        if state is None:
            state = _ConcState()
            self._states[service] = state
        return state

    def _peek(self, service: str) -> _ConcState | None:
        """只读取状态；从未失败过的服务不该因为「被问过一句」就出现在熔断快照里。"""
        return self._states.get(service)


class QuotaBreaker:
    """针对 `status=4` / `302`：**命中即打开，持续到次日零时，期间该服务任何请求都不许出网**。

    🔴 这里刻意**没有半开、没有阈值、没有重试**：配额次日零时才归零（FAQ 2366），当天任何
    一次重连都是纯损耗。这是本模块最容易被写回「30 s 熔断」的地方，所以类里连一个
    `half_open` 分支都不存在，让误写从「看不出来」变成「编译不过」。
    """

    def __init__(
        self,
        *,
        tz_offset_hours: int = 8,
        now: WallClock | None = None,
    ) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self._now = now or (lambda: datetime.now().astimezone())
        self._states: dict[str, _QuotaState] = {}

    def open(self, service: str, *, status: int | None = None, hold_s: int | None = None) -> int:
        """打开该服务的配额熔断，返回本次设定的存活秒数。

        存活期默认按「次日零时」算；传 `hold_s` 让上层能直接采用 `ErrorSpec.breaker_expires_in_s()`
        给出的值，避免两处各算一次零点。已经打开时不缩短——同一个服务当天第二次 302 不该
        让熔断提前结束。
        """
        state = self._raw(service)
        if hold_s:
            hold = max(1, int(hold_s))
        else:
            hold = max(1, seconds_until_next_midnight(self.now(), self.tz_offset_hours))
        deadline = self._epoch() + hold
        if state.expires_at is not None:
            hold = max(hold, int(state.expires_at - self._epoch()))
            deadline = max(deadline, state.expires_at)
            state.trips += 1
        else:
            state.trips = 1
        state.opened_at = self.now()
        state.expires_at = deadline
        state.status = status if status is not None else state.status
        return int(deadline - self._epoch())

    def is_open(self, service: str) -> bool:
        """该服务今天是否已被配额熔断拦停（到次日零时自动失效）。"""
        state = self._peek(service)
        if state is None or state.expires_at is None:
            return False
        return self._epoch() < state.expires_at

    def decision(self, service: str) -> BreakerDecision:
        """配额熔断只有「放不放行」两态，没有半开探测这一档。"""
        if not self.is_open(service):
            return BreakerDecision(True, service, state=BreakerState.CLOSED)
        state = self._raw(service)
        state.blocked_total += 1
        assert state.expires_at is not None  # 由 is_open() 的分支保证
        return BreakerDecision(
            False,
            service,
            kind=BreakerKind.QUOTA,
            state=BreakerState.OPEN,
            retry_after_s=max(1.0, state.expires_at - self._epoch()),
            reason="quota_breaker_open",
        )

    def remaining_s(self, service: str) -> int:
        """距熔断解除还有多少秒；未熔断返回 0。上层用它写 `Retry-After`。"""
        state = self._peek(service)
        if state is None or state.expires_at is None:
            return 0
        return max(0, int(state.expires_at - self._epoch()))

    def status_for(self, service: str) -> int | None:
        """当初触发熔断的那个 `status`（4 或 302），供异常与文案复用官方码。"""
        state = self._peek(service)
        if state is None or not self.is_open(service):
            return None
        return state.status

    def reset(self, service: str | None = None) -> None:
        """清零某服务（或全部）的熔断状态。

        为什么暴露这个口子：配额熔断是运行期状态，跨天复用进程、或测试里要模拟「新的一天」
        时必须能显式清掉；把它做成不可清会让「今天已耗尽」黏在进程上一整周。
        """
        if service is None:
            self._states.clear()
        else:
            self._states.pop(service, None)

    def snapshot(self) -> dict[str, Any]:
        """各服务的熔断剩余时间与累计拦停次数。"""
        return {
            service: {
                "open": self.is_open(service),
                "status": state.status,
                "remaining_s": self.remaining_s(service),
                "trips": state.trips,
                "blocked_total": state.blocked_total,
            }
            for service, state in sorted(self._states.items())
        }

    def _raw(self, service: str) -> _QuotaState:
        state = self._states.get(service)
        if state is None:
            state = _QuotaState()
            self._states[service] = state
        return state

    def _peek(self, service: str) -> _QuotaState | None:
        """只读取状态，不为「问过一句」的服务凭空建一条熔断记录。"""
        return self._states.get(service)

    def now(self) -> datetime:
        """当前墙上时间。公开是因为 `ErrorSpec.breaker_expires_in_s(now)` 也要同一个值。"""
        return self._now()

    def _epoch(self) -> float:
        return self.now().timestamp()


class BreakerRegistry:
    """把两个熔断器按服务成套管起来，并把「哪一类失败该动哪个熔断器」的判定收口到一处。

    上层（`BaiduClient`）只调 `before_request()` 与 `record_success()` / `record_failure()`，
    自己不再判断 status 属于限流还是配额——判据永远只在 `BAIDU_ERROR_TABLE` 里。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: MonotonicClock | None = None,
        now: WallClock | None = None,
        concurrency: ConcurrencyBreaker | None = None,
        quota: QuotaBreaker | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        cfg = self.settings
        self.concurrency = concurrency or ConcurrencyBreaker(
            threshold=cfg.concurrency_breaker_threshold,
            hold_s=cfg.concurrency_breaker_hold_s or CONCURRENCY_BREAKER_HOLD_S,
            clock=clock,
        )
        self.quota = quota or QuotaBreaker(
            tz_offset_hours=cfg.quota_breaker_tz_offset_hours,
            now=now,
        )

    def before_request(self, service: str) -> BreakerDecision:
        """出网前的闸门。`quota_breaker` 优先：它一旦打开，重试到明天都没意义。"""
        quota_decision = self.quota.decision(service)
        if not quota_decision.allowed:
            return quota_decision
        return self.concurrency.decision(service)

    def record_success(self, service: str) -> None:
        """成功收敛：连败清零、半开探测结束。配额熔断不会被成功关掉（当天不可能恢复）。"""
        self.concurrency.record_success(service)

    def record_failure(
        self, service: str, status: int | None = None, *, transport: bool = False
    ) -> None:
        """按官方码表分派失败：配额类开配额熔断，并发类累计并发熔断，其余两个都不动。

        为什么参数 / 鉴权错误两个都不动：`2` / `203` / `210` / `240` 是我们自己拼错了或
        Key 没开通，重试与熔断都改变不了结果（笔记 §3.2 全部标 ❌ 不重试）。把它们计入
        熔断计数只会让「一个能用的服务」被无关的鉴权错误拖停。
        """
        spec: ErrorSpec | None = spec_for(status) if status is not None else None
        kind = spec.breaker if spec is not None else BreakerKind.NONE
        lifetime = spec.breaker_lifetime if spec is not None else BreakerLifetime.NONE

        if kind is BreakerKind.QUOTA or lifetime is BreakerLifetime.UNTIL_NEXT_MIDNIGHT:
            hold = spec.breaker_expires_in_s(self.quota.now()) if spec is not None else None
            self.quota.open(service, status=status, hold_s=hold)
            return
        if kind is BreakerKind.CONCURRENCY:
            hold_s = spec.breaker_hold_s if spec is not None else None
            self.concurrency.record_failure(service, hold_s=hold_s)
            return
        if transport:
            # PRD §4.6：网络超时 / 连接错误计入 concurrency_breaker 的连续失败次数。
            self.concurrency.record_failure(service)

    def error_for(self, service: str) -> CircleDocError:
        """为「被熔断器拦下」造一个 typed error（**不发请求**，文案仍来自官方码表）。

        为什么复用当初触发的 `status`：`quota_breaker` 是因 302 开的，就仍报
        `baidu_daily_quota_limited`；换成固定 4 会让日志与前端文案指向一个今天没发生过的
        错误码，排查时被带到别的方向。
        """
        quota_status = self.quota.status_for(service)
        if self.quota.is_open(service) or quota_status is not None:
            return BaiduApiError.from_status(
                quota_status if quota_status is not None else 4,
                service=service,
                detail={
                    "blocked_by_breaker": BreakerKind.QUOTA.value,
                    "retry_after_s": self.quota.remaining_s(service),
                    "no_request_sent": True,
                },
            )
        return BaiduApiError.from_status(
            401,
            service=service,
            detail={
                "blocked_by_breaker": BreakerKind.CONCURRENCY.value,
                "retry_after_s": round(self.concurrency.remaining_s(service), 3),
                "no_request_sent": True,
            },
        )

    def snapshot(self) -> dict[str, Any]:
        """两个熔断器的完整状态，给 `/api/health` 与日志用。"""
        return {
            "concurrency_breaker": self.concurrency.snapshot(),
            "quota_breaker": self.quota.snapshot(),
        }
