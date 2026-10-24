from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core.film_auto_synthesis import (
    FilmSynthesisRequest, freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference

pytestmark = pytest.mark.algorithm


class Materials:
    def __init__(self, records):
        self.records = {r['id']: deepcopy(r) for r in records}

    def _records(self):
        return deepcopy(list(self.records.values()))

    def get(self, identifier):
        return deepcopy(self.records[identifier])


class Service:
    validate = staticmethod(validate_design)
    freeze_design = FilmDesignService.freeze_design

    def __init__(self, records):
        self.materials = Materials(records)


def setup_problem(*, upper=.005, low=520., high=580., samples=5, materials=('low', 'high')):
    records = []
    for name, n in [('glass', 1.5), ('low', np.sqrt(1.5)), ('high', 2.2)]:
        optical = dict(id='nk', name=name, kind='tabulated', notes='analytical fixture',
            source='synthetic validation oracle; not real material evidence', wavelength_unit='nm',
            wavelength_min=450., wavelength_max=700., wavelengths=[450., 550., 700.],
            n=[float(n)] * 3, k=[0.] * 3)
        records.append(dict(id=name, name=name, design_ready=True, optical_sets=[optical]))
    environment = new_design(design_id='bare-substrate-test')
    environment['substrate'].update(material_id='glass', optical_set_id='nk')
    service = Service(records)
    target = FilmOptimizationObjective(low, high, samples, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=None, upper_bound=upper))
    request = FilmSynthesisRequest(environment, (target,),
        tuple(FilmNeedleMaterialReference(m, 'nk') for m in materials),
        # Keep analytical needle/growth counter contracts on the explicit
        # v23 route. GUI/default-cluster behavior has separate durable tests.
        max_cycles=4, max_evaluations=300, ar_structure_mode="v23_matching", search_strategy="legacy")
    return service, request


def test_bare_substrate_meeting_target_returns_zero_layers_without_cycle():
    service, request = setup_problem(upper=.05)
    original = deepcopy(dict(request.environment))
    result = synthesize_film_design(service, request)
    assert result.state == 'completed' and result.targets_met
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == []
    assert result.cycles == 0 and result.evaluations == 2
    assert result.validation_complete and result.validation_evaluations == 1
    assert request.environment == original
    assert result.training_score is not None
    assert result.training_score['errors'][0].actual == pytest.approx(.04)


def test_single_layer_known_quarter_wave_solution_and_broadband_ar():
    service, request = setup_problem(upper=.001, materials=('low',))
    result = synthesize_film_design(service, request)
    assert result.state == 'completed', result.termination_reason
    assert result.candidate_design is not None
    assert len(result.candidate_design['front_layers']) == 1
    d = result.candidate_design['front_layers'][0]['thickness']['value']
    assert abs(d - 550 / (4 * np.sqrt(1.5))) < 20
    assert result.validation_score is not None
    assert result.validation_score['max_error'] <= request.acceptable_normalized_error
    # Independent direct physical service, not the synthesis score routine.
    actual = service.freeze_design(result.candidate_design, dict(
        wavelength_min_nm=520, wavelength_max_nm=580, samples=121))
    from coat_sim.core.film_design_service import simulate_frozen
    response = simulate_frozen('.', actual)
    assert max(response['R']) <= .002
    assert len(result.validation_score['errors']) >= 33
    assert result.validation_score['audit']['dense_points'] == 17
    assert any(t.stage == 'needle_probe' and t.sensitivity is not None for t in result.trace)


def test_exact_monochromatic_quarter_wave_and_deterministic_replay():
    service, request = setup_problem(upper=0., low=550, high=550, samples=1, materials=('low',))
    request = replace(request, acceptable_normalized_error=1e-10)
    first = synthesize_film_design(service, request)
    second = synthesize_film_design(service, first.frozen_request)
    # The floating-point residual is positive, so the exact R ≤ 0 target fails.
    assert not first.targets_met and not second.targets_met
    assert first.solver_acceptable is True and second.solver_acceptable is True
    assert first.validation_score is not None
    assert first.validation_score['errors'][0].actual > 0.
    assert first.candidate_design == second.candidate_design
    assert first.candidate_design is not None
    thickness = first.candidate_design['front_layers'][0]['thickness']['value']
    assert thickness == pytest.approx(550 / (4 * np.sqrt(1.5)), abs=1e-10)
    assert first.validation_score is not None
    assert first.validation_score['max_error'] < 1e-20


def test_multilayer_reflector_is_synthesized_without_starting_design(monkeypatch):
    # Keep this regression focused on the established multi-cycle needle route;
    # the GUI-to-job test covers the separate automatic structural-prior route.
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', lambda *args, **kwargs: iter(()))
    service, request = setup_problem(materials=('low', 'high'), low=540, high=560)
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=None, lower_bound=.9)),),
                      max_cycles=8, max_evaluations=1800, position_samples=1)
    result = synthesize_film_design(service, request)
    assert result.training_score is not None
    assert result.state == 'completed', (result.termination_reason, result.training_score['max_error'])
    assert result.candidate_design is not None
    assert len(result.candidate_design['front_layers']) >= 3
    assert result.validation_score is not None
    assert result.validation_score['max_error'] <= .001
    assert any(t.stage == 'needle_accepted' for t in result.trace)
    assert result.cycles > 1


def test_gradual_growth_keeps_worse_working_branch_and_separate_bare_incumbent():
    service, request = setup_problem(upper=0., low=550, high=550, samples=1, materials=('high',))
    request = replace(request, max_cycles=1, growth_quarter_wave_fractions=(1.,))
    result = synthesize_film_design(service, request)
    assert result.training_score is not None
    baseline = result.training_score['max_error']
    restarts = [t for t in result.trace if t.stage == 'growth_restart']
    assert restarts and restarts[0].max_normalized_error > baseline
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == []
    assert result.state == 'not_found' and not result.targets_met
    assert result.termination_reason == 'cycle_limit'


def test_manufacturing_minimum_is_explicit_and_no_implicit_work_budgets():
    service, request = setup_problem(materials=('low',))
    unrestricted = FilmSynthesisRequest(request.environment, request.objectives, request.candidate_materials)
    assert unrestricted.minimum_nonzero_thickness_nm is None
    assert unrestricted.max_cycles is None and unrestricted.max_evaluations is None
    assert unrestricted.max_layers is None and unrestricted.max_total_thickness_nm is None
    result = synthesize_film_design(service, replace(request, minimum_nonzero_thickness_nm=100,
                                                      max_layer_thickness_nm=130, max_layers=1))
    assert result.candidate_design is not None
    assert all(100 <= r['thickness']['value'] <= 130 for r in result.candidate_design['front_layers'])


def test_budget_reserves_independent_validation_and_returns_not_found():
    service, request = setup_problem(upper=0)
    result = synthesize_film_design(service, replace(request, max_evaluations=2))
    assert result.evaluations == 2 and result.validation_complete
    assert result.state == 'not_found' and result.termination_reason == 'evaluation_limit'
    assert not result.targets_met and '尚未找到' in result.message


def test_budget_keeps_already_scored_feasible_growth_candidate():
    service, request = setup_problem(upper=.001, low=550, high=550, samples=1, materials=('low',))
    result = synthesize_film_design(service, replace(request, max_evaluations=4,
        growth_quarter_wave_fractions=(1.,)))
    assert result.evaluations == 4
    assert result.state == 'completed' and result.targets_met
    assert result.candidate_design is not None
    assert len(result.candidate_design['front_layers']) == 1


def test_cancel_after_validation_before_commit_and_terminal_notification_boundary():
    service, request = setup_problem(upper=.05)
    event = Event()
    result = synthesize_film_design(service, request, cancel_event=event,
        progress_callback=lambda p: event.set() if p.stage == 'validation_complete' else None)
    assert result.state == 'cancelled' and not result.targets_met
    assert result.validation_complete
    event = Event()
    result = synthesize_film_design(service, request, cancel_event=event,
        progress_callback=lambda p: event.set() if p.stage == 'completed' else None)
    assert result.state == 'completed' and result.targets_met  # committed notification


def test_cancellation_in_progress_retains_only_complete_scores():
    service, request = setup_problem()
    event = Event()
    def progress(p):
        if p.stage == 'incumbent':
            event.set()
    result = synthesize_film_design(service, request, cancel_event=event, progress_callback=progress)
    assert result.state == 'cancelled' and not result.validation_complete
    assert result.candidate_design is not None
    assert result.evaluations == 1 and result.candidate_design['front_layers'] == []


def test_validation_failure_cannot_be_reported_as_success(monkeypatch):
    service, request = setup_problem(upper=.05)
    from coat_sim.core import film_synthesis_audit as audit
    original = audit.audit_frozen_synthesis_candidate
    def mismatch(*args, **kwargs):
        value = original(*args, **kwargs)
        value['score']['max_error'] = .2
        return value
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', mismatch)
    result = synthesize_film_design(service, request)
    assert result.state == 'not_found' and not result.targets_met
    assert result.termination_reason == 'validation_targets_not_met'


def test_real_between_node_violation_is_caught_on_independent_dense_grid(monkeypatch):
    # This regression exercises continued needle/growth after a failed audit.
    # Default matching-family recovery has its own GUI-to-executor regression.
    from coat_sim.core import film_synthesis_matching as matching
    monkeypatch.setattr(matching, 'iter_matching_proposals', lambda *args, **kwargs: iter(()))
    service, request = setup_problem(upper=.05, low=450, high=700, samples=2)
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.5, 3., 1.5]
    # Preserve the original leakage guarantee on the original bare candidate.
    # The new core may continue after finding it and return a better audited stack.
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    frozen = freeze_film_synthesis_request(service, request)
    initial_audit = audit_frozen_synthesis_candidate(frozen, frozen.environment)
    assert initial_audit['score']['max_error'] > .15
    result = synthesize_film_design(service, request)
    assert result.training_score is not None
    assert result.validation_score is not None
    assert request.acceptable_normalized_error < result.validation_score['max_error'] <= initial_audit['score']['max_error']
    assert result.cycles > 0  # Independent-audit failure must not stop at training pass.
    assert result.state == 'not_found' and not result.targets_met
    assert len(result.validation_score['errors']) >= 9
    assert result.validation_score['audit']['dense_points'] == 5


def test_validation_exception_and_cancellation_are_not_success(monkeypatch):
    service, request = setup_problem(upper=.05)
    from coat_sim.core import film_synthesis_audit as audit
    original = audit.audit_frozen_synthesis_candidate
    def broken(*args, **kwargs):
        raise ValueError('injected physical validation error')
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', broken)
    result = synthesize_film_design(service, request)
    assert result.state == 'failed' and not result.validation_complete and not result.targets_met
    assert result.candidate_design is not None
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', original)
    event = Event()
    result = synthesize_film_design(service, request, cancel_event=event,
        progress_callback=lambda p: event.set() if p.stage == 'validation' else None)
    assert result.state == 'cancelled' and not result.targets_met


def test_lossy_dispersive_candidate_matches_physical_service():
    service, request = setup_problem(materials=('low',), upper=.01)
    service.materials.records['low']['optical_sets'][0].update(n=[1.2, 1.3, 1.4], k=[.02, .03, .04])
    result = synthesize_film_design(service, request)
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers']
    frozen = service.freeze_design(result.candidate_design, dict(
        wavelength_min_nm=520., wavelength_max_nm=580., samples=5))
    from coat_sim.core.film_design_service import simulate_frozen
    direct = simulate_frozen('.', frozen)
    assert result.training_score is not None
    np.testing.assert_allclose(direct['R'], [r.actual for r in result.training_score['errors']], atol=2e-14)
    assert max(direct['A']) > 0


def test_analytic_needle_probe_agrees_with_one_sided_physics_and_splits_host():
    service, request = setup_problem()
    request = freeze_film_synthesis_request(service, request)
    assert request.frozen_material_snapshot is not None
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [dict(id='host', material_id='low', optical_set_id='nk',
        thickness={'value': 90., 'unit': 'nm'}, note='')]
    from coat_sim.core.film_optimization_structure import _site_records, _insert_at_site
    site = next(s for s in _site_records(design, request.frozen_material_snapshot, 1) if s['kind'] == 'interior')
    ref = FilmNeedleMaterialReference('high', 'nk')
    zero = _insert_at_site(design, site, ref, 0, 'needle', 'right', zero=True)
    finite = _insert_at_site(design, site, ref, 1e-5, 'needle', 'right')
    proxy = synthesis._proxy(request)
    probe = synthesis.evaluate_frozen_structure_candidate(proxy, zero, variable_ids=('needle',),
        with_jacobian=True, zero_thickness_layer_ids=('needle',))
    baseline = synthesis.evaluate_frozen_structure_candidate(proxy, design)
    next_value = synthesis.evaluate_frozen_structure_candidate(proxy, finite)
    def loss(value):
        # This fixture has one pointwise band and one angle: normalized band,
        # angle and mode masses are one. Integrate the physical squared hinge
        # independently; endpoints carry half of an interior sample's mass.
        assert len(request.objectives) == 1
        objective = request.objectives[0]
        assert objective.angles_deg == (0.,)
        assert objective.pointwise.enabled and not objective.mean.enabled
        assert objective.pointwise.upper_bound is not None
        response, = value['responses']
        waves = np.asarray(response['wavelength_nm'], float)
        actual = np.asarray(response['actual_values'], float)
        squared = (np.maximum(actual - objective.pointwise.upper_bound, 0.)
                   / objective.normalization_scale) ** 2
        return float(np.trapezoid(squared, waves) / (waves[-1] - waves[0]))
    assert probe['gradient_by_layer_id']['needle'] == pytest.approx(
        (loss(next_value) - loss(baseline)) / 1e-5, rel=2e-4, abs=2e-8)
    assert zero['front_layers'][0]['thickness']['value'] + zero['front_layers'][2]['thickness']['value'] == 90


def test_redundant_halfwave_is_deleted_after_full_physical_rescoring(monkeypatch):
    service, request = setup_problem(upper=.001, low=550, high=550, samples=1)
    request = replace(request, growth_quarter_wave_fractions=(1.,), max_evaluations=30)
    original = synthesis._insert_at_site
    def with_redundant_halfwave(*args, **kwargs):
        candidate = original(*args, **kwargs)
        if not kwargs.get('zero') and args[4].startswith('growth-'):
            candidate['front_layers'].append(dict(id='redundant-halfwave', material_id='high',
                optical_set_id='nk', thickness={'value': 550 / (2 * 2.2), 'unit': 'nm'}, note='test'))
        return candidate
    monkeypatch.setattr(synthesis, '_insert_at_site', with_redundant_halfwave)
    result = synthesize_film_design(service, request)
    assert result.targets_met
    assert result.candidate_design is not None
    assert len(result.candidate_design['front_layers']) == 1
    assert any(t.stage == 'deletion' and t.layer_count == 1 for t in result.trace)


def test_material_snapshot_replay_preserves_dispersive_k_and_mixed_target_semantics():
    service, request = setup_problem(upper=.1)
    service.materials.records['low']['optical_sets'][0].update(n=[1.4, 1.5, 1.6], k=[.01, .02, .03])
    target = FilmOptimizationObjective(500, 650, 4, (0., 35.), 's', 'T', pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=.1, upper_bound=1.))
    request = replace(request, objectives=(request.objectives[0], target))
    frozen = freeze_film_synthesis_request(service, request)
    service.materials.records.clear()
    result = synthesize_film_design(service, frozen)
    assert result.state == 'completed'
    assert result.frozen_request.frozen_material_snapshot is not None
    low = next(r for r in result.frozen_request.frozen_material_snapshot['materials'] if r['id'] == 'low')
    assert low['optical_sets'][0]['k'] == [.01, .02, .03]
    assert result.validation_score is not None
    rows = result.validation_score['errors']
    assert {r.angle_deg for r in rows if r.objective_index == 1} == {0., 35.}
    assert all(r.polarization == 's' and r.metric == 'T' for r in rows if r.objective_index == 1)


def test_pinned_real_mgf2_silica_data_can_synthesize_with_nonzero_source_k():
    from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
    store = RefractiveIndexLocalStore(packaged_store_root())
    records = []
    for material in ('SiO2', 'MgF2'):
        entries = store.search(material + ' Rodriguez', limit=10)['entries']
        row = next(r for r in entries if r['source_path'] == f'main/{material}/nk/Rodriguez-de Marcos.yml')
        optical = store.convert_entry(row['entry_id'], optical_set_id='nk', name=material)['optical_set']
        records.append(dict(id=material, name=material, design_ready=True, optical_sets=[optical]))
    service = Service(records)
    environment = new_design()
    environment['substrate'].update(material_id='SiO2', optical_set_id='nk')
    request = FilmSynthesisRequest(environment,
        (FilmOptimizationObjective(520, 580, 5, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=None, upper_bound=.026)),),
        (FilmNeedleMaterialReference('MgF2', 'nk'),), max_cycles=2, max_evaluations=300,
        acceptable_normalized_error=1e-4)
    result = synthesize_film_design(service, request)
    assert result.state == 'completed', result.termination_reason
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers']
    assert all(r['material_id'] == 'MgF2' for r in result.candidate_design['front_layers'])
    assert result.validation_score is not None
    assert max(r.actual for r in result.validation_score['errors']) <= .0261
    frozen = result.frozen_request.frozen_material_snapshot
    assert frozen is not None
    mgf = next(r for r in frozen['materials'] if r['id'] == 'MgF2')['optical_sets'][0]
    assert max(mgf['k']) > 0 and len(set(mgf['n'])) > 1
    assert '10.1364/OME.7.000989' in mgf['source']


@pytest.mark.parametrize('bad', ['nonempty', 'use_k', 'unknown_k', 'domain'])
def test_rejects_invented_optics_and_initial_design(bad):
    service, request = setup_problem()
    if bad == 'nonempty':
        environment = deepcopy(dict(request.environment))
        environment['front_layers'] = [dict(id='existing', material_id='low', optical_set_id='nk',
            thickness={'value': 100., 'unit': 'nm'}, note='')]
        request = replace(request, environment=environment)
    elif bad == 'use_k':
        request = replace(request, environment={**request.environment, 'use_k': False})
    elif bad == 'unknown_k':
        service.materials.records['high']['optical_sets'][0].pop('k')
    else:
        request = replace(request, objectives=(replace(request.objectives[0], wavelength_max_nm=800),))
    with pytest.raises(ValueError):
        freeze_film_synthesis_request(service, request)
