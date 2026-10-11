# CircleDoc 技术设计文档

| 项 | 内容 |
| --- | --- |
| 文档定位 | PRD `FR-411` 要求的技术设计文档；对应命题交付要求 3 |
| 覆盖范围 | API 调用策略、等时圈算法、POI 清洗策略、盲区识别算法、评分模型、真实对比测试 |
| 唯一事实出处 | 配额、接口路径、参数名、状态码、合规条款一律以 `docs/BAIDU_API_NOTES.md` 为准；本文**不复制**该笔记内容 |
| 对比数据留档 | `docs/bench_route_strategy.json`（由 `tools/bench_route_strategy.py` 生成） |
| 测试与验证记录 | `docs/TEST_REPORT.md`；前端验收记录 `docs/shots/README-isochrone-acceptance.md` |
| 最后更新 | 2026-10-10 |

---

## 0. 本文怎么读

### 0.1 四节按「引用 + 落实」写，不抄笔记

`FR-411` 明文要求：**API 调用策略、配额计量、坐标参数、合规判定四节直接引用
`docs/BAIDU_API_NOTES.md` §2.8 / §3 / §6.2 / §7.3，不重复抄写以免两处失同步。**

本文严格照办。第 3、4、5、6 章的写法固定为三段：

1. **笔记说什么** —— 只写「去读哪一节」，不复述结论；
2. **本项目怎么落实** —— 落到具体文件、常量名、函数名，这是笔记里没有的信息；
3. **为什么不走另一条路 / 代价是什么** —— 决策与代价。

这样做的直接收益：百度改了状态码或参数名时，只需要改笔记一处，本文不会变成第二份会过期的
「权威」。代价是本文单独阅读时不完整，必须与笔记并读——这是刻意接受的，因为**两份会分叉的
事实表比一份更难维护**。

### 0.2 数字口径约定

全文遵守渲染在 `tools/bench_route_strategy.py` 模块 docstring 里的那条纪律：

| 标记 | 含义 |
| --- | --- |
| **实测** | 本地可精确测量的量：HTTP 请求数、配额单位、本地 CPU 耗时、限流排队行为 |
| **推算** | 依赖真实网络的量：线上墙钟耗时。RTT 是**显式假设参数**，不是测出来的 |
| **未验证** | 本项目条件下无法验证，明确登记在第 12 章，不当作「已通过」 |

🔴 2026-10-10 起本项目**已接入真实百度 AK，并有 `live` 模式端到端记录**（三样例实跑见
`docs/TEST_REPORT.md` §4.3）。但**压测类实验仍然只在 `httpx.MockTransport` 上完成**——这不只
是条件问题，也是条款 3.2.3(ii) 的要求：它明文禁止对百度线上服务施压测试，而「跑 96 次串行
`direction`」正是一种压测。两类证据的口径不同，本文与测试报告都**分开陈述、不互相顶替**。

---

## 1. 架构总览

### 1.1 分层原则

三层，职责按「谁离 HTTP 最近」切，而不是按「谁离业务最近」切：

| 层 | 目录 | 只允许做什么 | 绝对不做什么 |
| --- | --- | --- | --- |
| **HTTP 形状层** | `api/` | 路由注册、入参模型、错误信封、`request_id`、三栏响应头、静态目录挂载 | 任何业务判断。`api/app.py` 的模块注释写死了这条：「任何业务判断都不许写进来」 |
| **业务与算法层** | `services/` | 编排、纯函数算法、降级链、落圈判定、评分 | 自己拼 HTTP、自己 `sleep`、自己发并发请求 |
| **出网与基础设施层** | `infra/` | 唯一的出网收口、令牌桶、熔断、缓存、错误语义表、配置、日志、计量 | 任何业务语义。它不认识「等时圈」这个词 |

这条切法的理由不是审美，而是 `FR-406` 的可测性要求：**算法层必须是纯函数、外部依赖可注入
mock**。`services/isochrone_geometry.py` 因此写成「零 I/O、零 asyncio、零 HTTP/百度概念」的
纯几何模块——它能被单测直接调用，不需要任何网络桩。反过来，出网逻辑全部挤在
`infra/baidu_client.py` 一处，CI 传一个 `httpx.MockTransport` 就能把整条链跑完。

**代价**：一次等时圈计算要在三层之间来回穿梭（几何层算下一个探测点 → 服务层出网 → 回到几何层），
接口（`IsochroneOracle` 协议）比「一个函数写完」复杂。收益是 `tests/test_isochrone_geometry.py`
的 54 例可以完全离线、毫秒级运行。

### 1.2 一张图

```
┌────────────────────────── Browser（零构建链、零公网 CDN）───────────────────────────────┐
│  index.html + app.js                    等时圈地图页                    报告面板        │
│  ─ 数据模式徽标                         isochrone.html/js/css          report.js/css    │
│  ─ 服务探测表                            ├─ SVG 舞台（默认，永远可用）   ├─ 评分卡      │
│  ─ FR-105 离线横幅                       ├─ JSAPI 舞台（仅 live 注入）   ├─ 雷达图      │
│                                          ├─ 等时圈多边形 / 顶点 / 热力层 ├─ 柱状图      │
│                                          └─ 灰色盲区块                  ├─ 明细表       │
│                                                                        └─ CSV/JSON      │
│                                   ▲                                                     │
│                                   │  window.CircleDocMap（窄接口，方法名见 §1.4）       │
└───────────────────────────────────┼─────────────────────────────────────────────────────┘
                                    │ REST / JSON
┌───────────────────────────────────▼──────────── FastAPI · api/ ─────────────────────────┐
│  只做「HTTP 形状」：ROUTERS 注册表、入参模型、统一错误信封、request_id、三栏响应头      │
│  /api/health        /api/geocode      /api/regeo        /api/suggest                    │
│  /api/isochrone     /api/isochrone/polyline            /api/poi                         │
│  /api/analysis      /api/analysis/{task_id}            /api/samples                     │
│  /api/cache/stats                                              （共 11 条路由）         │
└───────────────────────────────────┬─────────────────────────────────────────────────────┘
                                    │ 纯 Python 调用（同进程）
┌───────────────────────────────────▼──────────── services/ ──────────────────────────────┐
│  等时圈  isochrone_geometry.py（纯几何，零 I/O）                                        │
│            ↕ IsochroneOracle 协议        route_probe.py（批量出网 + 三栏计量）          │
│          isochrone_service.py（同步编排 + 线程桥，把两者接起来）                        │
│  POI    poi_categories.py（官方词表 + 逐词自检）→ poi_search.py（7 次 `$` 合并检索）    │
│         → coverage.py（射线法落圈 + 分层前 K 实测 + 单维得分）                          │
│  盲区   blind_spots.py（250 m 网格 + haversine + flood fill + 轮廓追踪，**零出网**）    │
│  评分   score.py（加权 / 归一化裁决 / 盲区惩罚 / 摘要，纯函数）                         │
│  编排   analysis.py（一键体检 ①圈 ②POI ③落圈 ④盲区 ⑤评分）                              │
│  样例   samples.py（读 config/samples.yaml，不出网、不合成）                            │
└───────────────────────────────────┬─────────────────────────────────────────────────────┘
                                    │ 唯一的出网收口
┌───────────────────────────────────▼──────────── infra/ ─────────────────────────────────┐
│  baidu_client.py  判据只看 body `status`；三栏计量；响应级缓存；重试；熔断；预算守卫    │
│  ratelimit.py     按服务分桶令牌桶；route 双模式共享父令牌（PoolView）                  │
│  breaker.py       双熔断器：concurrency_breaker / quota_breaker，**按服务独立**         │
│  cache_store.py   响应级 SQLite（五列，无可查询业务字段，过期物理删除）                 │
│  cache_keys.py    缓存键与 TTL 的唯一口径（AK/SN 绝不进 key）                           │
│  errors.py        BAIDU_ERROR_TABLE：状态码 → 处置语义的唯一出处                        │
│  config.py / log.py / metrics.py                                                        │
└───────────────────────────────────┬─────────────────────────────────────────────────────┘
                                    │ HTTPS · 服务端 AK
        api.map.baidu.com（地理编码 / 逆地理编码 / 地点检索 / 批量算路 / 路线规划）
```

### 1.3 三个值得单独说的结构决定

**① `ROUTERS` 是写死的列表，不做自动发现。** `api/app.py` 里刻意不用 `importlib` 扫目录：
路由注册属于「装配」，出事故时人要能一眼看出到底挂了哪几个。自动发现会让「health 没注册」
只在演示现场才暴露。**代价**：新增路由要改两处（模块 + 注册表），忘了改就是 404。

**② 等时圈的几何与出网被 `IsochroneOracle` 协议隔开，中间用线程桥连接。** 几何层是同步纯函数，
出网是异步的，两者按 `PRD §4.2` 的写法必须交替推进（算探测点 → 出网 → 判收敛 → 再算）。
`isochrone_service.py` 的解法是：几何在 `asyncio.to_thread` 的 worker 线程里推进，需要出网时用
`asyncio.run_coroutine_threadsafe(...)` 把 `RouteProbe.probe()` 投回**原事件循环**执行。
于是 CPU 不占事件循环、HTTP 仍由循环单点**串行**发出。🚫 任何时候都不 `gather` 多个
`route_matrix`——「有界线程池并发 `direction`」正是 PRD D8 与条款 3.2.3(ii) 明确否掉的施压形态。
**代价**：多了一层线程桥与 `BRIDGE_TIMEOUT_S = 90.0` 的超时语义（超时按 `MISSING` 回给几何层，
由状态机判 `blocked_timeout`，绝不当成官方禁行）。

**③ 降级链只有一条，写在 `infra/baidu_client.py` 里，各业务层不再各建一套。**
`live → cache → sample/mock` 三层，其中 **cache 层就是客户端内置的响应级短 TTL 缓存**。
`isochrone_service.py` 明确「不新建第二份 SQLite」：条款 3.3.2(ii) / 3.3.4 禁止把响应存成
可查询的数据库，再叠一层「等时圈结果库」是合规风险而不是性能优化。缓存命中时
`http_requests` 与 `quota_units` **双双归零**——那不是漏账，是真没花，`data_source` 据此报 `cache`。

### 1.4 前端的图层交接：一份响应，两个引擎，四层叠放

FR-212（POI 上图）与 FR-222（盲区上图）的数据都在 `/api/analysis` 的**同一次响应**里，
所以前端**不再发第二次请求**。交接走一个刻意收窄的接口：

```
report.js  ──setOverlays({pois, blindSpots})──▶  isochrone.js
   │                                                  │
   └── 只做「摊平 + 转交」                       ┌────┴────┐
      （flattenPois / blind_spots.geojson）      │         │
                                            SVG 舞台    JSAPI 实底图
                                            drawPois()  BMap.Circle
                                          drawBlindSpots() BMap.Polygon
```

**四条设计约束**（每条都对应一个具体的坑）：

1. **只暴露方法名，不共享数据结构。** `window.CircleDocMap` 是唯一接口面
   （`adoptIsochrone` / `setOverlays` / `setCenter` / `focusOn` / …）。
   报告面板不该知道舞台、投影、热力层这些内部结构，否则两边会一起长成没法改的样子。
2. 🔴 **`setOverlays` 与 `adoptIsochrone` 分开。** 前者是 **M4 报告的产物**，
   单独跑 `/api/isochrone` 不会产生它们。分开之后「接等时圈」与「接报告图层」互不牵连，
   M2 那条路径调 `setOverlays(null)` 就能清干净，不留上一份报告的残影。
3. 🔴 **`geojsonRings()` 必须同时认 `Polygon` 与 `MultiPolygon`。**
   `blocks_to_geojson()` 在连通块边界不连通时会输出 `MultiPolygon`（实测交道口 4 块里
   就有 1 块是 3 个分离环）。只取 `coordinates[0]` 会让那个块**被静默丢掉** ——
   「报告说 4 块、地图上只有 3 块」，控制台一条错误都没有。见 `docs/TEST_REPORT.md` §2.5。
4. **叠放顺序即语义**：`g-grid → g-ref → g-heat → g-other → g-poly → g-blind → g-pois → g-vertices → g-center`。
   盲区必须在等时圈**之上**（它是圈内的结论，被圈盖住就看不见）；
   POI 再压在盲区之上（否则「这里缺什么」会盖住「这里其实有什么」）。

**两条渲染路径的取舍**：SVG 舞台那条用 `DIMENSION_STYLE` 的**颜色 + 形状双通道**
（色盲可读，且离线演示走的正是这条）；JSAPI 那条用同色 `BMap.Circle`
——JSAPI 4.0 自定义图标要外部图片资源，而 R12 明令零公网外链。
两处共用同一份 `BLIND_STYLE` / `DIMENSION_STYLE` 常量，不各写一套配色。

**未实测的视觉区分**：`unmeasured` 的点在 SVG 里用**虚线描边**、在 tooltip 里明写原因
（「未实测 —— 未进前 K（配额取舍，不是数据缺失）」），**绝不渲染成 0 分钟**。
这与 §9 评分层「没实测就是 0 分时效分」是同一条纪律的前端落点。

---

## 2. 等时圈算法

对应 `PRD §4.1`/`§4.2`、`FR-201`~`FR-206`。实现分两个文件：
`services/isochrone_geometry.py`（纯几何，2234 行）与 `services/route_probe.py`（出网，1140 行）。

### 2.1 命题约束决定了算法形状

命题明确「不获取底层路网数据」，所以只能**外部测时 + 内部插值**：

> 给定中心点 C，只用公开 API 的调用结果，推导出该点的真实路网可达范围。

全量网格逐点测时的配额消耗是千级路线，直接超过个人档 5,000 条/日的一半以上（PRD D4）。
扇形采样把配额控制在 ≈96 条路线，且命题原文鼓励「以采样替代全量计算」。

### 2.2 六步流程

```
Step1 粗扫（12 方位角，间隔 30°）
  对每个方位角 θ，取探测点 P = C + d·u(θ)，d 上界 R_max
  → 1 次批量算路：origins=[C]，destinations=12 个探测点 → 12 条路线，1 个 HTTP 往返
  COARSE_BEARING_COUNT=12 / COARSE_BEARING_STEP_DEG=30.0

Step2 二分定位（BisectionMachine，每方向一台状态机）
  在 [d_lo, d_hi] 上二分，求 t(P) ≈ T
  停止条件三者任一：区间 ≤ TOLERANCE_S=30 s / 轮次用尽 MAX_BISECT_ROUNDS=5
                    / 区间宽度 ≤ MIN_INTERVAL_M=20 m
  该轮所有未定案方向打包成 1 次批量请求（按 ≤50 切分）
  R_max 处实测耗时仍 < T → 外推一次，标 extrapolated
  路网阻隔 → 判 blocked（官方硬判据见 §2.4）

Step3 中点加密（FINE_BEARING_COUNT=24，间隔 15°）
  仅对「相邻顶点耗时差 > REFINE_DURATION_GAP_S=60 s」的方向对加密，其余纯内插（零配额）
  总顶点数自适应控制在 MIN_VERTICES=12 ~ MAX_VERTICES=36

Step4 顶点排序与简化
  sort_ring_by_bearing()：按方位角升序 → 消除多边形自交（R4）
  douglas_peucker_ring() / simplify_ring()：容差 DP_TOLERANCE_M=15 m

Step5 路径坐标补拉（懒加载，不预取）
  批量算路**不返回 polyline** → 改为用户点击顶点时补调 1 次 direction
  硬性上限 POLYLINE_CALL_LIMIT=12

Step6 耗时场插值（供热力图与盲区使用）
  build_duration_grid()：圈内 GRID_STEP_M=250 m 网格
  IDW（IDW_POWER=2.0，最近 IDW_NEIGHBOURS=8 个源点，实测点优先于插值点）
```

### 2.3 两个必须分开的量：R_max 与理论半径

PRD 第 269 行纠正过 v1.0 的一个乐观假设（按 5 km/h 估出 1.25 km 理论半径；官方示例
`16748 m / 15491 s` 推算实际约 **3.9 km/h**）。本实现把两件事拆成两个函数，**防止调用方混用**：

| 概念 | 常量 / 函数 | 值 | 用途 |
| --- | --- | --- | --- |
| 二分**上界** | `WALKING_R_MAX_M` / `RIDING_R_MAX_M` | 1250 m / 4000 m | 只为「宁大勿小」——上界取小了会把顺畅方向截断成假圆，比多烧几条配额更糟 |
| **理论半径** | `theoretical_radius_m()` / `ModeSpec.theoretical_radius_m()` | 由 `WALKING_SPEED_KMH=3.9` 算 | **只用于评分阈值与文案**，绝不可当二分上界 |

骑行模式的 `ModeSpec.speed_kmh=None`——FR-206 只固定了 `R_max = 4 km`，官方没有可引用的骑行
速度口径，所以本模块**不编造数字**，`theoretical_radius_m()` 返回 `None` 让调用方自己决定。

### 2.4 `blocked` 只认官方硬判据

这是本项目与 v1.0 差别最大的地方之一。`route_probe.py` 的模块 docstring 第 ③ 条写死了这条纪律：
**绝不用「耗时超标」推断 `blocked`**。

| 官方信号 | 含义 | 归类 |
| --- | --- | --- |
| `status = 2001` | 无骑行路线 | `blocked_official` |
| `restrictions_status = 0x01 / 0x02` | 骑行禁行 / 逆行（按位与判定） | `blocked_official` |
| `links[].attr = 0x01` | 步行禁行（按位与判定） | `blocked_official` |
| `directionlite status = 7` | 官方明说无返回结果 | `blocked_official` |
| 空结果 / 缺格 / 网络不可达 / 超时 | **没测准**，不是路不通 | `blocked_timeout` |

对应常量：`STATUS_NO_RIDING_ROUTE = 2001`、`STATUS_NO_RESULT = 7`、
`RIDING_RESTRICTION_FLAGS = (0x01, 0x02)`、`LINK_ATTR_NO_WALKING = 0x01`。

**为什么必须分开**：`blocked_official` 是「这条路走不通」（修复动作：换中心点 / 换出行方式），
`blocked_timeout` 是「我没测准」（修复动作：重试）。把两者渲染成同一句话，等于把人往错误的
处置动作上引导——`web/isochrone.js` 的 `SAMPLE_LABEL` 因此用两段完全不同的文案，验收清单里
专门有一项断言「五种 sample 话术两两可区分」。

### 2.5 精度取舍与置信度标注

**扇形采样 + 直线推点是近似方法**，无法捕捉路网细粒度凹凸（如内部小巷）。不确定性用两个参数
表达：二分容差 `TOLERANCE_S = 30 s`、网格 `GRID_STEP_M = 250 m`。

每个顶点带 `SampleKind` 标签与 `confidence`，五档**两两不可合并**：

| `SampleKind` | 中文 | `confidence` | 语义 |
| --- | --- | ---: | --- |
| `measured` | 实测 | 0.95 | 批量算路直接测得该方向 |
| `interpolated` | 内插 | 0.70 | 由相邻实测方向内插，未单独出网 |
| `extrapolated` | 外推 | 0.45 | `R_max` 处仍顺畅，边界按外推估计 |
| `blocked_official` | 官方禁行 | 0.80 | 有官方硬证据 |
| `blocked_timeout` | 未测准 | 0.30 | 返回空或超时，**可重试** |

另有两条「区间已收窄但没命中 ±30 s」的降级置信度：
`CONFIDENCE_MEASURED_TIGHT = 0.85`（位置仍是真实实测点，只降置信度、**不改标签**）与
`CONFIDENCE_EXHAUSTED = 0.55`（烧完 5 轮仍不收敛 → 回退插值）。

**顶点数下限为什么是 12**：`MIN_VERTICES = 12` 对应粗扫的 12 个方位角。`blocked` 方向也必须
占位，否则多边形会在阻隔处自己收出一个**假缺口**——那等于在图上宣称那个方向走得通。

**DP 简化为什么可能「一个点都不删」**：偏离量是球面垂距（`cross_track_distance_m()`），
圆弧底线偏离 ≈ `R·(1-cos(θ/2))`，1 km / 24 点时约 34 m > 15 m 容差。此时不删点是**正确行为**——
15 m 清的是毛刺，不是把圆压成四边形。

---

## 3. API 调用策略

> **本节按 `FR-411` 要求引用 `docs/BAIDU_API_NOTES.md` §2.8（距离矩阵 / 批量算路），不复制其内容。**
> 关于「批量算路存在与否、官方名称、`origins × destinations` 上限、为何取代逐次路径规划」，
> 一律以该节为准。

### 3.1 本项目落实在哪

| 笔记 §2.8 的结论 | 本项目的落点 |
| --- | --- |
| 批量算路是官方设计的用法 | `infra/baidu_client.py` 的 `route_matrix()`；`ENDPOINTS` 里的 `routematrix_v2_walking` / `routematrix_v2_riding` |
| `origins × destinations` 有上限 | `MATRIX_MAX_ROWS = {"walking": 50, "riding": 50, "driving": 100}`；`route_probe.MATRIX_ROW_CAP = 50`；切分函数 `split_batches(items, cap)` |
| 并发 `direction` 属施压形态 | PRD D8 弃用「有界线程池并发」；`route_probe.py` 模块 docstring 明写「**不自己 sleep、不自己发并发请求**」，只顺序 `await` 1×≤50 批量请求 |

`RouteProbe.probe()` 的契约是「N 按 ≤50 切批、**顺序** `await`、缺格不抛异常」。
切分批次的实测结果见第 11 章（96 个目的地 → `batch_sizes = [50, 46]`，共 **2** 个 HTTP 请求）。

### 3.2 按服务分桶的令牌桶

`infra/ratelimit.py`。默认值在 `infra/config.py`（PRD §4.6 的建议值，**不是常量，须以账号控制台
实际档位为准**）：

| 桶 | `rate` (QPS) | `burst` | 稀缺度 |
| --- | ---: | ---: | --- |
| `place`（地点检索） | 0.5 | 1 | 100 次/日 —— 全线最紧 |
| `regeo`（逆地理编码） | 0.5 | 1 | 300 次/日 |
| `route`（批量算路 / 路线规划） | 2.5 | 2 | 5,000 条/日 |
| `geocode`（地理编码） | 1.0 | 2 | 5,000 次/日 |

**为什么不共用一个全局桶**：各服务稀缺度相差 50 倍。共用时，等时圈那 ≈96 条廉价路线会把令牌
吃光，紧随其后的地点检索先在 100 次/日的天花板之前撞上 QPS 墙——等于限流器替业务做了错误的
资源分配。

**🔴 双模式必须共享父令牌**（`PoolView`）。这是本项目修过的一个真实缺陷：`split_route_by_mode`
早期让 walking / riding 各建一个 2.5 QPS 独立池，合计 5 QPS > 官方「路线规划（合计类目）」的
3 QPS，现场表现是 `status=401` 直到 `status=302` 全天封禁。现在两个模式桶是**同一个 route 父池
的视图**，令牌、容量与排队锁都在父池上，「合计 ≤ rate」由**结构**保证而不是靠人记得除以 2。
`RateLimitRegistry.rate_warnings` 会把「同一官方档位下的独立池速率之和越界」如实报出来。

**⚠️ 一处刻意的口径冲突**：PRD §4.6 正文要求 `burst ≤ rate`，但它自己给的表里
`place(0.5/1)` 与 `geocode(1.0/2)` 的 `burst` **都大于** `rate`。`infra/config.py` 与
`infra/ratelimit.py` 都记了这条冲突，并且**刻意不加校验**——默认配置必须能直接启动（零配置约定），
贸然拦死会让 `uvicorn api.app:app` 在什么都没配错的机器上起不来。等 PRD 澄清后再收紧。

### 3.3 预算守卫

三栏各有独立预算（`infra/config.py`，`Settings.budget_snapshot()` 出快照）：

| 栏 | 默认上限 | 常量 | 守卫行为 |
| --- | ---: | --- | --- |
| `place_calls` | 8 | `PLACE_CALLS_PER_ANALYSIS_DEFAULT` | 6 维度 + 便民参考 + 1 次冗余 |
| `route_lines` | 150 × 模式数 | `Settings.route_lines_budget(mode_count)` | 出网前预检，不足即**显式截停并回传原因** |
| `quota_units` | 120 / 双模式 220 | `quota_units_budget(mode_count)` | 同上 |
| `http_requests` | 20 | `HTTP_REQUESTS_PER_ANALYSIS_DEFAULT` | 同上 |

**`route_lines` 为什么必须按模式数折算**：`route` 桶由步行、骑行、polyline 懒加载**三者共用**，
150 条是「单模式」口径。双模式连跑约 192 条 > 150，守卫会在第二轮中途截停并给出「结果不完整」——
这既不是配额问题（官方 5,000/日完全够用）也不是算法问题，**纯粹是预算单位写错**。
`Settings.route_lines_budget()` 的注释把这条写死了。

截停的实现口径：`route_probe.py` 出网前用 `client.route_budget_state(mode=..., next_lines=...)`
判断，不足时**显式截停并把原因回传**（供降级文案），**不静默丢数据**。缺格与部分完成一律
**返回结果对象、不抛异常**（`partial=True` + `missing_cells()`），照 `route_matrix` 既有契约做。

### 3.4 重试与熔断

判据表在 `infra/errors.py:BAIDU_ERROR_TABLE`，`infra/breaker.py` **不写任何 `if status == 4`
式的分支**——新增状态码只改那张表。双熔断器按服务独立：

* `concurrency_breaker`：连续失败达阈值 → 打开 `CONCURRENCY_BREAKER_HOLD_S`，半开只对它存在。
* `quota_breaker`：命中 `4` / `302` → **打开至次日零时**（`BreakerLifetime.UNTIL_NEXT_MIDNIGHT`）。

**为什么是两个而不是一个**：v1.0 只有一个 30 s 熔断器，它对并发超限是对的，对当日配额耗尽是
**错的**——`status=4`/`302` 当天不可恢复，30 s 后重连只会每隔半分钟再烧掉一次请求，把剩下的
配额加速耗光。**为什么按服务分**：地点检索 100 次/日、路线规划 5,000 次/日，全局一条熔断会让
「地点检索今天打光了」误伤成「路线也别打了」，而路线恰恰还能打 4,900 次。

---

## 4. 配额计量

> **本节按 `FR-411` 要求引用 `docs/BAIDU_API_NOTES.md` §3（配额与限流），不复制其内容。**
> 关于各服务的日配额与 QPS 档位、状态码表、限流行为的官方补充说明，一律以该节为准。

### 4.1 本项目落实在哪：三栏永不合并

`infra/metrics.py` 的 `Metrics` 是唯一账本，三栏字段名在整个项目里固定：

| 栏 | 口径 | 谁累加 |
| --- | --- | --- |
| `quota_units` | **配额口径** = `origins × destinations` 折算的路线条数 | `infra/baidu_client.py` |
| `http_requests` | **往返口径** = 真实 HTTP 往返次数 | 同上 |
| `place_calls` | 地点检索出网次数（`route` 类服务恒为 0） | 同上 |

外加 `route_lines` 按 mode 分栏（`{"walking": N, "riding": M}`），以及
`route_probe.TALLY_KEYS` 里那些审计字段（`matrix_batches`、`direction_calls`、`blocked_official`、
`budget_stops` 等）。

**🔴 为什么不许合并**：一次批量算路只发 **1 个 HTTP 请求**，却按 `origins × destinations` 折成
N 个配额单位。合成一个数字后，「8 次请求」既可能安全、也可能已经把 100 次/日的地点检索打光，
看板与验收都读不出差别。PRD §4.2 第 267 行明确：**只报 `http_requests` 会把配额消耗低估一个
数量级**。

三栏在 HTTP 响应头里直接回显，`curl -i` 就能核 §6.1 的三条断言，不必先解析整个响应体：

```
X-Place-Calls:  <place_calls>
X-Quota-Units:  <quota_units>
X-Http-Requests:<http_requests>
X-Elapsed-Ms:   <elapsed_ms>
```

### 4.2 三个容易写错的地方

**① `route_lines` 必须按 mode 分栏。** `FR-206` 硬事实①：骑行与步行**共用同一份 5,000 条/日
路线配额**，跑双模式即当日翻倍。若只给一个合并数字，§3.3 的预算守卫在第二轮中途截停时，
排查者无法判断是超在哪一层。

**② 缓存命中时两栏归 0，不是漏账。** `isochrone_service.py` 模块 docstring 第 3 条写死了这条：
命中缓存时 `http_requests` 与 `quota_units` **双双归 0**（真没花），`data_source` 据此报 `cache`。

**③ `coverage.py` 的 `place_calls` 恒为 0。** 落圈判定不打 `place`；POI 检索那一层的
`place_calls` 在 `PoiResultSet.usage` 里，由 `analysis.py` 汇总。在这里重复计一次就是两个数量级
的错。合成探针（`SyntheticCoverageProbe`）同样**如实为 0**，并刻意**不**模拟成「假装花了 96 条
配额」——`usage` 是给配额审计看的对账单，往里填没发生过的消耗比不填更糟。

### 4.3 预算断言的实测形态

第 11 章第 3 节给出了预算守卫真实生效的证据：把 `http_requests` 预算压到 §6.1 的 **20 次**，
同一份 96 个目的地的输入下，串行路径只完成 20 次（20.8%）且 76 次被截停，而批量路径用 2 次
请求覆盖全部 96 条路线。**这条例子的价值在于它证明了守卫是按 `http_requests` 而不是按
`quota_units` 计的**——两条路径的配额消耗完全相同（96），差别只在往返数。

---

## 5. 坐标参数

> **本节按 `FR-411` 要求引用 `docs/BAIDU_API_NOTES.md` §6.2（各接口指定坐标系的参数名），
> 不复制其内容。** 关于三种写法（`coordtype` / `coord_type` 数字 / `coord_type` 字符串）的
> 官方原文与坐标顺序差异，一律以该节为准。

### 5.1 本项目落实在哪：一张常量表收口

`infra/baidu_client.py:COORD_INPUT_PARAM` 是**唯一**的参数名出处，`BD09LL_NUMERIC = 3` 只出现
一次（它的注释写明：「同一个参数名两种值类型，是本项目最容易写错的地方，所以在这里只出现一次」）：

```python
COORD_INPUT_PARAM: dict[str, tuple[str, str] | None] = {
    "geocode": None,                                  # 地理编码无入参坐标
    "regeo": ("coordtype", BD09LL),                   # ⚠️ 无下划线、传字符串
    "place": ("coord_type", str(BD09LL_NUMERIC)),     # ⚠️ 有下划线、传数字
    "place_region": ("coord_type", str(BD09LL_NUMERIC)),
    "direction": ("coord_type", BD09LL),              # ⚠️ 有下划线、传字符串
    "routematrix": ("coord_type", BD09LL),
}
```

两条纪律：

1. **`ret_coordtype`（出参坐标系）一律不发。** 各接口默认即 BD09LL，而官方给出的值域三处不同，
   传 `bd09ll` 属于凭记忆猜测。`COORD_INPUT_PARAM` 的注释写明了这条。
2. **缺失的 service 一律不发该参数**，不假设它存在。

### 5.2 断言表把它钉住

`tests/test_coord_param_table.py`（12 例）是这张表的回归闸门。它断言的是「参数名 × 坐标顺序」
这一对容易静默出错的东西，其中一条值得单独引用：

```python
def test_underscoreless_spelling_belongs_to_regeo_only() -> None:
    no_underscore = sorted(service for service, (name, _) in entries.items() if name == "coordtype")
    with_underscore = sorted(
        service for service, (name, _) in entries.items() if name == "coord_type"
    )
    assert no_underscore == ["regeo"]
    assert with_underscore == ["direction", "place", "place_region", "routematrix"]
```

**为什么这条断言值得写**：三种写法只差一个下划线或一个引号，写错**不报错**——接口返回 200、
数据看起来正常，只是点位静默偏移。这是最坏的一类缺陷：没有异常、没有日志、只有一份错的报告。

### 5.3 坐标顺序与全链路 BD09LL

坐标顺序在项目里也有三种形态，各有明确归属：

| 形态 | 用在哪 | 收口点 |
| --- | --- | --- |
| `lnglat = "经度,纬度"` | 本项目对外 HTTP 契约 | `services/geo.py:parse_lnglat` |
| `{lng, lat}` 具名字段 | 内部几何计算 | `services/isochrone_geometry.py:LngLat` |
| `纬度,经度` | 百度 `location` 出参 | `infra/baidu_client.py:to_baidu_latlng` |

`LngLat` 是 `frozen=True, slots=True` 的数据类，**构造即校验**：非有限数或越界直接 `ValueError`。
它的注释写明理由：「顺序歧义是本项目历史坑（R8）」——用具名字段替代元组，让 `lng` / `lat`
在代码里不可能被写反。`as_lnglat()` 只接受恰好 2 个数的序列并按 `(lng, lat)` 解释，形状不对
当场 `ValueError`，**让服务层在入口就炸掉，而不是把偏移点位带进多边形**。

---

## 6. 合规判定

> **本节按 `FR-411` 要求引用 `docs/BAIDU_API_NOTES.md` §7.3（对 `FR-402` / `FR-412` 的逐条
> 判定），不复制其内容。** 关于各条款原文、`FR-402` / `FR-412` / `FR-213` / `FR-305` 的判定
> 结论与整改建议，一律以该节为准。

### 6.1 四件事，四个落点

| §7.3 的判定方向 | 本项目怎么落实 | 验证 |
| --- | --- | --- |
| 缓存必须限缩为「同进程运行时性能缓存」 | `infra/cache_store.py`：表只有五列 `cache(key, service, payload, created_at, expires_at)`，**没有**可按字段查询的 POI 维度表；索引只建在 `service` / `expires_at` | `tests/test_cache_keys_ttl.py`（86 例）断言 POI TTL 合规上限 |
| POI TTL 必须下调 | `CACHE_TTL_PLACE_HARD_MAX_S = 24 * 3600`；`cache_ttl_place_s` 默认取该硬上限；`ttl_for_service()` 在 `infra/cache_keys.py` 统一夹紧 | 同上 |
| 过期行必须**物理删除** | `CacheStore.cleanup()` 走 `DELETE`，全库**没有软删标记位** | `test_cache_keys_ttl.py` |
| 镜像不得预置 `.db` | `cache_enabled=False` 时连文件都不创建；`.dockerignore` 用 `**/*.db` 这类带层级的模式（`*.db` 只匹配根目录，排不掉 `data/cache.db`）；`Dockerfile` 不用 `COPY . .` 而是逐目录显式拷贝 | 静态核对（见第 12 章未验证项） |

### 6.2 最内层降级数据必须是程序合成的

这是 §7.3 里判定最重的一条（`FR-412` 的「录制真实返回随镜像分发」属**禁止形态**，不是扣分风险）。
本项目的落实是三个模块：

* `services/isochrone_service.py:SyntheticOracle` —— 用「中心点做种子的 PRNG + 各向异性速度场
  + 一条顺畅走廊 + 两道阻隔扇区」产出**耗时场**，多边形交给**同一套几何代码**二分算出来。
  所以合成等时圈既不是正圆（`FR-201` 要求与同半径正圆目视差异明显），也不是录制下来的百度返回。
* `services/poi_search.py:SyntheticPoiSamples` + `_ARCHETYPE_COUNTS` —— 词表驱动的虚构 POI 网格，
  名称一律 `样例-xxx-NN`，坐标落在 `SAMPLE_GRID_STEP_M = 180` 的虚构网格上。
* `tools/gen_samples.py` —— 把三份**冻结的 mock 体检报告**写进 `data/samples/`。

**🔴 一个容易被忽略的口径**：`data/samples/*.json` **不参与运行时降级**。`FR-105` 的 `mock`
模式走 `SyntheticPoiSamples` **现算**，不读这些文件。两者同源、结果一致，但**不能让运行时去读
基线文件**——那会把「可复算」变成「读死数据」，评审一改参数就会发现基线不再成立。这些文件的
价值是当**回归基线**（`tests/test_samples_baseline.py` 16 例逐字段回归），不是当数据源。

**代价**：演示真实感下降（合成点的名称是「样例-综合医院-01」这种虚构标识）。这是为合规付的
确定成本，PRD D9 记录了这个取舍。

### 6.3 不压测：所有验证都在本地 mock 网络层

条款 3.2.3(ii) 禁止对线上服务施压。落实方式是**结构性的**而不是靠自觉：

* `infra/baidu_client.py` 构造器接 `transport=`（`httpx.MockTransport`），CI 与演练都
  **不产生一次真实出网**。它的模块 docstring 把这条列为独立一节。
* `tools/bench_route_strategy.py` 的「96 次串行 `direction`」对照实验用 `MockTransport`
  且 AK 是假串（`FAKE_AK = "fake-ak-for-mock-transport-only"`）。
* 限流验证把桶调到 0.5 QPS 后**在本地排队**，不向真实 API 连发。
* 出网域名白名单：`BAIDU_BASE_URL = "https://api.map.baidu.com"`，实际校验以
  `Settings.baidu_allowed_hosts` 为准（PRD §5 防 SSRF）。

### 6.4 导出限缩与版权标识

* **导出只限当前一次分析**：`web/report.js` 的三条出口（`exportJson()` / `exportCsv()` /
  `exportBlindCsv()`）都只读内存里的 `current` 那一份结果，没有「全库导出」入口，也**不落 `localStorage`**。
* **JSON 只导结果对象**（分数 / 多边形 / 统计），不含原始 POI 记录集。
* **CSV 附来源与禁止二次分发声明**（`FR-213`）。
* **PNG 截图必须保留版权与审图号**（条款 6.6）——注意本项目当前**没有实现 PNG 导出**，
  因此不存在裁掉审图号的风险面。这是「功能未做」而不是「合规已满足」，
  如需补 PNG 导出，这条约束必须一并实现。
  🔴 **补的时候不能按「把屏幕上那张地图拍下来」设计**：我方图表确实是矢量（雷达图与柱状图由
  `report.js:130` 的 `svgNode()` 建出，克隆 SVG 走 `data URL → canvas → toDataURL()` 不引入跨域），
  但 **live 底图是 JSAPI 自己的 `<canvas>`、瓦片跨域且无 CORS 头**，`toDataURL()` 按规范应抛
  `SecurityError`——这一半**只有代码依据、没有实测**（验证要烧一次 JSAPI 浏览器端配额，按配额纪律等点头）。
  因此 FR-305 的 PNG 口径预先收口为「**矢量报告卡 + 版权/审图号文字带**」：条款 6.6 要的是导出品里
  **保留标识**，不是导出品**必须是那张在线瓦片图**。`静态图` API 返回的同样是跨域位图，绕不开这个判定。

### 6.5 页脚是条款 6.6 与 7.3 在本项目唯一的落点（FR-413）

「使用百度地图服务」这件事在页面上一共有两个落点：**地图区域内的版权条**（实时底图自带，由 JSAPI 渲染，
我方不删不改）与**页面页脚**。SVG 降级舞台不是真实地图、没有版权条，所以 6.6 / 7.3 的**主动声明**
只能写在页脚——`web/isochrone.html` 与 `web/index.html` 各一份，三行结构：

| 行 | 内容 | 条款 | 失效方式与守卫 |
| --- | --- | --- | --- |
| `.foot-links` | 首页 / 接口文档 / 健康探测 + 官方**隐私政策**与**服务条款**外链（`target="_blank" rel="noopener noreferrer"`，照官方页脚自己的 `<a>` 抄） | 7.3 | 外链哨兵 `test_no_external_asset_references` 反向钉死白名单：资源标签零外链，`<a>` 只允许这两条 URL |
| `.foot-legal` | 「本服务使用**百度地图开放平台**…」+ 缓存性质与 **TTL（地点 ≤ 24 h / 路线 ≤ 7 天 / 地理编码与逆地理 ≤ 30 天）**、合成示例声明、导出边界 | 3.3.4、7.3 | `test_compliance_footer_ttl_matches_config` 对着 `Settings.model_fields` 的**类默认值**逐项比对（读类默认值而非实例化 `Settings()`，免得本地调试值被判成合规写错） |
| `.foot-review` | **审图号 `GS(2025)4125`｜测绘资质 `甲测资字11111342`**（2026-10-11 实读自 `lbsyun.baidu.com` 页脚，带读取日期） | 6.6 | 🔴 **无可自动核对的真值**——官方只有 HTML 页脚，百度按年换号。自动化能验「这一行在不在」，验不了「还是不是那个号」，故登记为**季度人工复核项**（README §10.7） |

两个实现细节记在这里以免被当成随意选择：

* **`.page-foot` 是块级而不是 flex**：三段声明若挂在同一个 flex 容器里会被拉成横向并排的三块，
  `display:flex` 只给 `.foot-links`。
* **正文颜色不用 `--muted`**：#6b7280 在页面底色 #f6f7f9 上是 **4.51:1**，距 AA 的 4.5 只剩 0.01；
  同一支灰字在本项目调色板的五个真实底色里有**两个直接不过线**（`--ok-bg` 4.36、`--line` 3.87）。
  合规文字借用「次要文字」语义的 token 是类别错误，所以页脚用专用的 #4b5563（同一位置 7.05:1），
  由 `test_footer_disclosure_passes_aa_contrast` 在测试里重算 WCAG 对比度钉住。
  ⚠️ 这条断言**拦不住旧配色**（4.51 刚好过线），它拦的是以后换成更浅的灰；数字依据见
  [`docs/TEST_REPORT.md`](docs/TEST_REPORT.md) §2.16。

---

## 7. POI 清洗策略

对应 `PRD §4.3`、`FR-210`~`FR-214`。词表在 `services/poi_categories.py`，检索在
`services/poi_search.py`。

### 7.1 词表必须能被机器校验

`poi_categories.py` 的模块 docstring 把存在理由写得很直白：v1.0 有四个类别词在百度官方 POI
分类表里**根本不存在**（`菜市场`/`农贸市场`/`养老院`/`社区卫生服务中心`），另有三个词已被删除
（`快递驿站`/`理发店`/`社区服务中心`）。留着不存在的词，最坏的后果**不是「检索为空」，而是该
维度永远计数 0、被误判成「这个社区缺配套」**，直接污染 `FR-221` 的盲区结论。

所以词表有一把尺子：`assert_wordlist()` / `wordlist_problems()`。它逐词校验：

* 每个 query 词必须在 `OFFICIAL_POI_PATHS`（19 条官方分类路径）的二级名里；
* `BANNED_QUERY_TERMS` 里的词**绝不能出现**——`菜市场` 被特意钉在黑名单里，因为命题原文太有
  诱惑力，必须靠机器盯着而不是靠人记得；
* 每个维度的 `request_count` 必须恒为 1（谁改成按别名拆请求就会红）；
* `page_num` 必须恒为 1、`page_size` 必须是保守值 10；
* 六维权重合计必须正好等于 `SCORED_WEIGHT_SUM`。

`FR-221` 的「菜市场 → 市场」映射表 `PRD_V1_TERM_REPLACEMENTS` 同时服务两件事：README 要向
评审解释「命题写菜市场、代码里找的是市场」，自检要把已被替换的词钉在黑名单里防止有人改回去。

**构造即自检**：`PoiSearchService.__init__` 调一次 `assert_wordlist()`，**词表写歪就建不出服务
对象**，不会跑出一份「某维度永远 0」的假报告。`WordlistError` 刻意继承 `RuntimeError` 而不是
`CircleDocError`——词表写歪是**开发期缺陷**而不是运行时降级，绝不该被降级链吃成「本次结果为
sample」。

### 7.2 一个维度只用一次请求

这是把 v1.0 的「6 类 × 2~4 别名 = 12~24 次」压进个人档 **100 次/日**配额的关键手段。圆形区域
检索的 `query` 支持多关键字并集、`$` 分隔、最多 `MAX_QUERY_KEYWORDS = 10` 个：

| 维度 | 单次 `$` 合并 query | 权重 | 达标阈值 |
| --- | --- | ---: | --- |
| 基础医疗 | `药店$综合医院$诊所` | 20 % | ≥ 2 个 / ≤ 8 min |
| 教育 | `小学$幼儿园` | 15 % | ≥ 1 个 / ≤ 12 min |
| 购物市场 | `市场$超市$便利店` | 20 % | ≥ 3 个 / ≤ 8 min |
| 养老休闲 | `公园$休闲广场$健身中心$疗养院` | 15 % | ≥ 1 个 / ≤ 10 min |
| 公共交通 | `公交车站$地铁站` | 15 % | 公交车站 ≥ 2 个 / ≤ 5 min |
| 餐饮服务 | `中餐厅$小吃快餐店` | 10 % | ≥ 5 个 / ≤ 10 min |
| 便民服务（参考项） | `银行$ATM$维修点` | 不计分 | 仅列入明细表 |

六维度 + 便民参考 = **7 次**，加上 1 次冗余 → `PLACE_CALLS_PER_ANALYSIS = 8`。
`search_radius_for_mode()` 给出检索半径（步行 1500 m / 骑行 4800 m = `R_max × RADIUS_FACTOR 1.2`），
并在官方 10 km 上限内夹紧。

**FR-104 的类别多选就作用在这张表上**：`/api/analysis` 收一个可选的 `categories`（维度键数组），
勾选了哪几类就只发那几次的 `$` 合并检索——**它是「按维度削请求」而不是「按维度加请求」**，
省下的正是配额最紧的那几趟。三条口径必须在两端同时成立，少一端就是假数据：

1. **全选时不发这个键**。发七个键与不发在语义上等价，但缓存键不同——同一中心点会因为「全选的
   数组」与「缺省」是两把钥匙而**白烧一次真实检索**。前端 `selectedCategories()` 因此在
   `on.length === catBoxes.length` 时返回 `null`。
2. **空数组不等于「什么都不搜」**。`api/analysis.py` 写的是 `list(body.categories) if body.categories else None`，
   而 `[]` 是 falsy → 折成 `None` → **检索全七类**。所以「取消全部」必须由前端挡在 `fetch` 之前，
   不能指望后端报错兜住；后端对空数组的语义是「未提供」，不是「零类」。
3. **子集会改分母**。`weighted_total()` 按**本次检索到的**计分权重之和归一，`score.weight_sum`
   跟着变小，所以子集总分与全选总分不是同一个分母（报告里那条归一化说明恒在，测试
   `test_narrowed_categories_also_change_the_score_denominator` 钉住 0.95 → 0.55）。

第 2 条对盲区的污染是**单向**的，见 §8.3 ④。

**为什么半径要放大 1.2**：等时圈是多边形，POI 落在圈外的直线半径上仍可能是「最近可用设施」，
半径取小会让 `FR-211` 的「圈内 / 仅直线半径内」两种计数退化成一个数字。

**⚠️ 一个必须挡在 pydantic 强转之前的入参陷阱**：`api/poi.py:reject_bool()` 是
`mode="before"` 校验器，专门把布尔值从数值字段里挡掉。理由不是洁癖——pydantic 的宽松模式在
`int | float | str | None` 上会让 `true` 命中 `int` 分支并强转成 `1`，于是 `{"radius": true}`
变成一个 **1 米的检索半径**，一路跑到 `/place/v2/search` 然后**静默返回 200**：调用方拿到一份
看起来正常的报告，而它其实只搜了脚下一平方米。这类输入没有任何合理语义，只能拒。
（这是 `docs/TEST_REPORT.md` §2.3 记录的真实缺陷，`tests/test_coord_param_table.py` 之外由
`api/poi.py` 的校验器覆盖。）

### 7.3 类别判定走结构化字段

`FR-214` 要求优先用返回体的 `tag` / `classified_poi_tag`，而非依赖自由文本 `query` 是否命中。
`normalize_subcat()` 把两种来源统一折成英文 subcat 键：

* `"药店"` 与 `"医疗 | 药店"` → `pharmacy`（`SUBCAT_TOKEN_SEPARATORS` 切片后**逐片整词比对**）；
* **不做子串包含**——`综合医院` 含 `医院`、`小吃快餐店` 含 `快餐`，包含匹配会把它们判错，也会让
  「菜市场」这种不存在的词被模糊成某个合法类别；
* 归一不上时返回 `SUBCAT_FALLBACK = "other"`，保留原始 `tag` 文本让明细表看得见，而不返回
  `None`——盲区判定要的是「有没有命中三类设施」，`other` 天然是「未命中」。

🔴 **一个关键区分**：「药店」是 `medical` 维度里的**一个 subcat**，不是整个维度。`FR-221` 要的是
市场 / 药店 / 小学三类设施，按维度判会错成一判就判六个。所以 `BLIND_TRIO_SUBCATS =
("market", "pharmacy", "primary_school")` 是三个 **subcat**。

`FR-214` 的另一半是 **uid 去重**：同一 POI 被多个别名命中时按百度 `uid` 去重，保证明细表无重复项。

### 7.4 两套计数口径不许互相替换

`coverage.py` 模块 docstring 第 2 条列了三个数，**口径不同、不可相加**：

| 字段 | 口径 | 说明 |
| --- | --- | --- |
| `count_in` | 本地射线法判定「落在等时圈多边形内」的**返回页** POI 数 | 明细表只取第一页，所以 `count_in ≤ len(pois)`；达标阈值 ≥5 的维度（餐饮）**会被页数截断而系统性低估**，这是已知取舍 |
| `count_near` | `len(pois) - count_in` | 于是 `count_in + count_near == len(pois)` 恒成立，可自洽校验 |
| `count_total` | **官方返回体的 `total`**（上限 `TOTAL_CAP = 150`） | 含被官方 `place` 过滤掉的数据，与上面两个**不在同一个集合上** |

评分**只用 `count_in`**（PRD §4.5「圈内该类数量」）。

**`page_size` 按保守值 10 设计且一律不翻页**：官方三处自相矛盾（3.0 文档「10–20 最大 20」/
字段注释「单次最多 10 条」/ FAQ 2542「最多只支持召回 10 条」），取保守值。单类别最多 15 页，
**翻一次页就吃掉当日大半配额**。若实测能到 20，只改 `PAGE_SIZE` 这一个常量。

### 7.5 FAQ 2536：计数可能被系统性低估

检索返回空 → 该类别计数为 0 并标注 `empty_reason = "检索无结果"`，**不当作异常**
（`FR-403`：空结果是数据结论不是故障）。同时 `notes` 里带上 `NOTE_FAQ_2536_FILTERING` 的官方
口径说明：检索接口会出于数据保护过滤掉部分数据，因此圈内计数可能被**系统性低估**，
「0 个」应读作「检索未见」而非「绝对没有」。

**这条为什么必须写进报告**：否则评审看到「肖家河 0 个药店」会当成 bug。而且它确实会与
`services/poi_search.py:SyntheticPoiSamples` 的合成数据保持一致口径——合成层也复现了
「`total` 大于明细条数」这一现象（`SAMPLE_TOTAL_CAP = TOTAL_CAP`），好让这条口径在离线演示时
也能被看见。

---

## 8. 盲区识别算法

对应 `PRD §4.4`、`FR-220`~`FR-224`。实现全在 `services/blind_spots.py`（946 行）。

### 8.1 核心设计：主判定零 API 调用

这张卡存在的全部理由是**把「逐网格点 × 三类设施」的检索调用从 O(N×3) 次降为 0 次**——完全复用
`§4.3` 已经拉到的 POI 点集，只在本模块内做射线法预筛 + haversine 半径过滤 + 类别命中统计 +
8 邻域连通块聚合。

**这条纪律是可证伪的**：`detect_blind_spots()` 依然接收一个 `client` 参数，它存在的唯一目的是
让单测能塞进一个「被调用就抛错并计数」的假客户端——
`tests/test_blind_spots.py::test_main_verdict_makes_zero_api_calls`。`FR-221` 的调用量因此是
**0 次**，而不是「优化后很少」。

**为什么这条从「优化」升级为「前提」**：地点检索个人档只有 100 次/日且与输入提示**共用**
（笔记 §3.1、R1′）。逐网格检索一次体检就能打满当日上限。

### 8.2 四步流水线

```
① 候选点生成  generate_candidates(ring, step_m=250.0)
     bbox + destination() 步进 + point_in_ring() 射线法预筛
     → 圈内 250 m 网格候选点，按 (row, col) 升序；圈外不生成

② 半径判定    classify_cell(pois, point, radius_m=1000.0, required_subcats)
     在已拉取的 POI 点集内做 1 km 球面距离（haversine）过滤
     纯内存计算，零 API 调用

③ 两级判定    主判定（FR-221）：market ∩ pharmacy ∩ primary_school 三类命中数均为 0
                            → LEVEL_BLIND = "blind"（灰色，P0）
              次判定（FR-224）：命中 1~2 类 → LEVEL_INSUFFICIENT = "insufficient"（橙色，P2）

④ 聚合与轮廓  flood_fill(cells)          8 邻域连通块，种子按行列升序
              trace_block_outline(...)   单元格矩形并集的外轮廓 GeoJSON
              blocks_to_geojson(result)  FeatureCollection（FR-222 渲染用）
```

### 8.3 四个必须写对的口径

**① 网格必须与 M2 的耗时场同源。** 候选点直接照 `isochrone_geometry.build_duration_grid()` 的
格网算术生成（bbox + `destination()` 步进 + `point_in_ring()` 预筛），步长默认值取
`GRID_STEP_M = 250`。**两套网格若各算一份中心点，热力图与灰色区域图层会错位到看不出来**——
所以这里连「先向北走 row 步、再向东走 col 步」的顺序都保持一致；传入 `IsochroneResult` 时优先
复用其 `duration_field` 的 frame。

**② 判定粒度是 subcat，不是维度。** 见 §7.3。`REQUIRED_SUBCATS = ("market", "pharmacy",
"primary_school")`。命题原文写「菜市场」，官方分类表里没有这个词，对应项是**「市场」**——映射在
`SUBCAT_ALIASES` 与输出 `notes` 里都显式出现，避免评审按字面找「菜市场」判成功能缺失。

**③ `area_pct` / `area_km2` 只算完全盲区。** 两级判定**并存**呈现，但面积口径只统计
`level == "blind"`——把「配套不足」也计进去会虚增盲区面积。这也与 `score.py` 的盲区惩罚口径
一致（§9.4）。

**④ 「没检索」会被读成「没有」，而且只会往一个方向读错。** FR-221 的主判定是一行集合差
`missing = set(required_subcats) - set(hits)`（`services/blind_spots.py`），它看不见本次 POI 集合是
怎么来的。三个 `subcat` 住在 `market` / `medical` / `education` 三个维度里，所以 FR-104 取消这三个
维度中任何一个，对应那类设施**根本没进本次集合**，网格点上就表现为「1 km 内没有」——
**盲区面积与「完全盲区」计数只可能偏大、不可能偏小**（单向性来自集合差的结构：少喂数据只会让
`missing` 变大，永远不会让它变小）。实测的两个极端：天坛在多个维度子集下 `area_pct` 恒为 28.4054
（该点的盲区与本维度无关，说明「偏大」是上界而非每次都涨），交道口只留 `medical` 时从 47.63 %
跳到 93.49 %。次判定 `insufficient` 同样只依赖这三类，所以**非三类维度被取消时盲区层逐字节不变**——
这条既是解释也是边界：不能笼统写「任何子集都会让盲区偏大」。

处置是**双端各说一次**：后端在 `notes` 里挂警示（离线导出也带着走），前端在勾选变更时就先说
（`#category-hint`，态由 `data-blind-cat` 推出，而 `data-blind-cat` 与后端派生的
`BLIND_TRIO_DIMENSIONS` 由 CI 双向对账）。**不做**的两件事同样是决定：不锁死那三个勾选框，
也不静默补查被取消的三类——用户要的是「我勾了什么就查什么」，代价由文案承担而不是由请求量承担。

### 8.4 阈值可配置 + 诚实口径

`step_m`（默认 250 m）、`radius_m`（默认 1000 m）、`required_subcats`、`include_insufficient`
都是关键字参数，README 里按 `PRD §3.4` 的要求写明「1 km / 250 m 均可配」；关掉次判定即只剩
P0 口径。

**近似与诚实口径**（README 要照抄）：

* 距离是 **haversine 球面直线距离**，不是路网距离；
* `nearest_facility.minutes` 只在输入点自带 `measured_minutes`（`§4.3` 批量算路的实测值）时才置
  `measured=True`，其余一律按 `WALKING_SPEED_KMH = 3.9 km/h` 估算并置 `measured=False`，
  **绝不把估算当实测**；
* 面积口径始终是「格数 × 单格面积」（`(step_m/1000)²` km²），**与外轮廓形状无关**——轮廓是多边形
  并集，用它算面积会与格数口径不一致。

**精算是 P2 可选项**：`refine=False` **默认关闭**；开启时对每个盲区块几何中心调 1 次路线规划，
上限 `MAX_REFINE_CALLS = 5`，且必须单列在 `usage` 里（它属于「精算」，与主判定的 0 次分开报）。

---

## 9. 评分模型

对应 `PRD §4.5`、`FR-301`~`FR-304`。单维得分在 `services/coverage.py`，聚合在
`services/score.py`。`score.py` 是**纯函数层**：除 `load_score_config()` 读一次 YAML 外没有任何
I/O、没有网络、没有全局状态——这是 `FR-301`「分数可复算」的定义，`tests/test_score.py` 用
「打乱输入顺序 + 重复调用」两条断言钉住它。

### 9.1 单维度得分

```
S_i = 100 × (0.5 × 数量分 + 0.5 × 时效分)

数量分 = min(count_in / 达标阈值数量, 1)
         threshold_count <= 0 → 1.0 if count_in > 0 else 0.0（参考项，不做除零）

时效分 = clamp((T_上限 - 最近实测时间) / (T_上限 - T_理想), 0, 1)
         T_上限 = T_UPPER_MINUTES_DEFAULT = 15.0
```

🔴 **`nearest_minutes is None` → 时效分记 0**，并且调用方必须同时在 `nearest_reason` 里写明原因。
`timeliness_score()` 的注释写明：「这里绝不接受『直线距离换算的时间』作为入参替身——那个替身
会把『没测到』洗成『测得很好』，是本泳道唯一的假绿路径。」

### 9.2 `T_理想` 的 PRD 缺口

`PRD §4.5` 的公式只写符号 `T_理想`，`§4.3` 的阈值表**只给每个维度的「达标阈值分钟」**，
**没有任何一处给出逐维度的 `T_理想` 数值**。§4.5 第 318 行倒是有半句
`T_理想 = 阈值时间 × 0.5`，但它是个「关系」而不是「表」：既没写清 `阈值时间` 是不是同一张表里的
达标分钟，也和达标阈值本身冲突（公交达标 5 min × 0.5 = 2.5 min，在 15 min 上限之下几乎没有
区分度）。

本项目的处理：把 `T_理想` 做成**显式可配置参数**：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `T_IDEAL_RATIO_DEFAULT` | 0.5 | 来自 §4.5 第 318 行的半句关系式 |
| `t_ideal_minutes` | `{}` | 逐维度显式覆盖，优先级最高 |
| `resolve_t_ideal_minutes()` | — | 返回 `(值, 来源)`，来源进 `t_ideal_source` |

默认值在输出里以 `t_ideal_source` **如实标注来源**（`ratio:0.5` / `explicit_override:key=…`），
**不伪装成 PRD 定稿数字**。比例越界（不在开区间 `(0,1)`）时夹回 `(0.05, 0.95)` 并留痕：
比例 ≥1 会让分母 `T_上限 - T_理想 ≤ 0`，时效分整个失去定义。

### 9.3 95 % 权重缺口：完整推导与「为什么选归一」

这是 PRD 内部一个**真实缺口**，本项目必须显式裁决而不是悄悄糊过去。

**缺口是什么**：`§4.3` 的权重表是 `20 + 15 + 20 + 15 + 15 + 10 = 95 %`，**不是 100 %**；
而 `§4.5` 的等级线 `A ≥ 85 / B 70~84 / C 55~69 / D < 55` 是写在 **0~100 刻度**上的。

**两种读法都自洽**：

| 读法 | 公式 | 后果 |
| --- | --- | --- |
| **不归一** | `总分 = Σ(S_i × w_i)`，照 §4.5 字面 | 满分只有 **95**，A 档实际要求「达到可达上限的 **89.5 %**」 |
| **归一** | `总分 = Σ(S_i × w_i) / 0.95` | 满分 **100**，A 档就是字面的「85 分」 |

**为什么选归一**：`§4.5` 同时给出了公式与 A/B/C/D 四条线。若按字面不归一，满分 95 却用 85 当
A 线，等价于隐含要求「89.5 % 才算 A」——这个 **89.5 %** 在任何一处文档里都没写过，是**从缺口里
长出来的第二个隐含假设**。归一之后 A 线就是字面的「85 分」，与 `§4.5` 自洽；代价是 `§4.3` 表里
的 `20/15/20/15/15/10` 被当作**相对权重**而非绝对百分比——这也正是「权重」一词的常规读法。

**分层归属**：`services/poi_categories.py` 在词表层**刻意拒绝**替 M4 做这个决定（它的
`SCORED_WEIGHT_SUM = 0.95` 注释写明「归一与否会改变每一个维度的最终得分，属于 M4 评分层的产品
裁决」），并且把「合计必须正好是 0.95」当**断言**钉住——将来 PRD 澄清成 100 % 时，改这个常量会让
自检立刻要求同步改权重表，而不是让缺口静默地飘在两个文件里。

**裁决如何被审计与推翻**：响应里**同时**给出四个字段（`weighted_total()` 的返回值）：

| 字段 | 含义 |
| --- | --- |
| `total` | 用于评级的那个（默认已归一） |
| `total_raw` | `§4.5` 字面值 |
| `weight_sum` | 实际参与的权重之和（默认 0.95） |
| `normalized` | 本次是否归一 |

评审按哪种读法都能在自己的口径上复算，**不会看到一个对不上的数而无法判断是谁错了**。

**如何回退到字面口径**（两条路都不需要改代码）：

1. `config/weights.yaml` 里把 `normalize_weights` 设成 `false`（推荐——权重表本来就在那里，
   `load_score_config()` 会读这个键）；
2. 直接构造 `ScoreConfig(normalize_weights=False)` 传给 `build_score_block()`。

⚠️ **没有** `SCORE_NORMALIZE_WEIGHTS` 这个环境变量——该字段不由 `Settings` 提供，
   在 `.env` 里写它不会生效。这是本项目唯一一处「配置项在 YAML 而不在 `.env`」的旋钮，
   与 §3.3 的限流/预算旋钮（那些都在 `.env`）风格不同，写文档与演示脚本时别混。

**边界保护**：`weight_sum <= 0`（全部维度都不计分 / 上游没给权重）时**不归一**、总分记 0 并留一条
note——除以 0 会得到 `inf`，静默变成「满分」是最坏的一种失败。

### 9.4 等级与盲区惩罚

```python
DEFAULT_GRADE_BANDS = (("A", 85.0), ("B", 70.0), ("C", 55.0), ("D", 0.0))
PENALTY_PER_PCT = 0.5     # 每 1 % 圈内面积被标记为完全盲区，扣 0.5
PENALTY_CAP     = 15.0    # 上限 −15
```

* `grade_for()` 取第一个满足 `total >= 下界` 的档；`total` 不可解析时回 `"D"`。
* `blind_spot_penalty()` 返回**负数或 0**（不是「要扣多少」的正数），这样调用方直接
  `total + penalty` 即可，不必再记一次符号。`area_pct` 为 `None` / 负值 → 0（缺数据不扣分，
  也不奖励）。
* 惩罚按 `level == "blind"` 的面积占比算，与 `blind_spots.py` 的口径一致：`area_pct` **已只统计
  完全盲区**，不把「配套不足」混进来。

### 9.5 摘要生成

`build_summary()` 对应 `FR-304`，输出 3~5 条结论（`summary_max = 5`），由数据驱动而非硬编码文案：

| 门槛 | 值 | 用途 |
| --- | ---: | --- |
| `STRONG_SCORE` | 85.0 | 低于这条线就不说「充足」，改说「相对最好」 |
| `WEAK_SCORE` | 60.0 | 判「明显偏弱」 |

### 9.6 权重表外置

`config/weights.yaml` 由 `load_score_config()` 读取（`FR-305`/`§4.5` 要求权重表外置）。
文件缺失 / 读不动 / 格式错 → **回落到数据类默认值并把原因写进问题清单**，不抛异常
（理由与 `FR-403` 同一条：演示现场一个配错的权重文件不该让整个体检变成错误页），且「用了默认
权重」这件事必须可被看见——清单会进响应的 `score.notes`。

🔴 **真正起作用的那条线是 `ScoreConfig.dimension_overrides`**：它携带逐维度的
`weight` / `threshold_count` / `threshold_minutes`，交给 `build_coverage(threshold_overrides=...)`。
`score.py` 的注释写明：「只把权重写进 YAML 却没人读它，等于把『权重表外置』做成一句注释；
PRD §4.5 要的是**可替换**，不是可阅读。」

---

## 10. 三处对 PRD 的显式偏离

这一章是本文档最需要被仔细读的部分。三处偏离都不是「实现偷懒」，而是**照字面做会产出错误结论
或违反硬约束**。每处都给出：PRD 原文怎么写、为什么做不到/不该这么做、实测证据、如何回退到
字面口径。

### 10.1 偏离一：前 K 实测改用**分层选择**，而非 §4.3 字面的全局最近 K

**PRD 原文**（§4.3 第 4 条）：

> 只对「圈内 + 直线距离最近的前 K 个」（桌面 K = 12，移动端 K = 6）POI 取真实步行时间，
> **用 1 次批量算路**……这是控制调用量的关键设计。

**照字面实现后发生了什么**：实测三样例出现**分数倒挂**——

| 样例 | 建成形态 | 圈内点位数 | 总分 | 等级 |
| --- | --- | ---: | ---: | --- |
| `beijing-jiaodaokou` | 成熟老城 | **48** | **61.40** | **C** |
| `beijing-tiantongyuan` | 新建大盘 | 14 | **80.97** | **B** |

**设施更全的老城社区拿到 C，配套稀疏的新建大盘拿到 B。** 这与事实相反，会直接误导评审。

**机制**（`coverage.py:SELECTION_FAIR` 的长注释逐条写了）：

1. 配套密集的社区圈内点位多，全局最近的 12 个名额可能**全落在餐饮与便利店里**；
2. 于是医疗 / 购物 / 公交三个维度**一个都没实测到** → `nearest_minutes = None`；
3. → `§4.5` 的时效分记 **0**（`coverage.py` 刻意拒绝用直线距离顶替，见其模块 docstring 第 3 条）；
4. → 这三个维度被硬压在 **50 分**（数量分 100 × 0.5 + 时效分 0 × 0.5）；
5. 而稀疏社区点位少、几乎人人有实测值，六个维度时效分反而满分。

**为什么这是一处「该改 PRD 而不是改实现」的偏离**：`§4.3` 那条规则管的其实是**调用量**
（K ≤ 50、只发 1 批），这一点 `fair` **完全遵守**。但 `§4.3` 的达标阈值表本身写的是
「圈内数量 / **最近实测时间**」——**逐维度都要一个实测时间**，这说明 PRD 的本意**就是**每个
维度都得测到，只是没写清 K 该怎么分配。所以 `fair` 是在「K 总量不变、调用量不变、配额不变」的
前提下**补齐那个缺失的约束**。

**修法与代价**：新增 `select_measurement_targets_fair()`——第一轮给每个维度各取最近的一个
（按全局距离升序扫，谁最近谁先占名额），第二轮用全局最近点填满剩余名额（维度数 > K 时第一轮
就没填满，这里自然兜住）。

| 项 | `nearest`（字面） | `fair`（默认） |
| --- | --- | --- |
| K 总量 | 12 | **12（不变）** |
| 批次数 | 1 | **1（不变）** |
| 配额的 `quota_units` | `1 × 12` | **`1 × 12`（不变）** |
| 改变的只是 | — | 名额分配 |

**代价**：`fair` 牺牲了「全局最近的点一定被实测」这条直觉——某一维度里直线距离更近的点可能因为
本维度已占名额而落选，被标 `REASON_BEYOND_TOP_K = "beyond_top_k"`（"圈内但没进前 K——配额换出来
的取舍，不是数据缺失"）。这个代价是**可解释的**，而倒挂不是。

**修复后结果**（`docs/TEST_REPORT.md` §2.2）：

| 样例 | 总分 | 等级 | 六维雷达最小值 |
| --- | ---: | --- | ---: |
| `beijing-jiaodaokou` | **98.24** | **A** | 100 |
| `beijing-tiantongyuan` | 80.97 | B | 75.0 |
| `beijing-xiaojiahe` | 44.65 | D | 0 |

排序符合建成形态的事实。回归覆盖：
`tests/test_coverage.py::test_fair_selection_prevents_the_dense_area_score_inversion`。

**如何回退到字面口径**：`build_coverage(..., selection=SELECTION_NEAREST)`。报告里的
`meta.selection` 会**如实回显本次用了哪一种**，所以审计永远看得出跑的是哪条路。无法识别的
`selection` 值**回落到默认策略并继续**（不抛异常）——挑选策略是报告口径的一部分，不是入参契约的
一部分，一个拼错的值不该让整次体检 500。

### 10.2 偏离二：报告图表用**手绘 SVG**，而非 §5 写的 ECharts

**PRD 原文**（§5 前端资源）：

> Vue global build 与 ECharts（仅引入雷达/柱状模块）本地 vendored 随镜像分发，不引用公网 CDN；
> **总资源 ≤ 500 KB**。

**为什么三个条件无法同时成立**：ECharts 官方**只发整包**，不提供可裁剪的按模块产物：

| 产物 | 体积 |
| --- | ---: |
| ECharts `simple` | 457 KB |
| ECharts `common` | 647 KB |
| ECharts `full` | 1 007 KB |
| Vue（global build） | 143 KB |

而 `radar`（雷达图）属于 **`common` 层** → 最小可用组合 = **647 KB + 143 KB = 790 KB**，
**超 500 KB 预算 58 %**。要瘦下来必须上构建链做 tree-shaking，**那就推翻了 D2「零构建链」**。

**取舍：取第三方方案——手绘 SVG。** `web/report.js` 的 `drawRadar()` 与 `drawBars()` 共约
200 行、**零字节依赖**，与 M2 的 SVG 等时圈同一视觉风格（`svgNode()` 走
`createElementNS` + `textContent`，不做 `innerHTML` 拼接）。顺带把「断网也能出报告」这条承诺
做到毫无悬念——离线演示不再依赖任何一个第三方库的完整性。

**代价**（诚实登记）：

* 失去 ECharts 自带的交互（hover tooltip、图例点选、动画过渡）。当前实现里雷达图与柱状图是
  静态 SVG，数值直接标在图上，不提供悬浮交互。
* 图表代码由本项目自己维护，新增图表类型没有现成组件可复用。
* 与 `§5` 的「本地 vendored」纪律形式上不一致，但**实质目标（零公网 CDN、离线可用、体积可控）
  反而达成得更彻底**：当前前端第三方依赖总体积为 **0 字节**。

**如何回退**：若 500 KB 预算放宽或允许引入构建链，vendored 一份 ECharts `common` + Vue，
把 `drawRadar()` / `drawBars()` 换成对应组件即可——两者都只消费后端响应里的
`score.radar` 与 `coverage.groups`，接口边界是清晰的。这处偏离已登记在 `web/report.js`
的模块 docstring 第 1 条。

### 10.3 偏离三：前端实际是**原生 JS**，而非 D2 写的 Vue 3 global build

**PRD 原文**（§8.1 决策 D2）：

> 前端用 **Vue 3 global build（本地 vendored，无 Vite、无 npm 构建步骤）**……不引入 Vite 是为了
> 保住「零构建、`docker compose up` 即跑」，并砍掉构建失败这一类出错面。代价：没有 SFC、HMR
> 和 TS 类型检查，需靠 R11 的冒烟测试补一部分兜底。

**实际是什么**：前端全程是**原生 JS**，`web/vendor` 目录**不存在**。这不是 M4 才发生的——M2 起
就是这样。`web/isochrone.js` 的纪律第 3 条明确写着：

> D2/M2：沿用 M1 首页的**原生 JS**风格（不引 Vue、不引 vendored 库，那是 M4 的活）。

而 M4 的 `web/report.js` 接手时把同一路线延续下去（其模块 docstring 第 1 条：
「**零外部依赖、零构建链**」）。

**为什么最终没有引入 Vue**：三个理由叠在一起——

1. **D2 选 Vue 的原始理由是「一个结果对象要同时驱动四处渲染，手写 DOM 同步会失控」**。这个
   担忧是合理的，但实际解决它的是**结构而不是框架**：`report.js` 只维护 `current` 一个状态
   （"导出与重绘都只读它——没有第二处状态"），`isochrone.js` 只维护 `state` 一个对象，
   两边通过 `window.CircleDocMap` 这层窄接口隔开。四处渲染各自是一个只读 `report` 的函数
   （`renderScore()` / `renderCoverage()` / `renderPoiTable()` / `renderBlindSpots()`），
   没有双向绑定要管，Vue 的响应式系统在这里没有承担实际工作。
2. **与 10.2 是同一个决定的两面**：既然图表已经手绘 SVG，引入 Vue 就只为剩下的 DOM 装配付
   143 KB 与一整套运行时语义，性价比不成立。
3. **离线承诺**：`FR-105` 要求断网时报告与全部图表可用。少一个运行时依赖就少一类失败面。

**代价**（与 D2 预告的完全一致，R11 已登记）：没有 SFC、没有 HMR、**没有编译器与类型检查兜底**。
模板字符串拼 DOM 一律不用，所有节点走 `createElement` / `createElementNS` + `textContent`
（数据永不 `innerHTML` 拼接——顶点话术会回显后端给的错误文本）。

**这个代价的实际落点与兜底**：R11 那条「改了 HTML 忘了改 JS」的缺陷形态在本项目里**真实存在**——
`el.x` 会是 `null`，报告某一块永远空白，而**控制台一条错误都不会有**。兜底是
`test_web_smoke.py::test_report_js_ids_all_exist_in_the_page`：把 `web/report.js` 里
`getElementById("…")` 的 **26 个 id**（`grep -o … | sort -u | wc -l` 实测，FR-107 加了 `#profile-badge`）
全部抠出来，逐个断言 `isochrone.html` 里存在。
另有一条 `tools/report_dom_smoke.mjs`（Node + 最小 DOM 打桩，15 项断言）真跑渲染路径。

**顺带的后果**：`§5` 的「总资源 ≤ 500 KB」预算被**大幅超额达成**（第三方依赖 0 字节），
但 `§5` 字面要求的「Vue 与 ECharts 本地 vendored」这一条**没有满足**。README 需按此如实说明。

**如何回退**：若评审坚持 D2 的字面形态，需要把 `report.js` 与 `isochrone.js` 重写为 Vue
组件并 vendored 一份 Vue global build（143 KB）。这是一次结构性重写而不是配置改动——本节的
用意正是把这件事的成本讲清楚，避免被当成「换个引入方式」的小事。

### 10.4 三处偏离的一页速查

| # | PRD 字面 | 实际做法 | 触发原因 | 证据 | 回退方式 |
| --- | --- | --- | --- | --- | --- |
| 1 | §4.3：全局最近前 K 个 | `SELECTION_FAIR` 分层挑选 | 字面口径导致密集社区得分倒挂 | 交道口 61.40 C < 天通苑 80.97 B（2026-10-10 坐标核验后重算） | `selection=SELECTION_NEAREST` |
| 2 | §5：ECharts 雷达/柱状 | `drawRadar()` / `drawBars()` 手绘 SVG | ECharts 整包最小 647 KB + Vue 143 KB，超 500 KB 预算 58 % | `web/report.js` 模块 docstring | vendored ECharts `common` + Vue |
| 3 | D2：Vue 3 global build | 原生 JS，`web/vendor` 不存在 | M2 起即原生；窄接口 + 单状态已解决 D2 的原始担忧 | `isochrone.js` 纪律 3、`report.js` 第 1 条 | 重写为 Vue 组件（结构性改动） |

---

## 11. 对比测试报告

对应 `FR-405`、`§6.2` 第 1~2 条。原始数据留档 `docs/bench_route_strategy.json`。

**复现命令**：`python tools/bench_route_strategy.py --out docs/bench_route_strategy.json`
（零真实出网；`MockTransport` 不建任何连接、AK 是假串）

**输入**：同一中心点 `116.404, 39.915`，**96 个目的地**（等于等时圈一次采样的路线条数，`§4.2`）。
**推算假设**：RTT **120 ms**（假设值）、`route` 桶 **2.5 QPS**（`§4.6` 建议值）。

### 11.1 两条路径对照

| 指标 | 性质 | A 串行 `direction` | B 批量 `routematrix` |
| --- | --- | ---: | ---: |
| HTTP 请求数 | **实测** | 96 | **2** |
| 配额单位（`origins × destinations`） | **实测** | 96 | **96** |
| 本地 CPU 耗时 | **实测** | 0.0246 s | 0.0015 s |
| 线上墙钟耗时 | **推算** | 38.4 s | **0.8 s** |

* **请求数下降 48.0×**，**配额消耗完全相同**（`quota_units_identical = true`）。
* 批切分实测：`batch_sizes = [50, 46]`——96 个目的地按官方 `origins × destinations ≤ 50`
  约束切成 2 批（第 3 章 `split_batches` 的实际生效证据）。
* 推算提速 **48.0×**（模型值，**非实测**）。

**推算墙钟的瓶颈是令牌桶而不是往返**：`96 / 2.5 = 38.4 s` 远大于 `96 × 0.12 = 11.5 s`。
`model_wall_clock()` 取两者**较大值而不叠加**（桶在等的时候前一个请求已经在飞），所以这是个
偏保守的下界。这也是为什么推算必须用**真实档位**——用实验里那个 1000 QPS 的假桶去推算会把
主要成本整个漏掉。

### 11.2 真实出网预算下的行为（比上面更有说服力）

把 `http_requests` 预算调回 `§6.1` 的真实上限 **20 次**，同样输入 96 个目的地：

| 路径 | 完成的请求数 | 覆盖的路线条数 | 被守卫截停 | 结果 |
| --- | ---: | ---: | ---: | --- |
| A 串行 | 20 | 20 / 96（**20.8 %**） | 76 | 只能出一份不完整的等时圈 |
| B 批量 | 2 | **96 / 96（完整）** | 0 | 同样预算内完整覆盖 |

**串行路径不只是「慢」，它在 `§6.1` 认可的出网预算内根本跑不完。**
这是 `FR-405` 从「性能优化」变成「可行性与否」的那条线。

### 11.3 限流验证（`§6.2` 第 2 条，本地 mock 网络层）

把桶调到 **0.5 QPS**、`burst = 1`（`§4.6` 要求 `burst ≤ rate`，0.5 QPS 的桶容量只能是 1），
mock 掉网络层，连发 20 个请求：

| 项 | 实测值 |
| --- | --- |
| 拿到响应的请求数 | **20 / 20** |
| 排队过的请求数 | 19（`burst = 1` 立即放行 1 个） |
| 前三次等待 | 2.0 s / 2.0 s / 2.0 s |
| 最长单次等待 | 2.0 s |
| 总耗时 | **38.069 s** |
| 理论排队时间 `(20 − 1) / 0.5` | 38.0 s |
| **是否有请求绕过桶** | **否（`no_bypass = true`，零穿透）** |

实测 38.069 s 与理论 38.0 s 吻合，说明排队语义正确；「20/20 全部拿到响应」说明**没有任何请求
在穿透限流后被服务端拒绝**。

⚠️ 这一整节都在本地 mock 层完成，**没有向 `api.map.baidu.com` 发过一个请求**。

### 11.4 实测 / 推算分栏（本文档的诚实性声明）

| 指标 | 性质 | 依据 |
| --- | --- | --- |
| `http_requests` | **实测** | 从 `httpx.MockTransport` 真正看到的请求路径逐条计数 |
| `quota_units` | **实测** | 官方口径 `origins × destinations`，由真实客户端累加器给出 |
| `batch_sizes` | **实测** | 从批量请求的 `destinations` 参数切分计数 |
| 本地 CPU 耗时 | **实测** | 只反映本机解析与记账开销，**不是**线上耗时 |
| 限流排队行为（等待次数、单次等待、总耗时） | **实测** | 本地 0.5 QPS 桶 + `MockTransport`，零出网 |
| 线上墙钟耗时（38.4 s / 0.8 s） | **推算** | `max(请求数 × RTT, 请求数 / 桶速率)`；**RTT 120 ms 与 2.5 QPS 都是显式假设参数** |
| 「推算提速 48.0×」 | **推算** | 由上式导出，不是实测 |
| `live` 模式端到端耗时 | **实测**（2026-10-10） | 三样例 live 首跑 14.6 ~ 16.1 s（`docs/TEST_REPORT.md` §4.3）；**单次运行值**，受当时网络与配额排队影响，别当常量 |

**复现性说明**（本轮实测）：重跑该脚本后，**结构性数字逐字节可复现**——
`http_requests`（96 / 2）、`quota_units`（96 / 96）、`batch_sizes`（`[50, 46]`）、
`http_requests_ratio`（48.0）、`modeled_*`（38.4 / 0.8）全部一致。
**三项时间测量逐次不同**，这不是缺陷而是计时本身的抖动：

| 字段 | 留档值 | 重跑值 |
| --- | ---: | ---: |
| `serial.elapsed_local_s` | 0.0246 | 0.029 |
| `matrix.elapsed_local_s` | 0.0015 | 0.0018 |
| `rate_limit.elapsed_s` | 38.069 | 38.203 |

所以引用这些数字时要带「约」或明确说「本次运行」，不要当成常量。

---

## 12. 未验证项与已知局限

以下项目**没有**被验证过，不应被读作「已通过」。这份清单与 `docs/TEST_REPORT.md` §6 同源，
此处只补与本文设计决策相关的部分。

| 项 | 原因 | 对本文哪一节有影响 |
| --- | --- | --- |
| 🔴 **FR-205 的进度只到后端** | 端点与真实阶段回调都在（`background: true` → `202` + `task_id`，`GET /api/analysis/{task_id}` 回真实 `progress`，`test_analysis_api.py` 断言过），但**前端一次都没调用**：`grep -n 'task_id' web/*.js` 实测 **0 命中**（三个 js 文件各自 `grep -c` 都是 0），体检走同步 `POST /api/analysis`，状态行由 `web/report.js:1038` 的 `setInterval` 每秒走字 | §3 的调用策略里「同步为一跳、异步要轮询」这条取舍（`api/analysis.py:19` 写明：切异步会给零配置演示多担一个轮询失败点）。文案本身不假造百分比，但 PRD FR-205 的「前端流式或轮询更新」**没有满足**，别按「进度已完成」读 |
| ~~**`live` 模式端到端**~~ | ✅ **2026-10-10 已补验**（`docs/TEST_REPORT.md` §4.3） | §11 的推算模型已被一次真实实跑落在 PRD 目标内（14.6 ~ 16.1 s ≤ 25 s）；但 §2 `blocked` 判据与 §3 限流**仍只在 `MockTransport` 上验**——条款 3.2.3(ii) 禁压测，这一点不因「有了 AK」而改变 |
| **`docker compose up`** | 开发机没有 Docker，CI 用的是裸 `docker run` | `docker build` **已由 CI 真实执行并跑绿**（2026-10-07：镜像不含 `.db`、不含明文 AK、零 env 起容器 → `HEALTHCHECK` 转绿 → 三个接口可服务）；**compose 的起停与 bind mount 写权限仍未验** |
| **真实测时 vs 插值推算的偏差统计** | live 每样只跑过一次，**样本量 1**，凑不出偏差分布 | §2.5 的精度取舍仍只能给机制说明。2026-10-10 补到**两条逐顶点真实对照**（`measured` 差 +6.7 %、`interpolated` 差 +32 %，见 `docs/TEST_REPORT.md` §5 第 4 条）：**两个样本不是分布**，且它顺带证明 `RouteMatrix` 与 `Direction` 对同一 O-D 本身就差 ≈7 % |
| ~~**JSAPI 4.0 实底图上的顶点点击**~~ | ✅ **2026-10-10 已补验**（真实浏览器 + 真实 AK + 真实指针事件） | §4 的「SVG 舞台 / JSAPI 实底图双渲染」两条路径**现在都真跑过**：实时底图侧 23 个 `BMap_Marker`、点顶点出网 1 次即画出真实路网轨迹（48/45 折点），话术随取数层从「合成示意线」变「真实路网轨迹」。仍未验的是**12 次硬上限与连点竞态在实时底图路径上**（同一份 `loadPolyline()`，SVG 侧已实测；再跑要烧 10 次真实 `direction`）。逐条证据见 `docs/shots/README-isochrone-acceptance.md` |
| 🔴 **新发现，未修**：live 等时圈的响应体 `usage` 少记一个批次 | 同一次 `POST /api/isochrone`：`body.usage` 报 `quota_units=40 / http_requests=6`，同 `request_id` 的服务端日志是 `44 / 7` | §9 的三栏计量口径在**这条路径上偏乐观**（真实花费 ≥ 报告值）。§2 的 `ProbeTally` 与客户端累加器为何差一个批次**尚未定位**；mock 层两账本同源，所以零出网冒烟抓不到 |
| **移动端真机行为**（R15） | 只有 Chrome DevTools 模拟，无 iOS / Android 实机 | 手指惯性拖图、双指缩放、iOS Safari 滚动穿透三项未验证；官方也未公开 JSAPI 4.0 的精确移动兼容矩阵 |
| ~~🔴 **FR-106 底部抽屉**~~ | ✅ **2026-10-11 已实现并补验**：`body[data-sheet-mode="mobile"]` 由 JS 按 `matchMedia("(max-width: 768px)")` 写，三块面板原封不动地变成 `position: fixed` + `translateY()` 的抽屉，一次开一块，把手条可点可拖、Escape 收起 | §1.4 的前端形态现在**两档都有实测**：375×667 地图 `278 → 615`（高 337 px）、414×896 `278 → 844`（高 566 px），`scrollWidth / clientWidth` 分别是 375/375 与 414/414（无横向滚动），逐元素热区 ≥ 44 px。**这一节的历史要连着读**：README §10.3 一度把「抽屉可展开收起」写成已验收（不实），10-11 先更正为「未实现」、同日才真落地；数字与两轮缺陷见 `docs/TEST_REPORT.md` §2.12 与 `docs/shots/README-isochrone-acceptance.md` 的「FR-106 移动端底部抽屉」一节。**仍未验**的是真机那三项（上一行）——DevTools 里拖得动不等于手指拖得动，双指缩放更是至今没实现 |
| ~~**FR-107 只能靠视口宽度自动降档，没法手动指定**~~ | ✅ **2026-10-11 已补**：`?profile=m` / `?profile=d` 覆盖 `matchMedia` 探测，报告头新增档位徽标按**响应** `meta.lightweight` / `meta.k` 写文案 | 桌面 1280×800 加 `?profile=m` → 请求体 `platform:"mobile"`、徽标「简化模式（FR-107 · 实测前 K=6）」；375×667 加 `?profile=d` → `platform:"desktop"`、徽标「完整模式 · 实测前 K=12」，抽屉照常。`?profile=x`（非法值）**静默回落到视口探测**，与后端 `_platform()` 对非法值直接 400 是两套口径——URL 是用户手输的，把 `?profile=tablet` 做成一屏错误比回落更不吓人。逐条记录见 `docs/shots/README-isochrone-acceptance.md` 的「FR-107 档位显式化」一节 |
| ~~**FR-103 面板上只有坐标、没有地址**~~ | ✅ **2026-10-11 已接**：`setCenter()` 每次换点 GET 一次 `/api/regeo`，`#center-address` 铺 `formatted_address` + 行政区 / 街道 / 门牌 / adcode 四枚徽标与取数层 | 🔴 两条口径要一起读：① **字段一律取自响应，绝不切 `formatted_address`**（§4.7 末尾的禁令，`tests/test_geo_api.py::test_address_parts_never_come_from_splitting_formatted_address` 用一份「文本里像有区、`addressComponent` 里没给」的报文专门反证）；② 空态与失败态**都有可读文案**且不打断主流程，所以状态用 `data-state` 而不是 `hidden` 表达——`.address-line { display: flex }` 的作者规则权重高于 UA 的 `[hidden] { display: none }`，靠 `hidden` 是藏不掉的（FR-106 那轮踩过的同一类坑）。**未验**：真实 `live` 的一跳（要烧 300/日 逆地理配额里的 1 次，等用户点头）；浏览器里的成功态是 `window.fetch` 打桩量的。四例证据与 `request_id` 见 `docs/shots/README-isochrone-acceptance.md` 的「FR-103 中心点地址回显」一节 |
| ~~**FR-108 地址输入框没有联想**~~ | ✅ **2026-10-11 已实现**：`#suggest-on` 开关（默认关）+ `#suggest-list` 候选列表走 `GET /api/suggest`，选中即 `setCenter(..., "fly")` 落点 | 三条设计口径：① **「默认关闭」是前后端各把一半**——后端 `SUGGEST_ENABLED=false` 只保证不出网，前端开关没勾时**连请求都不发**（否则 Network 里全是 400/200，用户会把「未启用」当 bug 报）；② 门槛常量 `SUGGEST_MIN_KEYWORD_LEN` 两边逐字对齐、前端按**码点**（`Array.from`）数而不是 UTF-16 单元，否则 emoji 会让「前端放行、后端 400」；前端缓存键 `${region}|${keyword}`——换城市等于换答案；③ 候选只渲染**有坐标且在国内**的条目（后端丢一轮、前端 `pair()` 再挡一轮形状漂移），因为「点下去没反应的一行候选」在移动端会被当成页面卡死。🔴 顺带修掉一条**静默配置缺陷**：`suggest_enabled()` 原来兜底读 `os.environ`，而 pydantic-settings 的 `env_file` **只填字段、不写环境变量**，所以 `.env` 里那行 `SUGGEST_ENABLED=true` 从来没生效过（`docs/TEST_REPORT.md` §2.13）。**未验**：真实 `live` 一跳（烧与地点检索**共用**的 100 次/日池子，等用户点头）、`flyTo` 真机（本机无浏览器 AK，只跑 `panTo` 退路）、**`/place/v2/suggestion` 的真实响应字段名**（笔记 §2.4 只记了请求参数，`_parse_suggest` 的字段是按同类接口推的）。七例证据见 `docs/shots/README-isochrone-acceptance.md` 的「FR-108 地址联想」一节 |
| ~~**样例社区坐标的真实性**~~ | ✅ **2026-10-10 已补验**（`gen_samples.py --verify` 走 `/api/suggest` + 逆地理交叉核对行政区） | 三份清单标 `coord_verified: true`，含义是「**工具核验过且行政区对得上**」，不是人工实地踏勘；`/api/samples` 原样透出该标记。🔴 教训：**geocode 只能给到城市级坐标**，「非空就算核验」会把三个错坐标一起标成已核验（`docs/TEST_REPORT.md` §2.8） |
| **`live` 模式下 `market` 的召回** | 检索词 `市场$超市$便利店` 被「市场监督管理局」（`政府机构;行政单位`）等同名机构挤占，`tag` 过滤实测反而把结果清空 | FR-221 的盲区主判定依赖 `market` ∩ `pharmacy` ∩ `primary_school`，`market` 恒为 0 → **`live` 盲区面积系统性偏大**（交道口 62.29 %、肖家河 86.89 %）。本轮口径是**接受并明示**：后端对**真实检索那一侧**（`data_source = live` **与 `cache`**）的报告在 `notes` 里挂提示，`mock` 口径不受影响。`cache` 必须一起算——缓存里存的就是同一份真实返回（`docs/TEST_REPORT.md` §2.9）。试验数据与三条约束见 `docs/TEST_REPORT.md` §4.4 |
| **浏览器内真实渲染** | 本环境的无头 Chrome 无输出 | §10.2 / §10.3 的图表与 DOM 结论来自 Node + 最小 DOM 打桩真跑的两条渲染路径（`tools/report_dom_smoke.mjs` 15 项 + `tools/map_layers_smoke.mjs` 12 项断言，`grep -c 'OK '` 实测），它**不是**真实浏览器 |
| **PNG 报告导出** | 功能未实现 | §6.4：条款 6.6 的「截图必须保留版权与审图号」目前无对应风险面，但补该功能时必须一并实现该约束。🔴 设计口径已预先收口为「矢量报告卡 + 标识文字带」而不是「拍实时底图」——我方图表确实是矢量（`report.js:130` `svgNode()`），但 live 瓦片画在 JSAPI 自己的跨域 `<canvas>` 里，`toDataURL()` 按规范应抛 `SecurityError`，**这一半只有代码依据、没有实测**（验它要烧一次 JSAPI 浏览器端配额） |
| ~~**FR-413 页脚缺合规声明与审图号**~~ | ✅ **2026-10-11 已补齐**：`.foot-links`（含官方隐私政策与服务条款外链）+ `.foot-legal`（缓存性质与 TTL、合成示例声明、导出边界）+ `.foot-review`（审图号与测绘资质，带读取日期） | 三个落点、两种失效方式、两套守卫，逐条见 §6.5。🔴 **仍有一项做不到自动**：审图号是官方 HTML 页脚里的字符串，没有机器可读真值，自动化只能验「这一行在不在」——号码过期属**季度人工复核**（README §10.7）。同轮顺带修掉两条页脚相关缺陷：一键体检路径的 `request_id` 一直没交到页脚（`docs/TEST_REPORT.md` §2.15）、正文借用 `--muted` 导致对比度只剩 0.01 余量（§2.16） |
| **官方未公开的数值** | 需登录控制台核实 | `localhost` 能否作为 Referer 白名单、地点检索 `page_size` 真实上限是 10 还是 20、逆地理 `pois` 返回条数上限、JSAPI 4.0 下热力图插件可用性——`§7.4` 已按保守值设计（10 且不翻页） |

### 12.1 一处已修的内部注释不一致（留档）

`web/report.js` 的模块 docstring 曾写「两边只通过 `window.CircleDocMap` 的 **8 个方法**打交道」，
`web/isochrone.js` 末尾的接口注释写「刻意只暴露 **6 个方法**」，而该对象**当时**实际导出
**12 个方法 + 1 个 `constants`**（`adoptIsochrone` / `setOverlays` / `getCenter` / `setCenter` /
`getActiveMode` / `readControls` / `setBusy` / `setRunHint` / `focusOn` / `pushProgress` /
`clearProgress` / `isLiveMap`，外加 `constants`）。

这处不一致**不影响行为**（两边实际只用到其中几个），但两处注释互相矛盾且都与实际不符。

**已修**：两处都**删掉了数字**，改为指向 `Object.keys(window.CircleDocMap).length`。
理由是这个接口仍在增长（FR-212/FR-222 加了 `setOverlays`、FR-223 加了 `focusOn`），
写死个数的注释一定会再次过期 —— 而「注释说 6 个、实际 12 个」比不写更糟，
它会让人以为少挂了一个方法。

**这条判断随后就被验证了一次**：FR-106 又加了 `openSheet` 与 `isSheetMobile`，该对象现在
`Object.keys(...).length` 实测 **15**（14 个方法 + 1 个 `constants`）。如果当初只是把 6 改成 12，
这一轮就得再改一次——而现在代码里**没有任何数字要同步**，本节上面那句「当时 12 个」
是历史陈述、不参与对账。

⚠️ 本节刻意**不写行号**：校对过程中 `report.js` 的那句注释在本次会话内就因 docstring 被删掉一行
而移动了位置，硬编码行号会立刻过期。要定位按引号里的原文搜索即可。

---

## 附：本文引用的文件清单

| 类别 | 路径 |
| --- | --- |
| 需求 | `PRD.md` |
| 事实核验 | `docs/BAIDU_API_NOTES.md`（§2.8 / §3 / §6.2 / §7.3 为本文第 3~6 章的引用对象） |
| HTTP 层 | `api/app.py` `api/health.py` `api/geo.py` `api/isochrone.py` `api/poi.py` `api/analysis.py` `api/cache_stats.py` |
| 等时圈 | `services/isochrone_geometry.py` `services/route_probe.py` `services/isochrone_service.py` |
| POI 与盲区 | `services/poi_categories.py` `services/poi_search.py` `services/coverage.py` `services/blind_spots.py` |
| 评分与编排 | `services/score.py` `services/analysis.py` `services/samples.py` |
| 基础设施 | `infra/baidu_client.py` `infra/ratelimit.py` `infra/breaker.py` `infra/cache_store.py` `infra/cache_keys.py` `infra/errors.py` `infra/config.py` `infra/metrics.py` `infra/log.py` |
| 前端 | `web/index.html` `web/app.js` `web/isochrone.html` `web/isochrone.js` `web/report.js` `web/report.css` |
| 配置与数据 | `config/samples.yaml` `config/weights.yaml` `data/samples/*.json` |
| 工具 | `tools/bench_route_strategy.py` `tools/gen_samples.py` `tools/report_dom_smoke.mjs` |
| 验证记录 | `docs/TEST_REPORT.md` `docs/bench_route_strategy.json` `docs/shots/README-isochrone-acceptance.md` |
