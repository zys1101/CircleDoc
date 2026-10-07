"""测试共用小工具（不含 `test_` 前缀，pytest 不会收集本文件）。"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any


def async_test(coro: Callable[..., Any]) -> Callable[..., Any]:
    """在同步用例里跑一个协程。

    为什么不用 `pytest-asyncio`：本项目的依赖清单刻意钉得很紧（`requirements.txt` 的理由是
    「镜像里装出同一套库」），为几个协程测试多引一个插件不划算；`asyncio.run` 足够，
    而且每个用例拿到一个干净的事件循环，不会有跨用例的循环状态泄漏。
    """

    @functools.wraps(coro)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(coro(*args, **kwargs))

    return wrapper
