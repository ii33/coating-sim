"""Plan one atomic edit of existing physical-nm thickness bounds."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ThicknessBoundsUpdate:
    side: str
    layer_id: str
    lower_nm: float
    upper_nm: float | None


def _nm(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是非负有限数值")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}必须是非负有限数值")
    return float(value)


def plan_thickness_bounds(
    rows: Sequence[Mapping[str, object]], scope: str,
    lower_nm: float | None, upper_nm: float | None,
) -> tuple[ThicknessBoundsUpdate, ...]:
    """None means keep, including an existing unbounded upper; never select rows."""
    if scope not in {"all", "selected"}:
        raise ValueError("批量厚度范围必须是全部层或已选择的厚度变量")
    if lower_nm is None and upper_nm is None:
        return ()
    if lower_nm is not None:
        lower_nm = _nm(lower_nm, "厚度下限（nm）")
    if upper_nm is not None:
        upper_nm = _nm(upper_nm, "厚度上限（nm）")
    targets = [row for row in rows if scope == "all" or row.get("enabled") is True]
    if not targets:
        raise ValueError("当前范围没有膜层；请先选择厚度变量或加载膜系")
    updates: list[ThicknessBoundsUpdate] = []
    for row in targets:
        side, layer_id = row["side"], row["layer_id"]
        if not isinstance(side, str) or not isinstance(layer_id, str):
            raise ValueError("膜层身份无效，厚度边界未修改")
        old_lower = _nm(row["lower_nm"], "原厚度下限（nm）")
        old_upper = None if row["upper_nm"] is None else _nm(row["upper_nm"], "原厚度上限（nm）")
        lower = old_lower if lower_nm is None else lower_nm
        upper = old_upper if upper_nm is None else upper_nm
        if upper is not None and upper <= lower:
            raise ValueError(f"{row.get('side_label', side)} · {row.get('label', layer_id)}：上限必须大于下限；未修改任何层")
        if (lower, upper) != (old_lower, old_upper):
            updates.append(ThicknessBoundsUpdate(side, layer_id, lower, upper))
    return tuple(updates)
