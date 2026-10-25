"""JSON-only editor drafts for the opt-in accepted-objective progress policy.

The two floors are dimensionless relative ratios, not optical percent values.
No scheduling threshold is supplied by this editor on the user's behalf.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping

from coat_sim.core.film_synthesis_progress import progress_policy_from_payload


PROGRESS_FIELD_LABELS = {
    "accepted_window_steps": "每窗口已接受步数",
    "relative_sse_progress_floor": "SSE 相对进展下限（比例）",
    "relative_worst_progress_floor": "最坏违规相对进展下限（比例）",
    "stagnation_windows": "连续停滞窗口数",
}


@dataclass
class ObjectiveProgressDraft:
    enabled: bool = False
    texts: dict[str, str] = field(default_factory=lambda: dict.fromkeys(PROGRESS_FIELD_LABELS, ""))

    def policy_payload(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        values: dict[str, Any] = {}
        for name, label in PROGRESS_FIELD_LABELS.items():
            literal = self.texts[name].strip()
            if name in {"accepted_window_steps", "stagnation_windows"}:
                digits = literal.lstrip("0") or "0"
                if (not literal.isascii() or not literal.isdigit() or len(digits) > 16
                        or not 1 <= int(digits) <= (1 << 53) - 1):
                    raise ValueError(f"{label}须填写可精确表示的正十进制整数")
                values[name] = int(digits)
            else:
                try:
                    value = float(literal)
                except (ValueError, OverflowError) as exc:
                    raise ValueError(f"{label}须填写有限非负比例；0.001 表示 0.1%") from exc
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{label}须填写有限非负比例；0.001 表示 0.1%")
                values[name] = value
        progress_policy_from_payload(values)
        return values

    def to_configuration(self) -> dict[str, Any]:
        self.policy_payload()
        return {"enabled": self.enabled, "fields": dict(self.texts)}

    @classmethod
    def from_configuration(cls, value: Any, frozen_policy: Any) -> ObjectiveProgressDraft:
        policy = progress_policy_from_payload(frozen_policy)
        if value is None:
            return cls(policy is not None, ({name: str(getattr(policy, name))
                for name in PROGRESS_FIELD_LABELS} if policy is not None
                else dict.fromkeys(PROGRESS_FIELD_LABELS, "")))
        if (not isinstance(value, Mapping) or set(value) != {"enabled", "fields"}
                or type(value["enabled"]) is not bool or not isinstance(value["fields"], Mapping)
                or set(value["fields"]) != set(PROGRESS_FIELD_LABELS)
                or any(not isinstance(text, str) for text in value["fields"].values())):
            raise ValueError("目标进展编辑配置须含明确开关和四个原文输入")
        draft = cls(value["enabled"], dict(value["fields"]))
        if draft.policy_payload() != frozen_policy:
            raise ValueError("目标进展开关/输入与冻结策略不一致")
        return draft
