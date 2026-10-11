# CircleDoc M2 等时圈前端页面 · 真机验收清单

- 被测对象：`web/isochrone.html` + `web/isochrone.js`（原生 JS、零构建），后端 FastAPI
- 环境：`http://127.0.0.1:8766/isochrone.html`（venv uvicorn；8766 端口为上一轮遗留服务，本次复用，`/api/health` 确认 `data_mode_in_effect=mock`、无 AK）
- 验收方式：browser-use MCP（navigate / evaluate_script 注入主世界 / list_network_requests / list_console_messages）
- 日期：2026-10-05
- 🔴 零真实出网确认：全程未配置 AK、未切 live；两轮 DOM 检查 `script[src*=api.map.baidu.com]` 计数均为 **0**；network 列表只出现 `127.0.0.1:8766` 请求。

## 执行环境限制（先说清楚）

1. **截图全部无法落地**：browser-use `take_screenshot` 报 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`（in-app browser `visibilityState=hidden`，无可见 surface），重试无意义。按任务预案改为文字证据（DOM 计数 / 计算样式 / network / console）。`docs/shots/` 本次没有新增 PNG。
2. **headless 无 vsync，`requestAnimationFrame` 冻结**：页面分帧渲染队列（`runQueue`/`drawHeat`）依赖 rAF，在隐藏标签下不推进。验收时注入了手动帧泵（替换 `window.requestAnimationFrame` 为可同步 drain 的队列并逐帧执行），**驱动的是页面自身的真实渲染代码路径**，仅帧时钟由脚本代替。与 rAF 时机相关的缺陷（如热力层翻倍）正是在这种「旧队列与新批次同步交错」的泵驱动下测试的——比自然帧更容易触发翻倍。

## 逐项结论

| # | 项 | 结论 |
|---|-----|------|
| 1 | 首屏加载 / 控制台 | **通过** |
| 2 | mock 15min 多边形（非正圆） | **通过**（截图未落地，见环境限制 1） |
| 3 | FR-203 顶点点击 → tooltip + polyline | **通过**（原偏差 D-1 已修复并复验，见文末「D-1 复验记录」） |
| 4 | FR-204 热力层开关不翻倍（renderToken 修复实测） | **通过** |
| 5 | 竞态：连点 3 次 + 在飞切模式不串台 | **通过** |
| 6 | FR-205 进度「已测方向数/总数」 | **通过** |
| 7 | FR-105 降级合成 SVG + 离线标注 | **部分通过**（代码路径真跑，但未在真 DevTools offline/网络拦截下验证；截图未落地） |
| 8 | FR-206 双模式第二轮前显式提示 + 两栏计量 | **通过** |
| 9 | 五种 sample 话术两两可区分 | **通过** |
| 10 | 键盘可达性粗查 | **通过**（焦点可见性未验证，见未验证清单） |
| 11 | 首页回归 / 零外链 | **通过** |

---

### 1. 首屏不白屏、控制台无未捕获异常 —— 通过
- 证据：导航后 `list_console_messages`（含全部级别）返回 **0 条**；全部操作结束后复查仍为 **0 条**。
- 首屏 DOM：`#stage` 可见（宽度>0）、`#stage-tag` 显示「离线示意图，非真实地图」、engine=「地图引擎：SVG 示意图」、mode=「数据模式：离线合成数据（mock）」、FR-105 banner 显示「实时服务不可用，当前为离线报告模式」。
- `script/link` 全部 src 本地：`app.css`、`isochrone.css`、`data:,`（favicon）、`isochrone.js`；external=[]。
- 复现：打开页面 → 查控制台。

### 2. mock 15min 等时圈多边形 —— 通过
- 操作：`data-mode=mock`，`#minutes=15`，点「开始分析」→ `run-hint = 完成：步行 15 min（200）`，`request_id=mock-walking-15`，`degraded=false`，`area_km2=6.957`。
- 多边形：`#g-poly path` class=`poly poly-walking`，环 25 点；计算样式 `fill = rgba(95, 168, 216, 0.25)`（半透明填充）+ `stroke = rgb(45, 95, 143)` 2px（描边）。
- **非正圆实测**：24 个顶点到中心半径 min=178 m、max=1269 m，**max/min = 7.12**；同半径正圆对照线（`#g-ref circle`，FR-201 开关默认开）可见。
- 截图：`take_screenshot` 因无可见 surface 失败（见环境限制），以计算样式+几何数据代替。
- 复现：打开页面 → 数据模式选 mock → 点「开始分析」。

### 3. FR-203 顶点点击 → tooltip + 懒加载 —— 通过，带偏差（D-1）
- tooltip（真实派发 click 到 `#g-vertices circle` 第 1 个）：`顶点 #1（步行）｜方位角 0.0°｜实测耗时 11.9 min｜路径距离 785 m｜采样：实测：批量算路直接测得该方向｜置信 0.61｜坐标 116.40740, 39.91125`。三要素（方位角/实测耗时/路径距离）齐全。
- 懒加载端点：点顶点表行内「取真实路径」按钮 → network 捕获 `POST http://127.0.0.1:8766/api/isochrone/polyline [200]`，状态行「正在为顶点 #1 补拉真实路径（第 1/12 次）…」，服务端 12 次硬上限由前端先行拦截镜像（`polyline used/limit` 行）。
- **偏差**：按验收表述「顶点点击 → …且触发 /api/isochrone/polyline」，实测**点击 SVG 顶点本身只弹 tooltip、不发 polyline 请求**（network 列表在纯顶点点击后无新增），发请求的是表格行按钮。代码注释与页面提示语（「点击顶点或表格行内『取真实路径』」）表明这是有意设计，但与验收条款不一致 → 记缺陷 D-1。
- **D-1 修复后复验（实现人，见文末复验记录）**：点顶点本身现在会发出 `POST /api/isochrone/polyline` 并画出路径串，本项改为**通过**。

### 4. FR-204 热力层翻倍缺陷修复实测 —— 通过
- 响应 `duration_field.cells` 长度 = **57**（step_m=250）。
- 开→关→开→关→开，五轮后 `#g-heat rect` 计数实测：**57 / 0 / 57 / 0 / 57**；`#g-heat` group 子节点同步 57，**无残影、无翻倍**。
- layer-hint 文案与数字一致：开=「热力层绘制 57 格，网格步长 250 m（IDW 插值耗时）」；关=「耗时热力层已关闭：上一轮的网格已整组清除（不留残影）」。
- 竞态加固验证：在帧泵交错驱动下反复 `renderAll`+切开关（旧批次/旧队列与新 token 竞争），rect 数始终精确等于当前响应 cells 数（57），`web/isochrone.js:838-845` renderToken 世代号修复生效；未复现旧「49 画成 58」缺陷，也未出现「修好后热力层=0」的过杀。
- 注意：本项在 rAF 冻结环境下用手动帧泵验证（见环境限制 2），跨帧竞争场景被同步放大而非缩小。

### 5. 竞态实测 —— 通过
- 操作：连点「开始分析」3 次（绕过 disabled 直接 `click()`）→ 3 个 walking 请求同时在飞（`state.runSeq` 1→5）；随即 `#mode` 切 `riding` 并派发 change（runSeq bump、`state.polylineSeq` 复位）。
- 结果：3 份响应全部按过期处理——banner=「步行 结果已收到，但未替换当前视图」，正文「请求在飞期间改过出行方式或中心点，这次结果只存入对比数据（24 个顶点）。当前显示：骑行」；`activeResult=null`、`#g-poly path` **不存在** → **没有「用骑行配色画步行数据」的串台**；walking 数据只在 `#g-other` 对比组以对比样式出现（`compare` 自动勾选）。
- 切回 walking：`#g-poly path` class=`poly poly-walking`，热力层重开 `#g-heat rect = 57` = 当前响应 cells 57，视图不残留过期内容。

### 6. FR-205 进度显示 —— 通过
- 在飞（同步采样）：`progress-text = 「请求进行中：已测方向 0 / 12（progress 由响应体回传…）」`，`run-hint` 每 200ms 走秒（分阶段可交互）。
- 完成后：`「已测方向 36 / 36」`，进度条 `#progress-fill width:100%`，`#progress-stages` 3 条阶段记录（Step1 粗扫 / Step2 二分收敛 / …），全程不白屏（面板与舞台持续渲染，控制台 0 消息）。

### 7. FR-105 降级到合成 SVG —— 部分通过
- 已验证：调用页面真实失败出口 `degradeToSvg('…')`（注入失败/超时的同一渲染分支，`web/isochrone.js:1299`）→ banner=「实时底图不可用，已降级为离线示意图」+ 原因解释 + 处置话术；engine-badge `data-engine=failed`；`#stage-tag` 显示「**离线示意图，非真实地图**」；SVG 舞台持续可见，多边形/热力 57 格/顶点 tooltip 全部照常，**无未捕获异常**（控制台仍 0 条），降级前后 `api.map.baidu.com` 脚本注入数=0（零出网）。
- 页面首屏本身就处于 mock 降级态：无 AK 时 engine=SVG、FR-105 banner「实时服务不可用，当前为离线报告模式」已由 `/api/health` 驱动显示（真实数据路径，非手调函数）。
- 未验证（诚实登记）：**真 DevTools offline 模拟 / 网络层拦截 `api.map.baidu.com`** 未执行——本会话 chrome-devtools MCP 只暴露 `list_pages`/`handle_dialog`，无 emulation/interception 工具；且任务禁止任何真实出网尝试（含带假 AK 的注入请求），classifier 亦拦截了该操作。`jsapiWanted()` 的 `navigator.onLine===false` 分支（`web/isochrone.js:457`）只有代码审读证据。→ 记 **未真机验证**，建议评审在有头浏览器里勾选 DevTools Network offline 后把数据模式切 live 复验。
- 截图未落地（环境限制 1）。

### 8. FR-206 双模式配额提示 + 两栏计量 —— 通过
- 时序正确：切 riding 的瞬间（**早于任何第二轮请求**，此时 `results.riding=false`、network 无新 POST）`#quota-notice` 可见：
  - 标题「即将跑第二轮：骑行 等时圈」；
  - 正文「**骑行与步行共用同一份 5,000 条/日 路线配额**（不是两套额度：同账号下各 AK 配额共用）。**跑双模式当日路线配额翻倍**，超出后当天不再可用（配额熔断持续到次日零时）」；
  - meta「第一轮（步行）`quota_units = 96` ｜ `http_requests = 8` ｜ 路线条数合计 ≈ 96 ｜ 本地预算上限 route_lines={…} ｜ 本轮预计再消耗相近数量…」；焦点自动落到「确认配额可承担，运行第二轮」按钮。
- usage 表 `td.metric` 实测行：`quota_units`、`http_requests`、`place_calls`、`cache_hits`、`elapsed_ms`、`route_lines.walking`、`route_lines.riding` —— **quota_units(96) 与 http_requests(8) 各占独立一行，两栏都展示**，未合并。

### 9. 五种 sample 话术两两可区分 —— 通过
- mock 结果天然凑齐五种：顶点表 `sample` 集合实测 = `[measured, interpolated, extrapolated, blocked_official, blocked_timeout]`（真实渲染，非仅函数直调）。
- `SAMPLE_LABEL` 5 值去重后 5 条唯一；图例 5 个 chip 全部可见。
- 关键两条实测行文案（顶点表第 5 列真实文本）：
  - `blocked_official` → 「官方禁行证据：路线规划给出禁行/逆行/无路线，**这条路走不通**」；
  - `blocked_timeout` → 「本次未测准：返回空或超时，**可对该方向重试**」。
  - 两者不同句、处置动作指向不同（硬判据 vs 可重试）。验证方式：DOM 实文 + 直读页面 `SAMPLE_LABEL` 映射双重确认。

### 10. 键盘可达性粗查 —— 通过（有保留）
- `run / lng / lat / addr / city / mode / minutes / data-mode / heat / pick-point / recenter / geocode / browser-ak` 13 个控件程序化聚焦后 `document.activeElement` 全部命中（原生 button/input/select，Tab 可达）。
- 顶点信息不只靠 hover：SVG 顶点带 `tabindex=0`、`role=button`、`aria-label="顶点 N，方位角 … 度"`；派发 `keydown Enter` 实测弹出 tooltip（顶点 #4，方位角 45.0°/8.9 min/588 m）。Escape 关 tooltip 有全局监听。
- 未验证：焦点环视觉可见性、对比度、Tab 顺序合理性（需截图/人眼，本环境截图不可用）——只记录，未改代码。

### 11. 首页回归 —— 通过
- `curl http://127.0.0.1:8766/` → **200**。
- 首页 HTML 扫描 `https?://` 外链：**0 条**；等时圈页浏览器内 script/link src 全本地（见项 1）。零公网 CDN 纪律（R12/R13）保持。

---

## 缺陷清单（交主会话派修，验收侧未改任何代码）

| ID | 级别 | 描述 | 复现 |
|----|------|------|------|
| D-1 | P2 | FR-203 验收表述为「顶点点击 → tooltip **且**触发 /api/isochrone/polyline」；实测点击 SVG 顶点只弹 tooltip 不发请求，懒加载入口是顶点表行内「取真实路径」按钮（端点本身 200 正常）。属交互入口与条款不一致 / 条款歧义，需产品侧裁决：要么改文案对齐实现，要么让顶点点击（二次点击或 tooltip 内确认按钮）直接补拉 | 打开页面跑 mock → dispatch click 任一 `#g-vertices circle` → 看 network 无 polyline；再点表格行「取真实路径」→ POST polyline 200 |

（未发现其他缺陷。旧「热力层 49→58 翻倍」缺陷经项 4 五轮开关实测确认已修复。）

**D-1 状态：已按「让顶点点击直接补拉」裁决修复（实现人=代码角色），复验证据见文末「D-1 复验记录」；签 Done 留给提出人（测试角色）按原用例复跑。**

## D-1 复验记录（修复后，实现人自验）

环境：`127.0.0.1:8767`（本次新起 uvicorn，验后已关闭），`data-mode` 走启动探测 → mock，全程零真实出网。驱动方式：chrome-devtools MCP 在真实页面里对命中点派发 `pointerdown/pointerup/click`。

1. **点击顶点本身就是懒加载入口**（原偏差的直接反证）
   - 跑 mock 15 min → `#stage` 内 27 个 `.vertex` 圆点。
   - `document.elementFromPoint(圆心)` = `circle.vertex sample-measured` → 顶点确实是命中目标，不是被舞台盖住。
   - 派发点击后：network 新增 `POST /api/isochrone/polyline [200]`；页面出现 `.real-path`（1 条，25 个折点）；状态行「顶点 #4 路径已绘制（25 个折点，合成示意线，非真实路网轨迹，耗时 14.8 min，距离 967 m，懒加载 1/12 次，剩余 11 次）」；tooltip 末行「路径串：合成示意线，非真实路网轨迹｜补调耗时 14.8 min｜路径距离 967 m」。
   - 复现：打开页面跑 mock → 点任一顶点圆点 → 看 network 有 polyline、地图上有折线路径。
2. **根因与改法**（原判定「只弹 tooltip」的真实原因是事件被舞台吃掉）
   - `setPointerCapture` 会把该指针的后续派生事件（含 `click`）一律改派给捕获者（舞台 SVG），于是圆点上注册的 `click` 永远收不到——所以只有表格行按钮能触发。
   - 改法一：舞台 `pointerdown` 里先判 `event.target.closest(".vertex")`，落在顶点上的按下不进拖拽、不捕获指针（顶点约 12px，从这里起手平移的机会成本可忽略）。
   - 改法二：`onVertexPick` 统一走 `loadPolyline(vertex, event)`（SVG 与 JSAPI 两条路径都汇入），**不新开计数、不绕过** 12 次上限与 `polylineSeq` 世代号。
3. **12 次硬上限在两条入口上都生效**
   - 在一个已点满额度的服务进程上复跑：点击顶点后**未出网**，状态行「顶点 #1 未取真实路径：本次已用满 12 次懒加载额度（服务端 used=12），按契约只回显顶点坐标」，tooltip 同步显示「路径串：懒加载额度已用满（12 次），只回显顶点坐标」（不说「正在补拉」骗人）。
   - 计量表 `polyline_calls / polyline_limit` = 12/12 与状态行的 12/12 同源（`usage.polyline_*`），响应头 `X-*` 三处对账见 `swarm/m2_smoke.log`。
4. **连点不串台（补强项 5 里缺的 polyline 侧竞态）**
   - 用包装 `fetch` 放大第一次请求的返回时延（700 ms），先后点击顶点 #6、#10：network 两条 polyline 请求都发出（各 200），`.real-path` 始终只有 1 条且画的是**后点的 #10**；状态行「顶点 #6 的路径响应已过期（后面还有更新的一次点击），本次未采用」。
   - 切模式时 `polylineSeq` 复位，旧模式的路径串不会画到新模式上（`web/isochrone.js:2055`）。
5. ~~**仍未验证**：JSAPI 实时底图那条路径上的 `BMap.Marker` 点击~~ → **已于 2026-10-10 验掉**，见下一节「JSAPI 实时底图复验记录」。真机触摸点击仍属 R15 清单。

## JSAPI 实时底图复验记录（2026-10-10，实现人自验，真实出网）

上面 D-1 那一轮是在 **SVG 降级舞台**上复验的（当时禁真实出网，`BMap.Marker` 那条路径根本没跑到）。
本轮有了可用 AK，把那条路径补上。

**环境**：`.venv` 起 `uvicorn api.app:app --host 127.0.0.1 --port 8793`；`.env` 为 `DATA_MODE=auto` 且两类 AK 齐备，
`/api/health?force=true` → `data_mode_in_effect = live`、6 个服务 `available`。
浏览器端 AK 由**页面本地**提供（`localStorage` → `readStoredAk()`）：后端 `public_dict()` 只回 `has_browser_ak` 布尔值、
刻意不下发 AK 值，所以这条路径必须本机给值，不是配置缺失。给值这一步走 `127.0.0.1:8794` 上一个只在本地起、
不记日志的最小 CORS 服务（`swarm/m_ak_seed.py`，仓库外），目的是**不把 AK 明文写进任何命令或脚本调用里**。
🔴 但本轮仍有两处泄漏，须如实登记：a11y 快照会把 `#browser-ak` 输入框的值整串带出来，底图瓦片 URL 也带着 `ak=` 进了
会话记录；截图前已用 CSS 隐藏该输入框，且本轮不产出 PNG，所以泄漏面限于会话转录，不落在仓库里。

### 1. 确认加载的是真实底图，不是 SVG 降级

| 证据 | 实测值 |
| --- | --- |
| 引擎徽标 | `地图引擎：百度 JSAPI 4.0（实时底图）`（`#engine-text`） |
| 命名空间 | `window.BMap && window.BMap.Map` → true；`#map-live` 未隐藏、`#stage` 不再显示、`#stage-tag` 隐藏 |
| 瓦片请求 | `apimaponline{0,1,2,3}.bdimg.com/pvd/?qt=vtile&v=4.0&…&ak=…` 共 20 条，全部 200 |
| 覆盖物 DOM | `#map-live` 内 `div.BMap_Marker` **23 个**（22 顶点 + 1 中心点），`canvas` 1 个（JSAPI 4.0 矢量层走 canvas，无 SVG path 可数） |
| 版权条 | 图内出现官方文本 `© 2026 Baidu - GS(2025)4125号 - 甲测资字11111342 - 京ICP证030173号 - Data © 百度智图`，未删未遮 |

### 2. live 等时圈（顶点来源）

`POST /api/isochrone`（步行 15 min，中心点 116.40728, 39.94223）→ 200 / 2462 ms，
`meta.data_source = 实时调用`、圈内面积 1.706 km²、**22 个顶点**，进度条 12/12 方向、Step2 五轮收敛。
响应体 `usage` 报 `quota_units=40 / http_requests=6 / batches_sent=6`，而同一 `request_id` 的**服务端**
`http_request` 日志是 `quota_units=44 / http_requests=7` —— 差一个批次、方向是少报，
已作为**新发现未修**登记进 `README.md` §10.7 与 `docs/TEST_REPORT.md` §6。

### 3. 顶点点击（本轮的主验收项）

点击方式：`document.elementFromPoint(顶点圆心)` 先确认命中的就是 `div.BMap_Marker`，
再在该元素上按真实顺序派发 `pointerover → pointerdown → mousedown → pointerup → mouseup → click`
（带 `clientX/clientY`、`bubbles:true`）。**没有绕过 JSAPI 自己调 `marker.dispatchEvent` 之外的任何内部方法**，
事件由 BMap 的覆盖物监听器接住后再进我方 `onVertexPick`。

| 顶点 | 采样方式 | 出网 | 结果（状态行原文） |
| --- | --- | --- | --- |
| #6（方位角 90°） | `measured` | `POST /api/isochrone/polyline` 200 / 153 ms / `quota_units=1` | 「顶点 #6 路径已绘制（**48 个折点，真实路网轨迹**，耗时 15.4 min，距离 1.05 km，懒加载 1/12 次，剩余 11 次）」 |
| #2（方位角 15°） | `interpolated` | 同上 200 / 194 ms / `quota_units=1` | 「顶点 #2 路径已绘制（**45 个折点，真实路网轨迹**，耗时 13.6 min，距离 950 m，懒加载 2/12 次，剩余 10 次）」 |

- tooltip 与状态行同源，且**话术随取数层变**：live 下写「路径串：真实路网轨迹」，
  不再是 SVG/mock 那轮的「合成示意线，非真实路网轨迹」——同一条 `pathHonestLabel()` 在两条路径上都判对了。
- 计量表 `polyline_calls / polyline_limit` = 2/12，与状态行、`body.usage` 同源；`polyline_rejected = 0`。
- `highlightLiveVertex()` 的 `panTo` 生效；**中心点未被改写**（`onLiveMapClick` 只在 `pickMode` 下动中心，
  实测点顶点后 `#center-readout` 仍是 `116.40728, 39.94223 ｜ 样例：交道口街道`）→ 无「点顶点把中心点挪走」的回归。
- 目视确认（截图现场查看，未入库，原因见下「关于截图」）：等时圈多边形为**非正圆**蓝色面，
  橙色真实路网线沿街道从中心点走到 #6 / #2 顶点。

### 4. 顺带验掉的 live 报告提示，以及它抓出的缺陷

`一键体检`（交道口，live）→ `POST /api/analysis` 200 / 12463 ms，`place_calls=7 / quota_units=12 / http_requests=8`，
54.0 分（D），69 个圈内设施 + 3 块盲区。`#analysis-notes` 里**确实渲染出**了 `market` 召回不足那条诚实提示。

第二次点同一个样例：40 ms、`place_calls / quota_units / http_requests` **全 0**、徽标「数据来源：本地缓存」，
**但提示不见了** —— 因为当时的判定是 `data_source == "live"`，而缓存命中时 POI 层报 `cache`。
同一份真实数据、同一个缺陷，第二次跑却改了口。修法与回归断言见 `docs/TEST_REPORT.md` §2.9；
改完重启服务再跑第三次：178 ms、三栏仍全 0、提示以新文案（「本次 POI 层来自真实检索（或其本地缓存复用）」）出现。

### 5. 本轮**没有**验的（别当成已验）

1. **12 次懒加载硬上限在 JSAPI 底图路径上**：要再花 10 次真实 `direction`，按配额纪律不跑。
   上限逻辑（`state.polylineCalls >= limit` 先拦再出网 + 服务端 `polyline_call_limit`）与取数层无关，
   SVG 路径那轮已实测「用满后不出网、状态行与 tooltip 都不骗人」，此处不重复烧配额。
2. **连点竞态（`polylineSeq` 丢过期响应）在 JSAPI 路径**：同一份 `loadPolyline()`，SVG 路径已实测；
   实时底图侧只确认了「两次点击各自成线、后一次覆盖前一次」的常规序。
3. **真机触摸**（双指 pinch、惯性拖拽）——仍属下面 R15 清单，代码里确实没有 pinch 实现。
4. **骑行等时圈在真实底图上**：本轮刻意只跑步行单模式，避免第二轮再烧一份路线配额。

### 6. 关于截图

本轮**能**截图（chrome-devtools MCP，现场目视确认过橙色路网线），但 PNG **不入仓库**：
一是截图必然含百度瓦片与版权条（条款 6.6 允许展示、但把第三方地图影像固化进开源仓库是另一回事），
二是该 MCP 的写文件落点被限制在它的 workspace root（本机 `C:\Users\adm`），写不进仓库路径。
所以本文件延续既有口径：**以 DOM 计数、network 记录、状态行与 tooltip 原文作为可复核证据**。

## FR-104 设施类别多选 · 真实浏览器复验（2026-10-11，实现人自验，零出网）

本轮的条件与 10-10 那轮**相反**：这台机器现在配了真实 AK，所以「零出网」不再由缺配置保证，
必须自证。起服务时把两个 AK 用空值环境变量覆盖 `.env`（`BAIDU_SERVER_AK= BAIDU_BROWSER_AK=`），
端口 `8795`。

### 1. 先确认它真的没出网

| 证据 | 取值 |
| --- | --- |
| `GET /api/health` | `server_ak_type: not_configured` |
| 整轮 DevTools network 记录 | **8 条**，全部指向 `127.0.0.1:8795`（`health`、`samples`、2 条 `POST /api/analysis`、4 条 `POST /api/isochrone`），**零条出网** |
| `POST /api/analysis` 响应头 | `x-http-requests: 0`、`x-place-calls: 0`、`x-quota-units: 0` |

### 2. 收窄勾选 → 请求体、警示、分母三处同时对上

勾选 `基础医疗 / 养老休闲 / 公共交通 / 餐饮服务 / 便民服务`（取消 教育、购物市场），点「一键体检」：

```json
{"minutes":15,"modes":["walking"],"platform":"desktop","lnglat":"116.4074,39.9042",
 "data_mode":"mock","categories":["medical","leisure","transit","food","service_ref"]}
```

* 状态行原文：**「完成：51.7 分（D），耗时 83 ms。 地图叠加：21 个圈内设施 + 1 块盲区。」**
* `#analysis-notes` 里逐字渲染了盲区警示（含「只可能偏大、不可能偏小」「这一类这次没查」两句）；
  被取消的 `education` / `market` 正是装着 `primary_school` / `market` 的维度，所以该出现。
* 评分事实：**「已按计分权重之和 0.60 归一化到 0~100；字面口径值 40.0」**
  —— 0.60 = 0.20 + 0.15 + 0.15 + 0.10 + 0.00，与勾选的五类逐一对得上，**分母确实跟着变了**。
* **全选态不发 `categories` 键**（这条口径必须有请求体级证据，光看代码不算验过）。七类勾满再点一次，
  DevTools 记到的 body 是 `content-length: 102` 的
  `{"minutes":15,"modes":["walking"],"platform":"desktop","lnglat":"116.4074,39.9042","data_mode":"mock"}`
  —— **确实没有 `categories` 键**。同一次的状态行：**「完成：44.6 分（D），耗时 15 ms。 地图叠加：23 个圈内设施 + 1 块盲区。」**
  与收窄那次的 51.7 分 / 21 个设施摆在一起，正是「分母不同、不可比大小」的现场样本（同一中心点、同一 `mock` 层）。
* **0/7 挡在 `fetch` 之前**：取消全部后点「一键体检」，`performance.getEntriesByType('resource')` 里
  `/api/analysis` 的计数 **2 → 2 不变**（真的没发请求），`#analysis-hint` 原文
  「未勾选任何设施类别（0/7）：没有设施会被检索，本次没有发请求。至少勾一类。」
* **「恢复全选」按钮**：在 0/7 态可点，点完 `#category-hint` 回 `all`、按钮自身转 `disabled`、
  `readControls().categories` 回 `null`。

### 3. 🔴 本轮抓出的真实缺陷：全选时提示写的是「本次只检索这类」

七个框默认全勾，页面加载后 `#category-hint` 却是 `data-state="subset"` +
「已勾选 7/7：本次只检索这几类…」。根因、修法与三条回归断言见 `docs/TEST_REPORT.md` §2.10。
**这一条是本轮唯一的非文案收获**：`node --check` 与当时全部单测都是绿的，
只有把页面打开、读那一句字才看得见。

修复后四态的实测（`getComputedStyle`，重跑一轮逐态取样）：

| 勾选态 | `data-state` | 颜色 | 字重 | `readControls().categories` |
| --- | --- | --- | --- | --- |
| 7/7 全选 | `all` | `rgb(107, 114, 128)`（灰 = 不警示） | 400 | `null` |
| 6/7，只取消 `service_ref`（不碰三类） | `subset` | `rgb(169, 100, 10)`（琥珀） | 400 | 6 元素数组 |
| 6/7，取消 `market` | `hurt`（首句「已勾选 6/7，但取消的「购物市场」里装着盲区主判定（FR-…」） | `rgb(207, 34, 46)` | **600** | 6 元素数组 |
| 0/7 | `none` | `rgb(207, 34, 46)` | **600** | `[]`（由 `runAnalysis()` 挡下） |

### 4. 本轮**没有**验的

1. **live / cache 下的 `categories` 效果**：零出网是本轮前提。子集只改检索集合，不改算路，
   所以 live 侧的额外风险只在配额（子集省调用），不在结论形态。
2. **移动端（≤ 768 px）下的七行勾选**：当时抽屉尚未实现（见 README §10.3 的两步更正），本轮只在桌面宽度验。
   → **同日 FR-106 落地后已补**：`#categories .check` 在 375×667 与 414×896 两档各 12 行、实测高 44/45 px，
   见下一节「FR-106 移动端底部抽屉」第 1 / 2 项。
3. **键盘 Tab 顺序穿过七个框 + 「恢复全选」**：未逐项走查，只确认框是原生 `input[type=checkbox]`。

## FR-106 移动端底部抽屉 · DevTools 实测（2026-10-11，实现人自验，零出网）

**这一节补的是 PRD §6.3 那条一直没有可复现证据的验收项**：「两档视口 375×667 / 414×896，
无横向滚动、无元素重叠、点击热区 ≥ 44 px、抽屉可展开收起、自动走 FR-107 轻量档」。
此前 README §10.3 把它写成「已验收」是不实陈述，10-11 先更正为「未实现」、同日落地后按实测回填。

### 0. 前提与零出网自证

- 服务：`uvicorn api.app:app --port 8795`，用空值压掉 `.env` 里的两个真实 AK
  （`BAIDU_SERVER_AK= BAIDU_BROWSER_AK=`，pydantic-settings 的空环境变量优先级高于 `.env`）。
- 通道：chrome-devtools 设备模拟。**每次改完 CSS 必须 `navigate_page {type:"reload", ignoreCache:true}`**，
  否则读回来的计算样式还是旧的（本轮就差点把「规则没生效」误判成 bug）；信几何之前先确认那条规则真在
  `document.styleSheets` 里。
- 零出网证据：本轮 `POST /api/analysis`（`platform:"mobile"`）响应头
  `x-http-requests: 0`、`x-place-calls: 0`、`x-quota-units: 0`，`x-request-id: f0c790505f524336`。

### 1. 375×667（PRD §6.3 小档）

| 元素 | 实测 |
| --- | --- |
| `body[data-sheet-mode]` | `mobile` |
| 页头 `.page-head` | 高 **119 px** |
| 提示横幅 `.banner` | 高 **135 px**（`top 127 → 262`） |
| 配额通知 `.notice` | `display: none`（本轮未触发，高 0） |
| **地图 `.stage-area`** | **`top 278 → bottom 615`，高 337 px**；内联 `--map-h: 337px`，计算高度与之一致 |
| 把手条 `#sheet-bar` | `top 615 → bottom 667`，高 **52 px**，`display: flex` |
| 三个 tab `.sheet-tab` | 高 `[44, 44, 44]` |
| 拖拽区 `.sheet-grip` | 高 **44 px**（宽 104 px），`min-height: 44px` |
| `#controls .check` | **12 行**，高只有两种值：**44 / 45 px** |
| 「← 返回首页」`.back` | 高 **44 px**，`display: inline-flex` |
| 页脚链接 | 高 **44 px** |
| `documentElement.scrollWidth / clientWidth` | **375 / 375** → 无横向滚动 |
| 收起态 `#controls` | `transform: matrix(1,0,0,1,0,492.35)`、`visibility: hidden` |
| 展开态 `#controls` | **`top 175 → bottom 615`，高 440 px**，`translateY(0)`、`visibility: visible`、`overflow-y: auto`、圆角 `14px 14px 0 0` |
| 面板底边 ↔ 把手条顶边 | **`gap = 0`**（无缝，不露 1 px 残边） |
| `aria-expanded` | `controls=true, readout=false, report=false` |

**「地图占满首屏」的读法**：首屏 667 px 里，页头 + 横幅 + 留白占掉 278 px，剩下 **337 px 整条给地图**，
把手条贴死屏幕底边；图例（541 px）和页脚（124 px）在 615 px 以下，**靠滚动才看见**——这是刻意的，
把它们也算进高度扣减就是 §2.12 那个 bug。

### 2. 414×896（PRD §6.3 大档）

| 元素 | 实测 |
| --- | --- |
| 页头 / 横幅 | 119 / 135 px（与小档一致，压缩规则不是按宽度硬编码） |
| **地图 `.stage-area`** | **`top 278 → bottom 844`，高 566 px**，内联 `--map-h: 566px` |
| 把手条 | `top 844`，高 52 px |
| tab / `check` / `.back` | `44, 44, 44` / 44-45 px / 44 px |
| `scrollWidth / clientWidth` | **414 / 414** → 无横向滚动 |
| 整页高度 `scrollHeight` | 1520 px（抽屉是 `fixed`，不参与文档流） |
| 展开态 `#controls`（**等过渡落定后**） | **`top 253 → bottom 844`，高 591 px**，`max-height: 591.36px`（= 66vh），`translateY(0)`，`gap = 0`，未开的两块 `visibility: hidden` |

### 3. 开合：拖、点、键盘三条输入路径都走了一遍（375×667）

`data-sheet-open` 逐步实测（合成 `PointerEvent`：`pointerdown` 打在 `.sheet-grip` 上，
`pointermove/up` 派发到 `window`）：

| 动作 | 结果 `data-sheet-open` |
| --- | --- |
| 基线（收起） | `""` |
| **上拖 240 px** | `controls`（开） |
| **下拖 300 px** | `""`（关） |
| **轻点（收起态）** | `controls`（开） |
| **轻点（展开态）** | `""`（关）← 这条是第一版的 bug，见 §2.12 |
| 轻点（`readout` 开着时） | `""`（关的是当前那块，不是写死 `controls`） |
| Escape（`report` 开着时） | `""` |
| 收尾残留 | 三块面板的内联 `transform` 全为 `none`；`.sheet-dragging` 元素数 **0** |

三个 tab 逐个 `click()`：`controls → readout → report` 顺次接管，`aria-controls` 与 `aria-expanded` 同步。
**一次只开一块**落定后复测：`report=visible/ty=0`，另两块 `hidden/ty=492.35`。

⚠️ 这里有个必须记的测量坑（和 `docs/TEST_REPORT.md` §2.12 第 ③ 条同源）：收起态的 `visibility` 带
`transition: visibility 0s linear 0.2s` **延迟**，所以在同一个 `evaluate_script` 里刚切完就读，
三块面板会**同时报 `visible`**——那是过渡没走完，不是重叠。分两次调用重读才是真值。

### 4. 断点唯一真值 + 不伤桌面

| 视口 | `data-sheet-mode` | 把手条 | 面板 | 地图高 |
| --- | --- | --- | --- | --- |
| **768** × 900 | `mobile` | `flex` | fixed | 589 px（内联 `--map-h: 589px`） |
| **770** × 900（请求的 769 被模拟层报回 770） | `desktop` | `none` | `static` | 522 px（**无**内联 `--map-h`） |
| 1280 × 800（桌面三栏） | `desktop` | `none` | `static`，内联 `transform` 为 `none` | 464 px |

1280 × 800 那档还顺带确认了移动端压缩规则**没有漏到桌面**：`.layout` 网格是 `300px 540.8px 360px`、
`.sub span`（标语）回到 `inline`、`scrollWidth / clientWidth` 1265 / 1265 无横向滚动。

⚠️ 「770」这一档请求的是 769，DevTools 模拟层报回的 `innerWidth` 是 **770**（宽度取整），
**不是代码里的判断偏了**：`matchMedia("(max-width: 768px)")` 在 768 判真、770 判假，边界落在正确的两侧。

另外两条设计口径在这轮被证实：
- **开关是 body 属性而不是媒体查询**，所以「断点在哪」全项目只有 JS 里那个 768；
  768 档**没开 `isMobile` / `hasTouch`** 也照样进抽屉 —— 判定纯按宽度，不看 UA、不看触摸能力。
- **无 JS 就没有抽屉**，三块面板回到正常流堆叠（刻意降级）。CI 侧由
  `tests/test_web_smoke.py::test_no_js_path_keeps_the_panels_in_normal_flow` 钉住。

### 5. FR-107 联动：手机上点「一键体检」到底跑了什么

`#run-analysis` 的真实点击（375×667，`before: "readout"`）→ 完成后 **`data-sheet-open` 变成 `report`**、
`#report` `visible` / `translateY(0)` / `top 175 → bottom 615`、`aria-expanded` 只有 `report=true`。
不自动开的话，结果会写进一块收起态在屏幕外的固定层，「一键体检」在手机上看起来就是死的。

请求体只有 `{"minutes":15,"modes":["walking"],"platform":"mobile","lnglat":"116.4074,39.9042"}`
（**没有 `categories` 键** = 全选），响应 `meta.platform: "mobile"`、`meta.lightweight: true`、`meta.k: 6`、
`data_mode: "mock"` / `data_source: "sample"`，`score.total 44.65`。

📌 顺带澄清一处容易被读成 bug 的数字：全选态下 `score.weight_sum` 也是 **0.95**，
但**这不是类别子集造成的**——同一坐标不带 `platform`（桌面档 `k=12`、`lightweight: false`）跑一次，
`weight_sum` 同样是 0.95、总分同样 44.65。是这个 mock 样例本身有一个维度拿不到候选，
归一化提示如实写了 0.95。

### 6. 本轮抓出的两处缺陷（正文见 `docs/TEST_REPORT.md` §2.12）

1. **地图高度多扣了页脚与图例**，375×667 下只剩 ~240 px，「占满首屏」直接落空。
   改成只量 `.stage-area` 自己的上边界 + 把手条高；因为「上边界」会随横幅显隐变，重算点必须有
   **五处**：`hideBanner()`、`setBanner()`、`renderQuotaNotice()` 的两个出口，以及 `renderSheets()` 本身
   （`grep -n "sizeSheetStage()" web/isochrone.js` 可数）。少一处不会报错，只会在地图上留一条空白。
2. **把手条轻点能开不能关**（`moved < 6` 的轻点走到「按位移决定开合」的分支，位移 0 = 保持原样）。
   改成轻点按当前展开态取反。

### 7. 本轮**没有**验的（别当成已验）

1. **真机**上的抽屉手感、阻尼与 iOS Safari 的 `100vh` 地址栏抖动——本节全部是 Chrome DevTools 模拟。
   「拖得动」在模拟里成立，不等于手指好拖。
2. **双指缩放仍未实现**（代码只有 `wheel`），抽屉落地不改变这一条。
3. 过渡动画的**观感**（0.2 s `ease-out`）：本环境截图通道虽可用，但按「关于截图」一节的口径
   不提供 PNG；且上面的几何都是**落定后**复测的，过渡中间帧没有逐帧采样。
4. `prefers-reduced-motion: reduce` 下的关闭过渡只在 CSS 里写了规则，**没在模拟里切换该偏好实测**。

## FR-107 档位显式化 · `?profile=m\|d` 四例实测（2026-10-11，实现人自验，零出网）

FR-107 的后端两档（`desktop` K=12 / `mobile` K=6 + 跳过高成本推算）早就在，缺的是前端这半边：
档位**只能**由 `matchMedia("(max-width: 768px)")` 决定，评审无法在桌面上复现轻量档，用户也无从
看出自己拿到的是哪一档。本轮补 `?profile=m` / `?profile=d` 覆盖，并把档位写成报告头一枚按
**响应** `meta` 说话的徽标。

### 0. 前提与零出网自证

服务起在 `127.0.0.1:8796`，`GET /api/health` 回：

```
server_ak_type: not_configured   browser_ak_type: not_configured
data_mode: mock                  data_source: sample   data_mode_source: no_server_ak
```

也就是这台进程**没有 AK 可用**，出不了网——这不是「我叮嘱自己别出网」。四条浏览器用例的
`body.usage` 全 0，curl 那三条的响应头是 `x-http-requests: 0` / `x-place-calls: 0` / `x-quota-units: 0`。
留痕 `x-request-id`：浏览器对照例 `67ca281f75cf4af0`，curl `f27f8945bff7452c` / `bd00c4063e6a43e8` / `b8611fe920634172`。

### 1. 四例（DevTools 模拟视口；档位来源同时从「前端要发的请求体」和「真请求的 body」两处取证）

| # | 视口 | URL | 请求体 `platform` | 响应 `meta` | 徽标（`#profile-badge`） |
| --- | --- | --- | --- | --- | --- |
| A | 1280×800（`innerWidth=1280`，`body[data-sheet-mode="desktop"]`） | `?profile=m` | **`mobile`** | `lightweight=true`、`k=6` | 「简化模式（FR-107 · 实测前 K=6）」，`data-profile=mobile`，计算色 `rgb(169, 100, 10)` |
| B | 375×667（`isMobile,touch`，`data-sheet-mode="mobile"`） | `?profile=d` | **`desktop`** | `lightweight=false`、`k=12` | 「完整模式 · 实测前 K=12」，`rgb(107, 114, 128)` |
| C | 375×667 | `?profile=x`（非法值） | `mobile`（回落视口探测） | `k=6` | 「简化模式（FR-107 · 实测前 K=6）」，**console 0 条消息**、不抛错 |
| D（对照） | 1280×800 | 无 `profile` 参数 | `desktop` | `lightweight=false`、`k=12` | 「完整模式 · 实测前 K=12」 |

- A 证明「桌面也能手动进轻量档」，且抽屉那套 `data-sheet-mode` 完全没被牵连（仍是 `desktop`）。
- B 证明「手机也能手动要完整档」，同时 FR-106 的自动上浮照常：跑完 `body[data-sheet-open]="report"`，
  `#report` `175 → 615`（高 440 px）——和 FR-106 那节量的是同一组数。
- D 是必须有的对照组：没有 C/D 两例，就无法区分「URL 生效」和「URL 被忽略、一切照旧」。

网络层原文（对照例 D，`x-request-id: 67ca281f75cf4af0`）：

```
请求体  {"minutes":15,"modes":["walking"],"platform":"desktop","lnglat":"116.4074,39.9042"}
响应头  x-http-requests: 0   x-place-calls: 0   x-quota-units: 0   x-elapsed-ms: 20
响应 meta.platform="desktop"  lightweight=false  k=12  data_mode="mock"  data_source="sample"
```

### 2. 后端那半边绕开浏览器直接 curl 复核

`POST /api/analysis`（`sample_id=beijing-jiaodaokou`，`minutes=15`）：

| `platform` 入参 | HTTP | `meta.platform` | `lightweight` | `k` | `score.total` | grade | `x-request-id` |
| --- | ---: | --- | --- | ---: | ---: | --- | --- |
| `mobile` | 200 | `mobile` | `true` | 6 | **63.03** | C | `f27f8945bff7452c` |
| `desktop` | 200 | `desktop` | `false` | 12 | **67.8** | C | `bd00c4063e6a43e8` |
| `tablet` | 400 | — | — | — | — | — | `b8611fe920634172` |

`tablet` 的错误信封是 `code=invalid_poi_param`，`detail.allowed=["desktop","mobile"]`。

⚠️ **URL 与请求体是两套口径，这是设计不是疏漏**：`?profile=tablet` 在**前端**静默回落到视口探测，
`platform:"tablet"` 进 **API** 是硬 400。地址栏是用户手敲的，把一串错字符变成一屏红字太吓人；
编程调用则越早炸越好。两条路径都各测了一次，别拿其中一条去推另一条。

### 3. 顺手量出来的一个反直觉事实：两档**有时同分、有时差 4.77 分**

| 中心点 | `k=6` | `k=12` | 差 |
| --- | ---: | ---: | ---: |
| 天坛 `116.4074,39.9042`（浏览器默认） | 44.65（D） | 44.65（D） | **0** |
| 交道口样例 `beijing-jiaodaokou` | 63.03（C） | 67.8（C） | **4.77** |

所以「轻量档只是省调用、不影响结论」这句话**不成立**，不能写进任何面向评审的材料。徽标 title 里
那句「别把两档的分数横向比较」是有实测依据的，不是客套。
两例的 `score.weight_sum` 都是 **0.95**，与收窄勾选无关、与档位无关——和 FR-106 那节 §5 的结论一致
（样例本身有一个维度取不到候选，不是子集归一的 bug）。

### 4. 本轮**没有**验的（别当成已验）

1. **两档分差的分布**：只有两个中心点，一个差 0、一个差 4.77，凑不出期望，连「通常差多少」都说不了。
2. **`k=6` 到底省了多少调用**：PRD FR-107 的验收线是 `http_requests ≤ 5`、`quota_units ≤ 40`、
   `place_calls ≤ 8`、耗时 ≤ 20 s，而 mock/sample 层三栏计量恒为 **0**，本环境**量不出「省了多少」**，
   只能证明「没超标」这条在合成层平凡成立。真正的对照要在 `live` 下跑两档各一次，而那要烧真实配额。
3. **`cache` / `live` 两态下的徽标与降级横幅共存**：只验了 `sample`（`degraded=true`、`degraded_reason=mock_mode`）。
4. 用户自己在浏览器菜单里设缩放（非 DevTools 设备模拟）时 `matchMedia` 的结果——未测。
5. 徽标的**键盘可达性 / 朗读**：它是一枚纯展示 `<span>`，没加 `role` 或 `aria-label`，屏幕阅读器怎么念未验。

### 5. 顺带发现的一条桌面布局缺陷（本轮**未修**，只登记）

为了量徽标的位置才发现：1280×800 下 `.layout` 是三列（`300px 540.8px 360px`），而 `#report` 是
第 4 个自动放置的栅格子项，掉到**第 2 行第 1 列**，宽度被 `minmax(260px, 300px)` 钉在 300 px。实测后果：

| 量的东西 | 实测 | 本该 |
| --- | ---: | ---: |
| `.table-wrap` `clientWidth / scrollWidth` | 270 / **600** | 桌面机上九列 POI 表不该横向滚动 |
| 首个数据行高度 | **79 px** | 单元格逐列折行 |
| `#report` 高度 / 整页高度 | **7341 px / 12552 px** | 同一行右侧 541 px 的地图列是空的 |
| 雷达 / 柱状 svg | 280×280 / 280×140 | 柱状图 520×260 的 viewBox 被等比压扁 |

`git show f24a174:web/isochrone.css | grep -c grid-column` 与 `git show 7cee8f8:...` 都是 **0**，
`git show f24a174:web/isochrone.html` 里 `.layout` 的子项顺序也已经是
`controls / stage-wrap / readout / report`——**这条从初始提交就在，与 FR-106 / FR-107 无关**。没在本轮修，是因为它不是一行：把 `#report` 撑成全宽之后
`.chart { width: 100% }` 没有 `max-width`，雷达会涨到 ~600×600，得连 `.chart-box` 一起定口径，
再重测 1280 / 900（单列堆叠）/ 375（抽屉）三档。登记在 `docs/TEST_REPORT.md` §6 的未修项里。

## FR-103 中心点地址回显 · 零出网五例实测（2026-10-11，实现人自验，零出网）

FR-103 要的是「面板上不能只有一串坐标」。后端 `/api/regeo` 早就在（README §5 接口表与 §5.1 口径），缺的是**前端这一跳**：
`web/isochrone.js` 从没调用过它，`setCenter()` 只把 `116.40740, 39.90420（BD09LL）` 写进 `#center-readout`。
本轮新增 `#center-address` 一行，字段**只取自响应**——PRD §4.7 末尾专门禁止把 `formatted_address`
切成行政区/街道，这条写成了反证测试（`tests/test_geo_api.py::test_address_parts_never_come_from_splitting_formatted_address`）。
取不到就照实说取不到：空态与失败态都**不阻断**等时圈与地图平移。

> 计数口径：README §10.7 与 `docs/TEST_REPORT.md` §6 把这次实测记作「**四例 + 一条打桩成功态**」，
> 指的就是下面这张表的 A/B/D/E 与 C——**同一轮实测，不是两次实验**。

### 0. 前提与零出网自证

服务用空 AK 环境变量起在 `127.0.0.1:8801`（`BAIDU_SERVER_AK= BAIDU_BROWSER_AK= uvicorn api.app:app`），
`GET /api/health` 回 `server_ak_type=not_configured`、`data_mode=mock`、`baidu_reachable=False`。

绕开浏览器先 curl 一次，拿到空态那条文案的**原文**：

```
GET /api/regeo?lnglat=116.404,39.915&data_mode=mock      → 200
{ok: false, empty: true, degraded: true, degraded_reason: "mock_mode", data_source: "sample",
 meta.sample_layer: "unavailable", meta.data_mode: "mock",
 usage: {place_calls:0, quota_units:0, http_requests:0, attempts:0, cache_hits:0},
 request_id: "23814f90dfbf4256"}
"formatted_address" in body  →  False        ← 响应里根本没有这个键
```

最后一行是这条需求最硬的一条自证：**响应没给的东西，前端不可能「切」出来**——所以浏览器里那句
地址文案只可能是从 `message` 原样回显的，不是拼出来的。

### 1. 五例（A/B 打真实服务，C/D/E 把 `window.fetch` 换成桩）

| # | 中心点 | 取数方式 | `data-state` | 回显（DOM 实测文本） |
| --- | --- | --- | --- | --- |
| A 首屏 | `116.40740,39.90420` | **真实服务**（mock 层） | `empty` | 「中心点地址：当前为离线（mock）模式，不发起实时调用；如需实时数据请配置服务端 AK。 该查询暂无可用的合成示例数据；前端按上面的提示继续流程即可，不算失败。」+ 一枚 chip「取数层：程序合成示例」；`display:flex`、`getClientRects().length>0`、高 108 px；`state.address = {pending:"", seq:1, cache:1}` || B 重复同点 | 同 A | 真实服务 | `empty` | **没发出第二个请求**：`cache.size 1 → 1`、`seq` 仍 **1**、`pending` 复位 `""`；`#run` 未 disabled |
| C 成功态 | `116.41020,39.91420` | 🔴 `window.fetch` **打桩**（live 形状） | `ok` | `<b>`「中心点地址：北京市东城区交道口街道北总兵胡同3号」，五枚 `.address-part`：`东城区` / `交道口街道` / `北总兵胡同3号` / `110101` / `取数层：实时` |
| D 世代号 | `116.42000,39.93000`（慢 260 ms）→ `116.43000,39.94000`（快 10 ms） | 打桩 | `ok` | 留在页面上的是**新点**「中心点地址：新的点（快响应，应该留下）东城区东华门街道110101取数层：实时」，慢的那份被丢弃；`seq=4`、`cache.size=4` |
| E 失败态 | `116.44000,39.95000` | 打桩 `throw TypeError("Failed to fetch")` | `error` | 「中心点地址：没取到（网络错误，请检查连接或稍后重试）。不影响等时圈与体检。」计算色 `rgb(169, 100, 10)`（= `--warn`）；`#run.disabled=false`、降级横幅「当前处于离线模式…」文案仍在 |

A 这一跳的网络层原文（`reqid=7`，浏览器 Network 面板）：

```
GET http://127.0.0.1:8801/api/regeo?lnglat=116.4074%2C39.9042   [200]
响应头 content-length:675  x-elapsed-ms:1
        x-http-requests:0  x-place-calls:0  x-quota-units:0  x-request-id:33437b90d2c548b5
响应体 {ok:false, service:"regeo", data_source:"sample", coordinate_system:"bd09ll",
        empty:true, degraded:true, degraded_reason:"mock_mode", usage:{全 0},
        meta.params:{location:"39.904200,116.407400", coordtype:"bd09ll"}, ...}
```

- 出站是 **`纬度,经度`**（`location=39.904200,116.407400`），回显给用户的 `lnglat` 是**经度在前** —— 两个顺序
  各测一次，别拿一个去推另一个（`tests/test_geo_api.py::test_request_sends_lat_first_while_the_response_keeps_lng_first`）。
- 整个页面这轮只发了 **3 条请求**：`/api/regeo`(7)、`/api/health`(8)、`/api/samples`(9)。
  `/api/regeo` 只有 1 条，就是 B 例「同点不重复问」的直接证据。
- 请求 URL 里**没有** `data_mode` 参数：`#data-mode` 未选时前端不带这个键，由服务端运行时解析（与 `/api/geocode` 同口径）。

### 2. 三条配额守卫是量出来的，不是「代码里写了」

逆地理这一路是 **0.5 QPS / 300 次/日**，比等时圈那一路紧得多，所以 `state.address` 上加了三个键：

| 守卫 | 挡的事故 | 本轮实测 |
| --- | --- | --- |
| `cache`（键取 **5 位小数**，与回显精度一致） | 同一位置反复取点每次都问一次百度 | B 例 `cache.size 1→1`，Network 里 `/api/regeo` 恒为 1 条 |
| `pending`（同 key 在飞直接返回） | 连点两次把同一坐标发成两个并发请求、双份占桶 | B 例结束后 `pending` 回到 `""`，没留下卡死的锁 |
| `seq`（在飞响应的世代号） | 换点时**旧**地址盖到**新**点上——面板说东单、地图在天坛 | D 例慢响应被丢，`seq` 递增到 4 |

⚠️ 这三条在零出网下只能证明「**前端不多问**」，证明不了百度侧计费按预期。真实 `live` 的配额对照本轮没跑（§4 项 1）。

### 3. 几何：375×667 抽屉（FR-106 同口径）与桌面 1280×800

| 量的东西 | 375×667 空态 | 375×667 成功态 | 1280×800 空态 |
| --- | ---: | ---: | ---: |
| `#center-address` | **294×108** px | **294×112** px | 249×108 px（所在 `#controls` 列 300 px 宽） |
| 子节点换行行数 | 1 枚 chip | **4 行**（基线 top = 0 / 25 / 56 / 87） | 2 行 |
| `display` / `font-size` | `flex` / 12px | 同 | 同 |
| `documentElement.scrollWidth` | **360** ≤ `innerWidth` 375 | **360** | 1265 ≤ 1280 |
| 与下一个元素（`fieldset`）的间距 / 重叠 | 10 px / `false` | — | 10 px / `false` |
| chip 宽度 | 136 px | 91 / 103 / 121 / 97 / 112 px，**全部 ≤ 294** | — |

成功态那 6 个子节点的 `top` 落在 **4 个不同基线**上，这才是 `flex-wrap` 真的在换行、而不是把行撑到溢出的证据；
只测高度（112 px）分不清这两种情况。移动端抽屉里地址行一开始在可视区外（`#controls` 内容高、面板 `scrollTop≠0`），
`scrollIntoView` 之后才量到——**不是**被藏起来了（`getClientRects().length>0`、`display:flex`）。

顺带一个观察、本轮**没改**：空态文案是后端原文（`len()` 实测 74 字，含两段提示），在 249 px 宽的列里要占满 108 px 高、
比坐标那行重得多。要不要截断留到评审看真实 `live` 下的观感再定。

### 4. 本轮**没有**验的（别当成已验）

1. 🔴 **真实 `live` 的一跳**：要烧掉 300 次/日逆地理配额里的 1 次，按配额纪律**等用户点头**。
   所以**成功态（C 例）与失败态（E 例）都是 `window.fetch` 打桩**，桩的字段清单抄自
   `services/geo.py::_parse_regeo`，**不是**抓包来的真实响应。后端 live 分支的形状由
   `tests/test_geo_api.py`（16 例，真实 `BaiduClient` + `httpx.MockTransport`）钉住，但那是合成传输层，不等于线上。
2. **`data_source="cache"` 时地址行的取数层 chip**：浏览器里没出现过（只跑了 `sample` 空态与打桩 `live`）。
   服务端命中缓存这条路径由 `tests/test_geo_api.py::test_second_identical_regeo_is_served_from_cache` 覆盖
   （第二次调用 `http_requests` 不再增长、`usage.cache_hits=1`、`data_source="cache"`）。
3. **`pending`（解析中）态没被拍到**：本地服务端 1 ms 返回，文案一闪而过没来得及采样；而且它**没有专门的
   CSS 样式**（只有 base 色，`isochrone.css` 里只给 `error` 着了 `--warn`），所以这一态目前只由
   `tests/test_web_smoke.py::test_center_address_is_readable_in_every_state` 的四个态断言守着。
4. **JSAPI 实时底图叠加下的地址行**：本轮在无底图环境（mock、无浏览器 AK）测。
5. **屏幕阅读器朗读 / 语义**：`#center-address` 是纯展示 `<p>`，没加 `aria-live`，「解析中 → 成功」的变化
   朗读不出来；未验也未改。
6. **真实 `live` 下字段完整度**：`town` / `street_number` 在部分地址上会缺失，缺失时的 chip 数量本轮只由
   「渲染前逐字段过 `if`」这条实现与上面那条反证测试保证，没有真实样本。

### 5. 收尾

- 本轮起的两个本地服务都已停：`uvicorn api.app:app --port 8801`（`pid=18960`，`taskkill` 成功）、
  以及为复跑 `map_layers_smoke` 而再起的 `--port 8802`。`netstat -ano | grep -c '880[0-9].*LISTENING'` 两次实测都是 **0**。
- 临时文件全部删除：`.t103-health.json`、`.t103-regeo.json`、`.t103-server.log`、`.t103b.log`、`_map_input.json`、
  两条 node 冒烟的输出 `.smoke1.txt` / `.smoke2.txt`。`git status --short` 只剩本轮 7 个预期改动文件。
- chrome-devtools 设备模拟从 `375x667x2,mobile,touch` 复位回 `1280x800x1`。

## FR-108 地址联想 · 零出网七例实测（2026-10-11，实现人自验，零出网）

PRD FR-108 原文：「地址输入框支持中文搜索联想，走 `/api/suggest`（内部 `/place/v2/suggestion`），
**不复用 `/api/geocode`**；选中后地图 flyTo 并落点」，门槛四条：
「输入 ≥ 3 字 + 防抖 500 ms + 结果本地缓存 + **默认关闭、可手动开启**」。
这一轮验的是**四条门槛在浏览器里真的成立**，以及「默认关闭」这一半到底是谁把住的。

### 0. 前提与零出网自证

- 服务：`BAIDU_SERVER_AK= BAIDU_BROWSER_AK= ./.venv/Scripts/python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8803`
  （`pid=10500`）。两个 AK 用**空环境变量**压住：pydantic-settings 的环境变量优先级高于 `.env`，
  本机 `.env` 里有真实 AK，不显式压住就会**真的**打百度线上服务。
- `GET /api/health` 实测：`status=offline`、`ok=true`、`baidu_reachable=false`、
  `server_ak_type=not_configured`、`browser_ak_type=not_configured`、`services_enabled.*` 六项全 `false`。
- 后端侧先 curl 一条（`GET /api/suggest?keyword=温特豪德&region=北京`）：HTTP **200**、
  `request_id 786098ff106f4f6f`、`x-http-requests: 0`、`x-place-calls: 0`、`x-quota-units: 0`、
  `content-length: 663`，body `ok=false / degraded_reason=mock_mode / data_source=sample / usage.* 全 0`。
  **200 而不是 4xx** 是 FR-403 的口径：关闭与降级都是**结论**，不是异常。
- 浏览器侧的零出网证据是**每条 `/api/suggest` 响应头里的 `x-http-requests: 0`**（§1 的 E 例逐字贴出），
  加上「A/B/C/D 四例 Network 面板里根本没有 suggest 请求」这件事本身。

### 1. 七例（A–E 打真实服务，F/G 把 `window.fetch` 换成返回真实响应形状的桩）

页面 `http://127.0.0.1:8803/isochrone.html`，桌面视口起步，G 例之后切 375×667。
「请求数」一列是页内 `fetch` 包装器记下的**这条链上**的计数，与 DevTools Network 面板对得上。

| # | 操作 | 实测 | 判定 |
| --- | --- | --- | --- |
| **A** | 首屏加载后什么都不做 | `#suggest-on.checked=false`、`#suggest-list` `data-state="none"`、`#addr` `aria-expanded="false"`、列表 `display:none` 且 `0×0` | ✅ 默认关闭：连渲染都是收起的 |
| **B** | 开关**不勾**，地址填「温特豪德」、城市填「北京」，等 900 ms（> 防抖 500 ms） | `data-state="none"`、`children.length=0`、**suggest 请求数 0** | ✅ 「默认关闭」是**前端**把住的：不是发出去被后端拒，而是根本没发 |
| **C** | 勾上开关，地址只留 2 字「温特」 | `data-state="short"`、`aria-expanded="true"`、一行 `.suggest-note`：「再输入几个字（当前 2 字，门槛 3 字）：太短的关键字召回一大片，白烧一次检索配额。」、**请求数 0** | ✅ ≥3 字门槛在本地生效且文案说清了为什么 |
| **D** | 地址 3 字「温特豪德」、**城市清空** | `data-state="need_region"`、note「联想需要城市：官方把 region 列为必选参数。请在上面的「城市」里填一个，如「北京」。」、**请求数 0** | ✅ `region` 官方必选，不把它留给线上 `status=2` |
| **E** | 城市「北京」+ 地址「温特豪」 | **1 条真实请求** `GET /api/suggest?keyword=%E6%B8%A9%E7%89%B9%E8%B1%AA&region=%E5%8C%97%E4%BA%AC` → `reqid=10`、`200`、`x-elapsed-ms: 2`、`x-http-requests: 0`、`x-place-calls: 0`、`x-quota-units: 0`、`x-request-id: 81a1cc89dac349ed`、`content-length: 660`；列表 `data-state="empty"`，原样铺后端 `message`（「当前为离线（mock）模式…该查询暂无可用的合成示例数据…」） | ✅ 门槛齐了才真问；`mock` 下**没有候选但不报错** |
| **F** | `window.fetch` 换成桩：返回 4 条候选，其中**一条没有 `location`** | 渲染 **3** 行（无坐标那条被 `pair(item.lnglat)` 丢掉）、每行 `role="option"`、`aria-selected="false"`、文案 = 名称 + `市 区` + `116.45200, 39.91050`；桩只被调 **1** 次 | ✅ 候选只来自响应，且**每一行都能落点** |
| **G** | 键盘 + 点选 + 缓存 + 世代号 + 错误态（见 §2） | 全部符合预期 | ✅ |

整页请求数在 E 例之后是 **4 条**：`reqid=7 /api/regeo`、`8 /api/health`、`9 /api/samples`、`10 /api/suggest`。
也就是说这一轮**没有多出一条**出网请求，`x-http-requests: 0` 与 §0 的 curl 同口径。

### 2. G 例拆开看：五件事各自有独立证据

| 子项 | 操作 | 实测 |
| --- | --- | --- |
| 键盘四键 | 候选 3 行时连按 ArrowDown / ArrowDown / ArrowUp | `aria-selected` 依次 `[true,false,false]` → `[false,true,false]` → `[true,false,false]` |
| Enter 的归属 | 有选中行时按 Enter | 落点：`#lng=116.452000`、`#lat=39.910500`，状态行「中心点：116.45200, 39.91050（BD09LL）｜ 联想选中「国贸商城」」，列表回 `none`、`aria-expanded=false`，**没有再问一次 suggest**，顺带发了 1 条 `/api/regeo`（FR-103 那半边接着工作） |
| Enter 不被吞 | 列表空、无选中行时按 Enter | `GET /api/geocode?address=望京花园&city=北京` → 200，状态行「地址搜索未完成：当前为离线（mock）模式…。已保留原中心点。」——**FR-101 方式②没有被联想接管** |
| 点选（鼠标路径） | 在第 3 行派发 `mousedown` + `click` | `mousedown` 的 `dispatchEvent` 返回 **false**（监听器确实 `preventDefault()` 了，输入框不失焦）；落点 `116.461400, 39.908600`，地址框回填「国贸地铁站」，suggest 计数 **0 增量** |
| 缓存与键 | 同词同城市再打一次 → 再把城市改成「上海」 | 第一次：**3 行候选来自前端缓存，桩调用数仍是 1**（不再发）；换城市：桩调用数 → **2**，URL 变成 `region=%E4%B8%8A%E6%B5%B7`。缓存键确实是 `${region}|${keyword}` |
| 世代号 | 慢响应（900 ms）在飞时改关键字触发快响应 | 时间轴：慢请求 `+513 ms` 发出、快请求 `+1209 ms` 发出并立即返回、慢响应 `+1413 ms` 到达 → **被丢弃**，最终列表只有「新鲜候选乙」，`data-state="ok"` |
| 错误态 | 桩回 503 + 错误信封 | `data-state="error"`、note「联想未完成：上游暂时不可用（模拟）。地址搜索与坐标取点不受影响。」、`.suggest-note` 计算色 **`rgb(169, 100, 10)`**（与 FR-103 地址错误同色）；按 Escape → `none\|false` |
| 关掉开关 | 取消勾选后再打字 | `data-state="none"`、`children.length=0`、之后的打字**桩调用 0** |

整轮 console **零消息**（`list_console_messages` 返回 `<no console messages found>`）。

### 3. 几何：375×667 抽屉（FR-106 同口径）与桌面 1540×732

| 视口 | 指标 | 实测 |
| --- | --- | --- |
| 桌面 1540×732 | 列表 / 三行高 / 与下一块间距 | `249×178`，行高 `59 / 59 / 58`，`gapToNext 12`，`documentElement.scrollWidth 1525 < 1540` |
| 桌面（真实流程：先把输入框滚进视口再打字） | 页面是否被弹层顶走 | `window.scrollY` 前后都是 **386**（`jump 0`），列表 `top 535 / bottom 714` 本来就在视口内 |
| 移动 375×667（`deviceScaleFactor 3`、`hasTouch`） | 候选行热区 | 高 `44 / 66 / 44`，`min-height: 44px`、`font-size: 14px`、`align-items: center`（PRD §6.3 的 44 px 达标） |
| 移动 | 横向溢出 | `documentElement.scrollWidth = 375 = innerWidth`，**没撑出横向滚动** |
| 移动 | 列表是否看得见 | **修前**：`#controls` 是滚动容器（`clientHeight 440` / `scrollHeight 2653`），`#addr` 在 `top 568`，列表在 `top 742 / bottom 897`——**整块在 667 视口之外**；**修后**：`#controls.scrollTop` `0 → 282`，列表 `top 460 / bottom 615` 完整可见 |

那一处「修后」是本轮抓出的唯一缺陷，正文见 [`docs/TEST_REPORT.md`](../TEST_REPORT.md) §2.14：
候选列表是**文档流**里的 `<ul>` 而不是浮层，所以在移动端抽屉里必须自己 `scrollIntoView({block:"nearest"})`。
同一句在桌面是空操作（上表第二行），所以它不是「为了移动端把桌面搞抖」的补丁。

### 4. 本轮**没有**验的（别当成已验）

1. 🔴 **`/place/v2/suggestion` 的真实返回体形状从未核对过**。`docs/BAIDU_API_NOTES.md` §2.4 那一节
   **只记了请求参数、没记响应字段**，`_parse_suggest` 里的 `name` / `location` / `city` / `district` /
   `adcode` / `province` 是按同类接口推的，`tests/test_geo_api.py` 用的是**自造桩件**。
   对不上时的表现是「列表永远空」这种安静失败。核对要烧「地点输入提示与检索**共用** 100 次/日」池子里的 1 次，
   **等用户点头才跑**。
2. **F/G 两例的候选列表是 `window.fetch` 打桩量的**，不是真实响应。E 例那条真请求只证明了
   「门槛齐了才发、发出后 200、`x-http-requests: 0`、后端 message 能铺出来」。
3. **`flyTo` 没在真实底图上跑过**：本机这轮把 `BAIDU_BROWSER_AK` 压成空，JSAPI 起不来，
   `setCenter()` 走的是 `panTo` 退路（`typeof state.jsapi.map.flyTo === "function"` 守卫由
   `tests/test_web_smoke.py` 断言它在，但**没验过真飞**）。
4. **中文输入法（IME）组合输入没测**：CDP 走的是直接改 `value` + 派发 `input`，
   真实拼音输入会在 composition 期间连续触发 `input`。防抖 500 ms 挡住了大部分，
   但「composition 中每键都算一次输入」这件事未在浏览器里量过。
5. `data_source="cache"` 这一态在浏览器里**一次都没出现**（本地 mock 后端拿不到缓存态），
   只有 `tests/test_geo_api.py::test_second_identical_suggest_is_served_from_cache` 与
   `..._switching_the_flag_off_keeps_the_cache_readable` 两条接口级用例覆盖。
6. 触摸是真手指还是 CDP 合成？——**CDP 合成**（`isMobile` + `hasTouch` 模拟），惯性、长按、
   软键盘顶起视口这三项未测。

### 5. 收尾

- 本轮起的 `uvicorn api.app:app --port 8803`（`pid=10500`）已 `taskkill`，
  `netstat -ano | grep -c '880[0-9].*LISTENING'` 实测 **0**。
- 临时文件全部删除：`.fr108-scratch/`（curl 输出与两条 node 冒烟的日志）、`_map_input.json`（96 784 字节，
  为 FR-108 改过 `isochrone.js` 后复跑 `map_layers_smoke` 而生成，12 项 `exit=0`）。
- chrome-devtools 设备模拟：本轮从 `1540x732x1` → `375x667x3,mobile,touch` → 回到 `1280x800x1`。

## FR-413 合规页脚 · 零出网实测（2026-10-11，实现人自验）

PRD FR-413（**P0**）要求页脚同时落三件事：缓存性质与 TTL 声明（条款 3.3.4）、合成示例数据说明、
**百度版权与审图号**（条款 6.6）+ **显著披露与隐私政策链接**（条款 7.3）。
这一轮验的是**页面上真长出这三行了**，以及两串写死的官方字符分别会怎么过期。

### 0. 前提与零出网自证

- 服务：`BAIDU_SERVER_AK= BAIDU_BROWSER_AK= ./.venv/Scripts/python.exe -m uvicorn api.app:app --port 8804`
  （两个 AK 用**空环境变量**压住，`.env` 里那对真实 AK 优先级低于显式环境变量）。
  ⚠️ 别被任务管理器骗到：`Win32_Process` 对这条进程报的是 `D:\Anaconda\python.exe`——
  `.venv` 是从 Anaconda 的 3.11.4 建的（`pyvenv.cfg` 里 `home = D:\Anaconda`、
  `include-system-site-packages = false`），Windows 的 venv 启动器会把**宿主**二进制路径写进进程表，
  而 `sys.prefix` 仍是 `.venv`。所以「门禁要用 `.venv`、直接敲 `pytest` 会撞到 Anaconda 那边没装
  `fastapi`」那条口径依然成立，只是排查进程时不能只看 `ExecutablePath`。
- `GET /api/health` 本轮复测：`status=offline`、`ok=true`、`data_mode=mock`、`baidu_reachable=false`、
  `server_ak_type=not_configured`、`browser_ak_type=not_configured`、`services_enabled` **6/6 全 false**。
- 后端一条 `POST /api/analysis`（`{"lnglat":[116.407,39.904],"mode":"walking","minutes":15}`）：
  HTTP **200**、`x-request-id: f51daf48676a4795`、`x-http-requests: 0`、`x-place-calls: 0`、
  `usage.elapsed_ms=15`、`usage.http_requests=0`、`usage.place_calls=0`。
- 🔴 **这一节里唯一碰真实网络的是 §4**（百度官方公开文档页），它不调用任何带 AK 的接口、不占配额；
  页面渲染与 `request_id` 那几条全部在 8804 的 mock 服务上跑。

### 1. 页脚三行的几何（桌面 1280×800 / 移动 375×667×3）

| 量 | 桌面 `data-sheet-mode=desktop` | 移动 `data-sheet-mode=mobile` |
| --- | ---: | ---: |
| `.page-foot` 尺寸 | **1265 × 126** px | **375 × 366** px |
| `.foot-links` / `.foot-legal` / `.foot-review` 段高 | 22 / 42 / 21 | 137 / 146 / 42 |
| 五条 `<a>` 热区高 | 22 px ×5 | **44 px ×5**（FR-106 的 44 px 规则覆盖到页脚） |
| `.stage-area` | 三栏同屏，不走抽屉 | **331 × 337** |
| `documentElement.scrollWidth - innerWidth` | **−15**（无横向滚动） | **0** |

移动那张表里段高从 22/42/21 涨到 137/146/42 是**预期**而不是缺陷：375 宽下每条链接独占 44 px 行高、
两段声明按字换行。代价是页脚本身占到 366 px（视口的 55 %），所以它必须留在抽屉**外面**——
`.stage-area` 由 JS 量「视口 − stage-area 上边界 − 把手条」，本轮实测 331×337 说明地图没被页脚挤掉。

### 2. 对比度：`--muted` 的 0.01 余量（改前 4.51，改后 7.05）

先确认比对对象不是猜的，浏览器里 `.foot-legal` 的 computed 值：

```
color      rgb(75, 85, 99)   → #4b5563（改后）
向上第一个非透明背景 rgb(246, 247, 249) → #f6f7f9 = `--bg`
font-size  13px              → AA 正常字号，硬线 4.5:1
```

再用仓库自己的 `wcag_ratio()`（`tests/test_web_smoke.py`）对**本项目调色板的五个真实底色**逐个量：

| 底色 | 变量 | `--muted` #6b7280 | 页脚现用 #4b5563 |
| --- | --- | ---: | ---: |
| `#ffffff` | `--card` | 4.83 | 7.56 |
| `#fff7e6` | `--warn-bg` | 4.54 | 7.09 |
| `#f6f7f9` | `--bg`（页脚实际所在） | **4.51** | **7.05** |
| `#eaf6f0` | `--ok-bg` | 🔴 **4.36** 不过线 | 6.82 |
| `#e3e6ec` | `--line` | 🔴 **3.87** 不过线 | 6.05 |

🔴 **诚实记录一个我自己的错**：上一轮我把「#6b7280 对 #f6f7f9」写成 **4.49:1** 并抄进了
README §9.7、`web/app.css` 注释和测试 docstring 三处，本轮重算是 **4.51**——
也就是**刚好过线 0.01**，不是「差 0.01 不过」。三处已改成真值，
`docs/TEST_REPORT.md` §2.16 记了这件事。顺带说明**这条测试拦不住旧配色**（4.51 ≥ 4.5 会照样绿），
它拦的是以后换成更浅的灰；真正支撑这次改动的是上面那张五底色表。

### 3. `request_id`：一次体检里页脚从 `—` 变成号（§2.15 的现场复现）

信封形状实测（同一条 mock 响应）：顶层 13 个键里**有** `request_id`，
嵌给地图的 `isochrone` 子对象 19 个键里**没有**（`'request_id' in payload.isochrone` → `false`）。
旧代码 `git show HEAD:web/isochrone.js` 第 717 / 725 行只读 `result.request_id`，
所以主路径必然空。三步 A/B/C 在同一个页面、同一份响应上复现并验证（`request_id = 14e8a8fd085b4924`）：

| 步 | 操作 | 页脚 `#request-id-foot` |
| --- | --- | --- |
| A | `setRequestId("")` → `adoptIsochrone(payload.isochrone, {force:true})` | `request_id：—` ← **旧行为** |
| B | `setRequestId(payload.request_id)`（与响应头 `x-request-id` 同值） | `request_id：14e8a8fd085b4924` |
| C | `setOverlays(...)` 触发**第二次** `renderAll()` | 仍是 `14e8a8fd085b4924` ← 兜底生效，不再被重写 |

真实 UI（点「一键体检」按钮，不是打桩）两档各跑一次，页脚与「结果元信息」`dt=request_id` 那行**同值**：

- 桌面 1280×800：`request_id：dfe369002be847a6`
- 移动 375×667×3：`request_id：d398a2bfe9fc44d7`

⚠️ 第一版修法是**把 `setRequestId()` 放在 `adoptIsochrone` 之后**，实测页脚仍然 `—`——
一次体检会跑两轮 `renderAll()`（`adoptIsochrone` 与 `setOverlays` 各一轮），靠调用顺序抢写文本不可靠。
最终修法是把号存进 `state.requestId`、取号统一走 `currentRequestId(result)`。

存进 `state` 就带来一个新风险：**兜底可能报一个过期的号**（比报 `—` 更糟，它对不上服务端日志里
这一次请求）。所以 `/api/isochrone` 那条路径也必须刷同一个格子，并且**过期响应不许刷**
（沿用 `runSeq` 的世代判定）。三条按真实按钮跑完再量（`reqid=200/201/202`，全在 `127.0.0.1`，零出网）：

| 操作（点的是页面上的按钮，不是打桩） | 页脚 |
| --- | --- |
| 「开始分析」（`POST /api/isochrone`） | `request_id：9034cf7e71fe4942` |
| 「一键体检」（`POST /api/analysis`） | `request_id：4334bb27c8c04fe3` ← 顶层号交接成功 |
| 再点一次「开始分析」 | `request_id：cc527b798c894024`、`dt=request_id` 那行同值 ← **没有停在上一份体检的号上** |

### 4. 官方那两串字符：2026-10-11 逐字复核（公开页面，不占配额）

- `https://lbsyun.baidu.com/` 首页页脚 DOM `innerText` 实取：
  **`地图审图号：GS(2025)4125|测绘资质：甲测资字11111342`**（与 2026-10-04 首次读取一致），
  同一页脚另有 `京ICP证030173号`。
- 页脚外链两条都**照着官方页脚自己的 `<a>` 抄**，本轮实访确认重定向目标：
  `…/index.php?title=openprivacy` → `/docs/pcsa?title=compliance/openprivacy`，
  页面自注 **`更新时间: 2026/08/24 10:04`**；
  `…/index.php?title=open/law` → `/docs/pcsa?title=law/open/law`，自注 **`更新时间: 2026/08/05 19:39`**。
- 条款 **6.6 原文**逐字可读（就在服务条款页里）：「…您**不得以任何方式进行删除、掩藏、修改或替换**
  本服务及相关内容中所附的或包含的任何专有权利声明…其他任何链接或公告。」

🔴 **这一条自动化做不到**：审图号只存在于官方 HTML 页脚，没有机器可读真值，百度按年换号。
测试能验「页面上有这一行」，验不了「这一行还是不是官方那个号」。
所以 `.foot-review` 带**读取日期**，并把**季度人工复核**登记为唯一兜底（README §10.7 / DESIGN §6.5）。
与之相对，TTL 那句「24 h / 7 天 / 30 天」由 `test_compliance_footer_ttl_matches_config`
对着 `Settings.model_fields` 的**类默认值**逐项比对——改配置不改页脚就红。

### 5. 本轮**没有**验的（别当成已验）

1. **审图号会过期**：见 §4，只能靠人工。
2. **FR-305 的 PNG 导出仍不存在**，而且「live 底图能不能 `toDataURL()`」这件事**只有代码依据、没有实测**：
   JSAPI 把瓦片画进自己的跨域 `<canvas>`，按规范应抛 `SecurityError`，但验证它要打开 live 底图
   一次（烧 JSAPI 浏览器端配额），按配额纪律等点头。设计口径已预先收口为
   「**矢量报告卡 + 标识文字带**」（README §10.7 / DESIGN §6.4）。
3. **真机**：iOS Safari / Android 上页脚那段 13 px 小字的实际可读性、以及 §1 里 44 px 热区在真手指下的表现。
4. **首页不放审图号**这个决定：`web/index.html` 不加载地图，只挂 `.foot-legal` 与两条外链；
   「无地图的页面要不要标审图号」官方没写，本项目按「不需要，但把地图页的指向写清楚」处理，属**自主口径**。

### 6. 收尾

- 门禁（全部**连着退出码**复核，2026-10-11 本轮）：`pytest -q` **501 passed**，对最终代码状态连续五次
  `exit=0`，5.33 / 5.35 / 5.40 / 5.34 / 5.35 s；`ruff check .` `exit=0`；
  `black --check .`（49 个文件）**首跑 `exit=1`** 两次——一次是上一轮遗留的引号风格
  （`test_web_smoke.py` 一处 `'...'` 该写成 `"..."`），一次是本轮新加的那条 `assert ... , (msg)`
  换行形态；按 black 的写法改完后 `49 files would be left unchanged`、`exit=0`；
  `node --check web/isochrone.js` / `web/report.js` 各 `exit=0`；
  `node tools/report_dom_smoke.mjs | grep -c 'OK '` = **15**、`exit=0`；
  `node tools/map_layers_smoke.mjs _map_input.json | grep -c 'OK '` = **12**、`exit=0`
  （两条都跑了两遍：一遍用本轮早些时候按另一个中心点生成的 **57 901** 字节桩输入，
  一遍用 §7 第 5b 条那条命令重新生成的 **96 784** 字节桩输入，**断言数都是 12**——
  文件大小随请求变，别把它当回归信号）。
- DOM 计数三个都没变：`report.js` 取 **26** 个 id、`isochrone.js` **62** 个、`isochrone.html` 去重 **96** 个
  （FR-413 复用了已有的 `#request-id-foot`，没新增 `getElementById`）。
- 本轮起的 `uvicorn --port 8804` 已 `Stop-Process`，`netstat -ano | grep -c ':8804.*LISTENING'` 实测 **0**；
  `.fr113-health.json` / `.fr113-analysis.json` / `.fr113-headers.txt` / `.fr413-verify.log` /
  `_map_input.json` 全部删除；chrome-devtools 设备模拟回到 `1280x800x1`，两个百度官方页面标签已关闭 / 重置为 `about:blank`。

### 7. 提交之后：推送通道与 CI 红灯（同日）

- **推送**：`git push origin main`（`https://github.com/zys1101/CircleDoc.git`）走 HTTPS **不通**——
  `github.com` 能解析（`nslookup` 回 `20.205.243.166`），但 443 直连一次 `Connection was reset`、
  一次 `Failed to connect … port 443 after 21034 ms: Timed out`；同一时刻
  `curl https://api.github.com/zen` 回 **200**，所以不是全站不通。本机代理 `127.0.0.1:7897`
  **当时没在监听**（`netstat` 实测 `0`，只有无关的 `8888`）。
  改走 GitHub 官方的 **SSH over 443**：`~/.ssh/github_ed25519` 对 `ssh.github.com:443` 认证返回
  `Hi zys1101! You've successfully authenticated`，于是把 `~/.ssh/config` 的 `Host github.com`
  覆写成 `HostName ssh.github.com` + `Port 443`，`git push ssh://git@github.com:443/…` 成功
  （`5841857..a2d7d8a main -> main`，`exit=0`；随后 `git fetch` 同一 URL 把 `origin/main` 对齐，
  `git rev-list --left-right --count origin/main...HEAD` = **0 0**）。
  ⚠️ 这条改的是**仓库外的用户级 SSH 配置**，代理恢复后若想回到直连 HTTPS，把那两行删掉即可。
- **CI 红**：run `38128925252` 三个 job 实测 `test` = success、`lint` = **failure**、
  `build` = skipped。根因与逐条复现记录在 [`docs/TEST_REPORT.md`](../TEST_REPORT.md) §2.17——
  一句话版：CI 那条「零公网外链」的 grep **不分标签**，把页脚按条款 7.3 加的两条
  `<a href="https://lbsyun…">` 判成了公网资源，而测试那套同源哨子这一轮刚改成标签感知，
  于是 CI 红在测试绿了的地方。
- **修 CI 这一轮的本地证据**（不写没测过的数字）：
  `yaml.safe_load('.github/workflows/ci.yml')` → lint job **7 个 step**，新 step 正文 **24 行**；
  把新 step 的正文原样跑在仓库根 → `exit=0`；跑在注入了
  `<script src="https://cdn.example.com/x.js">` 的临时副本 → `exit=1`（stderr 指到
  `injected.html: <script> https://cdn.example.com/x.js`），临时副本随即删除。
  `pytest -q` **501 passed**、`ruff check .` `exit=0`、`black --check .` **49 files unchanged** `exit=0`
  （这一轮只动 `.github/workflows/ci.yml` 与三个 markdown，没碰 `.py`，black 无需重跑第二遍）。
  顺带抓到 README §10.4 两个手写计数漂移（45 例 → 实测 **54**、60 个 id → 实测 **62**），已改，见 TEST_REPORT §7.1。
- **本轮没有起任何本地服务**：`netstat -ano | grep -E ':88[0-9][0-9].*LISTENING'` 只剩
  `127.0.0.1:8888`（pid 4708，上一会话遗留的无关进程，按「不杀不属于本轮的进程」留着）。
- ✅ **回填（同日，`98b1478` = `98b1478115ac018ba658d53509e8bf0a9d0cb3aa`）**：
  `GET /repos/zys1101/CircleDoc/actions/runs/38129396148/jobs` 实测三个 job 全 `completed / success`
  （lint `114440383831` / test `114440384054` / build `114440445835`）。lint 的四个命令步逐条 success，
  其中就是本轮改的 `静态页零公网外链（R12 / R13）`；`build` 不再是上一轮那个 `skipped`，
  `构建镜像` → `断言镜像里没有 .db` → `断言镜像里没有真实 AK` → `零配置起容器并跑一次体检` 全 success。
  ⚠️ API 给的时长（lint 17 s / test 20 s / 构建 18 s）短得不像真实构建，而作业日志这一层是 `403`
  （公开仓库取 job logs 要 admin），所以这里**只登记步骤结论、不登记时长**；要看时长得开浏览器控制台权限。
- 🔴 **同轮自查发现的另一条缺口（不是本轮修的）**：FR-205 的进度只到后端。
  `grep -c 'task_id' web/report.js web/isochrone.js web/app.js` 实测 **0 / 0 / 0**，
  `grep -n 'task_id' web/*.js` 无输出（`exit=1`）——前端从第一天就没调用过 `/api/analysis/{task_id}`，
  状态行是 `web/report.js:1038` 的 `setInterval` 每秒走字。已在 README §10.7 / DESIGN §12 就地登记，
  没有写成「已完成」。

## FR-305 报告卡 PNG · 零出网浏览器实测（2026-10-11，实现人自验，零出网）

PRD FR-305（**P1**）要求「一键导出报告卡 PNG」，而条款 **6.6** 要求导出品里**不得删改**版权与审图号标识。
本项目把这张卡定成「**我方矢量内容合成的报告卡 + 合规文字带**」，**不截地图底图**（理由见 DESIGN §6.4）。
这一轮验四件事：按钮真在页面上并跟着门禁走、合规带是**从页脚读**而不是在 JS 里再抄一份、
合成路径**一次都没碰底图 canvas**、以及整条链路零出网。

### 0. 前提与零出网自证

- 服务：`BAIDU_SERVER_AK= BAIDU_BROWSER_AK= ./.venv/Scripts/python.exe -m uvicorn api.app:app --port 8805`
  （两个 AK 用**空环境变量**压住 `.env` 里那对真实 AK）。
- `GET /api/health` 本轮复测：`http=200`、`status=offline`、`ok=true`、`data_mode=mock`、
  `baidu_reachable=false`、`server_ak_type=not_configured`、`browser_ak_type=not_configured`、
  `services_enabled` **6/6 全 false**。
- `POST /api/analysis`（`{"lnglat":[116.4074,39.9042],"mode":"walking","minutes":15}`）：
  HTTP **200**、`x-request-id: 04a8a4522ff34e48`、`x-http-requests: 0`、`x-place-calls: 0`，
  `usage` = `{http_requests: 0, quota_units: 0, place_calls: 0, cache_hits: 0, elapsed_ms: 20}`。
  ⚠️ 卡片上那一行「本次调用：HTTP 0 次…」就是照着这个 `usage` 画的——**零出网由导出品自己声明**。
- 浏览器侧那份报告：`request_id=6a5db27de42f4d6b`、`meta.generated_at=2026-10-11T10:40:23+00:00`、
  `meta.data_source=sample`、完整档 **12 方向**。
- 🔴 **Network 面板本轮 14 条，逐条本地**：`127.0.0.1:8805` 共 **10 条**
  （`isochrone.html` + `app.css` / `isochrone.css` / `report.css` + `isochrone.js` / `report.js`
  + `/api/regeo` / `/api/health` / `/api/samples` / `POST /api/analysis`）、
  `data:image/svg+xml;charset=utf-8,…` **2 条**（雷达与柱状图各自的光栅化，不是网络）、
  `blob:http://127.0.0.1:8805/…` **2 条**（我探针 fetch 回来量尺寸用）。
  **没有一条发往 `*.baidu.com` / `lbsyun.*` / 任何公网地址**，本轮不占任何配额。
- Console 本轮 **1 条** error：`net::ERR_FILE_NOT_FOUND`。它是**我的探针**第二次去 fetch 一条已被
  `URL.revokeObjectURL()` 回收的 blob URL 造成的——`downloadBlob` 在 `setTimeout(…, 0)` 里回收是**正确行为**，
  不是应用缺陷。这条同时反证了一件好事：**blob URL 真的会被及时回收**，导出的图片不会在内存里挂着。

### 1. 门禁：PNG 按钮和另外三条导出按钮一起开关（点的是页面上的真按钮，不是打桩）

| 状态 | `#export-png.disabled` | `#export-hint` 文案（逐字） |
| --- | --- | --- |
| 刚加载（还没跑过） | **true**（与 `#export-json` / `#export-csv` / `#export-blind-csv` 四条一起 true） | 「先跑一次体检，才有可导出的结果。」 |
| 点「一键体检」后 | **false**（`runMs` 实测 **114** ms 后解禁） | 「导出内容仅限**当前这一次**分析。PNG 是矢量报告卡，不截取地图底图，因此不触碰底图瓦片的版权与使用条款。」 |
| 点 PNG 的瞬间（同步读） | **true** ← 自锁，防连点混用两份结果 | 「正在合成 PNG 报告卡（矢量图表 + 合规文字带，不含地图底图）…」 |
| 出图完成 | **false** | 「已导出 PNG 报告卡（1800 px 宽，约 655 KB）。内容仅限本次分析，不得二次分发。」 |

`exportPng()` 第一件事是 `const report = current;`——把引用**定死在点按钮那一份**报告上，
所以导出期间再来一次分析也不会把两份结果混进一张卡。

### 2. 合成结果实测

| 量 | 值 | 怎么量的 |
| --- | --- | --- |
| MIME | **`image/png`** | `blob.type` |
| 像素 | **1 800 × 2 584** | `createImageBitmap(blob)` 的 `width/height`（宽 = `PNG_W 900 × PNG_SCALE 2`；**高度是两遍布局算出来的**，不是写死） |
| 字节 | **670 295** | `blob.size`；提示里的「约 655 KB」正是 `Math.round(670295/1024)=655`，两处自洽 |
| 真实按钮路径耗时 | **89 ms**（点按钮 → `<a download>` 被点） | 拦下 `HTMLAnchorElement.prototype.click` 记时间戳 |
| 落盘文件名 | `circledoc-walking-116.4074_39.9042-report.png` | 同上，读 `a.download` |
| 同一份报告连跑 4 次 | **670 295 × 4**（distinct = 1），**82 / 85 / 50 / 58 ms** | 直接调 `CircleDocReport.buildPngBlob(getCurrent())` |
| 另一份报告连跑 3 次 | **668 709 × 3** | 同一张表换了 `request_id=d6886aa87eef4e7b` 那份报告 |
| `fillText` 行数 | **19** | 包一层 `CanvasRenderingContext2D.prototype.fillText` 计数 |
| `drawImage` 次数与图源 | **2** 个 `IMG`，本征 **640×640** 与 **1688×844** | 同上（本征 = 逻辑 320×320 与 844×422 各乘 `PNG_SCALE 2`，光栅化按设备像素给，`drawImage` 才是 1:1 贴图） |

⚠️ **两个数不能当回归哨兵**：字节数**随报告变**（`668 709` ↔ `670 295` 差的就是卡片里 `generated_at`
与 `request_id` 那两行的字形），只有「**同一份报告**连跑必得同一字节数」这条是确定性的。
⚠️ **冷跑没量出数**：页面重载后我第一次点按钮时，900 ms 的探针还没抓到下载事件，之后几次都是 ~89 ms。
说明**首次合成明显慢于热跑**（SVG 光栅化要等图片解码），但我没有冷跑的确切毫秒数，就不写进表里。

### 2b. 画布原语逐条数（把「卡片真有内容」做成可查的账）

包住 `scale` / `fillRect` / `stroke` / `fillText` 再合成一次，**blob 仍是 670 295**——
说明这层 instrumentation 不改变输出，下面的数就是产品那条路径的数：

| 量 | 实测值 | 说明 |
| --- | --- | --- |
| `ctx.scale` | `scale(2, 2)` **1** 次 | 只在真画那一遍调；`dryRun` 一遍不碰画布 |
| 背景 `fillRect` | `#ffffff`，`0, 0, 900, 1292`，**1** 次 | **逻辑 900 × 1292** → 设备 `1800 × 2584`，与 `× PNG_SCALE 2` 自洽；**1292 是两遍布局算出来的高度，不是常量** |
| `stroke()`（分隔线） | **2** 次，均 `strokeStyle=#d8dee4`、`lineWidth=1` | 两个 `rule()`（分数前 / 合规带前），都写在 `!dryRun` 分支里 |
| `drawImage` | **2** 次，本征 `640×640` 与 `1688×844` | 逻辑 `320×320` / `844×422` 各乘 2 |
| `fillText` 字号分布 | 22 px（600）**1** · 30 px（bold）**1** · 15 px（600）**1** · 13 px **11** · **12 px 5** | 合计 **19** 行；**最后 5 行全是 12 px，那就是合规文字带**（逐字见 §3） |

### 3. 合规文字带：读页脚，不是再抄一份

卡片最后 5 行 `fillText` 逐字（本轮实测抓取）：

```
[14] 本服务使用百度地图开放平台的接口与数据（地理编码 / 逆地理 / 地点检索 / 路线规划与批量算路）。
     本地缓存仅为减少重复调用的技术措施，不是可对外查询        ← .foot-legal 经 wrapText 换行
[15] 的数据源：地点数据 ≤ 24 小时、路线 ≤ 7 天、地理编码与逆地理 ≤ 30 天，到期即物理删除。
     示例数据由本程序按参数合成，不含任何第三方原始记录；导出
[16] 仅限当前一次分析的结果，不得二次分发。
[17] 地图审图号：GS(2025)4125｜测绘资质：甲测资字11111342 （读自 lbsyun.baidu.com 页脚，2026-10-11）
[18] 本图为矢量报告卡，不含地图底图瓦片；如需底图图像，请自行保留其版权与审图号标识。
```

`[14]`~`[16]` 就是页脚 `.foot-legal`（折叠空白后 **176** 字符）被 `wrapText` 按 844 px 内宽换行的产物，
`[17]` 是 `.foot-review`（**72** 字符）整段——也就是说页脚改了字，卡片**自动跟着改**，不存在两处维护。

「唯一真相源」的直接证据（本轮 grep，含退出码）：

| 命令 | 结果 |
| --- | --- |
| `grep -c 'GS(2025)4125' web/report.js` | **0** |
| `grep -c '甲测资字' web/report.js` | **0** |
| `grep -c 'GS(2025)4125' web/isochrone.html` | **1** ← 只在这里 |
| `grep -c 'foot-legal' web/report.js` | **1** ← 只有 `complianceText(".foot-legal", …)` 那一次读取 |
| `grep -n 'toDataURL' web/report.js` | **1 处，在第 958 行的注释里**；把注释剥掉后 `report.js` 代码里出现 **0** 次（`test_png_card_does_not_read_the_basemap_canvas` 用的就是剥注释后的口径） |

另外卡片第 `[2]` 行本身写的是 `数据来源 sample · 完整档（12 方向）· 生成于 … · request_id …`，
第 `[12]` 行是结论里那条「本次结论基于程序合成的示例数据（mock_mode）」——**降级/示例声明也在图上**。

### 4. 负路径：页脚一旦读不到，就**拒绝出图**

三条都在真实页面上做完，每条都**没有** fallback 字符串（`complianceText(selector)` 单参、缺文案即抛）：

| 操作 | `buildPngBlob` 结果（错误文案逐字） |
| --- | --- |
| `.foot-legal` 文本置为空白 | reject `报告卡缺合规文案：页脚 .foot-legal 为空` |
| `removeChild(.foot-legal)` 整个节点摘掉 | reject `报告卡缺合规文案：页脚 .foot-legal 为空`（与空白走同一条 `if (!text)`） |
| `.foot-review` 置空 | reject `报告卡缺合规文案：页脚 .foot-review 为空` |

真实按钮路径下这条 reject 会走 `exportPng()` 的失败分支，实测提示与门禁：

```
点按钮（.foot-legal 已被清空）
  → 同步：disabled=true、提示「正在合成 PNG 报告卡…」
  → 落定：disabled=false、提示「PNG 导出失败：报告卡缺合规文案：页脚 .foot-legal 为空。JSON / CSV 导出不受影响。」
把页脚恢复后再点一次
  → 「已导出 PNG 报告卡（1800 px 宽，约 655 KB）。内容仅限本次分析，不得二次分发。」
```

🔴 **为什么是拒绝出图而不是"少打一行"**：条款 6.6 原文要的是「不得删除、掩藏、修改或替换」标识。
一张**静默缺了审图号**的 PNG 会被用户当合规品转出去，比导不出来糟得多。宁可导出失败。

### 5. 本轮抓到并修掉的两条（都不是功能本身，是**测试基建**）

1. **`syncExportState()` 把结果提示冲掉了**：原来两个分支都先回门禁再写提示，
   而 `syncExportState()` 会把 `#export-hint` 刷回默认文案 → **成功 / 失败 / 进行中三态看起来一模一样**。
   这条只有真浏览器能照出来（静态测试当时还是绿的）。修法：**先 `syncExportState()`、后写提示**，
   并在 `test_png_card_button_is_wired_and_gated` 里按 `(error) => {` 把成功/失败两支**拆开各查一次顺序**。
   ⚠️ 第一版断言写的是 `runner.rfind("syncExportState();", 0, runner.index("PNG 导出失败"))`，
   在**坏代码上也通过**（成功分支那次调用就在失败文案前面），负例跑出来是「失败 old order rejected = False」——
   改成分支级断言后两个负例才都拒绝。这条教训登记在 TEST_REPORT §2.19。
2. **`tools/report_dom_smoke.mjs` 里有 5 条 `check()` 从 `f24a174` 起永远执行不到**：文件中部遗留了一段
   早退的 summary + `process.exit`，导致冒烟一直报「15 项」而实际写了 20 条。删掉早退块后本轮
   `grep -c '^check('` = **21**、`grep -c 'OK '` = **21**（新增了 FR-305 那条 `buildPngBlob` 接线检查），
   `exit=0`。口径固化：**冒烟条数用这两个 grep 对拍，不手写**。详见 TEST_REPORT §2.18。

### 6. 本轮**没有**验的（别当成已验）

1. 🔴 **真实落盘**：本轮把 `<a download>` 的点击**拦下来**测字节 / 尺寸 / 文件名，
   文件**没有真的写进下载目录**（刻意不往你的机器落盘）。「双击能打开、打开看到的和 blob 一致」
   这一条要人在浏览器里点一次按钮才算验完。
2. 🔴 **canvas taint 那一半仍未实测**：live 底图下 `toDataURL()` 会抛 `SecurityError`
   **只有代码与规范依据**。验它要开一次 live 底图 = 烧 JSAPI 浏览器端配额，按配额纪律等点头。
   对本功能**不构成阻塞**（卡片根本不读底图 canvas），只影响「以后有人想加底图截图」那条路。
3. **live / 真实 AK 下的两行文案**：本轮只在 mock（`data_source=sample`、完整档 12 方向）量过；
   live 下 `meta.degraded` / `degraded_reason` 会不会给出来、卡片那条黄色警示行
   （`PNG_WARN #9a6700`，白底对比度实测 **4.87:1**）会不会出现，未跑。
4. **纸质可读性**：1 800 px 宽 / 655 KB 只保证「聊天工具里放大可读」，A4 打印下 12 px 小字未评。
5. **真机**：移动端 `data-sheet-mode=mobile` 下导出 PNG 的完整链路（本轮全部在 1280×800 桌面档跑）。

### 7. 提交之前的门禁

- `./.venv/Scripts/python.exe -m pytest -q` → **506 passed**，对最终代码状态连跑五次
  `exit=0`（4.93 / 4.97 / 4.84 / 4.81 / 4.85 s）。
- `ruff check .` `exit=0`；`black --check .` **49 files would be left unchanged** `exit=0`。
  ⚠️ 本轮 black **首跑 `exit=1`**，改的是新加断行的 `assert … , "msg"` 形态；另外我自己在仓库根留的
  `.measure305.py` / `.png-contrast.py` 两个临时脚本也被 ruff 抓到——删掉才干净（临时脚本别落在仓库根）。
- `node --check web/report.js` / `node --check web/isochrone.js` 各 `exit=0`
  （⚠️ 别把 `.js` 交给 black：`black --check tests/… web/report.js` 实测 `exit=123`，JS 只能用 `node --check`）。
- `node tools/report_dom_smoke.mjs` → `exit=0`，`grep -c 'OK '` = **21** 且 `grep -c '^check('` = **21**
  （两个 grep 对拍，本轮新加的 FR-305 那条在其中）；
  `node tools/map_layers_smoke.mjs _map_input.json` → `exit=0`，`OK ` = **12** = `^check(` = **12**。
- DOM 与体积计数本轮全部重测：`grep -o 'getElementById("…")' | sort -u | wc -l` →
  `report.js` **27**（新增 `export-png`）、`isochrone.js` **62**；`isochrone.html` 去重 id **97**；
  `wc -c web/* | tail -1` → **276 678 字节 = 270.2 KB = PRD §5 预算的 54 %**；
  `pytest tests/test_web_smoke.py --collect-only -q` → **59 例**。README §10.4 / §10.7 与 DESIGN §10
  里这几个数**逐个对上了实测值**，没有沿用上一轮的写法。

## 未在真机验证清单（风险 R15 诚实登记）

1. 手指惯性拖拽（pointer 事件在触摸屏上的动量续滚）——`isochrone.js:2032-2058` 只有 pointerdown/move/up，无惯性实现，真机手感未测。
2. 双指缩放——代码只实现滚轮缩放（`wheel`，passive:false），**未见双指 pinch 处理**，触摸屏可能无法缩放（建议评审真机确认后立 P2）。
3. iOS Safari 滚动穿透 / 100vh 地址栏抖动——未测。
4. DevTools Network offline 下切 live 触发真实 JSAPI 注入失败 → 降级横幅全链路——本环境无拦截工具且禁真实出网，未跑（风险口径见 README §10.7 那一张表）。
5. ~~截图证据全部未落地（in-app browser 无可见 surface）~~ → 2026-10-10 起 chrome-devtools 通道**能**截图，
   但按上一节「关于截图」的口径仍**不提供 PNG**，本文件继续以 DOM/计算样式/network/console 文字证据为准。
   早先那条 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE` 限制属 browser-use 通道，与本轮用的 chrome-devtools 不是同一条路径。
6. **FR-108 的中文输入法路径**——真机上拼音是 composition 期间连续触发 `input`，且软键盘顶起视口会改变
   `#controls` 的可视高度；本轮 CDP 走的是「直接改 `value` + 派发 `input`」，两者都不等于真机键盘。
   逐条边界见上面「FR-108 地址联想」§4。
7. **FR-305 的 PNG 有没有真的落到磁盘上**——本轮把 `<a download>` 的点击**拦下来**测了 blob 的类型 / 字节 /
   尺寸 / 文件名，但**刻意没让浏览器往下载目录写文件**。`downloadBlob` 的形状
   （`createObjectURL → a.click → setTimeout(revoke, 0)`）是标准做法，blob 也确实是 `image/png` 且
   `createImageBitmap` 解得开，可「双击打开、看到的就是这张卡」这一条要人在真浏览器里点一次才算验完。
8. **FR-305 另一半：live 底图能不能截**——`toDataURL()` 在未命名允许的跨域画布上应抛 `SecurityError`
   只有代码与规范依据，验它要开一次 live 底图（JSAPI 浏览器端配额），等点头。详见「FR-305 报告卡 PNG」§6。

## 收尾

- 本次自起的 uvicorn（8766）因端口占用启动失败、进程已自动退出；8766 上运行的是上一轮遗留服务（任务指示不杀），无残留进程需清理。
- D-1 复验（见上节）用的是新起的 8767，验完已 `taskkill`；上一轮遗留的 8765 / 8766 服务也已一并关闭。当前 `netstat` 查 8765/8766/8767 无 LISTENING，无残留进程。
- 2026-10-10 JSAPI 复验那一轮起的两个本地进程都已收干净：`uvicorn api.app:app --port 8793`（真实出网那次实跑）
  与 `swarm/m_ak_seed.py --port 8794`（浏览器 AK 本地给值）。验完后 `netstat -ano | grep -E "8793|8794"` **无输出**，
  8765 ~ 8794 全线无 LISTENING。
- `pytest -q` 复跑结果见验收报告正文（基线 240 全绿，验收全程未改动 `web/ api/ services/ infra/ tests/ swarm/` 任何文件）。
- FR-106 那一轮（2026-10-11）起的本地服务与模拟状态也已收干净：`uvicorn api.app:app --port 8795`
  （用空 AK 环境变量压住真实 AK 起的那个）停止后 `netstat -ano | grep 8795` **无输出**；
  chrome-devtools 的设备模拟从 `375x667x2,mobile,touch` 复位回 `1280x800x1`；
  冒烟用的临时响应 `_map_input.json` 已删除（按 `docs/TEST_REPORT.md` §7 第 5b 步随时可再生，不入库）。
- FR-107 那一轮（2026-10-11，同日）起的 `uvicorn api.app:app --port 8796`（同样用空 AK 环境变量压住真实 AK）
  已停止，`netstat -ano | grep -E ":879[0-9] " | grep -c LISTENING` 实测 **0**；本轮为跑 `map_layers_smoke`
  再生过一次 `_map_input.json`，验完已删；设备模拟复位在 `1280x800x1`。
- FR-103 那一轮（2026-10-11，同日）起的 `8801`（地址行五例）与 `8802`（复跑 `map_layers_smoke` 用的临时响应）
  都已 `taskkill`，`netstat -ano | grep -c '880[0-9].*LISTENING'` 两次实测都是 **0**；
  `.t103-health.json` / `.t103-regeo.json` / `.t103-server.log` / `.t103b.log` / `_map_input.json` /
  `.smoke1.txt` / `.smoke2.txt` 全部删除，设备模拟复位 `1280x800x1`。逐条见上面「FR-103 中心点地址回显」§5。
- FR-108 那一轮（2026-10-11，同日）起的 `8803`（联想七例）已 `taskkill`（`pid=10500`），
  `netstat -ano | grep -c '880[0-9].*LISTENING'` 实测 **0**；`.fr108-scratch/` 与复跑冒烟用的
  `_map_input.json` 已删除，设备模拟复位 `1280x800x1`。逐条见上面「FR-108 地址联想」§0 ~ §5。
- FR-305 那一轮（2026-10-11，同日）起的 `uvicorn api.app:app --port 8805`（pid 3620，同样用空 AK 环境变量
  压住真实 AK）已 `Stop-Process`，验完 `netstat -ano | grep -cE ':880[0-9].*LISTENING'` 实测 **0**；
  整条 88xx 段只剩 `127.0.0.1:8888`（pid 4708，上一会话遗留的无关进程，按「不杀不属于本轮的进程」留着）。
  临时件 `.fr305-health.json` / `.fr305-health2.json` / `.fr305-a.json` / `.fr305-h.txt` /
  `.fr305-server.log` / `.domsmoke.txt` / `.d2.txt` / `.d3.txt` / `.d4.txt` / `.m3.txt` /
  `.mapsmoke.txt` / `.pt.txt` / `_map_input.json` 全部删除；实测过程中**没有**让浏览器往下载目录写文件
  （`<a download>` 被拦下），所以磁盘上不留 PNG。chrome-devtools 那条标签页已回 `about:blank`，
  设备模拟停在 `1280x800x1`。
  🔴 仓库根本轮两次被自己的临时脚本脏到：`.measure305.py` / `.png-contrast.py` 让 `ruff` 与 `black`
  首跑 `exit=1`。口径：**临时脚本放仓库外或用完即删**，别指望门禁替你记住。
