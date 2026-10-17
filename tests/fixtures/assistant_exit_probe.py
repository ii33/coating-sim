"""Real exit-menu clicks and sequential dirty-project native HWND checks."""
from pathlib import Path
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from PySide6.QtCore import QObject, QPointF, QMetaObject
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from coat_sim.ui.control import UiControlClient
from coat_sim.assistant_process import LIFECYCLE_OPERATIONS

def run(host, root, state):
    u = ctypes.WinDLL('user32', use_last_error=True)
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.WindowFromPoint.restype = wintypes.HWND
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = ctypes.c_long
    children, targets = ([], [])

    def wait_for(predicate, message):
        deadline = time.monotonic() + 8
        while not predicate():
            assert time.monotonic() < deadline, message
            QTest.qWait(25)

    def click(x, y, right=False):
        assert u.SetCursorPos(round(x), round(y)), 'Windows desktop input unavailable: WinError ' + str(ctypes.get_last_error())
        u.mouse_event(8 if right else 2, 0, 0, 0, 0)
        u.mouse_event(16 if right else 4, 0, 0, 0, 0)

    def action(target, name):
        return target['transport'].request('window_action', action=name)

    def start_exit():
        bubble = host.controller._bubble
        # Opening the existing QML menu is setup; Exit itself receives real
        # native input, which is the foreground permission gesture under test.
        QMetaObject.invokeMethod(bubble, 'openContextMenu')
        menu = bubble.findChild(QObject, 'assistantContextMenu')
        wait_for(lambda: menu.property('opened'), 'Owned exit menu did not open')
        item = bubble.findChild(QQuickItem, 'assistantExitApplicationItem')
        def exposed_exit_point():
            point = item.mapToGlobal(QPointF(item.width() * 0.6, item.height() / 2))
            pid = wintypes.DWORD()
            hit = u.WindowFromPoint(wintypes.POINT(round(point.x()), round(point.y())))
            u.GetWindowThreadProcessId(hit, ctypes.byref(pid))
            return point if pid.value == os.getpid() else None
        wait_for(lambda: exposed_exit_point() is not None, 'Owned native menu input surface not exposed')
        point = exposed_exit_point()
        assert point is not None
        click(point.x(), point.y())
    results = []

    def confirmation(target, case):
        wait_for(lambda: action(target, 'snapshot')['dialogVisible'], 'Missing exit confirmation')
        snapshot = action(target, 'snapshot')
        hwnd = snapshot['dialogHwnd']
        try:
            wait_for(lambda: int(u.GetForegroundWindow()) == hwnd, 'Confirmation not actual foreground: ' + str(snapshot))
        except AssertionError:
            (state / 'foreground-failure.json').write_text(json.dumps({'case': case, 'targetPid': target['endpoint']['pid'], 'targetHwnd': hwnd, 'foregroundHwnd': int(u.GetForegroundWindow()), 'dialogVisible': snapshot['dialogVisible']}), encoding='utf-8')
            raise
        QTest.qWait(100)
        sample = {'case': case, 'targetPid': target['endpoint']['pid'], 'editorHwnd': snapshot['editorHwnd'], 'confirmationHwnd': hwnd, 'foregroundHwnd': int(u.GetForegroundWindow()), 'restoredEditor': not bool(u.IsIconic(snapshot['editorHwnd'])), 'restoredConfirmation': not bool(u.IsIconic(hwnd)), 'flagsPreserved': u.GetWindowLongW(snapshot['editorHwnd'], -20) == target['flags'], 'draftPreserved': snapshot['draft'] == target['before']['draft']}
        results.append(sample)
        (state / 'exit-cases.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        assert sample['foregroundHwnd'] == hwnd and sample['restoredEditor'] and sample['restoredConfirmation']
        assert sample['flagsPreserved'] and sample['draftPreserved']
        if len(results) == 1:
            import PySide6.QtGui as gui
            assert gui.QGuiApplication.primaryScreen().grabWindow(hwnd).save(str(state / 'exit-confirmation.png'))
        return snapshot

    def choose(snapshot, accept=False):
        click(*snapshot['buttons']['projectRuntimeDiscardClose' if accept else 'projectRuntimeCancelClose'])
    try:
        for key in ('first', 'second'):
            directory = state / key
            directory.mkdir()
            child = subprocess.Popen([str(Path(sys.executable).with_name('pythonw.exe')), '-B', str(root / 'tests/fixtures/assistant_exit_target.py'), str(root), str(directory), key], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(child)
            wait_for(lambda: (directory / 'ready.json').exists() or child.poll() is not None, 'Target readiness timeout')
            assert child.poll() is None, str(directory / 'artifacts/logs/project.log')
            ready = json.loads((directory / 'ready.json').read_text())
            endpoint = ready['endpoint']
            transport = UiControlClient(endpoint['root'], endpoint['pid'], initial_endpoint=endpoint, operations=LIFECYCLE_OPERATIONS)
            target = {**ready, 'transport': transport, 'before': ready['snapshot'], 'flags': u.GetWindowLongW(ready['snapshot']['editorHwnd'], -20)}
            targets.append(target)
        first, second = targets
        host.registry.register_client(first['endpoint'], generation=first['generation'], process_start=first['process_start'])
        for case in ('normal', 'minimized', 'occluded'):
            if case != 'normal':
                action(first, 'minimize' if case == 'minimized' else 'cover')
            start_exit()
            snapshot = confirmation(first, case)
            choose(snapshot)
            wait_for(lambda: host._worker is None, 'Cancel did not finish exit')
            assert host._exit['state'] == 'declined', host._exit
            after = action(first, 'snapshot')
            assert after['draft'] == first['before']['draft'] and after['routes'] == first['before']['routes']
            assert after['commits'] == 0 and after['sessions'] == 1 and after['enabled']
        host.registry.register_client(second['endpoint'], generation=second['generation'], process_start=second['process_start'])
        action(first, 'minimize')
        action(second, 'minimize')
        start_exit()
        snapshot = confirmation(first, 'sequential-first')
        assert not action(second, 'snapshot')['dialogVisible'], 'Second confirmation was not sequential'
        choose(snapshot, accept=True)
        snapshot = confirmation(second, 'sequential-second-after-real-input')
        assert not action(first, 'snapshot')['enabled']
        choose(snapshot)
        wait_for(lambda: host._worker is None, 'Second Cancel did not release first freeze')
        assert host._exit['state'] == 'declined'
        for target in targets:
            after = action(target, 'snapshot')
            assert after['commits'] == 0 and after['enabled'] and after['dirty']
            assert after['draft'] == target['before']['draft'] and after['routes'] == target['before']['routes']
        return {'exit_cases': results, 'cancel_preserved_both_drafts': True, 'sequential_clients': True}
    finally:
        for target in targets:
            action(target, 'shutdown')
        for child in children:
            child.wait(timeout=40)
