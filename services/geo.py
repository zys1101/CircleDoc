"""地理编码 / 逆地理 / 地址联想的编排层：**缓存优先 → 实时调用 → 降级**。

这一层是 PRD §4.6 三层降级链的最外圈实现（客户端内部那道缓存检查是同一链的一环，
不是第二套口径，理由见下）。三条硬约束决定了本文件的全部形状：

1. 🔴 **全链路 BD09LL，坐标顺序在本层是 `[lng, lat]`**。百度请求参数 `location` 要的是
   **`纬度,经度`**，而逆地理的坐标系参数名是 `coordtype`（**无下划线**、传字符串）——与
   地点检索的 `coord_type`（有下划线、传数字）只差一个字符（笔记 §6.2）。写反顺序或写错
   参数名**不会报错**，只会让点位整体偏移，是本项目最难发现的 bug（R8 / FR-406）。
   因此：拼参数一律委托 `infra.baidu_client`（它内部用 `to_baidu_latlng`），本层传坐标时
   用 **`{"lng":…, "lat":…}` 字典而不是元组**——元组顺序 `(lat, lng)` 与 `(lng, lat)`
   在两种约定下都能过编译，字典键没有歧义。出参统一回显 `[lng, lat]` 并带
   `coordinate_system="bd09ll"`，前端不需要再猜。
2. 🔴 **境外一律拒绝**：境外地理编码 / 逆地理 / 地点检索对个人认证开发者日配额均为「-」
   （笔记 §3.1），发出去只会收到鉴权错误并白烧配额。判据用 `Settings.china_*` 四个边界，
   入参坐标越界直接 400；地理编码与联想的**结果**坐标越界同样拒绝（地址文本无法先判国别）。
3. 🔴 **失败不中断**：空 `results`、配额耗尽、预算截停、网络超时都不抛 5xx，而是 200 +
   `degraded` + 可读 `degraded_reason`（文案取自 `infra/errors.py`，本层不另编一套），
   对应 §6.2「模拟空 results / 网络超时 → 前端收到可读提示且流程继续」。

**为什么缓存读在本层也做一次**：`data_mode=mock`（无 AK 的零配置默认值）不允许出网，
而 `BaiduClient._execute()` 的实时闸门在**查缓存之前**就抛异常，客户端那条缓存路径根本
走不到。所以本层先读一次缓存、读不到再决定是否出网。客户端内部的缓存检查保留不动，
两层共用 `infra/cache_keys.cache_key` 这**唯一**键口径——本层的镜像参数表逐字段对齐
`BaiduClient` 的构造规则（见 `_params_for_*`），并有一条自测钉住「实时写入 → mock 模式
命中」，键算歪了那条断言立刻红。
"""

from __future__ import annotations

import inspect
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from infra.baidu_client import (
    BD09LL,
    GEOCODE_ADDRESS_MAX_BYTES,
    BaiduClient,
    BaiduResult,
    to_baidu_latlng,
)
from infra.cache_store import CacheStore, get_cache_store
from infra.config import (
    DataMode,
    DataSource,
    EffectiveDataMode,
    Settings,
    get_settings,
)
from infra.errors import CircleDocError
from infra.log import get_logger, log_fields
from infra.ratelimit import logical_bucket_name

__all__ = [
    "COORDINATE_SYSTEM",
    "GEO_SERVICES",
    "SUGGEST_MIN_KEYWORD_LEN",
    "GeoOutcome",
    "GeoService",
    "InvalidGeoParam",
    "OutsideServiceArea",
    "SampleProvider",
    "get_geo_service",
    "parse_lnglat",
    "reset_geo_service",
    "suggest_enabled",
]

logger = get_logger("circledoc.services.geo")

COORDINATE_SYSTEM = BD09LL
"""本项目唯一的坐标系（笔记 §6.1：非 BD09 必须先转 BD09，否则点位偏移 FAQ 2578）。"""

GEO_SERVICES: tuple[str, ...] = ("geocode", "regeo", "suggest")

SUGGEST_MIN_KEYWORD_LEN = 3
"""FR-108 门槛：少于 3 字不发联想。前端做防抖，后端做**配额**门槛——两者不等价，
浏览器脚本被绕过时只有这里能挡住把 100 次/日 打光的批量请求（笔记 §2.4 / FAQ 2540）。"""

# 用户可读文案。分工：百度侧的错误一律来自 `infra/errors.py`（`CircleDocError.user_message`），
# 这里只写「本层自己判断的输入/空结果/开关」这几类，两者不会打架。
MSG_CACHE_HIT = "该结果来自本地缓存，未发起实时调用，也不消耗今日配额。"
MSG_LIVE_OK = "已获取实时结果。"
MSG_EMPTY_GEOCODE = "百度没有解析出该地址的坐标，请补全城市或换用更完整的地址写法。"
MSG_EMPTY_REGEO = "该坐标暂未解析出结构化地址，可能是水域或未收录的新建道路，请微调位置。"
MSG_EMPTY_SUGGEST = "没有匹配的联想结果，再输入几个字试试。"
MSG_BUDGET_STOP = "本次分析的出网预算已用尽，改用缓存或合成数据继续，结果可能不完整。"
MSG_MOCK_MODE = "当前为离线（mock）模式，不发起实时调用；如需实时数据请配置服务端 AK。"
MSG_SUGGEST_OFF = (
    "地址联想当前未启用：它与地点检索共用每日 100 次配额（笔记 FAQ 2540 / 2549），"
    "确认要占用该配额后，把 SUGGEST_ENABLED 设为 true 再开启。"
)
MSG_SAMPLE = "实时服务本次不可用，已降级到降级链最内层。"
MSG_SAMPLE_UNAVAILABLE = "该查询暂无可用的合成示例数据；前端按上面的提示继续流程即可，不算失败。"


# --------------------------------------------------------------------------------------
# 异常：入参边界（HTTP 4xx 可读，绝不冒成 500）
# --------------------------------------------------------------------------------------
class InvalidGeoParam(CircleDocError):
    """地址 / 坐标 / 关键字不合法。

    为什么继承 `CircleDocError` 而不是直接抛 `HTTPException`：`api/app.py` 已经把这个基类
    映射成统一 JSON 信封（含 `request_id`、`retryable`、`category`），路由里就不需要再写
    一份状态码判断——PRD §5 要求「所有入参做类型/范围校验」，但错误形状必须全站一致。
    """

    error_id = "invalid_geo_param"
    category = "client"
    http_status = 400
    user_message = "提交的地址或坐标不符合要求，请检查后重新填写。"
    retryable = False
    degrade_to_sample = False

    def __init__(self, message: str, *, field_name: str, **detail: Any) -> None:
        super().__init__(user_message=message, detail={"field": field_name, **detail})


class OutsideServiceArea(CircleDocError):
    """境外坐标 / 境外地址：个人认证开发者对境外各服务未开放（笔记 §3.1 权益表「-」）。"""

    error_id = "outside_service_area"
    category = "client"
    http_status = 400
    user_message = (
        "该位置在中国大陆及港澳台范围之外：个人认证的百度账号对境外地理编码 / 逆地理 /"
        "地点检索均无配额（官方权益表为「-」），请改用国内的位置。"
    )
    retryable = False
    degrade_to_sample = False

    def __init__(self, *, lnglat: tuple[float, float], source: str) -> None:
        super().__init__(
            detail={
                "lnglat": [lnglat[0], lnglat[1]],
                "source": source,
                "coordinate_system": COORDINATE_SYSTEM,
            }
        )


# --------------------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GeoOutcome:
    """一次地理查询的结论：内容 + 来自哪一层 + 为什么降级 + 本次三栏计量。

    `data_source` 的值域是 `live|cache|sample`（FR-306 徽标），**不是** `data_mode` 的
    `live|cache|mock`——`config.py` 专门 warned 过这两个字段混用的坑，这里只回徽标值。
    """

    service: str
    ok: bool
    data_source: DataSource
    data: Mapping[str, Any] = field(default_factory=dict)
    message: str = MSG_LIVE_OK
    degraded: bool = False
    degraded_reason: str | None = None
    empty: bool = False
    usage: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """§4.7 的响应体：出参字段**平铺在顶层**，再叠信封字段。

        为什么不套一层 `data`：§4.7 把 `/api/geocode` 的出参写成 `{lnglat, confidence,
        precision, level}` 这种平铺形状，前端已经按它取；包一层会让每个调用点都要改。
        """
        body: dict[str, Any] = {
            "ok": self.ok,
            "service": self.service,
            "data_source": self.data_source,
            "coordinate_system": COORDINATE_SYSTEM,
            "empty": self.empty,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "message": self.message,
        }
        body.update(dict(self.data))
        body["usage"] = dict(self.usage)
        body["meta"] = dict(self.meta)
        if self.error is not None:
            body["error"] = dict(self.error)
        return body


@dataclass(frozen=True)
class _Parsed:
    """响应体解析结果：可用内容，或「为什么不可用」。"""

    data: Mapping[str, Any] = field(default_factory=dict)
    empty_reason: str | None = None
    message: str = MSG_LIVE_OK
    lnglat: tuple[float, float] | None = None
    """本条结果的主坐标（联想取第一条），用于境外判定；`None` = 该查询没有坐标。"""

    @property
    def usable(self) -> bool:
        return self.empty_reason is None


class SampleProvider(Protocol):
    """降级链最内层（**程序合成**示例数据，FR-412）的窄接口。

    为什么只给一个 `get`：本层不该关心合成数据长什么样、存在哪。M5 的合成数据集落地后
    注入实现即可；实现写成同步返回最省事，异步也能用（本层会 `await` 可等待对象）。
    🔴 合成数据必须由算法生成，绝不能是缓存/录制下来的百度返回（笔记 §7.3 方案 A）。
    """

    def get(self, service: str, params: Mapping[str, Any]) -> Any: ...


# --------------------------------------------------------------------------------------
# 输入解析
# --------------------------------------------------------------------------------------
def parse_lnglat(value: str) -> tuple[float, float]:
    """`"116.404,39.915"` → `(lng, lat)`（本项目对外一律 **经度在前**）。

    为什么在本层解析而不是靠 pydantic：FR-101 方式③ 让用户直接粘 `lng,lat`，输入形状是
    字符串；而百度请求里的 `location` 是 **`纬度,经度`**。两个顺序同时存在，就必须有一个
    地方把「用户的 lng,lat」翻成「百度的 lat,lng」——翻在这里、翻一次、翻完立刻校验，
    比让每个调用点各转一遍安全（笔记 §6.2 第 3 条）。
    """
    text = (value or "").strip()
    parts = [item for item in text.replace("，", ",").split(",") if item.strip()]
    if len(parts) != 2:
        raise InvalidGeoParam(
            "坐标格式应为「经度,纬度」，例如 116.404,39.915。",
            field_name="lnglat",
            value=text,
        )
    try:
        lng = float(parts[0])
        lat = float(parts[1])
    except ValueError as exc:
        raise InvalidGeoParam(
            "坐标里的经纬度不是数字，请检查是否复制了多余字符。",
            field_name="lnglat",
            value=text,
        ) from exc
    if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
        raise InvalidGeoParam(
            "坐标超出合法区间（经度 -180~180、纬度 -90~90）。",
            field_name="lnglat",
            lnglat=[lng, lat],
        )
    return (lng, lat)


def suggest_enabled(settings: Settings | None = None) -> bool:
    """地址联想是否启用（FR-108：**默认关闭**）。

    为什么要三跳：`infra/config.py` 里还没有 `suggest_enabled` 字段（本任务不改 `infra/`），
    但门槛必须现在就生效——它与地点检索共用 100 次/日，开着就等于让输入框替用户烧配额。
    顺序：① `Settings.suggest_enabled`（字段一旦落地自动接管）→ ② 环境变量
    `SUGGEST_ENABLED` / `CIRCLEDOC_SUGGEST_ENABLED` → ③ 默认关闭。
    """
    cfg = settings or get_settings()
    declared = getattr(cfg, "suggest_enabled", None)
    if isinstance(declared, bool):
        return declared
    for name in ("SUGGEST_ENABLED", "CIRCLEDOC_SUGGEST_ENABLED"):
        raw = os.environ.get(name)
        if raw is not None:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
    return False


# --------------------------------------------------------------------------------------
# 编排层
# --------------------------------------------------------------------------------------
class GeoService:
    """三个接口的业务流程；不做 HTTP、不拼百度参数、不算缓存键（那些都在 `infra/`）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: BaiduClient | None = None,
        cache: CacheStore | None = None,
        samples: SampleProvider | None = None,
    ) -> None:
        """依赖全部可注入：自测传 `BaiduClient(settings, transport=httpx.MockTransport(...))`
        就能在 0 次真实出网的前提下把三层降级链整条跑完（条款 3.2.3(ii) 禁止压测线上服务）。
        """
        cfg = settings or get_settings()
        self.settings = cfg
        self.cache: CacheStore | None = cache if cache is not None else get_cache_store(cfg)
        self.samples = samples
        self.client = client or BaiduClient(cfg, cache=self.cache)
        if self.client.cache is None and self.cache is not None:
            # 为什么补这一刀：缓存只允许一套口径。注入的客户端若忘了接缓存，实时结果就不会
            # 落库，本层的「缓存优先」永远读不到东西，表现是「命中率 0 但没人报错」。
            self.client.cache = self.cache

    # ------------------------------------------------------------------ 对外三个方法
    async def geocode(
        self,
        address: str,
        *,
        city: str | None = None,
        data_mode: DataMode | None = None,
    ) -> GeoOutcome:
        """地址 → 坐标（FR-101 方式②）。出参 `lnglat` 为 BD09LL 的 `[lng, lat]`。

        入口校验：空地址、超过官方 128 字节上限（笔记 §2.1）都在本层拒掉——客户端会把超长
        地址**静默截断**，截断后的地址是另一个地址，返回的坐标看着合理却是错的。
        """
        cleaned = self._require_address(address)
        params = self._params_for_geocode(cleaned, city)
        return await self._resolve(
            service="geocode",
            params=params,
            parse=lambda payload: self._parse_geocode(payload),
            live=lambda: self.client.geocode(cleaned, city=city),
            mode=self._resolve_mode(data_mode),
        )

    async def regeo(
        self,
        lnglat: str | tuple[float, float] | Mapping[str, float],
        *,
        data_mode: DataMode | None = None,
    ) -> GeoOutcome:
        """坐标 → 结构化地址（FR-101 方式③ + FR-103 回显行政区 / 街道）。

        🔴 不暴露 `radius` / `extensions_poi`：`_params_for_regeo` 必须与客户端的参数表逐字
        对齐才能保证缓存键一致，可选参数每多一个就多一处会写歪的地方；而 FR-103 要的
        `town` / `town_code` / `adcode` / `street` 全在默认返回的 `addressComponent` 里
        （笔记 §2.2，FR-103 据此成立）。需要 POI 召回的调用方请直接注入 `BaiduClient`，
        别在这里加参数——FAQ 2561：逆地理的 POI 只是辅助定位，检索 POI 要用地点检索。
        """
        lng, lat = self._coerce_lnglat(lnglat)
        self._require_domestic(lng, lat, source="request")
        params = self._params_for_regeo(lng, lat)
        return await self._resolve(
            service="regeo",
            params=params,
            parse=lambda payload: self._parse_regeo(payload, lng=lng, lat=lat),
            live=lambda: self.client.reverse_geocode({"lng": lng, "lat": lat}),
            mode=self._resolve_mode(data_mode),
        )

    async def suggest(
        self,
        keyword: str,
        *,
        region: str | None = None,
        city_limit: bool | None = None,
        data_mode: DataMode | None = None,
    ) -> GeoOutcome:
        """地址联想（FR-108）。⚠️ 走 `/place/v2/suggestion`，**不复用地理编码**：地理编码
        只回一个坐标、没有候选列表（笔记 §2.1 末段）。

        三道门槛：① 开关关闭（默认）时**不发实时请求**，只读缓存；② 输入 ≥ 3 字；
        ③ 结果缓存（bucket 名 `suggest`，TTL 走 POI 档 24 h，见 `infra/cache_keys.py`）。
        `region` 是官方必选参数（笔记 §2.4），少了它会收到 `status=2`，所以本地先挡。
        """
        word = self._require_keyword(keyword)
        area = self._require_region(region)
        params = self._params_for_suggest(word, area, city_limit)
        if not suggest_enabled(self.settings):
            # 关闭只关「出网」这一环：缓存读不花配额，留着它才能让「开启过一次后」的输入框
            # 依然有历史候选。实时调用一律不发——`live=None` 是最硬的保证。
            return await self._resolve(
                service="suggest",
                params=params,
                parse=lambda payload: self._parse_suggest(payload),
                live=None,
                mode=self._resolve_mode(data_mode),
                off_reason="suggest_disabled",
                off_message=MSG_SUGGEST_OFF,
            )
        return await self._resolve(
            service="suggest",
            params=params,
            parse=lambda payload: self._parse_suggest(payload),
            live=lambda: self.client.suggest(word, region=area, city_limit=city_limit),
            mode=self._resolve_mode(data_mode),
        )

    # ------------------------------------------------------------------ 入口校验
    def _require_address(self, address: str) -> str:
        cleaned = (address or "").strip()
        if not cleaned:
            raise InvalidGeoParam(
                "地址不能为空，请输入小区 / 街道等具体位置。", field_name="address"
            )
        size = len(cleaned.encode("utf-8"))
        if size > GEOCODE_ADDRESS_MAX_BYTES:
            raise InvalidGeoParam(
                f"地址过长：{size} 字节，官方上限 {GEOCODE_ADDRESS_MAX_BYTES} 字节"
                "（约 42 个汉字），请精简到具体门牌。",
                field_name="address",
                bytes=size,
                max_bytes=GEOCODE_ADDRESS_MAX_BYTES,
            )
        return cleaned

    def _require_keyword(self, keyword: str) -> str:
        cleaned = (keyword or "").strip()
        if not cleaned:
            raise InvalidGeoParam("请输入要联想的地址关键字。", field_name="keyword")
        if len(cleaned) < SUGGEST_MIN_KEYWORD_LEN:
            raise InvalidGeoParam(
                f"关键字至少 {SUGGEST_MIN_KEYWORD_LEN} 个字，太短会白烧地点检索的每日配额。",
                field_name="keyword",
                min_len=SUGGEST_MIN_KEYWORD_LEN,
                length=len(cleaned),
            )
        return cleaned

    def _require_region(self, region: str | None) -> str:
        cleaned = (region or "").strip()
        if not cleaned:
            raise InvalidGeoParam(
                "地址联想需要城市（官方把 region 列为必选），例如 region=北京。",
                field_name="region",
            )
        return cleaned

    def _coerce_lnglat(self, value: Any) -> tuple[float, float]:
        """把 `lnglat` 的几种写法统一成 `(lng, lat)`；顺带做区间校验。"""
        if isinstance(value, Mapping):
            lng = value.get("lng", value.get("longitude"))
            lat = value.get("lat", value.get("latitude"))
            if lng is None or lat is None:
                raise InvalidGeoParam(
                    "坐标缺少 lng 或 lat。", field_name="lnglat", value=repr(value)
                )
            return self._checked(float(lng), float(lat), repr(value))
        if isinstance(value, (tuple, list)) and len(value) == 2:
            # 约定：内部元组一律 (lng, lat)。**百度请求参数不是这个顺序**，换算在
            # `to_baidu_latlng` 里做一次；这里若再猜一次顺序就会出现「两套经纬」。
            return self._checked(float(value[0]), float(value[1]), repr(value))
        return self._checked(*parse_lnglat(str(value)), str(value))

    def _checked(self, lng: float, lat: float, raw: Any) -> tuple[float, float]:
        if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
            raise InvalidGeoParam(
                "坐标超出合法区间（经度 -180~180、纬度 -90~90）。",
                field_name="lnglat",
                value=str(raw),
            )
        return (lng, lat)

    def _require_domestic(self, lng: float, lat: float, *, source: str) -> None:
        if not self._within_china(lng, lat):
            raise OutsideServiceArea(lnglat=(lng, lat), source=source)

    def _within_china(self, lng: float, lat: float) -> bool:
        """国内判定用 `Settings.china_*` 的矩形边界，不引第三方地理库。

        为什么用矩形：PRD §6.1 的断言是「样例社区坐标全部落在国内」，一个可配置、可解释、
        零依赖的包围盒够用；而精确国境判定要引 shapefile，为了拒绝几个越界点位不值当。
        边界值可在 `.env` 调（`config.py` 里四个字段都是配置项）。
        """
        cfg = self.settings
        return (
            cfg.china_lng_min <= lng <= cfg.china_lng_max
            and cfg.china_lat_min <= lat <= cfg.china_lat_max
        )

    # ------------------------------------------------------------------ 模式
    def _resolve_mode(self, requested: DataMode | None) -> EffectiveDataMode:
        """请求级 `data_mode` 覆盖运行期解析值（FR-105 的开关在 UI 上，不在 `.env` 里）。"""
        if requested in ("live", "cache", "mock"):
            return cast(EffectiveDataMode, requested)
        return self.settings.data_mode_in_effect

    # ------------------------------------------------------------------ 镜像参数表
    # 🔴 这三处必须与 `infra/baidu_client.py` 的构造规则**逐字段一致**：缓存键由
    # `cache_key(service, params)` 算，参数差一个字段就等于换了个键，表现是命中率静默腰斩
    # （`infra/cache_keys.py` 开头写明了这个坑）。`output` 虽然会被
    # `drop_volatile_params` 剔掉，但照抄比「判断哪些能被剔掉」更不容易错。
    def _params_for_geocode(self, address: str, city: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"address": address, "output": "json"}
        if city:
            params["city"] = city
        return params

    def _params_for_regeo(self, lng: float, lat: float) -> dict[str, Any]:
        return {
            # `to_baidu_latlng` 吃字典（不是元组）：键名自己声明了顺序，翻不出「纬经/经纬」的坑。
            "location": to_baidu_latlng({"lng": lng, "lat": lat}),
            "output": "json",
            "coordtype": BD09LL,  # ⚠️ 无下划线 + 字符串（笔记 §6.2）；不是 `coord_type`
        }

    def _params_for_suggest(
        self, keyword: str, region: str, city_limit: bool | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": keyword, "region": region, "output": "json"}
        if city_limit is not None:
            params["city_limit"] = city_limit
        return params

    # ------------------------------------------------------------------ 三层链
    async def _resolve(
        self,
        *,
        service: str,
        params: dict[str, Any],
        parse: Callable[[dict[str, Any]], _Parsed],
        live: Callable[[], Awaitable[BaiduResult]] | None,
        mode: EffectiveDataMode,
        off_reason: str | None = None,
        off_message: str | None = None,
    ) -> GeoOutcome:
        """缓存 → 实时 → 合成。任何一环失败都继续下一环，返回值永远带 `data_source`。"""
        bucket = logical_bucket_name(service)
        cached = await self._cache_read(bucket, params)
        if cached is not None:
            payload, info = cached
            parsed = parse(payload)
            if parsed.usable:
                self._log_done(service, "cache", parsed=parsed, info=info)
                return self._outcome(
                    service,
                    params,
                    data_source="cache",
                    parsed=parsed,
                    message=MSG_CACHE_HIT,
                    usage=_usage_from(None, cache_hit=True),
                    mode=mode,
                    cache=info,
                )
            # 缓存里是空结果（`status=0` 但没内容，官方确实会给）：不能当命中回放给用户，
            # 否则一次抖动后的空数据会被钉成「事实」。继续往下试实时。

        if mode == "mock":
            return await self._degrade(
                service,
                params,
                reason="mock_mode",
                message=MSG_MOCK_MODE,
                mode=mode,
                parse=parse,
            )
        if live is None:
            return await self._degrade(
                service,
                params,
                reason=off_reason or "live_skipped",
                message=off_message or MSG_MOCK_MODE,
                mode=mode,
                parse=parse,
            )

        try:
            result = await live()
        except CircleDocError as exc:
            # 🔴 不重抛：`api/health` 那类配置错误要让页面继续跑（FR-403）。
            # 文案直接用 `errors.py` 的 `user_message`，本层不编第二套（FR-404 六类文案）。
            return await self._degrade(
                service,
                params,
                reason=exc.error_id,
                message=exc.user_message,
                mode=mode,
                parse=parse,
                error=exc.to_payload(),
            )

        if result.from_cache:
            parsed = parse(dict(result.data))
            if parsed.usable:
                return self._outcome(
                    service,
                    params,
                    data_source="cache",
                    parsed=parsed,
                    message=MSG_CACHE_HIT,
                    usage=_usage_from(result, cache_hit=True),
                    mode=mode,
                    cache={"hit": True, "layer": "client"},
                )
            # 客户端缓存命中的空报文：与上面同理，落到「空结果」分支，不给假命中。

        if result.blocked:
            reason = (
                "budget_exhausted"
                if result.budget_exhausted
                else (result.blocked_reason or "blocked")
            )
            return await self._degrade(
                service,
                params,
                reason=reason,
                message=MSG_BUDGET_STOP if result.budget_exhausted else MSG_SAMPLE,
                mode=mode,
                parse=parse,
                usage=_usage_from(result),
            )

        parsed = parse(dict(result.data))
        if not parsed.usable:
            # 空 results：**内容确实来自实时层**，所以 `data_source="live"` + `empty=true`。
            # 不退到合成层——「这个地址不存在」和「编一个地址出来」是两回事，后者会让用户
            # 把中心点落在错的位置上（PRD §4.6：猜错方向时不能给出错误结论）。
            return self._outcome(
                service,
                params,
                data_source="live",
                parsed=parsed,
                message=parsed.message,
                degraded=True,
                degraded_reason=parsed.empty_reason,
                empty=True,
                usage=_usage_from(result),
                mode=mode,
                cache={"hit": False, "layer": None},
            )

        # 结果坐标越界 = 境外地址（个人档境外各服务未开放）：在出网之后才发现，
        # 所以要**拒绝回显**而不是把点位丢给地图。地理编码只有这一种境外判据。
        if parsed.lnglat is not None and not self._within_china(*parsed.lnglat):
            raise OutsideServiceArea(lnglat=parsed.lnglat, source=f"{service}_response")

        self._log_done(service, "live", parsed=parsed)
        return self._outcome(
            service,
            params,
            data_source="live",
            parsed=parsed,
            message=MSG_LIVE_OK,
            usage=_usage_from(result),
            mode=mode,
            cache={"hit": False, "layer": None},
        )

    async def _degrade(
        self,
        service: str,
        params: dict[str, Any],
        *,
        reason: str,
        message: str,
        mode: EffectiveDataMode,
        parse: Callable[[dict[str, Any]], _Parsed],
        error: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> GeoOutcome:
        """实时不可用时落最内层：有合成数据就用，没有就回「空但可读」。"""
        sample = await self._sample_for(service, params)
        if sample is not None:
            parsed = parse(sample)
            data = dict(parsed.data) if parsed.usable else dict(sample)
            return self._outcome(
                service,
                params,
                data_source="sample",
                parsed=_Parsed(data=data, message=MSG_SAMPLE),
                message=MSG_SAMPLE,
                degraded=True,
                degraded_reason=reason,
                usage=dict(usage or _usage_from(None)),
                mode=mode,
                error=error,
                extra_meta={"sample_layer": "synthetic"},
            )
        return self._outcome(
            service,
            params,
            data_source="sample",
            parsed=_Parsed(),
            message=f"{message} {MSG_SAMPLE_UNAVAILABLE}",
            degraded=True,
            degraded_reason=reason,
            empty=True,
            usage=dict(usage or _usage_from(None)),
            mode=mode,
            error=error,
            extra_meta={"sample_layer": "unavailable"},
        )

    # ------------------------------------------------------------------ 缓存 / 合成
    async def _cache_read(
        self, service: str, params: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """读缓存；任何异常都当未命中（FR-403：缓存层挂了不能连带打死业务）。"""
        if self.cache is None:
            return None
        try:
            result = await self.cache.get_async(service, params)
        except Exception as exc:
            log_fields(
                logger,
                logging.WARNING,
                "geo_cache_read_failed",
                error=type(exc).__name__,
                service=service,
            )
            return None
        info = {
            "hit": bool(getattr(result, "hit", False)),
            "layer": getattr(result, "layer", None),
            "miss_reason": getattr(getattr(result, "miss_reason", None), "value", None),
        }
        payload = getattr(result, "payload", None)
        if not info["hit"] or not isinstance(payload, Mapping):
            return None
        return dict(payload), info

    async def _sample_for(self, service: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """最内层合成数据；M5 的 `SampleRepo` 没注入时返回 ``None``（本层不造假数据）。"""
        if self.samples is None:
            return None
        try:
            raw = self.samples.get(service, params)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:
            log_fields(
                logger,
                logging.WARNING,
                "geo_sample_failed",
                error=type(exc).__name__,
                service=service,
            )
            return None
        return dict(raw) if isinstance(raw, Mapping) else None

    # ------------------------------------------------------------------ 解析（§4.7 出参）
    def _parse_geocode(self, payload: Mapping[str, Any]) -> _Parsed:
        lng = _as_float(_dig(payload, "result", "location", "lng"))
        lat = _as_float(_dig(payload, "result", "location", "lat"))
        if lng is None or lat is None:
            return _Parsed(empty_reason="empty_results", message=MSG_EMPTY_GEOCODE)
        data: dict[str, Any] = {"lnglat": [lng, lat]}  # BD09LL，[经度, 纬度]
        for key, getter in (
            ("confidence", lambda: _as_int(_dig(payload, "result", "confidence"))),
            ("precision", lambda: _as_int(_dig(payload, "result", "precision"))),
            ("comprehension", lambda: _as_int(_dig(payload, "result", "comprehension"))),
            ("level", lambda: _as_text(_dig(payload, "result", "level"))),
            ("code", lambda: _as_int(_dig(payload, "result", "code"))),
            ("city", lambda: _as_text(_dig(payload, "result", "city"))),
        ):
            value = getter()
            if value is not None:
                data[key] = value
        # confidence / precision 是 §4.7 明确要求回显的字段（笔记 §8.7：前端据此提示定位精度）。
        data.setdefault("confidence", None)
        data.setdefault("precision", None)
        return _Parsed(data=data, lnglat=(lng, lat))

    def _parse_regeo(self, payload: Mapping[str, Any], *, lng: float, lat: float) -> _Parsed:
        """只取 `addressComponent` 的官方字段。

        为什么不做 `formatted_address` 文本切分：PRD §4.7 末尾专门写过——切分换个城市就
        错位；`town` / `town_code` / `adcode` / `street` 官方都有（笔记 §2.2），
        FR-103 因此成立。
        """
        formatted = _as_text(_dig(payload, "result", "formatted_address"))
        component = _dig(payload, "result", "addressComponent")
        component = component if isinstance(component, Mapping) else {}
        data: dict[str, Any] = {"lnglat": [lng, lat], "coordinate_system": COORDINATE_SYSTEM}
        data["formatted_address"] = formatted or ""
        for key in (
            "province",
            "city",
            "district",
            "town",
            "town_code",
            "street",
            "street_number",
            "adcode",
            "cityCode",
        ):
            value = component.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                data[key] = int(value)
            elif isinstance(value, str) and value.strip():
                data[key] = value.strip()
        has_component = any(
            key in data and data[key] not in (None, "")
            for key in ("district", "town", "street", "adcode")
        )
        if not data["formatted_address"] and not has_component:
            return _Parsed(empty_reason="empty_results", message=MSG_EMPTY_REGEO)
        return _Parsed(data=data, lnglat=(lng, lat))

    def _parse_suggest(self, payload: Mapping[str, Any]) -> _Parsed:
        """候选项解析。

        TODO(需官方核对)：笔记 §2.4 只核验了**入参**（`query` / `region` / `city_limit` /
        `output`），**没有记录返回体字段名**。这里按「`result` 是数组、每项含 `name` 与
        `location{lng,lat}`」做防御式解析，取不到坐标的条目直接丢弃，不猜 `nameTag` /
        `type` 之类的字段是否存在；正式对接前需照笔记 §9.2-⑨ 从控制台「示例代码」核对。
        """
        raw = payload.get("result")
        items: list[dict[str, Any]] = []
        dropped = 0
        if isinstance(raw, (list, tuple)):
            for entry in raw:
                if not isinstance(entry, Mapping):
                    dropped += 1
                    continue
                lng = _as_float(_dig(entry, "location", "lng"))
                lat = _as_float(_dig(entry, "location", "lat"))
                name = _as_text(entry.get("name"))
                if not name or lng is None or lat is None:
                    dropped += 1
                    continue
                if not self._within_china(lng, lat):
                    # 境外候选项对个人档不可用，后续 regeo / POI 都会白烧；留在返回里只会
                    # 让前端把中心点落在境外。丢掉而不是报错——列表里通常还有国内项。
                    dropped += 1
                    continue
                item: dict[str, Any] = {"name": name, "lnglat": [lng, lat]}
                for key in ("district", "city", "province", "adcode"):
                    text = _as_text(entry.get(key))
                    if text is not None:
                        item[key] = text
                items.append(item)
        if not items:
            return _Parsed(
                data={"items": [], "total": 0, "dropped": dropped},
                empty_reason="empty_results",
                message=MSG_EMPTY_SUGGEST,
            )
        first = items[0]
        return _Parsed(
            data={"items": items, "total": len(items), "dropped": dropped},
            lnglat=cast("tuple[float, float]", tuple(first["lnglat"])),
        )

    # ------------------------------------------------------------------ 组装
    def _outcome(
        self,
        service: str,
        params: Mapping[str, Any],
        *,
        data_source: DataSource,
        parsed: _Parsed,
        message: str,
        usage: Mapping[str, Any],
        mode: EffectiveDataMode,
        degraded: bool = False,
        degraded_reason: str | None = None,
        empty: bool = False,
        cache: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        extra_meta: Mapping[str, Any] | None = None,
    ) -> GeoOutcome:
        meta: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "data_mode": mode,
            "coordinate_system": COORDINATE_SYSTEM,
            # 回显的是**规范化后的入参**（不含 AK：`infra/cache_keys.py` 把密钥类参数
            # 一律剔出键，这里同理——日志与响应里都不该出现密钥）。
            "params": {k: v for k, v in params.items() if k != "output"},
        }
        if cache is not None:
            meta["cache"] = dict(cache)
        if extra_meta:
            meta.update(dict(extra_meta))
        return GeoOutcome(
            service=service,
            ok=not empty and parsed.usable,
            data_source=data_source,
            data=parsed.data,
            message=message,
            degraded=degraded,
            degraded_reason=degraded_reason,
            empty=empty,
            usage=usage,
            meta=meta,
            error=error,
        )

    def _log_done(
        self,
        service: str,
        layer: str,
        *,
        parsed: _Parsed,
        info: Mapping[str, Any] | None = None,
    ) -> None:
        """结构化日志：路径上「到底从哪一层拿到的数」必须可查（PRD §5 可观测）。"""
        fields: dict[str, Any] = {
            "service": service,
            "layer": layer,
            "empty": not parsed.usable,
        }
        if info:
            fields["cache_layer"] = info.get("layer")
        log_fields(logger, logging.INFO, "geo_resolved", **fields)


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _usage_from(result: BaiduResult | None, *, cache_hit: bool = False) -> dict[str, Any]:
    """本次接口调用的三栏计量（PRD §6.1 要求 place_calls / quota_units / http_requests 分开）。

    为什么取 `BaiduResult` 上的**本次**计数而不是累加器：QA 的验收口径是「命中缓存时
    `place_calls = 0`」，进程级累加器跨请求、只增不减，第二轮即使命中也永远不为 0。
    请求级总量由 `api/app.py` 的 `X-Place-Calls` 等响应头给出，两处口径不同、都要留。
    """
    if result is None:
        return {
            "place_calls": 0,
            "quota_units": 0,
            "http_requests": 0,
            "attempts": 0,
            "cache_hits": 1 if cache_hit else 0,
        }
    return {
        "place_calls": int(result.place_calls),
        "quota_units": int(result.quota_units),
        "http_requests": int(result.http_requests),
        "attempts": int(result.attempts),
        "cache_hits": 1 if (cache_hit or result.from_cache) else 0,
    }


def _dig(payload: Any, *path: str) -> Any:
    """按路径取嵌套字段；任何一层不是字典就返回 ``None``（百度字段时有时无，不能假设）。"""
    cursor: Any = payload
    for key in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(key)
    return cursor


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
# 缓存与限流状态必须跨请求存活：每请求新建 `GeoService` 会让「同进程运行时性能缓存」
# 退化成一次性对象，FR-402 的命中率验收就无从谈起。
_default_lock = threading.Lock()
_default_service: GeoService | None = None


def get_geo_service(settings: Settings | None = None) -> GeoService:
    """进程级 `GeoService`（默认依赖：真实 `BaiduClient` + 共享 `CacheStore`）。"""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = GeoService(settings)
        return _default_service


def reset_geo_service(settings: Settings | None = None) -> GeoService:
    """丢掉单例重建：换 `CACHE_DB_PATH` 或 `reload_settings()` 之后用。"""
    global _default_service
    with _default_lock:
        _default_service = GeoService(settings)
        return _default_service
