"""Real offline QML interaction, draft isolation and reload for toolkit settings."""
from tests.test_compute_server_category_ui import compute_ui as compute_ui, _find, _settle, _invoke
from coat_sim.ui import settings
from coat_sim.agent.toolkit_config import PRIVATE_CAPABILITIES, default_workspace_root
import pytest


@pytest.fixture(autouse=True)
def known_existing_advanced_layout_warnings():
    """Compatibility fixture name; the shared harness now rejects every warning."""



def _expand(scene):
    advanced = _find(scene.window, 'settingsAdvancedCategory')
    advanced.setProperty('expanded', True)
    _settle()
    section = _find(scene.window, 'assistantToolkitCategory')
    assert section.property('expanded') is False
    section.setProperty('expanded', True)
    _settle()
    return section


def test_toolkit_qml_persistence_and_empty_means_off(compute_ui, tmp_path):
    scene = compute_ui.open()
    section = _expand(scene)
    bridge = scene.bridge
    assert _find(section, 'assistantToolkitEnabledCheck').property('checked')
    root = _find(section, 'assistantToolkitWorkspaceRootField')
    root.setProperty('text', str(tmp_path))
    _invoke(root, 'textEdited')
    enabled = _find(section, 'assistantToolkitEnabledCheck')
    enabled.setProperty('checked', True)
    _invoke(enabled, 'clicked')
    context = _find(section, 'assistantToolkitContextCheck')
    context.setProperty('checked', True)
    _invoke(context, 'clicked')
    for name in ('toolkitGlobal_fs', 'toolkitProfile_fs'):
        check = _find(section, name)
        check.setProperty('checked', True)
        _invoke(check, 'clicked')
    for capability in PRIVATE_CAPABILITIES:
        if capability != 'fs':
            for prefix in ('toolkitGlobal_', 'toolkitProfile_'):
                check = _find(section, prefix + capability)
                check.setProperty('checked', False)
                _invoke(check, 'clicked')
    assert bridge.assistantToolkitCapabilities == ['fs']
    assert bridge.assistantToolkitProfileCapabilities == ['fs']
    assert bridge.save()
    scene.close()
    reopened = compute_ui.open()
    section = _expand(reopened)
    assert _find(section, 'assistantToolkitEnabledCheck').property('checked')
    assert _find(section, 'assistantToolkitWorkspaceRootField').property('text') == str(tmp_path)
    assert _find(section, 'toolkitGlobal_fs').property('checked')
    check = _find(section, 'toolkitProfile_fs')
    check.setProperty('checked', False)
    _invoke(check, 'clicked')
    assert reopened.bridge.assistantToolkitProfileCapabilities == []
    assert '全部' in reopened.bridge.assistantToolkitStatus or '所有工具关闭' in reopened.bridge.assistantToolkitStatus
    assert reopened.bridge.save()
    assert settings.load_ui_settings()['assistant_toolkit_profile_capabilities'] == []


def test_toolkit_invalid_root_and_draft_do_not_leak(compute_ui, tmp_path):
    scene = compute_ui.open()
    _expand(scene)
    scene.bridge.setAssistantToolkitConfig(True, 'relative/path', True)
    scene.bridge.setAssistantToolkitCapability('tasks', True, False)
    assert not scene.bridge.save()
    assert '绝对路径' in scene.bridge.statusMessage
    assert settings.load_ui_settings()['assistant_toolkit_enabled'] is True
    scene.close()
    reopened = compute_ui.open()
    _expand(reopened)
    assert reopened.bridge.assistantToolkitCapabilities == list(PRIVATE_CAPABILITIES)
    assert reopened.bridge.assistantToolkitWorkspaceRoot == str(default_workspace_root())
    assert reopened.bridge.assistantToolkitEnabled


def test_restore_default_workspace_is_draft_only_until_saved(compute_ui, tmp_path):
    scene = compute_ui.open()
    section = _expand(scene)
    scene.bridge.setAssistantToolkitConfig(False, str(tmp_path), False)
    assert scene.bridge.save()
    button = _find(section, 'assistantToolkitResetWorkspaceButton')
    _invoke(button, 'clicked')
    assert scene.bridge.assistantToolkitEnabled
    assert scene.bridge.assistantToolkitAllowModelContext
    assert scene.bridge.assistantToolkitWorkspaceRoot == str(default_workspace_root())
    assert scene.bridge.assistantToolkitCapabilities == list(PRIVATE_CAPABILITIES)
    assert not settings.load_ui_settings()['assistant_toolkit_enabled']
    assert scene.bridge.save()
    scene.close()
    reopened = compute_ui.open()
    assert reopened.bridge.assistantToolkitEnabled
    assert reopened.bridge.assistantToolkitWorkspaceRoot == str(default_workspace_root())


def test_offline_library_needs_no_settings_page_or_root_field(compute_ui, tmp_path):
    from PySide6.QtCore import QObject
    scene = compute_ui.open()
    _expand(scene)
    assert scene.window.findChild(QObject, 'researchLibraryCategory') is None
    assert scene.window.findChild(QObject, 'researchLibraryRootField') is None
    # Hidden compatibility configuration retains user research and its save flow.
    scene.bridge.setAssistantResearchLibraryRoot(str(tmp_path))
    assert scene.bridge.save()
    scene.close()
    before = settings.SETTINGS_PATH.read_bytes()
    reopened = compute_ui.open()
    assert reopened.bridge.assistantResearchLibraryRoot == str(tmp_path)
    assert settings.SETTINGS_PATH.read_bytes() == before
