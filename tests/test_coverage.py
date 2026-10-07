"""`services/coverage.py`（M3 圈内覆盖统计）单测。

这是 M3 泳道里此前**完全没有测试**的一层（`blind_spots` 有 32 例，本层 0 例），
而它正是「落圈判定 + 前 K 实测 + 单维度得分」三件事的所在地，也是 §4.5 时效分的唯一来源。

覆盖范围：
  * `classify_pois_in_ring` —— 射线法落圈 + 三个计数口径的自洽式；
  * `quantity_score` / `timeliness_score` / `dimension_score` —— §4.5 的三个纯函数与边界；
  * `select_measurement_targets`（全局最近 K）与 `select_measurement_targets_fair`（分层）；
  * `build_coverage` —— 端到端：实测回填、未实测归因、两栏计量、按 mode 分栏、可复算。

🔴 其中 `test_fair_selection_prevents_the_dense_area_score_inversion` 是一条**缺陷回归**：
全局最近 K 会让「配套密集」的社区因为维度被饿死而拿低分（实测 67.25 分 C 低于稀疏社区的
84.66 分 B），分层挑选把它修正过来。详见 `services/coverage.py:SELECTION_FAIR` 的长注释。

全部零出网：探测器一律用假对象（条款 3.2.3(ii) 禁止对线上服务施压）。
"""

from __future__ import annotations

import json

import pytest
from services.coverage import (
    DEFAULT_THRESHOLDS,
    REASON_BEYOND_TOP_K,
    REASON_NO_POI_IN_POLYGON,
    REASON_NO_PROBE,
    REASON_OUTSIDE_POLYGON,
    SELECTION_FAIR,
    SELECTION_NEAREST,
    build_coverage,
    canonical_dimension_key,
    classify_pois_in_ring,
    dimension_score,
    quantity_score,
    select_measurement_targets,
    select_measurement_targets_fair,
    timeliness_score,
)
from services.isochrone_geometry import distance_m

from tests.helpers import async_test

CENTER = (116.404, 39.915)


# 一个约 850 m × 1110 m 的方形环（±0.005° 经度 / ±0.005° 纬度），够放圈内圈外的点。
RING = [
    [CENTER[0] - 0.005, CENTER[1] - 0.005],
    [CENTER[0] + 0.005, CENTER[1] - 0.005],
    [CENTER[0] + 0.005, CENTER[1] + 0.005],
    [CENTER[0] - 0.005, CENTER[1] + 0.005],
]


def offset(east_m: float, north_m: float) -> list[float]:
    """从中心点向东 / 向北偏移若干米（近似等距圆柱，只用于造测试点位）。"""
    lat = CENTER[1] + north_m / 111319.49
    lng = CENTER[0] + east_m / (111319.49 * 0.7679)  # cos(39.915°) ≈ 0.7679
    return [round(lng, 6), round(lat, 6)]


def poi(subcat: str, east_m: float, north_m: float, *, uid: str | None = None) -> dict:
    return {
        "uid": uid or f"{subcat}-{east_m:g}-{north_m:g}",
        "name": f"样例-{subcat}",
        "category": "test",
        "subcat": subcat,
        "lnglat": offset(east_m, north_m),
        "distance_m": int(distance_m(CENTER, offset(east_m, north_m))),
    }


def group(key: str, pois: list[dict], **extra) -> dict:
    base_threshold = DEFAULT_THRESHOLDS.get(key, DEFAULT_THRESHOLDS["medical"])
    base = {
        "key": key,
        "category": key,
        "label": key,
        "weight": base_threshold.weight,
        "threshold_count": base_threshold.threshold_count,
        "threshold_minutes": base_threshold.threshold_minutes,
        "count_total": len(pois),
        "pois": pois,
    }
    base.update(extra)
    return base


class FakeProbe:
    """假探测器：按 `origins × destinations` 如实记账，零出网。

    `minutes_per_point` 给每个下标一个可预测的耗时（分钟），便于断言回填是否正确。
    """

    def __init__(
        self,
        *,
        minutes_per_point: float = 5.0,
        http_requests: int = 1,
        cache_hits: int = 0,
        fail: bool = False,
        measured_upto: int | None = None,
    ) -> None:
        self.minutes_per_point = minutes_per_point
        self.http_requests = http_requests
        self.cache_hits = cache_hits
        self.fail = fail
        self.measured_upto = measured_upto
        self.calls: list[tuple[int, str]] = []

    async def probe(self, origin, points, *, mode=None):
        self.calls.append((len(points), str(mode)))
        if self.fail:
            raise RuntimeError("boom")
        outcomes = []
        for index in range(len(points)):
            measured = self.measured_upto is None or index < self.measured_upto
            outcomes.append(
                {
                    "index": index,
                    "measured": measured,
                    "duration_s": self.minutes_per_point * 60.0 * (index + 1) if measured else None,
                    "distance_m": 400.0 + index,
                    "blocked_kind": None,
                    "signals": [],
                    "status": 0,
                }
            )
        return {
            "http_requests": self.http_requests,
            "quota_units": len(points),
            "place_calls": 0,
            "route_lines": len(points),
            "cache_hits": self.cache_hits,
            "requested_cells": len(points),
            "budget_stopped": False,
            "batches": [{"sent": True, "from_cache": bool(self.cache_hits)}],
            "outcomes": outcomes,
        }


# --------------------------------------------------------------------------------------
# canonical_dimension_key
# --------------------------------------------------------------------------------------
def test_canonical_key_maps_poi_layer_names_to_report_layer_names() -> None:
    """`config/weights.yaml` 用 POI 层的键、覆盖层用 §4.7 的键，两者必须能对上。"""
    assert canonical_dimension_key("market") == "shopping"
    assert canonical_dimension_key("service_ref") == "convenience"
    assert canonical_dimension_key("MEDICAL") == "medical"
    assert canonical_dimension_key("unknown_key") == "unknown_key"
    assert canonical_dimension_key(None) == ""


# --------------------------------------------------------------------------------------
# classify_pois_in_ring（射线法落圈）
# --------------------------------------------------------------------------------------
def test_points_inside_and_outside_the_ring_are_separated() -> None:
    pois = [poi("pharmacy", 100, 100), poi("market", 3000, 0), poi("market", 0, -200)]
    classified, count_in, count_near = classify_pois_in_ring(pois, RING)
    assert count_in == 2 and count_near == 1
    assert [item["in_polygon"] for item in classified] == [True, False, True]


def test_counts_are_self_consistent_and_input_is_not_mutated() -> None:
    pois = [poi("a", 10, 10), poi("b", 9000, 0)]
    classified, count_in, count_near = classify_pois_in_ring(pois, RING)
    assert count_in + count_near == len(classified)
    assert "in_polygon" not in pois[0], "上游对象不能被改写"


def test_unparsable_coordinates_count_as_outside_and_leave_a_trace() -> None:
    """判不了就按圈外算（宁可少计），并留 `polygon_error` —— 不把未知说成圈内。"""
    bad = {"uid": "x", "subcat": "market", "lnglat": "not-a-point"}
    classified, count_in, count_near = classify_pois_in_ring([bad], RING)
    assert count_in == 0 and count_near == 1
    assert classified[0]["polygon_error"] == "unparsable_lnglat"


def test_degenerate_ring_treats_everything_as_outside() -> None:
    classified, count_in, _ = classify_pois_in_ring([poi("market", 0, 0)], [])
    assert count_in == 0
    assert classified[0]["in_polygon"] is False


# --------------------------------------------------------------------------------------
# §4.5 的三个纯函数
# --------------------------------------------------------------------------------------
def test_quantity_score_is_capped_at_one() -> None:
    assert quantity_score(0, 2) == 0.0
    assert quantity_score(1, 2) == 0.5
    assert quantity_score(2, 2) == 1.0
    assert quantity_score(99, 2) == 1.0


def test_quantity_score_without_threshold_is_presence_only() -> None:
    """参考项 / 配置缺省：有就算满、没有就是 0，不做除零。"""
    assert quantity_score(3, 0) == 1.0
    assert quantity_score(0, 0) == 0.0


def test_timeliness_score_is_zero_when_nothing_was_measured() -> None:
    """🔴 没实测到就是 0 分：绝不用直线距离顶替（本层唯一的假绿路径）。"""
    assert timeliness_score(None, 15.0, 4.0) == 0.0


def test_timeliness_score_interpolates_between_ideal_and_upper() -> None:
    assert timeliness_score(4.0, 15.0, 4.0) == pytest.approx(1.0)  # 达到理想
    assert timeliness_score(15.0, 15.0, 4.0) == pytest.approx(0.0)  # 恰好到上限
    assert timeliness_score(30.0, 15.0, 4.0) == 0.0  # 超上限
    assert timeliness_score(9.5, 15.0, 4.0) == pytest.approx(0.5)


def test_timeliness_score_survives_a_misconfigured_span() -> None:
    """`T_上限 ≤ T_理想` 是配错，但必须退化判定而不是抛异常 / 给 NaN。"""
    assert timeliness_score(3.0, 4.0, 4.0) == 1.0
    assert timeliness_score(5.0, 4.0, 4.0) == 0.0


def test_dimension_score_is_half_quantity_half_timeliness() -> None:
    scored = dimension_score(2, 2, 4.0, 15.0, 4.0)
    assert scored["quantity_score"] == 1.0
    assert scored["timeliness_score"] == 1.0
    assert scored["score"] == 100.0
    half = dimension_score(1, 2, None, 15.0, 4.0)
    assert half["score"] == 25.0  # 0.5×0.5 数量分 + 0.5×0 时效分


# --------------------------------------------------------------------------------------
# 前 K 挑选：全局 vs 分层
# --------------------------------------------------------------------------------------
def make_targets(spec: list[tuple[str, float]]) -> list:
    """`[(维度, 直线距离米)]` → `MeasurementTarget` 列表。"""
    from services.coverage import MeasurementTarget

    return [
        MeasurementTarget(
            ref=f"{key}\u2016{index}",
            group_key=key,
            uid=f"{key}-{index}",
            position=_point(index),
            straight_distance_m=metres,
        )
        for index, (key, metres) in enumerate(spec)
    ]


def _point(index: int):
    from services.isochrone_geometry import as_lnglat

    return as_lnglat(offset(50.0 + index, 0.0))


def test_nearest_selection_takes_the_globally_closest_k() -> None:
    targets = make_targets([("medical", 900.0), ("food", 100.0), ("food", 150.0), ("food", 200.0)])
    picked, effective, capped = select_measurement_targets(targets, 3)
    assert effective == 3 and capped is False
    assert [item.group_key for item in picked] == ["food", "food", "food"]


def test_nearest_selection_is_clamped_to_the_batch_cap() -> None:
    targets = make_targets([(f"g{i}", float(i)) for i in range(60)])
    picked, effective, capped = select_measurement_targets(targets, 80)
    assert capped is True and effective == 50 and len(picked) == 50


def test_fair_selection_gives_every_dimension_a_seat_before_filling() -> None:
    targets = make_targets([("medical", 900.0), ("food", 100.0), ("food", 150.0), ("food", 200.0)])
    picked, effective, capped, allocation = select_measurement_targets_fair(targets, 3)
    assert effective == 3 and capped is False
    assert allocation == {"medical": 1, "food": 2}
    assert {item.group_key for item in picked} == {"medical", "food"}


def test_fair_selection_falls_back_to_global_fill_when_k_is_small() -> None:
    """维度数 > K 时第一轮就填满了，剩下的名额不存在，也不该报错。"""
    targets = make_targets([("a", 100.0), ("b", 200.0), ("c", 300.0)])
    picked, effective, _, allocation = select_measurement_targets_fair(targets, 2)
    assert len(picked) == 2 and effective == 2
    assert sum(allocation.values()) == 2


def test_fair_selection_is_order_independent_and_sorted() -> None:
    spec = [("food", 100.0), ("medical", 900.0), ("food", 150.0), ("medical", 800.0)]
    first = select_measurement_targets_fair(make_targets(spec), 2)[0]
    second = select_measurement_targets_fair(list(reversed(make_targets(spec))), 2)[0]
    assert [item.ref for item in first] == [item.ref for item in second]
    distances = [item.straight_distance_m for item in first]
    assert distances == sorted(distances)


def test_zero_k_selects_nothing() -> None:
    targets = make_targets([("a", 1.0)])
    assert select_measurement_targets_fair(targets, 0)[0] == []
    assert select_measurement_targets(targets, -5)[0] == []


# --------------------------------------------------------------------------------------
# build_coverage 端到端
# --------------------------------------------------------------------------------------
@async_test
async def test_build_coverage_fills_measured_minutes_from_one_batch() -> None:
    coverage = await build_coverage(
        {
            "center": list(CENTER),
            "groups": [group("medical", [poi("pharmacy", 100, 0), poi("clinic", 120, 40)])],
            "data_source": "live",
        },
        RING,
        probe=FakeProbe(minutes_per_point=5.0),
        k=12,
    )
    item = coverage["groups"][0]
    assert item["count_in"] == 2 and item["count_near"] == 0
    assert item["measured_count"] == 2
    assert item["nearest_minutes"] == pytest.approx(5.0)  # 下标 0 → 5 min
    assert item["nearest_minutes_source"] == "measured"
    assert item["unmeasured_count"] == 0


@async_test
async def test_build_coverage_usage_keeps_the_two_columns_apart() -> None:
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", [poi("pharmacy", 100, 0)])]},
        RING,
        probe=FakeProbe(http_requests=1),
        modes=["walking", "riding"],
        k=12,
    )
    usage = coverage["usage"]
    # K=1 个点位、两种模式 → 2 条路线，但仍是 1×2 批（每模式 1 批），互不折算。
    assert usage["quota_units"] == 2
    assert usage["route_lines"]["walking"] == 1
    assert usage["route_lines"]["riding"] == 1
    assert usage["place_calls"] == 0, "落圈判定本身零出网"
    assert usage["batches_sent"] == 2


@async_test
async def test_build_coverage_without_probe_is_zero_network_and_marks_reason() -> None:
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", [poi("pharmacy", 100, 0)])]},
        RING,
        probe=None,
        k=12,
    )
    item = coverage["groups"][0]
    assert item["nearest_minutes"] is None
    assert item["pois"][0]["unmeasured_reason"] == REASON_NO_PROBE
    assert coverage["usage"]["http_requests"] == 0


@async_test
async def test_pois_outside_the_ring_are_never_measured() -> None:
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", [poi("pharmacy", 9000, 0)])]},
        RING,
        probe=FakeProbe(),
        k=12,
    )
    item = coverage["groups"][0]
    assert item["count_in"] == 0 and item["count_near"] == 1
    assert item["pois"][0]["unmeasured_reason"] == REASON_OUTSIDE_POLYGON
    assert item["nearest_reason"] == REASON_NO_POI_IN_POLYGON


@async_test
async def test_pois_beyond_top_k_are_labelled_separately() -> None:
    """「配额换出来的取舍」与「数据缺失」必须是两种原因，前端文案不同。"""
    pois = [poi("pharmacy", 50 + index * 10, 0, uid=f"p{index}") for index in range(5)]
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", pois)]},
        RING,
        probe=FakeProbe(),
        k=2,
    )
    reasons = {item["unmeasured_reason"] for item in coverage["groups"][0]["pois"]}
    assert REASON_BEYOND_TOP_K in reasons
    assert coverage["groups"][0]["measured_count"] == 2


@async_test
async def test_probe_failure_degrades_instead_of_raising() -> None:
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", [poi("pharmacy", 100, 0)])]},
        RING,
        probe=FakeProbe(fail=True),
        k=12,
    )
    assert coverage["usage"]["probe_failures"] == 1
    assert coverage["groups"][0]["nearest_minutes"] is None
    assert any("批量算路" in note for note in coverage["notes"])


@async_test
async def test_partial_batch_leaves_the_rest_unmeasured() -> None:
    """发了这一批、这一格没回耗时 —— 不能算成实测，也不能算成失败。"""
    pois = [poi("pharmacy", 50 + index * 10, 0, uid=f"p{index}") for index in range(4)]
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", pois)]},
        RING,
        probe=FakeProbe(measured_upto=2),
        k=12,
    )
    item = coverage["groups"][0]
    assert item["measured_count"] == 2
    assert item["unmeasured_count"] == 2


@async_test
async def test_missing_ring_makes_everything_outside_and_says_so() -> None:
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", [poi("pharmacy", 10, 0)])]},
        None,
        probe=FakeProbe(),
    )
    assert coverage["groups"][0]["count_in"] == 0
    assert coverage["meta"]["has_ring"] is False
    assert any("未提供等时圈环" in note for note in coverage["notes"])


@async_test
async def test_build_coverage_is_reproducible() -> None:
    payload = {
        "center": list(CENTER),
        "groups": [group("medical", [poi("pharmacy", 100, 0), poi("clinic", 300, 50)])],
    }
    first = await build_coverage(payload, RING, probe=FakeProbe(), k=12)
    second = await build_coverage(payload, RING, probe=FakeProbe(), k=12)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == (
        json.dumps(second, sort_keys=True, ensure_ascii=False)
    )


@async_test
async def test_three_count_columns_are_reported_separately() -> None:
    """`count_in` / `count_near` / `count_total` 是三个不同集合，必须分开且带口径说明。"""
    coverage = await build_coverage(
        {
            "center": list(CENTER),
            "groups": [group("medical", [poi("pharmacy", 100, 0)], count_total=150)],
        },
        RING,
        probe=FakeProbe(),
        k=12,
    )
    item = coverage["groups"][0]
    assert item["count_in"] == 1
    assert item["count_near"] == 0
    assert item["count_total"] == 150  # 官方 total，含被 place 过滤掉的数据
    assert set(item["count_basis"]) == {"count_in", "count_near", "count_total"}


# --------------------------------------------------------------------------------------
# 🔴 缺陷回归：全局最近 K 造成「配套越密集、得分越低」
# --------------------------------------------------------------------------------------
@async_test
async def test_fair_selection_prevents_the_dense_area_score_inversion() -> None:
    """密集社区的维度会被全局最近 K 饿死 → 时效分记 0 → 得分反而低于稀疏社区。

    构造：`medical` 的点在 400 m 外（**圈内**，环的东向半径约 427 m），
    `market` 有 6 个点在 100~200 m 内，K=4。
      * `nearest`：4 个名额全给最近的 market 点 → medical `nearest_minutes=None` → 时效分 0；
      * `fair`  ：medical 先占 1 席 → 拿到实测时间 → 时效分正常。

    这正是实测中 `beijing-jiaodaokou`（老城，48 个圈内点）拿 67.25 分 C、
    而 `beijing-tiantongyuan`（新建大盘，13 个点）拿 84.66 分 B 的成因。
    """
    groups = [
        group("medical", [poi("pharmacy", 400, 0, uid="m0")]),
        group(
            "market",
            [poi("market", 100 + index * 20, 0, uid=f"f{index}") for index in range(6)],
        ),
    ]
    payload = {"center": list(CENTER), "groups": groups}

    starved = await build_coverage(
        payload, RING, probe=FakeProbe(), k=4, selection=SELECTION_NEAREST
    )
    fair = await build_coverage(payload, RING, probe=FakeProbe(), k=4, selection=SELECTION_FAIR)

    medical_nearest = {item["key"]: item["nearest_minutes"] for item in starved["groups"]}
    medical_fair = {item["key"]: item["nearest_minutes"] for item in fair["groups"]}
    assert medical_nearest["medical"] is None, "全局最近 K 让 medical 一个都没测到"
    assert medical_fair["medical"] is not None, "分层挑选必须给 medical 留一席"

    # 两条路径的调用量完全一致 —— 这不是拿配额换分数。
    assert starved["usage"]["quota_units"] == fair["usage"]["quota_units"] == 4
    assert starved["usage"]["http_requests"] == fair["usage"]["http_requests"]
    assert fair["meta"]["selection"] == SELECTION_FAIR
    assert starved["meta"]["selection"] == SELECTION_NEAREST


@async_test
async def test_unknown_selection_falls_back_to_fair_and_reports_it() -> None:
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": [group("medical", [poi("pharmacy", 100, 0)])]},
        RING,
        probe=FakeProbe(),
        k=4,
        selection="typo",
    )
    assert coverage["meta"]["selection"] == SELECTION_FAIR


@async_test
async def test_fair_selection_reports_dimension_allocation() -> None:
    groups = [
        group("medical", [poi("pharmacy", 400, 0, uid="m")]),
        group("market", [poi("market", 100, 0, uid="f1"), poi("market", 150, 0, uid="f2")]),
    ]
    coverage = await build_coverage(
        {"center": list(CENTER), "groups": groups},
        RING,
        probe=FakeProbe(),
        k=3,
        selection=SELECTION_FAIR,
    )
    allocation = coverage["meta"]["selection_allocation"]
    # ⚠️ 键是**规范键**：词表层的 `market` 会被收口成报告层的 `shopping`
    # （`canonical_dimension_key`）。配额表用哪个键，必须与 `weights.yaml` 一致。
    assert allocation == {"medical": 1, "shopping": 2}
    assert coverage["meta"]["selected_points"] == 3

    dims = {item["key"]: item for item in coverage["groups"]}
    assert dims["shopping"]["count_in"] == 2
    assert dims["medical"]["nearest_minutes"] is not None
