"""结构化 JSON 日志 + request_id 上下文。

为什么自己写而不是 `logging.basicConfig`：PRD §5「可观测」要求日志里能直接读到
请求路径、耗时、API 计量与降级原因——这些是每条请求的上下文，不是某个 logger 的参数。
把 request_id 放进 `ContextVar`，业务代码调用 `logger.info(...)` 时就能自动带上它，
不需要一路透传参数。

为什么内置脱敏：百度 Web 服务的 AK 是 URL query 参数（`?ak=...`）。一旦有人图省事
把请求 URL 或 httpx 的 debug 日志打出来，服务端 AK 就进了日志、也就进了镜像与 CI 产物
（PRD §5 明令禁止）。所以在 formatter 层面统一擦掉，而不是指望每个人都记得。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REQUEST_ID_HEADER = "X-Request-Id"
_NO_REQUEST_ID = "-"

_request_id_var: ContextVar[str] = ContextVar("circledoc_request_id", default=_NO_REQUEST_ID)

# 只擦值、保留键名，便于排查「是哪一个参数出了问题」而又不泄露内容。
_SECRET_QUERY_RE = re.compile(
    r"(?i)(?P<key>[?&]?(?:ak|sn|secret_key|baidu_server_ak|baidu_browser_ak))[=](?P<val>[^&\s\"']+)"
)
_SECRET_JSON_RE = re.compile(
    r"(?i)(?P<key>\"(?:ak|sn|secret_key|baidu_server_ak|baidu_browser_ak)\"\s*:\s*\")"
    r"(?P<val>[^\"]*)(?P<q>\")"
)

_REDACTED = "***REDACTED***"


def new_request_id() -> str:
    """生成一个短 request_id（16 位十六进制，够在日志里唯一，又不会撑爆终端）。"""
    return uuid.uuid4().hex[:16]


def get_request_id() -> str:
    """当前请求的 request_id；不在请求上下文中时为 ``"-"``。"""
    return _request_id_var.get()


@contextmanager
def request_id_context(value: str | None = None) -> Iterator[str]:
    """在 with 块内给所有日志绑定同一个 request_id。

    返回生成的 id，是因为异常处理器也要把它回写给响应头，不能自己再猜一次。
    """
    rid = value or new_request_id()
    token = _request_id_var.set(rid)
    try:
        yield rid
    finally:
        _request_id_var.reset(token)


def mask_secrets(text: str) -> str:
    """把 URL query 与 JSON 里的密钥字段值替换成占位符。"""
    masked = _SECRET_QUERY_RE.sub(lambda m: f"{m.group('key')}={_REDACTED}", text)
    return _SECRET_JSON_RE.sub(lambda m: f"{m.group('key')}{_REDACTED}{m.group('q')}", masked)


class RequestIdFilter(logging.Filter):
    """把 contextvar 里的 request_id 注入到每条记录，供 formatter 取用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象，字段固定，便于 CI / `jq` 直接筛。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_secrets(record.getMessage()),
            "request_id": getattr(record, "request_id", _NO_REQUEST_ID),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                # 保留字段名不许被业务字段覆盖，否则 `message` 会被悄悄顶掉。
                if key not in payload:
                    payload[key] = value
        if record.exc_info:
            payload["traceback"] = mask_secrets(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = mask_secrets(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """本机调试用的单行文本格式（`LOG_JSON=false` 时启用）。"""

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", _NO_REQUEST_ID)
        base = f"{record.levelname[0]} {record.created:,.0f} [{rid}] {record.name}: "
        extra = getattr(record, "fields", None)
        tail = f" {extra}" if isinstance(extra, dict) and extra else ""
        return mask_secrets(f"{base}{record.getMessage()}{tail}")


def configure_logging(
    level: str | int = "INFO",
    *,
    json_format: bool = True,
    force: bool = True,
) -> None:
    """初始化根 logger；重复调用安全（uvicorn reload 与 pytest 都会反复进来）。

    `force=True` 是必需的：uvicorn 启动时会给 root 挂自己的 handler，不摘掉就会出现
    同一条日志两种格式、grep 不到的情况。
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if json_format else TextFormatter())

    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            if existing is not handler:
                root.removeHandler(existing)
    root.addHandler(handler)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level)

    # uvicorn 的三个 logger 默认 propagate=False，不接管就会一直用它的纯文本格式。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """取模块级 logger。传 `__name__` 即可，不要自己 new。"""
    return logging.getLogger(name)


def log_fields(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """带结构化字段的日志入口：`log_fields(log, logging.INFO, "done", place_calls=6)`。

    为什么不直接用 `logger.info(msg, extra=...)`：`extra` 的键会直接铺到 LogRecord 上，
    和 `message` / `args` 这类内置属性撞名时就静默丢字段；统一收进 `fields` 字典就没这个
    坑，也让 JSON 里的业务字段和信封字段分层清楚。
    """
    logger.log(level, message, extra={"fields": fields})
