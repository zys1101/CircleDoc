"""services/coverage.py —— M3「圈内覆盖统计」：落圈判定 + 前 K 实测 + 单维度得分。

对应 PRD §4.3 第 3、4 条、§4.5 的评分函数与 §4.7 响应契约的 `coverage` 段。

数据来源是 POI 检索层（`services/poi_search.py`，**另一条泳道、本层不 import**）产出的
plain-dict 集合。本层按下面的「plain-dict 契约」消费 `Mapping`，只读不改（`_copy_poi` 出副本），
所以两条泳道可以独立联调，也不会把中间态写回上游对象。

四条硬约束决定了本文件的形状：

1. 🔴 **落圈判定零 API 调用**。点在多边形内用几何层的射线法 `point_in_ring`（签名稳定，
   几何模块的公共 API 块里明确写了「M3 落圈判定复用这个函数」），纯本地 CPU。
   因此本层的出网**只有**「前 K 实测」这一件事，且必须是 **1 次批量算路**
   （`origins=[中心点]`、`destinations=K 个 POI`）—— K 次路线规划是要被消灭的错误形态
   （PRD §4.3 第 4 条）。K 上限 50 来自官方 `origins × destinations ≤ 50`。
2. 🔴 **`count_in` / `count_near` / `count_total` 三个数口径不同，前端与报告必须分开显示**：
   * `count_in` —— 本地射线法判定「落在等时圈多边形内」的**返回页** POI 数。明细表只取第一页
     （`page_size=10`、一律不翻页，PRD §4.3 第 5 条），所以 `count_in ≤ len(pois)`，
     达标阈值 ≥ 5 的维度（餐饮）**会被页数截断而系统性低估**，这是已知取舍、不是 bug。
   * `count_near` —— 「半径内但圈外」= `len(pois) - count_in`（坐标不可解析的按圈外计并留痕）。
     于是 `count_in + count_near == len(pois)` 恒成立，可自洽校验。
   * `count_total` —— **官方返回体的 `total`**（上限 150，含被官方 `place` 过滤掉的数据，
     FAQ 2536 明确过滤是官方预期行为）。它与上面两个数**不在同一个集合上**，
     绝不能相加、互相校验或替代；评分只用 `count_in`（PRD §4.5「圈内该类数量」）。
3. 🔴 **没实测就是没有，绝不拿直线距离冒充实测时间**。`measured_minutes is None` 的点一律带
   `unmeasured=True` + `unmeasured_reason`（前端据此灰显「未实测」）；某维度一个都没实测到时
   `nearest_minutes=None` + `nearest_reason`，时效分直接记 0（而不是记 1，
   也不是用直线距离凑一个数）。
   这是本泳道最容易写成「假绿」的地方。
4. 🔴 **`usage` 两栏永不合并**：`quota_units = origins × destinations`（这里就是 `1 × K`，
   双模式则 `2 × K`）与 `http_requests`（真实批次数 / 往返数）分开记，`route_lines` 按
   walking / riding 分栏（PRD §4.2 第 267 行、§4.6 硬事实；M2 已因合并立过 bug）。
   本层 `place_calls` 恒为 0：落圈判定不打 `place`，POI 检索那一层的 `place_calls` 在
   `PoiResultSet.usage` 里，由 M4 汇总，这里重复计一次就是两个数量级的错。

⚠️ PRD 缺口（`T_理想`）—— 见 §4.5 与本文件 `T_IDEAL_RATIO_DEFAULT` 的注释：
PRD §4.5 的公式只写符号 `时效分 = clamp((T_上限 - 最近实测时间) / (T_上限 - T_理想), 0, 1)`，
§4.3 的阈值表**只给每个维度的「达标阈值分钟」**（医疗 ≤8 / 教育 ≤12 / 市场 ≤8 / 休闲 ≤10 /
公交 ≤5 / 餐饮 ≤10），没有任何一处给出**逐维度的 `T_理想` 数值**。§4.5 第 318 行倒是有半句
`T_理想 = 阈值时间 × 0.5，T_上限 = 15 min`，但它是个「关系」而不是「表」：既没写清 `阈值时间`
是不是同一张表里的达标分钟，也和达标阈值本身冲突（公交达标 5 min × 0.5 = 2.5 min，
在 15 min 上限之下几乎没有区分度）。所以这里把 `T_理想` 做成**显式可配置参数**：
`t_ideal_ratio`（默认取上面那半句的 0.5）+ `t_ideal_minutes`（逐维度覆盖）。
默认值在输出里以 `t_ideal_source` 如实标注来源，不伪装成 PRD 定稿数字。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from services.isochrone_geometry import LngLat, as_lnglat, distance_m, point_in_ring
from services.route_probe import (
    BLOCKED_OFFICIAL,
    BLOCKED_TIMEOUT,
    MATRIX_ROW_CAP,
    MODE_RIDING,
    MODE_WALKING,
)

__all__ = [
    "DEFAULT_K",
    "DEFAULT_SELECTION",
    "DEFAULT_THRESHOLDS",
    "K_DESKTOP",
    "K_MOBILE",
    "MAX_MEASURED_POINTS",
    "REASON_BEYOND_TOP_K",
    "REASON_MISSING",
    "REASON_NO_POI_IN_POLYGON",
    "REASON_NO_POI_RETURNED",
    "REASON_NO_PROBE",
    "REASON_OUTSIDE_POLYGON",
    "REASON_PROBE_FAILED",
    "SELECTION_FAIR",
    "SELECTION_NEAREST",
    "T_IDEAL_RATIO_DEFAULT",
    "T_UPPER_MINUTES_DEFAULT",
    "CategoryThreshold",
    "CoverageProbe",
    "build_coverage",
    "canonical_dimension_key",
    "classify_pois_in_ring",
    "dimension_score",
    "quantity_score",
    "resolve_t_ideal_minutes",
    "select_measurement_targets",
    "select_measurement_targets_fair",
    "timeliness_score",
]

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
K_DESKTOP = 12
"""桌面端「前 K 实测」的 K（PRD §4.3 第 4 条）。"""

K_MOBILE = 6
"""移动端 K：屏幕放不下 12 行明细，实测条数也跟着砍一半，配额口径不变。"""

DEFAULT_K = K_DESKTOP
"""默认按桌面端取；调用方（M4 编排层）按 `platform` 显式传 6，本层不猜设备。"""

MAX_MEASURED_POINTS = MATRIX_ROW_CAP
"""单次批量算路的目的地硬上限（官方 `origins × destinations ≤ 50`）。超了**夹紧到 50 并留痕**，
而不是悄悄切成两批：切批会把「1 次批量算路」这条设计前提从报告里抹掉，审计就看不出省了多少。"""

T_UPPER_MINUTES_DEFAULT = 15.0
"""`T_上限`（PRD §4.5 给的 15 min，同时是 §4.2 的默认目标时长，两者同口径）。"""

T_IDEAL_RATIO_DEFAULT = 0.5
"""`T_理想 = 达标阈值分钟 × 本比例`。⚠️ 这是**显式假设**，来源只是 PRD §4.5 第 318 行的
半句关系式、不是逐维度定稿数值（详见模块 docstring 的「PRD 缺口」段）。
要换口径请传 `t_ideal_minutes={...}` 覆盖，或在 `t_ideal_ratio` 上改比例，别改这里的默认值。"""

MINUTE_FIELDS: tuple[str, ...] = ("measured_minutes", "nearest_minutes")
"""对外一律**分钟**（与 §4.7 的 `nearest_minutes` 一致）；秒只在批量算路的回包里存在。"""

# `unmeasured_reason`：点位级。四种互斥来源，前端灰显文案按这里分档。
REASON_OUTSIDE_POLYGON = "outside_polygon"
"""圈外：按 PRD §4.3 第 4 条，实测只对「圈内」的点做，圈外点根本不进批量算路。"""

REASON_BEYOND_TOP_K = "beyond_top_k"
"""圈内但没进「直线距离最近的前 K 个」——配额换出来的取舍，不是数据缺失。"""

REASON_NO_PROBE = "no_probe_injected"
"""调用方没给探测器（离线/纯本地跑法）：本层零出网，全部点位只按直线距离呈现。"""

REASON_PROBE_FAILED = "probe_failed"
"""探测器抛异常 / 预算截停（FR-403：只降级不外抛）。"""

REASON_MISSING = "measurement_missing"
"""发了这一批、这一格没回耗时（官方禁行 / 空结果 / 缺格），语义由 `blocked_kind` 区分。"""

# `nearest_reason`：维度级。
REASON_NO_POI_RETURNED = "no_poi_returned"
"""该维度返回体里一个点都没有（对应 `empty_reason="检索无结果"`，PRD §4.3 第 6 条：不是异常）。"""

REASON_NO_POI_IN_POLYGON = "no_poi_in_polygon"
"""半径内有、圈内一个都没有：`count_in=0` 是真实结论，不是没测到。"""

REASON_NO_POI_SELECTED = "no_poi_selected"
"""圈内有但全被前 K 截断挤出去了 → 没有实测值，只能报 `None`。"""

MEASURED_BASIS = "measured"
"""`nearest_minutes_source` 的唯一合法值：明确告诉报告「这是实测步行时间」，没有第二种来源。"""

COUNT_BASIS: dict[str, str] = {
    "count_in": "本地射线法：落在等时圈多边形内的**返回页**（第一页，不翻页）POI 数，零 API 调用",
    "count_near": "半径内但圈外 = len(pois) - count_in，纯本地；与 count_in 自洽可校验",
    "count_total": (
        "官方返回体 total（上限 150，含被 place 过滤的数据），" "与上面两个不在同一集合，不可相加"
    ),
}
"""三个口径的原文说明，随 `meta` 下发给前端与报告，
防止渲染时被并成一列（模块 docstring 第 2 条）。"""


# --------------------------------------------------------------------------------------
# 阈值表（PRD §4.3 第 2 条的权重与达标阈值；`PoiResultSet` 若自带阈值则以它为准）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CategoryThreshold:
    """一个维度的评分输入。`threshold_count` / `threshold_minutes` 就是 §4.3 表里的达标阈值。"""

    key: str
    label: str
    weight: float
    threshold_count: int
    threshold_minutes: float
    scored: bool = True
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "threshold_count": self.threshold_count,
            "threshold_minutes": self.threshold_minutes,
            "scored": self.scored,
            "note": self.note,
        }


DEFAULT_THRESHOLDS: dict[str, CategoryThreshold] = {
    "medical": CategoryThreshold("medical", "基础医疗", 0.20, 2, 8.0),
    "education": CategoryThreshold("education", "教育", 0.15, 1, 12.0),
    "shopping": CategoryThreshold("shopping", "购物市场", 0.20, 3, 8.0),
    "leisure": CategoryThreshold("leisure", "养老休闲", 0.15, 1, 10.0),
    "transit": CategoryThreshold("transit", "公共交通", 0.15, 2, 5.0),
    "food": CategoryThreshold("food", "餐饮服务", 0.10, 5, 10.0),
    # 便民服务：PRD §4.3 明确「不计分、仅列入明细表」——`scored=False`，权重 0。
    "convenience": CategoryThreshold(
        "convenience", "便民服务（参考项）", 0.0, 0, 10.0, scored=False, note="参考项，不进总分"
    ),
}
"""键名沿用 §4.7 响应示例里的 `medical`。别名见 `_KEY_ALIASES`：POI 层可能用 `market` /
`healthcare` / `transit_station` 之类，本层做一层收口，**收不到就用调用方 group 自带的阈值**，
绝不因为键名不一致就把达标阈值猜成一个默认数。"""

_KEY_ALIASES: dict[str, str] = {
    "healthcare": "medical",
    "medical_clinic": "medical",
    "药店": "medical",
    "education_school": "education",
    "学校": "education",
    "market": "shopping",
    "shopping_market": "shopping",
    "购物": "shopping",
    "elderly": "leisure",
    "leisure_elderly": "leisure",
    "park": "leisure",
    "transit_station": "transit",
    "transport": "transit",
    "food_service": "food",
    "dining": "food",
    "service": "convenience",
    "convenience_service": "convenience",
    # POI 词表层用的键是 `service_ref`（`services/poi_categories.py:DIMENSIONS`），
    # 报告层的键是 §4.7 示例里的 `convenience`。不收口的话参考项会以 `service_ref`
    # 出现在报告里、而 `DEFAULT_THRESHOLDS` 与 `config/weights.yaml` 都在说 `convenience`，
    # 表现为「改了参考项权重却不生效」——参考项不计分，所以这个失配**永远看不出来**。
    "service_ref": "convenience",
    "便民": "convenience",
}


@runtime_checkable
class CoverageProbe(Protocol):
    """本层唯一依赖的出网接口（`services/route_probe.py:RouteProbe` 已满足它）。

    测试注入 fake/mock 即可零真实出网（条款 3.2.3(ii)）；返回值只按属性/键名读，
    不要求一定是 `MatrixProbeResult`。
    """

    async def probe(
        self, origin: Any, points: Sequence[Any], *, mode: str | None = None
    ) -> Any: ...


# --------------------------------------------------------------------------------------
# 评分（纯函数，PRD §4.5；M4 的总分 = Σ S_i × w_i 不在这里做）
# --------------------------------------------------------------------------------------
def quantity_score(count_in: int, threshold_count: int) -> float:
    """数量分 = `min(count_in / 达标阈值数量, 1)`。

    `threshold_count <= 0` 只出现在参考项（便民）或配置缺省：此时「有就有、没有就没有」，
    回 `1.0 if count_in > 0 else 0.0`，不做除零、也不假装阈值存在。
    """
    count = max(0, int(count_in))
    threshold = int(threshold_count)
    if threshold <= 0:
        return 1.0 if count > 0 else 0.0
    return min(count / float(threshold), 1.0)


def resolve_t_ideal_minutes(
    threshold_minutes: float,
    *,
    key: str,
    ratio: float = T_IDEAL_RATIO_DEFAULT,
    overrides: Mapping[str, float] | None = None,
) -> tuple[float, str]:
    """求 `T_理想` 并**如实报告它的来源**（`t_ideal_source` 会进响应，供审计与文案复核）。

    优先级：逐维度显式覆盖 > 全局比例 > 比例默认（PRD §4.5 第 318 行那句 `× 0.5`）。
    比例越界（不在开区间 `(0, 1)`）时夹回 `(0.05, 0.95)` 并留痕：比例 ≥1 会让分母
    `T_上限 - T_理想` ≤ 0，时效分整个失去定义，比给个数更糟。
    """
    given = overrides or {}
    raw = given.get(key)
    if raw is not None:
        value = float(raw)
        source = f"explicit_override:{key}={value:g}"
    else:
        use_ratio = float(ratio)
        clamped = min(0.95, max(0.05, use_ratio))
        source = f"ratio_clamped:{clamped:g}" if clamped != use_ratio else f"ratio:{clamped:g}"
        value = float(threshold_minutes) * clamped
    if not math.isfinite(value) or value < 0.0:
        return 0.0, f"invalid_fallback:{value!r}"
    return round(value, 4), source


def timeliness_score(
    nearest_minutes: float | None,
    t_upper: float,
    t_ideal: float,
) -> float:
    """时效分 = `clamp((T_上限 - 最近实测时间) / (T_上限 - T_理想), 0, 1)`。

    🔴 `nearest_minutes is None` → **0 分**（没实测到就是不达标），并且调用方必须同时在
    `nearest_reason` 里写明原因。这里绝不接受「直线距离换算的时间」作为入参替身——那个替身
    会把「没测到」洗成「测得很好」，是本泳道唯一的假绿路径。
    `T_上限 <= T_理想`（配错）→ 退化判定：实测 ≤ `T_理想` 记 1，否则 0，不抛异常也不给 NaN。
    """
    if nearest_minutes is None:
        return 0.0
    upper = float(t_upper)
    ideal = float(t_ideal)
    minutes = float(nearest_minutes)
    if not math.isfinite(minutes):
        return 0.0
    span = upper - ideal
    if span <= 0.0:
        return 1.0 if minutes <= ideal else 0.0
    raw = (upper - minutes) / span
    if raw <= 0.0:
        return 0.0
    return min(raw, 1.0)


def dimension_score(
    count_in: int,
    threshold_count: int,
    nearest_minutes: float | None,
    t_upper: float,
    t_ideal: float,
) -> dict[str, Any]:
    """单维度得分 `S_i = 100 × (0.5 × 数量分 + 0.5 × 时效分)`（含两个分项，便于报告解释）。"""
    quantity = quantity_score(count_in, threshold_count)
    timeliness = timeliness_score(nearest_minutes, t_upper, t_ideal)
    total = 100.0 * (0.5 * quantity + 0.5 * timeliness)
    return {
        "quantity_score": round(quantity, 4),
        "timeliness_score": round(timeliness, 4),
        "score": round(total, 2),
    }


# --------------------------------------------------------------------------------------
# 落圈判定（纯本地，零 API）
# --------------------------------------------------------------------------------------
def _coerce_ring(source: Any) -> list[LngLat]:
    """把多种上游形状收口成环：`IsochroneResult`（取其 `.ring`）/ `[lng,lat]` 序列 /
    `{"lng":..,"lat":..}` 序列 / GeoJSON Polygon·Geometry / `{"polygon": {...}}` 包装。

    递归扒 `coordinates` 时靠 `as_lnglat` 的失败来判「这一层还是不是点」，
    不额外猜结构（猜错的代价是把经度当纬度，R8 同源坑）。
    """
    if source is None:
        return []
    for attr in ("ring",):
        inner = getattr(source, attr, None)
        if inner is not None and inner is not source:
            source = inner
            break
    if isinstance(source, (LngLat, Mapping)) and not _is_geojson_mapping(source):
        return [as_lnglat(source)]
    if isinstance(source, str):
        raise ValueError("等时圈环不能是字符串")
    items = list(source)
    if not items:
        return []
    first = items[0]
    if isinstance(first, (list, tuple)) and len(first) == 2 and _is_number_pair(items):
        return [as_lnglat(item) for item in items]
    if isinstance(first, (list, tuple, Mapping)):
        nested = _unwrap_coordinates(first)
        if nested is not None:
            return _coerce_ring(nested)
    return [as_lnglat(item) for item in items]


def _is_number_pair(items: Sequence[Any]) -> bool:
    return all(
        isinstance(item, (list, tuple))
        and len(item) == 2
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in item)
        for item in items
    )


def _is_geojson_mapping(value: Mapping[str, Any]) -> bool:
    return "coordinates" in value or "geometry" in value or "polygon" in value


def _unwrap_coordinates(value: Any) -> Any:
    """从 `{"coordinates": ...}` / 嵌套 list 里再扒一层，扒不到回 `None`。"""
    if isinstance(value, Mapping):
        for key in ("polygon", "geometry", "coordinates", "ring"):
            if key in value:
                return value[key]
        return None
    if isinstance(value, (list, tuple)) and value:
        head = value[0]
        if isinstance(head, Mapping):
            return _unwrap_coordinates(head)
        if isinstance(head, (list, tuple)):
            return None if _is_number_pair(value) else value
    return None


def _poi_position(poi: Mapping[str, Any]) -> LngLat | None:
    """`PoiHit.lnglat` 是 BD09LL 的 `[经度, 纬度]`（契约已定死顺序，本层不转、不猜）。"""
    raw = poi.get("lnglat")
    if raw is None:
        raw = poi.get("location")
    try:
        return as_lnglat(raw)
    except (TypeError, ValueError, IndexError):
        return None


def classify_pois_in_ring(
    pois: Sequence[Mapping[str, Any]],
    ring: Any,
) -> tuple[list[dict[str, Any]], int, int]:
    """给每个 POI 回填 `in_polygon`，回 `(副本列表, count_in, count_near)`。

    自洽式：`count_in + count_near == len(pois)`。坐标不可解析的点按**圈外**计（宁可少计圈内，
    也不把「判不了」说成「在圈内」），并在该点上留 `polygon_error`。
    """
    pts = _coerce_ring(ring)
    out: list[dict[str, Any]] = []
    count_in = 0
    for poi in pois or ():
        copy = dict(poi)
        position = _poi_position(copy)
        if position is None:
            copy["in_polygon"] = False
            copy["polygon_error"] = "unparsable_lnglat"
        else:
            inside = point_in_ring(position, pts) if len(pts) >= 3 else False
            copy["in_polygon"] = bool(inside)
            if inside:
                count_in += 1
        out.append(copy)
    return out, count_in, max(0, len(out) - count_in)


# --------------------------------------------------------------------------------------
# 前 K 实测（1 次批量算路）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MeasurementTarget:
    """一个待实测点位：`ref` 是 `(维度, uid)` 的稳定引用，回包按下标贴回去。"""

    ref: str
    group_key: str
    uid: str
    position: LngLat
    straight_distance_m: float

    @property
    def as_point(self) -> dict[str, float]:
        """批量算路的 `destinations` 元素。用 `{lng,lat}` 而不是元组：元组在两种顺序下都能过，
        键名没有歧义（与 `services/isochrone_service.py` 同一口径）。"""
        return {"lng": self.position.lng, "lat": self.position.lat}


def _sort_key(target: MeasurementTarget) -> tuple[float, str, str]:
    """稳定排序：直线距离 → 维度键 → uid。后两个键是为了**可复现**（FR-301）：
    同距离的点若靠 dict 顺序或 `id()` 决胜，两次运行会选出两批点位，实测时间跟着变。"""
    return (round(float(target.straight_distance_m), 3), target.group_key, target.uid)


def select_measurement_targets(
    candidates: Sequence[MeasurementTarget],
    k: int = DEFAULT_K,
) -> tuple[list[MeasurementTarget], int, bool]:
    """「圈内 + 直线距离最近的前 K 个」。回 `(选中点位, 生效 K, 是否被 50 上限夹紧)`。

    超出 K 的点位不发、也不补第二批（PRD §4.3 第 4 条的控制调用量语义）。
    """
    wanted = max(0, int(k))
    capped = wanted > MAX_MEASURED_POINTS
    effective = min(wanted, MAX_MEASURED_POINTS) if wanted else 0
    ordered = sorted(candidates, key=_sort_key)
    return ordered[:effective], effective, capped


SELECTION_NEAREST = "nearest"
"""严格照 §4.3 第 4 条字面：**全局**直线距离最近的前 K 个，不保证每个维度都有点位。"""

SELECTION_FAIR = "fair"
"""分层选择：先给每个维度留一个最近点，再用全局最近点填满剩余名额。

🔴 为什么默认取这个而不是字面的 `nearest`（这是本项目对 PRD 的一处**显式偏离**，必须读）：

PRD §4.3 第 4 条写的是「圈内 + 直线距离最近的前 K 个」，它管的是**调用量**（K ≤ 50、
只发 1 批），这一点 `fair` 完全遵守（总数仍是 K、仍是 1 批、配额一模一样）。
但把同一条规则用到**评分**上会产生一个与事实相反的结果：

  * 配套**密集**的老城社区：圈内 48 个点，全局最近的 12 个可能全落在餐饮与便利店里，
    于是医疗 / 购物 / 公交三个维度**一个都没实测到** → `nearest_minutes=None` →
    §4.5 的时效分记 0（`services/coverage.py` 刻意拒绝用直线距离顶替，见模块 docstring 第 3 条）
    → 这三个维度被硬压在 50 分。
  * 配套**稀疏**的新建小区：圈内只有 13 个点，几乎人人有实测值 → 六个维度时效分都是满分。

净效果是「设施越全、得分越低」。实测数据（`beijing-jiaodaokou` 67.25 分 C vs
`beijing-tiantongyuan` 84.66 分 B）就是这个倒挂 —— 而 §4.3 的达标阈值表本身写的是
「圈内数量 / **最近实测时间**」，逐维度都要一个实测时间，这说明 PRD 的本意**就是**
每个维度都得测到，只是没写清 K 该怎么分配。

所以 `fair` 是在「K 总量不变、调用量不变」的前提下补齐那个缺失的约束。
要回到字面口径：`build_coverage(..., selection=SELECTION_NEAREST)`，
报告里的 `meta.selection` 会如实回显本次用了哪一种。
"""

DEFAULT_SELECTION = SELECTION_FAIR


def select_measurement_targets_fair(
    candidates: Sequence[MeasurementTarget],
    k: int = DEFAULT_K,
) -> tuple[list[MeasurementTarget], int, bool, dict[str, int]]:
    """分层挑选：每个维度先保一个最近点，剩余名额按全局直线距离填。

    回 `(选中点位, 生效 K, 是否被夹紧, 逐维度配额)`。`选中的点位按 `_sort_key` 升序` ——
    顺序决定批量请求里的 `destinations` 下标，必须稳定（FR-301 可复算）。
    """
    wanted = max(0, int(k))
    capped = wanted > MAX_MEASURED_POINTS
    effective = min(wanted, MAX_MEASURED_POINTS) if wanted else 0
    if effective <= 0:
        return [], effective, capped, {}

    ordered = sorted(candidates, key=_sort_key)
    picked: list[MeasurementTarget] = []
    seen_refs: set[str] = set()
    allocation: dict[str, int] = {}

    # 第一轮：每个维度各取最近的一个（按全局距离升序扫，谁最近谁先占名额）。
    for target in ordered:
        if len(picked) >= effective:
            break
        if target.group_key in allocation:
            continue
        picked.append(target)
        seen_refs.add(target.ref)
        allocation[target.group_key] = 1

    # 第二轮：剩余名额按全局距离填满（维度数 > K 时第一轮就没填满，这里自然兜住）。
    if len(picked) < effective:
        for target in ordered:
            if len(picked) >= effective:
                break
            if target.ref in seen_refs:
                continue
            picked.append(target)
            seen_refs.add(target.ref)
            allocation[target.group_key] = allocation.get(target.group_key, 0) + 1

    picked.sort(key=_sort_key)
    return picked, effective, capped, allocation


def _select_targets(
    candidates: Sequence[MeasurementTarget],
    k: int,
    selection: str,
) -> tuple[list[MeasurementTarget], int, bool, dict[str, int]]:
    """按 `selection` 分派到两种挑选策略；无法识别的值**回落到默认策略并继续**。

    为什么不在这里抛异常：挑选策略是报告口径的一部分，不是入参契约的一部分。
    一个拼错的 `selection` 不该让整次体检 500（FR-403），报告里的 `meta.selection`
    会显示实际生效的那一种，审计照样看得出来。
    """
    if str(selection).strip().lower() == SELECTION_NEAREST:
        picked, effective, capped = select_measurement_targets(candidates, k)
        return picked, effective, capped, {}
    return select_measurement_targets_fair(candidates, k)


def _num(obj: Any, key: str, default: int | float = 0) -> int | float:
    """`MatrixProbeResult` 属性 / dict 键两种形状都能读到的取值器（假对象便于用 dict 写）。"""
    value = obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value) if isinstance(value, int) else float(value)
    except (TypeError, ValueError):
        return default


def _cell_view(cell: Any) -> dict[str, Any]:
    """一格批量算路回包的归一化读法（属性或键名都认）。"""
    if isinstance(cell, Mapping):
        return dict(cell)
    return {
        "index": getattr(cell, "index", None),
        "measured": getattr(cell, "measured", None),
        "duration_s": getattr(cell, "duration_s", None),
        "distance_m": getattr(cell, "distance_m", None),
        "blocked_kind": getattr(cell, "blocked_kind", None),
        "signals": getattr(cell, "signals", None) or [],
        "status": getattr(cell, "status", None),
        "error": getattr(cell, "error", None),
    }


def _result_cells(result: Any) -> list[Any]:
    cells = getattr(result, "outcomes", None)
    if cells is None and isinstance(result, Mapping):
        cells = result.get("outcomes") or result.get("cells")
    return list(cells or ())


def _cache_hits(result: Any) -> int:
    """缓存命中：`batches[].from_cache` 优先，假对象也可直接给 `cache_hits` 键。"""
    if isinstance(result, Mapping):
        direct = result.get("cache_hits")
    else:
        direct = getattr(result, "cache_hits", None)
    if isinstance(direct, int):
        return direct
    batches = getattr(result, "batches", None)
    if batches is None and isinstance(result, Mapping):
        batches = result.get("batches")
    return sum(
        1
        for batch in (batches or ())
        if (
            batch.get("from_cache")
            if isinstance(batch, Mapping)
            else getattr(batch, "from_cache", False)
        )
    )


@dataclass
class CoverageUsage:
    """本层的三栏账（模块 docstring 第 4 条）。字段之间不做任何互相折算。"""

    http_requests: int = 0
    quota_units: int = 0
    place_calls: int = 0
    cache_hits: int = 0
    route_lines: dict[str, int] = field(default_factory=lambda: {MODE_WALKING: 0, MODE_RIDING: 0})
    batches_sent: int = 0
    requested_cells: int = 0
    measured_cells: int = 0
    blocked_official_cells: int = 0
    blocked_timeout_cells: int = 0
    budget_stops: int = 0
    probe_calls: int = 0
    probe_failures: int = 0

    def add(self, result: Any, mode: str, *, sent_cells: int) -> None:
        """把一段 `MatrixProbeResult` 并进总账（两栏各并各的）。"""
        self.probe_calls += 1
        self.http_requests += int(_num(result, "http_requests"))
        self.quota_units += int(_num(result, "quota_units"))
        self.place_calls += int(_num(result, "place_calls"))
        self.cache_hits += _cache_hits(result)
        self.route_lines[mode] = self.route_lines.get(mode, 0) + int(_num(result, "route_lines"))
        batches = getattr(result, "batches", None)
        if batches is None and isinstance(result, Mapping):
            batches = result.get("batches")
        self.batches_sent += sum(
            1
            for batch in (batches or ())
            if (batch.get("sent") if isinstance(batch, Mapping) else getattr(batch, "sent", False))
        )
        self.requested_cells += int(_num(result, "requested_cells", sent_cells))
        # 逐格计数按 `outcomes` 现算，不信任假对象里的汇总键；两种 blocked 也绝不合并。
        for cell in _result_cells(result):
            view = _cell_view(cell)
            if view.get("measured"):
                self.measured_cells += 1
            elif view.get("blocked_kind") == BLOCKED_OFFICIAL:
                self.blocked_official_cells += 1
            elif view.get("blocked_kind") == BLOCKED_TIMEOUT:
                self.blocked_timeout_cells += 1
        if bool(_num(result, "budget_stopped")):
            self.budget_stops += 1

    def as_dict(self) -> dict[str, Any]:
        """§4.7 的 `coverage.usage`：`quota_units`（1×K 口径）与 `http_requests`（批次数）并列。"""
        return {
            "http_requests": int(self.http_requests),
            "quota_units": int(self.quota_units),
            "cache_hits": int(self.cache_hits),
            "place_calls": int(self.place_calls),
            "route_lines": {
                MODE_WALKING: int(self.route_lines.get(MODE_WALKING, 0)),
                MODE_RIDING: int(self.route_lines.get(MODE_RIDING, 0)),
                **{
                    mode: int(value)
                    for mode, value in self.route_lines.items()
                    if mode not in (MODE_WALKING, MODE_RIDING)
                },
            },
            "batches_sent": int(self.batches_sent),
            "probe_calls": int(self.probe_calls),
            "probe_failures": int(self.probe_failures),
            "requested_cells": int(self.requested_cells),
            "measured_cells": int(self.measured_cells),
            "blocked_official_cells": int(self.blocked_official_cells),
            "blocked_timeout_cells": int(self.blocked_timeout_cells),
            "budget_stops": int(self.budget_stops),
        }


def _center_of(poi_result: Mapping[str, Any], fallback: Any) -> LngLat | None:
    raw = poi_result.get("center")
    if raw is None:
        raw = fallback
    if raw is None:
        return None
    try:
        return as_lnglat(raw)
    except (TypeError, ValueError, IndexError):
        return None


def _group_key(raw: Mapping[str, Any], index: int) -> str:
    key = str(raw.get("key") or raw.get("category") or f"group_{index}").strip().lower()
    return _KEY_ALIASES.get(key, key)


def canonical_dimension_key(key: Any) -> str:
    """把外部来源的维度键收口成本层使用的规范键（`market` → `shopping` 等）。

    存在的理由：`config/weights.yaml` 与 `services/poi_categories.py:DIMENSIONS` 用的是
    POI 层的键（`market` / `service_ref`），而本层的阈值表用的是 §4.7 响应示例里的键
    （`shopping` / `convenience`）。`_threshold_for()` 内部一直走 `_group_key()` 收口，
    但**外部传入的 `threshold_overrides` 必须用同一个函数收口**，否则「配了权重却不生效」
    会表现为静默失配——这类错在报告里看不出来，只会让分数悄悄不对。
    """
    text = str(key or "").strip().lower()
    return _KEY_ALIASES.get(text, text)


def _threshold_for(
    key: str,
    raw: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> CategoryThreshold:
    """阈值优先级：调用方显式 `threshold_overrides` > group 自带 > `DEFAULT_THRESHOLDS` > 中性值。

    group 自带的 `threshold_count` / `threshold_minutes` 是 POI 层从同一张 §4.3 表读的，
    所以「group 有就用 group 的」而不是用本表覆盖它——两处各改一份就会对不上。
    """
    base = DEFAULT_THRESHOLDS.get(key)
    merged: dict[str, Any] = dict(base.as_dict()) if base else {}
    for source in (
        {
            "threshold_count": raw.get("threshold_count"),
            "threshold_minutes": raw.get("threshold_minutes"),
            "weight": raw.get("weight"),
            "label": raw.get("label"),
            "scored": raw.get("scored"),
        },
        dict(overrides or {}).get(key, {}),
    ):
        for field_name, value in source.items():
            if value is not None:
                merged[field_name] = value
    threshold_count = int(merged.get("threshold_count") or 0)
    minutes = float(merged.get("threshold_minutes") or 0.0)
    weight_raw = merged.get("weight")
    scored = merged.get("scored")
    return CategoryThreshold(
        key=key,
        label=str(merged.get("label") or key),
        weight=float(weight_raw) if weight_raw is not None else 0.0,
        threshold_count=threshold_count,
        threshold_minutes=minutes,
        scored=True if scored is None else bool(scored),
        note=merged.get("note"),
    )


async def _measure_mode(
    probe: CoverageProbe | None,
    center: LngLat | None,
    targets: Sequence[MeasurementTarget],
    mode: str,
    usage: CoverageUsage,
    notes: list[str],
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """1 次批量算路取回 K 个点的真实步行耗时：`origins=[中心点]`、`destinations=K 个 POI`。

    回 `(ref → 分钟, ref → 缺测留痕)`。任何异常 / 空结果 / 缺格都只留痕不外抛（FR-403），
    并且**绝不**用直线距离补一个时间进来（模块 docstring 第 3 条）。
    """
    if not targets:
        return {}, {}
    if probe is None:
        return {}, {target.ref: {"reason": REASON_NO_PROBE} for target in targets}
    if center is None:
        return {}, {target.ref: {"reason": "missing_center_for_batch_route"} for target in targets}

    origin = {"lng": center.lng, "lat": center.lat}
    points = [target.as_point for target in targets]
    try:
        result = await probe.probe(origin, points, mode=mode)
    except Exception as exc:  # 探测器异常 / 预算截停：整批标注缺测，不中断统计
        usage.probe_failures += 1
        notes.append(f"批量算路（{mode}）失败：{type(exc).__name__}: {str(exc)[:120]}")
        return {}, {
            target.ref: {"reason": REASON_PROBE_FAILED, "error": type(exc).__name__}
            for target in targets
        }

    if result is None:
        usage.probe_failures += 1
        notes.append(f"批量算路（{mode}）返回空对象，本维度本轮一律按未实测处理。")
        return {}, {target.ref: {"reason": REASON_PROBE_FAILED} for target in targets}

    usage.add(result, mode, sent_cells=len(targets))
    stopped = bool(_num(result, "budget_stopped"))
    if stopped:
        reason = "budget_stopped"
        state = getattr(result, "stopped_reason", None)
        if state is None and isinstance(result, Mapping):
            state = result.get("stopped_reason")
        notes.append(f"批量算路（{mode}）被预算守卫截停：{state or '未给出原因'}")
    else:
        reason = REASON_MISSING

    minutes: dict[str, float] = {}
    failures: dict[str, dict[str, Any]] = {}
    cells = {_cell_view(cell).get("index"): _cell_view(cell) for cell in _result_cells(result)}
    for index, target in enumerate(targets):
        cell = cells.get(index)
        duration = None if cell is None else cell.get("duration_s")
        if cell is None or not cell.get("measured") or duration is None:
            entry: dict[str, Any] = {
                "reason": reason if not stopped else REASON_MISSING,
                "batch_status": None if cell is None else cell.get("status"),
                "blocked_kind": None if cell is None else cell.get("blocked_kind"),
                "signals": list(() if cell is None else (cell.get("signals") or ())),
            }
            if cell is None:
                entry["reason"] = "missing_cell"
            failures[target.ref] = entry
            continue
        value = float(duration) / 60.0
        if not math.isfinite(value) or value < 0.0:
            failures[target.ref] = {"reason": "invalid_duration", "duration_s": duration}
            continue
        minutes[target.ref] = round(value, 2)
    return minutes, failures


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------
async def build_coverage(
    poi_result: Mapping[str, Any],
    ring: Any = None,
    *,
    probe: CoverageProbe | None = None,
    modes: Sequence[str] = (MODE_WALKING,),
    k: int = DEFAULT_K,
    radius_m: float | None = None,
    center: Sequence[float] | Mapping[str, float] | None = None,
    threshold_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    t_upper_minutes: float = T_UPPER_MINUTES_DEFAULT,
    t_ideal_ratio: float = T_IDEAL_RATIO_DEFAULT,
    t_ideal_minutes: Mapping[str, float] | None = None,
    selection: str = DEFAULT_SELECTION,
) -> dict[str, Any]:
    """M3 的圈内覆盖统计（PRD §4.7 的 `coverage` 段）。

    参数
    ----
    `poi_result`
        POI 检索层的 `PoiResultSet`（plain dict，契约见模块 docstring）。**只读不改**。
    `ring`
        等时圈环：`IsochroneResult`（自动取 `.ring`）/ 点序列 / GeoJSON Polygon。
        `None` 时视为「没有圈」→ 所有点判圈外、`count_in=0`，并留一条 note（不静默当成满圈）。
    `probe`
        `RouteProbe` 兼容对象。`None` = 纯本地跑法：零出网、全部点位 `unmeasured=True`、
        `nearest_minutes=None`。这是离线演示与单测的默认形态。
    `modes`
        实测的出行方式，`modes[0]` 是评分用的主模式（默认步行），其余只进分栏账目与
        `measured_minutes_by_mode`。骑行入日程后 `route_lines` 必须两栏分开（PRD §4.6）。
    `k`
        前 K 实测的 K（桌面 12 / 移动端 6）。> 50 时夹紧并留痕，不会自动切第二批。
    `radius_m`
        只用于报告口径说明；`count_near` 的定义集合就是「group 返回的点位」，
        不重新按半径过滤（POI 层已经带 `radius_limit=true` 过滤过一次）。
    `threshold_overrides` / `t_upper_minutes` / `t_ideal_ratio` / `t_ideal_minutes`
        评分参数。`t_ideal_*` 两个是 **PRD 未给逐维度数值** 的显式补位，来源随
        `t_ideal_source` 下发（见模块 docstring 的「PRD 缺口」段）。

    返回
    ----
    `{"groups": [{"category","count_in","count_near","count_total","nearest_minutes",
    "nearest_reason","score","pois":[...]}], "usage": {...}, "meta": {...}, "notes": [...]}`
    """
    notes: list[str] = []
    usage = CoverageUsage()
    use_modes: list[str] = []
    for item in modes or (MODE_WALKING,):
        name = str(item or MODE_WALKING).strip().lower()
        if name not in use_modes:
            use_modes.append(name)
    if not use_modes:
        use_modes = [MODE_WALKING]
    primary = use_modes[0]

    raw_groups = list(poi_result.get("groups") or ())
    resolved_center = _center_of(poi_result, center)
    ring_points = _coerce_ring(ring)
    if ring is None:
        notes.append("未提供等时圈环：落圈判定一律按圈外处理（count_in 全为 0），不是检索失败。")
    if len(ring_points) < 3:
        ring_points = []

    effective_k = min(max(0, int(k)), MAX_MEASURED_POINTS)
    if int(k) > MAX_MEASURED_POINTS:
        notes.append(
            f"k={int(k)} 超过批量算路单次上限 {MAX_MEASURED_POINTS}，已夹紧为 {effective_k}；"
            "本层不自动切第二批，以免破坏「K 个点位只发 1 批」的调用量口径。"
        )

    states: list[dict[str, Any]] = []
    candidates: list[MeasurementTarget] = []
    for index, raw in enumerate(raw_groups):
        group = dict(raw or {})
        key = _group_key(group, index)
        threshold = _threshold_for(key, group, threshold_overrides)
        pois, count_in, count_near = classify_pois_in_ring(
            list(group.get("pois") or ()), ring_points
        )
        count_total_raw = group.get("count_total")
        count_total = (
            int(count_total_raw)
            if isinstance(count_total_raw, int)
            else (int(count_total_raw) if count_total_raw else len(pois))
        )
        if count_total < len(pois):
            # 官方 total 比返回页还小，说明上游两处口径没对齐；取 len 兜底并留痕，
            # 不静默改大任何一方的数（三个数是不同集合，见模块 docstring 第 2 条）。
            notes.append(
                f"维度 {key} 的 count_total={count_total} 小于返回页 {len(pois)}，已按页量回显。"
            )
            count_total = len(pois)

        # `refs[position]` 与该维度的点位一一对应：圈外点为空串（根本不进批量算路）。
        # ref 里带**下标**而不是只带 uid：上游 uid 缺失时退化成下标，若只用 uid 会撞成同一个
        # ref，两个点位共用一份实测时间 —— 那是数据污染，不是可复现的口径。
        refs: list[str] = [""] * len(pois)
        for position, poi in enumerate(pois):
            if not poi.get("in_polygon"):
                continue
            point = _poi_position(poi)
            if point is None or resolved_center is None:
                continue
            given = poi.get("distance_m")
            straight = (
                float(given)
                if isinstance(given, (int, float)) and not isinstance(given, bool)
                else distance_m(resolved_center, point)
            )
            uid = str(poi.get("uid") or f"anon-{position}")
            ref = f"{key}\u2016{index}\u2016{position}\u2016{uid}"
            refs[position] = ref
            candidates.append(
                MeasurementTarget(
                    ref=ref,
                    group_key=key,
                    uid=uid,
                    position=point,
                    straight_distance_m=straight,
                )
            )

        states.append(
            {
                "key": key,
                "threshold": threshold,
                "pois": pois,
                "refs": refs,
                "count_in": count_in,
                "count_near": count_near,
                "count_total": count_total,
                "empty_reason": group.get("empty_reason"),
                "query": group.get("query"),
                "official_paths": group.get("official_paths"),
            }
        )

    selected, _, _, allocation = _select_targets(candidates, effective_k, selection)
    if selection == SELECTION_FAIR and candidates:
        starved = sorted({target.group_key for target in candidates} - set(allocation))
        if starved:
            notes.append(
                f"分层挑选后仍无实测值的维度：{', '.join(starved)}"
                f"（候选点位少于 K={effective_k} 或该维度圈内无点）。"
            )
    measured_by_mode: dict[str, dict[str, float]] = {}
    failures_by_mode: dict[str, dict[str, dict[str, Any]]] = {}
    for mode in use_modes:
        minutes, failures = await _measure_mode(
            probe, resolved_center, selected, mode, usage, notes
        )
        measured_by_mode[mode] = minutes
        failures_by_mode[mode] = failures

    selected_refs = {target.ref for target in selected}
    out_groups: list[dict[str, Any]] = []
    for state in states:
        key = state["key"]
        threshold: CategoryThreshold = state["threshold"]
        minutes_by_ref: dict[str, float] = {}
        for target in selected:
            if target.group_key != key:
                continue
            value = measured_by_mode[primary].get(target.ref)
            if value is not None:
                minutes_by_ref[target.ref] = value

        pois_out: list[dict[str, Any]] = []
        for position, poi in enumerate(state["pois"]):
            copy = dict(poi)
            ref = state["refs"][position]
            measured = minutes_by_ref.get(ref) if ref else None
            copy["measured_minutes"] = measured
            if measured is not None:
                copy["unmeasured"] = False
                copy["unmeasured_reason"] = None
                copy["measured_minutes_by_mode"] = {
                    mode: measured_by_mode[mode].get(ref)
                    for mode in use_modes
                    if measured_by_mode[mode].get(ref) is not None
                }
            else:
                failure = failures_by_mode[primary].get(ref) if ref else None
                if not copy.get("in_polygon"):
                    reason = REASON_OUTSIDE_POLYGON
                elif not ref:
                    # 圈内有坐标不可解析的点（`refs[position]` 为空）→ 它根本没进候选集。
                    reason = "no_batch_route_target"
                elif ref not in selected_refs:
                    reason = REASON_BEYOND_TOP_K
                elif probe is None:
                    reason = REASON_NO_PROBE
                else:
                    reason = (failure or {}).get("reason", REASON_MISSING)
                copy["unmeasured"] = True
                copy["unmeasured_reason"] = reason
                copy["measured_minutes_by_mode"] = {}
                if failure:
                    copy["measurement_signals"] = list(failure.get("signals") or ())
                    if failure.get("blocked_kind"):
                        copy["measurement_blocked_kind"] = failure["blocked_kind"]
                    if failure.get("batch_status") is not None:
                        copy["measurement_status"] = failure["batch_status"]
            pois_out.append(copy)

        nearest = min(minutes_by_ref.values()) if minutes_by_ref else None
        nearest_reason: str | None = None
        if nearest is None:
            if not state["pois"]:
                nearest_reason = str(state["empty_reason"] or REASON_NO_POI_RETURNED)
            elif state["count_in"] == 0:
                # 圈内一个都没有：这是真实结论（盲区判定要用它），不是「没测到」。
                nearest_reason = REASON_NO_POI_IN_POLYGON
            elif probe is None:
                nearest_reason = REASON_NO_PROBE
            elif not selected:
                nearest_reason = REASON_NO_POI_SELECTED
            else:
                nearest_reason = "measured_batch_returned_no_duration"
        t_ideal, t_ideal_source = resolve_t_ideal_minutes(
            threshold.threshold_minutes,
            key=key,
            ratio=t_ideal_ratio,
            overrides=t_ideal_minutes,
        )
        scored = dimension_score(
            state["count_in"], threshold.threshold_count, nearest, t_upper_minutes, t_ideal
        )
        out_groups.append(
            {
                "category": key,
                "label": threshold.label,
                "key": key,
                "weight": threshold.weight,
                "scored": threshold.scored,
                "count_in": state["count_in"],
                "count_near": state["count_near"],
                "count_total": state["count_total"],
                "count_basis": dict(COUNT_BASIS),
                "measured_count": len(minutes_by_ref),
                "unmeasured_count": sum(1 for item in pois_out if item.get("unmeasured")),
                "threshold_count": threshold.threshold_count,
                "threshold_minutes": threshold.threshold_minutes,
                "nearest_minutes": nearest,
                "nearest_minutes_source": MEASURED_BASIS if nearest is not None else None,
                "nearest_reason": nearest_reason,
                "t_upper_minutes": float(t_upper_minutes),
                "t_ideal_minutes": t_ideal,
                "t_ideal_source": t_ideal_source,
                "empty_reason": state["empty_reason"],
                "query": state["query"],
                "official_paths": state["official_paths"],
                **scored,
                "pois": pois_out,
            }
        )

    payload_source = poi_result.get("data_source")
    degraded = bool(poi_result.get("degraded")) or bool(usage.probe_failures)
    reason = poi_result.get("degraded_reason")
    if usage.probe_failures and not reason:
        reason = "measurement_batch_failed"
    return {
        "groups": out_groups,
        "usage": usage.as_dict(),
        "totals": {
            "groups": len(out_groups),
            "pois": sum(len(item["pois"]) for item in out_groups),
            "count_in": sum(item["count_in"] for item in out_groups),
            "count_near": sum(item["count_near"] for item in out_groups),
            "measured": sum(item["measured_count"] for item in out_groups),
            "unmeasured": sum(item["unmeasured_count"] for item in out_groups),
        },
        "meta": {
            "center": (
                None if resolved_center is None else [resolved_center.lng, resolved_center.lat]
            ),
            "mode": primary,
            "modes": list(use_modes),
            "radius_m": radius_m if radius_m is not None else poi_result.get("radius_m"),
            "vertex_count": len(ring_points),
            "has_ring": bool(ring_points),
            "k_requested": int(k),
            "k_effective": effective_k,
            "selected_points": len(selected),
            "selection": (
                SELECTION_NEAREST
                if str(selection).strip().lower() == SELECTION_NEAREST
                else SELECTION_FAIR
            ),
            "selection_allocation": dict(allocation),
            "selection_definition": (
                "fair = 每个维度先保一个最近点、剩余名额按全局直线距离填（K 总量与调用量不变）；"
                "nearest = §4.3 第 4 条字面的全局最近 K 个，可能让部分维度无实测值"
            ),
            "scored_primary_mode": primary,
            "coordinate_system": "bd09ll",
            "data_source": payload_source,
            "count_basis": dict(COUNT_BASIS),
            "nearest_minutes_definition": (
                "只取批量算路的实测值；未实测的点位一律不参与，也不用直线距离替代"
            ),
            "t_ideal_definition": (
                "T_理想 = 达标阈值分钟 × t_ideal_ratio；PRD §4.5/§4.3 未给逐维度数值，"
                "故为显式可配置假设（见 t_ideal_source）"
            ),
        },
        "degraded": degraded,
        "degraded_reason": reason,
        "notes": notes,
    }
