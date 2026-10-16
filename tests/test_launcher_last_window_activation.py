"""Final clean view closure retires its runtime; the launcher can reopen it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable

import pytest

from coat_sim.assistant_process import AssistantProcessClient, LIFECYCLE_OPERATIONS
from coat_sim.core.process_identity import _process_identity
from coat_sim.core.project_store import ProjectStore
from coat_sim.ui.control import OPERATIONS, UiControlClient, UiControlError
from tests.test_application_process_smoke import _SUPPORT


pytestmark = [pytest.mark.contract, pytest.mark.qt]

# Hide is distinct from .09's launcher Close. This test-only exact-route hook
# adds no production action/provider and leaves the source IPC transport intact.
_HIDE_SUPPORT = r'''
if os.environ.get('COAT_TEST_APPLICATION_SMOKE') == '1':
    import coat_sim.ui.project_application_client as last_window_application
    _last_window_dispatch = last_window_application.ProjectApplicationClient.dispatch
    def last_window_dispatch(self, operation, arguments):
        if operation == 'qa_action' and arguments.get('action') == 'hide_launcher':
            if self.role != 'launcher':
                raise control.UiControlError('invalid_test_action', 'Not a launcher')
            self.bridge._launcher_window.hide()
            return {'accepted': True}
        if operation == 'qa_action' and arguments.get('action') == 'hide_project_window':
            owner = getattr(self.bridge, 'project_runtime', None)
            fields = ('project_id', 'session_generation', 'module_key', 'instance_id')
            row = next((row for row in self._project_rows()
                        if all(row[key] == arguments.get(key) for key in fields)), None)
            if owner is None or row is None:
                raise control.UiControlError('stale_window', 'No exact project window')
            route = next(route for route in owner.host.routes(row['project_id'])
                         if route.route_id == row['instance_id'] and route.module_key == row['module_key'])
            window = owner.host.window_for(route)
            if window is None:
                raise control.UiControlError('stale_window', 'Project window already closed')
            window.hide()
            return {'accepted': True, **{key: row[key] for key in fields}}
        return _last_window_dispatch(self, operation, arguments)
    last_window_application.ProjectApplicationClient.dispatch = last_window_dispatch
'''


@pytest.mark.skipif(os.name == "nt", reason="POSIX offscreen probe; Windows needs native desktop acceptance.")
def test_last_clean_project_window_close_releases_lease_and_reopens_new_generation(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    support, state, runtime = tmp_path / "support", tmp_path / "state", tmp_path / "assistant-runtime"
    support.mkdir()
    state.mkdir()
    (support / "sitecustomize.py").write_text(_SUPPORT + _HIDE_SUPPORT, encoding="utf-8")
    (state / "allow-registration").touch()
    store = ProjectStore.create(tmp_path / "Last view", "Last view")
    project_id = store.manifest["id"]
    environment = os.environ.copy()
    for key in ("COAT_UI_PID", "COAT_UI_PARENT_PID", "COAT_UI_READY_FD", "COAT_UI_READY_TOKEN",
                "COAT_UI_READY_ADDRESS", "COAT_UI_READY_FAMILY", "COAT_UI_READY_KEY"):
        environment.pop(key, None)
    environment.update({"QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
                        "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1",
                        "COAT_TEST_APPLICATION_SMOKE": "1", "COAT_TEST_APPLICATION_STATE": str(state),
                        "COAT_ASSISTANT_RUNTIME_ROOT": str(runtime),
                        "XDG_CONFIG_HOME": str(state / "config"), "XDG_DATA_HOME": str(state / "data"),
                        "XDG_CACHE_HOME": str(state / "cache"),
                        "PYTHONPATH": os.pathsep.join((str(support), str(root)))})
    assistant = AssistantProcessClient(runtime, application_root=root, timeout=2)
    processes: list[subprocess.Popen] = []
    launcher_client: UiControlClient | None = None
    log_paths = [tmp_path / "assistant.log", tmp_path / "launcher.log"]
    streams = [path.open("wb") for path in log_paths]
    extra = {"qa_action", "qa_snapshot"}

    def diagnostics() -> str:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")[-10000:]
                         for path in log_paths)

    def eventually(read: Callable[[], Any], accept: Callable[[Any], bool], label: str) -> Any:
        deadline = time.monotonic() + 35
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = read()
                if accept(last):
                    return last
            except (UiControlError, OSError, StopIteration) as exc:
                last = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
            time.sleep(0.05)
        raise AssertionError(f"{label}: {last!r}\n{diagnostics()}")

    def client_for(pid: int) -> UiControlClient:
        return UiControlClient(state / f"client-runtime-{pid}", pid, timeout=2,
                               operations=OPERATIONS | LIFECYCLE_OPERATIONS | extra)

    def runtime_snapshot() -> tuple[dict, dict, UiControlClient]:
        owners = [row for row in assistant.list_clients() if row["role"] == "project_host"
                  and any(item["project_id"] == project_id for item in row["project_sessions"])]
        assert len(owners) <= 1, f"multiple writable project owners: {owners!r}"
        owner = next(iter(owners))
        client = client_for(owner["pid"])
        return owner, client.request("qa_snapshot"), client

    def process_retired(identity: tuple[int, str]) -> bool:
        status, start = _process_identity(identity[0])
        # A reused PID is a different process; unknown access is not proof of exit.
        return status == "dead" or status == "alive" and start is not None and start != identity[1]

    def probe_lease(*, available: bool) -> None:
        # Match _SUPPORT's actual owner root: probing a different directory could
        # falsely acquire a second pair of locks while the runtime holds its own.
        script = """\
import sys
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
store = ProjectStore.open(sys.argv[1])
try:
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest['id'], owner_runtime_root=sys.argv[2])
except ProjectError as exc:
    if exc.code != 'active_project_owner':
        raise
    print('active_project_owner')
else:
    lease.close()
    print('acquired_and_released')
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(store.project_file), str(state / "recovery-runtime")],
            cwd=root, env=environment, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == ("acquired_and_released" if available else "active_project_owner")

    try:
        assistant_process = subprocess.Popen(
            [sys.executable, "-m", "coat_sim.assistant_process", "--runtime-root", str(runtime)],
            cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=streams[0], stderr=subprocess.STDOUT)
        processes.append(assistant_process)
        eventually(assistant.status, lambda row: row["state"] == "ready", "assistant startup")
        launcher = subprocess.Popen([sys.executable, "-m", "coat_sim.ui.launcher", "--software"],
                                    cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                    stdout=streams[1], stderr=subprocess.STDOUT)
        processes.append(launcher)
        launcher_client = client_for(launcher.pid)
        eventually(lambda: launcher_client.request("qa_snapshot"),
                   lambda row: row["admission_ready"], "launcher registration")
        launcher_record = next(row for row in assistant.list_clients() if row["pid"] == launcher.pid)
        assert launcher_record["role"] == "launcher" and launcher_record["project_sessions"] == []

        def open_from_launcher() -> None:
            assert launcher_client is not None
            assert launcher_client.request("qa_action", action="open", root=str(store.project_file))["accepted"]
            result = eventually(lambda: launcher_client.request("qa_snapshot"),
                                lambda row: not row["busy"], "launcher open request settles")
            assert result["error"] == "", f"{result!r}\n{diagnostics()}"

        open_from_launcher()
        owner, opened, project_client = eventually(runtime_snapshot,
            lambda value: len(value[1]["sessions"]) == 1 and len(value[1]["rows"]) == 1
                          and value[1]["rows"][0]["visible"], "independent project view")
        assert owner["pid"] != launcher.pid and owner["state"] == "alive"
        assert opened["role"] == "project_host" and opened["dirty"] == [False]
        original = opened["rows"][0]
        original_session, original_lease = opened["sessions"][0], opened["leases"][0]
        assert original["project_id"] == project_id and original["module_key"] == "film_design"
        assert original_session["project_file"] == str(store.project_file)
        assert original_session["root"] == str(store.root)
        assert owner["project_sessions"][0]["generation"] == original["session_generation"]
        assert original_session["generation"] == original["session_generation"]
        assert original_lease["generation"] and not original_lease["closed"]
        original_identity = (owner["pid"], owner["process_start"])
        assert _process_identity(owner["pid"]) == ("alive", original_identity[1])
        probe_lease(available=False)

        assert launcher_client.request("qa_action", action="hide_launcher")["accepted"]
        assert launcher_client.request("qa_snapshot")["visible"] is False
        still_open = project_client.request("qa_snapshot")
        assert still_open["rows"] == opened["rows"] and still_open["leases"] == opened["leases"]
        assert launcher.poll() is None and assistant_process.poll() is None
        identity_fields = {key: original[key] for key in
                           ("project_id", "session_generation", "module_key", "instance_id")}
        assert project_client.request("qa_action", action="hide_project_window", **identity_fields)["accepted"]
        hidden = project_client.request("qa_snapshot")
        assert len(hidden["rows"]) == 1 and hidden["rows"][0]["visible"] is False
        assert hidden["sessions"] == opened["sessions"] and hidden["leases"] == opened["leases"]
        assert hidden["dirty"] == [False] and not hidden["close_pending"]
        probe_lease(available=False)
        activated = assistant.request("activate_project", client_id=owner["client_id"],
            client_generation=owner["generation"], project_id=project_id,
            session_generation=original["session_generation"], root=str(store.root),
            project_file=str(store.project_file))
        assert activated["activated"] and activated["instance_id"] == original["instance_id"]
        restored = eventually(lambda: project_client.request("qa_snapshot"),
            lambda row: row["rows"][0]["visible"], "hidden project activates without reopening")
        assert restored["rows"] == opened["rows"] and restored["leases"] == opened["leases"]

        # A quick clean close before the first periodic checkpoint is allowed
        # to leave no new journal. This process scenario specifically verifies
        # clean closure after a *natural* checkpoint, not scheduler timing.
        journal_path = store.root / ".project-session.json"
        checkpoint = eventually(
            lambda: json.loads(journal_path.read_text(encoding="utf-8")),
            lambda row: isinstance(row, dict) and row.get("project_id") == project_id
                        and row.get("generation") == original_lease["generation"]
                        and row.get("clean") is False,
            "natural checkpoint for the current project lease")
        assert checkpoint["kind"] == "coat_sim.session_journal" and checkpoint["version"] == 1
        assert isinstance(checkpoint["checkpoint"], dict)
        assert checkpoint["checkpoint"]["kind"] == "coat_sim.project_recovery_checkpoint"

        closed = project_client.request("close_window", client_generation=owner["generation"],
            project_root=str(store.root), project_file=str(store.project_file), **identity_fields)
        assert closed["instance_id"] == original["instance_id"] and closed["close_review"] == "native_owner"
        # A runtime which must now exit cannot be required to serve another snapshot.
        eventually(lambda: process_retired(original_identity), bool, "last clean view retires its runtime")
        clients = eventually(assistant.list_clients,
            lambda rows: not any(row["client_id"] == owner["client_id"] for row in rows),
            "closed runtime leaves the registry")
        assert not any(item["project_id"] == project_id for row in clients for item in row["project_sessions"])
        retained_launcher = next(row for row in clients if row["pid"] == launcher.pid)
        assert retained_launcher == launcher_record
        assert launcher.poll() is None and assistant_process.poll() is None
        assert launcher_client.request("qa_snapshot")["admission_ready"]
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        assert journal["project_id"] == project_id
        assert journal["generation"] == original_lease["generation"]
        assert journal["clean"] is True and journal["checkpoint"] == {}
        probe_lease(available=True)

        open_from_launcher()
        new_owner, reopened, new_client = eventually(runtime_snapshot,
            lambda value: len(value[1]["sessions"]) == 1 and len(value[1]["rows"]) == 1
                          and value[1]["rows"][0]["visible"], "new runtime opens a fresh generation")
        assert new_owner["pid"] != launcher.pid
        assert (new_owner["pid"], new_owner["process_start"]) != original_identity
        assert new_owner["client_id"] != owner["client_id"] and new_owner["generation"] != owner["generation"]
        assert reopened["error"] == "" and reopened["dirty"] == [False]
        assert reopened["rows"][0]["project_id"] == project_id
        assert reopened["rows"][0]["instance_id"] != original["instance_id"]
        assert reopened["rows"][0]["session_generation"] != original["session_generation"]
        assert reopened["leases"][0]["generation"] != original_lease["generation"]
        assert not reopened["leases"][0]["closed"]
        assert new_owner["project_sessions"][0]["generation"] == reopened["sessions"][0]["generation"]
        probe_lease(available=False)
        launcher_client.request("qa_action", action="shutdown")
        assert launcher.wait(timeout=20) == 0
        survived = new_client.request("qa_snapshot")
        assert survived["sessions"] == reopened["sessions"] and survived["leases"] == reopened["leases"]
        assert _process_identity(new_owner["pid"]) == ("alive", new_owner["process_start"])
        assert assistant_process.poll() is None
        probe_lease(available=False)
        assistant.request("request_exit_all")
        assert assistant_process.wait(timeout=30) == 0
        eventually(lambda: process_retired((new_owner["pid"], new_owner["process_start"])),
                   bool, "global exit retires remaining test runtime")
        probe_lease(available=True)
    finally:
        # Only this test's authenticated temporary endpoints are eligible for
        # graceful cleanup. Never kill a runtime or contact user-owned processes.
        if launcher_client is not None and len(processes) > 1 and processes[1].poll() is None:
            try:
                launcher_client.request("qa_action", action="shutdown")
            except (UiControlError, OSError):
                pass
            processes[1].wait(timeout=20)
        if processes and processes[0].poll() is None:
            try:
                assistant.request("request_exit_all")
            except (UiControlError, OSError):
                pass
            processes[0].wait(timeout=30)
        for stream in streams:
            stream.close()
