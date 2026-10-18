import copy
import json
import math
import subprocess
import sys

import pytest

from coat_sim.core.ito_electrical import calculate_ito_electrical as calculate


def q(value, unit, u=None, provenance="measured"):
    result = {"value": value, "unit": unit, "provenance": provenance, "source": "fixture measurement"}
    if u is not None:
        result["standard_uncertainty"] = u
    return result


def base():
    return {"thickness": q(100, "nm"), "sheet_resistance": q(20, "ohm/sq")}


def test_rho_conversion_and_input_preservation():
    payload = base()
    before = copy.deepcopy(payload)
    result = calculate(payload)
    assert result["status"] == "solved"
    assert result["quantities"]["resistivity"]["value"] == pytest.approx(2e-4)
    assert result["quantities"]["resistivity"]["si_value"] == pytest.approx(2e-6)
    assert result["quantities"]["resistivity"]["dependencies"] == ["thickness", "sheet_resistance"]
    assert payload == before
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("unit,value", [("m", 1e-7), ("cm", 1e-5), ("mm", 1e-4), ("um", .1), ("nm", 100)])
def test_length_units(unit, value):
    result = calculate({"thickness": q(value, unit), "resistivity": q(2e-6, "ohm*m")})
    assert result["quantities"]["sheet_resistance"]["value"] == pytest.approx(20)


def test_thickness_theory_is_conditional():
    result = calculate({"sheet_resistance": q(20, "ohm/sq"), "resistivity": q(2e-4, "ohm*cm", provenance="theoretical")})
    assert result["quantities"]["thickness"]["value"] == pytest.approx(100)
    assert result["quantities"]["resistivity"]["provenance"] == "theoretical"
    assert "conditional" in result["explanation"]


@pytest.mark.parametrize("payload", [{}, {"sheet_resistance": q(20, "ohm/sq")}])
def test_insufficient_no_constant(payload):
    result = calculate(payload)
    assert result["status"] == "insufficient_data"
    assert "thickness" not in result["quantities"]
    assert "cannot identify" in result["explanation"]


@pytest.mark.parametrize("rho,status,residual", [(2e-4, "consistent", 0), (3e-4, "inconsistent", 1e-6)])
def test_overdetermined(rho, status, residual):
    payload = base() | {"resistivity": q(rho, "ohm*cm")}
    result = calculate(payload)
    assert result["status"] == status
    assert result["consistency"]["residual_ohm_m"] == pytest.approx(residual, abs=1e-20)
    assert result["quantities"]["resistivity"]["value"] == pytest.approx(rho)
    assert not result["derived"]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "100", None, 10**1000])
def test_invalid_physical_values(value):
    with pytest.raises(ValueError):
        calculate(base() | {"thickness": q(value, "nm")})


@pytest.mark.parametrize("field,value", [("unit", "angstrom"), ("source", ""), ("provenance", "derived"), ("standard_uncertainty", -1), ("standard_uncertainty", True), ("unexpected", 3)])
def test_invalid_quantity_contract(field, value):
    thickness = q(100, "nm") | {field: value}
    with pytest.raises(ValueError):
        calculate(base() | {"thickness": thickness})


@pytest.mark.parametrize("payload", [{"operation":"fit"}, {"unknown":1}, {"consistency_relative_tolerance":2}, {"consistency_relative_tolerance":True}, {"uncertainty_assumption":"correlated"}, {"rectangle":{}}])
def test_invalid_contract(payload):
    with pytest.raises(ValueError):
        calculate(payload)


def rectangle():
    return {"resistance": q(100, "ohm", 1), "length": q(50, "mm", .5), "width": q(10, "mm", .1),
            "electrode_boundary":"full_width_opposite_edges", "contact_assumption":"negligible_or_corrected"}


def test_rectangle_and_uncertainty():
    result = calculate({"rectangle":rectangle(), "thickness":q(100, "nm", 1), "uncertainty_assumption":"independent"})
    assert result["quantities"]["sheet_resistance"]["value"] == pytest.approx(20)
    assert result["quantities"]["sheet_resistance"]["standard_uncertainty"] == pytest.approx(20 * math.sqrt(3) * .01)
    assert result["quantities"]["resistivity"]["standard_uncertainty"] == pytest.approx(2e-4 * .02)
    assert result["rectangle_inputs"]["length"]["input_unit"] == "mm"


@pytest.mark.parametrize("change", [{"electrode_boundary":"point_contacts"}, {"contact_assumption":"unknown"}, {"length":q(0,"mm")}])
def test_rectangle_boundaries(change):
    with pytest.raises(ValueError):
        calculate({"rectangle":rectangle() | change})


def test_rectangle_duplicate_rs_rejected():
    with pytest.raises(ValueError):
        calculate(base() | {"rectangle":rectangle()})


def test_uncertainty_requires_explicit_independence_and_all_inputs():
    payload = {"thickness":q(100,"nm", 2), "sheet_resistance":q(20,"ohm/sq", 1)}
    assert "standard_uncertainty" not in calculate(payload)["quantities"]["resistivity"]
    result = calculate(payload | {"uncertainty_assumption":"independent"})
    assert result["quantities"]["resistivity"]["standard_uncertainty"] == pytest.approx(2e-4 * math.hypot(.02,.05))
    payload["sheet_resistance"].pop("standard_uncertainty")
    assert "standard_uncertainty" not in calculate(payload | {"uncertainty_assumption":"independent"})["quantities"]["resistivity"]


def test_division_and_residual_uncertainty():
    payload = {"resistivity":q(2e-4,"ohm*cm", 1e-5), "sheet_resistance":q(20,"ohm/sq", 1), "uncertainty_assumption":"independent"}
    assert calculate(payload)["quantities"]["thickness"]["standard_uncertainty"] == pytest.approx(100 * math.hypot(.05,.05))
    result = calculate(payload | {"thickness":q(100,"nm",2)})
    assert result["consistency"]["standard_uncertainty_ohm_m"] == pytest.approx(math.hypot(1e-7,4e-8,1e-7))


@pytest.mark.parametrize("payload", [base() | {"thickness": q(1e308,"m")}, {"thickness":q(1e-320,"nm")}, {"sheet_resistance":q(1e-300,"ohm/sq"),"resistivity":q(1e300,"ohm*m")}])
def test_numerical_range_rejected(payload):
    with pytest.raises(ValueError):
        calculate(payload)


def test_cli_success_and_failure():
    result = subprocess.run([sys.executable,"-m","coat_sim.ito_cli","solve"], input=json.dumps(base()), text=True,capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"]["status"] == "solved"
    result = subprocess.run([sys.executable,"-m","coat_sim.ito_cli","solve"], input='{"thickness":null}', text=True,capture_output=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
