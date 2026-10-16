"""Real Qt pointer/keyboard regressions for the shared editing boundary."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QInputMethodEvent, QWheelEvent
from PySide6.QtQml import QQmlComponent, QQmlContext, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from coat_sim.ui.theme_tokens import build_theme_snapshot
from coat_sim.ui.input_focus import install_input_focus_policy
from tests.qt_test_guard import qml_item_window

pytestmark = pytest.mark.qt
QML_ROOT = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml"


@pytest.fixture(params=("AppWindow", "Window"))
def inputs(request):
    app = QApplication.instance() or QApplication([])
    install_input_focus_policy()
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("Theme", {"snapshot": build_theme_snapshot("light"), "assets": {}, "mode": "light"})
    source = '''import QtQuick
import QtQuick.Controls
import "."
import "theme_controls" as T
AppWindow {
    width: 900; height: 700; visible: true
    property int clicks: 0
    property int acceptedCount: 0
    property int finishedCount: 0
    property int navigationCount: 0
    property int materialSelections: 0
    property int defaultAccepts: 0
    Flickable { id: scroll; objectName: "scroll"; x: 600; y: 70; width: 200; height: 250; contentHeight: 1000; clip: true
        Rectangle { width: 200; height: 1000; color: "#dddddd" } }
    Dialog { id: validationDialog; modal: true; width: 300; height: 160; standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: defaultAccepts += 1
        contentItem: T.ThemeField { id: dialogField; objectName: "dialogField"; text: "12"; validator: IntValidator { bottom: 1; top: 99 } } }
    Component { id: transientEditor; T.ThemeField { text: "transient" } }
    Component { id: transientMultiline; TextArea { text: "popup draft"; selectByMouse: true } }
    T.ThemeField { id: first; objectName: "first"; x: 30; y: 70; width: 220; text: "first value"
        onAccepted: acceptedCount += 1
        onEditingFinished: finishedCount += 1 }
    T.ThemeField { id: second; objectName: "second"; x: 30; y: 130; width: 220; text: "second value" }
    T.ThemeField { id: number; objectName: "number"; x: 30; y: 190; width: 220; text: "12"
        validator: IntValidator { bottom: 1; top: 99 }
        onAccepted: acceptedCount += 1 }
    T.ThemeField { id: table; objectName: "table"; x: 30; y: 250; width: 220; text: "table value"
        Keys.onPressed: event => handleColumnNavigation(event, delta => { navigationCount += delta; second.focusAndSelectAll() }) }
    T.ThemeSpinBox { id: spin; objectName: "spin"; x: 30; y: 310; width: 220; from: 0; to: 999; value: 123; editable: true }
    SpinBox { id: nativeSpin; objectName: "nativeSpin"; x: 30; y: 440; width: 220; from: 0; to: 999; value: 123; editable: true }
    ComboBox { id: nativeCombo; objectName: "nativeCombo"; x: 30; y: 500; width: 220; editable: true; model: ["alpha", "beta"] }
    T.ThemeComboBox { id: combo; objectName: "combo"; x: 30; y: 370; width: 220; editable: true; model: ["alpha", "beta"] }
    TextArea { id: multi; objectName: "multi"; x: 300; y: 70; width: 260; height: 100; text: "line one\\nline two"; selectByMouse: true }
    TextArea { objectName: "output"; x: 300; y: 190; width: 260; height: 80; text: "read only output"; readOnly: true; selectByMouse: true }
    T.ThemeButton { objectName: "button"; x: 300; y: 310; text: "click"; focusPolicy: Qt.NoFocus; onClicked: clicks += 1 }
    T.ThemeMaterialPicker { id: material; objectName: "material"; x: 300; y: 390; width: 220;
        model: [{id: "a", name: "Alpha"}, {id: "b", name: "Beta"}]; currentIndex: 0; onActivated: index => { currentIndex = index; materialSelections += 1 } }
}
'''
    if request.param == "Window":
        source = source.replace("AppWindow {", "Window {", 1)
    component = QQmlComponent(engine)
    component.setData(source.encode(), QUrl.fromLocalFile(str(QML_ROOT / "FocusPolicyHarness.qml")))
    assert component.isReady(), [error.toString() for error in component.errors()]
    window = component.create()
    assert isinstance(window, QQuickWindow), [error.toString() for error in component.errors()]
    window.requestActivate()
    QTest.qWait(30)
    window.contentItem().forceActiveFocus()
    QTest.qWait(20)
    window.setProperty("finishedCount", 0)

    def evaluate(code):
        expression = QQmlExpression(QQmlEngine.contextForObject(window), window, code)
        result, _undefined = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        return result.toVariant() if hasattr(result, "toVariant") else result

    def find(name):
        result = window.findChild(QQuickItem, name)
        assert result is not None, name
        return result

    def click(item, x=None, y=None):
        point = item.mapToScene(QPointF(item.width() / 2 if x is None else x,
                                       item.height() / 2 if y is None else y)).toPoint()
        surface = qml_item_window(item)
        assert surface is not None
        QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(20)

    try:
        yield SimpleNamespace(window=window, engine=engine, find=find, click=click, evaluate=evaluate)
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_each_new_mouse_focus_selects_but_repeated_click_and_drag_edit_locally(inputs):
    first, second = inputs.find("first"), inputs.find("second")
    for _ in range(3):
        inputs.click(first, 30)
        assert first.property("selectedText") == "first value"
        inputs.click(first, 40)
        assert first.property("selectedText") == ""
        start = first.mapToScene(QPointF(15, first.height() / 2)).toPoint()
        end = first.mapToScene(QPointF(55, first.height() / 2)).toPoint()
        QTest.mousePress(inputs.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(inputs.window, end, 30)
        QTest.mouseRelease(inputs.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        QTest.qWait(20)
        assert 0 < len(first.property("selectedText")) < len(first.property("text"))
        inputs.click(second)
        assert not first.property("activeFocus")
        assert second.property("selectedText") == "second value"


def test_tab_shift_tab_and_reentry_select_all(inputs):
    first, second = inputs.find("first"), inputs.find("second")
    inputs.click(first)
    QTest.keyClick(inputs.window, Qt.Key.Key_Tab)
    QTest.qWait(20)
    assert second.property("activeFocus")
    assert second.property("selectedText") == "second value"
    QTest.keyClick(inputs.window, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    QTest.qWait(20)
    assert first.property("activeFocus")
    assert first.property("selectedText") == "first value"


def test_enter_submits_then_blurs_and_invalid_number_stays(inputs):
    first, number = inputs.find("first"), inputs.find("number")
    inputs.click(first)
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert inputs.window.property("acceptedCount") == 1
    assert inputs.window.property("finishedCount") >= 1
    assert not first.property("activeFocus")
    inputs.click(number)
    QTest.keyClick(inputs.window, Qt.Key.Key_Backspace)
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert number.property("activeFocus")
    assert inputs.window.property("acceptedCount") == 1


def test_blank_click_blurs_without_swallowing_button_and_drag_keeps_focus(inputs):
    first = inputs.find("first")
    inputs.click(first)
    inputs.click(inputs.find("button"))
    assert inputs.window.property("clicks") == 1
    assert not first.property("activeFocus")
    inputs.click(first)
    QTest.mouseClick(inputs.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(700, 500))
    QTest.qWait(20)
    assert not first.property("activeFocus")


def test_table_enter_navigates_and_multiline_enter_keeps_native_editing(inputs):
    table, second, multi = inputs.find("table"), inputs.find("second"), inputs.find("multi")
    inputs.click(table)
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert inputs.window.property("navigationCount") == 1
    assert second.property("activeFocus")
    assert second.property("selectedText") == "second value"
    for _ in range(2):
        inputs.click(multi)
        assert multi.property("selectedText") == multi.property("text")
        QTest.keyClick(inputs.window, Qt.Key.Key_Return)
        QTest.qWait(20)
        assert multi.property("text") == "\n"
        assert multi.property("activeFocus")
        inputs.click(second)


def test_spin_combo_and_readonly_boundaries(inputs):
    second = inputs.find("second")
    spin = inputs.evaluate("spin.contentItem")
    combo = inputs.evaluate("combo.contentItem")
    native_spin = inputs.evaluate("nativeSpin.contentItem")
    native_combo = inputs.evaluate("nativeCombo.contentItem")
    for editor in (spin, combo, native_spin, native_combo):
        for _ in range(2):
            inputs.click(editor, 25)
            assert editor.property("selectedText") == editor.property("text")
            assert editor.property("text")
            inputs.click(second)
        inputs.click(editor)
        QTest.keyClick(inputs.window, Qt.Key.Key_Return)
        QTest.qWait(20)
        assert not editor.property("activeFocus")
    output = inputs.find("output")
    inputs.click(output, 30)
    assert output.property("selectedText") == ""


def test_ime_composition_does_not_select_or_navigate(inputs):
    table = inputs.find("table")
    inputs.click(table)
    preedit = QInputMethodEvent("拼", [])
    QCoreApplication.sendEvent(table, preedit)
    assert table.property("inputMethodComposing")
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert inputs.window.property("navigationCount") == 0
    assert table.property("activeFocus")
    commit = QInputMethodEvent()
    commit.setCommitString("拼")
    QCoreApplication.sendEvent(table, commit)
    assert not table.property("inputMethodComposing")


def test_material_focus_replaces_name_and_menu_click_preserves_selection(inputs):
    field = inputs.find("material_input")
    second = inputs.find("second")
    for _ in range(2):
        inputs.click(field, 30)
        assert field.property("selectedText") == "Alpha"
        assert inputs.evaluate("material.filteredRows.length") == 2
        inputs.click(second)
        assert inputs.window.property("materialSelections") == 0
    inputs.click(field)
    # Popup options retain their native Controls overlay hit path.
    option = inputs.evaluate("material.optionsListView.itemAtIndex(1)")
    assert option is not None
    inputs.click(option)
    assert inputs.window.property("materialSelections") == 1
    assert field.property("text") == "Beta"
    assert not field.property("activeFocus")
    inputs.click(field)
    assert field.property("selectedText") == "Beta"
    for character in "missing":
        QTest.keyClick(inputs.window, getattr(Qt.Key, "Key_" + character.upper()))
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert not field.property("activeFocus")
    assert field.property("text") == "Beta"
    assert inputs.window.property("materialSelections") == 1


def test_drag_outside_is_observation_only_and_does_not_blur(inputs):
    field = inputs.find("first")
    inputs.click(field)
    QTest.mousePress(inputs.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(700, 450))
    QTest.mouseMove(inputs.window, QPoint(700, 550), 30)
    QTest.mouseRelease(inputs.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(700, 550))
    QTest.qWait(20)
    assert field.property("activeFocus")


def test_destroy_cancels_deferred_selection_and_numeric_sync_with_legacy_negative_control(inputs):
    import shiboken6
    from PySide6.QtCore import qInstallMessageHandler

    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    try:
        # An alive legacy callback is a diagnostic negative control: it
        # proves that our warning capture sees delayed QML callback output.
        legacy_component = QQmlComponent(inputs.engine)
        legacy_component.setData(b"""import QtQuick
TextInput { id: field
    function scheduleLegacy() { Qt.callLater(function() { console.warn("input-focus-negative-control"); if (field) field.selectAll() }) }
}""", QUrl.fromLocalFile(str(QML_ROOT / "LegacyFocusHarness.qml")))
        assert legacy_component.isReady()
        legacy_context = QQmlContext(inputs.engine.rootContext())
        legacy = legacy_component.create(legacy_context)
        assert legacy is not None
        assert QMetaObject.invokeMethod(legacy, "scheduleLegacy")
        QTest.qWait(20)
        assert any("input-focus-negative-control" in message for message in messages), messages
        shiboken6.delete(legacy)
        shiboken6.delete(legacy_context)
        messages.clear()
        for index in range(40):
            field = inputs.evaluate("transientEditor.createObject(contentItem)")
            field.setNumericSourceValue(index + 0.123456)
            field.focusAndSelectAll()
            shiboken6.delete(field)
        QTest.qWait(30)
        assert messages == []
    finally:
        qInstallMessageHandler(previous)


def test_return_never_activates_dialog_default_even_when_validation_rejects(inputs):
    inputs.evaluate("validationDialog.open(); dialogField.forceActiveFocus()")
    QTest.qWait(20)
    field = inputs.find("dialogField")
    QTest.keyClick(inputs.window, Qt.Key.Key_Backspace)
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert field.property("activeFocus")
    assert inputs.window.property("defaultAccepts") == 0
    assert inputs.evaluate("validationDialog.visible")
    QTest.keyClick(inputs.window, Qt.Key.Key_5)
    QTest.keyClick(inputs.window, Qt.Key.Key_Return)
    QTest.qWait(20)
    assert not field.property("activeFocus")
    assert inputs.window.property("defaultAccepts") == 0
    assert inputs.evaluate("validationDialog.visible")
    inputs.evaluate("validationDialog.close()")


def test_wheel_and_combo_menu_keep_native_actions(inputs):
    first = inputs.find("first")
    inputs.click(first)
    wheel = QWheelEvent(QPointF(700, 150), QPointF(700, 150), QPoint(), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QCoreApplication.sendEvent(inputs.window, wheel)
    QTest.qWait(30)
    assert first.property("activeFocus")
    assert inputs.find("scroll").property("contentY") > 0
    editor = inputs.evaluate("combo.contentItem")
    inputs.click(editor)
    inputs.evaluate("combo.popup.open()")
    QTest.qWait(20)
    option = inputs.evaluate("combo.popup.contentItem.itemAtIndex(1)")
    assert option is not None
    inputs.click(option)
    assert inputs.find("combo").property("currentIndex") == 1
    assert not inputs.evaluate("combo.popup.visible")


def test_popup_native_multiline_waits_for_mouse_release_and_padding_blurs(inputs):
    inputs.evaluate("validationDialog.open(); dialogField.forceActiveFocus()")
    editor = inputs.evaluate("transientMultiline.createObject(validationDialog.contentItem, {width: 180, height: 40})")
    QTest.qWait(20)
    point = editor.mapToScene(QPointF(25, 20)).toPoint()
    surface = qml_item_window(editor)
    assert surface is not None
    QTest.mousePress(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.qWait(30)
    assert editor.property("selectedText") != "popup draft"
    QTest.mouseRelease(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.qWait(20)
    assert editor.property("activeFocus")
    assert editor.property("selectedText") == "popup draft"
    # Five logical pixels from the dialog's origin is padding, outside all
    # editors. Modal overlay handling must still let this inside click through.
    point = inputs.evaluate("validationDialog.contentItem.mapToItem(null, -5, -5)")
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(round(point.x()), round(point.y())))
    QTest.qWait(20)
    assert not editor.property("activeFocus")
    assert inputs.evaluate("validationDialog.visible")
    inputs.evaluate("validationDialog.close()")


def test_title_click_blurs_but_title_drag_keeps_editing_and_native_grab(inputs):
    title = inputs.window.findChild(QQuickItem, "windowChromeTitleDrag")
    if title is None:
        pytest.skip("ordinary standalone window has no custom title bar")
    field = inputs.find("first")
    inputs.click(field)
    inputs.click(title)
    assert not field.property("activeFocus")
    inputs.click(field)
    start = title.mapToScene(QPointF(180, 12)).toPoint()
    QTest.mousePress(inputs.window, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, start)
    assert title.property("pressed")
    QTest.mouseMove(inputs.window, start + QPoint(50, 20), 30)
    QTest.qWait(20)
    assert field.property("activeFocus")
    QTest.mouseRelease(inputs.window, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, start + QPoint(50, 20))
    QTest.qWait(20)
    assert field.property("activeFocus")
