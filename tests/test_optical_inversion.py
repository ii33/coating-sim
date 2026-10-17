"""Single-layer inversion against independent Fresnel/characteristic matrices."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any

import numpy as np
import pytest

from coat_sim.core.film_optics import coherent_rta, incoherent_plate_rta
from coat_sim.core.optical_constants import validate_optical_set
from coat_sim.core.optical_inversion import HC_EV_NM, fit_optical_constants


def cauchy(wave, parameters=(1.85, 0.008, 0.00015)):
    wave_um = np.asarray(wave) / 1000
    a, b, c = parameters
    return a + b / wave_um**2 + c / wave_um**4 + 0j


def lorentz(wave, parameters=(3.0, 12.0, 3.4, 0.35)):
    energy = HC_EV_NM / np.asarray(wave)
    eps_inf, amplitude, center, damping = parameters
    return np.sqrt(eps_inf + amplitude / (center**2 - energy**2 - 1j * damping * energy))


def independent_stack(wave, indices, thickness, angle=0, polarization="s"):
    """Test-only scalar characteristic matrix, independent of film_optics code."""
    beta = complex(indices[0]).real * math.sin(math.radians(angle))
    q = np.sqrt(np.asarray(indices, complex)**2 - beta**2 + 0j)
    q = np.where((q.imag < 0) | ((q.imag == 0) & (q.real < 0)), -q, q)
    admittance = q if polarization == "s" else np.asarray(indices)**2 / q
    matrix = np.eye(2, dtype=complex)
    for index, length in enumerate(thickness, 1):
        phase = 2 * math.pi * q[index] * length / wave
        layer = np.array([[np.cos(phase), -1j * np.sin(phase) / admittance[index]],
                          [-1j * admittance[index] * np.sin(phase), np.cos(phase)]])
        matrix = matrix @ layer
    b = matrix[0, 0] + matrix[0, 1] * admittance[-1]
    c = matrix[1, 0] + matrix[1, 1] * admittance[-1]
    denominator = admittance[0] * b + c
    r = (admittance[0] * b - c) / denominator
    t = 2 * admittance[0] / denominator
    return float(abs(r)**2), float(admittance[-1].real / admittance[0].real * abs(t)**2)


def independent_plate(wave, film, thickness, substrate=1.52, angle=0, polarization="s"):
    rf, tf = independent_stack(wave, [1.0, film, substrate], [thickness], angle, polarization)
    # Reverse incidence must preserve the transverse wavevector, not the angle.
    reverse_angle = math.degrees(math.asin(math.sin(math.radians(angle)) / substrate))
    rr, tr = independent_stack(wave, [substrate, film, 1.0], [thickness], reverse_angle, polarization)
    rb, tb = independent_stack(wave, [substrate, 1.0], [], reverse_angle, polarization)
    denominator = 1 - rr * rb
    return rf + tf * tr * rb / denominator, tf * tb / denominator


def make_request(model="cauchy", *, backside="semi_infinite", thickness=95.0,
                 noisy=0.0, channels=("R", "T"), angles=(0, 50), samples=22):
    specs = []
    rng = np.random.default_rng(71)
    for angle_index, angle in enumerate(angles):
        for channel_index, quantity in enumerate(channels):
            # Every spectrum uses its own raw grid, including R versus T.
            wave = np.linspace(420 + 3 * channel_index, 900 - 2 * angle_index,
                               samples + channel_index)
            film = cauchy(wave) if model == "cauchy" else lorentz(wave)
            values = []
            for point, index in zip(wave, film, strict=True):
                if backside == "incoherent":
                    r, t = independent_plate(point, index, thickness, angle=angle,
                                              polarization="p" if angle else "s")
                elif backside == "coherent":
                    r, t = independent_stack(point, [1, index, 1.52, 1], [thickness, 700],
                                              angle=angle, polarization="p" if angle else "s")
                else:
                    r, t = independent_stack(point, [1, index, 1.52], [thickness],
                                              angle=angle, polarization="p" if angle else "s")
                value = r if quantity == "R" else t
                values.append(float(np.clip(value + rng.normal(0, noisy), 0, 1)))
            specs.append({"id": f"{quantity}-{angle}", "quantity": quantity,
                          "wavelengths": wave.tolist(), "wavelength_unit": "nm",
                          "values": values, "value_unit": "fraction", "angle_deg": angle,
                          "polarization": "p" if angle else "s", "measurement": "absolute_specular",
                          "transmission_definition": "into_substrate" if backside == "semi_infinite" else "external",
                          "source": "Synthetic independent matrix benchmark"})
    return {"model": model, "transparent_region": model == "cauchy",
            "film_thickness_nm": thickness,
            "substrate": {"n": 1.52, "k": 0.0, "thickness_nm": 700},
            "geometry": {"backside": backside, "incident_n": 1.0, "exit_n": 1.0},
            "spectra": specs, "optimization": {"multistarts": 2, "max_nfev": 220, "seed": 0}}


def codes(result):
    return {item["code"] for item in result["warnings"]}


def assert_finite_json(result):
    json.dumps(result, allow_nan=False)


def test_forward_solver_matches_independent_quarter_wave_and_absorbing_matrix():
    r, t = independent_stack(600, [1, 2, 1.5], [75])
    assert r == pytest.approx(((1.5 - 4) / (1.5 + 4))**2, abs=1e-13)
    assert r + t == pytest.approx(1, abs=1e-13)
    for polarization in ("s", "p"):
        for angle in (0, 47):
            r, t = independent_stack(510, [1, 2.1 + 0.18j, 1.52], [83], angle, polarization)
            production = coherent_rta(510, [1, 2.1 + 0.18j, 1.52], [83],
                                      angle_deg=angle, polarization=polarization)
            assert production["R"] == pytest.approx(r, abs=1e-12)
            assert production["T"] == pytest.approx(t, abs=1e-12)


def test_transparent_shared_model_recovers_n_and_original_grids_without_writing(tmp_path, monkeypatch):
    request = make_request()
    before = deepcopy(request)
    monkeypatch.chdir(tmp_path)
    result = fit_optical_constants(request)
    assert result["ok"], result
    assert result["rmse"] < 1e-8
    expected = cauchy(result["nk"]["wavelength_nm"])
    assert np.max(abs(np.asarray(result["nk"]["n"]) - expected.real)) < 1e-6
    assert result["nk"]["k"] == [0.0] * len(expected)
    assert result["parameters"]["A"] == pytest.approx(1.85, abs=1e-5)
    assert result["parameters"]["B"] == pytest.approx(0.008, abs=1e-5)
    assert result["parameters"]["C"] == pytest.approx(0.00015, abs=1e-6)
    assert result["request"] == request == before
    assert result["saved"] is False and result["provenance_required_before_save"]
    assert list(tmp_path.iterdir()) == []
    for original, spectrum in zip(request["spectra"], result["spectra"], strict=True):
        assert spectrum["wavelength_nm"] == original["wavelengths"]
        assert spectrum["original"] == original
        assert len(spectrum["predicted_fraction"]) == len(original["values"])
        assert np.max(abs(np.asarray(spectrum["residual_fraction"]))) < 1e-7
    assert "non_unique" in codes(result)
    assert result["diagnostics"]["non_unique"] is True
    assert len(result["diagnostics"]["multistarts"]) == 2
    validate_optical_set(result["optical_set"])
    assert_finite_json(result)


def test_absorbing_lorentz_recovers_passive_nk_against_independent_matrix():
    request = make_request("lorentz", thickness=63)
    result = fit_optical_constants(request)
    assert result["ok"], result
    assert result["rmse"] < 2e-7
    expected = lorentz(result["nk"]["wavelength_nm"])
    actual = np.asarray(result["nk"]["n"]) + 1j * np.asarray(result["nk"]["k"])
    assert np.max(abs(actual - expected)) < 3e-5
    assert np.min(np.asarray(result["nk"]["k"])) > 0
    assert "limited_lorentz_model" in codes(result)
    assert_finite_json(result)


@pytest.mark.parametrize("backside", ["incoherent", "coherent"])
def test_finite_substrate_backside_is_included_in_fit(backside):
    request = make_request(backside=backside, angles=(0, 40), samples=17)
    result = fit_optical_constants(request)
    assert result["ok"], result
    assert result["rmse"] < 1e-7
    assert result["structure"]["substrate_thickness_nm"] == 700
    if backside == "coherent":
        assert "coherent_substrate_resolution" in codes(result)
    wave = request["spectra"][0]["wavelengths"][4]
    index = cauchy(np.asarray([wave]))[0]
    front_only = coherent_rta(wave, [1, index, 1.52], [95], polarization="s")
    predicted = result["spectra"][0]["predicted_fraction"][4]
    assert abs(predicted - front_only["R"]) > 0.002


def test_incoherent_backside_reference_is_independent_and_contains_multiple_reflections():
    for angle in (0, 45):
        r, t = independent_plate(630, 1.9 + 0.07j, 90, angle=angle)
        production = incoherent_plate_rta(630, 1, 1.52, 1, front_indices=[1.9 + 0.07j],
                                          front_thickness_nm=[90], substrate_thickness_nm=1e6,
                                          angle_deg=angle, polarization="s")
        assert production["R"] == pytest.approx(r, abs=1e-12)
        assert production["T"] == pytest.approx(t, abs=1e-12)


def test_noisy_absorption_returns_residuals_and_not_false_uniqueness():
    result = fit_optical_constants(make_request("lorentz", thickness=63, noisy=0.001))
    assert result["ok"] and 0.0005 < result["rmse"] < 0.002
    expected = lorentz(result["nk"]["wavelength_nm"])
    actual = np.asarray(result["nk"]["n"]) + 1j * np.asarray(result["nk"]["k"])
    assert np.max(abs(actual - expected)) < 0.03
    assert "non_unique" in codes(result)
    assert result["diagnostics"]["condition_number"] is None or result["diagnostics"]["condition_number"] > 0


def test_wrong_thickness_is_not_silently_fitted_or_declared_successful_physics():
    request = make_request()
    request["film_thickness_nm"] = 25
    request["optimization"]["acceptance_rmse"] = 0.003
    result = fit_optical_constants(request)
    assert result["ok"]
    assert "fixed_thickness" in codes(result) and "poor_fit" in codes(result)
    assert result["structure"]["film_thickness_nm"] == 25
    assert result["rmse"] > 0.003


def test_single_reflectance_is_allowed_only_as_explicitly_warned_model_estimate():
    result = fit_optical_constants(make_request(channels=("R",), angles=(0,)))
    assert result["ok"] and result["rmse"] < 1e-7
    assert {"reflectance_only", "normal_incidence_ambiguity", "non_unique"} <= codes(result)
    assert next(w for w in result["warnings"] if w["code"] == "reflectance_only")["severity"] == "strong"


def test_normal_incidence_r_plus_t_still_warns_about_ambiguity():
    result = fit_optical_constants(make_request(angles=(0,)))
    assert result["ok"]
    assert "normal_incidence_ambiguity" in codes(result) and "non_unique" in codes(result)
    assert "reflectance_only" not in codes(result)


def test_wrong_transparent_model_cannot_explain_absorbing_r_plus_t():
    request = make_request("lorentz", thickness=63)
    request["model"] = "cauchy"
    request["transparent_region"] = True
    request["optimization"]["acceptance_rmse"] = 0.005
    result = fit_optical_constants(request)
    assert result["ok"] and result["rmse"] > 0.005
    assert "poor_fit" in codes(result) and "transparent_model_assumption" in codes(result)


def test_percent_micrometer_inputs_and_uncertainties_use_explicit_units():
    request = make_request(angles=(0,), samples=13)
    reference = fit_optical_constants(request)
    for record in request["spectra"]:
        record["wavelengths"] = [value / 1000 for value in record["wavelengths"]]
        record["wavelength_unit"] = "um"
        record["values"] = [value * 100 for value in record["values"]]
        record["value_unit"] = "%"
        record["sigma"] = 0.1
    converted = fit_optical_constants(request)
    assert converted["ok"] and converted["rmse"] < 1e-7
    assert np.asarray(converted["nk"]["n"]) == pytest.approx(reference["nk"]["n"], abs=2e-6)
    assert converted["request"]["spectra"][0]["value_unit"] == "%"


def test_missing_substrate_k_is_rejected_unless_explicitly_assumed_lossless():
    request = make_request(angles=(0,), samples=10)
    request["substrate"]["k"] = None
    refused = fit_optical_constants(request)
    assert refused["status"] == "invalid_request" and refused["error"] == "unknown_substrate_k"
    request["substrate"]["assume_lossless"] = True
    accepted = fit_optical_constants(request)
    assert accepted["ok"] and "substrate_lossless_assumption" in codes(accepted)


def test_substrate_dataset_preserves_unknown_k_and_forbids_extrapolation():
    request = make_request(angles=(0,), samples=9)
    record: dict[str, Any] = {"id": "sub", "name": "Known substrate n", "kind": "tabulated",
              "wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 1000,
              "wavelengths": [400, 1000], "n": [1.52, 1.52], "k": None}
    request["substrate"] = {"optical_set": record, "thickness_nm": 700}
    result = fit_optical_constants(request)
    assert result["error"] == "unknown_substrate_k"
    request["substrate"]["assume_lossless"] = True
    result = fit_optical_constants(request)
    assert result["ok"] and result["request"]["substrate"]["optical_set"]["k"] is None
    record["wavelength_min"] = 500
    record["wavelengths"][0] = 500
    assert fit_optical_constants(request)["error"] == "substrate_out_of_range"


@pytest.mark.parametrize("change,error", [
    (lambda r: r.update(transparent_region=False), "transparency_required"),
    (lambda r: r["geometry"].pop("backside"), "backside_required"),
    (lambda r: r["spectra"][0].update(measurement="total"), "unsupported_measurement"),
    (lambda r: r["spectra"][1].update(measurement="bare_substrate_normalized"), "unsupported_measurement"),
    (lambda r: r["spectra"][1].update(transmission_definition="external"), "transmission_geometry_mismatch"),
    (lambda r: r["spectra"][0].update(wavelength_unit="m"), "invalid_units"),
    (lambda r: r["spectra"][0].update(value_unit="auto"), "invalid_units"),
    (lambda r: r["spectra"][0]["values"].__setitem__(0, 1.1), "nonphysical_measurement"),
    (lambda r: r["spectra"][0]["wavelengths"].__setitem__(0, 5000), "invalid_wavelength_grid"),
    (lambda r: r["spectra"][0]["values"].__setitem__(0, float("nan")), "invalid_number"),
    (lambda r: r.update(film_thickness_nm=True), "invalid_number"),
    (lambda r: r.update(parameter_bounds={"A": [0.5, 5]}), "invalid_parameter_bounds"),
    (lambda r: r["optimization"].update(multistarts=100), "invalid_number"),
    (lambda r: r.update(model="pointwise"), "unsupported_model"),
])
def test_invalid_inputs_fail_without_candidate(change, error):
    request = make_request(angles=(0,), samples=5)
    change(request)
    result = fit_optical_constants(request)
    assert not result["ok"] and result["status"] == "invalid_request", result
    assert result["error"] == error
    assert "optical_set" not in result


def test_finite_plate_requires_known_positive_thickness_and_external_t_definition():
    request = make_request(backside="incoherent", samples=5)
    request["substrate"].pop("thickness_nm")
    assert fit_optical_constants(request)["status"] == "invalid_request"
    request["substrate"]["thickness_nm"] = 700
    request["spectra"][1]["transmission_definition"] = "into_substrate"
    assert fit_optical_constants(request)["error"] == "transmission_geometry_mismatch"


def test_cancellation_inside_residual_immediately_returns_no_partial_dataset():
    request = make_request("lorentz")
    flag = {"cancel": False}
    events = []

    def progress(event):
        events.append(event)
        if event["stage"] == "fitting":
            flag["cancel"] = True

    result = fit_optical_constants(request, cancelled=lambda: flag["cancel"], progress=progress)
    assert result["status"] == "cancelled" and not result["ok"]
    assert "optical_set" not in result and "nk" not in result
    assert len(events) == 1 and events[0]["evaluations"] == 1
    result = fit_optical_constants(request, cancelled=lambda: True)
    assert result["status"] == "cancelled"


def test_tight_bounds_and_low_data_report_limits_without_nonfinite_diagnostics():
    request = make_request(angles=(0,), channels=("R",), samples=2)
    request["parameter_bounds"] = {"A": [1.0, 1.2]}
    request["optimization"]["max_nfev"] = 20
    result = fit_optical_constants(request)
    assert result["ok"]
    assert "insufficient_independent_data" in codes(result)
    assert len(result["diagnostics"]["multistarts"]) == 2
    assert_finite_json(result)


def test_invalid_unhashable_values_are_input_failures_not_numerical_failures():
    for change in (lambda r: r.update(model=[]),
                   lambda r: r["spectra"][0].update(quantity={}),
                   lambda r: r["geometry"].update(backside=[])):
        request = make_request(angles=(0,), samples=5)
        change(request)
        result = fit_optical_constants(request)
        assert result["status"] == "invalid_request" and not result["ok"]


def test_transmittance_only_and_noisy_mixed_weighted_spectra_keep_model_estimate_label():
    request = make_request(angles=(0,), channels=("T",), samples=15)
    request["spectra"][0]["sigma"] = [0.002] * 15
    result = fit_optical_constants(request)
    assert result["ok"] and result["rmse"] < 1e-7
    assert "transmittance_only" in codes(result)
    assert result["evidence_type"] == "model_estimate" and result["unique_solution"] is False


def test_absorbing_finite_substrate_propagation_is_not_assumed_lossless():
    request = make_request(backside="incoherent", angles=(0,), samples=14)
    request["substrate"].update(k=2e-5, thickness_nm=1e6)
    for spectrum in request["spectra"]:
        wave = np.asarray(spectrum["wavelengths"])
        reference = incoherent_plate_rta(wave, 1, 1.52 + 2e-5j, 1,
                                         front_indices=cauchy(wave)[:, None],
                                         front_thickness_nm=[95], substrate_thickness_nm=1e6,
                                         polarization="s")
        spectrum["values"] = reference[spectrum["quantity"]]
    result = fit_optical_constants(request)
    assert result["ok"] and result["rmse"] < 1e-7
    assert result["structure"]["substrate"]["k"] == 2e-5
    assert "substrate_lossless_assumption" not in codes(result)


def test_substrate_source_metadata_is_bounded():
    request = make_request(angles=(0,), samples=5)
    request["substrate"]["source"] = "x" * 2001
    result = fit_optical_constants(request)
    assert result["status"] == "invalid_request" and result["error"] == "invalid_text"


def test_hard_residual_budget_includes_jacobian_calls(monkeypatch):
    from coat_sim.core import optical_inversion

    calls = []

    def over_budget(residual, start, **kwargs):
        while True:
            calls.append(1)
            residual(start)

    monkeypatch.setattr(optical_inversion, "least_squares", over_budget)
    request = make_request(angles=(0,), samples=5)
    request["optimization"]["max_nfev"] = 10
    result = fit_optical_constants(request)
    assert not result["ok"] and result["status"] == "failed"
    runs = result["diagnostics"]["multistarts"]
    assert [run["error"] for run in runs] == ["residual_budget_exhausted"] * 2
    assert [run["residual_evaluations"] for run in runs] == [44, 44]
    assert len(calls) == 90


def test_unpolarized_measurement_matches_independent_s_p_intensity_average():
    request = make_request(angles=(43,), samples=15)
    for spectrum in request["spectra"]:
        spectrum["polarization"] = "unpolarized"
        channel = 0 if spectrum["quantity"] == "R" else 1
        spectrum["values"] = [float(np.mean([
            independent_stack(point, [1, index, 1.52], [95], 43, polarization)[channel]
            for polarization in ("s", "p")]))
            for point, index in zip(spectrum["wavelengths"], cauchy(spectrum["wavelengths"]),
                                    strict=True)]
    result = fit_optical_constants(request)
    assert result["ok"] and result["rmse"] < 1e-7
    assert all(spectrum["polarization"] == "unpolarized" for spectrum in result["spectra"])


@pytest.mark.parametrize("tabulated", [False, True])
def test_lossless_permission_does_not_mislabel_or_replace_known_substrate_k(tabulated):
    request = make_request(angles=(0,), samples=7)
    if tabulated:
        request["substrate"] = {"assume_lossless": True, "optical_set": {
            "id": "sub", "name": "Known absorbing substrate", "kind": "tabulated",
            "wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 1000,
            "wavelengths": [400, 1000], "n": [1.52, 1.52], "k": [0.02, 0.02]}}
    else:
        request["substrate"].update(k=0.02, assume_lossless=True)
    for spectrum in request["spectra"]:
        channel = 0 if spectrum["quantity"] == "R" else 1
        spectrum["values"] = [independent_stack(point, [1, index, 1.52 + 0.02j], [95])[channel]
                              for point, index in zip(spectrum["wavelengths"],
                                                      cauchy(spectrum["wavelengths"]), strict=True)]
    result = fit_optical_constants(request)
    assert result["ok"] and result["rmse"] < 1e-7
    assert "substrate_lossless_assumption" not in codes(result)
    expected = cauchy(result["nk"]["wavelength_nm"])
    assert np.max(abs(np.asarray(result["nk"]["n"]) - expected.real)) < 1e-5
    if tabulated:
        assert result["structure"]["substrate"]["optical_set"]["k"] == [0.02, 0.02]
    else:
        assert result["structure"]["substrate"]["k"] == 0.02
