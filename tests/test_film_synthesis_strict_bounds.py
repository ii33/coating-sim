from copy import deepcopy
from dataclasses import replace
from threading import Event
import json
import pytest
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design, _validated
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_request_from_payload, film_synthesis_result_to_payload, FilmSynthesisPayloadError
from tests.test_film_auto_synthesis import setup_problem


def _with_strict(request, *, lower=(), upper=(), **changes):
    objectives = tuple(replace(o, pointwise=replace(o.pointwise,
        strict_lower=i in lower, strict_upper=i in upper)) for i, o in enumerate(request.objectives))
    return replace(request, objectives=objectives, **changes)


def boundary_problem(metric='R', offset=0.):
    service, request = setup_problem(upper=.05,low=550.,high=550.,samples=1)
    bare = synthesize_film_design(service, request)
    assert bare.training_score is not None
    actual_r = bare.training_score['errors'][0].actual
    # Use the production evaluator to obtain T, rather than assuming R+T=1.
    if metric == 'T':
        probe = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=1.), metric='T'),))
        probe_result = synthesize_film_design(service,probe)
        assert probe_result.training_score is not None
        actual = probe_result.training_score['errors'][0].actual
    else:
        actual = actual_r
    objective=replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=actual+offset, upper_bound=1.), metric=metric)
    return service, replace(request,objectives=(objective,),max_layers=1,max_cycles=1,max_evaluations=4), actual


@pytest.mark.parametrize('metric', ['R','T'])
@pytest.mark.parametrize('offset', [0.,.0005])
def test_inclusive_equality_is_nominal_but_tolerated_lower_violation_is_solver_only(metric,offset):
    service,request,actual=boundary_problem(metric,offset)
    result=synthesize_film_design(service,request)
    assert result.state==('completed' if offset==0. else 'not_found')
    assert result.targets_met is (offset==0.)
    assert result.solver_acceptable is True
    assert result.validation_complete
    assert result.evaluations==(2 if offset==0. else 3)
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers']==[]
    assert result.training_score is not None
    assert result.training_score['errors'][0].actual==actual
    assert request.objectives[0].pointwise.strict_lower is False
    strict=synthesize_film_design(service,_with_strict(request,lower=(0,),max_evaluations=2))
    assert strict.state=='not_found' and not strict.targets_met
    assert strict.validation_complete
    assert strict.validation_score is not None
    assert request.objectives[0].pointwise.lower_bound is not None
    assert strict.validation_score['errors'][0].actual <= request.objectives[0].pointwise.lower_bound
    assert strict.evaluations==2


def test_mixed_objectives_only_explicit_indices_are_strict():
    service,request,actual=boundary_problem()
    transmission=replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=.9, upper_bound=1.), metric='T')
    mixed=_with_strict(replace(request,objectives=(*request.objectives,transmission)),lower=(1,),max_evaluations=2)
    result=synthesize_film_design(service,mixed)
    assert result.candidate_design is not None
    assert result.state=='completed' and result.candidate_design['front_layers']==[]
    assert result.training_score is not None
    assert result.training_score['errors'][0].actual==actual
    rejecting=synthesize_film_design(service,_with_strict(mixed,lower=(0,)))
    assert rejecting.state=='not_found' and not rejecting.targets_met


@pytest.mark.parametrize('indices', [(True,),(-1,),(1,),(0,0),(0.,),'0',None])
def test_invalid_nested_strict_lower_flags_are_rejected(indices):
    _,request,_=boundary_problem()
    with pytest.raises(ValueError,match='strict_lower'):
        _validated(replace(request,objectives=(replace(request.objectives[0],
            pointwise=replace(request.objectives[0].pointwise,strict_lower=indices)),)))


@pytest.mark.parametrize('changes', [dict(mode='value',target_value=.04,lower_bound=None,upper_bound=None),dict(lower_bound=None,upper_bound=.05)])
def test_nested_strict_lower_requires_range_with_lower_bound(changes):
    _,request,_=boundary_problem()
    with pytest.raises(ValueError,match='strict'):
        _validated(replace(request,objectives=(replace(request.objectives[0],
            pointwise=replace(request.objectives[0].pointwise,**changes,strict_lower=True)),)))


def test_json_nested_strict_roundtrip_and_old_payload_rejection():
    service,request,_=boundary_problem()
    frozen=freeze_film_synthesis_request(service,_with_strict(request,lower=(0,)))
    payload=json.loads(json.dumps(film_synthesis_request_to_payload('strict-bound-roundtrip',frozen)))
    assert payload['schema_version']==5
    assert payload['request']['objectives'][0]['pointwise']['strict_lower'] is True
    assert 'strict_lower_bound_objective_indices' not in payload['request']
    _,decoded,_=film_synthesis_request_from_payload(payload)
    assert decoded.objectives[0].pointwise.strict_lower is True
    assert decoded.objectives==frozen.objectives
    old=deepcopy(payload)
    old['schema_version']=1
    old['request']['strict_lower_bound_objective_indices']=[0]
    with pytest.raises(FilmSynthesisPayloadError):
        film_synthesis_request_from_payload(old)
    payload['request']['objectives'][0]['pointwise']['strict_lower']=[True]
    with pytest.raises(FilmSynthesisPayloadError,match='flags must be boolean'):
        film_synthesis_request_from_payload(payload)


def test_strict_cancel_retains_matching_completed_candidate_audit_without_success():
    service,request,_=boundary_problem()
    cancel=Event()
    strict=_with_strict(request,lower=(0,))
    snapshots=[]
    def checkpoint(design,training,validation,audit):
        snapshots.append((design,validation))
        cancel.set()
    result=synthesize_film_design(service,strict,cancel_event=cancel,snapshot_callback=checkpoint)
    assert result.state=='cancelled' and not result.targets_met
    assert snapshots and result.validation_complete
    assert result.candidate_design==snapshots[0][0]
    assert result.validation_score is not None and result.training_score is not None
    assert result.validation_score['design']==result.training_score['design']
    assert result.candidate_provenance['selection']=='completed_training_at_cancel'


def test_serialized_candidate_quality_and_objective_status_honor_explicit_strictness():
    service,request,_=boundary_problem()
    strict=synthesize_film_design(service,_with_strict(request,lower=(0,),max_evaluations=2))
    payload=film_synthesis_result_to_payload('strict-bound-quality',strict)
    assert payload['result']['targets_met'] is False
    assert payload['best_candidate_audit']['available'] is True
    assert payload['best_candidate_audit']['targets_met'] is False
    assert payload['review']['per_objective'][0]['targets_met'] is False
    assert payload['review']['per_objective'][0]['pointwise']['strict_lower'] is True
    assert payload['review']['per_objective'][0]['pointwise']['targets_met'] is False
    inclusive=synthesize_film_design(service,request)
    accepted=film_synthesis_result_to_payload('inclusive-bound-quality',inclusive)
    assert accepted['best_candidate_audit']['targets_met'] is True


def test_json_strict_cancel_quality_uses_matching_audit_and_remains_valid_result():
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result
    service,request,_=boundary_problem()
    cancel=Event()
    result=synthesize_film_design(service,_with_strict(request,lower=(0,)),
        cancel_event=cancel,snapshot_callback=lambda *_args:cancel.set())
    payload=film_synthesis_result_to_payload('strict-bound-cancel-quality',result)
    assert payload['best_candidate_audit']['available'] is True
    assert payload['best_candidate_audit']['targets_met'] is False
    assert payload['result']['candidate_provenance']['validation_matches_candidate'] is True
    assert is_synthesis_result(payload)
