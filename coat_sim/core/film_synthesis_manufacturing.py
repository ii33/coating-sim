"""Explicit selected-design A/B repair, separate from automatic synthesis.

Reference thickness is a user-selected repair reference, not a hidden global
manufacturing minimum. All optical arithmetic stays in existing evaluators and
canonical original-constraint minimax. See the manufacturing repair contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass, replace
import math
from numbers import Real
import sys
from time import monotonic
from typing import Any, Callable, Literal, Mapping, cast

import numpy as np

from .film_auto_synthesis import FilmSynthesisRequest, _resolve_synthesis_resource_guard
from .film_design import MAX_LAYERS, validate_design
from .film_nominal_acceptance import complete_original_score
from .film_objective_criteria import FilmConditionError, FilmObjectiveCoverage, signed_bound_count
from .film_optimization_structure import _StructureCancelled
from .film_synthesis_archive import physical_design_hash
from .film_synthesis_batch_needle import batch_needle_parent_fingerprint, cleanup_batch_needle_design
from .film_synthesis_multispectral_minimax import _score_key, _validate_objectives
from .film_synthesis_refinement import SynthesisResourceGuard, SynthesisStageBudget, refine_synthesis_full_minimax

Value = dict[str, Any]
RouteName = Literal['raise_selected', 'delete_selected_merge']


@dataclass(frozen=True)
class ManufacturingRepairRequest:
    synthesis: FilmSynthesisRequest
    source_fingerprint: str
    defect_layer_ids: tuple[str, ...]
    reference_nm: float
    refinement_evaluations_per_route: int
    max_iterations_per_route: int
    seconds_per_route: float


@dataclass(frozen=True)
class ManufacturingSnapshot:
    """Owned complete evidence, without derivative arrays; no physical replay claim."""
    design: Mapping[str, Any]
    training_score: Mapping[str, Any]
    audit_score: Mapping[str, Any] | None = None

    @property
    def targets_met(self) -> bool:
        return (self.audit_score is not None and self.training_score.get('nominal_pass') is True
                and self.audit_score.get('nominal_pass') is True)

    @property
    def physical_hash(self) -> str:
        return physical_design_hash(self.design)


@dataclass(frozen=True)
class ManufacturingRouteResult:
    route: RouteName
    status: str
    design: Mapping[str, Any]
    lower_bounds_nm: Mapping[str, float]
    removed_ids: tuple[str, ...] = ()
    merged_ids: Mapping[str, str] | None = None
    start: ManufacturingSnapshot | None = None
    current: ManufacturingSnapshot | None = None
    champion: ManufacturingSnapshot | None = None
    start_calls: int = 0
    refinement_calls: int = 0
    audit_calls: int = 0
    completed_training_calls: int = 0
    completed_audit_calls: int = 0
    elapsed_seconds: float = 0.
    diagnostics: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ManufacturingRepairResult:
    status: str
    source: ManufacturingSnapshot
    routes: tuple[ManufacturingRouteResult, ...]
    best_repaired: ManufacturingSnapshot | None
    spectral_incumbent: ManufacturingSnapshot
    estimated_peak_bytes: int
    effective_limit_bytes: int | None
    elapsed_seconds: float
    source_validated: bool = True


class ManufacturingRepairStop(Exception):
    """Caller-owned global stop, never swallowed as a local refinement quota."""
    def __init__(self, reason: Literal['cancelled', 'shared_budget', 'time_limit', 'resource_guard']):
        if reason not in ('cancelled', 'shared_budget', 'time_limit', 'resource_guard'):
            raise ValueError('unknown manufacturing repair stop reason')
        self.reason = reason
        super().__init__(reason)


class _RouteDeadline(Exception):
    pass


class _ObserverFailure(Exception):
    def __init__(self, error: Exception):
        self.error = error
        super().__init__(str(error))


def _notify(callback: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    try:
        callback(*args, **kwargs)
    except Exception as error:
        # In particular, do not let the refiner catch an observer's similarly
        # named stage/resource exception and then continue into an audit.
        raise _ObserverFailure(error) from error


def manufacturing_source_fingerprint(request: FilmSynthesisRequest, design: Mapping[str, Any]) -> str:
    if request.frozen_material_snapshot is None:
        raise ValueError('manufacturing repair requires frozen material content')
    return batch_needle_parent_fingerprint(design, request.frozen_material_snapshot)


def _copy_score(score: Mapping[str, Any]) -> dict[str, Any]:
    """Existing public evidence projection, never a second score formula."""
    keys = ('max_error', 'weighted_max_error', 'weighted_rms_error', 'nominal_pass',
            'solver_acceptable', 'condition_count', 'key')
    result = {key: deepcopy(score[key]) for key in keys if key in score}
    for key, record in (('errors', FilmConditionError), ('coverage', FilmObjectiveCoverage),
                        ('audit_coverage', FilmObjectiveCoverage)):
        if key in score:
            names = tuple(f.name for f in fields(record))
            result[key] = tuple({name: deepcopy(row[name]) for name in names}
                                if isinstance(row, Mapping) else deepcopy(row) for row in score[key])
    if 'audit' in score:
        names = ('physical_evaluations', 'wavelength_points', 'dense_points', 'offset_points',
            'extra_points', 'unique_condition_points', 'bracket_count', 'completed_extrema',
            'local_coordinate', 'extrema_xatol', 'continuous_certificate', 'rms_measure',
            'mean_measure', 'objective_status', 'absorption_assumption')
        result['audit'] = {key: deepcopy(score['audit'][key]) for key in names if key in score['audit']}
    return result


def manufacturing_snapshot(request: FilmSynthesisRequest, value: Value, *,
                           audit_value: Value | None = None) -> ManufacturingSnapshot:
    """Validate original complete evidence and copy only design and scores."""
    design = value['design']
    if (type(value['score'].get('nominal_pass')) is not bool
            or not complete_original_score(request, value['score'])):
        raise ValueError('manufacturing snapshot requires complete original training evidence')
    if 'design' in value['score'] and value['score']['design'] != design:
        raise ValueError('training score belongs to another design')
    if 'responses' in value:
        def raw_identity(row: Any) -> tuple[Any, ...]:
            def field(name: str) -> Any:
                return row[name] if isinstance(row, Mapping) else getattr(row, name)
            return (field('objective_index'), field('angle_deg'),
                    tuple(field('wavelength_nm')), tuple(field('actual_values')))
        actual = tuple(raw_identity(row) for row in value['responses'])
        expected = tuple(raw_identity(row) for row in value['score']['coverage'])
        actual_rows = {row[:2]: row[2:] for row in actual}
        expected_rows = {row[:2]: row[2:] for row in expected}
        if (len(actual_rows) != len(actual) or len(expected_rows) != len(expected)
                or actual_rows != expected_rows):
            raise ValueError('training responses and canonical score coverage differ')
    audit = None
    if audit_value is not None:
        if (audit_value['design'] != design or type(audit_value['score'].get('nominal_pass')) is not bool
                or not complete_original_score(
                request, audit_value['score'], independent_audit=True)):
            raise ValueError('manufacturing snapshot requires matching complete original audit')
        if 'design' in audit_value['score'] and audit_value['score']['design'] != design:
            raise ValueError('audit score belongs to another design')
        audit = _copy_score(audit_value['score'])
    return ManufacturingSnapshot(deepcopy(design), _copy_score(value['score']), audit)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f'{name} must be positive finite')
    result = float(value)
    if not math.isfinite(result) or result <= 0.:
        raise ValueError(f'{name} must be positive finite')
    return result


def _physical_reason(request: FilmSynthesisRequest, design: Mapping[str, Any],
                     bounds: Mapping[str, float]) -> str | None:
    rows = design['front_layers']
    if len(rows) > (request.max_layers if request.max_layers is not None else MAX_LAYERS):
        return 'layer_limit'
    for row in rows:
        thickness = row['thickness']
        if thickness['unit'] != 'nm':
            return 'physical_nm_required'
        raw = thickness['value']
        if isinstance(raw, bool) or not isinstance(raw, Real) or not math.isfinite(raw) or raw <= 0.:
            return 'positive_physical_thickness_required'
        raw = float(raw)
        if raw < bounds[row['id']]:
            return 'lower_bound'
        if request.max_layer_thickness_nm is not None and raw > request.max_layer_thickness_nm:
            return 'upper_bound'
    if (request.max_total_thickness_nm is not None
            and math.fsum(row['thickness']['value'] for row in rows) > request.max_total_thickness_nm):
        return 'total_thickness_limit'
    return None


def _owned_bytes(*objects: Any) -> int:
    """Identity-deduplicated retained Python/array storage, without serializing."""
    seen: set[int] = set()
    def visit(obj: Any) -> int:
        if id(obj) in seen:
            return 0
        seen.add(id(obj))
        size = sys.getsizeof(obj)
        if isinstance(obj, np.ndarray):
            return size + (visit(obj.base) if obj.base is not None else 0)
        if isinstance(obj, Mapping):
            return size + sum(visit(k) + visit(v) for k, v in obj.items())
        if isinstance(obj, (tuple, list, set, frozenset)):
            return size + sum(visit(v) for v in obj)
        if is_dataclass(obj) and not isinstance(obj, type):
            if hasattr(obj, '__dict__'):
                return size + visit(vars(obj))
            return size + sum(visit(getattr(obj, f.name)) for f in fields(obj))
        return size
    return sum(visit(obj) for obj in objects)


def manufacturing_refinement_working_set_bytes(request: FilmSynthesisRequest, layers: int) -> int:
    """Conservative legacy full-evidence proxy, not a packed-path estimate.

    Six potentially distinct live response/J owners (initial, cache, strict
    best, accepted, terminal, incoming), their Python condition records,
    three incoming/observer compact evidence projections, canonical reduction
    and SLSQP copies are reserved. Retained compact evidence and external owners are additional.
    """
    p = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    q = signed_bound_count(request.objectives)
    n = max(1, layers)
    return (128 * 1024**2 + 384 * p * (n + 1) + 9 * 256 * p + 9 * 1024 * q
            + 256 * q * (n + 1) + 128 * n**2)


def prepare_manufacturing_routes(request: ManufacturingRepairRequest,
                                 source: ManufacturingSnapshot) -> tuple[ManufacturingRouteResult, ...]:
    """Geometry-only validation/construction; never dispatch optical work."""
    if not isinstance(request, ManufacturingRepairRequest) or not isinstance(source, ManufacturingSnapshot):
        raise ValueError('typed manufacturing request and source snapshot required')
    synthesis = request.synthesis
    if not isinstance(synthesis, FilmSynthesisRequest):
        raise ValueError('frozen FilmSynthesisRequest required')
    _validate_objectives(synthesis)
    reference = _positive(request.reference_nm, 'reference_nm')
    _positive(request.seconds_per_route, 'seconds_per_route')
    for name in ('refinement_evaluations_per_route', 'max_iterations_per_route'):
        if type(getattr(request, name)) is not int or getattr(request, name) < 1:
            raise ValueError(f'{name} must be a positive integer')
    for name in ('minimum_nonzero_thickness_nm', 'max_layer_thickness_nm', 'max_total_thickness_nm'):
        if getattr(synthesis, name) is not None:
            _positive(getattr(synthesis, name), name)
    if synthesis.max_layers is not None and (type(synthesis.max_layers) is not int or synthesis.max_layers < 1):
        raise ValueError('max_layers must be positive or None')
    design = validate_design(deepcopy(dict(source.design)))
    if design != source.design:
        raise ValueError('source must use the normalized current design contract')
    if (design['back_layers'] or design['substrate']['sides'] != 'single'
            or design['first_surface'] != 'front' or not design['use_k']):
        raise ValueError('manufacturing repair supports single front, use_k=True only')
    environment = deepcopy(design)
    environment['front_layers'] = []
    expected = deepcopy(dict(synthesis.environment))
    # Editor labels do not define optics; physical hash does include all ports.
    if physical_design_hash(environment) != physical_design_hash(expected):
        raise ValueError('source environment differs from the frozen synthesis request')
    if synthesis.frozen_material_snapshot is None:
        raise ValueError('manufacturing repair requires frozen material content')
    from .film_design_service import active_material_references
    material_rows = {m['id']: m for m in synthesis.frozen_material_snapshot.get('materials', ())}
    for row in active_material_references(design):
        material = material_rows.get(row['material_id'])
        if material is None or not any(o['id'] == row['optical_set_id'] for o in material.get('optical_sets', ())):
            raise ValueError('source material/optical-set reference is not in frozen content')
    ids = tuple(row['id'] for row in design['front_layers'])
    if (len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids)
            or type(request.defect_layer_ids) is not tuple or not request.defect_layer_ids
            or any(not isinstance(i, str) for i in request.defect_layer_ids)
            or len(set(request.defect_layer_ids)) != len(request.defect_layer_ids)
            or not set(request.defect_layer_ids) <= set(ids)):
        raise ValueError('defect layer IDs must be explicit unique current IDs')
    native = synthesis.minimum_nonzero_thickness_nm or np.finfo(float).tiny
    reason = _physical_reason(synthesis, design, dict.fromkeys(ids, native))
    if reason is not None:
        raise ValueError(f'invalid manufacturing source: {reason}')
    if manufacturing_source_fingerprint(synthesis, design) != request.source_fingerprint:
        raise ValueError('manufacturing source fingerprint is stale')
    selected = set(request.defect_layer_ids)
    if any(row['thickness']['value'] >= reference for row in design['front_layers'] if row['id'] in selected):
        raise ValueError('selected defect rows must be thinner than reference_nm')
    a, b = deepcopy(design), deepcopy(design)
    for row in a['front_layers']:
        if row['id'] in selected:
            row['thickness'] = dict(value=reference, unit='nm')
    b['front_layers'] = [row for row in b['front_layers'] if row['id'] not in selected]
    for candidate in (a, b):
        if candidate['expression']['text']:
            candidate['expression']['in_sync'] = False
    assert synthesis.frozen_material_snapshot is not None
    cleaned = cleanup_batch_needle_design(b, synthesis.frozen_material_snapshot)
    result = []
    for name, candidate in (('raise_selected', a), ('delete_selected_merge', cleaned.design)):
        minima = {row['id']: max(native, min(row['thickness']['value'], reference))
                  for row in candidate['front_layers']}
        reason = _physical_reason(synthesis, candidate, minima)
        result.append(ManufacturingRouteResult(
            route=cast(RouteName, name), status='prepared' if reason is None else f'infeasible_{reason}',
            design=deepcopy(candidate), lower_bounds_nm=minima,
            removed_ids=() if name == 'raise_selected' else tuple(i for i in ids if i in selected),
            merged_ids={} if name == 'raise_selected' else dict(cleaned.merged_ids)))
    return tuple(result)


def _audited_key(snapshot: ManufacturingSnapshot) -> tuple[int, int, float, float]:
    assert snapshot.audit_score is not None
    # Same two-evidence nominal gate as audit_archive; no layer-cost incentive.
    return (int(not snapshot.targets_met), *_score_key({'score': snapshot.audit_score}))


def repair_manufacturing_pair(request: ManufacturingRepairRequest, source: ManufacturingSnapshot, *,
        evaluate: Callable[..., Value], audit: Callable[..., Value],
        audit_working_set_bytes: Callable[[Mapping[str, Any]], int],
        admit: Callable[..., None], archive: Callable[..., None] = lambda *a, **k: None,
        progress: Callable[..., None] = lambda *a: None,
        cancel_check: Callable[[], None] = lambda: None,
        external_retained_bytes: Callable[[], int] = lambda: 0,
        clock: Callable[[], float] = monotonic) -> ManufacturingRepairResult:
    """Bounded opt-in comparison; caller callbacks own actual optical blocks.

    evaluate/audit receive ``cancel_check`` and must use it between blocks.
    admit receives phase, route, design, jacobian and estimated_bytes before
    dispatch. It must reserve complete candidate/audit work in shared budgets.
    Global stops use ManufacturingRepairStop, not SynthesisStageBudget.
    Arbitrary evaluator/observer errors propagate; they are not acceptance.
    """
    started = clock()
    if not isinstance(request, ManufacturingRepairRequest) or not isinstance(source, ManufacturingSnapshot):
        raise ValueError('typed manufacturing request and source snapshot required')
    synthesis = request.synthesis
    if not isinstance(synthesis, FilmSynthesisRequest):
        raise ValueError('frozen FilmSynthesisRequest required')
    _validate_objectives(synthesis)
    _positive(request.seconds_per_route, 'seconds_per_route')
    if (type(synthesis.validation_samples_multiplier) is not int
            or synthesis.validation_samples_multiplier < 1):
        raise ValueError('validation_samples_multiplier must be a positive integer')
    if synthesis.max_working_set_bytes is not None and (type(synthesis.max_working_set_bytes) is not int
            or synthesis.max_working_set_bytes < 0):
        raise ValueError('max_working_set_bytes must be 0, positive, or None')
    resolution = _resolve_synthesis_resource_guard(synthesis)
    limit = resolution['effective_bytes']
    try:
        cancel_check()
    except (_StructureCancelled, ManufacturingRepairStop, SynthesisStageBudget, SynthesisResourceGuard) as stop:
        reason = (stop.reason if isinstance(stop, ManufacturingRepairStop) else
                  'shared_budget' if isinstance(stop, SynthesisStageBudget) else
                  'resource_guard' if isinstance(stop, SynthesisResourceGuard) else 'cancelled')
        return ManufacturingRepairResult(reason, source, (), None, source, 0, limit,
                                         clock() - started, source_validated=False)
    # Admission precedes request/evidence deepcopy and canonical reducer
    # temporaries. An early stop returns the untouched supplied reference and
    # explicitly does not claim that this invocation validated that evidence.
    points = sum(o.wavelength_samples * len(o.angles_deg) for o in synthesis.objectives)
    multiplier = synthesis.validation_samples_multiplier if source.audit_score is not None else 1
    validation_bytes = (128 * 1024**2 + 512 * points * multiplier
                        + 2048 * signed_bound_count(synthesis.objectives) * multiplier
                        + 2 * _owned_bytes(request, source))
    external = external_retained_bytes()
    if type(external) is not int or external < 0:
        raise ValueError('external_retained_bytes must return a nonnegative integer')
    peak = validation_bytes + external
    if limit is not None and peak > limit:
        return ManufacturingRepairResult('resource_guard', source, (), None, source, peak, limit,
                                         clock() - started, source_validated=False)
    original_request, original_source = request, source
    request = deepcopy(request)
    source = manufacturing_snapshot(request.synthesis, {'design': source.design, 'score': source.training_score},
        audit_value=None if source.audit_score is None else {'design': source.design, 'score': source.audit_score})
    routes = list(prepare_manufacturing_routes(request, source))
    synthesis = request.synthesis
    status = 'complete'

    def check() -> None:
        try:
            cancel_check()
        except SynthesisStageBudget as error:
            raise ManufacturingRepairStop('shared_budget') from error
        except SynthesisResourceGuard as error:
            raise ManufacturingRepairStop('resource_guard') from error
        if clock() >= started + 2 * request.seconds_per_route:
            raise ManufacturingRepairStop('time_limit')

    def reserve(route: ManufacturingRouteResult, phase: str, design: Mapping[str, Any]) -> None:
        nonlocal peak
        check()
        external = external_retained_bytes()
        if type(external) is not int or external < 0:
            raise ValueError('external_retained_bytes must return a nonnegative integer')
        numerical = (audit_working_set_bytes(deepcopy(dict(design))) if phase == 'audit' else
                     manufacturing_refinement_working_set_bytes(synthesis, len(design['front_layers'])))
        if type(numerical) is not int or numerical < 1:
            raise ValueError('working set estimate must be a positive integer')
        estimate = numerical + external + _owned_bytes(original_request, original_source, request, source, routes)
        peak = max(peak, estimate)
        if limit is not None and estimate > limit:
            raise ManufacturingRepairStop('resource_guard')
        try:
            admit(phase=phase, route=route.route, design=deepcopy(dict(design)),
                  jacobian=phase != 'audit', estimated_bytes=estimate)
        except SynthesisStageBudget as error:
            raise ManufacturingRepairStop('shared_budget') from error
        except SynthesisResourceGuard as error:
            raise ManufacturingRepairStop('resource_guard') from error
        check()

    try:
        check()
        for index, prepared in enumerate(routes):
            check()
            if prepared.status != 'prepared':
                continue
            route_started = clock()
            routes[index] = replace(prepared, status='running')

            def route_check(route_started: float = route_started) -> None:
                check()
                if clock() >= route_started + request.seconds_per_route:
                    raise _RouteDeadline()

            def measured(design: Mapping[str, Any], *, jacobian: bool, index: int = index,
                         route_check: Callable[[], None] = route_check) -> Value:
                route_check()
                state = routes[index]
                phase = 'start' if state.start is None else 'refinement'
                if phase == 'refinement' and state.refinement_calls >= request.refinement_evaluations_per_route:
                    raise SynthesisStageBudget()
                reason = _physical_reason(synthesis, design, state.lower_bounds_nm)
                if reason is not None:
                    raise ValueError(f'repair evaluator received invalid physical design: {reason}')
                reserve(state, phase, design)
                route_check()
                counter = 'start_calls' if phase == 'start' else 'refinement_calls'
                routes[index] = replace(state, **{counter: getattr(state, counter) + 1})
                expected_design = deepcopy(dict(design))
                try:
                    value = evaluate(deepcopy(expected_design), jacobian=jacobian, cancel_check=route_check)
                except SynthesisStageBudget as error:
                    raise ManufacturingRepairStop('shared_budget') from error
                except SynthesisResourceGuard as error:
                    raise ManufacturingRepairStop('resource_guard') from error
                if value['design'] != expected_design:
                    raise ValueError('repair evaluator returned another design')
                snapshot = manufacturing_snapshot(synthesis, value)
                state = routes[index]
                champion = state.champion
                if champion is None or _score_key({'score': snapshot.training_score}) < _score_key({'score': champion.training_score}):
                    champion = snapshot
                routes[index] = replace(state, start=state.start or snapshot, current=snapshot, champion=champion,
                    completed_training_calls=state.completed_training_calls + 1)
                raw = tuple({name: deepcopy(row[name] if isinstance(row, Mapping) else getattr(row, name))
                    for name in ('objective_index', 'angle_deg', 'wavelength_nm', 'actual_values')}
                    for row in snapshot.training_score['coverage'])
                archived = dict(design=deepcopy(snapshot.design), score=deepcopy(snapshot.training_score), responses=raw)
                _notify(archive, archived, audit_eligible=False)
                del archived, raw
                _notify(progress, 'manufacturing_repair_trial', deepcopy(snapshot), dict(route=state.route, phase=phase))
                route_check()
                return value

            initial = _best = None
            try:
                initial = measured(prepared.design, jacobian=True)
                # measured commits every trial before cancellation can unwind;
                # minimax's archive callback is intentionally not a second commit.
                _best, diagnostics = refine_synthesis_full_minimax(synthesis, initial, evaluate=measured,
                    archive=lambda *a, **k: None, cancel_check=route_check,
                    progress=lambda *a: None, max_iterations=request.max_iterations_per_route,
                    lower_bounds_nm=prepared.lower_bounds_nm)
                # Release derivative owners before the independently admitted audit.
                initial = _best = None
                routes[index] = replace(routes[index], diagnostics=diagnostics)
                route_check()
                champion = routes[index].champion
                assert champion is not None
                reserve(routes[index], 'audit', champion.design)
                route_check()
                routes[index] = replace(routes[index], audit_calls=1)
                audited = audit(deepcopy(dict(champion.design)), cancel_check=route_check)
                complete = manufacturing_snapshot(synthesis,
                    {'design': champion.design, 'score': champion.training_score}, audit_value=audited)
                del audited
                routes[index] = replace(routes[index], champion=complete, status='audited', completed_audit_calls=1)
                _notify(progress, 'manufacturing_repair_audit', deepcopy(complete), dict(route=prepared.route))
                route_check()
            except _RouteDeadline:
                routes[index] = replace(routes[index], status='route_time_limit')
            finally:
                initial = _best = None
                routes[index] = replace(routes[index], elapsed_seconds=clock() - route_started)
    except _ObserverFailure as failure:
        raise failure.error from failure
    except _StructureCancelled:
        status = 'cancelled'
    except ManufacturingRepairStop as stop:
        status = stop.reason
    except SynthesisStageBudget:
        status = 'shared_budget'
    except SynthesisResourceGuard:
        status = 'resource_guard'
    if status != 'complete':
        routes = [replace(r, status=status) if r.status == 'running' else r for r in routes]
    eligible = [r.champion for r in routes if r.champion is not None and r.champion.audit_score is not None]
    best_repaired = min(eligible, key=_audited_key, default=None)
    incumbent = source
    if (best_repaired is not None and source.audit_score is not None
            and _audited_key(best_repaired) < _audited_key(source)):
        incumbent = best_repaired
    return ManufacturingRepairResult(status, source, tuple(routes), best_repaired, incumbent,
                                     peak, limit, clock() - started)
