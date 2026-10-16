"""Shared taskbar icons and the Windows desktop shortcut icon path."""

from pathlib import Path
import sys

from PySide6.QtCore import QObject, QUrl, Qt, Slot
from PySide6.QtGui import QIcon, QGuiApplication, QPixmap


ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"
WINDOW_ICON_FILES = {
    "launcher": "launcher.png",
    "main": "deposit.png",
    "process": "process.png",
    "machine": "mechanic.png",
    "mask": "mask.png",
    "source": "source.png",
    # No separate substrate artwork is present; process.png depicts glass.
    "substrate": "process.png",
    "settings": "setting.png",
    "about": "about.png",
}


def icon_path(key: str, *, desktop_shortcut: bool = False) -> Path:
    if desktop_shortcut and key == "launcher":
        return ICON_DIR / "launcher.ico"
    return ICON_DIR / WINDOW_ICON_FILES.get(key, "launcher.png")


# Small-first raster representations stay within even X11's minimum request
# budget. A raw 1254px PNG can otherwise produce an empty _NET_WM_ICON.
TASKBAR_ICON_SIZES = (16, 24, 32, 48, 64, 128)
_icon_cache: dict[str, QIcon] = {}


def _taskbar_icon(path: Path) -> QIcon:
    key = str(path.resolve())
    if key not in _icon_cache:
        source = QPixmap(key)
        result = QIcon()
        if not source.isNull():
            for size in TASKBAR_ICON_SIZES:
                result.addPixmap(source.scaled(size, size, Qt.KeepAspectRatio,
                                               Qt.SmoothTransformation))
        _icon_cache[key] = result
    return QIcon(_icon_cache[key])


def _theme_icon_path(assets: object) -> Path | None:
    if not isinstance(assets, dict):
        return None
    value = assets.get("applicationIconUrl")
    if not isinstance(value, str) or not value:
        return None
    url = QUrl(value)
    if not url.isLocalFile():
        return None
    path = Path(url.toLocalFile())
    return path if path.is_file() else None


def _default_application_icon_path(key: str) -> Path:
    taskbar_path = ICON_DIR / "logolite.png"
    return taskbar_path if taskbar_path.is_file() else icon_path(key)


class _ThemeIconBinding(QObject):
    """Keep app, top-level window, and Windows relaunch icons in sync."""

    def __init__(self, app: QGuiApplication, theme_service) -> None:
        super().__init__(app)
        self.app = app
        self.theme_service = theme_service
        theme_service.snapshotReplaced.connect(self._theme_replaced)

    @Slot(object, int, str, str, str, str, object)
    def _theme_replaced(self, snapshot, revision, mode, preference, theme_id, theme_name, assets):
        key = str(self.app.property("coat_primary_module") or "launcher")
        themed_path = _theme_icon_path(assets)
        path = themed_path or _default_application_icon_path(key)
        icon = _taskbar_icon(path)
        if themed_path is not None and icon.isNull():
            themed_path = None
            path = _default_application_icon_path(key)
            icon = _taskbar_icon(path)
        branding_path = path
        if themed_path is None:
            native_icon = ICON_DIR / "logolite.ico"
            if native_icon.is_file():
                branding_path = native_icon
        from coat_sim.ui.taskbar import update_taskbar_branding

        update_taskbar_branding(self.app, branding_path, qt_icon=icon)


def _theme_service_for_app(app: QGuiApplication):
    from coat_sim.ui.theme_service import ThemeService

    service = ThemeService.instance()
    if not hasattr(app, "_coat_theme_icon_binding"):
        app._coat_theme_icon_binding = _ThemeIconBinding(app, service)
    return service


def window_icon(key: str) -> QIcon:
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        # QPixmap must only be constructed after a GUI application exists.
        return QIcon(str(_default_application_icon_path(key)))
    theme_service = _theme_service_for_app(app)
    themed_path = _theme_icon_path(theme_service.current_assets)
    path = themed_path or _default_application_icon_path(key)
    icon = _taskbar_icon(path)
    if themed_path is not None and icon.isNull():
        themed_path = None
        path = _default_application_icon_path(key)
        icon = _taskbar_icon(path)
    if not app.property("coat_primary_module"):
        app.setProperty("coat_primary_module", key)
    from coat_sim.ui.taskbar import application_id, install_taskbar_branding
    # A module opened in-process must not change the primary process identity.
    # Linux WM_CLASS and desktop ID must match the registered desktop entry;
    # application settings themselves use explicit project JSON paths.
    app_id = application_id(str(app.property("coat_primary_module")))
    app.setApplicationName(app_id if sys.platform == "linux" else "coat-sim")
    app.setApplicationDisplayName("")
    app.setOrganizationName("LYX")
    app.setDesktopFileName(app_id)
    branding_path = path
    if themed_path is None:
        native_icon = ICON_DIR / "logolite.ico"
        if native_icon.is_file():
            branding_path = native_icon
    install_taskbar_branding(app, branding_path, qt_icon=icon)
    return icon
