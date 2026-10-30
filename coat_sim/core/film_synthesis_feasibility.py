"""Bounded, fixed-topology feasibility repair after an independent audit.

Interior bounds are numerical guides only. Original scores, quadrature and
nominal acceptance remain unchanged. The synthesis parent owns all work,
budgets, cancellation, archival and subsequent independent audits.
"""
from copy import deepcopy
from dataclasses import replace
from importlib import import_module
import math
from typing import Callable, Mapping, Protocol, Sequence, TypedDict, cast

import numpy as np

from .film_synthesis_surfaces import selected_rows, selected_row_sites

from .film_objective_criteria import FilmConditionError, active_constraints, reduce_objective_responses
from .film_optimization import FilmOptimizationObjective, _checked_frozen_snapshot
from .film_optimization_derivatives import _PreparedMaterialData
from .film_synthesis_physics import simulate_synthesis_with_thickness_jacobian as simulate_with_thickness_jacobian
from .film_optimization_scoring import reduction_score
from .film_optimization_structure import _StructureCancelled
from .film_synthesis_hr_minimax import Array, _Constraint, _Result
from .film_synthesis_refinement import SynthesisStageBudget, SynthesisResourceGuard


class _CallbackMinimize(Protocol):
    """Existing scalar minimizer boundary, with the accepted-step callback."""

    def __call__(self, fun: Callable[[Array], float], x0: Array, *,
                 jac: Callable[[Array], Array], method: str,
                 bounds: Sequence[tuple[float | None, float | None]],
                 constraints: Sequence[_Constraint], callback: Callable[[Array], None],
                 options: Mapping[str, int | float]) -> _Result: ...


# Project search conventions, not physical tolerances or manufacturing limits.
INTERIOR_FRACTION = 2e-4
MAX_REPAIR_ROUNDS = 3
MAX_REPAIR_EVALUATIONS = 64
MAX_LOCAL_ITERATIONS = 40


class _InteriorMargin(TypedDict):
    objective_index: int
    reduction: str
    common_width: float
    lower_margin: float
    upper_margin: float


def interior_guides(objectives) -> tuple[tuple[FilmOptimizationObjective, ...], list[_InteriorMargin]]:
    """Shrink enabled ranges inside their common physical feasible interval.

    Value/zero-width targets have no interior. Inclusive 0/1 bounds are untouched;
    strict >0 / <1 boundaries may move inward when a physical interior exists.
    An eight-ULP displacement is used only when it fits below width/4; otherwise
    that side stays original. Coupled mean/pointwise ranges cannot be emptied.
    """
    guides = []
    records: list[_InteriorMargin] = []
    for index, objective in enumerate(objectives):
        active = active_constraints(objective)
        intervals = [(c.target_value, c.target_value) if c.mode == 'value' else
                     (0. if c.lower_bound is None else c.lower_bound,
                      1. if c.upper_bound is None else c.upper_bound) for _, c in active]
        width = min(hi for _, hi in intervals) - max(lo for lo, _ in intervals)
        changes = {}
        for name, constraint in active:
            margins = {'lower': 0., 'upper': 0.}
            if constraint.mode == 'range' and width > 0.:
                bounds = {}
                for side, sign in (('lower', 1.), ('upper', -1.)):
                    bound = getattr(constraint, side + '_bound')
                    if bound is None:
                        continue
                    strict_inward = ((side == 'lower' and bound == 0. and constraint.strict_lower)
                                      or (side == 'upper' and bound == 1. and constraint.strict_upper))
                    if (bound <= 0. or bound >= 1.) and not strict_inward:
                        continue
                    delta = max(width * INTERIOR_FRACTION, 8 * math.ulp(bound))
                    if delta <= width * .25:
                        shifted = bound + sign * delta
                        if shifted != bound:
                            bounds[side + '_bound'] = shifted
                            margins[side] = abs(shifted - bound)
                if bounds:
                    changes[name] = replace(constraint, **bounds)
            records.append(dict(objective_index=index, reduction=name, common_width=width,
                                lower_margin=margins['lower'], upper_margin=margins['upper']))
        guides.append(replace(objective, **changes))
    return tuple(guides), records


def near_feasible_conditions(objectives: Sequence[FilmOptimizationObjective],
                             errors: Sequence[FilmConditionError],
                             margins: Sequence[_InteriorMargin]) -> bool:
    """Pure per-condition gate; admits no audit, solver or physical work."""
    thresholds = {}
    for row in margins:
        objective = objectives[row['objective_index']]
        constraint = getattr(objective, row['reduction'])
        bound_ulps = [8 * math.ulp(float(bound)) for bound in (
            constraint.lower_bound, constraint.upper_bound, constraint.target_value) if bound is not None]
        thresholds[row['objective_index'], row['reduction']] = max(
            row['lower_margin'], row['upper_margin'], *bound_ulps) / objective.normalization_scale
    return bool(errors) and not any(
        e.normalized_violation > thresholds[e.objective_index, e.reduction] for e in errors)


def canonical_candidate(value):
    """Strip private repair evidence before global archival or serialization."""
    return {k: v for k, v in value.items() if not k.startswith('_feasibility_')}


class PreparedFeasibilityEvaluator:
    """One immutable audit snapshot and exact joint wavelength grids.

    evaluate() temporarily exposes joint responses for parent work accounting.
    restore() restores the original responses BEFORE any score/rank observer.
    Only the private constraint arrays include extra audit conditions/guides.
    """
    def __init__(self, request, audited, *, use_interior=True, previous=None):
        self.request = request
        self.objectives = request.objectives
        self.guides, self.margin_records = interior_guides(self.objectives)
        if not use_interior:
            self.guides = self.objectives
        self.points: dict[tuple[int, float], set[float]] = {}
        self.mean_grids: dict[tuple[int, float], tuple[float, ...]] = {}
        coverage = {(r.objective_index, r.angle_deg): r for r in audited['score']['coverage']}
        for error in audited['score']['errors']:
            constraint = getattr(self.objectives[error.objective_index], error.reduction)
            strict_failure = ((constraint.strict_lower and error.actual <= constraint.lower_bound)
                              or (constraint.strict_upper and error.actual >= constraint.upper_bound))
            if error.normalized_violation <= 0. and not strict_failure:
                continue
            key = error.objective_index, error.angle_deg
            if error.reduction == 'pointwise':
                self.points.setdefault(key, set()).add(error.wavelength_nm)
            else:
                self.mean_grids[key] = tuple(coverage[key].wavelength_nm)
        if previous is not None:
            for key, points in previous.points.items():
                self.points.setdefault(key, set()).update(points)
            for key, grid in previous.mean_grids.items():
                # This is the original fixed dense quadrature, not a sparse
                # union of extrema substituted into a mean/RMS denominator.
                self.mean_grids.setdefault(key, grid)
        payload = _checked_frozen_snapshot(request.frozen_material_snapshot, request.environment)
        self.materials = _PreparedMaterialData(payload, request.environment)
        self.grids = {}
        self.additional_conditions = 0
        self.additional_points = 0
        for index, objective in enumerate(self.objectives):
            base = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                               objective.wavelength_samples)
            for angle in objective.angles_deg:
                key = index, angle
                extra = self.points.get(key, set()) | set(self.mean_grids.get(key, ()))
                union = np.asarray(sorted(set(base) | extra))
                self.grids[key] = base, union
                self.additional_points += len(union) - len(base)
                self.additional_conditions += len(self.points.get(key, set()) - set(base))
                self.additional_conditions += int(key in self.mean_grids)

    def evaluate(self, design, *, cancel_event=None):
        ids = tuple(row['id'] for row in selected_rows(self.request, design))
        variables = [dict(side=side, layer_id=row['id'], lower_nm=0., upper_nm=np.finfo(float).max)
                     for side, row in selected_row_sites(self.request, design)]
        base_frames, work_frames, extras = [], [], []
        limit = min(10001, 1_000_000 // (len(design["front_layers"]) + len(design["back_layers"]) + 3))
        for index, objective in enumerate(self.objectives):
            for angle in objective.angles_deg:
                key = index, angle
                base, waves = self.grids[key]
                raw_values, raw_matrices = [], []
                for start in range(0, len(waves), limit):
                    if cancel_event is not None and cancel_event.is_set():
                        raise _StructureCancelled()
                    chunk = waves[start:start + limit]
                    response = simulate_with_thickness_jacobian(design, self.materials, variables,
                        dict(wavelength_min_nm=float(chunk[0]), wavelength_max_nm=float(chunk[-1]),
                             samples=len(chunk), angle_deg=angle, polarization=objective.polarization,
                             first_surface=design['first_surface']), wavelengths_nm=chunk)
                    raw_values.extend(response[objective.metric])
                    raw_matrices.extend(response['jacobian_' + objective.metric])
                values, matrix = np.asarray(raw_values), np.asarray(raw_matrices)
                if matrix.shape != (len(waves), len(ids)) or not np.all(np.isfinite(matrix)):
                    raise ValueError('feasibility repair has no complete finite analytic gradient')
                def frame(selected, objective_index=index, _waves=waves, _values=values, _matrix=matrix, _angle=angle):
                    positions = np.searchsorted(_waves, selected)
                    return dict(objective_index=objective_index, angle_deg=float(_angle),
                        wavelength_nm=list(map(float, selected)), actual_values=_values[positions].tolist(),
                        jacobian_values_by_layer_id={layer: _matrix[positions, column].tolist()
                                                    for column, layer in enumerate(ids)})
                base_frames.append(frame(base))
                work_frames.append(dict(objective_index=index, angle_deg=float(angle),
                                        wavelength_nm=waves.tolist(), actual_values=values.tolist()))
                for wave in sorted(self.points.get(key, set()) - set(base)):
                    isolated = replace(self.guides[index], wavelength_min_nm=wave,
                        wavelength_max_nm=wave, wavelength_samples=1, angles_deg=(angle,),
                        mean=replace(objective.mean, enabled=False))
                    extras.append((isolated, frame([wave], 0)))
                if key in self.mean_grids:
                    grid = self.mean_grids[key]
                    isolated = replace(self.guides[index], wavelength_samples=len(grid), angles_deg=(angle,),
                                       pointwise=replace(objective.pointwise, enabled=False))
                    extras.append((isolated, frame(grid, 0)))
        reduction = reduce_objective_responses(self.objectives, base_frames, layer_ids=ids,
                                               tolerance=self.request.acceptable_normalized_error)
        guide = reduce_objective_responses(self.guides, base_frames, layer_ids=ids)
        bounds, jacobians = [guide.signed_bound_values], [guide.signed_bound_jacobian]
        for objective, response in extras:
            extra = reduce_objective_responses((objective,), (response,), layer_ids=ids)
            bounds.append(extra.signed_bound_values)
            jacobians.append(extra.signed_bound_jacobian)
        score = dict(design=deepcopy(design), **reduction_score(reduction, self.request.acceptable_normalized_error))
        return dict(design=deepcopy(design), score=score,
                    responses=work_frames, _feasibility_original_responses=base_frames,
                    _feasibility_bounds=np.concatenate(bounds), _feasibility_jacobian=np.vstack(jacobians),
                    smooth_loss=float(reduction.residual_vector @ reduction.residual_vector),
                    gradient_by_layer_id=dict(zip(ids, 2 * reduction.jacobian_matrix.T @ reduction.residual_vector, strict=True)))

    @staticmethod
    def restore(value):
        value['responses'] = value.pop('_feasibility_original_responses')
        return value


def refine_feasibility_candidate(request, initial, *, evaluate, archive,
                                 cancel_check=lambda: None, progress=lambda *args: None,
                                 max_iterations=MAX_LOCAL_ITERATIONS):
    """Full original + admitted audit inequalities, with an internal guide only."""
    minimize = cast(_CallbackMinimize, import_module('scipy.optimize').minimize)
    design = initial['design']
    ds = np.asarray([r['thickness']['value'] for r in selected_rows(request, design)], float)
    if not len(ds):
        return initial, dict(termination='empty_topology', physical_candidates=0)
    # Fixed topology; finite phase-local box, never a new manufacturing cap.
    scales = np.full(ds.shape, 100.)
    lower = np.maximum(request.minimum_nonzero_thickness_nm or np.finfo(float).tiny, ds - 5.)
    upper = ds + 5.
    if request.max_layer_thickness_nm is not None:
        upper = np.minimum(upper, request.max_layer_thickness_nm)
    x0 = ds / scales
    q0, j0 = initial['_feasibility_bounds'], initial['_feasibility_jacobian']
    cache_x, cache, best = x0.copy(), (q0, j0 * scales, initial), initial
    best_key = (max(0., float(max(q0))), float(max(q0)), initial['score']['max_error'], initial['score']['weighted_rms_error'])
    accepted = None
    calls = iterations = 0
    outcome = None
    def data(x):
        nonlocal cache_x, cache, best, best_key, calls
        cancel_check()
        x = np.asarray(x, float)
        if cache_x is not None and np.array_equal(x, cache_x):
            return cache
        thicknesses = ds.copy() if np.array_equal(x, x0) else x * scales
        thicknesses = np.where((thicknesses < lower) & (x >= lower / scales), lower, thicknesses)
        thicknesses = np.where((thicknesses > upper) & (x <= upper / scales), upper, thicknesses)
        if (not np.all(np.isfinite(thicknesses)) or np.any(thicknesses < lower) or np.any(thicknesses > upper)):
            raise ValueError('feasibility solver requested invalid local thicknesses')
        trial = deepcopy(design)
        for row, d in zip(selected_rows(request, trial), thicknesses, strict=True):
            row['thickness'] = dict(value=float(d), unit='nm')
        if request.max_total_thickness_nm is not None and sum(thicknesses) > request.max_total_thickness_nm:
            # Line-search feasibility guard; never evaluated or archived.
            excess = sum(thicknesses) - request.max_total_thickness_nm
            count = len(initial['_feasibility_bounds'])
            return np.full(count, 1e6 + excess**2), np.tile(2 * excess * scales, (count, 1)), None
        value = evaluate(trial)
        calls += 1
        q, jac = value['_feasibility_bounds'], value['_feasibility_jacobian']
        if not len(q) or jac.shape != (len(q), len(ds)) or not np.all(np.isfinite(q)) or not np.all(np.isfinite(jac)):
            raise ValueError('feasibility repair has no complete finite analytic gradient')
        key = (max(0., float(max(q))), float(max(q)), value['score']['max_error'], value['score']['weighted_rms_error'])
        if best_key is None or key < best_key:
            best, best_key = value, key
        cache_x, cache = x.copy(), (q, jac * scales, value)
        archive(canonical_candidate(value), audit_eligible=False)
        progress('feasibility_repair_trial', value, dict(physical_candidates=calls, guide_max=float(max(q))))
        return cache
    termination = 'solver_failure'
    try:
        first = data(x0)
        start = np.r_[x0, max(0., float(max(first[0])))]
        def constraint(z):
            return z[-1] - data(z[:-1])[0]
        def derivative(z):
            q, jac, _ = data(z[:-1])
            return np.column_stack((-jac, np.ones(len(q))))
        def callback(z):
            nonlocal accepted, iterations
            accepted = data(z[:-1])[2]
            iterations += 1
            cancel_check()
        constraints: list[_Constraint] = [dict(type='ineq', fun=constraint, jac=derivative)]
        if request.max_total_thickness_nm is not None:
            constraints.append(dict(type='ineq', fun=lambda z: request.max_total_thickness_nm - float(z[:-1] @ scales),
                                    jac=lambda z: np.r_[-scales, 0.]))
        outcome = minimize(lambda z: float(z[-1]), start, jac=lambda z: np.r_[np.zeros(len(ds)), 1.],
            method='SLSQP', bounds=[*zip(lower / scales, upper / scales, strict=True), (0., None)],
            constraints=constraints, callback=callback, options=dict(maxiter=max_iterations, ftol=1e-11))
        data(np.asarray(outcome.x[:-1]))
        termination = 'converged' if outcome.success else 'solver_failure_or_iteration_limit'
    except SynthesisStageBudget:
        termination = 'stage_budget'
    except SynthesisResourceGuard:
        termination = 'resource_guard'
    result = best or accepted or initial
    return canonical_candidate(result), dict(termination=termination, physical_candidates=calls,
        iterations=iterations, optimizer_success=bool(outcome.success) if outcome is not None else False,
        guide_max=None if best_key is None else best_key[1], fixed_topology=True,
        margin_semantics='private range guide; original nominal targets unchanged', local_box_nm=5.)
