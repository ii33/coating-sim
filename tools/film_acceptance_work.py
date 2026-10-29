"""Shared research work accounting; no optimization policy or dispatch."""
from copy import deepcopy
from dataclasses import dataclass
import math

from tools.run_film_synthesis_acceptance import jsonable


@dataclass(frozen=True)
class Work:
    kernel_calls: int = 0
    wavelength_points: int = 0
    layer_points: int = 0

    def __add__(self, other):
        return Work(*(getattr(self, key) + getattr(other, key) for key in self.__dataclass_fields__))

    def fits(self, cap):
        return all(getattr(self, key) <= getattr(cap, key) for key in self.__dataclass_fields__)

    def scale(self, factor):
        return Work(*(getattr(self, key) * factor for key in self.__dataclass_fields__))


def grid_work(objectives, layers):
    """Match the current evaluator's complete objective/angle/chunk schedule."""
    limit = min(10001, 1_000_000 // (layers + 3))
    if limit < 1:
        raise ValueError("Unsupported physical layer count")
    calls = points = 0
    for objective in objectives:
        samples = objective["wavelength_samples"]
        angles = len(objective["angles_deg"])
        calls += math.ceil(samples / limit) * angles
        points += samples * angles
    return Work(calls, points, points * max(1, layers))


def audit_upper_work(objectives, multiplier, layers):
    """Conservative complete-audit reservation, using existing extrema maxiter.

    Reserve dense + offset + every possible bracket, on both bounded sides.
    This changes no audit work or convergence setting. Unused reservation is
    released after the complete audit; actual kernel work is recorded separately.
    """
    from coat_sim.core.film_synthesis_audit import EXTREMUM_MAXITER
    total = Work()
    for objective in objectives:
        dense = deepcopy(objective)
        n = objective["wavelength_samples"]
        dense["wavelength_samples"] = 1 if n == 1 else (n - 1) * multiplier + 1
        d = dense["wavelength_samples"]
        total += grid_work((dense,), layers)
        if d > 1:
            offset = {**dense, "wavelength_samples": d - 1}
            total += grid_work((offset,), layers)
        bound = objective["pointwise"]
        sides = (2 if bound["mode"] == "value" else
                 int(bound.get("lower_bound") is not None) + int(bound.get("upper_bound") is not None))
        if bound["enabled"] and d > 1:
            scalar_points = max(0, 2 * d - 3) * sides * EXTREMUM_MAXITER * len(objective["angles_deg"])
            total += Work(scalar_points, scalar_points, scalar_points * max(1, layers))
    return total


def score_metrics(score):
    """Saved raw evidence only. Means for disabled modes remain diagnostic."""
    if score is None:
        return None
    score = jsonable(score)
    per_band = []
    # Audit mean remains fixed dense-grid; extrema/offset are never integration weights.
    for row in score.get("coverage", []):
        waves, values = row["wavelength_nm"], row["actual_values"]
        mean = (values[0] if len(waves) == 1 else
                sum((b-a)*(x+y)/2 for a,b,x,y in zip(waves[:-1],waves[1:],values[:-1],values[1:], strict=True)) / (waves[-1]-waves[0]))
        audited = next((a for a in score.get("audit_coverage", [])
            if a["objective_index"] == row["objective_index"] and a["angle_deg"] == row["angle_deg"]), row)
        per_band.append(dict(objective_index=row["objective_index"], angle_deg=row["angle_deg"],
            sampled_min=min(audited["actual_values"]), sampled_max=max(audited["actual_values"]),
            fixed_grid_mean=mean,
            mean_measure=score.get("audit", {}).get("mean_measure", "original_training_grid")))
    rms = score.get("weighted_rms_error")
    return dict(max_error=score.get("max_error"), weighted_rms_error=rms,
        weighted_sse=(None if rms is None else rms*rms),
        weighted_sse_scope="Square of canonical active-constraint normalized weighted RMS; no sparse feedback guide contribution",
        mean_constraints=[e for e in score.get("errors", []) if e.get("reduction") == "mean"],
        pointwise_max_error=max((e["normalized_violation"] for e in score.get("errors", []) if e.get("reduction", "pointwise") == "pointwise"), default=None),
        per_band=per_band, nominal_pass=score.get("nominal_pass"), solver_acceptable=score.get("solver_acceptable"))
