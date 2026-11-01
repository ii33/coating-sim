"""Until-goal changes only outer scheduling; local convergence stays unchanged."""
from dataclasses import asdict,replace
from threading import Event
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
        _validated(replace(request,search_until_target=1))
    with pytest.raises(ValueError,match='hybrid'):
        _validated(replace(request,search_strategy='legacy'))
    for fields in (dict(max_cycles=-1),dict(max_evaluations=1),dict(max_seconds=float('nan'))):
        with pytest.raises(ValueError):
            _validated(replace(request,**fields))


def test_until_mode_keeps_local_stop_and_reports_unmet_without_auto_restart():
    service,request=unmet_problem(search_until_target=True,max_evaluations=2,max_seconds=1e-9)
    stages=[]
    result=synthesize_film_design(service,request,progress_callback=lambda p:stages.append(p.stage))
    assert result.state=='completed' and not result.targets_met
    assert result.termination_reason=='local_session_converged_targets_unmet'
    assert stages.count('hybrid_rotation')==1
    status=result.performance['search']['until_target']
    assert status['status']=='paused_local' and not status['search_complete']
    assert not status['global_infeasibility_proven'] and not status['declared_frontier_exhausted']
    assert '继续优化' in result.message
    local=result.performance['search']['hybrid']['local_session']
    assert local['session_complete'] and local['status']=='converged' and local['local_converged']
    assert result.evaluations>2
    assert len(result.candidate_design['front_layers'])<=request.max_layers


def test_explicit_continue_in_until_mode_advances_real_search_frontier():
    from tests.test_film_synthesis_session_stop import explorable_problem
    service,request=explorable_problem(317)
    request=replace(request,search_until_target=True)
    first=synthesize_film_design(service,request)
    assert first.state=='completed' and not first.targets_met
    resumed=continue_synthesis_request(first.frozen_request,first.search_checkpoint,current_design=first.candidate_design)
    assert resumed.search_until_target
    following=synthesize_film_design(None,resumed)
    assert following.state=='completed' and not following.targets_met
    assert sum(following.search_checkpoint['cursors'].values())>sum(first.search_checkpoint['cursors'].values())
    assert set(following.search_checkpoint['tried'])-set(first.search_checkpoint['tried'])
    assert any(p.stage=='structure_proposal' for p in following.trace)


def test_until_goal_returns_after_independent_pass_without_optional_post_goal_work():
    service,request=setup_problem(upper=.99)
    request=replace(request,search_strategy="hybrid",search_until_target=True,max_cycles=1,max_evaluations=2,max_seconds=1e-9,
        enable_thickness_reduction=True,enable_local_margin_refinement=True,max_working_set_bytes=None)
    result=synthesize_film_design(service,request)
    assert result.state=='completed' and result.targets_met and result.validation_complete
    hybrid=result.performance['search']['hybrid']
    assert hybrid['until_target_status']=='original_targets_independently_verified'
    assert result.performance['search']['until_target']['status']=='targets_met'
    assert result.performance['search']['until_target']['search_complete']
    assert 'local_margin' not in hybrid and 'thickness_reduction' not in hybrid


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
    args=dict(state='not_found',reason='continuation_frontier_exhausted',goal_met=False,
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


def test_result_validation_rejects_forged_goal_or_exhaustion_telemetry():
    from copy import deepcopy
    from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result
    service,request=unmet_problem(search_until_target=True)
    result=synthesize_film_design(service,request)
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
