"""`/api/isochrone` 与 `/api/isochrone/polyline` 两个路由（PRD §4.7 / FR-201 / FR-203）。

本文件只做 HTTP 形状的事：把请求体交给 `services/isochrone_service.py`，把 `IsochroneOutcome`
/ `PolylineOutcome` 原样序列化，异常交给 `api/app.py` 的处理器出统一 JSON。判据、二分、计量
全在服务层，路由里不重复算任何一遍。

与 `api/geo.py` 的三点口径保持一致（FR-404「所有接口的错误形状逐字段一致」）：

* 入参边界**不在路由里判**：`mode` 白名单 / `minutes` 区间 / 坐标格式 → 服务层抛
  `InvalidIsochroneParam`（400 + 指名字段），境外坐标 → `OutsideServiceArea`（400）。
  因此请求体字段一律声明为可选、且宽松类型（`int | float | str | None`）：FastAPI 的
  必填与类型校验出的是 422 通用文案「提交的参数不完整或有误」，而演示现场最需要知道的是
  **哪个字段为什么不行**（「坐标应为『经度,纬度』」「出行方式只支持 walking 与 riding」）。
  必填性由服务层保证，OpenAPI 里靠 description 标注。
* 取数失败 / 预算截停 / 配额耗尽 → **200** + `degraded` + 可读 `message`，不返 5xx
  （FR-403「任一接口失败不导致整体崩溃」；§6.2）。
* `request_id` 从 `infra.log` 的上下文变量取（中间件已绑定），不另生成，否则响应体里的号
  和日志里的号迟早对不上。

唯一的例外是 `data_mode`：它是枚举开关，取值非法属于「没打开对的档」而不是「某个业务字段
填错」，在服务层回落运行期默认值之前先在这里点名一次（同样走 `InvalidIsochroneParam`，
错误形状不变，只是不新造第二套语义）。
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from infra.config import DataMode
from infra.log import get_request_id
from pydantic import BaseModel, Field
from services.isochrone_service import (
    InvalidIsochroneParam,
    IsochroneOutcome,
    IsochroneService,
    PolylineOutcome,
)
from services.isochrone_service import get_isochrone_service as default_isochrone_service

__all__ = ["IsochroneRequest", "PolylineRequest", "router"]

router = APIRouter()

_DATA_MODES = ("live", "cache", "mock")

_MODE_DOC = (
    "本次取数策略：`live` 允许实时调用、`cache` 优先本地缓存、`mock` 强制不出网（FR-105）。"
    "缺省沿用 `/api/health` 启动探测解析出的运行模式。"
)
_LNGLAT_DOC = "中心点坐标，必填，格式「经度,纬度」（BD09LL），如 116.404,39.915"
_MODE_VALUE_DOC = (
    "出行方式，必填：`walking`（步行）/ `riding`（骑行）。FR-206：两者共用每日 5000 条路线配额。"
)
_MINUTES_DOC = "目标时长（分钟），必填，合理区间 5~60（FR-104 的档位是 5/10/15/30）。"


class IsochroneRequest(BaseModel):
    """`POST /api/isochrone` 的请求体（§4.7 入参）。

    字段类型刻意宽松（`int | float | str | None`）：数字写成字符串 `"15"` 时前端不该看到 422，
    而是由服务层 `float()` 之后按同一套区间规则判定，错误文案与 GET 接口完全一致。
    """

    lnglat: str | list[float] | dict[str, float] | None = Field(None, description=_LNGLAT_DOC)
    mode: str | None = Field(None, description=_MODE_VALUE_DOC)
    minutes: int | float | str | None = Field(None, description=_MINUTES_DOC)
    data_mode: str | None = Field(None, description=_MODE_DOC)


class PolylineRequest(IsochroneRequest):
    """`POST /api/isochrone/polyline` 的请求体：中心点 + 被点中的那个顶点。

    顶点位置有两种给法，都接受（前端的 GeoJSON 顶点已带 `lnglat`，而 `vertices[]` 里也有
    `bearing` / `distance_m`）：优先 `point`；缺 `point` 时才用 `bearing` + `distance_m`
    球面正算回推点。
    """

    point: str | list[float] | dict[str, float] | None = Field(
        None, description="顶点坐标，格式「经度,纬度」（BD09LL）"
    )
    bearing: int | float | str | None = Field(
        None, description="顶点方位角（度，正北为 0、顺时针），与 distance_m 成对使用"
    )
    distance_m: int | float | str | None = Field(
        None, description="顶点的推点直线距离（米），与 bearing 成对使用"
    )
    lite: bool | None = Field(
        None,
        description=(
            "只要坐标串不要逐段说明：省 ~40% 响应体（D8）。" "真实调用里这是 direction 的官方参数。"
        ),
    )


def get_isochrone_service(request: Request) -> IsochroneService:
    """依赖注入点：`app.state.isochrone_service` 优先，其次进程级单例。

    与 `api/geo.py:41-50` 同一套口子，理由相同：等时圈是**最烧配额**的接口，条款 3.2.3(ii)
    禁止对线上服务施压，自测必须注入假 client（`httpx.MockTransport` 或直接塞假对象）才能做
    到 0 次真实出网。用鸭子类型判断而不是 `isinstance`，QA 才能塞自己写的假对象。
    """
    injected = getattr(request.app.state, "isochrone_service", None)
    if injected is not None:
        return cast(IsochroneService, injected)
    return default_isochrone_service()


def _envelope(outcome: IsochroneOutcome | PolylineOutcome) -> dict[str, Any]:
    """响应体 = 服务层结论 + `request_id`（信封字段与 `/api/geocode` 逐字对齐）。"""
    body = outcome.as_dict()
    body["request_id"] = get_request_id()
    return body


def _data_mode(value: str | None) -> DataMode | None:
    """`data_mode` 的枚举归一：只认 `live|cache|mock`，其余点名报错而不是静默回落。"""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().lower()
    if raw not in _DATA_MODES:
        raise InvalidIsochroneParam(
            f"取数策略只支持 {'/'.join(_DATA_MODES)}，收到 {value!r}。",
            field_name="data_mode",
            value=value,
            allowed=list(_DATA_MODES),
        )
    return cast(DataMode, raw)


@router.post(
    "/isochrone",
    summary="等时圈多边形 + 耗时场（FR-201 / FR-202 / FR-205）",
    description=(
        "内部 `route/v2/matrix/`（1 起点 × ≤50 终点，`option=both` 同时取直线与实际行驶距离）。"
        "⚠️ 官方参数坑（笔记 §3，`infra/baidu_client.py` 已处理，这里只记录契约）："
        "**请求里的 `locations` 是 `纬度,经度`、坐标在前、小数截到 6 位**；`outputs` 反过来是"
        "「经度,纬度」；坐标系只有 **BD09LL**，无 `coordtype` 参数；`data` 是 JSON 字符串"
        "（必须 `urllib.parse.quote`）；配额口径是 **`origins × destinations`**，与 HTTP 请求数"
        "是两个数（`usage.quota_units` vs `usage.http_requests`，§4.2 验收口径：约 72~96 条配额"
        "但只有 5~8 次 HTTP）。**算法细节**：12 方向粗扫（30°）→ 逐方向二分（±30 s / ≤5 轮 / "
        "间隔 <20 m 即停）→ 相邻顶点耗时差 >60 s 才在中间补 15° 方向（FR-204）→ 按方位角升序 + "
        "Douglas-Peucker 成环（阻隔顶点受保护不被简化，PRD 自验第 2 条）"
        "→ IDW 耗时场（250 m 格网）。"
        "顶点 `sample` 五档不合并：`measured`/`interpolated`/`extrapolated`/"
        "`blocked_official`/`blocked_timeout`；`blocked_*` 各占一档、各自可数，阻隔证据原样透出。"
        "无地图 / 境外 / 预算截停一律 **200 + degraded + 可读文案**（FR-403）。"
    ),
)
async def isochrone(
    payload: IsochroneRequest | None = None,
    service: IsochroneService = Depends(get_isochrone_service),
) -> dict[str, Any]:
    """扇形采样 + 二分收敛，长出目标时长内的不规则可达圈。"""
    body = payload or IsochroneRequest()
    outcome = await service.compute(
        body.lnglat or "",
        mode=body.mode or "walking",
        minutes=15.0 if body.minutes is None else body.minutes,  # type: ignore[arg-type]
        data_mode=_data_mode(body.data_mode),
    )
    return _envelope(cast(IsochroneOutcome, outcome))


@router.post(
    "/isochrone/polyline",
    summary="点中顶点才补调真实路径（FR-203 / D8）",
    description=(
        "内部 `direction/v2/{walking|riding}`。**懒加载**：采样阶段不发这类请求，只有用户点某条"
        "边才为那个方向取坐标串；⚠️ 单次分析的 direction 调用**硬上限 12 次**，超限不报错、"
        "回 `allowed=false` + 可读文案的结果对象（PRD D8：逐方向调用会烧 QPS，是明令避免的路径）。"
        "`mode` 必须与 `/api/isochrone` 一致，否则内部会走另一种交通方式的路径、与顶点耗时矛盾。"
        "`path` 为 GeoJSON LineString（坐标一律 `[经度, 纬度]`）。"
    ),
)
async def isochrone_polyline(
    payload: PolylineRequest | None = None,
    service: IsochroneService = Depends(get_isochrone_service),
) -> dict[str, Any]:
    """单个顶点的真实步行/骑行路径。"""
    body = payload or PolylineRequest()
    outcome = await service.polyline(
        body.lnglat or "",
        mode=body.mode or "walking",
        minutes=15.0 if body.minutes is None else body.minutes,  # type: ignore[arg-type]
        bearing=body.bearing,  # type: ignore[arg-type]
        distance=body.distance_m,  # type: ignore[arg-type]
        point=body.point,
        data_mode=_data_mode(body.data_mode),
        lite=bool(body.lite) if body.lite is not None else False,
    )
    return _envelope(cast(PolylineOutcome, outcome))
