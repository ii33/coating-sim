"""Local safety stage and durable structural frontier, through real core calls."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, synthesize_film_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_continuation import continue_synthesis_request, validate_search_state
from coat_sim.core.film_synthesis_margin import refine_worst_margin, worst_signed_margin
from tests.test_film_auto_synthesis import setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def margin_fixture(metric='R', *, upper_nm=10., enabled_mean=False):
    ar = metric == 'R'
    objective = FilmOptimizationObjective(500., 600., 3, angles_deg=(0., 30.), metric=metric,
        normalization_scale=.004,
        pointwise=FilmObjectiveConstraint(lower_bound=0. if ar else .996, upper_bound=.004 if ar else 1.),
        mean=FilmObjectiveConstraint(enabled=enabled_mean,
            lower_bound=0. if ar else .996, upper_bound=.004 if ar else 1.))
    request = FilmSynthesisRequest({}, (objective,), (), max_layer_thickness_nm=upper_nm)
    design = dict(front_layers=[dict(id='d', material_id='m', optical_set_id='nk',
        thickness=dict(value=min(4., upper_nm), unit='nm'))])
    calls = []

    def evaluate(candidate, *, jacobian=True):
        d = candidate['front_layers'][0]['thickness']['value']
        assert 0. < d <= upper_nm
        calls.append(d)
        r = .001 + .001 * (d - 5.)**2
        derivative = .002 * (d - 5.)
        responses = [dict(objective_index=0, angle_deg=a, wavelength_nm=[500., 550., 600.],
            actual_values=[r if ar else 1-r] * 3,
            jacobian_values_by_layer_id={'d': [derivative if ar else -derivative] * 3})
            for a in objective.angles_deg]
        reduced = reduce_objective_responses(request.objectives, responses, layer_ids=('d',))
        return dict(design=deepcopy(candidate), responses=responses, score=reduction_score(reduced, 1e-3))
    return request, evaluate(design), evaluate, calls


@pytest.mark.parametrize('metric', ['R', 'T'])
@pytest.mark.parametrize('mean', [False, True])
def test_signed_margin_goes_past_hinge_zero_without_implicit_mean(metric, mean):
    request, initial, evaluate, calls = margin_fixture(metric, enabled_mean=mean)
    result, details = refine_worst_margin(request, initial, evaluate=evaluate, archive=lambda *a, **k: None)
    assert details['termination'] == 'converged'
    assert result['score']['nominal_pass'] and initial['score']['max_error'] == 0.
    assert result['design']['front_layers'][0]['thickness']['value'] == pytest.approx(5., abs=1e-5)
    assert worst_signed_margin(request, initial) == pytest.approx(-.5)
    assert worst_signed_margin(request, result) == pytest.approx(-.75)
    assert {r.reduction for r in result['score']['errors']} == ({'pointwise', 'mean'} if mean else {'pointwise'})
    assert details['physical_candidates'] == len(calls) - 1


def test_bound_stationarity_does_not_require_zero_raw_optical_gradient():
    request, initial, evaluate, _ = margin_fixture(upper_nm=4.)
    result, details = refine_worst_margin(request, initial, evaluate=evaluate, archive=lambda *a, **k: None)
    assert details['termination'] == 'converged'
    assert result['design']['front_layers'][0]['thickness']['value'] == 4.
    assert result['responses'][0]['jacobian_values_by_layer_id']['d'][0] != 0.


def test_best_line_search_trial_does_not_borrow_terminal_convergence(monkeypatch):
    from types import SimpleNamespace
    from coat_sim.core import film_synthesis_margin as margin
    request, initial, evaluate, _ = margin_fixture()
    def minimize(fun, start, **kwargs):
        kwargs['constraints'][0]['fun'](np.array([.05, -.75]))  # Better trial, not returned endpoint.
        return SimpleNamespace(x=np.array([.06, -.5]), success=True, status=0, nit=2,
            message='synthetic successful endpoint for identity test')
    monkeypatch.setattr(margin.import_module('scipy.optimize'), 'minimize', minimize)
    result, detail = margin.refine_worst_margin(request, initial, evaluate=evaluate, archive=lambda *a,**k:None)
    assert result['design']['front_layers'][0]['thickness']['value'] == 6.
    assert detail['termination'] == 'converged'
    assert detail['solver_terminal']['physical_hash'] == physical_design_hash(result['design'])
    assert detail['returned_best']['is_solver_terminal']
    assert detail['best_feasible_trial']['physical_hash'] != detail['solver_terminal']['physical_hash']


def test_terminal_audit_rejection_does_not_label_fallback_converged_or_thin(monkeypatch):
    from coat_sim.core import film_synthesis_margin as margin, film_synthesis_audit as audit
    from coat_sim.core import film_synthesis_thickness as thickness
    from tests.test_film_synthesis_thickness_integration import request_case
    service, request, _ = request_case('AR', 'pointwise')
    refine, audit_candidate = margin.refine_worst_margin, audit.audit_frozen_synthesis_candidate
    terminal_hashes, rejected = set(), []
    def track(*args, **kwargs):
        value, detail = refine(*args, **kwargs)
        if detail['termination'] == 'converged':
            terminal_hashes.add(physical_design_hash(value['design']))
        return value, detail
    def reject_terminal(frozen, design, **kwargs):
        value = audit_candidate(frozen, design, **kwargs)
        if physical_design_hash(design) in terminal_hashes:
            responses = [dict(objective_index=i,angle_deg=a,
                wavelength_nm=np.linspace(o.wavelength_min_nm,o.wavelength_max_nm,o.wavelength_samples),
                actual_values=[.5]*o.wavelength_samples) for i,o in enumerate(frozen.objectives) for a in o.angles_deg]
            value['score'] = reduction_score(reduce_objective_responses(frozen.objectives,responses),
                frozen.acceptable_normalized_error)
            value['score']['design'] = deepcopy(design)
            rejected.append(physical_design_hash(design))
        return value
    monkeypatch.setattr(margin,'refine_worst_margin',track)
    monkeypatch.setattr(audit,'audit_frozen_synthesis_candidate',reject_terminal)
    monkeypatch.setattr(thickness,'reduce_total_thickness',lambda *a,**k:pytest.fail('audit-rejected local stage must stop'))
    result = synthesize_film_design(service,request)
    assert rejected
    assert result.termination_reason == 'local_margin_audit_rejected'
    local = result.local_optimum
    assert not local['local_converged'] and local['termination'] == 'audit_rejected'
    assert local['solver_terminal']['physical_hash'] in rejected
    assert local['audited_selected']['physical_hash'] == physical_design_hash(result.candidate_design)
    assert local['audited_selected']['physical_hash'] != local['solver_terminal']['physical_hash']
    assert local['role'] == 'audited_fallback_before_incomplete_local_stage'
    assert 'thickness_reduction' not in result.performance['search']['hybrid']


@pytest.mark.parametrize('criterion, actual', [
    (FilmObjectiveConstraint(mode='value', target_value=.2, lower_bound=None, upper_bound=None), .2),
    (FilmObjectiveConstraint(lower_bound=.3, upper_bound=.3), .3),
    (FilmObjectiveConstraint(lower_bound=None, upper_bound=0.), 0.),
    (FilmObjectiveConstraint(lower_bound=1., upper_bound=None), 1.),
])
def test_value_and_zero_width_targets_do_not_invent_margin(criterion, actual):
    request, initial, _, _ = margin_fixture()
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=criterion),))
    responses = [dict(objective_index=0, angle_deg=a, wavelength_nm=[500., 550., 600.],
                      actual_values=[actual]*3) for a in (0., 30.)]
    initial['score'] = reduction_score(reduce_objective_responses(request.objectives, responses), 1e-3)
    result, details = refine_worst_margin(request, initial,
        evaluate=lambda *a, **k: pytest.fail('no interval direction'), archive=lambda *a, **k: None)
    assert result is initial and details['termination'] == 'no_interval_margin'


def hybrid_request():
    service, request = setup_problem(upper=.005)
    return service, replace(request, search_strategy='hybrid', cpu_workers=1, parallel_batch_size=1,
        max_cycles=2, max_evaluations=150, enable_local_margin_refinement=False, enable_thickness_reduction=False)


def test_resume_new_structure_with_fresh_scores_and_persistent_cursors():
    service, request = hybrid_request()
    first = synthesize_film_design(service, request)
    assert first.targets_met
    state = json.loads(json.dumps(first.search_checkpoint))
    before = deepcopy(state)
    second = synthesize_film_design(None, continue_synthesis_request(first.frozen_request, state,
        current_design=first.candidate_design))
    assert state == before  # view/save/continue cannot mutate the source state
    assert any(row.stage == 'search_resumed' for row in second.trace)
    assert any(row.stage == 'structure_proposal' for row in second.trace)
    assert sum(second.search_checkpoint['cursors'].values()) > sum(state['cursors'].values())
    assert set(second.search_checkpoint['tried']) - set(state['tried'])
    assert second.evaluations > 2
    assert validate_search_state(second.frozen_request, second.search_checkpoint)


def test_resume_cannot_return_immediately_from_old_passing_champion_and_keeps_unscored_pool():
    service, request = hybrid_request()
    first = synthesize_film_design(service, request)
    continued = continue_synthesis_request(first.frozen_request, first.search_checkpoint,
        current_design=first.candidate_design, max_evaluations=2)
    result = synthesize_film_design(None, continued)
    assert not result.targets_met and result.state != 'completed'
    assert result.termination_reason == 'evaluation_limit'
    old = {physical_design_hash(r['design']) for r in first.search_checkpoint['pool']}
    new = {physical_design_hash(r['design']) for r in result.search_checkpoint['pool']}
    assert old <= new


def test_duplicate_source_quantum_keeps_frontier_and_next_resume_finds_new_design():
    from coat_sim.core.film_synthesis_continuation import seal_search_state, SOURCE_NAMES
    service, request = hybrid_request()
    first = synthesize_film_design(service, request)
    old, new = deepcopy(first.candidate_design), deepcopy(first.candidate_design)
    new['front_layers'][0]['thickness']['value'] *= 1.002
    request = replace(first.frozen_request, seed_designs=(old,new), warm_start_seed_index=None)
    state = deepcopy(first.search_checkpoint)
    state.update(active_sources=['user_seed'],cursors=dict.fromkeys(SOURCE_NAMES,0),source_cursor=0,pending=[])
    state = seal_search_state(state)
    partial = synthesize_film_design(None,continue_synthesis_request(request,state))
    assert partial.termination_reason == 'continuation_quantum_no_new_candidate'
    assert partial.search_checkpoint['active_sources'] == ['user_seed']
    assert partial.search_checkpoint['cursors']['user_seed'] == 1
    assert not partial.performance['search']['hybrid']['continuation_session']['frontier_exhausted']
    cancel = Event()
    def stop(row):
        if row.stage == 'structure_proposal':
            cancel.set()
    continued = synthesize_film_design(None,continue_synthesis_request(partial.frozen_request,
        partial.search_checkpoint), cancel_event=cancel,progress_callback=stop)
    assert any(t.stage == 'structure_proposal' for t in continued.trace)
    assert physical_design_hash(new) in continued.search_checkpoint['tried']
    assert continued.search_checkpoint['cursors']['user_seed'] == 2


def test_edited_passing_seed_does_not_replace_freshly_rechecked_better_history():
    service, request = hybrid_request()
    first = synthesize_film_design(service, request)
    edited = deepcopy(first.candidate_design)
    edited['front_layers'][0]['thickness']['value'] *= 1.001
    cancel = Event()
    def stop_before_new_structure(row):
        if row.stage == 'structure_proposal':
            cancel.set()
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        first.search_checkpoint, current_design=edited), cancel_event=cancel,
        progress_callback=stop_before_new_structure)
    assert result.state == 'cancelled'
    assert result.validation_complete
    assert result.validation_score['nominal_pass']
    assert physical_design_hash(result.candidate_design) == physical_design_hash(first.candidate_design)
    assert physical_design_hash(result.validation_score['design']) == physical_design_hash(result.candidate_design)


@pytest.mark.parametrize('change', ['target', 'material', 'constraint', 'search'])
def test_changed_frozen_physics_requires_explicit_rebuild(change):
    service, request = hybrid_request()
    first = synthesize_film_design(service, request)
    frozen = first.frozen_request
    if change == 'target':
        frozen = replace(frozen, objectives=(replace(frozen.objectives[0], normalization_scale=.2),))
    elif change == 'material':
        snapshot = deepcopy(frozen.frozen_material_snapshot)
        snapshot['materials'][0]['optical_sets'][0]['n'][0] += .01
        frozen = replace(frozen, frozen_material_snapshot=snapshot)
    elif change == 'constraint':
        frozen = replace(frozen, max_total_thickness_nm=500.)
    else:
        frozen = replace(frozen, structure_search_seed=frozen.structure_search_seed + 1)
    with pytest.raises(ValueError, match='fingerprint changed'):
        continue_synthesis_request(frozen, first.search_checkpoint)


def test_edit_seed_and_cancel_keep_structural_progress_and_exact_candidate_evidence():
    service, request = hybrid_request()
    first = synthesize_film_design(service, request)
    edited = deepcopy(first.candidate_design)
    edited['front_layers'][0]['thickness']['value'] *= 1.01
    original = deepcopy(edited)
    cancel = Event()
    snapshots = []
    def progress(row):
        if row.stage == 'search_resumed':
            cancel.set()
    continued = continue_synthesis_request(first.frozen_request, first.search_checkpoint, current_design=edited)
    result = synthesize_film_design(None, continued, cancel_event=cancel, progress_callback=progress,
        search_checkpoint_callback=lambda state: snapshots.append(deepcopy(state)))
    assert result.state == 'cancelled' and edited == original
    assert result.performance['search']['warm_start']['physical_hash'] == physical_design_hash(edited)
    assert result.search_checkpoint['cursors'] == first.search_checkpoint['cursors']
    assert snapshots[-1] == result.search_checkpoint
    if result.validation_complete:
        assert physical_design_hash(result.validation_score['design']) == physical_design_hash(result.candidate_design)
    assert result.training_score['design'] == result.candidate_design


def test_topology_edit_is_fresh_seed_with_original_frontier_and_native_boundaries():
    service, request = hybrid_request()
    request = replace(request, max_layer_thickness_nm=500.)
    first = synthesize_film_design(service, request)
    edited = deepcopy(first.candidate_design)
    layer = deepcopy(edited['front_layers'][0])
    layer['id'] = 'user-added-layer'
    layer['thickness']['value'] = 2.
    edited['front_layers'].append(layer)
    continued = continue_synthesis_request(first.frozen_request, first.search_checkpoint, current_design=edited)
    assert continued.seed_designs[0] == edited
    assert continued.search_checkpoint == first.search_checkpoint
    cancel = Event()
    def stop(row):
        if row.stage == 'search_resumed':
            cancel.set()
    result = synthesize_film_design(None, continued, cancel_event=cancel, progress_callback=stop)
    assert result.performance['search']['warm_start']['physical_hash'] == physical_design_hash(edited)
    assert result.search_checkpoint['cursors'] == first.search_checkpoint['cursors']
    illegal = deepcopy(edited)
    illegal['front_layers'][-1]['thickness']['value'] = first.frozen_request.max_layer_thickness_nm + 1.
    with pytest.raises(ValueError):
        continue_synthesis_request(first.frozen_request, first.search_checkpoint, current_design=illegal)


def test_search_checkpoint_observer_failure_is_not_swallowed():
    service, request = hybrid_request()
    def fail(state):
        raise OSError('search checkpoint disk failed')
    with pytest.raises(OSError, match='search checkpoint disk failed'):
        synthesize_film_design(service, request, search_checkpoint_callback=fail)


def test_local_result_and_final_thinner_candidate_have_separate_audits():
    from tests.test_film_synthesis_thickness_integration import request_case, total
    service, request, _ = request_case('AR', 'pointwise')
    original = deepcopy(request.seed_designs)
    result = synthesize_film_design(service, request)
    assert result.targets_met and result.local_optimum['validation_complete']
    assert result.local_optimum['role'] == 'fixed_topology_local_result_before_thickness_reduction'
    assert sum(r['thickness']['value'] for r in result.local_optimum['design']['front_layers']) >= total(result)
    assert physical_design_hash(result.local_optimum['validation_score']['design']) == result.local_optimum['physical_hash']
    assert physical_design_hash(result.validation_score['design']) == physical_design_hash(result.candidate_design)
    assert request.seed_designs == original
    fixed = result.performance['search']['hybrid']['local_margin']['fixed_layer_ids']
    assert fixed == [r['id'] for r in result.local_optimum['design']['front_layers']]


def test_multiple_bands_angles_and_metrics_use_declared_normalization():
    request, initial, _, _ = margin_fixture()
    objectives = (
        replace(request.objectives[0], normalization_scale=.1,
            pointwise=FilmObjectiveConstraint(lower_bound=0., upper_bound=.8)),
        FilmOptimizationObjective(700., 800., 3, angles_deg=(45.,), metric='T', normalization_scale=.2,
            pointwise=FilmObjectiveConstraint(lower_bound=.2, upper_bound=1.)),
    )
    request = replace(request, objectives=objectives)
    def evaluate(design, **kwargs):
        d = design['front_layers'][0]['thickness']['value']
        responses = [dict(objective_index=i, angle_deg=a,
            wavelength_nm=np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, 3),
            actual_values=[d/10.]*3, jacobian_values_by_layer_id={'d': [.1]*3})
            for i,o in enumerate(objectives) for a in o.angles_deg]
        reduced = reduce_objective_responses(objectives, responses, layer_ids=('d',))
        return dict(design=deepcopy(design), responses=responses, score=reduction_score(reduced, 1e-3))
    result, detail = refine_worst_margin(request, evaluate(initial['design']),
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert detail['termination'] == 'converged'
    assert result['design']['front_layers'][0]['thickness']['value'] == pytest.approx(6., abs=1e-7)
    assert worst_signed_margin(request, result) == pytest.approx(-2.)
    assert {r.reduction for r in result['score']['errors']} == {'pointwise'}


@pytest.mark.parametrize('status', ['iteration_limit', 'solver_failure', 'resource_guard'])
def test_incomplete_local_stage_stops_before_thinning(monkeypatch, status):
    from coat_sim.core import film_synthesis_margin as margin, film_synthesis_thickness as thickness
    from tests.test_film_synthesis_thickness_integration import request_case
    service, request, _ = request_case()
    monkeypatch.setattr(margin, 'refine_worst_margin', lambda request, initial, **kw:
        (initial, dict(termination=status, success=False)))
    monkeypatch.setattr(thickness, 'reduce_total_thickness', lambda *a, **k: pytest.fail('incomplete local stage'))
    result = synthesize_film_design(service, request)
    assert not result.targets_met and result.state == 'not_found'
    assert result.termination_reason == 'local_margin_' + status
    assert result.local_optimum['termination'] == status


def test_local_budget_exhaustion_is_not_hidden_by_old_passing_candidate(monkeypatch):
    from coat_sim.core import film_synthesis_margin as margin
    from tests.test_film_synthesis_thickness_integration import request_case
    service, request, _ = request_case(budget=20)
    def consume(request, initial, *, evaluate, cancel_check, **kwargs):
        while True:
            cancel_check()
            evaluate(initial['design'], jacobian=True)
    monkeypatch.setattr(margin, 'refine_worst_margin', consume)
    result = synthesize_film_design(service, request)
    assert result.state == 'not_found' and not result.targets_met
    assert result.termination_reason == 'evaluation_limit' and result.evaluations <= 20
    assert 'thickness_reduction' not in result.performance['search']['hybrid']


def test_corrupt_version_truncation_and_material_closure_preserve_source(tmp_path):
    from coat_sim.core.film_synthesis_continuation import load_search_state, seal_search_state
    service, request = hybrid_request()
    result = synthesize_film_design(service, request)
    state = deepcopy(result.search_checkpoint)
    path = tmp_path/'state.json'
    path.write_text('{"kind":', encoding='utf-8')
    with pytest.raises(ValueError, match='incomplete or invalid'):
        load_search_state(path)
    assert path.read_text() == '{"kind":'
    state['version'] = 100
    with pytest.raises(ValueError, match='kind/version'):
        continue_synthesis_request(result.frozen_request, state)
    state = deepcopy(result.search_checkpoint)
    state['pool'][0]['design']['front_layers'] = [dict(id='foreign', material_id='unknown', optical_set_id='nk',
        thickness=dict(value=30.,unit='nm'))]
    with pytest.raises(ValueError, match='material/nm closure'):
        validate_search_state(result.frozen_request, seal_search_state(state))
    edited = deepcopy(result.candidate_design)
    edited['front_layers'][0]['material_id'] = 'unknown'
    with pytest.raises(ValueError):
        continue_synthesis_request(result.frozen_request, result.search_checkpoint, current_design=edited)


def test_cross_process_pending_pair_cursor_random_and_feedback_restore(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_continuation import seal_search_state
    from coat_sim.core.film_synthesis_feedback import SparseFeedbackRegistry
    from tests.test_film_synthesis_feedback import error_for
    service, base = hybrid_request()
    request = replace(base, randomize_template_search=True, max_evaluations=200)
    cancel = Event()
    def stop(row):
        if row.stage == 'template_randomization' and json.loads(row.message).get('status') == 'constructed':
            cancel.set()
    partial = synthesize_film_design(service, request, cancel_event=cancel, progress_callback=stop)
    assert partial.state == 'cancelled' and len(partial.search_checkpoint['pending']) == 2
    state = deepcopy(partial.search_checkpoint)
    registry = SparseFeedbackRegistry(partial.frozen_request)
    assert registry.record_audit({'errors': [error_for(request, 0, 527.5, .02)]}) == 1
    state['feedback'] = registry.export_state()
    state = seal_search_state(state)
    continued = continue_synthesis_request(partial.frozen_request, state)
    direct = synthesize_film_design(None, continued)
    envelope = tmp_path/'request.json'
    envelope.write_text(json.dumps(film_synthesis_request_to_payload('cross-process', continued)), encoding='utf-8')
    output = tmp_path/'out.json'
    script = '''
import json,sys
from pathlib import Path
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
from coat_sim.core.film_auto_synthesis import synthesize_film_design
_,request,_=film_synthesis_request_from_payload(json.loads(Path(sys.argv[1]).read_text()))
result=synthesize_film_design(None,request)
sequence=[json.loads(t.message)['physical_hash'] for t in result.trace if t.stage=='structure_proposal']
Path(sys.argv[2]).write_text(json.dumps(dict(state=result.search_checkpoint,sequence=sequence,evaluations=result.evaluations)))
'''
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    subprocess.run([sys.executable, '-c', script, str(envelope), str(output)], cwd=Path(__file__).resolve().parents[1],
                   env=env, check=True, capture_output=True, timeout=30)
    child = json.loads(output.read_text())
    sequence = [json.loads(t.message)['physical_hash'] for t in direct.trace if t.stage=='structure_proposal']
    assert child['sequence'] == sequence
    assert sequence[:2] == [physical_design_hash(r['design']) for r in state['pending']]
    assert child['evaluations'] == direct.evaluations <= request.max_evaluations
    for key in ('cursors', 'active_sources', 'source_cursor', 'pending', 'random_state', 'feedback'):
        assert child['state'][key] == json.loads(json.dumps(direct.search_checkpoint[key]))
    assert child['state']['feedback']['revision'] >= state['feedback']['revision']
    assert json.loads(json.dumps(state['feedback']['points'][0])) in child['state']['feedback']['points']
    uninterrupted = synthesize_film_design(service, request)
    whole = [json.loads(t.message)['physical_hash'] for t in uninterrupted.trace if t.stage=='structure_proposal']
    assert whole[:2] == sequence[:2]  # Actual deterministic source/extra pair, not an old pool warm start.
    assert child['state']['random_state']['next_rotation_seed'] > state['random_state']['next_rotation_seed']


def test_durable_local_and_final_evidence_replay_without_replacing_saved_state(tmp_path):
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import build_job_payload, execute_job, validate_checkpoint, is_synthesis_result
    from coat_sim.scheduler.io import write_json
    from tests.test_film_synthesis_thickness_integration import request_case
    service, request, _ = request_case()
    frozen = freeze_film_synthesis_request(service, request)
    job = build_job_payload(dict(payload=film_synthesis_request_to_payload('local-recovery', frozen),
                                 physical_structure_metadata=[]))
    path = tmp_path/'best.json'
    response = execute_job({'payload':job}, Event(), lambda *a, **k: None, path, write_json, str(tmp_path))
    assert is_synthesis_result(response)
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['audit']['local_optimum']['validation_complete']
    original = path.read_bytes()
    recovered = validate_checkpoint(saved)
    assert is_synthesis_result(recovered) and path.read_bytes() == original
    local = recovered['result']['local_optimum']
    assert local['physical_hash'] == response['result']['local_optimum']['physical_hash']
    tampered = deepcopy(saved)
    tampered['audit']['local_optimum']['solver_terminal']['physical_hash'] = '0'*64
    with pytest.raises(ValueError,match='convergence identity'):
        validate_checkpoint(tampered)
    assert recovered['result']['search_checkpoint']['cursors'] == response['result']['search_checkpoint']['cursors']
    assert (tmp_path/'best.search-state.json').is_file()


def test_atomic_search_write_failure_keeps_previous_file(tmp_path):
    from coat_sim.scheduler.io import write_json
    service, request = hybrid_request()
    path = tmp_path/'state.json'
    write_json(path, {'previous':'retained'})
    original = path.read_bytes()
    def save(state):
        write_json(path, state, max_bytes=1)
    with pytest.raises(ValueError, match='json_output_exceeds_byte_limit'):
        synthesize_film_design(service, request, search_checkpoint_callback=save)
    assert path.read_bytes() == original and not list(tmp_path.glob('*.tmp'))


def test_state_size_admission_includes_atomic_writer_separator_spaces(monkeypatch):
    from coat_sim.core import film_synthesis_continuation as state_io
    monkeypatch.setattr(state_io, 'MAX_SEARCH_STATE_BYTES', 600)
    # Compact hashing fits, but default-spaced JSON plus the sealed digest
    # would exceed the bounded reader. Refuse it before replacing any file.
    payload = {'items':['x']*110}
    assert len(json.dumps(payload, separators=(',',':')).encode()) < 600 - 128
    with pytest.raises(ValueError, match='JSON transport limit'):
        state_io.seal_search_state(payload)
