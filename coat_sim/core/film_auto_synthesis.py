"""GUI-independent synthesis from a bare substrate using analytic needles.

The working growth branch may get worse; the separately scored incumbent never
does. No random layer generation, application to a session, or implicit work
budget is involved. See docs/film-auto-synthesis-contract.md and algorithms.html.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from typing import Any, Callable, Mapping

import numpy as np

from .film_design import MAX_LAYERS, physical_thickness_nm, validate_design
from .film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, freeze_film_optimization_request
from .film_optimization_needle import FilmNeedleMaterialReference, _candidate_payload_from_materials, _normalize_objectives
from .film_optimization_structure import (
    FilmStructureOptimizationRequest, _StructureCancelled, _insert_at_site,
    _site_records, evaluate_frozen_structure_candidate,
)
from .optical_constants import UNITS_TO_UM, prepare_optical_set
from .film_synthesis_absorption import (annotate_synthesis_design, assumes_unknown_k_zero,
                                        sample_synthesis_optical)


@dataclass(frozen=True)
class FilmSynthesisRequest:
    """environment is a valid design with no front/back layers; it is copied.

    None means no user limit. position_samples and growth fractions control
    numerical exploration, not manufacturing rules or evaluation budgets.
    environment.assume_lossless is the explicit unknown-k=0 opt-in (default
    false). Known k, including nonzero absorption, is always retained and every
    n/k source domain remains strict; use_k must stay true.
    seed_designs are explicit additional real starting branches, with the same
    physical environment. Their materials are frozen even outside the insertion
    pool; positive physical thicknesses must satisfy every explicit constraint.
    """

    environment: Mapping[str, Any]
    objectives: tuple[FilmOptimizationObjective, ...]
    candidate_materials: tuple[FilmNeedleMaterialReference, ...]
    design_revision: int = 0
    minimum_nonzero_thickness_nm: float | None = None
    max_layer_thickness_nm: float | None = None
    max_total_thickness_nm: float | None = None
    max_layers: int | None = None
    max_cycles: int | None = None
    max_evaluations: int | None = None
    acceptable_normalized_error: float = 1e-3
    position_samples: int = 3
    growth_quarter_wave_fractions: tuple[float, ...] = (0.5, 1.0, 2.0)
    needle_seed_fraction: float = 1e-3
    relative_improvement_tolerance: float = 1e-9
    local_step_tolerance: float = 1e-6
    stagnation_cycles: int = 2
    validation_samples_multiplier: int = 4
    frozen_material_snapshot: Mapping[str, Any] | None = None
    seed_designs: tuple[Mapping[str, Any], ...] = ()
    cpu_workers: int = 1
    forward_backend: str = "cpu"
    forward_device: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FilmSynthesisProgress:
    stage: str
    cycle: int
    evaluations: int
    layer_count: int
    best_max_normalized_error: float | None
    message: str


@dataclass(frozen=True)
class FilmSynthesisTrace:
    stage: str
    cycle: int
    evaluations: int
    layer_count: int
    max_normalized_error: float | None = None
    material_id: str = ""
    position_nm: float | None = None
    sensitivity: float | None = None
    message: str = ""


@dataclass(frozen=True)
class FilmSynthesisResult:
    state: str
    termination_reason: str
    candidate_design: Mapping[str, Any] | None
    design_revision: int
    targets_met: bool
    training_score: Mapping[str, Any] | None
    validation_score: Mapping[str, Any] | None
    validation_complete: bool
    cycles: int
    evaluations: int
    validation_evaluations: int
    failed_evaluations: int
    trace: tuple[FilmSynthesisTrace, ...]
    message: str
    frozen_request: FilmSynthesisRequest
    audit_physical_evaluations: int = 0
    audit_wavelength_points: int = 0
    performance: Mapping[str, Any] = field(default_factory=dict)
    candidate_provenance: Mapping[str, Any] = field(default_factory=dict)


def _number(value, name, *, zero=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result) or (result < 0 if zero else result <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return result


def _validated(request):
    if not isinstance(request, FilmSynthesisRequest):
        raise ValueError("request must be FilmSynthesisRequest")
    if not isinstance(request.forward_backend, str) or request.forward_backend not in {"cpu", "cuda", "rocm", "xpu"}:
        raise ValueError("forward_backend must be cpu, cuda, rocm or xpu")
    forward_device = None
    if request.forward_backend != "cpu" or request.forward_device is not None:
        from .film_synthesis_forward import validate_forward_device
        forward_device = validate_forward_device(request.forward_device, request.forward_backend)
    design = validate_design(deepcopy(request.environment))
    if design["front_layers"] or design["back_layers"]:
        raise ValueError("automatic synthesis requires a bare substrate environment")
    if design["substrate"]["sides"] != "single" or design["first_surface"] != "front":
        raise ValueError("automatic synthesis supports single-sided front incidence only")
    if not design["use_k"]:
        raise ValueError("automatic synthesis requires use_k=True to retain absorption")
    if type(request.cpu_workers) is not int or request.cpu_workers < 0:
        raise ValueError("cpu_workers must be a nonnegative integer (0=auto, 1=serial)")
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if not isinstance(request.objectives, (tuple, list)):
        raise ValueError("objectives must be a sequence")
    objectives = _normalize_objectives(request.objectives)
    if not objectives:
        raise ValueError("at least one objective is required")
    if not isinstance(request.candidate_materials, (tuple, list)) or not request.candidate_materials:
        raise ValueError("explicit candidate materials are required")
    refs = set()
    for ref in request.candidate_materials:
        if not isinstance(ref, FilmNeedleMaterialReference):
            raise ValueError("candidate materials must be FilmNeedleMaterialReference values")
        pair = (ref.material_id, ref.optical_set_id)
        if any(not isinstance(v, str) or not v or len(v) > 128 for v in pair) or pair in refs:
            raise ValueError("candidate references must be valid and unique")
        refs.add(pair)
    for name in ("max_layers", "max_cycles", "max_evaluations", "position_samples",
                 "stagnation_cycles", "validation_samples_multiplier"):
        value = getattr(request, name)
        if value is None and name.startswith("max_"):
            continue
        if type(value) is not int or value < (0 if name == "position_samples" else 1):
            raise ValueError(f"{name} must be an integer in its positive domain")
    if request.validation_samples_multiplier < 2:
        raise ValueError("validation_samples_multiplier must be at least 2")
    if request.max_evaluations is not None and request.max_evaluations < 2:
        raise ValueError("max_evaluations must allow baseline and independent validation")
    if request.max_layers is not None and request.max_layers > MAX_LAYERS:
        raise ValueError(f"max_layers exceeds the design schema resource limit {MAX_LAYERS}")
    numeric = {}
    for name in ("minimum_nonzero_thickness_nm", "max_layer_thickness_nm", "max_total_thickness_nm"):
        value = getattr(request, name)
        if value is not None:
            numeric[name] = _number(value, name)
    if (numeric.get("minimum_nonzero_thickness_nm") is not None and numeric.get("max_layer_thickness_nm") is not None
            and numeric["minimum_nonzero_thickness_nm"] > numeric["max_layer_thickness_nm"]):
        raise ValueError("minimum nonzero thickness exceeds layer maximum")
    numeric["acceptable_normalized_error"] = _number(request.acceptable_normalized_error, "acceptable_normalized_error", zero=True)
    for name in ("needle_seed_fraction", "relative_improvement_tolerance", "local_step_tolerance"):
        numeric[name] = _number(getattr(request, name), name)
    if numeric["needle_seed_fraction"] > 1 or numeric["local_step_tolerance"] >= 1:
        raise ValueError("needle seed fraction and local step tolerance must be small fractions")
    if not isinstance(request.growth_quarter_wave_fractions, (tuple, list)) or not request.growth_quarter_wave_fractions:
        raise ValueError("growth_quarter_wave_fractions must be nonempty")
    for fraction in request.growth_quarter_wave_fractions:
        _number(fraction, "growth quarter-wave fraction")
    if not isinstance(request.seed_designs, (tuple, list)):
        raise ValueError("seed_designs must be a sequence of real design documents")
    seeds = []
    physical_keys = ("substrate", "incident_medium", "exit_medium", "first_surface",
                     "layer_order", "reference_wavelength_nm", "use_k", "assume_lossless")
    for raw in request.seed_designs:
        seed = validate_design(deepcopy(raw))
        if seed["back_layers"] or any(seed[key] != design[key] for key in physical_keys):
            raise ValueError("seed designs must share the bare environment and have front layers only")
        for row in seed["front_layers"]:
            if not row["material_id"] or not row["optical_set_id"] or row["thickness"]["value"] <= 0:
                raise ValueError("seed layers require explicit materials, optical sets and positive thickness")
        if len(seed["front_layers"]) > (request.max_layers or MAX_LAYERS):
            raise ValueError("seed layer count exceeds the explicit request constraint")
        seeds.append(seed)
    return replace(request, environment=design, objectives=objectives, seed_designs=tuple(seeds), forward_device=forward_device,
                   candidate_materials=tuple(request.candidate_materials),
                   growth_quarter_wave_fractions=tuple(float(v) for v in request.growth_quarter_wave_fractions),
                   **numeric)


def _proxy(request):
    # These legacy budget fields are unused by the pure evaluator. In particular
    # this does not invoke the legacy bounded/single-cycle structure search.
    base = FilmOptimizationRequest(
        request.environment, request.design_revision, (), request.objectives,
        acceptable_normalized_error=request.acceptable_normalized_error,
        validation_samples_multiplier=request.validation_samples_multiplier,
        frozen_material_snapshot=request.frozen_material_snapshot,
    )
    return FilmStructureOptimizationRequest(
        base, min_thickness_nm=0.0,
        # The Jacobian helper requires finite numeric bounds, but does not clip
        # the candidate. This is representable float range, not a search cap.
        max_thickness_nm=np.finfo(float).max,
    )


def freeze_film_synthesis_request(service: Any, request: FilmSynthesisRequest,
                                  *, cancel_event: Any = None) -> FilmSynthesisRequest:
    request = _validated(request)
    _check_cancel(cancel_event)
    base = freeze_film_optimization_request(service, _proxy(request).base_request)
    if base.frozen_material_snapshot is None:
        raise ValueError("material freeze did not produce a snapshot")
    references = list(request.candidate_materials)
    pairs = {(r.material_id, r.optical_set_id) for r in references}
    for seed in request.seed_designs:
        for row in seed["front_layers"]:
            pair = (row["material_id"], row["optical_set_id"])
            if pair not in pairs:
                references.append(FilmNeedleMaterialReference(*pair))
                pairs.add(pair)
    frozen = _candidate_payload_from_materials(service, base.frozen_material_snapshot, references)
    result = replace(request, frozen_material_snapshot=frozen)
    # Strict source-domain checks stay active in both policies. The existing
    # environment bool opts into zero only when no k source exists at all.
    rows = [result.environment["substrate"]]
    if result.environment["incident_medium"]["kind"] != "constant":
        rows.append(result.environment["incident_medium"])
    rows.extend({"material_id": r.material_id, "optical_set_id": r.optical_set_id}
                for r in references)
    materials = {r["id"]: r for r in frozen["materials"]}
    waves = {float(w) for o in request.objectives for w in np.linspace(
        o.wavelength_min_nm, o.wavelength_max_nm,
        1 if o.wavelength_samples == 1 else (o.wavelength_samples - 1) * request.validation_samples_multiplier + 1)}
    waves.add(_reference_wave(request))
    if any(r["thickness"]["unit"] == "qw" for seed in request.seed_designs for r in seed["front_layers"]):
        waves.add(request.environment["reference_wavelength_nm"])
    for row in rows:
        optical = next(o for o in materials[row["material_id"]]["optical_sets"]
                       if o["id"] == row["optical_set_id"])
        sample = prepare_optical_set(optical, k_sampling_policy="source")
        domains = ", ".join(f"[{o.wavelength_min_nm:g}, {o.wavelength_max_nm:g}] nm" for o in request.objectives)
        for wave in sorted(waves):
            _check_cancel(cancel_event)
            context = f"material={row['material_id']}, optical_set={row['optical_set_id']}, wavelength={wave:g} nm; target intervals: {domains}"
            try:
                n, k, details = sample_synthesis_optical(sample, wave,
                    assume_unknown_k_zero=assumes_unknown_k_zero(request))
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(f"synthesis optical coverage failure ({context}): {exc}") from exc
            if not math.isfinite(float(n)) or float(n) <= 0 or k is None or not math.isfinite(float(k)):
                raise ValueError(f"synthesis requires finite positive n and explicit finite k ({context})")
            if not details.get("k_known") and not assumes_unknown_k_zero(request):
                raise ValueError(f"synthesis requires known k; unknown absorption cannot be assumed zero ({context})")
    seeds = []
    for original in result.seed_designs:
        seed = deepcopy(original)
        for row in seed["front_layers"]:
            index = 1.0
            if row["thickness"]["unit"] == "qw":
                optical = next(o for o in materials[row["material_id"]]["optical_sets"]
                               if o["id"] == row["optical_set_id"])
                index, _, _ = prepare_optical_set(optical)(seed["reference_wavelength_nm"], "nm",
                    extrapolation_policy="error", include_k=True)
            value = physical_thickness_nm(row, seed["reference_wavelength_nm"], index)
            row["thickness"] = {"value": value, "unit": "nm"}
        if not _feasible(result, seed):
            raise ValueError("seed physical thicknesses violate the explicit request constraints")
        seeds.append(seed)
    return replace(result, seed_designs=tuple(seeds))


def _feasible(request, design):
    ds = [r["thickness"]["value"] for r in design["front_layers"]]
    return (len(ds) <= (request.max_layers if request.max_layers is not None else MAX_LAYERS)
            and all(math.isfinite(d) and d > 0 and d >= (request.minimum_nonzero_thickness_nm or 0)
                    and (request.max_layer_thickness_nm is None or d <= request.max_layer_thickness_nm) for d in ds)
            and (request.max_total_thickness_nm is None or sum(ds) <= request.max_total_thickness_nm))


def _synthesis_candidate_rank(candidate, acceptable):
    """Project convention: worst violation, fixed-grid RMS, then manufacture.

    Once every audited/training condition passes, lower layer count and total
    physical thickness take priority. This does not change legacy solvers.
    """
    rows = candidate["design"]["front_layers"]
    score = candidate["score"]
    cost = (len(rows), sum(r["thickness"]["value"] for r in rows))
    if score["max_error"] <= acceptable:
        return (0, *cost, score["weighted_rms_error"], score["max_error"])
    return (1, score["max_error"], score["weighted_rms_error"], *cost)


def _phase_step_limits_nm(request, *, cancel_event=None):
    """Sampled complex propagation cap |2*pi*q*Delta_d/lambda| <= pi/4.

    This is a numerical step rule, not a high-Q power trust region or a
    continuous-domain certificate. Accepted moves still need real rescoring.
    """
    materials = {r["id"]: r for r in request.frozen_material_snapshot["materials"]}
    references = {(r.material_id, r.optical_set_id) for r in request.candidate_materials}
    references.update((r["material_id"], r["optical_set_id"])
                      for seed in request.seed_designs for r in seed["front_layers"])
    def optical(pair):
        return next(o for o in materials[pair[0]]["optical_sets"] if o["id"] == pair[1])
    incident = request.environment["incident_medium"]
    incident_sample = (None if incident["kind"] == "constant" else
                       prepare_optical_set(optical((incident["material_id"], incident["optical_set_id"])),
                                           k_sampling_policy="source"))
    result = {}
    for pair in sorted(references):
        _check_cancel(cancel_event)
        record = optical(pair)
        sample = prepare_optical_set(record, k_sampling_policy="source")
        knot_waves: set[float] = set()
        knot_records = [record, record.get("k_table", {}), record.get("k_data", {})]
        if incident_sample is not None:
            incident_record = optical((incident["material_id"], incident["optical_set_id"]))
            knot_records.extend((incident_record, incident_record.get("k_table", {}), incident_record.get("k_data", {})))
        for knot_record in knot_records:
            if isinstance(knot_record, Mapping) and knot_record.get("wavelength_unit") in UNITS_TO_UM:
                factor = 1000 * UNITS_TO_UM[knot_record["wavelength_unit"]]
                knot_waves.update(float(w) * factor for w in knot_record.get("wavelengths", ()))
        maximum = 0.0
        for objective in request.objectives:
            count = 1 if objective.wavelength_samples == 1 else (objective.wavelength_samples - 1) * request.validation_samples_multiplier + 1
            waves = set(float(w) for w in np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, count))
            waves.update(w for w in knot_waves if objective.wavelength_min_nm <= w <= objective.wavelength_max_nm)
            for wave in sorted(waves):
                _check_cancel(cancel_event)
                n, k, _ = sample_synthesis_optical(sample, float(wave),
                    assume_unknown_k_zero=assumes_unknown_k_zero(request))
                n0 = incident["n"] if incident_sample is None else sample_synthesis_optical(
                    incident_sample, float(wave),
                    assume_unknown_k_zero=assumes_unknown_k_zero(request))[0]
                for angle in objective.angles_deg:
                    beta = n0 * math.sin(math.radians(angle))
                    q = np.sqrt(complex(n, k) ** 2 - beta ** 2 + 0j)
                    maximum = max(maximum, 2 * math.pi * abs(q) / wave)
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("invalid physical phase-step scale")
        result[pair] = (math.pi / 4) / maximum
    return result


def _proposal_layer_ids(design, *preferred, reserved_ids=None):
    """Reserve existing IDs and allocate deterministic distinct proposal IDs.

    The optional mutable reserve also protects IDs in other starting
    branches, archived candidates and earlier proposals in this run. IDs
    within one conceptual proposal are reused for its zero/finite variants.
    Existing layer identifiers are never changed.
    """
    reserved = {row["id"] for side in ("front_layers", "back_layers") for row in design[side]}
    if reserved_ids is not None:
        reserved.update(reserved_ids)
    result = []
    for root in preferred:
        identifier = root
        suffix = 1
        while identifier in reserved:
            ending = f"-{suffix}"
            identifier = root[:128 - len(ending)] + ending
            suffix += 1
        reserved.add(identifier)
        result.append(identifier)
    if reserved_ids is not None:
        reserved_ids.update(result)
    return tuple(result)


def _reference_wave(request):
    first = request.objectives[0]
    return 0.5 * (first.wavelength_min_nm + first.wavelength_max_nm)


def _check_cancel(event):
    if event is not None and event.is_set():
        raise _StructureCancelled()


class _Stopped(Exception):
    pass


def synthesize_film_design(service: Any, request: FilmSynthesisRequest, *, cancel_event: Any = None,
                           progress_callback: Callable[[FilmSynthesisProgress], None] | None = None,
                           snapshot_callback: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], None] | None = None) -> FilmSynthesisResult:
    """Return a reviewable candidate; completed means training and audit passed.

    Invalid requests raise ValueError. Numerical failures, cancellation and
    exhausted user limits are explicit result states. Callbacks run on the
    caller's thread. Cancellation is cooperative between physical helper calls.
    snapshot_callback receives copied (design, training, validation, audit)
    only when a new best independently audited candidate is retained.
    Cancellation returns the best complete feasible training score committed
    by the parent before it observes cancellation. An existing complete audit
    is attached only when it belongs to that exact candidate; no audit starts
    to make the cancellation candidate available.
    """
    request = _validated(request)
    from .film_synthesis_parallel import EvaluationTask, InfrastructureFailure, OrderedEvaluationExecutor
    execution = OrderedEvaluationExecutor(request.cpu_workers, cancel_event)
    forward = None
    reserved_layer_ids = {row["id"] for design in (request.environment, *request.seed_designs)
                          for side in ("front_layers", "back_layers") for row in design[side]}
    evaluations = validation_evaluations = failures = cycle = 0
    best = None
    audited_best = None
    completed_training_best = None
    completed_training_validation = None
    fully_scored_evaluations = 0
    candidate_provenance = {}
    audited_signatures = set()
    validation = None
    audit_physical_evaluations = audit_wavelength_points = 0
    trace = []
    reason, state = "search_exhausted", "not_found"

    def performance():
        result = execution.diagnostics()
        if forward is not None:
            work = forward.diagnostics()
            result["forward"] = work
            for total, known, extra in (
                ("training_physical_evaluations", "known_training_physical_evaluations", work["known_objective_spectral_calls"]),
                ("training_wavelength_points", "known_training_wavelength_points", work["known_wavelength_points"])):
                result[known] += extra
                result[total] = result[known] if result[total] is not None and work["actual_work_complete"] else None
            result["known_training_evaluator_invocations"] += work["candidates_completed"]
            result["training_evaluator_invocations"] = (result["known_training_evaluator_invocations"]
                if result["training_evaluator_invocations"] is not None and work["actual_work_complete"] else None)
            result["actual_work_complete"] = result["actual_work_complete"] and work["actual_work_complete"]
        return result

    def emit(stage, message, design=None, score=None, **kwargs):
        layers = len(design["front_layers"]) if design is not None else 0
        error = score["max_error"] if score is not None else None
        trace.append(FilmSynthesisTrace(stage, cycle, evaluations, layers, error, message=message, **kwargs))
        if progress_callback:
            progress_callback(FilmSynthesisProgress(stage, cycle, evaluations, layers,
                (audited_best[1]["score"]["max_error"] if audited_best is not None else
                 None if best is None else best["score"]["max_error"]), message))

    def feasible(design):
        return _feasible(request, design)

    def evaluate(design, *, jacobian=False, zero_ids=(), holdout=False, outcome=None, gpu_forward=False):
        nonlocal evaluations, validation_evaluations, failures
        nonlocal audit_physical_evaluations, audit_wavelength_points
        nonlocal completed_training_best, completed_training_validation, fully_scored_evaluations
        _check_cancel(cancel_event)
        limit = request.max_evaluations
        if limit is not None and evaluations >= limit - (0 if holdout else 1):
            raise _Stopped("evaluation_limit")
        evaluations += 1
        if holdout:
            validation_evaluations += 1
        try:
            ids = tuple(r["id"] for r in design["front_layers"]) if jacobian else ()
            value: Mapping[str, Any]
            if holdout:
                from .film_synthesis_audit import audit_frozen_synthesis_candidate
                last_work = [0, 0]
                def audit_progress(work):
                    nonlocal audit_physical_evaluations, audit_wavelength_points
                    current = [int(work.get("physical_evaluations", last_work[0])),
                               int(work.get("wavelength_points", last_work[1]))]
                    audit_physical_evaluations += max(0, current[0] - last_work[0])
                    audit_wavelength_points += max(0, current[1] - last_work[1])
                    last_work[:] = current
                    emit("audit_progress", f"{work.get('stage', 'audit')}: {current[0]} physical evaluations, {current[1]} wavelength points", design)
                value = audit_frozen_synthesis_candidate(request, design, cancel_event=cancel_event,
                                                        progress_callback=audit_progress)
                work = value.get("audit", {})
                audit_physical_evaluations += max(0, int(work.get("physical_evaluations", 0)) - last_work[0])
                audit_wavelength_points += max(0, int(work.get("wavelength_points", 0)) - last_work[1])
            else:
                task = EvaluationTask(design, jacobian, tuple(zero_ids))
                def physical_call():
                    return evaluate_frozen_structure_candidate(_proxy(request), design,
                        variable_ids=ids, with_jacobian=jacobian, zero_thickness_layer_ids=zero_ids,
                        derived_validation_grid=False, cancel_event=cancel_event)
                if forward is not None and (outcome is not None or gpu_forward):
                    from .film_synthesis_forward import ForwardOutcome
                    if isinstance(outcome, ForwardOutcome):
                        value = forward.take(outcome)
                    elif gpu_forward:
                        value = forward.single(task)
                    else:
                        try:
                            value = execution.take(outcome)
                        except InfrastructureFailure:
                            value = execution.serial(physical_call, task, fallback=True)
                elif outcome is None:
                    value = execution.serial(physical_call, task)
                else:
                    try:
                        value = execution.take(outcome)
                    except InfrastructureFailure:
                        value = execution.serial(physical_call, task, fallback=True)
            # Expose the same synthesis ordering used by the archive; the
            # underlying physical evaluator keeps its legacy score semantics.
            value["score"]["key"] = _synthesis_candidate_rank(value, request.acceptable_normalized_error)
            _check_cancel(cancel_event)
            # This parent-owned commit boundary excludes zero probes, partial
            # scores and unconsumed speculative worker results. It performs no
            # extra physical work and does not change the search archive.
            if not holdout and not zero_ids and feasible(value["design"]):
                fully_scored_evaluations += 1
                if (completed_training_best is None or
                        _synthesis_candidate_rank(value, request.acceptable_normalized_error) <
                        _synthesis_candidate_rank(completed_training_best, request.acceptable_normalized_error)):
                    completed_training_best = {"design": deepcopy(value["design"]),
                                               "score": deepcopy(value["score"])}
                    completed_training_validation = None
            return value
        except _StructureCancelled:
            raise
        except (ValueError, ArithmeticError):
            failures += 1
            raise

    def rank(candidate):
        return _synthesis_candidate_rank(candidate, request.acceptable_normalized_error)

    def audit_archive(value):
        nonlocal audited_best, completed_training_validation
        signature = hashlib.sha256(json.dumps(value["design"], sort_keys=True, allow_nan=False).encode("utf-8")).digest()
        if signature in audited_signatures:
            return
        emit("validation", "Audit dense, offset and local-extremum physical responses", value["design"], value["score"])
        audited = evaluate(value["design"], holdout=True)
        audited_signatures.add(signature)
        if (completed_training_best is not None and
                completed_training_best["design"] == value["design"]):
            completed_training_validation = {"design": deepcopy(audited["design"]),
                                             "score": deepcopy(audited["score"])}
        if audited_best is None or rank(audited) < rank(audited_best[1]):
            audited_best = (deepcopy(value), deepcopy(audited))
            emit("audit_incumbent", "Saved best audited candidate", value["design"], audited["score"])
            if snapshot_callback:
                checkpoint_audit = deepcopy(audited.get("audit", {}))
                checkpoint_audit["synthesis_cumulative_work"] = {
                    "cycle": cycle, "evaluations": evaluations,
                    "validation_evaluations": validation_evaluations,
                    "failed_evaluations": failures,
                    "audit_physical_evaluations": audit_physical_evaluations,
                    "audit_wavelength_points": audit_wavelength_points}
                checkpoint_audit["synthesis_performance"] = performance()
                snapshot_callback(annotate_synthesis_design(request, value["design"]), deepcopy(value["score"]),
                                  deepcopy(audited["score"]), checkpoint_audit)
        emit("validation_complete", "Independent physical candidate audit completed", value["design"], audited["score"])
        _check_cancel(cancel_event)

    def targets_verified():
        return (audited_best is not None
                and audited_best[0]["score"]["max_error"] <= request.acceptable_normalized_error
                and audited_best[1]["score"]["max_error"] <= request.acceptable_normalized_error)

    def archive(value):
        nonlocal best
        if feasible(value["design"]) and (best is None or rank(value) < rank(best)):
            best = deepcopy(value)
            emit("incumbent", "已保存独立评分的候选，等待最终密网格验证", value["design"], value["score"])
        # A training-feasible candidate may be better on audit even when it
        # loses the training cost tie-break. Audit it before discarding it.
        if feasible(value["design"]) and value["score"]["max_error"] <= request.acceptable_normalized_error:
            audit_archive(value)


    def clean(design):
        result = deepcopy(design)
        result["front_layers"] = [r for r in result["front_layers"] if r["thickness"]["value"] > 0]
        return result

    def project(ds):
        ds = np.maximum(ds, 0.0)
        if request.max_layer_thickness_nm is not None:
            ds = np.minimum(ds, request.max_layer_thickness_nm)
        minimum = request.minimum_nonzero_thickness_nm
        if minimum is not None:
            ds = np.where(ds < minimum / 2, 0.0, np.maximum(ds, minimum))
        return ds

    def polish(value, protected_id=None):
        """Projected analytic gradient with backtracking to numerical stationarity."""
        if forward is not None and value["design"]["front_layers"] and value.get("gradient_by_layer_id") is None:
            # The same already scored candidate gets analytic CPU derivatives;
            # it is not a new logical candidate or a finite-difference step.
            _check_cancel(cancel_event)
            task = EvaluationTask(value["design"], True)
            forward.refresh_attempts += 1
            value = execution.serial(lambda: evaluate_frozen_structure_candidate(_proxy(request), task.design,
                variable_ids=tuple(r["id"] for r in task.design["front_layers"]),
                with_jacobian=True, cancel_event=cancel_event), task)
            forward.record_refresh(value)
            value["score"]["key"] = _synthesis_candidate_rank(value, request.acceptable_normalized_error)
            _check_cancel(cancel_event)
        # A complete feasible proposal is already a valid incumbent even when
        # the remaining budget cannot pay for any additional refinement.
        archive(value)
        design = clean(value["design"])
        if not design["front_layers"]:
            return value if design == value["design"] else evaluate(design)
        ids = {row["id"] for row in design["front_layers"]}
        if (design != value["design"] or
                set(value.get("gradient_by_layer_id") or ()) != ids):
            value = evaluate(design, jacobian=True)
        archive(value)
        step = 0.25
        while True:
            _check_cancel(cancel_event)
            if value["score"]["max_error"] <= request.acceptable_normalized_error:
                break
            rows = design["front_layers"]
            ds = np.array([r["thickness"]["value"] for r in rows])
            scales = np.array([quarter[(r["material_id"], r["optical_set_id"])] for r in rows])
            gradient = np.array([value["gradient_by_layer_id"][r["id"]] for r in rows]) * scales
            if protected_id is not None:
                gradient[[r["id"] == protected_id for r in rows]] = 0
            gradient[(ds == 0) & (gradient > 0)] = 0
            magnitude = float(np.linalg.norm(gradient))
            if magnitude <= np.finfo(float).eps or step < request.local_step_tolerance:
                break
            limits = np.array([phase_limits[(r["material_id"], r["optical_set_id"])] for r in rows])
            delta = -step * scales * gradient / magnitude
            ratio = float(np.max(np.abs(delta) / limits))
            if ratio > 1:
                delta /= ratio
            next_ds = project(ds + delta)
            if np.any(np.abs(next_ds - ds) > limits * (1 + 1e-12)):
                step *= 0.5
                continue
            if np.max(np.abs(next_ds - ds) / scales) < request.local_step_tolerance:
                break
            trial = deepcopy(design)
            for row, thickness in zip(trial["front_layers"], next_ds, strict=True):
                row["thickness"] = {"value": float(thickness), "unit": "nm"}
            trial = clean(trial)
            if not feasible(trial):
                step *= 0.5
                continue
            candidate = evaluate(trial, jacobian=True)
            archive(candidate)
            improvement = value["smooth_loss"] - candidate["smooth_loss"]
            if rank(candidate) < rank(value) and (
                    improvement > request.relative_improvement_tolerance * max(value["smooth_loss"], 1e-30)
                    or value["score"]["max_error"] - candidate["score"]["max_error"] >
                    request.relative_improvement_tolerance * max(value["score"]["max_error"], 1e-30)):
                design, value = trial, candidate
                emit("refinement", "解析梯度局部精修", design, value["score"])
                step = min(0.5, step * 1.5)
                if not design["front_layers"]:
                    break
            else:
                step *= 0.5
        # Explicit deletion trials can remove nonzero layers without a hidden
        # manufacturing threshold. Every deletion receives a full-grid score.
        for layer in tuple(design["front_layers"]):
            if layer["id"] == protected_id:
                continue
            trial = deepcopy(design)
            trial["front_layers"] = [r for r in trial["front_layers"] if r["id"] != layer["id"]]
            candidate = evaluate(trial)
            if rank(candidate) <= rank(value):
                design, value = trial, candidate
                archive(value)
                emit("deletion", "完整评分后删除冗余层", design, value["score"])
        return value

    def replace_materials(value):
        """Try physical thickness and reference optical-thickness preservation.

        These two deterministic proposals are a project search convention,
        not exhaustive optimization of every replacement basin.
        """
        current = value
        for layer in tuple(value["design"]["front_layers"]):
            for ref in request.candidate_materials:
                old_pair = (layer["material_id"], layer["optical_set_id"])
                new_pair = (ref.material_id, ref.optical_set_id)
                if old_pair == new_pair:
                    continue
                original = next((r for r in current["design"]["front_layers"] if r["id"] == layer["id"]), None)
                if original is None:
                    break
                old_pair = (original["material_id"], original["optical_set_id"])
                thickness = original["thickness"]["value"]
                proposals = dict.fromkeys((thickness, thickness * quarter[new_pair] / quarter[old_pair]))
                for proposed in proposals:
                    trial = deepcopy(current["design"])
                    row = next((r for r in trial["front_layers"] if r["id"] == layer["id"]), None)
                    if row is None:
                        break
                    row["material_id"], row["optical_set_id"] = new_pair
                    row["thickness"] = {"value": float(proposed), "unit": "nm"}
                    if not feasible(trial):
                        continue
                    raw = evaluate(trial, jacobian=True)
                    emit("material_replacement", "Rescored physical/optical-thickness material replacement", trial, raw["score"], material_id=ref.material_id)
                    archive(raw)
                    if rank(raw) < rank(current):
                        candidate = polish(raw)
                        if rank(candidate) < rank(current):
                            current = candidate
        return current

    def legal_growth_thickness(design, proposed):
        upper = math.inf
        if request.max_layer_thickness_nm is not None:
            upper = min(upper, request.max_layer_thickness_nm)
        if request.max_total_thickness_nm is not None:
            upper = min(upper, request.max_total_thickness_nm - sum(r["thickness"]["value"] for r in design["front_layers"]))
        lower = request.minimum_nonzero_thickness_nm or 0.0
        thickness = min(max(lower, proposed), upper)
        return thickness if math.isfinite(thickness) and thickness > 0 and thickness >= lower else None

    try:
        try:
            emit("freezing", "冻结目标、基片和显式膜材")
            request = freeze_film_synthesis_request(service, request, cancel_event=cancel_event)
            snapshot = request.frozen_material_snapshot
            execution.configure(_proxy(request))
            if snapshot is None:
                raise ValueError("material freeze did not produce a snapshot")
            materials = {r["id"]: r for r in snapshot["materials"]}
            quarter = {}
            references = list(request.candidate_materials)
            pairs = {(r.material_id, r.optical_set_id) for r in references}
            for seed_design in request.seed_designs:
                for row in seed_design["front_layers"]:
                    pair = (row["material_id"], row["optical_set_id"])
                    if pair not in pairs:
                        references.append(FilmNeedleMaterialReference(*pair))
                        pairs.add(pair)
            for ref in references:
                optical = next(o for o in materials[ref.material_id]["optical_sets"] if o["id"] == ref.optical_set_id)
                n, _, _ = prepare_optical_set(optical)(_reference_wave(request), "nm", extrapolation_policy="error")
                quarter[(ref.material_id, ref.optical_set_id)] = _reference_wave(request) / (4 * n)
            phase_limits = _phase_step_limits_nm(request, cancel_event=cancel_event)
            if request.forward_backend != "cpu":
                from .film_synthesis_forward import SynthesisForward
                forward = SynthesisForward(request, _proxy(request), cancel_event)
            working = evaluate(request.environment, gpu_forward=forward is not None)
            archive(working)
            assert best is not None  # The scored bare substrate is always feasible.
            branches = [working]
            blocked_branches = set()
            if request.seed_designs:
                audit_archive(working)
            for seed_design in request.seed_designs:
                seed_value = evaluate(seed_design, jacobian=True)
                emit("seed_design", "Scored arbitrary real layer-sequence starting design", seed_design, seed_value["score"])
                candidate = polish(replace_materials(seed_value))
                audit_archive(candidate)
                branches.append(candidate)
            stagnant = 0
            while not targets_verified():
                _check_cancel(cancel_event)
                if request.max_cycles is not None and cycle >= request.max_cycles:
                    raise _Stopped("cycle_limit")
                branch_index = (cycle % len(branches))
                while branch_index in blocked_branches:
                    branch_index = (branch_index + 1) % len(branches)
                cycle += 1
                working = branches[branch_index]
                previous = best["score"]["weighted_rms_error"]
                if working["design"]["front_layers"]:
                    working = replace_materials(working)
                    if targets_verified():
                        break
                design = working["design"]
                emit("needle_scan", "扫描界面和层内位置的零厚度解析灵敏度", design, working["score"])
                probes = []
                sites = _site_records(design, snapshot, request.position_samples)
                maximum_layers = request.max_layers or MAX_LAYERS
                needle_count = sum(len(request.candidate_materials) for site in sites
                    if len(design["front_layers"]) + (1 if site["kind"] == "interface" else 2) <= maximum_layers)
                sample_points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
                def needle_tasks(sites=sites, design=design, cycle=cycle, maximum_layers=maximum_layers):
                    for index, site in enumerate(sites):
                        for material_index, ref in enumerate(request.candidate_materials):
                            ids = _proposal_layer_ids(design,
                                f"auto-{cycle}-{index}-{material_index}", f"split-{cycle}-{index}-{material_index}",
                                reserved_ids=reserved_layer_ids)
                            zero = _insert_at_site(design, site, ref, 0, *ids, zero=True)
                            if len(zero["front_layers"]) > maximum_layers:
                                continue
                            yield EvaluationTask(zero, True, (ids[0],), (index, material_index, site, ref, ids))
                needle_fits = request.max_evaluations is None or needle_count <= request.max_evaluations - evaluations - 1
                with execution.batch(needle_tasks(), count=needle_count,
                        work=needle_count * sample_points * max(1, len(design["front_layers"]) + 2),
                        budget_allows=needle_fits, phase="needle") as batch:
                    for task, outcome in batch:
                        index, material_index, site, ref, ids = task.context
                        probe = evaluate(task.design, jacobian=True, zero_ids=task.zero_ids, outcome=outcome)
                        sensitivity = probe["gradient_by_layer_id"][ids[0]]
                        emit("needle_probe", "P=d(加权残差平方和)/dd，d=0", design,
                             material_id=ref.material_id, position_nm=site["position_nm"], sensitivity=sensitivity)
                        if sensitivity < -np.finfo(float).eps:
                            probes.append((sensitivity, index, material_index, site, ref, ids))
                for _sensitivity, _, _, site, ref, ids in sorted(probes, key=lambda row: row[:3]):
                    seed = legal_growth_thickness(design,
                        quarter[(ref.material_id, ref.optical_set_id)] * request.needle_seed_fraction)
                    if seed is None:
                        continue
                    trial = _insert_at_site(design, site, ref, seed, *ids)
                    if not feasible(trial):
                        continue
                    candidate = polish(evaluate(trial, jacobian=True))
                    if rank(candidate) < rank(working):
                        working = candidate
                        archive(working)
                        emit("needle_accepted", "负灵敏度针插后精修改善目标", working["design"], working["score"])
                        break
                if not targets_verified():
                    # A small successful needle does not suppress finite-layer
                    # exploration. Both operators run on the cycle's input branch.
                    # Gradual evolution changes the working branch even when the
                    # thick layer initially worsens merit; the best archive stays.
                    grown = None
                    grown_protected_id = None
                    growth_specs = []
                    growth_count = 0
                    for interface in range(len(design["front_layers"]) + 1):
                        for material_index, ref in enumerate(request.candidate_materials):
                            thicknesses = []
                            for fraction in request.growth_quarter_wave_fractions:
                                thickness = legal_growth_thickness(design,
                                    quarter[(ref.material_id, ref.optical_set_id)] * fraction)
                                if thickness is not None and thickness not in thicknesses:
                                    thicknesses.append(thickness)
                            growth_specs.append((interface, material_index, ref, thicknesses))
                            if len(design["front_layers"]) < maximum_layers:
                                growth_count += len(thicknesses)
                    def growth_tasks(growth_specs=growth_specs, design=design, cycle=cycle):
                        for interface, material_index, ref, thicknesses in growth_specs:
                            growth_id, split_id = _proposal_layer_ids(design,
                                f"growth-{cycle}-{interface}-{material_index}", "unused",
                                reserved_ids=reserved_layer_ids)
                            for thickness in thicknesses:
                                trial = _insert_at_site(design, {"kind": "interface", "interface": interface}, ref,
                                                        thickness, growth_id, split_id)
                                trial["front_layers"][interface]["note"] = "Automatic gradual-evolution thick-layer proposal."
                                if feasible(trial):
                                    yield EvaluationTask(trial, True, (), (interface, ref, growth_id))
                    growth_batch = (forward.batch(growth_tasks(), admit=lambda: request.max_evaluations is None
                        or evaluations < request.max_evaluations - 1) if forward is not None else
                        execution.batch(growth_tasks(), count=growth_count,
                            work=growth_count * sample_points * max(1, len(design["front_layers"]) + 1),
                            budget_allows=request.max_evaluations is None, phase="growth"))
                    with growth_batch as batch:
                        for task, outcome in batch:
                            interface, ref, growth_id = task.context
                            trial = task.design
                            raw = evaluate(trial, jacobian=True, outcome=outcome)
                            if not design["front_layers"]:
                                emit("zero_layer_trial", "Finite legal positive-thickness trial despite zero bare-interface power derivative", trial, raw["score"], material_id=ref.material_id)
                            emit("growth_trial", "增加厚层的完整评分（允许暂时变差）", trial, raw["score"], material_id=ref.material_id)
                            archive(raw)
                            if grown is None or rank(raw) < rank(grown):
                                grown = raw
                                grown_protected_id = growth_id
                            if targets_verified():
                                break
                    if grown is None:
                        branches[branch_index] = working
                        blocked_branches.add(branch_index)
                        audit_archive(best)
                        if len(blocked_branches) == len(branches):
                            raise _Stopped("no_feasible_growth")
                        emit("branch_exhausted", "No feasible insertion remains in this branch; continue another explicit starting branch", working["design"], working["score"])
                        continue
                    working = polish(grown, protected_id=grown_protected_id)
                    emit("growth_restart", "增加光学厚度后重新开始针插周期（工作分支可暂时变差）", working["design"], working["score"])
                branches[branch_index] = working
                improved = previous - best["score"]["weighted_rms_error"]
                stagnant = stagnant + 1 if improved <= request.relative_improvement_tolerance * max(previous, 1e-30) else 0
                audit_archive(best)
                if stagnant >= request.stagnation_cycles:
                    # Stagnation of this branch is not a proof of infeasibility.
                    # Keep growing; only an explicit user/resource boundary stops.
                    emit("search_plateau", "No recent archive improvement; continue feasible structural exploration", working["design"], working["score"])
                    stagnant = 0
            reason = "training_targets_met"
        except _Stopped as exc:
            reason = str(exc)
        except _StructureCancelled:
            state, reason = "cancelled", "cancelled"
        except (ValueError, ArithmeticError) as exc:
            state, reason = "failed", f"numerical_failure: {exc}"

        if best is not None and state != "cancelled":
            try:
                audit_archive(best)
                if audited_best is not None:
                    validated_training, validation = audited_best
                    if state != "failed" and validated_training["score"]["max_error"] <= request.acceptable_normalized_error:
                        if validation["score"]["max_error"] <= request.acceptable_normalized_error:
                            state, reason = "completed", "validated_targets_met"
                        else:
                            reason = "validation_targets_not_met"
            except _StructureCancelled:
                state, reason = "cancelled", "cancelled_during_validation"
            except (ValueError, ArithmeticError, _Stopped) as exc:
                state, reason = "failed", f"validation_failed: {exc}"
        if state == "cancelled":
            # Freeze only values already committed above. Pool shutdown may
            # collect work counters, but cannot promote any late candidate.
            best, validation = completed_training_best, completed_training_validation
            candidate_provenance = {
                "selection": "completed_training_at_cancel" if best is not None else "none",
                "training_complete": best is not None, "frozen_at_cancel": True,
                "fully_scored_evaluations": fully_scored_evaluations,
                "validation_matches_candidate": validation is not None}
        elif audited_best is not None:
            best, validation = audited_best
        execution.close()
        if forward is not None:
            forward.close()
        met = state == "completed"
        message = ("找到通过训练网格与独立密网格验证的候选；尚未应用到用户设计。" if met else
                   "已取消；保留此前完整评分的候选，未确认达标。" if state == "cancelled" else
                   "尚未找到通过全部验证的膜系；候选仅供审查。")
        emit(state, message, None if best is None else best["design"], None if best is None else best["score"])
        return FilmSynthesisResult(state, reason, None if best is None else annotate_synthesis_design(request, best["design"]),
            request.design_revision, met, None if best is None else deepcopy(best["score"]),
            None if validation is None else deepcopy(validation["score"]), validation is not None,
            cycle, evaluations, validation_evaluations, failures, tuple(trace), message, request,
            audit_physical_evaluations, audit_wavelength_points, performance(), candidate_provenance)
    finally:
        execution.close()
        if forward is not None:
            forward.close()
