"""三栏分开的调用计量累加器。

🔴 为什么绝不合并成一个总数：PRD §6.1 的断言是 `place_calls` ≤ 8、`quota_units` ≤ 120、
`http_requests` ≤ 20 **三项分开统计**——一次批量算路只发 1 个 HTTP 请求，却按
`origins × destinations` 折算成 12 个配额单位、0 次地点检索。合成一个数字的话，
「8 次请求」既可能是安全的也可能是把 100 次/日的地点检索打光了，完全看不出差别。

为什么要按 `walking` / `riding` 分栏：骑行入日程后 `route` 桶由步行、骑行、polyline
懒加载三者共用，双模式连跑约 192 条路线会中途触发预算守护（PRD §4.6）。如果报告里只有
一个合并数字，截停发生在第二轮就根本排查不出来是哪一层超的。

本模块只负责**记账**，不做限流、不做熔断——那些是 `BaiduClient` 的职责（W2a 任务）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

MODE_KEYS: tuple[str, ...] = ("walking", "riding")
"""双模式（FR-206）固定分栏的两栏；出现别的 mode 时按原名动态加栏。"""

_UNSPECIFIED = "unspecified"
"""配额单位没带 mode 时的归栏：不丢账，但回显里一眼能看出「有账没分摊到模式」。"""


def quota_units_for_matrix(origins: int, destinations: int) -> int:
    """批量算路的配额折算：`origins × destinations` 条路线 = 这么多个 `quota_units`。

    单独一个函数是有原因的：笔记 §2.8 与 PRD §6.1 用的是同一口径，写第二处就会算错。
    """
    return max(0, int(origins)) * max(0, int(destinations))


@dataclass
class ServiceUsage:
    """单个服务桶的三栏计数。"""

    place_calls: int = 0
    quota_units: int = 0
    http_requests: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "place_calls": self.place_calls,
            "quota_units": self.quota_units,
            "http_requests": self.http_requests,
        }


@dataclass
class Metrics:
    """一次请求或整个进程的三栏计数。

    线程安全：uvicorn 下同步路由跑在线程池、异步路由跑在事件循环，两边都会记账，
    所以每处累加都要上锁——少锁一次就会在并发压测里丢计数，而丢计数正是最难查的账错。
    """

    label: str = "process"
    place_calls: int = 0
    quota_units: int = 0
    http_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    degraded_count: int = 0
    by_service: dict[str, ServiceUsage] = field(default_factory=dict)
    quota_units_by_mode: dict[str, int] = field(default_factory=dict)
    route_lines_by_mode: dict[str, int] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # ------------------------------------------------------------------ 记账入口
    def add_place_calls(self, count: int = 1, *, service: str = "place") -> int:
        """记地点检索 / 地址联想的**调用次数**（个人档只有 100 次/日，最紧的瓶颈）。"""
        with self._lock:
            self.place_calls += count
            self._bucket(service).place_calls += count
            return self.place_calls

    def add_http_requests(self, count: int = 1, *, service: str | None = None) -> int:
        """记 HTTP 往返次数；批量算路把 12 条路线折成 1 次，就记在这一栏。"""
        with self._lock:
            self.http_requests += count
            if service:
                self._bucket(service).http_requests += count
            return self.http_requests

    def add_quota_units(
        self,
        count: int,
        *,
        mode: str | None = None,
        service: str = "route",
    ) -> int:
        """记配额消耗单位（`origins × destinations`）。

        `mode` 传 `walking` / `riding` 时同时并进分栏；不传则进 `unspecified`，
        让「没分摊到模式」这件事在回显里可见，而不是被静悄悄算进总数。
        """
        with self._lock:
            return self._add_quota_units_locked(count, mode=mode, service=service)

    def _add_quota_units_locked(self, count: int, *, mode: str | None, service: str) -> int:
        """调用方已持锁的实现：`add_route_lines` 要在同一把锁里同时动两栏。"""
        self.quota_units += count
        self._bucket(service).quota_units += count
        key = mode or _UNSPECIFIED
        self.quota_units_by_mode[key] = self.quota_units_by_mode.get(key, 0) + count
        return self.quota_units

    def add_route_lines(self, count: int, *, mode: str, origins: int = 1) -> int:
        """记路线条数（预算守护看的就是这一栏），并按模式分栏。

        `origins` 默认 1：等时圈是「一个中心点 × N 个探测点」，批量算路传多个 origin 时
        由调用方显式给出，避免这里替业务猜折算。
        """
        with self._lock:
            total = self.route_lines_by_mode.get(mode, 0) + count
            self.route_lines_by_mode[mode] = total
            self._add_quota_units_locked(count * max(1, origins), mode=mode, service="route")
            return total

    def add_cache_hit(self, count: int = 1) -> int:
        with self._lock:
            self.cache_hits += count
            return self.cache_hits

    def add_cache_miss(self, count: int = 1) -> int:
        with self._lock:
            self.cache_misses += count
            return self.cache_misses

    def add_degradation(self, count: int = 1) -> int:
        """降级次数：`live` 打不通转 `cache` / `sample` 的每一跳都记一笔。

        为什么单列：FR-403 的「任一层失败不中断流程」要能被证明发生过，只有结果对象里
        的 `data_source` 是不够的——报告得能说清「降级了几次、从哪层落到哪层」。
        """
        with self._lock:
            self.degraded_count += count
            return self.degraded_count

    # ------------------------------------------------------------------ 读取
    def snapshot(self) -> dict[str, Any]:
        """PRD §4.7 `usage` 字段的形状；三栏永远平铺，不给合并总数的入口。"""
        with self._lock:
            modes = self._mode_keys()
            return {
                "label": self.label,
                "place_calls": self.place_calls,
                "quota_units": self.quota_units,
                "http_requests": self.http_requests,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "degraded_count": self.degraded_count,
                "quota_units_by_mode": {
                    mode: self.quota_units_by_mode.get(mode, 0) for mode in modes
                },
                "route_lines": {mode: self.route_lines_by_mode.get(mode, 0) for mode in modes},
                "by_service": {
                    service: usage.as_dict() for service, usage in sorted(self.by_service.items())
                },
            }

    def budget_violations(self, budget: Mapping[str, int]) -> list[str]:
        """返回超出预算的栏名（空列表表示没超）。

        只认 `place_calls` / `quota_units` / `http_requests` / `route_lines` 四个键；
        `route_lines` 传的是**按模式数折算后**的预算（见 `Settings.route_lines_budget`），
        折算不在这里做，免得守卫和报告各算一套。
        """
        snap = self.snapshot()
        violations: list[str] = []
        for key, limit in budget.items():
            used = sum(snap["route_lines"].values()) if key == "route_lines" else snap.get(key, 0)
            if isinstance(used, int) and used > limit:
                violations.append(key)
        return violations

    def merge(self, other: Metrics) -> None:
        """把请求级累加器并进进程级累加器（中间件在响应结束时调用）。"""
        with self._lock:
            self.place_calls += other.place_calls
            self.quota_units += other.quota_units
            self.http_requests += other.http_requests
            self.cache_hits += other.cache_hits
            self.cache_misses += other.cache_misses
            self.degraded_count += other.degraded_count
            for service, usage in other.by_service.items():
                target = self._bucket(service)
                target.place_calls += usage.place_calls
                target.quota_units += usage.quota_units
                target.http_requests += usage.http_requests
            for mode, value in other.quota_units_by_mode.items():
                self.quota_units_by_mode[mode] = self.quota_units_by_mode.get(mode, 0) + value
            for mode, value in other.route_lines_by_mode.items():
                self.route_lines_by_mode[mode] = self.route_lines_by_mode.get(mode, 0) + value

    def reset(self) -> None:
        """清零；给测试用（预算断言要能把累加器复位后再跑一遍）。"""
        with self._lock:
            self.place_calls = 0
            self.quota_units = 0
            self.http_requests = 0
            self.cache_hits = 0
            self.cache_misses = 0
            self.degraded_count = 0
            self.by_service.clear()
            self.quota_units_by_mode.clear()
            self.route_lines_by_mode.clear()

    # ------------------------------------------------------------------ 内部
    def _bucket(self, service: str) -> ServiceUsage:
        """调用方已持锁；本方法只负责取（并按需建）某个服务桶。"""
        usage = self.by_service.get(service)
        if usage is None:
            usage = ServiceUsage()
            self.by_service[service] = usage
        return usage

    def _mode_keys(self) -> list[str]:
        """分栏键。

        `walking` / `riding` 恒在（回显 0 比缺栏好，双模式断言靠两栏对齐才看得出第二轮
        花在哪）；其余模式照实加栏；`unspecified` 只在真有账时才出现——否则分栏加起来
        对不上 `quota_units` 总数，又变成一处「数字看着没事其实漏了」的账错。
        """
        keys = list(MODE_KEYS)
        for mode in sorted(set(self.route_lines_by_mode) | set(self.quota_units_by_mode)):
            if mode in keys:
                continue
            if mode == _UNSPECIFIED and self.quota_units_by_mode.get(mode, 0) == 0:
                continue
            keys.append(mode)
        return keys


_current_metrics: ContextVar[Metrics | None] = ContextVar("circledoc_metrics", default=None)
_process_metrics = Metrics(label="process")


def get_process_metrics() -> Metrics:
    """进程级累加器；`/api/cache/stats` 之类的总量接口读它。"""
    return _process_metrics


def current_metrics() -> Metrics | None:
    """当前请求上下文绑定的累加器；不在请求里则返回 ``None``。"""
    return _current_metrics.get()


@contextmanager
def metrics_scope(label: str = "request") -> Iterator[Metrics]:
    """给一段代码（通常是一个 HTTP 请求）绑定独立的累加器。

    用 contextvar 而不是参数透传：`services/` 的算法函数要保持纯，签名里不该出现
    「记账对象」这种基础设施参数；计量在 `infra` 内部完成，调用栈上看不出差别。
    """
    recorder = Metrics(label=label)
    token = _current_metrics.set(recorder)
    try:
        yield recorder
    finally:
        _current_metrics.reset(token)


def _emit(method: str, count: int, kwargs: dict[str, Any]) -> int:
    """把一次记账写到当前上下文的累加器；不在请求上下文时落到进程级。

    为什么不静默丢弃：脱离请求上下文的调用（启动自检、后台任务）同样在烧真实配额，
    丢了账就等于告诉评审「今天没打过百度的请求」。
    """
    recorder = current_metrics() or _process_metrics
    return int(getattr(recorder, method)(count, **kwargs))


def add_place_calls(count: int = 1, *, service: str = "place") -> int:
    """模块级记账入口，等价于 `current_metrics().add_place_calls(...)`。"""
    return _emit("add_place_calls", count, {"service": service})


def add_http_requests(count: int = 1, *, service: str | None = None) -> int:
    """模块级记账入口（HTTP 往返）。"""
    return _emit("add_http_requests", count, {"service": service})


def add_quota_units(count: int, *, mode: str | None = None, service: str = "route") -> int:
    """模块级记账入口（配额单位，按模式分栏）。"""
    return _emit("add_quota_units", count, {"mode": mode, "service": service})
