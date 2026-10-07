"""`get_cache_store()` 必须尊重传入的 Settings（M1 bug 回归）。

回归的正是那次「静默忽略」：单例被默认配置建好后，带着自定义 `cache_db_path` 的调用方
照样写进 `data/cache.db`，导致按配置隔离缓存的契约不成立。
"""

from __future__ import annotations

from pathlib import Path

from infra.cache_store import CacheStore, get_cache_store, reset_cache_store
from infra.config import Settings


def test_second_path_gets_its_own_store(tmp_path: Path) -> None:
    """先 A 后 B：写 B 只落 B 的库，A 的行数不变。"""
    cfg_a = Settings(cache_enabled=True, cache_db_path=tmp_path / "a.db")
    cfg_b = Settings(cache_enabled=True, cache_db_path=tmp_path / "b.db")
    try:
        store_a = get_cache_store(cfg_a)
        store_b = get_cache_store(cfg_b)

        assert store_a is not store_b
        assert store_b.db_path == (tmp_path / "b.db").resolve()

        store_a.set("geocode", {"status": 0, "result": {"x": 1}}, {"address": "A"})
        store_b.set("geocode", {"status": 0, "result": {"x": 2}}, {"address": "B"})

        assert store_b.get("geocode", {"address": "B"}) is not None
        assert (tmp_path / "a.db").exists()
        assert (tmp_path / "b.db").exists()
    finally:
        reset_cache_store()


def test_same_path_returns_same_instance(tmp_path: Path) -> None:
    """同一路径必须复用同一实例，否则 `/api/cache/stats` 的计数器跨请求归零。"""
    cfg = Settings(cache_enabled=True, cache_db_path=tmp_path / "c.db")
    try:
        assert get_cache_store(cfg) is get_cache_store(cfg)
    finally:
        reset_cache_store()


def test_reset_only_touches_the_given_path(tmp_path: Path) -> None:
    cfg_a = Settings(cache_enabled=True, cache_db_path=tmp_path / "d.db")
    cfg_b = Settings(cache_enabled=True, cache_db_path=tmp_path / "e.db")
    try:
        store_a = get_cache_store(cfg_a)
        store_b = get_cache_store(cfg_b)
        assert reset_cache_store(cfg_a) is not store_a
        assert get_cache_store(cfg_b) is store_b
    finally:
        reset_cache_store()


def test_default_path_unchanged_when_disabled(tmp_path: Path) -> None:
    """`cache_enabled=False` 不得为了建表而落盘一个 .db。"""
    cfg = Settings(cache_enabled=False, cache_db_path=tmp_path / "f.db")
    store = CacheStore(cfg)
    try:
        outcome = store.get("geocode", {"address": "X"})
        assert outcome.hit is False
        assert outcome.miss_reason.value == "disabled"
        assert not (tmp_path / "f.db").exists()
    finally:
        store.close()
