"""Explicit engineering-error units, process factors and reproducible clouds."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core import film_sensitivity as sensitivity
from tests.test_film_sensitivity import problem

pytestmark = pytest.mark.algorithm


def three_layer_problem():
    service, request = problem(two=True)
    design = deepcopy(request.design_snapshot)
    third = deepcopy(design["front_layers"][0])
    third.update(id="low-again", thickness=dict(value=30., unit="nm"))
    design["front_layers"].append(third)
    errors = tuple(sensitivity.FilmThicknessError(row["id"], absolute_nm=1., relative_fraction=.02)
                   for row in design["front_layers"])
    return service, replace(request, design_snapshot=design, errors=errors, scenarios=())


@pytest.mark.parametrize("model", ["independent", "common_mode", "material_common_mode", "linear_drift"])
@pytest.mark.parametrize("distribution,semantics", [("bounded_uniform", "bounded_amplitude"), ("gaussian", "standard_deviation")])
def test_process_cloud_reproducibility_explicit_semantics_and_correlation(model, distribution, semantics):
    service, request = three_layer_problem()
    def cloud(seed):
        return sensitivity.generate_film_sensitivity_scenarios(request.design_snapshot, request.errors,
            sample_count=32, seed=seed, error_model=model, distribution=distribution)
    scenarios = cloud(812)
    assert scenarios == cloud(812) and scenarios != cloud(813)
    coefficients = np.asarray([row.coefficients for row in scenarios])
    if distribution == "bounded_uniform":
        assert np.max(np.abs(coefficients)) <= 1.
    else:
        assert np.max(np.abs(coefficients)) > 1.  # Gaussian values never clipped.
    if model == "common_mode":
        np.testing.assert_array_equal(coefficients[:, 0], coefficients[:, 1])
        np.testing.assert_array_equal(coefficients[:, 0], coefficients[:, 2])
    elif model == "material_common_mode":
        np.testing.assert_array_equal(coefficients[:, 0], coefficients[:, 2])
        assert np.any(coefficients[:, 0] != coefficients[:, 1])
    elif model == "linear_drift":
        np.testing.assert_array_equal(coefficients[:, 0], -coefficients[:, 2])
        assert np.all(coefficients[:, 1] == 0.)
    else:
        assert np.any(coefficients[:, 0] != coefficients[:, 1])
    result = sensitivity.analyze_film_sensitivity(service, replace(request, scenarios=scenarios,
        error_model=model, error_amplitude_semantics=semantics, robustness_objective="finite_scenario_expected"))
    assert result.state == "completed", result.message
    payload = sensitivity.film_sensitivity_request_to_payload(result.frozen_request)
    assert sensitivity.film_sensitivity_request_from_payload(payload).scenarios == scenarios
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))


def test_absolute_plus_relative_share_one_factor_and_follow_candidate_physical_thickness():
    service, request = problem()
    error = sensitivity.FilmThicknessError("low-film", magnitude=1., absolute_nm=.5, relative_fraction=.02)
    request = replace(request, errors=(error,))
    result = sensitivity.analyze_film_sensitivity(service, request)
    assert result.best_analysis is not None
    analysis = result.best_analysis
    assert analysis.linear_conditions[0].error_amplitudes_nm == pytest.approx((3.1,))
    assert analysis.finite_scenarios[0].delta_thickness_nm == pytest.approx((-3.1,))
    assert analysis.finite_scenarios[1].delta_thickness_nm == pytest.approx((3.1,))
    changed = deepcopy(request.design_snapshot)
    changed["front_layers"][0]["thickness"]["value"] = 100.
    second = sensitivity.analyze_film_sensitivity(service, replace(request, design_snapshot=changed))
    assert second.best_analysis is not None
    assert second.best_analysis.linear_conditions[0].error_amplitudes_nm == pytest.approx((3.5,))
    sigma = sensitivity.analyze_film_sensitivity(service, replace(request, error_amplitude_semantics="standard_deviation"))
    assert sigma.best_analysis == analysis  # Same explicit factors, distinct declared meaning/cache identity.
    assert sigma.cache_identity != result.cache_identity


def test_linear_drift_uses_full_layer_order_not_optimized_or_error_subset():
    service, request = three_layer_problem()
    request = replace(request, errors=(request.errors[1], request.errors[2]))
    scenarios = sensitivity.generate_film_sensitivity_scenarios(request.design_snapshot, request.errors,
        sample_count=5, seed=8, error_model="linear_drift", distribution="bounded_uniform")
    assert all(row.coefficients[0] == 0. for row in scenarios)
    result = sensitivity.analyze_film_sensitivity(service, replace(request, scenarios=scenarios, error_model="linear_drift"))
    assert result.state == "completed"
    assert result.best_analysis is not None
    derivative = np.asarray(result.best_analysis.linear_conditions[0].derivative_per_nm)
    amplitudes = np.asarray(result.best_analysis.linear_conditions[0].error_amplitudes_nm)
    assert result.best_analysis.linear_sensitivity_norm == pytest.approx(abs((derivative * amplitudes)[1]))


def test_sigma_coefficients_require_explicit_sigma_contract_and_negative_thickness_is_not_clipped():
    service, request = problem()
    scenarios = (sensitivity.FilmSensitivityScenario("two-sigma", (2.,)),)
    with pytest.raises(ValueError, match="standard_deviation"):
        sensitivity.analyze_film_sensitivity(service, replace(request, scenarios=scenarios))
    sigma = sensitivity.analyze_film_sensitivity(service, replace(request, scenarios=scenarios,
        error_amplitude_semantics="standard_deviation"))
    assert sigma.state == "completed"
    failed = sensitivity.analyze_film_sensitivity(service, replace(request,
        errors=(sensitivity.FilmThicknessError("low-film", absolute_nm=41.),),
        scenarios=(sensitivity.FilmSensitivityScenario("minus-two-sigma", (-2.,)),),
        error_amplitude_semantics="standard_deviation"))
    assert failed.state == "failed" and "never clipped" in failed.message


@pytest.mark.parametrize("field,value", [("absolute_nm", -1.), ("relative_fraction", -.01),
    ("absolute_nm", float("nan")), ("relative_fraction", True)])
def test_process_scales_reject_invalid_values(field, value):
    service, request = problem()
    with pytest.raises(ValueError):
        sensitivity.analyze_film_sensitivity(service, replace(request, errors=(replace(request.errors[0], **{field: value}),)))


def test_material_and_drift_mismatch_are_rejected_not_reinterpreted():
    service, request = three_layer_problem()
    for model, coefficients, message in [("material_common_mode", (1., 1., -1.), "within each material"),
                                          ("linear_drift", (-1., .5, 1.), "signed slope")]:
        with pytest.raises(ValueError, match=message):
            sensitivity.analyze_film_sensitivity(service, replace(request, error_model=model,
                scenarios=(sensitivity.FilmSensitivityScenario("bad", coefficients),)))
