from tests.test_toolkit_settings_ui import compute_ui as compute_ui, known_existing_advanced_layout_warnings as known_existing_advanced_layout_warnings
from tests.test_compute_server_category_ui import _find, _settle
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QObject
from PySide6.QtGui import QKeyEvent
from coat_sim.ui import settings
from unittest.mock import Mock
import pytest

@pytest.mark.parametrize('trailing_slash', [False, True])
def test_key_save_after_typing(compute_ui, monkeypatch, trailing_slash):
    saved = Mock()
    monkeypatch.setattr(settings, 'save_api_key', saved)
    scene = compute_ui.open()
    _find(scene.window, 'settingsAdvancedCategory').setProperty('expanded', True)
    _settle()
    url = _find(scene.window, 'assistantUrlField')
    if trailing_slash:
        url.forceActiveFocus()
        url.setProperty('editText', scene.bridge.assistantApiUrl + '/')
        _settle()
    field = _find(scene.window, 'assistantKeyField')
    field.forceActiveFocus()
    _settle()
    secret = 'test-placeholder-only'
    QCoreApplication.sendEvent(scene.window, QKeyEvent(QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, secret))
    _settle()
    assert field.property('text') == secret
    buttons = [x for x in scene.window.findChildren(QObject) if x.metaObject().indexOfSignal('clicked()') >= 0 and x.property('text') == '\u4fdd\u5b58\u5bc6\u94a5']
    assert len(buttons) == 1
    button = buttons[0]
    assert button.property('enabled'), (url.property('text'), scene.bridge.assistantApiUrl)
    scene.click(button)
    saved.assert_called_once_with(secret, scene.bridge.assistantApiUrl)
    assert field.property('text') == ''
