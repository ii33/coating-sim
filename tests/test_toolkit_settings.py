"""Credential-free toolkit preferences and isolated QML persistence."""
import json
import pytest
from coat_sim.agent.toolkit_config import normalized_toolkit_settings, validate_workspace_root
from coat_sim.ui import settings


def test_default_empty_capabilities_and_no_shared_mutable_state():
    a = settings.normalized_ui_settings()
    b = settings.normalized_ui_settings()
    assert a['assistant_toolkit_enabled'] is False
    assert a['assistant_toolkit_allow_model_context'] is False
    assert a['assistant_toolkit_workspace_root'] == ''
    assert a['assistant_toolkit_capabilities'] == []
    assert a['assistant_toolkit_profile_capabilities'] == []
    a['assistant_toolkit_capabilities'].append('fs')
    assert b['assistant_toolkit_capabilities'] == []


def test_normalization_is_bounded_credential_free_and_explicit():
    source = {'assistant_toolkit_enabled': 'true', 'assistant_toolkit_allow_model_context': 1,
              'assistant_toolkit_capabilities': ['fs', 'fs', 'unknown'],
              'assistant_toolkit_profile_capabilities': [], 'assistant_toolkit_token': 'SECRET'}
    result = normalized_toolkit_settings(source)
    assert result['assistant_toolkit_capabilities'] == ['fs']
    assert not result['assistant_toolkit_enabled']
    assert not result['assistant_toolkit_allow_model_context']
    assert 'SECRET' not in json.dumps(result)
    assert result['assistant_toolkit_profile_capabilities'] == []


def test_workspace_validation(tmp_path):
    install = tmp_path / 'app'
    install.mkdir()
    workspace = tmp_path / 'work'
    workspace.mkdir()
    assert validate_workspace_root(str(workspace), install) == workspace
    for value in ('', '.', str(tmp_path), str(install), str(tmp_path / 'missing'), '/', '\x00'):
        with pytest.raises(ValueError):
            validate_workspace_root(value, install)
    child = install / 'nested'
    child.mkdir()
    with pytest.raises(ValueError):
        validate_workspace_root(str(child), install)
    link = workspace / 'app-link'
    try:
        link.symlink_to(install, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(ValueError):
        validate_workspace_root(str(link), install)


def test_settings_round_trip_preserves_theme_and_omits_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'settings.json')
    result = settings.save_ui_settings({'theme_mode': 'dark', 'font_family': 'Noto Sans',
        'assistant_toolkit_enabled': True, 'assistant_toolkit_workspace_root': str(tmp_path),
        'assistant_toolkit_capabilities': ['fs', 'tasks'], 'assistant_toolkit_profile_capabilities': ['fs'],
        'assistant_toolkit_secret': 'do-not-persist'})
    assert settings.load_ui_settings() == result
    assert result['font_family'] == 'Noto Sans'
    assert 'do-not-persist' not in settings.SETTINGS_PATH.read_text()


def test_qml_folded_section_contract():
    qml = (settings.PROJECT_ROOT / 'coat_sim/ui/qml/SettingsWindow.qml').read_text()
    section = qml.split('objectName: "assistantToolkitCategory"')[1].split('objectName: "researchLibraryCategory"')[0]
    assert 'expanded: false' in section
    assert 'setAssistantToolkitConfig' in section
    assert 'setAssistantToolkitCapability(modelData.id, checked, false)' in section
    assert 'setAssistantToolkitCapability(modelData.id, checked, true)' in section
    assert 'assistantToolkitStatus' in section
