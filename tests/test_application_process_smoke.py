"""Real isolated detached-runtime smoke with test-only settings/network guards.

The child runtimes, readiness protocol, registry and owner processes use their
source implementations; only external state roots and non-loopback IP traffic
are constrained. The application's original AF_UNIX transport is untouched.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import subprocess
import shutil
import sys
from threading import Barrier
import time
from typing import Any, Callable

import pytest

from coat_sim.assistant_process import AssistantProcessClient, LIFECYCLE_OPERATIONS
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.process_identity import _process_identity
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.ui.control import OPERATIONS, UiControlClient, UiControlError

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def _launcher_open_outcome(snapshot: dict[str, Any], descriptor: str) -> str:
    """Classify this launcher's request, never its shared history snapshot."""
    if snapshot["busy"]:
        return "pending"
    error = snapshot["open_error"]
    if error:
        return "retryable_error" if "重试" in error or "刷新" in error else "failed"
    return "opened" if snapshot["selected_file"] == descriptor else "unconfirmed"


def _journal_recovery_state(journal: dict[str, Any]) -> dict[str, Any]:
    """Ignore only the valid journal write timestamp, not recovery semantics."""
    assert isinstance(journal["updated_at"], str)
    assert datetime.fromisoformat(journal["updated_at"]).tzinfo is not None
    return {key: value for key, value in journal.items() if key != "updated_at"}


def _shutdown_review_pending(snapshot: dict[str, Any]) -> bool:
    """A retained dialog label alone is not a current exit decision."""
    return snapshot.get("review_kind") == "shutdown" and snapshot.get("pending_shutdown") is True


def _child_log_tail(path: Path) -> str:
    """Keep a native crash report visible even for an ordinary exit assertion."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 65536))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError as exc:
        tail = f"Child log could not be read: {exc}"
    return f"Child log: {path}\n--- last 64 KiB ---\n{tail}"


def _assert_clean_child_exit(process: subprocess.Popen, log_path: Path, *, timeout: float) -> None:
    code = process.wait(timeout=timeout)
    assert code == 0, f"Child pid={process.pid} exited with {code}; required 0.\n{_child_log_tail(log_path)}"


def _preserve_child_logs(paths: list[Path], junit_path: str | None) -> None:
    """CI already uploads the JUnit directory; retain complete original logs there."""
    if not junit_path:
        return
    destination = Path(junit_path).resolve().parent / "application-process-smoke-children"
    destination.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.is_file():
            shutil.copyfile(path, destination / path.name)


_SUPPORT = r'''import os
from pathlib import Path
import socket
import time

if os.environ.get('COAT_TEST_APPLICATION_SMOKE') == '1':
    state = Path(os.environ['COAT_TEST_APPLICATION_STATE']).resolve()
    # Reject non-loopback IP networking while leaving the application's actual
    # AF_UNIX control and ready-channel transports unchanged.
    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex
    _sendto = socket.socket.sendto
    def _loopback(address):
        return (not isinstance(address, tuple) or
                (bool(address) and address[0] in ('127.0.0.1', '::1', 'localhost')))
    def connect(self, address):
        if not _loopback(address):
            raise AssertionError('Non-loopback networking is forbidden in this smoke')
        return _connect(self, address)
    def connect_ex(self, address):
        if not _loopback(address):
            raise AssertionError('Non-loopback networking is forbidden in this smoke')
        return _connect_ex(self, address)
    def sendto(self, data, *args):
        address = args[-1] if args else None
        if not _loopback(address):
            raise AssertionError('Non-loopback networking is forbidden in this smoke')
        return _sendto(self, data, *args)
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto

    import coat_sim.ui.settings as settings
    settings.SETTINGS_PATH = state / 'ui_settings.json'
    configuration = settings.normalized_ui_settings({
        'assistant_enabled': True, 'assistant_api_url': '', 'assistant_model': '',
        'assistant_notification_sound': False,
    })
    settings.load_ui_settings = lambda: dict(configuration)
    settings.save_ui_settings = lambda values: dict(values)

    import coat_sim.agent.storage as storage
    _init = storage.AssistantStore.__init__
    _original_path = storage.STATE_PATH
    def store_init(self, path=_original_path):
        _init(self, state / 'assistant.sqlite3' if Path(path) == _original_path else path)
    storage.AssistantStore.__init__ = store_init

    import coat_sim.project_recovery as recovery
    recovery.project_owner_runtime_root = lambda: state / 'recovery-runtime'
    import coat_sim.ui.assistant_desktop as desktop
    desktop.CURSOR_PATH = state / 'desktop-cursor.json'
    desktop.QSoundEffect = None
    desktop.DesktopAssistantBridge.refreshQueue = lambda self: None

    import coat_sim.ui.control as control
    _server_init = control.UiControlServer.__init__
    def server_init(self, dispatch, **kwargs):
        role = kwargs.get('role', 'launcher')
        # A unique endpoint root per test-owned GUI process. The assistant has
        # its separate explicit runtime root.
        kwargs['operations'] = kwargs.get('operations', control.OPERATIONS) | {'qa_action', 'qa_snapshot'}
        if role != 'assistant':
            kwargs['root'] = state / ('client-runtime-' + str(os.getpid()))
        _server_init(self, dispatch, **kwargs)
    control.UiControlServer.__init__ = server_init

    from coat_sim.assistant_process import AssistantProcessClient
    _register_client = AssistantProcessClient.register_client
    def register_client(self, *args, **kwargs):
        deadline = time.monotonic() + 30
        while not (state / 'allow-registration').exists():
            if time.monotonic() >= deadline:
                raise control.UiControlError('test_registration_timeout', 'Registration barrier was not released')
            time.sleep(0.05)
        return _register_client(self, *args, **kwargs)
    AssistantProcessClient.register_client = register_client

    import coat_sim.ui.project_application_client as application
    _dispatch = application.ProjectApplicationClient.dispatch
    def _runtime_dialog(bridge):
        engine = getattr(bridge, 'engine', None)
        if engine is None:
            return None
        return next((row for row in engine.rootObjects()
                     if getattr(row, 'objectName', lambda: '')() == 'projectRuntimeDialogs'), None)
    def dispatch(self, operation, arguments):
        if operation == 'qa_action':
            action = arguments['action']
            if self.role == 'launcher':
                if action == 'open':
                    return {'accepted': self.bridge.project_launcher.openProject(arguments['root'])}
                if action == 'refresh_history':
                    self.bridge.project_launcher.refreshProjects()
                    return {'accepted': True}
                if action == 'shutdown':
                    self.bridge.shutdown()
                    return {'accepted': True}
                # Project edits must target the already authenticated runtime.
                # A shared-registry query here would challenge this GUI while
                # its dispatch is blocked waiting for that same query.
                raise control.UiControlError('invalid_test_action', 'Unsupported launcher QA action')
            owner = self.bridge.project_runtime
            sessions = owner.host.sessions()
            project_id = arguments.get('project_id')
            session = next((row for row in sessions if not project_id or row.binding.project_id == project_id), None)
            if action == 'dirty':
                if session is None:
                    return {'accepted': False}
                from coat_sim.core.project_film_session import ProjectFilmWorkspace
                workspace = ProjectFilmWorkspace.for_project(owner.host, session.binding)
                return {'accepted': workspace.editor.set_design_value('name', arguments.get('name', 'Smoke dirty draft'))}
            if action == 'close_project':
                return {'accepted': bool(session and owner.closeProject(session.binding.project_id))}
            if action == 'cancel_close':
                if session is not None:
                    owner.cancelCloseProject(session.binding.project_id)
                return {'accepted': True}
            if action == 'return_to_save':
                return {'accepted': bool(session and owner.returnToSaveProject(session.binding.project_id))}
            if action == 'discard_close':
                return {'accepted': bool(session and owner.confirmCloseProject(session.binding.project_id))}
            if action == 'cancel_shutdown':
                dialog = _runtime_dialog(self.bridge)
                if dialog is not None:
                    dialog.hide()
                self.bridge.cancelShutdown()
                return {'accepted': True}
            if action == 'confirm_shutdown':
                dialog = _runtime_dialog(self.bridge)
                if dialog is not None:
                    dialog.hide()
                self.bridge.confirmShutdown()
                return {'accepted': True}
            raise control.UiControlError('invalid_test_action', 'Unsupported project runtime QA action')
        if operation == 'qa_snapshot':
            if self.role == 'launcher':
                launcher = self.bridge.project_launcher
                return {'pid': os.getpid(), 'role': self.role,
                        'visible': self.bridge._launcher_window.isVisible(),
                        'admission_ready': self.admission.ready,
                        'busy': launcher.projectBusy, 'error': launcher.projectError,
                        'selected_file': launcher._selected_file, 'open_error': launcher._error,
                        'open_error_code': getattr(launcher, '_qa_open_error_code', ''),
                        'history_busy': launcher.recentBusy, 'history_error': launcher._history_error,
                        'recent': launcher.projectEntries}
            owner = self.bridge.project_runtime
            session_rows = []
            dirty = []
            film_drafts = []
            leases = []
            for session in owner.host.sessions():
                # Observation must not enter operation admission or construct
                # a workspace/editor while the real exit gate is frozen.
                workspace = owner.host.resource_for(session.binding, 'film_workspace')
                editor = getattr(workspace, '_editor', None)
                if editor is None:
                    raise control.UiControlError('test_snapshot_unavailable', 'Existing film draft is not initialized')
                store = owner.host.store_for(session.binding)
                session_rows.append({'project_id': session.binding.project_id,
                                     'generation': session.binding.generation,
                                     'root': str(session.binding.root),
                                     'project_file': str(store.project_file)})
                draft = editor.snapshot()
                dirty.append(draft.dirty)
                film_drafts.append({'project_id': session.binding.project_id,
                                    'session_generation': session.binding.generation,
                                    'design': draft.design})
                lease = owner._leases.get(session.binding.project_id)
                leases.append({'project_id': session.binding.project_id,
                               'generation': lease.generation if lease else '',
                               'closed': lease is None or lease._closed})
            dialog = _runtime_dialog(self.bridge)
            close_id = str(dialog.property('closeProjectId')) if dialog is not None else ''
            review_kind = str(dialog.property('reviewKind')) if dialog is not None else ''
            lifecycle = self.bridge.project_lifecycle
            return {'pid': os.getpid(), 'role': self.role,
                    'sessions': session_rows, 'rows': self._project_rows(), 'dirty': dirty,
                    'film_drafts': film_drafts,
                    'leases': leases, 'busy': owner.projectBusy, 'error': owner.projectError,
                    'review_kind': review_kind, 'close_project_id': close_id,
                    'close_pending': bool(close_id and close_id in owner.project_closer._reviews),
                    'pending_shutdown': self.bridge._shutdown_confirmation_pending,
                    'gate_active': lifecycle.active if lifecycle else False,
                    'gate_state': lifecycle.gate._state if lifecycle else 'unavailable',
                    'frozen': bool(lifecycle and lifecycle.active and
                                   any(not window.isEnabled() for window, _ in lifecycle._windows))}
        return _dispatch(self, operation, arguments)
    application.ProjectApplicationClient.dispatch = dispatch

    # The adapter normally exposes a concise UI error; preserve the original
    # exception trace in this test-only process log when a real child startup fails.
    import traceback
    import coat_sim.ui.launcher_projects as launcher_projects
    _complete_pending = launcher_projects.ProjectLauncherAdapter._complete_pending
    def trace_open_failure(self):
        pending = self._pending
        if pending is not None and pending.done():
            try:
                pending.result()
            except BaseException as exc:
                self._qa_open_error_code = getattr(exc, 'code', '')
                traceback.print_exc()
            else:
                self._qa_open_error_code = ''
        return _complete_pending(self)
    launcher_projects.ProjectLauncherAdapter._complete_pending = trace_open_failure

    import coat_sim.ui.assistant_process_host as assistant_host
    _assistant_dispatch = assistant_host.AssistantProcessHost.dispatch
    def assistant_dispatch(self, operation, arguments):
        if operation == 'qa_action' and arguments.get('action') == 'activate':
            return {'accepted': self.controller.desktop.activateWindow(arguments['identity'])}
        if operation == 'qa_action' and arguments.get('action') == 'close':
            return {'accepted': self.controller.desktop.closeWindow(arguments['identity'])}
        if operation == 'qa_snapshot':
            self.controller.desktop.startWindowListTracking()
            return {'rows': self.controller.desktop.openWindows,
                    'frozen': self.controller.bridge._application_exit_frozen,
                    'chat_enabled': self.controller._chat.contentItem().isEnabled()}
        return _assistant_dispatch(self, operation, arguments)
    assistant_host.AssistantProcessHost.dispatch = assistant_dispatch
'''


@pytest.mark.skipif(os.name == "nt", reason="POSIX offscreen smoke; Windows uses real desktop acceptance.")
def test_detached_project_runtime_ownership_across_launcher_lifetimes(
        tmp_path: Path, request: pytest.FixtureRequest) -> None:
    root = Path(os.environ.get("COAT_SMOKE_SOURCE_ROOT", Path(__file__).resolve().parents[1])).resolve()
    if not (root / "coat_sim/ui/project_runtime.py").is_file():
        pytest.skip("Detached project runtime is not present in this source checkout")
    support, state = tmp_path / "support", tmp_path / "state"
    assistant_root = tmp_path / "assistant-runtime"
    support.mkdir()
    state.mkdir()
    (support / "sitecustomize.py").write_text(_SUPPORT, encoding="utf-8")
    projects = [ProjectStore.create(tmp_path / f"Smoke Project {index}", f"Smoke Project {index}")
                for index in range(1, 4)]
    env = os.environ.copy()
    for key in ("COAT_UI_PID", "COAT_UI_PARENT_PID", "COAT_UI_READY_FD", "COAT_UI_READY_TOKEN",
                "COAT_UI_READY_ADDRESS", "COAT_UI_READY_FAMILY", "COAT_UI_READY_KEY"):
        env.pop(key, None)
    env.update({"QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
                "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1",
                "COAT_TEST_APPLICATION_SMOKE": "1", "COAT_TEST_APPLICATION_STATE": str(state),
                "COAT_ASSISTANT_RUNTIME_ROOT": str(assistant_root),
                "XDG_CACHE_HOME": str(state / "cache"),
                "PYTHONPATH": os.pathsep.join((str(support), str(root)))})
    assistant = AssistantProcessClient(assistant_root, application_root=root, timeout=3)
    extra = {"qa_action", "qa_snapshot"}
    launchers: list[subprocess.Popen] = []
    streams = []
    log_paths = [tmp_path / "assistant.log"]
    assistant_process: subprocess.Popen | None = None
    assistant_stream = None

    def launcher_client(process: subprocess.Popen) -> UiControlClient:
        return UiControlClient(state / f"client-runtime-{process.pid}", process.pid,
                               operations=OPERATIONS | LIFECYCLE_OPERATIONS | extra, timeout=2)

    def start_launcher() -> tuple[subprocess.Popen, UiControlClient]:
        path = tmp_path / f"launcher-{len(launchers) + 1}.log"
        log_paths.append(path)
        stream = path.open("wb")
        streams.append(stream)
        process = subprocess.Popen([sys.executable, "-X", "faulthandler", "-m", "coat_sim.ui.launcher", "--software"],
                                   cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT)
        launchers.append(process)
        return process, launcher_client(process)

    def eventually(read: Callable[[], Any], accept: Callable[[Any], bool], label: str,
                   timeout: float = 45) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = read()
                if accept(last):
                    return last
            except (UiControlError, OSError, StopIteration) as exc:
                last = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
            time.sleep(0.1)
        diagnostics = "\n".join(path.read_text(encoding="utf-8", errors="replace")[-10000:]
                                 for path in log_paths if path.exists())
        raise AssertionError(f"{label} did not settle: {last!r}\n{diagnostics}")

    def registered_clients() -> list[dict[str, Any]]:
        return assistant.list_clients()

    def project_client(project_id: str) -> tuple[dict[str, Any], UiControlClient]:
        row = next(client for client in registered_clients()
                   if client["role"] == "project_host" and
                   any(item["project_id"] == project_id for item in client["project_sessions"]))
        transport = UiControlClient(state / f"client-runtime-{row['pid']}", row["pid"],
            operations=OPERATIONS | LIFECYCLE_OPERATIONS | extra, timeout=2)
        return row, transport

    def runtime_snapshot(project_id: str) -> tuple[dict[str, Any], dict[str, Any], UiControlClient]:
        process_row, client = project_client(project_id)
        return process_row, client.request("qa_snapshot"), client

    def confirmed_open(client: UiControlClient, descriptor: str) -> dict[str, Any]:
        snapshot = eventually(lambda: client.request("qa_snapshot"),
                              lambda row: not row["busy"], "project open request completion")
        assert _launcher_open_outcome(snapshot, descriptor) == "opened", snapshot
        return snapshot

    def recent_visible(client: UiControlClient, descriptor: str) -> dict[str, Any]:
        # Metadata is independently asynchronous (and shared by all launchers).
        # Refresh is a real picker action, not a replacement for open admission.
        def read() -> dict[str, Any]:
            snapshot = client.request("qa_snapshot")
            if not snapshot["history_busy"] and not any(
                    row["descriptorFile"] == descriptor for row in snapshot["recent"]):
                assert client.request("qa_action", action="refresh_history")["accepted"]
            return snapshot
        return eventually(read, lambda row: not row["history_busy"] and any(
            item["descriptorFile"] == descriptor for item in row["recent"]),
            "independent recent-history refresh")

    def live(pid: int) -> bool:
        state_name, _start = _process_identity(pid)
        return state_name == "alive"

    def answer_global_prompts(project_ids: list[str], *, timeout: float = 25) -> list[int]:
        """Confirm only dialogs of project hosts registered by this smoke."""
        answered: list[int] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = []
            for project_id in project_ids:
                try:
                    row, snapshot, client = runtime_snapshot(project_id)
                except (StopIteration, UiControlError):
                    continue
                if _shutdown_review_pending(snapshot):
                    pending.append((row, client))
            for row, client in pending:
                client.request("qa_action", action="confirm_shutdown")
                answered.append(row["pid"])
            if assistant_process is not None and assistant_process.poll() is not None:
                return answered
            time.sleep(0.1)
        return answered

    try:
        assistant_stream = log_paths[0].open("wb")
        assistant_process = subprocess.Popen(
            [sys.executable, "-X", "faulthandler", "-m", "coat_sim.assistant_process", "--runtime-root", str(assistant_root)],
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=assistant_stream,
            stderr=subprocess.STDOUT, start_new_session=True)
        streams.append(assistant_stream)
        assistant_identity = eventually(assistant.status,
            lambda result: result["bubble_created"] and result["chat_created"], "shared assistant UI")
        assert assistant_identity["pid"] == assistant_process.pid
        assistant_qa = UiControlClient(assistant_root, assistant_process.pid,
            operations={"qa_action", "qa_snapshot"}, endpoint_prefix="assistant", timeout=2)

        first_launcher, first_client = start_launcher()
        second_launcher, second_client = start_launcher()
        first_waiting = eventually(lambda: first_client.request("qa_snapshot"),
                                   lambda _row: True, "first responsive launcher before registration")
        second_waiting = eventually(lambda: second_client.request("qa_snapshot"),
                                    lambda _row: True, "second responsive launcher before registration")
        assert first_waiting["role"] == second_waiting["role"] == "launcher"
        assert not first_waiting["admission_ready"] and not second_waiting["admission_ready"]
        (state / "allow-registration").touch()
        launcher_record = eventually(registered_clients,
            lambda rows: all(any(row["pid"] == process.pid and row["role"] == "launcher" for row in rows)
                             for process in (first_launcher, second_launcher)),
            "both authenticated launcher registrations")
        eventually(lambda: first_client.request("qa_snapshot"), lambda row: row["admission_ready"],
                   "first launcher GUI admission")
        eventually(lambda: second_client.request("qa_snapshot"), lambda row: row["admission_ready"],
                   "second launcher GUI admission")
        assert any(row["pid"] == first_launcher.pid for row in launcher_record)
        assert any(row["pid"] == second_launcher.pid for row in launcher_record)

        # Exercise the TOCTOU window, rather than merely reopening an already
        # registered owner: both detached launchers request the same descriptor
        # at once. Their independent source runtime startups must converge on
        # one registered project_host and one OS write lease. The loser may
        # activate that owner or report a retryable refusal, never create a
        # second writer.
        start_gate = Barrier(3)
        def simultaneous_open(client: UiControlClient) -> dict[str, Any]:
            start_gate.wait(timeout=10)
            return client.request("qa_action", action="open", root=str(projects[0].project_file))
        with ThreadPoolExecutor(max_workers=2) as pool:
            open_first = pool.submit(simultaneous_open, first_client)
            open_second = pool.submit(simultaneous_open, second_client)
            start_gate.wait(timeout=10)
            race_results = [open_first.result(timeout=15), open_second.result(timeout=15)]
        assert all(result["accepted"] for result in race_results), race_results
        race_settled = eventually(
            lambda: [first_client.request("qa_snapshot"), second_client.request("qa_snapshot")],
            lambda rows: all(not row["busy"] for row in rows),
            "both simultaneous open requests settle")
        race_owners = [row for row in registered_clients() if row["role"] == "project_host"
                       and any(item["project_id"] == projects[0].manifest["id"]
                               for item in row["project_sessions"])]
        assert race_owners, ("simultaneous open produced no registered project owner; "
                             f"launcher outcomes={race_settled!r}")

        runtime_a_row, runtime_a, runtime_a_client = eventually(
            lambda: runtime_snapshot(projects[0].manifest["id"]),
            lambda value: (value[1]["sessions"] and value[1]["rows"] and
                           any(row["module_key"] == "film_design" and row["visible"]
                               for row in value[1]["rows"])),
            "actual film designer window in project owner")
        assert runtime_a_row["pid"] != first_launcher.pid
        assert runtime_a_row["pid"] != second_launcher.pid
        assert runtime_a_row["role"] == "project_host"
        assert runtime_a["sessions"][0]["generation"] == runtime_a_row["project_sessions"][0]["generation"]
        descriptor = str(projects[0].project_file)
        outcomes = [_launcher_open_outcome(row, descriptor) for row in race_settled]
        assert all(outcome in {"opened", "retryable_error"} for outcome in outcomes), race_settled
        assert "opened" in outcomes, f"neither opener confirmed success: {race_settled!r}"
        race_owners = [row for row in registered_clients() if row["role"] == "project_host"
                       and any(item["project_id"] == projects[0].manifest["id"]
                               for item in row["project_sessions"])]
        assert len(race_owners) == 1, f"same-descriptor race created multiple project owners: {race_owners!r}"
        assert race_owners[0]["pid"] == runtime_a_row["pid"]
        for client, outcome in zip((first_client, second_client), outcomes, strict=True):
            if outcome == "opened":
                recent_visible(client, descriptor)
        # An independent acquisition must be rejected while the winner's
        # generation is alive, proving the single OS-level writer lease too.
        try:
            competing_lease = ProjectOwnerLease.acquire(
                projects[0].project_file, projects[0].manifest["id"],
                owner_runtime_root=state / "recovery-runtime")
        except Exception as exc:
            assert getattr(exc, "code", None) == "active_project_owner", exc
        else:
            competing_lease.close(clean=False)
            raise AssertionError("same-descriptor race did not retain the winning write lease")
        assert runtime_a_client.request("qa_action", action="dirty",
                                    project_id=projects[0].manifest["id"])["accepted"]
        runtime_a_row, runtime_a, runtime_a_client = runtime_snapshot(projects[0].manifest["id"])
        assert runtime_a["dirty"] == [True]
        original_a_pid = runtime_a_row["pid"]
        original_a_generation = runtime_a["sessions"][0]["generation"]
        original_a_lease = runtime_a["leases"][0]["generation"]
        original_a_drafts = runtime_a["film_drafts"]
        assert len(original_a_drafts) == 1
        assert original_a_drafts[0]["project_id"] == projects[0].manifest["id"]
        assert original_a_drafts[0]["session_generation"] == original_a_generation
        assert original_a_drafts[0]["design"]["name"] == "Smoke dirty draft"
        original_journal = Path(runtime_a["sessions"][0]["root"]) / ".project-session.json"
        # Record the actual draft only after its natural periodic checkpoint.
        # Neither a successful open nor a dirty editor implies that the first
        # recovery journal write has already completed.
        journal_before = eventually(
            lambda: json.loads(original_journal.read_text(encoding="utf-8")),
            lambda row: row.get("project_id") == projects[0].manifest["id"]
                and row.get("generation") == original_a_lease and row.get("clean") is False
                and row.get("checkpoint", {}).get("film", {}).get("dirty") is True
                and row.get("checkpoint", {}).get("film", {}).get("film", {}).get("design", {}).get("name")
                    == "Smoke dirty draft",
            "natural checkpoint of the exact current dirty project lease")
        assert journal_before["checkpoint"]["film"]["film"]["design"] == original_a_drafts[0]["design"]

        first_client.request("qa_action", action="shutdown")
        _assert_clean_child_exit(first_launcher, log_paths[1], timeout=20)
        still_owned = eventually(lambda: runtime_a_client.request("qa_snapshot"),
            lambda row: row["sessions"] and row["dirty"] == [True],
            "runtime survives launcher exit with original draft")
        assert still_owned["sessions"][0]["generation"] == original_a_generation
        assert still_owned["leases"][0]["generation"] == original_a_lease
        assert still_owned["film_drafts"] == original_a_drafts
        journal_after = json.loads(original_journal.read_text(encoding="utf-8"))
        assert _journal_recovery_state(journal_after) == _journal_recovery_state(journal_before)
        assert live(original_a_pid) and live(assistant_process.pid)
        clients_after_launcher = eventually(registered_clients,
            lambda rows: not any(row["pid"] == first_launcher.pid for row in rows)
                         and any(row["pid"] == original_a_pid for row in rows),
            "registry drops only exited launcher and retains runtime")

        assert live(second_launcher.pid)
        assert second_client.request("qa_action", action="open",
                                     root=str(projects[0].project_file))["accepted"]
        confirmed_open(second_client, str(projects[0].project_file))
        recent_visible(second_client, str(projects[0].project_file))
        owner_a_after_reopen_row, owner_a_after_reopen, runtime_a_client = eventually(
            lambda: runtime_snapshot(projects[0].manifest["id"]),
            lambda value: bool(value[1]["sessions"] and value[1]["rows"]),
            "existing project owner remains registered")
        assert owner_a_after_reopen_row["pid"] == original_a_pid
        assert owner_a_after_reopen["sessions"][0]["generation"] == original_a_generation
        assert owner_a_after_reopen["leases"][0]["generation"] == original_a_lease
        assert owner_a_after_reopen["dirty"] == [True]
        assert owner_a_after_reopen["film_drafts"] == original_a_drafts
        assert len([row for row in clients_after_launcher if row["role"] == "project_host"
                    and any(item["project_id"] == projects[0].manifest["id"]
                            for item in row["project_sessions"])]) == 1

        assert second_client.request("qa_action", action="open",
                                     root=str(projects[1].project_file))["accepted"]
        confirmed_open(second_client, str(projects[1].project_file))
        recent_visible(second_client, str(projects[1].project_file))
        runtime_b_row, runtime_b, runtime_b_client = eventually(
            lambda: runtime_snapshot(projects[1].manifest["id"]),
            lambda value: bool(value[1]["sessions"] and any(
                row["module_key"] == "film_design" and row["visible"] for row in value[1]["rows"])),
            "independent second project runtime")
        assert runtime_b_row["pid"] != original_a_pid
        assert runtime_b["sessions"][0]["generation"] != original_a_generation
        assert runtime_b_client.request("qa_action", action="dirty",
                                     project_id=projects[1].manifest["id"])["accepted"]
        runtime_b_row, runtime_b, runtime_b_client = runtime_snapshot(projects[1].manifest["id"])
        assert runtime_b["dirty"] == [True]
        assert runtime_b_row["pid"] != runtime_a_row["pid"]

        assert runtime_a_client.request("qa_action", action="close_project",
                                        project_id=projects[0].manifest["id"])["accepted"] is False
        pending_close = eventually(lambda: runtime_a_client.request("qa_snapshot"),
                                   lambda row: row["close_pending"], "dirty close review")
        assert pending_close["review_kind"] == "project"
        runtime_a_client.request("qa_action", action="cancel_close", project_id=projects[0].manifest["id"])
        cancelled_close = runtime_a_client.request("qa_snapshot")
        assert not cancelled_close["close_pending"] and cancelled_close["dirty"] == [True]
        assert cancelled_close["film_drafts"] == original_a_drafts
        assert runtime_a_client.request("qa_action", action="close_project",
                                        project_id=projects[0].manifest["id"])["accepted"] is False
        eventually(lambda: runtime_a_client.request("qa_snapshot"), lambda row: row["close_pending"],
                   "second dirty close review")
        assert runtime_a_client.request("qa_action", action="return_to_save",
                                        project_id=projects[0].manifest["id"])["accepted"]
        returned_to_save = eventually(lambda: runtime_a_client.request("qa_snapshot"),
            lambda row: not row["close_pending"] and row["dirty"] == [True] and
                        any(item["module_key"] == "film_design" and item["visible"] for item in row["rows"]),
            "Return to Save preserves the draft and reopens its editor")
        assert returned_to_save["sessions"][0]["generation"] == original_a_generation
        assert returned_to_save["film_drafts"] == original_a_drafts
        assert live(original_a_pid)

        assert runtime_a_client.request("qa_action", action="close_project",
                                        project_id=projects[0].manifest["id"])["accepted"] is False
        eventually(lambda: runtime_a_client.request("qa_snapshot"), lambda row: row["close_pending"],
                   "discard close review")
        assert runtime_a_client.request("qa_action", action="discard_close",
                                        project_id=projects[0].manifest["id"])["accepted"]
        eventually(lambda: live(original_a_pid), lambda is_alive: not is_alive,
                   "discard closes only project A runtime")
        assert live(runtime_b_row["pid"]) and live(assistant_process.pid)
        # Lock acquisition by this test proves the discarded runtime released ownership.
        lease_probe = ProjectOwnerLease.acquire(projects[0].project_file, projects[0].manifest["id"],
                                                owner_runtime_root=state / "recovery-runtime")
        lease_probe.close(clean=False)
        b_survives = runtime_b_client.request("qa_snapshot")
        assert b_survives["sessions"] and b_survives["dirty"] == [True]

        assert second_client.request("qa_action", action="open",
                                     root=str(projects[2].project_file))["accepted"]
        confirmed_open(second_client, str(projects[2].project_file))
        recent_visible(second_client, str(projects[2].project_file))
        runtime_c_row, runtime_c, runtime_c_client = eventually(
            lambda: runtime_snapshot(projects[2].manifest["id"]),
            lambda value: bool(value[1]["sessions"] and any(
                row["module_key"] == "film_design" and row["visible"] for row in value[1]["rows"])),
            "independent third project runtime")
        assert runtime_c_row["pid"] not in {original_a_pid, runtime_b_row["pid"]}
        assert runtime_c_client.request("qa_action", action="dirty",
                                     project_id=projects[2].manifest["id"])["accepted"]
        runtime_c_row, runtime_c, runtime_c_client = runtime_snapshot(projects[2].manifest["id"])
        assert runtime_c["dirty"] == [True]
        second_client.request("qa_action", action="shutdown")
        _assert_clean_child_exit(second_launcher, log_paths[2], timeout=20)
        assert live(runtime_b_row["pid"]) and live(runtime_c_row["pid"])

        def shutdown_snapshots() -> list[tuple[str, dict[str, Any], UiControlClient]]:
            # Both exact runtime transports and their admitted identities were
            # verified above. Observe each owner once without rediscovering it.
            values = []
            for process_row, admitted, client in (
                    (runtime_b_row, runtime_b, runtime_b_client),
                    (runtime_c_row, runtime_c, runtime_c_client)):
                snapshot = client.request("qa_snapshot")
                assert snapshot["pid"] == process_row["pid"] and snapshot["role"] == "project_host"
                assert snapshot["sessions"] == admitted["sessions"]
                assert snapshot["leases"] == admitted["leases"]
                assert snapshot["film_drafts"] == admitted["film_drafts"]
                assert snapshot["dirty"] == [True]
                values.append((admitted["sessions"][0]["project_id"], snapshot, client))
            return values

        # A global preflight cancellation must leave both owners and drafts intact.
        assistant.request("request_exit_all")
        first_prompt = eventually(
            shutdown_snapshots,
            lambda values: any(_shutdown_review_pending(snapshot) for _project_id, snapshot, _client in values),
            "global exit reaches first dirty project preflight")
        _prompted_id, _prompted_snapshot, prompted_client = next(
            value for value in first_prompt if _shutdown_review_pending(value[1]))
        prompted_client.request("qa_action", action="cancel_shutdown")
        declined = eventually(lambda: assistant.request("exit_status"),
                              lambda row: row["state"] == "declined", "cancelled aggregate exit preflight")
        assert declined["committed_clients"] == []
        assert live(runtime_b_row["pid"]) and live(runtime_c_row["pid"])
        assert live(assistant_process.pid)
        assert runtime_b_client.request("qa_snapshot")["dirty"] == [True]
        assert runtime_c_client.request("qa_snapshot")["dirty"] == [True]
        assistant_ready = assistant_qa.request("qa_snapshot")
        assert not assistant_ready["frozen"] and assistant_ready["chat_enabled"]

        # Confirm each dirty runtime. The first prepared owner waits alive and
        # frozen until the second client has passed its own review.
        assistant.request("request_exit_all")
        snapshots = eventually(
            shutdown_snapshots,
            lambda values: any(_shutdown_review_pending(snapshot) for _project_id, snapshot, _client in values),
            "confirmed aggregate exit reaches first project review")
        first_project_id, _first_snapshot, first_runtime_client = next(
            value for value in snapshots if _shutdown_review_pending(value[1]))
        first_runtime_client.request("qa_action", action="confirm_shutdown")
        waiting_second = eventually(
            shutdown_snapshots,
            lambda values: any(project_id != first_project_id and _shutdown_review_pending(snapshot)
                               for project_id, snapshot, _client in values)
                               and any(project_id == first_project_id and snapshot.get("gate_state") == "ready"
                                       for project_id, snapshot, _client in values),
            "second project review waits after first is prepared")
        prepared_first = next(snapshot for project_id, snapshot, _client in waiting_second
                              if project_id == first_project_id)
        assert prepared_first["frozen"]
        assert live(runtime_b_row["pid"]) and live(runtime_c_row["pid"])
        second_project_id, _second_snapshot, second_runtime_client = next(
            value for value in waiting_second if value[0] != first_project_id and _shutdown_review_pending(value[1]))
        second_runtime_client.request("qa_action", action="confirm_shutdown")
        _assert_clean_child_exit(assistant_process, log_paths[0], timeout=40)
        eventually(lambda: (live(runtime_b_row["pid"]), live(runtime_c_row["pid"])),
                   lambda values: not any(values), "both project runtimes gracefully exit")
        assert not live(assistant_process.pid)
        assert first_project_id != second_project_id
    finally:
        # Only this test's isolated assistant root, project IDs, and exact
        # authenticated test-created GUI processes are eligible for cleanup.
        (state / "allow-registration").touch()
        if assistant_process is not None and assistant_process.poll() is None:
            try:
                assistant.request("request_exit_all")
                answer_global_prompts([project.manifest["id"] for project in projects])
                assistant_process.wait(timeout=35)
            except (UiControlError, subprocess.TimeoutExpired, OSError):
                pass
        for launcher in launchers:
            if launcher.poll() is None:
                try:
                    launcher_client(launcher).request("qa_action", action="shutdown")
                except (UiControlError, OSError):
                    pass
                try:
                    launcher.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    pass
        if assistant_process is not None and assistant_process.poll() is None:
            try:
                assistant.request("request_exit_all")
                answer_global_prompts([project.manifest["id"] for project in projects])
                assistant_process.wait(timeout=35)
            except (UiControlError, subprocess.TimeoutExpired, OSError):
                pass
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        runtime_logs = sorted((assistant_root / "artifacts" / "logs").glob("project-*.log"))
        _preserve_child_logs([*log_paths, *runtime_logs], request.config.getoption("xmlpath", default=None))
