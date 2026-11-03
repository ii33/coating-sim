"""Until-goal changes only outer scheduling; local convergence stays unchanged."""
from dataclasses import asdict,replace
from threading import Event, Timer
from typing import Any, Mapping, cast
import pytest
from coat_sim.core.film_auto_synthesis import _validated,synthesize_film_design,freeze_film_synthesis_request
from coat_sim.core.film_synthesis_continuation import search_fingerprint,continue_synthesis_request
from tests.test_film_synthesis_session_stop import unmet_problem
from tests.test_film_auto_synthesis import setup_problem

pytestmark=[pytest.mark.algorithm,pytest.mark.contract]


def test_until_target_only_discards_work_budgets_not_physical_constraints():
    _,request=unmet_problem(search_until_target=True,max_cycles=1,max_evaluations=2,max_seconds=1e-9)
    checked=_validated(request)
    assert checked.max_cycles is checked.max_evaluations is checked.max_seconds is None
    assert checked.max_layers==1 and checked.max_layer_thickness_nm==180.
    assert checked.local_step_tolerance==request.local_step_tolerance
    assert checked.relative_improvement_tolerance==request.relative_improvement_tolerance
    assert _validated(replace(request,search_until_target=False)).max_evaluations==2
    with pytest.raises(ValueError,match='boolean'):
        _validated(replace(request,search_until_target=cast(Any, 1)))
    with pytest.raises(ValueError,match='hybrid'):
        _validated(replace(request,search_strategy='legacy'))
    for fields in (dict(max_cycles=-1),dict(max_evaluations=1),dict(max_seconds=float('nan'))):
        with pytest.raises(ValueError):
            _validated(replace(request,**fields))


def _run_until_rotation_boundary(service, request, boundary=3):
    """Bound an otherwise intentionally open-ended job at a rotation boundary."""
    cancel = Event()
    stages = []
    checkpoints: list[Mapping[str, Any]] = []
    def progress(row):
        stages.append(row.stage)
        if row.stage == 'hybrid_rotation' and stages.count('hybrid_rotation') == boundary:
            cancel.set()
    # Safety watchdog prevents a broken scheduler from hanging the test suite.
    expired = Event()
    def expire():
        expired.set()
        cancel.set()
    watchdog = Timer(60., expire)
    watchdog.start()
    try:
        result = synthesize_film_design(service, request, cancel_event=cancel,
            progress_callback=progress, search_checkpoint_callback=checkpoints.append)
    finally:
        watchdog.cancel()
    assert not expired.is_set(), 'rotation boundary not reached before safety watchdog'
    assert cancel.is_set() and result.state == 'cancelled'
    assert not result.targets_met
    status = result.performance['search']['until_target']
    assert status['status'] == 'cancelled' and not status['search_complete']
    assert not status['global_infeasibility_proven'] and not status['declared_frontier_exhausted']
    return result, stages, checkpoints


def _assert_two_full_rotations(stages):
    starts = [i for i, stage in enumerate(stages) if stage == 'hybrid_rotation']
    assert len(starts) == 3
    for start, end in zip(starts, starts[1:], strict=False):
        rotation = stages[start:end]
        # Both are genuine complete outer rotations, not merely prior screening.
        assert 'hybrid_global_trial' in rotation
        assert 'needle_scan' in rotation
        assert 'local_session_complete' in rotation
        assert 'until_target_continue' in rotation


def test_until_mode_automatically_runs_multiple_rotations_without_changing_local_stop():
    from tests.test_film_synthesis_session_stop import explorable_problem
    service, request = explorable_problem(317)
    request = replace(request, search_until_target=True, max_evaluations=2, max_seconds=1e-9)
    result, stages, checkpoints = _run_until_rotation_boundary(service, request)
    _assert_two_full_rotations(stages)
    assert result.evaluations > 2
    hybrid = result.performance['search']['hybrid']
    assert hybrid['automatic_local_sessions_completed'] == 2
    assert hybrid['last_automatic_local_session']['cycle'] == 2
    assert 'automatic_local_sessions' not in hybrid  # no unbounded per-rotation history
    assert result.search_checkpoint is not None
    assert result.search_checkpoint['rotations'] >= 2
    assert checkpoints and result.search_checkpoint['rotations'] >= checkpoints[0]['rotations']
    assert sum(result.search_checkpoint['cursors'].values()) > sum(checkpoints[0]['cursors'].values())
    assert result.candidate_design is not None and request.max_layers is not None
    assert len(result.candidate_design['front_layers']) <= request.max_layers


def test_until_mode_preserves_real_local_convergence_before_advancing():
    service, request = unmet_problem(search_until_target=True, max_evaluations=2, max_seconds=1e-9)
    result, stages, _ = _run_until_rotation_boundary(service, request, boundary=2)
    local = result.performance['search']['hybrid']['local_session']
    assert local['session_complete'] and local['status'] == 'converged' and local['local_converged']
    assert stages.index('local_session_complete') < stages.index('until_target_continue')
    assert stages.index('until_target_continue') < max(i for i, stage in enumerate(stages) if stage == 'hybrid_rotation')
    assert result.evaluations > 2
    assert result.candidate_design is not None and request.max_layers is not None
    assert len(result.candidate_design['front_layers']) <= request.max_layers


def test_resumed_until_mode_runs_multiple_full_rotations_in_one_job():
    from tests.test_film_synthesis_session_stop import explorable_problem
    service, request = explorable_problem(317)
    first = synthesize_film_design(service, request)
    assert first.state == 'completed' and not first.targets_met
    assert first.search_checkpoint is not None and first.candidate_design is not None
    resumed = continue_synthesis_request(replace(first.frozen_request, search_until_target=True),
        first.search_checkpoint, current_design=first.candidate_design)
    result, stages, checkpoints = _run_until_rotation_boundary(None, resumed)
    _assert_two_full_rotations(stages)
    hybrid = result.performance['search']['hybrid']
    assert hybrid['automatic_local_sessions_completed'] == 2
    assert hybrid['last_automatic_local_session']['cycle'] == 2
    assert stages.count('search_resumed') == 1  # pool recovery occurs once per job, not each rotation
    assert stages.count('warm_restart') <= 1
    assert 'restoration_audit_screen' not in stages[stages.index('search_resumed') + 1:]
    assert result.search_checkpoint is not None
    assert result.search_checkpoint['rotations'] >= first.search_checkpoint['rotations'] + 2
    assert sum(result.search_checkpoint['cursors'].values()) > sum(first.search_checkpoint['cursors'].values())
    assert result.search_checkpoint['archive_total_admitted'] >= first.search_checkpoint['archive_total_admitted']
    assert checkpoints


def test_until_goal_returns_after_independent_pass_without_optional_post_goal_work():
    service,request=setup_problem(upper=.99)
    request=replace(request,search_strategy="hybrid",search_until_target=True,max_cycles=1,max_evaluations=2,max_seconds=1e-9,
        enable_thickness_reduction=True,enable_local_margin_refinement=True,max_working_set_bytes=None)
    stages=[]
    result=synthesize_film_design(service,request,progress_callback=lambda row: stages.append(row.stage))
    assert result.state=='completed' and result.targets_met and result.validation_complete
    # The successful independent pass is terminal for this mode.
    last_audit=max(i for i, stage in enumerate(stages) if stage == 'validation_complete')
    assert not any(stage in {'hybrid_rotation', 'hybrid_global_trial', 'needle_scan',
        'local_session', 'until_target_continue', 'local_margin_start',
        'thickness_reduction_trial'} for stage in stages[last_audit + 1:])
    hybrid=result.performance['search']['hybrid']
    assert hybrid['until_target_status']=='original_targets_independently_verified'
    assert result.performance['search']['until_target']['status']=='targets_met'
    assert result.performance['search']['until_target']['search_complete']
    assert 'local_margin' not in hybrid and 'thickness_reduction' not in hybrid


def test_resumed_until_goal_stops_before_reauditing_unvisited_pool():
    from copy import deepcopy
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    from coat_sim.core.film_synthesis_continuation import seal_search_state
    service, request = setup_problem(upper=.99)
    first = synthesize_film_design(service, replace(request, search_strategy='hybrid', search_until_target=True))
    assert first.targets_met and first.search_checkpoint is not None and first.candidate_design is not None
    unvisited = deepcopy(dict(first.candidate_design))
    unvisited['front_layers'] = [dict(id='unvisited', material_id='low', optical_set_id='nk',
                                      thickness=dict(value=80., unit='nm'))]
    state = deepcopy(dict(first.search_checkpoint))
    state['pool'].append(dict(design=unvisited, provenance=dict(family='controlled_test'),
        parent_hash=None, operator='evaluated', insertion_index=99))
    state = seal_search_state(state)
    resumed = continue_synthesis_request(first.frozen_request, state, current_design=first.candidate_design)
    result = synthesize_film_design(None, resumed)
    assert result.state == 'completed' and result.targets_met and result.validation_complete
    assert not any(row.stage in {'hybrid_rotation', 'until_target_continue', 'needle_scan'} for row in result.trace)
    assert not any(row.stage == 'validation' and row.physical_hash == physical_design_hash(unvisited)
                   for row in result.trace)
    assert result.search_checkpoint is not None
    assert physical_design_hash(unvisited) in {physical_design_hash(row['design'])
                                               for row in result.search_checkpoint['pool']}


def test_stop_policy_is_not_a_physics_or_search_frontier_identity_change():
    service,request=unmet_problem()
    frozen=freeze_film_synthesis_request(service,request)
    assert search_fingerprint(frozen)==search_fingerprint(replace(frozen,search_until_target=True))


def test_current_schema_optional_stop_policy_defaults_false_and_never_guesses_true():
    from coat_sim.core.film_synthesis_io import SCHEMA_VERSION,REQUEST_KIND,film_synthesis_request_from_payload
    service,request=unmet_problem()
    frozen=freeze_film_synthesis_request(service,request)
    data=asdict(frozen)
    data.pop('search_until_target')
    envelope=dict(schema_version=SCHEMA_VERSION,kind=REQUEST_KIND,request_id='same-protocol-default',
        request=data,physical_structures=[])
    _,parsed,_=film_synthesis_request_from_payload(envelope)
    assert parsed.search_until_target is False
    assert parsed.max_cycles==request.max_cycles
    data['search_until_target']=True
    _,parsed,_=film_synthesis_request_from_payload(envelope)
    assert parsed.search_until_target is True and parsed.max_cycles is None


def test_existing_positional_request_parameters_keep_their_meaning():
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
    _,request=setup_problem()
    positional=FilmSynthesisRequest(request.environment,request.objectives,request.candidate_materials,
        0,None,None,None,None,None,None,.0123)
    assert positional.acceptable_normalized_error==.0123 and positional.search_until_target is False


def test_until_mode_can_be_cancelled_without_claiming_goal_or_exhaustion():
    service,request=unmet_problem(search_until_target=True)
    cancel=Event()
    def progress(update):
        if update.stage=='hybrid_global_trial':
            cancel.set()
    result=synthesize_film_design(service,request,cancel_event=cancel,progress_callback=progress)
    assert cancel.is_set() and result.state=='cancelled'
    status=result.performance['search']['until_target']
    assert status['status']=='cancelled' and not status['search_complete']
    assert not status['global_infeasibility_proven'] and not status['declared_frontier_exhausted']


def test_finite_exhaustion_requires_positive_frontier_evidence_and_is_not_infeasibility():
    from coat_sim.core.film_synthesis_stop_policy import until_target_summary
    args: dict[str, Any] = dict(state='not_found',reason='continuation_frontier_exhausted',goal_met=False,
        hybrid={},has_local_optimum=False,checkpoint_available=True)
    assert not until_target_summary(**args)['search_complete']
    args['hybrid']={'continuation_session':{'frontier_exhausted':True}}
    assert not until_target_summary(**args)['search_complete']
    args['hybrid']['resource_limited']=False
    summary=until_target_summary(**args)
    assert summary['search_complete'] and summary['status']=='declared_frontier_exhausted'
    assert not summary['global_infeasibility_proven']
    args['hybrid']['resource_limited']=True
    assert not until_target_summary(**args)['search_complete']
    args.update(state='failed',reason='local_session_resource_guard')
    assert until_target_summary(**args)['status']=='blocked_resource'


def test_fixed_bounds_with_no_admissible_work_stop_as_blocked_not_exhausted():
    service, request = unmet_problem(search_until_target=True, minimum_nonzero_thickness_nm=80.,
        max_layer_thickness_nm=80., max_total_thickness_nm=80.)
    cancel = Event()
    watchdog = Timer(30., cancel.set)
    watchdog.start()
    try:
        result = synthesize_film_design(service, request, cancel_event=cancel)
    finally:
        watchdog.cancel()
    assert not cancel.is_set(), 'operational block did not terminate before watchdog'
    assert result.state == 'not_found' and result.termination_reason == 'no_admissible_search_step'
    status = result.performance['search']['until_target']
    assert status['status'] == 'blocked_search' and not status['search_complete']
    assert not status['declared_frontier_exhausted'] and not status['global_infeasibility_proven']
    assert not result.targets_met
    assert '受阻' in result.message and '未证明' in result.message


@pytest.mark.parametrize('reason, expected', [
    ('no_admissible_search_step', 'blocked_search'),
    ('local_session_solver_failure', 'interrupted_error'),
    ('local_session_terminal_evidence_unavailable', 'interrupted_error'),
])
def test_operational_blocks_and_late_solver_errors_do_not_reuse_stale_local_success(reason, expected):
    from coat_sim.core.film_synthesis_stop_policy import until_target_summary
    result = until_target_summary(state='not_found', reason=reason, goal_met=False,
        hybrid={}, has_local_optimum=True, checkpoint_available=True)
    assert result['status'] == expected and not result['search_complete']
    assert not result['declared_frontier_exhausted'] and not result['global_infeasibility_proven']


def test_result_validation_rejects_forged_goal_or_exhaustion_telemetry():
    from copy import deepcopy
    from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result
    service,request=unmet_problem(search_until_target=True)
    result, _, _ = _run_until_rotation_boundary(service, request, boundary=2)
    payload=film_synthesis_result_to_payload('until-proof',result)
    assert is_synthesis_result(payload)
    forged=deepcopy(payload)
    forged['result']['performance']['search']['until_target'].update(
        status='targets_met',goal_met=True,search_complete=True)
    assert not is_synthesis_result(forged)
    forged=deepcopy(payload)
    forged['result']['performance']['search']['until_target'].update(
        status='declared_frontier_exhausted',declared_frontier_exhausted=True,search_complete=True)
    assert not is_synthesis_result(forged)

    for invalid_reason in (None,123,False):
        malformed=deepcopy(payload)
        malformed['result']['termination_reason']=invalid_reason
        assert not is_synthesis_result(malformed)


def _controlled_local_terminal(monkeypatch, *, solver_status):
    """Only replace the fixed-topology terminal solve, not earlier searches."""
    from types import SimpleNamespace
    import numpy as np
    from coat_sim.core import film_synthesis_refinement as refinement
    original = refinement.refine_synthesis_full_minimax
    calls = []

    def controlled(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        calls.append(True)

        def optimizer(_fun, start, **_options):
            # One iteration and no distinct physical neighbours: insufficient
            # evidence for the project's multi-candidate stalled criterion.
            return SimpleNamespace(x=np.asarray(start).copy(), success=False,
                status=solver_status, nit=1, message='controlled incomplete local solve')
        with monkeypatch.context() as scoped:
            scoped.setattr(refinement.import_module('scipy.optimize'), 'minimize', optimizer)
            return original(request, initial, **kwargs)

    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', controlled)
    return calls


@pytest.mark.parametrize('solver_status, termination', [(9, 'iteration_limit'), (4, 'solver_failure')])
def test_until_mode_audits_incomplete_local_terminal_then_rotates(monkeypatch, solver_status, termination):
    calls = _controlled_local_terminal(monkeypatch, solver_status=solver_status)
    service, request = unmet_problem(search_until_target=True)
    cancel = Event()
    stages = []

    def progress(row):
        stages.append(row.stage)
        if row.stage == 'hybrid_rotation' and stages.count('hybrid_rotation') == 2:
            cancel.set()

    watchdog = Timer(30., cancel.set)
    watchdog.start()
    try:
        result = synthesize_film_design(service, request, cancel_event=cancel, progress_callback=progress)
    finally:
        watchdog.cancel()
    assert calls and stages.count('hybrid_rotation') == 2, result.termination_reason
    assert result.state == 'cancelled' and not result.targets_met
    assert stages.index('local_session') < stages.index('until_target_continue') < len(stages) - 1
    local = result.performance['search']['hybrid']['local_session']
    assert local['status'] == termination
    assert local['outer_search_handoff'] == 'recoverable_local_solver_stop'
    assert not local['local_converged']
    assert local['attempts'][-1]['termination'] == termination
    assert local['last_scaled_improvement'] <= local['scaled_stagnation_tolerance']
    assert local['attempts'][-1]['iterations'] == 1
    assert local['terminal_audit_complete'] and local['audited_selected']['validation_complete']
    assert not local['audited_selected']['nominal_pass']
    assert result.validation_complete and result.validation_score is not None
    assert not result.validation_score['nominal_pass']


@pytest.mark.parametrize('solver_status, termination', [(9, 'iteration_limit'), (4, 'solver_failure')])
def test_incomplete_terminal_still_stops_ordinary_mode(monkeypatch, solver_status, termination):
    calls = _controlled_local_terminal(monkeypatch, solver_status=solver_status)
    service, request = unmet_problem()
    result = synthesize_film_design(service, request)
    assert calls and result.termination_reason == 'local_session_' + termination
    local = result.performance['search']['hybrid']['local_session']
    assert not local['local_converged'] and not local['session_complete']
    assert not result.targets_met
    assert not any(row.stage == 'until_target_continue' for row in result.trace)


@pytest.mark.parametrize('boundary, expected', [
    ('missing_terminal', 'local_session_terminal_evidence_unavailable'),
    ('resource_guard', 'local_session_resource_guard'),
    ('returned_resource_guard', 'local_session_resource_guard'),
    ('returned_stage_budget', 'local_session_stage_budget'),
])
def test_until_mode_does_not_continue_without_terminal_or_past_resource_guard(monkeypatch, boundary, expected):
    from coat_sim.core import film_synthesis_refinement as refinement
    original = refinement.refine_synthesis_full_minimax
    calls = []

    def interrupted(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        calls.append(True)
        if boundary == 'resource_guard':
            raise refinement.SynthesisResourceGuard('controlled local resource boundary')
        if boundary.startswith('returned_'):
            # The minimizer may catch a stage/resource boundary and return an
            # accepted terminal rather than raising; it still must not hand off.
            kwargs['terminal_callback'](initial)
            return initial, dict(termination=boundary.removeprefix('returned_'),
                                 iterations=1, physical_candidates=1)
        return initial, dict(termination='solver_failure', iterations=1, physical_candidates=1)

    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', interrupted)
    service, request = unmet_problem(search_until_target=True)
    result = synthesize_film_design(service, request)
    assert calls and result.termination_reason == expected
    assert not result.targets_met
    assert not result.performance['search']['hybrid']['local_session']['local_converged']
    assert not any(row.stage == 'until_target_continue' for row in result.trace)


def test_until_mode_cancel_at_local_session_does_not_audit_or_rotate():
    service, request = unmet_problem(search_until_target=True)
    cancel = Event()
    stages = []

    def progress(row):
        stages.append(row.stage)
        if row.stage == 'local_session':
            cancel.set()

    result = synthesize_film_design(service, request, cancel_event=cancel, progress_callback=progress)
    assert result.state == 'cancelled' and not result.targets_met
    index = stages.index('local_session')
    assert 'until_target_continue' not in stages[index + 1:]
    assert 'validation' not in stages[index + 1:]
    assert not result.performance['search']['hybrid']['local_session']['local_converged']


def test_until_mode_does_not_treat_unexpected_local_exception_as_recoverable(monkeypatch):
    from coat_sim.core import film_synthesis_refinement as refinement
    original = refinement.refine_synthesis_full_minimax
    calls = []

    def failing(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        calls.append(True)
        raise RuntimeError('controlled unexpected local failure')

    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', failing)
    service, request = unmet_problem(search_until_target=True)
    result = synthesize_film_design(service, request)
    assert calls and result.state == 'failed'
    assert result.termination_reason == 'numerical_failure: controlled unexpected local failure'
    assert not result.targets_met
    assert not any(row.stage == 'until_target_continue' for row in result.trace)


def test_until_mode_audits_distinct_incomplete_terminal_and_best_before_rotation(monkeypatch):
    from types import SimpleNamespace
    import numpy as np
    from coat_sim.core import film_synthesis_audit as audit
    from coat_sim.core import film_synthesis_refinement as refinement
    from coat_sim.core.film_synthesis_archive import physical_design_hash

    original_refine = refinement.refine_synthesis_full_minimax
    original_audit = audit.audit_frozen_synthesis_candidate
    audited_after_local = []
    in_local = False
    cancel = Event()
    stages = []

    def observe_audit(frozen, design, **kwargs):
        value = original_audit(frozen, design, **kwargs)
        if in_local:
            audited_after_local.append(physical_design_hash(design))
        return value

    def controlled(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original_refine(request, initial, **kwargs)

        def optimizer(_fun, start, **options):
            # Evaluate real neighbors but report one iteration, preventing a
            # stalled label. Return the worst of the neighbors as the endpoint.
            outcomes = []
            for delta in (-.01, .01):
                trial = np.asarray(start).copy()
                trial[0] += delta
                residual = options['constraints'][0]['fun'](trial)
                outcomes.append((float(np.min(residual)), trial))
            terminal = min(outcomes, key=lambda item: item[0])[1]
            return SimpleNamespace(x=terminal, success=False, status=9, nit=1,
                                   message='controlled distinct terminal')
        with monkeypatch.context() as scoped:
            scoped.setattr(refinement.import_module('scipy.optimize'), 'minimize', optimizer)
            return original_refine(request, initial, **kwargs)

    def progress(row):
        nonlocal in_local
        stages.append(row.stage)
        if row.stage == 'local_session':
            in_local = True
        elif row.stage == 'hybrid_rotation' and stages.count('hybrid_rotation') == 2:
            cancel.set()

    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', observe_audit)
    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', controlled)
    service, request = unmet_problem(search_until_target=True)
    watchdog = Timer(30., cancel.set)
    watchdog.start()
    try:
        result = synthesize_film_design(service, request, cancel_event=cancel, progress_callback=progress)
    finally:
        watchdog.cancel()
    assert stages.count('hybrid_rotation') == 2 and result.state == 'cancelled'
    local = result.performance['search']['hybrid']['local_session']
    terminal = local['solver_terminal']['physical_hash']
    best = local['returned_best']['physical_hash']
    assert terminal != best
    assert terminal in audited_after_local and best in audited_after_local
    assert not local['local_converged'] and not result.targets_met
