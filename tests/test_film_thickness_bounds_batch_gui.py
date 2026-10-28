"""Real constraints-page interaction over the atomic bound editor."""
from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from coat_sim.ui.theme_service import ThemeService
from tests.test_film_manufacturing_quick_controller import _readable_offscreen_font
from tests.test_film_synthesis_gui_review import _Scene
from tests.test_film_thickness_bounds_batch import app as app, case as case

pytestmark = pytest.mark.qt


def test_real_qml_batch_is_explicit_atomic_and_still_allows_single_layer_edits(case):
    c, _source, session = case
    scene = _Scene(c, "FilmOptimizationWindow.qml")
    try:
        scene.click(scene.item("filmOptimizationConstraintsTab"))
        scope = scene.item("filmOptimizationBatchBoundsScope")
        assert scope.property("currentIndex") == 0
        lower, upper = scene.item("filmOptimizationBatchLower"), scene.item("filmOptimizationBatchUpper")
        apply = scene.item("filmOptimizationBatchBoundsApply")
        assert not apply.isEnabled()
        scene.edit(lower, "0")
        scene.edit(upper, "40")
        assert not session.configuration_writes
        scene.click(apply)
        assert len(session.configuration_writes) == 1
        assert [(r["lower_nm"], r["upper_nm"]) for r in c.variables] == [(0., 40.)] * 3
        assert [r["enabled"] for r in c.variables] == [True, False, True]
        scene.click(scope)
        QTest.keyClick(scene.window, Qt.Key.Key_End)
        QTest.keyClick(scene.window, Qt.Key.Key_Return)
        assert scope.property("currentIndex") == 1
        scene.edit(lower, "2")
        scene.edit(upper, "")
        scene.click(apply)
        assert [(r["lower_nm"], r["upper_nm"]) for r in c.variables] == [(2., 40.), (0., 40.), (2., 40.)]
        before = deepcopy(c.variables)
        scene.edit(upper, "1")
        scene.click(apply)
        assert c.variables == before and len(session.configuration_writes) == 2
        assert upper.property("text") == "1"
        assert "未修改任何层" in scene.item("filmOptimizationBatchBoundsMessage").property("text")
        scene.edit(upper, "60")
        scene.click(apply)
        assert not c.error and len(session.configuration_writes) == 3
        scene.edit(scene.item("filmOptimizationUpper_batch-layer-0"), "70")
        assert c.variables[0]["upper_nm"] == 70.
        assert c.variables[1]["upper_nm"] == 40. and c.variables[2]["upper_nm"] == 60.
        assert [r["enabled"] for r in c.variables] == [True, False, True]
        c._busy = True
        c.changed.emit()
        QTest.qWait(10)
        assert not lower.isEnabled() and not apply.isEnabled()
        c._busy = False
        c.changed.emit()
    finally:
        scene.close()


@pytest.mark.parametrize("mode,width,font_scale", [("light", 1100, 1.0), ("dark", 760, 1.0), ("light", 760, 1.4)])
def test_batch_controls_fit_actual_constraints_page(case, tmp_path, mode, width, font_scale):
    c, _source, _session = case
    theme = ThemeService.instance()
    previous = (theme._mode, theme._font_scale, theme._density_scale)
    scene = None
    try:
        with _readable_offscreen_font():
            theme.configure(mode=mode, font_scale=font_scale, density_scale=1.0)
            scene = _Scene(c, "FilmOptimizationWindow.qml")
            scene.window.setWidth(width)
            scene.window.setHeight(860)
            scene.click(scene.item("filmOptimizationConstraintsTab"))
            panel = scene.item("filmOptimizationBatchBoundsPanel")
            assert panel.width() <= scene.evaluate(panel, "parent.width") + .5
            for name in ("Scope", "Lower", "Upper", "Apply"):
                object_name = {"Scope": "filmOptimizationBatchBoundsScope", "Lower": "filmOptimizationBatchLower",
                               "Upper": "filmOptimizationBatchUpper", "Apply": "filmOptimizationBatchBoundsApply"}[name]
                item = scene.item(object_name)
                assert item.isVisible() and item.width() >= 70.
                right = scene.evaluate(item, "mapToItem(null,width,0).x")
                bottom = scene.evaluate(item, "mapToItem(null,0,height).y")
                assert right <= scene.window.width() - 10 and bottom <= scene.window.height() - 20
                assert scene.evaluate(item, "mapToItem(parent.parent,width,0).x") <= panel.width() + .5
                assert scene.evaluate(item, """(function () {
                    let ancestor = parent;
                    while (ancestor && ancestor.objectName !== "filmOptimizationSetupContent") ancestor = ancestor.parent;
                    return ancestor && mapToItem(ancestor, width, 0).x <= ancestor.width + .5;
                })()""")
            image = scene.window.grabWindow()
            assert not image.isNull()
            filename = f"batch-bounds-{mode}-{width}-{font_scale}.png"
            assert image.save(str(tmp_path / filename))
            evidence = Path(__file__).resolve().parents[2] / "batch-bounds-evidence"
            evidence.mkdir(exist_ok=True)
            assert image.save(str(evidence / filename))
    finally:
        if scene is not None:
            scene.close()
        theme.configure(mode=previous[0], font_scale=previous[1], density_scale=previous[2])
