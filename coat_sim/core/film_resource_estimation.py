"""Read-only film resource assessment on the ordinary frozen full-design TMM.

MVP-A: necessary numerical screens and audited observed structures only. No
trained prediction, optimization submission, editor mutation or implicit caps.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import time
from typing import Any, Callable, Mapping

import numpy as np

from .film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from .film_design import physical_thickness_nm, validate_design
from .film_design_service import automatic_substrate_handling
from .film_nominal_acceptance import nominal_targets_pass, solver_acceptable
from .film_optimization_needle import FilmNeedleMaterialReference
from .film_optimization_structure import _StructureCancelled
from .film_optics import _coherent_many, _polarizations
from .film_resource_estimation_bounds import hr_point_layer_screen, hr_thickness_screen
from .film_synthesis_absorption import sample_synthesis_optical, synthesis_absorption_assumption
from .film_synthesis_archive import physical_design_hash
from .film_synthesis_audit import audit_frozen_synthesis_candidate
from .film_synthesis_surfaces import selected_rows, selected_sides, validate_candidate_environment
from .optical_constants import prepare_optical_set


@dataclass(frozen=True)
class FilmResourceEstimateRequest:
    synthesis_request: FilmSynthesisRequest
    candidate_designs: tuple[Mapping[str, Any], ...] = ()
    audit_candidates: bool = True
    layer_search_limit: int = 128
    max_seconds: float | None = None
    search_budget_reference: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FilmResourceEstimateProgress:
    stage: str
    message: str
    completed_units: int
    physical_evaluations: int
    wavelength_points: int


@dataclass(frozen=True)
class FilmResourceEstimateResult:
    state: str
    input_status: str
    message: str
    input_digest: str = ''
    source_digest: str = ''
    selected_surfaces: tuple[str, ...] = ()
    search_budget_reference: dict[str, Any] = field(default_factory=dict)
    bounds: tuple[dict[str, Any], ...] = ()
    screenings: tuple[dict[str, Any], ...] = ()
    candidate_reports: tuple[dict[str, Any], ...] = ()
    pareto_witnesses: tuple[dict[str, Any], ...] = ()
    recommended_budget: list[dict[str, Any]] | None = None
    work: dict[str, Any] = field(default_factory=dict)
    estimate_state: str = 'not_calibrated'
    feasibility_state: str = 'unknown'
    continuous_certificate: bool = False


class _Control:
    def __init__(self, event, seconds):
        self.event = event
        self.started = time.monotonic()
        self.seconds = seconds

    def is_set(self):
        return bool((self.event is not None and self.event.is_set()) or
                    (self.seconds is not None and time.monotonic() - self.started >= self.seconds))

    def check(self):
        if self.is_set():
            raise _StructureCancelled()

    def state(self):
        return 'cancelled' if self.event is not None and self.event.is_set() else 'deadline'


class _Sources:
    def __init__(self, request, work, control):
        self.request, self.work, self.control = request, work, control
        self.optics = {(r['id'], o['id']): o for r in request.frozen_material_snapshot['materials']
                       for o in r['optical_sets']}
        self.samplers = {key: prepare_optical_set(o, k_sampling_policy='source') for key, o in self.optics.items()}

    def sample(self, ref, wavelength):
        self.control.check()
        if ref.get('kind') == 'constant':
            return float(ref['n']), 0.
        key = ref['material_id'], ref['optical_set_id']
        self.work['source_samples'] += 1
        n, k, _ = sample_synthesis_optical(self.samplers[key], wavelength,
            assume_unknown_k_zero=self.request.environment['assume_lossless'])
        return n, k

    def constant_n(self, ref):
        if ref.get('kind') == 'constant':
            return float(ref['n'])
        optical = self.optics[ref['material_id'], ref['optical_set_id']]
        values = optical.get('n', ())
        if optical.get('kind') != 'tabulated' or not values or any(v != values[0] for v in values):
            raise ValueError('material_source_is_not_constant_tabulated_n')
        # This diagnoses a constant-index idealization, never global measured causality.
        for wave in optical['wavelengths']:
            factor = 1000 if optical['wavelength_unit'] == 'um' else 1
            _, k = self.sample(ref, wave * factor)
            if k != 0:
                raise ValueError('absorbing_source')
        return float(values[0])

    def thickness(self, design, row):
        nr = self.sample(row, design['reference_wavelength_nm'])[0] if row['thickness']['unit'] == 'qw' else None
        return physical_thickness_nm(row, design['reference_wavelength_nm'], nr)


def _interval(c):
    if c.mode == 'value':
        return c.target_value, c.target_value, False, False
    return c.lower_bound, c.upper_bound, c.strict_lower, c.strict_upper


def _lower(objective):
    if objective.metric != 'R' or not objective.pointwise.enabled:
        return None
    value = _interval(objective.pointwise)[0]
    return value if value is not None and value > 0 else None


def _bound(kind, reason='', **kw):
    result = dict(kind=kind, applicability='not_applicable', reason=reason, value_nm=None,
                  value_layers=None, excluded_through=None, search_limit=None,
                  claim_level='none', conditional_on_assumption=False, details={})
    result.update(kw)
    return result


def _screen(kind, indices, conflict, reason, details=None):
    return dict(kind=kind, objective_indices=list(indices), state='conflict' if conflict else 'not_excluded',
                reason=reason, message=reason, claim_level='model_numerical_screening', details=details or {})


def _target_conflicts(request):
    rows = request.objectives
    screens = []
    for i, a in enumerate(rows):
        for j, b in enumerate(rows[i + 1:], i + 1):
            if a.polarization != b.polarization or not set(a.angles_deg).intersection(b.angles_deg):
                continue
            for reduction in ('pointwise', 'mean'):
                ca, cb = getattr(a, reduction), getattr(b, reduction)
                if not ca.enabled or not cb.enabled:
                    continue
                if reduction == 'mean':
                    if (a.wavelength_min_nm, a.wavelength_max_nm, a.wavelength_samples) != (b.wavelength_min_nm, b.wavelength_max_nm, b.wavelength_samples):
                        continue
                elif max(a.wavelength_min_nm, b.wavelength_min_nm) > min(a.wavelength_max_nm, b.wavelength_max_nm):
                    continue
                la, ua, sla, sua = _interval(ca)
                lb, ub, slb, sub = _interval(cb)
                if a.metric == b.metric:
                    lo = max(v for v in (la, lb, 0) if v is not None)
                    hi = min(v for v in (ua, ub, 1) if v is not None)
                    strict = (la == lo and sla) or (lb == lo and slb) or (ua == hi and sua) or (ub == hi and sub)
                    conflict = lo > hi or (lo == hi and strict)
                else:
                    conflict = la is not None and lb is not None and (la + lb > 1 or (la + lb == 1 and (sla or slb)))
                if conflict:
                    screens.append(_screen('target_consistency', (i, j), True,
                                           f'conflicting_{reduction}_bounds_same_condition'))
    return screens


def _allowed_refs(request):
    """Explicit seed materials may remain during product synthesis as well."""
    pairs = {(r.material_id, r.optical_set_id) for r in request.candidate_materials}
    pairs.update((r['material_id'], r['optical_set_id'])
                 for seed in request.seed_designs for r in selected_rows(request, seed))
    return [dict(material_id=m, optical_set_id=o) for m, o in sorted(pairs)]


def _necessary_bounds(request, sources, limit, work, emit, control):
    refs = _allowed_refs(request)
    env = request.environment
    hr = [(i, o, _lower(o)) for i, o in enumerate(request.objectives) if _lower(o) is not None]
    reason = 'requires_pointwise_hr' if not hr else 'mvp_requires_single_front_half_space'
    if not hr or env['substrate']['sides'] != 'single' or request.selected_surfaces != ('front',):
        return [_bound('hr_thickness_lp', reason), _bound('hr_layer_dp', reason, search_limit=limit)]
    conditional = synthesis_absorption_assumption(request)['conditional_on_assumption']
    thickness = _bound('hr_thickness_lp', 'requires_pointwise_normal_hr_constant_lossless_n')
    windows = [(o.wavelength_min_nm, o.wavelength_max_nm, target) for _, o, target in hr
               if 0. in o.angles_deg and o.wavelength_max_nm > o.wavelength_min_nm]
    if windows:
        try:
            constants = [sources.constant_n(r) for r in refs]
            n0, ns = sources.constant_n(env['incident_medium']), sources.constant_n(env['substrate'])
            control.check()
            def record_solve():
                work['lp_solves'] += 1

            details = hr_thickness_screen(constants, windows, n0, ns, check=control.check, on_solve=record_solve)
            thickness.update(applicability='applicable', reason=details['reason'], value_nm=details['value_nm'],
                claim_level='model_numerical_screening', conditional_on_assumption=conditional,
                details=dict(details, manufacturing_constraints_relaxed=True,
                             assumptions=['constant_index_idealization', 'lossless', 'normal_incidence', 'pointwise_hr']))
        except (ValueError, ArithmeticError) as exc:
            thickness['reason'] = str(exc)
    emit('point_bounds', '点频必要界；枚举窗口不是优化层数上限')
    best, excluded, skipped, conditions = 0, -1, [], 0
    exceeds = False
    for index, objective, target in hr:
        for wave in np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples):
            control.check()
            optical = [sources.sample(ref, float(wave)) for ref in [env['incident_medium'], *refs, env['substrate']]]
            for angle in objective.angles_deg:
                control.check()
                if any(k != 0 for _, k in optical):
                    skipped.append(dict(objective_index=index, wavelength_nm=float(wave), angle_deg=angle, reason='absorbing_candidate_or_port'))
                    continue
                try:
                    work['dp_conditions_started'] += 1

                    def record_transitions(count):
                        work['dp_transitions'] += count

                    d = hr_point_layer_screen([n for n, _ in optical[1:-1]], optical[0][0], optical[-1][0],
                        angle, objective.polarization, target, limit, check=control.check, record_transitions=record_transitions)
                except ValueError as exc:
                    skipped.append(dict(objective_index=index, wavelength_nm=float(wave), angle_deg=angle, reason=str(exc)))
                    continue
                conditions += 1
                work['dp_conditions'] += 1
                excluded = max(excluded, d['excluded_through'])
                if d['value_layers'] is None:
                    exceeds = True
                else:
                    best = max(best, d['value_layers'])
    layer = _bound('hr_layer_dp', 'no_applicable_lossless_propagating_condition', search_limit=limit)
    if conditions:
        layer.update(applicability='partial' if skipped else 'applicable', reason='conditional_pointwise_hr_necessary_screen',
            value_layers=None if exceeds else best, excluded_through=excluded, claim_level='model_numerical_screening',
            conditional_on_assumption=conditional, details=dict(conditions=conditions, skipped_conditions=skipped,
                exceeds_search_window=exceeds, sufficient_budget=False, mean_only_hr_eligible=False, manufacturing_constraints_relaxed=True))
    else:
        layer['details'] = dict(skipped_conditions=skipped, mean_only_hr_eligible=False)
    return [thickness, layer]


def _fixed_face_screens(request, sources, work, emit, control):
    env, sides = request.environment, selected_sides(request)
    output: list[dict[str, Any]] = []
    if env['substrate']['sides'] != 'double' or len(sides) != 1:
        return output
    fixed_side = 'back' if sides[0] == 'front' else 'front'
    for i, o in enumerate(request.objectives):
        if o.metric != 'T' or not o.pointwise.enabled or _interval(o.pointwise)[0] is None:
            continue
        if automatic_substrate_handling(env, o.wavelength_max_nm)['resolved_mode'] != 'incoherent':
            output.append(dict(kind='fixed_face_transmission', objective_indices=[i], state='not_applicable',
                reason='requires_incoherent_substrate', message='固定面透射上限不适用于相干基片', claim_level='none', details={}))
            continue
        rows = env[fixed_side + '_layers']
        first = env['first_surface']
        port = env['incident_medium'] if fixed_side == 'front' else env['exit_medium']
        entry = env['incident_medium'] if first == 'front' else env['exit_medium']
        waves = np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples)
        target = _interval(o.pointwise)[0]
        failed, applicable = [], 0
        for wave in waves:
            control.check()
            ns, ks = sources.sample(env['substrate'], float(wave))
            external = [sources.sample(env[key], float(wave)) for key in ('incident_medium', 'exit_medium')]
            ni, ki = sources.sample(entry, float(wave))
            ne, ke = sources.sample(port, float(wave))
            if ks != 0 or ki != 0 or ke != 0 or any(k != 0 for _, k in external):
                continue
            ordered = list(reversed(rows)) if fixed_side == first else rows
            mids = [complex(*sources.sample(r, float(wave))) for r in ordered]
            ds = np.asarray([sources.thickness(env, r) for r in ordered])
            for angle in o.angles_deg:
                control.check()
                beta = ni * math.sin(math.radians(angle))
                left, right = (ne, ns) if fixed_side == first else (ns, ne)
                if beta >= min(left, right):
                    continue
                n = np.asarray([[left, *mids, right]], complex)
                powers = []
                for pol in _polarizations(o.polarization):
                    control.check()
                    raw = _coherent_many(np.asarray([float(wave)]), n, ds, np.asarray([beta]), pol)
                    work['physical_evaluations'] += 1
                    work['fixed_face_physical_evaluations'] += 1
                    work['wavelength_points'] += 1
                    t, r = float(raw[0, 1]), float(raw[0, 0])
                    if not math.isfinite(t) or not 0 <= t <= 1 + 1e-10 or r + t > 1 + 1e-9:
                        raise ValueError('fixed face power normalization/passivity failure')
                    powers.append(t)
                applicable += 1
                ceiling = math.fsum(powers) / len(powers)
                if ceiling < target:
                    failed.append(dict(wavelength_nm=float(wave), angle_deg=angle, transmission_upper=ceiling, target=target))
                emit('fixed_face', '固定面对面透射数值前检')
        output.append(_screen('fixed_face_transmission', (i,), bool(failed),
            'conditional_incoherent_fixed_face_ceiling' if applicable else 'no_lossless_propagating_substrate_conditions',
            dict(fixed_surface=fixed_side, checked_conditions=applicable, conflicts=failed,
                 conditional_on_assumption=synthesis_absorption_assumption(request)['conditional_on_assumption'],
                 continuous_certificate=False)))
        if not applicable:
            output[-1]['state'] = 'not_applicable'
    return output


def _resources(request, design, sources, search_reference):
    surfaces = {}
    for side in ('front', 'back'):
        rows = design[side + '_layers']
        if side == 'back' and design['substrate']['sides'] == 'single':
            surfaces[side] = dict(layer_count=len(rows), total_thickness_nm=None if rows else 0., active=False)
            continue
        values = [sources.thickness(design, row) for row in rows]
        surfaces[side] = dict(layer_count=len(values), total_thickness_nm=math.fsum(values), active=True)
    values = [sources.thickness(design, row) for row in selected_rows(request, design)]
    count, total = len(values), math.fsum(values)
    allowed = {(r['material_id'], r['optical_set_id']) for r in _allowed_refs(request)}
    within_pool = all((r['material_id'], r['optical_set_id']) in allowed for r in selected_rows(request, design))
    valid = (within_pool and all(d > 0 and d >= (request.minimum_nonzero_thickness_nm or 0) and
                 (request.max_layer_thickness_nm is None or d <= request.max_layer_thickness_nm) for d in values)
)
    within_search = ((search_reference.get('max_layers') is None or count <= search_reference['max_layers'])
                     and (search_reference.get('max_total_thickness_nm') is None or total <= search_reference['max_total_thickness_nm']))
    return dict(layer_count=count, total_thickness_nm=total, surfaces=surfaces, within_constraints=valid,
                within_material_pool=within_pool, within_search_budgets=within_search)


def observed_resource_pareto(reports):
    """Only complete passing witnesses; never combine independent minima."""
    eligible = [deepcopy(r) for r in reports if r['state'] == 'audited' and r['targets_met'] and r['within_constraints']]
    ordered = sorted(eligible, key=lambda r: (r['layer_count'], r['total_thickness_nm'], r['physical_hash']))
    result, best = [], math.inf
    for row in ordered:
        if row['total_thickness_nm'] < best:
            result.append(row)
            best = row['total_thickness_nm']
    return result


def freeze_resource_estimate_request(service: Any, request: FilmResourceEstimateRequest, *,
                                     cancel_event: Any = None) -> FilmResourceEstimateRequest:
    """Capture all audit sources without changing the allowed material pool.

    An existing material snapshot is validated and reused, never read live.
    Call again on fresh UI input to compare resource_estimate_source_digest.
    """
    if not isinstance(request, FilmResourceEstimateRequest) or not isinstance(request.synthesis_request, FilmSynthesisRequest):
        raise ValueError('resource assessment requires a synthesis request')
    request = deepcopy(request)
    base = request.synthesis_request
    reference = dict(request.search_budget_reference) if request.search_budget_reference is not None else {
        'max_layers': base.max_layers, 'max_total_thickness_nm': base.max_total_thickness_nm}
    if set(reference) - {'max_layers', 'max_total_thickness_nm'}:
        raise ValueError('invalid search budget reference keys')
    ncap, dcap = reference.get('max_layers'), reference.get('max_total_thickness_nm')
    if ncap is not None and (type(ncap) is not int or ncap < 0):
        raise ValueError('search max_layers reference must be nonnegative integer or None')
    if dcap is not None and (isinstance(dcap, bool) or not isinstance(dcap, (int, float)) or not math.isfinite(dcap) or dcap <= 0):
        raise ValueError('search total thickness reference must be positive finite or None')
    # The assessment is explicitly unconstrained by optimization search hats.
    # Manufacturing limits remain unchanged; original search hats are comparison-only.
    base = replace(base, max_layers=None, max_total_thickness_nm=None)
    if not base.candidate_materials:
        raise ValueError('explicit candidate material pool is required')
    refs = list(base.candidate_materials)
    pairs = {(r.material_id, r.optical_set_id) for r in refs}
    candidates = []
    for raw in request.candidate_designs:
        design = validate_design(deepcopy(dict(raw)))
        validate_candidate_environment(base, design)
        candidates.append(design)
        for row in selected_rows(base, design):
            pair = row['material_id'], row['optical_set_id']
            if pair not in pairs:
                refs.append(FilmNeedleMaterialReference(*pair))
                pairs.add(pair)
    if base.frozen_material_snapshot is not None:
        present = {(m['id'], o['id']) for m in base.frozen_material_snapshot['materials'] for o in m['optical_sets']}
        required = pairs | {(r['material_id'], r['optical_set_id']) for seed in base.seed_designs for r in selected_rows(base, seed)}
        if required - present:
            raise ValueError('frozen assessment snapshot missing audit source; explicitly refreeze from fresh input')
    frozen = freeze_film_synthesis_request(service, replace(base, candidate_materials=tuple(refs),
        search_checkpoint=None), cancel_event=cancel_event)
    frozen = replace(frozen, candidate_materials=tuple(base.candidate_materials))
    return replace(request, synthesis_request=frozen, candidate_designs=tuple(candidates), search_budget_reference=reference)


def resource_estimate_source_digest(request: FilmResourceEstimateRequest) -> str:
    """One UI/worker source identity, independent of assessment compute window."""
    base = request.synthesis_request
    if base.frozen_material_snapshot is None:
        raise ValueError('resource source digest requires frozen material sources')
    data = dict(environment=base.environment, objectives=[asdict(o) for o in base.objectives],
        selected_surfaces=selected_sides(base), candidate_materials=[asdict(r) for r in base.candidate_materials],
        materials=base.frozen_material_snapshot['materials'],
        constraints={k: getattr(base, k) for k in ('minimum_nonzero_thickness_nm', 'max_layer_thickness_nm',
            'max_total_thickness_nm', 'max_layers')},
        candidates=[physical_design_hash(d) for d in request.candidate_designs],
        explicit_seeds=[physical_design_hash(d) for d in base.seed_designs],
        search_budget_reference=request.search_budget_reference)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def estimate_film_resources(service: Any, request: FilmResourceEstimateRequest, *, cancel_event: Any = None,
                            progress_callback: Callable[[FilmResourceEstimateProgress], None] | None = None) -> FilmResourceEstimateResult:
    """One read-only worker call; returns partial evidence after cancellation.

    A callback runs synchronously on this worker. It receives counts only. Input
    errors become failed/invalid results, never plausible optical budgets.
    """
    started = time.monotonic()
    work: dict[str, Any] = dict(source_freeze_attempts=0, source_freeze_passes=0, source_samples=0, dp_conditions=0, dp_conditions_started=0, dp_transitions=0, lp_solves=0,
                fixed_face_physical_evaluations=0, audits_started=0, audits_completed=0,
                physical_evaluations=0, wavelength_points=0, elapsed_seconds=0.)
    bounds: list[dict[str, Any]] = []
    screens: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    digest, source_digest, sides, input_status = '', '', (), 'invalid'
    reference: dict[str, Any] = {}
    control = _Control(cancel_event, None)

    def emit(stage, message):
        if progress_callback is not None:
            progress_callback(FilmResourceEstimateProgress(stage, message,
                work['dp_conditions'] + work['audits_completed'], work['physical_evaluations'], work['wavelength_points']))
        control.check()

    def finish(state, message):
        work['elapsed_seconds'] = time.monotonic() - started
        pareto = observed_resource_pareto(reports)
        recommendation = [dict(
            {k: deepcopy(row[k]) for k in ('candidate_index', 'layer_count', 'total_thickness_nm', 'surfaces',
                'physical_hash', 'conditional_on_assumption', 'absorption_assumption', 'within_search_budgets')},
            source_digest=source_digest, claim_level='sampled_witness', continuous_certificate=False,
            applicability='audited_original_nominal_and_manufacturing',
            scope='selected_coating_surfaces_aggregate') for row in pareto] or None
        return FilmResourceEstimateResult(state=state, input_status=input_status, message=message, input_digest=digest,
            source_digest=source_digest, selected_surfaces=sides, search_budget_reference=reference, bounds=tuple(bounds), screenings=tuple(screens),
            candidate_reports=tuple(reports), pareto_witnesses=tuple(pareto), recommended_budget=recommendation, work=deepcopy(work),
            feasibility_state='sampled_witness_found' if pareto else 'unknown')

    try:
        if not isinstance(request, FilmResourceEstimateRequest):
            raise ValueError('request must be FilmResourceEstimateRequest')
        if type(request.layer_search_limit) is not int or request.layer_search_limit < 0:
            raise ValueError('layer_search_limit must be nonnegative; it is an assessment window, not an optimization cap')
        if type(request.audit_candidates) is not bool or not isinstance(request.candidate_designs, (tuple, list)):
            raise ValueError('invalid candidate assessment controls')
        if request.max_seconds is not None and (isinstance(request.max_seconds, bool) or
                not isinstance(request.max_seconds, (int, float)) or not math.isfinite(request.max_seconds) or request.max_seconds <= 0):
            raise ValueError('max_seconds must be positive finite or None')
        control.seconds = request.max_seconds
        control.check()
        emit('freeze', '冻结指标、候选材料和完整正背面环境')
        work['source_freeze_attempts'] += 1
        request = freeze_resource_estimate_request(service, request, cancel_event=control)
        work['source_freeze_passes'] += 1
        frozen = request.synthesis_request
        candidates = request.candidate_designs
        sides = selected_sides(frozen)
        source_digest = resource_estimate_source_digest(request)
        reference = dict(request.search_budget_reference or {})
        digest = hashlib.sha256(json.dumps(dict(request=asdict(frozen), candidates=candidates,
            layer_search_limit=request.layer_search_limit, audit_candidates=request.audit_candidates,
            search_budget_reference=reference),
            ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        input_status = 'ready'
        sources = _Sources(frozen, work, control)
        ports = ['incident_medium'] if frozen.environment['substrate']['sides'] == 'single' else ['incident_medium', 'exit_medium']
        for objective in frozen.objectives:
            for wave in np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples):
                for port in ports:
                    if sources.sample(frozen.environment[port], float(wave))[1] != 0:
                        input_status = 'invalid'
                        raise ValueError('resource assessment requires transparent external power ports')
        screens.extend(_target_conflicts(frozen))
        emit('precheck', '仅给模型筛查与已审计见证；未标定预测保持未知')
        bounds.extend(_necessary_bounds(frozen, sources, request.layer_search_limit, work, emit, control))
        screens.extend(_fixed_face_screens(frozen, sources, work, emit, control))
        if request.audit_candidates:
            for index, design in enumerate(candidates):
                control.check()
                row = dict(candidate_index=index, state='invalid', targets_met=False, solver_acceptable=None,
                    within_constraints=False, layer_count=None, total_thickness_nm=None, surfaces={},
                    max_normalized_error=None, physical_hash=None, message='', audit=None,
                    conditional_on_assumption=False, absorption_assumption={})
                try:
                    row.update(_resources(frozen, design, sources, reference))
                    row['physical_hash'] = physical_design_hash(design)
                    work['audits_started'] += 1
                    observed = dict(physical_evaluations=0, wavelength_points=0)
                    last_response = [None]

                    def retain(owners, observed=observed, last_response=last_response):
                        value = owners.get('current_response')
                        if value is None or value is last_response[0]:
                            return
                        last_response[0] = value
                        coverage = value['score']['coverage']
                        points = sum(len(r['wavelength_nm'] if isinstance(r, Mapping) else r.wavelength_nm) for r in coverage)
                        observed['physical_evaluations'] += 1
                        observed['wavelength_points'] += points
                        work['physical_evaluations'] += 1
                        work['wavelength_points'] += points

                    def progress(_value):
                        emit('audit', '完整膜系独立密网格、错位与极值审计')

                    audit = audit_frozen_synthesis_candidate(frozen, design, cancel_event=control,
                        progress_callback=progress, retained_callback=retain)
                    for key in observed:
                        if int(audit['audit'][key]) != observed[key]:
                            raise ValueError('resource audit work ledger mismatch')
                    work['audits_completed'] += 1
                    score = audit['score']
                    row.update(state='audited', targets_met=nominal_targets_pass(frozen, score, independent_audit=True),
                        solver_acceptable=solver_acceptable(frozen, score, independent_audit=True),
                        max_normalized_error=float(score['max_error']), message='finite_sampled_audit_not_continuous_certificate',
                        audit=deepcopy(audit['audit']), absorption_assumption=deepcopy(audit['audit']['absorption_assumption']),
                        conditional_on_assumption=bool(audit['audit']['absorption_assumption']['conditional_on_assumption']))
                except (ValueError, ArithmeticError, KeyError) as exc:
                    row.update(state='error', message=str(exc))
                reports.append(row)
                emit('candidate_complete', '当前结构审计完成；不修改处方或优化约束')
        return finish('completed', '资源评估完成；建议仅来自实际通过审计的联合资源节点，未标定预测未知')
    except _StructureCancelled:
        return finish(control.state(), '资源评估已取消或到时；已完成审计的见证保留，未完成不判失败')
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return finish('failed', str(exc))
