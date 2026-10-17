"""Owned native Qt probe: private settings/state/runtime; no provider calls."""
from pathlib import Path
import gc
import faulthandler
import json
import os
import socket
import sys
from contextlib import ExitStack
from unittest.mock import patch

root, state, runtime, report = map(Path, sys.argv[1:5])
mode = sys.argv[5]
sys.path.insert(0, str(root))
sys.dont_write_bytecode = True
state.mkdir(parents=True, exist_ok=True)
import coat_sim.project_logging as project_logging  # noqa: E402
project_logging.PROJECT_ROOT = state/'app'
import coat_sim.assistant_process as assistant_process  # noqa: E402
assistant_process.PROJECT_ROOT = state/'app'
os.environ['COAT_ASSISTANT_RUNTIME_ROOT'] = str(runtime)
original_dns = socket.getaddrinfo
def local_dns(host, *args, **kwargs):
    if host not in ('localhost', '127.0.0.1', '::1', None):
        raise OSError('native_qa_offline')
    return original_dns(host, *args, **kwargs)
socket.getaddrinfo = local_dns

from PySide6.QtCore import QSettings, QTimer  # noqa: E402 -- private paths must be selected before application imports.
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(state/'qsettings'))
import coat_sim.ui.settings as settings  # noqa: E402 -- private paths must be selected before application imports.
settings.SETTINGS_PATH = state/'ui_settings.json'
if mode != 'first_user':
    settings.save_ui_settings({'assistant_enabled': True, 'assistant_notification_sound': False,
                              'assistant_api_url': '', 'assistant_model': ''})
import coat_sim.agent.storage as storage  # noqa: E402 -- private paths must be selected before application imports.
original_init = storage.AssistantStore.__init__
original_path = storage.STATE_PATH
def private_store(self, path=original_path):
    original_init(self, state/'assistant.sqlite3' if Path(path)==original_path else path)
support = ExitStack()
support.enter_context(patch.object(storage.AssistantStore, "__init__", private_store))
import coat_sim.ui.assistant_desktop as desktop  # noqa: E402 -- private paths must be selected before application imports.
desktop.CURSOR_PATH = state/'cursor.json'
support.enter_context(patch.object(desktop, "QSoundEffect", None))
support.enter_context(patch.object(desktop.DesktopAssistantBridge, "refreshQueue", lambda self: None))
import coat_sim.ui.assistant_process_host as host_module  # noqa: E402 -- private paths must be selected before application imports.
from PySide6.QtWidgets import QApplication  # noqa: E402 -- private paths must be selected before application imports.
import coat_sim.ui.assistant_debug as debug  # noqa: E402 -- isolate before host construction.
original_debug_init = debug.AssistantDebugServer.__init__
def private_debug(self, bridge, root=None):
    original_debug_init(self, bridge, root=state/'debug-root')
support.enter_context(patch.object(debug.AssistantDebugServer, "__init__", private_debug))


def app() -> QApplication:
    value = QApplication.instance()
    assert isinstance(value, QApplication)
    return value

payload = {'pid': os.getpid(), 'mode': mode, 'ok': False, 'phase': 'starting'}
def write():
    report.write_text(json.dumps(payload), encoding='utf-8')

class ProbeHost(host_module.AssistantProcessHost):
    def __init__(self, *args):
        super().__init__(*args)
        QTimer.singleShot(120, self.begin_probe)
    def guarded(self, callback):
        try:
            callback()
        except Exception as exc:
            payload.update(phase='failed', error_type=type(exc).__name__, error=str(exc))
            write()
            app().exit(70)
    def later(self, callback):
        QTimer.singleShot(100, lambda: self.guarded(callback))
    def begin_probe(self):
        self.guarded(self.start_probe)
    def start_probe(self):
        application = app()
        assert not application.quitOnLastWindowClosed()
        if mode == 'fault_handler_policy' and os.name == 'nt':
            assert not faulthandler.is_enabled(), 'Inherited Windows native handler bypasses desktop safety policy'
        if mode == 'first_user':
            assert not self.controller.bridge.enabled
            assert self.controller._chat is None
            settings.save_ui_settings({'assistant_enabled': True, 'assistant_notification_sound': False,
                                      'assistant_api_url': '', 'assistant_model': ''})
            self.controller.reload_configuration()
        self.controller.show_chat()
        from shiboken6 import isValid
        assert isValid(self.controller._bubble) and isValid(self.controller._chat)
        assert self.controller._chat is not None and self.controller._chat.isVisible()
        payload.update(phase='ready', generation=self.generation)
        write()
        if mode == 'hold':
            self._hold_timer = QTimer(self)
            self._hold_timer.timeout.connect(lambda: app().quit() if (state/'stop').exists() else None)
            self._hold_timer.start(50)
            QTimer.singleShot(30000, lambda: app().quit())
            return
        if mode == 'crash':
            os._exit(23)  # Only this owned probe, to verify OS-lock crash release.
        if mode in ('destroy_chat', 'destroy_bubble'):
            getattr(self.controller, '_chat' if mode=='destroy_chat' else '_bubble').deleteLater()
            self.later(self.recover_destroyed)
        else:
            self.chat_id = id(self.controller._chat)
            self.cycles = 0
            self.later(self.close_chat)
    def recover_destroyed(self):
        gc.collect()
        self.controller.show_chat()
        from shiboken6 import isValid
        assert isValid(self.controller._bubble) and isValid(self.controller._chat)
        assert self.controller._chat is not None and self.controller._chat.isVisible()
        self.finish_probe()
    def close_chat(self):
        assert self.controller._chat is not None
        preview = state/'assistant-preview.png'
        if not preview.exists():
            image = self.controller._chat.grabWindow()
            assert not image.isNull() and image.save(str(preview))
            payload['preview'] = str(preview)
        self.controller._chat.close()
        assert not self.controller._chat.isVisible()
        self.later(self.reopen_chat)
    def reopen_chat(self):
        gc.collect()
        self.controller.bridge.toggleRequested.emit()
        assert self.controller._chat is not None and self.controller._chat.isVisible()
        assert id(self.controller._chat) == self.chat_id
        self.cycles += 1
        if self.cycles < 3:
            self.later(self.close_chat)
        else:
            self.controller.hide_windows()
            self.later(self.finish_probe)
    def finish_probe(self):
        payload.update(phase='done', ok=True, quit_on_last=app().quitOnLastWindowClosed())
        write()
        self.later(lambda: app().quit())

support.enter_context(patch.object(host_module, "AssistantProcessHost", ProbeHost))
from coat_sim.assistant_process import main  # noqa: E402 -- private paths must be selected before application imports.
raise SystemExit(main(['--runtime-root', str(runtime)]))
