"""POI 维度词表（PRD §4.3 / FR-210 / FR-221）：**纯数据 + 自检，零 I/O、零出网**。

这个文件是 M3 唯一的词表出处。它存在的理由不是「把常量集中放好」，而是 PRD §4.3 第 288
行那条真实的坑：v1.0 的四个类别词（`菜市场`/`农贸市场`/`养老院`/`社区卫生服务中心`）和三个
已被删除的词（`快递驿站`/`理发店`/`社区服务中心`）**在百度官方 POI 分类表里根本不存在**
（笔记 §8.5、Q1-15，官方页面 2024-04-18 版）。留着不存在的词，最坏的后果不是「检索为空」，
而是**该维度永远计数 0、被误判成「这个社区缺配套」**，直接污染 FR-221 的盲区结论。
所以词表必须能被**逐词机器校验**——`assert_wordlist()` 就是那把尺子，CI 里它一红，
说明有人往 query 里塞了官方分类外的词。

三条口径上的硬事实：

1. 🔴 **一个维度只用一次请求**：圆形区域检索的 `query` 支持多关键字并集、`$` 分隔、
   最多 10 个（笔记 §2.3）。六维度 + 便民参考项 = 7 次，加上 1 次冗余 → 单次分析
   `place_calls ≤ 8`（`PLACE_CALLS_PER_ANALYSIS`，PRD §4.3 第 294 行）。词表里的每一项
   都带 `request_count = 1`，`wordlist_problems()` 会拒绝任何「一个维度拆成多次请求」的写法。
2. 🔴 **类别判定靠返回体的 `tag` / `classified_poi_tag` 结构化字段**（FR-214），而不是
   「自由文本 query 是否命中」。`normalize_subcat()` 把两种来源（`药店` 与 `医疗 | 药店`）
   统一折成英文 subcat 键，盲区判定（`BLIND_TRIO_SUBCATS`）只认这个键。
   ⚠️ 注意「药店」是 **medical 维度里的一个 subcat**，不是整个维度——FR-221 要的是
   市场 / 药店 / 小学三类设施，按维度判会错成一判就判六个。
3. ⚠️ **六维权重合计 95 %，不是 100 %**（20+15+20+15+15+10）。这是 PRD §4.3 与 §4.5 之间
   真实的口径缺口，本文件**不擅自归一化**（见 `SCORED_WEIGHT_SUM` 的注释）。

`菜市場 → 市场` 的映射表（`PRD_V1_TERM_REPLACEMENTS`）同时服务两件事：README 要向评审说明
命题原文「菜市场」对应官方分类「市场」（FR-221 的验收标准专门点了这一条），以及自检要把
「已经被替换掉的词」钉在黑名单里，防止有人照命题原文把它改回去。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALL_DIMENSIONS",
    "BANNED_QUERY_TERMS",
    "BLIND_TRIO_SUBCATS",
    "COORD_TYPE_BD09LL_NUMERIC",
    "DIMENSIONS",
    "DIMENSION_ORDER",
    "EMPTY_REASON_NO_RESULT",
    "MAX_QUERY_KEYWORDS",
    "MODE_RIDING",
    "MODE_WALKING",
    "NOTE_FAQ_2536_FILTERING",
    "NOTE_NO_PAGINATION",
    "NOTE_RADIUS_LIMIT",
    "OFFICIAL_POI_PATHS",
    "PAGE_NUM",
    "PAGE_SIZE",
    "PLACE_CALLS_PER_ANALYSIS",
    "PLACE_RADIUS_MAX_M",
    "PRD_V1_TERM_REPLACEMENTS",
    "RADIUS_BY_MODE_M",
    "RADIUS_FACTOR",
    "R_MAX_BY_MODE_M",
    "SCORED_DIMENSIONS",
    "SCORED_WEIGHT_SUM",
    "SUBCAT_LABEL_CN",
    "SUBCAT_OFFICIAL_PATH",
    "TOTAL_CAP",
    "TRANSPORT_MODES",
    "WordlistError",
    "assert_wordlist",
    "dimension",
    "dimensions_for",
    "is_official_term",
    "normalize_subcat",
    "search_radius_for_mode",
    "wordlist_problems",
]


class WordlistError(RuntimeError):
    """词表自检失败。

    为什么继承 `RuntimeError` 而不是 `CircleDocError`：本模块**不做 HTTP、不产结果对象**，
    词表写歪是开发期缺陷而不是运行时降级，绝不该被降级链吃成「本次结果为 sample」。
    """


# --------------------------------------------------------------------------------------
# 半径与请求参数（笔记 §2.3 / FAQ 2544 / 2548 / 2542）
# --------------------------------------------------------------------------------------
MODE_WALKING = "walking"
MODE_RIDING = "riding"
TRANSPORT_MODES: tuple[str, ...] = (MODE_WALKING, MODE_RIDING)

R_MAX_BY_MODE_M: dict[str, int] = {MODE_WALKING: 1250, MODE_RIDING: 4000}
"""等时圈二分上界（PRD §4.2 第 269 行：`R_max` 宁大勿小，步行取 1.25 km 而非 3.9 km/h 的
理论值 1.0 km；骑行 4 km）。**这是二分上界，不是检索半径**，别把它直接递给 `radius`。"""

RADIUS_FACTOR = 1.2
"""检索半径 = `R_max × 1.2`（PRD §4.3 第 275 行）。为什么放大 1.2：等时圈是多边形，
POI 落在圈外的直线半径上仍可能是「最近可用设施」，半径取小会让 FR-211 的
「圈内 / 仅直线半径内」两种计数退化成一个数字。"""

RADIUS_BY_MODE_M: dict[str, int] = {MODE_WALKING: 1500, MODE_RIDING: 4800}
"""本层实际使用的检索半径，即 `R_MAX × RADIUS_FACTOR` 的取整改死值（步行 1500 m /
骑行 4800 m）。写成常量而不是留给运行时乘法，是为了让「骑行没超 10 km 上限」可断言。"""

PLACE_RADIUS_MAX_M = 10000
"""FAQ 2548：圆形检索半径最大 10 km；超过会**退化为城市范围检索**（与 `radius_limit`
是两个独立的坑，必须同时防）。"""

PAGE_SIZE = 10
PAGE_NUM = 1
"""🔴 写死不翻页。`page_size` 官方三处自相矛盾（3.0 文档「10–20 最大 20」/
字段注释「单次最多 10 条」/ FAQ 2542「最多只支持召回 10 条」）→ 按保守值 10
（PRD §4.3 第 292 行、笔记 §2.3）。**一律不翻页**：单类别最多 15 页，
翻一次页就吃掉当日大半配额（100 次/日的个人档）。"""

TOTAL_CAP = 150
"""返回体 `total` 的官方上限（FAQ 2534 / 2543）。圈内计数用它，明细表只有第一页 10 条。"""

COORD_TYPE_BD09LL_NUMERIC = 3
"""🔴 地点检索的 `coord_type` 是**数字** 3（笔记 §6.2 / R8）。
逆地理是 `coordtype`（无下划线）+ 字符串 `bd09ll`；路线规划是 `coord_type` + 字符串
`bd09ll`。三种写法只差一个字符或一个引号，写错**不报错**、只会让点位静默偏移。"""

MAX_QUERY_KEYWORDS = 10
"""`$` 并集的关键字上限（笔记 §2.3）。超出时客户端抛 `status=2`，本层在词表自检就挡住。"""

PLACE_CALLS_PER_ANALYSIS = 8
"""单次分析地点检索出网硬上限：6 维度 + 1 便民参考 + 1 冗余（PRD §4.3 第 294 行）。
个人档该服务 100 次/日，留 20 次给 `/api/suggest` 与人工调试。"""

EMPTY_REASON_NO_RESULT = "检索无结果"
"""PRD §4.3 第 293 行：空结果是**数据结论**不是异常，文案照原文。"""

NOTE_FAQ_2536_FILTERING = (
    "计数口径说明：百度 FAQ 2536 明确地点检索接口会出于数据保护过滤掉部分数据，"
    "因此本报告的圈内计数可能被**系统性低估**。这是官方预期行为，不是本项目缺陷；"
    "「0 个」应读作「检索未见」，而非「绝对没有」。"
)
NOTE_NO_PAGINATION = (
    f"明细表只取第一页（`page_size={PAGE_SIZE}`、`page_num={PAGE_NUM}`，一律不翻页："
    f"翻页一次即吃掉当日大半配额）；圈内计数取返回体 `total`（官方上限 {TOTAL_CAP}）。"
    "两者口径不同，不可互相替换。"
)
NOTE_RADIUS_LIMIT = (
    f"检索半径 {RADIUS_BY_MODE_M[MODE_WALKING]} m（步行）/ "
    f"{RADIUS_BY_MODE_M[MODE_RIDING]} m（骑行）＝ R_max × {RADIUS_FACTOR}，"
    "并显式携带 `radius_limit=true`：官方警告半径跨越城市边界时会退化为城市范围检索"
    "（FAQ 2544），口径与「圈内计数」完全不同。"
)

# --------------------------------------------------------------------------------------
# 官方分类词表与映射（笔记 §8.5 / Q1-15；命题原文 → 官方分类）
# --------------------------------------------------------------------------------------
BLIND_TRIO_SUBCATS: tuple[str, ...] = ("market", "pharmacy", "primary_school")
"""FR-221 主判定的三类设施（`购物 > 市场` / `医疗 > 药店` / `教育培训 > 小学`）。
⚠️ 是三个 **subcat** 而不是三个维度：药店属于 medical 维度。`services/coverage.py`
（另一泳道）按这个元组做 1 km 命中判定，FR-224 的「任一缺失」也读同一份数据。"""

PRD_V1_TERM_REPLACEMENTS: dict[str, str | None] = {
    # 命题原文 / PRD v1.0 用词 -> 官方 POI 分类名（None = 官方无对应项，已删除该词）
    "菜市场": "市场",
    "农贸市场": "市场",
    "养老院": "疗养院",
    "社区卫生服务中心": "诊所",
    "社区服务中心": None,
    "快递驿站": None,
    "理发店": None,
    "银行网点": "银行",
}
"""README（FR-407）要照这张表向评审解释「命题写菜市场、代码里找的是市场」。"""

BANNED_QUERY_TERMS = frozenset(PRD_V1_TERM_REPLACEMENTS)
"""🔴 绝不能出现在任何 `query` 里的词。命题原文的「菜市场」太有诱惑力，
所以它必须被机器盯着而不是靠人记得。"""

OFFICIAL_POI_PATHS: frozenset[str] = frozenset(
    {
        "医疗 > 药店",
        "医疗 > 综合医院",
        "医疗 > 诊所",
        "医疗 > 疗养院",
        "教育培训 > 小学",
        "教育培训 > 幼儿园",
        "购物 > 市场",
        "购物 > 超市",
        "购物 > 便利店",
        "旅游景点 > 公园",
        "休闲娱乐 > 休闲广场",
        "运动健身 > 健身中心",
        "交通设施 > 公交车站",
        "交通设施 > 地铁站",
        "美食 > 中餐厅",
        "美食 > 小吃快餐店",
        "金融 > 银行",
        "金融 > ATM",
        "生活服务 > 维修点",
    }
)
"""本词表用到的**全部**官方分类路径（一级 > 二级），逐字对照笔记 §8.5 的官方表抄录。
词表自检的判据就是这张表：query 里每个词都必须是这里某个路径的二级名。"""

OFFICIAL_TERMS: frozenset[str] = frozenset(path.split(" > ")[1] for path in OFFICIAL_POI_PATHS)
"""官方二级分类名的集合（query 词的唯一合法值域）。"""

SUBCAT_OFFICIAL_PATH: dict[str, str] = {
    "pharmacy": "医疗 > 药店",
    "hospital": "医疗 > 综合医院",
    "clinic": "医疗 > 诊所",
    "sanatorium": "医疗 > 疗养院",
    "primary_school": "教育培训 > 小学",
    "kindergarten": "教育培训 > 幼儿园",
    "market": "购物 > 市场",
    "supermarket": "购物 > 超市",
    "convenience": "购物 > 便利店",
    "park": "旅游景点 > 公园",
    "plaza": "休闲娱乐 > 休闲广场",
    "fitness": "运动健身 > 健身中心",
    "bus_stop": "交通设施 > 公交车站",
    "metro_station": "交通设施 > 地铁站",
    "restaurant": "美食 > 中餐厅",
    "snack": "美食 > 小吃快餐店",
    "bank": "金融 > 银行",
    "atm": "金融 > ATM",
    "repair": "生活服务 > 维修点",
}

SUBCAT_LABEL_CN: dict[str, str] = {
    subcat: path.split(" > ")[1] for subcat, path in SUBCAT_OFFICIAL_PATH.items()
}

SUBCAT_BY_OFFICIAL_TERM: dict[str, str] = {term: subcat for subcat, term in SUBCAT_LABEL_CN.items()}
"""官方分类名 → 英文 subcat 键（**跨泳道契约的唯一归一表**）。"""

SUBCAT_RESPONSE_ALIASES: dict[str, str] = {
    # ⬇️ 只用于**响应体归一**，绝不允许进 query（这些写法不在官方分类表的二级名里）。
    # 官方 `tag` / `classified_poi_tag` 偶见更细或更早的写法，匹配它们是纯增益；
    # 但把它们拿去检索就会重演「菜市场」那个错——检索静默为空、维度永远 0。
    "药房": "pharmacy",
    "药店": "pharmacy",
    "综合医院": "hospital",
    "三级医院": "hospital",
    "诊所": "clinic",
    "疗养院": "sanatorium",
    "干休所": "sanatorium",
    "小学": "primary_school",
    "幼儿园": "kindergarten",
    "市场": "market",
    "集贸市场": "market",
    "超市": "supermarket",
    "便利店": "convenience",
    "公园": "park",
    "休闲广场": "plaza",
    "广场": "plaza",
    "健身中心": "fitness",
    "公交车站": "bus_stop",
    "地铁站": "metro_station",
    "中餐厅": "restaurant",
    "小吃快餐店": "snack",
    "快餐": "snack",
    "银行": "bank",
    "atm": "atm",
    "维修点": "repair",
}

SUBCAT_TOKEN_SEPARATORS = ("|", "；", ";", "，", ",", ">", "、", "/", " ", "\n", "\t")
"""`classified_poi_tag` 形如 `医疗 | 药店`，`tag` 形如 `药店`；两种来源都先按这些
分隔符切片，再逐片查表——所以 `normalize_subcat()` 对两种字段同样生效（FR-214）。"""

SUBCAT_FALLBACK = "other"
"""归一不上时的取值：保留原始 `tag` 文本（契约里的 `tag` 字段），subcat 记 `other`，
让明细表看得见、盲区判定不会因为一个陌生写法就把它当成「三类皆无」。"""


# --------------------------------------------------------------------------------------
# 维度表（PRD §4.3 表格逐字实现；权重不改）
# --------------------------------------------------------------------------------------
def _dimension(
    *,
    key: str,
    label: str,
    weight: float,
    scored: bool,
    terms: tuple[str, ...],
    threshold_count: int,
    threshold_minutes: float,
    threshold_subcat: str | None = None,
) -> dict[str, Any]:
    subcats = [SUBCAT_BY_OFFICIAL_TERM[term] for term in terms]
    return {
        "key": key,
        "label": label,
        "weight": weight,
        "scored": scored,
        "query": "$".join(terms),
        "words": list(terms),
        "subcats": subcats,
        "official_paths": [SUBCAT_OFFICIAL_PATH[subcat] for subcat in subcats],
        "threshold_count": threshold_count,
        "threshold_minutes": threshold_minutes,
        "threshold_subcat": threshold_subcat,
        # 🔴 一个维度只用一次请求（PRD §4.3 第 276 行）。这个字段不是装饰：
        # `wordlist_problems()` 断言它恒为 1，谁改成按别名拆请求就会在 CI 里红。
        "request_count": 1,
        "page_size": PAGE_SIZE,
        "page_num": PAGE_NUM,
    }


DIMENSIONS: dict[str, dict[str, Any]] = {
    "medical": _dimension(
        key="medical",
        label="基础医疗",
        weight=0.20,
        scored=True,
        terms=("药店", "综合医院", "诊所"),
        threshold_count=2,
        threshold_minutes=8.0,
    ),
    "education": _dimension(
        key="education",
        label="教育",
        weight=0.15,
        scored=True,
        terms=("小学", "幼儿园"),
        threshold_count=1,
        threshold_minutes=12.0,
    ),
    "market": _dimension(
        key="market",
        label="购物市场",
        weight=0.20,
        scored=True,
        terms=("市场", "超市", "便利店"),
        threshold_count=3,
        threshold_minutes=8.0,
    ),
    "leisure": _dimension(
        key="leisure",
        label="养老休闲",
        weight=0.15,
        scored=True,
        terms=("公园", "休闲广场", "健身中心", "疗养院"),
        threshold_count=1,
        threshold_minutes=10.0,
    ),
    "transit": _dimension(
        key="transit",
        label="公共交通",
        weight=0.15,
        scored=True,
        terms=("公交车站", "地铁站"),
        threshold_count=2,
        threshold_minutes=5.0,
        # ⚠️ PRD 原文阈值写的是「**公交车站** ≥ 2 个」而不是「本维度 ≥ 2 个」：
        # 只看维度计数会让「2 个地铁站、0 个公交站」误判为达标。`threshold_subcat`
        # 由 M4 评分层消费（`services/score.py`，另一泳道），本层只如实带出。
        threshold_subcat="bus_stop",
    ),
    "food": _dimension(
        key="food",
        label="餐饮服务",
        weight=0.10,
        scored=True,
        terms=("中餐厅", "小吃快餐店"),
        threshold_count=5,
        threshold_minutes=10.0,
    ),
    "service_ref": _dimension(
        key="service_ref",
        label="便民服务（参考项）",
        weight=0.0,
        scored=False,
        terms=("银行", "ATM", "维修点"),
        threshold_count=0,
        threshold_minutes=0.0,
    ),
}
"""七项 = 六个计分维度 + 便民参考项（`scored=False`，不计分、只列明细表）。"""

DIMENSION_ORDER: tuple[str, ...] = (
    "medical",
    "education",
    "market",
    "leisure",
    "transit",
    "food",
    "service_ref",
)

ALL_DIMENSIONS: tuple[str, ...] = DIMENSION_ORDER
"""单次分析要跑的**全部**维度键（含参考项）——`place_calls` 的分母就是它的长度 7。"""

SCORED_DIMENSIONS: tuple[str, ...] = tuple(
    key for key in DIMENSION_ORDER if DIMENSIONS[key]["scored"]
)

SCORED_WEIGHT_SUM = 0.95
"""⚠️ 六个计分维度权重合计 **95 %（0.20+0.15+0.20+0.15+0.15+0.10），不是 100 %**。

这是 PRD §4.3 的表与 §4.5 的评分口径之间**真实存在的缺口**，不是本文件的笔误。
🔴 本层**不擅自归一化**（不除以 0.95、也不把余下 5 % 塞给 service_ref）：
归一与否会改变每一个维度的最终得分，属于 **M4 评分层的产品裁决**，
写死在这里会让评审读到的分数与 PRD §4.3 的原表对不上。
`wordlist_problems()` 把「合计必须正好是 0.95」当断言钉住：
将来 PRD 澄清成 100 % 时，改这个常量会让自检立刻要求同步改权重表，
而不是让缺口静默地飘在两个文件里。"""


# --------------------------------------------------------------------------------------
# 查询与归一
# --------------------------------------------------------------------------------------
def dimension(key: str) -> dict[str, Any]:
    """按维度键取词表项（返回的是模块级共享字典，调用方**只读**）。"""
    spec = DIMENSIONS.get(key)
    if spec is None:
        raise WordlistError(f"未知维度 {key!r}；可用：{', '.join(DIMENSION_ORDER)}")
    return spec


def dimensions_for(keys: list[str] | tuple[str, ...] | None) -> list[dict[str, Any]]:
    """按给定顺序取维度项；`keys=None` → 全七项默认顺序（未知键抛 `WordlistError`）。"""
    if keys is None:
        return [DIMENSIONS[key] for key in DIMENSION_ORDER]
    return [dimension(str(key)) for key in keys]


def query_words(query: str) -> list[str]:
    """`"药店$综合医院$诊所"` → `["药店", "综合医院", "诊所"]`（空片段丢掉）。"""
    return [word for word in (item.strip() for item in (query or "").split("$")) if word]


def is_official_term(term: str) -> bool:
    """该词是否是官方 POI 分类表里的二级名（**query 词的合法性判据**）。"""
    return term in OFFICIAL_TERMS


def search_radius_for_mode(mode: str, override: int | None = None) -> int:
    """检索半径（米）：默认 `R_max × 1.2` 的取整值，`override` 只允许收紧到官方 10 km 内。"""
    radius = RADIUS_BY_MODE_M.get(mode)
    if radius is None:
        raise WordlistError(f"未知出行方式 {mode!r}；可用：{', '.join(TRANSPORT_MODES)}")
    if override is None:
        return int(radius)
    requested = int(override)
    if requested <= 0:
        raise WordlistError("检索半径必须是正整数米。")
    return max(1, min(requested, PLACE_RADIUS_MAX_M))


def normalize_subcat(*texts: Any, category: str | None = None) -> str:
    """把 `tag` / `classified_poi_tag` / 官方 `type` 文本归一成契约里的英文 subcat 键。

    两种字段形态都要能吃（FR-214 的验收点）：`"药店"` 与 `"医疗 | 药店"` → `pharmacy`。
    为什么先长后短不猜：官方写法里 `综合医院` 含 `医院`、`小吃快餐店` 含 `快餐`，
    切片后**逐片整词比对**表，不做子串包含——包含匹配会把 `农贸市场` 判成 `market`
    以外的东西，也会让「菜市场」这种不存在的词被模糊成某个合法类别，
    那正是本要防的污染盲区结论的路径。

    匹配不上时返回 `SUBCAT_FALLBACK`（原始文本仍留在 `tag` 字段里，明细表看得见），
    不返回 `None`——盲区判定要的是「有没有命中三类设施」，`other` 天然是「未命中」。
    """
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        pieces = [text]
        for separator in SUBCAT_TOKEN_SEPARATORS:
            pieces = [piece for chunk in pieces for piece in chunk.split(separator)]
        for piece in pieces:
            token = piece.strip()
            if not token:
                continue
            exact = SUBCAT_BY_OFFICIAL_TERM.get(token)
            if exact is not None:
                return exact
            aliased = SUBCAT_RESPONSE_ALIASES.get(token) or SUBCAT_RESPONSE_ALIASES.get(
                token.lower()
            )
            if aliased is not None:
                return aliased
    if category is not None and category in DIMENSIONS:
        # 归一失败但维度明确：退到该维度的**第一个** subcat 会让盲区判定误命中，
        # 所以这里不猜，仍然回 `other`（保留 `category` 参数只为调用方写日志方便）。
        return SUBCAT_FALLBACK
    return SUBCAT_FALLBACK


def blind_trio_hits(hits: list[dict[str, Any]]) -> dict[str, int]:
    """FR-221 主判定的输入：三类设施各自的命中条数（按 subcat，不按维度）。

    本层只提供这个纯函数（`services/coverage.py` 会按 1 km 半径自己算），
    它存在的意义是让「药店属于 medical 维度、但判的是 subcat」这条口径有一份可测的实现。
    """
    counters = dict.fromkeys(BLIND_TRIO_SUBCATS, 0)
    for hit in hits:
        subcat = hit.get("subcat") if isinstance(hit, dict) else None
        if subcat in counters:
            counters[str(subcat)] += 1
    return counters


# --------------------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------------------
def wordlist_problems() -> list[str]:
    """词表体检：返回问题清单，空列表 = 通过。逐词校验，不含任何网络访问。"""
    problems: list[str] = []

    if len(ALL_DIMENSIONS) + 1 > PLACE_CALLS_PER_ANALYSIS:
        problems.append(
            f"维度数 {len(ALL_DIMENSIONS)} + 1 次冗余 > place_calls 上限 "
            f"{PLACE_CALLS_PER_ANALYSIS}（PRD §4.3 第 294 行）"
        )

    for key in DIMENSION_ORDER:
        spec = DIMENSIONS[key]
        words = query_words(str(spec["query"]))
        if words != list(spec["words"]):
            problems.append(f"{key}: `query` 与 `words` 不一致（{words} vs {spec['words']}）")
        if len(words) > MAX_QUERY_KEYWORDS:
            problems.append(f"{key}: query 词数 {len(words)} > 官方上限 {MAX_QUERY_KEYWORDS}")
        if not words:
            problems.append(f"{key}: query 为空")
        for word in words:
            if word in BANNED_QUERY_TERMS:
                problems.append(
                    f"{key}: query 含**官方分类表不存在**的词 {word!r}"
                    f"（应替换为 {PRD_V1_TERM_REPLACEMENTS[word]!r}，见 PRD §4.3 第 288 行）"
                )
            elif not is_official_term(word):
                problems.append(f"{key}: query 词 {word!r} 不在官方 POI 分类表内")
        if int(spec["request_count"]) != 1:
            problems.append(f"{key}: request_count 必须是 1（一个维度只用一次请求）")
        paths = list(spec["official_paths"])
        for word, path in zip(words, paths, strict=False):
            if path not in OFFICIAL_POI_PATHS:
                problems.append(f"{key}: 官方路径 {path!r} 未登记")
            elif path.split(" > ")[1] != word:
                problems.append(f"{key}: 路径 {path!r} 与 query 词 {word!r} 不对齐")
        for subcat in spec["subcats"]:
            if subcat not in SUBCAT_OFFICIAL_PATH:
                problems.append(f"{key}: subcat {subcat!r} 无官方路径映射")
        if int(spec["page_num"]) != 1:
            problems.append(f"{key}: page_num 必须恒为 1（一律不翻页）")
        if int(spec["page_size"]) != PAGE_SIZE:
            problems.append(f"{key}: page_size 必须是保守值 {PAGE_SIZE}")

    scored_sum = round(sum(DIMENSIONS[key]["weight"] for key in SCORED_DIMENSIONS), 4)
    if scored_sum != SCORED_WEIGHT_SUM:
        problems.append(
            f"六维权重合计 {scored_sum} != 断言常量 SCORED_WEIGHT_SUM {SCORED_WEIGHT_SUM}"
            "（PRD §4.3 的真实缺口；要改请先改 PRD，不要在本层静默归一）"
        )
    contradictions = [
        key
        for key in DIMENSION_ORDER
        if bool(DIMENSIONS[key]["scored"]) != (float(DIMENSIONS[key]["weight"]) > 0)
    ]
    if contradictions:
        problems.append(f"scored/weight 标记自相矛盾：{contradictions}")
    if DIMENSIONS["service_ref"]["scored"]:
        problems.append("service_ref 必须不计分（PRD §4.3「便民服务（参考项）」）")

    for subcat in BLIND_TRIO_SUBCATS:
        if subcat not in SUBCAT_OFFICIAL_PATH:
            problems.append(f"BLIND_TRIO_SUBCATS 里的 {subcat!r} 无官方路径映射")
    if "菜市场" not in BANNED_QUERY_TERMS:
        problems.append("「菜市场」必须留在黑名单里（FR-221 的 README 说明依赖它）")

    for alias, subcat in SUBCAT_RESPONSE_ALIASES.items():
        if subcat not in SUBCAT_OFFICIAL_PATH:
            problems.append(f"响应别名 {alias!r} 指向未知 subcat {subcat!r}")

    for mode in TRANSPORT_MODES:
        radius = RADIUS_BY_MODE_M[mode]
        if radius > PLACE_RADIUS_MAX_M:
            problems.append(f"{mode}: 检索半径 {radius} m 超官方上限 {PLACE_RADIUS_MAX_M} m")
        if round(R_MAX_BY_MODE_M[mode] * RADIUS_FACTOR) != radius:
            problems.append(f"{mode}: 检索半径应为 R_max×{RADIUS_FACTOR}={radius} m 的取值")
    if not isinstance(COORD_TYPE_BD09LL_NUMERIC, int):
        problems.append("地点检索的 coord_type 必须是**数字**（3），不是字符串")

    return problems


def assert_wordlist() -> None:
    """词表自检入口：有问题就抛 `WordlistError`（CI 与 `PoiSearchService.__init__` 都调它）。"""
    problems = wordlist_problems()
    if problems:
        raise WordlistError("POI 词表自检失败：\n  - " + "\n  - ".join(problems))
