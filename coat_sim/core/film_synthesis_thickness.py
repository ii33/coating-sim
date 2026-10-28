"""Fixed-topology physical thickness reduction under the original optical goals.

This is a feasible-candidate cost operator, not a spectral/sensitivity optimizer.
The caller owns evaluation, archival, budget/cancellation and independent audit.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import import_module
import math
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from .film_objective_criteria import signed_bound_count, wavelength_measure
from .film_synthesis_multispectral_minimax import _original_reduction, _validate_objectives
from .film_synthesis_refinement import SynthesisResourceGuard, _coordinate_thicknesses_nm

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest


Value = dict[str, Any]


def optical_quality_by_group(request: FilmSynthesisRequest, value: Value) -> dict[tuple[int, float, str], float]:
    """Directed worst normalized condition per enabled objective/angle/reduction.

    Interior interval sides retain their signed safety distance; physical R/T
    endpoints do not pull AR/HR toward the interval centre. Value and zero-width
    targets retain the original nonnegative violation, never an invented margin.
    """
    from .film_synthesis_margin import _field, _margin_sides
    result: dict[tuple[int, float, str], float] = {}
    for row in value['score']['errors']:
        index, reduction = _field(row, 'objective_index'), _field(row, 'reduction')
        objective = request.objectives[index]
        criterion = getattr(objective, reduction)
        lower, upper = _margin_sides(criterion)
        actual = float(_field(row, 'actual'))
        sides = []
        if lower:
            sides.append((criterion.lower_bound - actual) / objective.normalization_scale)
        if upper:
            sides.append((actual - criterion.upper_bound) / objective.normalization_scale)
        quality = max(sides) if sides else float(_field(row, 'normalized_violation'))
        key = (index, float(_field(row, 'angle_deg')), reduction)
        result[key] = max(result.get(key, -math.inf), quality)
    if not result or any(not math.isfinite(number) for number in result.values()):
        raise ValueError('optical quality requires complete finite original condition scores')
    return result


def preserves_optical_quality(request: FilmSynthesisRequest, source: Value, candidate: Value) -> bool:
    before, after = optical_quality_by_group(request, source), optical_quality_by_group(request, candidate)
    return before.keys() == after.keys() and all(after[key] <= before[key] for key in before)


def thickness_reduction_working_set_bytes(request: FilmSynthesisRequest, layers: int) -> int:
    """Conservative project estimate, not a layer limit or OS memory guarantee.

    Includes forward/Jacobian work and dense SLSQP's quadratic workspace. The
    parent resource callback additionally accounts for its retained archive.
    """
    points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    bounds = signed_bound_count(request.objectives) + 1
    return 128 * 1024**2 + 64 * points * layers + 128 * layers**2 + 64 * bounds * layers + 512 * (layers + bounds)


def reduce_total_thickness(request: FilmSynthesisRequest, initial: Value, *,
        evaluate: Callable[..., Value], archive: Callable[..., None],
        cancel_check: Callable[[], None] = lambda: None,
        progress: Callable[..., None] = lambda *args: None,
        resource_check: Callable[..., None] = lambda *args, **kwargs: None,
        max_iterations: int = 64, preserve_quality: bool = False) -> tuple[Value, dict[str, Any]]:
    """Minimize sum(d_nm) at fixed layer IDs/materials with original q <= 0.

    Normally the initial candidate must pass every active nominal constraint.
    With preserve_quality, each enabled objective/angle/reduction instead keeps
    its initial directed worst normalized value as an internal sublevel; this
    also supports incomplete goals without modifying the original request or
    reporting them as passed. Final independent audit remains the caller's job.
    Solver tolerance does not grant acceptance, especially for strict bounds.
    Return the thinnest actually evaluated nominally passing candidate, never
    a penalty value or a merely solver-acceptable endpoint. All callback errors
    (including budget, resource and cancel) propagate unchanged. A parent can
    preserve completed candidates through archive even if this call is stopped.
    """
    cancel_check()
    _validate_objectives(request)
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError('thickness reduction max_iterations must be a positive integer')
    design = initial['design']
    rows = design['front_layers']
    ids = tuple(row['id'] for row in rows)
    if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
        raise ValueError('thickness reduction requires unique nonempty layer ids')
    if request.max_layers is not None and len(ids) > request.max_layers:
        raise ValueError('thickness reduction initial topology exceeds max_layers')
    if any(row['thickness']['unit'] != 'nm' or isinstance(row['thickness']['value'], bool)
           for row in rows):
        raise ValueError('thickness reduction requires normalized nm thicknesses')
    for name in ('minimum_nonzero_thickness_nm', 'max_layer_thickness_nm', 'max_total_thickness_nm'):
        number = getattr(request, name)
        if number is not None and (isinstance(number, bool) or not math.isfinite(number) or number <= 0):
            raise ValueError(f'{name} must be positive finite nm or None')
    lower = request.minimum_nonzero_thickness_nm or np.finfo(float).tiny
    upper, total_cap = request.max_layer_thickness_nm, request.max_total_thickness_nm
    ds = np.asarray([row['thickness']['value'] for row in rows], float)
    initial_total = float(sum(ds))
    quality_bounds = optical_quality_by_group(request, initial) if preserve_quality else None

    def accepted(value, reduced):
        return (preserves_optical_quality(request, initial, value) if preserve_quality else reduced.nominal_pass)

    def optical_caps(reduced):
        if quality_bounds is None:
            return np.zeros(len(reduced.signed_bound_values))
        from .film_synthesis_margin import _margin_sides
        caps: list[float] = []
        for condition in reduced.conditions:
            criterion = getattr(request.objectives[condition.objective_index], condition.reduction)
            cap = quality_bounds[(condition.objective_index, float(condition.angle_deg), condition.reduction)]
            sides = _margin_sides(criterion)
            if criterion.mode == 'value':
                caps.extend((cap, cap))
            else:
                degenerate = (criterion.lower_bound is not None and criterion.upper_bound is not None
                    and criterion.lower_bound == criterion.upper_bound)
                if criterion.lower_bound is not None:
                    caps.append(cap if sides[0] or degenerate else 0.)
                if criterion.upper_bound is not None:
                    caps.append(cap if sides[1] or degenerate else 0.)
        return np.asarray(caps, float)

    def physical(d):
        return bool(d.shape == ds.shape and np.all(np.isfinite(d)) and np.all(d >= lower)
            and (upper is None or np.all(d <= upper)) and math.isfinite(float(sum(d)))
            and (total_cap is None or float(sum(d)) <= total_cap))

    if not physical(ds):
        raise ValueError('thickness reduction requires physically feasible initial thicknesses')
    # Resolve the existing frozen request policy before dense arrays or optics.
    from .film_auto_synthesis import _resolve_synthesis_resource_guard
    if request.max_working_set_bytes is not None and (type(request.max_working_set_bytes) is not int
            or request.max_working_set_bytes < 0):
        raise ValueError('max_working_set_bytes must be 0 (auto), positive, or None')
    resolution = _resolve_synthesis_resource_guard(request)
    estimated_bytes = thickness_reduction_working_set_bytes(request, len(ids))
    if resolution['effective_bytes'] is not None and estimated_bytes > resolution['effective_bytes']:
        raise SynthesisResourceGuard('thickness reduction estimated working set exceeds resolved resource guard')
    resource_check(design, jacobian=True, epigraph=True)
    cancel_check()
    calls = hits = rejected = repair_nextafter = repair_bisection = 0
    best = initial
    best_total = initial_total
    best_reduction = None
    cache_d = cache_value = cache_reduction = None
    scales = np.full(ds.shape, 100.)
    x0 = ds / scales

    def commit(value, d, *, physical_call):
        nonlocal calls, best, best_total, best_reduction, cache_d, cache_value, cache_reduction
        reduced = _original_reduction(request, value, ids)
        candidate_total = float(sum(d))
        if accepted(value, reduced) and candidate_total < best_total:
            best, best_total, best_reduction = value, candidate_total, reduced
        cache_d, cache_value, cache_reduction = d.copy(), value, reduced
        if physical_call:
            calls += 1
            archive(value, audit_eligible=False)
            progress('thickness_reduction_trial', value, dict(physical_candidates=calls,
                total_thickness_nm=candidate_total, nominal_pass=reduced.nominal_pass))
        cancel_check()
        return value, reduced

    def measure(d):
        nonlocal hits
        cancel_check()
        if cache_d is not None and np.array_equal(d, cache_d):
            hits += 1
            return cache_value, cache_reduction
        trial = deepcopy(design)
        for row, thickness in zip(trial['front_layers'], d, strict=True):
            row['thickness'] = dict(value=float(thickness), unit='nm')
        resource_check(trial, jacobian=True, epigraph=True)
        cancel_check()
        value = evaluate(trial, jacobian=True)
        if value['design'] != trial:
            raise ValueError('thickness reduction evaluator returned another design')
        return commit(value, d, physical_call=True)

    # Raw complete spectra, not score flags or tolerated error, establish entry.
    if initial.get('responses'):
        best_reduction = _original_reduction(request, initial, ())
        if not preserve_quality and not best_reduction.nominal_pass:
            raise ValueError('thickness reduction requires a nominally passing initial candidate')
    if not ids:
        if best_reduction is None:
            _, best_reduction = measure(ds)
    elif initial.get('responses') and all(
            'jacobian_values_by_layer_id' in row for row in initial['responses']):
        _, best_reduction = commit(initial, ds, physical_call=False)
    else:
        refreshed, reduced = measure(ds)
        if best_reduction is None:
            best, best_reduction = refreshed, reduced
    if not cache_reduction and ids:
        raise AssertionError('initial derivative contract was not established')
    if best_reduction is None or (not preserve_quality and (not best_reduction.nominal_pass or (
            cache_reduction is not None and not cache_reduction.nominal_pass))):
        raise ValueError('thickness reduction requires a nominally passing initial candidate')

    count = signed_bound_count(request.objectives)

    def coordinates(x):
        cancel_check()
        x = np.asarray(x, float)
        if x.shape != ds.shape or not np.all(np.isfinite(x)):
            raise ValueError('thickness reduction solver returned invalid coordinates')
        with np.errstate(over='ignore', invalid='ignore'):
            d = _coordinate_thicknesses_nm(x, scales, lower, upper, x0, ds)
            total = float(sum(d))
        if not np.all(np.isfinite(d)) or not math.isfinite(total):
            raise ValueError('thickness reduction solver returned nonfinite physical thicknesses')
        return d

    def objective(x):
        return float(sum(coordinates(x))) / 100.

    def gradient(x):
        coordinates(x)
        return np.ones(len(ids))

    def constraint_data(x):
        nonlocal rejected
        d = coordinates(x)
        if not physical(d):
            rejected += 1
            below = np.minimum(d - lower, 0.)
            above = np.zeros_like(d) if upper is None else np.maximum(d - upper, 0.)
            excess = 0. if total_cap is None else max(0., float(sum(d)) - total_cap)
            penalty = 1e6 + float(below @ below + above @ above) + excess**2
            derivative = 2 * (below + above + excess) * scales
            return np.full(count, -penalty), np.tile(-derivative, (count, 1))
        _, reduced = measure(d)
        return optical_caps(reduced) - reduced.signed_bound_values, -reduced.signed_bound_jacobian * scales

    constraints = [dict(type='ineq', fun=lambda x: constraint_data(x)[0], jac=lambda x: constraint_data(x)[1])]
    # A feasible initial total is a lossless objective sublevel, not an added
    # manufacturing limit: every strictly improving candidate lies inside it.
    constraints.append(dict(type='ineq', fun=lambda x: (initial_total - float(sum(coordinates(x)))) / 100.,
        jac=lambda x: -gradient(x)))
    outcome = None
    terminal_value: Value | None = initial
    terminal_reduction = best_reduction
    terminal_physical = True
    if ids and not (upper is not None and lower == upper):
        cancel_check()
        outcome = import_module('scipy.optimize').minimize(objective, x0, jac=gradient,
            bounds=[(lower / 100., None if upper is None else upper / 100.) for _ in ids],
            method='SLSQP', constraints=tuple(constraints),
            options=dict(maxiter=max_iterations, ftol=1e-12))
        cancel_check()
        endpoint = coordinates(outcome.x)
        terminal_physical = physical(endpoint)
        if terminal_physical:
            terminal_value, terminal_reduction = measure(endpoint)
            # Repair representation-level strict-bound failures first, then
            # search a bounded segment toward a known feasible incumbent. No
            # altered goal, epsilon acceptance or undisclosed safety margin.
            if not accepted(terminal_value, terminal_reduction) and float(sum(endpoint)) < best_total:
                anchor = np.asarray([r['thickness']['value'] for r in best['design']['front_layers']])
                near = endpoint.copy()
                for _ in range(4):
                    cancel_check()
                    new = np.nextafter(near, anchor)
                    if np.array_equal(new, near):
                        break
                    near = new
                    if not physical(near):
                        break
                    repair_nextafter += 1
                    repaired, reduced = measure(near)
                    if accepted(repaired, reduced):
                        break
                assert cache_reduction is not None
                if not accepted(cache_value, cache_reduction):
                    passing, failing = anchor.copy(), near.copy()
                    for _ in range(8):
                        cancel_check()
                        middle = .5 * passing + .5 * failing
                        if np.array_equal(middle, passing) or np.array_equal(middle, failing):
                            break
                        if not physical(middle):
                            break
                        repair_bisection += 1
                        repaired, reduced = measure(middle)
                        if accepted(repaired, reduced):
                            passing = middle
                        else:
                            failing = middle
        else:
            terminal_value = terminal_reduction = None

    cancel_check()
    extrema = []
    for coverage in best_reduction.coverage:
        obj = request.objectives[coverage.objective_index]
        weights = wavelength_measure(coverage.wavelength_nm, obj.wavelength_min_nm, obj.wavelength_max_nm)
        extrema.append(dict(objective_index=coverage.objective_index, angle_deg=coverage.angle_deg,
            metric=obj.metric, polarization=obj.polarization,
            sampled_min=float(min(coverage.actual_values)), sampled_max=float(max(coverage.actual_values)),
            wavelength_mean=float(weights @ coverage.actual_values)))
    termination = ('empty_topology' if not ids else 'fixed_bounds' if outcome is None else
        'solver_converged' if outcome.success else 'iteration_limit' if getattr(outcome, 'status', None) == 9
        else 'solver_failure')
    return best, dict(method='fixed_topology_total_thickness_slsqp', termination=termination,
        solver_success=bool(outcome.success) if outcome is not None else True,
        message=str(outcome.message) if outcome is not None else termination,
        iterations=int(getattr(outcome, 'nit', 0)), max_iterations=max_iterations,
        physical_candidates=calls, cache_hits=hits, rejected_infeasible_trials=rejected,
        initial_total_thickness_nm=initial_total, final_total_thickness_nm=best_total,
        cost_sublevel_total_nm=initial_total,
        reduction_nm=initial_total - best_total, improved=best_total < initial_total,
        nominal_pass=best_reduction.nominal_pass, max_signed_violation=float(max(best_reduction.signed_bound_values)),
        layer_count=len(ids), layer_ids=list(ids), signed_bound_conditions=count,
        boundary_nextafter_evaluations=repair_nextafter, boundary_bisection_evaluations=repair_bisection,
        terminal_physically_feasible=terminal_physical,
        terminal_nominal_pass=None if terminal_reduction is None else terminal_reduction.nominal_pass,
        terminal_total_thickness_nm=None if terminal_value is None else float(sum(
            row['thickness']['value'] for row in terminal_value['design']['front_layers'])),
        optical_extrema=extrema, estimated_working_set_bytes=estimated_bytes,
        resource_guard_resolution=resolution,
        acceptance_scope=('per-enabled-objective/angle/reduction signed-worst sublevel of the local result; original targets unchanged; independent audit required'
            if preserve_quality else 'original nominal constraints on training grid; independent original audit required'),
        preserves_local_optical_quality=preserve_quality,
        boundary_repair_policy='at most 4 physical-nm nextafter steps then 8 segment bisections; no goal margins')
