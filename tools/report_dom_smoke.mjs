/**
 * 一次性验收脚本：在最小 DOM 打桩下真跑 `web/report.js` 的渲染路径。
 *
 * 目的：`node --check` 只验语法，抓不到「函数名拼错 / 属性不存在」这类**运行时**引用错误；
 * 而报告面板有 25 个 DOM 节点与两套手绘 SVG，一旦某处炸了就是整块空白、且控制台只在
 * appendChild 那一刻才报。这里用真实 `/api/analysis` 响应喂进去，把渲染路径整个走一遍。
 *
 * 用法：node tools/report_dom_smoke.mjs [report.json]（不传则用内置样本）
 * 用完即删（不是交付物，也不进 CI —— CI 里没有 node）。
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");

/**
 * 无参数时用的内置样本。刻意把三类「最容易渲染错」的情形都塞进来：
 *   · `nearest_minutes: null`（没测到 —— 画成 0 分钟就变成「满分」）
 *   · `measured_minutes: null` 的 POI（明细表必须写「未实测」而不是 0.0 min）
 *   · 两级盲区（`blind` / `insufficient`）同时存在
 * 这样即使不跑后端也能验到回归点；有真实响应时传路径即可。
 */
const FIXTURE = {
  ok: true,
  meta: {
    center: [116.404, 39.915],
    mode: "walking",
    modes: ["walking"],
    minutes: 15,
    data_source: "sample",
    degraded_reason: "mock_mode",
    generated_at: "2026-10-05T00:00:00+00:00",
  },
  coverage: {
    groups: [
      {
        key: "medical",
        label: "基础医疗",
        scored: true,
        weight: 0.2,
        count_in: 0,
        threshold_count: 2,
        nearest_minutes: null,
        nearest_reason: "no_poi_in_polygon",
        pois: [
          {
            uid: "m1",
            name: "样例-药店-01",
            subcat: "pharmacy",
            official_path: "医疗 > 药店",
            lnglat: [116.41, 39.92],
            distance_m: 800,
            in_polygon: false,
            measured_minutes: null,
            unmeasured: true,
            unmeasured_reason: "outside_polygon",
          },
        ],
      },
      {
        key: "food",
        label: "餐饮服务",
        scored: true,
        weight: 0.1,
        count_in: 8,
        threshold_count: 5,
        nearest_minutes: 2.4,
        pois: [
          {
            uid: "f1",
            name: "样例-中餐厅-01",
            subcat: "restaurant",
            official_path: "美食 > 中餐厅",
            lnglat: [116.405, 39.916],
            distance_m: 120,
            in_polygon: true,
            measured_minutes: 2.4,
            unmeasured: false,
          },
        ],
      },
    ],
    meta: {
      selection: "fair",
      selection_allocation: { medical: 1, food: 1 },
      has_ring: true,
    },
  },
  blind_spots: {
    count: 1,
    area_pct: 12.5,
    area_km2: 0.4,
    grid: { step_m: 250, radius_m: 1000, candidates: 88, inside_ring: 61 },
    blocks: [
      {
        level: "blind",
        cells: 12,
        area_km2: 0.75,
        missing: ["market", "pharmacy", "primary_school"],
        distance_from_center_m: 430,
        center_lnglat: [116.401, 39.913],
        nearest_facility: {
          name: "样例-超市-02",
          subcat: "supermarket",
          distance_m: 1900,
          minutes: 29.2,
          speed_kmh: 3.9,
          measured: false,
        },
      },
      {
        level: "insufficient",
        cells: 4,
        area_km2: 0.25,
        missing: ["market"],
        distance_from_center_m: 210,
        center_lnglat: [116.406, 39.917],
        nearest_facility: null,
      },
    ],
    // 真实 `/api/analysis` 会用 `services/blind_spots.py:blocks_to_geojson()` 生成这一段
    // （FR-222 上图的唯一数据来源）。样本必须带上它，否则测的是「图层交接」而不是「有没有图可交」。
    geojson: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [116.3995, 39.9115],
                [116.4025, 39.9115],
                [116.4025, 39.9145],
                [116.3995, 39.9145],
                [116.3995, 39.9115],
              ],
            ],
          },
          properties: {
            block_index: 0,
            level: "blind",
            cells: 12,
            area_km2: 0.75,
            missing: ["market", "pharmacy", "primary_school"],
            missing_count: 3,
            distance_from_center_m: 430,
            center_lnglat: [116.401, 39.913],
            nearest_facility: { name: "样例-超市-02", subcat: "supermarket", minutes: 29.2, measured: false },
          },
        },
        {
          type: "Feature",
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [116.4045, 39.9155],
                [116.4075, 39.9155],
                [116.4075, 39.9185],
                [116.4045, 39.9185],
                [116.4045, 39.9155],
              ],
            ],
          },
          properties: {
            block_index: 1,
            level: "insufficient",
            cells: 4,
            area_km2: 0.25,
            missing: ["market"],
            missing_count: 1,
            distance_from_center_m: 210,
            center_lnglat: [116.406, 39.917],
            nearest_facility: null,
          },
        },
      ],
    },
  },
  score: {
    total: 61.5,
    grade: "C",
    radar: { medical: 0, food: 88 },
    penalty: -6.25,
    blind_area_pct: 12.5,
    weight_sum: 0.95,
    normalized: true,
    total_raw: 58.4,
    grade_bands: { A: 85, B: 70, C: 55, D: 0 },
    reference_only: ["convenience"],
  },
  summary: ["最弱项：基础医疗 0 分。", "最强项：餐饮服务 88 分。", "服务盲区：1 处。"],
  usage: {
    http_requests: 0,
    quota_units: 0,
    place_calls: 0,
    route_lines: { walking: 0, riding: 0 },
    route_lines_total: 0,
    stages: { "isochrone:walking": { http_requests: 0, quota_units: 0, place_calls: 0 } },
    elapsed_ms: 42,
  },
  assertions: {
    ok: true,
    limits: { place_calls: 8, quota_units: 120, http_requests: 20 },
    checks: {
      place_calls: { value: 0, limit: 8, ok: true },
      quota_units: { value: 0, limit: 120, ok: true },
      http_requests: { value: 0, limit: 20, ok: true },
    },
  },
  notes: ["等时圈本次由程序合成的示例数据给出。"],
};

const reportPath = process.argv[2];
const report = reportPath ? JSON.parse(readFileSync(reportPath, "utf-8")) : FIXTURE;
console.log(
  reportPath ? `数据源：${reportPath}` : "数据源：内置样本（未传路径）"
);

// ---------------------------------------------------------------------------------------------
// 最小 DOM：只实现 report.js 真正用到的那几个 API。多了反而会把 bug 藏起来。
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
    names.forEach((name) => set.add(name));
    this.node.className = [...set].join(" ");
  }
  remove(...names) {
    const set = this._set();
    names.forEach((name) => set.delete(name));
    this.node.className = [...set].join(" ");
  }
  contains(name) {
    return this._set().has(name);
  }
  toggle(name, force) {
    const has = this.contains(name);
    const want = force === undefined ? !has : Boolean(force);
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
    this.value = "";
    this.options = [];
    this.selectedIndex = -1;
    this._id = "";
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
  set textContent(value) {
    this.children = [];
    this._text = value === undefined || value === null ? "" : String(value);
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    // 真实 DOM 里 `class` 属性与 `className` 是同一份数据（`el.setAttribute('class', x)`
    // 之后 `el.className === x`）。打桩不模拟这一条，会让 `svgNode(..., {class: ...})`
    // 写进去的 class 在读的时候看不见 —— 那是桩不忠实，不是被测代码的问题。
    if (key === "class") this.className = String(value);
    if (key === "id") this.id = String(value);
  }
  getAttribute(key) {
    return this.attributes[key];
  }
  /** 深度优先收集后代（不含自身）。 */
  descendants(out = []) {
    for (const child of this.children) {
      out.push(child);
      if (child && typeof child.descendants === "function") child.descendants(out);
    }
    return out;
  }
  /**
   * 只支持本测试真正用到的那几种选择器：`tag`、`tag[attr]`、`[attr]`。
   * 不实现完整 CSS —— 桩越"聪明"，越容易把被测代码的 bug 掩盖成桩的行为。
   */
  querySelectorAll(selector) {
    return this.descendants().filter((item) => matchesSelector(item, selector));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  appendChild(child) {
    this.children.push(child);
    if (child && child.tagName === "OPTION") {
      this.options.push(child);
      if (this.selectedIndex < 0) this.selectedIndex = 0;
    }
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((item) => item !== child);
    return child;
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  /**
   * 派发一个事件。**必须带上 `currentTarget` / `target`** —— 真实 DOM 会把它们指向
   * 当前监听器所属的节点，而事件委托式的写法（本项目的表头排序就是）全靠 `event.currentTarget`
   * 拿到被点的那个 `th`。不模拟这一条，测出来的是「桩缺字段」而不是被测代码的问题。
   */
  dispatch(type, extra) {
    const event = Object.assign({ type, currentTarget: this, target: this }, extra || {});
    (this.listeners[type] || []).forEach((handler) => handler(event));
  }
}

const registry = new Map();

/** `tag` / `tag[attr]` / `[attr]` 三种选择器的最小匹配器。 */
function matchesSelector(element, selector) {
  if (!element || !element.tagName) return false;
  const match = /^([a-zA-Z]*)(?:\[([^\]=]+)(?:=([^\]]+))?\])?$/.exec(String(selector).trim());
  if (!match) return false;
  const [, tag, attr, expected] = match;
  if (tag && element.tagName !== tag.toUpperCase()) return false;
  if (attr && element.getAttribute(attr) === undefined) return false;
  if (attr && expected !== undefined) {
    const want = expected.replace(/^["']|["']$/g, "");
    if (String(element.getAttribute(attr)) !== want) return false;
  }
  return Boolean(tag || attr);
}

function makeElement(tag, ns) {
  const element = new Element(tag, ns);
  Object.defineProperty(element, "id", {
    get() {
      return this._id;
    },
    set(value) {
      this._id = value;
      registry.set(value, this);
    },
  });
  return element;
}

// report.js 的 cache() 会取 25 个 id：全部预建，这样任何 id 拼错都会在 el.x 上暴露为 null。
const REQUIRED_IDS = [
  "report",
  "report-empty",
  "report-body",
  "run-analysis",
  "analysis-hint",
  "sample-select",
  "sample-note",
  "source-badge",
  "score-card",
  "summary-list",
  "radar-chart",
  "bar-chart",
  "bar-note",
  "poi-table",
  "poi-count",
  "poi-category",
  "blind-summary",
  "blind-list",
  "analysis-usage",
  "analysis-assertions",
  "analysis-notes",
  "export-json",
  "export-csv",
  "export-blind-csv",
  "export-hint",
];
REQUIRED_IDS.forEach((id) => {
  const element = makeElement(id === "sample-select" || id === "poi-category" ? "select" : "div");
  element.id = id;
});
// 两张表需要稳定的 <tbody>：真实 DOM 里 `table.querySelector("tbody")` 每次都回**同一个**
// 节点，所以打桩也必须缓存，否则 render 写进一个实例、断言读另一个实例，永远读到空表。
["poi-table", "analysis-usage"].forEach((id) => {
  const table = registry.get(id);
  const tbody = makeElement("tbody");
  table.appendChild(tbody);
  table.tbody = tbody;
});

// FR-213：明细表的 thead 与可排序表头。列与 `web/isochrone.html` 一致（含 data-sort）。
const POI_COLUMNS = [
  ["name", "名称"],
  ["category", "类别"],
  [null, "官方分类"],
  ["distance", "直线距离"],
  ["minutes", "实测步行时间"],
  ["polygon", "落圈"],
  ["measured", "实测状态"],
  [null, "经度"],
  [null, "纬度"],
];
{
  const table = registry.get("poi-table");
  const thead = makeElement("thead");
  const tr = makeElement("tr");
  for (const [sortKey, label] of POI_COLUMNS) {
    const th = makeElement("th");
    if (sortKey) th.setAttribute("data-sort", sortKey);
    th.textContent = label;
    tr.appendChild(th);
  }
  thead.appendChild(tr);
  table.appendChild(thead);
  table.thead = thead;
}

const missing = REQUIRED_IDS.filter((id) => !registry.has(id));
if (missing.length) {
  console.error("harness 自检失败：缺少 id", missing);
  process.exit(2);
}

/** `window.CircleDocMap` 的替身：只记账，不画图（画图由 isochrone.js 负责、不在本文件范围）。 */
const mapCalls = { adopted: 0, overlays: [], centers: [] };

const documentStub = {
  readyState: "complete",
  body: makeElement("body"),
  createElement: (tag) => makeElement(tag),
  createElementNS: (ns, tag) => makeElement(tag, ns),
  getElementById: (id) => registry.get(id) || null,
  addEventListener: () => {},
};

// ---------------------------------------------------------------------------------------------
// 跑 report.js
// ---------------------------------------------------------------------------------------------
const sandbox = {
  document: documentStub,
  window: {},
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  Blob: class {},
  URL: { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} },
  fetch: async (url) => {
    // 只服务两条真实路径：样例清单与一键体检。别的一律 404，避免把拼错的 URL 蒙混过去。
    const path = String(url);
    if (path.includes("/api/samples")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, samples: [], count: 0, notes: ["harness"] }),
      };
    }
    if (path.includes("/api/analysis")) {
      return { ok: true, status: 200, json: async () => report };
    }
    return { ok: false, status: 404, json: async () => ({ message: `harness 未打桩：${path}` }) };
  },
};
sandbox.window = sandbox;
sandbox.window.matchMedia = () => ({ matches: false });
sandbox.window.CircleDocMap = {
  adoptIsochrone: () => {
    mapCalls.adopted += 1;
    return true;
  },
  setOverlays: (overlays) => {
    mapCalls.overlays.push(overlays);
    const pois = (overlays && overlays.pois) || [];
    const features =
      (overlays && overlays.blindSpots && overlays.blindSpots.features) || [];
    return { pois: pois.filter((p) => p.in_polygon).length, blindBlocks: features.length };
  },
  setCenter: (lnglat) => mapCalls.centers.push(lnglat),
  focusOn: () => true,
  setRunHint: () => {},
};
const context = vm.createContext(sandbox);
const source = readFileSync(resolve(root, "web", "report.js"), "utf-8");

const SKIP = Symbol("skip");
let failures = 0;
let skipped = 0;
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

console.log("=== report.js 渲染路径（真实 /api/analysis 响应）===");
try {
  vm.runInContext(source, context, { filename: "report.js" });
} catch (error) {
  console.log(`  FAIL 加载 report.js — ${error && error.stack}`);
  process.exit(1);
}

check("加载后暴露 window.CircleDocReport", () => {
  if (!sandbox.window.CircleDocReport) throw new Error("未暴露接口");
  return Object.keys(sandbox.window.CircleDocReport).join(",");
});

check("renderReport() 不抛异常", () => {
  sandbox.window.CircleDocReport.renderReport(report);
  return `新建 ${nodeCount} 个节点`;
});

const scoreCard = registry.get("score-card");
const radar = registry.get("radar-chart");
const bar = registry.get("bar-chart");

const blindList = registry.get("blind-list");
const summary = registry.get("summary-list");

check("评分卡渲染出总分与等级", () => {
  const text = scoreCard.textContent;
  // 卡片按 1 位小数展示，与后端的 44.42 不是逐字相等，比的是同一个数。
  const shown = Number(report.score.total).toFixed(1);
  if (!text.includes(shown)) throw new Error(`缺总分 ${shown}：${text.slice(0, 120)}`);
  if (!text.includes(report.score.grade)) throw new Error("缺等级");
  return text.slice(0, 60);
});

check("雷达图生成 <svg> 且含 6 条轴标签", () => {
  const svg = radar.children.find((c) => c.tagName === "SVG");
  if (!svg) throw new Error("没有生成 svg");
  const labels = svg.children.filter((c) => c.tagName === "TEXT");
  const area = svg.children.find((c) => c.className === "radar-area");
  if (!area) throw new Error("没有数据多边形");
  // 轴标签数要等于这份响应里**实际有的**维度数：真实报告是 6 个，
  // 但断言写死 6 会在喂进来一份 2 维的样本时误报（那是数据不同，不是代码坏了）。
  const expected = Object.keys(report.score.radar || {}).length;
  if (labels.length !== expected) {
    throw new Error(`标签 ${labels.length} 条 vs 报告 ${expected} 个维度`);
  }
  return `${svg.children.length} 个子节点 / ${labels.length} 条标签`;
});

check("柱状图生成 <svg>，未实测维度画成斜纹柱", () => {
  const svg = bar.children.find((c) => c.tagName === "SVG");
  if (!svg) throw new Error("没有生成 svg");
  const threshold = svg.children.find((c) => c.className === "bar-threshold");
  if (!threshold) throw new Error("没有阈值线");
  const bars = svg.children.filter((c) => c.tagName === "RECT");
  const unmeasured = svg.children.filter(
    (c) => c.tagName === "RECT" && c.className.includes("unmeasured")
  );
  const over = svg.children.filter(
    (c) => c.tagName === "RECT" && c.className.includes("over")
  );
  // 🔴 回归断言：`Number(null) === 0`，所以 `nearest_minutes: null` 极易被画成
  // 「0 分钟就到」的满分柱 —— 方向性错误。本份数据里必须真的有未实测维度，
  // 否则这条断言失去意义（换一份全测到的数据时它会明说，而不是静默通过）。
  const expectUnmeasured = report.coverage.groups.filter(
    (g) => g.scored !== false && g.nearest_minutes === null
  ).length;
  if (!expectUnmeasured) {
    // 全测到的数据（例如老城样例）覆盖不到这个 bug —— 明确跳过，不让它变成一个假绿。
    return SKIP;
  }
  if (expectUnmeasured !== unmeasured.length) {
    throw new Error(
      `未实测柱 ${unmeasured.length} 根 vs 报告里 ${expectUnmeasured} 个维度无实测值`
    );
  }
  return `${bars.length} 根柱（未实测 ${unmeasured.length} / 超阈值 ${over.length}）`;
});

check("未实测的设施在明细表里显示「未实测」而不是 0.0 min", () => {
  const body = registry.get("poi-table").tbody;
  const unmeasuredPois = report.coverage.groups.flatMap((g) =>
    (g.pois || []).filter((p) => p.measured_minutes === null || p.measured_minutes === undefined)
  );
  if (!unmeasuredPois.length) return SKIP;
  const cells = body.children.map((tr) => tr.children[4]);
  const wrong = cells.filter((cell) => /^\s*0(\.0+)?\s*min\s*$/.test(cell.textContent));
  if (wrong.length) throw new Error(`${wrong.length} 个未实测点位被渲染成 0 min`);
  const marked = cells.filter((cell) => cell.textContent === "未实测").length;
  if (marked !== unmeasuredPois.length) {
    throw new Error(`标「未实测」的单元格 ${marked} 个 vs 报告 ${unmeasuredPois.length} 个`);
  }
  return `${marked} 个点位正确标注为未实测`;
});

check("摘要渲染出 3~5 条结论", () => {
  const count = summary.children.length;
  if (count < 3 || count > 5) throw new Error(`结论条数 ${count} 不在 3~5`);
  return summary.children[0].textContent.slice(0, 60);
});

check("设施明细表行数与报告一致", () => {
  const expected = report.coverage.groups.reduce((sum, g) => sum + (g.pois || []).length, 0);
  const rows = registry.get("poi-table").tbody.children.length;
  if (rows !== expected) throw new Error(`表格 ${rows} 行 vs 报告 ${expected} 条`);
  return `${rows} 行`;
});

check("盲区清单渲染出块", () => {
  if (!blindList.children.length) throw new Error("清单为空");
  return `${blindList.children.length} 项：${blindList.children[0].textContent.slice(0, 70)}`;
});

check("数据来源徽标为 sample（离线报告的诚实标注）", () => {
  const badge = registry.get("source-badge");
  if (badge.dataset.source !== report.meta.data_source) {
    throw new Error(`徽标 ${badge.dataset.source} vs 报告 ${report.meta.data_source}`);
  }
  return badge.textContent;
});

check("运行一次后导出按钮解禁", () => {
  if (registry.get("export-json").disabled) throw new Error("JSON 导出仍禁用");
  if (registry.get("export-csv").disabled) throw new Error("CSV 导出仍禁用");
  return registry.get("export-hint").textContent.slice(0, 40);
});

check("列筛选切到单维度后行数减少", () => {
  const select = registry.get("poi-category");
  const first = select.options.find((o) => o.value);
  if (!first) throw new Error("筛选下拉没有维度选项");
  select.value = first.value;
  select.dispatch("change");
  const rows = registry.get("poi-table").tbody.children.length;
  const total = report.coverage.groups.reduce((sum, g) => sum + (g.pois || []).length, 0);
  if (rows >= total) throw new Error(`筛选后 ${rows} 行，未少于全部 ${total} 行`);
  return `${first.value} → ${rows} 行`;
});

// ---------------------------------------------------------------------------------------------
// FR-212 / FR-222：驱动**真实的一键体检流程**，看两个叠加图层有没有交给地图
// ---------------------------------------------------------------------------------------------
// 为什么不能只调 `renderReport()`：图层交接发生在 `runAnalysis()` 的响应处理里，
// 直接调渲染函数会绕开它 —— 那样测出来的是「渲染函数没问题」，而不是「点一下按钮真的会上图」。
async function driveRun() {
  registry.get("run-analysis").dispatch("click");
  // `runAnalysis` 是 async：让它把 fetch 微任务跑完。50 ms 足够，且不依赖真实计时精度。
  await new Promise((resolve) => setTimeout(resolve, 50));
}

await driveRun();

check("FR-212/FR-222：把 POI 与盲区块交给了地图图层", () => {
  if (!mapCalls.overlays.length) throw new Error("从未调用 CircleDocMap.setOverlays");
  const last = mapCalls.overlays[mapCalls.overlays.length - 1];
  const pois = last.pois || [];
  const features = (last.blindSpots && last.blindSpots.features) || [];
  const expectedPois = report.coverage.groups.reduce(
    (sum, g) => sum + (g.pois || []).length,
    0,
  );
  if (pois.length !== expectedPois) {
    throw new Error(`交给地图的 POI ${pois.length} 个 vs 报告 ${expectedPois} 个`);
  }
  for (const poi of pois) {
    for (const key of ["name", "category", "subcat", "lnglat", "in_polygon"]) {
      if (poi[key] === undefined) throw new Error(`POI 缺字段 ${key}`);
    }
  }
  if (features.length !== (report.blind_spots.blocks || []).length) {
    throw new Error(
      `盲区 feature ${features.length} 个 vs 报告 ${(report.blind_spots.blocks || []).length} 块`,
    );
  }
  return `${pois.length} 个 POI（字段齐全）+ ${features.length} 个盲区块`;
});

check("FR-222：每个盲区 feature 都带 level 与缺失类别", () => {
  const last = mapCalls.overlays[mapCalls.overlays.length - 1];
  const features = (last && last.blindSpots && last.blindSpots.features) || [];
  if (!features.length) return SKIP;
  for (const feature of features) {
    const props = feature.properties || {};
    if (props.level !== "blind" && props.level !== "insufficient") {
      throw new Error(`未知 level：${props.level}`);
    }
    if (!Array.isArray(props.missing) || !props.missing.length) {
      throw new Error("盲区块必须列出缺失类别（FR-223）");
    }
    const ring = (feature.geometry && feature.geometry.coordinates) || [];
    if (!ring.length || ring[0].length < 3) throw new Error("盲区多边形顶点少于 3 个");
  }
  return `${features.length} 个 feature 均有 level / missing / 多边形`;
});

check("体检完成后状态行同时报出分数与地图叠加数量", () => {
  const text = registry.get("analysis-hint").textContent;
  if (!text.includes("完成：")) throw new Error(`状态行未报完成：${text.slice(0, 60)}`);
  if (!text.includes("地图叠加：")) throw new Error(`状态行未报地图叠加：${text.slice(0, 90)}`);
  return text.slice(0, 78);
});

console.log(
  failures
    ? `\n${failures} 项失败${skipped ? `，${skipped} 项跳过` : ""}`
    : `\n全部通过${skipped ? `（${skipped} 项因数据不含该情形而跳过）` : ""}`
);
process.exit(failures ? 1 : 0);

// ---------------------------------------------------------------------------------------------
// FR-213：表头排序
// ---------------------------------------------------------------------------------------------
function headerFor(key) {
  return registry
    .get("poi-table")
    .thead.querySelectorAll("th")
    .find((th) => th.getAttribute("data-sort") === key);
}

function minutesColumn() {
  return registry
    .get("poi-table")
    .tbody.children.map((tr) => tr.children[4].textContent);
}

check("FR-213：表头可点击且带 aria-sort", () => {
  const th = headerFor("minutes");
  if (!th) throw new Error("找不到「实测步行时间」表头");
  if (!th.listeners.click || !th.listeners.click.length) throw new Error("表头没有 click 监听");
  th.dispatch("click");
  if (th.getAttribute("aria-sort") !== "ascending") {
    throw new Error(`点击后 aria-sort=${th.getAttribute("aria-sort")}`);
  }
  return "点击 → ascending";
});

check("FR-213：升序排列后未实测的设施全部垫底", () => {
  const cells = minutesColumn();
  if (!cells.length) return SKIP;
  const firstUnmeasured = cells.findIndex((text) => text === "未实测");
  if (firstUnmeasured < 0) return SKIP;
  // 垫底 = 从第一个「未实测」开始，后面不能再出现有实测值的行。
  const tail = cells.slice(firstUnmeasured);
  const leaked = tail.filter((text) => text !== "未实测");
  if (leaked.length) {
    throw new Error(`未实测行后面还跟着 ${leaked.length} 个已实测行：${leaked.slice(0, 3)}`);
  }
  // 已实测的那一段必须是真正的升序
  const measured = cells.slice(0, firstUnmeasured).map((t) => Number.parseFloat(t));
  for (let i = 1; i < measured.length; i += 1) {
    if (measured[i] < measured[i - 1]) {
      throw new Error(`升序被破坏：${measured[i - 1]} → ${measured[i]}`);
    }
  }
  return `${firstUnmeasured} 个已实测升序 + ${tail.length} 个未实测垫底`;
});

check("FR-213：再次点击同一列切到降序", () => {
  const th = headerFor("minutes");
  th.dispatch("click");
  if (th.getAttribute("aria-sort") !== "descending") {
    throw new Error(`二次点击后 aria-sort=${th.getAttribute("aria-sort")}`);
  }
  const cells = minutesColumn().filter((text) => text !== "未实测");
  for (let i = 1; i < cells.length; i += 1) {
    if (Number.parseFloat(cells[i]) > Number.parseFloat(cells[i - 1])) {
      throw new Error("降序被破坏");
    }
  }
  return `${cells.length} 个已实测降序`;
});

check("FR-213：换列排序会重置为升序", () => {
  headerFor("distance").dispatch("click");
  const th = headerFor("distance");
  if (th.getAttribute("aria-sort") !== "ascending") {
    throw new Error("换列未回到升序");
  }
  // 名列表头也必须可排序（文本列）
  const nameTh = headerFor("name");
  if (!nameTh) throw new Error("名称列不可排序");
  return "distance → ascending";
});

check("FR-223：盲区清单导出按钮已接线", () => {
  const button = registry.get("export-blind-csv");
  if (!button) throw new Error("缺少导出按钮");
  if (button.disabled) throw new Error("导出按钮仍禁用");
  if (!button.listeners.click || !button.listeners.click.length) {
    throw new Error("导出按钮没有 click 监听");
  }
  return "已接线";
});

console.log(
  failures
    ? `\n${failures} 项失败${skipped ? `，${skipped} 项跳过` : ""}`
    : `\n全部通过${skipped ? `（${skipped} 项因数据不含该情形而跳过）` : ""}`
);
process.exit(failures ? 1 : 0);
