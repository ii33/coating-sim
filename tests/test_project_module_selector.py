"""Real shared-menu input remains scoped to the presenting project's facade."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, Property, QCoreApplication, QEvent, QPointF, Qt, QUrl, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression, QJSValue
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.project_modules import project_module_options
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot


class Navigation(QObject):
    def __init__(self, name: str, modules: list[dict[str, str]] | None = None):
        super().__init__()
        self.name = name
        self.calls: list[str] = []
        self.modules = project_module_options() if modules is None else modules

    @Property(str, constant=True)
    def projectName(self) -> str:
        return self.name

    @Property(list, constant=True)
    def projectModules(self) -> list[dict[str, str]]:
        return self.modules

    @Slot(str, result=str)
    def openProjectModule(self, key: str) -> str:
        self.calls.append(key)
        return key


def events() -> None:
    QCoreApplication.processEvents()
    QTest.qWait(25)
    QCoreApplication.processEvents()


@dataclass
class SelectorRuntime:
    engine: QQmlEngine
    component: QQmlComponent
    theme: ThemeEngineBridge
    window: QQuickWindow
    roots: list[QQuickItem]
    owners: list[Navigation]

    def create(self, key: str, *, name: str = "工程甲", modules: list[dict[str, str]] | None = None,
               selector_name: str = "projectModuleSelector") -> tuple[QQuickItem, Navigation]:
        owner = Navigation(name, modules)
        self.owners.append(owner)
        root = self.component.createWithInitialProperties({
            "navigation": owner, "currentModuleKey": key, "selectorObjectName": selector_name,
        })
        assert isinstance(root, QQuickItem), self.component.errorString()
        root.setParent(self.window.contentItem())
        root.setParentItem(self.window.contentItem())
        root.setPosition(QPointF(20, 20 + len(self.roots) * 65))
        self.roots.append(root)
        events()
        return root, owner

    def qml(self, scope: QObject, source: str) -> Any:
        expression = QQmlExpression(self.engine.rootContext(), scope, source)
        result = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        value = result[0] if isinstance(result, tuple) else result
        return value.toVariant() if isinstance(value, QJSValue) else value

    def item(self, scope: QObject, name: str) -> QQuickItem:
        item = scope.findChild(QQuickItem, name)
        if item is None:
            pending = [self.window.contentItem()]
            while pending:
                parent = pending.pop()
                for child in parent.childItems():
                    if child.objectName() == name:
                        return child
                    pending.append(child)
        assert item is not None, name
        return item

    def menu(self, root: QQuickItem, name: str = "projectModuleMenu") -> QObject:
        menu = root.findChild(QObject, name)
        assert menu is not None, name
        return menu

    def click(self, item: QQuickItem) -> None:
        assert item.isVisible() and item.width() > 0 and item.height() > 0, item.objectName()
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         point.toPoint())
        events()

    def key(self, key: Qt.Key) -> None:
        QTest.keyClick(self.window, key)
        events()

    def category_item(self, menu: QObject, index: int) -> QQuickItem:
        item = self.qml(menu, f"itemAt({index})")
        assert isinstance(item, QQuickItem)
        return item


@pytest.fixture
def runtime() -> Iterator[SelectorRuntime]:
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    path = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/ProjectModuleSelector.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    assert not component.isError(), component.errorString()
    window = QQuickWindow()
    window.resize(640, 420)
    window.show()
    window.requestActivate()
    result = SelectorRuntime(engine, component, theme, window, [], [])
    events()
    try:
        yield result
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("module", project_module_options(), ids=lambda module: module["key"])
def test_collapsed_selector_displays_its_current_window(runtime: SelectorRuntime, module: dict[str, str]) -> None:
    root, owner = runtime.create(module["key"])
    button = runtime.item(root, "projectModuleSelector")
    assert button.property("text") == module["name"]
    assert root.findChild(QObject, "projectNavigationName") is None
    assert button.x() == 0
    assert root.property("implicitWidth") == 140
    assert owner.calls == []
    root.setProperty("currentModuleKey", "missing_module")
    assert button.property("text")
    assert button.property("text") not in {row["name"] for row in project_module_options()}
    assert owner.calls == []
    root.setProperty("navigation", None)
    assert not root.property("visible")


def test_three_submenus_preserve_all_leaves_and_separate_tools(runtime: SelectorRuntime) -> None:
    root, owner = runtime.create("ray_trace")
    top = runtime.menu(root)
    assert [runtime.category_item(top, index).property("text") for index in range(3)] == ["膜系", "沉积", "仿真"]
    expected = {
        "projectModuleFilmMenu": ["film_design", "film_optimization"],
        "projectModuleDepositionMenu": ["process", "machine", "source", "mask", "substrate"],
        "projectModuleSimulationMenu": ["film_simulation", "main", "ray_trace", "film_appearance"],
    }
    for name, keys in expected.items():
        menu = runtime.menu(root, name)
        assert menu.property("count") == len(keys)
        assert [runtime.category_item(menu, index).objectName() for index in range(len(keys))] == [
            "projectModuleAction_" + key for key in keys
        ]
    assert top.property("count") == 7  # Three categories, separator, assets, queue and BBS.
    assert runtime.category_item(top, 4).objectName() == "projectModuleAction_project_resources"
    assert runtime.category_item(top, 5).objectName() == "projectModuleAction_compute_queue"
    assert runtime.category_item(top, 6).objectName() == "projectModuleAction_project_bbs"
    assert not any(option["key"] == "materials" for option in runtime.qml(root, "toolModules()"))
    assert runtime.item(root, "projectModuleAction_ray_trace").property("checked")
    assert not runtime.item(root, "projectModuleAction_main").property("checked")
    assert owner.calls == []


def test_mouse_leaf_dispatch_and_cancel_are_project_local(runtime: SelectorRuntime) -> None:
    first, left = runtime.create("film_design")
    second, right = runtime.create("ray_trace", name="工程乙", selector_name="secondSelector")
    left_button = runtime.item(first, "projectModuleSelector")
    runtime.click(left_button)
    top = runtime.menu(first)
    assert top.property("visible")
    runtime.click(runtime.category_item(top, 1))
    submenu = runtime.menu(first, "projectModuleDepositionMenu")
    assert submenu.property("visible")
    assert left.calls == right.calls == []
    runtime.click(runtime.item(first, "projectModuleAction_process"))
    assert left.calls == ["process"] and right.calls == []
    assert not top.property("visible")
    assert left_button.property("text") == "膜系设计"
    assert runtime.item(first, "projectModuleAction_film_design").property("checked")
    assert not runtime.item(first, "projectModuleAction_process").property("checked")
    runtime.click(runtime.item(second, "secondSelector"))
    runtime.key(Qt.Key.Key_Escape)
    assert right.calls == [] and left.calls == ["process"]
    assert not runtime.menu(second).property("visible")
    assert runtime.item(second, "secondSelector").property("activeFocus")
    assert runtime.item(second, "secondSelector").property("text") == "光线追迹"


def test_keyboard_enters_leaves_and_repeats_current_leaf(runtime: SelectorRuntime) -> None:
    root, owner = runtime.create("film_design")
    button = runtime.item(root, "projectModuleSelector")
    button.forceActiveFocus()
    runtime.key(Qt.Key.Key_Space)
    top = runtime.menu(root)
    assert top.property("visible")
    if top.property("currentIndex") == -1:
        runtime.key(Qt.Key.Key_Down)
    runtime.key(Qt.Key.Key_Right)
    film = runtime.menu(root, "projectModuleFilmMenu")
    assert film.property("visible")
    runtime.key(Qt.Key.Key_Left)
    assert not film.property("visible") and top.property("visible")
    assert owner.calls == []
    runtime.key(Qt.Key.Key_Right)
    assert film.property("visible")
    if film.property("currentIndex") == -1:
        runtime.key(Qt.Key.Key_Down)
    runtime.key(Qt.Key.Key_Return)
    assert owner.calls == ["film_design"]
    assert not top.property("visible")
    assert button.property("text") == "膜系设计"
    assert runtime.item(root, "projectModuleAction_film_design").property("checked")
    button.forceActiveFocus()
    runtime.key(Qt.Key.Key_Down)
    assert top.property("visible")
    runtime.key(Qt.Key.Key_Escape)
    assert owner.calls == ["film_design"] and button.property("activeFocus")


def test_unknown_modules_remain_reachable_without_a_fourth_category(runtime: SelectorRuntime) -> None:
    modules = project_module_options() + [{"key": "future_tool", "name": "未来工具"}]
    root, owner = runtime.create("future_tool", modules=modules)
    assert runtime.item(root, "projectModuleSelector").property("text") == "未来工具"
    top = runtime.menu(root)
    assert top.property("count") == 8
    runtime.click(runtime.item(root, "projectModuleSelector"))
    runtime.click(runtime.item(root, "projectModuleAction_future_tool"))
    assert owner.calls == ["future_tool"]


def test_empty_navigation_is_safe_and_never_dispatches(runtime: SelectorRuntime) -> None:
    root, owner = runtime.create("unknown", modules=[])
    button = runtime.item(root, "projectModuleSelector")
    assert button.property("text")
    button.forceActiveFocus()
    runtime.key(Qt.Key.Key_Return)
    runtime.key(Qt.Key.Key_Escape)
    assert owner.calls == []


@pytest.mark.parametrize("mode", ["light", "dark", "high-contrast-dark"])
def test_theme_and_narrow_window_keep_the_same_navigation(runtime: SelectorRuntime, mode: str) -> None:
    snapshot = build_theme_snapshot(mode, font_scale=1.25, density_scale=1.25)
    runtime.theme._replace_snapshot(snapshot, 1, mode, mode, "qa", "QA", {})
    runtime.window.resize(360, 420)
    root, owner = runtime.create("ray_trace", name="很长的工程名称用于检查窄窗口")
    button = runtime.item(root, "projectModuleSelector")
    assert button.property("text") == "光线追迹"
    assert root.x() + root.width() <= runtime.window.width()
    for width in (360, 960, 360):
        runtime.window.resize(width, 420)
        events()
        runtime.click(button)
        top = runtime.menu(root)
        assert top.property("visible")
        runtime.click(runtime.category_item(top, 2))
        submenu = runtime.menu(root, "projectModuleSimulationMenu")
        assert submenu.property("visible")
        submenu.setProperty("currentIndex", 2)
        events()
        current = runtime.item(root, "projectModuleAction_ray_trace")
        assert current.property("highlighted")
        label = current.property("contentItem")
        surface = current.property("background")
        assert isinstance(label, QQuickItem) and isinstance(surface, QQuickItem)
        text_items = [child for child in label.childItems()
                      if child.property("text") == current.property("text")]
        assert len(text_items) == 1
        assert text_items[0].property("color").name() == str(snapshot["colorTextOnAccent"]).lower()
        assert surface.property("color").name() == str(snapshot["colorAccent"]).lower()
        runtime.click(current)
        assert not top.property("visible")
    assert owner.calls == ["ray_trace"] * 3
    assert button.property("text") == "光线追迹"
