"""Real Qt Quick input: running configurations stay navigable but immutable."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QInputDevice, QWheelEvent
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tests import test_film_resource_estimate_ui as resource_ui

resource_runtime = resource_ui.runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def item(window, name):
    found = window.findChild(QQuickItem, name)
    if found is None:
        pending = [window.contentItem()]
        while pending:
            candidate = pending.pop()
            if candidate.objectName() == name:
                return candidate
            pending.extend(candidate.childItems())
    assert found is not None, name
    return found


def wheel(window, scroll):
    flickable = scroll.property("contentItem")
    before = flickable.property("contentY")
    point = scroll.mapToScene(QPointF(scroll.width() / 2, scroll.height() / 2))
    event = QWheelEvent(point, QPointF(window.mapToGlobal(point.toPoint())),
                        QPoint(), QPoint(0, -480), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QGuiApplication.sendEvent(window, event)
    QTest.qWait(200)
    if flickable.property("contentHeight") > scroll.height() + 1:
        assert flickable.property("contentY") > before, (flickable.property("contentHeight"), scroll.height())


def set_running(controller, active):
    # Exercise the actual shared QML busy binding without launching a solver.
    controller._busy = active
    controller.changed.emit()
    QTest.qWait(50)


def test_running_scroll_and_locked_editors(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    for index in range(14):
        design["front_layers"].append({**deepcopy(design["front_layers"][0]), "id": f"extra-{index}"})
    with optimizer(service, design) as (controller, _source, session):
        original_workflow = session.workflow_configuration
        session.workflow_configuration = lambda scope: {} if scope == "simulation" else original_workflow(scope)
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(1000)
            window.setHeight(680)
            tabs = item(window, "filmOptimizationSetupTabs")
            modes = [
                ("fixed_topology", "filmOptimizationMaxIterationsField"),
                ("reduction", "filmReductionModeBox"),
                ("structure", "filmOptimizationStructureModeBox"),
                ("synthesis", "filmSynthesisFrontSurfaceCheck"),
                ("automatic_repair", "filmManufacturing_lower_limit_nm"),
                ("ideal_sensitivity", "filmIdealSensitivityModeBox"),
            ]
            for mode, editor in modes:
                assert controller.setSearchMode(mode)
                tabs.setProperty("currentIndex", 2)
                QTest.qWait(100)
                scroll = item(window, "filmOptimizationSolverOptionsScroll")
                editable = item(window, editor)
                assert editable.isEnabled(), mode
                if mode == "structure":
                    assert not item(window, "filmStructureGrowthCyclesField").isEnabled()
                before = deepcopy(controller.exportOptimizationConfiguration())
                for run in range(2):
                    set_running(controller, True)
                    assert scroll.isEnabled()
                    assert not editable.isEnabled(), mode
                    blocked = item(window, "filmOptimizationSearchModeBox")
                    assert not blocked.isEnabled()
                    if mode == "fixed_topology" and run == 0:
                        wheel(window, scroll)
                        flickable = scroll.property("contentItem")
                        flickable.setProperty("contentY", 0)
                        QTest.qWait(100)
                        start = scroll.mapToScene(QPointF(10, scroll.height() * .75)).toPoint()
                        end = scroll.mapToScene(QPointF(10, scroll.height() * .25)).toPoint()
                        device = QTest.createTouchDevice(QInputDevice.DeviceType.TouchScreen)
                        QTest.touchEvent(window, device).press(0, start, window).commit()
                        QTest.qWait(30)
                        QTest.touchEvent(window, device).move(0, end, window).commit()
                        QTest.qWait(30)
                        QTest.touchEvent(window, device).release(0, end, window).commit()
                        QTest.qWait(200)
                        assert flickable.property("contentY") > 0
                    assert controller.exportOptimizationConfiguration() == before
                    set_running(controller, False)
                    assert editable.isEnabled(), mode
                    if mode == "structure":
                        assert not item(window, "filmStructureGrowthCyclesField").isEnabled()
            assert controller.setSearchMode("fixed_topology")
            before = deepcopy(controller.exportOptimizationConfiguration())
            set_running(controller, True)
            for index, scroll_name, field in [
                (0, "filmOptimizationObjectiveEditorScroll", "filmOptimizationWavelengthMinField"),
                (1, "filmOptimizationVariableScroll", "filmOptimizationLower_extra-0"),
            ]:
                tabs.setProperty("currentIndex", index)
                QTest.qWait(100)
                scroll = item(window, scroll_name)
                assert scroll.isEnabled()
                assert not item(window, field).isEnabled()
                wheel(window, scroll)
                assert controller.exportOptimizationConfiguration() == before
            set_running(controller, False)
            assert item(window, "filmOptimizationLower_extra-0").isEnabled()
            tabs.setProperty("currentIndex", 0)
            QTest.qWait(30)
            assert item(window, "filmOptimizationWavelengthMinField").isEnabled()
            assert warnings == []


def test_plot_toolbar_single_row_and_narrow_access(app, tmp_path):
    from coat_sim.ui.theme_service import ThemeService
    from PySide6.QtQml import QQmlEngine, QQmlExpression

    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller) as (window, warnings):
            window.setHeight(680)
            names = ["filmOptimizationPlotTitle", "filmCandidateLivePreviewToggle",
                     "filmOptimizationFullDomainToggle", "filmCandidateLiveMetricBox",
                     "filmOptimizationYAxisAuto", "filmOptimizationPlotObjectiveBox",
                     "filmOptimizationPlotAngleBox", "filmCandidateLivePreviewStatus",
                     "filmOptimizationPlotBaselineTitle"]
            for scale in (1.0, 1.4):
                ThemeService.instance().configure(mode="light", font_scale=scale, density_scale=1.0)
                window.setWidth(1340)
                QTest.qWait(100)
                viewport = item(window, "filmOptimizationPlotControlsViewport")
                controls = [item(window, name) for name in names]
                assert viewport.property("contentWidth") <= viewport.width() + 1
                assert not item(window, "filmOptimizationPlotControlsScrollBar").isVisible()
                rects = [(control.mapToScene(QPointF()), control.width(), control.height()) for control in controls]
                for (left, width, height), (right, _next_width, next_height) in zip(rects, rects[1:], strict=False):
                    assert left.x() + width <= right.x() + 1
                    assert abs((left.y() + height / 2) - (right.y() + next_height / 2)) <= 1
                for control in controls:
                    left = control.mapToItem(viewport, QPointF()).x()
                    assert control.isVisible() and left >= -1
                    assert left + control.width() <= viewport.width() + 1
                # The compact objective selector opens a fully readable, on-screen popup.
                objective = item(window, "filmOptimizationPlotObjectiveBox")
                point = objective.mapToScene(QPointF(objective.width() / 2, objective.height() / 2)).toPoint()
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(100)
                popup_check = QQmlExpression(
                    QQmlEngine.contextForObject(objective), objective,
                    "popup.visible && popup.contentItem.itemAtIndex(0).text === currentText "
                    "&& popup.contentItem.itemAtIndex(0).contentItem.contentWidth "
                    "<= popup.contentItem.itemAtIndex(0).contentItem.width "
                    "&& popup.contentItem.mapToItem(null, 0, 0).x >= 0 "
                    "&& popup.contentItem.mapToItem(null, 0, 0).x + popup.contentItem.width <= window.width")
                assert popup_check.evaluate()[0] and not popup_check.hasError()
                QTest.keyClick(window, Qt.Key.Key_Escape)
                QTest.qWait(50)
                # The two potentially elided labels retain their complete text in tooltips.
                for status in controls[-2:]:
                    expression = QQmlExpression(QQmlEngine.contextForObject(status), status,
                                                "ToolTip.text === text && text.length > 0")
                    assert expression.evaluate()[0] and not expression.hasError()
                    point = status.mapToScene(QPointF(status.width() / 2, status.height() / 2)).toPoint()
                    QTest.mouseMove(window, point, 10)
                    QTest.qWait(1000)
                    shown = QQmlExpression(QQmlEngine.contextForObject(status), status, "ToolTip.visible")
                    assert shown.evaluate()[0] and not shown.hasError()
                canvas = item(window, "filmOptimizationComparisonCanvas")
                assert canvas.mapToScene(QPointF()).y() <= viewport.mapToScene(QPointF()).y() + viewport.height() + 8
                window.setWidth(720)
                QTest.qWait(100)
                assert viewport.property("contentWidth") > viewport.width()
                bar = item(window, "filmOptimizationPlotControlsScrollBar")
                assert bar.isVisible()
                first = bar.mapToScene(QPointF(5, bar.height() / 2)).toPoint()
                last = bar.mapToScene(QPointF(bar.width() - 5, bar.height() / 2)).toPoint()
                QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, first)
                QTest.mouseMove(window, last, 100)
                QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, last)
                QTest.qWait(50)
                assert viewport.property("contentX") > 0
                last_label = controls[-1]
                right_edge = last_label.mapToItem(viewport, QPointF(last_label.width(), 0)).x()
                assert right_edge <= viewport.width() + 1
                assert item(window, "filmCandidateLivePreviewStatus").isVisible()
                assert item(window, "filmOptimizationPlotBaselineTitle").isVisible()
            assert warnings == []


def test_resource_running_scroll_and_lock(resource_runtime, tmp_path):
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController

    app, source, _design_window, _warnings = resource_runtime
    service, _library, design = fixture(tmp_path / "materials")
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    owner = FilmOptimizationQuickController(source)
    estimator = source.resourceEstimateController
    try:
        assert owner.setSearchMode("resource_assessment")
        with window_runtime(app, owner) as (window, warnings):
            window.resize(900, 680)
            item(window, "filmOptimizationSetupTabs").setProperty("currentIndex", 2)
            QTest.qWait(100)
            scroll = item(window, "filmOptimizationSolverOptionsScroll")
            mode = item(window, "resourceAssessmentMode")
            assert mode.isEnabled()
            assert not item(window, "filmResourceEstimateLayerLimit").isEnabled()
            original = estimator.mode
            for _ in range(2):
                estimator._busy = True
                estimator.changed.emit()
                QTest.qWait(40)
                assert owner.busy and scroll.isEnabled() and not mode.isEnabled()
                assert estimator.mode == original
                estimator._busy = False
                estimator.changed.emit()
                QTest.qWait(40)
                assert mode.isEnabled()
                assert not item(window, "filmResourceEstimateLayerLimit").isEnabled()
            assert warnings == []
    finally:
        estimator._busy = False
        owner.shutdown(1000)
