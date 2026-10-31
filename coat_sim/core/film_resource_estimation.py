"""Read-only film resource assessment on the ordinary frozen full-design TMM.

Necessary numerical screens and freshly audited constructive witnesses. Native
hybrid synthesis explores general objectives without mutating the editor.
Numerical effort windows are not physical resource bounds or calibrated forecasts.
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

from .film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request, synthesize_film_design
from .film_design import physical_thickness_nm, validate_design
from .film_design_service import automatic_substrate_handling
from .film_nominal_acceptance import complete_original_score, nominal_targets_pass, solver_acceptable
from .film_optimization_needle import FilmNeedleMaterialReference
from .film_optimization_structure import _StructureCancelled
from .film_optics import _coherent_many, _polarizations
from .film_resource_estimation_bounds import hr_point_layer_screen, hr_thickness_screen
from .film_synthesis_absorption import sample_synthesis_optical, synthesis_absorption_assumption
from .film_synthesis_archive import physical_design_hash
from .film_synthesis_audit import audit_frozen_synthesis_candidate, frozen_synthesis_audit_identity
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
    # Per-seed logical evaluator window (including independent audits), not a physical-call or resource cap.
    # 0 explicitly selects prechecks/current-candidate audits only.
    probe_max_evaluations: int = 256
    probe_seeds: tuple[int, ...] = (0, 1)


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
                physical_evaluations=0, wavelength_points=0, elapsed_seconds=0.,
                probe_runs=0, probe_evaluations=0, probe_physical_evaluations=0,
                probe_wavelength_points=0, probe_outcomes=[], actual_work_complete=True)
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
            source=row.get('source', 'candidate'), probe_seed=row.get('probe_seed'),
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
        if type(request.probe_max_evaluations) is not int or request.probe_max_evaluations < 0 or request.probe_max_evaluations == 1:
            raise ValueError('probe_max_evaluations must be 0 (off) or integer >=2 (baseline plus independent audit)')
        if (not isinstance(request.probe_seeds, (tuple, list)) or not request.probe_seeds or
                any(type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF for seed in request.probe_seeds) or
                len(set(request.probe_seeds)) != len(request.probe_seeds)):
            raise ValueError('probe_seeds must be distinct uint32 integer seeds [0,4294967295]')
        work['probe_profile'] = dict(max_evaluations_per_seed=request.probe_max_evaluations,
            seeds=list(request.probe_seeds), enabled=bool(request.probe_max_evaluations),
            version='constructive_baseline_then_diverse_v1',
            branch_policy='first_native_constructive_baseline_then_seeded_template_or_warm_start',
            effort_role='editable_logical_evaluations_including_audits_not_physical_resource_bound',
            calibration='not_calibrated', template_randomization=True,
            start_policy='first_seed_constructive_remaining_current_and_explicit_nonempty_starts')
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
            search_budget_reference=reference, probe_max_evaluations=request.probe_max_evaluations,
            probe_seeds=request.probe_seeds),
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
                        audit=deepcopy(audit['audit']), objective_status=deepcopy(audit['audit']['objective_status']), absorption_assumption=deepcopy(audit['audit']['absorption_assumption']),
                        conditional_on_assumption=bool(audit['audit']['absorption_assumption']['conditional_on_assumption']))
                except (ValueError, ArithmeticError, KeyError) as exc:
                    row.update(state='error', message=str(exc))
                row['source'] = 'candidate'
                row['candidate_design'] = deepcopy(design)
                reports.append(row)
                emit('candidate_complete', '当前结构审计完成；不修改处方或优化约束')
        if request.probe_max_evaluations:
            _explore_resources(service, request, sources, reference, work, reports, control, emit,
                               progress_callback)
        control.check()
        return finish('completed', '资源评估完成；仅报告实际找到且审计通过的联合资源例子，不是最小值；未找到不代表不可实现')
    except _StructureCancelled:
        return finish(control.state(), '资源评估已取消或到时；已完成审计的见证保留，未完成不判失败')
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return finish('failed', str(exc))


def _explore_resources(service, request, sources, reference, work, reports, control, emit, progress_callback):
    """Reuse the native full-objective hybrid/local search and fresh audit owner.

    Snapshot callbacks are emitted *after* a new independent original-target
    audit, never on training scores alone. Their evidence survives cancellation.
    Work counters are native cumulative counters, delta-accounted per run; audit
    physical calls and feedback repair calls are accounted separately from logical evaluations.
    """
    frozen = request.synthesis_request
    seen = {r.get('physical_hash') for r in reports if r.get('state') == 'audited'}
    # Only legal starts may enter native search; invalid audit candidates remain
    # useful diagnostics but must not poison every constructive branch.
    starts = list(frozen.seed_designs)
    for design in request.candidate_designs:
        try:
            resources = _resources(frozen, design, sources, reference)
        except (ValueError, ArithmeticError, KeyError) as exc:
            # Candidate-specific bad physical thickness is already an error
            # diagnostic, not a reason to suppress independent constructive starts.
            work.setdefault('probe_skipped_starts', []).append(dict(
                physical_hash=physical_design_hash(design), reason=str(exc)))
            continue
        if resources['within_constraints']:
            starts.append(design)
        else:
            work.setdefault('probe_skipped_starts', []).append(dict(
                physical_hash=physical_design_hash(design), reason='outside_process_constraints_or_material_pool'))
    unique_starts: dict[str, Mapping[str, Any]] = {}
    for design in starts:
        # An empty current stack is the native baseline, not an explicit seed:
        # passing it as seed would disable native constructive HR/AR routes.
        if selected_rows(frozen, design):
            unique_starts.setdefault(physical_design_hash(design), design)
    for branch_index, seed in enumerate(request.probe_seeds):
        control.check()
        emit('probe', f'通用结构与厚度探测：种子 {seed}，逻辑评价窗口（含审计） {request.probe_max_evaluations}')
        work['probe_runs'] += 1
        outcome = dict(seed=seed, requested_max_evaluations=request.probe_max_evaluations,
            state='running', termination_reason=None, evaluations=0, physical_evaluations=0,
            wavelength_points=0, actual_work_complete=True, candidates_retained=0,
            randomized_template_search=branch_index > 0,
            audited_candidate_hashes=[], start_mode='constructive' if branch_index == 0 or not unique_starts else 'current_and_explicit_starts')
        work['probe_outcomes'].append(outcome)
        charged = dict(evaluations=0, physical_evaluations=0, wavelength_points=0)

        def account(cumulative, performance, charged=charged, outcome=outcome):
            feedback = performance.get('audit_feedback', {})
            physical = (int(performance.get('known_training_physical_evaluations', 0)) +
                int(cumulative.get('audit_physical_evaluations', 0)) +
                int(feedback.get('physical_evaluations', 0)))
            points = (int(performance.get('known_training_wavelength_points', 0)) +
                int(cumulative.get('audit_wavelength_points', 0)) +
                int(feedback.get('wavelength_points', 0)))
            totals = dict(evaluations=int(cumulative.get('evaluations', 0)),
                physical_evaluations=physical, wavelength_points=points)
            for key, value in totals.items():
                # Native cumulative ledgers must never regress within one run.
                if value < charged[key]:
                    raise ValueError('resource probe work ledger regression')
                delta = value - charged[key]
                work['probe_' + key] += delta
                if key != 'evaluations':
                    work[key] += delta
                charged[key] = outcome[key] = value
            complete = bool(performance.get('actual_work_complete', True))
            outcome['actual_work_complete'] = complete
            work['actual_work_complete'] = work['actual_work_complete'] and complete

        def snapshot(design, training, validation, audit, account=account, outcome=outcome, seed=seed):
            audit = audit if isinstance(audit, Mapping) else {}
            cumulative, performance = audit.get('synthesis_cumulative_work'), audit.get('synthesis_performance')
            if isinstance(cumulative, Mapping) and isinstance(performance, Mapping):
                account(cumulative, performance)
            signature = physical_design_hash(design)
            outcome['audited_candidate_hashes'].append(signature)
            identity = frozen_synthesis_audit_identity(frozen, design)
            audit_core = {k: v for k, v in audit.items() if not k.startswith('synthesis_')
                          and k not in ('search_checkpoint', 'local_optimum')}
            evidence_valid = (all(audit.get(key) == value for key, value in identity.items())
                and complete_original_score(frozen, training)
                and complete_original_score(frozen, validation, independent_audit=True)
                and audit_core == validation.get('audit'))
            if not evidence_valid:
                outcome['invalid_audit_snapshots'] = outcome.get('invalid_audit_snapshots', 0) + 1
                reports.append(dict(candidate_index=len(reports), source='probe', probe_seed=seed,
                    state='not_audited', physical_hash=signature, targets_met=False, solver_acceptable=None,
                    **_resources(frozen, design, sources, reference), max_normalized_error=None,
                    message='incomplete_or_mismatched_original_audit_evidence', audit=None,
                    conditional_on_assumption=False, absorption_assumption={}))
                return
            if signature in seen:
                return
            # Re-validate original environment and process/material constraints;
            # never trust a carried-over score, user target flag or training pass.
            validate_candidate_environment(frozen, design)
            row = dict(candidate_index=len(reports), source='probe', probe_seed=seed,
                state='audited', physical_hash=signature, candidate_design=deepcopy(design),
                **_resources(frozen, design, sources, reference),
                targets_met=(nominal_targets_pass(frozen, training) and
                    nominal_targets_pass(frozen, validation, independent_audit=True)),
                training_targets_met=nominal_targets_pass(frozen, training),
                audit_targets_met=nominal_targets_pass(frozen, validation, independent_audit=True),
                solver_acceptable=solver_acceptable(frozen, validation, independent_audit=True),
                max_normalized_error=float(validation['max_error']),
                message='fresh_native_full_original_audit_not_continuous_certificate',
                audit=deepcopy(audit_core), objective_status=deepcopy(audit_core['objective_status']),
                absorption_assumption=deepcopy(audit['absorption_assumption']),
                conditional_on_assumption=bool(audit['absorption_assumption']['conditional_on_assumption']))
            reports.append(row)
            seen.add(signature)
            outcome['candidates_retained'] += 1

        def progress(value, outcome=outcome, seed=seed):
            # The native owner observes the same cancellation event immediately
            # after callbacks. Do not throw from an observer and lose its final
            # exact physical ledger. Report logical progress separately.
            outcome['logical_evaluations_observed'] = value.evaluations
            if progress_callback is not None:
                progress_callback(FilmResourceEstimateProgress('probe_' + value.stage,
                    f'种子 {seed}：逻辑评价 {value.evaluations}/{request.probe_max_evaluations}；'
                    f'已完成审计且达标 {sum(r["state"] == "audited" and r["targets_met"] and r["within_constraints"] for r in reports)} 个；{value.message}',
                    work['dp_conditions'] + work['audits_completed'],
                    work['physical_evaluations'], work['wavelength_points']))

        probe = replace(frozen, search_strategy='hybrid', ar_structure_mode='clustered',
            max_layers=None, max_total_thickness_nm=None, max_cycles=None,
            max_evaluations=request.probe_max_evaluations, max_seconds=None,
            # Keep a deterministic native constructive reference. Randomizing
            # every branch can discard useful native priors on difficult AR
            # targets; later branches add reproducible diversity instead.
            structure_search_seed=seed, randomize_template_search=branch_index > 0,
            search_checkpoint=None, warm_start_seed_index=None,
            # Reserve at least the first branch for independent constructive
            # search; explicit/current starts must not disable every native prior.
            seed_designs=() if branch_index == 0 else tuple(unique_starts.values()))
        result = synthesize_film_design(service, probe, cancel_event=control,
            progress_callback=progress, snapshot_callback=snapshot)
        account(dict(evaluations=result.evaluations,
            audit_physical_evaluations=result.audit_physical_evaluations,
            audit_wavelength_points=result.audit_wavelength_points), result.performance)
        outcome.update(state=result.state, termination_reason=result.termination_reason,
            failed_evaluations=result.failed_evaluations, cycles=result.cycles,
            validation_evaluations=result.validation_evaluations,
            evaluation_window_exceeded=result.evaluations > request.probe_max_evaluations)
        if result.candidate_design is not None:
            signature = physical_design_hash(result.candidate_design)
            if signature not in seen:
                reports.append(dict(candidate_index=len(reports), source='probe', probe_seed=seed,
                    state='not_audited', targets_met=False, solver_acceptable=None,
                    physical_hash=signature, **_resources(frozen, result.candidate_design, sources, reference),
                    max_normalized_error=None if result.training_score is None else float(result.training_score['max_error']),
                    message='incomplete_candidate_not_a_feasibility_witness', audit=None,
                    conditional_on_assumption=False, absorption_assumption={}))
        control.check()
        emit('probe_complete', f'种子 {seed} 探测结束：{result.termination_reason}；未找到不能判定不可行')
