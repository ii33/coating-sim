"""Real Qt Quick coverage for NumericFormat and numeric ThemeField display."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]
QML_ROOT = ROOT / "coat_sim" / "ui" / "qml"
APP = QApplication.instance() or QApplication([])


def _component(engine: QQmlEngine, source: str, url: Path) -> QQmlComponent:
    component = QQmlComponent(engine)
    component.setData(source.encode("utf-8"), QUrl.fromLocalFile(str(url)))
    assert component.isReady(), [error.toString() for error in component.errors()]
    return component


def test_format_uses_qt_locale_and_handles_numeric_boundaries():
    engine = QQmlEngine()
    component = _component(engine, """
        import QtQml
        import "theme_controls/NumericFormat.js" as NumericFormat
        QtObject {
            readonly property string result: JSON.stringify([
                NumericFormat.format(1234.56789, Qt.locale("en_US"), 3),
                NumericFormat.format(-3.14159, Qt.locale("en_US"), 2),
                NumericFormat.format(12.0, Qt.locale("en_US"), 3),
                NumericFormat.format(1234.5, Qt.locale("de_DE"), 2),
                NumericFormat.format(0.0004, Qt.locale("en_US"), 3),
                NumericFormat.format(0.0005, Qt.locale("en_US"), 3),
                NumericFormat.format(-0.0004, Qt.locale("de_DE"), 3),
                NumericFormat.format(0, Qt.locale("en_US"), 3),
                NumericFormat.format(0/0, Qt.locale("en_US"), 3),
                NumericFormat.format(1/0, Qt.locale("en_US"), 3),
                NumericFormat.format(-1/0, Qt.locale("en_US"), 3),
                NumericFormat.format("", Qt.locale("en_US"), 3),
                NumericFormat.format(null, Qt.locale("en_US"), 3),
                NumericFormat.format(undefined, Qt.locale("en_US"), 3),
                NumericFormat.format("input", Qt.locale("en_US"), 3),
                NumericFormat.format(1.25, Qt.locale("en_US")),
                NumericFormat.format(1.25, Qt.locale("en_US"), 0),
                NumericFormat.format(1.123456789012345, Qt.locale("en_US"), 15),
                NumericFormat.format(1, Qt.locale("en_US"), 100)
            ])
        }
    """, QML_ROOT / "__numeric_format_test__.qml")
    result = component.create()
    assert result is not None, [error.toString() for error in component.errors()]
    assert json.loads(result.property("result")) == [
        "1,234.568", "-3.14", "12", "1.234,5", "4e-04", "0.001",
        "-4e-04", "0", "NaN", "Infinity", "-Infinity", "", "", "",
        "input", "1.25", "1", "1.123456789012345", "1",
    ]


def test_invalid_precision_raises_in_the_qml_javascript_engine():
    engine = QQmlEngine()
    component = _component(engine, """
        import QtQml
        import "theme_controls/NumericFormat.js" as NumericFormat
        QtObject {
            readonly property string errorText: {
                try {
                    NumericFormat.format(1, Qt.locale("en_US"), 1.5)
                    return "no error"
                } catch (error) {
                    return error.toString()
                }
            }
            readonly property string upperLimitErrorText: {
                try {
                    NumericFormat.format(1, Qt.locale("en_US"), 101)
                    return "no error"
                } catch (error) {
                    return error.toString()
                }
            }
        }
    """, QML_ROOT / "__numeric_format_test__.qml")
    result = component.create()
    assert result is not None, [error.toString() for error in component.errors()]
    assert "RangeError" in result.property("errorText")
    assert "RangeError" in result.property("upperLimitErrorText")


def _theme_tokens() -> dict[str, object]:
    return {
        "fontFamily": "Sans Serif", "fontSizeBody": 12,
        "fontWeightNormal": 400, "spaceSm": 8,
        "controlHeightMd": 36, "radiusSm": 3, "borderWidth": 1,
        "focusRingWidth": 2, "colorText": "#17212B",
        "colorDisabledText": "#687786", "colorTextMuted": "#526170",
        "colorSelection": "#D8EAFE", "colorWindow": "#F3F6FA",
        "colorSurface": "#FFFFFF", "colorSurfaceRaised": "#F8FAFC",
        "colorBorder": "#D4DDE6", "colorBorderStrong": "#7C8B9A",
        "colorFocusRing": "#095DB2", "colorDisabledSurface": "#E7ECF2",
    }


def test_theme_field_displays_variable_precision_without_changing_edit_value():
    engine = QQmlEngine()
    engine.rootContext().setContextProperty(
        "Theme", {"snapshot": _theme_tokens()}
    )
    component = _component(engine, """
        import QtQuick
        import QtQuick.Controls
        import "theme_controls" as ThemeControls
        Item {
            width: 400
            height: 100
            ThemeControls.ThemeField {
                objectName: "threeDecimals"
                width: 180
                numericDisplayValue: 0.123456789012345
            }
            ThemeControls.ThemeField {
                objectName: "fifteenDecimals"
                y: 48
                width: 180
                numericDisplayValue: 0.123456789012345
                numericDisplayDecimals: 15
            }
        }
    """, QML_ROOT / "__theme_field_numeric_test__.qml")
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]

    window = QQuickWindow()
    window.resize(400, 100)
    root.setParentItem(window.contentItem())
    window.show()
    QTest.qWait(30)
    short_field = root.findChild(QQuickItem, "threeDecimals")
    precise_field = root.findChild(QQuickItem, "fifteenDecimals")
    assert short_field is not None and precise_field is not None
    source_value = float(precise_field.property("numericDisplayValue"))

    assert short_field.property("text") == "0.123"
    assert precise_field.property("text") == "0.123456789012345"

    precise_field.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
    QTest.qWait(30)
    assert precise_field.property("activeFocus")
    assert precise_field.property("text") == "0.123456789012345"
    assert not precise_field.property("numericValueEdited")
    assert float(precise_field.property("numericDisplayValue")) == source_value

    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for key in (Qt.Key.Key_2, Qt.Key.Key_Period, Qt.Key.Key_7, Qt.Key.Key_5):
        QTest.keyClick(window, key)
    QTest.qWait(20)
    assert precise_field.property("text") == "2.75"
    assert precise_field.property("numericValueEdited")
    assert float(precise_field.property("numericDisplayValue")) == source_value

    short_field.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
    QTest.qWait(30)
    assert precise_field.property("text") == "0.123456789012345"
    assert float(precise_field.property("numericDisplayValue")) == source_value
    window.close()
