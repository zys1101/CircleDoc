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
    """按社区坐标与形态跑一次 **mock** 体检，返回冻结后的基线对象。

    🔴 **必须关掉缓存**（`cache_enabled=False`）。原因：三层降级链是
    `live → cache → sample`，而 `data_mode="mock"` 只表示「不出网」，**不表示「不读缓存」**
    —— `services/poi_search.py:_resolve_dimension` 在进入 mock 分支之前照样先读一次本地缓存。
    于是同一台机器上：跑过 live 的人生成出来的基线混进了**真实 POI**，
    没跑过的人生成出来的是**纯合成 POI**，两者 counts 与盲区面积完全不同。

    基线是**回归夹具**（`tests/test_samples_baseline.py` 逐字段比对），
    它的契约必须是「在没有任何真实数据的环境下，mock 长什么样」。所以生成时必须关缓存。
    这**不改运行期语义**，只影响本生成器。

    （实测踩过：全量测试跑到基线比对会失败、单独跑该文件却通过 —— 差别就是前面那些
    live 用例往同一个 SQLite 里写了缓存。）
    """
    baseline_settings = get_settings().model_copy(update={"cache_enabled": False})
    report = await run_analysis(
        list(community.lnglat),
        modes=["walking"],
        minutes=15.0,
        data_mode="mock",
        platform="desktop",
        archetype=community.archetype,
        settings=baseline_settings,
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
    """逐社区核验中心点坐标，回填 `config/samples.yaml`（PRD §10-1 规程②）。

    🔴 这一步**必须配好服务端 AK**，也是 PRD §10-1 规程②要求的那个动作
    （「坐标一律不凭记忆写死」）。没有 AK 时它会明确失败，而不是「看起来成功了」。

    🔴 **必须显式传 `data_mode="live"`**。原因是个容易踩的坑：`data_mode=auto` 要等
    `/api/health` 的启动探测才解析成具体值，而**命令行进程从来不会跑那个探测**
    （`probed_data_mode` 只活在跑过探测的那个进程里，不落盘）。于是 `Settings.data_mode_in_effect`
    在这个 CLI 里永远是保守兜底的 `mock`，每一次地理编码都会得到 `degraded_reason=mock_mode`
    —— 表现是「明明配了 AK，这个工具却永远说拿不到坐标」。这不是配额问题，是取数模式没打开。

    🔴 **两条核验路径，且必须校验结果质量**（这是第一版最严重的错误）：

    第一版只走地理编码、且**只要 `lnglat` 非空就标 `coord_verified: true`**。实测跑出来：

    | 查询 | 拿到的坐标 | 行政区 | 官方 `level` | 判定 |
    | --- | --- | --- | --- | --- |
    | 交道口街道 | 116.4073, 39.9422 | 东城区 | **乡镇** | ✅ 真的解析到了 |
    | 天通苑 | 116.4134, 39.9109 | **东城区东华门** | **城市** | ❌ 退化到市中心 |
    | 肖家河 | 116.4134, 39.9109 | **东城区东华门** | **城市** | ❌ 与上一条完全相同 |

    「天通苑」在昌平区，却被解析到东城区的市政府地址；「肖家河」拿到一个**一模一样**的坐标。
    只检查「非空」会把这三条一起标成「已核验」—— 那是在核验记录上撒谎，比不核验更糟。

    所以现在的规则是：

    1. **优先走 `/api/suggest`**（PRD §10-1 明文指定的路径，也是唯一能给出 POI 级
       精度的接口）。它默认关闭（FR-108），需 `SUGGEST_ENABLED=true`；每次调用占 1 次
       地点检索配额（与 `/api/place` 共用 100 次/日），工具会把消耗如实报出来。
    2. **地理编码只作兜底**，且查询串必须带上城市与行政区（`北京市昌平区天通苑北街道`），
       并**拒绝 `level == "城市"`** 的结果 —— 那是「没解析到具体地点」的官方信号。
    3. **行政区交叉核对**：最终坐标的 `district` 必须与清单里写的行政区一致，
       不一致就拒绝该社区并说明。这一条正是能挡住上面那个「天通苑 → 东城区」的检查。
    4. 任何一条拒绝，整次核验就**不改文件**并返回非零退出码。
    """
    import yaml
    from services.geo import GeoService, suggest_enabled

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

    # `data_mode="live"` 是本工具能工作的前提，理由见 docstring。
    live_mode = "live"
    use_suggest = suggest_enabled(settings)
    if not use_suggest:
        print(
            "[warn] /api/suggest 未启用（FR-108 默认关闭）。将只走地理编码兜底，"
            "并拒绝解析到「城市」级的结果 —— 那通常意味着拿不到社区级坐标。\n"
            "       想用 PRD 指定的 suggest 路径：先 `SUGGEST_ENABLED=true` 再跑本命令。"
        )

    service = GeoService(settings)
    updates: dict[str, dict[str, Any]] = {}
    rejects: list[str] = []
    place_calls = 0

    for community in communities:
        bare = community.name.split("（")[0].strip()
        picked: dict[str, Any] | None = None
        source = ""

        if use_suggest:
            outcome = await service.suggest(bare, region=community.city, data_mode=live_mode)
            payload = outcome.as_dict()
            place_calls += int((payload.get("usage") or {}).get("place_calls") or 0)
            # 候选排序：**精确同名 > 以关键字开头 > 名字更短 > 原始顺序**。
            # 为什么不是「取第一条」：百度的排序对「天通苑」把「天通苑-地铁站」排在
            # 「天通苑」住宅区之前，而本清单要的是**社区中心点**不是交通设施 ——
            # 精确同名那条才是居住区本身。名字更短作为次级判据，是因为长名字通常是
            # 「XX店」「XX-道路」这类具体设施，比社区整体偏一些。
            candidates = []
            for order, item in enumerate(payload.get("items") or []):
                district = str(item.get("district") or item.get("area") or "")
                lnglat = item.get("lnglat") or item.get("location")
                if not lnglat:
                    continue
                # 同名地点很多：只认行政区对得上的那一条（例如「天通苑」在别的城市也有）。
                if community.district and community.district not in district:
                    continue
                name = str(item.get("name") or "")
                rank = (
                    0 if name == bare else 1 if name.startswith(bare) else 2,
                    len(name),
                    order,
                )
                candidates.append((rank, item, district, lnglat, name))
            if candidates:
                candidates.sort(key=lambda entry: entry[0])
                _, item, district, lnglat, name = candidates[0]
                picked = {
                    "lnglat": [round(float(lnglat[0]), 6), round(float(lnglat[1]), 6)],
                    "name": name,
                    "district": district,
                    "level": "poi",
                }
                source = f"suggest:{name}"
                if len(candidates) > 1:
                    print(
                        f"       （{community.id}: {len(candidates)} 条同区候选，"
                        f"按精确同名优先选中「{name}」）"
                    )
            if picked is None:
                print(f"[warn] {community.id}: suggest 无匹配候选，改用地理编码兜底。")

        if picked is None:
            query = args.query_template.format(
                name=bare, city=community.city, district=community.district or ""
            )
            outcome = await service.geocode(query, city=community.city, data_mode=live_mode)
            payload = outcome.as_dict()
            lnglat = payload.get("lnglat")
            level = str(payload.get("level") or "")
            if not lnglat:
                rejects.append(
                    f"{community.id}: 地理编码未返回坐标"
                    f"（{payload.get('degraded_reason') or payload.get('message')}）"
                )
                continue
            # 🔴 「城市」级 = 没解析到具体地点，坐标是市中心，不是这个社区。
            if level == "城市":
                rejects.append(
                    f"{community.id}: 「{query}」只解析到**城市**级（confidence="
                    f"{payload.get('confidence')}），坐标是市中心而不是该社区；"
                    "请改用更精确的查询串（带上行政区/街道）或开启 SUGGEST_ENABLED。"
                )
                continue
            picked = {
                "lnglat": [round(float(lnglat[0]), 6), round(float(lnglat[1]), 6)],
                "name": query,
                "district": "",
                "level": level or "?",
            }
            source = f"geocode:{level}"

        # 逆地理：既取结构化地址，也用来交叉核对行政区。
        regeo = (
            await service.regeo(f"{picked['lnglat'][0]},{picked['lnglat'][1]}", data_mode=live_mode)
        ).as_dict()
        actual_district = str(regeo.get("district") or "")

        if community.district and actual_district and community.district != actual_district:
            rejects.append(
                f"{community.id}: 行政区不符 —— 清单写「{community.district}」，"
                f"实测落在「{actual_district}」（{regeo.get('formatted_address')}）。"
                "这通常意味着查询串解析到了别的同名地点，必须换查询词。"
            )
            continue

        updates[community.id] = {
            "lnglat": picked["lnglat"],
            "district": actual_district or community.district,
            "town": regeo.get("town"),
            "formatted_address": regeo.get("formatted_address"),
            "matched": picked.get("name"),
            "level": picked.get("level"),
            "source": source,
        }
        print(
            f"[ok] {community.id:26} {str(picked.get('name'))[:22]:22} "
            f"-> {picked['lnglat']}  ({actual_district} {regeo.get('town') or ''}) [{source}]"
        )

    print(f"\n本次核验消耗：地点检索 {place_calls} 次（个人档 100 次/日，与 /api/place 共用）")

    if rejects:
        print("\n[FAIL] 有社区未能核验，**未改写任何文件**：", file=sys.stderr)
        for item in rejects:
            print(f"  - {item}", file=sys.stderr)
        return 1
    if len(updates) != len(communities):
        print("[FAIL] 核验结果不完整，未改写任何文件。", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run：只打印，未改写 config/samples.yaml")
        print(json.dumps(updates, ensure_ascii=False, indent=2))
        return 0

    path = Path(settings.samples_config_path)
    original = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(original) or {}
    for entry in raw.get("communities") or []:
        patch = updates.get(str(entry.get("id")))
        if not patch:
            continue
        entry["lnglat"] = patch["lnglat"]
        entry["coord_verified"] = True
        if patch.get("district"):
            entry["district"] = patch["district"]
        entry["note"] = (
            f"坐标已实调核验（{patch['source']}）：匹配到「{patch.get('matched')}」，"
            f"逆地理确认落在 {patch['district']} {patch.get('town') or ''}"
            f"（{patch['formatted_address']}）。核验于 {datetime.now(UTC).date().isoformat()}。"
        )
    raw["verified_at"] = datetime.now(UTC).date().isoformat()
    # ⚠️ `yaml.safe_dump` 会把本文件里那大段「规程说明」注释全部抹掉，而那段注释正是
    #    `coord_verified` 纪律的出处。所以备份原文，回填后提示人工合并。
    backup = path.with_suffix(".yaml.bak")
    backup.write_text(original, encoding="utf-8")
    path.write_text(
        "# ⚠️ 本文件由 tools/gen_samples.py --verify 回填过坐标；"
        f"改写前的原文备份在 {backup.name}。\n"
        "# yaml.safe_dump 会丢掉注释，请把原文顶部的「规程说明」段落合并回来再提交。\n"
        + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\n已回填 {path}")
    print(f"⚠️ 原文（含规程说明注释）备份为 {backup}")
    print("⚠️ 提交前请：① 把注释合并回 samples.yaml；② 重新跑 `gen_samples.py` 刷新基线。")
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
