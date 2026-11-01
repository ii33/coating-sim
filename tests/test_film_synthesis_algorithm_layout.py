"""Actual optimizer geometry when switching to or restoring synthesis."""
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPointF
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt
pytest_plugins = ("tests.test_film_optimization_cancel_apply_review",)


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.mark.parametrize("startup", [False, True], ids=["switch", "restored"])
@pytest.mark.parametrize("size,scale", [((1400, 900), 1.0), ((1400, 900), 0.75), ((760, 520), 1.0), ((1100, 760), 1.5)])
def test_synthesis_selector_and_scroll_geometry(app, tmp_path, startup, size, scale):
    service, _library, design = fixture(tmp_path)
    configuration = None
    if startup:
        with optimizer(service, design) as (controller, _source, _session):
            assert controller.setSearchMode("synthesis")
            # Match a new design without candidate materials as in the report.
            for row in list(controller.synthesisMaterialRows):
                assert controller.removeSynthesisMaterialRow(row["id"])
            configuration = controller.exportOptimizationConfiguration()
    with optimizer(service, design, configuration) as (controller, _source, _session):
        with window_runtime(app, controller) as (window, warnings):
            window.resize(*size)
            ThemeService.instance().configure(font_scale=scale)
            tabs = window.findChild(QObject, "filmOptimizationSetupTabs")
            tabs.setProperty("currentIndex", 2)
            if not startup:
                assert controller.searchMode == "fixed_topology"
                assert controller.setSearchMode("synthesis")
                for row in list(controller.synthesisMaterialRows):
                    assert controller.removeSynthesisMaterialRow(row["id"])
            QTest.qWait(150)
            content = window.findChild(QQuickItem, "filmOptimizationSolverOptionsContent")
            selector = window.findChild(QQuickItem, "filmOptimizationSearchModeBox")
            group = window.findChild(QQuickItem, "filmSynthesisOptions")
            scroll = window.findChild(QQuickItem, "filmOptimizationSolverOptionsScroll")
            hidden = window.findChild(QQuickItem, "filmIdealSensitivityAlgorithmOptions")
            last = window.findChild(QQuickItem, "filmSynthesisCpuWorkersField")
            assert all(item is not None for item in (content, selector, group, scroll, hidden, last))
            assert selector.isVisible() and group.isVisible() and not hidden.isVisible()
            assert hidden.implicitHeight() > 0  # Hidden content must not reserve its height.
            assert group.height() > 0
            gap = group.y() - (selector.y() + selector.height())
            assert 0 <= gap <= 24, gap
            first = group.childItems()[0]
            assert first.isVisible() and first.y() <= 1
            assert content.implicitHeight() >= group.y() + group.height() - 1
            flick = scroll.property("contentItem")
            assert flick.property("contentHeight") > flick.height()
            flick.setProperty("contentY", flick.property("contentHeight") - flick.height())
            QTest.qWait(100)
            last_pos = last.mapToItem(scroll, QPointF(0, 0))
            assert last_pos.y() >= -1
            assert last_pos.y() + last.height() <= scroll.height() + 1
            assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_cancel_then_synthesis_from_hidden_tab_real_combo(review, app):
    """Controlled no-candidate terminal; real controller, window, and input events."""
    from PySide6.QtCore import Qt
    from tests.test_film_optimization_cancel_apply_review import arm, deliver

    controller = review.controller
    with window_runtime(app, controller) as (window, warnings):
        window.resize(1400, 900)
        tabs = window.findChild(QObject, "filmOptimizationSetupTabs")
        tabs.setProperty("currentIndex", 2)
        QTest.qWait(100)
        arm(review)
        controller.changed.emit()
        controller.cancelOptimization()
        deliver(review, None, error={"state": "cancelled"})
        QTest.qWait(100)
        assert not controller.busy
        # Switch mode while algorithm pane is hidden, then use the actual combo
        # on that pane to return fixed → synthesis through keyboard interaction.
        tabs.setProperty("currentIndex", 0)
        assert controller.setSearchMode("synthesis")
        QTest.qWait(100)
        tabs.setProperty("currentIndex", 2)
        QTest.qWait(100)
        hidden_mode_selector = window.findChild(QQuickItem, "filmOptimizationSearchModeBox")
        hidden_mode_group = window.findChild(QQuickItem, "filmSynthesisOptions")
        hidden_gap = hidden_mode_group.y() - (hidden_mode_selector.y() + hidden_mode_selector.height())
        assert hidden_mode_group.isVisible() and 0 <= hidden_gap <= 24, hidden_gap
        assert controller.setSearchMode("fixed_topology")
        QTest.qWait(100)
        selector = window.findChild(QQuickItem, "filmOptimizationSearchModeBox")
        group = window.findChild(QQuickItem, "filmSynthesisOptions")
        center = selector.mapToScene(QPointF(selector.width() / 2, selector.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=center)
        QTest.qWait(50)
        QTest.keyClick(window, Qt.Key.Key_End)
        QTest.keyClick(window, Qt.Key.Key_Up)
        QTest.keyClick(window, Qt.Key.Key_Return)
        QTest.qWait(150)
        assert controller.searchMode == "synthesis"
        assert group.isVisible()
        gap = group.y() - (selector.y() + selector.height())
        assert 0 <= gap <= 24, gap
        assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


@pytest.mark.parametrize("destination", ["synthesis", "fixed_topology", "ideal_sensitivity", "reduction", "automatic_repair", "structure"])
@pytest.mark.parametrize("hidden_switch", [False, True], ids=["visible-tab", "hidden-tab"])
def test_loaded_resource_panel_does_not_reserve_space_after_switch(review, app, destination, hidden_switch):
    """Load and unload a real resource estimator repeatedly before changing mode."""
    controller = review.controller
    with window_runtime(app, controller) as (window, warnings):
        window.resize(1400, 900)
        tabs = window.findChild(QObject, "filmOptimizationSetupTabs")
        tabs.setProperty("currentIndex", 2)
        assert controller.resourceEstimateController is not None
        loader = window.findChild(QQuickItem, "filmResourceAssessmentAlgorithmOptions")
        selector = window.findChild(QQuickItem, "filmOptimizationSearchModeBox")
        content = window.findChild(QQuickItem, "filmOptimizationSolverOptionsContent")
        for iteration in range(2):
            assert controller.setSearchMode("resource_assessment")
            QTest.qWait(150)
            assert loader.property("active") and loader.property("item") is not None
            assert loader.isVisible() and loader.height() > 100
            if hidden_switch:
                tabs.setProperty("currentIndex", 0)
            assert controller.setSearchMode(destination)
            QTest.qWait(100)
            if hidden_switch:
                tabs.setProperty("currentIndex", 2)
            QTest.qWait(150)
            assert not loader.property("active") and loader.property("item") is None
            assert not loader.isVisible()
            selector_end = selector.y() + selector.height()
            next_items = [item for item in content.childItems()
                          if item.isVisible() and item.height() > 0 and item.y() >= selector_end]
            assert next_items
            gap = min(item.y() for item in next_items) - selector_end
            assert 0 <= gap <= 24, gap
            qa_dir = os.environ.get("COATING_LAYOUT_QA_DIR")
            if qa_dir and destination == "synthesis" and not hidden_switch and iteration == 0:
                assert window.grabWindow().save(str(Path(qa_dir) / "resource-switch-after-fix.png"))
        assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings
