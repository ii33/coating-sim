"""Independent conservative film reduction over an immutable complete design.

The deterministic search uses nominal feasibility followed by a
lexicographic physical reduction key. It never relaxes an optical objective.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from typing import Any, Callable, Mapping

from .film_design import validate_design
from .film_objective_payload import condition_from_payload, coverage_from_payload
from .film_optimization import (FilmOptimizationRequest, FilmOptimizationResult,
    FilmOptimizationProgress, _checked_frozen_snapshot, _validate_optimization_controls,
    freeze_film_optimization_request)
from .film_optimization_needle import _physical_layer_thickness

REQUEST_KIND = "coat_sim.film_reduction_request"
RESULT_KIND = "coat_sim.film_reduction_result"
MODES = ("layer_count", "thickness", "combined")


@dataclass(frozen=True)
class FilmReductionRequest:
    base_request: FilmOptimizationRequest
    mode: str = "combined"
    max_iterations: int | None = None
    max_evaluations: int | None = None
    # Preserve exact historical v2 envelopes and their finite frozen budgets.
    # Newly constructed requests always use the independent optional v3 limits.
    _payload_version: int = 3


@dataclass(frozen=True)
class FilmReductionResult:
    base_result: FilmOptimizationResult
    frozen_request: FilmReductionRequest
    metrics: Mapping[str, Any]
    summary: Mapping[str, Any]
    topology_audit: Mapping[str, Any]
    request_digest: str
    candidate_verified: bool = True
    is_final: bool = True

    @property
    def state(self):
        return self.base_result.state

    @property
    def best_design(self):
        return self.base_result.best_design

    @property
    def best_thickness_nm(self):
        return self.base_result.best_thickness_nm

    @property
    def message(self):
        return self.base_result.message


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _validate_request(request):
    from .film_optimization_job import _validate_replay_request
    if not isinstance(request, FilmReductionRequest) or request.mode not in MODES:
        raise ValueError("film reduction mode must be layer_count, thickness or combined")
    base = request.base_request
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("reduction base_request must be a FilmOptimizationRequest")
    _validate_optimization_controls(base)
    _validate_replay_request(base)
    if base.validation_samples_multiplier < 2:
        raise ValueError("reduction requires validation_samples_multiplier>=2 for independent coverage")
    if type(request._payload_version) is not int or request._payload_version not in (2, 3):
        raise ValueError("unsupported reduction request version")
    for name, minimum in (("max_iterations", 1), ("max_evaluations", 2)):
        value = getattr(request, name)
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError(f"reduction {name} must be an integer >= {minimum} or None")
    if request._payload_version == 2 and (
            request.max_iterations != base.max_iterations or request.max_evaluations != base.max_evaluations):
        raise ValueError("historical reduction budgets must match their frozen base request")
    if base.execution_device is not None or base.execution_mode != "serial":
        raise ValueError("film reduction currently requires serial CPU evaluation")
    design = validate_design(deepcopy(dict(base.design_snapshot)))
    for variable in base.variables:
        for bound in (variable.lower_nm, variable.upper_nm):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound)):
                raise ValueError("reduction thickness bounds must be finite numbers or an explicit unbounded upper")
        if not any(row["id"] == variable.layer_id for row in design[variable.side + "_layers"]):
            raise ValueError("selected reduction layer is absent from its frozen side")
    if base.frozen_material_snapshot is not None:
        _checked_frozen_snapshot(base.frozen_material_snapshot, design)
        validate_film_reduction_candidate_topology(request, design)


def freeze_film_reduction_request(service, request: FilmReductionRequest) -> FilmReductionRequest:
    _validate_request(request)
    frozen = replace(request, base_request=freeze_film_optimization_request(service, request.base_request))
    _validate_request(frozen)
    return frozen


def film_reduction_request_to_payload(request: FilmReductionRequest) -> dict[str, Any]:
    from .film_optimization_job import serialize_request
    _validate_request(request)
    payload = {"schema_version": request._payload_version, "kind": REQUEST_KIND, "mode": request.mode,
               "base_request": serialize_request(request.base_request)}
    if request._payload_version == 3:
        payload["limits"] = {"max_iterations": request.max_iterations, "max_evaluations": request.max_evaluations}
    return payload


def film_reduction_request_from_payload(payload: Mapping[str, Any]) -> FilmReductionRequest:
    from .film_optimization_job import deserialize_request
    if not isinstance(payload, Mapping):
        raise ValueError("film reduction request must be a mapping")
    version = payload.get("schema_version")
    fields = {"schema_version", "kind", "mode", "base_request"}
    if version == 3:
        fields.add("limits")
    if (set(payload) != fields or type(version) is not int or version not in (2, 3) or
            payload["kind"] != REQUEST_KIND):
        raise ValueError("film reduction request kind/version/fields are invalid")
    base_payload = payload["base_request"]
    if not isinstance(base_payload, Mapping) or base_payload.get("kind") != "coat_sim.film_optimization_request":
        raise ValueError("reduction base_request must use the independent fixed-thickness request kind")
    base_request = deserialize_request(base_payload)
    if not isinstance(base_request, FilmOptimizationRequest):
        raise ValueError("reduction base_request must be a FilmOptimizationRequest")
    limits = payload.get("limits", {"max_iterations": base_request.max_iterations,
                                    "max_evaluations": base_request.max_evaluations})
    if not isinstance(limits, Mapping) or set(limits) != {"max_iterations", "max_evaluations"}:
        raise ValueError("reduction limits require exactly max_iterations and max_evaluations")
    request = FilmReductionRequest(base_request, payload["mode"],
        limits["max_iterations"], limits["max_evaluations"], version)
    _validate_request(request)
    return request


def _physical(request, design):
    frozen = request.base_request.frozen_material_snapshot
    if frozen is None:
        raise ValueError("reduction requires the complete frozen material snapshot")
    return {row["id"]: float(_physical_layer_thickness(row, design, frozen))
            for side in ("front", "back") for row in design[side + "_layers"]}


def validate_film_reduction_candidate_topology(request: FilmReductionRequest,
                                              design: Mapping[str, Any]) -> dict[str, Any]:
    """Check exact source projection, metadata, fixed rows and retained bounds.

    A deletion is legal only for a selected zero-lower-bound row. A surviving
    selected row may be repaired anywhere inside its own original bounds.
    Merge proposals require exact material and optical-set identity; fixed
    rows and interfaces are never merged or adjusted.
    """
    base = validate_design(deepcopy(dict(request.base_request.design_snapshot)))
    candidate = validate_design(deepcopy(dict(design)))
    variables = {row.layer_id: row for row in request.base_request.variables}
    before_nm, after_nm = _physical(request, base), _physical(request, candidate)
    for key in set(base) | set(candidate):
        if key in ("front_layers", "back_layers"):
            continue
        if key == "expression":
            original, changed = deepcopy(base[key]), deepcopy(candidate[key])
            if original.get("text") and original.get("in_sync") and changed.get("in_sync") is False:
                original["in_sync"] = False
            if original == changed:
                continue
        if base.get(key) != candidate.get(key):
            raise ValueError(f"non-layer design field {key!r} changed")
    removed = []
    for side in ("front", "back"):
        source = base[side + "_layers"]
        rows = candidate[side + "_layers"]
        original = {row["id"]: row for row in source}
        ids = [row["id"] for row in rows]
        if any(identifier not in original for identifier in ids) or ids != [
                row["id"] for row in source if row["id"] in set(ids)]:
            raise ValueError("reduction must preserve source layer IDs, side and order")
        missing = [row["id"] for row in source if row["id"] not in set(ids)]
        if request.mode == "thickness" and missing:
            raise ValueError("thickness reduction must preserve topology")
        for identifier in missing:
            if identifier not in variables or variables[identifier].lower_nm != 0:
                raise ValueError("only selected layers with lower_nm==0 may be deleted")
        removed.extend(missing)
        for row in rows:
            identifier = row["id"]
            initial = original[identifier]
            variable = variables.get(identifier)
            if variable is None:
                if row != initial:
                    raise ValueError("fixed/unselected layer was changed")
                continue
            if {k: v for k, v in row.items() if k != "thickness"} != {
                    k: v for k, v in initial.items() if k != "thickness"}:
                raise ValueError("selected layer metadata/material changed")
            value = after_nm[identifier]
            if value < variable.lower_nm - 1e-10 or (variable.upper_nm is not None and
                    value > variable.upper_nm + 1e-10):
                raise ValueError("retained selected layer violates its own bounds")
    if request.mode != "layer_count" and sum(after_nm.values()) > sum(before_nm.values()) + 1e-8:
        raise ValueError("thickness/combined reduction cannot increase source total physical thickness")
    return {"source_layer_ids": list(before_nm), "candidate_layer_ids": list(after_nm),
            "removed_layer_ids": removed, "selected_bounds_satisfied": True,
            "source_order_preserved": True, "fixed_layers_preserved": True,
            "layer_thickness_nm": after_nm}


def _metric(request, design, train, validation):
    physical = _physical(request, design)
    result = {"layer_count": len(physical), "total_thickness_nm": sum(physical.values()),
        "min_thickness_nm": min(physical.values(), default=0.0),
        **deepcopy(train), "validation": deepcopy(validation),
        "validation_complete": True, "validation_targets_met": validation["targets_met"],
        "validation_max_normalized_error": validation["max_normalized_error"]}
    for side in ("front", "back"):
        values = [physical[row["id"]] for row in design[side + "_layers"]]
        result[side] = {"layer_count": len(values), "total_thickness_nm": sum(values),
                        "min_thickness_nm": min(values, default=0.0)}
    return result


def _key(request, metric):
    if request.mode == "thickness":
        return (metric["total_thickness_nm"],)
    return (metric["layer_count"], metric["total_thickness_nm"])


def _feasible(metric):
    return metric["targets_met"] is True and metric["validation_targets_met"] is True


def _qualifies(request, baseline, candidate):
    if not _feasible(candidate):
        return False
    count, original_count = candidate["layer_count"], baseline["layer_count"]
    thickness, original_thickness = candidate["total_thickness_nm"], baseline["total_thickness_nm"]
    if request.mode == "layer_count":
        return count < original_count
    if request.mode == "thickness":
        return count == original_count and thickness < original_thickness - 1e-8
    return (count <= original_count and thickness <= original_thickness + 1e-8 and
            (count < original_count or thickness < original_thickness - 1e-8))


def _summary(request, baseline, candidate):
    return {"mode": request.mode, "source_feasible": _feasible(baseline),
        "candidate_feasible": _feasible(candidate),
        "reduction_achieved": _qualifies(request, baseline, candidate),
        "removed_layer_count": baseline["layer_count"] - candidate["layer_count"],
        "saved_thickness_nm": baseline["total_thickness_nm"] - candidate["total_thickness_nm"]}


def _base_result(request, design, metric, state, iterations, evaluations, message,
                 validation_evaluations=None, termination_reason=None):
    train, validation = metric, metric["validation"]
    return FilmOptimizationResult(state=state, design_revision=request.base_request.design_revision,
        best_design=deepcopy(design), best_thickness_nm=_physical(request, design),
        max_normalized_error=train["max_normalized_error"],
        weighted_max_normalized_error=train["weighted_max_normalized_error"],
        weighted_rms_error=train["weighted_rms_error"], targets_met=train["targets_met"],
        solver_acceptable=train["solver_acceptable"],
        per_condition_errors=tuple(condition_from_payload(row) for row in train["per_condition_errors"]),
        raw_coverage=tuple(coverage_from_payload(row) for row in train["raw_coverage"]),
        iterations=iterations, evaluations=evaluations, message=message,
        solver="baseline", seed=request.base_request.seed, termination_reason=termination_reason or state,
        validation_samples_multiplier=request.base_request.validation_samples_multiplier,
        validation_evaluations=(evaluations // 2 if validation_evaluations is None else validation_evaluations), validation_complete=True,
        validation_max_normalized_error=validation["max_normalized_error"],
        validation_weighted_rms_error=validation["weighted_rms_error"],
        validation_targets_met=validation["targets_met"],
        validation_solver_acceptable=validation["solver_acceptable"],
        validation_per_condition_errors=tuple(condition_from_payload(row) for row in validation["per_condition_errors"]),
        validation_raw_coverage=tuple(coverage_from_payload(row) for row in validation["raw_coverage"]),
        validation_message="Complete independent grid; source and candidate use identical grids")


class _Cancelled(Exception):
    pass


class _BudgetReached(Exception):
    pass


def _cancelled(event):
    return event is not None and event.is_set()


def _evaluate(service, request, design, event=None, evaluation_started=None):
    from .film_optimization_job import _replay_candidate_scores, _validation_objectives
    class Interruptible:
        def simulate(self, candidate, settings):
            if _cancelled(event):
                raise _Cancelled()
            try:
                result = service.simulate(candidate, settings, cancel_event=event)
            except InterruptedError as exc:
                if _cancelled(event):
                    raise _Cancelled() from exc
                raise
            if _cancelled(event):
                raise _Cancelled()
            return result
    proxy = Interruptible()
    base = request.base_request
    if evaluation_started is not None:
        evaluation_started(False)
    train = _replay_candidate_scores(proxy, design, base.objectives,
                                     tolerance=base.acceptable_normalized_error)
    if evaluation_started is not None:
        evaluation_started(True)
    validation = _replay_candidate_scores(proxy, design, _validation_objectives(base),
                                          tolerance=base.acceptable_normalized_error)
    return _metric(request, design, train, validation)


def _repair_candidate(service, request, design, event, evaluation_started,
                      iteration_advanced, evaluation_budget, iteration_budget):
    """Reuse the existing bounded thickness solver before topology acceptance.

    The service wrapper counts complete declared-grid starts, not individual
    wavelength chunks. The solver's own complete holdout is reused as the
    independent audit; no terminal optical replay is added by this workflow.
    """
    from .film_optimization import optimize_film_design
    from .film_optimization_job import _snapshot_for_candidate, _validation_objectives
    base = request.base_request
    ids = {row["id"] for side in ("front", "back") for row in design[side + "_layers"]}
    variables = tuple(variable for variable in base.variables if variable.layer_id in ids and
        (variable.side == "front" or design["substrate"]["sides"] == "double"))
    if (not variables or (iteration_budget is not None and iteration_budget < 1) or
            (evaluation_budget is not None and evaluation_budget < 3)):
        limited = (None if not variables else "iteration_limit" if iteration_budget == 0 else "evaluation_budget")
        return deepcopy(design), _evaluate(service, request, design, event, evaluation_started), limited
    # Reserve one complete independent grid only when the user chose a cap.
    # No per-topology iteration/evaluation quota truncates numerical repair.
    training_budget = None if evaluation_budget is None else evaluation_budget - 1
    repair_request = replace(base, design_snapshot=deepcopy(design), variables=variables,
        frozen_material_snapshot=_snapshot_for_candidate(base, design), solver="baseline",
        acceptable_normalized_error=0.0, execution_mode="serial", execution_device=None)
    repair_service = copy(service)
    phase = {"validation": False, "frame": 0, "iteration": 0}
    frame_counts = {
        False: sum(len(o.angles_deg) * ((o.wavelength_samples + 10000) // 10001) for o in base.objectives),
        True: sum(len(o.angles_deg) * ((o.wavelength_samples + 10000) // 10001) for o in _validation_objectives(base)),
    }
    def progress(value):
        delta = value.iteration - phase["iteration"]
        if delta > 0:
            iteration_advanced(delta)
            phase["iteration"] = value.iteration
        # optimize_film_design emits training progress only through 0.90,
        # then emits running/0.93 immediately before its holdout evaluate().
        # Its serial evaluate loop is objectives -> angles -> 10001 chunks;
        # protocol regressions cover both this phase marker and chunk counts.
        if value.state == "running" and value.fraction == 0.93:
            phase["validation"], phase["frame"] = True, 0
    def simulate(candidate, settings):
        if _cancelled(event):
            raise _Cancelled()
        if phase["frame"] == 0:
            evaluation_started(phase["validation"])
        try:
            response = service.simulate(candidate, settings, cancel_event=event)
        except Exception as exc:
            phase["frame"] = 0
            if _cancelled(event):
                raise _Cancelled() from exc
            raise
        if _cancelled(event):
            raise _Cancelled()
        phase["frame"] = (phase["frame"] + 1) % frame_counts[bool(phase["validation"])]
        return response
    repair_service.simulate = simulate
    repaired = optimize_film_design(repair_service, repair_request, cancel_event=event,
                                   progress_callback=progress,
                                   baseline_limits=(iteration_budget, training_budget))
    if repaired.state == "cancelled" or _cancelled(event):
        raise _Cancelled()
    if not repaired.validation_complete:
        raise ValueError("survivor repair did not complete its independent grid")
    from dataclasses import asdict
    from .film_objective_payload import coverage_to_payload
    def score(validation):
        from .film_objective_criteria import reduce_objective_responses
        coverage = repaired.validation_raw_coverage if validation else repaired.raw_coverage
        objectives = _validation_objectives(base) if validation else base.objectives
        reduction = reduce_objective_responses(objectives, [{"objective_index": row.objective_index,
            "angle_deg": row.angle_deg, "wavelength_nm": row.wavelength_nm,
            "actual_values": row.actual_values} for row in coverage], tolerance=base.acceptable_normalized_error)
        return {"max_normalized_error": reduction.max_error,
            "weighted_max_normalized_error": reduction.weighted_max_error,
            "weighted_rms_error": reduction.weighted_rms_error,
            "targets_met": reduction.nominal_pass, "solver_acceptable": reduction.solver_acceptable,
            "per_condition_errors": [asdict(row) for row in reduction.conditions],
            "raw_coverage": [coverage_to_payload(row) for row in reduction.coverage]}
    limited = repaired.termination_reason if repaired.termination_reason in {"iteration_limit", "evaluation_budget"} else None
    return deepcopy(dict(repaired.best_design)), _metric(request, repaired.best_design, score(False), score(True)), limited


def _changed(design):
    expression = design.get("expression", {})
    if expression.get("text") and expression.get("in_sync"):
        expression["in_sync"] = False
    return design


def _proposals(request, design, step_fraction):
    variables = {v.layer_id: v for v in request.base_request.variables}
    physical = _physical(request, design)
    if request.mode != "thickness":
        for side in ("front", "back"):
            rows = design[side + "_layers"]
            for index, row in enumerate(rows):
                variable = variables.get(row["id"])
                if variable is None or variable.lower_nm != 0:
                    continue
                candidate = deepcopy(design)
                del candidate[side + "_layers"][index]
                yield _changed(candidate)
                for other_index in (index - 1, index + 1):
                    if not 0 <= other_index < len(rows):
                        continue
                    other = rows[other_index]
                    survivor = variables.get(other["id"])
                    if survivor is None or survivor.lower_nm != 0 or (row["material_id"], row["optical_set_id"]) != (
                            other["material_id"], other["optical_set_id"]):
                        continue
                    total = physical[row["id"]] + physical[other["id"]]
                    if survivor.upper_nm is not None and total > survivor.upper_nm:
                        continue
                    candidate = deepcopy(design)
                    candidate[side + "_layers"][other_index]["thickness"] = {"value": total, "unit": "nm"}
                    del candidate[side + "_layers"][index]
                    yield _changed(candidate)
    if request.mode == "layer_count":
        return
    for side in ("front", "back"):
        for index, row in enumerate(design[side + "_layers"]):
            variable = variables.get(row["id"])
            if variable is None:
                continue
            value = physical[row["id"]]
            low = variable.lower_nm
            span = (variable.upper_nm if variable.upper_nm is not None else value) - low
            # Persistent rows stay positive. Zero is represented only by an
            # explicit legal deletion in the layer/combined modes.
            for target in (low, max(low, value - span * step_fraction)):
                if target <= 0 or target >= value - 1e-10:
                    continue
                candidate = deepcopy(design)
                candidate[side + "_layers"][index]["thickness"] = {"value": target, "unit": "nm"}
                yield _changed(candidate)


def reduce_film_design(service, request: FilmReductionRequest, *, cancel_event=None,
        progress_callback: Callable | None = None, checkpoint_callback: Callable | None = None,
        candidate_observer=None) -> FilmReductionResult:
    """Serial reduction until natural termination, cancellation or optional user limits."""
    from .film_optimization_job import _service_with_frozen_materials
    request = freeze_film_reduction_request(service, request)
    base = request.base_request
    frozen_snapshot = base.frozen_material_snapshot
    assert frozen_snapshot is not None
    service = _service_with_frozen_materials(service, frozen_snapshot)
    best = deepcopy(dict(base.design_snapshot))
    if _cancelled(cancel_event):
        raise InterruptedError("cancelled before a complete audited source")
    evaluations, validation_evaluations = 0, 0
    def evaluation_started(validation):
        nonlocal evaluations, validation_evaluations
        if _cancelled(cancel_event):
            raise _Cancelled()
        if request.max_evaluations is not None and evaluations >= request.max_evaluations:
            raise _BudgetReached("evaluation_budget")
        evaluations += 1
        validation_evaluations += int(validation)
    try:
        baseline = _evaluate(service, request, best, cancel_event, evaluation_started)
    except _Cancelled as exc:
        raise InterruptedError("cancelled before a complete audited source") from exc
    metric, iterations = baseline, 0
    digest = _digest(film_reduction_request_to_payload(request))
    def iteration_advanced(count):
        nonlocal iterations
        iterations += count
    reason = "incumbent"
    def result(state, message, final=True):
        return FilmReductionResult(_base_result(request, best, metric, state,
                iterations, evaluations, message, validation_evaluations,
                state if request._payload_version == 2 else reason), request,
            {"baseline": deepcopy(baseline), "candidate": deepcopy(metric)},
            _summary(request, baseline, metric),
            validate_film_reduction_candidate_topology(request, best), digest, True, final)
    def publish():
        if checkpoint_callback is not None:
            checkpoint_callback(result("completed", "Complete audited incumbent", False))
        if candidate_observer is not None:
            observe = getattr(candidate_observer, "observe", None)
            if callable(observe):
                observe(deepcopy(best))
    publish()
    step = base.initial_step_fraction
    tested_topologies = set()
    state, message = "completed", "Reduction search completed"
    def check_limits():
        if _cancelled(cancel_event):
            raise _Cancelled()
        if request.max_iterations is not None and iterations >= request.max_iterations:
            raise _BudgetReached("iteration_limit")
        if request.max_evaluations is not None and evaluations + 2 > request.max_evaluations:
            raise _BudgetReached("evaluation_budget")
    try:
        while True:
            check_limits()
            iterations += 1
            improved = False
            seen = set()
            for candidate in _proposals(request, best, step):
                if _cancelled(cancel_event):
                    raise _Cancelled()
                if request.max_evaluations is not None and evaluations + 2 > request.max_evaluations:
                    raise _BudgetReached("evaluation_budget")
                fingerprint = _digest(candidate)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                validate_film_reduction_candidate_topology(request, candidate)
                topology_changed = sum(len(candidate[side + "_layers"]) for side in ("front", "back")) < metric["layer_count"]
                repair_stop = None
                if topology_changed:
                    # Thickness step reduction does not change these proposals.
                    # A new incumbent invalidates this local-search cache below.
                    if fingerprint in tested_topologies:
                        continue
                    tested_topologies.add(fingerprint)
                    candidate, candidate_metric, repair_stop = _repair_candidate(service, request, candidate, cancel_event,
                        evaluation_started, iteration_advanced,
                        None if request.max_evaluations is None else request.max_evaluations - evaluations,
                        None if request.max_iterations is None else request.max_iterations - iterations)
                else:
                    candidate_metric = _evaluate(service, request, candidate, cancel_event, evaluation_started)
                if _qualifies(request, baseline, candidate_metric) and (
                        not _feasible(metric) or _key(request, candidate_metric) < _key(request, metric)):
                    validate_film_reduction_candidate_topology(request, candidate)
                    best, metric = candidate, candidate_metric
                    tested_topologies.clear()
                    improved = True
                    publish()
                    if repair_stop:
                        raise _BudgetReached(repair_stop)
                    break
                if repair_stop:
                    raise _BudgetReached(repair_stop)
                if request.max_iterations is not None and iterations >= request.max_iterations:
                    raise _BudgetReached("iteration_limit")
            if progress_callback is not None:
                progress_callback(FilmOptimizationProgress("running",
                    max(iterations / request.max_iterations if request.max_iterations else 0.0,
                        evaluations / request.max_evaluations if request.max_evaluations else 0.0),
                    iterations, evaluations, metric["max_normalized_error"], "Conservative film reduction"))
            if not improved:
                if not seen or request.mode == "layer_count":
                    reason, message = "proposal_exhausted", "All current legal topology proposals exhausted without improvement"
                    break
                step *= 0.5
                if step < base.min_step_fraction:
                    reason, message = "step_tolerance", "Reduction step tolerance reached; no further local improvement found"
                    break
        if _cancelled(cancel_event):
            raise _Cancelled()
    except _Cancelled:
        reason, state, message = "cancelled", "cancelled", "Cancelled; last complete audited candidate retained"
    except _BudgetReached as exc:
        reason = str(exc)
        label = "Evaluation" if reason == "evaluation_budget" else "Iteration"
        message = f"{label} budget reached (user configured); last complete audited candidate retained; search not exhausted"
    except Exception as exc:
        reason, state, message = "failed", "failed", f"Reduction failed; last complete audited candidate retained: {exc}"
    if not _feasible(baseline):
        message += "; source is infeasible on original/independent grids; original unmet status preserved"
    return result(state, message)


def film_reduction_result_to_payload(request: FilmReductionRequest,
                                      result: FilmReductionResult) -> dict[str, Any]:
    from .film_optimization_job import serialize_result
    frozen_payload = film_reduction_request_to_payload(request)
    if (request != result.frozen_request or result.request_digest != _digest(frozen_payload)):
        raise ValueError("reduction result is not bound to this frozen source request")
    payload = serialize_result(request.base_request, result.base_result)
    payload.update(kind=RESULT_KIND, request=frozen_payload, schema_version=request._payload_version)
    payload["reduction"] = {"metrics": deepcopy(dict(result.metrics)),
        "summary": deepcopy(dict(result.summary)), "topology_audit": deepcopy(dict(result.topology_audit)),
        "request_digest": result.request_digest, "is_final": result.is_final}
    payload["partial_result"]["candidate_verified"] = result.candidate_verified
    _json(payload)
    return payload


def _check_equal(actual, expected, label):
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or set(actual) != set(expected):
            raise ValueError(f"{label} fields differ")
        for key in expected:
            _check_equal(actual[key], expected[key], label + "." + key)
    elif isinstance(expected, (tuple, list)):
        if not isinstance(actual, (tuple, list)) or len(actual) != len(expected):
            raise ValueError(f"{label} coverage/length differs")
        for left, right in zip(actual, expected, strict=True):
            _check_equal(left, right, label)
    elif type(expected) is bool or expected is None or isinstance(expected, str):
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(f"{label} differs")
    elif isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12):
            raise ValueError(f"{label} number differs")
    elif actual != expected:
        raise ValueError(f"{label} differs")


def film_reduction_result_from_payload(payload: Mapping[str, Any]) -> FilmReductionResult:
    """Strict structural, objective-coverage and topology checks, without optics.

    Imported data still requires validate_film_reduction_payload before applying;
    a digest is a source binding, not an authentication or optical trust token.
    """
    from .film_optimization_job import _validation_objectives
    from .film_objective_criteria import reduce_objective_responses
    if (not isinstance(payload, Mapping) or set(payload) != {"schema_version", "kind", "request",
            "candidate", "partial_result", "solver_validation", "state", "message", "reduction"} or
            type(payload["schema_version"]) is not int or payload["schema_version"] not in (2, 3) or
            payload["kind"] != RESULT_KIND or payload["state"] not in ("completed", "failed", "cancelled") or
            not isinstance(payload["message"], str)):
        raise ValueError("film reduction result kind/version/fields are invalid")
    request = film_reduction_request_from_payload(payload["request"])
    if payload["schema_version"] != request._payload_version:
        raise ValueError("reduction result/request versions differ")
    metadata = payload["reduction"]
    if (not isinstance(metadata, Mapping) or set(metadata) != {"metrics", "summary", "topology_audit",
            "request_digest", "is_final"} or type(metadata["is_final"]) is not bool or
            metadata["request_digest"] != _digest(payload["request"])):
        raise ValueError("film reduction result source binding/metadata is invalid")
    candidate = payload["candidate"]
    if not isinstance(candidate, Mapping) or not isinstance(candidate.get("design"), Mapping):
        raise ValueError("film reduction candidate is invalid")
    design = validate_design(deepcopy(dict(candidate["design"])))
    audit = validate_film_reduction_candidate_topology(request, design)
    _check_equal(metadata["topology_audit"], audit, "topology audit")
    metrics = metadata["metrics"]
    if not isinstance(metrics, Mapping) or set(metrics) != {"baseline", "candidate"}:
        raise ValueError("reduction metrics require source and candidate")
    def rescore(record, objectives):
        if not isinstance(record, Mapping) or not isinstance(record.get("raw_coverage"), list):
            raise ValueError("complete raw spectral coverage is required")
        coverage = [coverage_from_payload(row) for row in record["raw_coverage"]]
        import numpy as np
        expected = [(i, angle, tuple(np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples)))
            for i, o in enumerate(objectives) for angle in o.angles_deg]
        if [(r.objective_index, r.angle_deg, r.wavelength_nm) for r in coverage] != expected:
            raise ValueError("raw coverage does not match the complete original/independent grid")
        responses = [{"objective_index": r.objective_index, "angle_deg": r.angle_deg,
            "wavelength_nm": r.wavelength_nm, "actual_values": r.actual_values} for r in coverage]
        reduced = reduce_objective_responses(objectives, responses,
            tolerance=request.base_request.acceptable_normalized_error)
        from dataclasses import asdict
        from .film_objective_payload import coverage_to_payload
        return {"max_normalized_error": reduced.max_error,
            "weighted_max_normalized_error": reduced.weighted_max_error,
            "weighted_rms_error": reduced.weighted_rms_error, "targets_met": reduced.nominal_pass,
            "solver_acceptable": reduced.solver_acceptable,
            "per_condition_errors": [asdict(r) for r in reduced.conditions],
            "raw_coverage": [coverage_to_payload(r) for r in reduced.coverage]}
    canonical_metrics = {}
    for name, source_design in (("baseline", request.base_request.design_snapshot), ("candidate", design)):
        record = metrics[name]
        train = rescore(record, request.base_request.objectives)
        validation = rescore(record.get("validation"), _validation_objectives(request.base_request))
        canonical_metrics[name] = _metric(request, source_design, train, validation)
        _check_equal(record, canonical_metrics[name], name + " metrics")
    summary = _summary(request, canonical_metrics["baseline"], canonical_metrics["candidate"])
    _check_equal(metadata["summary"], summary, "summary")
    for key in ("iterations", "evaluations"):
        if type(candidate.get(key)) is not int or candidate[key] < (0 if key == "iterations" else 2):
            raise ValueError("candidate counters are invalid")
    if ((request.max_iterations is not None and candidate["iterations"] > request.max_iterations) or
            (request.max_evaluations is not None and candidate["evaluations"] > request.max_evaluations)):
        raise ValueError("candidate counters exceed frozen budget")
    partial = payload["partial_result"]
    if not isinstance(partial, Mapping) or type(partial.get("candidate_verified")) is not bool:
        raise ValueError("candidate verification flag is invalid")
    if not isinstance(payload["solver_validation"], Mapping):
        raise ValueError("solver validation metadata must be a mapping")
    validation_evaluations = payload["solver_validation"].get("validation_evaluations")
    if type(validation_evaluations) is not int or not 1 <= validation_evaluations <= candidate["evaluations"] - 1:
        raise ValueError("validation evaluation count is invalid")
    reason = payload["solver_validation"].get("termination_reason")
    if request._payload_version == 3:
        allowed_reasons = ({"incumbent"} if not metadata["is_final"] else
            {"proposal_exhausted", "step_tolerance", "evaluation_budget", "iteration_limit"})
        if payload["state"] != "completed":
            allowed_reasons = {payload["state"]}
        if not isinstance(reason, str) or reason not in allowed_reasons:
            raise ValueError("reduction termination reason is invalid for its state")
        if ((reason == "evaluation_budget" and request.max_evaluations is None) or
                (reason == "iteration_limit" and request.max_iterations is None)):
            raise ValueError("reduction budget termination requires an explicit frozen limit")
    else:
        reason = payload["state"]
    result = FilmReductionResult(_base_result(request, design, canonical_metrics["candidate"],
        payload["state"], candidate["iterations"], candidate["evaluations"], payload["message"], validation_evaluations, reason),
        request, canonical_metrics, summary, audit, metadata["request_digest"],
        partial["candidate_verified"], metadata["is_final"])
    _check_equal(payload, film_reduction_result_to_payload(request, result), "reduction result")
    return result


def validate_film_reduction_payload(service, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Optically replay both source and candidate against identical frozen grids."""
    from .film_optimization_job import _service_with_frozen_materials
    result = film_reduction_result_from_payload(payload)
    request = result.frozen_request
    frozen_snapshot = request.base_request.frozen_material_snapshot
    assert frozen_snapshot is not None
    service = _service_with_frozen_materials(service, frozen_snapshot)
    for name, design in (("baseline", request.base_request.design_snapshot), ("candidate", result.best_design)):
        _check_equal(result.metrics[name], _evaluate(service, request, design), name + " frozen optical replay")
    return film_reduction_result_to_payload(request, replace(result, candidate_verified=True))


def validate_film_reduction_result(service, request: FilmReductionRequest,
                                   result: FilmReductionResult) -> FilmReductionResult:
    return film_reduction_result_from_payload(validate_film_reduction_payload(service,
        film_reduction_result_to_payload(request, result)))
