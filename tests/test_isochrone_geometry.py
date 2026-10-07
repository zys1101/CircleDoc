"""`services/isochrone_geometry.py` 的单元测试（PRD §4.2 Step1~Step6 的纯计算部分）。

**零真实出网**：这里没有任何 HTTP，也没有 `infra.baidu_client`；时长全部来自假 oracle。
`FanOracle` 就是「服务层该怎么喂数据」的示范实现 —— 收到 `ProbeRequest` 列表，回同样数量、
可按 `(bearing_deg, distance_m)` 配对的 `ProbeOutcome` 列表；二分轮次由被测模块驱动。

口径来源（改测试前先读 PRD）：
* 二分容差 ±30 s、最多 5 轮 → §4.2 Step2 / FR-201；
* `R_max` 只是**搜索上界**、理论半径按 3.9 km/h → §4.2 第 269 行；
* 五种顶点标签两两不可合并 → §4.2 第 271 行；
* 加密只在相邻耗时差 > 60 s 触发、总顶点 12~36 → §4.2 第 244~246 行；
* DP 15 m / IDW 250 m p=2 取最近 8 个 → §4.2 Step4 / Step6。
"""

from __future__ import annotations

import ast
import math
import pathlib
import random

import pytest
import services.isochrone_geometry as geo
from services.isochrone_geometry import (
    BisectionMachine,
    DurationField,
    IsochroneConfig,
    LngLat,
    ProbeOutcome,
    ProbeRequest,
    SampleKind,
    Vertex,
)
from services.isochrone_geometry import OutcomeKind as Block

CENTER = LngLat(116.397428, 39.90923)
"""一个境内城区中心（故宫附近）。全模块按 BD09LL 原样进出，不做坐标系转换。"""

TARGET = 900.0
"""15 分钟。"""


# --------------------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------------------
class FanOracle:
    """假探测层：`duration = 距离 / 该方向速度`。速度不同 → 等时圈是不规则形（FR-201）。"""

    def __init__(
        self,
        speeds: dict[float, float],
        *,
        default_speed: float = 0.85,
        blocked: dict[float, Block] | None = None,
        block_above_m: float = 0.0,
        evidence: str = "fake: 官方禁行信号",
        jitter_s: float = 0.0,
        seed: int = 2026,
    ) -> None:
        self.speeds = dict(speeds)
        self.default_speed = default_speed
        self.blocked = dict(blocked or {})
        self.block_above_m = block_above_m
        self.evidence = evidence
        self.jitter_s = jitter_s
        self._rng = random.Random(seed)
        self.calls = 0
        self.probes = 0
        self.largest_batch = 0
        self.seen_distances: dict[float, list[float]] = {}

    def probe(self, center: LngLat, probes: tuple[ProbeRequest, ...]) -> list[ProbeOutcome]:
        assert center == CENTER, "oracle 收到的中心点必须就是被计算的那个点"
        self.calls += 1
        self.probes += len(probes)
        self.largest_batch = max(self.largest_batch, len(probes))
        out: list[ProbeOutcome] = []
        for request in probes:
            bearing = round(request.bearing_deg, 6)
            distance = float(request.distance_m)
            self.seen_distances.setdefault(bearing, []).append(distance)
            kind = self.blocked.get(bearing)
            if kind is not None and distance > self.block_above_m:
                out.append(
                    ProbeOutcome(
                        bearing_deg=bearing,
                        distance_m=distance,
                        kind=kind,
                        evidence=self.evidence,
                    )
                )
                continue
            speed = self.speeds.get(bearing, self.default_speed)
            duration = distance / speed
            if self.jitter_s:
                duration += self._rng.uniform(-self.jitter_s, self.jitter_s)
            out.append(
                ProbeOutcome(
                    bearing_deg=bearing,
                    distance_m=distance,
                    kind=Block.OK,
                    duration_s=max(1.0, duration),
                    distance_traveled_m=distance,
                )
            )
        return out


def _vertex(
    bearing: float,
    distance: float,
    *,
    kind: SampleKind = SampleKind.MEASURED,
    duration: float | None = TARGET,
    confidence: float = 0.95,
) -> Vertex:
    return Vertex(
        bearing_deg=bearing,
        point=geo.destination(CENTER, bearing, distance),
        distance_m=distance,
        duration_s=duration,
        kind=kind,
        confidence=confidence,
    )


def _ok(bearing: float, distance: float, duration: float) -> ProbeOutcome:
    return ProbeOutcome(
        bearing_deg=bearing, distance_m=distance, kind=Block.OK, duration_s=duration
    )


def _blocked(bearing: float, distance: float, kind: Block, *, evidence: str) -> ProbeOutcome:
    return ProbeOutcome(bearing_deg=bearing, distance_m=distance, kind=kind, evidence=evidence)


def _drive(machine: BisectionMachine, measure) -> int:
    """把「状态机要距离 → 测试给结论」这条回路走完，返回提交次数（服务层同款写法）。"""
    submitted = 0
    while True:
        distance = machine.next_probe_m()
        if distance is None or not machine.pending:
            return submitted
        machine.submit(measure(machine.bearing_deg, distance))
        submitted += 1


def _speeds(**overrides: float) -> dict[float, float]:
    base = dict.fromkeys(geo.coarse_bearings(), 0.85)
    for name, value in overrides.items():
        base[float(name.lstrip("b"))] = value
    return base


# --------------------------------------------------------------------------------------
# 1. 球面正算 / 反算 / 方位角集合
# --------------------------------------------------------------------------------------
def test_destination_roundtrip_keeps_distance_and_bearing() -> None:
    """`destination` 与 `distance_m` 必须互逆 —— 半径口径歪了，整个等时圈就歪了。"""
    for bearing in (0.0, 45.0, 90.0, 135.0, 180.0, 270.0, 337.5):
        for distance in (120.0, 765.0, 1250.0, 4000.0):
            point = geo.destination(CENTER, bearing, distance)
            assert geo.distance_m(CENTER, point) == pytest.approx(distance, abs=0.35)
            assert geo.initial_bearing(CENTER, point) == pytest.approx(bearing, abs=0.02)


def test_destination_zero_distance_returns_center() -> None:
    assert geo.destination(CENTER, 90.0, 0.0) == CENTER
    with pytest.raises(ValueError):
        geo.destination(CENTER, 90.0, -1.0)


def test_as_lnglat_accepts_three_spellings_and_rejects_shapes() -> None:
    assert geo.as_lnglat([CENTER.lng, CENTER.lat]) == CENTER
    assert geo.as_lnglat({"lng": CENTER.lng, "lat": CENTER.lat}) == CENTER
    assert geo.as_lnglat(CENTER) == CENTER
    with pytest.raises(ValueError):
        geo.as_lnglat([CENTER.lng, CENTER.lat, 0.0])
    with pytest.raises(ValueError):
        geo.as_lnglat({"lng": CENTER.lng})
    with pytest.raises(ValueError):
        geo.as_lnglat(LngLat(200.0, 39.9))


def test_bearing_sets_are_twelve_and_twenty_four() -> None:
    """粗扫 12 个（30° 间隔）、加密 24 个（15° 间隔）—— PRD §4.2 Step1 / Step3。"""
    coarse = geo.coarse_bearings()
    fine = geo.fine_bearings()
    assert len(coarse) == 12 and len(fine) == 24
    assert coarse == tuple(float(i * 30) for i in range(12))
    assert all(b in fine for b in coarse), "15° 网格必须包含 30° 网格"
    assert len(set(fine) - set(coarse)) == 12, "净新增 12 个候选方位角"
    assert sorted(fine) == list(fine), "方位角集合本身要升序，排序步骤才有稳定输入"
    assert geo.mid_bearing(345.0, 15.0) == 0.0, "跨 0° 的中点要回绕，不能算成 180°"


# --------------------------------------------------------------------------------------
# 2. R_max / 理论半径（PRD 第 269 行的教训）
# --------------------------------------------------------------------------------------
def test_theoretical_radius_is_below_the_search_bound() -> None:
    walking = geo.get_mode_spec("walking")
    assert walking.r_max_m == 1250.0
    assert pytest.approx(3.9) == geo.WALKING_SPEED_KMH
    theoretical = walking.theoretical_radius_m(TARGET)
    assert theoretical == pytest.approx(975.0, abs=1.0), "3.9 km/h × 15 min ≈ 1.0 km"
    assert theoretical < walking.r_max_m, "理论半径只用于阈值；二分上界宁大勿小"
    assert geo.theoretical_radius_m(900.0, 3.9) == pytest.approx(975.0, abs=1.0)


def test_search_bound_never_shrinks_with_coarse_ratio() -> None:
    """调低首探距离能省配额，但**不许**把上界拉低（顺畅方向会被截成假圆）。"""
    walking = geo.get_mode_spec("walking")
    cfg = IsochroneConfig(coarse_distance_ratio=0.5)
    assert cfg.r_max_for(walking) == 1250.0
    assert cfg.coarse_distance_for(walking) == pytest.approx(625.0)
    assert geo.get_mode_spec("riding").r_max_m == 4000.0
    assert geo.get_mode_spec("riding").theoretical_radius_m() is None, "官方无骑行速度口径，不猜数"
    with pytest.raises(ValueError):
        geo.get_mode_spec("subway")


# --------------------------------------------------------------------------------------
# 3. 二分收敛状态机
# --------------------------------------------------------------------------------------
def test_bisection_converges_within_round_limit() -> None:
    machine = BisectionMachine(45.0, 600.0, 1000.0, inner_seed_m=100.0)
    submitted = _drive(machine, lambda b, d: _ok(b, d, d))  # 1 m/s → 边界在 600 m
    vertex = machine.to_vertex(CENTER)
    assert machine.status is geo.DirectionStatus.CONVERGED
    assert submitted <= machine.max_rounds + 1, "1 次粗扫 + 最多 5 轮二分"
    assert machine.bisect_rounds <= 5
    assert vertex.kind is SampleKind.MEASURED
    assert vertex.duration_s == pytest.approx(606.25)
    assert abs(vertex.duration_s - 600.0) <= geo.TOLERANCE_S
    assert vertex.confidence == geo.KIND_CONFIDENCE[SampleKind.MEASURED]


def test_tolerance_is_inclusive_at_exactly_thirty_seconds() -> None:
    """±30 s 是**闭区间**：刚好 30 s 就算命中，不该再多烧一次配额。"""
    machine = BisectionMachine(0.0, 600.0, 1000.0, inner_seed_m=100.0)
    machine.next_probe_m()
    machine.submit(_ok(0.0, 1000.0, 630.0))
    assert machine.status is geo.DirectionStatus.CONVERGED
    assert machine.resolved_kind is SampleKind.MEASURED


def test_just_over_tolerance_keeps_bisecting() -> None:
    machine = BisectionMachine(0.0, 600.0, 1000.0, inner_seed_m=100.0)
    machine.next_probe_m()
    machine.submit(_ok(0.0, 1000.0, 630.1))
    assert machine.pending is True
    assert machine.next_probe_m() == pytest.approx(550.0)
    assert machine.lo_m == 100.0 and machine.hi_m == 1000.0


def test_rounds_exhausted_falls_back_with_lower_confidence() -> None:
    """阶梯型耗时（永远落不进容差）→ 不收敛时回退插值并降置信度（PRD 第 271 行）。"""
    machine = BisectionMachine(0.0, 600.0, 1000.0, inner_seed_m=100.0)
    submitted = _drive(machine, lambda b, d: _ok(b, d, 400.0 if d < 700.0 else 900.0))
    assert submitted == 6, "1 次粗扫 + 5 轮二分后必须停手"
    assert machine.bisect_rounds == 5
    vertex = machine.to_vertex(CENTER)
    assert vertex.kind is SampleKind.INTERPOLATED
    assert vertex.confidence == geo.CONFIDENCE_EXHAUSTED
    assert vertex.confidence < geo.KIND_CONFIDENCE[SampleKind.INTERPOLATED]
    assert 662.0 < vertex.distance_m < 719.0, "内插位置要夹在两侧实测样本之间"


def test_smooth_direction_at_r_max_is_extrapolated_once() -> None:
    """R_max 处实测耗时仍小于目标（路网顺畅）→ 外推一次并标 `extrapolated`（PRD 239）。"""
    machine = BisectionMachine(90.0, 900.0, 1000.0, inner_seed_m=100.0)
    submitted = _drive(machine, lambda b, d: _ok(b, d, d / 2.0))  # 2 m/s → 1800 m 才到 900 s
    vertex = machine.to_vertex(CENTER)
    assert submitted == 1, "顺畅方向只测上界那一枪，外推是算出来的不是再测出来的"
    assert vertex.kind is SampleKind.EXTRAPOLATED
    assert vertex.distance_m > 1000.0, "外推必须越过 R_max，否则就是把顺畅方向截断"
    assert vertex.distance_m == pytest.approx(1200.0, abs=1.0)
    assert machine.next_probe_m() is None
    assert vertex.confidence == geo.KIND_CONFIDENCE[SampleKind.EXTRAPOLATED]
    assert vertex.evidence is None and not vertex.blocked


def test_extrapolation_uses_outer_slope_and_is_capped() -> None:
    """首探设在 R_max 之内时，先补测上界，再用最外侧两点的斜率外推（不放大误差）。"""
    machine = BisectionMachine(0.0, 900.0, 1000.0, coarse_distance_m=800.0, inner_seed_m=100.0)
    submitted = _drive(machine, lambda b, d: _ok(b, d, d / 2.0))
    vertex = machine.to_vertex(CENTER)
    assert submitted == 2, "首探一枪 + 补测上界一枪，此后只外推"
    assert vertex.kind is SampleKind.EXTRAPOLATED
    assert vertex.distance_m == pytest.approx(1200.0, abs=1.0)
    assert vertex.distance_m <= machine.effective_r_max_m + 1e-6, "外推被 cap 住"


def test_pending_machine_refuses_to_emit_a_vertex() -> None:
    machine = BisectionMachine(0.0, 900.0, 1000.0)
    machine.next_probe_m()
    with pytest.raises(ValueError):
        machine.to_vertex(CENTER)


def test_missing_probe_result_is_blocked_timeout_never_official() -> None:
    """服务层漏回 = 「我没测准」，绝不能升级成「官方说走不通」（话术与修复动作不同）。"""
    machine = BisectionMachine(0.0, 900.0, 1000.0, max_blocked_probes=0)
    machine.next_probe_m()
    machine.submit(None)
    vertex = machine.to_vertex(CENTER)
    assert vertex.kind is SampleKind.BLOCKED_TIMEOUT
    assert vertex.confidence <= geo.KIND_CONFIDENCE[SampleKind.BLOCKED_TIMEOUT]
    assert "未返回" in (vertex.evidence or "")


# --------------------------------------------------------------------------------------
# 4. 五种标签：blocked_official 与 blocked_timeout 绝不混淆
# --------------------------------------------------------------------------------------
def test_blocked_official_and_timeout_are_distinct() -> None:
    official = BisectionMachine(0.0, 900.0, 1000.0, max_blocked_probes=0)
    official.next_probe_m()
    official.submit(_blocked(0.0, 1000.0, Block.BLOCKED_OFFICIAL, evidence="links[].attr=0x01"))
    vertex_official = official.to_vertex(CENTER)

    timeout = BisectionMachine(180.0, 900.0, 1000.0, max_blocked_probes=0)
    timeout.next_probe_m()
    timeout.submit(_blocked(180.0, 1000.0, Block.BLOCKED_TIMEOUT, evidence="耗时超标/空结果"))
    vertex_timeout = timeout.to_vertex(CENTER)

    assert vertex_official.kind is SampleKind.BLOCKED_OFFICIAL
    assert vertex_timeout.kind is SampleKind.BLOCKED_TIMEOUT
    assert vertex_official.kind is not vertex_timeout.kind
    assert vertex_official.confidence > vertex_timeout.confidence
    assert vertex_official.evidence == "links[].attr=0x01"
    assert vertex_official.blocked and vertex_timeout.blocked
    assert vertex_official.as_dict()["sample_label"] == "官方禁行"
    assert vertex_timeout.as_dict()["sample_label"] == "未测准"
    assert vertex_official.blocked_above_m == pytest.approx(1000.0)


def test_official_evidence_outranks_timeout_evidence() -> None:
    machine = BisectionMachine(0.0, 900.0, 1000.0, inner_seed_m=100.0, max_blocked_probes=1)
    machine.next_probe_m()
    machine.submit(_blocked(0.0, 1000.0, Block.BLOCKED_TIMEOUT, evidence="空结果"))
    assert machine.pending, "上界被挡住不等于这个方向不可达，还能向内试一枪"
    machine.next_probe_m()
    machine.submit(_blocked(0.0, 550.0, Block.BLOCKED_OFFICIAL, evidence="restrictions=0x01"))
    vertex = machine.to_vertex(CENTER)
    assert vertex.kind is SampleKind.BLOCKED_OFFICIAL, "两种证据同时在场时按官方硬信号定案"
    assert vertex.evidence.startswith("restrictions=0x01")
    assert "空结果" in vertex.evidence


def test_blocked_at_ceiling_still_measures_the_real_boundary() -> None:
    """R_max 处遇阻、向内能走到 → 顶点是 `measured`，阻隔信息留在 `blocked_above_m`。"""
    machine = BisectionMachine(0.0, 600.0, 1000.0, inner_seed_m=100.0, max_blocked_probes=1)
    machine.next_probe_m()
    machine.submit(_blocked(0.0, 1000.0, Block.BLOCKED_OFFICIAL, evidence="水域"))
    machine.next_probe_m()
    machine.submit(_ok(0.0, 550.0, 550.0))
    machine.next_probe_m()
    machine.submit(_ok(0.0, 775.0, 775.0))
    machine.next_probe_m()
    machine.submit(_ok(0.0, 662.5, 620.0))
    vertex = machine.to_vertex(CENTER)
    assert vertex.kind is SampleKind.MEASURED
    assert not vertex.blocked
    assert vertex.blocked_above_m == pytest.approx(1000.0)
    assert vertex.evidence == "水域"


def test_foreign_bearing_sample_is_not_mixed_in() -> None:
    machine = BisectionMachine(90.0, 900.0, 1000.0)
    machine.next_probe_m()
    machine.submit(_ok(45.0, 500.0, 900.0))
    assert machine.samples == [], "45° 的样本绝不能记到 90° 方向头上"
    assert machine.status is geo.DirectionStatus.PENDING


# --------------------------------------------------------------------------------------
# 5. 排序 / 自交 / 面积 / 射线法
# --------------------------------------------------------------------------------------
def test_ring_is_sorted_by_bearing_ascending() -> None:
    scrambled = [_vertex(180.0, 700.0), _vertex(0.0, 750.0), _vertex(90.0, 690.0)]
    ordered = geo.sort_ring_by_bearing(scrambled)
    assert [v.bearing_deg for v in ordered] == [0.0, 90.0, 180.0]


def test_unsorted_bowtie_is_detected_as_self_intersecting() -> None:
    bowtie = [LngLat(0, 0), LngLat(2, 2), LngLat(2, 0), LngLat(0, 2)]
    assert geo.ring_is_simple(bowtie) is False
    circle = [geo.destination(CENTER, b, 800.0) for b in geo.coarse_bearings()]
    assert geo.ring_is_simple(circle) is True


def test_ring_area_of_one_km_square() -> None:
    """鞋带 + 纬度尺度校正的量级自检：用球面正算做出 1 km × 1 km 方环。"""
    north = geo.destination(CENTER, 0.0, 500.0)
    south = geo.destination(CENTER, 180.0, 500.0)
    west = geo.destination(CENTER, 270.0, 500.0)
    east = geo.destination(CENTER, 90.0, 500.0)
    square = [
        LngLat(west.lng, north.lat),
        LngLat(east.lng, north.lat),
        LngLat(east.lng, south.lat),
        LngLat(west.lng, south.lat),
    ]
    assert geo.ring_area_km2(square) == pytest.approx(1.0, rel=0.02)
    assert geo.ring_area_km2(square[:2]) == 0.0


def test_point_in_ring_on_concave_polygon() -> None:
    """凹多边形（L 形）：射线法在凹进去的那块必须判外面。"""
    lshape = [
        LngLat(0.0, 0.0),
        LngLat(4.0, 0.0),
        LngLat(4.0, 2.0),
        LngLat(2.0, 2.0),
        LngLat(2.0, 4.0),
        LngLat(0.0, 4.0),
    ]
    assert geo.point_in_ring(LngLat(1.0, 1.0), lshape) is True
    assert geo.point_in_ring(LngLat(1.0, 3.5), lshape) is True
    assert geo.point_in_ring(LngLat(3.0, 1.0), lshape) is True
    assert geo.point_in_ring(LngLat(3.0, 3.0), lshape) is False, "凹口里不算圈内"
    assert geo.point_in_ring(LngLat(5.0, 1.0), lshape) is False
    assert geo.point_in_ring(LngLat(-1.0, 1.0), lshape) is False
    assert geo.point_in_ring(LngLat(1.0, 1.0), lshape[:2]) is False, "不足 3 点不成环"


def test_point_in_ring_accepts_vertices_and_dicts() -> None:
    ring = [_vertex(bearing, 800.0) for bearing in geo.coarse_bearings()]
    assert geo.point_in_ring(CENTER, ring) is True
    assert geo.point_in_ring({"lng": CENTER.lng, "lat": CENTER.lat}, ring) is True
    assert geo.point_in_ring(geo.destination(CENTER, 0.0, 3000.0), ring) is False
    assert geo.point_in_ring(geo.destination(CENTER, 0.0, 240.0), ring) is True, "M3 落圈判定"


# --------------------------------------------------------------------------------------
# 6. Douglas-Peucker 闭合环简化
# --------------------------------------------------------------------------------------
def test_cross_track_distance_matches_point_to_segment() -> None:
    a, b = LngLat(0.0, 0.0), LngLat(0.0, 1.0)  # 一段 ~111 km 的经线弦
    # 0.001° 经度在赤道附近 ≈ 111.3 m；口径是球面垂距，不是平面投影
    assert geo.cross_track_distance_m(a, b, LngLat(0.001, 0.5)) == pytest.approx(111.3, abs=2.0)
    assert geo.cross_track_distance_m(a, b, LngLat(0.0, 0.5)) == pytest.approx(0.0, abs=1.0)
    assert geo.cross_track_distance_m(a, b, a) == pytest.approx(0.0, abs=1.0)
    behind = LngLat(0.0, -0.2)
    assert geo.cross_track_distance_m(a, b, behind) == pytest.approx(
        geo.distance_m(a, behind), abs=1.0
    ), "垂足落在弦外时退化成到较近端点的距离"


def test_dp_reduces_vertex_count_and_keeps_ring_simple() -> None:
    """15 m 容差下顶点数下降，且简化后仍不自交（PRD §4.2 Step4 / 风险 R4）。

    半径取 400 m：圆弧自身对「隔点弦」的底线偏离 400×(1-cos15°)≈14 m，刚好落在容差内侧，
    对应「边界已平滑、只是采样过密」。1 km 圈在 15 m 容差下删不掉点，那是 DP 的正常行为
    （真拐点删了会切角），见 `simplify_ring` 的口径提醒。
    """
    ring = [
        _vertex(bearing, 400.0 + (1.0 if index % 2 else 0.0))
        for index, bearing in enumerate(geo.fine_bearings())
    ]
    simplified = geo.simplify_ring(ring, tolerance_m=15.0)
    assert 3 <= len(simplified) < len(ring), f"冗余采样点该被压掉：{len(simplified)}/{len(ring)}"
    assert geo.ring_is_simple(ring)
    assert geo.ring_is_simple(simplified)
    assert [v.bearing_deg for v in simplified] == sorted(v.bearing_deg for v in simplified)
    assert geo.douglas_peucker_ring(ring, tolerance_m=0.0) == tuple(range(len(ring)))


def test_dp_keeps_blocked_vertices() -> None:
    """阻隔顶点不许被简化掉：抹掉那个齿，等于把「路走不通」画成顺畅边界。"""
    ring = [
        _vertex(
            bearing,
            300.0 if bearing == 180.0 else 400.0 + (1.0 if index % 2 else 0.0),
            kind=SampleKind.BLOCKED_OFFICIAL if bearing == 180.0 else SampleKind.MEASURED,
            duration=None if bearing == 180.0 else TARGET,
        )
        for index, bearing in enumerate(geo.fine_bearings())
    ]
    simplified = geo.simplify_ring(ring, tolerance_m=15.0)
    assert len(simplified) < len(ring)
    kept = [v for v in simplified if v.bearing_deg == 180.0]
    assert len(kept) == 1 and kept[0].kind is SampleKind.BLOCKED_OFFICIAL


# --------------------------------------------------------------------------------------
# 7. Step3 自适应加密
# --------------------------------------------------------------------------------------
def _gap_ring() -> list[Vertex]:
    return [
        _vertex(0.0, 700.0, duration=860.0),
        _vertex(30.0, 800.0, duration=950.0),
        _vertex(60.0, 765.0, duration=900.0),
        _vertex(90.0, 765.0, duration=910.0),
        _vertex(120.0, 765.0, duration=880.0),
    ]


def test_refine_only_triggers_on_gap_over_sixty_seconds() -> None:
    """只有「相邻顶点耗时差 > 60 s」的方向对才出网加密，其余走插值（PRD 第 244 行）。"""
    ring = _gap_ring()
    plans = geo.plan_gap_probes(ring, CENTER, TARGET)
    probed = [p for p in plans if p.needs_probe]
    assert len(probed) == 1, f"只有 0°~30° 那一对差 90 s，实测触发数 {len(probed)}"
    assert probed[0].bearing_deg == 15.0
    assert 700.0 <= probed[0].probe.distance_m < 800.0, "猜测距离由两侧内插，不越过邻点"
    assert len(plans) == 5, "其余缝隙仍然生成（不出网的）加密计划"
    assert 240.0 in {p.bearing_deg for p in plans}, "首尾闭合那道缝隙（120°→0°）也要有计划"
    assert all(not p.needs_probe for p in plans if p.bearing_deg != 15.0)


def test_refinement_budget_keeps_vertices_between_twelve_and_thirty_six() -> None:
    ring = [_vertex(b, 700.0 + i * 20) for i, b in enumerate(geo.coarse_bearings())]
    plans = geo.plan_gap_probes(ring, CENTER, TARGET, max_new=len(ring))
    assert len(plans) <= 12
    assert len(ring) + len(plans) <= geo.MAX_VERTICES


def test_refined_probe_within_tolerance_becomes_measured() -> None:
    plan = next(p for p in geo.plan_gap_probes(_gap_ring(), CENTER, TARGET) if p.needs_probe)
    vertex = geo.apply_gap_plan(
        plan,
        _ok(plan.bearing_deg, plan.probe.distance_m, TARGET + 12.0),
        TARGET,
    )
    assert vertex.kind is SampleKind.MEASURED
    assert vertex.point == plan.probe.point


def test_refined_probe_outside_tolerance_stays_interpolated() -> None:
    plan = next(p for p in geo.plan_gap_probes(_gap_ring(), CENTER, TARGET) if p.needs_probe)
    guessed = plan.probe.distance_m
    vertex = geo.apply_gap_plan(plan, _ok(plan.bearing_deg, guessed, TARGET + 120.0), TARGET)
    assert vertex.kind is SampleKind.INTERPOLATED
    assert vertex.distance_m < guessed, "实测明显偏慢 → 边界更靠近圆心"
    assert "超容差" in (vertex.note or "")


def test_refined_gap_blocked_keeps_label_and_shrinks_inside() -> None:
    plan = next(p for p in geo.plan_gap_probes(_gap_ring(), CENTER, TARGET) if p.needs_probe)
    official = geo.apply_gap_plan(
        plan,
        _blocked(plan.bearing_deg, plan.probe.distance_m, Block.BLOCKED_OFFICIAL, evidence="河"),
        TARGET,
    )
    timeout = geo.apply_gap_plan(
        plan,
        _blocked(plan.bearing_deg, plan.probe.distance_m, Block.BLOCKED_TIMEOUT, evidence="超时"),
        TARGET,
    )
    assert official.kind is SampleKind.BLOCKED_OFFICIAL
    assert timeout.kind is SampleKind.BLOCKED_TIMEOUT
    assert official.confidence != timeout.confidence
    assert official.distance_m < 700.0, "遇阻的探测点上不能放顶点"
    assert official.blocked_above_m == pytest.approx(plan.probe.distance_m)
    assert official.evidence == "河"


def test_blocked_vertices_do_not_trigger_refinement() -> None:
    """「没测准」不是加密理由：blocked 邻居不参与 60 s 差值比较，避免白烧配额。"""
    ring = [
        _vertex(0.0, 100.0, kind=SampleKind.BLOCKED_TIMEOUT, duration=None, confidence=0.3),
        _vertex(30.0, 900.0, duration=900.0),
    ]
    assert all(not p.needs_probe for p in geo.plan_gap_probes(ring, CENTER, TARGET))


# --------------------------------------------------------------------------------------
# 8. Step6 IDW 耗时场
# --------------------------------------------------------------------------------------
def _four_measured() -> list[Vertex]:
    return [_vertex(bearing, 500.0, duration=900.0) for bearing in (0.0, 90.0, 180.0, 270.0)]


def test_idw_prefers_measured_over_interpolated() -> None:
    """紧贴一个插值点时，估时也不能被它带走 —— 实测点优先（PRD §4.2 Step6）。"""
    measured = _four_measured()
    near_interpolated = _vertex(
        5.0, 300.0, kind=SampleKind.INTERPOLATED, duration=10.0, confidence=0.7
    )
    point = geo.destination(CENTER, 5.0, 305.0)
    value = geo.idw_duration(point, [*measured, near_interpolated])
    assert value is not None
    assert value > 600.0, "10 s 的插值点不许主导热力图"
    assert value == pytest.approx(900.0, rel=0.01)
    sources = geo.pick_idw_sources([*measured, near_interpolated], point)
    assert len(sources) == 4 and all(s.kind is SampleKind.MEASURED for s in sources)


def test_idw_backfills_with_interpolated_when_measured_are_scarce() -> None:
    measured = _four_measured()[:2]
    extras = [_vertex(45.0, 400.0, kind=SampleKind.INTERPOLATED, duration=900.0)]
    sources = geo.pick_idw_sources([*measured, *extras], CENTER, min_preferred=4)
    assert len(sources) == 3
    assert {s.kind for s in sources} == {SampleKind.MEASURED, SampleKind.INTERPOLATED}


def test_idw_never_uses_blocked_sources_and_returns_none_when_empty() -> None:
    blocked = [
        _vertex(0.0, 300.0, kind=SampleKind.BLOCKED_OFFICIAL, duration=None, confidence=0.8),
        _vertex(120.0, 400.0, kind=SampleKind.BLOCKED_TIMEOUT, duration=None, confidence=0.3),
    ]
    assert geo.idw_duration(CENTER, blocked) is None, "没有可信源点就留空洞，不填 0"
    assert geo.pick_idw_sources(blocked, CENTER) == ()


def test_duration_grid_keeps_only_inside_points() -> None:
    ring = [
        _vertex(bearing, 900.0 + 40.0 * math.sin(math.radians(bearing)))
        for bearing in geo.coarse_bearings()
    ]
    field = geo.build_duration_grid(ring, ring, step_m=250.0)
    assert isinstance(field, DurationField)
    assert field.step_m == 250.0
    assert field.rows >= 3 and field.cols >= 3
    assert field.cells, "圈内至少要有网格点"
    assert all(geo.point_in_ring(cell.point, ring) for cell in field.cells)
    assert all(cell.source_count >= 1 for cell in field.cells)
    assert field.mean_duration_s() == pytest.approx(TARGET, rel=0.25)
    assert field.to_geojson()["type"] == "FeatureCollection"


# --------------------------------------------------------------------------------------
# 9. 编排入口 compute_isochrone
# --------------------------------------------------------------------------------------
def test_compute_isochrone_end_to_end_walking() -> None:
    oracle = FanOracle(_speeds(b30=0.55, b150=1.05))
    result = geo.compute_isochrone(CENTER, oracle, TARGET, mode="walking")
    assert 12 <= len(result.ring) <= geo.MAX_VERTICES
    assert 12 <= len(result.vertices) <= geo.MAX_VERTICES, "总顶点数控制在 12~36"
    assert [v.bearing_deg for v in result.ring] == sorted(v.bearing_deg for v in result.ring)
    assert geo.ring_is_simple(result.ring)
    assert {v.kind for v in result.ring} <= set(SampleKind)
    assert oracle.largest_batch <= 50, "单次批量请求 origins × destinations ≤ 50"
    assert result.stats.oracle_calls < result.stats.probes_total, "HTTP 次数与配额条数是两个数"
    assert result.stats.r_max_m == 1250.0
    assert result.stats.theoretical_radius_m == pytest.approx(975.0, abs=1.0)
    assert result.stats.probes_total == oracle.probes


def test_walking_isochrone_area_is_in_the_right_order_of_magnitude() -> None:
    """15 min 步行圈应 ≈ 1~2 km²，不是按 R_max 画出来的 4.9 km² 大饼。"""
    result = geo.compute_isochrone(CENTER, FanOracle(_speeds()), TARGET)
    assert 0.8 < result.area_km2 < 2.5, f"面积量级不对：{result.area_km2}"
    circle_at_r_max = math.pi * (geo.WALKING_R_MAX_M / 1000.0) ** 2
    assert result.area_km2 < circle_at_r_max, "等时圈必须明显小于 R_max 正圆"


def test_isochrone_is_irregular_not_a_circle() -> None:
    """FR-201：与同半径正圆目视差异明显 → 半径极差要够大。"""
    result = geo.compute_isochrone(CENTER, FanOracle(_speeds(b0=1.15, b120=0.55)), TARGET)
    radii = [v.distance_m for v in result.ring if not v.blocked]
    assert max(radii) / max(min(radii), 1.0) > 1.4, "不该是个圆"


def test_stats_separates_http_calls_from_quota_units() -> None:
    oracle = FanOracle(_speeds())
    result = geo.compute_isochrone(CENTER, oracle, TARGET)
    payload = result.stats.as_dict()
    assert payload["quota_units_approx"] == result.stats.probes_total
    assert payload["oracle_calls"] == oracle.calls
    assert payload["oracle_calls"] < payload["quota_units_approx"]
    assert sum(result.stats.kind_counts.values()) == len(result.ring)


def test_compute_isochrone_accepts_plain_callable_oracle() -> None:
    def oracle(center: LngLat, probes: tuple[ProbeRequest, ...]) -> list[ProbeOutcome]:
        return [_ok(p.bearing_deg, p.distance_m, p.distance_m / 0.85) for p in probes]

    result = geo.compute_isochrone([CENTER.lng, CENTER.lat], oracle, TARGET)
    assert len(result.ring) >= 12
    assert result.center == CENTER


@pytest.mark.parametrize(
    ("kind", "sample_kind"),
    [
        (Block.BLOCKED_OFFICIAL, SampleKind.BLOCKED_OFFICIAL),
        (Block.BLOCKED_TIMEOUT, SampleKind.BLOCKED_TIMEOUT),
    ],
)
def test_compute_isochrone_keeps_the_two_blocked_labels_apart(kind, sample_kind) -> None:
    """同一方向、同一探测序列，只换证据类型 → 标签与置信度必须跟着分家。"""
    oracle = FanOracle(_speeds(), blocked={0.0: kind})
    result = geo.compute_isochrone(CENTER, oracle, TARGET)
    counts = result.stats.kind_counts
    assert counts[sample_kind.value] == 1
    other = {SampleKind.BLOCKED_OFFICIAL, SampleKind.BLOCKED_TIMEOUT} - {sample_kind}
    assert counts[other.pop().value] == 0
    vertex = next(v for v in result.ring if v.blocked)
    assert vertex.kind is sample_kind
    assert vertex.confidence == geo.KIND_CONFIDENCE[sample_kind]


def test_fully_blocked_direction_keeps_center_inside_ring() -> None:
    """整段禁行的方向默认停在 60 m 而不是圆心：环自碰到圆心时，FR-202 的多边形失真、
    M3 的 `point_in_ring(中心)` 也退化成边界情况。标签与证据不受影响，只是画图口径。"""
    oracle = FanOracle(_speeds(), blocked={0.0: Block.BLOCKED_OFFICIAL})
    result = geo.compute_isochrone(CENTER, oracle, TARGET)
    vertex = next(v for v in result.ring if v.kind is SampleKind.BLOCKED_OFFICIAL)
    assert vertex.distance_m == pytest.approx(60.0)
    assert vertex.evidence, "收缩顶点仍要带着官方禁行证据"
    assert geo.point_in_ring(CENTER, result.ring) is True
    assert geo.ring_is_simple(result.ring)
    # 显式要「咬到圆心的齿」时仍然可得
    tight = geo.compute_isochrone(
        CENTER,
        FanOracle(_speeds(), blocked={0.0: Block.BLOCKED_OFFICIAL}),
        TARGET,
        config=IsochroneConfig(blocked_no_data_radius_m=0.0),
    )
    assert next(v for v in tight.ring if v.blocked).distance_m == pytest.approx(0.0)


def test_compute_isochrone_smooth_directions_are_extrapolated() -> None:
    """0° 方向速度极快 → 该顶点必须是 extrapolated，而不是被 R_max 截断成 measured。"""
    result = geo.compute_isochrone(CENTER, FanOracle(_speeds(b0=2.5)), TARGET)
    vertex = next(v for v in result.ring if v.bearing_deg == 0.0)
    assert vertex.kind is SampleKind.EXTRAPOLATED
    assert vertex.distance_m > geo.WALKING_R_MAX_M


def test_lightweight_mode_skips_refinement_for_fr107() -> None:
    """FR-107 移动端轻量档：只跑 12 方向粗扫、跳过加密与热力场。"""
    oracle = FanOracle(_speeds())
    cfg = IsochroneConfig(refine=False, build_duration_field=False)
    result = geo.compute_isochrone(CENTER, oracle, TARGET, config=cfg)
    assert len(result.vertices) == 12
    assert result.duration_field is None
    assert result.stats.kind_counts["interpolated"] == 0
    assert oracle.calls <= 6, "1 次粗扫 + 最多 5 轮二分"


def test_riding_mode_uses_four_km_upper_bound() -> None:
    oracle = FanOracle(dict.fromkeys(geo.coarse_bearings(), 4.0), default_speed=4.0)
    result = geo.compute_isochrone(CENTER, oracle, TARGET, mode="riding")
    assert result.mode == "riding"
    assert result.stats.r_max_m == 4000.0
    assert max(d for distances in oracle.seen_distances.values() for d in distances) <= 4000.0 + 1


def test_probe_distances_are_never_repeated_per_direction() -> None:
    """同一方向重复测同一个距离 = 白烧配额，状态机必须避免。"""
    oracle = FanOracle(_speeds(b30=0.55))
    geo.compute_isochrone(CENTER, oracle, TARGET)
    for bearing, distances in oracle.seen_distances.items():
        assert len(distances) == len({round(d) for d in distances}), f"方位 {bearing}° 重复探测"


def test_progress_callback_reports_remaining_directions() -> None:
    """FR-205：进度是「已测 / 总数」，本模块每出一批报一条。"""
    seen: list[tuple[str, int, int]] = []
    geo.compute_isochrone(
        CENTER,
        FanOracle(_speeds()),
        TARGET,
        progress=lambda p: seen.append((p.stage, p.round_index, p.pending_directions)),
    )
    assert seen[0] == ("coarse", 0, 12)
    assert all(stage in {"coarse", "bisect", "refine"} for stage, _, _ in seen)
    assert [stage for stage, _, _ in seen] == sorted(
        [stage for stage, _, _ in seen], key={"coarse": 0, "bisect": 1, "refine": 2}.get
    )
    assert seen[-1][2] == 0, "最后一轮结束时所有方向都已定案"


def test_result_geojson_is_closed_lng_lat_polygon() -> None:
    result = geo.compute_isochrone(CENTER, FanOracle(_speeds()), TARGET)
    feature = result.to_geojson()
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Polygon"
    ring = feature["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1], "GeoJSON 线性环必须闭合"
    assert len(ring) == len(result.ring) + 1
    assert feature["properties"]["coordinate_system"] == "bd09ll"
    assert all(len(point) == 2 for point in ring)


def test_vertex_dict_carries_label_confidence_and_evidence() -> None:
    vertex = _vertex(45.0, 600.0, kind=SampleKind.BLOCKED_OFFICIAL, duration=None, confidence=0.8)
    data = vertex.as_dict()
    assert data["sample_kind"] == "blocked_official"
    assert data["sample_label"] == "官方禁行"
    assert data["blocked"] is True
    assert data["radius_m"] == pytest.approx(600.0)
    assert data["point"] == [vertex.point.lng, vertex.point.lat], "对外顺序是 [lng, lat]"


def test_compute_isochrone_rejects_bad_input() -> None:
    oracle = FanOracle(_speeds())
    with pytest.raises(ValueError):
        geo.compute_isochrone(CENTER, oracle, 0.0)
    with pytest.raises(ValueError):
        geo.compute_isochrone(CENTER, object(), TARGET)


# --------------------------------------------------------------------------------------
# 10. 架构约束
# --------------------------------------------------------------------------------------
def test_geometry_module_imports_only_stdlib_math_bits() -> None:
    """零出网 / 零 HTTP 的**结构性**保证：本模块只 import 数学与数据类。

    用 AST 而不是运行时反射 —— 一旦有人为了「方便」在几何层 import httpx 或 infra，
    这条测试先红，避免纯计算层被污染成第二套调用链。
    """
    allowed = {"__future__", "math", "dataclasses", "enum", "typing", "collections"}
    source = pathlib.Path(geo.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= allowed, f"几何层出现了 I/O 依赖：{names - allowed}"


def test_every_sample_kind_is_exported_and_distinct() -> None:
    """五种标签必须齐全、值互不相同、置信度两两有区分度（PRD 第 271 行）。"""
    kinds = list(SampleKind)
    assert len(kinds) == 5
    assert {k.value for k in kinds} == {
        "measured",
        "interpolated",
        "extrapolated",
        "blocked_official",
        "blocked_timeout",
    }
    confidences = [geo.KIND_CONFIDENCE[k] for k in kinds]
    assert len(set(confidences)) == len(confidences)
    assert [k for k in kinds if k.is_blocked] == [
        SampleKind.BLOCKED_OFFICIAL,
        SampleKind.BLOCKED_TIMEOUT,
    ]
    assert {k.label_cn for k in kinds} == {"实测", "内插", "外推", "官方禁行", "未测准"}
    assert geo.DIRECTION_STATUS_TO_SAMPLE_KIND[geo.DirectionStatus.FALLBACK] is (
        SampleKind.INTERPOLATED
    )
