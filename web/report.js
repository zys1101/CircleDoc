/**
 * web/report.js —— M4 体检报告面板（FR-301 ~ FR-306、FR-213、FR-223）。
 *
 * 与 `isochrone.js` 的分工：那一页负责「地图怎么画」，本文件负责「报告怎么读」。
 * 两边只通过 `window.CircleDocMap` 暴露的方法打交道（见 `isochrone.js` 末尾的接口注释），
 * 所以本文件完全不碰舞台、投影、热力层，也不会因为地图实现变化而重写。
 * ⚠️ 那里**不写死方法个数** —— 接口在增长，写死的数字必然过期。
 *
 * 🔴 三条与「省钱」有关的口径，全部照 PRD §5 的前端资源约束来：
 *
 * 1. **零外部依赖、零构建链**。雷达图与柱状图是**手绘 SVG**，不是 ECharts。
 *    PRD §5 要求「Vue global build + ECharts（仅雷达/柱状模块）本地 vendored，总资源 ≤ 500 KB」，
 *    这三个条件实测无法同时成立：ECharts 官方只发整包（simple 457 KB / common 647 KB /
 *    full 1 007 KB），而 radar 属 common 层 → 最小可用组合 647 KB + Vue 143 KB = 790 KB，
 *    超预算 58%；要瘦必须上构建链，那就推翻 D2「无构建链」。所以取第三方方案：
 *    手绘 SVG（约 200 行、零字节），与 M2 的 SVG 等时圈同一风格，顺带把「断网也能出报告」
 *    这条承诺做到毫无悬念。**这是对 PRD §5 的显式偏离，已登记在 `docs/DESIGN.md`。**
 * 2. **不引任何公网 CDN**（R12）：本文件与它引的 CSS 全是本地相对路径。
 * 3. **导出只限当前一次分析**（FR-213 / 条款 3.3.4）：CSV 与 JSON 都是内存里这一份结果，
 *    没有「全库导出」这种入口，也不落 localStorage。
 *
 * 报告里的每个数字都直接来自 `/api/analysis` 的响应，前端**不做任何再计算**：
 * 分数、雷达值、圈内计数、盲区面积全部照抄后端。前端一旦自己算一遍，
 * 两处口径就会分叉，而评审看到的是前端那个。
 */

"use strict";

(function () {
  const ANALYSIS_PATH = "/api/analysis";
  const SAMPLES_PATH = "/api/samples";

  const DIMENSION_LABEL = {
    medical: "基础医疗",
    education: "教育",
    shopping: "购物市场",
    leisure: "养老休闲",
    transit: "公共交通",
    food: "餐饮服务",
    convenience: "便民服务",
  };

  const SAMPLE_KIND_LABEL = {
    measured: "实测",
    interpolated: "内插",
    extrapolated: "外推",
    blocked_official: "官方禁行",
    blocked_timeout: "未测准",
  };

  const DATA_SOURCE_LABEL = {
    live: "实时调用",
    cache: "本地缓存",
    sample: "程序合成示例",
  };

  const CSV_HEADER = [
    "名称",
    "类别",
    "官方分类",
    "直线距离(米)",
    "实测步行时间(分钟)",
    "是否圈内",
    "是否实测",
    "未实测原因",
    "经度",
    "纬度",
  ];

  /** 当前这一份报告。导出与重绘都只读它 —— 没有第二处状态。 */
  let current = null;
  let busy = false;
  /**
   * FR-213 的排序状态。`key === null` 表示按后端返回的维度顺序呈现（**默认**）——
   * 一进页面就按时间重排，会把「同一维度的设施挨在一起」这个天然分组打散，反而更难读。
   */
  let sortState = { key: null, dir: "asc" };

  /** 明细表可排序列：`key` 对应 `web/isochrone.html` 表头上的 `data-sort`。 */
  const SORT_COLUMNS = {
    name: { label: "名称", type: "text" },
    category: { label: "类别", type: "text" },
    distance: { label: "直线距离", type: "number", get: (row) => row.poi.distance_m },
    minutes: { label: "实测步行时间", type: "number", get: (row) => row.poi.measured_minutes },
    polygon: { label: "落圈", type: "text", get: (row) => (row.poi.in_polygon ? 0 : 1) },
    measured: { label: "实测状态", type: "text", get: (row) => (row.poi.unmeasured ? 1 : 0) },
  };

  const el = {};

  function cache() {
    el.page = document.getElementById("report");
    el.empty = document.getElementById("report-empty");
    el.body = document.getElementById("report-body");
    el.run = document.getElementById("run-analysis");
    el.hint = document.getElementById("analysis-hint");
    el.samples = document.getElementById("sample-select");
    el.sampleNote = document.getElementById("sample-note");
    el.badge = document.getElementById("source-badge");
    el.profileBadge = document.getElementById("profile-badge");
    el.scoreCard = document.getElementById("score-card");
    el.summary = document.getElementById("summary-list");
    el.radar = document.getElementById("radar-chart");
    el.bar = document.getElementById("bar-chart");
    el.barNote = document.getElementById("bar-note");
    el.poiTable = document.getElementById("poi-table");
    el.poiCount = document.getElementById("poi-count");
    el.poiCategory = document.getElementById("poi-category");
    el.blindSummary = document.getElementById("blind-summary");
    el.blindList = document.getElementById("blind-list");
    el.usageTable = document.getElementById("analysis-usage");
    el.assertions = document.getElementById("analysis-assertions");
    el.notes = document.getElementById("analysis-notes");
    el.exportJson = document.getElementById("export-json");
    el.exportCsv = document.getElementById("export-csv");
    el.exportBlindCsv = document.getElementById("export-blind-csv");
    el.exportHint = document.getElementById("export-hint");
  }

  // -------------------------------------------------------------------------------------------
  // 小工具
  // -------------------------------------------------------------------------------------------
  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function svgNode(tag, attrs) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach((key) => item.setAttribute(key, String(attrs[key])));
    return item;
  }

  function clear(target) {
    while (target && target.firstChild) target.removeChild(target.firstChild);
  }

  /**
   * 🔴 把「缺值」与「0」分开。`Number(null) === 0`、`Number("") === 0`，
   * 所以直接 `Number(x)` 会把 `nearest_minutes: null`（**没测到**）渲染成「0.0 min」——
   * 那是本报告最不能犯的错：把「未实测」洗成「近得不得了」，时效分明明是 0 却看起来满分。
   * 所有读数值的地方一律先过这里。
   */
  function finiteOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function num(value, digits) {
    const n = finiteOrNull(value);
    if (n === null) return "—";
    return digits === undefined ? String(n) : n.toFixed(digits);
  }

  function fmtMinutes(value) {
    const n = finiteOrNull(value);
    if (n === null) return "未实测";
    return `${n.toFixed(1)} min`;
  }

  function fmtMeters(value) {
    const n = finiteOrNull(value);
    if (n === null) return "—";
    return n >= 1000 ? `${(n / 1000).toFixed(2)} km` : `${Math.round(n)} m`;
  }

  function label(key) {
    return DIMENSION_LABEL[key] || key;
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (error) {
      return { ok: false, message: `响应不是合法 JSON（${error && error.message}）` };
    }
  }

  /**
   * 手绘雷达图。六个轴，`radar` 是后端给的 `{维度: 0~100}`。
   * 刻意不做动画与交互：一张静态 SVG 在断网、低配机、截图三种场景下行为完全一致。
   */
  function drawRadar(target, radar) {
    clear(target);
    const keys = Object.keys(radar || {});
    if (!keys.length) {
      target.appendChild(node("p", "hint", "没有可绘制的维度得分。"));
      return;
    }
    const size = 320;
    const cx = size / 2;
    const cy = size / 2;
    const radius = size / 2 - 46;
    const svg = svgNode("svg", {
      viewBox: `0 0 ${size} ${size}`,
      class: "chart chart-radar",
      role: "img",
      "aria-label": `六维得分雷达图：${keys.map((k) => `${label(k)} ${num(radar[k], 1)} 分`).join("，")}`,
    });

    const angleOf = (index) => -Math.PI / 2 + (index * 2 * Math.PI) / keys.length;
    const pointAt = (index, ratio) => [
      cx + Math.cos(angleOf(index)) * radius * ratio,
      cy + Math.sin(angleOf(index)) * radius * ratio,
    ];

    // 网格：4 圈 + 6 条轴
    [0.25, 0.5, 0.75, 1].forEach((ratio) => {
      const points = keys.map((_, index) => pointAt(index, ratio).join(",")).join(" ");
      svg.appendChild(
        svgNode("polygon", { points, class: "radar-grid", fill: "none" })
      );
    });
    keys.forEach((_, index) => {
      const end = pointAt(index, 1);
      svg.appendChild(
        svgNode("line", { x1: cx, y1: cy, x2: end[0], y2: end[1], class: "radar-axis" })
      );
    });

    // 数据多边形
    const dataPoints = keys
      .map((key, index) => {
        const ratio = Math.max(0, Math.min(100, Number(radar[key]) || 0)) / 100;
        return pointAt(index, ratio).join(",");
      })
      .join(" ");
    svg.appendChild(svgNode("polygon", { points: dataPoints, class: "radar-area" }));

    // 轴标签 + 数值
    keys.forEach((key, index) => {
      const [x, y] = pointAt(index, 1.19);
      const anchor = Math.abs(x - cx) < 12 ? "middle" : x > cx ? "start" : "end";
      const text = svgNode("text", {
        x,
        y,
        class: "radar-label",
        "text-anchor": anchor,
        "dominant-baseline": "middle",
      });
      text.textContent = `${label(key)} ${num(radar[key], 0)}`;
      svg.appendChild(text);
    });

    target.appendChild(svg);
  }

  /**
   * 手绘柱状图：每个维度的**最近实测步行时间** vs 15 min 阈值线（FR-303）。
   * 超阈值的柱子标红；没有实测值的维度画成斜纹空柱并注明原因 —— 那是「没测到」，
   * 不是「0 分钟」，两者在图上必须一眼可分（PRD §4.5 的假绿防线）。
   */
  function drawBars(target, groups, thresholdMinutes) {
    clear(target);
    const rows = (groups || []).filter((group) => group.scored !== false);
    if (!rows.length) {
      target.appendChild(node("p", "hint", "没有可绘制的维度。"));
      return;
    }
    const limit = Number(thresholdMinutes) || 15;
    // 🔴 `finiteOrNull` 而不是 `Number`：`nearest_minutes: null` 是「没测到」，
    // 画成 0 分钟高的柱子等于宣称「步行 0 分钟就到」，方向性错误。
    const values = rows.map((row) => finiteOrNull(row.nearest_minutes));
    const measured = values.filter((value) => value !== null);
    // 纵轴上限：至少到阈值线，且留 15% 余量给「远超阈值」的柱子（否则超阈值看不到差别）。
    const top = Math.max(limit, ...measured, 1) * 1.15;
    const width = 520;
    const height = 260;
    const padLeft = 48;
    const padBottom = 54;
    const padTop = 16;
    const plotW = width - padLeft - 16;
    const plotH = height - padTop - padBottom;
    const slot = plotW / rows.length;
    const barW = Math.min(46, slot * 0.56);
    const yOf = (minutes) => padTop + plotH * (1 - minutes / top);

    const svg = svgNode("svg", {
      viewBox: `0 0 ${width} ${height}`,
      class: "chart chart-bar",
      role: "img",
      "aria-label": `各维度最近实测步行时间对比图，阈值 ${limit} 分钟`,
    });

    // 阈值线
    svg.appendChild(
      svgNode("line", {
        x1: padLeft,
        y1: yOf(limit),
        x2: width - 16,
        y2: yOf(limit),
        class: "bar-threshold",
      })
    );
    const thresholdText = svgNode("text", {
      x: width - 18,
      y: yOf(limit) - 6,
      class: "bar-threshold-label",
      "text-anchor": "end",
    });
    thresholdText.textContent = `阈值 ${limit} min`;
    svg.appendChild(thresholdText);

    // 纵轴刻度
    [0, 0.5, 1].forEach((ratio) => {
      const minutes = top * ratio;
      svg.appendChild(
        svgNode("line", {
          x1: padLeft,
          y1: yOf(minutes),
          x2: width - 16,
          y2: yOf(minutes),
          class: "bar-grid",
        })
      );
      const tick = svgNode("text", {
        x: padLeft - 8,
        y: yOf(minutes),
        class: "bar-tick",
        "text-anchor": "end",
        "dominant-baseline": "middle",
      });
      tick.textContent = minutes.toFixed(0);
      svg.appendChild(tick);
    });

    rows.forEach((row, index) => {
      const value = finiteOrNull(row.nearest_minutes);
      const x = padLeft + slot * index + (slot - barW) / 2;
      const isMeasured = value !== null;
      if (isMeasured) {
        const over = value > limit;
        svg.appendChild(
          svgNode("rect", {
            x,
            y: yOf(value),
            width: barW,
            height: Math.max(2, padTop + plotH - yOf(value)),
            class: over ? "bar-rect over" : "bar-rect",
          })
        );
        const valueText = svgNode("text", {
          x: x + barW / 2,
          y: yOf(value) - 6,
          class: "bar-value",
          "text-anchor": "middle",
        });
        valueText.textContent = value.toFixed(1);
        svg.appendChild(valueText);
      } else {
        // 未实测：斜纹空柱（`none` 的视觉语义），高度占满绘图区以表明「未知」而非「0」。
        svg.appendChild(
          svgNode("rect", {
            x,
            y: padTop,
            width: barW,
            height: plotH,
            class: "bar-rect unmeasured",
          })
        );
        const mark = svgNode("text", {
          x: x + barW / 2,
          y: padTop + plotH / 2,
          class: "bar-unmeasured",
          "text-anchor": "middle",
          "dominant-baseline": "middle",
        });
        mark.textContent = "未实测";
        svg.appendChild(mark);
      }
      const caption = svgNode("text", {
        x: x + barW / 2,
        y: height - padBottom + 20,
        class: "bar-caption",
        "text-anchor": "middle",
      });
      caption.textContent = label(row.key);
      svg.appendChild(caption);
      const count = svgNode("text", {
        x: x + barW / 2,
        y: height - padBottom + 36,
        class: "bar-count",
        "text-anchor": "middle",
      });
      count.textContent = `圈内 ${num(row.count_in, 0)}/${num(row.threshold_count, 0)}`;
      svg.appendChild(count);
    });

    target.appendChild(svg);
  }

  // -------------------------------------------------------------------------------------------
  // 报告渲染
  // -------------------------------------------------------------------------------------------
  function renderBadge(report) {
    const meta = (report && report.meta) || {};
    const source = meta.data_source || "—";
    el.badge.textContent = `数据来源：${DATA_SOURCE_LABEL[source] || source}`;
    el.badge.dataset.source = source;
    el.badge.title = meta.degraded_reason
      ? `降级原因：${meta.degraded_reason}`
      : "本次为实时调用结果";
    renderProfileBadge(meta);
  }

  /**
   * FR-107 的「显著标注简化模式」——档位标记必须来自**响应** `meta.lightweight`，不是前端自己算的那一份。
   *
   * 读 `meta` 而不是读 `payloadForRun().platform`：这两者在缓存命中时本来就该一致，
   * 但「报告是按哪一档算的」只有后端知道。前端把意图写进请求、再把后端确认的档位显示出来，
   * 才是对账；自己显示自己的那份，等于把「我以为我跑了简化档」当成证据。
   */
  function renderProfileBadge(meta) {
    const badge = el.profileBadge;
    if (!badge) return;
    const k = meta.k;
    if (meta.lightweight) {
      badge.textContent = `简化模式（FR-107${Number.isFinite(k) ? ` · 实测前 K=${k}` : ""}）`;
      badge.dataset.profile = "mobile";
      badge.title =
        "已按移动端轻量档计算：只跑 12 方向粗扫、跳过中点加密、关闭耗时热力场，POI 实测前 K 由 12 降到 6。" +
        "结论可与完整档差一个量级，别把两档的分数横向比较。";
      badge.hidden = false;
      return;
    }
    badge.textContent = `完整模式${Number.isFinite(k) ? ` · 实测前 K=${k}` : ""}`;
    badge.dataset.profile = "desktop";
    badge.title = "按完整档（桌面）计算：全 12 方向 + 中点加密 + 耗时热力场，前 K=12。";
    badge.hidden = false;
  }

  function renderScore(report) {
    clear(el.scoreCard);
    const score = (report && report.score) || {};
    const total = Number(score.total);
    const gradeText = score.grade || "—";
    const card = node("div", "score-main");
    card.dataset.grade = gradeText;
    card.appendChild(node("span", "score-value", Number.isFinite(total) ? total.toFixed(1) : "—"));
    card.appendChild(node("span", "score-grade", gradeText));
    card.appendChild(node("span", "score-unit", "/ 100"));
    el.scoreCard.appendChild(card);

    const facts = [
      ["盲区惩罚", `${num(score.penalty, 1)} 分（完全盲区占 ${num(score.blind_area_pct, 2)}%）`],
      [
        "总分口径",
        score.normalized
          ? `已按计分权重之和 ${num(score.weight_sum, 2)} 归一化到 0~100；字面口径值 ${num(score.total_raw, 1)}`
          : `照 PRD §4.5 字面公式 Σ(S×w)，权重之和 ${num(score.weight_sum, 2)}`,
      ],
      ["等级线", Object.entries(score.grade_bands || {}).map(([g, v]) => `${g}≥${v}`).join("  ")],
      ["不计分项", (score.reference_only || []).map(label).join("、") || "无"],
    ];
    const dl = node("dl", "score-facts");
    facts.forEach(([key, value]) => {
      dl.appendChild(node("dt", null, key));
      dl.appendChild(node("dd", null, value));
    });
    el.scoreCard.appendChild(dl);
  }

  function renderSummary(report) {
    clear(el.summary);
    (report.summary || []).forEach((line) => el.summary.appendChild(node("li", null, line)));
  }

  function renderCoverage(report) {
    const coverage = (report && report.coverage) || {};
    const groups = coverage.groups || [];
    drawRadar(el.radar, (report.score || {}).radar || {});

    const minutes = Number((report.meta || {}).minutes) || 15;
    const scored = groups.filter((group) => group.scored !== false);
    const nearest = scored
      .map((group) => finiteOrNull(group.nearest_minutes))
      .filter((value) => value !== null);
    // 阈值线取 PRD §4.5 的 T_上限（15 min，同时是默认目标时长）；目标时长改了就跟着改。
    drawBars(el.bar, groups, minutes);
    el.barNote.textContent = nearest.length
      ? `${nearest.length} / ${scored.length} 个计分维度有实测值；柱高为该维度**最近实测步行时间**，红线为 ${minutes} min 阈值线。`
      : "本次没有任何维度拿到实测步行时间，柱子全部为「未实测」——时效分按 0 计，不是 0 分钟。";

    renderPoiTable(groups);
  }

  /**
   * FR-213：按 `sortState` 给明细行排序。
   *
   * 🔴 **未实测的点永远排在最后**，无论升序还是降序 —— `measured_minutes` 是 `null`，
   * 若把 `null` 当 0 参与排序，「按时间升序」会把一堆**没测到**的设施排到最前面，
   * 读起来像「这些设施近得 0 分钟就到」。这是本项目最忌讳的假绿（PRD §4.5 的时效分同理）。
   * 没有实测值就是没有值，它既不最小也不最大，只能垫底并靠「未实测」列说明。
   */
  function sortRows(rows) {
    if (!sortState.key) return rows;
    const column = SORT_COLUMNS[sortState.key];
    if (!column) return rows;
    const dir = sortState.dir === "desc" ? -1 : 1;
    const valueOf = (row) => {
      if (column.get) return column.get(row);
      if (sortState.key === "name") return row.poi.name || row.poi.uid || "";
      return row.group.key;
    };
    return rows
      .map((row, index) => ({ row, index }))
      .sort((a, b) => {
        const left = valueOf(a.row);
        const right = valueOf(b.row);
        const leftMissing = left === null || left === undefined || left === "";
        const rightMissing = right === null || right === undefined || right === "";
        // 缺值垫底（与方向无关），缺值之间保持后端给的稳定顺序。
        if (leftMissing || rightMissing) {
          if (leftMissing && rightMissing) return a.index - b.index;
          return leftMissing ? 1 : -1;
        }
        let cmp;
        if (column.type === "number") cmp = Number(left) - Number(right);
        else cmp = String(left).localeCompare(String(right), "zh-Hans-CN");
        if (cmp === 0) return a.index - b.index; // 稳定排序：同值不打乱原有次序
        return cmp * dir;
      })
      .map((item) => item.row);
  }

  function renderPoiTable(groups) {
    const selected = el.poiCategory.value || "";
    const rows = [];
    (groups || []).forEach((group) => {
      if (selected && group.key !== selected) return;
      (group.pois || []).forEach((poi) => {
        rows.push({ group, poi });
      });
    });
    const ordered = sortRows(rows);
    const tbody = el.poiTable.querySelector("tbody");
    clear(tbody);
    let inCount = 0;
    ordered.forEach(({ group, poi }) => {
      const tr = node("tr");
      if (!poi.in_polygon) tr.className = "outside";
      if (poi.unmeasured) tr.classList.add("unmeasured");
      if (poi.in_polygon) inCount += 1;
      tr.appendChild(node("td", null, poi.name || poi.uid || "—"));
      tr.appendChild(node("td", null, label(group.key)));
      tr.appendChild(node("td", "sub", poi.official_path || poi.subcat || "—"));
      tr.appendChild(
        node("td", "num", poi.distance_estimated ? `${fmtMeters(poi.distance_m)}（估）` : fmtMeters(poi.distance_m))
      );
      const measured = finiteOrNull(poi.measured_minutes);
      const minutesCell = node("td", "num", measured === null ? "未实测" : fmtMinutes(measured));
      if (measured === null) {
        minutesCell.title = poi.unmeasured_reason || "";
        minutesCell.classList.add("muted");
      }
      tr.appendChild(minutesCell);
      tr.appendChild(node("td", null, poi.in_polygon ? "圈内" : "仅半径内"));
      tr.appendChild(node("td", null, poi.unmeasured ? unmeasuredLabel(poi) : "实测"));
      tr.appendChild(node("td", "num", `${num(poi.lnglat && poi.lnglat[0], 5)}`));
      tr.appendChild(node("td", "num", `${num(poi.lnglat && poi.lnglat[1], 5)}`));
      tbody.appendChild(tr);
    });
    syncSortHeaders();
    const sorted = sortState.key
      ? `；已按「${SORT_COLUMNS[sortState.key].label}」${sortState.dir === "desc" ? "降序" : "升序"}排列（未实测的排在最后）`
      : "；点表头可按该列排序";
    el.poiCount.textContent = rows.length
      ? `共 ${rows.length} 条（圈内 ${inCount} 条；仅返回第一页，不翻页）${sorted}`
      : "没有符合条件的设施记录。";
  }

  /** 把 `aria-sort` 与表头箭头刷成当前排序状态（无障碍：不能只靠一个箭头字符）。 */
  function syncSortHeaders() {
    if (!el.poiTable) return;
    for (const th of el.poiTable.querySelectorAll("th[data-sort]")) {
      const key = th.getAttribute("data-sort");
      const active = key === sortState.key;
      th.setAttribute("aria-sort", active ? (sortState.dir === "desc" ? "descending" : "ascending") : "none");
      th.classList.toggle("sorted", active);
      th.classList.toggle("sorted-desc", active && sortState.dir === "desc");
      th.setAttribute(
        "title",
        `${active ? "再次点击" : "点击"}按「${SORT_COLUMNS[key] ? SORT_COLUMNS[key].label : key}」排序`,
      );
    }
  }

  /** 点击表头：同一列在升/降之间切换；换列时回到升序（时间/距离通常关心「最近的」）。 */
  function onSortClick(event) {
    const th = event.currentTarget;
    const key = th.getAttribute("data-sort");
    if (!key || !SORT_COLUMNS[key]) return;
    if (sortState.key === key) {
      sortState = { key, dir: sortState.dir === "asc" ? "desc" : "asc" };
    } else {
      sortState = { key, dir: "asc" };
    }
    const groups = ((current || {}).coverage || {}).groups || [];
    renderPoiTable(groups);
  }

  const REASON_LABEL = {
    outside_polygon: "圈外（不对圈外点做实测）",
    beyond_top_k: "未进前 K（配额取舍）",
    no_probe_injected: "未接入探测器",
    probe_failed: "探测器失败",
    measurement_missing: "本批未回耗时",
    no_batch_route_target: "坐标不可用",
  };

  /** 未实测原因的短文案（表格列）。与 `REASON_LABEL` 同一份出处，只是长短两版。 */
  function unmeasuredLabel(poi) {
    const reason = poi.unmeasured_reason || "";
    if (reason === "outside_polygon") return "圈外";
    return REASON_LABEL[reason] || reason || "未实测";
  }

  function renderBlindSpots(report) {
    clear(el.blindList);
    const spots = (report && report.blind_spots) || {};
    const blocks = spots.blocks || [];
    const grid = spots.grid || {};
    const levels = blocks.reduce((acc, block) => {
      const key = block.level === "blind" ? "完全盲区" : "配套不足";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
    el.blindSummary.textContent =
      `完全盲区 ${spots.count || 0} 块，占圈内面积 ${num(spots.area_pct, 2)}%` +
      `（栅格 ${num(grid.step_m, 0)} m、判定半径 ${num(grid.radius_m, 0)} m）` +
      (Object.keys(levels).length
        ? `；分级：${Object.entries(levels).map(([k, v]) => `${k} ${v}`).join("、")}`
        : "");

    if (!blocks.length) {
      el.blindList.appendChild(
        node("li", "hint", "本次未识别到盲区块：三类基础设施（市场 / 药店 / 小学）至少各有一处可达。")
      );
      return;
    }
    blocks.forEach((block, index) => {
      const isBlind = block.level === "blind";
      const item = node("li", isBlind ? "blind-item blind" : "blind-item insufficient");
      const head = node("p", "blind-head");
      head.appendChild(
        node("strong", null, `#${index + 1} ${isBlind ? "完全盲区" : "配套不足"}`)
      );
      head.appendChild(
        node(
          "span",
          "blind-meta",
          `${num(block.area_km2, 4)} km² ｜ 距中心 ${fmtMeters(block.distance_from_center_m)} ｜ ${num(block.cells, 0)} 格`
        )
      );
      item.appendChild(head);
      const missing = (block.missing || []).map((key) => label(key)).join("、");
      item.appendChild(
        node("p", null, `缺失类别：${missing || "无（该块仅部分类别缺失）"}`)
      );
      const facility = block.nearest_facility;
      if (facility) {
        const how = facility.measured
          ? `${fmtMinutes(facility.minutes)}（实测）`
          : `${fmtMinutes(facility.minutes)}（按 ${num(facility.speed_kmh, 1)} km/h 估算，未实测）`;
        item.appendChild(
          node(
            "p",
            "blind-meta",
            `最近可用设施：${facility.name || "—"}（${label(facility.subcat)}）${
              facility.distance_m ? ` ｜ ${fmtMeters(facility.distance_m)}` : ""
            } ｜ ${how}`
          )
        );
      }
      const center = block.center_lnglat || [];
      if (center.length === 2) {
        const jump = node("button", "link-btn", "在地图上定位");
        jump.type = "button";
        jump.addEventListener("click", () => {
          if (window.CircleDocMap && window.CircleDocMap.setCenter) {
            window.CircleDocMap.setCenter(center, "盲区块几何中心");
          }
        });
        item.appendChild(jump);
      }
      el.blindList.appendChild(item);
    });
  }

  function renderUsage(report) {
    const tbody = el.usageTable.querySelector("tbody");
    clear(tbody);
    const usage = (report && report.usage) || {};
    const assertions = (report && report.assertions) || {};
    const checks = assertions.checks || {};
    const rows = [
      ["place_calls", "地点检索出网次数", usage.place_calls, checks.place_calls],
      ["quota_units", "配额消耗（origins×destinations 折算）", usage.quota_units, checks.quota_units],
      ["http_requests", "HTTP 往返次数", usage.http_requests, checks.http_requests],
    ];
    rows.forEach(([key, text, value, check]) => {
      const tr = node("tr");
      tr.appendChild(node("td", "metric", text));
      tr.appendChild(node("td", "num", num(value, 0)));
      if (check) {
        const status = node("td", check.ok ? "ok" : "over", check.ok ? "在预算内" : "超预算");
        status.title = `上限 ${check.limit}`;
        tr.appendChild(status);
      } else {
        tr.appendChild(node("td", null, "—"));
      }
      tr.dataset.metric = key;
      tbody.appendChild(tr);
    });
    const lines = usage.route_lines || {};
    const lineRow = node("tr");
    lineRow.appendChild(node("td", "metric", "路线条数（按出行方式分栏）"));
    lineRow.appendChild(
      node("td", "num", `步行 ${num(lines.walking, 0)} ｜ 骑行 ${num(lines.riding, 0)}`)
    );
    lineRow.appendChild(node("td", null, `合计 ${num(usage.route_lines_total, 0)}`));
    tbody.appendChild(lineRow);

    const stageRow = node("tr");
    stageRow.appendChild(node("td", "metric", "分段明细"));
    stageRow.appendChild(
      node(
        "td",
        "num",
        Object.entries(usage.stages || {})
          .map(([stage, view]) => `${stage}: h${num(view.http_requests, 0)}/q${num(view.quota_units, 0)}/p${num(view.place_calls, 0)}`)
          .join("；") || "—"
      )
    );
    stageRow.appendChild(node("td", null, `耗时 ${num(usage.elapsed_ms, 0)} ms`));
    tbody.appendChild(stageRow);

    clear(el.assertions);
    el.assertions.textContent = assertions.ok
      ? "§6.1 三栏预算断言：全部在预算内。"
      : "§6.1 三栏预算断言：有超限项，结果可能不完整（见上方标红行）。";
    el.assertions.dataset.ok = assertions.ok ? "true" : "false";
  }

  function renderNotes(report) {
    clear(el.notes);
    (report.notes || []).forEach((line) => el.notes.appendChild(node("li", null, line)));
    const coverage = (report.coverage || {}).meta || {};
    if (coverage.selection) {
      el.notes.appendChild(
        node(
          "li",
          null,
          `前 K 实测的挑选策略：${coverage.selection}（${
            coverage.selection === "fair"
              ? "每个维度先保一个最近点，剩余名额按全局直线距离填"
              : "全局直线距离最近的前 K 个"
          }）；逐维度配额 ${JSON.stringify(coverage.selection_allocation || {})}。`
        )
      );
    }
  }

  function renderReport(report) {
    current = report;
    el.empty.hidden = true;
    el.body.hidden = false;
    renderBadge(report);
    renderScore(report);
    renderSummary(report);
    // 🔴 顺序要紧：维度筛选下拉的选项来自本份报告的 groups，必须先建好选项，
    //    `renderPoiTable` 才知道当前该显示哪些行（否则第一遍渲染会把筛选当空值）。
    fillCategoryFilter();
    renderCoverage(report);
    renderBlindSpots(report);
    renderUsage(report);
    renderNotes(report);
    syncExportState();
  }

  // -------------------------------------------------------------------------------------------
  // 导出（FR-305 / FR-213：只限当前一次分析，附来源与禁止二次分发声明）
  // -------------------------------------------------------------------------------------------
  function syncExportState() {
    const disabled = !current;
    el.exportJson.disabled = disabled;
    el.exportCsv.disabled = disabled;
    if (el.exportBlindCsv) el.exportBlindCsv.disabled = disabled;
    el.exportHint.textContent = disabled
      ? "先跑一次体检，才有可导出的结果。"
      : "导出内容仅限**当前这一次**分析；PNG 截图请自行保留地图右下角的版权与审图号标识。";
  }

  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    // 立刻释放：报告可能被连续导出多次，不释放会攒下一串 blob。
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function stem() {
    const meta = (current && current.meta) || {};
    const center = meta.center || [];
    return `circledoc-${(meta.mode || "walking")}-${num(center[0], 4)}_${num(center[1], 4)}`;
  }

  function exportJson() {
    if (!current) return;
    // 🔴 FR-305：导出的是**结果对象**（分数 / 多边形 / 统计），不含原始 POI 记录集以外的
    // 任何百度返回原文；`meta.source_declaration` 与 `notice` 一起带上合规声明。
    const payload = Object.assign({}, current, {
      notice: {
        source: "数据来源：百度地图开放平台（地理编码 / 地点检索 / 批量算路）",
        generated_by: "CircleDoc · 15 分钟生活圈智能体检与规划助手",
        synthetic: (current.meta || {}).data_source === "sample",
        synthetic_note:
          (current.meta || {}).data_source === "sample"
            ? "本次为程序合成的示例数据，不含任何百度返回内容。"
            : null,
        redistribution: "导出内容仅限本次分析结果，不得二次分发。",
        scope: "only_this_analysis",
      },
    });
    download(`${stem()}.json`, JSON.stringify(payload, null, 2), "application/json");
  }

  function csvCell(value) {
    const text = value === undefined || value === null ? "" : String(value);
    // 逗号 / 引号 / 换行都要转义，否则含逗号的设施名会把整行拆错列。
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  /** 把各维度的 POI 摊平（与 `services/analysis.py:_flatten_pois` 同一口径，圈内圈外都要）。 */
  function flattenPois(report) {
    const out = [];
    (((report || {}).coverage || {}).groups || []).forEach((group) => {
      (group.pois || []).forEach((poi) => {
        out.push(Object.assign({ category: group.key }, poi));
      });
    });
    return out;
  }

  /**
   * FR-223：盲区清单导出。与 POI 明细分开一个文件，因为两者的行结构完全不同
   * （一个是设施、一个是区域），硬塞进同一张表会让两边都没法用。
   */
  function exportBlindCsv() {
    if (!current) return;
    const spots = current.blind_spots || {};
    const blocks = spots.blocks || [];
    const lines = [];
    lines.push("# CircleDoc 服务盲区清单（仅本次分析）｜数据来源：百度地图开放平台｜不得二次分发");
    lines.push(`# 生成时间：${(current.meta || {}).generated_at || "—"}`);
    lines.push(`# 中心点：${((current.meta || {}).center || []).join(",")}（BD09LL）`);
    lines.push(`# 判定口径：${(spots.grid || {}).step_m || 250} m 栅格，${(spots.grid || {}).radius_m || 1000} m 半径内三类设施（市场/药店/小学）`);
    lines.push(
      [
        "序号",
        "分级",
        "缺失类别",
        "缺失数",
        "面积(km²)",
        "栅格数",
        "距中心(米)",
        "中心经度",
        "中心纬度",
        "最近可用设施",
        "最近设施类别",
        "最近设施耗时(分钟)",
        "是否实测",
      ].join(","),
    );
    if (!blocks.length) {
      lines.push("# （本次未识别到盲区块）");
    }
    blocks.forEach((block, index) => {
      const center = block.center_lnglat || [];
      const facility = block.nearest_facility || {};
      lines.push(
        [
          index + 1,
          block.level === "blind" ? "完全盲区" : "配套不足",
          (block.missing || []).map((key) => label(key)).join(" / "),
          (block.missing || []).length,
          num(block.area_km2, 6),
          num(block.cells, 0),
          num(block.distance_from_center_m, 0),
          num(center[0], 6),
          num(center[1], 6),
          facility.name || "",
          facility.subcat ? label(facility.subcat) : "",
          finiteOrNull(facility.minutes) === null ? "" : num(facility.minutes, 2),
          facility.measured ? "是" : "否（按步行速度估算）",
        ]
          .map(csvCell)
          .join(","),
      );
    });
    download(`${stem()}-blind-spots.csv`, `\ufeff${lines.join("\n")}\n`, "text/csv;charset=utf-8");
  }

  function exportCsv() {    if (!current) return;
    const lines = [];
    lines.push(
      `# CircleDoc 设施明细（仅本次分析）｜数据来源：百度地图开放平台｜不得二次分发`
    );
    lines.push(`# 生成时间：${(current.meta || {}).generated_at || "—"}`);
    lines.push(`# 中心点：${((current.meta || {}).center || []).join(",")}（BD09LL）`);
    lines.push(`# 数据来源层：${(current.meta || {}).data_source || "—"}`);
    lines.push(CSV_HEADER.join(","));
    ((current.coverage || {}).groups || []).forEach((group) => {
      (group.pois || []).forEach((poi) => {
        const lnglat = poi.lnglat || [];
        const measured = finiteOrNull(poi.measured_minutes);
        lines.push(
          [
            poi.name,
            label(group.key),
            poi.official_path || poi.subcat,
            poi.distance_estimated ? `${num(poi.distance_m, 0)}(估)` : num(poi.distance_m, 0),
            // 未实测一律留空而不是写 0：CSV 进了 Excel 之后，0 会被当成真实耗时参与统计。
            measured === null ? "" : num(measured, 2),
            poi.in_polygon ? "是" : "否",
            poi.unmeasured ? "否" : "是",
            poi.unmeasured ? unmeasuredLabel(poi) : "",
            num(lnglat[0], 6),
            num(lnglat[1], 6),
          ]
            .map(csvCell)
            .join(",")
        );
      });
    });
    // BOM：Excel 打开 UTF-8 CSV 不加 BOM 会把中文显示成乱码。
    download(`${stem()}-pois.csv`, `\ufeff${lines.join("\n")}\n`, "text/csv;charset=utf-8");
  }

  // -------------------------------------------------------------------------------------------
  // 运行
  // -------------------------------------------------------------------------------------------
  function setBusy(next) {
    busy = !!next;
    el.run.disabled = busy;
    if (window.CircleDocMap && window.CircleDocMap.setBusy) window.CircleDocMap.setBusy(busy);
  }

  function currentCenter() {
    const map = window.CircleDocMap;
    if (map && map.getCenter) return map.getCenter();
    return null;
  }

  /**
   * FR-107 的档位判定：URL 上的 `?profile=` **优先于**视口宽度。
   *
   * PRD 的判据写的是「小屏**或** `?profile=m`」，只看 `matchMedia` 等于只实现了一半：
   * 桌面评审想在同一块屏上对比完整档与简化档两份报告，只能缩窗口或开 DevTools 模拟，
   * 而「简化档到底长什么样」恰恰是评审最该直接看到的东西。
   *
   * 只认 `m`/`mobile` 与 `d`/`desktop`，其余值**一律忽略并回落到视口判定**：URL 打错字不该让整次
   * 体检 400，而面板上的档位标记读的是响应 `meta.lightweight`，不会把一个回落说成强制。
   * `d` 这一侧同样有用——小屏演示时用它顶住完整档，免得被人问「为什么你这份分数不一样」。
   */
  function resolvePlatform() {
    let forced = "";
    try {
      forced = (new URLSearchParams(window.location.search).get("profile") || "").trim().toLowerCase();
    } catch (error) {
      // URLSearchParams 在极老的引擎上缺失：回落到视口判定即可，档位不是安全边界。
      forced = "";
    }
    if (forced === "m" || forced === "mobile") return "mobile";
    if (forced === "d" || forced === "desktop") return "desktop";
    return window.matchMedia && window.matchMedia("(max-width: 768px)").matches ? "mobile" : "desktop";
  }

  function payloadForRun() {
    const map = window.CircleDocMap;
    const controls = map && map.readControls ? map.readControls() : {};
    const center = currentCenter();
    const body = {
      minutes: controls.minutes || 15,
      modes: [controls.mode || "walking"],
      platform: resolvePlatform(),
    };
    // 选了预置样例就传 sample_id：后端据此套用该社区的建成形态画像（FR-102 / FR-412）。
    // 只有「样例中心点 == 地图当前中心点」时才传，否则会把样例画像套到用户自选的点上。
    const sampleId = el.samples.value;
    if (sampleId) {
      const option = el.samples.options[el.samples.selectedIndex];
      const sampleCenter = option ? (option.dataset.lnglat || "").split(",").map(Number) : [];
      const same =
        center &&
        sampleCenter.length === 2 &&
        Math.abs(sampleCenter[0] - center[0]) < 1e-6 &&
        Math.abs(sampleCenter[1] - center[1]) < 1e-6;
      if (same) body.sample_id = sampleId;
      else if (center) body.lnglat = `${center[0]},${center[1]}`;
    } else if (center) {
      body.lnglat = `${center[0]},${center[1]}`;
    }
    if (controls.data_mode) body.data_mode = controls.data_mode;
    // FR-104 类别多选：全七类勾选时 `readControls()` 给 `null`，这里就**不发**这个键
    // （后端缺省即全七类，缓存键与多选落地前完全一致；发七个键会让同一中心点换一把缓存键）。
    // 收窄过才发数组；`[]`（一个都没勾）也照实带上，由 `runAnalysis()` 在发请求前挡掉——
    // `api/analysis.py` 把 `[]` 折成 `None`（falsy），也就是「检索全部」，
    // 「取消全部却拿到全七类结果」是这份报告最不能出现的一种假数据。
    if (Array.isArray(controls.categories)) body.categories = controls.categories;
    return body;
  }

  async function runAnalysis() {
    if (busy) return;
    const body = payloadForRun();
    // 🔴 FR-104：一个类别都没勾时**不发请求**。`api/analysis.py` 把 `[]` 当 falsy 折成 `None`
    //    （= 检索全七类），于是「取消全部」会拿到一份看着完全正常的报告——比报错更糟。
    if (Array.isArray(body.categories) && !body.categories.length) {
      el.hint.textContent = "未勾选任何设施类别（0/7）：没有设施会被检索，本次没有发请求。至少勾一类。";
      return;
    }
    setBusy(true);
    el.hint.textContent = "正在请求 /api/analysis…";
    if (window.CircleDocMap && window.CircleDocMap.clearProgress) {
      window.CircleDocMap.clearProgress();
    }
    if (el.exportHint) el.exportHint.textContent = "分析进行中…";
    try {
      // 进度是**近似**的：同步接口一次性返回，这里用一个计时器按后端阶段数走字，
      // 明确标注「阶段由响应回传」，不假造百分比（真正的阶段表在响应 usage.stages 里）。
      let tick = 1;
      const timer = setInterval(() => {
        el.hint.textContent = `正在请求 /api/analysis…（已等待 ${tick++} s；阶段表在响应里）`;
      }, 1000);
      let response;
      try {
        response = await fetch(ANALYSIS_PATH, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify(body),
        });
      } finally {
        clearInterval(timer);
      }
      const payload = await readJson(response);
      // 🔴 交号要交在**判 ok 之前**：失败时最需要对上服务端日志报号，而 `/api/analysis` 的号
      // 只在响应顶层（嵌给地图的 `isochrone` 子对象没有它，见 `isochrone.js` 的 `setRequestId`）。
      if (window.CircleDocMap && window.CircleDocMap.setRequestId) {
        window.CircleDocMap.setRequestId(payload.request_id || response.headers.get("x-request-id"));
      }
      if (!response.ok || payload.ok === false) {
        const message =
          (payload.error && payload.error.message) || payload.message || `HTTP ${response.status}`;
        el.hint.textContent = `体检未完成：${message}`;
        return;
      }
      renderReport(payload);
      // 把等时圈那一段交给地图页的渲染管线（同一次请求的结果，不重复发 /api/isochrone）。
      const adopted =
        window.CircleDocMap && window.CircleDocMap.adoptIsochrone
          ? window.CircleDocMap.adoptIsochrone(payload.isochrone, { force: true })
          : false;
      el.hint.textContent = `完成：${num(payload.score && payload.score.total, 1)} 分（${
        (payload.score && payload.score.grade) || "—"
      }），耗时 ${num(payload.usage && payload.usage.elapsed_ms, 0)} ms${
        adopted ? "" : "；地图未接管（等时圈数据缺失）"
      }。`;
      // FR-212 / FR-222：把 POI 与盲区块交给地图图层。**同一次响应**的产物，不重复发请求。
      const overlays = window.CircleDocMap && window.CircleDocMap.setOverlays
        ? window.CircleDocMap.setOverlays({
            pois: flattenPois(payload),
            blindSpots: (payload.blind_spots || {}).geojson || null,
          })
        : null;
      if (overlays) {
        const blindCount = (payload.blind_spots || {}).count || 0;
        el.hint.textContent += ` 地图叠加：${overlays.pois} 个圈内设施${
          blindCount ? ` + ${overlays.blindBlocks} 块盲区` : ""
        }。`;
      }
      // FR-106：手机上把报告浮到地图上方。跑完体检却要滚到页面最底才看到内容，
      // 现场会被当成「按钮没反应」；桌面是三栏同屏，`openSheet` 内部判过视口，这里无需再判断。
      if (window.CircleDocMap && window.CircleDocMap.openSheet) {
        window.CircleDocMap.openSheet("report");
      }
    } catch (error) {
      el.hint.textContent = `体检请求失败：${(error && error.message) || error}`;
    } finally {
      setBusy(false);
      syncExportState();
    }
  }

  // -------------------------------------------------------------------------------------------
  // 预置样例（FR-102）
  // -------------------------------------------------------------------------------------------
  async function loadSamples() {
    try {
      const response = await fetch(SAMPLES_PATH, { headers: { accept: "application/json" } });
      const payload = await readJson(response);
      const items = payload.samples || [];
      clear(el.samples);
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "不使用预置样例（用地图当前中心点）";
      el.samples.appendChild(none);
      items.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.name} · ${item.form}`;
        option.dataset.lnglat = (item.lnglat || []).join(",");
        option.dataset.archetype = item.archetype || "";
        el.samples.appendChild(option);
      });
      const unverified = items.filter((item) => !item.coord_verified);
      const notes = (payload.notes || []).length;
      el.sampleNote.textContent = items.length
        ? `共 ${items.length} 个样例${
            unverified.length
              ? `；其中 ${unverified.length} 个的坐标**尚未实调核验**（标 coord_verified=false，见 README 已知局限）`
              : ""
          }${notes ? `；清单提示 ${notes} 条（见 /api/samples 的 notes）` : ""}`
        : `没有读到预置样例（${(payload.notes || [])[0] || "见 /api/samples"}）`;
    } catch (error) {
      el.sampleNote.textContent = `样例清单加载失败：${(error && error.message) || error}`;
    }
  }

  function onSampleChange() {
    const option = el.samples.options[el.samples.selectedIndex];
    if (!option || !option.value) return;
    const lnglat = (option.dataset.lnglat || "").split(",").map(Number);
    if (lnglat.length === 2 && lnglat.every((value) => Number.isFinite(value))) {
      // 只把中心点交给地图；形态画像由后端按 sample_id 套用（前端不复制一份画像表）。
      if (window.CircleDocMap && window.CircleDocMap.setCenter) {
        window.CircleDocMap.setCenter(lnglat, `样例：${option.textContent}`);
      }
      if (window.CircleDocMap && window.CircleDocMap.setRunHint) {
        window.CircleDocMap.setRunHint(`已载入样例中心点，点「一键体检」开始。`);
      }
    }
  }

  function fillCategoryFilter() {
    const groups = ((current || {}).coverage || {}).groups || [];
    const keep = el.poiCategory.value;
    clear(el.poiCategory);
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "全部维度";
    el.poiCategory.appendChild(all);
    groups.forEach((group) => {
      const option = document.createElement("option");
      option.value = group.key;
      option.textContent = `${label(group.key)}（${group.count_in} 个圈内）`;
      el.poiCategory.appendChild(option);
    });
    el.poiCategory.value = keep && groups.some((group) => group.key === keep) ? keep : "";
  }

  function init() {
    cache();
    if (!el.page) return; // 本页没有报告面板：静默退出，别影响等时圈页自身
    syncExportState();
    el.run.addEventListener("click", runAnalysis);
    el.samples.addEventListener("change", onSampleChange);
    el.exportJson.addEventListener("click", exportJson);
    el.exportCsv.addEventListener("click", exportCsv);
    if (el.exportBlindCsv) el.exportBlindCsv.addEventListener("click", exportBlindCsv);
    // FR-213：表头点击排序。事件委托挂在 thead 上，表体重绘不会丢监听。
    const head = el.poiTable.querySelector("thead");
    if (head) {
      for (const th of head.querySelectorAll("th[data-sort]")) {
        th.tabIndex = 0;
        th.setAttribute("role", "button");
        th.addEventListener("click", onSortClick);
        th.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onSortClick({ currentTarget: th });
          }
        });
      }
    }
    el.poiCategory.addEventListener("change", () => {
      // 只重画明细表：切筛选不该重画雷达与柱状图（那两张与筛选无关，重画纯属浪费）。
      const groups = ((current || {}).coverage || {}).groups || [];
      renderPoiTable(groups);
    });
    loadSamples();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 供自测与手工调试使用（页面逻辑不依赖它）
  window.CircleDocReport = {
    renderReport,
    drawRadar,
    drawBars,
    getCurrent: () => current,
    payloadForRun,
  };
})();
