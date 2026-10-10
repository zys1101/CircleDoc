"""POI 检索层（M3）：六维度 + 便民参考项的圆形区域检索编排。

与 `services/geo.py` / `services/isochrone_service.py` **同口径**的三层降级链
（live → cache → sample），HTTP、令牌桶、退避、熔断、三栏计量全部委托 M1 的
`infra/baidu_client.py`——本层一行网络代码都不写（条款 3.2.3(ii) 禁止对百度线上服务
压测；所有验证走 `httpx.MockTransport` + 假 AK + socket 守卫）。

四条决定本文件形状的硬事实：

1. 🔴 **一个维度一次请求**（PRD §4.3 第 276 行）：`query` 用 `$` 并集多关键字，
   六维度 + 便民参考 = 7 次，`place_calls` 上限 8（`_budget()`）。词表本身在
   `services/poi_categories.py`，那里有逐词自检；本层构造时调一次 `assert_wordlist()`，
   词表写歪就**建不出服务对象**，不会跑出一份「某维度永远 0」的假报告。
2. 🔴 **缓存键必须与客户端逐字段一致**：`_params_for_place()` 是
   `BaiduClient.search_place()` 圆形分支的参数镜像（含 `coord_type` 是**数字** 3、
   `radius_limit=True`、`page_size=10`、`page_num=1`）。差一个字段 = 换一把 key，
   表现是「实时写进去、下次读不到」，命中率静默归零。缓存读在本层做一次，
   因为 `data_mode=mock` 时客户端的实时闸门在查缓存**之前**就抛异常（见 `geo.py` 模块注释）。
3. 🔴 **计数分两套口径，不许互相替换**：圈内计数取返回体 `total`（官方上限 150），
   `pois` 明细只有第一页（最多 `PAGE_SIZE=10` 条），且**一律不翻页**。
   `count_in` / `count_near` / `nearest_minutes` 本层**预留键不计算**，
   由 `services/coverage.py`（M3 另一泳道，落圈射线法 + 批量算路实测）回填。
4. 🔴 **空结果是结论不是故障**：检索返回空 → `count_total=0` +
   `empty_reason="检索无结果"`，流程继续；同时 `notes` 里带上 FAQ 2536
   「检索接口会过滤部分数据、计数可能被系统性低估」的官方口径说明，
   避免评审把「0 个」当成本项目的 bug。

跨泳道只传 **plain dict**（契约见 `PoiHit` / `CategoryGroup` / `PoiResultSet` 三段注释），
不导出 dataclass——并行开发时 import 依赖会互相阻塞。
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from infra.baidu_client import BaiduClient, to_baidu_latlng
from infra.cache_store import CacheStore, get_cache_store
from infra.config import (
    DataMode,
    DataSource,
    EffectiveDataMode,
    Settings,
    get_settings,
)
from infra.errors import OK_STATUS, CircleDocError
from infra.log import get_logger, log_fields
from infra.ratelimit import logical_bucket_name

from services.geo import InvalidGeoParam, OutsideServiceArea, parse_lnglat
from services.poi_categories import (
    BLIND_TRIO_SUBCATS,
    COORD_TYPE_BD09LL_NUMERIC,
    DIMENSION_ORDER,
    EMPTY_REASON_NO_RESULT,
    NOTE_FAQ_2536_FILTERING,
    NOTE_NO_PAGINATION,
    NOTE_RADIUS_LIMIT,
    PAGE_NUM,
    PAGE_SIZE,
    PLACE_CALLS_PER_ANALYSIS,
    PLACE_RADIUS_MAX_M,
    SCOPE_DETAIL,
    SUBCAT_LABEL_CN,
    SUBCAT_OFFICIAL_PATH,
    TOTAL_CAP,
    TRANSPORT_MODES,
    WordlistError,
    assert_wordlist,
    dimensions_for,
    normalize_subcat,
    search_radius_for_mode,
)

__all__ = [
    "ARCHETYPES",
    "EMPTY_REASON_NO_RESULT",
    "InvalidPoiParam",
    "PoiSearchService",
    "SyntheticPoiSamples",
    "get_poi_search_service",
    "reset_poi_search_service",
    "subcats_of_dimension",
]

logger = get_logger("circledoc.services.poi_search")

PLACE_SERVICE = logical_bucket_name("place")
"""缓存与限流用的**规范服务名**（`"place"`）。

必须与 `BaiduClient` 的 `CallPlan("place", ...).bucket` 同一个值：那边按桶名写缓存、
这边按桶名读缓存，写成 `"poi"` / `"place_search"` 之类就是第二把 key。
TTL 不在本层指定——`infra/cache_keys.py` 的 POI 档（24 h 合规上限）统一裁决。
"""

COORD_SYSTEM_NOTE = "坐标一律 BD09LL，`lnglat` 顺序是 [经度, 纬度]。"

MSG_LIVE_OK = "已获取实时检索结果。"
MSG_CACHE_HIT = "该结果来自本地缓存，未发起实时调用，也不消耗今日配额。"
MSG_SAMPLE = "实时检索本次不可用，已降级到合成示例层（程序生成，不含任何百度返回内容）。"
MSG_MOCK_MODE = "当前为离线（mock）模式，不发起实时检索；如需真实数据请配置服务端 AK。"
MSG_BUDGET_STOP = "地点检索出网预算（单次分析 ≤ 8 次）已用尽，后续维度改用缓存或示例数据。"
MSG_EMPTY = "该维度检索无结果，计数按 0 记；0 读作「检索未见」而非「绝对没有」（FAQ 2536）。"

REASON_BUDGET = "place_call_budget"
REASON_MOCK = "mock_mode"
REASON_LIVE_FAILED = "live_failed"

SKIP_NOTE_BUDGET = "place_call_budget"
"""`CategoryGroup.skipped_reason` 的取值（**契约外的追加字段**，只加键不改键）。"""


# --------------------------------------------------------------------------------------
# 入参错误（沿用 services/geo.py 的 400 口径，不自创错误形状）
# --------------------------------------------------------------------------------------
class InvalidPoiParam(InvalidGeoParam):
    """中心点 / 模式 / 维度键不合法：`api/app.py` 已把基类映射成统一 400 信封。"""

    error_id = "invalid_poi_param"
    user_message = "POI 检索的提交内容不符合要求，请检查中心点或维度后重试。"


# --------------------------------------------------------------------------------------
# 内部记账（不外露：跨泳道只走 `usage` 字典）
# --------------------------------------------------------------------------------------
@dataclass
class _Tally:
    place_calls: int = 0
    http_requests: int = 0
    cache_hits: int = 0
    quota_units: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "place_calls": self.place_calls,
            "http_requests": self.http_requests,
            "cache_hits": self.cache_hits,
            "quota_units": self.quota_units,
        }

    def absorb(
        self,
        result: Any,
        *,
        cache_hit: bool = False,
    ) -> None:
        """把一次 `BaiduResult` 的**本次**三栏并进账本（缓存命中时全为 0，不是漏账）。"""
        if result is None:
            self.cache_hits += 1 if cache_hit else 0
            return
        self.place_calls += int(getattr(result, "place_calls", 0) or 0)
        self.http_requests += int(getattr(result, "http_requests", 0) or 0)
        self.quota_units += int(getattr(result, "quota_units", 0) or 0)
        if cache_hit or bool(getattr(result, "from_cache", False)):
            self.cache_hits += 1


# --------------------------------------------------------------------------------------
# 示例层：程序合成的三类社区形态（FR-102 / FR-412；条款 3.3.4）
# --------------------------------------------------------------------------------------
ARCHETYPES: tuple[str, ...] = ("old_town", "new_district", "transit_corridor")
"""三种典型社区：**必须肉眼可辨**。`old_town` 配套密集（盲区少）、
`new_district` 只有零星便利店与公交站（市场 / 药店 / 小学 缺 1~2 类 → FR-224 配套不足）、
`transit_corridor` 轨交枢纽但生活配套几乎为零（市场 / 药店 / 小学 三者皆无 → FR-221 完全盲区）。
每个形态的**逐 subcat 条数**见 `_ARCHETYPE_COUNTS`，测试直接断言差异。"""

_ARCHETYPE_COUNTS: dict[str, dict[str, int]] = {
    "old_town": {
        "pharmacy": 3,
        "hospital": 1,
        "clinic": 2,
        "primary_school": 2,
        "kindergarten": 2,
        "market": 2,
        "supermarket": 3,
        "convenience": 4,
        "park": 1,
        "plaza": 1,
        "fitness": 1,
        "sanatorium": 1,
        "bus_stop": 6,
        "metro_station": 2,
        "restaurant": 9,
        "snack": 6,
        "bank": 2,
        "atm": 3,
        "repair": 2,
    },
    "new_district": {
        "pharmacy": 1,
        "hospital": 0,
        "clinic": 0,
        "primary_school": 0,
        "kindergarten": 1,
        "market": 0,
        "supermarket": 1,
        "convenience": 2,
        "park": 1,
        "plaza": 0,
        "fitness": 0,
        "sanatorium": 0,
        "bus_stop": 2,
        "metro_station": 0,
        "restaurant": 3,
        "snack": 2,
        "bank": 0,
        "atm": 1,
        "repair": 0,
    },
    "transit_corridor": {
        # 市场 / 药店 / 小学 三档**全 0** → FR-221 的完全盲区样本；
        # 交通类刻意给到最多，形成「能上车、买不到菜」的对比。
        "pharmacy": 0,
        "hospital": 0,
        "clinic": 0,
        "primary_school": 0,
        "kindergarten": 0,
        "market": 0,
        "supermarket": 1,
        "convenience": 1,
        "park": 0,
        "plaza": 1,
        "fitness": 0,
        "sanatorium": 0,
        "bus_stop": 8,
        "metro_station": 4,
        "restaurant": 4,
        "snack": 3,
        "bank": 1,
        "atm": 2,
        "repair": 0,
    },
}

SAMPLE_GRID_STEP_M = 180
"""虚构网格的间距（米）。坐标以**中心点为原点**按 45° 方位 + 环数外推，
不是任何真实地址，也绝不来自百度返回（笔记 §7.3 方案 A）。"""

SAMPLE_TOTAL_CAP = TOTAL_CAP
"""与官方 `total` 同上限（FAQ 2534 / 2543），好让「total 大于明细条数」这条口径在示例层也可演示。"""


class SyntheticPoiSamples:
    """示例层：按社区形态**算法生成**一份百度形状的空报文，交给同一条解析路径。

    为什么合成「百度形状」而不是直接合成 `PoiHit`：这样 live / cache / sample 三层共用
    同一个解析器，示例数据也就顺带验证了 `tag` 与 `classified_poi_tag` **两种来源**的归一
    （FR-214）。🔴 内容全部程序生成：名称 `样例-药店-01`、地址「示例市示例区示范路 N 号」、
    adcode `000000`、坐标落虚构网格，一眼可辨、不可被误读成真实设施。
    """

    def __init__(self, archetype: str | None = None) -> None:
        self.archetype = archetype if archetype in _ARCHETYPE_COUNTS else None

    # 供 `services/geo.py` 风格的注入协议（`SampleProvider.get(service, params)`）
    def get(self, service: str, params: Mapping[str, Any]) -> dict[str, Any]:
        words = [item for item in str(params.get("query", "")).split("$") if item]
        counts = _ARCHETYPE_COUNTS[self.archetype or self.archetype_for_center(params)]
        results: list[dict[str, Any]] = []
        for order, word in enumerate(words):
            subcat = normalize_subcat(word)
            if subcat == "other":
                continue
            count = int(counts.get(subcat, 0))
            for index in range(count):
                results.append(self._entry(subcat, index, order, params))
        return {
            "status": OK_STATUS,
            "total": min(SAMPLE_TOTAL_CAP, len(results)),
            "results": results[:PAGE_SIZE],
        }

    def archetype_for_center(self, params: Mapping[str, Any]) -> str:
        """没指定形态时按中心点**确定性**挑一个（同一天同一社区必须同一个结果，FR-301 可复算）。"""
        text = str(params.get("location", ""))
        parts = [item.strip() for item in text.split(",") if item.strip()]
        if len(parts) != 2:
            return ARCHETYPES[0]
        try:
            lat, lng = float(parts[0]), float(parts[1])
        except ValueError:
            return ARCHETYPES[0]
        buckets = int(abs(lng) * 1000) + int(abs(lat) * 1000)
        return ARCHETYPES[buckets % len(ARCHETYPES)]

    def _entry(
        self, subcat: str, index: int, order: int, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        label = SUBCAT_LABEL_CN.get(subcat, subcat)
        path = SUBCAT_OFFICIAL_PATH.get(subcat, "")
        lng, lat = self._grid_point(subcat, index, params)
        seq = f"{index + 1:02d}"
        # 两种官方 tag 字段轮流出现：`tag="药店"` 与 `classified_poi_tag="医疗 | 药店"`，
        # 让示例层同时覆盖 FR-214 的两条判定路径。
        tag_fields: dict[str, str] = (
            {"tag": label}
            if (index + order) % 2 == 0
            else {"classified_poi_tag": path.replace(" > ", " | "), "tag": ""}
        )
        return {
            "name": f"样例-{label}-{seq}",
            "uid": f"sample-{subcat}-{seq}",
            "location": {"lng": lng, "lat": lat},
            "address": f"示例市示例区示范路{100 + index * 7}号",
            "province": "示例市",
            "city": "示例市",
            "area": "示例区",
            "town": "样例街道",
            "adcode": "000000",
            "detail_info": {
                "type": "poi",
                "distance": "",  # 🔴 空串：官方 distance 缺失 → 走本地 haversine 兜底
                "tag": tag_fields.get("tag", ""),
                "classified_poi_tag": tag_fields.get("classified_poi_tag", ""),
                "label": "合成示例数据（非真实设施）",
            },
        }

    def _grid_point(
        self, subcat: str, index: int, params: Mapping[str, Any]
    ) -> tuple[float, float]:
        """以中心点为原点的虚构网格点（BD09LL，`[经度, 纬度]` 由上层再折）。"""
        parts = [
            item.strip() for item in str(params.get("location", "")).split(",") if item.strip()
        ]
        lat0, lng0 = (39.915, 116.404)
        if len(parts) == 2:
            with contextlib.suppress(ValueError):
                lat0, lng0 = float(parts[0]), float(parts[1])
        slot = _SLOT.get(subcat, 0)
        ring = index // 8
        spoke = (index + slot) % 8
        bearing = math.radians(spoke * 45)
        distance = SAMPLE_GRID_STEP_M * (ring + 1)
        d_lat = distance * math.cos(bearing) / 110574.0
        d_lng = distance * math.sin(bearing) / (111320.0 * max(0.1, math.cos(math.radians(lat0))))
        return (round(lng0 + d_lng, 6), round(lat0 + d_lat, 6))


_SLOT: dict[str, int] = {subcat: order for order, subcat in enumerate(SUBCAT_OFFICIAL_PATH)}
"""给每个 subcat 一个固定「起始方位」，避免同类 POI 全挤在同一条射线上（视觉可辨要求）。"""


def subcats_of_dimension(key: str) -> list[str]:
    """维度键 → 该维度的 subcat 列表（`services/coverage.py` 回填计数时按这个分组）。"""
    return list(dimensions_for([key])[0]["subcats"])


# --------------------------------------------------------------------------------------
# 编排层
# --------------------------------------------------------------------------------------
class PoiSearchService:
    """POI 检索的业务流程：校验 → 三层降级 → 出契约字段。拼百度参数与算缓存键都在 `infra/`。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        cache: CacheStore | None = None,
        samples: Any | None = None,
        archetype: str | None = None,
    ) -> None:
        """依赖全部可注入：自测传 `BaiduClient(ak=假串, transport=httpx.MockTransport(...))`
        就能在 0 次真实出网的前提下把整条降级链跑完（条款 3.2.3(ii)）。

        🔴 构造时调 `assert_wordlist()`：词表里只要混进一个官方分类表**不存在**的词，
        那个维度会永远计数 0、被误判成「缺配套」并污染盲区结论（PRD §4.3 第 288 行）。
        与其产出一份好看的假报告，不如在这里就建不起来。
        """
        assert_wordlist()
        cfg = settings or get_settings()
        self.settings = cfg
        self.cache: CacheStore | None = cache
        self.samples = samples if samples is not None else SyntheticPoiSamples(archetype)
        if client is not None:
            self.client: Any = client
        else:
            if self.cache is None:
                self.cache = get_cache_store(cfg)
            self.client = BaiduClient(cfg, cache=self.cache)
        if getattr(self.client, "cache", None) is None and self.cache is not None:
            # 缓存只允许一套口径（与 `GeoService.__init__` 同一刀）：注入的客户端若没接缓存，
            # 实时结果不落库，表现是「命中率 0 但没人报错」。
            self.client.cache = self.cache

    # ------------------------------------------------------------------ 对外唯一入口
    async def search(
        self,
        center: Any,
        *,
        radius_m: int | None = None,
        mode: str = "walking",
        categories: list[str] | tuple[str, ...] | None = None,
        data_mode: DataMode | None = None,
    ) -> dict[str, Any]:
        """按维度做圆形区域检索，返回 `PoiResultSet`（**plain dict**，见模块注释）。

        `radius_m` 缺省 = `R_max × 1.2`（步行 1500 m / 骑行 4800 m），显式传值只会
        **收紧**并夹到官方 10 km 上限内；`categories` 缺省 = 全七项（六维度 + 便民参考项），
        顺序固定为 `DIMENSION_ORDER`，保证同一份输入产出同一份结果（FR-301 可复算）。
        `data_mode` 是**契约外的可选参数**（默认不改变任何行为），只为 `/api/poi`
        与自测提供 PRD §4.6 的取数模式开关。
        """
        use_mode = self._require_mode(mode)
        lng, lat = self._coerce_center(center)
        self._require_domestic(lng, lat)
        radius = self._require_radius(radius_m, use_mode)
        specs = self._require_specs(categories)
        layer = self._resolve_mode(data_mode)
        budget = self._budget()

        tally = _Tally()
        groups: list[dict[str, Any]] = []
        reasons: list[str] = []
        skipped: list[str] = []
        for spec in specs:
            outcome = await self._resolve_dimension(
                spec,
                lng=lng,
                lat=lat,
                radius=radius,
                layer=layer,
                budget=budget,
                tally=tally,
            )
            groups.append(outcome)
            if outcome["skipped_reason"] == SKIP_NOTE_BUDGET:
                skipped.append(str(spec["key"]))
            elif outcome["degraded_reason"]:
                reasons.append(str(outcome["degraded_reason"]))

        source = self._overall_source(groups)
        degraded = source != "live" or bool(skipped)
        degraded_reason = self._overall_reason(source=source, skipped=skipped, reasons=reasons)
        notes = self._notes(source=source, skipped=skipped, groups=groups)

        result: dict[str, Any] = {
            "center": [lng, lat],
            "mode": use_mode,
            "radius_m": radius,
            "groups": groups,
            "total_pois": sum(len(group["pois"]) for group in groups),
            "data_source": source,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "usage": tally.as_dict(),
            "notes": notes,
            # ---- 契约外的追加字段（只加键、不改键；供 meta / README 与 FR-306 徽标使用）
            "coordinate_system": "bd09ll",
            "place_call_budget": budget,
            "dimensions_requested": [str(spec["key"]) for spec in specs],
            "skipped_dimensions": skipped,
            "blind_trio_subcats": list(BLIND_TRIO_SUBCATS),
            "sample_archetype": getattr(self.samples, "archetype", None),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        log_fields(
            logger,
            logging.INFO,
            "poi_searched",
            mode=use_mode,
            layer=source,
            radius_m=radius,
            dims=len(groups),
            place_calls=tally.place_calls,
            http_requests=tally.http_requests,
            cache_hits=tally.cache_hits,
        )
        return result

    # ------------------------------------------------------------------ 入口校验
    def _require_mode(self, value: Any) -> str:
        mode = str(value or "").strip().lower()
        if mode not in TRANSPORT_MODES:
            raise InvalidPoiParam(
                f"出行方式只支持 {'/'.join(TRANSPORT_MODES)}，收到 {value!r}。",
                field_name="mode",
                value=str(value),
            )
        return mode

    def _coerce_center(self, value: Any) -> tuple[float, float]:
        if isinstance(value, str):
            return parse_lnglat(value)
        if isinstance(value, Mapping):
            lng, lat = value.get("lng", value.get("longitude")), value.get(
                "lat", value.get("latitude")
            )
            if lng is None or lat is None:
                raise InvalidPoiParam(
                    "中心点缺少 lng 或 lat。", field_name="center", value=repr(value)
                )
            return self._checked(float(lng), float(lat), repr(value))
        if isinstance(value, (list, tuple)) and len(value) == 2:
            # 内部元组一律 (lng, lat)，与 `services/geo.py` 同一约定；递给百度的
            # 「纬度,经度」由 `to_baidu_latlng` 换算，只在那一处发生一次。
            return self._checked(float(value[0]), float(value[1]), repr(value))
        raise InvalidPoiParam(
            "中心点应写成 lnglat 字符串、{lng,lat} 或 [lng,lat]。",
            field_name="center",
            value=repr(value),
        )

    def _checked(self, lng: float, lat: float, raw: Any) -> tuple[float, float]:
        if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
            raise InvalidPoiParam(
                "中心点坐标超出合法区间（经度 -180~180、纬度 -90~90）。",
                field_name="center",
                value=str(raw),
            )
        return (lng, lat)

    def _require_domestic(self, lng: float, lat: float) -> None:
        """境外坐标沿用 M1 的 400 口径（`services.geo.OutsideServiceArea`，不另编一套）。

        个人认证开发者对境外地点检索的日配额是「-」（笔记 §3.1）：发出去只会收到鉴权错误
        并白烧当日 100 次里的 7 次，所以入参就越界就先拒。
        """
        cfg = self.settings
        if not (
            cfg.china_lng_min <= lng <= cfg.china_lng_max
            and cfg.china_lat_min <= lat <= cfg.china_lat_max
        ):
            raise OutsideServiceArea(lnglat=(lng, lat), source="place")

    def _require_radius(self, override: int | None, mode: str) -> int:
        try:
            return search_radius_for_mode(mode, override)
        except WordlistError as exc:
            raise InvalidPoiParam(str(exc), field_name="radius_m", value=override) from exc

    def _require_specs(
        self, categories: list[str] | tuple[str, ...] | None
    ) -> list[dict[str, Any]]:
        if categories is not None:
            unknown = [str(key) for key in categories if key not in DIMENSION_ORDER]
            if unknown:
                raise InvalidPoiParam(
                    f"未知维度 {unknown}；可用：{', '.join(DIMENSION_ORDER)}。",
                    field_name="categories",
                    value=unknown,
                )
        return dimensions_for(categories)

    def _budget(self) -> int:
        """单次分析地点检索出网上限：配置值再夹合规硬上限 8（PRD §4.3 第 294 行）。"""
        configured = int(getattr(self.settings, "max_place_calls_per_analysis", 0) or 0)
        if configured <= 0:
            configured = PLACE_CALLS_PER_ANALYSIS
        return max(1, min(configured, PLACE_CALLS_PER_ANALYSIS))

    def _resolve_mode(self, requested: DataMode | None) -> EffectiveDataMode:
        if requested in ("live", "cache", "mock"):
            return str(requested)  # type: ignore[return-value]
        return self.settings.data_mode_in_effect

    # ------------------------------------------------------------------ 参数镜像
    def _params_for_place(self, spec: Mapping[str, Any], lng: float, lat: float, radius: int):
        """🔴 `BaiduClient.search_place()` 圆形分支的参数**逐字段镜像**（缓存键同源）。

        与客户端不一致的字段不会报错，只会让「实时写入 → 下次命中」变成 miss，
        表现是缓存命中率静默归零。`coord_type` 是数字 3（不是字符串，也不是
        逆地理的 `coordtype`），`radius_limit=True` 必带（FAQ 2544：半径跨城会
        退化为城市范围检索），`page_num` 恒 1（不翻页），
        `scope=SCOPE_DETAIL` 必带（见 `poi_categories.SCOPE_DETAIL`：不带它就没有
        `detail_info`，整条分类链路失效）。
        """
        return {
            "query": str(spec["query"]),
            "output": "json",
            "coord_type": COORD_TYPE_BD09LL_NUMERIC,
            "page_size": PAGE_SIZE,
            "page_num": PAGE_NUM,
            "location": to_baidu_latlng({"lng": lng, "lat": lat}),
            "radius": min(max(1, int(radius)), PLACE_RADIUS_MAX_M),
            "radius_limit": True,
            "scope": SCOPE_DETAIL,
        }

    # ------------------------------------------------------------------ 单个维度：三层链
    async def _resolve_dimension(
        self,
        spec: Mapping[str, Any],
        *,
        lng: float,
        lat: float,
        radius: int,
        layer: EffectiveDataMode,
        budget: int,
        tally: _Tally,
    ) -> dict[str, Any]:
        params = self._params_for_place(spec, lng, lat, radius)
        key = str(spec["key"])

        # ① 缓存优先：读缓存不花配额，`mock` 模式也照样读（与 `geo.py` 同一层理由）。
        if layer != "cache":
            cached = await self._cache_read(params)
            if cached is not None:
                return self._group(
                    spec,
                    cached,
                    source="cache",
                    tally=tally,
                    degraded_reason=None,
                    center=(lng, lat),
                )
        if layer == "mock":
            return await self._sample_group(
                spec, params, tally, reason=REASON_MOCK, center=(lng, lat)
            )
        if layer == "cache":
            return await self._sample_group(
                spec, params, tally, reason="cache_mode_no_entry", center=(lng, lat)
            )

        # ② 预算守卫：超限**截停并如实报告**（返回结果对象 + 原因），不抛异常、
        #    也不静默少跑维度——`skipped_dimensions` 与 `notes` 里都看得见。
        if tally.place_calls + 1 > budget:
            return await self._sample_group(
                spec,
                params,
                tally,
                reason=REASON_BUDGET,
                skipped=SKIP_NOTE_BUDGET,
                center=(lng, lat),
            )

        # ③ 实时：客户端内部还有一道同样的预算闸（`_budget_block`），返回 blocked 信号而不是
        #    抛异常，这里与 CircleDocError 一起收敛成「降级到示例层」。
        try:
            result = await self.client.search_place(
                str(spec["query"]),
                location={"lng": lng, "lat": lat},
                radius=radius,
                page_size=PAGE_SIZE,
                page_num=PAGE_NUM,
                radius_limit=True,
                # 🔴 缺了 `scope=2` 就没有 `detail_info`，`tag`/`classified_poi_tag` 全空，
                # 每个 POI 都判成 other → 盲区判定把配套密集的社区报成 95% 完全盲区。
                # 见 `services/poi_categories.py:SCOPE_DETAIL` 的实测记录。
                scope=SCOPE_DETAIL,
            )
        except CircleDocError as exc:
            log_fields(logger, logging.WARNING, "poi_live_rejected", dim=key, code=exc.error_id)
            return await self._sample_group(
                spec, params, tally, reason=exc.error_id, center=(lng, lat)
            )
        except Exception as exc:  # 任何非预期异常也不冒（FR-403 任一层失败不中断流程）
            log_fields(logger, logging.ERROR, "poi_live_failed", dim=key, error=type(exc).__name__)
            tally.absorb(None)
            return await self._sample_group(
                spec, params, tally, reason=REASON_LIVE_FAILED, center=(lng, lat)
            )

        tally.absorb(result)
        if getattr(result, "blocked", False):
            reason = (
                REASON_BUDGET
                if getattr(result, "budget_exhausted", False)
                else (getattr(result, "blocked_reason", None) or "blocked")
            )
            return await self._sample_group(spec, params, tally, reason=reason, center=(lng, lat))

        payload = dict(getattr(result, "data", {}) or {})
        if int(payload.get("status", OK_STATUS) or 0) != OK_STATUS:
            return await self._sample_group(
                spec,
                params,
                tally,
                reason=f"place_status_{payload.get('status')}",
                center=(lng, lat),
            )
        source: DataSource = "cache" if getattr(result, "from_cache", False) else "live"
        return self._group(
            spec,
            payload,
            source=source,
            tally=tally,
            degraded_reason=None,
            center=(lng, lat),
        )

    # ------------------------------------------------------------------ 缓存 / 示例
    async def _cache_read(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        cache = self.cache if self.cache is not None else getattr(self.client, "cache", None)
        if cache is None:
            return None
        try:
            outcome = await cache.get_async(PLACE_SERVICE, params)
        except Exception as exc:  # 缓存层坏了不能连带打死检索（FR-403）
            log_fields(logger, logging.WARNING, "poi_cache_read_failed", error=type(exc).__name__)
            return None
        payload = getattr(outcome, "payload", None)
        hit = bool(getattr(outcome, "hit", False))
        if isinstance(outcome, Mapping):  # 兼容「直接返回 payload」的外壳（同 baidu_client）
            payload, hit = outcome, bool(outcome)
        if not hit or not isinstance(payload, Mapping):
            return None
        if int(payload.get("status", OK_STATUS) or 0) != OK_STATUS:
            return None
        return dict(payload)

    async def _sample_payload(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        samples = self.samples
        if samples is None:
            return None
        try:
            raw = samples.get(PLACE_SERVICE, params)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:
            log_fields(logger, logging.WARNING, "poi_sample_failed", error=type(exc).__name__)
            return None
        return dict(raw) if isinstance(raw, Mapping) else None

    async def _sample_group(
        self,
        spec: Mapping[str, Any],
        params: Mapping[str, Any],
        tally: _Tally,
        *,
        reason: str,
        skipped: str | None = None,
        center: tuple[float, float] = (0.0, 0.0),
    ) -> dict[str, Any]:
        payload = await self._sample_payload(params)
        group = self._group(
            spec,
            payload or {},
            source="sample",
            tally=tally,
            degraded_reason=reason,
            skipped_reason=skipped,
            center=center,
        )
        if payload is None:
            group["empty_reason"] = None
            group["notes_extra"] = "示例层不可用：本维度按 0 条记，仍不算失败。"
        return group

    # ------------------------------------------------------------------ 解析（三层共用）
    def _group(
        self,
        spec: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        source: DataSource,
        tally: _Tally,
        degraded_reason: str | None,
        center: tuple[float, float],
        skipped_reason: str | None = None,
    ) -> dict[str, Any]:
        hits, dropped = self._hits(payload, spec, source=source, center=center)
        total = self._total(payload, count=len(hits))
        empty_reason = EMPTY_REASON_NO_RESULT if not hits and skipped_reason is None else None
        return {
            "key": str(spec["key"]),
            "label": str(spec["label"]),
            "weight": float(spec["weight"]),
            "threshold_count": int(spec["threshold_count"]),
            "threshold_minutes": float(spec["threshold_minutes"]),
            "scored": bool(spec["scored"]),
            "query": str(spec["query"]),
            "official_paths": list(spec["official_paths"]),
            "pois": hits,
            # 🔴 落圈计数由 `services/coverage.py`（M3 另一泳道）回填：
            #   count_in   = 落在等时圈多边形内的条数（射线法，纯本地零 API）
            #   count_near = 直线半径内的条数
            #   nearest_minutes = 该维度最近设施的**实测**步行分钟（1 次批量算路）
            # 本层只预留键并初始化为 0/None，绝不猜值。
            "count_in": 0,
            "count_near": 0,
            "count_total": total,
            "nearest_minutes": None,
            "empty_reason": empty_reason,
            # ---- 契约外的追加字段（只加键、不改键）
            "subcats": list(spec["subcats"]),
            "threshold_subcat": spec.get("threshold_subcat"),
            "data_source": source,
            "degraded_reason": degraded_reason,
            "skipped_reason": skipped_reason,
            "page_size": PAGE_SIZE,
            "page_num": PAGE_NUM,
            "detail_dropped_duplicates": dropped,
            "message": self._group_message(source, empty_reason),
        }

    def _hits(
        self, payload: Mapping[str, Any], spec: Mapping[str, Any], *, source: str, center
    ) -> tuple[list[dict[str, Any]], int]:
        key = str(spec["key"])
        raw = payload.get("results")
        items = raw if isinstance(raw, (list, tuple)) else []
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        dropped = 0
        for index, entry in enumerate(items):
            if not isinstance(entry, Mapping):
                dropped += 1
                continue
            hit = self._hit(entry, key, index, source=source, center=center)
            if hit is None:
                dropped += 1
                continue
            if hit["uid"] in seen:  # FR-214：同一 POI 被多个别名命中按 uid 去重
                dropped += 1
                continue
            seen.add(hit["uid"])
            hits.append(hit)
            if len(hits) >= PAGE_SIZE:  # 明细只有第一页
                break
        return hits, dropped

    def _hit(
        self, entry: Mapping[str, Any], category: str, index: int, *, source: str, center
    ) -> dict[str, Any] | None:
        location = entry.get("location")
        lng = _as_float(location.get("lng") if isinstance(location, Mapping) else None)
        lat = _as_float(location.get("lat") if isinstance(location, Mapping) else None)
        if lng is None or lat is None:
            return None  # 没有坐标的条目进不了地图与落圈判定，丢掉而不是编一个位置
        detail = entry.get("detail_info")
        detail = detail if isinstance(detail, Mapping) else {}
        tag = _as_text(detail.get("tag")) or ""
        classified = _as_text(detail.get("classified_poi_tag")) or ""
        subcat = normalize_subcat(classified, tag, _as_text(detail.get("type")), category=category)
        uid = _as_text(entry.get("uid")) or f"{category}:{subcat}:{index}"

        official_distance = _as_int(detail.get("distance"))
        distance_estimated = official_distance is None
        # `round()` 无 `ndigits` 时已经回 int，不需要再套一层 `int()`（RUF046）。
        distance_m = (
            official_distance
            if official_distance is not None
            else round(_haversine_m(center, (lng, lat)))
        )
        return {
            "uid": uid,
            "name": _as_text(entry.get("name")) or uid,
            "category": category,
            "subcat": subcat,
            "official_path": SUBCAT_OFFICIAL_PATH.get(subcat, ""),
            "tag": tag,
            "classified_poi_tag": classified,
            "lnglat": [round(lng, 6), round(lat, 6)],  # BD09LL，[经度, 纬度]
            "distance_m": distance_m,
            "distance_estimated": distance_estimated,
            "address": _as_text(entry.get("address")) or "",
            # ⚠️ 官方 `place/v2/search` 的 `results[]` 字段表里**没有** `town`
            # （`town` 属于逆地理；笔记 §2.3 只记载 province/city/area/adcode）。
            # 这里回退到 `area`，取不到就留空串——绝不为了填字段去猜文本切分。
            "town": _as_text(entry.get("town")) or _as_text(entry.get("area")) or "",
            "adcode": _as_text(entry.get("adcode")) or "",
            "source": source,  # type: ignore[typeddict-item]
        }

    def _total(self, payload: Mapping[str, Any], *, count: int) -> int:
        """圈内计数用返回体 `total`（官方上限 150）；取不到才退到明细条数。"""
        total = _as_int(payload.get("total"))
        if total is None or total < 0:
            return count
        return min(total, TOTAL_CAP)

    def _group_message(self, source: DataSource, empty_reason: str | None) -> str:
        if empty_reason:
            return MSG_EMPTY
        return {
            "live": MSG_LIVE_OK,
            "cache": MSG_CACHE_HIT,
            "sample": MSG_SAMPLE,
        }[source]

    # ------------------------------------------------------------------ 汇总
    def _overall_source(self, groups: list[dict[str, Any]]) -> DataSource:
        sources = {str(group["data_source"]) for group in groups}
        if "live" in sources:
            return "live"
        if "cache" in sources:
            return "cache"
        return "sample"

    def _overall_reason(self, *, source: str, skipped: list[str], reasons: list[str]) -> str | None:
        if skipped:
            return REASON_BUDGET
        if source == "sample":
            return reasons[0] if reasons else REASON_MOCK
        if source == "cache":
            return None if not reasons else reasons[0]
        return reasons[0] if reasons else None

    def _notes(self, *, source: str, skipped: list[str], groups: list[dict[str, Any]]) -> list[str]:
        notes = [
            NOTE_FAQ_2536_FILTERING,
            NOTE_NO_PAGINATION,
            NOTE_RADIUS_LIMIT,
            COORD_SYSTEM_NOTE,
        ]
        empty_dims = [str(group["key"]) for group in groups if group["empty_reason"]]
        if empty_dims:
            notes.append(f"以下维度检索无结果、按 0 计（不是异常）：{', '.join(empty_dims)}。")
        if skipped:
            notes.append(
                f"地点检索出网预算截停，以下维度未实时检索：{', '.join(skipped)}"
                f"（上限 {self._budget()} 次/次分析）。"
            )
        if source == "sample":
            archetype = getattr(self.samples, "archetype", None) or "按中心点确定性选取"
            notes.append(
                f"示例层为程序合成（社区形态：{archetype}），名称/地址/坐标均为虚构，"
                "不含任何百度返回内容（条款 3.3.4 / 6.3）。"
            )
        if source == "cache":
            notes.append("本次全部维度命中本地缓存，place_calls 与 http_requests 均为 0。")
        return notes


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点直线距离（米）。用于官方 `distance` 缺失时的兜底（置 `distance_estimated`）。"""
    lng1, lat1 = a
    lng2, lat2 = b
    radius = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    chord = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(chord)))


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


# --------------------------------------------------------------------------------------
# 进程级单例
# --------------------------------------------------------------------------------------
_default_lock = threading.Lock()
_default_service: PoiSearchService | None = None


def get_poi_search_service(settings: Settings | None = None) -> PoiSearchService:
    """进程级 `PoiSearchService`（默认依赖：真实 `BaiduClient` + 共享 `CacheStore`）。"""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = PoiSearchService(settings)
        return _default_service


def reset_poi_search_service(settings: Settings | None = None) -> PoiSearchService:
    """丢掉单例重建：换 `CACHE_DB_PATH` / 词表改动 / 重新 `reload_settings()` 之后用。"""
    global _default_service
    with _default_lock:
        _default_service = PoiSearchService(settings)
        return _default_service
