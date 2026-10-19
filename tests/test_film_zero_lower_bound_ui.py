"""The real optimization editor accepts and preserves a zero thickness lower bound."""

from copy import deepcopy
import os
from pathlib import Path
from typing import Any, cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.core.film_optimization import optimize_film_design
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_target_sampling import _snapshot


ROOT = Path(__file__).resolve().parents[1]


def _visual_item(parent, object_name):
    if parent.objectName() == object_name:
        return parent
    for child in parent.childItems():
        found = _visual_item(child, object_name)
        if found is not None:
            return found
    return None


@pytest.mark.parametrize("finish_key", [Qt.Key.Key_Return, Qt.Key.Key_Tab, None])
@pytest.mark.parametrize("restored", [False, True])
def test_constraint_lower_zero_commits_in_real_qml_and_survives_reopen(
        tmp_path, finish_key, restored):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    layer_id = design["front_layers"][0]["id"]
    if restored:
        seed = FilmOptimizationQuickController(_snapshot(service, design))
        assert seed.setVariableBound("front", layer_id, "lower_nm", "5")
        assert seed.setVariableBound("front", layer_id, "upper_nm", "200")
        assert seed.setSolver("simulated_annealing")
        design.setdefault("workflow_settings", {})["optimization"] = (
            seed.exportOptimizationConfiguration())
        seed.shutdown(1000)
    controller = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=restored)
    if not restored:
        assert controller.setVariableBound("front", layer_id, "lower_nm", "5")
    assert len(cast(list[dict[str, Any]], controller.variables)) == 1
    engine = QQmlEngine()
    ThemeService.instance().attach_engine(engine)
    engine.addImportPath(str(ROOT / "coat_sim/ui/qml"))
    component = QQmlComponent(engine)
    component.loadUrl(QUrl.fromLocalFile(str(
        ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml")))
    window = None
    reopened = None
    try:
        assert component.status() != QQmlComponent.Status.Error, component.errors()
        window = component.createWithInitialProperties({"controller": controller})
        assert isinstance(window, QQuickWindow), component.errors()
        window.resize(1100, 760)
        window.show()
        if restored:
            controller.startLoading()
            for _ in range(100):
                if not controller.loading:
                    break
                QTest.qWait(20)
            assert not controller.loading
            assert controller.solverId == "simulated_annealing"
            assert cast(list[dict[str, Any]], controller.variables)[0]["upper_nm"] == 200.0
        QTest.qWait(250)
        app.processEvents()
        tab = window.findChild(QQuickItem, "filmOptimizationConstraintsTab")
        assert tab is not None
        point = tab.mapToScene(QPointF(tab.width() / 2, tab.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        window.contentItem().ensurePolished()
        window.grabWindow()
        app.processEvents()
        QTest.qWait(30)
        setup = window.findChild(QQuickItem, "filmOptimizationSetupContent")
        assert setup is not None
        assert setup.property("currentIndex") == 1
        variable_scroll = window.findChild(QQuickItem, "filmOptimizationVariableScroll")
        lower = _visual_item(variable_scroll, "filmOptimizationLower_" + layer_id)
        assert lower is not None and lower.isVisible(), [
            item.objectName() for item in window.findChildren(QQuickItem)
            if "filmOptimization" in item.objectName() and (
                "Lower" in item.objectName() or "Variable" in item.objectName()
                or "Constraint" in item.objectName() or "Setup" in item.objectName())]
        assert float(lower.property("numericDisplayValue")) == 5
        lower_point = lower.mapToScene(QPointF(lower.width() / 2, lower.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, lower_point)
        app.processEvents()
        assert lower.property("activeFocus")
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window, Qt.Key.Key_0)
        assert lower.property("text") == "0"
        assert lower.property("acceptableInput"), "the validator must accept zero"
        QTest.qWait(30)
        assert lower.property("text") == "0", "draft rebounded before commit"
        if restored and finish_key is None:
            controller.changed.emit()
            app.processEvents()
            current = _visual_item(variable_scroll, "filmOptimizationLower_" + layer_id)
            assert current is not None and current.property("text") == "0", (
                "draft rebounded on controller refresh", current.property("text") if current else None)
        if finish_key is None:
            targets = window.findChild(QQuickItem, "filmOptimizationTargetsTab")
            assert targets is not None
            point = targets.mapToScene(
                QPointF(targets.width() / 2, targets.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, point)
            assert setup.property("currentIndex") == 0
        else:
            QTest.keyClick(window, finish_key)
        app.processEvents()
        QTest.qWait(30)
        assert cast(list[dict[str, Any]], controller.variables)[0]["lower_nm"] == 0.0
        assert not controller._pending_edit_error
        assert controller.canStart
        if finish_key is None:
            point = tab.mapToScene(QPointF(tab.width() / 2, tab.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, point)
            app.processEvents()
        displayed_lower = _visual_item(variable_scroll, "filmOptimizationLower_" + layer_id)
        assert displayed_lower is not None
        displayed_lower.forceActiveFocus()
        app.processEvents()
        assert displayed_lower.property("text") == "0", displayed_lower.property("text")

        configuration = controller.exportOptimizationConfiguration()
        assert configuration["variables"][0]["lower_nm"] == 0.0
        stored = deepcopy(design)
        stored.setdefault("workflow_settings", {})["optimization"] = configuration
        path = tmp_path / "zero-lower-bound.cfilm"
        service.save(path, stored)
        loaded = service.load(path)
        reopened = FilmOptimizationQuickController(_snapshot(service, loaded))
        assert reopened.loadOptimizationConfiguration(
            loaded["workflow_settings"]["optimization"])
        reopened_rows = cast(list[dict[str, Any]], reopened.variables)
        assert reopened_rows[0]["lower_nm"] == 0.0
        assert reopened_rows[0]["upper_nm"] == (200.0 if restored else None)
        assert reopened.canStart
        request = reopened._build_request()
        assert request.variables[0].lower_nm == 0.0
        assert request.variables[0].upper_nm == (200.0 if restored else None)
        if not restored:
            # A small complete solve exercises the core's half-unbounded boundary.
            objective = request.objectives[0]
            from dataclasses import replace
            request = replace(request, objectives=(replace(
                objective, wavelength_min_nm=550.0, wavelength_max_nm=550.0,
                wavelength_samples=1),), max_iterations=1, max_evaluations=3)
            result = optimize_film_design(service, request)
            assert result.state == "completed"
    finally:
        if window is not None:
            cast(QQuickWindow, window).close()
            window.deleteLater()
        if reopened is not None:
            reopened.shutdown(1000)
        controller.shutdown(1000)
        engine.deleteLater()
        app.processEvents()
