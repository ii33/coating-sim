"""Explicit missing-k assumptions preserve real absorption, domains and source data."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from coat_sim.core.film_auto_synthesis import (
    _phase_step_limits_nm, _proxy, freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.film_physical_structures import (
    FilmBraggStackSection, FilmStructureMaterialBinding, FilmStructureSpec,
    generate_physical_structure, structure_spec_to_mapping,
)
from coat_sim.core.film_process_import import _sha256
from coat_sim.core.film_synthesis_absorption import (
    annotate_synthesis_design, synthesis_absorption_assumption,
)
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
    film_synthesis_result_to_payload, prepare_film_synthesis_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, deserialize_job_payload, execute_job, is_synthesis_result, validate_checkpoint,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def opt_in(request):
    environment = deepcopy(dict(request.environment))
    environment['assume_lossless'] = True
    seeds = deepcopy(request.seed_designs)
    for seed in seeds:
        seed['assume_lossless'] = True
    return replace(request, environment=environment, seed_designs=seeds)


def test_unknown_k_explicit_opt_in_grows_audits_and_replays_without_patching_sources():
    service, strict = setup_problem(upper=0., low=550., high=550., samples=1, materials=('low',))
    service.materials.records['low']['optical_sets'][0]['k'] = None
    original = json.dumps(service.materials.records, sort_keys=True)
    with pytest.raises(ValueError, match='unknown k'):
        freeze_film_synthesis_request(service, strict)
    request = opt_in(replace(strict, acceptable_normalized_error=1e-9))
    frozen = freeze_film_synthesis_request(service, request)
    assert _phase_step_limits_nm(frozen)[('low', 'nk')] > 0
    checkpoints = []
    result = synthesize_film_design(None, frozen, snapshot_callback=lambda *items: checkpoints.append(items[0]))
    assert result.state == 'completed' and result.targets_met
    assert result.candidate_design is not None
    assert result.candidate_design['assume_lossless'] is True
    assert result.candidate_design['use_k'] is True
    assert result.candidate_design['front_layers']
    assert '未知 k 按 0' in result.candidate_design['note']
    assert 'low/nk' in result.candidate_design['note']
    assert checkpoints and all('low/nk' in row['note'] for row in checkpoints if row['front_layers'])
    assert result.validation_score is not None
    evidence = result.validation_score['audit']['absorption_assumption']
    assert evidence['policy'] == 'unknown_k_zero'
    assert evidence['affected_materials'] == [
        {'material_id': 'low', 'optical_set_id': 'nk', 'reason': 'no_k_source', 'assumed_k': 0.0}]
    assert evidence['conditional_on_assumption'] and not evidence['measured_absorption_validated']
    payload = film_synthesis_result_to_payload('unknown-policy', result)
    assert payload['absorption_assumption']['used_affected_materials'] == evidence['affected_materials']
    assert payload['review']['warnings']
    assert json.dumps(service.materials.records, sort_keys=True) == original
    assert result.frozen_request.frozen_material_snapshot is not None
    low = next(row for row in result.frozen_request.frozen_material_snapshot['materials'] if row['id'] == 'low')
    assert low['optical_sets'][0]['k'] is None
    replay = synthesize_film_design(None, result.frozen_request)
    assert replay.candidate_design == result.candidate_design


def test_known_nonzero_absorption_still_changes_the_actual_frozen_response():
    service, request = setup_problem(low=550., high=550., samples=1, materials=('low', 'high'))
    service.materials.records['low']['optical_sets'][0]['k'] = None
    service.materials.records['high']['optical_sets'][0]['k'] = [.12] * 3
    frozen = freeze_film_synthesis_request(service, opt_in(request))
    design = deepcopy(dict(frozen.environment))
    design['front_layers'] = [{'id':'absorbing', 'material_id':'high', 'optical_set_id':'nk',
        'thickness':{'value':100., 'unit':'nm'}, 'note':'Known absorbing film'}]
    absorbing = evaluate_frozen_structure_candidate(_proxy(frozen), design)['responses'][0]['actual_values']
    assert frozen.frozen_material_snapshot is not None
    lossless = deepcopy(dict(frozen.frozen_material_snapshot))
    high = next(row for row in lossless['materials'] if row['id'] == 'high')
    high['optical_sets'][0]['k'] = [0.] * 3
    lossless['sha256'] = _sha256(lossless)
    alternate = evaluate_frozen_structure_candidate(
        _proxy(replace(frozen, frozen_material_snapshot=lossless)), design)['responses'][0]['actual_values']
    assert absorbing != pytest.approx(alternate, abs=1e-5)
    assumption = synthesis_absorption_assumption(frozen, design)
    assert assumption['affected_materials'] == []
    assert assumption['known_k_preserved']


@pytest.mark.parametrize('fault', ['n_domain', 'known_k_domain', 'use_k'])
def test_opt_in_never_disables_source_domains_or_global_absorption(fault):
    service, request = setup_problem(materials=('low',))
    request = opt_in(request)
    optical = service.materials.records['low']['optical_sets'][0]
    optical['k'] = None
    if fault == 'n_domain':
        optical['wavelength_min'] = 550.
        optical['wavelengths'] = [550., 600., 700.]
    elif fault == 'known_k_domain':
        optical.clear()
        optical.update(id='nk', name='N wide K narrow', kind='cauchy',
            wavelength_unit='nm', wavelength_min=450., wavelength_max=700.,
            parameters={'A':1.3,'B':0.,'C':0.},
            k_data={'wavelength_unit':'nm','wavelength_min':540.,'wavelength_max':560.,
                    'wavelengths':[540.,560.], 'k':[.1,.1]})
    else:
        environment = deepcopy(dict(request.environment))
        environment['use_k'] = False
        request = replace(request, environment=environment)
    with pytest.raises(ValueError):
        freeze_film_synthesis_request(service, request)


def test_physical_seed_io_typed_job_roundtrip_keeps_policy_and_raw_unknown_k():
    service, request = setup_problem(low=550., high=550., samples=1, materials=('low',))
    service.materials.records['low']['optical_sets'][0]['k'] = None
    frozen = freeze_film_synthesis_request(service, opt_in(request))
    spec = FilmStructureSpec('multistack', (
        FilmStructureMaterialBinding('H','high','nk'), FilmStructureMaterialBinding('L','low','nk')),
        (FilmBraggStackSection(550., pairs=1),))
    envelope = film_synthesis_request_to_payload('assumed-seed', frozen,
                                                (structure_spec_to_mapping(spec),))
    canonical, metadata = prepare_film_synthesis_payload(service, envelope)
    _identifier, parsed, structures = film_synthesis_request_from_payload(canonical)
    assert not structures and parsed.environment['assume_lossless'] is True
    assert parsed.seed_designs[0]['assume_lossless'] is True
    assert 'low/nk' in parsed.seed_designs[0]['note']
    assert metadata[0]['metadata']['absorption_assumption']['policy'] == 'unknown_k_zero'
    job = build_job_payload({'payload': canonical, 'physical_structure_metadata': list(metadata)})
    _identifier, roundtrip, replay_metadata = deserialize_job_payload(json.loads(json.dumps(job)))
    assert roundtrip == parsed
    assert replay_metadata == list(metadata)
    assert parsed.frozen_material_snapshot is not None
    low = next(row for row in parsed.frozen_material_snapshot['materials'] if row['id'] == 'low')
    assert low['optical_sets'][0]['k'] is None
    with pytest.raises(ValueError, match='must match'):
        generate_physical_structure(request.environment, spec, parsed.frozen_material_snapshot,
                                    assume_unknown_k_zero=True)


@pytest.mark.parametrize('location', ['note', 'summary', 'full'])
def test_candidate_annotation_preserves_text_is_idempotent_and_never_truncates(location):
    service, request = setup_problem(materials=('high', 'low'))
    service.materials.records['low']['optical_sets'][0]['k'] = None
    service.materials.records['high']['optical_sets'][0]['k'] = None
    frozen = freeze_film_synthesis_request(service, opt_in(request))
    design = deepcopy(dict(frozen.environment))
    design['front_layers'] = [dict(id='film', material_id='low', optical_set_id='nk',
        thickness=dict(value=100., unit='nm'), note='')]
    design['note'] = 'Original note' if location == 'note' else 'N' * 20000
    design['summary'] = 'Original summary' if location != 'full' else 'S' * 20000
    original = deepcopy(design)
    if location == 'full':
        with pytest.raises(ValueError, match='not truncated'):
            annotate_synthesis_design(frozen, design)
    else:
        annotated = annotate_synthesis_design(frozen, design)
        assert annotated['note'].startswith(original['note'])
        assert annotated['summary'].startswith(original['summary'])
        text = annotated[location]
        assert '未知 k 按 0' in text and 'low/nk' in text
        assert 'high/nk' not in text  # Pool-only unknown material is not a used assumption.
        assert annotate_synthesis_design(frozen, annotated) == annotated
        assert annotated['assume_lossless'] is True and annotated['use_k'] is True
    assert design == original
    bare = deepcopy(dict(frozen.environment))
    assert annotate_synthesis_design(frozen, bare) == bare


def test_physical_seed_provenance_ignores_only_legal_presentation_text():
    service, request = setup_problem(low=550., high=550., samples=1, materials=('low',))
    service.materials.records['low']['optical_sets'][0]['k'] = None
    frozen = freeze_film_synthesis_request(service, opt_in(request))
    spec = FilmStructureSpec('multistack', (
        FilmStructureMaterialBinding('H','high','nk'), FilmStructureMaterialBinding('L','low','nk')),
        (FilmBraggStackSection(550., pairs=1),))
    envelope = film_synthesis_request_to_payload('annotated-seed', frozen,
                                                (structure_spec_to_mapping(spec),))
    canonical, metadata = prepare_film_synthesis_payload(service, envelope)
    seed = canonical['request']['seed_designs'][0]
    seed['note'] = 'A legitimate user note, preserved separately from physical identity.'
    seed['summary'] = 'A legitimate summary.'
    parameters = {'payload': canonical, 'physical_structure_metadata': list(metadata)}
    build_job_payload(parameters)
    seed['front_layers'][0]['thickness']['value'] += 1.
    with pytest.raises(ValueError, match='matching generated seed'):
        build_job_payload(parameters)


@pytest.mark.parametrize('opted', [False, True])
@pytest.mark.parametrize('role', ['material', 'incident'])
def test_phase_sampling_rejects_known_narrow_k_source_even_without_freeze_preflight(role, opted):
    service, request = setup_problem(materials=('low',))
    frozen = freeze_film_synthesis_request(service, opt_in(request) if opted else request)
    assert frozen.frozen_material_snapshot is not None
    snapshot = deepcopy(dict(frozen.frozen_material_snapshot))
    material_id = 'low' if role == 'material' else 'glass'
    record = next(row for row in snapshot['materials'] if row['id'] == material_id)['optical_sets'][0]
    record.clear()
    record.update(id='nk', name='Wide n with narrow measured k', kind='cauchy',
        wavelength_unit='nm', wavelength_min=450., wavelength_max=700.,
        parameters={'A':1.5, 'B':0., 'C':0.},
        k_data={'wavelength_unit':'nm', 'wavelength_min':540., 'wavelength_max':560.,
                'wavelengths':[540.,560.], 'k':[.1,.1]})
    environment = deepcopy(dict(frozen.environment))
    if role == 'incident':
        environment['incident_medium'] = dict(kind='material', material_id='glass', optical_set_id='nk')
    # Directly exercise the phase helper to isolate it from freeze's own domain check.
    changed = replace(frozen, environment=environment, frozen_material_snapshot=snapshot)
    with pytest.raises(ValueError):
        _phase_step_limits_nm(changed)


def test_unsupported_k_table_is_rejected_instead_of_becoming_a_hidden_k_source():
    from coat_sim.core.optical_constants import validate_optical_set
    service, _request = setup_problem(materials=('low',))
    record = deepcopy(service.materials.records['low']['optical_sets'][0])
    record['k'] = None
    record['k_table'] = dict(wavelength_unit='nm', wavelength_min=450., wavelength_max=700.,
                             wavelengths=[450.,700.], k=[.1,.1])
    with pytest.raises(ValueError):
        validate_optical_set(record)


def test_unused_unknown_k_pool_has_source_k_candidate_claim_without_losing_pool_provenance():
    service, request = setup_problem(upper=1., low=550., high=550., samples=1, materials=('low',))
    service.materials.records['low']['optical_sets'][0]['k'] = None
    result = synthesize_film_design(service, opt_in(request))
    assert result.candidate_design is not None and not result.candidate_design['front_layers']
    payload = film_synthesis_result_to_payload('unused-pool', result)
    for assumption in (payload['absorption_assumption'], payload['review']['absorption_assumption'],
                       payload['best_candidate_audit']['absorption_assumption']):
        assert assumption['affected_materials'][0]['material_id'] == 'low'
        assert assumption['used_affected_materials'] == []
        assert not assumption['conditional_on_assumption']
        assert 'using source k' in assumption['claim']
        assert 'conditional on explicit unknown-k=0' not in assumption['claim']
    assert '未知 k 按 0' not in result.candidate_design['note']


@pytest.mark.parametrize('scenario', ['failed_training', 'multiple_audits'])
def test_checkpoint_restores_original_cumulative_work_and_marks_legacy_unknowns(tmp_path, monkeypatch, scenario):
    from threading import Event
    from coat_sim.core import film_auto_synthesis as synthesis
    service, request = setup_problem(upper=.001, low=550., high=550., samples=1, materials=('low',))
    if scenario == 'multiple_audits':
        seed = deepcopy(dict(request.environment))
        seed['front_layers'] = [dict(id='seed', material_id='low', optical_set_id='nk',
            thickness=dict(value=550. / (4. * (1.5 ** .5)), unit='nm'), note='')]
        request = replace(request, seed_designs=(seed,))
    else:
        real_evaluate = synthesis.evaluate_frozen_structure_candidate
        calls = []
        def fail_second_training(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('deliberate failed physical evaluation for counter recovery test')
            return real_evaluate(*args, **kwargs)
        monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', fail_second_training)
    frozen = freeze_film_synthesis_request(service, request)
    payload = film_synthesis_request_to_payload('checkpoint-work', frozen)
    job = build_job_payload({'payload': payload, 'physical_structure_metadata': []})
    records = []
    execute_job({'payload': job}, Event(), lambda *args, **kwargs: None,
                tmp_path / 'checkpoint.json', lambda path, value: records.append(deepcopy(value)), str(tmp_path))
    assert records
    checkpoint = records[-1]
    progress = checkpoint['progress']
    if scenario == 'failed_training':
        assert progress['failed_evaluations'] == 1
    else:
        assert progress['validation_evaluations'] >= 2
        assert progress['audit_physical_evaluations'] > checkpoint['audit']['physical_evaluations']
    recovered = validate_checkpoint(checkpoint)
    assert is_synthesis_result(recovered)
    assert recovered['work_counters']['work_counters_complete']
    for key in ('evaluations', 'validation_evaluations', 'failed_evaluations',
                'audit_physical_evaluations', 'audit_wavelength_points'):
        assert recovered['result'][key] == recovered['review']['cost'][key] == progress[key]
    assert recovered['audit']['recovery_replay_work']['validation_evaluations'] == 1
    assert recovered['audit']['recovery_replay_work']['audit_physical_evaluations'] > 0
    legacy = deepcopy(checkpoint)
    for key in ('validation_evaluations', 'failed_evaluations',
                'audit_physical_evaluations', 'audit_wavelength_points'):
        legacy['progress'].pop(key)
    legacy_recovered = validate_checkpoint(legacy)
    assert is_synthesis_result(legacy_recovered)
    assert legacy_recovered['result']['candidate_design'] == checkpoint['design']
    assert not legacy_recovered['work_counters']['work_counters_complete']
    for key in legacy_recovered['work_counters']['unknown_fields']:
        assert legacy_recovered['result'][key] is None
        assert legacy_recovered['review']['cost'][key] is None
    assert legacy_recovered['result']['cycles'] == progress['cycle']
    assert legacy_recovered['result']['evaluations'] == progress['evaluations']
    assert legacy_recovered['audit']['physical_evaluations'] is None
    assert legacy_recovered['audit']['wavelength_points'] is None
    assert legacy_recovered['audit']['recovery_replay_work']['audit_physical_evaluations'] > 0
