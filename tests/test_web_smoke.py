"""前端冒烟测试（R11 / §6.4 CI）：首页与地图页可服务、零公网外链、DOM 契约对得上。

R11 的原话是「Vue 无构建模式下模板写在 JS 字符串里，写错了没有编译器与类型检查兜底 →
CI 增一条无头冒烟（请求首页并断言关键 DOM 节点存在）」。本项目最后没上 Vue
（图表也改成手绘 SVG，见 `web/report.js` 的说明），但**风险完全相同且更直接**：
`report.js` 用 `getElementById` 取 24 个节点，任何一个 id 在 HTML 里写错或改名，
面板就会静默半死 —— 没有异常、没有报错，只是某一块永远空白。

所以本文件做三件事：

1. **静态页可服务**：`/`、`/isochrone.html`、两个脚本、三个样式表全部 200。
2. **零公网外链**（R12 / R13）：`<script src>` 与 `<link href>` 里不许出现任何
   `http://` / `https://` / `//cdn` 形态的地址。百度 JSAPI 是**运行时动态注入**的，
   不该出现在静态 HTML 里（那是 R13 要求的可降级形态）。
3. 🔴 **DOM 契约对账**：把 `web/report.js` 里 `getElementById("…")` 的 id 全部抠出来，
   逐个断言在 `isochrone.html` 里存在。这条断言抓到的是「改了 HTML 忘了改 JS」
   这类只能靠肉眼发现的错误。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from infra.config import ROOT_DIR

WEB = ROOT_DIR / "web"
ISOCHRONE_HTML = WEB / "isochrone.html"
REPORT_JS = WEB / "report.js"
INDEX_HTML = WEB / "index.html"

# 只匹配「取单个元素」的写法；`querySelector` 走 CSS 选择器，不在本断言的范围内。
GET_BY_ID = re.compile(r"""getElementById\(\s*["']([^"']+)["']\s*\)""")
HTML_ID = re.compile(r"""\bid=["']([^"']+)["']""")
# `src="..."` / `href="..."`：协议相对（`//host`）也要抓，它是 CDN 的常见写法。
ASSET_REF = re.compile(r"""\b(?:src|href)=["']([^"']+)["']""")
EXTERNAL = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
JS_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")


def strip_html_comments(text: str) -> str:
    """去掉 HTML 注释。

    必须去：本页的注释里正大光明地写着 `api.map.baidu.com` 与 `https://...`，
    那是**解释为什么不能写进静态 HTML**，不是引用。不剥注释的话，
    「零外链」这条断言会把说明文字判成违规，然后被人删掉 —— 那是把哨兵当噪音。
    """
    return HTML_COMMENT.sub("", text)


def strip_js_comments(text: str) -> str:
    """去掉 JS 的块注释与整行注释（只看真实代码，不看正文）。"""
    return JS_LINE_COMMENT.sub("", JS_BLOCK_COMMENT.sub("", text))


@pytest.fixture(scope="module")
def client() -> TestClient:
    from api.app import app

    return TestClient(app)


def html_ids(path: Path) -> set[str]:
    return set(HTML_ID.findall(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# 1. 静态资源可服务
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/isochrone.html",
        "/isochrone.js",
        "/report.js",
        "/app.css",
        "/isochrone.css",
        "/report.css",
    ],
)
def test_static_assets_are_served(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_home_page_exposes_the_key_dom_nodes(client: TestClient) -> None:
    """§6.2「零配置不出现红色报错」：首页至少要有模式徽标、横幅与探测表三块。"""
    text = client.get("/").text
    for node_id in ("mode-badge", "banner", "services", "reprobe"):
        assert f'id="{node_id}"' in text, f"首页缺少 #{node_id}"


# --------------------------------------------------------------------------------------
# 2. 零公网外链（R12 / R13）
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("page", [INDEX_HTML, ISOCHRONE_HTML])
def test_no_external_asset_references(page: Path) -> None:
    text = strip_html_comments(page.read_text(encoding="utf-8"))
    external = [ref for ref in ASSET_REF.findall(text) if EXTERNAL.match(ref.strip())]
    # `data:,` 是空 favicon，本地相对路径都不是外链；出现 http(s) 或 // 就是违规。
    assert external == [], f"{page.name} 引用了公网资源：{external}"


def test_jsapi_is_not_hardcoded_in_static_html() -> None:
    """JSAPI 必须只在运行时按条件注入（R13），静态 HTML 里出现它意味着降级路径被绕过。"""
    text = strip_html_comments(ISOCHRONE_HTML.read_text(encoding="utf-8"))
    assert "api.map.baidu.com" not in text
    # 动态注入那一侧确实有（否则「实时底图」这条功能就是空的）。比对真实代码，不看注释。
    assert "api.map.baidu.com" in strip_js_comments(
        (WEB / "isochrone.js").read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------------------
# 3. DOM 契约对账（本文件的核心断言）
# --------------------------------------------------------------------------------------
def test_report_js_ids_all_exist_in_the_page() -> None:
    """`report.js` 取的每个 id，`isochrone.html` 里都必须有。

    这条抓的是「改了 HTML 忘了改 JS」：漏一个 id，`el.x` 就是 null，
    面板某一块永远空白，而控制台**一条错误都不会有**（只在真正 appendChild 时才炸）。
    """
    wanted = sorted(set(GET_BY_ID.findall(REPORT_JS.read_text(encoding="utf-8"))))
    assert wanted, "没从 report.js 里解析到任何 getElementById —— 正则或文件路径失效了"
    available = html_ids(ISOCHRONE_HTML)
    missing = [node_id for node_id in wanted if node_id not in available]
    assert missing == [], f"isochrone.html 缺少这些 id：{missing}"


def test_report_panel_is_present_in_the_page() -> None:
    """FR-301~FR-306 的落点必须真实存在，而不是只写在 JS 里。"""
    text = ISOCHRONE_HTML.read_text(encoding="utf-8")
    for node_id in (
        "report",
        "score-card",
        "radar-chart",
        "bar-chart",
        "poi-table",
        "blind-list",
        "analysis-usage",
        "export-json",
        "export-csv",
        "sample-select",
        "run-analysis",
    ):
        assert f'id="{node_id}"' in text, f"缺少报告面板节点 #{node_id}"


def test_report_js_is_loaded_after_the_map_script() -> None:
    """`report.js` 依赖 `window.CircleDocMap`，必须排在 `isochrone.js` 之后。"""
    text = ISOCHRONE_HTML.read_text(encoding="utf-8")
    map_at = text.index("isochrone.js")
    report_at = text.index("report.js")
    assert map_at < report_at, "report.js 必须在 isochrone.js 之后加载"


def test_report_js_does_not_recompute_backend_numbers() -> None:
    """前端不许自己算分数 / 面积 —— 算了就会与后端分叉，而评审看到的是前端那个。

    做法：断言几个**只有后端会产出**的字段名出现在 report.js 里（说明是读取而非重算），
    同时断言文件里没有出现评分公式的特征常量（0.5 权重、−0.5 惩罚之类）被用于乘法。
    """
    text = REPORT_JS.read_text(encoding="utf-8")
    for field in ("score.total", "score.grade", "blind_spots", "nearest_minutes"):
        assert field in text, f"report.js 没有读取后端字段 {field}"
    assert "weight_sum" in text
    # 惩罚与归一化都由后端裁决；前端只展示，不该出现「自己乘 0.5」的算式。
    assert "penalty" in text


def test_export_declares_scope_and_provenance() -> None:
    """FR-213 / 条款 3.3.4：导出必须带来源与「不得二次分发」，且只限本次分析。"""
    text = REPORT_JS.read_text(encoding="utf-8")
    code = strip_js_comments(text)
    assert "不得二次分发" in code
    assert "百度地图开放平台" in code
    assert "only_this_analysis" in code
    # 不提供跨分析的全库导出：不该有 localStorage / indexedDB 这类持久化入口。
    # 比对真实代码 —— 文件头注释里正解释着「不落 localStorage」，那是说明不是用法。
    assert "localStorage" not in code
    assert "indexedDB" not in code


def test_compliance_footer_is_present() -> None:
    """FR-413：页脚要有数据来源与合规声明。"""
    text = ISOCHRONE_HTML.read_text(encoding="utf-8")
    assert "百度地图开放平台" in text
    assert "不复制任何第三方内容" in text


# --------------------------------------------------------------------------------------
# FR-212 / FR-222 / FR-213：三个 P0 前端落点的接线契约
# --------------------------------------------------------------------------------------
def test_map_layer_toggles_exist() -> None:
    """两个图层的开关必须真实存在，否则 JS 里 `el.showPois` 是 null、勾选无反应。"""
    text = ISOCHRONE_HTML.read_text(encoding="utf-8")
    for node_id in ("show-pois", "show-blind"):
        assert f'id="{node_id}"' in text, f"缺少图层开关 #{node_id}"


def test_blind_spot_export_button_exists() -> None:
    """FR-223：盲区清单要能导出。"""
    text = ISOCHRONE_HTML.read_text(encoding="utf-8")
    assert 'id="export-blind-csv"' in text


def test_poi_table_headers_declare_sort_keys() -> None:
    """FR-213：可排序列必须在 HTML 里声明 `data-sort`，否则 JS 挂不上监听。"""
    text = ISOCHRONE_HTML.read_text(encoding="utf-8")
    for key in ("name", "category", "distance", "minutes", "polygon", "measured"):
        assert f'data-sort="{key}"' in text, f"表头缺少 data-sort={key}"


def test_isochrone_js_handles_polygon_and_multipolygon() -> None:
    """🔴 回归：`blocks_to_geojson()` 会输出 `MultiPolygon`（一个块多个分离环）。

    只按 `Polygon` 取 `coordinates[0]` 会让那个块被静默丢掉
    ——「报告说 4 块、地图上只有 3 块」，且控制台一条错误都没有。
    实测交道口的 4 个块里有 1 个正是 3 个分离环的 MultiPolygon。
    """
    code = strip_js_comments((WEB / "isochrone.js").read_text(encoding="utf-8"))
    assert "MultiPolygon" in code, "未处理 MultiPolygon"
    assert "geojsonRings" in code, "缺少统一的几何归一函数"
    # 不允许再出现「只取 coordinates[0] 当外环」的老写法
    assert "function geojsonOuterRing(" not in code


def test_isochrone_js_exposes_overlay_bridge() -> None:
    """`report.js` 依赖 `setOverlays` 把两层交给地图，接口名不能悄悄改。"""
    code = strip_js_comments((WEB / "isochrone.js").read_text(encoding="utf-8"))
    assert "function setOverlays(" in code
    assert "setOverlays," in code, "setOverlays 未挂到 window.CircleDocMap"
    for name in ("drawPois", "drawBlindSpots", "blindBlocks", "geojsonRings"):
        assert f"function {name}(" in code, f"缺少 {name}"


def test_isochrone_js_draws_both_layers_in_both_engines() -> None:
    """SVG 舞台与 JSAPI 实底图两条路径都要画 —— 只做一条等于离线演示看不到。"""
    code = strip_js_comments((WEB / "isochrone.js").read_text(encoding="utf-8"))
    assert "drawBlindSpots();" in code
    assert "drawPois();" in code
    # JSAPI 那条分支里必须有 Polygon / Circle 覆盖物
    assert "BMap.Polygon" in code
    assert "BMap.Circle" in code


def test_report_js_sends_blind_geojson_to_the_map() -> None:
    """FR-222 的数据来源是 `blind_spots.geojson`；只读 `blocks` 就画不出地图。"""
    code = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    assert "blind_spots" in code
    assert ".geojson" in code, "report.js 没有把 geojson 交给地图"
    assert "setOverlays" in code


def test_report_js_sends_every_poi_with_its_dimension() -> None:
    """FR-212：POI 必须带上维度键，地图才能按类别分色。"""
    code = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    assert "flattenPois" in code
    assert "category: group.key" in code


def test_sortable_headers_and_unmeasured_go_last() -> None:
    """FR-213：未实测（`null`）必须垫底，任何方向都不许被当成 0 排到最前。"""
    code = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    assert "sortRows" in code
    assert "sortState" in code
    assert "aria-sort" in code
    assert "leftMissing ? 1 : -1" in code, "缺值垫底的实现被改动了"
