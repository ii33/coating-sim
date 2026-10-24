"""GUI inner-worker setting stays separate from search budgets and host binding."""
from copy import deepcopy

import pytest

from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload,
    film_synthesis_request_to_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload,
    canonical_parameters,
    deserialize_job_payload,
)
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_cpu_workers
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def _select_material(controller):
    selected = False
    for row in controller.structureMaterialOptions:
        if row["material_id"] == "acceptance-film" and row["optical_set_id"] == "film-set":
            assert controller.setStructureMaterialSelected(row["material_id"], row["optical_set_id"], True)
            selected = True
    assert selected


def test_cpu_workers_freeze_restore_and_legacy_ui_default(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        assert controller.synthesisCpuWorkers == 0
        assert controller.setSynthesisCpuWorkers("2")
        assert controller._build_synthesis_request().cpu_workers == 2
        saved = controller.exportOptimizationConfiguration()
        assert saved["synthesis"]["cpu_workers"] == 2

        assert controller.setSynthesisCpuWorkers("1")
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisCpuWorkers == 2
        assert controller._build_synthesis_request().cpu_workers == 2

        legacy = deepcopy(saved)
        legacy["synthesis"].pop("cpu_workers")
        assert controller.loadOptimizationConfiguration(legacy)
        assert controller.synthesisCpuWorkers == 0
        with window_runtime(app, controller) as (window, warnings):
            from PySide6.QtCore import QObject

            assert window.findChild(QObject, "filmSynthesisCpuWorkersField") is not None
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings


def test_cpu_workers_strict_invalid_and_busy_edit_guard(app, tmp_path):
    for value in (True, -1, "-1", "1.5", 1.5, "", "9007199254740992"):
        with pytest.raises(ValueError):
            synthesis_cpu_workers(value)

    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        assert not controller.setSynthesisCpuWorkers("1.5")
        assert controller._pending_edit_error
        assert controller.setSynthesisCpuWorkers("0")
        assert not controller._pending_edit_error
        controller._busy = True
        assert not controller.setSynthesisCpuWorkers("3")
        assert controller.synthesisCpuWorkers == 0


def test_json_and_typed_job_roundtrip_preserve_inner_worker_request(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        assert controller.setObjectiveField("wavelength_min_nm", 550.0)
        assert controller.setObjectiveField("wavelength_max_nm", 550.0)
        assert controller.setSynthesisCpuWorkers("2")
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        payload = film_synthesis_request_to_payload("parallel-roundtrip", request)
        assert payload["schema_version"] == 3
        _, parsed, _ = film_synthesis_request_from_payload(payload)
        assert parsed.cpu_workers == 2
        assert parsed == request

        parameters = canonical_parameters(None, {"payload": payload})
        job = build_job_payload(parameters)
        _, queued_request, _ = deserialize_job_payload(job)
        assert queued_request.cpu_workers == 2
        assert queued_request == request

        incomplete = deepcopy(payload)
        incomplete["request"].pop("cpu_workers")
        with pytest.raises(ValueError, match="every field"):
            film_synthesis_request_from_payload(incomplete)
        with pytest.raises(ValueError, match="every field"):
            canonical_parameters(None, {"payload": incomplete})
        legacy = deepcopy(payload)
        legacy["schema_version"] = 1
        with pytest.raises(ValueError, match="schema_version"):
            film_synthesis_request_from_payload(legacy)
        for invalid in (True, -1, 1.5, "2"):
            malformed = deepcopy(payload)
            malformed["request"]["cpu_workers"] = invalid
            with pytest.raises(ValueError):
                film_synthesis_request_from_payload(malformed)


def test_history_execution_summary_reports_actual_values_or_unrecorded(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.synthesisExecutionSummary == ""
        controller._synthesis_payload = {"result": {"performance": {
            "backend": "headless_process", "requested_workers": 0,
            "effective_workers": 3, "worker_pids": [101, 102, 103],
            "submitted_tasks": 7, "completed_tasks": 6, "serial_fallback_tasks": 1,
            "consumed_tasks": 5, "discarded_tasks": 1,
            "fallback_events": ["pool startup failed"],
        }}}
        summary = controller.synthesisExecutionSummary
        assert "CPU 多进程" in summary
        assert "请求进程数：自动" in summary
        assert "实际工作进程数：3" in summary
        assert "记录的 PID 数：3" in summary
        assert "PID 仅为记录，不代表进程仍在运行" in summary
        assert "任务提交/完成：7/6" in summary
        assert "按原序采用：5" in summary and "投机未采用：1" in summary
        assert "串行回退：1（原因：pool startup failed）" in summary
        controller._synthesis_payload = {"result": {}}
        assert controller.synthesisExecutionSummary == "未记录合成执行诊断"


def test_checkpoint_performance_is_historical_and_missing_counters_stay_unknown():
    from coat_sim.core.film_synthesis_jobs import _historical_checkpoint_performance

    restored = _historical_checkpoint_performance({
        "recorded": True, "backend": "headless_process", "requested_workers": 0,
        "effective_workers": 2, "worker_pids": [1001, 1002],
        "active_worker_pids": [1001, 1002], "submitted_tasks": 4,
        "completed_tasks": 3, "unknown_fields": [],
    })
    assert restored["historical"] is True
    assert restored["execution_state"] == "historical_checkpoint"
    assert restored["actual_work_complete"] is False
    assert restored["active_worker_pids"] == []
    assert restored["worker_pids"] == [1001, 1002]
    assert restored["submitted_tasks"] == 4
    assert restored["consumed_tasks"] is None
    assert "consumed_tasks" in restored["unknown_fields"]

    legacy = _historical_checkpoint_performance(None)
    assert legacy["recorded"] is False
    assert legacy["historical"] is True
    assert legacy["active_worker_pids"] == []
    assert legacy["submitted_tasks"] is None
    assert "submitted_tasks" in legacy["unknown_fields"]
