"""Real QML budget-dialog interactions without starting a solver or queue worker.

The capture controller replaces only the final submission slot. Continuation
availability, frozen defaults, provenance guards and explanatory properties are
provided by the real controller over isolated temporary source/material data.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QMetaObject, QPointF, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
import shiboken6

from coat_sim.core.film_optimization import freeze_film_optimization_request
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_optimization_continue_source import build_source_binding
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture as material_fixture
from tests.qt_test_guard import qml_item_window


pytestmark = [pytest.mark.qt, pytest.mark.contract]
QML = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml"


class _Session:
    """Minimal observable editor session with explicit source-guard fields."""

    def __init__(self, design):
        self.design = deepcopy(design)
        self.design_revision = 19
        self.path = ""
        self.configuration: dict[str, Any] = {}
        self.subscriber = None

    def snapshot(self):
        return SimpleNamespace(
            design=deepcopy(self.design), design_revision=self.design_revision,
            path=self.path, pending_confirmation=False, expression_busy=False, dirty=False,
        )

    def subscribe(self, callback):
        self.subscriber = callback
        return lambda: setattr(self, "subscriber", None)

    def workflow_configuration(self, scope):
        assert scope == "optimization"
        return deepcopy(self.configuration)

    def set_workflow_configuration(self, scope, configuration):
        assert scope == "optimization"
        self.configuration = deepcopy(configuration)
        return True


class _Source(QObject):
    pendingEditEpochChanged = Signal()
    closeConfirmed = Signal()

    def __init__(self, service, session):
        super().__init__()
        self.service = service
        self.session = session
        self.pendingEditEpoch = 4

    def schematicLayers(self, side):
        return [{"id": row["id"], "physicalNmValue": row["thickness"]["value"]}
                for row in self.session.design.get(side + "_layers", [])]


class _BudgetCaptureController(FilmOptimizationQuickController):
    """Keep actual view-model/source guards but intercept the queue boundary."""

    def __init__(self, source):
        super().__init__(source)
        self.submitted_budgets: list[dict[str, Any]] = []
        self.submit_success = True

    @Slot("QVariantMap", result=bool)
    def continueOptimization(self, budget):
        self.submitted_budgets.append(deepcopy(budget))
        self._error = "" if self.submit_success else "测试提交失败，尚未创建新作业。"
        self.changed.emit()
        return self.submit_success


@dataclass
class _Runtime:
    app: QGuiApplication
    window: QQuickWindow
    controller: _BudgetCaptureController
    source: _Source
    session: _Session
    warnings: list[str]

    def item(self, object_name):
        item = self.window.findChild(QObject, object_name)
        assert item is not None, object_name
        return item

    def settle(self):
        QTest.qWait(20)
        self.app.processEvents()

    def invoke(self, owner, method):
        assert QMetaObject.invokeMethod(owner, method), method
        self.settle()

    def click(self, object_name):
        item = cast(QQuickItem, self.item(object_name))
        assert item.isVisible(), object_name
        target_window = qml_item_window(item)
        assert target_window is not None, object_name
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(target_window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, position)
        self.settle()

    @property
    def dialog(self):
        return self.item("filmOptimizationContinueBudgetDialog")

    @property
    def submit(self):
        return self.item("filmOptimizationContinueBudgetSubmitButton")

    def set_text(self, suffix, text):
        assert self.item("filmOptimizationContinue" + suffix + "Field").setProperty("text", text)
        self.app.processEvents()

    def open_dialog(self):
        button = cast(QQuickItem, self.item("filmOptimizationContinueButton"))
        if button.x() < 0:
            # The shared toolbar intentionally clips overflowed original
            # controls. Exercise the visible menu entry rather than clicking
            # its off-window source, including the large-font compact case.
            self.click("operationBarOverflowButton")
            self.click("operationBarOverflow_filmOptimizationContinueButton")
        else:
            self.click("filmOptimizationContinueButton")
        assert self.dialog.property("visible")

    def synthesis_record(self):
        record = self.controller._continuation_record
        assert record is not None
        fixed_request = record["request"]
        # The QML boundary does not execute this request. Preserve the real
        # frozen closure so the normal source/material guards remain active.
        record.update(
            mode="synthesis",
            request=SimpleNamespace(
                frozen_material_snapshot=fixed_request.frozen_material_snapshot,
                max_cycles=5, max_evaluations=None, max_seconds=1.5,
            ),
            synthesis_payload={"result": {"cycles": 3, "evaluations": 23}},
            editable_snapshot=deepcopy(fixed_request.frozen_material_snapshot),
        )
        self.controller.changed.emit()
        self.settle()


@pytest.fixture
def runtime(tmp_path):
    app = cast(QGuiApplication, QGuiApplication.instance() or QGuiApplication([]))
    service, _library, design = material_fixture(tmp_path)
    session = _Session(design)
    source = _Source(service, session)
    controller = _BudgetCaptureController(source)
    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    component = QQmlComponent(engine)
    warnings: list[str] = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    window = None
    try:
        request = freeze_film_optimization_request(service, controller._build_request())
        request = replace(request, max_iterations=123, max_evaluations=456)
        binding = build_source_binding(service, session.snapshot(), request)
        record = {
            "mode": "fixed_topology", "request": request,
            "result": SimpleNamespace(iterations=7, evaluations=23, max_normalized_error=.125),
            "synthesis_payload": None, "editable_snapshot": None,
            "source_binding": binding, "job_id": "qml-retained-cancelled-job",
            "spec_fingerprint": "qml-original-spec", "session": session,
            "source_controller": source, "materials_owner": service.materials,
            "project_operation": None, "allowed_revision": session.design_revision,
            "allowed_epoch": source.pendingEditEpoch,
            "allowed_design_hashes": {binding["source_design_sha256"]}, "history": False,
        }
        controller._continuation_record = record
        controller._prior_run_fallback = record
        assert controller.canContinueOptimization, controller.continueDisabledReason
        component.loadUrl(QUrl.fromLocalFile(str(QML / "FilmOptimizationWindow.qml")))
        assert not component.isError(), component.errorString()
        window = component.createWithInitialProperties({"controller": controller})
        assert isinstance(window, QQuickWindow), component.errorString()
        window.resize(1100, 760)
        window.show()
        window.requestActivate()
        state = _Runtime(app, window, controller, source, session, warnings)
        state.settle()
        yield state
        assert not warnings, warnings
    finally:
        if window is not None:
            window.setProperty("allowClose", True)
            cast(QQuickWindow, window).hide()
            shiboken6.delete(window)
        shiboken6.delete(component)
        shiboken6.delete(engine)
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(
            wait=True, cancel_futures=True)
        shiboken6.delete(controller)
        shiboken6.delete(source)
        theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
        app.processEvents()


def test_no_retained_candidate_disables_continue_and_explains_why(runtime):
    state = runtime
    state.controller._continuation_record = None
    state.controller.changed.emit()
    state.settle()
    button = state.item("filmOptimizationContinueButton")
    assert button.property("text") == "从保留结果继续（重新启动求解器）"
    assert not button.property("enabled")
    reason = state.item("filmOptimizationContinueDisabledReason")
    assert reason.property("visible") and reason.property("text")
    state.invoke(state.window, "continueFromButton")
    assert not state.dialog.property("visible")
    assert state.controller.submitted_budgets == []


def test_fixed_dialog_uses_frozen_defaults_and_submits_only_new_budget(runtime):
    state = runtime
    # Changing editable current limits must not become the continuation defaults.
    state.controller._max_iterations = 987
    state.controller._max_evaluations = 654
    state.controller.changed.emit()
    state.open_dialog()
    assert state.item("filmOptimizationContinueMaxIterationsField").property("text") == "123"
    assert state.item("filmOptimizationContinueMaxEvaluationsField").property("text") == "456"
    assert not state.item("filmOptimizationContinueMaxCyclesField").property("visible")
    assert not state.item("filmOptimizationContinueMaxSecondsField").property("visible")
    spent = state.item("filmOptimizationContinuePreviousSpent").property("text")
    assert "迭代 7" in spent and "评价 23" in spent and "时长未记录" in spent
    assert state.item("filmOptimizationContinueDialogFallback").property("visible")
    assert "不是精确续算" in state.item("filmOptimizationContinueExplanation").property("text")
    for invalid in ("", "0", "-1", "1.5", "9007199254740992"):
        state.set_text("MaxIterations", invalid)
        assert not state.submit.property("enabled"), invalid
        assert state.item("filmOptimizationContinueBudgetError").property("visible")
    state.set_text("MaxIterations", "12")
    state.set_text("MaxEvaluations", "34")
    assert state.submit.property("enabled")
    state.controller.submit_success = False
    state.click("filmOptimizationContinueBudgetSubmitButton")
    assert state.controller.submitted_budgets == [{"max_iterations": 12, "max_evaluations": 34}]
    assert state.dialog.property("visible")
    assert state.item("filmOptimizationContinueSubmissionError").property("visible")
    state.controller.submit_success = True
    state.click("filmOptimizationContinueBudgetSubmitButton")
    assert state.controller.submitted_budgets[-1] == {"max_iterations": 12, "max_evaluations": 34}
    assert not state.dialog.property("visible")
    assert state.controller._continuation_record["request"].max_iterations == 123


def test_cancel_creates_no_attempt_and_reopening_refreshes_original_limits(runtime):
    state = runtime
    original_design = deepcopy(state.session.design)
    original_record = state.controller._continuation_record
    state.open_dialog()
    state.set_text("MaxIterations", "77")
    state.click("filmOptimizationContinueBudgetCancelButton")
    assert not state.dialog.property("visible")
    assert state.controller.submitted_budgets == []
    assert state.controller._continuation_record is original_record
    assert state.session.design == original_design
    state.open_dialog()
    assert state.item("filmOptimizationContinueMaxIterationsField").property("text") == "123"
    assert state.item("filmOptimizationContinueMaxEvaluationsField").property("text") == "456"
    state.click("filmOptimizationContinueBudgetCancelButton")


def test_synthesis_dialog_submits_explicit_null_limits_and_independent_budget(runtime):
    state = runtime
    state.synthesis_record()
    state.open_dialog()
    assert state.dialog.property("synthesisMode")
    assert not state.item("filmOptimizationContinueMaxIterationsField").property("visible")
    assert state.item("filmOptimizationContinueMaxCyclesField").property("text") == "5"
    assert state.item("filmOptimizationContinueMaxEvaluationsField").property("text") == ""
    assert state.item("filmOptimizationContinueMaxSecondsField").property("text") == "1.5"
    assert "未知" in state.item("filmOptimizationContinuePreviousSpent").property("text")
    state.set_text("MaxCycles", "")
    state.set_text("MaxSeconds", "")
    assert not state.submit.property("enabled")
    for invalid in ("0", "-1", "nan", "inf"):
        state.set_text("MaxSeconds", invalid)
        assert not state.submit.property("enabled"), invalid
    state.set_text("MaxSeconds", "1e-7")
    assert state.submit.property("enabled")
    state.click("filmOptimizationContinueBudgetSubmitButton")
    assert state.controller.submitted_budgets == [{
        "max_cycles": None, "max_evaluations": None, "max_seconds": 1e-7,
    }]
    assert not state.dialog.property("visible")


@pytest.mark.parametrize("conflict", ["pending_edit", "revision"])
def test_source_staleness_while_dialog_open_blocks_submission(runtime, conflict):
    state = runtime
    state.open_dialog()
    if conflict == "pending_edit":
        state.source.pendingEditEpoch += 1
        state.source.pendingEditEpochChanged.emit()
    else:
        state.session.design_revision += 1
        state.controller.changed.emit()
    state.settle()
    assert not state.controller.canContinueOptimization
    assert not state.item("filmOptimizationContinueButton").property("enabled")
    assert not state.submit.property("enabled")
    assert state.dialog.property("visible")
    reason = state.item("filmOptimizationContinueDialogDisabledReason")
    assert reason.property("visible") and reason.property("text") == state.controller.continueDisabledReason
    state.click("filmOptimizationContinueBudgetSubmitButton")
    state.invoke(state.dialog, "submitNewAttempt")
    assert state.controller.submitted_budgets == []
    state.click("filmOptimizationContinueBudgetCancelButton")
    assert not state.dialog.property("visible")


@pytest.mark.parametrize("font_scale", [1.0, 1.5])
def test_compact_dialog_is_bounded_with_reachable_actions(runtime, font_scale):
    state = runtime
    ThemeService.instance().configure(mode="light", font_scale=font_scale, density_scale=1.0)
    state.window.resize(720, 500)
    state.settle()
    state.synthesis_record()
    state.open_dialog()
    dialog = state.dialog
    x, y = float(dialog.property("x")), float(dialog.property("y"))
    width, height = float(dialog.property("width")), float(dialog.property("height"))
    assert x >= 0 and y >= 0
    assert x + width <= state.window.width() + 1
    assert y + height <= state.window.height() + 1
    for name in ("filmOptimizationContinueBudgetSubmitButton", "filmOptimizationContinueBudgetCancelButton"):
        button = cast(QQuickItem, state.item(name))
        start = button.mapToScene(QPointF(0, 0))
        end = button.mapToScene(QPointF(button.width(), button.height()))
        assert start.x() >= 0 and start.y() >= 0
        assert end.x() <= state.window.width() + 1 and end.y() <= state.window.height() + 1
    assert not state.window.grabWindow().isNull()
    state.click("filmOptimizationContinueBudgetCancelButton")
    assert not state.dialog.property("visible")
