/**
 * 一次性验收脚本：在最小 DOM 打桩下真跑 `web/isochrone.js` 的 **SVG 舞台绘制路径**。
 *
 * 为什么需要它：`tools/report_dom_smoke.mjs` 只证明了「报告把 POI 与盲区块交给了地图」，
 * 至于**地图有没有真的画出来**，它管不到。而 FR-212（POI 上图）与 FR-222（盲区灰色区域上图）
 * 是 §6.1 那 40 % 里的 P0 条款 —— 「交接了」与「画出来了」是两件事，必须分别验证。
 *
 * 用法：node tools/map_layers_smoke.mjs <analysis-report.json>
 *   （report 用 `POST /api/analysis` 的真实响应；本脚本只读 `isochrone` / `coverage` / `blind_spots` 三段）
 *
 * ⚠️ 打桩的边界（诚实登记）：这里模拟的是 **SVG 降级引擎**那条路径，也就是零配置演示
 * 实际会走的那条。JSAPI 实底图那条路径需要真实 `api.map.baidu.com`，本环境禁真实出网，
 * 只做了代码审读与同构实现（两处都读同一份 `BLIND_STYLE` / `DIMENSION_STYLE`）。
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");

const reportPath = process.argv[2];
if (!reportPath) {
  console.error("用法：node tools/map_layers_smoke.mjs <analysis-report.json>");
  process.exit(2);
}
const report = JSON.parse(readFileSync(reportPath, "utf-8"));

// ---------------------------------------------------------------------------------------------
// 最小 DOM：按 id 自动创建元素（isochrone.js 取了几十个 id，逐个枚举只会变成维护负担）
// ---------------------------------------------------------------------------------------------
let nodeCount = 0;

class ClassList {
  constructor(node) {
    this.node = node;
  }
  _set() {
    return new Set(String(this.node.className || "").split(/\s+/).filter(Boolean));
  }
  add(...names) {
    const set = this._set();
    names.forEach((n) => set.add(n));
    this.node.className = [...set].join(" ");
  }
  remove(...names) {
    const set = this._set();
    names.forEach((n) => set.delete(n));
    this.node.className = [...set].join(" ");
  }
  contains(name) {
    return this._set().has(name);
  }
  toggle(name, force) {
    const want = force === undefined ? !this.contains(name) : Boolean(force);
    if (want) this.add(name);
    else this.remove(name);
    return want;
  }
}

class Element {
  constructor(tag, ns) {
    this.tagName = String(tag).toUpperCase();
    this.namespaceURI = ns || null;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this._text = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.options = [];
    this.selectedIndex = -1;
    this._id = "";
    this._queryCache = new Map();
    nodeCount += 1;
  }
  get classList() {
    return new ClassList(this);
  }
  get firstChild() {
    return this.children[0] || null;
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = v === undefined || v === null ? "" : String(v);
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "class") this.className = String(value);
    if (key === "id") this.id = String(value);
  }
  getAttribute(key) {
    return this.attributes[key];
  }
  /** 只支持 `tag` / `tag[attr]` / `[attr]`：桩越聪明，越会掩盖被测代码的问题。 */
  descendants(out = []) {
    for (const c of this.children) {
      out.push(c);
      if (c && typeof c.descendants === "function") c.descendants(out);
    }
    return out;
  }
  querySelectorAll(selector) {
    return this.descendants().filter((item) => matches(item, selector));
  }
  /**
   * 真实 DOM 里 `table.querySelector("tbody")` 每次都回同一个节点。这里按选择器记忆化，
   * 否则 render 写进一个实例、断言读另一个实例，永远读到空。
   */
  querySelector(selector) {
    if (!this._queryCache.has(selector)) {
      const found = this.querySelectorAll(selector)[0];
      if (found) {
        this._queryCache.set(selector, found);
      } else {
        const created = makeElement(selector.replace(/[[\].]/g, "") || "div");
        this.appendChild(created);
        this._queryCache.set(selector, created);
      }
    }
    return this._queryCache.get(selector);
  }
  appendChild(child) {
    this.children.push(child);
    if (child && child.tagName === "OPTION") {
      this.options.push(child);
      if (this.selectedIndex < 0) this.selectedIndex = 0;
    }
    return child;
  }
  append(...nodes) {
    nodes.forEach((n) => this.appendChild(n));
  }
  /** 真实 DOM 的 `replaceChildren()`：清空并可选地追加新子节点。 */
  replaceChildren(...nodes) {
    this.children = [];
    this._queryCache = new Map();
    this.append(...nodes);
  }
  removeChild(child) {
    this.children = this.children.filter((i) => i !== child);
    return child;
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter((h) => h !== handler);
  }
  dispatch(type, extra) {
    const event = Object.assign({ type, currentTarget: this, target: this }, extra || {});
    (this.listeners[type] || []).forEach((h) => h(event));
  }
  getBoundingClientRect() {
    return { width: 800, height: 520, top: 0, left: 0, right: 800, bottom: 520 };
  }
  setPointerCapture() {}
  releasePointerCapture() {}
  getBBox() {
    return { x: 0, y: 0, width: 800, height: 520 };
  }
}

function matches(element, selector) {
  if (!element || !element.tagName) return false;
  const m = /^([a-zA-Z]*)(?:\[([^\]=]+)(?:=([^\]]+))?\])?$/.exec(String(selector).trim());
  if (!m) return false;
  const [, tag, attr, expected] = m;
  if (tag && element.tagName !== tag.toUpperCase()) return false;
  if (attr && element.getAttribute(attr) === undefined) return false;
  if (attr && expected !== undefined) {
    const want = expected.replace(/^["']|["']$/g, "");
    if (String(element.getAttribute(attr)) !== want) return false;
  }
  return Boolean(tag || attr);
}

const byId = new Map();
function makeElement(tag, ns) {
  const element = new Element(tag, ns);
  Object.defineProperty(element, "id", {
    get() {
      return this._id;
    },
    set(value) {
      this._id = value;
      byId.set(value, this);
    },
  });
  return element;
}
/** 按 id 取值；不存在就造一个 —— isochrone.js 取了几十个 id，枚举它们只会变成维护负担。 */
function getById(id) {
  if (!byId.has(id)) {
    const tag = { stage: "svg", mode: "select", minutes: "select", "data-mode": "select" }[id] || "div";
    const element = makeElement(tag);
    element.id = id;
  }
  return byId.get(id);
}

// 几个必须预置初值的控件（isochrone.js 的 boot() 会读它们）
getById("mode").value = "walking";
getById("minutes").value = "15";
getById("data-mode").value = "mock";
for (const id of ["heat", "compare", "reference", "show-pois", "show-blind"]) {
  getById(id).checked = false;
}
getById("show-pois").checked = true;
getById("show-blind").checked = true;

const documentStub = {
  readyState: "complete",
  body: makeElement("body"),
  createElement: (tag) => makeElement(tag),
  createElementNS: (ns, tag) => makeElement(tag, ns),
  getElementById: (id) => getById(id),
  querySelector: () => null,
  /**
   * 🔴 文档级 `querySelectorAll` 一律回空数组，理由要说清：
   * `isochrone.js` 现在只有两条文档级查询 —— FR-104 的 `#categories input.cat`
   * 与 FR-106 的 `#sheet-bar .sheet-tab`。逐条枚举七个 `data-cat` 等于把后端词表
   * 在测试桩里抄一份（改词表时必漏），而抽屉那三条更不该由这个只管「画得对不对」的
   * 桩来兜。空数组在这两处都是**等价于「全选 + 桌面态」**的真实语义：
   * `selectedCategories()` 折成 `null`（不发 `categories` 键），`sheetPanels` 为空、
   * `syncSheetMode()` 停在 desktop。两条的接线契约分别由
   * `tests/test_web_smoke.py::test_category_checkboxes_match_the_backend_wordlist`
   * 和同文件的 FR-106 那组断言对着**真 HTML** 核，不靠这个桩。
   */
  querySelectorAll: () => [],
  addEventListener: () => {},
};

// ---------------------------------------------------------------------------------------------
// 跑 isochrone.js
// ---------------------------------------------------------------------------------------------
const healthStub = {
  status: "offline",
  ok: true,
  data_mode: "mock",
  data_source: "sample",
  server_ak_type: "not_configured",
  browser_ak_type: "not_configured",
  baidu_reachable: false,
  banner: { show: true, level: "warning", title: "离线报告模式", message: "无 AK", detail_messages: [], cta_hint: "配置 AK" },
  services: [],
  config: { data_mode_in_effect: "mock" },
  endpoints: {},
};

const sandbox = {
  document: documentStub,
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  // headless 无 vsync：把 rAF 降级成宏任务，让渲染队列能排空（M2 验收用的是同一手法）。
  requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
  navigator: { onLine: false, userAgent: "node" },
  location: { search: "", href: "http://127.0.0.1/isochrone.html" },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  fetch: async (url) => {
    const path = String(url);
    if (path.includes("/api/health")) {
      return { ok: true, status: 200, json: async () => healthStub };
    }
    return { ok: false, status: 404, json: async () => ({ message: `harness 未打桩：${path}` }) };
  },
  Promise,
  Date,
  Math,
  JSON,
  Object,
  Array,
  Number,
  String,
  Boolean,
  Error,
  isNaN,
  parseFloat,
  parseInt,
  // ⚠️ `URLSearchParams` / `URL` 是 **宿主全局**（Node 自带），不是 ECMAScript 内建，
  // 所以 `vm.createContext()` 造出来的新 realm 里**没有**它们。`readQueryFlags()` 正是
  // 用 `new URLSearchParams(window.location.search)` 读 `?profile=m` 的，不补就会在 boot 里炸。
  URLSearchParams,
  URL,
};
sandbox.window = sandbox;
sandbox.window.matchMedia = () => ({ matches: false });
sandbox.window.addEventListener = () => {};
sandbox.window.devicePixelRatio = 1;

const context = vm.createContext(sandbox);
const source = readFileSync(resolve(root, "web", "isochrone.js"), "utf-8");

let failures = 0;
let skipped = 0;
const SKIP = Symbol("skip");
function check(name, fn) {
  try {
    const detail = fn();
    if (detail === SKIP) {
      skipped += 1;
      console.log(`  SKIP ${name} — 本份数据不覆盖该情形`);
      return;
    }
    console.log(`  OK   ${name}${detail ? ` — ${detail}` : ""}`);
  } catch (error) {
    failures += 1;
    console.log(`  FAIL ${name} — ${error && error.message}`);
  }
}

console.log("=== isochrone.js SVG 舞台绘制路径（真实 /api/analysis 响应）===");
try {
  vm.runInContext(source, context, { filename: "isochrone.js" });
} catch (error) {
  console.log(`  FAIL 加载 isochrone.js — ${error && error.stack}`);
  process.exit(1);
}

/** 让 rAF 队列排空。 */
const drain = () => new Promise((r) => setTimeout(r, 60));

await drain();

check("暴露 window.CircleDocMap 且含 setOverlays", () => {
  const api = sandbox.window.CircleDocMap;
  if (!api) throw new Error("未暴露接口");
  if (typeof api.setOverlays !== "function") throw new Error("缺 setOverlays");
  return Object.keys(api).join(",");
});

// 灌入真实结果：先接等时圈，再交叠加图层
sandbox.window.CircleDocMap.adoptIsochrone(report.isochrone, { force: true });
await drain();

const pois = [];
for (const group of report.coverage.groups) {
  for (const poi of group.pois || []) pois.push(Object.assign({ category: group.key }, poi));
}
const overlaysResult = sandbox.window.CircleDocMap.setOverlays({
  pois,
  blindSpots: report.blind_spots.geojson,
});
await drain();

const stage = getById("stage");
const sceneRoot = stage.querySelectorAll("g").find((g) => g.getAttribute("id") === "scene-root");

check("setOverlays 回传绘制计数", () => {
  if (!overlaysResult) throw new Error("setOverlays 没回值");
  if (!overlaysResult.pois) throw new Error("一个 POI 都没接");
  return `pois=${overlaysResult.pois} blindBlocks=${overlaysResult.blindBlocks}`;
});

check("FR-212：POI 真的画进了 #g-pois", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-pois");
  if (!g) throw new Error("没有 #g-pois 图层");
  const marks = g.children.filter((c) => c.tagName === "G" && c.className.includes("poi"));
  const inRing = pois.filter((p) => p.in_polygon).length;
  if (marks.length !== inRing) {
    throw new Error(`画了 ${marks.length} 个，报告里圈内有 ${inRing} 个`);
  }
  if (!marks.length) return SKIP;
  return `${marks.length} 个标记`;
});

check("FR-212：每个 POI 标记都按维度上了色，且有可点热区", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-pois");
  const marks = g.children.filter((c) => c.tagName === "G" && c.className.includes("poi"));
  if (!marks.length) return SKIP;
  const colors = new Set();
  for (const mark of marks) {
    if (!mark.listeners.click || !mark.listeners.click.length) throw new Error("标记没有 click 监听");
    const shape = mark.children[0];
    if (!shape) throw new Error("标记里没有图形");
    const fill = shape.getAttribute("fill");
    if (!fill) throw new Error("图形没有 fill");
    colors.add(fill);
    if (!mark.getAttribute("aria-label")) throw new Error("标记缺 aria-label（键盘/读屏不可达）");
  }
  return `${marks.length} 个标记、${colors.size} 种维度色`;
});

check("FR-212：点击 POI 弹出的 tooltip 含名称 / 类别 / 实测步行时间", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-pois");
  const marks = g.children.filter((c) => c.tagName === "G" && c.className.includes("poi"));
  if (!marks.length) return SKIP;
  marks[0].dispatch("click", { clientX: 100, clientY: 100 });
  const tooltip = getById("tooltip");
  const text = tooltip.textContent;
  if (tooltip.hidden) throw new Error("tooltip 没有显示");
  for (const must of ["类别：", "实测步行时间：", "官方分类：", "直线距离："]) {
    if (!text.includes(must)) throw new Error(`tooltip 缺「${must}」：${text.slice(0, 120)}`);
  }
  return text.slice(0, 70);
});

check("FR-212：未实测的设施在 tooltip 里明说原因，不写成 0 分钟", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-pois");
  const marks = g.children.filter((c) => c.tagName === "G" && c.className.includes("poi"));
  const unmeasured = pois.filter((p) => p.in_polygon && p.unmeasured);
  if (!unmeasured.length) return SKIP;
  // 找到那个未实测点对应的标记：按 aria-label 里的名称匹配
  const target = marks.find((m) =>
    String(m.getAttribute("aria-label") || "").includes(unmeasured[0].name),
  );
  if (!target) throw new Error("找不到未实测点对应的标记");
  // 标记上应带虚线描边（视觉上把「未实测」与「测到了」分开）
  const shape = target.children[0];
  if (!shape.getAttribute("stroke-dasharray")) throw new Error("未实测标记没有虚线描边");
  target.dispatch("click", { clientX: 120, clientY: 120 });
  const text = getById("tooltip").textContent;
  if (!/未实测/.test(text)) throw new Error(`tooltip 未说明未实测：${text.slice(0, 120)}`);
  if (/实测步行时间：0(\.0)?\s*min/.test(text)) throw new Error("把未实测渲染成了 0 分钟");
  return "虚线描边 + 文案说明";
});

check("FR-222：盲区块真的画进了 #g-blind（含 MultiPolygon 的分离环）", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-blind");
  if (!g) throw new Error("没有 #g-blind 图层");
  const paths = g.children.filter((c) => c.tagName === "PATH" && c.className.includes("blind-block"));
  const blocks = report.blind_spots.blocks || [];
  // 🔴 一个块可能输出多个环（后端在连通块边界不连通时会发 MultiPolygon），
  // 所以断言的是「环总数」而不是「块总数」——只按 Polygon 取 coordinates[0]
  // 会把 MultiPolygon 那个块整个丢掉（实测交道口 4 块里就有 1 块是 3 个分离环）。
  const features = (report.blind_spots.geojson || {}).features || [];
  let expectedRings = 0;
  for (const f of features) {
    const geom = f.geometry || {};
    if (geom.type === "MultiPolygon") expectedRings += (geom.coordinates || []).length;
    else if (geom.type === "Polygon") expectedRings += 1;
  }
  if (paths.length !== expectedRings) {
    throw new Error(`画了 ${paths.length} 个多边形，GeoJSON 里有 ${expectedRings} 个环（${blocks.length} 块）`);
  }
  if (paths.length < blocks.length) {
    throw new Error(`多边形数 ${paths.length} 少于块数 ${blocks.length}，有块被丢掉`);
  }
  if (!paths.length) return SKIP;
  const multi = features.filter((f) => (f.geometry || {}).type === "MultiPolygon").length;
  return `${blocks.length} 块 → ${paths.length} 个多边形${multi ? `（含 ${multi} 个 MultiPolygon）` : ""}`;
});

check("FR-222：两级阈值同屏可分（完全盲区灰 / 配套不足橙）", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-blind");
  const paths = g.children.filter((c) => c.tagName === "PATH" && c.className.includes("blind-block"));
  if (!paths.length) return SKIP;
  const byClass = {};
  for (const p of paths) {
    const blind = p.className.includes("blind-blind");
    const insuff = p.className.includes("blind-insufficient");
    if (!blind && !insuff) throw new Error(`class 未标出分级：${p.className}`);
    const fill = p.getAttribute("fill");
    if (!fill) throw new Error("盲区块没有 fill");
    byClass[blind ? "blind" : "insufficient"] = fill;
  }
  const levels = Object.keys(byClass);
  if (levels.length > 1 && byClass.blind === byClass.insufficient) {
    throw new Error("两级阈值用了同一个颜色");
  }
  return `${levels.map((k) => `${k}=${byClass[k]}`).join(" / ")}`;
});

check("FR-222：点击盲区块弹出的 tooltip 含缺失类别与最近可用设施（FR-223）", () => {
  const g = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-blind");
  const paths = g.children.filter((c) => c.tagName === "PATH" && c.className.includes("blind-block"));
  if (!paths.length) return SKIP;
  paths[0].dispatch("click", { clientX: 200, clientY: 200 });
  const text = getById("tooltip").textContent;
  for (const must of ["分级：", "缺失类别：", "面积：", "最近可用设施："]) {
    if (!text.includes(must)) throw new Error(`tooltip 缺「${must}」：${text.slice(0, 140)}`);
  }
  return text.slice(0, 76);
});

check("两层都在等时圈之上渲染（否则会被多边形盖住）", () => {
  if (!sceneRoot) throw new Error("找不到 #scene-root");
  const ids = sceneRoot.children.map((c) => c.getAttribute("id"));
  const iPoly = ids.indexOf("g-poly");
  const iBlind = ids.indexOf("g-blind");
  const iPois = ids.indexOf("g-pois");
  if (iPoly < 0 || iBlind < 0 || iPois < 0) throw new Error(`图层缺失：${ids.join(",")}`);
  if (!(iPoly < iBlind && iBlind < iPois)) {
    throw new Error(`叠放顺序不对：${ids.join(" → ")}`);
  }
  return ids.join(" → ");
});

check("FR-222：图例说明了盲区两级阈值与 POI 色板", () => {
  const legend = getById("legend").textContent;
  for (const must of ["完全盲区", "配套不足", "基础医疗", "公共交通", "虚线描边＝该设施未实测"]) {
    if (!legend.includes(must)) throw new Error(`图例缺「${must}」`);
  }
  return "两级阈值 + 7 类色板 + 未实测说明";
});

getById("show-pois").checked = false;
getById("show-blind").checked = false;
// `change` 只是把渲染排进 rAF 队列，必须等它排空再断言 —— 否则测的是「还没来得及清」。
getById("show-pois").dispatch("change");

await drain();

check("图层开关关掉后两层都被清空", () => {
  const gPois = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-pois");
  const gBlind = stage.querySelectorAll("g").find((n) => n.getAttribute("id") === "g-blind");
  if (gPois && gPois.children.length) throw new Error(`关掉后 #g-pois 还有 ${gPois.children.length} 个节点`);
  if (gBlind && gBlind.children.length) {
    throw new Error(`关掉后 #g-blind 还有 ${gBlind.children.length} 个节点`);
  }
  return "两层都已清空";
});

console.log(
  failures
    ? `\n${failures} 项失败${skipped ? `，${skipped} 项跳过` : ""}`
    : `\n全部通过${skipped ? `（${skipped} 项因数据不含该情形而跳过）` : ""}`
);
process.exit(failures ? 1 : 0);
