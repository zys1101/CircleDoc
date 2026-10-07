"""`GET /api/health` —— 启动探测 + 「探测结果即改默认数据模式」（PRD §4.6 / §6.2、笔记 §4.5）。

🔴 这个文件存在的意义是**避免最坏现场**：用户不知道自己的账号档位，进来反复点「一键体检」
反复收鉴权错误，看起来像程序坏了。所以本模块只做三件事：

1. **逐个探测**笔记 §4.5 列出的服务（含 `routematrix/v2/walking` 到底存不存在——PRD R1 的
   兜底「退回串行 direction」就取决于这一条），每个服务给出**可区分**的状态。
2. 把结论喂给 `infra.config.resolve_data_mode()`，并用 `Settings.apply_probe()` **回写生效
   模式**；响应里一定带 `data_mode` / `data_source` / 不可用的人话原因 / 前端横幅文案。
3. 零配置（无 `.env`、两个 AK 全空）必须 **HTTP 200 且 0 次真实出网**：没 AK 就直接判不可用，
   连 `BaiduClient` 都不构造——「忘了配 AK」不该变成每次健康检查都去撞一次百度。

三条贯穿全文件的纪律：

- **不在这里判状态码、也不在这里写文案。** 状态语义一律来自 `infra/errors.BAIDU_ERROR_TABLE`
  （`spec_for()` / `is_auth_failure()` / `is_daily_quota_failure()`），本文件只做「归类 + 排版」。
  看板上有条 bug 专盯「203 与 210 返回同一条文案」，再写一份码表就是给它续命。
- **探测一律不重试。** `errors.py` 把 `401` 标成可重试，那是**业务请求**的处置；健康探测每重试
  一次就是替用户多烧一次真实配额，而且「第一次 401、第二次成功」会被读成「服务健康」——探测要
  的是**第一次的真相**。这里用 `retry_max_attempts=1` 把客户端内部退避夹到下限（本任务不许改
  `infra/`，所以夹配置而不是改客户端），并用 `FakeClock` 让那唯一一次退避不消耗墙钟。
- **探测的开销单独记账**（`Metrics(label="health_probe")`）。混进请求 / 进程级账本会同时弄坏两条
  验收：§6.1「单次体检 `place_calls` ≤ 8」会被无关的健康检查污染，FR-402「二次运行
  `place_calls` = 0」更是必然失败。

探测结论默认按服务记忆（便宜服务 5 min、吃 100 次/日 的地点检索 6 h），因为 `Dockerfile` 里的
HEALTHCHECK 每 30 s 就打一次 `/api/health`——不记忆等于每小时烧 2 次地点检索配额。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from infra.baidu_client import TRANSPORT_UNKNOWN_STATUS, BaiduClient, BaiduResult
from infra.config import (
    SERVICE_BUCKET_MAP,
    EffectiveDataMode,
    ProbeResult,
    Settings,
    expected_data_source,
)
from infra.errors import (
    CircleDocError,
    ErrorSpec,
    is_auth_failure,
    is_daily_quota_failure,
    spec_for,
)
from infra.log import get_logger, log_fields
from infra.metrics import Metrics
from infra.ratelimit import FakeClock

logger = get_logger("circledoc.api.health")

router = APIRouter()

__all__ = [
    "MOCK_SCENARIOS",
    "PROBE_ITEMS",
    "health",
    "probe_usage_totals",
    "reset_probe_memory",
    "router",
    "run_probe",
]

# --------------------------------------------------------------------------------------
# 可调旋钮：全部走环境变量。**不给 infra/config.py 加字段**（那是别人的文件，会撞车）
# --------------------------------------------------------------------------------------
PROBE_TTL_S = 300.0
"""普通探测项的结论有效期（秒）。HEALTHCHECK 每 30 s 打一次，300 s 就是只放行 1/10。"""

PROBE_TTL_SCARCE_S = 6 * 3600.0
"""地点检索（个人档 100 次/日、与地址联想同池，笔记 §3.1 / FAQ 2540）的结论有效期。

为什么给到 6 h 而不是跟着 5 min：一次体检的 `place_calls` 预算才 8 次，让容器健康检查去消耗
当日上限的百分之一是纯工程开销；而「这个服务今天到底开没开通」本来就不会在一小时内变化——开通
要去控制台点（笔记 §9.2-③）。
"""

PROBE_TIMEOUT_S = 4.0
"""单次探测的连接 / 读超时。4 s × 至多 2 次尝试 < `Dockerfile` HEALTHCHECK 的 10 s。"""

PROBE_ADDRESS = "北京市东城区东长安街116号"
"""地理编码探测用的国内地址（≤128 字节，笔记 §2.1）。

国内是硬要求：境外地理编码对个人档为「-」，探测必然失败（笔记 §3.1、PRD §6.1 断言）。
"""

NETWORK_SENTINEL = -100
"""剧本里的「网络不可达」标记：从 transport 直接抛 `httpx.ConnectError`。"""

MOCK_SERVER_AK = "MOCK-NO-EGRESS-AK"
"""剧本模式用来过闸的占位 Key：🔴 不是真 Key，也永远不会被发出去（transport 是 mock）。

为什么要占位：`BaiduClient` 的闸门是 `live_enabled = bool(ak)`，零配置进程演剧本时 AK 是空串，
每个探测项都在出网前拿到 101，六个错误码的文案差异就一条也演不出来。看到这个名字出现在日志里
应当立刻明白「这是剧本」，所以名字本身带 `MOCK-NO-EGRESS`。
"""


def _env_number(name: str, default: float) -> float:
    """读一个正数环境变量；读不出来或非法就用默认值。

    为什么不报错：探测旋钮写错时「按默认值跑」比「健康检查 500」正确得多（PRD §4.6 保守默认）。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


@dataclass(frozen=True)
class ProbeItem:
    """一个探测项，即笔记 §4.5 表格的一行。"""

    key: str
    label: str
    bucket: str
    """计量 / 限流口径用的服务名（`infra/ratelimit.logical_bucket_name` 认识它）。"""

    path: str
    """实际请求路径；mock 剧本按它反查该回哪个 `status`。"""

    call: Callable[[BaiduClient], Awaitable[BaiduResult]] | None = None
    """``None`` = 不真发请求（配合 `expect_denied`）。"""

    scarce: bool = False
    """是否消耗 100 次/日 的稀缺配额（决定记忆时长）。"""

    daily_quota_personal: int | None = None
    """笔记 §3.1 的**个人开发者档位标准值**；自己账号的实际档位须登录控制台核实。"""

    official_hint: str | None = None
    """该项专有的官方结论——§4.5「失败码解读」列里有几条是码表覆盖不了的。"""

    expect_denied: bool = False
    """预期失败、且**故意不发请求**的项（多边形区域检索要商用授权，本项目不用）。"""

    counts: bool = True
    """是否计入 `resolve_data_mode()` 的输入。`expect_denied` 必须 False，否则「本项目压根不用
    的功能没开通」会把一份完全正常的配置误降成 `cache`。"""


def _center(client: BaiduClient) -> dict[str, float]:
    """首屏默认中心点 → `{lng, lat}` 字典。

    为什么传字典而不是 `(lng, lat)` 元组：`to_baidu_latlng()` 对二元组的约定是**纬度在前**，
    而 `Settings.default_center_lnglat` 是经度在前；顺序写反不会报错，只会让探测点跑到别的
    城市去（笔记 §6.2 第 3 条）。
    """
    lng, lat = client.settings.default_center_lnglat
    return {"lng": lng, "lat": lat}


def _destination(client: BaiduClient) -> dict[str, float]:
    """探测终点 = 中心点偏移约 1 km：步行十几分钟可达，不会撞进「超距离」那类分支。"""
    origin = _center(client)
    return {"lng": origin["lng"] + 0.012, "lat": origin["lat"] + 0.006}


_PROBE_ITEMS: tuple[ProbeItem, ...] = (
    ProbeItem(
        key="geocode",
        label="地理编码",
        bucket="geocode",
        path="/geocoding/v3/",
        call=lambda c: c.geocode(PROBE_ADDRESS),
        daily_quota_personal=5000,
        official_hint="笔记 §4.5：240→未勾选服务；210→IP 不在白名单；203→AK 类型错",
    ),
    ProbeItem(
        key="regeo",
        label="全球逆地理编码",
        bucket="regeo",
        path="/reverse_geocoding/v3/",
        call=lambda c: c.reverse_geocode(_center(c), radius=1000),
        daily_quota_personal=300,
        official_hint=(
            "笔记 §4.5：接口页原文——V3.0「鉴权失败」请到 控制台 → 查看应用 → 设置，"
            "确认是否已获得「全球逆地理编码服务」权限"
        ),
    ),
    ProbeItem(
        key="place",
        label="地点检索",
        bucket="place",
        path="/place/v2/search",
        # 单个常见关键词 + 小半径：探测只问「这个服务通不通」，不问数据质量。
        call=lambda c: c.search_place("便利店", location=_center(c), radius=3000),
        scarce=True,
        daily_quota_personal=100,
        official_hint="笔记 §4.5：4 / 302→当日配额耗尽（个人档只有 100 次/日）",
    ),
    ProbeItem(
        key="direction_walking",
        label="步行路线规划 v2",
        bucket="direction",
        path="/direction/v2/walking",
        call=lambda c: c.direction("walking", _center(c), _destination(c)),
        daily_quota_personal=5000,
        official_hint="笔记 §4.5：FAQ 2523 —— 服务端 AK 默认含路线规划",
    ),
    ProbeItem(
        key="direction_lite_walking",
        label="步行路线规划（轻量）",
        bucket="direction",
        path="/directionlite/v1/walking",
        call=lambda c: c.direction("walking", _center(c), _destination(c), lite=True, steps_info=0),
        daily_quota_personal=5000,
        official_hint=(
            "笔记 §2.7：轻量版 2018-11-14 上线，**在此之前注册的 AK 默认无使用权限**"
            "→ 必须在控制台勾选「步行路线规划（轻量）」"
        ),
    ),
    ProbeItem(
        key="routematrix_walking",
        label="批量算路（步行）",
        bucket="routematrix",
        path="/routematrix/v2/walking",
        # 1 origin × 1 destination：探测只折 1 个配额单位（官方按路线条数计，笔记 §2.8）。
        call=lambda c: c.route_matrix("walking", [_center(c)], [_destination(c)]),
        daily_quota_personal=5000,
        official_hint=(
            "笔记 §4.5：prepare 页原文「该服务需要使用服务端 AK 调用」。"
            "🔴 PRD R1 的兜底看的就是这一条——批量算路不可用时等时圈退回串行 direction"
        ),
    ),
    ProbeItem(
        key="place_polygon",
        label="多边形区域检索",
        bucket="place",
        path="/place/v2/polygon",
        call=None,
        scarce=True,
        official_hint=(
            "笔记 §4.5：**预期失败**（`status=9 Permission Denied`，需商用授权，个人档不可用）。"
            "本项目不使用多边形形态，`BaiduClient.search_place()` 因此刻意不暴露 `bounds`；"
            "这里不发消息去撞，只把结论写进报告"
        ),
        expect_denied=True,
        counts=False,
    ),
)

PROBE_ITEMS: tuple[ProbeItem, ...] = _PROBE_ITEMS
"""探测项清单（笔记 §4.5 的 7 行）。暴露给测试与看板，别在别处再抄一份。"""

_STATE_NAMES: dict[str, str] = {
    "available": "可用",
    "ak_type": "鉴权失败：Key 类型用错（服务端 / 浏览器端混用）",
    "ak_ip": "鉴权失败：出口 IP 不在白名单",
    "ak_sn": "鉴权失败：SN 签名不符",
    "service_disabled": "未开通该服务",
    "ak_invalid": "Key 无效或无此服务权限",
    "bad_parameter": "探测参数有误（我方问题）",
    "advanced_permission": "需要商用授权",
    "transient": "服务端抖动",
    "route_unreachable": "该方向不可达",
    "unknown": "未预期的返回",
    "daily_quota": "今日配额已用尽",
    "concurrency": "并发已满",
    "skipped_no_ak": "未配置服务端 Key（未发起请求）",
    "blocked_budget": "被本地出网预算拦停（未发起请求）",
    "blocked_breaker": "被本地熔断器拦停（未发起请求）",
    "not_probed": "未探测（本项目不使用）",
    "network_unreachable": "地图服务无法访问（出网不可用）",
    "probe_failed": "探测自身异常",
}
"""`state` → 中文状态名。键取自 `infra/errors` 的 `category`，不构成第二套码表。"""

# 「挑一条最能解释现状的原因」用的优先级，越靠后越接近「整台机器都不能用」。
_STATE_RANK: dict[str, int] = {
    "available": 0,
    "route_unreachable": 1,
    "transient": 2,
    "concurrency": 3,
    "bad_parameter": 4,
    "advanced_permission": 5,
    "not_probed": 6,
    "probe_failed": 7,
    "blocked_budget": 8,
    "blocked_breaker": 9,
    "service_disabled": 10,
    "ak_sn": 11,
    "ak_ip": 12,
    "ak_type": 13,
    "ak_invalid": 14,
    "daily_quota": 15,
    "network_unreachable": 16,
    "skipped_no_ak": 17,
    "unknown": 18,
}

MOCK_SCENARIOS: dict[str, dict[str, int]] = {
    # 键 = 探测项 key，`"*"` = 兜底。全部命中同一个 handler，所以六个错误码的差别只体现在
    # 响应体的 `status` 上——正好用来证明「六类文案两两不同」。
    "all_ok": {"*": 0},
    "ak_type": {"*": 203},
    "ip_not_whitelisted": {"*": 210},
    "sn_mismatch": {"*": 211},
    "service_disabled": {"*": 240},
    "quota_exhausted": {"*": 4},
    "quota_limited": {"*": 302},
    "concurrency_full": {"*": 401},
    "no_key": {"*": 101},
    "network_down": {"*": NETWORK_SENTINEL},
    # PRD §4.6 分支 5 / 6：Key 能用但**部分**服务没开通、部分服务今日额度耗尽 → `cache`
    "partial": {"*": 0, "place": 240, "direction_lite_walking": 4},
    # 只让两个服务今日配额耗尽（4 / 302），其余照常 → 验证「配额用尽」这条分支的 retry_after
    "quota_only": {"*": 0, "regeo": 4, "direction_lite_walking": 302},
    # 最坏现场：同一个 Key 上既有类型错也有白名单错 → 整台机器不能用 → `mock`
    "all_dead": {"geocode": 203, "*": 210},
}
"""`GET /api/health?probe=mock&scenario=<名字>`。真实出网次数恒为 0。"""

# 探测项 path → key 的反查表（不发请求的多边形项自然不在表里）。
_PATH_TO_KEY: dict[str, str] = {item.path: item.key for item in _PROBE_ITEMS if item.call}


# --------------------------------------------------------------------------------------
# 单个探测项 → 可区分状态
# --------------------------------------------------------------------------------------
def _base_report(item: ProbeItem) -> dict[str, Any]:
    """一个探测项报告的公共骨架（字段含义见 `_apply_spec`）。"""
    return {
        "key": item.key,
        "label": item.label,
        "bucket": item.bucket,
        "available": False,
        "state": "unknown",
        "state_name": _STATE_NAMES["unknown"],
        "request_sent": True,
        "transport_ok": True,
        "baidu_status": None,
        "official_name": None,
        "code": None,
        "category": None,
        "message": "",
        "technical_hint": None,
        "operator_hint": item.official_hint,
        "action": "report_only",
        "retryable": False,
        "retry_max_attempts": 0,
        "breaker": "none",
        "retry_after_s": None,
        "degrade_to_sample": True,
        "counts_toward_data_mode": item.counts,
        "attempts": 0,
        "http_requests": 0,
        "place_calls": 0,
        "from_cache": False,
        "from_memory": False,
        "flaky": False,
        "daily_quota_personal": item.daily_quota_personal,
    }


def _action_for(spec: ErrorSpec) -> str:
    """处置动作：只读 `ErrorSpec` 的字段，不在这里比较状态码。"""
    if spec.breaker.value == "quota_breaker":
        return "stop_realtime_until_midnight"
    if spec.breaker.value == "concurrency_breaker":
        return "backoff_and_retry"
    if spec.counts_as_unreachable:
        return "mark_unreachable_no_retry"
    if spec.retryable:
        return "retry_once_then_degrade"
    if spec.degrade_to_sample:
        return "degrade_no_retry"
    return "report_only"


def _apply_spec(report: dict[str, Any], spec: ErrorSpec, *, status: int | None) -> dict[str, Any]:
    """把 `ErrorSpec` 抄进报告——**文案与处置一律来自 `infra/errors.py`，这里一个字都不写**。"""
    report.update(
        {
            "state": spec.category,
            "state_name": _STATE_NAMES.get(spec.category, spec.official_name),
            "baidu_status": status,
            "official_name": spec.official_name,
            "code": spec.error_id,
            "category": spec.category,
            "message": spec.user_message,
            "technical_hint": spec.technical_hint,
            "action": _action_for(spec),
            "retryable": spec.retryable,
            "retry_max_attempts": spec.retry_max_attempts,
            "breaker": spec.breaker.value,
            "degrade_to_sample": spec.degrade_to_sample,
            "retry_after_s": _retry_after_for(spec),
        }
    )
    return report


def _retry_after_for(spec: ErrorSpec) -> int | None:
    """多久之后值得再试：配额类是**次日零时**，并发类是熔断固定存活期，其余不重试。"""
    if spec.breaker.value == "quota_breaker":
        return spec.breaker_expires_in_s()
    if spec.breaker.value == "concurrency_breaker":
        return spec.breaker_hold_s
    return None


def _spec_of_error(exc: BaseException) -> ErrorSpec:
    """从 typed error 里取官方语义；取不到落 `UNKNOWN_SPEC`（不自己编文案）。"""
    spec = getattr(exc, "spec", None)
    if isinstance(spec, ErrorSpec):
        return spec
    status = getattr(exc, "status", None)
    return spec_for(int(status) if isinstance(status, int) else TRANSPORT_UNKNOWN_STATUS)


def _detail_of(exc: BaseException) -> Mapping[str, Any]:
    detail = getattr(exc, "detail", None)
    return detail if isinstance(detail, Mapping) else {}


def _report_success(item: ProbeItem, result: BaiduResult) -> dict[str, Any]:
    """`status=0` → 服务可用（探测不关心返回的数据内容，只关心通没通）。"""
    report = _base_report(item)
    report.update(
        {
            "available": True,
            "state": "available",
            "state_name": _STATE_NAMES["available"],
            "message": f"{item.label}：可用。",
            "action": "use_realtime",
            "degrade_to_sample": False,
            "attempts": result.attempts,
            "http_requests": result.http_requests,
            "place_calls": result.place_calls,
            "from_cache": result.from_cache,
            "baidu_status": result.status,
            "code": "ok",
            "category": "ok",
            "official_name": "ok 正常",
        }
    )
    if result.unreachable:
        # 2001 / 7：探测点之间确实没路线，是**数据**；接口本身是通的，不能报成故障。
        spec = (
            _spec_of_error(result.error)
            if result.error is not None
            else spec_for(result.status if result.status is not None else TRANSPORT_UNKNOWN_STATUS)
        )
        report["note"] = "接口可用，本次探测的起终点无路线（官方信号，见 technical_hint）"
        report["baidu_status"] = result.status
        report["official_name"] = spec.official_name
        report["technical_hint"] = spec.technical_hint
    if result.attempts > 1:
        # 第一次就被限流、退避后才成功：仍是「可用」，但必须让看板看见抖动，
        # 否则「今天一直在 401 边缘」这种现场会被一次成功彻底掩盖。
        report["flaky"] = True
        report["note"] = f"探测在第 {result.attempts} 次尝试才成功，说明当时正在被限流"
    return report


def _report_blocked(item: ProbeItem, result: BaiduResult) -> dict[str, Any]:
    """本地拦停（出网预算守卫 / 熔断闸门）：**没发请求**，所以既不占配额也不该报故障。"""
    report = _base_report(item)
    report["request_sent"] = False
    report["transport_ok"] = False
    report["blocked_reason"] = result.blocked_reason
    if result.error is not None:
        # 熔断拦停挂着 typed error，文案仍走官方码表；`no_request_sent` 由 errors.py 标注
        spec = _spec_of_error(result.error)
        _apply_spec(report, spec, status=spec.status if spec.status >= 0 else None)
        report["state"] = "blocked_breaker"
        report["state_name"] = _STATE_NAMES["blocked_breaker"]
        report["message"] = f"未发起探测：{result.error.user_message}"
        hold = getattr(result.error, "breaker_hold_s", None)
        report["retry_after_s"] = int(hold) if isinstance(hold, int) else report["retry_after_s"]
        return report
    report["state"] = "blocked_budget"
    report["state_name"] = _STATE_NAMES["blocked_budget"]
    report["code"] = "local_budget_guard"
    report["category"] = "local_budget"
    report["message"] = (
        f"未发起探测：本地出网预算已用满（{result.blocked_reason or 'budget'}），"
        "本次不再为该服务发实时请求。"
    )
    report["technical_hint"] = "守卫阈值来自 MAX_*_PER_ANALYSIS（PRD §4.6 配额预算守护）"
    report["action"] = "degrade_no_retry"
    return report


def _report_error(item: ProbeItem, exc: BaseException) -> dict[str, Any]:
    """客户端抛出的 typed error → 状态 + 文案 + 处置，全部取自 `infra/errors.py`。"""
    report = _base_report(item)
    spec = _spec_of_error(exc)
    raw_status = getattr(exc, "status", None)
    status = int(raw_status) if isinstance(raw_status, int) else None
    _apply_spec(report, spec, status=status)
    detail = _detail_of(exc)
    report["message"] = exc.user_message if isinstance(exc, CircleDocError) else spec.user_message
    report["attempts"] = int(detail.get("attempts") or 1)
    if detail.get("no_request_sent"):
        report["request_sent"] = False
        report["transport_ok"] = False
    if (
        detail.get("transport_error")
        or detail.get("reason") == "status_missing_in_body"
        or spec.status == TRANSPORT_UNKNOWN_STATUS
    ):
        # 没拿到带 `status` 的响应体：DNS / 连接 / 超时 / 非 JSON 都算这一类。
        # 🔴 它**不是**百度的限流信号——官方码表里根本没有 HTTP 429（笔记 §3.2）。
        report["state"] = "network_unreachable"
        report["state_name"] = _STATE_NAMES["network_unreachable"]
        report["transport_ok"] = False
        report["code"] = "network_unreachable"
        report["category"] = "network"
        report["message"] = "地图服务无法访问（本机出网不可用或服务端无响应）。"
        report["technical_hint"] = (
            "检查网络与 BAIDU_ALLOWED_HOSTS；这类失败按 PRD §4.6 落 mock，"
            "不计入百度的任何错误类别，也不该被当成限流信号"
        )
        report["action"] = "degrade_no_retry"
        report["retry_after_s"] = None
    if spec.breaker.value == "concurrency_breaker" and report["request_sent"]:
        report["note"] = (
            "客户端按 errors.py 的处置做了 1 次退避重试（探测已把 retry_max_attempts 夹到 1）；"
            "探测本身不会重跑"
        )
    return report


def _report_skipped(item: ProbeItem) -> dict[str, Any]:
    """零配置：没配服务端 AK 就**直接判定不可用，一次请求都不发**。

    文案复用 `status=101` 那条（`errors.py` 的 `technical_hint` 里写明它就是「零配置启动的正常
    路径」），这样「未配置 Key」和「Key 参数不存在」在看板上说的是同一句话，不会出现第二套措辞。
    `baidu_status` 保持 `None`：我们没收到过任何响应，谎报一个真实状态码会误导排查。
    """
    report = _base_report(item)
    spec = spec_for(101)
    report.update(
        {
            "request_sent": False,
            "transport_ok": False,
            "state": "skipped_no_ak",
            "state_name": _STATE_NAMES["skipped_no_ak"],
            "code": spec.error_id,
            "category": spec.category,
            "official_name": spec.official_name,
            "message": spec.user_message,
            "technical_hint": spec.technical_hint,
            "operator_hint": item.official_hint,
            "action": "degrade_no_retry",
            "retryable": spec.retryable,
            "breaker": spec.breaker.value,
            "degrade_to_sample": spec.degrade_to_sample,
        }
    )
    return report


def _report_not_probed(item: ProbeItem) -> dict[str, Any]:
    """`expect_denied` 项：不发消息，只把官方结论写进报告（笔记 §4.5 最后一行）。"""
    report = _base_report(item)
    spec = spec_for(9)  # Permission Denied 高级权限校验失败
    report.update(
        {
            "request_sent": False,
            "state": "not_probed",
            "state_name": _STATE_NAMES["not_probed"],
            "code": spec.error_id,
            "category": spec.category,
            "official_name": spec.official_name,
            "message": spec.user_message + " 本项目不使用该形态。",
            "technical_hint": spec.technical_hint,
            "operator_hint": item.official_hint,
            "action": "not_used_by_project",
            "retry_after_s": None,
        }
    )
    return report


async def _probe_one(client: BaiduClient | None, item: ProbeItem) -> dict[str, Any]:
    """跑一个探测项。**任何异常都咽掉**：探测本身挂了不能把 `/api/health` 变成 500。"""
    if item.expect_denied or item.call is None:
        return _report_not_probed(item)
    if client is None:  # 零配置：连客户端都没建，也就没有任何出网的可能
        return _report_skipped(item)
    try:
        result = await item.call(client)
    except CircleDocError as exc:
        return _report_error(item, exc)
    except Exception as exc:  # pragma: no cover - 兜底路径：宁可报「探测失败」也不 500
        log_fields(
            logger,
            logging.WARNING,
            "health_probe_unexpected_error",
            item=item.key,
            error_type=type(exc).__name__,
        )
        report = _report_error(item, exc)
        report["state"] = "probe_failed"
        report["state_name"] = _STATE_NAMES["probe_failed"]
        report["error_type"] = type(exc).__name__
        return report
    if result.blocked:
        return _report_blocked(item, result)
    if result.ok:
        return _report_success(item, result)
    # 兜底形状：客户端理论上一律抛 typed error，真走到这里按未收录码处理
    status = result.status if result.status is not None else TRANSPORT_UNKNOWN_STATUS
    report = _base_report(item)
    _apply_spec(report, spec_for(int(status)), status=int(status))
    report["attempts"] = result.attempts
    return report


# --------------------------------------------------------------------------------------
# mock：`--mock` 友好的开关（不引入新依赖，只用已有的 httpx.MockTransport）
# --------------------------------------------------------------------------------------
def _scenario_status(script: Mapping[str, int], path: str) -> int:
    key = _PATH_TO_KEY.get(path, "")
    if key in script:
        return int(script[key])
    return int(script.get("*", 0))


def _success_body(path: str) -> dict[str, Any]:
    """`status=0` 时的响应体形状：只补「这一格确实有数据」所需的最小字段。

    为什么要按路径分形状：`BaiduClient.route_matrix()` 的 `ok` 判据是
    `completed_cells > 0`（读的是 `result[]`），只回 `{"status": 0}` 会被判成「没算出任何
    路线」，`all_ok` 剧本就演不成「全部可用」。批量算路的行形状照笔记 §2.8 原文
    （`{distance:{text,value}, duration:{text,value}}`，不返回 polyline），数值只是 mock 值。
    """
    if path.startswith("/routematrix/"):
        return {
            "status": 0,
            "message": "成功（mock）",
            "result": [
                {
                    "distance": {"text": "1.0公里", "value": 1000},
                    "duration": {"text": "13分钟", "value": 800},
                }
            ],
        }
    return {"status": 0, "message": "成功（mock）"}


def _mock_transport(script: Mapping[str, int]) -> httpx.MockTransport:
    """按剧本回一个响应体。

    为什么回 HTTP 200 而不是 5xx：百度的限流与配额就是 **HTTP 200 + body `status`**
    （笔记 §3.2）。mock 要是演成 HTTP 429，就等于给全队教了一套错的判据。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        status = _scenario_status(script, request.url.path)
        if status == NETWORK_SENTINEL:
            raise httpx.ConnectError("mock：本机出网不可用", request=request)
        if status == 0:
            return httpx.Response(200, json=_success_body(request.url.path))
        return httpx.Response(200, json={"status": status, "message": "mock"})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------------------
# 客户端装配
# --------------------------------------------------------------------------------------
def _probe_settings(settings: Settings, *, mocking: bool = False) -> Settings:
    """探测专用的配置副本：把重试夹到下限、在途压到 2；剧本模式下再给一个假 Key。

    为什么必须夹重试：`errors.py` 给 `401` 的动作是「退避重试 3 次」，那是业务请求的正确处置；探测
    照抄就会出现「第一次 401、第二次成功 → 报告说服务健康」。`BaiduClient` 内部有
    `max(1, retry_max_attempts)` 的下限，1 就是不改 `infra/` 前提下能做到的最接近「不重试」。

    🔴 为什么剧本模式要塞假 Key：`BaiduClient.live_enabled = bool(ak)`，零配置进程里 AK 是空串，
    闸门会在出网前抛 101，于是 `?probe=mock&scenario=ak_type` 演出来的永远只是「没配 Key」，
    六个错误码的文案差异一条都看不到。假 Key 只用于过闸——transport 是 `MockTransport`，
    任何字节都不会离开本机（自测里 socket 守卫记到 0 次出网尝试）。
    """
    update: dict[str, Any] = {"retry_max_attempts": 1, "baidu_max_concurrency": 2}
    if mocking and not settings.baidu_server_ak.strip():
        update["baidu_server_ak"] = MOCK_SERVER_AK
    return settings.model_copy(update=update)


@asynccontextmanager
async def _probe_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None,
    mocking: bool = False,
) -> Any:
    """探测客户端的上下文：负责把注入的 `httpx.AsyncClient` 关掉。

    为什么要自己关：`BaiduClient.aclose()` 只关**它自己建**的那个连接池（`_owns_client`），
    注入进来的归调用方管——不关就是每次健康检查泄漏一个 client，半小时后全是 warning。

    `FakeClock` 同时给 `clock` 与 `sleep`：唯一那次退避重试不消耗墙钟（HEALTHCHECK 只有 10 s
    超时），也不会踩到令牌桶「睡了但时钟没走」的报错。
    """
    timeout_s = _env_number("CIRCLEDOC_PROBE_TIMEOUT_S", PROBE_TIMEOUT_S)
    http = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout_s, connect=timeout_s),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        follow_redirects=False,
    )
    fake = FakeClock()
    client = BaiduClient(
        _probe_settings(settings, mocking=mocking),
        http_client=http,
        clock=fake.now,
        sleep=fake.sleep,
        metrics=Metrics(label="health_probe"),
        cache=None,  # 探测绝不走响应缓存：命中缓存的探测会把昨天的 203 当成今天的结论
    )
    try:
        yield client
    finally:
        await http.aclose()


async def _probe_all(
    client: BaiduClient | None, items: tuple[ProbeItem, ...] | list[ProbeItem]
) -> list[dict[str, Any]]:
    """并发探测给定项。

    为什么并发而不是串行：串行 6 项最坏 6 × 4 s = 24 s，会被 `Dockerfile` 的 HEALTHCHECK
    （10 s 超时）判成容器不健康。并发既受客户端在途闸门约束，也不绕过按服务分桶的令牌桶
    （每个服务本来只探一次，真正并发的只有不同桶）。
    """
    results = await asyncio.gather(*(_probe_one(client, item) for item in items))
    return [dict(report) for report in results]


# --------------------------------------------------------------------------------------
# 探测结果 → ProbeResult → 数据模式
# --------------------------------------------------------------------------------------
def build_probe_result(
    reports: list[dict[str, Any]],
    *,
    server_ak_present: bool,
    probe_error: str | None = None,
) -> ProbeResult:
    """把逐项报告压成 `infra.config.ProbeResult`（`resolve_data_mode()` 的唯一输入）。

    只统计 `counts_toward_data_mode` 的项：多边形区域检索是「预期失败且本项目不用」，把它算进
    分母会让一份永远正常的配置被误降成 `cache`。
    """
    counted = [r for r in reports if r.get("counts_toward_data_mode")]
    enabled = {str(r["key"]): bool(r.get("available")) for r in counted}
    quota_exhausted = tuple(
        str(r["key"])
        for r in counted
        if isinstance(r.get("baidu_status"), int) and is_daily_quota_failure(int(r["baidu_status"]))
    )
    auth_status: int | None = None
    for report in counted:
        status = report.get("baidu_status")
        if isinstance(status, int) and is_auth_failure(status):
            auth_status = status
            break
    # 「至少有一项真的拿到了带 status 的响应」才叫出网可达；全 210 也算可达——
    # 那是 Key 的问题，不是网线的问题，两者的默认模式不同（mock vs cache）。
    reachable = any(r.get("request_sent") and r.get("transport_ok") for r in counted)
    if not counted:
        reachable = False
    return ProbeResult(
        baidu_reachable=bool(reachable),
        server_ak_present=bool(server_ak_present),
        services_enabled=enabled,
        auth_error_status=auth_status,
        quota_exhausted_services=quota_exhausted,
        probe_error=probe_error,
    )


def _server_ak_verdict(probe: ProbeResult, states: Mapping[str, str]) -> str:
    """服务端 AK 的类型判断：从**官方码表的分类**里读，不在这里比较状态码。"""
    if not probe.server_ak_present:
        return "not_configured"
    seen = set(states.values())
    if "ak_type" in seen:
        return "wrong_type"
    if "ak_ip" in seen:
        return "ip_rejected"
    if "ak_sn" in seen:
        return "sn_rejected"
    if probe.all_enabled:
        return "server_verified"
    if probe.none_enabled:
        return "rejected"
    return "server_ok_partially_enabled"


def _worst_report(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    """挑一条最能解释现状的失败原因给横幅用（按 `_STATE_RANK` 排序，不看状态码）。"""
    failures = [r for r in reports if r.get("counts_toward_data_mode") and not r.get("available")]
    if not failures:
        return None
    return max(failures, key=lambda r: _STATE_RANK.get(str(r.get("state")), -1))


def _retry_after(reports: list[dict[str, Any]]) -> int | None:
    """横幅上的「多久之后可以再试」：取各项 `retry_after_s` 的最大值。

    为什么取最大：`4` / `302` 要到次日零时才恢复（FAQ 2366），写 30 s 等于诱导用户反复点，
    正是 `quota_breaker` 要避免的行为。
    """
    values = [r["retry_after_s"] for r in reports if isinstance(r.get("retry_after_s"), int)]
    return max(values) if values else None


def _dedupe(messages: list[str]) -> list[str]:
    return list(dict.fromkeys([m for m in messages if m]))


def _max_attempts(reports: list[dict[str, Any]]) -> int:
    """本轮探测里单个服务最多发过几次 HTTP——**实测值**，不是配置声明。"""
    values = [int(r.get("attempts") or 0) for r in reports if r.get("request_sent")]
    return max(values, default=0)


def _retry_policy(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """把「探测不重试」写成可核对的事实，而不是硬声称。

    🔴 为什么这条必须写清楚：本模块的重试纪律是「探测只看第一次的真相」，`retry_max_attempts=1`
    已把配置夹到下限；但 `BaiduClient` 里是 `max(1, retry_max_attempts)` +
    `attempt <= min(spec.retry_max_attempts, max_attempts)`，所以对**官方标成可重试**的码
    （`401` 并发、以及传输层错误）配置下限就是 2 次尝试。`infra/` 本任务只读，不能改，
    于是这里如实报实测次数并解释差一条从哪里来——看板拿「配置声明」当证据是会骗人的。
    """
    observed = _max_attempts(reports)
    return {
        "probe_has_own_retry_loop": False,
        "configured_retry_max_attempts": 1,
        "observed_max_attempts": observed,
        "single_attempt_per_service": observed <= 1,
        "note": (
            "探测自身不重试；`401`（并发已满）与传输错误按 errors.py 属可重试码，"
            "客户端在配置下限处仍会多试 1 次，故 attempts 可能为 2 —— 该值来自实测，"
            "不是声明。鉴权类（203/210/211/240/101）与日配额类（4/302）实测恒为 1 次。"
        ),
    }


def _banner(
    settings: Settings,
    mode: EffectiveDataMode,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """M4 前端直接取用的横幅（FR-105 / §4.6：任一情形都要挂横幅说明为什么不是实时数据）。

    标题一律用 PRD 原句；「为什么」那段永远来自 `infra/errors.py` 的 `user_message`。
    """
    worst = _worst_report(reports)
    details = _dedupe([str(r.get("message") or "") for r in reports if not r.get("available")])
    if mode == "live":
        return {
            "show": False,
            "level": "ok",
            "mode": "realtime",
            "title": "实时数据",
            "message": f"{settings.app_name}：地图服务可用，结果来自实时接口。",
            "detail_messages": [],
            "retry_after_s": None,
            "map_placeholder_text": None,
            "cta_hint": None,
        }
    if mode == "cache":
        return {
            "show": True,
            "level": "warning",
            "mode": "cache_first",
            "title": "部分实时服务不可用，当前优先使用缓存数据",
            "message": str(
                (worst or {}).get("message")
                or "部分地图服务未开通或今日额度已耗尽，已优先使用缓存。"
            ),
            "detail_messages": details,
            "retry_after_s": _retry_after(reports),
            "map_placeholder_text": None,
            "cta_hint": "在控制台为该 Key 勾选缺失的服务后点「重新探测」（/api/health?force=true）",
        }
    # mock：没发过请求就用 101 的文案（errors.py 已注明那是零配置的正常路径）
    message = str((worst or {}).get("message") or spec_for(101).user_message)
    return {
        "show": True,
        "level": "warning",
        "mode": "offline_report",
        "title": "实时服务不可用，当前为离线报告模式",
        "message": message,
        "detail_messages": details,
        "retry_after_s": _retry_after(reports),
        # FR-105 / R13：mock 下地图区域要换成合成示意图并显式标注，下面这句是 PRD 原话
        "map_placeholder_text": "离线示意图，非真实地图",
        "cta_hint": "配置 BAIDU_SERVER_AK 后点「重新探测」（/api/health?force=true）",
    }


def _quota_remaining(
    reports: list[dict[str, Any]], usage: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """PRD §4.7 要求的 `quota_remaining{}`：只报**能证实的**数字。

    🔴 笔记 §3.1 的权益表是「档位标准值」，本账号的实际档位与当日已用量必须登录控制台看
    （笔记 §9.2-①②），所以 `account_remaining` 一律 `null` 并附原因，绝不编一个数出来。

    账本键要再过一次 `SERVICE_BUCKET_MAP`：`Metrics.by_service` 是按**令牌桶**归并的
    （`direction` / `directionlite` / `routematrix` 都并进 `route`），拿探测项自己的
    `bucket` 字面量去查会永远查不到，`probe_used_here` 就成了恒 0 的假数据。
    共用一个桶的探测项无法逐项拆分，故显式标 `shared_bucket` 而不是假装精确。
    """
    by_bucket = usage.get("by_service")
    by_bucket = by_bucket if isinstance(by_bucket, Mapping) else {}
    normalized = [
        SERVICE_BUCKET_MAP.get(str(r.get("bucket") or ""), str(r.get("bucket") or ""))
        for r in reports
    ]
    shared = {name for name in normalized if normalized.count(name) > 1}
    out: dict[str, dict[str, Any]] = {}
    for report, bucket in zip(reports, normalized, strict=True):
        used = by_bucket.get(bucket) or {}
        used = used if isinstance(used, Mapping) else {}
        out[str(report.get("key"))] = {
            "official_daily_personal": report.get("daily_quota_personal"),
            "bucket": bucket,
            "probe_used_here": {
                "http_requests": int(used.get("http_requests") or 0),
                "place_calls": int(used.get("place_calls") or 0),
                "quota_units": int(used.get("quota_units") or 0),
            },
            "account_remaining": None,
            "state": report.get("state"),
            "note": "账号实际档位与当日已用量须登录控制台核实（笔记 §9.2-①②）",
            **(
                {"shared_bucket": True}
                if bucket in shared
                else {}  # 同桶的几条路线类探测共用一份计数，逐项数字并不可靠
            ),
        }
    return out


# --------------------------------------------------------------------------------------
# 记忆层：探测结论按服务缓存（**不写进 SQLite 响应缓存**）
# --------------------------------------------------------------------------------------
@dataclass
class _ProbeMemory:
    """进程内记忆。

    为什么不用响应缓存：这里存的是「今天这个服务通不通」这种运行期判断。写进缓存层既违反
    「错误响应不缓存」（PRD §4.6 缓存表），也会让 `/api/cache/stats` 的命中率被健康检查的
    水票污染——FR-402 的验收口径就没法看了。
    """

    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    taken_at: dict[str, float] = field(default_factory=dict)
    last_runs: int = 0
    cumulative: Metrics = field(default_factory=lambda: Metrics(label="health_probe_cumulative"))

    def fresh(self, item: ProbeItem) -> dict[str, Any] | None:
        """有效期内的上次结论；没有或已过期给 ``None``。"""
        stamp = self.taken_at.get(item.key)
        stored = self.verdicts.get(item.key)
        if stamp is None or stored is None:
            return None
        age = monotonic() - stamp
        if age > _ttl_for(item):
            return None
        report = dict(stored)
        report["from_memory"] = True
        report["age_s"] = round(age, 1)
        report["ttl_s"] = round(_ttl_for(item), 1)
        return report

    def remember(self, item: ProbeItem, report: Mapping[str, Any]) -> None:
        if report.get("from_memory"):
            return
        self.verdicts[item.key] = dict(report)
        self.taken_at[item.key] = monotonic()

    def forget(self, item: ProbeItem) -> None:
        self.verdicts.pop(item.key, None)
        self.taken_at.pop(item.key, None)


_MEMORY = _ProbeMemory()
_MEMORY_LOCK: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    """懒建锁：模块 import 时可能还没有事件循环（脚本 / TestClient 直接 import）。"""
    global _MEMORY_LOCK
    if _MEMORY_LOCK is None:
        _MEMORY_LOCK = asyncio.Lock()
    return _MEMORY_LOCK


def _ttl_for(item: ProbeItem) -> float:
    """某探测项的结论有效期。"""
    if item.scarce:
        return _env_number("CIRCLEDOC_PROBE_TTL_SCARCE_S", PROBE_TTL_SCARCE_S)
    return _env_number("CIRCLEDOC_PROBE_TTL_S", PROBE_TTL_S)


def probe_usage_totals() -> dict[str, Any]:
    """累计探测开销：演示前要知道「今天被健康检查烧掉了几次地点检索」。"""
    snap = _MEMORY.cumulative.snapshot()
    return {
        "runs": _MEMORY.last_runs,
        "http_requests": snap["http_requests"],
        "place_calls": snap["place_calls"],
        "quota_units": snap["quota_units"],
        "label": snap["label"],
    }


def reset_probe_memory() -> None:
    """清空探测记忆与累计账（测试换 AK / 换剧本之后要能回到干净状态）。"""
    _MEMORY.verdicts.clear()
    _MEMORY.taken_at.clear()
    _MEMORY.cumulative.reset()
    _MEMORY.last_runs = 0


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def _resolve_probe_choice(requested: str | None) -> tuple[str, str | None]:
    """探测开关：query 参数 > 环境变量 > 默认 `auto`。

    为什么要环境变量：「`--mock` 友好」的意思是命令行给一个变量就能演离线预案 B
    （`CIRCLEDOC_HEALTH_PROBE=mock CIRCLEDOC_HEALTH_SCENARIO=quota_exhausted`），
    不用谁去改代码，也不用每次手敲查询串。
    """
    choice = (requested or os.environ.get("CIRCLEDOC_HEALTH_PROBE") or "auto").strip().lower()
    if choice not in {"auto", "off", "mock"}:
        choice = "auto"
    scenario = (os.environ.get("CIRCLEDOC_HEALTH_SCENARIO") or "").strip() or None
    return choice, scenario


async def run_probe(
    settings: Settings,
    *,
    force: bool = False,
    probe: str | None = None,
    scenario: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """跑一次探测并返回 `/api/health` 的完整响应体（也是 lifespan / 脚本可用的入口）。

    `transport=` 是测试注入口（`httpx.MockTransport`）：传了就**一次真实出网都不会有**，且响应
    内容由调用方决定；与 `scenario` 的区别只是「剧本谁写」。
    """
    choice, scenario_name = _resolve_probe_choice(probe)
    scenario_name = (scenario or scenario_name or "").strip() or None
    script: Mapping[str, int] | None = MOCK_SCENARIOS.get(scenario_name) if scenario_name else None
    mocking = transport is None and (choice == "mock" or bool(scenario_name))
    if mocking and script is None:
        script = MOCK_SCENARIOS["all_ok"]  # 只说「mock」没给剧本 = 演一个全通的healthy 现场

    has_ak = bool(settings.baidu_server_ak.strip())
    inject_real_ak = has_ak or mocking
    reports: list[dict[str, Any]] = []
    performed = False
    short_circuit: str | None = None
    # 🔴 没出网也要给一份**完整零值**三栏：前端 / QA 直接读 `probe.usage.http_requests`，
    # 空字典会让「零配置 0 次出网」这条断言写成 KeyError 而不是 0，等于把证据变成误报。
    usage: dict[str, Any] = Metrics(label="health_probe").snapshot()

    async with _lock():
        if choice == "off":
            # 只看上次结论：一个请求都不发。首屏刷新走这条路，避免每次开页面都烧配额。
            for item in _PROBE_ITEMS:
                reports.append(_MEMORY.fresh(item) or _report_skipped(item))
        elif not inject_real_ak:
            # 🔴 零配置主路：不建客户端 → 结构上不可能出网
            for item in _PROBE_ITEMS:
                reports.append(_report_skipped(item))
            short_circuit = "no_server_ak"
        else:
            pending = _PROBE_ITEMS if (force or mocking or transport is not None) else None
            todo = [
                item
                for item in (pending or _PROBE_ITEMS)
                if pending is not None or _MEMORY.fresh(item) is None
            ]
            async with _probe_client(
                settings,
                transport=transport or (_mock_transport(script or {}) if mocking else None),
                mocking=mocking,
            ) as client:
                fresh = await _probe_all(client, todo)
                performed = True
                usage = client.metrics.snapshot()
            _MEMORY.cumulative.merge(client.metrics)
            if mocking or transport is not None:
                # 剧本 / 注入的结果**不进记忆**：一次 `?scenario=quota_exhausted` 不能把
                # 「今天额度用尽」粘在真实进程上 6 小时，那会让下一次真探测永远不发。
                by_key = {str(r["key"]): r for r in fresh}
                for item in _PROBE_ITEMS:
                    _MEMORY.forget(item)
                reports = [
                    by_key.get(item.key) or _report_not_probed(item) for item in _PROBE_ITEMS
                ]
            else:
                for item, report in zip(todo, fresh, strict=True):
                    _MEMORY.remember(item, report)
                merged = {**{str(r["key"]): r for r in fresh}}
                reports = []
                for item in _PROBE_ITEMS:
                    cached = merged.get(item.key) or _MEMORY.fresh(item)
                    reports.append(cached or _report_not_probed(item))

        server_ak_present = inject_real_ak
        probe_result = build_probe_result(reports, server_ak_present=server_ak_present)
        # 🔴 回写 Settings：后续任何一层取数都读 `data_mode_in_effect`，不再各自猜
        mode: EffectiveDataMode = settings.apply_probe(probe_result)
        _MEMORY.last_runs += 1
        payload = _build_payload(
            settings,
            mode=mode,
            reports=reports,
            probe=probe_result,
            usage=usage,
            meta={
                "performed": performed,
                "short_circuit": short_circuit,
                # 剧本模式塞的是占位 Key（只为过闸门、永不出网）——如实标出来，别让人
                # 以为 `server_ak_present=true` 说的是他自己的 Key。
                "ak_source": (
                    "mock_placeholder"
                    if mocking and not settings.baidu_server_ak.strip()
                    else ("real_configured" if has_ak else "none")
                ),
                "mode": "mock" if mocking else ("off" if choice == "off" else "real_network"),
                "scenario": scenario_name,
                "synthetic": bool(mocking or transport is not None),
                "transport_injected": transport is not None,
                "force": bool(force),
                "configured": choice,
                "requests_sent": int(usage.get("http_requests") or 0),
                "items_probed": len([r for r in reports if r.get("request_sent")]),
                "ttl_s": {item.key: round(_ttl_for(item), 1) for item in _PROBE_ITEMS},
            },
        )

    log_fields(
        logger,
        logging.INFO,
        "health_probe",
        data_mode=payload["data_mode"],
        data_source=payload["data_source"],
        baidu_reachable=payload["baidu_reachable"],
        requests_sent=payload["probe"]["requests_sent"],
        enabled=probe_result.enabled_count,
        total=probe_result.total_services,
        scenario=scenario_name,
        mock=mocking,
    )
    return payload


def _build_payload(
    settings: Settings,
    *,
    mode: EffectiveDataMode,
    reports: list[dict[str, Any]],
    probe: ProbeResult,
    usage: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """组装响应体：PRD §4.7 的出参一项不少，§4.6 的模式结论一项不多。"""
    states = {str(r["key"]): str(r.get("state") or "") for r in reports}
    services_enabled = {
        str(r["key"]): bool(r.get("available")) for r in reports if r.get("counts_toward_data_mode")
    }
    overall = "ok" if mode == "live" else ("degraded" if mode == "cache" else "offline")
    return {
        "status": overall,
        "ok": True,
        "checked_at": _now_iso(),
        # ---- PRD §4.7 的 /api/health 出参 -----------------------------------------
        "baidu_reachable": probe.baidu_reachable,
        "server_ak_type": _server_ak_verdict(probe, states),
        # 浏览器端 AK 只能由前端加载 JSAPI 时验证（笔记 §4.2）：后端探不到就如实说探不到
        "browser_ak_type": "present_unverifiable" if settings.has_browser_ak else "not_configured",
        "services_enabled": services_enabled,
        "quota_remaining": _quota_remaining(reports, usage),
        # ---- §4.6「启动探测即改默认模式」的结论 -------------------------------------
        "data_mode": mode,
        "data_mode_configured": settings.data_mode,
        "data_mode_source": _mode_source(
            settings,
            mode,
            performed=bool(meta.get("performed")),
            short_circuit=meta.get("short_circuit"),
        ),
        "data_source": expected_data_source(mode),
        "data_source_note": (
            "data_mode 的值域是 live|cache|mock（用户选的策略），data_source 的值域是 "
            "live|cache|sample（实际取了哪一层）——别把 sample 当模式值写进前端状态机。"
            "cache 模式未命中时仍会继续降到 sample。"
        ),
        "degraded": mode != "live",
        "degraded_reason": (
            None if mode == "live" else str((_worst_report(reports) or {}).get("message") or "")
        ),
        "unavailable_services": sorted(k for k, v in services_enabled.items() if not v),
        "quota_exhausted_services": list(probe.quota_exhausted_services),
        "auth_error_status": probe.auth_error_status,
        "banner": _banner(settings, mode, reports),
        "services": sorted(reports, key=lambda r: str(r.get("key"))),
        "probe": {
            **dict(meta),
            "retry_policy": _retry_policy(reports),
            # 🔴 说真话：探测侧没有重试循环，但 `BaiduClient` 的重试下限是「1 次重试」。
            "no_retry": _max_attempts(reports) <= 1,
            "usage": dict(usage),
            "cumulative": probe_usage_totals(),
            "probe_items": [
                {"key": item.key, "label": item.label, "path": item.path if item.call else None}
                for item in _PROBE_ITEMS
            ],
        },
        "config": settings.public_dict(),
        "endpoints": {
            "health": "/api/health",
            "cache_stats": "/api/cache/stats",
            "docs": "/api/docs",
            "home": "/",
        },
    }


def _mode_source(
    settings: Settings, mode: EffectiveDataMode, *, performed: bool, short_circuit: str | None
) -> str:
    """`data_mode` 到底是谁定的：显式配置 > 本次探测 > 没 Key 的直接判定 > 上次记忆 > 保守值。

    为什么要分这么细：§6.2 的零配置断言要看的是「mock 是**判**出来的还是**蒙**出来的」。
    - `probe`：真的发过探测请求（或跑过剧本），结论有依据。
    - `no_server_ak`：一个请求都没发，但结论同样确定——没 Key 就不可能有实时数据。
    - `probe_memory`：`probe=off` 或结论仍在有效期内，沿用上次判定。
    - `conservative_default`：进程还没探过，`Settings.data_mode_in_effect` 的保守兜底。
    """
    if settings.data_mode != "auto":
        return "explicit_config"
    if performed:
        return "probe"
    if short_circuit:
        return short_circuit
    if settings.probed_data_mode == mode:
        return "probe_memory"
    return "conservative_default"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@router.get("/health", summary="探活 + AK 类型与服务开通校验（并解析默认数据模式）")
async def health(
    request: Request,
    force: bool = Query(False, description="true = 忽略记忆，立即重探"),
    probe: str | None = Query(None, description="auto | off | mock（mock 零真实出网）"),
    scenario: str | None = Query(None, description="mock 剧本名，取值见 MOCK_SCENARIOS"),
) -> dict[str, Any]:
    """`GET /api/health`。🔴 **任何情况下都返回 200**：`Dockerfile` 的 HEALTHCHECK 只看
    `status < 500`，而「探测失败」本来就是零配置演示的正常状态，不是服务坏了。
    """
    settings: Settings = getattr(request.app.state, "settings", None) or _global_settings()
    try:
        return await run_probe(settings, force=force, probe=probe, scenario=scenario)
    except Exception as exc:  # pragma: no cover - 探测炸了也不能让健康检查变红
        log_fields(
            logger,
            logging.WARNING,
            "health_probe_crashed",
            error_type=type(exc).__name__,
        )
        return _fallback_payload(settings, error=type(exc).__name__)


def _fallback_payload(settings: Settings, *, error: str) -> dict[str, Any]:
    """探测路径本身抛了异常时的响应：**仍然是 200，仍然落 mock，仍然有横幅**。

    为什么值得单独写一份：PRD §6.2 的零配置断言要求「首页不出现红色报错」，如果 health 自己
    能 500，那条断言永远过不去，而现场看到的会是一个白屏 + 一条 traceback。
    """
    mode = settings.apply_probe(build_probe_result([], server_ak_present=False))
    spec = spec_for(101)
    return {
        "status": "offline",
        "ok": True,
        "checked_at": _now_iso(),
        "baidu_reachable": False,
        "server_ak_type": "unknown",
        "browser_ak_type": "unknown",
        "services_enabled": {},
        "quota_remaining": {},
        "data_mode": mode,
        "data_mode_configured": settings.data_mode,
        "data_mode_source": "conservative_default",
        "data_source": expected_data_source(mode),
        "degraded": True,
        "degraded_reason": "启动探测自身异常，已按最保守的 mock 运行。",
        "unavailable_services": [],
        "quota_exhausted_services": [],
        "auth_error_status": None,
        "banner": {
            "show": True,
            "level": "warning",
            "mode": "offline_report",
            "title": "实时服务不可用，当前为离线报告模式",
            "message": spec.user_message,
            "detail_messages": [f"探测异常：{error}"],
            "retry_after_s": None,
            "map_placeholder_text": "离线示意图，非真实地图",
            "cta_hint": None,
        },
        "services": [],
        "probe": {"performed": False, "mode": "crashed", "error": error, "no_retry": True},
        "config": settings.public_dict(),
        "endpoints": {"health": "/api/health", "cache_stats": "/api/cache/stats", "home": "/"},
    }


def _global_settings() -> Settings:
    """兜底取全局配置（`app.state.settings` 不在时，例如脚本里直接调 `health()`）。"""
    from infra.config import get_settings

    return get_settings()
