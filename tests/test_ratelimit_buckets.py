"""按服务分桶令牌桶（`infra/ratelimit.py`）的行为契约。

覆盖 M1 卡「BaiduClient：按服务分桶令牌桶」里 QA 能独立验证的部分，共 5 组：

1. 分桶归属：`logical_bucket_name` 的拼写归一 + `pool_for` 的池归属（suggest 并进 place、
   route 家族并进同一个 route 池），以及 `split_route_by_mode` 的显式分池。
2. 速率取数：`route_walking` 必须拿到 route 档（2.5/2）而不是「未知服务 → 最保守 place 档」
   ——这是 `_POOL_RATE_KEY` 存在的理由，回归它等于守住等时圈 5 倍速差。
3. FakeClock 推进下的补充速率与容量上限。
4. QPS 与并发语义：`acquire` 在锁内排队（同池串行放行）、`burst` 决定瞬时放行数、
   等待时长按 `deficit / rate` 精确计费。
5. `try_acquire` 永不阻塞、`cost` 夹到容量、时钟与 sleep 误配时报错而不是永久空转。

2026-10-05 dev 按「谁实现谁修」处理 R5（分池合计 5 QPS > 官方 3 QPS 且无校验）后，第 1 组
新增一组 1b：分池模式下的 `route_walking` / `route_riding` 是同一个 route 父池的 `PoolView`，
令牌、容量与排队锁都归父池 ⇒ 「合计 ≤ rate」由结构保证；同时钉住新加的
`rate_warnings` 官方档位越界校验。原 `test_split_route_by_mode_*` 的断言值全部未变
（桶名 / rate 读数 / 别名形状对调用方兼容），只有用例名与注释里「各自负责把 rate 调半」
那句已被修复取代——那句正是缺陷本身的表述。

真实出网：0——这里只操作令牌桶与假时钟，不发任何 HTTP 请求。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from infra.config import Settings
from infra.errors import CircleDocError
from infra.ratelimit import (
    DEFAULT_MODE_VIEWS,
    MAX_STALL_ROUNDS,
    MIN_RATE_QPS,
    OFFICIAL_QPS_BY_TIER,
    FakeClock,
    PoolView,
    RateLimitRegistry,
    TokenBucket,
    logical_bucket_name,
)

# ---------------------------------------------------------------------------
# 公共构造：数值全部显式传入，避免受本机环境变量影响（QA 要的是可复现断言）
# ---------------------------------------------------------------------------

RATE_DEFAULTS: dict[str, float | int] = {
    "rate_place_qps": 0.5,
    "burst_place": 1,
    "rate_regeo_qps": 0.5,
    "burst_regeo": 1,
    "rate_route_qps": 2.5,
    "burst_route": 2,
    "rate_geocode_qps": 1.0,
    "burst_geocode": 2,
}


def make_settings(**overrides: Any) -> Settings:
    """一份把四档速率钉死的 Settings（显式实参优先于环境变量，断言因此可复现）。"""
    values = dict(RATE_DEFAULTS)
    values.update(overrides)
    return Settings(**values)


class Harness:
    """注册表 + 同一个 FakeClock：`clock` 与 `sleep` 必须成对，否则排队逻辑走不到分支。"""

    def __init__(self, **registry_kwargs: Any) -> None:
        self.clock = FakeClock()
        self.registry = RateLimitRegistry(
            make_settings(),
            clock=self.clock.now,
            sleep=self.clock.sleep,
            **registry_kwargs,
        )

    def acquire(self, service: str, *, mode: str | None = None, cost: float = 1.0) -> Any:
        return self.registry.acquire(service, mode=mode, cost=cost)


def run_async(coroutine: Any) -> Any:
    """在独立事件循环里跑协程，避免测试之间共享 loop 状态。"""
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# 1. 逻辑桶名归一 + 池归属
# ---------------------------------------------------------------------------


def test_logical_bucket_name_normalises_service_spellings() -> None:
    """上层传法不统一（`reverse_geocoding` / `place_search` / 大小写 / 连字符）都归到同一逻辑桶。"""
    assert logical_bucket_name("Geocoding") == "geocode"
    assert logical_bucket_name("geocoding_v3") == "geocode"
    assert logical_bucket_name("reverse-geocoding") == "regeo"
    assert logical_bucket_name(" place_search ") == "place"
    assert logical_bucket_name("place_suggest") == "suggest"
    assert logical_bucket_name("routematrix") == "route"


def test_empty_service_falls_back_to_place_bucket() -> None:
    """空服务名不能造出一个游离桶，否则 `snapshot()` 里会出现无法归因的桶名。"""
    assert logical_bucket_name("") == "place"
    assert logical_bucket_name(None) == "place"  # type: ignore[arg-type]


def test_route_modes_have_distinct_logical_names() -> None:
    """带 mode 时逻辑名区分步行/骑行——看板上要能分别排队，但逻辑名 ≠ 独立额度。"""
    assert logical_bucket_name("route", "walking") == "route_walking"
    assert logical_bucket_name("route", "riding") == "route_riding"
    assert logical_bucket_name("direction", "walking") == "route_walking"
    assert logical_bucket_name("direction") == "route"


def test_walking_and_riding_share_one_pool_by_default() -> None:
    """默认步行/骑行并进同一个 route 池：官方 3 QPS 是**合计**类目，两池各 2.5 就是 5 QPS，
    必然触发 status=401。"""
    registry = Harness().registry
    assert registry.pool_for("route", "walking") == "route"
    assert registry.pool_for("route", "riding") == "route"
    assert registry.bucket("route", "walking") is registry.bucket("route", "riding")
    for service in ("direction", "routematrix", "polyline"):
        assert registry.pool_for(service) == "route"
        assert registry.bucket(service) is registry.bucket("route", "walking")


def test_split_route_by_mode_gives_each_mode_its_own_named_bucket() -> None:
    """显式 `split_route_by_mode=True`（W2 任务书写法）才按模式分桶名。

    ⚠️ 本用例原样钉的是「两个独立池各取 route 全速 2.5 QPS ⇒ 合计 5 QPS」这个缺陷现状
    （QA 回归登记的 R5）。修复选了「分池共享一个父令牌」而不是「各自减半」，因此
    桶名、`rate` 读数与 `logical_aliases` 的可见形状**都没变**（调用方无需改代码），
    变的是令牌归属：见下面 `test_split_mode_buckets_share_one_token_budget` 等新增用例。
    """
    registry = Harness(split_route_by_mode=True).registry
    walking = registry.bucket("route", "walking")
    riding = registry.bucket("route", "riding")
    assert walking is not riding
    assert walking.name == "route_walking"
    assert riding.name == "route_riding"
    assert registry.snapshot()["logical_aliases"].get("route_walking") is None
    # 分池不改速率读数：两桶仍显示 route 档（合规靠父池的令牌归属保证，不靠调低 rate）
    assert (walking.rate, walking.capacity) == (2.5, 2.0)
    assert (riding.rate, riding.capacity) == (2.5, 2.0)


# ---------------------------------------------------------------------------
# 1b. R5：分池必须共享父令牌（官方 3 QPS 是合计类目）
# ---------------------------------------------------------------------------


def test_split_mode_buckets_share_one_token_budget() -> None:
    """🔴 R5 修复口径：`route_walking` / `route_riding` 是同一个 route 父池的视图。

    修复前两个池各 2.5 QPS、合计 5 QPS > 官方「路线规划（合计类目）」3 QPS
    （笔记 §3.1、PRD §4.6、FR-206 ①），照现状跑 live 就是 `status=401` 一路升级到
    `status=302` 全天封禁。选「共享父池」而不是「各自减半」的理由写在 `PoolView`。
    """
    registry = Harness(split_route_by_mode=True).registry
    walking = registry.bucket("route", "walking")
    riding = registry.bucket("route", "riding")
    parent = registry.bucket("route")
    assert isinstance(walking, PoolView) and isinstance(walking, TokenBucket)
    assert walking.parent is parent and riding.parent is parent
    assert walking.parent_pool == "route" == riding.parent_pool
    # 看板上「哪个桶在排队」与「配额记在谁头上」是两个问题，分开答
    assert registry.pool_for("route", "walking") == "route_walking"
    assert registry.token_pool_for("route", "walking") == "route"
    assert registry.token_pool_for("direction", "walking") == "route"
    # 令牌只有一份：把父池抽干，另一个模式立刻拿不到（修复前各池各有 2 个令牌）
    assert walking.try_acquire(2.0) is True
    assert riding.try_acquire() is False
    assert walking.available == 0.0 and riding.available == 0.0


def test_split_mode_alternating_acquires_are_billed_at_parent_rate() -> None:
    """合计速率断言：步行 / 骑行交替各 3 次，等待只能按父池 2.5 QPS 计费。

    修复前两个独立池各跑 2.5 QPS ⇒ 6 次只耗时 0.8 s（实际 7.5 次/秒量级），
    现在与单池一致：初满 2 个令牌，其余 4 个各 0.4 s，合计 1.6 s。
    """

    async def scenario(harness: Harness) -> list[float]:
        return [
            await harness.acquire("route", mode="walking" if index % 2 == 0 else "riding")
            for index in range(6)
        ]

    harness = Harness(split_route_by_mode=True)
    waits = run_async(scenario(harness))
    assert waits == pytest.approx([0.0, 0.0, 0.4, 0.4, 0.4, 0.4])
    assert harness.clock.now() == pytest.approx(1.6)
    assert harness.registry.bucket("route").acquired_total == 6
    assert harness.registry.bucket("route", "walking").acquired_total == 3
    assert harness.registry.bucket("route", "riding").acquired_total == 3


def test_split_mode_burst_is_capped_at_parent_capacity() -> None:
    """瞬时放行数也按合计管：两个模式并发 4 个只应 2 个零等待。
    修复前各池各 burst=2 ⇒ 同一秒放行 4 个 > 官方 3 QPS，这正是 v1.0 撞 401 的成因。"""
    clock = FakeClock()

    async def scenario() -> list[float]:
        registry = RateLimitRegistry(
            make_settings(), clock=clock.now, sleep=clock.sleep, split_route_by_mode=True
        )
        walking = registry.bucket("route", "walking")
        riding = registry.bucket("route", "riding")
        tasks = [walking.acquire(), walking.acquire(), riding.acquire(), riding.acquire()]
        return list(await asyncio.gather(*tasks))

    waits = run_async(scenario())
    assert sum(1 for wait in waits if wait == 0.0) == 2
    assert clock.now() == pytest.approx(0.8)


def test_split_mode_keeps_direction_family_merged_into_the_same_parent() -> None:
    """分池只拆模式，不拆 `direction` / `routematrix` / `polyline`：
    修复前连它们一起从别名表里 pop 掉，等于把 3 QPS 的档位放成 6 个独立池。"""
    registry = Harness(split_route_by_mode=True).registry
    for service in ("direction", "routematrix", "polyline"):
        assert registry.token_pool_for(service) == "route", service
        assert registry.bucket(service) is registry.bucket("route")
    assert registry.bucket("route", "walking").parent is registry.bucket("routematrix")


def test_default_config_produces_no_rate_warnings() -> None:
    """R5 的另一半是「没有任何校验」。加了校验但不能反过来拦死默认配置：
    默认（合并池）与合规分池两种写法都得零告警启动。"""
    assert Harness().registry.rate_warnings == []
    assert Harness(split_route_by_mode=True).registry.rate_warnings == []
    assert Harness(buckets={"place": (0.5, 1), "route": (0.25, 1)}).registry.rate_warnings == []


def test_rate_warnings_flag_independent_pools_over_official_tier() -> None:
    """`aliases={}`（手工取消所有合并）会让 route 家族各建一池 ⇒ 必须报警而不是静默越界。"""
    registry = Harness(aliases={}).registry
    assert len(registry.rate_warnings) == 1
    warning = registry.rate_warnings[0]
    assert warning.startswith("route 档")
    assert "15.00 QPS" in warning and "3 QPS" in warning
    assert "route_walking" in warning and "polyline" in warning
    # place 档即便拆开也没越界（0.5 + 0.5 = 1.0 ≤ 3），不该产生噪声
    assert "place 档" not in warning


def test_rate_warning_when_a_single_pool_exceeds_the_official_tier() -> None:
    """单池超官方档也要报（QA 演练把 route 调到 4 QPS 是常见误配）。"""
    registry = Harness(buckets={"route": (4.0, 4)}).registry
    assert any("route" in warning and "4.00" in warning for warning in registry.rate_warnings)
    assert registry.bucket("route").rate == 4.0  # 只告警、不改数值（配错方向也不能崩）


def test_buckets_override_on_a_view_name_is_ignored_with_warning() -> None:
    """覆盖视图名不生效（否则「合计 ≤ 官方」又被绕回去），但必须留下可读告警。"""
    registry = Harness(split_route_by_mode=True, buckets={"route_walking": (4.0, 4)}).registry
    assert registry.bucket("route", "walking").rate == 2.5
    assert any("route_walking" in warning for warning in registry.rate_warnings)
    # 想改速率就覆盖父池名，两个模式一起生效
    parent_only = Harness(split_route_by_mode=True, buckets={"route": (1.0, 1)}).registry
    assert parent_only.bucket("route", "riding").rate == 1.0
    assert parent_only.rate_warnings == []


def test_snapshot_separates_real_pools_from_mode_views() -> None:
    """看板口径：视图不进 `pools`（同池两个逻辑名重复计数会把看板带偏），
    单独进 `views` 并标出 `parent_pool`。"""
    registry = Harness(split_route_by_mode=True).registry
    registry.bucket("route", "walking")
    registry.bucket("route", "riding")
    registry.bucket("place")
    snap: dict[str, Any] = registry.snapshot()
    assert sorted(snap["pools"]) == ["place", "route"]
    assert sorted(snap["views"]) == ["route_riding", "route_walking"]
    assert snap["views"]["route_walking"]["parent_pool"] == "route"
    assert snap["views"]["route_walking"]["rate_qps"] == 2.5
    assert snap["views"]["route_walking"]["source"] == "parent:route"
    assert snap["mode_views"] == {"route_riding": "route", "route_walking": "route"}
    assert snap["rate_warnings"] == []
    assert snap["official_qps_by_tier"]["route"] == 3.0
    # 未分池时没有视图条目，`pools` 的形状与 M1 收口时一致
    plain = Harness().registry.snapshot()
    assert plain["views"] == {} and plain["mode_views"] == {}


def test_official_tier_and_mode_view_tables_are_self_consistent() -> None:
    """别名 / 视图 / 官方档位三张表的自检：登记错名字不会抛错，
    只会让视图绑到一个没人建的池上、或让某个档位漏检越界，所以逐条钉住。"""
    assert set(DEFAULT_MODE_VIEWS) == {"route_walking", "route_riding"}
    assert set(DEFAULT_MODE_VIEWS.values()) <= set(OFFICIAL_QPS_BY_TIER)
    assert set(OFFICIAL_QPS_BY_TIER) == {"place", "regeo", "geocode", "route"}
    # 个人开发者全线 3 QPS（笔记 §3.1）：任何一档写高都会让校验形同虚设
    assert set(OFFICIAL_QPS_BY_TIER.values()) == {3.0}
    # 视图的父池必须能从 `_POOL_RATE_KEY` 认到档位，否则速率落回 place 档
    registry = Harness(split_route_by_mode=True).registry
    for view_name, parent_name in DEFAULT_MODE_VIEWS.items():
        mode = view_name.removeprefix("route_")
        assert registry.bucket("route", mode).parent_pool == parent_name
        assert registry.token_pool_for("route", mode) == parent_name


def test_suggest_shares_place_pool() -> None:
    """地点输入提示与地点检索共用 100 次/日配额：两个独立 0.5 QPS 桶等于把配额算重一倍。"""
    registry = Harness().registry
    assert registry.pool_for("suggest") == "place"
    assert registry.bucket("suggest") is registry.bucket("place")


def test_regeo_and_geocode_are_their_own_pools() -> None:
    registry = Harness().registry
    assert registry.pool_for("regeo") == "regeo"
    assert registry.pool_for("geocoding") == "geocode"
    assert registry.bucket("regeo") is not registry.bucket("geocode")


# ---------------------------------------------------------------------------
# 2. 速率取数：池名 → Settings 档位
# ---------------------------------------------------------------------------


def test_route_walking_uses_route_rate_not_place_rate() -> None:
    """回归守卫：`route_walking` 直接递给 `Settings.rate_limit_for()` 会落到 place 档（0.5/1），
    等时圈慢 5 倍且毫无线索——`_POOL_RATE_KEY` 就是把这一步换算掉的那层。"""
    bucket = Harness().registry.bucket("route", "walking")
    assert (bucket.rate, bucket.capacity) == (2.5, 2.0)
    assert bucket.params.as_dict() == {"rate_qps": 2.5, "burst": 2, "source": "settings"}


def test_suggest_bucket_inherits_place_rate() -> None:
    bucket = Harness().registry.bucket("suggest")
    assert (bucket.rate, bucket.capacity) == (0.5, 1.0)


def test_unknown_service_falls_back_to_most_conservative_rate() -> None:
    """未登记服务按最保守的 place 档，宁可慢也不要越权提速。"""
    registry = Harness().registry
    assert registry.pool_for("mystery_service") == "mystery_service"
    bucket = registry.bucket("mystery_service")
    assert (bucket.rate, bucket.capacity) == (0.5, 1.0)


def test_buckets_override_wins_over_settings() -> None:
    """QA 把阈值调成 0.5 QPS 连发 20 个时走 `buckets=`，不必改环境变量。"""
    registry = Harness(buckets={"place": (0.5, 1), "route": (0.25, 1)}).registry
    assert registry.bucket("place").rate == 0.5
    walking = registry.bucket("route", "walking")
    assert (walking.rate, walking.capacity) == (0.25, 1.0)


def test_burst_larger_than_rate_is_accepted_silently() -> None:
    """PRD §4.6 正文与它自己的表口径冲突（place 0.5/1、geocode 1.0/2）：这里刻意不校验，
    否则「零配置启动」这条约定会被自己的默认配置拦死。"""
    geocode = Harness().registry.bucket("geocode")
    assert geocode.capacity > geocode.rate
    assert (geocode.rate, geocode.capacity) == (1.0, 2.0)


def test_rate_floor_and_capacity_floor() -> None:
    """rate<=0 让桶永不回满（第一个请求永久挂起）；burst<1 让第一个令牌都放不进去。"""
    assert TokenBucket("zero", 0.0, 1).rate == MIN_RATE_QPS
    assert TokenBucket("neg", -5.0, 1).rate == MIN_RATE_QPS
    assert TokenBucket("tiny", 1.0, 0).capacity == 1.0
    assert TokenBucket("neg-burst", 1.0, -3).capacity == 1.0


# ---------------------------------------------------------------------------
# 3. FakeClock 推进下的补充速率
# ---------------------------------------------------------------------------


def test_refill_tracks_elapsed_time_and_caps_at_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket("t", 1.0, 3, clock=clock.now, sleep=clock.sleep)
    assert bucket.available == 3.0  # 初始满桶
    assert bucket.try_acquire(3.0) is True
    assert bucket.available == 0.0

    clock.advance(1.0)
    assert bucket.available == pytest.approx(1.0)
    clock.advance(2.0)
    assert bucket.available == pytest.approx(3.0)
    # 长时间空闲不得无限攒令牌（否则一次分析可以把全日配额瞬发放行）
    clock.advance(3600.0)
    assert bucket.available == pytest.approx(3.0)


def test_refill_clamps_negative_elapsed_instead_of_unspending() -> None:
    """注入的假时钟被误设回退时，增量必须夹成 0：不得把已攒的令牌倒扣成负数。"""
    times = [100.0]
    bucket = TokenBucket("t", 1.0, 5, clock=lambda: times[0], initial_tokens=0.0)
    assert bucket.available == pytest.approx(0.0)
    times[0] = 90.0  # 时钟倒退
    assert bucket.available == pytest.approx(0.0)
    times[0] = 93.0  # 以倒退后的读数为新基准重新计增量
    assert bucket.available == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# 4. QPS 与并发语义
# ---------------------------------------------------------------------------


def test_sequential_acquire_bills_exact_wait() -> None:
    """0.5 QPS / burst 1：第二、三个请求各等满 2 s，等待时长就是 `deficit / rate`。"""

    async def scenario(harness: Harness) -> list[float]:
        return [await harness.acquire("place") for _ in range(3)]

    harness = Harness()
    waits = run_async(scenario(harness))
    assert waits == pytest.approx([0.0, 2.0, 2.0])
    assert harness.clock.now() == pytest.approx(4.0)
    bucket = harness.registry.bucket("place")
    assert bucket.acquired_total == 3
    assert bucket.waited_total_s == pytest.approx(4.0)


def test_burst_allows_that_many_immediate_passes_under_concurrency() -> None:
    """burst 是「瞬时并发放行数」：容量 2 时并发 4 个只应前 2 个零等待，其余必须排队。"""
    clock = FakeClock()

    async def scenario() -> list[float]:
        bucket = TokenBucket("route", 1.0, 2, clock=clock.now, sleep=clock.sleep)
        return list(await asyncio.gather(*(bucket.acquire() for _ in range(4))))

    waits = run_async(scenario())
    assert sum(1 for w in waits if w == 0.0) == 2
    assert sum(w for w in waits if w > 0.0) == pytest.approx(2.0)
    # 同池串行：后 2 个各等 1 s，总耗时 2 s
    assert clock.now() == pytest.approx(2.0)


def test_concurrent_acquires_never_double_spend_tokens() -> None:
    """两个协程同时读到「还剩 1 个令牌」就会双双放行、实际速率翻倍
    （v1.0 里 burst 过大导致 status=401 的同一种错）。"""
    clock = FakeClock()

    async def scenario() -> TokenBucket:
        bucket = TokenBucket("place", 0.5, 1, clock=clock.now, sleep=clock.sleep)
        await asyncio.gather(*(bucket.acquire() for _ in range(6)))
        return bucket

    bucket = run_async(scenario())
    assert bucket.acquired_total == 6
    # 初满 1 个、0.5 QPS ⇒ 其余 5 个令牌各需 2 s，总时长不得少于 10 s
    assert clock.now() == pytest.approx(10.0)


def test_acquire_cost_and_zero_cost() -> None:
    """`acquire(cost)` 支持一次扣多个令牌；cost<=0 视作 0 成本、不等待也不消耗。"""

    async def scenario() -> dict[str, float]:
        clock = FakeClock()
        bucket = TokenBucket("route", 1.0, 4, clock=clock.now, sleep=clock.sleep)
        first = await bucket.acquire(3.0)
        left = bucket.available
        zero = await bucket.acquire(0.0)
        second = await bucket.acquire(2.0)
        return {"first": first, "left": left, "zero": zero, "second": second, "now": clock.now()}

    out = run_async(scenario())
    assert out["first"] == 0.0
    assert out["left"] == pytest.approx(1.0)
    assert out["zero"] == 0.0
    # 还差 1 个令牌、1.0 QPS ⇒ 恰好等 1 s
    assert out["second"] == pytest.approx(1.0)
    assert out["now"] == pytest.approx(1.0)


def test_registry_acquire_delegates_to_the_right_pool() -> None:
    """注册表层 `acquire` 的等待必须计到对应池，而不是各池各记一份。"""

    async def scenario(harness: Harness) -> list[float]:
        return [
            await harness.acquire("place"),
            await harness.acquire("suggest"),  # 同池：这次必须排队
            await harness.acquire("geocode"),  # 另一池：满桶，零等待
        ]

    harness = Harness()
    waits = run_async(scenario(harness))
    assert waits[0] == 0.0
    assert waits[1] == pytest.approx(2.0)
    assert waits[2] == 0.0
    assert harness.registry.bucket("place").acquired_total == 2
    assert harness.registry.bucket("geocode").acquired_total == 1


# ---------------------------------------------------------------------------
# 5. try_acquire 非阻塞 + 误配守卫
# ---------------------------------------------------------------------------


def test_try_acquire_is_non_blocking() -> None:
    """`try_acquire` 用于「拿不到就走缓存」的分支：绝不 await，取不到直接 False。"""
    clock = FakeClock()
    bucket = TokenBucket("place", 0.5, 1, clock=clock.now, sleep=clock.sleep)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    assert clock.now() == 0.0  # 时钟没被推进 ⇒ 确实没有等待
    clock.advance(2.0)
    assert bucket.try_acquire() is True


def test_try_acquire_cost_is_clamped_to_capacity() -> None:
    """cost 超过容量时夹到容量：否则一次 cost=50 会把桶永久饿死。"""
    clock = FakeClock()
    bucket = TokenBucket("route", 1.0, 2, clock=clock.now, sleep=clock.sleep)
    assert bucket.try_acquire(50.0) is True
    assert bucket.available == pytest.approx(0.0)
    assert bucket.try_acquire(0.0) is True
    assert bucket.try_acquire(-4.0) is True


def test_registry_try_acquire_shares_pool_state() -> None:
    registry = Harness().registry
    assert registry.try_acquire("place") is True
    assert registry.try_acquire("suggest") is False  # 同池，令牌已被取走


def test_mismatched_clock_and_sleep_raises_instead_of_spinning() -> None:
    """`clock` 与 `sleep` 配成两个不同 FakeClock 时桶位永不回升。
    宁可在 `MAX_STALL_ROUNDS` 轮后抛一条说清原因的异常，也不要 100% CPU 空转的卡死进程。"""
    quiet_clock = FakeClock()
    loud_clock = FakeClock()

    async def scenario() -> None:
        bucket = TokenBucket(
            "place",
            0.5,
            1,
            clock=quiet_clock.now,
            sleep=loud_clock.sleep,
            initial_tokens=0.0,
        )
        await bucket.acquire()

    with pytest.raises(CircleDocError) as excinfo:
        run_async(scenario())
    detail = excinfo.value.detail
    assert detail["bucket"] == "place"
    assert detail["needed"] == 1.0
    assert quiet_clock.now() == 0.0
    # 每轮 2 s、容忍 3 轮后报错，不是无限循环
    assert loud_clock.now() == pytest.approx(MAX_STALL_ROUNDS * 2.0)


def test_registry_snapshot_shape_for_health_readout() -> None:
    """`/api/health` 与日志按池名读取；同池两个逻辑名重复计数会把看板带偏。"""
    registry = Harness().registry
    registry.bucket("place")
    registry.bucket("suggest")  # 同池，不得多出一个池条目
    registry.bucket("route", "riding")
    snap: dict[str, Any] = registry.snapshot()
    assert sorted(snap["pools"]) == ["place", "route"]
    assert snap["logical_aliases"]["suggest"] == "place"
    assert snap["logical_aliases"]["route_riding"] == "route"
    place = snap["pools"]["place"]
    assert place["name"] == "place"
    assert place["rate_qps"] == 0.5
    assert place["burst"] == 1
    assert {"available_tokens", "waited_total_s", "acquired_total"} <= set(place)
