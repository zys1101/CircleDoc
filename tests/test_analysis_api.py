"""`api/analysis.py` 与 `api/poi.py` 的 HTTP 契约测试（PRD §4.7 / FR-404 / FR-205）。

为什么需要这一层：`services/` 的单测证明的是「算法算对了」，`test_samples_baseline.py`
证明的是「结果可复现」，但**接口形状**（状态码、错误信封、任务轮询、后台模式）只有走
`TestClient` 才验得到。开发过程中那两个真实缺陷（协程未 `await` 导致 500、
密集社区评分倒挂）都是在「把服务层真正接上 HTTP」这一步才暴露的 —— 本文件就是那道关口。

三条贯穿全篇的口径：

1. **零配置**：整份用例不注入任何 AK 与环境变量，`data_mode` 一律显式传 `mock`，
   所以既不依赖 `.env`，也保证零真实出网。
2. **FR-404 的错误形状全站一致**：错误响应固定为
   `{"error": {...}, "request_id": "..."}`，其中 `error` 含
   `code` / `category` / `message` / `retryable` / `degrade_to_sample` / `breaker` / `detail`
   七个字段；**业务范围错一律 400 且指名是哪个字段**，类型错走 422 但同样点名。
3. **降级不冒 5xx**：FR-403 要求任何一层失败都回 200 + `degraded` + 可读文案。
"""

from __future__ import annotations

import pytest
from api.analysis import _TASKS_MAX, reset_tasks
from fastapi.testclient import TestClient
from infra.config import get_settings
from services.samples import load_samples

from tests.helpers import async_test

ERROR_ENVELOPE_KEYS = {
    "code",
    "category",
    "message",
    "retryable",
    "degrade_to_sample",
    "breaker",
    "detail",
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_tasks():
    """每个用例前清空任务表：否则跑满 32 条后旧的 `task_id` 会被挤掉，用例互相干扰。"""
    reset_tasks()
    yield
    reset_tasks()


def assert_error_envelope(body: dict, *, code: str | None = None) -> dict:
    assert "error" in body, f"缺少 error 信封：{list(body)}"
    error = body["error"]
    assert set(error) >= ERROR_ENVELOPE_KEYS, f"错误信封字段不全：{sorted(error)}"
    assert "request_id" in body, "错误响应必须带 request_id（便于对日志）"
    if code is not None:
        assert error["code"] == code, f"错误码 {error['code']} != {code}"
    return error


# ======================================================================================
# POST /api/analysis —— 主接口
# ======================================================================================
def test_analysis_returns_a_full_report_and_a_task_id(client: TestClient) -> None:
    response = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "minutes": 15, "data_mode": "mock"}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # §4.7 报告对象的六段 + 三个追加字段
    for key in ("meta", "isochrone", "coverage", "blind_spots", "score", "summary", "usage"):
        assert key in body, f"报告缺少 {key} 段"
    assert body["task_id"] and body["request_id"]

    # FR-301：分数在 0~100，且等级是四档之一
    assert 0.0 <= body["score"]["total"] <= 100.0
    assert body["score"]["grade"] in {"A", "B", "C", "D"}
    # FR-302：雷达只含计分维度（便民参考项不进雷达）
    assert "convenience" not in body["score"]["radar"]
    assert len(body["score"]["radar"]) == 6
    # FR-304：3~5 条结论
    assert 3 <= len(body["summary"]) <= 5


def test_analysis_reports_synthetic_provenance_in_mock_mode(client: TestClient) -> None:
    body = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()
    # FR-306：徽标值域是 live|cache|sample，不是 mock
    assert body["meta"]["data_source"] == "sample"
    assert body["meta"]["degraded_reason"]
    assert "百度地图开放平台" in body["meta"]["source_declaration"]
    # 合成层不出网，三栏就该如实为 0
    assert body["usage"]["place_calls"] == 0
    assert body["usage"]["http_requests"] == 0
    assert body["usage"]["quota_units"] == 0


def test_analysis_budget_assertions_are_self_consistent(client: TestClient) -> None:
    body = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()
    assertions = body["assertions"]
    assert assertions["limits"]["place_calls"] == 8
    assert assertions["limits"]["http_requests"] == 20
    for key, check in assertions["checks"].items():
        assert check["ok"] is (check["value"] <= check["limit"]), key


def test_analysis_usage_keeps_the_two_columns_apart(client: TestClient) -> None:
    """🔴 `quota_units` 与 `http_requests` 是两个数，必须各占一个键（PRD §2.2）。"""
    usage = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()["usage"]
    assert "quota_units" in usage and "http_requests" in usage and "place_calls" in usage
    # `route_lines` 按 mode 分栏（FR-206）
    assert set(usage["route_lines"]) >= {"walking", "riding"}


def test_dual_mode_widens_the_quota_budget_to_220(client: TestClient) -> None:
    body = client.post(
        "/api/analysis",
        json={"lnglat": "116.404,39.915", "modes": ["walking", "riding"], "data_mode": "mock"},
    ).json()
    assert body["meta"]["modes"] == ["walking", "riding"]
    assert set(body["isochrones"]) == {"walking", "riding"}
    # PRD §6.1：双模式配额上限放宽到 220
    assert body["assertions"]["limits"]["quota_units"] == 220
    # 单模式是 120
    single = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()
    assert single["assertions"]["limits"]["quota_units"] == 120


def test_pois_are_searched_once_even_in_dual_mode(client: TestClient) -> None:
    """FR-206 / D5：骑行圈复用同一份 POI 集合，`place_calls` 不得翻倍。"""
    single = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()
    dual = client.post(
        "/api/analysis",
        json={"lnglat": "116.404,39.915", "modes": ["walking", "riding"], "data_mode": "mock"},
    ).json()
    assert single["usage"]["place_calls"] == dual["usage"]["place_calls"]
    assert single["coverage"]["meta"]["k_effective"] == 12


def test_mobile_platform_halves_k_and_uses_lightweight_profile(client: TestClient) -> None:
    """FR-107：移动端 K=6、跳过中点加密与耗时热力场。"""
    body = client.post(
        "/api/analysis",
        json={"lnglat": "116.404,39.915", "platform": "mobile", "data_mode": "mock"},
    ).json()
    assert body["meta"]["platform"] == "mobile"
    assert body["meta"]["lightweight"] is True
    assert body["meta"]["k"] == 6
    assert body["coverage"]["meta"]["k_effective"] == 6
    # 轻量档不建耗时场（`build_duration_field=False`）
    assert not body["isochrone"]["duration_field"].get("cells")


def test_sample_id_loads_the_community_centre_and_archetype(client: TestClient) -> None:
    """FR-102：选样例后自动采用该社区的中心点，并返回一份**完整**报告。

    ⚠️ 这里只断言**接线契约**，不断言具体分数。原来它写死 `grade == "A"`，
    那是一个**随数据来源变化**的产物，不是契约 —— 而且会让用例依赖缓存状态：
    `data_mode="mock"` 只表示「不出网」，**不表示「不读缓存」**
    （降级链是 `live → cache → sample`，缓存键里既没有 `archetype` 也没有 `data_mode`），
    所以只要这台机器跑过一次 live，同一个请求就会命中真实 POI、分数随之改变。
    见 `docs/TEST_REPORT.md` §2.7。

    「形态画像生效」这件事由 `tests/test_samples_baseline.py` 覆盖 ——
    那里的基线是在 `cache_enabled=False` 下生成的，缓存不会把画像盖住。
    """
    communities, _ = load_samples(get_settings())
    community = next(item for item in communities if item.archetype == "old_town")
    response = client.post("/api/analysis", json={"sample_id": community.id, "data_mode": "mock"})
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["center"] == pytest.approx(list(community.lnglat))
    # 报告必须是完整的，不是「选错 id 之后返回一份空壳」
    assert body["coverage"]["groups"], "样例请求返回了空的分维度结果"
    assert body["score"]["grade"] in {"A", "B", "C", "D"}
    assert 0.0 <= float(body["score"]["total"]) <= 100.0
    assert body["blind_spots"]["count"] >= 0
    assert body["summary"], "样例请求返回了空摘要"


def test_explicit_lnglat_wins_over_the_sample_centre(client: TestClient) -> None:
    communities, _ = load_samples(get_settings())
    body = client.post(
        "/api/analysis",
        json={"sample_id": communities[0].id, "lnglat": "116.30,40.00", "data_mode": "mock"},
    ).json()
    assert body["meta"]["center"] == pytest.approx([116.30, 40.00])


# ======================================================================================
# POST /api/analysis —— 入参错误（FR-404：400 + 指名字段，不是 422）
# ======================================================================================
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"lnglat": "116.404,39.915", "platform": "tablet", "data_mode": "mock"}, "platform"),
        ({"lnglat": "116.404,39.915", "minutes": "abc", "data_mode": "mock"}, "minutes"),
        ({"lnglat": "116.404,39.915", "radius": "wide", "data_mode": "mock"}, "radius"),
        ({"lnglat": "116.404,39.915", "data_mode": "realtime"}, "data_mode"),
    ],
)
def test_bad_analysis_params_return_400_with_the_offending_field(
    client: TestClient, payload: dict, field: str
) -> None:
    response = client.post("/api/analysis", json=payload)
    assert response.status_code == 400, response.text
    error = assert_error_envelope(response.json())
    assert error["detail"].get("field") == field, error["detail"]
    assert error["retryable"] is False
    assert error["degrade_to_sample"] is False


def test_unknown_sample_id_is_rejected_instead_of_silently_substituted(
    client: TestClient,
) -> None:
    """静默换一个样例会让「点了 A 出来 B 的结论」，是现场最难查的一类错。"""
    response = client.post(
        "/api/analysis", json={"sample_id": "no-such-place", "data_mode": "mock"}
    )
    assert response.status_code == 400
    error = assert_error_envelope(response.json(), code="unknown_sample")
    assert "no-such-place" in error["detail"]["sample_id"]
    assert error["detail"]["available"]


def test_invalid_coordinate_reports_the_lnglat_field(client: TestClient) -> None:
    response = client.post("/api/analysis", json={"lnglat": "not-a-point", "data_mode": "mock"})
    assert response.status_code == 400
    assert assert_error_envelope(response.json())["detail"]["field"] == "lnglat"


# ======================================================================================
# GET /api/analysis/{task_id} —— 轮询（FR-205）
# ======================================================================================
def test_task_can_be_polled_after_a_synchronous_run(client: TestClient) -> None:
    """同步路径返回的报告也留了任务记录，所以「先拿报告、再按 id 复查」是通的。"""
    report = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()
    response = client.get(f"/api/analysis/{report['task_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["partial"] is False
    assert body["report"]["score"]["total"] == report["score"]["total"]
    # 阶段表来自服务层的真实回调，不是前端编的进度条
    stages = [item.get("stage") for item in body["progress"]]
    assert "isochrone" in stages and "poi" in stages and "blind_spots" in stages


def test_task_polling_can_omit_the_report(client: TestClient) -> None:
    report = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()
    body = client.get(f"/api/analysis/{report['task_id']}?include_report=false").json()
    assert "report" not in body
    assert body["stages_done"] >= 4


def test_unknown_task_returns_404_with_a_readable_reason(client: TestClient) -> None:
    """任务过期要说「重新发起体检」，不能复用「这个接口还不存在」那条通用文案。"""
    response = client.get("/api/analysis/deadbeefdeadbeef")
    assert response.status_code == 404
    error = assert_error_envelope(response.json(), code="analysis_task_gone")
    assert "重新发起" in error["message"]
    assert error["detail"]["tasks_max"] == _TASKS_MAX


def test_background_mode_returns_202_and_a_poll_path(client: TestClient) -> None:
    response = client.post(
        "/api/analysis",
        json={"lnglat": "116.404,39.915", "data_mode": "mock", "background": True},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "done"
    assert body["poll"] == f"/api/analysis/{body['task_id']}"
    # 202 只回任务号，不回整份报告（前端据此决定要不要轮询）
    assert "coverage" not in body
    polled = client.get(body["poll"]).json()
    assert polled["status"] == "done" and "report" in polled


def test_task_registry_is_bounded(client: TestClient) -> None:
    """任务表是进度通道不是结果存储：超出上限丢最旧的，不能无限增长。"""
    first = client.post(
        "/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"}
    ).json()["task_id"]
    for _ in range(_TASKS_MAX + 2):
        client.post("/api/analysis", json={"lnglat": "116.404,39.915", "data_mode": "mock"})
    assert client.get(f"/api/analysis/{first}").status_code == 404


# ======================================================================================
# GET /api/samples —— 预置样例（FR-102）
# ======================================================================================
def test_samples_endpoint_lists_domestic_communities(client: TestClient) -> None:
    body = client.get("/api/samples").json()
    assert body["ok"] is True
    assert body["count"] >= 3
    assert body["coordinate_system"] == "bd09ll"
    for item in body["samples"]:
        assert item["city"] and item["district"] and len(item["lnglat"]) == 2
        assert item["archetype"] in {"old_town", "new_district", "transit_corridor"}
        assert isinstance(item["coord_verified"], bool)


def test_samples_endpoint_is_honest_about_unverified_coordinates(client: TestClient) -> None:
    """§10-1 规程②：坐标没实调核验就必须标 false，并且 notes 里要说清补法。"""
    body = client.get("/api/samples").json()
    unverified = [item for item in body["samples"] if not item["coord_verified"]]
    if unverified:
        assert any("coord_verified" in note for note in body["notes"])
        assert any("suggest" in note for note in body["notes"])


def test_samples_endpoint_does_not_touch_the_network(client: TestClient) -> None:
    response = client.get("/api/samples")
    assert response.headers["X-Http-Requests"] == "0"
    assert response.headers["X-Place-Calls"] == "0"


# ======================================================================================
# POST /api/poi —— 分类检索（M3）
# ======================================================================================
def test_poi_endpoint_returns_seven_dimension_groups(client: TestClient) -> None:
    response = client.post("/api/poi", json={"lnglat": "116.404,39.915", "data_mode": "mock"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["groups"]) == 7  # 六维度 + 便民参考项
    assert body["data_source"] == "sample"
    assert body["request_id"]
    keys = [group["key"] for group in body["groups"]]
    assert keys == ["medical", "education", "market", "leisure", "transit", "food", "service_ref"]


def test_poi_endpoint_can_be_narrowed_to_one_dimension(client: TestClient) -> None:
    body = client.post(
        "/api/poi",
        json={"lnglat": "116.404,39.915", "categories": ["medical"], "data_mode": "mock"},
    ).json()
    assert [group["key"] for group in body["groups"]] == ["medical"]


def test_poi_endpoint_rejects_unknown_dimension_with_400(client: TestClient) -> None:
    response = client.post(
        "/api/poi", json={"lnglat": "116.404,39.915", "categories": ["nope"], "data_mode": "mock"}
    )
    assert response.status_code == 400
    assert assert_error_envelope(response.json())["detail"]["field"] == "categories"


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"lnglat": "116.404,39.915", "mode": "driving", "data_mode": "mock"}, "mode"),
        ({"lnglat": "116.404,39.915", "radius": "wide", "data_mode": "mock"}, "radius"),
        ({"lnglat": "116.404,39.915", "data_mode": "now"}, "data_mode"),
    ],
)
def test_bad_poi_params_return_400_with_the_offending_field(
    client: TestClient, payload: dict, field: str
) -> None:
    """业务范围错（模式白名单 / 半径不可解析 / 枚举外）→ 400 + 指名字段。"""
    response = client.post("/api/poi", json=payload)
    assert response.status_code == 400, response.text
    assert assert_error_envelope(response.json())["detail"]["field"] == field


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/poi", "radius"),
        ("/api/analysis", "radius"),
        ("/api/analysis", "minutes"),
    ],
)
def test_booleans_are_rejected_instead_of_coerced_to_one(
    client: TestClient, path: str, field: str
) -> None:
    """🔴 `true` 会被 pydantic 强转成 `1`，于是 `radius: true` 变成**1 米**的检索半径
    并静默返回 200 —— 调用方拿到一份看起来正常、其实只搜了脚下一平方米的报告。

    这类**类型错**走 FastAPI 的 422（通用校验层），与「范围错走 400」是两条不同路径，
    但都必须点名是哪个字段，不能只回一句「参数不完整或有误」。
    """
    response = client.post(path, json={"lnglat": "116.404,39.915", field: True})
    assert response.status_code == 422, response.text
    detail = response.json()["error"]["detail"]
    assert any(field in item for item in detail["fields"]), detail


def test_poi_endpoint_rejects_overseas_coordinates(client: TestClient) -> None:
    """境外各服务对个人档均为「-」（笔记 §3.1），必须在入口挡住并说清原因。"""
    response = client.post("/api/poi", json={"lnglat": "-0.1276,51.5072", "data_mode": "mock"})
    assert response.status_code == 400
    error = assert_error_envelope(response.json())
    assert "境外" in error["message"] or "中国" in error["message"]


def test_poi_endpoint_scales_the_default_radius_by_mode(client: TestClient) -> None:
    """步行 1500 m / 骑行 4800 m（`R_max × 1.2`）。"""
    walking = client.post("/api/poi", json={"lnglat": "116.404,39.915", "data_mode": "mock"}).json()
    riding = client.post(
        "/api/poi", json={"lnglat": "116.404,39.915", "mode": "riding", "data_mode": "mock"}
    ).json()
    assert walking["radius_m"] == 1500
    assert riding["radius_m"] == 4800


def test_poi_endpoint_never_returns_5xx_in_mock_mode(client: TestClient) -> None:
    """FR-403：离线也要出可读结果而不是错误页。"""
    response = client.post("/api/poi", json={"lnglat": "116.404,39.915", "data_mode": "mock"})
    assert response.status_code < 500
    assert response.json()["degraded"] is True


# --------------------------------------------------------------------------------------
# live 检索层的 `market` 召回缺口必须在报告里说出来（README §10.8 / TEST_REPORT §4.4）
# --------------------------------------------------------------------------------------
class _StubPoiService:
    """只换 POI 层的 `data_source`，其余形状与真实报文一致（点位复用 coverage 的辅助器）。"""

    def __init__(self, data_source: str) -> None:
        self._source = data_source

    async def search(self, center, **_kwargs):
        from tests.test_coverage import CENTER, group, poi

        return {
            "center": list(CENTER),
            "radius_m": 1500,
            "data_source": self._source,
            "degraded": False,
            "groups": [
                group("medical", [poi("clinic", 200, 0)]),
                group("shopping", [poi("supermarket", 300, 100)]),
            ],
            "usage": {"http_requests": 1, "quota_units": 1, "place_calls": 2, "cache_hits": 0},
        }


@async_test
@pytest.mark.parametrize("poi_source", ["live", "cache"])
async def test_real_retrieval_warns_that_blind_spots_are_biased_up(poi_source: str) -> None:
    """POI 层是真实检索时，`notes` 必须明说盲区偏大——读报告的人不该先去翻 README。

    `cache` 与 `live` 一起断言：浏览器实跑（2026-10-10）发现同一社区第二次跑时 POI 层报
    `cache`，如果只认 `live`，这条提示会在缓存命中后悄悄消失，而同一条数据缺陷还在。
    """
    from services.analysis import run_analysis

    report = await run_analysis(
        [116.404, 39.915],
        modes=["walking"],
        minutes=15.0,
        data_mode="mock",
        poi_service=_StubPoiService(poi_source),
    )
    joined = " ".join(report["notes"])
    assert "market" in joined, report["notes"]
    assert "盲区面积" in joined and "偏大" in joined, report["notes"]


@async_test
async def test_sample_layer_does_not_claim_a_problem_it_does_not_have() -> None:
    """`mock` / `sample` 层的 `market` 是按画像合成的、有值，挂这条提示就是制造假问题。"""
    from services.analysis import run_analysis

    report = await run_analysis(
        [116.404, 39.915],
        modes=["walking"],
        minutes=15.0,
        data_mode="mock",
        poi_service=_StubPoiService("sample"),
    )
    assert not any("盲区面积" in note and "偏大" in note for note in report["notes"]), report[
        "notes"
    ]


@async_test
@pytest.mark.parametrize(
    "categories,expect_note",
    [
        (None, False),
        (["medical", "education", "market", "leisure", "transit", "food", "service_ref"], False),
        (["medical", "education", "market"], False),
        (["medical", "education", "leisure", "transit", "food"], True),
        (["medical", "education", "transit"], True),
        (["medical"], True),
        (["leisure"], True),
    ],
)
async def test_narrowed_categories_warn_when_blind_trio_is_affected(
    categories: list[str] | None, expect_note: bool
) -> None:
    """FR-104 少勾类别时，只有**伤到盲区主判定**的那几种才必须挂警示。

    盲区判定（FR-221）固定吃 `market / pharmacy / primary_school` 三个 subcat，而
    `services/blind_spots.py` 只按「附近没命中」判缺失，分不清「这次没查」。少勾
    `market` / `medical` / `education` 里任何一维，盲区面积就只可能偏大、不可能偏小
    （实测交道口只勾 medical 时 47.63 % → 93.49 %；天坛口径下也可能与全选一样是
    28.4054 —— 偏量为 0 但可比性仍没了，所以文案是**口径声明**而不是「这次算错了」）；
    勾掉 leisure / transit 这类无关维度则盲区不受影响，这时候挂警示反而是噪声。
    判据来自 `BLIND_TRIO_DIMENSIONS`（词表层派生，不手写）。
    """
    from services.analysis import run_analysis

    report = await run_analysis(
        [116.404, 39.915],
        modes=["walking"],
        minutes=15.0,
        categories=categories,
        data_mode="mock",
        poi_service=_StubPoiService("sample"),
    )
    joined = " ".join(report["notes"])
    flagged = "这一类这次没查" in joined
    assert flagged is expect_note, (categories, report["notes"])
    if expect_note:
        assert "只可能偏大" in joined and "比大小" in joined, report["notes"]


@async_test
async def test_narrowed_categories_also_change_the_score_denominator() -> None:
    """勾掉任何一个**计分**维度，总分的分母就跟着变 —— 前端那句「不是同一个分母」的提示靠这条兜底。

    `services/score.py:weighted_total()` 默认按「本次计分维度权重之和」归一化：全选七类是
    0.95（PRD §4.3 的真实缺口），只勾三类是 0.55，只勾一类是 0.20。于是「取消餐饮服务」这种
    **不碰盲区判定**的操作也会移动总分，而 `weight_sum` 与那条归一化 note 是唯一的线索。

    期望值一律从词表算出来（不手写 0.95 / 0.55）：改权重表时这条不该红。
    ⚠️ 这里**不能用 `_StubPoiService`**：它返回固定两份 groups、不认识 `categories`，
    测出来的分母是 stub 的性质而不是产品的行为（第一版就栽在这里，`full` 读出 0.4）。
    """
    from services.analysis import run_analysis
    from services.poi_categories import DIMENSION_ORDER, DIMENSIONS

    def scored_sum(keys: list[str]) -> float:
        return sum(DIMENSIONS[key]["weight"] for key in keys if DIMENSIONS[key]["scored"])

    async def weight_sum(categories: list[str] | None) -> float:
        report = await run_analysis(
            [116.404, 39.915],
            modes=["walking"],
            minutes=15.0,
            categories=categories,
            data_mode="mock",
        )
        return float(report["score"]["weight_sum"])

    subset = ["medical", "education", "market"]
    full = await weight_sum(None)
    narrowed = await weight_sum(subset)
    assert full == pytest.approx(scored_sum(list(DIMENSION_ORDER))), full
    assert narrowed == pytest.approx(scored_sum(subset)), narrowed
    assert narrowed < full, (narrowed, full)
