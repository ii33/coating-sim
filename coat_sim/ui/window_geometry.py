"""Shared first-open and restored geometry policy for top-level UI windows."""
from __future__ import annotations

from typing import Any
import weakref

from PySide6.QtCore import QEvent, QObject, QRect, QSettings, QTimer
from PySide6.QtGui import QGuiApplication, QScreen, QWindow
from PySide6.QtWidgets import QWidget


DEFAULT_WINDOW_WIDTH = 1100
DEFAULT_WINDOW_HEIGHT = 760
GEOMETRY_SETTINGS_PREFIX = "ui/window_geometry/v1"


class WindowGeometryManager(QObject):
    """Place normal windows consistently and preserve user-adjusted geometry.

    Qt window geometry and ``QScreen.availableGeometry()`` use logical pixels,
    so the 1100 x 760 default is DPI-aware without applying a second scale.
    """

    def __init__(self, parent: QObject | None = None,
                 settings: QSettings | None = None):
        super().__init__(parent)
        self._settings = (settings if settings is not None
                          else QSettings("LYX", "coat-sim"))
        self._tracked: dict[int, tuple[Any, str, QTimer]] = {}
        self._watched_screens: set[tuple[int, int]] = set()

    def prepare(self, window: Any, module_key: str, *,
                screen: QScreen | None = None,
                preserve_existing: bool = False) -> QRect:
        """Set a clamped initial rectangle and begin saving user adjustments.

        ``preserve_existing`` is for pages such as FilmDesignQuick that already
        restore their own saved rectangle before the launcher receives them.
        Such pages remain responsible for persisting their geometry.
        """
        if not isinstance(window, (QWindow, QWidget)):
            raise TypeError("window geometry requires a QWindow or QWidget")
        if bool(window.property("_coat_sim_geometry_prepared")):
            return QRect(window.geometry())

        selected_screen = screen or self._window_screen(window)
        if isinstance(window, QWindow) and selected_screen is not None:
            if window.screen() is not selected_screen:
                window.setScreen(selected_screen)
        area = QRect(selected_screen.availableGeometry()) if selected_screen else QRect()
        if area.width() <= 0 or area.height() <= 0:
            area = QRect(0, 0, DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)

        requested_min_width = max(0, int(window.minimumWidth()))
        requested_min_height = max(0, int(window.minimumHeight()))
        minimum_width = min(requested_min_width, area.width())
        minimum_height = min(requested_min_height, area.height())
        if minimum_width < requested_min_width:
            window.setMinimumWidth(minimum_width)
        if minimum_height < requested_min_height:
            window.setMinimumHeight(minimum_height)

        restored = None if preserve_existing else self._read(module_key)
        if preserve_existing:
            requested = QRect(window.geometry())
        elif restored is not None:
            requested = restored
        else:
            width = min(DEFAULT_WINDOW_WIDTH, area.width())
            height = min(DEFAULT_WINDOW_HEIGHT, area.height())
            requested = QRect(0, 0, width, height)

        width = min(area.width(), max(minimum_width, requested.width()))
        height = min(area.height(), max(minimum_height, requested.height()))
        if restored is None and not preserve_existing:
            x = area.x() + (area.width() - width) // 2
            y = area.y() + (area.height() - height) // 2
        else:
            x = min(max(requested.x(), area.x()), area.x() + area.width() - width)
            y = min(max(requested.y(), area.y()), area.y() + area.height() - height)
        rectangle = QRect(x, y, width, height)
        if QRect(window.geometry()) != rectangle:
            window.setGeometry(rectangle)
        window.setProperty("_coat_sim_geometry_prepared", True)
        window.setProperty(
            "_coat_sim_geometry_source",
            "page" if preserve_existing else "restored" if restored is not None else "default")
        if not preserve_existing:
            self._track(window, module_key)
        return QRect(rectangle)

    @staticmethod
    def _window_screen(window: Any) -> QScreen | None:
        try:
            screen = window.screen()
        except (AttributeError, RuntimeError):
            screen = None
        app = QGuiApplication.instance()
        return screen or (app.primaryScreen() if app is not None else None)

    def _settings_key(self, module_key: str) -> str:
        return f"{GEOMETRY_SETTINGS_PREFIX}/{str(module_key).strip()}"

    def _read(self, module_key: str) -> QRect | None:
        value = self._settings.value(self._settings_key(module_key))
        if isinstance(value, QRect) and value.width() > 0 and value.height() > 0:
            return QRect(value)
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                x, y, width, height = (int(item) for item in value)
                if width > 0 and height > 0:
                    return QRect(x, y, width, height)
            except (TypeError, ValueError):
                pass
        return None

    def _track(self, window: Any, module_key: str) -> None:
        identity = id(window)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(500)
        timer.timeout.connect(lambda: self._save(window, module_key))
        self._tracked[identity] = (window, module_key, timer)

        if isinstance(window, QWindow):
            for name in ("xChanged", "yChanged", "widthChanged", "heightChanged"):
                signal = getattr(window, name, None)
                if signal is not None:
                    signal.connect(lambda *_args, timer=timer: timer.start())
            window.visibleChanged.connect(
                lambda visible, w=window, key=module_key:
                self._save(w, key) if not visible else None)
            window.screenChanged.connect(
                lambda current, w=window: self._screen_changed(w, current))
            self._watch_screen(window, window.screen())
            window.installEventFilter(self)
        else:
            window.installEventFilter(self)
        window.destroyed.connect(
            lambda _obj=None, key=identity: self._forget_tracking(key))

    def _clamp_to_screen(self, window: Any, screen: QScreen | None) -> None:
        if screen is None:
            return
        area = screen.availableGeometry()
        minimum_width = min(max(0, int(window.minimumWidth())), area.width())
        minimum_height = min(max(0, int(window.minimumHeight())), area.height())
        if minimum_width < int(window.minimumWidth()):
            window.setMinimumWidth(minimum_width)
        if minimum_height < int(window.minimumHeight()):
            window.setMinimumHeight(minimum_height)
        current = window.geometry()
        width = min(area.width(), max(minimum_width, current.width()))
        height = min(area.height(), max(minimum_height, current.height()))
        x = min(max(current.x(), area.x()), area.x() + area.width() - width)
        y = min(max(current.y(), area.y()), area.y() + area.height() - height)
        rectangle = QRect(x, y, width, height)
        if QRect(current) != rectangle:
            window.setGeometry(rectangle)

    def _screen_changed(self, window: Any, screen: QScreen | None) -> None:
        self._clamp_to_screen(window, screen)
        self._watch_screen(window, screen)

    def _watch_screen(self, window: Any, screen: QScreen | None) -> None:
        if screen is None:
            return
        key = (id(window), id(screen))
        if key in self._watched_screens:
            return
        signal = getattr(screen, "availableGeometryChanged", None)
        if signal is None:
            return
        window_ref = weakref.ref(window)
        signal.connect(
            lambda *_args, ref=window_ref, current=screen:
            self._clamp_to_screen(ref(), current) if ref() is not None else None)
        self._watched_screens.add(key)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, (QWindow, QWidget)):
            tracked = self._tracked.get(id(watched))
            if tracked is not None:
                _window, _key, timer = tracked
                if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
                    timer.start()
                elif event.type() == QEvent.Type.Close:
                    self._save(watched, tracked[1], allow_hidden=True)
        return False

    def _save(self, window: Any, module_key: str, *, allow_hidden: bool = False) -> None:
        if isinstance(window, QWindow):
            visibility = window.visibility()
            if (visibility != QWindow.Visibility.Windowed
                    and not (allow_hidden and visibility == QWindow.Visibility.Hidden)):
                return
        elif window.isMaximized() or window.isFullScreen():
            return
        rectangle = QRect(window.geometry())
        if rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self._settings.setValue(self._settings_key(module_key), rectangle)
        self._settings.sync()

    def _forget_tracking(self, identity: int) -> None:
        tracked = self._tracked.pop(identity, None)
        if tracked is not None:
            tracked[2].stop()
            tracked[2].deleteLater()
        self._watched_screens = {
            key for key in self._watched_screens if key[0] != identity}


def prepare_window_geometry(window: Any, module_key: str, *,
                            screen: QScreen | None = None,
                            preserve_existing: bool = False,
                            settings: QSettings | None = None) -> QRect:
    """Standalone helper for QML module owners outside LauncherBridge."""
    manager = WindowGeometryManager(window, settings=settings)
    return manager.prepare(window, module_key, screen=screen,
                           preserve_existing=preserve_existing)
