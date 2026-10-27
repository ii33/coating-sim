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
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest
from coat_sim.ui.theme_service import ThemeService

from tests.film_structure_fixtures import fixture as film_fixture
from tests.test_film_optimization_restore_layout import (
    click, click_operation_bar_action, optimizer, window_runtime,
)

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
            click_operation_bar_action(app, window, "filmSensitivityOpenButton")
            yield owner, controller, window, warnings


def _visual(root: QQuickItem, name: str):
    if root.objectName() == name:
        return root
    for child in root.childItems():
        found = _visual(child, name)
        if found is not None:
            return found
    return None


def _reveal_click(app, window, item):
    assert item is not None
    context = QQmlEngine.contextForObject(item)
    assert context is not None
    expression = QQmlExpression(context, item, """(function () {
        let ancestor = parent;
        while (ancestor !== null) {
            if ("contentY" in ancestor) {
                const point = mapToItem(ancestor.contentItem, 0, 0);
                ancestor.contentY = Math.min(Math.max(0, ancestor.contentHeight - ancestor.height),
                                            Math.max(0, point.y - 15));
            }
            ancestor = ancestor.parent;
        }
    })()""")
    expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    QTest.qWait(15)
    click(app, window, item)


def _enter_table_text(app, window, name, keys):
    field = _visual(window.contentItem(), name)
    _reveal_click(app, window, field)
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for key in keys:
        QTest.keyClick(window, key)
        app.processEvents()
        assert _visual(window.contentItem(), name) is field
        assert field.property("activeFocus")


def _choose_table_option(app, window, name, index):
    combo = _visual(window.contentItem(), name)
    _reveal_click(app, window, combo)
    QTest.keyClick(window, Qt.Key.Key_Home)
    for _ in range(index):
        QTest.keyClick(window, Qt.Key.Key_Down)
    QTest.keyClick(window, Qt.Key.Key_Return)
    app.processEvents()


@pytest.mark.parametrize("mode,width,height", [("light", 1280, 760), ("dark", 560, 500)])
def test_real_batch_fields_exceptions_fold_reset_and_request_mapping(
        sensitivity_window, app, tmp_path, mode, width, height):
    owner, panel, window, warnings = sensitivity_window
    window.resize(width, height)
    ThemeService.instance().configure(mode=mode)
    _reveal_click(app, window, _visual(window.contentItem(), "filmSensitivityTableTab"))
    assert all(row["enabled"] and not row["override"] for row in panel.thicknessErrorRows)
    _enter_table_text(app, window, "filmThicknessCommonBias",
                      (Qt.Key.Key_Minus, Qt.Key.Key_2, Qt.Key.Key_Period, Qt.Key.Key_5))
    _choose_table_option(app, window, "filmThicknessCommonBiasUnit", 2)
    _choose_table_option(app, window, "filmThicknessCommonUncertaintyKind", 1)
    _enter_table_text(app, window, "filmThicknessCommonUncertainty", (Qt.Key.Key_1,))
    _choose_table_option(app, window, "filmThicknessCommonUncertaintyUnit", 1)
    base = owner._build_request(require_variables=False, fixed_topology=False)
    request = panel._build_thickness_error_request(base)
    assert all(row.enabled and row.bias == -.025 and row.bias_unit == "relative_fraction" for row in request.rows)
    assert all(row.uncertainty_kind == "bounded" and row.uncertainty_magnitude == 1. for row in request.rows)
    scroll = _visual(window.contentItem(), "filmThicknessErrorTableScroll")
    context = QQmlEngine.contextForObject(scroll)
    assert context is not None
    top = QQmlExpression(context, scroll, "contentItem.contentY = 0")
    top.evaluate()
    assert not top.hasError(), top.error().toString()
    QTest.qWait(25)
    image = window.grabWindow()
    if not image.isNull():
        assert image.save(str(tmp_path / ("sensitivity-batch-" + mode + ".png")))

    section = _visual(window.contentItem(), "filmThicknessLayerOverridesSection")
    assert section is not None and not section.property("expanded")
    header = section.findChild(QQuickItem, "themeSectionHeader")
    _reveal_click(app, window, header)
    first, second = (row["layer_id"] for row in panel.thicknessErrorRows)
    _reveal_click(app, window, _visual(window.contentItem(), "filmThicknessOverride_" + first))
    _enter_table_text(app, window, "filmThicknessBias_" + first, (Qt.Key.Key_0,))
    _reveal_click(app, window, _visual(window.contentItem(), "filmThicknessErrorEnabled_" + second))
    _reveal_click(app, window, header)
    assert not section.property("expanded")
    _enter_table_text(app, window, "filmThicknessCommonBias", (Qt.Key.Key_2,))
    assert panel.thicknessErrorRows[0]["bias_text"] == "0"
    assert panel.thicknessErrorRows[0]["override"]
    assert not panel.thicknessErrorRows[1]["enabled"]
    _reveal_click(app, window, header)
    _reveal_click(app, window, _visual(window.contentItem(), "filmThicknessOverride_" + first))
    assert not panel.thicknessErrorRows[0]["override"]
    assert panel.thicknessErrorRows[0]["bias_text"] == "2"
    assert not panel.thicknessErrorRows[1]["enabled"]
    assert not warnings, warnings


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
    click_operation_bar_action(app, window, "filmSensitivityOpenButton")
    assert dialog.property("visible")
    assert tabs.property("currentIndex") == 1
    assert controller.activeWorkflow == "ideal"
    assert controller.thicknessErrorRows[0]["bias_text"] == "-1.25"
    assert controller.idealDegradationObjectives[0]["budget_text"] == "2"
    assert controller.idealSensitivityMode == ""
    assert not warnings, warnings


@pytest.mark.parametrize("width,overflowed", ((720, True), (1600, False)))
def test_sensitivity_entry_uses_real_visible_action_at_narrow_and_wide_widths(
        sensitivity_window, app, width, overflowed):
    _owner, controller, window, warnings = sensitivity_window
    dialog = window.findChild(QObject, "filmSensitivityDialog")
    assert dialog is not None and dialog.property("visible")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    app.processEvents()
    assert not dialog.property("visible")
    window.resize(width, 500)
    QTest.qWait(30)
    app.processEvents()
    source = window.findChild(QQuickItem, "filmSensitivityOpenButton")
    assert source is not None and (source.x() < 0) is overflowed
    click_operation_bar_action(app, window, "filmSensitivityOpenButton")
    assert dialog.property("visible")
    assert controller.activeWorkflow == "ideal"
    assert not warnings, warnings
