/* CircleDoc M2：等时圈前端渲染（web/isochrone.js）
 *
 * 覆盖 PRD §3.2 的验收编号：FR-201（多边形）FR-202（半透明填充+描边、缩放平移不错位）
 * FR-203（顶点可点击 → 方位角/实测耗时/路径距离 + 懒加载真实路径）FR-204（耗时热力层可开关）
 * FR-205（进度：已测方向数 / 总数 + 分阶段渲染不白屏）FR-206（步行/骑行切换 + 双模式配额显式提示
 * + quota_units 与 http_requests 两栏分开）；另外落实 FR-101（三种取点方式）与 FR-105（非实时横幅
 * + 离线示意图标注）。字段名严格按后端定稿契约，一个都不改。
 *
 * 🔴 五条纪律
 * 1. R13：JSAPI 运行时 + 瓦片不可 vendored，断网必挂 → 只在「data_mode=live 且本地有浏览器端
 *    AK 且 navigator.onLine」时**运行时动态注入**；注入失败/超时/离线/非 live 一律无损降级到
 *    本页自带的 SVG 舞台（FR-105），并标注「离线示意图，非真实地图」。整页永不白屏。
 * 2. R12：HTML 里一个公网 CDN 都不写（JSAPI 的 URL 只作为**运行时字符串**存在，见 JSAPI_BASE）。
 * 3. D2/M2：沿用 M1 首页的**原生 JS**风格（不引 Vue、不引 vendored 库，那是 M4 的活）。
 * 4. R11：无构建 = 无编译器兜底。所以模板字符串拼 DOM 一律不用：所有节点走 createElement /
 *    createElementNS + textContent，数据永不 innerHTML 拼接（顶点话术会回显后端给的错误文本）。
 * 5. R15：触摸/拖拽只走 pointer 事件，不写 touch* 分支。
 *
 * 失败一律落到渲染分支（`renderRequestFailure` / `degradeToSvg`），不向外抛：现场最常见的意外
 * 是后端还没挂上 /api/isochrone，此时页面仍要解释得通「现在画的是什么、为什么不是实时」。
 */

'use strict';

// ---------------------------------------------------------------------------------------------
// 常量：路径 / 话术 / 调色
// ---------------------------------------------------------------------------------------------

const HEALTH_PATH = "/api/health";
const GEOCODE_PATH = "/api/geocode";
const REGEO_PATH = "/api/regeo";
const SUGGEST_PATH = "/api/suggest";
const ISOCHRONE_PATH = "/api/isochrone";
const POLYLINE_PATH = "/api/isochrone/polyline";

/** 🔴 只在运行时注入用（R12：绝不写进 HTML；断网时这条 URL 必然取不到，故必须可降级）。 */
const JSAPI_BASE = "https://api.map.baidu.com/api";
const JSAPI_VERSION = "4.0";
const JSAPI_TIMEOUT_MS = 8000;
const JSAPI_CALLBACK = "__circleDocJsapiReady";

/** 浏览器端 AK 只存本机 localStorage：后端 `public_dict()` 只回布尔、永不下发 AK 值。 */
const AK_STORAGE_KEY = "circledoc.browser_ak";

const DEFAULT_CENTER = [116.4074, 39.9042];
const FALLBACK_TOTAL_DIRECTIONS = 12; // Step1 粗扫 12 方位角（PRD §4.2），progress 缺失时的兜底
const MAX_HEAT_CELLS = 500; // 超过则抽样绘制并在提示里说明，避免一帧塞几千个矩形
const POLYLINE_CALL_LIMIT = 12; // FR-203：与后端 polyline_call_limit 同口径
const VERTEX_RADIUS_PX = 6;
const M_PER_DEG_LAT = 111320;

const MODE_LABEL = { walking: "步行", riding: "骑行" };
const DATA_MODE_LABEL = { live: "实时数据", cache: "缓存优先", mock: "离线合成数据" };
const DATA_SOURCE_LABEL = { live: "实时调用", cache: "本地缓存", sample: "程序合成示例" };

/**
 * ⚠️ 五种 `sample` 标签话术两两不同，且**绝不合并**（PRD §4.2 第 271 行）：
 * `blocked_official` 是「这条路走不通」（官方硬判据，不可重试绕过），
 * `blocked_timeout` 是「这次没测准/返回空，可重试」（我方问题，可修复）。
 * 把两者渲染成同一句 = 把人往错误的处置动作上引导，也违反 R3。
 */
const SAMPLE_LABEL = {
  measured: "实测：批量算路直接测得该方向",
  interpolated: "插值：由相邻实测方向内插，未单独出网",
  extrapolated: "外推：上界处仍顺畅，边界按外推估计",
  blocked_official: "官方禁行证据：路线规划给出禁行/逆行/无路线，这条路走不通",
  blocked_timeout: "本次未测准：返回空或超时，可对该方向重试",
};

const STAGE_LABEL = {
  coarse: "Step1 粗扫（12 方位角）",
  bisect: "Step2 二分收敛", // 后端 progress[].stage 实际用这个词（契约里写的是 refine，两种都认）
  refine: "Step2 二分收敛",
  densify: "Step3 中点加密",
  simplify: "Step4 排序与简化",
  field: "Step6 耗时场插值",
};

/** 耗时 → 颜色：越接近目标时长越暖（FR-204 的「越久越难达」直觉映射）。输出 hex，JSAPI 也吃得下。 */
const HEAT_TARGET_FALLBACK = 15;

/**
 * FR-212 的 POI 图层样式：**按维度分色 + 分形状**。
 *
 * 为什么按「维度」（6 大民生 + 便民参考项）而不是按 19 个 subcat 分色：19 种颜色在同一张
 * 图上人眼分不开，而 §4.3 的评分、雷达图、柱状图全都是按这 7 个维度说话的 ——
 * 地图、图表、明细表用同一套色板，评审才能把「图上的红点」和「雷达图上的基础医疗」
 * 对上号。精确到官方分类路径的信息放在点击弹窗里（`official_path`）。
 *
 * `shape` 只有 SVG 舞台那条路径用（离线演示的默认路径）；JSAPI 那条路径用同色圆点，
 * 因为 4.0 的自定义图标要额外 `setIcon` 与图片资源，而 R12 要求零外链。
 * 颜色是主通道、形状是冗余通道 —— 任一通道单独可用，不依赖颜色也能区分（色盲可读）。
 */
const DIMENSION_STYLE = {
  medical: { label: "基础医疗", color: "#cf222e", shape: "circle" },
  education: { label: "教育", color: "#0969da", shape: "square" },
  shopping: { label: "购物市场", color: "#1a7f37", shape: "triangle" },
  leisure: { label: "养老休闲", color: "#8250df", shape: "diamond" },
  transit: { label: "公共交通", color: "#9a6700", shape: "pentagon" },
  food: { label: "餐饮服务", color: "#bc4c00", shape: "cross" },
  convenience: { label: "便民服务", color: "#57606a", shape: "plus" },
};

/** 维度键 → 样式；认不出来一律回退到便民参考项的灰，不凭空发明颜色。 */
function poiStyle(key) {
  return DIMENSION_STYLE[key] || DIMENSION_STYLE.convenience;
}

/** FR-221 / FR-224 两级阈值的配色：完全盲区是深灰，配套不足是橙（与图例文字一致）。 */
const BLIND_STYLE = {
  blind: { fill: "#6e6e6e", fillOpacity: 0.45, stroke: "#4a4a4a", label: "完全盲区（三类皆无）" },
  insufficient: {
    fill: "#d18616",
    fillOpacity: 0.3,
    stroke: "#a86400",
    label: "配套不足（缺 1~2 类）",
  },
};

// ---------------------------------------------------------------------------------------------
// DOM：一次性取齐，后续只读写这些引用
// ---------------------------------------------------------------------------------------------

const el = {
  modeBadge: document.getElementById("mode-badge"),
  modeText: document.getElementById("mode-text"),
  engineBadge: document.getElementById("engine-badge"),
  engineText: document.getElementById("engine-text"),
  banner: document.getElementById("banner"),
  bannerTitle: document.getElementById("banner-title"),
  bannerMessage: document.getElementById("banner-message"),
  bannerDetails: document.getElementById("banner-details"),
  bannerCta: document.getElementById("banner-cta"),
  quotaNotice: document.getElementById("quota-notice"),
  quotaTitle: document.getElementById("quota-title"),
  quotaBody: document.getElementById("quota-body"),
  quotaMeta: document.getElementById("quota-meta"),
  quotaConfirm: document.getElementById("quota-confirm"),
  quotaCancel: document.getElementById("quota-cancel"),
  lng: document.getElementById("lng"),
  lat: document.getElementById("lat"),
  addr: document.getElementById("addr"),
  city: document.getElementById("city"),
  suggestOn: document.getElementById("suggest-on"),
  suggestList: document.getElementById("suggest-list"),
  centerReadout: document.getElementById("center-readout"),
  centerAddress: document.getElementById("center-address"),
  browserAk: document.getElementById("browser-ak"),
  akState: document.getElementById("ak-state"),
  mode: document.getElementById("mode"),
  minutes: document.getElementById("minutes"),
  dataMode: document.getElementById("data-mode"),
  run: document.getElementById("run"),
  runHint: document.getElementById("run-hint"),
  catAll: document.getElementById("cat-all"),
  categoryHint: document.getElementById("category-hint"),
  pickPoint: document.getElementById("pick-point"),
  recenter: document.getElementById("recenter"),
  applyLnglat: document.getElementById("apply-lnglat"),
  resetCenter: document.getElementById("reset-center"),
  geocode: document.getElementById("geocode"),
  saveAk: document.getElementById("save-ak"),
  clearAk: document.getElementById("clear-ak"),
  heat: document.getElementById("heat"),
  compare: document.getElementById("compare"),
  reference: document.getElementById("show-reference"),
  showPois: document.getElementById("show-pois"),
  showBlind: document.getElementById("show-blind"),
  layerHint: document.getElementById("layer-hint"),
  stageWrap: document.getElementById("stage-wrap"),
  stageArea: document.getElementById("stage-area"),
  mapLive: document.getElementById("map-live"),
  stage: document.getElementById("stage"),
  stageTag: document.getElementById("stage-tag"),
  stageHint: document.getElementById("stage-hint"),
  tooltip: document.getElementById("tooltip"),
  legend: document.getElementById("legend"),
  progressText: document.getElementById("progress-text"),
  progressFill: document.getElementById("progress-fill"),
  progressStages: document.getElementById("progress-stages"),
  resultMeta: document.getElementById("result-meta"),
  usageTable: document.getElementById("usage-table"),
  polylineStatus: document.getElementById("polyline-status"),
  verticesTable: document.getElementById("vertices-table"),
  requestIdFoot: document.getElementById("request-id-foot"),
};

const tableBody = el.usageTable.querySelector("tbody");
const vertexBody = el.verticesTable.querySelector("tbody");

// ---------------------------------------------------------------------------------------------
// FR-104：设施类别多选
// ---------------------------------------------------------------------------------------------

/**
 * 七个勾选框。**一次性取出**：类别组是静态 HTML，运行期不增减；`data-cat` 的取值与顺序
 * = 后端 `DIMENSION_ORDER`，文案 = `DIMENSIONS[key].label`，`data-blind-cat`
 * = `BLIND_TRIO_DIMENSIONS`，三处都由 `tests/test_web_smoke.py` 与后端词表逐一对账，
 * 所以这里只读 DOM、不再抄一份名单（抄了就会有第二出处，改词表时静默失配）。
 */
const catBoxes = Array.from(document.querySelectorAll("#categories input.cat"));

/** 勾选框旁边的中文名（读 DOM 而不是查表：避免前端自己维护一份维度文案）。 */
function catLabel(box) {
  const span = box.nextElementSibling;
  return span && span.tagName === "SPAN" ? span.textContent.trim() : box.dataset.cat;
}

/**
 * 本次要检索的维度键。
 *
 * 返回 `null` = 全七类勾选，此时**不传** `categories`：后端缺省就是全七类，
 * 而传七个键会让同一中心点的缓存换一把键、白烧一次地点检索配额。
 * 返回数组 = 只检索这几类。`[]`（一个都没勾）也照实返回，由 `report.js` 在发请求前挡掉 ——
 * `api/analysis.py` 把 `[]` 当 falsy 折成 `None`，即「检索全部」，
 * 「取消全部却拿到全七类结果」是最难被看出来的一种假数据。
 */
function selectedCategories() {
  const on = catBoxes.filter((box) => box.checked).map((box) => box.dataset.cat);
  return on.length === catBoxes.length ? null : on;
}

/** 被取消、且**装着盲区主判定三个 subcat** 的那几类（取消它们会污染 FR-221 的口径）。 */
function blindCatsOff() {
  return catBoxes.filter((box) => box.dataset.blindCat && !box.checked);
}

/**
 * 把当前勾选态翻译成人话。四条分支各对应一种可比性：
 * 全选 = 与后端缺省一致；子集但不碰三类 = 盲区口径仍可比、总分分母跟着变；
 * 碰了三类 = 盲区面积只可能偏大；零勾选 = 不许跑。
 * ⚠️ 分支顺序按「最严重优先」写：`on === total` 必须单独成一条，
 * 否则全选会掉进 subset 文案，而那句是在描述「收窄之后的口径」（实测踩过：7/7 显示成 subset）。
 */
function renderCategoryHint() {
  if (!el.categoryHint) return;
  const total = catBoxes.length;
  const on = catBoxes.filter((box) => box.checked).length;
  if (el.catAll) el.catAll.disabled = on === total;
  const hurt = blindCatsOff();
  let state;
  let text;
  if (!on) {
    state = "none";
    text = `已取消全部 ${total} 类：没有任何设施会被检索，点「一键体检」不会发请求。`;
  } else if (hurt.length) {
    state = "hurt";
    text =
      `已勾选 ${on}/${total}，但取消的「${hurt.map(catLabel).join("、")}」里装着盲区主判定` +
      "（FR-221）依赖的设施。盲区层只看得见「附近没命中」，看不见「这一类这次没查」，" +
      "所以本次盲区面积与「完全盲区」计数只可能偏大、不可能偏小，不能和全选七类的结果比大小" +
      "（后端会在同一次响应的 notes 里再挂一条同样的警示）。";
  } else if (on === total) {
    state = "all";
    text = "七类全选 = 不限制类别（后端缺省），盲区面积与总分口径都和类别多选落地前一致。";
  } else {
    state = "subset";
    text =
      `已勾选 ${on}/${total}：本次只检索这几类（每类一次地点检索请求，PRD §4.3）。` +
      "盲区主判定依赖的三类都还在，盲区面积与全选时可比；但总分会按这几类的权重之和重新归一" +
      "（报告里的 score.weight_sum 会跟着变小），与全选七类不是同一个分母。";
  }
  el.categoryHint.dataset.state = state;
  el.categoryHint.textContent = text;
}

// ---------------------------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------------------------

const state = {
  health: null,
  effectiveDataMode: "mock",
  center: DEFAULT_CENTER.slice(),
  // FR-103：中心点的结构化地址。cache 键取 5 位小数（与回显精度一致），同一位置反复取点只问一次；
  // pending 挡住同一坐标的重复在飞请求；seq 是在飞响应的世代号，连点换点时只认最新那份
  // —— 和 runSeq / polylineSeq 同一个思路。
  address: { pending: "", seq: 0, cache: new Map() },
  // FR-108：地址联想。timer 是 500 ms 防抖的句柄（边打字边问 = 每改一个字就烧一次检索配额）；
  // cache 键是 `城市|关键字`，同一个词反复删改只问一次；seq 同 FR-103 的世代号；
  // items/active 是列表内容与键盘高亮位（-1 = 还没选中任何一行）。
  suggest: { timer: 0, pending: "", seq: 0, cache: new Map(), items: [], active: -1 },
  minutes: 15,
  results: { walking: null, riding: null }, // FR-206：两模式结果都留着，用于叠加对比
  activeMode: "walking",
  activeResult: null,
  // 🔴 本页最近一次请求的 `request_id`（页脚与「结果元信息」的兜底来源）。
  // `/api/isochrone` 把号放在结果对象里，`/api/analysis` 放在响应**顶层**，嵌给地图的
  // `isochrone` 子对象没有它；没有这一格，主路径（一键体检）刷完屏页脚就退回 `—`，
  // 而且 `renderAll()` 会跑很多次，靠调用顺序去抢写是不可靠的（2026-10-11 就踩在
  // `setOverlays()` 的第二次 `renderAll()` 上）。
  requestId: "",
  lastPolylineResult: null,
  polylineCalls: 0,
  polylineSeq: 0, // FR-203 懒加载世代号：顶点连点时只认最新一次的响应（同 renderToken 的思路）
  serverPolylineCalls: null, // 服务端 calls 回传值（{used,limit,remaining}），有它就以它为准
  runSeq: 0, // /api/isochrone 世代号：在飞期间改了模式/中心点 → 过期响应不再抢走当前视图
  pickMode: false,
  running: false,
  pendingSecondRound: null,
  runTimer: null,
  // FR-212 / FR-222：从 `/api/analysis` 结果里带过来的两个叠加图层。
  // 与 `activeResult` 分开存：它们是 M4 报告的产物，生命周期跟着报告走，不跟着等时圈走
  // （单独跑一次 `/api/isochrone` 不会产生它们，此时清空即可，不该留上一份报告的残影）。
  overlays: { pois: [], blindSpots: null },
  layerNotes: {},
  // SVG 舞台视图：场景坐标单位是「米」（相对中心点），视图 = 场景中心 + 每米像素数。
  view: { x: 0, y: 0, scale: 0.06 },
  size: { w: 640, h: 420 },
  jsapi: {
    phase: "off",
    reason: "",
    ak: "",
    map: null,
    bmap: null,
    overlays: [],
    epoch: 0, // R13 注入世代号：注入在飞时用户改了参数/清了 AK → 这次注入结果整条作废
    pending: null, // 同一 AK 的注入 Promise 复用，避免连点「开始分析」插进多个 <script>
    pendingAk: "",
  },
};

// ---------------------------------------------------------------------------------------------
// 小工具：全部返回 DOM 节点或数值，不做任何字符串拼 HTML
// ---------------------------------------------------------------------------------------------

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}

function svgNode(tag, attrs) {
  const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const key of Object.keys(attrs || {})) {
    const value = attrs[key];
    if (value !== null && value !== undefined) item.setAttribute(key, String(value));
  }
  return item;
}

function clear(node_) {
  if (node_) node_.replaceChildren();
}

function num(value, digits) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits === undefined ? 1 : digits) : "—";
}

function intOf(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n) : null;
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

function fmtKm(meters) {
  const m = Number(meters);
  if (!Number.isFinite(m)) return "—";
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

function pair(value) {
  /** 契约里坐标一律 `[lng, lat]`；这里只接受两个有限数，别的形状一律判为无效。 */
  const arr = list(value);
  const lng = Number(arr[0]);
  const lat = Number(arr[1]);
  if (Number.isFinite(lng) && Number.isFinite(lat)) return [lng, lat];
  return null;
}

// ---------------------------------------------------------------------------------------------
// 投影：等距近似（BD09LL 直接当平面用，示意图精度足够；真实贴合路网交给 JSAPI 覆盖物）
// ---------------------------------------------------------------------------------------------

function metersPerDegLng(lat) {
  return M_PER_DEG_LAT * Math.max(0.01, Math.cos((lat * Math.PI) / 180));
}

function toScene(lnglat) {
  const c = state.center || DEFAULT_CENTER;
  const x = (lnglat[0] - c[0]) * metersPerDegLng(c[1]);
  const y = -(lnglat[1] - c[1]) * M_PER_DEG_LAT;
  return [x, y];
}

function fromScene(x, y) {
  const c = state.center || DEFAULT_CENTER;
  return [c[0] + x / metersPerDegLng(c[1]), c[1] - y / M_PER_DEG_LAT];
}

function sceneToScreen(point) {
  return [
    state.size.w / 2 + (point[0] - state.view.x) * state.view.scale,
    state.size.h / 2 + (point[1] - state.view.y) * state.view.scale,
  ];
}

function screenToScene(px, py) {
  return [
    state.view.x + (px - state.size.w / 2) / state.view.scale,
    state.view.y + (py - state.size.h / 2) / state.view.scale,
  ];
}

function fitView() {
  /** 把当前结果（含对比模式）+ 中心点框进视野；FR-202 的缩放平移以此为初始视野。 */
  const points = collectScenePoints();
  if (!points.length) {
    state.view = { x: 0, y: 0, scale: 0.06 };
    return;
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    minX = Math.min(minX, p[0]);
    maxX = Math.max(maxX, p[0]);
    minY = Math.min(minY, p[1]);
    maxY = Math.max(maxY, p[1]);
  }
  const padX = Math.max(120, (maxX - minX) * 0.15);
  const padY = Math.max(120, (maxY - minY) * 0.15);
  const spanX = maxX - minX + padX * 2;
  const spanY = maxY - minY + padY * 2;
  const scale = Math.max(
    0.005,
    Math.min(state.size.w / spanX, state.size.h / spanY, 0.5),
  );
  state.view = { x: (minX + maxX) / 2, y: (minY + maxY) / 2, scale };
}

function collectScenePoints() {
  const out = [];
  out.push([0, 0]); // 中心点
  const modes = [state.activeMode];
  if (el.compare.checked && state.results[otherMode(state.activeMode)]) {
    modes.push(otherMode(state.activeMode));
  }
  for (const mode of modes) {
    const result = state.results[mode];
    for (const lnglat of polygonRing(result)) out.push(toScene(lnglat));
  }
  // FR-212：只把**圈内** POI 计入视野。圈外的点（`count_near`）可能远到 1.5 km 外，
  // 把它们算进来会把等时圈缩成一个小疙瘩 —— 画了却看不清，等于没画。
  if (el.showPois && el.showPois.checked) {
    for (const poi of state.overlays.pois || []) {
      if (!poi.in_polygon) continue;
      const lnglat = pair(poi.lnglat);
      if (lnglat) out.push(toScene(lnglat));
    }
  }
  // FR-222：盲区块一律在圈内（候选点由射线法预筛），计入视野不会拉散。
  if (el.showBlind && el.showBlind.checked) {
    for (const block of blindBlocks()) {
      for (const ring of block.rings) {
        for (const lnglat of ring) out.push(toScene(lnglat));
      }
    }
  }
  return out;
}

function otherMode(mode) {
  return mode === "riding" ? "walking" : "riding";
}

// ---------------------------------------------------------------------------------------------
// 数据归一化：契约字段名不改，只做「缺字段就不画」的容错
// ---------------------------------------------------------------------------------------------

function polygonRing(result) {
  if (!result) return [];
  const coords = list(result.polygon && result.polygon.coordinates);
  let ring = list(coords[0])
    .map(pair)
    .filter(Boolean);
  if (!ring.length) {
    // 容错：多边形缺失但顶点在，按方位角升序连成环。⚠️ 只用于不把图开天窗，
    // 且在 `stageHint` 里明说「多边形由顶点近似闭合」，不冒充后端产物。
    ring = normalizeVertices(result)
      .slice()
      .sort((a, b) => a.bearing - b.bearing)
      .map((v) => v.lnglat)
      .filter(Boolean);
    if (ring.length >= 3) markRingApproximated();
  }
  return ring;
}

let ringApproximated = false;

function markRingApproximated() {
  ringApproximated = true;
}

function normalizeVertices(result) {
  return list(result && result.vertices)
    .map((raw, index) => {
      const item = raw || {};
      const lnglat = pair(item.lnglat);
      if (!lnglat) return null;
      const sample = String(item.sample || "");
      return {
        index,
        bearing: Number(item.bearing),
        minutes: Number(item.minutes),
        distanceM: Number(item.distance_m),
        confidence: Number(item.confidence),
        sample,
        sampleLabel: SAMPLE_LABEL[sample] || `采样方式：${sample || "未标注"}`,
        lnglat,
      };
    })
    .filter(Boolean);
}

function heatCells(result) {
  const field = (result && result.duration_field) || {};
  const cells = list(field.cells)
    .map((raw) => {
      const item = raw || {};
      const lnglat = pair(item.lnglat);
      if (!lnglat) return null;
      return { lnglat, minutes: Number(item.minutes), sample: String(item.sample || "") };
    })
    .filter((cell) => cell && Number.isFinite(cell.minutes));
  const step = Number(field.step_m);
  return { cells, stepM: Number.isFinite(step) && step > 0 ? step : 250 };
}

function progressList(result) {
  return list(result && result.progress).filter((item) => item && typeof item === "object");
}

// ---------------------------------------------------------------------------------------------
// 横幅 / 引擎徽标 / 配额提示
// ---------------------------------------------------------------------------------------------

function hideBanner() {
  el.banner.hidden = true;
  el.banner.dataset.level = "warning";
  el.bannerTitle.textContent = "";
  el.bannerMessage.textContent = "";
  el.bannerCta.textContent = "";
  clear(el.bannerDetails);
  // 横幅在地图上方：它出现/消失都会改变 stage-area 的上边界，移动端不重量就是把地图压矮。
  sizeSheetStage();
}

/** 当前生效的运行模式：以启动探测解析出的 `config.data_mode_in_effect` 为准（FR-105 的默认值来源）。 */
function healthMode() {
  const health = state.health || {};
  const config = health.config || {};
  return config.data_mode_in_effect || health.data_mode || "mock";
}

function setBanner(level, title, message, details, cta) {
  if (!title) {
    hideBanner();
    return;
  }
  el.banner.hidden = false;
  el.banner.dataset.level = level || "warning";
  el.bannerTitle.textContent = title;
  el.bannerMessage.textContent = message || "";
  clear(el.bannerDetails);
  for (const line of list(details)) {
    if (line) el.bannerDetails.append(node("li", null, line));
  }
  el.bannerCta.textContent = cta || "";
  sizeSheetStage();
}

function setEngineBadge(phase, reason) {
  // 🔴 世代号在这里自增：任何一次引擎状态变化（切回 SVG、降级、重新 loading）都要让
  // 「还在 await 中的那一次注入」作废。否则用户把数据模式切回 mock 之后，几秒前发出的
  // 注入仍会成功，并把已经不想要实时底图的页面强行切成 live。
  state.jsapi.epoch += 1;
  state.jsapi.phase = phase;
  state.jsapi.reason = reason || "";
  el.engineBadge.dataset.engine = phase;
  const text =
    phase === "live"
      ? "地图引擎：百度 JSAPI 4.0（实时底图）"
      : phase === "loading"
        ? "地图引擎：正在注入 JSAPI…"
        : phase === "failed"
          ? "地图引擎：JSAPI 不可用，已降级 SVG 示意图"
          : "地图引擎：SVG 示意图";
  el.engineText.textContent = text;
  const live = phase === "live";
  el.mapLive.hidden = !live;
  el.stage.style.display = live ? "none" : "";
  el.stageTag.hidden = live; // 🔴 FR-105：非真实地图时必须挂着这句标注
  el.stageTag.textContent = "离线示意图，非真实地图";
}

/** JSAPI 注入的三重判定条件（R13）：live + 本地 AK + 在线。任一不满足 → SVG 舞台。 */
function jsapiWanted() {
  if (state.effectiveDataMode !== "live") {
    return { use: false, reason: `数据模式为 ${state.effectiveDataMode}，不发公网请求` };
  }
  const ak = clientAk();
  if (!ak) {
    const configured = state.health && state.health.config && state.health.config.has_browser_ak;
    return {
      use: false,
      reason: configured
        ? "后端已配置浏览器端 AK，但按安全约定不下发前端；请在下方填入浏览器端类型 AK"
        : "本地未配置浏览器端 AK（JSAPI 只接受浏览器类型 AK，与服务端 AK 混用报 status=203）",
    };
  }
  if (typeof navigator === "object" && navigator.onLine === false) {
    return { use: false, reason: "浏览器处于离线状态，JSAPI 运行时与瓦片无法本地化" };
  }
  return { use: true, ak, reason: "" };
}

function clientAk() {
  return (state.jsapi.ak || "").trim();
}

function readStoredAk() {
  try {
    return window.localStorage.getItem(AK_STORAGE_KEY) || "";
  } catch (error) {
    return ""; // 隐私模式里 localStorage 会抛，降级为「内存内使用」而不是报错页
  }
}

function renderAkState() {
  const ak = clientAk();
  const configured = Boolean(
    state.health && state.health.config && state.health.config.has_browser_ak,
  );
  const parts = [
    ak ? `本机已提供 AK（尾号 ${ak.slice(-3)}），值不写入仓库` : "本地未提供浏览器端 AK",
    `后端 has_browser_ak = ${String(configured)}`,
    jsapiWanted().use ? "满足注入条件：将尝试实时底图" : `SVG 降级原因：${jsapiWanted().reason}`,
  ];
  el.akState.textContent = parts.join(" ｜ ");
}

function renderQuotaNotice() {
  /** FR-206 ①：第二轮**之前**的显式提示。第一轮已存在结果时，切另一模式必须先问。 */
  const pending = state.pendingSecondRound;
  if (!pending) {
    el.quotaNotice.hidden = true;
    sizeSheetStage();
    return;
  }
  const first = state.results[otherMode(pending.mode)];
  const firstUsage = (first && first.usage) || {};
  const firstLines = routeLinesTotal(firstUsage);
  el.quotaNotice.hidden = false;
  el.quotaNotice.dataset.warn = "true";
  el.quotaTitle.textContent = `即将跑第二轮：${MODE_LABEL[pending.mode] || pending.mode} 等时圈`;
  el.quotaBody.textContent =
    "骑行与步行共用同一份 5,000 条/日 路线配额（不是两套额度：同账号下各 AK 配额共用）。" +
    "跑双模式当日路线配额翻倍，超出后当天不再可用（配额熔断持续到次日零时）。";
  const meta = [
    `第一轮（${MODE_LABEL[otherMode(pending.mode)]}）quota_units = ${intOf(firstUsage.quota_units) ?? "—"}`,
    `http_requests = ${intOf(firstUsage.http_requests) ?? "—"}`,
    `路线条数合计 ≈ ${firstLines || "—"}`,
  ];
  const budget = budgetHint();
  if (budget) meta.push(budget);
  el.quotaMeta.textContent = `${meta.join(" ｜ ")} ｜ 本轮预计再消耗相近数量，请确认今日额度足够。`;
  sizeSheetStage();
}

function budgetHint() {
  const budget = state.health && state.health.config && state.health.config.budget;
  if (!budget) return "";
  const lines = budget.route_lines;
  const text = lines && typeof lines === "object" ? JSON.stringify(lines) : String(lines ?? "");
  return text ? `本地预算上限 route_lines=${text}` : "";
}

function routeLinesTotal(usage) {
  const byMode = (usage && usage.route_lines) || {};
  if (typeof byMode !== "object") return intOf(byMode) || 0;
  let sum = 0;
  for (const value of Object.values(byMode)) sum += intOf(value) || 0;
  return sum;
}

// ---------------------------------------------------------------------------------------------
// 渲染：结果元信息 / 计量 / 进度 / 顶点表 / 图例
// ---------------------------------------------------------------------------------------------

function renderModeBadge() {
  const mode = state.effectiveDataMode || "unknown";
  el.modeBadge.dataset.mode = mode;
  const label = DATA_MODE_LABEL[mode] || mode;
  const token = DATA_MODE_LABEL[mode] ? `（${mode}）` : "";
  el.modeText.textContent = `数据模式：${label}${token}`;
}

/** 这份结果对应的 `request_id`：结果自带的最准，否则回落到本页最近一次请求的号。 */
function currentRequestId(result) {
  return String((result && result.request_id) || state.requestId || "");
}

function renderResultMeta(result) {
  clear(el.resultMeta);
  if (!result) {
    el.resultMeta.append(node("dd", "empty", "尚无结果。"));
    return;
  }
  const center = pair(result.center) || state.center;
  // 🔴 号本身不能只读 `result.request_id`：`/api/analysis` 把号放在响应顶层，嵌给地图的
  // `isochrone` 子对象没有它，主路径（一键体检）刷完屏页脚就退回 `—`。见 `setRequestId()`。
  const requestId = currentRequestId(result);
  const rows = [
    ["中心点（BD09LL）", `${num(center[0], 5)}, ${num(center[1], 5)}`],
    [
      "模式 / 时长",
      `${MODE_LABEL[result.mode] || result.mode || "—"} · ${intOf(result.minutes) ?? "—"} min`,
    ],
    ["取数层", DATA_SOURCE_LABEL[result.data_source] || result.data_source || "—"],
    ["运行模式", DATA_MODE_LABEL[result.data_mode] || result.data_mode || "—"],
    ["圈内面积", result.area_km2 === null ? "—" : `${num(result.area_km2, 3)} km²`],
    ["顶点数", String(normalizeVertices(result).length)],
    ["request_id", requestId || "—"],
  ];
  if (result.degraded) {
    rows.push(["降级说明", `本次为降级结果：${result.message || "后端未给出原因"}`]);
  }
  for (const [key, value] of rows) {
    el.resultMeta.append(node("dt", null, key), node("dd", null, value));
  }
  el.requestIdFoot.textContent = `request_id：${requestId || "—"}`;
}

function renderUsage(result) {
  clear(tableBody);
  const usage = (result && result.usage) || {};
  const byMode = usage.route_lines || {};
  // 🔴 两栏各占一行、永不合并（PRD §4.2 第 267 行：只报 http_requests 会把配额低估一个数量级）。
  const rows = [
    ["quota_units", "路线配额折算条数（origins × destinations）", intOf(usage.quota_units), true],
    ["http_requests", "实际 HTTP 往返次数", intOf(usage.http_requests), true],
    ["place_calls", "地点检索调用（FR-206 ③：双模式不额外检索）", intOf(usage.place_calls), false],
    ["cache_hits", "本地缓存命中", intOf(usage.cache_hits), false],
    ["elapsed_ms", "服务端耗时", intOf(usage.elapsed_ms), false],
  ];
  for (const mode of ["walking", "riding"]) {
    if (byMode[mode] !== undefined) {
      rows.push([
        `route_lines.${mode}`,
        `${MODE_LABEL[mode]}路线条数`,
        intOf(byMode[mode]),
        false,
      ]);
    }
  }
  // 🔴 [BUG][M2] 连带修：后端 `usage` 已出口懒加载额度与批次数（键名与 `RouteProbe.usage()`
  // 逐字一致），这里就只读这**一个**数。原来第 592 行读 `usage.polyline_calls_remaining`
  // （后端从未出口，分支永远进不去），再靠 polyline 响应的 `calls` 镜像补一行 —— 计量表和
  // 状态行两套口径迟早分叉。兜底那行随之删掉，镜像只留给「还能点几下」的守卫用。
  const lazyRows = [
    [
      "polyline_calls / polyline_limit",
      "FR-203 懒加载已用 / 上限（服务端会话计数，与顶点状态行同源）",
      usage.polyline_calls !== undefined || usage.polyline_limit !== undefined
        ? `${intOf(usage.polyline_calls) ?? 0}/${intOf(usage.polyline_limit) ?? POLYLINE_CALL_LIMIT}`
        : null,
    ],
    [
      "polyline_rejected",
      "超出上限被拒的补调次数（按契约不再出网）",
      usage.polyline_rejected === undefined ? null : intOf(usage.polyline_rejected),
    ],
    [
      "batches_sent",
      "FR-205 批量算路实际发出的批次数（≤50 一切一批）",
      usage.batches_sent === undefined ? null : intOf(usage.batches_sent),
    ],
    [
      "budget_stops",
      "路线预算守卫截停次数（FR-206 ①，0 = 本轮没被截停）",
      usage.budget_stops === undefined ? null : intOf(usage.budget_stops),
    ],
  ];
  for (const [key, label, value] of lazyRows) {
    if (value !== null) rows.push([key, label, value, false]);
  }
  for (const [key, label, value, emphatic] of rows) {
    const tr = node("tr", emphatic ? "key" : null);
    const k = node("td", "metric", key);
    const l = node("td", null, label);
    const v = node("td", "value", value === null ? "—" : String(value));
    tr.append(k, l, v);
    tableBody.append(tr);
  }
  if (!result) {
    const tr = node("tr");
    const td = node("td", null, "尚无计量数据（未运行或接口未就绪）。");
    td.colSpan = 3;
    tr.append(td);
    tableBody.append(tr);
  }
}

function renderProgress(result, live) {
  clear(el.progressStages);
  const stages = progressList(result);
  let done = 0;
  let total = stages.length
    ? intOf(stages[stages.length - 1].total_directions) || FALLBACK_TOTAL_DIRECTIONS
    : FALLBACK_TOTAL_DIRECTIONS;
  if (stages.length) {
    const last = stages[stages.length - 1];
    const pending = intOf(last.pending_directions);
    done = Math.max(0, total - (pending === null ? 0 : pending));
  } else if (result) {
    done = total; // 有结果但没回 progress：按「已完成」显示，并在下方标注后端未回传
  }
  if (live) done = 0;
  const ratio = total > 0 ? Math.max(0, Math.min(1, done / total)) : 0;
  el.progressFill.style.width = `${Math.round(ratio * 100)}%`;
  el.progressText.textContent = live
    ? `请求进行中：已测方向 ${done} / ${total}（progress 由响应体回传，接口未提供流式通道时以分阶段渲染兜住不白屏）`
    : result
      ? `已测方向 ${done} / ${total}${stages.length ? "" : "（后端未回传 progress，按顶点数计为完成）"}`
      : "等待运行。";
  for (const item of stages) {
    const li = node("li");
    const label = STAGE_LABEL[item.stage] || String(item.stage || "阶段");
    const parts = [
      `${label}${item.round_index === undefined ? "" : ` 第 ${intOf(item.round_index) ?? ""} 轮`}`,
      `探测 ${intOf(item.probes) ?? "—"}`,
      `待测 ${intOf(item.pending_directions) ?? "—"}`,
      `共 ${intOf(item.total_directions) ?? "—"}`,
    ];
    li.textContent = parts.join(" ｜ ");
    el.progressStages.append(li);
  }
}

function renderVerticesTable(result) {
  clear(vertexBody);
  const vertices = normalizeVertices(result);
  if (!vertices.length) {
    const tr = node("tr");
    const td = node("td", null, result ? "本次结果没有顶点。" : "尚无结果。");
    td.colSpan = 6;
    tr.append(td);
    vertexBody.append(tr);
    return;
  }
  for (const v of vertices) {
    const tr = node("tr");
    tr.append(
      node("td", null, `#${v.index + 1}`),
      node("td", null, Number.isFinite(v.bearing) ? `${num(v.bearing, 1)}°` : "—"),
      node("td", null, Number.isFinite(v.minutes) ? `${num(v.minutes, 1)} min` : "—"),
      node("td", null, Number.isFinite(v.distanceM) ? fmtKm(v.distanceM) : "—"),
    );
    const sampleCell = node("td", `sample sample-${v.sample || "unknown"}`);
    sampleCell.append(node("div", null, v.sampleLabel));
    sampleCell.append(node("div", "raw", `${v.sample || "—"}｜置信 ${num(v.confidence, 2)}`));
    tr.append(sampleCell);

    const actionCell = node("td");
    const button = node("button", "tiny", "取真实路径");
    button.type = "button";
    button.addEventListener("click", () => loadPolyline(v));
    actionCell.append(button);
    tr.append(actionCell);
    vertexBody.append(tr);
  }
}

function renderLegend() {
  clear(el.legend);
  const items = [
    ["swatch poly", `等时圈 ${MODE_LABEL[state.activeMode] || ""}（半透明填充 + 描边，FR-202）`],
    ["swatch other", "另一模式多边形（FR-206 叠加对比）"],
    ["swatch blind", `${BLIND_STYLE.blind.label}（FR-221，点击看缺失类别与最近设施）`],
    ["swatch insufficient", `${BLIND_STYLE.insufficient.label}（FR-224 次级阈值）`],
    ["swatch vertex", "边界顶点（可点击查看，FR-203）"],
    ["swatch heat", "耗时热力格（FR-204，网格插值耗时）"],
    ["swatch ref", "同半径正圆对照线（FR-201：等时圈明显不是正圆）"],
    ["swatch center", "中心点"],
  ];
  const ul = node("ul", "legend-list");
  for (const [cls, text] of items) {
    const li = node("li");
    li.append(node("i", cls), node("span", null, text));
    ul.append(li);
  }
  el.legend.append(ul);

  // FR-212：POI 色板的图例。地图、雷达图、柱状图、明细表共用同一套维度色，
  // 评审才能把「图上的红点」与「雷达图的基础医疗」对上号。
  el.legend.append(node("p", "legend-h", "设施类别（颜色 + 形状双通道，色盲亦可分辨）："));
  const pois = node("ul", "legend-list legend-pois");
  for (const key of Object.keys(DIMENSION_STYLE)) {
    const style = DIMENSION_STYLE[key];
    const li = node("li");
    const swatch = svgNode("svg", { width: 14, height: 14, viewBox: "-7 -7 14 14", class: "poi-swatch" });
    const mark = poiSymbol(style.shape, 5.4);
    mark.setAttribute("fill", style.color);
    swatch.append(mark);
    li.append(swatch, node("span", null, style.label));
    pois.append(li);
  }
  const dashed = node("li");
  const dashedSvg = svgNode("svg", { width: 14, height: 14, viewBox: "-7 -7 14 14", class: "poi-swatch" });
  const dashedMark = svgNode("circle", { r: 5.4, fill: "none", stroke: "#8c8c8c", "stroke-width": 1.5, "stroke-dasharray": "3 2" });
  dashedSvg.append(dashedMark);
  dashed.append(dashedSvg, node("span", null, "虚线描边＝该设施未实测（不参与时效评分）"));
  pois.append(dashed);
  el.legend.append(pois);

  const dl = node("ul", "sample-list");
  for (const key of Object.keys(SAMPLE_LABEL)) {
    const li = node("li", `chip chip-${key}`);
    li.append(node("b", null, key), node("span", null, SAMPLE_LABEL[key]));
    dl.append(li);
  }
  el.legend.append(node("p", "legend-h", "顶点采样方式话术（五种两两不同，处置动作也不同）："));
  el.legend.append(dl);
}

// ---------------------------------------------------------------------------------------------
// SVG 舞台绘制：网格 / 正圆对照 / 热力格 / 多边形 / 顶点 / 中心点
// ---------------------------------------------------------------------------------------------

const scene = { root: null };

function ensureScene() {
  clear(el.stage);
  const defs = svgNode("defs");
  scene.root = svgNode("g", { id: "scene-root" });
  scene.grid = svgNode("g", { id: "g-grid", class: "grid" });
  scene.ref = svgNode("g", { id: "g-ref" });
  scene.heat = svgNode("g", { id: "g-heat" });
  scene.other = svgNode("g", { id: "g-other" });
  scene.poly = svgNode("g", { id: "g-poly" });
  // 叠放顺序即语义：盲区在等时圈**之上**（它是圈内的结论，被圈盖住就看不见），
  // POI 再压在盲区之上（点必须可见，否则「这里缺什么」看不出「这里其实有什么」）。
  scene.blind = svgNode("g", { id: "g-blind" });
  scene.pois = svgNode("g", { id: "g-pois" });
  scene.vertices = svgNode("g", { id: "g-vertices" });
  scene.center = svgNode("g", { id: "g-center" });
  scene.root.append(
    scene.grid,
    scene.ref,
    scene.heat,
    scene.other,
    scene.poly,
    scene.blind,
    scene.pois,
    scene.vertices,
    scene.center,
  );
  el.stage.append(defs, scene.root);
}

function resizeStage() {
  const rect = el.stageArea.getBoundingClientRect();
  const w = Math.max(240, Math.round(rect.width || el.stageArea.clientWidth || 640));
  const h = Math.max(220, Math.round(rect.height || 420));
  state.size = { w, h };
  el.stage.setAttribute("viewBox", `0 0 ${w} ${h}`);
  el.stage.setAttribute("width", String(w));
  el.stage.setAttribute("height", String(h));
}

function applyView() {
  const { w, h } = state.size;
  const s = state.view.scale;
  scene.root.setAttribute(
    "transform",
    `translate(${w / 2} ${h / 2}) scale(${s}) translate(${-state.view.x} ${-state.view.y})`,
  );
  // 顶点圆点半径按屏幕像素反算，缩放后仍是同一个视觉大小（不改变数据坐标 → 不会错位）。
  const r = VERTEX_RADIUS_PX / s;
  for (const dot of scene.vertices.querySelectorAll("circle")) dot.setAttribute("r", String(r));
  for (const tick of scene.center.querySelectorAll("circle")) tick.setAttribute("r", String(r * 0.85));
}

function drawGrid() {
  clear(scene.grid);
  const { w, h } = state.size;
  const step = chooseGridStep(state.view.scale);
  const half = [w / 2 / state.view.scale, h / 2 / state.view.scale];
  const x0 = Math.floor((state.view.x - half[0]) / step) * step;
  const x1 = Math.ceil((state.view.x + half[0]) / step) * step;
  const y0 = Math.floor((state.view.y - half[1]) / step) * step;
  const y1 = Math.ceil((state.view.y + half[1]) / step) * step;
  for (let x = x0; x <= x1; x += step) {
    scene.grid.append(svgNode("line", { x1: x, y1: y0, x2: x, y2: y1, class: "grid-line" }));
  }
  for (let y = y0; y <= y1; y += step) {
    scene.grid.append(svgNode("line", { x1: x0, y1: y, x2: x1, y2: y, class: "grid-line" }));
  }
  // 比例尺：一句「网格 = N m」比任何图例都直观，也让评审能判断示意图的量纲。
  const label = svgNode("text", { x: 8 / state.view.scale, y: 14 / state.view.scale, class: "grid-label" });
  label.textContent = `网格 ${fmtKm(step)}`;
  scene.grid.append(label);
}

function chooseGridStep(scale) {
  for (const step of [100, 250, 500, 1000, 2000]) {
    if (step * scale >= 42) return step;
  }
  return 2000;
}

// ---------------------------------------------------------------------------------------------
// FR-222 盲区图层 / FR-212 POI 图层（两张图共用同一份绘制入口，SVG 与 JSAPI 各一套实现）
// ---------------------------------------------------------------------------------------------

/**
 * 把 GeoJSON 的 `Polygon` **或** `MultiPolygon` 几何收成「外环数组的数组」。
 *
 * 🔴 必须两种都认：`services/blind_spots.py:blocks_to_geojson()` 在连通块**边界不连通**时
 * 会输出 `MultiPolygon`（实测交道口的 4 个块里就有 1 个是 3 个分离环）。
 * 只按 `Polygon` 取 `coordinates[0]` 的话，那一个块的坐标会变成 `[[[lng,lat],…]]`，
 * 逐点 `Number(point[0])` 得到 `NaN` → 整个块被静默丢掉。
 * 表现是「报告说有 4 块盲区、地图上只有 3 块」，而控制台一条错误都没有。
 */
function geojsonRings(geometry) {
  const type = geometry && geometry.type;
  const coordinates = geometry && geometry.coordinates;
  if (!Array.isArray(coordinates)) return [];
  const polygons =
    type === "MultiPolygon"
      ? coordinates
      : type === "Polygon"
        ? [coordinates]
        : [];
  const out = [];
  for (const polygon of polygons) {
    const outer = Array.isArray(polygon) ? polygon[0] : null;
    if (!Array.isArray(outer)) continue;
    const ring = [];
    for (const point of outer) {
      const lng = Number(point && point[0]);
      const lat = Number(point && point[1]);
      if (Number.isFinite(lng) && Number.isFinite(lat)) ring.push([lng, lat]);
    }
    if (ring.length >= 3) out.push(ring);
  }
  return out;
}

/** `blind_spots.geojson`（FeatureCollection）→ 可直接绘制的块列表（每块可含多个分离环）。 */
function blindBlocks() {
  const geojson = state.overlays.blindSpots;
  const out = [];
  if (!geojson || !Array.isArray(geojson.features)) return out;
  for (const feature of geojson.features) {
    const rings = geojsonRings(feature && feature.geometry);
    if (!rings.length) continue;
    const props = (feature && feature.properties) || {};
    out.push({
      level: props.level === "insufficient" ? "insufficient" : "blind",
      rings,
      areaKm2: Number(props.area_km2) || 0,
      cells: Number(props.cells) || 0,
      missing: Array.isArray(props.missing) ? props.missing : [],
      distanceM: Number(props.distance_from_center_m) || 0,
      nearest: props.nearest_facility || null,
      index: Number(props.block_index) || 0,
    });
  }
  return out;
}

/** 维度键 → 中文名（图例与弹窗共用；缺键回退到键本身，不编造）。 */
function dimensionLabel(key) {
  return (DIMENSION_STYLE[key] && DIMENSION_STYLE[key].label) || key || "—";
}

/** 未实测原因 → 短文案（与 `web/report.js` 的 `unmeasuredLabel` 同一套措辞）。 */
function poiUnmeasuredLabel(poi) {
  const reason = (poi && poi.unmeasured_reason) || "";
  const map = {
    outside_polygon: "圈外（不对圈外点做实测）",
    beyond_top_k: "未进前 K（配额取舍，不是数据缺失）",
    no_probe_injected: "未接入探测器",
    probe_failed: "探测器失败",
    measurement_missing: "本批未回耗时",
    no_batch_route_target: "坐标不可用",
  };
  return map[reason] || reason || "未实测";
}

/** 一个 POI 的弹窗文案。FR-212 要求的三样（名称 / 类别 / 实测步行时间）永远在最前面。 */
function poiLines(poi) {
  const measured = Number(poi.measured_minutes);
  const hasMeasured = poi.measured_minutes !== null && poi.measured_minutes !== undefined
    && Number.isFinite(measured);
  const straight = Number(poi.distance_m);
  return [
    `类别：${dimensionLabel(poi.category)}${poi.subcat ? `（${poi.subcat}）` : ""}`,
    hasMeasured
      ? `实测步行时间：${num(measured, 1)} min`
      : `实测步行时间：未实测 —— ${poiUnmeasuredLabel(poi)}`,
    `官方分类：${poi.official_path || poi.subcat || "—"}`,
    `直线距离：${Number.isFinite(straight) ? fmtKm(straight) : "—"}${poi.distance_estimated ? "（估算）" : ""}`,
    `落圈：${poi.in_polygon ? "圈内（射线法判定）" : "仅直线半径内，不在等时圈内"}`,
    `坐标：${num(poi.lnglat && poi.lnglat[0], 5)}, ${num(poi.lnglat && poi.lnglat[1], 5)}`,
    poi.address ? `地址：${poi.address}` : "",
  ];
}

/** 一个盲区块的弹窗文案。FR-223 要的「缺失类别 + 最近可用设施及时间」都在这里。 */
function blindLines(block) {
  const style = BLIND_STYLE[block.level];
  const facility = block.nearest;
  const lines = [
    `分级：${style.label}`,
    `缺失类别：${block.missing.map((key) => dimensionLabel(key)).join("、") || "—"}`,
    `面积：${num(block.areaKm2, 4)} km²（${block.cells} 个 250 m 栅格）`,
    `距中心点：${fmtKm(block.distanceM)}`,
  ];
  if (facility) {
    const minutes = Number(facility.minutes);
    lines.push(
      `最近可用设施：${facility.name || "—"}（${dimensionLabel(facility.category)}）｜` +
        `${Number.isFinite(minutes) ? `${num(minutes, 1)} min` : "—"}` +
        `${facility.measured ? "（实测）" : "（按步行速度估算，未实测）"}`,
    );
  } else {
    lines.push("最近可用设施：本次未取到（该块周边阈值内没有候选设施）");
  }
  lines.push("判定口径：栅格中心 1 km 半径内三类设施（市场 / 药店 / 小学）是否齐全");
  return lines;
}

/** 按 `shape` 造一个以 `(0,0)` 为中心、半径 `r` 的 SVG 图形（形状是颜色的冗余通道）。 */
function poiSymbol(shape, r) {
  const p = (n) => Number(n.toFixed(3));
  switch (shape) {
    case "square":
      return svgNode("rect", { x: p(-r * 0.85), y: p(-r * 0.85), width: p(r * 1.7), height: p(r * 1.7) });
    case "triangle":
      return svgNode("polygon", { points: `0,${p(-r)} ${p(r * 0.95)},${p(r * 0.75)} ${p(-r * 0.95)},${p(r * 0.75)}` });
    case "diamond":
      return svgNode("polygon", { points: `0,${p(-r)} ${p(r)},0 0,${p(r)} ${p(-r)},0` });
    case "pentagon": {
      const pts = [];
      for (let i = 0; i < 5; i += 1) {
        const a = -Math.PI / 2 + (i * 2 * Math.PI) / 5;
        pts.push(`${p(Math.cos(a) * r)},${p(Math.sin(a) * r)}`);
      }
      return svgNode("polygon", { points: pts.join(" ") });
    }
    case "cross": {
      const t = r * 0.34;
      return svgNode("polygon", {
        points:
          `${p(-t)},${p(-r)} ${p(t)},${p(-r)} ${p(t)},${p(-t)} ${p(r)},${p(-t)} ${p(r)},${p(t)} ` +
          `${p(t)},${p(t)} ${p(t)},${p(r)} ${p(-t)},${p(r)} ${p(-t)},${p(t)} ${p(-r)},${p(t)} ` +
          `${p(-r)},${p(-t)} ${p(-t)},${p(-t)}`,
      });
    }
    case "plus": {
      const t = r * 0.36;
      return svgNode("polygon", {
        points: `${p(-t)},${p(-r)} ${p(t)},${p(-r)} ${p(t)},${p(-t)} ${p(r)},${p(-t)} ${p(r)},${p(t)} ${p(t)},${p(t)} ${p(t)},${p(r)} ${p(-t)},${p(r)} ${p(-t)},${p(t)} ${p(-r)},${p(t)} ${p(-r)},${p(-t)} ${p(-t)},${p(-t)}`,
      });
    }
    default:
      return svgNode("circle", { r: p(r) });
  }
}

/**
 * FR-222：把盲区块画成灰色（完全盲区）/ 橙色（配套不足）多边形。
 *
 * 顶点直接用经纬度 → `toScene()` 投影后写入 SVG；**不做 Douglas-Peucker 简化**，
 * 因为块边界本来就是 250 m 栅格的直角折线，简化会把它磨成弧线、看起来像精确边界。
 */
function drawBlindSpots() {
  clear(scene.blind);
  if (!el.showBlind || !el.showBlind.checked) return;
  const blocks = blindBlocks();
  let ringCount = 0;
  for (const block of blocks) {
    const style = BLIND_STYLE[block.level];
    const open = (event) => {
      if (state.pickMode) return;
      showTooltip(`服务盲区 #${block.index + 1}：${style.label}`, blindLines(block), event);
    };
    // 一个块可能有多个分离环（MultiPolygon）：它们同属一个块，共用同一个 tooltip。
    for (const ring of block.rings) {
      const d = ring
        .map(toScene)
        .map((p, index) => `${index === 0 ? "M" : "L"}${p[0].toFixed(2)} ${p[1].toFixed(2)}`)
        .join(" ")
        .concat(" Z");
      const path = svgNode("path", {
        d,
        class: `blind-block blind-${block.level}`,
        fill: style.fill,
        "fill-opacity": style.fillOpacity,
        stroke: style.stroke,
        "stroke-width": 1.5,
        "vector-effect": "non-scaling-stroke",
        tabindex: "0",
        role: "button",
        "aria-label": `${style.label}，缺失 ${block.missing.join("、")}`,
      });
      path.addEventListener("click", open);
      path.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") open(event);
      });
      scene.blind.append(path);
      ringCount += 1;
    }
  }
  if (blocks.length) {
    const blindCount = blocks.filter((b) => b.level === "blind").length;
    const insuffCount = blocks.length - blindCount;
    setLayerHint(
      `盲区层：${blindCount} 块完全盲区 + ${insuffCount} 块配套不足` +
        `（共 ${ringCount} 个多边形；点击灰色/橙色区域看缺失类别与最近设施）。`,
      "blind",
    );
  } else {
    setLayerHint("", "blind");
  }
}

/**
 * FR-212：把 POI 画到地图上（按维度分色 + 分形状），点击弹窗给出名称 / 类别 / 实测步行时间。
 *
 * 只画**圈内**的点：圈外的点点上去只会得到「这个设施不在你的等时圈里」，
 * 在地图上铺一层用不到的标记会淹掉真正可达的那些（FR-211 的两种计数在明细表里看）。
 */
function drawPois() {
  clear(scene.pois);
  if (!el.showPois || !el.showPois.checked) return;
  const pois = state.overlays.pois || [];
  const r = 5 / state.view.scale; // 屏幕像素恒定：缩放时点不变大，但位置严格贴数据坐标
  let drawn = 0;
  for (const poi of pois) {
    if (!poi.in_polygon) continue;
    const lnglat = pair(poi.lnglat);
    if (!lnglat) continue;
    const center = toScene(lnglat);
    const style = poiStyle(poi.category);
    const group = svgNode("g", {
      class: `poi poi-${poi.category || "other"}`,
      transform: `translate(${center[0].toFixed(2)} ${center[1].toFixed(2)})`,
      tabindex: "0",
      role: "button",
      "aria-label": `${poi.name || "设施"}，${dimensionLabel(poi.category)}`,
    });
    const mark = poiSymbol(style.shape, r);
    mark.setAttribute("fill", style.color);
    mark.setAttribute("fill-opacity", poi.unmeasured ? "0.45" : "0.95");
    mark.setAttribute("stroke", "#ffffff");
    mark.setAttribute("stroke-width", (1 / state.view.scale).toFixed(3));
    // 未实测的点加一圈虚线，与「测到了」在视觉上分开（PRD 最忌讳把两者混为一谈）。
    if (poi.unmeasured) mark.setAttribute("stroke-dasharray", `${(2 / state.view.scale).toFixed(2)} ${(1.5 / state.view.scale).toFixed(2)}`);
    const open = (event) => {
      if (state.pickMode) return;
      showTooltip(poi.name || "设施", poiLines(poi), event);
    };
    group.addEventListener("click", open);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") open(event);
    });
    group.append(mark);
    scene.pois.append(group);
    drawn += 1;
  }
  if (drawn) {
    setLayerHint(
      `POI 层：绘制 ${drawn} 个圈内设施（按 6 大民生维度分色 + 分形状；虚线描边＝未实测）。`,
      "pois",
    );
  } else {
    setLayerHint("", "pois");
  }
}

function drawReferenceCircle(result) {
  clear(scene.ref);
  if (!el.reference.checked || !result) return;
  const vertices = normalizeVertices(result);
  let max = 0;
  for (const v of vertices) max = Math.max(max, Math.hypot(...toScene(v.lnglat)));
  if (!(max > 0)) return;
  scene.ref.append(
    svgNode("circle", { cx: 0, cy: 0, r: max, class: "ref-circle" }),
  );
}

function heatColor(minutes, target) {
  const t = Math.max(0, Math.min(1.2, minutes / (target || HEAT_TARGET_FALLBACK)));
  const hue = 120 * (1 - Math.min(1, t)); // 0 min → 绿（120°），到点 → 红（0°）
  const light = t > 1 ? 78 : 52;
  const alpha = t > 1 ? 0.28 : 0.62;
  return { fill: hslToHex(hue, 68, light), opacity: alpha };
}

/** HSL → #rrggbb：JSAPI 的 `fillColor` 不认 `hsl()`，两边共用一份 hex 才不会出现两种色。 */
function hslToHex(h, s, l) {
  const sat = s / 100;
  const lig = l / 100;
  const k = (n) => (n + h / 30) % 12;
  const a = sat * Math.min(lig, 1 - lig);
  const f = (n) => lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const hex = (value) =>
    Math.round(255 * Math.max(0, Math.min(1, value)))
      .toString(16)
      .padStart(2, "0");
  return `#${hex(f(0))}${hex(f(8))}${hex(f(4))}`;
}

/**
 * 渲染世代号：一次 renderAll 一个号，跨帧的分批绘制（热力层）与分帧队列都要在执行前对号。
 * 热力层要跨帧分批，没有守卫时连续两次开热力层会各跑一个批次、往同一个 group 塞两遍格子
 * （实跑量到 49 格画成 58 个 rect）。但只给批次加号也不够——舞台整幅重建（ensureScene）
 * 会把热力 group 换成新节点，而旧队列里排着的那一步 renderStage 会抢在新批次 drawHeat
 * 之后把号加掉，最新一轮热力层被自己的旧队列误杀（改后第一次实跑量到 0 个 rect）。
 * 所以「整次渲染」共用一个号：旧的整条队列自己作废，而不是去杀新的。
 */
let renderToken = 0;

function isStale(token) {
  return token !== renderToken;
}

function drawHeat(result, onDone, token) {
  const myToken = token === undefined ? renderToken : token;
  clear(scene.heat);
  if (!el.heat.checked) {
    // 关掉热力层时要把话说完：否则状态行还留着上一轮的「绘制 477 格」，像是有残影。
    setLayerHint("耗时热力层已关闭：上一轮的网格已整组清除（不留残影）。");
    if (onDone) onDone();
    return;
  }
  if (!result) {
    setLayerHint("热力层与顶点只在有结果后可用。");
    if (onDone) onDone();
    return;
  }
  const { cells, stepM } = heatCells(result);
  if (!cells.length) {
    setLayerHint("本次响应没有 duration_field，热力层无内容可画。");
    if (onDone) onDone();
    return;
  }
  const target = intOf(result.minutes) || HEAT_TARGET_FALLBACK;
  const sampled = cells.length > MAX_HEAT_CELLS;
  const stride = sampled ? Math.ceil(cells.length / MAX_HEAT_CELLS) : 1;
  const half = stepM / 2;
  // 分帧绘制（FR-205「长任务不白屏」的前端那一半）：每帧最多 40 格。
  let cursor = 0;
  const batch = () => {
    if (isStale(myToken)) return; // 已被新一轮渲染接管，旧批次静默退出
    const limit = Math.min(cursor + 40, cells.length);
    for (; cursor < limit; cursor += stride) {
      const cell = cells[cursor];
      const [x, y] = toScene(cell.lnglat);
      const color = heatColor(cell.minutes, target);
      scene.heat.append(
        svgNode("rect", {
          x: x - half,
          y: y - half,
          width: stepM,
          height: stepM,
          fill: color.fill,
          "fill-opacity": color.opacity,
          class: "heat-cell",
        }),
      );
    }
    if (cursor < cells.length) {
      requestAnimationFrame(batch);
    } else {
      setLayerHint(
        sampled
          ? `热力层抽样绘制（${cells.length} 格取每 ${stride} 格）；步长 ${fmtKm(stepM)}。`
          : `热力层绘制 ${cells.length} 格，网格步长 ${fmtKm(stepM)}（IDW 插值耗时）。`,
      );
      if (onDone) onDone();
    }
  };
  requestAnimationFrame(batch);
}

function drawPolygon(result, mode, group) {
  clear(group);
  const ring = polygonRing(result);
  if (ring.length < 3) return;
  const points = ring.map(toScene);
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)} ${p[1].toFixed(2)}`)
    .join(" ");
  group.append(
    svgNode("path", {
      d: `${d} Z`,
      class: `poly poly-${mode}`,
      "vector-effect": "non-scaling-stroke",
    }),
  );
}

function drawVertices(result) {
  clear(scene.vertices);
  const vertices = normalizeVertices(result);
  for (const v of vertices) {
    const [x, y] = toScene(v.lnglat);
    const dot = svgNode("circle", {
      cx: x,
      cy: y,
      r: VERTEX_RADIUS_PX / state.view.scale,
      class: `vertex sample-${v.sample || "unknown"}`,
      tabindex: "0",
      role: "button",
      "aria-label": `顶点 ${v.index + 1}，方位角 ${num(v.bearing, 0)} 度`,
    });
    dot.addEventListener("click", (event) => onVertexPick(v, event));
    dot.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onVertexPick(v, null);
      }
    });
    scene.vertices.append(dot);
  }
}

function drawCenter() {
  clear(scene.center);
  const r = (VERTEX_RADIUS_PX * 0.85) / state.view.scale;
  const arm = 12 / state.view.scale;
  scene.center.append(
    svgNode("line", { x1: -arm, y1: 0, x2: arm, y2: 0, class: "cross" }),
    svgNode("line", { x1: 0, y1: -arm, x2: 0, y2: arm, class: "cross" }),
    svgNode("circle", { cx: 0, cy: 0, r, class: "center-dot" }),
  );
  scene.center.addEventListener("click", () => {
    showTooltip(
      "中心点",
      [
        `${num(state.center[0], 5)}, ${num(state.center[1], 5)}（BD09LL）`,
        state.pickMode ? "选点模式开启中：点击图上任意位置可换中心点" : "已按 FR-101 设定",
      ],
      null,
    );
  });
}

/**
 * FR-203 路径串取三形状兼容（实跑对齐后端时发现字段名比契约更宽，两种都得吃）：
 * ① `path_strings`：契约里的 `["经度,纬度;经度,纬度", …]`（steps[].path 原样串）
 * ② `path`：后端实际返回的 GeoJSON LineString `{type, coordinates:[[lng,lat], …]}`
 * ③ `polyline` / `points`：兜底
 */
function pathLnglat(result) {
  if (!result || typeof result !== "object") return [];
  const candidates = [
    result.path && Array.isArray(result.path.coordinates) ? result.path.coordinates : null,
    result.polyline && Array.isArray(result.polyline.coordinates)
      ? result.polyline.coordinates
      : null,
    Array.isArray(result.points) ? result.points : null,
  ];
  const out = [];
  for (const group of candidates) {
    if (!group) continue;
    for (const item of list(group)) {
      const p = pair(item);
      if (p) out.push(p);
    }
    if (out.length) return out;
  }
  for (const chunk of list(result.path_strings)) {
    if (Array.isArray(chunk)) {
      for (const item of list(chunk)) {
        const p = pair(item);
        if (p) out.push(p);
      }
      continue;
    }
    for (const piece of String(chunk || "").split(";")) {
      const parts = piece.split(",");
      const p = pair([Number(parts[0]), Number(parts[1])]);
      if (p) out.push(p);
    }
  }
  return out;
}

/** 后端把「这条线是不是真实路网」写在 `degraded` / `signals` 里，前端不能一律说成真实路径。 */
function pathHonestLabel(result) {
  const signals = list(result && result.signals).map(String);
  if (result && (result.degraded || signals.includes("synthetic") || result.data_source === "sample")) {
    return "合成示意线，非真实路网轨迹";
  }
  return "真实路网轨迹";
}

/**
 * 图层说明。带 `key` 时按槽位**并存**（盲区层、POI 层、热力层各占一格），
 * 不带 `key` 时沿用旧的「整条覆盖」语义（既有的热力层调用点不受影响）。
 *
 * 为什么需要并存：M2 时期只有热力层一个动态图层，谁写谁生效没问题；加了 FR-212/FR-222
 * 两层之后，三个图层会互相把对方的说明挤掉 —— 表现是「勾了 POI 层，热力层的说明消失」，
 * 而用户完全不知道是覆盖还是没生效。
 */
function setLayerHint(text, key) {
  if (key) {
    if (text) state.layerNotes[key] = text;
    else delete state.layerNotes[key];
    const parts = [state.layerNotes.default, ...Object.entries(state.layerNotes)
      .filter(([slot]) => slot !== "default")
      .map(([, value]) => value)].filter(Boolean);
    el.layerHint.textContent = parts.join("  ");
    return;
  }
  state.layerNotes.default = text;
  setLayerHint(null, "blind"); // 触发一次重算，保持 keyed 槽位与 default 一起显示
}

function setStageHint(extra) {
  if (extra) {
    el.stageHint.textContent = String(extra);
    return;
  }
  const result = state.activeResult;
  const parts = [];
  if (state.jsapi.phase !== "live") {
    parts.push("SVG 舞台：坐标按等距近似投影，量纲为米；缩放平移由数据重投影，结构上不会错位");
  }
  if (ringApproximated) {
    parts.push("⚠️ 本次未返回 polygon.coordinates，多边形由顶点按方位角闭合近似绘制（不作为算法结论）");
  }
  if (result) {
    parts.push(
      `面积 ${num(result.area_km2, 3)} km² ｜ 顶点 ${normalizeVertices(result).length} ｜ 取数 ${
        DATA_SOURCE_LABEL[result.data_source] || result.data_source || "—"
      }`,
    );
  } else {
    parts.push("未运行分析：底图网格与中心点为占位，不含任何算法结论");
  }
  el.stageHint.textContent = parts.join(" ｜ ");
}

// ---------------------------------------------------------------------------------------------
// Tooltip（FR-203）
// ---------------------------------------------------------------------------------------------

function showTooltip(title, lines, event) {
  clear(el.tooltip);
  el.tooltip.append(node("h3", null, title));
  const ul = node("ul");
  for (const line of lines) {
    if (line) ul.append(node("li", null, line));
  }
  el.tooltip.append(ul);
  el.tooltip.hidden = false;
  positionTooltip(event);
}

function positionTooltip(event) {
  const rect = el.stageArea.getBoundingClientRect();
  let x = rect.width - 16;
  let y = 16;
  if (event && Number.isFinite(event.clientX)) {
    x = event.clientX - rect.left;
    y = event.clientY - rect.top;
  }
  const box = el.tooltip.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.width - box.width - 12, x - box.width / 2));
  const top = Math.max(8, Math.min(rect.height - box.height - 12, y + 14));
  el.tooltip.style.left = `${left}px`;
  el.tooltip.style.top = `${top}px`;
}

function hideTooltip() {
  el.tooltip.hidden = true;
}

function vertexLines(v) {
  return [
    `方位角 ${Number.isFinite(v.bearing) ? `${num(v.bearing, 1)}°` : "—"}`,
    `实测耗时 ${Number.isFinite(v.minutes) ? `${num(v.minutes, 1)} min` : "—"}`,
    `路径距离 ${Number.isFinite(v.distanceM) ? fmtKm(v.distanceM) : "—"}`,
    `采样：${v.sampleLabel}`,
    `置信 ${num(v.confidence, 2)} ｜ 坐标 ${num(v.lnglat[0], 5)}, ${num(v.lnglat[1], 5)}`,
  ];
}

/** 顶点 tooltip：FR-203 验收要的那三样（方位角 / 实测耗时 / 路径距离）永远在最前面。 */
function vertexTooltip(vertex, extraLine, event) {
  showTooltip(
    `顶点 #${vertex.index + 1}（${MODE_LABEL[state.activeMode] || state.activeMode}）`,
    vertexLines(vertex).concat(extraLine ? [extraLine] : []),
    event,
  );
}

/**
 * D-1（P2）修复：FR-203 的验收写的是「边界点**可点击查看**：该方向的实测步行时间与距离」，
 * 而浏览器走查发现点顶点本身只弹 tooltip、不发 `/api/isochrone/polyline`（只有明细表里的
 * 按钮才触发）。这里让顶点点击（SVG 与 JSAPI 两条路径都汇到本函数）走**同一条**懒加载逻辑：
 * `loadPolyline` 里已有 12 次上限守卫与 `polylineSeq` 世代号，不新开一套计数、也不绕过它们。
 * tooltip 先按顶点自身数据落地（方位角 / 实测耗时 / 路径距离），路径回来后 `loadPolyline`
 * 会再刷新一次同一个 tooltip；连点时过期响应由 `polylineSeq` 丢弃。
 */
function onVertexPick(vertex, event) {
  if (state.pickMode) return; // 选点模式下点顶点 = 换中心点，不弹 tooltip、不补拉路径
  vertexTooltip(vertex, "正在补拉该方向真实路径…", event);
  if (state.jsapi.phase === "live") highlightLiveVertex(vertex);
  loadPolyline(vertex, event);
}

// ---------------------------------------------------------------------------------------------
// 渲染主流程：分阶段（FR-205）
// ---------------------------------------------------------------------------------------------

function renderStage() {
  if (state.jsapi.phase === "live") {
    renderLive();
    return;
  }
  ensureScene();
  applyView();
  drawGrid();
  drawReferenceCircle(state.activeResult);
  const result = state.activeResult;
  drawPolygon(result, state.activeMode, scene.poly);
  if (el.compare.checked) {
    drawPolygon(state.results[otherMode(state.activeMode)], otherMode(state.activeMode), scene.other);
  } else {
    clear(scene.other);
  }
  drawCenter();
  drawBlindSpots();
  drawPois();
  drawVertices(result);
  const realPath = pathLnglat(state.lastPolylineResult);
  if (realPath.length > 1) {
    const d = realPath
      .map(toScene)
      .map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)} ${p[1].toFixed(2)}`)
      .join(" ");
    scene.vertices.append(
      svgNode("path", { d, class: "real-path", "vector-effect": "non-scaling-stroke" }),
    );
  }
}

/** FR-205：一次结果分帧落地——先多边形、再顶点、再热力格，避免长任务把主线程卡成白屏。 */
function renderAll(resetView) {
  const token = ++renderToken; // 这一轮渲染的世代号，旧队列与旧批次看到它不是最新就自己收手
  ringApproximated = false;
  if (resetView && state.jsapi.phase !== "live") {
    resizeStage();
    fitView();
  } else if (state.jsapi.phase === "live") {
    liveFit();
  }
  renderModeBadge();
  renderResultMeta(state.activeResult);
  renderUsage(state.activeResult);
  renderProgress(state.activeResult, false);
  renderVerticesTable(state.activeResult);
  renderLegend();
  setStageHint();
  renderAkState();
  const steps = [renderStage];
  // 🔴 世代号必须一路带到跨帧批次里：`scene.heat` 是「当前节点」的属性查找，旧队列的批次
  // 回调不改指针也能把格子塞进新一轮的 group —— 这正是 49 格画成 58 个 rect 的根因。
  // 实时底图（JSAPI）那一支的热力格由 renderLive 自己画，不能再排一次 SVG 批次。
  // 🔴 这里不能加 `el.heat.checked` 短路：关掉热力层时也要进 drawHeat，
  // 由它 clear(scene.heat) 把上一轮的网格整组收掉；否则关掉的瞬间只剩残影。
  if (state.jsapi.phase !== "live") {
    steps.push((next) => drawHeat(state.activeResult, next, token));
  }
  runQueue(steps, token);
}

/**
 * 分帧跑渲染步骤：形参个数 > 0 的步骤自己决定何时交接（热力层要跨帧），
 * 否则一帧一步。任何一步抛错都只写进舞台说明——白屏是 R13 里最坏的现场。
 * 世代号过期时整条旧队列立刻停手，把主线程让给最新那一轮。
 */
function runQueue(steps, token) {
  const queue = steps.slice();
  const tick = () => {
    if (token !== undefined && isStale(token)) return;
    const step = queue.shift();
    if (!step) return;
    let handed = false;
    const next = () => {
      if (handed) return;
      handed = true;
      requestAnimationFrame(tick);
    };
    try {
      if (step.length > 0) step(next);
      else {
        step();
        next();
      }
    } catch (error) {
      setStageHint(`渲染中断：${error && error.name ? error.name : String(error)}`);
      if (!handed) requestAnimationFrame(tick);
    }
  };
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------------------------------------
// JSAPI 动态注入与实时底图（R13 的落点）
// ---------------------------------------------------------------------------------------------

function injectJsapi(ak) {
  if (window.BMap && window.BMap.Map) return Promise.resolve(window.BMap);
  // 同一 AK 的在飞注入只留一条脚本：连点「开始分析」/ 反复切模式时插进多个
  // `api.map.baidu.com/api` 脚本，等于让「哪一次注入赢」变成不可控竞态（R13）。
  if (state.jsapi.pending && state.jsapi.pendingAk === ak) return state.jsapi.pending;
  const promise = new Promise((resolve, reject) => {
    let settled = false;
    let script = null;
    const drop = () => {
      if (script && script.parentNode) script.remove();
    };
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      fn(value);
    };
    window[JSAPI_CALLBACK] = () => finish(resolve, window.BMap || window.BMapGL || null);
    const timer = window.setTimeout(() => {
      drop(); // 超时后撤掉脚本，用户改完 AK 重试时才是干净的一次注入
      finish(reject, new Error("jsapi_timeout"));
    }, JSAPI_TIMEOUT_MS);
    script = document.createElement("script");
    script.src = `${JSAPI_BASE}?v=${JSAPI_VERSION}&ak=${encodeURIComponent(ak)}&callback=${JSAPI_CALLBACK}`;
    script.async = true;
    script.addEventListener("error", () => {
      drop();
      finish(reject, new Error("jsapi_network_error"));
    });
    document.head.append(script);
  });
  state.jsapi.pending = promise;
  state.jsapi.pendingAk = ak;
  const forget = () => {
    if (state.jsapi.pending === promise) {
      state.jsapi.pending = null;
      state.jsapi.pendingAk = "";
    }
  };
  promise.then(forget, forget); // 只用来清理引用，拒绝仍由调用方处理
  return promise;
}

async function tryEnableLiveMap() {
  const want = jsapiWanted();
  if (!want.use) {
    setEngineBadge("svg", want.reason);
    return false;
  }
  setEngineBadge("loading", "");
  const epoch = state.jsapi.epoch; // 本次注入尝试的世代号（setEngineBadge 刚刚自增过）
  try {
    const BMap = await injectJsapi(want.ak);
    // 🔴 过期注入丢弃：await 期间用户可能已把数据模式切回 mock、清了 AK，或页面已经降级过。
    // 不丢弃的话，一张「已经不想要实时底图」的页面会被旧注入强行切成 live（R13 的反面）。
    if (epoch !== state.jsapi.epoch) {
      el.runHint.textContent = "已忽略一次过期的 JSAPI 注入结果（参数在此期间已变更）。";
      return false;
    }
    const stillWanted = jsapiWanted();
    if (!stillWanted.use) {
      degradeToSvg(`注入完成前不再满足条件：${stillWanted.reason}`);
      return false;
    }
    if (!BMap || typeof BMap.Map !== "function") {
      throw new Error("jsapi_namespace_missing");
    }
    state.jsapi.map = new BMap.Map(el.mapLive);
    state.jsapi.bmap = BMap;
    state.jsapi.phase = "live";
    setEngineBadge("live", "");
    if (state.center) {
      state.jsapi.map.centerAndZoom(new BMap.Point(state.center[0], state.center[1]), 14);
    }
    for (const method of ["enableScrollWheelZoom", "enableDragging"]) {
      try {
        if (typeof state.jsapi.map[method] === "function") state.jsapi.map[method]();
      } catch (error) {
        /* 可选交互：失败不影响底图 */
      }
    }
    state.jsapi.map.addEventListener("click", (event) => onLiveMapClick(event));
    renderAll(true);
    return true;
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    if (epoch !== state.jsapi.epoch) {
      // 过期注入的失败不值得吓用户：期间已经有人改过参数并重新判定了引擎状态。
      setEngineBadge("svg", `过期注入已忽略：${message}`);
      return false;
    }
    degradeToSvg(message);
    return false;
  }
}

function degradeToSvg(reason) {
  /** 🔴 唯一的失败出口：把实时底图整块换成 SVG，页面其余部分照常可用（不白屏、不变红）。 */
  liveClearOverlays();
  state.jsapi.map = null;
  state.jsapi.bmap = null;
  setEngineBadge("failed", reason);
  setBanner(
    "warning",
    "实时底图不可用，已降级为离线示意图",
    "百度 JSAPI 运行时与地图瓦片无法本地化（R13），注入失败或超时后本页改用合成 SVG 舞台：等时圈、顶点、热力层与全部数字结论仍然照常渲染，只是底图不是真实地图。",
    [`降级原因：${reason}`, "处置：填入浏览器端类型 AK 后重试，或改用 mock/cache 模式看完整结论。"],
    "地图区域已标注「离线示意图，非真实地图」。",
  );
  renderAll(true);
}

function onLiveMapClick(event) {
  const point = event && event.point;
  if (state.pickMode && point) {
    setCenter([Number(point.lng), Number(point.lat)], "在实时底图上点选");
    return;
  }
  hideTooltip();
}

function liveClearOverlays() {
  const map = state.jsapi.map;
  if (!map) return;
  for (const overlay of state.jsapi.overlays) {
    try {
      map.removeOverlay(overlay);
    } catch (error) {
      /* 覆盖物已随地图销毁 */
    }
  }
  state.jsapi.overlays = [];
}

function liveAdd(overlay) {
  state.jsapi.map.addOverlay(overlay);
  state.jsapi.overlays.push(overlay);
  return overlay;
}

function liveFit() {
  const BMap = state.jsapi.bmap;
  const map = state.jsapi.map;
  if (!BMap || !map) return;
  const points = [];
  for (const lnglat of polygonRing(state.activeResult)) points.push(new BMap.Point(lnglat[0], lnglat[1]));
  if (!points.length) return;
  try {
    map.setViewport(points);
  } catch (error) {
    try {
      map.centerAndZoom(points[0], 14);
    } catch (inner) {
      /* 视野调整失败时保持当前视野，数据层照常渲染 */
    }
  }
}

function renderLive() {
  const BMap = state.jsapi.bmap;
  const map = state.jsapi.map;
  if (!BMap || !map) {
    degradeToSvg("JSAPI 命名空间在渲染时已不可用");
    return;
  }
  liveClearOverlays();
  const result = state.activeResult;
  const ring = polygonRing(result);
  try {
    if (ring.length >= 3) {
      const points = ring.map((p) => new BMap.Point(p[0], p[1]));
      // FR-202：半透明填充 + 描边；覆盖物用经纬度构造，缩放平移由 JSAPI 保证贴合路网。
      liveAdd(
        new BMap.Polygon(points, {
          strokeColor: state.activeMode === "riding" ? "#2f6f4f" : "#2d5f8f",
          strokeWeight: 2,
          strokeOpacity: 0.9,
          fillColor: state.activeMode === "riding" ? "#57b78a" : "#5fa8d8",
          fillOpacity: 0.25,
        }),
      );
    }
    if (el.compare.checked) {
      const other = polygonRing(state.results[otherMode(state.activeMode)]);
      if (other.length >= 3) {
        liveAdd(
          new BMap.Polygon(
            other.map((p) => new BMap.Point(p[0], p[1])),
            {
              strokeColor: "#8a6d3b",
              strokeWeight: 2,
              strokeStyle: "dashed",
              fillColor: "#d9b86a",
              fillOpacity: 0.12,
            },
          ),
        );
      }
    }
    if (el.reference.checked) {
      const vertices = normalizeVertices(result);
      let max = 0;
      for (const v of vertices) max = Math.max(max, straightDistance(v.lnglat));
      const center = pair(result && result.center) || state.center;
      if (max > 0 && center && typeof BMap.Circle === "function") {
        liveAdd(
          new BMap.Circle(new BMap.Point(center[0], center[1]), max, {
            strokeColor: "#9aa3af",
            strokeStyle: "dashed",
            strokeWeight: 1,
            fillOpacity: 0,
          }),
        );
      }
    }
    if (el.heat.checked && result) {
      const { cells, stepM } = heatCells(result);
      const target = intOf(result.minutes) || HEAT_TARGET_FALLBACK;
      const stride = cells.length > MAX_HEAT_CELLS ? Math.ceil(cells.length / MAX_HEAT_CELLS) : 1;
      const radius = stepM / 2;
      let drawn = 0;
      for (let i = 0; i < cells.length; i += stride) {
        const cell = cells[i];
        if (typeof BMap.Circle !== "function") break;
        const color = heatColor(cell.minutes, target);
        // FR-204 有 JSAPI 的一支：用官方 `Circle` 覆盖物按网格步长铺色（4.0 无内置热力图，
        // 笔记 §5.3 明确「不得假设自带」），自绘网格既免第三方库也不触碰数据分发条款。
        liveAdd(
          new BMap.Circle(new BMap.Point(cell.lnglat[0], cell.lnglat[1]), radius, {
            strokeOpacity: 0,
            fillColor: color.fill,
            fillOpacity: color.opacity,
          }),
        );
        drawn += 1;
      }
      // 覆盖物每轮都先 liveClearOverlays() 清干净，所以这里不可能出现「一格两层」。
      setLayerHint(
        cells.length
          ? `热力层绘制 ${drawn} 格（实时底图覆盖物，网格步长 ${fmtKm(stepM)}）。`
          : "本次响应没有 duration_field，热力层无内容可画。",
      );
    }
    const path = pathLivePoints();
    if (path.length > 1) {
      liveAdd(new BMap.Polyline(path, { strokeColor: "#c2410c", strokeWeight: 3, strokeOpacity: 0.9 }));
    }
    // FR-222：盲区块用官方 Polygon 覆盖物（同 SVG 路径的配色语义）。
    // 一个块可能有多个分离环（MultiPolygon）→ 每个环一个覆盖物，共用同一个点击弹窗。
    if (el.showBlind && el.showBlind.checked && typeof BMap.Polygon === "function") {
      for (const block of blindBlocks()) {
        const style = BLIND_STYLE[block.level];
        for (const ring of block.rings) {
          const polygon = new BMap.Polygon(
            ring.map((p) => new BMap.Point(p[0], p[1])),
            {
              strokeColor: style.stroke,
              strokeWeight: 1.5,
              fillColor: style.fill,
              fillOpacity: style.fillOpacity,
            },
          );
          polygon.addEventListener("click", (event) => {
            if (state.pickMode) return;
            showTooltip(
              `服务盲区 #${block.index + 1}：${style.label}`,
              blindLines(block),
              event && event.domEvent ? event.domEvent : event,
            );
          });
          liveAdd(polygon);
        }
      }
    }
    // FR-212：POI 用官方 Circle 覆盖物。半径是**米**，取 20 m —— 缩放 15 级时约 7 px，
    // 再小就点不中、再大就盖住邻点。颜色按维度区分（JSAPI 4.0 自定义图标要外部图片，R12 禁外链）。
    if (el.showPois && el.showPois.checked && typeof BMap.Circle === "function") {
      for (const poi of state.overlays.pois || []) {
        if (!poi.in_polygon) continue;
        const lnglat = pair(poi.lnglat);
        if (!lnglat) continue;
        const style = poiStyle(poi.category);
        const dot = new BMap.Circle(new BMap.Point(lnglat[0], lnglat[1]), 20, {
          strokeColor: "#ffffff",
          strokeWeight: 1,
          fillColor: style.color,
          fillOpacity: poi.unmeasured ? 0.45 : 0.95,
        });
        dot.addEventListener("click", (event) => {
          if (state.pickMode) return;
          showTooltip(
            poi.name || "设施",
            poiLines(poi),
            event && event.domEvent ? event.domEvent : event,
          );
        });
        liveAdd(dot);
      }
    }
    for (const v of normalizeVertices(result)) {
      const marker = new BMap.Marker(new BMap.Point(v.lnglat[0], v.lnglat[1]));
      marker.addEventListener("click", (event) => {
        onVertexPick(v, event && event.domEvent ? event.domEvent : event);
        highlightLiveVertex(v);
      });
      liveAdd(marker);
    }
    liveAdd(new BMap.Marker(new BMap.Point(state.center[0], state.center[1])));
  } catch (error) {
    degradeToSvg(`实时覆盖物绘制异常：${error && error.name ? error.name : String(error)}`);
  }
}

function pathLivePoints() {
  const BMap = state.jsapi.bmap;
  if (!BMap) return [];
  return pathLnglat(state.lastPolylineResult).map((p) => new BMap.Point(p[0], p[1]));
}

function highlightLiveVertex(vertex) {
  const BMap = state.jsapi.bmap;
  const map = state.jsapi.map;
  if (!BMap || !map || typeof map.getZoom !== "function") return;
  try {
    map.panTo(new BMap.Point(vertex.lnglat[0], vertex.lnglat[1]));
  } catch (error) {
    /* 平移失败时 tooltip 仍然有效 */
  }
}

function straightDistance(lnglat) {
  const [x, y] = toScene(lnglat);
  return Math.hypot(x, y);
}

// ---------------------------------------------------------------------------------------------
// 请求：/api/health /api/geocode /api/regeo /api/suggest /api/isochrone /api/isochrone/polyline
// ---------------------------------------------------------------------------------------------

async function readJson(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (error) {
    return { ok: false, parse_error: "响应不是合法 JSON", snippet: text.slice(0, 160) };
  }
}

async function loadHealth() {
  try {
    const response = await fetch(HEALTH_PATH, { headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(`http_${response.status}`);
    const body = await response.json();
    state.health = body;
    state.healthBanner = body.banner || null; // 失败态要把「为什么不是实时」一起说，不能把 FR-105 的横幅吃掉
    const inEffect = (body.config && body.config.data_mode_in_effect) || body.data_mode || "mock";
    if (!el.dataMode.value) state.effectiveDataMode = inEffect;
    renderModeBadge();
    renderAkState();
    const banner = body.banner || {};
    if (banner.show === true) {
      setBanner(
        banner.level || "warning",
        banner.title || "当前不是实时数据",
        banner.message || "",
        list(banner.detail_messages),
        [banner.cta_hint, banner.map_placeholder_text].filter(Boolean).join("；"),
      );
    }
  } catch (error) {
    state.effectiveDataMode = el.dataMode.value || "mock";
    setBanner(
      "warning",
      "未能读取 /api/health",
      "页面按离线模式继续工作：仍可输入中心点、运行 mock 分析并查看等时圈，只是拿不到实时探测结论。",
      [`请求异常：${error && error.message ? error.message : String(error)}`],
      "确认服务已启动（run.sh / run.ps1）后刷新本页。",
    );
    renderModeBadge();
    renderAkState();
  }
}

/**
 * FR-103：坐标 → 结构化地址（行政区 / 街道 / 门牌），补掉「面板上只有坐标」这一项。
 *
 * 🔴 三条守卫一个都不能省：
 * ① 同一坐标只问一次 —— 逆地理走 0.5 QPS 的桶，换点连点会把令牌排满；
 * ② 世代号挡住过期响应，否则「点已经换了，地址行还在坠上一份」；
 * ③ 失败绝不打断主流程 —— 地址只是回显，等时圈与体检都不依赖它。
 */
async function fetchCenterAddress(lnglat) {
  const line = el.centerAddress;
  if (!line) return;
  const key = `${lnglat[0].toFixed(5)},${lnglat[1].toFixed(5)}`;
  const cached = state.address.cache.get(key);
  if (cached) {
    renderAddressLine(cached);
    return;
  }
  if (state.address.pending === key) return; // 同坐标的请求还在飞，不重复占桶
  state.address.pending = key;
  const seq = (state.address.seq += 1);
  line.dataset.state = "pending";
  line.textContent = `中心点地址：解析中…（${key}）`;
  try {
    const params = new URLSearchParams({ lnglat: `${lnglat[0]},${lnglat[1]}` });
    if (el.dataMode && el.dataMode.value) params.set("data_mode", el.dataMode.value);
    const response = await fetch(`${REGEO_PATH}?${params.toString()}`);
    const body = await readJson(response);
    if (seq !== state.address.seq) return; // 中心点又变了：这份响应已不属于当前视野
    if (!response.ok) {
      throw new Error((body && body.error && body.error.message) || `http_${response.status}`);
    }
    state.address.cache.set(key, body);
    renderAddressLine(body);
  } catch (error) {
    if (seq !== state.address.seq) return;
    line.dataset.state = "error";
    line.textContent = `中心点地址：没取到（${
      error && error.message ? error.message : String(error)
    }）。不影响等时圈与体检。`;
  } finally {
    if (state.address.pending === key) state.address.pending = "";
  }
}

/**
 * 把 `/api/regeo` 的官方字段摊成一行。
 *
 * 空结果按「可读文案」处理而不是报错：后端在 mock / 无 AK 下回 `ok=false` + 一句人话
 * （合成层不替逆地理造地址），这句话本身就是要显示的内容。
 */
function renderAddressLine(body) {
  const line = el.centerAddress;
  const source = DATA_SOURCE_LABEL[body.data_source] || body.data_source || "—";
  const parts = [
    ["行政区", body.district],
    ["街道", body.town],
    ["门牌", [body.street, body.street_number].filter(Boolean).join("")],
    ["adcode", body.adcode],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "");
  clear(line);
  if (body.ok !== true || (!body.formatted_address && !parts.length)) {
    line.dataset.state = "empty";
    line.appendChild(node("b", "", `中心点地址：${String(body.message || "未取到可读地址").trim()}`));
    line.appendChild(node("span", "address-part", `取数层：${source}`));
    return;
  }
  line.dataset.state = "ok";
  line.appendChild(node("b", "", `中心点地址：${body.formatted_address}`));
  parts.forEach(([label, value]) => line.appendChild(node("span", "address-part", `${label} ${value}`)));
  line.appendChild(node("span", "address-part", `取数层：${source}`));
}

async function runGeocode() {
  const address = el.addr.value.trim();
  if (!address) {
    el.centerReadout.textContent = "地址搜索：请先填写地址文本（FR-101 方式②）。";
    return;
  }
  el.geocode.disabled = true;
  el.centerReadout.textContent = `正在地理编码「${address}」…`;
  try {
    const params = new URLSearchParams({ address });
    const city = el.city.value.trim();
    if (city) params.set("city", city);
    if (el.dataMode.value) params.set("data_mode", el.dataMode.value);
    const response = await fetch(`${GEOCODE_PATH}?${params.toString()}`);
    const body = await readJson(response);
    const lnglat = readGeoLnglat(body);
    if (!response.ok || !lnglat) {
      throw new Error(
        (body && body.error && body.error.message) ||
          (body && body.message) ||
          `http_${response.status}`,
      );
    }
    setCenter(lnglat, `地址「${address}」解析成功（${DATA_SOURCE_LABEL[body.data_source] || body.data_source}）`);
  } catch (error) {
    // 地理编码失败不能打断主流程：留在原中心点，只把原因说清楚。
    el.centerReadout.textContent = `地址搜索未完成：${error && error.message ? error.message : String(error)}。已保留原中心点。`;
  } finally {
    el.geocode.disabled = false;
  }
}

function readGeoLnglat(body) {
  if (!body || typeof body !== "object") return null;
  const direct = pair(body.lnglat);
  if (direct) return direct;
  const location = body.result && body.result.location;
  const shaped = pair([location && location.lng, location && location.lat]);
  return shaped;
}

// ---------------------------------------------------------------------------------------------
// FR-108 地址联想：/api/suggest（内部 /place/v2/suggestion）
// ---------------------------------------------------------------------------------------------

const SUGGEST_MIN_KEYWORD_LEN = 3;
// 与后端 `services/geo.SUGGEST_MIN_KEYWORD_LEN` 同值：两边算法不一致会出现「前端放行了、
// 接口回 400」。门槛存在的理由是它与地点检索共用每日 100 次配额（笔记 FAQ 2540 / 2549）。

const SUGGEST_DEBOUNCE_MS = 500;
// PRD FR-108 原文要求的防抖时长。

function codePointLength(value) {
  // 后端数的是 Python `len()`（码点），`String.length` 数的是 UTF-16 单元：一个 emoji 或生僻字
  // 会被算成 2，于是「前端觉得够了、后端 400」。用 Array.from 对齐。
  return Array.from(value || "").length;
}

function suggestBlockReason(word, region) {
  if (codePointLength(word) < SUGGEST_MIN_KEYWORD_LEN) return "short";
  if (!region) return "need_region";
  return "";
}

function setSuggestState(value) {
  el.suggestList.dataset.state = value;
  // 列表可见 ⇔ aria-expanded，两个状态分开维护就会出现「读屏说有弹层、屏幕上什么都没有」。
  el.addr.setAttribute("aria-expanded", value === "none" ? "false" : "true");
  // 候选列表是**文档流里**的一块，不是浮层：移动端抽屉（FR-106）里 `#controls` 自己滚动，
  // 输入框下方 40 px 就是抽屉底沿，不滚一下就等于「打字有反应、候选看不见」。
  // `block: "nearest"` 只在真的在视口外时才动，已经看得见就是空操作。
  if (value !== "none") el.suggestList.scrollIntoView({ block: "nearest" });
}

function closeSuggest() {
  clear(el.suggestList);
  state.suggest.items = [];
  state.suggest.active = -1;
  setSuggestState("none");
}

function renderSuggestNote(kind, text) {
  clear(el.suggestList);
  el.suggestList.appendChild(node("li", "suggest-note", text));
  state.suggest.items = [];
  state.suggest.active = -1;
  setSuggestState(kind);
}

function suggestOption(item, index) {
  const row = node("li", "suggest-item");
  row.setAttribute("role", "option");
  row.setAttribute("aria-selected", "false");
  row.dataset.index = String(index);
  row.appendChild(node("span", "suggest-name", item.name));
  const where = [item.city, item.district].filter(Boolean).join(" ");
  if (where) row.appendChild(node("span", "suggest-part", where));
  // 坐标一并铺出来：候选项最终就是落点，评审与用户都能核对「点下去会落到哪」。
  row.appendChild(node("span", "suggest-part", `${item.lnglat[0].toFixed(5)}, ${item.lnglat[1].toFixed(5)}`));
  return row;
}

function renderSuggest(body) {
  const items = (Array.isArray(body.items) ? body.items : []).filter((item) => pair(item && item.lnglat));
  if (!items.length) {
    // 后端已经把没坐标和境外的条目丢过了；这里再挡一次形状漂移，宁可回一句「没有候选」，
    // 也不给一行「点了没反应」的候选。
    renderSuggestNote("empty", String(body.message || "没有匹配的联想结果，再输入几个字试试。"));
    return;
  }
  clear(el.suggestList);
  state.suggest.items = items;
  state.suggest.active = -1;
  items.forEach((item, index) => el.suggestList.appendChild(suggestOption(item, index)));
  setSuggestState("ok");
}

function onAddrInput() {
  clearTimeout(state.suggest.timer);
  if (!el.suggestOn.checked) {
    // 🔴 默认关闭这一半由前端把住：开关没勾就**一个请求都不发**，而不是发出去让后端拒绝。
    closeSuggest();
    return;
  }
  state.suggest.timer = setTimeout(fetchSuggestions, SUGGEST_DEBOUNCE_MS);
}

async function fetchSuggestions() {
  const word = el.addr.value.trim();
  const region = el.city.value.trim();
  const blocked = suggestBlockReason(word, region);
  if (blocked) {
    renderSuggestNote(
      blocked,
      blocked === "short"
        ? `再输入几个字（当前 ${codePointLength(word)} 字，门槛 ${SUGGEST_MIN_KEYWORD_LEN} 字）：太短的关键字召回一大片，白烧一次检索配额。`
        : "联想需要城市：官方把 region 列为必选参数。请在上面的「城市」里填一个，如「北京」。",
    );
    return;
  }
  const key = `${region}|${word}`;
  const cached = state.suggest.cache.get(key);
  if (cached) {
    renderSuggest(cached);
    return;
  }
  if (state.suggest.pending === key) return; // 同一个词的请求还在飞，不重复占桶
  state.suggest.pending = key;
  const seq = (state.suggest.seq += 1);
  renderSuggestNote("pending", `正在联想「${word}」…`);
  try {
    const params = new URLSearchParams({ keyword: word, region });
    if (el.dataMode.value) params.set("data_mode", el.dataMode.value);
    const response = await fetch(`${SUGGEST_PATH}?${params.toString()}`);
    const body = await readJson(response);
    if (seq !== state.suggest.seq) return; // 关键字又变了：这批候选已经不属于当前输入
    if (!response.ok) {
      throw new Error((body && body.error && body.error.message) || `http_${response.status}`);
    }
    // 只有「真的问过百度」的那两类结果才值得留下：把 mock / 未启用的提示缓存下来，
    // 用户随后开了 `SUGGEST_ENABLED` 也要到刷新才看得到候选。
    if (body.data_source === "live" || body.data_source === "cache") state.suggest.cache.set(key, body);
    renderSuggest(body);
  } catch (error) {
    if (seq !== state.suggest.seq) return;
    renderSuggestNote(
      "error",
      `联想未完成：${error && error.message ? error.message : String(error)}。` +
        "地址搜索与坐标取点不受影响。",
    );
  } finally {
    if (state.suggest.pending === key) state.suggest.pending = "";
  }
}

function moveSuggestActive(delta) {
  const count = state.suggest.items.length;
  if (!count) return false;
  const last = count - 1;
  const next =
    state.suggest.active < 0
      ? delta > 0
        ? 0
        : last
      : (state.suggest.active + delta + last + 1) % (last + 1);
  state.suggest.active = next;
  Array.from(el.suggestList.children).forEach((row, index) =>
    row.setAttribute("aria-selected", String(index === next)),
  );
  return true;
}

function chooseSuggestion(index) {
  const item = state.suggest.items[index];
  if (!item) return;
  el.addr.value = String(item.name); // 直接赋值不触发 input 事件，不会绕回来再问一次
  closeSuggest();
  setCenter(item.lnglat, `联想选中「${item.name}」`, "fly"); // PRD FR-108：flyTo 并落点
}

function onSuggestToggle() {
  clearTimeout(state.suggest.timer);
  if (!el.suggestOn.checked) {
    closeSuggest();
    return;
  }
  const word = el.addr.value.trim();
  const region = el.city.value.trim();
  const blocked = suggestBlockReason(word, region);
  if (blocked) {
    // 刚勾上就先说清下一步要什么，而不是留一个空列表等 500 ms 才冒字。
    fetchSuggestions();
    return;
  }
  state.suggest.timer = setTimeout(fetchSuggestions, SUGGEST_DEBOUNCE_MS);
}

function pendingRun() {
  return {
    mode: el.mode.value,
    minutes: Number(el.minutes.value),
    data_mode: el.dataMode.value || null,
  };
}

async function runIsochrone(request) {
  const payload = {
    lnglat: `${num(request.center[0], 6)},${num(request.center[1], 6)}`,
    mode: request.mode,
    minutes: request.minutes,
  };
  if (request.data_mode) payload.data_mode = request.data_mode;
  const seq = ++state.runSeq; // 🔴 请求世代号：在飞期间换了模式/中心点，这次响应不再抢走当前视图
  state.activeMode = request.mode;
  state.minutes = request.minutes;
  state.running = true;
  el.run.disabled = true;
  startRunTicker();
  renderProgress(null, true);
  el.runHint.textContent = "正在请求 /api/isochrone…";
  try {
    const response = await fetch(ISOCHRONE_PATH, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await readJson(response);
    const requestId = body.request_id || (response.headers.get("X-Request-Id") || "");
    // 这条路径也刷新「本页最近一次的号」：不刷的话，`state.requestId` 会一直停在上一份
    // 体检报告的号上，而 `currentRequestId()` 的兜底就是拿它填页脚——报一个**过期**的号给
    // 服务端日志比报 `—` 更糟。过期响应同样不许写（同 `runSeq` 那条规则）。
    if (seq === state.runSeq) state.requestId = requestId;
    if (!response.ok || body.ok === false) {
      if (seq !== state.runSeq) {
        el.runHint.textContent = `过期请求（${MODE_LABEL[request.mode]}）未成功，已忽略：当前视图属于更新的一次分析。`;
        return;
      }
      renderRequestFailure(response.status, body, requestId, request);
      return;
    }
    state.results[request.mode] = body;
    // 🔴 过期结果不抢视图：在飞期间换了出行方式或中心点，就把这次响应只当作「对比数据」存下来。
    // 若照常写 activeResult，会出现「用骑行的配色画步行的数据」（drawPolygon 的 mode 取 activeMode），
    // 以及换了中心点后把旧中心点的等时圈按新投影偏移渲染——两者都是看得见的错误结论。
    const superseded = seq !== state.runSeq;
    if (!superseded) {
      state.activeResult = body;
      state.lastPolylineResult = null;
      // 🔴 FR-203 的 12 次额度按「一次分析」记在后端会话里，新分析的响应就带着当前用量
      // （`usage.polyline_calls`）；直接用它起算本地计数，不再用「同 key 才有镜像」的猜测
      // ——后端计数不随新分析清零，前端自计会报「第 1/12 次」却被服务端拒。
      syncPolylineCalls(body);
    }
    state.effectiveDataMode = body.data_mode || state.effectiveDataMode;
    el.requestIdFoot.textContent = `request_id：${body.request_id || requestId || "—"}`;
    if (superseded) {
      setBanner(
        "ok",
        `${MODE_LABEL[request.mode] || request.mode} 结果已收到，但未替换当前视图`,
        `请求在飞期间改过出行方式或中心点，这次结果只存入对比数据（${
          normalizeVertices(body).length
        } 个顶点）。当前显示：${MODE_LABEL[el.mode.value] || el.mode.value}。`,
        [`request_id：${body.request_id || requestId || "—"}`],
        "把出行方式切回目标模式，或重新点「开始分析」。",
      );
      state.activeMode = el.mode.value;
      state.activeResult = state.results[el.mode.value] || null;
      renderAll(true);
      el.runHint.textContent = `已存下 ${MODE_LABEL[request.mode]} ${request.minutes} min 结果（未作为当前视图）。`;
      return;
    }
    if (body.degraded) {
      const source = DATA_SOURCE_LABEL[body.data_source] || body.data_source || "—";
      setBanner(
        "warning",
        `结果已降级（取数层：${source}）`,
        body.message || "后端未给出降级原因，但结果仍可用（详见计量与顶点采样方式）。",
        [
          body.degraded_reason ? `降级原因：${body.degraded_reason}` : "",
          `request_id：${body.request_id || "—"}`,
        ].filter(Boolean),
        "如要看实时路网，请配置服务端 AK 后以 live 模式重跑。",
      );
    } else if (state.jsapi.phase !== "live") {
      setBanner(
        "ok",
        "结果已渲染（底图为 SVG 舞台）",
        `取数层：${DATA_SOURCE_LABEL[body.data_source] || body.data_source || "—"}；数字结论与实时态同源，仅底图不可用。`,
        [`request_id：${body.request_id || "—"}`],
        jsapiWanted().use ? "满足条件时可切换实时底图。" : `SVG 原因：${jsapiWanted().reason}`,
      );
    } else {
      hideBanner();
    }
    if (state.jsapi.phase === "live" && state.center) {
      try {
        state.jsapi.map.centerAndZoom(new state.jsapi.bmap.Point(state.center[0], state.center[1]), 14);
      } catch (error) {
        /* 视野复位失败不影响结果渲染 */
      }
    }
    renderAll(true);
    el.runHint.textContent = `完成：${MODE_LABEL[request.mode]} ${request.minutes} min（${response.status}）`;
  } catch (error) {
    renderRequestFailure(null, { message: error && error.message }, "", request);
  } finally {
    state.running = false;
    stopRunTicker();
    el.run.disabled = false;
  }
}

/** 🔴 接口未就绪 / 网络失败都只出「说明 + 保留现场」，不出红色报错页（R13、§6.2）。 */
function renderRequestFailure(status, body, requestId, request) {
  const message =
    (body && body.error && body.error.message) ||
    body.message ||
    body.parse_error ||
    "接口没有返回可读结论。";
  const code = status === null ? "网络异常" : `HTTP ${status}`;
  // 404 与 405 都是「路由还没挂上」：`StaticFiles(html=True)` 挂在 `/` 上会把未注册的
  // POST 吃掉并回 405，所以两种码归为同一支可读说明，而不是误导成「地址写错了」。
  const notWired = status === 404 || status === 405 || status === 501;
  // FR-105 的「为什么不是实时数据」不能被失败态吃掉：把启动探测的结论并进来一起说。
  const healthBanner = state.healthBanner;
  const modeLine =
    healthBanner && healthBanner.show === true
      ? `运行模式：${DATA_MODE_LABEL[healthMode()] || healthMode()}｜${healthBanner.title}`
      : "";
  setBanner(
    "warning",
    notWired ? "/api/isochrone 尚未接入" : "本次分析未完成",
    notWired
      ? "服务层还没挂上这个路由（M2 并行开发中）。本页已按定稿契约实现渲染逻辑，接口一挂上就能直接出图；当前可以先用「图例 / 参数面板」确认交互，或稍后重试。"
      : `${code}：${message}`,
    [
      `请求：mode=${request.mode}，minutes=${request.minutes}，data_mode=${request.data_mode || "跟随探测"}`,
      `request_id：${requestId || "—"}`,
      body && body.error && body.error.code ? `code：${body.error.code}` : "",
      modeLine,
    ].filter(Boolean),
    "页面其它部分（选点、参数、图层）仍可用。",
  );
  el.runHint.textContent = `未取到结果（${code}）。`;
  renderProgress(null, false);
  renderUsage(null);
  renderResultMeta(null);
  renderVerticesTable(null);
  setStageHint();
}

function startRunTicker() {
  stopRunTicker();
  const started = Date.now();
  state.runTimer = window.setInterval(() => {
    const secs = ((Date.now() - started) / 1000).toFixed(1);
    el.runHint.textContent = `请求中 ${secs} s…（分阶段渲染，页面可交互）`;
  }, 200);
}

function stopRunTicker() {
  if (state.runTimer) {
    window.clearInterval(state.runTimer);
    state.runTimer = null;
  }
}

function polylineKey() {
  return `${(state.center || []).join(",")}|${state.activeMode}`;
}

function polylineLimit() {
  const mirror = state.serverPolylineCalls;
  const limit = mirror ? intOf(mirror.limit) : null;
  return limit && limit > 0 ? limit : POLYLINE_CALL_LIMIT;
}

/**
 * 把服务端回传的懒加载额度记成镜像：额度是后端在管的，前端自计只作兜底，两边不能各算一套。
 * 两个出口都是**同一个数**（服务层都读 `_polyline_calls_used` / `RouteProbe.usage()`）：
 * `/api/isochrone/polyline` 回 `calls.{used,limit,remaining}`，`/api/isochrone` 回
 * `usage.{polyline_calls,polyline_limit}`（[BUG][M2] 修好后新增）。计量表读 `usage`，
 * 守卫读这里的镜像，两者同源所以不会再出现「表里 12/12、状态行 3/12」。
 */
function syncPolylineCalls(body) {
  const usage = (body && body.usage) || {};
  const calls = body && typeof body.calls === "object" ? body.calls : null;
  const used = intOf(calls ? calls.used : usage.polyline_calls);
  const limit = intOf(calls ? calls.limit : usage.polyline_limit);
  if (used === null && limit === null) return; // 两个出口都没有：保留现有镜像，不猜
  const resolvedLimit = limit && limit > 0 ? limit : polylineLimit();
  const resolvedUsed = used === null ? state.polylineCalls : Math.min(used, resolvedLimit);
  const remaining = intOf(calls ? calls.remaining : resolvedLimit - resolvedUsed);
  const rejected = intOf(usage.polyline_rejected);
  state.serverPolylineCalls = {
    used: resolvedUsed,
    limit: resolvedLimit,
    remaining: remaining === null ? Math.max(0, resolvedLimit - resolvedUsed) : remaining,
    rejected,
    key: polylineKey(),
  };
  state.polylineCalls = resolvedUsed;
  // 🔴 计量表只读 `usage.polyline_*` 这一处：分析响应落地后没人再刷新它，点满 12 次后
  // 会出现「状态行 12/12、计量表 0/12」。把服务端最新的**会话**计数写回当前结果的展示模型，
  // 两个显示位就只剩一个来源（值仍是服务端那个数，前端不自计）。
  const shown = state.activeResult && state.activeResult.usage;
  if (shown) {
    shown.polyline_calls = resolvedUsed;
    shown.polyline_limit = resolvedLimit;
    if (rejected !== null) shown.polyline_rejected = rejected;
  }
}

/** 「第 13 次被拒」在契约里长这样：`ok:false / allowed:false / signals:["polyline_call_limit"]`。 */
function isPolylineLimitRefusal(body) {
  if (!body || typeof body !== "object") return false;
  if (list(body.signals).map(String).includes("polyline_call_limit")) return true;
  const calls = body.calls;
  return (
    body.allowed === false && calls && typeof calls === "object" && intOf(calls.remaining) === 0
  );
}

/**
 * FR-203 懒加载：`vertex` 决定拉哪个方向，`event` 只在**点顶点**（SVG / JSAPI）时传进来，
 * 用于把 tooltip 摆在点击处。明细表里的按钮不传 event，行为与以前一致（只写状态行）。
 */
async function loadPolyline(vertex, event) {
  /** FR-203：单次分析上限 12 次懒加载（与后端 `polyline_call_limit` 同口径），先拦再出网。 */
  const limit = polylineLimit();
  if (state.polylineCalls >= limit) {
    const refusal = `懒加载额度已用满（${limit} 次），只回显顶点坐标`;
    el.polylineStatus.textContent =
      `顶点 #${vertex.index + 1} 未取真实路径：本次已用满 ${limit} 次懒加载额度` +
      `（服务端 used=${state.polylineCalls}），按契约只回显顶点坐标` +
      "——顶点本身的实测耗时与路径距离仍在表里，不影响等时圈结论。";
    if (event) vertexTooltip(vertex, `路径串：${refusal}`, event); // 别说「正在补拉」骗人
    return;
  }
  const seq = ++state.polylineSeq; // 🔴 懒加载世代号：连点多个顶点时只认最新那一次
  state.polylineCalls += 1;
  el.polylineStatus.textContent = `正在为顶点 #${vertex.index + 1} 补拉真实路径（第 ${state.polylineCalls}/${limit} 次）…`;
  let body = null;
  let httpOk = false;
  let httpStatus = 0;
  try {
    const response = await fetch(POLYLINE_PATH, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({
        lnglat: `${num(state.center[0], 6)},${num(state.center[1], 6)}`,
        point: `${num(vertex.lnglat[0], 6)},${num(vertex.lnglat[1], 6)}`,
        mode: state.activeMode,
      }),
    });
    httpStatus = response.status;
    body = await readJson(response);
    httpOk = response.ok;
  } catch (error) {
    body = { message: `请求异常：${error && error.message ? error.message : String(error)}` };
  }
  // 🔴 过期响应丢弃：先发的请求可能后到，不丢弃就会画出「用户没点的那个顶点」的路径，
  // 并把 tooltip 指到错误的顶点上（与热力层同一类竞态，只是触发方式是连点顶点）。
  if (seq !== state.polylineSeq) {
    el.polylineStatus.textContent = `顶点 #${vertex.index + 1} 的路径响应已过期（后面还有更新的一次点击），本次未采用。`;
    return;
  }
  syncPolylineCalls(body);
  const atLimit = isPolylineLimitRefusal(body);
  const points = pathLnglat(body);
  if (!httpOk || !body || body.ok === false || points.length < 2) {
    // 🔴 懒加载失败绝不阻塞主流程：退回「只显示顶点」，原因写进状态行，等时圈/热力层照旧。
    const message = atLimit
      ? `本次分析的懒加载额度已用满（服务端 used=${state.polylineCalls}/${polylineLimit()}），按契约不再补调 direction`
      : (body && body.error && body.error.message) ||
        (body && body.message) ||
        (points.length < 2 ? "路径串为空，只回显顶点坐标" : `HTTP ${httpStatus}`);
    state.lastPolylineResult = null;
    if (!atLimit) state.polylineCalls = Math.max(0, state.polylineCalls - 1); // 真失败不占额度；被上限拦下则已经占着
    el.polylineStatus.textContent =
      `顶点 #${vertex.index + 1} 未取到真实路径：${message}` +
      (body && body.request_id ? `（request_id：${body.request_id}）` : "") +
      "。已退回只显示顶点，等时圈与热力层不受影响。";
    if (event) vertexTooltip(vertex, `路径串：未取到（${message}）`, event);
    rerenderAfterPath();
    return;
  }
  state.lastPolylineResult = body;
  const shownLimit = polylineLimit();
  const remaining = state.serverPolylineCalls ? state.serverPolylineCalls.remaining : null;
  el.polylineStatus.textContent =
    `顶点 #${vertex.index + 1} 路径已绘制（${points.length} 个折点，${pathHonestLabel(body)}，` +
    `耗时 ${num(body.duration_minutes ?? (Number(body.duration_s) || 0) / 60, 1)} min，` +
    `距离 ${fmtKm(body.distance_m)}，懒加载 ${state.polylineCalls}/${shownLimit} 次` +
    (remaining === null ? "" : `，剩余 ${remaining} 次`) +
    `）。` + (body.message ? ` ${body.message}` : "");
  // 🔴 tooltip 里 FR-203 那三样（方位角 / 实测耗时 / 路径距离）照旧在，只把最后一行换成路径结论。
  vertexTooltip(
    vertex,
    `路径串：${pathHonestLabel(body)}｜补调耗时 ${num(body.duration_minutes, 1)} min` +
      `｜路径距离 ${fmtKm(body.distance_m)}`,
    event,
  );
  rerenderAfterPath();
}

function rerenderAfterPath() {
  // 走完整渲染队列而不是只调 renderStage：否则热力层会因为缺少 drawHeat 那一步而消失
  // （浏览器实跑抓到的第二个问题：取完真实路径后热力层被静默关掉）。
  renderAll(false);
}

// ---------------------------------------------------------------------------------------------
// 中心点 / 事件 / 启动
// ---------------------------------------------------------------------------------------------

function setCenter(lnglat, note_, mapHop) {
  const value = pair(lnglat);
  if (!value) {
    el.centerReadout.textContent = "坐标无效：请按「经度,纬度」（BD09LL）填写，例如 116.4074,39.9042。";
    return;
  }
  state.runSeq += 1; // 中心点变了：在飞的那次分析结果已不属于当前视野，落地时按过期处理
  state.center = value;
  el.lng.value = value[0].toFixed(6);
  el.lat.value = value[1].toFixed(6);
  el.centerReadout.textContent = `中心点：${value[0].toFixed(5)}, ${value[1].toFixed(5)}（BD09LL）｜ ${
    note_ || ""
  }`;
  fetchCenterAddress(value); // 不 await：地址只是回显，不许拖住等时圈与地图平移
  if (state.jsapi.phase === "live" && state.jsapi.map) {
    try {
      const point = new state.jsapi.bmap.Point(value[0], value[1]);
      // FR-108 选中候选项时走 flyTo（PRD 原文「选中后地图 flyTo 并落点」）。本机没有浏览器 AK、
      // 实时底图跑不了，所以留一条 panTo 退路而不是把 flyTo 当硬依赖。
      if (mapHop === "fly" && typeof state.jsapi.map.flyTo === "function") state.jsapi.map.flyTo(point);
      else state.jsapi.map.panTo(point);
    } catch (error) {
      /* 平移失败时保持当前视野 */
    }
  }
  setPickMode(false);
  renderAll(true);
}

function setPickMode(on) {
  state.pickMode = on;
  el.pickPoint.setAttribute("aria-pressed", String(on));
  el.pickPoint.classList.toggle("on", on);
  el.pickPoint.textContent = on ? "选点中：点图上任意位置" : "在图上选点";
}

function applyLnglatFromInputs() {
  const lnglat = parseLngLat(el.lng.value, el.lat.value);
  if (!lnglat) {
    el.centerReadout.textContent = "坐标无法解析：请填数字，格式「经度 纬度」，或直接粘贴「116.4074,39.9042」。";
    return;
  }
  setCenter(lnglat, "按经纬度输入（FR-101 方式③）");
}

function parseLngLat(rawLng, rawLat) {
  const single = String(rawLng || "").trim();
  if (single.includes(",")) {
    const parts = single.split(",");
    return pair([Number(parts[0]), Number(parts[1])]);
  }
  return pair([Number(rawLng), Number(rawLat)]);
}

function toggleEnginePhase() {
  /** 参数变化后重新判定是否满足注入条件；不满足就立刻退回 SVG（不做「半张实时图」）。 */
  const want = jsapiWanted();
  if (!want.use) {
    if (state.jsapi.phase === "live") {
      degradeToSvg(`数据模式已切到 ${state.effectiveDataMode}，按 R13 不再使用公网底图`);
    } else {
      setEngineBadge("svg", want.reason);
    }
    return;
  }
  if (state.jsapi.phase === "off" || state.jsapi.phase === "failed" || state.jsapi.phase === "svg") {
    tryEnableLiveMap();
  }
}

function onRunClick() {
  const request = pendingRun();
  request.center = state.center;
  state.effectiveDataMode = request.data_mode || healthMode();
  renderModeBadge();
  state.pendingSecondRound = request;
  if (state.results[otherMode(request.mode)]) {
    // FR-206 ①：第二轮之前必须先显式提示配额翻倍，确认后才发请求。
    renderQuotaNotice();
    el.quotaConfirm.focus();
    return;
  }
  proceedRun(request);
}

function proceedRun(request) {
  state.pendingSecondRound = null;
  renderQuotaNotice();
  toggleEnginePhase();
  runIsochrone(request);
}

el.run.addEventListener("click", onRunClick);
el.quotaConfirm.addEventListener("click", () => {
  const request = state.pendingSecondRound || pendingRun();
  request.center = state.center;
  proceedRun(request);
});
el.quotaCancel.addEventListener("click", () => {
  state.pendingSecondRound = null;
  renderQuotaNotice();
  el.runHint.textContent = "已取消第二轮（未消耗任何配额）。";
});

el.applyLnglat.addEventListener("click", applyLnglatFromInputs);
el.lng.addEventListener("keydown", (event) => {
  if (event.key === "Enter") applyLnglatFromInputs();
});
el.lat.addEventListener("keydown", (event) => {
  if (event.key === "Enter") applyLnglatFromInputs();
});
el.resetCenter.addEventListener("click", () => setCenter(DEFAULT_CENTER.slice(), "已回到默认中心点（北京）"));
el.geocode.addEventListener("click", runGeocode);
el.addr.addEventListener("input", onAddrInput);
el.suggestOn.addEventListener("change", onSuggestToggle);
// 点候选行时先别让输入框失焦：blur 会把列表清掉，click 就落在空列表上了。
el.suggestList.addEventListener("mousedown", (event) => event.preventDefault());
el.suggestList.addEventListener("click", (event) => {
  const row = event.target && event.target.closest ? event.target.closest(".suggest-item") : null;
  if (row) chooseSuggestion(Number(row.dataset.index));
});
el.addr.addEventListener("blur", closeSuggest);
el.addr.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") {
    if (moveSuggestActive(1)) event.preventDefault();
    return;
  }
  if (event.key === "ArrowUp") {
    if (moveSuggestActive(-1)) event.preventDefault();
    return;
  }
  if (event.key === "Escape") {
    closeSuggest();
    return;
  }
  if (event.key === "Enter") {
    // 有选中行时联想优先（FR-108），没有才落回手动地理编码（FR-101 方式②）。
    if (state.suggest.active >= 0) {
      event.preventDefault();
      chooseSuggestion(state.suggest.active);
      return;
    }
    runGeocode();
  }
});
el.pickPoint.addEventListener("click", () => setPickMode(!state.pickMode));
el.recenter.addEventListener("click", () => renderAll(true));

el.saveAk.addEventListener("click", () => {
  const value = el.browserAk.value.trim();
  state.jsapi.ak = value;
  try {
    if (value) window.localStorage.setItem(AK_STORAGE_KEY, value);
    else window.localStorage.removeItem(AK_STORAGE_KEY);
  } catch (error) {
    /* 存不下去就只在内存里用，不弹红色报错 */
  }
  renderAkState();
  el.runHint.textContent = value ? "已保存：下次运行将尝试实时底图。" : "已清空本地 AK。";
});
el.clearAk.addEventListener("click", () => {
  el.browserAk.value = "";
  state.jsapi.ak = "";
  try {
    window.localStorage.removeItem(AK_STORAGE_KEY);
  } catch (error) {
    /* 无需提示 */
  }
  if (state.jsapi.phase === "live") degradeToSvg("本地 AK 已清除");
  else setEngineBadge("svg", jsapiWanted().reason);
  renderAkState();
});

el.mode.addEventListener("change", () => {
  state.runSeq += 1; // 🔴 过期请求标记：这次切换之后，仍在飞的响应只存进对比数据、不再抢当前视图
  state.polylineSeq += 1; // 已切换模式，顶点懒加载的路径串属于旧模式，过期丢弃
  state.activeMode = el.mode.value;
  state.activeResult = state.results[el.mode.value] || null;
  state.lastPolylineResult = null;
  // 懒加载额度按「中心点｜模式」计，切换模式后旧模式的已用数不适用，按镜像重新起算。
  const mirror = state.serverPolylineCalls;
  state.polylineCalls = mirror && mirror.key === polylineKey() ? intOf(mirror.used) ?? 0 : 0;
  const hasOther = Boolean(state.results[otherMode(el.mode.value)]);
  el.runHint.textContent = hasOther
    ? "已存在另一模式结果：再跑一次属于第二轮，运行前会显式提示配额翻倍（FR-206）。"
    : `已选择${MODE_LABEL[el.mode.value]}。`;
  if (hasOther) {
    el.compare.checked = true;
    state.pendingSecondRound = pendingRun();
    renderQuotaNotice(); // 先提示，等用户点「开始分析」或直接确认
  }
  renderAll(true);
});
el.minutes.addEventListener("change", () => {
  state.minutes = Number(el.minutes.value);
});
el.dataMode.addEventListener("change", () => {
  state.effectiveDataMode = el.dataMode.value || healthMode();
  renderModeBadge();
  renderAkState();
  toggleEnginePhase();
});

for (const box of [el.heat, el.compare, el.reference, el.showPois, el.showBlind]) {
  if (box) box.addEventListener("change", () => renderAll(false));
}

// FR-104 类别勾选：只改提示与请求体，**不重画地图**（图层渲染与类别子集无关，重画纯属浪费）。
for (const box of catBoxes) box.addEventListener("change", renderCategoryHint);
if (el.catAll) {
  el.catAll.addEventListener("click", () => {
    catBoxes.forEach((box) => {
      box.checked = true;
    });
    renderCategoryHint();
  });
}
renderCategoryHint();

// ---------------------------------------------------------------------------------------------
// FR-106 移动端底部抽屉
// ---------------------------------------------------------------------------------------------

/**
 * 断点只在这里写一次：CSS 侧靠 `body[data-sheet-mode="mobile"]` 跟随，不在样式表里重复 768。
 * `report.js` 里 FR-107 的 `platform` 判定用的是同一个数，两边由 tests/test_web_smoke.py 对账。
 */
const SHEET_BREAKPOINT_PX = 768;
const sheetQuery = window.matchMedia ? window.matchMedia(`(max-width: ${SHEET_BREAKPOINT_PX}px)`) : null;
const sheetTabs = Array.from(document.querySelectorAll("#sheet-bar .sheet-tab"));
const sheetGrip = document.getElementById("sheet-grip");
/** 面板 id 与把手条上的 `data-sheet` / `aria-controls` 同名，靠这个键建立对应关系。 */
const sheetPanels = sheetTabs
  .map((tab) => ({ key: tab.dataset.sheet, tab, panel: document.getElementById(tab.dataset.sheet) }))
  .filter((sheet) => !!sheet.panel);

let sheetMode = "desktop";
let sheetOpenKey = "";
/** 收起后再拖上来时开哪一块：记住最后打开过的面板，而不是每次都跳回「参数」。 */
let sheetLastKey = "controls";
let sheetDrag = null;

function isSheetMobile() {
  return sheetMode === "mobile";
}

/**
 * 地图可用高度 = 视口 − stage-area 的上边界 − 把手条。
 *
 * 🔴 刻意**不扣**页脚与图例：FR-106 要的是「地图占满首屏」，而它们本来就在首屏之下、
 * 靠滚动才看到。把这两块也算进扣减里等于自己把地图压扁——375×667 实测扣完只剩 240 px，
 * 抽屉做得再对也白搭。
 * 量 stage-area 自己的上边界，比按 class 逐个累加可靠：margin、gap 和 `[hidden]`
 * 都已经体现在几何里，不需要在 JS 里把「这块现在到底可不可见」再判一遍。
 */
function sizeSheetStage() {
  if (!isSheetMobile() || !el.stageArea) return;
  const top = el.stageArea.getBoundingClientRect().top + window.scrollY;
  const bar = parseFloat(getComputedStyle(document.body).getPropertyValue("--sheet-bar-h")) || 52;
  const height = Math.max(240, Math.round(window.innerHeight - top - bar));
  el.stageArea.style.setProperty("--map-h", `${height}px`);
}

function renderSheets() {
  document.body.dataset.sheetMode = sheetMode;
  document.body.dataset.sheetOpen = isSheetMobile() ? sheetOpenKey : "";
  for (const sheet of sheetPanels) {
    sheet.tab.setAttribute("aria-expanded", isSheetMobile() && sheet.key === sheetOpenKey ? "true" : "false");
  }
  sizeSheetStage();
}

function openSheet(key) {
  sheetOpenKey = key || "";
  if (sheetOpenKey) sheetLastKey = sheetOpenKey;
  renderSheets();
}

/** `offset` = 面板顶边距「完全展开」还差多少像素：0 = 展开，`travel` = 正好滑出屏幕下沿。 */
function moveSheet(panel, offset) {
  panel.style.transform = `translateY(${Math.round(offset)}px)`;
}

function sheetPointerDown(event) {
  if (!isSheetMobile() || sheetDrag) return;
  const key = sheetOpenKey || sheetLastKey;
  const sheet = sheetPanels.find((item) => item.key === key);
  if (!sheet) return;
  const startExpanded = sheetOpenKey === sheet.key;
  // 收起态在屏幕外，没有可拖的东西：先让它可见（位移仍等于 travel），再跟手。
  if (!startExpanded) openSheet(sheet.key);
  const travel = Math.max(1, sheet.panel.offsetHeight);
  sheetDrag = {
    id: event.pointerId,
    startY: event.clientY,
    panel: sheet.panel,
    key: sheet.key,
    travel,
    startExpanded,
    moved: 0,
    offset: startExpanded ? 0 : travel,
  };
  sheet.panel.classList.add("sheet-dragging");
  moveSheet(sheet.panel, sheetDrag.offset);
}

function sheetPointerMove(event) {
  if (!sheetDrag || event.pointerId !== sheetDrag.id) return;
  const dy = event.clientY - sheetDrag.startY;
  sheetDrag.moved = Math.max(sheetDrag.moved, Math.abs(dy));
  // 展开态：向下拖才动（`dy`）；收起态：向上拖才动（`travel + dy`）。
  const raw = sheetDrag.startExpanded ? dy : sheetDrag.travel + dy;
  sheetDrag.offset = Math.min(sheetDrag.travel, Math.max(0, raw));
  moveSheet(sheetDrag.panel, sheetDrag.offset);
}

function sheetPointerUp(event) {
  if (!sheetDrag || (event && event.pointerId !== sheetDrag.id)) return;
  const { panel, key, travel, moved, offset, startExpanded } = sheetDrag;
  panel.classList.remove("sheet-dragging");
  panel.style.transform = "";
  sheetDrag = null;
  // 位移不到 6 px 就是「按了一下把手」而不是拖：抽屉按手势语义翻状态即可。
  // 注意这里不会和 click 打架——`pointerdown` 只绑在把手 `<span>` 上，三个 tab 的
  // `click` 各自独立，把手上没有第二个监听器。
  if (moved < 6) {
    openSheet(startExpanded ? "" : key);
    return;
  }
  openSheet(offset < travel / 2 ? key : "");
}

if (sheetGrip) {
  sheetGrip.addEventListener("pointerdown", sheetPointerDown);
  // 🔴 `pointermove` / `pointerup` 挂在 **window** 而不是挂把手上：D-1 那次的教训
  //    （见上方 `el.stage` 的注释）是「指针事件会跟着捕获者走」，把手只有 ~104 px 宽，
  //    手指一滑出它就断流——抽屉拖到一半停住比不拖更糟。窗口层监听 + `moved < 6` 的
  //    点击阈值，既跟得住手也不会把一次拖拽误记成两次开合。
  window.addEventListener("pointermove", sheetPointerMove);
  window.addEventListener("pointerup", sheetPointerUp);
  window.addEventListener("pointercancel", sheetPointerUp);
}

for (const sheet of sheetPanels) {
  sheet.tab.addEventListener("click", () => openSheet(sheetOpenKey === sheet.key ? "" : sheet.key));
}

function syncSheetMode() {
  const next = sheetQuery && sheetQuery.matches ? "mobile" : "desktop";
  if (next !== sheetMode) {
    sheetMode = next;
    if (sheetMode !== "mobile") {
      // 回桌面：内联位移与开合状态全部交还给三栏布局，别把一个 `translateY` 留在面板上。
      sheetOpenKey = "";
      for (const sheet of sheetPanels) sheet.panel.style.transform = "";
      // `--map-h` 只有移动端规则会读，但留着会让「转到桌面又转回移动端」时先套一次旧值。
      if (el.stageArea) el.stageArea.style.removeProperty("--map-h");
    }
  }
  renderSheets();
}

if (sheetQuery) {
  if (sheetQuery.addEventListener) sheetQuery.addEventListener("change", syncSheetMode);
  else if (sheetQuery.addListener) sheetQuery.addListener(syncSheetMode); // 老 Safari 只认这条
}
window.addEventListener("resize", syncSheetMode);
syncSheetMode();

// SVG 舞台交互：pointer 事件（R15：不写 touch* 分支）
let drag = null;
el.stage.addEventListener("pointerdown", (event) => {
  if (state.pickMode) return;
  // 🔴 D-1（P2）「点顶点没反应」的真实根因：`setPointerCapture` 会把这次指针的后续事件
  // （含派生出来的 mousedown/mouseup 与 click）一律改派给捕获者本身——也就是舞台，
  // 于是顶点 <circle> 上注册的 click 永远收不到，只有明细表里的按钮能触发懒加载。
  // 顶点只有 ~12px，从它正上方起手平移的机会成本可以忽略，所以这里让路：
  // 落在顶点上的按下不进拖拽、不捕获指针，click 正常落到该圆点，仍走 `onVertexPick`
  // 同一条懒加载逻辑（12 次上限与 `polylineSeq` 世代号都在 `loadPolyline` 里，不绕过）。
  if (event.target && typeof event.target.closest === "function" && event.target.closest(".vertex")) {
    return;
  }
  drag = { x: event.clientX, y: event.clientY, view: Object.assign({}, state.view) };
  el.stage.setPointerCapture(event.pointerId);
  hideTooltip();
});
el.stage.addEventListener("pointermove", (event) => {
  if (!drag) return;
  state.view.x = drag.view.x - (event.clientX - drag.x) / drag.view.scale;
  state.view.y = drag.view.y - (event.clientY - drag.y) / drag.view.scale;
  applyView();
  drawGrid();
});
el.stage.addEventListener("pointerup", (event) => {
  if (drag) {
    drag = null;
    try {
      el.stage.releasePointerCapture(event.pointerId);
    } catch (error) {
      /* 指针已释放 */
    }
    return;
  }
  if (!state.pickMode) return;
  const rect = el.stage.getBoundingClientRect();
  const scenePoint = screenToScene(event.clientX - rect.left, event.clientY - rect.top);
  setCenter(fromScene(scenePoint[0], scenePoint[1]), "在 SVG 舞台上点选（离线示意图）");
});
el.stage.addEventListener("pointercancel", () => {
  drag = null;
});
// 滚轮缩放必须显式 passive:false，否则 preventDefault 在 Chrome 里被忽略、页面跟着一起滚。
el.stage.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.2 : 1 / 1.2;
    state.view.scale = Math.max(0.002, Math.min(2, state.view.scale * factor));
    applyView();
    drawGrid();
  },
  { passive: false },
);
el.stage.addEventListener("dblclick", () => renderAll(true));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  hideTooltip();
  // FR-106：Escape 收起抽屉。tooltip 与抽屉共用同一个键，先后顺序不重要——
  // 两者都在「当前最靠近指针/焦点的那一层」，同时收掉符合直觉。
  if (isSheetMobile() && sheetOpenKey) openSheet("");
});

window.addEventListener("resize", () => {
  if (state.jsapi.phase === "live") return;
  resizeStage();
  // 走完整队列而不是只调 renderStage：ensureScene 会重建热力 group，
  // 只补舞台不补 drawHeat 的话，窗口一缩放热力层就整幅消失（保留用户的缩放比，所以 resetView=false）。
  renderAll(false);
});

function readQueryFlags() {
  const params = new URLSearchParams(window.location.search);
  const ak = params.get("ak");
  if (ak) {
    state.jsapi.ak = ak;
    el.browserAk.value = ak;
  }
  const rawLng = params.get("lng");
  const rawLat = params.get("lat");
  if (rawLng !== null && rawLat !== null) {
    const lng = Number(rawLng);
    const lat = Number(rawLat);
    // ⚠️ 必须显式判 null：`Number(null)` 是 0，缺参数时会被误读成「中心点 = 几内亚湾」，
    // 整个等时圈就画到视野外去了（这是浏览器实跑第一个被抓出来的 bug）。
    if (Number.isFinite(lng) && Number.isFinite(lat) && Math.abs(lng) <= 180 && Math.abs(lat) <= 85) {
      state.center = [lng, lat];
    }
  }
  const mode = params.get("mode");
  if (mode === "walking" || mode === "riding") el.mode.value = mode;
  const minutes = Number(params.get("minutes"));
  if ([5, 10, 15, 30].includes(minutes)) el.minutes.value = String(minutes);
  const dataMode = params.get("data_mode");
  if (["live", "cache", "mock"].includes(dataMode)) el.dataMode.value = dataMode;
}

// ---------------------------------------------------------------------------------------------
// M4 报告面板的接入点（`web/report.js` 用；本页其余逻辑不依赖它）
// ---------------------------------------------------------------------------------------------
/**
 * 把一份**从别处拿到的**等时圈结果接进本页的渲染管线（M4 的 `/api/analysis` 走这里）。
 *
 * 为什么不复用 `runIsochrone()`：那个函数自己发 `/api/isochrone` 请求。报告面板要的是
 * 「一次请求同时拿到等时圈 + POI + 盲区 + 评分」，重复发一次等时圈请求会白烧一份配额
 * （§4.6 的预算守卫会把它算进去）。所以这里只保留「拿到结果之后」的那一段，
 * 与 `runIsochrone` 的成功分支逐字段一致：
 *   · 写 `state.results[mode]`（FR-206 的双模式对比仍可用）
 *   · 非当前模式时**不抢视图**（与在飞竞态那条纪律同源）
 *   · 同步 FR-203 的 12 次懒加载额度镜像
 */
function adoptIsochrone(payload, options) {
  const settings = options || {};
  if (!payload || typeof payload !== "object") return false;
  const mode = payload.mode || state.activeMode;
  state.results[mode] = payload;
  if (mode !== state.activeMode && !settings.force) {
    // 报告面板跑的是「当前选中的模式」，正常不会走到这里；真走到了就只当对比数据存着，
    // 免得出现「用骑行的配色画步行的数据」。
    state.results[mode] = payload;
    return false;
  }
  state.activeMode = mode;
  state.activeResult = payload;
  state.lastPolylineResult = null;
  syncPolylineCalls(payload);
  state.effectiveDataMode = payload.data_mode || state.effectiveDataMode;
  const center = pair(payload.center);
  if (center) state.center = center;
  renderAll(true);
  return true;
}

/**
 * FR-212 / FR-222：接收 `/api/analysis` 带来的两个叠加图层（POI 点 + 盲区块）。
 *
 * 为什么单独一个方法而不是塞进 `adoptIsochrone`：这两层是 **M4 报告的产物**，
 * 单独跑一次 `/api/isochrone` 不会产生它们。分开之后语义很干净 ——
 * 「接等时圈」与「接报告图层」互不牵连，跑 M2 那条路径时调用 `setOverlays(null)` 清干净即可。
 */
function setOverlays(overlays) {
  const next = overlays || {};
  state.overlays = {
    pois: Array.isArray(next.pois) ? next.pois : [],
    blindSpots: next.blindSpots || null,
  };
  if (state.jsapi.phase !== "live") fitViewForOverlays();
  renderAll(true);
  return {
    pois: state.overlays.pois.length,
    blindBlocks: blindBlocks().length,
  };
}

/** 视野要把圈内 POI 一并框住，否则「画了但看不见」等于没画（圈外的点不参与，避免把视野拉散）。 */
function fitViewForOverlays() {
  const points = collectScenePoints();
  if (!points.length) return;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    minX = Math.min(minX, p[0]);
    maxX = Math.max(maxX, p[0]);
    minY = Math.min(minY, p[1]);
    maxY = Math.max(maxY, p[1]);
  }
  const padX = Math.max(140, (maxX - minX) * 0.12);
  const padY = Math.max(140, (maxY - minY) * 0.12);
  const spanX = maxX - minX + padX * 2;
  const spanY = maxY - minY + padY * 2;
  state.view = {
    x: (minX + maxX) / 2,
    y: (minY + maxY) / 2,
    scale: Math.max(0.005, Math.min(state.size.w / spanX, state.size.h / spanY, 0.5)),
  };
}

/**
 * 给 `report.js` 的接口面。刻意只暴露方法、不暴露内部结构：报告面板不该知道舞台、投影、
 * 热力层这些实现细节，否则两边会一起长成没法改的样子。
 *
 * ⚠️ 这里**不写死方法个数**：接口在增长（FR-212/FR-222 加了 `setOverlays`、FR-223 加了 `focusOn`），
 * 写死数字的注释一定会过期，而「注释说 6 个、实际 12 个」这种不一致比不写更糟 ——
 * 它会让人以为少挂了一个方法。要数就数 `Object.keys(window.CircleDocMap).length`。
 */
window.CircleDocMap = {
  adoptIsochrone,
  setOverlays,
  getCenter: () => (state.center || DEFAULT_CENTER).slice(),
  setCenter: (lnglat, note) => setCenter(lnglat, note),
  getActiveMode: () => state.activeMode,
  readControls: () => ({
    mode: el.mode.value,
    minutes: Number(el.minutes.value),
    data_mode: el.dataMode.value || null,
    // FR-104：全七类勾选 → null（不发 `categories`，与多选落地前的请求体/缓存键一致）。
    categories: selectedCategories(),
  }),
  setBusy: (busy) => {
    state.running = !!busy;
    el.run.disabled = !!busy;
  },
  setRunHint: (text) => {
    el.runHint.textContent = text;
  },
  /**
   * 页脚与「结果元信息」的 `request_id` 兜底来源。🔴 必须有这个方法：`/api/analysis` 的信封号
   * 在响应**顶层**，嵌给地图渲染的 `isochrone` 子对象里没有它（`as_dict()` 按 §4.7 出字段，
   * 号由路由层补），而 `renderResultMeta()` 读的是「当前这份结果」的号。两者一错开，
   * 跑「一键体检」这条主路径时页脚就永远停在 `request_id：—`——号子明明存在却看不见，
   * 出问题时无法对着服务端日志报号。
   *
   * 存进 `state` 而不是直接写文本：`renderAll()` 在一次体检里要被 `adoptIsochrone` 与
   * `setOverlays` 各调一次，谁最后跑谁定页脚，靠调用顺序赢不了。
   * 2026-10-11 实测：同一页 `POST /api/isochrone` 能把号写进页脚，`POST /api/analysis` 不能。
   */
  setRequestId: (value) => {
    state.requestId = String(value || "");
    el.requestIdFoot.textContent = `request_id：${currentRequestId(state.activeResult) || "—"}`;
  },
  /** 供 `report.js` 定位到某个盲区块（FR-223 的「可点击定位到地图」）。 */
  focusOn: (lnglat) => {
    const point = pair(lnglat);
    if (!point) return false;
    if (state.jsapi.phase === "live" && state.jsapi.map && state.jsapi.bmap) {
      state.jsapi.map.panTo(new state.jsapi.bmap.Point(point[0], point[1]));
      return true;
    }
    state.view = { ...state.view, x: toScene(point)[0], y: toScene(point)[1] };
    applyView();
    return true;
  },
  pushProgress: (item) => {
    // 复用 M2 的阶段表（`#progress-stages`）：体检是「等时圈 + POI + 盲区 + 评分」四段，
    // 阶段名由 `services/analysis.py` 的真实回调给出，不是前端编的进度条。
    if (el.progressText) el.progressText.textContent = `体检进行中：${item.stage || "—"}`;
    if (!el.progressStages || !item || !item.stage) return;
    const line = node("li", null, `${item.stage}${item.mode ? `（${item.mode}）` : ""}`);
    el.progressStages.appendChild(line);
  },
  clearProgress: () => {
    if (el.progressStages) clear(el.progressStages);
    if (el.progressText) el.progressText.textContent = "等待运行。";
    if (el.progressFill) el.progressFill.style.width = "0%";
  },
  isLiveMap: () => state.jsapi.phase === "live",
  /**
   * FR-106：移动端抽屉的开合。`report.js` 在体检成功后调 `openSheet("report")`，
   * 让报告浮到地图上方——手机上「跑完了但内容在屏幕外」是最容易被当成失败的形态。
   * 桌面是空操作（三栏本来就在屏幕上），所以调用方不需要先判断视口。
   */
  openSheet: (key) => {
    if (isSheetMobile()) openSheet(key);
  },
  isSheetMobile: () => isSheetMobile(),
  constants: { MODE_LABEL, DATA_SOURCE_LABEL, DATA_MODE_LABEL },
};

async function boot() {
  readQueryFlags();
  if (!state.jsapi.ak) {
    const stored = readStoredAk();
    state.jsapi.ak = stored;
    if (stored) el.browserAk.value = stored;
  }
  setCenter(state.center, "初始中心点");
  state.activeMode = el.mode.value;
  state.minutes = Number(el.minutes.value);
  resizeStage();
  renderLegend();
  renderUsage(null);
  renderResultMeta(null);
  renderVerticesTable(null);
  setEngineBadge("svg", jsapiWanted().reason);
  await loadHealth();
  // FR-106：`loadHealth()` 可能刚把降级横幅亮起来，地图可用高度跟着变，重算一次。
  syncSheetMode();
  if (jsapiWanted().use) {
    const ok = await tryEnableLiveMap();
    if (!ok) renderAll(true);
  } else {
    renderAll(true);
  }
}

boot();
