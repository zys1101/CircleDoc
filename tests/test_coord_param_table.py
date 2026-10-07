"""FR-406 的「接口 × 坐标参数名 × 坐标顺序」常量表断言（PRD §3.2 FR-406、风险 R8、笔记 §6.2）。

为什么单独立一个文件：官方自己三处不统一——逆地理 `coordtype`（**无下划线**、传字符串）、
地点检索 `coord_type`（有下划线、传**数字** 3）、路线规划 `coord_type`（有下划线、传字符串）。
坐标顺序同时有 `纬度,经度` 与 `{lng,lat}` 两种写法。写错**不会报错**，只会让点位整体静默
偏移，是最难事后发现的一类数据错误，所以这里把它当数据正确性来钉，而不是当参数校验来测。

本文件有两条腿，缺一条就测不到真问题：
  ① 钉住 `infra/baidu_client.py:COORD_INPUT_PARAM` 的字面值与命名/取值类型规则；
  ② 用**真实** `BaiduClient` + `httpx.MockTransport` 抓实际发出去的 query 参数，与表逐条对照。
只做 ① 不够：`COORD_INPUT_PARAM` 目前没有任何调用点读它（各方法逐条硬编码），它是一张
「文档表」；真正的漂移只可能发生在调用点，所以必须由 ② 兜住。

真实出网：0——`MockTransport` 不建任何连接，AK 是假串，`metrics=` 注入独立累加器，
`rate_limiter=` 用高吞吐假桶以免单测被 0.5 QPS 拖成十几秒。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from infra.baidu_client import (
    BD09LL,
    BD09LL_NUMERIC,
    COORD_INPUT_PARAM,
    ENDPOINTS,
    PLACE_MAX_KEYWORDS,
    PLACE_PAGE_SIZE_MAX,
    PLACE_RADIUS_MAX_M,
    BaiduClient,
    to_baidu_latlng,
)
from infra.errors import CircleDocError
from infra.metrics import Metrics
from infra.ratelimit import FakeClock, RateLimitRegistry

CENTER = (31.2304, 121.4737)
OTHER = (31.2411, 121.4612)
"""两个上海坐标，`(lat, lng)` 约定（笔记 §2.5）。lat≈31、lng≈121 差得很远，
顺序写反了一定会被断言抓到——所以不要换成两个接近的数。"""

COORD_PARAM_NAMES = {"coord_type", "coordtype", "ret_coordtype"}


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


class Recorder:
    """把每个真实发出的请求记下来：路径 + query 参数字典。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def params_for(self, path: str) -> dict[str, str]:
        hits = [params for p, params in self.calls if p == path]
        assert hits, f"没有请求打到 {path}；实际抓到 {self.paths}"
        return hits[-1]

    def handler(self, request: Any) -> Any:
        import httpx

        self.calls.append((request.url.path, dict(request.url.params)))
        path = request.url.path
        if "routematrix" in path:
            body = {
                "status": 0,
                "result": [{"status": 0, "duration": {"value": 300}, "distance": {"value": 600}}],
            }
        else:
            body = {"status": 0, "result": {}, "results": [], "total": 0}
        return httpx.Response(200, json=body)


def make_client(recorder: Recorder) -> BaiduClient:
    """假 AK + MockTransport + 高吞吐假令牌桶：零出网，但不绕过任何真实链路逻辑。"""
    import httpx

    clock = FakeClock()
    buckets = dict.fromkeys(("place", "regeo", "route", "geocode", "suggest"), (500.0, 500))
    return BaiduClient(
        ak="fake-ak-for-mock-transport-only",
        transport=httpx.MockTransport(recorder.handler),
        metrics=Metrics(label="coord-param-table-test"),
        rate_limiter=RateLimitRegistry(clock=clock.now, sleep=clock.sleep, buckets=buckets),
        mode_count=1,
    )


def drive_all_services(client: BaiduClient) -> None:
    """六个接口各发一次真实（mock）请求，让参数表与调用点在同一轮里对照。"""

    async def scenario() -> None:
        await client.geocode("上海市黄浦区样例路 1 号")
        await client.reverse_geocode(CENTER)
        await client.search_place("药店$综合医院", location=CENTER, radius=1500)
        await client.search_place_in_region("药店", region="黄浦区", center=CENTER)
        await client.direction("walking", CENTER, OTHER)
        await client.route_matrix("walking", [CENTER], [OTHER])

    run(scenario())


# --------------------------------------------------------------------------- ① 常量表本身
def test_coord_input_param_table_is_verbatim() -> None:
    """逐字钉住这张表：任何一格改动都必须先回去核对笔记 §6.2，而不是顺手改测试。"""
    expected = {
        "geocode": None,  # §6.2：地理编码没有入参坐标
        "regeo": ("coordtype", BD09LL),  # ⚠️ 无下划线 + 字符串
        "place": ("coord_type", str(BD09LL_NUMERIC)),  # ⚠️ 有下划线 + 数字
        "place_region": ("coord_type", str(BD09LL_NUMERIC)),
        "direction": ("coord_type", BD09LL),  # ⚠️ 有下划线 + 字符串
        "routematrix": ("coord_type", BD09LL),
    }
    assert expected == COORD_INPUT_PARAM


def test_underscoreless_spelling_belongs_to_regeo_only() -> None:
    """`coordtype`（无下划线）是逆地理独有拼写；混用即 R8 的原始形态。"""
    entries = {service: entry for service, entry in COORD_INPUT_PARAM.items() if entry}
    no_underscore = sorted(service for service, (name, _) in entries.items() if name == "coordtype")
    with_underscore = sorted(
        service for service, (name, _) in entries.items() if name == "coord_type"
    )
    assert no_underscore == ["regeo"]
    assert with_underscore == ["direction", "place", "place_region", "routematrix"]
    assert BD09LL == "bd09ll"


def test_place_is_the_only_numeric_coord_type() -> None:
    """地点检索传数字、其余传字符串——**同一个参数名两种值类型**是本项目最易写错处。"""
    assert BD09LL_NUMERIC == 3
    assert str(BD09LL_NUMERIC).isdigit()
    assert not BD09LL.isdigit()
    for service, entry in COORD_INPUT_PARAM.items():
        if entry is None:
            continue
        name, value = entry
        if name == "coord_type":
            is_numeric_family = service in ("place", "place_region")
            assert value.isdigit() is is_numeric_family, service
        else:
            assert value == BD09LL, service


def test_table_never_declares_ret_coordtype() -> None:
    """出参坐标系一律不发：官方值域三处不同，传 `bd09ll` 属于凭记忆猜测（笔记 §6.2）。"""
    for entry in COORD_INPUT_PARAM.values():
        if entry is not None:
            assert entry[0] != "ret_coordtype"


# --------------------------------------------------------------------------- ② 真实请求体对照
def test_outgoing_coord_params_match_the_table() -> None:
    """抓真实发出的 query 参数，逐接口对照常量表；表与调用点任一漂移都在这里炸。"""
    recorder = Recorder()
    client = make_client(recorder)
    try:
        drive_all_services(client)
    finally:
        run(client.aclose())

    expected = {
        ENDPOINTS["geocode"]: None,
        ENDPOINTS["regeo"]: ("coordtype", "bd09ll"),
        ENDPOINTS["place"]: ("coord_type", "3"),
        ENDPOINTS["place_region"]: None,  # 见下方专门用例
        ENDPOINTS["direction_v2_walking"]: ("coord_type", "bd09ll"),
        ENDPOINTS["routematrix_v2_walking"]: ("coord_type", "bd09ll"),
    }
    for path, entry in expected.items():
        params = recorder.params_for(path)
        if entry is None:
            continue
        name, value = entry
        assert params.get(name) == value, f"{path} 的 {name} 应为 {value}，实发 {params.get(name)}"

    # 反向：错误拼写绝不能同时出现（出现说明有人把两种写法都发了，官方按其中一个解读）。
    assert "coord_type" not in recorder.params_for(ENDPOINTS["regeo"])
    assert "coordtype" not in recorder.params_for(ENDPOINTS["place"])
    assert "coordtype" not in recorder.params_for(ENDPOINTS["direction_v2_walking"])

    # 全局：任何请求都不带 ret_coordtype。
    assert all("ret_coordtype" not in params for _, params in recorder.calls)


def test_geocode_sends_no_coordinate_param_at_all() -> None:
    """地理编码入参是地址，表里是 `None` ⇒ 实际请求里一个坐标参数都不该有。"""
    recorder = Recorder()
    client = make_client(recorder)
    try:
        run(client.geocode("上海市黄浦区样例路 1 号", city="上海"))
    finally:
        run(client.aclose())
    params = recorder.params_for(ENDPOINTS["geocode"])
    assert set(params) & COORD_PARAM_NAMES == set()
    assert params["output"] == "json"  # 官方默认 xml，不显式传就在解析处炸


def test_place_region_omits_coord_type_and_is_declared_not_sent() -> None:
    """行政区划形态没有输入坐标，实际不发 `coord_type`。

    这是本轮新发现的**表与调用点的一处口径差**（不修代码、先如实登记）：
    `COORD_INPUT_PARAM["place_region"]` 声明了 `coord_type=3`，而
    `search_place_in_region()` 的形态是 `region` 名称检索、只有可选的 `center`。
    官方 `coord_type` 默认即 3（bd09ll），所以不发不影响结果；但表按字面读会以为
    它在发。保留声明、断言实测口径，两边都要能被后人看懂。
    """
    recorder = Recorder()
    client = make_client(recorder)
    try:
        run(client.search_place_in_region("药店", region="黄浦区", center=CENTER))
    finally:
        run(client.aclose())
    params = recorder.params_for(ENDPOINTS["place_region"])
    assert params["region"] == "黄浦区"
    assert params["center"] == to_baidu_latlng(CENTER)  # 有坐标入参时也保持 `纬度,经度`
    assert "coord_type" not in params


# --------------------------------------------------------------------------- 坐标顺序
def test_to_baidu_latlng_normalises_three_shapes_to_lat_first() -> None:
    assert to_baidu_latlng(CENTER) == "31.230400,121.473700"
    assert to_baidu_latlng({"lat": 31.2304, "lng": 121.4737}) == "31.230400,121.473700"
    assert to_baidu_latlng("31.2304, 121.4737") == "31.230400,121.473700"
    # 顺序写反不会报错、只会偏移，所以显式断言「不是 lng 在前」。
    assert to_baidu_latlng(CENTER) != "121.473700,31.230400"


def test_every_coordinate_field_in_real_requests_is_lat_first() -> None:
    """`location` / `center` / `origin` / `destination` 全部纬度在前（笔记 §2.5、§6.2 第 3 条）。"""
    recorder = Recorder()
    client = make_client(recorder)
    try:
        drive_all_services(client)
    finally:
        run(client.aclose())

    lat = f"{CENTER[0]:.6f}"
    dest_lat = f"{OTHER[0]:.6f}"
    # 字段 → 该字段的纬度（六位小数）。`destination` 用的是另一个点，正因为两个点的
    # 纬度差得远，写反顺序或填错点都会在这里露出来。
    expected_lat: dict[str, str] = {
        "location": lat,  # 逆地理 / 圆形检索的圆心的前缀
        "center": lat,
        "origin": lat,
        "destination": dest_lat,
        "origins": lat,  # 批量算路是 `|` 分隔多值
        "destinations": dest_lat,
    }
    seen = 0
    for path, params in recorder.calls:
        for field_name, prefix in expected_lat.items():
            value = params.get(field_name)
            if value is None:
                continue
            for item in value.split("|"):
                seen += 1
                assert item.startswith(f"{prefix},"), f"{path} 的 {field_name}={item} 纬度不匹配"
                assert not item.startswith("121."), f"{path} 的 {field_name}={item} 经度在前"
    assert (
        set(expected_lat) - {name for _, params in recorder.calls for name in params} == set()
    ), "有坐标字段没被抓到，本用例已退化成空断言"
    assert seen >= 6, f"只抓到 {seen} 个坐标字段值"


# --------------------------------------------------------------------------- 地点检索配额族常量
def test_place_family_constants_match_official_limits() -> None:
    """`$` 最多 10 个关键字、`page_size` 按保守值 10、半径上限 10 km（笔记 §2.3、FAQ 2548）。"""
    assert PLACE_MAX_KEYWORDS == 10
    assert PLACE_PAGE_SIZE_MAX == 10
    assert PLACE_RADIUS_MAX_M == 10000


def test_over_keyword_cap_is_refused_before_egress() -> None:
    """11 个关键字必须在本地就拒掉：真发出去只会静默截断，把维度计数算少。"""
    recorder = Recorder()
    client = make_client(recorder)
    query = "$".join(f"类别{index}" for index in range(PLACE_MAX_KEYWORDS + 1))
    try:
        with pytest.raises(CircleDocError) as exc:
            run(client.search_place(query, location=CENTER, radius=1500))
    finally:
        run(client.aclose())
    assert recorder.calls == []
    assert "query" in str(exc.value.detail)


def test_radius_over_10km_is_clamped_not_forwarded() -> None:
    """超 10 km 官方退化成城市级检索——静默污染落圈判定，所以必须在本地夹住。"""
    recorder = Recorder()
    client = make_client(recorder)
    try:
        run(client.search_place("药店", location=CENTER, radius=15000))
    finally:
        run(client.aclose())
    params = recorder.params_for(ENDPOINTS["place"])
    assert params["radius"] == str(PLACE_RADIUS_MAX_M)
    assert params["radius_limit"] == "true"  # 大写 True 会让参数静默失效
    assert params["page_size"] == "10" and params["page_num"] == "1"  # 一律不翻页
