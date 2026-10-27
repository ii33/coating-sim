"""Rendered keyboard/pointer state contrast for shared dropdowns and menus."""
from pathlib import Path
import os
import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from shiboken6 import getCppPointer, isValid
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_scoped_resource_libraries import qml
from tests.test_project_module_selector import runtime as runtime, events

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def _qml_object_identity(obj: QObject | None) -> dict[str, object] | None:
    if obj is None:
        return None
    result: dict[str, object] = {"valid": isValid(obj), "python_type": type(obj).__name__}
    if isValid(obj):
        parent = obj.parent()
        result.update(cpp=getCppPointer(obj)[0], object_name=obj.objectName(),
                      qml_type=obj.metaObject().className(),
                      ownership=QQmlEngine.objectOwnership(obj).name,
                      parent_cpp=getCppPointer(parent)[0] if parent is not None else None)
    return result


def _picker_lookup_evidence(engine, component, window, picker, field):
    """Read failed-lookup evidence without waiting, recreating, or accepting a fallback."""
    evidence = {"engine": _qml_object_identity(engine),
                "component": _qml_object_identity(component),
                "source_url": component.url().toString(),
                "component_status": component.status().name,
                "qml_errors": component.errorString(),
                "root": _qml_object_identity(window),
                "picker": _qml_object_identity(picker),
                "field": _qml_object_identity(field)}
    if isValid(window):
        children = window.findChildren(QObject)
        evidence["qobject_descendant_count"] = len(children)
        evidence["root_children"] = [_qml_object_identity(child) for child in window.children()]
        evidence["named_qobject_descendants"] = [_qml_object_identity(child) for child in children
                                                 if child.objectName()]
        visual_items = []
        pending = [window.contentItem()]
        while pending:
            item = pending.pop()
            if item is None:
                continue
            if item.objectName():
                visual_items.append(_qml_object_identity(item))
            pending.extend(item.childItems())
        evidence["named_visual_items"] = visual_items
    return evidence


def contrast(first, second):
    def luminance(color):
        color = QColor(color)
        values = [color.redF(), color.greenF(), color.blueF()]
        values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(values, [.2126, .7152, .0722], strict=True))
    a, b = sorted([luminance(first), luminance(second)])
    return (b + .05) / (a + .05)


def check_row(row, tokens, foreground, background):
    label = row.property("contentItem")
    surface = row.property("background")
    if label.property("color") is None:
        label = next(item for item in label.childItems() if item.property("text") == row.property("text"))
    actual_text = label.property("color").name()
    actual_background = surface.property("color").name()
    assert actual_text == tokens[foreground].lower()
    assert actual_background == tokens[background].lower()
    assert contrast(actual_text, actual_background) >= (2.0 if foreground == "colorDisabledText" else 4.5)


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("kind", ["combo", "material"])
def test_shared_dropdown_keyboard_hover_current_disabled_render(mode, kind):
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    tokens = build_theme_snapshot(mode)
    theme = ThemeEngineBridge(tokens, 0, mode, mode, "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    source = """import QtQuick
import QtQuick.Controls
import "theme_controls" as T
Window {
 width: 650; height: 420; visible: true; color: Theme.snapshot.colorWindow
 T.ThemeComboBox { id: combo; objectName: "combo"; x: 25; y: 25; width: 260; model: ["Alpha", "Beta", "Gamma"]; currentIndex: 0 }
 T.ThemeMaterialPicker { id: material; objectName: "material"; x: 330; y: 25; width: 260;
  model: [{id:"a",name:"Alpha"},{id:"b",name:"Beta"},{id:"c",name:"Gamma"}]; currentIndex: 0;
  onActivated: index => currentIndex = index }
}"""
    component = QQmlComponent(engine)
    root = Path(__file__).parents[1] / "coat_sim/ui/qml/DropdownContrastHarness.qml"
    component.setData(source.encode(), QUrl.fromLocalFile(str(root)))
    window = component.create()
    assert window is not None, component.errorString()
    assert isinstance(window, QQuickWindow)
    try:
        window.requestActivate()
        QTest.qWait(35)
        picker = window.findChild(QQuickItem, kind)
        assert picker is not None
        if kind == "combo":
            picker.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_Space)
        else:
            material_input = window.findChild(QQuickItem, "material_input")
            assert material_input is not None
            material_input.forceActiveFocus()
        QTest.qWait(35)
        QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.qWait(25)
        highlighted = picker.property("highlightedIndex")
        assert highlighted >= 0
        expression = "popup.contentItem.itemAtIndex" if kind == "combo" else "optionsListView.itemAtIndex"
        row = qml(engine, picker, f"{expression}({highlighted})")
        assert row.property("highlighted")
        check_row(row, tokens, "colorTextOnAccent", "colorAccent")
        QTest.keyClick(window, Qt.Key.Key_Up)
        QTest.qWait(25)
        hover = qml(engine, picker, f"{expression}(2)")
        point = hover.mapToScene(QPointF(hover.width()/2, hover.height()/2)).toPoint()
        QTest.mouseMove(window, QPoint(620, 360))
        QTest.qWait(20)
        QTest.mouseMove(window, point)
        QTest.qWait(25)
        assert hover.property("hovered")
        check_row(hover, tokens, "colorTextOnAccent", "colorAccent")
        current = qml(engine, picker, f"{expression}(0)")
        if not current.property("highlighted"):
            check_row(current, tokens, "colorText", "colorSelection")
        assert window.grabWindow().save(str(Path(os.environ["TEMP"]) / f"dropdown-{kind}-{mode}.png"))
        hover.setEnabled(False)
        QTest.qWait(20)
        check_row(hover, tokens, "colorDisabledText", "colorDisabledSurface")
        hover.setEnabled(True)
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(25)
        assert picker.property("currentIndex") == 2
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("object_variant", ["metaobject", "native"])
def test_material_picker_duplicate_labels_are_human_facing_and_keep_ids(monkeypatch, object_variant):
    if object_variant == "metaobject":
        original_qml = qml

        def opaque_object_result(engine: QQmlEngine, scope: QObject, expression: str) -> object:
            value = original_qml(engine, scope, expression)
            # Model the exact CI return type only at the object-valued QVariant
            # boundary. Scalar labels/IDs still come from the real QML engine.
            return value.metaObject() if isinstance(value, QObject) else value

        monkeypatch.setitem(globals(), "qml", opaque_object_result)
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    tokens = build_theme_snapshot("light")
    theme = ThemeEngineBridge(tokens, 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    source = '''import QtQuick
import QtQuick.Controls
import "theme_controls" as T
Window {
 width: 650; height: 420; color: Theme.snapshot.colorWindow
 T.ThemeMaterialPicker {
  id: picker; objectName: "picker"; x: 25; y: 25; width: 500
  duplicateContextLabel: "材料"
  model: [
   {id: "builtin_ideal_air", name: "Air", material_categories: ["film"]},
   {id: "builtin_ideal_air_substrate", name: "Air", material_categories: ["substrate"]},
   {id: "builtin_zns_literature_20261004", name: "ZnS", origin: "global"},
   {id: "project_zns", name: "ZnS", origin: "project"},
   {id: "same_name_one", name: "Same purpose"},
   {id: "same_name_two", name: "Same purpose"},
   {id: "unnamed_secret_id", name: "unnamed_secret_id"}
  ]
 }
}'''
    component = QQmlComponent(engine)
    harness = Path(__file__).parents[1] / "coat_sim/ui/qml/DropdownContrastHarness.qml"
    component.setData(source.encode("utf-8"), QUrl.fromLocalFile(str(harness)))
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errorString()
    try:
        window.show()
        window.requestActivate()
        QTest.qWait(35)
        picker = window.findChild(QQuickItem, "picker")
        field = window.findChild(QQuickItem, "picker_input")
        lookup_evidence = ""
        if picker is None or field is None:
            try:
                lookup_evidence = str(_picker_lookup_evidence(engine, component, window, picker, field))
            except Exception as error:
                lookup_evidence = f"Failed to capture picker lookup evidence: {type(error).__name__}: {error}"
        assert picker is not None and field is not None, lookup_evidence
        field.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.qWait(35)
        assert picker.property("popupVisible")
        labels = []
        for index in range(7):
            assert qml(engine, picker, f"optionsListView.itemAtIndex({index}) !== null") is True
            label = qml(engine, picker, f"optionsListView.itemAtIndex({index}).text")
            assert isinstance(label, str), f"Option {index} text returned {type(label).__name__}"
            labels.append(label)
        assert labels == [
            "Air · 薄膜材料", "Air · 基片材料",
            "ZnS · 全局材料库", "ZnS · 当前工程",
            "Same purpose · 材料 · 选项 1", "Same purpose · 材料 · 选项 2",
            "未命名材料",
        ]
        shown_labels = " ".join(labels)
        for internal_id in ("builtin_ideal_air", "builtin_ideal_air_substrate",
                            "builtin_zns_literature_20261004", "project_zns",
                            "same_name_one", "same_name_two", "unnamed_secret_id"):
            assert internal_id not in shown_labels
        assert qml(engine, picker, "valueAt(0)") == "builtin_ideal_air"
        assert qml(engine, picker, "valueAt(1)") == "builtin_ideal_air_substrate"
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_project_menu_keyboard_pressed_and_disabled_states(runtime, mode):
    tokens = build_theme_snapshot(mode)
    runtime.theme._replace_snapshot(tokens, 1, mode, mode, "qa", "QA", {})
    root, _owner = runtime.create("source")
    button = runtime.item(root, "projectModuleSelector")
    button.forceActiveFocus()
    runtime.key(Qt.Key.Key_Space)
    menu = runtime.menu(root)
    if menu.property("currentIndex") == -1:
        runtime.key(Qt.Key.Key_Down)
    row = runtime.category_item(menu, menu.property("currentIndex"))
    assert row.property("highlighted")
    check_row(row, tokens, "colorTextOnAccent", "colorAccent")
    runtime.key(Qt.Key.Key_Up)
    row = runtime.category_item(menu, 1)
    point = row.mapToScene(QPointF(row.width()/2, row.height()/2)).toPoint()
    QTest.mouseMove(runtime.window, QPoint(600, 380))
    events()
    QTest.mouseMove(runtime.window, point)
    events()
    assert row.property("hovered")
    check_row(row, tokens, "colorTextOnAccent", "colorAccent")
    # Keep the pointer down without triggering navigation.
    QTest.mousePress(runtime.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    events()
    check_row(row, tokens, "colorTextOnAccent", "colorAccent")
    row.setEnabled(False)
    events()
    check_row(row, tokens, "colorDisabledText", "colorDisabledSurface")
    assert runtime.window.grabWindow().save(str(Path(os.environ["TEMP"]) / f"dropdown-menu-{mode}.png"))
    QTest.mouseRelease(runtime.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
