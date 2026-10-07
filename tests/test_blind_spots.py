"""服务盲区识别单测（M3 / PRD §4.4、FR-220~FR-224）。

三条不可让的断言（这张卡的存在理由）：

1. `test_main_verdict_makes_zero_api_calls`：塞进一个**碰一下就抛错并计数**的假 client 与
   一个会计数的假路由器，跑完整个盲区判定后两者都是 0 次 —— 证明「O(N×3) 次检索 → 0 次」。
2. `test_detect_survives_socket_guard`：把 `socket` 全部改成抛错，判定照样出结果（条款
   3.2.3(ii) 零真实出网的兜底证据）。
3. `test_same_input_is_byte_identical`：同一 POI 集合两次运行（含打乱 POI 顺序）逐字节相同，
   且 blocks 顺序一致（FR-301）。

零出网手段：假 client + 假 route oracle + socket 守卫；测试不设 `NO_PROXY`、不碰真实 AK。
"""

from __future__ import annotations

import json
import socket

import pytest
from services import blind_spots
from services.blind_spots import (
    DEFAULT_STEP_M,
    LEVEL_BLIND,
    LEVEL_INSUFFICIENT,
    MAX_REFINE_CALLS,
    REQUIRED_SUBCATS,
    CandidateCell,
    blocks_to_geojson,
    cell_center,
    classify_cell,
    detect_blind_spots,
    flood_fill,
    frame_from_ring,
    generate_candidates,
    nearest_facility,
    normalize_subcat,
    trace_block_outline,
)
from services.isochrone_geometry import (
    GRID_STEP_M,
    IsochroneConfig,
    IsochroneResult,
    IsochroneStats,
    LngLat,
    build_duration_grid,
    destination,
    distance_m,
    point_in_ring,
    ring_area_km2,
)

CENTER = LngLat(116.40, 39.90)
CELL_AREA_KM2 = (DEFAULT_STEP_M / 1000.0) ** 2  # 单格面积 = 0.0625 km²


# --------------------------------------------------------------------------------------
# 测试夹具与假对象
# --------------------------------------------------------------------------------------
def polar(bearing_deg: float, distance_m_: float) -> LngLat:
    """从中心点按方位角直推（与等时圈探测点同一口径）。"""
    return destination(CENTER, float(bearing_deg) % 360.0, float(distance_m_))


def make_ring(radius_m: float = 1000.0, bearings: int = 24) -> list[LngLat]:
    return [polar(index * 360.0 / bearings, radius_m) for index in range(bearings)]


def poi(
    subcat: str, point: LngLat, *, name: str | None = None, measured: float | None = None
) -> dict:
    """§4.3 已拉取 POI 的 plain dict 契约。"""
    return {
        "subcat": subcat,
        "category": "medical" if subcat in ("pharmacy", "hospital", "clinic") else "other",
        "lnglat": [point.lng, point.lat],
        "name": name or f"{subcat}@{point.lng:.5f},{point.lat:.5f}",
        "measured_minutes": measured,
    }


class ForbiddenClient:
    """任何属性访问都算「出网」——主判定一次都不该碰到它。"""

    def __init__(self) -> None:
        self.__dict__["accesses"] = []

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        self.accesses.append(name)
        raise AssertionError(f"盲区主判定不得调用 client（被访问到 {name}）")


class CountingRouter:
    """精算用的假路线规划：计数 + 可返回固定值或「测不到」（None）。"""

    def __init__(self, result: object = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[float], list[float]]] = []
        self.result = {"minutes": 18.5, "distance_m": 1200.0} if result is None else result
        self.fail = fail

    def __call__(self, origin: list[float], target: list[float]) -> object:
        self.calls.append((list(origin), list(target)))
        return None if self.fail else self.result


def blind_cells(result: dict) -> int:
    return sum(block["cells"] for block in result["blocks"] if block["level"] == LEVEL_BLIND)


# --------------------------------------------------------------------------------------
# 1. 零 API 调用（本卡的存在理由）
# --------------------------------------------------------------------------------------
def test_main_verdict_makes_zero_api_calls() -> None:
    client = ForbiddenClient()
    router = CountingRouter()
    pois = [poi("hospital", polar(45, 300)), poi("supermarket", polar(225, 500))]

    result = detect_blind_spots(pois, make_ring(1000.0), client=client, route_fn=router)

    assert client.accesses == []  # 假 client 连属性都没被碰过
    assert router.calls == []  # refine 默认关闭
    assert result["usage"] == {
        "http_requests": 0,
        "quota_units": 0,
        "place_calls": 0,
        "route_lines": {
            "calls": 0,
            "budget": MAX_REFINE_CALLS,
            "measured_blocks": 0,
            "skipped_blocks": 0,
            "enabled": False,
        },
    }
    assert result["extra_api_calls"] == 0
    # 0 次调用不是「没算出东西」的另一种说法：确实产出了灰色盲区块
    assert result["count"] >= 1
    assert blind_cells(result) == result["grid"]["inside_ring"]


def test_detect_survives_socket_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("盲区判定不得触网")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    result = detect_blind_spots([], make_ring(800.0), client=ForbiddenClient())
    assert result["count"] == 1
    assert result["blocks"][0]["level"] == LEVEL_BLIND


# --------------------------------------------------------------------------------------
# 2. 两级判定（FR-221 主判定 / FR-224 次判定并存）
# --------------------------------------------------------------------------------------
def test_all_three_absent_is_blind() -> None:
    result = detect_blind_spots([], make_ring(1000.0))

    assert result["count"] == 1
    block = result["blocks"][0]
    assert block["level"] == LEVEL_BLIND
    assert block["missing"] == list(REQUIRED_SUBCATS)
    assert block["missing_count"] == 3
    assert block["cells"] == result["grid"]["inside_ring"]
    assert block["area_km2"] == pytest.approx(block["cells"] * CELL_AREA_KM2)


def test_pharmacy_present_is_insufficient_not_blind() -> None:
    """圈内每个候选点都能走到药店 -> 一个完全盲区都没有（两级判定的分界）。"""
    result = detect_blind_spots([poi("pharmacy", CENTER)], make_ring(500.0))

    assert result["area_km2"] == 0.0
    assert result["area_pct"] == 0.0
    assert [b["level"] for b in result["blocks"]] == [LEVEL_INSUFFICIENT]
    assert result["blocks"][0]["missing"] == ["market", "primary_school"]
    assert result["blocks"][0]["missing_count"] == 2


def test_medical_dimension_without_pharmacy_is_still_blind() -> None:
    """`medical` 维度非空（综合医院 + 诊所）但**没有药店** -> 仍算完全盲区。

    判定粒度是 `subcat`（官方分类路径），不是维度 —— 这是 FR-221/FR-214 的硬要求。
    """
    pois = [poi("hospital", CENTER), poi("clinic", polar(90, 200))]
    result = detect_blind_spots(pois, make_ring(500.0))

    assert {b["level"] for b in result["blocks"]} == {LEVEL_BLIND}
    assert blind_cells(result) == result["grid"]["inside_ring"]
    assert set(result["blocks"][0]["missing"]) == set(REQUIRED_SUBCATS)


def test_two_levels_coexist_on_the_same_poi_set() -> None:
    """(a) 三者皆无 = 完全盲区、(b) 任一缺失 = 配套不足，两种读法同时呈现。"""
    east = polar(90, 900)
    result = detect_blind_spots([poi("market", east), poi("pharmacy", east)], make_ring(1000.0))

    levels = {block["level"] for block in result["blocks"]}
    assert levels == {LEVEL_BLIND, LEVEL_INSUFFICIENT}
    for block in result["blocks"]:
        if block["level"] == LEVEL_BLIND:
            assert block["missing"] == list(REQUIRED_SUBCATS)
        else:
            assert block["missing"] == ["primary_school"]
    assert result["area_pct"] > 0.0


def test_include_insufficient_off_keeps_p0_only() -> None:
    east = polar(90, 900)
    pois = [poi("market", east), poi("pharmacy", east)]
    full = detect_blind_spots(pois, make_ring(1000.0))
    strict = detect_blind_spots(pois, make_ring(1000.0), include_insufficient=False)

    assert {b["level"] for b in full["blocks"]} == {LEVEL_BLIND, LEVEL_INSUFFICIENT}
    assert {b["level"] for b in strict["blocks"]} == {LEVEL_BLIND}
    assert strict["area_km2"] == full["area_km2"]  # 面积口径本来就只算 blind
    assert any("include_insufficient" in note for note in strict["notes"])


# --------------------------------------------------------------------------------------
# 3. 8 邻域聚合与外轮廓
# --------------------------------------------------------------------------------------
def test_flood_fill_uses_eight_neighbourhood() -> None:
    def cell(row: int, col: int) -> CandidateCell:
        return CandidateCell(row=row, col=col, point=CENTER)

    assert [len(c) for c in flood_fill([cell(0, 0), cell(1, 1)])] == [2]  # 对角相接也算一块

    l_shape = [cell(0, 0), cell(0, 1), cell(1, 1), cell(1, 2)]
    assert [len(c) for c in flood_fill(l_shape)] == [4]

    separated = flood_fill([cell(0, 0), cell(5, 5)])
    assert len(separated) == 2
    assert [len(c) for c in separated] == [1, 1]


def test_diagonal_touching_outline_is_multipolygon() -> None:
    frame = frame_from_ring(make_ring(1000.0), DEFAULT_STEP_M)
    cells = [CandidateCell(row=0, col=0, point=CENTER), CandidateCell(row=1, col=1, point=CENTER)]

    outline = trace_block_outline(frame, DEFAULT_STEP_M, cells)

    assert len(flood_fill(cells)) == 1  # 一个连通块
    assert outline["type"] == "MultiPolygon"  # 但几何上是两个相切的矩形
    assert len(outline["coordinates"]) == 2
    # 面积口径始终是「格数 × 单格面积」，与轮廓形状无关
    assert pytest.approx(0.125) == 2 * CELL_AREA_KM2


def test_outline_is_a_true_outer_ring_with_hole() -> None:
    """被覆盖格围成孤岛时，外轮廓是真外环 + 内洞（不是 per-cell 碎块的兜底输出）。"""
    cluster = [poi("market", CENTER), poi("pharmacy", CENTER), poi("primary_school", CENTER)]
    result = detect_blind_spots(cluster, make_ring(1000.0), radius_m=250.0)

    block = result["blocks"][0]
    assert block["polygon"]["type"] == "Polygon"
    rings = block["polygon"]["coordinates"]
    assert len(rings) == 2  # 外环 + 岛形内洞
    assert len(rings[0]) > 4  # 真外轮廓，不是单个格子的 4 角矩形
    assert block["area_km2"] == pytest.approx(block["cells"] * CELL_AREA_KM2)
    assert not any("per-cell" in note for note in result["notes"])


def test_blocks_to_geojson_is_renderable_feature_collection() -> None:
    result = detect_blind_spots([poi("market", polar(90, 900))], make_ring(1000.0))
    fc = blocks_to_geojson(result)

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == result["count"]
    feature = fc["features"][0]
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert feature["properties"]["level"] in (LEVEL_BLIND, LEVEL_INSUFFICIENT)


def test_every_block_ring_is_closed_and_non_self_touching() -> None:
    """所有块的线性环都闭合且没有重复顶点（GeoJSON 合法性；共点风车形不能输出 8 字环）。"""
    scenarios = [
        ([], make_ring(1000.0), {}),
        ([poi("market", polar(90, 900))], make_ring(1000.0), {"radius_m": 250.0}),
        (_wedge_pois(), make_ring(1200.0), {"radius_m": 250.0}),
    ]
    for pois, ring, kwargs in scenarios:
        for block in detect_blind_spots(pois, ring, **kwargs)["blocks"]:
            rings = _all_rings(block["polygon"])
            assert rings, block["polygon"]
            for linear_ring in rings:
                assert len(linear_ring) >= 5
                assert linear_ring[0] == linear_ring[-1]
                assert len({tuple(point) for point in linear_ring}) == len(linear_ring) - 1


def _all_rings(geometry: dict) -> list[list[list[float]]]:
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    return [ring for polygon in geometry["coordinates"] for ring in polygon]


def test_outline_falls_back_to_per_cell_rects_and_keeps_area_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外轮廓追踪失败时退化成 per-cell 矩形，但面积口径仍是「格数 × 单格面积」并在 notes 留痕。"""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("故意让轮廓追踪失败")

    monkeypatch.setattr(blind_spots, "trace_block_outline", boom)
    result = detect_blind_spots([], make_ring(600.0))
    block = result["blocks"][0]

    assert block["polygon"]["type"] == "MultiPolygon"
    assert len(block["polygon"]["coordinates"]) == block["cells"]  # 一格一个矩形
    assert block["area_km2"] == pytest.approx(block["cells"] * CELL_AREA_KM2)
    assert result["area_km2"] == pytest.approx(result["grid"]["inside_ring"] * CELL_AREA_KM2)
    assert any("per-cell" in note for note in result["notes"])
    assert any("面积口径不变" in note for note in result["notes"])


# --------------------------------------------------------------------------------------
# 4. 候选网格（FR-220）：与 M2 耗时场同源、圈外不生成
# --------------------------------------------------------------------------------------
def test_candidates_generated_only_inside_ring() -> None:
    ring = make_ring(1000.0)
    candidates = generate_candidates(ring, step_m=DEFAULT_STEP_M)
    grid = detect_blind_spots([], ring)["grid"]

    assert grid["inside_ring"] == len(candidates)
    assert grid["candidates"] > grid["inside_ring"]  # bbox 里的圈外格被预筛掉了
    assert all(point_in_ring(cell.point, ring) for cell in candidates)
    assert all(distance_m(CENTER, cell.point) <= 1000.0 + DEFAULT_STEP_M for cell in candidates)


def test_outside_ring_generates_nothing_and_degenerate_ring_is_empty() -> None:
    tiny = [CENTER, polar(180, 120), polar(90, 120)]
    result = detect_blind_spots([], tiny)

    assert result["grid"]["inside_ring"] == len(generate_candidates(tiny))
    assert result["blocks"] == []  # 环太小，一个候选点都没落到圈里
    assert generate_candidates([CENTER, polar(90, 100)]) == ()
    assert detect_blind_spots([], [CENTER, polar(90, 100)])["blocks"] == []


def test_grid_is_same_source_as_m2_duration_field() -> None:
    """盲区网格与热力图网格必须同源（步长与格心算法都取 M2 的口径），否则图层错位。"""
    ring = make_ring(1000.0)
    field = build_duration_grid(ring, (), step_m=GRID_STEP_M)
    mine = frame_from_ring(ring, DEFAULT_STEP_M)

    assert DEFAULT_STEP_M == GRID_STEP_M == IsochroneConfig().grid_step_m == 250.0
    assert (mine.origin.lng, mine.origin.lat) == (field.origin.lng, field.origin.lat)
    assert (mine.rows, mine.cols) == (field.rows, field.cols)

    centers = {
        (row, col): cell_center(mine, DEFAULT_STEP_M, row, col)
        for row in range(mine.rows)
        for col in range(mine.cols)
    }
    assert len(centers) == detect_blind_spots([], ring)["grid"]["candidates"]
    assert all(point_in_ring(centers[(c.row, c.col)], ring) for c in generate_candidates(ring))


# --------------------------------------------------------------------------------------
# 5. 面积口径（area_pct 只算完全盲区）
# --------------------------------------------------------------------------------------
def test_area_pct_counts_blind_only() -> None:
    east = polar(90, 900)
    pois = [poi("market", east), poi("pharmacy", east)]
    result = detect_blind_spots(pois, make_ring(1000.0))

    blind_area = round(sum(b["area_km2"] for b in result["blocks"] if b["level"] == LEVEL_BLIND), 6)
    all_area = round(sum(b["area_km2"] for b in result["blocks"]), 6)
    assert any(b["level"] == LEVEL_INSUFFICIENT for b in result["blocks"])
    assert result["area_km2"] == blind_area
    assert result["area_km2"] < all_area  # 把「配套不足」计进去会虚增盲区面积
    assert result["area_pct"] == pytest.approx(
        blind_area / ring_area_km2(make_ring(1000.0)) * 100.0, abs=1e-3
    )
    assert 0.0 < result["area_pct"] <= 100.0
    assert any('只计 `level=="blind"`' in note for note in result["notes"])


# --------------------------------------------------------------------------------------
# 6. 确定性（FR-301）
# --------------------------------------------------------------------------------------
def test_same_input_is_byte_identical() -> None:
    east = polar(90, 800)
    pois = [
        poi("market", east, measured=11.0),
        poi("pharmacy", CENTER, measured=4.0),
        poi("hospital", polar(200, 700)),
        poi("primary_school", polar(20, 600)),
    ]
    ring = make_ring(1100.0)

    first = json.dumps(detect_blind_spots(pois, ring), ensure_ascii=False, sort_keys=True)
    second = json.dumps(detect_blind_spots(pois, ring), ensure_ascii=False, sort_keys=True)
    shuffled = json.dumps(
        detect_blind_spots(list(reversed(pois)), ring), ensure_ascii=False, sort_keys=True
    )

    assert first == second
    assert first == shuffled  # POI 输入顺序不参与判定，blocks 顺序由行列排序固定


# --------------------------------------------------------------------------------------
# 7. 最近可用设施（FR-223）与精算（P2）
# --------------------------------------------------------------------------------------
def test_facility_estimate_is_never_labelled_as_measured() -> None:
    result = detect_blind_spots([poi("pharmacy", polar(90, 900), name="药店")], make_ring(500.0))
    facility = result["blocks"][0]["nearest_facility"]

    assert facility["measured"] is False
    assert facility["subcat"] == "pharmacy"
    assert facility["minutes"] == pytest.approx(
        round(facility["distance_m"] / (3.9 * 1000.0 / 60.0), 1), abs=0.15
    )
    assert any("measured=False" in note for note in result["notes"])


def test_measured_minutes_are_preferred() -> None:
    """§4.3 已实测的步行时间优先复用；没实测的点标 `measured=False` 而不是留空。"""
    pois = [
        poi("market", polar(90, 400), name="远市场", measured=7.5),
        poi("market", polar(90, 80), name="近市场（未实测）"),
        poi("pharmacy", CENTER, name="药店"),
    ]
    result = detect_blind_spots(pois, make_ring(500.0))
    facility = result["blocks"][0]["nearest_facility"]

    assert facility["measured"] is True
    assert facility["name"] == "远市场"
    assert facility["minutes"] == 7.5
    assert result["usage"]["http_requests"] == 0  # 复用别人的实测，自己不出网


def test_facility_and_centre_distance_fields_present() -> None:
    result = detect_blind_spots([poi("market", polar(0, 700))], make_ring(800.0), center=CENTER)
    block = result["blocks"][0]

    assert set(block["nearest_facility"]) == {
        "name",
        "category",
        "subcat",
        "minutes",
        "distance_m",
        "measured",
    }
    assert isinstance(block["distance_from_center_m"], int)
    assert block["distance_from_center_m"] >= 0
    assert len(block["center_lnglat"]) == 2


def _wedge_pois() -> list[dict]:
    """三条「设施带」把圈内切成 6 个互不相连的盲区（用来验证精算上限 5 次）。"""
    pois: list[dict] = []
    for line_bearing in (0, 60, 120):
        for offset in range(-1200, 1201, 200):
            point = (
                polar(line_bearing, offset) if offset >= 0 else polar(line_bearing + 180, -offset)
            )
            for subcat in REQUIRED_SUBCATS:
                pois.append(poi(subcat, point))
    return pois


def test_refine_is_off_by_default_and_capped_at_five() -> None:
    ring = make_ring(1200.0)
    pois = _wedge_pois()

    quiet = CountingRouter()
    off = detect_blind_spots(pois, ring, radius_m=250.0, route_fn=quiet)
    assert off["count"] == 6
    assert quiet.calls == []
    assert off["usage"]["http_requests"] == 0

    router = CountingRouter()
    on = detect_blind_spots(pois, ring, radius_m=250.0, refine=True, route_fn=router)
    assert len(router.calls) == MAX_REFINE_CALLS == 5
    assert on["usage"]["http_requests"] == 5
    assert on["usage"]["quota_units"] == 5
    assert on["usage"]["place_calls"] == 0
    assert on["extra_api_calls"] == 5
    assert on["usage"]["route_lines"]["measured_blocks"] == 5
    assert on["usage"]["route_lines"]["skipped_blocks"] == 1
    measured = [block["nearest_facility"]["measured"] for block in on["blocks"]]
    assert measured == [True] * 5 + [False]  # 上限之外的块保持估算，不假装实测


def test_refine_respects_smaller_budget_and_async_oracle() -> None:
    ring = make_ring(1200.0)
    pois = _wedge_pois()

    router = CountingRouter()
    capped = detect_blind_spots(
        pois, ring, radius_m=250.0, refine=True, route_fn=router, max_refine_calls=2
    )
    assert len(router.calls) == 2
    assert capped["usage"]["route_lines"]["budget"] == 2

    async def async_router(origin: list[float], target: list[float]) -> dict:
        return {"minutes": 21.0, "distance_m": 1350.0}

    refined = detect_blind_spots(
        pois, ring, radius_m=250.0, refine=True, route_fn=async_router, max_refine_calls=1
    )
    assert refined["usage"]["http_requests"] == 1
    assert refined["blocks"][0]["nearest_facility"]["minutes"] == 21.0
    assert refined["blocks"][0]["nearest_facility"]["measured"] is True


def test_refine_failure_keeps_estimate() -> None:
    """精算测不到就退回估算，绝不把估算值当实测输出。"""
    pois = [poi("pharmacy", polar(90, 900))]
    before = detect_blind_spots(pois, make_ring(500.0))
    estimator = before["blocks"][0]["nearest_facility"]

    failing = CountingRouter(fail=True)
    after = detect_blind_spots(pois, make_ring(500.0), refine=True, route_fn=failing)
    facility = after["blocks"][0]["nearest_facility"]

    assert len(failing.calls) == after["count"]  # 每个块各调 1 次
    assert after["usage"]["http_requests"] == after["count"]  # 调用发生了，如实记账
    assert after["usage"]["route_lines"]["measured_blocks"] == 0
    assert all(b["nearest_facility"]["measured"] is False for b in after["blocks"])
    assert facility["minutes"] == estimator["minutes"]  # 退回估算值本身
    assert facility["measured"] is False
    assert facility["distance_m"] > 0


def test_refine_without_route_fn_makes_no_calls() -> None:
    result = detect_blind_spots([], make_ring(800.0), refine=True)
    assert result["usage"]["http_requests"] == 0
    assert result["usage"]["route_lines"]["enabled"] is False
    assert any("route_fn" in note for note in result["notes"])


# --------------------------------------------------------------------------------------
# 8. 阈值可配置（PRD §3.4 需求澄清）+ 「菜市场」映射
# --------------------------------------------------------------------------------------
def test_thresholds_are_configurable() -> None:
    ring = make_ring(1000.0)
    cluster = [poi("pharmacy", CENTER), poi("primary_school", CENTER)]

    wide = detect_blind_spots(cluster, ring, radius_m=1000.0)
    tight = detect_blind_spots(cluster, ring, radius_m=100.0)
    assert blind_cells(tight) > blind_cells(wide)  # 半径越小，盲区越大
    assert tight["grid"]["radius_m"] == 100.0

    coarse = detect_blind_spots([], ring, step_m=500.0)
    fine = detect_blind_spots([], ring, step_m=250.0)
    assert coarse["grid"]["step_m"] == 500.0
    assert coarse["grid"]["inside_ring"] < fine["grid"]["inside_ring"]
    assert coarse["area_km2"] == pytest.approx(coarse["grid"]["inside_ring"] * 0.25)

    # 判定类别也可配：只盯「市场」时，有药店有小学的点照样算盲区
    market_only = detect_blind_spots(cluster, ring, required_subcats=("market",))
    assert {b["level"] for b in market_only["blocks"]} == {LEVEL_BLIND}
    assert market_only["blocks"][0]["missing"] == ["market"]


def test_caishichang_maps_to_official_market_in_notes() -> None:
    result = detect_blind_spots([], make_ring(600.0))

    assert any("菜市场" in note and "市场" in note for note in result["notes"])
    assert any("购物 > 市场" in note for note in result["notes"])
    assert normalize_subcat("菜市场") == "market"
    assert normalize_subcat("农贸市场") == "market"
    assert normalize_subcat("market") == "market"
    assert normalize_subcat("药店") == "pharmacy"
    assert normalize_subcat("小学") == "primary_school"
    assert normalize_subcat("综合医院") is None  # 维度里别的类别不能顶替药店
    assert normalize_subcat(None) is None


def test_notes_declare_configurable_thresholds_and_zero_calls() -> None:
    notes = " ".join(detect_blind_spots([], make_ring(600.0))["notes"])

    assert "step_m=250" in notes and "radius_m=1000" in notes
    assert "0 次 API 调用" in notes
    assert "两种读法" in notes


# --------------------------------------------------------------------------------------
# 9. 输入契约与出参形状
# --------------------------------------------------------------------------------------
def test_output_contract_shape() -> None:
    result = detect_blind_spots([poi("market", CENTER)], make_ring(700.0))

    assert set(result) == {
        "count",
        "area_pct",
        "area_km2",
        "blocks",
        "grid",
        "extra_api_calls",
        "notes",
        "usage",
    }
    assert set(result["grid"]) == {"step_m", "radius_m", "candidates", "inside_ring"}
    assert set(result["usage"]) == {"http_requests", "quota_units", "place_calls", "route_lines"}
    assert set(result["blocks"][0]) == {
        "polygon",
        "cells",
        "center_lnglat",
        "area_km2",
        "missing",
        "missing_count",
        "level",
        "distance_from_center_m",
        "nearest_facility",
    }
    assert isinstance(result["count"], int) and result["count"] == len(result["blocks"])
    assert isinstance(result["notes"], list) and all(isinstance(n, str) for n in result["notes"])


def test_accepts_isochrone_result_and_bare_ring() -> None:
    """`isochrone` 两种都吃：`IsochroneResult`（含 duration_field 的 frame）与裸 ring 顶点序列。"""
    ring = make_ring(1000.0)
    field = build_duration_grid(ring, (), step_m=GRID_STEP_M)
    stats = IsochroneStats(
        mode="walking",
        target_seconds=900.0,
        r_max_m=1250.0,
        theoretical_radius_m=975.0,
        oracle_calls=0,
        probes_total=0,
        rounds=0,
        vertex_count=len(ring),
        kind_counts={},
        converged_ratio=1.0,
        blocked_ratio=0.0,
    )
    result_obj = IsochroneResult(
        center=CENTER,
        mode="walking",
        target_seconds=900.0,
        ring=tuple(ring),  # type: ignore[arg-type]
        vertices=(),
        area_km2=ring_area_km2(ring),
        duration_field=field,
        stats=stats,
    )
    pois = [poi("market", polar(90, 900)), poi("pharmacy", polar(270, 900))]

    from_result = json.dumps(
        detect_blind_spots(pois, result_obj), ensure_ascii=False, sort_keys=True
    )
    from_ring_ = json.dumps(
        detect_blind_spots(pois, ring, center=CENTER), ensure_ascii=False, sort_keys=True
    )

    assert json.loads(from_result)["count"] >= 1
    assert from_result == from_ring_  # 复用同一 frame -> 逐字节一致


def test_malformed_pois_are_skipped_with_a_note() -> None:
    pois = [
        poi("market", CENTER),
        {"subcat": "pharmacy", "name": "没有坐标的药店"},
        {"subcat": "primary_school", "lnglat": ["经度", "纬度"], "name": "坐标不是数"},
        "不是字典的条目",
    ]
    result = detect_blind_spots(pois, make_ring(500.0))

    assert result["count"] >= 1
    assert any("跳过" in note for note in result["notes"])
    points, skipped = blind_spots.coerce_pois(pois)
    assert skipped == 3
    assert classify_cell(points, CENTER, radius_m=1000.0) == ("market",)


def test_nearest_facility_falls_back_to_any_poi_when_no_required_class_exists() -> None:
    points, _ = blind_spots.coerce_pois([poi("supermarket", polar(90, 600), name="超市")])
    facility = nearest_facility(CENTER, points, list(REQUIRED_SUBCATS))

    assert facility is not None
    assert facility["subcat"] == "supermarket"  # 三类全局都没有 -> 退到任一点
    assert facility["measured"] is False
    assert nearest_facility(CENTER, [], list(REQUIRED_SUBCATS)) is None
