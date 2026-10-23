"""Nominal-preserving finite scene-cloud local optimization.

This module deliberately has no UI, scheduler or manufacturing defaults. Its
private entry point is called after a fully scored feasible baseline exists.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

import numpy as np
from scipy.optimize import minimize  # type: ignore[import-untyped]

from .film_sensitivity import _error_components


def _constraint_rows(values, jacobian, conditions, strict_upper=()):
    """Positive range slack and equality residual, with exact bound operators."""
    inequalities, inequality_jac, equalities, equality_jac = [], [], [], []
    for actual, derivative, (index, _wave, _angle, objective) in zip(
            values, jacobian, conditions, strict=True):
        scale = objective.normalization_scale
        if objective.mode == "value":
            equalities.append((actual - objective.target_value) / scale)
            equality_jac.append(derivative / scale)
        else:
            if objective.lower_bound is not None:
                inequalities.append((actual - objective.lower_bound) / scale)
                inequality_jac.append(derivative / scale)
            if objective.upper_bound is not None:
                upper = (np.nextafter(objective.upper_bound, -np.inf)
                         if index in strict_upper else objective.upper_bound)
                inequalities.append((upper - actual) / scale)
                inequality_jac.append(-derivative / scale)
    columns = jacobian.shape[-1]
    return (np.asarray(inequalities), np.asarray(inequality_jac).reshape(-1, columns),
            np.asarray(equalities), np.asarray(equality_jac).reshape(-1, columns))


def _scenario_residuals(values, jacobian, conditions):
    """Weighted normalized violations and analytic piecewise gradients."""
    residual = np.zeros_like(values)
    derivative = np.zeros_like(jacobian)
    weights = np.asarray([row[3].weight for row in conditions])
    for index, (_oi, _wave, _angle, objective) in enumerate(conditions):
        actual = values[:, index]
        if objective.mode == "value":
            difference = actual - objective.target_value
            violation = np.abs(difference)
            sign = np.sign(difference)
        else:
            low = np.zeros_like(actual) if objective.lower_bound is None else objective.lower_bound - actual
            high = np.zeros_like(actual) if objective.upper_bound is None else actual - objective.upper_bound
            violation = np.maximum(0., np.maximum(low, high))
            sign = np.where(low > 0., -1., np.where(high > 0., 1., 0.))
        residual[:, index] = violation / objective.normalization_scale
        derivative[:, index, :] = ((jacobian[:, index, :] * sign[:, None])
                                   / objective.normalization_scale)
    return residual, derivative, weights


def _expected_loss(values, jacobian, conditions, scenario_weights):
    residual, derivative, weights = _scenario_residuals(values, jacobian, conditions)
    # Normalize square-root weights before multiplying. Unlike w*p or r**2,
    # these operations remain finite for valid extreme (but finite) weights.
    def roots(raw):
        raw = np.sqrt(np.asarray(raw, dtype=float))
        scaled = raw / raw.max()
        return scaled / np.linalg.norm(scaled)
    condition_roots = roots(weights)
    scenario_roots = roots(scenario_weights)
    weighted_residual = (residual * condition_roots[None, :]) * scenario_roots[:, None]
    peak = float(np.max(np.abs(weighted_residual)))
    loss = 0. if peak == 0. else float(peak * np.linalg.norm(weighted_residual / peak))
    weighted_derivative = ((derivative * condition_roots[None, :, None])
                           * scenario_roots[:, None, None])
    gradient = (np.zeros(jacobian.shape[-1]) if loss == 0. else
        np.sum((weighted_residual / loss)[:, :, None] * weighted_derivative, axis=(0, 1)))
    if not np.isfinite(loss) or not np.all(np.isfinite(gradient)):
        raise ValueError("constrained expected objective/gradient is not finite")
    return loss, gradient



def _weighted_scaled_array(values, scale, weight):
    """Vector form of scoring.weighted_scaled_value, without unsafe w/b.

    Frexp/ldexp keeps the same signed sqrt(weight)*value/scale target and
    derivative even when the intermediate multiplier itself is unrepresentable.
    A genuinely nonfinite final value is rejected, never dropped or clipped.
    """
    values = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(values)):
        raise ValueError("constrained weighted value/derivative must be finite")
    vm, ve = np.frexp(values)
    sm, se = math.frexp(scale)
    wm, we = math.frexp(weight)
    we2, odd = divmod(we, 2)
    root_mantissa = math.sqrt(wm * (2. if odd else 1.))
    mantissa, adjustment = np.frexp(vm / sm * root_mantissa)
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        result = np.ldexp(mantissa, ve - se + we2 + adjustment)
    if not np.all(np.isfinite(result)):
        raise ValueError("constrained weighted value/derivative is not finite")
    return result

def _worst_epigraph(values, jacobian, conditions, epigraph):
    """Every signed target face, not a finite-differenced max subgradient."""
    rows: list[Any] = []
    derivatives: list[Any] = []
    for index, (_oi, _wave, _angle, objective) in enumerate(conditions):
        actual, jac = values[:, index], jacobian[:, index, :]
        if objective.mode == "value":
            faces = [(actual - objective.target_value, jac),
                     (objective.target_value - actual, -jac)]
        else:
            faces = []
            if objective.lower_bound is not None:
                faces.append((objective.lower_bound - actual, -jac))
            if objective.upper_bound is not None:
                faces.append((actual - objective.upper_bound, jac))
        for violation, gradient in faces:
            rows.extend(epigraph - _weighted_scaled_array(violation,
                objective.normalization_scale, objective.weight))
            derivatives.extend(np.column_stack((-_weighted_scaled_array(gradient,
                objective.normalization_scale, objective.weight), np.ones(len(actual)))))
    if not np.all(np.isfinite(rows)):
        raise ValueError("constrained epigraph slack is not finite")
    return np.asarray(rows), np.asarray(derivatives)


def _physical_bounds(request):
    """Intersect nominal variable bounds with the exact scene physical domain."""
    bounds = [[variable.lower_nm, variable.upper_nm] for variable in request.variables]
    selected = {variable.layer_id: index for index, variable in enumerate(request.variables)}
    for index, error in enumerate(request.errors):
        if error.layer_id not in selected:
            continue  # Baseline already checked every unselected scene layer.
        column = selected[error.layer_id]
        absolute, relative = _error_components(error)
        for scenario in request.scenarios:
            coefficient = scenario.coefficients[index]
            slope, intercept = 1. + coefficient * relative, coefficient * absolute
            if slope > 0:
                bounds[column][0] = max(bounds[column][0], -intercept / slope)
            elif slope < 0:
                bounds[column][1] = min(bounds[column][1], -intercept / slope)
            elif intercept < 0:
                raise ValueError("scenario has no nonnegative physical thickness domain")
    if any(low > high for low, high in bounds):
        raise ValueError("scenario physical domain and nominal variable bounds do not intersect")
    # The algebraic root can round to a point whose actual d+c*(A+r*d)
    # evaluation is slightly negative. Move only to adjacent representable
    # points inside the physical interval, using exactly the forward's order
    # of operations. This selects a representable domain; no d' is clipped.
    for variable, bound in zip(request.variables, bounds, strict=True):
        affected = [(error, scenario.coefficients[index])
                    for index, error in enumerate(request.errors) if error.layer_id == variable.layer_id
                    for scenario in request.scenarios]
        for side, direction in ((0, math.inf), (1, -math.inf)):
            value = bound[side]
            for _ in range(64):
                finite_domain = True
                for error, coefficient in affected:
                    absolute, relative = _error_components(error)
                    perturbed = value + coefficient * (absolute + relative * value)
                    if not math.isfinite(perturbed) or perturbed < 0.:
                        finite_domain = False
                        break
                if finite_domain:
                    bound[side] = value
                    break
                value = float(np.nextafter(value, direction))
                if not bound[0] <= value <= bound[1]:
                    raise ValueError("scenario physical domain contains no representable endpoint")
            else:
                raise ValueError("could not establish a representable scene physical endpoint")
    return bounds


def run_nominal_preserving(request, evaluator, initial_design, baseline, *,
                           accept_candidate, iteration_callback, emit, observe_candidate=None):
    """Run SLSQP, returning status only; caller owns feasible incumbent/results."""
    physical = evaluator.physical(initial_design)
    physical_bounds = _physical_bounds(request)
    lows = np.asarray([bound[0] for bound in physical_bounds])
    highs = np.asarray([bound[1] for bound in physical_bounds])
    spans = highs - lows
    initial_physical = np.asarray([physical[variable.layer_id] for variable in request.variables])
    if np.any(initial_physical < lows) or np.any(initial_physical > highs):
        raise ValueError("initial thickness is outside the representable scene physical domain")
    initial_coordinates = np.divide(initial_physical - lows, spans,
        out=np.zeros_like(spans), where=spans != 0.)
    # Distinct physical interior points must not alias a normalized endpoint
    # after subtraction/division roundoff; endpoints remain exactly reachable.
    initial_coordinates = np.where((initial_physical > lows) & (initial_coordinates == 0.),
        np.nextafter(0., 1.), initial_coordinates)
    initial_coordinates = np.where((initial_physical < highs) & (initial_coordinates == 1.),
        np.nextafter(1., 0.), initial_coordinates)
    initial = initial_coordinates.copy()
    worst = request.robustness_objective == "finite_scenario_worst"
    bounds: list[tuple[float, float | None]] = [(0., 1. if span else 0.) for span in spans]
    if worst:
        initial = np.append(initial, baseline.robustness_value)
        bounds.append((0., None))
    last_x: Any = None
    last_data = None

    def evaluate(x):
        nonlocal last_x, last_data
        evaluator.check()
        candidate_x = np.asarray(x[:len(spans)])
        if last_x is not None and last_data is not None and np.array_equal(last_x, candidate_x):
            if observe_candidate is not None:
                observe_candidate(last_data["design"], last_data["analysis"])
            return last_data
        if (not np.all(np.isfinite(candidate_x)) or np.any(candidate_x < 0.)
                or np.any(candidate_x > 1.) or np.any((spans == 0.) & (candidate_x != 0.))):
            raise ValueError("solver trial is outside explicit normalized thickness bounds")
        # Endpoint-aware interpolation avoids low + (high-low) exceeding high
        # by one ULP. Use the nearer endpoint for interior points, and preserve
        # the exact scored starting point rather than round-tripping its ratio.
        thickness = np.where(candidate_x <= .5, lows + candidate_x * spans,
                             highs - (1. - candidate_x) * spans)
        thickness = np.where(candidate_x == 0., lows, np.where(candidate_x == 1., highs, thickness))
        thickness = np.where(candidate_x == initial_coordinates, initial_physical, thickness)
        if np.any(thickness < lows) or np.any(thickness > highs):
            raise ValueError("solver trial violates the representable physical thickness domain")
        candidate = deepcopy(initial_design)
        selected = {variable.layer_id: value for variable, value in zip(request.variables, thickness, strict=True)}
        for layer in candidate["front_layers"]:
            if layer["id"] in selected:
                layer["thickness"] = {"value": float(selected[layer["id"]]), "unit": "nm"}
        if candidate.get("expression", {}).get("in_sync"):
            candidate["expression"]["in_sync"] = False
        analysis = evaluator.evaluate(candidate)
        # Full physical nominal grid verification happens before every retained
        # incumbent, regardless of SLSQP's looser internal feasibility tolerance.
        accept_candidate(candidate, analysis)
        evaluator.check()
        data = evaluator.solver_data
        assert data is not None
        last_data = dict(data, design=candidate, analysis=analysis, nominal_jac=data["nominal_jac"] * spans,
                         scenario_jac=data["scenario_jac"] * spans)
        last_x = candidate_x.copy()
        return last_data

    def objective(x):
        if worst:
            evaluator.check()
            return float(x[-1])
        data = evaluate(x)
        return _expected_loss(data["scenario_values"], data["scenario_jac"], data["conditions"],
                              [scenario.weight for scenario in request.scenarios])[0]

    def gradient(x):
        if worst:
            evaluator.check()
            return np.append(np.zeros(len(spans)), 1.)
        data = evaluate(x)
        return _expected_loss(data["scenario_values"], data["scenario_jac"], data["conditions"],
                              [scenario.weight for scenario in request.scenarios])[1]

    def nominal(x, equality, derivative=False):
        data = evaluate(x)
        rows = _constraint_rows(data["nominal"], data["nominal_jac"], data["conditions"],
                                request.strict_upper_bound_objective_indices)
        result = rows[(2 if equality else 0) + int(derivative)]
        return np.column_stack((result, np.zeros(len(result)))) if derivative and worst else result

    initial_data = evaluate(initial)
    nominal_rows = _constraint_rows(initial_data["nominal"], initial_data["nominal_jac"],
                                    initial_data["conditions"], request.strict_upper_bound_objective_indices)
    constraints = []
    if len(nominal_rows[0]):
        constraints.append(dict(type="ineq", fun=lambda x: nominal(x, False),
                                jac=lambda x: nominal(x, False, True)))
    if len(nominal_rows[2]):
        constraints.append(dict(type="eq", fun=lambda x: nominal(x, True),
                                jac=lambda x: nominal(x, True, True)))
    if worst:
        def epigraph(x, derivative=False):
            data = evaluate(x)
            return _worst_epigraph(data["scenario_values"], data["scenario_jac"],
                                   data["conditions"], x[-1])[int(derivative)]
        constraints.append(dict(type="ineq", fun=epigraph, jac=lambda x: epigraph(x, True)))
    emit("search")
    evaluator.check()
    solved = minimize(objective, initial, jac=gradient, bounds=bounds,
        constraints=constraints, method="SLSQP", callback=iteration_callback,
        options=dict(maxiter=request.max_iterations or 100, ftol=1e-10))
    evaluator.check()
    # Force final full-scene/nominal evidence; solver success alone is not accepted.
    final = evaluate(solved.x)
    if not final["analysis"].nominal_targets_met:
        return "partial", "constrained_nominal_rejected", "Solver's final trial fails exact original nominal bounds; only a previously evaluated nominal-feasible incumbent is retained."
    if solved.success:
        return "completed", "constrained_tolerance", "Local scene-cloud solver stopped; retained candidates satisfy the unchanged original nominal grid. No global or manufacturing-yield certificate."
    if solved.status == 9:
        return "partial", "iteration_limit", "Constrained solver iteration limit; a fully evaluated nominal-feasible incumbent is retained."
    return "partial", "constrained_solver_stopped", f"Constrained solver stopped ({solved.message}); only a fully evaluated nominal-feasible incumbent is retained."
