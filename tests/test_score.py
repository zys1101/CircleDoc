"""`services/score.py` 单测：权重缺口裁决、等级线、盲区惩罚、摘要生成与配置加载。

对齐 PRD §4.5 / §4.7 / FR-301 / FR-302 / FR-304，以及 §5「算法层为纯函数」。
全部零出网、零文件系统副作用（`load_score_config` 用 `tmp_path`）。
"""

from __future__ import annotations

import json

import pytest
from services.score import (
    DEFAULT_GRADE_BANDS,
    PENALTY_CAP,
    ScoreConfig,
    aggregate_dimensions,
    blind_spot_penalty,
    build_score_block,
    build_summary,
    grade_for,
    load_score_config,
    weighted_total,
)

# PRD §4.3 的六个计分维度（权重合计 0.95 —— 这正是那个真实缺口）
PRD_WEIGHTS: dict[str, float] = {
    "medical": 0.20,
    "education": 0.15,
    "shopping": 0.20,
    "leisure": 0.15,
    "transit": 0.15,
    "food": 0.10,
}

THRESHOLDS: dict[str, tuple[int, float]] = {
    "medical": (2, 8.0),
    "education": (1, 12.0),
    "shopping": (3, 8.0),
    "leisure": (1, 10.0),
    "transit": (2, 5.0),
    "food": (5, 10.0),
}


def make_group(
    key: str,
    score: float,
    *,
    weight: float | None = None,
    scored: bool | None = None,
    count_in: int = 3,
    nearest_minutes: float | None = 5.0,
    nearest_reason: str | None = None,
    measured: int = 2,
    unmeasured: int = 0,
) -> dict:
    """造一个 `build_coverage()` 形状的 group。"""
    count_threshold, minute_threshold = THRESHOLDS.get(key, (0, 10.0))
    group: dict = {
        "category": key,
        "key": key,
        "label": {"medical": "基础医疗", "education": "教育", "shopping": "购物市场"}.get(key, key),
        "score": score,
        "count_in": count_in,
        "count_near": 1,
        "count_total": count_in + 1,
        "threshold_count": count_threshold,
        "threshold_minutes": minute_threshold,
        "nearest_minutes": nearest_minutes,
        "nearest_reason": nearest_reason,
        "measured_count": measured,
        "unmeasured_count": unmeasured,
        "weight": PRD_WEIGHTS.get(key, 0.0) if weight is None else weight,
    }
    if scored is not None:
        group["scored"] = scored
    return group


def make_coverage(groups: list[dict]) -> dict:
    return {"groups": groups, "usage": {}, "totals": {}, "meta": {}, "notes": []}


def full_coverage(score: float = 80.0) -> dict:
    """六个计分维度同分 + 一个不计分的便民参考项。"""
    groups = [make_group(key, score) for key in PRD_WEIGHTS]
    groups.append(make_group("convenience", 100.0, weight=0.0, scored=False))
    return make_coverage(groups)


# --------------------------------------------------------------------------------------
# grade_for
# --------------------------------------------------------------------------------------
def test_grade_boundaries_are_inclusive_at_the_lower_bound() -> None:
    assert grade_for(100.0) == "A"
    assert grade_for(85.0) == "A"  # PRD §4.5 写 `A ≥ 85`
    assert grade_for(84.99) == "B"
    assert grade_for(70.0) == "B"  # `B 70~84`
    assert grade_for(69.99) == "C"
    assert grade_for(55.0) == "C"  # `C 55~69`
    assert grade_for(54.99) == "D"
    assert grade_for(0.0) == "D"


def test_grade_for_handles_garbage_without_raising() -> None:
    assert grade_for(None) == "D"  # type: ignore[arg-type]
    assert grade_for("not-a-number") == "D"  # type: ignore[arg-type]


def test_grade_bands_are_high_to_low() -> None:
    lowers = [lower for _, lower in DEFAULT_GRADE_BANDS]
    assert lowers == sorted(lowers, reverse=True)
    assert [grade for grade, _ in DEFAULT_GRADE_BANDS] == ["A", "B", "C", "D"]


# --------------------------------------------------------------------------------------
# blind_spot_penalty
# --------------------------------------------------------------------------------------
def test_penalty_is_negative_and_prorated() -> None:
    assert blind_spot_penalty(6.4) == pytest.approx(-3.2)  # 6.4 % × 0.5
    assert blind_spot_penalty(10.0) == pytest.approx(-5.0)


def test_penalty_is_capped_at_minus_15() -> None:
    assert blind_spot_penalty(30.0) == pytest.approx(-PENALTY_CAP)
    assert blind_spot_penalty(100.0) == pytest.approx(-15.0)


def test_penalty_of_missing_or_zero_area_is_zero() -> None:
    assert blind_spot_penalty(None) == 0.0
    assert blind_spot_penalty(0.0) == 0.0
    assert blind_spot_penalty(-3.0) == 0.0  # 负占比是坏数据，不奖励


# --------------------------------------------------------------------------------------
# aggregate_dimensions
# --------------------------------------------------------------------------------------
def test_reference_dimension_is_kept_but_not_scored() -> None:
    dims = aggregate_dimensions(full_coverage())
    by_key = {item["key"]: item for item in dims}
    assert by_key["convenience"]["scored"] is False
    assert by_key["convenience"]["weight"] == 0.0
    assert sum(1 for item in dims if item["scored"]) == 6  # FR-302 的「6 维度」


def test_scored_is_inferred_from_weight_when_absent() -> None:
    coverage = make_coverage(
        [make_group("medical", 90.0, weight=0.2), make_group("service_ref", 50.0, weight=0.0)]
    )
    by_key = {item["key"]: item for item in aggregate_dimensions(coverage)}
    assert by_key["medical"]["scored"] is True
    assert by_key["service_ref"]["scored"] is False


def test_missing_score_is_marked_unavailable_not_silently_zero() -> None:
    group = make_group("medical", 0.0)
    del group["score"]
    dims = aggregate_dimensions(make_coverage([group]))
    assert dims[0]["score"] == 0.0
    assert dims[0]["score_available"] is False


def test_empty_coverage_yields_no_dimensions() -> None:
    assert aggregate_dimensions(None) == []
    assert aggregate_dimensions({}) == []


# --------------------------------------------------------------------------------------
# weighted_total —— 权重缺口的两条口径
# --------------------------------------------------------------------------------------
def test_normalized_and_raw_reading_of_the_095_weight_gap() -> None:
    """六个维度全 100 分：字面口径满分 95，归一后满分 100。"""
    dims = aggregate_dimensions(full_coverage(100.0))
    normal = weighted_total(dims, normalize=True)
    literal = weighted_total(dims, normalize=False)
    assert normal["weight_sum"] == pytest.approx(0.95)  # PRD §4.3 的真实缺口
    assert literal["total"] == pytest.approx(95.0)  # §4.5 字面 `Σ S_i × w_i`
    assert normal["total"] == pytest.approx(100.0)  # 归一到 0~100 刻度
    assert normal["total_raw"] == pytest.approx(95.0)
    assert normal["normalized"] is True and literal["normalized"] is False
    assert any("归一化" in note for note in normal["notes"])


def test_zero_weight_sum_does_not_divide_by_zero() -> None:
    dims = [
        {
            "key": "x",
            "label": "x",
            "score": 100.0,
            "score_available": True,
            "weight": 0.0,
            "scored": True,
        }
    ]
    result = weighted_total(dims, normalize=True)
    assert result["total"] == 0.0
    assert result["normalized"] is False
    assert any("除零" in note for note in result["notes"])


# --------------------------------------------------------------------------------------
# build_score_block
# --------------------------------------------------------------------------------------
def test_score_block_radar_covers_only_scored_dimensions() -> None:
    block = build_score_block(full_coverage(80.0), {"count": 0, "area_pct": 0.0})
    assert set(block["radar"]) == set(PRD_WEIGHTS)
    assert "convenience" not in block["radar"]  # 参考项不进雷达
    assert block["reference_only"] == ["convenience"]
    assert block["total"] == pytest.approx(80.0)
    assert block["grade"] == "B"
    assert block["penalty"] == 0.0


def test_score_block_applies_blind_spot_penalty() -> None:
    no_blind = build_score_block(full_coverage(80.0), {"count": 0, "area_pct": 0.0})
    with_blind = build_score_block(full_coverage(80.0), {"count": 2, "area_pct": 6.4})
    assert with_blind["penalty"] == pytest.approx(-3.2)
    assert with_blind["total"] == pytest.approx(no_blind["total"] - 3.2)
    assert with_blind["total_before_penalty"] == pytest.approx(80.0)
    assert any("盲区惩罚" in note for note in with_blind["notes"])


def test_score_block_clamps_to_zero_floor() -> None:
    """低分 + 满额惩罚不得把总分压成负数。"""
    block = build_score_block(full_coverage(5.0), {"count": 9, "area_pct": 90.0})
    assert block["penalty"] == pytest.approx(-PENALTY_CAP)
    assert block["total"] == 0.0
    assert block["grade"] == "D"


def test_score_block_reports_grade_basis_and_bands() -> None:
    block = build_score_block(full_coverage(90.0), {})
    assert block["grade_basis"] == "normalized"
    assert block["grade_bands"] == {"A": 85.0, "B": 70.0, "C": 55.0, "D": 0.0}
    assert block["weight_sum"] == pytest.approx(0.95)


def test_literal_reading_switch_restores_prd_formula() -> None:
    """把开关关掉就回到 §4.5 的字面口径：满分 95，同分下等级不会更高。"""
    cfg = ScoreConfig(normalize_weights=False)
    block = build_score_block(full_coverage(100.0), {}, config=cfg)
    assert block["total"] == pytest.approx(95.0)
    assert block["grade"] == "A"
    assert block["grade_basis"] == "raw"


def test_unscored_dimension_does_not_change_total() -> None:
    """便民参考项无论多高分都不该动总分（PRD §4.3「不计分」）。"""
    base = full_coverage(70.0)
    boosted = full_coverage(70.0)
    boosted["groups"][-1]["score"] = 100.0
    assert build_score_block(base, {})["total"] == build_score_block(boosted, {})["total"]


# --------------------------------------------------------------------------------------
# build_summary (FR-304)
# --------------------------------------------------------------------------------------
def test_summary_is_data_driven_and_reproducible() -> None:
    coverage = full_coverage(72.0)
    coverage["groups"][0]["score"] = 30.0  # medical 最弱
    coverage["groups"][4]["score"] = 95.0  # transit 最强
    spots = {"count": 2, "area_pct": 6.4, "grid": {"radius_m": 1000}, "blocks": []}
    first = build_summary(coverage, spots, data_source="live", minutes=15)
    second = build_summary(coverage, spots, data_source="live", minutes=15)
    assert first == second
    assert 3 <= len(first) <= 5
    assert any("最弱项" in line and "基础医疗" in line for line in first)
    assert any("最强项" in line and "transit" in line for line in first)
    assert any("2 处" in line for line in first)


def test_summary_reports_blind_spot_absence_explicitly() -> None:
    lines = build_summary(full_coverage(80.0), {"count": 0, "area_pct": 0.0}, data_source="live")
    assert any("未识别到完全盲区" in line for line in lines)


def test_summary_mentions_missing_measurements() -> None:
    coverage = full_coverage(80.0)
    coverage["groups"][1]["nearest_minutes"] = None
    coverage["groups"][1]["unmeasured_count"] = 4
    lines = build_summary(coverage, {"count": 0, "area_pct": 0.0}, data_source="live")
    assert any("未实测" in line for line in lines)
    assert any("education" in line for line in lines)


def test_summary_flags_degraded_data_source() -> None:
    lines = build_summary(
        full_coverage(80.0),
        {"count": 0, "area_pct": 0.0},
        data_source="sample",
        degraded_reason="mock_mode",
    )
    assert any("程序合成的示例数据" in line and "mock_mode" in line for line in lines)


def test_summary_without_any_dimension_still_has_three_lines() -> None:
    lines = build_summary(None, {"count": 0, "area_pct": 0.0}, data_source="live")
    assert len(lines) >= 3
    assert any("未取得任何计分维度" in line for line in lines)


def test_summary_respects_max_items() -> None:
    lines = build_summary(
        full_coverage(80.0),
        {"count": 3, "area_pct": 12.0},
        data_source="sample",
        degraded_reason="quota",
        max_items=3,
    )
    assert len(lines) == 3


# --------------------------------------------------------------------------------------
# load_score_config
# --------------------------------------------------------------------------------------
def test_missing_weights_file_falls_back_with_a_problem_note(tmp_path) -> None:
    cfg, problems = load_score_config(tmp_path / "nope.yaml")
    assert cfg.normalize_weights is True
    assert cfg.grade_bands == DEFAULT_GRADE_BANDS
    assert problems and "未找到权重表" in problems[0]


def test_valid_weights_file_overrides_bands_and_penalty(tmp_path) -> None:
    path = tmp_path / "weights.yaml"
    path.write_text(
        "normalize_weights: false\n"
        "grades:\n  A: 90\n  B: 75\n  C: 60\n  D: 0\n"
        "blind_spot_penalty:\n  per_pct: 1.0\n  cap: 20\n"
        "t_ideal:\n  ratio: 0.4\n  minutes:\n    transit: 2.0\n"
        "summary_max: 4\n",
        encoding="utf-8",
    )
    cfg, problems = load_score_config(path)
    assert problems == []
    assert cfg.normalize_weights is False
    assert cfg.grade_bands == (("A", 90.0), ("B", 75.0), ("C", 60.0), ("D", 0.0))
    assert cfg.penalty_per_pct == 1.0 and cfg.penalty_cap == 20.0
    assert cfg.t_ideal_ratio == pytest.approx(0.4)
    assert cfg.t_ideal_minutes == {"transit": 2.0}
    assert cfg.summary_max == 4
    # 等级线换了，评级也要跟着换
    assert grade_for(88.0, cfg.grade_bands) == "B"


def test_broken_weights_file_falls_back_instead_of_raising(tmp_path) -> None:
    path = tmp_path / "weights.yaml"
    path.write_text("grades: [oops\n", encoding="utf-8")  # 故意的坏 YAML
    cfg, problems = load_score_config(path)
    assert problems, "坏文件必须留下问题记录"
    assert cfg.grade_bands == DEFAULT_GRADE_BANDS


def test_non_mapping_weights_file_falls_back(tmp_path) -> None:
    path = tmp_path / "weights.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    _, problems = load_score_config(path)
    assert problems and "不是映射" in problems[0]


def test_partial_band_override_drops_unparsable_entries(tmp_path) -> None:
    path = tmp_path / "weights.yaml"
    path.write_text("grades:\n  A: high\n  B: 70\n", encoding="utf-8")
    cfg, problems = load_score_config(path)
    assert ("B", 70.0) in cfg.grade_bands
    assert all(grade != "A" for grade, _ in cfg.grade_bands)
    assert any("不是数字" in problem for problem in problems)


# --------------------------------------------------------------------------------------
# 可复算（FR-301）
# --------------------------------------------------------------------------------------
def test_score_is_invariant_under_group_reordering() -> None:
    """FR-301 可复算：维度顺序变了，总分 / 等级 / 雷达不应跟着变。

    只比数值与字典（`radar` 是 dict，与顺序无关）；`dimensions` 是列表，按契约保留入参顺序，
    不参与这条断言 —— 拿整份 JSON 比对会把「顺序保留」误判成「分数不稳定」。
    """
    forward = full_coverage(77.0)
    reversed_coverage = make_coverage(list(reversed(forward["groups"])))
    left = build_score_block(forward, {})
    right = build_score_block(reversed_coverage, {})
    assert left["total"] == right["total"]
    assert left["total_raw"] == right["total_raw"]
    assert left["grade"] == right["grade"]
    assert left["radar"] == right["radar"]
    assert left["weight_sum"] == right["weight_sum"]


def test_score_block_is_deterministic_for_identical_input() -> None:
    coverage = full_coverage(63.5)
    spots = {"count": 1, "area_pct": 2.5}
    first = build_score_block(coverage, spots)
    second = build_score_block(coverage, spots)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == (
        json.dumps(second, sort_keys=True, ensure_ascii=False)
    )
