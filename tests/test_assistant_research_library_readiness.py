"""Local connection discovery is actionable, read-only, and separately gated."""
import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from coat_sim.agent import worker
from coat_sim.agent.research_library import TextFileResearchLibrary, assistant_tools, status_tool


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    path = tmp_path / "config" / "assistant_research_library.json"
    monkeypatch.setattr(worker, "_research_library_sidecar_path", lambda: path)
    return path


def test_missing_builtin_is_actionable_without_tools(sidecar, monkeypatch, tmp_path):
    from coat_sim.agent import research_library
    monkeypatch.setattr(research_library, "builtin_research_library_root", lambda: tmp_path / "absent-bundle")
    provider, state, message, metadata = worker._configured_research_library({})
    assert provider is None and state == "builtin_missing" and metadata is None
    assert "重新安装" in message and "无需配置绝对路径" in message
    assert assistant_tools(provider, allow_model_context=True) == []
    status = status_tool(provider, readiness={"status": state, "message": message})()
    assert not status["ready"] and status["tools"] == []
    assert "OpenAlex/Crossref" in status["other_sources"]


@pytest.mark.parametrize("value", ["not json", "{}", '{"root":"relative"}'])
def test_invalid_route_is_explicit(sidecar, value):
    sidecar.parent.mkdir()
    sidecar.write_text(value)
    provider, state, message, _ = worker._configured_research_library({})
    assert provider is None and state == "unreadable"
    assert "JSON" in message


def test_explicit_root_precedes_bad_sidecar_and_does_not_scan(tmp_path, sidecar, monkeypatch):
    sidecar.parent.mkdir()
    sidecar.write_text("invalid")
    root = tmp_path / "library"
    root.mkdir()
    (root / "sample.md").write_text("# Thin films\nITO optical constants")
    with monkeypatch.context() as scoped:
        scoped.setattr(TextFileResearchLibrary, "_iter_documents", Mock(side_effect=AssertionError("scan")))
        provider, state, message, metadata = worker._configured_research_library(
            {"assistant_research_library_root": str(root)})
        status = status_tool(provider, allow_model_context=True,
                             readiness={"status": state, "message": message, "metadata": metadata})()
        assert status["ready"] and state == "connected_read_only"
    tools = {tool.__name__: tool for tool in assistant_tools(provider, allow_model_context=True)}
    result = tools["research_library_search"]("ITO")
    assert result["ok"] and result["results"]
    assert tools["research_library_read"]("sample.md")["ok"]


def test_denied_context_does_not_leak_metadata_or_connected_message():
    status = status_tool(TextFileResearchLibrary(documents={}), readiness={
        "status": "connected_read_only", "message": "connected secret", "metadata": {"revision": "secret"}})()
    assert not status["ready"] and status["metadata"] is None
    assert status["status"] == "context_not_authorized"
    assert "secret" not in json.dumps(status)


def test_relative_and_missing_root_rejected(sidecar, tmp_path):
    for root in ("relative", str(tmp_path / "missing")):
        provider, state, message, _ = worker._configured_research_library({"assistant_research_library_root": root})
        assert provider is None and state == "unreadable" and "绝对路径" in message


def test_worker_forwards_readiness_and_gate(sidecar, monkeypatch, tmp_path):
    from coat_sim.agent import research_library
    monkeypatch.setattr(research_library, "builtin_research_library_root", lambda: tmp_path / "absent-bundle")
    run = AsyncMock()
    writer = Mock()
    asyncio.run(worker._run_task_with_research_library({"config": {}, "request_id": "request"}, writer, run))
    assert run.call_args.kwargs["research_library"] is None
    assert run.call_args.kwargs["allow_research_library_context"] is False
    assert run.call_args.kwargs["research_library_readiness"]["status"] == "builtin_missing"
    assert writer.emit.call_args.kwargs["research_library_status"] == "builtin_missing"


def test_settings_input_preserves_invalid_text_for_save_validation():
    from coat_sim.ui.settings import SettingsBridge
    bridge = SimpleNamespace(_settings={}, assistantStateChanged=Mock())
    SettingsBridge.setAssistantResearchLibraryRoot(cast(SettingsBridge, bridge), " relative ")
    assert bridge._settings["assistant_research_library_root"] == "relative"
    bridge.assistantStateChanged.emit.assert_called_once()


def test_partial_sidecar_retains_verified_coverage(sidecar, tmp_path):
    root = tmp_path / "body"
    root.mkdir()
    metadata = {"snapshot_kind": "initial_partial", "revision": "1234567",
                "included_files": 1, "excluded_files": 9, "repository_files": 10,
                "coverage_note": "Only one selected text document is present."}
    sidecar.parent.mkdir()
    sidecar.write_text(json.dumps({"root": str(root), **metadata}))
    provider, state, message, actual = worker._configured_research_library({})
    assert state == "connected_read_only_partial" and "1/10" in message
    result = status_tool(provider, allow_model_context=True,
                         readiness={"status": state, "metadata": actual})()
    assert result["metadata"]["excluded_files"] == 9
    assert result["metadata"]["snapshot_kind"] == "initial_partial"


def test_settings_save_rejects_bad_root_and_persists_valid_root(tmp_path, monkeypatch):
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui import settings
    app = QGuiApplication.instance() or QGuiApplication([])
    assert app is not None
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", path)
    monkeypatch.setattr(settings, "apply_font_settings", lambda *_: None)
    bridge = settings.SettingsBridge()
    bridge._settings["assistant_enabled"] = False
    monkeypatch.setattr(bridge, "_schedule_auto_probe", lambda: None)
    monkeypatch.setattr(bridge, "_sync_assistant_notification_volume", lambda: None)
    try:
        bridge.setAssistantResearchLibraryRoot("relative")
        assert not bridge.save()
        assert not path.exists()
        assert bridge.assistantResearchLibraryRoot == "relative"
        root = tmp_path / "library"
        root.mkdir()
        bridge.setAssistantResearchLibraryRoot(str(root))
        assert bridge.save()
        assert json.loads(path.read_text())["assistant_research_library_root"] == str(root)
        bridge.setAssistantResearchLibraryRoot("")
        assert bridge.save()
        assert json.loads(path.read_text())["assistant_research_library_root"] == ""
    finally:
        bridge.closeModelProbe()
        bridge.closeAssistantSettingsBootstrap()
        bridge.closeMcpConnections()


def test_settings_qml_exposes_local_root_without_mcp_registration(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent, Qt, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    from coat_sim.ui import settings
    from coat_sim.ui.theme_service import ThemeService
    from tests.test_mcp_service_rows_qml import ROOT, find
    app = QGuiApplication.instance() or QGuiApplication([])
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "MODEL_CACHE_PATH", tmp_path / "models.json")
    monkeypatch.setattr(settings, "read_api_key", lambda *_: None)
    monkeypatch.setattr(settings, "read_compute_token", lambda *_: None)
    bridge = settings.SettingsBridge()
    bridge.setAssistantConfig(False, "", "")
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    ThemeService.instance().attach_engine(engine)
    engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SettingsWindow.qml")))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow)
    try:
        window.resize(560, 600)
        window.showNormal()
        find(window, "settingsAdvancedCategory").setProperty("expanded", True)
        QTest.qWait(80)
        find(window, "researchLibraryCategory").setProperty("expanded", True)
        QTest.qWait(30)
        field = find(window, "researchLibraryRootField")
        assert field.width() > 100 and field.width() < 560
        field.forceActiveFocus()
        QTest.qWait(30)
        for letter in "relative":
            QTest.keyClick(window, ord(letter))
        assert bridge.assistantResearchLibraryRoot == "relative"
        assert not bridge.save()
        assert field.property("text") == "relative"
        assert not (tmp_path / "settings.json").exists()
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window, Qt.Key.Key_Backspace)
        assert bridge.assistantResearchLibraryRoot == ""
        assert len(bridge.property("mcpServers")) == 3
    finally:
        bridge.closeModelProbe()
        bridge.closeAssistantSettingsBootstrap()
        bridge.closeMcpConnections()
        window.close()
        engine.deleteLater()
        # processEvents alone does not drain DeferredDelete. Destroy QML while
        # the context bridge is still alive to avoid null-binding teardown.
        QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
        app.processEvents()
