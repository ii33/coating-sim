"""GUI-independent unified hybrid synthesis and explicit legacy reproduction.

Source-derived multi-family priors, a real shared diversity/Pareto design
archive, global/local thickness exploration, analytic batch needles and
full-original-constraint SLSQP share one physical evaluator and audit owner.
The real exploration endpoint may get worse; its strict champion stays separate.
No candidate is applied to a session. See the synthesis contract and algorithms.
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
from .film_synthesis_progress import HybridProgressPolicy, validate_progress_policy

from .film_design import MAX_LAYERS, physical_thickness_nm, validate_design
from .film_optimization import FilmConditionError, FilmOptimizationObjective, FilmOptimizationRequest, freeze_film_optimization_request
from .film_optimization_needle import FilmNeedleMaterialReference, _candidate_payload_from_materials, _normalize_objectives
from .film_optimization_structure import (
    FilmStructureOptimizationRequest, _StructureCancelled, _insert_at_site,
    _site_records, _PreparedStructureEvaluator, evaluate_frozen_structure_candidate,
)
from .optical_constants import UNITS_TO_UM, prepare_optical_set
from .film_nominal_acceptance import nominal_targets_pass, solver_acceptable
from .film_objective_criteria import active_constraints
from .film_synthesis_absorption import (annotate_synthesis_design, assumes_unknown_k_zero,
                                        sample_synthesis_optical)
from .film_synthesis_randomization import (validate_template_seed, randomized_template_start,
    template_randomization_metadata)


if TYPE_CHECKING:
    from .film_synthesis_global import StructuralProposal


# Scheduling work proxy, not a request/physics budget or a feasibility bound.
STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM = 1_000_000

# Advisory numerical-work admission, not an OS memory quota or a layer bound.
# 0 requests one conservative host/container headroom measurement at freeze;
# positive integers and None retain their explicit-limit/off meanings.
SYNTHESIS_WORKING_SET_FALLBACK_BYTES = 1_610_612_736
_RESOURCE_RESOLUTION_KEYS = frozenset(("version", "mode", "requested_bytes",
    "effective_bytes", "available_bytes", "basis"))
_RESOURCE_FALLBACK_BASES = frozenset(("unknown_memory_fallback",
    "invalid_memory_fallback", "memory_probe_error_fallback"))


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
    hybrid: NotRequired[dict[str, Any]]
    design_archive: NotRequired[dict[str, Any]]
    template_randomization: NotRequired[dict[str, Any]]
    warm_start: NotRequired[dict[str, Any]]


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
    max_working_set_bytes uses 0=auto, positive=explicit bytes, None=off. Derived
    resource_guard_resolution freezes its v1 policy for deterministic replay;
    it is staged numerical admission, not a process memory isolation guarantee.
    acceptable_normalized_error is solver tolerance only, never nominal target
    acceptance. parallel_batch_size=None is AUTO, resolved once at freeze from
    requested/available CPUs and preserved as a positive logical quantum when
    execution cpu_workers changes for serial/parallel replay. Range bounds
    use the independently enabled pointwise/mean constraints and their nested
    strict bounds for nominal acceptance.
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
    structure_search_seed: int | None = 0
    ar_structure_mode: str = "clustered"
    max_seconds: float | None = None
    search_strategy: str = "hybrid"
    archive_capacity: int = 32
    needle_batch_size: int = 6
    needle_pre_local: bool = False
    needle_position_refinement_rounds: int = 1
    max_working_set_bytes: int | None = 0
    resource_guard_resolution: Mapping[str, Any] | None = None
    parallel_batch_size: int | None = None
    randomize_template_search: bool = False
    enable_thickness_reduction: bool = True
    objective_progress_policy: HybridProgressPolicy | None = None
    # Explicit current-protocol candidate warm restart. The referenced seed is
    # freshly evaluated first; no saved score, audit or solver state is reused.
    warm_start_seed_index: int | None = None


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
    """Nominal task success and independent solver tolerance are separate.

    solver_acceptable is None without a complete matching independent audit;
    candidate quality never overrides cancelled, failed or budget-limited state.
    """

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
    solver_acceptable: bool | None = None


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


def _checked_resource_guard_resolution(request: FilmSynthesisRequest) -> dict[str, Any] | None:
    """Validate frozen v1 resource policy without sampling this replay host.

    Changing the requested policy deliberately discards an otherwise valid old
    resolution. A saved unchanged auto request retains its exact numerical cap,
    even on another host; set the resolution to None to explicitly resample.
    """
    raw = request.resource_guard_resolution
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != _RESOURCE_RESOLUTION_KEYS:
        raise ValueError("resource_guard_resolution must be a complete versioned object or None")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("unsupported resource_guard_resolution version")
    if type(raw["mode"]) is not str or type(raw["basis"]) is not str:
        raise ValueError("resource_guard_resolution mode and basis must be strings")
    requested, effective, available = (raw[name] for name in
        ("requested_bytes", "effective_bytes", "available_bytes"))
    for name, value in (("requested_bytes", requested), ("effective_bytes", effective),
                        ("available_bytes", available)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"resource_guard_resolution {name} must be a nonnegative integer or None")
    if requested is None:
        valid = (raw["mode"] == "disabled" and effective is None and available is None
                 and raw["basis"] == "disabled")
    elif requested > 0:
        valid = (raw["mode"] == "explicit" and effective == requested and available is None
                 and raw["basis"] == "explicit_request")
    elif available is not None:
        valid = (raw["mode"] == "auto" and effective == available // 2
                 and raw["basis"] == "half_available_memory_at_freeze")
    else:
        valid = (raw["mode"] == "auto" and effective == SYNTHESIS_WORKING_SET_FALLBACK_BYTES
                 and raw["basis"] in _RESOURCE_FALLBACK_BASES)
    if not valid:
        raise ValueError("resource_guard_resolution is inconsistent with its versioned policy")
    return deepcopy(dict(raw)) if requested == request.max_working_set_bytes else None


def _resolve_synthesis_resource_guard(request: FilmSynthesisRequest) -> dict[str, Any]:
    """Freeze an explicit/off policy or one allocation-free conservative sample.

    One half of available host/container memory is a project heuristic, not an
    allocation guarantee. Unknown measurements keep the old conservative byte
    estimate, visibly labelled as fallback rather than inferred free memory.
    """
    existing = _checked_resource_guard_resolution(request)
    if existing is not None:
        return existing
    requested = request.max_working_set_bytes
    available = None
    if requested is None:
        mode, effective, basis = "disabled", None, "disabled"
    elif requested > 0:
        mode, effective, basis = "explicit", requested, "explicit_request"
    else:
        from .compute_resources import available_memory_bytes
        mode, effective, basis = "auto", SYNTHESIS_WORKING_SET_FALLBACK_BYTES, "unknown_memory_fallback"
        try:
            measured = available_memory_bytes()
        except (_StructureCancelled, _Stopped):
            raise
        except Exception:
            basis = "memory_probe_error_fallback"
        else:
            if type(measured) is int and measured >= 0:
                available, effective = measured, measured // 2
                basis = "half_available_memory_at_freeze"
            elif measured is not None:
                basis = "invalid_memory_fallback"
    return dict(version=1, mode=mode, requested_bytes=requested,
                effective_bytes=effective, available_bytes=available, basis=basis)


def _synthesis_working_set_estimate_bytes(request: FilmSynthesisRequest, layer_count: int,
                                        *, jacobian=False, epigraph=False) -> int:
    """Allocation-free estimate shared by actual admission and policy tests."""
    count = max(1, layer_count)
    points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    estimate = 128 * 1024**2 + points * count * (64 if jacobian else 32)
    if epigraph:
        from .film_objective_criteria import signed_bound_count
        constraints = signed_bound_count(request.objectives)
        estimate += 16 * constraints * (count + 1)
    return estimate


def _validated(request):
    if not isinstance(request, FilmSynthesisRequest):
        raise ValueError("request must be FilmSynthesisRequest")
    if type(request.search_strategy) is not str or request.search_strategy not in {"hybrid", "legacy"}:
        raise ValueError("search_strategy must be hybrid or legacy")
    for field_name, minimum in (("archive_capacity", 2), ("needle_batch_size", 1),
                                ("needle_position_refinement_rounds", 0)):
        if type(getattr(request, field_name)) is not int or getattr(request, field_name) < minimum:
            raise ValueError(f"{field_name} must be an integer >= {minimum}")
    if type(request.enable_thickness_reduction) is not bool:
        raise ValueError("enable_thickness_reduction must be boolean")
    validate_progress_policy(request.objective_progress_policy)
    if request.objective_progress_policy is not None and request.search_strategy != "hybrid":
        raise ValueError("objective_progress_policy is supported only by hybrid search")
    if type(request.needle_pre_local) is not bool:
        raise ValueError("needle_pre_local must be boolean")
    if request.max_working_set_bytes is not None and (type(request.max_working_set_bytes) is not int
                                                     or request.max_working_set_bytes < 0):
        raise ValueError("max_working_set_bytes must be 0 (auto), a positive integer, or None (off)")
    resource_guard_resolution = _checked_resource_guard_resolution(request)
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
    validate_template_seed(request.structure_search_seed, enabled=request.randomize_template_search)
    if type(request.cpu_workers) is not int or request.cpu_workers < 0:
        raise ValueError("cpu_workers must be a nonnegative integer (0=auto, 1=serial)")
    if request.parallel_batch_size is not None and (
            type(request.parallel_batch_size) is not int or request.parallel_batch_size < 1):
        raise ValueError("parallel_batch_size must be a positive integer or None (AUTO)")
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
    if request.warm_start_seed_index is not None and (
            type(request.warm_start_seed_index) is not int
            or not 0 <= request.warm_start_seed_index < len(request.seed_designs)):
        raise ValueError("warm_start_seed_index must reference an existing seed or be null")
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
                   resource_guard_resolution=resource_guard_resolution,
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
    _check_cancel(cancel_event)
    resource_resolution = _resolve_synthesis_resource_guard(result)
    _check_cancel(cancel_event)
    batch_size = result.parallel_batch_size
    if batch_size is None and result.search_strategy == "hybrid":
        from .film_performance import available_cpu_workers
        available = available_cpu_workers()
        batch_size = max(1, min(result.cpu_workers or available, available))
    resolved_template_seed = result.structure_search_seed
    if result.randomize_template_search and resolved_template_seed is None:
        # The only entropy owner. Validation/property reads and stream
        # derivation never draw, and replay already has an integer.
        from secrets import randbits
        resolved_template_seed = randbits(32)
    return replace(result, seed_designs=tuple(seeds), resource_guard_resolution=resource_resolution,
                   parallel_batch_size=batch_size, structure_search_seed=resolved_template_seed)


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
                           snapshot_callback: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], None] | None = None,
                           candidate_observer: Any = None) -> FilmSynthesisResult:
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
    template_seed_provenance = ("auto_entropy_at_freeze" if request.randomize_template_search
        and request.structure_search_seed is None else "explicit_or_frozen")
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
    reserved_training_slots = 0
    best = None
    audited_best = None
    completed_training_best = None
    completed_training_validation = None
    fully_scored_evaluations = 0
    terminal_hybrid_cleanup: Callable[[], None] | None = None
    candidate_provenance = {}
    search_diagnostics: _SearchDiagnostics = {"structural_proposals": [], "global_search": [],
        "layer_distribution": {}, "local_refinement": []}
    from .film_synthesis_archive import SynthesisDesignArchive
    design_archive = SynthesisDesignArchive(len(request.objectives),
        capacity=request.archive_capacity, rank=lambda value: rank(value))
    archive_context: dict[str, Any] = {}
    hybrid_streams: list[Any] = []
    audited_signatures = set()
    pending_structural_scans: dict[int, Generator[StructuralProposal, None, None]] = {}
    pending_structural_proposals: dict[int, StructuralProposal] = {}
    # Persistent proposal generators consult the current slice, rather than
    # retaining the work counter of the call that first constructed them.
    legacy_template_work_admission: list[Callable[[Any], bool]] = [lambda proposal: True]
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
    from .film_candidate_display import CandidateDisplayObserver, finish_candidate_display, record_candidate

    def display_value(value, *, is_best=False, is_current=True):
        if type(candidate_observer) is not CandidateDisplayObserver or value is None:
            return
        record_candidate(candidate_observer, value["design"],
            weighted_rms_error=value["score"]["weighted_rms_error"],
            max_normalized_error=value["score"]["max_error"], condition_rows=value["score"]["errors"],
            raw_coverage=value["score"]["coverage"],
            physical_thickness_nm=tuple(row["thickness"]["value"]
                for side in ("front_layers", "back_layers") for row in value["design"].get(side, ())),
            is_best=is_best, is_current=is_current, iterations=cycle, evaluations=evaluations,
            source="film_synthesis", scope="training")

    def close_proposal_streams():
        # Merged generators publish their consumed-family diagnostics on close.
        # Finish that publication before taking the terminal result snapshot;
        # do not exhaust pending candidates or admit any additional physics.
        for pending_scan in pending_structural_scans.values():
            pending_scan.close()
        pending_structural_scans.clear()
        pending_structural_proposals.clear()
        for stream in hybrid_streams:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        hybrid_streams.clear()
        for stream in (cavity_stream, matching_stream, cluster_stream):
            if stream[0] is not None:
                stream[0].close()
                stream[0] = None

    def performance():
        result = execution.diagnostics()
        result["stage_wall_seconds"] = dict(stage_times)
        result["search"] = deepcopy(search_diagnostics)
        result["search"]["design_archive"] = design_archive.snapshot()
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
        if request.max_evaluations is not None and evaluations + reserved_training_slots >= request.max_evaluations - 1:
            raise _Stopped("evaluation_limit")

    def extra_template_start(proposal, *, stage, candidate_slots, cancel_check=None,
                             resource_check=None, jacobian=False):
        """Construct only after reserving base+extra candidate/physical slots.

        This branch is never called when off. The caller keeps BASE first and
        must dispatch its extra before admitting another deterministic template.
        Physical work and subsequent local refinement retain existing owners.
        """
        from .film_synthesis_global import StructuralProposal
        from .film_synthesis_archive import physical_design_hash
        check = cancel_check or check_training_admission
        _check_cancel(cancel_event)
        metadata = search_diagnostics["template_randomization"]
        record = dict(stage=stage, parent_physical_hash=physical_design_hash(proposal.design),
            status="skipped", attempts=0, reserved_candidate_slots=0,
            reason="insufficient_extra_candidate_or_physical_budget_base_retained")
        metadata["templates"].append(record)
        if candidate_slots < 2:
            emit("template_randomization", json.dumps(record, sort_keys=True), proposal.design)
            return None
        check()
        record["reserved_candidate_slots"] = 2
        if resource_check is not None:
            resource_check(proposal.design, jacobian=jacobian)
        else:
            # Legacy work has no hybrid stage owner. Admission still uses the
            # frozen shared numerical resource guard before any extra draw/F/J.
            limit = _resolve_synthesis_resource_guard(request)["effective_bytes"]
            estimate = _synthesis_working_set_estimate_bytes(request,
                len(proposal.design["front_layers"]), jacobian=jacobian)
            if limit is not None and estimate > limit:
                record.update(reason="resource_guard_base_retained", estimated_bytes=estimate,
                              effective_max_working_set_bytes=limit)
                emit("template_randomization", json.dumps(record, sort_keys=True), proposal.design)
                return None
        assert request.structure_search_seed is not None
        randomized, outcome = randomized_template_start(proposal.design,
            root_seed=request.structure_search_seed, stage=stage,
            minimum_nm=request.minimum_nonzero_thickness_nm,
            maximum_nm=request.max_layer_thickness_nm,
            total_maximum_nm=request.max_total_thickness_nm, feasible=feasible,
            cancel_check=check, seen_hashes=randomized_template_hashes)
        record.update(outcome)
        emit("template_randomization", json.dumps(record, sort_keys=True), proposal.design)
        if randomized is None:
            return None
        randomized_template_hashes.add(outcome["result_physical_hash"])
        return StructuralProposal(randomized, dict(proposal.provenance,
            template_randomization=dict(outcome, algorithm_version=metadata["algorithm_version"],
                effective_seed=request.structure_search_seed)))

    randomized_template_hashes: set[str] = set()

    def legacy_template_stream(source, *, stage, jacobian=False, extra_admit=lambda proposal: True):
        """Enabled-only additive stream; caller consumes BASE before extra."""
        try:
            for proposal in source:
                yield proposal
                if targets_verified():
                    return
                # BASE has been consumed; reserve one remaining training slot
                # for the extra (the final independent audit stays reserved).
                remaining = (2 if request.max_evaluations is None else
                    request.max_evaluations - evaluations - reserved_training_slots)
                if not extra_admit(proposal):
                    remaining = 0
                extra = extra_template_start(proposal, stage=stage,
                    candidate_slots=remaining, jacobian=jacobian)
                if extra is not None:
                    yield extra
        finally:
            close = getattr(source, "close", None)
            if close is not None:
                close()

    prepared_training: list[_PreparedStructureEvaluator | None] = [None]

    def evaluate(design, *, jacobian=False, zero_ids=(), holdout=False, outcome=None, gpu_forward=False, _reserved=False,
                 feasibility_evaluator=None):
        nonlocal evaluations, validation_evaluations, failures, reserved_training_slots
        nonlocal audit_physical_evaluations, audit_wavelength_points
        nonlocal completed_training_best, completed_training_validation, fully_scored_evaluations
        if holdout:
            _check_cancel(cancel_event)
            if request.max_evaluations is not None and evaluations + reserved_training_slots >= request.max_evaluations:
                raise _Stopped("evaluation_limit")
        elif _reserved:
            _check_cancel(cancel_event)
            if reserved_training_slots <= 0:
                raise RuntimeError("hybrid evaluation has no reserved training slot")
            reserved_training_slots -= 1
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
                    if feasibility_evaluator is not None:
                        if not jacobian or zero_ids or outcome is not None or gpu_forward:
                            raise ValueError("feasibility repair requires a serial complete analytic evaluation")
                        return feasibility_evaluator.evaluate(design, cancel_event=cancel_event)
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
                    value = execution.serial(physical_call, task, fallback=execution.disabled)
                else:
                    try:
                        value = execution.take(outcome)
                    except InfrastructureFailure:
                        value = execution.serial(physical_call, task, fallback=True)
            if feasibility_evaluator is not None:
                # The executor counted the exact joint physical grids. Restore
                # canonical original responses before any rank/display/commit.
                value = feasibility_evaluator.restore(value)
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
                display_value(value)
            return value
        except _StructureCancelled:
            raise
        except (ValueError, ArithmeticError, RuntimeError):
            failures += 1
            raise

    def rank(candidate):
        legacy = _synthesis_candidate_rank(candidate, 0.0)
        strict_active = any(c.strict_lower or c.strict_upper
                            for o in request.objectives for _, c in active_constraints(o))
        passed = nominal_targets_pass(request, candidate["score"])
        if request.search_strategy == "hybrid":
            if not strict_active:
                return (int(not passed), *legacy)
            score = candidate["score"]
            rows = candidate["design"]["front_layers"]
            cost = (len(rows), sum(row["thickness"]["value"] for row in rows))
            if passed:
                return (0, *cost, score["max_error"], score["weighted_rms_error"])
            return (1, score["max_error"], score["weighted_rms_error"], *cost)
        if not (automatic_reflector_route or automatic_matching_route) and not strict_active:
            return (int(not passed), *legacy)
        score = candidate["score"]
        slacks = []
        for error in score.get("errors", ()):
            criterion = getattr(request.objectives[error.objective_index], error.reduction)
            if criterion.strict_lower or (automatic_reflector_route
                    and criterion.mode == "range" and criterion.lower_bound is not None):
                slacks.append(error.actual - criterion.lower_bound)
            if criterion.strict_upper:
                slacks.append(criterion.upper_bound - error.actual)
        slack = min(slacks, default=0.) if score["max_error"] == 0 else 0.
        return (int(not passed), score["max_error"], -slack, *legacy)

    def audit_archive(value, *, allow_feedback_repair=True, refresh=False):
        nonlocal audited_best, completed_training_validation
        signature = hashlib.sha256(json.dumps(value["design"], sort_keys=True, allow_nan=False).encode("utf-8")).digest()
        _check_cancel(cancel_event)
        if signature in audited_signatures and not refresh:
            return (audited_best[1] if audited_best is not None
                    and audited_best[0]["design"] == value["design"] else None)
        emit("validation", "Audit dense, offset and local-extremum physical responses", value["design"], value["score"])
        audited = evaluate(value["design"], holdout=True)
        audited_signatures.add(signature)
        design_archive.mark_audited(value, audited)
        if (completed_training_best is not None and
                completed_training_best["design"] == value["design"]):
            completed_training_validation = {"design": deepcopy(audited["design"]),
                                             "score": _copy_internal_candidate(audited["score"])}
        def audited_rank(training, audit):
            both_nominal = (nominal_targets_pass(request, training["score"])
                and nominal_targets_pass(request, audit["score"], independent_audit=True))
            return (int(not both_nominal), *rank(audit))
        if (audited_best is None
                or audited_rank(value, audited) < audited_rank(*audited_best)):
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
        return audited

    def targets_verified():
        return (audited_best is not None
                and nominal_targets_pass(request, audited_best[0]["score"])
                and nominal_targets_pass(request, audited_best[1]["score"], independent_audit=True))

    def archive(value, *, audit_eligible=True, provenance=None, parent_hash=None, operator=None):
        nonlocal best
        if feasible(value["design"]):
            design_archive.add(value, provenance=provenance or archive_context.get("provenance"),
                parent_hash=parent_hash or archive_context.get("parent_hash"),
                operator=operator or archive_context.get("operator", "evaluated"))
        if feasible(value["design"]) and (best is None or rank(value) < rank(best)):
            best = _copy_internal_candidate(value)
            display_value(best, is_best=True, is_current=False)
            emit("incumbent", "已保存独立评分的候选，等待最终密网格验证", value["design"], value["score"])
        # A training-feasible candidate may be better on audit even when it
        # loses the training cost tie-break. Audit it before discarding it.
        if (audit_eligible and feasible(value["design"])
                and value["score"]["max_error"] <= request.acceptable_normalized_error
                and solver_acceptable(request, value["score"])):
            audit_archive(value, allow_feedback_repair=request.search_strategy == "legacy")


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

    def run_hybrid(initial):
        """One shared staged search; strict archive and real explorer are distinct."""
        nonlocal cycle, terminal_hybrid_cleanup
        assert request.structure_search_seed is not None  # Formal freeze resolved auto.
        hybrid_structure_seed = request.structure_search_seed
        frozen_snapshot = request.frozen_material_snapshot
        assert frozen_snapshot is not None
        from .film_synthesis_archive import physical_design_hash
        from .film_synthesis_refinement import (
            SynthesisStageBudget, SynthesisResourceGuard, synthesis_window_masses,
            weighted_sse_loss_gradient, refine_synthesis_explorer,
            refine_synthesis_full_minimax,
        )
        from .film_synthesis_batch_needle import (
            scan_batch_needles, build_batch_needle_design, cleanup_batch_needle_design,
        )
        from .film_synthesis_global import (StructuralProposal, iter_reflector_proposals,
            iter_bandpass_proposals, DifferentialEvolutionProposals)
        from .film_synthesis_matching import iter_matching_proposals
        from .film_synthesis_ar_clusters import iter_ar_cluster_proposals
        from .film_synthesis_multispectral_priors import iter_multispectral_hr_proposals
        from .film_synthesis_hr_priors import iter_dual_substack_hr_proposals

        resource_resolution = _resolve_synthesis_resource_guard(request)
        assert request.parallel_batch_size is not None  # Resolved once at freeze.
        batch_quantum = request.parallel_batch_size
        if request.cpu_workers == 0:
            # Hybrid AUTO follows its frozen request rather than the legacy
            # executor default of eight. Available CPUs remain an actual cap.
            execution.workers = min(batch_quantum, execution.available_workers)
        details: dict[str, Any] = dict(strategy="hybrid",
            objective_masses=synthesis_window_masses(request).tolist(), stages=[], needle_scans=[],
            parallel_batches=[],
            exploration_endpoints=[], resource_skips=[], original_grid_points=sum(
                o.wavelength_samples * len(o.angles_deg) for o in request.objectives),
            max_working_set_bytes=resource_resolution["effective_bytes"],
            requested_max_working_set_bytes=request.max_working_set_bytes,
            effective_max_working_set_bytes=resource_resolution["effective_bytes"],
            resource_guard_resolution=deepcopy(resource_resolution), resource_limited=False,
            resource_guard_formula="128 MiB + points*layers*(64 Jacobian / 32 forward) bytes + retained archive Python storage; full SLSQP adds 16*bound_conditions*(layers+1) bytes",
            source_prefix_batch_quantum=batch_quantum,
            frozen_parallel_batch_size=batch_quantum,
            maximum_simultaneous_hybrid_workers=min(batch_quantum, execution.workers),
            budget_policy="each rotation: <=25% remaining screening, <=15% local/global, <=25% scan, <=45% joint SSE, <=15% compression; remaining reserved for full-original SLSQP and final audit; stage expiry is not convergence",
            strict_champion_role="original physical full-grid max violation; never overwritten by SSE endpoint",
            no_implicit_layer_limit=True, schema_layer_limit=MAX_LAYERS)
        search_diagnostics["hybrid"] = details
        active_slice = None
        explorer = initial
        objective_handoff_cycle = None
        source_request = replace(request, seed_designs=())
        seen_priors = set()
        no_negative_rounds = 0
        total_real_growth = 0

        def begin_slice(stage, fraction, quantum):
            nonlocal active_slice
            check_training_admission()
            remaining = (None if request.max_evaluations is None else
                         max(0, request.max_evaluations - evaluations - 1))
            cap = quantum if remaining is None else min(quantum, max(1, int(remaining * fraction)))
            now = monotonic()
            seconds = None if stop_event.deadline is None else max(0., stop_event.deadline - now)
            record = dict(stage=stage, cycle=cycle, start_evaluations=evaluations,
                admitted_evaluation_quantum=cap, remaining_training_evaluations=remaining,
                wall_time_share=fraction, elapsed_seconds=0., evaluations_used=0, termination="running",
                reserved_audit_slots=1 if stage == "engineering_prior_screen" and cap > 1 else 0)
            details["stages"].append(record)
            active_slice = (record, evaluations + cap, None if seconds is None else now + fraction * seconds, now)
            return record

        def stage_check():
            check_training_admission()
            if active_slice is not None:
                record, end_evaluations, deadline, _start = active_slice
                if evaluations >= end_evaluations or (deadline is not None and monotonic() >= deadline):
                    record["termination"] = "stage_budget"
                    raise SynthesisStageBudget(record["stage"])

        def end_slice(termination="quantum_complete"):
            nonlocal active_slice
            if active_slice is not None:
                record, _end, _deadline, start = active_slice
                record.update(evaluations_used=evaluations - record["start_evaluations"],
                              elapsed_seconds=monotonic() - start)
                if record["termination"] == "running":
                    record["termination"] = termination
            active_slice = None

        archive_storage_cache: list[Any] = [None, 0]

        def archive_storage_estimate_bytes():
            # The archive owns compact designs/summaries, not full spectral/J
            # matrices. Count retained Python object storage without copying it.
            # Values are conservatively counted by occurrence, independent of
            # interning/IPC aliases. Allocator/RSS overhead remains unknown.
            key = (design_archive.total_admitted, len(design_archive.entries),
                   design_archive.audited_champion_hash)
            if archive_storage_cache[0] == key:
                return archive_storage_cache[1]
            def storage(value):
                # Deterministic conservative Python-container model, not live
                # getsizeof/alias accounting (which differs after IPC/replay).
                # Count shared values by occurrence so sharing never shrinks
                # admission differently between serial and process runs.
                if isinstance(value, Mapping):
                    return 192 + 72 * len(value) + sum(storage(k) + storage(v) for k, v in value.items())
                if isinstance(value, (list, tuple, set, frozenset)):
                    return 64 + 8 * len(value) + sum(storage(v) for v in value)
                if isinstance(value, str):
                    return 96 + 4 * len(value)
                if hasattr(value, "__dict__"):
                    return 128 + storage(vars(value))
                return 32
            estimate = storage(design_archive.entries)
            archive_storage_cache[:] = [key, estimate]
            return estimate

        def resource_check(design, *, jacobian=False, epigraph=False, additional_points=0, additional_constraints=0):
            numerical_estimate = _synthesis_working_set_estimate_bytes(request, len(design["front_layers"]),
                jacobian=jacobian, epigraph=epigraph)
            numerical_estimate += (additional_points * len(design["front_layers"]) * 64
                                   + 16 * additional_constraints * (len(design["front_layers"]) + 1))
            archive_estimate = archive_storage_estimate_bytes()
            estimate = numerical_estimate + archive_estimate
            limit = resource_resolution["effective_bytes"]
            details["largest_estimated_working_set_bytes"] = max(estimate,
                details.get("largest_estimated_working_set_bytes", 0))
            if limit is not None and estimate > limit:
                skip = dict(stage=None if active_slice is None else active_slice[0]["stage"],
                    layer_count=len(design["front_layers"]), estimated_bytes=estimate, limit_bytes=limit,
                    numerical_estimate_bytes=numerical_estimate, retained_archive_estimate_bytes=archive_estimate,
                    requested_limit_bytes=request.max_working_set_bytes,
                    resource_policy=resource_resolution["mode"],
                    jacobian=jacobian, full_epigraph=epigraph,
                    reason="working_set_estimate_exceeds_resolved_resource_guard_not_layer_infeasibility")
                details["resource_skips"].append(skip)
                details["resource_limited"] = True
                emit("hybrid_resource_guard", json.dumps(skip, sort_keys=True), design)
                raise SynthesisResourceGuard(skip["reason"])

        def hybrid_evaluate(design, **kwargs):
            stage_check()
            resource_check(design, jacobian=bool(kwargs.get("jacobian")))
            if forward is not None and not kwargs.get("jacobian") and not kwargs.get("zero_ids"):
                kwargs["gpu_forward"] = True
            return evaluate(design, **kwargs)

        def batch_remaining():
            remaining = (batch_quantum if request.max_evaluations is None else
                         max(0, request.max_evaluations - evaluations - reserved_training_slots - 1))
            if active_slice is not None:
                remaining = min(remaining, max(0, active_slice[1] - evaluations - reserved_training_slots
                    - active_slice[0].get("reserved_audit_slots", 0)))
            return min(batch_quantum, remaining)

        def hybrid_batch_evaluate(tasks, *, phase, evaluate_one=None):
            """Reserve then eagerly consume a bounded independent source prefix.

            Batch shape depends on frozen memory/stage limits, not CPU width.
            All physical outcomes precede serial archive/adoption callbacks.
            """
            nonlocal evaluations, reserved_training_slots
            stage_check()
            tasks = list(tasks)[:batch_remaining()]
            if not tasks:
                return []
            points = details["original_grid_points"]
            use_forward = forward is not None and all(not task.get("variable_ids") for task in tasks)
            estimates = []
            retained = []
            for task in tasks:
                jacobian = bool(task.get("variable_ids"))
                design = task["design"]
                resource_check(design, jacobian=jacobian)
                layers = max(1, len(design["front_layers"]) + len(design["back_layers"]))
                estimates.append(_synthesis_working_set_estimate_bytes(request, layers, jacobian=jacobian))
                # Returned arrays are Python float lists and condition records,
                # not only raw doubles. Deliberately conservative project proxy.
                retained.append(points * (320 + 40 * (layers if jacobian else 0)) + 4096 * layers)
            limit = resource_resolution["effective_bytes"]
            archive_estimate = archive_storage_estimate_bytes()
            parent_estimate = max(estimates) + archive_estimate
            while len(tasks) > 1 and limit is not None and parent_estimate + sum(retained[:-1]) > limit:
                tasks.pop()
                estimates.pop()
                retained.pop()
                parent_estimate = max(estimates) + archive_estimate
            base = 128 * 1024**2
            live = len(execution.children) if not execution.closed else 0
            from .film_synthesis_parallel import MAX_FRAME_BYTES
            frame_limited = max(retained) >= MAX_FRAME_BYTES
            width = 1 if use_forward or frame_limited else min(execution.workers, len(tasks))
            peak = parent_estimate + sum(retained) + max(live, width) * base + width * (max(estimates) - base)
            while width > 1 and limit is not None and peak > limit:
                width -= 1
                peak = parent_estimate + sum(retained) + max(live, width) * base + width * (max(estimates) - base)
            if width <= 1:
                # Idle workers still occupy resident baseline memory. Retire
                # this run-owned pool rather than pretend they cost nothing.
                if live and limit is not None and parent_estimate + sum(retained[:-1]) + live * base > limit:
                    execution.close()
                    live = 0
                peak = parent_estimate + sum(retained[:-1]) + live * base
            record = dict(phase=phase, cycle=cycle, reserved_slots=len(tasks), consumed_slots=0,
                dispatched_unused_slots=0, released_unsubmitted_slots=0,
                requested_workers=request.cpu_workers, available_cpu_workers=execution.available_workers,
                resource_worker_cap=width, frozen_budget_bytes=limit,
                full_response_frame_limited=frame_limited, max_frame_bytes=MAX_FRAME_BYTES,
                parent_working_set_estimate_bytes=parent_estimate,
                resident_workers_estimate=max(live, width) if width > 1 else live,
                resident_worker_baseline_estimate_bytes=(max(live, width) if width > 1 else live) * base,
                retained_archive_estimate_bytes=archive_estimate,
                archive_storage_scope="deterministic compact archive Python-container proxy; shared values counted per occurrence; not allocator overhead or RSS",
                source_prefix_batch_quantum=batch_quantum,
                retained_result_estimate_bytes=sum(retained),
                estimated_peak_bytes=peak, estimate_is_os_quota=False,
                memory_policy="parent + retained Python responses + resident worker baselines + active worker numerical worksets",
                stage=None if active_slice is None else active_slice[0]["stage"])
            details["parallel_batches"].append(record)
            reserved_training_slots += len(tasks)
            token_start = execution.next_token
            record["executor_decision_index"] = len(execution.batch_decisions)
            values: list[Mapping[str, Any]] = []
            remaining_reserved = len(tasks)
            consumed_before = evaluations
            try:
                work = sum(points * max(1, len(task["design"]["front_layers"])) for task in tasks)
                prepared_tasks = [EvaluationTask(task["design"], bool(task.get("variable_ids")),
                    tuple(task.get("zero_ids", ())), context=task, retain_responses=True) for task in tasks]
                with execution.batch(prepared_tasks, count=len(tasks), work=work, budget_allows=True,
                                     phase=phase, worker_limit=width) as ordered:
                    for task, outcome in ordered:
                        if outcome is None and values and active_slice is not None:
                            deadline = active_slice[2]
                            if deadline is not None and monotonic() >= deadline:
                                active_slice[0]["termination"] = "stage_budget"
                                break
                        before = evaluations
                        try:
                            if evaluate_one is None:
                                value = evaluate(task.design, jacobian=task.jacobian, zero_ids=task.zero_ids,
                                                 outcome=outcome, gpu_forward=use_forward, _reserved=True)
                            else:
                                value = evaluate_one(task.design, variable_ids=tuple(
                                    row["id"] for row in task.design["front_layers"]) if task.jacobian else (),
                                    zero_ids=task.zero_ids, outcome=outcome, _reserved=True)
                        finally:
                            remaining_reserved -= evaluations - before
                        values.append(value)
            finally:
                unused = sum(not outcome.consumed for token, outcome in execution.outcomes.items()
                             if token >= token_start and token in execution.submitted)
                charge = min(remaining_reserved, unused)
                evaluations += charge
                reserved_training_slots -= remaining_reserved
                record.update(consumed_slots=evaluations - consumed_before - charge,
                    dispatched_unused_slots=charge,
                    released_unsubmitted_slots=remaining_reserved - charge,
                    completed_results=len(values),
                    submitted_tokens=[token for token in execution.outcomes if token >= token_start],
                    backend=("forward_" + str(forward.actual_backend if forward is not None else "unknown") if use_forward else
                             "headless_process" if execution.next_token > token_start else "serial"),
                    worker_pids=sorted({outcome.worker_pid for token, outcome in execution.outcomes.items()
                        if token >= token_start and outcome.worker_pid is not None}))
                record["effective_workers"] = (len(record["worker_pids"]) or
                    (1 if record["consumed_slots"] else 0))
                if not record["submitted_tokens"] and not record["consumed_slots"]:
                    record["backend"] = "not_dispatched"
                    decision = execution.batch_decisions[record["executor_decision_index"]]
                    record["startup_state"] = decision.get("startup_state", "no_dispatch")
                    record["owned_worker_pids"] = decision.get("owned_worker_pids", [])
            return values

        def local_progress(stage, value, metadata):
            emit(stage, json.dumps(metadata, sort_keys=True), value["design"], value["score"])

        def record_objective_handoff(local, value):
            nonlocal objective_handoff_cycle
            if request.objective_progress_policy is None:
                return False
            if "objective_progress" in local:
                search_diagnostics["local_refinement"].append(local)
            if local["termination"] != "objective_progress_handoff":
                return False
            # No cleanup or deletion may replace this exact accepted design.
            decision = local["objective_progress"]["decision"]
            if decision is None or decision["trigger"]["physical_hash"] != physical_design_hash(value["design"]):
                raise ValueError("objective handoff does not match the committed accepted design")
            check_training_admission()  # Global cancellation/limits win.
            objective_handoff_cycle = cycle
            metadata = dict(cycle=cycle, **deepcopy(decision))
            details.setdefault("objective_progress_handoffs", []).append(metadata)
            emit("objective_progress_handoff", json.dumps(metadata, sort_keys=True), value["design"], value["score"])
            end_slice("objective_progress_handoff")
            return True

        def reduce_audited_thickness():
            """One optional in-budget cost slice after normal hybrid success.

            Never called from terminal cleanup or failure/cancel paths. A
            reduced training champion cannot replace the independent audited
            incumbent unless its own original audit also passes.
            """
            if not request.enable_thickness_reduction:
                return
            record: dict[str, Any] = dict(enabled=True, status="not_started",
                policy="single audited incumbent; <=32 shared physical candidates, <=16 SLSQP iterations; original independent audit",
                initial_cost_sublevel="audited incumbent total; not a user manufacturing cap",
                independent_audit_performed=False, promoted=False)
            details["thickness_reduction"] = record
            if not targets_verified() or audited_best is None:
                record["status"] = "skipped_no_audited_nominal_incumbent"
                return
            source = audited_best[0]
            if not source["design"]["front_layers"]:
                record["status"] = "skipped_empty_topology"
                return
            source_hash = physical_design_hash(source["design"])
            source_total = sum(row["thickness"]["value"] for row in source["design"]["front_layers"])
            record.update(initial_physical_hash=source_hash, initial_total_thickness_nm=source_total)
            # Skip optional work without turning existing success into a budget
            # error. Cancellation and wall deadline still remain authoritative.
            _check_cancel(cancel_event)
            if request.max_evaluations is not None and evaluations + reserved_training_slots >= request.max_evaluations - 1:
                record["status"] = "skipped_evaluation_budget"
                return
            from .film_synthesis_thickness import (reduce_total_thickness,
                                                   thickness_reduction_working_set_bytes)
            reduced = source
            before = evaluations

            def admit(design, **_kwargs):
                stage_check()
                numerical = thickness_reduction_working_set_bytes(request, len(design["front_layers"]))
                retained = archive_storage_estimate_bytes()
                resident = (0 if execution.closed else len(execution.children)) * 128 * 1024**2
                estimate = numerical + retained + resident
                limit = resource_resolution["effective_bytes"]
                record.update(estimated_peak_bytes=estimate, numerical_estimate_bytes=numerical,
                    retained_archive_estimate_bytes=retained, resident_worker_baseline_estimate_bytes=resident,
                    memory_policy="one parent TMM/Jacobian+dense SLSQP workset + retained archive + idle worker baseline; no duplicate numerical arrays",
                    effective_limit_bytes=limit)
                details["largest_estimated_working_set_bytes"] = max(estimate,
                    details.get("largest_estimated_working_set_bytes", 0))
                if limit is not None and estimate > limit:
                    details["resource_limited"] = True
                    details["resource_skips"].append(dict(stage="nominal_thickness_reduction",
                        estimated_bytes=estimate, limit_bytes=limit, layer_count=len(design["front_layers"]),
                        reason="combined_thickness_solver_archive_worker_estimate_exceeds_resource_guard"))
                    raise SynthesisResourceGuard("combined thickness solver/archive/worker estimate exceeds resource guard")

            def retain(value, *, audit_eligible=False):
                nonlocal reduced
                archive(value, audit_eligible=False, parent_hash=source_hash,
                        operator="nominal_total_thickness_reduction")
                candidate_total = sum(row["thickness"]["value"] for row in value["design"]["front_layers"])
                reduced_total = sum(row["thickness"]["value"] for row in reduced["design"]["front_layers"])
                if nominal_targets_pass(request, value["score"]) and candidate_total < reduced_total:
                    reduced = value

            try:
                begin_slice("nominal_thickness_reduction", .15, 32)
                admit(source["design"])
                reduced, numerical_details = reduce_total_thickness(request, source,
                    evaluate=hybrid_evaluate, archive=retain, cancel_check=stage_check,
                    resource_check=admit, progress=local_progress, max_iterations=16)
                record.update(status="complete", numerical=numerical_details)
            except SynthesisStageBudget:
                record["status"] = "partial_stage_budget"
            except _Stopped as exc:
                if str(exc) != "evaluation_limit":
                    raise
                record["status"] = "partial_evaluation_budget"
            except SynthesisResourceGuard:
                record["status"] = "skipped_resource_guard"
            finally:
                end_slice(record["status"])
                record["training_evaluations_used"] = evaluations - before
            _check_cancel(cancel_event)
            reduced_total = sum(row["thickness"]["value"] for row in reduced["design"]["front_layers"])
            record.update(proposed_total_thickness_nm=reduced_total,
                proposed_physical_hash=physical_design_hash(reduced["design"]))
            if reduced_total < source_total:
                # The training slice preserves the global audit slot. Parent
                # auditing owns final acceptance and retains the old incumbent
                # when dense means or off-grid extrema reject this proposal.
                audit_archive(reduced, allow_feedback_repair=False)
                record["independent_audit_performed"] = True
                record["promoted"] = (audited_best is not None
                    and physical_design_hash(audited_best[0]["design"]) == physical_design_hash(reduced["design"]))
                record["audit_outcome"] = "promoted" if record["promoted"] else "rejected_incumbent_retained"
            record["evaluations_used_including_audit"] = evaluations - before
            emit("thickness_reduction_complete", json.dumps(record, sort_keys=True),
                 audited_best[0]["design"], audited_best[0]["score"])

        def score_sse(value):
            return weighted_sse_loss_gradient(request, value, ())[0]

        def same_spectrum(first, second):
            a = {(r["objective_index"], r["angle_deg"]): r for r in first.get("responses", ())}
            b = {(r["objective_index"], r["angle_deg"]): r for r in second.get("responses", ())}
            return (bool(a) and a.keys() == b.keys() and all(
                np.array_equal(a[key]["wavelength_nm"], b[key]["wavelength_nm"])
                and np.allclose(a[key]["actual_values"], b[key]["actual_values"], atol=2e-11, rtol=2e-10)
                for key in a))

        def physical_cleanup(value, *, cleaned=None):
            if cleaned is None:
                cleaned = cleanup_batch_needle_design(value["design"], frozen_snapshot)
            if cleaned.design == value["design"] or not feasible(cleaned.design):
                return value
            current = value
            if not current.get("responses"):
                zero_ids = tuple(row["id"] for row in current["design"]["front_layers"]
                                 if row["thickness"]["value"] == 0.)
                current = hybrid_evaluate(current["design"], zero_ids=zero_ids)
            candidate = hybrid_evaluate(cleaned.design)
            if not same_spectrum(current, candidate):
                raise ValueError("exact-zero/same-optical-reference cleanup failed full-original spectral equivalence")
            metadata = dict(family="compression", removed_exact_zero_ids=list(cleaned.removed_exact_zero_ids),
                            merged_ids=dict(cleaned.merged_ids), full_original_spectral_equivalence=True)
            archive(candidate, audit_eligible=False, provenance=metadata,
                    parent_hash=physical_design_hash(value["design"]), operator="exact_zero_same_reference_merge")
            emit("hybrid_cleanup", json.dumps(metadata, sort_keys=True), candidate["design"], candidate["score"])
            return candidate

        def cleanup_champion(*, terminal=False):
            # The explorer may still be an older endpoint when a growth slice
            # expires. Compress the independently retained real champion too,
            # without replacing that endpoint with the champion's topology.
            assert best is not None
            value = best
            cleaned = cleanup_batch_needle_design(value["design"], frozen_snapshot)
            record = dict(source_physical_hash=physical_design_hash(value["design"]),
                source_layer_count=len(value["design"]["front_layers"]),
                proposed_layer_count=len(cleaned.design["front_layers"]),
                terminal=terminal, independent_audit_performed=False,
                promoted_to_strict_champion=False, evaluations_used=0)
            if cleaned.design == value["design"]:
                if terminal:
                    details["terminal_champion_cleanup"] = dict(record, status="not_needed")
                return value
            record["status"] = "pending"
            details.setdefault("champion_cleanup", []).append(record)
            if terminal:
                details["terminal_champion_cleanup"] = record
            if not feasible(cleaned.design):
                record["status"] = "skipped_physical_constraints"
                return value
            before = evaluations
            started_slice = False
            try:
                if terminal:
                    # Only a real, feasible topology change admits work. The
                    # ordinary training guard still reserves the audit slot.
                    begin_slice("terminal_champion_cleanup", 1., 2)
                    started_slice = True
                result = physical_cleanup(value, cleaned=cleaned)
                record.update(status="complete", full_original_spectral_equivalence=True,
                    result_physical_hash=physical_design_hash(result["design"]),
                    promoted_to_strict_champion=best["design"] == result["design"],
                    selected_physical_hash=physical_design_hash(best["design"]))
                return result
            except _Stopped as exc:
                record.update(status="skipped_" + str(exc))
                if not terminal:
                    raise
                return value
            except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                record["status"] = ("skipped_resource_guard" if isinstance(exc, SynthesisResourceGuard)
                                    else "skipped_stage_budget")
                if not terminal:
                    raise
                return value
            except _StructureCancelled:
                record["status"] = "interrupted_cancel_or_time_limit"
                raise
            except (ValueError, ArithmeticError, RuntimeError):
                record["status"] = "failed"
                raise
            finally:
                record["evaluations_used"] = evaluations - before
                if started_slice:
                    end_slice(record["status"])

        def finish_champion_cleanup():
            if objective_handoff_cycle == cycle:
                # The trigger commits an exact topology for this entire cycle,
                # including the shared normal-exit finalization path.
                return
            cleanup_champion(terminal=True)

        terminal_hybrid_cleanup = finish_champion_cleanup

        def report(event):
            search_diagnostics["structural_proposals"].append(deepcopy(event))

        def simple_priors():
            fractions = request.growth_quarter_wave_fractions
            if automatic_cluster_route and 1.0 in fractions:
                # Preserve the cheap homogeneous AR prior before other allowed
                # phase fractions, even with only one remaining training slot.
                fractions = (1.0, *(fraction for fraction in fractions if fraction != 1.0))
            for index, ref in enumerate(request.candidate_materials):
                for fraction in fractions:
                    thickness = legal_growth_thickness(request.environment,
                        quarter[(ref.material_id, ref.optical_set_id)] * fraction)
                    if thickness is None:
                        continue
                    ids = _proposal_layer_ids(request.environment, f"hybrid-simple-{index}", "unused",
                                               reserved_ids=reserved_layer_ids)
                    design = _insert_at_site(request.environment, dict(kind="interface", interface=0), ref, thickness, *ids)
                    if feasible(design):
                        yield StructuralProposal(design, dict(family="homogeneous", quarter_fraction=fraction))

        def multispectral_priors():
            source = iter_multispectral_hr_proposals(source_request,
                cancel_check=check_training_admission, report=report)
            hybrid_streams.append(source)
            # Look ahead without physics to interleave actual estimated-demand
            # scales, rather than spend the entire cold budget in the first
            # under-demand tier. No fixed 32/70/104-layer search center.
            while True:
                chunk = []
                for _ in range(32):
                    try:
                        chunk.append(next(source))
                    except StopIteration:
                        break
                if not chunk:
                    return
                buckets: dict[float, list[StructuralProposal]] = {}
                for proposal in chunk:
                    meta = proposal.provenance
                    ratio = meta["candidate_pair_budget"] / meta["estimated_pair_demand"]
                    buckets.setdefault(round(ratio, 8), []).append(proposal)
                tiers = sorted(buckets, key=lambda ratio: (abs(math.log(max(ratio, 1e-15))), ratio))
                while any(buckets.values()):
                    for tier in tiers:
                        if not buckets[tier]:
                            continue
                        proposal = buckets[tier].pop(0)
                        yield proposal
                        # Non-QW terminal impedance profiles are additional real
                        # physical families, not assumed optical improvements.
                        if proposal.provenance.get("matching_end_layers"):
                            continue
                        for amplitude in (.0, .12):
                            design = deepcopy(proposal.design)
                            rows = design["front_layers"]
                            for pair_index in range(len(rows) // 2):
                                a, b = rows[2 * pair_index:2 * pair_index + 2]
                                if (a["material_id"], a["optical_set_id"]) == (b["material_id"], b["optical_set_id"]):
                                    continue
                                z = pair_index / max(1, len(rows) // 2 - 1)
                                high = tuple(proposal.provenance["windows"][0]["high"])
                                duty = (.8 if z < .10 else .05 if z > .90 else
                                        .5 + amplitude * math.sin(2 * math.pi * z))
                                for row in (a, b):
                                    fraction = duty if (row["material_id"], row["optical_set_id"]) == high else 1 - duty
                                    row["thickness"]["value"] *= 2 * fraction
                            if feasible(design):
                                meta = dict(proposal.provenance, family="multispectral_matched_profile",
                                    duty_amplitude=amplitude, substrate_high_duty=.8, incident_high_duty=.05,
                                    rule="project terminal impedance/non-QW phase profile; full original scoring required")
                                yield StructuralProposal(design, meta)

        def seed_priors():
            for index, design in enumerate(request.seed_designs):
                yield StructuralProposal(deepcopy(dict(design)), dict(family="user_seed", seed_index=index))

        streams = [iter(seed_priors()), iter(simple_priors()), iter(multispectral_priors()),
            iter(iter_reflector_proposals(source_request, cancel_check=check_training_admission, report=report)),
            iter(iter_matching_proposals(source_request, cancel_check=check_training_admission, report=report)),
            iter(iter_ar_cluster_proposals(source_request, cancel_check=check_training_admission, report=report)),
            iter(iter_bandpass_proposals(source_request, cancel_check=check_training_admission, report=report)),
            iter(iter_dual_substack_hr_proposals(source_request, cancel_check=check_training_admission, report=report))]
        hybrid_streams.extend(streams)
        source_cursor = 0
        repair_attempted = set()

        def repair_near_feasible(trigger_boundary="initial_champion"):
            """Resolve audited boundary failures before another topology rotation."""
            if targets_verified() or best is None:
                return
            source = best
            if not source["design"]["front_layers"]:
                return
            from .film_synthesis_feasibility import (
                PreparedFeasibilityEvaluator, refine_feasibility_candidate,
                interior_guides, MAX_REPAIR_ROUNDS, MAX_REPAIR_EVALUATIONS,
            )
            _guides, margins = interior_guides(request.objectives)
            thresholds = {}
            for row in margins:
                objective = request.objectives[row["objective_index"]]
                constraint = getattr(objective, row["reduction"])
                bound_ulps = [8 * math.ulp(float(bound)) for bound in (
                    constraint.lower_bound, constraint.upper_bound, constraint.target_value) if bound is not None]
                thresholds[row["objective_index"], row["reduction"]] = max(
                    row["lower_margin"], row["upper_margin"], *bound_ulps) / objective.normalization_scale
            # A loose user solver tolerance must not prematurely end ordinary
            # topology search. Every active condition must be near its own
            # interval/ULP scale; no wide band can hide a narrow group's miss.
            if not source["score"].get("errors") or any(
                    e.normalized_violation > thresholds[e.objective_index, e.reduction]
                    for e in source["score"]["errors"]):
                return
            input_key = hashlib.sha256(json.dumps(dict(
                physical_hash=physical_design_hash(source["design"]), margins=margins),
                sort_keys=True).encode()).hexdigest()
            # Frozen inputs and a physical candidate determine its audit.
            # Deduplicate BEFORE a potentially expensive cache refresh, without
            # retaining an unbounded cache of full spectra/Jacobians.
            if input_key in repair_attempted:
                details["feasibility_repair_deduplicated_checks"] = (
                    details.get("feasibility_repair_deduplicated_checks", 0) + 1)
                return
            # Strict equality may be solver-unacceptable despite zero residual;
            # audit the near-feasible champion without that legacy audit gate.
            audited = audit_archive(source, allow_feedback_repair=False)
            if audited is None:
                audited = audit_archive(source, allow_feedback_repair=False, refresh=True)
            if targets_verified():
                return
            record = dict(status="running", rounds=[], fixed_topology=True,
                trigger_evaluations=evaluations, trigger_cycle=cycle, trigger_boundary=trigger_boundary,
                initial_physical_hash=physical_design_hash(source["design"]),
                initial_total_thickness_nm=sum(r["thickness"]["value"] for r in source["design"]["front_layers"]),
                margin_semantics="private 2e-4 of common feasible interval; inclusive 0/1/value/empty interiors unchanged",
                margins=margins, maximum_rounds=MAX_REPAIR_ROUNDS,
                trigger_scale="per enabled criterion interior displacement / ULP; not solver tolerance",
                maximum_shared_evaluations=MAX_REPAIR_EVALUATIONS,
                formal_target_changed=False, original_rms_measure_unchanged=True)
            attempt_key = hashlib.sha256(json.dumps(dict(
                physical_hash=record["initial_physical_hash"], margins=margins,
                failed_conditions=[(e.objective_index, e.angle_deg, e.reduction, e.wavelength_nm)
                                   for e in audited["score"]["errors"]
                                   if e.normalized_violation > 0 or (
                                       e.strict_lower and e.actual <= e.lower_bound) or (
                                       e.strict_upper and e.actual >= e.upper_bound)]),
                sort_keys=True).encode()).hexdigest()
            record["attempt_key"] = attempt_key
            record["preaudit_attempt_key"] = input_key
            repair_attempted.add(input_key)
            details["feasibility_repair"] = record
            details.setdefault("feasibility_repair_attempts", []).append(record)
            emit("feasibility_repair_start", json.dumps(record, sort_keys=True), source["design"], source["score"])
            before = evaluations
            before_validation = validation_evaluations
            before_audit_calls, before_audit_points = audit_physical_evaluations, audit_wavelength_points
            stop = "feasibility_repair_round_limit"
            try:
                begin_slice("audited_feasibility_repair", .85, MAX_REPAIR_EVALUATIONS)
                previous_evaluator = None
                for round_number in range(1, MAX_REPAIR_ROUNDS + 1):
                    stage_check()
                    evaluator = PreparedFeasibilityEvaluator(request, audited, previous=previous_evaluator)
                    def repair_evaluate(design, _evaluator=evaluator):
                        stage_check()
                        resource_check(design, jacobian=True, epigraph=True,
                            additional_points=_evaluator.additional_points,
                            additional_constraints=2 * _evaluator.additional_conditions)
                        return evaluate(design, jacobian=True, feasibility_evaluator=_evaluator)
                    initial = repair_evaluate(source["design"])
                    candidate, local = refine_feasibility_candidate(request, initial,
                        evaluate=repair_evaluate, archive=archive, cancel_check=stage_check,
                        progress=local_progress)
                    archive(candidate, audit_eligible=False, operator="audited_feasibility_repair")
                    stage_check()
                    next_audit = audit_archive(candidate, allow_feedback_repair=False)
                    row = dict(round=round_number, local=local,
                        additional_points=evaluator.additional_points,
                        training_max_error=candidate["score"]["max_error"],
                        audit_max_error=None if next_audit is None else next_audit["score"]["max_error"],
                        layer_count=len(candidate["design"]["front_layers"]),
                        total_thickness_nm=sum(r["thickness"]["value"] for r in candidate["design"]["front_layers"]),
                        shared_evaluations_used=evaluations - before)
                    record["rounds"].append(row)
                    emit("feasibility_repair_round", json.dumps(row, sort_keys=True), candidate["design"], candidate["score"])
                    if targets_verified():
                        record["status"] = "original_training_and_independent_audit_pass"
                        return
                    if next_audit is None:
                        stop = "feasibility_repair_cached_audit_unavailable"
                        break
                    old_points = (evaluator.points, evaluator.mean_grids)
                    following = PreparedFeasibilityEvaluator(request, next_audit, previous=evaluator)
                    if old_points == (following.points, following.mean_grids):
                        stop = "feasibility_repair_stalled"
                        break
                    source, audited, previous_evaluator = candidate, next_audit, evaluator
                record["status"] = stop
                # A finite fixed-topology failure is not global infeasibility.
                # The attempt key prevents repetition; ordinary search and its
                # existing independent budgets/stopping conditions continue.
                return
            except _StructureCancelled:
                record["status"] = "cancelled_or_deadline"
                raise
            except _Stopped as exc:
                if record["status"] == "running":
                    record["status"] = str(exc)
                raise
            except SynthesisStageBudget:
                record["status"] = "feasibility_repair_stage_budget"
                return
            except SynthesisResourceGuard:
                record["status"] = "resource_guard"
                return
            except (ValueError, ArithmeticError, RuntimeError) as exc:
                record.update(status="feasibility_repair_unavailable", error=str(exc))
                return
            finally:
                record["shared_evaluations_used"] = evaluations - before
                record["training_evaluations_used"] = evaluations - before - (validation_evaluations - before_validation)
                record["audit_evaluations_used"] = validation_evaluations - before_validation
                record["audit_physical_evaluations"] = audit_physical_evaluations - before_audit_calls
                record["audit_wavelength_points"] = audit_wavelength_points - before_audit_points
                end_slice(record["status"])

        repair_near_feasible()
        while not targets_verified():
            check_training_admission()
            if request.max_cycles is not None and cycle >= request.max_cycles:
                raise _Stopped("cycle_limit")
            cycle += 1
            emit("hybrid_rotation", "结构族广筛、共享多样档案、局部/全局、真实解析批针、压缩及原全约束复核", explorer["design"], explorer["score"])
            begin_slice("engineering_prior_screen", .25, 32)
            try:
                misses = 0
                while streams and misses < len(streams) and not targets_verified():
                    stage_check()
                    prior_proposals: list[StructuralProposal] = []
                    while (streams and len(prior_proposals) < batch_remaining() and misses < len(streams)
                           and (not request.randomize_template_search or not prior_proposals)):
                        stage_check()
                        source_cursor %= len(streams)
                        source = streams[source_cursor]
                        try:
                            proposal = next(source)
                        except StopIteration:
                            streams.pop(source_cursor)
                            continue
                        source_cursor += 1
                        signature = physical_design_hash(proposal.design)
                        if signature in seen_priors:
                            misses += 1
                            continue
                        misses = 0
                        seen_priors.add(signature)
                        if feasible(proposal.design):
                            try:
                                resource_check(proposal.design)
                            except SynthesisResourceGuard:
                                # One oversized family member cannot discard
                                # feasible source-prefix work already gathered.
                                if prior_proposals:
                                    break
                                continue
                            prior_proposals.append(proposal)
                    if not prior_proposals:
                        break
                    if request.randomize_template_search:
                        # Reserve a BASE/extra pair independently of execution
                        # width. Even frozen batch quantum=1 can consume the
                        # two reserved starts in source order, in two batches.
                        assert active_slice is not None
                        slots = active_slice[1] - evaluations - reserved_training_slots - active_slice[0].get("reserved_audit_slots", 0)
                        if request.max_evaluations is not None:
                            slots = min(slots, request.max_evaluations - evaluations - reserved_training_slots - 1)
                        extra = extra_template_start(prior_proposals[0], stage="hybrid_template",
                            candidate_slots=slots, cancel_check=stage_check, resource_check=resource_check)
                        if extra is not None:
                            prior_proposals.append(extra)
                        screened = []
                        for prior in prior_proposals:
                            # Commit each complete physical result immediately;
                            # cancellation during extra work retains its BASE.
                            values = hybrid_batch_evaluate([dict(design=prior.design)], phase="hybrid_prior")
                            if not values:
                                break
                            raw = values[0]
                            archive(raw, audit_eligible=False, provenance=prior.provenance, operator="engineering_prior")
                            emit("structure_proposal", json.dumps(prior.provenance, sort_keys=True), raw["design"], raw["score"])
                            if (not explorer["design"]["front_layers"] or score_sse(raw) < score_sse(explorer)):
                                explorer = raw
                            screened.append(raw)
                        # Pair slots cannot be consumed by a BASE audit. Once
                        # both starts are scored, use the unchanged audit rule.
                        for raw in screened:
                            if (raw["score"]["max_error"] <= request.acceptable_normalized_error
                                    and solver_acceptable(request, raw["score"])):
                                if (evaluations < active_slice[1] and
                                        (active_slice[2] is None or monotonic() < active_slice[2])):
                                    audit_archive(raw, allow_feedback_repair=False)
                                else:
                                    details["deferred_prior_audits"] = details.get("deferred_prior_audits", 0) + 1
                            if targets_verified():
                                break
                        continue
                    values = None  # Release the preceding batch before new physical work.
                    values = hybrid_batch_evaluate([dict(design=p.design) for p in prior_proposals], phase="hybrid_prior")
                    # A resource-limited batch can return a prefix. Preserve
                    # unscored priors for a later turn, without old-code replay.
                    if len(values) < len(prior_proposals):
                        for pending_proposal in prior_proposals[len(values):]:
                            seen_priors.discard(physical_design_hash(pending_proposal.design))
                        pending = iter(prior_proposals[len(values):])
                        streams.insert(source_cursor % max(1, len(streams)), pending)
                    for adopted_index, (proposal, raw) in enumerate(zip(prior_proposals, values, strict=False)):
                        archive(raw, audit_eligible=False, provenance=proposal.provenance, operator="engineering_prior")
                        if (raw["score"]["max_error"] <= request.acceptable_normalized_error
                                and solver_acceptable(request, raw["score"])):
                            assert active_slice is not None
                            if (evaluations < active_slice[1] and
                                    (active_slice[2] is None or monotonic() < active_slice[2])):
                                audit_archive(raw, allow_feedback_repair=False)
                            else:
                                details["deferred_prior_audits"] = details.get("deferred_prior_audits", 0) + 1
                        emit("structure_proposal", json.dumps(proposal.provenance, sort_keys=True), raw["design"], raw["score"])
                        if (not explorer["design"]["front_layers"] or score_sse(raw) < score_sse(explorer)):
                            explorer = raw
                        if targets_verified():
                            # Eager work is already counted. Do not adopt later
                            # results after the source-ordered successful audit.
                            batch_record = details["parallel_batches"][-1]
                            batch_record["completed_unadopted_results"] = len(values) - adopted_index - 1
                            for token in batch_record["submitted_tokens"][adopted_index + 1:]:
                                execution.outcomes[token].discarded = True
                            execution.batch_decisions[batch_record["executor_decision_index"]]["discarded_tasks"] = sum(
                                execution.outcomes[token].discarded for token in batch_record["submitted_tokens"])
                            break
            except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                end_slice("resource_guard" if isinstance(exc, SynthesisResourceGuard) else "stage_budget")
            finally:
                end_slice()
            repair_near_feasible("engineering_prior_screen")
            if targets_verified():
                break

            # Low-dimensional global modes preserve real topologies and explore
            # broad physical thickness basins; specialists get rotating turns.
            begin_slice("local_global_exploration", .15, 24)
            try:
                parents = design_archive.select_parents(3)
                for entry in parents:
                    stage_check()
                    if not entry.design["front_layers"]:
                        continue
                    archive_context.update(provenance=entry.provenance, parent_hash=entry.physical_hash,
                                           operator="low_dimensional_global")
                    n = len(entry.design["front_layers"])
                    dimension = min(8, n)
                    coordinates = np.linspace(0., 1., n)
                    basis = np.column_stack([np.ones(n)] + [np.cos(math.pi * k * coordinates)
                                                for k in range(1, dimension)])
                    evolution = DifferentialEvolutionProposals(np.zeros(dimension),
                        np.full(dimension, -.35), np.full(dimension, .35),
                        seed=hybrid_structure_seed + cycle + entry.insertion_index, population_size=6)
                    for _generation in range(2):
                        losses = np.full(len(evolution.population), np.inf)
                        global_proposals: list[tuple[int, Any]] = []
                        for index, point in enumerate(evolution.proposals()):
                            stage_check()
                            trial = deepcopy(entry.design)
                            for row, delta in zip(trial["front_layers"], basis @ point, strict=True):
                                row["thickness"]["value"] *= math.exp(float(delta))
                            if feasible(trial):
                                try:
                                    resource_check(trial)
                                except SynthesisResourceGuard:
                                    continue
                                global_proposals.append((index, trial))
                        cursor = 0
                        while cursor < len(global_proposals):
                            stage_check()
                            batch = global_proposals[cursor:cursor + batch_remaining()]
                            values = None  # Do not retain the preceding full responses.
                            values = hybrid_batch_evaluate([dict(design=trial) for _, trial in batch], phase="hybrid_global")
                            if not values:
                                break
                            for (index, trial), value in zip(batch, values, strict=False):
                                losses[index] = score_sse(value)
                                archive(value, audit_eligible=False)
                                if losses[index] < score_sse(explorer):
                                    explorer = value
                                emit("hybrid_global_trial", "有界低维厚度群体探索，原网格完整评分", trial, value["score"])
                            cursor += len(values)
                        evolution.accept(losses)
                if request.needle_pre_local and explorer["design"]["front_layers"]:
                    explorer, local = refine_synthesis_explorer(request, explorer,
                        evaluate=hybrid_evaluate, archive=archive, cancel_check=stage_check,
                        progress=local_progress, max_iterations=24)
                    search_diagnostics["local_refinement"].append(local)
            except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                end_slice("resource_guard" if isinstance(exc, SynthesisResourceGuard) else "stage_budget")
            finally:
                end_slice()

            repair_near_feasible("local_global_exploration")
            if targets_verified():
                break
            parent_hash = physical_design_hash(explorer["design"])
            archive_context.update(parent_hash=parent_hash, operator="analytic_batch_needle",
                                   provenance=dict(family="needle_exploration"))
            begin_slice("analytic_needle_scan", .25, 256)
            emit("needle_scan", "原全网格解析针扫描，已为有限生长和终末复核预留预算", explorer["design"], explorer["score"])
            scan = None
            try:
                assert active_slice is not None
                record = active_slice[0]
                probe_quota = record["admitted_evaluation_quantum"]
                def scan_stop_requested():
                    # Whole-request limits/cancellation still propagate. Only
                    # this scheduling slice ends gracefully with its shortlist.
                    check_training_admission()
                    assert active_slice is not None
                    record, end_evaluations, deadline, _start = active_slice
                    stopped = evaluations >= end_evaluations or (deadline is not None and monotonic() >= deadline)
                    if stopped:
                        record["termination"] = "stage_budget"
                    return stopped
                def resource_admit(design):
                    try:
                        resource_check(design, jacobian=True)
                        return True
                    except SynthesisResourceGuard:
                        return False
                def probe_evaluate(design, *, variable_ids=(), zero_ids=(), outcome=None, _reserved=False):
                    # A probe admitted just before a local deadline may finish;
                    # do not throw away earlier probes at a second slice check.
                    if not _reserved:
                        check_training_admission()
                        resource_check(design, jacobian=bool(variable_ids))
                    return evaluate(design, jacobian=bool(variable_ids), zero_ids=zero_ids,
                                    outcome=outcome, _reserved=_reserved)
                def probe_evaluate_batch(tasks, *, evaluate):
                    if scan_stop_requested():
                        return []
                    try:
                        return hybrid_batch_evaluate(tasks, phase="hybrid_needle", evaluate_one=evaluate)
                    except SynthesisStageBudget:
                        return []
                def on_probe(proposal, explorer=explorer):
                    emit("needle_probe", "P=d(显式等窗/用户权重SSE)/dd，d=0；不作为最坏点验收", explorer["design"],
                        material_id=proposal.reference.material_id,
                        position_nm=proposal.site["position_nm"], sensitivity=proposal.sensitivity)
                scan = scan_batch_needles(explorer["design"], frozen_snapshot, request.candidate_materials,
                    evaluate=probe_evaluate, position_samples=request.position_samples + 2 * min(2, no_negative_rounds),
                    valley_refinement_rounds=request.needle_position_refinement_rounds,
                    max_needles=request.needle_batch_size, max_layers=request.max_layers,
                    seed=hybrid_structure_seed + cycle, max_probes=probe_quota, genuine_only=True,
                    gradient=lambda value, ids: weighted_sse_loss_gradient(request, value, ids)[1],
                    on_probe=on_probe, stop_check=scan_stop_requested, admit=resource_admit,
                    evaluate_batch=probe_evaluate_batch, probe_batch_size=batch_quantum)
                details["needle_scans"].append(dict(cycle=cycle, parent_hash=parent_hash,
                    probes=scan.probe_count, refined_probes=scan.refined_probe_count,
                    selected=len(scan.selected), complete=scan.complete, stop_reason=scan.stop_reason,
                    skipped=scan.skipped_probe_count, topology_equivalent_skips=scan.topology_equivalent_skip_count))
                no_negative_rounds = no_negative_rounds + 1 if not scan.selected and scan.complete else 0
                end_slice(scan.stop_reason)
            except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                details["needle_scans"].append(dict(cycle=cycle, parent_hash=parent_hash,
                    complete=False, selected=0, stop_reason="resource_guard" if isinstance(exc, SynthesisResourceGuard) else "stage_budget"))
                end_slice(details["needle_scans"][-1]["stop_reason"])
            finally:
                end_slice()

            begin_slice("joint_needle_sse_growth", .45, 96)
            inserted = False
            objective_handoff_requested = False
            try:
                if scan is not None and scan.selected:
                    proposals = list(scan.selected)
                    thickness = max(request.minimum_nonzero_thickness_nm or 0., min(
                        quarter[(p.reference.material_id, p.reference.optical_set_id)] * request.needle_seed_fraction
                        for p in proposals))
                    while proposals:
                        build = build_batch_needle_design(explorer["design"], frozen_snapshot, proposals,
                                                         seed_thickness_nm=thickness)
                        if feasible(build.design):
                            break
                        proposals.pop()
                    if proposals:
                        value = hybrid_evaluate(build.design, jacobian=True)
                        archive(value, audit_eligible=False)
                        explorer, local = refine_synthesis_explorer(request, value,
                            evaluate=hybrid_evaluate, archive=archive, cancel_check=stage_check,
                            progress=local_progress, max_iterations=64,
                            **({} if request.objective_progress_policy is None else
                               {"progress_policy": request.objective_progress_policy}))
                        objective_handoff_requested = record_objective_handoff(local, explorer)
                        inserted = True
                        total_real_growth += 1
                        details["last_batch"] = dict(cycle=cycle, needle_ids=list(build.needle_ids),
                            original_parent_hash=parent_hash, independent_layer_ids=list(build.layer_ids),
                            raw_layer_count=len(build.layer_ids), anchors=deepcopy(build.anchors),
                            optimizer=local)
                        emit("needle_accepted", "真实批针联合厚度优化终态继续；严格最坏点冠军独立保留", explorer["design"], explorer["score"])
                if not inserted:
                    # Bare-interface power derivatives may vanish. Finite legal
                    # optical-thickness starts remain genuine scored branches.
                    design = explorer["design"]
                    grown = None
                    combinations = [(i, ref, fraction) for i in range(len(design["front_layers"]) + 1)
                        for ref in request.candidate_materials for fraction in request.growth_quarter_wave_fractions]
                    if combinations:
                        offset = (cycle - 1) * 12 % len(combinations)
                        combinations = combinations[offset:] + combinations[:offset]
                    for index, (interface, ref, fraction) in enumerate(combinations[:12]):
                        stage_check()
                        if len(design["front_layers"]) >= (request.max_layers if request.max_layers is not None else MAX_LAYERS):
                            break
                        thickness = legal_growth_thickness(design, quarter[(ref.material_id, ref.optical_set_id)] * fraction)
                        if thickness is None:
                            continue
                        ids = _proposal_layer_ids(design, f"hybrid-growth-{cycle}-{index}", "unused",
                                                   reserved_ids=reserved_layer_ids)
                        trial = _insert_at_site(design, dict(kind="interface", interface=interface), ref, thickness, *ids)
                        if not feasible(trial):
                            continue
                        value = hybrid_evaluate(trial)
                        archive(value, audit_eligible=False, operator="finite_optical_growth")
                        emit("growth_trial", "有限正厚真实生长，工作分支允许最坏点暂时变差", trial, value["score"])
                        if grown is None or score_sse(value) < score_sse(grown):
                            grown = value
                    if grown is not None:
                        explorer, local = refine_synthesis_explorer(request, grown,
                            evaluate=hybrid_evaluate, archive=archive, cancel_check=stage_check,
                            progress=local_progress, max_iterations=48,
                            **({} if request.objective_progress_policy is None else
                               {"progress_policy": request.objective_progress_policy}))
                        objective_handoff_requested = record_objective_handoff(local, explorer)
                        total_real_growth += 1
                        emit("growth_restart", "从真实SSE终态续接，不重置为严格冠军", explorer["design"], explorer["score"])
                if not objective_handoff_requested:
                    explorer = physical_cleanup(explorer)
            except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                end_slice("resource_guard" if isinstance(exc, SynthesisResourceGuard) else "stage_budget")
            finally:
                end_slice()
            repair_near_feasible("joint_needle_sse_growth")
            if targets_verified():
                break
            assert best is not None
            endpoint = dict(cycle=cycle, physical_hash=physical_design_hash(explorer["design"]),
                design=deepcopy(explorer["design"]), layer_count=len(explorer["design"]["front_layers"]),
                max_error=explorer["score"]["max_error"], weighted_sse=score_sse(explorer),
                strict_champion_hash=physical_design_hash(best["design"]),
                strict_champion_max_error=best["score"]["max_error"])
            details["exploration_endpoints"].append(endpoint)
            emit("hybrid_exploration_terminal", json.dumps({k: v for k, v in endpoint.items() if k != "design"}, sort_keys=True), explorer["design"], explorer["score"])

            if not objective_handoff_requested:
                begin_slice("deletion_and_same_reference_merge", .15, 16)
                try:
                    champion_before = best
                    cleaned_champion = cleanup_champion()
                    if explorer["design"] == champion_before["design"]:
                        explorer = cleaned_champion
                    explorer = physical_cleanup(explorer)
                    parents = design_archive.select_parents(2)
                    for entry in parents:
                        rows = entry.design["front_layers"]
                        if not rows:
                            continue
                        order = sorted(rows, key=lambda row: row["thickness"]["value"])
                        offset = (cycle - 1) * 4 % len(order)
                        order = order[offset:] + order[:offset]
                        for row in order[:4]:
                            stage_check()
                            trial = deepcopy(entry.design)
                            trial["front_layers"] = [r for r in trial["front_layers"] if r["id"] != row["id"]]
                            value = hybrid_evaluate(trial)
                            archive(value, audit_eligible=False, provenance=dict(entry.provenance,
                                deleted_layer_id=row["id"]), parent_hash=entry.physical_hash, operator="physical_layer_deletion")
                            emit("deletion", "删层后原全部条件真实重算，保存实际设计而不只记分数", trial, value["score"])
                            if score_sse(value) < score_sse(explorer):
                                explorer = value
                except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                    end_slice("resource_guard" if isinstance(exc, SynthesisResourceGuard) else "stage_budget")
                finally:
                    end_slice()

            repair_near_feasible("deletion_and_same_reference_merge")
            if targets_verified():
                break
            # Always start the terminal epigraph from the real exploration
            # endpoint, even when its physical strict max has become worse.
            begin_slice("full_original_slsqp_minimax", .85, 256)
            try:
                if not objective_handoff_requested:
                    explorer = physical_cleanup(explorer)
                resource_check(explorer["design"], jacobian=True, epigraph=True)
                minimax_start = physical_design_hash(explorer["design"])
                endpoint.update(physical_hash=minimax_start, design=deepcopy(explorer["design"]),
                    layer_count=len(explorer["design"]["front_layers"]),
                    max_error=explorer["score"]["max_error"], weighted_sse=score_sse(explorer),
                    phase="actual_post_compression_minimax_input")
                emit("hybrid_pre_minimax_terminal", json.dumps({k: v for k, v in endpoint.items() if k != "design"}, sort_keys=True), explorer["design"], explorer["score"])
                archive_context.update(parent_hash=minimax_start, operator="full_original_slsqp_minimax")
                refined, minimax_details = refine_synthesis_full_minimax(request, explorer,
                    evaluate=hybrid_evaluate, archive=archive, cancel_check=stage_check,
                    progress=local_progress, max_iterations=120)
                minimax_details["initial_physical_hash"] = minimax_start
                details["last_full_minimax"] = minimax_details
                search_diagnostics["local_refinement"].append(minimax_details)
                archive(refined, audit_eligible=False)
                end_slice(minimax_details["termination"])
            except (SynthesisStageBudget, SynthesisResourceGuard) as exc:
                end_slice("resource_guard" if isinstance(exc, SynthesisResourceGuard) else "stage_budget")
            finally:
                end_slice()
                archive_context.clear()
            # Parent owns one independent audit and its original full-constraint
            # acceptance. Audit feedback cannot consume the next scan reserve.
            terminal_hybrid_cleanup()
            audit_archive(best, allow_feedback_repair=False)
            repair_near_feasible("rotation_audit")
            details["completed_rotations"] = cycle
            details["real_growth_rounds"] = total_real_growth
            if not streams and not explorer["design"]["front_layers"] and details["resource_skips"]:
                raise _Stopped("resource_limit")

        # All normal success exits, including the early engineering-prior
        # break, converge here. No optional optics after failure/deadline/cancel.
        reduce_audited_thickness()
        if "feasibility_repair" in details:
            details["feasibility_repair"]["subsequent_thickness_reduction"] = (
                details.get("thickness_reduction", {}).get("status", "disabled_or_not_entered"))

    try:
        try:
            _check_cancel(cancel_event)
            emit("freezing", "冻结目标、基片和显式膜材")
            request = freeze_film_synthesis_request(service, request, cancel_event=cancel_event)
            assert request.structure_search_seed is not None  # Every execution uses an explicit frozen root.
            resolved_structure_seed = request.structure_search_seed
            if request.randomize_template_search:
                metadata = template_randomization_metadata(request.structure_search_seed,
                    provenance=template_seed_provenance)
                search_diagnostics["template_randomization"] = dict(metadata, enabled=True, templates=[],
                    quota_policy="BASE retained; at most one legal extra per admitted template; reserve candidate/physical slots before drawing; original final audit slot retained",
                    replay_scope="same seed/config reconstruct starts; wall-time-limited final output is not bitwise guaranteed")
                emit("template_randomization_config", json.dumps(metadata, sort_keys=True))
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
            initial_design = request.environment
            if request.warm_start_seed_index is not None:
                initial_design = request.seed_designs[request.warm_start_seed_index]
                from .film_synthesis_archive import physical_design_hash
                search_diagnostics["warm_start"] = dict(
                    mode="warm_restart", seed_index=request.warm_start_seed_index,
                    physical_hash=physical_design_hash(initial_design),
                    solver_state_preserved=False, saved_score_reused=False,
                    policy="first candidate freshly scored inside the new job budget; search restarts")
                emit("warm_restart", "从保留候选重新启动求解器；首个完整评分计入本次新预算")
            working = evaluate(initial_design, gpu_forward=forward is not None)
            archive(working)
            assert best is not None  # The frozen bare or explicit warm seed is feasible.
            if request.search_strategy == "hybrid":
                run_hybrid(working)
                raise _Stopped("training_targets_met" if targets_verified() else "search_exhausted")
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
                    extra_raw = None
                    if request.randomize_template_search:
                        from .film_synthesis_global import StructuralProposal
                        slots = (2 if request.max_evaluations is None else request.max_evaluations - evaluations)
                        extra = extra_template_start(StructuralProposal(trial, dict(family="homogeneous")),
                            stage="legacy_homogeneous_template", candidate_slots=slots)
                        if extra is not None:
                            extra_raw = evaluate(extra.design, gpu_forward=forward is not None)
                            emit("structure_proposal", json.dumps(extra.provenance, sort_keys=True), extra.design, extra_raw["score"])
                            archive(extra_raw, audit_eligible=False, provenance=extra.provenance, operator="engineering_prior")
                    archive(raw)
                    if extra_raw is not None and not targets_verified():
                        archive(extra_raw, provenance=extra.provenance, operator="engineering_prior")
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
                o.metric == "R" and not o.mean.enabled and o.pointwise.enabled
                and o.pointwise.mode == "range" and o.pointwise.lower_bound is not None
                and o.pointwise.lower_bound >= .5
                and (o.pointwise.upper_bound is None or o.pointwise.upper_bound >= 1.)
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
                                and solver_acceptable(request, raw["score"])):
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
                                    and solver_acceptable(request, candidate["score"])):
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
                batch_work: int = 0
                source = iter_multispectral_hr_proposals(request,
                    cancel_check=check_training_admission, report=multi_report)
                if request.randomize_template_search:
                    source = legacy_template_stream(source, stage="legacy_multispectral_template",
                        extra_admit=lambda proposal: batch_work + sample_points * max(1,
                            len(proposal.design["front_layers"])) <= STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM)
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
                hr_source = iter_dual_substack_hr_proposals(request,
                    cancel_check=check_training_admission, report=hr_report)
                for proposal in hr_source:
                    check_training_admission()
                    entry: dict[str, object] = dict(proposal.provenance,
                        layer_count=len(proposal.design["front_layers"]), state="evaluating_prior")
                    hr_runs.append(entry)
                    raw_hr = evaluate(proposal.design, jacobian=True)
                    entry["initial_training_min_R"] = min(error.actual for error in raw_hr["score"]["errors"])
                    emit("hr_minimax_prior", json.dumps(entry, sort_keys=True), raw_hr["design"], raw_hr["score"])
                    archive(raw_hr, audit_eligible=False)
                    if request.randomize_template_search:
                        slots = (2 if request.max_evaluations is None else request.max_evaluations - evaluations)
                        extra = extra_template_start(proposal, stage="legacy_hr_template",
                            candidate_slots=slots, jacobian=True)
                        if extra is not None:
                            extra_hr = evaluate(extra.design, jacobian=True)
                            archive(extra_hr, audit_eligible=False, provenance=extra.provenance, operator="engineering_prior")
                            emit("hr_minimax_prior", json.dumps(extra.provenance, sort_keys=True), extra_hr["design"], extra_hr["score"])
                            if rank(extra_hr) < rank(raw_hr):
                                raw_hr = extra_hr
                    if (raw_hr["score"]["max_error"] <= request.acceptable_normalized_error
                            and solver_acceptable(request, raw_hr["score"])):
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
                if request.randomize_template_search:
                    # Consume BASE before the wrapper reserves its extra, so
                    # physical admission never depends on speculative workers.
                    chunk_size = 1
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
                    if request.randomize_template_search:
                        pending_structural_scans[epoch] = legacy_template_stream(
                            pending_structural_scans[epoch], stage="legacy_structure_template",
                            extra_admit=lambda proposal: legacy_template_work_admission[0](proposal))
                proposals = pending_structural_scans[epoch]
                exhausted = yielded = False
                stage_work = consumed = 0
                keepers: dict[object, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
                sample_points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
                if request.randomize_template_search:
                    legacy_template_work_admission[0] = lambda proposal: (work_quantum is None or
                        stage_work + sample_points * max(1, len(proposal.design["front_layers"])) <= work_quantum)
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
                            seed=resolved_structure_seed + epoch, epoch=epoch, expansion=expansion, coverage_complete=False, stop_reason="stage_quantum")
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
                        evolution = DifferentialEvolutionProposals(initial, lower, upper, seed=resolved_structure_seed + epoch, population_size=population_size)
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
                                    and solver_acceptable(request, global_best["score"])):
                                audit_archive(global_best)
                            if targets_verified():
                                global_record["stop_reason"] = "validated_targets_met"
                                break
                        if parameterization == "structure" and not targets_verified():
                            global_best = interior_refine(global_best)
                            archive(global_best, audit_eligible=False)
                            if (global_best["score"]["max_error"] <= request.acceptable_normalized_error
                                    and solver_acceptable(request, global_best["score"])):
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
                    if audited_best is None or epoch % 2 == 1 or (best["score"]["max_error"] <= request.acceptable_normalized_error and solver_acceptable(request, best["score"])):
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
                if request.randomize_template_search:
                    from .film_synthesis_global import StructuralProposal
                    archive(seed_value, audit_eligible=False)
                    slots = (2 if request.max_evaluations is None else request.max_evaluations - evaluations)
                    extra = extra_template_start(StructuralProposal(deepcopy(dict(seed_design)), dict(family="user_seed")),
                        stage="legacy_seed_template", candidate_slots=slots, jacobian=True)
                    if extra is not None:
                        extra_seed = evaluate(extra.design, jacobian=True)
                        archive(extra_seed, audit_eligible=False, provenance=extra.provenance, operator="engineering_prior")
                        emit("seed_design", json.dumps(extra.provenance, sort_keys=True), extra_seed["design"], extra_seed["score"])
                        if rank(extra_seed) < rank(seed_value):
                            seed_value = extra_seed
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
                if terminal_hybrid_cleanup is not None:
                    terminal_hybrid_cleanup()
                audit_archive(best, allow_feedback_repair=request.search_strategy == "legacy")
                if audited_best is not None:
                    validated_training, validation = audited_best
                    if state != "failed":
                        if targets_verified():
                            state, reason = "completed", "validated_targets_met"
                        elif (nominal_targets_pass(request, validated_training["score"])
                                and (request.search_strategy != "hybrid" or reason not in {
                                    "cycle_limit", "evaluation_limit", "time_limit", "resource_limit"})):
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
        close_proposal_streams()
        execution.close()
        if forward is not None:
            forward.close()
        met = state == "completed" and best is not None and validation is not None and (
            nominal_targets_pass(request, best["score"])
            and nominal_targets_pass(request, validation["score"], independent_audit=True))
        acceptable = (solver_acceptable(request, best["score"])
            and solver_acceptable(request, validation["score"], independent_audit=True)
            if best is not None and validation is not None else None)
        message = ("找到通过训练网格与独立密网格验证的候选；尚未应用到用户设计。" if met else
                   "已取消；保留此前完整评分的候选，未确认达标。" if state == "cancelled" else
                   "尚未找到通过全部验证的膜系；候选仅供审查。")
        emit(state, message, None if best is None else best["design"], None if best is None else best["score"])
        display_value(best, is_best=True, is_current=False)
        finish_candidate_display(candidate_observer, state=state)
        return FilmSynthesisResult(state, reason, None if best is None else annotate_synthesis_design(request, best["design"]),
            request.design_revision, met, None if best is None else deepcopy(best["score"]),
            None if validation is None else deepcopy(validation["score"]), validation is not None,
            cycle, evaluations, validation_evaluations, failures, tuple(trace), message, request,
            audit_physical_evaluations, audit_wavelength_points, performance(), candidate_provenance, acceptable)
    except _ObserverFailure as exc:
        raise exc.error.with_traceback(exc.error.__traceback__) from None
    finally:
        execution.close()
        if forward is not None:
            forward.close()
        close_proposal_streams()
