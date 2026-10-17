"""Actual settings QML: ordinary MCP rows, pointer, keyboard, compact layout."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")

from PySide6.QtCore import QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.ui import settings
from coat_sim.ui.theme_service import ThemeService

ROOT = Path(__file__).resolve().parents[1]


def find(root: QQuickWindow, name: str) -> QQuickItem:
    found = root.findChild(QQuickItem, name)
    if isinstance(found, QQuickItem):
        return found
    pending = [root.contentItem()]
    while pending:
        item = pending.pop()
        if item.objectName() == name:
            return item
        pending.extend(item.childItems())
    raise AssertionError(name)


def test_ordinary_mcp_rows_are_collapsed_and_keyboard_operable(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "MODEL_CACHE_PATH", tmp_path / "models.json")
    monkeypatch.setattr(settings, "read_api_key", lambda *_a, **_kw: None)
    monkeypatch.setattr(settings, "read_compute_token", lambda *_a, **_kw: None)
    settings.apply_font_settings(settings.normalized_ui_settings(), app)
    bridge = settings.SettingsBridge()
    bridge.setAssistantConfig(False, "", "")
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda errors: warnings.extend(str(error) for error in errors))
    engine.rootContext().setContextProperty("bridge", bridge)
    theme = ThemeService.instance()
    theme.attach_engine(engine)
    engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SettingsWindow.qml")))
    assert engine.rootObjects(), "\n".join(warnings)
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow)
    try:
        window.resize(560, 600)
        window.showNormal()
        QTest.qWait(80)
        find(window, "settingsAdvancedCategory").setProperty("expanded", True)
        QTest.qWait(100)
        assert window.property("mcpExpandedId") == ""
        for kind in ("zhihu", "openalex", "crossref"):
            assert not find(window, "mcpDetails_preset_" + kind).isVisible()
        scroll = find(window, "settingsScroll")
        flick = scroll.property("contentItem")
        assert isinstance(flick, QQuickItem)
        def reveal(item: QQuickItem) -> None:
            origin = item.mapToScene(QPointF(0, 0))
            offset = float(flick.property("contentY")) + origin.y() - window.height() * .4
            maximum = float(flick.property("contentHeight")) - flick.height()
            flick.setProperty("contentY", max(0, min(maximum, offset)))
            QTest.qWait(40)
        def click(item: QQuickItem) -> None:
            reveal(item)
            point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(round(point.x()), round(point.y())))
            QTest.qWait(40)
        name = find(window, "mcpName_preset_zhihu")
        switch = find(window, "mcpEnabled_preset_zhihu")
        assert name.height() > 20 and switch.height() > 20
        interface = QAccessible.queryAccessibleInterface(name)
        assert interface is not None
        assert interface.text(QAccessible.Text.Name) == "知乎搜索"
        click(switch)
        assert window.property("mcpExpandedId") == ""
        assert next(row for row in cast(list[dict[str, Any]], bridge.property("mcpServers"))
                    if row["id"] == "preset_zhihu")["enabled"]
        click(find(window, "mcpName_preset_zhihu"))
        assert find(window, "mcpDetails_preset_zhihu").isVisible()
        name = find(window, "mcpName_preset_zhihu")
        reveal(name)
        name.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Return)
        QTest.qWait(30)
        assert window.property("mcpExpandedId") == ""
        QTest.keyClick(window, Qt.Key.Key_Space)
        QTest.qWait(30)
        assert window.property("mcpExpandedId") == "preset_zhihu"
        QTest.keyClick(window, Qt.Key.Key_Space)
        QTest.qWait(30)
        assert window.property("mcpExpandedId") == ""
        QTest.keyClick(window, Qt.Key.Key_Tab)
        QTest.qWait(20)
        assert find(window, "mcpEnabled_preset_zhihu").hasActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Space)
        QTest.qWait(30)
        assert window.property("mcpExpandedId") == ""
        assert not next(row for row in cast(list[dict[str, Any]], bridge.property("mcpServers"))
                    if row["id"] == "preset_zhihu")["enabled"]
        # At the minimum window width, names and switches do not overlap or
        # extend beyond the visible scroll viewport; opening preserves values.
        for width in (560, 960):
            window.resize(width, 600)
            QTest.qWait(50)
            for kind in ("zhihu", "openalex", "crossref"):
                name = find(window, "mcpName_preset_" + kind)
                switch = find(window, "mcpEnabled_preset_" + kind)
                left = name.mapToScene(QPointF(0, 0)).x()
                right = switch.mapToScene(QPointF(switch.width(), 0)).x()
                assert left >= 0 and right <= window.width() + 1
                assert name.mapToScene(QPointF(name.width(), 0)).x() <= switch.mapToScene(QPointF(0, 0)).x()
            reveal(find(window, "mcpName_preset_zhihu"))
            assert window.grabWindow().save(str(tmp_path / f"ordinary-mcp-rows-{width}.png"))
            if width == 560:
                click(find(window, "mcpName_preset_zhihu"))
                assert find(window, "mcpDetails_preset_zhihu").isVisible()
                assert window.grabWindow().save(str(tmp_path / "ordinary-mcp-expanded-560.png"))
                click(find(window, "mcpName_preset_zhihu"))
        image = window.grabWindow()
        assert not image.isNull() and image.save(str(tmp_path / "ordinary-mcp-rows.png"))
        assert not any("Error:" in message or "ReferenceError" in message for message in warnings), warnings
    finally:
        window.close()
        bridge._shutdown_mcp()
        bridge.closeModelProbe()
        bridge.closeAssistantSettingsBootstrap()
        engine.deleteLater()
        app.processEvents()
