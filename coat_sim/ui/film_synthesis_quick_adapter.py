"""Pure UI adaptation for the frozen v3 synthesis protocol; no solver changes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from typing import Any, Mapping
from uuid import uuid4

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, _validated
from coat_sim.core.film_optimization import FilmConditionError, FilmOptimizationRequest, FilmOptimizationResult
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import is_synthesis_result
from coat_sim.core.film_synthesis_forward import validate_forward_device

SYNTHESIS_DEFAULT_OPTIONS = dict.fromkeys(("max_cycles", "max_evaluations", "max_layers",
    "max_seconds", "minimum_nonzero_thickness_nm", "max_layer_thickness_nm",
    "max_total_thickness_nm"))


SYNTHESIS_SEARCH_DEFAULTS = {
    "search_strategy": "hybrid", "objective_weighting": "equal_window",
    "archive_capacity": 32, "needle_batch_size": 6,
    "needle_pre_local": False, "needle_position_refinement_rounds": 1,
    "max_working_set_bytes": 0,
    "parallel_batch_size": None,
}


def synthesis_options(values: Mapping[str, Any]) -> dict[str, Any]:
    if set(values) - (set(SYNTHESIS_DEFAULT_OPTIONS) | set(SYNTHESIS_SEARCH_DEFAULTS)):
        raise ValueError("Unknown synthesis constraint")
    result = dict(SYNTHESIS_DEFAULT_OPTIONS)
    for name, raw in values.items():
        if name == "parallel_batch_size" and (raw is None or (isinstance(raw, str)
                and (not raw.strip() or raw.strip().lower() == "auto"))):
            result[name] = None
            continue
        if name == "max_working_set_bytes" and (raw is None or (isinstance(raw, str) and not raw.strip())):
            result[name] = None  # Explicit off, distinct from 0=auto and an omitted default.
            continue
        if name in {"search_strategy", "objective_weighting"}:
            choices = ({"hybrid", "legacy"} if name == "search_strategy"
                       else {"equal_window", "point_count"})
            if raw not in choices:
                raise ValueError(f"{name} must be one of {', '.join(sorted(choices))}")
            result[name] = raw
            continue
        if name == "needle_pre_local":
            if type(raw) is not bool:
                raise ValueError("needle_pre_local must be an explicit boolean")
            result[name] = raw
            continue
        if name in {"archive_capacity", "needle_batch_size", "needle_position_refinement_rounds", "max_working_set_bytes", "parallel_batch_size"}:
            if isinstance(raw, bool):
                raise ValueError(f"{name} requires an integer")
            number = float(raw)
            minimum = (0 if name in {"needle_position_refinement_rounds", "max_working_set_bytes"}
                       else 2 if name == "archive_capacity" else 1)
            if not math.isfinite(number) or not number.is_integer() or not minimum <= number <= (1 << 53) - 1:
                raise ValueError(f"{name} requires an exactly representable integer >= {minimum}")
            result[name] = int(number)
            continue
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if isinstance(raw, bool):
            raise ValueError(f"{name} requires a positive number or an empty value")
        number = float(raw)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} requires a positive number or an empty value")
        if name in {"max_cycles", "max_evaluations", "max_layers"}:
            if not number.is_integer() or number > (1 << 53) - 1:
                raise ValueError(f"{name} requires an exactly representable positive integer")
            result[name] = int(number)
        else:
            result[name] = number
    return result


def synthesis_cpu_workers(value: Any) -> int:
    """Parse the UI's independent inner-worker setting without applying core limits."""
    if isinstance(value, bool):
        raise ValueError("CPU work processes must be a nonnegative integer")
    if isinstance(value, int):
        workers = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        workers = int(value.strip())
    else:
        raise ValueError("CPU work processes must be a nonnegative integer")
    if workers < 0 or workers > (1 << 53) - 1:
        raise ValueError("CPU work processes must be a nonnegative exactly representable integer")
    return workers


def synthesis_strict_upper_bound_indices(objectives) -> tuple[int, ...]:
    """Parse only editor strict flags, independent of budgets or material rows.

    This consumes UI percentages, not ordinary optimization request fields.
    Invalid explicit flags are errors, never silently ignored by other tools.
    """
    if not isinstance(objectives, (list, tuple)):
        raise ValueError("Synthesis objectives must be an ordered list")
    indices = []
    for index, row in enumerate(objectives):
        if not isinstance(row, Mapping):
            raise ValueError("Synthesis objective must be an object")
        flag = row.get("synthesis_strict_upper_bound", False)
        if type(flag) is not bool:
            raise ValueError("Synthesis strict upper bound must be an explicit boolean")
        if flag:
            upper = row.get("upper_bound_percent")
            if isinstance(upper, bool) or upper is None:
                raise ValueError("Synthesis strict upper bound requires a finite upper bound")
            try:
                number = float(upper)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Synthesis strict upper bound requires a finite upper bound") from exc
            if row.get("mode") != "range" or not math.isfinite(number) or not 0 <= number <= 100:
                raise ValueError("Synthesis strict upper bound requires a range upper bound in [0,100]%")
            indices.append(index)
    return tuple(indices)


def build_synthesis_request(base: FilmOptimizationRequest, references, options,
                            use_current_seed: bool = False,
                            assume_unknown_k_zero: bool = False,
                            cpu_workers: int = 1,
                            forward_backend: str = "cpu",
                            forward_device: Mapping[str, Any] | None = None,
                            strict_upper_bound_objective_indices: tuple[int, ...] = ()) -> FilmSynthesisRequest:
    if type(assume_unknown_k_zero) is not bool:
        raise ValueError("Unknown-k assumption must be an explicit boolean")
    original = deepcopy(dict(base.design_snapshot))
    original["assume_lossless"] = assume_unknown_k_zero
    environment = deepcopy(original)
    environment["front_layers"], environment["back_layers"] = [], []
    environment["expression"] = {"text": "", "symbols": {}, "in_sync": True}
    # Preserve the actual physical environment; unsupported incidence/loss is
    # rejected by the core validator, never silently converted by the UI.
    return _validated(FilmSynthesisRequest(environment, base.objectives, tuple(references),
        design_revision=base.design_revision,
        acceptable_normalized_error=base.acceptable_normalized_error,
        validation_samples_multiplier=base.validation_samples_multiplier,
        seed_designs=(original,) if use_current_seed else (),
        strict_upper_bound_objective_indices=strict_upper_bound_objective_indices,
        cpu_workers=synthesis_cpu_workers(cpu_workers),
        forward_backend=forward_backend,
        forward_device=validate_forward_device(forward_device, forward_backend),
        **synthesis_options(options)))


def synthesis_base_request(request: FilmSynthesisRequest) -> FilmOptimizationRequest:
    return FilmOptimizationRequest(request.environment, request.design_revision, (), request.objectives,
        acceptable_normalized_error=request.acceptable_normalized_error,
        validation_samples_multiplier=request.validation_samples_multiplier,
        frozen_material_snapshot=request.frozen_material_snapshot)


def synthesis_spec(service, request):
    from coat_sim.scheduler import freeze_spec
    envelope = film_synthesis_request_to_payload(uuid4().hex, request)
    return freeze_spec(service, "film_synthesis", {"payload": envelope, "physical_structure_metadata": []})


@dataclass(frozen=True)
class SynthesisQuickResult:
    payload: Mapping[str, Any]
    optimization_result: FilmOptimizationResult | None


def synthesis_result_view(value: Any) -> SynthesisQuickResult:
    if not is_synthesis_result(value):
        raise ValueError("Queue result is not a verified film synthesis result")
    payload = deepcopy(value)
    raw = payload["result"]
    design = raw.get("candidate_design")
    if design is None:
        return SynthesisQuickResult(payload, None)
    training = raw["training_score"]
    validation = raw.get("validation_score")
    audit = payload["best_candidate_audit"]
    if not isinstance(training, dict) or (validation is not None and not isinstance(validation, dict)):
        raise ValueError("Synthesis result scores have an invalid shape")
    # The legacy presentation type requires integers. Unknown historical work
    # uses an internal placeholder only; the copied typed envelope retains null
    # and every synthesis-facing summary reads its original counters.
    def presentation_count(name: str) -> int:
        value = raw[name]
        return 0 if value is None else int(value)

    result = FilmOptimizationResult(
        state="failed" if raw["state"] == "not_found" else raw["state"], design_revision=int(raw["design_revision"]), best_design=deepcopy(design),
        best_thickness_nm={r["id"]: float(r["thickness"]["value"]) for r in design["front_layers"]},
        max_normalized_error=float(training["max_error"]),
        weighted_max_normalized_error=float(training["weighted_max_error"]),
        weighted_rms_error=float(training["weighted_rms_error"]),
        targets_met=bool(raw["targets_met"]),
        per_condition_errors=tuple(FilmConditionError(**r) for r in training["errors"]),
        iterations=presentation_count("cycles"), evaluations=presentation_count("evaluations"),
        message=str(raw["message"]), solver="film_synthesis", termination_reason=str(raw["termination_reason"]),
        failed_evaluations=presentation_count("failed_evaluations"), validation_complete=bool(raw["validation_complete"]),
        validation_evaluations=presentation_count("validation_evaluations"),
        validation_max_normalized_error=(float(validation["max_error"])
                                         if validation is not None else None),
        validation_weighted_rms_error=(float(validation["weighted_rms_error"])
                                       if validation is not None else None),
        validation_targets_met=audit.get("targets_met"),
        validation_per_condition_errors=(
            tuple(FilmConditionError(**r) for r in validation["errors"])
            if validation is not None else ()),
        validation_message=("Sampled audit only; no continuous-spectrum certificate"
                            if validation is not None else
                            "Cancellation candidate has no completed sampled holdout audit"))
    return SynthesisQuickResult(payload, result)


def synthesis_preview_handoff(request: FilmOptimizationRequest, result: FilmOptimizationResult, *, task_state=None):
    # Rebind a frozen, independently audited candidate for the existing spectrum
    # viewer. No live library is consulted and no source session is changed.
    from coat_sim.core.film_optimization_preview import create_optimization_candidate_handoff
    snapshot = deepcopy(dict(request.frozen_material_snapshot or {}))
    snapshot["design"] = deepcopy(dict(result.best_design))
    identity = {key: value for key, value in snapshot.items() if key != "sha256"}
    snapshot["sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    candidate_request = replace(request, design_snapshot=deepcopy(dict(result.best_design)),
                                frozen_material_snapshot=snapshot)
    physical_view = replace(result, targets_met=result.validation_targets_met is True,
        max_normalized_error=(result.validation_max_normalized_error
                              if result.validation_max_normalized_error is not None else result.max_normalized_error))
    handoff = create_optimization_candidate_handoff(candidate_request, physical_view)
    handoff["optimization_summary"]["synthesis_task_state"] = task_state or result.state
    handoff["optimization_summary"]["best_candidate_audit_targets_met"] = result.validation_targets_met
    return handoff


def verify_synthesis_file_references(service, request: FilmOptimizationRequest, design) -> None:
    """The reference-only .cfilm format cannot embed a task's optical closure."""
    from coat_sim.core.film_design_service import active_material_references
    snapshot = request.frozen_material_snapshot
    if snapshot is None:
        raise ValueError("Synthesis candidate has no frozen optical closure")
    frozen = {row["id"]: row for row in snapshot["materials"]}
    for ref in active_material_references(design):
        material_id, set_id = ref["material_id"], ref["optical_set_id"]
        expected = next((row for row in frozen.get(material_id, {}).get("optical_sets", [])
                         if row["id"] == set_id), None)
        try:
            actual = next((row for row in service.materials.get(material_id)["optical_sets"]
                           if row["id"] == set_id), None)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"材料引用已变化：{material_id}/{set_id}；请保存到含冻结材料的项目") from exc
        if expected is None or actual != expected:
            raise ValueError(f"材料引用已变化：{material_id}/{set_id}；.cfilm 仅保存引用，请保存到含冻结材料的项目")
