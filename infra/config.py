"""集中配置：所有可调参数只能来自环境变量 / `.env`，并且**零配置也必须能用**。

为什么所有出网预算、限流阈值、TTL 都堆在这里：PRD §6.2 要求「把 `route` 日预算临时调成
1 条 → 第一次分析中途触发预算守护；调成 5,000 → 行为不变、只是永不触发」。这类验证
必须只改 `.env` 就能完成，任何写死在代码里的数字都会让回归变成改代码。

三条硬约束（写在这里以免后续任务再讨论）：

1. `.env` 不存在不能报错，`BAIDU_SERVER_AK` / `BAIDU_BROWSER_AK` 默认**空串**而不是必填
   —— 缺 AK 是零配置演示的正常状态，不是异常。
2. `data_mode` 的值域是 `auto|live|cache|mock`（用户选的策略），`data_source` 的值域是
   `live|cache|sample`（实际取了哪一层）。`auto` 只是「等启动探测给答案」，运行期一定
   要经 `resolve_data_mode()` 变成三个具体值之一。
3. 默认值一律取最保守假设（PRD §4.6）：档位猜低了由 `quota_breaker` 兜住，猜高了只是
   没用满，两个方向都不能崩、不能给错结论。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DataMode = Literal["auto", "live", "cache", "mock"]
"""用户在 `.env` / 请求里可选择的取数策略。"""

EffectiveDataMode = Literal["live", "cache", "mock"]
"""运行期真正生效的模式：`auto` 一定在启动探测时被解析掉，不会进入业务代码。"""

DataSource = Literal["live", "cache", "sample"]
"""响应里回显的「实际数据来源」徽标值（FR-306）。注意有 `sample` 而没有 `mock`。"""

TransportMode = Literal["walking", "riding"]

SERVICE_BUCKET_MAP: dict[str, str] = {
    "geocode": "geocode",
    "regeo": "regeo",
    "place": "place",
    # 地址联想与地点检索共用 100 次/日的配额（PRD §4.7），因此也必须共用同一个桶，
    # 否则两个桶各放 0.5 QPS 实际就是 1 QPS，把配额算重了一倍。
    "suggest": "place",
    "route": "route",
    "routematrix": "route",
    "direction": "route",
    "polyline": "route",
}

CACHE_TTL_PLACE_HARD_MAX_S = 24 * 3600
"""POI 缓存的合规上限：条款 3.3.4 把缓存限定为「减少重复调用的技术措施」（笔记 §7.3）。

注意：24 h 是**产品假设**，官方从未公开「允许缓存多久」，所以这里是上限而不是推荐值。
"""

ROOT_DIR = Path(__file__).resolve().parents[1]

PLACE_CALLS_PER_ANALYSIS_DEFAULT = 8
ROUTE_LINES_PER_ANALYSIS_DEFAULT = 150
QUOTA_UNITS_SINGLE_MODE_DEFAULT = 120
QUOTA_UNITS_DUAL_MODE_DEFAULT = 220
HTTP_REQUESTS_PER_ANALYSIS_DEFAULT = 20


def _dotenv_files() -> tuple[str, ...]:
    """`.env` 候选路径。缺失由 pydantic-settings 静默跳过，绝不报错。

    同时找仓库根与当前工作目录，是因为 `uvicorn` 在 Docker 里和在本机 dev 时的 cwd
    不一样；只认 cwd 会让「配了 .env 却没生效」变成现场最难排查的问题。
    """
    candidates: list[Path] = []
    override = os.environ.get("CIRCLEDOC_ENV_FILE")
    if override:
        candidates.append(Path(override))
    candidates.append(ROOT_DIR / ".env")
    candidates.append(Path.cwd() / ".env")

    ordered: list[str] = []
    for path in candidates:
        text = str(path)
        if text not in ordered:
            ordered.append(text)
    return tuple(ordered)


@dataclass(frozen=True)
class ProbeResult:
    """`/api/health` 启动自检的结论（探测项清单见笔记 §4.5）。

    为什么要单独一个数据结构而不是几个 bool 参数：探测有 7 个服务级结果，
    「AK 完全不可用」和「只有部分服务没开通」这两件事在 PRD §4.6 里对应**不同的**
    默认模式（前者 `mock`、后者 `cache`），必须能一次性、可单测地表达出来。
    """

    baidu_reachable: bool = False
    server_ak_present: bool = False
    services_enabled: Mapping[str, bool] = field(default_factory=dict)
    auth_error_status: int | None = None
    """首个鉴权 / 服务开通类错误码（203 / 210 / 211 / 240 / 101 / …），供文案展示用。"""

    quota_exhausted_services: tuple[str, ...] = ()
    probe_error: str | None = None
    """探测本身失败的原因（DNS、超时、TLS）；有值即视为「出网不可用」。"""

    @property
    def total_services(self) -> int:
        """参与探测的服务数；为 0 表示什么都没探到，按最保守处理。"""
        return len(self.services_enabled)

    @property
    def enabled_count(self) -> int:
        return sum(1 for enabled in self.services_enabled.values() if enabled)

    @property
    def all_enabled(self) -> bool:
        return self.total_services > 0 and self.enabled_count == self.total_services

    @property
    def none_enabled(self) -> bool:
        return self.total_services > 0 and self.enabled_count == 0

    @property
    def partially_enabled(self) -> bool:
        return self.total_services > 0 and 0 < self.enabled_count < self.total_services


EMPTY_PROBE = ProbeResult()


def resolve_data_mode(
    probe_result: ProbeResult = EMPTY_PROBE,
    configured_mode: DataMode = "auto",
) -> EffectiveDataMode:
    """启动探测结论 → 本次运行实际使用的 `data_mode`（返回值一定不是 `auto`）。

    判定分支按优先级从上到下，全部对应 PRD §4.6「启动探测即改默认模式」：

    1. 用户在 `.env` 里显式写了 `live|cache|mock` → 尊重它，不覆盖（测试要把模式钉死）。
    2. 出网不可用 / 探测自身报错 / 一个服务都没探到 → `mock`（对应「无 AK 或 AK 完全不
       可用」；合成数据在最内层兜着，报告仍完整）。
    3. 没有服务端 AK → `mock`。
    4. 全部服务探测失败 → `mock`（Key 类型错、IP 白名单、SN 之类整台机器都不能用）。
    5. 部分服务未开通 / 探测里出现鉴权错误码 → `cache`：还能用的那些服务照常实时，
       不能用的那条单独降级并标注原因。
    6. 有服务今日配额耗尽（`4` / `302`）→ `cache`：实时调用今天注定失败，但缓存里可能
       还有命中；继续打实时只会更快烧配额（笔记 §3.3）。
    7. 其余（全通过）→ `live`。

    任何没被上面覆盖的意外组合都落回 `mock`——猜错方向时「结果仍是完整报告」比
    「看起来更真实」重要。
    """
    if configured_mode != "auto":
        return configured_mode

    if probe_result.probe_error or not probe_result.baidu_reachable:
        return "mock"
    if not probe_result.server_ak_present:
        return "mock"
    if probe_result.total_services == 0:
        return "mock"
    if probe_result.none_enabled:
        return "mock"
    if probe_result.partially_enabled:
        return "cache"
    if probe_result.quota_exhausted_services:
        return "cache"
    if probe_result.auth_error_status is not None:
        return "cache"
    if probe_result.all_enabled:
        return "live"
    return "mock"  # 兜底：分支没覆盖到的组合按最保守走


def expected_data_source(mode: EffectiveDataMode) -> DataSource:
    """模式对应的「最理想数据来源」徽标值，用于给前端一个稳定的映射口径。

    为什么要有这个函数：`data_mode` 与 `data_source` 名字像、值域不同（`mock` vs
    `sample`），把它们当成同一个字段写进前端状态机是 PRD §4.6 明确点名的坑。注意
    `cache` 模式命中失败时会继续降级到 `sample`，所以这里的返回值只是「起点」。
    """
    mapping: dict[EffectiveDataMode, DataSource] = {
        "live": "live",
        "cache": "cache",
        "mock": "sample",
    }
    return mapping[mode]


class Settings(BaseSettings):
    """应用配置。字段名即环境变量名（大小写不敏感），例如 `BAIDU_SERVER_AK`。"""

    model_config = SettingsConfigDict(
        env_file=_dotenv_files(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------------------------------------------------------------
    # 应用与日志
    # ----------------------------------------------------------------------------------
    app_name: str = "CircleDoc"
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    api_prefix: str = "/api"
    log_level: str = "INFO"
    log_json: bool = True
    """False 时输出人类可读单行文本，只为本机调试方便；CI 与 Docker 都用 JSON。"""

    # ----------------------------------------------------------------------------------
    # 百度 AK：两个变量名是硬规定（笔记 §4.4）
    # ----------------------------------------------------------------------------------
    baidu_server_ak: str = ""
    """服务端类型 AK。**与浏览器端 AK 混用会报 `status=203 APP 类型错误`**（FAQ 2639）。
    只从环境变量读，禁止写进代码、日志或前端（PRD §5）。默认空串 = 零配置正常态。"""

    baidu_browser_ak: str = ""
    """浏览器类型 AK，只用于前端加载 JSAPI；后端永不用它调 Web 服务。"""

    # ----------------------------------------------------------------------------------
    # 出网：域名白名单防 SSRF（PRD §5），超时保守
    # ----------------------------------------------------------------------------------
    baidu_allowed_hosts: str = "api.map.baidu.com"
    baidu_connect_timeout_s: float = 5.0
    baidu_read_timeout_s: float = 12.0
    baidu_max_concurrency: int = 4
    """同时在途请求上限。刻意低于官方并发口径：并发超限的表现是 `status=401`
    而不是 HTTP 429（笔记 §3.2），本机先收紧比指望服务端宽容可靠。"""

    # ----------------------------------------------------------------------------------
    # 取数模式
    # ----------------------------------------------------------------------------------
    data_mode: DataMode = "auto"
    """默认 `auto` 而不是 `live`：默认值不能假设 AK 可用（PRD §4.6 / §6.2 零配置断言）。"""

    probed_data_mode: EffectiveDataMode | None = None
    """启动探测写入的运行模式；由 `Settings.apply_probe()` 设置，不由环境变量提供。"""

    suggest_enabled: bool = False
    """FR-108 地址联想总开关，**默认关闭**：地点输入提示与地点检索**共用 100 次/日**配额
    （笔记 FAQ 2540 / 2549），开着它就等于让输入框替用户烧检索额度。
    ⚠️ 这个字段是 `.env` 唯一生效路径：`services/geo.suggest_enabled()` 的兜底读的是
    `os.environ`，而 pydantic-settings 的 `env_file` **只填字段、不写环境变量**——
    没有它时，`.env` 里写 `SUGGEST_ENABLED=true` 会静默无效。"""

    # ----------------------------------------------------------------------------------
    # 令牌桶：按服务分桶（PRD §4.6）。稀缺度相差 50 倍的服务共用一个桶会互相挤占。
    # 数值是**保守建议值**，须经 `.env` 覆盖，并以账号控制台实际档位为准（笔记 §9.2-①）。
    #
    # 注意：PRD §4.6 正文要求 `burst ≤ rate`，但它自己给的表里 place(0.5/1) 与
    # geocode(1.0/2) 的 burst 都大于 rate。这里**照表原值保留、不加校验**——先把口径
    # 冲突暴露出来，等 PRD 澄清后再收紧；贸然校验会让默认配置直接启动失败。
    # ----------------------------------------------------------------------------------
    rate_place_qps: float = 0.5
    burst_place: int = 1
    rate_regeo_qps: float = 0.5
    burst_regeo: int = 1
    rate_route_qps: float = 2.5
    burst_route: int = 2
    rate_geocode_qps: float = 1.0
    burst_geocode: int = 2

    # ----------------------------------------------------------------------------------
    # 单次分析的出网预算：三栏各自独立（PRD §6.1 断言要求 place_calls /
    # quota_units / http_requests 分开统计，混成一个数字会掩盖真实消耗）
    # ----------------------------------------------------------------------------------
    max_place_calls_per_analysis: int = PLACE_CALLS_PER_ANALYSIS_DEFAULT
    """地点检索只有 100 次/日，是最紧的瓶颈；6 类 × 多别名 query 会瞬间打满（笔记 §8.3）。"""

    max_route_lines_per_analysis: int = ROUTE_LINES_PER_ANALYSIS_DEFAULT
    """口径是**单模式**（步行）。双模式必须按模式数折算，见 `route_lines_budget()`。"""

    max_quota_units_per_analysis: int = QUOTA_UNITS_SINGLE_MODE_DEFAULT
    max_quota_units_dual_mode: int = QUOTA_UNITS_DUAL_MODE_DEFAULT
    max_http_requests_per_analysis: int = HTTP_REQUESTS_PER_ANALYSIS_DEFAULT

    # ----------------------------------------------------------------------------------
    # 重试与双熔断（判据来自 `infra/errors.py`，这里只放阈值）
    # ----------------------------------------------------------------------------------
    retry_max_attempts: int = 3
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0
    retry_concurrency_first_delay_s: float = 1.0
    """`status=401` 的首次退避下限：QPS 窗口以秒计，退避小于 1 s 等于白重试（PRD §4.6）。"""

    concurrency_breaker_threshold: int = 5
    concurrency_breaker_hold_s: int = 30
    quota_breaker_tz_offset_hours: int = 8
    """「次日零时」按东八区算（FAQ 2366）；用固定偏移而不是 tzdata，Windows 上常缺时区库。"""

    # ----------------------------------------------------------------------------------
    # 缓存：定位为同进程运行时性能缓存，不是数据资产库（FR-402）
    # ----------------------------------------------------------------------------------
    cache_enabled: bool = True
    cache_db_path: Path = ROOT_DIR / "data" / "cache.db"
    cache_ttl_geocode_s: int = 30 * 24 * 3600
    cache_ttl_regeo_s: int = 30 * 24 * 3600
    cache_ttl_place_s: int = CACHE_TTL_PLACE_HARD_MAX_S
    """⚠️ 合规硬上限 24 h（v1.0 的 3 天已下调）；超过会被强制夹紧，见 model validator。"""

    cache_ttl_route_s: int = 7 * 24 * 3600
    cache_ttl_isochrone_s: int = 7 * 24 * 3600
    cache_error_ttl_s: int = 0
    """错误响应不缓存（PRD §4.6 缓存表），任何正数都会被当成配置错误处理。"""

    # ----------------------------------------------------------------------------------
    # 样例 / 合成数据
    # ----------------------------------------------------------------------------------
    samples_dir: Path = ROOT_DIR / "data" / "samples"
    """**合成数据**的落点（FR-412）：`tools/gen_samples.py` 写、样例层读。
    与下面的 `samples_config_path` 是两件事，别混用。"""

    samples_config_path: Path = ROOT_DIR / "config" / "samples.yaml"
    """预置样例社区清单（FR-102）：社区名、行政区、中心点坐标与所属建成形态。
    坐标必须国内（境外各服务对个人档为「-」，笔记 §3.1）。文件里的 `coord_verified`
    标记该坐标是**实调核验过**还是待回填，`/api/samples` 会如实透出这个标记。"""

    weights_path: Path = ROOT_DIR / "config" / "weights.yaml"
    """评分权重表（PRD §4.5 明确要求以配置文件外置，而不是写死在代码里）：
    改权重口径不该改代码，也不该让「产品假设而非国标」这句话失去可替换性。"""

    web_dir: Path = ROOT_DIR / "web"
    default_center_lng: float = 116.404
    default_center_lat: float = 39.915
    """首屏默认中心点（BD09LL，北京）。仅用于零配置时地图有落点，不承载业务结论。

    为什么拆成两个标量而不是一个元组：pydantic-settings 对复杂类型要求环境变量是合法
    JSON，`(116.404, 39.915)` 这种写法会让「把 .env.example 复制成 .env」直接启动失败。
    """

    @property
    def default_center_lnglat(self) -> tuple[float, float]:
        """对外统一给 `(lng, lat)`；百度请求参数里的 `location` 是「纬度,经度」，
        顺序换算由 `services/` 的客户端负责，不在配置层猜。
        """
        return (self.default_center_lng, self.default_center_lat)

    # ----------------------------------------------------------------------------------
    # 入参边界（PRD §5：类型/范围校验在入口就做）
    # ----------------------------------------------------------------------------------
    min_minutes: int = 5
    max_minutes: int = 60
    max_radius_m: int = 5000
    china_lng_min: float = 73.5
    china_lng_max: float = 135.1
    china_lat_min: float = 18.1
    china_lat_max: float = 53.5
    """样例社区必须在国内：境外各服务对个人档配额为「-」，探测会直接失败（笔记 §4.5）。"""

    config_notes: list[str] = Field(default_factory=list)
    """启动期对配置做过的强制修正（例如 POI TTL 被夹回 24 h），供 `/api/health` 回显。"""

    # ----------------------------------------------------------------------------------
    # 校验与派生
    # ----------------------------------------------------------------------------------
    @model_validator(mode="after")
    def _enforce_compliance_caps(self) -> Settings:
        """把违反合规或不合常理的取值夹回安全范围，并记录一条 note。

        为什么夹紧而不是抛异常：PRD §4.6 的原则是「配错方向也不能崩」——有人把 POI TTL
        写成 3 天时，正确的响应是照 24 h 跑并告诉他被改了，而不是让服务起不来。
        """
        if self.cache_ttl_place_s > CACHE_TTL_PLACE_HARD_MAX_S:
            self.config_notes.append(
                f"CACHE_TTL_PLACE_S={self.cache_ttl_place_s} 超过 24 h 合规上限，"
                f"已夹回 {CACHE_TTL_PLACE_HARD_MAX_S}"
            )
            self.cache_ttl_place_s = CACHE_TTL_PLACE_HARD_MAX_S
        if self.cache_error_ttl_s > 0:
            self.config_notes.append("CACHE_ERROR_TTL_S 必须为 0：错误响应不进缓存")
            self.cache_error_ttl_s = 0
        if self.baidu_max_concurrency < 1:
            self.baidu_max_concurrency = 1
        for name in ("min_minutes", "max_minutes", "max_radius_m"):
            if getattr(self, name) <= 0:
                msg = f"{name.upper()} 必须为正数，已恢复默认"
                self.config_notes.append(msg)
                defaults = {"min_minutes": 5, "max_minutes": 60, "max_radius_m": 5000}
                setattr(self, name, defaults[name])
        if self.min_minutes > self.max_minutes:
            self.config_notes.append("MIN_MINUTES 大于 MAX_MINUTES，已对调")
            self.min_minutes, self.max_minutes = self.max_minutes, self.min_minutes
        return self

    @property
    def has_server_ak(self) -> bool:
        """是否配置了服务端 AK；空串按「没配」处理，零配置启动的正常分支。"""
        return bool(self.baidu_server_ak.strip())

    @property
    def has_browser_ak(self) -> bool:
        return bool(self.baidu_browser_ak.strip())

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """出网域名白名单，逗号分隔写法便于 `.env` 维护。"""
        return tuple(host.strip() for host in self.baidu_allowed_hosts.split(",") if host.strip())

    @property
    def data_mode_in_effect(self) -> EffectiveDataMode:
        """当前应使用的取数模式：显式指定 > 启动探测结果 > `mock`（未探测时的保守值）。

        为什么兜底是 `mock` 而不是 `live`：探测还没跑（或跑挂了）就发实时请求，正是
        PRD §4.6 要避免的最坏现场——用户反复点「一键体检」反复收鉴权错误。
        """
        if self.data_mode != "auto":
            return self.data_mode
        if self.probed_data_mode is not None:
            return self.probed_data_mode
        return "mock"

    def apply_probe(self, probe_result: ProbeResult) -> EffectiveDataMode:
        """把启动探测结论写回配置并返回解析后的模式（`/api/health` 在启动时调用）。"""
        mode = resolve_data_mode(probe_result, self.data_mode)
        self.probed_data_mode = mode
        return mode

    def bucket_for(self, service: str) -> str:
        """服务名 → 令牌桶名。地址联想并进 place 桶（与地点检索共用日配额）。"""
        return SERVICE_BUCKET_MAP.get(service, service)

    def rate_limit_for(self, service: str) -> tuple[float, int]:
        """取某服务的 `(rate_qps, burst)`；未单独配置时用最保守的 place 桶。"""
        bucket = self.bucket_for(service)
        rates = {
            "place": (self.rate_place_qps, self.burst_place),
            "regeo": (self.rate_regeo_qps, self.burst_regeo),
            "route": (self.rate_route_qps, self.burst_route),
            "geocode": (self.rate_geocode_qps, self.burst_geocode),
        }
        return rates.get(bucket, (self.rate_place_qps, self.burst_place))

    def route_lines_budget(self, mode_count: int = 1) -> int:
        """路线条数预算，按本次要跑的模式数折算。

        为什么必须乘：`route` 桶由步行、骑行、polyline 懒加载三者共用，150 条是「单模式」
        口径。双模式连跑约 192 条 > 150，守卫会在第二轮中途截停并给出「结果不完整」——
        这既不是配额问题也不是算法问题，纯粹是预算单位写错（PRD §4.6）。
        """
        return self.max_route_lines_per_analysis * max(1, mode_count)

    def quota_units_budget(self, mode_count: int = 1) -> int:
        """配额单位预算：单模式 120，双模式放宽到 220（PRD §6.1 验收口径）。"""
        if mode_count <= 1:
            return self.max_quota_units_per_analysis
        return self.max_quota_units_dual_mode

    def budget_snapshot(self, mode_count: int = 1) -> dict[str, int]:
        """一次分析的三栏预算快照，供 `/api/analysis` 的守卫与报告共用。"""
        return {
            "place_calls": self.max_place_calls_per_analysis,
            "quota_units": self.quota_units_budget(mode_count),
            "http_requests": self.max_http_requests_per_analysis,
            "route_lines": self.route_lines_budget(mode_count),
        }

    def public_dict(self) -> dict[str, object]:
        """可以回显给前端 / 日志的配置子集：**任何 AK 值都不在这里出现**。"""
        return {
            "app_name": self.app_name,
            "app_env": self.app_env,
            "data_mode": self.data_mode,
            "data_mode_in_effect": self.data_mode_in_effect,
            "has_server_ak": self.has_server_ak,
            "has_browser_ak": self.has_browser_ak,
            "cache_enabled": self.cache_enabled,
            "budget": self.budget_snapshot(),
            "notes": list(self.config_notes),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级配置单例。

    为什么缓存：`Settings()` 每次都要读 `.env` 与全部环境变量，请求路径上重复构造
    既慢又会让「同一请求前后读到不同配置」这种鬼故事变成可能。
    """
    return Settings()


def reload_settings() -> Settings:
    """丢掉缓存重新读取环境变量，返回新的单例。

    为什么要暴露给测试：预算断言（`route` 日预算 1 条 / 5000 条两向验证）必须能在
    不重启进程的情况下切换配置；让 QA 自己去清 `lru_cache` 是重复劳动。
    """
    get_settings.cache_clear()
    return get_settings()
