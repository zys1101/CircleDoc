"""等时圈纯几何算法层（PRD §4.2 Step1~Step6 中**所有不需要出网**的部分）。

设计口径（M2，四条硬约束决定了本文件的形状）：

1. **零 I/O、零 asyncio、零 HTTP/百度概念**。这里只有球面三角、状态机与插值。
   「批量算路」属于服务层（route_probe），它通过本模块的 `IsochroneOracle` 协议被调用；
   二分轮次的**驱动逻辑留在本模块**，出网留在服务层。blocked 的证据由调用方**原样传入**
   （`ProbeOutcome.kind` + `evidence` 文本），本模块**不解释任何响应体**。
2. **全链路 BD09LL**：坐标一律 `(lng, lat)` 具名字段进出，本模块**不做任何坐标系转换**，
   只有球面正/反算（PRD §4.2 的探测点是「沿大圆方位角直推」，与路网无关）。
3. **顶点标签是语义标签**：`measured` / `interpolated` / `extrapolated` /
   `blocked_official` / `blocked_timeout` 五种两两不可合并（PRD §4.2 精度取舍）。
   `blocked_official` =「有官方硬证据说这条路走不通」（修复动作：换中心点/换方式），
   `blocked_timeout` =「我没测准或返回空」（修复动作：重试），话术完全不同；
   `extrapolated` =「该方向路网顺畅，R_max 处仍未到时限，边界是外推的」。
   每个顶点都带 `confidence`（PRD 第 271 行「不收敛 → 回退插值并降置信度」）。
4. **R_max 是二分上界，不是理论半径**。步行 1.25 km 只作「宁大勿小」的搜索上界，
   阈值与文案一律按官方推算的 3.9 km/h（15 min ≈ 975 m）写 —— PRD 第 269 行明确纠正过
   v1.0 按 5 km/h 估的乐观假设，本模块用 `theoretical_radius_m()` 与 `r_max_for()` 把两件事
   拆成两个函数，防止调用方混用。

近似声明（README 要照抄这段）：扇形采样 + 直线推点是**近似**方法，无法捕捉路网细粒度凹凸
（如内部小巷）。不确定性用两个参数表达：二分容差 `TOLERANCE_S = 30 s`、网格 `GRID_STEP_M = 250 m`。
"""

# ======================================================================================
# 公共 API（后续代理按这里的签名引用；以本文件实现为准）
# --------------------------------------------------------------------------------------
# 常量
#   EARTH_RADIUS_M = 6_378_137.0
#   COARSE_BEARING_COUNT = 12 / COARSE_BEARING_STEP_DEG = 30.0     # Step1
#   FINE_BEARING_COUNT = 24 / FINE_BEARING_STEP_DEG = 15.0         # Step3
#   TOLERANCE_S = 30.0 / MAX_BISECT_ROUNDS = 5 / MIN_INTERVAL_M = 20.0     # Step2
#   DP_TOLERANCE_M = 15.0                                          # Step4
#   GRID_STEP_M = 250.0 / IDW_POWER = 2.0 / IDW_NEIGHBOURS = 8     # Step6
#   REFINE_DURATION_GAP_S = 60.0                                   # Step3 触发阈值
#   MIN_VERTICES = 12 / MAX_VERTICES = 36
#   DEFAULT_TARGET_SECONDS = 900.0
#   MODE_WALKING / MODE_RIDING / WALKING_R_MAX_M = 1250.0 / RIDING_R_MAX_M = 4000.0
#   WALKING_SPEED_KMH = 3.9（只用于阈值与文案，**不作二分上界**）
#   BLOCKED_GAP_SHRINK_RATIO = 0.5    # 缝隙里遇阻顶点的收缩比例
#   KIND_CONFIDENCE: Mapping[SampleKind, float]  # 0.95/0.70/0.45/0.80/0.30
#   CONFIDENCE_MEASURED_TIGHT = 0.85 / CONFIDENCE_EXHAUSTED = 0.55  # 回退插值的两档置信度
#   MODES: Mapping[str, ModeSpec]
# 类型
#   LngLat(lng: float, lat: float)  # 构造即校验：非有限数或越界 → ValueError（R8 同源坑）
#   SampleKind(StrEnum) MEASURED/INTERPOLATED/EXTRAPOLATED/BLOCKED_OFFICIAL/BLOCKED_TIMEOUT
#       .is_blocked .is_measured_family .label_cn
#   OutcomeKind(StrEnum) OK/BLOCKED_OFFICIAL/BLOCKED_TIMEOUT/MISSING
#   DirectionStatus(StrEnum) PENDING/CONVERGED/FALLBACK/EXTRAPOLATED
#                          /BLOCKED_OFFICIAL/BLOCKED_TIMEOUT
#   DIRECTION_STATUS_TO_SAMPLE_KIND: Mapping[DirectionStatus, SampleKind]
#   ModeSpec(name, label, r_max_m, speed_kmh, reference_target_seconds, theoretical_radius_m())
#   ProbeRequest(bearing_deg, distance_m, point, round_index, stage) .key()
#   ProbeOutcome(bearing_deg, distance_m, kind, duration_s, distance_traveled_m, evidence)
#       .outcome .usable
#   Vertex(bearing_deg, point, distance_m, duration_s, kind, confidence, round_index,
#          evidence, blocked_above_m, traveled_m, note) .blocked .as_dict()
#   GridCell / DurationField(.cells .step_m .as_points() .mean_duration_s() .to_geojson())
#   GapPlan(center, inner, outer, bearing_deg, probe) .needs_probe .duration_gap_s
#   RingPoint = Vertex | LngLat | Sequence[float] | Mapping[str, float]
#   IsochroneConfig / RoundProgress / IsochroneStats / IsochroneResult —— 见下面「编排」
# 几何
#   as_lnglat(value: RingPoint) -> LngLat
#   destination(center, bearing_deg: float, distance_m: float) -> LngLat      # 球面正算
#   distance_m(a, b) -> float                                                 # haversine
#   initial_bearing(a, b) -> float
#   normalize_bearing(deg) -> float / mid_bearing(a_deg, b_deg) -> float   # 劣弧中点
#   gap_mid_bearing(a_deg, b_deg) -> float   # 环上相邻顶点「前向缝隙」中点（加密用这个）
#   cross_track_distance_m(a, b, p) -> float        # 点到弦的**球面**垂距（DP 用）
#   coarse_bearings() -> tuple[float, ...] / fine_bearings() -> tuple[float, ...]
#   bearing_circle(step_deg, count, offset_deg=0.0) -> tuple[float, ...]
#   make_probe_request(center, bearing_deg, distance_m, *, round_index=0, stage="bisect")
#   pair_key(bearing_deg, distance_m) -> tuple[float, int]   # 请求↔回包配对口径，服务层复用
#   theoretical_radius_m(target_seconds, speed_kmh) -> float
#   get_mode_spec(mode: str) -> ModeSpec
# 多边形
#   sort_ring_by_bearing(vertices) -> tuple[Vertex, ...]           # 方位角升序，消除自交（R4）
#   compact_ring(vertices, *, min_separation_m=1.0, center=None) -> tuple[Vertex, ...]
#   point_in_ring(lnglat, ring) -> bool            # 射线法。M3 落圈判定复用，签名稳定
#   ring_is_simple(ring) -> bool
#   ring_area_km2(ring) -> float                   # 鞋带 + 纬度尺度校正（近似）
#   douglas_peucker_ring(points, *, tolerance_m=15.0, protect=()) -> tuple[int, ...]
#   simplify_ring(vertices, *, tolerance_m=15.0, protect_blocked=True) -> tuple[Vertex, ...]
#       偏离量是球面垂距；圆弧底线偏离 ≈ R·(1-cos(θ/2))，1 km/24 点时约 34 m > 15 m，
#       此时「一个点都不删」是正确行为（15 m 清的是毛刺，不是把圆压成四边形）。
#   ring_to_geojson(ring, *, properties=None) -> dict              # FR-201 Polygon
# 二分状态机（Step2）
#   BisectionMachine(bearing_deg, target_seconds, r_max_m, *, tolerance_s=30.0,
#       max_rounds=5, min_interval_m=20.0, coarse_distance_m=None, inner_seed_m=None,
#       max_blocked_probes=1, extrapolation_max_m=0.0, inner_seed_ratio=0.15,
#       blocked_floor_m=0.0)
#     .pending .status .resolved_kind .samples .blocked_outcomes .note
#     .rounds_used（含首探的提交轮次）/ .bisect_rounds（只数二分，PRD「最多 5 轮」口径）
#     .rounds_left .pending_distance_m .blocked_above_m .effective_r_max_m .evidence
#     .next_probe_m() -> float | None      # 本轮该方向要测的距离；None = 这个方向定案了
#     .submit(outcome: ProbeOutcome | None) -> None   # None/非法/方位不符 → blocked_timeout
#     .to_vertex(center) -> Vertex         # 尚未定案时 → ValueError（先把轮次跑完）
#   plan_round_probes(machines, center, *, round_index, stage="bisect", max_probes=None)
#       -> Sequence[ProbeRequest]          # 本轮「要出网」的探测点清单（服务层只管发）
# 加密策略（Step3）
#   plan_gap_probes(ring, center, target_seconds, *, config=None, round_index=1, max_new=None)
#       -> tuple[GapPlan, ...]             # .needs_probe 的才出网；其余纯内插（零配额）
#   apply_gap_plan(gap, outcome: ProbeOutcome | None, target_seconds, *, tolerance_s=30.0,
#                  round_index=0, shrink_ratio=BLOCKED_GAP_SHRINK_RATIO) -> Vertex
#       实测超容差时让**本方向的新样本**参与夹逼内插（耗时非单调，见 `_pick_bracket`）
# 耗时场（Step6）
#   pick_idw_sources(vertices, point, *, k=8, min_preferred=4) -> tuple[Vertex, ...]
#   idw_duration(point, vertices, *, k=8, p=2.0, min_preferred=4) -> float | None
#   build_duration_grid(ring, vertices, *, step_m=250.0, k=8, p=2.0, min_preferred=4)
# 编排
#   IsochroneOracle(Protocol)：.probe(center, probes) -> Sequence[ProbeOutcome | None]
#   IsochroneConfig(tolerance_s=30.0, max_rounds=5, min_interval_m=20.0, inner_seed_ratio=.15,
#       max_blocked_probes=1, extrapolation_cap_ratio=.5, coarse_distance_ratio=1.0,
#       refine=True, refine_threshold_s=60.0, refine_probes=True, max_refine_passes=2,
#       max_vertices=36, min_vertices=12, dp_tolerance_m=15.0, dp_protect_blocked=True,
#       build_duration_field=True, grid_step_m=250.0, idw_k=8, idw_power=2.0, chunk_size=50,
#       max_probes=None, blocked_no_data_radius_m=60.0, blocked_gap_shrink_ratio=0.5)
#     .machine_for(bearing, target_seconds, spec) -> BisectionMachine   # 自研驱动也用它
#     .r_max_for(spec) -> float            # 二分上界，**永不缩小**
#     .coarse_distance_for(spec) -> float  # 首探距离，可调低省配额
#     .theoretical_radius_m(spec, target_seconds) -> float | None      # 只用于阈值/文案
#   RoundProgress(stage, round_index, probes, pending_directions, total_directions,
#                 calls_total, probes_total)               # FR-205 每批一条
#   IsochroneStats(mode, target_seconds, r_max_m, theoretical_radius_m, oracle_calls,
#                  probes_total, rounds, vertex_count, kind_counts, converged_ratio,
#                  blocked_ratio) .as_dict()   # 含 quota_units_approx；两栏分开报
#   IsochroneResult(center, mode, target_seconds, ring, vertices, area_km2,
#                  duration_field, stats) .vertex_count .to_geojson()
#   compute_isochrone(center, oracle, target_seconds=900.0, *, mode="walking",
#                     config=None, progress=None) -> IsochroneResult
#     oracle 也可传 `(center, probes) -> outcomes` 可调用对象；本函数完全同步、不出网。
#     漏回的探测点由状态机判 `blocked_timeout`（绝不当成官方禁行），不会静默丢弃。
# ======================================================================================

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

__all__ = [
    "BLOCKED_GAP_SHRINK_RATIO",
    "COARSE_BEARING_COUNT",
    "COARSE_BEARING_STEP_DEG",
    "CONFIDENCE_EXHAUSTED",
    "CONFIDENCE_MEASURED_TIGHT",
    "DEFAULT_TARGET_SECONDS",
    "DIRECTION_STATUS_TO_SAMPLE_KIND",
    "DP_TOLERANCE_M",
    "EARTH_RADIUS_M",
    "FINE_BEARING_COUNT",
    "FINE_BEARING_STEP_DEG",
    "GRID_STEP_M",
    "IDW_NEIGHBOURS",
    "IDW_POWER",
    "KIND_CONFIDENCE",
    "MAX_BISECT_ROUNDS",
    "MAX_VERTICES",
    "MIN_INTERVAL_M",
    "MIN_VERTICES",
    "MODES",
    "MODE_RIDING",
    "MODE_WALKING",
    "REFINE_DURATION_GAP_S",
    "RIDING_R_MAX_M",
    "TOLERANCE_S",
    "WALKING_R_MAX_M",
    "WALKING_SPEED_KMH",
    "DirectionStatus",
    "DurationField",
    "GapPlan",
    "GridCell",
    "IsochroneConfig",
    "IsochroneOracle",
    "IsochroneResult",
    "IsochroneStats",
    "LngLat",
    "ModeSpec",
    "OutcomeKind",
    "ProbeOutcome",
    "ProbeRequest",
    "RingPoint",
    "RoundProgress",
    "SampleKind",
    "Vertex",
    "apply_gap_plan",
    "as_lnglat",
    "bearing_circle",
    "coarse_bearings",
    "compact_ring",
    "compute_isochrone",
    "cross_track_distance_m",
    "destination",
    "distance_m",
    "douglas_peucker_ring",
    "fine_bearings",
    "gap_mid_bearing",
    "get_mode_spec",
    "idw_duration",
    "initial_bearing",
    "make_probe_request",
    "mid_bearing",
    "normalize_bearing",
    "pair_key",
    "pick_idw_sources",
    "plan_gap_probes",
    "plan_round_probes",
    "point_in_ring",
    "ring_area_km2",
    "ring_is_simple",
    "ring_to_geojson",
    "simplify_ring",
    "sort_ring_by_bearing",
    "theoretical_radius_m",
]

# --------------------------------------------------------------------------------------
# 算法口径常量（PRD §4.2 逐条对应；改这里的数 = 改验收结论）
# --------------------------------------------------------------------------------------
EARTH_RADIUS_M = 6_378_137.0
"""球半径（WGS84 长半轴）。BD09LL 是经纬网，本模块按球面近似算，城市尺度误差 <1 m。"""

_M_PER_DEG_LAT = math.radians(EARTH_RADIUS_M)  # ≈ 111 319.49 m/°，面积公式用

COARSE_BEARING_COUNT = 12
COARSE_BEARING_STEP_DEG = 30.0
"""Step1 粗扫：12 个方位角，间隔 30°。"""

FINE_BEARING_COUNT = 24
FINE_BEARING_STEP_DEG = 15.0
"""Step3 加密网格：间隔 15° 共 24 个方位角（其中 12 个与粗扫重合，净新增 12 个候选）。"""

TOLERANCE_S = 30.0
MAX_BISECT_ROUNDS = 5
MIN_INTERVAL_M = 20.0
"""Step2 二分：容差 ±30 s（闭区间）、最多 5 轮、区间宽度 ≤20 m 就停止再测。"""

DP_TOLERANCE_M = 15.0
"""Step4 Douglas-Peucker 容差（偏差量用球面垂距，见 `cross_track_distance_m`）。"""

GRID_STEP_M = 250.0
IDW_NEIGHBOURS = 8
IDW_POWER = 2.0
"""Step6 耗时场：250 m 网格 + IDW（p=2，最近 8 个源点，实测点优先）。"""

REFINE_DURATION_GAP_S = 60.0
"""Step3 触发加密的相邻顶点耗时差阈值（PRD 第 244 行）。"""

BLOCKED_GAP_SHRINK_RATIO = 0.5
"""加密缝隙里遇阻的顶点，收在「两侧较近邻点半径 × 该比例」处。
绝不把顶点放在遇阻的探测点上 —— 那等于在图上宣称那个方向走得通。"""

MIN_VERTICES = 12
MAX_VERTICES = 36
"""总顶点数区间。12 个方位角**本身就是下限**：PRD §4.2 Step4 要求「按方位角升序排列以消除
自交」，`blocked` 方向也必须占位，否则多边形会在阻隔处自己收出一个假缺口。
12 + 一级 15° 加密 = 24 是**单轮加密的上限**，要撞到 PRD 写的 36 需要再叠一级 7.5° 加密
（`IsochroneConfig.max_refine_passes = 2`，第二轮零出网，纯内插）。"""

DEFAULT_TARGET_SECONDS = 900.0
"""15 分钟生活圈默认目标时长。"""

MODE_WALKING = "walking"
MODE_RIDING = "riding"

WALKING_R_MAX_M = 1250.0
RIDING_R_MAX_M = 4000.0
"""二分**上界**。步行 1.25 km 不是理论半径（理论半径见 `WALKING_SPEED_KMH`），
而是「宁大勿小」的搜索上界：上界取小了会把顺畅方向截断成假圆，比多烧几条配额更糟（PRD 269）。"""

WALKING_SPEED_KMH = 3.9
"""官方示例 `16748 m / 15491 s` 推算的步行速度（PRD 第 269 行），**只用于阈值与文案**。"""


# --------------------------------------------------------------------------------------
# 枚举与基本类型
# --------------------------------------------------------------------------------------
class SampleKind(StrEnum):
    """边界顶点的**采样方式**标签（PRD §4.2 精度取舍，五种语义两两不可合并）。"""

    MEASURED = "measured"
    INTERPOLATED = "interpolated"
    EXTRAPOLATED = "extrapolated"
    BLOCKED_OFFICIAL = "blocked_official"
    BLOCKED_TIMEOUT = "blocked_timeout"

    @property
    def is_blocked(self) -> bool:
        """「这条路走不通 / 我没测准」——两者都算阻隔，但话术不同，见 `label_cn`。"""
        return self in (SampleKind.BLOCKED_OFFICIAL, SampleKind.BLOCKED_TIMEOUT)

    @property
    def is_measured_family(self) -> bool:
        """有真实实测样本作锚点（内插/外推的两侧仍是实测）。"""
        return self in (
            SampleKind.MEASURED,
            SampleKind.INTERPOLATED,
            SampleKind.EXTRAPOLATED,
        )

    @property
    def label_cn(self) -> str:
        """给报告与 tooltip 的中文口径（别在别处再编一套）。"""
        return _KIND_LABEL_CN[self]


_KIND_LABEL_CN: dict[SampleKind, str] = {
    SampleKind.MEASURED: "实测",
    SampleKind.INTERPOLATED: "内插",
    SampleKind.EXTRAPOLATED: "外推",
    SampleKind.BLOCKED_OFFICIAL: "官方禁行",
    SampleKind.BLOCKED_TIMEOUT: "未测准",
}

# 置信度：PRD 第 271 行要求「保留置信度字段 / 不收敛降置信度」。
KIND_CONFIDENCE: dict[SampleKind, float] = {
    SampleKind.MEASURED: 0.95,
    SampleKind.INTERPOLATED: 0.70,
    SampleKind.EXTRAPOLATED: 0.45,
    SampleKind.BLOCKED_OFFICIAL: 0.80,
    SampleKind.BLOCKED_TIMEOUT: 0.30,
}

CONFIDENCE_MEASURED_TIGHT = 0.85
"""区间已收窄但从未命中 ±30 s：位置仍是真实实测点，置信度略降而不是改标签。"""

CONFIDENCE_EXHAUSTED = 0.55
"""烧完 5 轮仍不收敛 → 回退插值并降置信度。"""


class OutcomeKind(StrEnum):
    """调用方喂进来的**单次探测结论**。`BLOCKED_*` 判据由调用方给出，本模块不解释响应。"""

    OK = "ok"
    BLOCKED_OFFICIAL = "blocked_official"
    BLOCKED_TIMEOUT = "blocked_timeout"
    MISSING = "missing"
    """服务层压根没回这条（批量被截断 / 解析失败）。语义等同「没测准」，但没有证据。"""


class DirectionStatus(StrEnum):
    """单方向二分进度。与 `SampleKind` 是两套概念：状态描述收敛过程，标签描述顶点来源。"""

    PENDING = "pending"
    CONVERGED = "converged"
    FALLBACK = "fallback"
    EXTRAPOLATED = "extrapolated"
    BLOCKED_OFFICIAL = "blocked_official"
    BLOCKED_TIMEOUT = "blocked_timeout"


DIRECTION_STATUS_TO_SAMPLE_KIND: dict[DirectionStatus, SampleKind] = {
    DirectionStatus.CONVERGED: SampleKind.MEASURED,
    DirectionStatus.FALLBACK: SampleKind.INTERPOLATED,
    DirectionStatus.EXTRAPOLATED: SampleKind.EXTRAPOLATED,
    DirectionStatus.BLOCKED_OFFICIAL: SampleKind.BLOCKED_OFFICIAL,
    DirectionStatus.BLOCKED_TIMEOUT: SampleKind.BLOCKED_TIMEOUT,
}


@dataclass(frozen=True, slots=True)
class LngLat:
    """经纬度对（BD09LL，`lng` 在前）。用具名字段替代元组：顺序歧义是本项目历史坑（R8）。"""

    lng: float
    lat: float

    def __post_init__(self) -> None:
        """入口即校验：越界/NaN 的坐标不进几何，否则多边形会画到地图外（R8 同源坑）。"""
        lng = float(self.lng)
        lat = float(self.lat)
        if not (math.isfinite(lng) and math.isfinite(lat)):
            raise ValueError(f"坐标必须是有限实数，收到 lng={lng} lat={lat}")
        if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
            raise ValueError(f"坐标越界：lng={lng} lat={lat}")
        object.__setattr__(self, "lng", lng)
        object.__setattr__(self, "lat", lat)

    def as_tuple(self) -> tuple[float, float]:
        return (self.lng, self.lat)


@dataclass(frozen=True, slots=True)
class Vertex:
    """等时圈边界顶点。`kind` 是 PRD §4.2 的五种标签之一，`confidence` 必须随之下发。"""

    bearing_deg: float
    point: LngLat
    distance_m: float
    duration_s: float | None
    kind: SampleKind
    confidence: float
    round_index: int = 0
    evidence: str | None = None
    blocked_above_m: float | None = None
    traveled_m: float | None = None
    note: str | None = None

    @property
    def blocked(self) -> bool:
        return self.kind.is_blocked

    def duration_or_default(self, default: float = 0.0) -> float:
        return default if self.duration_s is None else float(self.duration_s)

    def as_dict(self) -> dict[str, object]:
        """给服务层直接进 JSON 的扁平字典（`point` 按项目约定输出 `[lng, lat]`）。"""
        data: dict[str, object] = {
            "bearing_deg": round(self.bearing_deg, 3),
            "point": [self.point.lng, self.point.lat],
            "radius_m": round(self.distance_m, 1),
            "duration_s": None if self.duration_s is None else round(self.duration_s, 1),
            "sample_kind": self.kind.value,
            "sample_label": self.kind.label_cn,
            "confidence": round(self.confidence, 3),
            "round_index": self.round_index,
            "blocked": self.blocked,
        }
        for name in ("evidence", "note"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        for name in ("blocked_above_m", "traveled_m"):
            value = getattr(self, name)
            if value is not None:
                data[name] = round(float(value), 1)
        return data


RingPoint = LngLat | Sequence[float] | Mapping[str, float] | Vertex


def as_lnglat(value: RingPoint) -> LngLat:
    """把 `LngLat` / `{\"lng\":…, \"lat\":…}` / `[lng, lat]` 归一成 `LngLat`。

    字典优先（无歧义）；序列只接受**恰好 2 个**数并按 `(lng, lat)` 解释；形状不对当场
    `ValueError`，让服务层在入口就炸掉，而不是把偏移点位带进多边形。
    """
    if isinstance(value, LngLat):
        return value
    if isinstance(value, Vertex):
        return value.point
    if isinstance(value, Mapping):
        missing = [k for k in ("lng", "lat") if k not in value]
        if missing:
            raise ValueError(f"坐标缺少字段 {missing}，需要 {{'lng':…, 'lat':…}}")
        raw_lng, raw_lat = value["lng"], value["lat"]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            raise ValueError(f"坐标序列必须是 [lng, lat] 两个数，收到 {len(value)} 个")
        raw_lng, raw_lat = value
    else:
        raise ValueError(f"无法识别的坐标类型：{type(value).__name__}")
    lng = float(cast("float", raw_lng))
    lat = float(cast("float", raw_lat))
    if not (math.isfinite(lng) and math.isfinite(lat)):
        raise ValueError("坐标必须是有限实数")
    if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
        raise ValueError(f"坐标越界：lng={lng} lat={lat}")
    return LngLat(lng, lat)


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """模式参数表：只有半径口径，没有任何接口/配额/HTTP 概念。"""

    name: str
    label: str
    r_max_m: float
    speed_kmh: float | None = None
    reference_target_seconds: float = DEFAULT_TARGET_SECONDS

    def theoretical_radius_m(self, target_seconds: float | None = None) -> float | None:
        """理论半径（阈值/文案用）。速度口径缺失时返回 `None`，**不猜数**。"""
        if self.speed_kmh is None:
            return None
        seconds = self.reference_target_seconds if target_seconds is None else target_seconds
        return theoretical_radius_m(seconds, self.speed_kmh)


MODES: dict[str, ModeSpec] = {
    MODE_WALKING: ModeSpec(
        name=MODE_WALKING,
        label="步行",
        r_max_m=WALKING_R_MAX_M,
        speed_kmh=WALKING_SPEED_KMH,
    ),
    MODE_RIDING: ModeSpec(
        name=MODE_RIDING,
        label="骑行",
        r_max_m=RIDING_R_MAX_M,
        # FR-206 只固定了 R_max=4 km；官方没有可引用的骑行速度口径，故 speed_kmh=None，
        # 由服务层显式传 `IsochroneConfig` / 阈值文案自己决定，本模块不编造数字。
        speed_kmh=None,
    ),
}


def get_mode_spec(mode: str) -> ModeSpec:
    key = (mode or MODE_WALKING).strip().lower()
    try:
        return MODES[key]
    except KeyError:
        raise ValueError(f"未知出行方式 {mode!r}，可选：{', '.join(MODES)}") from None


def theoretical_radius_m(target_seconds: float, speed_kmh: float) -> float:
    """按推算速度得到的**理论半径**，只用于评分阈值与文案，**绝不可当二分上界**。"""
    if target_seconds <= 0 or speed_kmh <= 0:
        raise ValueError("目标时长与速度都必须为正数")
    return speed_kmh * 1000.0 / 3600.0 * target_seconds


# --------------------------------------------------------------------------------------
# 方位角集合与球面正/反算
# --------------------------------------------------------------------------------------
def normalize_bearing(deg: float) -> float:
    """归一到 `[0, 360)`。"""
    value = float(deg) % 360.0
    return 0.0 if value >= 360.0 - 1e-9 else value


def bearing_circle(step_deg: float, count: int, offset_deg: float = 0.0) -> tuple[float, ...]:
    """等间隔方位角集合（升序、模 360、去重）。"""
    if count <= 0 or step_deg <= 0:
        raise ValueError("方位角步长与个数必须为正数")
    seen: set[float] = set()
    out: list[float] = []
    for i in range(count):
        value = round(normalize_bearing(offset_deg + i * step_deg), 6)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(sorted(out))


def coarse_bearings() -> tuple[float, ...]:
    """Step1 粗扫：12 个方位角，间隔 30°。"""
    return bearing_circle(COARSE_BEARING_STEP_DEG, COARSE_BEARING_COUNT)


def fine_bearings() -> tuple[float, ...]:
    """Step3 加密：间隔 15° 的 24 个方位角（含与粗扫重合的 12 个）。"""
    return bearing_circle(FINE_BEARING_STEP_DEG, FINE_BEARING_COUNT)


def mid_bearing(a_deg: float, b_deg: float) -> float:
    """两个方位角的**劣弧中点**（跨 0° 时正确回绕）。"""
    delta = ((float(b_deg) - float(a_deg)) + 180.0) % 360.0 - 180.0
    return normalize_bearing(float(a_deg) + delta / 2.0)


def gap_mid_bearing(a_deg: float, b_deg: float) -> float:
    """环上相邻顶点之间**前向缝隙**的中点方位角（`a → b` 沿顺时针走完的那段弧）。

    与 `mid_bearing`（劣弧中点）的区别只在首尾闭合那一段：12 点环里 330°→0° 的前向缝隙是
    30°，劣弧算出来同样是 345°；但稀疏环（如只有 0/120/240 三点）的 120°→0° 前向缝隙有 240°，
    劣弧中点会落到 60°——那里可能早已有顶点，缝隙就被静悄悄吞掉了。加密阶段用本函数。
    """
    span = (float(b_deg) - float(a_deg)) % 360.0
    return normalize_bearing(float(a_deg) + span / 2.0)


def destination(center: RingPoint, bearing_deg: float, distance_m_: float) -> LngLat:
    """球面正算：从 `center` 沿 `bearing_deg`（正北顺时针）走 `distance_m_` 米。

    等时圈的所有探测点都由它生成 —— 「沿直线方向推点、再由实测耗时修正半径」就是
    PRD §4.2 的「外部测时 + 内部插值」。纬度精度保留 7 位小数（≈1 cm）。
    """
    c = as_lnglat(center)
    d = float(distance_m_)
    if d < 0:
        raise ValueError("距离不能为负")
    if d == 0:
        return c
    theta = math.radians(float(bearing_deg))
    lat1 = math.radians(c.lat)
    lng1 = math.radians(c.lng)
    delta = d / EARTH_RADIUS_M  # 角距离
    sin_lat2 = math.sin(lat1) * math.cos(delta) + math.cos(lat1) * math.sin(delta) * math.cos(theta)
    # asin 在 |lat|→90° 会丢象限；极点不在项目服务范围内（境内城区），只做 clamp 保护。
    lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
    lng2 = lng1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(lat1),
        math.cos(delta) - math.sin(lat1) * sin_lat2,
    )
    return LngLat(round(math.degrees(lng2), 7), round(math.degrees(lat2), 7))


def distance_m(a: RingPoint, b: RingPoint) -> float:
    """haversine 反算两点球面距离（米）。"""
    p, q = as_lnglat(a), as_lnglat(b)
    dlat = math.radians(q.lat - p.lat)
    dlng = math.radians(q.lng - p.lng)
    sin_dlat = math.sin(dlat / 2.0)
    sin_dlng = math.sin(dlng / 2.0)
    h = (
        sin_dlat * sin_dlat
        + math.cos(math.radians(p.lat)) * math.cos(math.radians(q.lat)) * sin_dlng * sin_dlng
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing(a: RingPoint, b: RingPoint) -> float:
    """反算初始方位角（0~360，正北顺时针）。FR-203 tooltip 与几何自检用。"""
    p, q = as_lnglat(a), as_lnglat(b)
    lat1, lat2 = math.radians(p.lat), math.radians(q.lat)
    dlng = math.radians(q.lng - p.lng)
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return normalize_bearing(math.degrees(math.atan2(y, x)))


def cross_track_distance_m(a: RingPoint, b: RingPoint, p: RingPoint) -> float:
    """点 `p` 到弦 `a→b` 的**球面**垂距（米）。

    口径（PRD §4.2 Step4 要 15 m 容差，偏差量必须与它同量纲）：用球面 cross-track /
    along-track 而不是平面欧氏距离。城市尺度（≤4 km）两者差在厘米级，但**口径必须是球面**：
    纬度越高经度压缩越大，平面公式会系统性地偏袒东西向顶点、把南北向的凹凸先简化掉。
    垂足落在弦外时退化为到较近端点的距离（与平面 DP 的「点到线段」语义一致）。
    """
    pa, pb, pp = as_lnglat(a), as_lnglat(b), as_lnglat(p)
    d_ab = distance_m(pa, pb) / EARTH_RADIUS_M
    if d_ab < 1e-12:
        return distance_m(pa, pp)
    d_ap = distance_m(pa, pp) / EARTH_RADIUS_M
    delta_theta = math.radians(initial_bearing(pa, pp) - initial_bearing(pa, pb))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d_ap) * math.sin(delta_theta))))
    # 沿线距离：acos(cos(d_ap)/cos(xt))，符号由垂足在 a 的前方还是后方决定。
    ratio = max(-1.0, min(1.0, math.cos(d_ap) / max(math.cos(xt), 1e-12)))
    along = math.acos(ratio)
    if math.cos(delta_theta) < 0:
        along = -along
    if along <= 0.0:
        return distance_m(pa, pp)
    if along >= d_ab:
        return distance_m(pb, pp)
    return abs(xt) * EARTH_RADIUS_M


# --------------------------------------------------------------------------------------
# 探测请求 / 探测结论（`Vertex` 定义在上面的「枚举与基本类型」，因为它被 `RingPoint` 引用）
# --------------------------------------------------------------------------------------
def pair_key(bearing_deg: float, distance_m_: float) -> tuple[float, int]:
    """请求 ↔ 回包的配对键：方位角精到 1e-6°、距离取整米。

    服务层按同一口径配对即可（`(round(bearing, 6), round(distance))`）；本模块内部只用这一个函数
    生成键，避免「请求侧取整、回包侧不取整」这类漂移。
    """
    return (round(float(bearing_deg), 6), round(float(distance_m_)))


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """本模块请服务层测的一条「方向 + 距离」。`point` 已算好，服务层不必再做球面正算。"""

    bearing_deg: float
    distance_m: float
    point: LngLat
    round_index: int = 0
    stage: str = "bisect"  # coarse | bisect | refine

    def key(self) -> tuple[float, int]:
        """与服务层回包的配对键：方位角到 1e-6、距离取整米。"""
        return pair_key(self.bearing_deg, self.distance_m)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """服务层回给本模块的**一个**结论（与 `ProbeRequest` 按 `(bearing_deg, distance_m)` 配对）。

    - `kind`：`ok` / `blocked_official` / `blocked_timeout` / `missing`。
    - `duration_s`：`kind=ok` 时必填（秒）。
    - `evidence`：blocked 的**原始证据文本**（例如官方返回的状态码字段名与值）。本模块只搬运、
      只展示，**绝不解析** —— 解释响应是 `infra/baidu_client` 那一层的事。
    - `distance_traveled_m`：可选的真实路径长度（与直线推点距离不同）。FR-203 tooltip 想显示
      「路径距离」就用它；缺省则退回直线推点距离。
    """

    bearing_deg: float
    distance_m: float
    kind: OutcomeKind | str = OutcomeKind.OK
    duration_s: float | None = None
    distance_traveled_m: float | None = None
    evidence: str | None = None

    @property
    def outcome(self) -> OutcomeKind:
        return OutcomeKind(self.kind)

    @property
    def usable(self) -> bool:
        return self.outcome is OutcomeKind.OK and self.duration_s is not None


def _kind_confidence(kind: SampleKind, reason: str | None) -> float:
    if kind is SampleKind.MEASURED and reason == "interval_tight":
        return CONFIDENCE_MEASURED_TIGHT
    if kind is SampleKind.INTERPOLATED and reason == "rounds_exhausted":
        return CONFIDENCE_EXHAUSTED
    if kind is SampleKind.BLOCKED_TIMEOUT and reason == "missing_result":
        return 0.25
    return KIND_CONFIDENCE[kind]


# --------------------------------------------------------------------------------------
# 多边形：排序 / 收纳 / 射线法 / 自检 / 面积 / Douglas-Peucker / GeoJSON
# --------------------------------------------------------------------------------------
def _point_of(value: RingPoint) -> LngLat:
    return as_lnglat(value)


def sort_ring_by_bearing(vertices: Sequence[Vertex]) -> tuple[Vertex, ...]:
    """按方位角**升序**输出环（PRD §4.2 Step4 / 风险 R4）。

    极坐标环只要方位角单调、半径非负就不自交；乱序是本项目最容易画出自交花的 bug。
    同方位角按半径小的在前（保证射线法的边方向一致）。
    """
    return tuple(sorted(vertices, key=lambda v: (round(v.bearing_deg, 6), v.distance_m)))


def compact_ring(
    vertices: Sequence[Vertex],
    *,
    min_separation_m: float = 1.0,
    center: RingPoint | None = None,
) -> tuple[Vertex, ...]:
    """丢掉重复/过近的顶点（DP 与 `ring_is_simple` 都依赖这一点）。

    `distance_m == 0` 的顶点代表「该方向没有任何可行实测」，会全部重合到圆心 —— 只保留一个，
    并优先保留证据更强的 `blocked_official`。传入 `center` 时被丢弃顶点会归并进首个顶点的
    `note`，不丢信息。
    """
    ordered = sort_ring_by_bearing(vertices)
    kept: list[Vertex] = []
    dropped: list[Vertex] = []
    for vertex in ordered:
        if not kept:
            kept.append(vertex)
            continue
        previous = kept[-1]
        gap = (
            abs(vertex.distance_m - previous.distance_m)
            if center is None
            else distance_m(previous.point, vertex.point)
        )
        if gap >= min_separation_m:
            kept.append(vertex)
            continue
        if vertex.kind.is_blocked and not previous.kind.is_blocked:
            kept[-1] = vertex
            dropped.append(previous)
        else:
            dropped.append(vertex)
    if center is not None and dropped and kept:
        names = "、".join(sorted({f"{d.bearing_deg:.0f}°/{d.kind.value}" for d in dropped}))
        note = f"收纳 {len(dropped)} 个过近顶点：{names}"
        kept[0] = replace(kept[0], note=note if not kept[0].note else f"{kept[0].note}；{note}")
    return tuple(kept)


def point_in_ring(lnglat: RingPoint, ring: Sequence[RingPoint]) -> bool:
    """射线法（crossing number）判点在环内。**M3 的落圈判定复用这个函数，签名保持稳定**。

    - 环元素可以是 `LngLat` 或 `Vertex` 混合序列（`Vertex` 取其 `point`）。
    - 直接在经纬度空间做穿越判断：BD09LL 是经纬网，射线法只需拓扑正确性，**不需要投影**，
      因此本函数不做任何坐标系转换（也就不引入转换猜测）。
    - **边界上的点不保证**（可能在开/闭侧摆动）。M3 若要「含边界」，请外扩环或加距离容差。
    """
    p = as_lnglat(lnglat)
    n = len(ring)
    if n < 3:
        return False
    pts = [_point_of(q) for q in ring]
    inside = False
    j = n - 1
    for i in range(n):
        pi, pj = pts[i], pts[j]
        # 半开区间规则 (yi > y) != (yj > y)：避免顶点被数两次。
        if (pi.lat > p.lat) != (pj.lat > p.lat):
            x_cross = (pj.lng - pi.lng) * (p.lat - pi.lat) / (pj.lat - pi.lat) + pi.lng
            if p.lng < x_cross:
                inside = not inside
        j = i
    return inside


def ring_area_km2(ring: Sequence[RingPoint]) -> float:
    """环面积（km²）。

    近似口径（必须写清）：鞋带公式 + 局部等距投影 —— 取环的**平均纬度**做经度尺度校正，
    `x = lng · M_PER_DEG_LAT · cos(lat₀)`、`y = lat · M_PER_DEG_LAT`。生活圈尺度（≤5 km）
    纬度余弦沿环的变化 <0.1%，远小于扇形采样本身的近似误差。
    """
    pts = [_point_of(q) for q in ring]
    n = len(pts)
    if n < 3:
        return 0.0
    lat0 = sum(q.lat for q in pts) / n
    kx = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    ky = _M_PER_DEG_LAT
    total = 0.0
    for i in range(n):
        p1, p2 = pts[i], pts[(i + 1) % n]
        total += (p1.lng * kx) * (p2.lat * ky) - (p2.lng * kx) * (p1.lat * ky)
    return abs(total) / 2.0 / 1_000_000.0


def ring_is_simple(ring: Sequence[RingPoint]) -> bool:
    """不自交自检（O(n²) 两两线段相交测试 —— 风险 R4 的可验证形式）。

    相邻边共享端点、以及**完全重合的点**（退化边）都不算相交；只有真正的跨界才算。
    顶点数 ≤ 40 时这个量级完全够（单次分析，不是离线批处理）。
    """
    pts = [_point_of(q) for q in ring]
    n = len(pts)
    if n < 4:
        return True

    def orient(a: LngLat, b: LngLat, c: LngLat) -> float:
        return (b.lng - a.lng) * (c.lat - a.lat) - (b.lat - a.lat) * (c.lng - a.lng)

    for i in range(n):
        j = (i + 1) % n
        for k in range(i + 2, n):
            m = (k + 1) % n
            if m == i:  # 反向相邻：末边 (n-1)→0 与首边 0→1 共享 0 号端点
                continue
            a1, b1, a2, b2 = pts[i], pts[j], pts[k], pts[m]
            if a1 == b1 or a2 == b2:
                continue
            o1, o2 = orient(a1, b1, a2), orient(a1, b1, b2)
            o3, o4 = orient(a2, b2, a1), orient(a2, b2, b1)
            if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
                return False
    return True


def _dp_open(points: Sequence[LngLat], tolerance_m: float) -> list[int]:
    """开链 Douglas-Peucker，返回**除首点外**要保留的相对下标（升序）。"""
    n = len(points)
    if n <= 2:
        return [1] if n == 2 else []
    keep: set[int] = {n - 1}
    stack: list[tuple[int, int]] = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        worst, index = 0.0, -1
        for k in range(start + 1, end):
            dev = cross_track_distance_m(points[start], points[end], points[k])
            if dev > worst:
                worst, index = dev, k
        if worst > tolerance_m and index > 0:
            keep.add(index)
            stack.append((start, index))
            stack.append((index, end))
    return sorted(i for i in keep if i != 0)


def douglas_peucker_ring(
    points: Sequence[RingPoint],
    *,
    tolerance_m: float = DP_TOLERANCE_M,
    protect: Iterable[int] = (),
) -> tuple[int, ...]:
    """**闭合环** DP，返回保留下来的原始下标（升序，模 n）。

    做法：把环切成若干段开链，每段首尾都是「必留点」——锚点取 0 号与离它最远的点，并上调用方
    给的 `protect`（例如 blocked 顶点：把「路网阻隔」的齿简化掉，等于把阻隔伪装成顺畅边界）。
    顶点数 ≤ 3 原样返回（三角形简化就不是多边形了）。
    """
    pts = [as_lnglat(p) for p in points]
    n = len(pts)
    if n <= 3:
        return tuple(range(n))
    mandatory = {int(i) % n for i in protect}
    farthest = max(range(n), key=lambda i: distance_m(pts[0], pts[i]))
    mandatory |= {0, farthest}
    marks = sorted(mandatory)
    kept: set[int] = set(mandatory)
    for start, end in zip(marks, marks[1:] + marks[:1], strict=False):
        if end > start:
            chain = [pts[i] for i in range(start, end + 1)]
        else:  # 跨过 0 号点的那一段
            chain = [pts[i] for i in (*range(start, n), *range(0, end + 1))]
        for rel in _dp_open(chain, tolerance_m):
            kept.add((start + rel) % n)
    out: list[int] = []
    for index in sorted(kept):
        if out and distance_m(pts[out[-1]], pts[index]) <= 0.0:
            continue  # 位置重合的两点只留一个，否则 DP 会造出零长度边
        out.append(index)
    if len(out) < 3:
        return tuple(range(n))
    return tuple(out)


def simplify_ring(
    vertices: Sequence[Vertex],
    *,
    tolerance_m: float = DP_TOLERANCE_M,
    protect_blocked: bool = True,
) -> tuple[Vertex, ...]:
    """闭合环 DP 简化（Step4）。

    默认只保护 blocked 顶点：实测顶点若离弦 <15 m 就允许被抹掉（它们全部保留在
    `IsochroneResult.vertices` 里，FR-203 的 tooltip 不会丢数据），否则「12 个方向全是实测」
    的真实场景下 DP 会一步都走不动，PRD 要的「保证平滑」就落空了。

    **口径提醒**（给前端/服务层）：偏离量按球面垂距算，且比较对象是「相隔一点的两端连线」，
    所以一段圆弧天生就有 `R·(1-cos(θ/2))` 的底线偏离（θ 为相邻顶点方位角间隔）。1 km 半径、
    24 点时约 34 m > 15 m —— 此时 DP 一个点都不删是**正确行为**（真拐点删了会切掉角落、
    面积失真），15 m 容差清的是采样毛刺与抖动，不是把密网格压成四边形。
    """
    if len(vertices) <= 3:
        return tuple(vertices)
    protect = {i for i, v in enumerate(vertices) if protect_blocked and v.kind.is_blocked}
    keep = douglas_peucker_ring(
        [v.point for v in vertices], tolerance_m=tolerance_m, protect=protect
    )
    return tuple(vertices[i] for i in keep)


def ring_to_geojson(
    ring: Sequence[RingPoint], *, properties: Mapping[str, object] | None = None
) -> dict:
    """输出 GeoJSON Polygon（**FR-201 验收要求的形状**）。

    只描述几何、不做坐标转换：环里的数就是 BD09LL 的数，前端按 BD09LL 图层直接画。
    首尾自动闭合（GeoJSON 规定线性环必须闭合），前端不必再补第一点。
    """
    pts = [_point_of(q) for q in ring]
    if len(pts) < 3:
        raise ValueError("GeoJSON 线性环至少需要 3 个顶点")
    coords = [[q.lng, q.lat] for q in pts]
    coords.append([pts[0].lng, pts[0].lat])
    return {
        "type": "Feature",
        "properties": dict(properties or {}),
        "geometry": {"type": "Polygon", "coordinates": [coords]},
    }


# --------------------------------------------------------------------------------------
# 二分收敛状态机（Step2 的驱动核心；输入是调用方已经实测到的样本）
# --------------------------------------------------------------------------------------
class BisectionMachine:
    """单方向的二分收敛状态机。**纯状态推进，不做任何 I/O**。

    用法（服务层照抄）：

        machine = BisectionMachine(bearing, target_seconds, spec.r_max_m)
        while (d := machine.next_probe_m()) is not None:
            machine.submit(实测(center, bearing, d))       # 测不到也要回一条 blocked
        vertex = machine.to_vertex(center)

    收敛判据：实测 `|duration - target| <= tolerance_s`（±30 s，闭区间）→ `CONVERGED`。
    不收敛（区间宽度 ≤ `min_interval_m` 或烧完 `max_rounds`）→ 用两侧实测样本线性内插 →
    `FALLBACK`，置信度下调（PRD 第 271 行）。
    R_max 处实测仍快于目标 → 路网顺畅 → **外推一次**（不再额外花一次出网）→ `EXTRAPOLATED`。

    上界语义：`r_max_m` 是**二分上界**且永不自动缩小；`coarse_distance_m` 只调首探距离
    （省配额时可以从理论半径起测），两者分开正是 PRD 第 269 行的教训。
    """

    __slots__ = (
        "_coarse_distance_m",
        "_evidence",
        "_pending_distance_m",
        "_probed",
        "_resolved_confidence",
        "_resolved_distance_m",
        "_resolved_duration_s",
        "_resolved_kind",
        "_traveled_m",
        "bearing_deg",
        "blocked_floor_m",
        "blocked_outcomes",
        "extrapolation_max_m",
        "hi_m",
        "lo_m",
        "max_blocked_probes",
        "max_rounds",
        "min_interval_m",
        "note",
        "r_max_m",
        "rounds_used",
        "samples",
        "status",
        "target_seconds",
        "tolerance_s",
    )

    def __init__(
        self,
        bearing_deg: float,
        target_seconds: float,
        r_max_m: float,
        *,
        tolerance_s: float = TOLERANCE_S,
        max_rounds: int = MAX_BISECT_ROUNDS,
        min_interval_m: float = MIN_INTERVAL_M,
        coarse_distance_m: float | None = None,
        inner_seed_m: float | None = None,
        max_blocked_probes: int = 1,
        extrapolation_max_m: float = 0.0,
        inner_seed_ratio: float = 0.15,
        blocked_floor_m: float = 0.0,
    ) -> None:
        if target_seconds <= 0 or r_max_m <= 0:
            raise ValueError("目标时长与 R_max 都必须为正数")
        self.bearing_deg = normalize_bearing(bearing_deg)
        self.target_seconds = float(target_seconds)
        self.r_max_m = float(r_max_m)
        self.tolerance_s = float(tolerance_s)
        self.max_rounds = int(max_rounds)
        self.min_interval_m = float(min_interval_m)
        self.max_blocked_probes = int(max_blocked_probes)
        self.extrapolation_max_m = float(extrapolation_max_m)
        self.blocked_floor_m = max(0.0, float(blocked_floor_m))
        self.status: DirectionStatus = DirectionStatus.PENDING
        self.rounds_used = 0
        self.samples: list[ProbeOutcome] = []
        self.blocked_outcomes: list[ProbeOutcome] = []
        self.note: str | None = None
        self._coarse_distance_m = (
            self.r_max_m if coarse_distance_m is None else float(coarse_distance_m)
        )
        self.lo_m = (
            max(0.0, self.r_max_m * float(inner_seed_ratio))
            if inner_seed_m is None
            else float(inner_seed_m)
        )
        self.hi_m = self.r_max_m
        self._probed: set[int] = set()
        self._pending_distance_m: float | None = None
        self._resolved_distance_m: float | None = None
        self._resolved_duration_s: float | None = None
        self._resolved_kind: SampleKind = SampleKind.INTERPOLATED
        self._resolved_confidence: float = KIND_CONFIDENCE[SampleKind.INTERPOLATED]
        self._traveled_m: float | None = None
        self._evidence: str | None = None

    # -- 状态读出 -----------------------------------------------------------
    @property
    def pending(self) -> bool:
        return self.status is DirectionStatus.PENDING

    @property
    def bisect_rounds(self) -> int:
        """Step2 的**二分轮数**（首探/粗扫不计入 —— PRD §4.2「最多 5 轮」指的是二分）。"""
        return max(0, self.rounds_used - 1)

    @property
    def rounds_left(self) -> int:
        return max(0, self.max_rounds - self.bisect_rounds)

    @property
    def pending_distance_m(self) -> float | None:
        """上一次 `next_probe_m()` 请服务层测的距离（`submit` 之后清空）。"""
        return self._pending_distance_m

    @property
    def blocked_above_m(self) -> float | None:
        """最早那次「走不通」发生在多远（米）；`None` 表示该方向从未遇阻。"""
        if not self.blocked_outcomes:
            return None
        return min(float(o.distance_m) for o in self.blocked_outcomes)

    @property
    def evidence(self) -> str | None:
        return self._evidence

    @property
    def resolved_kind(self) -> SampleKind:
        return self._resolved_kind

    @property
    def effective_r_max_m(self) -> float:
        """允许顶点落到的最远距离（顺畅方向外推时才会超过 R_max）。"""
        if self.status is DirectionStatus.EXTRAPOLATED:
            return self.r_max_m + (self.extrapolation_max_m or 0.5 * self.r_max_m)
        return self.r_max_m

    def __repr__(self) -> str:  # pragma: no cover - 只为调试日志
        return (
            f"BisectionMachine(bearing={self.bearing_deg:.0f}, status={self.status.value}, "
            f"lo={self.lo_m:.0f}, hi={self.hi_m:.0f}, rounds={self.rounds_used})"
        )

    # -- 驱动 ---------------------------------------------------------------
    def next_probe_m(self) -> float | None:
        """本轮还该测哪个距离（米）。`None` = 该方向已定案 / 轮次用完 / 已无新点可测。

        决策顺序（每一条都对应一次「不白烧配额」的取舍）：

        1. 首探 = `coarse_distance_m`（默认 R_max，Step1 的 12 个探测点）；
        2. 上界还没测过、且已有样本全部「比目标快」→ 补测 R_max（顺畅方向必须先确认上界）；
        3. 已经有「比目标慢」的样本 → 在 `[lo, hi]` 中点二分；
        4. 候选距离重复/越界/区间已紧 → 就地定案（能外推就外推，否则回退插值），**不再出网**。
        """
        if not self.pending or self.bisect_rounds >= self.max_rounds:
            return None
        distance: float | None = None
        if self.rounds_used == 0:
            distance = self._coarse_distance_m
        elif not self._ceiling_probed() and not self._has_over_target_sample():
            distance = self.r_max_m
        elif self._has_over_target_sample() or self.blocked_outcomes:
            if self.hi_m - self.lo_m <= self.min_interval_m:
                self._finalize_by_state(reason="interval_tight")
                return None
            distance = (self.lo_m + self.hi_m) / 2.0
        if distance is None:
            self._finalize_by_state()
            return None
        candidate = round(distance)
        if candidate in self._probed or distance > self.hi_m or distance < self.lo_m:
            # 重复点 = 白烧配额；越界点 = 已知不可能改变结论的位置。
            self._finalize_by_state()
            return None
        self._pending_distance_m = max(1.0, float(distance))
        return self._pending_distance_m

    def _ceiling_probed(self) -> bool:
        return round(self.r_max_m) in self._probed

    def _finalize_by_state(self, reason: str | None = None) -> None:
        """跑不动了：按已有样本的形态选一种定案方式（三种都不再出网）。"""
        if self.samples and not self._has_over_target_sample():
            # 全是「比目标快」的样本 → 顺畅方向，外推一次。
            farthest = max(self.samples, key=lambda o: float(o.distance_m))
            self._finalize_extrapolated(farthest)
            return
        if self.samples:
            self._finalize_fallback(reason=reason or "no_new_probe")
            return
        if self.blocked_outcomes:
            decisive = self._decisive_block()
            self._finalize_blocked(decisive, _blocked_sample_kind(decisive))
            return
        self._finalize_fallback(reason=reason or "no_samples")

    def submit(self, outcome: ProbeOutcome | None) -> None:
        """喂进一条实测结论。`None`（服务层漏回）按「没测准」处理，**绝不当成官方禁行**。"""
        requested = self._pending_distance_m
        self._pending_distance_m = None
        if outcome is None:
            outcome = ProbeOutcome(
                bearing_deg=self.bearing_deg,
                distance_m=float(requested if requested is not None else self.r_max_m),
                kind=OutcomeKind.MISSING,
                evidence="服务层未返回该方向的探测结果",
            )
            if self.note is None:
                self.note = "missing_result"
        elif outcome.distance_m <= 0 or (
            outcome.usable and (outcome.duration_s is None or outcome.duration_s < 0)
        ):
            outcome = replace(
                outcome,
                kind=OutcomeKind.MISSING,
                duration_s=None,
                evidence=outcome.evidence or "探测结果缺少有效耗时（距离或耗时非法）",
            )
            if self.note is None:
                self.note = "missing_result"
        if not math.isclose(outcome.bearing_deg, self.bearing_deg, abs_tol=1e-4):
            # 配对错位的兜底：宁可判「没测准」，也不要把 60° 的样本记到 90° 的头上。
            outcome = replace(
                outcome,
                kind=OutcomeKind.MISSING,
                duration_s=None,
                evidence=f"回包方位 {outcome.bearing_deg:.2f}° 与请求 {self.bearing_deg:.2f}° 不符",
            )
            if self.note is None:
                self.note = "missing_result"
        self._probed.add(round(float(outcome.distance_m)))
        if outcome.usable:
            self._on_ok(outcome)
        else:
            self._on_blocked(outcome)

    # -- 内部推进 -----------------------------------------------------------
    def _has_over_target_sample(self) -> bool:
        return any(float(o.duration_s or 0.0) > self.target_seconds for o in self.samples)

    def _on_ok(self, outcome: ProbeOutcome) -> None:
        distance = float(outcome.distance_m)
        duration = float(outcome.duration_s or 0.0)
        self.rounds_used += 1
        self.samples.append(outcome)
        if distance >= self.r_max_m and duration < self.target_seconds:
            # 顺畅方向：上界处仍未到时限 → 外推一次并定案。
            self._finalize_extrapolated(outcome)
            return
        if duration > self.target_seconds:
            self.hi_m = min(self.hi_m, distance)
        else:
            self.lo_m = max(self.lo_m, min(distance, self.r_max_m))
        if abs(duration - self.target_seconds) <= self.tolerance_s:
            self._finalize_converged(outcome)
            return
        if self.hi_m - self.lo_m <= self.min_interval_m:
            self._finalize_fallback(reason="interval_tight")
            return
        if self.bisect_rounds >= self.max_rounds:
            self._finalize_fallback(reason="rounds_exhausted")

    def _on_blocked(self, outcome: ProbeOutcome) -> None:
        """阻隔处理：官方硬证据 + 区间已无可收缩 → 直接定案；否则收缩上界再试。

        为什么允许向内重试：`R_max` 处 blocked **不代表** 500 m 处走不通（河在更远的地方）。
        直接定案会把「其实能量出来的边界」误标成阻隔，比多花 1 次探测更糟。用
        `max_blocked_probes`（默认 1）把重试次数钉死，配额不会被吃掉。
        """
        self.rounds_used += 1
        self.blocked_outcomes.append(outcome)
        self._merge_evidence(outcome)
        distance = float(outcome.distance_m)
        self.hi_m = min(self.hi_m, distance)
        search_left = self.hi_m - self.lo_m > self.min_interval_m
        exhausted = (
            len(self.blocked_outcomes) > self.max_blocked_probes
            or self.bisect_rounds >= self.max_rounds
            or not search_left
        )
        if exhausted:
            decisive = self._decisive_block()
            self._finalize_blocked(decisive, _blocked_sample_kind(decisive))
            return
        # 官方硬证据落在**已确认可达的距离之内或同处** → 边界本身不可达，立刻定案。
        if outcome.outcome is OutcomeKind.BLOCKED_OFFICIAL and distance <= self.lo_m:
            self._finalize_blocked(outcome, SampleKind.BLOCKED_OFFICIAL)

    def _merge_evidence(self, outcome: ProbeOutcome) -> None:
        if outcome.evidence is None or outcome.evidence in (self._evidence or ""):
            return  # 同一条证据重复出现（定案时二次合并）不再拼接
        if self._evidence is None:
            self._evidence = outcome.evidence
            return
        # 官方证据排在前面：报告里「路走不通」不能被「没测准」淹没。
        if outcome.outcome is OutcomeKind.BLOCKED_OFFICIAL:
            self._evidence = f"{outcome.evidence}｜{self._evidence}"
        else:
            self._evidence = f"{self._evidence}｜{outcome.evidence}"

    def _decisive_block(self) -> ProbeOutcome:
        official = [o for o in self.blocked_outcomes if o.outcome is OutcomeKind.BLOCKED_OFFICIAL]
        return official[0] if official else self.blocked_outcomes[-1]

    def _finalize_converged(self, outcome: ProbeOutcome, reason: str | None = None) -> None:
        self.status = DirectionStatus.CONVERGED
        self._resolved_distance_m = float(outcome.distance_m)
        self._resolved_duration_s = float(outcome.duration_s or 0.0)
        self._traveled_m = outcome.distance_traveled_m
        self._resolved_kind = SampleKind.MEASURED
        self._resolved_confidence = _kind_confidence(SampleKind.MEASURED, reason)

    def _finalize_extrapolated(self, coarse: ProbeOutcome) -> None:
        """外推**一次**：用局部速度把剩余时间折成距离，不再多花一次出网。

        取舍：PRD 第 239 行的「外推一次」可以是「再测一次」或「算一次」。这里选后者 ——
        顺畅方向再补一枪会突破 R_max 的配额预算，而线性外推的误差本来就写进 `confidence`
        与 `extrapolated` 标签里，读者能看见。
        """
        self.status = DirectionStatus.EXTRAPOLATED
        duration = float(coarse.duration_s or 0.0)
        speed = self._outer_speed(coarse)
        extra = (self.target_seconds - duration) / speed if speed > 0 else 0.0
        cap = self.extrapolation_max_m or 0.5 * self.r_max_m
        self._resolved_distance_m = float(coarse.distance_m) + max(0.0, min(extra, cap))
        self._resolved_duration_s = self.target_seconds
        self._traveled_m = coarse.distance_traveled_m
        self._resolved_kind = SampleKind.EXTRAPOLATED
        self._resolved_confidence = KIND_CONFIDENCE[SampleKind.EXTRAPOLATED]
        if self.note is None:
            self.note = "顺畅方向：R_max 处实测耗时仍小于目标时长，边界为外推值"

    def _outer_speed(self, coarse: ProbeOutcome) -> float:
        """外推用的局部速度（米/秒）：优先最外侧两个样本的斜率，退化到全程均速。"""
        ordered = sorted(self.samples, key=lambda o: float(o.distance_m))
        if len(ordered) >= 2:
            first, second = ordered[-2], ordered[-1]
            dd = float(second.distance_m) - float(first.distance_m)
            dt = float(second.duration_s or 0.0) - float(first.duration_s or 0.0)
            if dd > 0 and dt > 0:
                return dd / dt
        span = float(coarse.distance_m)
        base = float(coarse.duration_s or 0.0)
        return span / base if span > 0 and base > 0 else 0.0

    def _finalize_blocked(self, outcome: ProbeOutcome, kind: SampleKind) -> None:
        self.status = (
            DirectionStatus.BLOCKED_OFFICIAL
            if kind is SampleKind.BLOCKED_OFFICIAL
            else DirectionStatus.BLOCKED_TIMEOUT
        )
        feasible = sorted(self.samples, key=lambda o: float(o.distance_m))
        if feasible:
            # 有可行样本：顶点停在「已知能走到的最远处」，位置比 0 有信息量。
            best = feasible[-1]
            self._resolved_distance_m = float(best.distance_m)
            self._resolved_duration_s = float(best.duration_s or 0.0)
            self._traveled_m = best.distance_traveled_m
        else:
            # 该方向没有任何可行实测：停在 `blocked_floor_m`（状态机默认 0 m = 圆心，
            # `IsochroneConfig` 默认传 60 m 以免环自碰到圆心）；标签/证据/置信度照旧，
            # 只是画图口径，不假装那里可达。
            self._resolved_distance_m = self.blocked_floor_m
            self._resolved_duration_s = None
        self._merge_evidence(outcome)
        self._resolved_kind = kind
        self._resolved_confidence = _kind_confidence(kind, self.note)

    def _finalize_fallback(self, *, reason: str | None = None) -> None:
        """不收敛 → 回退插值并降置信度（PRD 第 271 行）。

        但先做一次「区间已紧」的抢救：若最接近目标的实测样本偏离 ≤ 2×容差且区间宽度
        ≤ `min_interval_m`，位置精度已经好于容差本身，这时**保留 `measured`**、置信度降到
        `CONFIDENCE_MEASURED_TIGHT` —— 两条几乎等时的样本之间做线性内插不会更准，反而把
        一个真实测点伪装成插值点。
        """
        closest = self._closest_sample()
        if (
            closest is not None
            and reason == "interval_tight"
            and abs(float(closest.duration_s or 0.0) - self.target_seconds)
            <= 2.0 * self.tolerance_s
        ):
            self._finalize_converged(closest, reason="interval_tight")
            return
        below = [s for s in self.samples if float(s.duration_s or 0.0) < self.target_seconds]
        above = [s for s in self.samples if float(s.duration_s or 0.0) > self.target_seconds]
        if below and above:
            anchor = max(below, key=lambda s: float(s.distance_m))
            upper = min(above, key=lambda s: float(s.distance_m))
            d0, t0 = float(anchor.distance_m), float(anchor.duration_s or 0.0)
            d1, t1 = float(upper.distance_m), float(upper.duration_s or 0.0)
            ratio = 0.0 if t1 == t0 else (self.target_seconds - t0) / (t1 - t0)
            ratio = max(0.0, min(1.0, ratio))
            distance: float = d0 + (d1 - d0) * ratio
            duration: float | None = self.target_seconds
            self._traveled_m = anchor.distance_traveled_m
        elif closest is not None:
            distance = float(closest.distance_m)
            duration = float(closest.duration_s or 0.0)
            self._traveled_m = closest.distance_traveled_m
        else:
            distance = self.lo_m
            duration = None
        self.status = DirectionStatus.FALLBACK
        self._resolved_distance_m = distance
        self._resolved_duration_s = duration
        self._resolved_kind = SampleKind.INTERPOLATED
        self._resolved_confidence = _kind_confidence(SampleKind.INTERPOLATED, reason)
        if reason == "rounds_exhausted":
            self.note = "二分轮次用尽，位置由两侧实测样本内插（置信度已下调）"
        elif reason == "interval_tight":
            self.note = "区间已收窄，位置由两侧实测样本内插"

    def _closest_sample(self) -> ProbeOutcome | None:
        if not self.samples:
            return None
        return min(
            self.samples, key=lambda s: abs(float(s.duration_s or 0.0) - self.target_seconds)
        )

    # -- 出参 ---------------------------------------------------------------
    def to_vertex(self, center: RingPoint) -> Vertex:
        """定案顶点。未定案时抛 `ValueError`（服务层要先把轮次跑完）。"""
        if self.pending:
            raise ValueError(f"方位 {self.bearing_deg:.0f}° 的二分尚未定案")
        distance = max(0.0, min(float(self._resolved_distance_m or 0.0), self.effective_r_max_m))
        return Vertex(
            bearing_deg=self.bearing_deg,
            point=destination(center, self.bearing_deg, distance),
            distance_m=distance,
            duration_s=self._resolved_duration_s,
            kind=self._resolved_kind,
            confidence=self._resolved_confidence,
            round_index=self.rounds_used,
            evidence=self._evidence,
            blocked_above_m=self.blocked_above_m,
            traveled_m=self._traveled_m,
            note=self.note,
        )


def _blocked_sample_kind(outcome: ProbeOutcome | None) -> SampleKind:
    """把 blocked 结论映射到顶点标签（`MISSING` 归入「没测准」，不是官方证据）。"""
    if outcome is not None and outcome.outcome is OutcomeKind.BLOCKED_OFFICIAL:
        return SampleKind.BLOCKED_OFFICIAL
    return SampleKind.BLOCKED_TIMEOUT


def plan_round_probes(
    machines: Sequence[BisectionMachine],
    center: RingPoint,
    *,
    round_index: int,
    stage: str = "bisect",
    max_probes: int | None = None,
) -> tuple[ProbeRequest, ...]:
    """本轮还该测哪些距离。

    未收敛方向优先（它们才是烧轮次的地方），次序按方位角升序，便于对照日志。
    `max_probes` 只是**兜底闸门**；「一批 ≤ 50 条」的切分由 `compute_isochrone` 统一做，
    服务层拿到的是已切好的小块，不需要自己再判数量。
    """
    pending = sorted(
        (m for m in machines if m.pending),
        key=lambda m: (0 if m.samples or m.blocked_outcomes else 1, m.bearing_deg),
    )
    requests: list[ProbeRequest] = []
    for machine in pending:
        distance = machine.next_probe_m()
        if distance is None:
            continue
        requests.append(
            make_probe_request(
                center,
                machine.bearing_deg,
                distance,
                round_index=round_index,
                stage=stage,
            )
        )
        if max_probes is not None and len(requests) >= max_probes:
            break
    return tuple(requests)


def make_probe_request(
    center: RingPoint,
    bearing_deg: float,
    distance_m: float,
    *,
    round_index: int = 0,
    stage: str = "bisect",
) -> ProbeRequest:
    """把「方向 + 距离」变成带坐标的探测请求（服务层直接用 `point` 去算路）。"""
    bearing = normalize_bearing(bearing_deg)
    distance = max(0.0, float(distance_m))
    return ProbeRequest(
        bearing_deg=bearing,
        distance_m=distance,
        point=destination(center, bearing, distance),
        round_index=int(round_index),
        stage=stage,
    )


# --------------------------------------------------------------------------------------
# Step3 加密策略：只有相邻耗时差 > 60 s 的方向对才真去测，其余内插
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GapPlan:
    """一个「相邻顶点对」的处理计划。`probe is None` → 该缝隙只做内插，**不出网**。"""

    center: LngLat
    inner: Vertex
    outer: Vertex
    bearing_deg: float
    probe: ProbeRequest | None

    @property
    def needs_probe(self) -> bool:
        return self.probe is not None

    @property
    def duration_gap_s(self) -> float:
        return abs(self.outer.duration_or_default() - self.inner.duration_or_default())


def _gap_between(inner: Vertex, outer: Vertex) -> float | None:
    """两个相邻顶点的实测耗时差；任一侧不可信（blocked / 无耗时）则返回 `None`。

    blocked 顶点**不参与**「差 > 60 s」的比较：把「没测准」当成阻隔信号会误导加密决策，
    让它们触发一堆注定失败的探测。
    """
    if inner.kind.is_blocked or outer.kind.is_blocked:
        return None
    if inner.duration_s is None or outer.duration_s is None:
        return None
    return abs(float(outer.duration_s) - float(inner.duration_s))


def _interp_distance(inner: Vertex, outer: Vertex, target_seconds: float) -> float:
    """缝隙中点的**猜测距离**：两侧「距离-耗时」按时间线性内插（严格夹在两点之间）。"""
    d0, d1 = float(inner.distance_m), float(outer.distance_m)
    t0, t1 = inner.duration_or_default(), outer.duration_or_default()
    if t1 == t0:
        return (d0 + d1) / 2.0
    ratio = (target_seconds - t0) / (t1 - t0)
    return d0 + (d1 - d0) * max(0.0, min(1.0, ratio))


def plan_gap_probes(
    ring: Sequence[Vertex],
    center: RingPoint,
    target_seconds: float,
    *,
    config: IsochroneConfig | None = None,
    round_index: int = 1,
    max_new: int | None = None,
) -> tuple[GapPlan, ...]:
    """相邻顶点对 → 加密计划（PRD 第 244 行「耗时差 > 60 s 才加密，其余用插值」）。

    中点顶点**总是**插入（它不出网，只是让环更平滑），差别只在标签：
      * 两侧实测耗时之差 > `refine_threshold_s` → 生成 `probe`，实测后可能升为 `measured`；
      * 否则 → `probe=None`，`apply_gap_plan` 直接内插出 `interpolated` 顶点。
    预算：`max_new`（默认按 `IsochroneConfig.max_vertices` 推）限制本轮新增数，
    优先保「要出网的缝隙」，再按缝隙从大到小补内插缝隙 —— 保证总顶点数落在 12~36。
    """
    cfg = config or IsochroneConfig()
    ordered = sort_ring_by_bearing(ring)
    n = len(ordered)
    if n < 2:
        return ()
    budget = n if max_new is None else int(max_new)
    if budget <= 0:
        return ()
    c = as_lnglat(center)
    candidates: list[GapPlan] = []
    for i in range(n):
        inner, outer = ordered[i], ordered[(i + 1) % n]
        # 缝隙中点用「前向弧」而不是劣弧：首尾闭合那段（末点 → 0°）的劣弧会指回环内侧，
        # 与既有顶点撞上后被 continue 吞掉，最大的那道缝隙反而永远不加密。
        bearing = gap_mid_bearing(inner.bearing_deg, outer.bearing_deg)
        if any(abs(bearing - v.bearing_deg) < 1e-6 for v in ordered):
            continue  # 该方位角已经有顶点（例如上一轮加密过）
        gap = _gap_between(inner, outer)
        probe: ProbeRequest | None = None
        if gap is not None and gap > cfg.refine_threshold_s and cfg.refine_probes:
            probe = make_probe_request(
                c,
                bearing,
                _interp_distance(inner, outer, target_seconds),
                round_index=round_index,
                stage="refine",
            )
        candidates.append(GapPlan(c, inner, outer, bearing, probe))
    if len(candidates) <= budget:
        return tuple(candidates)
    probed = sorted((g for g in candidates if g.needs_probe), key=lambda g: g.bearing_deg)
    filler = sorted((g for g in candidates if not g.needs_probe), key=lambda g: -g.duration_gap_s)
    picked = probed[:budget] + filler[: max(0, budget - len(probed))]
    return tuple(sorted(picked, key=lambda g: g.bearing_deg))


def apply_gap_plan(
    gap: GapPlan,
    outcome: ProbeOutcome | None,
    target_seconds: float,
    *,
    tolerance_s: float = TOLERANCE_S,
    round_index: int = 0,
    shrink_ratio: float = BLOCKED_GAP_SHRINK_RATIO,
) -> Vertex:
    """把一个加密计划落成顶点。四种去向互不混淆：

    * 实测落在 ±容差内 → `measured`（加密真的钉住了边界）；
    * 实测偏差超容差 → 用这条新样本重排两侧锚点、**重新内插** → `interpolated`；
    * 实测为 blocked → 继承对应的 `blocked_official` / `blocked_timeout`（缝隙里的阻隔证据
      同样重要，不能降级成插值点）；
    * 没出网（`probe is None`）或漏回 → 纯内插 → `interpolated`。
    """
    bearing = gap.bearing_deg
    inner, outer = gap.inner, gap.outer

    def vertex(
        distance: float,
        duration: float | None,
        kind: SampleKind,
        *,
        confidence: float | None = None,
        note: str | None = None,
        point: LngLat | None = None,
        evidence: str | None = None,
        traveled: float | None = None,
        blocked_above: float | None = None,
    ) -> Vertex:
        return Vertex(
            bearing_deg=bearing,
            point=point if point is not None else destination(gap.center, bearing, distance),
            distance_m=distance,
            duration_s=duration,
            kind=kind,
            confidence=KIND_CONFIDENCE[kind] if confidence is None else confidence,
            round_index=round_index,
            evidence=evidence,
            blocked_above_m=blocked_above,
            traveled_m=traveled,
            note=note,
        )

    if gap.probe is None or outcome is None or not outcome.usable:
        if outcome is not None and not outcome.usable and gap.probe is not None:
            kind = _blocked_sample_kind(outcome)
            # 遇阻探测点上**不能**当顶点：那里已经证实走不通。收在较近邻点的一半处，
            # 并把遇阻距离留在 `blocked_above_m`，让报告能说清「多远开始就断了」。
            nearer = min(inner.distance_m, outer.distance_m)
            distance = max(0.0, nearer * shrink_ratio)
            return vertex(
                distance,
                None,
                kind,
                evidence=outcome.evidence,
                blocked_above=float(outcome.distance_m),
                note="加密探测遇阻，顶点收缩到邻点内侧",
            )
        distance = _interp_distance(inner, outer, target_seconds)
        note = (
            "未触发加密（相邻耗时差未过阈值），位置由内插"
            if gap.probe is None
            else "加密探测未返回有效结果，位置由内插"
        )
        return vertex(distance, target_seconds, SampleKind.INTERPOLATED, note=note)

    duration = float(outcome.duration_s or 0.0)
    deviation = abs(duration - target_seconds)
    if deviation <= tolerance_s:
        return vertex(
            float(outcome.distance_m),
            duration,
            SampleKind.MEASURED,
            point=gap.probe.point,
            traveled=outcome.distance_traveled_m,
        )
    # 超容差：这条新样本本身就是该方向上的实测证据，重新内插时**优先**让它当锚点；
    # 跨方向的邻点只在「新样本无法与任何样本夹住目标」时才单独使用。
    sample = _outcome_as_vertex(gap, outcome)
    low, high = _pick_bracket([inner, outer, sample], target_seconds, preferred=sample)
    distance = _interp_distance(low, high, target_seconds)
    return vertex(
        distance,
        target_seconds,
        SampleKind.INTERPOLATED,
        note=f"加密实测偏差 {deviation:.0f}s 超容差，改用该方向实测点重新内插",
    )


def _pick_bracket(
    samples: Sequence[Vertex], target_seconds: float, *, preferred: Vertex | None = None
) -> tuple[Vertex, Vertex]:
    """在若干「距离-耗时」样本里挑一对夹住目标时长的锚点，供 `_interp_distance` 内插。

    路由耗时**不随距离严格单调**（同方向 740 m 可能比 800 m 更堵），所以不能假定「远 = 慢」，
    而是枚举所有 `a.耗时 <= 目标 <= b.耗时` 的有序对：含同方向新样本（`preferred`）者优先，
    再按耗时跨度最小（内插最不外推）排序；一对都夹不住时退化为耗时最远的两端。
    """
    pairs = [
        (a, b)
        for a in samples
        for b in samples
        if a is not b
        and a.duration_or_default() <= target_seconds <= b.duration_or_default()
        and b.duration_or_default() > a.duration_or_default()
    ]
    if not pairs:
        ordered = sorted(samples, key=lambda v: v.duration_or_default())
        return ordered[0], ordered[-1]

    def rank(pair: tuple[Vertex, Vertex]) -> tuple[int, float]:
        uses_preferred = 0 if preferred is not None and preferred in pair else 1
        return (uses_preferred, pair[1].duration_or_default() - pair[0].duration_or_default())

    return min(pairs, key=rank)


def _outcome_as_vertex(gap: GapPlan, outcome: ProbeOutcome) -> Vertex:
    distance = max(0.0, float(outcome.distance_m))
    return Vertex(
        bearing_deg=gap.bearing_deg,
        point=destination(gap.center, gap.bearing_deg, distance),
        distance_m=distance,
        duration_s=float(outcome.duration_s or 0.0),
        kind=SampleKind.MEASURED,
        confidence=KIND_CONFIDENCE[SampleKind.MEASURED],
        traveled_m=outcome.distance_traveled_m,
    )


# --------------------------------------------------------------------------------------
# Step6 耗时场：IDW 反距离加权
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GridCell:
    """一个圈内网格点的估时结果。"""

    row: int
    col: int
    point: LngLat
    duration_s: float
    source_count: int
    source_kinds: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "row": self.row,
            "col": self.col,
            "point": [self.point.lng, self.point.lat],
            "duration_s": round(self.duration_s, 1),
            "source_count": self.source_count,
            "source_kinds": list(self.source_kinds),
        }


@dataclass(frozen=True, slots=True)
class DurationField:
    """圈内 250 m 网格耗时场（FR-204 热力图与盲区判定的数据源）。"""

    step_m: float
    origin: LngLat
    rows: int
    cols: int
    cells: tuple[GridCell, ...]
    k: int = IDW_NEIGHBOURS
    power: float = IDW_POWER

    def as_points(self) -> tuple[LngLat, ...]:
        return tuple(cell.point for cell in self.cells)

    def mean_duration_s(self) -> float | None:
        if not self.cells:
            return None
        return sum(cell.duration_s for cell in self.cells) / len(self.cells)

    def to_geojson(self) -> dict:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [c.point.lng, c.point.lat]},
                    "properties": {
                        "duration_s": round(c.duration_s, 1),
                        "sources": c.source_count,
                    },
                }
                for c in self.cells
            ],
        }


def pick_idw_sources(
    vertices: Sequence[Vertex],
    point: RingPoint,
    *,
    k: int = IDW_NEIGHBOURS,
    min_preferred: int = 4,
) -> tuple[Vertex, ...]:
    """网格点 `point` 的 IDW 源点集合：**实测点优先于插值点**（PRD §4.2 Step6）。

    `blocked` 顶点永不作为源点：它们没有可信耗时，把「路走不通」当成低耗时源渲染进热力图，
    是这张图最危险的误读。实测源点不足 `min_preferred` 个时才补内插/外推点（否则整个
    耗时场会因某个方向没测到而空掉）。
    """
    p = as_lnglat(point)
    measured = [v for v in vertices if v.kind is SampleKind.MEASURED and v.duration_s is not None]
    pool = list(measured)
    if len(pool) < min_preferred:
        pool += [
            v
            for v in vertices
            if v.kind in (SampleKind.INTERPOLATED, SampleKind.EXTRAPOLATED)
            and v.duration_s is not None
        ]
    pool.sort(key=lambda v: distance_m(p, v.point))
    return tuple(pool[:k])


def _idw_value(sources: Sequence[Vertex], point: LngLat, *, p: float) -> float | None:
    if not sources:
        return None
    weight_sum = 0.0
    value_sum = 0.0
    for vertex in sources:
        dist = distance_m(point, vertex.point)
        if dist < 1.0:
            # 网格点与实测顶点几乎重合：直接取实测值，避免 1/d^2 爆炸成单点主导。
            return float(vertex.duration_s or 0.0)
        weight = 1.0 / dist**p
        weight_sum += weight
        value_sum += weight * float(vertex.duration_s or 0.0)
    if weight_sum <= 0:
        return None
    return value_sum / weight_sum


def idw_duration(
    point: RingPoint,
    vertices: Sequence[Vertex],
    *,
    k: int = IDW_NEIGHBOURS,
    p: float = IDW_POWER,
    min_preferred: int = 4,
) -> float | None:
    """单点 IDW 耗时（秒）。没有可用源点时返回 `None` —— 调用方自己决定留空洞，别填 0。"""
    target = as_lnglat(point)
    sources = pick_idw_sources(vertices, target, k=k, min_preferred=min_preferred)
    return _idw_value(sources, target, p=p)


def build_duration_grid(
    ring: Sequence[RingPoint],
    vertices: Sequence[Vertex],
    *,
    step_m: float = GRID_STEP_M,
    k: int = IDW_NEIGHBOURS,
    p: float = IDW_POWER,
    min_preferred: int = 4,
) -> DurationField:
    """Step6：对**圈内** 250 m 网格点做 IDW 估时（`point_in_ring` 筛圈外点）。

    网格是「经纬度近似方格」：先用球面正算沿 0°/90° 步进得到格点，再筛。城市尺度上
    250 m 方格的经纬度近似与实际差异 <1 m，而 PRD 已声明整个方法是近似，所以不做投影
    —— 保持 BD09LL 单一口径，杜绝坐标系转换猜测。
    """
    pts = [_point_of(q) for q in ring]
    if len(pts) < 3:
        return DurationField(step_m=step_m, origin=LngLat(0.0, 0.0), rows=0, cols=0, cells=())
    min_lng, max_lng = min(q.lng for q in pts), max(q.lng for q in pts)
    min_lat, max_lat = min(q.lat for q in pts), max(q.lat for q in pts)
    mid_lat = (min_lat + max_lat) / 2.0
    kx = _M_PER_DEG_LAT * math.cos(math.radians(mid_lat))
    cols = max(1, math.ceil(abs(max_lng - min_lng) * kx / step_m) + 1)
    rows = max(1, math.ceil(abs(max_lat - min_lat) * _M_PER_DEG_LAT / step_m) + 1)
    origin = LngLat(min_lng, min_lat)
    cells: list[GridCell] = []
    for row in range(rows):
        row_point = destination(origin, 0.0, row * step_m)
        for col in range(cols):
            point = destination(row_point, 90.0, col * step_m)
            if not point_in_ring(point, pts):
                continue
            sources = pick_idw_sources(vertices, point, k=k, min_preferred=min_preferred)
            value = _idw_value(sources, point, p=p)
            if value is None:
                continue
            cells.append(
                GridCell(
                    row=row,
                    col=col,
                    point=point,
                    duration_s=value,
                    source_count=len(sources),
                    source_kinds=tuple(sorted({s.kind.value for s in sources})),
                )
            )
    return DurationField(
        step_m=step_m,
        origin=origin,
        rows=rows,
        cols=cols,
        cells=tuple(cells),
        k=k,
        power=p,
    )


# --------------------------------------------------------------------------------------
# 编排：驱动逻辑在这里，出网在 oracle 里
# --------------------------------------------------------------------------------------
@runtime_checkable
class IsochroneOracle(Protocol):
    """服务层实现的探测接口：收到「方向+距离」列表，回一批结论。

    约定（务必遵守，否则状态机会把漏掉的方向判成「没测准」）：

    * 返回顺序无所谓，但每条要能按 `(round(bearing_deg, 6), round(distance_m))` 与请求配对；
    * **测不到也要回一条** `ProbeOutcome(kind=BLOCKED_*/MISSING, ...)`，不要静默丢；
      静默丢等价于 `MISSING`（本模块会补一条），但会丢掉你手上的官方证据文本；
    * 批量切分（一批 ≤ 50 条）由 `compute_isochrone` 负责，单次收到的数量可控；
    * 允许传普通可调用对象 `(center, probes) -> outcomes`；异步的服务层请在 `probe` 里
      自己 `await` 协程 —— 本模块不碰 asyncio，也不会替你管事件循环。
    """

    def probe(
        self, center: LngLat, probes: Sequence[ProbeRequest]
    ) -> Sequence[ProbeOutcome | None]: ...


ProbeFn = Callable[[LngLat, Sequence[ProbeRequest]], Sequence[ProbeOutcome | None]]


@dataclass(frozen=True, slots=True)
class IsochroneConfig:
    """算法口径集中在这里。改一个数 = 改一份验收结论，所以每个字段都写清为什么。"""

    tolerance_s: float = TOLERANCE_S
    max_rounds: int = MAX_BISECT_ROUNDS
    min_interval_m: float = MIN_INTERVAL_M
    inner_seed_ratio: float = 0.15
    max_blocked_probes: int = 1
    extrapolation_cap_ratio: float = 0.5
    coarse_distance_ratio: float = 1.0
    refine: bool = True
    refine_threshold_s: float = REFINE_DURATION_GAP_S
    refine_probes: bool = True
    max_refine_passes: int = 2
    max_vertices: int = MAX_VERTICES
    min_vertices: int = MIN_VERTICES
    dp_tolerance_m: float = DP_TOLERANCE_M
    dp_protect_blocked: bool = True
    build_duration_field: bool = True
    grid_step_m: float = GRID_STEP_M
    idw_k: int = IDW_NEIGHBOURS
    idw_power: float = IDW_POWER
    chunk_size: int = 50
    max_probes: int | None = None
    blocked_no_data_radius_m: float = 60.0
    """方向上**一次可行实测都没有**时顶点落在多远（默认 60 m）。

    0 m 会把顶点推到圆心，环在那一点自碰（面积少一块楔形、`point_in_ring(中心)` 退化成
    边界情况，M3 的落圈判定随之不稳），所以默认留一点半径。标签、证据、`blocked_above_m`
    与置信度都不受影响 —— 这只改画图口径，不假装那里可达；要literal 的「咬到圆心的齿」就设 0。
    """

    blocked_gap_shrink_ratio: float = BLOCKED_GAP_SHRINK_RATIO
    """加密缝隙里遇阻的顶点，收缩到「两侧较近邻点半径 × 该比例」处（默认一半）。
    放在遇阻探测点上会把「走不通」画成「能走到」，方向性错误。"""

    def machine_for(
        self, bearing: float, target_seconds: float, spec: ModeSpec
    ) -> BisectionMachine:
        """按配置造一个方向的状态机（`compute_isochrone` 与自测脚本共用，避免两份口径）。"""
        return BisectionMachine(
            bearing,
            target_seconds,
            self.r_max_for(spec),
            tolerance_s=self.tolerance_s,
            max_rounds=self.max_rounds,
            min_interval_m=self.min_interval_m,
            coarse_distance_m=self.coarse_distance_for(spec),
            inner_seed_ratio=self.inner_seed_ratio,
            max_blocked_probes=self.max_blocked_probes,
            extrapolation_max_m=self.extrapolation_cap_ratio * spec.r_max_m,
            blocked_floor_m=self.blocked_no_data_radius_m,
        )

    def r_max_for(self, spec: ModeSpec) -> float:
        """二分上界：**永远不小于 R_max**（宁大勿小）。

        `coarse_distance_ratio` 只允许调**首探距离**（省配额时从理论半径起测），它不会把
        上界拉低 —— 上界缩小会把顺畅方向截成假圆，比多烧几条配额更糟（PRD 269）。
        """
        return spec.r_max_m * max(1.0, float(self.coarse_distance_ratio))

    def coarse_distance_for(self, spec: ModeSpec) -> float:
        """首探距离（Step1 的 `d`）。允许低于 R_max，但区间下界仍由状态机保证。"""
        ratio = max(0.05, min(float(self.coarse_distance_ratio), 1.0))
        return max(
            self.min_interval_m,
            min(spec.r_max_m * ratio, self.r_max_for(spec)),
        )

    def theoretical_radius_m(self, spec: ModeSpec, target_seconds: float) -> float | None:
        """阈值/文案用的理论半径。与 `r_max_for` 是两个东西，别混用。"""
        return spec.theoretical_radius_m(target_seconds)


@dataclass(frozen=True, slots=True)
class RoundProgress:
    """FR-205 进度回调的载荷（每出一批给一条）。"""

    stage: str
    round_index: int
    probes: int
    pending_directions: int
    total_directions: int
    calls_total: int
    probes_total: int


@dataclass(frozen=True, slots=True)
class IsochroneStats:
    """**两个数字必须分开报**（PRD §4.2 配额口径）：`probes_total ≈ quota_units`、
    `oracle_calls ≈ http_requests`。只报后者会把配额消耗低估一个数量级。"""

    mode: str
    target_seconds: float
    r_max_m: float
    theoretical_radius_m: float | None
    oracle_calls: int
    probes_total: int
    rounds: int
    vertex_count: int
    kind_counts: dict[str, int]
    converged_ratio: float
    blocked_ratio: float

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = asdict(self)
        data["quota_units_approx"] = self.probes_total
        return data


@dataclass(frozen=True, slots=True)
class IsochroneResult:
    """等时圈计算结果（Step1~Step6 的完整出参）。"""

    center: LngLat
    mode: str
    target_seconds: float
    ring: tuple[Vertex, ...]
    vertices: tuple[Vertex, ...]
    area_km2: float
    duration_field: DurationField | None
    stats: IsochroneStats

    @property
    def vertex_count(self) -> int:
        return len(self.ring)

    def to_geojson(self) -> dict:
        """FR-201 的 GeoJSON Polygon + FR-204 的热力点（分开两个图层，前端自己开关）。"""
        props: dict[str, object] = {
            "mode": self.mode,
            "target_seconds": self.target_seconds,
            "area_km2": round(self.area_km2, 4),
            "vertex_count": self.vertex_count,
            "coordinate_system": "bd09ll",
            "method": "fan-sampling + bisection + IDW（近似，未取路网数据）",
        }
        return ring_to_geojson(self.ring, properties=props)


def _chunks(items: Sequence[ProbeRequest], size: int) -> list[Sequence[ProbeRequest]]:
    step = max(1, int(size))
    return [items[i : i + step] for i in range(0, len(items), step)]


def _as_probe_fn(oracle: object) -> ProbeFn:
    """把 `IsochroneOracle` 对象或裸可调用对象统一成 `ProbeFn`。"""
    probe = getattr(oracle, "probe", None)
    if callable(probe):
        return cast("ProbeFn", probe)
    if callable(oracle):
        return cast("ProbeFn", oracle)
    raise ValueError(
        "oracle 必须实现 .probe(center, probes)，或本身就是 (center, probes) -> outcomes"
    )


def compute_isochrone(
    center: RingPoint,
    oracle: IsochroneOracle | ProbeFn,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    *,
    mode: str = MODE_WALKING,
    config: IsochroneConfig | None = None,
    progress: Callable[[RoundProgress], None] | None = None,
) -> IsochroneResult:
    """**服务层直接调用的编排入口**：Step1 粗扫 → Step2 二分 → Step3 自适应加密 →
    Step4 排序 + DP 简化 → Step6 IDW 耗时场。

    `oracle` 可以是实现 `IsochroneOracle` 的对象，也可以是 `(center, probes) -> outcomes`
    的可调用对象。本函数**完全同步、零出网**：所有 I/O 都在 oracle 里；二分轮次怎么推进、
    还该测哪些距离，全部由本模块决定。
    """
    cfg = config or IsochroneConfig()
    spec = get_mode_spec(mode)
    c = as_lnglat(center)
    if target_seconds <= 0:
        raise ValueError("目标时长必须为正数")
    probe = _as_probe_fn(oracle)

    calls = 0
    issued = 0
    machines: list[BisectionMachine] = []

    def run_batch(
        requests: Sequence[ProbeRequest],
        stage: str,
        round_index: int,
        total_directions: int,
    ) -> dict[tuple[float, int], ProbeOutcome]:
        nonlocal calls, issued
        if not requests:
            return {}
        issued += len(requests)
        got: dict[tuple[float, int], ProbeOutcome] = {}
        for chunk in _chunks(tuple(requests), cfg.chunk_size):
            calls += 1
            raw = probe(c, chunk)
            for outcome in raw or ():
                if outcome is None:
                    continue
                key = pair_key(outcome.bearing_deg, outcome.distance_m)
                got[key] = outcome
        return got

    def emit(stage: str, round_index: int, probes: int, total_directions: int) -> None:
        """进度回调（FR-205）：**回包配完之后**才报，所以 `pending_directions` 是本轮真实剩余。"""
        assert progress is not None
        progress(
            RoundProgress(
                stage=stage,
                round_index=round_index,
                probes=probes,
                pending_directions=sum(1 for m in machines if m.pending),
                total_directions=total_directions,
                calls_total=calls,
                probes_total=issued,
            )
        )

    def feed(
        requested: Sequence[BisectionMachine],
        got: Mapping[tuple[float, int], ProbeOutcome],
        *,
        stage: str,
        round_index: int,
    ) -> None:
        """把回包按「本轮请它测的距离」配回去；漏回的那条由状态机判成「没测准」。"""
        for machine in requested:
            if not machine.pending:
                continue
            distance = machine.pending_distance_m
            if distance is None:
                continue
            key = pair_key(machine.bearing_deg, distance)
            machine.submit(got.get(key))
        if progress is not None and requested:
            emit(stage, round_index, len(requested), len(machines))

    # Step1 + Step2：12 个方向的二分，每轮一批。
    machines = [cfg.machine_for(bearing, target_seconds, spec) for bearing in coarse_bearings()]
    first = plan_round_probes(machines, c, round_index=0, stage="coarse")
    feed(machines, run_batch(first, "coarse", 0, len(machines)), stage="coarse", round_index=0)
    rounds_used = 0
    for round_index in range(1, cfg.max_rounds + 1):
        pending = plan_round_probes(machines, c, round_index=round_index, stage="bisect")
        if not pending:
            break
        feed(
            machines,
            run_batch(pending, "bisect", round_index, len(machines)),
            stage="bisect",
            round_index=round_index,
        )
        rounds_used = round_index

    vertices: list[Vertex] = [m.to_vertex(c) for m in machines]

    # Step3：自适应加密。出网只给「相邻耗时差 > 60 s」的缝隙，其余纯内插。
    if cfg.refine:
        for pass_index in range(cfg.max_refine_passes):
            ring_now = sort_ring_by_bearing(vertices)
            budget = cfg.max_vertices - len(ring_now)
            if budget <= 0:
                break
            round_index = rounds_used + pass_index + 1
            plans = plan_gap_probes(
                ring_now,
                c,
                target_seconds,
                config=cfg,
                round_index=round_index,
                max_new=budget,
            )
            if not plans:
                break
            requests = tuple(p.probe for p in plans if p.probe is not None)
            got = run_batch(requests, "refine", round_index, len(ring_now))
            for plan in plans:
                key = None if plan.probe is None else plan.probe.key()
                outcome = None if key is None else got.get(key)
                vertices.append(
                    apply_gap_plan(
                        plan,
                        outcome,
                        target_seconds,
                        tolerance_s=cfg.tolerance_s,
                        round_index=round_index,
                        shrink_ratio=cfg.blocked_gap_shrink_ratio,
                    )
                )
            if progress is not None and requests:
                emit("refine", round_index, len(requests), len(ring_now))

    # Step4：方位角升序 → 收纳过近顶点 → DP 简化（blocked 顶点必留）。
    ordered = compact_ring(vertices, center=c)
    ring = simplify_ring(
        ordered, tolerance_m=cfg.dp_tolerance_m, protect_blocked=cfg.dp_protect_blocked
    )
    if len(ring) < 3:
        ring = ordered

    # Step6：耗时场（FR-204 / 盲区）。
    duration_field: DurationField | None = None
    if cfg.build_duration_field:
        duration_field = build_duration_grid(
            ring,
            ordered,
            step_m=cfg.grid_step_m,
            k=cfg.idw_k,
            p=cfg.idw_power,
        )

    counts: dict[str, int] = {kind.value: 0 for kind in SampleKind}
    for vertex in ring:
        counts[vertex.kind.value] += 1
    blocked = counts[SampleKind.BLOCKED_OFFICIAL.value] + counts[SampleKind.BLOCKED_TIMEOUT.value]
    stats = IsochroneStats(
        mode=spec.name,
        target_seconds=target_seconds,
        r_max_m=spec.r_max_m,
        theoretical_radius_m=cfg.theoretical_radius_m(spec, target_seconds),
        oracle_calls=calls,
        probes_total=issued,
        rounds=rounds_used,
        vertex_count=len(ring),
        kind_counts=counts,
        converged_ratio=(len(ring) - blocked) / len(ring) if ring else 0.0,
        blocked_ratio=blocked / len(ring) if ring else 0.0,
    )
    return IsochroneResult(
        center=c,
        mode=spec.name,
        target_seconds=target_seconds,
        ring=ring,
        vertices=ordered,
        area_km2=ring_area_km2(ring),
        duration_field=duration_field,
        stats=stats,
    )
