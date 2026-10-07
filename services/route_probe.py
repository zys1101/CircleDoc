"""services/route_probe.py —— M2 等时圈的「批量测时 + 路径串懒加载 + 三栏计量」出网层。

职责边界（PRD §4.2 的 Step1~3 出网部分 + Step5 懒加载）：

① 给一批探测点，用 `BaiduClient.route_matrix` 做 **1 origin × N destinations** 的批量测时；
   N 按官方 `origins × destinations ≤ 50` 约束切分成多批（笔记 §2.8），再按输入顺序合并结果。
② 三栏分开记账并对外暴露 `usage()`：`quota_units` 是**配额口径**（`起 × 终` 折算，96 条路线
   就是 96），`http_requests` 是**往返口径**（切分后 5~8 次），`place_calls` 本层恒为 0。
   PRD §4.2 第 267 行明确：只报后者会把配额消耗低估一个数量级，所以两个数字都必须可取，
   且按 mode 分栏（walking / riding 各自计数，FR-206 硬事实① 双模式即当日翻倍）。
③ `blocked` 只按**官方硬信号**归类，绝不用「耗时超标」推断：
   `status=2001`（无骑行路线）、`restrictions_status=0x01/0x02`（骑行禁行/逆行）、
   `links[].attr=0x01`（步行禁行）、`directionlite status=7`（官方明说无返回结果）。
   这些进 `blocked_official`；「空结果 / 缺格 / 网络不可达 / 超时」进 `blocked_timeout`。
   两个标签在几何层的语义与用户话术完全不同（"路走不通" vs "我没测准"，PRD §4.2 精度取舍）。
④ FR-203 的 polyline 懒加载：RouteMatrix **不返回 polyline**，所以只在用户点击顶点时补调
   `direction/v2/{walking|riding}`，**硬性单次上限 12 次**，超限返回结果对象而不是继续打。
⑤ 日预算守卫：出网前用 `client.route_budget_state(mode=..., next_lines=...)` 判断，不足时
   **显式截停并把原因回传**（供降级文案），不静默丢数据；个人档 5,000 条/日，且骑行与步行
   **共用同一份配额**（`route_budget_state` 的 `remaining` 本来就是跨模式总量，正合口径）。
⑥ 缺格与部分完成：一律**返回结果对象、不抛异常**——这是 `route_matrix` 既有契约（额度打满
   与不可达也返回 `partial=True + missing_cells()`），本层照它做（FR-403 任一层失败不中断）。

本层**不做**的事：扇形采样 / 二分收敛 / 顶点排序 / DP / IDW 都在 `services/isochrone_geometry.py`；
编排与 FastAPI 路由在 `services/isochrone_service.py`。也**不自己 sleep、不自己发并发请求**：
3 QPS 与令牌桶、熔断是 `BaiduClient` 的职责（D8：有界线程池并发 `direction` 正是条款 3.2.3(ii)
禁止的「对线上施压」形态，所以这里只顺序 `await` 官方设计的 1×≤50 批量请求）。
"""

# =============================================================================
# 公共 API（供 services/isochrone_service.py 与等时圈几何层的 oracle Protocol 对接）
# -----------------------------------------------------------------------------
# 常量
#   BLOCKED_OFFICIAL = "blocked_official"   BLOCKED_TIMEOUT = "blocked_timeout"
#   MODE_WALKING / MODE_RIDING / MATRIX_ROW_CAP = 50 / POLYLINE_CALL_LIMIT = 12
#   STATUS_NO_RIDING_ROUTE = 2001 / STATUS_NO_RESULT = 7
#   RIDING_RESTRICTION_FLAGS = (0x01, 0x02) / LINK_ATTR_NO_WALKING = 0x01
#
# 纯函数（无出网，几何层与单测可直接复用）
#   split_batches(items: Sequence[T], cap: int) -> list[list[T]]
#   official_matrix_signals(*, batch_status: int | None, cell: Mapping | None) -> list[str]
#   official_direction_signals(result: BaiduResult, *, lite: bool = False) -> list[str]
#   route_budget_text(mode: str, planned: int, state: Mapping) -> str
#
# @dataclass(frozen=True) ProbePoint(index: int, position: Any, azimuth_deg: float | None = None,
#                                    radial_m: float | None = None, label: str = "")
# @dataclass CellOutcome(index, point, duration_s, distance_m, measured, blocked, blocked_kind,
#                        signals, batch_index, sent, status, error)
# @dataclass BatchRecord(index, dest_start, dest_count, sent, status, chunks_sent, http_requests,
#                        quota_units, route_lines, place_calls, from_cache, blocked, unreachable,
#                        error, note)
# @dataclass MatrixProbeResult(mode, origin, requested_cells, batch_count, outcomes, batches,
#     quota_units, http_requests, place_calls, route_lines, partial, needs_interpolation, ok,
#     completed_cells, budget_stopped, stopped_reason, budget_state, errors,
#     duration_s(i) / distance_m(i) / outcome(i) / blocked_kind_for(i) / measured_indices /
#     blocked_official_indices / blocked_timeout_indices / missing_cells / usage() / as_dict())
# @dataclass PolylineResult(mode, point_index, allowed, ok, from_cache, reason, blocked,
#     blocked_kind, signals, path_strings, duration_s, distance_m, status, http_requests,
#     quota_units, route_lines, place_calls, error, as_dict())
#
# @runtime_checkable Protocol RouteMatrixClient —— 本层只依赖这三个方法：
#     async route_matrix(mode, origins, destinations, **kwargs) -> RouteMatrixResult
#     async direction(mode, origin, destination, **kwargs) -> BaiduResult
#     route_budget_state(mode=None, *, next_lines=0) -> dict[str, Any]
#
# class RouteProbe:
#     def __init__(self, client: RouteMatrixClient, *, mode: str = "walking",
#                  batch_size: int | None = None, polyline_call_limit: int = 12,
#                  ret_straight_dist: int | None = None, riding_type: int | None = None,
#                  road_prefer: int | None = None) -> None
#     async def probe(self, origin: Any, points: Sequence[Any], *,
#                     mode: str | None = None) -> MatrixProbeResult
#         # 1 origin × N destinations：N 按 ≤50 切批、顺序 await、缺格不抛异常
#     async def polyline(self, origin: Any, point: Any, *, index: int | None = None,
#                        mode: str | None = None, lite: bool = False) -> PolylineResult
#     async def polylines(self, origin: Any, points: Sequence[Any], *,
#                         mode: str | None = None, lite: bool = False) -> list[PolylineResult]
#     def usage(self) -> dict[str, Any]              # 三栏 + by_mode 分栏，两栏永不合并
#     def budget_state(self, mode=None, *, next_lines=0) -> dict[str, Any]  # 出网前预检
#     def guard_errors(self) -> list[str]             # 守卫透传失败留痕（排查用）
#     @property remaining_polyline_calls -> int
#
# ⚠️ 服务层接线：双模式（walking+riding）必须以 `BaiduClient(..., mode_count=2)` 构造，
#    否则本地预算按单模式折算（route_lines 150 / quota_units 120），第二轮会被预算守卫截停。
# =============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from infra.baidu_client import MATRIX_MAX_ROWS, BaiduResult, RouteMatrixResult, to_baidu_latlng

MODE_WALKING = "walking"
MODE_RIDING = "riding"
"""FR-206 双模式的两个分栏键（与 `infra/metrics.py` 的 `MODE_KEYS` 对齐）。"""

BLOCKED_OFFICIAL = "blocked_official"
BLOCKED_TIMEOUT = "blocked_timeout"
"""两种 `blocked` 标签。前者是「官方说这条路不通」，后者是「这一格我没测到」，语义与话术不同。"""

MATRIX_ROW_CAP = 50
"""批量算路单次路线上限（`origins × destinations`，笔记 §2.8）。取 client 常量表兜底值。"""

POLYLINE_CALL_LIMIT = 12
"""FR-203 懒加载的硬性单次上限：一次分析最多补调 12 次 `direction`（D8 代价条款）。"""

STATUS_NO_RIDING_ROUTE = 2001
STATUS_NO_RESULT = 7
"""官方硬信号状态码：`2001` 无骑行路线、`7` 无返回结果（PRD §4.2 判据清单）。

只有**官方明确回出的这两个码**才算 `blocked_official`；批量算路里 cell 存在但没耗时、
或整批一行都没有，属「空结果」，按 `blocked_timeout` 记——不能反过来把没测准说成路不通。
"""

RIDING_RESTRICTION_FLAGS: tuple[int, ...] = (0x01, 0x02)
"""骑行禁行（0x01）与逆行（0x02）位标记，按位与判定（FR-206 硬事实②）。"""

LINK_ATTR_NO_WALKING = 0x01
"""步行 links 里的禁行位（PRD §4.2：`links[].attr = 0x01`）。按位与判定，与上面同源。"""

TALLY_KEYS: tuple[str, ...] = (
    "quota_units",
    "http_requests",
    "place_calls",
    "route_lines",
    "matrix_batches",
    "matrix_cells_requested",
    "matrix_cells_measured",
    "direction_calls",
    "polyline_rejected",
    "blocked_official",
    "blocked_timeout",
    "budget_stops",
    "client_errors",
)
"""按 mode 分栏的账目字段。三栏（`quota_units` / `http_requests` / `place_calls`）永远单列，
不给合并成一个总数的入口——PRD §6.1 的预算断言是三项分开统计的。"""

_SIGNAL_LIMIT_NOTE = "polyline 懒加载已达单次上限"


# --------------------------------------------------------------------------- 工具函数
def _normalize_mode(mode: str | None) -> str:
    """出行方式归一。未知值**不抛异常**，原样交给 client 判（本层契约是永不抛）。"""
    normalized = (mode or MODE_WALKING).strip().lower()
    return normalized or MODE_WALKING


def _as_int(value: Any) -> int | None:
    """把 `0x01` / `"0x01"` / `"1"` / `1` 统一成 int；读不出就回 None（不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        for base in (0, 10):
            try:
                return int(text, base)
            except ValueError:
                continue
    return None


def _as_count(value: Any) -> int:
    """把结果对象上的计数读成非负整数；缺字段按 0 记账，绝不抛。"""
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed > 0 else 0


def _scalar_or_mapping(value: Any) -> float | None:
    """`duration` / `distance` 既可能是裸数字，也可能是 `{"value": n}`；两种都取。"""
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _flag_text(flag: int) -> str:
    return f"0x{flag:02d}"


def official_matrix_signals(
    *, batch_status: int | None = None, cell: Mapping[str, Any] | None = None
) -> list[str]:
    """批量算路的**官方硬信号**提取（不碰耗时、不碰距离，所以不可能推断出「耗时超标」）。

    返回形如 `["status=2001"]` / `["restrictions_status=0x01"]` 的信号清单；空列表表示
    官方没有给任何「此路不通」的证据——是否可达交由调用方按「有没有耗时」判为实测或缺格。
    """
    signals: list[str] = []
    for raw in (batch_status, None if cell is None else cell.get("status")):
        code = _as_int(raw)
        if code in (STATUS_NO_RIDING_ROUTE, STATUS_NO_RESULT):
            signal = f"status={code}"
            if signal not in signals:
                signals.append(signal)
    flags = _as_int(_pick_flag(cell, "restrictions_status"))
    if flags is not None:
        for bit in RIDING_RESTRICTION_FLAGS:
            if flags & bit:
                signals.append(f"restrictions_status={_flag_text(bit)}")
    return signals


def _pick_flag(container: Any, key: str) -> Any:
    """从可能不是 Mapping 的容器里取一个字段（本层对注入对象一律容忍）。"""
    if isinstance(container, Mapping):
        return container.get(key)
    return getattr(container, key, None)


def _direction_steps(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """取 `steps` 清单：v2 在 `result.routes[0].steps`，directionlite 在 `result.steps`。"""
    result = data.get("result")
    if not isinstance(result, Mapping):
        return []
    candidates: list[Any] = []
    routes = result.get("routes")
    if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes)) and routes:
        first = routes[0]
        if isinstance(first, Mapping):
            candidates.append(first.get("steps"))
    candidates.append(result.get("steps"))
    for item in candidates:
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and item:
            return [step for step in item if isinstance(step, Mapping)]
    return []


def official_direction_signals(result: BaiduResult, *, lite: bool = False) -> list[str]:
    """单条路线规划的官方硬信号：`status`、`restrictions_status`、`links[].attr=0x01`。

    `status=7` 只在 `directionlite` 才是官方的「无返回结果」硬判据（PRD §4.2 判据清单），
    所以 `lite=False`（v2）时不把 7 当硬信号，避免把一次普通的空响应说成官方禁行。
    """
    signals: list[str] = []
    status = _as_int(getattr(result, "status", None))
    if status == STATUS_NO_RIDING_ROUTE:
        signals.append(f"status={STATUS_NO_RIDING_ROUTE}")
    elif lite and status == STATUS_NO_RESULT:
        signals.append(f"directionlite.status={STATUS_NO_RESULT}")
    data = getattr(result, "data", None) or {}
    if not isinstance(data, Mapping):  # pragma: no cover - 防御：注入的假对象不该崩在这里
        return signals
    nested = data.get("result") if isinstance(data.get("result"), Mapping) else {}
    # `restrictions_status` 在 riding 的响应里可能挂在 body、result 或 routes[0] 任一层，
    # 三层都扫一遍再取第一个读得出的值：漏扫一层就是漏判官方禁行，多扫一层不会误判。
    flags = None
    for holder in (data, nested, _first_route(data)):
        flags = _as_int(_pick_flag(holder, "restrictions_status"))
        if flags is not None:
            break
    if flags is not None:
        for bit in RIDING_RESTRICTION_FLAGS:
            if flags & bit:
                signals.append(f"restrictions_status={_flag_text(bit)}")
    for step in _direction_steps(data):
        links = step.get("links")
        if not isinstance(links, Sequence) or isinstance(links, (str, bytes)):
            continue
        for link in links:
            attr = _as_int(link.get("attr")) if isinstance(link, Mapping) else None
            if attr is not None and attr & LINK_ATTR_NO_WALKING:
                signal = f"links.attr={_flag_text(LINK_ATTR_NO_WALKING)}"
                if signal not in signals:
                    signals.append(signal)
    return signals


def route_budget_text(mode: str, planned: int, state: Mapping[str, Any]) -> str:
    """把预算守卫的原始快照翻成**给用户的降级文案**（PRD §4.6 配额守护）。

    必须显式带上「步行与骑行共用同一份 5,000 条/日」这一句（FR-206 硬事实①）：双模式跑到
    第二轮被拦时，如果文案只说「骑行额度不足」，用户会以为再等等就有，实际是当日总量已尽。
    """
    remaining = _as_count(state.get("remaining"))
    used = _as_count(state.get("used"))
    budget = _as_count(state.get("budget"))
    return (
        f"路线预算守卫截停：本次打算发 {planned} 条（mode={mode}），"
        f"当日用掉 {used}/{budget} 条、剩 {remaining} 条。"
        f"注意步行与骑行**共用同一份 5,000 条/日配额**，截停不是某一模式单独耗尽。"
        f"已测顶点可直接进入插值兜底（needs_interpolation）。"
    )


def _coerce_points(points: Sequence[Any]) -> list[ProbePoint]:
    """入参容忍三种写法：`ProbePoint`、`(lat, lng)`、`{"lat":..,"lng":..}`。

    **列表顺序即下标**：几何层按方位角顺序传点，回填时靠下标对齐，`ProbePoint.index` 一律
    重写为位置序号，避免出现「结果按自带 index 排、上层按顺序取」两套口径。
    """
    collected: list[ProbePoint] = []
    for position_index, item in enumerate(points or ()):
        if isinstance(item, ProbePoint):
            collected.append(replace(item, index=position_index))
        else:
            collected.append(ProbePoint(index=position_index, position=item))
    return collected


def split_batches(items: Sequence[Any], cap: int) -> list[list[Any]]:
    """按 `origins × destinations ≤ 50` 切批（本层固定 1 个起点，所以批大小就是 cap）。

    为什么在本层切而不是把 60 个点全交给 client 让它自己分段：`route_matrix` 内部也会切，
    但它切完只回一个聚合对象，「第 3 段被预算拦停」这种信息就糊在 `status` 里了；本层自己
    切才能逐批记账（`BatchRecord`）并做到「截停后剩余批次根本不发」。
    """
    size = max(1, int(cap))
    pool = list(items)
    if not pool:
        return []
    return [pool[start : start + size] for start in range(0, len(pool), size)]


# --------------------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class ProbePoint:
    """一个探测点：位置 + 供上层归因的元信息（本层完全不解释 azimuth/radial 的几何含义）。"""

    index: int
    position: Any
    azimuth_deg: float | None = None
    radial_m: float | None = None
    label: str = ""


@dataclass
class CellOutcome:
    """一个探测点的测时结果 + 它的 blocked 归类与证据。"""

    index: int
    point: ProbePoint
    duration_s: float | None = None
    distance_m: float | None = None
    measured: bool = False
    blocked: bool = False
    blocked_kind: str | None = None
    signals: list[str] = field(default_factory=list)
    batch_index: int | None = None
    sent: bool = False
    status: int | None = None
    error: str | None = None

    def add_signal(self, signal: str) -> None:
        if signal not in self.signals:
            self.signals.append(signal)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.point.label,
            "azimuth_deg": self.point.azimuth_deg,
            "radial_m": self.point.radial_m,
            "duration_s": self.duration_s,
            "distance_m": self.distance_m,
            "measured": self.measured,
            "blocked": self.blocked,
            "blocked_kind": self.blocked_kind,
            "signals": list(self.signals),
            "batch_index": self.batch_index,
            "sent": self.sent,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class BatchRecord:
    """一批（≤50 条路线）的出网账目：这一批发了几次往返、折了几个配额单位。"""

    index: int
    dest_start: int
    dest_count: int
    sent: bool = False
    status: int | None = None
    chunks_sent: int = 0
    http_requests: int = 0
    quota_units: int = 0
    route_lines: int = 0
    place_calls: int = 0
    from_cache: bool = False
    blocked: bool = False
    unreachable: bool = False
    error: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "dest_start": self.dest_start,
            "dest_count": self.dest_count,
            "sent": self.sent,
            "status": self.status,
            "chunks_sent": self.chunks_sent,
            "http_requests": self.http_requests,
            "quota_units": self.quota_units,
            "route_lines": self.route_lines,
            "place_calls": self.place_calls,
            "from_cache": self.from_cache,
            "blocked": self.blocked,
            "unreachable": self.unreachable,
            "error": self.error,
            "note": self.note,
        }


@dataclass
class MatrixProbeResult:
    """一次「1 起点 × N 探测点」批量测时的结果对象（**永不抛异常**）。

    几何层要的三个东西都在这里：每个探测点的实测耗时、blocked 证据归类、这一段的三栏计数。
    """

    mode: str = MODE_WALKING
    origin: Any = None
    requested_cells: int = 0
    batch_count: int = 0
    outcomes: list[CellOutcome] = field(default_factory=list)
    batches: list[BatchRecord] = field(default_factory=list)
    quota_units: int = 0
    http_requests: int = 0
    place_calls: int = 0
    route_lines: int = 0
    partial: bool = False
    needs_interpolation: bool = False
    budget_stopped: bool = False
    stopped_reason: str | None = None
    budget_state: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """拿到了至少一条实测耗时。"""
        return any(outcome.measured for outcome in self.outcomes)

    @property
    def completed_cells(self) -> int:
        """实测到耗时的格子数（对齐 `RouteMatrixResult.completed_cells` 的叫法）。"""
        return sum(1 for outcome in self.outcomes if outcome.measured)

    @property
    def completion_ratio(self) -> float:
        if self.requested_cells <= 0:
            return 1.0
        return self.completed_cells / self.requested_cells

    @property
    def measured_indices(self) -> list[int]:
        return [o.index for o in self.outcomes if o.measured]

    @property
    def blocked_official_indices(self) -> list[int]:
        return [o.index for o in self.outcomes if o.blocked_kind == BLOCKED_OFFICIAL]

    @property
    def blocked_timeout_indices(self) -> list[int]:
        return [o.index for o in self.outcomes if o.blocked_kind == BLOCKED_TIMEOUT]

    @property
    def missing_cells(self) -> list[int]:
        """没拿到实测耗时的下标（含官方禁行与空结果两种；靠 `blocked_kind_for` 区分）。"""
        return [o.index for o in self.outcomes if not o.measured]

    def outcome(self, index: int) -> CellOutcome | None:
        for item in self.outcomes:
            if item.index == index:
                return item
        return None

    def duration_s(self, index: int) -> float | None:
        item = self.outcome(index)
        return item.duration_s if item else None

    def distance_m(self, index: int) -> float | None:
        item = self.outcome(index)
        return item.distance_m if item else None

    def blocked_kind_for(self, index: int) -> str | None:
        item = self.outcome(index)
        return item.blocked_kind if item else None

    def usage(self) -> dict[str, Any]:
        """这一段花掉的三栏（平铺，不给合并总数的入口）。"""
        return {
            "mode": self.mode,
            "quota_units": self.quota_units,
            "http_requests": self.http_requests,
            "place_calls": self.place_calls,
            "route_lines": self.route_lines,
            "batches": len([b for b in self.batches if b.sent]),
            "requested_cells": self.requested_cells,
            "completed_cells": self.completed_cells,
            "blocked_official": len(self.blocked_official_indices),
            "blocked_timeout": len(self.blocked_timeout_indices),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "requested_cells": self.requested_cells,
            "batch_count": self.batch_count,
            "completed_cells": self.completed_cells,
            "completion_ratio": self.completion_ratio,
            "missing_cells": self.missing_cells,
            "partial": self.partial,
            "needs_interpolation": self.needs_interpolation,
            "budget_stopped": self.budget_stopped,
            "stopped_reason": self.stopped_reason,
            "usage": self.usage(),
            "batches": [batch.as_dict() for batch in self.batches],
            "cells": [outcome.as_dict() for outcome in self.outcomes],
            "errors": list(self.errors),
        }


@dataclass
class PolylineResult:
    """FR-203 懒加载的一条路径串（点击顶点时才补调，单次上限 12 次）。

    `path_strings` 是官方 `steps[].path` 原样串（`经度,纬度;…`，笔记 §6.2 第 3 条：
    direction 的 `location` 是 `{lng,lat}` 而 `steps[].path` 是「经度,纬度」顺序）。
    本层**不做坐标顺序换算**——那是几何层的事，两处各转一遍必出错。
    """

    mode: str = MODE_WALKING
    point_index: int | None = None
    allowed: bool = True
    ok: bool = False
    from_cache: bool = False
    reason: str | None = None
    blocked: bool = False
    blocked_kind: str | None = None
    signals: list[str] = field(default_factory=list)
    path_strings: list[str] = field(default_factory=list)
    duration_s: float | None = None
    distance_m: float | None = None
    status: int | None = None
    http_requests: int = 0
    quota_units: int = 0
    route_lines: int = 0
    place_calls: int = 0
    error: str | None = None

    def add_signal(self, signal: str) -> None:
        if signal not in self.signals:
            self.signals.append(signal)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "point_index": self.point_index,
            "allowed": self.allowed,
            "ok": self.ok,
            "from_cache": self.from_cache,
            "reason": self.reason,
            "blocked": self.blocked,
            "blocked_kind": self.blocked_kind,
            "signals": list(self.signals),
            "path_segments": len(self.path_strings),
            "duration_s": self.duration_s,
            "distance_m": self.distance_m,
            "status": self.status,
            "http_requests": self.http_requests,
            "quota_units": self.quota_units,
            "route_lines": self.route_lines,
            "place_calls": self.place_calls,
            "error": self.error,
        }


@runtime_checkable
class RouteMatrixClient(Protocol):
    """本层唯一依赖的窄接口——`BaiduClient` 天然满足，测试传假对象即可零出网。

    为什么不直接 `import BaiduClient` 做类型标注：等时圈单测必须先有 mock 才跑得起来，
    按协议注入后 CI 传一个 stub 就能验证切批与三栏记账（对齐 `baidu_client` 里
    `ResponseCache` Protocol 的做法）。
    """

    async def route_matrix(
        self, mode: str, origins: Sequence[Any], destinations: Sequence[Any], **kwargs: Any
    ) -> RouteMatrixResult: ...

    async def direction(
        self, mode: str, origin: Any, destination: Any, **kwargs: Any
    ) -> BaiduResult: ...

    def route_budget_state(
        self, mode: str | None = None, *, next_lines: int = 0
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- 出网层
class RouteProbe:
    """批量测时 + 路径串懒加载 + 三栏计量（M2 等时圈的唯一出网入口）。"""

    def __init__(
        self,
        client: RouteMatrixClient,
        *,
        mode: str = MODE_WALKING,
        batch_size: int | None = None,
        polyline_call_limit: int = POLYLINE_CALL_LIMIT,
        ret_straight_dist: int | None = None,
        riding_type: int | None = None,
        road_prefer: int | None = None,
    ) -> None:
        """构造探测器。

        `batch_size` 是每批目的地数上限，**不会超过**官方的 `起 × 终 ≤ 50`；留空即取 50。
        `polyline_call_limit` 默认 12（FR-203 顶点数上限），设成 0 表示禁用懒加载。
        `road_prefer` / `riding_type` 只在 riding 下透传（FR-206 用 `road_prefer=3`
        作「无逆行且无阶梯」的质量开关）；留空一律不发——凡官方未列的组合不猜。
        """
        self.client = client
        self.mode = _normalize_mode(mode)
        requested = _as_count(batch_size)
        self.batch_size = requested if requested else MATRIX_ROW_CAP
        self.polyline_call_limit = max(0, _as_int(polyline_call_limit) or 0)
        self.ret_straight_dist = ret_straight_dist
        self.riding_type = riding_type
        self.road_prefer = road_prefer
        self._tally: dict[str, dict[str, int]] = {}
        self._polyline_calls = 0
        self._guard_errors: list[str] = []
        # 同一顶点重复点击不该再打出去：命中缓存不计数，省下的都是同日共享的配额。
        self._polyline_cache: dict[str, PolylineResult] = {}

    # ------------------------------------------------------------------ 记账
    def _bucket(self, mode: str) -> dict[str, int]:
        return self._tally.setdefault(_normalize_mode(mode), _new_tally())

    def _note(self, mode: str, **deltas: int) -> None:
        bucket = self._bucket(mode)
        for key, value in deltas.items():
            bucket[key] = bucket.get(key, 0) + int(value)

    def usage(self) -> dict[str, Any]:
        """三栏快照（总量 + 按 mode 分栏）。

        `quota_units` 与 `http_requests` 是两个**不可互换**的口径：前者是百度扣配额的路线条数
        （96 条就是 96），后者是真实往返（切分后 5~8）。报告里只写后者会把配额低估一个数量级
        （PRD §4.2 第 267 行）。`place_calls` 本层恒为 0：等时圈只测时，POI 一律复用已拉到的
        集合（D5 / FR-206 硬事实③）。
        """
        totals = _new_tally()
        for bucket in self._tally.values():
            for key, value in bucket.items():
                totals[key] = totals.get(key, 0) + value
        return {
            "place_calls": totals["place_calls"],
            "quota_units": totals["quota_units"],
            "http_requests": totals["http_requests"],
            "route_lines": totals["route_lines"],
            "by_mode": {mode: dict(bucket) for mode, bucket in sorted(self._tally.items())},
            "polyline_calls": totals["direction_calls"],
            "polyline_limit": self.polyline_call_limit,
            "polyline_rejected": totals["polyline_rejected"],
            "budget_stops": totals["budget_stops"],
            "client_errors": totals["client_errors"],
            "guard_errors": len(self._guard_errors),
        }

    @property
    def remaining_polyline_calls(self) -> int:
        return max(0, self.polyline_call_limit - self._polyline_calls)

    def budget_state(self, mode: str | None = None, *, next_lines: int = 0) -> dict[str, Any]:
        """出网前的预算预检（透传 client 的守卫，供 UI 在跑第二轮前显式提示）。

        注意 `remaining` 是**跨模式的当日总量**——步行与骑行共用 5,000 条/日（FR-206 ①），
        所以这里的 `mode` 只影响 `used_in_mode`，不影响能不能发。取不到就当不拦截，
        由 client 自身的守卫兜底（本层永不抛）。

        ⚠️ 接线必读：本地守卫取的是 `Settings.budget_snapshot(mode_count)`——单模式
        `route_lines=150 / quota_units=120`，双模式 `300 / 220`。PRD §4.2 一轮步行体检是
        72~96 条 + 懒加载 ≤12 条 = 84~108，单模式刚好放得下；但**跑 walking+riding 必须让
        `BaiduClient(mode_count=2)` 构造**，否则第二轮（骑行）会被本守卫按单模式预算截停——
        这是有意的保守行为（宁停不错），但服务层要显式声明模式数才能拿到应得的额度。
        """
        try:
            state = self.client.route_budget_state(
                _normalize_mode(mode or self.mode), next_lines=int(next_lines)
            )
        except Exception as exc:  # pragma: no cover - 假 client 少实现一个方法时不该崩
            self._record_error(f"route_budget_state:{type(exc).__name__}")
            return {}
        return state if isinstance(state, Mapping) else {}

    def _record_error(self, detail: str) -> None:
        """守卫自身的异常只留痕不抛：本层的契约是「任何失败都回结果对象」。"""
        self._guard_errors.append(detail)

    def guard_errors(self) -> list[str]:
        """预算守卫透传失败的历史（排查「为什么没截停」用）。"""
        return list(self._guard_errors)

    # ------------------------------------------------------------------ 批量测时
    async def probe(
        self,
        origin: Any,
        points: Sequence[Any],
        *,
        mode: str | None = None,
    ) -> MatrixProbeResult:
        """1 起点 × N 探测点的批量测时：切批（≤50）→ 顺序 await → 按下标合并。

        返回值永远是 `MatrixProbeResult`：预算截停、官方禁行、整批空结果、client 抛异常都只
        反映在 `partial` / `blocked_kind` / `budget_stopped` / `errors` 上（FR-403）。
        """
        use_mode = _normalize_mode(mode or self.mode)
        probes = _coerce_points(points)
        result = MatrixProbeResult(
            mode=use_mode,
            origin=origin,
            requested_cells=len(probes),
            outcomes=[CellOutcome(index=p.index, point=p) for p in probes],
        )
        if not probes:
            result.partial = False
            return result

        batches = split_batches(probes, self._cap_for(use_mode))
        result.batch_count = len(batches)
        self._note(use_mode, matrix_cells_requested=len(probes))

        for batch_index, group in enumerate(batches):
            planned = len(group)
            state = self.budget_state(use_mode, next_lines=planned)
            if state and not state.get("fits_next_chunk", True):
                # 显式截停：不发这一批、也不发后面的批次，原因回传给调用方做降级文案。
                reason = route_budget_text(use_mode, planned, state)
                result.budget_stopped = True
                result.stopped_reason = reason
                result.budget_state = dict(state)
                self._note(use_mode, budget_stops=1)
                for skipped_index, pending in enumerate(batches[batch_index:], start=batch_index):
                    record = BatchRecord(
                        index=skipped_index,
                        dest_start=pending[0].index,
                        dest_count=len(pending),
                        sent=False,
                        note="budget_stopped",
                    )
                    result.batches.append(record)
                    result.errors.append(
                        {
                            "batch": skipped_index,
                            "error": reason,
                            "not_sent_cells": len(pending),
                            "cause": "budget_guard",
                        }
                    )
                    for item in pending:
                        outcome = result.outcome(item.index)
                        if outcome is None:  # pragma: no cover - 下标由本层生成，不该缺
                            continue
                        # 注意：**不**归 blocked。官方没说这条路不通，只是当日额度没了，
                        # 几何层该把它当 interpolated（可修复）而不是 blocked_*（路走不通）。
                        outcome.batch_index = skipped_index
                        outcome.sent = False
                        outcome.add_signal("budget_stopped")
                _finalize(result)
                return result

            record = BatchRecord(
                index=batch_index,
                dest_start=group[0].index,
                dest_count=planned,
                sent=True,
            )
            result.batches.append(record)
            await self._send_batch(origin, group, use_mode, result, record)

        _finalize(result)
        return result

    def _cap_for(self, mode: str) -> int:
        """批大小 = min(用户设定, 官方上限 // 起点数)。本层固定 1 起点，所以就是官方上限。"""
        official = _as_int(MATRIX_MAX_ROWS.get(mode)) or MATRIX_ROW_CAP
        cap = min(self.batch_size, official) if official > 0 else self.batch_size
        return max(1, cap)

    async def _send_batch(
        self,
        origin: Any,
        group: Sequence[ProbePoint],
        mode: str,
        result: MatrixProbeResult,
        record: BatchRecord,
    ) -> None:
        """发一批（1 次 client 调用）。3 QPS 与桶等待全在 client 里，这里不 sleep、不并发。"""
        kwargs: dict[str, Any] = {}
        if self.ret_straight_dist is not None:
            kwargs["ret_straight_dist"] = self.ret_straight_dist
        if mode == MODE_RIDING:
            if self.riding_type is not None:
                kwargs["riding_type"] = self.riding_type
            if self.road_prefer is not None:
                kwargs["road_prefer"] = self.road_prefer
        try:
            out = await self.client.route_matrix(
                mode, [origin], [item.position for item in group], **kwargs
            )
        except Exception as exc:
            # 契约：任何异常都不外抛。官方没给硬信号，所以按「没测到」归 blocked_timeout。
            record.error = type(exc).__name__
            record.note = "client_raised"
            result.errors.append(
                {"batch": record.index, "error": type(exc).__name__, "detail": str(exc)[:200]}
            )
            self._note(mode, client_errors=1)
            for item in group:
                outcome = result.outcome(item.index)
                if outcome is None:  # pragma: no cover
                    continue
                outcome.batch_index = record.index
                outcome.sent = True
                outcome.error = type(exc).__name__
                outcome.blocked = True
                outcome.blocked_kind = BLOCKED_TIMEOUT
                outcome.add_signal(f"client_error={type(exc).__name__}")
            self._note(mode, blocked_timeout=len(group))
            return

        record.chunks_sent = _as_count(getattr(out, "chunks_sent", 0))
        record.status = _as_int(getattr(out, "status", None))
        record.http_requests = _as_count(getattr(out, "http_requests", 0))
        record.quota_units = _as_count(getattr(out, "quota_units", 0))
        record.route_lines = _as_count(getattr(out, "route_lines", 0))
        record.place_calls = _as_count(getattr(out, "place_calls", 0))
        record.from_cache = bool(getattr(out, "from_cache", False))
        record.blocked = bool(getattr(out, "blocked", False))
        record.unreachable = bool(getattr(out, "unreachable", False))
        if record.blocked:
            record.note = getattr(out, "blocked_reason", None) or "blocked"

        # 三栏直接取 client 的口径：quota_units=起×终折算、http_requests=真实往返。
        # 缓存命中时两者都是 0——这不是漏账，而是缓存确实没消耗百度配额。
        result.http_requests += record.http_requests
        result.quota_units += record.quota_units
        result.route_lines += record.route_lines
        result.place_calls += record.place_calls
        self._note(
            mode,
            matrix_batches=1,
            http_requests=record.http_requests,
            quota_units=record.quota_units,
            route_lines=record.route_lines,
            place_calls=record.place_calls,
        )

        missing_offsets = self._missing_offsets(out, len(group))
        # 整批一行都没回（`status=0` 但 `result=[]`）与「少了几行」是两个不同的证据名，
        # 但都归 blocked_timeout——官方没说这条路不通，只是我没测到。
        batch_has_rows = _as_count(getattr(out, "completed_cells", 0)) > 0
        for offset, item in enumerate(group):
            outcome = result.outcome(item.index)
            if outcome is None:  # pragma: no cover
                continue
            outcome.batch_index = record.index
            outcome.sent = True
            outcome.status = record.status
            cell = self._cell(out, offset)
            signals = official_matrix_signals(batch_status=record.status, cell=cell)
            for signal in signals:
                outcome.add_signal(signal)
            duration = self._duration_at(out, 0, offset)
            distance = self._distance_at(out, 0, offset)
            if signals:
                outcome.blocked = True
                outcome.blocked_kind = BLOCKED_OFFICIAL
                outcome.duration_s = duration
                outcome.distance_m = distance
                self._note(mode, blocked_official=1)
            elif duration is not None:
                outcome.measured = True
                outcome.duration_s = duration
                outcome.distance_m = distance
                self._note(mode, matrix_cells_measured=1)
            else:
                # 既没有官方硬信号、也没拿到耗时 → 分两种账：
                #   client 内部被预算拦停（可修复，**不算 blocked**，几何层按 interpolated 走）
                #   空结果 / 缺格 / 网络不可达（= blocked_timeout，"这一格我没测准"）
                outcome.distance_m = distance
                if record.blocked:
                    outcome.add_signal(getattr(out, "blocked_reason", None) or "budget_stopped")
                else:
                    outcome.blocked = True
                    outcome.blocked_kind = BLOCKED_TIMEOUT
                    if cell is None or offset in missing_offsets:
                        # 整批一行没回 = 空结果；回了几行少几行 = 缺格。两者都是 blocked_timeout。
                        outcome.add_signal("missing_cell" if batch_has_rows else "empty_result")
                    else:
                        outcome.add_signal("no_duration")
                    if record.unreachable:
                        outcome.add_signal("unreachable")
                    if not record.chunks_sent:
                        outcome.add_signal("nothing_sent")
                    self._note(mode, blocked_timeout=1)

    @staticmethod
    def _cell(out: RouteMatrixResult, offset: int) -> Mapping[str, Any] | None:
        """只从 `cells` 取官方禁行证据；耗时/距离仍走 `duration_s` / `distance_m` 访问器。"""
        try:
            row = out.cells[0]
        except (AttributeError, IndexError, TypeError):
            return None
        try:
            value = row[offset]
        except (IndexError, TypeError):
            return None
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _duration_at(out: RouteMatrixResult, origin_index: int, offset: int) -> float | None:
        try:
            value = out.duration_s(origin_index, offset)
        except Exception:  # pragma: no cover - 访问器已经做了防御，这里只兜注入对象
            return None
        if value is not None:
            return float(value)
        cell = RouteProbe._cell(out, offset)
        return None if cell is None else _scalar_or_mapping(cell.get("duration"))

    @staticmethod
    def _distance_at(out: RouteMatrixResult, origin_index: int, offset: int) -> float | None:
        try:
            value = out.distance_m(origin_index, offset)
        except Exception:  # pragma: no cover - 访问器已做防御，这里只兜注入对象
            return None
        if value is not None:
            return float(value)
        cell = RouteProbe._cell(out, offset)
        return None if cell is None else _scalar_or_mapping(cell.get("distance"))

    @staticmethod
    def _missing_offsets(out: RouteMatrixResult, group_size: int) -> set[int]:
        """用现成的 `missing_cells()` 判缺格，不自己解析 payload 行数。"""
        try:
            pairs = out.missing_cells()
        except Exception:  # pragma: no cover
            return set(range(group_size))
        offsets: set[int] = set()
        for pair in pairs or ():
            try:
                origin_index, dest_index = pair
            except (TypeError, ValueError):  # pragma: no cover
                continue
            if int(origin_index) == 0:
                offsets.add(int(dest_index))
        return offsets

    # ------------------------------------------------------------------ polyline 懒加载
    async def polyline(
        self,
        origin: Any,
        point: Any,
        *,
        index: int | None = None,
        mode: str | None = None,
        lite: bool = False,
    ) -> PolylineResult:
        """FR-203：只在用户点击某个顶点时补调一次 `direction`。超限回结果对象、不打。

        RouteMatrix 不返回 polyline（D8 的代价），但多边形顶点坐标本身已够用（FR-202），
        所以路径串必须懒加载；这里硬性限制 `polyline_call_limit`（默认 12）次。
        """
        use_mode = _normalize_mode(mode or self.mode)
        probe_point = _coerce_points([point])[0] if point is not None else None
        position = probe_point.position if probe_point is not None else None
        point_index = index if index is not None else (probe_point.index if probe_point else None)
        result = PolylineResult(mode=use_mode, point_index=point_index)
        if position is None:
            result.allowed = False
            result.reason = "缺少点位，无法补调 direction"
            return result

        key = self._polyline_key(origin, position, use_mode, lite)
        cached = self._polyline_cache.get(key) if key else None
        if cached is not None:
            # 命中缓存不出网，所以三栏都不加计数（缓存不消耗百度配额，记了反而是假账）。
            return replace(cached, from_cache=True, point_index=point_index)

        if self._polyline_calls >= self.polyline_call_limit:
            # 超限：返回结果对象（带 reason），不继续打——这是「不再对线上施压」的硬闸。
            self._note(use_mode, polyline_rejected=1)
            result.allowed = False
            result.reason = (
                f"{_SIGNAL_LIMIT_NOTE}（{self.polyline_call_limit} 次），"
                f"已用 {self._polyline_calls} 次；该顶点只回显顶点坐标，不画真实路径。"
            )
            return result

        self._polyline_calls += 1
        kwargs: dict[str, Any] = {"lite": bool(lite)}
        if not lite:
            kwargs["steps_info"] = 1
        if use_mode == MODE_RIDING:
            if self.riding_type is not None:
                kwargs["riding_type"] = self.riding_type
            if self.road_prefer is not None:
                kwargs["road_prefer"] = self.road_prefer
        try:
            out = await self.client.direction(use_mode, origin, position, **kwargs)
        except Exception as exc:
            self._note(use_mode, client_errors=1)
            result.error = type(exc).__name__
            result.blocked = True
            result.blocked_kind = BLOCKED_TIMEOUT
            result.add_signal(f"client_error={type(exc).__name__}")
            return result

        result.http_requests = _as_count(getattr(out, "http_requests", 0))
        result.quota_units = _as_count(getattr(out, "quota_units", 0))
        result.route_lines = _as_count(getattr(out, "route_lines", 0))
        result.place_calls = _as_count(getattr(out, "place_calls", 0))
        result.status = _as_int(getattr(out, "status", None))
        result.from_cache = bool(getattr(out, "from_cache", False))
        self._note(
            use_mode,
            direction_calls=1,
            http_requests=result.http_requests,
            quota_units=result.quota_units,
            route_lines=result.route_lines,
            place_calls=result.place_calls,
        )

        signals = official_direction_signals(out, lite=bool(lite))
        for signal in signals:
            result.add_signal(signal)
        data = getattr(out, "data", None) or {}
        steps = _direction_steps(data) if isinstance(data, Mapping) else []
        result.path_strings = [str(step["path"]) for step in steps if step.get("path") is not None]
        route = _first_route(data)
        result.duration_s = _scalar_or_mapping(route.get("duration"))
        result.distance_m = _scalar_or_mapping(route.get("distance"))
        if signals:
            result.blocked = True
            result.blocked_kind = BLOCKED_OFFICIAL
            result.ok = bool(result.path_strings)
        elif result.path_strings:
            result.ok = True
        else:
            result.blocked = True
            result.blocked_kind = BLOCKED_TIMEOUT
            result.add_signal("empty_result")
            self._note(use_mode, blocked_timeout=1)
        if signals:
            self._note(use_mode, blocked_official=1)
        if key:
            self._polyline_cache[key] = replace(result)
        return result

    async def polylines(
        self,
        origin: Any,
        points: Sequence[Any],
        *,
        mode: str | None = None,
        lite: bool = False,
    ) -> list[PolylineResult]:
        """少量顶点的批量懒加载。超出剩余额度的点位**顺序拿到拒绝对象**，不抛不沉默。"""
        collected: list[PolylineResult] = []
        for probe_point in _coerce_points(points):
            collected.append(
                await self.polyline(
                    origin, probe_point, index=probe_point.index, mode=mode, lite=lite
                )
            )
        return collected

    def _polyline_key(self, origin: Any, position: Any, mode: str, lite: bool) -> str:
        """缓存键：坐标走 client 的同一个归一函数，避免两套小数位口径把命中率腰斩。"""
        try:
            return f"{mode}|{int(bool(lite))}|{to_baidu_latlng(origin)}|{to_baidu_latlng(position)}"
        except Exception:
            return ""


# --------------------------------------------------------------------------- 内部收尾
def _new_tally() -> dict[str, int]:
    return dict.fromkeys(TALLY_KEYS, 0)


def _first_route(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):  # pragma: no cover
        return {}
    result = data.get("result")
    if not isinstance(result, Mapping):
        return {}
    routes = result.get("routes")
    if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes)) and routes:
        first = routes[0]
        if isinstance(first, Mapping):
            return first
    return result


def _finalize(result: MatrixProbeResult) -> None:
    """把「有没有缺格」翻译成 client 契约里的 `partial` / `needs_interpolation`。"""
    incomplete = any(not outcome.measured for outcome in result.outcomes)
    result.partial = bool(result.budget_stopped or incomplete or result.errors)
    result.needs_interpolation = result.partial
    if result.budget_stopped and not result.stopped_reason:  # pragma: no cover - 防御
        result.stopped_reason = "路线预算守卫截停"


__all__ = [
    "BLOCKED_OFFICIAL",
    "BLOCKED_TIMEOUT",
    "LINK_ATTR_NO_WALKING",
    "MATRIX_ROW_CAP",
    "MODE_RIDING",
    "MODE_WALKING",
    "POLYLINE_CALL_LIMIT",
    "RIDING_RESTRICTION_FLAGS",
    "STATUS_NO_RESULT",
    "STATUS_NO_RIDING_ROUTE",
    "BatchRecord",
    "CellOutcome",
    "MatrixProbeResult",
    "PolylineResult",
    "ProbePoint",
    "RouteMatrixClient",
    "RouteProbe",
    "official_direction_signals",
    "official_matrix_signals",
    "route_budget_text",
    "split_batches",
]
