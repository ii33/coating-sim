"""Scoped temporary roots for Qt tests that exercise LauncherBridge."""
from __future__ import annotations

import shutil
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSettings as _QtQSettings

from coat_sim.core import data_files
from coat_sim.ui import assistant_credentials, settings, window_geometry
from tests.qt_test_guard import NoNetworkAccessManagerFactory


class _TemporaryOnlyQSettings:
    """Require an explicit absolute INI path instead of a user profile key."""

    Format = _QtQSettings.Format
    Scope = _QtQSettings.Scope
    Status = _QtQSettings.Status

    def __new__(cls, *args, **kwargs):
        if (kwargs or len(args) != 2
                or args[1] != _QtQSettings.Format.IniFormat):
            raise AssertionError(
                "launcher UI tests require QSettings(absolute_filename, IniFormat)")
        filename = Path(args[0])
        if not filename.is_absolute():
            raise AssertionError("launcher UI test QSettings path must be absolute")
        return _QtQSettings(str(filename), _QtQSettings.Format.IniFormat)


# Kept as a module symbol because legacy viewport tests patch this exact seam.
QSettings = _TemporaryOnlyQSettings


def _blocked(name):
    def reject(*_args, **_kwargs):
        raise AssertionError(f"{name} is disabled by LauncherTestIsolation")
    return reject


class LauncherTestIsolation:
    """Redirect app-owned state to ``root`` and deny external service starts.

    Static optical catalog data is copied into the temporary data tree.  User
    settings and credentials are never read; all patches are restored by
    :meth:`stop`.
    """

    def __init__(self, root, launcher_module):
        self.root = Path(root).expanduser().resolve()
        self.launcher_module = launcher_module
        self.data_root = self.root / "data"
        self._stack: ExitStack | None = None
        self._settings_counter = 0
        self._qsettings_type = None

    def apply(self) -> "LauncherTestIsolation":
        if self._stack is not None:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._install_optical_catalog()

        stack = ExitStack()
        try:
            self._stack = stack
            self._set(self.launcher_module, "PROJECT_ROOT", self.root)
            self._set(data_files, "PROJECT_ROOT", self.root)
            self._set(data_files, "DATA_ROOT", self.data_root)
            self._set(settings, "PROJECT_ROOT", self.root)
            self._set(settings, "SETTINGS_PATH", self.root / "config" / "ui_settings.json")
            self._set(settings, "MODEL_CACHE_PATH",
                      self.root / "config" / "assistant_model_cache.json")
            self._set(settings, "_model_discovery_bus", None)
            self._set(assistant_credentials, "SETTINGS_PATH",
                      self.root / "config" / "ui_settings.json")

            # Catch both default construction and the factory's explicit
            # create_settings() calls.  QSettings remains patchable for older
            # tests that replace this module-level symbol.
            self._qsettings_type = _root_bound_qsettings(self.root)
            self._set(window_geometry, "QSettings", self._qsettings_type)

            # A settings bridge can be created by QML.  Its endpoint-bound
            # key lookup must never enter the OS keyring in UI tests.
            self._set(settings, "read_api_key", lambda *_a, **_kw: None)
            self._set(settings, "save_api_key", _blocked("credential save"))
            self._set(settings, "delete_api_key", lambda *_a, **_kw: None)
            self._set(settings, "save_mcp_secret", _blocked("MCP credential save"))
            self._set(settings, "delete_mcp_secret", lambda *_a, **_kw: None)
            self._set(settings, "run_probe", _blocked("external MCP probe"))
            self._set(settings, "QNetworkAccessManager", _offline_network_manager)
            self._set(assistant_credentials, "read_api_key", lambda *_a, **_kw: None)
            self._set(assistant_credentials, "save_api_key", _blocked("credential save"))
            self._set(assistant_credentials, "delete_api_key", lambda *_a, **_kw: None)
            self._set(assistant_credentials, "save_mcp_secret",
                      _blocked("MCP credential save"))
            self._set(assistant_credentials, "delete_mcp_secret", lambda *_a, **_kw: None)

            self._block_service_construction()
            return self
        except BaseException:
            self.stop()
            raise

    def _set(self, module, name, value) -> None:
        self._stack.enter_context(patch.object(module, name, value))

    def _install_optical_catalog(self) -> None:
        catalog = Path(__file__).resolve().parents[1] / "data" / "optical_catalog.json"
        if not catalog.is_file():
            raise FileNotFoundError(f"repository optical catalog is missing: {catalog}")
        destination = self.data_root / "optical_catalog.json"
        if not destination.exists():
            shutil.copy2(catalog, destination)

    def _block_service_construction(self) -> None:
        # Patch class methods so aliases imported before apply() are guarded too.
        from coat_sim.scheduler import SchedulerClient
        from coat_sim.ui.assistant import AssistantBridge, AssistantController
        from coat_sim.ui.assistant_debug import AssistantDebugServer
        from coat_sim.ui.assistant_desktop import DesktopAssistantController
        from coat_sim.ui.control import UiControlServer
        from coat_sim.ui.mcp_endpoint_process import McpEndpointProcess

        self._set(SchedulerClient, "__init__", _blocked("scheduler client"))
        self._set(UiControlServer, "__init__", _blocked("UI control server"))
        self._set(UiControlServer, "start", _blocked("UI control server"))
        self._set(AssistantBridge, "__init__", _blocked("assistant bridge"))
        self._set(AssistantController, "__init__", _blocked("assistant controller"))
        self._set(DesktopAssistantController, "__init__",
                  _blocked("desktop assistant controller"))
        self._set(AssistantDebugServer, "__init__", _blocked("assistant debug server"))
        self._set(McpEndpointProcess, "start", _blocked("MCP endpoint process"))

    def create_settings(self):
        if self._stack is None or self._qsettings_type is None:
            raise RuntimeError("call apply() before create_settings()")
        self._settings_counter += 1
        filename = (self.root / "qt-settings"
                    / f"launcher-window-{self._settings_counter}.ini")
        filename.parent.mkdir(parents=True, exist_ok=True)
        value = self._qsettings_type(str(filename), QSettings.Format.IniFormat)
        actual = Path(value.fileName()).resolve()
        if actual != filename.resolve() or not actual.is_relative_to(self.root):
            raise AssertionError(f"QSettings escaped launcher test root: {actual}")
        return value

    def stop(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
        self._qsettings_type = None


def _root_bound_qsettings(root: Path):
    root = root.resolve()

    class RootBoundQSettings(_TemporaryOnlyQSettings):
        def __new__(cls, *args, **kwargs):
            value = super().__new__(cls, *args, **kwargs)
            actual = Path(value.fileName()).resolve()
            if not actual.is_relative_to(root):
                raise AssertionError(f"QSettings escaped launcher test root: {actual}")
            return value

    return RootBoundQSettings


def _offline_network_manager(parent=None):
    factory = NoNetworkAccessManagerFactory()
    manager = factory.create(parent)
    # Retain the factory for callers that inspect requests during the test.
    manager._launcher_test_factory = factory
    return manager
