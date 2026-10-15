"""全套件级的**真实出网守卫**：任何一条用例试图建立真实 TCP 连接，立刻失败。

为什么放在 conftest 而不是各测试文件里自己想办法：本仓库对「零出网」的承诺是**跨模块**的
（条款 3.2.3(ii) 禁止对百度线上施压、README §2.4 的「零配置零 AK 零出网」门禁、CI 里没有
AK）。原先这条承诺靠两样东西维持：注入 `httpx.MockTransport` 的约定，和「没人会去碰真网络」
这个假设。2026-10-15 实测证明假设不成立——`tests/test_egress_sharing.py` 的 ④ 在带系统代理
的机器上**真打到了 `api.map.baidu.com`**（httpx 的 `trust_env` 从注册表读出代理并挂进
`AsyncClient._mounts`，优先级高于注入的假传输层）。那次是修 `trust_env=False` 治的根，
这条守卫治的是**下一次换一种方式漏**。

口径钉死三件事：
  1. 钩的是 `httpx.AsyncHTTPTransport.handle_async_request`——真连接的唯一入口。
     注入的 `MockTransport`、Starlette `TestClient` 的 ASGI 传输都不经过它，所以正常用例
     一个都不受影响（实测整套在守卫下全绿）。
  2. 抛 `OSError` 而不是断言失败：让**发起方**的栈帧出现在报告里，直接读出是哪条链路、
     哪个 URL 想出去。收尾再把测试判失败，是因为「漏了但被上层吞掉」的用例同样得变红。
  3. 没有开关。真要在浏览器之外打一次真实网络，请写 `tools/` 下的独立脚本并自己交代
     配额从哪来，别往这套件里塞一个「有时候会出网」的用例——那正是这条文件要防的形态。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest


class RealEgressAttempt(OSError):
    """守卫命中时抛的错误。名字就是结论，不需要再读日志。"""


@pytest.fixture(autouse=True)
def _no_real_egress(request: pytest.FixtureRequest) -> Iterator[None]:
    """每条用例都在守卫下跑，收尾时确认它一次都没被踩到。

    `request.node` 出现在失败信息里是刻意的：漏点的表现通常是「上层把异常吞了，用例照样绿」，
    只有 teardown 这一层能把它翻成红灯。
    """
    leaked: list[str] = []

    async def guarded(_self: httpx.AsyncHTTPTransport, pending: httpx.Request) -> httpx.Response:
        leaked.append(f"{pending.method} {pending.url}")
        raise RealEgressAttempt(
            f"测试套件禁止真实出网：{pending.method} {pending.url}（条款 3.2.3(ii)）"
        )

    original = httpx.AsyncHTTPTransport.handle_async_request
    httpx.AsyncHTTPTransport.handle_async_request = guarded
    try:
        yield
    finally:
        httpx.AsyncHTTPTransport.handle_async_request = original
        if leaked:
            pytest.fail(
                f"{request.node.name} 试图真实出网 {len(leaked)} 次，首次：{leaked[0]}",
                pytrace=False,
            )
