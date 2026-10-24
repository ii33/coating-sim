"""Published raw coverage must keep identical types across JSON persistence."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from typing import Any

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, FilmObjectiveCoverage
from coat_sim.core.film_objective_payload import coverage_from_payload, coverage_to_payload
from coat_sim.core.film_optimization import optimize_film_design
from coat_sim.core.film_optimization_job import serialize_checkpoint, serialize_request, serialize_result
from coat_sim.core.film_optimization_structure import (
    film_structure_checkpoint_to_payload, film_structure_request_to_payload,
    film_structure_result_from_payload, film_structure_result_to_payload,
    optimize_film_design_with_structure,
)
from tests.film_performance_fixtures import fixture
from tests.test_film_optimization_forward_cache import _request
from tests.test_film_optimization_structure import _request as structure_request, _service


pytestmark = pytest.mark.algorithm


def _assert_json_native(value: Any, path: str = "payload") -> None:
    if type(value) is dict:
        assert all(type(key) is str for key in value), path
        for key, item in value.items():
            _assert_json_native(item, path + "." + key)
    elif type(value) is list:
        for index, item in enumerate(value):
            _assert_json_native(item, path + f"[{index}]")
    else:
        assert value is None or type(value) in (bool, int, float, str), (path, type(value))


def _assert_exact_roundtrip(payload: dict[str, Any]) -> None:
    _assert_json_native(payload)
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


@pytest.mark.parametrize("mapping", [False, True])
def test_coverage_wire_arrays_are_native_detached_and_restore_immutable_evidence(mapping):
    row = FilmObjectiveCoverage(2, 35., (500., 550., 600.), (.1, .2, .3))
    source = asdict(row) if mapping else row
    payload = coverage_to_payload(source)
    _assert_exact_roundtrip(payload)
    assert payload["wavelength_nm"] == [500., 550., 600.]
    assert payload["actual_values"] == [.1, .2, .3]
    assert coverage_from_payload(payload) == row
    payload["wavelength_nm"][0] = 499.
    payload["actual_values"][0] = .9
    assert row.wavelength_nm == (500., 550., 600.)
    assert row.actual_values == (.1, .2, .3)
    if mapping:
        assert source == asdict(row)


@pytest.fixture(scope="module")
def ordinary_wire():
    service, design, frozen = fixture(layers=1)
    request = _request(design, frozen, budget=2)
    objective = replace(request.objectives[0], angles_deg=(0., 35.),
        pointwise=FilmObjectiveConstraint(enabled=False),
        mean=FilmObjectiveConstraint(lower_bound=0., upper_bound=None, strict_lower=True))
    request = replace(request, objectives=(objective,))
    checkpoints: list[dict[str, Any]] = []
    result = optimize_film_design(service, request, checkpoint_callback=checkpoints.append)
    assert checkpoints and result.validation_raw_coverage
    return request, result, checkpoints


def _forbid_physics(*_args, **_kwargs):
    pytest.fail("serialization must not evaluate optical physics")


def test_ordinary_request_checkpoint_and_result_are_json_native_without_physics(ordinary_wire, monkeypatch):
    from coat_sim.core import film_design_service, film_optimization_derivatives

    request, result, checkpoints = ordinary_wire
    monkeypatch.setattr(film_design_service.FilmDesignService, "simulate", _forbid_physics)
    monkeypatch.setattr(film_optimization_derivatives, "simulate_with_thickness_jacobian", _forbid_physics)
    _assert_exact_roundtrip(serialize_request(request))
    for checkpoint in checkpoints:
        _assert_exact_roundtrip(checkpoint)
        wire = serialize_checkpoint(request, checkpoint)
        _assert_exact_roundtrip(wire)
        assert wire["candidate"]["raw_coverage"] == checkpoint["raw_coverage"]
    wire = serialize_result(request, result)
    _assert_exact_roundtrip(wire)
    assert [coverage_from_payload(row) for row in wire["candidate"]["raw_coverage"]] == list(result.raw_coverage)
    assert [coverage_from_payload(row) for row in wire["solver_validation"]["validation_raw_coverage"]] == list(
        result.validation_raw_coverage)
    assert wire["request"]["objectives"][0]["mean"]["strict_lower"] is True
    assert wire["request"]["frozen_material_snapshot"] == request.frozen_material_snapshot
    assert type(result.raw_coverage[0].wavelength_nm) is tuple
    assert type(result.raw_coverage[0].actual_values) is tuple


@pytest.fixture(scope="module")
def structure_wire():
    service, design = _service()
    request = structure_request(design, mode="growth_only", max_evaluations=2)
    objective = replace(request.base_request.objectives[0],
        pointwise=FilmObjectiveConstraint(enabled=False),
        mean=FilmObjectiveConstraint(lower_bound=0., upper_bound=None, strict_lower=True))
    request = replace(request, base_request=replace(request.base_request, objectives=(objective,)))
    result = optimize_film_design_with_structure(service, request)
    assert result.frozen_request is not None and result.validation_raw_coverage
    return result.frozen_request, result


def test_structure_request_checkpoint_and_result_are_json_native_without_physics(structure_wire, monkeypatch):
    from coat_sim.core import film_optimization_structure as structure

    request, result = structure_wire
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", _forbid_physics)
    _assert_exact_roundtrip(film_structure_request_to_payload(request))
    for serializer in (film_structure_checkpoint_to_payload, film_structure_result_to_payload):
        wire = serializer(request, result)
        _assert_exact_roundtrip(wire)
        restored = film_structure_result_from_payload(json.loads(json.dumps(wire)))
        assert restored.raw_coverage == result.raw_coverage
        assert restored.validation_raw_coverage == result.validation_raw_coverage
        assert wire["request"]["base_request"]["objectives"][0]["mean"]["strict_lower"] is True
        assert wire["request"]["base_request"]["frozen_material_snapshot"] == request.base_request.frozen_material_snapshot
    assert type(result.raw_coverage[0].wavelength_nm) is tuple
    assert type(result.raw_coverage[0].actual_values) is tuple


def test_structure_split_metadata_wire_arrays_are_detached(structure_wire, monkeypatch):
    """Representation oracle only; this synthetic metadata is not a physical claim."""
    from coat_sim.core import film_optimization_structure as structure

    request, result = structure_wire
    group = dict(layer_ids=("left-host", "right-host"), fractions=(.25, .75))
    metadata = dict(result.topology_audit, split_groups={"host": group})
    result = replace(result, topology_audit=metadata)
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", _forbid_physics)
    wire = film_structure_result_to_payload(request, result)
    _assert_exact_roundtrip(wire)
    split = wire["structure"]["topology_audit"]["split_groups"]["host"]
    assert split == dict(layer_ids=["left-host", "right-host"], fractions=[.25, .75])
    split["layer_ids"][0] = "changed"
    split["fractions"][0] = .5
    assert group == dict(layer_ids=("left-host", "right-host"), fractions=(.25, .75))
