"""`GET /api/cache/stats` —— 把 `CacheStore.stats()` 原样端出去，给 QA 一条能断言的路。

PRD §6.1 / FR-402 的验收口径是「同一参数**二次运行**命中率 ≥ 95 % 且 `place_calls = 0`」。
这句话里没有一条能靠看日志证明：命中率在 SQLite 与进程计数器里，`place_calls` 在三栏账本里，
探测开销又必须与它们分开。所以本模块只做一个**只读汇总**：

- `stats` —— `CacheStore.stats().as_dict()` 的全部字段（`hits / misses / lookups / hit_rate /
  by_service / entries / expired_entries / counters_scope`），不改名、不加过滤。
- `assertion` —— 把「≥ 95 %」这条阈值写在响应里，并给出 `meets_target`；`lookups = 0` 时返回
  `None` 而不是 `False`（没查过 ≠ 查得差），与 `CacheStats.hit_rate` 的较真口径一致。
- `metrics` —— 进程级三栏账（`place_calls / quota_units / http_requests / cache_hits …`），
  QA 在这里核 `place_calls = 0`。
- `probe` —— 启动探测的**独立**账本（`api.health.probe_usage_totals()`）。

🔴 为什么这里绝不记命中：`CacheStore(mirror_metrics=True)` 已经把每一次命中镜像进了
`infra.metrics` 的进程账（`cache_hits` / `cache_misses`）。本模块再 `add_cache_hit()` 一次就
是**翻倍**，那条「二次运行 place_calls = 0」会被一个统计接口自己弄脏。所以这个文件只调
`stats()`（纯读），不调任何计数方法。

🔴 为什么探测账要单列：`/api/health` 每 30 s 被 `Dockerfile` 的 HEALTHCHECK 打一次，它发的
请求若混进同一本账，「二次运行 place_calls = 0」会因无关的健康检查而必然失败。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from infra.cache_store import get_cache_store
from infra.config import Settings, expected_data_source, get_settings
from infra.log import get_logger, log_fields
from infra.metrics import get_process_metrics

from api.health import probe_usage_totals

logger = get_logger("circledoc.api.cache_stats")

router = APIRouter()

__all__ = ["build_cache_stats", "get_process_metrics_snapshot", "router"]

# PRD §6.1 的验收阈值：写在这里是为了让响应能自证「达标/没达标」，不用 QA 再抄一遍数字。
TARGET_HIT_RATE = 0.95


def build_cache_stats(settings: Settings) -> dict[str, Any]:
    """组装响应体：缓存快照 + 阈值判定 + 进程三栏账 + 探测独立账。

    为什么单独一个函数而不是全写在路由里：`tools/` 的冒烟脚本要能在不经过 HTTP 的情况下拿到
    同一份数据（起 uvicorn 只是为了验端口，断言不该依赖它）。
    """
    mode = settings.data_mode_in_effect
    store = get_cache_store(settings)
    stats = store.stats().as_dict()

    lookups = int(stats.get("lookups") or 0)
    hit_rate = stats.get("hit_rate")
    metrics = get_process_metrics_snapshot()

    return {
        "ok": True,
        "checked_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        # ---- 现在到底在什么模式下取数（模式由 /api/health 的探测决定） ----------------
        "data_mode": mode,
        "data_source": expected_data_source(mode),
        "cache_enabled": bool(stats.get("enabled")),
        # FR-402 的「缓存」层只在 `cache` / `live` 模式下参与取数；`mock` 模式根本不查缓存，
        # 这时 lookups=0 是**预期**而不是故障，故把模式一起给出，免得看板误判。
        "counters_scope": stats.get("counters_scope", "process"),
        "stats": stats,
        "assertion": {
            "target_hit_rate": TARGET_HIT_RATE,
            "lookups": lookups,
            "hit_rate": hit_rate,
            # None = 一次都没查过（零配置首轮、或 mock 模式），不能报成「未达标」。
            "meets_target": None if not lookups else float(hit_rate or 0.0) >= TARGET_HIT_RATE,
            "hint": (
                "同一进程内用完全相同的参数跑两次分析：第二次应当几乎全是命中。"
                "首轮请只看 lookups>0，别把 hit_rate=null 读成缓存坏了。"
            ),
        },
        # ---- 三栏账：`place_calls = 0` 这条断言核这里 ---------------------------------
        "metrics": metrics,
        # ---- 探测的开销单独一列，从进程账里剔除后再看 place_calls ----------------------
        "probe": {
            **probe_usage_totals(),
            "note": (
                '启动探测用自己的 Metrics(label="health_probe")，不进进程账也不写缓存；'
                "这里列出是为了让「今天被健康检查烧掉几次」可核对。"
            ),
        },
        "policy": {
            "db_path": str(settings.cache_db_path),
            "ttl_s": {
                "geocode": settings.cache_ttl_geocode_s,
                "regeo": settings.cache_ttl_regeo_s,
                "place": settings.cache_ttl_place_s,
                "route": settings.cache_ttl_route_s,
                "isochrone": settings.cache_ttl_isochrone_s,
                # 错误响应恒为 0：`Settings` 的校验会把任何正数改回 0 并记一条 config_note，
                # 回显出来就不会有人以为「缓存了失败响应」。
                "error": settings.cache_error_ttl_s,
            },
            "compliance_note": (
                "POI 类缓存受官方条款约束（响应不得存储、只允许运行时性能缓存），"
                "place 的 TTL 有 24 h 硬上限，超出会被夹紧——见 config.notes。"
            ),
        },
        "config": settings.public_dict(),
        "endpoints": {
            "health": "/api/health",
            "cache_stats": "/api/cache/stats",
            "docs": "/api/docs",
            "home": "/",
        },
    }


def get_process_metrics_snapshot() -> dict[str, Any]:
    """进程级三栏账快照（单独一个函数：冒烟脚本要拿到与响应里同一个对象）。"""
    return get_process_metrics().snapshot()


@router.get("/cache/stats", summary="缓存命中统计（命中率 / 按服务条目 / 三栏账 / 探测开销）")
async def cache_stats(request: Request) -> dict[str, Any]:
    """`GET /api/cache/stats`。🔴 只读：不计数、不写库、不清库。

    永远返回 200（与 `/api/health` 同一纪律）：统计接口变红会让 QA 把「读不到统计」误报成
    「缓存坏了」，而 `CacheStore` 的设计目标恰恰是**db 打不开也照样返回可用结果**。
    """
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        payload = build_cache_stats(settings)
    except Exception as exc:  # pragma: no cover - SQLite 被外部删库之类
        log_fields(
            logger,
            logging.WARNING,
            "cache_stats_failed",
            error_type=type(exc).__name__,
        )
        return {
            "ok": True,
            "checked_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "data_mode": settings.data_mode_in_effect,
            "cache_enabled": False,
            "stats": {},
            "assertion": {
                "target_hit_rate": TARGET_HIT_RATE,
                "lookups": 0,
                "hit_rate": None,
                "meets_target": None,
                "hint": f"读取统计失败：{type(exc).__name__}",
            },
            "metrics": get_process_metrics_snapshot(),
            "probe": probe_usage_totals(),
            "error": type(exc).__name__,
        }
    log_fields(
        logger,
        logging.INFO,
        "cache_stats",
        lookups=payload["assertion"]["lookups"],
        hit_rate=payload["assertion"]["hit_rate"],
        entries=payload["stats"].get("entries"),
    )
    return payload
