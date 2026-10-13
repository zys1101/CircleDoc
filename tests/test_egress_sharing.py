"""出网资源必须全进程只有**一份**：连接池、在途闸、令牌桶、熔断器。

回归的是 2026-10-13 用户在百度账号控制台实拍到的「并发 12」。`.env` 写的是
`BAIDU_MAX_CONCURRENCY=4`，可进程里 `poi_search` / `geo` / `isochrone` 三个服务单例各建一个
`BaiduClient`、各带一套桶与闸，最坏 `3 × 4 = 12` 条在途，`RATE_*_QPS` 同样被乘三——百度按
AK 统计 QPS 与在途并发，它不认我们进程里有几个 client。修法是 `infra/baidu_client.py` 的
进程级共享出口 `Egress`。这四件事钉住它：

① 三个默认服务的客户端**共用一份**出口（对象同一性，不是数值相等）；
② 注入 `transport` / `http_client` / 假时钟 / 假 `base_url` 的客户端仍走**私有**出口——
   测试隔离与 `/api/health` 探测不能被牵连（假时钟进真实桶会把补令牌算术打乱）；
③ 熔断是**账号级**的：一个客户端攒够连败，另一个客户端的请求就该被拦；
④ 跨三个客户端并发 12 个请求，在途峰值 ≤ `BAIDU_MAX_CONCURRENCY`（**这一条在修之前必红**）。

全程假传输层，一次真实出网都没有（条款 3.2.3(ii)）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from infra.baidu_client import BaiduClient, Egress, egress_snapshot, get_egress, reset_egress
from infra.cache_store import reset_cache_store
from infra.config import Settings
from infra.ratelimit import RateLimitRegistry
from services.geo import GeoService
from services.isochrone_service import IsochroneService
from services.poi_search import PoiSearchService

FAKE_AK = "fake-ak-for-mock-transport-only"
MAX_CONCURRENCY = 3
"""本模块钉死的并发上限。共享出口的连接池形状由它决定（`_egress_fingerprint`）。"""


def _settings(**extra: object) -> Settings:
    """构造一份「只用于假传输层」的 Settings：显式压掉所有会真的等很久的限速。

    限速给到 500 QPS 是为了让**信号量**成为唯一的约束——否则 ④ 测到的是桶不是闸。
    熔断阈值给 2 是为了 ③ 不必攒 5 次。显式传入的值优先于 `.env`（pydantic-settings 的
    init kwargs 压过环境变量），所以本机 `.env` 里配了真实 AK 也不会污染这些用例。
    """
    values: dict[str, object] = {
        "baidu_server_ak": FAKE_AK,
        "baidu_browser_ak": "",
        "data_mode": "live",
        "cache_enabled": False,
        "suggest_enabled": False,
        "baidu_max_concurrency": MAX_CONCURRENCY,
        "rate_place_qps": 500.0,
        "burst_place": 500,
        "rate_regeo_qps": 500.0,
        "burst_regeo": 500,
        "rate_route_qps": 500.0,
        "burst_route": 500,
        "rate_geocode_qps": 500.0,
        "burst_geocode": 500,
        "concurrency_breaker_threshold": 2,
        "retry_max_attempts": 1,
    }
    values.update(extra)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def shared_egress() -> Iterator[Egress]:
    """先丢弃别的模块建好的出口，再用本模块的限速档建一份。

    `reset_egress()` 只丢引用不关连接池（它是同步函数，await 关池得把整条构造链变协程），
    收尾同样只丢引用——测试里的假传输层没有真连接要还。
    """
    reset_egress()
    yield get_egress(_settings())
    reset_egress()


async def _instant_sleep(_seconds: float) -> None:
    """注入用的假 `sleep`：立刻返回。和 `clock` 一起出现就说明「这活在假时钟世界」。"""


class _CountingTransport(httpx.AsyncBaseTransport):
    """数「同时在途」的假传输层：每次睡 20 ms，好让 12 个并发真的叠在闸口上。"""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self.total = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.live += 1
        self.total += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(
                200, json={"status": 0, "result": {"location": {"lng": 116.4, "lat": 39.9}}}
            )
        finally:
            self.live -= 1


def test_three_default_services_share_one_egress(tmp_path: Path) -> None:
    """①：三个服务单例形状的客户端必须落在同一份出口上。

    走的是各 service 的**默认**构造（不注 client），也就是生产 `app.state` 缺失时路由真正
    回落的那条路（`api/poi.py:82`、`api/geo.py:38`）。
    """
    cfg = _settings(cache_db_path=tmp_path / "egress-share.db")
    try:
        poi = PoiSearchService(cfg)
        geo = GeoService(cfg)
        iso = IsochroneService(cfg)
        egresses = {id(poi.client.egress), id(geo.client.egress), id(iso.client.egress)}
        assert len(egresses) == 1, "三个服务又各建一套出网资源了：桶与闸会被乘三"
        assert poi.client.rate_limiter is geo.client.rate_limiter is iso.client.rate_limiter
        assert poi.client.breakers is geo.client.breakers is iso.client.breakers
        assert poi.client.egress.shared is True
        # `mode_count` 仍是各 client 自己的属性：合并的是账号级资源，不是把两次分析捏成一个预算。
        assert iso.client.mode_count == 2 and geo.client.mode_count == 1
        assert egress_snapshot()["bindings"] >= 3
    finally:
        reset_cache_store()


def test_injected_clients_stay_on_private_egress(shared_egress: Egress) -> None:
    """②：任何注入都退回私有出口，且**不占用**共享出口的绑定计数。

    这条是「测试隔离不能被修没」的证明：`httpx.MockTransport` 客户端若和真实链路共用一份桶，
    单测的 FakeClock 会把真实桶的补令牌算术打乱，`/api/health` 探测的假时钟世界也一样。
    """
    cfg = _settings()
    before = egress_snapshot()["bindings"]
    mock = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": 0}))
    by_transport = BaiduClient(cfg, transport=mock)
    http_in = httpx.AsyncClient(transport=mock)
    by_http_client = BaiduClient(cfg, http_client=http_in)
    by_clock = BaiduClient(cfg, transport=mock, clock=lambda: 0.0, sleep=_instant_sleep)
    by_registry = BaiduClient(cfg, transport=mock, rate_limiter=RateLimitRegistry(cfg))
    by_base_url = BaiduClient(cfg, base_url="http://127.0.0.1:9")

    for client in (by_transport, by_http_client, by_clock, by_registry, by_base_url):
        assert client.egress is not shared_egress
        assert client.egress.shared is False
    assert egress_snapshot() == {
        "built": True,
        "bindings": before,
        "max_concurrency": MAX_CONCURRENCY,
        "stale": 0,
    }
    # 注入连接池时归调用方管：`_owns_client` 必须仍是 False，否则每次探测泄漏一个池。
    assert by_http_client._owns_client is False
    assert by_transport._owns_client is True


def test_breaker_is_account_level_not_client_level(shared_egress: Egress) -> None:
    """③：一个客户端攒够连败，另一个客户端的请求应当立刻被拦。

    修之前两份熔断注册表各数各的连败，「账号已被限」这个事实永远凑不齐阈值——这正是
    PRD §4.6 熔断要防的事：明知不通还继续打，只会在烧配额。
    """
    cfg = _settings()
    first = BaiduClient(cfg)
    second = BaiduClient(cfg)
    for _ in range(cfg.concurrency_breaker_threshold):
        first.breakers.record_failure("place", transport=True)

    assert first.breakers.before_request("place").allowed is False
    assert second.breakers.before_request("place").allowed is False, "熔断还是每 client 一份"
    # 按服务分桶的隔离性没丢：geocode 不受 place 连败的影响。
    assert second.breakers.before_request("geocode").allowed is True


def test_in_flight_peak_is_capped_across_clients(shared_egress: Egress) -> None:
    """④：跨三个客户端并发 12 个请求，同时在途不许超过 `BAIDU_MAX_CONCURRENCY`。

    🔴 为什么这里要手改 `egress.http._transport`：默认路径的客户端**没有** transport 注入口
    （一注就变成私有出口，那就测不到共享了）。只换最底层的传输层，其余接线原样，
    请求依然走「桶 → 熔断 → 闸 → 连接池」整条真实链路，只是最后一个字节不出本机。
    """
    spy = _CountingTransport()
    # 只换最底层的传输层，其余接线原样：默认路径的客户端没有 transport 注入口，一注就变成
    # 私有出口，那就测不到「共享」了。请求依然走桶 → 熔断 → 闸 → 连接池整条真实链路。
    shared_egress.http._transport = spy
    clients = [BaiduClient(_settings()) for _ in range(3)]

    async def drive() -> list[object]:
        pairs = [(client, index) for index, client in enumerate(clients * 4)]
        return await asyncio.gather(*(client.geocode(f"地址{index}") for client, index in pairs))

    results = asyncio.run(drive())
    assert spy.total == 12
    assert all(getattr(result, "ok", False) for result in results)
    assert spy.peak >= 2, "这轮没有真正并发起来，断言是空的"
    assert spy.peak <= MAX_CONCURRENCY, f"在途峰值 {spy.peak} 突破了账号级上限"
