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
2. **移动端（≤ 768 px）下的七行勾选**：抽屉本身还没实现（见 README §10.3 的更正），
   本轮只在桌面宽度验。
3. **键盘 Tab 顺序穿过七个框 + 「恢复全选」**：未逐项走查，只确认框是原生 `input[type=checkbox]`。

## 未在真机验证清单（风险 R15 诚实登记）

1. 手指惯性拖拽（pointer 事件在触摸屏上的动量续滚）——`isochrone.js:2032-2058` 只有 pointerdown/move/up，无惯性实现，真机手感未测。
2. 双指缩放——代码只实现滚轮缩放（`wheel`，passive:false），**未见双指 pinch 处理**，触摸屏可能无法缩放（建议评审真机确认后立 P2）。
3. iOS Safari 滚动穿透 / 100vh 地址栏抖动——未测。
4. DevTools Network offline 下切 live 触发真实 JSAPI 注入失败 → 降级横幅全链路——本环境无拦截工具且禁真实出网，未跑（见项 7）。
5. ~~截图证据全部未落地（in-app browser 无可见 surface）~~ → 2026-10-10 起 chrome-devtools 通道**能**截图，
   但按上一节「关于截图」的口径仍**不提供 PNG**，本文件继续以 DOM/计算样式/network/console 文字证据为准。
   早先那条 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE` 限制属 browser-use 通道，与本轮用的 chrome-devtools 不是同一条路径。

## 收尾

- 本次自起的 uvicorn（8766）因端口占用启动失败、进程已自动退出；8766 上运行的是上一轮遗留服务（任务指示不杀），无残留进程需清理。
- D-1 复验（见上节）用的是新起的 8767，验完已 `taskkill`；上一轮遗留的 8765 / 8766 服务也已一并关闭。当前 `netstat` 查 8765/8766/8767 无 LISTENING，无残留进程。
- 2026-10-10 JSAPI 复验那一轮起的两个本地进程都已收干净：`uvicorn api.app:app --port 8793`（真实出网那次实跑）
  与 `swarm/m_ak_seed.py --port 8794`（浏览器 AK 本地给值）。验完后 `netstat -ano | grep -E "8793|8794"` **无输出**，
  8765 ~ 8794 全线无 LISTENING。
- `pytest -q` 复跑结果见验收报告正文（基线 240 全绿，验收全程未改动 `web/ api/ services/ infra/ tests/ swarm/` 任何文件）。
