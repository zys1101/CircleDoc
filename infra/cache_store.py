"""响应级 SQLite 缓存：定位为**同进程运行时性能缓存**，不是数据资产库（FR-402）。

改动这个文件前先读 `docs/BAIDU_API_NOTES.md` §7.2 条款原文与 §7.3 的逐条判定。合规红线
（是合规判定，不是代码风格）：

1. 🔴 表只有一张 `cache(key, service, payload, created_at, expires_at)`，按**响应报文**
   建模（key → payload → expires_at）。**禁止**新增 `pois(id, name, category, lng, lat)`
   这类可按字段查询的 POI 维度表——条款 3.3.4 明文禁止「生成或用于数据库」。索引只允许
   建在缓存自身的管理字段（`service` / `expires_at`）上，不得把 POI 字段抽成列。
2. 🔴 POI 类 TTL ≤ 24 h（`Settings.cache_ttl_place_s`，见 `infra.cache_keys.ttl_for_service`），
   且**过期行物理删除**：`cleanup()` 走 `DELETE`，全库没有软删标记位。
3. 🔴 镜像内不得预置任何 `.db`：本模块只在运行时**惰性**建库——`cache_enabled=False`
   时连文件都不创建，任何读路径也不会为了「顺手建表」而落盘。
4. 缓存**不是对外数据源**（条款 3.3.3）：本模块只给 `get/set/delete/cleanup/stats/purge/close`，
   不提供「列出全部 POI」「按类别导出」这类遍历接口；`/api/cache/stats` 只出计数、不出内容。

另两条工程约定：

- **永不抛异常**。缓存层挂了就当未命中（`miss_reason=ERROR`），继续走实时 / 合成层，
  对应 FR-403「任一层失败不中断流程」。
- **绝不累加 `place_calls`**。QA 的验收口径是「二次运行同一样例命中率 ≥ 95 % **且**
  `place_calls = 0`」，命中路径只要碰过一次 `add_place_calls` 这条断言就永远过不去。
  本模块只镜像 `cache_hits` / `cache_misses`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from infra.cache_keys import (
    cache_key,
    hard_max_ttl_for_service,
    is_cacheable_payload,
    ttl_for_service,
)
from infra.config import DataSource, Settings, get_settings
from infra.log import get_logger, log_fields
from infra.metrics import current_metrics, get_process_metrics

logger = get_logger(__name__)

__all__ = [
    "CacheResult",
    "CacheStats",
    "CacheStore",
    "MissReason",
    "close_cache_store",
    "get_cache_store",
    "reset_cache_store",
]

# 条款 3.3.4 的收敛形态：五列、一行一次响应，没有任何可查询的业务字段。
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS cache (
        key TEXT PRIMARY KEY,
        service TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )
    """,
    # 下面两个索引服务的是缓存自己的管理字段（清理节拍与分组统计），不是业务维度。
    "CREATE INDEX IF NOT EXISTS cache_expires_at_idx ON cache(expires_at)",
    "CREATE INDEX IF NOT EXISTS cache_service_idx ON cache(service)",
)

_L1_MAX_ENTRIES = 256
"""内存层容量。PRD §4.6 的降级链最外圈就是 `内存 LRU → SQLite 缓存 → 实时调用 → 合成`。"""

_L1_MAX_PAYLOAD_CHARS = 256 * 1024
"""超过这个长度的响应体不进内存层（批量算路矩阵能到几百 KB，留着会挤掉真正的高频小条目）。"""

_MAX_ENTRIES = 5000
"""全库行数上界：运行时性能缓存必须有界，无界会长成事实上的数据库（3.3.4）。"""

_EVICT_ROWS = 200
_CLEANUP_EVERY_WRITES = 32
_BUSY_TIMEOUT_MS = 2000

_MISS = object()

_INSERT_SQL = (
    "INSERT OR REPLACE INTO cache (key, service, payload, created_at, expires_at)"
    " VALUES (?, ?, ?, ?, ?)"
)
_SELECT_SQL = "SELECT payload, created_at, expires_at FROM cache WHERE key = ?"
_DELETE_KEY_SQL = "DELETE FROM cache WHERE key = ?"
_DELETE_EXPIRED_SQL = "DELETE FROM cache WHERE expires_at <= ?"
_DELETE_SERVICE_SQL = "DELETE FROM cache WHERE service = ?"
_EVICT_SQL = (
    "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires_at ASC LIMIT ?)"
)

_STATS_SQL = (
    "SELECT service, COUNT(*), SUM(LENGTH(payload)), MIN(expires_at), MAX(expires_at),"
    " SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END)"
    " FROM cache GROUP BY service"
)


class MissReason(StrEnum):
    """未命中原因：让上层区分「没存过」「存过但过期了」「缓存本身不可用」。"""

    DISABLED = "disabled"
    ABSENT = "absent"
    EXPIRED = "expired"
    ERROR = "error"


def _iso(value: int | None) -> str | None:
    """epoch 秒 → ISO 字符串，让 `/api/cache/stats` 的过期时间可读。"""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CacheResult:
    """一次 `get` 的结论：命中标记 + payload + 为什么没命中。"""

    hit: bool
    key: str
    service: str
    payload: Any = None
    created_at: int | None = None
    expires_at: int | None = None
    miss_reason: MissReason | None = None
    layer: str | None = None
    """命中来自哪一层：`memory`（L1）或 `sqlite`（L2）。"""

    @property
    def data_source(self) -> DataSource | None:
        """命中即 `cache`（FR-306 徽标值）；未命中返回 ``None``，由上层继续降级。"""
        return "cache" if self.hit else None

    def as_dict(self) -> dict[str, Any]:
        """日志 / 响应里可安全输出的字段：**不含 payload**（3.3.3：缓存不作对外数据源）。"""
        return {
            "hit": self.hit,
            "key": self.key,
            "service": self.service,
            "layer": self.layer,
            "data_source": self.data_source,
            "miss_reason": self.miss_reason.value if self.miss_reason else None,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "expires_at_iso": _iso(self.expires_at),
        }


@dataclass(frozen=True)
class ServiceStat:
    """单个服务的缓存条目（来自 SQLite）与命中情况（来自进程内计数器）。"""

    service: str
    entries: int = 0
    payload_chars: int = 0
    expired_entries: int = 0
    ttl_s: int = 0
    earliest_expires_at: int | None = None
    latest_expires_at: int | None = None
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float | None:
        lookups = self.hits + self.misses
        return round(self.hits / lookups, 4) if lookups else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "payload_chars": self.payload_chars,
            "expired_entries": self.expired_entries,
            "ttl_s": self.ttl_s,
            "earliest_expires_at": self.earliest_expires_at,
            "latest_expires_at": self.latest_expires_at,
            "earliest_expires_at_iso": _iso(self.earliest_expires_at),
            "latest_expires_at_iso": _iso(self.latest_expires_at),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
        }


@dataclass(frozen=True)
class CacheStats:
    """/api/cache/stats 的数据源（PRD §4.7 出参 `{hits, misses, by_service{}}`）。"""

    enabled: bool
    db_path: str
    entries: int = 0
    hits: int = 0
    misses: int = 0
    skipped: int = 0
    expired_entries: int = 0
    payload_chars: int = 0
    earliest_expires_at: int | None = None
    latest_expires_at: int | None = None
    by_service: dict[str, ServiceStat] = field(default_factory=dict)
    last_cleanup_deleted: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        """命中率；**一次查询都没做过就返回 ``None`` 而不是 0.0**。

        为什么较真：`0.0` 会被读成「缓存彻底没用过」，而零配置首轮本来就没查过。
        验收要求的 ≥ 95 % 只在 `lookups > 0` 时有意义，QA 断言前先看 `lookups`。
        """
        return round(self.hits / self.lookups, 4) if self.lookups else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "db_path": self.db_path,
            "hits": self.hits,
            "misses": self.misses,
            "lookups": self.lookups,
            "hit_rate": self.hit_rate,
            "skipped": self.skipped,
            "entries": self.entries,
            "expired_entries": self.expired_entries,
            "payload_chars": self.payload_chars,
            "earliest_expires_at": self.earliest_expires_at,
            "latest_expires_at": self.latest_expires_at,
            "earliest_expires_at_iso": _iso(self.earliest_expires_at),
            "latest_expires_at_iso": _iso(self.latest_expires_at),
            "by_service": {name: stat.as_dict() for name, stat in sorted(self.by_service.items())},
            # 计数器作用域必须写明：命中统计是**进程内**的（与「同进程运行时性能缓存」一致），
            # 进程重启后条目还在、计数从零开始。
            "counters_scope": "process",
            "last_cleanup_deleted": self.last_cleanup_deleted,
        }


class CacheStore:
    """响应级缓存的读写入口：`get / set / delete / cleanup / stats / purge / close`。

    并发模型：按线程各持一条 SQLite 连接（同步路由在线程池、异步路由在事件循环，两边都会
    进来），写入用一把锁串行化，跨进程冲突交给 `busy_timeout`。异步包装用
    `asyncio.to_thread` 把 SQLite 挪出事件循环——一次矩阵写入加 WAL fsync 卡住的会是整个
    请求循环，不是单个请求。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db_path: Path | str | None = None,
        enabled: bool | None = None,
        clock: Callable[[], float] | None = None,
        mirror_metrics: bool = True,
    ) -> None:
        """默认值全部来自 `Settings`；`db_path` 与 `clock` 是给测试留的注入点。

        `clock` 为什么必须有：POI TTL 是 24 h，过期行为不可能靠真等来验证。传一个可推进的
        假时钟就能复现「写入 → 过期 → `cleanup()` 物理删行」这条合规要求。
        """
        self._settings = settings or get_settings()
        self._db_path = Path(db_path) if db_path is not None else Path(self._settings.cache_db_path)
        self._enabled = self._settings.cache_enabled if enabled is None else bool(enabled)
        self._clock = clock or time.time
        self._mirror_metrics = mirror_metrics

        self._connections: dict[int, sqlite3.Connection] = {}
        self._conn_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._counter_lock = threading.Lock()
        self._l1: OrderedDict[str, tuple[int, int, Any]] = OrderedDict()
        self._l1_lock = threading.Lock()

        self._schema_ready = False
        self._broken = False
        self._closed = False
        self._writes_since_cleanup = 0
        self._last_cleanup_deleted = 0
        self._hits = 0
        self._misses = 0
        self._skipped = 0
        self._hits_by_service: dict[str, int] = {}
        self._misses_by_service: dict[str, int] = {}

    # ------------------------------------------------------------------ 基本信息
    @property
    def enabled(self) -> bool:
        """可用性总判断：开关打开、库能开、连接没被 `close()` 关掉。"""
        return self._enabled and not self._broken and not self._closed

    @property
    def db_path(self) -> Path:
        """当前使用的 db 路径（来自 `Settings.cache_db_path`，默认 `data/cache.db`）。"""
        return self._db_path

    def key(self, service: str, params: Mapping[str, Any] | None = None) -> str:
        """某次查询的缓存键（口径统一在 `infra.cache_keys.cache_key`）。"""
        return cache_key(service, params)

    def ttl_for(self, service: str) -> int:
        """某服务实际生效的 TTL 秒数（配置值再夹一道合规上限）。"""
        return ttl_for_service(service, self._settings)

    def is_cacheable(self, payload: Any) -> bool:
        """该响应体能不能进缓存：非成功报文与空响应一律拒收（错误响应不缓存）。"""
        return is_cacheable_payload(payload)

    # ------------------------------------------------------------------ 读
    def get(
        self,
        service: str,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> CacheResult:
        """取一次响应缓存：返回 `payload` + 命中标记，上层据此决定要不要发实时请求。

        命中路径**不碰 `place_calls`**；只有真发 HTTP 才由 `BaiduClient` 记出网账。
        读到过期行时顺手把它 `DELETE` 掉——「TTL 到期即物理删除」不能等定时任务。
        """
        name = _canonical_service(service)
        entry_key = key or self.key(service, params)
        if not self.enabled:
            # 关掉或库不可用：不计进命中率分母，否则一次配置切换会把 hit_rate 压成 0。
            self._bump_skipped()
            reason = MissReason.ERROR if (self._broken or self._closed) else MissReason.DISABLED
            return CacheResult(hit=False, key=entry_key, service=name, miss_reason=reason)

        now = self._now()
        memory = self._l1_get(entry_key, now)
        if memory is not _MISS:
            created_at, expires_at, payload = memory
            self._record(name, hit=True)
            return CacheResult(
                hit=True,
                key=entry_key,
                service=name,
                payload=payload,
                created_at=created_at,
                expires_at=expires_at,
                layer="memory",
            )

        if not self._has_db_file():
            # 库文件都还没写过 = 从没缓存过：直接算未命中。
            # 🔴 顺序很重要——必须在 `_connect()` 之前判断，否则 SQLite 打开就会顺手建一个
            # 空 .db，「只为查一下缓存」也会往 `data/` 落文件，与「镜像不预置 .db」对冲。
            self._record(name, hit=False)
            return CacheResult(
                hit=False, key=entry_key, service=name, miss_reason=MissReason.ABSENT
            )

        conn = self._connect()
        if conn is None:
            self._bump_skipped()
            return CacheResult(hit=False, key=entry_key, service=name, miss_reason=MissReason.ERROR)

        try:
            row = conn.execute(_SELECT_SQL, (entry_key,)).fetchone()
        except sqlite3.Error as exc:
            self._log_problem("cache_get_failed", exc, service=name)
            self._bump_skipped()
            return CacheResult(hit=False, key=entry_key, service=name, miss_reason=MissReason.ERROR)

        if row is None:
            self._record(name, hit=False)
            return CacheResult(
                hit=False, key=entry_key, service=name, miss_reason=MissReason.ABSENT
            )

        text, created_at, expires_at = str(row[0]), int(row[1]), int(row[2])
        if expires_at <= now:
            with self._write_lock:
                removed = self._execute_write(conn, _DELETE_KEY_SQL, (entry_key,))
            log_fields(
                logger,
                logging.DEBUG,
                "cache_expired_row_removed",
                service=name,
                key_short=entry_key[:12],
                deleted=removed or 0,
            )
            self._record(name, hit=False)
            return CacheResult(
                hit=False,
                key=entry_key,
                service=name,
                created_at=created_at,
                expires_at=expires_at,
                miss_reason=MissReason.EXPIRED,
            )

        payload = _decode_payload(text)
        if len(text) <= _L1_MAX_PAYLOAD_CHARS:
            self._l1_put(entry_key, created_at, expires_at, payload)
        self._record(name, hit=True)
        return CacheResult(
            hit=True,
            key=entry_key,
            service=name,
            payload=payload,
            created_at=created_at,
            expires_at=expires_at,
            layer="sqlite",
        )

    async def get_async(
        self,
        service: str,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> CacheResult:
        """`get` 的异步外壳。"""
        return await asyncio.to_thread(self.get, service, params, key=key)

    def get_payload(
        self,
        service: str,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
        default: Any = None,
    ) -> Any:
        """只要 payload 的简写；未命中给 `default`。需要区分未命中原因时请用 `get`。"""
        result = self.get(service, params, key=key)
        return result.payload if result.hit else default

    # ------------------------------------------------------------------ 写
    def set(
        self,
        service: str,
        payload: Any,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
        ttl_s: int | None = None,
        cacheable: bool | None = None,
    ) -> bool:
        """写一条响应；返回是否真的落库（关闭、拒收、出错都返回 ``False``）。

        TTL 按服务取（POI 类上限 24 h）；显式传 `ttl_s` 也会被 `hard_max_ttl_for_service`
        再夹一次——调用方没法用这个参数绕过合规上限。
        """
        name = _canonical_service(service)
        if not self.enabled:
            return False

        entry_key = key or self.key(service, params)
        allowed = is_cacheable_payload(payload) if cacheable is None else bool(cacheable)
        if not allowed:
            return False

        requested = self.ttl_for(service) if ttl_s is None else int(ttl_s)
        ttl = max(0, min(requested, hard_max_ttl_for_service(name)))
        if ttl == 0:
            return False

        text = _encode_payload(payload)
        if text is None:
            log_fields(
                logger,
                logging.WARNING,
                "cache_payload_unencodable",
                service=name,
                key_short=entry_key[:12],
            )
            return False

        conn = self._connect()
        if conn is None:
            return False

        now = self._now()
        expires_at = now + ttl
        with self._write_lock:
            written = self._execute_write(
                conn, _INSERT_SQL, (entry_key, name, text, now, expires_at)
            )
            if written is None:
                return False
            self._writes_since_cleanup += 1
            self._maintain(conn, now)

        if len(text) <= _L1_MAX_PAYLOAD_CHARS:
            self._l1_put(entry_key, now, expires_at, payload)
        return True

    async def set_async(
        self,
        service: str,
        payload: Any,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
        ttl_s: int | None = None,
        cacheable: bool | None = None,
    ) -> bool:
        """`set` 的异步外壳。"""
        return await asyncio.to_thread(
            self.set, service, payload, params, key=key, ttl_s=ttl_s, cacheable=cacheable
        )

    # ------------------------------------------------------------------ 删与清理
    def delete(
        self,
        service: str | None = None,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> int:
        """删除条目，返回被删行数（物理删除）。

        粒度按 `key` > `service+params` > `service` 依次生效：只给 `service` 时删掉该服务的
        全部条目——配额熔断当天想让某类结果立刻失效时用。
        """
        if not self._has_db_file():
            return 0
        conn = self._connect()
        if conn is None:
            return 0

        if key is not None or params is not None:
            entry_key = key or self.key(service or "", params)
            self._l1_discard(entry_key)
            with self._write_lock:
                return self._execute_write(conn, _DELETE_KEY_SQL, (entry_key,)) or 0

        if service is None:
            return 0
        self._l1_clear()
        with self._write_lock:
            return (
                self._execute_write(conn, _DELETE_SERVICE_SQL, (_canonical_service(service),)) or 0
            )

    async def delete_async(
        self,
        service: str | None = None,
        params: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> int:
        """`delete` 的异步外壳。"""
        return await asyncio.to_thread(self.delete, service, params, key=key)

    def cleanup(self, *, now: int | None = None) -> int:
        """**物理删除**所有过期行，返回删除行数（不是打标记）。

        四个调用时机：首次建库后、每 `_CLEANUP_EVERY_WRITES` 次写入、`get` 命中过期行时
        单行删、以及 `close()` 之前。QA 要验收「过期行真的没了」看的就是这里。
        """
        if not self._has_db_file():
            return 0
        conn = self._connect()
        if conn is None:
            return 0
        moment = self._now() if now is None else int(now)
        with self._write_lock:
            deleted = self._delete_expired(conn, moment)
        if deleted:
            log_fields(
                logger,
                logging.INFO,
                "cache_cleanup",
                deleted=deleted,
                db_path=str(self._db_path),
            )
        return deleted

    async def cleanup_async(self, *, now: int | None = None) -> int:
        """`cleanup` 的异步外壳（lifespan 关闭钩子里用）。"""
        return await asyncio.to_thread(self.cleanup, now=now)

    def purge(self) -> int:
        """清空整库（含未过期条目），返回删除行数。

        两个用途：FR-405 的「串行 direction vs 批量算路」对比要求**清空缓存后各跑一次**；
        以及交付 / 打包前把本地跑出来的缓存倒干净，避免误把 `.db` 带进镜像（FR-402）。
        """
        if not self._has_db_file():
            return 0
        conn = self._connect()
        if conn is None:
            return 0
        self._l1_clear()
        with self._write_lock:
            return self._execute_write(conn, "DELETE FROM cache", ()) or 0

    async def purge_async(self) -> int:
        """`purge` 的异步外壳。"""
        return await asyncio.to_thread(self.purge)

    # ------------------------------------------------------------------ 统计
    def stats(self) -> CacheStats:
        """给 `/api/cache/stats` 的完整快照：命中率、按服务条目数、最早/最晚过期时间。

        命中与未命中是**进程内计数器**（每次 `get` 都记，含 L1 命中），所以「二次运行同一
        样例命中率 ≥ 95 %」在同一进程里必然可算；条目数与过期时间来自 SQLite，重启后仍真实。
        """
        with self._counter_lock:
            hits, misses, skipped = self._hits, self._misses, self._skipped
            service_hits = dict(self._hits_by_service)
            service_misses = dict(self._misses_by_service)

        now = self._now()
        stats_rows: list[tuple[str, int, int, int | None, int | None, int]] = []
        conn = self._connect() if self._has_db_file() else None
        if conn is not None:
            try:
                stats_rows = [
                    (
                        str(row[0]),
                        int(row[1]),
                        int(row[2] or 0),
                        None if row[3] is None else int(row[3]),
                        None if row[4] is None else int(row[4]),
                        int(row[5] or 0),
                    )
                    for row in conn.execute(_STATS_SQL, (now,)).fetchall()
                ]
            except sqlite3.Error as exc:
                self._log_problem("cache_stats_failed", exc)

        by_service: dict[str, ServiceStat] = {}
        for name, entries, chars, earliest, latest, expired in stats_rows:
            by_service[name] = ServiceStat(
                service=name,
                entries=entries,
                payload_chars=chars,
                expired_entries=expired,
                ttl_s=self.ttl_for(name),
                earliest_expires_at=earliest,
                latest_expires_at=latest,
                hits=service_hits.get(name, 0),
                misses=service_misses.get(name, 0),
            )
        # 只查询过、条目已被清掉的服务也要出现，否则「place 命中率 100 %」会连同分母一起消失。
        for name in set(service_hits) | set(service_misses):
            by_service.setdefault(
                name,
                ServiceStat(
                    service=name,
                    ttl_s=self.ttl_for(name),
                    hits=service_hits.get(name, 0),
                    misses=service_misses.get(name, 0),
                ),
            )

        return CacheStats(
            enabled=self._enabled,
            db_path=str(self._db_path),
            entries=sum(stat.entries for stat in by_service.values()),
            hits=hits,
            misses=misses,
            skipped=skipped,
            expired_entries=sum(stat.expired_entries for stat in by_service.values()),
            payload_chars=sum(stat.payload_chars for stat in by_service.values()),
            earliest_expires_at=_min_or_none(
                stat.earliest_expires_at for stat in by_service.values()
            ),
            latest_expires_at=_max_or_none(stat.latest_expires_at for stat in by_service.values()),
            by_service=by_service,
            last_cleanup_deleted=self._last_cleanup_deleted,
        )

    async def stats_async(self) -> CacheStats:
        """`stats` 的异步外壳。"""
        return await asyncio.to_thread(self.stats)

    def reset_counters(self) -> None:
        """清零命中计数器（不动 db 条目）。给「预热前 / 预热后分段统计」用。"""
        with self._counter_lock:
            self._hits = 0
            self._misses = 0
            self._skipped = 0
            self._hits_by_service.clear()
            self._misses_by_service.clear()

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """先尽力清一次过期行，再关掉所有线程的连接；之后读写都安全地退化为未命中。"""
        if self._closed:
            return
        if not self._broken:
            try:
                self.cleanup()
            except Exception as exc:  # pragma: no cover - 关闭路径不能抛
                self._log_problem("cache_cleanup_on_close_failed", exc)
        self._closed = True
        with self._conn_lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for conn in connections:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._l1_clear()
        self._schema_ready = False

    async def close_async(self) -> None:
        """`close` 的异步外壳。"""
        await asyncio.to_thread(self.close)

    # ------------------------------------------------------------------ 内部
    def _now(self) -> int:
        return int(self._clock())

    def _has_db_file(self) -> bool:
        """db 文件是否已经存在（读与清理路径用它避免「只为查一下就落一个空 .db」）。"""
        return self._db_path.exists()

    def _open_connection(self) -> sqlite3.Connection:
        """建父目录并开连接；db 文件不存在时由 SQLite 惰性创建（零配置可用）。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` 是给 `close()` 用的：连接按线程登记，退出时由调用
        # `close()` 的那个线程（通常是 lifespan 所在协程）统一回收。若不放开，跨线程
        # close 会抛 ProgrammingError 被吞掉，线程池里的工作线程连接就一直占着 db 文件——
        # Windows 上表现为文件删不掉。每条连接仍然只被它所属线程读写，写入另有 `_write_lock`，
        # 真被并发用到时 SQLite 自身是序列化模式，最坏抛 ProgrammingError 被 `get` 接住当未命中。
        conn = sqlite3.connect(
            str(self._db_path), timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False
        )
        try:
            # WAL：读不挡写（事件循环线程与工作线程会同时在跑）。synchronous=NORMAL 是
            # 性能缓存的合理折中——极端情况丢几条缓存，不影响结果正确性。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        except sqlite3.Error:
            pass  # 个别文件系统不支持 WAL：退回默认 journal 模式，功能不受影响
        return conn

    def _connect(self) -> sqlite3.Connection | None:
        """取当前线程的连接；开不了库就标记「不可用」并返回 ``None``（不抛异常）。"""
        if self._closed or self._broken or not self._enabled:
            return None
        ident = threading.get_ident()
        with self._conn_lock:
            conn = self._connections.get(ident)
            if conn is None:
                try:
                    conn = self._open_connection()
                except (sqlite3.Error, OSError) as exc:
                    self._broken = True
                    self._log_problem("cache_db_unavailable", exc, db_path=str(self._db_path))
                    return None
                self._connections[ident] = conn
        if not self._schema_ready:
            self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """建表建索引，并在首次就绪时清一次过期行（笔记 §7.3 整改建议 2）。"""
        with self._write_lock:
            if self._schema_ready:
                return
            try:
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.commit()
            except sqlite3.Error as exc:
                self._broken = True
                self._log_problem("cache_schema_init_failed", exc, db_path=str(self._db_path))
                return
            self._schema_ready = True
        try:
            self.cleanup()
        except Exception as exc:  # pragma: no cover - 清理失败不影响可用性
            self._log_problem("cache_startup_cleanup_failed", exc)

    def _execute_write(
        self, conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]
    ) -> int | None:
        """执行一条写语句并提交；失败返回 ``None`` 并记日志，**绝不向上抛**。"""
        try:
            cursor = conn.execute(sql, args)
            conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            self._log_problem("cache_write_failed", exc, sql=sql.split(" ", 2)[0])
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            return None

    def _maintain(self, conn: sqlite3.Connection, now: int) -> None:
        """写入节拍上的维护：周期性清理过期行，超过行数上界按最早过期淘汰。

        为什么要淘汰：这是「运行时性能缓存」，无界增长会让它变成事实上的 POI 库，正是
        条款 3.3.4 的禁止形态。`_MAX_ENTRIES` 保证库里只留得下一小段时间窗口的响应。
        调用方已持 `_write_lock`。
        """
        if self._writes_since_cleanup >= _CLEANUP_EVERY_WRITES:
            self._delete_expired(conn, now)
        self._evict_capacity(conn)

    def _delete_expired(self, conn: sqlite3.Connection, moment: int) -> int:
        """物理删除所有已过期行。调用方已持 `_write_lock`。"""
        deleted = self._execute_write(conn, _DELETE_EXPIRED_SQL, (moment,)) or 0
        self._last_cleanup_deleted = deleted
        self._writes_since_cleanup = 0
        self._l1_prune(moment)
        if deleted:
            log_fields(
                logger, logging.INFO, "cache_cleanup", deleted=deleted, db_path=str(self._db_path)
            )
        return deleted

    def _evict_capacity(self, conn: sqlite3.Connection) -> int:
        """行数超界时按 `expires_at` 升序淘汰一批（先扔最快过期的，留下的最有价值）。"""
        total = self._count_entries(conn)
        if total is None or total <= _MAX_ENTRIES:
            return 0
        batch = min(max(1, total - _MAX_ENTRIES), _EVICT_ROWS)
        removed = self._execute_write(conn, _EVICT_SQL, (batch,)) or 0
        if removed:
            log_fields(
                logger,
                logging.INFO,
                "cache_capacity_evicted",
                rows=removed,
                entries=total,
                max_entries=_MAX_ENTRIES,
            )
        return removed

    def _count_entries(self, conn: sqlite3.Connection) -> int | None:
        """当前条目数；读失败返回 ``None``，调用方据此跳过淘汰（不能抛给写路径）。"""
        try:
            row = conn.execute("SELECT COUNT(*) FROM cache").fetchone()
        except sqlite3.Error as exc:
            self._log_problem("cache_count_failed", exc)
            return None
        return int(row[0]) if row else None

    def _record(self, service: str, *, hit: bool) -> None:
        """记一次真实查询；命中率的分母只由这里贡献。"""
        with self._counter_lock:
            if hit:
                self._hits += 1
                self._hits_by_service[service] = self._hits_by_service.get(service, 0) + 1
            else:
                self._misses += 1
                self._misses_by_service[service] = self._misses_by_service.get(service, 0) + 1
        self._mirror_to_metrics(hit)

    def _bump_skipped(self) -> None:
        """缓存不可用时的计数：单独一栏，不进命中率分母。"""
        with self._counter_lock:
            self._skipped += 1

    def _mirror_to_metrics(self, hit: bool) -> None:
        """把命中情况并进 `infra.metrics` 的 `cache_hits` / `cache_misses` 两栏。

        只镜像这两栏：`place_calls` 由 `BaiduClient` 在**真发 HTTP** 时记。上层因此**不必**
        自己为缓存查询调 `add_cache_hit`，否则同一件事会被记两次。
        """
        if not self._mirror_metrics:
            return
        recorder = current_metrics() or get_process_metrics()
        if hit:
            recorder.add_cache_hit()
        else:
            recorder.add_cache_miss()

    def _log_problem(self, message: str, exc: BaseException, **fields: Any) -> None:
        """缓存层的失败只到 WARNING：它是降级链上的一环，不是故障（FR-403）。"""
        log_fields(
            logger,
            logging.WARNING,
            message,
            error_type=type(exc).__name__,
            error=str(exc)[:200],
            **fields,
        )

    # 内存层（L1）
    def _l1_get(self, key: str, now: int) -> Any:
        with self._l1_lock:
            item = self._l1.get(key)
            if item is None:
                return _MISS
            created_at, expires_at, payload = item
            if expires_at <= now:
                self._l1.pop(key, None)
                return _MISS
            self._l1.move_to_end(key)
            return (created_at, expires_at, payload)

    def _l1_put(self, key: str, created_at: int, expires_at: int, payload: Any) -> None:
        with self._l1_lock:
            self._l1[key] = (created_at, expires_at, payload)
            self._l1.move_to_end(key)
            while len(self._l1) > _L1_MAX_ENTRIES:
                self._l1.popitem(last=False)

    def _l1_discard(self, key: str) -> None:
        with self._l1_lock:
            self._l1.pop(key, None)

    def _l1_clear(self) -> None:
        with self._l1_lock:
            self._l1.clear()

    def _l1_prune(self, now: int) -> None:
        with self._l1_lock:
            stale = [key for key, (_, expires_at, _) in self._l1.items() if expires_at <= now]
            for key in stale:
                self._l1.pop(key, None)


def _canonical_service(service: str) -> str:
    return (service or "").strip().lower() or "unknown"


def _encode_payload(payload: Any) -> str | None:
    """响应体 → 文本。`sort_keys` 让同一份数据两次写入得到同一文本，便于 diff 与统计。"""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def _decode_payload(text: str) -> Any:
    """文本 → 对象；不是 JSON 就原样返回（百度少数接口返回纯文本）。"""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _min_or_none(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _max_or_none(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


# --------------------------------------------------------------------------------------
# 进程级单例
# --------------------------------------------------------------------------------------
# 命中计数器必须挂在一个跨请求存活的对象上：每请求新建 store 的话，`/api/cache/stats`
# 永远读到 0 命中，FR-402 的验收就无从计算。
#
# 为什么按 `cache_db_path` 分键而不是只留一个：单例如果固定成「第一次传入的那份配置」，
# 后面带着自定义 `cache_db_path` 的调用方会被**静默忽略**，写进默认库——测试与真实缓存
# 串味、且没有任何告警（M1 冒烟就是这么被坑到两条假 FAIL 的）。按路径分键既保留跨请求
# 的计数器，又让「换库」这件事真的生效；同一进程里出现第二个路径会告警，因为那通常意味着
# 某处配置写岔了，而不是有人真的想并行用两个库。
_store_lock = threading.Lock()
_stores: dict[str, CacheStore] = {}


def _store_key(settings: Settings | None) -> str:
    cfg = settings or get_settings()
    return str(Path(cfg.cache_db_path).resolve())


def get_cache_store(settings: Settings | None = None) -> CacheStore:
    """进程级 `CacheStore`；`app.state.cache` 与 `/api/cache/stats` 都该用它。

    同一 `cache_db_path` 返回同一个实例（计数器因此跨请求存活）；不同路径返回各自实例。
    """
    key = _store_key(settings)
    with _store_lock:
        store = _stores.get(key)
        if store is None:
            if _stores:
                log_fields(
                    logger,
                    logging.WARNING,
                    "cache_store_extra_path",
                    db_path=key,
                    existing=list(_stores),
                    hint="同一进程出现第二个缓存库路径，请确认是否某处配置写岔了。",
                )
            store = _stores[key] = CacheStore(settings, db_path=Path(key))
        return store


def reset_cache_store(settings: Settings | None = None) -> CacheStore:
    """关掉并丢弃单例后重建（测试换 `CACHE_DB_PATH`、或 `reload_settings()` 之后调用）。

    不传 `settings` 重置全部；传了就只重置它对应的那条路径。
    """
    key = _store_key(settings) if settings is not None else None
    with _store_lock:
        keys = list(_stores) if key is None else [key]
        previous = [_stores.pop(k, None) for k in keys]
    for store in previous:
        if store is not None:
            store.close()
    if key is None:
        return get_cache_store()
    return get_cache_store(settings)


def close_cache_store() -> None:
    """lifespan 关闭钩子：顺带清掉过期行再释放连接。"""
    with _store_lock:
        stores = list(_stores.values())
        _stores.clear()
    for store in stores:
        store.close()
