"""Fail-closed Qt test isolation for user settings and QML networking.

This helper is deliberately imported before application modules or a Qt
application is created.  Both supported import names resolve to this module so
all tests share one settings root and one implementation of the network guard.
"""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QIODevice, QObject, QSettings, QTimer
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply
from PySide6.QtQml import QQmlNetworkAccessManagerFactory


_settings_temp = tempfile.TemporaryDirectory(prefix="coat-qt-test-settings-")
QT_TEST_SETTINGS_ROOT = Path(_settings_temp.name).resolve()
QT_TEST_ORGANIZATION_NAME = "CoatSimTests"
QT_TEST_APPLICATION_NAME = "deposition-sim-test-suite"
atexit.register(_settings_temp.cleanup)

# Qt's organization/application overload is safe only when its default format
# and both scopes point at the private test root.  Tests can change these
# process-global settings, so conftest reasserts this contract at each test
# boundary. Keep the helper available to focused regression tests as well.
def restore_qt_test_settings() -> None:
    """Restore Qt's process-global settings paths and application identity."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      str(QT_TEST_SETTINGS_ROOT))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.SystemScope,
                      str(QT_TEST_SETTINGS_ROOT))
    QCoreApplication.setOrganizationName(QT_TEST_ORGANIZATION_NAME)
    QCoreApplication.setApplicationName(QT_TEST_APPLICATION_NAME)


restore_qt_test_settings()
os.environ["XDG_CONFIG_HOME"] = str(QT_TEST_SETTINGS_ROOT)
os.environ["XDG_DATA_HOME"] = str(QT_TEST_SETTINGS_ROOT)
os.environ["XDG_CACHE_HOME"] = str(QT_TEST_SETTINGS_ROOT / "cache")
(QT_TEST_SETTINGS_ROOT / "cache").mkdir(parents=True, exist_ok=True)

_original_qsettings_init = QSettings.__init__


def _isolated_qsettings_init(self, *args, **kwargs):
    """Rewrite every implicit-profile overload and reject native format."""
    fmt = QSettings.Format
    scope_type = QSettings.Scope
    if kwargs.get("format") == fmt.NativeFormat:
        raise AssertionError("NativeFormat QSettings is disabled in Qt tests")

    parent = kwargs.pop("parent", None)
    explicit_format = kwargs.pop("format", None)
    keyword_scope = kwargs.pop("scope", None)
    organization = kwargs.pop("organization", None)
    application = kwargs.pop("application", None)
    if kwargs:
        raise AssertionError("unsupported QSettings constructor in Qt tests")

    if args and isinstance(args[0], fmt):
        if args[0] == fmt.NativeFormat:
            raise AssertionError("NativeFormat QSettings is disabled in Qt tests")
        # The explicit IniFormat overload is safe, including callers that
        # name a temporary file. Leave it to Qt to validate the arguments.
        return _original_qsettings_init(self, *args, **({} if parent is None else {"parent": parent}))

    if args and isinstance(args[0], str):
        # Filename overloads must state their format. In particular, reject
        # QSettings(filename, NativeFormat) before Qt can inspect that path.
        if len(args) >= 2 and isinstance(args[1], fmt):
            if args[1] == fmt.NativeFormat:
                raise AssertionError("NativeFormat QSettings is disabled in Qt tests")
            return _original_qsettings_init(
                self, *args, **({} if parent is None else {"parent": parent}))
        if explicit_format is not None and explicit_format != fmt.IniFormat:
            raise AssertionError("only IniFormat QSettings is allowed in Qt tests")
        org = args[0]
        if len(args) >= 2 and isinstance(args[1], str):
            app = args[1]
            if len(args) >= 3:
                parent = args[2]
        elif len(args) >= 2 and (args[1] is None or isinstance(args[1], QObject)):
            app = ""
            parent = args[1]
        elif len(args) == 1:
            app = ""
        else:
            raise AssertionError("unsupported organization QSettings overload")
        if organization is not None:
            raise AssertionError("ambiguous QSettings organization arguments")
        if application is not None:
            if len(args) >= 2 and isinstance(args[1], str):
                raise AssertionError("ambiguous QSettings application arguments")
            app = application
        return _original_qsettings_init(
            self, fmt.IniFormat, scope_type.UserScope, org, app, parent)

    if args and isinstance(args[0], scope_type):
        scope = args[0]
        if len(args) >= 2 and isinstance(args[1], str):
            org = args[1]
            app = args[2] if len(args) >= 3 and isinstance(args[2], str) else ""
            if len(args) >= 3 and not isinstance(args[2], str):
                parent = args[2]
            if len(args) >= 4:
                parent = args[3]
        elif len(args) == 1 or (len(args) == 2 and isinstance(args[1], QObject)):
            org, app = "coat-qt-tests", "settings"
            if len(args) == 2:
                parent = args[1]
        else:
            raise AssertionError("unsupported scoped QSettings overload")
        if organization is not None:
            org = organization
        if application is not None:
            app = application
        return _original_qsettings_init(self, fmt.IniFormat, scope, org, app, parent)

    if explicit_format is not None and explicit_format != fmt.IniFormat:
        raise AssertionError("only IniFormat QSettings is allowed in Qt tests")
    if keyword_scope is not None:
        org = organization or "coat-qt-tests"
        app = application or "settings"
        return _original_qsettings_init(
            self, fmt.IniFormat, keyword_scope, org, app, parent)
    if organization is not None or application is not None:
        return _original_qsettings_init(
            self, fmt.IniFormat, scope_type.UserScope,
            organization or "coat-qt-tests", application or "settings", parent)

    # Default and parent-only constructors would otherwise use the platform's
    # profile format. Give them a stable private test identity and Ini path.
    if len(args) <= 1 and (not args or args[0] is None or isinstance(args[0], QObject)):
        if args:
            parent = args[0]
        return _original_qsettings_init(
            self, fmt.IniFormat, scope_type.UserScope,
            "coat-qt-tests", "settings", parent)
    raise AssertionError("unsupported QSettings constructor in Qt tests")


QSettings.__init__ = _isolated_qsettings_init


class _BlockedReply(QNetworkReply):
    """Asynchronous failed reply that never opens a socket."""

    def __init__(self, request, operation, parent=None):
        super().__init__(parent)
        self.setRequest(request)
        self.setUrl(request.url())
        self.setOperation(operation)
        self.open(QIODevice.OpenModeFlag.ReadOnly | QIODevice.OpenModeFlag.Unbuffered)
        self.setError(QNetworkReply.NetworkError.ContentAccessDenied,
                      "Network access is disabled in Qt tests")
        QTimer.singleShot(0, self._finish_blocked_request)

    def abort(self) -> None:
        if self.isFinished():
            return
        self.setError(QNetworkReply.NetworkError.OperationCanceledError,
                      "Network request cancelled")
        self._finish_blocked_request()

    def bytesAvailable(self) -> int:
        return super().bytesAvailable()

    def readData(self, _maximum: int) -> bytes:
        return b""

    def _finish_blocked_request(self) -> None:
        if self.isFinished():
            return
        self.setFinished(True)
        self.readyRead.emit()
        self.finished.emit()


class _NoNetworkAccessManager(QNetworkAccessManager):
    _LOCAL_SCHEMES = frozenset(("file", "qrc", "data"))

    def __init__(self, factory, parent=None):
        super().__init__(parent)
        self._factory = factory

    def createRequest(self, operation, request, outgoingData=None):
        scheme = request.url().scheme().lower()
        if scheme in self._LOCAL_SCHEMES:
            return super().createRequest(operation, request, outgoingData)
        self._factory.blocked_urls.append(request.url().toString())
        return _BlockedReply(request, operation, self)


class NoNetworkAccessManagerFactory(QQmlNetworkAccessManagerFactory):
    """Block outbound QML requests while retaining local file/qrc/data loads."""

    def __init__(self):
        super().__init__()
        self.blocked_urls: list[str] = []

    def create(self, parent):
        return _NoNetworkAccessManager(self, parent)


def qml_item_window(item):
    """Return a QML item's actual surface without changing binding ownership.

    On the tested PySide6 6.12.0 build, QQuickItem.window() reparents its
    returned window wrapper to the item, creating a reciprocal ownership
    cycle that can crash Shiboken during GC. Reading the attached QML
    Window.window property preserves the real popup/window event target.
    """
    from PySide6.QtQml import QQmlEngine, QQmlExpression
    from PySide6.QtQuick import QQuickWindow

    context = QQmlEngine.contextForObject(item)
    if context is None:
        raise AssertionError("window lookup requires an item created by QML")
    expression = QQmlExpression(context, item, "Window.window")
    window, undefined = expression.evaluate()
    if expression.hasError():
        raise AssertionError(expression.error().toString())
    if undefined:
        raise AssertionError("QML Window.window is undefined for this item")
    if hasattr(window, "toVariant"):
        window = window.toVariant()
    if window is not None and not isinstance(window, QQuickWindow):
        raise AssertionError(f"QML Window.window returned {type(window).__name__}")
    return window


# Legacy tests put ``tests`` directly on sys.path and import the top-level
# name.  Newer tests use the package-qualified form.  Alias the first import
# immediately so the two spellings can never create separate global guards.
_module = sys.modules[__name__]
sys.modules.setdefault("qt_test_guard", _module)
sys.modules.setdefault("tests.qt_test_guard", _module)
