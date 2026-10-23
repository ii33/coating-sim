"""Display-only candidate events must not become sensitivity search inputs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_candidate_display as display
from coat_sim.core import film_ideal_sensitivity as ideal
from coat_sim.core import film_ideal_sensitivity_v2 as ideal_v2
from coat_sim.core import film_sensitivity as sensitivity
from coat_sim.core import film_sensitivity_constrained as constrained
from tests.test_film_ideal_sensitivity import problem as ideal_problem
from tests.test_film_ideal_sensitivity_v2 import request_v2
from tests.test_film_sensitivity import problem as sensitivity_problem
from tests.test_film_sensitivity_constrained import feasible_problem

pytestmark = pytest.mark.algorithm
KINDS = ("sensitivity", "constrained", "ideal", "ideal_v2")


def case(kind, *, budget=7):
    execute: Any
    request: Any
    frozen: Any
    if kind in ("sensitivity", "constrained"):
        service, request = (sensitivity_problem(operation="optimize")
                            if kind == "sensitivity" else feasible_problem())
        request = replace(request, max_evaluations=budget)
        execute = sensitivity.execute_film_sensitivity_request
        frozen = sensitivity.freeze_film_sensitivity_request(service, request)
    else:
        service, request = (ideal_problem(max_evaluations=budget) if kind == "ideal"
                            else request_v2(max_evaluations=budget))
        execute = (ideal.execute_film_ideal_sensitivity_request if kind == "ideal"
                   else ideal_v2.execute_film_ideal_sensitivity_request)
        frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    return frozen, execute


def observer(request, *, enabled=True, publish=None, control=None):
    packets: list[dict[str, Any]] = []
    tick = iter(range(10000))
    result = display.CandidateDisplayObserver(
        context_id="sensitivity-display-test", objectives=request.objectives,
        control_provider=control or (lambda: {"enabled": enabled}),
        publish_callback=publish or packets.append, clock=lambda: next(tick),
        strict_lower_bound_objective_indices=getattr(
            request, "strict_lower_bound_objective_indices", ()),
        strict_upper_bound_objective_indices=request.strict_upper_bound_objective_indices,
    )
    return result, packets


@pytest.mark.parametrize("kind", KINDS)
def test_runtime_observer_none_preserves_frozen_result(kind):
    request, execute = case(kind)
    expected = execute(None, request)
    assert execute(None, request, candidate_observer=None) == expected


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("enabled", [False, True])
def test_real_fixed_budget_preview_changes_no_physics_or_incumbent(kind, enabled, monkeypatch):
    request, execute = case(kind)
    module = sensitivity if kind in ("sensitivity", "constrained") else ideal
    original = module.simulate_with_thickness_jacobian
    calls = []
    scalar_calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "simulate_with_thickness_jacobian", counted)
    if kind in ("ideal", "ideal_v2"):
        original_scalar = ideal._simulate_thickness_jacobian_arrays

        def counted_scalar(*args, **kwargs):
            scalar_calls.append(1)
            return original_scalar(*args, **kwargs)

        monkeypatch.setattr(ideal, "_simulate_thickness_jacobian_arrays", counted_scalar)
    expected = execute(None, request)
    expected_calls = len(calls)
    expected_scalar_calls = len(scalar_calls)
    calls.clear()
    scalar_calls.clear()
    observed, packets = observer(request, enabled=enabled)
    actual = execute(None, request, candidate_observer=observed)
    assert actual == expected
    assert len(calls) == expected_calls
    assert len(scalar_calls) == expected_scalar_calls
    assert len(calls) + len(scalar_calls) == actual.forward_evaluations
    assert not observed.last_error
    if enabled:
        assert packets and packets[-1]["terminal"]
        assert packets[-1]["state"] == actual.state
        assert packets[-1]["best"]["sensitivity_objective"] == (
            actual.best_analysis.robustness_value if kind in ("sensitivity", "constrained")
            else actual.best_analysis.objective_value)
        assert packets[-1]["best"]["weighted_rms_error"] == actual.best_analysis.nominal_weighted_rms_error
        assert all(len(p["top10"]) <= 10 for p in packets)
        assert all(not row["apply_eligible"] for p in packets for row in p["top10"])
    else:
        assert packets and all(not packet["enabled"] for packet in packets)
        assert all(packet[role] is None or packet[role]["spectrum"] is None
                   for packet in packets for role in ("current", "best"))


@pytest.mark.parametrize("kind", KINDS)
def test_disabled_preview_builds_no_spectrum_or_spectral_json(kind, monkeypatch):
    request, execute = case(kind)
    observed, packets = observer(request, enabled=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled preview must not sample spectra")

    def metadata_json(value, *args, **kwargs):
        assert all(value[role] is None or value[role]["spectrum"] is None
                   for role in ("current", "best"))
        return json.dumps(value, *args, **kwargs)

    monkeypatch.setattr(display.CandidateDisplayObserver, "_slot", forbidden)
    monkeypatch.setattr(display, "json", SimpleNamespace(dumps=metadata_json, loads=json.loads))
    result = execute(None, request, candidate_observer=observed)
    assert result.best_analysis is not None
    assert packets and not observed.last_error
    assert observed.spectral_snapshot_builds == observed.spectral_points_copied == observed.spectral_serializations == 0
    assert observed._top  # Independent ranking still sees normal candidates.


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("failure", ["publish", "control", "unknown_observer"])
def test_display_failures_and_detached_packet_mutation_cannot_change_search(kind, failure):
    request, execute = case(kind)
    expected = execute(None, request)

    def malicious_publish(packet):
        packet["top10"].clear()
        if packet["current"] is not None and packet["current"]["spectrum"] is not None:
            packet["current"]["spectrum"]["actual"][0] = float("nan")
        raise RuntimeError("display subscriber failed")

    def broken_control():
        raise RuntimeError("display control failed")

    def unknown_record(*_args, **_kwargs):
        raise AssertionError("an unknown observer must not receive compute-owned inputs")

    observed, _packets = observer(request, publish=malicious_publish if failure == "publish" else None,
                                  control=broken_control if failure == "control" else None)
    if failure == "unknown_observer":
        observed = SimpleNamespace(record=unknown_record, finish=unknown_record)
    assert execute(None, request, candidate_observer=observed) == expected


@pytest.mark.parametrize("kind", KINDS)
def test_independent_analysis_does_not_fabricate_candidate_search(kind):
    result: Any
    request, _execute = case(kind)
    observed, packets = observer(request)
    if kind in ("sensitivity", "constrained"):
        result = sensitivity.analyze_film_sensitivity(
            None, replace(request, operation="analyze"), candidate_observer=observed)
    else:
        result = ideal.analyze_film_ideal_sensitivity(None, request, candidate_observer=observed)
    assert result.state == "completed"
    assert not packets and not observed._top


@pytest.mark.parametrize("kind", ["ideal", "ideal_v2"])
def test_fd_probes_and_cancelled_half_gradient_never_enter_candidate_display(kind, monkeypatch):
    request, execute = case(kind, budget=20)
    expected_event = Event()
    original = ideal._simulate_thickness_jacobian_arrays

    def cancelled_forward(*args, **kwargs):
        result = original(*args, **kwargs)
        expected_event.set()  # First scalar block, after one complete baseline block.
        return result

    monkeypatch.setattr(ideal, "_simulate_thickness_jacobian_arrays", cancelled_forward)
    observed, packets = observer(request)
    result = execute(None, request, cancel_event=expected_event, candidate_observer=observed)
    assert result.state == "cancelled"
    assert result.forward_evaluations == 2 and result.fd_gradient_evaluations == 0
    assert result.best_analysis == result.baseline_analysis
    hashes = {row["physical_hash"] for packet in packets for row in packet["top10"]}
    assert len(hashes) == 1
    assert all(packet["current"]["total_thickness_nm"] == 80.0 for packet in packets)


def test_v2_nominal_failed_guard_pass_candidate_is_retained_without_apply_authority():
    service, request = request_v2(upper=0.001, max_evaluations=7)
    design = deepcopy(request.design_snapshot)
    design["front_layers"][0]["thickness"]["value"] = 180.0
    request = replace(request, design_snapshot=design,
        variables=(replace(request.variables[0], lower_nm=130.0, upper_nm=230.0),))
    request = ideal_v2.freeze_film_ideal_sensitivity_request(service, request)
    observed, packets = observer(request)
    result = ideal_v2.execute_film_ideal_sensitivity_request(None, request, candidate_observer=observed)
    assert result.best_guard is not None and result.best_analysis is not None
    assert result.best_guard.guard_pass and not result.best_analysis.nominal_targets_met
    assert packets[-1]["best"]["guard_pass"]
    assert packets[-1]["best"]["usable_candidate"]
    assert not packets[-1]["best"]["nominal_pass"]
    assert not packets[-1]["best"]["apply_eligible"]
    assert any(not row["nominal_pass"] and row["guard_pass"] for row in packets[-1]["top10"])


@pytest.mark.parametrize("kind", ["ideal", "ideal_v2"])
@pytest.mark.parametrize("budget", [3, 4])
def test_normal_trial_after_scalar_probe_is_fully_evaluated_and_cache_hits_publish(
    kind, budget, monkeypatch,
):
    import scipy.optimize  # type: ignore[import-untyped]

    request, execute = case(kind, budget=budget)
    observed, packets = observer(request)

    def fake_minimize(fun, z, *, jac, callback, **_kwargs):
        fun(z)
        jac(z)  # Scalar FD probes fill neither candidate cache nor leaderboard.
        initial_count = len(observed._top)
        assert initial_count == 1
        current_packets = len(packets)
        fun(z)  # Normal same-point local cache hit must still reach the observer.
        assert len(packets) > current_packets
        # A former scalar probe must be fully evaluated before it is a candidate.
        physical = 80.0
        stencil = ideal._bounded_stencil if kind == "ideal" else ideal_v2._bounded_stencil
        x1, _x2 = stencil(physical, 60.0, 145.0)
        trial = np.asarray([(x1 - 60.0) / 85.0])
        fun(trial)
        assert len(observed._top) == 2
        callback(z)
        return SimpleNamespace(x=z, success=True, status=0, message="test normal cache hit")

    monkeypatch.setattr(scipy.optimize, "minimize", fake_minimize)
    result = execute(None, request, candidate_observer=observed)
    assert result.probe_evaluations == 2 and result.fd_gradient_evaluations == 1
    # With only three blocks the normal trial cannot borrow the scalar F as
    # a complete candidate. Four blocks allow its full nominal/guard work.
    assert result.forward_evaluations == budget
    assert result.state == ("partial" if budget == 3 else "completed")
    assert len(packets[-1]["top10"]) == (1 if budget == 3 else 2)
    if budget == 4:
        assert result.cache_hits >= 1


def test_robust_coordinate_trials_not_mistaken_for_fd_and_finite_scenarios_not_ranked():
    request, execute = case("sensitivity", budget=13)
    observed, packets = observer(request)
    result = execute(None, request, candidate_observer=observed)
    assert result.evaluations == 13
    assert len(packets[-1]["top10"]) == 10
    assert all(row["evaluations"] <= result.evaluations for row in packets[-1]["top10"])
    # Every full candidate runs nominal plus both scenarios, without any display forward.
    assert result.forward_evaluations == 3 * result.evaluations
    assert result.jacobian_evaluations == result.evaluations


def test_constrained_same_point_local_cache_hit_still_observes_normal_trial(monkeypatch):
    request, execute = case("constrained", budget=10)
    observed, packets = observer(request)

    def fake_minimize(fun, z, **_kwargs):
        before = len(packets)
        fun(z)
        assert len(packets) > before
        before = len(packets)
        fun(z)
        assert len(packets) > before
        return SimpleNamespace(x=z, success=True, status=0, message="same point")

    monkeypatch.setattr(constrained, "minimize", fake_minimize)
    result = execute(None, request, candidate_observer=observed)
    assert result.evaluations == 1
    assert len(packets[-1]["top10"]) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_cancelled_partial_baseline_has_no_current_best_or_ranked_candidate(kind, monkeypatch):
    request, execute = case(kind)
    module = sensitivity if kind in ("sensitivity", "constrained") else ideal
    original = module.simulate_with_thickness_jacobian
    event = Event()

    def cancelled(*args, **kwargs):
        values = original(*args, **kwargs)
        event.set()
        return values

    monkeypatch.setattr(module, "simulate_with_thickness_jacobian", cancelled)
    observed, packets = observer(request)
    result = execute(None, request, cancel_event=event, candidate_observer=observed)
    assert result.state == "cancelled" and result.best_analysis is None
    assert packets[-1]["terminal"] and not packets[-1]["top10"]
    assert packets[-1]["current"] is None and packets[-1]["best"] is None


def test_display_rank_does_not_replace_real_robustness_incumbent():
    request, execute = case("sensitivity", budget=13)
    initial = sensitivity.analyze_film_sensitivity(None, replace(request, operation="analyze"))
    assert initial.best_analysis is not None
    target = initial.best_analysis.nominal_conditions[0].actual
    request = replace(request, objectives=(replace(request.objectives[0],
        mode="value", upper_bound=None, target_value=target),))
    observed, packets = observer(request)
    result = execute(None, request, candidate_observer=observed)
    assert result.improved and not result.best_analysis.nominal_targets_met
    best, top = packets[-1]["best"], packets[-1]["top10"][0]
    assert top["nominal_pass"] and not best["nominal_pass"]
    assert best["physical_hash"] != top["physical_hash"]
    assert best["sensitivity_objective"] == result.best_analysis.robustness_value


@pytest.mark.parametrize("unit", ["nm", "um", "qw"])
def test_display_counts_complete_zero_layers_and_converts_all_physical_thicknesses(unit):
    service, request = sensitivity_problem(operation="optimize")
    request = replace(request, max_evaluations=1)
    execute = sensitivity.execute_film_sensitivity_request
    design = deepcopy(request.design_snapshot)
    value = {"nm": 80.0, "um": 0.08, "qw": 80.0 * 4 * np.sqrt(1.5) / 550.0}[unit]
    design["front_layers"][0]["thickness"] = {"value": value, "unit": unit}
    design["front_layers"].append(dict(id="complete-zero", material_id="high", optical_set_id="nk",
                                       thickness={"value": 0.0, "unit": "nm"}))
    request = sensitivity.freeze_film_sensitivity_request(
        service, replace(request, design_snapshot=design))
    observed, packets = observer(request)
    result = execute(None, request, candidate_observer=observed)
    assert result.evaluations == 1
    assert packets[-1]["best"]["layer_count"] == 2
    assert packets[-1]["best"]["total_thickness_nm"] == pytest.approx(80.0)
    assert result.best_design is not None
    assert len(result.best_design["front_layers"]) == 2


def test_selected_metric_and_angle_use_only_sampled_complete_nominal_grid_without_forward():
    import json

    service, request = sensitivity_problem(operation="optimize")
    objective = replace(request.objectives[0], wavelength_min_nm=500.0,
        wavelength_max_nm=600.0, wavelength_samples=2049, angles_deg=(0.0, 42.0),
        metric="T", polarization="p", upper_bound=1.0)
    request = sensitivity.freeze_film_sensitivity_request(
        service, replace(request, objectives=(objective,), max_evaluations=1))
    control = {"enabled": True, "objective_index": 0, "angle_deg": 42.0, "metric": "T"}
    packets = []

    def publish(packet):
        packets.append(packet)
        control["metric"] = "R"  # This component was deliberately not retained.

    observed, _unused = observer(request, publish=publish, control=lambda: control)
    result = sensitivity.execute_film_sensitivity_request(None, request, candidate_observer=observed)
    assert result.best_analysis is not None
    spectrum = packets[0]["current"]["spectrum"]
    assert spectrum["original_point_count"] == 2049
    assert len(spectrum["actual"]) == len(spectrum["wavelength_nm"]) == 1024
    rows = [row for row in result.best_analysis.nominal_conditions if row.angle_deg == 42.0]
    assert spectrum["wavelength_nm"] == [rows[i].wavelength_nm for i in spectrum["indices"]]
    assert spectrum["actual"] == [rows[i].actual for i in spectrum["indices"]]
    assert all(len(json.dumps(packet).encode()) <= 128 * 1024 for packet in packets)
    assert packets[-1]["current"]["availability"] == "waiting"
    assert packets[-1]["current"]["spectrum"] is None
    assert result.forward_evaluations == 6 and result.jacobian_evaluations == 2
