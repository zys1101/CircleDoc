"""services/samples.py —— 预置样例社区清单（FR-102 / FR-412）。

职责边界刻意切得很窄：**本模块只负责「读清单 + 校验」**，不合成任何数据、不出网。
`config/samples.yaml` 是唯一出处，`/api/samples` 与 `tools/gen_samples.py` 共用它，
避免「接口列了三个社区、生成器只认识两个」这种失同步。

三条来自 PRD 的硬约束：

1. 🔴 **坐标必须国内**（§10-1 规程①）：境外逆地理 / 境外地点检索 / 境外路线规划对个人档
   均为「-」（未开放），坐标写到境外会让 `live` 模式整条链直接不可用。越界项**在读入时
   就被剔除并留问题记录**，而不是等演示当天由百度报一个晦涩的错误码。
2. 🔴 **坐标不得凭记忆写死**（§10-1 规程②）：清单里每一项都带 `coord_verified`。
   `false` 表示「社区名与行政区属实，坐标待用 `/api/suggest` 或点图回填」——
   `/api/samples` 会把这个标记原样透出，`README` 与演示脚本据此说明。
   **本模块绝不把未核验的坐标伪装成已核验**，那是这个项目里最容易骗过自己的一处。
3. ⚠️ 三个社区必须落在**同一座城市**、且**三种建成形态各一**（成熟老城 / 新建大盘 /
   城中村或老旧小区）。形态用 `archetype` 指向 `services/poi_search.py:ARCHETYPES`
   的合成画像，三型的圈内计数与盲区面积必须有肉眼可辨的差异，否则样例失去展示价值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.config import Settings, get_settings
from infra.errors import CircleDocError

from services.poi_search import ARCHETYPES

__all__ = [
    "SampleCommunity",
    "SampleConfigError",
    "find_sample",
    "load_samples",
    "samples_as_dicts",
]


class SampleConfigError(RuntimeError):
    """清单本身不可用（文件缺失或一项都读不出来）。

    为什么不让它变成「空列表」：预置样例是 FR-102 的 **P0** 要求，静默返回空会让
    `/api/samples` 显示成功但没有样例，现场看起来像「功能没做」，而日志里什么都没有。
    这与词表自检（`WordlistError`）是同一种取舍：开发期缺陷不该被降级链吃成正常结果。
    """


@dataclass(frozen=True)
class SampleCommunity:
    """一个预置样例社区。`id` 是稳定标识，前端与生成器都按它对齐。"""

    id: str
    name: str
    city: str
    district: str
    lnglat: tuple[float, float]
    form: str
    archetype: str
    coord_verified: bool = False
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """`/api/samples` 的单项出参（PRD §4.7：`[{id, name, city, lnglat}]` + 追加说明字段）。"""
        return {
            "id": self.id,
            "name": self.name,
            "city": self.city,
            "district": self.district,
            "lnglat": [self.lnglat[0], self.lnglat[1]],
            "form": self.form,
            "archetype": self.archetype,
            "coord_verified": self.coord_verified,
            "coordinate_system": "bd09ll",
            "note": self.note,
        }


def _as_float(value: Any) -> float | None:
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


def _coerce_lnglat(raw: Any) -> tuple[float, float] | None:
    """接受 `[lng, lat]` / `{lng, lat}` / `"lng,lat"` 三种写法（与等时圈入参同一套宽容度）。"""
    if isinstance(raw, Mapping):
        lng = _as_float(raw.get("lng") if raw.get("lng") is not None else raw.get("longitude"))
        lat = _as_float(raw.get("lat") if raw.get("lat") is not None else raw.get("latitude"))
        return None if lng is None or lat is None else (lng, lat)
    if isinstance(raw, str):
        parts = [piece.strip() for piece in raw.split(",")]
        if len(parts) != 2:
            return None
        lng, lat = _as_float(parts[0]), _as_float(parts[1])
        return None if lng is None or lat is None else (lng, lat)
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        lng, lat = _as_float(raw[0]), _as_float(raw[1])
        return None if lng is None or lat is None else (lng, lat)
    return None


def _inside_china(lng: float, lat: float, cfg: Settings) -> bool:
    """境内判定用 `Settings` 的边界（`.env` 可覆盖），与 POI 检索层同一份口径。"""
    return (
        cfg.china_lng_min <= lng <= cfg.china_lng_max
        and cfg.china_lat_min <= lat <= cfg.china_lat_max
    )


def load_samples(
    settings: Settings | None = None,
    *,
    path: str | Path | None = None,
) -> tuple[list[SampleCommunity], list[str]]:
    """读 `config/samples.yaml`，返回 `(样例列表, 问题清单)`。

    单条记录有问题 → 跳过该条并记一条问题（其余照常可用）；**整份不可用** → 抛
    `SampleConfigError`（见类注释的取舍说明）。
    """
    cfg = settings or get_settings()
    target = Path(path) if path is not None else Path(cfg.samples_config_path)
    if not target.is_file():
        raise SampleConfigError(
            f"预置样例清单不存在：{target}。FR-102 要求至少 3 个样例社区，"
            "请从版本库取回 `config/samples.yaml`（可用 `tools/gen_samples.py` 校验）。"
        )
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 只在缺 pyyaml 的环境触发
        raise SampleConfigError("环境缺少 pyyaml，无法读取 config/samples.yaml。") from exc
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SampleConfigError(f"预置样例清单 {target} 解析失败：{type(exc).__name__}") from exc
    if not isinstance(raw, Mapping):
        raise SampleConfigError(f"预置样例清单 {target} 顶层不是映射。")

    entries = raw.get("communities")
    if not isinstance(entries, list) or not entries:
        raise SampleConfigError(f"预置样例清单 {target} 里 `communities` 不是非空列表。")

    out: list[SampleCommunity] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            problems.append(f"第 {index + 1} 项不是映射，已跳过。")
            continue
        ident = str(item.get("id") or "").strip()
        if not ident:
            problems.append(f"第 {index + 1} 项缺少 `id`，已跳过。")
            continue
        if ident in seen:
            problems.append(f"`id` 重复：{ident}，已跳过后出现的那条。")
            continue
        lnglat = _coerce_lnglat(item.get("lnglat"))
        if lnglat is None:
            problems.append(f"{ident}：`lnglat` 缺失或格式不对，已跳过。")
            continue
        lng, lat = lnglat
        if not _inside_china(lng, lat, cfg):
            problems.append(
                f"{ident}：坐标 ({lng}, {lat}) 落在境外，已剔除"
                "（境外逆地理 / 地点检索 / 路线规划对个人档均为「-」，笔记 §3.1）。"
            )
            continue
        archetype = str(item.get("archetype") or "").strip()
        if archetype not in ARCHETYPES:
            problems.append(
                f"{ident}：`archetype`={archetype!r} 不在 {list(ARCHETYPES)} 内，已跳过。"
            )
            continue
        verified = item.get("coord_verified")
        seen.add(ident)
        out.append(
            SampleCommunity(
                id=ident,
                name=str(item.get("name") or ident),
                city=str(item.get("city") or ""),
                district=str(item.get("district") or ""),
                lnglat=(lng, lat),
                form=str(item.get("form") or ""),
                archetype=archetype,
                coord_verified=bool(verified) if verified is not None else False,
                note=(str(item["note"]) if item.get("note") else None),
            )
        )

    if not out:
        raise SampleConfigError(
            "预置样例清单里没有一条可用记录：\n  - " + "\n  - ".join(problems or ["（无详情）"])
        )
    unverified = [item.id for item in out if not item.coord_verified]
    if unverified:
        problems.append(
            "以下样例的中心点坐标尚未实调核验（`coord_verified: false`）："
            f"{', '.join(unverified)}。请用 `/api/suggest` 或前端点图取到 BD09LL 后回填，"
            "再人工核对后提交（PRD §10-1 规程②）。"
        )
    return out, problems


def samples_as_dicts(
    settings: Settings | None = None,
    *,
    path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """`/api/samples` 直接用这个：清单 + 问题（问题以 `notes` 下发，不隐藏）。"""
    communities, problems = load_samples(settings, path=path)
    return [item.as_dict() for item in communities], problems


class UnknownSample(CircleDocError):
    """`sample_id` 不在清单里（前端下拉与清单不同步、或手改了 URL）。"""

    error_id = "unknown_sample"
    category = "client"
    http_status = 400
    user_message = "指定的预置样例社区不存在，请从样例下拉里重新选择。"
    retryable = False
    degrade_to_sample = False


def find_sample(
    sample_id: str,
    settings: Settings | None = None,
    *,
    path: str | Path | None = None,
) -> SampleCommunity:
    """按 `id` 取一个样例社区；找不到抛 `UnknownSample`（400 + 可读文案）。

    为什么抛错而不是「回第一个样例」：静默替换会让 `/api/analysis` 用**另一个社区**的
    中心点出一份报告，而调用方完全不知道 —— 演示现场表现为「点了 A 出来 B 的结论」，
    这是最难排查的一类错。
    """
    communities, _ = load_samples(settings, path=path)
    wanted = str(sample_id or "").strip()
    for item in communities:
        if item.id == wanted:
            return item
    raise UnknownSample(
        detail={"sample_id": wanted, "available": [item.id for item in communities]}
    )
