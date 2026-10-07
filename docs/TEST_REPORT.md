# CircleDoc 测试与对比实验报告

| 项 | 内容 |
| --- | --- |
| 文档定位 | PRD §9 交付物「对比测试报告」；对应 FR-405、FR-406、§6.1、§6.2 |
| 最后更新 | 2026-10-05 |
| 测试命令 | `pytest -q`（零配置、零 AK、零真实出网） |
| 结论 | **434 例全绿**，`ruff` / `black` 零告警；零真实出网可全程复现 |

---

## 0. 先说清楚这份报告的边界

本项目**从未有过可用的百度 AK**，因此：

* 所有「实时链路」的验证都在 **`httpx.MockTransport` 上完成**（不建任何连接）。这是条款 3.2.3(ii)
  要求的形态——它明文禁止对百度线上服务施压测试，而「跑 96 次串行 `direction`」正是一种压测。
* 报告里凡是**依赖真实网络**才能得到的数字，一律标注为「**推算**」并给出假设参数；
  凡是本地能精确测的（HTTP 请求数、配额单位、CPU 耗时、排队时长）一律标注为「**实测**」。
* **没有一条 live 模式的端到端记录**。这不是遗漏，是既定条件；演示预案 A（live）
  必须在有 AK 的机器上另行实跑一次，见 README 的演示脚本章节。

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
| `test_analysis_api.py` | 35 | `/api/analysis` 报告形状、任务轮询、后台模式、`/api/poi` 七维度、错误信封 | §4.7、FR-205、FR-404 |
| `test_web_smoke.py` | 27 | 静态页可服务、零公网外链、**DOM 契约对账**、FR-212/222/213 的前端接线 | R11、R12、R13、FR-213 |
| `test_samples_baseline.py` | 16 | 三样例一致性、三型差异方向、基线不漂 | FR-102、FR-412、§6.1 |
| `test_coord_param_table.py` | 12 | 接口 × 坐标参数名 × 坐标顺序常量表 | FR-406、R8 |
| `test_health_contract.py` | 10 | `/api/health` 三个「数据模式」字段的位置与值域、零配置横幅、AK 不外泄 | §4.6、FR-404、FR-410 |
| `test_cache_store_singleton.py` | 4 | 缓存单例口径唯一 | FR-402 |
| **合计** | **434** | | |

耗时：**5.1 s**（最慢单例 0.27 s）。无网络、无外部数据库、无 Redis。

### 1.1 三条值得单独说的断言

**`test_web_smoke.py::test_report_js_ids_all_exist_in_the_page`** —— 把 `web/report.js` 里
`getElementById("…")` 的 24 个 id 全部抠出来，逐个断言 `isochrone.html` 里存在。
抓的是「改了 HTML 忘了改 JS」：漏一个 id，`el.x` 就是 `null`，报告某一块永远空白，
而**控制台一条错误都不会有**。这是 R11「无构建模式没有类型检查兜底」那条风险的实际落点。

**`test_analysis_api.py::test_booleans_are_rejected_instead_of_coerced_to_one`** ——
pydantic 的宽松模式会把 `true` 强转成 `1`，于是 `{"radius": true}` 变成一个**1 米**的
检索半径并**静默返回 200**：调用方拿到一份看起来正常、其实只搜了脚下一平方米的报告。
这条断言把它钉成 422。

**`test_coverage.py::test_fair_selection_prevents_the_dense_area_score_inversion`** ——
一条缺陷回归，见下节。

---

## 2. 开发过程中发现并修复的五个真实缺陷

五份都**不是**通过 code review 发现的，而是「把服务层真正接上 API 并跑起来」
「把 CI 脚本真的执行一遍」「在最小 DOM 打桩下真跑一遍绘制路径」之后暴露的。

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
| `beijing-jiaodaokou` | 成熟老城 | 48 | **67.25** | C | 7.59 % |
| `beijing-tiantongyuan` | 新建大盘 | 14 | **84.66** | B | 18.44 % |
| `beijing-xiaojiahe` | 城中村 | 23 | 44.42 | D | 96.81 % |

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
| `beijing-jiaodaokou` | 96.20 | A | 100 |
| `beijing-tiantongyuan` | 84.66 | B | 74.09 |
| `beijing-xiaojiahe` | 44.42 | D | 0 |

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
| 交道口街道 | 成熟老城 | 96.20 | A | 48 | 4 | 7.59 % |
| 天通苑 | 新建大盘 | 84.66 | B | 14 | 2 | 18.44 % |
| 肖家河 | 城中村 | 44.42 | D | 23 | 1 | 96.81 % |

三型的分数与盲区面积呈**单调反向**关系，差异肉眼可辨——这正是 FR-102
「三型的圈内设施计数与盲区面积要有可见差异」那条要求的验收形态，
`test_three_forms_differ_in_the_expected_direction` 把它钉成了断言。

**六维雷达（`mock`，步行 15 min）**：

| 维度 | 交道口 | 天通苑 | 肖家河 |
| --- | ---: | ---: | ---: |
| 基础医疗 | 100 | 74.09 | 0 |
| 教育 | 100 | 100 | 0 |
| 购物市场 | 100 | 100 | 83.33 |
| 养老休闲 | 100 | 100 | 100 |
| 公共交通 | 100 | 95.76 | 98.56 |
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
| `score` | **96.20 A**，盲区 4 块 / 7.59 % | **报告完整**，不是错误页 |
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

**仍未验证的（说清楚边界）**：`live` 链路本身——真实等时圈形状、真实 POI 检索结果、
真实 `direction` 补路径、以及 JSAPI 底图，**一条都没跑过**，因为本环境从未有过可用 AK。
上表验证的是「没有 AK 时系统是否诚实且不崩」，不能读作「预案 A 已通过」。

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
4. **待补**：拿到 AK 后，对同一样例各跑一次 `live` 与 `mock`，比较两种模式下
   `area_km2` 与各维度 `count_in` 的差异——那才是「实测 vs 插值」的真实偏差。
   当前环境下无法进行。

---

## 6. 未验证项（诚实登记 / 风险 R15）

以下项目**没有**被验证过，不应被读作「已通过」：

| 项 | 原因 | 影响 |
| --- | --- | --- |
| live 模式端到端 | 本项目从未有过可用 AK | 演示预案 A 必须在有 AK 的机器上另行实跑 |
| `docker compose up` / `docker build` | 开发机没有 Docker | Dockerfile 与 compose 只做过静态校验（YAML 解析、指令与上下文路径核对、`.dockerignore` 规则推演） |
| 移动端**真机**行为 | 只有 Chrome DevTools 模拟，无 iOS / Android 实机 | 手指惯性拖图、双指缩放、iOS Safari 滚动穿透三项未验证；官方也未公开 JSAPI 4.0 的精确移动兼容矩阵 |
| 样例社区坐标的真实性 | 无 AK，无法走 `/api/suggest` 或地理编码核验 | 三份清单均标 `coord_verified: false`，`/api/samples` 会把这个标记原样透出 |
| 浏览器内真实渲染 | 本环境的无头 Chrome 无输出（被沙箱挡住） | 改用 Node + 最小 DOM 打桩真跑 `web/report.js` 渲染路径（`tools/report_dom_smoke.mjs`，12 项断言全部通过），但它**不是**真实浏览器 |
| 前端截图 | 本项目历史上浏览器无法落地截图 | 以 DOM 计数、计算样式、network / console 文字证据替代 |

---

## 7. 复现步骤

```bash
# 1) 全量单测（零配置、零 AK、零出网）
pytest -q

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
python -c "import json;from fastapi.testclient import TestClient;from api.app import app;open('_map_input.json','w',encoding='utf-8').write(json.dumps(TestClient(app).post('/api/analysis',json={'sample_id':'beijing-jiaodaokou','minutes':15,'data_mode':'mock'}).json(),ensure_ascii=False))"
node tools/map_layers_smoke.mjs _map_input.json

# 6) 静态检查
ruff check . && black --check .
```

第 4、5、5b 三条都不依赖 AK，也不访问任何外部服务。
