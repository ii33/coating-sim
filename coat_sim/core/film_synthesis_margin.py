"""Fixed-topology signed worst-margin optimization; no implicit objectives.

Only enabled, nondegenerate range sides have a safety margin. Reflectance and
transmittance physical endpoints 0/1 remain hard constraints but do not attract
the solution toward the middle of an AR/HR admissible interval. All distances
use the user's existing objective.normalization_scale, across bands and angles.
Value/zero-width conditions remain hard original constraints, never margins.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import import_module

import numpy as np

from .film_synthesis_multispectral_minimax import _original_reduction
from .film_synthesis_refinement import _coordinate_thicknesses_nm


def _field(row, name):
    return row[name] if isinstance(row, dict) else getattr(row, name)


def _margin_sides(criterion):
    low, high = criterion.lower_bound, criterion.upper_bound
    interval = criterion.mode == 'range' and (0. if low is None else low) < (1. if high is None else high)
    return (interval and low is not None and low > 0.,
            interval and high is not None and high < 1.)


def worst_signed_margin(request, value):
    """Adverse signed distance: negative is safety, zero is the nominal edge.

    Scores, including independent audit scores, keep their actual enabled
    conditions; pointwise and mean are never interchanged or synthesized.
    None means that no enabled condition defines an interval safety direction.
    """
    signed = []
    for row in value['score']['errors']:
        objective = request.objectives[_field(row, 'objective_index')]
        criterion = getattr(objective, _field(row, 'reduction'))
        low, high = _margin_sides(criterion)
        actual = float(_field(row, 'actual'))
        if low:
            signed.append((criterion.lower_bound - actual) / objective.normalization_scale)
        if high:
            signed.append((actual - criterion.upper_bound) / objective.normalization_scale)
    return max(signed) if signed else None


def refine_worst_margin(request, initial, *, evaluate, archive,
        cancel_check=lambda: None, resource_check=lambda *args, **kwargs: None,
        progress=lambda *args: None, max_iterations=256):
    """Minimize unrestricted t with q_margin <= t and all original q <= 0.

    A finite iteration safeguard is reported as a limit, not convergence.
    SLSQP success means constrained local stationarity to its scaled ftol;
    neither a zero raw optical gradient nor global optimality is claimed.
    Parent callbacks retain all work/budget/cancellation and audit ownership.
    """
    cancel_check()
    design = initial['design']
    rows = design['front_layers']
    initial_margin = worst_signed_margin(request, initial)
    details = dict(method='SLSQP_signed_worst_margin', termination='not_started',
        success=False, initial_signed_margin=initial_margin, final_signed_margin=initial_margin,
        physical_candidates=0, iterations=0, all_original_conditions=True,
        normalization='signed distance / user objective.normalization_scale; unweighted worst over enabled bands/angles/reductions',
        margin_policy='nonzero-width range sides inside R/T physical endpoints; value/zero-width/physical endpoints hard only')
    if initial_margin is None or not rows:
        details['termination'] = 'no_interval_margin' if initial_margin is None else 'empty_topology'
        return initial, details
    if not initial['score'].get('nominal_pass', initial['score']['max_error'] == 0.):
        raise ValueError('margin refinement requires a nominally feasible candidate')
    ids = tuple(row['id'] for row in rows)
    ds = np.asarray([row['thickness']['value'] for row in rows], float)
    scales = np.full(ds.shape, 100.)
    lower = request.minimum_nonzero_thickness_nm or np.finfo(float).tiny
    upper, total = request.max_layer_thickness_nm, request.max_total_thickness_nm
    x0 = ds / scales
    best, best_margin = initial, initial_margin
    cache_x = cache_q = cache_j = cache_value = cache_mask = None

    def data(x):
        nonlocal cache_x, cache_q, cache_j, cache_value, cache_mask, best, best_margin
        cancel_check()
        x = np.asarray(x, float)
        if x.shape != ds.shape or not np.all(np.isfinite(x)):
            raise ValueError('invalid margin solver coordinates')
        if cache_x is not None and np.array_equal(x, cache_x):
            return cache_q, cache_j, cache_mask
        d = _coordinate_thicknesses_nm(x, scales, lower, upper, x0, ds)
        below = np.minimum(d - lower, 0.)
        above = np.zeros_like(d) if upper is None else np.maximum(d - upper, 0.)
        excess = 0. if total is None else max(0., float(sum(d)) - total)
        if np.any(below) or np.any(above) or excess:
            # No infeasible physical work is submitted. A smooth guard gives
            # SLSQP a direction back into native manufacturing constraints.
            assert cache_q is not None and cache_mask is not None
            penalty = 1e6 + float(below @ below + above @ above) + excess**2
            return (np.full(len(cache_q), penalty),
                    np.tile(2 * (below + above + excess) * scales, (len(cache_q), 1)), cache_mask)
        trial = deepcopy(design)
        for row, thickness in zip(trial['front_layers'], d, strict=True):
            row['thickness'] = dict(value=float(thickness), unit='nm')
        resource_check(trial, jacobian=True, epigraph=True)
        value = evaluate(trial, jacobian=True)
        if value['design'] != trial:
            raise ValueError('margin evaluator returned a different physical candidate')
        reduced = _original_reduction(request, value, ids)
        mask: list[bool] = []
        for condition in reduced.conditions:
            criterion = getattr(request.objectives[condition.objective_index], condition.reduction)
            if criterion.mode == 'value':
                mask.extend((False, False))
            else:
                selected = _margin_sides(criterion)
                if criterion.lower_bound is not None:
                    mask.append(selected[0])
                if criterion.upper_bound is not None:
                    mask.append(selected[1])
        cache_x, cache_q, cache_j, cache_value, cache_mask = (x.copy(), reduced.signed_bound_values,
            reduced.signed_bound_jacobian * scales, value, np.asarray(mask, bool))
        details['physical_candidates'] += 1
        margin = float(np.max(cache_q[cache_mask]))
        if reduced.nominal_pass and margin < best_margin:
            best, best_margin = value, margin
        archive(value, audit_eligible=False)
        progress('local_margin_trial', value, dict(signed_margin=margin,
            nominal_pass=reduced.nominal_pass, physical_candidates=details['physical_candidates']))
        cancel_check()
        return cache_q, cache_j, cache_mask

    q0, _, selected = data(x0)
    assert cache_value is not None  # data(x0) committed the initial physical evaluation.
    if not _original_reduction(request, cache_value, ()).nominal_pass:
        raise ValueError('margin refinement requires nominally feasible raw initial responses')
    start = np.concatenate((x0, [float(np.max(q0[selected]))]))

    def constraints(x):
        q, _, mask = data(x[:-1])
        return np.concatenate((-q, x[-1] - q[mask]))

    def jacobian(x):
        _, j, mask = data(x[:-1])
        return np.vstack((np.column_stack((-j, np.zeros(len(j)))),
                          np.column_stack((-j[mask], np.ones(np.count_nonzero(mask))))))

    conditions = [dict(type='ineq', fun=constraints, jac=jacobian)]
    if total is not None:
        conditions.append(dict(type='ineq', fun=lambda x: total - float(x[:-1] @ scales),
            jac=lambda x: np.concatenate((-scales, [0.]))))
    outcome = import_module('scipy.optimize').minimize(lambda x: float(x[-1]), start,
        jac=lambda x: np.concatenate((np.zeros(len(ids)), [1.])), method='SLSQP',
        bounds=[*((lower / 100., None if upper is None else upper / 100.) for _ in ids), (None, None)],
        constraints=tuple(conditions), callback=lambda x: cancel_check(),
        options=dict(maxiter=max_iterations, ftol=1e-11))
    cancel_check()
    terminal_q, _, terminal_mask = data(np.asarray(outcome.x[:-1], float))
    feasible = bool(np.max(terminal_q) <= 1e-9 and
                    np.max(terminal_q[terminal_mask]) <= float(outcome.x[-1]) + 1e-8)
    from .film_synthesis_archive import physical_design_hash
    terminal_evaluated = cache_x is not None and np.array_equal(cache_x, outcome.x[:-1])
    terminal = cache_value if terminal_evaluated else None
    nominal_terminal = terminal is not None and _original_reduction(request, terminal, ()).nominal_pass
    converged = bool(outcome.success and feasible and nominal_terminal)
    # A best line-search trial is not the solver's stationary endpoint. Only
    # return the exact feasible endpoint with its convergence claim; fallback
    # trials retain their evidence but cannot borrow that endpoint's status.
    if converged:
        assert terminal is not None
        selected = terminal
    else:
        selected = best
    selected_margin = worst_signed_margin(request, selected)
    terminal_hash = None if terminal is None else physical_design_hash(terminal['design'])
    details.update(termination=('converged' if converged else
        'best_feasible_not_stationary' if outcome.success else
        'iteration_limit' if int(outcome.status) == 9 else 'solver_failure'),
        success=converged, terminal_epigraph_feasible=feasible,
        message=str(outcome.message), iterations=int(outcome.nit),
        final_signed_margin=float(selected_margin), optimizer_t=float(outcome.x[-1]),
        scaled_improvement=float(initial_margin - selected_margin),
        solver_terminal=dict(physical_hash=terminal_hash, nominal_pass=bool(nominal_terminal),
            epigraph_feasible=feasible, local_stationary=converged),
        best_feasible_trial=dict(physical_hash=physical_design_hash(best['design']), signed_margin=float(best_margin)),
        returned_best=dict(physical_hash=physical_design_hash(selected['design']),
            role='solver_terminal' if converged else 'best_feasible_trial',
            is_solver_terminal=converged))
    return selected, details
