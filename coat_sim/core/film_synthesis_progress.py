"""Opt-in accepted-iterate progress diagnostics, never an optical scorer.

This project scheduling convention neither changes original goals nor certifies
stationarity. Non-overlapping windows count accepted steps, not line-search calls.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import sys
from typing import Any, Mapping


@dataclass(frozen=True)
class HybridProgressPolicy:
    accepted_window_steps: int
    relative_sse_progress_floor: float
    relative_worst_progress_floor: float
    stagnation_windows: int

    def __post_init__(self) -> None:
        for name in ("accepted_window_steps", "stagnation_windows"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"objective_progress_policy.{name} must be a positive integer")
        for name in ("relative_sse_progress_floor", "relative_worst_progress_floor"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"objective_progress_policy.{name} must be finite and nonnegative")


def progress_policy_from_payload(value: Any) -> HybridProgressPolicy | None:
    """Decode only this current explicit object or null; no default thresholds."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(
        HybridProgressPolicy.__dataclass_fields__
    ):
        raise ValueError(
            "objective_progress_policy requires exactly the four declared policy fields or null"
        )
    return HybridProgressPolicy(**dict(value))


def validate_progress_policy(value: Any) -> HybridProgressPolicy | None:
    if value is not None:
        if not isinstance(value, HybridProgressPolicy):
            raise ValueError("objective_progress_policy must be HybridProgressPolicy or None")
        value.__post_init__()
    return value


def relative_progress(previous: float, current: float) -> float:
    """Signed gain in [-1, 1], no epsilon; an all-zero window has zero gain."""
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
        for v in (previous, current)
    ):
        raise ValueError("progress observations must be finite nonnegative numbers")
    denominator = max(previous, current)
    return 0.0 if denominator == 0 else (previous - current) / denominator


@dataclass(frozen=True)
class ProgressSample:
    accepted_iteration: int
    weighted_sse: float
    max_error: float
    weighted_rms_error: float
    nominal_pass: bool
    physical_hash: str
    thicknesses_nm: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.accepted_iteration) is not int or self.accepted_iteration < 0:
            raise ValueError("accepted_iteration must be a nonnegative integer")
        for value in (self.weighted_sse, self.max_error, self.weighted_rms_error):
            relative_progress(value, value)
        if type(self.nominal_pass) is not bool:
            raise ValueError("progress requires an explicit original nominal_pass boolean")
        if not isinstance(self.physical_hash, str) or not self.physical_hash:
            raise ValueError("progress requires an exact physical identity")
        if (
            not isinstance(self.thicknesses_nm, tuple)
            or not self.thicknesses_nm
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or v < 0
                for v in self.thicknesses_nm
            )
        ):
            raise ValueError("progress requires finite nonnegative physical nm coordinates")


@dataclass(frozen=True)
class ProgressDecision:
    reason: str
    baseline: ProgressSample
    trigger: ProgressSample
    relative_sse_progress: float
    relative_worst_progress: float
    completed_windows: int
    consecutive_stagnation_windows: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AcceptedProgressMonitor:
    """Compact, opt-in state. Keeps no designs, responses or Jacobian owners."""

    def __init__(
        self,
        policy: HybridProgressPolicy,
        initial: ProgressSample,
        *,
        receiver_minimum_thickness_nm: float = sys.float_info.min,
    ):
        validate_progress_policy(policy)
        if initial.accepted_iteration != 0:
            raise ValueError("progress initial sample must precede accepted iterations")
        if not math.isfinite(receiver_minimum_thickness_nm) or receiver_minimum_thickness_nm <= 0:
            raise ValueError("minimax receiver lower bound must be finite and positive")
        self.receiver_minimum_thickness_nm = receiver_minimum_thickness_nm
        self.policy = policy
        self.baseline: ProgressSample | None = (
            initial
            if all(d >= self.receiver_minimum_thickness_nm for d in initial.thicknesses_nm)
            else None
        )
        self.latest = initial
        self.completed_windows = 0
        self.stagnation_count = 0
        self.decision: ProgressDecision | None = None
        self.last_window: ProgressDecision | None = None
        self.window_resets = 0
        self.last_reset: dict[str, Any] | None = None

    def reset_window(self, reason: str) -> None:
        """Break eligible continuity without inventing an accepted observation."""
        self.baseline = None
        self.stagnation_count = 0
        self.window_resets += 1
        self.last_reset = {"reason": reason, "sample": asdict(self.latest)}

    def observe(self, sample: ProgressSample) -> ProgressDecision | None:
        if self.decision is not None:
            raise ValueError("no accepted SSE samples are allowed after a handoff decision")
        if sample.accepted_iteration != self.latest.accepted_iteration + 1:
            raise ValueError("accepted progress observations must be consecutive")
        self.latest = sample
        if any(d < self.receiver_minimum_thickness_nm for d in sample.thicknesses_nm):
            # Zero is a legitimate SSE probe but outside the native minimax
            # receiver domain. Never clean/delete it after deciding to hand off.
            self.reset_window(
                "minimax_receiver_zero_thickness"
                if any(d == 0 for d in sample.thicknesses_nm)
                else "minimax_receiver_lower_bound"
            )
            return None
        if self.baseline is None:
            self.baseline = sample
            return None
        if (
            sample.accepted_iteration - self.baseline.accepted_iteration
            < self.policy.accepted_window_steps
        ):
            return None
        baseline = self.baseline
        self.completed_windows += 1
        sse_gain = relative_progress(baseline.weighted_sse, sample.weighted_sse)
        worst_gain = relative_progress(baseline.max_error, sample.max_error)
        sse_advances = sse_gain > self.policy.relative_sse_progress_floor
        worst_advances = worst_gain > self.policy.relative_worst_progress_floor
        self.stagnation_count = (
            self.stagnation_count + 1
            if not sample.nominal_pass and not sse_advances and not worst_advances
            else 0
        )
        reason = "continue"
        if not sample.nominal_pass:
            if sse_advances and not worst_advances:
                reason = "sse_worst_tradeoff"
            elif self.stagnation_count >= self.policy.stagnation_windows:
                reason = "declared_stagnation"
        window = ProgressDecision(
            reason,
            baseline,
            sample,
            sse_gain,
            worst_gain,
            self.completed_windows,
            self.stagnation_count,
        )
        self.last_window = window
        self.baseline = sample
        if reason != "continue":
            self.decision = window
        return self.decision

    def summary(self) -> dict[str, Any]:
        return {
            "policy": asdict(self.policy),
            "receiver_minimum_thickness_nm": self.receiver_minimum_thickness_nm,
            "scope": "joint_needle_sse_growth_only",
            "accepted_samples": self.latest.accepted_iteration,
            "completed_windows": self.completed_windows,
            "consecutive_stagnation_windows": self.stagnation_count,
            "window_resets": self.window_resets,
            "last_reset": self.last_reset,
            "last_window": None if self.last_window is None else self.last_window.to_dict(),
            "decision": None if self.decision is None else self.decision.to_dict(),
        }
