"""Read-only file notifications propagate saved themes to independent GUI owners."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from coat_sim.ui import settings
from coat_sim.ui.theme_settings_sync import ThemeSettingsSync

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def atomic_write(path, payload):
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    from coat_sim.core.atomic_io import replace_with_retry
    replace_with_retry(temporary, path)


def test_notifications_coalesce_ignore_unrelated_changes_and_rearm_atomic_replacement(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "ui.json"
    initial = settings.normalized_ui_settings({"theme_mode": "light", "font_family": "Explicit Font"})
    atomic_write(path, initial)
    applied: list[dict[str, object]] = []
    observer = ThemeSettingsSync(path, applied.append, initial, fallback_ms=10000, debounce_ms=15)
    try:
        QTest.qWait(50)
        assert observer.read_count == 1 and not applied
        for _ in range(20):
            observer.check()
        assert observer.read_count == 1  # Unchanged file: stat only, no JSON I/O.
        for index in range(8):
            atomic_write(path, {**initial, "theme_mode": "dark", "assistant_icon_size": 48 + index})
        QTest.qWait(80)
        assert len(applied) == 1 and applied[-1]["theme_mode"] == "dark"
        reads = observer.read_count
        atomic_write(path, {**applied[-1], "assistant_icon_size": 120})
        QTest.qWait(80)
        assert observer.read_count == reads + 1 and len(applied) == 1
        atomic_write(path, {**initial, "theme_mode": "high-contrast-dark"})
        QTest.qWait(80)
        assert len(applied) == 2 and applied[-1]["font_family"] == "Explicit Font"
    finally:
        observer.stop()
    atomic_write(path, initial)
    QTest.qWait(40)
    assert len(applied) == 2
    app.processEvents()


def test_malformed_missing_and_missed_notifications_preserve_theme_and_recover(tmp_path, caplog):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "not-yet-created" / "ui.json"
    initial = settings.normalized_ui_settings({"theme_mode": "light"})
    applied: list[dict[str, object]] = []
    observer = ThemeSettingsSync(path, applied.append, initial, fallback_ms=30, debounce_ms=5)
    try:
        QTest.qWait(30)
        assert not applied and observer.last_error == "settings_missing"
        path.parent.mkdir()
        atomic_write(path, {**initial, "theme_mode": "dark"})
        # Simulate unavailable notifications; the low-frequency stat fallback recovers.
        observer._watcher.blockSignals(True)
        QTest.qWait(100)
        assert len(applied) == 1
        path.write_text("{invalid", encoding="utf-8")
        QTest.qWait(70)
        assert len(applied) == 1 and "JSONDecodeError" in observer.last_error
        assert "keeping the current theme" in caplog.text
        reads = observer.read_count
        QTest.qWait(70)
        assert observer.read_count == reads  # No repeated read/log for a bad revision.
        path.unlink()
        QTest.qWait(70)
        assert len(applied) == 1
        atomic_write(path, {**initial, "theme_mode": "light"})
        QTest.qWait(70)
        assert len(applied) == 2 and not observer.last_error
    finally:
        observer.stop()
        app.processEvents()



def test_transient_read_failure_retries_without_another_save(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "ui.json"
    initial = settings.normalized_ui_settings({"theme_mode": "light"})
    atomic_write(path, initial)
    applied: list[dict[str, object]] = []
    observer = ThemeSettingsSync(path, applied.append, initial, fallback_ms=10000, debounce_ms=5)
    try:
        QTest.qWait(30)
        original_open = Path.open
        denied: list[bool] = []
        def temporarily_locked(target, *args, **kwargs):
            if target == path and args and args[0] == "rb" and not denied:
                denied.append(True)
                raise PermissionError("synthetic temporary Windows read lock")
            return original_open(target, *args, **kwargs)
        monkeypatch.setattr(Path, "open", temporarily_locked)
        atomic_write(path, {**initial, "theme_mode": "dark"})
        QTest.qWait(280)
        assert denied and len(applied) == 1 and applied[0]["theme_mode"] == "dark"
        assert not observer.last_error
    finally:
        observer.stop()
        app.processEvents()

def wait_state(path, predicate, processes, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        for role, process, _state, _stop in processes:
            assert process.poll() is None, f"{role} exited {process.returncode}"
        try:
            last = json.loads(path.read_text(encoding="utf-8"))
            if predicate(last):
                return last
        except (OSError, ValueError):
            pass
        time.sleep(.03)
    pytest.fail(f"Timed out waiting for theme state at {path}: {last}")


def test_saved_preferences_reach_two_project_and_assistant_processes_without_launcher(tmp_path, monkeypatch):
    path = tmp_path / "preferences" / "ui.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", path)
    values = settings.save_ui_settings({"theme_mode": "light", "font_family": "DejaVu Sans", "font_point_size": 13})
    worker = Path(__file__).with_name("theme_settings_sync_worker.py")
    processes = []
    streams = []
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    def start(role):
        state = tmp_path / (role + ".json")
        stop = tmp_path / (role + ".stop")
        log = (tmp_path / (role + ".log")).open("w", encoding="utf-8")
        streams.append(log)
        process = subprocess.Popen([sys.executable, str(worker), "--settings", str(path),
            "--state", str(state), "--stop", str(stop), "--role", role], env=env,
            stdout=log, stderr=subprocess.STDOUT)
        row = (role, process, state, stop)
        processes.append(row)
        return row
    def same(mode, points):
        return lambda state: all(window["mode"] == mode and window["points"] == points
            and window["family"] == "DejaVu Sans" for window in state["windows"].values())
    try:
        for role in ("launcher", "project-one", "project-two", "assistant"):
            start(role)
        for _role, _process, state, _stop in processes:
            wait_state(state, same("light", 13), processes)
        values = settings.save_ui_settings({**values, "theme_mode": "dark", "font_point_size": 16})
        for role, _process, state, _stop in processes:
            updated = wait_state(state, same("dark", 16), processes)
            assert updated["font_family"] == "DejaVu Sans" and updated["font_points"] == 16
            if role.startswith("project"):
                assert set(updated["windows"]) == {"design", "optimization", "simulation", "assets"}
        # Install a data-only package after processes started; explicit font wins.
        package = path.parent / "themes/sync-package"
        package.mkdir(parents=True)
        manifest = {"formatVersion": 1, "id": "sync-package", "name": "Sync Package",
            "modes": {"light": {"tokenOverrides": {"colorWindow": "#F8EEDD", "fontFamily": "Package Family"}},
                      "dark": {"tokenOverrides": {"colorWindow": "#222A44", "fontFamily": "Package Family"}}},
            "sounds": {}}
        (package / "theme.json").write_text(json.dumps(manifest), encoding="utf-8")
        values = settings.save_ui_settings({**values, "theme_package_id": "sync-package"})
        for _role, _process, state, _stop in processes:
            wait_state(state, lambda data: same("dark", 16)(data) and all(
                item["theme_id"] == "sync-package" and item["surface"] == "#222a44"
                for item in data["windows"].values()), processes)
        # Close launcher only; project/assistant subscriptions own their lifetimes.
        launcher = processes.pop(0)
        launcher[3].touch()
        assert launcher[1].wait(timeout=10) == 0
        values = settings.save_ui_settings({**values, "theme_mode": "light", "font_point_size": 18})
        for _role, _process, state, _stop in processes:
            wait_state(state, same("light", 18), processes)
        late = start("project-late")
        late_state = wait_state(late[2], same("light", 18), processes)
        assert all(item["surface"] == "#f8eedd" for item in late_state["windows"].values())
        previous = {role: wait_state(state, same("light", 18), processes)["windows"]
                    for role, _p, state, _s in processes}
        # Unrelated setting saves do not invalidate any existing engine snapshot.
        settings.save_ui_settings({**values, "assistant_icon_size": 88})
        time.sleep(.3)
        for role, _process, state, _stop in processes:
            assert wait_state(state, lambda data: True, processes)["windows"] == previous[role]
        assert len({row[1].pid for row in processes}) == 4
    finally:
        for _role, _process, _state, stop in processes:
            stop.touch()
        for _role, process, _state, _stop in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        for stream in streams:
            stream.close()
