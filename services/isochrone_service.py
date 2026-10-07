"""services/isochrone_service.py —— 等时圈服务层：把「几何状态机」和「RouteProbe 批量出网」接起来。

接线口径（PRD §3.2 FR-201~206 / §4.2 / §4.7），四条硬约束决定了本文件的形状：

1. 🔴 **不阻塞事件循环、也不对线上施压**。`compute_isochrone()` 是完全同步的纯几何编排，
   二分轮次的驱动逻辑留在几何层（它自己的模块注释就是这么写的：「批量算路属于服务层，
   它通过本模块的 `IsochroneOracle` 协议被调用；出网留在服务层」）。所以这里走
   **同步编排 + 线程桥**：几何在 `asyncio.to_thread` 的 worker 线程里推进，oracle 要出网时用
   `asyncio.run_coroutine_threadsafe(...)` 把 `RouteProbe.probe()` 投回**原事件循环**执行，
   worker 线程等它回来。于是 CPU 不占事件循环、HTTP 仍由循环单点**串行**发出。
   🚫 任何时候都不 `gather` 多个 `route_matrix`——「有界线程池并发 direction」正是条款
   3.2.3(ii) 禁止的施压形态（PRD D8 / 笔记 §8.4）；官方 1×≤50 的批量请求已经省掉了 RTT。
2. 🔴 **配对只用几何层的 `pair_key(bearing_deg, distance_m)` 口径**：请求侧与回包侧共用同一个
   函数，杜绝「请求取整、回包不取整」的漂移。`MatrixProbeResult` 的逐格结果换成 `ProbeOutcome`
   时，`blocked_official` / `blocked_timeout` 与官方证据文本**原样透传**，本层绝不自己另判、
   也绝不把两种 blocked 合并（PRD §4.2 精度取舍：「路走不通」与「我没测准」话术完全不同）。
3. 🔴 **三层降级沿用 `services/geo.py` 的既有口径**：`live → cache → sample/mock`，其中
   **cache 层就是 `BaiduClient` 内置的响应级短 TTL 缓存**（`route_matrix` 已经走它）。
   本层**不新建第二份 SQLite**：条款 3.3.2(ii) / 3.3.4 禁止把响应存成可查询的数据库，
   再叠一层「等时圈结果库」是合规风险而不是性能优化。缓存命中时 `http_requests` 与
   `quota_units` 双双归 0（不是漏账，是真没花），`data_source` 据此报 `cache`。
4. 🔴 **mock/sample 态零出网且必须是不规则多边形**：`SyntheticOracle` 用「中心点做种子的
   PRNG + 各向异性速度场 + 一条顺畅走廊 + 两道阻隔扇区」产出**耗时场**，多边形交给同一套几何
   代码二分算出来。它不是正圆（FR-201 要求与同半径正圆目视差异明显），也不是录制下来的百度
   返回（PRD D9 / FR-412：最内层数据必须由我方算法合成）。

预算不足 / 额度耗尽一律 **200 + 可读降级文案**，不冒 500（FR-403 / FR-404）；只有入参边界
（`mode` 白名单、`minutes` 区间、坐标格式、境外坐标）才抛 `CircleDocError` 子类出 400。
`quota_units` 与 `http_requests` 两栏**永不相加合并**，`route_lines` 按 mode 分栏
（PRD §4.2 第 267 行 + FR-206 硬事实①）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import math
import random
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast

from infra.baidu_client import BaiduClient
from infra.cache_store import CacheStore, get_cache_store
from infra.config import DataMode, DataSource, EffectiveDataMode, Settings, get_settings
from infra.errors import CircleDocError
from infra.log import get_logger, log_fields

from services.geo import InvalidGeoParam, OutsideServiceArea, parse_lnglat
from services.isochrone_geometry import (
    MODE_RIDING,
    MODE_WALKING,
    IsochroneConfig,
    IsochroneResult,
    LngLat,
    OutcomeKind,
    ProbeOutcome,
    ProbeRequest,
    RoundProgress,
    Vertex,
    as_lnglat,
    compute_isochrone,
    destination,
    distance_m,
    get_mode_spec,
    initial_bearing,
    pair_key,
)
from services.route_probe import (
    BLOCKED_OFFICIAL,
    BLOCKED_TIMEOUT,
    POLYLINE_CALL_LIMIT,
    MatrixProbeResult,
    ProbePoint,
    RouteProbe,
)

__all__ = [
    "ALLOWED_MODES",
    "InvalidIsochroneParam",
    "IsochroneOutcome",
    "IsochroneService",
    "PolylineOutcome",
    "SyntheticOracle",
    "get_isochrone_service",
    "reset_isochrone_service",
]

logger = get_logger("circledoc.services.isochrone")

ALLOWED_MODES: tuple[str, ...] = (MODE_WALKING, MODE_RIDING)
"""`mode` 白名单（FR-104 / FR-206）。只这两档：`driving` 不在 PRD 范围内，配额口径也不同。"""

DEFAULT_MINUTES = 15.0
"""FR-104 的默认档位（5/10/15/30 里的 15）。"""

MINUTES_HARD_MIN = 5.0
MINUTES_HARD_MAX = 60.0
"""`minutes` 的合理区间：下限守住 FR-104 的最小档，上限守住 §5「低配机也能演示」。

真实判据还会被 `Settings.min_minutes / max_minutes` 收一道（可在 `.env` 调），两个数取
**交集**而不是各判一次，避免配置放宽后算法层还在偷偷截（或反之）。
"""

COORDINATE_SYSTEM = "bd09ll"
"""与 `services/geo.py` 同一口径：对外一律 BD09LL、一律 `[经度, 纬度]`（笔记 §6.1 / R8）。"""

BRIDGE_TIMEOUT_S = 90.0
"""单次 `route_matrix` 批请求在桥上的最长等待。超时**不外抛**：整批按 `MISSING` 回给几何层，
由状态机判 `blocked_timeout`（「我没测准」），绝不当成官方禁行。"""

SESSIONS_MAX = 8
"""按「分析会话」（中心点 + mode + 时长）保留的 `RouteProbe` 数量上限（LRU）。

为什么要会话这一层：FR-203 的 polyline 懒加载硬上限是「**一次分析** 12 次」，而每次点击是
一个独立 HTTP 请求。计数器若挂在服务单例上，现场点满 12 次就再也出不来路径串；若每请求新建
`RouteProbe`，12 次上限退化成「每次点击 1 次」，同一顶点重复点击也不再省配额。按会话键控
同时满足两头。M4 的 `/api/analysis/{task_id}` 落地后应由 task 持有会话键。
"""

# 用户可读文案。分工：百度侧的错误一律来自 `infra/errors.py`（`CircleDocError.user_message`），
# 这里只写「本层自己判断的输入 / 降级 / 合成」几类，两者不会打架（FR-404）。
MSG_MOCK_MODE = (
    "当前为离线（mock）模式：等时圈由我方算法按中心点合成，未发起任何实时调用，也不消耗今日配额。"
)
MSG_CACHE_HIT = "本次批量算路全部命中本地缓存，未发起实时调用，也不消耗今日配额。"
MSG_BUDGET_STOP = (
    "本次分析的路线配额预算已用尽，剩余方向改用插值/遇阻标注，多边形可能比真实形状小。"
)
MSG_PARTIAL = "部分方向没有取回实测耗时，已按几何层口径标注内插/遇阻，置信度随顶点一并下发。"
MSG_NO_LIVE_DATA = (
    "实时批量算路没有取回任何可用耗时（额度耗尽或服务未开通），已降级到我方合成的示例等时圈。"
)
MSG_LIVE_FAILED = "实时等时圈本次计算失败，已降级到我方合成的示例等时圈，流程未中断。"
MSG_POLYLINE_LIMIT = (
    "本次分析的路径串补调次数已达上限 {limit} 次（PRD D8：FR-203 只在点击时补调 direction，"
    "不预先拉取全部方向），顶点上的实测耗时与距离仍然可用。"
)
MSG_POLYLINE_SAMPLE = "离线（mock）模式：路径串为按方位角合成的示意线，非真实路网轨迹，不消耗配额。"
MSG_POLYLINE_UNAVAILABLE = (
    "实时路径规划本次不可用，已给出顶点的实测耗时与距离（来自等时圈那一批）。"
)

_SYNTHETIC_SPEED_KMH: dict[str, float] = {
    MODE_WALKING: 3.9,
    MODE_RIDING: 12.0,
}
"""合成速度口径：步行 3.9 km/h 是 PRD §4.2 第 269 行按官方样例推出的口径；骑行 12 km/h 是
**我方为示例数据自定的假设**（官方没有可引用的骑行速度数，几何层的 `MODES["riding"].speed_kmh`
因此是 `None`）。它只影响合成圈的大小，绝不进真实链路。"""


# --------------------------------------------------------------------------------------
# 异常：入参边界（HTTP 4xx 可读，绝不冒成 500）
# --------------------------------------------------------------------------------------
class InvalidIsochroneParam(InvalidGeoParam):
    """等时圈入参不合法（`mode` / `minutes` / 顶点位置）。

    为什么继承 `InvalidGeoParam` 而不是另起一个基类：`api/app.py` 只认 `CircleDocError`
    这一条线（统一 JSON 信封 + `request_id` + `retryable` + `category`），复用子类能让错误
    形状与 `/api/geocode` **逐字段一致**（FR-404）；只把 `error_id` 换成等时圈自己的，
    便于前端区分「是哪个输入框红了」。坐标与境外判定继续复用 `InvalidGeoParam` /
    `OutsideServiceArea`，本文件不造第二套语义。
    """

    error_id = "invalid_isochrone_param"
    user_message = "等时圈参数不符合要求，请检查出行方式、目标时长或顶点位置。"


# --------------------------------------------------------------------------------------
# 计量与结果对象
# --------------------------------------------------------------------------------------
@dataclass
class ProbeTally:
    """一次请求内花掉的三栏计量（**请求级**，不是进程累加器）。

    口径与 `services/geo.py:_usage_from` 一致：QA 的验收是「命中缓存时 `http_requests=0`」，
    而进程级累加器跨请求只增不减，第二轮即使命中也永远不为 0。
    """

    http_requests: int = 0
    quota_units: int = 0
    place_calls: int = 0
    route_lines_by_mode: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    oracle_calls: int = 0
    batches_sent: int = 0
    measured: int = 0
    blocked_official: int = 0
    blocked_timeout: int = 0
    missing: int = 0
    budget_stops: int = 0
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def add(self, result: MatrixProbeResult) -> None:
        """把一段 `MatrixProbeResult` 并进总账（两栏各并各的，永不互相折算）。"""
        self.http_requests += int(result.http_requests)
        self.quota_units += int(result.quota_units)
        self.place_calls += int(result.place_calls)
        key = result.mode or MODE_WALKING
        self.route_lines_by_mode[key] = self.route_lines_by_mode.get(key, 0) + int(
            result.route_lines
        )
        self.batches_sent += sum(1 for batch in result.batches if batch.sent)
        self.cache_hits += sum(1 for batch in result.batches if batch.from_cache)
        self.measured += len(result.measured_indices)
        self.blocked_official += len(result.blocked_official_indices)
        self.blocked_timeout += len(result.blocked_timeout_indices)
        if result.budget_stopped:
            self.budget_stops += 1
            self.stopped_reason = result.stopped_reason or self.stopped_reason

    def merge(self, other: ProbeTally) -> None:
        """把桥上累计的账并回调用方（`_run_live` 与 polyline 共用）。"""
        self.http_requests += other.http_requests
        self.quota_units += other.quota_units
        self.place_calls += other.place_calls
        for key, value in other.route_lines_by_mode.items():
            self.route_lines_by_mode[key] = self.route_lines_by_mode.get(key, 0) + value
        self.cache_hits += other.cache_hits
        self.oracle_calls += other.oracle_calls
        self.batches_sent += other.batches_sent
        self.measured += other.measured
        self.blocked_official += other.blocked_official
        self.blocked_timeout += other.blocked_timeout
        self.missing += other.missing
        self.budget_stops += other.budget_stops
        self.stopped_reason = self.stopped_reason or other.stopped_reason
        for item in other.errors:
            self.note_error(item)

    def note_error(self, text: str) -> None:
        if text and text not in self.errors:
            self.errors.append(text)

    @property
    def route_lines_total(self) -> int:
        return sum(self.route_lines_by_mode.values())

    def usage(self, *, elapsed_ms: int, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """§4.7 的 `usage`：`http_requests` 与 `quota_units` 分开，`route_lines` 按 mode 分栏。"""
        by_mode = {mode: int(self.route_lines_by_mode.get(mode, 0)) for mode in ALLOWED_MODES}
        for mode, value in self.route_lines_by_mode.items():
            by_mode.setdefault(str(mode), int(value))
        usage: dict[str, Any] = {
            "http_requests": int(self.http_requests),
            "quota_units": int(self.quota_units),
            "place_calls": int(self.place_calls),
            "cache_hits": int(self.cache_hits),
            # 🔴 [BUG][M2] 两个「累加了但从不出口」的数在这里补上（键名与 RouteProbe 对齐）：
            #   `batches_sent` 是 ≤50 切分真正发出的批次数（FR-205：解释 http_requests 为什么
            #     远小于方向数的唯一直接证据，原来只活在 `ProbeTally` 内部）；
            #   `budget_stops` 是预算守卫截停次数（FR-206 ①：降级文案说「预算已用尽」时，
            #     审计要能数出截停过几次，而不是只读 `degraded_reason` 字符串）。
            "batches_sent": int(self.batches_sent),
            "budget_stops": int(self.budget_stops),
            "elapsed_ms": int(elapsed_ms),
            "route_lines": by_mode,
        }
        if extra:
            usage.update(dict(extra))
        return usage


@dataclass(frozen=True)
class IsochroneOutcome:
    """一次等时圈分析的结论：内容 + 来自哪一层 + 为什么降级 + 本次三栏计量。

    `as_dict()` 就是 `/api/isochrone` 的响应体（与前端约定的契约，字段名已定稿别改）。
    `data_source` 值域 `live|cache|sample`（FR-306 徽标），`data_mode` 值域 `live|cache|mock`
    （用户选的策略）——两个字段同时下发，前端别把它们当成一个（PRD §4.6 命名区分）。
    """

    ok: bool
    data_mode: EffectiveDataMode
    data_source: DataSource
    degraded: bool
    message: str | None
    center: tuple[float, float]
    mode: str
    minutes: float
    target_seconds: float
    polygon: dict[str, Any]
    area_km2: float
    vertices: list[dict[str, Any]]
    duration_field: dict[str, Any]
    progress: list[dict[str, Any]]
    usage: dict[str, Any]
    stats: dict[str, Any] = field(default_factory=dict)
    degraded_reason: str | None = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """§4.7 的响应体（字段顺序即契约文档顺序；`request_id` 由路由层补）。"""
        return {
            "ok": self.ok,
            "data_mode": self.data_mode,
            "data_source": self.data_source,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "message": self.message,
            "center": [self.center[0], self.center[1]],
            "mode": self.mode,
            "minutes": self.minutes,
            "coordinate_system": COORDINATE_SYSTEM,
            "polygon": self.polygon,
            "area_km2": self.area_km2,
            "vertices": list(self.vertices),
            "duration_field": self.duration_field,
            "progress": list(self.progress),
            "usage": dict(self.usage),
            "stats": dict(self.stats),
            "meta": {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "target_seconds": self.target_seconds,
                "target_minutes": self.minutes,
                "vertex_count": len(self.vertices),
                "sample_layer": "synthetic" if self.data_source == "sample" else None,
            },
            "error": dict(self.error) if self.error is not None else None,
        }


@dataclass(frozen=True)
class PolylineOutcome:
    """FR-203 的一次「点击顶点补调 direction」结论（超限 / 失败都回结果对象，不抛异常）。"""

    ok: bool
    allowed: bool
    data_mode: EffectiveDataMode
    data_source: DataSource
    degraded: bool
    message: str | None
    center: tuple[float, float]
    mode: str
    minutes: float
    vertex: dict[str, Any]
    path: dict[str, Any] | None
    duration_s: float | None
    distance_m: float | None
    blocked: bool
    blocked_kind: str | None
    signals: list[str]
    status: int | None
    calls: dict[str, int]
    usage: dict[str, Any]
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "allowed": self.allowed,
            "data_mode": self.data_mode,
            "data_source": self.data_source,
            "degraded": self.degraded,
            "message": self.message,
            "center": [self.center[0], self.center[1]],
            "mode": self.mode,
            "minutes": self.minutes,
            "coordinate_system": COORDINATE_SYSTEM,
            "vertex": dict(self.vertex),
            "path": dict(self.path) if self.path is not None else None,
            "duration_s": self.duration_s,
            "duration_minutes": (
                None if self.duration_s is None else round(float(self.duration_s) / 60, 2)
            ),
            "distance_m": self.distance_m,
            "blocked": self.blocked,
            "blocked_kind": self.blocked_kind,
            "signals": list(self.signals),
            "status": self.status,
            "calls": dict(self.calls),
            "usage": dict(self.usage),
            "error": dict(self.error) if self.error is not None else None,
        }


# --------------------------------------------------------------------------------------
# 合成 oracle（mock / sample 层，零出网）
# --------------------------------------------------------------------------------------
def _seed_for(center: LngLat, mode: str, target_seconds: float) -> int:
    """中心点 → 稳定种子。

    用 `sha256` 而不是 `hash()`：后者按进程随机化（`PYTHONHASHSEED`），同一个社区今天演示和
    明天演示会长出两个不同的圈，FR-301「同一输入输出恒定」当场就破。
    """
    raw = f"{center.lng:.6f},{center.lat:.6f}|{mode}|{round(float(target_seconds))}"
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


class SyntheticOracle:
    """按中心点做种的确定性 oracle：产出**耗时场**，多边形交给同一套几何代码算。

    为什么这样设计（FR-201 / D9 / FR-412 三条同时成立的地方）：

    * 本层只回答「从中心沿 θ 方向走 d 米要几秒」，不直接画多边形。不规则性来自**各向异性的
      速度场**（三次谐波叠加 + 一条顺畅走廊 + 两道阻隔扇区），形状由二分收敛自己长出来——
      比手工撒一圈不规则坐标诚实，也保证 `measured` / `interpolated` / `extrapolated` /
      `blocked_timeout` 这些标签是算法判定而不是硬编码文案。
    * 阻隔方向回 `BLOCKED_TIMEOUT` 而**不是** `BLOCKED_OFFICIAL`：后者的语义是「百度给了官方
      硬信号」（`status=2001` / `restrictions_status` 位），合成数据没有资格冒充官方口径。
      `evidence` 文本一律带 `[synthetic]` 前缀，前端横幅据此说明这是示例数据。
    * 同一个中心点永远生成同一个圈（种子来自坐标而不是进程状态），演示预案 B 可复算。
    """

    def __init__(
        self,
        center: Any,
        *,
        mode: str = MODE_WALKING,
        target_seconds: float = 900.0,
        seed: int | None = None,
    ) -> None:
        self.center = as_lnglat(center)
        self.mode = mode if mode in ALLOWED_MODES else MODE_WALKING
        self.target_seconds = float(target_seconds)
        self.spec = get_mode_spec(self.mode)
        self.speed_mps = _SYNTHETIC_SPEED_KMH.get(self.mode, 3.9) / 3.6
        rng = random.Random(
            _seed_for(self.center, self.mode, self.target_seconds) if seed is None else int(seed)
        )
        coarse = [float(index * 30) for index in range(12)]
        # 各向异性：3 次谐波，振幅与相位由种子决定 → 半径在约 0.55~1.35 倍之间起伏。
        self.harmonics: tuple[tuple[float, float, float], ...] = tuple(
            (float(order), rng.uniform(0.08, 0.22), rng.uniform(0.0, 2.0 * math.pi))
            for order in (1, 2, 3)
        )
        # 一道「顺畅走廊」：中心方位角对准某个粗扫方向，保证至少一个方向在 R_max 处仍不到时限
        # → 几何层判 extrapolated（顶点标签因此至少两种，FR-201 的目视差异也来自这里）。
        self.corridor_bearing = float(rng.choice(coarse))
        self.corridor_width = rng.uniform(16.0, 26.0)
        self.corridor_gain = rng.uniform(1.55, 1.85)
        # 两道「阻隔扇区」：越过该半径后无解（高速路 / 铁路 / 水体 / 围墙）。
        self.barriers: tuple[tuple[float, float, float, str], ...] = tuple(
            (
                float(bearing),
                rng.uniform(13.0, 22.0),
                rng.uniform(0.32, 0.62),
                rng.choice(("河流", "高速公路", "铁路", "封闭园区围墙")),
            )
            for bearing in rng.sample(coarse, 2)
        )
        self.detour_gain = rng.uniform(1.08, 1.32)

    # -- 场模型 ---------------------------------------------------------------
    def speed_factor(self, bearing_deg: float) -> float:
        """方向上的速度倍率（<1 拥挤、>1 顺畅）。粗扫与加密的两个方向族取到同一个值。"""
        theta = math.radians(float(bearing_deg))
        factor = 1.0 + sum(
            amp * math.sin(float(order) * theta + phase) for order, amp, phase in self.harmonics
        )
        if _angular_offset_deg(theta, math.radians(self.corridor_bearing)) <= self.corridor_width:
            factor *= self.corridor_gain
        return max(0.45, min(2.2, factor))

    def barrier_for(self, bearing_deg: float) -> tuple[float, str] | None:
        """该方向是否被阻隔带截住；是则回「截住半径 + 阻隔物名称」。"""
        r_max = max(
            float(self.spec.r_max_m),
            self.target_seconds * self.speed_mps * 1.6,
        )
        for center_bearing, half_width, ratio, label in self.barriers:
            if _bearing_gap_deg(bearing_deg, center_bearing) <= half_width:
                return (r_max * ratio, label)
        return None

    def duration_at(self, bearing_deg: float, radial_m: float) -> float:
        """从中心沿 `bearing_deg` 走 `radial_m` 米的合成耗时（秒）。

        对距离单调、对方向起伏 → 二分能收敛，同时留出「轮次用尽 → 内插」的真实分支。
        """
        return float(radial_m) / (self.speed_mps * self.speed_factor(bearing_deg))

    def path_distance_m(self, bearing_deg: float, radial_m: float) -> float:
        """路径距离（米）：绕行系数加正弦扰动，让 FR-203 的 tooltip 不与直线推点距离相等。"""
        wiggle = 1.0 + 0.06 * math.sin(math.radians(float(bearing_deg)) * 3.0)
        return float(radial_m) * self.detour_gain * wiggle

    # -- oracle 协议 -----------------------------------------------------------
    def probe(self, center: LngLat, probes: Sequence[ProbeRequest]) -> list[ProbeOutcome]:
        """实现 `IsochroneOracle`：同步、零出网、每条请求都回一条（绝不静默丢）。"""
        outcomes: list[ProbeOutcome] = []
        for request in probes:
            bearing = float(request.bearing_deg)
            radial = float(request.distance_m)
            barrier = self.barrier_for(bearing)
            if barrier is not None and radial >= barrier[0]:
                block_at, label = barrier
                outcomes.append(
                    ProbeOutcome(
                        bearing_deg=bearing,
                        distance_m=radial,
                        kind=OutcomeKind.BLOCKED_TIMEOUT,
                        duration_s=None,
                        distance_traveled_m=self.path_distance_m(bearing, min(radial, block_at)),
                        evidence=(
                            f"[synthetic] 示例阻隔带（{label}）：越过约 {block_at:.0f} m 后该方向"
                            "无可行路径。合成数据，非百度返回信号。"
                        ),
                    )
                )
                continue
            outcomes.append(
                ProbeOutcome(
                    bearing_deg=bearing,
                    distance_m=radial,
                    kind=OutcomeKind.OK,
                    duration_s=self.duration_at(bearing, radial),
                    distance_traveled_m=self.path_distance_m(bearing, radial),
                    evidence=None,
                )
            )
        return outcomes

    def compute(self, config: IsochroneConfig | None = None) -> IsochroneResult:
        """直接算一圈（mock 分支与冒烟脚本共用；纯 CPU，调用方可自行决定是否丢线程）。"""
        return compute_isochrone(
            self.center, self, self.target_seconds, mode=self.mode, config=config
        )

    def synthetic_path(
        self, bearing_deg: float, radial_m: float, *, samples: int = 24
    ) -> list[list[float]]:
        """mock 态的示意路径串（FR-203 点击顶点时用）：沿大圆方向等距推进 + 轻微摆动。

        明确标注为示意线，不假装是路网轨迹；不发任何请求、不占配额。
        """
        count = max(4, int(samples))
        radial = max(1.0, float(radial_m))
        points: list[list[float]] = []
        for step in range(count + 1):
            ratio = step / count
            drift = math.sin(ratio * math.pi * 2.0) * 0.045
            point = destination(self.center, float(bearing_deg) + drift * 12.0, radial * ratio)
            points.append([point.lng, point.lat])
        return points


def _bearing_gap_deg(a: float, b: float) -> float:
    """两个方位角之间的劣弧夹度（0~180）。"""
    raw = abs((float(a) - float(b)) % 360.0)
    return raw if raw <= 180.0 else 360.0 - raw


def _angular_offset_deg(theta_rad: float, corridor_rad: float) -> float:
    """两个弧度角之间的带符号夹角绝对值（度）。"""
    delta = theta_rad - corridor_rad
    return abs(math.degrees(math.atan2(math.sin(delta), math.cos(delta))))


# --------------------------------------------------------------------------------------
# 出网 oracle 的线程桥（几何在 worker 线程、HTTP 在原事件循环）
# --------------------------------------------------------------------------------------
class _LiveOracleBridge:
    """实现 `IsochroneOracle` 的同步外观，把每批测时投回事件循环上的 `RouteProbe`。

    为什么不是 `asyncio.gather`：等时圈的探测点彼此不独立（下一轮的二分距离取决于上一轮的耗时），
    能并的只有「一轮之内的多条路线」，而官方把它们打包成 1×≤50 的一次批量算路本来就省掉了
    RTT；再往外并发就变成条款 3.2.3(ii) 禁止的施压形态（PRD D8）。
    """

    def __init__(
        self,
        probe: RouteProbe,
        loop: asyncio.AbstractEventLoop,
        *,
        mode: str = MODE_WALKING,
        timeout_s: float = BRIDGE_TIMEOUT_S,
    ) -> None:
        self._probe = probe
        self._loop = loop
        self.mode = mode
        self.timeout_s = float(timeout_s)
        self.tally = ProbeTally()

    def probe(self, center: LngLat, probes: Sequence[ProbeRequest]) -> list[ProbeOutcome]:
        """`IsochroneOracle` 协议入口（由 worker 线程调用，所以内部是阻塞的）。"""
        self.tally.oracle_calls += 1
        if not probes:
            return []
        # 坐标用 `{"lng":.., "lat":..}` 字典而不是元组：元组在 `(lat,lng)` / `(lng,lat)` 两种
        # 约定下都能过编译，键名没有歧义（与 `services/geo.py` 同一套防偏移口径）。
        origin = {"lng": center.lng, "lat": center.lat}
        points = [
            ProbePoint(
                index=index,
                position={"lng": request.point.lng, "lat": request.point.lat},
                azimuth_deg=request.bearing_deg,
                radial_m=request.distance_m,
                label=f"{request.bearing_deg:.6f}|{round(request.distance_m)}",
            )
            for index, request in enumerate(probes)
        ]
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._probe.probe(origin, points, mode=self.mode), self._loop
            )
            result = future.result(timeout=self.timeout_s)
        except Exception as exc:
            # 桥本身出问题（超时 / 循环已关 / 客户端异常）也不外抛：整批回 MISSING。
            reason = f"live_probe_{type(exc).__name__}"
            self.tally.note_error(f"{reason}: {str(exc)[:160]}")
            log_fields(
                logger,
                logging.WARNING,
                "isochrone_probe_failed",
                mode=self.mode,
                probes=len(probes),
                error=type(exc).__name__,
            )
            return [
                _missing_outcome(request, f"[live] 批量算路未取回结果（{reason}）")
                for request in probes
            ]
        return self._pair(result, probes)

    def _pair(
        self, result: MatrixProbeResult, probes: Sequence[ProbeRequest]
    ) -> list[ProbeOutcome]:
        """逐格结果 → `ProbeOutcome`，配对键只用几何层的 `pair_key`。"""
        self.tally.add(result)
        unmeasured = len(result.missing_cells)
        self.tally.missing += max(
            0,
            unmeasured - len(result.blocked_official_indices) - len(result.blocked_timeout_indices),
        )
        if result.stopped_reason:
            self.tally.note_error(f"budget: {result.stopped_reason}")
        by_key: dict[tuple[float, int], ProbeOutcome] = {}
        for cell in result.outcomes:
            index = int(cell.index)
            if not 0 <= index < len(probes):
                continue
            request = probes[index]
            # `bearing_deg` / `distance_m` 一律取**请求侧**的值，不用回包坐标反推：百度把坐标
            # 截到 6 位小数（≈0.1 m），反推出来的键会与请求差一格，配对就静默错位。
            by_key[pair_key(request.bearing_deg, request.distance_m)] = _cell_outcome(request, cell)
        return [
            by_key.get(request.key())
            or _missing_outcome(request, "[live] 批量算路未回该格（missing_cell）")
            for request in probes
        ]


def _cell_outcome(request: ProbeRequest, cell: Any) -> ProbeOutcome:
    evidence = _cell_evidence(cell)
    duration = getattr(cell, "duration_s", None)
    traveled = getattr(cell, "distance_m", None)
    if getattr(cell, "measured", False):
        return ProbeOutcome(
            bearing_deg=request.bearing_deg,
            distance_m=request.distance_m,
            kind=OutcomeKind.OK,
            duration_s=None if duration is None else float(duration),
            distance_traveled_m=None if traveled is None else float(traveled),
            evidence=evidence,
        )
    kind = getattr(cell, "blocked_kind", None)
    if kind == BLOCKED_OFFICIAL:
        # 🔴 官方硬信号原样透传（status=2001 / restrictions_status=0x01…），本层不改写、
        # 不合并，也绝不用「耗时超标」另判。
        outcome_kind = OutcomeKind.BLOCKED_OFFICIAL
    elif kind == BLOCKED_TIMEOUT:
        outcome_kind = OutcomeKind.BLOCKED_TIMEOUT
    else:
        # 预算截停 / 根本没发出：官方没说话，交给几何层按「没测准」处理，不当成禁行。
        outcome_kind = OutcomeKind.MISSING
    return ProbeOutcome(
        bearing_deg=request.bearing_deg,
        distance_m=request.distance_m,
        kind=outcome_kind,
        duration_s=None if duration is None else float(duration),
        distance_traveled_m=None if traveled is None else float(traveled),
        evidence=evidence,
    )


def _missing_outcome(request: ProbeRequest, evidence: str) -> ProbeOutcome:
    return ProbeOutcome(
        bearing_deg=request.bearing_deg,
        distance_m=request.distance_m,
        kind=OutcomeKind.MISSING,
        evidence=evidence,
    )


def _cell_evidence(cell: Any) -> str | None:
    """blocked 证据文本：把 `CellOutcome.signals` 原样拼起来（几何层只搬运、不解释）。"""
    signals = [str(item) for item in (getattr(cell, "signals", None) or [])]
    status = getattr(cell, "status", None)
    if status is not None and not any(item.startswith("status=") for item in signals):
        signals.append(f"status={status}")
    error = getattr(cell, "error", None)
    if error:
        signals.append(f"error={error}")
    return "; ".join(signals) or None


# --------------------------------------------------------------------------------------
# 编排层
# --------------------------------------------------------------------------------------
class IsochroneService:
    """等时圈的业务流程：校验 → 三层降级 → 出契约字段。拼百度参数与算缓存键都在 `infra/`。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        cache: CacheStore | None = None,
        probe: RouteProbe | None = None,
        config: IsochroneConfig | None = None,
        mode_count: int = 2,
        bridge_timeout_s: float = BRIDGE_TIMEOUT_S,
        polyline_call_limit: int = POLYLINE_CALL_LIMIT,
    ) -> None:
        """依赖全部可注入：自测传一个只实现 `route_matrix` / `direction` / `route_budget_state`
        的假对象即可做到 0 次真实出网（条款 3.2.3(ii)）；真实链路对照测用
        `BaiduClient(settings, transport=httpx.MockTransport(...))`。

        🔴 `mode_count=2` 是默认值而不是疏忽：`services/route_probe.py` 的接线警告写明双模式
        （walking+riding）必须以 `BaiduClient(..., mode_count=2)` 构造，否则本地预算按单模式
        折算，FR-206 的第二轮（骑行圈）会被预算守卫在中途误截。

        注入了 `client` 时**不碰** `CacheStore`：单例缓存由 `BaiduClient` 自己接，测试里
        不该顺带打开一个 SQLite 文件（也不新建第二份，见模块注释第 3 条）。
        """
        cfg = settings or get_settings()
        self.settings = cfg
        self.config = config or IsochroneConfig()
        self.bridge_timeout_s = float(bridge_timeout_s)
        self.polyline_call_limit = max(0, int(polyline_call_limit))
        self.mode_count = max(1, int(mode_count))
        self._injected_probe = probe
        self._sessions: OrderedDict[tuple[Any, ...], RouteProbe] = OrderedDict()
        self._mock_polyline_calls: dict[tuple[Any, ...], int] = {}
        # 示例层被 12 次上限挡下来的次数（`usage.polyline_rejected` 的 mock 侧账本）。
        self._mock_polyline_rejections: dict[tuple[Any, ...], int] = {}
        self._lock = threading.Lock()

        self.cache: CacheStore | None = cache
        if client is not None:
            self.client: Any = client
            if getattr(self.client, "cache", None) is None and cache is not None:
                self.client.cache = cache
        else:
            if self.cache is None:
                self.cache = get_cache_store(cfg)
            self.client = BaiduClient(cfg, cache=self.cache, mode_count=self.mode_count)
        if getattr(self.client, "cache", None) is None and self.cache is not None:
            # 缓存只允许一套口径：注入的客户端若没接缓存，实时结果不落库，表现是
            # 「命中率 0 但没人报错」。与 `GeoService.__init__` 同一刀。
            self.client.cache = self.cache

    # ------------------------------------------------------------------ 对外方法
    async def compute(
        self,
        lnglat: Any,
        *,
        mode: str = MODE_WALKING,
        minutes: float = DEFAULT_MINUTES,
        data_mode: DataMode | None = None,
        config: IsochroneConfig | None = None,
        target_seconds: float | None = None,
    ) -> IsochroneOutcome:
        """算一个等时圈（FR-201）。取数失败一律落回降级链并带可读文案，不抛 5xx（FR-403）。"""
        started = perf_counter()
        use_mode = self._require_mode(mode)
        use_minutes = self._require_minutes(minutes)
        seconds = self._require_seconds(use_minutes, target_seconds)
        center = self._coerce_center(lnglat)
        cfg = config or self.config
        layer = self._resolve_mode(data_mode)

        if layer == "mock":
            result, progress = await self._run_synthetic(center, use_mode, seconds, cfg)
            return self._outcome(
                result,
                ProbeTally(),
                progress,
                elapsed_ms=_elapsed(started),
                data_mode=layer,
                data_source="sample",
                degraded=True,
                degraded_reason="mock_mode",
                message=MSG_MOCK_MODE,
                center=center,
                mode=use_mode,
                minutes=use_minutes,
                seconds=seconds,
            )

        tally = ProbeTally()
        progress: list[dict[str, Any]] = []
        try:
            result = await self._run_live(center, use_mode, seconds, cfg, tally, progress)
        except CircleDocError as exc:
            log_fields(
                logger, logging.WARNING, "isochrone_live_rejected", mode=use_mode, code=exc.error_id
            )
            return await self._degrade(
                center,
                use_mode,
                seconds,
                cfg,
                tally,
                started=started,
                layer=layer,
                reason=exc.error_id,
                message=exc.user_message,
                error=exc.to_payload(),
            )
        except Exception as exc:
            log_fields(
                logger,
                logging.ERROR,
                "isochrone_live_failed",
                mode=use_mode,
                error=type(exc).__name__,
            )
            tally.note_error(f"{type(exc).__name__}: {str(exc)[:160]}")
            return await self._degrade(
                center,
                use_mode,
                seconds,
                cfg,
                tally,
                started=started,
                layer=layer,
                reason="live_failed",
                message=MSG_LIVE_FAILED,
            )

        if tally.measured == 0:
            # 一次实测都没拿到（额度耗尽 / 服务未开通 / 整批超时）：不能给用户一个「看着像圈」
            # 的东西冒充实时结果，落最内层合成数据——但**已经花掉的账要留着**。
            return await self._degrade(
                center,
                use_mode,
                seconds,
                cfg,
                tally,
                started=started,
                layer=layer,
                reason="budget_stopped" if tally.budget_stops else "no_live_data",
                message=MSG_NO_LIVE_DATA,
            )

        source: DataSource = "cache" if (tally.http_requests == 0 and tally.cache_hits) else "live"
        degraded = False
        reason: str | None = None
        message: str | None = None
        if tally.stopped_reason:
            degraded, reason = True, "budget_stopped"
            message = f"{MSG_BUDGET_STOP} {tally.stopped_reason}"
        elif tally.blocked_official or (tally.blocked_timeout + tally.missing):
            degraded, reason, message = True, "partial_no_data", MSG_PARTIAL
        if source == "cache" and not degraded:
            message = MSG_CACHE_HIT

        log_fields(
            logger,
            logging.INFO,
            "isochrone_computed",
            mode=use_mode,
            layer=source,
            vertices=len(result.ring),
            http_requests=tally.http_requests,
            quota_units=tally.quota_units,
        )
        return self._outcome(
            result,
            tally,
            progress,
            elapsed_ms=_elapsed(started),
            data_mode=layer,
            data_source=source,
            degraded=degraded,
            degraded_reason=reason,
            message=message,
            center=center,
            mode=use_mode,
            minutes=use_minutes,
            seconds=seconds,
        )

    async def polyline(
        self,
        lnglat: Any,
        *,
        mode: str = MODE_WALKING,
        minutes: float = DEFAULT_MINUTES,
        bearing: float | None = None,
        distance: float | None = None,
        point: Any = None,
        data_mode: DataMode | None = None,
        lite: bool = False,
    ) -> PolylineOutcome:
        """FR-203/D8：点顶点才补调 direction 取路径串，单次分析硬上限 12 次。"""
        started = perf_counter()
        use_mode = self._require_mode(mode)
        use_minutes = self._require_minutes(minutes)
        seconds = self._require_seconds(use_minutes, None)
        center = self._coerce_center(lnglat)
        vertex_point, vertex_bearing, vertex_radial = self._coerce_vertex(
            center, point=point, bearing=bearing, distance=distance
        )
        layer = self._resolve_mode(data_mode)
        payload: dict[str, Any] = {
            "bearing": round(vertex_bearing, 2),
            "distance_m": round(vertex_radial, 1),
            "lnglat": [vertex_point.lng, vertex_point.lat],
        }

        if layer == "mock":
            calls = self._consume_mock_polyline_slot(center, use_mode, seconds)
            if calls is None:
                # 超限：示例层也一样回可读结果对象（`allowed=false`），不抛异常也不静默截数据。
                return PolylineOutcome(
                    ok=False,
                    allowed=False,
                    data_mode=layer,
                    data_source="sample",
                    degraded=True,
                    message=MSG_POLYLINE_LIMIT.format(limit=self.polyline_call_limit),
                    center=center.as_tuple(),
                    mode=use_mode,
                    minutes=use_minutes,
                    vertex=payload,
                    path=None,
                    duration_s=None,
                    distance_m=None,
                    blocked=False,
                    blocked_kind=None,
                    signals=["polyline_call_limit"],
                    status=None,
                    calls={
                        "used": self.polyline_call_limit,
                        "limit": self.polyline_call_limit,
                        "remaining": 0,
                    },
                    usage=ProbeTally().usage(
                        elapsed_ms=_elapsed(started),
                        extra=self._polyline_quota_extra(center, use_mode, seconds),
                    ),
                )
            oracle = SyntheticOracle(center, mode=use_mode, target_seconds=seconds)
            return PolylineOutcome(
                ok=True,
                allowed=True,
                data_mode=layer,
                data_source="sample",
                degraded=True,
                message=MSG_POLYLINE_SAMPLE,
                center=center.as_tuple(),
                mode=use_mode,
                minutes=use_minutes,
                vertex=payload,
                path={
                    "type": "LineString",
                    "coordinates": oracle.synthetic_path(vertex_bearing, vertex_radial),
                },
                duration_s=round(oracle.duration_at(vertex_bearing, vertex_radial), 1),
                distance_m=round(oracle.path_distance_m(vertex_bearing, vertex_radial), 1),
                blocked=False,
                blocked_kind=None,
                signals=["synthetic"],
                status=None,
                calls=calls,
                usage=ProbeTally().usage(
                    elapsed_ms=_elapsed(started),
                    extra=self._polyline_quota_extra(center, use_mode, seconds),
                ),
            )

        probe = self._probe_for(center, use_mode, seconds)
        origin = {"lng": center.lng, "lat": center.lat}
        target = {"lng": vertex_point.lng, "lat": vertex_point.lat}
        tally = ProbeTally()
        try:
            result = await probe.polyline(origin, target, mode=use_mode, lite=lite)
        except CircleDocError as exc:
            return self._polyline_failure(
                layer,
                center,
                use_mode,
                use_minutes,
                payload,
                started,
                tally,
                message=exc.user_message,
                reason=exc.error_id,
                error=exc.to_payload(),
            )
        except Exception as exc:
            tally.note_error(f"{type(exc).__name__}: {str(exc)[:160]}")
            return self._polyline_failure(
                layer,
                center,
                use_mode,
                use_minutes,
                payload,
                started,
                tally,
                message=MSG_POLYLINE_UNAVAILABLE,
                reason="polyline_failed",
                error=None,
            )

        tally.http_requests += int(result.http_requests)
        tally.quota_units += int(result.quota_units)
        tally.place_calls += int(result.place_calls)
        tally.route_lines_by_mode[result.mode or use_mode] = int(result.route_lines)
        tally.cache_hits = 1 if result.from_cache else 0
        calls = self._calls_for(center, use_mode, seconds)

        if not result.allowed:
            return PolylineOutcome(
                ok=False,
                allowed=False,
                data_mode=layer,
                data_source="live",
                degraded=True,
                message=MSG_POLYLINE_LIMIT.format(limit=self.polyline_call_limit),
                center=center.as_tuple(),
                mode=use_mode,
                minutes=use_minutes,
                vertex=payload,
                path=None,
                duration_s=result.duration_s,
                distance_m=result.distance_m,
                blocked=False,
                blocked_kind=None,
                signals=list(result.signals),
                status=result.status,
                calls=calls,
                usage=tally.usage(
                    elapsed_ms=_elapsed(started),
                    extra=self._polyline_quota_extra(center, use_mode, seconds),
                ),
            )
        coords = _path_coordinates(result.path_strings)
        return PolylineOutcome(
            ok=bool(result.ok),
            allowed=True,
            data_mode=layer,
            data_source="cache" if result.from_cache else "live",
            degraded=not bool(result.ok),
            message=None if result.ok else MSG_POLYLINE_UNAVAILABLE,
            center=center.as_tuple(),
            mode=use_mode,
            minutes=use_minutes,
            vertex=payload,
            path={"type": "LineString", "coordinates": coords} if coords else None,
            duration_s=result.duration_s,
            distance_m=result.distance_m,
            blocked=bool(result.blocked),
            blocked_kind=result.blocked_kind,
            signals=list(result.signals),
            status=result.status,
            calls=calls,
            usage=tally.usage(
                elapsed_ms=_elapsed(started),
                extra=self._polyline_quota_extra(center, use_mode, seconds),
            ),
        )

    async def probe_points(
        self, lnglat: Any, points: Sequence[Any], *, mode: str = MODE_WALKING
    ) -> MatrixProbeResult:
        """一批任意点的批量测时（M3 的 FR-212「POI 实测步行时间」直接用，不必再包一层）。

        ≤50 切分、缺格不抛异常、三栏记账都是 `RouteProbe` 的既有契约，这里只透传。
        """
        use_mode = self._require_mode(mode)
        center = self._coerce_center(lnglat)
        probe = self._probe_for(center, use_mode, 0.0)
        coerced = [_as_position(item) for item in (points or ())]
        return await probe.probe({"lng": center.lng, "lat": center.lat}, coerced, mode=use_mode)

    def budget_state(self, mode: str | None = None, *, next_lines: int = 0) -> dict[str, Any]:
        """出网前的预算预检（FR-206 硬事实①：跑第二轮前要在 UI 上显式提示还剩多少）。"""
        state = getattr(self.client, "route_budget_state", None)
        if not callable(state):
            return {}
        return dict(cast(Any, state)(mode, next_lines=int(next_lines)))

    def usage(self) -> dict[str, Any]:
        """进程级三栏快照（客户端累加器）。请求级口径在响应的 `usage` 里，两处不要混用。"""
        snapshot = getattr(self.client, "usage", None)
        if callable(snapshot):
            return dict(cast(Any, snapshot()))
        return {}

    # ------------------------------------------------------------------ 三层链
    async def _run_live(
        self,
        center: LngLat,
        mode: str,
        target_seconds: float,
        cfg: IsochroneConfig,
        tally: ProbeTally,
        progress: list[dict[str, Any]],
    ) -> IsochroneResult:
        """同步几何跑在 worker 线程，出网投回当前事件循环，**串行**等待每一批。"""
        probe = self._probe_for(center, mode, target_seconds)
        loop = asyncio.get_running_loop()
        bridge = _LiveOracleBridge(probe, loop, mode=mode, timeout_s=self.bridge_timeout_s)

        def on_round(item: RoundProgress) -> None:
            progress.append(dataclasses.asdict(item))

        try:
            return await asyncio.to_thread(
                compute_isochrone,
                center,
                bridge,
                target_seconds,
                mode=mode,
                config=cfg,
                progress=on_round,
            )
        finally:
            tally.merge(bridge.tally)

    async def _run_synthetic(
        self, center: LngLat, mode: str, target_seconds: float, cfg: IsochroneConfig
    ) -> tuple[IsochroneResult, list[dict[str, Any]]]:
        """最内层：零出网的合成圈。多边形由**同一套几何代码**算，不是手写坐标。

        仍然丢进 `to_thread`：合成也是 CPU 活（12~36 顶点二分 + 圈内 250 m 网格 IDW），
        不该让事件循环替它排队（FR-205「长任务不出现无响应白屏」）。
        """
        oracle = SyntheticOracle(center, mode=mode, target_seconds=target_seconds)
        progress: list[dict[str, Any]] = []

        def on_round(item: RoundProgress) -> None:
            progress.append(dataclasses.asdict(item))

        result = await asyncio.to_thread(
            compute_isochrone,
            center,
            oracle,
            target_seconds,
            mode=mode,
            config=cfg,
            progress=on_round,
        )
        return result, progress

    async def _degrade(
        self,
        center: LngLat,
        mode: str,
        seconds: float,
        cfg: IsochroneConfig,
        tally: ProbeTally,
        *,
        started: float,
        layer: EffectiveDataMode,
        reason: str,
        message: str,
        error: Mapping[str, Any] | None = None,
    ) -> IsochroneOutcome:
        """实时层不可用 → 合成层。返回 200 + 可读文案（FR-403 / FR-404），绝不冒 500。"""
        result, progress = await self._run_synthetic(center, mode, seconds, cfg)
        return self._outcome(
            result,
            tally,
            progress,
            elapsed_ms=_elapsed(started),
            data_mode=layer,
            data_source="sample",
            degraded=True,
            degraded_reason=reason,
            message=message,
            center=center,
            mode=mode,
            minutes=seconds / 60.0,
            seconds=seconds,
            error=error,
        )

    # ------------------------------------------------------------------ 组装
    def _outcome(
        self,
        result: IsochroneResult,
        tally: ProbeTally,
        progress: Sequence[dict[str, Any]],
        *,
        elapsed_ms: int,
        data_mode: EffectiveDataMode,
        data_source: DataSource,
        degraded: bool,
        degraded_reason: str | None,
        message: str | None,
        center: LngLat,
        mode: str,
        minutes: float,
        seconds: float,
        error: Mapping[str, Any] | None = None,
    ) -> IsochroneOutcome:
        return IsochroneOutcome(
            ok=True,
            data_mode=data_mode,
            data_source=data_source,
            degraded=degraded,
            degraded_reason=degraded_reason,
            message=message,
            center=center.as_tuple(),
            mode=mode,
            minutes=float(minutes),
            target_seconds=float(seconds),
            polygon=dict(result.to_geojson().get("geometry") or {}),
            area_km2=round(float(result.area_km2), 4),
            vertices=[_vertex_payload(item) for item in result.ring],
            duration_field=_duration_field(result),
            progress=[dict(item) for item in progress],
            usage=tally.usage(
                elapsed_ms=elapsed_ms,
                extra={
                    "oracle_calls": tally.oracle_calls,
                    "measured_cells": tally.measured,
                    "blocked_official_cells": tally.blocked_official,
                    "blocked_timeout_cells": tally.blocked_timeout,
                    # 🔴 [BUG][M2]：FR-203/FR-205 的懒加载额度（用了/上限/被拒）按会话键现取，
                    # 三个键名与 `RouteProbe.usage()` 一致；`batches_sent` / `budget_stops`
                    # 由 `ProbeTally.usage()` 自己出口。
                    **self._polyline_quota_extra(center, mode, seconds),
                },
            ),
            stats=dict(result.stats.as_dict()),
            error=dict(error) if error is not None else None,
        )

    def _calls_for(self, center: LngLat, mode: str, seconds: float) -> dict[str, int]:
        """本分析的 polyline 懒加载额度（`limit=12`，PRD D8：一次分析最多补调 12 次）。"""
        used = self._polyline_calls_used(center, mode, seconds)
        limit = self.polyline_call_limit
        return {"used": min(used, limit), "limit": limit, "remaining": max(0, limit - used)}

    def _polyline_quota_extra(self, center: LngLat, mode: str, seconds: float) -> dict[str, int]:
        """FR-203/FR-205 的懒加载额度出口：把会话探测器里的计数搬进响应 `usage`。

        🔴 键名与 `services/route_probe.py:RouteProbe.usage()` **逐字一致**
        （`polyline_calls` / `polyline_limit` / `polyline_rejected`），本层不造第三套命名：
        同一个数在 `infra/metrics`、`RouteProbe`、服务层三处已经各有一套叫法了。

        为什么按会话键**现取**而不是在 `ProbeTally` 里累加：12 次上限的语义是「一次分析 12 次」，
        而每次点击顶点是一个独立 HTTP 请求（见 `SESSIONS_MAX` 注释）。计数器若在请求级累加，
        每次点击各自从 0 起算、上限形同虚设；按会话键回读才是「本次分析一共用了多少」。
        示例层（mock）没有 `RouteProbe` 会话，但同样吃这条硬上限，所以取 mock 计数器。

        ⚠️ 口径：`usage` 其余字段是**请求级**（这一次 HTTP 花掉多少），这三个 `polyline_*` 是
        **会话级**（这一次分析累计用到第几下）。不是自相矛盾——额度本来就是按分析发的，
        与同响应里的 `calls.{used,limit,remaining}` 同源同值，两处读出来必然一样。
        """
        key = _session_key(center, mode, seconds)
        with self._lock:
            probe = self._sessions.get(key) or self._injected_probe
            snapshot = probe.usage() if probe is not None else None
            mock_used = int(self._mock_polyline_calls.get(key, 0))
            mock_rejected = int(self._mock_polyline_rejections.get(key, 0))
        if snapshot is None:
            return {
                "polyline_calls": min(mock_used, self.polyline_call_limit),
                "polyline_limit": int(self.polyline_call_limit),
                "polyline_rejected": int(mock_rejected),
            }
        limit = snapshot.get("polyline_limit")
        return {
            "polyline_calls": int(snapshot.get("polyline_calls", 0) or 0),
            "polyline_limit": self.polyline_call_limit if limit is None else int(limit),
            "polyline_rejected": int(snapshot.get("polyline_rejected", 0) or 0),
        }

    def _polyline_failure(
        self,
        layer: EffectiveDataMode,
        center: LngLat,
        mode: str,
        minutes: float,
        payload: dict[str, Any],
        started: float,
        tally: ProbeTally,
        *,
        message: str,
        reason: str,
        error: Mapping[str, Any] | None,
    ) -> PolylineOutcome:
        """补调失败（鉴权 / 额度 / 客户端异常）也回结果对象：200 + 可读文案，不冒 500。"""
        return PolylineOutcome(
            ok=False,
            allowed=True,
            data_mode=layer,
            data_source="live",
            degraded=True,
            message=f"{message}（{reason}）",
            center=center.as_tuple(),
            mode=mode,
            minutes=minutes,
            vertex=payload,
            path=None,
            duration_s=None,
            distance_m=None,
            blocked=False,
            blocked_kind=None,
            signals=[reason],
            status=None,
            calls=self._calls_for(center, mode, minutes * 60.0),
            usage=tally.usage(
                elapsed_ms=_elapsed(started),
                extra=self._polyline_quota_extra(center, mode, minutes * 60.0),
            ),
            error=dict(error) if error is not None else None,
        )

    # ------------------------------------------------------------------ 入口校验
    @staticmethod
    def _require_mode(value: Any) -> str:
        raw = (value if isinstance(value, str) else MODE_WALKING).strip().lower()
        if raw not in ALLOWED_MODES:
            raise InvalidIsochroneParam(
                f"出行方式只支持 walking（步行）与 riding（骑行），收到 {value!r}。",
                field_name="mode",
                value=value,
                allowed=list(ALLOWED_MODES),
            )
        return raw

    @staticmethod
    def _require_minutes(value: Any) -> float:
        settings = get_settings()
        low = max(MINUTES_HARD_MIN, float(getattr(settings, "min_minutes", MINUTES_HARD_MIN)))
        high = min(MINUTES_HARD_MAX, float(getattr(settings, "max_minutes", MINUTES_HARD_MAX)))
        if isinstance(value, bool) or value is None:
            raise InvalidIsochroneParam(
                "目标时长必须是数字（分钟），例如 15。", field_name="minutes", value=value
            )
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InvalidIsochroneParam(
                "目标时长必须是数字（分钟），例如 15。", field_name="minutes", value=value
            ) from exc
        if not math.isfinite(number) or number < low or number > high:
            raise InvalidIsochroneParam(
                f"目标时长应在 {low:g}~{high:g} 分钟之间（FR-104 的档位是 5/10/15/30）。",
                field_name="minutes",
                value=value,
                min=low,
                max=high,
            )
        return number

    @staticmethod
    def _require_seconds(minutes: float, override: float | None) -> float:
        seconds = minutes * 60.0 if override is None else float(override)
        if not math.isfinite(seconds) or seconds <= 0.0:
            raise InvalidIsochroneParam(
                "目标时长换算成的秒数必须是正数。", field_name="minutes", value=seconds
            )
        return seconds

    def _coerce_center(self, value: Any) -> LngLat:
        """`"经度,纬度"` / `[lng, lat]` / `{"lng":..,"lat":..}` → `LngLat`，并挡境外。"""
        lng, lat = _coerce_pair(value)
        self._require_domestic(lng, lat, source="request")
        return LngLat(lng, lat)

    def _coerce_vertex(
        self, center: LngLat, *, point: Any, bearing: float | None, distance: float | None
    ) -> tuple[LngLat, float, float]:
        """顶点位置：优先用显式 `point`，否则按「方位角 + 推点距离」球面正算回去。"""
        if point is not None:
            lng, lat = _coerce_pair(point)
            self._require_domestic(lng, lat, source="vertex")
            resolved = LngLat(lng, lat)
            angle = center_bearing(center, resolved) if bearing is None else float(bearing) % 360.0
            return resolved, angle, max(1.0, straight_distance(center, resolved))
        if bearing is None or distance is None:
            raise InvalidIsochroneParam(
                "请提供顶点的 point（经度,纬度），或 bearing + distance_m 两个字段。",
                field_name="point",
            )
        angle = float(bearing) % 360.0
        radial = float(distance)
        if not math.isfinite(angle) or not (1.0 <= radial <= 20_000.0):
            raise InvalidIsochroneParam(
                "distance_m 应在 1~20000 米之间（等时圈顶点的推点半径不会明显超过 R_max）。",
                field_name="distance_m",
                value=distance,
            )
        resolved = destination(center, angle, radial)
        self._require_domestic(resolved.lng, resolved.lat, source="vertex")
        return resolved, angle, radial

    def _require_domestic(self, lng: float, lat: float, *, source: str) -> None:
        """境外坐标 400 + 可读理由（个人认证开发者对境外各服务无配额，笔记 §3.1）。

        等时圈是**最烧配额**的接口，境外点发出去只会收到鉴权错误并白烧当日路线配额，
        所以判定放在出网之前，并复用 `services/geo.py` 的 `OutsideServiceArea` 语义。
        """
        cfg = self.settings
        if not (
            cfg.china_lng_min <= lng <= cfg.china_lng_max
            and cfg.china_lat_min <= lat <= cfg.china_lat_max
        ):
            raise OutsideServiceArea(lnglat=(lng, lat), source=f"isochrone_{source}")

    def _resolve_mode(self, requested: DataMode | None) -> EffectiveDataMode:
        """请求级 `data_mode` 覆盖运行期解析值（FR-105 的开关在 UI 上，不在 `.env` 里）。"""
        if requested in ("live", "cache", "mock"):
            return cast(EffectiveDataMode, requested)
        return self.settings.data_mode_in_effect

    # ------------------------------------------------------------------ 会话（RouteProbe）
    def _probe_for(self, center: LngLat | None, mode: str, target_seconds: float) -> RouteProbe:
        """按「中心点 + mode + 时长」取会话级 `RouteProbe`（键的口径见 `SESSIONS_MAX` 注释）。"""
        key = _session_key(center, mode, target_seconds)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                self._sessions.move_to_end(key)
                return existing
            if self._injected_probe is not None:
                # 注入的探测器用于自测：直接复用，保证假 client 的调用可被断言。
                return self._injected_probe
            probe = self._new_probe(mode)
            self._sessions[key] = probe
            self._sessions.move_to_end(key)
            while len(self._sessions) > SESSIONS_MAX:
                self._sessions.popitem(last=False)
            return probe

    def _new_probe(self, mode: str) -> RouteProbe:
        """会话探测器：批大小与几何层的 `chunk_size` 对齐（都是 ≤50，谁先切都行，双保险）。

        🔴 `road_prefer=3` 只在 riding 下发（FR-206 的「无逆行且无阶梯」质量开关）；留空的
        官方未列组合一律不发。
        """
        return RouteProbe(
            self.client,
            mode=mode,
            batch_size=self.config.chunk_size,
            polyline_call_limit=self.polyline_call_limit,
            road_prefer=3 if mode == MODE_RIDING else None,
        )

    def _polyline_calls_used(self, center: LngLat, mode: str, target_seconds: float) -> int:
        with self._lock:
            probe = self._sessions.get(_session_key(center, mode, target_seconds))
            if probe is not None:
                return max(0, probe.polyline_call_limit - probe.remaining_polyline_calls)
            return int(self._mock_polyline_calls.get(_session_key(center, mode, target_seconds), 0))

    def _consume_mock_polyline_slot(
        self, center: LngLat, mode: str, target_seconds: float
    ) -> dict[str, int] | None:
        """示例层也吃 12 次硬上限：额度是按「一次分析」算的，与出不出网无关。

        为什么要一视同仁：上限的真实目的是挡住失控的前端循环（D8 的原话是「逐方向调用会烧
        QPS」），mock 态不烧配额但照样能在一秒里点满几百次；两边同一套语义，前端才不需要
        为两种模式写两份「还能点几下」的判断。
        """
        key = _session_key(center, mode, target_seconds)
        with self._lock:
            used = int(self._mock_polyline_calls.get(key, 0)) + 1
            if used > self.polyline_call_limit:
                # 拒一次也记账：`usage.polyline_rejected` 在 mock 层也要有出口，
                # 否则前端计量表在离线演示时显示「被拒 0 次」，与真实链路两套口径。
                self._mock_polyline_rejections[key] = (
                    int(self._mock_polyline_rejections.get(key, 0)) + 1
                )
                while len(self._mock_polyline_rejections) > SESSIONS_MAX:
                    self._mock_polyline_rejections.pop(next(iter(self._mock_polyline_rejections)))
                return None
            self._mock_polyline_calls[key] = used
            while len(self._mock_polyline_calls) > SESSIONS_MAX:
                self._mock_polyline_calls.pop(next(iter(self._mock_polyline_calls)))
        return {
            "used": used,
            "limit": self.polyline_call_limit,
            "remaining": max(0, self.polyline_call_limit - used),
        }


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _elapsed(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def center_bearing(center: LngLat, point: LngLat) -> float:
    """中心点 → 顶点的方位角（透传几何层，本层不做球面数学）。"""
    return initial_bearing(center, point)


def straight_distance(center: LngLat, point: LngLat) -> float:
    return distance_m(center, point)


def _session_key(center: LngLat | None, mode: str, target_seconds: float) -> tuple[Any, ...]:
    if center is None:
        return ("", mode, round(float(target_seconds)))
    return (round(center.lng, 5), round(center.lat, 5), mode, round(float(target_seconds)))


def _as_position(value: Any) -> Any:
    """把外部传进来的点统一成 `{"lng":.., "lat":..}`（元组按 `(lng, lat)` 解释）。"""
    lng, lat = _coerce_pair(value)
    return {"lng": lng, "lat": lat}


def _coerce_pair(value: Any) -> tuple[float, float]:
    """`(lng, lat)` 归一：字符串走 `services.geo.parse_lnglat`，其余按经度在前解释。"""
    if isinstance(value, LngLat):
        return (value.lng, value.lat)
    if isinstance(value, Mapping):
        missing = [key for key in ("lng", "lat") if value.get(key) is None]
        if missing:
            raise InvalidIsochroneParam(
                f"坐标缺少字段 {missing}，需要 {{'lng':…, 'lat':…}}。",
                field_name="lnglat",
                value=repr(value),
            )
        lng, lat = float(value["lng"]), float(value["lat"])
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        lng, lat = float(value[0]), float(value[1])
    else:
        lng, lat = parse_lnglat(str(value))
    if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
        raise InvalidIsochroneParam(
            "坐标超出合法区间（经度 -180~180、纬度 -90~90）。",
            field_name="lnglat",
            lnglat=[lng, lat],
        )
    return (lng, lat)


def _vertex_payload(vertex: Vertex) -> dict[str, Any]:
    """顶点 → 契约字段（`bearing/minutes/distance_m/sample/confidence/lnglat` + 遇阻证据）。"""
    data: dict[str, Any] = {
        "bearing": round(float(vertex.bearing_deg), 2),
        "minutes": None if vertex.duration_s is None else round(vertex.duration_s / 60.0, 2),
        "distance_m": round(float(vertex.distance_m), 1),
        "sample": vertex.kind.value,
        "sample_label": vertex.kind.label_cn,
        "confidence": round(float(vertex.confidence), 3),
        "lnglat": [vertex.point.lng, vertex.point.lat],
        "blocked": bool(vertex.kind.is_blocked),
        "evidence": vertex.evidence,
    }
    if vertex.traveled_m is not None:
        data["path_distance_m"] = round(float(vertex.traveled_m), 1)
    if vertex.blocked_above_m is not None:
        data["blocked_above_m"] = round(float(vertex.blocked_above_m), 1)
    return data


def _duration_field(result: IsochroneResult) -> dict[str, Any]:
    """耗时场 → 契约字段（FR-204 热力层）。格点值全是 IDW 估的，标签只区分「源点是不是实测」。"""
    field_obj = result.duration_field
    if field_obj is None:
        return {"step_m": 0.0, "cells": []}
    anchors = [item for item in result.vertices if item.kind.is_measured_family]
    cells: list[dict[str, Any]] = []
    for cell in field_obj.cells:
        nearest = min(
            (distance_m(cell.point, item.point) for item in anchors),
            default=float("inf"),
        )
        cells.append(
            {
                "lnglat": [cell.point.lng, cell.point.lat],
                "minutes": round(cell.duration_s / 60.0, 2),
                "duration_s": round(cell.duration_s, 1),
                "sample": "measured" if nearest <= field_obj.step_m * 0.5 else "interpolated",
                "sources": cell.source_count,
                "source_kinds": list(cell.source_kinds),
            }
        )
    return {"step_m": round(float(field_obj.step_m), 1), "cells": cells}


def _path_coordinates(path_strings: Sequence[str]) -> list[list[float]]:
    """官方 `steps[].path` 的「经度,纬度;经度,纬度」串 → GeoJSON 坐标数组。

    ⚠️ direction 的 `location` 是 `{lng,lat}`、`steps[].path` 却是**经度在前**的字符串
    （笔记 §6.2 第 3 条）；这里只做拆分与转数，不做任何坐标系转换。
    """
    coords: list[list[float]] = []
    for text in path_strings or ():
        for chunk in str(text).split(";"):
            parts = [item.strip() for item in chunk.split(",") if item.strip()]
            if len(parts) != 2:
                continue
            try:
                coords.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return coords


# --------------------------------------------------------------------------------------
# 进程级单例
# --------------------------------------------------------------------------------------
_default_lock = threading.Lock()
_default_service: IsochroneService | None = None


def get_isochrone_service(settings: Settings | None = None) -> IsochroneService:
    """进程级 `IsochroneService`（默认依赖：真实 `BaiduClient` + 共享 `CacheStore`）。

    与 `get_geo_service` 同理由：缓存与限流状态必须跨请求存活，每请求新建会让 FR-402 的
    命中率验收无从谈起。测试用 `app.state.isochrone_service` 注假对象，不会走到这里。
    """
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = IsochroneService(settings)
        return _default_service


def reset_isochrone_service(settings: Settings | None = None) -> IsochroneService:
    """丢掉单例重建：换 `CACHE_DB_PATH` 或 `reload_settings()` 之后用。"""
    global _default_service
    with _default_lock:
        _default_service = IsochroneService(settings)
        return _default_service
