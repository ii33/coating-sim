"""Process-local, read-only subscription to atomically saved UI preferences."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer

_LOG = logging.getLogger(__name__)
_THEME_KEYS = ("theme_mode", "theme_package_id", "font_point_size", "font_family", "density_scale_percent",
               "film_thickness_decimals")
_MAX_SETTINGS_BYTES = 4 * 1024 * 1024


class ThemeSettingsSync(QObject):
    """Each GUI process observes the shared file; no launcher or IPC owner needed.

    Native file/directory events are debounced. The fallback only stats the file;
    file content is read once per observed signature, including invalid revisions.
    No configuration is ever written or unrelated preference applied here.
    """

    def __init__(self, path: Path, apply: Callable[[dict], None], initial: dict, parent=None,
                 *, fallback_ms: int = 2000, debounce_ms: int = 80):
        super().__init__(parent)
        self.path = Path(path).absolute()
        self._apply = apply
        self._signature = None
        self._retry_signature = None
        self._read_retries = 0
        self._theme_values = tuple(initial.get(key) for key in _THEME_KEYS)
        self.last_error = ""
        self.read_count = 0
        self.apply_count = 0
        self._stopped = False
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._schedule)
        self._watcher.directoryChanged.connect(self._schedule)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce_ms = debounce_ms
        self._debounce.setInterval(debounce_ms)
        self._debounce.timeout.connect(self.check)
        self._fallback = QTimer(self)
        self._fallback.setInterval(fallback_ms)
        self._fallback.timeout.connect(self.check)
        self._fallback.start()
        self._arm()
        # Re-read after the startup load, closing its load/subscribe race.
        self._debounce.start()

    def _schedule(self, _path=""):
        if not self._stopped:
            self._debounce.start(self._debounce_ms)

    def _arm(self):
        parent = self.path.parent
        while not parent.is_dir() and parent != parent.parent:
            parent = parent.parent
        desired = {str(parent)}
        if self.path.is_file():
            desired.add(str(self.path))
        current = set(self._watcher.files()) | set(self._watcher.directories())
        if current - desired:
            self._watcher.removePaths(sorted(current - desired))
        if desired - current:
            self._watcher.addPaths(sorted(desired - current))

    def _file_signature(self):
        try:
            info = self.path.stat()
            return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
        except FileNotFoundError:
            return ("missing",)
        except OSError as error:
            return ("stat_error", type(error).__name__, error.errno)

    def check(self):
        if self._stopped:
            return
        self._arm()  # Atomic replacement can drop the file watch.
        signature = self._file_signature()
        if signature == self._signature:
            return
        self._signature = signature
        if signature != self._retry_signature:
            self._retry_signature = signature
            self._read_retries = 0
        if signature == ("missing",):
            # Removal is not a request to reset the user's in-memory theme.
            self.last_error = "settings_missing"
            return
        try:
            with self.path.open("rb") as stream:
                content = stream.read(_MAX_SETTINGS_BYTES + 1)
            self.read_count += 1
            if len(content) > _MAX_SETTINGS_BYTES:
                raise ValueError("UI preferences exceed the theme subscription size limit")
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("UI preferences must contain an object")
            from .settings import normalized_ui_settings
            settings = normalized_ui_settings(payload)
            theme_values = tuple(settings.get(key) for key in _THEME_KEYS)
            if theme_values != self._theme_values:
                self._apply(settings)
                self._theme_values = theme_values
                self.apply_count += 1
            self.last_error = ""
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            self.last_error = type(error).__name__ + ": " + str(error)
            if isinstance(error, OSError) and self._read_retries < 3:
                # Windows may briefly deny a newly replaced file. Retry on the
                # GUI timer, never by sleeping or repeatedly reading a bad JSON.
                self._read_retries += 1
                self._signature = None
                self._debounce.start(150)
                return
            _LOG.warning("Theme preference reload failed at %s; keeping the current theme: %s",
                         self.path, self.last_error)

    def stop(self):
        self._stopped = True
        self._debounce.stop()
        self._fallback.stop()
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)
