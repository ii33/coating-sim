"""A single trailing overflow and truthful current-resource picker labels."""
from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest

from coat_sim.core.project_film_session import ProjectFilmWorkspace

from tests.test_project_resource_adapter import project as project
from tests.test_responsive_operation_bar import click, events, item, qml

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def assert_tail(window):
    controls = item(window, "projectResourceControls")
    bar = controls.parentItem()
    more = item(window, "projectResourceMoreActionsButton")
    assert more.parentItem() is bar and more.isVisible()
    assert more.x() >= 0 and more.x() + more.width() <= bar.width() + 1
    for child in bar.childItems():
        if child is more or not child.isVisible() or child.width() <= 0 or child.x() < 0:
            continue
        assert child.x() + child.width() <= more.x() + 1, (child.objectName(), child.x(), child.width(), more.x())
    assert not item(window, "operationBarOverflowButton").isVisible()
    assert qml(bar, "collectMetrics().every(entry => !entry.item.operationBarExternalAnchor)")
    return bar, more


def test_process_overflow_is_last_and_menu_has_no_hidden_item_height(project):
    _app, stores, _host, _film, _resources, view = project
    window, controller = view("process")
    before = deepcopy(stores["First"].manifest)
    window.resize(1280, 800)
    events()
    _bar, more = assert_tail(window)
    assert not item(window, "processResourceDisplayName").isVisible()
    picker = item(window, "projectManagedDocumentPicker")
    assert picker.property("currentIndex") == -1
    assert picker.property("displayText") == controller.resourceInformation["name"]
    click(window, more)
    menu = window.findChild(QObject, "projectResourceActionsMenu")
    assert menu is not None and menu.property("visible")
    visible_rows = qml(menu, "Array.from({length: count}, (_, i) => itemAt(i)).filter(item => item.visible)")
    assert len(visible_rows) == 1
    entry = item(window, "projectResourceSaveConfigurationAction")
    assert entry.isVisible() and entry.property("text") == "保存沉积配置…"
    assert qml(menu, "Array.from({length: count}, (_, i) => itemAt(i)).every(item => item.visible || item.height === 0)")
    assert qml(menu, "contentHeight <= itemAt(5).height + topPadding + bottomPadding + 2")
    assert not any("选择默认" in str(row.property("text")) for row in visible_rows)
    assert window.grabWindow().save("/tmp/process-overflow-menu-tail.png")
    QTest.keyClick(window, Qt.Key.Key_Down)
    events()
    selected = qml(menu, "itemAt(currentIndex).objectName")
    assert selected == "projectResourceSaveConfigurationAction"
    QTest.keyClick(window, Qt.Key.Key_Return)
    events()
    dialog = window.findChild(QObject, "projectDefaultConfigurationDialog")
    assert dialog is not None and dialog.property("visible")
    QTest.keyClick(window, Qt.Key.Key_Escape)
    events()
    assert stores["First"].manifest == before
    assert window.grabWindow().save("/tmp/process-overflow-tail-wide.png")


@pytest.mark.parametrize("width", [640, 900, 1280])
def test_tail_mouse_keyboard_font_growth_and_width_roundtrip(project, width):
    _app, _stores, _host, _film, _resources, view = project
    window, _controller = view("process")
    for scale in (1, 1.8, 1):
        copy = item(window, "projectResourceCreateCopyButton")
        qml(copy, f"font.pointSize = Theme.snapshot.fontSizeBody * {scale}")
        window.resize(width, max(720, window.minimumHeight()))
        events()
        bar, more = assert_tail(window)
        assert not bar.property("layoutPending")
        more.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Space)
        events()
        menu = window.findChild(QObject, "projectResourceActionsMenu")
        assert menu.property("visible")
        if width == 640 and scale == 1:
            assert window.grabWindow().save("/tmp/process-overflow-tail-small-menu.png")
        if width == 1280 and scale == 1.8:
            assert window.grabWindow().save("/tmp/process-overflow-tail-large-font-menu.png")
        assert qml(menu, "Array.from({length: count}, (_, i) => itemAt(i)).every(item => item.visible || item.height === 0)")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        events()
        assert not menu.property("visible")
        assert more.hasActiveFocus()
        if width == 640 and scale == 1:
            assert window.grabWindow().save("/tmp/process-overflow-tail-small.png")
        click(window, more)
        assert menu.property("visible")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        events()
    window.resize(1280, 800)
    events()
    assert item(window, "projectResourceCreateCopyButton").isVisible()
    if width == 640:
        assert window.grabWindow().save("/tmp/process-overflow-tail-restored.png")


@pytest.mark.parametrize("key", ["source", "substrate", "machine", "mask", "process", "materials"])
def test_clean_unsaved_resource_name_is_in_picker_without_a_fake_saved_row(project, key):
    _app, _stores, _host, _film, _resources, view = project
    window, controller = view(key)
    picker = item(window, "projectManagedDocumentPicker")
    assert not controller.currentDocumentId and picker.property("currentIndex") == -1
    expected = controller.resourceInformation["name"]
    assert picker.property("displayText") == (expected or "未命名资源")
    assert not controller.dirty
    assert not str(picker.property("currentValue") or "")
    if key != "materials":
        assert not item(window, key + "ResourceDisplayName").isVisible()


def test_picker_name_follows_save_copy_information_rename_open_and_project_identity(project):
    _app, stores, host, _film, _resources, view = project
    first, controller = view("source")
    second_session = host.open_project(stores["Second"].root)
    second_owner = ProjectFilmWorkspace.for_project(host, second_session.binding)
    second, other = view("source", second_owner)
    picker, other_picker = item(first, "projectManagedDocumentPicker"), item(second, "projectManagedDocumentPicker")
    original_other_name = other_picker.property("displayText")
    controller.bridge.setField("name", "未保存的当前源")
    events()
    assert picker.property("currentIndex") == -1 and picker.property("displayText") == "未保存的当前源 *"
    assert controller.saveCurrent(), controller.status
    original = controller.currentDocumentId
    events()
    assert picker.property("currentValue") == original and picker.property("displayText") == "未保存的当前源"
    assert controller.saveCopyCurrent(), controller.status
    events()
    copied = controller.currentDocumentId
    assert copied != original and "副本" in picker.property("displayText")
    # Information is an independent caller of the same retained owner; the
    # picker must update without adopting another project's row or stale label.
    info = controller.resourceInformation
    assert controller.updateResourceInformation("信息中改名的副本", info["summary"], info["notes"], info["token"])
    events()
    assert picker.property("displayText") == "信息中改名的副本 *"
    assert controller.saveCurrent(), controller.status
    events()
    assert picker.property("currentValue") == copied and picker.property("displayText") == "信息中改名的副本"
    assert controller.openDocument(original)
    events()
    assert picker.property("currentValue") == original and picker.property("displayText") == "未保存的当前源"
    assert other_picker.property("displayText") == original_other_name
    assert not other.currentDocumentId and other_picker.property("currentIndex") == -1
