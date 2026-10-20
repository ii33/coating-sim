"""Native QML cancel-button smoke over real source sessions and one-shot apply."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QPointF, Qt
from PySide6.QtTest import QTest

from tests.test_film_optimization_cancel_apply_review import (
    arm, cancelled_result, deliver,
)
from tests.test_film_synthesis_gui_review import _Scene, _wait

pytestmark = [pytest.mark.qt, pytest.mark.contract]
pytest_plugins = ("tests.test_film_optimization_cancel_apply_review",)


@pytest.mark.parametrize("conflict", [False, True], ids=["apply", "source-conflict"])
def test_visible_cancel_button_applies_once_or_preserves_conflicting_source(review, conflict):
    state = review
    scene = _Scene(state.controller, "FilmOptimizationWindow.qml")
    try:
        arm(state)
        result = cancelled_result(state)
        state.controller.changed.emit()
        button = scene.item("filmOptimizationCancelButton")
        _wait(lambda: bool(button.property("enabled")))
        scene.click(button)
        assert state.controller._cancel_event.is_set()
        if conflict:
            state.source._pending_edit_epoch += 1
        deliver(state, result)
        QCoreApplication.processEvents()
        QTest.qWait(80)
        after = state.source.session.snapshot()
        visible_status = str(scene.window.property("statusText"))
        if conflict:
            assert after.design == state.base.design
            assert after.design_revision == state.base.design_revision
            assert state.controller.hasCandidate
            assert state.controller.property("error") or state.controller.property("candidateStale")
            assert not state.refreshes
            assert "未自动应用" in visible_status
        else:
            assert after.design == result.best_design
            assert after.design_revision == state.base.design_revision + 1
            assert len(state.refreshes) == 1
            assert state.source.canUndoOptimizedDesign
            assert "已应用" in visible_status and "未保存" in visible_status
            assert "刷新光谱" in visible_status
            button = scene.item("filmOptimizationCancelButton")
            assert not button.isEnabled()
            point = button.mapToScene(QPointF(button.width()/2, button.height()/2)).toPoint()
            QTest.mouseClick(scene.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
            deliver(state, result)
            assert state.source.session.snapshot().design_revision == after.design_revision
            assert len(state.refreshes) == 1
        assert not state.saves
        capture = os.environ.get("COAT_CANCEL_NATIVE_CAPTURE_DIR")
        if capture:
            folder = Path(capture)
            folder.mkdir(parents=True, exist_ok=True)
            assert scene.window.grabWindow().save(str(folder / ("conflict.png" if conflict else "applied.png")))
        if not conflict:
            assert state.source.undoOptimizedDesign()
            assert state.source.session.snapshot().design == state.base.design
    finally:
        scene.close()
