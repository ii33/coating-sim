"""Own the assistant's independent desktop windows and their bridge."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess

from PySide6.QtCore import QObject, QPoint, QSize, QTimer, QUrl, Slot, Qt
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlContext

from coat_sim.ui.icons import window_icon
from coat_sim.ui.settings import SETTINGS_PATH

UI_DIR = Path(__file__).resolve().parent
QML_DIR = UI_DIR / "qml"
PORTRAIT = UI_DIR / "assets" / "assistant.png"


def _default_bridge_factory(parent: QObject) -> QObject:
    # Resolve AssistantBridge only at construction time to keep the facade
    # importable from assistant.py without a module import cycle. This lookup
    # also preserves assistant.AssistantBridge as the compatibility patch seam.
    from coat_sim.ui import assistant
    return assistant.AssistantBridge(parent)


class AssistantController(QObject):
    """Own two independent top-level windows outside the launcher QML tree."""

    def __init__(self, engine: QQmlApplicationEngine,
                 open_settings_callback: Callable[[], None] | None = None, *,
                 bridge_factory: Callable[[QObject], QObject] | None = None) -> None:
        super().__init__(engine)
        from coat_sim.ui.preview_cursor import register_preview_cursor
        register_preview_cursor()
        self.engine = engine
        self.open_settings_callback = open_settings_callback
        self.bridge = (bridge_factory or _default_bridge_factory)(self)
        self.bridge.openRequested.connect(self.show_chat)
        self.bridge.toggleRequested.connect(self.toggle_chat)
        self.bridge.settingsRequested.connect(self.show_settings)
        self.bridge.closeRequested.connect(self.hide_windows)
        self._context: QQmlContext | None = None
        self._bubble_component: QQmlComponent | None = None
        self._chat_component: QQmlComponent | None = None
        self._bubble = None
        self._chat = None
        self._chat_screen = None
        self._chat_has_been_shown = False
        self._settings_stamp = self._configuration_stamp()
        self._configuration_timer = QTimer(self)
        self._configuration_timer.setInterval(1000)
        self._configuration_timer.timeout.connect(self._poll_configuration)
        self._configuration_timer.start()
        if self.bridge.enabled:
            self._ensure_windows()
            self._bubble.show()

    @staticmethod
    def _configuration_stamp() -> int:
        try:
            return SETTINGS_PATH.stat().st_mtime_ns
        except OSError:
            return -1

    def _poll_configuration(self) -> None:
        stamp = self._configuration_stamp()
        if stamp != self._settings_stamp:
            self._settings_stamp = stamp
            self.reload_configuration()

    def _create_window(self, name: str, context: QQmlContext) -> tuple[QQmlComponent, object]:
        component = QQmlComponent(self.engine, QUrl.fromLocalFile(str(QML_DIR / name)))
        if component.isError():
            raise RuntimeError("\n".join(str(item) for item in component.errors()))
        window = component.create(context)
        if window is None:
            raise RuntimeError("\n".join(str(item) for item in component.errors()))
        return component, window

    def _ensure_windows(self) -> None:
        if self._bubble is not None:
            return
        if not PORTRAIT.is_file():
            raise RuntimeError(f"缺少看板娘图片：{PORTRAIT}")
        context = QQmlContext(self.engine.rootContext())
        context.setContextProperty("assistant", self.bridge)
        bubble_component, bubble = self._create_window("AssistantBubble.qml", context)
        chat_component, chat = self._create_window("AssistantChatWindow.qml", context)
        icon = window_icon("launcher")
        bubble.setIcon(icon)
        chat.setIcon(icon)
        self._context = context
        self._bubble_component, self._chat_component = bubble_component, chat_component
        self._bubble, self._chat = bubble, chat
        chat.screenChanged.connect(self._chat_screen_changed)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            bubble.setPosition(QPoint(area.right() - bubble.width() - 24,
                                      area.bottom() - bubble.height() - 24))
        self._chat_screen_changed(chat.screen() or screen)
        self._fit_chat_to_screen(center=True)

    def _chat_screen_changed(self, screen) -> None:
        if screen is not self._chat_screen:
            if self._chat_screen is not None:
                try:
                    self._chat_screen.availableGeometryChanged.disconnect(self._screen_geometry_changed)
                except (RuntimeError, TypeError):
                    pass
            self._chat_screen = screen
            if screen is not None:
                screen.availableGeometryChanged.connect(self._screen_geometry_changed)
        self._fit_chat_to_screen()

    def _screen_geometry_changed(self, _area) -> None:
        self._fit_chat_to_screen()

    def _fit_chat_to_screen(self, *, center: bool = False, screen=None) -> None:
        """Qt provides logical pixels here, including desktop work-area insets."""
        chat = self._chat
        screen = screen or (chat.screen() if chat is not None else None) or QGuiApplication.primaryScreen()
        if chat is None or screen is None:
            return
        area = screen.availableGeometry()
        if area.width() < 1 or area.height() < 1:
            return
        margin = min(16, max(0, (min(area.width(), area.height()) - 1) // 2))
        available_width = max(1, area.width() - margin * 2)
        available_height = max(1, area.height() - margin * 2)
        chat.setMinimumSize(QSize(min(560, available_width), min(420, available_height)))
        if chat.visibility() in (QWindow.Visibility.Maximized, QWindow.Visibility.FullScreen):
            return
        width = min(max(chat.width(), chat.minimumWidth()), available_width)
        height = min(max(chat.height(), chat.minimumHeight()), available_height)
        if chat.width() != width or chat.height() != height:
            chat.resize(width, height)
        if center:
            x = area.left() + (area.width() - width) // 2
            y = area.top() + (area.height() - height) // 2
        else:
            x = max(area.left() + margin, min(chat.x(), area.right() - margin - width + 1))
            y = max(area.top() + margin, min(chat.y(), area.bottom() - margin - height + 1))
        chat.setPosition(QPoint(x, y))

    @Slot()
    def reload_configuration(self) -> None:
        self._settings_stamp = self._configuration_stamp()
        self.bridge.reload_configuration()
        if self.bridge.enabled:
            try:
                self._ensure_windows()
            except RuntimeError:
                return
            screen = self._bubble.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self._bubble.setPosition(QPoint(
                    max(area.left(), min(self._bubble.x(), area.right() - self._bubble.width() + 1)),
                    max(area.top(), min(self._bubble.y(), area.bottom() - self._bubble.height() + 1))))
            self._bubble.show()
        else:
            if self._bubble is not None:
                self._bubble.hide()
            if self._chat is not None:
                self._chat.hide()

    @Slot()
    def show_chat(self) -> None:
        if not self.bridge.enabled:
            return
        self._ensure_windows()
        self._chat.setFlag(Qt.WindowStaysOnTopHint, False)
        if not self._chat_has_been_shown:
            screen = self._bubble.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                self._chat.setScreen(screen)
            self._fit_chat_to_screen(center=True, screen=screen)
            self._chat_has_been_shown = True
        else:
            self._fit_chat_to_screen()
        self._chat.setIcon(window_icon("launcher"))
        self._present_chat_window()

    def _present_chat_window(self) -> None:
        """Restore, show, raise, and request activation of the chat window."""
        if self._chat is None:
            return
        if self._chat.visibility() == QWindow.Visibility.Minimized:
            self._chat.showNormal()
        else:
            self._chat.show()
        self._chat.raise_()
        self._chat.requestActivate()

    @Slot()
    def toggle_chat(self) -> None:
        if not self.bridge.enabled:
            return
        self._ensure_windows()
        if (self._chat.isVisible()
                and self._chat.visibility() != QWindow.Visibility.Minimized
                and self._chat.isActive()):
            self._chat.hide()
        else:
            self.show_chat()

    @Slot()
    def show_settings(self) -> None:
        if callable(self.open_settings_callback):
            self.open_settings_callback()
            return
        # Independent settings entry shares the same configuration as the launcher.
        root = UI_DIR.parents[1]
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        from coat_sim.process_entry import module_command
        subprocess.Popen(module_command("coat_sim.ui.settings"), cwd=str(root), **kwargs)

    @Slot()
    def hide_windows(self) -> None:
        if self._bubble is not None:
            self._bubble.hide()
        if self._chat is not None:
            self._chat.hide()

    def shutdown(self) -> None:
        self._configuration_timer.stop()
        if self._chat_screen is not None:
            try:
                self._chat_screen.availableGeometryChanged.disconnect(self._screen_geometry_changed)
            except (RuntimeError, TypeError):
                pass
            self._chat_screen = None
        self.bridge.shutdown()
        self.hide_windows()
        for window in (self._bubble, self._chat):
            if window is not None:
                window.deleteLater()
