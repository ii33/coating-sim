from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
from threading import Event
from typing import Any

import pytest

from coat_sim import film_classic_local_cli as cli
from coat_sim.core import film_classic_local as classics
from coat_sim.core.film_classic_local import (
    FilmClassicLocalRequest, classic_seed_count, film_classic_local_request_from_payload,
    film_classic_local_request_to_payload, freeze_film_classic_local_request,
    iter_classic_seed_specs, optimize_film_design_from_classics,
)
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def problem(*, templates=('ar_single_qw',), waves=(480., 550.), pairs=(), **options):
    service, shared = setup_problem(**options)
    shared = replace(shared, max_cycles=None, max_evaluations=None)
    return service, FilmClassicLocalRequest(shared, waves, pairs, templates)


def test_explicit_full_catalog_role_product_and_pair_counts_without_truncation():
    _, request = problem(templates=None, pairs=(1, 3))
    specs = list(iter_classic_seed_specs(request))
    assert classic_seed_count(request) == len(specs) == 76
    assert {s.template_id for s in specs} == {r['stable_id'] for r in classics.template_catalog()}
    assert {s.pairs for s in specs if s.template_id == 'hr_hl_qw'} == {1, 3}
    assert len({s.candidate_id for s in specs}) == 76
    assert specs == list(iter_classic_seed_specs(request))
    cavities = [s for s in specs if s.template_id == 'fp_halfwave_defect']
    assert any(dict(s.role_bindings)['C'] != dict(s.role_bindings)['H'] for s in cavities)
    with pytest.raises(ValueError, match='explicit pair_counts'):
        classic_seed_count(replace(request, pair_counts=()))


def test_every_requested_seed_returned_with_per_objective_results_and_applicability():
    service, request = problem(templates=None, waves=(550.,), pairs=(1,), upper=1.)
    second = FilmOptimizationObjective(520, 580, 3, (0., 35.), 'p', 'T', 'range', lower_bound=0.)
    request = replace(request, shared_request=replace(request.shared_request,
        objectives=(*request.shared_request.objectives, second)))
    original = deepcopy(dict(request.shared_request.environment))
    result = optimize_film_design_from_classics(service, request)
    assert result.state == 'completed' and result.coverage_complete
    assert result.planned == result.attempted == 26
    assert request.shared_request.environment == original
    valid = [c for c in result.candidates if c.state == 'completed']
    rejected = [c for c in result.candidates if c.state == 'inapplicable']
    assert len(valid) == 17 and len(rejected) == 9
    assert all(c.validation_complete and len(c.objectives) == 2 for c in valid)
    assert all(all(o.training_met and o.validation_met for o in c.objectives) for c in valid)
    assert all(c.applicability and c.limitations and c.message for c in result.candidates)
    assert all(c.training_score is None and not c.targets_met for c in rejected)
    assert result.evaluations == 2 * len(valid)


def test_off_center_classic_seed_improves_locally_and_keeps_baseline():
    service, request = problem(upper=0., low=550, high=550, samples=1, materials=('low',))
    request = replace(request, shared_request=replace(request.shared_request,
        acceptable_normalized_error=1e-8, max_evaluations=200))
    result = optimize_film_design_from_classics(service, request)
    assert result.state == 'completed'
    first, exact = result.candidates
    assert first.seed_score is not None and first.training_score is not None
    assert first.training_score['max_error'] < first.seed_score['max_error'] / 1000
    assert first.local_iterations > 0 and first.validation_complete and first.targets_met
    assert exact.local_iterations == 0 and exact.targets_met
    assert first.seed_design is not None and first.candidate_design is not None
    assert first.seed_design['front_layers'][0]['thickness']['value'] != first.candidate_design['front_layers'][0]['thickness']['value']
    assert first.candidate_design['incident_medium'] == request.shared_request.environment['incident_medium']
    assert len(result.ranked_candidate_ids) == 2


def test_explicit_reflector_pair_range_against_known_quarter_wave_baseline():
    service, request = problem(templates=('hr_hl_qw',), waves=(550.,), pairs=(1, 2, 3))
    target = replace(request.shared_request.objectives[0], wavelength_min_nm=540., wavelength_max_nm=560.,
        lower_bound=.9, upper_bound=None)
    request = replace(request, shared_request=replace(request.shared_request, objectives=(target,)), max_local_iterations=3)
    result = optimize_film_design_from_classics(service, request)
    assert result.coverage_complete and result.planned == 12
    matched = [c for c in result.candidates if c.spec.pairs == 3 and c.targets_met]
    assert len(matched) == 1
    candidate = matched[0]
    assert candidate.validation_score is not None and candidate.candidate_design is not None
    assert len(candidate.candidate_design['front_layers']) == 6
    assert min(e.actual for e in candidate.validation_score['errors']) > .92
    assert candidate.local_iterations == 0  # actual classical baseline already passes


def test_whole_run_budget_returns_validated_subset_with_honest_coverage():
    service, request = problem(upper=1., materials=('low',))
    request = replace(request, shared_request=replace(request.shared_request, max_evaluations=2))
    result = optimize_film_design_from_classics(service, request)
    assert result.state == 'partial' and result.termination_reason == 'evaluation_limit'
    assert result.evaluations == 2 and result.validation_evaluations == 1
    assert result.attempted == 1 and result.planned == 2 and not result.coverage_complete
    assert result.candidates[0].targets_met and result.any_targets_met


def test_local_iteration_limit_is_explicit_and_still_validates_candidate():
    service, request = problem(upper=0., low=550, high=550, samples=1, materials=('low',), waves=(480.,))
    request = replace(request, max_local_iterations=1,
        shared_request=replace(request.shared_request, acceptable_normalized_error=1e-12))
    result = optimize_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.local_iterations == 1 and candidate.validation_complete
    assert candidate.termination_reason == 'local_iteration_limit'
    assert not candidate.targets_met and candidate.state == 'not_found'
    assert result.coverage_complete


@pytest.mark.parametrize('stage', ['freezing', 'generating', 'seed_scored', 'refinement', 'validation', 'validation_complete'])
def test_cancel_is_observable_and_does_not_promote_partial_candidate(stage):
    service, request = problem(upper=0., low=550, high=550, samples=1, materials=('low',))
    event = Event()
    seen = []
    def progress(p):
        seen.append(p.stage)
        if p.stage == stage:
            event.set()
    result = optimize_film_design_from_classics(service, request, cancel_event=event, progress_callback=progress)
    assert stage in seen
    assert result.state == 'cancelled'
    assert all(not c.targets_met for c in result.candidates)
    assert result.attempted < result.planned


def test_absorbing_template_material_is_reported_inapplicable_without_zeroing_k():
    service, request = problem(waves=(550.,), upper=1.)
    service.materials.records['high']['optical_sets'][0]['k'] = [.02] * 3
    result = optimize_film_design_from_classics(service, request)
    assert result.coverage_complete and result.any_targets_met
    rejected = next(c for c in result.candidates if c.spec.role_bindings[0][1].material_id == 'high')
    assert rejected.state == 'inapplicable' and '阈值' in rejected.message
    frozen = result.frozen_request.shared_request.frozen_material_snapshot
    assert frozen is not None
    assert next(r for r in frozen['materials'] if r['id'] == 'high')['optical_sets'][0]['k'] == [.02] * 3


def test_between_training_nodes_violation_fails_independent_validation():
    service, request = problem(waves=(450.,), upper=.05, low=450, high=700, samples=2, materials=('low',))
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.5, 3., 1.5]
    result = optimize_film_design_from_classics(service, request)
    candidate = result.candidates[0]
    assert candidate.training_score is not None and candidate.validation_score is not None
    assert candidate.training_score['max_error'] == 0
    assert candidate.validation_score['max_error'] > 0
    assert candidate.state == 'not_found' and not candidate.targets_met
    assert candidate.objectives[0].training_met and not candidate.objectives[0].validation_met


def test_constraint_inapplicability_is_explicit_and_no_default_work_cap():
    service, request = problem(waves=(550.,), upper=1.)
    assert request.shared_request.max_evaluations is None and request.max_local_iterations is None
    request = replace(request, shared_request=replace(request.shared_request, minimum_nonzero_thickness_nm=100.))
    result = optimize_film_design_from_classics(service, request)
    assert any(c.state == 'completed' for c in result.candidates)
    assert any(c.state == 'inapplicable' and '制造约束' in c.message for c in result.candidates)


def test_frozen_replay_and_strict_serialization_retain_candidate_identity():
    service, request = problem(waves=(550.,), upper=1.)
    frozen = freeze_film_classic_local_request(service, request)
    payload = json.loads(json.dumps(film_classic_local_request_to_payload(frozen)))
    replay = film_classic_local_request_from_payload(payload)
    service.materials.records.clear()
    first = optimize_film_design_from_classics(None, replay)
    second = optimize_film_design_from_classics(None, replay)
    assert first.candidates == second.candidates
    assert first.ranked_candidate_ids == second.ranked_candidate_ids
    payload['ignored_setting'] = True
    with pytest.raises(ValueError):
        film_classic_local_request_from_payload(payload)


def test_cli_frozen_request_progress_new_result_and_overwrite_refusal(tmp_path, capsys):
    service, request = problem(waves=(550.,), upper=1.)
    request = freeze_film_classic_local_request(service, request)
    source, output = tmp_path / 'request.json', tmp_path / 'result.json'
    source.write_text(json.dumps(film_classic_local_request_to_payload(request)), encoding='utf-8')
    assert cli.main(['--request', str(source), '--output', str(output)]) == 0
    capture = capsys.readouterr()
    assert all('stage' in json.loads(line) for line in capture.err.splitlines())
    result = json.loads(output.read_text(encoding='utf-8'))['result']
    assert result['coverage_complete'] and len(result['candidates']) == 2
    before = output.read_bytes()
    assert cli.main(['--request', str(source), '--output', str(output)]) == 2
    assert output.read_bytes() == before


def test_cli_refuses_live_catalog_and_bad_input(tmp_path, capsys):
    _, request = problem(waves=(550.,), upper=1.)
    source = tmp_path / 'request.json'
    source.write_text(json.dumps(film_classic_local_request_to_payload(request)), encoding='utf-8')
    assert cli.main(['--request', str(source)]) == 2
    assert 'frozen snapshot' in capsys.readouterr().out


def test_validation_error_preserves_training_candidate_and_continues(monkeypatch):
    service, request = problem(upper=1., materials=('low',))
    original = classics.evaluate_frozen_structure_candidate
    def broken(*args, **kwargs):
        if kwargs.get('derived_validation_grid'):
            raise ValueError('injected holdout error')
        return original(*args, **kwargs)
    monkeypatch.setattr(classics, 'evaluate_frozen_structure_candidate', broken)
    result = optimize_film_design_from_classics(service, request)
    assert result.coverage_complete and not result.any_targets_met
    assert result.state == 'failed' and result.termination_reason == 'all_applicable_candidates_failed'
    assert all(c.state == 'failed' and c.candidate_design is not None for c in result.candidates)
    assert all(not c.validation_complete and not c.targets_met for c in result.candidates)


def test_payload_contains_no_nonfinite_or_unserializable_values():
    service, request = problem(waves=(550.,), upper=1.)
    result = optimize_film_design_from_classics(service, request)
    encoded = json.dumps(asdict(result), allow_nan=False)
    assert len(json.loads(encoded)['candidates']) == 2


@pytest.mark.parametrize('incident', [dict(kind='constant', n=1.2),
    dict(kind='material', material_id='low', optical_set_id='nk')])
def test_declared_constant_or_material_incident_port_is_retained(incident):
    service, request = problem(waves=(550.,), upper=1.)
    environment = deepcopy(dict(request.shared_request.environment))
    environment['incident_medium'] = incident
    request = replace(request, shared_request=replace(request.shared_request, environment=environment))
    result = optimize_film_design_from_classics(service, request)
    assert result.any_targets_met
    for candidate in result.candidates:
        assert candidate.candidate_design is not None
        assert candidate.candidate_design['incident_medium'] == incident


def test_pinned_real_transparent_franta_datasets_remain_dispersive_with_original_k():
    from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
    from tests.test_film_auto_synthesis import Service
    store = RefractiveIndexLocalStore(packaged_store_root())
    records: list[dict[str, Any]] = []
    for material in ('SiO2', 'MgF2'):
        row = next(r for r in store.search(material, limit=100)['entries']
            if r['source_path'] == f'main/{material}/nk/Franta.yml')
        optical = store.convert_entry(row['entry_id'], optical_set_id='nk', name=material)['optical_set']
        records.append(dict(id=material, name=material, design_ready=True, optical_sets=[optical]))
    service, request = problem(waves=(500., 550.), upper=.02, materials=('low',))
    environment = deepcopy(dict(request.shared_request.environment))
    environment['substrate'].update(material_id='SiO2', optical_set_id='nk')
    request = replace(request, shared_request=replace(request.shared_request, environment=environment,
        candidate_materials=(FilmNeedleMaterialReference('MgF2', 'nk'),), acceptable_normalized_error=0.))
    result = optimize_film_design_from_classics(Service(records), request)
    assert result.coverage_complete and all(c.targets_met for c in result.candidates)
    for candidate in result.candidates:
        assert candidate.validation_score is not None
        assert max(e.actual for e in candidate.validation_score['errors']) <= .02
    snapshot = result.frozen_request.shared_request.frozen_material_snapshot
    assert snapshot is not None
    original = records[1]['optical_sets'][0]
    frozen = next(r for r in snapshot['materials'] if r['id'] == 'MgF2')['optical_sets'][0]
    assert frozen == original and len(set(frozen['n'])) > 1 and max(frozen['k']) > 0
