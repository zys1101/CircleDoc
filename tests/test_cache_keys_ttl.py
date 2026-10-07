"""缓存键归一化与 TTL 合规上限（`infra/cache_keys.py`）的行为契约。

对应 M1 卡「CacheStore：SQLite 短 TTL 运行时缓存」的键/ TTL 口径部分——QA 不碰数据库
就能断言的那一半：

1. 键稳定：同一个业务查询永远算出同一个 key（参数顺序无关、数值统一精度、噪声剔除），
   否则 FR-402「二次运行命中率 ≥ 95 %」直接不成立。
2. 🔴 密钥不进 key：AK / SN 折进 key 会让同账号（共用配额）的同一份数据缓存两遍、
   命中率腰斩，还把敏感值带进缓存文件与日志。
3. TTL：按服务取值后再夹一道**合规上限**（POI 档 ≤ 24 h 是条款 3.3.4 的限缩措施），
   `Settings` 只夹了 place，这一层是 `CacheStore.set(ttl_s=...)` 那条旁路的第二道闸。
4. 可缓存性：判据是响应体内的 `status` 而不是 HTTP 码——限流/配额都是 HTTP 200 + 非零
   `status`，把降级结论存进缓存等于把当天故障钉死成「事实」。

2026-10-05 dev 按「谁实现谁修」处理 QA 登记的 4 条 infra 缺陷后，本文件同时钉住**修复后**的口径：
R1 空集合/空壳与 `""` 一样不进 key、R2 服务别名既折叠 key 也落到正确 TTL 档、
R3 密钥在任意深度都被剔除（🔴 安全）、R4 `set` 参数不随 `PYTHONHASHSEED` 变键
（跨进程断言靠起子进程复现）。原来钉「修复前错误行为」的两条用例（空集合保留、
`reverse_geocoding` 夹到 24 h）已按缺陷清单一并改写，改动理由写在各自 docstring 里。

真实出网：0——纯函数，不建缓存库、不发请求。
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from infra.cache_keys import (
    HARD_MAX_TTL_BY_SERVICE,
    POI_SERVICES,
    SERVICE_NAME_ALIASES,
    cache_key,
    canonical_service_name,
    drop_volatile_params,
    extract_status,
    hard_max_ttl_for_service,
    is_cacheable_payload,
    is_cacheable_status,
    normalize_params,
    service_tier,
    short_key,
    ttl_for_service,
)
from infra.config import CACHE_TTL_PLACE_HARD_MAX_S, Settings
from infra.errors import OK_STATUS

DAY = 24 * 3600


# ---------------------------------------------------------------------------
# 1. 键稳定：参数归一化
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic_sha1() -> None:
    key = cache_key("geocode", {"address": "北京市东城区故宫", "ret_coordtype": "bd09ll"})
    assert len(key) == 40
    assert all(c in "0123456789abcdef" for c in key)
    assert key == cache_key("geocode", {"address": "北京市东城区故宫", "ret_coordtype": "bd09ll"})
    # 任何一个实质参数变化都必须换 key，否则第二次查询会拿到上一次的坐标
    assert key != cache_key("geocode", {"address": "北京市西城区故宫", "ret_coordtype": "bd09ll"})
    assert key != cache_key("geocode", {"address": "北京市东城区故宫"})


def test_param_insertion_order_does_not_change_key() -> None:
    """字典书写顺序不承载语义——键排序归一，命中率不该受调用方代码风格影响。"""
    a = cache_key("place", {"query": "咖啡", "region": "北京", "radius": 1500})
    b = cache_key("place", {"radius": 1500, "region": "北京", "query": "咖啡"})
    assert a == b
    assert normalize_params({"b": 2, "a": 1}) == normalize_params({"a": 1, "b": 2})


def test_float_noise_collapses_to_six_decimals() -> None:
    """坐标来自前端输入框 / JSON 往返：`116.404` 与 `116.40400000001` 是同一个查询。
    6 位小数在赤道约 0.11 m，远小于任何检索半径的分辨率。"""
    variants = [
        {"location": 116.404, "lat": 39.915},
        {"location": 116.40400000001, "lat": 39.915000000002},
        {"location": 116.4040, "lat": 39.915},
        {"location": "116.404", "lat": "39.915"},
    ]
    keys = {cache_key("regeo", v) for v in variants}
    assert len(keys) == 1
    # 但真正不同的坐标不能折叠
    assert cache_key("regeo", {"location": 116.404}) != cache_key("regeo", {"location": 116.405})


def test_negative_zero_and_int_float_string_forms_unify() -> None:
    assert cache_key("route", {"radius": 0.0}) == cache_key("route", {"radius": -0.0})
    assert cache_key("route", {"radius": 4}) == cache_key("route", {"radius": 4.0})
    assert cache_key("route", {"radius": "4"}) == cache_key("route", {"radius": 4})
    assert cache_key("route", {"radius": "  4  "}) == cache_key("route", {"radius": 4})
    assert cache_key("route", {"radius": 4.5}) != cache_key("route", {"radius": 4})


def test_bool_is_not_collapsed_into_a_number() -> None:
    """`bool` 是 `int` 的子类：`True` 与 `1` 若折成同一个值，`extensions=1` 这类开关会串味。"""
    assert cache_key("place", {"flag": True}) != cache_key("place", {"flag": 1})
    assert cache_key("place", {"flag": True}) != cache_key("place", {"flag": False})
    assert '"flag":true' in normalize_params({"flag": True}).replace(" ", "")
    # 写成字符串的 "1" 与裸数字 1 才是同一个值
    assert cache_key("place", {"flag": "1"}) == cache_key("place", {"flag": 1})


def test_none_and_empty_values_are_dropped_from_key() -> None:
    """可选参数「传 None / 传空串 / 不传」必须是同一个查询——这是命中率的主要杀手。"""
    base = cache_key("place", {"query": "咖啡"})
    assert cache_key("place", {"query": "咖啡", "filter": None}) == base
    assert cache_key("place", {"query": "咖啡", "filter": ""}) == base
    assert cache_key("place", {"query": "咖啡", "city": "   "}) == base


def test_empty_collections_are_dropped_like_empty_strings() -> None:
    """R1 修复后的口径：`None` / `""` / `[]` / `{}` / 集合全都不进 key。

    「传空筛选」与「不传筛选」是同一个查询；修复前 `[]` 会留在 key 里，于是同一份数据
    按调用方写法存两遍，FR-402「二次运行命中率 ≥ 95 %」被静默稀释。
    """
    base = cache_key("place", {"query": "咖啡"})
    empties: list[Any] = [[], {}, set(), frozenset(), (), [None], {"tag": None}, {"": "x"}]
    empties.append({"ak": "SECRET-VALUE"})  # 密钥即使在嵌套里也不进 key（见 R3）
    for empty in empties:
        assert cache_key("place", {"query": "咖啡", "filter": empty}) == base, empty
    # 空壳嵌套也一并清掉：外层 `filter` 里只剩空 dict 时，等价于没传 filter
    assert cache_key("place", {"query": "咖啡", "filter": {"sub": []}}) == base
    # 但「非空」与「空」必须还能区分，否则真正的筛选会串味
    assert cache_key("place", {"query": "咖啡", "filter": ["美食"]}) != base
    assert cache_key("place", {"query": "咖啡", "filter": {"radius": 1500}}) != base
    # False / 0 不是空值：把它们当空剔除会让 `city_limit=false` 与「不传」混为一谈
    assert cache_key("place", {"query": "咖啡", "city_limit": False}) != base
    assert cache_key("place", {"query": "咖啡", "page": 0}) != base


def test_order_insensitive_collections_are_sorted_but_others_are_not() -> None:
    """类别集合换个顺序还是同一个查询；`origins` / `location` 的顺序承载矩阵行列与
    经纬度次序，绝不能排序。"""
    assert cache_key("place", {"categories": ["美食", "购物"]}) == cache_key(
        "place", {"categories": ["购物", "美食"]}
    )
    for key_name in ("tags", "types", "fields", "extensions", "optionalExtensions"):
        assert cache_key("place", {key_name: ["b", "a"]}) == cache_key(
            "place", {key_name: ["a", "b"]}
        )
    assert cache_key("routematrix", {"origins": ["A", "B"]}) != cache_key(
        "routematrix", {"origins": ["B", "A"]}
    )
    assert cache_key("regeo", {"location": "39.915,116.404"}) != cache_key(
        "regeo", {"location": "116.404,39.915"}
    )


def test_mixed_type_collection_is_left_unsorted_instead_of_raising() -> None:
    """混合类型（dict 与 str 同列）排序会 TypeError：宁可不排序（key 变粗）也不能崩
    ——「任一层失败不中断流程」（FR-403）。"""
    mixed_a = {"tags": [{"lng": 1}, "b"]}
    mixed_b = {"tags": ["b", {"lng": 1}]}
    assert cache_key("place", mixed_a) != cache_key("place", mixed_b)
    # 集合里的 None 仍被剔除，因此这两种写法等价
    assert cache_key("place", {"tags": ["a", None]}) == cache_key("place", {"tags": ["a"]})


# ---------------------------------------------------------------------------
# 1b. R4：`set` / `frozenset` 的参数不得随 PYTHONHASHSEED 变键
# ---------------------------------------------------------------------------


def test_set_params_are_insertion_order_insensitive_and_deduplicated() -> None:
    """R4：集合语义就是「无序 + 不重复」，所以 key 必须先按文本排序再去重，
    否则 `{a,b}` 与 `{b,a}`、`{a,a}` 与 `{a}` 会各算一个 key。"""
    assert cache_key("place", {"types": {"美食", "购物"}}) == cache_key(
        "place", {"types": {"购物", "美食"}}
    )
    # 集合本身就去重：`frozenset(("咖啡","咖啡"))` 与 `{"咖啡"}` 是同一个集合、同一个 key
    assert cache_key("place", {"types": frozenset(("咖啡", "咖啡"))}) == cache_key(
        "place", {"types": {"咖啡"}}
    )
    # 排序后的集合与「顺序无关键的有序列表」同键，调用方换容器不该换 key
    assert cache_key("place", {"tags": {"b", "a"}}) == cache_key("place", {"tags": ["a", "b"]})
    # 数字集合也不靠 `str()`：混合类型按稳定的文本表示排序，绝不抛 TypeError
    assert cache_key("place", {"page_set": {10, 2, 1}}) == cache_key(
        "place", {"page_set": {1, 2, 10}}
    )
    # ⚠️ 反面教训：`origins` 用 set 传就已经丢了矩阵行序（set 本身无序），
    # 归一成排序列表是唯一的确定解；想表达行序必须用 list，这里两者同键是刻意的。
    assert cache_key("routematrix", {"origins": {"A", "B"}}) == cache_key(
        "routematrix", {"origins": ["A", "B"]}
    )
    assert cache_key("routematrix", {"origins": ["B", "A"]}) != cache_key(
        "routematrix", {"origins": ["A", "B"]}
    )


@pytest.mark.parametrize("seed", ["0", "1", "982451653"])
def test_set_key_is_independent_of_python_hash_seed(seed: str) -> None:
    """真钉子的版本：`str.__hash__` 只在**进程之间**随种子变，所以必须起子进程复现。
    修复前 QA 实测 4 个种子 4 个 key（跨进程/重启各算各的键 ⇒ §2.2 命中率直接打掉）。

    零真实出网：子进程只 import `infra.cache_keys` 算一个 sha1，不发任何请求。
    """
    snippet = (
        "from infra.cache_keys import cache_key;"
        "print(cache_key('place',"
        " {'types': {'\\u7f8e\\u98df', '\\u8d2d\\u7269', '\\u6559\\u80b2'},"
        " 'tags': frozenset(('b', 'a', 'a')),"
        " 'nest': {'k': {'x', 'y'}}}))"
    )
    expected = cache_key(
        "place",
        {
            "types": {"美食", "购物", "教育"},
            "tags": frozenset(("a", "b", "a")),
            "nest": {"k": {"x", "y"}},
        },
    )
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected, f"seed={seed} 算出了另一个 key"


def test_unknown_types_fall_back_to_string_without_crashing() -> None:
    """Decimal / 自定义对象走 `str()` 兜底：抛异常会违背「不中断流程」。"""
    assert cache_key("place", {"radius": Decimal("1.5")}) == cache_key("place", {"radius": "1.5"})

    class _Odd:
        def __str__(self) -> str:
            return "odd-value"

    assert normalize_params({"x": _Odd()}) == '{"x":"odd-value"}'


def test_non_finite_numbers_are_stable_and_distinct() -> None:
    """nan / inf 不进 `repr` 也不抛：百度不会给这类值，但前端把空坐标算成 NaN 时
    缓存层不能因此崩掉。"""
    nan_key = cache_key("regeo", {"location": float("nan")})
    assert nan_key == cache_key("regeo", {"location": float("nan")})
    assert nan_key != cache_key("regeo", {"location": float("inf")})
    assert cache_key("regeo", {"location": float("-inf")}) != cache_key(
        "regeo", {"location": float("inf")}
    )


def test_service_name_is_a_key_component_and_is_normalised() -> None:
    assert cache_key("Geocode ", {"a": 1}) == cache_key("geocode", {"a": 1})
    assert cache_key("geocode", {"a": 1}) != cache_key("regeo", {"a": 1})
    # 空服务名归到 "unknown"，与直接写 unknown 一致（不留游离桶）
    assert cache_key("", {"a": 1}) == cache_key("unknown", {"a": 1})


def test_short_key_is_a_log_safe_prefix() -> None:
    key = cache_key("geocode", {"address": "故宫"})
    assert short_key(key) == key[:12]
    assert len(short_key(key)) == 12


# ---------------------------------------------------------------------------
# 2. 🔴 密钥与噪声参数剔除
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_key",
    ["ak", "sn", "secret", "secret_key", "baidu_server_ak", "baidu_browser_ak"],
)
def test_secret_params_never_reach_the_key(secret_key: str) -> None:
    params = {"address": "北京市东城区故宫", secret_key: "AK-SENSITIVE-VALUE"}
    cleaned = drop_volatile_params(params)
    assert cleaned == {"address": "北京市东城区故宫"}
    assert "AK-SENSITIVE-VALUE" not in normalize_params(params)
    # 换一把 AK 仍是同一个 key：同账号配额共用，带 AK 只会把同一份数据缓存两遍
    assert cache_key("geocode", params) == cache_key(
        "geocode", {"address": "北京市东城区故宫", secret_key: "OTHER-AK"}
    )


def test_secret_and_volatile_matching_is_case_insensitive() -> None:
    assert drop_volatile_params({"AK": "x", "Sn": "y", "TimeStamp": 1}) == {}
    # 大小写只用于「匹配」，保留原写法用于「实质参数」
    assert drop_volatile_params({"Address": "故宫"}) == {"Address": "故宫"}


def test_volatile_params_are_stripped_so_hits_are_reusable() -> None:
    """每次请求都变、又不影响返回内容的参数留着就等于每次都是新 key。"""
    noisy = {
        "query": "咖啡",
        "callback": "cb_123",
        "charset": "utf-8",
        "host": "api.map.baidu.com",
        "log_id": "9007199254740993",
        "output": "json",
        "request_id": "req-001",
        "schema": "http",
        "sdk": "3.0",
        "timestamp": 1_700_000_000,
        "trace": "on",
        "uaddr": "1.2.3.4",
        "udt": 12,
        "width": 640,
    }
    assert drop_volatile_params(noisy) == {"query": "咖啡"}
    assert cache_key("place", noisy) == cache_key("place", {"query": "咖啡"})


def test_blank_keys_are_discarded() -> None:
    assert drop_volatile_params({"": "x", "   ": "y", "ok": "z"}) == {"ok": "z"}


def test_nested_structures_are_normalised_too() -> None:
    """嵌套 dict 里的 None / 空键同样被剔除，数字照样归一化。"""
    cleaned = drop_volatile_params(
        {"filter": {"radius": 1500.0000001, "tag": None, "": "skip"}, "page": 2}
    )
    assert cleaned == {"filter": {"radius": "1500"}, "page": "2"}
    # 嵌套的顺序无关键（如 fields）也会被排序
    assert cache_key("place", {"nest": {"fields": ["b", "a"]}}) == cache_key(
        "place", {"nest": {"fields": ["a", "b"]}}
    )


@pytest.mark.parametrize(
    "secret_key",
    ["ak", "sn", "secret", "secret_key", "baidu_server_ak", "baidu_browser_ak", "AK", "Sn"],
)
@pytest.mark.parametrize(
    "wrap",
    [
        lambda v: {"auth": v},  # 一层
        lambda v: {"options": {"headers": v}},  # 两层
        lambda v: {"a": {"b": {"c": v}}},  # 三层
        lambda v: {"items": [{"meta": v, "name": "故宫"}]},  # 列表里的 dict
    ],
)
def test_secrets_are_stripped_at_every_depth(secret_key: str, wrap: Any) -> None:
    """R3（🔴 安全）：密钥过滤原来只在顶层生效，嵌套里的 `ak` / `sn` 会原样进 key。

    后果不是「命中率」而是「泄漏面」：缓存键写进 SQLite 的 `key` 列、`normalize_params()`
    的文本又常被日志层原样打出，AK 一旦落在里面就等于把服务端密钥发到日志与缓存文件。
    """
    secret = {"ak": "AK-SENSITIVE-VALUE"}
    params = {"query": "咖啡", **wrap(secret)}
    text = normalize_params(params)
    assert "AK-SENSITIVE-VALUE" not in text
    assert "AK-SENSITIVE-VALUE" not in str(drop_volatile_params(params))
    assert "AK-SENSITIVE-VALUE" not in cache_key("place", params)
    assert "AK-SENSITIVE-VALUE" not in short_key(cache_key("place", params))
    # 换一把 AK 不能换 key（同账号配额共用），实质参数仍要保留
    other = {"query": "咖啡", **wrap({secret_key: "ANOTHER-AK"})}
    assert cache_key("place", params) == cache_key("place", other)
    assert "咖啡" in text


def test_business_keys_that_merely_look_similar_are_kept() -> None:
    """只按**名字**剔除密钥，不看值：`rank` / `snake` / `task_id` 里含 `ak` 子串也必须留着，
    否则等于把业务参数误删成「同一个查询」——那是给错结论而不是少缓存。"""
    params = {"rank": 1, "snake_case": "keep", "task_id": "t1", "makeup": "keep"}
    cleaned = drop_volatile_params(params)
    assert cleaned == {"rank": "1", "snake_case": "keep", "task_id": "t1", "makeup": "keep"}
    # 噪声参数只在顶层剔除（嵌套里 `width` 是真实业务字段，见模块注释）
    assert drop_volatile_params({"nest": {"width": 640}}) == {"nest": {"width": "640"}}
    assert drop_volatile_params({"width": 640}) == {}


def test_empty_input_is_handled() -> None:
    assert drop_volatile_params(None) == {}
    assert drop_volatile_params({}) == {}
    assert normalize_params(None) == "{}"
    assert cache_key("place") == cache_key("place", {})


# ---------------------------------------------------------------------------
# 3. TTL：按服务取值 + 合规上限
# ---------------------------------------------------------------------------


def test_place_and_poi_family_ttl_is_capped_at_24h() -> None:
    """条款 3.3.4 的限缩措施：POI 类缓存不得超过 24 h（这是产品假设的上限，不是推荐值）。"""
    cfg = Settings()
    assert CACHE_TTL_PLACE_HARD_MAX_S == DAY
    for service in sorted(POI_SERVICES):
        assert hard_max_ttl_for_service(service) == DAY, service
        assert ttl_for_service(service, cfg) <= DAY, service
        assert ttl_for_service(service, cfg) == DAY, service


def test_suggest_does_not_inherit_geocode_30d_ttl() -> None:
    """suggest 与地点检索共用 100 次/日配额、返回的也是 POI 内容，不能按地理编码放行。"""
    cfg = Settings()
    assert ttl_for_service("suggest", cfg) == DAY
    assert ttl_for_service("suggest", cfg) < ttl_for_service("geocode", cfg)
    assert HARD_MAX_TTL_BY_SERVICE["suggest"] == CACHE_TTL_PLACE_HARD_MAX_S


def test_geocode_regeo_route_isochrone_default_tiers() -> None:
    cfg = Settings()
    assert ttl_for_service("geocode", cfg) == 30 * DAY
    assert ttl_for_service("regeo", cfg) == 30 * DAY
    assert ttl_for_service("route", cfg) == 7 * DAY
    assert ttl_for_service("isochrone", cfg) == 7 * DAY
    # 别名服务名要落到同一档，否则路线家族会各按 place 档只存 24 h
    for alias in ("routematrix", "direction", "polyline"):
        assert ttl_for_service(alias, cfg) == 7 * DAY, alias
    assert ttl_for_service("poi", cfg) == DAY


def test_reverse_geocoding_alias_resolves_to_regeo_tier() -> None:
    """R2 修复后的口径：`reverse_geocoding` 与 `regeo` 是同一条 `/reverse_geocoding/v3/`
    （笔记 §1 Q1-2），别名既要能拿到 30 天档，也必须算出同一个 key。

    修复前 `HARD_MAX_TTL_BY_SERVICE` 只登记 `regeo`，别名被兜底夹到 POI 档 24 h，
    且 `cache_key` 不归一服务名 ⇒ 同一次查询存两份缓存。
    """
    cfg = Settings()
    assert ttl_for_service("reverse_geocoding", cfg) == 30 * DAY
    assert hard_max_ttl_for_service("reverse_geocoding") == 30 * DAY
    assert ttl_for_service("reverse_geocode", cfg) == 30 * DAY
    assert ttl_for_service("regeo", cfg) == 30 * DAY
    # 大小写 / 连字符 / 首尾空白的写法差异同样折叠
    assert cache_key("REgeo", {"location": "39.915,116.404"}) == cache_key(
        "reverse-geocoding", {"location": "39.915,116.404"}
    )
    assert canonical_service_name("reverse-geocoding") == "regeo"
    assert canonical_service_name("Geocoding ") == "geocode"
    assert canonical_service_name("place_search") == "place"
    assert canonical_service_name("place-suggest") == "suggest"


def test_distinct_endpoints_are_never_folded_into_one_key() -> None:
    """别名表只收「同一个端点的不同写法」：`place` 与 `suggest`、`route` 与 `routematrix`、
    `route_walking` 与 `route_riding` 参数表可能完全相同而响应不同，折叠会让联想结果被
    当成检索结果回放——比命中率低严重得多，所以这里钉住「不许折叠」。"""
    same = {"query": "咖啡", "region": "北京"}
    assert cache_key("place", same) != cache_key("suggest", same)
    assert cache_key("route", same) != cache_key("routematrix", same)
    assert cache_key("route_walking", same) != cache_key("route_riding", same)
    # 未登记的拼法保持原样（最坏只多一个 key，不会串数据）
    assert canonical_service_name("mystery_service") == "mystery_service"


def test_every_registered_alias_folds_to_one_key() -> None:
    """别名表的自检：登记了就必须真的折叠，且别名与规范名的 TTL 档位一致。
    漏登记不会有报错、只会静默表现为「同一份数据缓存两遍」，所以逐条断言。"""
    cfg = Settings()
    for alias, canon in SERVICE_NAME_ALIASES.items():
        assert canonical_service_name(alias) == canon, alias
        assert cache_key(alias, {"q": "咖啡"}) == cache_key(canon, {"q": "咖啡"}), alias
        assert ttl_for_service(alias, cfg) == ttl_for_service(canon, cfg), alias
        assert hard_max_ttl_for_service(alias) == hard_max_ttl_for_service(canon), alias


def test_route_mode_bucket_names_get_route_ttl_tier() -> None:
    """`logical_bucket_name(service, mode)` 产出的 `route_walking` / `route_riding` 会被
    等时圈与双模式服务层直接当服务名递给 TTL：它们属路线类，PRD §4.6 给 7 天，
    落回 POI 档 24 h 会让 §2.2「命中率 ≥ 95 %」的主要贡献项提前过期。"""
    cfg = Settings()
    for name in ("route_walking", "route_riding", "routematrix", "direction", "polyline"):
        assert service_tier(name) == "route", name
        assert ttl_for_service(name, cfg) == 7 * DAY, name
        assert hard_max_ttl_for_service(name) == 7 * DAY, name
    # 档位归一不改变键的分量：桶名仍然各算各的 key（见上一条用例）
    assert ttl_for_service("poi", cfg) == DAY and service_tier("poi") == "place"


def test_ttl_alias_still_clamps_when_config_is_over_cap() -> None:
    """别名同样受合规上限这道第二道闸约束（`CacheStore.set(ttl_s=...)` 旁路不经过配置校验）。"""
    cfg = Settings(cache_ttl_regeo_s=90 * DAY)
    assert ttl_for_service("reverse_geocoding", cfg) == 30 * DAY
    assert ttl_for_service("route_walking", Settings(cache_ttl_route_s=30 * DAY)) == 7 * DAY


def test_over_cap_config_is_clamped_and_recorded_as_a_note() -> None:
    """配错方向也不能崩：夹回安全值并记一条 note 供 `/api/health` 回显。"""
    cfg = Settings(cache_ttl_place_s=3 * DAY)
    assert cfg.cache_ttl_place_s == DAY
    assert any("24 h" in note or "86400" in note for note in cfg.config_notes)
    assert ttl_for_service("place", cfg) == DAY
    # 错误响应永远不进缓存
    err_cfg = Settings(cache_error_ttl_s=60)
    assert err_cfg.cache_error_ttl_s == 0
    assert err_cfg.config_notes


def test_hard_max_is_a_second_gate_for_caller_supplied_config() -> None:
    """`Settings` 只夹 POI；地理编码/路线写超上限时靠 `hard_max_ttl_for_service()` 兜住——
    这条路径对应 `CacheStore.set(ttl_s=...)` 不经配置校验的旁路。"""
    cfg = Settings(cache_ttl_geocode_s=90 * DAY, cache_ttl_route_s=30 * DAY)
    assert cfg.cache_ttl_geocode_s == 90 * DAY  # 配置层没夹（只有 place 夹）
    assert ttl_for_service("geocode", cfg) == 30 * DAY
    assert ttl_for_service("route", cfg) == 7 * DAY


def test_unregistered_service_falls_back_to_the_most_conservative_tier() -> None:
    """未登记（含拼错）的服务名按 POI 档：猜错方向时宁可缓存得短一点。"""
    cfg = Settings()
    assert hard_max_ttl_for_service("mystery_service") == DAY
    assert ttl_for_service("mystery_service", cfg) == DAY
    assert hard_max_ttl_for_service("") == DAY


def test_ttl_never_goes_negative() -> None:
    assert ttl_for_service("place", Settings(cache_ttl_place_s=0)) == 0
    assert ttl_for_service("geocode", Settings(cache_ttl_geocode_s=-1)) == 0


# ---------------------------------------------------------------------------
# 4. status 可缓存性
# ---------------------------------------------------------------------------


def test_extract_status_reads_body_not_http_code() -> None:
    assert extract_status({"status": 0}) == 0
    assert extract_status({"status": "302"}) == 302
    assert extract_status({"status": -1}) == -1
    assert extract_status({"status": OK_STATUS}) == OK_STATUS
    assert extract_status({"message": "SUCCESS"}) is None
    assert extract_status({}) is None
    assert extract_status(None) is None
    assert extract_status("boom") is None
    assert extract_status({"status": None}) is None
    assert extract_status({"status": "abc"}) is None


def test_only_success_status_is_cacheable() -> None:
    assert is_cacheable_status(None) is True
    assert is_cacheable_status(OK_STATUS) is True
    # 鉴权 / 配额 / 并发全是 HTTP 200 + 非零 status：存进缓存等于把当天降级钉成「事实」
    for status in (4, 10, 203, 210, 211, 240, 302, 401, -1):
        assert is_cacheable_status(status) is False, status


@pytest.mark.parametrize(
    "payload,cacheable",
    [
        ({"status": 0, "result": {"location": {}}}, True),
        ({"status": "0"}, True),
        ({"status": 302, "message": "APP并发配额超限"}, False),
        ({"status": 4}, False),
        ({"status": 210}, False),
        (None, False),
        ("", False),
        (b"", False),
        ({"results": []}, True),
        ([], True),
    ],
)
def test_is_cacheable_payload_decision_table(payload: Any, cacheable: bool) -> None:
    assert is_cacheable_payload(payload) is cacheable


def test_non_baidu_payload_is_treated_as_success_not_rejected() -> None:
    """样例/合成数据没有 `status` 字段：取不到 status 视为 None ⇒ 可缓存，
    否则本地 sample 层永远存不进缓存（这条边界必须钉住，不是「漏判」）。"""
    assert extract_status({"name": "故宫"}) is None
    assert is_cacheable_status(extract_status({"name": "故宫"})) is True
    assert is_cacheable_payload({"name": "故宫"}) is True
