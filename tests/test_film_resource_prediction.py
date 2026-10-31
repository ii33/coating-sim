"""Online inference contract tests. Tiny model fixture is not calibration evidence."""
from copy import deepcopy
from dataclasses import replace
import json
import math
from threading import Event

import pytest

from coat_sim.core import film_resource_prediction as prediction
from coat_sim.core import film_resource_estimation as estimation
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


@pytest.fixture
def problem(tmp_path, monkeypatch):
    service, base = setup_problem()
    for name, value in [('low', 1.38), ('high', 2.2), ('glass', 1.52)]:
        service.materials.records[name]['optical_sets'][0]['n'] = [value]*3
    request = estimation.FilmResourceEstimateRequest(base)
    frozen = estimation.freeze_resource_estimate_request(service, request)
    family, features, wavelength, _domain = prediction._features(frozen)
    model = dict(regressor='ridge', basis_mean=[0.]*21, basis_scale=[1.]*21,
        coefficients=[[0., 0.] for _ in range(21)], intercept=[math.log(7), math.log(.5)],
        features=[features.tolist()], paired_log_resources=[[math.log(7), math.log(.5)]],
        feature_min=(features-.1).tolist(), feature_max=(features+.1).tolist(),
        feature_scales=[1.]*5, neighbors=1, max_neighbor_distance=1., distance_regularizer=.02,
        joint_log_radius=.2, coverage_note='synthetic contract fixture; no calibration claim')
    package = dict(schema_version=1, validation_status='passed', model_id='test-only',
        reference_profile='test-only', applicability_note='test-only', families={family:model})
    path = tmp_path/'model.json'
    path.write_text(json.dumps(package), encoding='utf-8')
    monkeypatch.setattr(prediction, '_MODEL_PATH', path)
    return service, request, package, path, wavelength


def test_prediction_never_calls_solver_auditor_or_necessary_bound(problem, monkeypatch):
    service, request, _, _, wave = problem
    def forbidden(*args, **kwargs):
        pytest.fail('Online prediction entered an offline solver or diagnostic')
    for name in ('synthesize_film_design', 'audit_frozen_synthesis_candidate',
                 'hr_point_layer_screen', 'hr_thickness_screen'):
        monkeypatch.setattr(estimation, name, forbidden)
    before = deepcopy(request.synthesis_request.environment)
    result = prediction.predict_film_resources(service, request)
    assert result.state == 'completed' and result.estimate_state == 'estimated'
    assert result.prediction is not None
    assert result.prediction['layer_count'] == 6
    assert result.prediction['total_thickness_nm'] == pytest.approx(wave*.5)
    region = result.prediction['joint_interval']
    assert region['layer_min'] <= 6 <= region['layer_max']
    assert region['thickness_min_nm'] < wave*.5 < region['thickness_max_nm']
    assert result.work['physical_evaluations'] == result.work['probe_runs'] == 0
    assert result.feasibility_state == 'unknown' and not result.continuous_certificate
    assert request.synthesis_request.environment == before
    assert result.source_digest == estimation.resource_estimate_source_digest(
        estimation.freeze_resource_estimate_request(service, request))


@pytest.mark.parametrize('damage', ['dispersion', 'absorption', 'angle', 'mean', 'manufacturing', 'strict'])
def test_outside_domain_does_not_silently_simplify_target(problem, damage):
    service, request, _, _, _ = problem
    base = request.synthesis_request
    if damage == 'dispersion':
        service.materials.records['low']['optical_sets'][0]['n'][1] += .01
    elif damage == 'absorption':
        service.materials.records['low']['optical_sets'][0]['k'] = [.01]*3
    elif damage == 'angle':
        base = replace(base, objectives=(replace(base.objectives[0], angles_deg=(15.,)),))
    elif damage == 'mean':
        obj = base.objectives[0]
        base = replace(base, objectives=(replace(obj, mean=replace(obj.pointwise, enabled=True)),))
    elif damage == 'manufacturing':
        base = replace(base, minimum_nonzero_thickness_nm=25.)
    else:
        obj = base.objectives[0]
        base = replace(base, objectives=(replace(obj, pointwise=replace(obj.pointwise, strict_upper=True)),))
    result = prediction.predict_film_resources(service, replace(request, synthesis_request=base))
    assert result.estimate_state == 'out_of_domain' and result.prediction is None
    assert result.work['physical_evaluations'] == 0


def test_missing_model_and_failed_validation_are_not_predictions(problem):
    service, request, package, path, _ = problem
    package['validation_status'] = 'failed'
    path.write_text(json.dumps(package), encoding='utf-8')
    result = prediction.predict_film_resources(service, request)
    assert result.estimate_state == 'not_calibrated' and result.prediction is None
    path.unlink()
    result = prediction.predict_film_resources(service, request)
    assert result.estimate_state == 'not_calibrated' and result.prediction is None


def test_missing_family_is_unsupported_not_automatic_fallback(problem):
    service, request, package, path, _ = problem
    package['families'] = {}
    path.write_text(json.dumps(package), encoding='utf-8')
    result = prediction.predict_film_resources(service, request)
    assert result.estimate_state == 'out_of_domain' and result.prediction is None


def test_cancellation_keeps_no_prediction(problem):
    service, request, _, _, _ = problem
    event = Event()
    event.set()
    result = prediction.predict_film_resources(service, request, cancel_event=event)
    assert result.state == 'cancelled' and result.prediction is None


def test_invalid_source_stays_input_error(problem):
    service, request, _, _, _ = problem
    service.materials.records['low']['optical_sets'][0]['k'] = None
    result = prediction.predict_film_resources(service, request)
    assert result.state == 'failed' and result.input_status == 'invalid'
    assert result.prediction is None


def test_redundant_physical_bound_does_not_reject_ar(problem):
    service, request, _, _, _ = problem
    objective = request.synthesis_request.objectives[0]
    request = replace(request, synthesis_request=replace(request.synthesis_request, objectives=(
        replace(objective, pointwise=replace(objective.pointwise, lower_bound=0.)),)))
    result = prediction.predict_film_resources(service, request)
    assert result.estimate_state == 'estimated'
    assert result.prediction is not None
    assert result.prediction['layer_count'] == 6


def test_prediction_source_freeze_has_fixed_grid_and_original_identity(problem, monkeypatch):
    from coat_sim.core import film_auto_synthesis
    service, request, _, _, _ = problem
    original = film_auto_synthesis.sample_synthesis_optical
    calls = []
    def observed(sampler, wavelength, **kwargs):
        calls.append(wavelength)
        return original(sampler, wavelength, **kwargs)
    monkeypatch.setattr(film_auto_synthesis, 'sample_synthesis_optical', observed)
    counts = []
    for count in (301, 10001):
        obj = replace(request.synthesis_request.objectives[0], wavelength_samples=count)
        current = replace(request, synthesis_request=replace(request.synthesis_request, objectives=(obj,)))
        calls.clear()
        frozen = prediction.freeze_film_prediction_request(service, current)
        counts.append(len(calls))
        assert frozen.synthesis_request.objectives == current.synthesis_request.objectives
        assert frozen.synthesis_request.validation_samples_multiplier == current.synthesis_request.validation_samples_multiplier
        assert counts[-1] < 100
    assert counts[0] == counts[1]


def _real_problem():
    """Actual public source tables, not constant-index replacements."""
    from pathlib import Path
    from tests.test_film_auto_synthesis import Service
    from coat_sim.core.optical_substrate_presets import builtin_optical_substrates
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root/'data/optical_catalog.json').read_text(encoding='utf-8'))
    refs = [('SiO2_film', 'ri_sio2_rodriguez_de_marcos'), ('TiO2_film', 'ri_tio2_jolivet_amorphous')]
    records = [dict(id=m['material_id'], name=m['material_id'], design_ready=True,
                    optical_sets=deepcopy(m['optical_constants']))
               for m in catalog['materials'] if m['material_id'] in {r[0] for r in refs}]
    substrate = next(m for m in builtin_optical_substrates() if m['id'] == 'film_substrate_schott_nbk7')
    records.append(substrate)
    service = Service(records)
    _, base = setup_problem()
    env = deepcopy(base.environment)
    env['assume_lossless'] = True  # Explicitly unknown substrate k only; film k is retained.
    env['substrate'].update(material_id=substrate['id'], optical_set_id='schott_nbk7_sellmeier')
    base = replace(base, environment=env,
        objectives=(FilmOptimizationObjective(400., 700., 301,
                    pointwise=FilmObjectiveConstraint(upper_bound=.005)),),
        candidate_materials=tuple(FilmNeedleMaterialReference(*r) for r in refs))
    return service, estimation.FilmResourceEstimateRequest(base)


def test_packaged_model_handles_registered_real_nk_without_optical_solves(monkeypatch):
    service, request = _real_problem()
    def forbidden(*args, **kwargs):
        pytest.fail('Prediction must not perform synthesis, candidate audit or a necessary-bound solve')
    for name in ('synthesize_film_design', 'audit_frozen_synthesis_candidate',
                 'hr_point_layer_screen', 'hr_thickness_screen'):
        monkeypatch.setattr(estimation, name, forbidden)
    before = deepcopy(service.materials.records)
    result = prediction.predict_film_resources(service, request)
    assert result.state == 'completed' and result.estimate_state == 'estimated', result.message
    assert result.prediction is not None
    assert result.prediction['feature_domain'] == 'registered_real_nk'
    assert result.prediction['conditional_on_assumption']
    assert result.prediction['known_k_preserved']
    assert result.work['feature_domain']['k_ranges'][0][0] > 0
    assert result.work['feature_domain']['n_ranges'][0][0] != result.work['feature_domain']['n_ranges'][0][1]
    assert result.work['physical_evaluations'] == result.work['probe_runs'] == 0
    assert result.input_digest and result.source_digest and result.work['model_sha256']
    assert service.materials.records == before
    assert result.feasibility_state == 'unknown' and not result.pareto_witnesses


@pytest.mark.parametrize('channel', ['n', 'k'])
def test_changed_real_source_cannot_reuse_registered_curve_claim(channel):
    service, request = _real_problem()
    optical = next(o for o in service.materials.records['SiO2_film']['optical_sets']
                   if o['id'] == 'ri_sio2_rodriguez_de_marcos')
    index = min(range(len(optical['wavelengths'])), key=lambda i: abs(optical['wavelengths'][i]-.55))
    optical[channel][index] += .001
    result = prediction.predict_film_resources(service, request)
    assert result.estimate_state == 'out_of_domain' and result.prediction is None
    assert result.work['physical_evaluations'] == 0


def test_real_substrate_unknown_k_still_requires_explicit_opt_in():
    service, request = _real_problem()
    env = deepcopy(request.synthesis_request.environment)
    env['assume_lossless'] = False
    result = prediction.predict_film_resources(service, replace(request,
        synthesis_request=replace(request.synthesis_request, environment=env)))
    assert result.state == 'failed' and result.input_status == 'invalid'
    assert result.prediction is None


def test_quick_forecast_does_not_validate_or_audit_unrelated_current_stack(problem):
    service, request, _, _, _ = problem
    unrelated = deepcopy(request.synthesis_request.environment)
    unrelated['front_layers'] = [dict(id='unrelated-layer', material_id='unavailable-current-material',
        optical_set_id='missing', thickness=dict(value=100., unit='nm'), note='not a predictor input')]
    request = replace(request, candidate_designs=(unrelated,), synthesis_request=replace(
        request.synthesis_request, seed_designs=(unrelated,)))
    result = prediction.predict_film_resources(service, request)
    assert result.estimate_state == 'estimated', result.message
    frozen = prediction.freeze_film_prediction_request(service, request)
    assert frozen.candidate_designs == () and frozen.synthesis_request.seed_designs == ()
    assert frozen.synthesis_request.objectives == request.synthesis_request.objectives


@pytest.mark.parametrize('damage', ['unknown_regressor', 'zero_scale', 'not_object'])
def test_invalid_model_never_falls_back_to_another_inference_or_solver(problem, damage):
    service, request, package, path, _ = problem
    if damage == 'not_object':
        path.write_text('[]', encoding='utf-8')
    else:
        model = package['families']['AR']
        if damage == 'unknown_regressor':
            model['regressor'] = 'unverified_search'
        else:
            model['basis_scale'][0] = 0.
        path.write_text(json.dumps(package), encoding='utf-8')
    result = prediction.predict_film_resources(service, request)
    assert result.state == 'failed' and result.input_status == 'ready'
    assert result.prediction is None and result.work['physical_evaluations'] == 0


def test_registered_real_domain_accepts_equivalent_unit_air_material():
    service, request = _real_problem()
    service.materials.records['unit-air'] = dict(id='unit-air', name='unit air', optical_sets=[
        dict(id='nk', name='unit air', kind='tabulated', wavelength_unit='nm',
             wavelength_min=400., wavelength_max=700., wavelengths=[400., 550., 700.],
             n=[1., 1., 1.], k=[0., 0., 0.])])
    env = deepcopy(request.synthesis_request.environment)
    env['incident_medium'] = dict(kind='material', material_id='unit-air', optical_set_id='nk')
    result = prediction.predict_film_resources(service, replace(request,
        synthesis_request=replace(request.synthesis_request, environment=env)))
    assert result.estimate_state == 'estimated', result.message
    assert result.prediction is not None and result.prediction['feature_domain'] == 'registered_real_nk'


def test_back_incidence_cannot_be_silently_treated_as_front(problem):
    service, request, _, _, _ = problem
    env = deepcopy(request.synthesis_request.environment)
    env['first_surface'] = 'back'
    result = prediction.predict_film_resources(service, replace(request,
        synthesis_request=replace(request.synthesis_request, environment=env)))
    assert result.estimate_state != 'estimated' and result.prediction is None
