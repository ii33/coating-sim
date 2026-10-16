"""Real single-row overflow actions keep their native, live control state."""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_project_resource_adapter import project as project
from tests.qt_test_guard import qml_item_window

pytestmark = [pytest.mark.qt, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[1]


def events():
    QCoreApplication.processEvents()
    QTest.qWait(20)
    QCoreApplication.processEvents()


def qml(scope, source):
    expression = QQmlExpression(QQmlEngine.contextForObject(scope), scope, source)
    value, _undefined = expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    return value.toVariant() if hasattr(value, "toVariant") else value


@pytest.fixture
def runtime():
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    component = QQmlComponent(engine)
    component.setData(b'''pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import "coat_sim/ui/qml"
import "coat_sim/ui/qml/theme_controls" as ThemeControls
Window {
    id: window
    width: 900; height: 240; visible: true
    property int calls: 0
    property bool actionsEnabled: true
    property bool actionsVisible: true
    property string actionLabel: "Start"
    ResponsiveOperationBar {
        id: bar; objectName: "testBar"
        x: 10; y: 10; width: parent.width - 20
        ProjectModuleSelector {
            objectName: "testNavigation"
            currentModuleKey: "film_design"
            navigation: QtObject {
                property string projectName: "Long project name"
                property var projectModules: [{key: "film_design", name: "Film design"}]
                function openProjectModule(key) {}
            }
        }
        ThemeControls.ThemeButton {
            id: actionButton; objectName: "startAction"; width: 180
            text: window.actionLabel
            enabled: window.actionsEnabled
            visible: window.actionsVisible
            checkable: true
            onClicked: window.calls += 1
        }
        ThemeControls.ThemeButton {
            id: menuButton; objectName: "menuAction"; width: 180; text: "Exports"
            onClicked: nestedMenu.open()
            Menu {
                id: nestedMenu; objectName: "originalActionMenu"; y: menuButton.height
                MenuItem { text: "Original export"; onTriggered: window.calls += 10 }
            }
        }
    }
    Shortcut { sequence: "Ctrl+J"; onActivated: actionButton.click() }
}''', QUrl.fromLocalFile(str(ROOT / "responsive-bar-test.qml")))
    assert not component.isError(), component.errorString()
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errorString()
    window.requestActivate()
    events()
    try:
        yield window
    finally:
        # Close popup owners and finish deferred work before destroying the
        # engine; do not leave Python wrappers parented to a deleted QML item.
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def item(window, name):
    value = window.findChild(QQuickItem, name)
    assert value is not None, name
    return value


def click(window, control):
    window = qml_item_window(control)
    assert window is not None
    if QGuiApplication.platformName() == "windows" and not window.isActive():
        window.requestActivate()
        assert QTest.qWaitForWindowActive(window, 2000)
    if QGuiApplication.platformName() == "windows":
        assert QTest.qWaitForWindowExposed(window, 2000)
    point = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point.toPoint())
    events()


def supported_narrow_size(window):
    # Offscreen accepts widths that a native window manager must reject.
    width = max(360, window.minimumWidth())
    window.resize(width, max(650, window.minimumHeight()))
    events()
    assert window.width() == width


def module_action(window, width):
    bar = item(window, "projectResourceControls").parentItem()
    context = QQmlEngine.contextForObject(bar)
    component = QQmlComponent(context.engine())
    component.setData(b'''import QtQuick
import "coat_sim/ui/qml/theme_controls" as ThemeControls
ThemeControls.ThemeButton {
    objectName: "liveCarrierAction"
    property string liveLabel: "Original label"
    text: liveLabel
    checkable: true
}''', QUrl.fromLocalFile(str(ROOT / "carrier-action-test.qml")))
    action = component.create(context)
    assert isinstance(action, QQuickItem), component.errorString()
    action.setWidth(width)
    action.setParent(bar)
    action.setParentItem(bar)
    return component, action


def test_wide_narrow_restore_geometry_and_native_overflow_click(runtime, tmp_path):
    window = runtime
    bar = item(window, "testBar")
    action = item(window, "startAction")
    navigation = item(window, "testNavigation")
    wide_height = bar.height()
    assert not item(window, "operationBarOverflowButton").isVisible()
    click(window, action)
    assert window.property("calls") == 1 and action.property("checked")
    window.resize(320, 240)
    events()
    assert bar.height() == wide_height
    assert action.x() < 0 and action.property("focusPolicy") == Qt.FocusPolicy.NoFocus.value
    assert qml(action, "Accessible.ignored")
    assert navigation.x() >= 0 and navigation.x() + navigation.width() <= bar.width()
    click(window, item(window, "operationBarOverflowButton"))
    entry = item(window, "operationBarOverflow_startAction")
    assert entry.property("checked")
    click(window, entry)
    assert window.property("calls") == 2 and not action.property("checked")
    assert not entry.property("checked")
    capture = window.grabWindow()
    assert not capture.isNull() and capture.save(str(tmp_path / "narrow-toolbar.png"))
    window.resize(900, 240)
    events()
    assert action.x() >= 0 and action.property("focusPolicy") == Qt.FocusPolicy.StrongFocus.value
    assert not qml(action, "Accessible.ignored")
    assert not item(window, "operationBarOverflowButton").isVisible()
    click(window, action)
    assert window.property("calls") == 3


def test_overflow_keeps_live_text_enabled_visibility_and_keyboard_shortcut(runtime):
    window = runtime
    window.resize(320, 240)
    events()
    click(window, item(window, "operationBarOverflowButton"))
    entry = item(window, "operationBarOverflow_startAction")
    window.setProperty("actionLabel", "Continue latest")
    window.setProperty("actionsEnabled", False)
    events()
    assert entry.property("text") == "Continue latest" and not entry.isEnabled()
    click(window, entry)
    assert window.property("calls") == 0
    window.setProperty("actionsEnabled", True)
    events()
    click(window, entry)
    assert window.property("calls") == 1
    QTest.keyClick(window, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
    events()
    assert window.property("calls") == 2
    window.setProperty("actionsVisible", False)
    events()
    assert qml(item(window, "testBar"), 'overflowItems.map(value => value.objectName).indexOf("startAction") === -1')
    assert window.findChild(QObject, "operationBarOverflow_startAction") is None


def test_overflow_opens_original_submenu_at_visible_anchor(runtime):
    window = runtime
    window.resize(320, 240)
    events()
    click(window, item(window, "operationBarOverflowButton"))
    click(window, item(window, "operationBarOverflow_menuAction"))
    menu = window.findChild(QObject, "originalActionMenu")
    assert menu is not None and menu.property("visible")
    assert qml(menu, 'parent.objectName') == "operationBarOverflowButton"
    assert qml(menu, 'x >= -parent.x && x + width <= window.width - parent.x')
    qml(menu, "itemAt(0).click()")
    events()
    assert window.property("calls") == 10


@pytest.mark.parametrize("key", ["source", "substrate", "machine", "mask", "process", "materials"])
def test_managed_navigation_and_information_remain_in_one_narrow_row(project, key):
    _app, _stores, _host, _film, _resources, view = project
    window, _controller = view(key)
    supported_narrow_size(window)
    bar = item(window, "projectResourceControls")
    navigation = item(window, "projectModuleSelectorRoot")
    picker = item(window, "projectManagedDocumentPicker")
    information = item(window, "projectResourceInformationButton")
    parent = bar.parentItem()
    assert abs(bar.y() - navigation.y()) <= 5
    assert navigation.x() >= 0 and bar.x() >= navigation.x() + navigation.width()
    assert bar.x() + bar.width() <= parent.width() + 1
    assert picker.x() + picker.width() <= information.x()
    assert information.x() - picker.x() - picker.width() <= 5
    assert information.x() + information.width() <= bar.width() + 1
    capture = window.grabWindow()
    assert not capture.isNull()
    if key == "source":
        review = ROOT / "artifacts" / "responsive-operation-bar-review"
        review.mkdir(parents=True, exist_ok=True)
        assert capture.save(str(review / f"project-source-narrow-{window.width()}.png"))
        window.resize(960, 650)
        events()
        assert window.grabWindow().save(str(review / "project-source-normal-960.png"))


def test_narrow_resource_menu_uses_original_save_copy_and_transfer_state(project):
    app, _stores, _host, _film, resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    identity = controller.currentDocumentId
    supported_narrow_size(window)
    # Inline save remains available at the real supported minimum. Consume
    # space with an adjacent module action to exercise genuine resource overflow.
    assert item(window, "sourceSaveButton").isVisible()
    resource_bar = item(window, "projectResourceControls")
    bar = resource_bar.parentItem()
    available = qml(bar, "width - collectMetrics().reduce((sum, entry) => "
                         "sum + entry.minimum, 0) - spacing * collectMetrics().length "
                         "- overflowAnchor.width - spacing")
    component, action = module_action(window, available - 1)
    events()
    assert action.x() >= 0
    controller.bridge.setField("source_exponent", "2.25")
    events()
    more = item(window, "projectResourceMoreActionsButton")
    assert not item(window, "sourceSaveButton").isVisible()
    click(window, more)
    save = item(window, "projectResourceOverflowSaveAction")
    assert save.isVisible() and save.isEnabled()
    click(window, save)
    assert not controller.dirty and controller.currentDocumentId == identity
    assert resources.service.load_document(identity).data["source_exponent"] == 2.25
    controller.bridge.setField("source_exponent", "-1")
    events()
    click(window, more)
    copy_action = item(window, "projectResourceCreateCopyAction")
    assert copy_action.property("text") == "创建副本"
    assert not copy_action.isEnabled() and not save.isEnabled()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    events()
    controller.bridge.setField("source_exponent", "2.25")
    events()
    click(window, more)
    click(window, copy_action)
    assert controller.currentDocumentId != identity
    assert len(resources.service.list_documents("source")) == 2
    click(window, more)
    click(window, item(window, "projectResourceOverflowTransferAction"))
    assert item(window, "projectResourceImportMenuItem").isVisible()
    assert item(window, "projectResourceExportMenuItem").isVisible()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    app.processEvents()


def test_project_resource_carrier_merges_live_module_actions_into_one_menu(project):
    _app, _stores, _host, _film, _resources, view = project
    window, _controller = view("source")
    supported_narrow_size(window)
    resource_bar = item(window, "projectResourceControls")
    bar = resource_bar.parentItem()
    # Guarantee actual overflow at the supported window size, rather than
    # relying on a window manager accepting a below-minimum resize.
    component, action = module_action(window, bar.width())
    clicked_signal = action.metaObject().method(action.metaObject().indexOfSignal("clicked()"))
    clicked = QSignalSpy(action, clicked_signal)
    assert clicked.isValid()
    events()
    assert action.x() < 0
    assert not item(window, "operationBarOverflowButton").isVisible()
    more = item(window, "projectResourceMoreActionsButton")
    assert more.isVisible()
    click(window, more)
    entry = item(window, "operationBarOverflow_liveCarrierAction")
    action.setProperty("liveLabel", "Latest label")
    action.setEnabled(False)
    events()
    assert entry.property("text") == "Latest label" and not entry.isEnabled()
    action.setEnabled(True)
    events()
    click(window, entry)
    assert clicked.count() == 1 and action.property("checked")
    click(window, more)
    assert entry.property("checked")
    click(window, entry)
    assert clicked.count() == 2 and not action.property("checked")


@pytest.fixture
def resource_strip(project):
    """Use the real source controller with precisely controlled strip widths."""
    _app, _stores, _host, _film, resources, view = project
    window, controller = view("source")
    controls = item(window, "projectResourceControls")
    outer = controls.parentItem()
    controls.setParentItem(window.contentItem())
    controls.setX(10)
    controls.setY(10)
    controls.setZ(100)
    outer.setVisible(False)
    events()
    return window, controller, resources, controls


def resource_width(controls, *names):
    objects = {"newButton": "projectResourceNewButton", "saveButton": "sourceSaveButton",
               "copyButton": "projectResourceCreateCopyButton"}
    return controls.property("operationBarMinimumWidth") + sum(
        controls.findChild(QQuickItem, objects[name]).implicitWidth() + controls.property("spacing")
        for name in names)


def assert_resource_strip_fits(controls):
    shown = [child for child in controls.childItems() if child.isVisible() and child.width() > 0]
    shown.sort(key=lambda child: child.x())
    for index, child in enumerate(shown):
        assert child.x() >= -1
        assert child.x() + child.width() <= controls.width() + 1
        if index:
            previous = shown[index - 1]
            assert child.x() >= previous.x() + previous.width() - 1


@pytest.mark.parametrize("key", ["source", "substrate", "machine", "mask", "process", "materials"])
def test_wide_managed_resource_copy_is_inline_without_duplicate_menu_entry(project, key):
    _app, _stores, _host, _film, _resources, view = project
    window, _controller = view(key)
    window.resize(1600, max(800, window.minimumHeight()))
    events()
    controls = item(window, "projectResourceControls")
    copy_button = item(window, "projectResourceCreateCopyButton")
    assert copy_button.isVisible() and copy_button.property("text") == "创建副本"
    assert not qml(copy_button, "Accessible.ignored")
    if key == "source":
        # The parent must use measured preferred width, even beyond the old cap.
        qml(copy_button, "font.pointSize = font.pointSize * 4")
        events()
        assert controls.property("operationBarPreferredWidth") > 520
        assert controls.width() > 520 and copy_button.isVisible()
    assert_resource_strip_fits(controls)
    more = item(window, "projectResourceMoreActionsButton")
    if more.isVisible():
        click(window, more)
        assert not item(window, "projectResourceCreateCopyAction").isVisible()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        events()


def test_resource_copy_uses_measured_width_boundaries_and_restores(resource_strip):
    window, _controller, _resources, controls = resource_strip
    copy_button = item(window, "projectResourceCreateCopyButton")
    more = item(window, "projectResourceMoreActionsButton")
    copy_width = resource_width(controls, "newButton", "saveButton", "copyButton")
    for _ in range(2):
        controls.setWidth(controls.property("operationBarPreferredWidth"))
        events()
        assert copy_button.isVisible() and not more.isVisible()
        assert item(window, "projectResourceTransferMenuButton").isVisible()
        assert_resource_strip_fits(controls)
        # Medium: copy fits exactly; only the larger transfer action overflows.
        controls.setWidth(copy_width)
        events()
        assert copy_button.isVisible() and more.isVisible()
        assert not item(window, "projectResourceTransferMenuButton").isVisible()
        assert_resource_strip_fits(controls)
        click(window, more)
        assert not item(window, "projectResourceCreateCopyAction").isVisible()
        assert item(window, "projectResourceOverflowTransferAction").isVisible()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        events()
        # One real logical pixel less no longer fits copy plus the shared menu.
        controls.setWidth(copy_width - 1)
        events()
        assert not copy_button.isVisible() and more.isVisible()
        assert_resource_strip_fits(controls)
        click(window, more)
        assert item(window, "projectResourceCreateCopyAction").isVisible()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        events()
    controls.setWidth(controls.property("operationBarPreferredWidth"))
    events()
    assert copy_button.isVisible() and not more.isVisible()


@pytest.mark.parametrize("valid_on_restore", [True, False])
@pytest.mark.parametrize("menu_open_on_restore", [True, False])
def test_resource_copy_font_growth_focus_and_accessibility_restore(
        resource_strip, valid_on_restore, menu_open_on_restore):
    window, controller, _resources, controls = resource_strip
    copy_button = item(window, "projectResourceCreateCopyButton")
    more = item(window, "projectResourceMoreActionsButton")
    # A wider real font changes both the capacity decision and preferred width.
    qml(copy_button, "font.pointSize = font.pointSize * 2")
    events()
    controls.setWidth(controls.property("operationBarPreferredWidth"))
    events()
    assert copy_button.isVisible()
    assert_resource_strip_fits(controls)
    copy_button.forceActiveFocus()
    assert copy_button.hasActiveFocus()
    controls.setWidth(resource_width(controls, "newButton", "saveButton", "copyButton") - 1)
    events()
    assert not copy_button.isVisible() and more.hasActiveFocus()
    assert copy_button.property("focusPolicy") == Qt.FocusPolicy.NoFocus.value
    assert qml(copy_button, "Accessible.ignored")
    QTest.keyClick(window, Qt.Key.Key_Space)
    events()
    assert item(window, "projectResourceCreateCopyAction").isVisible()
    resource_menu = window.findChild(QObject, "projectResourceActionsMenu")
    assert resource_menu is not None and resource_menu.property("visible")
    if not menu_open_on_restore:
        QTest.keyClick(window, Qt.Key.Key_Escape)
        events()
        more.forceActiveFocus()
        assert more.hasActiveFocus()
    if not valid_on_restore:
        controller.bridge.setField("source_exponent", "-1")
        events()
        assert not copy_button.isEnabled()
    controls.setWidth(controls.property("operationBarPreferredWidth"))
    events()
    assert copy_button.isVisible()
    assert copy_button.property("focusPolicy") == Qt.FocusPolicy.StrongFocus.value
    assert not qml(copy_button, "Accessible.ignored")
    assert not more.isVisible() and not more.hasActiveFocus()
    assert not resource_menu.property("visible")
    assert more.property("focusPolicy") == Qt.FocusPolicy.NoFocus.value
    assert qml(more, "Accessible.ignored")
    assert copy_button.hasActiveFocus() or item(window, "projectResourceInformationButton").hasActiveFocus()
    # Space now acts on the restored inline target, never on a hidden menu entry.
    QTest.keyClick(window, Qt.Key.Key_Space)
    events()
    assert not item(window, "projectResourceCreateCopyAction").isVisible()
    assert not resource_menu.property("visible")
    assert_resource_strip_fits(controls)


def test_inline_and_overflow_copy_share_guard_validation_busy_and_original_action(resource_strip):
    window, controller, resources, controls = resource_strip
    assert controller.saveCurrent(), controller.status
    for inline in (True, False):
        controls.setWidth(controls.property("operationBarPreferredWidth") if inline
                          else resource_width(controls, "newButton", "saveButton") - 1)
        events()
        copy_button = item(window, "projectResourceCreateCopyButton")
        more = item(window, "projectResourceMoreActionsButton")
        if not inline:
            click(window, more)
        action = copy_button if inline else item(window, "projectResourceCreateCopyAction")
        assert action.isVisible()
        count = len(resources.service.list_documents("source"))
        identity = controller.currentDocumentId
        qml(controls, "beforeDocumentAction = (action, documentId) => false")
        click(window, action)
        assert len(resources.service.list_documents("source")) == count
        assert controller.currentDocumentId == identity
        qml(controls, "beforeDocumentAction = (action, documentId) => true")
        for busy in (False, True):
            controller.bridge.setField("source_exponent", "2.25" if busy else "-1")
            controller._transfer_busy = busy
            controller.transferChanged.emit()
            events()
            if not inline and not action.isVisible():
                # Keep the one overflow menu available while its source is busy.
                qml(more, "click()")
                events()
            assert not copy_button.isEnabled() and not action.isEnabled()
            click(window, action)
            assert len(resources.service.list_documents("source")) == count
            assert controller.currentDocumentId == identity
        controller._transfer_busy = False
        controller.transferChanged.emit()
        controller.bridge.setField("source_exponent", "2.25")
        events()
        if not inline and not action.isVisible():
            click(window, more)
        assert action.isEnabled()
        click(window, action)
        assert controller.currentDocumentId != identity
        assert len(resources.service.list_documents("source")) == count + 1
