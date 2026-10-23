"""Native QML pointer flow for cancel, re-preview and a copy conflict choice."""
from copy import deepcopy
import json

import pytest
from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest

from coat_sim.core.film_design import new_design
from coat_sim.core.project_store import ProjectStore
from tests.test_project_last_window_close import desktop as desktop, spin_until
from tests.test_launcher_information_global_resources import global_root as global_root
from tests.test_scoped_resource_libraries import open_library, settled

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def _qml(owner, expression):
    query = QQmlExpression(QQmlEngine.contextForObject(owner), owner, expression)
    result = query.evaluate()
    assert not query.hasError(), query.error().toString()
    return result[0] if isinstance(result, tuple) else result


def _named(root, name):
    matches = [root] if root.objectName() == name else []
    for child in root.childItems():
        matches.extend(_named(child, name))
    return matches


def _visible_rect(window, item):
    rect = item.mapRectToScene(QRectF(0, 0, item.width(), item.height()))
    parent = item.parentItem()
    while parent is not None:
        if parent.clip():
            rect = rect.intersected(parent.mapRectToScene(QRectF(0, 0, parent.width(), parent.height())))
        parent = parent.parentItem()
    return rect.intersected(QRectF(0, 0, window.width(), window.height()))


def _click(window, item):
    assert isinstance(item, QQuickItem) and item.isEnabled() and item.isVisible()
    rect = _visible_rect(window, item)
    assert not rect.isEmpty(), "Target has no visible intersection with the window/clip ancestors"
    position = rect.center()
    assert item.contains(item.mapFromScene(position))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position.toPoint())
    QTest.qWait(25)
    return {"point": [position.x(), position.y()],
            "visible_rect": [rect.x(), rect.y(), rect.width(), rect.height()]}


def test_cancel_repreview_conflict_popup_and_copy_are_pointer_reachable(
        desktop, tmp_path, global_root, record_testsuite_property):
    project = desktop.open("Target")
    target = desktop.adapter.host.store_for(project.session.binding)
    existing = target.save_film_document(new_design("Existing", design_id="same-film"))
    donor = ProjectStore.create(tmp_path / "Donor", "Donor")
    selected = donor.save_film_document(new_design("Incoming", design_id="same-film"))
    before_donor = deepcopy(donor.manifest)
    _route_id, _route, view = open_library(desktop, project)
    window, bridge = view[:2]
    window.resize(800, 760)
    assert bridge.browseSource(str(donor.project_file))
    settled(bridge)
    window.setProperty("categoryKey", "film")
    for round_index in range(2):
        assert bridge.previewLibraryTransfer([selected], "project")
        settled(bridge)
        dialog = window.findChild(QObject, "globalResourcesPreviewDialog")
        assert dialog is not None and dialog.property("visible")
        if round_index == 0:
            _click(window, window.findChild(QQuickItem, "globalResourcesPreviewCancelButton"))
            assert not dialog.property("visible") and bridge.transferPlan == {}
            assert len(target.manifest["documents"]) == 1
            continue
        named = _named(window.contentItem(), "globalResourceConflict_0")
        candidates = [item for item in named if item.isVisible()
                      and not _visible_rect(window, item).isEmpty()
                      and _qml(item, "String(parent.modelData.id)") == selected]
        assert len(candidates) == 1, {"all_named": len(named), "current_visible": len(candidates)}
        picker = candidates[0]
        assert _qml(picker, "popup.visible") is False
        pointer_evidence = {**_click(window, picker), "all_named": len(named),
                            "current_visible": len(candidates), "row_id": selected}
        assert _qml(picker, "popup.visible") is True, {
            "picker_enabled": picker.isEnabled(), "picker_visible": picker.isVisible(),
            "rect": [picker.x(), picker.y(), picker.width(), picker.height()],
            "busy": bridge.transferBusy, "diagnostics": bridge.transferDiagnostics,
        }
        QTest.keyClick(window, Qt.Key.Key_Home)
        QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Return)
        QTest.qWait(25)
        assert _qml(picker, "popup.visible") is False
        decisions = window.property("decisions")
        decisions = decisions.toVariant() if hasattr(decisions, "toVariant") else decisions
        assert decisions[selected] == "copy"
        _click(window, window.findChild(QQuickItem, "globalResourcesApplyButton"))
        settled(bridge)
        assert not dialog.property("visible"), {"status": bridge.transferStatus,
            "message": bridge.libraryMessage, "diagnostics": bridge.transferDiagnostics,
            "plan": bridge.transferPlan}
    assert len(target.manifest["documents"]) == 2
    assert target.resolve_document(existing)["documents"][0]["data"]["name"] == "Existing"
    names = [target._read_record(row)["name"] for row in target.manifest["documents"]]
    assert set(names) == {"Existing", "Incoming (copy)"}
    assert donor.manifest == before_donor
    spin_until(lambda: bridge.transferBusy, lambda busy: not busy, "copy delivery")
    record_testsuite_property("resource_library_copy_pointer", json.dumps(pointer_evidence, sort_keys=True))
