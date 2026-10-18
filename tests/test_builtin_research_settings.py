"""Optional external research configuration does not obstruct bundled resources."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coat_sim.ui import settings


def _save_bridge(root):
    """Exercise the production save method without starting GUI/network workers."""
    state = settings.normalized_ui_settings({"assistant_enabled": False,
        "assistant_toolkit_enabled": False, "assistant_research_library_root": root})
    signal = SimpleNamespace(emit=lambda: None)
    bridge = SimpleNamespace(_settings=state, assistantToolkitEnabled=False,
        assistantApiUrl="", _context_profile_touched=set(), dpiScalePercent=100,
        _context_profile_save_error=lambda: "", _sync_assistant_notification_volume=lambda: None,
        _set_restart_required=lambda value: None, _schedule_auto_probe=lambda: None,
        changed=signal, assistantStateChanged=signal, assistantSettingsSaved=signal)
    bridge._set_status = lambda message: setattr(bridge, "status", message)
    return bridge


@pytest.mark.parametrize("root", ["", "external"])
def test_optional_root_saves_without_enabling_external_permissions(tmp_path, monkeypatch, root):
    if root:
        root = str(tmp_path / root)
        Path(root).mkdir()
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "apply_font_settings", lambda value: None)
    from coat_sim.ui import theme_service
    monkeypatch.setattr(theme_service, "configure_theme_from_settings", lambda value: None)
    bridge = _save_bridge(root)
    assert settings.SettingsBridge.save(bridge)
    saved = json.loads(settings.SETTINGS_PATH.read_text())
    assert saved["assistant_research_library_root"] == root
    assert not saved["assistant_toolkit_enabled"]


@pytest.mark.parametrize("root", ["relative/path", "/definitely-missing-external-research-library"])
def test_bad_external_override_keeps_draft_and_does_not_save(tmp_path, monkeypatch, root):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    bridge = _save_bridge(root)
    assert not settings.SettingsBridge.save(bridge)
    assert bridge._settings["assistant_research_library_root"] == root
    assert "保存失败" in bridge.status
    assert not settings.SETTINGS_PATH.exists()


def test_loading_private_settings_does_not_rewrite_or_enable_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    original = json.dumps({"assistant_research_library_root": "/private/library",
        "assistant_toolkit_allow_model_context": False, "assistant_mcp_servers": []})
    settings.SETTINGS_PATH.write_text(original)
    result = settings.load_ui_settings()
    assert result["assistant_research_library_root"] == "/private/library"
    assert result["assistant_toolkit_allow_model_context"] is False
    assert settings.SETTINGS_PATH.read_text() == original


def test_qml_labels_default_and_override_without_claiming_live_connection():
    qml = (settings.PROJECT_ROOT / "coat_sim/ui/qml/SettingsWindow.qml").read_text()
    section = qml.split('objectName: "researchLibraryCategory"')[1].split('objectName: "externalMcpCategory"')[0]
    assert "内置公开研究资料随应用只读提供" in section
    assert "可选" in section
    assert "绝对路径" in section
    assert "已有本机 sidecar" in section
    assert "实际来源、版本和覆盖范围以研究库状态为准" in section
    assert "无配置时研究库不可用" not in section
