"""Bounded observations never become search, acceptance or apply authority."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
from threading import Event
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_candidate_display as display
from coat_sim.core.film_batch_de import BatchEvaluation, run_batch_de
from coat_sim.core.film_design import new_design
from coat_sim.core.film_optimization import FilmConditionError, FilmOptimizationObjective, optimize_film_design
from tests.film_performance_fixtures import fixture
from tests.test_film_optimization_forward_cache import _request

pytestmark = pytest.mark.algorithm


def design(thickness=100., *, unit="nm", identifier="row"):
    result = new_design()
    result["front_layers"] = [{"id": identifier, "material_id": "film", "optical_set_id": "nk",
        "thickness": {"value": thickness, "unit": unit}, "note": "editor label"}]
    return result


def objective(samples=3):
    return FilmOptimizationObjective(500., 600., samples, mode="range", upper_bound=.2)


def rows(target, actual=.1):
    return tuple(FilmConditionError(0, float(wave), angle, target.polarization, target.metric,
        actual, target.target_value, target.lower_bound, target.upper_bound,
        max(0., actual - (target.upper_bound or 1.)), max(0., actual - (target.upper_bound or 1.)))
        for angle in target.angles_deg
        for wave in np.linspace(target.wavelength_min_nm, target.wavelength_max_nm, target.wavelength_samples))


def harness(target=None, *, enabled=True, strict_lower=(), strict_upper=()):
    target = target or objective()
    clock = [0.]
    control = {"enabled": enabled, "objective_index": 0, "angle_deg": 0., "metric": target.metric}
    packets: list[dict[str, Any]] = []
    observer = display.CandidateDisplayObserver(context_id="frozen-test-context", objectives=(target,),
        control_provider=lambda: control, publish_callback=packets.append, clock=lambda: clock[0],
        strict_lower_bound_objective_indices=strict_lower,
        strict_upper_bound_objective_indices=strict_upper)
    return observer, packets, clock, control


def record(observer, thickness=100., *, target=None, actual=.1, best=False, **kwargs):
    target = target or observer.objectives[0]
    observer.record(design(thickness), weighted_rms_error=max(0., actual - (target.upper_bound or 1.)),
        max_normalized_error=max(0., actual - (target.upper_bound or 1.)),
        condition_rows=rows(target, actual), physical_thickness_nm=[thickness], is_best=best, **kwargs)


def test_display_score_has_nominal_tiers_and_strict_complexity_monotonicity():
    def score(error=0., count=10, thickness=1000., passed=True):
        return display.score_candidate(weighted_rms_error=error, layer_count=count,
            total_thickness_nm=thickness, nominal_pass=passed)
    assert score(error=.1)["quality"] > score(error=.2)["quality"]
    assert score(count=9)["quality"] > score(count=10)["quality"]
    assert score(thickness=900.)["quality"] > score(thickness=1000.)["quality"]
    assert score(error=1e9)["score"] > score(passed=False)["score"]
    assert score(count=0, thickness=0.)["score"] == 100.


@pytest.mark.parametrize("value", [0., -1., float("nan"), float("inf"), True, "90", 10 ** 1000])
def test_configuration_rejects_nonpositive_or_nonfinite_parameters(value):
    with pytest.raises(ValueError):
        display.score_config_from_payload({"weight_error": value})


def test_configuration_freezes_defaults_and_handles_large_weights():
    payload = display.score_config_to_payload()
    assert payload == {"weight_error": 90., "weight_layers": 5., "weight_thickness": 5.,
                       "layer_reference": 10., "thickness_reference_nm": 1000.}
    with pytest.raises(ValueError):
        display.score_config_from_payload({"unknown": 1})
    result = display.score_candidate(weighted_rms_error=1., layer_count=2,
        total_thickness_nm=30., nominal_pass=False,
        score_config=display.CandidateScoreConfig(weight_error=1e308, weight_layers=1e308, weight_thickness=1e308))
    assert math.isfinite(result["score"])


def test_streaming_identity_normalizes_units_and_ignores_editor_ids():
    first, second = design(100.), design(.1, unit="um", identifier="new-id")
    second["front_layers"][0]["note"] = "changed annotation"
    assert display.candidate_physical_hash(first, [100.]) == display.candidate_physical_hash(second, [100.])
    assert display.candidate_physical_hash(first, [100.]) != display.candidate_physical_hash(first, [101.])
    second["front_layers"][0]["optical_set_id"] = "different-source"
    assert display.candidate_physical_hash(first, [100.]) != display.candidate_physical_hash(second, [100.])


def test_all_physical_rows_count_including_zero_and_back_not_substrate():
    observer, packets, _clock, _control = harness()
    stack = design(0.)
    stack["back_layers"] = [dict(stack["front_layers"][0], id="back", thickness={"value": .2, "unit": "um"})]
    observer.record(stack, weighted_rms_error=0., max_normalized_error=0., condition_rows=rows(objective()),
        physical_thickness_nm={"back": 200., "row": 0.}, is_best=True)
    summary = packets[-1]["current"]
    assert summary["layer_count"] == 2 and summary["total_thickness_nm"] == 200.


def test_off_and_throttled_updates_do_not_sample_copy_or_serialize(monkeypatch):
    observer, packets, clock, control = harness(enabled=False)
    monkeypatch.setattr(observer, "_slot", lambda *_args: pytest.fail("off sampled spectra"))
    original = display.json.dumps
    def metadata_only(value, **kwargs):
        if isinstance(value, dict) and value.get("kind") == display.DISPLAY_KIND:
            assert all(value[role] is None or value[role]["spectrum"] is None for role in ("current", "best"))
        return original(value, **kwargs)
    monkeypatch.setattr(display.json, "dumps", metadata_only)
    record(observer, best=True)
    record(observer, 90., best=True)
    observer.finish()
    assert packets and all(not packet["enabled"] for packet in packets) and observer.last_error == ""
    assert observer.spectral_snapshot_builds == observer.spectral_points_copied == observer.spectral_serializations == 0
    assert observer.metadata_only_packets == len(packets)
    assert len(observer._top) == 2


def test_true_best_changes_between_frames_are_lazily_sampled_without_stale_preview():
    observer, packets, clock, _control = harness()
    record(observer, 100., best=True)
    clock[0] = .25
    record(observer, 90., actual=.15, best=True)
    assert len(packets) == 1
    clock[0] = 1.
    record(observer, 80.)
    assert len(packets) == 2
    assert packets[-1]["best"]["total_thickness_nm"] == 90.
    assert packets[-1]["best"]["spectrum"]["actual"] == [.15] * 3
    assert packets[-1]["current"]["total_thickness_nm"] == 80.
    assert packets[-1]["top10"][0]["physical_hash"] != packets[-1]["best"]["physical_hash"]


def test_current_view_switch_reports_missing_metric_without_new_computation():
    observer, packets, clock, control = harness()
    record(observer, best=True)
    control["metric"] = "T"
    clock[0] = 1.
    record(observer, 90.)
    assert packets[-1]["best"]["availability"] == "waiting"
    assert packets[-1]["best"]["spectrum"] is None


def test_strict_nominal_e_zero_is_not_solver_feasible_and_value_is_exact():
    target = replace(objective(), lower_bound=.1, upper_bound=None)
    observer, packets, _clock, _control = harness(target, strict_lower=(0,))
    record(observer, target=target, actual=.1)
    assert not packets[-1]["current"]["nominal_pass"]
    target = replace(objective(), upper_bound=.1)
    observer, packets, _clock, _control = harness(target, strict_upper=(0,))
    record(observer, target=target, actual=.1)
    assert not packets[-1]["current"]["nominal_pass"]
    value = replace(objective(), mode="value", upper_bound=None, target_value=.1)
    observer, packets, _clock, _control = harness(value)
    record(observer, target=value, actual=np.nextafter(.1, 1.))
    assert not packets[-1]["current"]["nominal_pass"]


def test_same_length_wrong_grid_and_other_scope_never_enter_top_ten():
    observer, packets, _clock, _control = harness()
    wrong = replace(objective(), wavelength_min_nm=501.)
    record(observer, target=wrong)
    record(observer, scope="audit")
    assert not packets and not observer._top


def test_metadata_only_requires_actual_complete_count_and_explicit_nominal():
    observer, packets, _clock, _control = harness()
    arguments = dict(weighted_rms_error=0., max_normalized_error=0., condition_rows=None,
                     physical_thickness_nm=[100.], nominal_pass=False)
    observer.record(design(), **arguments)
    assert not observer._top
    observer.record(design(), **arguments, condition_count=3)
    assert packets[-1]["current"]["availability"] == "waiting"
    assert packets[-1]["current"]["nominal_pass"] is False


def test_top_ten_is_small_complete_history_not_search_incumbents():
    observer, packets, _clock, _control = harness()
    for thickness in range(20, 0, -1):
        record(observer, float(thickness), best=thickness == 20)
    observer.finish(state="cancelled")
    last = packets[-1]
    assert len(last["top10"]) == 10
    assert [row["total_thickness_nm"] for row in last["top10"]] == list(range(1, 11))
    assert last["best"]["total_thickness_nm"] == 20.
    assert all("spectrum" not in row and "design" not in row for row in last["top10"])
    assert last["terminal"] and last["state"] == "cancelled"
    observer.finish()
    assert len(packets) == 2


def test_preview_packet_is_bounded_detached_and_closed_schema():
    target = objective(4097)
    observer, packets, _clock, _control = harness(target)
    record(observer, target=target, best=True)
    packet = packets[-1]
    assert len(packet["current"]["spectrum"]["actual"]) == 1024
    assert len(json.dumps(packet).encode()) <= display.MAX_DISPLAY_BYTES
    packet["current"]["spectrum"]["actual"][0] = .9
    observer.finish()
    assert packets[-1]["best"]["spectrum"]["actual"][0] == .1
    bad = deepcopy(packets[-1])
    bad["surprise"] = 1
    assert display.validate_candidate_display_packet(bad) is None
    bad = deepcopy(packets[-1])
    bad["best"]["spectrum"]["actual"][0] = float("nan")
    assert display.validate_candidate_display_packet(bad) is None
    bad = deepcopy(packets[-1])
    bad["top10"].append(deepcopy(bad["top10"][0]))
    assert display.validate_candidate_display_packet(bad) is None
    bad = deepcopy(packets[-1])
    bad["best"]["candidate_eligible"] = not bad["best"]["candidate_eligible"]
    assert display.validate_candidate_display_packet(bad) is None


def test_publish_failure_consumes_time_slot_without_stopping_candidate_tracking():
    observer, _packets, clock, _control = harness()
    calls = []
    def fail(packet):
        calls.append(packet)
        raise OSError("display unavailable")
    observer.publish_callback = fail
    record(observer, best=True)
    clock[0] = .2
    record(observer, 90., best=True)
    assert len(calls) == 1 and len(observer._top) == 2
    assert observer.last_error.startswith("OSError")


def test_ephemeral_existing_spectrum_provider_is_gated_and_never_retained():
    observer, packets, clock, control = harness(enabled=False)
    observer.record(design(), weighted_rms_error=0., max_normalized_error=0., condition_rows=None,
        nominal_pass=True, condition_count=3, physical_thickness_nm=[100.], publish=False)
    calls = []
    def source(_selection, _limit):
        calls.append(1)
        return {"wavelength_nm": [500., 550., 600.], "actual": [.1] * 3,
                "indices": [0, 1, 2], "original_point_count": 3}
    observer.refresh(spectrum_provider=source)
    assert not calls
    control["enabled"] = True
    clock[0] = 1.
    observer.refresh(spectrum_provider=source)
    assert len(calls) == 1 and packets[-1]["current"]["availability"] == "available"
    clock[0] = 1.5
    observer.refresh(spectrum_provider=source)
    assert len(calls) == 1
    assert not any(value is source for value in vars(observer).values())
    clock[0] = 2.
    observer.refresh()
    assert packets[-1]["current"]["availability"] == "waiting"


def test_batch_live_current_uses_last_atomic_input_not_chunk_best(monkeypatch):
    from coat_sim.core.film_optimization_batch_forward import OptimizationBatchForward
    service, stack, frozen = fixture(layers=2)
    request = _request(stack, frozen, budget=7)
    request = replace(request, solver="differential_evolution", execution_mode="deferred_batch", batch_size=3)
    observer, packets, clock, _control = harness(request.objectives[0])
    expected = []
    original = OptimizationBatchForward.responses
    def response(forward, points, **kwargs):
        result = original(forward, points, **kwargs)
        expected.append((float(points[-1, 0]), result[0]["R"][-1].tolist()))
        clock[0] += 1.1
        return result
    monkeypatch.setattr(OptimizationBatchForward, "responses", response)
    optimize_film_design(service, request, candidate_observer=observer)
    frames = [packet for packet in packets if not packet["terminal"]
              and packet["current"]["source"] == "film_optimization_batch"]
    assert len(frames) == len(expected)
    for packet, (last_thickness, actual) in zip(frames, expected, strict=True):
        assert packet["current"]["spectrum"]["actual"] == actual
        # The fixed second layer contributes 67 nm to the full-stack total.
        assert packet["current"]["total_thickness_nm"] == last_thickness + 67.


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_normal_optimizer_observer_does_not_change_forward_budget_or_incumbent(monkeypatch, batch, enabled):
    from coat_sim.core import film_design_service
    service, stack, frozen = fixture(layers=2)
    request = _request(stack, frozen, budget=9)
    if batch:
        request = replace(request, solver="differential_evolution", execution_mode="deferred_batch", batch_size=3)
    calls = []
    original = film_design_service.FilmDesignService.simulate
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(film_design_service.FilmDesignService, "simulate", counted)
    baseline = optimize_film_design(service, request)
    count = len(calls)
    assert count > 0
    calls.clear()
    observer, packets, _clock, _control = harness(request.objectives[0], enabled=enabled)
    watched = optimize_film_design(service, request, candidate_observer=observer)
    assert len(calls) == count
    assert (watched.evaluations, watched.validation_evaluations, watched.failed_evaluations) == (
        baseline.evaluations, baseline.validation_evaluations, baseline.failed_evaluations)
    assert watched.best_design == baseline.best_design
    assert watched.per_condition_errors == baseline.per_condition_errors
    assert len(observer._top) > 1
    if not enabled:
        assert packets and all(not packet["enabled"] for packet in packets)
        assert all(packet["current"]["spectrum"] is None and packet["best"]["spectrum"] is None
                   for packet in packets if packet["best"] is not None)
        return
    assert packets[-1]["best"]["weighted_rms_error"] == watched.weighted_rms_error
    if batch:
        assert packets[-1]["current"]["availability"] == "waiting"


@pytest.mark.parametrize("solver", ["powell", "trf", "lbfgsb"])
def test_analytic_and_scalar_solvers_keep_identical_physical_calls(monkeypatch, solver):
    from coat_sim.core import film_optimization_derivatives as derivatives
    service, stack, frozen = fixture(layers=2)
    request = _request(stack, frozen, solver=solver, budget=7)
    calls = []
    original = derivatives.simulate_with_thickness_jacobian
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(derivatives, "simulate_with_thickness_jacobian", counted)
    baseline = optimize_film_design(service, request)
    count = len(calls)
    calls.clear()
    observer, packets, _clock, _control = harness(request.objectives[0])
    watched = optimize_film_design(service, request, candidate_observer=observer)
    assert len(calls) == count and watched == baseline
    assert packets[-1]["best"]["weighted_rms_error"] == watched.weighted_rms_error


@pytest.mark.parametrize("actual", [1., np.nextafter(1., 0.)])
def test_batch_metadata_exact_nominal_reduce_matches_full_condition_checker(monkeypatch, actual):
    from coat_sim.core.film_optimization_batch_forward import OptimizationBatchForward
    from coat_sim.core.film_nominal_acceptance import nominal_condition_pass
    service, stack, frozen = fixture(layers=2)
    request = _request(stack, frozen, budget=7)
    target = replace(request.objectives[0], target_value=1.)
    request = replace(request, objectives=(target,), solver="differential_evolution",
                      execution_mode="deferred_batch", batch_size=3)
    def supplied(_forward, points, **_kwargs):
        return [{"wavelength_nm": np.linspace(target.wavelength_min_nm, target.wavelength_max_nm,
            target.wavelength_samples), "R": np.full((len(points), target.wavelength_samples), actual),
            "T": np.full((len(points), target.wavelength_samples), 1. - actual)}]
    monkeypatch.setattr(OptimizationBatchForward, "responses", supplied)
    observer, packets, _clock, _control = harness(target)
    optimize_film_design(service, request, candidate_observer=observer)
    current = packets[-1]["current"]
    assert current["source"] == "film_optimization_batch"
    assert current["nominal_pass"] is nominal_condition_pass(target, actual)
    assert current["condition_count"] == target.wavelength_samples


@pytest.mark.parametrize("failure", ["cancel", "invalid"])
def test_batch_atomic_rejection_never_calls_accepted_display_hook(failure):
    cancel = Event()
    observed = []
    attempts = [0]
    def evaluate(points):
        attempts[0] += 1
        if attempts[0] > 1:
            if failure == "cancel":
                cancel.set()
            else:
                return []
        return [BatchEvaluation((float(np.sum(point ** 2)), 0.), float(np.sum(point ** 2)), None)
                for point in points]
    result = run_batch_de(evaluate, [1.], [0.], [2.], max_iterations=1, max_evaluations=6,
        max_batch_size=3, cancel_event=cancel, accepted_callback=lambda *items: observed.append(items))
    assert not observed
    assert result.evaluations > 1


def test_atomic_display_hook_cannot_mutate_de_coordinates_or_score_payloads():
    def evaluate(points):
        return [BatchEvaluation((float(np.sum(point ** 2)), 0.), float(np.sum(point ** 2)),
            {"display_metrics": (1., 1., False, 3)}) for point in points]
    baseline = run_batch_de(evaluate, [1.], [0.], [2.],
        max_iterations=1, max_evaluations=8, max_batch_size=3, seed=3)
    attempted = []
    def observer(items, points, _evaluations, _generations):
        attempted.append(1)
        with pytest.raises(ValueError):
            points[0, 0] = 999.
        with pytest.raises(TypeError):
            items[0].value["display_metrics"] = (0., 0., True, 3)
    watched = run_batch_de(evaluate, [1.], [0.], [2.], accepted_callback=observer,
        max_iterations=1, max_evaluations=8, max_batch_size=3, seed=3)
    assert attempted and watched.best.key == baseline.best.key
    assert np.array_equal(watched.best_thickness, baseline.best_thickness)
    assert watched.evaluations == baseline.evaluations


@pytest.mark.parametrize("upper", [.05, .001])
def test_structure_and_hybrid_observers_preserve_existing_evaluation_counts(upper):
    from coat_sim.core.film_optimization_structure import optimize_film_design_with_structure
    from coat_sim.core.film_auto_synthesis import synthesize_film_design
    from tests.test_film_optimization_structure import _service, _request as structure_request
    from tests.test_film_auto_synthesis import setup_problem
    service, stack = _service()
    request = structure_request(stack, max_evaluations=12, max_iterations=1)
    structure_base = optimize_film_design_with_structure(service, request)
    observer, packets, _clock, _control = harness(request.base_request.objectives[0])
    structure_watched = optimize_film_design_with_structure(service, request, candidate_observer=observer)
    assert structure_watched.best_design == structure_base.best_design and structure_watched.counters == structure_base.counters
    assert packets[-1]["best"]["max_normalized_error"] == structure_watched.max_normalized_error
    service, request = setup_problem(upper=upper, materials=("low",))
    request = replace(request, search_strategy="hybrid", max_evaluations=20, max_cycles=1)
    base = synthesize_film_design(service, request)
    observer, packets, _clock, _control = harness(request.objectives[0])
    watched = synthesize_film_design(service, request, candidate_observer=observer)
    assert watched.candidate_design == base.candidate_design
    assert (watched.evaluations, watched.validation_evaluations, watched.failed_evaluations) == (
        base.evaluations, base.validation_evaluations, base.failed_evaluations)
    assert watched.candidate_design is not None
    assert packets[-1]["best"]["layer_count"] == len(watched.candidate_design["front_layers"])
    assert all(row["condition_count"] == request.objectives[0].wavelength_samples for row in packets[-1]["top10"])
