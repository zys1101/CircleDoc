#!/usr/bin/env python
"""`tools/gen_samples.py` —— 合成示例数据集生成器（FR-412 / 交付物清单 §9）。

三条子命令：

```
python tools/gen_samples.py            # 生成：把 3 个样例社区的合成体检报告写进 data/samples/
python tools/gen_samples.py --check     # 只校验清单与既有基线，不出网、不写文件（CI 用）
python tools/gen_samples.py --verify    # 用地理编码 / 逆地理核验坐标并回填 config/samples.yaml
```

🔴 **为什么是「合成」而不是「录制」**（D9 / 条款 3.3.4）：

v1.0 的设计是「真实调一次、把返回存成 JSON 随镜像分发」。核验后判定这**违反条款 3.3.4
（不得将百度数据生成或用于数据库）与 6.3（运营数据归百度）**，属禁止形态而非扣分风险。
本脚本因此改为：用**我方自己的算法**（`SyntheticOracle` 的各向异性速度场 + 词表驱动的
虚构 POI 网格）按给定中心点算出等时圈与设施点，名称一律 `样例-xxx-NN`、坐标落在虚构网格上，
**不复制任何百度返回内容**。所以本数据集附带的是「**生成方法与参数说明**」，
而不是「数据来源声明」—— 后者会暗示内容来自百度。

🔴 **生成物是什么**：每个社区一份**冻结的 mock 体检报告**（分数 / 圈内计数 / 盲区面积）。
它的价值不是「运行时要用它」（运行时按同一套算法现算），而是当**回归基线**：
PRD §6.1 要求「对预置样例，盲区数量与圈内设施计数在缓存命中下 100% 可复现」，
有了一份签入版本库的基线，这句话才有一条能自动跑的断言
（`tests/test_samples_baseline.py`）。基线漂了就是算法变了，必须有人解释为什么。

⚠️ **`data/samples/*.json` 不参与运行时降级**：FR-105 的 `mock` 模式走
`services/poi_search.py:SyntheticPoiSamples` 现算，不读这里的文件。两者同源、结果一致，
但**不要让运行时去读基线文件** —— 那会把「可复算」变成「读死数据」，
评审一改参数就会发现基线不再成立。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许直接 `python tools/gen_samples.py`
    sys.path.insert(0, str(ROOT))

from infra.config import get_settings  # noqa: E402
from infra.log import configure_logging, get_logger  # noqa: E402
from services.analysis import run_analysis  # noqa: E402
from services.poi_categories import DIMENSION_ORDER  # noqa: E402
from services.samples import (  # noqa: E402
    SampleCommunity,
    SampleConfigError,
    load_samples,
)

logger = get_logger("circledoc.tools.gen_samples")

GENERATOR_VERSION = "1.0"
"""生成器版本。算法参数变了就要升它 —— 基线文件里会记下版本，便于判断是哪一侧动了。"""

VOLATILE_KEYS = ("elapsed_ms",)
"""写基线前剔除的易变字段：每次运行的耗时必然不同，留在基线里会让回归永远为红。"""


# --------------------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------------------
def _strip_volatile(report: dict[str, Any]) -> dict[str, Any]:
    """把报告削成**可复现的子集**：去掉时间戳、耗时、任务号这些每次都变的东西。

    保留的是结论本身（分数、计数、盲区、雷达），也就是 §6.1 那条断言真正要比对的部分。
    """
    pruned = dict(report)
    pruned.pop("task_id", None)
    pruned.pop("request_id", None)
    meta = dict(pruned.get("meta") or {})
    meta.pop("generated_at", None)
    pruned["meta"] = meta
    usage = dict(pruned.get("usage") or {})
    for key in VOLATILE_KEYS:
        usage.pop(key, None)
    pruned["usage"] = usage
    return pruned


async def generate_one(community: SampleCommunity) -> dict[str, Any]:
    """按社区坐标与形态跑一次 **mock** 体检，返回冻结后的基线对象。"""
    report = await run_analysis(
        list(community.lnglat),
        modes=["walking"],
        minutes=15.0,
        data_mode="mock",
        platform="desktop",
        archetype=community.archetype,
    )
    pruned = _strip_volatile(report)
    return {
        "generator": {
            "tool": "tools/gen_samples.py",
            "version": GENERATOR_VERSION,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            # ⚠️ 措辞是「生成方法与参数说明」而不是「数据来源声明」（D9 / FR-412）：
            #    本文件内容**全部由本程序合成**，不含任何百度返回内容。
            "method": (
                "等时圈由本项目的扇形采样 + 二分收敛算法配合确定性合成速度场"
                "（三次谐波各向异性 + 一条顺畅走廊 + 两道阻隔扇区，种子取自中心点坐标）算出；"
                "设施点由 POI 维度词表按虚构网格外推。**不含任何百度地图返回内容。**"
            ),
            "deterministic": True,
            "seed_source": "center_coordinates",
            "parameters": {
                "minutes": 15.0,
                "modes": ["walking"],
                "platform": "desktop",
                "data_mode": "mock",
                "dimensions": list(DIMENSION_ORDER),
            },
        },
        "community": community.as_dict(),
        "baseline": {
            "score": {
                key: pruned["score"][key]
                for key in (
                    "total",
                    "grade",
                    "radar",
                    "penalty",
                    "blind_area_pct",
                    "weight_sum",
                    "normalized",
                    "reference_only",
                )
            },
            "counts_in": {
                group["key"]: group["count_in"] for group in pruned["coverage"]["groups"]
            },
            "nearest_minutes": {
                group["key"]: group["nearest_minutes"] for group in pruned["coverage"]["groups"]
            },
            "blind_spots": {
                "count": pruned["blind_spots"]["count"],
                "area_pct": pruned["blind_spots"]["area_pct"],
                "area_km2": pruned["blind_spots"]["area_km2"],
                "levels": _level_histogram(pruned["blind_spots"].get("blocks") or []),
                "missing": sorted(
                    {
                        str(item)
                        for block in (pruned["blind_spots"].get("blocks") or [])
                        for item in (block.get("missing") or [])
                    }
                ),
            },
            "usage": pruned["usage"],
            "summary": pruned["summary"],
        },
    }


def _level_histogram(blocks: list[dict[str, Any]]) -> dict[str, int]:
    """`blind` / `insufficient` 各几块（FR-221 与 FR-224 两种口径分开计数）。"""
    out: dict[str, int] = {}
    for block in blocks:
        level = str(block.get("level") or "unknown")
        out[level] = out.get(level, 0) + 1
    return out


def sample_path(settings: Any, community: SampleCommunity) -> Path:
    return Path(settings.samples_dir) / f"{community.id}.json"


async def cmd_generate(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        communities, problems = load_samples(settings)
    except SampleConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"[warn] {problem}")

    out_dir = Path(settings.samples_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for community in communities:
        payload = await generate_one(community)
        target = sample_path(settings, community)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        baseline = payload["baseline"]
        print(
            f"[ok] {community.id:26} -> {target.name}  "
            f"总分 {baseline['score']['total']:>6} ({baseline['score']['grade']})  "
            f"盲区 {baseline['blind_spots']['count']} 块 / {baseline['blind_spots']['area_pct']}%  "
            f"圈内合计 {sum(baseline['counts_in'].values())}"
        )
    print(f"\n共生成 {len(communities)} 份基线到 {out_dir}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """只校验：清单可读、基线齐全、基线里的形态与清单一致。**不出网、不写文件**。"""
    settings = get_settings()
    try:
        communities, problems = load_samples(settings)
    except SampleConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"[warn] {problem}")

    failures: list[str] = []
    for community in communities:
        target = sample_path(settings, community)
        if not target.is_file():
            failures.append(f"{community.id}: 缺少基线文件 {target.name}（跑 gen_samples.py 生成）")
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{community.id}: 基线 {target.name} 读不动（{type(exc).__name__}）")
            continue
        recorded = (payload.get("community") or {}).get("archetype")
        if recorded != community.archetype:
            failures.append(
                f"{community.id}: 基线的 archetype={recorded!r} 与清单的 "
                f"{community.archetype!r} 不一致（改过形态就要重新生成）"
            )
        version = (payload.get("generator") or {}).get("version")
        if version != GENERATOR_VERSION:
            failures.append(
                f"{community.id}: 基线由生成器 v{version} 产出，当前是 v{GENERATOR_VERSION}"
            )
        print(f"[ok] {community.id:26} {target.name}")

    if failures:
        print("\n[FAIL] 基线校验未通过：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"\n{len(communities)} 份基线齐全且与清单一致")
    return 0


# --------------------------------------------------------------------------------------
# --verify：用真实接口核验坐标并回填 samples.yaml
# --------------------------------------------------------------------------------------
async def cmd_verify(args: argparse.Namespace) -> int:
    """逐社区走地理编码 + 逆地理，把取到的坐标与结构化地址回填 `config/samples.yaml`。

    🔴 这一步**必须配好服务端 AK**，也是 PRD §10-1 规程②要求的那个动作
    （「坐标一律不凭记忆写死」）。没有 AK 时它会明确失败，而不是「看起来成功了」。
    """
    import yaml
    from services.geo import GeoService

    settings = get_settings()
    if not settings.has_server_ak:
        print(
            "[FAIL] 未配置 BAIDU_SERVER_AK，无法核验坐标。\n"
            "       请先在 .env 里填服务端类型 AK（与浏览器端 AK 不同，混用会报 status=203）。",
            file=sys.stderr,
        )
        return 2
    try:
        communities, _ = load_samples(settings)
    except SampleConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    service = GeoService(settings)
    updates: dict[str, dict[str, Any]] = {}
    for community in communities:
        query = args.query_template.format(name=community.name.split("（")[0], city=community.city)
        outcome = await service.geocode(query, city=community.city)
        payload = outcome.as_dict()
        lnglat = payload.get("lnglat")
        if not lnglat:
            print(
                f"[FAIL] {community.id}: 地理编码未返回坐标"
                f"（{payload.get('degraded_reason') or payload.get('message')}）",
                file=sys.stderr,
            )
            return 1
        regeo = (await service.regeo(f"{lnglat[0]},{lnglat[1]}")).as_dict()
        updates[community.id] = {
            "lnglat": [round(float(lnglat[0]), 6), round(float(lnglat[1]), 6)],
            "district": regeo.get("district") or community.district,
            "town": regeo.get("town"),
            "formatted_address": regeo.get("formatted_address"),
            "coord_verified": True,
        }
        print(
            f"[ok] {community.id:26} {query} -> {updates[community.id]['lnglat']}  "
            f"({regeo.get('district')} {regeo.get('town') or ''})"
        )

    if args.dry_run:
        print("\n--dry-run：只打印，未改写 config/samples.yaml")
        print(json.dumps(updates, ensure_ascii=False, indent=2))
        return 0

    path = Path(settings.samples_config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for entry in raw.get("communities") or []:
        patch = updates.get(str(entry.get("id")))
        if not patch:
            continue
        entry["lnglat"] = patch["lnglat"]
        entry["coord_verified"] = True
        if patch.get("town"):
            entry["district"] = patch["district"]
        entry["note"] = (
            f"坐标已实调核验：{patch['formatted_address']}"
            f"（核验于 {datetime.now(UTC).date().isoformat()}）"
        )
    raw["verified_at"] = datetime.now(UTC).date().isoformat()
    path.write_text(
        "# 本文件由 tools/gen_samples.py --verify 回填过坐标，请人工复核后提交。\n"
        + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\n已回填 {path}")
    print("⚠️ 人工复核坐标无误后再提交；提交前请重新运行 `gen_samples.py` 刷新基线。")
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_samples.py",
        description="CircleDoc 合成示例数据集生成器（FR-412）。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验清单与基线（不出网、不写文件）")
    mode.add_argument(
        "--verify", action="store_true", help="用地理编码核验坐标并回填 config/samples.yaml"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="配合 --verify：只打印结果，不改写 samples.yaml",
    )
    parser.add_argument(
        "--query-template",
        default="{name}",
        help="配合 --verify：送给地理编码的查询串模板，可用 {name} / {city}（默认 {name}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("INFO", json_format=False)
    if args.check:
        return cmd_check(args)
    if args.verify:
        return asyncio.run(cmd_verify(args))
    return asyncio.run(cmd_generate(args))


if __name__ == "__main__":
    raise SystemExit(main())
