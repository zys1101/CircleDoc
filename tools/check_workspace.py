"""可选的真实浏览器回归（需 pip install playwright）。

先启动零配置 / mock 服务，然后运行：
  python tools/check_workspace.py --base-url http://127.0.0.1:8000
可传 --browser-path 使用已有 Chromium；否则需 playwright install chromium。
脚本只调用本地服务，不加载外部地图；不写结果或截图进仓库。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright


def check(base_url: str, browser_path: str | None) -> None:
    with sync_playwright() as playwright, TemporaryDirectory() as temporary:
        options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if browser_path:
            options["executable_path"] = browser_path
        browser = playwright.chromium.launch(**options)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        posts = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: posts.append(request.url) if request.method == "POST" else None,
        )
        page.goto(f"{base_url}/isochrone.html#facilities")
        page.wait_for_function("window.CircleDocWorkspace && window.CircleDocReport")
        assert page.locator("#report-empty").is_visible()
        assert not page.locator("#controls").is_visible()
        page.locator('#report-empty a[href="#setup"]').click()
        page.locator('[data-route="settings"]').click()
        page.locator("#data-mode").select_option("mock")
        page.locator('[data-route="setup"]').click()
        page.locator("#run-analysis").click()
        page.wait_for_function('window.CircleDocWorkspace.getPage() === "report"')
        assert page.locator("#score-card").is_visible()
        result = page.evaluate("window.CircleDocReport.getCurrent()")
        assert result is not None
        count = len(posts)
        page.locator('[data-route="facilities"]').click()
        assert not page.locator("#score-card").is_visible()
        assert page.locator("#poi-table").is_visible()
        total = sum(len(group.get("pois", [])) for group in result["coverage"]["groups"])
        assert page.locator("#poi-table tbody tr").count() == min(20, total)
        if total > 20:
            page.locator("#poi-next").click()
            assert "第 2" in page.locator("#poi-page").inner_text()
            assert page.locator("#poi-table tbody tr").count() == min(20, total - 20)
        page.locator("#poi-category").select_option(index=1)
        assert "第 1" in page.locator("#poi-page").inner_text()
        page.locator('#poi-table th[data-sort="name"]').click()
        assert (
            page.locator('#poi-table th[data-sort="name"]').get_attribute("aria-sort")
            == "ascending"
        )
        page.locator('[data-route="blind"]').click()
        assert page.locator("#blind-list").is_visible()
        before = page.evaluate("window.CircleDocMap.getCenter()")
        jump = page.locator("#blind-list button").first
        if jump.count():
            jump.click()
            assert page.locator("#stage-wrap").is_visible()
            assert page.evaluate("window.CircleDocMap.getCenter()") == before
        else:
            page.locator('[data-route="map"]').click()
        page.locator("#heat").check()
        assert page.locator("#heat").is_checked()
        page.locator('[data-route="report"]').click()
        assert page.evaluate("window.CircleDocReport.getCurrent()") == result
        page.go_back()
        page.wait_for_function('window.CircleDocWorkspace.getPage() === "map"')
        page.go_forward()
        page.wait_for_function('window.CircleDocWorkspace.getPage() === "report"')
        assert len(posts) == count, "页面切换、筛选、分页与定位不得增加接口请求"
        # Downloads contain the full result, not just the visible pagination/filter slice.
        with page.expect_download() as pending:
            page.locator("#export-json").click()
        path = Path(temporary) / "report.json"
        pending.value.save_as(path)
        exported = json.loads(path.read_text(encoding="utf-8"))
        assert exported["score"] == result["score"]
        with page.expect_download() as pending:
            page.locator("#export-csv").click()
        path = Path(temporary) / "pois.csv"
        pending.value.save_as(path)
        assert len(path.read_text(encoding="utf-8-sig").splitlines()) > total
        for width in [390, 768, 1024, 1440]:
            page.set_viewport_size({"width": width, "height": 844})
            for route in ["setup", "map", "report", "facilities", "blind", "details", "settings"]:
                page.evaluate("route => window.CircleDocWorkspace.navigate(route)", route)
                assert (
                    page.locator(f'[data-route="{route}"]').get_attribute("aria-current") == "page"
                )
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    width,
                    route,
                )
        # The partial calculation opens the map without replacing the last full report.
        page.evaluate('window.CircleDocWorkspace.navigate("setup")')
        page.locator("#run").click()
        page.wait_for_function('window.CircleDocWorkspace.getPage() === "map"')
        assert page.evaluate("window.CircleDocReport.getCurrent()") == result
        # An API failure remains visible globally.
        page.route(
            "**/api/analysis",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body='{"ok":false,"message":"浏览器回归：服务暂不可用"}',
            ),
        )
        page.evaluate('window.CircleDocWorkspace.navigate("setup")')
        page.locator("#run-analysis").click()
        page.wait_for_function(
            'document.getElementById("workspace-status-text").textContent.includes("服务暂不可用")'
        )
        assert page.evaluate("window.CircleDocReport.getCurrent()") == result
        assert page.evaluate("window.CircleDocWorkspace.getPage()") == "setup"
        assert page.locator("#run-analysis").is_enabled()
        page.goto(f"{base_url}/isochrone.html#unknown")
        page.wait_for_function('window.CircleDocWorkspace.getPage() === "setup"')
        page.goto(f"{base_url}/")
        assert page.locator('a[href="./isochrone.html#setup"]').first.is_visible()
        assert not errors, errors
        browser.close()
        print("工作台浏览器回归通过：导航、结果保留、分页、定位、导出、响应式、失败提示。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--browser-path")
    args = parser.parse_args()
    check(args.base_url.rstrip("/"), args.browser_path)
