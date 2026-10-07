"""`/api/health` 的**字段契约**测试（§4.6 启动探测 / FR-105 / FR-404）。

这个文件的存在理由是一次真实事故：CI 的端到端冒烟脚本里写了
`body["data_mode_in_effect"] == "mock"`，而 `/api/health` 的**顶层根本没有这个键** ——
`data_mode_in_effect` 只存在于 `config` 子对象里（`.env` 配置的回显），
顶层是 `data_mode`（本次生效的模式）。那条断言会让 CI 在第一次运行时就 `KeyError`。

而它之所以能写错，是因为**同一份信息在响应里有三个近似字段**，文档里又都叫「数据模式」：

| 字段 | 位置 | 值域 | 含义 |
| --- | --- | --- | --- |
| `data_mode` | 顶层 | `live` / `cache` / `mock` | **本次生效的模式**（业务该读这个） |
| `data_mode_configured` | 顶层 | `auto` / `live` / `cache` / `mock` | `.env` 里写的原始值 |
| `data_mode_in_effect` | **`config` 子对象** | `live` / `cache` / `mock` | 配置层解析后的回显 |

三者不是同义词：`auto` 只存在于 `data_mode_configured`，而 `data_mode_in_effect`
**永远不会**是 `auto`（PRD §4.6 要求运行期必须解析成具体值）。
`web/isochrone.js` 的读法是 `config.data_mode_in_effect || health.data_mode`，两处都兜住；
但任何只取顶层 `data_mode_in_effect` 的代码都会炸。

所以本文件把这三个字段的**位置与值域**钉死，谁改了响应形状都会在这里红。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

EFFECTIVE_MODES = {"live", "cache", "mock"}
CONFIGURED_MODES = {"auto", "live", "cache", "mock"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def health(client: TestClient) -> dict:
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    return response.json()


# ======================================================================================
# 三个「数据模式」字段的位置与值域
# ======================================================================================
def test_top_level_data_mode_is_the_effective_one(health: dict) -> None:
    """顶层 `data_mode` 是**本次生效的模式** —— 业务代码与脚本该读的就是它。"""
    assert "data_mode" in health, "顶层缺 data_mode"
    assert health["data_mode"] in EFFECTIVE_MODES, health["data_mode"]


def test_data_mode_in_effect_is_not_a_top_level_key(health: dict) -> None:
    """🔴 事故回归：`data_mode_in_effect` 只在 `config` 子对象里。

    这条断言一旦变红，说明有人把 `data_mode_in_effect` 提升到了顶层 ——
    那时请**同时**更新 `web/isochrone.js` 的读取、`README.md` 的演示脚本、
    `tools/README-deploy.md` §5 与 `Dockerfile` 的 HEALTHCHECK 注释，
    以及 `.github/workflows/ci.yml` 的端到端冒烟。
    """
    assert (
        "data_mode_in_effect" not in health
    ), "data_mode_in_effect 出现在顶层了 —— 请确认这是有意的，并同步下游读法"
    assert "config" in health and "data_mode_in_effect" in health["config"]
    assert health["config"]["data_mode_in_effect"] in EFFECTIVE_MODES


def test_configured_mode_may_be_auto_but_effective_mode_never_is(health: dict) -> None:
    """`auto` 是配置值，不是运行值：PRD §4.6 要求运行期必须解析成具体模式。"""
    assert health["data_mode_configured"] in CONFIGURED_MODES
    assert health["data_mode"] in EFFECTIVE_MODES
    assert health["config"]["data_mode_in_effect"] in EFFECTIVE_MODES


def test_effective_mode_is_consistent_across_the_three_views(health: dict) -> None:
    """顶层与 `config` 里的生效模式必须一致（两处各算一遍就会分叉）。"""
    assert health["data_mode"] == health["config"]["data_mode_in_effect"]


# ======================================================================================
# 零配置：必须落到 mock 并挂横幅（§6.2）
# ======================================================================================
def test_zero_config_resolves_to_mock_with_a_warning_banner(health: dict) -> None:
    """无 AK 时不是红色报错，而是一条解释「为什么不是实时数据」的横幅。"""
    assert health["server_ak_type"] == "not_configured"
    assert health["baidu_reachable"] is False
    assert health["data_mode"] == "mock"
    banner = health["banner"]
    assert banner["show"] is True
    assert banner["level"] == "warning"
    assert banner["title"] and banner["message"]
    # 横幅必须给出可执行的下一步，而不只是「出错了」
    assert banner["cta_hint"]


def test_probe_short_circuits_without_an_ak_instead_of_calling_out(health: dict) -> None:
    """没有 AK 就不该发起任何探测请求（省下的是最稀缺的地点检索配额）。"""
    assert health["probe"]["requests_sent"] == 0
    assert health["probe"]["short_circuit"] == "no_server_ak"
    assert health["probe"]["performed"] is False


def test_health_uses_no_quota(health: dict) -> None:
    """探测自身**不消耗**三栏里的任何一栏（有 AK 时才可能真的发一次轻量请求）。"""
    usage = health["probe"]["usage"]
    assert usage["place_calls"] == 0
    assert usage["http_requests"] == 0
    assert usage["quota_units"] == 0


# ======================================================================================
# 逐服务结论：六类鉴权/配额错误要能互区分（FR-404）
# ======================================================================================
def test_every_service_row_carries_an_actionable_verdict(health: dict) -> None:
    """每一行都要有 key / state / message / action，前端据此选卡片样式与按钮。"""
    services = health["services"]
    assert len(services) >= 6
    for item in services:
        assert item["key"] and item["label"]
        assert item["state"], item["key"]
        assert item["message"], item["key"]
        assert item["action"], item["key"]
        assert isinstance(item["available"], bool)
    keys = {item["key"] for item in services}
    # PRD §4.7 要求这四项都被探测（批量算路、地点检索、地理编码、逆地理）
    assert {"geocode", "regeo", "place"} <= keys
    assert any(key.startswith("routematrix") or key.startswith("direction") for key in keys)


def test_health_advertises_the_three_column_budget(health: dict) -> None:
    """三栏预算与 PRD §4.6 的建议值一致，前端与报告都读它。"""
    budget = health["config"]["budget"]
    assert budget["place_calls"] == 8
    assert budget["http_requests"] == 20
    assert budget["quota_units"] == 120
    assert budget["route_lines"] == 150


def test_health_never_leaks_an_ak_value(health: dict) -> None:
    """FR-410：AK 只以布尔存在性出现，值本身永不下发（`public_dict()` 的硬约束）。"""
    import json

    blob = json.dumps(health, ensure_ascii=False)
    assert "baidu_server_ak" not in blob.replace("has_server_ak", "")
    assert health["config"]["has_server_ak"] is False
    assert health["config"]["has_browser_ak"] is False
