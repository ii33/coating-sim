"""Rendered theme and input regression coverage for film-layer dataset menus."""

from pathlib import Path
import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_scoped_resource_libraries import qml


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.qt, pytest.mark.contract]


def contrast(first, second):
    def luminance(value):
        color = QColor(value)
        channels = [color.redF(), color.greenF(), color.blueF()]
        channels = [
            channel / 12.92 if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return sum(channel * weight for channel, weight in zip(
            channels, (0.2126, 0.7152, 0.0722), strict=True
        ))

    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


def _check_row_colors(row, tokens, foreground, background):
    label = row.property("contentItem")
    surface = row.property("background")
    assert isinstance(label, QQuickItem)
    assert isinstance(surface, QQuickItem)
    text_color = label.property("color").name().lower()
    background_color = surface.property("color").name().lower()
    assert text_color == tokens[foreground].lower()
    assert background_color == tokens[background].lower()
    assert contrast(text_color, background_color) >= (
        2.0 if foreground == "colorDisabledText" else 4.5
    )


def _check_pixels(window, row, foreground, background):
    render_window = row.window() or window
    image = render_window.grabWindow()
    assert not image.isNull()
    scale = image.devicePixelRatio()

    surface = row.property("background")
    sample = surface.mapToScene(QPointF(8, 2)).toPoint()
    actual_background = image.pixelColor(
        round(sample.x() * scale), round(sample.y() * scale)
    ).name().lower()
    assert actual_background == QColor(background).name().lower()

    label = row.property("contentItem")
    origin = label.mapToScene(QPointF(0, 0)).toPoint()
    x0 = max(0, round(origin.x() * scale))
    y0 = max(0, round(origin.y() * scale))
    x1 = min(image.width(), x0 + round(label.width() * scale))
    y1 = min(image.height(), y0 + round(label.height() * scale))
    expected = QColor(foreground).name().lower()
    assert any(
        image.pixelColor(x, y).name().lower() == expected
        for y in range(y0, y1)
        for x in range(x0, x1)
    ), f"rendered text pixels did not contain {expected}"


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("window_width", [360, 760])
def test_film_layer_dataset_menu_keeps_state_colors_paired_and_tooltip_transient(
    mode, window_width
):
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    tokens = build_theme_snapshot(mode)
    theme = ThemeEngineBridge(
        tokens, 0, mode, mode, "qa", "QA", {}, parent=engine
    )
    engine.rootContext().setContextProperty("Theme", theme)

    source = f"""import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as UI

Window {{
    id: host
    width: {window_width}
    height: 320
    visible: true
    color: Theme.snapshot.colorWindow
    palette.highlight: Theme.snapshot.colorAccent
    palette.highlightedText: Theme.snapshot.colorTextOnAccent

    QtObject {{
        id: catalog
        objectName: "dropdownTestCatalog"
        property var filmOptions: [{{id: "material-zns", name: "ZnS"}}]
        property var opticalSets: [
            {{
                id: "optical-current",
                name: "Visible reference",
                wavelength_min: 400,
                wavelength_max: 700,
                wavelength_unit: "nm",
                value_range_text: "n 1.4–1.6; k 0–0.01",
                notes: "Current data source and process conditions"
            }},
            {{
                id: "optical-hover",
                name: "Extended reference",
                wavelength_min: 700,
                wavelength_max: 12000,
                wavelength_unit: "nm",
                value_range_text: "n 2.1–2.6; k 0–0.03",
                notes: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            }},
            {{
                id: "optical-third",
                name: "Third reference",
                wavelength_min: 450,
                wavelength_max: 900,
                wavelength_unit: "nm",
                value_range_text: "n 1.5–2.0; k 0–0.02",
                notes: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            }}
        ]
        property string lastField: ""
        property string lastValue: ""
        function opticalSetsForFilm(materialId) {{ return opticalSets }}
        function setLayerValue(side, stableId, field, value) {{
            lastField = field
            lastValue = String(value)
            return true
        }}
        function markEditingDirty() {{}}
    }}

    UI.FilmDesignLayerRow {{
        id: layer
        objectName: "dropdownTestLayer"
        x: 8
        y: 12
        width: host.width - 16
        controller: catalog
        columnMetrics: ({{
            horizontalPadding: 4,
            columnGap: 4,
            indexWidth: 28,
            materialWidth: 108,
            datasetWidth: Math.max(96, Math.min(220, host.width - 250)),
            qwWidth: 76,
            nmWidth: 76,
            insertWidth: 28,
            deleteWidth: 28
        }})
        index: 0
        stableId: "dropdown-test"
        symbol: "A"
        materialId: "material-zns"
        opticalSetId: "optical-current"
        physicalNmValue: 100.0
        physicalNmText: "100"
        qwText: "1"
        derivedReason: ""
        note: ""
    }}
}}
"""
    component = QQmlComponent(engine)
    base_url = QUrl.fromLocalFile(
        str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")
    )
    component.setData(source.encode("utf-8"), base_url)
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errorString()
    try:
        window.show()
        window.requestActivate()
        QTest.qWait(40)
        selector = window.findChild(QQuickItem, "filmLayerOpticalSet_dropdown-test")
        assert selector is not None

        selector.forceActiveFocus()
        QTest.qWait(20)
        assert selector.property("activeFocus")
        assert qml(engine, selector, "background.border.color").name().lower() == (
            str(tokens["colorFocusRing"]).lower()
        )

        QTest.keyClick(window, Qt.Key.Key_Space)
        QTest.qWait(35)
        assert qml(engine, selector, "popup.visible")
        for _ in range(3):
            if selector.property("highlightedIndex") == 1:
                break
            QTest.keyClick(window, Qt.Key.Key_Down)
            QTest.qWait(20)
        assert selector.property("highlightedIndex") == 1

        highlighted = qml(engine, selector, "popup.contentItem.itemAtIndex(1)")
        selected = qml(engine, selector, "popup.contentItem.itemAtIndex(0)")
        disabled_candidate = qml(engine, selector, "popup.contentItem.itemAtIndex(2)")
        assert highlighted and selected and disabled_candidate
        popup_width = qml(engine, selector, "popup.width")
        popup_x = highlighted.mapToScene(QPointF(0, 0)).x()
        assert popup_width <= window_width
        assert popup_x >= 0
        assert popup_x + popup_width <= window_width
        _check_row_colors(
            highlighted, tokens, "colorTextOnAccent", "colorAccent"
        )
        _check_pixels(
            window, highlighted, tokens["colorTextOnAccent"], tokens["colorAccent"]
        )

        assert selected.property("currentSelection")
        assert not selected.property("highlighted")
        _check_row_colors(selected, tokens, "colorText", "colorSelection")

        other_mode = "dark" if mode == "light" else "light"
        other_tokens = build_theme_snapshot(other_mode)
        theme._replace_snapshot(
            other_tokens, 1, other_mode, other_mode, "qa", "QA", {}
        )
        QTest.qWait(25)
        _check_row_colors(
            highlighted, other_tokens, "colorTextOnAccent", "colorAccent"
        )
        _check_pixels(
            window,
            highlighted,
            other_tokens["colorTextOnAccent"],
            other_tokens["colorAccent"],
        )
        theme._replace_snapshot(tokens, 2, mode, mode, "qa", "QA", {})
        QTest.qWait(25)
        _check_row_colors(
            highlighted, tokens, "colorTextOnAccent", "colorAccent"
        )

        point = disabled_candidate.mapToScene(
            QPointF(disabled_candidate.width() / 2, disabled_candidate.height() / 2)
        ).toPoint()
        disabled_candidate.setEnabled(False)
        QTest.qWait(20)
        _check_row_colors(
            disabled_candidate, tokens, "colorDisabledText", "colorDisabledSurface"
        )
        QTest.mouseMove(window, QPoint(1, 1))
        QTest.mouseMove(window, point)
        QTest.qWait(25)
        assert not disabled_candidate.property("accentState")
        QTest.mouseClick(
            window,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            point,
        )
        QTest.qWait(25)
        assert selector.property("currentIndex") == 0

        disabled_candidate.setEnabled(True)
        QTest.qWait(20)
        QTest.mouseMove(window, QPoint(1, 1))
        QTest.mouseMove(window, point)
        QTest.qWait(700)
        assert disabled_candidate.property("hovered")
        _check_row_colors(
            disabled_candidate, tokens, "colorTextOnAccent", "colorAccent"
        )
        tooltip = disabled_candidate.findChild(QObject, "filmOpticalSetTooltip_optical-third")
        assert tooltip is not None and tooltip.property("visible")
        assert tooltip.property("width") <= 360
        _check_row_colors(tooltip, tokens, "colorText", "colorSurfaceRaised")
        assert len(str(tooltip.property("text")).splitlines()[-1]) <= 32

        theme._replace_snapshot(
            other_tokens, 3, other_mode, other_mode, "qa", "QA", {}
        )
        QTest.qWait(25)
        _check_row_colors(tooltip, other_tokens, "colorText", "colorSurfaceRaised")
        theme._replace_snapshot(tokens, 4, mode, mode, "qa", "QA", {})
        QTest.qWait(25)

        QTest.mouseClick(
            window,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            point,
        )
        QTest.qWait(40)
        assert selector.property("currentIndex") == 2
        catalog = window.findChild(QObject, "dropdownTestCatalog")
        assert catalog is not None
        assert catalog.property("lastField") == "optical_set_id"
        assert catalog.property("lastValue") == "optical-third"
        assert not qml(engine, selector, "popup.visible")
        assert window.findChild(QObject, "filmOpticalSetTooltip_optical-third") is None

    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        app.processEvents()

