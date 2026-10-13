"""工作台页面划分契约；浏览器行为另由 tools/check_workspace.py 验证。"""

import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
ROUTES = {"setup", "map", "report", "facilities", "blind", "details", "settings"}


class WorkspaceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.routes = set()
        self.views = set()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if "id" in data:
            self.ids.append(data["id"])
        if "data-route" in data:
            self.routes.add(data["data-route"])
            assert data.get("href") == "#" + data["data-route"]
        if "data-view" in data:
            self.views.update(data["data-view"].split())
        if tag == "script" or (tag == "link" and data.get("rel") == "stylesheet"):
            self.refs.append(data.get("src", data.get("href")))


def parse_workspace():
    parser = WorkspaceParser()
    parser.feed((WEB / "isochrone.html").read_text(encoding="utf-8"))
    return parser


def test_every_workspace_route_has_a_view_and_navigation():
    parser = parse_workspace()
    assert parser.routes == ROUTES
    assert parser.views == ROUTES
    assert len(parser.ids) == len(set(parser.ids)), "页面拆分不能复制有 id 的业务节点"
    js = (WEB / "workspace.js").read_text(encoding="utf-8")
    for route in ROUTES:
        assert re.search(rf"\b{route}: \[", js)


def test_workspace_dom_and_assets_are_local_and_complete():
    parser = parse_workspace()
    js = (WEB / "workspace.js").read_text(encoding="utf-8")
    ids = re.findall(r'getElementById\("([^"]+)"\)', js)
    assert set(ids) <= set(parser.ids)
    for ref in parser.refs:
        assert ref.startswith("./"), f"工作台不得加载外部资源：{ref}"
        assert (WEB / ref[2:]).is_file()
    assert sum((WEB / ref[2:]).stat().st_size for ref in parser.refs) <= 500 * 1024


def test_workspace_navigation_does_not_fetch_or_duplicate_results():
    js = (WEB / "workspace.js").read_text(encoding="utf-8")
    assert "fetch(" not in js
    assert "history.pushState" in js
    assert '"popstate"' in js and '"hashchange"' in js
    assert 'setAttribute("aria-current", "page")' in js
    assert "refreshViewport()" in js
    assert "circledoc:analysis-complete" in js
    assert "circledoc:isochrone-complete" in js
    css = (WEB / "workspace.css").read_text(encoding="utf-8")
    assert ".workspace [hidden] { display: none !important; }" in css


def test_pagination_controls_preserve_single_analysis_export():
    parser = parse_workspace()
    assert {"poi-prev", "poi-next", "poi-page"} <= set(parser.ids)
    js = (WEB / "report.js").read_text(encoding="utf-8")
    assert "const POI_PAGE_SIZE = 20" in js
    assert "ordered.slice(start, start + POI_PAGE_SIZE)" in js
    assert "poiPage = 1" in js
    # Map navigation must not turn a blind-spot location into a new analysis origin.
    jump = js[js.index('const jump = node("button"') : js.index("item.appendChild(jump)")]
    assert "focusOn(center)" in jump
    assert "setCenter(" not in jump
