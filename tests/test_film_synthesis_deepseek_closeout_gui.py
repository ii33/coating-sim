"""Focused GUI regression for cancelled synthesis without an independent audit."""
from __future__ import annotations

from copy import deepcopy
import os
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings
from PySide6.QtGui import QGuiApplication

from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.core.film_synthesis_jobs import is_synthesis_result
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import window_runtime
from tests.test_film_synthesis_cancel_jobs import _cancelled_payload

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def test_cancelled_training_candidate_without_audit_is_not_shown_as_passed(app, tmp_path):
    payload = _cancelled_payload()
    assert is_synthesis_result(payload)
    assert payload["result"]["state"] == "cancelled"
    assert payload["result"]["targets_met"] is False
    assert payload["result"]["validation_score"] is None
    assert payload["best_candidate_audit"]["available"] is False
    assert payload["best_candidate_audit"]["targets_met"] is None

    service, _library, design = fixture(tmp_path)
    film_service = cast(FilmDesignService, service)
    path = tmp_path / "gui-source.cfilm"
    film_service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=film_service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    assert source.session is not None
    assert source.session.request_open_file(str(path))
    controller = FilmOptimizationQuickController(source)
    try:
        assert controller.setSearchMode("synthesis")
        from coat_sim.ui.film_synthesis_quick_adapter import synthesis_result_view
        view = synthesis_result_view(payload)
        assert view.optimization_result is not None
        controller._synthesis_payload = deepcopy(dict(payload))
        controller._result = view.optimization_result
        controller._result_terminal_state = "已取消"
        controller.changed.emit()

        with window_runtime(app, controller) as (window, warnings):
            summary = window.findChild(QObject, "filmOptimizationConditionEmptySummary")
            assert summary is not None
            text = str(summary.property("text"))
            assert "未验证" in text and "独立审计" in text, text
            assert "已达标" not in text, text
            assert controller.conditionErrors == []
            footer = window.findChild(QObject, "filmOptimizationConditionFooterEmptySummary")
            assert footer is not None
            assert str(footer.property("text")) == text
            plot_title = window.findChild(QObject, "filmOptimizationPlotBaselineTitle")
            assert plot_title is not None
            assert "N=0 空栈基线" in str(plot_title.property("text"))
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings
            assert controller.targetsMet is False
            plot = controller.plotData
            assert plot["baseline_label"] == "N=0 空栈基线"
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


def test_angle_preparation_start_exception_clears_only_unsubmitted_attempt(app, tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    film_service = cast(FilmDesignService, service)
    path = tmp_path / "angle-start-failure.cfilm"
    film_service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=film_service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    assert source.session is not None and source.session.request_open_file(str(path))
    controller = FilmOptimizationQuickController(source)

    class ProjectCompute:
        editing_frozen = False

        def capture(self, job_id):
            return object()

        def unregister_thaw_callback(self, _callback):
            pass

    def fail_start(**_kwargs):
        raise RuntimeError("injected coordinator start failure")

    try:
        monkeypatch.setattr(controller, "_project_compute", ProjectCompute())
        monkeypatch.setattr(controller._run_coordinator, "start", fail_start)
        with pytest.raises(RuntimeError, match="injected coordinator start failure"):
            controller._start_angle_preparation(object())
        assert controller._pending_angle_preparation is None
        assert controller._project_operation is None
        assert controller._prepared_angle_job_id is None
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


def test_rejected_prepared_start_discards_reserved_job_identity(app, tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    film_service = cast(FilmDesignService, service)
    path = tmp_path / "angle-submit-rejected.cfilm"
    film_service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=film_service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    assert source.session is not None and source.session.request_open_file(str(path))
    controller = FilmOptimizationQuickController(source)

    def reject_start(**_kwargs):
        return False

    from coat_sim.ui.film_optimization_quick_controller import _PreparedAngleGrid

    operation_id = controller._run_coordinator.operation_serial
    controller._pending_angle_preparation = {
        "operation_id": operation_id,
        "design_revision": controller._source_revision,
        "edit_epoch": controller._source_edit_epoch(),
        "target_identity": controller._compute_target.selection_identity(),
        "job_id": "reserved-angle-job",
    }
    monkeypatch.setattr(controller, "_project_operation", object())
    monkeypatch.setattr(controller, "_start_optimization", reject_start)
    try:
        controller._on_angle_preparation_finished(
            operation_id, _PreparedAngleGrid(object(), {}), None)
        assert controller._prepared_angle_job_id is None
        assert controller._project_operation is None
        assert controller._queue_job_id != "reserved-angle-job"
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize(
    ("terminal_state", "expected"),
    [("cancelled", "已取消"), ("not_found", "未找到可行候选")],
)
def test_no_candidate_terminal_status_is_explicit(app, tmp_path, terminal_state, expected):
    service, _library, design = fixture(tmp_path)
    film_service = cast(FilmDesignService, service)
    path = tmp_path / f"no-candidate-{terminal_state}.cfilm"
    film_service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=film_service,
        geometry_settings=QSettings(str(tmp_path / f"geometry-{terminal_state}.ini"), QSettings.Format.IniFormat))
    assert source.session is not None and source.session.request_open_file(str(path))
    controller = FilmOptimizationQuickController(source)
    try:
        controller._result = None
        controller._result_terminal_state = terminal_state
        assert expected in str(controller.property("statusText"))
        assert controller._result_application_eligible is False
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


def test_history_status_stays_read_only_when_source_is_stale(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    film_service = cast(FilmDesignService, service)
    path = tmp_path / "historical-candidate.cfilm"
    film_service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=film_service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    assert source.session is not None and source.session.request_open_file(str(path))
    controller = FilmOptimizationQuickController(source)
    try:
        controller._inspection_objectives = []
        controller._result_terminal_state = "历史合成结果 / cancelled"
        controller._source_stale = True
        controller._result_application_eligible = False
        status = str(controller.property("statusText"))
        assert "只读历史候选" in status
        assert "不会应用到当前设计" in status
        assert controller._source_stale is True
        assert controller._result_application_eligible is False
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()
