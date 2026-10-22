"""GUI-independent synthesis from a bare substrate using analytic needles.

The working growth branch may get worse; the separately scored incumbent never
does. No random layer generation, application to a session, or implicit work
budget is involved. See docs/film-auto-synthesis-contract.md and algorithms.html.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Generator, Mapping, NotRequired, TypedDict

import numpy as np

from .film_design import MAX_LAYERS, physical_thickness_nm, validate_design
from .film_optimization import FilmConditionError, FilmOptimizationObjective, FilmOptimizationRequest, freeze_film_optimization_request
from .film_optimization_needle import FilmNeedleMaterialReference, _candidate_payload_from_materials, _normalize_objectives
from .film_optimization_structure import (
    FilmStructureOptimizationRequest, _StructureCancelled, _insert_at_site,
    _site_records, _PreparedStructureEvaluator, evaluate_frozen_structure_candidate,
)
from .optical_constants import UNITS_TO_UM, prepare_optical_set
from .film_synthesis_absorption import (annotate_synthesis_design, assumes_unknown_k_zero,
                                        sample_synthesis_optical)


if TYPE_CHECKING:
    from .film_synthesis_global import StructuralProposal


# Scheduling work proxy, not a request/physics budget or a feasibility bound.
STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM = 1_000_000


class _SearchDiagnostics(TypedDict):
    structural_proposals: list[dict[str, object]]
    global_search: list[dict[str, object]]
    layer_distribution: dict[str, int]
    local_refinement: list[dict[str, object]]
    ar_structure_mode: NotRequired[str]
    automatic_cluster_route: NotRequired[bool]
    ar_simple_start: NotRequired[dict[str, object]]
    hr_minimax_start: NotRequired[dict[str, object]]
    multispectral_start: NotRequired[dict[str, object]]
    structure_batch_policy: NotRequired[dict[str, object]]
    multispectral_structure_schedule: NotRequired[dict[str, object]]
    bandpass_structure_schedule: NotRequired[dict[str, object]]
    matching_structure_schedule: NotRequired[dict[str, object]]
    cluster_structure_schedule: NotRequired[dict[str, object]]


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
    structure_search_seed: int = 0
    strict_lower_bound_objective_indices: tuple[int, ...] = ()
    ar_structure_mode: str = "clustered"
    max_seconds: float | None = None


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
    weighted_rms_error: float | None = None
    layer_penalty: float | None = None
    candidate_rank: tuple = ()


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


def strict_lower_bounds_pass(request, score):
    """Explicit strict lower checks for in-memory or JSON condition scores."""
    indices = set(request.strict_lower_bound_objective_indices)
    if not indices:
        return True
    def field(row, key):
        return row[key] if isinstance(row, Mapping) else getattr(row, key)
    errors = [row for row in score.get("errors", ()) if field(row, "objective_index") in indices]
    return (indices <= {field(row, "objective_index") for row in errors}
            and all(field(row, "actual") > request.objectives[field(row, "objective_index")].lower_bound for row in errors))


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
    if type(request.ar_structure_mode) is not str or request.ar_structure_mode not in {"clustered", "v23_matching"}:
        raise ValueError("ar_structure_mode must be clustered or v23_matching")
    if type(request.structure_search_seed) is not int or request.structure_search_seed < 0:
        raise ValueError("structure_search_seed must be a nonnegative integer")
    if type(request.cpu_workers) is not int or request.cpu_workers < 0:
        raise ValueError("cpu_workers must be a nonnegative integer (0=auto, 1=serial)")
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if not isinstance(request.objectives, (tuple, list)):
        raise ValueError("objectives must be a sequence")
    objectives = _normalize_objectives(request.objectives)
    if not objectives:
        raise ValueError("at least one objective is required")
    strict_indices = request.strict_lower_bound_objective_indices
    if not isinstance(strict_indices, (tuple, list)):
        raise ValueError("strict_lower_bound_objective_indices must be a sequence")
    if any(type(index) is not int or index < 0 or index >= len(objectives) for index in strict_indices):
        raise ValueError("strict_lower_bound_objective_indices must contain valid nonboolean objective indices")
    if len(set(strict_indices)) != len(strict_indices):
        raise ValueError("strict_lower_bound_objective_indices must be unique")
    if any(objectives[index].mode != "range" or objectives[index].lower_bound is None for index in strict_indices):
        raise ValueError("strict lower bounds require range objectives with an explicit lower_bound")
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
    for name in ("minimum_nonzero_thickness_nm", "max_layer_thickness_nm", "max_total_thickness_nm",
                 "max_seconds"):
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
                   strict_lower_bound_objective_indices=tuple(strict_indices),
                   candidate_materials=tuple(request.candidate_materials),
                   growth_quarter_wave_fractions=tuple(float(v) for v in request.growth_quarter_wave_fractions),
                   **numeric)


def _copy_internal_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy mutable candidate data, sharing only checked frozen scalar errors.

    This is for parent-owned internal archives only. Observer/checkpoint/result
    boundaries keep their existing independent copies. A malformed/custom error
    or a mutable field falls back to ordinary deepcopy semantics.
    """
    memo: dict[int, Any] = {}
    scalar_types = (bool, int, float, str, type(None))
    for score in (value, value.get("score")):
        if not isinstance(score, Mapping):
            continue
        errors = score.get("errors")
        if type(errors) is tuple and all(
                type(error) is FilmConditionError
                and all(type(field) in scalar_types for field in vars(error).values())
                for error in errors):
            memo[id(errors)] = errors
    return deepcopy(dict(value), memo)


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


class _ObserverFailure(Exception):
    """Keep caller callback failures distinct from physical runtime failures."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


class _Stopped(Exception):
    pass


class _SynthesisStopEvent:
    """Share one cooperative wall/caller stop signal with all owned work."""

    def __init__(self, caller_event: Any, deadline: float | None):
        self.caller_event = caller_event
        self.deadline = deadline

    def caller_cancelled(self) -> bool:
        return self.caller_event is not None and bool(self.caller_event.is_set())

    def time_limit_reached(self) -> bool:
        return self.deadline is not None and monotonic() >= self.deadline

    def is_set(self) -> bool:
        return self.caller_cancelled() or self.time_limit_reached()


def synthesize_film_design(service: Any, request: FilmSynthesisRequest, *, cancel_event: Any = None,
                           progress_callback: Callable[[FilmSynthesisProgress], None] | None = None,
                           snapshot_callback: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], None] | None = None) -> FilmSynthesisResult:
    """Return a reviewable candidate; completed means training and audit passed.

    Invalid requests raise ValueError. Numerical failures, cancellation and
    exhausted user limits are explicit result states. Callbacks run on the
    caller's thread. Observer exceptions propagate unchanged after owned-resource
    cleanup, including snapshot/checkpoint write failures; they are not physical
    failures and the search does not continue. Cancellation is cooperative
    between physical helper calls.
    snapshot_callback receives copied (design, training, validation, audit)
    only when a new best independently audited candidate is retained.
    Cancellation returns the best complete feasible training score committed
    by the parent before it observes cancellation. An existing complete audit
    is attached only when it belongs to that exact candidate; no audit starts
    to make the cancellation candidate available.
    """
    started = monotonic()
    request = _validated(request)
    stop_event = _SynthesisStopEvent(cancel_event,
        None if request.max_seconds is None else started + request.max_seconds)
    cancel_event = stop_event
    from .film_synthesis_global import reflector_objectives
    automatic_reflector_route = not request.seed_designs and bool(reflector_objectives(request))
    from .film_synthesis_matching import matching_objectives
    automatic_matching_route = (not request.seed_designs and len(request.candidate_materials) >= 2
        and (request.max_layers is None or request.max_layers >= 3)
        and bool(matching_objectives(request)))
    automatic_cluster_route = (automatic_matching_route and not automatic_reflector_route
        and request.ar_structure_mode == "clustered")
    from .film_synthesis_parallel import EvaluationTask, InfrastructureFailure, OrderedEvaluationExecutor
    execution = OrderedEvaluationExecutor(request.cpu_workers, cancel_event)
    from .film_synthesis_feedback import (
        SparseFeedbackRegistry, augment_feedback_guide, restore_feedback_base,
        feedback_candidate_key,
    )
    feedback_registry = SparseFeedbackRegistry(request)
    feedback_work = dict(parent_evaluator_invocations=0, physical_evaluations=0,
                         wavelength_points=0, repair_stages=0, local_stages=0)
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
    search_diagnostics: _SearchDiagnostics = {"structural_proposals": [], "global_search": [],
        "layer_distribution": {}, "local_refinement": []}
    audited_signatures = set()
    pending_structural_scans: dict[int, Generator[StructuralProposal, None, None]] = {}
    pending_structural_proposals: dict[int, StructuralProposal] = {}
    cavity_stream: list[Generator[StructuralProposal, None, None] | None] = [None]
    cavity_stream_epoch = [None]
    matching_stream: list[Generator[StructuralProposal, None, None] | None] = [None]
    matching_stream_epoch = [None]
    cluster_stream: list[Generator[StructuralProposal, None, None] | None] = [None]
    cluster_stream_epoch = [None]
    matching_deferred = [automatic_matching_route]
    search_diagnostics["ar_structure_mode"] = request.ar_structure_mode
    search_diagnostics["automatic_cluster_route"] = automatic_cluster_route
    validation = None
    audit_physical_evaluations = audit_wavelength_points = 0
    trace = []
    stage_times: dict[str, float] = {}
    last_stage_time = __import__("time").perf_counter()
    last_stage = "initialization"
    reason, state = "search_exhausted", "not_found"

    def performance():
        result = execution.diagnostics()
        result["stage_wall_seconds"] = dict(stage_times)
        result["search"] = deepcopy(search_diagnostics)
        feedback_snapshot = feedback_registry.snapshot()
        result["audit_feedback"] = dict(revision=feedback_snapshot.revision,
            points=[asdict(point) for point in feedback_snapshot.points],
            **feedback_work,
            scope="fixed-snapshot local repair only; original training/audit/global population scores unchanged")
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

    def observe(callback, *args):
        try:
            callback(*args)
        except Exception as exc:
            raise _ObserverFailure(exc) from exc

    def emit(stage, message, design=None, score=None, **kwargs):
        nonlocal last_stage_time, last_stage
        now = __import__("time").perf_counter()
        stage_times[last_stage] = stage_times.get(last_stage, 0.0) + now - last_stage_time
        last_stage_time, last_stage = now, stage
        layers = len(design["front_layers"]) if design is not None else 0
        error = score["max_error"] if score is not None else None
        trace.append(FilmSynthesisTrace(stage, cycle, evaluations, layers, error, weighted_rms_error=None if score is None else score["weighted_rms_error"],
            layer_penalty=None, candidate_rank=() if design is None or score is None else rank({"design": design, "score": score}),
            message=message, **kwargs))
        if progress_callback:
            observe(progress_callback, FilmSynthesisProgress(stage, cycle, evaluations, layers,
                (audited_best[1]["score"]["max_error"] if audited_best is not None else
                 None if best is None else best["score"]["max_error"]), message))

    def feasible(design):
        return _feasible(request, design)

    def check_training_admission():
        # Apply the same remaining logical budget before lazy preparation as
        # before physical scoring; keep one existing slot for independent audit.
        _check_cancel(cancel_event)
        if request.max_evaluations is not None and evaluations >= request.max_evaluations - 1:
            raise _Stopped("evaluation_limit")

    prepared_training: list[_PreparedStructureEvaluator | None] = [None]

    def evaluate(design, *, jacobian=False, zero_ids=(), holdout=False, outcome=None, gpu_forward=False):
        nonlocal evaluations, validation_evaluations, failures
        nonlocal audit_physical_evaluations, audit_wavelength_points
        nonlocal completed_training_best, completed_training_validation, fully_scored_evaluations
        if holdout:
            _check_cancel(cancel_event)
            if request.max_evaluations is not None and evaluations >= request.max_evaluations:
                raise _Stopped("evaluation_limit")
        else:
            check_training_admission()
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
                    prepared = prepared_training[0]
                    if prepared is None:
                        prepared = _PreparedStructureEvaluator(_proxy(request))
                        prepared_training[0] = prepared
                    return prepared.evaluate(design, evaluator=evaluate_frozen_structure_candidate,
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
            value["score"]["key"] = rank(value)
            _check_cancel(cancel_event)
            # This parent-owned commit boundary excludes zero probes, partial
            # scores and unconsumed speculative worker results. It performs no
            # extra physical work and does not change the search archive.
            if not holdout and not zero_ids and feasible(value["design"]):
                fully_scored_evaluations += 1
                distribution = search_diagnostics["layer_distribution"]
                layer_key = str(len(value["design"]["front_layers"]))
                distribution[layer_key] = distribution.get(layer_key, 0) + 1
                if (completed_training_best is None or
                        rank(value) < rank(completed_training_best)):
                    completed_training_best = {"design": deepcopy(value["design"]),
                                               "score": _copy_internal_candidate(value["score"])}
                    completed_training_validation = None
            return value
        except _StructureCancelled:
            raise
        except (ValueError, ArithmeticError, RuntimeError):
            failures += 1
            raise

    def strict_lower_pass(score):
        return strict_lower_bounds_pass(request, score)

    def rank(candidate):
        legacy = _synthesis_candidate_rank(candidate, request.acceptable_normalized_error)
        strict_indices = set(request.strict_lower_bound_objective_indices)
        if not (automatic_reflector_route or automatic_matching_route) and not strict_indices:
            return legacy
        score = candidate["score"]
        # Raw optical merit guides automatic structural search. Only explicitly
        # selected strict lower bounds change feasibility; ordinary saved range
        # objectives keep their inclusive/tolerance acceptance semantics.
        slack_indices = strict_indices
        if not slack_indices and automatic_reflector_route:
            slack_indices = {i for i, objective in enumerate(request.objectives)
                             if objective.metric == "R" and objective.mode == "range"
                             and objective.lower_bound is not None}
        slacks = [error.actual - request.objectives[error.objective_index].lower_bound
                  for error in score.get("errors", ()) if error.objective_index in slack_indices]
        slack = min(slacks, default=0.0) if score["max_error"] == 0 else 0.0
        optical = (score["max_error"], -slack, *legacy)
        if strict_indices:
            return (0 if strict_lower_pass(score) and score["max_error"] <= request.acceptable_normalized_error else 1, *optical)
        return optical

    def audit_archive(value, *, allow_feedback_repair=True):
        nonlocal audited_best, completed_training_validation
        signature = hashlib.sha256(json.dumps(value["design"], sort_keys=True, allow_nan=False).encode("utf-8")).digest()
        _check_cancel(cancel_event)
        if signature in audited_signatures:
            return
        emit("validation", "Audit dense, offset and local-extremum physical responses", value["design"], value["score"])
        audited = evaluate(value["design"], holdout=True)
        audited_signatures.add(signature)
        if (completed_training_best is not None and
                completed_training_best["design"] == value["design"]):
            completed_training_validation = {"design": deepcopy(audited["design"]),
                                             "score": _copy_internal_candidate(audited["score"])}
        if audited_best is None or rank(audited) < rank(audited_best[1]):
            audited_best = (_copy_internal_candidate(value), _copy_internal_candidate(audited))
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
                observe(snapshot_callback, annotate_synthesis_design(request, value["design"]), deepcopy(value["score"]),
                                  deepcopy(audited["score"]), checkpoint_audit)
        emit("validation_complete", "Independent physical candidate audit completed", value["design"], audited["score"])
        _check_cancel(cancel_event)
        added = feedback_registry.record_audit(audited["score"])
        if added:
            snapshot = feedback_registry.snapshot()
            emit("audit_feedback_added", json.dumps(dict(revision=snapshot.revision,
                added=added, points=[asdict(point) for point in snapshot.points]),
                sort_keys=True), value["design"], audited["score"])
        if (allow_feedback_repair and feedback_registry.snapshot().points
                and value["design"]["front_layers"]
                and not targets_verified()):
            check_training_admission()
            feedback_work["repair_stages"] += 1
            emit("audit_feedback_repair", "One fixed-snapshot local repair followed by original independent audit", value["design"], audited["score"])
            repaired = interior_refine(value)
            # One repair invocation has a fixed condition snapshot. Its new
            # independent audit may register later points but never recurse.
            archive(repaired, audit_eligible=False)
            audit_archive(repaired, allow_feedback_repair=False)

    def targets_verified():
        return (audited_best is not None
                and audited_best[0]["score"]["max_error"] <= request.acceptable_normalized_error
                and audited_best[1]["score"]["max_error"] <= request.acceptable_normalized_error
                and strict_lower_pass(audited_best[0]["score"])
                and strict_lower_pass(audited_best[1]["score"]))

    def archive(value, *, audit_eligible=True):
        nonlocal best
        if feasible(value["design"]) and (best is None or rank(value) < rank(best)):
            best = _copy_internal_candidate(value)
            emit("incumbent", "已保存独立评分的候选，等待最终密网格验证", value["design"], value["score"])
        # A training-feasible candidate may be better on audit even when it
        # loses the training cost tie-break. Audit it before discarding it.
        if (audit_eligible and feasible(value["design"])
                and value["score"]["max_error"] <= request.acceptable_normalized_error
                and strict_lower_pass(value["score"])):
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

    def polish(value, protected_id=None, *, max_steps=None):
        """Projected analytic gradient with backtracking to numerical stationarity."""
        if forward is not None and value["design"]["front_layers"] and value.get("gradient_by_layer_id") is None:
            # The same already scored candidate gets analytic CPU derivatives;
            # it is not a new logical candidate or a finite-difference step.
            check_training_admission()
            task = EvaluationTask(value["design"], True)
            forward.refresh_attempts += 1
            value = execution.serial(lambda: evaluate_frozen_structure_candidate(_proxy(request), task.design,
                variable_ids=tuple(r["id"] for r in task.design["front_layers"]),
                with_jacobian=True, cancel_event=cancel_event), task)
            forward.record_refresh(value)
            value["score"]["key"] = rank(value)
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
        local_trials = 0
        while True:
            check_training_admission()
            if max_steps is not None and local_trials >= max_steps:
                emit("local_quantum_complete", f"Completed {local_trials} local trial scheduling quantum; continue global/structural search", value["design"], value["score"])
                return value
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
            local_trials += 1
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
        if max_steps is not None:
            return value
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

    def interior_refine(value):
        check_training_admission()
        from .film_synthesis_refinement import refine_synthesis_candidate
        snapshot = feedback_registry.snapshot()
        def local_progress(stage, candidate, details):
            candidate = restore_feedback_base(candidate)
            emit(stage, json.dumps(details, sort_keys=True), candidate["design"], candidate["score"])
        def point_evaluation(design, objectives):
            # Sparse guide points belong to an already admitted candidate.
            # They add physical work, not another logical candidate, and still
            # observe the same elapsed-time/cancel boundary on every call.
            _check_cancel(cancel_event)
            proxy = _proxy(request)
            proxy = replace(proxy, base_request=replace(proxy.base_request, objectives=objectives))
            task = EvaluationTask(design, True)
            extra = execution.serial(lambda: evaluate_frozen_structure_candidate(
                proxy, design, variable_ids=tuple(r["id"] for r in design["front_layers"]),
                with_jacobian=True, derived_validation_grid=False,
                cancel_event=cancel_event), task)
            # Completed physical work remains counted even if cancellation is
            # observed before publishing the assembled guide.
            feedback_work["parent_evaluator_invocations"] += 1
            feedback_work["physical_evaluations"] += len(extra["responses"])
            feedback_work["wavelength_points"] += sum(len(r["wavelength_nm"]) for r in extra["responses"])
            _check_cancel(cancel_event)
            return extra
        def guide(value):
            return augment_feedback_guide(request, value, snapshot,
                evaluate_points=lambda objectives: point_evaluation(value["design"], objectives))
        def guided_evaluate(design, **kwargs):
            return guide(evaluate(design, **kwargs))
        def guided_archive(candidate, **kwargs):
            archive(restore_feedback_base(candidate), **kwargs)
        if snapshot.points:
            feedback_work["local_stages"] += 1
            # Workers may omit response matrices; refresh through the normal
            # budget boundary rather than compare a partial initial guide loss.
            if not value.get("responses"):
                value = evaluate(value["design"], jacobian=True)
            value = guide(value)
        refined, details = refine_synthesis_candidate(request, value,
            evaluate=guided_evaluate if snapshot.points else evaluate,
            archive=guided_archive if snapshot.points else archive, quarter_nm=quarter,
            cancel_check=check_training_admission, progress=local_progress,
            max_iterations=64,
            candidate_key=feedback_candidate_key if snapshot.points else None)
        details.update(feedback_revision=snapshot.revision,
                       feedback_points=len(snapshot.points), cache_scope="one invocation with immutable feedback snapshot")
        refined = restore_feedback_base(refined)
        search_diagnostics["local_refinement"].append(details)
        emit("interior_local_complete", json.dumps(details, sort_keys=True), refined["design"], refined["score"])
        return refined

    def replace_materials(value):
        """Try physical thickness and reference optical-thickness preservation.

        These two deterministic proposals are a project search convention,
        not exhaustive optimization of every replacement basin.
        """
        current = value
        for layer in tuple(value["design"]["front_layers"]):
            for ref in request.candidate_materials:
                check_training_admission()
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
            _check_cancel(cancel_event)
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
            if automatic_cluster_route and not targets_verified():
                # Try one physically legal homogeneous layer per allowed material
                # before preparing the much larger multicluster prior sweep.
                # This is an additive scheduling prefix, not a V-band special
                # case or a replacement for the persistent complex families.
                simple_start: dict[str, object] = dict(
                    attempted=0, feasible=0, audited_pass=False,
                    policy="one legal reference-quarter-wave layer per allowed material; full shared objective and audit; then unchanged complex search")
                search_diagnostics["ar_simple_start"] = simple_start
                feasible_simple_count = 0
                for material_index, ref in enumerate(request.candidate_materials):
                    check_training_admission()
                    simple_start["attempted"] = material_index + 1
                    thickness = legal_growth_thickness(request.environment,
                        quarter[(ref.material_id, ref.optical_set_id)])
                    if thickness is None:
                        continue
                    layer_id, split_id = _proposal_layer_ids(request.environment,
                        f"ar-simple-{material_index}", "ar-simple-unused",
                        reserved_ids=reserved_layer_ids)
                    trial = _insert_at_site(request.environment,
                        {"kind": "interface", "interface": 0}, ref, thickness,
                        layer_id, split_id)
                    if not feasible(trial):
                        continue
                    feasible_simple_count += 1
                    simple_start["feasible"] = feasible_simple_count
                    raw = evaluate(trial, gpu_forward=forward is not None)
                    emit("ar_simple_start", "Scored legal homogeneous-layer prior before multicluster screening", trial, raw["score"], material_id=ref.material_id)
                    archive(raw)
                    if targets_verified():
                        simple_start["audited_pass"] = True
                        emit("ar_simple_targets_verified", "Simple prior passed the same independent physical audit; complex search remains available for unmet targets", trial, raw["score"])
                        break
            # archive(working) already fully audits a training-qualified bare
            # substrate. An obvious training failure remains explicitly unaudited
            # until it is selected for a final audit; it must not consume search
            # time with dense certification before any layer proposal is tried.
            from .film_synthesis_multispectral_priors import (
                multispectral_prior_eligible, iter_multispectral_hr_proposals,
            )
            multispectral_route = multispectral_prior_eligible(request)
            separated_hr_only = multispectral_route and all(
                o.metric == "R" and o.mode == "range"
                and o.lower_bound is not None and o.lower_bound >= .5
                and (o.upper_bound is None or o.upper_bound >= 1.)
                for o in request.objectives)

            def multispectral_prefix(existing=()):
                # This finite scheduling prefix shares the parent physical work,
                # original score, strict acceptance, archive and independent audit.
                # Mixed targets retain the existing fast structural screen first.
                from .film_synthesis_multispectral_minimax import refine_multispectral_minimax
                runs: list[dict[str, object]] = []
                start: dict[str, object] = dict(
                    runs=runs, screened=0, audited_pass=False,
                    max_iterations_per_stage=48, betas=[100., 400., 1500.],
                    policy="lower demand tiers before explicit layer boundary; original coherent R/T scoring; window-normalized search guide only; unchanged fallback")
                search_diagnostics["multispectral_start"] = start
                initial_work = evaluations
                signatures = set()
                sample_points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
                batches: list[dict[str, object]] = []
                start["screening_batches"] = batches
                def multi_report(event):
                    search_diagnostics["structural_proposals"].append(deepcopy(event))
                    emit(event["stage"], json.dumps(event, sort_keys=True))
                def refine_selected(selected_multi):
                    for raw, metadata in selected_multi:
                        check_training_admission()
                        if targets_verified():
                            break
                        entry: dict[str, object] = dict(metadata,
                            layer_count=len(raw["design"]["front_layers"]),
                            total_thickness_nm=sum(r["thickness"]["value"] for r in raw["design"]["front_layers"]),
                            initial_max_error=raw["score"]["max_error"], state="refining")
                        runs.append(entry)
                        if (raw["score"]["max_error"] <= request.acceptable_normalized_error
                                and strict_lower_pass(raw["score"])):
                            entry["state"] = "auditing_prior"
                            audit_archive(raw, allow_feedback_repair=False)
                            if targets_verified():
                                entry["state"] = "completed"
                                break
                        entry["state"] = "refining"
                        local_best = raw
                        def multi_archive(value, *, audit_eligible=False):
                            nonlocal local_best
                            if rank(value) < rank(local_best):
                                local_best = value
                            archive(value, audit_eligible=audit_eligible)
                        def multi_progress(stage, value, details):
                            start["evaluations_used"] = evaluations - initial_work
                            emit(stage, json.dumps(details, sort_keys=True), value["design"], value["score"])
                        refined, details = refine_multispectral_minimax(request, raw,
                            evaluate=evaluate, archive=multi_archive,
                            cancel_check=check_training_admission, progress=multi_progress,
                            max_iterations=48, soft_method="L-BFGS-B", coordinate_scale_nm=100.)
                        entry["optimizer"] = details
                        entry["final_max_error"] = refined["score"]["max_error"]
                        entry["evaluations_used"] = evaluations - initial_work
                        archive(refined, audit_eligible=False)
                        # Keep both the helper's margin tie winner and the parent's
                        # strict-rank champion. A guide never grants acceptance.
                        for candidate in (refined, local_best):
                            if (candidate["score"]["max_error"] <= request.acceptable_normalized_error
                                    and strict_lower_pass(candidate["score"])):
                                audit_archive(candidate)
                            if targets_verified():
                                break
                        entry["state"] = "completed"
                control = None
                if existing:
                    original, original_metadata = min(existing, key=lambda row: rank(row[0]))
                    control = (original, dict(family=original_metadata["family"], demand_tier="existing"))
                    original_work = sample_points * max(1, len(original["design"]["front_layers"]))
                    start["source_first"] = original_work >= STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM
                source = iter_multispectral_hr_proposals(request,
                    cancel_check=check_training_admission, report=multi_report)
                source_exhausted = False
                pending_source: StructuralProposal | None = None
                try:
                    while not source_exhausted and not targets_verified():
                        keepers: dict[int, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
                        batch_work = batch_count = 0
                        while batch_work < STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM:
                            check_training_admission()
                            if pending_source is not None:
                                proposal, pending_source = pending_source, None
                            else:
                                try:
                                    proposal = next(source)
                                except StopIteration:
                                    source_exhausted = True
                                    break
                            signature = tuple((r["material_id"], r["optical_set_id"],
                                r["thickness"]["value"]) for r in proposal.design["front_layers"])
                            if signature in signatures:
                                continue
                            item_work = sample_points * max(1, len(proposal.design["front_layers"]))
                            if batch_count and batch_work + item_work > STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM:
                                pending_source = proposal
                                break
                            signatures.add(signature)
                            raw = evaluate(proposal.design)
                            archive(raw, audit_eligible=False)
                            batch_count += 1
                            batch_work += item_work
                            metadata = proposal.provenance
                            fraction = metadata["candidate_pair_budget"] / metadata["estimated_pair_demand"]
                            tier = 0 if fraction <= .625 else 1 if fraction <= .875 else 2 if fraction <= 1.125 else 3
                            if tier not in keepers or rank(raw) < rank(keepers[tier][0]):
                                keepers[tier] = raw, metadata
                            start["screened"] = len(signatures)
                            emit("multispectral_prior", json.dumps(dict(
                                tier=tier, layers=len(raw["design"]["front_layers"]),
                                pair_budget=metadata["candidate_pair_budget"],
                                estimated_pair_demand=metadata["estimated_pair_demand"]), sort_keys=True),
                                raw["design"], raw["score"])
                        batch_record: dict[str, object] = dict(
                            batch=len(batches), admitted_candidates=batch_count,
                            estimated_admitted_point_layer_work=batch_work,
                            point_layer_work_quantum=STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM,
                            source_exhausted=source_exhausted, pending_candidate=pending_source is not None)
                        batches.append(batch_record)
                        emit("multispectral_screen_complete" if source_exhausted else "multispectral_screen_yield",
                            json.dumps(batch_record, sort_keys=True))
                        selected_multi = [(value, dict(metadata, demand_tier=tier))
                            for tier, (value, metadata) in sorted(keepers.items())]
                        if control is not None:
                            if not start["source_first"]:
                                selected_multi.insert(0, control)
                                control = None
                            elif source_exhausted:
                                selected_multi.append(control)
                                control = None
                        refine_selected(selected_multi)
                finally:
                    close_source = getattr(source, "close", None)
                    if close_source is not None:
                        close_source()
                start["evaluations_used"] = evaluations - initial_work
                start["audited_pass"] = targets_verified()
                if targets_verified():
                    assert audited_best is not None
                    value = audited_best[0]
                    emit("multispectral_targets_verified", "Source-derived multiwindow topology and joint R/T refinement passed original independent audit", value["design"], value["score"])
                elif best is not None:
                    audit_archive(best)

            if separated_hr_only and not targets_verified():
                multispectral_prefix()
            # Add a source-derived pure-HR prefix before the much larger classic
            # structural sweep. All physical calls and final audits retain the
            # existing parent budget, provenance and cancellation boundaries.
            from .film_synthesis_hr_priors import (
                pure_hr_prior_eligible, iter_dual_substack_hr_proposals,
            )
            if not targets_verified() and pure_hr_prior_eligible(request):
                from .film_synthesis_hr_minimax import refine_hr_minimax
                hr_runs: list[dict[str, object]] = []
                hr_start: dict[str, object] = dict(
                    runs=hr_runs, completed_priors=0, audited_pass=False,
                    scheduling="source-admittance two-substack priors; staged minimum-R refinement; original audit; then unchanged classic search",
                    soft_method="L-BFGS-B", coordinate_scale_nm=100.0,
                    betas=[100.0, 400.0, 1500.0], max_iterations_per_stage=180)
                search_diagnostics["hr_minimax_start"] = hr_start
                initial_evaluations = evaluations
                initial_audit_calls = audit_physical_evaluations
                def hr_report(event):
                    search_diagnostics["structural_proposals"].append(deepcopy(event))
                    emit(event["stage"], json.dumps(event, sort_keys=True))
                def hr_progress(stage, candidate, details):
                    hr_start["latest_optimizer_progress"] = deepcopy(details)
                    hr_start["evaluations_used"] = evaluations - initial_evaluations
                    emit(stage, json.dumps(details, sort_keys=True), candidate["design"], candidate["score"])
                for proposal in iter_dual_substack_hr_proposals(request,
                        cancel_check=check_training_admission, report=hr_report):
                    check_training_admission()
                    entry: dict[str, object] = dict(proposal.provenance,
                        layer_count=len(proposal.design["front_layers"]), state="evaluating_prior")
                    hr_runs.append(entry)
                    raw_hr = evaluate(proposal.design, jacobian=True)
                    entry["initial_training_min_R"] = min(error.actual for error in raw_hr["score"]["errors"])
                    emit("hr_minimax_prior", json.dumps(entry, sort_keys=True), raw_hr["design"], raw_hr["score"])
                    archive(raw_hr, audit_eligible=False)
                    if (raw_hr["score"]["max_error"] <= request.acceptable_normalized_error
                            and strict_lower_pass(raw_hr["score"])):
                        audit_archive(raw_hr, allow_feedback_repair=False)
                    if not targets_verified():
                        entry["state"] = "refining"
                        refined_hr, hr_details = refine_hr_minimax(request, raw_hr,
                            evaluate=evaluate, archive=archive,
                            cancel_check=check_training_admission, progress=hr_progress,
                            max_iterations=180, soft_method="L-BFGS-B", coordinate_scale_nm=100.0)
                        entry["optimizer"] = hr_details
                        entry["final_training_min_R"] = min(error.actual for error in refined_hr["score"]["errors"])
                        archive(refined_hr, audit_eligible=False)
                        audit_archive(refined_hr)
                    entry["state"] = "completed"
                    hr_start["completed_priors"] = len(hr_runs)
                    hr_start["evaluations_used"] = evaluations - initial_evaluations
                    hr_start["audit_physical_evaluations_used"] = audit_physical_evaluations - initial_audit_calls
                    if targets_verified():
                        hr_start["audited_pass"] = True
                        assert audited_best is not None
                        passed_hr = audited_best[0]
                        emit("hr_minimax_targets_verified", "Generated HR prior and full thickness refinement passed original independent audit", passed_hr["design"], passed_hr["score"])
                        break
            # Automatic structural priors and true fixed-topology population
            # search share this parent evaluator, archive, audit and cancellation.
            from .film_synthesis_global import (
                iter_reflector_proposals, iter_bandpass_proposals, bandpass_objectives,
                DifferentialEvolutionProposals, structured_thickness_design,
                symmetric_thickness_parameters, symmetric_thickness_design,
            )
            from .film_synthesis_matching import iter_matching_proposals
            from .film_synthesis_ar_clusters import (
                iter_ar_cluster_proposals, ar_cluster_parameters, ar_cluster_design,
            )
            has_bandpass = bool(bandpass_objectives(request))

            def coverage_key(metadata):
                if metadata["family"] == "ar_cluster":
                    return (metadata["family"], metadata["variant"], metadata["cluster_count"],
                        tuple(metadata["pair_counts"]), tuple(metadata["high"]), tuple(metadata["low"]),
                        tuple(metadata["third_material"] or ()), metadata["boundary_variant"], metadata["first_role"])
                if metadata["family"] == "matching":
                    return (metadata["family"], metadata["variant"],
                        tuple(metadata["high"]), tuple(metadata["low"]), metadata["pairs"],
                        metadata["first_role"], metadata["reverse"], metadata["thin_mode"],
                        metadata.get("thin_quarter_fraction"))
                if metadata["family"] != "coupled_cavity":
                    return metadata["family"]
                order = metadata["inner_phase_orders"]["high"]
                return (metadata["family"], metadata["variant"],
                    tuple(metadata["high"]), tuple(metadata["low"]),
                    metadata["mirror_pairs"], metadata["cavity_count"],
                    metadata["matching_quarters"], metadata["reverse"],
                    int(order).bit_length() - 1, metadata["cavity_quarters"] % 2)

            def choose_structural_candidates(values):
                keepers: dict[object, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
                for value, metadata in values:
                    key = coverage_key(metadata)
                    if key not in keepers or rank(value) < rank(keepers[key][0]):
                        keepers[key] = (value, metadata)
                ordered = sorted(keepers.values(), key=lambda row: rank(row[0]))
                if not (has_bandpass or automatic_matching_route):
                    return ordered
                # Preserve phase/topology basins before revisiting another
                # mirror count from the same class. No lifetime work cap.
                buckets: dict[object, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
                for row in ordered:
                    meta = row[1]
                    key = ((meta["family"], meta["variant"], meta["cavity_count"],
                            int(meta["inner_phase_orders"]["high"]).bit_length() - 1)
                           if meta["family"] == "coupled_cavity" else
                           (meta["family"], meta["variant"], meta["pairs"], meta["thin_mode"])
                           if meta["family"] == "matching" else
                           (meta["family"], meta["variant"], meta["cluster_count"])
                           if meta["family"] == "ar_cluster" else (meta["family"],))
                    buckets.setdefault(key, []).append(row)
                result = []
                while any(buckets.values()):
                    for rows in buckets.values():
                        if rows:
                            result.append(rows.pop(0))
                return result
            def structural_report(event):
                search_diagnostics["structural_proposals"].append(deepcopy(event))
                emit(event["stage"], json.dumps(event, sort_keys=True))

            def scan_structural_proposals(epoch=0, *, work_quantum=None):
                check_training_admission()
                requested_epoch = epoch
                # Finish deferred coverage before opening an expanded epoch.
                # The live merged generator also retains its cavity/AR progress.
                if pending_structural_scans:
                    epoch = min(pending_structural_scans)

                # A bounded parent-owned chunk limits prepared designs and
                # speculative physical work. Keep only one complete best per
                # family, rather than retaining every full evaluated design.
                chunk_size = min(64, max(2, 2 * execution.workers))
                search_diagnostics["structure_batch_policy"] = dict(
                    cpu_chunk_size=chunk_size, maximum_chunk_size=64,
                    policy="min(64,max(2,2*effective_requested_workers)); ordered parent consumption",
                    gpu_policy="unchanged selected-device single forward")
                def merged_proposals():
                    if has_bandpass:
                        if cavity_stream[0] is None:
                            cavity_stream[0] = iter_bandpass_proposals(request, epoch=epoch,
                                cancel_check=check_training_admission, report=structural_report)
                            cavity_stream_epoch[0] = epoch
                        count = 0
                        try:
                            # This is a scheduling quantum. Keep the live lazy
                            # iterator for the next rotation instead of dropping
                            # unexplored phase orders or adding a total budget.
                            while count < 512:
                                try:
                                    active_cavity = cavity_stream[0]
                                    assert active_cavity is not None
                                    proposal = next(active_cavity)
                                except StopIteration:
                                    cavity_stream[0] = None
                                    break
                                count += 1
                                yield proposal
                        finally:
                            search_diagnostics["bandpass_structure_schedule"] = dict(
                                proposal_quantum=512, current_epoch=cavity_stream_epoch[0],
                                latest_consumed=count, pending=cavity_stream[0] is not None,
                                policy="persistent lazy cavity iterator; quantum is not a total budget")
                    if automatic_matching_route and (epoch > 0 or automatic_cluster_route):
                        matching_deferred[0] = False
                        ar_epoch = max(0, epoch - (0 if automatic_cluster_route else 1))
                        if matching_stream[0] is None:
                            matching_stream[0] = iter_matching_proposals(request, epoch=ar_epoch,
                                cancel_check=check_training_admission, report=structural_report)
                            matching_stream_epoch[0] = ar_epoch
                        if automatic_cluster_route and cluster_stream[0] is None:
                            cluster_stream[0] = iter_ar_cluster_proposals(request, epoch=ar_epoch,
                                cancel_check=check_training_admission, report=structural_report)
                            cluster_stream_epoch[0] = ar_epoch
                        streams = [matching_stream, cluster_stream] if automatic_cluster_route else [matching_stream]
                        counts = [0] * len(streams)
                        try:
                            # Interleave real noncluster/cluster structures so a
                            # long first family cannot consume every stage slot.
                            while sum(counts) < 512 and any(s[0] is not None for s in streams):
                                for index, stream in enumerate(streams):
                                    if stream[0] is None:
                                        continue
                                    try:
                                        proposal = next(stream[0])
                                    except StopIteration:
                                        stream[0] = None
                                        continue
                                    counts[index] += 1
                                    yield proposal
                                    if sum(counts) >= 512:
                                        break
                        finally:
                            search_diagnostics["matching_structure_schedule"] = dict(
                                proposal_quantum=512, current_epoch=matching_stream_epoch[0],
                                latest_consumed=counts[0], pending=matching_stream[0] is not None,
                                policy="persistent lazy AR iterator; v23 mode defers first cycle; no total budget")
                            if automatic_cluster_route:
                                search_diagnostics["cluster_structure_schedule"] = dict(
                                    shared_proposal_quantum=512, current_epoch=cluster_stream_epoch[0],
                                    latest_consumed=counts[1], pending=cluster_stream[0] is not None,
                                    noncluster_control_retained=True,
                                    policy="round-robin persistent noncluster/cluster iterators; initial global screening")
                    yield from iter_reflector_proposals(request, epoch=epoch,
                        cancel_check=check_training_admission, report=structural_report)
                if epoch not in pending_structural_scans:
                    pending_structural_scans[epoch] = merged_proposals()
                proposals = pending_structural_scans[epoch]
                exhausted = yielded = False
                stage_work = consumed = 0
                keepers: dict[object, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
                sample_points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
                def commit(proposal, value):
                    entry = dict(proposal.provenance, layer_count=len(proposal.design["front_layers"]),
                                 max_error=value["score"]["max_error"], rms=value["score"]["weighted_rms_error"])
                    search_diagnostics["structural_proposals"].append(entry)
                    emit("structure_proposal", json.dumps(entry, sort_keys=True), value["design"], value["score"])
                    archive(value)
                    key = coverage_key(proposal.provenance)
                    if key not in keepers or rank(value) < rank(keepers[key][0]):
                        keepers[key] = (value, proposal.provenance)
                    if targets_verified():
                        emit("structure_targets_verified", "Structural prior passed physical audit; thickness global search not needed", value["design"], value["score"])
                try:
                    while not targets_verified():
                        if work_quantum is not None and stage_work >= work_quantum:
                            yielded = True
                            break
                        check_training_admission()
                        remaining = None if request.max_evaluations is None else request.max_evaluations - evaluations - 1
                        if remaining is not None and remaining <= 0:
                            raise _Stopped("evaluation_limit")
                        count = chunk_size if remaining is None else min(chunk_size, remaining)
                        # GPU candidates retain the explicitly selected device;
                        # they are never silently routed through CPU workers.
                        if forward is not None:
                            count = 1
                        chunk: list[StructuralProposal] = []
                        chunk_work = 0
                        for _ in range(count):
                            proposal = pending_structural_proposals.pop(epoch, None)
                            if proposal is None:
                                try:
                                    proposal = next(proposals)
                                except StopIteration:
                                    exhausted = True
                                    break
                            item_work = sample_points * max(1, len(proposal.design["front_layers"]))
                            if (work_quantum is not None and (consumed or chunk)
                                    and stage_work + chunk_work + item_work > work_quantum):
                                pending_structural_proposals[epoch] = proposal
                                yielded = True
                                break
                            chunk.append(proposal)
                            chunk_work += item_work
                            # A candidate larger than the quantum is still one
                            # complete admitted candidate, never partial physics.
                            if work_quantum is not None and stage_work + chunk_work >= work_quantum:
                                break
                        if not chunk:
                            break
                        stage_work += chunk_work
                        consumed += len(chunk)
                        if forward is not None:
                            proposal = chunk[0]
                            commit(proposal, evaluate(proposal.design, gpu_forward=True))
                        else:
                            tasks = [EvaluationTask(p.design, False, (), p) for p in chunk]
                            work = sum(sample_points * max(1, len(p.design["front_layers"])) for p in chunk)
                            fits = remaining is None or len(tasks) <= remaining
                            with execution.batch(tasks, count=len(tasks), work=work,
                                    budget_allows=fits, phase="structure") as batch:
                                for task, outcome in batch:
                                    commit(task.context, evaluate(task.design, outcome=outcome))
                                    if targets_verified():
                                        break
                    return list(keepers.values())
                finally:
                    if exhausted or targets_verified():
                        proposals.close()
                        pending_structural_scans.pop(epoch, None)
                        pending_structural_proposals.pop(epoch, None)
                    if work_quantum is not None or requested_epoch != epoch:
                        schedule = dict(requested_epoch=requested_epoch, active_epoch=epoch,
                            point_layer_work_quantum=work_quantum,
                            estimated_admitted_point_layer_work=stage_work,
                            admitted_candidates=consumed, yielded=yielded,
                            pending=epoch in pending_structural_scans,
                            pending_candidate=epoch in pending_structural_proposals,
                            scope="initial classic screening slice, not a request budget; deferred iterator resumes before expanded epochs")
                        search_diagnostics["multispectral_structure_schedule"] = schedule
                        emit("structure_scan_yield" if yielded else "structure_scan_complete",
                            json.dumps(schedule, sort_keys=True))
            initial_scan_quantum = (STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM
                if multispectral_route and not separated_hr_only else None)
            structural_values = ([] if targets_verified() else
                scan_structural_proposals(work_quantum=initial_scan_quantum))
            # A stage quantum schedules population search, without claiming
            # exhaustive coverage. Subsequent cycles alternate expanded
            # structural enumeration/global epochs with ordinary needle growth.
            selected = choose_structural_candidates(structural_values)
            if multispectral_route and not separated_hr_only and not targets_verified():
                multispectral_prefix(selected)
            if selected and not targets_verified() and not automatic_cluster_route:
                prior, prior_metadata = selected[0]
                emit("structural_local_refinement", "64 analytic interior local iterations before global population; scheduling quantum, not a search stop", prior["design"], prior["score"])
                selected[0] = (interior_refine(prior), prior_metadata)
                audit_archive(best)
            def global_epoch(selected, epoch):
                if not selected or targets_verified():
                    return selected
                check_training_admission()
                explored_values = []
                next_selected = []
                for seed_value, metadata in selected:
                    if targets_verified():
                        break
                    for parameterization in ("structure", "thickness"):
                        check_training_admission()
                        ds = np.array([r["thickness"]["value"] for r in seed_value["design"]["front_layers"]])
                        expansion = 1 + .25 * epoch
                        lower = np.maximum(ds * .35 / expansion, request.minimum_nonzero_thickness_nm or np.finfo(float).tiny)
                        upper = ds * 2.0 * expansion
                        if request.max_layer_thickness_nm is not None:
                            upper = np.minimum(upper, request.max_layer_thickness_nm)
                        initial = ds
                        population_size = max(12, min(32, 2 * len(ds)))
                        symmetric_basis = None
                        if parameterization == "structure":
                            population_size = 24
                            if metadata["family"] == "ar_cluster":
                                initial, lower, upper = ar_cluster_parameters(
                                    seed_value["design"], metadata, epoch=epoch,
                                    minimum_nm=request.minimum_nonzero_thickness_nm,
                                    maximum_nm=request.max_layer_thickness_nm)
                                population_size = max(24, min(64, 4 * len(initial)))
                            elif metadata["family"] == "coupled_cavity":
                                # Local/full-layer refinement may improve an
                                # asymmetric stack. Retain that scored best;
                                # mirror-mean projection is only another real
                                # candidate, evaluated by the parent below.
                                symmetric_basis = deepcopy(seed_value["design"])
                                for group in metadata["symmetry_groups"]:
                                    mean = float(np.exp(np.mean(np.log(ds[list(group)]))))
                                    for index in group:
                                        symmetric_basis["front_layers"][index]["thickness"] = dict(value=mean, unit="nm")
                                initial, lower, upper = symmetric_thickness_parameters(
                                    symmetric_basis, metadata["symmetry_groups"], epoch=epoch,
                                    minimum_nm=request.minimum_nonzero_thickness_nm,
                                    maximum_nm=request.max_layer_thickness_nm)
                                population_size = max(24, min(64, 4 * len(initial)))
                            else:
                                initial = np.zeros(3)
                                upper = np.log([1.2, 1.2, 1.15]) * expansion
                                lower = -upper
                        global_record = dict(family=metadata["family"], layer_count=len(ds),
                            parameterization=parameterization, dimension=len(initial),
                            lower_parameters=lower.tolist(), upper_parameters=upper.tolist(),
                            bound_source="project proposal box: [0.35/expansion, 2*expansion] times current topology thickness; expansion=1+0.25*epoch; intersect explicit layer bounds; not manufacturing limits",
                            population_size=population_size, stage_generations=4, completed_generations=0,
                            seed=request.structure_search_seed + epoch, epoch=epoch, expansion=expansion, coverage_complete=False, stop_reason="stage_quantum")
                        if parameterization == "structure":
                            global_record["bound_source"] = "project log-parameter box: uniform scale/ramp +/-log(1.2), HL ratio +/-log(1.15), times epoch expansion; not manufacturing limits"
                            global_record["mapping"] = "d_i'=d_i*exp(a+b*t_pair+c*s_HL); t_pair in [-1,1], s_HL +/-1"
                            if metadata["family"] == "ar_cluster":
                                global_record["bound_source"] = "project cluster log-phase-budget/logit-fraction/envelope/boundary box; physical limits checked by parent; no manufacturing optical cap"
                                global_record["mapping"] = metadata["parameter_model"]
                                global_record["cluster_count"] = metadata["cluster_count"]
                                global_record["pair_counts"] = metadata["pair_counts"]
                                global_record["parameter_coordinates"] = metadata["parameter_coordinates"]
                            if symmetric_basis is not None:
                                global_record["bound_source"] = "project symmetric log(nm) groups, +/-log(8)*(epoch+1); intersect explicit layer bounds; not manufacturing limits"
                                global_record["mapping"] = "d_i=exp(x_group(i)); symmetric phase/topology parameters; original asymmetric incumbent retained"
                                global_record["symmetry_groups"] = metadata["symmetry_groups"]
                                global_record["symmetry_seed_projection"] = symmetric_basis != seed_value["design"]
                        search_diagnostics["global_search"].append(global_record)
                        evolution = DifferentialEvolutionProposals(initial, lower, upper, seed=request.structure_search_seed + epoch, population_size=population_size)
                        global_best = seed_value
                        for generation in range(5):
                            check_training_admission()
                            points = evolution.proposals()
                            tasks = []
                            losses = np.full(len(points), np.inf)
                            for index, point in enumerate(points):
                                check_training_admission()
                                if parameterization == "structure":
                                    if metadata["family"] == "ar_cluster":
                                        trial = ar_cluster_design(seed_value["design"], point, metadata)
                                    elif symmetric_basis is not None:
                                        trial = symmetric_thickness_design(symmetric_basis, point,
                                            symmetry_groups=metadata["symmetry_groups"])
                                    else:
                                        trial = structured_thickness_design(seed_value["design"], point, high_pair=metadata["high"])
                                else:
                                    trial = deepcopy(seed_value["design"])
                                    for row, thickness in zip(trial["front_layers"], point, strict=True):
                                        row["thickness"] = dict(value=float(thickness), unit="nm")
                                if feasible(trial):
                                    tasks.append(EvaluationTask(trial, False, (), index))
                            sample_points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
                            limit = request.max_evaluations
                            fits = limit is None or len(tasks) <= limit - evaluations - 1
                            prefix = "structure_global" if parameterization == "structure" else "global"
                            emit(prefix + ("_initial_population" if generation == 0 else "_generation"),
                                 json.dumps(dict(global_record, generation=generation, feasible_population=len(tasks)), sort_keys=True), global_best["design"], global_best["score"])
                            with execution.batch(tasks, count=len(tasks),
                                    work=len(tasks) * sample_points * max(1, len(ds)),
                                    budget_allows=fits, phase="structure_global" if parameterization == "structure" else "global") as batch:
                                for task, outcome in batch:
                                    value = evaluate(task.design, outcome=outcome)
                                    # Worst normalized error is the common primary merit;
                                    # exact shared rank retains every scored incumbent.
                                    losses[task.context] = value["score"]["max_error"]
                                    archive(value, audit_eligible=False)
                                    if rank(value) < rank(global_best):
                                        global_best = value
                                    emit(prefix + "_proposal", "Population proposal: " + parameterization + "; no layer penalty", value["design"], value["score"])
                            evolution.accept(losses)
                            global_record["completed_generations"] = evolution.generations
                            if (global_best["score"]["max_error"] <= request.acceptable_normalized_error
                                    and strict_lower_pass(global_best["score"])):
                                audit_archive(global_best)
                            if targets_verified():
                                global_record["stop_reason"] = "validated_targets_met"
                                break
                        if parameterization == "structure" and not targets_verified():
                            global_best = interior_refine(global_best)
                            archive(global_best, audit_eligible=False)
                            if (global_best["score"]["max_error"] <= request.acceptable_normalized_error
                                    and strict_lower_pass(global_best["score"])):
                                audit_archive(global_best)
                        seed_value = global_best
                        if targets_verified():
                            break
                    explored_values.append(global_best)
                    next_selected.append((global_best, metadata))
                    emit("global_complete", json.dumps(global_record, sort_keys=True), global_best["design"], global_best["score"])
                if explored_values and not targets_verified():
                    # One shared local refinement/audit at the stage boundary;
                    # rejected structures do not each trigger an expensive audit.
                    refined = interior_refine(min(explored_values, key=rank))
                    archive(refined)
                    # Feed the refined topology back into the next population.
                    matching = next((i for i, (v, _m) in enumerate(next_selected) if [r["id"] for r in v["design"]["front_layers"]] == [r["id"] for r in refined["design"]["front_layers"]]), None)
                    if matching is not None:
                        next_selected[matching] = (refined, next_selected[matching][1])
                    assert best is not None  # The baseline was archived before global search.
                    if audited_best is None or epoch % 2 == 1 or (best["score"]["max_error"] <= request.acceptable_normalized_error and strict_lower_pass(best["score"])):
                        audit_archive(best)
                    else:
                        emit("audit_deferred", "Keep prior audited incumbent; defer nonpassing stage audit to every second global epoch", best["design"], best["score"])
                return next_selected
            selected = global_epoch(selected, 0)
            branches = [working]
            if structural_values and not targets_verified():
                # Retain one globally explored branch in addition to bare growth.
                assert best is not None  # The baseline was archived before structural search.
                branches.append(best)
            blocked_branches: set[int] = set()
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
                growth_available = len(blocked_branches) < len(branches)
                cycle += 1
                continuous_available = (bool(selected) or bool(pending_structural_scans)
                    or cavity_stream[0] is not None
                    or matching_stream[0] is not None or cluster_stream[0] is not None
                    or matching_deferred[0])
                if continuous_available and (not growth_available or cycle % 2 == 0):
                    # Alternate continuous global epochs with real structural
                    # growth; increasing proposal families also revisit layer
                    # counts independently of the greedy working branch.
                    extra = scan_structural_proposals(epoch=cycle // 2)
                    selected = choose_structural_candidates((*selected, *extra))
                    selected = global_epoch(selected, cycle // 2)
                    if (not growth_available and not selected and not pending_structural_scans and cavity_stream[0] is None
                            and matching_stream[0] is None and cluster_stream[0] is None
                            and not matching_deferred[0]):
                        raise _Stopped("no_feasible_growth")
                    emit("search_rotation", "Completed structural/thickness epoch; retain remaining proposals even when insertion branches are exhausted", best["design"], best["score"])
                    continue
                if not growth_available:
                    raise _Stopped("no_feasible_growth")
                branch_index = ((cycle - 1) % len(branches))
                while branch_index in blocked_branches:
                    branch_index = (branch_index + 1) % len(branches)
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
                            check_training_admission()
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
                            check_training_admission()
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
                            budget_allows=request.max_evaluations is None or growth_count <= request.max_evaluations - evaluations - 1, phase="growth"))
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
                        if (len(blocked_branches) == len(branches)
                                and not selected and not pending_structural_scans and cavity_stream[0] is None
                                and matching_stream[0] is None and cluster_stream[0] is None
                                and not matching_deferred[0]):
                            raise _Stopped("no_feasible_growth")
                        emit("branch_exhausted", "No feasible insertion remains in this branch; continue other growth branches or structural/thickness search", working["design"], working["score"])
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
            if search_diagnostics["global_search"]:
                search_diagnostics["global_search"][-1]["stop_reason"] = reason
        except _StructureCancelled:
            state, reason = (("not_found", "time_limit")
                if stop_event.time_limit_reached() and not stop_event.caller_cancelled()
                else ("cancelled", "cancelled"))
            if search_diagnostics["global_search"]:
                search_diagnostics["global_search"][-1]["stop_reason"] = reason
        except (ValueError, ArithmeticError, RuntimeError) as exc:
            state, reason = "failed", f"numerical_failure: {exc}"
            if search_diagnostics["global_search"]:
                search_diagnostics["global_search"][-1]["stop_reason"] = reason

        # A wall deadline, like cancellation, never admits a new final audit.
        if best is not None and state != "cancelled" and reason != "time_limit":
            try:
                audit_archive(best)
                if audited_best is not None:
                    validated_training, validation = audited_best
                    if state != "failed" and validated_training["score"]["max_error"] <= request.acceptable_normalized_error:
                        if (validation["score"]["max_error"] <= request.acceptable_normalized_error
                                and (not request.strict_lower_bound_objective_indices or targets_verified())):
                            state, reason = "completed", "validated_targets_met"
                        else:
                            reason = "validation_targets_not_met"
            except _StructureCancelled:
                state, reason = (("not_found", "time_limit")
                    if stop_event.time_limit_reached() and not stop_event.caller_cancelled()
                    else ("cancelled", "cancelled_during_validation"))
            except _Stopped as exc:
                reason = str(exc)
            except (ValueError, ArithmeticError, RuntimeError) as exc:
                state, reason = "failed", f"validation_failed: {exc}"
        if state == "cancelled" or reason == "time_limit":
            # Freeze only values already committed above. Pool shutdown may
            # collect work counters, but cannot promote any late candidate.
            best, validation = completed_training_best, completed_training_validation
            candidate_provenance = {
                "selection": ("completed_training_at_cancel" if state == "cancelled"
                              else "completed_training_at_time_limit") if best is not None else "none",
                "training_complete": best is not None,
                "fully_scored_evaluations": fully_scored_evaluations,
                "validation_matches_candidate": validation is not None}
            candidate_provenance["frozen_at_cancel" if state == "cancelled" else "frozen_at_time_limit"] = True
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
    except _ObserverFailure as exc:
        raise exc.error.with_traceback(exc.error.__traceback__) from None
    finally:
        execution.close()
        if forward is not None:
            forward.close()
        for pending_scan in pending_structural_scans.values():
            pending_scan.close()
        if cavity_stream[0] is not None:
            cavity_stream[0].close()
        if matching_stream[0] is not None:
            matching_stream[0].close()
        if cluster_stream[0] is not None:
            cluster_stream[0].close()
