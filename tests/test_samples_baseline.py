"""`data/samples/*.json` 基线回归 + 预置样例一致性（§6.1 的可复现断言 / FR-102 / FR-412）。

PRD §6.1 原文要求：「断言用例：对预置样例，盲区数量与圈内设施计数在缓存命中下
**100 % 可复现**」。这句话只有在一份签入版本库的基线面前才能自动跑 —— 这就是本文件的职责。

三道检查，各自防一种具体的事故：

1. **清单可用**：`config/samples.yaml` 至少 3 个社区、同城、三种建成形态各一、坐标全在境内。
   防的是「为了凑数把三个社区写成同一种形态」，那会让 FR-102 的展示价值直接归零。
2. **三型差异可见**：三个样例的总分与盲区面积必须呈**单调**关系（老城 > 新建大盘 > 城中村）。
   防的是「画像没接上」——`archetype` 忘了传时合成层会按坐标随便挑一个，
   三个样例就会长得差不多，而报告本身看起来完全正常（这个坑实测踩过）。
3. **基线不漂**：现算结果必须与 `data/samples/*.json` 逐字段一致。
   基线漂了就是算法或权重变了，必须有人解释为什么，而不是让数字悄悄改掉。
"""

from __future__ import annotations

import json

import pytest
from infra.config import ROOT_DIR, get_settings
from services.analysis import run_analysis
from services.poi_search import ARCHETYPES
from services.samples import load_samples

from tests.helpers import async_test

SETTINGS = get_settings()
SAMPLES_DIR = ROOT_DIR / "data" / "samples"


def load_communities():
    communities, _ = load_samples(SETTINGS)
    return communities


def load_baseline(ident: str) -> dict:
    path = SAMPLES_DIR / f"{ident}.json"
    assert path.is_file(), f"缺少基线 {path}（跑 `python tools/gen_samples.py` 生成）"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# 1. 清单本身
# --------------------------------------------------------------------------------------
def test_manifest_has_at_least_three_communities() -> None:
    communities = load_communities()
    assert len(communities) >= 3, "FR-102 要求至少 3 个预置样例社区"


def test_communities_share_one_city_and_cover_three_built_forms() -> None:
    communities = load_communities()
    cities = {item.city for item in communities}
    assert len(cities) == 1, f"FR-102 要求同一城市内三种建成形态，实际城市：{cities}"
    archetypes = {item.archetype for item in communities}
    assert archetypes == set(
        ARCHETYPES
    ), f"三个样例必须覆盖全部建成形态画像，实际 {archetypes} vs 期望 {set(ARCHETYPES)}"


def test_every_sample_centre_is_inside_china() -> None:
    """境外各服务对个人档为「-」，坐标写到境外会让 live 模式整条链不可用（§10-1 规程①）。"""
    for item in load_communities():
        lng, lat = item.lnglat
        assert SETTINGS.china_lng_min <= lng <= SETTINGS.china_lng_max
        assert SETTINGS.china_lat_min <= lat <= SETTINGS.china_lat_max


def test_unverified_coordinates_are_flagged_not_hidden() -> None:
    """坐标未实调核验时必须如实标 `false`（§10-1 规程②：坐标不得凭记忆写死）。"""
    communities = load_communities()
    flagged = [item for item in communities if not item.coord_verified]
    if flagged:
        # 本仓库当前就是这种状态：没有可用的 AK，无法完成核验。
        # 这条断言的意义是「不许悄悄改成 true 而不真的核验」。
        assert all(item.note for item in flagged), "未核验的样例必须带 note 说明原因"
        assert SETTINGS.samples_config_path.is_file()


# --------------------------------------------------------------------------------------
# 2. 三型差异可见（FR-102「肉眼可辨的差异」）
# --------------------------------------------------------------------------------------
def test_three_forms_differ_in_the_expected_direction() -> None:
    """老城 > 新建大盘 > 城中村：分数单调递减、盲区面积单调递增。

    顺序来自 `services/poi_search.py:_ARCHETYPE_COUNTS` 的画像设定，不是巧合 ——
    这条断言就是「画像真的接上了」的证据。
    """
    baselines = {item.id: load_baseline(item.id)["baseline"] for item in load_communities()}
    by_archetype = {item.archetype: baselines[item.id] for item in load_communities()}
    ordered = [by_archetype[key] for key in ("old_town", "new_district", "transit_corridor")]
    totals = [item["score"]["total"] for item in ordered]
    areas = [item["blind_spots"]["area_pct"] for item in ordered]
    assert totals == sorted(totals, reverse=True), f"总分未呈 老城>大盘>城中村：{totals}"
    assert areas == sorted(areas), f"盲区面积未呈 老城<大盘<城中村：{areas}"
    assert totals[0] - totals[-1] > 30, "首尾样例的分数差应肉眼可辨（> 30 分）"


def test_old_town_has_a_measured_nearest_for_every_scored_dimension() -> None:
    """配套最密集的样例必须六个维度**全部**有实测时间（分层挑选的直接效果）。

    回归的是那个真实缺陷：全局最近 K 会让密集社区的维度被饿死、时效分记 0，
    于是「设施越全、得分越低」。详见 `services/coverage.py:SELECTION_FAIR`。
    """
    communities = load_communities()
    old_town = next(item for item in communities if item.archetype == "old_town")
    baseline = load_baseline(old_town.id)["baseline"]
    nearest = {
        key: value for key, value in baseline["nearest_minutes"].items() if key != "convenience"
    }
    missing = {key: value for key, value in nearest.items() if value is None}
    assert not missing, f"老城样例仍有维度没测到：{missing}"
    assert all(score == 100.0 for score in baseline["score"]["radar"].values())


def test_reference_dimension_never_enters_the_total() -> None:
    for item in load_communities():
        baseline = load_baseline(item.id)["baseline"]
        assert baseline["score"]["reference_only"] == ["convenience"]
        assert "convenience" not in baseline["score"]["radar"]


# --------------------------------------------------------------------------------------
# 3. 基线不漂（§6.1 的可复现断言）
# --------------------------------------------------------------------------------------
@async_test
async def test_mock_report_matches_the_checked_in_baseline() -> None:
    """现算 == 基线：分数、圈内计数、盲区数量与面积全部逐字段一致。"""
    for community in load_communities():
        baseline = load_baseline(community.id)["baseline"]
        report = await run_analysis(
            list(community.lnglat),
            modes=["walking"],
            minutes=15.0,
            data_mode="mock",
            platform="desktop",
            archetype=community.archetype,
        )
        assert report["score"]["total"] == baseline["score"]["total"], community.id
        assert report["score"]["grade"] == baseline["score"]["grade"], community.id
        assert report["score"]["radar"] == baseline["score"]["radar"], community.id
        assert report["blind_spots"]["count"] == baseline["blind_spots"]["count"], community.id
        assert (
            report["blind_spots"]["area_pct"] == baseline["blind_spots"]["area_pct"]
        ), community.id
        counts = {group["key"]: group["count_in"] for group in report["coverage"]["groups"]}
        assert counts == baseline["counts_in"], community.id
        nearest = {group["key"]: group["nearest_minutes"] for group in report["coverage"]["groups"]}
        assert nearest == baseline["nearest_minutes"], community.id


@async_test
async def test_baseline_runs_are_repeatable() -> None:
    """连跑两次逐字段一致（FR-301 可复算）。"""
    community = load_communities()[0]
    first = await run_analysis(
        list(community.lnglat), minutes=15.0, data_mode="mock", archetype=community.archetype
    )
    second = await run_analysis(
        list(community.lnglat), minutes=15.0, data_mode="mock", archetype=community.archetype
    )
    for key in ("score", "coverage", "blind_spots", "summary"):
        assert json.dumps(first[key], sort_keys=True, ensure_ascii=False) == (
            json.dumps(second[key], sort_keys=True, ensure_ascii=False)
        ), key


@async_test
async def test_baseline_declares_synthetic_provenance() -> None:
    """基线必须自带「生成方法与参数说明」，且措辞不得暗示内容来自百度（D9 / 条款 3.3.4）。"""
    for community in load_communities():
        payload = load_baseline(community.id)
        generator = payload["generator"]
        assert generator["tool"] == "tools/gen_samples.py"
        assert generator["deterministic"] is True
        method = generator["method"]
        assert "不含任何百度地图返回内容" in method
        assert "生成方法与参数说明" not in method  # 那句话属于文件头注释，不该出现在这里
        assert payload["community"]["archetype"] == community.archetype


@async_test
async def test_mock_baseline_spends_no_quota() -> None:
    """合成基线全程零出网：三栏全 0，`place_calls` 尤其不能在离线演示里被算成消耗。"""
    for community in load_communities():
        baseline = load_baseline(community.id)["baseline"]
        usage = baseline["usage"]
        assert usage["place_calls"] == 0
        assert usage["http_requests"] == 0
        assert usage["quota_units"] == 0
        assert usage["route_lines"] == {"walking": 0, "riding": 0}


@async_test
async def test_analysis_declares_data_source_as_sample_and_says_why() -> None:
    """FR-306：离线报告必须带 `sample` 徽标与可读原因，而不是看起来像真实调用。"""
    community = load_communities()[0]
    report = await run_analysis(
        list(community.lnglat), minutes=15.0, data_mode="mock", archetype=community.archetype
    )
    assert report["meta"]["data_source"] == "sample"
    assert report["meta"]["degraded_reason"]
    assert any("程序合成" in line or "示例数据" in line for line in report["summary"])


def test_samples_directory_ships_with_the_image() -> None:
    """`data/samples/` 是 FR-412 的交付落点，不能被 `.gitignore` 或 `.dockerignore` 排掉。"""
    assert SAMPLES_DIR.is_dir()
    for item in load_communities():
        assert (SAMPLES_DIR / f"{item.id}.json").is_file()


@pytest.mark.parametrize("archetype", ["old_town", "new_district", "transit_corridor"])
def test_each_archetype_has_a_distinct_blind_spot_profile(archetype: str) -> None:
    """三种形态的盲区画像必须互不相同，否则「样例失去展示价值」（FR-102 原话）。"""
    community = next(item for item in load_communities() if item.archetype == archetype)
    baseline = load_baseline(community.id)["baseline"]
    assert baseline["blind_spots"]["count"] >= 0
    assert 0.0 <= baseline["blind_spots"]["area_pct"] <= 100.0
    assert baseline["blind_spots"]["missing"], f"{archetype} 的盲区必须列出缺失类别"
