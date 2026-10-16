"""Independent malformed-request checks at the frozen queue boundary."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from test_film_structure_preview import _fixture
from coat_sim.core.film_optimization_structure import (
    film_structure_request_from_payload, film_structure_request_to_payload,
)


@pytest.mark.parametrize("scope", ["envelope", "base_request", "limits"])
def test_request_rejects_unknown_fields_in_every_control_scope(scope):
    request, _ = _fixture()
    payload = film_structure_request_to_payload(request)
    target = payload if scope == "envelope" else payload[scope]
    target["unknown_execution_option"] = "unsupported"
    with pytest.raises((TypeError, ValueError)):
        film_structure_request_from_payload(payload)


@pytest.mark.parametrize("scope", ["envelope", "base_request"])
@pytest.mark.parametrize("field", ["solver", "solver_id"])
def test_structure_request_rejects_solver_identifiers(scope, field):
    request, _ = _fixture()
    payload = film_structure_request_to_payload(request)
    target = payload if scope == "envelope" else payload[scope]
    target[field] = "trf"
    with pytest.raises(ValueError, match="solver"):
        film_structure_request_from_payload(payload)


@pytest.mark.parametrize("field,value", [
    ("max_added_layers", True), ("max_added_layers", 2),
    ("growth_cycles", True), ("growth_cycles", 2),
    ("candidate_budget", 2.5), ("position_samples", True),
])
def test_structure_request_rejects_unimplemented_or_malformed_integer_controls(field, value):
    request, _ = _fixture()
    payload = film_structure_request_to_payload(request)
    payload["limits"][field] = value
    with pytest.raises((TypeError, ValueError)):
        film_structure_request_from_payload(payload)


def test_request_schema_version_cannot_be_a_boolean():
    request, _ = _fixture()
    payload = film_structure_request_to_payload(request)
    payload["schema_version"] = True
    with pytest.raises(ValueError):
        film_structure_request_from_payload(payload)


def test_request_roundtrip_retains_every_frozen_control():
    request, _ = _fixture()
    payload = film_structure_request_to_payload(request)
    copied = deepcopy(payload)
    parsed = film_structure_request_from_payload(payload)
    assert film_structure_request_to_payload(parsed) == copied
    assert payload == copied
