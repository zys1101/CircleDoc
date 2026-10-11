"""`/api/geocode`、`/api/regeo`、`/api/suggest` 的**接口级**单测（FR-103 / FR-108）。

为什么单独立一个文件：这三条路由此前只在 `api/health.py` 的启动探测里被间接触碰过，
`tests/` 里没有任何一条用例真正 GET 过 `/api/regeo` 或 `/api/suggest`。而 FR-103 与 FR-108
把这两条链推到了前台——`web/isochrone.js` 现在每次换中心点请求 `/api/regeo`，把
`district` / `town` / `street` / `adcode` 直接铺到面板上；地址输入框则边打字边问 `/api/suggest`。
响应形状一旦漂移，前端不会报错，只会把地址行渲染成空态、把联想列表渲染成空的，
而用户会以为「这个位置就是查不到地址」「这功能不认我的小区」。

🔴 零真实出网：与 `tests/test_isochrone_api.py` 用同一个口子（`api/geo.py:38` 的
`app.state.geo_service` 优先），注入的是**真实** `BaiduClient` + `httpx.MockTransport`，
所以令牌桶、退避、缓存写入、三栏计量全是真链路，只有网线是假的
（条款 3.2.3(ii) 禁止对线上服务施压）。每条用例都另断言 `transport.calls`，
「根本没被叫到」比「叫到了但是假的」更能证明没出网。

本文件盯的五类问题：
  ① **平铺形状**：§4.7 的出参在顶层（`GeoOutcome.as_dict()` 不套 `data` 层），
     前端 `renderAddressLine()` 就是按顶层取的；
  ② **字段来源**：`town` / `district` / `adcode` 只能来自官方 `addressComponent`，
     绝不从 `formatted_address` 文本切分（PRD §4.7 末尾专门禁止）；
  ③ **坐标顺序**：入参 `lnglat` 是经度在前，发给百度的 `location` 是纬度在前（R8）；
  ④ **降级说人话**：mock / 空 results 一律 **200 + `empty` + 可读 `message`**，
     而且**不造假地址**——「这个坐标查不到」和「编一个像样的地址」是两回事；
  ⑤ **配额门槛**（FR-108）：联想**默认关闭**、`region` 必选、关键字 ≥ 3 字，三道门都挡在
     出网之前，而开启的那一次必须记进 `place_calls`——它与地点检索同池，记错栏就等于没人看见消耗。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from api.app import app
from fastapi.testclient import TestClient
from infra.baidu_client import ENDPOINTS, BaiduClient
from infra.cache_store import CacheStore
from infra.config import Settings
from infra.metrics import Metrics
from infra.ratelimit import FakeClock, RateLimitRegistry
from services.geo import (
    MSG_CACHE_HIT,
    MSG_EMPTY_REGEO,
    MSG_EMPTY_SUGGEST,
    MSG_MOCK_MODE,
    MSG_SAMPLE_UNAVAILABLE,
    MSG_SUGGEST_OFF,
    GeoService,
    suggest_enabled,
)

CENTER = "116.404,39.915"
"""「经度,纬度」——与前端 `state.center` 交给 `/api/regeo` 的形状逐字一致。"""

FAKE_AK = "fake-ak-for-mock-transport-only"
"""假 AK 只为让 `BaiduClient.live_enabled` 为真，从而把实时分支整条跑完。"""

BUCKET_CAPACITY = dict.fromkeys(("place", "regeo", "route", "geocode", "suggest"), (500.0, 500))
"""高吞吐假令牌桶：逆地理真实是 0.5 QPS，单测里等不起那个排队。"""

RE_GEO_PAYLOAD: dict[str, Any] = {
    "status": 0,
    "result": {
        "location": {"lng": 116.404, "lat": 39.915},
        "formatted_address": "北京市东城区王府井大街1号",
        "addressComponent": {
            "country": "中国",
            "country_code_iso": "CN",
            "province": "北京市",
            "city": "北京市",
            "district": "东城区",
            "town": "东华门街道",
            "town_code": "110101001000",
            "street": "王府井大街",
            "street_number": "1号",
            "adcode": 110101,
            "cityCode": 1,
        },
    },
}

GEOCODE_PAYLOAD: dict[str, Any] = {
    "status": 0,
    "result": {
        "location": {"lng": 116.4074, "lat": 39.9042},
        "precision": 4,
        "confidence": 70,
        "comprehension": 100,
        "level": "道路",
    },
}

SUGGEST_PAYLOAD: dict[str, Any] = {
    "status": 0,
    # 笔记 §2.4 只核验了**入参**，返回体字段名尚未与官方核对（§9.2-⑨ 那条待办）。
    # 这里按 `_parse_suggest` 的防御式假设给三条，恰好覆盖它丢条目的三种理由：
    # 完整可用 / 没有坐标（点了也落不了点）/ 境外（个人档不可用，且会连带拖垮 regeo 与 POI）。
    "result": [
        {
            "name": "温特豪德花园",
            "location": {"lng": 116.4074, "lat": 39.9042},
            "district": "东城区",
            "city": "北京市",
            "province": "北京市",
            "adcode": 110101,
        },
        {"name": "没有坐标的那条", "district": "朝阳区"},
        {"name": "境外的一条", "location": {"lng": 139.7, "lat": 35.6}},
    ],
}


class FakeTransport:
    """按路径分发 canned 报文，同时记下真实发出去的 query 参数。"""

    def __init__(self, bodies: dict[str, dict[str, Any]]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, dict[str, str]]] = []

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def params_for(self, path: str) -> dict[str, str]:
        hits = [params for p, params in self.calls if p == path]
        assert hits, f"没有请求打到 {path}；实际抓到 {self.paths}"
        return hits[-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=self.bodies.get(request.url.path, {"status": 0}))


def make_service(
    tmp_path: Path,
    *,
    bodies: dict[str, dict[str, Any]] | None = None,
    cache_enabled: bool = False,
    suggest_enabled: bool = False,
) -> tuple[GeoService, FakeTransport]:
    """真 `GeoService` + 真 `BaiduClient` + 假网线 + 独立临时缓存库。

    `cache_enabled=False` 是默认：绝大多数用例要看的正是「这一层没东西时回什么」。
    唯一打开它的那条用例（缓存去重）自己传 `tmp_path` 下的库，绝不碰仓库里那份
    运行期缓存——那里面可能是真实 AK 跑出来的报文。

    `suggest_enabled=False` 同样是默认，而且它就是 FR-108 的**线上默认值**：联想与地点
    检索共用 100 次/日，默认开着的那个实现才是异常态。
    """
    transport = FakeTransport(bodies or {})
    settings = Settings(
        baidu_server_ak=FAKE_AK,
        baidu_browser_ak="",
        data_mode="auto",
        cache_enabled=cache_enabled,
        suggest_enabled=suggest_enabled,
    )
    clock = FakeClock()
    client = BaiduClient(
        ak=FAKE_AK,
        transport=httpx.MockTransport(transport.handler),
        metrics=Metrics(label="geo-api-test"),
        rate_limiter=RateLimitRegistry(clock=clock.now, sleep=clock.sleep, buckets=BUCKET_CAPACITY),
        mode_count=1,
    )
    service = GeoService(
        settings,
        client=client,
        cache=CacheStore(settings, db_path=str(tmp_path / "geo-cache.db"), enabled=cache_enabled),
    )
    return service, transport


@pytest.fixture(autouse=True)
def no_leftover_injection() -> Iterator[None]:
    """注入的假服务必须收干净：`app` 是模块级单例，别的测试文件也用它。

    漏一根线的话，后面任何模块用 `TestClient(app)` 都会拿到这里的假对象——
    那种「单独跑红、整跑绿」的交错失败最难查。
    """
    yield
    if hasattr(app.state, "geo_service"):
        del app.state.geo_service


def serve(service: GeoService) -> TestClient:
    """不跑 lifespan（`TestClient(app)` 而不是 `with TestClient(app)`）：启动探测不碰网络。"""
    app.state.geo_service = service
    return TestClient(app)


def regeo(http: TestClient, lnglat: str = CENTER, mode: str = "live") -> dict[str, Any]:
    return http.get(f"/api/regeo?lnglat={lnglat}&data_mode={mode}").json()


# --------------------------------------------------------------------------------------
# ① mock：可读的空，而不是假地址
# --------------------------------------------------------------------------------------
def test_mock_regeo_is_readable_empty_and_never_egresses(tmp_path: Path) -> None:
    """FR-103 在 mock / 无合成层时必须显示「取不到」，而且一次出网都不能有。

    这条同时钉住两件事：① 前端地址行在空态直接铺 `message`，所以这句话本身要能读；
    ② 合成层**不替逆地理造地址**（`services/geo.py:_sample_for` 没有 provider 就回 None），
    所以响应里连 `formatted_address` 这个键都不该出现——出现了就是假数据。

    零出网的证据是 `transport.calls == []`：本栈里唯一能通向网络的口子就是这个
    MockTransport 处理器，它没被叫到就等于没出网。这里**不**套
    `tests/test_blind_spots.py` 那种 `socket` 守卫——`TestClient` 每轮请求都要建
    asyncio 事件循环，而 Windows 的 proactor 循环靠 `socket.socketpair()` 起自管道，
    守卫会先把测试框架打死（实测报 `AssertionError: … 不允许碰 socket`）。
    """
    service, transport = make_service(tmp_path)
    http = serve(service)
    response = http.get(f"/api/regeo?lnglat={CENTER}&data_mode=mock")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["empty"] is True
    assert body["degraded"] is True and body["degraded_reason"] == "mock_mode"
    assert body["data_source"] == "sample"
    assert body["meta"]["sample_layer"] == "unavailable"
    assert MSG_MOCK_MODE in body["message"]
    assert "formatted_address" not in body
    assert body["usage"]["http_requests"] == 0
    assert transport.calls == []


# --------------------------------------------------------------------------------------
# ② live：§4.7 的出参平铺在顶层，且只来自 addressComponent
# --------------------------------------------------------------------------------------
def test_live_regeo_flattens_the_prd_fields(tmp_path: Path) -> None:
    """`{formatted_address, district, town, town_code, adcode, street}` 全在顶层。

    🔴 前端只读顶层：`GeoOutcome.as_dict()` 一旦改成套一层 `data`，
    `renderAddressLine()` 会拿到 undefined 并静默退化成空态，而这条断言会先红。
    """
    service, _ = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    body = regeo(serve(service))

    assert body["ok"] is True and body["service"] == "regeo"
    assert body["data_source"] == "live"
    assert body["degraded"] is False and body["empty"] is False
    for key in ("formatted_address", "district", "town", "town_code", "adcode", "street"):
        assert key in body, f"§4.7 出参缺 {key} —— 前端地址行会退化成空态"
    assert body["district"] == "东城区"
    assert body["town"] == "东华门街道"
    assert body["street"] == "王府井大街" and body["street_number"] == "1号"
    assert body["adcode"] == 110101
    assert body["coordinate_system"] == "bd09ll"
    assert body["request_id"]


def test_address_parts_never_come_from_splitting_formatted_address(tmp_path: Path) -> None:
    """报文里只有 `formatted_address` 像地址、`addressComponent` 没给区与街道时，
    响应**不能**凭文本切出 `district` / `town`。

    PRD §4.7 末尾专门禁止切分：换个城市「省市区街道门牌」的排列就错位，切出来的街道
    可能是区、也可能是路名，而面板上没人看得出它错了。
    """
    payload = {
        "status": 0,
        "result": {
            "formatted_address": "上海市黄浦区南京东路",
            "addressComponent": {"country": "中国", "province": "上海市", "city": "上海市"},
        },
    }
    service, _ = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: payload})
    body = regeo(serve(service))

    assert body["ok"] is True, "有 formatted_address 就不算空结果"
    assert body["formatted_address"] == "上海市黄浦区南京东路"
    assert body["province"] == "上海市"
    for key in ("district", "town", "town_code", "adcode", "street"):
        assert key not in body, f"{key} 是从 formatted_address 切出来的假字段"


# --------------------------------------------------------------------------------------
# ③ 坐标顺序与坐标回显
# --------------------------------------------------------------------------------------
def test_request_sends_lat_first_while_the_response_keeps_lng_first(tmp_path: Path) -> None:
    """R8 的现场形态：对外「经度,纬度」、对内「纬度,经度」，换算只做一次。

    写反不会报错，只会让点位整体偏移几百公里，所以两侧顺序都要断言。
    """
    service, transport = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    body = regeo(serve(service))

    params = transport.params_for(ENDPOINTS["regeo"])
    assert params["location"] == "39.915000,116.404000"
    assert params["coordtype"] == "bd09ll"  # 无下划线 + 字符串，两处都是雷区
    assert "coord_type" not in params
    assert body["lnglat"] == [116.404, 39.915]


def test_response_lnglat_is_the_normalized_input_not_the_raw_string(tmp_path: Path) -> None:
    """入参写成 `116.4040, 39.915` 也要回规范化的 `[116.404, 39.915]`（前端按它做缓存键）。"""
    service, _ = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    body = regeo(serve(service), "116.4040, 39.915")
    assert body["lnglat"] == [116.404, 39.915]


# --------------------------------------------------------------------------------------
# ④ 配额护栏：同一坐标第二次不再出网
# --------------------------------------------------------------------------------------
def test_second_identical_regeo_is_served_from_cache(tmp_path: Path) -> None:
    """FR-103 的配额依据：逆地理是 0.5 QPS / 300 次/日（PRD §4.7 配额表），
    而换中心点就会触发一次查询，所以同坐标的第二次必须命中缓存。

    这条同时验证 `GeoService._params_for_regeo()` 与 `BaiduClient` 的参数镜像逐字段一致：
    缓存键由参数算，差一个字段就等于换了个键，表现是命中率静默归零而没人报错
    （`infra/cache_keys.py` 开头写明了这个坑）。
    """
    service, transport = make_service(
        tmp_path,
        bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD},
        cache_enabled=True,
    )
    http = serve(service)
    first = regeo(http)
    second = regeo(http)

    assert first["data_source"] == "live"
    assert second["data_source"] == "cache"
    assert second["ok"] is True
    assert second["message"] == MSG_CACHE_HIT
    assert second["usage"]["http_requests"] == 0
    assert second["usage"]["cache_hits"] == 1
    assert second["meta"]["cache"]["hit"] is True
    assert second["formatted_address"] == first["formatted_address"]
    assert len(transport.calls) == 1, f"出网 {len(transport.calls)} 次，缓存去重没成立"


# --------------------------------------------------------------------------------------
# ⑤ 空 results 留在实时层：查不到 ≠ 编一个
# --------------------------------------------------------------------------------------
def test_empty_live_result_stays_at_the_live_layer(tmp_path: Path) -> None:
    """`status=0` 但 `result` 是空的：内容确实来自实时层，所以 `data_source` 仍是 `live`。

    不退到合成层是刻意的（`services/geo.py:_resolve` 里有注释）：一次抖动后的空数据
    若被当成合成结果，前端会把「这里没地址」显示成「示例数据」。
    """
    service, _ = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: {"status": 0, "result": {}}})
    body = regeo(serve(service))

    assert body["ok"] is False and body["empty"] is True
    assert body["degraded"] is True and body["degraded_reason"] == "empty_results"
    assert body["data_source"] == "live"
    assert MSG_EMPTY_REGEO in body["message"]
    assert "formatted_address" not in body
    assert body["usage"]["http_requests"] == 1


# --------------------------------------------------------------------------------------
# ⑥ 入参校验：400 + 指名字段，而且本地就挡下
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "空串也要指名字段，而不是 FastAPI 的 422 通用文案"),
        ("116.404", "只有一个数"),
        ("东经116,北纬39", "粘进了带汉字的写法"),
        ("200,100", "超出经纬度合法区间"),
    ],
)
def test_invalid_lnglat_is_a_400_naming_the_field(tmp_path: Path, raw: str, reason: str) -> None:
    service, transport = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    response = serve(service).get("/api/regeo", params={"lnglat": raw, "data_mode": "live"})

    assert response.status_code == 400, f"{reason}：实得 {response.status_code}"
    error = response.json()["error"]
    assert error["code"] == "invalid_geo_param"
    assert error["category"] == "client"
    assert error["detail"]["field"] == "lnglat"
    assert error["message"], "400 要带一句能直接显示给用户的话"
    assert response.json()["request_id"]
    assert transport.calls == [], "入参校验必须在本地挡住，不该先发一次真实请求"


def test_missing_lnglat_is_refused_not_defaulted(tmp_path: Path) -> None:
    """少了 `lnglat` 不能悄悄用默认坐标回一个地址——那会把别人的社区写成你的中心点。"""
    service, transport = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    http = serve(service)
    response = http.get("/api/regeo?data_mode=live")
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["field"] == "lnglat"
    assert transport.calls == []


def test_foreign_coordinate_is_refused_before_egress(tmp_path: Path) -> None:
    """境外坐标：个人认证开发者对境外逆地理无配额（笔记 §3.1 权益表「-」），必须本地拒。"""
    service, transport = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    http = serve(service)
    response = http.get("/api/regeo?lnglat=139.745,35.680&data_mode=live")

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "outside_service_area"
    assert payload["error"]["detail"]["source"] == "request"
    assert payload["error"]["detail"]["lnglat"] == [139.745, 35.68]
    assert transport.calls == []


# --------------------------------------------------------------------------------------
# ⑦ 信封卫生：request_id 有、AK 永不出现
# --------------------------------------------------------------------------------------
def test_envelope_carries_request_id_and_never_leaks_the_ak(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, bodies={ENDPOINTS["regeo"]: RE_GEO_PAYLOAD})
    body = regeo(serve(service))

    assert isinstance(body["request_id"], str) and body["request_id"]
    blob = json.dumps(body, ensure_ascii=False)
    assert FAKE_AK not in blob
    assert "server_ak" not in blob
    # `meta.params` 回显的是规范化入参，用于排查缓存键，同样不许掺密钥
    assert set(body["meta"]["params"]) == {"location", "coordtype"}


# --------------------------------------------------------------------------------------
# ⑧ 顺带补上 /api/geocode：FR-101 方式② 此前也没有接口级用例
# --------------------------------------------------------------------------------------
def test_geocode_returns_a_bd09ll_pair(tmp_path: Path) -> None:
    service, transport = make_service(tmp_path, bodies={ENDPOINTS["geocode"]: GEOCODE_PAYLOAD})
    http = serve(service)
    body = http.get(
        "/api/geocode", params={"address": "北京市东城区北新桥三条", "data_mode": "live"}
    ).json()

    assert body["ok"] is True and body["service"] == "geocode"
    assert body["lnglat"] == [116.4074, 39.9042], "[经度, 纬度]，与 /api/regeo 同一约定"
    assert body["confidence"] == 70 and body["precision"] == 4
    assert transport.params_for(ENDPOINTS["geocode"])["output"] == "json"


def test_empty_address_is_a_400_naming_the_field(tmp_path: Path) -> None:
    service, transport = make_service(tmp_path, bodies={ENDPOINTS["geocode"]: GEOCODE_PAYLOAD})
    response = serve(service).get("/api/geocode?address=&data_mode=live")

    assert response.status_code == 400
    assert response.json()["error"]["detail"]["field"] == "address"
    assert transport.calls == []


# --------------------------------------------------------------------------------------
# ⑥ FR-108 地址联想：默认关闭、三道门槛、候选形状
# --------------------------------------------------------------------------------------


def suggest(
    http: TestClient,
    keyword: str = "温特豪德",
    region: str | None = "北京",
    mode: str = "live",
    **extra: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = {"keyword": keyword, "data_mode": mode}
    if region is not None:
        params["region"] = region
    params.update(extra)
    return http.get("/api/suggest", params=params).json()


def test_suggest_is_off_by_default_and_egresses_nothing(tmp_path: Path) -> None:
    """PRD FR-108 的门槛原文是「**默认关闭**、可手动开启」，默认态必须是零出网加一句解释。

    关闭走的是 `_resolve(live=None)`：不是「请求失败了」，而是**根本没发请求**，
    所以 `transport.calls` 必须是空数组（比 200 更能证明没烧配额）。
    而文案要能读——前端 `#suggest-list` 直接铺 `message`，用户得知道去哪开。
    """
    service, transport = make_service(
        tmp_path, bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD}, suggest_enabled=False
    )
    body = suggest(serve(service))

    assert body["ok"] is False
    assert body["empty"] is True
    assert body["degraded_reason"] == "suggest_disabled"
    assert MSG_SUGGEST_OFF in body["message"]
    assert MSG_SAMPLE_UNAVAILABLE in body["message"], "合成层没有联想样本，这句话也得照实说"
    assert not body.get("items"), f"关闭态不该有候选，实际 {body.get('items')}"
    assert transport.calls == []


def test_suggest_switch_reaches_the_service_from_dotenv(tmp_path: Path) -> None:
    """🔴 这条是本轮改 `infra/config.py` 的原因：开关此前只能靠**真·环境变量**。

    `suggest_enabled()` 的兜底读 `os.environ`，而 pydantic-settings 的 `env_file`
    只把值填进模型字段、**不写回环境变量**。所以在字段落地之前，
    `.env` 里写 `SUGGEST_ENABLED=true` 是静默无效的——用户照着 README 改了配置，
    输入框却还是「未启用」，而且没有任何一处报错。
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text("SUGGEST_ENABLED=true\n", encoding="utf-8")

    assert Settings().suggest_enabled is False, "默认必须关闭：它与地点检索共用 100 次/日"
    assert suggest_enabled(Settings(_env_file=str(dotenv))) is True
    assert suggest_enabled(Settings(suggest_enabled=True)) is True


def test_enabled_suggest_keeps_only_candidates_the_map_can_use(tmp_path: Path) -> None:
    """候选列表里每一项都必须能直接落点：没坐标的、境外的一律丢掉，而不是留给前端。

    丢境外那条尤其重要：个人档对境外配额为「-」，落到境外后 `regeo` / POI 全部白烧，
    而面板上只表现为「地址行说取不到」——FR-103 那条链会跟着一起假失败。
    """
    service, transport = make_service(
        tmp_path, bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD}, suggest_enabled=True
    )
    body = suggest(serve(service))

    assert body["ok"] is True
    assert body["data_source"] == "live"
    assert body["total"] == 1 and body["dropped"] == 2
    item = body["items"][0]
    assert item["name"] == "温特豪德花园"
    assert item["lnglat"] == [116.4074, 39.9042], "[经度, 纬度]，与 /api/regeo 同一约定"
    assert item["district"] == "东城区" and item["adcode"] == "110101"
    assert "没有坐标的那条" not in json.dumps(body, ensure_ascii=False)
    assert "境外的一条" not in json.dumps(body, ensure_ascii=False)

    outbound = transport.params_for(ENDPOINTS["suggest"])
    assert outbound["query"] == "温特豪德" and outbound["region"] == "北京"
    assert outbound["output"] == "json"
    # 笔记 §2.4 没记载 suggestion 有坐标系入参，未列出的参数不许假设存在（§2 开头那条纪律）。
    assert "coordtype" not in outbound and "coord_type" not in outbound


def test_enabled_suggest_is_counted_as_a_place_call(tmp_path: Path) -> None:
    """联想吃的是**地点检索**那 100 次/日（FAQ 2540 / 2549），所以 `place_calls` 必须 +1。

    这条是 FR-108 全部门槛存在的理由：如果它记在别的栏里，§6.1 的出网预算断言就会
    系统性低估真实消耗，而「一次体检即打满当日上限」的风险（R1′）没人看得见。
    """
    service, _ = make_service(
        tmp_path, bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD}, suggest_enabled=True
    )
    body = suggest(serve(service))

    assert body["usage"]["place_calls"] == 1
    assert body["usage"]["http_requests"] == 1


def test_city_limit_is_forwarded_only_when_the_caller_asked(tmp_path: Path) -> None:
    """`city_limit` 不传就不该出现在 query 里：它进缓存键，多一个键就多一份缓存分叉。"""
    service, transport = make_service(
        tmp_path, bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD}, suggest_enabled=True
    )
    http = serve(service)
    suggest(http)
    suggest(http, keyword="温特豪德花园", city_limit="true")

    plain, limited = transport.calls
    assert "city_limit" not in plain[1]
    assert limited[1]["city_limit"] == "true"
    assert len(transport.calls) == 2, "不同关键字就该问两次，这里没有去重可言"


@pytest.mark.parametrize("keyword", ["温", "温特"])
def test_short_keyword_is_a_400_naming_the_field(tmp_path: Path, keyword: str) -> None:
    """≥3 字这道门槛在**出网之前**：两个字的检索几乎必然召回一大片，白烧一次配额。

    前端 `#addr` 的输入框也挡一次（`web/isochrone.js` 的防抖里），但挡在本地不等于
    接口可以被绕过——这条钉的是后端那一半。
    """
    service, transport = make_service(
        tmp_path, bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD}, suggest_enabled=True
    )
    response = serve(service).get("/api/suggest", params={"keyword": keyword, "region": "北京"})

    assert response.status_code == 400
    detail = response.json()["error"]["detail"]
    assert detail["field"] == "keyword" and detail["min_len"] == 3
    assert transport.calls == []


def test_missing_region_is_a_400_naming_the_field(tmp_path: Path) -> None:
    """官方把 `region` 列为必选，少了它线上回 `status=2`；本地先挡，别把 2 号错误传给用户。"""
    service, transport = make_service(
        tmp_path, bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD}, suggest_enabled=True
    )
    response = serve(service).get(
        "/api/suggest", params={"keyword": "温特豪德", "data_mode": "live"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["detail"]["field"] == "region"
    assert transport.calls == []


def test_empty_suggestion_list_is_readable_not_an_error(tmp_path: Path) -> None:
    """`status=0` 但 `result` 为空是官方的正常回答，得留成「再输入几个字试试」而不是降级。

    和 FR-103 的 empty results 同一个取舍：`data_source` 仍是 `live`（这一层确实问过了），
    `degraded_reason` 是 `empty_results`，而不是偷偷退回合成层造几个候选。
    """
    service, transport = make_service(
        tmp_path,
        bodies={ENDPOINTS["suggest"]: {"status": 0, "result": []}},
        suggest_enabled=True,
    )
    body = suggest(serve(service))

    assert body["data_source"] == "live"
    assert body["empty"] is True
    assert body["degraded_reason"] == "empty_results"
    assert MSG_EMPTY_SUGGEST in body["message"]
    assert body["items"] == [] and body["total"] == 0
    assert len(transport.calls) == 1


def test_second_identical_suggest_is_served_from_cache(tmp_path: Path) -> None:
    """同一个词第二次问必须 0 出网、0 place 调用——这是「防抖 + 缓存」这道门槛的兑现处。"""
    service, transport = make_service(
        tmp_path,
        bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD},
        cache_enabled=True,
        suggest_enabled=True,
    )
    http = serve(service)
    first = suggest(http)
    second = suggest(http)

    assert first["data_source"] == "live" and len(transport.calls) == 1
    assert second["data_source"] == "cache"
    assert second["usage"]["http_requests"] == 0
    assert second["usage"]["place_calls"] == 0, "命中缓存不能再记一次检索"
    assert second["items"] == first["items"]
    assert len(transport.calls) == 1, f"出网 {len(transport.calls)} 次，缓存去重没成立"


def test_switching_the_flag_off_keeps_the_cache_readable(tmp_path: Path) -> None:
    """「关闭」只关**出网**那一环：缓存读不花配额，所以开过一次后的历史候选照样还在。

    这是 `services/geo.suggest()` 里那句注释的行为化断言。反过来说，如果关掉连缓存都不读，
    用户关掉开关后输入框会突然变得「什么都想不出来」，那比留着历史更容易被当成坏了。
    """
    service, transport = make_service(
        tmp_path,
        bodies={ENDPOINTS["suggest"]: SUGGEST_PAYLOAD},
        cache_enabled=True,
        suggest_enabled=True,
    )
    http = serve(service)
    assert suggest(http)["data_source"] == "live"

    service.settings = service.settings.model_copy(update={"suggest_enabled": False})
    body = suggest(http)

    assert body["data_source"] == "cache"
    assert body["degraded_reason"] != "suggest_disabled", "关掉开关不该把已有的历史候选也抹掉"
    assert body["usage"]["place_calls"] == 0
    assert len(transport.calls) == 1, "读缓存不是出网，第二次调用绝不能碰网线"
