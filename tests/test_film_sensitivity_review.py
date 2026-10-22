"""Independent adversarial physics/replay gates for the sensitivity primitives."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from threading import Event
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_sensitivity as sensitivity
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_sensitivity import (
    FilmSensitivityScenario, FilmThicknessError, analyze_film_sensitivity,
    film_sensitivity_result_to_payload, validate_film_sensitivity_result_payload,
)
from tests.test_film_sensitivity import problem

pytestmark = pytest.mark.algorithm


def test_absorbing_multiobjective_rt_and_error_column_order_match_independent_physics():
    service, request = problem(two=True)
    for name, n, k in [('low', 1.61, .017), ('high', 2.37, .04)]:
        for optical in service.materials.records[name]['optical_sets']:
            optical['n'] = [n, n + .05, n + .02]
            optical['k'] = [k, k * 1.2, k * .8]
    design = deepcopy(request.design_snapshot)
    design['front_layers'][0]['thickness'] = dict(value=.08, unit='um')
    request = replace(request, design_snapshot=design, objectives=(
        FilmOptimizationObjective(475., 720., 13, (0., 31., 76.), 'unpolarized',
            'R', 'value', .15, None, None, 3., .2),
        FilmOptimizationObjective(520., 590., 7, (21., 64.), 'p',
            'T', 'range', None, .1, .95, 7., .3)),
        errors=(FilmThicknessError('high-film', .03, 'relative_fraction'),
                FilmThicknessError('low-film', 2.5)),
        scenarios=(FilmSensitivityScenario('reverse', (-.25, .75), 4.),
                   FilmSensitivityScenario('positive', (.6, .4), 7.)))
    result = analyze_film_sensitivity(service, request)
    assert result.state == 'completed', result.message
    analysis = result.best_analysis
    assert analysis is not None
    assert len(analysis.linear_conditions) == 53
    assert analysis.linear_conditions[0].error_amplitudes_nm == (1.2, 2.5)
    frozen = result.frozen_request.frozen_material_snapshot
    assert frozen is not None
    for index, objective in enumerate(result.frozen_request.objectives):
        for angle in objective.angles_deg:
            snapshot = deepcopy(dict(frozen))
            snapshot['settings'].update(wavelength_min_nm=objective.wavelength_min_nm,
                wavelength_max_nm=objective.wavelength_max_nm,
                samples=objective.wavelength_samples, angle_deg=angle,
                polarization=objective.polarization)
            # Canonical normalized settings include the FIRST objective's step;
            # remove it before requesting a distinct objective's sample count.
            snapshot['settings'].pop('step_nm', None)
            snapshot.pop('sha256')
            snapshot['sha256'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            canonical = simulate_frozen('.', snapshot)[objective.metric]
            actual = [row.nominal_value for row in analysis.linear_conditions
                      if row.objective_index == index and row.angle_deg == angle]
            np.testing.assert_allclose(actual, canonical, atol=3e-13, rtol=2e-12)
    step_nm = 1e-4
    for column, error in enumerate(request.errors):
        samples = []
        for sign in (-1., 1.):
            changed = deepcopy(design)
            layer = next(row for row in changed['front_layers'] if row['id'] == error.layer_id)
            unit_scale = 1000. if layer['thickness']['unit'] == 'um' else 1.
            layer['thickness']['value'] += sign * step_nm / unit_scale
            perturbed = analyze_film_sensitivity(service, replace(request, design_snapshot=changed))
            assert perturbed.state == 'completed', perturbed.message
            assert perturbed.best_analysis is not None
            samples.append(np.array([row.nominal_value
                for row in perturbed.best_analysis.linear_conditions]))
        derivative = [row.derivative_per_nm[column] for row in analysis.linear_conditions]
        np.testing.assert_allclose(derivative, (samples[1] - samples[0]) / (2 * step_nm),
            rtol=1e-5, atol=2e-10)


@pytest.mark.parametrize('target', ['derivative', 'finite_response', 'nominal_summary'])
def test_replay_rejects_numeric_tampering_even_with_unchanged_valid_request_identity(target):
    service, request = problem()
    result = analyze_film_sensitivity(service, request)
    payload = film_sensitivity_result_to_payload(result)
    if target == 'derivative':
        payload['best_analysis']['linear_conditions'][0]['derivative_per_nm'][0] += 1e-6
    elif target == 'finite_response':
        payload['best_analysis']['finite_scenarios'][0]['finite_perturbation_values'][0] += .001
    else:
        payload['best_analysis']['nominal_weighted_rms_error'] += .001
    # A display/schema check is deliberately not a physical trust boundary.
    validate_film_sensitivity_result_payload(payload,
        expected_request=result.frozen_request, replay=False)
    with pytest.raises(ValueError, match='replay'):
        validate_film_sensitivity_result_payload(payload,
            expected_request=result.frozen_request, replay=True)


def test_4097_sample_chunk_boundary_retains_exact_grid_and_replays_full_analysis():
    service, request = problem()
    request = replace(request, scenarios=(), objectives=(replace(request.objectives[0],
        wavelength_min_nm=500., wavelength_max_nm=600., wavelength_samples=4097),))
    result = analyze_film_sensitivity(service, request)
    assert result.state == 'completed', result.message
    assert result.best_analysis is not None
    actual = [row.wavelength_nm for row in result.best_analysis.linear_conditions]
    np.testing.assert_array_equal(actual, np.linspace(500., 600., 4097))
    assert result.forward_evaluations == result.jacobian_evaluations == 2
    validate_film_sensitivity_result_payload(film_sensitivity_result_to_payload(result),
        expected_request=result.frozen_request)


def test_cancel_during_first_finite_scenario_never_retains_incomplete_baseline(monkeypatch):
    service, request = problem()
    cancel = Event()
    checkpoints: list[dict[str, Any]] = []
    original = sensitivity.simulate_with_thickness_jacobian
    calls = []
    def simulate(*args, **kwargs):
        value = original(*args, **kwargs)
        calls.append(value)
        if len(calls) == 2:
            cancel.set()
        return value
    monkeypatch.setattr(sensitivity, 'simulate_with_thickness_jacobian', simulate)
    result = analyze_film_sensitivity(service, request, cancel_event=cancel,
        checkpoint_callback=checkpoints.append)
    assert result.state == result.termination_reason == 'cancelled'
    assert result.best_design is result.best_analysis is result.baseline_analysis is None
    assert not checkpoints
    assert result.evaluations == 1 and result.forward_evaluations == 2
    validate_film_sensitivity_result_payload(film_sensitivity_result_to_payload(result),
        expected_request=result.frozen_request)
