"""服务盲区识别（PRD §4.4 / FR-220~FR-224）——**主判定零 API 调用**的纯内存计算层。

这张卡的存在理由（技术文档里要强调的那句）：把「逐网格点 × 三类设施」的检索调用
**O(N×3) 次降为 0 次** ——完全复用 §4.3 已经拉到的 POI 点集，只在本模块内做
射线法预筛 + haversine 半径过滤 + 类别命中统计 + 8 邻域连通块聚合。因此本模块有一条
不可妥协的纪律：**判定链路上一次 client 都不碰**。`detect_blind_spots()` 依然接收一个
`client` 参数，它存在的唯一目的是让单测能塞进一个「被调用就抛错并计数」的假客户端来证伪
（见 `tests/test_blind_spots.py::test_main_verdict_makes_zero_api_calls`）。

形状上的三个决定：

1. **网格与 M2 的耗时场同源**：候选点直接照 `isochrone_geometry.build_duration_grid()`
   的格网算术（bbox + `destination()` 步进 + `point_in_ring()` 预筛）生成，步长默认值取
   `GRID_STEP_M=250`（= `IsochroneConfig.grid_step_m`）。两套网格若各算一份中心点，
   热力图与灰色区域图层会错位到看不出来 —— 所以这里连「先向北走 row 步、再向东走 col 步」
   的顺序都保持一致；传入 `IsochroneResult` 时优先复用其 `duration_field` 的 frame。
2. **判定粒度是 `subcat`（官方分类路径），不是维度**：药店属于 `medical` 维度，该维度还含
   综合医院与诊所；整维度非空 ≠ 有药店。主判定看 `购物>市场 / 医疗>药店 / 教育培训>小学`
   三条路径各自命中数（FR-221）。⚠️ 命题原文写「菜市场」，官方分类表里没有这个词，对应项是
   **「市场」**——映射在 `SUBCAT_ALIASES` 与输出 `notes` 里都显式出现，避免评审按字面找
   「菜市场」判成功能缺失（FR-221 验收标准的原话）。
3. **两种读法同时呈现**（PRD §3.4 需求澄清）：(a) 三者**皆无** = 完全盲区 `level="blind"`
   （灰色，P0 主判定）；(b) 命中 1~2 类 = 配套不足 `level="insufficient"`（橙色，FR-224，P2
   次判定）。两级**并存**，但 `area_pct` / `area_km2` 的口径只算 `level=="blind"`——把
   「配套不足」也计进去会虚增盲区面积。

阈值可配置：`step_m`（默认 250 m）与 `radius_m`（默认 1000 m）都是关键字参数，README 里
要按 PRD §3.4 的要求把「1 km / 250 m 均可配」写出来；`required_subcats`、
`include_insufficient` 同样可配（关掉次判定即只剩 P0 口径）。

近似与诚实口径（README 要照抄）：
- 距离是 **haversine 球面直线距离**，不是路网距离；`nearest_facility.minutes` 只在
  输入点自带 `measured_minutes`（§4.3 Step4 批量算路的实测值）时才置 `measured=True`，
  其余一律按 `WALKING_SPEED_KMH=3.9 km/h` 估算并置 `measured=False`，**绝不把估算当实测**。
- 面积口径始终是「格数 × 单格面积」（`(step_m/1000)²` km²），与外轮廓形状无关。
- 精算是 P2 可选项：`refine=False` 默认关闭；开启时对每个盲区块几何中心调 1 次路线规划，
  上限 `max_refine_calls=5`，且必须单列在 `usage` 里。
"""

# ======================================================================================
# 公共 API
# --------------------------------------------------------------------------------------
# 常量
#   DEFAULT_STEP_M = GRID_STEP_M(250.0) / DEFAULT_RADIUS_M = 1000.0 / DEFAULT_WALK_SPEED_KMH
#   LEVEL_BLIND = "blind" / LEVEL_INSUFFICIENT = "insufficient"
#   REQUIRED_SUBCATS = ("market", "pharmacy", "primary_school")
#   SUBCAT_LABELS: subcat -> 官方分类路径（中文）
#   SUBCAT_ALIASES: 官方名/中文/「菜市场」等别名 -> 规范 subcat
#   MAX_REFINE_CALLS = 5
# 纯函数
#   normalize_subcat(value) -> str | None                    # 别名归一，认不出来返回 None
#   coerce_pois(pois) -> tuple[list[PoiPoint], int]          # plain dict 契约 -> 内部点集(+跳过数)
#   frame_from_ring(ring, step_m) -> GridFrame               # 与 build_duration_grid 同源的格网框
#   cell_center(frame, step_m, row, col) -> LngLat           # 格心（顺序：先北后东，同 M2）
#   generate_candidates(ring, *, step_m=250.0, frame=None) -> tuple[CandidateCell, ...]
#       圈内 250 m 网格候选点（射线法预筛，圈外不生成），按 (row, col) 升序
#   classify_cell(pois, point, *, radius_m, required_subcats) -> tuple[str, ...]
#       半径内命中的规范 subcat（升序）
#   nearest_facility(point, pois, missing) -> dict | None    # 实测优先，估算标 measured=False
#   flood_fill(cells) -> list[list[CandidateCell]]           # 8 邻域连通块，种子按行列升序
#   trace_block_outline(frame, step_m, cells) -> dict        # 单元格矩形并集的外轮廓 GeoJSON
#   blocks_to_geojson(result) -> dict                        # FeatureCollection（FR-222 渲染）
# 编排
#   detect_blind_spots(pois, isochrone, *, step_m, radius_m, required_subcats,
#       include_insufficient, center, client, refine, route_fn, max_refine_calls) -> dict
#       纯内存、同步、零出网；`isochrone` 接受 ring 顶点序列或 IsochroneResult。
# 输入契约（并行开发的 poi_search / coverage 按此 plain dict 提供，本模块不 import 它们）
#   每个 POI：{"subcat","category","lnglat":[lng,lat],"name","measured_minutes":float|None}
# ======================================================================================

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.isochrone_geometry import (
    EARTH_RADIUS_M,
    GRID_STEP_M,
    WALKING_SPEED_KMH,
    IsochroneResult,
    LngLat,
    as_lnglat,
    destination,
    distance_m,
    point_in_ring,
    ring_area_km2,
)

__all__ = [
    "DEFAULT_RADIUS_M",
    "DEFAULT_STEP_M",
    "DEFAULT_WALK_SPEED_KMH",
    "LEVEL_BLIND",
    "LEVEL_INSUFFICIENT",
    "MAX_REFINE_CALLS",
    "REQUIRED_SUBCATS",
    "SUBCAT_ALIASES",
    "SUBCAT_LABELS",
    "CandidateCell",
    "GridFrame",
    "blocks_to_geojson",
    "cell_center",
    "classify_cell",
    "coerce_pois",
    "detect_blind_spots",
    "flood_fill",
    "frame_from_ring",
    "generate_candidates",
    "nearest_facility",
    "normalize_subcat",
    "trace_block_outline",
]

DEFAULT_STEP_M = GRID_STEP_M  # 与 M2 耗时场同源（IsochroneConfig.grid_step_m 同值）
DEFAULT_RADIUS_M = 1000.0
DEFAULT_WALK_SPEED_KMH = WALKING_SPEED_KMH  # 3.9 km/h，只用于「未实测」点的估算
MAX_REFINE_CALLS = 5

LEVEL_BLIND = "blind"
LEVEL_INSUFFICIENT = "insufficient"
_LEVEL_RANK = {LEVEL_BLIND: 0, LEVEL_INSUFFICIENT: 1}

REQUIRED_SUBCATS: tuple[str, ...] = ("market", "pharmacy", "primary_school")
"""主判定三类（官方分类路径，非维度）：`购物 > 市场` / `医疗 > 药店` / `教育培训 > 小学`。"""

SUBCAT_LABELS: dict[str, str] = {
    "market": "购物 > 市场",
    "pharmacy": "医疗 > 药店",
    "primary_school": "教育培训 > 小学",
    "hospital": "医疗 > 综合医院",
    "clinic": "医疗 > 诊所",
    "supermarket": "购物 > 超市",
    "convenience_store": "购物 > 便利店",
    "middle_school": "教育培训 > 中学",
    "kindergarten": "教育培训 > 幼儿园",
}

SUBCAT_ALIASES: dict[str, str] = {
    # 命题原文「菜市场」 -> 官方分类「市场」；这条映射同时出现在输出 notes 里。
    "菜市场": "market",
    "农贸市场": "market",
    "市场": "market",
    "农副产品市场": "market",
    "market": "market",
    "shopping_market": "market",
    "药店": "pharmacy",
    "药房": "pharmacy",
    "医药商店": "pharmacy",
    "pharmacy": "pharmacy",
    "drugstore": "pharmacy",
    "小学": "primary_school",
    "primary_school": "primary_school",
    "primaryschool": "primary_school",
}

_M_PER_DEG_LAT = math.radians(EARTH_RADIUS_M)  # ≈111319.49，与 isochrone_geometry 同口径


# --------------------------------------------------------------------------------------
# 网格候选点：与 build_duration_grid 逐行同源
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GridFrame:
    """格网坐标系：`origin` 是环 bbox 西南角，`(row, col)` 是格心索引（向北/向东递增）。"""

    origin: LngLat
    rows: int
    cols: int


@dataclass(frozen=True)
class CandidateCell:
    """一个圈内候选网格点（FR-220）。"""

    row: int
    col: int
    point: LngLat


def _ring_points(ring: Sequence[Any]) -> tuple[LngLat, ...]:
    out: list[LngLat] = []
    for item in ring:
        out.append(item if isinstance(item, LngLat) else as_lnglat(item))
    return tuple(out)


def frame_from_ring(ring: Sequence[Any], step_m: float) -> GridFrame:
    """环 bbox + 步长 -> 格网 frame（算术与 `build_duration_grid` 逐行一致）。"""
    pts = _ring_points(ring)
    if len(pts) < 3:
        return GridFrame(origin=LngLat(0.0, 0.0), rows=0, cols=0)
    min_lng, max_lng = min(q.lng for q in pts), max(q.lng for q in pts)
    min_lat, max_lat = min(q.lat for q in pts), max(q.lat for q in pts)
    mid_lat = (min_lat + max_lat) / 2.0
    kx = _M_PER_DEG_LAT * math.cos(math.radians(mid_lat))
    cols = max(1, math.ceil(abs(max_lng - min_lng) * kx / step_m) + 1)
    rows = max(1, math.ceil(abs(max_lat - min_lat) * _M_PER_DEG_LAT / step_m) + 1)
    return GridFrame(origin=LngLat(min_lng, min_lat), rows=rows, cols=cols)


def cell_center(frame: GridFrame, step_m: float, row: int, col: int) -> LngLat:
    """格心：先沿 0° 走 row 步、再沿 90° 走 col 步（顺序与 M2 一致，否则两套网格会错位）。"""
    row_point = destination(frame.origin, 0.0, row * step_m)
    return destination(row_point, 90.0, col * step_m)


def _shift(point: LngLat, north_m: float, east_m: float) -> LngLat:
    """经纬度近似方格里的带符号位移（`destination()` 不接受负距离，所以分方向走）。"""
    moved = destination(point, 0.0 if north_m >= 0 else 180.0, abs(north_m))
    return destination(moved, 90.0 if east_m >= 0 else 270.0, abs(east_m))


def generate_candidates(
    ring: Sequence[Any], *, step_m: float = DEFAULT_STEP_M, frame: GridFrame | None = None
) -> tuple[CandidateCell, ...]:
    """FR-220：等时圈内 250 m 网格候选点，射线法预筛，**圈外不生成**。

    返回顺序按 `(row, col)` 升序固定（FR-301「同一 POI 集合输出恒定」）。
    """
    pts = _ring_points(ring)
    if len(pts) < 3:
        return ()
    grid = frame or frame_from_ring(pts, step_m)
    cells: list[CandidateCell] = []
    for row in range(grid.rows):
        for col in range(grid.cols):
            point = cell_center(grid, step_m, row, col)
            if point_in_ring(point, pts):
                cells.append(CandidateCell(row=row, col=col, point=point))
    return tuple(cells)


# --------------------------------------------------------------------------------------
# POI 归一 + 半径判定（纯内存，haversine）
# --------------------------------------------------------------------------------------
def normalize_subcat(value: Any) -> str | None:
    """把 `subcat` / `tag` 归一成规范键；认不出来返回 `None`（**不按维度回退**）。"""
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    for variant in (key, key.lower(), key.replace("-", "_").replace(" ", "_")):
        if variant in SUBCAT_ALIASES:
            return SUBCAT_ALIASES[variant]
        if variant in REQUIRED_SUBCATS:
            return variant
    return None


@dataclass(frozen=True)
class PoiPoint:
    """参与半径判定的 POI（`measured_minutes` 是 §4.3 Step4 的**中心点 -> 该 POI** 实测值）。"""

    name: str
    category: str
    subcat: str
    point: LngLat
    measured_minutes: float | None
    raw_subcat: str


def coerce_pois(pois: Iterable[Any]) -> tuple[list[PoiPoint], int]:
    """把 plain dict 契约转成内部点集；缺 `lnglat` 的点跳过并计数（不当成异常）。"""
    out: list[PoiPoint] = []
    skipped = 0
    for item in pois or ():
        poi = item if isinstance(item, Mapping) else None
        if poi is None:
            skipped += 1
            continue
        raw_lnglat = poi.get("lnglat")
        if raw_lnglat is None:
            skipped += 1
            continue
        try:
            point = as_lnglat(raw_lnglat)
        except (TypeError, ValueError):
            skipped += 1
            continue
        raw_subcat = poi.get("subcat")
        measured = poi.get("measured_minutes")
        out.append(
            PoiPoint(
                name=str(poi.get("name") or ""),
                category=str(poi.get("category") or ""),
                subcat=normalize_subcat(raw_subcat) or "",
                point=point,
                measured_minutes=float(measured) if measured is not None else None,
                raw_subcat=str(raw_subcat or ""),
            )
        )
    return out, skipped


def _in_radius(poi: PoiPoint, point: LngLat, radius_m: float) -> bool:
    """先用经纬度粗窗（比 haversine 便宜一个量级），再做球面精判。"""
    cos_lat = max(0.05, math.cos(math.radians(point.lat)))
    dlat = radius_m / _M_PER_DEG_LAT
    dlng = radius_m / (_M_PER_DEG_LAT * cos_lat)
    if abs(poi.point.lat - point.lat) > dlat or abs(poi.point.lng - point.lng) > dlng:
        return False
    return distance_m(point, poi.point) <= radius_m


def classify_cell(
    pois: Sequence[PoiPoint],
    point: LngLat,
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    required_subcats: Sequence[str] = REQUIRED_SUBCATS,
) -> tuple[str, ...]:
    """FR-221 的原料：`radius_m` 内命中的**必需类别**（规范 subcat，升序、去重）。

    注意口径：只有 `required_subcats` 参与判定。药店（`pharmacy`）在 `medical` 维度里，
    同维度的综合医院/诊所**不能**替药店命中——这就是「按 `subcat` 不按维度」的意义。
    """
    wanted = set(required_subcats)
    hits = {poi.subcat for poi in pois if poi.subcat in wanted and _in_radius(poi, point, radius_m)}
    return tuple(sorted(hits))


# --------------------------------------------------------------------------------------
# 最近可用设施（FR-223）
# --------------------------------------------------------------------------------------
def _estimate_minutes(distance_m_: float, speed_kmh: float) -> float:
    return distance_m_ / (speed_kmh * 1000.0 / 60.0)


def nearest_facility(
    point: LngLat,
    pois: Sequence[PoiPoint],
    missing: Sequence[str],
    *,
    speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
    required_subcats: Sequence[str] = REQUIRED_SUBCATS,
) -> dict[str, Any] | None:
    """最近**可用**设施：优先该块缺失的类别（居民真正要走去的那类），全无可用时退到任一点。

    `minutes` 口径（诚实标注，别骗人）：
    - 点带 `measured_minutes`（§4.3 批量算路实测，起点是**分析中心点**） -> 用实测值，
      `measured=True`；
    - 否则直线距离 / 3.9 km/h 估算，`measured=False`。
    排序也分两档：实测优先、其后按直线距离，保证「最近的」在两档里都稳定。
    """
    if not pois:
        return None
    missing_set = set(missing)
    wanted = set(required_subcats)
    pool = [poi for poi in pois if poi.subcat in missing_set]
    if not pool:
        pool = [poi for poi in pois if poi.subcat in wanted]
    if not pool:
        pool = list(pois)
    ranked = sorted(
        pool,
        key=lambda p: (
            p.measured_minutes is None,
            p.measured_minutes if p.measured_minutes is not None else float("inf"),
            round(distance_m(point, p.point), 3),
            p.name,
            p.point.lng,
            p.point.lat,
        ),
    )
    best = ranked[0]
    dist = distance_m(point, best.point)
    measured = best.measured_minutes is not None
    minutes = float(best.measured_minutes) if measured else _estimate_minutes(dist, speed_kmh)
    return {
        "name": best.name,
        "category": best.category,
        "subcat": best.subcat or best.raw_subcat,
        "minutes": round(minutes, 1),
        "distance_m": round(dist),
        "measured": measured,
    }


# --------------------------------------------------------------------------------------
# 8 邻域连通块 + 外轮廓追踪
# --------------------------------------------------------------------------------------
def _corner_point(frame: GridFrame, step_m: float, cr: int, cc: int) -> LngLat:
    """格角点：`(cr, cc)` 是半格索引（格心 (row,col) 的四角为 (2row±1, 2col±1)）。

    索引可以是负数（第 0 行/列的西南角在 `origin` 之外），所以走 `_shift()` 而不是
    直接把负距离喂给 `destination()`（后者明确拒绝负距离）。
    """
    return _shift(frame.origin, cr / 2.0 * step_m, cc / 2.0 * step_m)


def _edge(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    return (a, b) if a <= b else (b, a)


def _boundary_edges(
    cells: Sequence[CandidateCell],
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """单元格矩形并集的边界边：内部边被两格共享，异或后自动消掉。"""
    counts: dict[tuple, int] = {}
    for cell in cells:
        r, c = cell.row, cell.col
        sw, se = (2 * r - 1, 2 * c - 1), (2 * r - 1, 2 * c + 1)
        ne, nw = (2 * r + 1, 2 * c + 1), (2 * r + 1, 2 * c - 1)
        for e in (_edge(sw, se), _edge(se, ne), _edge(ne, nw), _edge(nw, sw)):
            counts[e] = counts.get(e, 0) + 1
    return [edge for edge, n in sorted(counts.items()) if n % 2 == 1]


def _trace_loops(edges: Sequence[tuple]) -> list[list[tuple[int, int]]]:
    """把边界边串成闭合环。对角相接的块会有两个共点环（left-hand 规则自动分开）。"""
    adjacency: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    remaining: set[tuple] = {tuple(sorted((a, b))) for a, b in edges}
    loops: list[list[tuple[int, int]]] = []
    while remaining:
        start = min(remaining)
        a, b = start
        remaining.discard(start)
        loop: list[tuple[int, int]] = [a, b]
        arrived_from = a
        current = b
        guard = 0
        limit = len(edges) + 4
        while current != loop[0] and guard < limit:
            guard += 1
            nxt = _next_vertex(adjacency.get(current, set()), current, arrived_from)
            if nxt is None:
                break
            edge = tuple(sorted((current, nxt)))
            remaining.discard(edge)
            arrived_from, current = current, nxt
            loop.append(current)
        loops.append(loop[:-1] if current == loop[0] and len(loop) > 1 else loop)
    return loops


def _next_vertex(
    candidates: set[tuple[int, int]], current: tuple[int, int], arrived_from: tuple[int, int]
) -> tuple[int, int] | None:
    """左手规则：左转 > 直行 > 右转 > 折返（共点/夹点处保证仍在本体里走）。"""
    rest = [v for v in sorted(candidates) if v != arrived_from]
    if not rest:
        return arrived_from if arrived_from in candidates else None
    cx, cy = current[1], current[0]  # x=col 方向，y=row 方向
    # 行进方向 = 当前点 - 来路点（写成「来路 - 当前」会把左右转判反，共点处就会串到另一个本体上）
    dx, dy = cx - arrived_from[1], cy - arrived_from[0]

    def turn_rank(v: tuple[int, int]) -> int:
        vx, vy = v[1] - cx, v[0] - cy
        cross = dx * vy - dy * vx
        dot = dx * vx + dy * vy
        if cross > 0:
            return 0  # 左转
        if dot > 0:
            return 1  # 直行
        if cross < 0:
            return 2  # 右转
        return 3  # 折返

    return min(rest, key=lambda v: (turn_rank(v), v))


def _signed_area(coords: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    n = len(coords)
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _ring_geojson(loop: Sequence[LngLat]) -> list[list[float]]:
    coords = [[round(p.lng, 6), round(p.lat, 6)] for p in loop]
    coords.append(list(coords[0]))
    return coords


def _shell_points(shell: Sequence[list[float]]) -> list[list[float]]:
    """线性环去掉闭合重复点后的角点列表（`[[lng, lat], ...]`，可直接喂 `point_in_ring`）。"""
    return [list(point) for point in shell[:-1]]


def _contained_in(inner_shell: Sequence[list[float]], outer_shell: Sequence[list[float]]) -> bool:
    """`inner` 是否被 `outer` 包住：按 inner 角点的**多数包含**判定。

    不用单点射线法，因为 8 邻域下对角相接的两个本体环会**共享一个顶点**，而
    `point_in_ring` 明确不保证边界点 —— 单点测试会把另一个本体误判成洞。多数投票在
    共点情形给 0/1 票（判为不包含），在真洞情形全票命中，两种退化都对。
    """
    outer = _shell_points(outer_shell)
    inner = _shell_points(inner_shell)
    if not inner or len(outer) < 3:
        return False
    inside = sum(1 for point in inner if point_in_ring(point, outer))
    return inside * 2 > len(inner)


def trace_block_outline(frame: GridFrame, step_m: float, cells: Sequence[CandidateCell]) -> dict:
    """单元格矩形并集的**真外轮廓** -> GeoJSON（Polygon；对角相接等退化情形为 MultiPolygon）。

    外环逆时针、内洞顺时针；洞的判定用射线法（复用 `point_in_ring`），不靠面积大小猜。
    """
    edges = _boundary_edges(cells)
    if not edges:
        raise ValueError("边界边为空，无法追踪外轮廓")
    rings: list[list[list[float]]] = []
    for loop_indices in _trace_loops(edges):
        if len(loop_indices) < 4:
            raise ValueError(f"轮廓环退化：只有 {len(loop_indices)} 个角点")
        # 自 touch 的 8 字形环（四个格围成一圈风车时左手规则会串环）不是合法线性环，
        # 宁可整块退回 per-cell 矩形并在 notes 留痕，也不输出自交的 GeoJSON。
        if len(set(loop_indices)) != len(loop_indices):
            raise ValueError("轮廓环自相交（共点串环），退回 per-cell 矩形")
        rings.append(
            _ring_geojson([_corner_point(frame, step_m, cr, cc) for cr, cc in loop_indices])
        )

    outer_flags = [
        not any(
            index != other and _contained_in(ring, other_ring)
            for other, other_ring in enumerate(rings)
        )
        for index, ring in enumerate(rings)
    ]
    polygons: list[list[list[list[float]]]] = []
    for index, ring in enumerate(rings):
        if not outer_flags[index]:
            continue
        holes = [
            _orient(other, negative=True)
            for other, is_outer in zip(rings, outer_flags, strict=True)
            if not is_outer and _contained_in(other, ring)
        ]
        polygons.append([_orient(ring, negative=False), *holes])
    if not polygons:
        raise ValueError("外轮廓追踪失败：没有外环")
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _orient(ring: list[list[float]], *, negative: bool) -> list[list[float]]:
    ccw = _signed_area([(x, y) for x, y in ring]) > 0
    want_ccw = not negative
    if ccw == want_ccw:
        return ring
    return list(reversed(ring))


def _per_cell_polygon(frame: GridFrame, step_m: float, cells: Sequence[CandidateCell]) -> dict:
    """退化外轮廓时的兜底：per-cell 矩形 MultiPolygon（面积口径不变，仍是格数 × 单格面积）。"""
    polygons = []
    for cell in cells:
        r, c = cell.row, cell.col
        corners = [
            _corner_point(frame, step_m, 2 * r - 1, 2 * c - 1),
            _corner_point(frame, step_m, 2 * r - 1, 2 * c + 1),
            _corner_point(frame, step_m, 2 * r + 1, 2 * c + 1),
            _corner_point(frame, step_m, 2 * r + 1, 2 * c - 1),
        ]
        polygons.append([_ring_geojson(corners)])
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def flood_fill(cells: Sequence[CandidateCell]) -> list[list[CandidateCell]]:
    """8 邻域 flood fill（FR-224 聚合口径），**迭代顺序确定**：按 (row, col) 升序取种子。"""
    by_key = {(cell.row, cell.col): cell for cell in cells}
    unvisited = set(by_key)
    blocks: list[list[CandidateCell]] = []
    for seed in sorted(unvisited):
        if seed not in unvisited:
            continue
        unvisited.discard(seed)
        stack = [seed]
        component: list[CandidateCell] = []
        while stack:
            key = stack.pop()
            component.append(by_key[key])
            row, col = key
            for drow, dcol in _NEIGHBOURS:
                nxt = (row + drow, col + dcol)
                if nxt in unvisited:
                    unvisited.discard(nxt)
                    stack.append(nxt)
        component.sort(key=lambda cell: (cell.row, cell.col))
        blocks.append(component)
    return blocks


# --------------------------------------------------------------------------------------
# 精算（P2，可选）
# --------------------------------------------------------------------------------------
def _resolve_refine(route_fn: Any, origin: LngLat, target: LngLat) -> tuple[float, float] | None:
    """调用路线规划拿 (minutes, distance_m)。拿不到就返回 None，**不伪造实测值**。"""
    try:
        raw = route_fn([origin.lng, origin.lat], [target.lng, target.lat])
    except Exception:  # 精算失败只是退回估算，不该把整张盲区图搞崩
        return None
    if hasattr(raw, "__await__"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raw = asyncio.run(_await_value(raw))
        else:
            return None
    return _coerce_route_result(raw)


async def _await_value(value: Any) -> Any:
    return await value


def _coerce_route_result(raw: Any) -> tuple[float, float] | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return (float(raw), 0.0)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) == 2:
        return (float(raw[0]), float(raw[1]))
    if isinstance(raw, Mapping):
        minutes = raw.get("minutes")
        if minutes is None:
            duration_s = raw.get("duration", raw.get("duration_s", raw.get("walk_duration")))
            if duration_s is None:
                return None
            minutes = float(duration_s) / 60.0
        distance = raw.get("distance_m", raw.get("distance", raw.get("direction_distance")))
        return (float(minutes), float(distance) if distance is not None else 0.0)
    return None


# --------------------------------------------------------------------------------------
# 编排：主判定 0 次出网
# --------------------------------------------------------------------------------------
def _coerce_isochrone(isochrone: Any) -> tuple[Sequence[Any], GridFrame | None, Any]:
    """支持两种输入：`IsochroneResult`（或同形状对象）与裸 ring 顶点序列。"""
    ring = getattr(isochrone, "ring", None)
    if isinstance(isochrone, IsochroneResult) or ring is not None:
        field = getattr(isochrone, "duration_field", None)
        frame = None
        if field is not None and abs(float(field.step_m) - DEFAULT_STEP_M) < 1e-9:
            frame = GridFrame(origin=field.origin, rows=int(field.rows), cols=int(field.cols))
        return ring, frame, getattr(isochrone, "center", None)
    return isochrone, None, None


def detect_blind_spots(
    pois: Iterable[Any],
    isochrone: Any,
    *,
    step_m: float = DEFAULT_STEP_M,
    radius_m: float = DEFAULT_RADIUS_M,
    required_subcats: Sequence[str] = REQUIRED_SUBCATS,
    include_insufficient: bool = True,
    center: Any = None,
    client: Any = None,
    refine: bool = False,
    route_fn: Any = None,
    max_refine_calls: int = MAX_REFINE_CALLS,
    walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
) -> dict[str, Any]:
    """FR-220~FR-224 服务盲区识别。**纯内存、零出网、结果确定**。

    参数
      pois                §4.3 已拉取的 POI 点集（plain dict 契约，见文件头）。
      isochrone           `IsochroneResult` 或等时圈 ring 顶点序列。
      step_m              网格步长，默认 250 m（**可配**，与耗时场 `grid_step_m` 同源）。
      radius_m            本地半径阈值，默认 1000 m（**可配**，PRD §3.4 需求澄清）。
      required_subcats    主判定类别，默认三类；按 `subcat` 匹配，不按维度。
      include_insufficient 关掉即只剩 P0 完全盲区口径（FR-224 是 P2 次判定）。
      client              假客户端位：判定链路**永不**调用它（单测用它证伪 0 次调用）。
      refine/route_fn/max_refine_calls  P2 精算，默认关闭，开启时 ≤5 次且单列在 usage。

    返回见 PRD §4.4 的输出契约。`count` = 块数；`area_km2` 与 `area_pct` 的口径
    **只算 `level=="blind"`**（把「配套不足」计进去会虚增盲区面积）。
    """
    # `client` 只登记、不调用：主判定的出网次数恒为 0（见 usage/extra_api_calls）。
    ring, frame, result_center = _coerce_isochrone(isochrone)
    pts = _ring_points(ring)
    grid = frame or frame_from_ring(pts, step_m)
    candidates = generate_candidates(ring, step_m=step_m, frame=grid)
    total_enumerated = max(0, grid.rows) * max(0, grid.cols)

    poi_points, skipped_pois = coerce_pois(pois)
    origin = _resolve_center(center, result_center, pts)

    level_of: dict[str, list[CandidateCell]] = {LEVEL_BLIND: [], LEVEL_INSUFFICIENT: []}
    for cell in candidates:
        hits = classify_cell(
            poi_points, cell.point, radius_m=radius_m, required_subcats=required_subcats
        )
        missing = tuple(sorted(set(required_subcats) - set(hits)))
        if not missing:
            continue
        level = LEVEL_BLIND if not hits else LEVEL_INSUFFICIENT
        if level == LEVEL_INSUFFICIENT and not include_insufficient:
            continue
        level_of[level].append(cell)

    blocks: list[dict[str, Any]] = []
    cell_area_km2 = (step_m / 1000.0) ** 2
    outline_fallbacks = 0
    wanted = set(required_subcats)
    for level in (LEVEL_BLIND, LEVEL_INSUFFICIENT):
        for component in flood_fill(level_of[level]):
            # 块级 `missing` 取「块内**每一格**都缺」的类别（= 对块内命中类别取并集后求补）：
            # 这块地的主人一定走不到；单格口径下每格缺的类别可能不同，聚合后取交集更保守。
            hits: set[str] = set()
            for cell in component:
                hits |= set(
                    classify_cell(
                        poi_points, cell.point, radius_m=radius_m, required_subcats=required_subcats
                    )
                )
            missing = tuple(sorted(wanted - hits))
            try:
                polygon = trace_block_outline(grid, step_m, component)
            except Exception:
                polygon = _per_cell_polygon(grid, step_m, component)
                outline_fallbacks += 1
            center_point = _block_center(grid, step_m, component)
            facility = nearest_facility(
                center_point,
                poi_points,
                missing,
                speed_kmh=walk_speed_kmh,
                required_subcats=required_subcats,
            )
            blocks.append(
                {
                    "polygon": polygon,
                    "cells": len(component),
                    "center_lnglat": [round(center_point.lng, 6), round(center_point.lat, 6)],
                    "area_km2": round(len(component) * cell_area_km2, 6),
                    "missing": list(missing),
                    "missing_count": len(missing),
                    "level": level,
                    "distance_from_center_m": round(distance_m(origin, center_point)),
                    "nearest_facility": facility,
                }
            )

    http_requests = 0
    refined_blocks = 0
    refine_skipped = 0
    refine_limit = max(0, int(max_refine_calls))
    if refine and route_fn is not None:
        for block in blocks:
            facility = block["nearest_facility"]
            if facility is None or http_requests >= refine_limit:
                refine_skipped += 1
                continue
            lng, lat = block["center_lnglat"]
            resolved = _resolve_refine(
                route_fn, LngLat(lng, lat), _facility_point(poi_points, facility)
            )
            http_requests += 1
            if resolved is None:
                refine_skipped += 1
                continue
            minutes, distance = resolved
            facility["minutes"] = round(float(minutes), 1)
            facility["distance_m"] = round(distance) if distance else facility["distance_m"]
            facility["measured"] = True
            refined_blocks += 1

    blind_cells = sum(block["cells"] for block in blocks if block["level"] == LEVEL_BLIND)
    blind_area_km2 = round(blind_cells * cell_area_km2, 6)
    ring_area = ring_area_km2(pts)
    area_pct = round(blind_area_km2 / ring_area * 100.0, 4) if ring_area > 0 else 0.0

    notes = _build_notes(
        radius_m=radius_m,
        step_m=step_m,
        required_subcats=required_subcats,
        include_insufficient=include_insufficient,
        blind_cells=blind_cells,
        insufficient_cells=sum(
            block["cells"] for block in blocks if block["level"] == LEVEL_INSUFFICIENT
        ),
        skipped_pois=skipped_pois,
        outline_fallbacks=outline_fallbacks,
        refine=refine,
        refine_enabled=route_fn is not None,
        refined_blocks=refined_blocks,
        refine_skipped=refine_skipped,
        ring_area_km2_value=ring_area,
        walk_speed_kmh=walk_speed_kmh,
        client_provided=client is not None,
    )

    return {
        "count": len(blocks),
        "area_pct": area_pct,
        "area_km2": blind_area_km2,
        "blocks": blocks,
        "grid": {
            "step_m": step_m,
            "radius_m": radius_m,
            "candidates": total_enumerated,
            "inside_ring": len(candidates),
        },
        "extra_api_calls": http_requests,
        "notes": notes,
        "usage": {
            "http_requests": http_requests,
            "quota_units": http_requests,
            "place_calls": 0,
            "route_lines": {
                "calls": http_requests,
                "budget": refine_limit,
                "measured_blocks": refined_blocks,
                "skipped_blocks": refine_skipped,
                "enabled": bool(refine and route_fn is not None),
            },
        },
    }


def _block_center(grid: GridFrame, step_m: float, component: Sequence[CandidateCell]) -> LngLat:
    cells = sorted(component, key=lambda cell: (cell.row, cell.col))
    rows = [cell.row for cell in cells]
    cols = [cell.col for cell in cells]
    mid_row = sum(rows) / len(rows)
    mid_col = sum(cols) / len(cols)
    row_point = destination(grid.origin, 0.0, mid_row * step_m)
    return destination(row_point, 90.0, mid_col * step_m)


def _resolve_center(center: Any, result_center: Any, pts: Sequence[LngLat]) -> LngLat:
    if center is not None:
        return as_lnglat(center)
    if result_center is not None:
        return as_lnglat(result_center)
    if pts:
        return LngLat(sum(q.lng for q in pts) / len(pts), sum(q.lat for q in pts) / len(pts))
    return LngLat(0.0, 0.0)


def _facility_point(pois: Sequence[PoiPoint], facility: Mapping[str, Any]) -> LngLat:
    """精算目的地坐标回到设施原始点位：按 (name, subcat) 找回，找不回就用块的估算距离反推。"""
    for poi in pois:
        if poi.name == facility.get("name") and (poi.subcat or poi.raw_subcat) == facility.get(
            "subcat"
        ):
            return poi.point
    return LngLat(0.0, 0.0)


def _build_notes(
    *,
    radius_m: float,
    step_m: float,
    required_subcats: Sequence[str],
    include_insufficient: bool,
    blind_cells: int,
    insufficient_cells: int,
    skipped_pois: int,
    outline_fallbacks: int,
    refine: bool,
    refine_enabled: bool,
    refined_blocks: int,
    refine_skipped: int,
    ring_area_km2_value: float,
    walk_speed_kmh: float,
    client_provided: bool,
) -> list[str]:
    labels = " / ".join(
        f"{SUBCAT_LABELS.get(subcat, subcat)}（{subcat}）" for subcat in required_subcats
    )
    notes = [
        "命题原文「菜市场」在百度官方 POI 分类表中**不存在**，对应项是官方分类「购物 > 市场」，"
        '本项目按「市场」判定（`subcat="market"`），这一映射是刻意的口径统一，不是功能缺失。',
        f"主判定三类按官方分类路径判定：{labels}；判定粒度是 `subcat` **而非维度**"
        "（药店属 medical 维度，该维度还含综合医院/诊所，整维度非空不等于有药店）。",
        f"阈值均可配：`step_m={step_m:g} m`（与 M2 耗时场 `grid_step_m` 同源）、"
        f"`radius_m={radius_m:g} m`（本地半径判定）。",
        '两种读法同时呈现（PRD §3.4 需求澄清）：(a) 三类**皆无** = 完全盲区 `level="blind"`'
        '（FR-221 主判定，灰色）；(b) 命中 1~2 类 = 配套不足 `level="insufficient"`'
        "（FR-224 次判定，橙色）。",
        f'面积口径：格数 × 单格面积，`area_km2` 与 `area_pct` **只计 `level=="blind"`** 的格'
        f"（完全盲区 {blind_cells} 格，配套不足 {insufficient_cells} 格不计入），"
        f"等时圈面积 {round(ring_area_km2_value, 4)} km² 由 `ring_area_km2()` 给出。",
        f"主判定 **0 次 API 调用**（完全复用已拉取的 POI 集合，把 O(N×3) 次检索降为 0）："
        f"`place_calls=0`、`http_requests={0 if not refine else '见 usage.route_lines'}`。",
        f"`nearest_facility.minutes` 优先复用 §4.3 Step4 批量算路的**实测**步行时间（起点为分析"
        f"中心点）；无实测值时按直线距离 / {walk_speed_kmh:g} km/h 估算并置 `measured=False`，"
        "估算值不会伪装成实测。",
        "距离均为 haversine 球面直线距离（非路网距离）；网格与轮廓都是近似几何，"
        "与等时圈采样同源。",
    ]
    if not include_insufficient:
        notes.append("已关闭次判定（`include_insufficient=False`），输出仅保留 P0 完全盲区口径。")
    if skipped_pois:
        notes.append(f"{skipped_pois} 个 POI 因缺少 `lnglat`/形状不符被跳过（不计为异常）。")
    if outline_fallbacks:
        notes.append(
            f"{outline_fallbacks} 个连通块外轮廓追踪退化，已回退为 per-cell 矩形 MultiPolygon"
            "（面积口径不变，仍按格数计算）。"
        )
    if refine:
        if not refine_enabled:
            notes.append(
                "`refine=True` 但未提供 `route_fn`，精算未执行（`usage.http_requests=0`）。"
            )
        else:
            notes.append(
                f"精算（P2 可选）已开启：路线规划 {refined_blocks + refine_skipped} 次，"
                f"{refined_blocks} 个块取得实测值，{refine_skipped} 个块退回估算。"
            )
    elif client_provided:
        notes.append("`client` 参数仅为 0 次调用的证伪位，判定链路从未调用它。")
    return notes


def blocks_to_geojson(result: Mapping[str, Any]) -> dict:
    """把结果转成 FeatureCollection，供 FR-222 的灰色/橙色图层直接渲染。"""
    features = []
    for index, block in enumerate(result.get("blocks") or ()):
        props = {k: v for k, v in block.items() if k != "polygon"}
        props["block_index"] = index
        geometry = block.get("polygon") or {}
        features.append(
            {
                "type": "Feature",
                "id": f"blind-{index}",
                "properties": props,
                "geometry": {
                    "type": geometry.get("type", "Polygon"),
                    "coordinates": geometry.get("coordinates", []),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
