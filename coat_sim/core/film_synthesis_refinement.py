"""Analytic local refinement for automatically proposed reflector topologies.

Interior targets and finite phase-coordinate trust boxes are project search
conventions. Original objectives, physical scoring and acceptance stay owned by
synthesis; no physical call, budget or cancellation owner is introduced here.
"""
from copy import deepcopy
import math
import numpy as np


def interior_bounds(objective, acceptable):
    lower, upper = objective.lower_bound, objective.upper_bound
    if objective.mode != 'range':
        return lower, upper
    width = (1.0 if upper is None else upper) - (0.0 if lower is None else lower)
    if width <= 0:
        return lower, upper
    def margin(bound):
        # Project numerical guide floor, independent of acceptance tolerance;
        # zero requested violation must not collapse the guide to roundoff.
        return min(width * .1, max(width * .001, 8 * math.ulp(float(bound)), acceptable * objective.normalization_scale))
    return (lower if lower is None or lower <= 0 else lower + margin(lower),
            upper if upper is None or upper >= 1 else upper - margin(upper))


def _interior_response_residual(objective, response, acceptable):
    actual = np.asarray(response['actual_values'], float)
    if not np.all(np.isfinite(actual)):
        raise ValueError('nonfinite interior refinement response')
    if objective.mode == 'value':
        return actual - objective.target_value
    lower, upper = interior_bounds(objective, acceptable)
    residual = np.zeros_like(actual)
    if lower is not None:
        residual = np.minimum(actual - lower, 0.)
    if upper is not None:
        residual += np.maximum(actual - upper, 0.)
    return residual


def interior_loss(request, value):
    """Compute the guide from existing physical responses, without a Jacobian."""
    loss = 0.0
    for response in value['responses']:
        objective = request.objectives[response['objective_index']]
        residual = _interior_response_residual(objective, response, request.acceptable_normalized_error)
        factor = objective.weight / objective.normalization_scale**2
        loss += factor * float(residual @ residual)
    if not math.isfinite(loss):
        raise ValueError('nonfinite interior refinement loss')
    return loss


def interior_loss_gradient(request, value, layer_ids):
    """Return sum weighted interior hinge squares and derivatives per nm."""
    gradient = np.zeros(len(layer_ids))
    loss = 0.0
    for response in value['responses']:
        objective = request.objectives[response['objective_index']]
        residual = _interior_response_residual(objective, response, request.acceptable_normalized_error)
        factor = objective.weight / objective.normalization_scale**2
        loss += factor * float(residual @ residual)
        jacobian = response['jacobian_values_by_layer_id']
        for i, identifier in enumerate(layer_ids):
            gradient[i] += 2 * factor * float(residual @ np.asarray(jacobian[identifier], float))
    if not math.isfinite(loss) or not np.all(np.isfinite(gradient)):
        raise ValueError('nonfinite interior refinement loss or derivative')
    return loss, gradient


def refine_synthesis_candidate(request, initial, *, evaluate, archive, quarter_nm,
                               cancel_check=lambda: None, progress=lambda *args: None,
                               max_iterations=64):
    """Refine through parent callbacks; stop/cancel exceptions propagate intact.

    Each layer has a trust interval d0 +/- quarter_nm/2, intersected with
    explicit manufacturing bounds. This limits each invocation's phase-box
    displacement; later scheduled invocations recenter it. A finite total cap
    uses SLSQP's linear inequality. Returned best uses original worst violation,
    then the interior guide loss, independent of transient optimizer iterates.
    """
    from scipy.optimize import minimize
    design = initial['design']
    rows = design['front_layers']
    if not rows:
        return initial, dict(method='none', reason='empty_topology', iterations=0)
    ids = tuple(r['id'] for r in rows)
    scales = np.asarray([quarter_nm[(r['material_id'], r['optical_set_id'])] for r in rows])
    ds = np.asarray([r['thickness']['value'] for r in rows])
    low = np.maximum(ds - scales * .5, request.minimum_nonzero_thickness_nm or np.finfo(float).tiny)
    high = ds + scales * .5
    if request.max_layer_thickness_nm is not None:
        high = np.minimum(high, request.max_layer_thickness_nm)
    if np.any(low > ds) or np.any(high < ds):
        raise ValueError('local trust box must contain feasible initial thicknesses')
    bounds = list(zip(low / scales, high / scales, strict=True))
    best = initial
    if not math.isfinite(float(initial['score']['max_error'])):
        raise ValueError('nonfinite initial physical score')
    # The phase-coordinate round trip may worsen the original score by one
    # ULP. Associate the guide with the original retained candidate rather
    # than borrowing a recomputed candidate's loss or exporting a sentinel.
    initial_loss = interior_loss(request, initial) if initial.get('responses') else None
    best_key = (float(initial['score']['max_error']), math.inf if initial_loss is None else initial_loss)
    best_loss_source = ('initial_cached_responses' if initial_loss is not None
                        else 'initial_responses_unavailable_without_additional_physical_evaluation')
    cache = {}
    calls = 0
    total_cap = request.max_total_thickness_nm
    diagnostics = dict(method='SLSQP' if total_cap is not None else 'L-BFGS-B',
        max_iterations=max_iterations, lower_nm=low.tolist(), upper_nm=high.tolist(),
        bounds_source='project phase trust box d0 +/- quarter_nm/2 intersect explicit layer limits',
        interior_bounds=[interior_bounds(o, request.acceptable_normalized_error) for o in request.objectives],
        reason='running', iterations=0, physical_candidates=0)

    def objective(point):
        nonlocal best, best_key, calls, best_loss_source
        cancel_check()
        key = tuple(float(x) for x in point)
        if key in cache:
            return cache[key]
        thicknesses = np.clip(np.asarray(point) * scales, low, high)
        # Reject roundoff/line-search infeasibility before a physical call;
        # optimizer receives a differentiable quadratic feasibility penalty.
        if total_cap is not None and float(sum(thicknesses)) > total_cap:
            excess = float(sum(thicknesses)) - total_cap
            return 1e6 + excess**2, 2 * excess * scales
        trial = deepcopy(design)
        for row, thickness in zip(trial['front_layers'], thicknesses, strict=True):
            row['thickness'] = dict(value=float(thickness), unit='nm')
        value = evaluate(trial, jacobian=True)
        calls += 1
        loss, gradient = interior_loss_gradient(request, value, ids)
        archive(value, audit_eligible=False)
        current_key = (float(value['score']['max_error']), loss)
        if current_key < best_key:
            best, best_key = value, current_key
            best_loss_source = 'parent_evaluated_candidate_responses'
        progress('interior_local_trial', value, dict(loss=loss, physical_candidates=calls))
        cancel_check()
        cache[key] = (loss, gradient * scales)
        return cache[key]

    constraints = ()
    if total_cap is not None:
        constraints = ({'type': 'ineq', 'fun': lambda x: total_cap - float(np.asarray(x) @ scales),
                        'jac': lambda x: -scales},)
    options = dict(maxiter=max_iterations, ftol=1e-15)
    if total_cap is None:
        options.update(gtol=1e-11, maxls=30)
    # Establish the initial guide loss/Jacobian through the same parent-owned
    # evaluation boundary before comparing any transient solver iterate.
    objective(ds / scales)
    outcome = minimize(objective, ds / scales, jac=True, bounds=bounds,
                       method=diagnostics['method'], constraints=constraints, options=options)
    cancel_check()
    diagnostics.update(reason=str(outcome.message), iterations=int(outcome.nit),
                       physical_candidates=calls,
                       interior_loss=None if best is initial and initial_loss is None else best_key[1],
                       interior_loss_source=best_loss_source,
                       original_max_error=best['score']['max_error'])
    return best, diagnostics
