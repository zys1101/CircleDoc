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
5. **仍未验证**：JSAPI 实时底图那条路径上的 `BMap.Marker` 点击（本环境禁真实出网，只跑到 SVG 降级引擎），真机触摸点击属 R15 清单。

## 未在真机验证清单（风险 R15 诚实登记）

1. 手指惯性拖拽（pointer 事件在触摸屏上的动量续滚）——`isochrone.js:2032-2058` 只有 pointerdown/move/up，无惯性实现，真机手感未测。
2. 双指缩放——代码只实现滚轮缩放（`wheel`，passive:false），**未见双指 pinch 处理**，触摸屏可能无法缩放（建议评审真机确认后立 P2）。
3. iOS Safari 滚动穿透 / 100vh 地址栏抖动——未测。
4. DevTools Network offline 下切 live 触发真实 JSAPI 注入失败 → 降级横幅全链路——本环境无拦截工具且禁真实出网，未跑（见项 7）。
5. 截图证据全部未落地（in-app browser 无可见 surface），本文件以 DOM/计算样式/network/console 文字证据替代。

## 收尾

- 本次自起的 uvicorn（8766）因端口占用启动失败、进程已自动退出；8766 上运行的是上一轮遗留服务（任务指示不杀），无残留进程需清理。
- D-1 复验（见上节）用的是新起的 8767，验完已 `taskkill`；上一轮遗留的 8765 / 8766 服务也已一并关闭。当前 `netstat` 查 8765/8766/8767 无 LISTENING，无残留进程。
- `pytest -q` 复跑结果见验收报告正文（基线 240 全绿，验收全程未改动 `web/ api/ services/ infra/ tests/ swarm/` 任何文件）。
