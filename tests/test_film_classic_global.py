from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
from threading import Event

import pytest

from coat_sim import film_classic_global_cli as cli
from coat_sim.core import film_classic_global as global_search
from coat_sim.core.film_classic_global import (
    FilmClassicGlobalRequest, FilmGlobalThicknessBounds, film_classic_global_request_from_payload,
    film_classic_global_request_to_payload, freeze_film_classic_global_request, search_film_design_from_classics,
)
from coat_sim.core.film_classic_local import classic_seed_count, optimize_film_design_from_classics
from tests.test_film_classic_local import problem

pytestmark = pytest.mark.algorithm


def setup(**kwargs):
    service, enumeration = problem(**kwargs)
    return service, FilmClassicGlobalRequest(enumeration,
        FilmGlobalThicknessBounds(0., 300., 'Explicit synthetic comparison search interval: 0..300 nm'), seed=17)


def test_explicit_bounds_seed_and_no_implicit_iteration_or_evaluation_cap():
    _, request = setup()
    assert request.max_generations is None
    assert request.enumeration.shared_request.max_evaluations is None
    for bounds in (FilmGlobalThicknessBounds(0., float('inf'), 'x'),
        FilmGlobalThicknessBounds(0., 300., ''), FilmGlobalThicknessBounds(300., 0., 'x')):
        with pytest.raises(ValueError):
            film_classic_global_request_to_payload(replace(request, thickness_bounds=bounds))
    for options in (dict(seed=True), dict(seed=-1), dict(population_size=4),
        dict(max_generations=0), dict(stagnation_generations=0),
        dict(enumeration=replace(request.enumeration, max_local_iterations=3))):
        with pytest.raises(ValueError):
            film_classic_global_request_to_payload(replace(request, **options))


def test_all_current_catalog_candidates_and_fixed_topology_remain_extension_driven():
    service, request = setup(templates=None, pairs=(1,), waves=(550.,), upper=1.)
    before = deepcopy(dict(request.enumeration.shared_request.environment))
    result = search_film_design_from_classics(service, request)
    assert result.state == 'completed' and result.coverage_complete
    assert classic_seed_count(request.enumeration) == result.planned == result.attempted == 26
    assert len([c for c in result.candidates if c.targets_met]) == 17
    assert request.enumeration.shared_request.environment == before
    for candidate in result.candidates:
        if candidate.seed_design is not None and candidate.candidate_design is not None:
            initial = candidate.seed_design['front_layers']
            final = candidate.candidate_design['front_layers']
            assert [(r['id'], r['material_id'], r['optical_set_id']) for r in initial] == [
                (r['id'], r['material_id'], r['optical_set_id']) for r in final]
    assert result.evaluations == 34


def test_off_center_known_ar_is_improved_by_real_de_and_replays_exactly():
    service, request = setup(waves=(480.,), upper=0., low=550., high=550., samples=1, materials=('low',))
    shared = replace(request.enumeration.shared_request, acceptable_normalized_error=1e-8, max_evaluations=200)
    request = replace(request, enumeration=replace(request.enumeration, shared_request=shared))
    frozen = freeze_film_classic_global_request(service, request)
    result = search_film_design_from_classics(None, frozen)
    replay = search_film_design_from_classics(None, film_classic_global_request_from_payload(
        json.loads(json.dumps(film_classic_global_request_to_payload(frozen)))))
    assert result == replay and result.any_targets_met
    candidate = result.candidates[0]
    assert candidate.seed_score is not None and candidate.training_score is not None
    assert candidate.training_score['max_error'] < candidate.seed_score['max_error'] / 10000
    assert candidate.proposals > 5 and candidate.generations > 0
    assert candidate.validation_complete and candidate.targets_met
    assert candidate.seed_design is not None and candidate.candidate_design is not None
    assert candidate.seed_design['front_layers'][0]['thickness'] != candidate.candidate_design['front_layers'][0]['thickness']
    assert json.dumps(asdict(result), allow_nan=False)


def test_same_bounds_and_budget_fair_local_global_comparison():
    service, request = setup(waves=(480.,), upper=0., low=550., high=550., samples=1, materials=('low',))
    shared = replace(request.enumeration.shared_request, acceptable_normalized_error=1e-8,
        max_evaluations=80, max_layer_thickness_nm=300.)
    local_request = replace(request.enumeration, shared_request=shared)
    frozen = freeze_film_classic_global_request(service, replace(request, enumeration=local_request))
    local = optimize_film_design_from_classics(None, frozen.enumeration)
    global_result = search_film_design_from_classics(None, frozen)
    assert local.any_targets_met and not global_result.any_targets_met
    assert global_result.termination_reason == 'evaluation_limit'
    a, b = local.candidates[0], global_result.candidates[0]
    assert a.seed_design == b.seed_design and a.seed_score == b.seed_score
    assert local.evaluations <= 80 and global_result.evaluations <= 80
    assert a.validation_complete and b.validation_complete
    assert b.effective_bounds.lower_nm == 0 and b.effective_bounds.upper_nm == 300


@pytest.mark.parametrize('stage', ['freezing', 'generating', 'seed_scored', 'global_proposal',
    'global_generation', 'validation', 'validation_complete'])
def test_cancellation_boundaries_do_not_promote_partial_results(stage):
    service, request = setup(waves=(480., 550.), upper=0., low=550., high=550., samples=1, materials=('low',))
    request = replace(request, enumeration=replace(request.enumeration,
        shared_request=replace(request.enumeration.shared_request, acceptable_normalized_error=1e-12)))
    event, seen = Event(), []
    def progress(p):
        seen.append(p.stage)
        if p.stage == stage:
            event.set()
    result = search_film_design_from_classics(service, request, cancel_event=event, progress_callback=progress)
    assert stage in seen and result.state == 'cancelled'
    assert not any(c.targets_met for c in result.candidates)
    assert result.attempted < result.planned


def test_cancel_after_committed_candidate_retains_its_success():
    service, request = setup(waves=(550., 480.), upper=1., materials=('low',))
    event = Event()
    def progress(p):
        if p.stage == 'candidate_complete':
            event.set()
    result = search_film_design_from_classics(service, request, cancel_event=event, progress_callback=progress)
    assert result.state == 'cancelled' and not result.coverage_complete
    assert result.any_targets_met and result.candidates[0].targets_met


def test_budget_reserves_dense_validation_and_preserves_seed():
    service, request = setup(waves=(480., 550.), upper=0., low=550., high=550., samples=1, materials=('low',))
    request = replace(request, enumeration=replace(request.enumeration, shared_request=replace(
        request.enumeration.shared_request, max_evaluations=5, acceptable_normalized_error=1e-12)))
    result = search_film_design_from_classics(service, request)
    assert result.state == 'partial' and result.termination_reason == 'evaluation_limit'
    assert result.evaluations == 5 and result.validation_evaluations == 1
    assert result.attempted == 1 and result.candidates[0].candidate_design is not None
    assert result.candidates[0].validation_complete


def test_generation_limit_still_independently_validates_best():
    service, request = setup(waves=(480.,), upper=0., low=550., high=550., samples=1, materials=('low',))
    request = replace(request, max_generations=1, enumeration=replace(request.enumeration,
        shared_request=replace(request.enumeration.shared_request, acceptable_normalized_error=0.)))
    result = search_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.generations == 1 and candidate.termination_reason == 'generation_limit'
    assert candidate.validation_complete and not candidate.targets_met and result.state == 'not_found'


def test_stagnation_is_detected_without_a_generation_budget():
    service, request = setup(waves=(550.,), upper=0., low=550., high=550., samples=1, materials=('high',))
    request = replace(request, thickness_bounds=FilmGlobalThicknessBounds(62., 63., 'Explicit narrow no-solution interval'),
        stagnation_generations=2, convergence_relative_tolerance=0., enumeration=replace(request.enumeration,
        shared_request=replace(request.enumeration.shared_request, acceptable_normalized_error=0.,
            relative_improvement_tolerance=1.)))
    result = search_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.generations == 2 and candidate.termination_reason == 'global_stagnation'
    assert candidate.validation_complete and not result.any_targets_met


def test_out_of_bounds_seed_is_explicit_and_manufacturing_intersection_is_returned():
    service, request = setup(waves=(550.,), upper=1.)
    request = replace(request, enumeration=replace(request.enumeration, shared_request=replace(
        request.enumeration.shared_request, max_layer_thickness_nm=90.)))
    result = search_film_design_from_classics(service, request)
    assert any(c.state == 'inapplicable' for c in result.candidates)
    assert all(c.effective_bounds.upper_nm == 90 and 'intersected' in c.effective_bounds.source for c in result.candidates)
    assert result.evaluations == 2


def test_dense_validation_failure_cannot_be_reported_as_success():
    service, request = setup(waves=(450.,), upper=.05, low=450, high=700, samples=2, materials=('low',))
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.5, 3., 1.5]
    result = search_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.objectives[0].training_met and not candidate.objectives[0].validation_met
    assert candidate.termination_reason == 'validation_targets_not_met'
    assert result.state == 'not_found' and not result.any_targets_met


def test_constraint_holes_and_zero_slots_do_not_change_topology():
    service, request = setup(waves=(480.,), upper=0., low=550., high=550., samples=1, materials=('low',))
    request = replace(request, max_generations=1, enumeration=replace(request.enumeration,
        shared_request=replace(request.enumeration.shared_request,
            acceptable_normalized_error=0., minimum_nonzero_thickness_nm=95., max_total_thickness_nm=120.)))
    result = search_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.rejected_proposals > 0
    assert candidate.seed_design is not None and candidate.candidate_design is not None
    assert len(candidate.seed_design['front_layers']) == len(candidate.candidate_design['front_layers']) == 1
    zero = deepcopy(candidate.seed_design)
    zero['front_layers'][0]['thickness']['value'] = 0.
    assert global_search._feasible(request, zero, request.thickness_bounds)
    zero['front_layers'][0]['thickness']['value'] = 50.
    assert not global_search._feasible(request, zero, request.thickness_bounds)


def test_failed_holdout_retains_candidate_and_reports_failure(monkeypatch):
    service, request = setup(waves=(550.,), upper=1.)
    original = global_search.evaluate_frozen_structure_candidate
    def broken(*args, **kwargs):
        if kwargs.get('derived_validation_grid'):
            raise ValueError('injected holdout error')
        return original(*args, **kwargs)
    monkeypatch.setattr(global_search, 'evaluate_frozen_structure_candidate', broken)
    result = search_film_design_from_classics(service, request)
    assert result.state == 'failed' and result.termination_reason == 'all_applicable_candidates_failed'
    assert all(c.candidate_design is not None and not c.validation_complete and not c.targets_met for c in result.candidates)


def test_cli_strict_frozen_replay_progress_and_overwrite_refusal(tmp_path, capsys):
    service, request = setup(waves=(550.,), upper=1.)
    frozen = freeze_film_classic_global_request(service, request)
    source, output = tmp_path / 'request.json', tmp_path / 'result.json'
    payload = film_classic_global_request_to_payload(frozen)
    source.write_text(json.dumps(payload), encoding='utf-8')
    assert cli.main(['--request', str(source), '--output', str(output)]) == 0
    assert all('stage' in json.loads(line) for line in capsys.readouterr().err.splitlines())
    result = json.loads(output.read_text(encoding='utf-8'))['result']
    assert result['coverage_complete'] and result['any_targets_met']
    before = output.read_bytes()
    assert cli.main(['--request', str(source), '--output', str(output)]) == 2
    assert output.read_bytes() == before
    payload['silently_ignored'] = True
    with pytest.raises(ValueError):
        film_classic_global_request_from_payload(payload)
    payload.pop('silently_ignored')
    payload['thickness_bounds']['silently_ignored'] = True
    with pytest.raises(TypeError):
        film_classic_global_request_from_payload(payload)
    source.write_text(json.dumps(film_classic_global_request_to_payload(request)), encoding='utf-8')
    assert cli.main(['--request', str(source)]) == 2
    assert 'frozen snapshot' in capsys.readouterr().out


def test_disjoint_infrared_windows_use_nm_and_reject_unknown_or_out_of_range_k():
    service, request = setup(waves=(4000.,), upper=1., materials=('low',))
    shared = request.enumeration.shared_request
    first = replace(shared.objectives[0], wavelength_min_nm=3000., wavelength_max_nm=5000., wavelength_samples=3)
    second = replace(first, wavelength_min_nm=8000., wavelength_max_nm=12000., polarization='p', angles_deg=(0., 35.))
    for record in service.materials.records.values():
        optical = record['optical_sets'][0]
        optical.update(wavelength_min=3000., wavelength_max=12000., wavelengths=[3000., 5000., 12000.])
    request = replace(request, thickness_bounds=FilmGlobalThicknessBounds(0., 2000.,
        'Explicit synthetic infrared fixture interval, not manufacturing advice'),
        enumeration=replace(request.enumeration, shared_request=replace(shared, objectives=(first, second))))
    result = search_film_design_from_classics(service, request)
    assert result.any_targets_met and result.candidates[0].validation_score is not None
    errors = result.candidates[0].validation_score['errors']
    assert {e.objective_index for e in errors} == {0, 1}
    assert all(3000 <= e.wavelength_nm <= 5000 or 8000 <= e.wavelength_nm <= 12000 for e in errors)
    assert max(e.wavelength_nm for e in errors) == 12000
    service.materials.records['low']['optical_sets'][0]['wavelength_max'] = 11000.
    service.materials.records['low']['optical_sets'][0]['wavelengths'][-1] = 11000.
    with pytest.raises(ValueError):
        freeze_film_classic_global_request(service, request)
    service.materials.records['low']['optical_sets'][0]['wavelength_max'] = 12000.
    service.materials.records['low']['optical_sets'][0]['wavelengths'][-1] = 12000.
    service.materials.records['low']['optical_sets'][0].pop('k')
    with pytest.raises(ValueError):
        freeze_film_classic_global_request(service, request)


def test_zero_thickness_keeps_slot_and_uses_actual_bare_substrate_physics(monkeypatch):
    from types import SimpleNamespace
    service, request = setup(waves=(550.,), upper=0., low=550., high=550., samples=1, materials=('low',))
    shared = request.enumeration.shared_request
    objective = replace(shared.objectives[0], mode='value', target_value=.04, upper_bound=None)
    request = replace(request, enumeration=replace(request.enumeration, shared_request=replace(
        shared, objectives=(objective,), acceptable_normalized_error=1e-12)))
    class ZeroBackend:
        def __init__(self, objective, bounds, **kwargs):
            self.objective = objective
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def __next__(self):
            import numpy as np
            return self.objective(np.array([0.]))
        def converged(self):
            return False
    monkeypatch.setattr(global_search, 'import_module', lambda _: SimpleNamespace(DifferentialEvolutionSolver=ZeroBackend))
    result = search_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.targets_met and candidate.candidate_design is not None
    assert len(candidate.candidate_design['front_layers']) == 1
    assert candidate.candidate_design['front_layers'][0]['thickness']['value'] == 0.
    assert candidate.validation_score is not None
    assert candidate.validation_score['max_error'] < 1e-12


def test_known_reflector_same_seed_and_budget_has_honest_failed_and_passing_pairs():
    service, request = setup(templates=('hr_hl_qw',), waves=(550.,), pairs=(1, 2, 3))
    shared = request.enumeration.shared_request
    objective = replace(shared.objectives[0], wavelength_min_nm=540., wavelength_max_nm=560.,
        lower_bound=.9, upper_bound=None)
    request = replace(request, max_generations=1, enumeration=replace(request.enumeration,
        shared_request=replace(shared, objectives=(objective,), max_evaluations=150)))
    result = search_film_design_from_classics(service, request)
    assert result.planned == result.attempted == 12 and result.coverage_complete
    passing = [c for c in result.candidates if c.targets_met]
    assert len(passing) == 1 and passing[0].spec.pairs == 3 and passing[0].generations == 0
    assert passing[0].validation_score is not None
    assert min(e.actual for e in passing[0].validation_score['errors']) > .92
    assert all(c.state == 'not_found' for c in result.candidates if c.spec.pairs < 3 and c.state != 'inapplicable')


def test_search_preserves_real_dispersion_and_nonzero_k_in_frozen_materials():
    from typing import Any
    from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from tests.test_film_auto_synthesis import Service
    store = RefractiveIndexLocalStore(packaged_store_root())
    records: list[dict[str, Any]] = []
    for material in ('SiO2', 'MgF2'):
        row = next(r for r in store.search(material, limit=100)['entries']
            if r['source_path'] == f'main/{material}/nk/Franta.yml')
        optical = store.convert_entry(row['entry_id'], optical_set_id='nk', name=material)['optical_set']
        records.append(dict(id=material, name=material, design_ready=True, optical_sets=[optical]))
    _, request = setup(waves=(500., 550.), upper=.02, materials=('low',))
    shared = request.enumeration.shared_request
    environment = deepcopy(dict(shared.environment))
    environment['substrate'].update(material_id='SiO2', optical_set_id='nk')
    request = replace(request, enumeration=replace(request.enumeration, shared_request=replace(
        shared, environment=environment, candidate_materials=(FilmNeedleMaterialReference('MgF2', 'nk'),))))
    result = search_film_design_from_classics(Service(records), request)
    assert result.coverage_complete and result.any_targets_met
    frozen = result.frozen_request.enumeration.shared_request.frozen_material_snapshot
    assert frozen is not None
    optical = next(r for r in frozen['materials'] if r['id'] == 'MgF2')['optical_sets'][0]
    assert optical == records[1]['optical_sets'][0] and len(set(optical['n'])) > 1 and max(optical['k']) > 0


def test_mixed_rt_value_and_range_multiangle_polarization_semantics():
    service, request = setup(waves=(550.,), upper=1., materials=('low',))
    shared = request.enumeration.shared_request
    r = replace(shared.objectives[0], wavelength_min_nm=550., wavelength_max_nm=550., wavelength_samples=1,
        polarization='s', angles_deg=(0., 30.), upper_bound=1., weight=1e-4)
    t = replace(r, metric='T', mode='value', target_value=0., upper_bound=None, polarization='p', weight=1e4)
    request = replace(request, max_generations=1, enumeration=replace(request.enumeration,
        shared_request=replace(shared, objectives=(r, t), acceptable_normalized_error=1e-6)))
    result = search_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.validation_score is not None and not candidate.targets_met
    errors = candidate.validation_score['errors']
    assert {e.metric for e in errors} == {'R', 'T'} and {e.angle_deg for e in errors} == {0., 30.}
    assert candidate.objectives[0].validation_met and not candidate.objectives[1].validation_met


@pytest.mark.parametrize('failed', [False, True])
def test_cancel_after_last_failed_or_inapplicable_commit_preserves_cancel_reason(monkeypatch, failed):
    service, request = setup(waves=(550.,), upper=1., materials=('low',))
    if failed:
        def broken(*args, **kwargs):
            raise ValueError('injected seed failure')
        monkeypatch.setattr(global_search, 'evaluate_frozen_structure_candidate', broken)
    else:
        request = replace(request, thickness_bounds=FilmGlobalThicknessBounds(0., 1., 'Explicit exclusion test'))
    event = Event()
    def progress(p):
        if p.stage == 'candidate_complete':
            event.set()
    result = search_film_design_from_classics(service, request, cancel_event=event, progress_callback=progress)
    assert result.state == result.termination_reason == 'cancelled'
    assert result.coverage_complete and not result.any_targets_met
