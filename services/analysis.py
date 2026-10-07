"""services/analysis.py —— M4「一键体检」编排层：把 M2 等时圈、M3 POI/盲区、M4 评分串成一条链。

对应 PRD §1.4 的核心用户旅程、§4.7 的 `/api/analysis` 主接口与 §4.1 的 service layer。

一次体检的固定顺序（每一步都复用上一步的产物，**不重复出网**）：

```
① 等时圈   IsochroneService.compute()        → 每个 mode 一个环（PRD §4.2）
② POI      PoiSearchService.search()          → 一份 POI 集合（PRD §4.3，place_calls ≤ 8）
③ 落圈     build_coverage()                   → count_in / count_near + 前 K 实测（1 次批量算路）
④ 盲区     detect_blind_spots()               → 复用 ③ 的点集，**0 次出网**（D5 / FR-221）
⑤ 评分     build_score_block() + build_summary() → 总分 / 雷达 / 摘要（PRD §4.5）
```

四条把这条链写对的关键约束：

1. 🔴 **主模式（`modes[0]`）决定 POI 半径与盲区判定所用的环**。双模式（步行 + 骑行）时，
   POI 只检索**一次**、环形覆盖只实测**一次**，骑行圈复用同一份 POI 集合 —— 这是 FR-206
   明文要求（「骑行等时圈的设施计数仍复用同一份 POI 集合，不额外发检索，`place_calls` 不变」，D5）。
   两种模式各拉一份 POI 会让 `place_calls` 直接翻倍并击穿当日 100 次的地点检索配额（R1′）。
2. 🔴 **前后两个阶段的路线预算共用同一个客户端**。③ 的批量实测走
   `IsochroneService.probe_points()`，它取的是同一个 `BaiduClient` 的会话探测器，
   所以 `route` 桶的日预算守卫横跨①②③ 全程生效；自己 new 一个 `RouteProbe` 会让预算
   各算各的，表现是「等时圈跑完了、实测又偷偷多烧一批」。
3. 🔴 **三栏计量全程分开累加、`route_lines` 按 mode 分栏**（PRD §4.2 / §4.6）。
   本层只做加法，不做任何折算 —— `quota_units` 与 `http_requests` 混成一个数会把配额消耗
   低估一个数量级（v1.0 就错在这里）。
4. 🔴 **任何一步降级都不中断**（FR-403）：等时圈失败仍出报告（没有圈则 `count_in` 全 0 并留 note）、
   POI 降级到合成层则报告标注 `sample`、实测批次失败则相关点位 `unmeasured` 且时效分记 0。
   整条链**没有一条通往 500 的路径**。

`platform` 只影响两处，且都是 PRD 明写的：前 K 实测的 K（桌面 12 / 移动端 6，FR-107）与
是否走轻量档（跳过中点加密与耗时热力场，FR-107）。本层不猜设备，缺省按桌面。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from infra.config import Settings, get_settings
from infra.log import get_logger, log_fields

from services.blind_spots import (
    DEFAULT_RADIUS_M as BLIND_RADIUS_M_DEFAULT,
)
from services.blind_spots import (
    DEFAULT_STEP_M as BLIND_STEP_M_DEFAULT,
)
from services.blind_spots import (
    blocks_to_geojson,
    detect_blind_spots,
)
from services.coverage import K_DESKTOP, K_MOBILE, build_coverage
from services.isochrone_geometry import IsochroneConfig, as_lnglat, distance_m, initial_bearing
from services.isochrone_service import IsochroneService, SyntheticOracle, get_isochrone_service
from services.poi_search import PoiSearchService, get_poi_search_service
from services.route_probe import BatchRecord, CellOutcome, MatrixProbeResult, ProbePoint
from services.score import ScoreConfig, build_score_block, build_summary, load_score_config

__all__ = [
    "PLATFORM_PROFILES",
    "SyntheticCoverageProbe",
    "UsageTally",
    "aggregate_usage",
    "budget_assertions",
    "run_analysis",
]

logger = get_logger("circledoc.services.analysis")

PLATFORM_PROFILES: dict[str, dict[str, Any]] = {
    "desktop": {"k": K_DESKTOP, "lightweight": False},
    "mobile": {"k": K_MOBILE, "lightweight": True},
}
"""FR-107 的两个档位。`mobile` 除了 K 减半，还关掉中点加密与耗时热力场：
只跑 12 方向粗扫 → `http_requests` ≤ 5、路线类 `quota_units` ≤ 40、耗时 ≤ 20 s。"""

DEFAULT_PLATFORM = "desktop"


# --------------------------------------------------------------------------------------
# ③ 的适配器：把 IsochroneService 的探针接成 coverage 的 CoverageProbe 协议
# --------------------------------------------------------------------------------------
class _SharedProbe:
    """`services/coverage.py:CoverageProbe` 协议的最小实现，**复用 M2 的会话探测器**。

    为什么不在本层 new 一个 `RouteProbe`：预算守卫挂在 `BaiduClient` 上，而
    `IsochroneService.probe_points()` 走的是它自己 `_probe_for()` 取出来的探测器。
    从这里绕一道，全程就只有一套预算账；自己造一个探测器等于把「单次分析的路线预算」
    悄悄变成两份（见模块 docstring 第 2 条）。
    """

    def __init__(self, service: IsochroneService) -> None:
        self._service = service

    async def probe(self, origin: Any, points: Sequence[Any], *, mode: str | None = None) -> Any:
        return await self._service.probe_points(origin, points, mode=mode or "walking")


class SyntheticCoverageProbe:
    """离线/降级时给「前 K 实测」用的**合成探针**（FR-105 / FR-403 / D9）。

    为什么必须有它：没有它的话，`mock` 模式下的体检报告会退化成「所有维度
    `nearest_minutes=None` → 时效分全 0 → 总分一律 50 上下」。那种报告**看起来像跑通了，
    其实算法什么都没证明**：柱状图（FR-303）空着、雷达图六个维度全是同一个数、
    「1 次批量算路测 K 个点」这条核心设计在报告里完全不可见。断网演示是 PRD §4.6 的
    硬需求（演示预案 B），它要求的是一份**完整**报告，不是一份「能渲染但没内容」的报告。

    两条设计约束：

    1. 🔴 **与等时圈合成层同源**。直接复用 `services/isochrone_service.py:SyntheticOracle`
       的各向异性速度场（三次谐波 + 顺畅走廊 + 阻隔扇区），而不是另写一套「随便编个数」。
       这样「等时圈说 12 分钟可达、明细表说这个菜市场 11.8 分钟」在合成数据里也是自洽的 ——
       两套模型各编各的，报告内部立刻自相矛盾，评审一眼就能看出来。
    2. 🔴 **计量如实为 0**。合成不发出网请求，所以 `http_requests` / `quota_units` /
       `place_calls` 全部保持 0，批次记录标 `sent=False` + `note="synthetic"`。
       刻意**不**模拟成「假装花了 96 条配额」：`usage` 是给配额审计看的对账单，
       往里填没发生过的消耗，比不填更糟。
    """

    def __init__(self, center: Any, *, target_seconds: float = 900.0) -> None:
        self.center = as_lnglat(center)
        self.target_seconds = float(target_seconds)
        self._oracles: dict[str, SyntheticOracle] = {}

    def _oracle(self, mode: str) -> SyntheticOracle:
        if mode not in self._oracles:
            self._oracles[mode] = SyntheticOracle(
                self.center, mode=mode, target_seconds=self.target_seconds
            )
        return self._oracles[mode]

    async def probe(self, origin: Any, points: Sequence[Any], *, mode: str | None = None) -> Any:
        use_mode = str(mode or "walking")
        try:
            center = as_lnglat(origin) if origin is not None else self.center
        except (TypeError, ValueError):
            center = self.center
        oracle = self._oracle(use_mode)
        outcomes: list[CellOutcome] = []
        for index, item in enumerate(points or ()):
            try:
                target = as_lnglat(item)
            except (TypeError, ValueError):
                # 坐标不可解析：留一条未实测记录而不是丢掉，否则下标会与上游错位
                # （`build_coverage` 按 `index` 回填实测值，错位等于把 A 的时间记到 B 头上）。
                outcomes.append(
                    CellOutcome(
                        index=index,
                        point=ProbePoint(index=index, position=item, label="synthetic"),
                        measured=False,
                    )
                )
                continue
            bearing = initial_bearing(center, target)
            radial = distance_m(center, target)
            outcomes.append(
                CellOutcome(
                    index=index,
                    point=ProbePoint(
                        index=index,
                        position=target,
                        azimuth_deg=bearing,
                        radial_m=radial,
                        label="synthetic",
                    ),
                    duration_s=oracle.duration_at(bearing, radial),
                    distance_m=oracle.path_distance_m(bearing, radial),
                    measured=True,
                    batch_index=0,
                    sent=False,
                )
            )
        return MatrixProbeResult(
            mode=use_mode,
            origin=center,
            requested_cells=len(outcomes),
            batch_count=1 if outcomes else 0,
            outcomes=outcomes,
            batches=(
                [
                    BatchRecord(
                        index=0,
                        dest_start=0,
                        dest_count=len(outcomes),
                        sent=False,
                        note="synthetic",
                    )
                ]
                if outcomes
                else []
            ),
            partial=False,
        )


# --------------------------------------------------------------------------------------
# 三栏计量
# --------------------------------------------------------------------------------------
class UsageTally:
    """把各阶段的 `usage` 字典加总。**只做加法**：任何折算都在各自的层里做完。"""

    FIELDS = (
        "http_requests",
        "quota_units",
        "place_calls",
        "cache_hits",
        "batches_sent",
        "budget_stops",
    )

    def __init__(self) -> None:
        self.totals: dict[str, int] = dict.fromkeys(self.FIELDS, 0)
        self.route_lines: dict[str, int] = {}
        self.stages: dict[str, dict[str, int]] = {}

    def add(self, stage: str, usage: Mapping[str, Any] | None) -> None:
        """并入一段 usage；缺键按 0 处理，`route_lines` 按 mode 分栏累加。"""
        payload = usage or {}
        stage_view: dict[str, int] = {}
        for key in self.FIELDS:
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            stage_view[key] = int(value)
            self.totals[key] += int(value)
        lines = payload.get("route_lines")
        if isinstance(lines, Mapping):
            for mode, value in lines.items():
                # `blind_spots` 的 route_lines 是个说明性映射（calls/budget/enabled…），
                # 不是按 mode 分栏的账；只认数字，其余原样忽略。
                if mode in {"walking", "riding"} and isinstance(value, (int, float)):
                    self.route_lines[str(mode)] = self.route_lines.get(str(mode), 0) + int(value)
        self.stages[stage] = stage_view

    def as_dict(self, *, elapsed_ms: int) -> dict[str, Any]:
        lines = {"walking": 0, "riding": 0}
        for mode, value in self.route_lines.items():
            lines[mode] = int(value)
        return {
            **{key: int(value) for key, value in self.totals.items()},
            "elapsed_ms": int(elapsed_ms),
            "route_lines": lines,
            "route_lines_total": sum(lines.values()),
            "stages": self.stages,
        }


def aggregate_usage(*, elapsed_ms: int) -> UsageTally:
    """给调用方一个空账本（`run_analysis` 内部也用同一个类）。"""
    return UsageTally()


# --------------------------------------------------------------------------------------
# §6.1 的三条断言（在报告里自证，而不是等验收时手算）
# --------------------------------------------------------------------------------------
def budget_assertions(
    usage: Mapping[str, Any],
    *,
    mode_count: int,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """把本次计量与 §6.1 / FR-107 的上限逐条对照。

    ⚠️ 这是**自证**不是**自夸**：`ok=False` 只会出现在真的超限时，报告照常返回，
    超限原因写进 `notes`。评审拿到的是一份可核对的对账单，而不是一句「已优化」。
    """
    cfg = settings or get_settings()
    limits = cfg.budget_snapshot(max(1, int(mode_count)))
    checks = {
        "place_calls": int(usage.get("place_calls") or 0),
        "quota_units": int(usage.get("quota_units") or 0),
        "http_requests": int(usage.get("http_requests") or 0),
    }
    per_field = {
        key: {"value": value, "limit": limits[key], "ok": value <= limits[key]}
        for key, value in checks.items()
    }
    return {
        "mode_count": int(mode_count),
        "limits": limits,
        "checks": per_field,
        "ok": all(item["ok"] for item in per_field.values()),
        "note": (
            "三栏分开统计：`quota_units` 按 `origins × destinations` 折算、"
            "`http_requests` 是真实往返数，二者不是一回事（PRD §2.2）。"
        ),
    }


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def _profile(platform: str | None) -> tuple[str, dict[str, Any]]:
    key = str(platform or DEFAULT_PLATFORM).strip().lower()
    if key not in PLATFORM_PROFILES:
        key = DEFAULT_PLATFORM
    return key, PLATFORM_PROFILES[key]


def _normalize_modes(modes: Sequence[str] | None) -> list[str]:
    """去重保序，缺省步行；非法值交给 `IsochroneService` 抛 400，本层不吞。"""
    out: list[str] = []
    for item in modes or ("walking",):
        name = str(item or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out or ["walking"]


def _ring_of(isochrone: Mapping[str, Any]) -> list[list[float]]:
    """从 GeoJSON Polygon 取出外环坐标（`[[lng, lat], ...]`）。

    `IsochroneOutcome` 是 frozen dataclass、不带 `.ring`，而 `detect_blind_spots()` 与
    `build_coverage()` 都要环。从 `polygon` 里取是唯一不需要改动 M2 出口的接法；
    顺带去掉 GeoJSON 惯例的闭合重复点，免得它变成一条零长度边。
    """
    polygon = isochrone.get("polygon")
    if not isinstance(polygon, Mapping):
        return []
    coordinates = polygon.get("coordinates")
    if not isinstance(coordinates, Sequence) or isinstance(coordinates, (str, bytes)):
        return []
    if not coordinates:
        return []
    outer = coordinates[0]
    if not isinstance(outer, Sequence) or isinstance(outer, (str, bytes)):
        return []
    ring: list[list[float]] = []
    for point in outer:
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)) or len(point) < 2:
            continue
        try:
            ring.append([float(point[0]), float(point[1])])
        except (TypeError, ValueError):
            continue
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def _flatten_pois(coverage: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """把各维度的 POI 摊平成一份点集，供盲区判定复用（**零额外检索**，D5）。

    圈内圈外**都要**：FR-221 判的是「网格点 1 km 内有没有这类设施」，
    一个刚好落在等时圈外 200 m 的菜市场仍然是「走得到但不算达标」的真实供给。
    """
    out: list[dict[str, Any]] = []
    for group in (coverage or {}).get("groups") or ():
        if not isinstance(group, Mapping):
            continue
        for poi in group.get("pois") or ():
            if isinstance(poi, Mapping):
                out.append(dict(poi))
    return out


async def run_analysis(
    lnglat: Any,
    *,
    modes: Sequence[str] | None = None,
    minutes: float = 15.0,
    categories: Sequence[str] | None = None,
    data_mode: Any = None,
    platform: str | None = None,
    radius_m: int | None = None,
    blind_spot_step_m: float = BLIND_STEP_M_DEFAULT,
    blind_spot_radius_m: float = BLIND_RADIUS_M_DEFAULT,
    include_insufficient: bool = True,
    isochrone_service: IsochroneService | None = None,
    poi_service: PoiSearchService | None = None,
    score_config: ScoreConfig | None = None,
    settings: Settings | None = None,
    archetype: str | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """跑完一次完整体检，返回 PRD §4.7 的报告对象（**永不抛 5xx**）。

    `archetype` 用于把预置样例社区绑定到它的建成形态画像（`old_town` / `new_district` /
    `transit_corridor`）：不指定时合成层会按中心点坐标确定性地挑一个，那样三个样例社区
    的差异就成了「碰巧」，而 FR-412 要求的是**三型社区必然可见的差异**。
    """
    started = perf_counter()
    cfg = settings or get_settings()
    profile_name, profile = _profile(platform)
    use_modes = _normalize_modes(modes)
    primary = use_modes[0]
    tally = UsageTally()
    notes: list[str] = []

    def emit(stage: str, **extra: Any) -> None:
        if callable(progress):
            try:
                progress({"stage": stage, **extra})
            except Exception:
                logger.warning("progress callback failed", exc_info=True)

    iso_service = isochrone_service or get_isochrone_service(cfg)
    poi_svc = poi_service or get_poi_search_service(cfg)
    if archetype:
        # 复用**同一个 client 与 cache**，只换掉示例层画像：换 client 会让路线预算与
        # 三栏计量各算各的（模块 docstring 第 2 条），换 cache 会让命中率无端掉一半。
        poi_svc = PoiSearchService(
            cfg,
            client=getattr(poi_svc, "client", None),
            cache=getattr(poi_svc, "cache", None),
            archetype=str(archetype),
        )
    if score_config is None:
        score_config, config_problems = load_score_config(cfg.weights_path)
        notes.extend(config_problems)

    # 轻量档（FR-107）：只影响等时圈那一半，POI 与盲区口径不变。
    iso_config = (
        IsochroneConfig(refine=False, build_duration_field=False)
        if profile["lightweight"]
        else None
    )

    # ---- ① 等时圈：逐个 mode 各算一个环 ------------------------------------------------
    isochrones: dict[str, dict[str, Any]] = {}
    for index, mode in enumerate(use_modes):
        emit("isochrone", mode=mode, index=index + 1, total=len(use_modes))
        outcome = await iso_service.compute(
            lnglat, mode=mode, minutes=minutes, data_mode=data_mode, config=iso_config
        )
        payload = outcome.as_dict()
        isochrones[mode] = payload
        tally.add(f"isochrone:{mode}", payload.get("usage"))
        if payload.get("degraded") and payload.get("degraded_reason"):
            notes.append(
                f"{mode} 等时圈降级（{payload['degraded_reason']}）："
                f"{payload.get('message') or '详见 isochrones 段'}"
            )
    primary_iso = isochrones[primary]
    ring = _ring_of(primary_iso)
    if not ring:
        notes.append(
            "主模式等时圈没有可用多边形（上游降级或返回空），落圈判定将按「全部圈外」处理，"
            "`count_in` 会全为 0 —— 这是缺数据，不是「这个社区没有设施」。"
        )

    # ---- ② POI：只检索一次，半径由主模式决定（FR-206 / D5）-----------------------------
    emit("poi", mode=primary)
    poi_result = await poi_svc.search(
        primary_iso.get("center") or lnglat,
        radius_m=radius_m,
        mode=primary,
        categories=list(categories) if categories else None,
        data_mode=data_mode,
    )
    tally.add("poi", poi_result.get("usage"))

    # ---- ③ 落圈 + 前 K 实测（1 次批量算路）--------------------------------------------
    # 🔴 测量层必须与等时圈层**同层**：等时圈已经是合成圈（`data_source == "sample"`）时，
    #    再拿真探针去测 POI 只会得到一堆「测不到」——那时报告等于没有时效数据。
    #    反过来，等时圈是实时圈时也绝不用合成时间冒充实测（那是本泳道唯一的假绿路径）。
    probe_source = "measured"
    probe: Any = None
    if ring:
        if str(primary_iso.get("data_source")) == "sample":
            probe = SyntheticCoverageProbe(
                primary_iso.get("center") or lnglat, target_seconds=float(minutes) * 60.0
            )
            probe_source = "synthetic"
        else:
            probe = _SharedProbe(iso_service)
    emit("coverage", k=profile["k"], probe=probe_source)
    coverage = await build_coverage(
        poi_result,
        ring,
        probe=probe,
        modes=use_modes,
        k=int(profile["k"]),
        center=primary_iso.get("center") or lnglat,
        threshold_overrides=score_config.dimension_overrides or None,
        t_ideal_ratio=score_config.t_ideal_ratio,
        t_ideal_minutes=score_config.t_ideal_minutes,
    )
    tally.add("coverage", coverage.get("usage"))
    notes.extend(coverage.get("notes") or [])
    if probe_source == "synthetic":
        notes.append(
            "等时圈本次由**程序合成的示例数据**给出（非实时路网），因此设施实测耗时同样取"
            "同源的合成速度场，以保证报告内部自洽；计量中的出网数与配额消耗如实为 0。"
        )

    # ---- ④ 盲区：复用 ③ 的点集，0 次出网 ----------------------------------------------
    emit("blind_spots")
    blind_spots = detect_blind_spots(
        _flatten_pois(coverage),
        ring,
        step_m=float(blind_spot_step_m),
        radius_m=float(blind_spot_radius_m),
        include_insufficient=bool(include_insufficient),
        center=primary_iso.get("center") or lnglat,
    )
    tally.add("blind_spots", blind_spots.get("usage"))
    notes.extend(blind_spots.get("notes") or [])

    # ---- ⑤ 评分 + 摘要 ----------------------------------------------------------------
    emit("score")
    data_source = _overall_source([primary_iso.get("data_source"), poi_result.get("data_source")])
    degraded_reason = _overall_reason(
        primary_iso.get("degraded_reason"), poi_result.get("degraded_reason")
    )
    score = build_score_block(coverage, blind_spots, config=score_config)
    summary = build_summary(
        coverage,
        blind_spots,
        score,
        minutes=minutes,
        data_source=data_source,
        degraded_reason=degraded_reason,
        max_items=score_config.summary_max,
    )

    elapsed_ms = int((perf_counter() - started) * 1000)
    usage = tally.as_dict(elapsed_ms=elapsed_ms)
    assertions = budget_assertions(usage, mode_count=len(use_modes), settings=cfg)
    if not assertions["ok"]:
        over = [key for key, item in assertions["checks"].items() if not item["ok"]]
        notes.append(f"本次超出 §6.1 预算上限：{', '.join(over)}；报告照常返回，但结果可能不完整。")

    report: dict[str, Any] = {
        "ok": True,
        "meta": {
            "center": primary_iso.get("center"),
            "mode": primary,
            "modes": list(use_modes),
            "minutes": float(minutes),
            "platform": profile_name,
            "lightweight": bool(profile["lightweight"]),
            "k": int(profile["k"]),
            "data_mode": primary_iso.get("data_mode"),
            "data_source": data_source,
            "degraded": bool(primary_iso.get("degraded")) or bool(poi_result.get("degraded")),
            "degraded_reason": degraded_reason,
            "coordinate_system": "bd09ll",
            "generated_at": _now_iso(),
            "source_declaration": (
                "数据来源：百度地图开放平台（地理编码 / 地点检索 / 批量算路）。"
                "示例数据由本程序合成，不含任何百度返回内容。"
            ),
        },
        "isochrone": primary_iso,
        "isochrones": isochrones,
        "coverage": {
            "groups": coverage.get("groups"),
            "totals": coverage.get("totals"),
            "meta": coverage.get("meta"),
        },
        "blind_spots": {
            **{key: value for key, value in blind_spots.items() if key != "notes"},
            "geojson": blocks_to_geojson(blind_spots),
        },
        "score": score,
        "summary": summary,
        "usage": usage,
        "assertions": assertions,
        "notes": notes,
    }
    log_fields(
        logger,
        logging.INFO,
        "analysis_done",
        mode=primary,
        modes=len(use_modes),
        place_calls=usage["place_calls"],
        quota_units=usage["quota_units"],
        http_requests=usage["http_requests"],
        elapsed_ms=elapsed_ms,
        total=score.get("total"),
        grade=score.get("grade"),
        blind_spots=blind_spots.get("count"),
        assertions_ok=assertions["ok"],
    )
    return report


def _overall_source(sources: Sequence[Any]) -> str:
    """整份报告的数据来源：只要有 `sample` 就是 `sample`，全 `live` 才是 `live`。

    取最差的一层而不是最常见的一层：报告是整体交付物，一部分实时、一部分合成时，
    标 `live` 会让评审以为等时圈和 POI 都是真实调用（FR-306 的目的正相反）。
    """
    seen = [str(item) for item in sources if item]
    for level in ("sample", "cache", "live"):
        if level in seen:
            return level
    return "live"


def _overall_reason(*reasons: Any) -> str | None:
    """取第一条非空降级原因（顺序即上游优先级：等时圈在前）。"""
    for reason in reasons:
        if reason:
            return str(reason)
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
