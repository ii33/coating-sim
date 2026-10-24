"""Analytic local refinement for automatically proposed reflector topologies.

Interior targets and finite phase-coordinate trust boxes are project search
conventions. Original objectives, physical scoring and acceptance stay owned by
synthesis; no physical call, budget or cancellation owner is introduced here.
"""
from copy import deepcopy
from dataclasses import replace
from importlib import import_module
import math
from typing import Callable, Literal, Mapping, Protocol, Sequence, TypedDict, cast

import numpy as np
from numpy.typing import NDArray

from .film_objective_criteria import (active_constraints, normalized_band_weights,
    reduce_objective_responses, signed_bound_count, validate_objective_criteria)


class _InequalityConstraint(TypedDict):
    type: Literal["ineq"]
    fun: Callable[[NDArray[np.float64]], float]
    jac: Callable[[NDArray[np.float64]], NDArray[np.float64]]


class _LocalMinimizerResult(Protocol):
    @property
    def message(self) -> str: ...

    @property
    def nit(self) -> int: ...


class _LocalMinimizer(Protocol):
    def __call__(self,
                 fun: Callable[[NDArray[np.float64]], tuple[float, NDArray[np.float64]]],
                 x0: NDArray[np.float64], *, jac: Literal[True],
                 bounds: Sequence[tuple[float, float]],
                 method: Literal["SLSQP", "L-BFGS-B"],
                 constraints: tuple[_InequalityConstraint, ...],
                 options: Mapping[str, int | float]) -> _LocalMinimizerResult: ...


def interior_bounds(constraint, acceptable, normalization_scale=1.):
    """Project search convention: slightly interior range targets, per mode."""
    lower, upper = constraint.lower_bound, constraint.upper_bound
    if constraint.mode != 'range':
        return lower, upper
    width = (1.0 if upper is None else upper) - (0.0 if lower is None else lower)
    if width <= 0:
        return lower, upper
    def margin(bound):
        return min(width * .1, max(width * .001, 8 * math.ulp(float(bound)), acceptable * normalization_scale))
    return (lower if lower is None or lower <= 0 else lower + margin(lower),
            upper if upper is None or upper >= 1 else upper - margin(upper))


def _interior_objectives(objectives, acceptable):
    result = []
    for objective in objectives:
        changed = {}
        for name, constraint in active_constraints(objective):
            if constraint.mode == 'range':
                lower, upper = interior_bounds(constraint, acceptable, objective.normalization_scale)
                changed[name] = replace(constraint, lower_bound=lower, upper_bound=upper)
        candidate = replace(objective, **changed)
        validate_objective_criteria(objective)
        try:
            validate_objective_criteria(candidate)
        except ValueError:
            # Tight coupled constraints may leave no common interior. Their
            # exact original feasible boundary remains the valid search guide.
            candidate = objective
        result.append(candidate)
    return tuple(result)


def _guide_reductions(request, value, layer_ids=(), *, interior=False):
    """Reduce each complete physical band before including private audit guides."""
    groups = [(request.objectives, value['responses'])]
    groups.extend(value.get('_feedback_response_groups', ()))
    for objectives, responses in groups:
        if interior:
            objectives = _interior_objectives(objectives, request.acceptable_normalized_error)
        yield reduce_objective_responses(objectives, responses, layer_ids=tuple(layer_ids),
                                         tolerance=request.acceptable_normalized_error)


def _reduction_loss_gradient(reductions, layer_ids):
    loss, gradient = 0., np.zeros(len(layer_ids))
    for reduction in reductions:
        residual = reduction.residual_vector
        loss += float(residual @ residual)
        gradient += 2 * (residual @ reduction.jacobian_matrix)
    if not math.isfinite(loss) or not np.all(np.isfinite(gradient)):
        raise ValueError('nonfinite reduced objective loss or derivative')
    return float(loss), gradient


def interior_loss(request, value):
    """Compute an active-mode interior guide from cached complete responses."""
    return _reduction_loss_gradient(_guide_reductions(request, value, interior=True), ())[0]


def interior_loss_gradient(request, value, layer_ids):
    """Use the shared physical reduction and its analytic derivative."""
    return _reduction_loss_gradient(_guide_reductions(request, value, layer_ids, interior=True), layer_ids)


def refine_synthesis_candidate(request, initial, *, evaluate, archive, quarter_nm,
                               cancel_check=lambda: None, progress=lambda *args: None,
                               max_iterations=64, candidate_key=None):
    """Refine through parent callbacks; stop/cancel exceptions propagate intact.

    Each layer has a trust interval d0 +/- quarter_nm/2, intersected with
    explicit manufacturing bounds. This limits each invocation's phase-box
    displacement; later scheduled invocations recenter it. A finite total cap
    uses SLSQP's linear inequality. Returned best uses original worst violation,
    then the interior guide loss, independent of transient optimizer iterates.
    """
    # Describe only the external numerical interface used by this local stage.
    # Resolve the live module attribute so existing SciPy monkeypatches remain visible.
    minimize = cast(_LocalMinimizer, import_module("scipy.optimize").minimize)
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
    def original_key(value, loss):
        score = value['score']
        return (int(not score.get('nominal_pass', score['max_error'] == 0.)),
                float(score['max_error']), float(score.get('weighted_rms_error', loss)), loss)
    key_function = original_key if candidate_key is None else candidate_key
    best_loss = initial_loss
    best_key = key_function(initial, math.inf if initial_loss is None else initial_loss)
    best_loss_source = ('initial_cached_responses' if initial_loss is not None
                        else 'initial_responses_unavailable_without_additional_physical_evaluation')
    cache: dict[tuple[float, ...], tuple[float, NDArray[np.float64]]] = {}
    calls = 0
    total_cap = request.max_total_thickness_nm
    method: Literal['SLSQP', 'L-BFGS-B'] = 'SLSQP' if total_cap is not None else 'L-BFGS-B'
    diagnostics = dict(method=method,
        max_iterations=max_iterations, lower_nm=low.tolist(), upper_nm=high.tolist(),
        bounds_source='project phase trust box d0 +/- quarter_nm/2 intersect explicit layer limits',
        fixed_layer_ids=[identifier for identifier, lo, hi in zip(ids, low, high, strict=True) if lo == hi],
        interior_bounds=[{name: interior_bounds(c, request.acceptable_normalized_error, o.normalization_scale)
                          for name, c in active_constraints(o)} for o in request.objectives],
        reason='running', iterations=0, physical_candidates=0)

    def objective(point):
        nonlocal best, best_key, calls, best_loss_source, best_loss
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
        current_key = key_function(value, loss)
        if current_key < best_key:
            best, best_key, best_loss = value, current_key, loss
            best_loss_source = 'parent_evaluated_candidate_responses'
        progress('interior_local_trial', value, dict(loss=loss, physical_candidates=calls))
        cancel_check()
        cache[key] = (loss, gradient * scales)
        return cache[key]

    constraints: tuple[_InequalityConstraint, ...] = ()
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
                       method=method, constraints=constraints, options=options)
    cancel_check()
    diagnostics.update(reason=str(outcome.message), iterations=int(getattr(outcome, 'nit', 0)),
                       physical_candidates=calls,
                       interior_loss=best_loss,
                       interior_loss_source=best_loss_source,
                       original_max_error=best['score']['max_error'])
    return best, diagnostics


class SynthesisStageBudget(Exception):
    """A scheduling slice ended; not convergence or a whole-search failure."""


class SynthesisResourceGuard(Exception):
    """A disclosed working-set estimate prevents this numerical operation."""


def synthesis_window_masses(request):
    """Normalized user band weights; physical wavelength measure is independent."""
    return normalized_band_weights(request.objectives)


def weighted_sse_loss_gradient(request, value, layer_ids=()):
    """Canonical active-band SSE and analytic nm gradient, without optics."""
    from .film_synthesis_multispectral_minimax import _signed_bounds
    ids = tuple(layer_ids)
    _signed_bounds(request, value, ids)  # complete original grid/Jacobian contract
    return _reduction_loss_gradient(_guide_reductions(request, value, ids), ids)


def _coordinate_thicknesses_nm(point, scales, lower, upper, initial_point, initial_nm):
    """Canonical nm reconstruction inside the declared encoded solver bounds.

    Dividing a physical bound by a scale and multiplying back can cross that
    bound by one ULP. Restore the exact initial design at its encoded point;
    at encoded bounds snap only multiplication-roundoff back to the legal
    physical bound. Genuinely out-of-bounds coordinates are not made feasible.
    """
    point = np.asarray(point, float)
    if np.array_equal(point, initial_point):
        return initial_nm.copy()
    thicknesses = point * scales
    encoded_lower = lower / scales
    thicknesses = np.where((thicknesses < lower) & (point >= encoded_lower), lower, thicknesses)
    if upper is not None:
        encoded_upper = upper / scales
        thicknesses = np.where((thicknesses > upper) & (point <= encoded_upper), upper, thicknesses)
    return thicknesses


def refine_synthesis_explorer(request, initial, *, evaluate, archive,
        cancel_check=lambda: None, progress=lambda *args: None, max_iterations=64):
    """Continue the actual accepted SSE endpoint, never the strict champion.

    All old layers, split hosts and new needles have independent variables.
    Exact-zero rows are allowed only as explicitly identified mathematical
    probes; positive minima/upper/total limits remain explicit. A slice expiry
    returns the last accepted solver iterate, not the best line-search sample.
    """
    minimize = import_module('scipy.optimize').minimize
    design = initial['design']
    rows = design['front_layers']
    if not rows:
        return initial, dict(method='none', termination='empty_topology', success=True,
                             physical_candidates=0, iterations=0)
    ids = tuple(row['id'] for row in rows)
    ds = np.asarray([row['thickness']['value'] for row in rows], float)
    scales = np.full(ds.shape, 100.)
    lower = request.minimum_nonzero_thickness_nm or 0.
    upper, total = request.max_layer_thickness_nm, request.max_total_thickness_nm
    bounds = [(lower / 100., None if upper is None else upper / 100.) for _ in rows]
    point = ds / scales
    cache_x = None
    cache_value = None
    cache_loss = None
    accepted = initial
    accepted_x = point.copy()
    calls = 0
    iterations = 0
    outcome = None

    def data(x):
        nonlocal cache_x, cache_value, cache_loss, calls
        cancel_check()
        x = np.asarray(x, float)
        if x.shape != point.shape or not np.all(np.isfinite(x)):
            raise ValueError('SSE solver returned invalid coordinates')
        if cache_x is not None and np.array_equal(x, cache_x):
            return cache_value, cache_loss
        thicknesses = _coordinate_thicknesses_nm(x, scales, lower, upper, point, ds)
        if (np.any(thicknesses < lower) or (upper is not None and np.any(thicknesses > upper))
                or (total is not None and float(sum(thicknesses)) > total)):
            raise ValueError('SSE solver requested infeasible physical thicknesses')
        trial = deepcopy(design)
        for row, thickness in zip(trial['front_layers'], thicknesses, strict=True):
            row['thickness'] = dict(value=float(thickness), unit='nm')
        zero_ids = tuple(row['id'] for row in trial['front_layers'] if row['thickness']['value'] == 0.)
        value = evaluate(trial, jacobian=True, zero_ids=zero_ids)
        calls += 1
        measured = weighted_sse_loss_gradient(request, value, ids)
        cache_x, cache_value, cache_loss = x.copy(), value, (measured[0], measured[1] * scales)
        archive(value, audit_eligible=False)
        progress('hybrid_sse_trial', value, dict(weighted_sse=measured[0], physical_candidates=calls))
        return value, cache_loss

    def objective(x):
        # Feasibility line-search guard, not a surrogate physical score.
        d = _coordinate_thicknesses_nm(x, scales, lower, upper, point, ds)
        below = np.minimum(d - lower, 0.)
        above = np.zeros_like(d) if upper is None else np.maximum(d - upper, 0.)
        excess = 0. if total is None else max(0., float(sum(d)) - total)
        if np.any(below) or np.any(above) or excess:
            cancel_check()
            return 1e6 + float(below @ below + above @ above) + excess**2, 2 * (below + above + excess) * scales
        return data(x)[1]

    def callback(x):
        nonlocal accepted, accepted_x, iterations
        # Acceptance itself costs no optics. Commit an exact complete cache
        # entry BEFORE the local slice check: the last admitted physical call
        # may exhaust the slice just before SciPy reports accepting that point.
        point_x = np.asarray(x, float)
        if cache_x is None or not np.array_equal(point_x, cache_x):
            objective(point_x)
        if cache_x is not None and np.array_equal(point_x, cache_x):
            accepted, accepted_x = cache_value, point_x.copy()
            iterations += 1
        cancel_check()  # Do not suppress caller cancellation or whole limits.
        progress('hybrid_sse_accepted_iterate', accepted, dict(iterations=iterations))

    constraints = () if total is None else ({'type': 'ineq',
        'fun': lambda x: total - float(np.asarray(x) @ scales), 'jac': lambda x: -scales},)
    method = 'L-BFGS-B' if total is None else 'SLSQP'
    options = dict(maxiter=max_iterations, ftol=1e-13)
    if total is None:
        options.update(gtol=1e-10, maxls=30)
    termination = 'solver_failure'
    try:
        # Reuse only a complete initial matrix with the exact optimized ids.
        if initial.get('responses') and all(set(r.get('jacobian_values_by_layer_id', ())) == set(ids)
                                            for r in initial['responses']):
            measured = weighted_sse_loss_gradient(request, initial, ids)
            cache_x, cache_value, cache_loss = point.copy(), initial, (measured[0], measured[1] * scales)
        else:
            data(point)
            accepted = cache_value
        outcome = minimize(objective, point, jac=True, bounds=bounds, method=method,
            constraints=constraints, callback=callback, options=options)
        endpoint = np.asarray(outcome.x, float)
        # The returned PHYSICALLY FEASIBLE endpoint, not an archived strict
        # winner or an infeasible solver trial, continues.
        endpoint_ds = _coordinate_thicknesses_nm(endpoint, scales, lower, upper, point, ds)
        endpoint_physical = (endpoint.shape == ds.shape and np.all(np.isfinite(endpoint_ds))
            and np.all(endpoint_ds >= lower) and (upper is None or np.all(endpoint_ds <= upper))
            and (total is None or float(sum(endpoint_ds)) <= total))
        if endpoint_physical:
            accepted, _ = data(endpoint)
            accepted_x = endpoint.copy()
        iterations = int(getattr(outcome, 'nit', 0))
        termination = ('solver_failure' if not endpoint_physical else
                       'fixed_bounds' if bool(outcome.success) and upper is not None and lower == upper else
                       'converged' if bool(outcome.success) else
                       'iteration_limit' if int(getattr(outcome, 'status', -1)) in (1, 9) else 'solver_failure')
    except SynthesisStageBudget:
        termination = 'stage_budget'
    except SynthesisResourceGuard:
        termination = 'resource_guard'
    terminal_loss = (weighted_sse_loss_gradient(request, accepted, ())[0]
                     if accepted.get('responses') else None)
    return accepted, dict(method=method, termination=termination,
        success=bool(outcome.success) if outcome is not None and termination in ('converged', 'fixed_bounds') else False,
        message=str(outcome.message) if outcome is not None else termination,
        iterations=iterations, physical_candidates=calls, weighted_sse=terminal_loss,
        endpoint_role='actual_solver_terminal_or_last_accepted_iterate_not_strict_champion',
        endpoint_thicknesses_nm=[float(row['thickness']['value']) for row in accepted['design']['front_layers']],
        fixed_layer_ids=list(ids) if upper is not None and lower == upper else [],
        free_thickness_variables=0 if upper is not None and lower == upper else len(ids),
        all_original_conditions=True, independent_layer_variables=list(ids),
        objective_masses=synthesis_window_masses(request).tolist())


def refine_synthesis_full_minimax(request, initial, *, evaluate, archive,
        cancel_check=lambda: None, progress=lambda *args: None, max_iterations=120):
    """Mature full-original-constraint SLSQP epigraph, with physical verification.

    No sparse active set and no optimizer t-only acceptance. Original responses
    and Jacobians define every inequality. The strict best is independently
    retained while diagnostics describe the actual epigraph terminal only.
    """
    from .film_synthesis_multispectral_minimax import _signed_bounds
    from .film_synthesis_archive import physical_design_hash
    minimize = import_module('scipy.optimize').minimize
    design = initial['design']
    rows = design['front_layers']
    if not rows:
        return initial, dict(method='none', termination='empty_topology', success=True,
                             physical_candidates=0, all_original_conditions=True)
    ids = tuple(row['id'] for row in rows)
    ds = np.asarray([row['thickness']['value'] for row in rows], float)
    scales = np.full(ds.shape, 100.)
    lower = request.minimum_nonzero_thickness_nm or np.finfo(float).tiny
    upper, total = request.max_layer_thickness_nm, request.max_total_thickness_nm
    bounds = [(lower / 100., None if upper is None else upper / 100.) for _ in rows]
    best = initial
    terminal = initial
    accepted = initial
    accepted_t = None
    accepted_iterations = 0
    cache_x = cache_data = cache_value = None
    calls = 0
    outcome = None
    q_terminal = None
    t_terminal = None
    full_feasibility = None
    rejected_infeasible_trials = 0
    condition_count = signed_bound_count(request.objectives)

    def data(x):
        nonlocal cache_x, cache_data, cache_value, best, terminal, calls, rejected_infeasible_trials
        cancel_check()
        x = np.asarray(x, float)
        if x.shape != ds.shape or not np.all(np.isfinite(x)):
            raise ValueError('full minimax returned invalid coordinates')
        if cache_x is not None and np.array_equal(x, cache_x):
            return cache_data
        thicknesses = _coordinate_thicknesses_nm(x, scales, lower, upper, ds / scales, ds)
        below = np.minimum(thicknesses - lower, 0.)
        above = np.zeros_like(thicknesses) if upper is None else np.maximum(thicknesses - upper, 0.)
        excess = 0. if total is None else max(0., float(sum(thicknesses)) - total)
        if np.any(below) or np.any(above) or excess:
            # SLSQP may request a trial outside its total inequality. Reject it
            # before optics and supply a differentiable feasibility penalty;
            # this is never archived or called an original physical score.
            rejected_infeasible_trials += 1
            penalty = 1e6 + float(below @ below + above @ above) + excess**2
            penalty_jac = 2 * (below + above + excess) * scales
            return np.full(condition_count, penalty), np.tile(penalty_jac, (condition_count, 1))
        trial = deepcopy(design)
        for row, thickness in zip(trial['front_layers'], thicknesses, strict=True):
            row['thickness'] = dict(value=float(thickness), unit='nm')
        value = evaluate(trial, jacobian=True)
        q, jac, _ = _signed_bounds(request, value, ids)
        cache_x, cache_data, cache_value = x.copy(), (q, jac * scales), value
        terminal = value
        calls += 1
        from .film_synthesis_multispectral_minimax import _score_key
        if _score_key(value) < _score_key(best):
            best = value
        archive(value, audit_eligible=False)
        progress('hybrid_full_minimax_trial', value, dict(physical_candidates=calls,
            full_original_signed_constraints=len(q), physical_max_error=value['score']['max_error']))
        return cache_data

    termination = 'solver_failure'
    try:
        x0 = ds / scales
        if initial.get('responses') and all(set(r.get('jacobian_values_by_layer_id', ())) == set(ids)
                                            for r in initial['responses']):
            q, jac, _ = _signed_bounds(request, initial, ids)
            cache_x, cache_data, cache_value = x0.copy(), (q, jac * scales), initial
        else:
            data(x0)
        assert cache_data is not None
        start: NDArray[np.float64] = np.concatenate((x0, np.asarray([max(0., float(np.max(cache_data[0])))], float)))
        def constraint(x):
            q, _ = data(x[:-1])
            return x[-1] - q
        def derivative(x):
            _, jac = data(x[:-1])
            return np.column_stack((-jac, np.ones(len(jac))))
        constraints = [{'type': 'ineq', 'fun': constraint, 'jac': derivative}]
        if total is not None:
            constraints.append({'type': 'ineq', 'fun': lambda x: total - float(x[:-1] @ scales),
                                'jac': lambda x: np.concatenate((-scales, [0.]))})
        def accepted_callback(x):
            nonlocal accepted, accepted_t, accepted_iterations
            point_x = np.asarray(x[:-1], float)
            if cache_x is None or not np.array_equal(point_x, cache_x):
                data(point_x)
            if cache_x is not None and np.array_equal(point_x, cache_x):
                accepted, accepted_t = cache_value, float(x[-1])
                accepted_iterations += 1
            cancel_check()
        outcome = minimize(lambda x: float(x[-1]), start,
            jac=lambda x: np.concatenate((np.zeros(len(ids)), [1.])), method='SLSQP',
            bounds=[*bounds, (0., None)], constraints=tuple(constraints), callback=accepted_callback,
            options=dict(maxiter=max_iterations, ftol=1e-11))
        endpoint = np.asarray(outcome.x, float)
        if endpoint.shape != (len(ids) + 1,) or not np.all(np.isfinite(endpoint)):
            raise ValueError('full minimax returned invalid epigraph endpoint')
        endpoint_ds = _coordinate_thicknesses_nm(endpoint[:-1], scales, lower, upper, ds / scales, ds)
        endpoint_physical = (np.all(endpoint_ds >= lower)
            and (upper is None or np.all(endpoint_ds <= upper))
            and (total is None or float(sum(endpoint_ds)) <= total))
        t_terminal = float(endpoint[-1])
        if endpoint_physical:
            q_terminal = float(np.max(data(endpoint[:-1])[0]))
            terminal = cache_value
            full_feasibility = q_terminal <= t_terminal + 1e-8 and t_terminal >= -1e-12
        else:
            terminal = accepted
            full_feasibility = False
        termination = ('converged' if bool(outcome.success) and full_feasibility else
                       'iteration_limit' if int(getattr(outcome, 'status', -1)) == 9 else 'solver_failure')
    except SynthesisStageBudget:
        termination = 'stage_budget'
        terminal, t_terminal = accepted, accepted_t
    except SynthesisResourceGuard:
        termination = 'resource_guard'
        terminal, t_terminal = accepted, accepted_t
    if q_terminal is None and terminal.get('responses'):
        q_terminal = float(np.max(_signed_bounds(request, terminal, ())[0]))
    return best, dict(method='SLSQP_full_original_epigraph', termination=termination,
        success=bool(outcome.success) and bool(full_feasibility) if outcome is not None else False,
        message=str(outcome.message) if outcome is not None else termination,
        iterations=int(getattr(outcome, 'nit', 0)) if outcome is not None else accepted_iterations, physical_candidates=calls,
        rejected_infeasible_trials=rejected_infeasible_trials,
        all_original_conditions=True, sparse_active_set=False,
        fixed_layer_ids=list(ids) if upper is not None and lower == upper else [],
        free_thickness_variables=0 if upper is not None and lower == upper else len(ids),
        original_signed_constraints=(0 if cache_data is None else len(cache_data[0])),
        terminal_role=('actual_solver_endpoint' if outcome is not None and termination not in ('stage_budget', 'resource_guard')
                       else 'last_accepted_iterate_or_initial_when_interrupted'),
        terminal_physical_hash=physical_design_hash(terminal['design']),
        terminal_max_error=float(terminal['score']['max_error']),
        terminal_signed_max=q_terminal, optimizer_t=t_terminal,
        terminal_full_grid_epigraph_feasible=full_feasibility,
        returned_role='strict_original_grid_champion_not_optimizer_t')
