"""Automatic manufacturing repair with paired, pointwise optical evidence.

The shadow response .5 + (candidate-reference)/2 lets the existing full-grid
minimax and independent extremum auditor operate on curve differences. Shadow
responses are never presented as physical R/T or original-goal scores.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
import math
from time import monotonic
from typing import Any, Callable, Mapping
from numbers import Real

import numpy as np

from .film_auto_synthesis import FilmSynthesisRequest, _proxy
from .film_design import MAX_LAYERS, validate_design
from .film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from .film_optimization import FilmOptimizationRequest, FilmThicknessVariable, FilmOptimizationObjective
from .film_optimization_scoring import reduction_score
from .film_optimization_structure import _StructureCancelled, evaluate_frozen_structure_candidate
from .film_reduction import FilmReductionRequest, _proposals
from .film_synthesis_archive import physical_design_hash
from .film_synthesis_audit import audit_frozen_synthesis_candidate
from .film_synthesis_batch_needle import cleanup_batch_needle_design
from .film_synthesis_manufacturing import (
    _owned_bytes, manufacturing_refinement_working_set_bytes, manufacturing_snapshot,
)
from .film_synthesis_refinement import (
    SynthesisResourceGuard,
    SynthesisStageBudget,
    refine_synthesis_full_minimax,
)

Value = dict[str, Any]
POLICY = dict(version=1, proposals=32, local_iterations=80, physical_calls=2048, seconds=60.0)
AUDIT_FEEDBACK_LIMIT = 1
AUDIT_FEEDBACK_POINTS = 8


def candidate_accepted(snapshot: Mapping[str, Any] | None, original_targets_present: bool) -> bool:
    """Only a complete jointly audited candidate is an applicable repair."""
    return bool(
        type(original_targets_present) is bool
        and isinstance(snapshot, Mapping)
        and snapshot.get("thickness_pass") is True
        and isinstance(snapshot.get("optical_guard"), Mapping)
        and snapshot["optical_guard"].get("passed") is True
        and (not original_targets_present or all(
            isinstance(snapshot.get(name), Mapping)
            and snapshot[name].get("nominal_pass") is True
            for name in ("training_score", "audit_score")
        ))
    )


def _difference_summary(evidence: Mapping[str, Any], design: Mapping[str, Any]) -> Value:
    worst = None
    for row in evidence["records"]:
        for wave, actual, reference in zip(row["wavelength_nm"], row["candidate_values"],
                                            row["reference_values"], strict=True):
            change = abs(actual - reference)
            if worst is None or change > worst["absolute_change"]:
                worst = dict(objective_index=row["objective_index"], metric=row["metric"],
                    angle_deg=row["angle_deg"], polarization=row["polarization"],
                    wavelength_nm=wave, candidate_value=actual, reference_value=reference,
                    absolute_change=change)
    return dict(candidate_physical_hash=physical_design_hash(design),
        maximum_absolute_change=evidence["maximum_absolute_change"], worst_point=worst,
        passed=evidence["passed"])


def _interior_margin_objective(objective: FilmOptimizationObjective) -> FilmOptimizationObjective:
    """Guide-only cushion; exact/value targets and physical endpoints stay exact."""
    def tightened(criterion):
        if not criterion.enabled or criterion.mode != "range":
            return criterion
        lower, upper = criterion.lower_bound, criterion.upper_bound
        width = math.inf if lower is None or upper is None else upper - lower
        if width <= 0:
            return criterion

        def cushion(bound):
            if bound is None or not 0.0 < bound < 1.0:
                return 0.0
            return min(1e-5, min(bound, 1.0 - bound) * 1e-3, width * 0.01)

        return replace(criterion, lower_bound=None if lower is None else lower + cushion(lower),
                       upper_bound=None if upper is None else upper - cushion(upper))

    return replace(objective, pointwise=tightened(objective.pointwise), mean=tightened(objective.mean))


def _audit_feedback(request, guard, original_audit, *, original_targets_present):
    """At most eight independently found violations enter one corrective solve."""
    from .film_nominal_acceptance import nominal_condition_pass

    proposals = []
    for row in guard["records"]:
        for wave, actual, before in zip(row["wavelength_nm"], row["candidate_values"],
                                        row["reference_values"], strict=True):
            if not paired_interval_pass([actual], [before], guard["tolerance_fraction"]):
                proposals.append((abs(actual - before) - guard["tolerance_fraction"],
                    "difference", row["objective_index"], row["angle_deg"], wave))
    if original_targets_present:
        for error in original_audit["score"]["errors"]:
            objective = request.objectives[error.objective_index]
            criterion = getattr(objective, error.reduction)
            if criterion.enabled and not nominal_condition_pass(criterion, error.actual):
                proposals.append((error.normalized_violation, "original", error.objective_index,
                                  error.angle_deg, None if error.reduction == "mean" else error.wavelength_nm))
    specifications = []
    seen = set()
    for _, kind, index, angle, wave in sorted(proposals, key=lambda item: item[0], reverse=True):
        key = kind, index, angle, wave
        if key in seen:
            continue
        seen.add(key)
        original = request.objectives[index]
        if wave is None:
            objective = replace(original, angles_deg=(angle,), wavelength_samples=(
                1 if original.wavelength_samples == 1 else
                (original.wavelength_samples - 1) * request.validation_samples_multiplier + 1))
        else:
            objective = replace(original, angles_deg=(angle,), wavelength_min_nm=wave,
                wavelength_max_nm=wave, wavelength_samples=1,
                mean=replace(original.mean, enabled=False))
        specifications.append((kind, index, objective))
        if len(specifications) >= AUDIT_FEEDBACK_POINTS:
            break
    return specifications


class RepairLimit(Exception):
    pass


def effective_policy(
    request: FilmSynthesisRequest, policy: Mapping[str, Any] | None = None
) -> Value:
    controls = dict(POLICY if policy is None else policy)
    if set(controls) != set(POLICY):
        raise ValueError("automatic repair policy fields differ")
    for name in ("version", "proposals", "local_iterations", "physical_calls"):
        if type(controls[name]) is not int or controls[name] < 1:
            raise ValueError("automatic repair work controls must be positive integers")
    if (
        isinstance(controls["seconds"], bool)
        or not isinstance(controls["seconds"], Real)
        or not math.isfinite(controls["seconds"])
        or controls["seconds"] <= 0
    ):
        raise ValueError("automatic repair duration must be positive finite")
    if request.max_evaluations is not None:
        controls["physical_calls"] = min(controls["physical_calls"], request.max_evaluations)
    if request.max_seconds is not None:
        controls["seconds"] = min(controls["seconds"], request.max_seconds)
    return controls


def paired_interval_pass(actual, reference, tolerance: float) -> bool:
    """Closed physical intervals; zero tolerance has no implicit epsilon floor.

    Forming 0.51-0.50 first would overstate an exact one-percentage-point
    boundary in binary arithmetic. Compare against the declared endpoints once.
    """
    a, b = np.asarray(actual, float), np.asarray(reference, float)
    return bool(
        a.shape == b.shape
        and np.all(np.isfinite(a))
        and np.all(np.isfinite(b))
        and np.all(a >= b - tolerance)
        and np.all(a <= b + tolerance)
    )


def covers_grid(waves, expected) -> bool:
    """Recognize equivalent floating grids without relaxing optical values."""
    waves, expected = np.asarray(waves, float), np.asarray(expected, float)
    if waves.ndim != 1 or not len(waves) or not np.all(np.isfinite(waves)):
        return False
    positions = np.searchsorted(waves, expected)
    left = np.maximum(positions - 1, 0)
    right = np.minimum(positions, len(waves) - 1)
    distance = np.minimum(np.abs(waves[left] - expected), np.abs(waves[right] - expected))
    return bool(np.all(distance <= 8 * np.spacing(np.abs(expected))))


def curve_audit_metadata(value: Mapping[str, Any]) -> Value:
    audit = value["score"]["audit"]
    return dict(
        kind="paired_curve_difference",
        **{
            name: audit[name]
            for name in (
                "physical_evaluations",
                "wavelength_points",
                "dense_points",
                "offset_points",
                "unique_condition_points",
                "bracket_count",
                "completed_extrema",
                "continuous_certificate",
            )
        },
    )


def lower_limit(target_nm: float, thickness_tolerance_nm: float) -> float:
    for value in (target_nm, thickness_tolerance_nm):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("repair thicknesses must be finite numbers in nm")
    if target_nm <= 0 or thickness_tolerance_nm < 0:
        raise ValueError("target must be positive and thickness tolerance nonnegative")
    return max(0.0, target_nm - thickness_tolerance_nm)


def deficient_ids(design: Mapping[str, Any], floor: float) -> list[str]:
    return [r["id"] for r in design["front_layers"] if r["thickness"]["value"] < floor]


def legal_thicknesses(
    request: FilmSynthesisRequest, design: Mapping[str, Any], floor: float
) -> bool:
    rows = design["front_layers"]
    lower = max(floor, request.minimum_nonzero_thickness_nm or 0.0)
    return bool(
        len(rows) <= min(MAX_LAYERS, request.max_layers or MAX_LAYERS)
        and all(
            row["thickness"]["unit"] == "nm"
            and row["thickness"]["value"] >= lower
            and (
                request.max_layer_thickness_nm is None
                or row["thickness"]["value"] <= request.max_layer_thickness_nm
            )
            for row in rows
        )
        and (
            request.max_total_thickness_nm is None
            or math.fsum(row["thickness"]["value"] for row in rows)
            <= request.max_total_thickness_nm
        )
    )


def structural_candidates(
    request: FilmSynthesisRequest, source: Mapping[str, Any], target: float, floor: float
):
    """Reuse deletion/merge proposals; also explore legal positive insertions."""
    assert request.frozen_material_snapshot is not None
    bad = set(deficient_ids(source, floor))
    raised = deepcopy(dict(source))
    if raised.get("expression", {}).get("text"):
        raised["expression"]["in_sync"] = False
    for row in raised["front_layers"]:
        if row["id"] in bad:
            row["thickness"] = dict(value=target, unit="nm")
    minimum = deepcopy(raised)
    for row in minimum["front_layers"]:
        if row["id"] in bad:
            row["thickness"] = dict(value=floor, unit="nm")
    yield "raise_to_lower_limit", minimum
    yield "raise_thin_layers", raised
    removed = deepcopy(dict(source))
    if removed.get("expression", {}).get("text"):
        removed["expression"]["in_sync"] = False
    removed["front_layers"] = [r for r in removed["front_layers"] if r["id"] not in bad]
    yield (
        "delete_thin_layers_merge",
        cleanup_batch_needle_design(removed, request.frozen_material_snapshot).design,
    )
    variables = tuple(
        FilmThicknessVariable("front", r["id"], 0.0, request.max_layer_thickness_nm)
        for r in raised["front_layers"]
    )
    reduction = FilmReductionRequest(
        FilmOptimizationRequest(
            raised,
            request.design_revision,
            variables,
            request.objectives,
            frozen_material_snapshot=request.frozen_material_snapshot,
        ),
        mode="layer_count",
    )
    for candidate in _proposals(reduction, raised, 0.1):
        yield (
            "delete_or_merge",
            cleanup_batch_needle_design(candidate, request.frozen_material_snapshot).design,
        )
    # Original material pairs only. The user has not authorized a different nk
    # source or an arbitrary new material pool.
    pairs = list(
        dict.fromkeys((r["material_id"], r["optical_set_id"]) for r in source["front_layers"])
    )
    for base in (removed, raised):
        rows = base["front_layers"]
        for index in range(len(rows) + 1):
            for material, optical in pairs:
                candidate = deepcopy(dict(base))
                identifier = f"repair-{physical_design_hash(base)[:16]}-{index}-{pairs.index((material, optical))}"
                existing_ids = {row["id"] for row in rows}
                while identifier in existing_ids:
                    identifier += "-new"
                candidate["front_layers"].insert(
                    index,
                    dict(
                        id=identifier,
                        material_id=material,
                        optical_set_id=optical,
                        thickness=dict(value=target, unit="nm"),
                    ),
                )
                yield "insert_material_layer", candidate


def _guard_request(request: FilmSynthesisRequest, tolerance: float) -> FilmSynthesisRequest:
    if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("optical tolerance must be finite nonnegative fraction")
    criterion = FilmObjectiveConstraint(
        mode="range",
        target_value=None,
        lower_bound=max(0.0, 0.5 - tolerance / 2),
        upper_bound=min(1.0, 0.5 + tolerance / 2),
    )
    return replace(
        request,
        acceptable_normalized_error=0.0,
        objectives=tuple(
            replace(
                o, pointwise=criterion, mean=replace(o.mean, enabled=False), normalization_scale=1.0
            )
            for o in request.objectives
        ),
    )


class CurvePair:
    """One frozen source, paired on each exact requested comparison grid."""

    def __init__(
        self,
        request: FilmSynthesisRequest,
        source: Mapping[str, Any],
        physical: Callable[..., Value],
    ):
        self.request, self.source, self.physical = request, deepcopy(dict(source)), physical
        self.cache: OrderedDict[tuple, Value] = OrderedDict()
        self.points: dict[tuple[int, float], dict[float, tuple[float, float]]] = {}
        self.pending_candidate: Mapping[str, Any] | None = None

    def retained_values(self):
        return self.request, self.source, self.cache, self.points, self.pending_candidate

    @staticmethod
    def _rows(objectives, value):
        expected = {(i, float(a)) for i, o in enumerate(objectives) for a in o.angles_deg}
        rows = {}
        for row in value["responses"]:
            index, angle = row.get("objective_index"), row.get("angle_deg")
            if (
                type(index) is not int
                or not 0 <= index < len(objectives)
                or isinstance(angle, bool)
                or not isinstance(angle, Real)
                or not math.isfinite(angle)
            ):
                raise ValueError("curve response physical key is invalid")
            key = index, float(angle)
            objective = objectives[index]
            if (
                key not in expected
                or key in rows
                or row.get("metric", objective.metric) != objective.metric
                or row.get("polarization", objective.polarization) != objective.polarization
            ):
                raise ValueError("curve response samples have duplicate or different physical keys")
            waves = np.asarray(row["wavelength_nm"], float)
            values = np.asarray(row["actual_values"], float)
            expected_waves = np.linspace(
                objective.wavelength_min_nm,
                objective.wavelength_max_nm,
                objective.wavelength_samples,
            )
            if (
                waves.ndim != 1
                or not np.array_equal(waves, expected_waves)
                or values.shape != waves.shape
                or not np.all(np.isfinite(values))
            ):
                raise ValueError("curve response grid or values are incomplete")
            rows[key] = row
        if set(rows) != expected:
            raise ValueError("curve response samples are missing a physical key")
        return rows

    def transform(
        self, objectives, candidate: Mapping[str, Any], index: int | None = None
    ) -> Value:
        if index is None:
            if len(objectives) != len(self.request.objectives):
                raise ValueError("curve objective scope differs")
            scope = tuple(enumerate(objectives))
        else:
            if (
                type(index) is not int
                or not 0 <= index < len(self.request.objectives)
                or len(objectives) != 1
            ):
                raise ValueError("curve auxiliary objective scope differs")
            scope = ((index, objectives[0]),)
        for global_index, objective in scope:
            original = self.request.objectives[global_index]
            if (
                objective.metric != original.metric
                or objective.polarization != original.polarization
                or not set(objective.angles_deg) <= set(original.angles_deg)
                or objective.wavelength_min_nm < original.wavelength_min_nm
                or objective.wavelength_max_nm > original.wavelength_max_nm
            ):
                raise ValueError("curve band/angle/metric/polarization scope differs")
        self.pending_candidate = candidate
        key = tuple(
            (
                o.wavelength_min_nm,
                o.wavelength_max_nm,
                o.wavelength_samples,
                o.angles_deg,
                o.polarization,
                o.metric,
            )
            for o in objectives
        )
        if key not in self.cache:
            self.cache[key] = self.physical(self.source, objectives=objectives, jacobian=False)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
        reference = self.cache[key]
        rows = []
        refs = self._rows(objectives, reference)
        candidates = self._rows(objectives, candidate)
        for row in candidates.values():
            local_index, angle = int(row["objective_index"]), float(row["angle_deg"])
            ref = refs[local_index, angle]
            waves = np.asarray(row["wavelength_nm"], float)
            if not np.array_equal(waves, np.asarray(ref["wavelength_nm"], float)):
                raise ValueError("curve comparison grids differ")
            actual, before = (
                np.asarray(row["actual_values"], float),
                np.asarray(ref["actual_values"], float),
            )
            if (
                actual.shape != before.shape
                or not np.all(np.isfinite(actual))
                or not np.all(np.isfinite(before))
            ):
                raise ValueError("curve comparison is incomplete or nonfinite")
            global_index = local_index if index is None else index
            points = self.points.setdefault((global_index, angle), {})
            for wave, a, b in zip(waves, actual, before, strict=True):
                points[float(wave)] = (float(a), float(b))
            difference = actual - before
            changed = dict(row, actual_values=0.5 + 0.5 * difference, extremum_values=difference)
            if "jacobian_values_by_layer_id" in row:
                changed["jacobian_values_by_layer_id"] = {
                    k: 0.5 * np.asarray(v, float)
                    for k, v in row["jacobian_values_by_layer_id"].items()
                }
            rows.append(changed)
        value = dict(candidate)
        value["responses"] = rows
        reduced = reduce_objective_responses(
            objectives, rows, layer_ids=tuple(candidate.get("variable_ids", ()))
        )
        value["score"] = reduction_score(reduced, 0.0)
        self.pending_candidate = None
        return value

    def evidence(self, tolerance: float) -> Value:
        records = []
        for (index, angle), points in sorted(self.points.items()):
            objective = self.request.objectives[index]
            waves = sorted(points)
            records.append(
                dict(
                    objective_index=index,
                    angle_deg=angle,
                    metric=objective.metric,
                    polarization=objective.polarization,
                    wavelength_nm=waves,
                    candidate_values=[points[w][0] for w in waves],
                    reference_values=[points[w][1] for w in waves],
                )
            )
        maximum = max(
            (abs(a - b) for points in self.points.values() for a, b in points.values()), default=0.0
        )
        expected_keys = {
            (i, float(a)) for i, o in enumerate(self.request.objectives) for a in o.angles_deg
        }
        complete = (
            bool(expected_keys)
            and set(self.points) == expected_keys
            and all(
                covers_grid(
                    sorted(self.points[i, float(a)]),
                    np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples),
                )
                for i, o in enumerate(self.request.objectives)
                for a in o.angles_deg
            )
        )
        passed = complete and all(
            paired_interval_pass(
                [a for a, _ in points.values()], [b for _, b in points.values()], tolerance
            )
            for points in self.points.values()
        )
        return dict(
            maximum_absolute_change=maximum,
            tolerance_fraction=tolerance,
            passed=passed,
            records=records,
            continuous_certificate=False,
        )


def automatic_repair(
    request: FilmSynthesisRequest,
    source: Mapping[str, Any],
    *,
    target_nm: float,
    thickness_tolerance_nm: float,
    optical_tolerance: float,
    cancel_event=None,
    report=lambda value: None,
    checkpoint=lambda value: None,
    policy: Mapping[str, Any] | None = None,
    external_retained_bytes: Callable[[], int] = lambda: 0,
    optical_settings: Mapping[str, Any] | None = None,
    original_targets_present: bool = True,
) -> Value:
    """Bounded structural search, complete local refinement, paired audit.

    Policy is internal and recorded. A limit is not a proof of infeasibility.
    Original criteria and physical curves remain separate from delta guidance.
    """
    from .film_design_service import simulation_settings

    inherited_settings = (request.frozen_material_snapshot or {}).get("settings")
    optical_settings = simulation_settings(deepcopy(dict(
        optical_settings if optical_settings is not None else inherited_settings or {}
    )))
    if type(original_targets_present) is not bool:
        raise ValueError("original target applicability must be boolean")
    controls = effective_policy(request, policy)
    floor = lower_limit(target_nm, thickness_tolerance_nm)
    guide = _guard_request(request, optical_tolerance)
    # A small internal cushion helps dense/extremum verification; the formal
    # guard below always uses the user's exact tolerance, including zero.
    local_tolerance = optical_tolerance - min(optical_tolerance * 0.01, 1e-5)
    local_delta = _guard_request(request, local_tolerance)
    source = validate_design(deepcopy(dict(source)))
    if (
        source["back_layers"]
        or source["first_surface"] != "front"
        or optical_settings["first_surface"] != source["first_surface"]
        or source["substrate"]["sides"] != "single"
    ):
        raise ValueError("automatic repair requires single front incidence")
    if not legal_thicknesses(request, source, 0.0):
        raise ValueError(
            "automatic repair source violates native physical limits or is not physical nm"
        )
    started = monotonic()
    work = dict(
        physical_calls=0,
        wavelength_points=0,
        candidates=0,
        local_calls=0,
        audit_calls=0,
        reference_calls=0,
        partial_physical_work_possible=False,
    )
    source_snapshot: Value | None = None
    champion: Value | None = None
    reviewed: Value | None = None
    trials: list[dict[str, Any]] = []
    reason = "search_quantum_complete"
    active_pairs: list[CurvePair] = []
    owners: dict[str, Any] = {}
    audit_reservation = 0

    def retained_bytes():
        external = external_retained_bytes()
        if type(external) is not int or external < 0:
            raise ValueError("external retained bytes must be a nonnegative integer")
        return external + _owned_bytes(
            request,
            guide,
            source,
            source_snapshot,
            champion,
            reviewed,
            owners,
            *(pair.retained_values() for pair in active_pairs),
        )

    def admit_memory(working):
        limit = (request.resource_guard_resolution or {}).get("effective_bytes")
        if limit is not None and working + retained_bytes() > limit:
            raise SynthesisResourceGuard("resource_guard")

    def check(*, budget=True):
        if cancel_event is not None and cancel_event.is_set():
            raise _StructureCancelled()
        if monotonic() - started >= controls["seconds"]:
            raise RepairLimit("time_limit")
        if budget and work["physical_calls"] >= controls["physical_calls"]:
            raise RepairLimit("evaluation_limit")

    def physical(design, *, objectives=None, jacobian=False):
        check()
        active = request if objectives is None else replace(request, objectives=tuple(objectives))
        estimate = manufacturing_refinement_working_set_bytes(active, len(design["front_layers"]))
        admit_memory(estimate + audit_reservation)
        work["physical_calls"] += 1
        if objectives is not None:
            work["reference_calls"] += 1
        work["wavelength_points"] += sum(
            o.wavelength_samples * len(o.angles_deg) for o in active.objectives
        )

        class Cancel:
            def is_set(self):
                check(budget=False)
                return False

        previous_partial = work["partial_physical_work_possible"]
        work["partial_physical_work_possible"] = True
        evaluation_options: dict[str, Any] = {} if optical_settings is None else dict(optical_settings=optical_settings)
        value = evaluate_frozen_structure_candidate(
            _proxy(active),
            design,
            variable_ids=tuple(r["id"] for r in design["front_layers"]) if jacobian else (),
            with_jacobian=jacobian,
            cancel_event=Cancel(),
            **evaluation_options,
        )
        work["partial_physical_work_possible"] = previous_partial
        return value

    def audited(design, active, transform=None):
        nonlocal audit_reservation
        from .film_manufacturing_jobs import estimate_audit_resources

        reserve = estimate_audit_resources(
            active,
            design,
            physical_call_limit=max(0, controls["physical_calls"] - work["physical_calls"]),
        )
        audit_reservation = reserve["working_set_bytes"]
        admit_memory(audit_reservation)
        previous_partial = work["partial_physical_work_possible"]
        work["partial_physical_work_possible"] = True

        class Cancel:
            def is_set(self):
                check(budget=False)
                return False

        previous = [0, 0]

        def progress(value):
            calls, points = value["physical_evaluations"], value["wavelength_points"]
            completed = calls - previous[0]
            work["wavelength_points"] += points - previous[1]
            previous[:] = calls, points
            if not completed:
                check()
                work["physical_calls"] += (
                    1  # Reserve before paired-reference work can take the last slot.
                )
            if value["stage"] in {"audit_dense", "audit_offset", "audit_extremum"}:
                report(dict(stage="repair_audit", **work))

        try:
            audit_options: dict[str, Any] = {} if optical_settings is None else dict(optical_settings=optical_settings)
            result = audit_frozen_synthesis_candidate(
                active,
                design,
                cancel_event=Cancel(),
                progress_callback=progress,
                response_transform=transform,
                **audit_options,
            )
        finally:
            audit_reservation = 0
        work["audit_calls"] += 1
        work["partial_physical_work_possible"] = previous_partial
        # The auditor owns physical execution and reports its true call count.
        return result

    def result():
        return dict(
            source=source_snapshot,
            best=champion,
            reviewed=reviewed,
            detected_layer_ids=deficient_ids(source, floor),
            lower_limit_nm=floor,
            policy=controls,
            work=deepcopy(work),
            trace=deepcopy(trials),
            elapsed_seconds=monotonic() - started,
            termination=reason,
        )

    try:
        check()
        original = physical(source)
        owners["source_training"] = original
        original_audit = audited(source, request)
        frozen_source = manufacturing_snapshot(request, dict(original), audit_value=original_audit)
        source_snapshot = dict(
            design=deepcopy(source),
            training_score=frozen_source.training_score,
            audit_score=frozen_source.audit_score,
        )
        del frozen_source
        owners.clear()
        if not deficient_ids(source, floor):
            reason = "no_thin_layers"
            pair = CurvePair(guide, source, lambda _design, computed=original, **_kwargs: computed)
            active_pairs[:] = [pair]
            pair.transform(guide.objectives, original)
            identical_guard = pair.evidence(optical_tolerance)
            identical_guard["audit"] = dict(
                kind="identical_frozen_design", physical_hash=physical_design_hash(source)
            )
            unchanged = dict(
                **source_snapshot,
                optical_guard=identical_guard,
                thickness_pass=True,
            )
            reviewed = unchanged
            champion = unchanged if candidate_accepted(unchanged, original_targets_present) else None
            return result()
        del original, original_audit
        seen: set[str] = set()
        frontier = structural_candidates(request, source, target_nm, floor)
        for operator, proposed in frontier:
            check()
            if work["candidates"] >= controls["proposals"]:
                reason = "proposal_quantum_limit"
                break
            design = validate_design(deepcopy(dict(proposed)))
            if not legal_thicknesses(request, design, floor):
                continue
            identity = physical_design_hash(design)
            if identity in seen:
                continue
            seen.add(identity)
            work["candidates"] += 1
            feedback: list[tuple[str, int, FilmOptimizationObjective]] = []
            for feedback_round in range(AUDIT_FEEDBACK_LIMIT + 1):
                check()
                pair = CurvePair(local_delta, source, physical)
                extra_pairs = [
                    CurvePair(replace(request, objectives=(objective,)), source, physical)
                    if kind == "difference" else None
                    for kind, _index, objective in feedback
                ]
                active_pairs[:] = [pair, *(p for p in extra_pairs if p is not None)]
                owners.clear()
                local_best: Value | None = None
                originals = tuple(_interior_margin_objective(o) for o in request.objectives) if original_targets_present else ()
                extra_objectives = tuple(
                    replace(objective, pointwise=local_delta.objectives[index].pointwise,
                            mean=replace(objective.mean, enabled=False), normalization_scale=1.0)
                    if kind == "difference" else _interior_margin_objective(objective)
                    for kind, index, objective in feedback
                )
                local_guide = replace(local_delta,
                    objectives=local_delta.objectives + originals + extra_objectives,
                    minimum_nonzero_thickness_nm=max(
                        floor, request.minimum_nonzero_thickness_nm or 0.0, np.finfo(float).tiny))
                owners["local_guide"] = local_guide
                admit_memory(manufacturing_refinement_working_set_bytes(local_guide, len(design["front_layers"])))

                def evaluate(candidate, *, jacobian=True, curve_pair=pair, operation=operator,
                             specifications=tuple(feedback), auxiliary_objectives=extra_objectives,
                             auxiliary_pairs=extra_pairs, guide_request=local_guide,
                             protected_objectives=originals, attempt=feedback_round):
                    nonlocal local_best
                    value = physical(candidate, jacobian=jacobian)
                    owners["incoming"] = value
                    transformed = curve_pair.transform(local_delta.objectives, value)
                    rows = list(transformed["responses"])
                    if original_targets_present:
                        rows.extend(dict(row, objective_index=row["objective_index"] + len(request.objectives))
                                    for row in value["responses"])
                    for position, ((kind, _index, _objective), objective) in enumerate(zip(specifications, auxiliary_objectives, strict=True)):
                        extra = physical(candidate, objectives=(objective,), jacobian=jacobian)
                        extra_pair = auxiliary_pairs[position]
                        if kind == "difference":
                            assert extra_pair is not None
                            extra = extra_pair.transform((objective,), extra)
                        rows.extend(dict(row, objective_index=len(local_delta.objectives) + len(protected_objectives) + position)
                                    for row in extra["responses"])
                    transformed["responses"] = rows
                    transformed["variable_ids"] = tuple(r["id"] for r in candidate["front_layers"])
                    transformed["score"] = reduction_score(reduce_objective_responses(
                        guide_request.objectives, rows, layer_ids=transformed["variable_ids"]), 0.0)
                    if local_best is None or transformed["score"]["key"] < local_best["score"]["key"]:
                        local_best = transformed
                        owners["local_best"] = local_best
                    owners["incoming"] = transformed
                    work["local_calls"] += 1
                    report(dict(stage="repair_local", operator=operation, feedback_round=attempt, **work))
                    return transformed

                phase_started = monotonic()
                initial = evaluate(design)
                refined, diagnostics = refine_synthesis_full_minimax(
                    local_guide, initial, evaluate=evaluate, archive=lambda *a, **k: None,
                    cancel_check=check, max_iterations=controls["local_iterations"])
                local_seconds = monotonic() - phase_started
                design = refined["design"]
                if not legal_thicknesses(request, design, floor):
                    raise ValueError("local repair returned a newly deficient layer")
                # Fresh owners bind both training and audit evidence to the
                # returned best, which need not be the solver's last trial.
                raw = physical(design)
                owners["returned_training"] = raw
                training_pair = CurvePair(guide, source, physical)
                active_pairs.append(training_pair)
                training_pair.transform(guide.objectives, raw)
                training_guard = training_pair.evidence(optical_tolerance)
                owners["training_guard"] = training_guard
                verification = CurvePair(guide, source, physical)
                active_pairs.append(verification)
                audit_started = monotonic()
                guard_audit = audited(design, guide, verification.transform)
                owners["guard_audit"] = guard_audit
                guard = verification.evidence(optical_tolerance)
                guard["audit"] = curve_audit_metadata(guard_audit)
                original_candidate_audit = audited(design, request)
                owners["original_audit"] = original_candidate_audit
                audit_seconds = monotonic() - audit_started
                frozen_candidate = manufacturing_snapshot(request, dict(raw), audit_value=original_candidate_audit)
                snapshot = dict(design=deepcopy(design), training_score=frozen_candidate.training_score,
                    audit_score=frozen_candidate.audit_score, optical_guard=guard, thickness_pass=True)
                del frozen_candidate
                reviewed = snapshot
                accepted = candidate_accepted(snapshot, original_targets_present) and training_guard["passed"]
                trials.append(dict(operator=operator, physical_hash=physical_design_hash(design),
                    layers=len(design["front_layers"]), maximum_absolute_change=guard["maximum_absolute_change"],
                    accepted=bool(accepted), feedback_rounds=feedback_round,
                    feedback_points=len(feedback), local_termination=diagnostics.get("termination", "unknown"),
                    audit_feedback_limit=AUDIT_FEEDBACK_LIMIT, feedback_point_limit=AUDIT_FEEDBACK_POINTS,
                    guide_tolerance_fraction=local_tolerance, formal_tolerance_fraction=optical_tolerance,
                    original_guide_margin="range_interior_only_max_1e-5_fraction",
                    solver_terminal_hash=diagnostics.get("terminal_physical_hash"),
                    returned_role=diagnostics.get("returned_role", "returned_best"),
                    training_difference=_difference_summary(training_guard, design),
                    audit_difference=_difference_summary(guard, design),
                    original_targets_present=original_targets_present,
                    original_training_max_error=raw["score"]["max_error"],
                    original_audit_max_error=original_candidate_audit["score"]["max_error"],
                    original_training_pass=raw["score"]["nominal_pass"],
                    original_audit_pass=original_candidate_audit["score"]["nominal_pass"],
                    local_seconds=local_seconds, audit_seconds=audit_seconds))
                next_feedback = ([] if accepted or feedback_round >= AUDIT_FEEDBACK_LIMIT else
                    _audit_feedback(request, guard, original_candidate_audit,
                        original_targets_present=original_targets_present))
                # Drop numerical owners before checkpoint JSON admission.
                owners.clear()
                active_pairs.clear()
                del initial, refined, local_best, raw, original_candidate_audit, guard_audit, evaluate
                del pair, extra_pairs, training_pair, verification
                if accepted:
                    champion = snapshot
                    reason = "repair_feasible"
                    checkpoint(result())
                    report(dict(stage="repair_audited_best", maximum_absolute_change=guard["maximum_absolute_change"], **work))
                    if cancel_event is not None and cancel_event.is_set():
                        raise _StructureCancelled()
                    break
                if not next_feedback:
                    break
                feedback = next_feedback
                report(dict(stage="repair_audit_feedback", feedback_points=len(feedback), **work))
            if champion is not None:
                break
    except _StructureCancelled:
        reason = "cancelled"
    except RepairLimit as error:
        reason = str(error)
    except SynthesisStageBudget:
        reason = "evaluation_limit"
    except SynthesisResourceGuard:
        reason = "resource_guard"
    return result()
