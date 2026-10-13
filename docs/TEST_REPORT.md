# CircleDoc 测试与对比实验报告

| 项 | 内容 |
| --- | --- |
| 文档定位 | PRD §9 交付物「对比测试报告」；对应 FR-405、FR-406、§6.1、§6.2 |
| 最后更新 | 2026-10-13 |
| 测试命令 | `pytest -q`（零配置、零 AK、零真实出网） |
| 结论 | **513 例全绿**，`ruff` / `black` 零告警；测试套件零真实出网、可全程离线复现 |

---

## 0. 先说清楚这份报告的边界

**2026-10-10 起本项目已接入真实百度 AK**（服务端 + 浏览器端两类，配在 gitignored 的 `.env` 里）。
所以这份报告现在有两类证据，**口径不同、不互相顶替**：

* **`mock` 侧**：513 例单测与两套冒烟自测仍在**零配置、零 AK、零出网**下全程复现——这是 CI 的形态，
  也是本文 §1、§2、§3、§5 的主口径。
* **`live` 侧**：§4.3 是一次**真实出网**的端到端实跑（三样例各一次），§6 的「JSAPI 实底图顶点点击」
  是另一次真实浏览器 + 真实出网的补验。§2.6 ~ §2.9 四个缺陷**只有在 live 下才暴露**——
  离线演示阶段它们全被「合成报文比真实报文更完整」盖住了。
* **浏览器侧**：§2.10 与 §6 的若干断言要把页面真打开才看得见。**§2.10 不属于 live 缺陷**——
  它在 `data_mode=mock`、零出网的服务器上照样复现，漏网的原因和前四条不同：
  **缺的是「有人读过页面上的字」，不是缺真实数据。**

两条纪律没有因为「有了 AK」而放松：

* 压测类实验**继续只在 `httpx.MockTransport` 上完成**（不建任何连接）。条款 3.2.3(ii) 明文禁止
  对百度线上服务施压测试，而「跑 96 次串行 `direction`」正是一种压测——**有 AK 也不做**（§3）。
* 报告里凡是**依赖真实网络**才能得到的数字，一律标注为「**推算**」并给出假设参数；本地能精确测的
  （HTTP 请求数、配额单位、CPU 耗时、排队时长）与真实实跑的墙钟标注为「**实测**」，并注明样本量。

---

## 1. 测试清单（按泳道）

| 测试文件 | 例数 | 覆盖对象 | 对应需求 |
| --- | ---: | --- | --- |
| `test_cache_keys_ttl.py` | 86 | 缓存键稳定性（跨 `PYTHONHASHSEED`）、分服务 TTL、POI TTL 合规上限 | FR-402、条款 3.3.4 |
| `test_isochrone_geometry.py` | 54 | 扇形采样、二分收敛、顶点排序、DP 简化、IDW 插值、FR-107 轻量档 | FR-201、FR-204 |
| `test_route_probe.py` | 36 | 批量算路 ≤50 切分、两栏计量、预算截停、禁行归类 | FR-405、§4.6 |
| `test_ratelimit_buckets.py` | 35 | 按服务分桶令牌桶、`burst ≤ rate`、排队串行化 | FR-401、§4.6 |
| `test_coverage.py` | 30 | 落圈射线法、前 K 挑选（全局 vs 分层）、未实测归因、三栏计量 | FR-211、§4.3 |
| `test_blind_spots.py` | 32 | 网格生成、haversine 判定、flood fill、轮廓追踪、两级阈值 | FR-220 ~ FR-224 |
| `test_score.py` | 31 | 加权、95 % 权重缺口裁决、等级线、盲区惩罚、摘要生成 | FR-301、FR-302、FR-304 |
| `test_isochrone_api.py` | 26 | `/api/isochrone` 全形状、降级、polyline 12 次硬上限 | FR-201、FR-203、FR-403 |
| `test_geo_api.py` | 27 | **`/api/regeo` 与 `/api/geocode` 的接口级用例（上一轮新增，此前一条都没有）**：§4.7 出参**平铺在顶层**、字段只来自 `addressComponent`（用一份「文本里像有区、组件里没给」的报文反证不切文本）、`location` 纬度在前而响应 `lnglat` 经度在前、同坐标第二次**命中缓存且 0 次出网**、空 results 留在 `live` 层不退合成、mock 态无 `formatted_address` 键、四类非法 `lnglat` 与境外坐标 400 且指名字段、信封带 `request_id` 且不泄 AK。<br>**本轮 +11 例 `/api/suggest`（FR-108）**：默认关闭时 `degraded_reason = suggest_disabled` 且 `transport.calls == []`（**根本没发请求**，比 200 更能证明没烧配额）、`.env` 里的 `SUGGEST_ENABLED=true` 真能到达服务（§2.13）、候选只留「有坐标且在国内」的两条并计数 `total/dropped`、按 **place 桶**计量、缺 `region` 与 < 3 字各自 400 且指名字段、空列表是**结论**而不是错误、同词第二次走缓存 0 出网、**关掉开关后已有缓存仍可读** | FR-103、FR-108、FR-101、§4.7、R1′、R8、FR-403 |
| `test_analysis_api.py` | 46 | `/api/analysis` 报告形状、任务轮询、后台模式、`/api/poi` 七维度、错误信封、**真实检索盲区偏大提示（`live` 与 `cache` 两态各断言一次 + `sample` 反向断言）**、**FR-104 类别子集：伤及盲区主判定的 7 种勾选组合逐一断言 + 总分分母随子集收缩** | §4.7、FR-205、FR-404、§4.4、§2.9、FR-104 |
| `test_web_smoke.py` | 62 | 静态页可服务、零公网外链、**DOM 契约对账（两个脚本）**、**前端类别勾选 ↔ 后端词表逐项对账**、FR-212/222/213 的前端接线、**FR-106 抽屉的把手条 ↔ 面板对账 / 断点唯一真值 / 无 JS 降级 / 44 px 规则 / 地图高度口径**、**FR-107 档位来源（`?profile=` 优先于视口探测）与简化模式徽标取自响应 `meta`**、**FR-103 地址行的接线（`setCenter` 里必须真的调用、且不 `await`）/ 三条配额守卫齐全 / 地址字段无 `split` 无 `toFixed` / 四个状态都用 `data-state`**、**FR-108 开关默认关 + 前后端门槛逐字相等 + 世代号/缓存键/在飞去重**。<br>**本轮 +3 例 FR-413**：外链反向钉死（资源标签零外链 **且** `<a>` 只允许官方那两个 URL 带 `rel="noopener noreferrer"`，把「哨兵按标签收窄」这件事本身钉住，防止它变成后门）、**页脚 TTL 三个数字对着 `Settings.model_fields` 的类默认值逐项比对**（改配置不改页脚就红）、**页脚正文对页面底色的 WCAG 对比度在测试里重算**（4.5:1 硬线）、**一键体检路径的 `request_id` 交接**（见 §2.15）。<br>**本轮 +5 例 FR-305 PNG 报告卡**：按钮在 HTML / `cache()` / 点击监听 / **导出门禁**四处都接上，且**结果提示必须写在 `syncExportState()` 之后**（按成功与失败**分支各自**判，见 §2.19）；合规文案只能 `querySelector` 读页脚、**读不到就抛错**，不许在 JS 里存第二份；**不许碰底图画布**（`report.js` 里不出现 `toDataURL`，两张图的宿主写死是 `#radar-chart` / `#bar-chart`）；SVG → data URL 走 `encodeURIComponent` 而不是 `btoa`（中文标签会让后者直接抛），且配色按 `getComputedStyle` 内联；卡片三种文字色对卡底在测试里**重算 WCAG 对比度**（实测 15.52 / 7.56 / 4.87，最紧的是降级提示那条琥珀色）。<br>**本轮桌面报告版式 +3 例**（见 §2.20）：**`#report` 在桌面栅格里必须占满整行**（`grid-column: 1 / -1`，缺它就被 `minmax(260px,300px)` 钉成 300 px 窄条）、**图表宽度上限只绑定在抽屉断点之上**（`max-width` 与 `matchMedia` 那个数字同源，769 以下放开、之上钉死，防止撑成全宽后雷达涨到 600 px）、**评分卡三档文字色对渐变两端各 9 个组合重算对比度**（不是对着「大概是个蓝底」验）。这三条全是静态文本断言（读 CSS 源码），零出网、零浏览器 | R11、R12、R13、FR-213、FR-104、FR-106、FR-107、FR-103、FR-108、FR-413、**FR-305**、条款 3.3.4 / 6.6 / 7.3 |
| `test_samples_baseline.py` | 16 | 三样例一致性、三型差异方向、基线不漂 | FR-102、FR-412、§6.1 |
| `test_coord_param_table.py` | 13 | 接口 × 坐标参数名 × 坐标顺序常量表 | FR-406、R8 |
| `test_health_contract.py` | 11 | `/api/health` 三个「数据模式」字段的位置与值域、零配置横幅、AK 不外泄 | §4.6、FR-404、FR-410 |
| `test_cache_store_singleton.py` | 4 | 缓存单例口径唯一 | FR-402 |
| `test_egress_sharing.py` | **4**（本轮新增，见 §2.21） | **出网资源是不是全进程一份**：① 三个默认服务单例形状的客户端必须落在**同一个** `Egress` 上（同一个 `rate_limiter` / `breakers` 对象、`shared is True`、`bindings ≥ 3`），同时反向钉住 **`mode_count` 仍是各自的**（`isochrone == 2`、`geo == 1`——合并出口不等于合并预算）；② 五种注入形态（`transport` / `http_client` / `clock` / `rate_limiter` / `base_url`）各自必须留在**私有出口**上，且共享出口 `bindings` 不因它们增长（否则 MockTransport 用例与 `/api/health` 的 FakeClock 探针会互相污染令牌桶）；③ **熔断是账号级而不是 client 级**：第一个 client 打满失败阈值后，第二个 client 的 `before_request("place")` 也必须 `.allowed is False`，而未被污染的 `"geocode"` 桶照常放行；④ **在途峰值真的被闸住**：3 个 client × 4 次请求 = 12 次全发出去（`total == 12`），但计数桩同时观测到 `peak ≥ 2`（证明断言非空转）且 `peak ≤ 3` | FR-401、FR-403、§4.4、§4.6、R5 同族 |
| **合计** | **513** | | |

耗时：**5.06 ~ 5.78 s**（同一轮 513 例，本机对**最终代码状态**连续五次实测，
5.28 / 5.06 / 5.48 / 5.47 / 5.78 s，五连跑均报 `513 passed`；退出码另单独跑一次用
`$?` 直接取，不经管道——**5.28 / 5.06 / 5.48 / 5.47 / 5.78 那一轮的 `exit=0` 是 `tr` 的**，
不是 pytest 的，所以这条不能省）。`chrome-devtools` 与 node MCP 全程在线。
上一轮记录的 4.92 ~ 5.13 s 与更旧的 8.94 / 11.55 s **不是代码回归**而是后台负载：8.94 那次
`chrome-devtools` 浏览器与它的 node MCP 服务都在跑，
纯 CPU 基准 `sum(range(10**7))` 同时段实测 0.644 s（正常量级的两倍），最慢单例
`test_task_registry_is_bounded` 也从 0.68 s 涨到 1.9 ~ 2.3 s。本轮五次测量时浏览器 MCP
**仍然在线**，所以那个区间只能说明「同一台机器同一份代码可以差 2.4 倍」，
不能说明任何代码性质。无网络、无外部数据库、无 Redis。

### 1.1 四条值得单独说的断言

**`test_web_smoke.py::test_every_get_element_by_id_exists_in_the_page`** —— 把**两个**脚本
（`web/report.js` 与 `web/isochrone.js`）里 `getElementById` 取到的 id 全部抠出来，逐个断言
`isochrone.html` 里存在。抓的是「改了 HTML 忘了改 JS」：漏一个 id，`el.x` 就是 `null`，
报告某一块永远空白，而**控制台一条错误都不会有**。这是 R11「无构建模式没有类型检查兜底」
那条风险的实际落点。两个脚本都要覆盖是 FR-104 这轮补上的：先前只盯 `report.js`，
`isochrone.js` 新取的 `#cat-all` / `#category-hint` 正好落在哨兵外面。

**`test_web_smoke.py::test_category_checkboxes_match_the_backend_wordlist`** ——
FR-104 的七个勾选框与 `services/poi_categories.py` **逐键、逐序、逐文案**对齐，
连「哪几类装着盲区主判定」的 `data-blind-cat` 标记也要等于派生常量 `BLIND_TRIO_DIMENSIONS`。
这条是前端与后端词表之间唯一的自动对账：`data-cat` 写了后端不认的键就是每次体检直接 400，
而 `data-blind-cat` 指错维度会让页面提示「口径可比」、实际盲区面积已被高估 ——
后者恰好是这份报告最不该错的东西。

**`test_analysis_api.py::test_booleans_are_rejected_instead_of_coerced_to_one`** ——
pydantic 的宽松模式会把 `true` 强转成 `1`，于是 `{"radius": true}` 变成一个**1 米**的
检索半径并**静默返回 200**：调用方拿到一份看起来正常、其实只搜了脚下一平方米的报告。
这条断言把它钉成 422。

**`test_coverage.py::test_fair_selection_prevents_the_dense_area_score_inversion`** ——
一条缺陷回归，见下节。

---

## 2. 开发过程中发现并修复的二十一个真实缺陷

这二十一个都**不是**通过 code review 发现的，而是「把服务层真正接上 API 并跑起来」
「把 CI 脚本真的执行一遍」「在最小 DOM 打桩下真跑一遍绘制路径」
「把移动端抽屉在 DevTools 里真拖一遍并量 `getBoundingClientRect`」
「**配上真实 AK 跑一遍 live 全链路**」「**打开百度账号控制台看它眼里我们的并发是多少**」之后暴露的。
其中 §2.6 ~ §2.9 四个**只在接入真实 AK 之后才可能出现** —— 在离线演示阶段，
它们全都被「合成数据比真实数据更完整」这件事盖住了。
§2.10 是**在真实浏览器里点出来的**：`node --check` 与 451 例单测全绿，全选态的提示文案
却仍然写错，因为那条分支只有真人打开页面才会看到。
§2.11 更难看一档：那一轮**写在文档里的冒烟数字本身是假的**，`node --check` 拦不住、单测也拦不住。
§2.15 与 §2.10 / §2.11 是同一族：**那一轮 501 例全绿、`ruff` / `black` 全绿、`node --check` 全绿，
页脚上那串 `—` 照样是错的**。它踩的是「两条路径共用一个渲染函数，而两条路径的信封形状不一样」。
§2.16 踩的还是老坑：**一个我自己在上一轮算错、并抄进三处文档的对比度数字**（写的 4.49，真值 4.51）。
§2.17 是同族里新的一种：**本地三套门禁全绿，红在 CI**——同一份「零公网外链」规则有两套判定，
测试那套这一轮刚改成标签感知，CI 那条自称「与测试同源」的 grep 没跟着改，于是它红在了测试绿的地方。
§2.18 / §2.19 是 FR-305 这一轮的两条，**共同点是三套门禁与「跑一次看条数」都拦不到**：
一条是冒烟脚本中间多了一份 `process.exit`，让 5 条断言从建仓起就是不可达代码（输出稳定 15 项 OK、
退出码 0，看着完全正常）；一条是导出反馈被门禁函数覆盖回默认文案，**只有真点一次、真读一次提示**才看得出来。
§2.20 是**只有真实浏览器能看见的一类**：`#report` 在桌面三栏下被压成 300 px 窄条，自初始提交就存在，
509 例单测（**那**一轮的数，本轮之后是 513）、两套冒烟自测与 `node --check` 全都不读 CSS 布局，所以它躲过了前面每一条门禁。
§2.21 是**只有账号控制台能看见的一类**：README 写「`BAIDU_MAX_CONCURRENCY` 是在途上限」，
可出网资源是**每个 `BaiduClient` 一套**、进程里有三个 client，实际并发 3 × 4 = 12、QPS 也乘三，
连熔断器都按 client 计数而百度按 **AK** 计数——**513 例单测全绿拦不住一条对账号的错误承诺**。

### 2.1 `services/poi_search.py`：7 处协程未 `await`（会导致接口 500）

`_sample_group()` 是 `async def`，但 7 个调用点全部写成 `return self._sample_group(...)`
（缺 `await`）。在 `data_mode=mock` 这条**最常用的离线路径**上，它会返回一个协程对象，
紧接着 `outcome["skipped_reason"]` 抛 `TypeError: 'coroutine' object is not subscriptable`
→ HTTP 500。

为什么之前 285 例测试没抓到：M3 泳道交付了服务层，但 `api/poi.py` 与 `api/analysis.py`
当时都不存在，**没有任何测试调用过 `PoiSearchService.search()`**。
本次接入后第一次 POST 就复现了，同一条路径上还伴随
`RuntimeWarning: coroutine 'PoiSearchService._sample_group' was never awaited`。

修复：7 处补 `await`。回归覆盖：`tests/test_coverage.py` 与
`tests/test_samples_baseline.py` 现在会真正走完 `search()` 的 mock 分支。

### 2.2 前 K 实测的全局最近选择造成「配套越密集、得分越低」

PRD §4.3 第 4 条写的是「圈内 + **直线距离最近的前 K 个**」，K 是全局的。
按字面实现后，实测三样例出现**倒挂**：

| 样例 | 建成形态 | 圈内点位数 | 总分 | 等级 | 盲区面积 |
| --- | --- | ---: | ---: | --- | ---: |
| `beijing-jiaodaokou` | 成熟老城 | 48 | **61.40** | C | 3.53 % |
| `beijing-tiantongyuan` | 新建大盘 | 14 | **80.97** | B | 26.33 % |
| `beijing-xiaojiahe` | 城中村 | 23 | 44.65 | D | 97.12 % |

机制：密集社区的圈内点位多，全局最近的 12 个名额可能全被餐饮与便利店占满，
于是医疗 / 购物 / 公交三个维度**一个都没实测到** → `nearest_minutes = None`
→ §4.5 的时效分记 0（`services/coverage.py` 刻意拒绝用直线距离顶替，见其模块 docstring 第 3 条）
→ 这三个维度被硬压在 50 分。而稀疏社区点位少、几乎人人有实测值，六个维度时效分反而满分。

**设施更全的社区得分更低**，这与事实相反，会直接误导评审。

修复：新增 `select_measurement_targets_fair()`（分层挑选）——先给每个维度留一个最近点，
再用全局最近点填满剩余名额。**K 总量、批次数量、配额消耗完全不变**，改变的只是名额分配。
修复后：

| 样例 | 总分 | 等级 | 六维雷达最小值 |
| --- | ---: | --- | ---: |
| `beijing-jiaodaokou` | 98.24 | A | 100 |
| `beijing-tiantongyuan` | 80.97 | B | 75.0 |
| `beijing-xiaojiahe` | 44.65 | D | 0 |

排序符合建成形态的事实。这处偏离 PRD 字面口径的理由、证据与回退方式（`selection="nearest"`）
写在 `services/coverage.py:SELECTION_FAIR` 的长注释与 `docs/DESIGN.md` 里。

### 2.3 pydantic 把 `true` 强转成 `1`，让「1 米的检索半径」静默成功

`{"radius": true}` 在 `int | float | str | None` 上会命中 `int` 分支被强转成 `1`，
一路跑到 `/place/v2/search` 并**返回 200**：报告看起来完全正常，实际只搜了脚下一平方米。
同类问题还有 `{"minutes": true}` → 1 分钟等时圈。

修复：在 `api/poi.py` 里加 `reject_bool()`，以 `mode="before"` 的 `field_validator`
挂在 `radius` / `minutes` 上（必须在 pydantic 强转**之前**才拦得住），
`api/analysis.py` 复用同一个函数。这类**类型错**走 422 并点名字段；
业务**范围错**（模式白名单、半径不可解析）仍走 400 + `detail.field`——两条路径职责不同。

### 2.4 CI 的端到端冒烟脚本写了一个必然 `KeyError` 的断言

`.github/workflows/ci.yml` 里原本写的是：

```python
assert body["data_mode_in_effect"] == "mock"   # ← /api/health 顶层没有这个键
```

`/api/health` 的响应有**三个名字都像「数据模式」的字段**，位置和值域各不相同：

| 字段 | 位置 | 值域 | 含义 |
| --- | --- | --- | --- |
| `data_mode` | **顶层** | `live` / `cache` / `mock` | 本次生效的模式（业务该读这个） |
| `data_mode_configured` | 顶层 | `auto` / `live` / `cache` / `mock` | `.env` 里写的原始值 |
| `data_mode_in_effect` | **`config` 子对象** | `live` / `cache` / `mock` | 配置层解析后的回显 |

这条断言会让 CI 在**第一次推送时就 `KeyError`**，而它此前从未被运行过 ——
因为 CI 只在 GitHub 上跑，本机没有执行它。

修复与防复发：

1. 断言改用顶层 `data_mode`，并顺带补上 §6.2 的零配置横幅断言（`banner.show` / `level`）
   与 `probe.short_circuit`（无 AK 时不许发起探测请求）；
2. **把 CI 里那段 Python 从 YAML 中抽出来原样执行了一遍**（零 AK 环境，退出码 0）——
   没跑过的 CI 脚本不算验证过；
3. 新增 `tests/test_health_contract.py`（10 例）把这三个字段的**位置与值域**钉死，
   其中 `test_data_mode_in_effect_is_not_a_top_level_key` 就是这条事故的回归断言；
4. 同步修正 `README.md`、`tools/README-deploy.md` §5、`Dockerfile` 的 HEALTHCHECK 注释里
   同样含混的表述 —— 它们正是这个错误的信息来源。

### 2.5 前端只认 `Polygon`，把 `MultiPolygon` 的盲区块整个丢掉

补 FR-222（盲区上图）时，`tools/map_layers_smoke.mjs` 报了第一条不对账的数字：

```
FAIL FR-222：盲区块真的画进了 #g-blind — 画了 3 块，报告里有 4 块
```

查下来是**后端合法输出、前端没接住**：

* `services/blind_spots.py:blocks_to_geojson()` 在连通块**边界不连通**时会输出
  `MultiPolygon`。实测交道口的 4 个盲区块里，第 4 块正是 **3 个分离环**的 MultiPolygon；
* 我第一版只写了 `geojsonOuterRing()`，取 `geometry.coordinates[0]` 当外环。
  对 `MultiPolygon` 来说这一层是「第一个多边形的环数组」，逐点 `Number(point[0])`
  得到 `NaN` → 该块被静默跳过。

**表现是「报告说 4 块盲区、地图上只有 3 块」，且控制台一条错误都没有** ——
又是一次假绿，和 §2.3 的 `true → 1` 是同一类。

修复：`geojsonRings()` 同时处理 `Polygon` 与 `MultiPolygon`，返回**外环数组的数组**；
`blindBlocks()` 的每个块带 `rings: [[…], …]`；SVG 与 JSAPI 两条绘制路径都按环迭代
（同属一个块的多个环共用同一个 tooltip，块号不会重复）。修复后实测：

```
4 块 → 6 个多边形（含 1 个 MultiPolygon）
```

回归覆盖：`tests/test_web_smoke.py::test_isochrone_js_handles_polygon_and_multipolygon`
（并断言旧的 `geojsonOuterRing` 写法不得复活）+
`tools/map_layers_smoke.mjs` 的环数对账。

### 2.6 🔴 地点检索漏了 `scope=2`，让整条 POI 分类链路失效（最严重的一个）

**现象**：接入真实 AK 后，配套密集的老城社区（交道口）被报成 **95.27 % 完全盲区**，
而离线演示里它是 3.53 %。

**根因**：`/place/v2/search` 的 `scope` 参数取 1 或留空时只返回基本信息；
**`tag` / `classified_poi_tag` / `type` / `distance` 全都只存在于 `detail_info` 里，
而 `detail_info` 只在 `scope=2` 时才返回**。`infra/baidu_client.py:search_place()` 支持这个参数，
但 `services/poi_search.py` 从来没传过。实测（同一入参只差这一个参数）：

```
scope 缺省 → results[0] 的键 = address, area, city, detail, location, name,
                              province, street_id, telephone, uid     ← 没有 detail_info
scope=2   → 多出 detail_info = tag「医疗;综合医院」/ classified_poi_tag
                              「医疗;医院;综合医院」/ type「hospital」/ distance 1000
```

少了它的后果**不是报错**：`normalize_subcat()` 拿不到任何结构化文本 → 每个 POI 判成 `other`
→ FR-221 的盲区主判定找不到任何市场/药店/小学 → 把密集社区报成 95 % 盲区。

**为什么离线阶段完全看不出来**：`SyntheticPoiSamples` 合成的报文**自带 `detail_info`**
—— 它按「百度形状」合成，而那个形状是 **scope=2 的形状**。
**合成数据比真实数据更完整，反而把这个 bug 盖住了。** 这是本项目最值得记的一条教训：
最内层降级数据不能只是「形状像」，它必须与真实返回的**字段有无**也一致，
否则它会把真实链路的缺陷伪装成正常。

**修复**：新增 `services/poi_categories.py:SCOPE_DETAIL = 2`，在 `_params_for_place()`
的参数镜像与 `search_place()` 调用点**两处**都带上（参数镜像必须与调用点逐字段一致，
否则缓存键分叉、命中率静默归零）。

**效果**：交道口盲区 95.27 % → **62.29 %**，`subcat` 开始正确落在
`hospital` / `primary_school` / `park` / `bus_stop` / `restaurant` / `bank` 上。

回归覆盖：`tests/test_coord_param_table.py::test_poi_layer_requests_detail_scope`
（三条：客户端确实发出 `scope=2`、POI 层参数镜像与调用点一致、不带时不发该参数）。

### 2.7 基线生成会混进缓存里的真实数据，让「可复现」失效

**现象**：`tests/test_samples_baseline.py` 在**全量跑**时失败，**单独跑该文件**却通过。

**根因**：三层降级链是 `live → cache → sample`，而 `data_mode="mock"` 只表示「不出网」，
**不表示「不读缓存」** —— `poi_search._resolve_dimension` 在进入 mock 分支之前照样先读一次
本地 SQLite。于是同一台机器上：

* 跑过 live 的人，`gen_samples.py` 生成出来的基线混进了**真实 POI**；
* 没跑过的人生成出来的是**纯合成 POI**。

两者 `counts_in` 与盲区面积完全不同。实测差异：

| | 交道口总分 | 盲区 | `counts_in` |
| --- | --- | --- | --- |
| 缓存里有 live 数据时 | 67.80 C | 47.63 % | `medical 10, shopping 4, …` |
| 关掉缓存后 | **98.24 A** | **3.53 %** | `medical 6, shopping 9, …` |

这个污染还**连带制造了一个假象**：一度看起来「老城(67.8) < 新建大盘(80.97)」，
像是 FR-102 的「三型差异方向」被破坏了 —— 其实那只是缓存污染的副作用，
清掉缓存后排序完全正常（98.24 > 80.97 > 44.65）。

**修复**：`tools/gen_samples.py:generate_one()` 用
`get_settings().model_copy(update={"cache_enabled": False})` 生成基线。
基线的契约是「在没有任何真实数据的环境下 mock 长什么样」，所以生成时必须关缓存。
**不改运行期语义**，只影响这个生成器。

### 2.8 `--verify` 会把「只解析到城市级」的坐标标成「已核验」

**现象**：第一次跑 `gen_samples.py --verify` 时它「成功」了，但给出的坐标是错的：

| 查询 | 拿到的坐标 | 行政区 | 官方 `level` |
| --- | --- | --- | --- |
| 交道口街道 | 116.4073, 39.9422 | 东城区 | **乡镇** ✅ |
| 天通苑 | 116.4134, 39.9109 | **东城区东华门街道** ❌ | **城市** |
| 肖家河 | 116.4134, 39.9109 ❌ | **东城区东华门街道** ❌ | **城市** |

「天通苑」在昌平区，却被解析到东城区的市政府地址；「肖家河」拿到一个**一模一样**的坐标。
而原实现**只要 `lnglat` 非空就写 `coord_verified: true`** —— 那是在核验记录上撒谎，
比不核验更糟。

同时它**永远跑不通**：`data_mode=auto` 要等 `/api/health` 的启动探测才解析成具体值，
而命令行进程从不跑那个探测，所以 `data_mode_in_effect` 恒为保守兜底的 `mock`，
每次地理编码都返回 `degraded_reason=mock_mode`。表现是「明明配了 AK，工具却说拿不到坐标」。

**修复**（三条规则，全部写进工具的 docstring）：

1. **必须显式传 `data_mode="live"`**，不依赖进程里的探测结果；
2. **优先走 `/api/suggest`**（PRD §10-1 规程②明文指定的路径，也是唯一能给出 POI 级精度的接口），
   并按「精确同名 > 以关键字开头 > 名字更短」排序候选 —— 百度把「天通苑-地铁站」排在
   「天通苑」住宅区之前，而清单要的是社区中心点；
3. **地理编码只作兜底**，且**拒绝 `level == "城市"`** 的结果，并**交叉核对行政区**：
   最终坐标的 `district` 必须与清单里写的一致，不一致就拒绝该社区、**整次不改文件**。

回归覆盖：`tests/` 无（这是运维工具，不在 pytest 范围），但 `--verify` 的拒绝路径
已在上表用真实数据验证过。

### 2.9 🔴 `market` 提示只认 `live`，同一份数据第二次跑就悄悄改口

**现象**（2026-10-10 真实浏览器复验 JSAPI 底图时顺带抓到）：交道口连点两次「一键体检」。

| 第几次 | 徽标 | `place_calls / quota_units / http_requests` | `notes` 里有没有「盲区面积系统性偏大」 |
| --- | --- | --- | --- |
| 1 | 实时调用 | 7 / 12 / 8 | **有** ✅ |
| 2 | 本地缓存 | 0 / 0 / 0 | **没有** ❌ |
| 3（修复后） | 本地缓存 | 0 / 0 / 0 | **有** ✅（新文案） |

两次跑的是**同一份 POI 报文**（第二次只是从 SQLite 里读回来），分数都是 54.0 D、盲区都是 3 块，
但提示在第二次消失了。

**根因**：判定条件写成 `poi_result["data_source"] == "live"`。而 POI 层的取数层出口有三态
（`services/poi_search.py:_overall_source`：任一组 `live` → `live`，否则任一组 `cache` → `cache`，否则 `sample`），
**`cache` 装的就是上一次真实检索的原样返回**，`market` 恒为 0 这条缺陷一分不减。
只认 `live` 等于宣布「缓存里的真实数据没有这个问题」，那是假的。

**修复**：条件改为 `in ("live", "cache")`，并把提示首句从「本次为实时检索」改成
「本次 POI 层来自真实检索（或其本地缓存复用）」——文案也得能覆盖 `cache`，否则读者会以为在说别的东西。
`sample` 仍然**不挂**：合成层的 `market` 是按画像生成的、本来有值，挂上就是制造一个不存在的问题。

**回归覆盖**：`tests/test_analysis_api.py::test_real_retrieval_warns_that_blind_spots_are_biased_up`
按 `[live, cache]` 两个参数各断言一次（用只换 `data_source` 的 POI 桩服务，零出网），
`test_sample_layer_does_not_claim_a_problem_it_does_not_have` 反向断言 `sample` 不挂。

**这条为什么值得单独记**：它是**第四个**「只有接了真实 AK 才看得见」的缺陷（前三条见 §2.6 ~ §2.8），
而且和前两条不同——它不在数据层，在**我自己为了「诚实披露缺陷」而写的那段代码里**。
用 mock 报文跑的单测永远只会喂 `live` 或 `sample`，`cache` 这一态要有一份真实缓存才会出现。

### 2.10 🔴 七类全选时提示写的是「本次只检索这几类」——`node --check` 与 451 例单测全绿拦不住

**现象**（2026-10-11 真实浏览器复验 FR-104 时抓到）。`web/isochrone.html` 的七个勾选框默认全勾，
但页面加载后 `#category-hint` 渲染出来的是：

| 勾选态 | 修复前 `data-state` | 修复前首句 | 修复后 |
| --- | --- | --- | --- |
| 7/7 全选 | `subset` ❌ | 「已勾选 7/7：本次只检索这几类…」 | `all`：「七类全选 = 不限制类别（后端缺省）…」 |
| 5/7，不碰盲区三类 | `subset` ✅ | 正确 | 不变 |
| 4/7，碰了盲区三类 | `hurt` ✅ | 正确 | 不变 |
| 0/7 | `none` ✅ | 正确 | 不变 |

只有全选那一格是错的，而它恰好是**用户进页面第一眼看到、也是缺省态**的那一格。

**根因**：`renderCategoryHint()` 里 `all` 只写成了一个初值（`let state = "all"`），
`if / else if / else` 链**没有 `on === total` 这条分支**，于是全选必然落进末尾的 `else`（subset）。
而 subset 那句描述的是「收窄之后的口径」——它宣称「每类一次地点检索请求」
「总分会按这几类的权重之和重新归一」，这两件事在全选时**都没发生**（全选时前端根本不发 `categories` 键，
后端也就不存在按子集归一）。这是一句假陈述，不是措辞不当。

**为什么门禁没拦住**：`node --check` 只验语法，初值与分支写法在语法上无可指摘；451 例单测里
**没有任何一例断言过全选态的文案**。这是本条最值得记的点——纯文本分支错误，只有把页面打开、
不看逻辑只看字才看得见。

**修复**：改成四条显式分支、按「最严重优先」排序（`none` → `hurt` → `all` → `subset`），
函数上方留 ⚠️ 注释写明「`on === total` 必须单独成一条」；
HTML 里 `#category-hint` 的静态首屏文案一并改成与 `all` 分支**逐字一致**——JS 一加载就覆盖它，
两边写岔就是「刷新瞬间看到第五套口径」。

**回归覆盖**（`tests/test_web_smoke.py`，三条各钉一个失效面）：

* `test_category_checkboxes_match_the_backend_wordlist`：断言 `isochrone.js` 里同时出现
  `state = "all"` 与 `on === total`（少一条分支就红），并要求 HTML 静态文案能在 JS 里逐字找到；
* `test_category_hint_states_are_styled`：态名单**从 JS 里取**（`state = "([a-z]+)"`）而不是测试里手写，
  要求恰好 `{all, none, subset, hurt}`，且除 `all` 外每态都在 CSS 里有 `[data-state=…]` 规则
  ——否则加第五态时手写名单会静默通过；
* `test_category_selection_is_only_sent_when_narrowed`：钉住「全选折叠成 `null`、零勾选挡在 `fetch` 之前」。

**同一次浏览器验证里顺带自证的零出网**（这台机器已配真实 AK，所以必须验证没有烧配额）：
服务端用空值覆盖 `.env`（`BAIDU_SERVER_AK= BAIDU_BROWSER_AK=`）起在 `127.0.0.1:8795`，
`/api/health` 报 `server_ak_type: not_configured`；一轮 `mock` 走查（天坛 `116.4074,39.9042`）的
network 记录 **8 条、全部指向 `127.0.0.1`**（`health` / `samples` / 2 次 `POST /api/analysis` / 4 次
`POST /api/isochrone`），响应头 `x-http-requests: 0 / x-place-calls: 0 / x-quota-units: 0`。
收窄五类（`medical,leisure,transit,food,service_ref`）那次的请求体带
`"categories":["medical","leisure","transit","food","service_ref"]`，页面结果
「完成：51.7 分（D），耗时 83 ms。 地图叠加：21 个圈内设施 + 1 块盲区。」，`notes` 里同时渲染了盲区警示
与「已按计分权重之和 0.60 归一化到 0~100」；**同一中心点全选那次的请求体里没有 `categories` 键**，结果
「完成：44.6 分（D），耗时 15 ms。 地图叠加：23 个圈内设施 + 1 块盲区。」。四态文字色实测
`rgb(107,114,128)` / `rgb(169,100,10)` / `rgb(207,34,46)`。逐条出处见 `docs/shots/README-isochrone-acceptance.md`。

### 2.11 🔴 上一轮写进文档的「冒烟自测 12 项实测」是**手抄的**：FR-104 提交时那条脚本已经崩了

**这一条是本文自己的不实陈述，性质和 §2.10 一样严重。**

提交 `3047917`（FR-104）里，本文有两处把 `tools/map_layers_smoke.mjs` 写成**跑过并且 12 项通过**：
§6 的「浏览器内真实渲染」行（`**12 项**，grep -c 'OK ' 实测`）和 §7.1 的复核表
（`map_layers_smoke.mjs 断言条数 … 12 … 此前文档写的 12（对）`）。

**而在那同一个提交上，这条脚本一条断言都没跑。** 可复现证据——把 `3047917` 的两份文件取到临时目录直接执行：

```
$ git show 3047917:tools/map_layers_smoke.mjs > T/tools/map_layers_smoke.mjs
$ git show 3047917:web/isochrone.js           > T/web/isochrone.js
$ node T/tools/map_layers_smoke.mjs _map_input.json
=== isochrone.js SVG 舞台绘制路径（真实 /api/analysis 响应）===
  FAIL 加载 isochrone.js — isochrone.js:196
const catBoxes = Array.from(document.querySelectorAll("#categories input.cat"));
TypeError: document.querySelectorAll is not a function
$ echo "exit=$?  OK行数=$(… | grep -c 'OK ')"
exit=1  OK行数=0
```

`0`，不是 `12`。

**根因**：FR-104 在 `web/isochrone.js` **模块顶层**加了 `document.querySelectorAll("#categories input.cat")`，
而冒烟脚本的 `documentStub` 只桩了 `getElementById` / `querySelector` / `createElement` / `addEventListener`——
**桩件缺一个方法，脚本在加载阶段就死**，后面的绘制路径根本没执行到。

**为什么没被发现**：那一轮 `pytest` / `ruff` / `black` / `node --check` 确实全绿，只有这条脚本没重跑，
于是 12 这个数字是从**上一轮输出抄来的**。讽刺的是 §7.1 立的正是「自测数字也要用命令复核」这条规矩，
**写规矩的那一轮就在这条上破了它**。

**修法**：给 stub 补 `querySelectorAll: () => []`，并在注释里写明它等于「全选 + 桌面态」——
FR-104 的勾选对账、FR-106 的抽屉接线都由 `tests/test_web_smoke.py` **对真实 HTML/CSS 断言**，
不靠这个桩去验语义。

**顺带得到一条更一般的纪律**：`grep -c 'OK '` 在崩溃的输出上照样返回一个数（这里是 `0`），
所以复核门禁的命令必须**同时看退出码**，只看计数就分不清「跑绿了」和「跑都没跑」。
本轮起本文引用的每个冒烟数字都按 `node … ; echo exit=$?` 的形式重跑过。

### 2.12 移动端抽屉：地图高度多扣了页脚与图例；把手条轻点能开不能关

FR-106 落地时在 DevTools 里真量出来的两个缺陷，都属于「代码写完、样式看着也对，但没读过几何」。

**① 地图没占满首屏——扣减算重了。** 第一版 `sizeSheetStage()` 按「视口高 − 页头 − 横幅 − 图例 − 页脚」
逐项累加，量出来 375×667 下只剩 ~240 px：抽屉做得再对也白搭。根因是把**滚动后才看得见**的块
也算进了「首屏」扣减，而 FR-106 要的是地图占满首屏。改成只量 `.stage-area` 自己的上边界：

```js
const top = el.stageArea.getBoundingClientRect().top + window.scrollY;
const height = Math.max(240, Math.round(window.innerHeight - top - bar));
```

量几何而不是按 class 逐个累加，好处是 `margin`、`gap` 和 `[hidden]` **已经体现在结果里**，
不需要在 JS 里把「这块现在到底可不可见」再判一遍。
附带一个不报错的空白：横幅显隐会改变 `top`，所以 `hideBanner()` / `setBanner()` /
`renderQuotaNotice()` 的两个出口、加上 `renderSheets()` 本身，**五处**都要重算
（`grep -n "sizeSheetStage()" web/isochrone.js` 可数）——漏掉的话关掉横幅后 `--map-h` 停在旧值，
地图下方留一条横幅高的空白，**控制台一个字都不出**。

**② 把手条轻点只能开、不能关。** grip 是个 `<span>`（没有 click 监听），抬手时 `moved < 6` 的轻点
走到「按位移决定开合」的分支，位移 0 就是「保持原样」，于是永远关不掉。修成轻点按展开态取反：

```js
if (moved < 6) { openSheet(startExpanded ? "" : key); return; }
```

这也是 **D-1 那条老教训第 N 次应验**：`setPointerCapture` 会把后续 pointer 事件改投递到 capture 目标，
所以拖拽的 `pointermove/up` 一律绑在 `window` 上，不绑元素。

**③ 两条验证方法的坑**（不是代码 bug，但会让人误判「修复没生效」，所以记在这儿）：

* 改了 CSS 之后 `navigate_page` 可能复用缓存的样式表，`getComputedStyle` 读回来还是旧值。
  必须 `reload {ignoreCache: true}`，并且在信几何之前先确认那条规则**真的在 `document.styleSheets` 里**。
* `openSheet()` 之后**立刻** `getBoundingClientRect()` 读到的是 `transition` 中途的位置：
  414×896 那次读到 `top: 896 / translateY(643.35)`，落定后是 `top: 253 / translateY(0)`。
  更反直觉的是**在同一个 `evaluate_script` 里 `await 400 ms` 也不会落定**——这个 headless 通道的
  过渡帧只在 CDP 往返之间推进，所以落定态必须**分两次调用**读。本文 §6 表里那两组展开态数字都是这么量的。

### 2.13 🔴 `.env` 里写 `SUGGEST_ENABLED=true` 曾经**静默无效**：pydantic-settings 不写回环境变量

FR-108 的后端开关最初只有一处读取——`services/geo.suggest_enabled()` 在没有 `Settings` 实例时
兜底 `os.environ.get("SUGGEST_ENABLED")`。看上去「`.env` 里改一行就能开」，实际改了也白改：
**pydantic-settings 的 `env_file` 只把值填进模型字段，不会写回 `os.environ`**。
于是 `.env` 里那行从来没有到达过任何一处判断，用户照着文档改完配置，输入框依旧回「未启用」，
而**没有任何一处报错**。这类「配置项存在但没人读」的键一旦进了 `.env.example`，
文档与行为之间的距离就只能靠人肉发现。

修法不是补一句「请用 shell 环境变量」的说明，而是把开关做成 `infra/config.py` 里的真字段
（`suggest_enabled: bool = False`），让 `.env` 有唯一生效路径；`suggest_enabled(settings)`
退化成「有实例读实例、没实例读环境变量」。回归用例是
`tests/test_geo_api.py::test_suggest_switch_reaches_the_service_from_dotenv`：它往 `tmp_path`
写一份 `.env`，断言 `Settings(_env_file=...).suggest_enabled is True`，
同时断言 `Settings().suggest_enabled is False`——「默认关闭」这条不能被 `.env` 机制顶掉。

**判据**：凡是靠 `os.environ` 读的配置键，都必须有一个同名的 `Settings` 字段兜着，
否则 `.env.example` 里那一行是假的。

### 2.14 候选列表在移动端抽屉里「打字有反应、候选看不见」：文档流弹层必须自己滚进视口

FR-108 的 `#suggest-list` 是**文档流里**的一块 `<ul>`，不是 `position: absolute` 的浮层。
375×667 实测（`?profile` 不设、`data-sheet-mode="mobile"`、展开「参数」抽屉）：
`#controls` 自己是滚动容器（`clientHeight 440` / `scrollHeight 2653`），
`#addr` 落在 `top 568`（贴着抽屉底沿），紧跟其后的候选列表落在 **`top 742 / bottom 897`——整块在 667 视口之外**。
表现是「字已经打进去了、下面什么也没冒出来」，而代码、样式、断言全是对的。

修在状态的唯一写入口 `setSuggestState()`：进入任何可见态都补一句最小滚动。

```js
if (value !== "none") el.suggestList.scrollIntoView({ block: "nearest" });
```

复测同一台设备：`#controls.scrollTop` 由 `0` → `282`，列表 `top 460 / bottom 615` 完整落在视口内，
三行候选高度 `44 / 66 / 44`（`min-height: 44px` 生效），`documentElement.scrollWidth` 仍是 `375`（没撑出横向滚动）。
桌面侧同一句是**空操作**：先把输入框滚进视口再打字，`window.scrollY` 前后都是 `386`（`jump 0`），
列表 `top 535 / bottom 714` 本来就在视口内——`block: "nearest"` 的语义就是「只在真看不见时才动」。

这条与 §2.12 的「地图高度扣错」是同一类错误：**改了 DOM 结构没读几何**。
`static` 定位的下拉列表落在哪个滚动容器里，决定它到底可不可见；
`tests/test_web_smoke.py` 里那句 `scrollIntoView({ block: "nearest" })` 断言就是钉住这一点的。

### 2.15 🔴 一键体检把 `request_id` 整个丢了：一个渲染函数服务两种信封形状（已修）

FR-413 的页脚要在现场对服务端日志报号，而实测**主路径报不出来**：`POST /api/isochrone` 那条能显示，
`POST /api/analysis`（页面上「一键体检」就是它）跑完页脚一直停在 `request_id：—`，
「结果元信息」里那一行同样空。号拿不到 = 出问题时用户只能说「我按了体检」，日志里几十条号没法指认。

根因不是前端读错字段，而是**两条路径的信封形状不同**（本轮在 `127.0.0.1:8804` 的 mock 服务上实测，
`x-http-requests: 0` 全程零出网）：

* 顶层键：`ok, meta, isochrone, isochrones, coverage, blind_spots, score, summary, usage, assertions,
  notes, task_id, request_id` —— **`request_id` 在顶层**，且响应头 `x-request-id` 同值。
* 交给地图渲染的 `isochrone` 子对象 19 个键里**没有** `request_id`（`'request_id' in payload.isochrone` → `False`）。
* 旧代码 `git show HEAD:web/isochrone.js` 第 717 / 725 行读的就是 `result.request_id`，
  那条路径上 `result` 永远是那个没有号的子对象。

**第一版修法是错的，而且是跑过之后才知道错的。** 把 `setRequestId()` 放在 `adoptIsochrone()` 之后
（按调用顺序抢写页脚文本），实测页脚**仍是 `—`**：一次体检里 `renderAll()` 会被 `adoptIsochrone`
与 `setOverlays` **各调一次**，后一轮把文本重写回去了。

修法因此换成**状态兜底**而不是顺序依赖：号存进 `state.requestId`，取号统一走
`currentRequestId(result)`（结果自带优先，否则回落到本页最近一次请求的号），页脚与「结果元信息」
那一行都读它；`report.js` 的交号动作放在 `readJson` 之后、**判 `ok` 之前**——失败时最需要号，
排在 `if (!response.ok …) return` 后面就等于失败路径上一条也交不出去。

🔴 存进 `state` 随即引入**第二个风险**：兜底可能报一个**过期**的号（对不上服务端日志里这一次请求，
比报 `—` 更糟）。所以 `/api/isochrone` 那条路径也必须刷同一个格子，且**过期响应不许刷**
（沿用 `runSeq` 的世代判定，与「过期结果不抢视图」同一条规则）。三条真按钮序列实测：
「开始分析」`9034cf7e71fe4942` → 「一键体检」`4334bb27c8c04fe3` → 再「开始分析」**`cc527b798c894024`**
（没有停在上一份体检的号上，`dt=request_id` 同值），全程 `reqid=200/201/202` 全在 `127.0.0.1`。

三步 A/B/C 复现（同一份响应，`request_id = 14e8a8fd085b4924`）：

| 步 | 操作 | 页脚 |
| --- | --- | --- |
| A | `setRequestId("")` → `adoptIsochrone(payload.isochrone, {force:true})` | `request_id：—`（**旧行为复现**） |
| B | `setRequestId(payload.request_id)` | `request_id：14e8a8fd085b4924` |
| C | `setOverlays(...)`（触发第二次 `renderAll()`） | 仍是 `14e8a8fd085b4924`（**不再被重写**） |

真实 UI 复测两条路径都带上号：桌面 1280×800 点「一键体检」→ 页脚 `dfe369002be847a6`、
「结果元信息」`dt=request_id` 那行同值；移动 375×667×3 → 页脚 `d398a2bfe9fc44d7`、dl 行同值。
守卫是 `test_footer_request_id_survives_the_analysis_path`，它钉的是**行为**
（兜底必须读 `state.requestId`、交号必须在判 `ok` 之前），不是调用顺序——
钉顺序的那版断言在 C 步之前也会全绿，正是它的失败方式。

### 2.16 页脚对比度：上一轮我算错的 4.49，真值 4.51 —— 以及它顺手盖住的那笔欠债（已修）

这一条有两个错，第二个更难看。

**第一个是判断错。** FR-413 落地时页脚正文沿用了 `--muted`（#6b7280）。先确认比对对象没选错：
浏览器里 `.foot-legal` 的 computed 值是 `color rgb(75,85,99)`、**向上第一个非透明背景
`rgb(246,247,249)`**（正是 `--bg`）、`font-size 13px`，所以「页脚坐在页面底色上、13 px 正文、
AA 硬线 4.5」这套前提是对的。错的是算术：上一轮写进 README / `web/app.css` 注释 /
测试 docstring 三处的 **4.49:1 是手算失误**，用仓库自己的 `wcag_ratio()` 重算得 **4.51**，
即**刚好过线 0.01**而不是「差 0.01 不过」。三处已同步改成真值。

**第二个才是真问题**：4.51 这种余量对一段「条款 7.3 要求的显著披露」是不够的，因为它取决于底色。
把同一支 `--muted` 摆到本项目调色板里**五个真实底色**上逐个量：

| 底色 | 变量 | `--muted` #6b7280 | 页脚现用 #4b5563 |
| --- | --- | ---: | ---: |
| `#ffffff` | `--card` | 4.83 | 7.56 |
| `#fff7e6` | `--warn-bg` | 4.54 | 7.09 |
| `#f6f7f9` | `--bg`（页脚实际所在） | **4.51** | **7.05** |
| `#eaf6f0` | `--ok-bg` | 🔴 **4.36** | 6.82 |
| `#e3e6ec` | `--line` | 🔴 **3.87** | 6.05 |

两个直接不过线，最紧的一个只剩 0.01。`--muted` 的语义是「次要文字」，而合规披露不是次要文字——
借用它是类别错误，底色一挪就掉线。改成页脚专用的 `#4b5563` 后五个底色全过（6.05 ~ 7.56）。

🔴 **诚实标注这条守卫的能力边界**：`test_footer_disclosure_passes_aa_contrast` 断言 `ratio >= 4.5`，
而旧配色是 4.51 —— **这条断言拦不住旧配色**，它拦的是以后有人把页脚换成更浅的灰。
所以表格里那五个数字是这条改动的真正依据，测试只是防止回退。

**顺带记一笔同族的第三个错**：本轮第一次跑 `black --check tests/` 就是 `exit=1`
（`test_web_smoke.py` 一处 `'...'` 该写成 `"..."`），而上一轮文档里登记的是「black 全绿」——
说明那次跑的不是最终文件状态。改成 double-quote 后 `17 files would be left unchanged`、`exit=0`。

### 2.17 🔴 CI 的「零公网外链」哨子不分标签，把 FR-413 的合规外链判成公网资源（已修）

**发现方式**：`a2d7d8a` 推上去之后 CI 红。这一条不在本地任何门禁的能力范围内——
本地 `pytest` / `ruff` / `black` 全绿，红的是 GitHub Actions 里一条**与测试重复但不等价**的检查。

**三个 job 的实测状态**（2026-10-11，`GET /repos/zys1101/CircleDoc/commits/a2d7d8aa…/check-runs`，
未带 token 也可读；run id `38128925252`）：

| job | `status` | `conclusion` |
| --- | --- | --- |
| `test（pytest，零 AK）` | completed | ✅ success |
| `lint（ruff + black + 密钥扫描）` | completed | 🔴 **failure** |
| `build（Docker 镜像 + 无 .db 断言）` | completed | skipped（`needs: [lint, test]` 被 lint 拦住） |

🔴 **哪一步红的，不能从 CI 日志直接证明**：`GET /repos/…/actions/jobs/114439010462/logs`
回 `403 Must have admin rights to Repository`（公开仓库的作业日志要鉴权）。所以判定靠
**把 lint 的四个命令步逐条在本地复跑**，只有一条能复现红：

| 复跑的命令步 | 结果 |
| --- | --- |
| `ruff check . --output-format=github` | `exit=0`（版本区间 `>=0.16,<0.17`，本地 0.16.10） |
| `black --check --diff .` | `exit=0`（`>=26.5,<27`，本地 26.5.1） |
| 密钥扫描那串 `grep … \| grep -vE '^\./(docs/\|README…)'` | `exit=1` = 没有命中 = 该步通过 |
| `grep -nE '(src\|href)="(https?:)?//' web/*.html` | 🔴 `exit=0`，**命中 4 行** |

命中的 4 行正是 FR-413 新加的两条官方外链，每个页面各一条：
`web/index.html:76`、`web/index.html:77`、`web/isochrone.html:453`、`web/isochrone.html:454`
（`https://lbsyun.baidu.com/index.php?title=openprivacy` 与 `…?title=open/law`）。

**为什么这是检查写错，而不是页脚写错**：这条步骤守的是 R12——「Vue / ECharts / 样式必须本地
vendored，否则断网现场白屏」，白屏的成因是**下载期就要取资源**。`<a href>` 是点击才导航的文档
链接，断网首屏照样渲染得出来。而条款 7.3 明文要求页脚「附其隐私政策链接」——
粗 grep 与合规要求本来就冲突，只是 FR-413 之前页脚没有任何外链，所以一直没暴露。

🔴 **真正的教训是「同一个规则别有两套判定」**：这一轮已经把 `tests/test_web_smoke.py` 的外链哨子
做成了标签感知（`ASSET_TAGS` + 反向白名单 + `rel` 钉死，54 例全绿），而 CI 里那条注释写着
「与 `tests/test_web_smoke.py` 的断言同源」的 grep **没有跟着改**。多出来的这条更粗的门禁不是双保险，
是第二个真相源：它会红在测试已经绿了的地方，而且只在 CI 红。

**改法**：CI 步骤换成一段同样标签分类的内联 Python（**故意不 import 测试代码**，保留「第二双眼睛」的意义），
只查 `script` / `link` / `img` / `iframe` / `source` / `video` / `audio` / `embed` / `object` 的
`src` / `href` / `srcset`，并在注释里写清 `<a href>` 为什么不算。双向验证：

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| YAML 仍然合法 | `yaml.safe_load` 读 `.github/workflows/ci.yml` | lint job 7 个 step，新 step 正文 24 行 |
| 现状不再误红 | 直接执行 CI 步骤正文（cwd = 仓库根） | `exit=0`，打印「零公网外链检查通过（资源标签口径…）」 |
| 真公网资源仍被抓 | 同一段正文跑在 `web/` 临时副本，注入 `<script src="https://cdn.example.com/x.js">` | `exit=1`，stderr `injected.html: <script> https://cdn.example.com/x.js` |

临时副本跑完即删。**没有放松检查**：正控制那一条就是防这个的。

**改完之后的 CI 实测**（2026-10-11，`GET /repos/zys1101/CircleDoc/actions/runs/38129396148/jobs`，
head sha `98b14781…`）：三个 job 全部 `completed / success`——

| job | 结论 | 关键步骤结论（同一 API 的 `steps`） |
| --- | --- | --- |
| `lint（ruff + black + 密钥扫描）` | success | `ruff（零告警）` / `black（格式检查）` / `密钥扫描（FR-410…）` / **`静态页零公网外链（R12 / R13）`** 四步全 success |
| `test（pytest，零 AK）` | success | `词表自检` / `合成样例基线校验` / `pytest` / `端到端冒烟` 全 success |
| `build（Docker 镜像 + 无 .db 断言）` | success | 上一轮它是 `skipped`（被 lint 拦住），这次真的跑了：`构建镜像` → 两条镜像内断言 → `零配置起容器并跑一次体检` 全 success |

⚠️ **只引用步骤结论，不引用时长**：同一份 API 给的时长是 lint 17 s / test 20 s / `构建镜像` 18 s，
短得不像一次真实镜像构建，而作业日志这一层拿不到（`403`，见上）。所以「18 秒」不当证据写，
能断言的只有**每一步的 `conclusion` 是 success**。

### 2.18 🔴 `report_dom_smoke.mjs` 里有 5 条断言**从建仓起就没执行过**（本轮加 PNG 断言时才发现）

文档里这条命令一直记的是 **15 项 OK**，`grep -c 'OK '` 也确实稳定返回 15。真相是脚本里有
**两份**「汇总 + `process.exit`」：一份卡在文件中间（FR-222 那两条之后），一份在真正的末尾。
中间那份一 exit，后面的 5 条 `check()` 就成了不可达代码——

| 从未执行的断言 | 内容 |
| --- | --- |
| `FR-213：表头可点击且带 aria-sort` | 排序接线 |
| `FR-213：升序排列后未实测的设施全部垫底` | 「未实测不当 0 分钟」在排序里也不许冒出来 |
| `FR-213：再次点击同一列切到降序` | 列内切换 |
| `FR-213：换列排序会重置为升序` | 跨列状态 |
| `FR-223：盲区清单导出按钮已接线` | 导出按钮的监听与禁用态 |

`git show <sha>:tools/report_dom_smoke.mjs | grep -c '^check('` 在**初始提交 `f24a174`** 上就是 20，
`grep -c 'process.exit'` 是 4——两份汇总从第一天就在一起。所以这不是「后来谁改坏了」，
而是**写完就没跑过**。

**为什么 15 这个数一直没被质疑**：它每次都很稳定，而且 15 是「跑绿」的样子。
§2.11 立的那条纪律（复核命令要连着看退出码）在这里**仍然不够**——退出码也是 0。
真正能拦住它的是**断言条数与脚本里 `check(` 的个数对账**，本轮补进了 §7.1 的表：
`grep -c '^check('` = 21 对 `grep -c 'OK '` = 21，两者必须相等。

**修法**：删掉中间那份汇总（保留原位一段注释说明为什么这里不能有 exit）。修完 20 条全跑，
本轮新加的 PNG 接线那条 = **21 项 OK、`exit=0`**，5 条老断言一次都没失败——
它们是**恰好一直是对的**，这一点不能反过来当成「所以没跑也没关系」。

### 2.19 PNG 导出把成功与失败提示写进了同一个被覆盖的字段（已修，浏览器实测抓到）

`exportPng()` 两条出口原本都写成「先给 `#export-hint` 填结果，再调 `syncExportState()`」——
而 `syncExportState()` 的职责恰恰是把 `#export-hint` 刷回默认文案。结果是**成功、页脚缺失、
恢复后三种状态在屏幕上显示的是同一句话**，导出反馈等于没有。

这条是**真实浏览器**里量的，不是审代码审出来的：在 127.0.0.1:8805（`mock`，AK 两个环境变量置空）
跑完体检后依次触发三次导出，`#export-hint` 三次读数完全一致——

| 触发 | 期望提示 | 当时的实际提示 |
| --- | --- | --- |
| 点 `#export-png`（正常） | 「已导出 PNG 报告卡（1 800 px 宽，约 653 KB）…」 | 默认文案「导出内容仅限**当前这一次**分析…」 |
| 抽掉 `.foot-legal` 再导 | 「PNG 导出失败：报告卡缺合规文案…」 | 同一句默认文案 |
| 恢复页脚再导 | 成功文案 | 同一句默认文案 |

同一次量到 `#export-png` 的 `<a download>` 点击确实产出了 `blob:` URL、`image/png`、**668 709 字节**，
也就是说**功能一直是通的，坏的只是反馈**。修法是把 `syncExportState()` 挪到写提示**之前**，
并在 `test_web_smoke.py` 里按**分支各自**钉住这个顺序（只看「函数里某处先调用过门禁」不够：
成功分支那次调用天然在失败分支文案之前，会把写反的失败分支放过——这条断言本身先写错过一次，
用旧的错误顺序做过反证才改对）。

**修完再量一次**（同日，同一台 127.0.0.1:8805，另一份报告 `request_id=6a5db27de42f4d6b`），
四态各读一次 `#export-hint`，这次**四句互不相同**：

| 态 | `#export-hint` 实测（逐字） | `#export-png.disabled` |
| --- | --- | --- |
| 未跑体检 | 「先跑一次体检，才有可导出的结果。」 | true（四条导出按钮一起） |
| 跑完体检（114 ms 后） | 「导出内容仅限**当前这一次**分析。PNG 是矢量报告卡，不截取地图底图，因此不触碰底图瓦片的版权与使用条款。」 | false |
| 点下 PNG 的瞬间（同步读） | 「正在合成 PNG 报告卡（矢量图表 + 合规文字带，不含地图底图）…」 | **true** ← 自锁 |
| 落定 | 「已导出 PNG 报告卡（1800 px 宽，约 655 KB）。内容仅限本次分析，不得二次分发。」 | false |

再补一组 **A/B**，专门给「拒绝出图」那条口径拿证据：把 `.foot-legal` 清空后点真按钮，提示落定为
「PNG 导出失败：报告卡缺合规文案：页脚 .foot-legal 为空。JSON / CSV 导出不受影响。」且被拦下来的
`<a download>` 点击次数 **0**；恢复页脚再点一次 → 成功提示 + 点击次数 **1**
（文件名 `circledoc-walking-116.4074_39.9042-report.png`）。也就是说出图失败这一支**真的没有落盘**，
不是"落了一张缺标识的图"。

🔎 顺带一条可复现性事实，**别把字节数当回归哨兵**：**同一份**报告连续合成字节数完全一致
（本轮 `668 709` × 3、另一份 `670 295` × 4），换一次体检就换个数——差的是卡里那行 `generated_at`
与 `request_id` 的字形，不是编码抖动。所以 `668 709` 与 `670 295` **都**是真的，
别拿其中一个去「纠正」另一个。另外：页面重载后的**第一次**导出明显慢于热跑
（热跑是 89 / 82 / 85 / 50 / 58 ms 那批），我 900 ms 的探针在冷跑那一次还没抓到下载事件，
**没量出冷跑的确切毫秒数，所以这里不写冷跑数字**。

### 2.20 🔴 桌面三栏下报告面板被栅格压成 300 px 窄条（自 `f24a174` 起存在，本轮修掉）

**现象**：1280×800 真实 Chrome 里 `#report` 宽 **300 px**。它装的是九列 POI 表和六维雷达图，
后果全部可量：`.table-wrap` `clientWidth=270 / scrollWidth=600`（桌面机上报告里的表**要横向滚动**）、
首个数据行被折成 **79 px** 高（单元格逐列折行）、雷达 svg 只剩 **280×280**、
柱状图把 `520×260` 的 viewBox 等比压到 280 宽，而它右侧那条 **541 px** 的列在同一行是空的。

**根因**：`.layout` 是三列栅格
（`minmax(260px,300px) minmax(0,1fr) minmax(280px,360px)`），而 `#report` 是**第 4 个自动放置**的子项，
于是掉到第 2 行第 1 列，宽度被第一列的 `minmax(260px,300px)` 钉死。
`grep -c grid-column` 在初始提交上就是 **0** —— 与 FR-106 移动端抽屉无关，**从第一天就在**。

**为什么改前那 506 例抓不到它**（本轮之后是 509）：`test_web_smoke.py` 里**确实有**读 CSS 的断言，
但读的都是**颜色、断点数字、44 px 热区**这类「文本级」口径（FR-413 那轮量页脚底色、FR-106 那轮量抽屉），
**没有任何一条读过 `.layout` 的轨道声明与子项放置**；`tools/report_dom_smoke.mjs` 是最小 DOM 桩、
**没有 CSSOM**，`getComputedStyle` 拿不到真实布局结果。「宽度 300」这件事只存在于把页面真打开的那条通道里
——这也是本轮那三条新断言的定位：它们**读栅格文本**（能离线跑），但**尺寸与溢出那半只有浏览器给得了**，
所以两套证据必须并存，谁也不能顶掉谁。

**修复不是一行**：只加 `grid-column: 1 / -1` 会让 `.chart { width: 100% }` 没有上限——
雷达直接涨到 **589×589**（下表 S1）。所以拆成两条：`#report` 撑满整行，**加上** `.chart` 的上限
只绑定在**抽屉断点之上**（雷达 320、柱状 520），断点之下仍 `width: 100%`，让 375 抽屉里铺满 346。

三态实测（**同一份数据、同一个 `request_id`**，注入 `<style>` 逐条中和，不是跑三次）：

| 状态 | `#report` | 雷达 svg | 柱状 svg | POI 行高 | `.table-wrap` client / scroll |
| --- | --- | --- | --- | ---: | --- |
| S0 改前（HEAD 原样） | 300 × 10 250 | 280 × 280 | 280 | 79 | 270 / 600 |
| S1 只加 `grid-column` | 1225 × 3 913 | **589 × 589** | 589 | 26 | 1195 / 1195 |
| S2 两条都加（当前树） | 1225 × 3 644 | 320 × 320 | 520 × 260 | 26 | 1195 / 1195 |

四档复测：769（单列区间）面板 714 宽、两块图堆叠、表 685/685、无横向溢出；
**1082 有无这条规则读数完全一致**（单列区间是 no-op，已证）；1122 三列、面板 1067、表 1038/1038；
1280 见 S2。375×667×3 抽屉**无回归**（`#report` 仍 `position: fixed`，抽屉顶 175 / 高 440 / 宽 375、
卡 346×203、整页零横向溢出）。

⚠️ **上一轮登记的「面板总高 7 341 px」与本轮的 10 250 px 不可比**：7 341 那次跑的是**被缓存污染**的数据
（69 行），本轮关掉缓存后是 48 行——**行数更少反而更高**，说明两次之间还有别的变量（不同的 `notes` 文案长度
与 `request_id`），我没有回到旧服务复现那一次，所以本节只引用**同一台机器、同一份数据、同一 `request_id`**
的那三态，旧数字作废而不是「被修正」。

**顺带照出 §2.7 的复发**：本轮在旧 8806 服务上复现同一份报告得 **67.8 C / 盲区 47.63 %**，
而 `pytest -q` 509 例与 `tools/gen_samples.py --check` 全绿——⚠️ **这句「全绿」有一半是测试顺序给的假象**，
本轮把 `tests/test_samples_baseline.py` **单独**跑一遍就红了：`assert 67.8 == 98.24`（§2.7 那对数字，
在**当前代码**上原样复现）。三条命令实测（每条都在这台带脏缓存的机器上跑过）：

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_samples_baseline.py` | **1 failed**, 15 passed（`67.8 != 98.24`） |
| `pytest tests/test_analysis_api.py tests/test_cache_store_singleton.py tests/test_samples_baseline.py` | 66 passed |
| `pytest -q`（全量） | 509 passed —— **该轮实测量**，§2.21 之后同一格是 513，这条顺序依赖**仍在、未修** |

`test_cache_keys_ttl.py` + 基线**仍然红**，要 `test_analysis_api.py` **加上** `test_cache_store_singleton.py`
才转绿 —— 所以真凶是**进程级缓存单例**：`run_analysis` 直接调用时用的是那时那刻的 store 单例，
前面的文件一旦调过 `reset_cache_store(...)` 把它换成 tmp 路径或禁用态，后面的基线断言就读不到真数据了。
**这条登记在 §6，本轮没修**。能确定的干净复现命令只有一条（本轮实测）：
`CACHE_ENABLED=false pytest tests/test_samples_baseline.py` → **16 passed**；
只压 `BAIDU_SERVER_AK=` 不压 `CACHE_ENABLED` 照样红。⚠️ `-o cache_db_path=...` 试过了，**压不掉**——
那是 pydantic-settings 读的，不是 pytest ini。
回到污染本身：`data/cache.db`（118 784 字节，10-10 live 验收时写的；
本轮 `select created_at` 实测 16 行全在 2026-10-10 15:27~15:32，文件 mtime 也是 10-10）里装着真实 POI，
而 `data_mode=mock` 只保证「不出网」，`_resolve_dimension` 在进 mock 分支**之前**先读一次本地 SQLite。
换成 `CACHE_ENABLED=false` 起服务后立刻回到 **98.2 A / 3.53 %**。
**真实数据没有受损**：mock 分支返回 `_sample_group` 且**不写回缓存**，那条 live 条目没被覆盖。
口径此前只写在 `tools/gen_samples.py` 的注释里，README / DESIGN / TEST_REPORT **一处都没有**；
本轮补进 README §10.4 第 3 步、本文 §7 第 1 与 5b/5c 步与验收文档。
**仍未修的是运行期语义本身**：`data_mode=mock` 到底该不该读缓存是一个产品口径问题，
不是这一轮的一行修复（登记在 §6）。

🔴 **本轮新登记的第二个问题（未修）**：`test_mock_report_matches_the_checked_in_baseline`
的**通过与否取决于测试顺序**——单独跑该文件在带脏缓存的机器上**必红**，
全量跑却因为前面的文件替换了进程级 store 单例而**恰好绿**。
详见 §2.20 末那三条命令的实测表。它的含义要说准：**「509 例全绿」并不证明基线可复现**，
只证明「在这个顺序、这台机器的当前缓存状态下整条链路没崩」。
干净口径要么按 §7 第 1 步那样显式压掉缓存，要么在 CI（无 `.env`、无 `data/cache.db`）里跑。
**修法没有一行级的**：要么给这条断言显式注入 `cache_enabled=False` 的 settings（与 `gen_samples.py` 同源，
是最小改动），要么给 `run_analysis` 加一个「本次不许读缓存」的参数——两者都要重新对账 §6.1 那句
「缓存命中下 100 % 可复现」的口径，属于产品判定而不是修 bug。

### 2.21 🔴 出网资源按 client 复制：`BAIDU_MAX_CONCURRENCY=4` 在真进程里是 **12**，熔断器看不见账号级限流（本轮修掉）

**怎么暴露的**：2026-10-13 用户在百度账号控制台实拍「并发达到 12」，同时问「地图为什么还是离线底图、
额度为什么在扣」。三个症状**只有一个**是缺陷，先把它们分开——这份文档里最容易混的一段就是这里：

| 症状 | 结论 | 证据位置 |
| --- | --- | --- |
| 地图没有真实街道、只有示意 SVG | **不是 bug**：JSAPI 底图只能在**浏览器侧**拿到 browser 型 AK 时才挂载，后端只回 `has_browser_ak` 布尔、**不下发 AK**（本轮用户口径「浏览器端的 AK 不从后端下发」）。填法见 README §5 与 `#browser-ak` 输入框 | `api/health.py` 的 `has_browser_ak` / `browser_ak_type: present_unverifiable`，`web/isochrone.js` 的挂载分支 |
| 百度额度在扣 | **不是 bug**：`.env` 是 `DATA_MODE=auto` + 真实服务端 AK，改中心点 → `/api/regeo`、点地图 → `POST /api/isochrone`、一键体检全是**真出网**；`CACHE_ENABLED=true` 只让**同参数第二次**免费 | 每份响应头都带 `X-Http-Requests` / `X-Place-Calls` / `X-Quota-Units`，见 `api/app.py:245-254` |
| 控制台并发 = 12 | **是 bug，而且是承诺被架构违背**：README 与 `.env.example` 一直写「`BAIDU_MAX_CONCURRENCY` 是在途上限」，实际是**每个 client** 的上限 | 本节 |

**根因**（一句话：一个账号被进程里的三个 client 各限了一次）。
`infra/baidu_client.py` 里每构造一个 `BaiduClient`，就顺带新建**一整套**出网资源：
`httpx.AsyncClient(Limits(max_connections=cfg.baidu_max_concurrency))`、一把
`asyncio.Semaphore(同一个数)`、一个 `RateLimitRegistry`、一个 `BreakerRegistry`。
而进程里有**三个**服务单例各建一个 client——`services/poi_search.py`、`services/geo.py`、
`services/isochrone_service.py`。百度按 **AK** 统计 QPS 与在途并发，它不认我们进程里有几个 client：

* 在途并发：`3 × 4 = 12`（控制台那个数）。
* QPS：`RATE_PLACE_QPS=0.5` 变成最坏 **1.5**，`route` 2.5 变成最坏 7.5——比官方类目上限高一截。
  **同族前例早已写在代码里**：`infra/ratelimit.py` 的 R5 注释——早期让步行 / 骑行各一个独立池、
  各取 2.5 QPS，合计 5 QPS，跑 live 必然换回 `status=401`。这次是同一个坑换了个维度。
* 最隐蔽的一条：**熔断器按 client 计数**。账号已经被限流时，client A 攒够连败把并发熔断打开，
  client B 那侧计数器还是 **0**，于是继续发请求。也就是说 FR-401 的「连续失败保护」在多 client
  进程里**基本没生效过**——它保护的是「某个 client 自己别撞墙」，而百度惩罚的是**账号**。

**实测（假传输层计数桩，一次真实出网都没有）**：同一个实验、`BAIDU_MAX_CONCURRENCY=3`、
3 个 client × 4 次并发 `geocode` = 12 个请求全部发出。

| 形态 | 各 client 在途峰值 | **进程内峰值** | 发出总数 |
| --- | --- | --- | --- |
| 修前（3 套独立池 / 闸 / 桶 / 熔断） | 3 / 3 / 3 | **9** | 12 |
| 修后（1 份进程级共享出口） | — | **3** | 12 |

9 就是 `3 × 3`；把那个 3 换回 tracked 默认值 4，就是控制台实拍到的 **12**。
两个数都在**当前代码**上复跑确认过（本轮第二次跑：`修前 3/3/3 → 9`，`修后 → 3`，`snapshot.bindings == 3`）。

**修法：`Egress` —— 进程级共享出口**（`infra/baidu_client.py`）。一个 dataclass 装住
`http`（连接池）、`rate_limiter`（桶）、`breakers`（熔断）、`max_concurrency` 与在途闸，
`get_egress(cfg)` 保证同指纹全进程只有一份；三个服务单例的客户端从此落在同一份上。
配套的四个口子：`egress_snapshot()`（可观测）、`close_egress()`（收尾关池）、`reset_egress()`（测试用）、
`_egress_fingerprint(cfg)`（并发数或超时变了就重建、旧的记进 `stale`，不让池形状被静默复用）。

**共享的判据是「什么都没注入才共享」**，不是默认全员共享。注入 `transport` / `http_client` /
`rate_limiter` / `breakers` / `clock` / `sleep` / `metrics` / 非默认 `base_url` 任一项 → 走**私有出口**。
这条边界不是为了好看：MockTransport 用例不能被真实桶拖住，而 `/api/health` 的探测是
**假时钟 + 真桶**的形状——假时钟一旦进真实桶，补令牌算术会被打乱（`elapsed` 算出来是 0 或负）。
所以 `api/health.py` 的 `_probe_client` 保持注入，它是共享出口的**例外**而不是漏网。

三个刻意的「没合并」：

* **`mode_count` 仍是 per-client**。`isochrone` 是 2（步行 + 骑行双模式，FR-206 的路线预算要按两份折算）、
  `geo` 是 1。**合并出口不等于合并预算**——把预算也合成一份会让双模式那条守卫松掉。测试①里
  用对象同一性钉住了这个差别（同一个 `Egress`、不同的 `mode_count`）。
* **在途闸按事件循环各建一把**。`asyncio.Semaphore` 在首次 `await` 时就绑死所在 loop，
  跨 loop 复用会 `RuntimeError`；所以 `_gates` 是 `weakref.WeakKeyDictionary`（loop → 闸），
  不在 loop 里时退到 `_detached_gate`。httpx 那个池仍只有一份——**单事件循环**是本项目的运行假设，
  写进了注释。
* **`close_egress()` 放在 `api/app.py` 的 lifespan 收尾**，不放在任何 service 的 `aclose()` 里：
  三个 client 共用一份出口，**谁都不是最后一个用完的人**，放在任意一个 service 上都会提前把池关掉。

**改完之后要让非开发者也能自查**：`/api/health` 顶层多了 `egress` 块（`_fallback_payload` 里也有，
降级态同样可查）。本机起服务实测三段——刚起来是 `{"built": false}`；
跑一次 `DATA_MODE=mock` 的一键体检（`request_id 1aa010e32bec4c5c`，响应头 `x-http-requests: 0`，
即**一次都没出网**）变 `{"built": true, "bindings": 2}`；再打一次 mock 的 `/api/regeo` 变 `bindings: 3`。
`bindings` 就是「挂到这份共享出口上的 client 数」，正常应为 `poi_search` / `geo` / `isochrone` = 3。

**配置**：`.env`（gitignored）把 `BAIDU_MAX_CONCURRENCY` 从 4 改成 **3**——按用户口径「并发上限 3 条」。
tracked 的 `.env.example` 与代码默认值**保持 4**：共享闸让这个数**第一次真的等于账号级在途上限**，
调它等于调对百度的压力，不该由我顺手改基线。

**本轮新登记的代价（不是 bug，是没修的取舍）**：共享在途闸会带来**跨服务的队头阻塞**——
一次等时圈的路线突发能把 3 个名额占满，此时一个 `/api/regeo` 得排队。登记在 §6，
和「没做真正的请求队列」是同一条：FIFO + 背压会把 **FR-205 前端进度轮询**（任务 #17）
从 P2 变成必做项，否则用户只看得到「转圈不动」。本轮按用户口径只做「共享出口 + 账号级熔断」两件事。

**自测**：`tests/test_egress_sharing.py` 4 例（清单见 §1）。其中第④例带一条**反向断言**
`spy.peak >= 2`：计数桩若因为签名对不上而在加载阶段就死，`peak == 0` 也会让 `peak <= 3` 绿——
这正是 §2.11 那次「文档里的冒烟数字是假的」的同族坑，所以并发上限这类断言必须同时钉「它真的发生过」。

---

## 3. 对比实验：串行 `direction` vs 批量算路（FR-405）

**复现命令**：`python tools/bench_route_strategy.py --out _bench.json`（零真实出网）

输入：同一中心点、96 个目的地（等于等时圈一次采样的路线条数，PRD §4.2）。
推算假设：RTT **120 ms**（假设值）、`route` 桶 **2.5 QPS**（PRD §4.6 建议值）。

| 指标 | 性质 | A 串行 `direction` | B 批量 `routematrix` |
| --- | --- | ---: | ---: |
| HTTP 请求数 | **实测** | 96 | **2** |
| 配额单位（`origins × destinations`） | **实测** | 96 | **96** |
| 本地 CPU 耗时 | **实测** | 0.0246 s | 0.0015 s |
| 线上墙钟耗时 | **推算** | 38.4 s | **0.8 s** |

* **请求数下降 48.0×**，**配额消耗完全相同**（`quota_units_identical = true`）。
  这正是 PRD §4.2 第 267 行的要点：**批量算路省的是 HTTP 往返与限流等待，不省配额**。
  只报「请求数」会把优化幅度说大，只报「配额」会把优化说没——两栏必须并列。
* 推算墙钟的瓶颈是**令牌桶**（96 / 2.5 = 38.4 s）而不是往返（96 × 0.12 = 11.5 s）。
  这也是为什么推算必须用真实档位：用实验里那个 1000 QPS 的假桶去推算会把主要成本整个漏掉。

### 3.1 真实出网预算下的行为（这一条比上面更有说服力）

把 `http_requests` 预算调回 §6.1 的真实上限 **20 次**，同样输入 96 个目的地：

| 路径 | 完成的请求数 | 覆盖的路线条数 | 结果 |
| --- | ---: | ---: | --- |
| A 串行 | 20 | 20 / 96（**20.8 %**） | 76 次被预算守卫截停，只能出一份不完整的等时圈 |
| B 批量 | 2 | **96 / 96（完整）** | 同样预算内完整覆盖 |

**串行路径不只是「慢」，它在 §6.1 认可的出网预算内根本跑不完。**
这是 FR-405 从「性能优化」变成「可行性与否」的那条线。

### 3.2 限流验证（§6.2 第二条，本地 mock 网络层）

把桶调到 **0.5 QPS**、`burst = 1`（PRD §4.6 要求 `burst ≤ rate`），mock 掉网络层，连发 20 个请求：

| 项 | 实测值 |
| --- | --- |
| 拿到响应的请求数 | **20 / 20** |
| 排队过的请求数 | 19（burst=1 立即放行 1 个） |
| 前三次等待 | 2.0 s / 2.0 s / 2.0 s |
| 最长单次等待 | 2.0 s |
| 总耗时 | **38.07 s** |
| 理论排队时间 `(20 − 1) / 0.5` | 38.0 s |
| **是否有请求绕过桶** | **否（零穿透）** |

实测 38.07 s 与理论 38.0 s 吻合，说明排队语义正确；
「20/20 全部拿到响应」说明**没有任何请求在穿透限流后被服务端拒绝**。

⚠️ 再次强调：这一整节都在本地 mock 层完成，**没有向 `api.map.baidu.com` 发过一个请求**。

---

## 4. 预置样例的实测结果（FR-102 / FR-412）

数据模式 `mock`、步行、15 min、桌面档（K=12）。基线签入 `data/samples/*.json`，
由 `tests/test_samples_baseline.py` 逐字段回归。

| 样例 | 建成形态 | 总分 | 等级 | 圈内点位数 | 完全盲区块数 | 盲区面积占比 |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| 交道口街道 | 成熟老城 | 98.24 | A | 48 | 1 | 3.53 % |
| 天通苑 | 新建大盘 | 80.97 | B | 14 | 2 | 26.33 % |
| 肖家河 | 城中村 | 44.65 | D | 23 | 1 | 97.12 % |

> 这三行是 **2026-10-10 坐标实调核验后重算**的基线。旧值（96.20 / 84.66 / 44.42，盲区 7.59 % /
> 18.44 % / 96.81 %）记的是「区级近似占位」坐标，已随 §4.3 第 1 步的核验作废。
> **圈内点位总数不变**（48 / 14 / 23，因为合成 POI 是按中心点坐标外推的确定性网格），
> 但**逐维度分布变了**（交道口 `medical` 6 / `shopping` 9、天通苑 `medical` 1、肖家河 `medical` 0），
> 于是总分与盲区面积随之移动。**三型方向关系不变**：老城 > 新建大盘 > 城中村，
> `test_three_forms_differ_in_the_expected_direction` 钉的就是这个。

三型的分数与盲区面积呈**单调反向**关系，差异肉眼可辨——这正是 FR-102
「三型的圈内设施计数与盲区面积要有可见差异」那条要求的验收形态，
`test_three_forms_differ_in_the_expected_direction` 把它钉成了断言。

**六维雷达（`mock`，步行 15 min）**：

| 维度 | 交道口 | 天通苑 | 肖家河 |
| --- | ---: | ---: | ---: |
| 基础医疗 | 100 | 75.0 | 0 |
| 教育 | 100 | 100 | 0 |
| 购物市场 | 100 | 100 | 83.33 |
| 养老休闲 | 100 | 100 | 100 |
| 公共交通 | 100 | 96.16 | 100 |
| 餐饮服务 | 100 | 100 | 100 |

肖家河的医疗与教育为 0 是**真实结论而非缺数据**：`transit_corridor` 画像里
药店与小学的合成条数就是 0，`count_in = 0` → `nearest_reason = no_poi_in_polygon`
→ 时效分按 0 计。它演示的正是「能上车、买不到菜」那类地方。

### 4.1 离线计量的诚实性

三个样例在 `mock` 模式下的三栏计量**全部为 0**（`place_calls` / `http_requests` / `quota_units`），
`route_lines` 两栏也全为 0 —— 合成层不发出网请求，就不该在配额对账单上记下没发生过的消耗。
这一条由 `test_mock_baseline_spends_no_quota` 钉住。

### 4.2 演示预案 A 的实跑记录（**无 AK 条件下**）

预案 A 的前提是「有可用的服务端 AK」，而本环境**没有**。这里如实记录把预案 A 的每一步
在无 AK 条件下真的执行一遍的结果——它验证的不是 live 链路本身，而是
**live 链路不可用时系统是否表现得体**（这恰恰是 §4.6 与 FR-404 的要求）。

**第 0 步：坐标核验工具**

```
$ python tools/gen_samples.py --verify
[FAIL] 未配置 BAIDU_SERVER_AK，无法核验坐标。
       请先在 .env 里填服务端类型 AK（与浏览器端 AK 不同，混用会报 status=203）。
$ echo $?
2
```

✅ **明确失败**：退出码 2、指名要填哪个变量、并提醒两类 AK 不能混用。
它**没有**静默跳过、也没有把近似占位坐标写成 `coord_verified: true`。

**第 1 步：显式请求 `live` 模式跑一次完整体检**

`POST /api/analysis {"sample_id":"beijing-jiaodaokou","data_mode":"live"}` →

| 项 | 实测值 | 判读 |
| --- | --- | --- |
| HTTP 状态 | **200** | FR-403：不冒 5xx |
| `meta.data_mode` | `live` | 尊重了用户选的策略 |
| `meta.data_source` | `sample` | 实际取了最内层（FR-306 徽标） |
| `meta.degraded` / `degraded_reason` | `true` / `no_live_data` | 降级原因可读 |
| `score` | **96.20 A**，盲区 4 块 / 7.59 % | **报告完整**，不是错误页。⚠️ 这一行是 2026-10-05 用**当时的占位坐标**实跑的留档，不改写；坐标核验后同一条链的现基线是 **98.24 A / 1 块 / 3.53 %**（见 §4 表注） |
| `usage` 三栏 | 全 **0** | 没发生过出网，就不记消耗 |

等时圈那一段给出的文案是：「实时批量算路没有取回任何可用耗时（额度耗尽或服务未开通），
已降级到我方合成的示例等时圈。」——说清了**是什么坏了**与**现在用的是什么**。

**第 2 步：`GET /api/health?force=true`（强制真实探测）**

| 项 | 实测值 |
| --- | --- |
| `data_mode`（顶层） | `mock` |
| `config.data_mode_in_effect` | `mock` |
| `baidu_reachable` | `false` |
| `probe.requests_sent` | **0**（`short_circuit: no_server_ak`） |
| `banner.show` / `level` | `true` / `warning` |
| `banner.cta_hint` | 「配置 BAIDU_SERVER_AK 后点『重新探测』（/api/health?force=true）」 |
| 7 个服务行的 `state` | 全部 `skipped_no_ak` |

✅ **零配置下不出现红色报错**（§6.2），且**没有为一个没有 AK 的探测白花一次出网配额**。

### 4.3 演示预案 A 的实跑记录（**接入真实 AK 后**，2026-10-10）

> §4.2 记的是「没有 AK 时会怎样」。**下面这段才是预案 A 本身。**
> 两类 AK 配进 `.env` 后重跑，本节所有数字都是真实出网拿到的。

**第 0 步：服务探测（`GET /api/health?force=true`）**

| 项 | 实测值 |
| --- | --- |
| `server_ak_type` | **`server_verified`** |
| `browser_ak_type` | `present_unverifiable`（浏览器端 AK 无法在服务端验证，符合预期） |
| `baidu_reachable` | `true` |
| `data_mode` / `data_mode_source` | **`live`** / `probe` |
| 6 个服务 | `geocode` / `regeo` / `place` / `direction` / `direction_lite` / `routematrix` **全部 `available`，`baidu_status=0`** |
| `place_polygon` | `available=false`，`state=not_probed`，message「该功能需要商用授权，本项目不使用」 |
| 探测自身用量 | 6 次 `http_requests`、1 次 `place_calls`、3 个 `quota_units` |

✅ `place_polygon` 的结论与 `docs/BAIDU_API_NOTES.md` §4.5 的预判**完全一致**
（个人档不开放多边形检索，本项目也不使用该形态）。
✅ 探测只花了 6 次请求，不是「每个服务反复试」。

**第 1 步：坐标核验（`tools/gen_samples.py --verify`）**

三条坐标全部走 **`/api/suggest`**（PRD §10-1 规程②指定的路径）取得，并用逆地理交叉核对行政区：

| 社区 | 匹配到的候选 | 实测行政区 / 街道 | 坐标（BD09LL） |
| --- | --- | --- | --- |
| `beijing-jiaodaokou` | 交道口街道 | 东城区 交道口街道 | `116.407282, 39.942233` |
| `beijing-tiantongyuan` | 天通苑 | 昌平区 天通苑北街道 | `116.431025, 40.076584` |
| `beijing-xiaojiahe` | 肖家河公园 | 海淀区 马连洼街道 | `116.302470, 40.024652` |

三条的 `coord_verified` 已置 `true`，含义是「工具核验过且行政区对得上」，**不是**「人工实地踏勘过」。

**第 2 步：live 全链路一键体检（三个样例各一次，清空缓存后首跑）**

| 社区 | 墙钟 | 总分 | 盲区 | `data_source` | `place` | `quota_units` | `http` | §6.1 断言 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 交道口 | 15.2 s | 54.01 D | 62.29 % | `live` | 7/8 | 52/120 | 14/20 | ✅ |
| 天通苑 | 16.1 s | 64.45 C | 0.00 % | `live` | 6/8 | 59/120 | 12/20 | ✅ |
| 肖家河 | 14.6 s | 0.00 D | 86.89 % | `live` | 6/8 | 58/120 | 12/20 | ✅ |

✅ **预案 A 通过**：三条全部 `meta.data_source=live`、`degraded=false`，
§6.1 三栏预算**逐条在内**，耗时 14.6~16.1 s（PRD §2.2 要求 ≤25 s）。
✅ **二次运行 `place_calls=0` / `quota_units=0` / `http_requests=0`**、`data_source=cache`
（§2.2 的缓存断言），实测确认。

**第 3 步：接入 AK 后新暴露的三个缺陷**（见 §2.6 ~ §2.8）

这一步最大的价值不是「跑通了」，而是**离线演示阶段完全看不出来的三个缺陷被真实数据照出来了**。

**④ 2026-10-11 真实 AK 复验：骑行单模式首次 live 出网 + 真实数据下的桌面版式**（用户点头，
`127.0.0.1:8000`，`.env` 原样：`DATA_MODE=auto`、`CACHE_ENABLED=true`、两个 AK 都在）。

上面 ① 那三轮**都是步行单模式**——上一轮那张表里「双模式预算内」那一列当时只有步行这一半的证据
（骑行只是按共用额度折算过预算）。现在骑行真的跑过一次 live 了。

| # | 请求 | `request_id` | `x-elapsed-ms` | `x-http-requests` | `x-place-calls` | `x-quota-units` | `meta.data_source` |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | `POST /api/analysis` 交道口 **步行** 15 min（**命中本地缓存，零出网**） | `59446ff3eb704e8b` | 56 | **0** | **0** | **0** | `cache` |
| 2 | 同参数重发一次（对照，仍零出网） | `3739454b7f2c4e00` | 25 | 0 | 0 | 0 | `cache` |
| 3 | `POST /api/analysis` 交道口 **骑行** 15 min（**首次骑行 live**） | `f86241f1be7c40be` | **14 462** | **14** | **7** | **67** | `live` |
| 4 | `GET /api/suggest?keyword=和平里&region=北京`（FR-108 真实一跳） | `d043dd017b0f43e0` | 369 | 1 | 1 | 0 | `live` |

* **墙钟**：骑行 live **14.46 s**，与 ① 那三轮（14.6 / 16.1 / 15.2 s）同档，
  PRD §2.2 的 ≤25 s 在骑行上同样达标。
* **骑行 vs 步行（同一中心点、同一 15 min）**：圈内面积 **6.2298 km² vs 1.7057 km²（3.65 倍）**、
  顶点 **33 vs 22**、`duration_field` 的 `sources` **7 vs 8**。33 个顶点里 **7 个 `measured`、26 个
  `interpolated`**，其中 11 个带 `path_distance_m`（1 665–2 725 m），`blocked_official_cells=0`、
  `blocked_timeout_cells=0`。**这一条把「live 下的骑行等时圈」从待验清单里划掉了。**
* 🔴 **骑行把等级换了**：同一社区同一时长，**骑行 72.12 B vs 步行 54.01 D**。原因是圈内面积放大 3.65 倍后
  同一个 1 000 m 判定半径能覆盖到更远的设施，盲区随之变小——**所以「等级」这个词在双模式下不是稳定结论**，
  报告里必须连着出行方式一起读（PRD §4.3 的评分口径本来就绑在等时圈上，但这一点此前没有任何真实数据
  把它量化过）。本轮没为骑行单独取盲区 %，取数与统计口径见下面的复验。
* **FR-206 的第二轮闸门第一次在真实数据上被走到**：步行结果已在屏上时把出行方式切到骑行，页面弹出
  「即将跑第二轮：骑行 等时圈」＋骑行/步行共用 5 000 条/日 的原文，`#quota-meta` 回填第一轮的真实计量
  （`quota_units = 0 ｜ http_requests = 0`——那一轮确实整份走缓存，页面没把它粉饰成出网消耗），
  并显示本地预算上限 `route_lines=150`；点「确认配额可承担，运行第二轮」才发请求。**没有静默出网**。
* **真实数据下的桌面版式**（1280×800，§2.20 那两条规则在真实数据上复测）：`#report` **1 225 px** 宽、
  雷达 **320×320**、柱状 **520×260**、`.table-wrap` `client 1195 / scroll 1195`（**不横滚**）、
  首个 POI 行高 **26 px**、页面横向溢出 `−15`。与合成数据那次只差在内容量：真实数据给了 **74 行**
  ——改前那个 300 px 窄条要横滚 600 px 的正是这张表。
* **真实数据把评分拉开了，方向与 §4.4 的判定一致**：交道口 live/cache 下 **54.0 D**
  （盲区 62.29 %、惩罚 −15.0 分、已按权重和 0.95 归一，字面口径值 65.6），而 `mock` 基线是 **98.24 A**
  （3.53 %）。合成圈把 `market` 补上、真实检索拿不到 `market`，一上 live 就从 A 掉到 D——
  `#blind-summary` 给出「完全盲区 3 块，栅格 250 m、判定半径 1 000 m」，`#analysis-notes` 带上了
  盲区偏大的自动提示。**这是设计上要说实话的那一条，不是回归**；但它也让 §4.1 的「分数只用于口径演示」
  有了量化后果，见下面那条 🔴。
* 🔴 **顺带抓出一条未修缺陷**（登记在 §6）：`meta.data_source=cache` 的同一次请求，响应里
  `isochrone.data_source` 仍是 `"live"`、`usage.cache_hits` 仍是 `0`，而前端地图栏的「取数层」读的正是
  `isochrone.data_source`（`web/isochrone.js:727`）——于是**一次 `x-http-requests: 0` 的请求在页面上写成「实时调用」**。

### 4.4 仍需说明的一处数据层局限：`market` 在三个社区都为 0

live 实测中，**三个社区的圈内 `market` 命中数都是 0**，`pharmacy` / `primary_school` 也各有缺失：

| 社区 | 圈内 `market` | 圈内 `pharmacy` | 圈内 `primary_school` |
| --- | --- | --- | --- |
| 交道口 | 0 | 0 | 0 |
| 天通苑 | 0 | 1 | 0 |
| 肖家河 | 0 | 0 | 0 |

**根因已定位，是检索层而不是分类层**：分类函数本身是对的（实测把
`'医疗;综合医院'` → `hospital`、`'教育培训;小学'` → `primary_school`、
`'交通设施;公交车站'` → `bus_stop` 全部判对），问题出在**招回来的东西不对**：

* 「购物市场」维度的检索词是 `市场$超市$便利店`。百度把**「市场监督管理局」（`政府机构;行政单位`）
  和「出入口;门」**排在前列，真正的农贸市场被挤出 `page_size=10` 的第一页；
* 「医疗」维度的 `药店$综合医院$诊所` 同理，前三页全被综合医院/门诊占满，`药店` 进不来。

**试过的补救办法（`tag` 过滤）实测无效，已排除**：

| 试验 | 结果 |
| --- | --- |
| `query=市场$超市$便利店`，无 `tag` | 10 条，`market` **0** |
| `query=市场` + `tag=购物;市场` | 10 条，仍 9 条 `other`（烟酒超市 → `购物;商铺`） |
| `query=市场$超市$便利店` + `tag=购物` | 与不加 `tag` 完全相同（该组合下 `tag` 未生效） |
| `query=药店` + `tag=医疗;药店` | **返回 0 条** —— `tag` 过滤反而把结果清空了 |

所以**没有在 PRD 框架内干净的修法**，原因是叠加了三条 PRD 已定死的约束：
① 每个维度只用一次请求（FR-210）；② `page_size=10` 且**一律不翻页**（笔记 FAQ 2542/2534）；
③ 检索词用官方类别名（笔记 §8.5 明确「菜市场/农贸市场」不是官方词条，不能加进词表）。

这与 PRD §4.3 自己登记的取舍是同一类（「达标阈值 ≥5 的维度会被页数截断而系统性低估，
这是已知取舍、不是 bug」），但**本项超出了那条取舍的范围**：「市场」会被
「市场监督管理局」这类同名政府机构系统性挤占，导致 `market` **恒为 0**，
而 FR-221 的盲区主判定恰恰依赖 `market`。因此**盲区面积在 live 模式下会系统性偏大**
（交道口 62.29 %、肖家河 86.89 %），这一点必须在使用 live 数据解读报告时明确告知，
不能当成真实结论呈现。

🔴 **决策（2026-10-10）：接受这个局限并明示，不动检索策略。** 修它要按类别拆成多次请求、或改用
`/place/v3/search`，而这两条都是 PRD §4.3 明确否掉的方向（① 每维度只用一次请求 FR-210；
② `page_size=10` 且一律不翻页）。落地方式三处，缺一不可：

* `README.md` §10.8 新增一条已知局限，写清「live 盲区面积系统性偏大、只能读作相对参考」；
* **后端对 `data_source = live` 的报告在 `notes` 里如实挂出这条提示**——前端 `#analysis-notes`
  会渲染它，Markdown 导出同源。拿到报告的人不必先读 README 才知道这个数字有水分；
* `docs/DESIGN.md` §12 的未验证表补一行，指回本节。

**离线演示（mock）不受影响**——合成层按画像直接给出三类设施，盲区结论是设计好的。

---

## 5. 真实测时 vs 插值推算的差异

PRD §9 要求本报告包含这一项。**诚实的回答是：本项目没有可用的真实测时样本，
因此无法给出「实测 vs 插值」的偏差统计。** 已知的与可报的是：

1. **机制上两者的区分是显式的**。每个等时圈顶点带 `sample` 标签，五档互不合并：
   `measured`（批量算路直接测得）/ `interpolated`（二分轮次用尽后内插）/
   `extrapolated`（`R_max` 处仍未达时限，边界外推）/ `blocked_official`（官方禁行硬证据）/
   `blocked_timeout`（返回空或超时，**可重试**）。后两者分开的理由是处置动作完全不同——
   「路走不通」和「我没测准」对用户是两件事。
2. **`mock` 模式下五档都能真实出现**。`web/isochrone.js` 的验收清单记录了实测到的
   `sample` 集合恰为五档齐全，说明标签是算法判定而非硬编码文案。
3. **IDW 插值只用于耗时场，不参与评分**。评分用的 `nearest_minutes` 只接受批量算路的
   实测值；`services/coverage.py` 明确拒绝用直线距离换算的时间顶替实测时间
   （见其模块 docstring 第 3 条与 `tests/test_coverage.py::test_timeliness_score_is_zero_when_nothing_was_measured`）。
   所以「插值误差会不会污染分数」这个问题在本项目里是**结构上不成立**的。
4. **2026-10-10 补到的两条真实对照**（JSAPI 底图那一轮顺带取得，见
   `docs/shots/README-isochrone-acceptance.md`「JSAPI 实时底图复验记录」第 3 节）：
   对同一中心点已算出的顶点，用顶点懒加载补调一次 `direction/v2/walking`，与等时圈顶点的记账对比——

   | 顶点 | 顶点上的账 | `direction` 实测 | 差 |
   | --- | --- | --- | --- |
   | #6（`measured`，方位角 90°） | 984 m / 15.0 min | 1050 m / 15.4 min | 距离 **+6.7 %** |
   | #2（`interpolated`，方位角 15°） | 719 m / 15.0 min | 950 m / 13.6 min | 距离 **+32 %** |

   两个结论：① **`RouteMatrix` 与 `Direction` 这两个接口对同一 O-D 给的数就不一致**（≈7 %），
   所以「实测」本身带一层接口口径差，不是绝对真值；② `interpolated` 顶点继承的是相邻实测方向的距离，
   偏 32 % 是**机制的必然**而不是意外——这也正是评分只吃 `measured`、不吃插值的原因（第 3 条）。
   ⚠️ **样本量 2，给不出分布**，别把它读成「插值误差 ≈30 %」。
5. **仍待补**：对同一样例各跑一次 `live` 与 `mock`、比较 `area_km2` 与各维度 `count_in` 的差异。
   单次已能做（§4.3 就是 live 实跑），但**多次 live 的 place 配额撑不起**（100 次/日，一次体检 7~8 次），
   所以这条仍然只能停在「样本量 1」。

---

## 6. 未验证项（诚实登记 / 风险 R15）

以下项目**没有**被验证过，不应被读作「已通过」：

| 项 | 原因 | 影响 |
| --- | --- | --- |
| ~~live 模式端到端~~ | ✅ **2026-10-10 已补验**，见 §4.3 | 三个样例全部 `data_source=live`、§6.1 预算在内、14.6~16.1 s |
| ~~样例社区坐标的真实性~~ | ✅ **2026-10-10 已补验**，见 §4.3 第 1 步 | 三条坐标走 `/api/suggest` 取得并交叉核对行政区，`coord_verified: true` |
| ~~JSAPI 4.0 **实底图**渲染~~ | ✅ **2026-10-10 已补验**：真实浏览器加载 JSAPI 4.0、23 个 `BMap_Marker`、在顶点上派发真实指针事件 → `POST /api/isochrone/polyline` 200 并画出真实路网轨迹（48/45 折点）。证据在 `docs/shots/README-isochrone-acceptance.md` | 这一轮**抓出了 §2.9**（提示只认 `live`）。仍未验的两项：12 次硬上限与连点竞态**在实时底图路径上**（同一份 `loadPolyline()`，SVG 路径已实测；再跑要烧 10 次真实 `direction`，按配额纪律不跑） |
| 🔴 **新发现，未修**：live 等时圈的响应体 `usage` 比服务端请求级计量少记 | 同一次 `POST /api/isochrone`：`body.usage` 是 `quota_units=40 / http_requests=6`，同 `request_id` 的服务端日志是 `44 / 7`，差一个批次 | 方向是**少报**（真实花费 ≥ 报告值），所以 §6.1 的预算断言在 live 等时圈这条路径上偏乐观。mock 层两账本同源、冒烟自测抓不到。**尚未定位**是哪一批没并进 `ProbeTally`，登记为待修而不是文档口径 |
| ✅ ~~桌面三栏下 `#report` 被栅格挤成 300 px 宽~~ **2026-10-11 已修**（§2.20） | 1280×800 实测：`.layout` 是三列（`300px 540.8px 360px`），`#report` 是第 4 个自动放置的子项，落到**第 2 行第 1 列**，宽度被 `minmax(260px, 300px)` 钉死。自**初始提交 `f24a174`** 就存在（上一轮这里写的 `7cee8f8` 是 10-11 的一个修复提交，不是起点——本轮 `git log --reverse -- web/isochrone.css` 实测首条就是 `f24a174`） | 改前 / 只加一条 / 两条都加**三态在同一份数据、同一个 `request_id` 上实测**：面板 300 → 1225 宽、高 10 250 → 3 644，雷达 280×280 → 589×589 → **320×320**（只加 `grid-column` 会让它涨过口径，所以 `.chart` 的上限是必需的另一半），柱状 280 → 520×260，POI 行高 79 → 26，`.table-wrap` 从 `270 / 600`（要横滚）变成 `1195 / 1195`。复测 769 / 1082 / 1122 / 1280 四档 + 375 抽屉无回归；1082 那档证明这条规则在单列区间是 no-op。三条静态断言进 `test_web_smoke.py`，四条「改坏」实测都能测红。逐条见 `docs/shots/README-isochrone-acceptance.md` 的「桌面报告版式与评分卡」一节 |
| 🔴 **新发现，未修**：`data_mode=mock` 到底该不该读本地缓存，是个**产品口径**问题 | `poi_search._resolve_dimension`（与 `geo.py` 同一层）在进 mock 分支**之前**先读一次本地 SQLite，所以「mock」只保证不出网、不保证「只用合成数据」。本轮就在真实浏览器里被它咬了一次：旧 8806 服务上同一份报告 67.8 C，`CACHE_ENABLED=false` 起服务后 98.2 A（§2.7 的复发，基线生成器早就关了缓存，**只有运行期这条路没关**） | 现在靠**文档口径**兜（README §10.4 第 3 步、本文 §7 第 5c 步都写明要 `CACHE_ENABLED=false`），代码语义未动。真要改是二选一：① mock 层跳过缓存读（那 `cache` 模式与 `mock` 的边界要重写，且 `test_cache_keys_ttl.py`（86 例）与 `test_cache_store_singleton.py`（4 例）的口径要跟着对账）；② 保持读缓存，但报告里把「本次混入了 N 条缓存中的真实数据」写进 `meta`/`notes`（**这条更轻**，但要多一个「缓存命中的是不是 live 数据」的判定）。按配额纪律与「不静默改结论口径」两条，**这一轮两个都没做**，登记为待决策 |
| 🔴 **新发现，未修**：基线可复现那条断言**绿得依赖测试顺序** | `pytest tests/test_samples_baseline.py` 在这台带脏缓存的机器上**必红**（`assert 67.8 == 98.24`），`pytest -q` 全量**却绿**；实测要 `test_analysis_api.py` + `test_cache_store_singleton.py` 同时在前才转绿，只加 `test_cache_keys_ttl.py` 不转（§2.20 末那张三行表） | 直接后果是**「509 例全绿」不能当成「基线可复现」的证据**——它只证明「这个顺序 + 这个缓存状态下整条链路没崩」。CI 里两件事同时成立（无 `.env`、无 `data/cache.db`），所以 CI 的绿是真的；**开发机的绿是顺序给的**。临时口径：单跑基线文件时显式 `CACHE_ENABLED=false`（实测 16 passed；只压 `BAIDU_SERVER_AK=` 不压它照样红；`-o cache_db_path=` 压不掉，那是 pydantic-settings 读的）。真正的修法要么给这条断言注入 `cache_enabled=False` 的 settings（与 `gen_samples.py` 同源，最小改动），要么给 `run_analysis` 加「本次不许读缓存」的参数——两者都要重新对账 §6.1「缓存命中下 100 % 可复现」那句口径，属产品判定，**本轮没动** |
| ✅ ~~🔴 **出网资源按 client 复制**：`BAIDU_MAX_CONCURRENCY` 名义 4、实际进程里 12，QPS 同样乘三，**熔断器按 client 计数所以账号级限流从来没被看见过**~~ **2026-10-13 已修**（§2.21） | 症状来自用户实拍百度账号控制台「并发达到 12」。根因在 `infra/baidu_client.py`：每个 `BaiduClient` 自带一套 httpx 池 / 在途闸 / `RateLimitRegistry` / `BreakerRegistry`，而进程里有 `poi_search` / `geo` / `isochrone` 三个服务单例各建一个 client。假传输层计数桩实测：cap=3 时修前三套独立 → **进程峰值 9（= 3 × 3）**，修后一份共享出口 → **峰值 3**，两次都是 12 个请求全发出 | 修法是进程级共享出口 `Egress`（池 + 桶 + 熔断 + 在途闸各一份），`/api/health` 新增 `egress` 块（`bindings` 从 `2` 涨到 `3` 是本机实跑的观察），`.env`（gitignored）把并发钉到 **3**、tracked 默认值保持 **4**。同轮把 README §4.3 / §4.4 那句错的口径改对了，并补 `tests/test_egress_sharing.py` 4 例 |
| 🔴 **新登记，本轮不修**：共享在途闸带来**跨服务的队头阻塞** | 3 个名额现在全进程共用：一次等时圈的路线突发能把名额占满，此时一个 `/api/regeo` 得排队。这是「按 AK 计数」的必然代价，不是回归 | 未做**公平性**（没有按服务预留名额、没有优先级）。更彻底的方案是真正的请求队列（FIFO + 背压 + 可查排队位置），但那会把 **FR-205 前端进度轮询**（任务 #17）从 P2 变成必做项——否则用户只看得到「转圈不动」。本轮按用户口径只做「共享出口 + 账号级熔断」两件事，队列与进度条一起留给下一步 |
| live 模式下 FR-221 盲区结论的**准确性** | `market` 系统性为 0，见 §4.4 | 盲区面积在 live 模式下**系统性偏大**，不能当成真实结论呈现；根因已定位在检索层，无 PRD 框架内的干净修法。**2026-10-10 定口径：接受并明示**（见 §4.4 末的决策段）——真实检索那一侧（`data_source = live` **与 `cache`**）的报告由后端 `notes` 自动带这条提示，`mock` 口径不受影响；`cache` 一起算的原因见 §2.9 |
| `docker compose up` | 开发机没有 Docker，CI 用的是裸 `docker run` | `docker build` **已由 CI 真实执行并跑绿**（2026-10-07：镜像不含 `.db`、不含明文 AK、零 env 起容器 → `HEALTHCHECK` 转绿 → `/api/health` + `/api/samples` + `POST /api/analysis` 可服务）。**仍未验**：compose 的起停、以及 CI 没挂 `-v` 所以「Linux 宿主 UID 1000 对 `./data` 的写权限」这条在开发机上复现不出来 |
| ~~🔴 **FR-106 底部抽屉：功能未实现**~~ | ✅ **2026-10-11 已实现并补验**，抽屉自身的两处缺陷见 §2.12，逐条测量见 `docs/shots/README-isochrone-acceptance.md` 的「FR-106 移动端底部抽屉」一节 | 这条 10-11 才登记为「未实现」，同日落地。PRD §6.3 的「两档视口 375×667 / 414×896 无横向滚动、无元素重叠」现在**有可复现的测量记录了**（`scrollWidth / clientWidth` 两档都是 375/375 与 414/414，逐元素热区 ≥ 44 px）。**仍未验**的是真机手感那三项（下一行）——抽屉能拖不等于手指好拖 |
| ~~**FR-107 只能按视口宽度自动降档，无法手动指定**~~ | ✅ **2026-10-11 已补**：`?profile=m` / `?profile=d` 覆盖 `matchMedia` 探测，报告头徽标按**响应** `meta.lightweight` / `meta.k` 写 | 四例真实浏览器实测 + 三次 curl 直连后端，逐条见 `docs/shots/README-isochrone-acceptance.md` 的「FR-107 档位显式化」一节。**顺带照出一个口径事实**：两档分数**既可能相同也可能不同**——⚠️ 上一轮这一行写的「交道口 `mobile` 63.03（C）vs `desktop` 67.8（C），差 4.77 分」是**缓存污染**下的两个数（§2.20 末），本轮 `CACHE_ENABLED=false` 重测：交道口 `k=6` **92.1 A**（盲区 0.00 %）vs `k=12` **98.2 A**（3.53 %），差 **6.1** 分；天坛中心点 `k=6` 与 `k=12` **都是 44.6 D**，但盲区是 **98.92 % vs 94.66 %**——**同分而盲区反向移动**，轻量档并不是「少跑几维、结论不变」。所以「轻量档只是省调用、不影响结论」这句**不成立**，徽标里那句「别把两档的分数横向比较」是必需的而不是客套。**换到干净数据后等级也换了**：污染态两档都是 C，干净态两档都是 A |
| ✅ ~~FR-103 地址行的真实 `live` 一跳~~ **2026-10-11 已补验** | 零出网下**四例已验**（真实服务端 mock 空态 + 前端缓存去重 + 世代号挡过期响应 + 断网失败态），当时成功态是打桩验的；**现在成功态有真实一跳了**：交道口中心点由 `GET /api/regeo?lnglat=116.407282,39.942233` 真实返回并渲染成五枚字段徽标（`北京市东城区…`／`东城区`／`前门街道`／`长巷二条乙5号`／`adcode 110101`／`取数层：实时调用`） | ⚠️ **成本比预期高一倍**：页面加载时会对**默认中心点** `116.4074,39.9042` 先问一次 regeo，换样例后再问一次，一次开页烧 **2 次**逆地理（300 次/日 池子）。逐条见 `docs/shots/README-isochrone-acceptance.md` 的「FR-103 中心点地址回显」一节 |
| ✅ ~~🔴 FR-108 候选列表的真实 `live` 一跳 + `/place/v2/suggestion` 报文形状~~ **2026-10-11 已补验** | 零出网下那**七例**继续有效（默认关闭零请求 / 两个本地门槛零请求 / 真实一跳打到本地 `mock` 后端 / 打桩后渲染与点选与键盘 / 前端缓存命中不再发 / 换城市重问 / 世代号丢慢响应 / 移动端抽屉滚动进视口）；**候选列表本身现在是真实返回**：`request_id d043dd017b0f43e0`、`x-http-requests: 1`、`x-place-calls: 1`、`x-elapsed-ms: 369`，**10 条候选、`dropped: 0`**，`name` / `lnglat` / `district` / `city` / `province` / `adcode` 全有值 | 三件里两件结了：① 真实一跳已跑（要开 `SUGGEST_ENABLED=true` 起服务，**默认关着**，见 §7）；③ **`_parse_suggest` 的字段名核对上了**——判据是「键名猜错就不可能有条目活下来」，这条 TODO 已从代码注释里摘掉，笔记 §2.4 补了实测段。**仍未验的是 ② `flyTo`**：无浏览器端 AK 的机器上实时底图起不来，只留 `panTo` 退路 + `typeof` 守卫（`tests/test_web_smoke.py` 断言这条退路在）。逐条见 `docs/shots/README-isochrone-acceptance.md` 的「FR-108 地址联想」一节 |
| 移动端**真机**行为 | 只有 Chrome DevTools 模拟，无 iOS / Android 实机 | 手指惯性拖图、双指缩放、iOS Safari 滚动穿透三项未验证；官方也未公开 JSAPI 4.0 的精确移动兼容矩阵 |
| 🔴 **新发现，未修**：缓存命中时地图栏把「取数层」写成**实时调用** | 2026-10-11 真实 AK 复验抓到：同一次 `POST /api/analysis`（`request_id 59446ff3eb704e8b`）响应头 `x-http-requests: 0 / x-place-calls: 0 / x-quota-units: 0`、`meta.data_source=cache`，但 `isochrone.data_source` 仍是 `"live"`、`usage.cache_hits` 仍是 `0`；前端地图栏那一格读的是 `isochrone.data_source`（`web/isochrone.js:727`） | **在 3 次不同的缓存命中请求上都复现**（`59446ff3eb704e8b` 步行、`3739454b7f2c4e00` 重发、`7fca8d5460d94ffb` 骑行），不是偶发。报告头写「本地缓存」、地图栏同屏写「实时调用」，**同一页两个取数层互相打脸**，而 0 出网那一个是错的。判据在 `services/isochrone_service.py:856`：`cache` 只在 `http_requests == 0` **且** `cache_hits > 0` 时才成立——本例 `cache_hits` 没动，于是落到 `live`。**两层未解**：① 为什么 6 个批次全部命中 SQLite 却 `cache_hits=0`（`isochrone_service.py:212` 只统计 `result.batches[].from_cache`，而 `polyline_calls=0 / oracle_calls=6` 说明这份结果不是从那条计数路径回来的，**尚未定位**）；② 报告侧的 `data_source` 是 `_overall_source([...])` 聚合（`services/analysis.py:549`），所以它对 `cache` 的判定与 isochrone 子对象各说各话。修法要么让子对象继承聚合值，要么前端改读 `meta.data_source`——**后者只动一行但会牵动 §2.9 那条「提示只认 live」的判定**，属产品口径，本轮没动。它同时是 §2.20 末那条「67.8 是怎么被当成 live 结果读出来的」的放大器。**实操口径：判断有没有出网看响应头 `x-http-requests`** |
| 浏览器内真实渲染（**两条通道，别混**） | Node + 最小 DOM 打桩没有 CSSOM 与 canvas；真实 Chrome（chrome-devtools MCP）有 | 打桩那条跑两条绘制路径（`tools/report_dom_smoke.mjs` **21 项** + `tools/map_layers_smoke.mjs` **12 项**，`grep -c 'OK '` 与脚本里 `^check(` 的条数**对账**，见 §2.18），只验「节点建对了」。**像素与字节那一半只有真实浏览器能给**：FR-305 的 PNG 尺寸 / 大小 / `drawImage` 与 `fillText` 内容、以及 §2.19 那条提示覆盖缺陷，都是 chrome-devtools 量的。历史：早期写的「无头 Chrome 无输出」是 browser-use 通道的限制，不影响 chrome-devtools |
| 前端截图（**能拍、但不入库**） | 2026-10-10 起 chrome-devtools 通道能截图（实时底图与橙色真实路网线已现场目视确认） | **仍不提供 PNG**：截图必然含百度瓦片与版权条，把第三方地图影像固化进开源仓库是另一回事；本文件继续以 DOM 计数、network、状态行/tooltip 原文作证据。⚠️ 别和 **FR-305 的报告卡 PNG** 混成一件事：那是**用户在自己浏览器里点出来的运行时导出物**，由我方矢量内容合成、不含任何瓦片，也**不提交进仓库**（验收时只在内存里量字节数与尺寸，跑完即弃） |
| ✅ ~~FR-305 的报告图片（PNG）导出未实现~~ **2026-10-11 已落地**；🔴 但**「live 底图能不能截」仍未实测** | 现在有**四条**出口：`exportJson` / `exportCsv` / `exportBlindCsv` / **`exportPng`**（`web/report.js`）。已实测的一半：卡片由**页面里那两张 SVG 的克隆**合成，`mock` 零出网下真实 Chrome 量得 `1 800 × 2 584 px`、`image/png`、**670 295 字节**（页面提示同一数写作「约 655 KB」），**真实按钮路径**从点 `#export-png` 到 `<a download>` 被点 **89 ms**，`drawImage` 收到 **2** 个 `IMG`（本征 `640×640` / `1688×844`）、`fillText` **19** 行里含 `GS(2025)4125` 与「不得二次分发」；**同一份**报告连跑 4 次字节数完全一致（`670 295` × 4），换一份报告就换数——所以字节数不是回归哨兵。🔴 **真实落盘这一条仍未验**：本轮把 `<a download>` 拦下来量的 blob，文件没真写进下载目录。没实测的还是那半句：JSAPI 把瓦片画在自己的跨域 `<canvas>` 里、无 CORS 头，`toDataURL()` 按规范应抛 `SecurityError`——**这条 2026-10-11 有了真实浏览器测量，但只测到机制那一半**：在 `http://127.0.0.1:8001` 的页面里
拿一个**跨源且无 CORS 头**的位图（本机另起 `127.0.0.1:8002` 静态服务，只放一张 1×1 PNG）画进 canvas
再 `toDataURL()` → 实测抛 **`SecurityError: Failed to execute 'toDataURL' on 'HTMLCanvasElement': Tainted canvases may not be exported`**；
同一张图带 `crossOrigin="anonymous"` 加载直接 `onerror`（证明无 ACAO 头时连干净路径都走不通）；
同源对照（`data:` URL）`toDataURL` 正常返回 166 字节。**没测到的那一半**是百度瓦片本身到底带不带 ACAO：
`api.map.baidu.com/images/logo.png` 一类候选资产在本机 `<img>` 直接 `onerror`（404 或被拦，分不清），
而浏览器侧的 `fetch(mode:'cors')` 抛 `TypeError` 也无法区分「无 CORS 头」和「连不上」——
**所以「JSAPI 瓦片必然污染画布」这句现在是机制实测 + 宿主未实测的组合，不是完整证明**。
真要实测得开一次 live 底图，而那需要**人手工把浏览器端 AK 填进 `#browser-ak`**（后端按约定只回
`has_browser_ak` 布尔、密钥值永不下发，`infra/config.py:470` / `api/health.py:1204` 实测确认），
本轮没有把密钥抄进任何工具调用参数，**这一格仍然开着**。 口径没变：**做「矢量报告卡 + 版权/审图号文字带」，不承诺拍到在线瓦片**（条款 6.6 要的是导出品里**保留标识**，不是导出品**必须是那张底图**）。标识直接 `querySelector` 读页脚，**读不到就拒绝出图**（实测抽掉 `.foot-legal` → 零次下载 + 提示给出原因）。`静态图` API 作为备选同样返回跨域位图，绕不开这个判定。逐条见 `docs/shots/README-isochrone-acceptance.md` 的「FR-305 报告卡 PNG」一节 |
| 🔴 **页脚审图号字符串没有可自动核对的来源** | `GS(2025)4125` / `甲测资字11111342` 是 2026-10-11 从 `lbsyun.baidu.com` 首页页脚的文本节点**实读**的（不是记忆），TTL 那三个数字有 `test_compliance_footer_ttl_matches_config` 对着 `Settings.model_fields` 默认值守着，**审图号没有对应的机器可读真值** | 自动化只能验「这一行在不在」，验不了「这一行还是不是官方那个号」。页脚因此带**读取日期**，并把**季度人工复核**登记为唯一兜底（README §10.7 / §9.7） |

---

## 7. 复现步骤

```bash
# 1) 全量单测（零配置、零 AK、零出网）
#    ⚠️ 如果本机 .env 里配了真实 AK，按 CI 口径先把它们压掉，别依赖「碰巧没读到」：
#      DATA_MODE=mock BAIDU_SERVER_AK= BAIDU_BROWSER_AK= pytest -q
#    ⚠️ 如果本机 data/cache.db 里有真实 POI（跑过 live 的机器必然有），
#      **全量绿不代表基线可复现**——它绿是靠前面的文件替换了进程级缓存单例（§2.20、§6）。
#      单跑基线文件要显式压掉缓存，否则在脏缓存机器上是 1 failed / 15 passed：
#      CACHE_ENABLED=false pytest tests/test_samples_baseline.py   # 实测 16 passed
pytest -q

# 1b) 出网资源是否全进程只有**一份**（§2.21；零真实出网，走假传输层计数桩）
#     这一支不读 .env、不碰 data/cache.db，单独跑也稳定绿；**在修之前的代码上第④例必红**
#     （三套独立闸 → 在途峰值 9 > 上限 3）
pytest tests/test_egress_sharing.py -q                       # 4 passed
#     运行期自查（不用读代码）：/api/health 顶层的 egress 块
#       刚起服务        → {"built": false, ...}
#       一次体检之后    → {"built": true, "bindings": 2, "max_concurrency": 3, "stale": 0}
#       再打一次 regeo → {"built": true, "bindings": 3, ...}   ← 3 = poi_search / geo / isochrone
curl -s http://127.0.0.1:8000/api/health | python -c "import json,sys;print(json.load(sys.stdin)['egress'])"

# 2) 词表自检（官方 POI 分类表逐词核对）
python -c "from services.poi_categories import assert_wordlist; assert_wordlist()"

# 3) 合成样例基线校验（不写文件、不出网）
python tools/gen_samples.py --check

# 4) 对比实验（零真实出网，产出本文第 3 节的全部数字）
#    本文引用的原始数据留档在 docs/bench_route_strategy.json
python tools/bench_route_strategy.py --out docs/bench_route_strategy.json

# 5) 报告面板渲染路径（需要 node；无参数则用内置样本）
node tools/report_dom_smoke.mjs

# 5b) 地图两个叠加图层的绘制路径（FR-212 / FR-222，需要 node + 一份真实响应）
#     ⚠️ 生成桩输入这一条**同样走运行期缓存**：脏缓存机器上不加前缀会得到 67.8 / 44 条的桩，
#       冒烟自测照样 12 项全过（它数的是绘制路径，不核对分数）——**桩是污染的，测试却看不出来**。
#       前缀实测对比：无 `CACHE_ENABLED` → `67.8 / 44`；`CACHE_ENABLED=false` → `98.24 / 48`（与基线一致）。
python -c "import json;from fastapi.testclient import TestClient;from api.app import app;open('_map_input.json','w',encoding='utf-8').write(json.dumps(TestClient(app).post('/api/analysis',json={'sample_id':'beijing-jiaodaokou','minutes':15,'data_mode':'mock'}).json(),ensure_ascii=False))"
node tools/map_layers_smoke.mjs _map_input.json
#     干净口径（在脏缓存机器上照这条跑，桩里才是基线那份数）：
#     BAIDU_SERVER_AK= BAIDU_BROWSER_AK= CACHE_ENABLED=false python -c "…同上…"

# 5c) 报告卡 PNG（FR-305）——**只能在真实浏览器里量**，Node 桩没有 CSSOM 与 canvas
#     ⚠️ 起服务这一条**必须带 `CACHE_ENABLED=false`**：脏缓存机器上按老写法起 8806，页面里点出来的
#       就是 67.8 C / 盲区 47.63 % 那份污染报告（§2.20）。本轮量版式用的是这一条：
#     DATA_MODE=mock BAIDU_SERVER_AK= BAIDU_BROWSER_AK= CACHE_ENABLED=false \
#       python -m uvicorn api.app:app --host 127.0.0.1 --port 8805
#     打开 http://127.0.0.1:8805/isochrone.html，点「一键体检」，然后在控制台跑：
#       const R = window.CircleDocReport;
#       const blob = await R.buildPngBlob(R.getCurrent());
#       const img = await createImageBitmap(blob);
#       [blob.type, blob.size, img.width + 'x' + img.height];   // 期望 image/png、1800x2584；字节数**只同一份报告可复现**（本轮 668 709 / 670 295 都是真值，别拿其中一个当期望值）
#     2026-10-11 实测：同一份报告连导三次字节数一致；换一次体检（卡片里的 generated_at /
#     request_id 变了）会是 670 130，两个数都是真的。

# 5d) live 复验（**会真的出网、真的烧配额**，2026-10-11 跑过，逐条数字见 §4.3 ④）
#     ⚠️ 三条前置：① 只在用户点头后跑；② 同一时刻只留一个真实请求在飞（账号并发上限 3）；
#        ③ 想复现「第一次 live」而不是命中缓存，得换没跑过的参数（本轮用的是**骑行**）或先 `CACHE_ENABLED=false`。
#     直接照 5c 那条命令起服务即可（.env 原样，DATA_MODE=auto 会自动走 live）。
#     FR-108 那一跳另需运行时打开闸门——后端 `suggest_enabled` 默认 False，.env 里也没写：
#       SUGGEST_ENABLED=true python -m uvicorn api.app:app --host 127.0.0.1 --port 8001
#     然后在页面勾「输入联想」+ 填城市 + 输入 ≥3 字，一次输入只发一跳（防抖实测生效）。
#     实时底图那一格**没法用命令复现**：后端只回 `has_browser_ak` 布尔，密钥值按约定不下发前端，
#     必须人在 `#browser-ak` 里手填浏览器端 AK（本轮没有把密钥抄进任何工具调用参数）。

# 6) 静态检查
ruff check . && black --check .
```

第 4、5、5b 三条都不依赖 AK，也不访问任何外部服务。

两套冒烟自测（`m1_smoke` / `m2_smoke`，脚本在仓库外的 `swarm/` 下）**自己压掉 AK**：
脚本在 `chdir` 前就把 `BAIDU_SERVER_AK` / `BAIDU_BROWSER_AK` 两个环境变量置空。原因是
pydantic-settings 的环境变量优先级高于 `.env`，本机一旦配了真实 AK，不显式压掉脚本会**真的**
打百度线上服务；而冒烟的出网守卫只装在父进程，`[E]` 段起的 uvicorn 子进程没有守卫，全靠这一手。
CI 本来就没有 `.env`，这条改动对它无感；2026-10-10 复跑 `m1` 35/35、`m2` 102/102，守卫均未触发。

### 7.1 文档里那几个「自测数字」也要用命令复核（这张表每次改前端都得重跑）

本文与 README / DESIGN 引用的**断言条数**都是手写的，而这三处早就和脚本真实输出对不上了。
复现命令与实测值（**2026-10-11 再次全部重跑**，右两列分别是本轮值与上一轮登记的旧值。
这张表已经连着六轮抓到东西：第一轮三处漂移、FR-106 那轮四处、FR-107 那轮 `report.js` 与
`test_web_smoke.py` 各 +1；FR-103 那轮动了前端，`isochrone.js` 与 `test_web_smoke.py`
各 +1，另新增一行 `test_geo_api.py`；FR-108 那轮又同时动了前端与接口层，
`isochrone.js` +2、两个测试文件 +6 / +11 —— 这些都是**真实增长**不是漂移，
但只有重跑命令才分得清。**FR-413 这轮抓到的是另一种东西**：三个 DOM 计数一个没变（26 / 62 / 96），
`test_web_smoke.py` +3，可是**上一轮写进文档的两个数字本身是错的**——页脚对比度 4.49（真值 4.51，
见 §2.16）与「black 全绿」（本轮首跑 `black --check tests/` 是 `exit=1`）。
**修 CI 那一轮（§2.17）又抓到一条同类漂移**：README §10.4 手写的 `test_web_smoke.py`（45 例）与
`isochrone.js`（60 个 id）停在 FR-106 那轮，实测 `54 tests collected` 与 **62** 个唯一 id，
两个数字在改 README 之前就已经错了两轮。**FR-305 这轮抓到的是第三种东西，也是最坏的一种**：
`report_dom_smoke.mjs` 里有一份卡在文件中间的「汇总 + `process.exit`」，把末尾 5 条断言变成不可达代码——
**计数对、退出码也对、就是少了 5 条**（§2.18）。所以这张表从今天起多一列对照：冒烟脚本的
`grep -c '^check('` 必须等于 `grep -c 'OK '`，单看后者分不清「跑绿了」和「压根没跑」。
本轮 `report.js` 的静态体积数字也一并重测（README §10.4 那张表停在 235.7 KB / 47 %，实测 270.2 KB / 54 %）。

| 数字 | 复核命令 | 实测 | 此前文档写的 |
| --- | --- | --- | --- |
| `report.js` 取的 DOM 节点数 | `grep -o 'getElementById("[^"]*"' web/report.js \| sed 's/.*("\(.*\)"/\1/' \| sort -u \| wc -l` | **27** | 24 → 25 → 26（FR-107 新增 `#profile-badge`）→ **27**（FR-305 新增 `#export-png`）。FR-413 那轮复用了已有的 `#request-id-foot`，当时三个数（26 / 62 / 96）一个没变 |
| `isochrone.js` 取的 DOM 节点数 | 同上换成 `web/isochrone.js` | **62**（本轮未动） | 58 → 59（FR-106 新增 `#sheet-bar` 及其三个 tab）→ 60（FR-103 新增 `#center-address`）→ **62**（FR-108 新增 `#suggest-on` 与 `#suggest-list`）。`web/isochrone.html` 的 `id=` 去重后同步到 **97**（FR-305 加按钮那一个） |
| `report_dom_smoke.mjs` 断言条数 | `node tools/report_dom_smoke.mjs \| grep -c 'OK '`，**并且** `grep -c '^check(' tools/report_dom_smoke.mjs` | **21 = 21**，`exit=0` | 🔴 此前一直记 **15**，而 15 是**真的少跑了 5 条**：脚本里有两份「汇总 + `process.exit`」，中间那份让末尾 5 条断言从建仓起就是不可达代码（§2.18）。删掉中间那份 = 20，本轮加 PNG 接线那条 = **21**。左列两条命令**必须相等**，单看 `OK` 数分不清「跑绿」与「没跑」 |
| `map_layers_smoke.mjs` 断言条数 | `node tools/map_layers_smoke.mjs _map_input.json \| grep -c 'OK '` | **12**，`exit=0`。⚠️ 本轮重生成桩输入时**两种缓存态都量过**：默认（脏缓存）→ `96 784` 字节、`67.8 / 44` 条；`CACHE_ENABLED=false` → **`80 677`** 字节、`98.24 / 48` 条（与基线一致）。**两次都是 12 项全过、`exit=0`** —— 这条冒烟数的是绘制路径，**对数据层不敏感，所以它拦不住污染**（§2.20）。早前用另一个中心点也生成过 57 901 字节的桩输入，同样 12 项：字节数随请求与数据层变、断言数不变，**别拿文件大小当回归信号** | 上一轮也写 12 —— 但**那个 12 是抄的**，同一提交上这条脚本崩了、`grep -c` 实返 `0`，见 §2.11 |
| `test_web_smoke.py` 例数 | `pytest -q --collect-only tests/test_web_smoke.py` | **62**（`62 tests collected in 0.29s`） | 27 → 18（README §10.4 曾写）→ FR-104 补到 31 → FR-106 补到 39 → FR-107 补到 41 → FR-103 补到 45 → FR-108 补到 51 → FR-413 新增 3 例到 54 → FR-305 新增 5 例（按钮接线与提示顺序 / 合规文案单一来源 / 不碰底图画布 / CJK 光栅化 / 卡片文字色对比度重算）到 59 → **本轮桌面版式 +3 例（栅格整行 / 图表上限只绑抽屉断点 / 评分卡渐变两端 × 三档文字色重算）到 62** |
| `test_geo_api.py` 例数 | `pytest -q --collect-only tests/test_geo_api.py` | **27**（本轮未动，复测确认） | FR-103 那轮新增文件（16 例）→ **FR-108 那轮 +11 例 `/api/suggest`**：`/api/suggest` 此前同样**没有任何接口级用例**，只在启动探测里被间接触碰 |
| `test_health_contract.py` 例数 | 同上换成该文件 | **11** | 本文 §1 表写 10（一轮重算合计时暴露，已就地改成 11） |
| 全量单测 | `./.venv/Scripts/python.exe -m pytest -q` | **513**（共享出口落地后本机实测 5.06 ~ 5.78 s 五连跑均报 `513 passed`，退出码单独取一次 = 0）<br>🔴 **但这个绿不代表基线可复现**：同一棵树上 `pytest tests/test_samples_baseline.py` **单独跑必红**（`assert 67.8 == 98.24`），全量绿是靠前面的文件替换了进程级缓存单例（§2.20 末三条命令的实测表）。CI 那侧没有这个污染源（仓库不签入 `data/cache.db`，`infra/cache_store.py` 头注释第 3 条「镜像内不得预置任何 `.db`」+ 惰性建库；**这是按代码与仓库内容推的，本轮没在 CI 上实测这一条**） | 451 → FR-106 后 459 → FR-107 补 2 例到 461 → FR-103 补 20 例（16 + 4）到 481 → FR-108 补 17 例（11 + 6）到 498 → FR-413 新增 3 例到 501 → FR-305 新增 5 例到 506 → 桌面版式新增 3 例到 509 → **共享出口（§2.21）新增 4 例到 513** |
| `web/` 八个静态文件合计体积（PRD §5 的 500 KB 预算） | `wc -c web/* \| tail -1` → 本轮 **`278 583 total`**（⚠️ 别再手写 `wc -c web/* \| awk '{s+=$1} END{…}'`：`wc` 多文件时**自己就带一行 total**，那样是把它加了两遍，实返 **540.4 KB**，比真实值大一倍——本轮就这么错过一次） | **278 583 字节 = 272.1 KB = 预算的 54 %**（本轮 +1 905 字节：`isochrone.css` 21.1 → **21.7 KB**、`report.css` 7.3 → **8.7 KB**，其余六个文件一个字节没动） | 🔴 README §10.4 那张表停在 **235.7 KB / 47 %**，是 FR-106/107 那轮量的；后面四轮（FR-103 / FR-108 / FR-413 / FR-305）一直在往同一批文件里加代码，**谁都没回来改这个数**。差额 34.5 KB 里 `isochrone.js` 15.1 KB、`report.js` 12.9 KB。结论（远未超预算、零第三方资源）没变，但数字必须重测才能写 |

**本轮新增的纪律：复核命令要连着看退出码，还要拿「应该有多少条」去对账。** `grep -c 'OK '` 在崩溃的
输出上照样返回一个数（`0`，§2.11），而 §2.18 更进一层：它返回 **15**、`exit=0`、一切正常，
只是脚本里 5 条断言根本没被执行。**只看输出自己跟自己一致是分不出「跑绿」和「没跑」的**，
所以冒烟脚本从现在起两条命令一起量：`grep -c '^check(' 文件` 与 `grep -c 'OK ' 输出`，两者必须相等。

⚠️ **门禁必须用 `.venv` 而不是系统 Anaconda**：`pytest` 直接敲会走 `D:\Anaconda`，那边没装 `fastapi`，
`tests/test_web_smoke.py` 在**收集阶段**就 `ModuleNotFoundError`，一个用例都不会跑。
本轮就撞上过一次（`no tests collected, 1 error`），差点把「收集失败」读成「测试没变化」。

顺带修掉 `tools/report_dom_smoke.mjs` 头注释里那条**照抄会失败**的用法：它写的是
`node tools/_report_dom_smoke.mjs <report.json>`（文件名多个下划线前缀、且参数其实可省），
实际路径是 `tools/report_dom_smoke.mjs`，不传参数走内置样本。

🔴 教训与 §2.7 同源：**能算出来的数字就别手写**。DOM 契约那条断言本身是动态取 id 的，
所以「24」写错了它照样全绿——数字只存在于散文里，没有任何测试守着它。
