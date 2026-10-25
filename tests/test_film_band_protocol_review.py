"""Independent regressions for current band protocol admission and capabilities."""
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event

import pytest

from coat_sim.core.film_auto_synthesis import (
    freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
    film_synthesis_result_to_payload,
)
from coat_sim.core.film_synthesis_jobs import is_synthesis_result
from coat_sim.core.film_objective_payload import objective_from_payload
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.scheduler.result_capability import (
    is_best_candidate_result, job_has_viewable_result,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _generic_candidate():
    return dict(run_id="a" * 32, asset=dict(run_id="a" * 32),
        partial_result=dict(schema_version=1, kind="best_candidate", best_so_far=True,
            candidate_verified=True, quality=dict(metric="fake_scalar", value=0.)),
        result_artifacts=[dict(role="bundle", artifact_id="fake-bundle")])


@pytest.mark.parametrize("executor,kind", [
    ("film_synthesis", "coat_sim.film_synthesis_result"),
    ("film_optimization", "coat_sim.film_optimization_result"),
    ("film_structure_optimization", "coat_sim.film_structure_optimization_result"),
])
@pytest.mark.parametrize("schema", [1, 2])
def test_invalid_typed_result_cannot_acquire_generic_scalar_or_bundle_capability(executor, kind, schema):
    result = dict(_generic_candidate(), schema_version=schema, kind=kind)
    assert not job_has_viewable_result(dict(kind=executor, state="succeeded", result=result))
    assert not job_has_viewable_result(dict(kind="simulation", state="succeeded", result=result))
    assert not is_best_candidate_result(result)


def test_film_executor_does_not_accept_untyped_asset_and_generic_executor_retains_its_asset():
    result = _generic_candidate()
    assert not job_has_viewable_result(dict(kind="film_synthesis", state="succeeded", result=result))
    assert job_has_viewable_result(dict(kind="simulation", state="succeeded", result=result))


@pytest.fixture(scope="module")
def completed_cancel_payload():
    service, request = setup_problem(upper=.03)
    request = freeze_film_synthesis_request(service, replace(request,
        seed_designs=(deepcopy(request.environment),), max_evaluations=12))
    event = Event()
    result = synthesize_film_design(None, request, cancel_event=event,
        progress_callback=lambda progress: event.set() if progress.stage == "zero_layer_trial" else None)
    assert result.state == "cancelled"
    payload = film_synthesis_result_to_payload("band-protocol-review", result)
    assert is_synthesis_result(payload)
    return payload


@pytest.mark.parametrize("missing", ["structure_search_seed", "randomize_template_search"])
def test_current_frozen_request_requires_explicit_seed_and_toggle(completed_cancel_payload, missing):
    result = completed_cancel_payload["result"]
    payload = dict(schema_version=5, kind="coat_sim.film_synthesis_request",
        request_id="band-protocol-seed", request=deepcopy(result["frozen_request"]), physical_structures=[])
    payload["request"]["randomize_template_search"] = True
    del payload["request"][missing]
    with pytest.raises(ValueError, match="every field"):
        film_synthesis_request_from_payload(payload)


def test_explicit_zero_seed_survives_current_request_roundtrip(completed_cancel_payload):
    result = completed_cancel_payload["result"]
    payload = dict(schema_version=5, kind="coat_sim.film_synthesis_request",
        request_id="band-protocol-seed-zero", request=deepcopy(result["frozen_request"]), physical_structures=[])
    payload["request"].update(randomize_template_search=True, structure_search_seed=0)
    request_id, request, _ = film_synthesis_request_from_payload(payload)
    assert request.structure_search_seed == 0
    assert film_synthesis_request_to_payload(request_id, request)["request"]["structure_search_seed"] == 0


@pytest.mark.parametrize("field", ["cycles", "evaluations", "validation_evaluations", "failed_evaluations"])
def test_cancelled_training_counters_obey_the_same_frozen_budgets(completed_cancel_payload, field):
    payload = deepcopy(completed_cancel_payload)
    body = payload["result"]
    if field == "cycles":
        body[field] = body["frozen_request"]["max_cycles"] + 1
    elif field == "evaluations":
        body[field] = body["frozen_request"]["max_evaluations"] + 1
    else:
        body[field] = body["evaluations"] + 1
    assert not is_synthesis_result(payload)


@pytest.mark.parametrize("field,value", [
    ("wavelength_samples", True), ("wavelength_samples", 3.0),
    ("wavelength_samples", 1), ("wavelength_min_nm", "500"),
    ("angles_deg", [True]), ("angles_deg", [0., 0.]),
    ("metric", "A"), ("wavelength_max_nm", float("nan")),
])
def test_current_wire_rejects_ambiguous_or_incomplete_physical_band(field, value):
    payload = asdict(FilmOptimizationObjective(500., 600., 3))
    payload[field] = value
    with pytest.raises(ValueError):
        objective_from_payload(payload)


def test_current_request_serialization_uses_json_native_arrays(completed_cancel_payload):
    source = dict(schema_version=5, kind="coat_sim.film_synthesis_request",
        request_id="json-native", request=completed_cancel_payload["result"]["frozen_request"],
        physical_structures=[])
    request_id, request, _ = film_synthesis_request_from_payload(source)
    payload = film_synthesis_request_to_payload(request_id, request)
    assert type(payload["request"]["objectives"]) is list
    assert type(payload["request"]["objectives"][0]["angles_deg"]) is list
    assert type(payload["request"]["growth_quarter_wave_fractions"]) is list


@pytest.mark.parametrize("claim", [True, None, 0, "false"])
def test_finite_audit_cannot_advertise_continuous_certificate(completed_cancel_payload, claim):
    payload = deepcopy(completed_cancel_payload)
    payload["best_candidate_audit"]["continuous_certificate"] = claim
    assert not is_synthesis_result(payload)
