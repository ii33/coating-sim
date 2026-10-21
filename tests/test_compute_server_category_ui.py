"""Offline QML coverage for the independent compute-server settings category.

Settings and theme files live under pytest's temporary directory. Credential
operations use an in-memory fake, and both Python and QML networking fail shut.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QFont, QGuiApplication, QPalette
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.ui import settings, theme_service
from tests.qt_test_guard import NoNetworkAccessManagerFactory
from tests.qt_visual_lookup import find_visual_item


QML = settings.PROJECT_ROOT / "coat_sim" / "ui" / "qml" / "SettingsWindow.qml"
ASSISTANT_URL = "https://assistant-category.fixture.invalid/v1"
ORIGINAL_URL = "https://compute-category.fixture.invalid/api"
SAVED_URL = "https://saved-compute.fixture.invalid/api"
FAKE_TOKEN = "compute-ui-fixture-token-never-a-real-secret"


def _unexpected_warnings(warnings: list[str]) -> list[str]:
    # Settings sections now use Layout sizing: no anchor-warning exemptions.
    return list(warnings)


def _settle() -> None:
    QTest.qWait(35)
    QCoreApplication.processEvents()


def _find(root: QQuickWindow | QQuickItem, name: str) -> QQuickItem:
    item = find_visual_item(root, name, reverse_children=True)
    assert item is not None, f"QML item not found: {name}"
    return item


def _invoke(target: QObject, method: str) -> None:
    assert QMetaObject.invokeMethod(target, method, Qt.ConnectionType.DirectConnection)
    _settle()


@dataclass
class _Scene:
    engine: QQmlApplicationEngine
    bridge: settings.SettingsBridge
    window: QQuickWindow
    network: NoNetworkAccessManagerFactory
    warnings: list[str]
    closed: bool = False

    def form(self) -> QObject:
        form = _find(self.window, "settingsContentLoader").property("item")
        assert isinstance(form, QObject)
        return form

    def expand_compute(self) -> QQuickItem:
        category = _find(self.window, "computeServerSettingsCategory")
        if not category.property("expanded"):
            self.click(_find(category, "themeSectionHeader"))
        assert category.property("expanded") is True
        return category

    def click(self, item: QQuickItem) -> None:
        scroll = _find(self.window, "settingsScroll")
        flickable = scroll.property("contentItem")
        assert isinstance(flickable, QQuickItem)
        origin = item.mapToItem(flickable, QPointF(0, 0))
        current_y = float(flickable.property("contentY"))
        maximum_y = max(0.0, float(flickable.property("contentHeight")) - flickable.height())
        flickable.setProperty("contentY", min(maximum_y, max(0.0, current_y + origin.y() - 12)))
        _settle()
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        assert 0 <= center.x() < self.window.width()
        assert 0 <= center.y() < self.window.height()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(round(center.x()), round(center.y())))
        _settle()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.window.close()
        self.bridge.closeModelProbe()
        self.bridge.closeAssistantSettingsBootstrap()
        self.bridge.closeMcpConnections()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


class _Harness:
    def __init__(self, service: theme_service.ThemeService, read_token: Mock,
                 save_token: Mock, read_key: Mock, network: Mock) -> None:
        self.service = service
        self.scenes: list[_Scene] = []
        self.read_token = read_token
        self.save_token = save_token
        self.read_key = read_key
        self.network = network

    def open(self) -> _Scene:
        engine = QQmlApplicationEngine()
        network = NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(network)
        bridge = settings.SettingsBridge(parent=engine)
        context = engine.rootContext()
        assert context is not None
        context.setContextProperty("bridge", bridge)
        self.service.attach_engine(engine)
        warnings: list[str] = []
        engine.warnings.connect(lambda errors: warnings.extend(str(error) for error in errors))
        engine.load(QUrl.fromLocalFile(str(QML)))
        roots = engine.rootObjects()
        assert len(roots) == 1, "\n".join(warnings)
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        scene = _Scene(engine, bridge, window, network, warnings)
        self.scenes.append(scene)
        window.resize(960, 760)
        window.show()
        _settle()
        return scene


@pytest.fixture
def compute_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    app = QGuiApplication.instance()
    assert isinstance(app, QGuiApplication)
    previous_font = QFont(app.font())
    previous_palette = QPalette(app.palette())
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "MODEL_CACHE_PATH", tmp_path / "model-cache.json")
    monkeypatch.setattr(settings, "_model_discovery_bus", None)
    monkeypatch.setattr(theme_service.ThemeService, "PACKAGE_ROOT", tmp_path / "themes")
    service = theme_service.ThemeService(_construction_key=theme_service._INSTANCE_KEY)
    monkeypatch.setattr(theme_service.ThemeService, "instance", classmethod(lambda cls: service))
    service.set_theme("light")
    tokens: dict[str, str] = {}
    read_token = Mock(side_effect=lambda url, **_kwargs: tokens.get(url))
    save_token = Mock(side_effect=lambda token, url: tokens.__setitem__(url, token))
    monkeypatch.setattr(settings, "read_compute_token", read_token)
    monkeypatch.setattr(settings, "save_compute_token", save_token)
    monkeypatch.setattr(settings, "delete_compute_token", lambda url: tokens.pop(url, None))
    read_key = Mock(return_value=None)
    monkeypatch.setattr(settings, "read_api_key", read_key)
    network = Mock()
    network.get.side_effect = AssertionError("settings UI must not send HTTP requests")
    network.post.side_effect = AssertionError("settings UI must not send HTTP requests")
    monkeypatch.setattr(settings, "QNetworkAccessManager", lambda _parent: network)
    settings.save_ui_settings({
        "assistant_enabled": False,
        "assistant_api_url": ASSISTANT_URL,
        "assistant_api_urls": [ASSISTANT_URL],
        "assistant_model": "fixture-model",
        "assistant_user_name": "Fixture User",
        "assistant_tone": "intimate",
        "assistant_icon_size": 112,
        "assistant_notification_volume_percent": 64,
        "assistant_daily_token_limit": 65000,
        "assistant_period_token_limit": 8000,
        "assistant_period_minutes": 20,
        "compute_server_url": ORIGINAL_URL,
        "compute_server_tls": True,
        "compute_server_ca_file": "/fixture/original-ca.pem",
    })
    harness = _Harness(service, read_token, save_token, read_key, network)
    try:
        yield harness
        for scene in harness.scenes:
            assert not scene.network.blocked_urls
            unexpected = _unexpected_warnings(scene.warnings)
            assert not unexpected, "\n".join(unexpected)
        network.get.assert_not_called()
        network.post.assert_not_called()
    finally:
        for scene in reversed(harness.scenes):
            scene.close()
        service.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.setFont(previous_font)
        app.setPalette(previous_palette)
        app.processEvents()


def test_compute_category_is_independent_and_cold_start_is_lazy(compute_ui: _Harness) -> None:
    scene = compute_ui.open()
    # Also pass the existing discovery debounce, so deferred work cannot hide
    # an accidental startup credential or HTTP request behind the first frame.
    QTest.qWait(settings.AUTO_PROBE_DEBOUNCE_MS + 100)
    QCoreApplication.processEvents()
    category = _find(scene.window, "computeServerSettingsCategory")
    advanced = _find(scene.window, "settingsAdvancedCategory")
    assert category.isVisible()
    assert category.property("title") == "计算服务器连接"
    assert not advanced.property("expanded")
    assert not _find(scene.window, "settingsAdvancedSectionsLoader").property("active")
    assert not _find(scene.window, "computeServerSettingsLoader").property("active")
    ancestor = category.parentItem()
    while ancestor is not None:
        assert ancestor is not advanced, "compute settings are still nested in assistant details"
        ancestor = ancestor.parentItem()
    assert scene.bridge.property("assistantEnabled") is False
    compute_ui.read_token.assert_not_called()
    compute_ui.read_key.assert_not_called()
    scene.expand_compute()
    assert _find(scene.window, "computeServerUrlField").isVisible()
    assert _find(scene.window, "computeServerUrlField").property("text") == ORIGINAL_URL
    compute_ui.read_token.assert_called_once_with(ORIGINAL_URL, strict=True)
    assert not advanced.property("expanded")
    assert not _find(scene.window, "settingsAdvancedSectionsLoader").property("active")
    compute_ui.read_key.assert_not_called()


def test_compute_drafts_restore_and_close_without_saving(compute_ui: _Harness) -> None:
    scene = compute_ui.open()
    scene.expand_compute()
    url = _find(scene.window, "computeServerUrlField")
    tls = _find(scene.window, "computeServerTlsCheck")
    ca = _find(scene.window, "computeServerCaField")
    token = _find(scene.window, "computeServerTokenField")
    url.setProperty("text", "https://unsaved.fixture.invalid")
    tls.setProperty("checked", False)
    ca.setProperty("text", "/fixture/unsaved-ca.pem")
    token.setProperty("text", FAKE_TOKEN)
    _invoke(scene.window, "restoreFields")
    assert url.property("text") == ORIGINAL_URL
    assert tls.property("checked") is True
    assert ca.property("text") == "/fixture/original-ca.pem"
    assert token.property("text") == ""
    url.setProperty("text", "https://discarded.fixture.invalid")
    tls.setProperty("checked", False)
    ca.setProperty("text", "/fixture/discarded-ca.pem")
    scene.close()
    reopened = compute_ui.open()
    reopened.expand_compute()
    assert _find(reopened.window, "computeServerUrlField").property("text") == ORIGINAL_URL
    assert _find(reopened.window, "computeServerTlsCheck").property("checked") is True
    assert _find(reopened.window, "computeServerCaField").property("text") == "/fixture/original-ca.pem"
    compute_ui.save_token.assert_not_called()


def test_compute_save_token_defaults_and_reopen_preserve_assistant(compute_ui: _Harness) -> None:
    original_assistant = {key: value for key, value in settings.load_ui_settings().items()
                          if key.startswith("assistant_")}
    scene = compute_ui.open()
    category = scene.expand_compute()
    _find(scene.window, "computeServerUrlField").setProperty("text", SAVED_URL)
    _find(scene.window, "computeServerTlsCheck").setProperty("checked", False)
    _find(scene.window, "computeServerCaField").setProperty("text", "/fixture/saved-ca.pem")
    category.setProperty("expanded", False)
    _settle()
    scene.click(_find(scene.window, "saveSettingsButton"))
    saved = settings.load_ui_settings()
    assert saved["compute_server_url"] == SAVED_URL
    assert saved["compute_server_tls"] is False
    assert saved["compute_server_ca_file"] == "/fixture/saved-ca.pem"
    assert saved["assistant_enabled"] is False
    assert saved["assistant_user_name"] == "Fixture User"
    assert saved["assistant_tone"] == "intimate"
    assert saved["assistant_model"] == "fixture-model"
    assert {key: value for key, value in saved.items()
            if key.startswith("assistant_")} == original_assistant
    scene.expand_compute()
    token = _find(scene.window, "computeServerTokenField")
    token.setProperty("text", FAKE_TOKEN)
    scene.click(_find(scene.window, "saveComputeServerTokenButton"))
    compute_ui.save_token.assert_called_once_with(FAKE_TOKEN, SAVED_URL)
    assert token.property("text") == ""
    persisted = settings.SETTINGS_PATH.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in persisted
    assert "compute_server_token" not in json.loads(persisted)
    scene.close()
    reopened = compute_ui.open()
    reopened.expand_compute()
    assert _find(reopened.window, "computeServerUrlField").property("text") == SAVED_URL
    assert _find(reopened.window, "computeServerTlsCheck").property("checked") is False
    assert _find(reopened.window, "computeServerCaField").property("text") == "/fixture/saved-ca.pem"
    assert reopened.bridge.property("computeServerHasToken") is True
    reopened.click(_find(reopened.window, "deleteComputeServerTokenButton"))
    assert reopened.bridge.property("computeServerHasToken") is False
    assert compute_ui.read_token(SAVED_URL) is None
    reopened.click(_find(reopened.window, "restoreDefaultsButton"))
    assert _find(reopened.window, "computeServerUrlField").property("text") == ""
    assert _find(reopened.window, "computeServerTlsCheck").property("checked") is True
    assert _find(reopened.window, "computeServerCaField").property("text") == ""
    # Restoring is still a draft until the user saves it.
    assert settings.load_ui_settings()["compute_server_url"] == SAVED_URL


def test_compute_controls_fit_narrow_window_and_survive_repeated_disclosure(compute_ui: _Harness) -> None:
    scene = compute_ui.open()
    category = scene.expand_compute()
    url = _find(scene.window, "computeServerUrlField")
    url.setProperty("text", "https://draft-kept.fixture.invalid")
    category.setProperty("expanded", False)
    _settle()
    scene.expand_compute()
    assert _find(scene.window, "computeServerUrlField") is url
    assert url.property("text") == "https://draft-kept.fixture.invalid"
    scene.window.resize(560, 420)
    _settle()
    controls = [_find(scene.window, name) for name in (
        "computeServerUrlField", "computeServerTlsCheck", "computeServerCaField",
        "computeServerTokenField", "saveComputeServerTokenButton", "deleteComputeServerTokenButton",
    )]
    rectangles = []
    for item in controls:
        origin = item.mapToItem(category, QPointF(0, 0))
        assert item.isVisible() and item.width() > 0 and item.height() > 0
        assert origin.x() >= -1
        assert origin.x() + item.width() <= category.width() + 1
        rectangles.append((origin.x(), origin.y(), item.width(), item.height()))
    for index, (x, y, width, height) in enumerate(rectangles):
        for other_x, other_y, other_width, other_height in rectangles[index + 1:]:
            overlap_x = min(x + width, other_x + other_width) - max(x, other_x)
            overlap_y = min(y + height, other_y + other_height) - max(y, other_y)
            assert overlap_x <= 1 or overlap_y <= 1, "compute controls overlap in narrow window"
