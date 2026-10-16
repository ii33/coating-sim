"""Shared native-title and custom-chrome project-name composition."""
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQml import QQmlComponent, QQmlEngine

from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot


@pytest.fixture
def app_window() -> Iterator[QQuickWindow]:
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {},
                              parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    path = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/AppWindow.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    assert not component.isError(), component.errorString()
    root = component.createWithInitialProperties({
        "titleBase": "膜系设计 *",
        "captionBase": "膜系设计 *",
        "projectNameForTitle": "工程甲",
    })
    assert isinstance(root, QQuickWindow), component.errorString()
    try:
        yield root
    finally:
        root.close()
        root.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_project_name_is_shared_by_native_title_and_window_chrome(app_window):
    caption = app_window.findChild(QQuickItem, "windowChromeCaption")
    assert caption is not None
    assert app_window.property("title") == "膜系设计 * — 工程甲"
    assert app_window.property("caption") == "膜系设计 * — 工程甲"
    assert caption.property("text") == "膜系设计 * — 工程甲"


def test_empty_project_name_and_existing_suffix_do_not_duplicate(app_window):
    app_window.setProperty("projectNameForTitle", "")
    assert app_window.property("title") == "膜系设计 *"
    assert app_window.property("caption") == "膜系设计 *"

    app_window.setProperty("projectNameForTitle", "工程甲")
    app_window.setProperty("captionBase", "机器结构 — 工程甲")
    assert app_window.property("caption") == "机器结构 — 工程甲"


def test_global_window_caption_can_remain_empty(app_window):
    app_window.setProperty("titleBase", "行星盘沉积仿真")
    app_window.setProperty("captionBase", "")
    app_window.setProperty("projectNameForTitle", "")
    assert app_window.property("title") == "行星盘沉积仿真"
    assert app_window.property("caption") == ""
