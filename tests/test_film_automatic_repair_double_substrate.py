"""Double-sided thin-film repair must retain the real optical environment."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core import film_automatic_repair as repair
from coat_sim.core import film_automatic_repair_jobs as jobs
from coat_sim.core.film_design import validate_design
from coat_sim.core.film_design_service import automatic_substrate_handling, simulate_frozen
from coat_sim.core.film_optimization import FilmThicknessVariable
from coat_sim.core.film_synthesis_physics import simulate_synthesis_with_thickness_jacobian
from tests.test_film_auto_synthesis import setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def _problem(mode='auto', thickness_nm=12000.):
    service, synthesis = setup_problem(upper=1., low=540., high=560., samples=3)
    environment = deepcopy(synthesis.environment)
    environment['substrate'].update(sides='double', mode=mode,
                                    thickness={'value': thickness_nm, 'unit': 'nm'})
    environment['first_surface'] = 'front'
    environment['use_k'] = True
    # Keep a nonzero extinction coefficient so a silent k=0 substitution is observable.
    service.materials.records['glass']['optical_sets'][0]['k'] = [.002] * 3
    source = deepcopy(environment)
    source['front_layers'] = [
        dict(id='left', material_id='high', optical_set_id='nk',
             thickness={'value': 40., 'unit': 'nm'}, note=''),
        dict(id='thin', material_id='low', optical_set_id='nk',
             thickness={'value': .5, 'unit': 'nm'}, note=''),
    ]
    source['back_layers'] = [dict(id='back-thin', material_id='low', optical_set_id='nk',
                                  thickness={'value': .4, 'unit': 'nm'}, note='')]
    return service, replace(synthesis, environment=environment, selected_surfaces=('front', 'back'),
                            validation_samples_multiplier=2), source


def _make(service, synthesis, source, *, live=False):
    method = jobs.make_live_request_payload if live else jobs.make_request_payload
    return method(service, synthesis, source, reference_nm=1., thickness_tolerance_nm=.1,
                  optical_tolerance_percentage_points=100.,
                  comparison_settings={'wavelength_min_nm': 540., 'wavelength_max_nm': 560.,
                                       'samples': 3, 'first_surface': source['first_surface']})


@pytest.mark.parametrize('mode,thickness_nm,expected', [
    ('auto', 12000., 'incoherent'),
    ('coherent', 700., 'coherent'),
    ('incoherent', 12000., 'incoherent'),
])
@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('first_surface', ['front', 'back'])
def test_double_environment_survives_frozen_request_and_real_jacobian(mode, thickness_nm, expected, live, first_surface):
    service, request, source = _problem(mode, thickness_nm)
    source['first_surface'] = first_surface
    request = replace(request, environment={**deepcopy(request.environment), 'first_surface': first_surface})
    original = deepcopy(source)
    normalized = validate_design(deepcopy(source))
    payload = _make(service, request, source, live=live)
    frozen, decoded = jobs.request_from_payload(payload)
    assert payload['schema_version'] == (3 if live else 2)
    assert decoded == normalized and decoded['back_layers']
    # Legacy mode labels are intentionally canonicalized to auto by the design schema.
    assert decoded['substrate']['mode'] == 'auto'
    for key in ('substrate', 'incident_medium', 'exit_medium', 'first_surface', 'use_k'):
        assert frozen.environment[key] == normalized[key]
    assert automatic_substrate_handling(decoded, 560.)['resolved_mode'] == expected
    settings = payload['comparison_settings']
    variables = [FilmThicknessVariable('front', 'thin', 0., 1000.),
                 FilmThicknessVariable('back', 'back-thin', 0., 1000.)]
    response = simulate_synthesis_with_thickness_jacobian(
        decoded, frozen.frozen_material_snapshot, variables, settings)
    direct = simulate_frozen('.', service.freeze_design(decoded, settings))
    for metric in ('R', 'T'):
        np.testing.assert_allclose(response[metric], direct[metric], rtol=0., atol=1e-13)
    for column, (side, index) in enumerate((('front', 1), ('back', 0))):
        plus, minus = deepcopy(decoded), deepcopy(decoded)
        step = 1e-3
        plus[side + '_layers'][index]['thickness']['value'] += step
        minus[side + '_layers'][index]['thickness']['value'] -= step
        p = simulate_frozen('.', service.freeze_design(plus, settings))
        m = simulate_frozen('.', service.freeze_design(minus, settings))
        for metric in ('R', 'T'):
            finite_difference = (np.asarray(p[metric]) - np.asarray(m[metric])) / (2 * step)
            np.testing.assert_allclose(np.asarray(response['jacobian_' + metric])[:, column],
                                       finite_difference, rtol=3e-6, atol=2e-10)
    assert source == original


def test_double_real_repair_keeps_environment_and_paired_independent_audit(monkeypatch):
    service, request, source = _problem()
    payload = _make(service, request, source)
    frozen, source = jobs.request_from_payload(payload)
    original = deepcopy(source)
    # Use a deterministic physical proposal, but retain the real evaluator and
    # independent dense/offset/extremum audit rather than a fabricated score.
    def evaluate_initial(_request, initial, **_kwargs):
        return initial, {'termination': 'controlled initial proposal'}
    monkeypatch.setattr(repair, 'refine_synthesis_full_minimax', evaluate_initial)
    result = repair.automatic_repair(
        frozen, source, target_nm=1., thickness_tolerance_nm=.1, optical_tolerance=1.,
        optical_settings=payload['comparison_settings'],
        policy=dict(version=1, proposals=1, local_iterations=2, physical_calls=200, seconds=20.))
    assert result['source']['audit_score']['audit']['dense_points'] >= 3
    assert set(result['detected_layer_ids']) == {'thin', 'back-thin'}
    candidate = result['best']
    assert candidate is not None, result
    assert candidate['thickness_pass'] and candidate['optical_guard']['passed']
    assert candidate['training_score']['nominal_pass'] and candidate['audit_score']['nominal_pass']
    assert candidate['optical_guard']['audit']['kind'] == 'paired_curve_difference'
    assert candidate['optical_guard']['audit']['dense_points'] >= 3
    assert candidate['optical_guard']['records']
    assert all(repair.paired_interval_pass(row['candidate_values'], row['reference_values'], 1.)
               for row in candidate['optical_guard']['records'])
    for key in ('substrate', 'incident_medium', 'exit_medium', 'first_surface', 'use_k'):
        assert candidate['design'][key] == original[key]
    assert candidate['design']['back_layers']
    assert all(row['thickness']['value'] >= .9 for side in ('front', 'back')
               for row in candidate['design'][side + '_layers'])
    assert source == original


def test_source_without_k_remains_rejected():
    service, request, source = _problem()
    source['use_k'] = False
    request = replace(request, environment={**deepcopy(request.environment), 'use_k': False})
    for live in (False, True):
        with pytest.raises(ValueError):
            _make(service, request, source, live=live)


@pytest.mark.parametrize('first_surface', ['front', 'back'])
def test_live_double_sided_repair_audits_both_sides_before_full_success(monkeypatch, first_surface):
    from threading import Event, Timer
    from types import SimpleNamespace
    from importlib import import_module
    scipy_optimize = import_module("scipy.optimize")

    service, request, source = _problem()
    source['first_surface'] = first_surface
    request = replace(request, environment={**deepcopy(request.environment), 'first_surface': first_surface})
    payload = _make(service, request, source, live=True)
    frozen, source = jobs.request_from_payload(payload)
    original = deepcopy(source)
    stop = Event()
    timer = Timer(15., stop.set)
    frames = []
    # Keep the local solver finite and deterministic; the existing proposal,
    # full physical evaluator and independent audits all remain real.
    def identity_solver(_fun, x0, **_kwargs):
        return SimpleNamespace(x=np.asarray(x0), success=True, status=0, nit=0, message='initial point')
    monkeypatch.setattr(scipy_optimize, 'minimize', identity_solver)
    def report(frame):
        frames.append(deepcopy(frame))
        champion = frame.get('champion')
        if champion and champion.get('assessment', {}).get('all_requirements_met'):
            stop.set()
    timer.start()
    try:
        result = repair.automatic_repair_live(
            frozen, source, target_nm=1., thickness_tolerance_nm=.1, optical_tolerance=1.,
            optical_settings=payload['comparison_settings'], cancel_event=stop, report=report,
            policy=dict(repair.LIVE_POLICY, local_iterations=2, feedback_rounds=0))
    finally:
        timer.cancel()
    assert set(repair.deficient_ids(source, .9)) == {'thin', 'back-thin'}
    best = result['best']
    assert best is not None, result
    assert best['assessment']['all_requirements_met']
    assert best['optical_guard']['passed'] and best['audit_score']['audit']['dense_points'] >= 3
    assert all(row['thickness']['value'] >= .9 for side in ('front', 'back')
               for row in best['design'][side + '_layers'])
    for key in ('substrate', 'incident_medium', 'exit_medium', 'first_surface', 'use_k'):
        assert best['design'][key] == original[key]
    assert source == original


@pytest.mark.parametrize('first_surface', ['front', 'back'])
@pytest.mark.parametrize('live', [False, True])
def test_double_substrate_empty_back_remains_double_and_uses_actual_exit_port(first_surface, live):
    service, request, source = _problem()
    source['back_layers'] = []
    source['first_surface'] = first_surface
    request = replace(request, environment={**deepcopy(request.environment), 'first_surface': first_surface})
    payload = _make(service, request, source, live=live)
    frozen, decoded = jobs.request_from_payload(payload)
    assert decoded['substrate']['sides'] == 'double' and decoded['back_layers'] == []
    assert decoded['first_surface'] == first_surface
    assert repair.deficient_ids(decoded, .9) == ['thin']
    settings = payload['comparison_settings']
    response = simulate_synthesis_with_thickness_jacobian(decoded, frozen.frozen_material_snapshot,
        [FilmThicknessVariable('front', 'thin', 0., 1000.)], settings)
    actual = simulate_frozen('.', service.freeze_design(decoded, settings))
    for metric in ('R', 'T'):
        np.testing.assert_allclose(response[metric], actual[metric], rtol=0., atol=1e-13)


def test_live_double_sided_real_scipy_optimization_and_physical_audit(monkeypatch):
    from threading import Event, Timer
    from importlib import import_module
    scipy_optimize = import_module("scipy.optimize")

    service, request, source = _problem()
    payload = _make(service, request, source, live=True)
    frozen, source = jobs.request_from_payload(payload)
    calls = []
    actual_minimize = scipy_optimize.minimize
    def observed_minimize(*args, **kwargs):
        calls.append((kwargs.get('method'), len(args[1])))
        return actual_minimize(*args, **kwargs)
    monkeypatch.setattr(scipy_optimize, 'minimize', observed_minimize)
    stop = Event()
    timer = Timer(15., stop.set)
    def report(frame):
        champion = frame.get('champion')
        if champion and champion.get('assessment', {}).get('all_requirements_met'):
            stop.set()
    timer.start()
    try:
        result = repair.automatic_repair_live(
            frozen, source, target_nm=1., thickness_tolerance_nm=.1, optical_tolerance=1.,
            optical_settings=payload['comparison_settings'], cancel_event=stop, report=report,
            policy=dict(repair.LIVE_POLICY, local_iterations=10, feedback_rounds=0))
    finally:
        timer.cancel()
    assert calls and any(method == 'SLSQP' and size == 3 for method, size in calls), calls
    best = result['best']
    assert best is not None, result
    assert best['assessment']['all_requirements_met'] and best['optical_guard']['passed']
    assert best['audit_score']['audit']['dense_points'] >= 3
    assert all(row['thickness']['value'] >= .9 for side in ('front', 'back')
               for row in best['design'][side + '_layers'])


def test_audit_resource_reservation_counts_active_back_layer():
    from coat_sim.core.film_manufacturing_jobs import estimate_audit_resources
    from coat_sim.core.film_synthesis_manufacturing import _owned_bytes

    service, request, source = _problem()
    frozen, source = jobs.request_from_payload(_make(service, request, source, live=True))
    without_back = deepcopy(source)
    without_back['back_layers'] = []
    full = estimate_audit_resources(frozen, source, physical_call_limit=2)
    empty = estimate_audit_resources(frozen, without_back, physical_call_limit=2)
    dense = sum((1 if o.wavelength_samples == 1 else
                 (o.wavelength_samples - 1) * frozen.validation_samples_multiplier + 1)
                * len(o.angles_deg) for o in frozen.objectives)
    assert full['working_set_bytes'] - empty['working_set_bytes'] == (
        384 * dense * len(source['back_layers'])
        + 6 * (_owned_bytes(frozen, source) - _owned_bytes(frozen, without_back)))


def test_zero_cleanup_keeps_opposite_surface_layers_separate_and_lineage_local():
    service, request, source = _problem()
    frozen, source = jobs.request_from_payload(_make(service, request, source, live=True))
    raw = deepcopy(source)
    for side in ('front', 'back'):
        template = deepcopy(raw[side + '_layers'][-1])
        template['id'] = side + '-zero'
        template['thickness']['value'] = 0.
        raw[side + '_layers'].append(template)
    origins = {row['id']: ([row['id']] if not row['id'].endswith('-zero') else [])
               for row in repair.repair_rows(raw)}
    cleaned, lineage = repair._live_cleanup(frozen, raw, origins)
    assert set(cleaned.removed_exact_zero_ids) == {'front-zero', 'back-zero'}
    assert not cleaned.merged_ids
    assert [row['id'] for row in cleaned.design['front_layers']] == ['left', 'thin']
    assert [row['id'] for row in cleaned.design['back_layers']] == ['back-thin']
    assert lineage['thin'] == ['thin'] and lineage['back-thin'] == ['back-thin']
    jobs._validate_live_layers(repair.live_layers(cleaned.design, lineage), cleaned.design, source)
