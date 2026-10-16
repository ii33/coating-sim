"""Headless, opt-in coordination of application-owned local clients.

This is a lifecycle contract, not a process launcher or an IPC server. Desktop
adapters must use the existing authenticated UI control transport for graceful
requests and provide complete dirty-document and active-job close policies.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import RLock
from typing import Callable, Protocol
from uuid import uuid4

from coat_sim.project_session import ProjectBinding, ProjectSessionHost


class LocalProcess(Protocol):
    """A retained handle to a process this application actually started."""

    pid: int

    def poll(self) -> int | None: ...


class LifecycleError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _noop() -> None:
    pass


@dataclass(frozen=True)
class PreparedClose:
    """An adapter-held close decision; prepare must preserve its validity.

    A project adapter owns dirty-document prompts and must prevent new edits
    from invalidating an accepted permit until commit or cancel. Job adapters
    similarly decide whether independently owned tasks may continue or stop.
    Commit must use graceful close, never process termination.
    """

    commit: Callable[[], bool]
    cancel: Callable[[], None] = _noop


@dataclass(frozen=True)
class ProcessLease:
    role: str
    key: str
    generation: str
    pid: int


@dataclass(frozen=True)
class ExitResult:
    code: str
    shutdown_requests: tuple[ProcessLease, ...] = ()
    error: str = ""
    uncertain_shutdown: ProcessLease | None = None

    @property
    def accepted(self) -> bool:
        return self.code == "shutdown_requested"


@dataclass
class _ProcessState:
    lease: ProcessLease
    handle: LocalProcess
    request_shutdown: Callable[[], bool]


@dataclass
class _ProjectState:
    binding: ProjectBinding
    prepare_close: Callable[[], PreparedClose | None]


class ProjectLifecycleCoordinator:
    """In-process registry for exact local ownership and two-phase exit intent.

    It does not discover processes by name/PID, issue OS termination, create a
    globally singleton assistant or claim cross-process project ownership.
    Those require a desktop coordinator using the existing authenticated local
    control transport and verified process start identity/generation.
    """

    PROCESS_ROLES = frozenset({"project_host", "assistant", "launcher"})

    def __init__(self, host: ProjectSessionHost):
        self._host = host
        self._lock = RLock()
        self._processes: dict[tuple[str, str], _ProcessState] = {}
        self._projects: dict[str, _ProjectState] = {}
        self._exiting = False

    def close_launcher(self, dismiss: Callable[[], None]) -> bool:
        """Dismiss only the launcher presentation; leave other owners alive."""
        if not callable(dismiss):
            raise TypeError("dismiss must be callable")
        dismiss()
        return True

    def register_project(
        self, binding: ProjectBinding,
        prepare_close: Callable[[], PreparedClose | None],
    ) -> None:
        if not callable(prepare_close):
            raise TypeError("prepare_close must be callable")
        with self._lock:
            if self._exiting:
                raise LifecycleError("exit_in_progress", "Application exit is in progress")
            if binding not in (session.binding for session in self._host.sessions()):
                raise LifecycleError("stale_project", "Project session is not open")
            previous = self._projects.get(binding.project_id)
            if previous is not None and previous.binding != binding:
                raise LifecycleError("stale_project", "A newer project session is registered")
            self._projects[binding.project_id] = _ProjectState(binding, prepare_close)

    def unregister_project(self, binding: ProjectBinding) -> bool:
        with self._lock:
            current = self._projects.get(binding.project_id)
            if current is None or current.binding != binding:
                return False
            del self._projects[binding.project_id]
            return True

    def register_owned_process(
        self, role: str, key: str, handle: LocalProcess,
        request_shutdown: Callable[[], bool],
    ) -> ProcessLease:
        """Register a retained owned handle, not a discovered PID.

        `request_shutdown` should send the graceful command over the existing
        authenticated control channel. Its True result means *requested*, not
        that the process exited. No PID-based kill is available here.
        """
        if role not in self.PROCESS_ROLES or not isinstance(key, str) or not key:
            raise LifecycleError("invalid_process", "Invalid local process role or key")
        if not isinstance(getattr(handle, "pid", None), int) or handle.pid <= 0:
            raise LifecycleError("invalid_process", "An owned process handle needs a positive PID")
        if not callable(getattr(handle, "poll", None)) or not callable(request_shutdown):
            raise TypeError("A retained process handle and graceful shutdown callback are required")
        if handle.poll() is not None:
            raise LifecycleError("process_exited", "Cannot register an exited process")
        slot = (role, key)
        with self._lock:
            if self._exiting:
                raise LifecycleError("exit_in_progress", "Application exit is in progress")
            previous = self._processes.get(slot)
            if previous is not None and previous.handle.poll() is None:
                raise LifecycleError("process_already_owned", "A live process already owns this slot")
            lease = ProcessLease(role, key, uuid4().hex, handle.pid)
            self._processes[slot] = _ProcessState(lease, handle, request_shutdown)
            return lease

    def unregister_owned_process(self, lease: ProcessLease) -> bool:
        with self._lock:
            slot = (lease.role, lease.key)
            current = self._processes.get(slot)
            if current is None or current.lease != lease:
                return False
            del self._processes[slot]
            return True

    def request_exit_all(
        self, prepare_active_jobs: Callable[[], PreparedClose | None],
    ) -> ExitResult:
        """Preflight every project and job before any close or shutdown request.

        This does not wait for processes to exit. A denied or failed preflight
        sends no shutdown requests. Adapters must provide stable prepared
        decisions and keep native windows intact until commit.
        """
        if not callable(prepare_active_jobs):
            raise TypeError("prepare_active_jobs must be callable")
        with self._lock:
            if self._exiting:
                return ExitResult("exit_in_progress")
            self._exiting = True
        prepared: list[PreparedClose] = []
        requested: list[ProcessLease] = []
        committed = 0
        stage = "prepare"
        inflight: ProcessLease | None = None
        try:
            # The host gate starts before the session snapshot and stays held
            # through the final graceful request. Reentrant callbacks cannot
            # publish another project after it has been preflighted.
            with self._host.exit_admission_freeze():
                with self._lock:
                    sessions = self._host.sessions()
                    projects = tuple(self._projects.get(item.binding.project_id) for item in sessions)
                    processes = tuple(self._processes.values())
                if any(project is None or project.binding != session.binding
                       for project, session in zip(projects, sessions, strict=True)):
                    return ExitResult("unregistered_project")
                for project in projects:
                    assert project is not None
                    permit = project.prepare_close()
                    if permit is None:
                        return ExitResult("project_close_declined")
                    if not isinstance(permit, PreparedClose):
                        raise LifecycleError("invalid_close_decision", "Project prepare must return a close permit")
                    prepared.append(permit)
                job_permit = prepare_active_jobs()
                if job_permit is None:
                    return ExitResult("active_jobs_declined")
                if not isinstance(job_permit, PreparedClose):
                    raise LifecycleError("invalid_close_decision", "Job prepare must return a close permit")
                prepared.append(job_permit)
                if tuple(item.binding for item in self._host.sessions()) != tuple(item.binding for item in sessions):
                    return ExitResult("projects_changed")
                stage = "commit"
                for permit in prepared:
                    accepted = permit.commit()
                    if type(accepted) is not bool:
                        raise LifecycleError("invalid_close_decision", "Close commit must return a bool")
                    if not accepted:
                        return ExitResult("close_declined")
                    committed += 1
                original = {item.binding for item in sessions}
                if any(item.binding not in original for item in self._host.sessions()):
                    return ExitResult("projects_changed")
                stage = "shutdown"
                for process in sorted(processes, key=lambda item:
                                      {"project_host": 0, "assistant": 1, "launcher": 2}[item.lease.role]):
                    with self._lock:
                        current = self._processes.get((process.lease.role, process.lease.key))
                        if current is not process:
                            continue
                    inflight = process.lease
                    if process.handle.poll() is not None:
                        inflight = None
                        continue
                    if process.request_shutdown() is not True:
                        return ExitResult("shutdown_refused", tuple(requested),
                                          uncertain_shutdown=inflight)
                    requested.append(process.lease)
                    inflight = None
                return ExitResult("shutdown_requested", tuple(requested))
        except Exception as exc:
            error_code = getattr(exc, "code", "")
            label = f"{type(exc).__name__}[{error_code}]" if error_code else type(exc).__name__
            return ExitResult("shutdown_failed" if stage == "shutdown" else "close_failed",
                              tuple(requested), f"{label}: {exc}",
                              inflight if stage == "shutdown" else None)
        finally:
            for permit in reversed(prepared[committed:]):
                try:
                    permit.cancel()
                except Exception:
                    logging.getLogger(__name__).exception("Failed to release prepared close permit")
            with self._lock:
                self._exiting = False
