/* Hash-routed screens share the existing map/report state. Navigation never fetches data. */
"use strict";

(function () {
  if (!document.body.classList.contains("workspace")) return;
  const pages = {
    setup: ["新建体检", "选好地点与参数，一次体检了解身边的生活配套。"],
    map: ["生活圈地图", "查看可达范围、设施点位与服务盲区；图层可按需开关。"],
    report: ["体检报告", "先看综合结论，再了解六个维度的配套能力。"],
    facilities: ["设施明细", "按类别筛选、按表头排序，查找本次体检返回的设施。"],
    blind: ["服务盲区", "查看缺失的服务类别，定位需要关注的区域。"],
    details: ["运行详情", "运行进度、边界顶点、请求计量与预算说明。"],
    settings: ["系统设置", "管理数据模式与本机地图配置；默认设置即可体验完整流程。"],
  };
  const title = document.getElementById("workspace-title");
  const description = document.getElementById("workspace-description");
  const status = document.getElementById("workspace-status-text");
  const views = Array.from(document.querySelectorAll("[data-view]"));
  let active = "";

  function showPage(focus) {
    let route = location.hash.slice(1) || "setup";
    if (!Object.prototype.hasOwnProperty.call(pages, route)) {
      route = "setup";
      history.replaceState(null, "", "#setup");
    }
    const changed = route !== active;
    active = route;
    for (const node of views) node.hidden = !node.dataset.view.split(" ").includes(route);
    for (const link of document.querySelectorAll("[data-route]")) {
      if (link.dataset.route === route) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    title.textContent = pages[route][0];
    description.textContent = pages[route][1];
    document.title = `CircleDoc · ${pages[route][0]}`;
    document.body.dataset.page = route;
    if (route === "map" && changed && window.CircleDocMap) {
      // Hidden stages have no dimensions. Re-measure after restoring the map screen.
      window.CircleDocMap.refreshViewport();
    }
    if (changed && focus) {
      title.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "instant" });
    }
  }

  function navigate(route) {
    if (!Object.prototype.hasOwnProperty.call(pages, route)) return;
    if (location.hash !== `#${route}`) {
      // pushState + synchronous rendering also allows a subsequent focusOn() to work immediately.
      history.pushState(null, "", `#${route}`);
    }
    showPage(true);
  }

  window.addEventListener("hashchange", () => showPage(true));
  window.addEventListener("popstate", () => showPage(true));
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#']");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const route = link.getAttribute("href").slice(1);
    if (!Object.prototype.hasOwnProperty.call(pages, route)) return;
    event.preventDefault();
    navigate(route);
  });
  document.getElementById("pick-point").addEventListener("click", () => navigate("map"));
  document.addEventListener("circledoc:analysis-complete", () => navigate("report"));
  document.addEventListener("circledoc:isochrone-complete", () => navigate("map"));

  // Surface the original request messages on every screen, including failures. No fake progress.
  const analysisHint = document.getElementById("analysis-hint");
  const runHint = document.getElementById("run-hint");
  for (const hint of [analysisHint, runHint]) {
    new MutationObserver(() => { status.textContent = hint.textContent; })
      .observe(hint, { childList: true, characterData: true, subtree: true });
  }
  window.CircleDocWorkspace = { navigate, getPage: () => active };
  showPage(false);
})();
