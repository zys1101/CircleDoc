"""`/api/isochrone` 与 `/api/isochrone/polyline` 的接口级单测（M2 服务层 + HTTP 层）。

🔴 零真实出网：全部通过 `app.state.isochrone_service` 注入假 client（`api/geo.py:41-50` 的
同一个口子），任何一条请求都不会打到 `api.map.baidu.com`（条款 3.2.3(ii) 禁止对线上服务施压）。
假 client 的记账口径逐字复刻 `infra/baidu_client.py:797-833`：**一次 `route_matrix` = 1 个 HTTP
往返 + `origins × destinations` 个配额单位**，缓存命中时两者都是 0（缓存不消耗百度配额，记了
反而是假账）——所以这里断言出来的 `quota_units` / `http_requests` 与真实链路同口径。

覆盖 PRD §3.2 FR-201~206 与 §4.2 的验收点：
  ① 示例层（mock）长出的是**不规则**多边形且 0 次出网，且明显不等于同半径圆（FR-201 / D9）；
  ② 一轮 walking 体检落在 72~96 条配额、HTTP 只有 5~8 次（§4.2 第 267 行的两栏口径）；
  ③ ≤50 切分真的生效（每一批的目的地数都不超官方上限）；
  ④ `blocked_official` 与 `blocked_timeout` 各占一档、证据原样透出，绝不被合并推断（§4.2）；
  ⑤ 非法 `mode` / 境外坐标 / 越界 `minutes` → 400，且错误信封与 `api/app.py` 逐字段一致；
  ⑥ 预算不足 → **200 + degraded + 可读文案**，不是 500（FR-403）；
  ⑦ 第二次同参数请求命中缓存 → `http_requests` 掉下来、`cache_hits` 上去；
  ⑧ polyline 懒加载硬上限 12 次，第 13 次回可读结果对象而不是异常（FR-203 / D8）；
  ⑨ FR-205 的分轮进度（`progress`）逐轮上报；
  ⑩ `[BUG][M2]`：`usage` 出口懒加载额度与批次数，且键名与 `RouteProbe.usage()` 一致。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pytest
from api.app import app, error_payload
from fastapi.testclient import TestClient
from infra.baidu_client import BaiduResult, RouteMatrixResult
from services.isochrone_service import IsochroneService
from services.route_probe import POLYLINE_CALL_LIMIT, RouteProbe

CENTER = "116.404,39.915"
"""北京某社区的 BD09LL 中心点，格式「经度,纬度」（FR-104 的默认档 15 分钟从这里出发）。"""

LAZY_USAGE_KEYS: tuple[str, ...] = (
    "polyline_calls",
    "polyline_limit",
    "polyline_rejected",
    "budget_stops",
    "batches_sent",
)
"""🔴 `[BUG][M2]` 的验收清单：`POST /api/isochrone` 的 `usage` 必须出口这五个键。

前三个是 FR-203 的懒加载额度（本次用了 12/12、被拒几次），第四个是 FR-206 ① 的预算截停次数，
第五个是 FR-205 的 ≤50 批次数。键名**逐字取自** `services/route_probe.py:RouteProbe.usage()`
（除 `batches_sent` 由 `ProbeTally` 自己出口），本文件不许出现第四种叫法。
"""

EARTH_R = 6_378_137.0
OFFICIAL_BLOCK_BAND = (82.0, 98.0)
TIMEOUT_BLOCK_BAND = (262.0, 278.0)
BLOCK_FROM_M = 400.0


# --------------------------------------------------------------------------- 距离/方位
def _latlng(value: Any) -> tuple[float, float]:
    """把 `{"lat","lng"}` / `"lat,lng"` / `(lat, lng)` 三种写法统一成 `(lat, lng)`。"""
    if isinstance(value, Mapping):
        return float(value["lat"]), float(value["lng"])
    if isinstance(value, str):
        first, second = (part.strip() for part in value.split(","))
        return float(first), float(second)
    return float(value[0]), float(value[1])


def _great_circle(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    central = 2 * math.asin(
        math.sqrt(
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
        )
    )
    return EARTH_R * central


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dx = math.sin(lng2 - lng1) * math.cos(lat2)
    dy = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lng2 - lng1)
    return math.degrees(math.atan2(dx, dy)) % 360.0


# --------------------------------------------------------------------------- 假 client
class FakeRouteMatrixClient:
    """零出网的假 `BaiduClient`：只实现接线需要的四个方法，距离→耗时用一个各向异性模型。

    为什么要「各向异性」（同距离不同方向耗时差很多）：等时圈的不规则性来自速度场，假数据若
    各方向同速，二分会在一轮内齐刷刷收敛到同一个半径，正好把 PRD 想防的「正圆」演出来。

    两套调用计数分开记，因为「缓存命中」在真实链路里是**客户端被调了但没出网**：
    `matrix_calls` / `direction_calls` 数客户端被叫到几次（含命中），
    `live_matrix_calls` / `live_direction_calls` 数真的产生往返的几次——FR-402 的命中率验收断言后者。
    """

    def __init__(
        self,
        *,
        speed_mps: float = 1.10,
        field: str = "smooth",
        block_official: bool = False,
        block_timeout: bool = False,
        budget: Mapping[str, Any] | None = None,
        cache: bool = True,
    ) -> None:
        """`speed_mps=1.10`（≈4.0 km/h）与上面的绕行系数是配套选的：

        几何层的粗扫落在**理论半径**（步行 3.9 km/h × 目标时长）上，容差只有 ±30 s。假数据
        若各方向的实际均速正好等于理论值，12 个方向第一轮全部当场定案，只剩 40 来条探测——
        那是在给算法放水，PRD §4.2 的 72~96 条口径根本不会被触发。真实路网总是比理论直线慢
        （绕行、门禁、围墙），所以这里让「直线速度 × 绕行系数」普遍低于理论值，二分才跑得满。
        """
        self.speed_mps = float(speed_mps)
        self.field = field
        self.block_official = block_official
        self.block_timeout = block_timeout
        self.budget = dict(budget or {})
        self.memoize = cache
        self.matrix_calls: list[dict[str, Any]] = []
        self.direction_calls: list[dict[str, Any]] = []
        self.budget_queries: list[tuple[str | None, int]] = []
        self.live_matrix_calls = 0
        self.live_direction_calls = 0
        self._memo: dict[tuple[Any, ...], RouteMatrixResult] = {}
        self._direction_memo: dict[tuple[Any, ...], BaiduResult] = {}

    # -- 记账口径（逐字照抄 infra/baidu_client.py 的三栏语义） --------------------
    def _impedance(self, bearing: float) -> float:
        """绕行系数场（**恒 >1**）：路网实际行驶距离总比直线推点长。

        为什么要刻意保证 >1.17：几何层的粗扫落在理论半径上、容差只有 ±30 s。若假数据允许
        「比理论值还快」的方向存在，那些方向在第一轮就命中容差、当场定案，二分只跑两三轮就
        收工，实测会掉到 40~50 条探测——那样 §4.2 的 72~96 条口径根本没被走到（放水）。
        """
        theta = math.radians(bearing)
        return 1.65 + 0.26 * math.sin(3 * theta + 0.7) + 0.22 * math.cos(theta + 1.2)

    def _cell_status(self, bearing: float, radial: float) -> tuple[str, int] | None:
        in_official = OFFICIAL_BLOCK_BAND[0] <= bearing <= OFFICIAL_BLOCK_BAND[1]
        in_timeout = TIMEOUT_BLOCK_BAND[0] <= bearing <= TIMEOUT_BLOCK_BAND[1]
        if self.block_official and in_official and radial >= BLOCK_FROM_M:
            return ("official", 2001)  # 官方硬信号：无骑行/步行路线
        if self.block_timeout and in_timeout and radial >= BLOCK_FROM_M:
            return ("timeout", 0)  # 空结果/缺格：官方没说这条路不通
        return None

    def _wall_m(self, bearing: float) -> float:
        """`gated` 场里围墙所在的半径（520~760 m，按方向定）。"""
        return 640.0 + 120.0 * math.sin(math.radians(bearing) * 2.0 + 0.4)

    def _path_len_m(self, bearing: float, radial: float) -> float:
        """实际行驶距离（米）：平滑场按绕行系数放大，`gated` 场翻墙再多走一段。"""
        if self.field == "gated":
            return radial * 1.05 + (420.0 if radial >= self._wall_m(bearing) else 0.0)
        return radial * self._impedance(bearing)

    def _duration_s(self, bearing: float, radial: float) -> float:
        """耗时（秒）。

        `field="gated"` 是一个**有围墙的超级街区**：围墙内畅通（1.25 m/s），围墙外只有远端
        一个出口，于是耗时随半径呈台阶跳变（跳幅 560 s，远大于 ±30 s 容差）。这不是编出来
        难看的——它正是 §4.2「一轮约 72~96 条探测」的成立条件：只有当各方向都无法靠容差提前
        定案时，12 个方向才会把 5 轮二分跑满（72 条），自适应加密再往上补（≤96）。平滑场里
        半数方向两三轮就收敛，实测会掉到 50 出头，所以「72~96」是**预算上界**而非常数；
        两栏的比例（配额 ≈ 12× HTTP 次数）在两种场里都成立，另有一条测试专门钉它。
        """
        if self.field == "gated":
            wall = self._wall_m(bearing)
            inner = wall / 1.25
            if radial < wall:
                return radial / 1.25
            return inner + 560.0 + (radial - wall) / 1.25
        return self._path_len_m(bearing, radial) / self.speed_mps

    async def route_matrix(
        self, mode: str, origins: Any, destinations: Any, **kwargs: Any
    ) -> RouteMatrixResult:
        origin_list = list(origins)
        dest_list = list(destinations)
        origin = _latlng(origin_list[0])
        self.matrix_calls.append({"mode": mode, "origins": origin_list, "destinations": dest_list})
        key = (mode, str(origin_list), tuple(str(item) for item in dest_list))
        if self.memoize and key in self._memo:
            # 命中缓存：不出网 → 三栏全 0，`from_cache=True`（与真实客户端同一口径）
            cached = self._memo[key]
            hit = RouteMatrixResult(
                service=cached.service,
                ok=cached.ok,
                status=cached.status,
                data=dict(cached.data or {}),
                attempts=0,
                http_requests=0,
                quota_units=0,
                route_lines=0,
                place_calls=0,
                mode=mode,
                from_cache=True,
                origins=cached.origins,
                destinations=cached.destinations,
                chunks_sent=cached.chunks_sent,
                requested_cells=cached.requested_cells,
                cells=[list(row) for row in cached.cells],
            )
            return hit

        cells: list[dict[str, Any] | None] = []
        for dest in dest_list:
            position = _latlng(dest)
            radial = _great_circle(origin, position)
            bearing = _bearing(origin, position)
            kind = self._cell_status(bearing, radial)
            if kind is not None and kind[0] == "timeout":
                cells.append(None)  # 官方这一格没回数据
                continue
            cell: dict[str, Any] = {
                "status": kind[1] if kind else 0,
                "duration": {"value": round(self._duration_s(bearing, radial))},
                "distance": {"value": round(self._path_len_m(bearing, radial))},  # 实际绕行 > 直线
            }
            cells.append(cell)

        self.live_matrix_calls += 1
        filled = [cell for cell in cells if cell is not None]
        out = RouteMatrixResult(
            service="routematrix",
            ok=bool(filled),
            status=0,
            data={"result": filled},
            attempts=1,
            http_requests=1,
            quota_units=len(dest_list),  # 配额口径：1 起点 × N 终点
            route_lines=len(dest_list),
            place_calls=0,
            mode=mode,
            from_cache=False,
            partial=len(filled) < len(dest_list),
            origins=1,
            destinations=len(dest_list),
            chunks_sent=1,
            requested_cells=len(dest_list),
            cells=[cells],
        )
        if self.memoize:
            self._memo[key] = out
        return out

    async def direction(
        self, mode: str, origin: Any, destination: Any, **kwargs: Any
    ) -> BaiduResult:
        self.direction_calls.append({"mode": mode, "origin": origin, "destination": destination})
        a = _latlng(origin)
        b = _latlng(destination)
        radial = _great_circle(a, b)
        bearing = _bearing(a, b)
        key = (mode, str(a), str(b))
        if self.memoize and key in self._direction_memo:
            cached = self._direction_memo[key]
            return BaiduResult(
                service="direction",
                ok=cached.ok,
                status=cached.status,
                data=dict(cached.data or {}),
                attempts=0,
                http_requests=0,
                quota_units=0,
                route_lines=0,
                place_calls=0,
                mode=mode,
                from_cache=True,
            )
        steps: list[dict[str, Any]] = []
        self.live_direction_calls += 1
        for fraction in (0.0, 0.34, 0.67, 1.0):
            lat = a[0] + (b[0] - a[0]) * fraction
            lng = a[1] + (b[1] - a[1]) * fraction + 0.0006 * math.sin(math.pi * fraction)
            steps.append({"path": f"{lng:.6f},{lat:.6f}", "distance": round(radial / 4)})
        duration = round(self._duration_s(bearing, radial))
        route = {
            "duration": duration,
            "distance": round(self._path_len_m(bearing, radial)),
            "steps": steps,
        }
        out = BaiduResult(
            service="direction",
            ok=True,
            status=0,
            data={"status": 0, "result": {"routes": [route]}},
            attempts=1,
            http_requests=1,
            quota_units=1,
            route_lines=1,
            place_calls=0,
            mode=mode,
        )
        if self.memoize:
            self._direction_memo[key] = out
        return out

    def route_budget_state(self, mode: Any = None, *, next_lines: int = 0) -> dict[str, Any]:
        self.budget_queries.append((mode, int(next_lines)))
        if self.budget:
            return dict(self.budget)
        return {
            "mode": mode,
            "budget": 5000,
            "used": 0,
            "remaining": 5000,
            "fits_next_chunk": True,
        }

    def usage(self) -> dict[str, int]:
        return {
            "http_requests": sum(1 for _ in self.matrix_calls)
            + sum(1 for _ in self.direction_calls),
            "quota_units": sum(len(call["destinations"]) for call in self.matrix_calls),
            "place_calls": 0,
        }


# --------------------------------------------------------------------------- 夹具
def make_service(client: Any | None = None, **kwargs: Any) -> IsochroneService:
    return IsochroneService(
        client=client if client is not None else FakeRouteMatrixClient(), **kwargs
    )


def make_client(service: IsochroneService) -> TestClient:
    """真实 `api/app.py` 的 app（异常处理器在里面），只把服务换成注入的假对象。

    用 `TestClient(app)` 而不是 `with TestClient(app)`：不跑 lifespan，启动探测不会去碰
    `BaiduClient`，本文件从头到尾 0 次真实出网。
    """
    app.state.isochrone_service = service
    return TestClient(app)


def post(client: TestClient, payload: dict[str, Any], path: str = "/api/isochrone") -> Any:
    return client.post(path, json=payload)


def ring_distances(body: Mapping[str, Any]) -> list[float]:
    return [float(item["distance_m"]) for item in body["vertices"]]


def circle_area(radius_m: float) -> float:
    return math.pi * radius_m**2 / 1_000_000.0


# --------------------------------------------------------------------------- ① 示例层
def test_mock_isochrone_is_irregular_closed_and_sends_zero_requests() -> None:
    """FR-201 + D9/FR-412：mock 的圈必须不规则、0 出网，而且明显不是「同半径正圆」。"""
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "mock"},
    ).json()

    assert body["ok"] is True and body["data_mode"] == "mock" and body["data_source"] == "sample"
    assert body["degraded"] is True and body["message"]
    assert body["error"] is None and body["request_id"]
    # 🔴 零出网：假 client 一次都没被叫到，用量三栏都是 0
    assert client.matrix_calls == [] and client.direction_calls == []
    usage = body["usage"]
    assert (usage["http_requests"], usage["quota_units"], usage["place_calls"]) == (0, 0, 0)
    # 🔴 [BUG][M2] 的复现路径就是这条命令：示例层同样要出口懒加载额度与批次数，
    # 否则前端与审计都读不到「本次用了 12/12、被拒几次、切了几批」。
    assert set(LAZY_USAGE_KEYS) <= set(usage), f"usage 缺键：{set(LAZY_USAGE_KEYS) - set(usage)}"
    assert (usage["polyline_calls"], usage["polyline_limit"], usage["polyline_rejected"]) == (
        0,
        POLYLINE_CALL_LIMIT,
        0,
    )
    assert usage["batches_sent"] == 0 and usage["budget_stops"] == 0

    polygon = body["polygon"]
    assert polygon["type"] == "Polygon"
    ring = polygon["coordinates"][0]
    assert ring[0] == ring[-1], "环必须闭合（GeoJSON Polygon 的硬性要求）"
    assert len(set(map(tuple, ring))) > 12, "顶点数应明显多于 12 个粗扫方向（15° 加密生效）"

    distances = ring_distances(body)
    mean = sum(distances) / len(distances)
    spread = max(distances) - min(distances)
    variance = sum((item - mean) ** 2 for item in distances) / len(distances)
    assert variance > 0, "顶点到中心距离方差必须 >0 —— 正圆意味着示例层在糊弄"
    assert spread / mean > 0.25, f"最远/最近顶点差应超过平均半径的 25%，实测 {spread / mean:.2f}"
    assert max(distances) / min(distances) > 1.25

    # 「明显不是同半径圆」用等周商判：正圆取到最大值 1/(4π)≈0.0796，有阻隔齿的形状必然更低。
    # 面积对径向方差远不如周长敏感（正负偏差会互相抵消），拿面积差判不规则会在正圆边缘误判。
    ring_m = [(point[0], point[1]) for point in ring]
    perimeter = sum(
        _great_circle(ring_m[index], ring_m[index + 1]) for index in range(len(ring_m) - 1)
    )
    area_m2 = float(body["area_km2"]) * 1_000_000.0
    assert (
        area_m2 / perimeter**2 < 0.072
    ), f"等周商 {area_m2 / perimeter**2:.4f} 接近正圆的 0.0796，形状可疑"
    assert circle_area(min(distances)) < body["area_km2"] < circle_area(max(distances))

    bearings = [float(item["bearing"]) for item in body["vertices"]]
    assert bearings == sorted(bearings), "顶点必须按方位角升序（成环前不排序会自交）"
    kinds = {str(item["sample"]) for item in body["vertices"]}
    assert len(kinds) >= 2, "至少要出现两种采样标签"
    assert "blocked_timeout" in kinds, "阻隔扇区要长出来（示例层的『路走不通』证据）"
    assert "blocked_official" not in kinds, "合成数据不许冒充官方硬信号（FR-412）"
    assert all(item["confidence"] is not None for item in body["vertices"])
    blocked = [item for item in body["vertices"] if item["blocked"]]
    assert blocked and all("[synthetic]" in str(item["evidence"]) for item in blocked)

    field_obj = body["duration_field"]
    assert field_obj["cells"] and field_obj["step_m"] > 0
    assert all(len(cell["lnglat"]) == 2 for cell in field_obj["cells"])
    assert {str(cell["sample"]) for cell in field_obj["cells"]} <= {"measured", "interpolated"}


@pytest.mark.parametrize("minutes", [5, 15, 30])
def test_mock_layer_is_deterministic_for_the_same_center(minutes: int) -> None:
    """FR-301：同一输入两次输出恒定（种子来自坐标而不是进程状态，演示预案 B 可复算）。"""
    http = make_client(make_service())
    payload = {"lnglat": CENTER, "mode": "walking", "minutes": minutes, "data_mode": "mock"}
    first = http.post("/api/isochrone", json=payload)
    second = http.post("/api/isochrone", json=payload)
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert ring_distances(a) == ring_distances(b)
    assert [item["lnglat"] for item in a["vertices"]] == [item["lnglat"] for item in b["vertices"]]


# --------------------------------------------------------------------------- ②③ 用量两栏
def test_live_round_costs_72_to_96_quota_units_but_only_5_to_8_http_requests() -> None:
    """PRD §4.2 第 267 行的验收口径：两栏数字必须都真断言，不能只报 HTTP 次数。

    用 `field="gated"`（围墙街区）跑**满预算**：12 个方向都无法靠 ±30 s 容差提前定案，
    于是 5 轮二分全部跑满 = 72 条，自适应加密再补若干 → 应该正好落进 72~96。
    """
    client = FakeRouteMatrixClient(field="gated")
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    ).json()

    assert body["data_mode"] == "live" and body["data_source"] == "live"
    usage = body["usage"]
    assert 72 <= usage["quota_units"] <= 96, f"配额单位应落在 72~96，实测 {usage['quota_units']}"
    assert 5 <= usage["http_requests"] <= 8, f"HTTP 往返应落在 5~8，实测 {usage['http_requests']}"
    assert usage["quota_units"] != usage["http_requests"], "两栏永远不许合并成一个总数"
    assert usage["place_calls"] == 0
    assert usage["route_lines"] == {
        "walking": usage["quota_units"],
        "riding": 0,
    }, "route_lines 按 mode 分栏；walking 一轮体检不该记到 riding 头上"

    # ③ ≤50 切分真的生效：官方单批上限 50 行，且每次调用都是 1 起点
    assert client.matrix_calls, "live 路径必须真的调用过批量算路"
    assert all(len(call["destinations"]) <= 50 for call in client.matrix_calls)
    assert all(len(call["origins"]) == 1 for call in client.matrix_calls)
    assert sum(len(call["destinations"]) for call in client.matrix_calls) == usage["quota_units"]

    # 几何结论
    assert body["polygon"]["type"] == "Polygon"
    assert body["area_km2"] > 0
    assert body["stats"]["rounds"] == 5, "满预算场景应当把 5 轮二分跑满"


def test_batching_keeps_http_requests_an_order_of_magnitude_below_quota() -> None:
    """平滑场（一半方向提前收敛）里也要成立：重点是**两栏的比例**，不是绝对条数。

    单独一条测试钉比例，是因为 §4.2 的 72~96 是「跑满预算」的上界口径；真实路网里不少
    方向两三轮就进了 ±30 s 容差，探测数会落到 50 出头。只断言绝对值会把这个算法事实
    藏起来，所以这里同时断言 `quota_units ≥ 6 × http_requests`（打包前后的倍数关系）。
    """
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    ).json()

    usage = body["usage"]
    assert usage["http_requests"] <= 8, "打包后不该出现「一个方向一次请求」的形态"
    assert usage["quota_units"] >= 6 * usage["http_requests"]
    assert usage["quota_units"] <= 50 * usage["http_requests"], "每批 ≤50 行的上界"
    assert any(
        str(item["sample"]) == "measured" for item in body["vertices"]
    ), "容差内收敛的方向要留下 measured 顶点（否则精度档位全丢了）"
    assert usage["measured_cells"] > 0


def test_riding_mode_uses_riding_bucket_and_counts_its_own_column() -> None:
    client = FakeRouteMatrixClient(speed_mps=3.6)
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "riding", "minutes": 15, "data_mode": "live"},
    ).json()

    assert body["mode"] == "riding"
    assert {call["mode"] for call in client.matrix_calls} == {"riding"}
    usage = body["usage"]
    assert usage["route_lines"] == {"walking": 0, "riding": usage["quota_units"]}
    assert body["vertices"] and body["area_km2"] > 0


# --------------------------------------------------------------------------- ④ 遇阻两档
def test_blocked_official_and_blocked_timeout_stay_two_separate_labels() -> None:
    """§4.2：「路走不通」与「我没测准」语义不同，两个标签不许合并、也不许互相推断。"""
    client = FakeRouteMatrixClient(block_official=True, block_timeout=True)
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    ).json()

    kinds = [str(item["sample"]) for item in body["vertices"]]
    assert "blocked_official" in kinds, f"官方 2001 的方向要成档，实测标签 {sorted(set(kinds))}"
    assert "blocked_timeout" in kinds, "空结果/缺格的方向要成档"
    official = [item for item in body["vertices"] if item["sample"] == "blocked_official"]
    timeout = [item for item in body["vertices"] if item["sample"] == "blocked_timeout"]
    assert all("2001" in str(item["evidence"]) for item in official), "官方证据要原样透出"
    assert all(
        "2001" not in str(item["evidence"]) for item in timeout
    ), "timeout 档不许冒充拿到了官方硬信号"
    assert all(item["blocked"] is True for item in official + timeout)
    usage = body["usage"]
    # 探测级的遇阻格数 ≥ 顶点级的遇阻档数：某方向二分中途遇阻、后来又在内侧测通，
    # 顶点就回到 measured/interpolated，但那次官方硬信号仍然要如实记账。
    assert usage["blocked_official_cells"] >= len(official) >= 1
    assert usage["blocked_timeout_cells"] >= len(timeout) >= 1
    assert usage["measured_cells"] > 0
    # 阻隔顶点受 DP 保护：两个方向的顶点都还在环上（PRD 自验第 2 条）
    ring = body["polygon"]["coordinates"][0]
    for item in official + timeout:
        assert list(item["lnglat"]) in [list(point) for point in ring]


# --------------------------------------------------------------------------- ⑤ 入参 400
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"lnglat": CENTER, "mode": "driving", "minutes": 15}, "mode"),
        ({"lnglat": CENTER, "mode": "walking", "minutes": 999}, "minutes"),
        ({"lnglat": CENTER, "mode": "walking", "minutes": 1}, "minutes"),
        ({"lnglat": CENTER, "mode": "walking", "minutes": "abc"}, "minutes"),
        ({"lnglat": "116.404", "mode": "walking", "minutes": 15}, "lnglat"),
        ({"mode": "walking", "minutes": 15}, "lnglat"),
        (
            {"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "sideways"},
            "data_mode",
        ),
    ],
)
def test_bad_input_returns_400_with_the_shared_error_envelope(payload: dict, field: str) -> None:
    http = make_client(make_service())
    response = http.post("/api/isochrone", json=payload)

    assert response.status_code == 400, "边界错误是 400，不是 422/500"
    body = response.json()
    assert set(body) == set(
        error_payload(code="x", category="y", message="z")
    ), "错误信封与 api/app.py 的共用信封逐字段一致（FR-404）"
    error = body["error"]
    assert set(error) == {
        "code",
        "category",
        "message",
        "retryable",
        "degrade_to_sample",
        "breaker",
        "detail",
    }
    assert field in str(error["message"]) or field in str(error["detail"]), "得点名是哪个字段"
    assert error["retryable"] is False and body["request_id"]


def test_foreign_center_is_rejected_before_any_egress() -> None:
    """境外坐标：个人档境外无配额，探测只白烧当日额度 → 必须在出网前挡下。"""
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    response = http.post(
        "/api/isochrone", json={"lnglat": "2.3522,48.8566", "mode": "walking", "minutes": 15}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert "境外" in str(error["message"]) or error["code"]
    assert client.matrix_calls == [] and client.direction_calls == []


# --------------------------------------------------------------------------- ⑥ 降级不 5xx
def test_budget_exhaustion_returns_200_with_readable_copy_not_500() -> None:
    """FR-206：额度打满不是错误，是「今天到此为止」——200 + degraded + 文案。"""
    client = FakeRouteMatrixClient(
        budget={"budget": 5000, "used": 4990, "remaining": 10, "fits_next_chunk": False}
    )
    http = make_client(make_service(client))
    response = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    )

    assert response.status_code == 200, "预算截停绝不能变成 5xx"
    body = response.json()
    assert body["ok"] is True and body["degraded"] is True
    assert body["degraded_reason"] in {"budget_stopped", "no_live_data"}
    assert body["message"]
    assert client.matrix_calls == [], "守卫不足时这一批根本不该发出去"
    # 🔴 [BUG][M2]：截停次数以前只体现在文案字符串里，现在 `usage.budget_stops` 数得出来
    # （键名与 `RouteProbe.usage()` 一致），审计不必再从 `degraded_reason` 反推。
    assert body["usage"]["budget_stops"] >= 1
    assert body["usage"]["batches_sent"] == 0, "被守卫拦下的批不计入已发出的批次"


def test_total_live_failure_degrades_instead_of_raising() -> None:
    """批量算路全挂（网络抖动）：落回示例层并说明原因，不抛 500（FR-403）。"""
    client = FakeRouteMatrixClient()

    async def always_raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network down")

    client.route_matrix = always_raise  # type: ignore[method-assign]
    http = make_client(make_service(client))
    response = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["degraded"] is True
    assert body["data_source"] == "sample" and body["message"]


# --------------------------------------------------------------------------- ⑦ 缓存命中
def test_second_identical_call_hits_cache_and_lowers_http_requests() -> None:
    """FR-402 的命中率口径：命中缓存时 `http_requests=0`、`cache_hits>0`、`data_source=cache`。"""
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    payload = {"lnglat": CENTER, "mode": "walking", "minutes": 10, "data_mode": "live"}

    first = http.post("/api/isochrone", json=payload).json()
    live_after_first = client.live_matrix_calls
    second = http.post("/api/isochrone", json=payload).json()

    assert first["usage"]["http_requests"] > 0 and first["data_source"] == "live"
    assert second["usage"]["http_requests"] < first["usage"]["http_requests"]
    assert second["usage"]["http_requests"] == 0, "同参数第二次应当一次网都不出"
    assert second["usage"]["cache_hits"] > 0
    assert second["usage"]["quota_units"] == 0, "缓存不消耗百度配额，记了是假账"
    assert second["data_source"] == "cache"
    assert client.live_matrix_calls == live_after_first, "第二次没有新增对百度的真实调用"
    assert ring_distances(first) == ring_distances(second), "同一份缓存数据必须长出同一个圈"


# --------------------------------------------------------------------------- ⑧ polyline
def test_polyline_lazy_fetch_hard_cap_is_twelve_calls() -> None:
    """FR-203 / D8：只有点顶点才补调 direction，单次分析硬上限 12 次，超限回可读对象。"""
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    ).json()
    vertices = body["vertices"]

    calls = 0
    over_limit: dict[str, Any] | None = None
    for vertex in vertices:
        response = http.post(
            "/api/isochrone/polyline",
            json={
                "lnglat": CENTER,
                "mode": "walking",
                "minutes": 15,
                "bearing": vertex["bearing"],
                "distance_m": vertex["distance_m"],
                "data_mode": "live",
            },
        )
        assert response.status_code == 200, "超限也不能变 5xx：这是「请少点几下」而不是故障"
        payload = response.json()
        if payload["allowed"]:
            calls += 1
        else:
            over_limit = payload
            break

    assert calls == 12, f"允许的 direction 调用应恰好 12 次，实测 {calls}"
    assert len(client.direction_calls) == 12, "第 13 次不许真的打到百度"
    assert over_limit is not None and over_limit["ok"] is False
    assert over_limit["message"] and over_limit["path"] is None
    assert over_limit["calls"] == {"used": 12, "limit": 12, "remaining": 0}

    # 成功那批的形状：GeoJSON LineString，坐标一律 [经度, 纬度]
    first = http.post(
        "/api/isochrone/polyline",
        json={
            "lnglat": CENTER,
            "mode": "walking",
            "minutes": 15,
            "point": vertices[0]["lnglat"],
            "data_mode": "live",
        },
    )
    assert first.status_code == 200
    assert len(client.direction_calls) == 12, "同一个点复用已取到的路径，不再出网"


def test_polyline_returns_linestring_and_counts_usage() -> None:
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    ).json()
    vertex = body["vertices"][3]
    response = http.post(
        "/api/isochrone/polyline",
        json={
            "lnglat": CENTER,
            "mode": "walking",
            "minutes": 15,
            "bearing": vertex["bearing"],
            "distance_m": vertex["distance_m"],
            "data_mode": "live",
        },
    )
    payload = response.json()
    assert payload["ok"] is True and payload["allowed"] is True
    assert payload["path"]["type"] == "LineString"
    assert all(len(point) == 2 for point in payload["path"]["coordinates"])
    assert payload["duration_s"] and payload["distance_m"]
    assert payload["usage"]["http_requests"] >= 1
    assert payload["usage"]["place_calls"] == 0
    assert payload["request_id"]


@pytest.mark.parametrize("path", ["/api/isochrone/polyline"])
def test_polyline_bad_input_shapes_match_the_shared_envelope(path: str) -> None:
    http = make_client(make_service())
    for payload in (
        {"lnglat": CENTER, "mode": "sailing", "minutes": 15, "bearing": 0, "distance_m": 500},
        {"lnglat": CENTER, "mode": "walking", "minutes": 15},  # 既没 point 也没 bearing+distance
        {"lnglat": CENTER, "mode": "walking", "minutes": 15, "bearing": 10},  # 缺 distance_m
        {"lnglat": CENTER, "mode": "walking", "minutes": 15, "bearing": 10, "distance_m": 999999},
    ):
        response = http.post(path, json=payload)
        assert response.status_code == 400
        assert set(response.json()) == set(error_payload(code="x", category="y", message="z"))


def test_polyline_in_mock_mode_never_touches_the_network() -> None:
    """示例层的 polyline 也必须 0 出网（演练/离线演示时顶点是可点的）。"""
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    response = http.post(
        "/api/isochrone/polyline",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "bearing": 45, "distance_m": 600},
        headers={},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data_source"] in {"live", "sample"}

    mock = http.post(
        "/api/isochrone/polyline",
        json={
            "lnglat": CENTER,
            "mode": "walking",
            "minutes": 15,
            "bearing": 45,
            "distance_m": 600,
            "data_mode": "mock",
        },
    )
    assert mock.status_code == 200
    payload = mock.json()
    assert payload["data_source"] == "sample" and payload["path"]["type"] == "LineString"
    assert payload["usage"]["http_requests"] == 0
    assert client.direction_calls == []
    # 🔴 [BUG][M2]：示例层也走同一份出口，且 `usage.polyline_calls` 与同响应 `calls.used`
    # 同源同值——前端计量表只读这一处，不留「两套数各说各话」的分叉。
    assert set(LAZY_USAGE_KEYS) <= set(payload["usage"])
    # 上面那条不带 `data_mode` 的请求同样落在示例层，所以这里是「会话已用 ≥2」而不是恰好 1
    assert payload["usage"]["polyline_calls"] == payload["calls"]["used"] >= 2
    assert payload["usage"]["polyline_limit"] == payload["calls"]["limit"] == POLYLINE_CALL_LIMIT
    assert payload["usage"]["polyline_rejected"] == 0

    # 示例层吃的是同一条 12 次硬上限：第 13 次出口要写清「用了 12、被拒 1」
    over_limit: dict[str, Any] | None = None
    for step in range(2, POLYLINE_CALL_LIMIT + 2):
        refused = http.post(
            "/api/isochrone/polyline",
            json={
                "lnglat": CENTER,
                "mode": "walking",
                "minutes": 15,
                "bearing": step * 7,
                "distance_m": 600,
                "data_mode": "mock",
            },
        ).json()
        if not refused["allowed"]:
            over_limit = refused
            break
    assert over_limit is not None, "示例层不该绕过 12 次硬上限"
    assert over_limit["usage"]["polyline_calls"] == over_limit["calls"]["used"] == 12
    assert over_limit["usage"]["polyline_rejected"] == 1
    assert client.direction_calls == [], "整段仍然 0 次真实出网"


# ------------------------------------------- ⑩ [BUG][M2] usage 出口懒加载额度与批次数
def test_usage_exports_lazy_quota_and_batch_count() -> None:
    """FR-203/FR-205 的账要出得了口：`usage` 里的五个键、且键名与 `RouteProbe` 一致。

    原 bug：`RouteProbe.usage()` 算出了 `polyline_calls / polyline_limit / polyline_rejected /
    budget_stops`，`ProbeTally.batches_sent` 也一直在累加，但生产链路里没人读——
    于是 `POST /api/isochrone` 的 `usage` 只有 10 个键，审计看不到「本次用了 12/12、
    被拒几次、预算有没有截停、切了几批」。这条断言把门守住。
    """
    client = FakeRouteMatrixClient()
    http = make_client(make_service(client))
    payload = {"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"}
    body = http.post("/api/isochrone", json=payload).json()
    usage = body["usage"]

    assert set(LAZY_USAGE_KEYS) <= set(usage), f"usage 缺键：{set(LAZY_USAGE_KEYS) - set(usage)}"
    # 🔴 命名对齐：响应里的 polyline_* 必须**正好是** `RouteProbe.usage()` 的那三个键名，
    # 少一个是漏出口、多一个是造第三套命名（同一个数在 infra/metrics、RouteProbe、
    # 服务层三处已经各有一套叫法了）。
    probe_keys = set(RouteProbe(FakeRouteMatrixClient()).usage())
    assert {k for k in usage if k.startswith("polyline_")} == {
        k for k in probe_keys if k.startswith("polyline_")
    }
    assert "budget_stops" in probe_keys

    assert usage["polyline_limit"] == POLYLINE_CALL_LIMIT == 12
    assert (usage["polyline_calls"], usage["polyline_rejected"]) == (0, 0)
    assert usage["budget_stops"] == 0
    assert 1 <= usage["batches_sent"] <= usage["http_requests"], "批次数不可能大于真实往返数"

    # 点到硬上限：拒绝那一次的响应里，额度出口与 `calls` 必须是同一个数
    allowed = 0
    refusal: dict[str, Any] | None = None
    for vertex in body["vertices"]:
        result = http.post(
            "/api/isochrone/polyline",
            json={
                "lnglat": CENTER,
                "mode": "walking",
                "minutes": 15,
                "bearing": vertex["bearing"],
                "distance_m": vertex["distance_m"],
                "data_mode": "live",
            },
        ).json()
        if result["allowed"]:
            allowed += 1
        else:
            refusal = result
            break
    assert allowed == POLYLINE_CALL_LIMIT and refusal is not None
    assert refusal["usage"]["polyline_calls"] == refusal["calls"]["used"] == 12
    assert refusal["usage"]["polyline_limit"] == refusal["calls"]["limit"] == 12
    assert refusal["usage"]["polyline_rejected"] == 1

    # ⚠️ 口径：这三个 `polyline_*` 是**会话级**（一次分析 12 次），其余 `usage` 字段是请求级。
    # 重跑同参数分析命中缓存、0 出网，但已花掉的懒加载额度不会被清零。
    again = http.post("/api/isochrone", json=payload).json()
    assert again["usage"]["http_requests"] == 0
    assert again["usage"]["polyline_calls"] == 12
    assert again["usage"]["polyline_rejected"] == 1


# --------------------------------------------------------------------------- ⑨ 分轮进度
def test_progress_reports_every_bisect_round() -> None:
    """FR-205：进度要能画出「现在跑到第几轮、还剩几个方向」。"""
    http = make_client(make_service())
    body = http.post(
        "/api/isochrone",
        json={"lnglat": CENTER, "mode": "walking", "minutes": 15, "data_mode": "live"},
    ).json()

    progress = body["progress"]
    assert progress, "live 路径必须逐轮上报，不能等跑完才一次给"
    for item in progress:
        assert set(item) >= {
            "stage",
            "round_index",
            "probes",
            "pending_directions",
            "total_directions",
        }
        assert item["probes"] > 0 and item["total_directions"] > 0
    assert [int(item["round_index"]) for item in progress] == sorted(
        int(item["round_index"]) for item in progress
    )
    assert any(str(item["stage"]) == "coarse" for item in progress)
    assert int(progress[0]["round_index"]) == 0


def test_openapi_lists_both_isochrone_endpoints() -> None:
    """契约面：路由确实挂进了 app（ROUTERS 注册表），且是 POST。"""
    http = make_client(make_service())
    spec = http.get("/api/openapi.json").json()
    assert "/api/isochrone" in spec["paths"] and "/api/isochrone/polyline" in spec["paths"]
    assert "post" in spec["paths"]["/api/isochrone"]
