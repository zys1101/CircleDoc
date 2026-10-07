"""按服务分桶的异步令牌桶（PRD §4.6「限流：按服务分桶的令牌桶」）。

为什么分桶而不是一个全局桶：各服务稀缺度相差 50 倍（地点检索 100 次/日、路线规划
5,000 次/日，笔记 §3.1）。共用一个桶时，等时圈那 ≈96 条廉价路线会把令牌吃光，紧随其后的
地点检索先在 100 次/日的天花板之前撞上 QPS 墙——等于限流器替业务做了错误的资源分配。

为什么 `burst` 可以大于 `rate`：PRD §4.6 正文要求 `burst ≤ rate`，但它自己给的表里
place(0.5/1) 与 geocode(1.0/2) 的 `burst` 都大于 `rate`（`infra/config.py` 同段注释已记录
这个口径冲突）。这里刻意**不对这一条加校验**：默认配置必须能直接启动（零配置约定），贸然拦死
会让 `uvicorn api.app:app` 在什么都没配错的机器上起不来。

为什么分池必须共享父令牌（R5）：`split_route_by_mode=True` 早期让 walking / riding 各建一个
2.5 QPS 的独立池，合计 5 QPS > 官方「路线规划（合计类目）」3 QPS（笔记 §3.1、PRD §4.6），
现场表现是 `status=401` 直到 `status=302` 全天封禁。现在两个模式桶是**同一个 route 父池的视图**
（`PoolView`），令牌、容量与排队锁都在父池上，「合计 ≤ rate」由结构保证而不是靠人记得除以 2。
同时 `RateLimitRegistry.rate_warnings` 会把「同一官方档位下的独立池速率之和越界」如实报出来。

为什么时钟与 sleep 都可注入：QA 要把阈值调成 0.5 QPS 连发 20 个请求来验排队。真等就是
40 s，CI 里不可能跑；`FakeClock` 让 `sleep()` 只推进假时钟而不消耗墙钟，排队顺序与等待
时长照样可断言，测试却在毫秒级完成。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from infra.config import Settings, get_settings
from infra.errors import CircleDocError

__all__ = [
    "BUCKET_NAMES",
    "DEFAULT_MODE_VIEWS",
    "DEFAULT_POOL_ALIASES",
    "MAX_STALL_ROUNDS",
    "MIN_RATE_QPS",
    "OFFICIAL_QPS_BY_TIER",
    "OFFICIAL_QPS_DEFAULT",
    "Clock",
    "FakeClock",
    "PoolView",
    "RateLimitRegistry",
    "Sleeper",
    "TokenBucket",
    "logical_bucket_name",
]

Clock = Callable[[], float]
"""单调时钟：返回「秒」。默认 `time.monotonic`，测试里换成 `FakeClock.now`。"""

Sleeper = Callable[[float], Awaitable[None]]
"""异步等待函数。默认 `asyncio.sleep`，测试里换成 `FakeClock.sleep`。"""

MIN_RATE_QPS = 0.01
"""令牌补充速率下限。为什么要有：有人把 `RATE_PLACE_QPS` 写成 0 或负数时，桶永远不会再
满，第一个请求就永久挂起——整个分析卡死比「限流形同虚设」更难排查。夹成极小正数后行为
只是「慢」，而且照样会被出网预算守卫截停。"""

MAX_STALL_ROUNDS = 3
"""排队时「睡了但时钟没走」的最大容忍轮数。

为什么必须有：注入 `clock=FakeClock().now` 却忘了配对 `sleep=fake.sleep`（或把两个
FakeClock 实例分别传给两者）时，桶位永远不会回升，`acquire()` 就成了无限循环——
整个分析挂死且没有任何堆栈线索，是最难复现的一类卡死。宁可在这里抛一条说清原因的
异常，也不要让调用方对着 100% CPU 的空转进程发呆。
"""

BUCKET_NAMES: tuple[str, ...] = (
    "place",
    "regeo",
    "geocode",
    "suggest",
    "direction",
    "route_walking",
    "route_riding",
)
"""可寻址的**逻辑桶名**。注意「可寻址」不等于「独立额度」：默认合并关系见
`DEFAULT_POOL_ALIASES`，分池模式下 `route_walking` / `route_riding` 是父池视图，见
`DEFAULT_MODE_VIEWS` 与 `PoolView`。"""

DEFAULT_POOL_ALIASES: dict[str, str] = {
    # FAQ 2540 / 2549：地点输入提示与地点检索**共用同一份 100 次/日配额**（笔记 §2.4）。
    # 两个各自 0.5 QPS 的独立桶实际会放行 1 QPS，等于把配额算重一倍，与 config.py 里
    # SERVICE_BUCKET_MAP 的注释保持同一口径。
    "suggest": "place",
    # 官方权益表把「路线规划」列为**合计类目** 5,000/日 · 3 QPS（笔记 §3.1），步行、骑行、
    # polyline 懒加载共用这一档；PRD §4.6 也明确要求 route 桶三者共用。
    "direction": "route",
    "routematrix": "route",
    "polyline": "route",
    "route_walking": "route",
    "route_riding": "route",
}
"""逻辑桶名 → 实际令牌池名。默认把 route 家族并进同一个池，保证总放行速率 ≤ 官方 3 QPS。"""

_POOL_RATE_KEY: dict[str, str] = {
    # `Settings.rate_limit_for()` 只认 place / regeo / route / geocode 四个键（它是按
    # `SERVICE_BUCKET_MAP` 的取值写的）。把 `route_walking` 直接递给它会落到「未知服务
    # → 最保守的 place 档」，于是步行矩阵从 2.5 QPS 掉到 0.5 QPS，等时圈慢 5 倍且完全
    # 看不出原因。这里先把令牌池名换算成那四个键之一，数值仍然只来自 Settings。
    "place": "place",
    "suggest": "place",
    "regeo": "regeo",
    "geocode": "geocode",
    "route": "route",
    "route_walking": "route",
    "route_riding": "route",
    "direction": "route",
    "routematrix": "route",
    "polyline": "route",
}


DEFAULT_MODE_VIEWS: dict[str, str] = {
    # `split_route_by_mode=True` 时生效：这两个逻辑桶**不建独立令牌池**，而是父池的视图
    # （名字与排队统计独立、令牌全部来自父池）。取舍与理由见 `PoolView` 与注册表构造函数。
    "route_walking": "route",
    "route_riding": "route",
}

OFFICIAL_QPS_BY_TIER: dict[str, float] = {
    # 笔记 §3.1 个人开发者栏：地理编码 / 逆地理编码 / 地点检索 / 路线规划（合计类目）
    # 全是 3 QPS。这里是「同档位所有独立池之和」的上限，不是单个池的上限。
    "place": 3.0,
    "regeo": 3.0,
    "geocode": 3.0,
    "route": 3.0,
}

OFFICIAL_QPS_DEFAULT = 3.0
"""未登记档位的官方 QPS：按全线最保守的已知值 3 处理（笔记 §3.1 个人档没有高于 3 的类目，
多维/深度检索还只有 1）。"""


def logical_bucket_name(service: str, mode: str | None = None) -> str:
    """把调用方给的服务名 / 别名规范成 `BUCKET_NAMES` 里的逻辑桶名。

    为什么需要这一层：上层（W3 的 `/api/geocode`、等时圈任务）传进来的写法并不统一
    （`geocoding`、`reverse_geocoding`、`place_search`、`routematrix`…），而桶名必须是
    有限集合，否则 `snapshot()` 里会出现十几个语义重叠的桶，看板上就分不清哪个桶在排队。
    """
    key = (service or "").strip().lower().replace("-", "_")
    if key in {"route", "routematrix", "direction", "directionlite", "polyline", "matrix"}:
        if mode in {"walking", "riding"}:
            return f"route_{mode}"
        return "route"
    if key in {"regeo", "reverse_geocoding", "reverse_geocode"}:
        return "regeo"
    if key in {"suggest", "suggestion", "place_suggest"}:
        return "suggest"
    if key in {"geocode", "geocoding", "geocoding_v3"}:
        return "geocode"
    if key in {"place", "place_search", "search", "region"}:
        return "place"
    return key or "place"


@dataclass(frozen=True)
class BucketParams:
    """某个桶的速率配置快照，供日志与 `/api/health` 回显。"""

    rate_qps: float
    burst: int
    source: str = "settings"

    def as_dict(self) -> dict[str, Any]:
        return {"rate_qps": self.rate_qps, "burst": self.burst, "source": self.source}


class FakeClock:
    """测试用假时钟：`sleep` 不消耗墙钟时间，只把时钟往前推。

    为什么放在 `infra` 而不是 tests：QA 的限流断言（0.5 QPS 连发 20 个）与演练脚本都要用，
    收在 infra 里能保证「大家用的是同一个推进语义」，否则两处各写一个假时钟，其中一个
    忘了 `await sleep(0)` 就会出现「本地过、CI 挂」这种最难复现的问题。
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        """当前假时间（秒）。作为 `clock` 传给令牌桶。"""
        return self._now

    def advance(self, seconds: float) -> None:
        """手动把时钟往前推，用于断言「等待若干秒后桶确实回满了令牌」。"""
        self._now += max(0.0, float(seconds))

    async def sleep(self, seconds: float) -> None:
        """替代 `asyncio.sleep`：推进时钟并让出事件循环，让排队逻辑真的走一遍。"""
        self.advance(seconds)
        await asyncio.sleep(0)


class TokenBucket:
    """单个令牌池。`rate` 为每秒补充的令牌数，`burst` 为桶容量（允许瞬时并发放行数）。"""

    def __init__(
        self,
        name: str,
        rate_qps: float,
        burst: int,
        *,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        initial_tokens: float | None = None,
    ) -> None:
        """建立令牌桶。

        `burst` 大于 `rate` 时不报错（见模块 docstring 的口径冲突说明）；小于 1 时容量夹到
        1，否则桶里永远放不进一个完整令牌，第一个请求就会永久卡住。
        """
        self.name = name
        self.rate = max(float(rate_qps), MIN_RATE_QPS)
        self.capacity = max(float(burst), 1.0)
        self.tokens = self.capacity if initial_tokens is None else float(initial_tokens)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._last_refill = self._clock()
        # 等待必须串行化：两个协程同时读到「还剩 1 个令牌」就会双双放行，实际速率翻倍——
        # 这正是 v1.0 里 burst 设置过大导致 status=401 的同一种错误。
        self._lock = asyncio.Lock()
        self.waited_total_s = 0.0
        self.acquired_total = 0

    def refill(self) -> float:
        """按流逝时间补令牌，返回补充后的可用令牌数（不超过容量）。"""
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        return self.tokens

    @property
    def available(self) -> float:
        """当前可用令牌数（读取前先补一次，避免调用方拿到过期值）。"""
        return self.refill()

    def try_acquire(self, cost: float = 1.0) -> bool:
        """非阻塞取令牌；取不到就返回 False（调用方自己决定是排队还是放弃）。"""
        self.refill()
        need = min(max(float(cost), 0.0), self.capacity)
        if self.tokens >= need:
            self.tokens -= need
            self.acquired_total += 1
            return True
        return False

    async def acquire(self, cost: float = 1.0) -> float:
        """阻塞式取令牌，返回实际等待了多少秒。

        为什么在持锁期间 `await`：排队者必须一个接一个地重新看桶位，否则唤醒后彼此都以为
        还有令牌，桶就等于没生效。代价是同一桶内的请求严格串行放行——这正是我们想要的
        行为（PRD §4.6 的「令牌桶」不是「信号量」）。
        """
        need = min(max(float(cost), 0.0), self.capacity)
        async with self._lock:
            self.refill()
            waited = 0.0
            stalls = 0
            while self.tokens < need:
                deficit = need - self.tokens
                wait_s = deficit / self.rate
                before = self._clock()
                await self._sleep(wait_s)
                waited += wait_s
                # 睡了一觉但时钟没走：只有「clock 与 sleep 不是同一个假时钟」才可能发生，
                # 再睡下去也只是把 CPU 挂成 100%，所以连着几次就立刻报错。
                if self._clock() <= before:
                    stalls += 1
                    if stalls >= MAX_STALL_ROUNDS:
                        raise CircleDocError(
                            message=(
                                f"令牌桶 {self.name} 等待期间时钟没有前进："
                                "注入的 clock 与 sleep 必须来自同一个 FakeClock"
                            ),
                            detail={
                                "bucket": self.name,
                                "rate_qps": self.rate,
                                "needed": need,
                                "tokens": round(self.tokens, 6),
                                "waited_s": round(waited, 3),
                            },
                        )
                else:
                    stalls = 0
                self.refill()
            self.tokens -= need
            self.acquired_total += 1
            self.waited_total_s += waited
            return waited

    def snapshot(self) -> dict[str, Any]:
        """桶的状态字典：速率、容量、当前令牌、累计等待。给 `/api/health` 与日志用。"""
        return {
            "name": self.name,
            "available_tokens": round(self.available, 3),
            **self.params.as_dict(),
            "waited_total_s": round(self.waited_total_s, 3),
            "acquired_total": self.acquired_total,
        }

    @property
    def params(self) -> BucketParams:
        """本桶的速率参数。"""
        return BucketParams(rate_qps=self.rate, burst=int(self.capacity))


class PoolView(TokenBucket):
    """「按模式分池」的合规实现：桶名与排队统计各自独立，**令牌全部来自父池**。

    为什么选它而不是「分池后各自把 rate 减半」（另一种可行修法，R5 的两选项）：

    1. 官方 3 QPS 是**合计类目**（笔记 §3.1「路线规划（合计类目）5,000/日 · 3 QPS」、
       PRD §4.6 明确 `route` 桶由步行 / 骑行 / polyline 三者共用，FR-206 ① 又确认骑行与
       步行共用同一份 5,000 条/日配额）。既然总量是共同的，「除以池数」这种写法只在池数
       恰好等于 2 时正确——M2 之后还有 polyline，池数一变就要重算，忘算一次就是现场
       `status=401` 一路升级到 `status=302` 全天封禁。共享父池让「合计 ≤ rate」成为
       **结构不变量**，不依赖调用方记得做除法。
    2. 减半在单模式下白付一倍延迟：M1 只跑步行，1.25 QPS 会把「冷启动 ≤ 45 s」这条 §2.2
       指标推到临界，而视图模式下单跑一个池照样吃满 2.5 QPS。
    3. `burst` 也按合计管：两个独立池各 burst=2 时瞬时能放行 4 个 > 官方 3 QPS，
       这正是 v1.0 里 `status=401` 的成因；视图共用父池容量，瞬时放行数天然 ≤ 2。

    代价（如实写明，别当成免费午餐）：分池不再**隔离**——步行排队会连带拖慢骑行，
    一个模式卡住另一个也拿不到令牌。这里要的是合规而不是公平；真要隔离，请把父池速率
    调低（`buckets={"route": (...)}`）或换独立父池，而不是指望两个视图各跑全速。
    """

    def __init__(
        self,
        name: str,
        parent: TokenBucket,
        *,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        """绑定父池：`rate` / `capacity` 直接取父池读数，因此 `params` 与看板回显诚实。"""
        self.parent = parent
        # `initial_tokens=0.0`：视图自己永远不攒令牌，`tokens` 只在 `refill()` 同步后有意义。
        super().__init__(
            name,
            parent.rate,
            int(parent.capacity),
            clock=clock,
            sleep=sleep,
            initial_tokens=0.0,
        )

    @property
    def parent_pool(self) -> str:
        """令牌真正来自哪个池（`snapshot()` 里 `pools` 的那一项）。"""
        return self.parent.name

    def refill(self) -> float:
        """把父池位读过来；视图不做第二次补充，否则速率翻倍。"""
        self.rate = self.parent.rate
        self.capacity = self.parent.capacity
        self.tokens = self.parent.refill()
        return self.tokens

    def try_acquire(self, cost: float = 1.0) -> bool:
        """非阻塞取令牌：只在父池上扣，父池没令牌就 False。"""
        granted = self.parent.try_acquire(cost)
        if granted:
            self.acquired_total += 1
        return granted

    async def acquire(self, cost: float = 1.0) -> float:
        """排队取令牌。等待发生在**父池的锁**里 ⇒ 步行与骑行互斥放行，合计不越界。"""
        waited = await self.parent.acquire(cost)
        self.acquired_total += 1
        self.waited_total_s += waited
        return waited

    def snapshot(self) -> dict[str, Any]:
        """视图的看板条目：额外标出 `parent_pool` 与 `shared_tokens`，防止被读成独立额度。"""
        return {**super().snapshot(), "parent_pool": self.parent_pool, "shared_tokens": True}

    @property
    def params(self) -> BucketParams:
        """参数即父池参数（`source` 标成 `parent:<池名>`，日志里能看出令牌归属）。"""
        return BucketParams(
            rate_qps=self.parent.rate,
            burst=int(self.parent.capacity),
            source=f"parent:{self.parent.name}",
        )


class RateLimitRegistry:
    """逻辑桶名 → 令牌池的注册表，参数一律从 `Settings` 读（PRD §4.6：须经 `.env` 覆盖）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        aliases: Mapping[str, str] | None = None,
        split_route_by_mode: bool = False,
        buckets: Mapping[str, tuple[float, int]] | None = None,
    ) -> None:
        """建注册表。

        `split_route_by_mode=True` 时步行 / 骑行各自独立成**桶名**（对应 W2 任务书里
        `route_walking` / `route_riding` 各一桶的写法），但两者都是同一个 route 父池的
        `PoolView`：官方把路线规划列为合计类目 3 QPS（笔记 §3.1、PRD §4.6），早期实现
        让两个独立池各取 2.5 QPS、合计 5 QPS，跑 live 必然换回 `status=401` → `status=302`
        全天封禁。取舍与代价写在 `PoolView` 的 docstring 里。

        `buckets` 直接覆盖 `(rate, burst)`，给 QA 把阈值调成 0.5 QPS 用，不必改环境变量。
        覆盖**父池名**（如 `route`）才有效；覆盖视图名（`route_walking`）会被忽略并记进
        `rate_warnings`——否则「合计 ≤ 官方」这条不变量又被绕回去了。

        构造结束时算一次 `rate_warnings`：把同一官方档位下所有**独立**令牌池的速率加总，
        超过 `OFFICIAL_QPS_BY_TIER` 就逐条报警（默认配置不产生告警，`aliases={}` 这类
        手工放开合并的写法会）。告警只报不改行为，符合「配错方向也不能崩」（PRD §4.6）。
        """
        self.settings = settings or get_settings()
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._overrides: dict[str, tuple[float, int]] = dict(buckets or {})
        self._aliases = dict(DEFAULT_POOL_ALIASES if aliases is None else aliases)
        self._mode_views: dict[str, str] = {}
        if split_route_by_mode:
            # 只把两个模式名从「合并进 route 池」改成「route 池的视图」；
            # direction / routematrix / polyline 仍并进同一个父池——它们同样是合计类目的一部分，
            # 早期实现连它们一起拆开，等于把 3 QPS 的档位放成了 6 个独立池。
            self._mode_views = dict(DEFAULT_MODE_VIEWS)
            for name in self._mode_views:
                self._aliases.pop(name, None)
        self._pools: dict[str, TokenBucket] = {}
        self._views: dict[str, PoolView] = {}
        self.rate_warnings: list[str] = self._check_pool_totals()

    def pool_for(self, service: str, mode: str | None = None) -> str:
        """逻辑桶名最终落到的桶名（分池模式下就是视图名，见 `token_pool_for`）。"""
        logical = logical_bucket_name(service, mode)
        return self._aliases.get(logical, logical)

    def token_pool_for(self, service: str, mode: str | None = None) -> str:
        """令牌实际来自哪个池：`pool_for` 的答案若是视图名，这里给它的父池。

        为什么要分开：`pool_for` 回答「看板上哪个桶在排队」，`token_pool_for` 回答
        「配额实际记在谁头上」。合计类目的 QPS 只跟后者有关，把两者混成一个概念就是
        R5 的根因。
        """
        logical = logical_bucket_name(service, mode)
        view_parent = self._mode_views.get(logical)
        if view_parent is not None:
            return view_parent
        return self._aliases.get(logical, logical)

    def bucket(self, service: str, mode: str | None = None) -> TokenBucket:
        """取（必要时按 Settings 懒建）某个服务对应的令牌桶。

        签名不变；分池模式下返回的是 `PoolView`（`TokenBucket` 的子类），
        调用方的 `acquire / try_acquire / rate / params / snapshot` 用法完全一致。
        """
        logical = logical_bucket_name(service, mode)
        parent_name = self._mode_views.get(logical)
        if parent_name is not None:
            view = self._views.get(logical)
            if view is None:
                view = PoolView(
                    logical, self._ensure_pool(parent_name), clock=self._clock, sleep=self._sleep
                )
                self._views[logical] = view
            return view
        return self._ensure_pool(self._aliases.get(logical, logical))

    def _ensure_pool(self, pool: str) -> TokenBucket:
        """懒建**真实**令牌池（视图不走这里，所以 `_pools` 里只有独立持令牌的池）。"""
        existing = self._pools.get(pool)
        if existing is not None:
            return existing
        rate, burst = self._params_for(pool)
        built = TokenBucket(pool, rate, burst, clock=self._clock, sleep=self._sleep)
        self._pools[pool] = built
        return built

    def _params_for(self, pool: str) -> tuple[float, int]:
        """取某池的 `(rate_qps, burst)`：显式覆盖优先，否则问 `Settings.rate_limit_for()`。"""
        if pool in self._overrides:
            return self._overrides[pool]
        return self.settings.rate_limit_for(_POOL_RATE_KEY.get(pool, "place"))

    def _is_view(self, name: str) -> bool:
        """这个名字是不是「视图」（不独立持令牌）：仅在分池模式下成立。"""
        return bool(self._mode_views) and name in self._mode_views and name not in self._aliases

    def _independent_pools(self) -> set[str]:
        """所有可能成为**独立令牌池**的名字：已知桶名 + 别名两端 + 显式覆盖，视图除外。"""
        candidates = set(_POOL_RATE_KEY) | set(self._aliases) | set(self._overrides)
        pools: set[str] = set()
        for name in candidates:
            if self._is_view(name):
                continue
            pools.add(self._aliases.get(name, name))
        return {pool for pool in pools if not self._is_view(pool)}

    def _check_pool_totals(self) -> list[str]:
        """按官方档位加总独立池速率，越界就产一条告警（R5 缺的那道校验）。"""
        warnings: list[str] = []
        grouped: dict[str, list[str]] = {}
        for pool in self._independent_pools():
            grouped.setdefault(_POOL_RATE_KEY.get(pool, "place"), []).append(pool)
        for tier, members in sorted(grouped.items()):
            cap = OFFICIAL_QPS_BY_TIER.get(tier, OFFICIAL_QPS_DEFAULT)
            members.sort()
            total = sum(self._params_for(pool)[0] for pool in members)
            if total > cap + 1e-9:
                warnings.append(
                    f"{tier} 档独立令牌池合计 {total:.2f} QPS 超过官方 {cap:.0f} QPS"
                    f"（合计类目，笔记 §3.1）：{', '.join(members)}"
                    f"—— 现场会表现为 status=401，持续越界升级成 302 全天封禁"
                )
        for name in sorted(self._overrides):
            if self._is_view(name):
                warnings.append(
                    f"buckets 里的 {name} 覆盖被忽略：它是 {self._mode_views[name]} 池的视图，"
                    "不独立持令牌。要改速率请覆盖父池名。"
                )
        return warnings

    async def acquire(self, service: str, *, mode: str | None = None, cost: float = 1.0) -> float:
        """排队等待该服务的出网令牌，返回等待秒数（0 表示立即放行）。"""
        return await self.bucket(service, mode).acquire(cost)

    def try_acquire(self, service: str, *, mode: str | None = None, cost: float = 1.0) -> bool:
        """非阻塞版取令牌，用于「拿不到就走缓存」这类分支。"""
        return self.bucket(service, mode).try_acquire(cost)

    def snapshot(self) -> dict[str, Any]:
        """全部令牌池的状态。

        `pools` 的 key 是**独立持令牌**的池名（视图不进这里，否则同池两个逻辑名会把看板
        带偏）；`views` 是分池模式下的模式桶，条目里用 `parent_pool` 指明令牌归属；
        `rate_warnings` 是构造期算出的官方档位越界告警。
        """
        return {
            "pools": {name: pool.snapshot() for name, pool in sorted(self._pools.items())},
            "views": {name: view.snapshot() for name, view in sorted(self._views.items())},
            "logical_aliases": dict(sorted(self._aliases.items())),
            "mode_views": dict(sorted(self._mode_views.items())),
            "rate_warnings": list(self.rate_warnings),
            "official_qps_by_tier": dict(sorted(OFFICIAL_QPS_BY_TIER.items())),
        }
