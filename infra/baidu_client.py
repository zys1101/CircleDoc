"""百度 Web 服务客户端：判据只看响应体 `status`，三栏分开计量，双熔断 + 出网预算守卫。

🔴 为什么所有判定都不看 HTTP 状态码：百度用 **HTTP 200 + body `status`** 表达限流与配额，
官方码表里根本不存在 429（笔记 §3.2、PRD §4.6）。并发超限是 `status=401`、当日配额耗尽是
`4` / `302`。按「5xx 才重试」写客户端会同时犯两个错：漏掉真正的限流信号，又把「今天已耗尽」
当成可重试错误反复打——后者只在加速烧配额，用户看到的还是同一个错误。

三栏计量为什么不许混：一次批量算路只发 **1 个 HTTP 请求**，却按 `origins × destinations`
折成 N 个配额单位、0 次地点检索（笔记 §2.8 官方原文）。合成一个数字后，「8 次请求」既可能
安全也可能已经把 100 次/日的地点检索打光，看板与验收都读不出差别（PRD §6.1）。

测试与演练为什么一律走 mock：服务条款 3.2.3(ii) 禁止「对本服务进行压力测试并对百度线上服务
稳定性造成影响」（笔记 §7.2）。构造器接 `transport=`（`httpx.MockTransport`），CI 与演练
都不产生一次真实出网。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from infra.breaker import BreakerRegistry
from infra.config import ProbeResult, Settings, get_settings, resolve_data_mode
from infra.errors import (
    OK_STATUS,
    BaiduApiError,
    BreakerKind,
    CircleDocError,
    ErrorSpec,
    RouteUnreachable,
    is_retryable_status,
    spec_for,
)
from infra.log import get_logger, log_fields
from infra.metrics import Metrics, current_metrics, get_process_metrics
from infra.ratelimit import Clock, RateLimitRegistry, Sleeper, logical_bucket_name

__all__ = [
    "BAIDU_BASE_URL",
    "BaiduClient",
    "BaiduResult",
    "RouteMatrixResult",
    "to_baidu_latlng",
]

logger = get_logger("circledoc.baidu_client")

BAIDU_BASE_URL = "https://api.map.baidu.com"
"""唯一允许出网的域名（PRD §5 防 SSRF）；实际校验以 `Settings.allowed_hosts` 为准。"""

BD09LL = "bd09ll"
BD09LL_NUMERIC = 3
"""地点检索的 `coord_type` **取数字**（3=bd09ll），其余接口同参数名取字符串（笔记 §6.2）。
   同一个参数名两种值类型，是本项目最容易写错的地方，所以在这里只出现一次。"""

ENDPOINTS: dict[str, str] = {
    "geocode": "/geocoding/v3/",
    "regeo": "/reverse_geocoding/v3/",
    "place": "/place/v2/search",
    "place_region": "/place/v3/region",
    "suggest": "/place/v2/suggestion",
    "direction_v2_walking": "/direction/v2/walking",
    "direction_v2_riding": "/direction/v2/riding",
    "directionlite_v1_walking": "/directionlite/v1/walking",
    "directionlite_v1_riding": "/directionlite/v1/riding",
    "routematrix_v2_walking": "/routematrix/v2/walking",
    "routematrix_v2_riding": "/routematrix/v2/riding",
}
"""路径全部逐字抄自笔记 §2 的核验结果，**不凭记忆补全**。

`TODO(需官方核对)`：坐标转换（geoconv）的 endpoint 笔记 §9.1-④ 明确「本次未取到正文」，
禁止凭记忆写入；本项目核心链路全为 BD09LL、不需要该接口（笔记 §6.3），故此处不提供方法。
"""

COORD_INPUT_PARAM: dict[str, tuple[str, str] | None] = {
    # service -> (参数名, 值)。None = 该接口没有入参坐标；缺失的 service = 官方文档未记载
    # 该参数，**一律不发**（笔记 §2 开头：凡未列出的参数即官方文档未记载，不要假设存在）。
    "geocode": None,  # §6.2：地理编码无入参坐标
    "regeo": ("coordtype", BD09LL),  # ⚠️ 无下划线、传字符串
    "place": ("coord_type", str(BD09LL_NUMERIC)),  # ⚠️ 有下划线、传数字
    "place_region": ("coord_type", str(BD09LL_NUMERIC)),
    "direction": ("coord_type", BD09LL),  # ⚠️ 有下划线、传字符串
    "routematrix": ("coord_type", BD09LL),
}
"""入参坐标系参数名表（笔记 §6.2 三条硬性约束之一）。

`ret_coordtype`（出参坐标系）**一律不发**：各接口默认即 BD09LL，而官方给出的值域三处不同
（地理编码只写 `gcj02ll`/`bd09mc`、地点检索只写 `gcj02ll`），传 `bd09ll` 属于凭记忆猜测。
"""

MATRIX_MAX_ROWS: dict[str, int] = {"walking": 50, "riding": 50, "driving": 100}
"""批量算路单次路线上限：步行/骑行 50、驾车 100，且起终点个数之积不得超过该值（笔记 §2.8）。"""

PLACE_MAX_KEYWORDS = 10
PLACE_PAGE_SIZE_MAX = 10
PLACE_RADIUS_MAX_M = 10000
GEOCODE_ADDRESS_MAX_BYTES = 128
DIRECTION_COORD_DECIMALS = 6
"""以上数值全部来自笔记 §2.1 / §2.3 / §2.5 / §9.1-②；`page_size` 官方三处矛盾，
按笔记建议取保守值 10（若实测能到 20，只改这一个常量）。"""

TRANSPORT_UNKNOWN_STATUS = -1
"""网络层失败 / body 里读不到 `status` 时用的伪状态码：`spec_for(-1)` 落到 `UNKNOWN_SPEC`，
其处置是「不重试、不开熔断」——这正是「拿不准时不采取行动」。不自己编新文案。"""


@runtime_checkable
class ResponseCache(Protocol):
    """缓存层的窄接口，形状对齐 `infra/cache_store.py` 的异步外壳。

    为什么客户端只认这个协议而不 import 那个模块：缓存是另一条任务线正在写的文件，硬依赖会
    让「客户端单测必须先有 SQLite」变成事实；按协议注入后 CI 传一个 async stub 就能测。

    为什么键的计算不在这里：`infra/cache_keys.py` 把自己定义为「缓存键的唯一口径」，客户端
    再实现一份 sha1 就是第二套口径——两处对参数归一化稍有差别（比如坐标保留几位），命中率
    会静默掉一半而两边单测都还是绿的。
    """

    async def get_async(
        self, service: str, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> Any: ...

    async def set_async(
        self,
        service: str,
        payload: Any,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any: ...


def to_baidu_latlng(value: Any) -> str:
    """把各种写法统一成百度请求里的 **`纬度,经度`** 字符串（最多 6 位小数）。

    为什么需要它：官方三种坐标顺序并存——`location` / `origin` / `destination` 要
    `纬度,经度`，接口返回的 `location` 是 `{lng, lat}` 对象，`steps[].path` 又是
    `经度,纬度` 串（笔记 §6.2 第 3 条）。写反了不会报错，只会让点位整体偏移，是最难发现的
    数据错误；顺序换算收敛到这一个函数里，FR-406 的单测只需要盯它。
    """
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if len(parts) != 2:
            raise BaiduApiError.from_status(2, detail={"latlng": value})
        lat, lng = float(parts[0]), float(parts[1])
    elif isinstance(value, Mapping):
        lat = float(value["lat"])
        lng = float(value["lng"])
    elif isinstance(value, (Sequence, tuple)) and len(value) == 2:
        lat, lng = float(value[0]), float(value[1])  # 约定：(lat, lng)
    else:
        raise BaiduApiError.from_status(2, detail={"latlng": repr(value)})
    return f"{lat:.{DIRECTION_COORD_DECIMALS}f},{lng:.{DIRECTION_COORD_DECIMALS}f}"


def _clean_params(params: Mapping[str, Any]) -> dict[str, str]:
    """把参数表压成 httpx 需要的字符串字典。

    为什么要自己转 `bool`：httpx 会把 `True` 编码成 `"True"`，而百度要的是小写 `true`
    （如 `radius_limit=true`，FAQ 2544）。大写字面量不会报错，只会让参数静默失效。
    """
    cleaned: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            cleaned[key] = str(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            cleaned[key] = "|".join(str(item) for item in value)
        else:
            cleaned[key] = str(value)
    return cleaned


@dataclass
class BaiduResult:
    """一次接口调用的结果 + 本次真实产生的三栏计量。

    为什么把计量挂在结果上而不只写进累加器：等时圈与 POI 任务要把「这一段花了几次出网、
    折了几个配额单位」原样上报给报告层；只给一个总数就没法回答「第二轮花在哪」。
    """

    service: str = ""
    ok: bool = False
    status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    http_requests: int = 0
    place_calls: int = 0
    quota_units: int = 0
    route_lines: int = 0
    mode: str | None = None
    from_cache: bool = False
    blocked: bool = False
    budget_exhausted: bool = False
    needs_interpolation: bool = False
    partial: bool = False
    unreachable: bool = False
    breaker_open: BreakerKind | None = None
    blocked_reason: str | None = None
    error: CircleDocError | None = None
    http_status: int | None = None

    def get(self, path: str, default: Any = None) -> Any:
        """按点号路径取值：`result.get("result.location.lng")`；任何一层缺失都回 `default`。"""
        cursor: Any = self.data
        for part in path.split("."):
            if isinstance(cursor, Mapping):
                if part not in cursor:
                    return default
                cursor = cursor[part]
            elif isinstance(cursor, Sequence) and not isinstance(cursor, (str, bytes)):
                try:
                    cursor = cursor[int(part)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return cursor

    def as_dict(self) -> dict[str, Any]:
        """精简回显（**不含响应体、不含任何密钥**），给日志与 `/api/health` 用。"""
        return {
            "service": self.service,
            "ok": self.ok,
            "status": self.status,
            "attempts": self.attempts,
            "http_requests": self.http_requests,
            "place_calls": self.place_calls,
            "quota_units": self.quota_units,
            "route_lines": self.route_lines,
            "mode": self.mode,
            "from_cache": self.from_cache,
            "blocked": self.blocked,
            "budget_exhausted": self.budget_exhausted,
            "needs_interpolation": self.needs_interpolation,
            "partial": self.partial,
            "unreachable": self.unreachable,
            "blocked_reason": self.blocked_reason,
        }


@dataclass
class RouteMatrixResult(BaiduResult):
    """批量算路结果：`cells[i][j]` 对应第 i 个起点到第 j 个终点。

    为什么要摊成二维而不是直接把 `result[]` 交给上层：官方返回是**一维按 origin 优先展开**
    的（笔记 §2.8），漏一个空结果就会让后面所有下标集体错位——等时圈会安静地把 A 方向的耗时
    当成 B 方向用。这里按 `origins × destinations` 重塑，缺口显式留 `None`。
    """

    origins: int = 0
    destinations: int = 0
    chunks_sent: int = 0
    requested_cells: int = 0
    cells: list[list[dict[str, Any] | None]] = field(default_factory=list)

    @property
    def completed_cells(self) -> int:
        """已拿到数据的格子数；`partial=True` 时上层靠它决定插值权重。"""
        return sum(1 for row in self.cells for cell in row if cell is not None)

    @property
    def completion_ratio(self) -> float:
        """完成比例（0–1）；请求数为 0 时视作 1，避免除零把「没活要干」报成「全失败」。"""
        if self.requested_cells <= 0:
            return 1.0
        return self.completed_cells / self.requested_cells

    def duration_s(self, origin_index: int, dest_index: int) -> float | None:
        """取某起终点的耗时（秒）；缺失返回 ``None``，由上层插值而不是这里猜。"""
        cell = self._cell(origin_index, dest_index)
        if cell is None:
            return None
        value = cell.get("duration")
        if isinstance(value, Mapping):
            value = value.get("value")
        return float(value) if isinstance(value, (int, float)) else None

    def distance_m(self, origin_index: int, dest_index: int) -> float | None:
        """取某起终点的路线距离（米）；`ret_straight_dist=1` 时为直线距离。"""
        cell = self._cell(origin_index, dest_index)
        if cell is None:
            return None
        value = cell.get("distance")
        if isinstance(value, Mapping):
            value = value.get("value")
        return float(value) if isinstance(value, (int, float)) else None

    def missing_cells(self) -> list[tuple[int, int]]:
        """需要插值的下标清单，供等时圈任务定位「哪些方位的测时没跑完」。"""
        return [
            (i, j) for i, row in enumerate(self.cells) for j, cell in enumerate(row) if cell is None
        ]

    def _cell(self, origin_index: int, dest_index: int) -> dict[str, Any] | None:
        try:
            return self.cells[origin_index][dest_index]
        except (IndexError, TypeError):
            return None


@dataclass(frozen=True)
class CallPlan:
    """一次出网的内部描述：走哪个桶、折几个配额单位、是否算地点检索次数。"""

    service: str
    path: str
    params: dict[str, Any]
    mode: str | None = None
    place_call: bool = False
    lines: int = 0

    @property
    def bucket(self) -> str:
        """本次调用归属的**逻辑桶名**（计量与限流共用的标签）。"""
        return logical_bucket_name(self.service, self.mode)


@dataclass
class _SendOutcome:
    """一次底层发送的结果。`transport_error` 与 `payload` 互斥。"""

    payload: dict[str, Any] | None = None
    http_status: int | None = None
    transport_error: httpx.TransportError | None = None


class BaiduClient:
    """百度 Web 服务异步客户端：令牌桶 → 熔断闸门 → 发送 → 按 body `status` 决策。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimitRegistry | None = None,
        breakers: BreakerRegistry | None = None,
        metrics: Metrics | None = None,
        cache: ResponseCache | None = None,
        ak: str | None = None,
        mode_count: int = 1,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        base_url: str = BAIDU_BASE_URL,
    ) -> None:
        """建立客户端。

        `transport` / `http_client` 就是「测试与演练走 mock」的注入口：传 `httpx.MockTransport`
        后本类不会有任何真实出网，令牌桶、熔断、退避与三栏计量照常生效，所以 mock 跑出来的
        usage 与真实链路同口径。

        `mode_count` 是本次分析要跑的模式数（步行=1、步行+骑行=2）；`route` 与 `quota_units`
        预算要按它折算（`Settings.route_lines_budget`），否则双模式第二轮会被守卫误截。
        """
        self.settings = settings or get_settings()
        cfg = self.settings
        self.base_url = base_url.rstrip("/")
        self._ak = (ak if ak is not None else cfg.baidu_server_ak).strip()
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self.mode_count = max(1, int(mode_count))
        self.cache = cache

        self.rate_limiter = rate_limiter or RateLimitRegistry(cfg, clock=clock, sleep=self._sleep)
        self.breakers = breakers or BreakerRegistry(cfg, clock=clock, now=wall_clock)

        self._explicit_metrics = metrics
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                cfg.baidu_read_timeout_s,
                connect=cfg.baidu_connect_timeout_s,
                read=cfg.baidu_read_timeout_s,
                write=cfg.baidu_read_timeout_s,
                pool=cfg.baidu_connect_timeout_s,
            ),
            limits=httpx.Limits(
                max_connections=max(1, cfg.baidu_max_concurrency),
                max_keepalive_connections=max(1, cfg.baidu_max_concurrency),
            ),
            follow_redirects=False,
        )
        # 在途请求上限。为什么还要一道：令牌桶管「每秒放行多少个」，不管「几个还挂在网上」。
        # 百度读超时默认 12 s，2.5 QPS 下最坏情况能堆出 30 个在途请求，本机内存与连接都不够。
        self._semaphore = asyncio.Semaphore(max(1, cfg.baidu_max_concurrency))

        self.live_enabled = bool(self._ak)
        """False 时任何请求都不出网（无 AK / 启动探测判定不能实时），统一抛 typed error。"""

        self.enabled_services: set[str] | None = None
        """启动探测里确认开通的服务；None = 没探测过，不做限制。"""

        self.probe_auth_status: int | None = None
        """探测里首个鉴权类错误码，用于把「实时已关闭」的异常文案对齐真实原因。"""

    # ------------------------------------------------------------------ 构造辅助
    @classmethod
    def from_probe(
        cls,
        probe: ProbeResult,
        settings: Settings | None = None,
        **kwargs: Any,
    ) -> BaiduClient:
        """按 `/api/health` 的启动探测结论建客户端（PRD §4.6「探测即改默认模式」）。

        为什么不崩而是关闸：探测失败是零配置演示的正常状态。这里把 `resolve_data_mode()` 的
        结论翻译成「实时开关 + 未开通服务清单」，上层拿到的仍是同一个可调用的客户端，
        调用只会抛 `BaiduApiError` / `BaiduServiceDisabled`，降级到缓存或合成数据由上层决定。
        """
        client = cls(settings, **kwargs)
        resolved = resolve_data_mode(probe, client.settings.data_mode)
        client.live_enabled = resolved != "mock" and bool(probe.server_ak_present)
        client.probe_auth_status = probe.auth_error_status
        services = dict(probe.services_enabled or {})
        client.enabled_services = {name for name, ok in services.items() if ok} or None
        return client

    # ------------------------------------------------------------------ 生命周期
    async def aclose(self) -> None:
        """关闭底层连接池；只关自己建的那个，注入进来的 `http_client` 归调用方管。"""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> BaiduClient:
        """`async with BaiduClient() as client:` 支持。"""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """退出时释放连接池。"""
        await self.aclose()

    # ------------------------------------------------------------------ 状态回显
    @property
    def metrics(self) -> Metrics:
        """当前生效的三栏累加器：显式注入 > 请求上下文 > 进程级。"""
        return self._explicit_metrics or current_metrics() or get_process_metrics()

    @property
    def has_ak(self) -> bool:
        """是否配了服务端 AK（空串按没配处理，零配置的正常分支）。"""
        return bool(self._ak)

    def budget(self) -> dict[str, int]:
        """本次分析的三栏预算快照（已按 `mode_count` 折算）。"""
        return self.settings.budget_snapshot(self.mode_count)

    def usage(self) -> dict[str, Any]:
        """三栏计量的当前快照，形状即 PRD §4.7 的 `usage` 字段。"""
        return self.metrics.snapshot()

    def remaining_place_calls(self) -> int:
        """地点检索还剩几次出网额度（含地址联想，两者共用 100 次/日）。"""
        return max(0, self.budget()["place_calls"] - self.metrics.place_calls)

    def route_budget_state(self, mode: str | None = None, *, next_lines: int = 0) -> dict[str, Any]:
        """路线预算状态：等时圈任务据此决定「停止测时并用已完成顶点插值」。

        `needs_interpolation=True` 且没有异常抛出，是 FR-403「任一层失败不中断流程」在预算
        维度的体现——截停应当给出一份「部分真实 + 部分插值」的等时圈，而不是让整条分析失败。

        `next_lines` 是下一段打算一次算路折出的条数（批量算路是 `起 × 终`）。守卫拦停的是
        「已用 + 这一段的 projected > 预算」，所以只看 `remaining` 会得出「还剩 4 条、可以继续」
        的错误结论，而实际上下一个 6 条的段就被拦停了；`fits_next_chunk` 才是可用的判据。
        """
        snap = self.metrics.snapshot()
        budget = self.budget()
        used = sum(snap["route_lines"].values())
        by_mode = snap["route_lines"].get(mode, 0) if mode else 0
        remaining = max(0, budget["route_lines"] - used)
        planned = max(0, int(next_lines))
        exhausted = remaining == 0
        return {
            "mode": mode,
            "budget": budget["route_lines"],
            "used": used,
            "used_in_mode": by_mode,
            "remaining": remaining,
            "next_lines": planned,
            "fits_next_chunk": (remaining >= planned) if planned else not exhausted,
            "complete": not exhausted,
            "needs_interpolation": exhausted or remaining < planned,
            "quota_units_budget": budget["quota_units"],
            "quota_units_used": snap["quota_units"],
        }

    def stats(self) -> dict[str, Any]:
        """限流 / 熔断 / 计量三件套的总览，供 `/api/health` 直接回显。"""
        return {
            "live_enabled": self.live_enabled,
            "has_ak": self.has_ak,
            "mode_count": self.mode_count,
            "budget": self.budget(),
            "usage": self.usage(),
            "rate_limiter": self.rate_limiter.snapshot(),
            "breakers": self.breakers.snapshot(),
        }

    # ------------------------------------------------------------------ 公开接口
    async def geocode(self, address: str, *, city: str | None = None) -> BaiduResult:
        """地址 → 坐标。`GET /geocoding/v3/`（笔记 §2.1）。

        `output=json` 必须显式传：官方默认是 **xml**，不传就会拿到 XML 然后在解析处报错。
        """
        if not address or not address.strip():
            raise BaiduApiError.from_status(2, service="geocode", detail={"field": "address"})
        clipped = _clip_bytes(address.strip(), GEOCODE_ADDRESS_MAX_BYTES)
        params: dict[str, Any] = {"address": clipped, "output": "json"}
        if city:
            params["city"] = city
        return await self._execute(CallPlan("geocode", ENDPOINTS["geocode"], params))

    async def reverse_geocode(
        self,
        lnglat: Any,
        *,
        extensions_poi: int | None = None,
        radius: int | None = None,
        poi_types: Sequence[str] | None = None,
        entire_poi: bool | None = None,
        sort_strategy: str | None = None,
        extensions_road: bool | None = None,
    ) -> BaiduResult:
        """坐标 → 地址。`GET /reverse_geocoding/v3/`（笔记 §2.2）。

        `coordtype` 无下划线且传字符串（§6.2）；`location` 是 **`纬度,经度`**。
        """
        params: dict[str, Any] = {
            "location": to_baidu_latlng(lnglat),
            "output": "json",
            "coordtype": BD09LL,
        }
        if extensions_poi is not None:
            params["extensions_poi"] = 1 if int(extensions_poi) else 0
        if radius is not None:
            # 官方：允许区间 0–3000 m，超过按 3000 召回（§2.2）。夹住而不是报错：
            # 传 5000 期望「圈内全量」的调用，报错会中断分析，夹紧只是样本边界更保守。
            params["radius"] = min(max(0, int(radius)), 3000)
        if poi_types:
            params["poi_types"] = "|".join(poi_types)
        if entire_poi is not None:
            params["entire_poi"] = 1 if entire_poi else 0
        if sort_strategy:
            params["sort_strategy"] = sort_strategy
        if extensions_road is not None:
            params["extensions_road"] = extensions_road
        return await self._execute(CallPlan("regeo", ENDPOINTS["regeo"], params))

    async def search_place(
        self,
        query: str,
        *,
        location: Any = None,
        radius: int | None = None,
        region: str | None = None,
        tag: str | None = None,
        page_size: int = PLACE_PAGE_SIZE_MAX,
        page_num: int = 1,
        city_limit: bool | None = None,
        radius_limit: bool | None = None,
        center: Any = None,
        sort_by_distance: bool = False,
        scope: int | None = None,
        extensions_adcode: bool | None = None,
    ) -> BaiduResult:
        """地点检索（圆形 / 行政区划两种形态）。`GET /place/v2/search`（笔记 §2.3）。

        🔴 这是 100 次/日的稀缺服务：每次真正出网都记 `place_calls`（缓存命中为 0），
        并且受 `max_place_calls_per_analysis` 守卫。多边形形态是高级权限、个人不可用，
        所以本方法不暴露 `bounds`。
        """
        keywords = [item for item in query.split("$") if item]
        if not keywords or len(keywords) > PLACE_MAX_KEYWORDS:
            raise BaiduApiError.from_status(
                2,
                service="place",
                detail={"field": "query", "keywords": len(keywords), "max": PLACE_MAX_KEYWORDS},
            )
        circular = location is not None
        if not circular and not region:
            raise BaiduApiError.from_status(
                2, service="place", detail={"field": "location|region", "reason": "两种形态二选一"}
            )
        params: dict[str, Any] = {
            "query": "$".join(keywords),
            "output": "json",
            "coord_type": BD09LL_NUMERIC,
            "page_size": min(max(10, int(page_size)), PLACE_PAGE_SIZE_MAX),
            "page_num": max(1, int(page_num)),
        }
        if circular:
            params["location"] = to_baidu_latlng(location)
            if radius is not None:
                # FAQ 2548：最大 10 km。超过 10 km 官方会退化成「城市范围检索」，
                # 静默返回一份城市级 POI 比直接失败更危险（§4.3 的落圈判定会被整体污染）。
                params["radius"] = min(max(1, int(radius)), PLACE_RADIUS_MAX_M)
            # FAQ 2544：不显式带 `radius_limit=true` 时不严格限域，默认打开。
            params["radius_limit"] = True if radius_limit is None else radius_limit
        else:
            params["region"] = region
            params["city_limit"] = True if city_limit is None else city_limit
        if tag:
            params["tag"] = tag
        if center is not None:
            params["center"] = to_baidu_latlng(center)
        if sort_by_distance:
            # 笔记 §2.3：距离排序需要 `center` + `filter=sort_name:distance`，两者必须成对。
            params["filter"] = "sort_name:distance"
            sort_center = location if location is not None else center
            if sort_center is not None:
                params.setdefault("center", to_baidu_latlng(sort_center))
        if scope is not None:
            params["scope"] = scope
        if extensions_adcode is not None:
            params["extensions_adcode"] = extensions_adcode
        return await self._execute(CallPlan("place", ENDPOINTS["place"], params, place_call=True))

    async def search_place_in_region(
        self,
        query: str,
        *,
        region: str,
        type: str | None = None,
        page_size: int = PLACE_PAGE_SIZE_MAX,
        page_num: int = 1,
        center: Any = None,
        filter: str | None = None,
        address_result: bool | None = None,
        is_light_version: bool | None = None,
    ) -> BaiduResult:
        """行政区划检索 3.0 形态。`GET /place/v3/region`（笔记 §2.3）。

        与 `/place/v2/search` 共用地点检索配额，所以同样记 `place_calls`。
        """
        if not query or not region:
            raise BaiduApiError.from_status(2, service="place", detail={"field": "query|region"})
        params: dict[str, Any] = {
            "query": query,
            "region": region,
            "output": "json",
            "page_size": min(max(10, int(page_size)), PLACE_PAGE_SIZE_MAX),
            "page_num": max(1, int(page_num)),
        }
        if type:
            params["type"] = type
        if center is not None:
            params["center"] = to_baidu_latlng(center)
        if filter:
            params["filter"] = filter
        if address_result is not None:
            params["address_result"] = address_result
        if is_light_version is not None:
            params["is_light_version"] = is_light_version
        return await self._execute(
            CallPlan("place", ENDPOINTS["place_region"], params, place_call=True)
        )

    async def suggest(
        self,
        query: str,
        *,
        region: str | None = None,
        city_limit: bool | None = None,
    ) -> BaiduResult:
        """地点输入提示（FR-108 地址联想）。`GET /place/v2/suggestion`（笔记 §2.4）。

        ⚠️ 与地点检索**共用 100 次/日配额**（FAQ 2540 / 2549），所以照样记进 `place_calls`，
        并且和 place 共用同一个令牌桶（`infra/ratelimit.DEFAULT_POOL_ALIASES`）。
        """
        if not query:
            raise BaiduApiError.from_status(2, service="suggest", detail={"field": "query"})
        if not region:
            # 官方把 region 列为必选；缺了会拿到 2 参数非法，不如在本地先挡下来。
            raise BaiduApiError.from_status(2, service="suggest", detail={"field": "region"})
        params: dict[str, Any] = {"query": query, "region": region, "output": "json"}
        if city_limit is not None:
            params["city_limit"] = city_limit
        # 笔记 §2.4 未记载 suggestion 有坐标系入参，按 §2 开头「未列出的参数不要假设存在」不发。
        return await self._execute(
            CallPlan("suggest", ENDPOINTS["suggest"], params, place_call=True)
        )

    async def direction(
        self,
        mode: str,
        origin: Any,
        destination: Any,
        *,
        lite: bool = False,
        steps_info: int | None = None,
        riding_type: int | None = None,
        road_prefer: int | None = None,
        origin_uid: str | None = None,
        destination_uid: str | None = None,
    ) -> BaiduResult:
        """单条路线规划：`/direction/v2/{walking|riding}` 或 `/directionlite/v1/...`。

        `origin` / `destination` 是 **`纬度,经度`**（§2.5，与多数接口相反）。一次调用折
        1 条路线 = 1 个配额单位，落进 `route_lines_by_mode[mode]` 分栏。
        """
        normalized = _require_travel_mode(mode)
        key = f"directionlite_v1_{normalized}" if lite else f"direction_v2_{normalized}"
        try:
            path = ENDPOINTS[key]
        except KeyError as exc:
            raise BaiduApiError.from_status(
                260, service="direction", detail={"mode": normalized, "lite": lite}
            ) from exc
        params: dict[str, Any] = {
            "origin": to_baidu_latlng(origin),
            "destination": to_baidu_latlng(destination),
            "output": "json",
            "coord_type": BD09LL,
        }
        if lite:
            if steps_info is not None:
                # §2.7：`steps_info=0` 不下发 step 详情，纯测时场景字节量最小。
                params["steps_info"] = 1 if int(steps_info) else 0
        elif normalized == "riding":
            if riding_type is not None:
                params["riding_type"] = riding_type
            if road_prefer is not None:
                params["road_prefer"] = road_prefer
        if origin_uid:
            params["origin_uid"] = origin_uid
        if destination_uid:
            params["destination_uid"] = destination_uid
        return await self._execute(CallPlan("direction", path, params, mode=normalized, lines=1))

    async def route_matrix(
        self,
        mode: str,
        origins: Iterable[Any],
        destinations: Iterable[Any],
        *,
        ret_straight_dist: int | None = None,
        riding_type: int | None = None,
        road_prefer: int | None = None,
        max_rows_per_request: int | None = None,
    ) -> RouteMatrixResult:
        """批量算路（FR-405 主链路）。`GET /routematrix/v2/{walking|riding}`（笔记 §2.8）。

        为什么是它而不是逐条 direction：1 个 HTTP 请求能换 ≤50 条路线，把 ≈96 次串行请求压到
        2–3 次；⚠️ **配额消耗完全不变**（官方按最终路线数计，2 起点 × 5 终点 = 计 10 次），
        所以这里折出来的 `quota_units` 与 direction 一模一样，只是 `http_requests` 大降。

        预算耗尽时不抛异常：返回 `partial=True + needs_interpolation=True + missing_cells()`，
        等时圈任务据此用已完成顶点插值（PRD §4.6 配额预算守护）。
        """
        normalized = _require_travel_mode(mode)
        origin_list = [to_baidu_latlng(item) for item in origins]
        dest_list = [to_baidu_latlng(item) for item in destinations]
        if not origin_list or not dest_list:
            raise BaiduApiError.from_status(
                2,
                service="routematrix",
                detail={"origins": len(origin_list), "destinations": len(dest_list)},
            )
        cap = int(max_rows_per_request or MATRIX_MAX_ROWS.get(normalized, 50))
        per_chunk_dests = max(1, cap // len(origin_list))  # 保证 起×终 ≤ cap
        path_key = f"routematrix_v2_{normalized}"
        try:
            path = ENDPOINTS[path_key]
        except KeyError as exc:
            raise BaiduApiError.from_status(
                260, service="routematrix", detail={"mode": normalized}
            ) from exc

        grid: list[list[dict[str, Any] | None]] = [[None] * len(dest_list) for _ in origin_list]
        aggregate = RouteMatrixResult(
            service="routematrix",
            mode=normalized,
            origins=len(origin_list),
            destinations=len(dest_list),
            requested_cells=len(origin_list) * len(dest_list),
        )
        base_params: dict[str, Any] = {"coord_type": BD09LL, "output": "json"}
        if ret_straight_dist is not None:
            base_params["ret_straight_dist"] = ret_straight_dist
        if normalized == "riding":
            if riding_type is not None:
                base_params["riding_type"] = riding_type
            if road_prefer is not None:
                base_params["road_prefer"] = road_prefer

        for start in range(0, len(dest_list), per_chunk_dests):
            group = dest_list[start : start + per_chunk_dests]
            lines = len(origin_list) * len(group)
            params = dict(base_params)
            params["origins"] = "|".join(origin_list)
            params["destinations"] = "|".join(group)
            plan = CallPlan("routematrix", path, params, mode=normalized, lines=lines)
            result = await self._execute(plan)
            aggregate.attempts += result.attempts
            aggregate.http_requests += result.http_requests
            aggregate.place_calls += result.place_calls
            aggregate.quota_units += result.quota_units
            aggregate.route_lines += result.route_lines

            if result.blocked:
                # 预算拦停：已经跑完的分段保留，剩余分段交给上层插值。
                aggregate.blocked = True
                aggregate.budget_exhausted = result.budget_exhausted
                aggregate.blocked_reason = result.blocked_reason
                aggregate.needs_interpolation = True
                aggregate.partial = True
                aggregate.error = result.error
                break
            aggregate.chunks_sent += 1
            aggregate.status = result.status
            if result.unreachable:
                aggregate.unreachable = True
                continue
            _merge_matrix_rows(grid, start, len(group), result)

        aggregate.cells = grid
        incomplete = aggregate.completed_cells < aggregate.requested_cells
        aggregate.partial = aggregate.partial or incomplete
        aggregate.needs_interpolation = aggregate.needs_interpolation or incomplete
        aggregate.ok = aggregate.completed_cells > 0
        aggregate.data = {"result_cells": aggregate.completed_cells}
        return aggregate

    # ------------------------------------------------------------------ 内部：一次出网
    async def _execute(self, plan: CallPlan) -> BaiduResult:
        """单个请求的完整生命周期：闸门 → 缓存 → 令牌 → 发送 → 按 body `status` 决策。

        返回值只用于「预算拦停 / 缓存命中 / 该方向不可达」这三类**数据性**结果；所有故障
        一律抛 `infra/errors.py` 的 typed error（上层一次 `except CircleDocError` 就能降级）。
        """
        gate = self._gate_error(plan.service)
        if gate is not None:
            raise gate

        cached = await self._cache_lookup(plan)
        if cached is not None:
            return cached

        blocked = self._budget_block(plan)
        if blocked is not None:
            return blocked

        decision = self.breakers.before_request(plan.service)
        if not decision.allowed:
            error = self.breakers.error_for(plan.service)
            log_fields(logger, logging.WARNING, "blocked_by_breaker", **decision.as_dict())
            raise error
        probe_taken = decision.reason == "concurrency_breaker_probe"

        settled = False
        try:
            max_attempts = max(1, int(self.settings.retry_max_attempts))
            attempt = 0
            while True:
                attempt += 1
                waited = await self.rate_limiter.acquire(plan.service, mode=plan.mode)
                if waited:
                    log_fields(
                        logger,
                        logging.DEBUG,
                        "rate_limiter_wait",
                        bucket=plan.bucket,
                        waited_s=round(waited, 3),
                    )
                outcome = await self._send_once(plan)

                if outcome.transport_error is not None:
                    self.breakers.record_failure(plan.service, transport=True)
                    settled = True
                    if attempt <= max_attempts:
                        await self._sleep(self._backoff_delay(None, attempt, transport=True))
                        continue
                    raise BaiduApiError(
                        status=TRANSPORT_UNKNOWN_STATUS,
                        service=plan.service,
                        detail={
                            "transport_error": type(outcome.transport_error).__name__,
                            "attempts": attempt,
                        },
                    ) from outcome.transport_error

                status = _body_status(outcome.payload)
                if status is None:
                    # 读不到 body `status`（HTTP 500 的 HTML、空响应、非 JSON）。
                    # 🔴 这里绝不当成限流信号：官方没有任何 HTTP 层限流语义（笔记 §3.2），
                    # 按 HTTP 码退避会把 5xx 误判成 401 并错误地打开 concurrency_breaker。
                    settled = True
                    raise BaiduApiError(
                        status=TRANSPORT_UNKNOWN_STATUS,
                        service=plan.service,
                        detail={
                            "reason": "status_missing_in_body",
                            "http_status": outcome.http_status,
                            "attempts": attempt,
                        },
                    )

                if status == OK_STATUS:
                    self.breakers.record_success(plan.service)
                    settled = True
                    await self._cache_store(plan, outcome.payload or {})
                    return self._success(plan, outcome, attempt)

                spec = spec_for(status)
                self.breakers.record_failure(plan.service, status=status)
                settled = True

                if spec.counts_as_unreachable:
                    # 2001 / 7：这是「该方向没有路线」的数据，不是故障（errors.py 已标
                    # degrade_to_sample=False），必须作为结果返回，否则 FR-206 的 blocked
                    # 判定会把「确实走不通」读成「接口坏了」。
                    return self._unreachable(plan, outcome, status, attempt)

                retries_allowed = min(spec.retry_max_attempts, max_attempts)
                if is_retryable_status(status) and attempt <= retries_allowed:
                    await self._sleep(self._backoff_delay(spec, attempt))
                    continue
                raise BaiduApiError.from_status(
                    status,
                    service=plan.service,
                    detail={"attempts": attempt, "http_status": outcome.http_status},
                )
        finally:
            if probe_taken and not settled:
                # 半开探测被取消 / 抛了非预期异常：名额必须归还，否则该服务永久停在半开。
                self.breakers.concurrency.release_probe(plan.service)

    async def _send_once(self, plan: CallPlan) -> _SendOutcome:
        """真正发一次 HTTP 请求；**三栏计量只在这里累加**（没出网就不记账）。"""
        url = self._allowed_url(plan.path)
        label = self._meter_label(plan)
        async with self._semaphore:
            metrics = self.metrics
            metrics.add_http_requests(1, service=label)
            if plan.place_call:
                metrics.add_place_calls(1, service=label)
            if plan.lines:
                metrics.add_route_lines(plan.lines, mode=plan.mode or "unspecified")
            params = _clean_params({**plan.params, "ak": self._ak})
            try:
                response = await self._http.get(url, params=params)
            except httpx.TimeoutException as exc:
                return _SendOutcome(transport_error=exc)
            except httpx.TransportError as exc:
                return _SendOutcome(transport_error=exc)
        payload: dict[str, Any] | None = None
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
        return _SendOutcome(payload=payload, http_status=response.status_code)

    def _success(self, plan: CallPlan, outcome: _SendOutcome, attempt: int) -> BaiduResult:
        """成功结果：响应体原样返回，附上本次真实产生的三栏计数。"""
        payload = outcome.payload or {}
        quota_units = plan.lines
        return BaiduResult(
            service=plan.bucket,
            ok=True,
            status=OK_STATUS,
            data=payload,
            attempts=attempt,
            http_requests=1,
            place_calls=1 if plan.place_call else 0,
            quota_units=quota_units,
            route_lines=plan.lines,
            mode=plan.mode,
            http_status=outcome.http_status,
        )

    def _unreachable(
        self, plan: CallPlan, outcome: _SendOutcome, status: int, attempt: int
    ) -> BaiduResult:
        """该方向不可达：返回结果而不是抛异常，同时挂上 typed error 供上层按需处理。"""
        return BaiduResult(
            service=plan.bucket,
            ok=False,
            status=status,
            data=outcome.payload or {},
            attempts=attempt,
            http_requests=1,
            quota_units=plan.lines,
            route_lines=plan.lines,
            mode=plan.mode,
            unreachable=True,
            blocked=True,
            blocked_reason="route_unreachable",
            error=RouteUnreachable(status=status, service=plan.service),
            http_status=outcome.http_status,
        )

    def _backoff_delay(
        self, spec: ErrorSpec | None, attempt: int, *, transport: bool = False
    ) -> float:
        """指数退避秒数。`401` 的首次退避有下限 1 s：QPS 窗口按秒计，退避太短等于白重试。"""
        cfg = self.settings
        if transport:
            base = cfg.retry_base_delay_s
        else:
            base = (spec.retry_base_delay_s if spec is not None else cfg.retry_base_delay_s) or (
                cfg.retry_base_delay_s
            )
        if spec is not None and spec.breaker is BreakerKind.CONCURRENCY:
            base = max(base, cfg.retry_concurrency_first_delay_s)
        return min(cfg.retry_max_delay_s, base * (2 ** (attempt - 1)))

    def _gate_error(self, service: str) -> CircleDocError | None:
        """实时不可用时的 typed error：不崩、不出网，让上层降级到缓存或合成数据。"""
        if not self.live_enabled or not self.has_ak:
            status = self.probe_auth_status if self.probe_auth_status is not None else 101
            return BaiduApiError.from_status(
                status,
                service=service,
                detail={
                    "live_enabled": self.live_enabled,
                    "has_ak": self.has_ak,
                    "no_request_sent": True,
                },
            )
        if self.enabled_services is not None and service not in self.enabled_services:
            return BaiduApiError.from_status(
                240, service=service, detail={"reason": "not_enabled_by_probe"}
            )
        return None

    def _budget_block(self, plan: CallPlan) -> BaiduResult | None:
        """出网前的预算守卫（PRD §4.6）。超限**返回信号**而不是抛异常。"""
        budget = self.budget()
        snap = self.metrics.snapshot()
        reason: str | None = None
        needs_interpolation = False

        if plan.place_call and snap["place_calls"] + 1 > budget["place_calls"]:
            reason = "place_calls"
        used_lines = sum(snap["route_lines"].values())
        if plan.lines and used_lines + plan.lines > budget["route_lines"]:
            reason = "route_lines"
            needs_interpolation = True
        if plan.lines and snap["quota_units"] + plan.lines > budget["quota_units"]:
            reason = reason or "quota_units"
            needs_interpolation = True
        if reason is None and snap["http_requests"] + 1 > budget["http_requests"]:
            reason = "http_requests"

        if reason is None:
            return None
        self.metrics.add_degradation(1)
        log_fields(
            logger,
            logging.WARNING,
            "budget_guard_stop",
            bucket=plan.bucket,
            reason=reason,
            usage={
                "place_calls": snap["place_calls"],
                "quota_units": snap["quota_units"],
                "http_requests": snap["http_requests"],
                "route_lines": used_lines,
            },
            budget=budget,
        )
        return BaiduResult(
            service=plan.bucket,
            ok=False,
            attempts=0,
            mode=plan.mode,
            blocked=True,
            budget_exhausted=True,
            needs_interpolation=needs_interpolation,
            partial=True,
            blocked_reason=reason,
        )

    def _meter_label(self, plan: CallPlan) -> str:
        """三栏计量在 `by_service` 里用的归组名：**配额池名**而不是逻辑桶名。

        为什么：`Metrics.add_route_lines` 内部把 quota_units 记在 `route` 键下（它的签名没有
        service 参数），若这里用 `route_walking` 记账，同一次调用会拆到两个键上，上层要合并
        才看得清真实用量。步行/骑行要分栏看 `route_lines_by_mode`，不该靠这个标签。
        """
        return self.rate_limiter.pool_for(plan.service, plan.mode)

    def _allowed_url(self, path: str) -> str:
        """拼 URL 并核对域名白名单（PRD §5：防 SSRF）。"""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        host = urlparse(url).hostname or ""
        if host not in self.settings.allowed_hosts:
            raise CircleDocError(
                message=f"出网域名不在白名单：{host}",
                detail={"host": host, "allowed": list(self.settings.allowed_hosts)},
            )
        return url

    # ------------------------------------------------------------------ 内部：缓存
    async def _cache_lookup(self, plan: CallPlan) -> BaiduResult | None:
        """命中缓存就返回——**一次出网都没有**，所以 `place_calls` / `http_requests` 全为 0。"""
        if self.cache is None:
            return None
        try:
            outcome = await self.cache.get_async(plan.bucket, plan.params)
        except Exception as exc:  # 缓存层坏了不能连带打死实时链路（FR-403）
            log_fields(logger, logging.WARNING, "cache_read_failed", error=type(exc).__name__)
            return None
        payload = _cache_payload(outcome)
        if payload is None:
            return None
        self.metrics.add_cache_hit(1)
        return BaiduResult(
            service=plan.bucket,
            ok=True,
            status=OK_STATUS,
            data=payload,
            from_cache=True,
            mode=plan.mode,
        )

    async def _cache_store(self, plan: CallPlan, payload: Mapping[str, Any]) -> None:
        """只存成功响应：错误响应不进缓存，防止一次抖动污染整天结果（PRD §4.6 缓存表）。

        不传 `ttl_s`：TTL 与合规上限由 `infra/cache_keys.py` 统一裁决。客户端再写一张 TTL 表
        就是第二套口径，将来「POI 24 h 上限」改动只改一处时，另一处会静默继续写 3 天。
        """
        if self.cache is None:
            return
        self.metrics.add_cache_miss(1)
        try:
            await self.cache.set_async(plan.bucket, payload, plan.params)
        except Exception as exc:  # 同上：写失败降级为「这次没缓存住」，不影响本次结果
            log_fields(logger, logging.WARNING, "cache_write_failed", error=type(exc).__name__)


def _cache_payload(outcome: Any) -> dict[str, Any] | None:
    """把缓存层的返回值压成响应体；未命中给 ``None``。

    为什么用 duck typing 而不是 import `CacheResult`：缓存那边同时存在「返回 CacheResult」
    与「直接返回 payload」两种外壳（`get` / `get_payload`），客户端只认「有没有命中、
    body 是不是字典」，就不必跟着那边的类型改名一起改。
    """
    if outcome is None:
        return None
    if isinstance(outcome, Mapping):
        return dict(outcome)
    if not getattr(outcome, "hit", False):
        return None
    payload = getattr(outcome, "payload", None)
    return dict(payload) if isinstance(payload, Mapping) else None


def _body_status(payload: Mapping[str, Any] | None) -> int | None:
    """从响应体里取 `status`；取不到返回 ``None``（调用方按「非官方语义」处理）。"""
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("status")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return int(raw)
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _require_travel_mode(mode: str) -> str:
    """出行方式归一。个人档只有步行 / 骑行 / 驾车可用，摩托与境外为「-」（笔记 §3.1）。"""
    normalized = (mode or "").strip().lower()
    if normalized not in {"walking", "riding", "driving"}:
        raise BaiduApiError.from_status(
            2, service="direction", detail={"field": "mode", "value": mode}
        )
    return normalized


def _merge_matrix_rows(
    grid: list[list[dict[str, Any] | None]], dest_start: int, group_size: int, result: BaiduResult
) -> None:
    """把批量算路的一维 `result[]` 填回 `cells[origin][dest]`。

    官方顺序是 origin 优先展开（origin1-dest1, origin1-dest2, …，笔记 §2.8），本次实现固定
    传全部 origins，所以一行分段就是 `origins × group_size` 个元素。
    """
    rows = (result.data or {}).get("result")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return
    origins = len(grid)
    for index, row in enumerate(rows):
        origin_index = index // group_size
        dest_offset = index % group_size
        if origin_index >= origins or dest_start + dest_offset >= len(grid[origin_index]):
            continue
        if isinstance(row, Mapping):
            grid[origin_index][dest_start + dest_offset] = dict(row)


def _clip_bytes(text: str, limit: int) -> str:
    """按 **字节** 截断（地理编码 `address` 上限 128 字节，笔记 §2.1）。

    为什么按字节而不是字符：中文一个字符 3 字节，按 128 字符截断会发出 384 字节的请求，
    官方直接回 `status=2`，而本地看着一切正常。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")
