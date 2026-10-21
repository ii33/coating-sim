"""Independent sampled/extremum audit using the project's frozen TMM evaluator.

This is evidence on searched points, never a continuous spectral certificate.
Adaptive points affect worst violations, not the fixed-grid RMS measure.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Mapping

import numpy as np

from .film_design import validate_design
from .film_design_service import active_material_references
from .film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
from .optical_constants import prepare_optical_set
from .film_synthesis_absorption import (assumes_unknown_k_zero, sample_synthesis_optical,
                                        synthesis_absorption_assumption)
from .film_optimization_structure import (
    FilmStructureOptimizationRequest, _StructureCancelled, _PreparedStructureEvaluator,
    evaluate_frozen_structure_candidate,
)

EXTREMUM_XATOL = 1e-10
EXTREMUM_MAXITER = 500


def audit_frozen_synthesis_candidate(
    request: Any, design: Mapping[str, Any], *, cancel_event: Any = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Audit dense endpoints, shifted midpoints and bracketed power extrema.

    ``request`` is a frozen FilmSynthesisRequest (duck typed to avoid a cyclic
    import). Every physical evaluation reuses the current objective/TMM engine.
    The caller alone owns candidate budgets, archive and terminal commit.
    """
    from scipy.optimize import minimize_scalar  # type: ignore[import-untyped]

    if request.frozen_material_snapshot is None:
        raise ValueError("synthesis audit requires a frozen material snapshot")
    design = validate_design(deepcopy(dict(design)))
    if design["substrate"]["sides"] != "single" or design["first_surface"] != "front":
        raise ValueError("synthesis audit supports single-sided front incidence only; double-sided/back incidence is unsupported")
    if not design["use_k"]:
        raise ValueError("synthesis audit requires use_k=True to retain known absorption")
    assumed = assumes_unknown_k_zero(request)
    if design["assume_lossless"] != assumed:
        raise ValueError("synthesis audit candidate unknown-k policy differs from the frozen request")
    base = FilmOptimizationRequest(
        request.environment, request.design_revision, (), request.objectives,
        acceptable_normalized_error=request.acceptable_normalized_error,
        validation_samples_multiplier=request.validation_samples_multiplier,
        frozen_material_snapshot=request.frozen_material_snapshot,
    )
    proxy = FilmStructureOptimizationRequest(base, min_thickness_nm=0.0,
                                             max_thickness_nm=np.finfo(float).max)
    # A distinct owner per audit; no training grid/cache or candidate score is reused.
    if cancel_event is not None and cancel_event.is_set():
        raise _StructureCancelled()
    prepared = _PreparedStructureEvaluator(proxy)
    physical = points = dense_points = offset_points = 0
    bracket_count = completed_extrema = 0
    # This public helper also rejects ordinary frozen snapshots that never went
    # through the stricter synthesis freeze. Legacy TMM compatibility can return
    # k=0 outside a k table, even with k_known=True; do not trust that policy here.
    material_rows = {row["id"]: row for row in request.frozen_material_snapshot.get("materials", ())
                     if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
    domains = ", ".join(f"[{o.wavelength_min_nm:g}, {o.wavelength_max_nm:g}] nm" for o in request.objectives)
    strict_samplers: dict[tuple[str, str], Callable[..., Any]] = {}
    for row in active_material_references(design):
        key = (row["material_id"], row["optical_set_id"])
        if key in strict_samplers:
            continue
        context = f"audit material={key[0]}, optical_set={key[1]}; target intervals: {domains}"
        material = material_rows.get(key[0])
        if material is None:
            raise ValueError(f"{context}: active frozen material is missing")
        optical = next((o for o in material.get("optical_sets", ())
                        if isinstance(o, Mapping) and o.get("id") == key[1]), None)
        if optical is None:
            raise ValueError(f"{context}: active frozen optical set is missing")
        try:
            strict_samplers[key] = prepare_optical_set(optical, k_sampling_policy="source")
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            raise ValueError(f"{context}: invalid active optical source: {exc}") from exc
    checked_waves: set[float] = set()
    extra_errors: dict[tuple[int, float, float, str], Any] = {}

    def check() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _StructureCancelled()

    def emit(stage: str, index: int | None = None) -> None:
        if progress_callback is not None:
            progress_callback(dict(stage=stage, objective_index=index,
                                   physical_evaluations=physical, wavelength_points=points))
        check()

    def evaluate(objectives: tuple[FilmOptimizationObjective, ...], *, stage: str,
                 index: int | None = None) -> Mapping[str, Any]:
        nonlocal physical, points
        check()
        emit(stage, index)
        for objective in objectives:
            for sampled_wave in np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples):
                wavelength = float(sampled_wave)
                if wavelength in checked_waves:
                    continue
                check()
                for (material_id, optical_set_id), sampler in strict_samplers.items():
                    context = f'audit material={material_id}, optical_set={optical_set_id}, wavelength={wavelength:g} nm; target intervals: {domains}'
                    try:
                        n, k, details = sample_synthesis_optical(sampler, wavelength,
                            assume_unknown_k_zero=assumed)
                    except (ValueError, ArithmeticError) as exc:
                        raise ValueError(f'{context}: {exc}') from exc
                    if not np.isfinite(n) or n <= 0 or k is None or not np.isfinite(k) or k < 0 or (not details.get('k_known') and not assumed):
                        raise ValueError(f'{context}: explicit finite passive n/k source data required; unknown k cannot be zero')
                checked_waves.add(wavelength)
        value = prepared.evaluate(
            design, objectives=objectives, evaluator=evaluate_frozen_structure_candidate,
            cancel_event=cancel_event,
        )
        check()
        physical += 1
        points += sum(o.wavelength_samples * len(o.angles_deg) for o in objectives)
        emit(stage, index)
        return value

    dense_objectives = tuple(replace(o, wavelength_samples=(1 if o.wavelength_samples == 1 else
        (o.wavelength_samples - 1) * request.validation_samples_multiplier + 1))
        for o in request.objectives)
    dense = evaluate(dense_objectives, stage="audit_dense")
    dense_points = points
    series = {(int(row["objective_index"]), float(row["angle_deg"])):
              list(zip(row["wavelength_nm"], row["actual_values"], strict=True))
              for row in dense["responses"]}

    def add_errors(value, index: int) -> None:
        for error in value["score"]["errors"]:
            error = replace(error, objective_index=index)
            extra_errors[(index, error.angle_deg, error.wavelength_nm, error.polarization)] = error

    # A shifted grid adds no duplicate integral weight. A line remains a line.
    for index, objective in enumerate(dense_objectives):
        check()
        count = objective.wavelength_samples
        if count <= 1 or objective.wavelength_min_nm == objective.wavelength_max_nm:
            continue
        step = (objective.wavelength_max_nm - objective.wavelength_min_nm) / (count - 1)
        offset = replace(objective,
            wavelength_min_nm=objective.wavelength_min_nm + 0.5 * step,
            wavelength_max_nm=objective.wavelength_max_nm - 0.5 * step,
            wavelength_samples=count - 1)
        previous_points = points
        value = evaluate((offset,), stage="audit_offset", index=index)
        offset_points += points - previous_points
        add_errors(value, index)
        for row in value["responses"]:
            series[(index, float(row["angle_deg"]))].extend(zip(row["wavelength_nm"], row["actual_values"], strict=True))

    def extremum_function(objective: FilmOptimizationObjective, angle: float, index: int,
                          a: float, b: float, sign: float,
                          scalar_cache: dict[float, Any]) -> Callable[[float], float]:
        # Bind one bracket explicitly; a later loop iteration cannot change a
        # callback's interval, objective, angle, sign, or shared scalar cache.
        def scalar(t: float) -> float:
            check()
            wavelength = a + (b - a) * float(t)
            if wavelength not in scalar_cache:
                local = replace(objective, wavelength_min_nm=wavelength,
                    wavelength_max_nm=wavelength, wavelength_samples=1, angles_deg=(angle,))
                value = evaluate((local,), stage="audit_extremum", index=index)
                add_errors(value, index)
                scalar_cache[wavelength] = value["score"]["errors"][0]
            return sign * float(scalar_cache[wavelength].actual)
        return scalar

    # Find power extrema instead of optimizing a flat zero-violation plateau.
    # For value targets both signs matter; a range needs only bounded sides.
    for (index, angle), records in series.items():
        objective = request.objectives[index]
        records.sort(key=lambda pair: pair[0])
        signs = ((1.0, -1.0) if objective.mode == "value" else
                 tuple(sign for bound, sign in ((objective.upper_bound, -1.0),
                                                (objective.lower_bound, 1.0)) if bound is not None))
        scalar_cache: dict[float, Any] = {}
        for sign in signs:
            for cursor in range(1, len(records) - 1):
                left, center, right = records[cursor - 1:cursor + 2]
                lv, cv, rv = sign * left[1], sign * center[1], sign * right[1]
                # Reject only relative roundoff plateaus; a tiny transmission
                # tail keeps its own scale and can still bracket a narrow peak.
                spread = max(lv, cv, rv) - min(lv, cv, rv)
                noise = 32 * np.finfo(float).eps * max(abs(lv), abs(cv), abs(rv), np.finfo(float).tiny)
                if spread <= noise:
                    continue
                if not (cv <= lv and cv <= rv and (cv < lv or cv < rv)):
                    continue
                bracket_count += 1
                a, b = float(left[0]), float(right[0])
                if not b > a:
                    continue

                scalar = extremum_function(objective, angle, index, a, b, sign, scalar_cache)
                outcome = minimize_scalar(scalar, method="bounded", bounds=(0.0, 1.0),
                    options={"xatol": EXTREMUM_XATOL, "maxiter": EXTREMUM_MAXITER})
                check()
                if not outcome.success or not np.isfinite(outcome.fun):
                    raise ValueError(f"spectral extremum did not converge for objective {index}")
                completed_extrema += 1

    errors_by_point = {(e.objective_index, e.angle_deg, e.wavelength_nm, e.polarization): e
                       for e in dense["score"]["errors"]}
    errors_by_point.update(extra_errors)
    errors = tuple(errors_by_point.values())
    maximum = max(e.normalized_violation for e in errors)
    weighted_maximum = max(e.weighted_normalized_violation for e in errors)
    score = deepcopy(dense["score"])
    score.update(errors=errors, max_error=maximum, weighted_max_error=weighted_maximum,
        key=(0.0 if maximum <= request.acceptable_normalized_error else 1.0, maximum, score["weighted_rms_error"]))
    objective_status = []
    for index, objective in enumerate(request.objectives):
        worst = max((e for e in errors if e.objective_index == index), key=lambda e: e.normalized_violation)
        objective_status.append(dict(objective_index=index, metric=objective.metric,
            wavelength_min_nm=objective.wavelength_min_nm, wavelength_max_nm=objective.wavelength_max_nm,
            discrete_line=objective.wavelength_min_nm == objective.wavelength_max_nm,
            worst_wavelength_nm=worst.wavelength_nm, worst_angle_deg=worst.angle_deg,
            worst_actual=worst.actual, max_normalized_error=worst.normalized_violation,
            targets_met=worst.normalized_violation <= request.acceptable_normalized_error))
    metadata = dict(physical_evaluations=physical, wavelength_points=points,
        dense_points=dense_points, offset_points=offset_points,
        extra_points=points - dense_points, unique_condition_points=len(errors),
        bracket_count=bracket_count, completed_extrema=completed_extrema,
        local_coordinate="lambda=a+(b-a)*t; t in [0,1]", extrema_xatol=EXTREMUM_XATOL,
        continuous_certificate=False, rms_measure="fixed_dense_grid_only",
        objective_status=objective_status,
        absorption_assumption=synthesis_absorption_assumption(request, design))
    score["audit"] = deepcopy(metadata)
    check()
    return dict(design=deepcopy(dense["design"]), score=score,
                smooth_loss=None, gradient_by_layer_id=None,
                responses=dense["responses"], jacobian_calls=0, audit=metadata)
