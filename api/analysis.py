"""`/api/analysis`、`/api/analysis/{task_id}`、`/api/samples` 三个路由（PRD §4.7）。

本文件只做 HTTP 形状的事：把请求体交给 `services/analysis.py:run_analysis()`，
把报告对象原样序列化并补 `request_id`，异常交给 `api/app.py` 的处理器出统一 JSON。
管线编排、三栏计量、评分与摘要全在服务层，路由里不重复算任何一遍。

三段职责：

* `POST /api/analysis` —— **一键体检主接口**。默认**同步**跑完并直接返回完整报告；
  显式传 `background: true` 时立刻回 `202` + `task_id`，前端改走轮询。
* `GET /api/analysis/{task_id}` —— 轮询进度（FR-205）。同步模式跑完后任务记录**照样留存**，
  所以「先拿到报告、再按 task_id 复查阶段表与计量」这条路径是通的。
* `GET /api/samples` —— 预置样例社区（FR-102）。清单来自 `config/samples.yaml`，
  只读清单、不出网、不合成数据。

🔴 三条口径：

1. **同步是默认，不是妥协**。一次体检只走 5~8 次 HTTP 往返（PRD §2.2），本地通常秒级完成；
   为了「进度条」把默认路径改成异步，会让零配置演示平白多一个失败点与一次轮询。
   需要真进度时显式传 `background: true`。
2. **进度绝不伪造**。任务记录里的 `progress` 由 `run_analysis(progress=...)` 的**真实阶段回调**
   写入；同步请求返回时它已经是一份完整的阶段表，不存在「假装在跑」的百分比。
3. `request_id` 从 `infra.log` 的上下文变量取，不另生成（与 `api/geo.py` 同一刀）。

任务记录只存在**进程内存**里且有上限（`_TASKS_MAX`）：它是进度通道，不是结果存储。
跨进程或重启后按 `task_id` 查不到是正常行为，响应里会明确说「请重新发起体检」，
而不是回一个空壳让前端一直转圈。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from infra.config import Settings, get_settings
from infra.errors import CircleDocError
from infra.log import get_logger, get_request_id, log_fields
from pydantic import BaseModel, Field, field_validator
from services.analysis import PLATFORM_PROFILES, run_analysis
from services.poi_categories import DIMENSION_ORDER
from services.poi_search import InvalidPoiParam
from services.samples import SampleConfigError, find_sample, samples_as_dicts

from api.poi import _data_mode, reject_bool

__all__ = ["AnalysisRequest", "AnalysisTaskGone", "reset_tasks", "router"]

router = APIRouter()

logger = get_logger("circledoc.api.analysis")

_TASKS_MAX = 32
"""进程内任务记录上限。超出丢最旧的：这是进度通道，不是结果存储（见模块 docstring）。"""


class AnalysisTaskGone(CircleDocError):
    """按 `task_id` 查不到任务（服务重启 / 超出内存上限 / id 写错）。

    为什么不用裸 `HTTPException(404)`：`api/app.py` 给 `StarletteHTTPException` 的通用文案是
    「这个接口还不存在，对应的功能可能尚未开发完成」，而这里要表达的是**任务过期**，
    处置动作完全不同（重新发起体检，而不是改 URL）。继承 `CircleDocError` 就自动拿到
    全站一致的 JSON 信封与 `request_id`，路由里不必再写一份。
    """

    error_id = "analysis_task_gone"
    category = "client"
    http_status = 404
    user_message = "该体检任务的记录已不存在（服务重启或超出内存上限），请重新发起一次体检。"
    retryable = False
    degrade_to_sample = False


@dataclass
class AnalysisTask:
    """一次体检的任务记录（FR-205 的轮询对象）。"""

    task_id: str
    status: Literal["running", "done", "failed"] = "running"
    created_at: str = ""
    finished_at: str | None = None
    progress: list[dict[str, Any]] = field(default_factory=list)
    request: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def as_dict(self, *, include_report: bool) -> dict[str, Any]:
        """`GET /api/analysis/{task_id}` 的响应体。

        `partial` 的含义就是「还没跑完，继续轮询」；同步路径下能查到的记录必然已
        `done`，所以它只在 `background` 模式下有意义 —— 前端据此决定是否再轮询。
        """
        body: dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "progress": list(self.progress),
            "stages_done": len(self.progress),
            "partial": self.status == "running",
            "request": dict(self.request),
            "error": dict(self.error) if self.error else None,
        }
        if include_report and self.report is not None:
            body["report"] = self.report
        return body


_TASKS: OrderedDict[str, AnalysisTask] = OrderedDict()


def _remember(task: AnalysisTask) -> None:
    _TASKS[task.task_id] = task
    while len(_TASKS) > _TASKS_MAX:
        _TASKS.popitem(last=False)


def reset_tasks() -> None:
    """测试与演练用：清空任务记录，避免用例之间互相看见对方的 `task_id`。"""
    _TASKS.clear()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------
# 请求体
# --------------------------------------------------------------------------------------
_LNGLAT_DOC = "中心点坐标，必填，格式「经度,纬度」（BD09LL），如 116.404,39.915"
_MODES_DOC = (
    '出行方式列表，可选，缺省 `["walking"]`。传 `["walking", "riding"]` 即双模式叠加'
    "（FR-206）。⚠️ 两种模式**共用同一份 5000 条/日 路线配额**，双模式当日消耗翻倍，"
    "配额上限也从 120 放宽到 220（PRD §6.1）。"
)
_MINUTES_DOC = "目标时长（分钟），可选，默认 15，合理区间 5~60（FR-104 的档位是 5/10/15/30）。"
_CATEGORIES_DOC = (
    "要统计的维度键，可选，缺省为全七项（六维度 + 便民参考项）。"
    f"可用值：{', '.join(DIMENSION_ORDER)}。"
)
_PLATFORM_DOC = (
    "档位，可选：`desktop`（默认，前 K 实测 K=12）/ `mobile`（FR-107 轻量档：K=6、"
    "只跑 12 方向粗扫、跳过中点加密与耗时热力场）。"
)
_MODE_DOC = (
    "本次取数策略：`live` 允许实时调用、`cache` 优先本地缓存、`mock` 强制不出网（FR-105）。"
    "缺省沿用 `/api/health` 启动探测解析出的运行模式。"
)
_BACKGROUND_DOC = (
    "是否后台执行：`false`（默认）同步跑完直接返回完整报告；`true` 立刻回 202 + `task_id`，"
    "前端用 `GET /api/analysis/{task_id}` 轮询进度（FR-205）。"
)


class AnalysisRequest(BaseModel):
    """`POST /api/analysis` 的请求体（§4.7 入参 + 两个档位开关）。

    类型刻意宽松（数字可写成字符串），与 `api/isochrone.py` 同一理由：422 的通用文案
    在演示现场没有诊断价值，服务层才说得清「哪个字段为什么不行」。
    """

    lnglat: str | list[float] | dict[str, float] | None = Field(None, description=_LNGLAT_DOC)
    sample_id: str | None = Field(
        None,
        description=(
            "载入预置样例社区（FR-102）：取自 `GET /api/samples` 的 `id`。"
            "载入后自动采用该社区的中心点与**建成形态画像**（老城 / 新建大盘 / 城中村），"
            "三型的圈内计数与盲区面积有可见差异。显式传了 `lnglat` 时以 `lnglat` 为准。"
        ),
    )
    modes: list[str] | None = Field(None, description=_MODES_DOC)
    mode: str | None = Field(None, description="单模式便捷写法，等价于 `modes: [mode]`。")
    minutes: int | float | str | None = Field(None, description=_MINUTES_DOC)
    categories: list[str] | None = Field(None, description=_CATEGORIES_DOC)
    platform: str | None = Field(None, description=_PLATFORM_DOC)
    radius: int | float | str | None = Field(
        None, description="POI 检索半径（米），可选；缺省 = `R_max × 1.2`。"
    )
    data_mode: str | None = Field(None, description=_MODE_DOC)
    background: bool | None = Field(None, description=_BACKGROUND_DOC)
    include_insufficient: bool | None = Field(
        None,
        description=(
            "是否把「配套不足」（缺 1~2 类）一并列为盲区块，默认 true。"
            "关掉即只剩 FR-221 的完全盲区口径；`area_pct` **只算完全盲区**，不受本开关影响。"
        ),
    )

    # `true` 会被 pydantic 强转成 `1`（`int` 分支先匹配），于是 `minutes: true` 变成
    # 「1 分钟等时圈」并静默返回 200。这类输入没有合理语义，在字段校验阶段就挡掉。
    _no_bool_numbers = field_validator("minutes", "radius", mode="before")(reject_bool)


def get_settings_dep() -> Settings:
    """装配点：测试可覆盖 `app.dependency_overrides[get_settings_dep]`。"""
    return get_settings()


def _modes_of(body: AnalysisRequest) -> list[str]:
    """`modes` 优先，其次单数 `mode`，缺省步行。去重保序；非法值交给服务层报 400。"""
    raw: list[str] = []
    if body.modes:
        raw.extend(str(item) for item in body.modes)
    elif body.mode:
        raw.append(str(body.mode))
    out: list[str] = []
    for item in raw:
        name = item.strip().lower()
        if name and name not in out:
            out.append(name)
    return out or ["walking"]


def _minutes(value: int | float | str | None) -> float:
    """缺省 15；解析不出来时报 400（错误文案只有一处出处）。"""
    if value is None:
        return 15.0
    if isinstance(value, bool):
        raise InvalidPoiParam("目标时长不能是布尔值。", field_name="minutes", value=str(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 15.0
    try:
        return float(text)
    except ValueError as exc:
        raise InvalidPoiParam(
            f"目标时长应为分钟数，收到 {value!r}。", field_name="minutes", value=text
        ) from exc


def _platform(value: str | None) -> str:
    key = str(value or "desktop").strip().lower()
    if key not in PLATFORM_PROFILES:
        raise InvalidPoiParam(
            f"档位只支持 {'/'.join(PLATFORM_PROFILES)}，收到 {value!r}。",
            field_name="platform",
            value=value,
            allowed=list(PLATFORM_PROFILES),
        )
    return key


def _radius(value: int | float | str | None) -> int | None:
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
            f"检索半径应为数字（米），收到 {value!r}。", field_name="radius", value=text
        ) from exc


# --------------------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------------------
@router.post(
    "/analysis",
    summary="一键体检（主接口，FR-201~FR-304）",
    description=(
        "按固定顺序串起四步，**每一步都复用上一步的产物、不重复出网**："
        "① 等时圈（扇形采样 + 二分收敛）→ ② POI 检索（每维度 1 次 `$` 合并检索，"
        "`place_calls ≤ 8`）→ ③ 落圈判定 + 前 K 实测（**1 次批量算路**，K=12/6）→ "
        "④ 盲区判定（**复用 ③ 的点集，0 次出网**，D5）→ ⑤ 评分与摘要。"
        "🔴 双模式（`walking` + `riding`）时 POI **只检索一次**、骑行圈复用同一份 POI 集合，"
        "`place_calls` 不翻倍（FR-206 / D5）。"
        "响应含 `usage`（三栏分开：`place_calls` / `quota_units` / `http_requests`，"
        "`route_lines` 按 mode 分栏）与 `assertions`（与 §6.1 上限逐条对照的自证表）。"
        "报告对象即 FR-305 导出 JSON 的内容；示例数据一律为程序合成，"
        "见响应里的 `meta.source_declaration`。"
        "任何一步降级都不中断：任一层失败 → **200** + `degraded` + 可读文案（FR-403）。"
    ),
    status_code=200,
)
async def analysis(
    response: Response,
    payload: AnalysisRequest | None = None,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """跑完一次完整体检并返回报告对象。"""
    body = payload or AnalysisRequest()
    modes = _modes_of(body)
    platform = _platform(body.platform)

    # 预置样例：解析出中心点与建成形态；`lnglat` 显式给了就尊重它，但仍用样例的画像
    # （否则「选了城中村样例、却把中心点挪到老城区」会得到一份自相矛盾的合成报告）。
    lnglat: Any = body.lnglat
    archetype: str | None = None
    if body.sample_id:
        community = find_sample(body.sample_id, settings)
        archetype = community.archetype
        if not lnglat:
            lnglat = list(community.lnglat)

    task = AnalysisTask(
        task_id=uuid4().hex,
        created_at=_now(),
        request={
            "lnglat": body.lnglat,
            "sample_id": body.sample_id,
            "archetype": archetype,
            "modes": modes,
            "minutes": body.minutes,
            "platform": platform,
            "categories": list(body.categories) if body.categories else None,
            "data_mode": body.data_mode,
        },
    )
    _remember(task)

    def on_progress(item: dict[str, Any]) -> None:
        task.progress.append({"at": _now(), **item})

    kwargs: dict[str, Any] = {
        "modes": modes,
        "minutes": _minutes(body.minutes),
        "categories": list(body.categories) if body.categories else None,
        "data_mode": _data_mode(body.data_mode),
        "platform": platform,
        "radius_m": _radius(body.radius),
        "settings": settings,
        "archetype": archetype,
        "progress": on_progress,
    }
    if body.include_insufficient is not None:
        kwargs["include_insufficient"] = bool(body.include_insufficient)

    try:
        report = await run_analysis(lnglat or "", **kwargs)
    except Exception as exc:
        # 兜底必须落在路由层：任务记录不能永远停在 `running`，否则前端会一直轮询。
        # 真正的错误形状仍由 `api/app.py` 的处理器决定（这里只补任务状态后原样抛回）。
        task.status = "failed"
        task.finished_at = _now()
        task.error = {"type": type(exc).__name__, "message": str(exc)}
        log_fields(
            logger,
            logging.WARNING if isinstance(exc, CircleDocError) else logging.ERROR,
            "analysis_failed",
            task_id=task.task_id,
            error_type=type(exc).__name__,
        )
        raise

    task.status = "done"
    task.finished_at = _now()
    task.report = report

    if body.background:
        # 报告已算完、但只按 `task_id` 取；202 表示「已受理，去轮询」。
        response.status_code = 202
        return {
            "ok": True,
            "task_id": task.task_id,
            "status": task.status,
            "progress": list(task.progress),
            "poll": f"/api/analysis/{task.task_id}",
            "request_id": get_request_id(),
        }
    return {**report, "task_id": task.task_id, "request_id": get_request_id()}


@router.get(
    "/analysis/{task_id}",
    summary="体检进度轮询（FR-205）",
    description=(
        "`partial=true` 表示仍在跑，前端应继续轮询；`status=done` 时 `report` 是完整报告。"
        "任务记录只存在**进程内存**里且上限 32 条，重启或跨进程查不到属正常行为，"
        "此时返回 404 + 「请重新发起体检」的可读文案，而不是空壳让前端一直转圈。"
    ),
)
async def analysis_progress(task_id: str, include_report: bool = True) -> dict[str, Any]:
    """按 `task_id` 取任务状态（可带完整报告）。"""
    task = _TASKS.get(task_id)
    if task is None:
        raise AnalysisTaskGone(detail={"task_id": task_id, "tasks_max": _TASKS_MAX})
    body = task.as_dict(include_report=bool(include_report))
    body["request_id"] = get_request_id()
    return body


@router.get(
    "/samples",
    summary="预置样例社区列表（FR-102）",
    description=(
        "来自 `config/samples.yaml`，**只读清单、不出网、不合成数据**。"
        "每项含 `id` / `name` / `city` / `district` / `lnglat` / `form` / `archetype`，"
        "前端下拉选中后 flyTo 并填充坐标即可。"
        "🔴 **`coord_verified` 必须如实展示**：`false` 表示该坐标是区级近似占位、"
        "尚未用 `/api/suggest` 或点图核验（PRD §10-1 规程②明令坐标不得凭记忆写死）。"
        "⚠️ 样例中心点必须在国内：境外逆地理 / 地点检索 / 路线规划对个人档均为「-」，"
        "越界项已在读入时剔除并把原因写进 `notes`。"
        "同城三种建成形态（老城 / 新建大盘 / 城中村）的合成画像在计数与盲区面积上有可见差异。"
    ),
)
async def samples() -> dict[str, Any]:
    """预置样例社区清单 + 校验问题（问题如实下发，不隐藏）。"""
    try:
        items, problems = samples_as_dicts()
    except SampleConfigError as exc:
        # 清单不可用是**配置缺陷**，不能伪装成「空列表」：FR-102 是 P0 要求，
        # 静默返回空会让现场看起来像「功能没做」，而日志里什么都没有。
        return {
            "ok": False,
            "samples": [],
            "count": 0,
            "coordinate_system": "bd09ll",
            "notes": [str(exc)],
            "request_id": get_request_id(),
        }
    return {
        "ok": True,
        "samples": items,
        "count": len(items),
        "coordinate_system": "bd09ll",
        "notes": problems,
        "request_id": get_request_id(),
    }
