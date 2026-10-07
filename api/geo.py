"""`/api/geocode`、`/api/regeo`、`/api/suggest` 三个路由（PRD §4.7）。

本文件只做 HTTP 形状的事：把查询参数交给 `services/geo.py`，把 `GeoOutcome` 原样序列化，
把异常交给 `api/app.py` 的处理器出可读 JSON。

三条边界都刻意留在 `services/`，不在路由里判：
- 空地址 / 坐标格式 / 关键字太短 / 缺 `region` → `InvalidGeoParam`（400 + 指名字段）；
- 境外坐标与境外地址结果 → `OutsideServiceArea`（400 + 说明个人档境外无配额）；
- 空 `results`、配额耗尽、预算截停、网络超时 → **200** + `degraded` + `degraded_reason`
  （§6.2「模拟空 results / 网络超时 → 前端收到可读提示且流程继续」）。

之所以用 `str | None = Query(None)` 而不是 `Query(...)` 声明必填：FastAPI 的必填校验出的是
422 通用文案「提交的参数不完整或有误」，而 FR-101 的现场最需要知道的是**哪个字段**为什么不行
（「地址不能为空」「坐标应为『经度,纬度』」）。必填性仍由服务层保证，OpenAPI 里靠 description
标注；这是可读性与契约之间的取舍，不是漏校验。
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from infra.config import DataMode
from infra.log import get_request_id
from services.geo import GeoOutcome, GeoService
from services.geo import get_geo_service as default_geo_service

__all__ = ["router"]

router = APIRouter()

_MODE_DOC = (
    "本次取数策略：`live` 允许实时调用、`cache` 优先本地缓存、`mock` 强制不出网。"
    "缺省沿用 `/api/health` 启动探测解析出的运行模式（FR-105）。"
)


def get_geo_service(request: Request) -> GeoService:
    """依赖注入点：`app.state.geo_service` 优先，其次进程级单例。

    为什么留这个口子：自测与演练要注入 `BaiduClient(transport=httpx.MockTransport(...))`
    才能做到 0 次真实出网（条款 3.2.3(ii) 禁止对线上服务施压）。装配方不需要改 `api/app.py`，
    挂一个 `app.state.geo_service` 就能把整条链换成 mock。这里用鸭子类型判断而不是
    `isinstance`，是为了让 QA 能塞一个自己写的假对象。
    """
    injected = getattr(request.app.state, "geo_service", None)
    if injected is not None:
        return cast(GeoService, injected)
    return default_geo_service()


def _envelope(outcome: GeoOutcome) -> dict[str, Any]:
    """响应体 = §4.7 出参字段 + 信封（`data_source` / `usage` / `meta` / `request_id`）。

    `request_id` 从 `infra.log` 的上下文变量取（中间件已绑定），而不是再生成一个：
    两处各写一份迟早会不一致，而排查现场靠的就是响应体里的号和日志里的号能对上。
    """
    body = outcome.as_dict()
    body["request_id"] = get_request_id()
    return body


@router.get(
    "/geocode",
    summary="地址 → 坐标（FR-101 方式②）",
    description=(
        "内部 `/geocoding/v3/`，`output=json` 必须显式传（官方默认是 XML，笔记 §2.1）。"
        "返回的 `lnglat` 为 **BD09LL 的 `[经度, 纬度]`**；`confidence` / `precision` 供前端"
        "提示定位精度。境外地址一律拒绝（个人档境外地理编码无配额）。"
    ),
)
async def geocode(
    address: str | None = Query(None, description="地址文本，必填，最长 128 字节（官方上限）"),
    city: str | None = Query(None, description="城市名，可选，用于提高解析精度"),
    data_mode: DataMode | None = Query(None, description=_MODE_DOC),
    service: GeoService = Depends(get_geo_service),
) -> dict[str, Any]:
    """把地址文本换成地图中心点。"""
    outcome = await service.geocode(address or "", city=city, data_mode=data_mode)
    return _envelope(outcome)


@router.get(
    "/regeo",
    summary="坐标 → 结构化地址（FR-101 方式③ / FR-103）",
    description=(
        "内部 `/reverse_geocoding/v3/`。⚠️ 坐标系参数名是 **`coordtype`**（无下划线、传字符串 "
        "`bd09ll`），请求里的 `location` 是 **`纬度,经度`**，而本接口的入参 `lnglat` 是"
        "**`经度,纬度`**——换算只在 `services/geo.py` 做一次（笔记 §6.2）。"
        "`town` / `town_code` / `adcode` / `street` 直接取官方 `addressComponent`，"
        "不对 `formatted_address` 做文本切分（PRD §4.7）。"
    ),
)
async def regeo(
    lnglat: str | None = Query(
        None, description="中心点坐标，必填，格式「经度,纬度」（BD09LL），如 116.404,39.915"
    ),
    data_mode: DataMode | None = Query(None, description=_MODE_DOC),
    service: GeoService = Depends(get_geo_service),
) -> dict[str, Any]:
    """把地图点击 / 用户粘贴的坐标换成行政区与街道。"""
    outcome = await service.regeo(lnglat or "", data_mode=data_mode)
    return _envelope(outcome)


@router.get(
    "/suggest",
    summary="地址联想（FR-108）",
    description=(
        "内部 `/place/v2/suggestion`，**不复用 `/api/geocode`**（地理编码只回一个坐标、"
        "没有候选列表）。⚠️ 与 `/api/poi` **共用每日 100 次**地点检索配额（笔记 FAQ 2540 / "
        "2549），因此**默认关闭**、输入门槛 ≥ 3 字、结果走 POI 档缓存（24 h）。"
        "关闭时返回「未启用」的可读提示（HTTP 200，不发任何实时请求），不是报错。"
    ),
)
async def suggest(
    keyword: str | None = Query(None, description="联想关键字，必填，至少 3 个字（支持拼音）"),
    region: str | None = Query(None, description="城市名，必填（官方把 region 列为必选），如 北京"),
    city_limit: bool | None = Query(None, description="是否只返回该城市的结果，可选"),
    data_mode: DataMode | None = Query(None, description=_MODE_DOC),
    service: GeoService = Depends(get_geo_service),
) -> dict[str, Any]:
    """输入框的中文搜索联想。"""
    outcome = await service.suggest(
        keyword or "", region=region, city_limit=city_limit, data_mode=data_mode
    )
    return _envelope(outcome)
