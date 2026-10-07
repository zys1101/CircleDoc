"""百度 Web 服务错误语义表 + 项目异常类型。

🔴 本模块存在的唯一理由：百度的限流与配额**不用 HTTP 状态码表达**，而是
「HTTP 200 + 响应体 `status`」。官方状态码表里根本不存在 429；并发超限是
`status=401`、天配额耗尽是 `4` / `302`（笔记 §3.2、PRD §4.6）。若按「5xx 才重试」
写客户端，会完全漏掉限流信号，同时把「当日已耗尽」的错误当成可重试错误反复打，
反而加速烧配额。

因此重试判据、熔断判据、熔断存活期、前端文案、REST HTTP 码**全部只从
`BAIDU_ERROR_TABLE` 查**，任何地方都不许再自己写一份 if 判断。

六类必须区分开（PRD FR-404 / §6.2 断言）：`203` AK 类型错、`210` IP 白名单、
`211` SN 校验、`240` 服务被禁用、`4`+`302` 天配额耗尽、`401` 并发超限——
它们的用户文案两两不同、处置动作也不同。`verify_user_messages_are_distinct()`
用于钉死这条约束（看板上有过一条 bug 是 203 与 210 返回同一条文案）：导入时只告警、
不抛异常，因为「零配置必须能跑」比「文案写重了」更不能崩。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

__all__ = [
    "AUTH_FAILURE_STATUSES",
    "BAIDU_ERROR_TABLE",
    "CATEGORY_ADVANCED_PERMISSION",
    "CATEGORY_AK_GENERIC",
    "CATEGORY_AK_IP",
    "CATEGORY_AK_SN",
    "CATEGORY_AK_TYPE",
    "CATEGORY_CONCURRENCY",
    "CATEGORY_PARAM",
    "CATEGORY_QUOTA",
    "CATEGORY_SERVICE_DISABLED",
    "CATEGORY_TRANSIENT",
    "CATEGORY_UNKNOWN",
    "CATEGORY_UNREACHABLE",
    "CONCURRENCY_BREAKER_HOLD_S",
    "DAILY_QUOTA_STATUSES",
    "SERVICE_UNAVAILABLE_STATUSES",
    "UNKNOWN_SPEC",
    "BaiduAkIpRejected",
    "BaiduAkSnRejected",
    "BaiduAkTypeMismatch",
    "BaiduApiError",
    "BaiduConcurrencyLimited",
    "BaiduDailyQuotaExhausted",
    "BaiduServiceDisabled",
    "BreakerKind",
    "BreakerLifetime",
    "CircleDocError",
    "ErrorSpec",
    "RouteUnreachable",
    "is_auth_failure",
    "is_daily_quota_failure",
    "is_retryable_status",
    "quota_opens_breaker",
    "seconds_until_next_midnight",
    "spec_for",
    "user_message_for",
    "verify_user_messages_are_distinct",
]

OK_STATUS = 0


class BreakerKind(StrEnum):
    """熔断器种类（PRD FR-401 的「双熔断器」）。"""

    NONE = "none"
    CONCURRENCY = "concurrency_breaker"
    QUOTA = "quota_breaker"


class BreakerLifetime(StrEnum):
    """熔断存活期。

    `UNTIL_NEXT_MIDNIGHT` 是给 `4` / `302` 用的：配额次日零时归零且当天不可恢复
    （FAQ 2361 / 2366），v1.0 那种 30 s 重连只会加速烧配额。
    """

    NONE = "none"
    FIXED_SECONDS = "fixed_seconds"
    UNTIL_NEXT_MIDNIGHT = "until_next_midnight"


@dataclass(frozen=True)
class ErrorSpec:
    """一个百度 `status` 的完整处置语义。"""

    status: int
    official_name: str
    category: str
    error_id: str
    user_message: str
    technical_hint: str
    retryable: bool
    retry_max_attempts: int
    retry_base_delay_s: float
    breaker: BreakerKind
    breaker_lifetime: BreakerLifetime
    breaker_hold_s: int | None
    http_status: int
    degrade_to_sample: bool
    counts_as_unreachable: bool

    @property
    def should_open_quota_breaker(self) -> bool:
        """是否应打开 `quota_breaker`（打开后当日不再发该服务的实时请求）。"""
        return self.breaker is BreakerKind.QUOTA

    def breaker_expires_in_s(self, now: datetime | None = None) -> int | None:
        """熔断应存活多少秒；不熔断返回 ``None``。"""
        if self.breaker is BreakerKind.NONE:
            return None
        if self.breaker_lifetime is BreakerLifetime.UNTIL_NEXT_MIDNIGHT:
            return seconds_until_next_midnight(now)
        return self.breaker_hold_s


def _spec(
    status: int,
    official_name: str,
    category: str,
    error_id: str,
    user_message: str,
    technical_hint: str,
    *,
    retryable: bool = False,
    retry_max_attempts: int = 0,
    retry_base_delay_s: float = 0.0,
    breaker: BreakerKind = BreakerKind.NONE,
    breaker_lifetime: BreakerLifetime = BreakerLifetime.NONE,
    breaker_hold_s: int | None = None,
    http_status: int = 502,
    degrade_to_sample: bool = True,
    counts_as_unreachable: bool = False,
) -> ErrorSpec:
    return ErrorSpec(
        status=status,
        official_name=official_name,
        category=category,
        error_id=error_id,
        user_message=user_message,
        technical_hint=technical_hint,
        retryable=retryable,
        retry_max_attempts=retry_max_attempts,
        retry_base_delay_s=retry_base_delay_s,
        breaker=breaker,
        breaker_lifetime=breaker_lifetime,
        breaker_hold_s=breaker_hold_s,
        http_status=http_status,
        degrade_to_sample=degrade_to_sample,
        counts_as_unreachable=counts_as_unreachable,
    )


# 分类常量：异常类与前端 `error.code` 都按这里的字符串对齐。
# 分类常量。两个字段分工明确，别混用：
#   `code`（= ErrorSpec.error_id）**按状态码唯一**，给测试断言与日志检索用；
#   `category` 是**处置分组**，前端按它选卡片样式与按钮（要不要「转离线」、要不要「去控制台」）。
# 之所以两者都要：FR-404 要求六类文案互不相同（靠 code），但 4 与 302 的处置动作是一样的
# （靠 category），只留一个字段就会在「文案要分、动作要合」这里卡住。
CATEGORY_AK_TYPE = "ak_type"
CATEGORY_AK_IP = "ak_ip"
CATEGORY_AK_SN = "ak_sn"
CATEGORY_SERVICE_DISABLED = "service_disabled"
CATEGORY_QUOTA = "daily_quota"
CATEGORY_CONCURRENCY = "concurrency"
CATEGORY_AK_GENERIC = "ak_invalid"
CATEGORY_PARAM = "bad_parameter"
CATEGORY_ADVANCED_PERMISSION = "advanced_permission"
CATEGORY_TRANSIENT = "transient"
CATEGORY_UNREACHABLE = "route_unreachable"
CATEGORY_UNKNOWN = "unknown"

# 并发熔断的固定存活期：PRD §4.6 concurrency_breaker「连续 5 次失败 → 打开 30 s」。
CONCURRENCY_BREAKER_HOLD_S = 30

# 来源：笔记 §3.2（官方附录 `webapi/appendix`，查阅 2026-10-04）。新增状态码只改这张表。
BAIDU_ERROR_TABLE: dict[int, ErrorSpec] = {
    spec.status: spec
    for spec in (
        # --- 1) AK 类型错：服务端 / 浏览器端 AK 混用，本项目最高频的坑 -----------------
        _spec(
            203,
            "APP 类型错误",
            CATEGORY_AK_TYPE,
            "baidu_ak_type_mismatch",
            "地图服务鉴权失败：服务端用错了 Key 类型（后端调用需要「服务端类型」AK，"
            "网页里的地图需要「浏览器类型」AK，两者不能互换）。",
            "在控制台分别建 Server(类型 1) 与 Browser(类型 3) 两个 AK，"
            "后端只读 BAIDU_SERVER_AK、前端只注入 BAIDU_BROWSER_AK。",
            http_status=502,
        ),
        # --- 2) IP 白名单：AK 类型对了但出口 IP 不在名单里 -----------------------------
        _spec(
            210,
            "APP IP 校验失败",
            CATEGORY_AK_IP,
            "baidu_ak_ip_rejected",
            "地图服务鉴权失败：这台机器的出口 IP 不在该 Key 的白名单里。",
            "把服务器出口 IP 加进服务端 AK 的白名单，本地开发可临时填 0.0.0.0/0。",
            http_status=502,
        ),
        # --- 3) SN 签名：与 IP 白名单是两种不同的开关 ----------------------------------
        _spec(
            211,
            "APP SN 校验失败",
            CATEGORY_AK_SN,
            "baidu_ak_sn_rejected",
            "地图服务鉴权失败：请求签名（SN）与服务端算出的期望值不一致。",
            "该 AK 开了 SN 校验：要么按官方算法补 sn 参数，要么在控制台关掉 SN 校验。",
            http_status=502,
        ),
        # --- 4) 服务被禁用：AK 有效但没勾这个服务 --------------------------------------
        _spec(
            240,
            "APP 服务被禁用",
            CATEGORY_SERVICE_DISABLED,
            "baidu_service_disabled",
            "该 Key 还没有开通本次需要的地图服务，请在控制台勾选后重试。",
            "控制台 → 我的应用 → 设置，勾选地理编码 / 全球逆地理编码 / 地点检索 / 路线规划。",
            http_status=502,
        ),
        # --- 5) 天配额耗尽：当日不可恢复，必须熔断到次日零时 ---------------------------
        _spec(
            4,
            "Quota Failure 配额校验失败（当日调用次数超限）",
            CATEGORY_QUOTA,
            "baidu_daily_quota_exhausted",
            "该地图服务今天的免费额度已用完，次日零时自动恢复；今天将不再发起实时查询，"
            "改用缓存或合成示例数据出具报告。",
            "quota_breaker 打开至次日零时；期间任何重试都只是在加速烧配额。",
            breaker=BreakerKind.QUOTA,
            breaker_lifetime=BreakerLifetime.UNTIL_NEXT_MIDNIGHT,
            http_status=503,
        ),
        _spec(
            302,
            "天配额超限，限制访问",
            CATEGORY_QUOTA,
            "baidu_daily_quota_limited",
            "该地图服务今日调用频次超限被临时限制，次日零时自动恢复；今天将不再发起实时查询"
            "，改用缓存或合成示例数据出具报告。",
            "与 status=4 同属天配额耗尽（FAQ 2366：次日零时归零），处置完全一致。",
            breaker=BreakerKind.QUOTA,
            breaker_lifetime=BreakerLifetime.UNTIL_NEXT_MIDNIGHT,
            http_status=503,
        ),
        # --- 6) 并发超限：本项目唯一真正对应「限流」的码，可指数退避 -------------------
        _spec(
            401,
            "当前并发量超过约定并发配额",
            CATEGORY_CONCURRENCY,
            "baidu_concurrency_limited",
            "地图服务当前并发已满，系统正在退避重试，请稍后再试。",
            "首次退避必须 ≥ 1 s（QPS 窗口以秒计，退避太短等于白重试，PRD §4.6）；"
            "burst 设置过大是主要诱因。",
            retryable=True,
            retry_max_attempts=3,
            retry_base_delay_s=1.0,
            breaker=BreakerKind.CONCURRENCY,
            breaker_lifetime=BreakerLifetime.FIXED_SECONDS,
            breaker_hold_s=CONCURRENCY_BREAKER_HOLD_S,
            http_status=429,
        ),
        # --- 其余：服务器 / 参数 / 权限类，按官方表逐条对齐 -----------------------------
        _spec(
            1,
            "服务器内部错误",
            CATEGORY_TRANSIENT,
            "baidu_server_error",
            "地图服务暂时不可用，系统正在重试。",
            "计入 concurrency_breaker 的连续失败次数（PRD §4.6）。",
            retryable=True,
            retry_max_attempts=3,
            retry_base_delay_s=0.5,
            breaker=BreakerKind.CONCURRENCY,
            breaker_lifetime=BreakerLifetime.FIXED_SECONDS,
            breaker_hold_s=CONCURRENCY_BREAKER_HOLD_S,
            http_status=502,
        ),
        _spec(
            8,
            "Parse Proto Failure 数据解析失败",
            CATEGORY_TRANSIENT,
            "baidu_parse_failure",
            "地图服务返回的数据不完整，系统正在重试。",
            "笔记 §3.2 明确「可重试 1 次」，比 PRD §4.6 的通用 3 次更严格，以笔记为准。",
            retryable=True,
            retry_max_attempts=1,
            retry_base_delay_s=0.5,
            breaker=BreakerKind.CONCURRENCY,
            breaker_lifetime=BreakerLifetime.FIXED_SECONDS,
            breaker_hold_s=CONCURRENCY_BREAKER_HOLD_S,
            http_status=502,
        ),
        _spec(
            2,
            "Parameter Invalid 请求参数非法",
            CATEGORY_PARAM,
            "baidu_bad_parameter",
            "本次查询参数有误，已停止重试。",
            "是我们拼参数出错，重试没有意义；查笔记 §6.2 的参数名与坐标顺序。",
            http_status=400,
            degrade_to_sample=True,
        ),
        _spec(
            3,
            "Verify Failure 权限校验失败",
            CATEGORY_AK_GENERIC,
            "baidu_verify_failure",
            "该 Key 没有调用此地图服务的权限。",
            "通常是控制台里该 AK 未勾选对应服务，与 240 同因但错误码不同。",
            http_status=502,
        ),
        _spec(
            5,
            "AK Failure",
            CATEGORY_AK_GENERIC,
            "baidu_ak_invalid",
            "地图服务 Key 无效或已失效。",
            "检查 BAIDU_SERVER_AK 是否被复制进多余空格、是否已在控制台删除。",
            http_status=502,
        ),
        _spec(
            9,
            "Permission Denied 高级权限校验失败",
            CATEGORY_ADVANCED_PERMISSION,
            "baidu_advanced_permission",
            "该功能需要商用授权，本项目不使用。",
            "笔记 §4.5：多边形区域检索返回 9 属预期，不应触发任何降级以外的行为。",
            http_status=502,
        ),
        _spec(
            101,
            "AK 参数不存在",
            CATEGORY_AK_GENERIC,
            "baidu_ak_missing",
            "未配置地图服务 Key，已改用离线数据出具报告。",
            "零配置启动的正常路径：BAIDU_SERVER_AK 为空即会走到这里或直接不发请求。",
            http_status=502,
        ),
        _spec(
            200,
            "APP 不存在",
            CATEGORY_AK_GENERIC,
            "baidu_app_missing",
            "地图服务应用不存在，请核对 Key。",
            "控制台已删除该应用。",
            http_status=502,
        ),
        _spec(
            201,
            "APP 被用户自己禁用",
            CATEGORY_AK_GENERIC,
            "baidu_app_disabled",
            "地图服务应用已被停用。",
            "在控制台重新启用该应用。",
            http_status=502,
        ),
        _spec(
            202,
            "APP 被管理员删除",
            CATEGORY_AK_GENERIC,
            "baidu_app_removed",
            "地图服务应用已被删除。",
            "重新创建应用并更新 Key。",
            http_status=502,
        ),
        _spec(
            250,
            "用户不存在",
            CATEGORY_AK_GENERIC,
            "baidu_user_missing",
            "地图服务账号状态异常。",
            "确认开发者账号是否存在。",
            http_status=502,
        ),
        _spec(
            251,
            "用户被自己删除",
            CATEGORY_AK_GENERIC,
            "baidu_user_disabled",
            "地图服务账号已被停用。",
            "确认开发者账号状态。",
            http_status=502,
        ),
        _spec(
            252,
            "用户被管理员删除",
            CATEGORY_AK_GENERIC,
            "baidu_user_removed",
            "地图服务账号已被删除。",
            "重新注册开发者账号。",
            http_status=502,
        ),
        _spec(
            260,
            "服务不存在",
            CATEGORY_AK_GENERIC,
            "baidu_service_not_found",
            "请求的地图服务不存在。",
            "核对接口路径，路径只能抄笔记 §2 的核验结果。",
            http_status=502,
        ),
        _spec(
            261,
            "服务被禁用（已下线）",
            CATEGORY_SERVICE_DISABLED,
            "baidu_service_offline",
            "该地图服务已下线，无法调用。",
            "换用笔记里标注仍在维护的接口。",
            http_status=502,
        ),
        _spec(
            500,
            "FAQ 2555：返回值 500 需将调用 IP 加白名单",
            CATEGORY_AK_IP,
            "baidu_ip_whitelist_required",
            "地图服务拒绝了这台机器的 IP。",
            "与 status=210 同因：白名单未含当前出口 IP。",
            http_status=502,
        ),
        # --- 路线类专有码：不是错误，是「该方向不可达」的数据，供 blocked 判定使用 -------
        _spec(
            2001,
            "无骑行路线（direction v2 专有）",
            CATEGORY_UNREACHABLE,
            "baidu_route_unreachable",
            "该方向没有可用的骑行路线，已记为不可达。",
            "FR-206：骑行的 blocked 判定走官方信号，不用耗时推断。",
            http_status=200,
            degrade_to_sample=False,
            counts_as_unreachable=True,
        ),
        _spec(
            7,
            "无返回结果（directionlite v1 专有）",
            CATEGORY_UNREACHABLE,
            "baidu_route_empty",
            "该方向没有返回路线，已记为不可达。",
            "空 results 属正常数据，不能当异常抛出中断整次分析。",
            http_status=200,
            degrade_to_sample=False,
            counts_as_unreachable=True,
        ),
    )
}

UNKNOWN_SPEC = _spec(
    -1,
    "未收录的状态码",
    CATEGORY_UNKNOWN,
    "baidu_unknown_error",
    "地图服务返回了未预期的结果，本次已改用离线数据出具报告。",
    "官方表新增码时先扩 BAIDU_ERROR_TABLE，不要就地猜处置动作——保守起见不重试、不开熔断。",
    http_status=502,
)

# 鉴权 / AK 配置类错误：探测到这些说明「Key 本身不能用」，PRD §4.6 据此决定 mock 或 cache。
AUTH_FAILURE_STATUSES = frozenset(
    {3, 5, 101, 200, 201, 202, 203, 210, 211, 240, 250, 251, 252, 260, 261, 500}
)
DAILY_QUOTA_STATUSES = frozenset({4, 302})
CONCURRENCY_STATUSES = frozenset({401})
# 「服务未开通」与「Key 不能用」在 health 探测里要分开看（笔记 §4.5）。
SERVICE_UNAVAILABLE_STATUSES = frozenset({9, 240, 260, 261})


def spec_for(status: int) -> ErrorSpec:
    """取某百度 `status` 的处置语义；未收录的码返回 `UNKNOWN_SPEC` 而不抛异常。

    未收录也不抛：真实现场常见官方新增码，抛异常会让 FR-403 的「任一层失败不中断流程」
    直接破功。
    """
    return BAIDU_ERROR_TABLE.get(status, UNKNOWN_SPEC)


def user_message_for(status: int) -> str:
    """用户可读文案（前端错误卡片直接用，禁止在前端再写一份码表）。"""
    return spec_for(status).user_message


def is_retryable_status(status: int) -> bool:
    """重试判据只看 body `status`，永远不看 HTTP 状态码。"""
    return spec_for(status).retryable


def quota_opens_breaker(status: int) -> bool:
    """该 `status` 是否应打开 `quota_breaker`（当日不再发该服务）。"""
    return spec_for(status).should_open_quota_breaker


def is_auth_failure(status: int) -> bool:
    """是否属于「AK 类型 / 白名单 / SN / 服务未勾选」这一类配置问题。"""
    return status in AUTH_FAILURE_STATUSES


def is_daily_quota_failure(status: int) -> bool:
    """是否属于天配额耗尽（`4` / `302`，次日零时归零）。"""
    return status in DAILY_QUOTA_STATUSES


def seconds_until_next_midnight(now: datetime | None = None, tz_offset_hours: int = 8) -> int:
    """距离「次日零时」还有多少秒——`quota_breaker` 的存活期。

    用固定 UTC+8 偏移而不是 `zoneinfo`：东八区无夏令时，偏移恒定；而 Windows 常缺
    tzdata 导致 `ZoneInfo` 抛异常，熔断存活期不能依赖它。
    """
    tz = timezone(timedelta(hours=tz_offset_hours))
    current = (now or datetime.now(tz)).astimezone(tz)
    next_midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_midnight - current).total_seconds()))


def verify_user_messages_are_distinct() -> list[str]:
    """校验 FR-404 六类文案两两不同，返回重复项描述（无重复则为空列表）。

    为什么需要它：这类合并只在真实鉴权失败当天才看得出来，等发现时用户已经看到的是
    「Key 类型错了」这种错误指引。QA 侧断言本函数返回空列表即可锁死这条约束。
    """
    required = (
        203,
        210,
        211,
        240,
        4,
        302,
        401,
    )
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for status in required:
        message = BAIDU_ERROR_TABLE[status].user_message
        if message in seen:
            duplicates.append(f"status={seen[message]} 与 status={status} 文案相同")
        seen[message] = status
    if len({BAIDU_ERROR_TABLE[s].category for s in (4, 302)}) != 1:
        duplicates.append("status=4 与 status=302 应同属天配额一类")
    return duplicates


class CircleDocError(Exception):
    """项目内所有可预期异常的基类：自带用户可读文案与 REST HTTP 码。

    设计要点：异常本身就够描述一次「用户能看到什么、要不要重试、要不要熔断」，
    API 层只负责把它序列化成 JSON，不再维护第二套映射表。
    """

    error_id = "internal_error"
    category = "internal"
    http_status = 500
    user_message = "服务内部异常，本次已按离线模式给出可用的部分结果。"
    retryable = False
    breaker: BreakerKind = BreakerKind.NONE
    degrade_to_sample = True

    def __init__(
        self,
        message: str | None = None,
        *,
        user_message: str | None = None,
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message or self.user_message)
        self.message = message or self.user_message
        self.user_message = user_message or type(self).user_message
        self.detail: dict[str, Any] = detail or {}
        self.request_id = request_id

    def to_payload(self) -> dict[str, Any]:
        """给 `/api/*` 错误响应用的字典；**不含 traceback、不含 AK**。

        `request_id` 不在这里放——响应信封顶层已经有了，两处各写一份迟早会不一致。
        """
        return {
            "code": self.error_id,
            "category": self.category,
            "message": self.user_message,
            "retryable": self.retryable,
            "degrade_to_sample": self.degrade_to_sample,
            "breaker": self.breaker.value,
            "detail": self.detail,
        }


class BaiduApiError(CircleDocError):
    """由百度响应体 `status` 翻译出来的异常。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int = -1,
        service: str | None = None,
        user_message: str | None = None,
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status = status
        self.service = service
        self.spec = spec_for(status)
        super().__init__(
            message or f"baidu status={status} ({self.spec.official_name})",
            user_message=user_message or self.spec.user_message,
            detail={
                "baidu_status": status,
                "service": service,
                "technical_hint": self.spec.technical_hint,
                **(detail or {}),
            },
            request_id=request_id,
        )
        self.error_id = self.spec.error_id
        self.category = self.spec.category
        self.http_status = self.spec.http_status
        self.retryable = self.spec.retryable
        self.breaker = self.spec.breaker
        self.degrade_to_sample = self.spec.degrade_to_sample

    @property
    def retry_max_attempts(self) -> int:
        """客户端应使用的重试上限（`0` 表示禁止重试）。"""
        return self.spec.retry_max_attempts

    @property
    def retry_base_delay_s(self) -> float:
        """指数退避的首次等待；`401` 至少 1 s，否则等于白重试（PRD §4.6）。"""
        return self.spec.retry_base_delay_s

    @property
    def breaker_hold_s(self) -> int | None:
        """本次失败应让对应熔断器打开多少秒；不熔断返回 ``None``。"""
        return self.spec.breaker_expires_in_s()

    @classmethod
    def from_status(
        cls, status: int, *, service: str | None = None, **kwargs: Any
    ) -> BaiduApiError:
        """按 `status` 抛出对应类别的子类，让上层能 `except BaiduDailyQuotaExhausted`。"""
        category = spec_for(status).category
        subclass = _CATEGORY_EXCEPTIONS.get(category, cls)
        return subclass(status=status, service=service, **kwargs)  # type: ignore[return-value]


class BaiduAkTypeMismatch(BaiduApiError):
    """`status=203`：服务端与浏览器端 AK 混用。"""

    error_id = "baidu_ak_type_mismatch"
    category = CATEGORY_AK_TYPE


class BaiduAkIpRejected(BaiduApiError):
    """`status=210` / `500`：出口 IP 不在服务端 AK 白名单内。"""

    error_id = "baidu_ak_ip_rejected"
    category = CATEGORY_AK_IP


class BaiduAkSnRejected(BaiduApiError):
    """`status=211`：SN 校验失败。"""

    error_id = "baidu_ak_sn_rejected"
    category = CATEGORY_AK_SN


class BaiduServiceDisabled(BaiduApiError):
    """`status=240` / `261`：该 AK 未开通（或已下线）对应服务。"""

    error_id = "baidu_service_disabled"
    category = CATEGORY_SERVICE_DISABLED


class BaiduDailyQuotaExhausted(BaiduApiError):
    """`status=4` / `302`：当日配额耗尽，**必须熔断到次日零时**，重试只会加速烧配额。"""

    error_id = "baidu_daily_quota_exhausted"
    category = CATEGORY_QUOTA
    breaker = BreakerKind.QUOTA


class BaiduConcurrencyLimited(BaiduApiError):
    """`status=401`：并发超限，本项目唯一真正对应「限流」的码，可指数退避。"""

    error_id = "baidu_concurrency_limited"
    category = CATEGORY_CONCURRENCY
    breaker = BreakerKind.CONCURRENCY


class RouteUnreachable(BaiduApiError):
    """`status=2001` / `7`：该方向确实没有路线，是数据而不是故障，不重试不熔断。"""

    error_id = "baidu_route_unreachable"
    category = CATEGORY_UNREACHABLE
    degrade_to_sample = False


_CATEGORY_EXCEPTIONS: dict[str, type[BaiduApiError]] = {
    CATEGORY_AK_TYPE: BaiduAkTypeMismatch,
    CATEGORY_AK_IP: BaiduAkIpRejected,
    CATEGORY_AK_SN: BaiduAkSnRejected,
    CATEGORY_SERVICE_DISABLED: BaiduServiceDisabled,
    CATEGORY_QUOTA: BaiduDailyQuotaExhausted,
    CATEGORY_CONCURRENCY: BaiduConcurrencyLimited,
    CATEGORY_UNREACHABLE: RouteUnreachable,
}


_DUPLICATE_USER_MESSAGES = verify_user_messages_are_distinct()
if _DUPLICATE_USER_MESSAGES:  # pragma: no cover - 只有把表里的文案改重了才触发
    warnings.warn(
        "FR-404 六类文案出现重复：" + "；".join(_DUPLICATE_USER_MESSAGES),
        RuntimeWarning,
        stacklevel=1,
    )
