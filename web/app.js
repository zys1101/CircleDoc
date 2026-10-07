/* CircleDoc 首页脚本：拉 /api/health 并把「当前数据模式 + 为什么不是实时」说清楚。
 *
 * 三条纪律（和后端一致）：
 * 1. 🔴 拿不到接口也不报错页：整个页面唯一的失败态是「离线报告模式」横幅（§6.2 断言：
 *    零配置首页不出现红色报错）。所有 catch 都落到渲染分支，不往外抛。
 * 2. 只读 `/api/health` 已经算好的文案（`banner.*`、`services[].message`），
 *    前端不再判一次百度状态码——203 与 210 必须是两条不同的话，而这件事只有 errors.py 说了算。
 * 3. 一律 `textContent`，不用 `innerHTML` 拼数据：探测结论里会回显官方错误名。
 */

const HEALTH_PATH = "/api/health";

const MODE_LABEL = {
  live: "实时数据",
  cache: "缓存优先",
  mock: "离线合成数据",
};

/** 探测方式的人话映射：`probe.mode` 只有三种取值，写全就不会把「沿用记忆」演成「刚探过」。 */
const PROBE_MODE_LABEL = {
  real_network: "真实网络",
  mock: "本地剧本（零真实出网）",
  off: "沿用上次结论（本次未出网）",
};

const ELEMENTS = {
  appName: document.getElementById("app-name"),
  badge: document.getElementById("mode-badge"),
  modeText: document.getElementById("mode-text"),
  banner: document.getElementById("banner"),
  bannerTitle: document.getElementById("banner-title"),
  bannerMessage: document.getElementById("banner-message"),
  bannerDetails: document.getElementById("banner-details"),
  bannerCta: document.getElementById("banner-cta"),
  bannerRetry: document.getElementById("banner-retry"),
  meta: document.getElementById("probe-meta"),
  services: document.getElementById("services"),
  reprobe: document.getElementById("reprobe"),
  checkedAt: document.getElementById("checked-at"),
};

/** 横幅是唯一「非实时」的告知位；`show=false`（live）时整块隐藏而不是留个空框。 */
function renderBanner(banner) {
  const node = ELEMENTS.banner;
  if (!banner || banner.show !== true) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  node.dataset.level = banner.level || "warning";
  ELEMENTS.bannerTitle.textContent = banner.title || "当前不是实时数据";
  ELEMENTS.bannerMessage.textContent = banner.message || "";

  ELEMENTS.bannerDetails.replaceChildren();
  for (const line of banner.detail_messages || []) {
    const li = document.createElement("li");
    li.textContent = line;
    ELEMENTS.bannerDetails.append(li);
  }
  // 地图占位说明由 M4 的地图组件取用；首页先把它显示出来，避免「示意图」和「真图」混淆。
  ELEMENTS.bannerCta.textContent = [banner.cta_hint, banner.map_placeholder_text]
    .filter(Boolean)
    .join("；");
  const wait = Number(banner.retry_after_s);
  ELEMENTS.bannerRetry.textContent =
    Number.isFinite(wait) && wait > 0
      ? `今日额度已用尽的接口约 ${Math.ceil(wait / 60)} 分钟后可恢复（次日零时重置）。`
      : "";
}

function renderMode(data) {
  const mode = data.data_mode || "unknown";
  ELEMENTS.badge.dataset.mode = mode;
  const label = MODE_LABEL[mode] || mode;
  // 人话 + 机器值一起写：QA 拿页面对 `data_mode` 时不必再开 DevTools 看 JSON，
  // 也不会把 `mock`（模式）和 `sample`（实际取数层）当成同一个词。
  const token = MODE_LABEL[mode] ? `（${mode}）` : "";
  const source = data.data_source ? ` ｜ 取数：${data.data_source}` : "";
  ELEMENTS.modeText.textContent = `数据模式：${label}${token}${source}`;
  if (ELEMENTS.appName && data.config && data.config.app_name) {
    ELEMENTS.appName.textContent = `${data.config.app_name} · 零配置即可运行`;
  }
}

function renderMeta(data) {
  const probe = data.probe || {};
  const usage = probe.usage || {};
  const parts = [
    `探测方式：${PROBE_MODE_LABEL[probe.mode] || probe.mode || "未知"}`,
    `本次出网 ${probe.requests_sent ?? usage.http_requests ?? 0} 次`,
    `重试：不重试`,
  ];
  if (probe.performed === false) {
    parts.push("结论取自进程记忆（未重新出网）");
  }
  if (probe.scenario) {
    parts.push(`剧本：${probe.scenario}`);
  }
  ELEMENTS.meta.textContent = parts.join(" ｜ ");
  ELEMENTS.checkedAt.textContent = data.checked_at ? `检测时间 ${data.checked_at}` : "";
}

/** 服务表：一行一个探测项。状态名与文案都来自后端，前端只做排版。 */
function renderServices(data) {
  const rows = Array.isArray(data.services) ? data.services : [];
  ELEMENTS.services.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = "没有探测项。";
    tr.append(td);
    ELEMENTS.services.append(tr);
    return;
  }
  for (const item of rows) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.textContent = item.label || item.key || "";

    const state = document.createElement("td");
    state.className = "state";
    state.dataset.available = item.available ? "true" : "false";
    state.textContent = item.state_name || item.state || "";

    const code = document.createElement("td");
    code.textContent =
      item.baidu_status === null || item.baidu_status === undefined
        ? "—"
        : `${item.baidu_status}${item.official_name ? ` ${item.official_name}` : ""}`;

    const detail = document.createElement("td");
    const message = document.createElement("div");
    // `message` 已经是 errors.py 的 user_message：这就是「六种错误六条不同文案」的落点。
    message.textContent = item.message || "";
    detail.append(message);
    const extra = [item.operator_hint, item.flaky ? "多次尝试结果不一致，已按最差情况判定" : ""]
      .filter(Boolean)
      .join("；");
    if (extra) {
      const hint = document.createElement("div");
      hint.className = "cta";
      hint.textContent = extra;
      detail.append(hint);
    }

    tr.append(name, state, code, detail);
    ELEMENTS.services.append(tr);
  }
}

/**
 * 失败态渲染：连 `/api/health` 都没打到时的兜底。
 * 为什么不显示「加载失败」：评审现场最常见的意外是后端还没起来，此时页面要仍然解释得通
 * 「现在拿到的是什么数据」，一句红色报错既不符合 §6.2，也不告诉人下一步做什么。
 */
function renderOffline(errorName) {
  ELEMENTS.badge.dataset.mode = "mock";
  ELEMENTS.modeText.textContent = "数据模式：离线合成数据（未取到探测结论）";
  renderBanner({
    show: true,
    level: "warning",
    title: "实时服务不可用，当前为离线报告模式",
    message: "未能读取 /api/health，页面按离线模式展示；接口恢复后刷新即可。",
    detail_messages: errorName ? [`请求异常：${errorName}`] : [],
    cta_hint: "确认服务已启动（run.sh / docker compose up）后重新探测",
  });
  ELEMENTS.meta.textContent = "探测结论读取失败，已按离线模式展示。";
  renderServices({ services: [] });
}

async function loadHealth({ force = false } = {}) {
  ELEMENTS.reprobe.disabled = true;
  try {
    const url = force ? `${HEALTH_PATH}?force=true` : HEALTH_PATH;
    const response = await fetch(url, { headers: { accept: "application/json" } });
    // 🔴 只有 2xx 才当结论用：/api/health 正常永远 200，非 200 说明是网关或装配问题。
    if (!response.ok) throw new Error(`http_${response.status}`);
    const data = await response.json();
    renderMode(data);
    renderBanner(data.banner);
    renderMeta(data);
    renderServices(data);
  } catch (error) {
    renderOffline(error && error.name ? `${error.name}: ${error.message}` : String(error));
  } finally {
    ELEMENTS.reprobe.disabled = false;
  }
}

ELEMENTS.reprobe.addEventListener("click", () => loadHealth({ force: true }));
loadHealth();
