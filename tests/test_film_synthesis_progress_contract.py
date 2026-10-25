"""Current v4 opt-in policy is exact across core, GUI, wire, MCP and queue."""

from dataclasses import asdict, replace
from copy import deepcopy
import json

import pytest

from coat_sim.core.capabilities import CapabilityError
from coat_sim.core.film_auto_synthesis import _validated, freeze_film_synthesis_request
from coat_sim.core.film_synthesis_progress import HybridProgressPolicy
from coat_sim.core.film_synthesis_io import (
    SCHEMA_VERSION,
    FilmSynthesisPayloadError,
    film_synthesis_request_from_payload,
    film_synthesis_request_to_payload,
    prepare_film_synthesis_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload,
    canonical_parameters,
    deserialize_job_payload,
)
from coat_sim.core.mcp_contracts import FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, validate_mcp_arguments
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_options
from tests.test_film_synthesis_jobs import _request

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("policy", [None, HybridProgressPolicy(2, 0.01, 0.02, 2)])
def test_current_freeze_json_mcp_and_queue_roundtrip(policy):
    request = freeze_film_synthesis_request(
        None, replace(_request(), objective_progress_policy=policy)
    )
    payload = json.loads(json.dumps(film_synthesis_request_to_payload("progress", request)))
    assert SCHEMA_VERSION == payload["schema_version"] == 4
    assert payload["request"]["objective_progress_policy"] == (
        None if policy is None else asdict(policy)
    )
    validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {"payload": payload})
    assert film_synthesis_request_from_payload(payload)[1] == request
    planned, _ = prepare_film_synthesis_payload(None, payload)
    assert planned == payload
    job = build_job_payload(canonical_parameters(None, {"payload": payload}))
    assert job["schema_version"] == 4 and deserialize_job_payload(job)[1] == request


def test_gui_default_is_explicit_null_and_current_policy_survives():
    assert synthesis_options({})["objective_progress_policy"] is None
    policy = HybridProgressPolicy(3, 0.01, 0.02, 2)
    assert synthesis_options({"objective_progress_policy": asdict(policy)})[
        "objective_progress_policy"
    ] == asdict(policy)
    assert json.loads(json.dumps(synthesis_options({"objective_progress_policy": asdict(policy)})))[
        "objective_progress_policy"
    ] == asdict(policy)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_historical_synthesis_versions_are_not_decoded(version):
    payload = film_synthesis_request_to_payload("old", _request())
    payload["schema_version"] = version
    original = deepcopy(payload)
    with pytest.raises(FilmSynthesisPayloadError, match="current schema 4.*no migration"):
        film_synthesis_request_from_payload(payload)
    assert payload == original


def test_missing_or_unknown_policy_fields_are_rejected():
    payload = film_synthesis_request_to_payload("bad", _request())
    del payload["request"]["objective_progress_policy"]
    with pytest.raises(FilmSynthesisPayloadError, match="every field"):
        film_synthesis_request_from_payload(payload)
    with pytest.raises(CapabilityError):
        validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {"payload": payload})
    payload["request"]["objective_progress_policy"] = {}
    with pytest.raises(FilmSynthesisPayloadError, match="exactly"):
        film_synthesis_request_from_payload(payload)
    with pytest.raises(CapabilityError):
        validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {"payload": payload})


def test_policy_cannot_silently_claim_legacy_search_handoff():
    with pytest.raises(ValueError, match="only by hybrid"):
        _validated(
            replace(
                _request(),
                search_strategy="legacy",
                objective_progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
            )
        )
