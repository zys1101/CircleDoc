/*
 * web/shell.js —— 「一屏一页」的视图与子页签接线。
 *
 * 为什么单独一个文件：`isochrone.js` 管地图与等时圈、`report.js` 管报告渲染，
 * 两者都不该知道「现在屏幕上是哪一页」。这一层只做版式状态，不碰数据、不发请求、
 * 不写 localStorage（导出与配额的口径都在 `report.js` 那边，别在这里开第二个持久化入口）。
 *
 * 🔴 三条实现约束，改之前先读：
 *
 *  1. **顶层视图只用 `visibility`，绝不用 `hidden` / `display: none`。**
 *     舞台尺寸是 `resizeStage()` / `sizeSheetStage()` 量 `getBoundingClientRect()` 得来的，
 *     盒子一旦从布局里摘掉就量到 0，等时圈会退回 240×220 的兜底尺寸；JSAPI 那条路径
 *     还要额外补 `checkResize()`。所以这里只往 `<html>` 写一个 `data-view`，
 *     由 `isochrone.css` 用 `visibility: hidden` 关掉不活跃那一页（保盒子、只关绘制与命中）。
 *     面板内部的分节（`.group` / `.chunk`）不含任何需要测量的几何，才用 `hidden`。
 *
 *  2. **移动端不接管。** ≤768 px 时导航归 FR-106 的把手条（`#sheet-bar`），两套导航同时
 *     管同一块面板必然打架：一个改 `visibility`、一个改 `transform`，结果是「报告被把手
 *     拉出来了、但它仍然是 invisible」。所以进入抽屉态就把 `<html>` 上的视图属性整个摘掉，
 *     让 CSS 那批选择器一条都不匹配。
 *     ⚠️ 这里**不写第二个断点数字**：抽屉的开关是 `body[data-sheet-mode]` 这个属性，
 *     768 那个真值只有 `isochrone.js: SHEET_BREAKPOINT_PX` 一处。本文件用 MutationObserver
 *     跟着它走，而不是自己再 `matchMedia` 一遍 —— `tests/test_web_smoke.py` 专门钉了
 *     「isochrone.js 里只许出现一条 max-width」，多一处真值迟早分叉。
 *
 *  3. **无脚本 = 全部展开。** 所有隐藏动作都由本文件发起（`hidden` 属性 / `data-view`），
 *     脚本没跑起来就是逐段堆叠的长页，读得到也滚得到。`<head>` 里那句
 *     `documentElement.classList.add("shell-js")` 只负责让首屏不闪，不承担功能开关。
 */

(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;
  var workbench = document.getElementById("workbench");
  var reportPanel = document.getElementById("report");
  // 结构不在（改版前的缓存页 / 半截加载）：整套跳过，页面回到逐段堆叠的旧版式。
  if (!workbench || !reportPanel) return;

  var VIEWS = { map: workbench, report: reportPanel };
  var viewStrip = document.querySelector('[data-strip="view"]');
  var viewButtons = viewStrip
    ? Array.prototype.slice.call(viewStrip.querySelectorAll("[data-view]"))
    : [];
  var view = "map";
  // 必须在首次 paint 之前读到：`isochrone.js` 的 boot() 已经写好这个属性，
  // 先按 desktop 画一帧再纠正，手机上就是「地图被 visibility 关掉一瞬」的白屏闪烁。
  var sheetMode = body.getAttribute("data-sheet-mode") || "desktop";

  function inSheet() {
    return sheetMode === "mobile";
  }

  function paintView() {
    if (inSheet()) root.removeAttribute("data-view");
    else root.setAttribute("data-view", view);
    viewButtons.forEach(function (button) {
      var on = button.getAttribute("data-view") === view;
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /**
   * @param {string} key map / report
   * @param {boolean} moveFocus 只有用户主动翻页才送焦点。体检结果是异步落地的，
   *   跑完那一刻用户可能正在别的输入框里打字，把焦点抢走比不通知更糟。
   */
  function showView(key, moveFocus) {
    if (!VIEWS[key]) return;
    var changed = key !== view;
    view = key;
    paintView();
    if (!changed || moveFocus === false || inSheet()) return;
    var heading = VIEWS[key].querySelector("h2");
    if (!heading) return;
    if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
    if (heading.focus) heading.focus();
  }

  // -------------------------------------------------------------------------------------------
  // 面板内部分节（`.group` / `.chunk`）：两处走同一套接线
  // -------------------------------------------------------------------------------------------
  var idSeq = 0;
  function nextId(prefix) {
    idSeq += 1;
    return prefix + "-" + idSeq;
  }

  function panelFor(scope, attr, key) {
    return scope.querySelector("." + attr + '[data-' + attr + '="' + key + '"]');
  }

  /**
   * 把一条 `.tabstrip` 升格成真正的 ARIA tabs。
   *
   * 角色是**运行时**才加上去的：无脚本时这些按钮只是普通按钮、分节全在正常流里，
   * 提前在 HTML 里写 `role="tablist"` 会让读屏对着一个「翻不过去」的结构念 tab 语义。
   */
  function wireStrip(strip, attr, scope) {
    var tabs = Array.prototype.slice.call(strip.querySelectorAll("[data-" + attr + "]"));
    var panels = tabs
      .map(function (tab) {
        var key = tab.getAttribute("data-" + attr);
        return { tab: tab, key: key, panel: panelFor(scope, attr, key) };
      })
      .filter(function (item) {
        return !!item.panel;
      });
    if (panels.length < 2) return;
    var active = panels[0].key;

    function select(key, moveFocus) {
      var hit = null;
      panels.forEach(function (item) {
        var on = item.key === key;
        if (on) hit = item;
        item.tab.setAttribute("aria-selected", on ? "true" : "false");
        item.tab.setAttribute("tabindex", on ? "0" : "-1");
        // 分节里有输入框，`hidden` 是唯一能把它们一并移出 Tab 序与读屏树的做法。
        if (on) item.panel.removeAttribute("hidden");
        else item.panel.setAttribute("hidden", "");
      });
      if (!hit) return;
      active = key;
      if (moveFocus && hit.tab.focus) hit.tab.focus();
    }

    panels.forEach(function (item) {
      item.tab.setAttribute("role", "tab");
      if (!item.tab.id) item.tab.id = nextId("tab-" + item.key);
      if (!item.panel.id) item.panel.id = nextId("panel-" + item.key);
      item.panel.setAttribute("role", "tabpanel");
      item.panel.setAttribute("aria-labelledby", item.tab.id);
      // tabpanel 要能进 Tab 序，否则键盘用户跳进面板后只能靠里面的控件接着走。
      if (!item.panel.hasAttribute("tabindex")) item.panel.setAttribute("tabindex", "0");
      item.tab.setAttribute("aria-controls", item.panel.id);
      item.tab.addEventListener("click", function () {
        select(item.key, false);
      });
    });

    strip.setAttribute("role", "tablist");
    strip.addEventListener("keydown", function (event) {
      var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      var keys = panels.map(function (item) {
        return item.key;
      });
      var index = -1;
      if (event.key === "Home") index = 0;
      else if (event.key === "End") index = panels.length - 1;
      else if (step) index = (keys.indexOf(active) + step + panels.length) % panels.length;
      else return;
      event.preventDefault();
      select(panels[index].key, true);
    });

    select(active, false);
  }

  // -------------------------------------------------------------------------------------------
  // 接线
  // -------------------------------------------------------------------------------------------
  viewButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      showView(button.getAttribute("data-view"), true);
    });
  });
  paintView();

  [
    ["controls", "group"],
    ["readout", "chunk"],
    ["report", "chunk"],
  ].forEach(function (spec) {
    var strip = document.querySelector('[data-strip="' + spec[0] + '"]');
    var scope = document.getElementById(spec[0]);
    if (strip && scope) wireStrip(strip, spec[1], scope);
  });

  new MutationObserver(function () {
    var next = body.getAttribute("data-sheet-mode") || "desktop";
    if (next === sheetMode) return;
    sheetMode = next;
    paintView();
  }).observe(body, { attributes: true, attributeFilter: ["data-sheet-mode"] });

  // 体检跑完自动翻到报告页。`report.js` 成功分支里那句 `openSheet("report")` 管手机抽屉，
  // 这一句管桌面的整屏页 —— 两条路径各说一次，是因为「谁在移动端说话」只能有一个答案。
  // 刻意不做成「观察 #report-body 的 hidden 变化」：隐式触发看不出是谁改的，出问题时两头查。
  window.CircleDocShell = {
    showView: showView,
    currentView: function () {
      return view;
    },
  };
})();
