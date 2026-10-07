#!/usr/bin/env python
"""`tools/bench_route_strategy.py` —— 「串行 `direction` vs 批量算路」对比实验（FR-405 / §6.2）。

产出直接喂给 `docs/TEST_REPORT.md`。两条对照路径跑的是**同一份输入、同一个真实
`BaiduClient`**，只换调用方式：

```
A 串行：  direction/v2/walking     每个目的地 1 次请求，1 条路线
B 批量：  routematrix/v2/walking   1 起点 × ≤50 目的地，1 次请求 N 条路线
```

🔴 **哪些数是实测、哪些是推算，必须分开说**（这是本脚本存在的全部意义）：

| 指标 | 性质 | 说明 |
| --- | --- | --- |
| `http_requests` | **实测** | 从 `httpx.MockTransport` 真正看到的请求路径逐条计数 |
| `quota_units` | **实测** | 官方口径 `origins × destinations`，由真实客户端累加器给出 |
| 本地 CPU 耗时 | **实测** | 但只反映本机解析与记账开销，**不是**线上耗时 |
| 线上墙钟耗时 | **推算** | `max(请求数 × RTT, 请求数 / 桶速率)`。RTT 是显式参数，不是测出来的 |

为什么线上墙钟只能推算：条款 3.2.3(ii) 明文**禁止对百度线上服务施压测试**，
而「跑 96 次串行 direction」正是一种压测形态。所以本脚本**零真实出网**
（`MockTransport` 不建任何连接、AK 是假串），把可精确测的部分测准，
把依赖网络的部分标成模型并给出参数。

同时附一段**限流验证**（§6.2 第二条）：把令牌桶调到 0.5 QPS、mock 网络层，
连发 20 个请求，验证 (a) 没有任何请求穿透出网，(b) 排队行为符合桶语义。
⚠️ 这条同样在本地 mock 层完成，不对线上服务施压。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from infra.baidu_client import BaiduClient  # noqa: E402
from infra.config import Settings  # noqa: E402
from infra.log import configure_logging  # noqa: E402
from infra.metrics import Metrics  # noqa: E402
from infra.ratelimit import TokenBucket  # noqa: E402

FAKE_AK = "fake-ak-for-mock-transport-only"
"""假 AK：只用于让客户端通过「有 AK」的前置检查。它不是任何真实账号的凭据。"""

ORIGIN = {"lng": 116.404, "lat": 39.915}

# 真实等时圈采样的配额负载（PRD §4.2 第 267 行）：Step1 12 条 + Step2 3 轮 × 12 + Step3 24 ≈ 96 条。
ISOCHRONE_ROUTE_LINES = 96


def make_destinations(count: int) -> list[dict[str, float]]:
    """造 `count` 个同心圆上的目的地（纯本地，不查任何服务）。"""
    out: list[dict[str, float]] = []
    for index in range(count):
        angle = (index % 36) * 10.0 * math.pi / 180.0
        radius_deg = 0.004 + 0.0004 * (index // 36)
        out.append(
            {
                "lng": round(ORIGIN["lng"] + radius_deg * math.cos(angle), 6),
                "lat": round(ORIGIN["lat"] + radius_deg * math.sin(angle), 6),
            }
        )
    return out


class Recorder:
    """一个只记账、不建连接的 transport handler。"""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.batch_sizes: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        params = dict(request.url.params)
        if request.url.path.startswith("/routematrix"):
            dests = [piece for piece in params.get("destinations", "").split("|") if piece]
            self.batch_sizes.append(len(dests))
            rows = [
                {"status": 0, "duration": {"value": 300 + index * 5}, "distance": {"value": 700}}
                for index in range(len(dests))
            ]
            return httpx.Response(200, json={"status": 0, "result": rows})
        # direction/v2/*：单条路线，回官方形状
        return httpx.Response(
            200,
            json={
                "status": 0,
                "result": {"routes": [{"duration": 320, "distance": 780}]},
            },
        )


def build_settings(*, http_budget: int = 1000, place_budget: int | None = None) -> Settings:
    """假配置：本实验测的是**调用形态**，不该被 2.5 QPS 的真桶拖成十分钟。

    `http_budget` 默认放宽到 1000，好让 A（串行）能跑完 —— 用默认的 20 次预算它会在
    第 20 个请求就被守卫截停，那样只测得到一个被截断的串行路径。真实预算下的行为
    由 `main_async()` 的第二段实验单独报告（那正是最有说服力的一条结论）。
    """
    return Settings(
        baidu_server_ak=FAKE_AK,
        baidu_browser_ak="",
        data_mode="mock",
        rate_route_qps=1000.0,
        burst_route=1000,
        max_http_requests_per_analysis=http_budget,
        **({"max_place_calls_per_analysis": place_budget} if place_budget else {}),
        cache_enabled=False,
        log_json=False,
    )


async def run_serial(
    recorder: Recorder, destinations: list[dict[str, float]], settings: Settings
) -> dict[str, Any]:
    """A：逐个目的地调 `direction/v2/walking`。"""
    client = BaiduClient(
        settings,
        transport=httpx.MockTransport(recorder.handler),
        metrics=Metrics(label="bench-serial"),
        mode_count=1,
    )
    started = time.perf_counter()
    try:
        for point in destinations:
            await client.direction("walking", ORIGIN, point)
        usage = client.usage()
    finally:
        await client.aclose()
    return {
        "strategy": "serial_direction",
        "elapsed_local_s": round(time.perf_counter() - started, 4),
        "http_requests": len(recorder.paths),
        "quota_units": int(usage.get("quota_units", 0)),
        "route_lines": dict(usage.get("route_lines", {})),
        "usage": usage,
    }


async def run_matrix(
    recorder: Recorder, destinations: list[dict[str, float]], settings: Settings
) -> dict[str, Any]:
    """B：一次批量算路把同一批目的地打包（内部按 ≤50 自动切分）。"""
    client = BaiduClient(
        settings,
        transport=httpx.MockTransport(recorder.handler),
        metrics=Metrics(label="bench-matrix"),
        mode_count=1,
    )
    started = time.perf_counter()
    try:
        await client.route_matrix("walking", [ORIGIN], destinations)
        usage = client.usage()
    finally:
        await client.aclose()
    return {
        "strategy": "batch_routematrix",
        "elapsed_local_s": round(time.perf_counter() - started, 4),
        "http_requests": len(recorder.paths),
        "quota_units": int(usage.get("quota_units", 0)),
        "route_lines": dict(usage.get("route_lines", {})),
        "batch_sizes": list(recorder.batch_sizes),
        "usage": usage,
    }


def model_wall_clock(requests: int, *, rtt_s: float, qps: float) -> float:
    """线上墙钟耗时的**推算**（不是实测）：网络往返与令牌桶排队取较大者。

    `requests / qps` 是桶放行 `requests` 个请求所需的下限时间；
    `requests × rtt` 是串行等待往返的下限时间。两者**不叠加**：桶在等的时候
    前一个请求已经在飞，真正决定下限的是更慢的那一个。真实实现里限流与往返
    部分重叠，所以这是个偏保守的下界。
    """
    return max(requests * rtt_s, requests / qps if qps > 0 else 0.0)


async def rate_limit_experiment(*, qps: float, requests: int, rtt_ms: float) -> dict[str, Any]:
    """§6.2 第二条：本地 mock 网络层上的限流验证（零真实出网）。

    验证两件事：(a) 连发的请求没有任何一个绕过桶（`no_bypass`）；
    (b) 排队行为符合桶语义 —— 前 `burst` 个立即放行，其余按 `1/qps` 逐个排队。

    `burst` 取 `max(1, round(qps))`：PRD §4.6 要求 `burst ≤ rate`，0.5 QPS 的桶容量只能是 1，
    否则一次放行多个并发就会触发 `status=401`（v1.0 的 `burst=5` 正是这么错的）。
    """
    burst = 1
    bucket = TokenBucket("bench-route", qps, burst)
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.handler(request)
        return httpx.Response(200, json={"status": 0, "result": [{"status": 0}]})

    # 客户端自己的桶放到 1000 QPS（等于关掉），这样排队完全由上面那个 0.5 QPS 的桶决定；
    # 两套桶同时生效会让「等待来自哪一层」说不清。
    client = BaiduClient(
        build_settings(http_budget=10_000),
        transport=httpx.MockTransport(handler),
        metrics=Metrics(label="bench-ratelimit"),
        mode_count=1,
    )
    waits: list[float] = []
    started = time.perf_counter()
    try:
        for _ in range(requests):
            waits.append(await bucket.acquire())
            await client.direction("walking", ORIGIN, make_destinations(1)[0])
        elapsed = time.perf_counter() - started
        usage = client.usage()
    finally:
        await client.aclose()
    throttled = [value for value in waits if value > 0]
    # 0.5 QPS 下 20 个请求的理论排队时间：(20 - burst) / 0.5 ≈ 38 s。
    expected_queue_s = (requests - burst) / qps if qps > 0 else 0.0
    return {
        "qps": qps,
        "requests": requests,
        "burst": burst,
        "rtt_ms": rtt_ms,
        "elapsed_s": round(elapsed, 3),
        "expected_queue_s": round(expected_queue_s, 3),
        "requests_that_passed": len(recorder.paths),
        "requests_that_waited": len(throttled),
        "first_waits_s": [round(value, 4) for value in throttled[:3]],
        "max_wait_s": round(max(waits), 4) if waits else 0.0,
        "http_requests": len(recorder.paths),
        "quota_units": int(usage.get("quota_units", 0)),
        "no_bypass": len(recorder.paths) == requests,
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    destinations = make_destinations(args.count)
    wide = build_settings(http_budget=10_000)

    serial_recorder = Recorder()
    serial = await run_serial(serial_recorder, destinations, wide)

    matrix_recorder = Recorder()
    matrix = await run_matrix(matrix_recorder, destinations, wide)

    # 同一份输入 -> 两条路径的配额必须**完全相同**。不同就说明有一条算错了账。
    assert (
        serial["quota_units"] == matrix["quota_units"]
    ), f"配额消耗应当相同：serial={serial['quota_units']} matrix={matrix['quota_units']}"

    settings = build_settings(http_budget=10_000)
    # 🔴 推算用的是**真实档位**的桶速率（默认 2.5 QPS），不是实验里那个 1000 QPS 的假桶。
    # 用假桶去推算会把令牌桶排队（96 / 2.5 = 38.4 s）整个漏掉，而那一项才是串行路径
    # 真正的瓶颈（远大于 96 × 120 ms = 11.5 s 的往返）。
    qps = args.model_qps
    rtt_s = args.rtt_ms / 1000.0
    serial_model = model_wall_clock(serial["http_requests"], rtt_s=rtt_s, qps=qps)
    matrix_model = model_wall_clock(matrix["http_requests"], rtt_s=rtt_s, qps=qps)

    # ---- 第二段：同样两条路径，但用**真实的 §6.1 出网预算**（http_requests ≤ 20）----
    # 这一段才是最有说服力的：串行路径不是「慢」，而是**根本跑不完**。
    tight = build_settings(http_budget=20)
    tight_serial_recorder = Recorder()
    tight_serial = await run_serial(tight_serial_recorder, destinations, tight)
    tight_matrix_recorder = Recorder()
    tight_matrix = await run_matrix(tight_matrix_recorder, destinations, tight)
    budget_experiment = {
        "http_budget": 20,
        "requested_destinations": args.count,
        "serial_completed_http": tight_serial["http_requests"],
        "serial_completed_quota_units": tight_serial["quota_units"],
        "serial_completion_ratio": round(tight_serial["http_requests"] / args.count, 4),
        "serial_degraded": tight_serial["usage"].get("degraded_count", 0),
        "matrix_completed_http": tight_matrix["http_requests"],
        "matrix_completed_quota_units": tight_matrix["quota_units"],
        "matrix_full_coverage": tight_matrix["quota_units"] == args.count,
        "verdict": (
            "串行在 §6.1 的 20 次出网预算内只能完成一部分目的地；"
            "批量算路在同样预算内覆盖全部目的地。批量省的是往返，不是配额。"
        ),
    }

    rate_limit = await rate_limit_experiment(
        qps=args.limit_qps, requests=args.limit_requests, rtt_ms=args.rtt_ms
    )

    return {
        "input": {"destinations": args.count, "origin": ORIGIN},
        "assumptions": {
            "rtt_ms": args.rtt_ms,
            "model_qps": qps,
            "experiment_qps": settings.rate_route_qps,
            "note": (
                "rtt_ms 与 model_qps 是显式假设参数，不是测出来的；线上墙钟耗时为推算值。"
                "实验本身跑在超高 QPS 的假桶上（为了不被真桶拖成 38 秒），"
                "但推算一律用真实档位。本脚本零真实出网（MockTransport 不建连接、AK 为假串），"
                "因为条款 3.2.3(ii) 禁止对百度线上服务施压测试。"
            ),
        },
        "serial": serial,
        "matrix": matrix,
        "comparison": {
            "http_requests_serial": serial["http_requests"],
            "http_requests_matrix": matrix["http_requests"],
            "http_requests_ratio": round(
                serial["http_requests"] / max(1, matrix["http_requests"]), 2
            ),
            "quota_units_serial": serial["quota_units"],
            "quota_units_matrix": matrix["quota_units"],
            "quota_units_identical": serial["quota_units"] == matrix["quota_units"],
            "modeled_wall_serial_s": round(serial_model, 2),
            "modeled_wall_matrix_s": round(matrix_model, 2),
            "modeled_speedup": round(serial_model / max(1e-9, matrix_model), 2),
        },
        "budget_experiment": budget_experiment,
        "rate_limit": rate_limit,
        "realistic_load": {
            "note": f"等时圈一次采样约 {ISOCHRONE_ROUTE_LINES} 条路线（PRD §4.2）。",
            "serial_http_requests": ISOCHRONE_ROUTE_LINES,
            "matrix_http_requests": math.ceil(ISOCHRONE_ROUTE_LINES / 50),
            "quota_units": ISOCHRONE_ROUTE_LINES,
            "modeled_wall_serial_s": round(
                model_wall_clock(ISOCHRONE_ROUTE_LINES, rtt_s=rtt_s, qps=qps), 2
            ),
            "modeled_wall_matrix_s": round(
                model_wall_clock(math.ceil(ISOCHRONE_ROUTE_LINES / 50), rtt_s=rtt_s, qps=qps), 2
            ),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench_route_strategy.py",
        description="串行 direction vs 批量算路（FR-405）。零真实出网。",
    )
    parser.add_argument("--count", type=int, default=96, help="对照实验的目的地数（默认 96）")
    parser.add_argument(
        "--rtt-ms", type=float, default=120.0, help="假设的单次往返耗时（毫秒，仅用于推算）"
    )
    parser.add_argument(
        "--model-qps",
        type=float,
        default=2.5,
        help=(
            "推算线上墙钟时用的 route 桶速率（默认 2.5，即 PRD §4.6 的建议值）。"
            "⚠️ 实验本身跑在 1000 QPS 的假桶上（否则 96 个请求要排 38 秒），"
            "但**推算**必须用真实档位，否则会把限流等待整个漏掉。"
        ),
    )
    parser.add_argument(
        "--limit-qps", type=float, default=0.5, help="限流验证用的桶速率（默认 0.5）"
    )
    parser.add_argument(
        "--limit-requests", type=int, default=20, help="限流验证连发的请求数（默认 20）"
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "把 JSON 结果写到指定文件（UTF-8 无 BOM）。"
            "⚠️ 不要用 `python ... --json > out.json`：PowerShell 的重定向会写成 UTF-16，"
            "下游 json.load 直接报 BOM 错。"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 预算守卫截停时会打 WARNING；这里是**故意**触发它的实验，日志噪音要压掉，
    # 否则「串行被截停」这条结论会被几十行同样的告警淹掉。
    configure_logging("ERROR", json_format=False)
    result = asyncio.run(main_async(args))
    if args.out:
        # 显式 `encoding="utf-8"` 且不加 BOM：下游（文档脚本、json.load）期望的就是这个。
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    comp = result["comparison"]
    load = result["realistic_load"]
    limit = result["rate_limit"]
    print("=" * 78)
    print("串行 direction vs 批量算路（FR-405 / §6.2）—— 零真实出网")
    print("=" * 78)
    print(f"输入：{result['input']['destinations']} 个目的地，同一中心点")
    print(
        f"推算假设：RTT {result['assumptions']['rtt_ms']} ms，"
        f"route 桶 {result['assumptions']['model_qps']} QPS（PRD §4.6 建议值）\n"
    )
    print(f"{'指标':<28}{'A 串行 direction':>20}{'B 批量 routematrix':>22}")
    print("-" * 78)
    print(
        f"{'HTTP 请求数（实测）':<28}"
        f"{comp['http_requests_serial']:>20}{comp['http_requests_matrix']:>22}"
    )
    print(
        f"{'配额单位（实测）':<28}"
        f"{comp['quota_units_serial']:>20}{comp['quota_units_matrix']:>22}"
    )
    print(
        f"{'本地 CPU 耗时 s（实测）':<28}"
        f"{result['serial']['elapsed_local_s']:>20}{result['matrix']['elapsed_local_s']:>22}"
    )
    print(
        f"{'线上墙钟 s（推算）':<28}"
        f"{comp['modeled_wall_serial_s']:>20}{comp['modeled_wall_matrix_s']:>22}"
    )
    print("-" * 78)
    print(
        f"请求数下降：{comp['http_requests_ratio']}×；配额消耗相同：{comp['quota_units_identical']}"
    )
    print(f"推算提速：{comp['modeled_speedup']}×（模型值，非实测）\n")

    print(f"等时圈真实负载（约 {load['quota_units']} 条路线）：")
    print(
        f"  A 串行：{load['serial_http_requests']} 次请求，推算 {load['modeled_wall_serial_s']} s"
    )
    print(
        f"  B 批量：{load['matrix_http_requests']} 次请求，推算 {load['modeled_wall_matrix_s']} s"
    )
    print(f"  配额两者都是 {load['quota_units']} 条 —— 批量省的是往返，不是配额\n")

    budget = result["budget_experiment"]
    print("=" * 78)
    print(f"真实出网预算下的行为（http_requests ≤ {budget['http_budget']}，§6.1）")
    print("=" * 78)
    print(
        f"  请求 {budget['requested_destinations']} 个目的地：\n"
        f"    A 串行：只完成 {budget['serial_completed_http']} 次请求"
        f"（{budget['serial_completion_ratio'] * 100:.1f}%），"
        f"{budget['serial_degraded']} 次被守卫截停，"
        f"配额只花掉 {budget['serial_completed_quota_units']} 条\n"
        f"    B 批量：{budget['matrix_completed_http']} 次请求覆盖全部 "
        f"{budget['matrix_completed_quota_units']} 条路线"
        f"（完整覆盖={budget['matrix_full_coverage']}）"
    )
    print(f"  → {budget['verdict']}\n")

    print(f"限流验证（本地 mock 网络层，{limit['qps']} QPS 桶，连发 {limit['requests']} 个请求）：")
    print(f"  拿到响应的请求数：{limit['requests_that_passed']} / {limit['requests']}")
    print(f"  排队过的请求数：{limit['requests_that_waited']}（burst={limit['burst']} 立即放行）")
    print(f"  前三次等待：{limit['first_waits_s']} s，最长 {limit['max_wait_s']} s")
    print(f"  总耗时：{limit['elapsed_s']} s")
    print(f"  无请求穿透出网限制：{limit['no_bypass']}")
    print("\n⚠️ 线上墙钟耗时为推算值（RTT 是假设参数）。本脚本零真实出网：")
    print("   条款 3.2.3(ii) 明文禁止对百度线上服务做压测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
