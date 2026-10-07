"""services/route_probe.py 的单测（M2 批量算路出网层）。

🔴 零真实出网：全部走 `FakeClient`（假 `BaiduClient`）。假 client 的记账口径逐字复刻
`infra/baidu_client.py:797-833` 的真实实现——**一次 `route_matrix` 调用 = 1 个 HTTP 往返
+ `origins × destinations` 个配额单位**，一次 `direction` = 1 个往返 + 1 个配额单位，
`place_calls` 恒 0。所以这里断言出来的 `quota_units` / `http_requests` 与真实链路同口径。

覆盖 PRD §4.2 / §3.2 FR-201~206 的出网层验收点：
  ① 60 个探测点 → 切成 2 批、`quota_units=60` 而 `http_requests=2`（两栏必须分开报）；
  ② `status=2001` → `blocked_official`；空结果 → `blocked_timeout`（两个标签不混用）；
  ③ 缺格进 `missing_cells` 且不抛异常；
  ④ polyline 懒加载第 13 次被拒；
  ⑤ walking / riding 分栏计数；
  ⑥ 预算不足显式截停并带原因（步行与骑行共用 5,000 条/日）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from infra.baidu_client import BaiduClient, BaiduResult, RouteMatrixResult
from infra.metrics import Metrics
from services.route_probe import (
    BLOCKED_OFFICIAL,
    BLOCKED_TIMEOUT,
    MatrixProbeResult,
    ProbePoint,
    RouteProbe,
    official_direction_signals,
    official_matrix_signals,
    route_budget_text,
    split_batches,
)

ORIGIN = (31.2304, 121.4737)
"""一个 `(lat, lng)` 形式的中心点（坐标顺序按笔记 §2.5：纬度在前）。"""


# --------------------------------------------------------------------------- 造数据
def make_points(count: int) -> list[tuple[float, float]]:
    return [(31.23 + index * 0.001, 121.47 + index * 0.001) for index in range(count)]


def matrix_result(
    mode: str,
    destinations: list[Any],
    *,
    status: int = 0,
    rows: int | None = None,
    duration_base: int = 300,
    cell_extra: dict[str, Any] | None = None,
    http_requests: int = 1,
    chunks_sent: int = 1,
    blocked: bool = False,
    budget_exhausted: bool = False,
    unreachable: bool = False,
    from_cache: bool = False,
) -> RouteMatrixResult:
    """按真实 `route_matrix` 的口径造一个结果对象（一维 result[] 摊成 1×N 网格）。

    `rows` 控制官方实际回了几个元素：小于目的地数就是「缺格」，0 就是「空结果」。
    """
    total = len(destinations)
    filled = total if rows is None else max(0, min(int(rows), total))
    cells: list[dict[str, Any] | None] = []
    for index in range(total):
        if index >= filled:
            cells.append(None)
            continue
        cell: dict[str, Any] = {
            "status": status,
            "duration": {"value": duration_base + index * 10},
            "distance": {"value": 500 + index * 10},
        }
        if cell_extra:
            cell.update(cell_extra)
        cells.append(cell)
    lines = total
    return RouteMatrixResult(
        service="routematrix",
        ok=filled > 0 and status == 0,
        status=status,
        data={"result": [cell for cell in cells if cell is not None]},
        attempts=1,
        http_requests=http_requests,
        quota_units=lines,
        route_lines=lines,
        place_calls=0,
        mode=mode,
        from_cache=from_cache,
        blocked=blocked,
        budget_exhausted=budget_exhausted,
        partial=filled < total,
        unreachable=unreachable,
        origins=1,
        destinations=total,
        chunks_sent=chunks_sent,
        requested_cells=total,
        cells=[cells],
    )


def direction_result(
    *,
    status: int = 0,
    paths: tuple[str, ...] = ("121.47,31.23;121.48,31.24",),
    duration: int = 900,
    distance: int = 1600,
    link_attr: Any = None,
    restrictions_status: Any = None,
    lite: bool = False,
    mode: str = "walking",
) -> BaiduResult:
    """按 direction v2 / directionlite v1 的形状造结果对象。

    v2 的步骤在 `result.routes[0].steps`，lite 的在 `result.steps`——两处都要能读，
    因为 FR-203 的懒加载和 PRD §4.2 的 `status=7` 判据分别来自这两种响应。
    """
    steps: list[dict[str, Any]] = []
    for path in paths:
        step: dict[str, Any] = {"path": path, "distance": distance}
        if link_attr is not None:
            step["links"] = [{"attr": link_attr}]
        steps.append(step)
    route: dict[str, Any] = {"duration": duration, "distance": distance, "steps": steps}
    if restrictions_status is not None:
        route["restrictions_status"] = restrictions_status
    result: dict[str, Any] = route if lite else {"routes": [route]}
    data: dict[str, Any] = {"status": status, "result": result}
    return BaiduResult(
        service="direction",
        ok=status == 0 and bool(paths),
        status=status,
        data=data,
        attempts=1,
        http_requests=1,
        quota_units=1,
        route_lines=1,
        place_calls=0,
        mode=mode,
    )


class FakeClient:
    """零出网的假 client：只实现 `RouteProbe` 依赖的三个方法，记账口径照抄真实实现。

    `matrix_responder` / `direction_responder` 可以是可调用对象（按批返回结果或异常），
    也可以直接就是一个结果对象或异常实例——后者用来表达「这个调用永远这样回」。
    """

    def __init__(
        self,
        matrix_responder: Any = None,
        *,
        direction_responder: Any = None,
        budget: dict[str, Any] | None = None,
        budget_responder: Any = None,
    ) -> None:
        self.matrix_calls: list[dict[str, Any]] = []
        self.direction_calls: list[dict[str, Any]] = []
        self.matrix_responder = matrix_responder
        self.direction_responder = direction_responder
        self.budget = budget
        self.budget_responder = budget_responder
        self.budget_queries: list[tuple[str | None, int]] = []

    @staticmethod
    def _resolve(responder: Any, *args: Any) -> Any:
        if responder is None:
            return None
        return responder(*args) if callable(responder) else responder

    async def route_matrix(self, mode, origins, destinations, **kwargs) -> RouteMatrixResult:
        origin_list = list(origins)
        dest_list = list(destinations)
        self.matrix_calls.append(
            {"mode": mode, "origins": origin_list, "destinations": dest_list, "kwargs": kwargs}
        )
        batch_index = len(self.matrix_calls)
        out = self._resolve(self.matrix_responder, mode, origin_list, dest_list, batch_index)
        if out is None:
            out = matrix_result(mode, dest_list)
        if isinstance(out, BaseException):
            raise out
        return out

    async def direction(self, mode, origin, destination, **kwargs) -> BaiduResult:
        self.direction_calls.append(
            {"mode": mode, "origin": origin, "destination": destination, "kwargs": kwargs}
        )
        index = len(self.direction_calls)
        out = self._resolve(self.direction_responder, mode, origin, destination, index)
        if out is None:
            out = direction_result(mode=mode)
        if isinstance(out, BaseException):
            raise out
        return out

    def route_budget_state(self, mode=None, *, next_lines=0) -> dict[str, Any]:
        self.budget_queries.append((mode, int(next_lines)))
        if self.budget_responder:
            return dict(self.budget_responder(mode, int(next_lines)))
        if self.budget is not None:
            return dict(self.budget)
        # 默认：额度充足（真实 client 的 `fits_next_chunk` 判据是 remaining ≥ next_lines）。
        return {"mode": mode, "budget": 5000, "used": 0, "remaining": 5000, "fits_next_chunk": True}


def run(coroutine: Any) -> Any:
    """跑协程。本仓库没装 pytest-asyncio（只有 anyio），用 `asyncio.run` 包一层最稳。"""
    return asyncio.run(coroutine)


# --------------------------------------------------------------------------- ① 切批 + 两栏
def test_60_probe_points_become_two_batches_with_two_separate_counters() -> None:
    """核心断言：60 点 → 2 批；`quota_units=60` 而 `http_requests=2`。

    PRD §4.2 第 267 行：只报 `http_requests` 会把配额消耗低估一个数量级，
    所以这一条同时钉住两个数字，而不是钉它们的和。
    """
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    result = run(probe.probe(ORIGIN, make_points(60)))

    assert isinstance(result, MatrixProbeResult)
    assert len(client.matrix_calls) == 2, "60 个目的地必须按 ≤50 切成 2 批"
    assert [len(call["destinations"]) for call in client.matrix_calls] == [50, 10]
    assert result.batch_count == 2
    assert result.quota_units == 60, "配额口径：起×终 = 60"
    assert result.http_requests == 2, "往返口径：切分后只有 2 次"
    assert result.quota_units != result.http_requests, "两栏不许合并成一个总数"
    assert result.requested_cells == 60
    assert result.completed_cells == 60
    assert result.missing_cells == []
    assert result.partial is False and result.needs_interpolation is False
    assert result.usage()["quota_units"] == 60 and result.usage()["http_requests"] == 2


def test_every_batch_respects_the_official_50_row_cap() -> None:
    client = FakeClient()
    probe = RouteProbe(client, mode="riding")
    run(probe.probe(ORIGIN, make_points(137)))

    assert all(len(call["destinations"]) <= 50 for call in client.matrix_calls)
    assert len(client.matrix_calls) == 3
    assert client.matrix_calls[0]["mode"] == "riding"


def test_step2_round_of_12_directions_costs_one_http_and_12_quota() -> None:
    """PRD §4.2 Step2：一轮 12 个中点打包成 1 次批量请求（12 条路线配额、1 个往返）。"""
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    result = run(probe.probe(ORIGIN, make_points(12)))

    assert len(client.matrix_calls) == 1
    assert (result.quota_units, result.http_requests) == (12, 1)


def test_batch_size_override_stays_under_the_cap() -> None:
    client = FakeClient()
    probe = RouteProbe(client, batch_size=12)
    result = run(probe.probe(ORIGIN, make_points(36)))

    assert len(client.matrix_calls) == 3
    assert result.batch_count == 3
    assert (result.quota_units, result.http_requests) == (36, 3)
    assert split_batches(list(range(60)), 50) == [list(range(50)), list(range(50, 60))]
    assert split_batches([], 50) == []


def test_durations_are_read_back_by_input_order() -> None:
    """下标错位是这里最危险的静默错误：A 方向的耗时被当成 B 方向用。"""
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    points = make_points(60)
    result = run(probe.probe(ORIGIN, points))

    assert result.duration_s(0) == 300
    assert result.duration_s(49) == 300 + 49 * 10
    assert result.duration_s(50) == 300, "第 2 批从 0 重新开始，回填要接上全局下标"
    assert result.distance_m(59) == 500 + 9 * 10
    assert [outcome.index for outcome in result.outcomes] == list(range(60))


def test_probe_point_metadata_is_carried_through_for_attribution() -> None:
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    points = [
        ProbePoint(index=99, position=ORIGIN, azimuth_deg=30.0, radial_m=600.0, label="E"),
        ProbePoint(index=7, position=(31.24, 121.48), azimuth_deg=60.0, radial_m=900.0, label="NE"),
    ]
    result = run(probe.probe(ORIGIN, points))

    # 列表顺序即下标（自带的 index 一律重写），几何层按顺序取就不会错位的两套口径
    assert [o.index for o in result.outcomes] == [0, 1]
    assert result.outcome(1).point.azimuth_deg == 60.0
    assert result.outcome(0).point.label == "E"
    assert result.as_dict()["cells"][1]["azimuth_deg"] == 60.0


def test_riding_quality_switch_params_are_forwarded_only_for_riding() -> None:
    client = FakeClient()
    probe = RouteProbe(client, mode="riding", road_prefer=3, riding_type=4, ret_straight_dist=1)
    run(probe.probe(ORIGIN, make_points(3)))

    kwargs = client.matrix_calls[0]["kwargs"]
    assert kwargs == {"ret_straight_dist": 1, "riding_type": 4, "road_prefer": 3}

    walking_client = FakeClient()
    walking = RouteProbe(walking_client, road_prefer=3, riding_type=4)
    run(walking.probe(ORIGIN, make_points(2)))
    assert walking_client.matrix_calls[0]["kwargs"] == {}, "步行不该带骑行专属参数"


# --------------------------------------------------------------------------- ② blocked 归类
def test_status_2001_is_blocked_official_not_blocked_timeout() -> None:
    """FR-206 硬事实②：`status=2001`（无骑行路线）是官方硬信号，不许归到「没测准」。"""

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        out = matrix_result(mode, dests, status=2001, rows=0)
        out.ok = False
        return out

    client = FakeClient(responder)
    probe = RouteProbe(client, mode="riding")
    result = run(probe.probe(ORIGIN, make_points(12)))

    assert result.blocked_official_indices == list(range(12))
    assert result.blocked_timeout_indices == []
    assert result.completed_cells == 0
    assert all("status=2001" in o.signals for o in result.outcomes)
    assert result.outcome(0).blocked_kind == BLOCKED_OFFICIAL
    assert result.usage()["blocked_official"] == 12


def test_restrictions_status_bits_are_blocked_official() -> None:
    for bit, label in ((0x01, "0x01"), (0x02, "0x02")):

        def responder(
            mode: str, origins: list[Any], dests: list[Any], index: int, bit: Any = bit
        ) -> RouteMatrixResult:
            return matrix_result(mode, dests, cell_extra={"restrictions_status": bit})

        probe = RouteProbe(FakeClient(responder), mode="riding")
        result = run(probe.probe(ORIGIN, make_points(6)))
        assert result.blocked_official_indices == list(range(6))
        assert result.blocked_timeout_indices == []
        assert f"restrictions_status={label}" in result.outcome(0).signals


def test_empty_batch_is_blocked_timeout_not_blocked_official() -> None:
    """空结果（`status=0` 但一行没回）= 我没测准，属 blocked_timeout。"""

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        return matrix_result(mode, dests, rows=0, chunks_sent=0)

    probe = RouteProbe(FakeClient(responder), mode="walking")
    result = run(probe.probe(ORIGIN, make_points(12)))

    assert result.blocked_timeout_indices == list(range(12))
    assert result.blocked_official_indices == []
    assert set(result.outcome(0).signals) == {"empty_result", "nothing_sent"}
    assert result.partial is True and result.needs_interpolation is True
    assert result.ok is False


def test_missing_cells_land_in_missing_cells_without_raising() -> None:
    """缺格不抛异常（client 既有契约：额度打满与不可达也返回结果对象）。"""

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        return matrix_result(mode, dests, rows=3)

    client = FakeClient(responder)
    probe = RouteProbe(client, mode="walking")
    result = run(probe.probe(ORIGIN, make_points(6)))

    assert result.missing_cells == [3, 4, 5]
    assert result.completed_cells == 3
    assert result.completion_ratio == 0.5
    assert result.blocked_timeout_indices == [3, 4, 5]
    assert all("missing_cell" in o.signals for o in result.outcomes[3:])
    assert result.partial is True and result.needs_interpolation is True


def test_client_exception_never_propagates() -> None:
    client = FakeClient(RuntimeError("routematrix 503"))
    probe = RouteProbe(client, mode="walking")
    result = run(probe.probe(ORIGIN, make_points(4)))

    assert result.missing_cells == [0, 1, 2, 3]
    assert result.blocked_timeout_indices == [0, 1, 2, 3]
    assert result.blocked_official_indices == []
    assert "client_error=RuntimeError" in result.outcome(0).signals
    assert result.errors and result.errors[0]["error"] == "RuntimeError"


def test_client_internal_budget_block_is_not_labelled_blocked() -> None:
    """client 内部被额度拦停：保留已测数据，缺的格归「没发出/额度」，不冒充 blocked。"""

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        out = matrix_result(mode, dests, rows=2, blocked=True, budget_exhausted=True)
        out.blocked_reason = "route_lines 预算已用尽"
        return out

    client = FakeClient(responder)
    probe = RouteProbe(client, mode="walking")
    result = run(probe.probe(ORIGIN, make_points(4)))

    assert result.blocked_official_indices == []
    assert result.blocked_timeout_indices == [], "额度截停不是「路不通」也不是「没测准」"
    assert result.missing_cells == [2, 3]
    assert result.outcome(2).signals == ["route_lines 预算已用尽"]
    assert result.partial is True and result.needs_interpolation is True
    assert client.matrix_calls, "这一批确实发出去了，是 client 内部停的"


def test_unreachable_batch_is_blocked_timeout() -> None:
    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        out = matrix_result(mode, dests, rows=0, unreachable=True, chunks_sent=0)
        out.partial = True
        return out

    probe = RouteProbe(FakeClient(responder), mode="walking")
    result = run(probe.probe(ORIGIN, make_points(2)))
    assert result.blocked_timeout_indices == [0, 1]
    assert "unreachable" in result.outcome(0).signals


def test_blocked_kind_is_never_inferred_from_duration() -> None:
    """耗时再大也不许判 blocked：顺畅方向只是需要 extrapolated（几何层负责）。"""

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        return matrix_result(mode, dests, duration_base=99999)

    probe = RouteProbe(FakeClient(responder), mode="walking")
    result = run(probe.probe(ORIGIN, make_points(3)))
    assert result.blocked_official_indices == [] and result.blocked_timeout_indices == []
    assert result.measured_indices == [0, 1, 2]


def test_pure_signal_helpers_do_not_invent_codes() -> None:
    measured_cell = {"status": 0, "duration": {"value": 1}}
    assert official_matrix_signals(batch_status=0, cell=measured_cell) == []
    assert official_matrix_signals(batch_status=None, cell={"status": 3}) == []
    assert official_matrix_signals(batch_status=None, cell={"restrictions_status": "0x02"}) == [
        "restrictions_status=0x02"
    ]
    assert official_matrix_signals(batch_status=7, cell=None) == ["status=7"]
    assert official_direction_signals(BaiduResult(status=7), lite=False) == []
    assert official_direction_signals(BaiduResult(status=7), lite=True) == [
        "directionlite.status=7"
    ]


# --------------------------------------------------------------------------- ③ 三栏分栏
def test_walking_and_riding_are_counted_in_separate_columns() -> None:
    """FR-206 硬事实①：双模式即当日 quota_units 翻倍，报告必须能分开回答「花在哪一轮」。"""
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")

    walking = run(probe.probe(ORIGIN, make_points(12)))
    riding = run(probe.probe(ORIGIN, make_points(10), mode="riding"))
    usage = probe.usage()

    assert walking.quota_units == 12 and riding.quota_units == 10
    assert usage["by_mode"]["walking"]["quota_units"] == 12
    assert usage["by_mode"]["riding"]["quota_units"] == 10
    assert usage["quota_units"] == 22, "配额口径两栏相加 = 22"
    assert usage["http_requests"] == 2, "往返口径只有 2 次"
    assert usage["place_calls"] == 0, "本层不发地点检索（D5 / FR-206 硬事实③）"
    assert usage["by_mode"]["walking"]["matrix_cells_measured"] == 12
    assert usage["by_mode"]["riding"]["matrix_cells_measured"] == 10


def test_three_columns_are_never_merged_into_one_number() -> None:
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    run(probe.probe(ORIGIN, make_points(60)))
    usage = probe.usage()

    for key in ("quota_units", "http_requests", "place_calls"):
        assert key in usage
    assert set(usage["by_mode"]) == {"walking"}
    assert sum(b["quota_units"] for b in usage["by_mode"].values()) == usage["quota_units"]
    assert sum(b["http_requests"] for b in usage["by_mode"].values()) == usage["http_requests"]
    assert usage["place_calls"] == usage["by_mode"]["walking"]["place_calls"] == 0


def test_two_columns_hold_against_the_real_client_with_mock_transport() -> None:
    """用**真实** `BaiduClient` + 注入 `httpx.MockTransport` 复核两栏（仍然零出网）。

    假 client 只能证明本层没算错账；这条证明本层的口径与 `infra/baidu_client.py:797-833`
    的真实实现一致：60 个点在真实链路上就是 `quota_units=60` / `http_requests=2`，差 30 倍
    ——正是 PRD §4.2 第 267 行说的「只报 `http_requests` 会把配额消耗低估一个数量级」。
    `MockTransport` 不建任何连接，AK 是假串，`metrics=` 注入独立累加器以免污染进程级计数。
    """
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        params = dict(request.url.params)
        dests = [item for item in params.get("destinations", "").split("|") if item]
        rows = [
            {"status": 0, "duration": {"value": 300 + index * 10}, "distance": {"value": 600}}
            for index in range(len(dests))
        ]
        return httpx.Response(200, json={"status": 0, "result": rows})

    async def scenario() -> tuple[MatrixProbeResult, dict[str, Any]]:
        client = BaiduClient(
            ak="fake-ak-for-mock-transport-only",
            transport=httpx.MockTransport(handler),
            metrics=Metrics(label="route-probe-test"),
            mode_count=1,
        )
        try:
            result = await RouteProbe(client, mode="walking").probe(ORIGIN, make_points(60))
            return result, client.usage()
        finally:
            await client.aclose()

    result, client_usage = run(scenario())

    assert paths == ["/routematrix/v2/walking", "/routematrix/v2/walking"]
    assert (result.quota_units, result.http_requests) == (60, 2)
    assert result.completed_cells == 60 and result.missing_cells == []
    assert result.blocked_official_indices == [] and result.blocked_timeout_indices == []
    assert client_usage["quota_units"] == 60, "client 侧的配额口径与本层一致"
    assert client_usage["http_requests"] == 2
    assert client_usage["place_calls"] == 0
    assert client_usage["route_lines"] == {"walking": 60, "riding": 0}


def test_probe_does_not_sleep_or_fan_out() -> None:
    """3 QPS 由 client 的桶负责；本层出现 sleep / gather 就是绕过了限流。"""

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("route_probe 不许自己 sleep 或自己发并发请求（条款 3.2.3(ii)）")

    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    original_sleep, original_gather = asyncio.sleep, asyncio.gather
    asyncio.sleep = forbidden  # type: ignore[assignment]
    asyncio.gather = forbidden  # type: ignore[assignment]
    try:
        result = run(probe.probe(ORIGIN, make_points(120)))
    finally:
        asyncio.sleep, asyncio.gather = original_sleep, original_gather  # type: ignore[assignment]

    assert len(client.matrix_calls) == 3, "必须顺序 await，一批发完才发下一批"
    assert result.quota_units == 120


def test_budget_queries_use_the_chunk_size_as_next_lines() -> None:
    client = FakeClient()
    probe = RouteProbe(client, mode="walking")
    run(probe.probe(ORIGIN, make_points(60)))

    assert client.budget_queries == [("walking", 50), ("walking", 10)], "预检按批大小问，不看总数"


# --------------------------------------------------------------------------- ④ 预算截停
def test_budget_guard_stops_before_sending_and_returns_the_reason() -> None:
    """预算不足：不发、显式截停、原因回传（供降级文案），已测数据一律不静默丢。"""
    calls: list[int] = []

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        calls.append(index)
        return matrix_result(mode, dests)

    def budget(mode: str | None, next_lines: int) -> dict[str, Any]:
        # 第一批够发（剩 60 ≥ 50），第二批剩 10 < 12 就停
        remaining = 60 if not calls else 10
        return {
            "budget": 5000,
            "used": 5000 - remaining,
            "remaining": remaining,
            "fits_next_chunk": remaining >= next_lines,
            "complete": remaining > 0,
            "needs_interpolation": remaining < next_lines,
        }

    client = FakeClient(responder, budget_responder=budget)
    probe = RouteProbe(client, mode="riding", batch_size=50)
    result = run(probe.probe(ORIGIN, make_points(62)))

    assert len(calls) == 1, "预算不足的那一批根本不该发出去"
    assert result.budget_stopped is True
    assert result.stopped_reason and "共用同一份" in result.stopped_reason
    assert result.completed_cells == 50
    assert result.missing_cells == list(range(50, 62))
    assert [o.blocked_kind for o in result.outcomes[50:]] == [None] * 12
    assert all("budget_stopped" in o.signals for o in result.outcomes[50:])
    assert result.needs_interpolation is True
    assert result.budget_state["remaining"] == 10
    assert probe.usage()["budget_stops"] == 1
    assert probe.usage()["quota_units"] == 50, "没发出去的批次不记配额账（不虚报消耗）"


def test_guard_errors_never_become_blocked_labels() -> None:
    """守卫自己坏掉（client 没实现该方法）时不拦停、也不抛。"""

    class NoBudgetClient(FakeClient):
        def route_budget_state(self, mode=None, *, next_lines=0) -> dict[str, Any]:
            raise AttributeError("route_budget_state")

    client = NoBudgetClient()
    probe = RouteProbe(client, mode="walking")
    result = run(probe.probe(ORIGIN, make_points(4)))

    assert len(client.matrix_calls) == 1
    assert result.budget_stopped is False
    assert probe.guard_errors() == ["route_budget_state:AttributeError"]


def test_route_budget_text_states_the_shared_quota() -> None:
    text = route_budget_text("riding", 12, {"remaining": 4, "used": 4996, "budget": 5000})
    assert "5,000" in text or "5000" in text
    assert "步行与骑行" in text and "共用" in text
    assert "mode=riding" in text and "12" in text


# --------------------------------------------------------------------------- ⑤ 懒加载上限
def test_polyline_call_limit_rejects_the_13th_request() -> None:
    """FR-203 硬性单次上限 12 次：第 13 个顶点拿到结果对象（带 reason），且一次不出网。"""
    client = FakeClient()
    probe = RouteProbe(client, mode="walking", polyline_call_limit=12)

    granted = [
        run(probe.polyline(ORIGIN, (31.23 + i * 0.001, 121.47 + i * 0.001))) for i in range(12)
    ]
    rejected = run(probe.polyline(ORIGIN, (31.30, 121.50)))

    assert all(item.allowed and item.ok for item in granted)
    assert len(client.direction_calls) == 12
    assert rejected.allowed is False
    assert rejected.ok is False
    assert rejected.reason and "12" in rejected.reason
    assert len(client.direction_calls) == 12, "第 13 次绝不能真的出网"
    assert (rejected.http_requests, rejected.quota_units, rejected.place_calls) == (0, 0, 0)
    assert probe.remaining_polyline_calls == 0
    assert probe.usage()["polyline_rejected"] == 1


def test_polylines_batch_keeps_order_and_marks_every_over_limit_item() -> None:
    client = FakeClient()
    probe = RouteProbe(client, polyline_call_limit=3)
    items = run(probe.polylines(ORIGIN, make_points(6)))

    assert len(items) == 6
    assert [item.allowed for item in items] == [True, True, True, False, False, False]
    assert [item.point_index for item in items] == [0, 1, 2, 3, 4, 5]
    assert len(client.direction_calls) == 3
    assert probe.usage()["polyline_calls"] == 3


def test_polyline_cache_hit_does_not_consume_the_limit() -> None:
    client = FakeClient()
    probe = RouteProbe(client, polyline_call_limit=2)

    first = run(probe.polyline(ORIGIN, make_points(1)[0]))
    second = run(probe.polyline(ORIGIN, make_points(1)[0]))
    third = run(probe.polyline(ORIGIN, make_points(2)[1]))
    fourth = run(probe.polyline(ORIGIN, make_points(3)[2]))

    assert first.allowed and second.allowed
    assert second.from_cache is True
    assert len(client.direction_calls) == 2, "同顶点重复点击不该再打（只第 1、3 个真出网）"
    assert third.allowed and third.http_requests == 1
    assert fourth.allowed is False, "缓存命中不占用 12 次的额度，但额度本身不放水"
    assert probe.usage()["quota_units"] == 2, "缓存命中不消耗百度配额，记了就是假账"


def test_polyline_reads_duration_and_path_strings() -> None:
    probe = RouteProbe(FakeClient(), polyline_call_limit=12)
    item = run(probe.polyline(ORIGIN, make_points(1)[0]))

    assert item.path_strings == ["121.47,31.23;121.48,31.24"]
    assert item.duration_s == 900 and item.distance_m == 1600
    assert item.as_dict()["path_segments"] == 1


def test_walking_link_attr_0x01_is_blocked_official() -> None:
    """PRD §4.2：direction 返回 `links[].attr=0x01`（步行禁行）是官方硬判据。"""
    client = FakeClient(direction_responder=lambda *a: direction_result(link_attr=0x01))
    probe = RouteProbe(client)
    item = run(probe.polyline(ORIGIN, make_points(1)[0], lite=True))

    assert item.blocked_kind == BLOCKED_OFFICIAL
    assert "links.attr=0x01" in item.signals
    assert item.allowed is True


def test_riding_restriction_from_direction_is_blocked_official() -> None:
    client = FakeClient(direction_responder=lambda *a: direction_result(restrictions_status=0x01))
    probe = RouteProbe(client)
    item = run(probe.polyline(ORIGIN, make_points(1)[0]))

    assert item.blocked_kind == BLOCKED_OFFICIAL
    assert "restrictions_status=0x01" in item.signals


def test_direction_status_2001_is_blocked_official() -> None:
    client = FakeClient(
        direction_responder=lambda *a: direction_result(status=2001, paths=(), duration=0)
    )
    probe = RouteProbe(client)
    item = run(probe.polyline(ORIGIN, make_points(1)[0]))
    assert item.blocked_kind == BLOCKED_OFFICIAL and "status=2001" in item.signals


def test_direction_empty_result_is_blocked_timeout() -> None:
    """v2 的 `status=7` 不在官方禁行清单里 → 只能算「没测到」，不许升格成官方禁行。"""
    client = FakeClient(direction_responder=lambda *a: direction_result(status=7, paths=()))
    probe = RouteProbe(client)
    item = run(probe.polyline(ORIGIN, make_points(1)[0]))

    assert item.blocked_kind == BLOCKED_TIMEOUT
    assert "empty_result" in item.signals


def test_direction_exception_is_captured() -> None:
    client = FakeClient(direction_responder=lambda *a: TimeoutError("read timeout"))
    probe = RouteProbe(client)
    item = run(probe.polyline(ORIGIN, make_points(1)[0]))

    assert item.error == "TimeoutError"
    assert item.blocked_kind == BLOCKED_TIMEOUT
    assert "client_error=TimeoutError" in item.signals


def test_riding_polyline_forwards_the_quality_switch() -> None:
    client = FakeClient()
    probe = RouteProbe(client, mode="riding", road_prefer=3)
    run(probe.polyline(ORIGIN, make_points(1)[0]))

    kwargs = client.direction_calls[0]["kwargs"]
    assert kwargs["lite"] is False and kwargs["steps_info"] == 1
    assert kwargs["road_prefer"] == 3


def test_zero_and_empty_inputs_return_result_objects() -> None:
    client = FakeClient()
    probe = RouteProbe(client)

    empty = run(probe.probe(ORIGIN, []))
    assert isinstance(empty, MatrixProbeResult) and empty.requested_cells == 0
    assert empty.completion_ratio == 1.0 and client.matrix_calls == []

    missing_point = run(probe.polyline(ORIGIN, None))
    assert missing_point.allowed is False and client.direction_calls == []


def test_mode_normalisation_does_not_raise_for_unknown_mode() -> None:
    """未知 mode 交给 client 判错，本层只把异常转成结果对象（永不抛）。"""

    def responder(mode: str, origins: list[Any], dests: list[Any], index: int) -> RouteMatrixResult:
        raise ValueError(f"unsupported mode {mode}")

    probe = RouteProbe(FakeClient(responder), mode="transit")
    result = run(probe.probe(ORIGIN, make_points(2)))
    assert result.mode == "transit"
    assert result.blocked_timeout_indices == [0, 1]
    assert probe.usage()["by_mode"]["transit"]["client_errors"] == 1
