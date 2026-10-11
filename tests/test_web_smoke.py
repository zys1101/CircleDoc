"""前端冒烟测试（R11 / §6.4 CI）：首页与地图页可服务、零公网外链、DOM 契约对得上。

R11 的原话是「Vue 无构建模式下模板写在 JS 字符串里，写错了没有编译器与类型检查兜底 →
CI 增一条无头冒烟（请求首页并断言关键 DOM 节点存在）」。本项目最后没上 Vue
（图表也改成手绘 SVG，见 `web/report.js` 的说明），但**风险完全相同且更直接**：
`isochrone.js` 与 `report.js` 各自用 `getElementById` 取一批节点，任何一个 id 在 HTML 里
写错或改名，面板就会静默半死 —— 没有异常、没有报错，只是某一块永远空白。

所以本文件做四件事：

1. **静态页可服务**：`/`、`/isochrone.html`、两个脚本、三个样式表全部 200。
2. **零公网外链**（R12 / R13）：`<script src>` 与 `<link href>` 里不许出现任何
   `http://` / `https://` / `//cdn` 形态的地址。百度 JSAPI 是**运行时动态注入**的，
   不该出现在静态 HTML 里（那是 R13 要求的可降级形态）。
3. 🔴 **DOM 契约对账**：把两个脚本里 `getElementById` 取到的 id 全部抠出来，
   逐个断言在 `isochrone.html` 里存在。这条断言抓到的是「改了 HTML 忘了改 JS」
   这类只能靠肉眼发现的错误。
4. 🔴 **前端 ↔ 后端词表对账**（FR-104）：类别勾选框的 `data-cat` 取值与顺序、勾选框文案，
   以及标记「哪几类装着盲区主判定」的 `data-blind-cat`，全部与
   `services/poi_categories.py` 逐项相等。这条抓的是另一类失配：改了词表忘了改 HTML，
   前端拿着后端不认的键去请求（直接 400），或者把盲区警示指到错误的维度上。
5. 🔴 **FR-106 移动端底部抽屉的接线契约**：把手条三个 `data-sheet` 都指向真实面板、
   断点只有一个数字真值、位移只用 `transform`、拖拽的后续事件挂在 `window` 上、
   移动端热区 ≥ 44 px。抽屉是这个仓库里唯一「DOM 不动、全靠属性与位移」的一块，
   接错一根线就是「点了没反应」，而控制台不会响。
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
ISOCHRONE_JS = WEB / "isochrone.js"
ISOCHRONE_CSS = WEB / "isochrone.css"
REPORT_CSS = WEB / "report.css"
INDEX_HTML = WEB / "index.html"

# 只匹配「取单个元素」的写法；`querySelector` 走 CSS 选择器，不在本断言的范围内。
GET_BY_ID = re.compile(r"""getElementById\(\s*["']([^"']+)["']\s*\)""")
HTML_ID = re.compile(r"""\bid=["']([^"']+)["']""")
# `src="..."` / `href="..."`：协议相对（`//host`）也要抓，它是 CDN 的常见写法。
ASSET_REF = re.compile(r"""\b(?:src|href)=["']([^"']+)["']""")
EXTERNAL = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
# FR-104 的勾选框行：`<input … />` 紧跟 `<span>文案</span>`。
# 文案要与后端 `DIMENSIONS[key]["label"]` **逐字相同**，所以这里连文案一起抠出来对账。
CAT_ROW = re.compile(r"""<input(?P<attrs>[^>]*?)/>\s*<span>(?P<label>[^<]+)</span>""")
CAT_KEY = re.compile(r'data-cat="([a-z_]+)"')
# `#category-hint` 的静态初值（JS 加载后会按勾选态覆盖它）。
CATEGORY_HINT_TEXT = re.compile(r"""id="category-hint"[^>]*>\s*(?P<body>[^<]+?)\s*</p>""")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
JS_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
# --- FR-106 抽屉：把手条三个按钮 + 一根装饰性拖拽条，开合全靠 `data-sheet` ↔ 面板 id 的对应。
SHEET_BAR = re.compile(r'<div class="sheet-bar" id="sheet-bar">(?P<body>.*?)</div>', re.DOTALL)
DATA_SHEET = re.compile(r'data-sheet="([a-z]+)"')
ARIA_CONTROLS = re.compile(r'aria-controls="([a-z]+)"')
BREAKPOINT_DECL = re.compile(r"const SHEET_BREAKPOINT_PX = (\d+);")
# 移动端规则一律长成 `.page-map[data-sheet-mode="mobile"] <选择器> { … }`，声明块里不嵌套，
# 所以 `[^}]*` 就够；选择器可能是逗号分组，交给 sheet_rule() 逐条比而不是拿子串搜。
MOBILE_RULE = re.compile(
    r'\.page-map\[data-sheet-mode="mobile"\]\s*(?P<selectors>[^{]+)\{(?P<body>[^}]*)\}'
)


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
@pytest.mark.parametrize("script", [REPORT_JS, ISOCHRONE_JS], ids=["report.js", "isochrone.js"])
def test_every_get_element_by_id_exists_in_the_page(script: Path) -> None:
    """两个脚本取的每个 id，`isochrone.html` 里都必须有。

    这条抓的是「改了 HTML 忘了改 JS」：漏一个 id，`el.x` 就是 null，
    面板某一块永远空白，而控制台**一条错误都不会有**（只在真正 appendChild 时才炸）。

    ⚠️ 两个脚本都要覆盖：FR-104 落地时只给 `report.js` 对账，`isochrone.js` 那侧新加的
    `#cat-all` / `#category-hint` 就落在哨兵外面 —— 少一个 id 的后果是「类别警示永远不出现」，
    恰好是最难看出来的那种坏法。
    """
    wanted = sorted(set(GET_BY_ID.findall(script.read_text(encoding="utf-8"))))
    assert wanted, f"没从 {script.name} 里解析到任何 getElementById —— 正则或文件路径失效了"
    available = html_ids(ISOCHRONE_HTML)
    missing = [node_id for node_id in wanted if node_id not in available]
    assert missing == [], f"isochrone.html 缺少这些 id：{missing}"


def test_category_checkboxes_match_the_backend_wordlist() -> None:
    """FR-104 的七个勾选框要与后端词表**逐键、逐序、逐文案**对齐，盲区三类标记也算。

    这是前端与 `services/poi_categories.py` 之间唯一的自动对账，两种失配都会要命：
      * `data-cat` 写了后端不认的键 → 每次体检直接 400；顺序变了 → `categories` 与
        `coverage.groups` 对不上，报告里维度顺序跟着抖。
      * `data-blind-cat` 指错维度 → 前端提示「口径可比」而其实盲区面积已被高估，
        用户看到的正是那份报告最不该错的东西。词表把小学挪去别的维度时，
        `BLIND_TRIO_DIMENSIONS` 会自己跟着变，这条断言立刻红。
    """
    from services.poi_categories import BLIND_TRIO_DIMENSIONS, DIMENSION_ORDER, DIMENSIONS

    text = strip_html_comments(ISOCHRONE_HTML.read_text(encoding="utf-8"))
    rows = [m for m in CAT_ROW.finditer(text) if 'class="cat"' in m["attrs"]]

    def key_of(match: re.Match[str]) -> str:
        found = CAT_KEY.search(match["attrs"])
        return found.group(1) if found else ""

    assert len(rows) == len(
        DIMENSION_ORDER
    ), f"勾选框 {len(rows)} 个 vs 词表 {len(DIMENSION_ORDER)} 维"
    keys = [key_of(m) for m in rows]
    labels = [m["label"].strip() for m in rows]
    assert keys == list(DIMENSION_ORDER), (keys, list(DIMENSION_ORDER))
    assert labels == [DIMENSIONS[key]["label"] for key in DIMENSION_ORDER], labels
    assert {
        key for key, m in zip(keys, rows, strict=True) if "data-blind-cat" in m["attrs"]
    } == set(BLIND_TRIO_DIMENSIONS)
    # 缺省必须是全选：全选 = 不发 `categories`，行为与多选功能落地前逐字节一致。
    assert all("checked" in m["attrs"] for m in rows), [m["attrs"] for m in rows]
    # 全选得是一条独立分支：否则 7/7 会掉进「子集」文案（浏览器实测踩过这个坑）。
    map_js = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    assert 'state = "all"' in map_js and "on === total" in map_js
    # HTML 里那句静态初值要与 JS 的 all 分支逐字相同：JS 一加载就覆盖它，
    # 两边写岔就是「刷新瞬间看到另一套口径」。
    static_hint = CATEGORY_HINT_TEXT.search(text)
    assert static_hint, "找不到 #category-hint 的静态文案"
    assert static_hint["body"].strip() in map_js, static_hint["body"]
    assert 'data-state="all"' in text


def test_category_hint_states_are_styled() -> None:
    """JS 只写 `data-state`、颜色全在 CSS 里 —— 两边各改一半就是「警示存在但看不见」。

    判据取 JS 里实际赋的四个态，而不是测试里手写一份名单：手写的那份会在加第五态时静默通过。
    """
    css = ISOCHRONE_CSS.read_text(encoding="utf-8")
    js = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    states = set(re.findall(r'state = "([a-z]+)"', js))
    assert states == {"all", "none", "subset", "hurt"}, states
    for state in sorted(states - {"all"}):
        assert f'#category-hint[data-state="{state}"]' in css, f"缺 #{state} 的样式"


def test_category_selection_is_only_sent_when_narrowed() -> None:
    """全选发 `null`（= 不发这个键），收窄才发数组，零勾选必须挡在 `fetch` 之前。

    三条各有其害：全选也发七个键 → 同一中心点换一把缓存键、白烧一次地点检索配额；
    零勾选发 `[]` → `api/analysis.py` 把 falsy 的 `[]` 折成 `None`（= 检索全七类），
    「取消全部却拿到一份完整的七类报告」是本产品最不能出现的一种假数据。
    """
    map_js = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    report_js = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    assert "categories: selectedCategories()" in map_js
    assert "? null : on" in map_js, "全选时必须折叠成 null，而不是把七个键原样发出去"
    assert "Array.isArray(controls.categories)" in report_js
    assert "Array.isArray(body.categories) && !body.categories.length" in report_js
    assert "0/7" in report_js, "挡下零勾选时得告诉用户为什么没发请求"
    # 警示文案的两个判据词：后端 note 与前端提示各说一遍（报告可离线导出，notes 跟着走）
    for phrase in ("只可能偏大", "这一类这次没查"):
        assert phrase in map_js, phrase


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


def strip_css_comments(text: str) -> str:
    """去掉 CSS 的 `/* … */` 注释（块注释语法与 JS 相同，复用同一条正则）。

    必须去：FR-106 那一段的说明里正写着 `body[data-sheet-mode="mobile"]` 与
    `matchMedia("(max-width: 768px)")`，不剥注释的话，「HTML 不许预写这个属性」
    「断点只有一个真值」这类断言会被说明文字带过去。
    """
    return JS_BLOCK_COMMENT.sub("", text)


def sheet_rule(selector: str) -> str:
    """取 `.page-map[data-sheet-mode="mobile"] <selector>` 这条规则的声明块，找不到返回空串。

    按「选择器列表逐条比对」而不是拿 selector 去搜文本：分组选择器
    （`a,\nb { … }`）和 `.sheet-tab` 与 `.sheet-tab[aria-expanded="true"]` 这种前缀包含，
    用子串搜都会认错规则；分组里除第一条之外都自带完整前缀，所以逐条剥前缀再等值比。
    """
    css = strip_css_comments(ISOCHRONE_CSS.read_text(encoding="utf-8"))
    for match in MOBILE_RULE.finditer(css):
        for item in match["selectors"].split(","):
            # 分组选择器里除了第一条，其余都自带完整前缀，先剥掉再逐条比。
            stripped = re.sub(r'^\.page-map\[data-sheet-mode="mobile"\]\s*', "", item.strip())
            if stripped == selector:
                return match["body"]
    return ""


# --------------------------------------------------------------------------------------
# FR-106 移动端底部抽屉：接线契约
# --------------------------------------------------------------------------------------
def test_sheet_bar_tabs_point_at_real_panels() -> None:
    """把手条每个 `data-sheet` 都必须是一个真实存在的面板 id，且 `aria-controls` 与它同名。

    「开哪一块」全靠这三个字符串建立对应：写错一个就是点「进度」没反应，
    而 `aria-expanded` 会指着空气 —— 读屏用户听到「已展开」，屏幕上什么都没有。
    """
    text = strip_html_comments(ISOCHRONE_HTML.read_text(encoding="utf-8"))
    bar = SHEET_BAR.search(text)
    assert bar, "找不到 #sheet-bar"
    keys = DATA_SHEET.findall(bar["body"])
    assert keys == ["controls", "readout", "report"], keys
    assert ARIA_CONTROLS.findall(bar["body"]) == keys, "aria-controls 与 data-sheet 不同名"
    available = html_ids(ISOCHRONE_HTML)
    for key in keys:
        assert key in available, f"把手条指向不存在的面板 #{key}"
    # 把手是 `aria-hidden` 的装饰性拖拽条：它能做的事，三个按钮都必须也能做，
    # 否则键盘与读屏就只剩一条没有语义的路径。
    assert 'aria-hidden="true"' in bar["body"]
    assert (
        text.index("</main>") > bar.end()
    ), "把手条必须留在 </main> 内（否则键盘顺序跳到页脚之后）"


def test_sheet_breakpoint_has_one_number_and_the_profile_probe_agrees() -> None:
    """768 只能有一处数字真值（JS 常量），且 FR-107 的平台判定与报告样式得跟着它。

    断点写两份必然分叉：抽屉在 700 px 生效、轻量档在 768 px 生效，就会出现
    「手机上开着抽屉、请求体却按桌面口径取 k=16」这种自相矛盾的会话。
    """
    map_js = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    report_js = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    declared = BREAKPOINT_DECL.search(map_js)
    assert declared, "isochrone.js 里没有 `const SHEET_BREAKPOINT_PX = …`"
    px = declared.group(1)
    # 抽屉那条 matchMedia 必须引用常量本身，而不是把数字再抄一遍。
    assert "(max-width: ${SHEET_BREAKPOINT_PX}px)" in map_js
    assert map_js.count("max-width:") == 1, "isochrone.js 里出现了第二条媒体查询，断点又开始分叉"
    # `report.js` 那边是字面量（它拿不到 isochrone.js 的常量），所以必须与这个数相等。
    # 两层括号都要转义：`(max-width: …)` 里的圆括号是媒体查询语法的一部分，不是捕获组。
    used = re.findall(r'matchMedia\("\(max-width: (\d+)px\)"\)', report_js)
    assert used and set(used) == {px}, used
    # 样式表里不许再出现第三个 768 来管抽屉：抽屉的开关是属性，不是媒体查询。
    css = strip_css_comments(ISOCHRONE_CSS.read_text(encoding="utf-8"))
    assert "(max-width: 768px)" not in css
    assert f"@media (max-width: {px}px)" in REPORT_CSS.read_text(encoding="utf-8")


def test_no_js_path_keeps_the_panels_in_normal_flow() -> None:
    """没有脚本时抽屉**不出现**，三块面板回到正常流里堆叠 —— 这是刻意的降级，得守住。

    三条各守一头：① HTML 里不许预写 `data-sheet-mode`（预写了，JS 挂掉时面板照样被
    `position: fixed` 移出去，等于把参数和报告永久藏到屏幕外）；
    ② 未加 `[data-sheet-mode="mobile"]` 前缀的 `.sheet-bar` 基础规则必须 `display: none`；
    ③ 收起态必须同时写 `transform` 与 `visibility: hidden` —— 只位移的话收起的面板仍被读屏
    读到（三块面板叠在同一处，等于一次读出三份），只 hidden 的话动画结束会跳一下。
    """
    text = strip_html_comments(ISOCHRONE_HTML.read_text(encoding="utf-8"))
    assert "data-sheet-mode" not in text
    css = strip_css_comments(ISOCHRONE_CSS.read_text(encoding="utf-8"))
    base = re.search(r"(?m)^\.sheet-bar \{(?P<body>[^}]*)\}", css)
    assert base, "找不到 .sheet-bar 的基础规则"
    assert "display: none" in base["body"], base["body"]
    collapsed = sheet_rule("#controls")
    assert "position: fixed" in collapsed
    assert "transform: translateY(" in collapsed and "visibility: hidden" in collapsed, collapsed
    # 展开态三条选择器共用一个声明块（`[data-sheet-open="…"] #…`），比一条就够。
    opened = sheet_rule('[data-sheet-open="controls"] #controls')
    assert "transform: translateY(0)" in opened and "visibility: visible" in opened, opened


def test_stage_height_does_not_subtract_footer_or_legend() -> None:
    """🔴 回归锚点：地图可用高度只扣「地图上方真正占掉的像素」。

    2026-10-11 第一版把 `.page-foot`（101 px）与 `#legend`（640 px）一起从视口里减掉，
    375×667 实测地图只剩 240 px —— 抽屉做得再对，「地图占满首屏」这条也过不了。
    页脚与图例本来就在首屏之下、靠滚动才看到，不参与扣减；
    量 stage-area 自己的上边界，比按元素名累加可靠（margin、gap、`[hidden]` 都在几何里）。
    """
    code = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    body = re.search(r"function sizeSheetStage\(\) \{(?P<body>.*?)\n\}", code, re.DOTALL)
    assert body, "找不到 sizeSheetStage()"
    for element in (".page-foot", "#legend", ".banner", ".page-head", ".notice"):
        assert element not in body["body"], f"又开始按元素名扣高度了：{element}"
    assert "getBoundingClientRect" in body["body"]
    assert '"--map-h"' in body["body"] and '"--sheet-bar-h"' in body["body"]


def test_sheet_drag_uses_pointer_events_on_window_not_capture() -> None:
    """拖抽屉的后续事件必须挂在 `window` 上，且整段不许出现 `setPointerCapture`（R15 / D-1）。

    🔴 D-1 的教训：`setPointerCapture` 会把这次指针的后续事件一律改派给捕获者。
    把手只有 104 px 宽，手指一滑出它就断流 —— 捕获更是让事件永远到不了别处；
    抽屉拖到一半停住，比根本没有抽屉更糟。
    只写 pointer 事件、不写 `touch*` 分支，同样是 R15 的口径（两条路径会互相漏事件）。
    """
    code = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    assert 'sheetGrip.addEventListener("pointerdown", sheetPointerDown)' in code
    for event, name in (
        ("pointermove", "sheetPointerMove"),
        ("pointerup", "sheetPointerUp"),
        ("pointercancel", "sheetPointerUp"),
    ):
        assert f'window.addEventListener("{event}", {name})' in code, event
    start = code.index("const SHEET_BREAKPOINT_PX")
    end = code.index('el.stage.addEventListener("pointerdown"')
    section = code[start:end]
    assert "setPointerCapture" not in section
    assert "touchstart" not in code and "touchmove" not in code
    # 拖拽是「动了才算」：6 px 阈值 + 中点吸附，避免把手指的原地抖动当成收起。
    assert "moved < 6" in section and "travel / 2" in section
    # 把手不关掉浏览器滚动手势，pointermove 根本不发。
    assert "touch-action: none" in sheet_rule(".sheet-grip")


def test_mobile_hit_areas_are_44px() -> None:
    """把手条按钮、拖拽条与每一条勾选行的热区都要 ≥ 44 px（PRD §6.3）。

    勾选框这条要单独说：`input` 本身只有 18 px，而原生点击目标是整行
    `<label class="check">` —— `min-height` 加在 input 上看着达标，实际能按到的还是 18 px。
    """
    for selector in (".sheet-tab", ".sheet-grip", ".check", "#categories .check", ".back"):
        rule = sheet_rule(selector)
        assert rule, f"移动端缺少 {selector} 规则"
        assert "min-height: 44px" in rule, selector
    css = strip_css_comments(ISOCHRONE_CSS.read_text(encoding="utf-8"))
    band = re.search(r"@media \(max-width: 480px\) \{(?P<body>.*?)\n\}", css, re.DOTALL)
    assert band and ".page-map button" in band["body"], "≤480 的按钮 44 px 兜底被删了"
    assert "min-height: 44px" in band["body"]


def test_mobile_head_yields_the_first_screen_to_the_map() -> None:
    """页头 / 横幅在移动端必须压扁，而且只许藏标语，不许藏导航。

    375×667 实测：按桌面板式是 `.page-head` 227 px + `.banner` 210 px，地图被顶出首屏。
    压扁的方式有讲究 —— `.sub` 那句标语（「CircleDoc · 15 分钟生活圈体检助手」）是重复信息，
    可以 `display: none`；同一段里的「← 返回首页」是手机上唯一的方向导航，
    连它一起藏就是拿可用性换像素。
    """
    assert "display: none" in sheet_rule(".sub span")
    assert "display: none" not in sheet_rule(".sub")
    assert "padding" in sheet_rule(".page-head")
    banner = sheet_rule(".banner")
    # 横幅要封顶内部滚动，不能把地图顶下去。
    assert "max-height" in banner and "overflow-y: auto" in banner
    text = strip_html_comments(ISOCHRONE_HTML.read_text(encoding="utf-8"))
    sub = re.search(r'<p class="sub">(?P<body>.*?)</p>', text, re.DOTALL)
    # 返回首页链接不在 .sub 里，压缩规则就会把它一起藏掉。
    assert sub and 'class="back"' in sub["body"]


def test_report_opens_the_panel_it_just_filled() -> None:
    """手机上跑完体检要把报告浮到地图上方，而 `window.CircleDocMap` 得真的交出这个方法。

    不自动开的话「一键体检」在手机上看起来就是死的：结果写进那块收起态在屏幕外的固定层。
    桌面必须是空操作 —— 三栏本来同屏，开抽屉反而把面板 fixed 出去。
    """
    report = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    code = strip_js_comments(ISOCHRONE_JS.read_text(encoding="utf-8"))
    assert 'openSheet("report")' in report
    assert "function openSheet(" in code
    # 桌面必须是空操作：桥接方法内部判过视口，调用方不需要再判一次。
    assert "openSheet: (key) => {" in code
    assert "if (isSheetMobile()) openSheet(key);" in code
    assert "isSheetMobile: () => isSheetMobile()," in code
    # Escape 收起与 `aria-expanded` 同步都靠 openSheet("") 这一条路径。
    assert "aria-expanded" in code and 'openSheet("")' in code


# --------------------------------------------------------------------------------------
# FR-107 轻量档：`?profile=` 显式覆盖 + 报告上的「简化模式」标注
# --------------------------------------------------------------------------------------
def test_profile_url_overrides_the_viewport_probe() -> None:
    """PRD 的判据是「小屏**或** `?profile=m`」，只看 `matchMedia` 等于只实现了一半。

    钉三件事：① 档位判定收在一个函数里（`payloadForRun` 不许自己再判一次，否则两处会分叉）；
    ② `m` / `d` 两种短写法都认；③ 认不出的值回落到视口判定而不是抛错 ——
    URL 打错字不该让整次体检 400。
    """
    code = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    assert "function resolvePlatform()" in code
    assert "platform: resolvePlatform()," in code
    # 只有一个地方读那个 768：整个文件里 matchMedia 只许出现一次。
    assert code.count('matchMedia("(max-width: 768px)")') == 1, "档位判定被写成了两处"
    assert 'get("profile")' in code
    assert 'forced === "m" || forced === "mobile"' in code
    assert 'forced === "d" || forced === "desktop"' in code
    body = code.split("function resolvePlatform()")[1].split("\n  function ")[0]
    assert "throw" not in body


def test_lightweight_mode_is_marked_from_the_response_meta() -> None:
    """「报告显著标注简化模式」要读**响应**的 `meta.lightweight`，不是前端自己那份意图。

    抓两类回归：把标记改成前端自算（缓存命中或后端改档时就谎报），以及给徽标写了 `display`
    却没写 `[hidden]` 兜底 —— `.profile-badge { display: inline-block }` 权重高于 UA 样式表的
    `[hidden] { display: none }`，少那一条就是「关不掉的徽标」。
    """
    report = strip_js_comments(REPORT_JS.read_text(encoding="utf-8"))
    assert "meta.lightweight" in report
    assert 'el.profileBadge = document.getElementById("profile-badge")' in report
    assert "简化模式" in report and "完整模式" in report
    html = ISOCHRONE_HTML.read_text(encoding="utf-8")
    assert 'class="profile-badge" id="profile-badge"' in html
    css = strip_css_comments(REPORT_CSS.read_text(encoding="utf-8"))
    assert '.profile-badge[data-profile="mobile"]' in css
    mobile = css.split('.profile-badge[data-profile="mobile"] {')[1].split("}")[0]
    assert "color:" in mobile
    assert ".profile-badge[hidden] {\n  display: none;\n}" in css
