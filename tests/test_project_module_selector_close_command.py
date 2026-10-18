"""The shared project menu routes closure to its current project owner."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Property, QCoreApplication, QEvent, QPointF, Qt, QUrl, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression, QJSValue
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.project_modules import project_module_options
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot


class OwnerNavigation(QObject):
    def __init__(self, name: str = "工程甲"):
        super().__init__()
        self.name = name
        self.module_calls: list[str] = []
        self.close_calls = 0

    @Property(str, constant=True)
    def projectName(self) -> str:
        return self.name

    @Property(list, constant=True)
    def projectModules(self) -> list[dict[str, str]]:
        return project_module_options()

    @Slot(str, result=str)
    def openProjectModule(self, key: str) -> str:
        self.module_calls.append(key)
        return key

    @Slot(result=bool)
    def closeProject(self) -> bool:
        self.close_calls += 1
        return True


def _process_events() -> None:
    QCoreApplication.processEvents()
    QTest.qWait(25)
    QCoreApplication.processEvents()


def _item(root: QQuickItem, name: str) -> QQuickItem:
    found = root.findChild(QQuickItem, name)
    if found is not None:
        return found
    pending = [root]
    while pending:
        parent = pending.pop()
        for child in parent.childItems():
            if child.objectName() == name:
                return child
            pending.append(child)
    raise AssertionError(f"QML item not found: {name}")


def _qml_value(engine: QQmlEngine, scope: QObject, source: str):
    expression = QQmlExpression(engine.rootContext(), scope, source)
    result = expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    value = result[0] if isinstance(result, tuple) else result
    return value.toVariant() if isinstance(value, QJSValue) else value


@pytest.fixture
def shared_menu() -> Iterator[tuple[QQmlEngine, QQuickWindow, QQuickItem, OwnerNavigation]]:
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {},
                              parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    qml_path = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/ProjectModuleSelector.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml_path)))
    assert not component.isError(), component.errorString()

    navigation = OwnerNavigation()
    root = component.createWithInitialProperties({
        "navigation": navigation,
        "currentModuleKey": "ray_trace",
    })
    assert isinstance(root, QQuickItem), component.errorString()
    window = QQuickWindow()
    window.resize(640, 420)
    root.setParent(window.contentItem())
    root.setParentItem(window.contentItem())
    window.show()
    window.requestActivate()
    _process_events()
    try:
        yield engine, window, root, navigation
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("replace_owner", [False, True], ids=["initial-owner", "rebound-owner"])
def test_close_project_is_a_separate_bottom_command_and_dispatches_to_current_owner(shared_menu, replace_owner):
    engine, window, root, original_navigation = shared_menu
    navigation = OwnerNavigation("工程乙") if replace_owner else original_navigation
    if replace_owner:
        root.setProperty("navigation", navigation)
        _process_events()
    assert root.findChild(QObject, "projectNavigationName") is None
    button = _item(root, "projectModuleSelector")
    assert button.x() == 0
    assert root.property("canCloseProject") is True
    initial_label = button.property("text")
    assert initial_label == "光线追迹"

    point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point.toPoint())
    _process_events()

    menu = root.findChild(QObject, "projectModuleMenu")
    assert menu is not None and menu.property("visible")
    separators = root.findChild(QObject, "projectCloseSeparators")
    actions = root.findChild(QObject, "projectCloseActions")
    assert separators is not None and actions is not None
    assert (separators.property("count"), actions.property("count")) == (1, 1)
    # Materials is a retained compatibility route, not an independent menu
    # entry. Its editor is reached through the project asset library instead.
    count = menu.property("count")
    entries = [_qml_value(engine, menu, f"itemAt({index})") for index in range(count)]
    assert [entry.property("text") for entry in entries[:3]] == ["膜系", "沉积", "仿真"]
    order = [entry.objectName() for entry in entries]
    assert order[3:] == [
        "projectModuleToolsSeparator",
        "projectModuleAction_project_resources",
        "projectModuleAction_compute_queue",
        "projectModuleAction_project_bbs",
        "projectCloseSeparator",
        "projectCloseAction",
    ], order
    assert root.findChild(QObject, "projectModuleAction_materials") is None
    assert any(row["key"] == "materials" for row in navigation.property("projectModules"))
    close_separator, close_action = entries[-2:]
    assert isinstance(close_separator, QQuickItem)
    assert close_separator.objectName() == "projectCloseSeparator", order
    assert close_action is _item(root, "projectCloseAction")
    assert close_action.property("text") == "关闭工程"
    assert close_action.property("enabled")

    point = close_action.mapToScene(QPointF(close_action.width() / 2, close_action.height() / 2))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point.toPoint())
    _process_events()

    assert navigation.close_calls == 1
    assert navigation.module_calls == []
    if replace_owner:
        assert original_navigation.close_calls == 0
        assert original_navigation.module_calls == []
    assert root.property("currentModuleKey") == "ray_trace"
    assert button.property("text") == initial_label
    assert window.isVisible()
    assert not menu.property("visible")
