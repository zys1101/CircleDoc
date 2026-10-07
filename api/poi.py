"""`/api/poi` 路由（PRD §4.7 / FR-210~FR-214）。

本文件只做 HTTP 形状的事：把请求体交给 `services/poi_search.py`，把它的 plain-dict 结果
原样序列化并补 `request_id`，异常交给 `api/app.py` 的处理器出统一 JSON。类别词表、预算守卫、
三层降级、`$` 合并检索全在服务层，路由里不重复判一遍。

与 `api/geo.py` / `api/isochrone.py` 保持同一套口径（FR-404「所有接口的错误形状逐字段一致」）：

* 入参边界不在路由里判：`mode` 白名单 / 坐标格式 / 半径范围 / 未知类别键 → 服务层抛
  `InvalidPoiParam`（400 + 指名字段），境外坐标 → `OutsideServiceArea`（400）。
  请求体字段一律声明为可选且宽松类型，理由与 `api/isochrone.py` 逐字相同：FastAPI 的
  必填校验出的是 422 通用文案，而现场最需要知道的是**哪个字段为什么不行**。
* 检索为空 / 预算截停 / 配额耗尽 → **200** + `degraded` + 可读 `message`，不返 5xx（FR-403）。
* `request_id` 从 `infra.log` 的上下文变量取，不另生成，否则响应体里的号和日志里的号对不上。

🔴 本接口是**全线最稀缺资源**的消耗者：地点检索个人档只有 100 次/日，且与 `/api/suggest`
**共用同一配额池**（笔记 FAQ 2540 / 2549）。所以「一个维度只用一次请求」不是优化建议而是
硬约束（`services/poi_categories.py` 的 `PLACE_CALLS_PER_ANALYSIS = 8` 是它的出网预算上限）。
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from infra.config import DataMode
from infra.log import get_request_id
from pydantic import BaseModel, Field, field_validator
from services.poi_categories import DIMENSION_ORDER
from services.poi_search import InvalidPoiParam, PoiSearchService
from services.poi_search import get_poi_search_service as default_poi_service

__all__ = ["PoiRequest", "reject_bool", "router"]

router = APIRouter()


def reject_bool(value: Any) -> Any:
    """把布尔值从数值字段里挡掉，**必须在 pydantic 强转之前**（`mode="before"` 校验器）。

    🔴 为什么这是个真问题而不是洁癖：pydantic 的宽松模式在 `int | float | str | None` 上
    会让 `true` 命中 `int` 分支并强转成 `1`。于是 `{"radius": true}` 会变成一个
    **1 米的检索半径**，一路跑到 `/place/v2/search` 然后**静默返回 200** ——
    调用方拿到一份看起来正常的报告，而它其实只搜了脚下一平方米。
    这类输入没有任何合理语义，只能拒。
    """
    if isinstance(value, bool):
        raise ValueError("布尔值不是合法的数值入参；请传数字或数字字符串。")
    return value


_DATA_MODES = ("live", "cache", "mock")

_MODE_DOC = (
    "本次取数策略：`live` 允许实时调用、`cache` 优先本地缓存、`mock` 强制不出网（FR-105）。"
    "缺省沿用 `/api/health` 启动探测解析出的运行模式。"
)
_LNGLAT_DOC = "中心点坐标，必填，格式「经度,纬度」（BD09LL），如 116.404,39.915"
_MODE_VALUE_DOC = "出行方式：`walking`（默认）/ `riding`。骑行半径更大（4800 m vs 1500 m）。"
_RADIUS_DOC = (
    "检索半径（米），可选。缺省 = `R_max × 1.2`（步行 1500 m / 骑行 4800 m）；"
    "显式传值只会**收紧**并被夹到官方 10 km 上限内（FAQ 2548）。"
)
_CATEGORIES_DOC = (
    "维度键多选，可选，缺省为全七项（六维度 + 便民参考项）。"
    f"可用值：{', '.join(DIMENSION_ORDER)}。"
)


class PoiRequest(BaseModel):
    """`POST /api/poi` 的请求体（§4.7 入参 `lnglat, radius, categories[]`）。"""

    lnglat: str | list[float] | dict[str, float] | None = Field(None, description=_LNGLAT_DOC)
    radius: int | float | str | None = Field(None, description=_RADIUS_DOC)
    categories: list[str] | None = Field(None, description=_CATEGORIES_DOC)
    mode: str | None = Field(None, description=_MODE_VALUE_DOC)
    data_mode: str | None = Field(None, description=_MODE_DOC)

    _no_bool_radius = field_validator("radius", mode="before")(reject_bool)


def get_poi_service(request: Request) -> PoiSearchService:
    """依赖注入点：`app.state.poi_service` 优先，其次进程级单例。

    与 `api/geo.py:38` / `api/isochrone.py:96` 同一套口子：自测要注入
    `BaiduClient(..., transport=httpx.MockTransport(...))` 才能做到 0 次真实出网
    （条款 3.2.3(ii) 禁止对线上服务施压）。用鸭子类型而不是 `isinstance`，
    让 QA 可以塞自己写的假对象。
    """
    injected = getattr(request.app.state, "poi_service", None)
    if injected is not None:
        return cast(PoiSearchService, injected)
    return default_poi_service()


def _data_mode(value: str | None) -> DataMode | None:
    """`data_mode` 的枚举归一：只认 `live|cache|mock`，其余点名报错而不是静默回落。"""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().lower()
    if raw not in _DATA_MODES:
        raise InvalidPoiParam(
            f"取数策略只支持 {'/'.join(_DATA_MODES)}，收到 {value!r}。",
            field_name="data_mode",
            value=value,
            allowed=list(_DATA_MODES),
        )
    return cast(DataMode, raw)


def _radius(value: int | float | str | None) -> int | None:
    """把宽松类型的半径收成 `int | None`。

    `None` / 空串 → `None`（走服务层默认半径）；能解析成数字就用数字，剩下的正负与上界
    一律交给服务层的 `_require_radius()` —— 那里才知道「按哪种出行方式折算」。
    解析不出来时**不在这里报错**，把原值交给服务层，让错误文案只有一处出处。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidPoiParam("检索半径不能是布尔值。", field_name="radius", value=str(value))
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError as exc:
        raise InvalidPoiParam(
            f"检索半径应为数字（米），收到 {value!r}。",
            field_name="radius",
            value=str(value),
        ) from exc


@router.post(
    "/poi",
    summary="按 6 大民生维度检索 POI（FR-210 / FR-211 / FR-214）",
    description=(
        "内部 `/place/v2/search`（圆形区域检索）。固定携带 `radius_limit=true`、"
        "`coord_type=3`（**数字**，笔记 §6.2 的三种写法之一）、`filter=sort_name:distance`、"
        "`page_size=10`、`page_num=1`。**不使用多边形区域检索**——它需商用授权，个人档报 `status=9`"
        "（PRD §8.1 D10）。"
        "🔴 **一个维度只用一次请求**：`query` 用 `$` 做多关键字并集（最多 10 个），"
        "把 v1.0 的「6 类 × 2~4 别名 = 12~24 次」压进个人档 **100 次/日**；"
        "单次分析 `place_calls ≤ 8` 是硬上限，超限即停并转缓存/示例层。"
        "⚠️ 类别词一律取**百度官方 POI 分类名**（`购物 > 市场` 而非命题原文的「菜市场」）；"
        "词表在 `services/poi_categories.py`，构造时自检，混入官方分类表不存在的词会直接建不起来。"
        "⚠️ 官方 FAQ 2536 明确检索接口会过滤掉部分数据，因此计数可能被**系统性低估**，"
        "这是官方预期行为、不是本接口缺陷；`0` 应读作「检索未见」而非「绝对没有」。"
        "⚠️ `page_size` 官方三处自相矛盾（10~20 / 最多 10 / 最多 10），本接口按**保守值 10** 设计"
        "且**一律不翻页**（翻一次页就吃掉当日大半配额）。"
    ),
)
async def poi(
    payload: PoiRequest | None = None,
    service: PoiSearchService = Depends(get_poi_service),
) -> dict[str, Any]:
    """一次拿到六个民生维度 + 便民参考项的 POI 集合。"""
    body = payload or PoiRequest()
    result = await service.search(
        body.lnglat or "",
        radius_m=_radius(body.radius),
        mode=body.mode or "walking",
        categories=body.categories,
        data_mode=_data_mode(body.data_mode),
    )
    return {**result, "request_id": get_request_id()}
