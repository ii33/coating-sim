"""Explicit classical seed enumeration and local refinement, independent of Qt.

No global search, needle insertion, catalog writes or session application occurs.
Every attempted seed retains its own applicability and independent validation.
"""
from __future__ import annotations

from .film_objective_payload import objective_from_payload

from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
from itertools import product
import json
import math
from typing import Any

import numpy as np

from .film_auto_synthesis import (
    FilmSynthesisRequest, _check_cancel, _number, _proxy, _validated,
    freeze_film_synthesis_request,
)
from .film_design import MAX_LAYERS
from .film_design_templates import MAX_PAIRS, TemplateGenerationError, build_preview, template_catalog
from .film_optimization_needle import FilmNeedleMaterialReference
from .film_optimization_structure import _StructureCancelled, evaluate_frozen_structure_candidate
from .optical_constants import prepare_optical_set

Evaluation = Mapping[str, Any]


@dataclass(frozen=True)
class FilmClassicLocalRequest:
    shared_request: FilmSynthesisRequest
    wavelengths_nm: tuple[float, ...]
    pair_counts: tuple[int, ...]
    template_ids: tuple[str, ...] | None = None
    max_local_iterations: int | None = None


@dataclass(frozen=True)
class FilmClassicSeedSpec:
    candidate_id: str
    template_id: str
    wavelength_nm: float
    pairs: int
    role_bindings: tuple[tuple[str, FilmNeedleMaterialReference], ...]


@dataclass(frozen=True)
class FilmClassicObjectiveStatus:
    objective_index: int
    seed_max_normalized_error: float | None
    training_max_normalized_error: float | None
    validation_max_normalized_error: float | None
    training_met: bool | None
    validation_met: bool | None


@dataclass(frozen=True)
class FilmClassicLocalCandidate:
    spec: FilmClassicSeedSpec
    state: str
    termination_reason: str
    seed_design: Mapping[str, Any] | None
    candidate_design: Mapping[str, Any] | None
    seed_score: Evaluation | None
    training_score: Evaluation | None
    validation_score: Evaluation | None
    objectives: tuple[FilmClassicObjectiveStatus, ...]
    targets_met: bool
    validation_complete: bool
    evaluations: int
    local_iterations: int
    applicability: str
    limitations: str
    message: str


@dataclass(frozen=True)
class FilmClassicLocalProgress:
    stage: str
    candidate_id: str
    attempted: int
    planned: int
    evaluations: int
    local_iteration: int
    best_max_normalized_error: float | None
    message: str


@dataclass(frozen=True)
class FilmClassicLocalResult:
    state: str
    termination_reason: str
    candidates: tuple[FilmClassicLocalCandidate, ...]
    ranked_candidate_ids: tuple[str, ...]
    planned: int
    attempted: int
    coverage_complete: bool
    any_targets_met: bool
    evaluations: int
    validation_evaluations: int
    failed_evaluations: int
    frozen_request: FilmClassicLocalRequest
    message: str


def _validate(request: FilmClassicLocalRequest) -> FilmClassicLocalRequest:
    if not isinstance(request, FilmClassicLocalRequest):
        raise ValueError("request must be FilmClassicLocalRequest")
    shared = _validated(request.shared_request)
    if shared.max_cycles is not None:
        raise ValueError("max_cycles belongs to needle evolution; use max_local_iterations here")
    catalog = {r["stable_id"]: r for r in template_catalog()}
    selected = tuple(catalog) if request.template_ids is None else request.template_ids
    if (not isinstance(selected, (tuple, list)) or not selected or
            any(not isinstance(v, str) or v not in catalog for v in selected) or
            len(set(selected)) != len(selected)):
        raise ValueError("template_ids must select unique existing template IDs")
    if not isinstance(request.wavelengths_nm, (tuple, list)) or not request.wavelengths_nm:
        raise ValueError("explicit design wavelengths are required")
    waves = tuple(_number(v, "design wavelength") for v in request.wavelengths_nm)
    if len(set(waves)) != len(waves):
        raise ValueError("design wavelengths must be unique")
    if not isinstance(request.pair_counts, (tuple, list)):
        raise ValueError("pair_counts must be a sequence")
    if any(catalog[v]["default_pairs"] is not None for v in selected) and not request.pair_counts:
        raise ValueError("periodic templates require explicit pair_counts; no default pair range is inferred")
    if any(type(v) is not int or not 1 <= v <= MAX_PAIRS for v in request.pair_counts):
        raise ValueError(f"pair_counts must satisfy the existing generator range 1..{MAX_PAIRS}")
    if len(set(request.pair_counts)) != len(request.pair_counts):
        raise ValueError("pair_counts must be unique")
    if request.max_local_iterations is not None and (
            type(request.max_local_iterations) is not int or request.max_local_iterations < 1):
        raise ValueError("max_local_iterations must be positive or None")
    return replace(request, shared_request=shared, template_ids=tuple(selected),
                   wavelengths_nm=waves, pair_counts=tuple(request.pair_counts))


def classic_seed_count(request: FilmClassicLocalRequest) -> int:
    request = _validate(request)
    catalog = {r["stable_id"]: r for r in template_catalog()}
    return sum(len(request.wavelengths_nm) * len(request.shared_request.candidate_materials) ** len(catalog[t]["roles"])
               * (len(request.pair_counts) if catalog[t]["default_pairs"] is not None else 1)
               for t in request.template_ids or ())


def iter_classic_seed_specs(request: FilmClassicLocalRequest) -> Iterator[FilmClassicSeedSpec]:
    """Enumerate all requested role products lazily, without a hidden cap.

    Repetition is allowed: e.g. the two outer Q/H/Q roles can share a material.
    H>L and transparency are checked by the actual generator, not guessed here.
    """
    request = _validate(request)
    catalog = {r["stable_id"]: r for r in template_catalog()}
    for template_id in request.template_ids or ():
        template = catalog[template_id]
        counts = request.pair_counts if template["default_pairs"] is not None else (0,)
        for wave, count in product(request.wavelengths_nm, counts):
            for refs in product(request.shared_request.candidate_materials, repeat=len(template["roles"])):
                bindings = tuple(zip(template["roles"], refs, strict=True))
                identity = [template_id, wave, count, [(role, asdict(ref)) for role, ref in bindings]]
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
                yield FilmClassicSeedSpec("classic-" + digest, template_id, wave, count, bindings)


def freeze_film_classic_local_request(service: Any, request: FilmClassicLocalRequest,
                                      *, cancel_event: Any = None) -> FilmClassicLocalRequest:
    request = _validate(request)
    shared = freeze_film_synthesis_request(service, request.shared_request, cancel_event=cancel_event)
    return replace(request, shared_request=shared)


def _preview(request: FilmClassicLocalRequest, spec: FilmClassicSeedSpec) -> dict[str, Any]:
    shared = request.shared_request
    snapshot = shared.frozen_material_snapshot
    if snapshot is None:
        raise ValueError("classical request must be frozen")
    rows = {r["id"]: deepcopy(r) for r in snapshot["materials"]}
    options = {"film": list(rows.values()), "substrate": list(rows.values()), "medium": []}
    incident = shared.environment["incident_medium"]
    if incident["kind"] == "constant":
        # Adapter only for an explicitly declared constant incident port. It is
        # not a fabricated film/substrate dataset, and is restored before score.
        identifier = "classic-port-" + hashlib.sha256(json.dumps(incident, sort_keys=True).encode()).hexdigest()[:16]
        waves = [*request.wavelengths_nm,
                 *(o.wavelength_min_nm for o in shared.objectives),
                 *(o.wavelength_max_nm for o in shared.objectives)]
        optical: dict[str, Any] = dict(id="declared-constant", name="Declared constant incident port", kind="tabulated",
            wavelength_unit="nm", wavelength_min=min(waves), wavelength_max=max(waves),
            wavelengths=sorted(set(waves)), n=[incident["n"]] * len(set(waves)),
            k=[0.0] * len(set(waves)), source="Explicit environment constant; not measured material data")
        # The optical table contract needs two points, even for one wavelength.
        if len(optical["wavelengths"]) == 1:
            optical.update(wavelength_min=waves[0] * 0.5, wavelength_max=waves[0] * 1.5,
                wavelengths=[waves[0] * 0.5, waves[0] * 1.5], n=[incident["n"]] * 2, k=[0.0] * 2)
        options["medium"] = [dict(id=identifier, name="Declared incident port", optical_sets=[optical])]
        incident_binding = dict(material_id=identifier, optical_set_id="declared-constant")
    else:
        incident_binding = dict(material_id=incident["material_id"], optical_set_id=incident["optical_set_id"])
    preview = build_preview(inputs=dict(template_id=spec.template_id, wavelength_nm=spec.wavelength_nm,
        pairs=spec.pairs, assume_lossless=False,
        role_bindings={role: asdict(ref) for role, ref in spec.role_bindings},
        incident_binding=incident_binding,
        substrate_binding={k: shared.environment["substrate"][k] for k in ("material_id", "optical_set_id")}),
        base_design=shared.environment, material_options=options)
    design = preview["candidate_design"]
    design["id"] = spec.candidate_id
    design["incident_medium"] = deepcopy(incident)
    for index, layer in enumerate(design["front_layers"]):
        layer["id"] = f"{spec.candidate_id}-{index}"
    return preview


def _feasible(shared: FilmSynthesisRequest, design: Mapping[str, Any]) -> bool:
    ds = [r["thickness"]["value"] for r in design["front_layers"]]
    return (len(ds) <= (shared.max_layers if shared.max_layers is not None else MAX_LAYERS)
            and all(math.isfinite(d) and d > 0 and d >= (shared.minimum_nonzero_thickness_nm or 0)
                    and (shared.max_layer_thickness_nm is None or d <= shared.max_layer_thickness_nm) for d in ds)
            and (shared.max_total_thickness_nm is None or sum(ds) <= shared.max_total_thickness_nm))


def _rank(value: Evaluation) -> tuple[Any, ...]:
    rows = value["design"]["front_layers"]
    return (*value["score"]["key"], len(rows), sum(r["thickness"]["value"] for r in rows))


def _objective_status(shared: FilmSynthesisRequest, seed: Evaluation | None,
                      best: Evaluation | None, validation: Evaluation | None) -> tuple[FilmClassicObjectiveStatus, ...]:
    def maximum(value: Evaluation | None, index: int) -> float | None:
        if value is None:
            return None
        return max(e.normalized_violation for e in value["score"]["errors"] if e.objective_index == index)
    result = []
    for index in range(len(shared.objectives)):
        train, valid = maximum(best, index), maximum(validation, index)
        result.append(FilmClassicObjectiveStatus(index, maximum(seed, index), train, valid,
            None if train is None else train <= shared.acceptable_normalized_error,
            None if valid is None else valid <= shared.acceptable_normalized_error))
    return tuple(result)


class _Budget(Exception):
    pass


def optimize_film_design_from_classics(service: Any, request: FilmClassicLocalRequest, *, cancel_event: Any = None,
        progress_callback: Callable[[FilmClassicLocalProgress], None] | None = None) -> FilmClassicLocalResult:
    """Refine every requested classical seed, returning multiple isolated results.

    The shared max_evaluations is a whole-run budget, with one validation pass
    reserved for the candidate currently being refined. None is unbounded.
    """
    request = _validate(request)
    planned = classic_seed_count(request)
    candidates: list[FilmClassicLocalCandidate] = []
    evaluations = validation_evaluations = failures = 0
    stop = "enumeration_complete"
    current_id = ""
    local_iteration = 0
    best: Evaluation | None = None

    def emit(stage: str, message: str) -> None:
        if progress_callback:
            progress_callback(FilmClassicLocalProgress(stage, current_id, len(candidates), planned,
                evaluations, local_iteration, None if best is None else best["score"]["max_error"], message))

    def evaluate(design: Mapping[str, Any], *, holdout: bool = False) -> Evaluation:
        nonlocal evaluations, validation_evaluations, failures
        _check_cancel(cancel_event)
        limit = request.shared_request.max_evaluations
        if limit is not None and evaluations >= limit - (0 if holdout else 1):
            raise _Budget()
        evaluations += 1
        validation_evaluations += int(holdout)
        try:
            value = evaluate_frozen_structure_candidate(_proxy(request.shared_request), design,
                variable_ids=() if holdout else tuple(r["id"] for r in design["front_layers"]),
                with_jacobian=not holdout, derived_validation_grid=holdout, cancel_event=cancel_event)
            _check_cancel(cancel_event)
            return value
        except _StructureCancelled:
            raise
        except (ValueError, ArithmeticError):
            failures += 1
            raise

    def refine(start: Evaluation, spec: FilmClassicSeedSpec) -> tuple[Evaluation, str]:
        nonlocal best, local_iteration
        shared = request.shared_request
        snapshot = shared.frozen_material_snapshot
        if snapshot is None:
            raise ValueError("missing material snapshot")
        rows = {r["id"]: r for r in snapshot["materials"]}
        quarter = {}
        for _, ref in spec.role_bindings:
            optical = next(o for o in rows[ref.material_id]["optical_sets"] if o["id"] == ref.optical_set_id)
            n, _, _ = prepare_optical_set(optical)(spec.wavelength_nm, "nm", extrapolation_policy="error")
            quarter[(ref.material_id, ref.optical_set_id)] = spec.wavelength_nm / (4 * n)
        value, step = start, 0.25
        while value["design"]["front_layers"]:
            _check_cancel(cancel_event)
            if value["score"]["max_error"] <= shared.acceptable_normalized_error:
                return value, "training_targets_met"
            if request.max_local_iterations is not None and local_iteration >= request.max_local_iterations:
                return value, "local_iteration_limit"
            layers = value["design"]["front_layers"]
            ds = np.array([r["thickness"]["value"] for r in layers])
            scales = np.array([quarter[(r["material_id"], r["optical_set_id"])] for r in layers])
            gradient = np.array([value["gradient_by_layer_id"][r["id"]] for r in layers]) * scales
            magnitude = float(np.linalg.norm(gradient))
            if magnitude <= np.finfo(float).eps or step < shared.local_step_tolerance:
                return value, "local_stagnation"
            proposal = np.maximum(ds - step * scales * gradient / magnitude, 0.0)
            if shared.max_layer_thickness_nm is not None:
                proposal = np.minimum(proposal, shared.max_layer_thickness_nm)
            minimum = shared.minimum_nonzero_thickness_nm
            if minimum is not None:
                proposal = np.where(proposal < minimum / 2, 0.0, np.maximum(proposal, minimum))
            if np.max(np.abs(proposal - ds) / scales) < shared.local_step_tolerance:
                return value, "local_stagnation"
            local_iteration += 1
            trial = deepcopy(value["design"])
            for layer, thickness in zip(trial["front_layers"], proposal, strict=True):
                layer["thickness"] = dict(value=float(thickness), unit="nm")
            trial["front_layers"] = [r for r in trial["front_layers"] if r["thickness"]["value"] > 0]
            if not _feasible(shared, trial):
                step *= 0.5
                emit("refinement", "投影点超出显式约束，缩短局部步长")
                continue
            candidate = evaluate(trial)
            if best is None or _rank(candidate) < _rank(best):
                best = deepcopy(candidate)
            emit("refinement", "完整目标网格解析梯度局部精修")
            improvement = value["smooth_loss"] - candidate["smooth_loss"]
            if improvement > shared.relative_improvement_tolerance * max(value["smooth_loss"], 1e-30):
                value, step = candidate, min(0.5, step * 1.5)
            else:
                step *= 0.5
        return value, "zero_layers"

    try:
        emit("freezing", "冻结环境、目标和显式材料；不修改用户设计")
        request = freeze_film_classic_local_request(service, request, cancel_event=cancel_event)
        catalog = {r["stable_id"]: r for r in template_catalog()}
        for spec in iter_classic_seed_specs(request):
            _check_cancel(cancel_event)
            limit = request.shared_request.max_evaluations
            if limit is not None and evaluations + 2 > limit:
                stop = "evaluation_limit"
                break
            current_id, local_iteration, best = spec.candidate_id, 0, None
            seed: Evaluation | None = None
            validation: Evaluation | None = None
            design: Mapping[str, Any] | None = None
            reason, state, message = "local_stagnation", "not_found", "尚未找到通过全部目标验证的膜系"
            before = evaluations
            template = catalog[spec.template_id]
            emit("generating", "按明确模板、材料角色、波长和层对数生成起点")
            try:
                preview = _preview(request, spec)
                design = preview["candidate_design"]
                if design is None or not _feasible(request.shared_request, design):
                    raise TemplateGenerationError("经典起点超出显式制造约束；未静默投影或截断模板")
                seed = evaluate(design)
                best = deepcopy(seed)  # archive before any more work or callback
                emit("seed_scored", "已完整评分经典起点，保留其独立基线")
                _, reason = refine(seed, spec)
            except TemplateGenerationError as exc:
                state, reason, message = "inapplicable", "template_inapplicable", str(exc)
            except _Budget:
                reason, stop = "evaluation_limit", "evaluation_limit"
            except _StructureCancelled:
                state, reason, stop = "cancelled", "cancelled", "cancelled"
                message = "已取消；此前完整评分的候选仅供审查"
            except (ValueError, ArithmeticError) as exc:
                state, reason, message = "failed", "numerical_failure", str(exc)
            if best is not None and state != "cancelled":
                try:
                    emit("validation", "独立密网格验证每项目标的全部角度、偏振和上下限")
                    validation = evaluate(best["design"], holdout=True)
                    emit("validation_complete", "密网格评分完整，检查取消后提交候选状态")
                    _check_cancel(cancel_event)
                    if (state != "failed" and best["score"]["max_error"] <= request.shared_request.acceptable_normalized_error
                            and validation["score"]["max_error"] <= request.shared_request.acceptable_normalized_error):
                        state, reason, message = "completed", "validated_targets_met", "该候选通过训练与独立密网格验证，尚未应用"
                    elif state != "failed" and best["score"]["max_error"] <= request.shared_request.acceptable_normalized_error:
                        reason = "validation_targets_not_met"
                except _StructureCancelled:
                    state, reason, stop = "cancelled", "cancelled_during_validation", "cancelled"
                    message = "已取消；完整评分保留，未提交该候选的达标状态"
                except (ValueError, ArithmeticError, _Budget) as exc:
                    state, reason, message = "failed", "validation_failed", str(exc)
            candidates.append(FilmClassicLocalCandidate(spec, state, reason, deepcopy(design),
                None if best is None else deepcopy(best["design"]),
                None if seed is None else deepcopy(seed["score"]),
                None if best is None else deepcopy(best["score"]),
                None if validation is None else deepcopy(validation["score"]),
                _objective_status(request.shared_request, seed, best, validation), state == "completed",
                validation is not None, evaluations - before, local_iteration,
                template["applicability"], template["limitations"], message))
            emit("candidate_complete", "候选状态已提交；继续枚举其他请求内的起点")
            _check_cancel(cancel_event)
            if stop in ("evaluation_limit", "cancelled"):
                break
    except _StructureCancelled:
        stop = "cancelled"
    except (ValueError, ArithmeticError) as exc:
        stop = "freeze_failed: " + str(exc)
    coverage = len(candidates) == planned
    any_met = any(c.targets_met for c in candidates)
    all_numerical_failures = (coverage and any(c.state == "failed" for c in candidates)
        and all(c.state in ("failed", "inapplicable") for c in candidates))
    state = ("cancelled" if stop == "cancelled" else "failed" if stop.startswith("freeze_failed") else
             "partial" if not coverage else "failed" if all_numerical_failures else
             "completed" if any_met else "not_found")
    if all_numerical_failures:
        stop = "all_applicable_candidates_failed"
    elif coverage and all(c.state == "inapplicable" for c in candidates):
        stop = "no_applicable_seed"
    def result_rank(candidate: FilmClassicLocalCandidate) -> tuple[Any, ...]:
        score = candidate.validation_score if candidate.validation_score is not None else candidate.training_score
        if score is None:
            raise ValueError("ranking requires a complete score")
        return (not candidate.targets_met, not candidate.validation_complete,
                score["weighted_rms_error"], candidate.spec.candidate_id)
    ranked = sorted((c for c in candidates if c.training_score is not None), key=result_rank)
    message = ("请求内经典起点枚举完成，返回各候选及逐项目标结果" if coverage else
               "枚举未完成；保留已完成候选，不宣称覆盖全部请求范围")
    emit(state, message)
    return FilmClassicLocalResult(state, stop, tuple(candidates), tuple(c.spec.candidate_id for c in ranked),
        planned, len(candidates), coverage, any_met, evaluations, validation_evaluations, failures, request, message)


def film_classic_local_request_to_payload(request: FilmClassicLocalRequest) -> dict[str, Any]:
    request = _validate(request)
    return dict(schema_version=2, kind="coat_sim.film_classic_local_request", **asdict(request))


def film_classic_local_request_from_payload(payload: Mapping[str, Any]) -> FilmClassicLocalRequest:
    allowed = {f.name for f in fields(FilmClassicLocalRequest)}
    if (not isinstance(payload, Mapping) or set(payload) - allowed - {"schema_version", "kind"}
            or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 2
            or payload.get("kind") != "coat_sim.film_classic_local_request"):
        raise ValueError("invalid classic local request payload")
    data = deepcopy(dict(payload))
    data.pop("schema_version")
    data.pop("kind")
    raw = data["shared_request"]
    raw["objectives"] = tuple(objective_from_payload(r) for r in raw["objectives"])
    raw["candidate_materials"] = tuple(FilmNeedleMaterialReference(**r) for r in raw["candidate_materials"])
    data["shared_request"] = FilmSynthesisRequest(**raw)
    return _validate(FilmClassicLocalRequest(**data))
