"""FastAPI 应用工厂：中间件、错误响应、路由集中注册。

分工：这里只做「HTTP 形状」的事——给每个请求绑 request_id 与计量累加器、把
`infra.errors` 的语义翻译成用户可读的 JSON、把入参校验错误收敛成同一种错误形状。
任何业务判断都不许写进来（PRD §5「算法层为纯函数、外部依赖可注入 mock」）。

🔴 关于 HTTP 状态码：百度返回的 `status` 与 HTTP 状态码是两套东西，重试与熔断只看前者
（笔记 §3.2，官方码表里没有 429）。本文件的 HTTP 码只承担 REST 语义——429 表示「你该退避」、
503 表示「今天没额度且已熔断到次日零时」、502 表示「Key/服务配置错」——处置动作一律来自
`ErrorSpec`，任何地方都不许拿 HTTP 码反推百度那边发生了什么。

零配置约定：不填 `.env`、两个 AK 都是空串时，`create_app()` 必须成功、
`uvicorn api.app:app` 必须起得来且不抛 traceback。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from infra.config import Settings, get_settings
from infra.errors import BreakerKind, CircleDocError, seconds_until_next_midnight
from infra.log import (
    REQUEST_ID_HEADER,
    configure_logging,
    get_logger,
    get_request_id,
    log_fields,
    new_request_id,
    request_id_context,
)
from infra.metrics import Metrics, get_process_metrics, metrics_scope
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from api.analysis import router as analysis_router
from api.cache_stats import router as cache_router
from api.geo import router as geo_router
from api.health import router as health_router
from api.isochrone import router as isochrone_router
from api.poi import router as poi_router

logger = get_logger("circledoc.api")

# --------------------------------------------------------------------------------------
# 路由集中注册表
# --------------------------------------------------------------------------------------
# 后续任务各自把 router 加进这个列表，一个模块一条，顺序即 OpenAPI 里的分组顺序。例如：
#
#     from api.routes_health import router as health_router
#
#     ROUTERS: list[tuple[APIRouter, dict[str, Any]]] = [
#         (health_router, {"prefix": "/api", "tags": ["health"]}),
#     ]
#
# 刻意写死列表、不用 importlib 或扫目录自动发现：路由注册属于「装配」，出事故时人要能
# 一眼看出到底挂了哪几个；自动发现会让「health 没注册」只在演示现场才暴露。
ROUTERS: list[tuple[APIRouter, dict[str, Any]]] = [
    (health_router, {"prefix": "/api", "tags": ["health"]}),  # /api/health + 启动探测
    (cache_router, {"prefix": "/api", "tags": ["cache"]}),  # /api/cache/stats
    (geo_router, {"prefix": "/api", "tags": ["geo"]}),  # /api/geocode /regeo /suggest
    (isochrone_router, {"prefix": "/api", "tags": ["isochrone"]}),  # /api/isochrone
    (poi_router, {"prefix": "/api", "tags": ["poi"]}),  # M3：/api/poi
    (analysis_router, {"prefix": "/api", "tags": ["analysis"]}),  # M4：主接口 + samples
]


def error_payload(
    *,
    code: str,
    category: str,
    message: str,
    retryable: bool = False,
    degrade_to_sample: bool = True,
    breaker: str = BreakerKind.NONE.value,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """所有错误响应共用的信封：`{"error": {...}, "request_id": "..."}`。

    `request_id` 放在顶层而不是 error 里，方便前端在错误卡片上直接给出可复制的排查号。
    """
    return {
        "error": {
            "code": code,
            "category": category,
            "message": message,
            "retryable": retryable,
            "degrade_to_sample": degrade_to_sample,
            "breaker": breaker,
            "detail": detail or {},
        },
        "request_id": get_request_id(),
    }


def error_headers(exc: CircleDocError) -> dict[str, str]:
    """按 `ErrorSpec` 的熔断语义给出 `Retry-After`。

    为什么配额类要给「到次日零时」而不是 30 s：`status=4` / `302` 当天不可恢复
    （FAQ 2361 / 2366），告诉客户端半秒钟后再试只会让它更快把配额烧干净。
    """
    if exc.breaker is BreakerKind.QUOTA:
        return {"Retry-After": str(seconds_until_next_midnight())}
    if exc.breaker is BreakerKind.CONCURRENCY:
        return {"Retry-After": "1"}
    return {}


def register_exception_handlers(app: FastAPI) -> None:
    """把各类异常统一映射成可读 JSON，并且**永不外泄 traceback 与 AK**。"""

    @app.exception_handler(CircleDocError)
    async def handle_circledoc_error(request: Request, exc: CircleDocError) -> JSONResponse:
        payload = exc.to_payload()
        level = logging.INFO if exc.retryable else logging.WARNING
        log_fields(
            logger,
            level,
            "upstream_request_failed",
            path=request.url.path,
            code=payload["code"],
            retryable=payload["retryable"],
            detail=payload["detail"],
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": payload, "request_id": exc.request_id or get_request_id()},
            headers=error_headers(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 只回「哪个字段不对」，不回反射出来的完整请求体——里面通常有用户地址这类隐私。
        fields = [str(item.get("loc", "")) for item in exc.errors()]
        log_fields(
            logger, logging.WARNING, "request_rejected", path=request.url.path, fields=fields
        )
        return JSONResponse(
            status_code=422,
            content=error_payload(
                code="invalid_request",
                category="client",
                message="提交的参数不完整或有误，请检查后重新提交。",
                retryable=False,
                degrade_to_sample=False,
                detail={"fields": fields},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code >= 500:
            message = "服务暂时无法完成这个请求，已按离线模式保留可用的部分结果。"
        else:
            message = "这个接口还不存在，对应的功能可能尚未开发完成。"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code=f"http_{exc.status_code}",
                category="http",
                message=message,
                degrade_to_sample=exc.status_code >= 500,
                detail={"reason": str(exc.detail)},
            ),
            headers=dict(getattr(exc, "headers", None) or {}),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底：任何没归类到 `CircleDocError` 的异常都不能变成 500 白屏（PRD §5 可用性）。"""
        log_fields(
            logger,
            logging.ERROR,
            "unhandled_error",
            path=request.url.path,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                code="internal_error",
                category="internal",
                message="服务内部异常，本次结果可能不完整，请稍后重试。",
                detail={"request_id": get_request_id()},
            ),
        )


def register_middleware(app: FastAPI) -> None:
    """请求计时 + request_id + 三栏计量绑定。

    关于顺序：Starlette 的 `ExceptionMiddleware` 在本中间件的**内侧**，所以已经被处理器
    转成 JSON 的错误响应照样会走到这里计时与记账——出错不会丢 usage；只有没被接住的异常
    才需要在下面自己兜一层。
    """

    @app.middleware("http")
    async def request_context_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        process_metrics: Metrics = app.state.metrics
        started = perf_counter()

        with request_id_context(rid), metrics_scope(f"request:{request.url.path}") as usage:
            response: Response
            try:
                response = await call_next(request)
                status_code = response.status_code
            except CircleDocError as exc:
                status_code = exc.http_status
                response = JSONResponse(
                    status_code=status_code,
                    content={"error": exc.to_payload(), "request_id": rid},
                    headers=error_headers(exc),
                )
            except Exception as exc:
                status_code = 500
                log_fields(
                    logger,
                    logging.ERROR,
                    "unhandled_error",
                    path=request.url.path,
                    error_type=type(exc).__name__,
                )
                response = JSONResponse(
                    status_code=status_code,
                    content=error_payload(
                        code="internal_error",
                        category="internal",
                        message="服务内部异常，本次结果可能不完整，请稍后重试。",
                        detail={"request_id": rid},
                    ),
                )

            elapsed_ms = int((perf_counter() - started) * 1000)
            process_metrics.merge(usage)
            _log_completion(request, status_code, elapsed_ms, usage)

            response.headers[REQUEST_ID_HEADER] = rid
            response.headers["X-Elapsed-Ms"] = str(elapsed_ms)
            # 三栏分开回显：`curl -i` 就能核 §6.1 的三条断言，不必先解析整个响应体。
            response.headers["X-Place-Calls"] = str(usage.place_calls)
            response.headers["X-Quota-Units"] = str(usage.quota_units)
            response.headers["X-Http-Requests"] = str(usage.http_requests)
            return response


def _log_completion(request: Request, status_code: int, elapsed_ms: int, usage: Metrics) -> None:
    """一行请求日志：路径、状态、耗时、三栏计量（PRD §5「可观测」）。"""
    snapshot = usage.snapshot()
    level = (
        logging.ERROR
        if status_code >= 500
        else logging.WARNING if status_code >= 400 else logging.INFO
    )
    log_fields(
        logger,
        level,
        "http_request",
        method=request.method,
        path=request.url.path,
        status=status_code,
        elapsed_ms=elapsed_ms,
        place_calls=snapshot["place_calls"],
        quota_units=snapshot["quota_units"],
        http_requests=snapshot["http_requests"],
        degraded_count=snapshot["degraded_count"],
    )


@asynccontextmanager
async def report_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时把「现在到底在什么模式下跑」写进日志。

    为什么值得单独一行：现场最难查的不是报错，而是「明明配了 AK 却一直出 sample」——
    答案通常卡在 `data_mode` 还是 `auto`、启动探测根本没跑。模式和配置修正一起打出来，
    看日志就能定位。真实探测由 `/api/health`（W3）负责，这里只报当前已解析的值。
    """
    settings: Settings = app.state.settings
    log_fields(
        logger,
        logging.INFO,
        "startup",
        app=settings.app_name,
        env=settings.app_env,
        data_mode=settings.data_mode,
        data_mode_in_effect=settings.data_mode_in_effect,
        has_server_ak=settings.has_server_ak,
        has_browser_ak=settings.has_browser_ak,
        routes_registered=len(ROUTERS),
        config_notes=settings.config_notes,
    )
    if not settings.has_server_ak:
        log_fields(
            logger,
            logging.WARNING,
            "server_ak_missing",
            hint="未配置 BAIDU_SERVER_AK：按零配置路径运行，实时调用会降级到合成数据。",
        )
    yield


def register_routers(app: FastAPI) -> None:
    """按 `ROUTERS` 列表集中挂载路由。

    列表里没挂上的业务路由在装配前返回 404 JSON 而不是 500 —— 零配置状态依然合法。
    """
    for router, kwargs in ROUTERS:
        app.include_router(router, **kwargs)


def register_static(app: FastAPI, settings: Settings) -> None:
    """挂 `web/` 静态目录，让 `/` 有首页（PRD §6.2「不填 `.env` 也能起首页」）。

    为什么在 `register_routers` 之后：`Mount("/")` 会吃掉所有路径，注册顺序决定优先级，
    先挂 API 才不会让 `/api/health` 被静态目录判成 404。

    目录不存在时**跳过而不是抛异常**：镜像里 `web/` 由 `Dockerfile` COPY 进来，本机
    `uvicorn` 直接在仓库根跑时目录可能被挪走——为一个首页把整个服务起不来是最坏现场。
    """
    if not settings.web_dir.is_dir():
        log_fields(
            logger,
            logging.WARNING,
            "static_dir_missing",
            web_dir=str(settings.web_dir),
            hint="未找到 web/ 目录，首页不挂载；API 路由不受影响。",
        )
        return
    # `html=True`：`/` 直接返回 `index.html`，省掉一条我们自己写的重定向路由。
    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。传 `settings` 让测试注入自己的配置，而不碰真实环境变量。"""
    cfg = settings or get_settings()
    configure_logging(cfg.log_level, json_format=cfg.log_json)

    app = FastAPI(
        title="CircleDoc API",
        version="0.1.0",
        summary="15 分钟生活圈智能体检与规划助手 · 后端接口",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=report_lifespan,
    )
    app.state.settings = cfg
    # 进程级累加器与 `infra.metrics` 的模块级默认值必须是同一个对象，否则脱离请求上下文的
    # 记账（启动自检、后台任务）会落到另一份账本上，`/api/cache/stats` 就永远对不上。
    app.state.metrics = get_process_metrics()

    register_exception_handlers(app)
    register_middleware(app)
    register_routers(app)
    register_static(app, cfg)
    return app


# `uvicorn api.app:app` 的目标；create_app() 里没有任何会因缺 AK 而失败的分支。
app = create_app()
