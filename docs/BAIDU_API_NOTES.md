# 百度地图开放平台能力核验笔记（CircleDoc）

| 项 | 内容 |
| --- | --- |
| 文档目的 | 逐条核验 `PRD.md` 中关于百度地图开放平台的假设，把"猜测值"替换为"官方确认值"，并给出 PRD 修改建议 |
| 核验方法 | 实际打开官方页面（`lbsyun.baidu.com` 文档站 / `lbs.baidu.com` FAQ 与权益页 / 服务条款页）逐字核对 |
| 全部来源查阅日期 | **2026-10-04**（本文所有 URL 均在当日访问并读取正文；下文不再逐条重复该日期）。**例外**：§7.1 的隐私政策条目与 §7.2 的审图号是 **2026-10-11** 为 FR-413 页脚单独复核的，行内已标日期 |
| 编写说明 | 独立会话产出；**未修改 `PRD.md` 及任何既有文件** |
| 红线声明 | **凡官方文档未公开的数值，本文一律写「官方文档未公开该数值」；凡需登录控制台才能确认的，写「需登录控制台核实」并列出应点开的页面。不编造任何配额数字、endpoint 路径或参数名。** |

**置信度标记说明**

- `✅官方确认` —— 官方页面正文有明确表述，本文逐字转述
- `⚠️官方矛盾` —— 官方两处表述互相冲突，本文同时列出，不择一采信
- `🔶需控制台核实` —— 官方未公开或需登录自己的 AK 才能确认
- `❌未公开` —— 官方文档未公开该数值

---

## 1. 结论摘要

### 1.1 Q1 服务端 Web 服务 API 逐个核验

| # | PRD 中的假设 / 待核验点 | 官方实际值 | 置信度 | 要改 PRD 吗 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- | --- |
| Q1-1 | 地理编码可用 | `GET https://api.map.baidu.com/geocoding/v3/`，V3.0（V2.0 及以前自 2019-06-18 起对新注册用户不可用）。必选 `address`(≤128 字节)、`ak`；返回 `status/count/result.location{lng,lat}/precise/confidence/comprehension/level` | ✅官方确认 | 否 | [webservice-geocoding-base](https://lbsyun.baidu.com/docs/webapi?title=geocoding/guide/webservice-geocoding-base) |
| Q1-2 | 逆地理编码能回显地址 / 行政区 / 街道（FR-103） | `GET https://api.map.baidu.com/reverse_geocoding/v3/`。`addressComponent{province,city,district,town,town_code,street,street_number,adcode,cityCode}` 齐全，**FR-103 成立** | ✅官方确认 | 否 | [webservice-geocoding-abroad-base](https://lbsyun.baidu.com/docs/webapi?title=reverse_geocoding/guide/webservice-geocoding-abroad-base) |
| Q1-3 | **`extensions_poi` 是否可用** | 可用。`extensions_poi=0` 不召回 / `1` 返回 `pois` 并返回 `sematic_description`，**默认值 0**；官方注「默认显示周边 1000 米内的 poi」 | ✅官方确认 | 是（§4.7 接口表未提此参数） | 同上 |
| Q1-4 | **逆地理能返回几条 POI** | 由 `radius`（poi 召回半径，**允许区间 0–3000 m，超过按 3000 召回，默认 1000**）与 `poi_types`（多类型用 `\|` 分隔）控制；`entire_poi=1` 可召回更多 POI。**返回条数上限：官方文档未公开该数值** | ⚠️部分未公开（见 §9-①） | 记为不确定项 | 同上 |
| Q1-5 | **能否借此省掉一次独立 POI 检索** | **不能。** FAQ 2561 官方原文：「逆地理编码是用来定位的服务，poi 只是辅助定位的；**检索 POI 推荐使用地点检索服务**」。且半径上限 3000 m、`tag` 粒度远不如地点检索 | ✅官方确认 | 是（若 PRD 有此意图必须删掉） | [FAQ 2561](https://lbsyun.baidu.com/faq/details?id=2294&title=2561) |
| Q1-6 | §4.3「圆形区域检索」叫什么、endpoint 是什么 | 官方接口名 **「圆形区域检索」**，V2.0：`GET https://api.map.baidu.com/place/v2/search?query=银行&location=39.915,116.404&radius=2000` | ✅官方确认 | 否 | [placev3 概览 / interfaceDocumentV2](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV2) |
| Q1-7 | 关键字检索 / 多边形检索叫什么 | 同一 endpoint `/place/v2/search`，三种形态：**行政区划区域检索**（`region=北京`）、**圆形区域检索**（`location`+`radius`）、**多边形区域检索**（`bounds=lat1,lng1,lat2,lng2`）。行政区划形态 `query` **不支持多关键字**；圆形形态 `query` **支持多关键字并集，以 `$` 分隔、最多 10 个** | ✅官方确认 | 是（§4.3 多 query 合并方式需按此改写） | 同上 |
| Q1-8 | **多边形区域检索能否用** | **官方原文：「多边形区域检索为高级权限，购买商用授权后可申请开通」** → 个人 / 免费场景**不可用** | ✅官方确认 | 是（反向印证 §4.4 本地射线法判定的必要性） | 同上 |
| Q1-9 | **`page_size` 上限** | 3.0 接口文档：默认 10、取值 **10–20**、最大 **20**。⚠️ 矛盾：3.0 返回字段注释又写「name: poi 名称，**单次请求最多返回 10 条结果**」；FAQ 2542 写「**最多只支持召回 10 条数据，不支持更多**」 | ⚠️官方矛盾（见 §9-②） | 按 **10** 规划，20 需实测 | [interfaceDocumentV3](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV3) · [FAQ 2542](https://lbsyun.baidu.com/faq/details?id=2294&title=2542) |
| Q1-10 | **总结果数上限** | **单次请求最多 150 条**（3.0 的 `total` 上限）。FAQ 2534 / 2543：出于数据安全保护，一次请求最多返回 150 条 | ✅官方确认 | 是（§4.3 翻页预算要按 150 封顶） | [interfaceDocumentV3](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV3) · [FAQ 2534](https://lbsyun.baidu.com/faq/details?id=2294&title=2534) |
| Q1-11 | **能否按距离排序** | 能。`center` + `filter=sort_name:distance` | ✅官方确认 | 否 | [interfaceDocumentV3](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV3) |
| Q1-12 | **`distance` 是直线还是路网距离** | **直线距离**（FAQ 2546）。圆形区域检索时 `detail_info.distance` 返回「距中心点的直线距离」 | ✅官方确认 | 否（FR-211 / FR-213 的「直线 vs 实测」双列口径成立） | [FAQ 2546](https://lbsyun.baidu.com/faq/details?id=2294&title=2546) |
| Q1-13 | **圆形检索半径上限** | 默认 1000 m，**最大 10 公里**，无最小半径限制（FAQ 2548）。⚠️ 官方警告：**半径过大超过中心点所在城市边界时，会退化为「城市范围检索」**；需 `radius_limit=true` 才严格限域（FAQ 2544） | ✅官方确认 | 是（§4.3 的 `R_max×1.2`：步行 1.5 km、骑行 4.8 km 均安全，但须显式声明 `radius_limit`） | [FAQ 2548](https://lbsyun.baidu.com/faq/details?id=2294&title=2548) · [FAQ 2544](https://lbsyun.baidu.com/faq/details?id=2294&title=2544) |
| Q1-14 | POI 去重键 | `uid` 字段确实存在于 2.0 / 3.0 返回体及逆地理 `pois[]` → **FR-214 成立** | ✅官方确认 | 否 | [interfaceDocumentV2](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV2) |
| Q1-15 | **tag 分类体系怎么用（§4.3 query 词表）** | 以官方 POI 分类表（一级 / 二级）为准。**官方分类里没有「养老院」「菜市场」「农贸市场」「社区服务中心」「快递驿站」「银行网点」「理发店」**；应改用：市场、药店、疗养院、小学、幼儿园、公交车站、地铁站、公园、休闲广场、维修点、银行、住宅区、居民委员会、福利机构、小吃快餐店、综合医院、诊所、超市、便利店、购物中心 | ✅官方确认 | **是（直接命中 R5，§4.3 词表必须重写，见 §8.5）** | [POI 分类表（页面标注最近更新 2024-04-18）](https://lbs.baidu.com/index.php?title=open/poitags) · [POI 展示分类表.xlsx（对应 `classified_poi_tag`）](https://mapopen-website-wiki.cdn.bcebos.com/static/doc/POI%E5%B1%95%E7%A4%BA%E5%88%86%E7%B1%BB%E8%A1%A8.xlsx) |
| Q1-16 | 步行路线规划是否返回 `duration` | **返回**。`result.routes[].distance`(米) + `duration`(**秒**)，`steps[]` 含 `direction/distance/duration/instructions/name/path` | ✅官方确认 | 否 | [directionv2 walking](https://lbsyun.baidu.com/docs/webapi?title=directionv2/webservice-direction/walking) |
| Q1-17 | 是否返回路径 polyline 坐标串 | **返回**。`steps[].path` = `116.339966,40.011156;116.340006,40.010546;…`（`经度,纬度`，分号分隔） | ✅官方确认 | 否 | 同上 |
| Q1-18 | 步行最大距离 / 途经点数 | **步行接口参数表中无途经点字段**（全部参数：`origin/destination/origin_uid/destination_uid/coord_type/ret_coordtype/output/sn/timestamp/callback`）。**步行最大距离限制：官方文档未公开该数值。** FAQ 2524 所述「支持途经点」指**驾车**（驾车 v2 最多 18 个内存途经点；轻量版驾车 10 个） | ❌未公开 + ✅无途经点 | 是（§4.2 不得假设途经点） | 同上 · [FAQ 2524](https://lbsyun.baidu.com/faq/details?id=2294&title=2524) |
| Q1-19 | 骑行路线规划 | 存在：`GET https://api.map.baidu.com/direction/v2/riding`，可选 `riding_type`(0 普通自行车 / 1 电动自行车)、`road_prefer`(0 默认 / 3 无逆行且无阶梯)；返回含 `restrictions_status`(0x01 禁行 / 0x02 逆行) 与 `restrictions_info` | ✅官方确认 | 否（FR-104 骑行可做） | [directionv2 riding](https://lbsyun.baidu.com/docs/webapi?title=directionv2/webservice-direction/riding) |
| Q1-20 | **距离矩阵到底存不存在** | **存在**，官方名称 **「批量算路 / RouteMatrix API V2.0」** | ✅官方确认 | **是（R1 可直接解除，见 §8.6）** | [routematrix/routchtout](https://lbsyun.baidu.com/docs/webapi?title=routematrix/routchtout) |
| Q1-21 | **是否对个人开发者开放** | **开放**：权益表「批量算路」个人开发者栏为 5,000/日 · 3 QPS。⚠️ prepare 页原文要求「**该服务需要使用服务端 AK 调用**」 | ✅官方确认 | 否 | [routematrix/prepare](https://lbsyun.baidu.com/docs/webapi?title=routematrix/prepare) · [开发者权益](https://lbs.baidu.com/solutions/privilege) |
| Q1-22 | **支持哪些出行方式** | **驾车、摩托车、骑行（自行车 / 电动车）、步行** 四类（摩托车栏对个人为「-」） | ✅官方确认 | 是（§4.2 可改用步行矩阵） | [routchtout](https://lbsyun.baidu.com/docs/webapi?title=routematrix/routchtout) · [routchtout-walk](https://lbsyun.baidu.com/docs/webapi?title=routematrix/routchtout-walk) |
| Q1-23 | **单次 origins / destinations 数量上限** | 官方原文：**「步行、骑行批量算路一次最多计算 50 条路线，起终点个数之积不能超过 50」**；**「驾车批量算路一次最多计算 100 条路线，起终点个数之积不能超过 100」**；**「步行时任意起终点之间的距离不得超过 200KM，超过此限制会返回参数错误」** | ✅官方确认 | 是 | [routchtout](https://lbsyun.baidu.com/docs/webapi?title=routematrix/routchtout) |
| Q1-24 | 批量算路是否返回路径坐标 | **不返回 polyline**，只返回 `distance{text,value}` / `duration{text,value}` → 要渲染真实路径仍须用 direction | ✅官方确认 | 是（§4.2 需说明两者分工） | 同上 |
| Q1-25 | **配额怎么计（最易踩坑）** | 官方原文：**「批量算路服务的配额和并发是按最终路线数来计算，而非 RouteMatrix API 请求数。如一次请求 2 个起点 5 个终点，则最终路线输出为 2\*5=10 条，配额计为 10 次。」** | ✅官方确认 | **是（§4.6 调用计量必须按路线数折算）** | 同上 |

### 1.2 Q2 配额与 QPS

| # | 问题 | 官方实际值 | 置信度 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- |
| Q2-1 | **个人认证各接口日配额与 QPS** | 见 §3.1 逐行表。关键值：**地理编码 5,000/日 · 3 QPS**；**逆地理编码 300/日 · 3 QPS**；**地点检索 100/日 · 3 QPS**；**路线规划 5,000/日 · 3 QPS**；**轻量版驾车 / 步行 / 骑行 / 公交各 5,000/日 · 3 QPS**；**批量算路 5,000/日 · 3 QPS**；坐标转换 5,000/日 · 3 QPS；静态图 5,000/日 · 3 QPS；**多维检索 10/日 · 1 QPS**；**深度检索 10/日 · 1 QPS** | ✅官方确认 | [开发者权益（页面自注「更新时间：2025 年 10 月」）](https://lbs.baidu.com/solutions/privilege) |
| Q2-2 | 企业开发者（试用版） | 地理编码 / 逆地理 / 路线规划 100,000/日 · 30 QPS；**地点检索 2,000/日 · 30 QPS**；批量算路 100,000/日 · 30 QPS；多维检索 1,000/日 · 1 QPS；深度检索 20/日 · 1 QPS | ✅官方确认 | 同上 |
| Q2-3 | 企业开发者（授权版） | 地理 / 逆地理 / 坐标转换 3,000,000/日 · 1,000 QPS；路线规划 / 轻量规划 / 批量算路 300,000/日 · 200 QPS；地点检索 50,000/日 · 200 QPS | ✅官方确认 | 同上 |
| Q2-4 | **未认证个人的配额** | **官方权益表只列「个人开发者 / 企业开发者（试用版）/ 企业开发者（授权版）」三列，未给出「未认证」档任何数值 → 官方文档未公开该数值。** FAQ 2614 原文：**「认证不通过无法使用百度地图开放平台产品」** | ❌未公开 + ✅须认证 | [权益页](https://lbs.baidu.com/solutions/privilege) · [FAQ 2614](https://lbsyun.baidu.com/faq/details?id=2294&title=2614) |
| Q2-5 | 个人认证门槛与流程 | 见 §4.3。资料仅需个人身份证；FAQ 2617：个人认证适用「学生、研究所或个人兴趣学习研究」，权益为「初级个人配额」；FAQ 2606：个人认证与对公打款**即时返回结果**，企业组织认证 3 个工作日 | ✅官方确认 | [FAQ 2640](https://lbsyun.baidu.com/faq/details?id=2294&title=2640) · [FAQ 2617](https://lbsyun.baidu.com/faq/details?id=2294&title=2617) · [FAQ 2606](https://lbsyun.baidu.com/faq/details?id=2294&title=2606) |
| Q2-6 | **超限错误码（用于重试 / 熔断判定）** | 完整表见 §3.2。核心：**`status=4 Quota Failure`「服务当日调用次数已超限」**；**`status=302 天配额超限，限制访问`**；**`status=401 当前并发量已经超过约定并发配额，限制访问`** | ✅官方确认 | [Web 服务 API 状态码附录](https://lbsyun.baidu.com/faq/api?title=webapi/appendix) |
| Q2-7 | **有没有 HTTP 429** | **官方状态码表中不存在 429。** 百度以 HTTP 200 + body `status` 表达配额 / 并发限制（4 / 302 / 401）。FAQ 2361 原文：「某秒钟超出并发后，在这一秒服务不会受影响，会在接下来的相应时间出现部分调用失败」 | ✅官方确认 | [状态码附录](https://lbsyun.baidu.com/faq/api?title=webapi/appendix) · [FAQ 2361](https://lbsyun.baidu.com/faq/details?id=2294&title=2361) |
| Q2-8 | 同一账号多 AK 是否配额独立 | **不独立。FAQ 2594：同账号下 AK 之间的配额是共用的** → 双 AK 方案会互相抢配额 | ✅官方确认 | [FAQ 2594](https://lbsyun.baidu.com/faq/details?id=2294&title=2594) |
| Q2-9 | 配额何时恢复 | FAQ 2366：配额 / 并发**次日零时归零、不可累计**；FAQ 2828：超免费额度先扣已购流量包，耗尽后返回配额超限错误，**不会自动扣费** | ✅官方确认 | [FAQ 2366](https://lbsyun.baidu.com/faq/details?id=2294&title=2366) · [FAQ 2828](https://lbsyun.baidu.com/faq/details?id=2294&title=2828) |

### 1.3 Q3 AK 类型与鉴权

| # | 问题 | 官方实际值 | 置信度 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- |
| Q3-1 | **两类 AK 是否必须分开申请** | **必须。** FAQ 2639 原文：「**JS API 只支持浏览器类型的 AK**，移动端可创建 Android / IOS SDK 类型 AK，**Web 端可创建服务端类型 AK**」。FAQ 2772：JS API 任何版本都需浏览器端 AK；FAQ 2541 同义 | ✅官方确认 | [FAQ 2639](https://lbsyun.baidu.com/faq/details?id=2298&title=2639) · [FAQ 2772](https://lbsyun.baidu.com/faq/details?id=2298&title=2772) · [FAQ 2541](https://lbsyun.baidu.com/faq/details?id=2294&title=2541) |
| Q3-2 | 混用会怎样 | 报 `status=203 APP 类型错误`（Server 类型 1、Mobile 2 → 新版区分 Mobile_Android 21 / Mobile_IPhone 22、Browser 类型 3）。FAQ 2787：「APP Referer 校验失败」三类原因＝AK 类型不对 / 域名不在 referer 白名单 / 第三方组件内置他人 AK。FAQ 2547：地点输入提示报 `240` → 需用服务端 AK | ✅官方确认 | [状态码附录](https://lbsyun.baidu.com/faq/api?title=webapi/appendix) · [FAQ 2787](https://lbsyun.baidu.com/faq/details?id=2298&title=2787) |
| Q3-3 | 浏览器端 AK 鉴权方式 | **Referer 域名白名单**（可填 `*` 表示不限制） | ✅官方确认 | [FAQ 2590](https://lbsyun.baidu.com/faq/details?id=2298&title=2590) · [FAQ 2584](https://lbsyun.baidu.com/faq/details?id=2298&title=2584) |
| Q3-4 | 服务端 AK 鉴权方式 | **IP 白名单**（支持 IPV4；不限制填 `0.0.0.0/0`）或 **SN 校验**；失败分别报 `status=210 APP IP 校验失败` / `211 APP SN 校验失败` | ✅官方确认 | [FAQ 2590](https://lbsyun.baidu.com/faq/details?id=2298&title=2590) · [FAQ 2581](https://lbsyun.baidu.com/faq/details?id=2294&title=2581) · [状态码附录](https://lbsyun.baidu.com/faq/api?title=webapi/appendix) |
| Q3-5 | **`localhost` / `127.0.0.1` 能否作为白名单域名用于本地演示** | **官方从未把 `localhost` 或 `127.0.0.1` 作为 Referer 白名单的特例明文列出。** 官方对本地场景的**正面原文指引是填 `*`**：FAQ 2781「**本地可以设置 `*`，前端代码容易泄露 AK，被其他网站非法调用，上线前可以用作 Debug，线上正式 ak 请设置合理的 Referer 白名单**」；JSAPI 4.0 开发前准备「**Referer 白名单：开发阶段填写 `*`（上线后请改为实际域名）**」 | ✅官方确认（结论表述应为「本地填 `*`」，而非「填 localhost」） | [FAQ 2781](https://lbsyun.baidu.com/faq/details?id=2298&title=2781) · [JSAPI4 开发前准备（2026-07-17）](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/quickstart/prepare) |
| Q3-6 | 域名白名单写法 | FAQ 2590：支持 `*.mysite.com`、`*myapp.com`、`myapp.com` 三种格式；多个域名用**英文半角逗号**隔开；「如果不想对任何域名做限制，设置为英文半角星号 `*`」；支持 IPV4 | ✅官方确认 | [FAQ 2590](https://lbsyun.baidu.com/faq/details?id=2298&title=2590) · [FAQ 2783](https://lbsyun.baidu.com/faq/details?id=2298&title=2783) |
| Q3-7 | 一个账号能建几个 AK | FAQ 2589：**企业认证可创建 40 个 AK，个人认证 10 个** | ✅官方确认 | [FAQ 2589](https://lbsyun.baidu.com/faq/details?id=2298&title=2589) |
| Q3-8 | 服务端 AK 是否默认开通各服务 | FAQ 2523：创建服务端 AK **默认支持路线规划服务**。但 `status=240 APP 服务被禁用` 说明存在未勾选情况（尤其轻量版路线规划、批量算路、全球逆地理编码） | ✅官方确认 + 🔶需控制台核实 | [FAQ 2523](https://lbsyun.baidu.com/faq/details?id=2294&title=2523) · [FAQ 2585](https://lbsyun.baidu.com/faq/details?id=2294&title=2585) |

### 1.4 Q4 JSAPI 版本与能力

| # | 问题 | 官方实际值 | 置信度 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- |
| Q4-1 | **官方推荐版本** | **JSAPI 4.0**。原文「JSAPI 最新版本为 4.0，新版 API 兼容旧版」。**JSAPI GL 与 JSAPI 3.0 文档均已标注「历史版本文档，仅用于维护现有项目，不建议用于新项目开发。新项目请使用 JSAPI 4.0」** | ✅官方确认 | [jsapi4/index（2026-08-03）](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/index) · [jsapi4/upgrade（2026-08-18）](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/upgrade) |
| Q4-2 | Polygon / Marker 支持 | 支持。4.0 新特性原文：「**Marker、Label、InfoWindow、Polyline、Polygon、Circle 等覆盖物组件样式全面升级**」；4.0 文档目录含点标记 / 点标记样式 / 文本标注 / 信息窗口 / 折线 / 贝塞尔曲线 / **多边形** / 圆 / 矩形 | ✅官方确认 | [jsapi4/index](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/index) |
| Q4-3 | 自定义覆盖物 | 4.0 提供 **`BMap.WebGLCustomLayer`**（WebGL 自定义图层，2026-09-17 更新）；`BMap.CustomOverlay` / `CustomHtmlLayer` 见于 GL / 3.0 文档；4.0 目录未单列「自定义覆盖物」条目，但 4.0 的 gcj02 页仍把「自定义覆盖物」列为受 `coordType` 影响的模块 | ✅官方确认（4.0 目录单列情况 ⚠️不完整） | [jsapi4/guide/layer/custom](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/layer/custom) · [jsapi4/guide/map/gcj02](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/map/gcj02) |
| Q4-4 | **热力图是否还维护 / 官方推荐做法** | **JSAPI 4.0 文档目录中无热力图条目。** 官方可查三条线索：① 4.0 升级指南「如果你之前用到了 BMapLib 的插件，我们对 BMapLib 也进行了兼容」，热力库旧地址 `.../library/Heatmap/2.0/src/Heatmap_min.js` → 新地址 `./BMap-JavaScript-library/Heatmap/Heatmap.min.js` 或官方 CDN `https://jsapi-demo.bj.bcebos.com/BMap-JavaScript-library/Heatmap/Heatmap.min.js`；② JSAPI 3.0 开源库页（已标为历史版本）「热力图…注：支持 chrome, safari, IE9 及以上的浏览器。核心的代码主要来自于第三方 heatmap.js，**主入口类是 HeatmapOverlay**」，另有大数据可视化库 **Mapv**；③ GL 升级指南「**(5) 添加热力图图层更改：GL 需要调用 MapVGL 的方式实现热力图的加载**」 | ✅官方确认（**无「4.0 内置热力图」证据**） | [jsapi4/upgrade](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/upgrade) · [jspopular3.0/openlibrary（历史版本）](https://lbs.baidu.com/index.php?title=jspopular3.0/openlibrary) |
| Q4-5 | 移动端浏览器兼容性 | GL 使用须知原文：「基于 WebGL 开发，对浏览器环境有兼容性要求，需要完整支持 WebGL 的现代浏览器；WebGL 支持欠佳的浏览器会**降级为 Canvas 绘制**，若仍存在兼容性问题则**降级到瓦片图渲染**」；GL 简介「支持 PC 端和移动端基于浏览器的地图应用开发」；4.0 快速开始要求 `<meta name="viewport" content="width=device-width, initial-scale=1.0">`，推荐 Chrome / Firefox / Edge / Safari。**4.0 精确浏览器版本兼容矩阵：官方文档未公开** | ✅官方确认 + ❌矩阵未公开 | [jspopularGL/guide/usage](https://lbsyun.baidu.com/docs/jsapi?title=jspopularGL/guide/usage) · [jsapi4 firstdemo](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/quickstart/firstdemo) |
| Q4-6 | **浏览器定位在移动端是否可用（PRD D7 的官方依据）** | **官方原文印证 D7**：4.0 定位页「**由于 Chrome、iOS10 以上系统等已不再支持非安全域的浏览器定位请求，为保证定位成功率和精度，请尽快升级您的站点到 HTTPS。iOS15 系统浏览器默认关闭位置请求**」 | ✅官方确认 | [jsapi4/guide/lbs/locate](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/lbs/locate) |
| Q4-7 | JSAPI 侧是否也有配额 | **有。** 权益表：JS API 地点检索 个人 **100/日 · 3 QPS**；JS API 地理编码 / 逆地理编码 / 路线规划类 / 坐标转换 / 定位 各 5,000/日 · 3 QPS。FAQ 2775：地图展示、添加点 / 覆盖物**无配额限制**；地点检索、正逆地理编码、定位**有配额**。FAQ 2597：Android / iOS SDK 及 **JavaScript 基础服务不限制日配额** | ✅官方确认 | [权益页](https://lbs.baidu.com/solutions/privilege) · [FAQ 2775](https://lbsyun.baidu.com/faq/details?id=2298&title=2775) · [FAQ 2597](https://lbsyun.baidu.com/faq/details?id=2294&title=2597) |
| Q4-8 | 加载方式与命名空间 | 同步 `<script src="https://api.map.baidu.com/api?v=4.0&ak=您的密钥"></script>`；异步加 `&callback=initMap`；npm `@baidumap/jsapi-loader` → `BMapLoader.load({ak, version:'4.0'})`，resolve 出 `window.BMap` 或 `window.BMapGL`；4.0 兼容 `BMap`(2.0/3.0) 与 `BMapGL` 命名空间；3.0→4.0 只需 `?v=3.0` 改 `?v=4.0`；GL→4.0 删 `type=webgl` 并把 `v=1.0` 改 `v=4.0`；可用 `BMapGL.apiVersion='gl'` / `BMapGL.uiVersion='gl'` 回退（须在创建地图前、运行期不可改）；TS 类型包 `@baidumap/jsapi-v4-types` | ✅官方确认 | [jsapi4/upgrade](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/upgrade) · [concepts/types](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concepts/types) · [prepare](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/quickstart/prepare) |
| Q4-9 | 保护服务端 AK 的官方机制 | 4.0 **代理模式**：`load()` 的 `serviceHost` **末尾需带 `/`**，设置后 URL 不再携带 ak，由服务端反向代理到 `https://api.map.baidu.com` 并注入 ak | ✅官方确认 | [jsapi4/guide/concepts/proxy](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concepts/proxy) |

### 1.5 Q5 坐标系

| # | 问题 | 官方实际值 | 置信度 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- |
| Q5-1 | 四者关系 | 4.0 坐标系概念页原文：「百度地图 API 使用百度坐标系（**BD-09**），**所有接口的默认输入输出坐标均基于此坐标系**。**BD-09 坐标系在 GCJ-02 坐标系基础上进行二次加密**」；WGS84 = GPS 大地坐标系；GCJ02 = 火星坐标系 / 国测局坐标，由 WGS84 加密；「**非中国地区地图统一使用 WGS-84 坐标**」。FAQ 2580：中国地区（含港澳台）所有产品支持返回 GCJ02 与 BD09，海外返回 WGS84；FAQ 2579：未设置坐标参数时默认 BD09 | ✅官方确认 | [jsapi4/guide/concept/coord（2026-07-02）](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concept/coord) · [FAQ 2580](https://lbsyun.baidu.com/faq/details?id=2294&title=2580) · [FAQ 2579](https://lbsyun.baidu.com/faq/details?id=2294&title=2579) |
| Q5-2 | **BD09 与 BD09LL 的区别** | 官方在 JSAPI 转换映射表中把「5 = BD09（经纬度）」与「6 = BD09（墨卡托米制）」分列；Web 服务参数值域用 `bd09ll`（经纬度）/ `bd09mc`（墨卡托）。**BD09LL = BD-09 经纬度形式，BD09MC = 墨卡托米制形式** | ✅官方确认（ll / mc 二分） | 同上 · [Web 服务坐标转换（2026-03-19）](https://lbsyun.baidu.com/docs/webapi?title=geoconv/guide/changeposition) |
| Q5-3 | **指定输入坐标系的具体参数名（命名不统一）** | 逐接口见 §6.2。⚠️ 三种写法并存：逆地理编码 v3 = **`coordtype`（无下划线，取字符串）**；地点检索 v2 / v3 = **`coord_type`（有下划线，取数字 1/2/3/4）**；direction / directionlite / routematrix = **`coord_type`（有下划线，取字符串）** | ✅官方确认 | 见 §6.2 各行 |
| Q5-4 | 指定输出坐标系 | 统一为 **`ret_coordtype`**（地理编码 / 逆地理 / 地点检索 / 路线规划均有）。地点检索取值 `gcj02ll`；direction v2 支持 `bd09ll` / `gcj02` | ✅官方确认 | 见 §6.2 |
| Q5-5 | JSAPI 侧 | 全局 `BMap.coordType = BMAP_COORD_GCJ02`（须在创建地图前设置、运行期不可改，影响折线 / 多边形 / 圆形 / 自定义覆盖物 / 检索 / 地理编码 / 定位 / 路径规划）；单功能可在 options 传 `coordType`；运行时转换 `new BMap.Convertor().translate(points, 1, 5, cb)`，映射 **1=WGS84, 2=WGS84(米制), 3=GCJ02, 4=GCJ02(米制), 5=BD09, 6=BD09(米制)** | ✅官方确认 | [concept/coord](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concept/coord) · [map/gcj02（2026-07-20）](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/map/gcj02) |
| Q5-6 | 坐标转换 API | 官方：「单次请求可批量解析 **100 个**坐标」；配额 个人 5,000/日 · 3 QPS。**该接口确切 endpoint 路径本次未从官方页面取到 → 见 §9-④，禁止凭记忆写入代码** | ✅批量上限 + ❌endpoint 未取到 | [geoconv/guide/changeposition](https://lbsyun.baidu.com/docs/webapi?title=geoconv/guide/changeposition) |
| Q5-7 | 旧「坐标系说明书」页 | `index.php?title=coordinate` 本次打开**无正文（空页）**，不可作为依据 | ✅确认已失效 | 实测（2026-10-04） |

**结论**：PRD R8 的「全链路统一 BD09LL」方向正确且必要，但**必须在文档中显式写出各接口参数名的三种不统一写法**，否则代码里会漏传或拼错而表现为点位偏移。

### 1.6 Q6 服务条款红线（详见 §7）

| # | 问题 | 官方实际值（逐字原文见 §7.2） | 置信度 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- |
| Q6-1 | **缓存 API 返回结果的态度** | 3.3.2(ii) 明文禁止「对本服务运行过程中释放出的任何地图数据或其他数据进行包括但不限于删除、修改、**爬取、截图、存储、缓存、下载或镜像**等操作」；3.3.3 进一步禁止未经审批「**预读取、缓存、下载、复制、存储**或修改任何服务相关内容」 | ✅官方确认（禁止性表述存在） | [开放平台服务条款（更新时间 2026-08-05）](https://lbsyun.baidu.com/index.php?title=open/law) |
| Q6-2 | **把 POI 数据存进自己的库并随 Docker 镜像分发** | 3.3.4 禁止「复制、翻译、修改或创造相关内容的**衍生品（包括生成或用于数据库）**，或**公开展示相关内容的全部或任何一部分**」；6.3「本服务所涉及的运营数据的全部权利，均归属百度所有，未经百度事先书面同意，您不得为任何目的使用」；2.3 商业目的需商用授权 | ✅官方确认（高风险） | 同上 |
| Q6-3 | **批量抓取** | 2.2「不得直接存取、技术手段抓取、使用内部数据、图片、程序、模块或是任何其他百度地图的服务或功能」；3.3.2(ii) 禁止使用「漫游器、抓取工具、插件、外挂、或非经授权的第三方工具」非法获取存储数据；3.2.3 禁止「同一产品或应用通过注册多个个人或企业账号获取更高服务配额」及「对本服务进行压力测试并对百度线上服务稳定性造成影响」 | ✅官方确认（禁止） | 同上 |
| Q6-4 | **FR-402（SQLite 缓存 POI 3 天）判定** | **🟠 有风险 —— 可用但必须限缩形态**（详见 §7.3） | ✅判定完成 | 同上 |
| Q6-5 | **FR-412（内置示例数据集随镜像分发）判定** | **🔴 禁止当前形态 —— 必须改造为合成数据**（详见 §7.3） | ✅判定完成 | 同上 |

---

## 2. 接口清单核验

> 本节所有 endpoint、参数名、返回字段均逐字摘自官方页面正文（2026-10-04），未做任何补全或推断。**凡未列出的参数即官方文档未记载，不要假设其存在。**

### 2.1 地理编码（正向）

- **Endpoint**：`https://api.map.baidu.com/geocoding/v3/` · **请求方式**：GET · **版本**：V3.0
- **来源**：`https://lbsyun.baidu.com/docs/webapi?title=geocoding/guide/webservice-geocoding-base`（页面更新时间 2026-03-20）
- 页面原文：V2.0 及以前的接口自 **2019-06-18** 起对新注册用户不可用。

| 参数 | 必选 | 官方说明 |
| --- | --- | --- |
| `address` | 是 | 地址字符串，**最长 128 字节** |
| `ak` | 是 | 服务端 AK |
| `city` | 否 | 指定城市以提高解析精度 |
| `ret_coordtype` | 否 | `gcj02ll` / `bd09mc`；**默认 bd09ll** |
| `output` | 否 | `json` / `xml`；**默认 xml** ← 不显式传 `output=json` 会拿到 XML |
| `callback` | 否 | JSONP |
| `sn` | 否 | SN 校验签名 |
| `extension_analys_level` | 否 | 默认 0 |
| `extension_poi_infos` | 否 | 默认 false |

**返回字段**：`status`、`count`、`result.location{lng,lat}`、`result.precision`、`result.confidence`、`result.comprehension`、`result.level`、`result.code`、`result.city`。
**confidence 官方分档**：100 → <20 m；…；20 → <10000 m。

```
https://api.map.baidu.com/geocoding/v3/?address=%E5%8C%97%E4%BA%AC%E5%B8%82%E9%A1%BA%E4%B9%89%E5%8C%BA%E6%B8%A9%E6%B3%89%E8%B7%AF11%E5%8F%B7&output=json&ret_coordtype=bd09ll&ak=您的AK
```

**对 PRD 的意义**：FR-101 方式② 可用此接口；但 **FR-108 的"中文搜索联想"应改用「地点输入提示」（§2.4）**——地理编码只返回一个坐标，不返回候选列表。

---

### 2.2 逆地理编码（国内已并入「全球逆地理编码」）

- **Endpoint**：`https://api.map.baidu.com/reverse_geocoding/v3/` · GET
- **来源**：`https://lbsyun.baidu.com/docs/webapi?title=reverse_geocoding/guide/webservice-geocoding-abroad-base`（概览页 `.../webservice-geocoding-abroad`）

| 参数 | 必选 | 官方说明（逐字要点） |
| --- | --- | --- |
| `location` | 是 | **`纬度,经度`** |
| `ak` | 是 | 服务端 AK |
| `coordtype` | 否 | ⚠️ **参数名无下划线**。`bd09ll`（默认）/ `bd09mc` / `gcj02ll`（仅限中国）/ `wgs84ll` |
| `ret_coordtype` | 否 | `bd09ll`（默认）/ `gcj02ll` / `bd09mc` |
| **`extensions_poi`** | 否 | **0 = 不召回 POI / 1 = 返回 `pois` 并返回 `sematic_description`；默认值 0**。官方注：默认显示周边 1000 米内的 poi |
| `radius` | 否 | **poi 召回半径，允许区间 0–3000 米，超过按 3000 米召回，默认 1000** |
| `entire_poi` | 否 | `1` = 召回更多 POI 并优化 `formatted_address_poi` |
| `poi_types` | 否 | 需 `extensions_poi=1`；多类型用 `\|` 分隔 |
| `sort_strategy` | 否 | `distance` / `rank` / `default` |
| `extensions_road` | 否 | `true` 召回最近 3 条道路 |
| `region_data_source` | 否 | 1 = 统计局 / 2 = 民政部（**默认 2**） |
| `output` | 否 | json / xml |

**返回字段**：`formatted_address`、`formatted_address_poi`、`sematic_description`、`addressComponent{province, city, district, town, town_code, village, street, street_number, adcode, cityCode, edz}`、`pois[]{addr, direction, distance, name, tag, point{x,y}, tel, uid, zip, popularity_level(1-9), parent_poi, aoi_name}`、`business`（最多 3 个）、`business_info[]`、`roads[]`、`poiRegions[]`。
**注意**：`village`（村 / 社区级）需商用授权；境外服务与五级行政区划需购买商用授权或提工单开通。FAQ 2567：可用 `extensions_town` / `extensions_road=true` / `extensions_poi=1` 提升地址精细度。

**对 FR-103 的判定**：地址、行政区、街道三级均可直接回显，**FR-103 成立**。

**能否省掉一次独立 POI 检索？→ 不能。** FAQ 2561 官方原文：「逆地理编码是用来定位的服务，poi 只是辅助定位的；**检索 POI 推荐使用地点检索服务**」。<br>叠加三条硬约束：`radius` 上限 3000 m、`pois` 条数上限官方未公开、`tag` 粒度粗（形如 `美食;中餐厅`）→ 不足以支撑 §4.3 的六维分类统计。

```
https://api.map.baidu.com/reverse_geocoding/v3/?ak=您的AK&output=json
  &coordtype=bd09ll&location=39.95,116.51
  &extensions_poi=1&entire_poi=1&sort_strategy=distance&radius=3000&poi_types=药店|小学|市场
```

---

### 2.3 地点检索（POI 搜索）—— 三种形态的官方名称

- **来源**：概览 `https://lbsyun.baidu.com/docs/webapi?title=placev3/guide/webservice-placeapiV3`；3.0 接口文档 `.../placev3/interfaceDocumentV3`；2.0 接口文档 `.../placev3/interfaceDocumentV2`；另有「多维检索」「深度检索」两类高级服务 · GET

| PRD 里的说法 | 官方正式名称 | Endpoint | 关键参数 | 免费可用性 |
| --- | --- | --- | --- | --- |
| 「圆形区域检索」（§4.3 用的就是这个） | **圆形区域检索** | `https://api.map.baidu.com/place/v2/search` | `query` + `location=纬度,经度` + `radius` | ✅ 可用 |
| 按城市关键字搜 | **行政区划区域检索** | 2.0 同 `/place/v2/search`；3.0 为 `https://api.map.baidu.com/place/v3/region` | 2.0：`query` + `region=北京`（+ `tag`）；3.0：`query` + `type` + `region` | ✅ 可用 |
| 按多边形框搜 | **多边形区域检索** | `https://api.map.baidu.com/place/v2/search` | `query` + `bounds=lat1,lng1,lat2,lng2`（首尾坐标对需相同；矩形可传左下、右上两顶点） | ❌ **「多边形区域检索为高级权限，购买商用授权后可申请开通」** |

**圆形区域检索示例（§4.3 主链路）**
```
https://api.map.baidu.com/place/v2/search?query=药店$诊所$综合医院&region=北京
  &location=39.915,116.404&radius=1500&output=json
  &coord_type=3&ret_coordtype=bd09ll&radius_limit=true
  &page_size=10&page_num=1&ak=您的AK
```

**多边形区域检索示例（仅记录，本项目不可用）**
```
http://api.map.baidu.com/place/v2/search?query=银行&region=北京&output=json
  &bounds=39.915,116.404,39.975,116.414&ak=您的AK
```

**关键参数与限制（2.0 圆形区域检索）**

| 项 | 官方说明 |
| --- | --- |
| `query` | 圆形形态**支持多关键字并集，以 `$` 分隔，最多支持 10 个关键字**；行政区划形态**不支持多关键字** |
| `location` | **不支持多个点** |
| `radius` | 默认 1000 米；FAQ 2548：**最大 10 公里**，无最小半径；官方警告：**半径过大超过中心点所在城市边界时会变为城市范围检索**；FAQ 2544：需 `radius_limit=true` / `city_limit` 才严格限域 |
| `page_size` | ⚠️ **矛盾**：3.0 文档「默认 10、取值 10–20、最大 20」 vs 3.0 返回字段注释「单次请求最多返回 10 条结果」 vs FAQ 2542「最多只支持召回 10 条数据，不支持更多」 |
| `total` | **单次请求最多 150**（FAQ 2534 / 2543：出于数据安全保护，一次请求最多返回 150 条） |
| 距离排序 | `center` + `filter=sort_name:distance` |
| `coord_type` | ⚠️ **取数字**：`1`=wgs84ll / `2`=gcj02ll / `3`=bd09ll（默认）/ `4`=bd09mc |
| `ret_coordtype` | `gcj02ll` |
| `uid` | ✅ 存在，**FR-214 去重键成立** |
| `detail_info.distance` | 圆形区域检索时返回**距中心点的直线距离**（FAQ 2546） |
| 需商用授权 + 工单 | `photos`、`status`（营业状态）、`language` 等；`d7_hot_value` |
| 数据过滤 | FAQ 2536：检索接口会**过滤掉部分数据**（数据保护）→ 计数偏低是官方预期行为，报告需注明 |
| 其它 2.0 可选参数 | `tag`（`,` 分隔）、`city_limit`、`extensions_adcode`、`scope`(1/2)、`center`、`filter`、`page_num`、`sn`、`timestamp`、`photo_show`、`address_result` |

**返回字段（2.0 `results[]`）**：`name`、`location{lng,lat}`、`address`、`province`、`city`、`area`、`adcode`、`telephone`、`uid`、`street_id`、`detail`、`detail_info{classified_poi_tag, distance, type, tag, label, navi_location, children[], price, shop_hours, overall_rating, brand, content_tag, …}`。

**3.0 行政区划检索**：`https://api.map.baidu.com/place/v3/region?query=&type=&region=&ak=`，可选 `is_light_version`、`address_result`、`center`、`filter`、`page_size`(10–20)、`page_num`。

---

### 2.4 地点输入提示（FR-108 应使用此接口）

- **Endpoint**：`https://api.map.baidu.com/place/v2/suggestion` · GET · 必选 `query`（支持拼音）、`region`、`ak`
- **示例**：`https://api.map.baidu.com/place/v2/suggestion?query=天安门&region=北京&city_limit=true&output=json&ak=您的AK`
- ⚠️ **配额陷阱**：FAQ 2540 / 2549 —— **地点输入提示与地点检索共用服务配额**；个人认证下这 **100 次/日**由两者瓜分。
- ⚠️ **AK 类型**：FAQ 2547 —— 报 `240` 说明用错 AK 类型，**必须用服务端 AK**。
- FAQ 2551：`query` 不支持传入经纬度坐标。
- ✅ **返回体字段名已于 2026-10-11 用真实服务端 AK 实测核对**（此前只核验过入参，返回体是防御式猜测）。
  实调 `query=和平里&region=北京`，经本项目的 `/api/suggest`（`request_id: d043dd017b0f43e0`、
  `x-http-requests: 1`、`x-place-calls: 1`、`x-elapsed-ms: 369`）返回 **10 条候选、`dropped: 0`**：
  `result[]` 每项**确实**含 `name` 与 `location{lng, lat}`，并且 `district` / `city` / `province` / `adcode`
  四个字段全部有值（例：`和平里北街-地铁站` → `[116.42505, 39.964752]`、`东城区` / `北京市` / `110101`）。
  判据是「字段名猜错就不可能有条目活下来」：`services/geo._parse_suggest` 只认这几个键，取不到坐标的条目
  一律丢弃，10 条全通过说明键名对上了。**§9.2-⑨ 的「示例代码核对」不再是这件事的前置条件**。

**对 PRD 的影响**：FR-108 的 300 ms 防抖联想**会直接吃掉 §4.3 的检索配额**，必须加最少输入长度门槛 + 本地缓存 + 可选开关（见 §8.3）。

---

### 2.5 步行路线规划（Direction V2）

- **Endpoint**：`https://api.map.baidu.com/direction/v2/walking` · GET
- **来源**：`https://lbsyun.baidu.com/docs/webapi?title=directionv2/webservice-direction/walking`（更新时间 2026-03-19）

| 参数 | 必选 | 说明 |
| --- | --- | --- |
| `origin` / `destination` | 是 | **`纬度,经度`**（⚠️ 与多数接口的经纬度顺序相反），小数点后最多 6 位 |
| `ak` | 是 | 服务端 AK |
| `origin_uid` / `destination_uid` | 否 | 官方建议填写以提升准确性 |
| `coord_type` | 否 | ⚠️ **有下划线、取字符串**：`bd09ll`（默认）/ `bd09mc` / `gcj02` / `wgs84` |
| `ret_coordtype` | 否 | `bd09ll` / `gcj02` |
| `output` / `sn` / `timestamp` / `callback` | 否 | — |

**⚠️ 参数表中无途经点字段** → PRD 不可假设步行支持途经点。FAQ 2524 所述「支持途经点」指**驾车**（驾车 v2 最多 18 个内存途经点；轻量版驾车 10 个）。

**返回**：`status`、`message`、`type`、`info.copyright`、`result.routes[]{distance(米), duration(秒), steps[]{direction, distance, duration, instructions, name, path, turn_type_id, stepOriginLocation, stepDestinationLocation}, links[]{length, attr(0x01 禁行)}}`。

- **`duration` → ✅ 返回，单位秒**
- **`path` → ✅ 返回**，格式 `116.339966,40.011156;116.340006,40.010546;…`（`经度,纬度`，分号分隔）
- **步行最大距离限制 → ❌ 官方文档未公开该数值**（见 §9-③）
- 官方示例：`distance=16748 m` / `duration=15491 s` → 推算步行速度 ≈ **3.9 km/h**（15 min ≈ **0.98 km**，**低于 PRD §4.2 假设的 1.25 km**，见 §8.4）
- ⚠️ 文档瑕疵①：步行接口页状态码表照抄了骑行页的「2001 无骑行路线」。
- ⚠️ 文档瑕疵②：概览页 `.../directionv2/direction-api-v2` 正文只写「支持公交、骑行、驾车路线规划」，与导航栏含「步行」不一致 → **以步行接口页为准**。

```
https://api.map.baidu.com/direction/v2/walking?origin=40.01116,116.339303
  &destination=39.936404,116.452562&output=json&coord_type=bd09ll&ak=您的AK
```

---

### 2.6 骑行路线规划

- **Endpoint**：`https://api.map.baidu.com/direction/v2/riding` · GET
- 额外参数：`riding_type`（0 普通自行车 / 1 电动自行车）、`road_prefer`（0 默认 / 3 无逆行且无阶梯）
- 额外返回：`restrictions_status`（**0x01 禁行 / 0x02 逆行**）、`restrictions_info`
- **对 PRD 的意义**：FR-104 的骑行模式官方支持；`restrictions_status` 与 `links[].attr=0x01` 可作为 §4.2 `blocked` 标记的**官方硬证据**

---

### 2.7 轻量级路线规划 DirectionLite V1.0

- **Endpoint**：`https://api.map.baidu.com/directionlite/v1/walking`（另有 `/riding`、`/driving`、`/transit`）· GET
- **来源**：`https://lbsyun.baidu.com/docs/webapi?title=directionlite/guide/webservice-lwrouteplanapi/walk`（更新时间 2026-03-19）
- 可选 `steps_info`：**1 = 下发 step 详情 / 0 = 不下发**（可显著减小响应体，适合只做测时的采样请求）
- 返回：`status`（0 / 1 / 2 / **7 = 无返回结果**）、`result.routes[]{distance, duration, steps[]{direction(0–11，每档 30°), distance, duration, instruction, start_location, end_location, path}}`
- 权益表：轻量版驾车 / 步行 / 骑行 / 公交 **各 5,000/日 · 3 QPS（个人）**，与标准路线规划同档
- ⚠️ **权限陷阱**：官方原文「轻量级路线规划服务 2018 年 11 月 14 日上线，**在此之前注册的 AK 默认无使用权限**」→ **必须在控制台为 AK 勾选「步行路线规划（轻量）」**
- FAQ 2529：轻量版含驾车 / 骑行 / 步行 / 公交，但**不具备**驾车多路线、未来出行、公交跨城

**对 §4.2 的价值**：`steps_info=0` 的轻量请求只取 `duration`，字节最小，适合扇形采样的纯测时用途。

---

### 2.8 距离矩阵 —— **存在，官方名「批量算路 / RouteMatrix API V2.0」**

- **来源**：概览 `https://lbsyun.baidu.com/docs/webapi?title=routematrix/routchtout`；步行 `.../routematrix/routchtout-walk`；准备 `.../routematrix/prepare`（更新时间 2026-03-19）· GET
- **Endpoint（步行）**：`https://api.map.baidu.com/routematrix/v2/walking`

```
https://api.map.baidu.com/routematrix/v2/walking?output=json
  &origins=40.45,116.34|40.54,116.35
  &destinations=40.34,116.45|40.35,116.46
  &coord_type=bd09ll&ak=您的AK
```

| 项 | 官方说明 |
| --- | --- |
| `origins` / `destinations` | **`\|` 分隔**，格式 `纬度,经度`；步行 / 骑行 / 摩托支持 `纬度,经度;POI的uid` |
| 支持的出行方式 | **驾车、摩托车、骑行（自行车 / 电动车）、步行** |
| 单次路线数上限 | **步行、骑行：一次最多 50 条路线，起终点个数之积不能超过 50**；**驾车：一次最多 100 条路线，起终点个数之积不能超过 100** |
| 步行起终点距离上限 | **不得超过 200 KM，超过返回参数错误** |
| `ret_straight_dist` | 0 = 路线距离（默认）/ 1 = 直线距离 |
| `tactics` | 仅驾车 / 摩托 |
| `riding_type` / `road_prefer` | 骑行相关 |
| `coord_type` | ⚠️ **有下划线、取字符串**：`bd09ll`（默认）/ `bd09mc` / `gcj02` / `wgs84` |
| 返回 | `result[]`，顺序为 origin1-dest1, origin1-dest2, origin2-dest1…；每项 `{distance:{text:"19.3公里",value:19336}, duration:{text:"4.6小时",value:16569}}`；无结果时为 0；`message="成功"` |
| **是否返回 polyline** | **❌ 不返回路径坐标串** → 要渲染真实路径必须用 direction（§2.5） |
| AK 类型 | prepare 页原文：「**该服务需要使用服务端 AK 调用**」 |
| **配额计量（最易踩坑）** | 官方原文：「**批量算路服务的配额和并发是按最终路线数来计算，而非 RouteMatrix API 请求数。如一次请求 2 个起点 5 个终点，则最终路线输出为 2\*5=10 条，配额计为 10 次。**」 |

**对 §4.2 的直接结论**：可以用 **1 个 origin × ≤50 个探测点** 的一次 HTTP 请求，替代 50 次串行 direction。
**HTTP 请求数**从 ≈96 降到 **2–3 次**（端到端耗时大幅缩短，对 §2.2 的「≤45 s」指标关键），但**配额消耗完全不变**（仍按路线条数计）。
若还需真实轨迹坐标（FR-202 目视验证、FR-203 点击查看路径），对最终入选的边界顶点补调 direction（建议改为用户点击时懒加载）。

---

## 3. 配额与限流

### 3.1 开发者权益总表（按认证等级）

**来源**：`https://lbs.baidu.com/solutions/privilege`（页面自注「更新时间：2025 年 10 月」）；由各服务 overview 页「配额及费用说明 → 开发者权益」及 FAQ 2588 指入。查阅 2026-10-04。
官方定义原文：「**配额：开发者每天可调用服务的总次数**」「**并发（QPS）：开发者每秒钟可调用服务的次数**」。

格式：`日配额(次/日) / 并发(次/秒)`。

| 服务 | 个人开发者 | 企业（试用版） | 企业（授权版） | CircleDoc 相关性 |
| --- | --- | --- | --- | --- |
| 地理编码 | **5,000 / 3** | 100,000 / 30 | 3,000,000 / 1,000 | FR-101 / 108 |
| **逆地理编码** | **300 / 3** | 100,000 / 30 | 3,000,000 / 1,000 | FR-103 ⚠️**仅 300/日** |
| **地点检索** | **100 / 3** | 2,000 / 30 | 50,000 / 200 | §4.3 核心 ⚠️**仅 100/日** |
| 多维检索 | **10 / 1** | 1,000 / 1 | 2,000 / 1 | 个人几乎不可用 |
| 深度检索 | **10 / 1** | 20 / 1 | 100 / 1 | 个人几乎不可用 |
| 路线规划（合计类目） | **5,000 / 3** | 100,000 / 30 | 300,000 / 200 | §4.2 核心 |
| 驾车路线规划（轻量） | 5,000 / 3 | 100,000 / 30 | 300,000 / 200 | — |
| **步行路线规划（轻量）** | **5,000 / 3** | 100,000 / 30 | 300,000 / 200 | §4.2 备选 |
| 骑行路线规划（轻量） | 5,000 / 3 | 100,000 / 30 | 300,000 / 200 | FR-104 |
| 公交路线规划（轻量） | 5,000 / 3 | 100,000 / 30 | 300,000 / 200 | — |
| **批量算路（RouteMatrix）** | **5,000 / 3** | 100,000 / 30 | 300,000 / 200 | §4.2 并发测时首选 ✅**对个人开放** |
| 坐标转换 | 5,000 / 3 | 100,000 / 30 | 3,000,000 / 1,000 | R8 |
| 普通 IP 定位 | 5,000 / 3 | 100,000 / 30 | 3,000,000 / 1,000 | D7（不使用） |
| 行政区划查询 | 5,000 / 3 | 100,000 / 30 | 3,000,000 / 1,000 | — |
| 静态图 | 5,000 / 3 | 100,000 / 30 | 6,000,000 / 1,000 | FR-305 导出备选 |
| 国内天气 / 海外天气 | 5,000 / 3 · 5,000 / 3 | — | — | — |
| 实时路况 | 2,000 / 3 | 5,000 / 20 | — | 明确不做 |
| 时区服务 | 300 / 3 | 1,000 / 30 | 1,000 / 100 | — |
| 鹰眼轨迹 | 5,000（查询 20 / 分析 20 / SDK 上传 20 / 图像 1 / 其他 40） | — | — | — |
| 轨迹纠偏 API / 轨迹重合率分析 API | 100 / 1 | — | — | — |
| 摩托车路线规划 / 摩托车批量算路 | **-** | 100 / 20 | — | 个人不可用 |
| 境外路线规划 / 境外地点检索 / 境外地理编码 | **-** | 1,000 / 20 | — | 个人不可用 |
| **境外逆地理编码** | **-** | 1,000 / 100 | — | 个人不可用 → **样例社区必须全在国内** |
| 地址解析聚合 | **-** | 100 / 20 | — | 个人不可用 |
| JS API 地点检索 | **100 / 3** | 2,000 / 30 | — | ⚠️ 前端直连会挤占同一心智池 |
| JS API 地理编码 / 逆地理编码 | 5,000 / 3 · 5,000 / 3 | — | — | — |
| JS API 驾车 / 步行 / 骑行 / 公交路线规划 | 5,000 / 3 | — | — | — |
| JS API 坐标转换 / 定位 | 5,000 / 3 · 5,000 / 3 | — | — | — |

**关键推论（均有上表支撑）**：

1. **个人认证全线 QPS 上限就是 3**（多维 / 深度检索为 1）。
2. **地点检索 100 次/日 + 逆地理编码 300 次/日** 是全项目最紧的两个瓶颈，远小于路线规划的 5,000/日。
3. §4.2 的 120 次路线预算在 5,000/日 下仅占 2.4 % → **R1「配额极低」的担忧对路线规划不成立**。
4. 同一份预算若落到地点检索：**6 类 × 多别名 query 会瞬间打满 100 次/日**（详见 §8.3）。

### 3.2 状态码表（写重试 / 熔断逻辑的唯一依据）

**来源**：`https://lbsyun.baidu.com/faq/api?title=webapi/appendix`（Web 服务 API 状态码附录）+ 各接口页「服务状态码」节。查阅 2026-10-04。

| `status` | 官方英文名 / 中文说明 | 处置建议 |
| --- | --- | --- |
| 0 | ok 正常 | — |
| 1 | 服务器内部错误 | ✅ 可重试 |
| 2 | Parameter Invalid 请求参数非法 | ❌ 不重试（修参数） |
| 3 | Verify Failure 权限校验失败 | ❌ 不重试（查 AK 服务勾选） |
| **4** | **Quota Failure 配额校验失败 ——「服务当日调用次数已超限，请前往 API 控制台提升（请优先进行）开发者认证」** | 🔴 **配额熔断**：当日不再重试 |
| 5 | AK Failure | ❌ 不重试 |
| 8 | Parse Proto Failure 数据解析失败 | ✅ 可重试 1 次 |
| **9** | **Permission Denied 高级权限校验失败**（如多边形区域检索、商用授权项） | ❌ 不重试，走降级 |
| 101 | AK 参数不存在 | ❌ 不重试 |
| 200 | APP 不存在 | ❌ 不重试 |
| 201 | APP 被用户自己禁用 | ❌ 不重试 |
| 202 | APP 被管理员删除 | ❌ 不重试 |
| **203** | **APP 类型错误** —— Server(类型 1)、Mobile(类型 2 → 新版区分 Mobile_Android 21 / Mobile_IPhone 22)、Browser(类型 3) | ❌ 不重试，**换 AK**（对应 R7） |
| **210** | **APP IP 校验失败**（SERVER 类型选 IP 校验时，请求 IP 不在白名单或不是 `0.0.0.0/0`） | ❌ 不重试，改白名单 |
| **211** | **APP SN 校验失败** | ❌ 不重试，修 sn 算法 |
| **240** | **APP 服务被禁用**（需控制台为该 AK 勾选对应服务；FAQ 2585 / 2788 同义） | ❌ 不重试，控制台勾选 |
| 250 / 251 / 252 | 用户不存在 / 被自己删除 / 被管理员删除 | ❌ 不重试 |
| 260 | 服务不存在 | ❌ 不重试 |
| 261 | 服务被禁用（已下线） | ❌ 不重试 |
| **302** | **天配额超限，限制访问** | 🔴 **配额熔断** |
| **401** | **当前并发量已经超过约定并发配额，限制访问** | 🟠 **并发退避**（本项目唯一真正对应"限流"的码） |
| 500 | FAQ 2555：返回值 500 → 需将调用 IP 加白名单 | ❌ 不重试 |
| 2001（direction 专有） | 无骑行路线 | 视作该方向不可达 → `blocked` |
| 7（directionlite 专有） | 无返回结果 | 视作该方向不可达 → `blocked` |

> **⚠️ 官方状态码表中不存在 HTTP 429。** PRD §2.2「限流导致的 429 错误 0 次」这条指标在百度的协议下无法直接度量，必须改写（见 §8.2）。

### 3.3 限流行为的官方补充说明

| 事实 | 来源（2026-10-04） |
| --- | --- |
| 配额超限时**当天禁止调用**该服务，购买后立即恢复 | [FAQ 2361](https://lbsyun.baidu.com/faq/details?id=2294&title=2361) |
| 「某秒钟超出并发后，在这一秒服务不会受影响，会在接下来的相应时间出现部分调用失败」→ **超 QPS 的表现是零星失败而非全部拒绝**，必须重试 | [FAQ 2361](https://lbsyun.baidu.com/faq/details?id=2294&title=2361) |
| 配额 / 并发**次日零时归零、不可累计** | [FAQ 2366](https://lbsyun.baidu.com/faq/details?id=2294&title=2366) |
| 超免费额度 → 先扣已购流量包 → 耗尽后返回配额超限错误，**不会自动扣费** | [FAQ 2828](https://lbsyun.baidu.com/faq/details?id=2294&title=2828) |
| **同账号下 AK 之间配额共用** | [FAQ 2594](https://lbsyun.baidu.com/faq/details?id=2294&title=2594) |
| 平台有权限制每日请求次数与并发量，达到限制后「**百度将暂时中断您的服务正常使用**」 | [服务条款 3.2.2](https://lbsyun.baidu.com/index.php?title=open/law) |
| 禁止「同一产品或应用通过注册多个个人或企业账号获取更高服务配额」「对本服务进行压力测试并对百度线上服务稳定性造成影响」 | [服务条款 3.2.3](https://lbsyun.baidu.com/index.php?title=open/law) |
| 企业认证本身**免费**，上传营业执照即可完成 | [FAQ 2824](https://lbsyun.baidu.com/faq/details?id=2294&title=2824) |
| 实际调用量：控制台「我的额度」→ 流量图；权益档位：`https://lbs.baidu.com/apiconsole/quota#/privilege` | [FAQ 2593](https://lbsyun.baidu.com/faq/details?id=2294&title=2593) · [FAQ 2595](https://lbsyun.baidu.com/faq/details?id=2294&title=2595) |

---

## 4. AK 申请与白名单配置步骤

> 目标：README（FR-407）可直接照抄本节，做到"点击级"可复现。来源查阅 2026-10-04。

### 4.1 建两个 AK（缺一不可）

FAQ 2639 官方原文：「**JS API 只支持浏览器类型的 AK**，移动端可创建 Android / IOS SDK 类型 AK，**Web 端可创建服务端类型 AK**」→ CircleDoc 必须建 2 个 AK，与 PRD §4.1「两个独立 AK」一致。

| 用途 | 应用类型 | 请求校验方式 | 本地演示设置 |
| --- | --- | --- | --- |
| 后端 FastAPI 调 Web 服务 API | **服务端** | **IP 白名单** | 填 `0.0.0.0/0`（官方在 210 错误说明中把 `0.0.0.0/0` 列为合法值） |
| 前端 BMap JSAPI | **浏览器端** | **Referer 白名单** | 填 `*`（FAQ 2781 + JSAPI 4.0 prepare 页双重印证） |

**点击级步骤（控制台）**

1. 打开 `https://lbsyun.baidu.com/` → 右上角「控制台」（需已注册并实名认证的开发者账号）
2. 左侧边栏 →【应用管理】→【我的应用】→【创建应用】
3. 填写：**应用名称**（如 `CircleDoc-server`）、**应用类型**（服务端 / 浏览器端）
4. **启用服务**：官方说明"默认全部勾选"——⚠️ 但**轻量版路线规划、批量算路、全球逆地理编码等需逐项确认**（FAQ 2523 仅保证"默认支持路线规划服务"；`status=240` 即未勾选的表现）
5. **请求校验方式**：
   - 服务端 AK → 选「IP 白名单」，本地 / 演示填 `0.0.0.0/0`；也可选「SN 校验」（需自行实现 sn 签名，失败报 `211`）
   - 浏览器端 AK → 填 **Referer 白名单**，开发阶段填 `*`
6. 【提交】→ 在应用列表**复制 AK**
7. 配置页直链：`https://lbs.baidu.com/apiconsole/key/create#/home`（FAQ 2584）
8. 修改白名单：【我的应用】→ 对应 AK 右侧【设置】→ 修改 Referer / IP 白名单

**域名白名单合法写法**（FAQ 2590）

- `*.mysite.com`
- `*myapp.com`
- `myapp.com`
- 多个域名用**英文半角逗号**隔开
- 「如果不想对任何域名做限制，设置为**英文半角星号 `*`**」
- FAQ 2590：**支持 IPV4**

### 4.2 `localhost` 问题的准确结论（PRD R6 需按此改写）

**官方从未把 `localhost` 或 `127.0.0.1` 作为 Referer 白名单的特例明文列出。** 官方对本地场景给出的**唯一正面指引是"填 `*`"**：

- FAQ 2781 原文：「**本地可以设置 `*`，前端代码容易泄露 AK，被其他网站非法调用，上线前可以用作 Debug，线上正式 ak 请设置合理的 Referer 白名单**」（`https://lbsyun.baidu.com/faq/details?id=2298&title=2781`）
- FAQ 2782：测试环境同理
- JSAPI 4.0 开发前准备原文：「**Referer 白名单：开发阶段填写 `*`（上线后请改为实际域名）**」（`https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/quickstart/prepare`，2026-07-17）

**因此 README 应写**：「浏览器端 AK 的 Referer 白名单填 `*`（官方推荐做法，FAQ 2781）」，**而不是**「把 `localhost` 加入白名单」——后者是我方推断，官方未确认其可行。

**失败表现**：Referer 不匹配时 JSAPI 地图空白 / 报「APP Referer 校验失败」。FAQ 2787 列出三类原因（AK 类型不对 / 域名不在 referer 白名单 / 第三方组件内置他人 AK）→ 这正是 R6 场景，前端必须捕获 JSAPI 加载失败并把这三条排查项打印出来（FR-404）。

**进阶（可选）**：若不希望 AK 出现在前端 URL，用 JSAPI 4.0 **代理模式** —— `BMapLoader.load({ ak, serviceHost: 'https://your-host/' })`，原文要求 `serviceHost` **末尾需带 `/`**，设置后 URL 不再携带 ak，由服务端反代到 `https://api.map.baidu.com` 并注入 ak。来源：`https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concepts/proxy`。

### 4.3 个人实名认证（必须做，否则不可用）

FAQ 2614 原文：「**认证不通过无法使用百度地图开放平台产品**」；FAQ 2611：依据《网络安全法》须认证。

**流程（FAQ 2640）**

1. 完成开发者注册
2. 进入「开始认证」→ 选择「**我不是企业员工**」（学生选「我是学生」分支，需填学校 / 专业 / 毕业日期）
3. 填写**身份证号**、行业、地址、用途、使用场景
4. **下载百度 APP，扫码完成人脸识别**
5. FAQ 2606：个人认证**即时返回结果**（企业组织认证 3 个工作日；对公打款亦即时）
6. FAQ 2617：个人认证适用于「学生、研究所或个人兴趣学习研究」，权益为「初级个人配额」，资料仅需个人身份证
7. 控制台入口：`https://lbs.baidu.com/apiconsole/auth`

**⚠️ 未认证个人的配额数值：官方文档未公开该数值**（权益页只有"个人开发者 / 企业试用 / 企业授权"三列）。

### 4.4 环境变量命名（补一条官方事实）

| 变量 | 说明 | 官方依据 |
| --- | --- | --- |
| `BAIDU_SERVER_AK` | 服务端类型 AK，仅后端读取 | FAQ 2639：Web 端可创建服务端类型 AK |
| `BAIDU_BROWSER_AK` | 浏览器端类型 AK，注入前端 | FAQ 2639：JS API 只支持浏览器类型 AK |

**⚠️ 新增 PRD 未记录的事实（R7 需补）**：FAQ 2594 明确「**同账号下 AK 之间的配额是共用的**」→ 两个 AK **不能**用来把配额翻倍；它只解决"鉴权类型分离"，不解决"配额不足"。

### 4.5 `/api/health` 应探测的项（对应 R1 的"逐个探测"）

依据 §3.2，启动自检应逐一发一次最小请求并断言 `status==0`，把失败映射为可读文案：

| 探测项 | 期望 | 失败码解读 |
| --- | --- | --- |
| 地理编码 | `status=0` | 240→未勾选服务；210→IP 不在白名单；203→AK 类型错 |
| 全球逆地理编码 | `status=0` | 页面原文：V3.0「鉴权失败」请到 控制台 → 查看应用 → 设置，确认是否已获得"全球逆地理编码服务"权限 |
| 地点检索 | `status=0` | 4 / 302→当日配额耗尽（仅 100 次/日） |
| 步行路线规划 v2 | `status=0` | FAQ 2523：服务端 AK 默认含路线规划 |
| 步行路线规划（轻量） | `status=0` | 官方：2018-11-14 前注册的 AK 默认无权限 |
| 批量算路（步行） | `status=0` | prepare 页：需服务端 AK |
| 多边形区域检索 | **预期失败** | `status=9 Permission Denied` 属正常（需商用授权）→ health 报告应标注"高级权限，本项目不使用" |

---

## 5. JSAPI 能力核验

### 5.1 版本选择

| 项 | 官方结论 | 来源（2026-10-04） |
| --- | --- | --- |
| 当前版本 | **JSAPI 4.0**。原文「JSAPI 最新版本为 4.0，新版 API 兼容旧版」 | [jsapi4/index](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/index)（2026-08-03） |
| 3.0 / GL 状态 | 均已标注「**历史版本文档，仅用于维护现有项目，不建议用于新项目开发。新项目请使用 JSAPI 4.0**」 | JSAPI 3.0 / GL 文档页首 |
| 升级到 4.0 的成本 | 3.0→4.0 只需把 `?v=3.0` 改 `?v=4.0`；GL→4.0 删 `type=webgl`、把 `v=1.0` 改 `v=4.0`；4.0 同时兼容 `BMap`(2.0/3.0) 与 `BMapGL` 命名空间 | [jsapi4/upgrade](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/upgrade)（2026-08-18） |
| 回退开关 | `BMapGL.apiVersion='gl'` / `BMapGL.uiVersion='gl'`，**必须在创建地图前设置，运行期不可改** | 同上 |

**给 CircleDoc 的建议**：用 `?v=4.0`。若 FR-204 热力图最终只能用 `HeatmapOverlay`（BMapLib），需实测 BMapLib 在 4.0 下的兼容性 —— 官方仅承诺"对 BMapLib 也进行了兼容"，未给出热力图专项说明（见 §5.3）。

### 5.2 覆盖物能力（PRD 需要项逐条核对）

| PRD 需求 | JSAPI 4.0 官方证据 | 判定 |
| --- | --- | --- |
| FR-201 / 202 等时圈多边形 | 4.0 文档含「多边形」章节；新特性原文列 `Polygon` | ✅ 支持 |
| FR-212 POI 分色标注 | 「点标记」「点标记样式」「文本标注」章节；新特性列 `Marker`、`Label` | ✅ 支持 |
| FR-203 边界点点击 tooltip | 「信息窗口」`InfoWindow` | ✅ 支持 |
| FR-222 灰色盲区区域 | 同 `Polygon`（填充 + 描边） | ✅ 支持 |
| 自定义覆盖物 | 4.0 有 `BMap.WebGLCustomLayer`（详见下）；`CustomOverlay` / `CustomHtmlLayer` 见于 GL / 3.0 文档 | ✅ 有官方机制 |
| 地图控件 / 地图类型 | `BMAP_NORMAL_MAP` / `BMAP_SATELLITE_MAP` / `BMAP_HYBRID_MAP`、`MapTypeControl` | ✅ 支持 |

**`BMap.WebGLCustomLayer`（4.0 自定义图层，2026-09-17）关键约定** —— `https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/layer/custom`

- 必填 `vertexShader` + `fragmentShader`；建议 `referCenter`
- `attributes` 支持 `type:'lnglat'` → **自动投影经纬度**（对等时圈 / 热力网格渲染极有用）
- 内置 uniform：`u_mvpMatrix` / `u_opacity` / `u_zoom` / `u_time` / `u_resolution` / `u_pixelRatio` / `u_unitsPerPixel` / `u_center` / `u_heading` / `u_tilt`
- 传 `indices` 走 `drawElements`
- 命令式 `onRender(ctx)`：`ctx.setAttribute` / `ctx.draw` / `ctx.getUnitsPerPixel` / `ctx.getTime`
- `renderState.blend` 可选 `normal` / `additive` / `screen`
- ⚠️ 原文警告：「**必须通过 renderState 声明，切勿直接调用 `gl.enable()`、`gl.blendFunc()`**」

**`coordType` 的影响面**（4.0 gcj02 页）：全局 `coordType` 会影响折线、多边形、圆形、自定义覆盖物、检索、地理编码、定位、路径规划 → **FR-202「缩放 4 级后顶点仍贴合路网」的前提是前后端坐标系一致**（呼应 R8）。

### 5.3 热力图（FR-204）—— **4.0 未把它列为内置能力**

**事实链（全部官方，2026-10-04）**：

1. **JSAPI 4.0 文档目录中无热力图条目。**
2. 4.0 升级指南：「如果你之前用到了 BMapLib 的插件，我们对 BMapLib 也进行了兼容」；热力库地址从旧版 `https://api.map.baidu.com/library/Heatmap/2.0/src/Heatmap_min.js` 改为 `./BMap-JavaScript-library/Heatmap/Heatmap.min.js` 或官方 CDN `https://jsapi-demo.bj.bcebos.com/BMap-JavaScript-library/Heatmap/Heatmap.min.js`。
3. JSAPI 3.0 开源库页（**已标注为历史版本**）原文：「热力图…注：支持 chrome, safari, IE9 及以上的浏览器。核心的代码主要来自于第三方 heatmap.js，**主入口类是 HeatmapOverlay**」；同页另列大数据可视化库 **Mapv**（直接打点、热力图、网格、聚合）。
4. GL 升级指南：「**(5) 添加热力图图层更改：GL 需要调用 MapVGL 的方式实现热力图的加载**」（点聚合改为 `new mapvgl.ClusterLayer()`）。

**结论（不要写成"官方推荐 HeatmapOverlay"）**：

- `HeatmapOverlay` 属 **BMapLib / JSAPI 3.0 时代**的库；官方对 4.0 **只承诺"BMapLib 兼容 + 新 CDN 地址"**，**没有**把热力图列为 4.0 内置能力，**也没有**在 4.0 文档中给出热力图用法示例。
- FR-204 有三条可选实现路径，均需实测，且**必须本地 vendored**（呼应 R12：不引用公网 CDN）：
  1. `HeatmapOverlay`（BMapLib）+ 4.0 兼容模式 → 需实测
  2. `MapVGL` → 官方在 GL 路线上明确的方式
  3. **`BMap.WebGLCustomLayer` 自绘 IDW 网格热力层** → 4.0 原生、无外部库、可精确控制，且数据源本来就是 §4.5 Step5 我方计算的耗时场，**不涉及第三方数据缓存合规问题**
- **推荐路径 3**。PRD FR-204 应改写为"自绘热力层（或经实测验证的 BMapLib 热力层）"，**不得假设 JSAPI 4.0 自带热力图**。

### 5.4 移动端与渲染降级

| 事实 | 来源（2026-10-04） |
| --- | --- |
| 「基于 WebGL 开发，对浏览器环境有兼容性要求，需要完整支持 WebGL 的现代浏览器；WebGL 支持欠佳的浏览器会**降级为 Canvas 绘制**，若仍存在兼容性问题则**降级到瓦片图渲染**」 | [jspopularGL/guide/usage](https://lbsyun.baidu.com/docs/jsapi?title=jspopularGL/guide/usage) |
| 「支持 PC 端和移动端基于浏览器的地图应用开发」 | GL 简介 |
| 4.0 快速开始要求 `<meta name="viewport" content="width=device-width, initial-scale=1.0">`；「现代浏览器：Chrome、Firefox、Edge、Safari 等（推荐 Chrome）」；纯 HTML 双击即可运行 | [firstdemo](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/quickstart/firstdemo) |
| **4.0 精确浏览器版本兼容矩阵：官方文档未公开** | 实测各页均无该表 |

### 5.5 浏览器定位（PRD D7 获得官方背书）

4.0 定位页原文（`https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/lbs/locate`，2026-10-04）：

> 「**由于 Chrome、iOS10 以上系统等已不再支持非安全域的浏览器定位请求，为保证定位成功率和精度，请尽快升级您的站点到 HTTPS。iOS15 系统浏览器默认关闭位置请求**」
> 「浏览器定位精度和性能依赖浏览器自身特性，**IP 定位精度为城市级别**」

**→ D7「不做浏览器定位」应引用这两句官方原文作为理由，而非仅凭我方推断。**

### 5.6 JSAPI 侧的 LBS 服务（前端直连会消耗配额）

4.0 文档确认存在：`BMap.DrivingRoute` / `WalkingRoute` / `RidingRoute` / `TransitRoute`（`DrivingRoute` 途经点最多 10 个；`Walking` / `Riding` 支持传地址字符串；`plan.getDistance()`、`plan.getDuration()`）；`BMap.LocalSearch` + `BMap.Bounds` 支持普通 / 周边 / 矩形检索。

**⚠️ 这些是配额消耗型调用**：权益表「JS API 地点检索」个人 **100/日 · 3 QPS**，「JS API 驾车 / 步行 / 骑行 / 公交路线规划」5,000/日 · 3 QPS。

**→ CircleDoc 应坚持 D3（后端承担全部百度调用），前端只用 JSAPI 做渲染，不使用其 LBS 服务类 API**，否则配额双倍消耗且 AK 暴露在前端。

---

## 6. 坐标系对照与指定参数

### 6.1 四个坐标系的关系（官方原文）

**来源**：`https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concept/coord`（2026-07-02）· [FAQ 2580](https://lbsyun.baidu.com/faq/details?id=2294&title=2580) · [FAQ 2579](https://lbsyun.baidu.com/faq/details?id=2294&title=2579) · [FAQ 2578](https://lbsyun.baidu.com/faq/details?id=2294&title=2578)。查阅 2026-10-04。

| 坐标系 | 官方定义 | 备注 |
| --- | --- | --- |
| **WGS84** | GPS 大地坐标系 | 海外地图统一使用 |
| **GCJ02** | 火星坐标系 / 国测局坐标，由 WGS84 加密而来 | 高德、腾讯使用 |
| **BD09 / BD09LL** | 百度坐标系，**在 GCJ-02 基础上二次加密** | 「**所有接口的默认输入输出坐标均基于此坐标系**」；`bd09ll` = 经纬度形式 |
| **BD09MC** | 百度墨卡托（米制）形式 | 出现在各接口参数值域中 |

**JSAPI 转换映射表（官方逐字）**：`1 = WGS84（经纬度）`、`2 = WGS84（墨卡托米制）`、`3 = GCJ02（高德 / 腾讯 / 谷歌）`、`4 = GCJ02（墨卡托米制）`、`5 = BD09（百度经纬度）`、`6 = BD09（墨卡托米制）`。
用法：`const convertor = new BMap.Convertor(); convertor.translate(points, 1, 5, callback);`
FAQ 2578：使用百度服务需 BD09，非 BD09（WGS84 / GCJ02）**必须先转 BD09，否则点位偏移**。
FAQ 2580：中国地区（含港澳台）所有产品支持返回 GCJ02 与 BD09；**海外返回 WGS84**。
⚠️ 旧的「坐标系说明书」`index.php?title=coordinate` 本次打开为**空页**，不可作为依据。

### 6.2 ⚠️ 各接口指定坐标系的参数名（**本项目最容易写错的地方**）

**这是 PRD 未覆盖、但会直接导致 R8 偏移 bug 的关键事实。** 参数名与值类型在三处互不统一：

| 接口 | 入参参数名 | 入参值类型与值域 | 出参参数名 | 出参值域 | 默认 | 来源（2026-10-04） |
| --- | --- | --- | --- | --- | --- | --- |
| 地理编码 v3 | **（无入参坐标）** | — | **`ret_coordtype`** | `gcj02ll` / `bd09mc` | **bd09ll** | [webservice-geocoding-base](https://lbsyun.baidu.com/docs/webapi?title=geocoding/guide/webservice-geocoding-base) |
| 逆地理编码 v3 | **`coordtype`**（⚠️**无下划线**） | 字符串：`bd09ll` / `bd09mc` / `gcj02ll`（仅限中国）/ `wgs84ll` | **`ret_coordtype`** | `bd09ll` / `gcj02ll` / `bd09mc` | **bd09ll** | [webservice-geocoding-abroad-base](https://lbsyun.baidu.com/docs/webapi?title=reverse_geocoding/guide/webservice-geocoding-abroad-base) |
| 地点检索 v2 / v3 | **`coord_type`**（⚠️**有下划线且取数字**） | **数字：`1`=wgs84ll / `2`=gcj02ll / `3`=bd09ll / `4`=bd09mc** | **`ret_coordtype`** | `gcj02ll` | **3（bd09ll）** | [interfaceDocumentV3](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV3) · [interfaceDocumentV2](https://lbsyun.baidu.com/docs/webapi?title=placev3/interfaceDocumentV2) |
| 路线规划 v2（步行 / 骑行 / 驾车 / 公交） | **`coord_type`**（⚠️**有下划线、取字符串**） | 字符串：`bd09ll` / `bd09mc` / `gcj02` / `wgs84` | **`ret_coordtype`** | `bd09ll` / `gcj02` | **bd09ll** | [webservice-direction/walking](https://lbsyun.baidu.com/docs/webapi?title=directionv2/webservice-direction/walking) |
| 轻量路线规划 v1 | **`coord_type`** | 同上（字符串） | **`ret_coordtype`** | 同上 | **bd09ll** | [webservice-lwrouteplanapi/walk](https://lbsyun.baidu.com/docs/webapi?title=directionlite/guide/webservice-lwrouteplanapi/walk) |
| 批量算路 v2 | **`coord_type`** | 字符串：`bd09ll` / `bd09mc` / `gcj02` / `wgs84` | — | — | **bd09ll** | [routematrix/routchtout](https://lbsyun.baidu.com/docs/webapi?title=routematrix/routchtout) |
| JSAPI 全局 | **`BMap.coordType`** | 常量 `BMAP_COORD_GCJ02` 等 | — | — | BD09 | [concept/coord](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/concept/coord) |
| JSAPI 单对象 | `options.coordType` | 同上 | — | — | 继承全局 | [map/gcj02](https://lbsyun.baidu.com/docs/jsapi?title=jsapi4/guide/map/gcj02) |

**必须写进 `docs/DESIGN.md`（FR-411）与代码的三条硬性约束**：

1. **`coordtype`（逆地理，无下划线）与 `coord_type`（其余，有下划线）拼写不可互换**；
2. **地点检索的 `coord_type` 传数字**（`3`），其余接口的 `coord_type` 传字符串（`bd09ll`）—— **同一个参数名、两种值类型**；
3. **Web 服务坐标顺序不统一**：`location`（逆地理 / 圆形检索）与 `origin` / `destination`（direction / routematrix）是 **`纬度,经度`**；而地理编码返回的 `result.location` 与检索返回的 `location` 是 **`{lng, lat}` 对象**；direction 返回的 `steps[].path` 是 **`经度,纬度` 分号串**。→ **FR-406 应有专门一条单测锁住这三种顺序。**

### 6.3 坐标转换 API

- 官方：「**单次请求可批量解析 100 个坐标**」；配额 个人 5,000/日 · 3 QPS
- 来源：`https://lbsyun.baidu.com/docs/webapi?title=geoconv/guide/changeposition`（2026-03-19）
- ⚠️ **该接口的确切 endpoint 路径本次未从官方页面取得正文确认 → 见 §9-④。禁止凭记忆写入代码，需重新打开该页核对，或在控制台对应服务的"示例代码"处复制。**

**对 PRD R8 的判定**：全链路统一 BD09LL 的方案**可行且必要**；§4.2 的算法本身**不需要坐标转换**（所有输入都是百度自己返回的 BD09 坐标）。**只有当 FR-101 允许用户粘贴 GCJ02 / WGS84 坐标（例如从高德地图复制）时才需要转换** → 建议把坐标转换 API 从核心链路移到"可选辅助"，并在 UI 上显式标注输入坐标系（默认 BD09LL）。

---

## 7. 服务条款合规判定

### 7.1 条款版本与来源

| 页面 | URL | 查阅日期 |
| --- | --- | --- |
| 百度地图开放平台产品和服务协议（主条款；页面自注「更新时间: 2026-08-05 19:39」） | `https://lbsyun.baidu.com/index.php?title=open/law`（实际渲染于 `https://lbsyun.baidu.com/docs/pcsa?title=law/open/law`） | 2026-10-04 · **2026-10-11 复核同一 URL 可达、自注更新时间未变** |
| **《百度地图开放平台隐私政策》**（条款 7.3 要求外链的那一份；页面自注「更新时间: 2026-08-24 10:04」） | `https://lbsyun.baidu.com/index.php?title=openprivacy`（实际渲染于 `https://lbsyun.baidu.com/docs/pcsa?title=compliance/openprivacy`） | **2026-10-11**（FR-413 页脚外链就是这一条；`web/isochrone.html` 与 `web/index.html` 用的是 `index.php` 那种旧形式，与官方页脚自己的 `<a>` 逐字一致） |
| 使用者须知 | `https://lbsyun.baidu.com/index.php?title=open/question` | 2026-10-04 |
| 《技术服务授权协议》 | `https://lbsyun.baidu.com/index.php?title=open/law-commercial` | 2026-10-04 |
| 《服务购买协议》 | `https://lbsyun.baidu.com/index.php?title=open/law-apisale` | 2026-10-04 |
| 《开发者认证协议》 | `https://lbs.baidu.com/protocol/agree` | 2026-10-04 |

### 7.2 与本项目直接相关的条款原文（逐字摘录）

> **2.2** 「您只可使用在本平台官方文档中所列明开放的功能来对相关服务数据的结果进行展示，**不得直接存取、技术手段抓取、使用内部数据、图片、程序、模块或是任何其他百度地图的服务或功能**。」

> **2.3** 「开发者如因**非商业目的**使用本服务，仅需注册本平台开发者账号、签署本协议即可申请一定额度内的免费配额和并发服务…开发者如因**商业目的（包括但不限于对第三方用户收费、项目投标、后台管理系统、任何其他直接或间接获取收益）**…」需商用授权。

> **3.2.2** 「本平台有权对每个开发者账号每天发起的服务请求次数及并发请求量等进行一定的限制并不定时对限制标准进行调整…相关服务达到限制后，**百度将暂时中断您的服务正常使用**。」

> **3.2.3** 「请您合理使用…服务权限及配额，**不得随意转借**…(i) **同一产品或应用通过注册多个个人或企业账号获取更高服务配额**；(ii) 对本平台或本服务进行压力测试并对百度线上服务稳定性造成影响；(iii) 注册申请账号时提交的使用场景与实际使用场景严重不符…有权随时终止…采取账号、AK 封禁等措施。」

> **3.3.2(ii)** 「使用任何漫游器、抓取工具、插件、外挂、或非经授权的第三方工具或服务非法获取存储数据或通过任何方式获取原始地图数据或其他数据，**对本服务运行过程中释放出的任何地图数据或其他数据进行包括但不限于删除、修改、爬取、截图、存储、缓存、下载或镜像等操作**。」

> **3.3.3** 「未经行政主管部门审批、许可或备案，擅自脱离本服务单独使用或展示相关内容或通过对本服务的使用使得您或任何其他人可以**预读取、缓存、下载、复制、存储或修改任何服务相关内容**。」

> **3.3.4** 「**复制、翻译、修改或创造相关内容的衍生品（包括生成或用于数据库）**，或**公开展示相关内容的全部或任何一部分**；为本服务创造或提供封装，例如，在您向其他人提供的任何应用接口中使用或提供本服务或相关内容的任何部分，或创造一个产品以重现或复制本服务或本平台；为了销售拓展的目的，印刷包含相关内容截图的销售附属材料，或为了再销售的目的将相关内容作为核心内容整合到印刷品（如印刷地图或使用指南）中。」

> **6.1 / 6.3 / 6.4** 知识产权归百度；「**本服务所涉及的运营数据的全部权利，均归属百度所有，未经百度事先书面同意，您不得为任何目的使用**」；未经百度事先书面同意，不得为任何商业或非商业目的自行或许可第三方实施、利用、转让。

> **6.5** 仅授予「在使用本服务过程中…使用百度地图标志用以宣传」的许可，并附 a–g 项禁止行为。

> **6.6** 「通过本服务提供给您的地图图片、搜索结果中可能包含法律法规要求标明的**审图号**、出版号…以及百度…的任何版权标识…您**不得以任何方式进行删除、掩藏、修改或替换**本服务及相关内容中所附的或包含的任何专有权利声明…、其他任何链接或公告。」

> **7.3** 须向用户显著披露使用本服务，并提供《百度地图开放平台隐私政策》链接。

> **使用者须知**：「**必须保留百度版权信息**」；「针对商业应用场景，建议申请百度地图商业授权服务」。

> 站点页脚当前审图号 **GS(2025)4125**、测绘资质 **甲测资字11111342**（2026-10-04 读取；
> **2026-10-11 用真实浏览器在 `https://lbsyun.baidu.com/` 首页页脚复核未变**，DOM `innerText` 逐字取到的
> 是 `地图审图号：GS(2025)4125|测绘资质：甲测资字11111342`，同一页脚还带 `京ICP证030173号`）。

> 🔴 **这两串字符会过期，而且自动守不住**：官方只在首页页脚给 HTML 文本，没有机器可读的真值，
> 百度按年换号。所以 `web/isochrone.html` 的 `.foot-review` 在号码旁边写了**读取日期**，
> 复核节奏登记为**季度人工**（README §10.7）。与之相对，页脚里那句 TTL（24 h / 7 天 / 30 天）
> 有 `test_compliance_footer_ttl_matches_config` 对着 `Settings.model_fields` 的类默认值自动比对——
> **能自动的都自动了，剩下的这段必须靠人**。

### 7.3 对 FR-402 / FR-412 的逐条判定

#### FR-402 —— 响应级 SQLite 缓存（按服务分 TTL：POI 3 天 / 路线 7 天 / 地理编码 30 天）

> ## **判定：🟠 有风险 —— 可用，但必须限缩形态**
> 必须把它定位为"**同进程运行时性能缓存**"，而不是"**数据资产库**"。

| 条款 | 冲突点 | 风险 | 缓解 |
| --- | --- | --- | --- |
| **3.3.2(ii)** | 明文把「**存储、缓存**」列为对被释放数据的禁止操作之一 | 🟠 主要风险来源 | 缓存仅作技术性能措施：**不落原始字段全量、不建可查询的 POI 库、TTL 到期即物理删除** |
| **3.3.3** | 「脱离本服务单独使用…使得您或任何其他人可以**预读取、缓存、下载、复制、存储**」 | 🟠 中 | **缓存不得成为对外数据源**：任何 API 响应都不得把缓存中的百度原始记录输出为可批量导出的数据集；FR-305 的 JSON 导出应只含**结果（分数 / 多边形 / 统计）**，不含原始 POI 记录集 |
| **3.3.4** | 「创造相关内容的**衍生品（包括生成或用于数据库）**」 | 🟠 中 | SQLite 表按**响应报文缓存**建模（`key → payload → expires_at`），**不要**建成 `pois(id, name, category, lng, lat)` 这类"POI 数据库"结构 —— 后者字面命中"生成或用于数据库" |
| **2.2** | 「不得直接存取、技术手段抓取…任何百度地图的服务或功能」 | 🟡 低 | 只走公开 API、不做网页抓取即可 |
| **3.2.3(iii)** | 「提交的**使用场景与实际使用场景严重不符**」可被封号 | 🟠 注意 | 认证 / 申请时填写的使用场景应写"15 分钟生活圈可达性辅助分析工具（非商业、演示用途）"，与实现一致 |

**可执行整改建议（4 条）**

1. **把 POI TTL 从 3 天降到 ≤ 24 h**（PRD §4.6 现值 3 天）。理由：TTL 越短越接近"性能缓存"，越长越接近"数据库衍生品"。**⚠️ 这是本笔记给出的产品假设而非官方规定，需在 README 中如实说明——官方未公开"允许缓存多久"这一数值。**
2. 缓存表结构收敛为 `cache(key TEXT PRIMARY KEY, service TEXT, payload TEXT, created_at INTEGER, expires_at INTEGER)`，**不建 POI 维度表**；启动时与每次写入时清理过期行。
3. **镜像中不得预置任何 `.db` 缓存文件**（否则变成"随镜像分发缓存数据"，同时触碰 3.3.2(ii) 的"镜像"与 3.3.4 的"公开展示"）。
4. 在 README 与 `docs/DESIGN.md` 增加"数据来源与合规声明"章节，写明缓存用途是减少重复调用、不做数据资产留存，并附本节条款原文。

#### FR-412 —— 内置示例数据集（≥ 1 个社区的 POI + 边界实测样本），带来源声明，随 Docker 镜像分发

> ## **判定：🔴 禁止当前形态 —— 必须改造**
> 不得以"复制下来的百度 POI 数据"作为镜像内容分发。

| 条款 | 冲突点 | 判定 |
| --- | --- | --- |
| **3.3.4** | 「复制…或创造相关内容的衍生品（**包括生成或用于数据库**），或**公开展示相关内容的全部或任何一部分**」 | 🔴 **直接命中**。把调用得到的 POI 名称 + 坐标 + 类别存成 `data/samples/*.json` 并随镜像分发给评审方，字面即"生成或用于数据库" + "公开展示相关内容的一部分" |
| **6.3** | 「本服务所涉及的**运营数据的全部权利，均归属百度所有**，未经百度事先书面同意，您**不得为任何目的使用**」 | 🔴 高。POI 数据属运营数据 |
| **3.3.3** | 「未经…审批、许可或备案，**擅自脱离本服务单独使用**或展示相关内容」 | 🔴 高。`mock` 模式的本质就是脱离服务单独使用 |
| **3.3.2(ii)** | 「…存储、下载或**镜像**等操作」 | 🔴 "随 Docker **镜像**分发"在语义上即镜像分发 |
| **6.6** | 「不得删除、掩藏、修改或替换…审图号…专有权利声明」 | 🟠 若示例数据里含地图截图，必须原样保留版权与审图号标识 |
| **2.3** | 「非商业目的…可申请一定额度内的免费配额」 | 🟡 评审演示属非商业，可援此说明意图，**但不能覆盖 3.3.4 的禁止性表述** |

**FR-412 的合规改造方案（三选一或组合，PRD 必须改写）**

| 方案 | 做法 | 合规性 | 代价 |
| --- | --- | --- | --- |
| **A. 程序化合成的示例数据（推荐）** | 示例集**不来自任何真实 API 返回**：给定中心点 → 由我方算法合成等时圈多边形顶点与"设施点"，名称使用虚构标识（如 `样例-综合医院-01`），坐标落在虚构网格上。README 声明「本数据为程序合成的结构示例，不含任何百度地图服务数据」 | 🟢 高。不复制百度内容，不命中 3.3.4 | 演示真实感下降 |
| **B. 运行时预热，镜像内不预置数据** | 由演示者本人用自己 AK 先跑一遍样例生成缓存，`docker compose up` 时由容器进程调用生成，**镜像不含任何数据文件** | 🟡 中。仍是缓存，但数据不出演示者账号、不随镜像分发 | 评审方离线 / 无 AK 时失效，与 FR-105 的 `mock` 承诺冲突 |
| **C. 取得百度事先书面同意 / 商用授权** | 依 6.3「未经百度事先书面同意」→ 提工单或购买商用授权，取得留存与分发权 | 🟢 最高 | 需时间与费用（**商用授权与额度提升价格：官方文档未公开 → 见 §9-⑤**） |

**FR-403 的连带影响（重要）**：三层降级链的最内层（内置示例数据）**必须采用方案 A 的合成数据**，才能保证"断网演示"既跑通又不违规。
→ PRD 需把 FR-412 的表述从「内置示例数据集（含来源声明）」改为「**合成示例数据集**（程序生成，不含任何第三方服务原始数据；来源声明改为"生成方法与参数说明"）」。
→ 同时 §9 交付物清单里 `data/samples/*.json` 的"来源声明"应写明"合成数据"而非"来自百度地图"。

#### 批量抓取 / 压测（对应 FR-409、§6.2「人为把限流阈值调到 1 QPS 并连发 20 个请求」）

> **判定：🟠 有风险 —— 只能在本地客户端侧做，不得对百度线上服务施压。**

- **3.2.3(ii)** 明文禁止「对本平台或本服务进行**压力测试**并对百度线上服务稳定性造成影响」。
- §6.2 该验收项应改写为：**「对本地令牌桶 / 队列做压测（网络层被 mock），验证无请求穿透到百度」**，而非向真实 API 连发。CI 用 `--mock` 模式即可（PRD §5「可测试」已要求 CI 无需真实 AK）✅ 方向一致。
- **2.2 / 3.3.2(ii)** 禁止"批量抓取"类操作。→ §4.3 的多类别检索属**有明确业务目的的功能调用**（有半径上界、有 150 条封顶），不等同于批量抓取；但仍须避免"遍历全市网格调 API"，这与 PRD §2.3「不做全市批量扫描」一致 ✅。

#### 版权与审图号标识（对应 §5「页面展示百度地图版权标识」）

> **判定：🟢 PRD 已合规，但需追加两条硬约束。**

- §5 已要求"页面展示百度地图版权标识" ✅
- **6.6 追加要求**：不得删除 / 掩藏 / 修改 / 替换任何专有权利声明，**包括审图号**。JSAPI 渲染的地图自带版权层；**做 FR-305 报告 PNG 导出时，截图必须保留版权与审图号，不得裁掉**。
- **7.3 追加要求**：页面需显著披露"本服务使用百度地图开放平台"并提供其隐私政策链接 → **建议新增一条 FR（或并入 FR-407 README）**：页脚含《百度地图开放平台产品和服务隐私政策》链接。

#### 一张总表：PRD 各 FR 的合规判定

| FR | 内容 | 判定 | 关键条款 |
| --- | --- | --- | --- |
| FR-402 | SQLite 响应级缓存（POI 3 天 TTL） | 🟠 有风险（限缩后可用） | 3.3.2(ii)、3.3.3、3.3.4 |
| FR-403 | 三层降级到内置示例数据 | 🟠 有风险（须改为合成数据） | 3.3.3 |
| FR-412 | 内置示例数据集随镜像分发 | 🔴 禁止当前形态 | 3.3.4、6.3、3.3.3 |
| FR-213 | 设施明细表**支持导出 CSV** | 🟠 有风险 | 3.3.4「公开展示相关内容的全部或任何一部分」→ 建议：导出仅限**当前一次分析的结果**、加"数据来源：百度地图开放平台"与"不得二次分发"声明，不做全库导出 |
| FR-305 | 导出 JSON 原始结果 | 🟠 有风险 | 同上 → JSON 只导出**结果对象（分数 / 多边形 / 统计）**，不导出原始 POI 记录集 |
| FR-405 | 并发批量测时 | 🟢 合规 | 走公开批量算路接口，属文档列明的功能 |
| FR-406 | 单元测试（限流 / 缓存 / 算法纯函数） | 🟢 合规 | 纯本地计算，不涉外部数据 |
| FR-409 | CI（lint + 单测 + 构建镜像） | 🟢 合规 | 前提是 CI 不持真实 AK（PRD §5 已要求）✅ |
| FR-202 / 203 / 204 / 222 | 地图渲染（等时圈 / 热力 / 盲区） | 🟢 合规 | 属"对服务数据结果进行展示"，且保留版权标识 |
| §5 前端资源 vendored | 本地化 Vue / ECharts | 🟢 合规 | 不涉及百度数据；⚠️ 但**JSAPI 运行时本身无法 vendored**，见 §8.2 末段 |
| §2.3 不做全市批量扫描 | — | 🟢 合规且必要 | 与 3.2.3、3.3.2(ii) 一致 |

---

## 8. 对 PRD 的修改建议

> ⚠️ 本笔记**不修改** `PRD.md`（另有会话在改）。以下建议供其采纳，每条均给出官方依据。

### 8.1 §4.6 限流参数：令牌桶 `rate = 3 QPS` / `burst = 5` → 必须下调

| 项 | PRD 现值 | 问题 | 建议值 | 依据 |
| --- | --- | --- | --- | --- |
| `rate` | 3 QPS | 恰好**等于**个人认证全线 QPS 上限，令牌桶满速即贴着天花板跑；超发表现为零星失败（FAQ 2361） | **2.0 QPS**（留 1/3 余量） | §3.1 全线 3 QPS |
| `burst` | 5 | ⚠️ **burst > QPS 上限**，桶满时一次放行 5 个并发请求必然触发 `status=401` | **1–2**（严格 ≤ rate） | §3.1 + §3.2 的 401 |
| 全局限流器 vs 分服务限流器 | 未区分（所有出网请求经同一客户端） | 路线规划 5,000/日 与地点检索 **100/日** 稀缺度完全不同，共用一个桶会让稀缺服务被廉价服务挤占 | **按服务分桶**：`place` 0.5 QPS / 80 次-日预算；`regeo` 0.5 QPS / 250 次；`route` 2.5 QPS / 4,000 次；`geocode` 1 QPS / 4,000 次 | §3.1 |
| 重试判据 | 「仅对 5xx / 网络超时 / 百度业务码表示限流的响应重试」 | 🔴 **重大错判风险**：百度**不用 HTTP 5xx 表示限流**，而是 HTTP 200 + body `status`。按 5xx 写会**完全漏掉限流信号** | 判据改为按 body `status`：**{1, 8} 重试**；**401 指数退避重试**；**{4, 302} 立即熔断、当日不再重试**；**{2,3,5,9,101,200,201,202,203,210,211,240,250,251,252,260,261} 不重试直接降级** | §3.2 状态码表 |
| 退避序列 | 0.5 s → 1 s → 2 s，最多 3 次 | 与"秒级并发窗口"语义基本契合，可保留 | **保留**，但 `status=401` 单独用 ≥1 s 首退（QPS 窗口以秒计） | FAQ 2361 |
| 熔断 | 连续 5 次失败 → 打开 30 s | ✅ 合理；但**必须对"配额耗尽"单独设长熔断**，30 s 重连只会加速烧配额 | 拆两个熔断器：`concurrency_breaker`（5 次 / 30 s，针对 401、1、超时）与 **`quota_breaker`（status 4 或 302 → 打开至次日零时）** | FAQ 2361「配额超限当天禁止调用」+ FAQ 2366「次日零时归零」 |
| 单次分析调用上限 | 150 | 对路线规划安全（5,000/日 → 每天最多 33 次分析），但**对地点检索完全失效**：150 > 100/日 | 拆预算：`route_budget = 150`、`place_budget = 80`、`regeo_budget = 3`、`geocode_budget = 20` | §3.1 + §8.3 |
| 缓存键 | `sha1(service + sorted(params) + ak_profile_tag)` | ✅ 可用；`ak_profile_tag` 无必要（同账号配额共用，见 FAQ 2594） | 保留键结构，把 TTL 表改为 §8.1 下条 | §3.3 |

**§4.6 TTL 表建议值**（PRD 现值 → 建议）

| 服务 | PRD 现值 | 建议 | 理由 |
| --- | --- | --- | --- |
| 地理编码 / 逆地理编码 | 30 天 | 30 天（**保留**） | 地址映射极稳定，属纯技术缓存，风险最低 |
| POI 检索 | **3 天** | **≤ 24 h** | 缩短 TTL 以贴近"性能缓存"定位，降低 3.3.4「生成或用于数据库」风险（见 §7.3）。⚠️ 此为产品假设，官方未公开允许时长 |
| 路线规划 / 等时圈边界 | 7 天 | 7 天（**保留**） | 路网稳定；且这是 §2.2「缓存命中率 ≥ 95 %」的主要来源 |
| 错误响应 | 不缓存 | 不缓存（**保留**） | ✅ 正确 |

**⚠️ 两条必须新增进 §4.6 的官方事实（PRD 现在完全没有）**：

1. **FAQ 2594：同账号下 AK 之间配额共用。** → §4.1「两个独立 AK」的表述应补充：独立的是**鉴权类型**，不是**配额池**。
2. **批量算路配额按路线条数计，不按请求数计**（官方原文见 §2.8）→ `/api/analysis` 的 `api_calls` 字段与 §2.2 的"调用次数"必须明确按 `origins × destinations` 折算，否则计量口径失真。

### 8.2 §2.2 成功指标：四条必须改数字或改口径

| 指标 | 现值 | 判定 | 建议改为 | 依据 |
| --- | --- | --- | --- | --- |
| 「限流导致的 **429** 错误 0 次」 | 429 | 🔴 **无法度量：官方状态码表中不存在 429** | 「**`status ∈ {4, 302, 401}` 出现次数 = 0**（由客户端重试计数器统计）；`401` 经重试后最终成功的次数单独记录为 `throttled_recovered`」 | §3.2 |
| 「百度 API 调用次数 / 单次体检 ≤ 120 次」 | ≤ 120 | 🟠 口径不明（HTTP 请求数 vs 配额消耗数） | 拆两栏报告：**「HTTP 请求数 ≤ N」** + **「配额消耗数 ≤ 120（路线类按 origins×destinations 折算）」** | §2.8 配额计量原文 |
| 「缓存命中率（第二次运行同一样例）≥ 95 %」 | ≥ 95 % | 🟡 可行，但一旦有 POI 检索未命中就立刻消耗 100/日 的份额 | 保留 ≥ 95 %，**同时新增**「第二次运行 `place` 服务实际出网请求数 = 0」 | §3.1 |
| 「完整体检端到端耗时（冷启动，无缓存）≤ 45 s」 | ≤ 45 s | ✅ 可达；**若采纳 §8.4 建议可下调目标** | 冷启动 ≤ **25 s**（改用批量算路后 HTTP 往返从 ≈96 降到 5–8 次）；缓存命中 ≤ 3 s 保留 | §2.8 + §3.1（3 QPS） |
| 「前端首屏渲染 ≤ 1 s」 | ≤ 1 s | 🟠 受 JSAPI 动态加载影响（无法 vendored，见下） | 限定为「**本地 vendored 资源加载 + 页面骨架渲染 ≤ 1 s**，不含百度 JSAPI 运行时与底图瓦片」 | §5.4 + R12 |

**⚠️ 与 §5「前端资源不引用公网 CDN」的直接冲突（必须写进 PRD）**：

JSAPI 4.0 通过 `https://api.map.baidu.com/api?v=4.0&ak=...` 动态加载，**其核心运行时与瓦片均来自百度服务器，无法 vendored**。因此：

- **FR-105 的 `mock` 模式承诺需精确表述为**：「断网时 Vue / ECharts 仍本地加载成功，**页面结构、雷达图 / 柱状图、设施明细表、盲区清单与评分卡均可完整渲染**；地图底图与等时圈覆盖物无法显示，前端显示离线占位卡片」。
- **§6.3 那条「拔掉网线后打开页面，Vue 与 ECharts 仍加载成功并进入 mock 报告」应同步改写**，避免评审按"地图也能显示"判定失败。
- 若确需"断网也能看到等时圈形态"，官方机制里**没有离线瓦片方案** → 建议改为展示**我方合成的 SVG / Canvas 静态示意图**（数据为合成，符合 §7.3 方案 A），并在 UI 上明确标注"离线示意图，非真实地图"。

### 8.3 🔴 最严重的现实冲突：**地点检索 100 次/日**

PRD §4.3 的检索方案在个人认证配额下，**一次体检就会打满甚至超出当日上限**。

**现方案调用量估算**：6 大维度 × 每维度 2–4 个别名 query = **12–24 次检索**；若再加翻页（`page_size=10`、`total ≤ 150` → 每类别最多 15 页），单类别即可耗尽配额。叠加 FR-108 联想（**与地点检索共用配额，FAQ 2540 / 2549**），100 次/日的池子极脆弱。

**建议（写入 §4.3 + §4.6）**

| 措施 | 具体做法 | 依据 |
| --- | --- | --- |
| **1. 别名合并成一次请求（关键收益）** | 圆形区域检索 `query` **支持多关键字并集、以 `$` 分隔、最多 10 个** → **一个维度一次调用**。例：`query=药店$诊所$综合医院$社区卫生服务中心` | §2.3 ✅官方确认 |
| **2. 硬性预算** | 6 维度 = **6 次**地点检索（+ 便民服务参考项 1 次 = **7 次**）；`place_budget = 80/日`，留 20 次给联想与人工调试 | §3.1 的 100/日 |
| **3. 只取第一页、不翻页** | `page_size=10`（按 ⚠️官方矛盾取保守值）、`page_num=1`；用 `center` + `filter=sort_name:distance` 保证拿到的是最近的 10 个 → 对"1 km 内有无药店"这类判定足够 | FAQ 2542 + §2.3 距离排序 |
| **4. 用 `total` 做圈内计数** | `total` 上限 150 → FR-211 的"圈内数量"用 `total`（再本地射线法复核），明细表用第一页 10 条 | FAQ 2534 / 2543 |
| **5. FR-108 联想加门槛** | 改为「输入 ≥ 3 字 + 防抖 **500 ms** + 结果本地缓存 + **默认关闭、可手动开启**」；或改用**地理编码**（5,000/日，池子大 50 倍）做落点 | FAQ 2540 / 2549 + §3.1 |
| **6. FR-103 逆地理降为 1 次 / 分析** | 逆地理 300/日 → 每天仅够 **300 次分析**（若每次 1 个逆地理）。演示阶段可接受，但报告需标注逆地理来源与失败降级 | §3.1 的 300/日 |
| **7. 样例社区预热策略** | FR-412 改造为合成数据后，预置样例的 `mock` 路径**不消耗配额**；`live` 路径仅用于评审现场自愿演示。README 需明确"建议先以 `cache` 模式预热一次" | §7.3 方案 A |
| **8. §6.1 验收项补一条** | 「断言：单次完整体检的 `place` 服务出网请求数 ≤ 8」——这是比"总调用 ≤ 120"更有说服力的配额意识证明 | §3.1 |

**另需修正的 §4.3 表述**

- 「检索返回空 → 该类别计数为 0 并标注'检索无结果'，**不**当作异常」✅ 正确，与 FAQ 2536（接口会过滤掉部分数据）一致；建议再加一句「**计数可能因平台数据保护而被低估，报告须注明这是官方预期行为**」。
- 「以中心点做圆形区域检索，半径取 `R_max × 1.2`」→ 补注「**官方上限 10 km（FAQ 2548），本值步行 1.5 km / 骑行 4.8 km 均安全；须同时传 `radius_limit=true`，否则半径跨越城市边界会退化为城市范围检索（§2.3 官方警告）**」。

### 8.4 §4.2 等时圈调用量预算：改用批量算路

| 项 | PRD 现值 | 官方事实支撑下的建议 | 依据 |
| --- | --- | --- | --- |
| 理论最大直线半径 `R_max` | 步行 15 min ≈ **1.25 km**（按 5 km/h） | 🟠 **PRD 假设偏乐观**。官方示例 `distance=16748 m / duration=15491 s` 推算步行 ≈ **3.9 km/h** → 15 min ≈ **0.98 km**。建议：`R_max` 仍取 **1.25 km 作为二分上界**（上界宁大勿小），但在 §4.2 与 DESIGN 中把"步行 5 km/h"改为「**观测值约 3.9 km/h，据此 15 min 理论半径约 1.0 km，取 1.25 km 为搜索上界**」 | §2.5 官方示例 |
| 骑行 `R_max` | ≈ 4 km | ✅ 可作为二分上界 | §2.6 |
| Step1 粗扫（12 方位角） | 12 次 direction | **改为 1 次 `routematrix/v2/walking`**：`origins` = 中心点（1 个），`destinations` = 12 个探测点 → 12 条路线，**满足 ≤ 50 条限制**，HTTP 往返 1 次 | §2.8 |
| Step2 二分收敛 | 平均 3 轮 × 12 = 36 次 | **每轮 1 次批量请求**（12 条 / 轮；3 轮 = 36 条配额、**3 次 HTTP**）；⚠️ 若做 5 轮 = 60 条 > 50 限制 → **每轮按 ≤ 50 条切分**，或每轮只处理尚未收敛的方向 | §2.8「起终点之积 ≤ 50」 |
| Step3 中点加密 | 约 24 × 2 = 48 次 | **1 次批量请求**（24 条路线）即可，或合并进 Step2 末轮 | §2.8 |
| **调用量上限** | ≈ 96 次 direction | **配额消耗不变（仍 ≈ 96 条路线）但 HTTP 请求数从 96 降到 5–8 次** → 端到端耗时从"96 × (限流等待 + RTT)"降到"8 × (限流等待 + RTT)" | §2.8 配额计量原文 |
| FR-405 的串行 vs 并发对比 | 「并发批量测时（有界线程池）」 | 🔄 **改为「串行 direction vs 批量算路」对比**，比"线程池并发"更有说服力且不违反 3.2.3(ii) 压测条款。预期对比表：串行 ≈ 96 请求 / QPS 3 → **≥ 32 s**；批量 ≈ 8 请求 → **< 5 s** | §2.8 + §7.3 压测判定 |
| 路径可视化 | 未区分 | ⚠️ **批量算路不返回 polyline** → FR-202 / FR-203 所需的真实路径坐标**必须**对入选边界顶点补调 `direction/v2/walking`（≤ 12 次）。**建议 FR-203 的"点击查看该方向路径"改为用户点击时懒加载 1 次**，不预先拉取 | §2.8 |
| `blocked` 判定 | 最小探测步长已 > T | ✅ 补充**官方硬证据**：`links[].attr = 0x01 禁行`、骑行 `restrictions_status = 0x01 禁行 / 0x02 逆行`、`status = 2001 无骑行路线`、directionlite `status = 7 无返回结果` → 比"耗时超标"更可靠 | §2.5 / 2.6 / 2.7 |
| 途经点 | PRD 未假设 | ✅ 确认：步行接口**无途经点参数** → §4.2 的"扇形直线上取探测点"方案不受影响 | §2.5 |
| 轻量版替代 | 未提及 | 🟢 可选：`directionlite/v1/walking?steps_info=0` 响应体最小，适合纯测时；⚠️ 需控制台勾选 | §2.7 |
| 调用预算上限 | §4.6「单次分析设调用上限（默认 150）」 | ✅ 对路线类保留 150；但按 §2.8 折算口径写明"150 条路线配额，非 150 次 HTTP" | §2.8 |

### 8.5 §4.3 POI 词表：按官方分类重写（直接消解 R5）

**官方 POI 分类表中不存在以下 PRD 用词**：养老院、菜市场、农贸市场、社区服务中心、快递驿站、银行网点、理发店。

**建议对照表**（左：PRD；右：官方分类名；均出自 `open/poitags`，2026-10-04）

| PRD §4.3 原词 | 官方分类替代 |
| --- | --- |
| 菜市场 / 农贸市场 | **市场**（购物） |
| 药店 | **药店**（医疗）✅ 原词正确 |
| 综合医院 | **综合医院**（医疗）✅ |
| 社区卫生服务中心 | **诊所**（医疗）；或 **居民委员会**（政府机构）作近似 |
| 小学 / 幼儿园 | **小学** / **幼儿园**（教育培训）✅ |
| 超市 / 便利店 | **超市** / **便利店**（购物）✅ |
| 公园 | **公园**（旅游景点）✅ |
| 健身广场 | **休闲广场**（休闲娱乐）+ **健身中心**（运动健身） |
| 养老院 | **疗养院**（医疗）/ **福利机构**（政府机构） |
| 公交站 / 地铁站 | **公交车站** / **地铁站**（交通设施）✅ |
| 中餐厅 / 快餐厅 / 餐饮 | **中餐厅** / **小吃快餐店**（美食）；如需更宽可用 **购物中心** 周边的餐饮大类 |
| 快递驿站 | （官方一级/二级表中无对应）→ 建议删除，或降级为明细表参考项而不计分 |
| 银行网点 | **银行** / **ATM**（金融）✅ |
| 维修点 | **维修点**（生活服务）✅ |
| 理发店 | （表中未见）→ 建议删除 |

**🔴 必须同步修改的 FR-221（盲区主判据）**：
PRD 原文"周边 1 公里内同时缺少**菜市场、药店、小学**"——其中"菜市场"**不是官方分类**。
→ 改为「**市场 / 药店 / 小学**」，并**优先用返回体的 `tag` 或 `classified_poi_tag` 字段判定类别**（而非依赖自由文本 `query` 命中），这样同时解决 PRD §3.4 的"需求澄清"歧义：判定基于官方结构化分类字段，两种口径（皆无 / 任一缺失）都能在同一数据上计算。

**§4.3 表格建议增列**：在每个维度后加"官方分类路径"列（如 `购物 > 市场`、`医疗 > 药店`、`教育培训 > 小学`），使 README（FR-407）与 DESIGN（FR-411）可直接引用官方出处。

### 8.6 §8.2 风险登记：等级调整

| 风险 | PRD 现判定 | 证据 | 建议调整 |
| --- | --- | --- | --- |
| **R1** 个人 AK 未开通"距离矩阵"类接口或配额极低 | 概率 **高** / 影响 **中** | 批量算路**存在**、**对个人开放**（5,000/日 · 3 QPS）、支持步行；prepare 页仅要求"服务端 AK" | 🔽 **概率降为「低」，影响「中」**。<br>🔄 **同时替换 / 新增 R1'**：「**地点检索个人配额仅 100 次/日、逆地理 300 次/日，且地点输入提示与地点检索共用配额** → 一次体检即可能打满当日上限」，**概率 高 / 影响 高**（这才是真正的配额风险，见 §8.3） |
| **R6** 浏览器端 AK 白名单未配 `localhost` 导致地图空白 | 概率 **高** / 影响 **高** | 官方对本地场景的正面指引是「**本地可以设置 `*`**」（FAQ 2781）与「开发阶段填写 `*`」（JSAPI4 prepare，2026-07-17）；**`localhost` 是否被 Referer 校验接受，官方未明文说明** | 🔄 **改写表述**：「README 未说明 Referer 白名单在本地开发应填 `*`，或误填受限域名导致 JSAPI 加载失败」。<br>解决方案改为**填 `*`**（有官方原文支撑），并把「`localhost` / `127.0.0.1` 能否作为白名单项」列入 §9 需人工实测项。等级维持 **高 / 高**。 |
| **R7** 服务端 AK 与浏览器端 AK 混用导致鉴权失败 | 概率 **中** / 影响 **中** | ✅ 官方确认混用报 `status=203 APP 类型错误`；FAQ 2787「APP Referer 校验失败」三类原因；FAQ 2547 提示词报 240 需服务端 AK | 🔼 **概率升为「高」**（高频踩坑，且 R6 与 R7 会在同一现场叠加）。<br>⚠️ **必须补充应对措施**：「`/api/health` 启动时打印两个 AK 的类型与探测结果；把 `203 / 210 / 211 / 240` 映射为四种不同前端文案」。<br>⚠️ **必须补充新事实**：「**同账号下 AK 配额共用（FAQ 2594）** → 双 AK 只能分离鉴权类型，**不能**扩配额」。 |
| **R5** POI 标签与 query 词不匹配导致漏检 | 概率 **高** / 影响 **中** | ✅ 已确认官方分类表中 7 个 PRD 用词不存在；FAQ 2536 明确"检索接口会过滤掉部分数据" | ➡️ **保持「高 / 中」**，但应对措施从"人工回填词表"升级为「**采用 §8.5 官方分类对照表 + `$` 多关键字单次合并检索 + 用 `tag`/`classified_poi_tag` 判定类别**」，工作量与失配面同时下降。 |
| **R8** 坐标系不一致导致偏移 | 概率 **低** / 影响 **高** | ⚠️ 新发现：参数名与值类型三处不统一（`coordtype` 无下划线 / `coord_type` 传数字 / `coord_type` 传字符串），且坐标顺序有三种写法（`纬度,经度` / `{lng,lat}` / path 的 `经度,纬度`） | 🔼 **概率升为「中」**（写错参数的现实概率不低），影响 **高**。应对：见 §6.2 三条硬性约束 + FR-406 增加"坐标顺序与参数名"单测。 |
| **R2** 演示现场配额耗尽 | 概率 **中** / 影响 **高** | §8.3 已证明地点检索 100/日 是真实瓶颈；FAQ 2361「配额超限当天禁止调用」意味着**现场无法恢复** | ➡️ **保持「中 / 高」，但严重性上调说明**：一旦触发是**全天不可用**，不是短时失败。应对必须写明「**演示前用同一 AK 预热缓存并记录当日已用量；演示当天地点检索出网预算 ≤ 8**」。 |
| **R3** 二分不收敛 / 边界突变 | 概率 **中** / 影响 **中** | ✅ 新增官方判据：`links[].attr=0x01`、`restrictions_status=0x01/0x02`、`status=2001/7` | 🔽 **概率降为「低 / 中」**：有官方硬信号可判 `blocked`，不必只靠"耗时超标"推断。 |
| **R11** Vue 无构建模式模板写错无编译器兜底 | 概率 **中** / 影响 **低** | 与本次核验无关 | ➡️ 保持。 |
| **R12** 前端资源走 CDN 导致白屏 | 概率 **中** / 影响 **高** | ⚠️ 新事实：**JSAPI 运行时本身无法 vendored**（`https://api.map.baidu.com/api?v=4.0&ak=` 动态加载 + 瓦片来自百度服务器） | 🔼 **影响升为「高」并改写**：R12 应限定为「**自托管的 Vue / ECharts 不引用 CDN**」；另加一条 **R13**：「**JSAPI 运行时与瓦片断网不可用 → `mock` 模式下的地图区域必须有无地图降级形态**」（见 §8.2）。 |
| **新增 R14** 合规风险 | PRD 未列 | §7：3.3.2(ii) 禁缓存 / 存储、3.3.4 禁"生成或用于数据库"与"公开展示一部分"、6.3 运营数据归百度、6.6 不得删除审图号 | 🆕 **概率 中 / 影响 高**（评审方若在意开源合规会直接扣分）。应对：FR-402 限缩 + **FR-412 改为合成数据** + README 增"数据来源与合规声明"章节 + 页脚保留版权与隐私政策链接（条款 7.3）。 |

### 8.7 §4.7 接口设计与 §10 待确认事项的补充

| 位置 | 建议 |
| --- | --- |
| §4.7 `GET /api/geocode` | 出参补 `confidence` 与 `precision`（官方返回），前端据此提示"定位精度"；说明内部走 `/geocoding/v3/`，`output=json` 必须显式传 |
| §4.7 `GET /api/regeo` | 明确内部走 `/reverse_geocoding/v3/`，参数名是 **`coordtype`（无下划线）**；出参补 `town`、`town_code`、`adcode`（官方 `addressComponent` 均有）→ 让 FR-103 的"街道"有官方字段支撑，而不是靠文本切分 |
| §4.7 新增 `GET /api/suggest` | FR-108 应由这个接口承担（内部 `/place/v2/suggestion`），**不要复用 `/api/geocode`**（PRD §3.1 FR-108 现写的是"调 `/api/geocode`"）；⚠️ 需在文档中标注它与 `/api/poi` **共用配额** |
| §4.7 `POST /api/poi` | 入参补 `radius_limit=true`、`coord_type=3`（数字）、`filter=sort_name:distance`、`page_size=10`、`page_num=1`；出参补 `total`（官方上限 150）与 `degraded_reason`；**明确不使用多边形区域检索（需商用授权，`status=9`）** |
| §4.7 `POST /api/isochrone` | 明确内部主用 `routematrix/v2/walking`，边界顶点补 `direction/v2/walking`（懒加载）；`vertices[].sample` 枚举建议扩为 `measured / interpolated / extrapolated / blocked(官方禁行证据) / blocked(超时或空结果)` |
| §4.7 返回体 `usage` | 拆为 `{ http_requests, quota_units, route_lines, place_calls, cache_hits, elapsed_ms }`——`quota_units` 是批量算路按路线条数折算后的真实配额消耗（§2.8） |
| §10 待确认 1（样例社区选哪个） | ⚠️ 新增强约束：**样例必须在国内**（境外逆地理编码 / 境外地点检索 / 境外路线规划 对个人开发者均为「-」，见 §3.1） |
| §10 待确认 2（百度 AK 现状） | 本文 §3 已给出官方权益表数值；剩余"我的 AK 实际档位与已用量"→ **需登录控制台核实**，见 §9-⑤ |
| §10 待确认 4（是否需要骑行） | ✅ 官方支持（`direction/v2/riding` + `routematrix/v2/riding`），建议保留为 P1；`road_prefer=3`（无逆行且无阶梯）可作为质量开关 |
| §10 待确认 6（演示网络环境） | 🔄 **必须改写**：断网时**地图不可用是官方架构决定的**（JSAPI 无法 vendored），不是工程缺陷。应写明"断网演示 = 报告 + 图表 + 合成示意图；地图演示需现场可访问 `api.map.baidu.com`" |
| §5「前端资源」 | 补一句：「JSAPI 运行时与地图瓦片由百度服务器提供，不纳入本地 vendored 范围与 ≤ 500 KB 预算」 |
| §6.4「开源工程规范」 | 新增一条验收：「README 含"数据来源与合规声明"，引用本笔记 §7 的条款编号，并说明缓存 TTL 与示例数据的合成性质」 |

---

## 9. 仍未解决 / 需人工在控制台确认

### 9.1 官方文档未公开、且**不可凭记忆填写**的数值

| # | 未解决项 | 现状 | 影响 | 处置 |
| --- | --- | --- | --- | --- |
| ① | **逆地理编码 `pois` 返回条数上限** | 官方只给出 `radius`(0–3000 m)、`poi_types`、`entire_poi`、`sort_strategy` 四个控制参数，**未公开条数上限** | 无法估算"能否用逆地理补充 POI"；FR-103 无影响 | 用自己 AK 实测一次（`extensions_poi=1&radius=3000`）数返回条数；**实测前不要在代码里假设上限** |
| ② | **地点检索 `page_size` 真实上限（10 还是 20）** | ⚠️ 官方三处矛盾：3.0 文档「10–20，最大 20」 vs 3.0 字段注释「单次请求最多返回 10 条」 vs FAQ 2542「最多只支持召回 10 条，不支持更多」 | 决定 FR-213 明细表是否需要翻页，进而决定配额消耗 | **按 10 设计（取保守值）**；若需 20，实测或提工单确认。README 里把这个矛盾如实写出 |
| ③ | **步行路线规划的最大距离限制** | direction v2 步行页参数表与说明**均未给出**。仅批量算路步行有明确「任意起终点距离不得超过 200 KM」 | §4.2 探测点上界选择 | 步行 15 min 场景远低于任何合理上界，**不影响本项目**；DESIGN 中注明"官方未公开该数值" |
| ④ | **坐标转换 API 的确切 endpoint 路径** | 官方文档页 `geoconv/guide/changeposition` 说明了"单次 100 个坐标"与配额，**本次未取到正文中的完整请求 URL 路径** | 仅当 FR-101 允许用户输入 GCJ02 / WGS84 坐标时才需要 | 🔴 **禁止凭记忆写路径**。需重新打开该页在"接口地址"小节复制，或到控制台该服务的"示例代码"处复制 |
| ⑤ | **未认证个人开发者的具体配额数值** | 权益页只有三档（个人 / 企业试用 / 企业授权），无"未认证"档数字；FAQ 2614 只说"认证不通过无法使用" | 无法量化"不认证能跑多少" | 按"不认证不可用"规划（先完成个人认证，见 §4.3） |
| ⑥ | **商用授权与额度提升的价格 / 套餐** | 官方 FAQ 2588 只说明"高级服务需付费、部分仅对企业认证开放、商业目的需商用授权"，**具体价格官方文档未公开** | 影响 FR-412 方案 C 是否可行 | 需登录控制台或提工单询价 |
| ⑦ | **`localhost` / `127.0.0.1` 能否作为浏览器端 Referer 白名单项** | 官方从未明文列出；官方对本地场景的正面指引是填 `*`（FAQ 2781） | R6 的最终定论 | 实测：分别用 `localhost:5173`、`127.0.0.1:8000`、`*` 三种白名单各加载一次 JSAPI，记录 `console` 报错。README 按官方口径写"本地填 `*`"，把实测结论作为补充 |
| ⑧ | **JSAPI 4.0 下 `HeatmapOverlay`（BMapLib）能否正常工作** | 官方只承诺"对 BMapLib 也进行了兼容"，**未给出热力图专项说明或示例** | FR-204 实现路径选择 | 实测三条路径（§5.3）；**若走自绘 `WebGLCustomLayer` 则不依赖此项**（推荐） |
| ⑨ | **JSAPI 4.0 精确浏览器版本兼容矩阵** | 官方文档未公开；仅有 GL 的三级降级说明与"推荐 Chrome" | FR-106 移动端验收 | 真机实测（iOS Safari / Android Chrome），在验收清单中把"未验证"项显式标注（呼应 PRD §10 待确认 5） |

### 9.2 需登录控制台核实的项（附应点开的页面）

| # | 要确认什么 | 应点开的控制台页面 | 为什么重要 |
| --- | --- | --- | --- |
| ① | **我的账号当前实际权益档位**（个人 / 企业试用 / 企业授权）与各服务实时配额 | `https://lbs.baidu.com/apiconsole/quota#/privilege`（控制台 →【额度管理】→【开发者权益】；依据 FAQ 2593 / 2595） | §3.1 表是"档位标准值"，你的账号可能被下调或已升级；**限流参数必须按实测档位写** |
| ② | **当日各服务已用量与剩余配额** | 控制台 →【我的额度】→ **流量图**（FAQ 2595） | 演示前检查，避免 R2（配额耗尽当天不可恢复，FAQ 2361） |
| ③ | **服务端 AK 是否勾选了这些服务**：路线规划、**步行路线规划（轻量）**、**批量算路**、**全球逆地理编码**、地点检索、地点输入提示、地理编码 | 控制台 →【应用管理】→【我的应用】→ 服务端 AK →【设置】→【启用服务】逐项核对；配置页 `https://lbs.baidu.com/apiconsole/key/create#/home` | `status=240 APP 服务被禁用` 的唯一成因；轻量版有"2018-11-14 前注册的 AK 默认无权限"的历史坑（§2.7） |
| ④ | **全球逆地理编码权限** | 控制台 →【查看应用】→【设置】确认是否已获得"全球逆地理编码服务"权限 | 逆地理页原文提示：V3.0「鉴权失败」即查此处 |
| ⑤ | **两个 AK 的类型是否为 Browser / Server，以及各自白名单** | 控制台 →【我的应用】→ 逐个查看【应用类型】与【请求校验方式】/Referer/IP 白名单 | R6 + R7；确认服务端 AK 的 IP 白名单是否已含演示机出口 IP 或已填 `0.0.0.0/0` |
| ⑥ | **AK 数量上限**（个人 10 个 / 企业 40 个） | 控制台 →【我的应用】列表 | 若需为演示新建专用 AK（FAQ 2589）；⚠️ 记住 FAQ 2594：**多 AK 不增配额** |
| ⑦ | **开发者认证状态与人脸识别是否完成** | `https://lbs.baidu.com/apiconsole/auth`（控制台 →【开发者认证】） | FAQ 2614：未认证不可用平台产品 |
| ⑧ | **是否已购 / 需购商用授权，以解锁多边形区域检索、`photos`、`status`（营业状态）、境外服务、五级行政区划** | 控制台 →【商用授权】/【产品服务】相关入口，或直接提工单 | 决定 FR-412 能否走方案 C（§7.3）；也决定 §10 待确认 3 是否可替换为官方指标 |
| ⑨ | **各服务的官方示例代码（含正确的 `sn` 签名实现与 endpoint）** | 控制台内每个服务详情页的"示例代码"标签 | 用于交叉核对 §6.2 的参数名差异，尤其 §9.1-④ 的坐标转换 endpoint |

### 9.3 本笔记与 PRD 的关系与使用建议

1. **本笔记不修改 `PRD.md`**（另有会话在改）。§8 的每条建议都标注了 PRD 的章节 / FR 编号，可直接作为 PRD 修订输入。
2. **写入代码前必须先做的三件事**（按优先级）：
   - 按 §3.1 与 §9.2-① 确认**你自己账号的实际档位**，再决定 §4.6 的 `rate` / `burst` —— **不要直接用 §8.1 的建议值当常量**，先把它做成可经 `.env` 覆盖的配置项。
   - 按 §3.2 把重试判据从"HTTP 5xx"改成 **body `status`**，并落实 `quota_breaker`（4 / 302 → 打开到次日零时）。
   - 按 §6.2 三条硬性约束写一个"接口参数名与坐标顺序"的常量表 + 单测，锁死 `coordtype` / `coord_type`(数字) / `coord_type`(字符串) 三种写法。
3. **`docs/DESIGN.md`（FR-411）应直接引用本文的 §2.8、§3、§6.2、§7.3** —— 这四节正是"API 调用策略、等时圈算法、POI 清洗策略、盲区识别算法"的官方依据。
4. **本文所有 URL 均于 2026-10-04 读取正文**。百度文档站为前端渲染页面，**离线 curl 抓取会只拿到样式而取不到正文**；后续复现核验建议用浏览器打开。文档若更新，需重新核对 §3.1 权益页（其自注更新时间为 2025-10）与服务条款页（自注 2026-08-05）。

