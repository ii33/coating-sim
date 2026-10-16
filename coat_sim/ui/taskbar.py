"""Qt application identity and bounded icons, with a Windows shell adapter.

Qt controls window icons on every platform.  A Python-hosted application on
Windows also needs its own taskbar identity and relaunch icon; otherwise the
shell can select python.exe's icon even when QWindow.setIcon was called.
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path
import subprocess
import sys
import uuid

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPlatformSurfaceEvent, QWindow

APP_ID = "com.lyx.coat-sim"
APP_NAME = "沉积仿真"
MODULE_NAMES = {
    "launcher": APP_NAME, "main": APP_NAME, "process": "工艺",
    "machine": "机器结构", "mask": "修正板", "source": "蒸发源",
    "substrate": "基片", "settings": "设置", "about": "关于与帮助",
}
_LOG = logging.getLogger(__name__)


def application_id(module: str) -> str:
    """One stable identity per independently launched module."""
    return APP_ID if module == "launcher" else APP_ID + "." + module


class _QtWindowBranding(QObject):
    """Apply a usable Qt icon before mapping all top-level project windows.

    This changes only windows in this process, never desktop settings or files.
    Qt publishes the image through the platform integration (X11 _NET_WM_ICON).
    """

    def __init__(self, app: QGuiApplication, icon: QIcon) -> None:
        super().__init__(app)
        self.icon = QIcon(icon)
        app.setWindowIcon(self.icon)
        app.installEventFilter(self)
        for window in app.topLevelWindows():
            self.apply(window)

    def apply(self, window: QWindow) -> None:
        kind = window.flags() & Qt.WindowType.WindowType_Mask
        if kind in (Qt.WindowType.Window, Qt.WindowType.Dialog):
            window.setIcon(self.icon)

    def set_icon(self, icon: QIcon) -> None:
        self.icon = QIcon(icon)
        app = self.parent()
        if isinstance(app, QGuiApplication):
            app.setWindowIcon(self.icon)
            for window in app.topLevelWindows():
                self.apply(window)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWindow):
            if event.type() == QEvent.Type.Show:
                self.apply(watched)
            elif (event.type() == QEvent.Type.PlatformSurface and
                  event.surfaceEventType() == QPlatformSurfaceEvent.SurfaceEventType.SurfaceCreated):
                self.apply(watched)
        return False


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", ctypes.c_uint32)]


class _CountedPointer(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("pointer", ctypes.c_void_p)]


class _VariantValue(ctypes.Union):
    # The counted-pointer member preserves PROPVARIANT's native union size
    # (16 bytes on 64-bit Windows, 8 bytes on 32-bit Windows).
    _fields_ = [("pointer", ctypes.c_void_p), ("integer", ctypes.c_uint64),
                ("counted", _CountedPointer)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved1", ctypes.c_uint16),
                ("reserved2", ctypes.c_uint16), ("reserved3", ctypes.c_uint16),
                ("value", _VariantValue)]


def _guid(value: str) -> _Guid:
    return _Guid.from_buffer_copy(uuid.UUID(value).bytes_le)


def _checked(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"{operation} failed (HRESULT 0x{result & 0xffffffff:08X})")


def _relaunch_command(app: QGuiApplication) -> str:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return subprocess.list2cmdline([str(executable)])
    # Preserve the branded process image. Falling back to pythonw here would
    # give a pinned/reopened window the interpreter's name and icon again.
    module = str(app.property("coat_primary_module") or "launcher")
    host_name = MODULE_NAMES.get(module, APP_NAME) + ".exe"
    if executable.name != host_name:
        branded = executable.with_name(host_name)
        hidden = executable.with_name("pythonw.exe")
        if sys.platform == "win32" and branded.is_file():
            executable = branded
        elif hidden.is_file():
            executable = hidden
    launcher = Path(__file__).resolve().parents[2] / "launcher.pyw"
    arguments = list(sys.argv[1:])
    if module not in ("launcher", "main") and "--module" not in arguments:
        arguments = ["--module", module, *arguments]
    elif module == "main" and "--module" not in arguments:
        arguments = ["--module", "main", *arguments]
    return subprocess.list2cmdline([str(executable), str(launcher), *arguments])


class _WindowsShell:
    """Called only on Windows; no native GUI toolkit or extra package."""

    def __init__(self, app_id: str) -> None:
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        self.shell.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_int32
        self.shell.SHGetPropertyStoreForWindow.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
        self.shell.SHGetPropertyStoreForWindow.restype = ctypes.c_int32
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole.CoInitializeEx.restype = ctypes.c_int32
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.interface = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
        self.format = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")
        _checked(self.shell.SetCurrentProcessExplicitAppUserModelID(app_id),
                 "SetCurrentProcessExplicitAppUserModelID")

    def set_properties(self, handle: int, values: tuple[tuple[int, str | None], ...]) -> None:
        # Qt usually initializes COM on its GUI thread. Balance our own call
        # while allowing an already initialized apartment of either kind.
        initialized = self.ole.CoInitializeEx(None, 2)
        if initialized < 0 and (initialized & 0xffffffff) != 0x80010106:
            _checked(initialized, "CoInitializeEx")
        store = ctypes.c_void_p()
        try:
            _checked(self.shell.SHGetPropertyStoreForWindow(
                ctypes.c_void_p(handle), ctypes.byref(self.interface), ctypes.byref(store)),
                "SHGetPropertyStoreForWindow")
            table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            setter = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                                       ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(table[6])
            release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(table[2])
            try:
                for pid, value in values:
                    key = _PropertyKey(self.format, pid)
                    variant = _PropVariant()  # VT_EMPTY removes a property.
                    if value is not None:
                        buffer = ctypes.create_unicode_buffer(value)
                        variant.vt = 31  # VT_LPWSTR; SetValue copies this borrowed string.
                        variant.value.pointer = ctypes.cast(buffer, ctypes.c_void_p)
                    _checked(setter(store, ctypes.byref(key), ctypes.byref(variant)),
                             f"IPropertyStore.SetValue({pid})")
            finally:
                release(store)
        finally:
            if initialized >= 0:
                self.ole.CoUninitialize()


class _TaskbarBranding(QObject):
    def __init__(self, app: QGuiApplication, icon_path: Path) -> None:
        super().__init__(app)
        self.app = app
        module = str(app.property("coat_primary_module") or "launcher")
        name = MODULE_NAMES.get(module, APP_NAME)
        # Shared windows stay in one process. Independently launched modules
        # have distinct shell identities instead of merging into the launcher.
        app_id = application_id(module)
        self.shell = _WindowsShell(app_id)
        self.icon = QIcon(str(icon_path))
        self.properties = ((2, _relaunch_command(app)), (3, f"{icon_path.resolve()},0"),
                           (4, name), (5, app_id))
        self.handles: dict[int, int] = {}
        self.warned = False
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.clear_all)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWindow):
            try:
                if event.type() == QEvent.Type.PlatformSurface:
                    if event.surfaceEventType() == QPlatformSurfaceEvent.SurfaceEventType.SurfaceAboutToBeDestroyed:
                        self.clear(id(watched))
                    elif event.surfaceEventType() == QPlatformSurfaceEvent.SurfaceEventType.SurfaceCreated:
                        self.apply(watched)
                elif event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange):
                    self.apply(watched)
            except (OSError, RuntimeError, AttributeError) as exc:
                if not self.warned:
                    _LOG.warning("Taskbar branding could not be applied: %s", exc)
                    self.warned = True
        return False

    def apply(self, window: QWindow, *, force: bool = False) -> None:
        kind = window.flags() & Qt.WindowType.WindowType_Mask
        if kind not in (Qt.WindowType.Window, Qt.WindowType.Dialog):
            return
        handle = int(window.winId())
        if not handle:
            return
        previous = self.handles.get(id(window))
        if previous == handle and not force:
            return
        if previous is not None and previous != handle:
            self.clear(id(window))
        window.setIcon(self.icon)
        # Set relaunch metadata first: setting AppUserModel.ID tells the shell
        # to refresh the taskbar group using the complete metadata.
        try:
            self.shell.set_properties(handle, self.properties)
        except OSError:
            self.shell.set_properties(handle, tuple((pid, None) for pid, _ in self.properties))
            raise
        self.handles[id(window)] = handle

    def set_icon(self, icon_path: Path, *, qt_icon: QIcon | None = None) -> None:
        self.icon = QIcon(qt_icon) if qt_icon is not None else QIcon(str(icon_path))
        icon_resource = f"{icon_path.resolve()},0"
        self.properties = tuple(
            (pid, icon_resource if pid == 3 else value)
            for pid, value in self.properties
        )
        for window in self.app.topLevelWindows():
            if id(window) not in self.handles:
                continue
            try:
                self.apply(window, force=True)
            except (OSError, RuntimeError, AttributeError) as exc:
                if not self.warned:
                    _LOG.warning("Taskbar icon could not be updated: %s", exc)
                    self.warned = True

    def clear(self, object_id: int) -> None:
        handle = self.handles.pop(object_id, None)
        if handle:
            try:
                self.shell.set_properties(handle, tuple((pid, None) for pid, _ in self.properties))
            except OSError:
                _LOG.debug("Taskbar properties already released for window %s", handle)

    def clear_all(self) -> None:
        for object_id in tuple(self.handles):
            self.clear(object_id)


def install_taskbar_branding(app: QGuiApplication, icon_path: Path,
                            *, qt_icon: QIcon | None = None) -> None:
    """Install before visible windows; native Windows metadata remains optional."""
    if not hasattr(app, "_coat_qt_branding"):
        app._coat_qt_branding = _QtWindowBranding(
            app, qt_icon if qt_icon is not None else QIcon(str(icon_path)))
    if sys.platform != "win32":
        return
    try:
        from coat_sim.ui.windows_snap import install_windows_snap_support

        install_windows_snap_support(app)
    except (OSError, AttributeError, RuntimeError) as exc:
        _LOG.warning("Windows native Snap hit testing could not be installed: %s", exc)
    if hasattr(app, "_coat_taskbar_branding"):
        return
    try:
        app._coat_taskbar_branding = _TaskbarBranding(app, icon_path)
    except (OSError, AttributeError) as exc:
        app._coat_taskbar_branding = None
        _LOG.warning("Windows taskbar identity could not be installed: %s", exc)


def update_taskbar_branding(app: QGuiApplication, icon_path: Path,
                            *, qt_icon: QIcon | None = None) -> None:
    """Hot-apply a changed theme icon to Qt windows and Windows shell metadata."""
    icon = QIcon(qt_icon) if qt_icon is not None else QIcon(str(icon_path))
    qt_branding = getattr(app, "_coat_qt_branding", None)
    if qt_branding is not None:
        qt_branding.set_icon(icon)
    else:
        app.setWindowIcon(icon)
        for window in app.topLevelWindows():
            window.setIcon(icon)
    taskbar_branding = getattr(app, "_coat_taskbar_branding", None)
    if taskbar_branding is not None:
        taskbar_branding.set_icon(icon_path, qt_icon=icon)
