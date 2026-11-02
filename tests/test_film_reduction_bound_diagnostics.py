from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core.film_optimization import FilmThicknessVariable
from coat_sim.core.film_reduction import (
    freeze_film_reduction_request,
    reduce_film_design,
    validate_film_reduction_candidate_topology,
)
from tests.test_film_reduction import Service, layer, request


OLD_MESSAGE = "retained selected layer violates its own bounds"


def _with_bounds(source, side, identifier, lower, upper):
    variable = FilmThicknessVariable(side, identifier, lower, upper)
    return replace(source, base_request=replace(source.base_request, variables=(variable,)))


def test_source_below_lower_bound_fails_before_any_simulation_without_modifying_input():
    source = _with_bounds(request(), "front", "a", 25.0, 200.0)
    original = deepcopy(source)
    service = Service()
    with pytest.raises(ValueError) as exc:
        reduce_film_design(service, source)
    message = str(exc.value)
    assert OLD_MESSAGE in message
    assert "正面源第 1 层" in message
    assert "当前物理厚度为 20.0 nm" in message
    assert "下限为 25.0 nm" in message and "上限为 200.0 nm" in message
    assert "请先修复" in message and "检查" in message
    assert service.calls == []
    assert source == original


@pytest.mark.parametrize("side, side_name", [("front", "正面"), ("back", "背面")])
def test_above_upper_reports_original_side_layer_number_after_deletion(side, side_name):
    identifiers = ("hash_1234567890", "hash_abcdef0123")
    layers = [layer(identifier) for identifier in identifiers]
    source = request(rows=layers if side == "front" else [],
                     back=layers if side == "back" else None)
    design = deepcopy(source.base_request.design_snapshot)
    if side == "back":
        design["substrate"]["sides"] = "double"
    source = replace(source, base_request=replace(source.base_request, design_snapshot=design,
        variables=(FilmThicknessVariable(side, identifiers[0], 0.0, 100.0),
                   FilmThicknessVariable(side, identifiers[1], 10.0, 25.0))))
    frozen = freeze_film_reduction_request(Service(), source)
    candidate = deepcopy(dict(frozen.base_request.design_snapshot))
    candidate[side + "_layers"] = [candidate[side + "_layers"][1]]
    candidate[side + "_layers"][0]["thickness"]["value"] = 26.0
    with pytest.raises(ValueError) as exc:
        validate_film_reduction_candidate_topology(frozen, candidate)
    message = str(exc.value)
    assert OLD_MESSAGE in message
    assert f"{side_name}源第 2 层" in message
    assert "当前物理厚度为 26.0 nm" in message
    assert "下限为 10.0 nm" in message and "上限为 25.0 nm" in message
    assert not any(identifier in message for identifier in identifiers)


@pytest.mark.parametrize("lower, upper", [(20.0, 25.0), (10.0, 20.0), (20.0, None)])
def test_exact_bounds_are_accepted(lower, upper):
    service = Service()
    source = _with_bounds(request(), "front", "a", lower, upper)
    frozen = freeze_film_reduction_request(service, source)
    validate_film_reduction_candidate_topology(frozen, frozen.base_request.design_snapshot)
    assert service.calls == []


def test_no_upper_bound_is_explicit_in_diagnostic():
    source = _with_bounds(request(), "front", "a", 25.0, None)
    with pytest.raises(ValueError) as exc:
        freeze_film_reduction_request(Service(), source)
    assert "下限为 25.0 nm，上限为 无上界" in str(exc.value)


def test_65_layer_report_keeps_source_layer_33_value_and_bound():
    rows = [layer(f"secret-hash-{i}", 4.641560626997322 if i == 33 else 20.0)
            for i in range(1, 66)]
    source = _with_bounds(request(rows), "front", "secret-hash-33", 6.1, None)
    service = Service()
    with pytest.raises(ValueError) as exc:
        freeze_film_reduction_request(service, source)
    message = str(exc.value)
    assert "正面源第 33 层" in message
    assert "4.641560626997322 nm" in message and "下限为 6.1 nm" in message
    assert "secret-hash" not in message
    assert service.calls == []


@pytest.mark.parametrize("thickness, expected_nm", [
    ({"value": 0.020, "unit": "um"}, 20.0),
    ({"value": 0.2, "unit": "qw"}, 0.2 * 550.0 / (4.0 * 1.2)),
])
def test_diagnostic_reports_converted_physical_nm(thickness, expected_nm):
    row = layer("private-id")
    row["thickness"] = thickness
    source = _with_bounds(request([row]), "front", "private-id", 25.0, None)
    with pytest.raises(ValueError) as exc:
        freeze_film_reduction_request(Service(), source)
    assert f"当前物理厚度为 {expected_nm} nm" in str(exc.value)
    assert "private-id" not in str(exc.value)
