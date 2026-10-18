"""Independent runtime ownership, startup and settle boundaries."""
from __future__ import annotations

import ast
from concurrent.futures import Future
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from coat_sim.core.project_store import ProjectStore
from coat_sim.ui.launcher_projects import ProjectLauncherAdapter
from coat_sim.ui.project_runtime_client import open_project_runtime
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner
from coat_sim.ui.recent_projects import RecentProjects

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def test_smoke_nonzero_exit_keeps_zero_requirement_timeout_and_native_log(tmp_path):
    from tests.test_application_process_smoke import _assert_clean_child_exit
    path = tmp_path / "launcher-2.log"
    path.write_text("Fatal Python error: Segmentation fault\nCurrent thread: native cleanup\n", encoding="utf-8")
    waits = []
    def wait(*, timeout):
        waits.append(timeout)
        return -11
    process: Any = SimpleNamespace(pid=123, wait=wait)
    with pytest.raises(AssertionError) as error:
        _assert_clean_child_exit(process, path, timeout=20)
    assert waits == [20]
    assert "-11; required 0" in str(error.value)
    assert str(path) in str(error.value) and "Current thread: native cleanup" in str(error.value)


def test_smoke_preserves_complete_original_child_logs_beside_ci_junit(tmp_path):
    from tests.test_application_process_smoke import _child_log_tail, _preserve_child_logs
    log = tmp_path / "temporary-child" / "launcher-2.log"
    log.parent.mkdir()
    raw = b"prefix not in tail\n" + b"x" * 70000 + b"\nFatal Python error: Segmentation fault\n"
    log.write_bytes(raw)
    junit = tmp_path / "ci-artifacts" / "qt" / "junit.xml"
    _preserve_child_logs([log], str(junit))
    archived = junit.parent / "application-process-smoke-children" / log.name
    assert archived.read_bytes() == raw
    assert "prefix not in tail" not in _child_log_tail(log)
    assert "Fatal Python error: Segmentation fault" in _child_log_tail(log)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGSEGV diagnostic for the POSIX process smoke")
def test_smoke_real_crashed_child_retains_faulthandler_and_is_never_accepted(tmp_path):
    from tests.test_application_process_smoke import _assert_clean_child_exit
    path = tmp_path / "self-created-crash.log"
    script = ("import os, resource, signal; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
              "os.kill(os.getpid(), signal.SIGSEGV)")
    with path.open("wb") as stream:
        process = subprocess.Popen([sys.executable, "-X", "faulthandler", "-c", script],
                                   stdout=stream, stderr=subprocess.STDOUT)
    with pytest.raises(AssertionError) as error:
        _assert_clean_child_exit(process, path, timeout=20)
    assert process.returncode == -11
    assert "Fatal Python error: Segmentation fault" in str(error.value)
    assert "Current thread" in str(error.value) and str(path) in str(error.value)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def history(tmp_path):
    return RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))


def settle(app, picker):
    deadline = time.monotonic() + 3
    while (picker.projectBusy or picker.recentBusy) and time.monotonic() < deadline:
        app.processEvents()
        picker._complete_pending()
        picker._complete_history()
        time.sleep(0.005)
    assert not picker.projectBusy and not picker.recentBusy


class _BarrierHistory:
    """Small history fake with an explicitly controlled slow refresh."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.entered = Event()
        self.release = Event()
        self.finished = Event()
        self._block_next = False
        self._fail_next = False

    def worker_factory(self):
        return lambda: self

    def block_next_refresh(self, *, fail=False):
        self.entered.clear()
        self.release.clear()
        self.finished.clear()
        self._block_next = True
        self._fail_next = fail

    def refresh(self):
        if not self._block_next:
            return
        self._block_next = False
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("test did not release blocked history refresh")
        try:
            if self._fail_next:
                raise OSError("settings read-only")
        finally:
            self.finished.set()

    def entries(self):
        return [dict(row) for row in self.rows]


def _await_open(picker):
    pending = picker._pending
    assert pending is not None
    try:
        pending.result(timeout=2)
    except Exception:
        # The adapter turns opener exceptions into its user-visible error.
        pass
    picker._complete_pending()
    assert not picker.projectBusy


def _await_history(picker):
    for future in tuple(picker._history_requests.values()):
        try:
            future.result(timeout=2)
        except Exception:
            # History failures are auxiliary and are surfaced in projectError.
            pass
    picker._complete_history()
    assert not picker.recentBusy


def _launcher_snapshot(picker):
    return {
        "busy": picker.projectBusy,
        "selected_file": picker._selected_file,
        "open_error": picker._error,
        "history_busy": picker.recentBusy,
        "recent": picker.projectEntries,
        "error": picker.projectError,
    }


@pytest.fixture
def smoke_qa_dispatch(monkeypatch):
    """Run the actual test-only dispatch without installing sitecustomize."""
    from coat_sim.assistant_process import AssistantProcessClient
    from coat_sim.ui.control import UiControlError
    from tests.test_application_process_smoke import _SUPPORT

    def forbidden_shared_rpc(*_args, **_kwargs):
        pytest.fail("GUI QA dispatch must not synchronously rediscover its own registry")

    monkeypatch.setattr(AssistantProcessClient, "list_clients", forbidden_shared_rpc)
    source = ast.parse(_SUPPORT)
    dispatch = next(node for node in ast.walk(source)
                    if isinstance(node, ast.FunctionDef) and node.name == "dispatch")
    namespace: dict[str, Any] = {
        "control": SimpleNamespace(UiControlError=UiControlError,
                                   UiControlClient=forbidden_shared_rpc),
        "_dispatch": forbidden_shared_rpc,
        "_runtime_dialog": lambda bridge: bridge.dialog,
        "os": os,
    }
    exec(compile(ast.Module(body=[dispatch], type_ignores=[]), "<smoke QA dispatch>", "exec"),
         namespace)
    return namespace["dispatch"]


def test_smoke_launcher_rejects_dirty_without_registry_lookup(smoke_qa_dispatch):
    from coat_sim.ui.control import UiControlError

    launcher = SimpleNamespace(role="launcher", bridge=SimpleNamespace())
    with pytest.raises(UiControlError) as caught:
        smoke_qa_dispatch(launcher, "qa_action", {"action": "dirty", "project_id": "project-a"})
    assert caught.value.code == "invalid_test_action"


@pytest.mark.parametrize("project_id", ["project-a", "project-b", "project-c"])
def test_smoke_dirty_dispatch_edits_only_explicit_runtime_project(
        monkeypatch, smoke_qa_dispatch, project_id):
    from coat_sim.core.project_film_session import ProjectFilmWorkspace

    binding = SimpleNamespace(project_id=project_id)
    host = SimpleNamespace(sessions=lambda: [SimpleNamespace(binding=binding)])
    runtime = SimpleNamespace(role="project_host",
                              bridge=SimpleNamespace(project_runtime=SimpleNamespace(host=host)))
    edits = []
    selected = []

    def workspace_for_project(actual_host, actual_binding):
        selected.append((actual_host, actual_binding))
        def set_design_value(key, value):
            edits.append((key, value))
            return True
        return SimpleNamespace(editor=SimpleNamespace(set_design_value=set_design_value))

    monkeypatch.setattr(ProjectFilmWorkspace, "for_project", workspace_for_project)
    assert smoke_qa_dispatch(runtime, "qa_action", {
        "action": "dirty", "project_id": project_id, "name": "Explicit runtime draft",
    }) == {"accepted": True}
    assert selected == [(host, binding)]
    assert edits == [("name", "Explicit runtime draft")]
    assert smoke_qa_dispatch(runtime, "qa_action", {
        "action": "dirty", "project_id": "unrelated-project",
    }) == {"accepted": False}
    assert selected == [(host, binding)] and edits == [("name", "Explicit runtime draft")]


def test_smoke_dirty_requests_use_each_discovered_runtime_client():
    from tests import test_application_process_smoke as smoke

    source = ast.parse(Path(smoke.__file__).read_text(encoding="utf-8"))
    test = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                and node.name == "test_detached_project_runtime_ownership_across_launcher_lifetimes")
    requests = []
    for node in ast.walk(test):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "request" and any(
                    item.arg == "action" and isinstance(item.value, ast.Constant)
                    and item.value.value == "dirty" for item in node.keywords)):
            project = next(item.value for item in node.keywords if item.arg == "project_id")
            requests.append((ast.unparse(node.func.value), ast.unparse(project)))
    assert requests == [(f"runtime_{letter}_client", f"projects[{index}].manifest['id']")
                        for index, letter in enumerate("abc")]


@pytest.mark.parametrize("action, decision", [
    ("confirm_shutdown", "confirm"), ("cancel_shutdown", "cancel"),
])
def test_smoke_global_review_answers_are_local_without_registry_lookup(
        smoke_qa_dispatch, action, decision):
    calls = []
    bridge = SimpleNamespace(
        project_runtime=SimpleNamespace(host=SimpleNamespace(sessions=lambda: [])),
        dialog=SimpleNamespace(hide=lambda: calls.append("hide")),
        confirmShutdown=lambda: calls.append("confirm"),
        cancelShutdown=lambda: calls.append("cancel"),
    )
    runtime = SimpleNamespace(role="project_host", bridge=bridge)
    assert smoke_qa_dispatch(runtime, "qa_action", {"action": action}) == {"accepted": True}
    assert calls == ["hide", decision]


def test_smoke_snapshot_observes_actual_prepared_freeze_without_admitting_operations(
        app, tmp_path, monkeypatch, smoke_qa_dispatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtQuick import QQuickItem
    from shiboken6 import isValid
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.project_session import ProjectSessionError, ProjectSessionHost
    from coat_sim.ui.project_application_client import ProjectApplicationClient
    from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle
    from tests.test_project_application_lifecycle import Bridge

    store = ProjectStore.create(tmp_path / "frozen-project", "Frozen project")
    host = ProjectSessionHost(lambda _root: store)
    session = host.open_project(store.root)
    route = host.reserve_module(session.binding.project_id, "film_design").route
    host.attach_window(route, SimpleNamespace(isVisible=lambda: True, title=lambda: "Existing film designer"))
    workspace = ProjectFilmWorkspace.for_project(host, session.binding)
    editor = workspace.editor
    assert editor.set_design_value("name", "Dirty draft survives observation")
    before = editor.snapshot()
    bridge: Any = Bridge()
    content = QQuickItem()
    content.setParent(bridge)
    owner = SimpleNamespace(
        host=host, _busy=False, projectBusy=False, projectError="",
        desktop=SimpleNamespace(_views={route: (content,)}),
        project_closer=SimpleNamespace(_reviews={}),
        _leases={session.binding.project_id: SimpleNamespace(generation="test-lease", _closed=False)},
        settled_for_exit=lambda: True,
    )
    bridge.project_runtime = owner
    activated: list[QQuickItem] = []
    bridge._activate_window = activated.append
    bridge.dialog = SimpleNamespace(property=lambda key: "shutdown" if key == "reviewKind" else "")
    lifecycle = ProjectApplicationLifecycle(bridge, "test-client-generation")
    bridge.project_lifecycle = lifecycle
    runtime: Any = SimpleNamespace(role="project_host", bridge=bridge, _sessions=host.sessions)
    runtime._project_rows = lambda: ProjectApplicationClient._project_rows(runtime)
    runtime._project_capabilities = ProjectApplicationClient._project_capabilities
    arguments = {"client_generation": "test-client-generation", "exit_id": "test-aggregate-exit"}
    try:
        assert lifecycle.dispatch("lifecycle_prepare", arguments)["state"] == "pending"
        assert activated == [content]
        pending = smoke_qa_dispatch(runtime, "qa_snapshot", {})
        assert pending["pending_shutdown"] and pending["gate_state"] == "pending"
        lifecycle.decide(True)
        ready = lifecycle.dispatch("lifecycle_prepare", arguments)
        assert ready["state"] == "ready" and host.admission_frozen and not content.isEnabled()
        for admission in (lambda: ProjectFilmWorkspace.for_project(host, session.binding), lambda: workspace.editor):
            with pytest.raises(ProjectSessionError) as caught:
                admission()
            assert caught.value.code == "application_exiting"

        def forbidden_lazy_access(*_args):
            pytest.fail("QA observation must not create or admit a workspace/editor")

        monkeypatch.setattr(ProjectFilmWorkspace, "for_project", forbidden_lazy_access)
        monkeypatch.setattr(ProjectFilmWorkspace, "editor", property(forbidden_lazy_access))
        prepared = smoke_qa_dispatch(runtime, "qa_snapshot", {})
        assert prepared["gate_state"] == "ready" and prepared["gate_active"] and prepared["frozen"]
        assert not prepared["pending_shutdown"] and prepared["dirty"] == [True]
        assert prepared["film_drafts"] == pending["film_drafts"]
        assert prepared["sessions"] == pending["sessions"] and prepared["leases"] == pending["leases"]
        assert prepared["rows"] == pending["rows"] and prepared["rows"][0]["instance_id"] == route.route_id
        assert editor.snapshot() == before and host.admission_frozen
        assert host.resource_for(session.binding, "film_workspace") is workspace and workspace._editor is editor
        lifecycle.dispatch("lifecycle_cancel", arguments | {"permit_id": ready["permit_id"]})
        cancelled = smoke_qa_dispatch(runtime, "qa_snapshot", {})
        assert not cancelled["gate_active"] and not cancelled["frozen"]
        assert cancelled["film_drafts"] == pending["film_drafts"]
        assert not host.admission_frozen and content.isEnabled() and not bridge._closing
    finally:
        try:
            if lifecycle.active:
                lifecycle.dispatch("lifecycle_cancel", arguments | {"permit_id": lifecycle.gate._permit})
        finally:
            lifecycle._timer.stop()
            bridge.deleteLater()
            QCoreApplication.sendPostedEvents(bridge, QEvent.Type.DeferredDelete)
            assert all(not isValid(value) for value in (bridge, content, lifecycle))


@pytest.mark.parametrize("has_workspace", [False, True])
def test_smoke_snapshot_never_creates_missing_film_resource_or_editor(
        tmp_path, smoke_qa_dispatch, has_workspace):
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.project_session import ProjectSessionHost
    from coat_sim.ui.control import UiControlError

    store = ProjectStore.create(tmp_path / "project", "Uninitialized draft")
    host = ProjectSessionHost(lambda _root: store)
    session = host.open_project(store.root)
    workspace = ProjectFilmWorkspace.for_project(host, session.binding) if has_workspace else None
    runtime = SimpleNamespace(role="project_host", bridge=SimpleNamespace(
        project_runtime=SimpleNamespace(host=host)))
    with host.exit_admission_freeze():
        with pytest.raises(UiControlError) as caught:
            smoke_qa_dispatch(runtime, "qa_snapshot", {})
        assert caught.value.code == "test_snapshot_unavailable"
        assert host.resource_for(session.binding, "film_workspace") is workspace
        assert workspace is None or workspace._editor is None
        assert host.admission_frozen


@pytest.mark.parametrize("snapshots", [
    [{"review_kind": "shutdown", "pending_shutdown": False},
     {"review_kind": "shutdown", "pending_shutdown": True}],
    [{"review_kind": "error", "pending_shutdown": True},
     {"review_kind": "shutdown", "pending_shutdown": True}],
    [{"review_kind": "shutdown", "pending_shutdown": True},
     {"review_kind": "shutdown", "pending_shutdown": False}],
])
def test_smoke_review_selection_ignores_retained_or_nonshutdown_labels(snapshots):
    from tests.test_application_process_smoke import _shutdown_review_pending

    selected = [row for row in snapshots if _shutdown_review_pending(row)]
    assert selected == [{"review_kind": "shutdown", "pending_shutdown": True}]


def test_picker_has_no_owner_and_records_only_confirmed_open(app, tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    recent = history(tmp_path)
    calls = []
    recent_factory = recent.worker_factory()
    def opened(path):
        calls.append(path)
        recent_factory().record(store.project_file, store.manifest["name"])
        return store
    picker: Any = ProjectLauncherAdapter(recent_projects=recent, runtime_opener=opened)
    assert not any(hasattr(picker, key) for key in ("host", "desktop", "_leases", "project_closer"))
    assert picker.openProject(str(store.project_file))
    settle(app, picker)
    assert calls == [store.project_file]
    assert [row["descriptorFile"] for row in picker.projectEntries] == [str(store.project_file)]
    picker.removeRecentProject(str(store.project_file))
    settle(app, picker)
    assert picker.projectEntries == [] and store.project_file.is_file()
    picker.stop()


def test_failed_open_does_not_promote_history(app, tmp_path):
    recent = history(tmp_path)
    def failed(_path):
        raise ValueError("actual startup failure")
    picker: Any = ProjectLauncherAdapter(recent_projects=recent, runtime_opener=failed)
    assert picker.openProject(str(tmp_path / "missing.coatproj"))
    settle(app, picker)
    assert picker.projectEntries == [] and picker.projectError == "actual startup failure"
    picker.stop()


def test_open_is_confirmed_before_blocked_history_and_history_warning_is_auxiliary(app, tmp_path):
    from tests.test_application_process_smoke import _launcher_open_outcome

    store = ProjectStore.create(tmp_path / "project", "Project")
    history = _BarrierHistory()
    picker: Any = ProjectLauncherAdapter(recent_projects=history, runtime_opener=lambda _path: store)
    worker = picker._history_worker._thread
    try:
        _await_history(picker)
        history.block_next_refresh(fail=True)
        descriptor = str(store.project_file)
        assert picker.openProject(descriptor)
        _await_open(picker)
        assert history.entered.wait(1), "the post-open history refresh did not start"

        snapshot = _launcher_snapshot(picker)
        assert snapshot["busy"] is False
        assert snapshot["history_busy"] is True
        assert snapshot["recent"] == []
        assert snapshot["selected_file"] == descriptor
        assert snapshot["open_error"] == ""
        assert _launcher_open_outcome(snapshot, descriptor) == "opened"

        history.release.set()
        assert history.finished.wait(1), "the blocked history refresh did not finish"
        _await_history(picker)
        snapshot = _launcher_snapshot(picker)
        assert "settings read-only" in snapshot["error"]
        assert snapshot["open_error"] == ""
        assert _launcher_open_outcome(snapshot, descriptor) == "opened"
    finally:
        history.release.set()
        picker.stop()
        worker.join(timeout=1)


@pytest.mark.parametrize(("open_error", "expected"), [
    ("启动失败，请刷新后重试", "retryable_error"),
    ("descriptor is invalid", "failed"),
])
def test_existing_recent_and_previous_selection_do_not_mask_new_open_failure(
        app, tmp_path, open_error, expected):
    from tests.test_application_process_smoke import _launcher_open_outcome

    store = ProjectStore.create(tmp_path / "project", "Project")
    descriptor = str(store.project_file)
    recent = _BarrierHistory([{
        "descriptorFile": descriptor,
        "name": "Project",
        "lastOpened": "2026-10-15T00:00:00+00:00",
        "available": True,
    }])
    picker: Any = ProjectLauncherAdapter(recent_projects=recent, runtime_opener=lambda _path: store)
    worker = picker._history_worker._thread
    try:
        _await_history(picker)
        assert picker.openProject(descriptor)
        _await_open(picker)
        _await_history(picker)
        assert picker._selected_file == descriptor
        assert [row["descriptorFile"] for row in picker.projectEntries] == [descriptor]

        def fail(_path):
            raise ValueError(open_error)

        picker._runtime_opener = fail
        assert picker.openProject(descriptor)
        _await_open(picker)
        snapshot = _launcher_snapshot(picker)
        assert snapshot["recent"][0]["descriptorFile"] == descriptor
        assert snapshot["selected_file"] == descriptor
        assert snapshot["open_error"] == open_error
        assert _launcher_open_outcome(snapshot, descriptor) == expected
    finally:
        recent.release.set()
        picker.stop()
        worker.join(timeout=1)


@pytest.mark.parametrize(("snapshot", "expected"), [
    ({"busy": True, "selected_file": "same.coatproj", "open_error": "failure"}, "pending"),
    ({"busy": False, "selected_file": "", "open_error": ""}, "unconfirmed"),
    ({"busy": False, "selected_file": "other.coatproj", "open_error": "刷新后重试"},
     "retryable_error"),
    ({"busy": False, "selected_file": "same.coatproj", "open_error": "fatal"}, "failed"),
])
def test_launcher_open_outcome_classifies_only_the_request(snapshot, expected):
    from tests.test_application_process_smoke import _launcher_open_outcome

    assert _launcher_open_outcome(snapshot, "same.coatproj") == expected


def _journal_fixture():
    return {
        "updated_at": "2026-10-15T10:20:30+00:00",
        "project_id": "project-1",
        "generation": "generation-1",
        "clean": False,
        "revision": 7,
        "checkpoint": {"draft": {"name": "Project", "layers": ["a", "b"]}},
    }


def test_journal_state_comparison_ignores_only_valid_updated_at():
    from tests.test_application_process_smoke import _journal_recovery_state

    before = _journal_fixture()
    after = _journal_fixture() | {"updated_at": "2026-10-15T11:20:30+00:00"}
    assert _journal_recovery_state(after) == _journal_recovery_state(before)


@pytest.mark.parametrize("change", [
    lambda journal: journal["checkpoint"]["draft"].clear(),
    lambda journal: journal.update(clean=True),
    lambda journal: journal.update(generation="generation-2"),
    lambda journal: journal.update(project_id="project-2"),
    lambda journal: journal.update(extra="must be compared"),
])
def test_journal_state_comparison_preserves_recovery_semantics(change):
    from tests.test_application_process_smoke import _journal_recovery_state

    before = _journal_fixture()
    after = _journal_fixture()
    change(after)
    assert _journal_recovery_state(after) != _journal_recovery_state(before)


@pytest.mark.parametrize("updated_at", [None, "not-a-timestamp"])
def test_journal_state_comparison_rejects_missing_or_invalid_timestamp(updated_at):
    from tests.test_application_process_smoke import _journal_recovery_state

    journal = _journal_fixture()
    if updated_at is None:
        journal.pop("updated_at")
    else:
        journal["updated_at"] = updated_at
    with pytest.raises((KeyError, ValueError)):
        _journal_recovery_state(journal)


def test_create_submits_spec_to_runtime_without_creating_in_launcher(app, tmp_path):
    recent = history(tmp_path)
    recent_factory = recent.worker_factory()
    calls = []
    def created(name, path):
        calls.append((name, path, path.exists()))
        store = ProjectStore.create(path, name)  # Stand-in for the detached owner.
        recent_factory().record(store.project_file, name)
        return store
    picker: Any = ProjectLauncherAdapter(recent_projects=recent, runtime_creator=created)
    assert picker.createProject("New", str(tmp_path / "new"))
    settle(app, picker)
    assert calls == [("New", tmp_path / "new", False)]
    assert len(picker.projectEntries) == 1
    picker.stop()


def test_detached_open_requires_ready_then_exact_owner_activation(monkeypatch, tmp_path):
    import coat_sim.ui.project_runtime_client as module
    store = ProjectStore.create(tmp_path / "project", "P")
    shared = SimpleNamespace(root=tmp_path / "assistant", ensure_running=lambda: None)
    target = {"client_id": "c", "client_generation": "cg", "project_id": store.manifest["id"],
              "session_generation": "sg", "root": str(store.root), "project_file": str(store.project_file)}
    found = iter((None, target))
    monkeypatch.setattr(module, "find_shared_project_owner", lambda *_args: next(found))
    activations = []
    def activated(_client, value):
        activations.append(value)
        return {"history_managed": True}
    monkeypatch.setattr(module, "activate_shared_project", activated)
    spawned: list[tuple[list[str], dict[str, Any], Any]] = []
    class Channel:
        def child_environment(self):
            return {"COAT_UI_READY_KEY": "new-key"}
        def wait(self, process, timeout):
            assert process is spawned[0][2] and timeout == 50
            assert activations == []
            return {"status": "ready", "endpoint": {"pid": 111}}
        def close(self):
            pass
    monkeypatch.setattr(module, "UiReadyChannel", Channel)
    def popen(command, **kwargs):
        process = SimpleNamespace(pid=111)
        spawned.append((command, kwargs, process))
        return process
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setenv("COAT_UI_PID", "999")
    monkeypatch.setenv("COAT_UI_PARENT_PID", "999")
    result = open_project_runtime(store.project_file, client=shared)
    assert result.project_file == store.project_file and activations == [target]
    command, kwargs, _process = spawned[0]
    assert "coat_sim.ui.project_runtime" in command
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdout"] != subprocess.PIPE
    assert "COAT_UI_PID" not in kwargs["env"] and "COAT_UI_PARENT_PID" not in kwargs["env"]
    if module.os.name != "nt":
        assert kwargs["start_new_session"] is True


def test_existing_owner_open_does_not_spawn(monkeypatch, tmp_path):
    import coat_sim.ui.project_runtime_client as module
    store = ProjectStore.create(tmp_path / "project", "P")
    target = {"client_id": "already-owned"}
    shared = SimpleNamespace(ensure_running=lambda: None)
    monkeypatch.setattr(module, "find_shared_project_owner", lambda *_args: target)
    called = []
    def activated(*args):
        called.append(args)
        return {"history_managed": True}
    monkeypatch.setattr(module, "activate_shared_project", activated)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_k: pytest.fail("existing owner must not spawn"))
    assert open_project_runtime(store.project_file, client=shared).project_file == store.project_file
    assert called == [(shared, target)]


def test_release_settles_after_checkpoint_and_surfaces_release_error(app, tmp_path):
    owner: Any = ProjectRuntimeOwner(geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    checkpoint: Future[Any] = Future()
    calls = []
    owner.release_lease(SimpleNamespace(close=lambda **kw: calls.append(kw)), checkpoint)
    assert not owner.settled_for_exit() and calls == []
    checkpoint.set_result(None)
    assert owner.settled_for_exit() and calls == [{"clean": True}]
    def fail(**_kw):
        raise OSError("release failure")
    owner.release_lease(SimpleNamespace(close=fail))
    with pytest.raises(OSError, match="release failure"):
        owner.settled_for_exit()
    owner.stop()


def test_stopped_pending_admission_remains_tracked_until_unclean_release(app, tmp_path):
    owner: Any = ProjectRuntimeOwner(geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    pending: Future[Any] = Future()
    pending.set_running_or_notify_cancel()
    owner._pending = pending
    owner._busy = True
    calls = []
    owner.stop()
    assert not owner.settled_for_exit()
    pending.set_result((None, None, SimpleNamespace(close=lambda **kw: calls.append(kw))))
    assert owner.settled_for_exit() and calls == [{"clean": False}]


def test_stopped_pending_package_remains_tracked_until_io_settles(app, tmp_path):
    owner: Any = ProjectRuntimeOwner(geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    pending: Future[Any] = Future()
    pending.set_running_or_notify_cancel()
    owner.packages.pending = pending
    owner.stop()
    assert not owner.settled_for_exit()
    pending.set_result({"destination": "done"})
    assert owner.settled_for_exit()


def test_startup_window_is_noninteractive_until_latest_registration(monkeypatch):
    import coat_sim.ui.project_runtime as module
    class Item:
        enabled = True
        def isEnabled(self):
            return self.enabled
        def setEnabled(self, value):
            self.enabled = value
    class Window:
        visible = True
        item = Item()
        def contentItem(self):
            return self.item
        def isVisible(self):
            return self.visible
        def hide(self):
            self.visible = False
        def show(self):
            self.visible = True
        def raise_(self):
            pass
        def requestActivate(self):
            pass
    window = Window()
    registrations = []
    client = SimpleNamespace(_pending=object(), admission=SimpleNamespace(ready=False),
                             _register=lambda: registrations.append(True))
    runtime: Any = SimpleNamespace(_completion=None, _startup_windows=[], application=client,
        owner=SimpleNamespace(_busy=False, desktop=SimpleNamespace(_views={"film": (window,)}),
                              host=SimpleNamespace(sessions=lambda: ["live-session"])),
        bridge=SimpleNamespace(_project_starting=True), _finished=False, _failed=False,
        _requested=True, _started_at=time.monotonic(), _endpoint={"pid": 1},
        _poll_history=lambda: True, _record_history=lambda: None)
    module.ProjectRuntime._open_finished(runtime, True)
    assert registrations == [True] and not window.visible and not window.item.enabled
    reports = []
    monkeypatch.setattr(module, "report_startup_event", lambda status, **kw: reports.append((status, kw)))
    module.ProjectRuntime._tick(runtime)
    assert not reports and not runtime._finished
    client._pending = None
    client.admission.ready = True
    module.ProjectRuntime._tick(runtime)
    assert runtime._finished and not runtime.bridge._project_starting
    assert window.visible and window.item.enabled
    assert reports == [("ready", {"endpoint": {"pid": 1}})]


def test_startup_timeout_aborts_only_before_editing_and_always_stops(monkeypatch):
    import coat_sim.ui.project_runtime as module
    calls = []
    def failed_abort():
        calls.append("abort")
        raise OSError("abort close failed")
    owner = SimpleNamespace(abort_startup=failed_abort, stop=lambda: calls.append("stop"),
                            _set_error=lambda message: calls.append(message))
    runtime: Any = SimpleNamespace(_failed=False, owner=owner)
    monkeypatch.setattr(module, "report_startup_event", lambda status, **kw: calls.append(status))
    module.ProjectRuntime._fail(runtime, "registration timeout")
    assert runtime._failed and calls[0:2] == ["error", "abort"] and calls[-1] == "stop"
    assert "abort close failed" in calls[2]


def test_async_error_cannot_replace_pending_close_review():
    from coat_sim.ui.project_runtime import ProjectRuntime
    changes = []
    dialog = SimpleNamespace(isVisible=lambda: True, property=lambda _key: "project",
                             setProperty=lambda *args: changes.append(args),
                             show=lambda: changes.append("show"))
    runtime: Any = SimpleNamespace(_finished=True, _failed=False,
                              owner=SimpleNamespace(projectError="checkpoint failed"), dialog_window=dialog)
    ProjectRuntime._owner_changed(runtime)
    assert changes == []  # Original review, Cancel and Return to save remain usable.
    dialog.isVisible = lambda: False
    ProjectRuntime._owner_changed(runtime)
    assert changes == [("reviewKind", "error"), "show"]


def test_runtime_quit_uses_exact_close_coordinator_and_waits_for_actual_close():
    from coat_sim.ui.project_runtime import ProjectRuntime
    session = SimpleNamespace(binding=SimpleNamespace(project_id="p"))
    sessions = [session]
    calls = []
    bridge = SimpleNamespace(_closing=False, _runtime_shutdown_requested=False,
                             _project_shutdown_approved=False)
    def refused(identity):
        calls.append(identity)
        return False
    owner = SimpleNamespace(_busy=False, host=SimpleNamespace(sessions=lambda: tuple(sessions)),
        closeProject=refused,
        _set_error=lambda message: pytest.fail(message))
    runtime: Any = SimpleNamespace(bridge=bridge, owner=owner)
    ProjectRuntime.request_shutdown(runtime)
    assert calls == ["p"] and not bridge._runtime_shutdown_requested
    def confirmed(identity):
        calls.append(identity)
        sessions.clear()
        return True
    owner.closeProject = confirmed
    ProjectRuntime.request_shutdown(runtime)
    assert calls == ["p", "p"] and bridge._runtime_shutdown_requested


@pytest.mark.parametrize("event, owner_present, activation_fails, code", [
    ({"status": "timeout"}, False, False, "project_start_failed"),
    ({"status": "error", "error": "actual loader error"}, False, False, "project_start_failed"),
    ({"status": "process_exit", "returncode": 2}, False, False, "project_start_failed"),
    ({"status": "channel_error", "error": "identity mismatch"}, False, False, "project_start_failed"),
    ({"status": "ready"}, False, False, "project_owner_unconfirmed"),
    ({"status": "ready"}, True, True, "activation_unconfirmed"),
])
def test_startup_failure_matrix_never_signals_process_or_records_history(
        monkeypatch, tmp_path, event, owner_present, activation_fails, code):
    import coat_sim.ui.project_runtime_client as module
    from coat_sim.ui.control import UiControlError
    store = ProjectStore.create(tmp_path / "project", "P")
    shared = SimpleNamespace(root=tmp_path / "assistant", ensure_running=lambda: None)
    found = iter((None, {"owner": "exact"} if owner_present else None))
    monkeypatch.setattr(module, "find_shared_project_owner", lambda *_args: next(found))
    class Channel:
        def child_environment(self):
            return {}
        def wait(self, _process, timeout):
            return event
        def close(self):
            pass
    monkeypatch.setattr(module, "UiReadyChannel", Channel)
    def forbidden_signal():
        pytest.fail("startup failure must never terminate an owner or contender")
    process = SimpleNamespace(pid=123, terminate=forbidden_signal, kill=forbidden_signal)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(module, "_record_recent_success", lambda _store: pytest.fail("failed open promoted history"))
    def activate(*_args):
        if activation_fails:
            raise UiControlError("activation_unconfirmed", "not confirmed")
        pytest.fail("unconfirmed owner cannot activate")
    monkeypatch.setattr(module, "activate_shared_project", activate)
    with pytest.raises(UiControlError) as caught:
        open_project_runtime(store.project_file, client=shared)
    assert caught.value.code == code


def test_history_failure_and_broken_stderr_do_not_change_confirmed_open(monkeypatch, tmp_path):
    import coat_sim.ui.project_runtime_client as module
    store = ProjectStore.create(tmp_path / "project", "P")
    monkeypatch.setattr(module, "find_shared_project_owner", lambda *_args: {"legacy": "owner"})
    monkeypatch.setattr(module, "activate_shared_project", lambda *_args: {"activated": True})
    def failed_history(*_args):
        raise OSError("settings read-only")
    monkeypatch.setattr(RecentProjects, "record", failed_history)
    class BrokenStream:
        def write(self, _text):
            raise BrokenPipeError("launcher stream gone")
        def flush(self):
            raise BrokenPipeError("launcher stream gone")
    monkeypatch.setattr(module.sys, "stderr", BrokenStream())
    shared = SimpleNamespace(ensure_running=lambda: None)
    assert open_project_runtime(store.project_file, client=shared).project_file == store.project_file


def test_history_io_cannot_block_getter_gui_ticks_or_launcher_stop(app):
    from threading import Event
    from PySide6.QtCore import QTimer
    entered, release = Event(), Event()
    class SlowHistory:
        def refresh(self):
            entered.set()
            release.wait(3)
        def entries(self):
            return []
    picker: Any = ProjectLauncherAdapter(recent_projects=SlowHistory())
    assert entered.wait(1)
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    started = time.monotonic()
    for _ in range(100):
        assert picker.projectEntries == []
        picker.refreshProjects()  # Repeated refresh is coalesced while I/O waits.
        app.processEvents()
    assert time.monotonic() - started < 0.25
    assert len(picker._history_requests) == 1
    deadline = time.monotonic() + 0.05
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    assert len(ticks) >= 3
    started = time.monotonic()
    picker.stop()
    assert time.monotonic() - started < 0.1
    release.set()
    timer.stop()


def test_stale_history_result_cannot_replace_newer_cached_snapshot(app, tmp_path):
    picker: Any = ProjectLauncherAdapter(recent_projects=history(tmp_path))
    settle(app, picker)
    older: Future[Any] = Future()
    latest: Future[Any] = Future()
    picker._history_requests = {100: older, 101: latest}
    picker._history_version = 101
    current = {"descriptorFile": "new.coatproj", "name": "new", "lastOpened": "new", "available": True}
    latest.set_result([current])
    picker._complete_history()
    older.set_result([current | {"descriptorFile": "old.coatproj"}])
    picker._complete_history()
    assert [row["descriptorFile"] for row in picker.projectEntries] == ["new.coatproj"]
    picker.stop()


def test_runtime_history_is_independent_coalesced_and_auxiliary(tmp_path):
    from coat_sim.ui.project_runtime import ProjectRuntime
    store = ProjectStore.create(tmp_path / "project", "P")
    session = SimpleNamespace(binding="binding", name="P")
    submitted = []
    pending: Future[Any] = Future()
    def submit(*args):
        submitted.append(args)
        return pending
    errors: list[str] = []
    runtime: Any = SimpleNamespace(_history_pending={}, _history_last={},
        _history_worker=SimpleNamespace(submit=submit),
        owner=SimpleNamespace(host=SimpleNamespace(sessions=lambda: [session], store_for=lambda _binding: store),
                              _set_error=errors.append))
    ProjectRuntime._record_history(runtime)
    ProjectRuntime._record_history(runtime)
    assert submitted == [("record_only", store.project_file, "P")]
    assert not ProjectRuntime._poll_history(runtime)
    pending.set_exception(OSError("settings unavailable"))
    assert ProjectRuntime._poll_history(runtime)
    assert "settings unavailable" in errors[0] and runtime.owner.host.sessions() == [session]


def test_create_spec_is_passed_as_arguments_and_never_overwrites(monkeypatch, tmp_path):
    import coat_sim.ui.project_runtime_client as module
    from coat_sim.ui.control import UiControlError
    root = tmp_path / "new"
    name = "A; literal argument"
    shared = SimpleNamespace(ensure_running=lambda: None)
    calls = []
    def start(arguments, _shared, _application_root, _timeout):
        calls.append(arguments)
        assert not root.exists()
        ProjectStore.create(root, name)  # Represents the independently owned transaction.
    monkeypatch.setattr(module, "_start_runtime", start)
    monkeypatch.setattr(module, "_confirm_owner", lambda _client, store: store)
    assert module.create_project_runtime(name, root, client=shared).manifest["name"] == name
    assert calls == [["--create-root", str(root), "--name", name]]
    marker = root / "untouched.txt"
    marker.write_text("keep")
    with pytest.raises(UiControlError, match="不会覆盖"):
        module.create_project_runtime(name, root, client=shared)
    assert marker.read_text() == "keep" and len(calls) == 1


@pytest.mark.parametrize("arguments", [
    [], ["--create-root", "new"], ["--project", "a.coatproj", "--name", "wrong"],
    ["--project", "a.coatproj", "--create-root", "new", "--name", "wrong"],
])
def test_runtime_create_argument_pairs_are_validated_before_qt(arguments):
    from coat_sim.ui.project_runtime import main
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == 2


def test_full_history_queue_stop_cancels_unstarted_tasks_and_worker_exits():
    from threading import Event
    from coat_sim.ui.recent_project_tasks import RecentProjectTasks
    entered, release = Event(), Event()
    class BlockedHistory:
        def refresh(self):
            entered.set()
            release.wait(2)
        def entries(self):
            return []
    worker = RecentProjectTasks(BlockedHistory)
    current = worker.submit("refresh")
    assert entered.wait(1)
    queued = [worker.submit("refresh") for _ in range(16)]
    overflow = worker.submit("refresh")
    assert overflow.done() and isinstance(overflow.exception(), RuntimeError)
    worker.stop()
    assert all(value.cancelled() for value in queued)
    release.set()
    worker._thread.join(timeout=1)
    assert not worker._thread.is_alive() and current.done()


def test_frozen_runtime_uses_registered_child_role(monkeypatch):
    import coat_sim.process_entry as entry
    monkeypatch.setattr(entry.sys, "frozen", True, raising=False)
    command = entry.module_command("coat_sim.ui.project_runtime", "--project", "named.coatproj")
    assert command == [entry.sys.executable, "--coat-worker", "project-runtime", "--project", "named.coatproj"]


def test_pending_project_transaction_blocks_close_before_lease_release():
    from coat_sim.ui.project_close_coordinator import ProjectCloseCoordinator
    from coat_sim.core.project_dependencies import ProjectError
    closer = ProjectCloseCoordinator(SimpleNamespace(_busy=True))
    with pytest.raises(ProjectError) as caught:
        closer._preflight(object())
    assert caught.value.code == "project_busy"


def test_journal_close_failure_acknowledges_without_touching_new_owner(app, tmp_path, monkeypatch):
    from coat_sim.project_recovery import ProjectOwnerLease
    owner = ProjectRuntimeOwner(geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    store = ProjectStore.create(tmp_path / "synthetic", "synthetic")
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"],
                                      owner_runtime_root=tmp_path / "owners")
    lease.checkpoint({"synthetic_draft": "keep for recovery"})
    original_write = lease._write
    attempts = []
    def fail_once(**kwargs):
        attempts.append(kwargs)
        raise OSError("synthetic transient journal failure")
    monkeypatch.setattr(lease, "_write", fail_once)
    owner.release_lease(lease)
    monkeypatch.setattr(lease, "_write", original_write)
    notifications = []
    owner.projectsChanged.connect(lambda: notifications.append(owner.projectError))
    try:
        assert lease._closed
        assert not owner.settled_for_exit()
        assert owner.releaseWarningPending
        assert "所有权已释放" in str(owner.projectError)
        assert "未能确认" in str(owner.projectError)
        assert not owner._pending_lease_releases
        for _ in range(3):
            assert not owner.settled_for_exit()
        assert len(notifications) == 1
        second = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"],
                                           owner_runtime_root=tmp_path / "owners")
        try:
            assert second.recovery_candidate()["checkpoint"] == {"synthetic_draft": "keep for recovery"}
            second.checkpoint({"new_owner": "must remain untouched"})
            journal = second._journal.read_bytes()
            assert owner.acknowledgeReleaseWarning()
            assert not owner.acknowledgeReleaseWarning()
            assert owner.settled_for_exit()
            from coat_sim.ui.project_runtime import ProjectRuntime
            shutdown = []
            runtime = SimpleNamespace(_finished=True, _failed=False, owner=owner,
                application=SimpleNamespace(), _poll_history=lambda: True,
                _timer=SimpleNamespace(stop=lambda: shutdown.append("timer")),
                _history_worker=SimpleNamespace(stop=lambda: shutdown.append("history")),
                bridge=SimpleNamespace(shutdown=lambda: shutdown.append("shutdown")))
            ProjectRuntime._tick(cast(ProjectRuntime, runtime))
            assert shutdown == ["timer", "history", "shutdown"]
            assert runtime.bridge._runtime_shutdown_authorized
            assert second._journal.read_bytes() == journal
            assert not second._closed
            assert len(attempts) == 1  # No retry after release, even after I/O repair.
            assert not owner.releaseWarningPending
        finally:
            second.close(clean=False)
    finally:
        owner.stop()


def test_unknown_release_failure_cannot_be_acknowledged(app, tmp_path):
    owner = ProjectRuntimeOwner(geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    def fail(**_kwargs):
        raise OSError("unknown lock release status")
    from coat_sim.project_recovery import ProjectOwnerLease
    owner.release_lease(cast(ProjectOwnerLease, SimpleNamespace(close=fail)))
    try:
        with pytest.raises(OSError, match="unknown lock"):
            owner.settled_for_exit()
        assert not owner.releaseWarningPending
        assert not owner.acknowledgeReleaseWarning()
        with pytest.raises(OSError, match="unknown lock"):
            owner.settled_for_exit()
    finally:
        owner.stop()


def test_journal_warning_has_explicit_runtime_dialog_route():
    from coat_sim.ui.project_runtime import ProjectRuntime
    changes = []
    dialog = SimpleNamespace(isVisible=lambda: False,
                             setProperty=lambda *args: changes.append(args),
                             show=lambda: changes.append("show"))
    runtime = SimpleNamespace(_finished=True, _failed=False,
        owner=SimpleNamespace(projectError="uncertain journal", releaseWarningPending=True),
        dialog_window=dialog)
    ProjectRuntime._owner_changed(cast(ProjectRuntime, runtime))
    assert changes == [("reviewKind", "releaseWarning"), "show"]
    qml = (Path(__file__).parents[1] / "coat_sim/ui/qml/ProjectRuntimeDialogs.qml").read_text()
    assert "projectBridge.acknowledgeReleaseWarning()" in qml
    assert '"确认并退出"' in qml
