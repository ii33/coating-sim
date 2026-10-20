"""Classical template enumeration plus fixed-topology thickness DE, without Qt.

The bounded SciPy backend is advanced one generation at a time so None means
no generation cap. Private iterator compatibility is tested and documented.
No local polish, layer insertion, catalog mutation or session application.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
from importlib import import_module
import math
from typing import Any

import numpy as np

from .film_auto_synthesis import _check_cancel, _number, _proxy
from .film_classic_local import (
    Evaluation, FilmClassicLocalRequest, FilmClassicObjectiveStatus, FilmClassicSeedSpec,
    _objective_status, _preview, _rank, _validate as _validate_enumeration,
    classic_seed_count, film_classic_local_request_from_payload,
    film_classic_local_request_to_payload, freeze_film_classic_local_request, iter_classic_seed_specs,
)
from .film_design import MAX_LAYERS
from .film_design_templates import TemplateGenerationError, template_catalog
from .film_optimization_solvers import SolverStop
from .film_optimization_structure import _StructureCancelled, evaluate_frozen_structure_candidate


@dataclass(frozen=True)
class FilmGlobalThicknessBounds:
    lower_nm: float
    upper_nm: float
    source: str


@dataclass(frozen=True)
class FilmClassicGlobalRequest:
    enumeration: FilmClassicLocalRequest
    thickness_bounds: FilmGlobalThicknessBounds
    seed: int
    population_size: int = 12
    max_generations: int | None = None
    stagnation_generations: int = 20
    convergence_relative_tolerance: float = 1e-6


@dataclass(frozen=True)
class FilmClassicGlobalCandidate:
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
    generations: int
    proposals: int
    rejected_proposals: int
    failed_evaluations: int
    effective_bounds: FilmGlobalThicknessBounds
    effective_seed: int
    applicability: str
    limitations: str
    message: str


@dataclass(frozen=True)
class FilmClassicGlobalProgress:
    stage: str
    candidate_id: str
    attempted: int
    planned: int
    evaluations: int
    generation: int
    proposals: int
    best_max_normalized_error: float | None
    message: str


@dataclass(frozen=True)
class FilmClassicGlobalResult:
    state: str
    termination_reason: str
    candidates: tuple[FilmClassicGlobalCandidate, ...]
    ranked_candidate_ids: tuple[str, ...]
    planned: int
    attempted: int
    coverage_complete: bool
    any_targets_met: bool
    evaluations: int
    validation_evaluations: int
    failed_evaluations: int
    frozen_request: FilmClassicGlobalRequest
    message: str


def _validate(request: FilmClassicGlobalRequest) -> FilmClassicGlobalRequest:
    if not isinstance(request, FilmClassicGlobalRequest):
        raise ValueError("request must be FilmClassicGlobalRequest")
    enumeration = _validate_enumeration(request.enumeration)
    if enumeration.max_local_iterations is not None:
        raise ValueError("max_local_iterations is not used by the global route; set None")
    bounds = request.thickness_bounds
    if not isinstance(bounds, FilmGlobalThicknessBounds):
        raise ValueError("explicit finite thickness_bounds and source are required")
    lo = _number(bounds.lower_nm, "lower_nm", zero=True)
    hi = _number(bounds.upper_nm, "upper_nm", zero=True)
    if lo > hi or not isinstance(bounds.source, str) or not bounds.source.strip():
        raise ValueError("finite thickness bounds require lower <= upper and explicit source")
    if type(request.seed) is not int or request.seed < 0:
        raise ValueError("seed must be an explicit nonnegative integer")
    if type(request.population_size) is not int or request.population_size < 5:
        raise ValueError("population_size must be at least 5 individuals (SciPy requirement)")
    for name in ("max_generations", "stagnation_generations"):
        value = getattr(request, name)
        if value is None and name == "max_generations":
            continue
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer" + (" or None" if name == "max_generations" else ""))
    tolerance = _number(request.convergence_relative_tolerance, "convergence_relative_tolerance", zero=True)
    return replace(request, enumeration=enumeration,
        thickness_bounds=FilmGlobalThicknessBounds(lo, hi, bounds.source.strip()),
        convergence_relative_tolerance=tolerance)


def freeze_film_classic_global_request(service: Any, request: FilmClassicGlobalRequest,
                                       *, cancel_event: Any = None) -> FilmClassicGlobalRequest:
    request = _validate(request)
    return replace(request, enumeration=freeze_film_classic_local_request(
        service, request.enumeration, cancel_event=cancel_event))


def _effective_bounds(request: FilmClassicGlobalRequest) -> FilmGlobalThicknessBounds:
    bounds = request.thickness_bounds
    maximum = request.enumeration.shared_request.max_layer_thickness_nm
    if maximum is None or maximum >= bounds.upper_nm:
        return bounds
    return FilmGlobalThicknessBounds(bounds.lower_nm, maximum,
        bounds.source + "; intersected with explicit shared.max_layer_thickness_nm")


def _feasible(request: FilmClassicGlobalRequest, design: Mapping[str, Any],
              bounds: FilmGlobalThicknessBounds) -> bool:
    shared = request.enumeration.shared_request
    ds = [r["thickness"]["value"] for r in design["front_layers"]]
    return (len(ds) <= (MAX_LAYERS if shared.max_layers is None else shared.max_layers)
        and all(math.isfinite(d) and bounds.lower_nm <= d <= bounds.upper_nm
            and (d == 0 or shared.minimum_nonzero_thickness_nm is None
                 or d >= shared.minimum_nonzero_thickness_nm) for d in ds)
        and (shared.max_total_thickness_nm is None or sum(ds) <= shared.max_total_thickness_nm))


class _Budget(Exception):
    pass


def search_film_design_from_classics(service: Any, request: FilmClassicGlobalRequest, *, cancel_event: Any = None,
        progress_callback: Callable[[FilmClassicGlobalProgress], None] | None = None) -> FilmClassicGlobalResult:
    """Search each explicit classical topology; return candidates, never apply them."""
    request = _validate(request)
    planned = classic_seed_count(request.enumeration)
    candidates: list[FilmClassicGlobalCandidate] = []
    evaluations = validation_evaluations = failures = 0
    generation = proposals = rejected = 0
    current_id, stop = "", "enumeration_complete"
    best: Evaluation | None = None

    def emit(stage: str, message: str) -> None:
        if progress_callback:
            progress_callback(FilmClassicGlobalProgress(stage, current_id, len(candidates), planned,
                evaluations, generation, proposals, None if best is None else best["score"]["max_error"], message))

    def evaluate(design: Mapping[str, Any], *, holdout: bool = False) -> Evaluation:
        nonlocal evaluations, validation_evaluations, failures
        _check_cancel(cancel_event)
        limit = request.enumeration.shared_request.max_evaluations
        if limit is not None and evaluations >= limit - (0 if holdout else 1):
            raise _Budget()
        evaluations += 1
        validation_evaluations += int(holdout)
        try:
            value = evaluate_frozen_structure_candidate(_proxy(request.enumeration.shared_request), design,
                derived_validation_grid=holdout, cancel_event=cancel_event)
            _check_cancel(cancel_event)
            return value
        except _StructureCancelled:
            raise
        except (ValueError, ArithmeticError):
            failures += 1
            raise

    def evolve(seed: Evaluation, bounds: FilmGlobalThicknessBounds, effective_seed: int) -> str:
        nonlocal best, generation, proposals, rejected
        # Same SciPy backend and strategy as film_optimization_solvers. Iterate
        # directly instead of solve() so its default maxiter is never a cap.
        try:
            solver_type = import_module("scipy.optimize._differentialevolution").DifferentialEvolutionSolver
        except (ImportError, AttributeError) as exc:
            raise ValueError("SciPy incremental DE backend is unavailable; no fallback search was substituted") from exc
        shared = request.enumeration.shared_request
        if seed["score"]["max_error"] <= shared.acceptable_normalized_error:
            return "training_targets_met"
        if bounds.lower_nm == bounds.upper_nm:
            return "fixed_bounds"
        initial = np.array([r["thickness"]["value"] for r in seed["design"]["front_layers"]])
        rng = np.random.default_rng(effective_seed)
        population = rng.uniform(bounds.lower_nm, bounds.upper_nm, (request.population_size, len(initial)))
        population[0] = initial

        def objective(point: np.ndarray) -> float:
            nonlocal best, proposals, rejected
            _check_cancel(cancel_event)
            proposals += 1
            trial = deepcopy(seed["design"])
            for layer, thickness in zip(trial["front_layers"], point, strict=True):
                layer["thickness"] = dict(value=float(thickness), unit="nm")
            if not _feasible(request, trial, bounds):
                rejected += 1
                emit("proposal_rejected", "厚度提案超出显式制造约束，未调用物理解算")
                _check_cancel(cancel_event)
                return math.inf
            try:
                value = seed if np.array_equal(point, initial) else evaluate(trial)
            except (ValueError, ArithmeticError):
                emit("proposal_failed", "该提案数值失败，保留已存档候选并继续")
                _check_cancel(cancel_event)
                return math.inf
            if best is None or _rank(value) < _rank(best):
                best = deepcopy(value)
            emit("global_proposal", "固定材料和层序，仅评估厚度；未做局部精修")
            _check_cancel(cancel_event)
            if value["score"]["max_error"] <= shared.acceptable_normalized_error:
                raise SolverStop("training_targets_met")
            # Before the first passing proposal every score has the same
            # feasibility flag, so weighted RMS preserves the archive ordering.
            return float(value["score"]["weighted_rms_error"])

        stalled = 0
        previous = float(seed["score"]["weighted_rms_error"])
        try:
            with solver_type(objective,
                    [(bounds.lower_nm, bounds.upper_nm)] * len(initial),
                    strategy="best1bin", rng=rng, init=population, polish=False,
                    mutation=(0.5, 1.0), recombination=0.7, updating="immediate", workers=1,
                    tol=request.convergence_relative_tolerance, atol=0.0) as solver:
                while request.max_generations is None or generation < request.max_generations:
                    _check_cancel(cancel_event)
                    next(solver)
                    generation += 1
                    if best is None:
                        raise ValueError("global archive lost its seed")
                    current = float(best["score"]["weighted_rms_error"])
                    improved = previous - current > shared.relative_improvement_tolerance * max(abs(previous), 1e-30)
                    stalled = 0 if improved else stalled + 1
                    previous = current if improved else previous
                    emit("global_generation", "完成一代差分进化，检查离散度与最佳值停滞")
                    _check_cancel(cancel_event)
                    if solver.converged():
                        return "population_converged"
                    if stalled >= request.stagnation_generations:
                        return "global_stagnation"
        except SolverStop as exc:
            return exc.reason
        return "generation_limit"

    try:
        emit("freezing", "冻结环境、目标和真实材料，不修改用户设计")
        request = freeze_film_classic_global_request(service, request, cancel_event=cancel_event)
        bounds = _effective_bounds(request)
        catalog = {r["stable_id"]: r for r in template_catalog()}
        for spec in iter_classic_seed_specs(request.enumeration):
            _check_cancel(cancel_event)
            limit = request.enumeration.shared_request.max_evaluations
            if limit is not None and evaluations + 2 > limit:
                stop = "evaluation_limit"
                break
            current_id, best = spec.candidate_id, None
            generation = proposals = rejected = 0
            seed_value: Evaluation | None = None
            validation: Evaluation | None = None
            design: Mapping[str, Any] | None = None
            state, reason, message = "not_found", "global_stagnation", "尚未找到通过全部目标验证的膜系"
            before, failed_before = evaluations, failures
            effective_seed = int.from_bytes(hashlib.sha256(f"{request.seed}:{spec.candidate_id}".encode()).digest()[:8], "big")
            template = catalog[spec.template_id]
            emit("generating", "从当前目录按显式范围枚举经典起点；目录不代表所有经典结构")
            try:
                _check_cancel(cancel_event)
                preview = _preview(request.enumeration, spec)
                design = preview["candidate_design"]
                if design is None or not _feasible(request, design, bounds):
                    raise TemplateGenerationError("经典起点超出显式搜索/制造约束，未静默投影或截断")
                seed_value = evaluate(design)
                best = deepcopy(seed_value)
                emit("seed_scored", "保存经典基线，随后只搜索该候选的厚度")
                _check_cancel(cancel_event)
                reason = evolve(seed_value, bounds, effective_seed)
            except TemplateGenerationError as exc:
                state, reason, message = "inapplicable", "template_inapplicable", str(exc)
            except _Budget:
                reason, stop = "evaluation_limit", "evaluation_limit"
            except _StructureCancelled:
                state, reason, stop = "cancelled", "cancelled", "cancelled"
                message = "已取消，保留当前未验收的候选供检查"
            except (ValueError, ArithmeticError) as exc:
                state, reason, message = "failed", "numerical_failure", str(exc)
            if best is not None and state != "cancelled":
                try:
                    emit("validation", "独立密网格验证全部角度、偏振与上下限")
                    validation = evaluate(best["design"], holdout=True)
                    emit("validation_complete", "密网格已完成，在候选提交前检查取消")
                    _check_cancel(cancel_event)
                    tolerance = request.enumeration.shared_request.acceptable_normalized_error
                    if state != "failed" and best["score"]["max_error"] <= tolerance:
                        if validation["score"]["max_error"] <= tolerance:
                            state, reason, message = "completed", "validated_targets_met", "该候选通过训练和独立密网格，尚未应用"
                        else:
                            reason = "validation_targets_not_met"
                except _StructureCancelled:
                    state, reason, stop = "cancelled", "cancelled_during_validation", "cancelled"
                    message = "已取消，未提交该候选的达标状态"
                except (ValueError, ArithmeticError, _Budget) as exc:
                    state, reason, message = "failed", "validation_failed", str(exc)
            candidates.append(FilmClassicGlobalCandidate(spec, state, reason, deepcopy(design),
                None if best is None else deepcopy(best["design"]),
                None if seed_value is None else deepcopy(seed_value["score"]),
                None if best is None else deepcopy(best["score"]),
                None if validation is None else deepcopy(validation["score"]),
                _objective_status(request.enumeration.shared_request, seed_value, best, validation),
                state == "completed", validation is not None, evaluations - before,
                generation, proposals, rejected, failures - failed_before, bounds, effective_seed,
                template["applicability"], template["limitations"], message))
            emit("candidate_complete", "候选状态已提交，继续枚举剩余经典起点")
            _check_cancel(cancel_event)
            if stop in ("evaluation_limit", "cancelled"):
                break
    except _StructureCancelled:
        stop = "cancelled"
    except (ValueError, ArithmeticError) as exc:
        stop = "freeze_failed: " + str(exc)
    coverage = len(candidates) == planned
    any_met = any(c.targets_met for c in candidates)
    all_failed = coverage and any(c.state == "failed" for c in candidates) and all(
        c.state in ("failed", "inapplicable") for c in candidates)
    state = ("cancelled" if stop == "cancelled" else "failed" if stop.startswith("freeze_failed") else
        "partial" if not coverage else "failed" if all_failed else "completed" if any_met else "not_found")
    if all_failed and stop != "cancelled":
        stop = "all_applicable_candidates_failed"
    elif stop != "cancelled" and coverage and all(c.state == "inapplicable" for c in candidates):
        stop = "no_applicable_seed"
    def result_rank(candidate: FilmClassicGlobalCandidate) -> tuple[Any, ...]:
        score = candidate.validation_score if candidate.validation_score is not None else candidate.training_score
        if score is None:
            raise ValueError("ranking requires a complete score")
        return (not candidate.targets_met, not candidate.validation_complete,
            score["weighted_rms_error"], candidate.spec.candidate_id)
    ranked = sorted((c for c in candidates if c.training_score is not None), key=result_rank)
    message = ("本次经典目录和显式范围枚举完成，不代表所有经典结构或全局最优" if coverage else
               "枚举未完成，仅返回已尝试候选，不宣称覆盖全部显式范围")
    emit(state, message)
    return FilmClassicGlobalResult(state, stop, tuple(candidates), tuple(c.spec.candidate_id for c in ranked),
        planned, len(candidates), coverage, any_met, evaluations, validation_evaluations, failures, request, message)


def film_classic_global_request_to_payload(request: FilmClassicGlobalRequest) -> dict[str, Any]:
    request = _validate(request)
    return dict(schema_version=1, kind="coat_sim.film_classic_global_request", **{
        **asdict(request), "enumeration": film_classic_local_request_to_payload(request.enumeration)})


def film_classic_global_request_from_payload(payload: Mapping[str, Any]) -> FilmClassicGlobalRequest:
    allowed = {f.name for f in fields(FilmClassicGlobalRequest)}
    if (not isinstance(payload, Mapping) or set(payload) - allowed - {"schema_version", "kind"}
            or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1
            or payload.get("kind") != "coat_sim.film_classic_global_request"):
        raise ValueError("invalid classic global request payload")
    data = deepcopy(dict(payload))
    data.pop("schema_version")
    data.pop("kind")
    data["enumeration"] = film_classic_local_request_from_payload(data["enumeration"])
    data["thickness_bounds"] = FilmGlobalThicknessBounds(**data["thickness_bounds"])
    return _validate(FilmClassicGlobalRequest(**data))
