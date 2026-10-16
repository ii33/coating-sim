"""Headless contracts for presentation close and coordinated local shutdown."""

from pathlib import Path
from threading import Event, Thread

import pytest

from coat_sim.project_lifecycle import (
    LifecycleError, PreparedClose, ProjectLifecycleCoordinator,
)
from coat_sim.project_session import ProjectSessionError, ProjectSessionHost


pytestmark = pytest.mark.contract


class FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.manifest = {"id": self.root.name, "name": self.root.name}


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.exit_code = None

    def poll(self):
        return self.exit_code


def setup(tmp_path, count=2):
    host = ProjectSessionHost(FakeStore)
    sessions = [host.open_project(tmp_path / str(index)) for index in range(count)]
    return host, ProjectLifecycleCoordinator(host), sessions


def record(calls: list[str], event: str) -> bool:
    calls.append(event)
    return True


def prepare_logged(calls: list[str], event: str, commit_event: str) -> PreparedClose:
    calls.append(event)
    return PreparedClose(lambda: record(calls, commit_event))


def test_launcher_close_only_dismisses_presentation(tmp_path):
    host, coordinator, sessions = setup(tmp_path)
    assistant = FakeProcess(12345)
    called: list[str] = []
    coordinator.register_owned_process("assistant", "global", assistant,
                                       lambda: record(called, "stop assistant"))
    assert coordinator.close_launcher(lambda: called.append("hide launcher"))
    assert called == ["hide launcher"]
    assert host.sessions() == tuple(sessions)
    assert assistant.poll() is None


def test_all_projects_and_jobs_are_prepared_before_any_commit_or_shutdown(tmp_path):
    host, coordinator, sessions = setup(tmp_path)
    calls: list[str] = []
    for session in sessions:
        name = session.binding.project_id
        coordinator.register_project(session.binding, lambda name=name: prepare_logged(
            calls, "prepare " + name, "commit " + name))
    process = FakeProcess(120)
    coordinator.register_owned_process("assistant", "global", process,
                                       lambda: record(calls, "stop assistant"))
    result = coordinator.request_exit_all(lambda: prepare_logged(
        calls, "prepare jobs", "commit jobs"))
    assert result.accepted
    assert calls == ["prepare 0", "prepare 1", "prepare jobs",
                     "commit 0", "commit 1", "commit jobs", "stop assistant"]


def test_project_veto_cancels_previous_permits_and_stops_nothing(tmp_path):
    _, coordinator, sessions = setup(tmp_path)
    calls: list[str] = []
    coordinator.register_project(sessions[0].binding, lambda: PreparedClose(
        lambda: record(calls, "commit"),
        lambda: calls.append("cancel first")))
    coordinator.register_project(sessions[1].binding, lambda: None)
    coordinator.register_owned_process("assistant", "global", FakeProcess(120),
                                       lambda: record(calls, "stop"))
    result = coordinator.request_exit_all(lambda: PreparedClose(lambda: True))
    assert result.code == "project_close_declined"
    assert calls == ["cancel first"]


def test_jobs_veto_preserves_projects_and_processes(tmp_path):
    host, coordinator, sessions = setup(tmp_path)
    calls: list[str] = []
    for session in sessions:
        coordinator.register_project(session.binding, lambda: PreparedClose(
            lambda: record(calls, "commit"),
            lambda: calls.append("cancel")))
    coordinator.register_owned_process("project_host", "one", FakeProcess(120),
                                       lambda: record(calls, "stop"))
    result = coordinator.request_exit_all(lambda: None)
    assert result.code == "active_jobs_declined"
    assert calls == ["cancel", "cancel"]
    assert host.sessions() == tuple(sessions)


def test_prepared_commit_can_close_registered_session_before_shutdown(tmp_path):
    host, coordinator, sessions = setup(tmp_path, count=1)
    binding = sessions[0].binding
    coordinator.register_project(binding, lambda: PreparedClose(
        lambda: host.close_project(binding, prepare_close=lambda _binding, _routes: True)))
    called: list[str] = []
    coordinator.register_owned_process("project_host", "one", FakeProcess(120),
                                       lambda: record(called, "stop"))
    assert coordinator.request_exit_all(lambda: PreparedClose(lambda: True)).accepted
    assert host.sessions() == ()
    assert called == ["stop"]


def test_unregistered_project_blocks_exit(tmp_path):
    _, coordinator, _ = setup(tmp_path)
    called: list[str] = []
    coordinator.register_owned_process("assistant", "global", FakeProcess(120),
                                       lambda: record(called, "stop"))
    assert coordinator.request_exit_all(lambda: PreparedClose(lambda: True)).code == "unregistered_project"
    assert called == []


def test_live_assistant_singleton_and_stale_generation_cannot_unregister_replacement(tmp_path):
    _, coordinator, _ = setup(tmp_path, count=0)
    old = FakeProcess(120)
    old_lease = coordinator.register_owned_process("assistant", "global", old, lambda: True)
    with pytest.raises(LifecycleError, match="already owns") as error:
        coordinator.register_owned_process("assistant", "global", FakeProcess(121), lambda: True)
    assert error.value.code == "process_already_owned"
    old.exit_code = 0
    new = FakeProcess(120)  # PID reuse must not give the old lease ownership.
    new_lease = coordinator.register_owned_process("assistant", "global", new, lambda: True)
    assert old_lease != new_lease
    assert not coordinator.unregister_owned_process(old_lease)
    assert coordinator.unregister_owned_process(new_lease)


def test_exit_only_requests_exact_live_registered_local_handles(tmp_path):
    _, coordinator, _ = setup(tmp_path, count=0)
    called: list[str] = []
    first = FakeProcess(120)
    dead = FakeProcess(121)
    dead.exit_code = 0
    assistant = FakeProcess(122)
    host_lease = coordinator.register_owned_process("project_host", "one", first,
        lambda: record(called, "host"))
    dead_lease = coordinator.register_owned_process("project_host", "two", FakeProcess(121),
        lambda: record(called, "dead"))
    # The retained handle exits after registration; no stale PID command is sent.
    coordinator._processes[("project_host", "two")].handle.exit_code = 0
    assistant_lease = coordinator.register_owned_process("assistant", "global", assistant,
        lambda: record(called, "assistant"))
    unregistered = FakeProcess(123)
    result = coordinator.request_exit_all(lambda: PreparedClose(lambda: True))
    assert result.accepted
    assert result.shutdown_requests == (host_lease, assistant_lease)
    assert called == ["host", "assistant"]
    assert unregistered.poll() is None
    assert dead_lease not in result.shutdown_requests


def test_failed_commit_or_shutdown_never_forces_process_termination(tmp_path):
    _, coordinator, sessions = setup(tmp_path, count=1)
    called: list[str] = []
    coordinator.register_project(sessions[0].binding, lambda: PreparedClose(lambda: False))
    process = FakeProcess(120)
    coordinator.register_owned_process("assistant", "global", process,
                                       lambda: record(called, "stop"))
    assert coordinator.request_exit_all(lambda: PreparedClose(lambda: True)).code == "close_declined"
    assert called == []
    assert process.poll() is None


def test_reentrant_project_open_during_commit_aborts_before_shutdown(tmp_path):
    host, coordinator, sessions = setup(tmp_path, count=1)
    called: list[str] = []

    def commit():
        host.open_project(tmp_path / "new")
        return True

    coordinator.register_project(sessions[0].binding,
                                 lambda: PreparedClose(commit))
    coordinator.register_owned_process("assistant", "global", FakeProcess(120),
                                       lambda: record(called, "stop"))
    result = coordinator.request_exit_all(lambda: PreparedClose(lambda: True))
    assert result.code == "close_failed"
    assert "application_exiting" in result.error
    assert "ProjectSessionError" in result.error
    assert called == []
    assert host.sessions() == tuple(sessions)
    # The gate is released after an aborted exit.
    assert host.open_project(tmp_path / "new").binding.project_id == "new"


def test_open_loading_before_exit_cannot_publish_during_exit(tmp_path):
    loading = Event()
    release = Event()

    def load(root):
        loading.set()
        assert release.wait(2)
        return FakeStore(root)

    host = ProjectSessionHost(load)
    outcomes: list[object] = []

    def open_in_background():
        try:
            outcomes.append(host.open_project(tmp_path / "new"))
        except ProjectSessionError as exc:
            outcomes.append(exc)

    worker = Thread(target=open_in_background)
    worker.start()
    assert loading.wait(2)
    coordinator = ProjectLifecycleCoordinator(host)

    def prepare_jobs():
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        return PreparedClose(lambda: True)

    try:
        assert coordinator.request_exit_all(prepare_jobs).accepted
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], ProjectSessionError)
        assert outcomes[0].code == "application_exiting"
        assert host.sessions() == ()
    finally:
        release.set()
        worker.join(2)


def test_shutdown_exception_reports_exact_prior_requests(tmp_path):
    _, coordinator, _ = setup(tmp_path, count=0)
    called: list[str] = []
    first = coordinator.register_owned_process("project_host", "one", FakeProcess(120),
                                                lambda: record(called, "first"))

    def fail():
        called.append("second")
        raise RuntimeError("transport closed")

    second = coordinator.register_owned_process("assistant", "global", FakeProcess(121), fail)
    coordinator.register_owned_process("launcher", "manager", FakeProcess(122),
                                       lambda: record(called, "third"))
    result = coordinator.request_exit_all(lambda: PreparedClose(lambda: True))
    assert result.code == "shutdown_failed"
    assert result.shutdown_requests == (first,)
    assert result.uncertain_shutdown == second
    assert result.error == "RuntimeError: transport closed"
    assert called == ["first", "second"]
