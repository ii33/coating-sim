"""The shared test lookup follows visual ownership and keeps strict failures."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
import shiboken6
from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow

from tests.qt_visual_lookup import find_visual_item
from tests.test_compute_server_category_ui import _find

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def visual_window(tmp_path) -> Iterator[QQuickWindow]:
    engine = QQmlEngine()
    component = QQmlComponent(engine)
    component.setData(b"""import QtQuick
Window {
    objectName: "window-is-not-an-item"
    property alias lookupScope: lookupScope
    property alias firstMatch: firstMatch
    property alias lastMatch: lastMatch
    QtObject { objectName: "nonvisual" }
    Item {
        id: lookupScope
        objectName: "scope"
        Item { id: firstMatch; objectName: "duplicate" }
        Item { id: lastMatch; objectName: "duplicate" }
    }
    Item { objectName: "outside" }
}""", QUrl.fromLocalFile(str(tmp_path / "Lookup.qml")))
    root = component.create()
    assert isinstance(root, QQuickWindow), component.errorString()
    try:
        yield root
    finally:
        shiboken6.delete(root)
        shiboken6.delete(component)
        shiboken6.delete(engine)


def test_visual_only_child_and_quoted_name_never_enumerate_python_children(visual_window):
    class VisualBranch(QQuickItem):
        def childItems(self):
            raise AssertionError("Visual enumeration must stay in the QML engine")

    branch = VisualBranch()
    target = QQuickItem()
    try:
        branch.setParentItem(visual_window.contentItem())
        target.setParentItem(branch)
        target.setObjectName('visual_"quoted"\\target')
        assert target.parent() is None
        assert visual_window.findChildren(QObject, target.objectName()) == []
        assert find_visual_item(visual_window, target.objectName()) is target
        assert _find(visual_window, target.objectName()) is target
    finally:
        shiboken6.delete(target)
        shiboken6.delete(branch)


def test_item_scope_and_root_match_do_not_search_other_visual_branches(visual_window):
    scope = visual_window.property("lookupScope")
    assert isinstance(scope, QQuickItem)
    assert find_visual_item(scope, "scope") is scope
    assert find_visual_item(visual_window, "outside") is not None
    assert find_visual_item(scope, "outside") is None
    assert find_visual_item(visual_window, "window-is-not-an-item") is None


def test_dfs_order_preserves_first_and_last_child_search_modes(visual_window):
    first = visual_window.property("firstMatch")
    last = visual_window.property("lastMatch")
    assert isinstance(first, QQuickItem) and isinstance(last, QQuickItem)
    assert first is not last
    assert find_visual_item(visual_window, "duplicate") is first
    assert find_visual_item(visual_window, "duplicate", reverse_children=True) is last
    assert _find(visual_window, "duplicate") is last


@pytest.mark.parametrize("name", ["absent", "nonvisual"])
def test_missing_and_nonvisual_names_keep_none_and_strict_wrapper_failure(visual_window, name):
    assert find_visual_item(visual_window, name) is None
    with pytest.raises(AssertionError, match="QML item not found: " + name):
        _find(visual_window, name)
