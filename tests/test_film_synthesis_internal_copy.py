"""Internal immutable-error sharing must retain every external copy boundary."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_optimization import FilmConditionError
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def error():
    return FilmConditionError(0,550.,0.,"s","R",.2,None,None,.5,0.,0.)


def candidate(errors=None):
    return dict(design=dict(front_layers=[dict(id="layer",thickness=dict(value=10.,unit="nm"))]),
        score=dict(errors=(error(),) if errors is None else errors,max_error=0.,extra=dict(values=[1.])),
        responses=[dict(actual_values=[.2],jacobian_values_by_layer_id={"layer":[.1]})],
        extra=dict(array=np.array([1.,2.]),values=[dict(flag=True)]))


@pytest.mark.parametrize("score_only", [False,True])
def test_only_checked_frozen_scalar_error_tuple_is_shared_and_values_match(score_only):
    value=candidate()
    source=value["score"] if score_only else value
    copied=synthesis._copy_internal_candidate(source)
    expected=deepcopy(source)
    score=copied if score_only else copied["score"]
    source_score=source if score_only else source["score"]
    assert score["errors"] is source_score["errors"]
    expected_score=expected if score_only else expected["score"]
    assert score == expected_score
    assert score["max_error"] == source_score["max_error"]
    assert score is not source_score
    assert score["extra"] is not source_score["extra"]
    with pytest.raises(FrozenInstanceError):
        score["errors"][0].actual=.9


def test_mutable_design_response_score_and_extra_arrays_have_no_aliases():
    source=candidate()
    copied=synthesis._copy_internal_candidate(source)
    copied["design"]["front_layers"][0]["thickness"]["value"]=99.
    copied["responses"][0]["actual_values"][0]=.9
    copied["responses"][0]["jacobian_values_by_layer_id"]["layer"][0]=.8
    copied["score"]["extra"]["values"].append(2.)
    copied["extra"]["values"][0]["flag"]=False
    copied["extra"]["array"][0]=77.
    assert source["design"]["front_layers"][0]["thickness"]["value"]==10.
    assert source["responses"][0]["actual_values"]==[.2]
    assert source["responses"][0]["jacobian_values_by_layer_id"]["layer"]==[.1]
    assert source["score"]["extra"]["values"]==[1.]
    assert source["extra"]["values"]==[dict(flag=True)]
    np.testing.assert_array_equal(source["extra"]["array"],[1.,2.])
    assert not np.shares_memory(source["extra"]["array"],copied["extra"]["array"])


@pytest.mark.parametrize("kind", ["mutable_field","extra_mutable_field","custom_error","tuple_subclass","list","dict","numpy_scalar"])
def test_unchecked_error_representations_fall_back_to_deepcopy(kind):
    record=error()
    class CustomError(FilmConditionError):
        pass
    class CustomTuple(tuple):
        pass
    if kind=="mutable_field":
        object.__setattr__(record,"actual",[.2])
    elif kind=="extra_mutable_field":
        object.__setattr__(record,"metadata",dict(values=[1.]))
    elif kind=="custom_error":
        record=CustomError(**vars(record))
    elif kind=="numpy_scalar":
        object.__setattr__(record,"actual",np.float64(.2))
    errors: Any=(record,)
    if kind=="tuple_subclass":
        errors=CustomTuple(errors)
    elif kind=="list":
        errors=list(errors)
    elif kind=="dict":
        errors=(dict(vars(record)),)
    source=candidate(errors)
    copied=synthesis._copy_internal_candidate(source)
    assert copied["score"]["errors"] is not source["score"]["errors"]
    assert copied["score"]["errors"][0] is not source["score"]["errors"][0]
    if kind=="mutable_field":
        copied["score"]["errors"][0].actual.append(.3)
        assert vars(record)["actual"]==[.2]
    elif kind=="extra_mutable_field":
        copied["score"]["errors"][0].metadata["values"].append(2.)
        assert vars(record)["metadata"]==dict(values=[1.])


def test_real_snapshot_observer_receives_independent_errors_even_from_shared_internal_archives():
    service,request=setup_problem(upper=1.,low=550.,high=550.,samples=1,materials=("low",))
    request=replace(request,max_cycles=1,max_evaluations=8)
    control=synthesis.synthesize_film_design(service,request)
    snapshots=[]
    def observer(design,training,validation,audit):
        assert training["errors"] is not validation["errors"]
        snapshots.append((design,training,validation,audit))
        # Exercise even reflective mutation: normal frozen assignment is blocked,
        # but external copies must remain independent under this stronger probe.
        object.__setattr__(training["errors"][0],"actual",-50.)
        object.__setattr__(validation["errors"][0],"actual",-60.)
        training["max_error"]=99.
        validation["max_error"]=88.
        design["name"]="observer-owned mutation"
        audit["observer_only"]=[1.]
    observed=synthesis.synthesize_film_design(service,request,snapshot_callback=observer)
    assert snapshots
    assert observed.state==control.state=="completed"
    assert observed.targets_met and control.targets_met
    assert observed.candidate_design==control.candidate_design
    assert observed.training_score==control.training_score
    assert observed.validation_score==control.validation_score
    assert observed.training_score is not None and observed.validation_score is not None
    assert observed.training_score is not snapshots[-1][1]
    assert observed.validation_score is not snapshots[-1][2]
    assert observed.training_score["errors"][0] is not snapshots[-1][1]["errors"][0]
    assert observed.validation_score["errors"][0] is not snapshots[-1][2]["errors"][0]
