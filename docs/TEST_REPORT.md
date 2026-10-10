# CircleDoc 测试与对比实验报告

| 项 | 内容 |
| --- | --- |
| 文档定位 | PRD §9 交付物「对比测试报告」；对应 FR-405、FR-406、§6.1、§6.2 |
| 最后更新 | 2026-10-10 |
| 测试命令 | `pytest -q`（零配置、零 AK、零真实出网） |
| 结论 | **439 例全绿**，`ruff` / `black` 零告警；测试套件零真实出网、可全程离线复现 |

---

## 0. 先说清楚这份报告的边界

**2026-10-10 起本项目已接入真实百度 AK**（服务端 + 浏览器端两类，配在 gitignored 的 `.env` 里）。
所以这份报告现在有两类证据，**口径不同、不互相顶替**：

* **`mock` 侧**：439 例单测与两套冒烟自测仍在**零配置、零 AK、零出网**下全程复现——这是 CI 的形态，
  也是本文 §1、§2、§3、§5 的主口径。
* **`live` 侧**：§4.3 是一次**真实出网**的端到端实跑（三样例各一次），§6 的「JSAPI 实底图顶点点击」
  是另一次真实浏览器 + 真实出网的补验。§2.6 ~ §2.9 四个缺陷**只有在 live 下才暴露**——
  离线演示阶段它们全被「合成报文比真实报文更完整」盖住了。

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
| `test_analysis_api.py` | 38 | `/api/analysis` 报告形状、任务轮询、后台模式、`/api/poi` 七维度、错误信封、**真实检索盲区偏大提示（`live` 与 `cache` 两态各断言一次 + `sample` 反向断言）** | §4.7、FR-205、FR-404、§4.4、§2.9 |
| `test_web_smoke.py` | 27 | 静态页可服务、零公网外链、**DOM 契约对账**、FR-212/222/213 的前端接线 | R11、R12、R13、FR-213 |
| `test_samples_baseline.py` | 16 | 三样例一致性、三型差异方向、基线不漂 | FR-102、FR-412、§6.1 |
| `test_coord_param_table.py` | 13 | 接口 × 坐标参数名 × 坐标顺序常量表 | FR-406、R8 |
| `test_health_contract.py` | 10 | `/api/health` 三个「数据模式」字段的位置与值域、零配置横幅、AK 不外泄 | §4.6、FR-404、FR-410 |
| `test_cache_store_singleton.py` | 4 | 缓存单例口径唯一 | FR-402 |
| **合计** | **439** | | |

耗时：**4.8 ~ 11.6 s**（同一轮 439 例，本机两次实测 4.76 / 4.84 s，另两次 8.94 / 11.55 s）。
这个区间**不是代码回归**，是后台负载：慢的那两次 `chrome-devtools` 浏览器与它的 node MCP 服务都在线，
纯 CPU 基准 `sum(range(10**7))` 同时段实测 0.644 s（正常量级的两倍），最慢单例
`test_task_registry_is_bounded` 也从 0.68 s 涨到 1.9 ~ 2.3 s。无网络、无外部数据库、无 Redis。

### 1.1 三条值得单独说的断言

**`test_web_smoke.py::test_report_js_ids_all_exist_in_the_page`** —— 把 `web/report.js` 里
`getElementById("…")` 的 25 个 id 全部抠出来，逐个断言 `isochrone.html` 里存在。
抓的是「改了 HTML 忘了改 JS」：漏一个 id，`el.x` 就是 `null`，报告某一块永远空白，
而**控制台一条错误都不会有**。这是 R11「无构建模式没有类型检查兜底」那条风险的实际落点。

**`test_analysis_api.py::test_booleans_are_rejected_instead_of_coerced_to_one`** ——
pydantic 的宽松模式会把 `true` 强转成 `1`，于是 `{"radius": true}` 变成一个**1 米**的
检索半径并**静默返回 200**：调用方拿到一份看起来正常、其实只搜了脚下一平方米的报告。
这条断言把它钉成 422。

**`test_coverage.py::test_fair_selection_prevents_the_dense_area_score_inversion`** ——
一条缺陷回归，见下节。

---

## 2. 开发过程中发现并修复的九个真实缺陷

九份都**不是**通过 code review 发现的，而是「把服务层真正接上 API 并跑起来」
「把 CI 脚本真的执行一遍」「在最小 DOM 打桩下真跑一遍绘制路径」
「**配上真实 AK 跑一遍 live 全链路**」之后暴露的。
其中 §2.6 ~ §2.9 四个**只在接入真实 AK 之后才可能出现** —— 在离线演示阶段，
它们全都被「合成数据比真实数据更完整」这件事盖住了。

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
| live 模式下 FR-221 盲区结论的**准确性** | `market` 系统性为 0，见 §4.4 | 盲区面积在 live 模式下**系统性偏大**，不能当成真实结论呈现；根因已定位在检索层，无 PRD 框架内的干净修法。**2026-10-10 定口径：接受并明示**（见 §4.4 末的决策段）——真实检索那一侧（`data_source = live` **与 `cache`**）的报告由后端 `notes` 自动带这条提示，`mock` 口径不受影响；`cache` 一起算的原因见 §2.9 |
| `docker compose up` | 开发机没有 Docker，CI 用的是裸 `docker run` | `docker build` **已由 CI 真实执行并跑绿**（2026-10-07：镜像不含 `.db`、不含明文 AK、零 env 起容器 → `HEALTHCHECK` 转绿 → `/api/health` + `/api/samples` + `POST /api/analysis` 可服务）。**仍未验**：compose 的起停、以及 CI 没挂 `-v` 所以「Linux 宿主 UID 1000 对 `./data` 的写权限」这条在开发机上复现不出来 |
| 移动端**真机**行为 | 只有 Chrome DevTools 模拟，无 iOS / Android 实机 | 手指惯性拖图、双指缩放、iOS Safari 滚动穿透三项未验证；官方也未公开 JSAPI 4.0 的精确移动兼容矩阵 |
| 浏览器内真实渲染 | 本环境的无头 Chrome 无输出（被沙箱挡住） | 改用 Node + 最小 DOM 打桩真跑 `web/report.js` 与 `web/isochrone.js` 的**绘制路径**（`tools/report_dom_smoke.mjs` **15 项** + `tools/map_layers_smoke.mjs` **12 项**，`grep -c 'OK '` 实测；此前本文写 17、README/DESIGN 写 12，三处都对不上脚本真实输出，已统一），但它**不是**真实浏览器 |
| 前端截图（**能拍、但不入库**） | 2026-10-10 起 chrome-devtools 通道能截图（实时底图与橙色真实路网线已现场目视确认） | **仍不提供 PNG**：截图必然含百度瓦片与版权条，把第三方地图影像固化进开源仓库是另一回事；本文件继续以 DOM 计数、network、状态行/tooltip 原文作证据 |

---

## 7. 复现步骤

```bash
# 1) 全量单测（零配置、零 AK、零出网）
#    ⚠️ 如果本机 .env 里配了真实 AK，按 CI 口径先把它们压掉，别依赖「碰巧没读到」：
#      DATA_MODE=mock BAIDU_SERVER_AK= BAIDU_BROWSER_AK= pytest -q
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

两套冒烟自测（`m1_smoke` / `m2_smoke`，脚本在仓库外的 `swarm/` 下）**自己压掉 AK**：
脚本在 `chdir` 前就把 `BAIDU_SERVER_AK` / `BAIDU_BROWSER_AK` 两个环境变量置空。原因是
pydantic-settings 的环境变量优先级高于 `.env`，本机一旦配了真实 AK，不显式压掉脚本会**真的**
打百度线上服务；而冒烟的出网守卫只装在父进程，`[E]` 段起的 uvicorn 子进程没有守卫，全靠这一手。
CI 本来就没有 `.env`，这条改动对它无感；2026-10-10 复跑 `m1` 35/35、`m2` 102/102，守卫均未触发。

### 7.1 文档里那几个「自测数字」也要用命令复核（本轮抓到三处漂移）

本文与 README / DESIGN 引用的**断言条数**都是手写的，而这三处早就和脚本真实输出对不上了。
复现命令与本轮实测值：

| 数字 | 复核命令 | 实测 | 此前文档写的 |
| --- | --- | --- | --- |
| `report.js` 取的 DOM 节点数 | `grep -o 'getElementById("[^"]*"' web/report.js \| sed 's/.*("\(.*\)"/\1/' \| sort -u \| wc -l` | **25** | 24（README §10.4 / 本文 §1.1 / DESIGN §10 / 两个脚本的注释都写的 24） |
| `report_dom_smoke.mjs` 断言条数 | `node tools/report_dom_smoke.mjs \| grep -c 'OK '` | **15** | 本文写 17、README/DESIGN 写 12 |
| `map_layers_smoke.mjs` 断言条数 | `node tools/map_layers_smoke.mjs _map_input.json \| grep -c 'OK '` | **12** | 12（对） |
| `test_web_smoke.py` 例数 | `pytest -q --collect-only tests/test_web_smoke.py` | **27** | README §10.4 写 18 例（本文 §1 表的 27 是对的） |

顺带修掉 `tools/report_dom_smoke.mjs` 头注释里那条**照抄会失败**的用法：它写的是
`node tools/_report_dom_smoke.mjs <report.json>`（文件名多个下划线前缀、且参数其实可省），
实际路径是 `tools/report_dom_smoke.mjs`，不传参数走内置样本。

🔴 教训与 §2.7 同源：**能算出来的数字就别手写**。DOM 契约那条断言本身是动态取 id 的，
所以「24」写错了它照样全绿——数字只存在于散文里，没有任何测试守着它。
