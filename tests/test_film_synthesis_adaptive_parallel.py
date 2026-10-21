"""Run-local scheduling observations do not change ordered physical decisions."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_parallel as parallel
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def observe_serial(executor, *, jacobian=False, zero_probe=False):
    task = parallel.EvaluationTask({"front_layers": [{}, {}]}, jacobian=jacobian,
                                  zero_ids=("probe",) if zero_probe else ())
    value = {"responses": [{"wavelength_nm": list(range(50))}]}
    assert executor.serial(lambda: value, task) is value


def test_cold_reasons_and_existing_three_argument_interface(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 8)
    executor = parallel.OrderedEvaluationExecutor(2)
    assert not executor.eligible(1, parallel.MIN_PARALLEL_WORK, True)
    assert executor._last_selection["selection_reason"] == "insufficient_candidates"
    assert not executor.eligible(2, 1, True)
    assert executor._last_selection["selection_reason"] == "cold_work_below_threshold"
    assert executor.eligible(2, parallel.MIN_PARALLEL_WORK, True)
    assert executor._last_selection["selection_reason"] == "cold_work_threshold"
    assert executor._last_selection["ready_pool_workers"] == 0
    assert executor.children == []  # Eligibility alone never starts workers.


def test_completed_work_discovers_only_two_workers_and_budget_guard_wins(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 16)
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 200)
    executor = parallel.OrderedEvaluationExecutor(0)
    assert executor.workers == 8  # Existing auto ceiling is retained.
    observe_serial(executor)
    assert not executor.eligible(6, 50, True, phase="structure")
    observe_serial(executor)
    assert executor.eligible(6, 50, True, phase="structure")
    assert executor._last_selection["selection_reason"] == "cold_accumulated_work_discovery"
    assert executor._last_selection["selected_worker_cap"] == 2
    assert executor._last_selection["serial_cost_samples"] == 2
    assert not executor.eligible(6, 50, False, phase="structure")
    assert executor._last_selection["selection_reason"] == "finite_budget_serial"
    assert executor.children == []


def test_explicit_single_worker_and_machine_cap_remain_authoritative(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 2)
    serial = parallel.OrderedEvaluationExecutor(1)
    observe_serial(serial)
    assert not serial.eligible(10, 10**9, True)
    assert serial._last_selection["selection_reason"] == "requested_serial"
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 1)
    capped = parallel.OrderedEvaluationExecutor(4)
    assert not capped.eligible(10, 10**9, True)
    assert capped._last_selection["selection_reason"] == "available_cpu_serial"


def test_warm_measured_cost_and_unobserved_cost_are_distinct(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 8)
    executor = parallel.OrderedEvaluationExecutor(8)
    monkeypatch.setattr(executor, "children", [SimpleNamespace(ready=True), SimpleNamespace(ready=True)])
    assert not executor.eligible(4, 50, True, phase="structure")
    assert executor._last_selection["selection_reason"] == "warm_insufficient_cost_samples"
    observe_serial(executor)
    executor.finished.update((10, 11))
    executor.ipc_seconds = 10.
    assert not executor.eligible(4, 50, True, phase="structure")
    assert executor._last_selection["selection_reason"] == "warm_ipc_cost_serial"
    executor.ipc_seconds = 0.
    assert executor.eligible(4, 50, True, phase="structure")
    assert executor._last_selection["selection_reason"] == "warm_measured_reuse"
    assert executor._last_selection["selected_worker_cap"] == 2
    assert not executor.eligible(4, 50, True, phase="needle")
    assert executor._last_selection["serial_cost_samples"] == 0
    assert executor._last_selection["selection_reason"] == "warm_insufficient_cost_samples"


def test_failed_callbacks_do_not_create_successful_timing_observations(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 2)
    executor = parallel.OrderedEvaluationExecutor(2)
    task = parallel.EvaluationTask({"front_layers": []})
    def failed():
        raise ValueError("physical failure")
    with pytest.raises(ValueError, match="physical failure"):
        executor.serial(failed, task)
    observe_serial(executor, jacobian=True, zero_probe=True)
    observe_serial(executor, jacobian=True)
    observe_serial(executor)
    assert executor.serial_unknown == 1
    assert executor.serial_observed_work == 300
    assert set(executor.serial_costs) == {(True, True), (True, False), (False, False)}
    assert all(sample.samples == 1 for sample in executor.serial_costs.values())


def test_real_accumulated_discovery_preserves_two_worker_order_scores_and_work():
    if parallel.available_cpu_workers() < 2:
        pytest.skip("needs two available workers")
    service, request = setup_problem(low=520., high=580., samples=1001)
    request = synthesis.freeze_film_synthesis_request(service, replace(request,
        cpu_workers=2, max_evaluations=8))
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [dict(id=f"adaptive-layer-{index}", material_id=("high" if index % 2 else "low"),
        optical_set_id="nk", thickness=dict(value=55., unit="nm"), note="") for index in range(64)]
    task = parallel.EvaluationTask(design, jacobian=False)
    assert request.max_evaluations is not None
    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(synthesis._proxy(request))
    def physical():
        return evaluate_frozen_structure_candidate(synthesis._proxy(request), design)
    try:
        reference = executor.serial(physical, task)
        executor.serial(physical, task)
        points = sum(len(response["wavelength_nm"]) for response in reference["responses"])
        work = 2 * points * len(design["front_layers"])
        assert work < parallel.MIN_PARALLEL_WORK
        assert executor.serial_observed_work + work >= parallel.MIN_PARALLEL_WORK
        tasks = [parallel.EvaluationTask(design, jacobian=False, context=index) for index in range(2)]
        order = []
        with executor.batch(tasks, count=2, work=work,
                budget_allows=2 <= request.max_evaluations - executor.serial_invocations - 1,
                phase="structure") as batch:
            for candidate, outcome in batch:
                assert outcome is not None
                value = executor.take(outcome)
                assert value["score"] == reference["score"]
                order.append(candidate.context)
        assert order == [0, 1]
    finally:
        executor.close()
    diagnostics = executor.diagnostics()
    decision = diagnostics["batch_decisions"][-1]
    assert decision["selection_reason"] == "cold_accumulated_work_discovery"
    assert decision["parallel_selected"] is True and decision["selected_worker_cap"] == 2
    assert diagnostics["effective_workers"] == 2 and len(diagnostics["worker_pids"]) == 2
    assert diagnostics["submitted_tasks"] == diagnostics["completed_tasks"] == diagnostics["consumed_tasks"] == 2
    assert diagnostics["serial_evaluator_invocations"] == 2
    assert diagnostics["known_training_evaluator_invocations"] == 4
    assert diagnostics["known_training_wavelength_points"] == 4 * points
    assert diagnostics["unknown_tasks"] == diagnostics["serial_fallback_tasks"] == 0
    assert diagnostics["active_worker_pids"] == [] and diagnostics["cleanup_complete"]
    assert all(not worker["qt_imported"] for worker in diagnostics["workers"])
    assert diagnostics["worker_cpu_seconds"] == diagnostics["known_worker_cpu_seconds"] > 0
    cpu_records = diagnostics["worker_cpu_by_pid"]
    assert {row["pid"] for row in cpu_records} == set(diagnostics["worker_pids"])
    assert all(row["completed_tasks"] == row["cpu_samples"] == 1
               and row["known_cpu_seconds"] > 0 for row in cpu_records)
    print("adaptive real workers", {"worker_pids": diagnostics["worker_pids"],
        "observed_serial_work": decision["observed_serial_work"],
        "submitted": diagnostics["submitted_tasks"], "consumed": diagnostics["consumed_tasks"],
        "startup_seconds": diagnostics["startup_seconds"],
        "worker_compute_seconds": diagnostics["worker_compute_seconds"],
        "worker_cpu_seconds": diagnostics["worker_cpu_seconds"],
        "worker_cpu_by_pid": diagnostics["worker_cpu_by_pid"],
        "actual_work_complete": diagnostics["actual_work_complete"]})


def test_startup_fallback_is_distinct_from_eligibility(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 2)
    executor = parallel.OrderedEvaluationExecutor(2)
    def unavailable(_count):
        executor.disabled = True
        executor.closed = True
    monkeypatch.setattr(executor, "_ensure_pool", unavailable)
    task = parallel.EvaluationTask({"front_layers": []})
    with executor.batch([task, task], count=2, work=parallel.MIN_PARALLEL_WORK,
                        budget_allows=True, phase="growth") as batch:
        assert all(outcome is None for _task, outcome in batch)
    decision = executor.batch_decisions[-1]
    assert decision["pool_eligible"] is True
    assert decision["eligibility_reason"] == "cold_work_threshold"
    assert decision["selection_reason"] == "startup_unavailable_serial"
    assert decision["parallel_selected"] is False
    assert decision["startup_seconds_in_batch"] == 0.
    assert executor.submitted == set()


def test_selected_cap_limits_existing_larger_pool_dispatch(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 2)
    executor = parallel.OrderedEvaluationExecutor(8)
    children = [SimpleNamespace(ready=True, index=index) for index in range(4)]
    monkeypatch.setattr(executor, "children", children)
    observe_serial(executor)
    executor.finished.update((10, 11))
    executor.ipc_seconds = 0.
    dispatched: list[int] = []
    def submit(child, task):
        token = len(dispatched) + 100
        dispatched.append(child.index)
        outcome = parallel.EvaluationOutcome(token, value=task.context)
        executor.outcomes[token] = outcome
        executor.finished.add(token)
        executor.submitted.add(token)
        return outcome
    monkeypatch.setattr(executor, "_submit", submit)
    tasks = [parallel.EvaluationTask({"front_layers": []}, context=index) for index in range(4)]
    consumed = []
    with executor.batch(tasks, count=4, work=50, budget_allows=True, phase="structure") as batch:
        for _task, outcome in batch:
            assert outcome is not None
            consumed.append(executor.take(outcome))
    assert consumed == [0, 1, 2, 3]
    assert set(dispatched) == {0, 1}
    assert executor.batch_decisions[-1]["selected_worker_cap"] == 2
    assert len(executor.submitted) == sum(outcome.consumed for outcome in executor.outcomes.values()) == 4




@pytest.mark.parametrize("missing_cpu", [False, True])
def test_cpu_complete_total_is_unknown_until_all_replies_are_recorded(monkeypatch, missing_cpu):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 2)
    executor = parallel.OrderedEvaluationExecutor(2)
    monkeypatch.setattr(executor, "children", [SimpleNamespace(actual_pid=12345, pending=0,
        actual_dead=False, process=SimpleNamespace(pid=54321))])
    executor.submitted.update((0, 1))
    executor.outcomes[0] = parallel.EvaluationOutcome(0)
    reply: dict[str, object] = {"kind": "result", "token": 0, "status": "ok", "value": {}, "work": {}}
    if not missing_cpu:
        reply["cpu_seconds"] = .25
    executor.events.put((0, reply, 0., 0))
    executor._receive(timeout=0.)
    diagnostics = executor.diagnostics()
    assert diagnostics["worker_cpu_seconds"] is None
    assert diagnostics["known_worker_cpu_seconds"] == (0. if missing_cpu else .25)
    assert diagnostics["unrecorded_worker_cpu_tasks"] == int(missing_cpu)
    assert diagnostics["worker_cpu_by_pid"][0]["completed_tasks"] == 1
    assert diagnostics["worker_cpu_by_pid"][0]["cpu_samples"] == int(not missing_cpu)
