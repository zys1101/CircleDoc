"""services/score.py —— M4「体检评分」：把六维得分加权成总分与等级，并生成 FR-304 摘要。

对应 PRD §4.5（评分模型）、§4.7 的 `score` / `summary` 两段、FR-301~FR-304。

本模块是**纯函数层**（PRD §5「算法层为纯函数、外部依赖可注入 mock」）：除了
`load_score_config()` 读一次 YAML 之外没有任何 I/O、没有网络、没有全局状态。
给定同一份 `coverage` + `blind_spots`，输出恒定 —— 这是 FR-301「分数可复算」的定义，
`tests/test_score.py` 用「打乱输入顺序 + 重复调用」两条断言钉住它。

🔴 PRD §4.3 与 §4.5 之间有一个**真实缺口**，本模块必须显式裁决而不是悄悄糊过去：
§4.3 的权重表是 20+15+20+15+15+10 = **95 %**，不是 100 %；而 §4.5 的等级线
`A ≥ 85 / B 70~84 / C 55~69 / D < 55` 是写在 0~100 刻度上的。两种读法都自洽：

* **不归一**（照 §4.5 字面 `总分 = Σ(S_i × w_i)`）：满分只有 95，A 档实际要求
  「达到可达上限的 89.5 %」。
* **归一**（除以计分权重之和 0.95）：满分 100，A 档就是「达到 85 %」。

`services/poi_categories.py` 在词表层**刻意拒绝**替 M4 做这个决定（它的
`SCORED_WEIGHT_SUM` 注释写明「归一与否会改变每一个维度的最终得分，属于 M4 评分层的产品裁决」）。
本模块的裁决是：**默认归一**，理由见 `ScoreConfig.normalize_weights`。为了让裁决可被审计、
也可被推翻，响应里**同时**给出 `total`（用于评级的那个）、`total_raw`（§4.5 字面值）、
`weight_sum` 与 `grade_basis` —— 评审按哪种读法都能在自己的口径上复算，不会看到「一个
对不上的数」而无法判断是谁错了。

盲区惩罚（§4.5 末行）按 `level == "blind"` 的面积占比算，与
`services/blind_spots.py` 的口径一致：`area_pct` 已只统计完全盲区，不把「配套不足」混进来。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.coverage import canonical_dimension_key

__all__ = [
    "DEFAULT_GRADE_BANDS",
    "PENALTY_CAP",
    "PENALTY_PER_PCT",
    "STRONG_SCORE",
    "WEAK_SCORE",
    "ScoreConfig",
    "aggregate_dimensions",
    "blind_spot_penalty",
    "build_score_block",
    "build_summary",
    "grade_for",
    "load_score_config",
    "weighted_total",
]

# --------------------------------------------------------------------------------------
# 默认参数（PRD §4.5）
# --------------------------------------------------------------------------------------
DEFAULT_GRADE_BANDS: tuple[tuple[str, float], ...] = (
    ("A", 85.0),
    ("B", 70.0),
    ("C", 55.0),
    ("D", 0.0),
)
"""等级线，**从高到低**排列（`grade_for()` 取第一个满足 `total >= 下界` 的档）。
写成有序元组而不是 dict：dict 在 py3.7+ 虽然有序，但「顺序即语义」这件事要一眼可见。"""

PENALTY_PER_PCT = 0.5
"""每 1 % 圈内面积被标记为完全盲区，总分扣 0.5（PRD §4.5 末行）。"""

PENALTY_CAP = 15.0
"""盲区惩罚上限（PRD §4.5 末行「最多 −15」）。"""

STRONG_SCORE = 85.0
"""摘要里判「最强项」的门槛：低于这条线就不说「充足」，改说「相对最好」。"""

WEAK_SCORE = 60.0
"""摘要里判「明显偏弱」的门槛。"""


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoreConfig:
    """评分参数（可由 `config/weights.yaml` 覆盖，PRD §4.5 要求权重表外置）。

    `normalize_weights` 是上面模块 docstring 里那个缺口的开关，默认 **True**：

    取 True 的理由是「等级线写在 0~100 刻度上」。§4.5 同时给出了公式与 A/B/C/D 四条线，
    若按字面不归一，满分 95 却用 85 当 A 线，等价于隐含要求「89.5 % 才算 A」——这个 89.5 %
    在任何一处文档里都没写过，是**从缺口里长出来的第二个隐含假设**。归一之后 A 线就是字面的
    「85 分」，与 §4.5 自洽；代价是 §4.3 表里的 20/15/20/15/15/10 被当作**相对权重**而非绝对百分比，
    这也正是「权重」一词的常规读法。

    ⚠️ 这不是「把 PRD 的错悄悄改掉」：`total_raw` 与 `weight_sum` 一并下发，README 与
    `docs/DESIGN.md` 都写明该缺口与本次裁决。

    要改回字面口径有两条路，**都不需要改代码**：
      1. `config/weights.yaml` 里把 `normalize_weights` 设成 `false`（推荐，权重表本来就在那）；
      2. 直接构造 `ScoreConfig(normalize_weights=False)` 传给 `build_score_block()`。
    ⚠️ 注意**没有** `SCORE_NORMALIZE_WEIGHTS` 这个环境变量 —— 本字段不由
    `infra/config.py:Settings` 提供，`.env` 改了不会有任何效果。
    """

    normalize_weights: bool = True
    grade_bands: tuple[tuple[str, float], ...] = DEFAULT_GRADE_BANDS
    penalty_per_pct: float = PENALTY_PER_PCT
    penalty_cap: float = PENALTY_CAP
    t_ideal_ratio: float = 0.5
    t_ideal_minutes: Mapping[str, float] = field(default_factory=dict)
    dimension_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """逐维度的 `weight` / `threshold_count` / `threshold_minutes`，键已收口成
    `services/coverage.py:canonical_dimension_key()` 的规范键。

    调用方把它交给 `build_coverage(threshold_overrides=...)`。**这是
    `config/weights.yaml` 真正起作用的那条线** —— 只把权重写进 YAML 却没人读它，
    等于把「权重表外置」做成一句注释；PRD §4.5 要的是可替换，不是可阅读。"""

    summary_max: int = 5
    """FR-304 要求 3~5 条结论，上界取 5。"""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any) -> float | None:
    """宽容地把上游给的值转成 float；bool 与不可解析值一律回 None（不猜）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def load_score_config(path: str | Path | None = None) -> tuple[ScoreConfig, list[str]]:
    """读 `config/weights.yaml`；返回 `(配置, 问题清单)`。

    文件缺失 / 读不动 / 格式错 → **回落到数据类默认值并把原因写进问题清单**，不抛异常。
    理由与 FR-403 同一条：演示现场一个配错的权重文件不该让整个体检变成错误页，
    而「用了默认权重」这件事必须可被看见（清单会进响应的 `score.notes`）。

    YAML 依赖来自 `uvicorn[standard]`；若环境里确实没有 pyyaml，同样只记一条问题不改行为。
    """
    problems: list[str] = []
    if path is None:
        from infra.config import ROOT_DIR

        path = Path(ROOT_DIR) / "config" / "weights.yaml"
    target = Path(path)
    if not target.is_file():
        return ScoreConfig(), [f"未找到权重表 {target}，已使用内置默认值（PRD §4.3 原表）。"]
    try:
        import yaml
    except ImportError:  # pragma: no cover - 只在缺 pyyaml 的环境触发
        return ScoreConfig(), ["环境缺少 pyyaml，无法读取权重表，已使用内置默认值。"]
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return ScoreConfig(), [
            f"权重表 {target} 读取失败（{type(exc).__name__}），已使用内置默认值。"
        ]
    if not isinstance(raw, Mapping):
        return ScoreConfig(), [f"权重表 {target} 顶层不是映射，已使用内置默认值。"]

    bands_raw = raw.get("grades")
    bands = DEFAULT_GRADE_BANDS
    if isinstance(bands_raw, Mapping) and bands_raw:
        parsed: list[tuple[str, float]] = []
        for grade, lower in bands_raw.items():
            value = _as_float(lower)
            if value is None:
                problems.append(f"等级线 {grade} 的值 {lower!r} 不是数字，已忽略该项。")
                continue
            parsed.append((str(grade), value))
        if parsed:
            bands = tuple(sorted(parsed, key=lambda item: item[1], reverse=True))
        else:
            problems.append("等级线一项都没解析成功，已使用内置默认值。")

    penalty_raw = raw.get("blind_spot_penalty")
    penalty = penalty_raw if isinstance(penalty_raw, Mapping) else {}
    t_ideal_raw = raw.get("t_ideal")
    t_ideal = t_ideal_raw if isinstance(t_ideal_raw, Mapping) else {}
    overrides: dict[str, float] = {}
    for key, value in (t_ideal.get("minutes") or {}).items():
        parsed_value = _as_float(value)
        if parsed_value is not None:
            overrides[canonical_dimension_key(key)] = parsed_value

    dimension_overrides, dimension_problems = _parse_dimensions(raw.get("dimensions"))
    problems.extend(dimension_problems)

    normalize = raw.get("normalize_weights")
    return (
        ScoreConfig(
            normalize_weights=True if normalize is None else bool(normalize),
            grade_bands=bands,
            penalty_per_pct=_as_float(penalty.get("per_pct")) or PENALTY_PER_PCT,
            penalty_cap=_as_float(penalty.get("cap")) or PENALTY_CAP,
            t_ideal_ratio=_as_float(t_ideal.get("ratio")) or 0.5,
            t_ideal_minutes=overrides,
            dimension_overrides=dimension_overrides,
            summary_max=int(_as_float(raw.get("summary_max")) or 5),
        ),
        problems,
    )


def _parse_dimensions(
    raw: Any,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """把 YAML 的 `dimensions:` 段收成 `build_coverage(threshold_overrides=...)` 的形状。

    只认三个真正影响得分的字段（`weight` / `threshold_count` / `threshold_minutes`）：
    `label` 与 `scored` 由词表层（`services/poi_categories.py`）裁决，从配置文件再传一份
    会让「参考项不计分」这条口径出现第二个出处。

    键在这里就收口成规范键（`market` → `shopping`）：不收口的话，
    `_threshold_for()` 拿到的是 POI 层的键、配置里写的是另一个键，两边静默失配 ——
    表现是「改了权重但分数没变」，报告里完全看不出来。
    """
    problems: list[str] = []
    if not isinstance(raw, Mapping) or not raw:
        return {}, problems
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            problems.append(f"权重表的维度 {key!r} 不是映射，已忽略。")
            continue
        entry: dict[str, Any] = {}
        weight = _as_float(value.get("weight"))
        if weight is not None:
            entry["weight"] = weight
        count = _as_float(value.get("threshold_count"))
        if count is not None:
            entry["threshold_count"] = int(count)
        minutes = _as_float(value.get("threshold_minutes"))
        if minutes is not None:
            entry["threshold_minutes"] = minutes
        if not entry:
            problems.append(f"权重表的维度 {key!r} 没有任何可识别字段，已忽略。")
            continue
        out[canonical_dimension_key(key)] = entry
    return out, problems


# --------------------------------------------------------------------------------------
# 单维度聚合
# --------------------------------------------------------------------------------------
def aggregate_dimensions(
    coverage: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """把 `build_coverage()` 的 `groups` 收成评分用的维度列表（**只读、不改上游**）。

    只认 `scored=True` 的维度进总分与雷达图（FR-302 的「6 维度」）；参考项照样出现在
    返回里并带 `scored=False`，供明细表使用 —— 把它算进总分会让六维权重表失去意义
    （PRD §4.3「不计分、仅列入明细表」）。
    """
    groups = list((coverage or {}).get("groups") or ())
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(groups):
        group = dict(raw or {})
        key = str(group.get("key") or group.get("category") or f"group_{index}").strip()
        score = _as_float(group.get("score"))
        weight = _as_float(group.get("weight")) or 0.0
        # 上游没给 scored 时以权重反推：权重为 0 即不计分（与 `poi_categories` 的自检同一条判据）。
        scored_raw = group.get("scored")
        scored = (weight > 0.0) if scored_raw is None else bool(scored_raw)
        out.append(
            {
                "key": key,
                "label": str(group.get("label") or key),
                "score": 0.0 if score is None else round(score, 2),
                "score_available": score is not None,
                "weight": round(weight, 4),
                "scored": scored,
                "count_in": int(_as_float(group.get("count_in")) or 0),
                "threshold_count": int(_as_float(group.get("threshold_count")) or 0),
                "nearest_minutes": _as_float(group.get("nearest_minutes")),
                "nearest_reason": group.get("nearest_reason"),
                "measured_count": int(_as_float(group.get("measured_count")) or 0),
                "unmeasured_count": int(_as_float(group.get("unmeasured_count")) or 0),
            }
        )
    return out


def weighted_total(
    dimensions: Sequence[Mapping[str, Any]],
    *,
    normalize: bool = True,
) -> dict[str, Any]:
    """`Σ(S_i × w_i)`，并如实报告权重之和与归一化与否。

    `weight_sum` 为 0（全部维度都不计分 / 上游没给权重）时不归一、总分记 0 并留一条 note：
    除以 0 会得到 inf，静默变成「满分」是最坏的一种失败。
    """
    scored = [item for item in dimensions if item.get("scored")]
    weight_sum = round(sum(float(item.get("weight") or 0.0) for item in scored), 6)
    raw = sum(float(item.get("score") or 0.0) * float(item.get("weight") or 0.0) for item in scored)
    notes: list[str] = []
    if weight_sum <= 0.0:
        total = 0.0
        notes.append("计分维度权重之和为 0，无法加权；总分按 0 记（不做除零，也不假充满分）。")
        normalized = False
    elif normalize:
        total = raw / weight_sum
        normalized = True
    else:
        total = raw
        normalized = False
    if normalized and abs(weight_sum - 1.0) > 1e-6:
        notes.append(
            f"计分维度权重之和为 {weight_sum:g}（非 1），总分已按其归一化到 0~100；"
            "若要与 PRD §4.5 的字面公式对齐请读 `total_raw`。"
        )
    return {
        "total": round(total, 2),
        "total_raw": round(raw, 2),
        "weight_sum": weight_sum,
        "normalized": normalized,
        "notes": notes,
    }


# --------------------------------------------------------------------------------------
# 等级与惩罚
# --------------------------------------------------------------------------------------
def grade_for(total: float, bands: Sequence[tuple[str, float]] = DEFAULT_GRADE_BANDS) -> str:
    """把总分映射成 A/B/C/D（PRD §4.5）。`bands` 从高到低，取第一个满足的档。"""
    value = _as_float(total)
    if value is None:
        return "D"
    for grade, lower in bands:
        if value >= float(lower):
            return str(grade)
    return str(bands[-1][0]) if bands else "D"


def blind_spot_penalty(
    area_pct: float | None,
    *,
    per_pct: float = PENALTY_PER_PCT,
    cap: float = PENALTY_CAP,
) -> float:
    """盲区惩罚 = `-per_pct × area_pct`，下限 `-cap`（PRD §4.5 末行）。

    返回**负数或 0**（不是「要扣多少」的正数），这样调用方直接 `total + penalty` 即可，
    不必再记一次符号。`area_pct` 为 None / 负值 → 0（缺数据不扣分，也不奖励）。
    """
    value = _as_float(area_pct)
    if value is None or value <= 0.0:
        return 0.0
    limit = abs(float(cap))
    raw = -abs(float(per_pct)) * value
    return round(max(raw, -limit), 2)


# --------------------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------------------
def build_score_block(
    coverage: Mapping[str, Any] | None,
    blind_spots: Mapping[str, Any] | None = None,
    *,
    config: ScoreConfig | None = None,
    extra_notes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """PRD §4.7 的 `score` 段：`{total, grade, radar}` + 一组可审计的中间量。

    `radar` 只含计分维度（FR-302 的「6 维度」）；`dimensions` 含全部（含便民参考项），
    两者不要互相替代 —— 雷达图画参考项会让人误以为它进了总分。
    """
    cfg = config or ScoreConfig()
    dims = aggregate_dimensions(coverage)
    aggregation = weighted_total(dims, normalize=cfg.normalize_weights)
    notes: list[str] = list(aggregation["notes"])
    notes.extend(extra_notes or ())

    missing = [item["key"] for item in dims if item.get("scored") and not item["score_available"]]
    if missing:
        notes.append(
            f"维度 {', '.join(missing)} 没有得分（上游未提供），已按 0 分计入总分；"
            "这不是「该项满分」，报告里需与 nearest_reason 一起读。"
        )

    area_pct = _as_float((blind_spots or {}).get("area_pct"))
    penalty = blind_spot_penalty(area_pct, per_pct=cfg.penalty_per_pct, cap=cfg.penalty_cap)
    if penalty:
        notes.append(
            f"盲区惩罚：完全盲区占圈内面积 {area_pct:g} %，按每 1 % 扣 {cfg.penalty_per_pct:g} 分"
            f"计 {penalty:g} 分（上限 −{cfg.penalty_cap:g}）。"
        )

    total = _clamp(aggregation["total"] + penalty, 0.0, 100.0)
    radar = {item["key"]: item["score"] for item in dims if item.get("scored")}
    return {
        "total": round(total, 2),
        "grade": grade_for(total, cfg.grade_bands),
        "radar": radar,
        "penalty": penalty,
        "blind_area_pct": area_pct,
        # ---- 审计字段（§4.7 之外，只加键不改键）----
        "total_before_penalty": aggregation["total"],
        "total_raw": aggregation["total_raw"],
        "weight_sum": aggregation["weight_sum"],
        "normalized": aggregation["normalized"],
        "grade_basis": "normalized" if aggregation["normalized"] else "raw",
        "grade_bands": dict(cfg.grade_bands),
        "reference_only": [item["key"] for item in dims if not item.get("scored")],
        "dimensions": dims,
        "notes": notes,
    }


def build_summary(
    coverage: Mapping[str, Any] | None,
    blind_spots: Mapping[str, Any] | None = None,
    score: Mapping[str, Any] | None = None,
    *,
    minutes: float = 15.0,
    data_source: str | None = None,
    degraded_reason: str | None = None,
    max_items: int = 5,
) -> list[str]:
    """FR-304：由数据驱动地生成 3~5 条结论文本（**没有一句硬编码文案**）。

    生成顺序即重要性顺序：最弱项 → 最强项 → 盲区 → 未实测 → 数据来源。
    每条都从入参现算，因此同一份数据永远得到同一份结论（FR-301 可复算的另一面）。
    """
    dims = [item for item in aggregate_dimensions(coverage) if item["scored"]]
    lines: list[str] = []
    limit = max(3, min(5, int(max_items)))

    if not dims:
        # 一个计分维度都没有：这本身就是结论（上游整链降级），不能返回空数组让前端自己编。
        lines.append(
            "本次未取得任何计分维度的数据，无法给出分维度结论；"
            "请检查数据模式、AK 配置与 POI 检索是否全部降级。"
        )

    weakest = min(dims, key=lambda item: item["score"], default=None)
    strongest = max(dims, key=lambda item: item["score"], default=None)

    if weakest is not None:
        reason = weakest.get("nearest_reason")
        tail = f"（{reason}）" if reason else ""
        lines.append(
            f"最弱项：{weakest['label']} {weakest['score']:g} 分，圈内 {weakest['count_in']} 个、"
            f"达标阈值 {weakest['threshold_count']} 个{tail}，是本次最该优先补齐的方向。"
        )
    if strongest is not None and strongest is not weakest:
        verdict = "配套充足" if strongest["score"] >= STRONG_SCORE else "相对最好"
        lines.append(
            f"最强项：{strongest['label']} {strongest['score']:g} 分（{verdict}），"
            f"圈内 {strongest['count_in']} 个、达标阈值 {strongest['threshold_count']} 个。"
        )

    spots = blind_spots or {}
    count = int(_as_float(spots.get("count")) or 0)
    area_pct = _as_float(spots.get("area_pct")) or 0.0
    if count > 0:
        blocks = list(spots.get("blocks") or ())
        missing_keys: list[str] = []
        for block in blocks:
            for key in block.get("missing") or ():
                if str(key) not in missing_keys:
                    missing_keys.append(str(key))
        detail = f"，缺失类别：{', '.join(missing_keys)}" if missing_keys else ""
        lines.append(
            f"服务盲区：识别到 {count} 处，占圈内面积 {area_pct:g}%"
            f"（按 {spots.get('grid', {}).get('radius_m', 1000):g} m 半径判定）{detail}。"
        )
    else:
        lines.append(
            "服务盲区：本次未识别到完全盲区（三类基础设施至少各有一处可达），"
            "但仍需注意「配套不足」区域。"
        )

    unmeasured = sum(item["unmeasured_count"] for item in dims)
    measured = sum(item["measured_count"] for item in dims)
    no_nearest = [item["key"] for item in dims if item.get("nearest_minutes") is None]
    if unmeasured or no_nearest:
        detail = f"；其中 {', '.join(no_nearest)} 无任何实测值，时效分按 0 计" if no_nearest else ""
        lines.append(
            f"实测覆盖：{measured} 个点位有实测时间、{unmeasured} 个未实测"
            f"（前 K 截断 / 圈外 / 探测器不可用）{detail}；未实测点位不参与时效评分。"
        )

    if data_source and data_source != "live":
        layer = {"sample": "程序合成的示例数据", "cache": "本地缓存"}.get(
            str(data_source), str(data_source)
        )
        suffix = f"（{degraded_reason}）" if degraded_reason else ""
        lines.append(
            f"数据来源：本次结论基于{layer}{suffix}，不是实时路网与实时检索结果，"
            f"仅作辅助性量化参考。"
        )
    else:
        lines.append(
            f"数据来源：实时路网与实时检索（目标时长 {minutes:g} min）。"
            "结论为辅助性量化参考，不构成规划审批依据。"
        )

    return lines[:limit]
