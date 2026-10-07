"""缓存键与 TTL 的**唯一**口径：同一个业务查询必须永远算出同一个 key。

为什么单独一个模块（不塞进 `cache_store.py`）：FR-402 的验收（二次运行命中率 ≥ 95 %）
和 FR-406 的单测要求都落在「key 怎么算」上。键计算与 SQLite 实现分开，QA 不碰数据库
就能断言键稳定；更重要的是——一旦两处客户端各拼一份 `sha1`，就会出现「同一个查询两个
key」，表现是命中率莫名腰斩、配额被重复烧掉，而这恰恰是最难在现场发现的错。

四条硬规则都在下面：

1. 🔴 **AK / SN 绝不进 key，任何深度都不进**。条款 3.3.4 之外还有个实测坑：同账号下各 AK
   的配额是共用的（笔记 FAQ 2594），把 AK 折进 key 只会让同一份数据缓存两遍、命中率直接减半，
   零隔离收益（PRD §4.6 已据此删掉 v1.0 的 `ak_profile_tag` 分量）。「任何深度」是安全要求而不是
   美观要求：调用方常把参数打包成 `{"filter": {...}, "auth": {"ak": ...}}` 再递进来，只在顶层
   过滤等于把密钥原样写进缓存库和日志。
2. **空值一律剔除**（`None` / `""` / `[]` / `{}` / 清空后变空的嵌套结构）。「传空筛选」与「不传」
   是同一个查询，算成两个 key 就是在稀释 FR-402 的命中率。
3. **错误响应不缓存**（PRD §4.6 缓存表；`Settings.cache_error_ttl_s` 恒为 0）。
4. **POI 类 TTL 夹在 24 h 以内**（条款 3.3.4「生成或用于数据库」的限缩措施，笔记 §7.3）。

另外两件与「同一个查询必须算出同一个 key」同等重要的事：`set` / `frozenset` 的迭代顺序受
`PYTHONHASHSEED` 影响，所以集合一律排序 + 去重后再进 key（否则重启一次进程就是一个新 key）；
服务名要先经 `canonical_service_name()` 折叠别名，`reverse_geocoding` 与 `regeo` 是同一个端点，
写成两种样式却产生两个 key，表现正是 §2.2「命中率 ≥ 95 %」不达标。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from infra.config import CACHE_TTL_PLACE_HARD_MAX_S, Settings, get_settings
from infra.errors import OK_STATUS

__all__ = [
    "HARD_MAX_TTL_BY_SERVICE",
    "POI_SERVICES",
    "SERVICE_NAME_ALIASES",
    "SERVICE_TIER_ALIASES",
    "cache_key",
    "canonical_service_name",
    "drop_volatile_params",
    "extract_status",
    "hard_max_ttl_for_service",
    "is_cacheable_payload",
    "is_cacheable_status",
    "normalize_params",
    "service_tier",
    "short_key",
    "ttl_for_service",
]

_KEY_SEPARATOR = "|"
_COORD_DECIMALS = 6
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# 密钥类参数：进 key 会把敏感值带到缓存文件与日志里，且同账号配额共用、隔离无意义。
# 🔴 这批名字在**每一层**都过滤（见 `_normalize_value`），因为调用方会把鉴权字段塞进嵌套结构。
_SECRET_KEYS = frozenset(
    {"ak", "sn", "secret", "secret_key", "baidu_server_ak", "baidu_browser_ak"}
)
# 每次请求都会变、但不影响返回内容的参数：留着就等于每次都是新 key，命中率归零。
# 只在**顶层**过滤：噪声参数是客户端拼 query 串时带上的，嵌套结构里则是业务字段
# （例如某个 `filter` 里的 `width` 是真实分辨率参数），按名字剔掉会误伤语义。
_VOLATILE_KEYS = frozenset(
    {
        "callback",
        "charset",
        "header",
        "host",
        "log_id",
        "output",
        "request_id",
        "schema",
        "sdk",
        "timestamp",
        "trace",
        "uaddr",
        "udt",
        "width",
    }
)
# 顺序**不**承载语义的参数：类别集合换个顺序还是同一个查询。
# 反过来，`origins` / `destinations` / `location` 的顺序有语义（批量算路的矩阵行列、
# 经纬度顺序），绝不能排序——它们不在这里。
_ORDER_INSENSITIVE_KEYS = frozenset(
    {"categories", "extensions", "fields", "optionalextensions", "tags", "types"}
)

POI_SERVICES = frozenset({"place", "poi", "suggest"})
"""按 POI（地点检索）语义对待的服务名。`suggest` 与地点检索共用 100 次/日配额、
返回的也是 POI 内容，所以 TTL 必须和 POI 同档，不能按「地理编码 30 天」放行。"""

SERVICE_NAME_ALIASES: dict[str, str] = {
    # ⬇️ 只收「**同一个端点的不同写法**」。折叠的判据是「同样的参数必然同样的响应」：
    # 反过来，`place` 与 `suggest`、`route` 与 `routematrix`、`route_walking` 与 `route_riding`
    # 都是不同接口，参数表可能撞得一模一样而返回体完全不同——把它们折进同一个 key 会让
    # 联想结果被当成检索结果回放给用户，那是比命中率低一百倍严重的错。
    "reverse_geocoding": "regeo",
    "reverse_geocode": "regeo",
    "geocoding": "geocode",
    "geocoding_v3": "geocode",
    "place_search": "place",
    "suggestion": "suggest",
    "place_suggest": "suggest",
    "route_matrix": "routematrix",
}
"""服务名别名表（键归一用）。写法来自 `api/`、`services/` 与 `infra/ratelimit.logical_bucket_name`
里出现过的拼法；未登记的拼法保持原样，最坏结果只是多一个 key，不会串数据。"""

SERVICE_TIER_ALIASES: dict[str, str] = {
    # TTL 档位比 key 宽：这里可以按**配额类目**合并，因为两个接口同档时缓存时长本来就该一致。
    # `route_walking` / `route_riding` 是 `logical_bucket_name(service, mode)` 的产物，
    # 等时圈与 M2 的双模式服务层会直接把它们当服务名递给 `ttl_for_service()`；不登记就会落回
    # POI 档只存 24 h，而 PRD §4.6 给路线类的是 7 天——§2.2 的命中率高概率是这一项贡献的。
    "routematrix": "route",
    "direction": "route",
    "directionlite": "route",
    "matrix": "route",
    "polyline": "route",
    "route_walking": "route",
    "route_riding": "route",
    "poi": "place",
}
"""服务名 → TTL 档位名（`HARD_MAX_TTL_BY_SERVICE` 与 `Settings.cache_ttl_*` 的公共键）。"""

HARD_MAX_TTL_BY_SERVICE: dict[str, int] = {
    "geocode": 30 * 24 * 3600,
    "regeo": 30 * 24 * 3600,
    "place": CACHE_TTL_PLACE_HARD_MAX_S,
    "suggest": CACHE_TTL_PLACE_HARD_MAX_S,
    "poi": CACHE_TTL_PLACE_HARD_MAX_S,
    "route": 7 * 24 * 3600,
    "routematrix": 7 * 24 * 3600,
    "direction": 7 * 24 * 3600,
    "polyline": 7 * 24 * 3600,
    "isochrone": 7 * 24 * 3600,
}
"""各**档位**的 TTL 合规上限（秒），键是 `service_tier()` 的输出而不是任意服务拼法。

与 `Settings` 里的可配值是两回事：配置项决定「实际缓存多久」，这里决定「最多允许缓存
多久」。`config.py` 已经夹过 POI，这一层是第二道闸——因为调用方还能通过
`CacheStore.set(ttl_s=...)` 直接传任意秒数，那条路径不经过配置校验。
"""

_DROP = object()


def _format_number(value: float) -> str:
    """把数字统一成「四舍五入到 6 位小数、去掉多余零」的字符串。

    为什么要四舍五入：坐标来自前端输入框 / JSON 往返，`116.404` 与 `116.40400000001`
    是同一个查询，直接 `repr` 会算出两个 key。6 位小数在赤道约 0.11 m，远小于任何
    百度检索半径的分辨率，归一化不损失业务含义。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，必须在数字分支之前挡掉
        return "true" if value else "false"
    if not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    rounded = round(float(value), _COORD_DECIMALS) + 0.0  # `+ 0.0` 把 -0.0 折成 0.0
    return f"{rounded:.{_COORD_DECIMALS}f}".rstrip("0").rstrip(".")


def _coerce_scalar(value: Any) -> Any:
    """标量归一化。数字（含写成字符串的数字）折叠成同一种文本形态。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, str):
        text = value.strip()
        return _format_number(float(text)) if _NUMBER_RE.match(text) else text
    if value is None:
        return _DROP
    # 兜底：不认识的类型（Decimal / Path / 自定义枚举）按字符串处理。
    # 这里抛异常会违背「任一层失败不中断流程」（FR-403），宁可让 key 变粗也不崩。
    return str(value)


def _drop_if_empty(value: Any) -> Any:
    """空容器 / 空文本折算成 `_DROP`：「传空筛选」与「不传」是同一个查询。

    为什么连嵌套层一起清：`{"filter": {"tag": None}}` 清完里面就剩 `{}`，留着它就是把
    「不传 filter」和「传了一个空壳 filter」算成两个 key——同一份数据缓存两遍，
    FR-402 的 ≥ 95 % 命中率就是这么被吃掉的。`False` / `0` 不是空值，必须留着。
    """
    if value is _DROP or value is None:
        return _DROP
    if isinstance(value, (str, list, tuple, set, frozenset, dict)):
        return value if value else _DROP
    return value


def _sort_token(item: Any) -> str:
    """给集合元素一个**与哈希无关**的排序依据。

    为什么不用 `str(item)`：归一化后的元素可能是 dict / list，`str()` 依赖插入顺序，
    跨进程不稳定；`json.dumps(sort_keys=True)` 才是同一份数据的同一段文本。
    """
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_sequence(value: Any, *, sort_sequences: bool, unique: bool) -> Any:
    """序列归一化：逐项递归 → （可选）排序 → （可选）去重 → 空序列折成 `_DROP`。

    `unique=True` 只给 `set` / `frozenset` 用：集合本身语义就无序、不重复，而它的迭代顺序受
    `PYTHONHASHSEED` 影响，QA 实测 4 个种子产出 4 个 key——重启一次进程就是一个新键，
    等于把 §2.2「命中率 ≥ 95 %」这条指标打掉。列表必须保留顺序（`origins` 的次序就是矩阵的
    行次），所以排序与去重按调用点分别开关。
    """
    items = [
        item
        for item in (_normalize_value(element, sort_sequences=sort_sequences) for element in value)
        if item is not _DROP
    ]
    if unique:
        seen: dict[str, Any] = {}
        for item in items:
            seen.setdefault(_sort_token(item), item)
        items = list(seen.values())
    # 列表只在「顺序不承载语义」且元素全是文本时才排序（混合类型排序会 TypeError，
    # 而且顺序可能就是业务语义）；集合则无条件排序——无序是它的定义，且已按文本键去重，必定可比。
    if items and (unique or (sort_sequences and all(isinstance(item, str) for item in items))):
        items = sorted(items, key=_sort_token)
    return _drop_if_empty(items)


def _normalize_value(value: Any, *, sort_sequences: bool) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            lowered = key.lower()
            if not key or lowered in _SECRET_KEYS:
                # 🔴 密钥过滤必须在每一层生效：只在顶层过滤等于放任
                # `{"auth": {"ak": "..."}}` 把 AK 写进缓存键，
                # 再顺着 `normalize_params()` 的文本写进日志。
                continue
            item = _normalize_value(raw_value, sort_sequences=lowered in _ORDER_INSENSITIVE_KEYS)
            if item is not _DROP:
                result[key] = item
        return _drop_if_empty(result)
    if isinstance(value, (set, frozenset)):
        return _normalize_sequence(value, sort_sequences=False, unique=True)
    if isinstance(value, (list, tuple)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        return _normalize_sequence(value, sort_sequences=sort_sequences, unique=False)
    return _drop_if_empty(_coerce_scalar(value))


def canonical_service_name(service: str) -> str:
    """服务名 → 规范名：去空白、小写、`-` 归成 `_`，再折叠**同一端点**的别名。

    为什么需要：`cache_key` 把服务名当作键的一个分量，`regeo` 与 `reverse_geocoding` 是同一条
    `/reverse_geocoding/v3/`，不归一就会为同一次查询存两份缓存（笔记 §1 Q1-2）。
    别名表只收同一端点的写法，不同接口（`place` / `suggest`、`route` / `routematrix`）
    绝不折叠——它们的参数表可能相同而响应不同。
    """
    name = (service or "").strip().lower().replace("-", "_").replace(" ", "_")
    return SERVICE_NAME_ALIASES.get(name, name) or "unknown"


def service_tier(service: str) -> str:
    """服务名 → TTL 档位名。先折叠端点别名，再按配额类目并档（见两张别名表的注释）。"""
    name = canonical_service_name(service)
    if name == "unknown":
        return name
    return SERVICE_TIER_ALIASES.get(name, name)


def drop_volatile_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """丢掉「不进 key 也不影响结果」的参数（AK / SN / 时间戳 / 空值 / None / 空集合）。"""
    if not params:
        return {}
    cleaned: dict[str, Any] = {}
    for raw_key, raw_value in params.items():
        key = str(raw_key).strip()
        lowered = key.lower()
        if not key or lowered in _SECRET_KEYS or lowered in _VOLATILE_KEYS:
            continue
        value = _normalize_value(raw_value, sort_sequences=lowered in _ORDER_INSENSITIVE_KEYS)
        if value is _DROP:
            continue
        cleaned[key] = value
    return cleaned


def normalize_params(params: Mapping[str, Any] | None) -> str:
    """参数表 → 规范化文本（键排序、数字统一精度、密钥与噪声参数剔除）。"""
    return json.dumps(
        drop_volatile_params(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def cache_key(service: str, params: Mapping[str, Any] | None = None) -> str:
    """`sha1(canonical(service) + normalized(params))`，与 PRD §4.6 的键定义一一对应。

    用 SHA-1 不是安全用途（缓存键不需要抗碰撞强度到 SHA-2，也不涉密），
    选它只为跟 PRD 写的算法一致；`usedforsecurity=False` 让 FIPS 环境的 Python
    也不会在此抛错。

    服务名走 `canonical_service_name()`：签名不变（仍是 `(service, params)`），但
    `reverse_geocoding` 与 `regeo` 从此是同一个键。**注意**这会改变别名写法的旧键，
    升级后第一次运行必然 miss 并重新拉取，属于一次性成本，不是回归。
    """
    name = canonical_service_name(service)
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(name.encode("utf-8"))
    digest.update(_KEY_SEPARATOR.encode("ascii"))
    digest.update(normalize_params(params).encode("utf-8"))
    return digest.hexdigest()


def short_key(key: str) -> str:
    """日志里用的短 key；完整 sha1 打出来既刷屏又容易被当成凭据。"""
    return key[:12]


def hard_max_ttl_for_service(service: str) -> int:
    """某服务的合规 TTL 上限；未登记的服务按最保守的 POI 档处理。

    先过 `service_tier()`：别名（`reverse_geocoding`）与按模式拆出来的桶名（`route_walking`）
    本来就该和它们指向的档位同长，落回 POI 档只是把「写法不统一」伪装成「数据变得更快过期」。
    """
    return HARD_MAX_TTL_BY_SERVICE.get(service_tier(service), CACHE_TTL_PLACE_HARD_MAX_S)


def ttl_for_service(service: str, settings: Settings | None = None) -> int:
    """某服务实际使用的 TTL（秒）＝ 配置值再夹一道合规上限。

    默认值来自 `Settings.cache_ttl_*`：POI 类是 `CACHE_TTL_PLACE_S`
    （`infra/config.py` 里等于 `CACHE_TTL_PLACE_HARD_MAX_S` = 86400 s = 24 h，
    写更大会被 model validator 夹回并记进 `config_notes`）；路线类 `CACHE_TTL_ROUTE_S`，
    地理编码类 `CACHE_TTL_GEOCODE_S` / `CACHE_TTL_REGEO_S`，等时圈 `CACHE_TTL_ISOCHRONE_S`。

    查表键是 `service_tier()` 的输出，因此表里只需登记**档位名**：调用方递别名、
    按模式拆分的桶名或大小写混写都会落到正确的档位上。
    """
    cfg = settings or get_settings()
    tier = service_tier(service)
    configured: dict[str, int] = {
        "geocode": cfg.cache_ttl_geocode_s,
        "regeo": cfg.cache_ttl_regeo_s,
        "place": cfg.cache_ttl_place_s,
        "suggest": cfg.cache_ttl_place_s,
        "route": cfg.cache_ttl_route_s,
        "routematrix": cfg.cache_ttl_route_s,
        "direction": cfg.cache_ttl_route_s,
        "polyline": cfg.cache_ttl_route_s,
        "isochrone": cfg.cache_ttl_isochrone_s,
    }
    # 未登记的服务（含拼错的服务名）落回 POI 档：猜错方向时宁可缓存得短一点。
    ttl = configured.get(tier, cfg.cache_ttl_place_s)
    return max(0, min(int(ttl), hard_max_ttl_for_service(service)))


def extract_status(payload: Any) -> int | None:
    """从响应体里取百度 `status`；取不到（非 dict、非百度报文）返回 ``None``。"""
    if isinstance(payload, Mapping):
        raw = payload.get("status")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return int(raw)
    return None


def is_cacheable_status(status: int | None) -> bool:
    """只有成功报文进缓存。

    判据是**响应体内的 `status`** 而不是 HTTP 码——百度限流与配额都是 HTTP 200 + 非零
    `status`（笔记 §3.2）。把 `4` / `302` / `401` 这种失败存进缓存，等于把当天的降级
    结论钉死成「事实」，比不缓存糟糕得多。
    """
    return status is None or status == OK_STATUS


def is_cacheable_payload(payload: Any) -> bool:
    """按响应体判断该不该缓存（`None` / 空响应也拒掉，空结果会污染降级链）。"""
    if payload is None:
        return False
    if isinstance(payload, (str, bytes)) and not payload:
        return False
    return is_cacheable_status(extract_status(payload))
