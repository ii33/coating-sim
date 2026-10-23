"""Real Qt Quick checks for the two current sensitivity entry pages."""
from __future__ import annotations

from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from tests.film_structure_fixtures import fixture as film_fixture
from tests.test_film_optimization_restore_layout import click, optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def sensitivity_window(app, tmp_path):
    service, _library, design = film_fixture(tmp_path)
    design["front_layers"].append({
        **deepcopy(design["front_layers"][0]), "id": "current-qml-layer-2",
    })
    with optimizer(service, design) as (owner, _source, _session):
        assert owner.setObjectiveField("wavelength_min_nm", 550.0)
        assert owner.setObjectiveField("wavelength_max_nm", 550.0)
        controller = owner.sensitivityController
        assert controller.prepare(), controller.error
        with window_runtime(app, owner) as (window, warnings):
            open_button = window.findChild(QQuickItem, "filmSensitivityOpenButton")
            assert open_button is not None
            click(app, window, open_button)
            yield owner, controller, window, warnings


def _visual(root: QQuickItem, name: str):
    if root.objectName() == name:
        return root
    for child in root.childItems():
        found = _visual(child, name)
        if found is not None:
            return found
    return None


def test_real_qml_exposes_exactly_two_current_pages_and_default_ideal(sensitivity_window):
    _owner, controller, window, warnings = sensitivity_window
    tabs = window.findChild(QObject, "filmSensitivityWorkflowTabs")
    pages = window.findChild(QQuickItem, "filmSensitivityWorkflowPages")
    table_tab = _visual(window.contentItem(), "filmSensitivityTableTab")
    ideal_tab = _visual(window.contentItem(), "filmSensitivityIdealTab")

    assert tabs is not None and tabs.property("count") == 2
    assert pages is not None and len(pages.childItems()) == 2
    assert tabs.property("currentIndex") == 1
    assert controller.activeWorkflow == "ideal"
    assert table_tab is not None and table_tab.property("text") == "参数表预期误差分析"
    assert ideal_tab is not None and ideal_tab.property("text") == "理想敏感度优化"
    assert table_tab.isVisible() and ideal_tab.isVisible()
    assert _visual(window.contentItem(), "filmIdealSensitivityScroll").isVisible()
    assert _visual(window.contentItem(), "filmThicknessErrorTableScroll") is not None

    qml_names = {obj.objectName() for obj in window.findChildren(QObject) if obj.objectName()}
    assert "filmSensitivityLegacyTab" not in qml_names
    assert "filmSensitivityAnalyzeButton" not in qml_names
    assert "filmSensitivityConfigurationScroll" not in qml_names
    assert not warnings, warnings


def test_blank_scale_click_is_rejected_and_current_drafts_survive_switch_reopen(
        sensitivity_window, app):
    _owner, controller, window, warnings = sensitivity_window
    layer_id = controller.thicknessErrorRows[0]["layer_id"]
    objective_id = controller.idealDegradationObjectives[0]["objective_id"]
    assert controller.setTableDraft(layer_id, "bias", "-1.25")
    assert controller.setTableUnit(layer_id, "bias", "relative_percent")
    assert controller.idealSensitivityMode == ""

    optimize = window.findChild(QQuickItem, "filmIdealSensitivityOptimizeButton")
    assert optimize is not None and optimize.isEnabled()
    click(app, window, optimize)
    assert "请选择绝对或相对" in controller.idealError
    assert controller.idealSensitivityMode == ""
    assert not controller.busy and not controller.idealHasResult

    tabs = window.findChild(QObject, "filmSensitivityWorkflowTabs")
    table_tab = _visual(window.contentItem(), "filmSensitivityTableTab")
    ideal_tab = _visual(window.contentItem(), "filmSensitivityIdealTab")
    click(app, window, table_tab)
    assert controller.activeWorkflow == "table"
    bias = _visual(window.contentItem(), "filmThicknessBias_" + layer_id)
    assert bias is not None and bias.property("text") == "-1.25"
    click(app, window, ideal_tab)
    assert controller.activeWorkflow == "ideal"
    budget = _visual(window.contentItem(), "filmIdealDegradationBudget_" + objective_id)
    assert budget is not None and budget.property("text") == ""
    budget.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_2)
    app.processEvents()
    assert controller.idealDegradationObjectives[0]["budget_text"] == "2"

    dialog = window.findChild(QObject, "filmSensitivityDialog")
    open_button = window.findChild(QQuickItem, "filmSensitivityOpenButton")
    assert dialog is not None and open_button is not None
    dialog.setProperty("visible", False)
    app.processEvents()
    assert not dialog.property("visible")
    click(app, window, open_button)
    assert dialog.property("visible")
    assert tabs.property("currentIndex") == 1
    assert controller.activeWorkflow == "ideal"
    assert controller.thicknessErrorRows[0]["bias_text"] == "-1.25"
    assert controller.idealDegradationObjectives[0]["budget_text"] == "2"
    assert controller.idealSensitivityMode == ""
    assert not warnings, warnings
