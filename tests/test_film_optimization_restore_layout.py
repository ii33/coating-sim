"""ID-safe workflow recovery and the optimizer's shared sidebar/device UI."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
import shiboken6

from coat_sim.core.film_optimization import FilmOptimizationProgress
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_target_sampling import _WorkflowSession
from tests.test_film_optimization_run_contract import _Source


pytestmark = pytest.mark.qt
ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "coat_sim/ui/qml"


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@contextmanager
def optimizer(service, design, configuration=None):
    session = _WorkflowSession(deepcopy(design), 19, configuration)
    source = _Source(service, session)
    controller = FilmOptimizationQuickController(source)
    try:
        yield controller, source, session
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(
            wait=True, cancel_futures=True)
        shiboken6.delete(controller)
        shiboken6.delete(source)


@contextmanager
def window_runtime(app, controller, *, source=None):
    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    warnings: list[str] = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    component = QQmlComponent(engine)
    if source is None:
        component.loadUrl(QUrl.fromLocalFile(str(QML / "FilmOptimizationWindow.qml")))
    else:
        component.setData(source.encode(), QUrl.fromLocalFile(str(QML / "_optimizer_ui_test.qml")))
    window = None
    try:
        assert not component.isError(), component.errorString()
        window = component.createWithInitialProperties({"controller": controller})
        assert isinstance(window, QQuickWindow), component.errorString()
        window.show()
        window.requestActivate()
        QTest.qWait(40)
        app.processEvents()
        yield window, warnings
    finally:
        if window is not None:
            window.setProperty("allowClose", True)
            cast(QQuickWindow, window).hide()
            shiboken6.delete(window)
        shiboken6.delete(component)
        shiboken6.delete(engine)
        theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
        app.processEvents()


def saved_configuration(service, design):
    with optimizer(service, design) as (controller, _source, _session):
        configuration = controller.exportOptimizationConfiguration()
        configuration["objectives"][0]["name"] = "保留的目标"
        configuration["objectives"][0]["target_value_percent"] = 17.25
        configuration["solver"]["max_evaluations"] = 321
        return configuration


def test_document_restore_retains_only_exact_side_and_layer_matches(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"].append({**deepcopy(design["front_layers"][0]), "id": "surviving-layer"})
    configuration = saved_configuration(service, design)
    removed = configuration["variables"][0]
    removed.update(lower_nm=12, upper_nm=34)
    retained = configuration["variables"][1]
    retained.update(enabled=True, lower_nm=65, upper_nm=135)
    changed = deepcopy(design)
    # Identical material, thickness and list index do not prove identity.
    changed["front_layers"][0]["id"] = "replacement-layer"
    with optimizer(service, changed, configuration) as (controller, _source, session):
        assert controller.error == ""
        assert controller._objectives[0]["name"] == "保留的目标"
        assert controller._max_evaluations == 321
        replacement, surviving = controller.variables
        assert (surviving["lower_nm"], surviving["upper_nm"], surviving["enabled"]) == (65, 135, True)
        assert replacement["enabled"] is False
        assert (replacement["lower_nm"], replacement["upper_nm"]) == (0, None)
        assert removed["layer_id"] in controller.configurationRestoreNotice
        assert "未转移" in controller.configurationRestoreNotice
        assert controller.canStart
        assert session.configuration_writes == []
        assert session.workflow_configuration("optimization") == configuration


def test_all_replaced_layers_require_explicit_selection_but_keep_targets(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    configuration = saved_configuration(service, design)
    changed = deepcopy(design)
    changed["front_layers"][0]["id"] = "new-stable-layer"
    with optimizer(service, changed, configuration) as (controller, _source, session):
        assert controller.error == ""
        assert controller._active_configuration_error() == ""
        assert not controller.canStart
        assert controller.computeTarget.canSubmit  # CPU readiness is unrelated.
        assert "约束页选择" in controller.statusText
        assert controller._objectives[0]["target_value_percent"] == 17.25
        assert session.configuration_writes == []
        controller.setVariableEnabled("front", "new-stable-layer", True)
        assert controller.canStart
        stored = session.workflow_configuration("optimization")
        assert stored["variables"][0]["layer_id"] == "new-stable-layer"
        assert stored["variables"][0]["enabled"] is True
        assert stored["objectives"][0]["name"] == "保留的目标"


@pytest.mark.parametrize("corruption", ("duplicate", "invalid_bound"))
def test_orphan_tolerance_does_not_hide_malformed_saved_configuration(app, tmp_path, corruption):
    service, _library, design = fixture(tmp_path)
    configuration = saved_configuration(service, design)
    configuration["variables"][0]["layer_id"] = "orphan-layer"
    if corruption == "duplicate":
        configuration["variables"].append(deepcopy(configuration["variables"][0]))
    else:
        configuration["variables"][0]["lower_nm"] = -1
    with optimizer(service, design, configuration) as (controller, _source, session):
        assert "未恢复优化配置" in controller.error
        assert not controller.canStart
        assert controller._objectives[0]["name"] != "保留的目标"
        assert session.configuration_writes == []


def test_same_source_refresh_never_opts_new_layers_into_old_variable_scope(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setVariableBound("front", "existing-stable-layer", "lower_nm", 70)
        session.design["front_layers"].append({**deepcopy(design["front_layers"][0]), "id": "added-layer"})
        session.design_revision += 1
        controller.refreshDesign()
        existing, added = controller.variables
        assert existing["lower_nm"] == 70 and existing["enabled"]
        assert not added["enabled"]
        assert "恢复时未自动勾选" in controller.configurationRestoreNotice
        assert controller.canStart


def test_progress_statistics_use_typed_counts_and_reject_late_attempts(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.progressStatistics == "迭代 0 · 评估 0"
        controller._busy = True
        controller._active_operation_id = 12
        progress = FilmOptimizationProgress("running", .42, 7, 31, .00125, "正在评估")
        controller._on_progress_for_operation(11, progress)
        assert controller.progressStatistics == "迭代 0 · 评估 0"
        controller._on_progress_for_operation(12, progress)
        assert controller.progressStatistics == "迭代 7 · 评估 31 · 最大违约 0.125%"
        controller._busy = False
        controller._invalidate_result()
        assert controller.progressStatistics == "迭代 0 · 评估 0"


def click(app, window, item):
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.qWait(20)
    app.processEvents()


def test_sidebar_reuses_content_and_toolbar_has_statistics_without_details_button(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller) as (window, warnings):
            window.resize(1100, 760)
            QTest.qWait(30)
            sidebar = window.findChild(QQuickItem, "filmOptimizationDetailsSidebar")
            toggle = window.findChild(QQuickItem, "filmOptimizationSidebarToggle")
            assert sidebar is not None and toggle is not None
            assert window.findChild(QObject, "filmOptimizationDetailsButton") is None
            assert window.findChild(QQuickItem, "filmOptimizationApplyButton").property("text") == "应用"
            assert window.findChild(QQuickItem, "filmOptimizationProgress").property("text") == controller.progressStatistics
            assert not sidebar.isVisible()
            click(app, window, toggle)
            assert sidebar.isVisible() and window.property("detailsDocked")
            status = window.findChild(QQuickItem, "filmOptimizationSidebarStatus")
            window.resize(720, 500)
            QTest.qWait(30)
            assert sidebar.isVisible() and not window.property("detailsDocked")
            for _ in range(2):
                click(app, window, toggle)
                assert not sidebar.isVisible()
                QTest.keyClick(window, Qt.Key.Key_F4)
                app.processEvents()
                assert sidebar.isVisible()
            assert status is window.findChild(QQuickItem, "filmOptimizationSidebarStatus")
            assert not window.grabWindow().isNull()
            assert not warnings, warnings


@pytest.mark.parametrize("size,scale,near_bottom", ((QPoint(720, 500), 1., False),
    (QPoint(360, 260), 1.5, False), (QPoint(360, 260), 1.5, True)))
def test_device_popup_is_bounded_and_scrollable_in_small_windows(app, tmp_path, size, scale, near_bottom):
    service, _library, design = fixture(tmp_path)
    source = '''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    required property var controller
    property bool allowClose: true
    property real contentTopMargin: 44
    width: 720; height: 500
    ComputeTargetSelector {
        objectName: "qaSelector"
        x: parent.width - width - 4
        y: 48
        width: 150
        target: controller.computeTarget
    }
}'''
    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller, source=source) as (window, warnings):
            ThemeService.instance().configure(mode="light", font_scale=scale, density_scale=scale)
            window.resize(size.x(), size.y())
            selector = window.findChild(QQuickItem, "qaSelector")
            if near_bottom:
                selector.setY(window.height() - selector.height() - 4)
            app.processEvents()
            controller.computeTarget._message += " 设备能力说明。" * 12
            controller.computeTarget.changed.emit()
            button = window.findChild(QQuickItem, "computeTargetButton")
            popup = window.findChild(QObject, "computeTargetPopup")
            assert popup is not None and button is not None
            for _ in range(2):
                click(app, window, button)
                assert popup.property("visible")
                x, y = float(popup.property("x")), float(popup.property("y"))
                width, height = float(popup.property("width")), float(popup.property("height"))
                assert x >= 0 and x + width <= window.width() + .5
                margin = float(popup.property("edgeMargin"))
                anchor_x = selector.mapToScene(QPointF(0, 0)).x()
                expected_x = max(margin, min(window.width() - width - margin, anchor_x))
                assert x == pytest.approx(expected_x, abs=.5)
                assert y >= window.property("contentTopMargin")
                assert y + height <= window.height() + .5
                window.resize(size.x() + 20, size.y() + 20)
                QTest.qWait(20)
                app.processEvents()
                assert float(popup.property("y")) + float(popup.property("height")) <= window.height() + .5
                window.resize(size.x(), size.y())
                QTest.qWait(20)
                app.processEvents()
                scroll = window.findChild(QQuickItem, "computeTargetPopupScroll")
                assert scroll is not None
                if size.y() < 300:
                    assert scroll.property("contentHeight") > scroll.height()
                QTest.keyClick(window, Qt.Key.Key_Escape)
                app.processEvents()
                assert not popup.property("visible")
                assert button.hasActiveFocus()
            assert not window.grabWindow().isNull()
            assert not warnings, warnings


def test_side_is_part_of_layer_identity_and_corrected_config_clears_old_error(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    configuration = saved_configuration(service, design)
    changed = deepcopy(design)
    changed["back_layers"] = changed["front_layers"]
    changed["front_layers"] = []
    with optimizer(service, changed, configuration) as (controller, _source, session):
        assert len(controller.variables) == 1
        assert controller.variables[0]["side"] == "back"
        assert not controller.variables[0]["enabled"]
        assert "front/existing-stable-layer" in controller.configurationRestoreNotice
        valid = controller.exportOptimizationConfiguration()
        invalid = deepcopy(valid)
        invalid["variables"][0]["layer_id"] = "nonexistent-layer"
        assert not controller.loadOptimizationConfiguration(invalid)
        assert controller.error
        session._configuration = valid
        assert controller._restore_session_configuration(reset_missing=False)
        assert not controller.error
        assert not controller._active_configuration_error()


def test_sidebar_f4_does_not_steal_native_combo_popup(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller) as (window, warnings):
            window.resize(1100, 760)
            QTest.qWait(40)
            combo = window.findChild(QQuickItem, "filmOptimizationMetricBox")
            sidebar = window.findChild(QQuickItem, "filmOptimizationDetailsSidebar")
            assert combo is not None
            combo.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_F4)
            app.processEvents()
            assert not sidebar.isVisible()
            # Escape dismisses the native list before the shared rail shortcut.
            QTest.keyClick(window, Qt.Key.Key_Escape)
            window.findChild(QQuickItem, "filmOptimizationShortcutFocusSink").forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_F4)
            app.processEvents()
            assert sidebar.isVisible()
            assert not warnings, warnings


def test_overflow_device_popup_uses_visible_anchor_and_consumes_outside_start_press(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    source = '''import QtQuick
import QtQuick.Controls
import "."
import "theme_controls" as ThemeControls
ApplicationWindow {
    required property var controller
    property bool allowClose: true
    property real contentTopMargin: 44
    property int submissions: 0
    width: 360; height: 360
    ResponsiveOperationBar {
        objectName: "qaBar"
        x: 12; y: 48; width: parent.width - 24
        ThemeControls.ThemeButton {
            objectName: "qaStart"; text: "开始优化"; width: 200
            onClicked: submissions += 1
        }
        ComputeTargetSelector {
            objectName: "qaCompute"; width: 170
            target: controller.computeTarget
        }
        ThemeControls.ThemeButton { text: "保存候选"; width: 200 }
    }
}'''

    def visual(root, name):
        if root.objectName() == name:
            return root
        for child in root.childItems():
            match = visual(child, name)
            if match is not None:
                return match
        return None

    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller, source=source) as (window, warnings):
            QTest.qWait(50)
            overflow = window.findChild(QQuickItem, "operationBarOverflowButton")
            selector = window.findChild(QQuickItem, "qaCompute")
            popup = window.findChild(QObject, "computeTargetPopup")
            assert selector.property("operationBarOverflowAnchor") == overflow
            click(app, window, overflow)
            action = visual(window.contentItem(), "operationBarOverflow_qaCompute")
            assert action is not None and action.isVisible()
            click(app, window, action)
            assert popup.property("visible")
            assert popup.property("anchorItem") == overflow
            assert float(popup.property("x")) >= 0
            assert float(popup.property("x")) + float(popup.property("width")) <= window.width()
            QTest.keyClick(window, Qt.Key.Key_Escape)
            app.processEvents()
            window.resize(900, 500)
            QTest.qWait(50)
            assert selector.property("operationBarOverflowAnchor") is None
            click(app, window, window.findChild(QQuickItem, "computeTargetButton"))
            assert popup.property("visible")
            margin = float(popup.property("edgeMargin"))
            expected_x = max(margin, min(window.width() - float(popup.property("width")) - margin,
                                        selector.mapToScene(QPointF(0, 0)).x()))
            assert float(popup.property("x")) == pytest.approx(expected_x, abs=.5)
            click(app, window, window.findChild(QQuickItem, "qaStart"))
            assert not popup.property("visible")
            assert window.property("submissions") == 0
            assert not warnings, warnings


@pytest.mark.parametrize("dismissal", ("close", "escape", "outside"))
def test_overflow_popup_close_never_focuses_hidden_compute_button(app, tmp_path, dismissal):
    from PySide6.QtQml import QQmlExpression

    service, _library, design = fixture(tmp_path)
    source = '''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    required property var controller
    property bool allowClose: true
    property real contentTopMargin: 44
    width: 220; height: 500
    ResponsiveOperationBar {
        objectName: "qaBar"
        x: 10; y: 48; width: parent.width - 20
        ProjectModuleSelector {
            navigation: QtObject {
                property string projectName: "Name"
                property var projectModules: [{key: "film_optimization", name: "Opt"}]
                function openProjectModule(key) {}
            }
            currentModuleKey: "film_optimization"
        }
        ComputeTargetSelector {
            objectName: "qaCompute"; width: 180
            target: controller.computeTarget
        }
    }
}'''

    def qml(scope, expression):
        query = QQmlExpression(QQmlEngine.contextForObject(scope), scope, expression)
        result = query.evaluate()[0]
        assert not query.hasError(), query.error().toString()
        return result

    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller, source=source) as (window, warnings):
            QTest.qWait(50)
            bar = window.findChild(QQuickItem, "qaBar")
            selector = window.findChild(QQuickItem, "qaCompute")
            button = window.findChild(QQuickItem, "computeTargetButton")
            overflow = window.findChild(QQuickItem, "operationBarOverflowButton")
            popup = window.findChild(QObject, "computeTargetPopup")
            assert selector.x() < 0
            assert button.property("focusPolicy") == Qt.FocusPolicy.NoFocus.value
            assert qml(button, "Accessible.ignored")
            qml(bar, "triggerItem(overflowItems[0])")
            QTest.qWait(20)
            assert popup.property("visible")
            if dismissal == "close":
                qml(popup, "close()")
            elif dismissal == "escape":
                QTest.keyClick(window, Qt.Key.Key_Escape)
            else:
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                                 QPoint(1, window.height() - 1))
            QTest.qWait(20)
            app.processEvents()
            assert not popup.property("visible")
            assert not button.hasActiveFocus()
            assert overflow.hasActiveFocus()
            # Space may open the visible overflow menu, never the clipped
            # source device button that is excluded from accessibility.
            QTest.keyClick(window, Qt.Key.Key_Space)
            app.processEvents()
            assert not popup.property("visible")
            assert not button.hasActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_Escape)
            app.processEvents()
            assert not warnings, warnings
