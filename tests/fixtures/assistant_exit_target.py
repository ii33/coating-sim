"""Owned real project draft and runtime confirmation, isolated from user state."""
# ruff: noqa: E402 -- private logging/settings must precede Qt and application imports.
from pathlib import Path
import json
import sys
root, state = map(Path, sys.argv[1:3])
key = sys.argv[3]
sys.path.insert(0, str(root))
from coat_sim.project_logging import install_process_log
install_process_log('project', state)
import coat_sim.ui.settings as settings
settings.SETTINGS_PATH = state / 'settings.json'
from PySide6.QtCore import QSettings, QTimer, QPointF
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(state / 'qsettings'))
from PySide6.QtWidgets import QApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from coat_sim.ui.theme_service import configure_theme_from_settings
from coat_sim.ui.project_runtime import ProjectRuntime
from coat_sim.ui.control import UiControlServer
from coat_sim.assistant_process import LIFECYCLE_OPERATIONS
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
engine = QQmlApplicationEngine()
configure_theme_from_settings().attach_engine(engine)
runtime = ProjectRuntime(app, engine, None)
owner = runtime.owner
store = ProjectStore.create(state / 'project', key)
owner._staged[store.root] = store
workspace = owner.desktop.open_project(store.root)
owner._leases[workspace.binding.project_id] = ProjectOwnerLease.acquire(store.project_file, workspace.binding.project_id)
editor = workspace.editor
assert editor.set_design_value('name', 'UNSAVED ' + key)
window = next((view[0] for route, view in owner.desktop._views.items() if route.module_key == 'film_design'))
window.setGeometry(60, 60, 960, 650)
cover = QQuickWindow()
cover.setTitle('Owned cover ' + key)
cover.setGeometry(50, 50, 1000, 690)
runtime.bridge._project_starting = False
commits: list[bool] = []
assert runtime.bridge.project_lifecycle is not None
original_commit = runtime.bridge.project_lifecycle.gate._commit
def record_commit():
    commits.append(True)
    return original_commit()

runtime.bridge.project_lifecycle.gate._commit = record_commit

def snapshot():
    dialog = runtime.dialog_window
    buttons = {}
    for name in ('projectRuntimeCancelClose', 'projectRuntimeDiscardClose'):
        item = dialog.findChild(QQuickItem, name)
        point = item.mapToGlobal(QPointF(item.width() / 2, item.height() / 2))
        buttons[name] = [point.x(), point.y()]
    return {'editorHwnd': int(window.winId()), 'dialogHwnd': int(dialog.winId()), 'editorVisible': window.isVisible(), 'dialogVisible': dialog.isVisible(), 'reviewKind': dialog.property('reviewKind'), 'buttons': buttons, 'draft': repr(editor.snapshot()), 'dirty': editor.snapshot().dirty, 'routes': [route.route_id for route in owner.host.routes(workspace.binding.project_id)], 'commits': len(commits), 'sessions': len(owner.host.sessions()), 'enabled': window.contentItem().isEnabled()}

def dispatch(op, arguments):
    if op == 'window_action':
        action = arguments['action']
        if action == 'minimize':
            window.showMinimized()
            runtime.dialog_window.showMinimized()
            runtime.dialog_window.hide()
        elif action == 'cover':
            runtime.bridge._activate_window(cover)
        elif action == 'shutdown':
            QTimer.singleShot(0, lambda: app.exit(0))
        return snapshot()
    return runtime.application.dispatch(op, arguments)
server = UiControlServer(dispatch, root=state / 'runtime', role='project_host', operations=LIFECYCLE_OPERATIONS)
endpoint = server.start()
(state / 'ready.json').write_text(json.dumps({'endpoint': endpoint, 'generation': runtime.application.generation, 'process_start': runtime.application.process_start, 'snapshot': snapshot()}), encoding='utf-8')
app.aboutToQuit.connect(server.close)
app.aboutToQuit.connect(owner.stop)
QTimer.singleShot(35000, lambda: app.exit(71))
raise SystemExit(app.exec())
