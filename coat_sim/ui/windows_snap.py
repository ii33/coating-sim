"""Native Windows hit testing for the custom Qt Quick application frame."""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QPlatformSurfaceEvent, QWindow
from PySide6.QtQuick import QQuickItem, QQuickWindow


_LOG = logging.getLogger(__name__)

_GWL_STYLE = -16
_WS_POPUP = 0x80000000
_WS_OVERLAPPEDWINDOW = 0x00CF0000
_WS_THICKFRAME = 0x00040000
_WS_MAXIMIZEBOX = 0x00010000
_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_HTCLIENT = 1
_HTCAPTION = 2
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _Point),
        ("lPrivate", wintypes.DWORD),
    ]


class _Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _NativeSnapFilter(QAbstractNativeEventFilter):
    def __init__(self, owner: "WindowsSnapSupport") -> None:
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):
        if hasattr(event_type, "data"):
            event_type = bytes(event_type)
        if event_type not in (b"windows_generic_MSG", "windows_generic_MSG"):
            return False, 0
        try:
            address = message.toInt()[0] if hasattr(message, "toInt") else int(message)
            native_message = ctypes.cast(address, ctypes.POINTER(_Message)).contents
            hwnd = int(native_message.hwnd or 0)
            entry = self.owner._windows.get(hwnd)
            window = entry[0] if entry else None
            if window is None:
                return False, 0
            if native_message.message == _WM_NCCALCSIZE:
                # Keep QML's existing full-window titlebar and content layout.
                return True, 0
            if native_message.message == _WM_NCHITTEST:
                return True, self.owner._hit_test(window, native_message.lParam)
        except (AttributeError, OSError, TypeError, ValueError):
            return False, 0
        return False, 0


class WindowsSnapSupport(QObject):
    """Give QML titlebars native caption/resize hit tests without drawing a frame."""

    def __init__(self, app: QGuiApplication) -> None:
        super().__init__(app)
        self.app = app
        self._windows: dict[int, tuple[QQuickWindow, dict[str, QQuickItem]]] = {}
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._get_style = getattr(self._user32, "GetWindowLongPtrW", self._user32.GetWindowLongW)
        self._get_style.argtypes = (wintypes.HWND, ctypes.c_int)
        self._get_style.restype = ctypes.c_ssize_t
        self._set_style = getattr(self._user32, "SetWindowLongPtrW", self._user32.SetWindowLongW)
        self._set_style.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        self._set_style.restype = ctypes.c_ssize_t
        self._set_window_pos = self._user32.SetWindowPos
        self._set_window_pos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         wintypes.UINT)
        self._set_window_pos.restype = wintypes.BOOL
        self._get_window_rect = self._user32.GetWindowRect
        self._get_window_rect.argtypes = (wintypes.HWND, ctypes.POINTER(_Rect))
        self._get_window_rect.restype = wintypes.BOOL
        self._native_filter = _NativeSnapFilter(self)
        app.installNativeEventFilter(self._native_filter)
        app.installEventFilter(self)
        for window in app.topLevelWindows():
            self._register(window)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWindow):
            return False
        event_type = event.type()
        if event_type == QEvent.Type.PlatformSurface:
            surface_type = event.surfaceEventType()
            if surface_type == QPlatformSurfaceEvent.SurfaceEventType.SurfaceAboutToBeDestroyed:
                self._forget_window(watched)
            elif surface_type == QPlatformSurfaceEvent.SurfaceEventType.SurfaceCreated:
                self._register(watched)
        elif event_type in (QEvent.Type.Show, QEvent.Type.WinIdChange,
                            QEvent.Type.WindowStateChange):
            self._register(watched)
        return False

    def _forget_window(self, window: QWindow) -> None:
        try:
            current_handle = int(window.winId())
        except RuntimeError:
            current_handle = 0
        for handle, entry in tuple(self._windows.items()):
            if entry[0] is window or (current_handle and handle == current_handle):
                self._windows.pop(handle, None)

    @staticmethod
    def _named_items(window: QQuickWindow) -> dict[str, QQuickItem]:
        found: dict[str, QQuickItem] = {}
        root = window.contentItem()
        pending = [root] if root is not None else []
        while pending:
            item = pending.pop()
            name = item.objectName()
            if name:
                found[name] = item
            pending.extend(item.childItems())
        return found

    def _register(self, window: QWindow) -> None:
        if not isinstance(window, QQuickWindow):
            return
        if not (window.flags() & Qt.WindowType.FramelessWindowHint):
            return
        items = self._named_items(window)
        if "windowChromeTitleDrag" not in items:
            return
        handle = int(window.winId())
        if not handle:
            return
        for old_handle, entry in tuple(self._windows.items()):
            if old_handle != handle and entry[0] is window:
                self._windows.pop(old_handle, None)
        self._windows[handle] = (window, items)
        style = int(self._get_style(wintypes.HWND(handle), _GWL_STYLE)) & 0xFFFFFFFF
        updated_style = (style & ~_WS_POPUP) | _WS_OVERLAPPEDWINDOW
        if updated_style == style:
            return
        self._set_style(wintypes.HWND(handle), _GWL_STYLE, updated_style)
        changed = self._set_window_pos(
            wintypes.HWND(handle), None, 0, 0, 0, 0,
            _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        if not changed:
            raise OSError(ctypes.get_last_error(), "SetWindowPos(SWP_FRAMECHANGED) failed")

    def _hit_test(self, window: QQuickWindow, lparam: int) -> int:
        if window.visibility() != QWindow.Visibility.Windowed:
            return _HTCLIENT
        handle = int(window.winId())
        rect = _Rect()
        if not self._get_window_rect(wintypes.HWND(handle), ctypes.byref(rect)):
            return _HTCLIENT
        screen_x = ctypes.c_short(lparam & 0xFFFF).value
        screen_y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        scale = max(float(window.devicePixelRatio()), 1.0)
        x = (screen_x - rect.left) / scale
        y = (screen_y - rect.top) / scale
        width, height = float(window.width()), float(window.height())
        grip = 7.0  # Matches WindowChrome.qml's resizeEdges.grip.
        left, right = x < grip, x >= width - grip
        top, bottom = y < grip, y >= height - grip
        if top and left:
            return _HTTOPLEFT
        if top and right:
            return _HTTOPRIGHT
        if bottom and left:
            return _HTBOTTOMLEFT
        if bottom and right:
            return _HTBOTTOMRIGHT
        if left:
            return _HTLEFT
        if right:
            return _HTRIGHT
        if top:
            return _HTTOP
        if bottom:
            return _HTBOTTOM

        entry = self._windows.get(handle)
        title_drag = entry[1].get("windowChromeTitleDrag") if entry else None
        if title_drag is None:
            return _HTCLIENT
        origin = title_drag.mapToItem(window.contentItem(), QPointF(0, 0))
        if (origin.x() <= x < origin.x() + title_drag.width()
                and origin.y() <= y < origin.y() + title_drag.height()):
            return _HTCAPTION
        return _HTCLIENT


def install_windows_snap_support(app: QGuiApplication) -> WindowsSnapSupport | None:
    """Install once per Windows GUI process; other platforms keep Qt behavior."""
    if sys.platform != "win32":
        return None
    # A Windows host can still run Qt with the offscreen/minimal plugin in
    # tests or headless tools. Those windows do not own Win32 HWNDs, so native
    # style changes and hit testing must stay disabled for that platform.
    if str(app.platformName()).lower() != "windows":
        return None
    existing = getattr(app, "_coat_windows_snap_support", None)
    if existing is not None:
        return existing
    support = WindowsSnapSupport(app)
    app._coat_windows_snap_support = support
    return support
