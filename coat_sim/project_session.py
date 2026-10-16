"""Project-scoped routing foundation, independent of Qt and process transport.

The host owns identities and opaque window registrations, never document edits,
native window lifetime, or process termination. A desktop adapter supplies the
project store and the complete unsaved-document close policy.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from threading import RLock, get_ident
from typing import Any, Callable, Iterator, Protocol
from uuid import uuid4

from .project_modules import PROJECT_MODULES


PROJECT_MODULE_KEYS = frozenset(key for key, _name in PROJECT_MODULES)
PROJECT_ENTRY_MODULE = "film_design"


class ProjectStoreView(Protocol):
    """Read-only identity boundary implemented by the headless ProjectStore."""

    root: Path

    @property
    def manifest(self) -> dict[str, Any]: ...


class ProjectSessionError(ValueError):
    def __init__(self, code: str, message: str, *, project_id: str = ""):
        self.code = code
        self.project_id = project_id
        super().__init__(message)


@dataclass(frozen=True)
class ProjectBinding:
    project_id: str
    root: Path
    generation: str


@dataclass(frozen=True)
class ProjectSession:
    binding: ProjectBinding
    name: str
    entry_module: str = PROJECT_ENTRY_MODULE


@dataclass(frozen=True)
class ModuleRoute:
    binding: ProjectBinding
    module_key: str
    route_id: str


@dataclass(frozen=True)
class ModuleReservation:
    route: ModuleRoute
    created: bool
    window: Any | None


@dataclass(frozen=True)
class OperationBinding:
    session: ProjectBinding
    operation_id: str


@dataclass
class _WindowState:
    route: ModuleRoute
    window: Any | None = None


@dataclass
class _SessionState:
    session: ProjectSession
    store: ProjectStoreView
    windows: dict[str, _WindowState] = field(default_factory=dict)
    closing: bool = False
    closing_thread: int | None = None
    resources: dict[str, Any] = field(default_factory=dict)


def _nonempty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectSessionError("invalid_identity", f"{field_name} must be a nonempty string")
    return value


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root))


class ProjectSessionHost:
    """An in-process registry; it does not enforce a cross-process singleton.

    Opening invokes the injected store factory only for an unregistered root.
    Its result must already be validated by the persistence service. Admission
    captures explicit project identity; no method consults foreground focus.
    """

    def __init__(self, store_factory: Callable[[Path], ProjectStoreView]):
        self._store_factory = store_factory
        self._sessions: dict[str, _SessionState] = {}
        self._roots: dict[str, str] = {}
        self._lock = RLock()
        self._exit_thread: int | None = None

    @property
    def admission_frozen(self) -> bool:
        with self._lock:
            return self._exit_thread is not None

    def _reject_exit_admission(self) -> None:
        if self._exit_thread is not None:
            raise ProjectSessionError("application_exiting", "Application exit is in progress")

    @contextmanager
    def exit_admission_freeze(self) -> Iterator[None]:
        """Block new project/window/operation admission during coordinated exit.

        An open already loading a store must recheck the gate before publish.
        Closing and dirty-document policy operations remain adapter-owned.
        This is not a lock on existing document edits or external processes.
        """
        with self._lock:
            self._reject_exit_admission()
            self._exit_thread = get_ident()
        try:
            yield
        finally:
            with self._lock:
                self._exit_thread = None

    def open_project(self, root: Path | str) -> ProjectSession:
        requested_root = Path(root).expanduser().absolute()
        canonical_root = requested_root.resolve()
        key = _root_key(canonical_root)
        with self._lock:
            self._reject_exit_admission()
            existing_id = self._roots.get(key)
            if existing_id is not None and requested_root == canonical_root:
                return self._state(existing_id).session

        # Opening is read-only. Do not hold the registry lock while loading a
        # store; competing opens are reconciled before publishing ownership.
        # Preserve the requested path for the store's link/reparse validation;
        # canonicalization must not turn a denied linked root into a safe one.
        store = self._store_factory(requested_root)
        if _root_key(Path(store.root).resolve()) != key:
            raise ProjectSessionError("root_mismatch", "The store belongs to a different project root")
        manifest = store.manifest
        # The persistence manifest calls this field "id". Resolved graphs use
        # "project_id"; routing must not invent a second manifest schema.
        project_id = _nonempty(manifest.get("id"), "project_id")
        name = _nonempty(manifest.get("name"), "project name")
        with self._lock:
            self._reject_exit_admission()
            existing_id = self._roots.get(key)
            if existing_id is not None:
                if existing_id != project_id:
                    raise ProjectSessionError(
                        "root_identity_conflict", "This root already has a different writable owner",
                        project_id=project_id)
                return self._state(existing_id).session
            if project_id in self._sessions:
                raise ProjectSessionError(
                    "project_identity_conflict",
                    "A different root has this project ID; explicitly clone/reidentify the copy first",
                    project_id=project_id)
            session = ProjectSession(ProjectBinding(project_id, canonical_root, uuid4().hex), name)
            self._sessions[project_id] = _SessionState(session, store)
            self._roots[key] = project_id
            return session

    def sessions(self) -> tuple[ProjectSession, ...]:
        with self._lock:
            return tuple(state.session for state in self._sessions.values())

    def refresh_project_information(self, binding: ProjectBinding) -> ProjectSession:
        """Refresh display metadata without changing a session's identity/resources."""
        with self.operation_scope(binding) as store:
            state = self._state(binding.project_id, binding=binding)
            manifest = store.manifest
            if manifest.get("id") != binding.project_id:
                raise ProjectSessionError("project_identity_mismatch", "The owning store identity changed",
                                          project_id=binding.project_id)
            name = _nonempty(manifest.get("name"), "project name")
            state.session = replace(state.session, name=name)
            return state.session

    def _state(self, project_id: str, *, binding: ProjectBinding | None = None,
               allow_closing: bool = False) -> _SessionState:
        _nonempty(project_id, "project_id")
        state = self._sessions.get(project_id)
        if state is None:
            raise ProjectSessionError("project_closed", "The specified project is not open",
                                      project_id=project_id)
        if binding is not None and state.session.binding != binding:
            raise ProjectSessionError("stale_session", "The originating project session has ended",
                                      project_id=project_id)
        if state.closing and not allow_closing:
            raise ProjectSessionError("project_closing", "The specified project is awaiting its close decision",
                                      project_id=project_id)
        return state

    def store_for(self, binding: ProjectBinding) -> ProjectStoreView:
        with self._lock:
            return self._state(binding.project_id, binding=binding).store

    @contextmanager
    def operation_scope(self, binding: ProjectBinding, *,
                        close_policy: bool = False) -> Iterator[ProjectStoreView]:
        """Admit a bound service operation atomically against project close.

        The registry lock remains held until the operation leaves. Consumers
        must enter before their own service/store locks and must not wait for
        GUI callbacks inside the scope. Reentrant same-thread calls are safe.
        A domain service may opt into close-policy operations, but only the
        thread executing this project's explicit prepare_close callback can
        use that exception; unrelated callers remain blocked while closing.
        """
        with self._lock:
            if self._exit_thread is not None and (not close_policy or self._exit_thread != get_ident()):
                raise ProjectSessionError("application_exiting", "Application exit is in progress",
                                          project_id=binding.project_id)
            state = self._state(binding.project_id, binding=binding, allow_closing=True)
            if state.closing and (not close_policy or state.closing_thread != get_ident()):
                raise ProjectSessionError("project_closing", "The project is awaiting its close decision",
                                          project_id=binding.project_id)
            yield state.store

    def resource_for(self, binding: ProjectBinding, resource_key: str) -> Any | None:
        """Return a session-owned resource without consulting foreground UI."""
        _nonempty(resource_key, "resource_key")
        with self._lock:
            return self._state(binding.project_id, binding=binding).resources.get(resource_key)

    def claim_resource(self, binding: ProjectBinding, resource_key: str, resource: Any) -> None:
        """Keep one service/draft owner for a named resource in this generation."""
        _nonempty(resource_key, "resource_key")
        if resource is None:
            raise ValueError("A project resource must have an owner")
        with self._lock:
            self._reject_exit_admission()
            state = self._state(binding.project_id, binding=binding)
            existing = state.resources.get(resource_key)
            if existing is not None and existing is not resource:
                raise ProjectSessionError("resource_already_owned", "The project resource already has an owner",
                                          project_id=binding.project_id)
            state.resources[resource_key] = resource

    def reserve_module(self, project_id: str, module_key: str) -> ModuleReservation:
        """Publish the pending slot before a caller constructs expensive UI.

        A repeated/reentrant request returns the same route, including while its
        window is pending. It does not replace the document or replay a payload.
        """
        if not isinstance(module_key, str) or module_key not in PROJECT_MODULE_KEYS:
            raise ProjectSessionError("invalid_module", "Unknown or application-scoped module",
                                      project_id=project_id)
        with self._lock:
            self._reject_exit_admission()
            state = self._state(project_id)
            existing = state.windows.get(module_key)
            if existing is not None:
                return ModuleReservation(existing.route, False, existing.window)
            route = ModuleRoute(state.session.binding, module_key, uuid4().hex)
            state.windows[module_key] = _WindowState(route)
            return ModuleReservation(route, True, None)

    def _route_state(self, route: ModuleRoute, *, allow_closing: bool = False) -> _WindowState:
        state = self._state(route.binding.project_id, binding=route.binding,
                            allow_closing=allow_closing)
        window_state = state.windows.get(route.module_key)
        if window_state is None or window_state.route != route:
            raise ProjectSessionError("stale_route", "The originating module reservation has ended",
                                      project_id=route.binding.project_id)
        return window_state

    def attach_window(self, route: ModuleRoute, window: Any) -> None:
        """Commit a completed open; stale completion never reaches a fresh slot.

        A rejected window remains the adapter's cleanup responsibility. This
        method deliberately cannot destroy Qt objects from a worker thread.
        """
        if window is None:
            raise ProjectSessionError("invalid_window", "A completed open requires a window",
                                      project_id=route.binding.project_id)
        with self._lock:
            self._reject_exit_admission()
            state = self._route_state(route)
            if state.window is not None and state.window is not window:
                raise ProjectSessionError("window_already_attached", "The module already owns a window",
                                          project_id=route.binding.project_id)
            state.window = window

    def window_for(self, route: ModuleRoute) -> Any | None:
        with self._lock:
            return self._route_state(route).window

    def routes(self, project_id: str) -> tuple[ModuleRoute, ...]:
        with self._lock:
            return tuple(item.route for item in self._state(project_id).windows.values())

    def release_window(self, route: ModuleRoute) -> bool:
        """Forget a failed/closed registration without closing its native window.

        An old destruction callback cannot clear a newer generation/reservation.
        Cleanup is also permitted inside the explicit project-close callback.
        """
        with self._lock:
            try:
                self._route_state(route, allow_closing=True)
            except ProjectSessionError as exc:
                if exc.code in {"project_closed", "stale_session", "stale_route"}:
                    return False
                raise
            del self._sessions[route.binding.project_id].windows[route.module_key]
            return True

    def capture_operation(self, project_id: str, operation_id: str) -> OperationBinding:
        """Capture a target only; document revision checks belong to its service.

        This is not a task executor or operation-id deduplication store. Callers
        must still freeze inputs and check expected revisions before mutations.
        """
        _nonempty(operation_id, "operation_id")
        with self._lock:
            self._reject_exit_admission()
            return OperationBinding(self._state(project_id).session.binding, operation_id)

    def resolve_operation(self, operation: OperationBinding) -> ProjectStoreView:
        """Check the captured target now; this is not a revocable write lease.

        A caller retaining the returned store must still revalidate admission
        and revision at its service-owned commit/writeback boundary. Registry
        locking does not make document mutations atomic with project close.
        """
        return self.store_for(operation.session)

    def close_project(
        self, binding: ProjectBinding, *,
        prepare_close: Callable[[ProjectBinding, tuple[ModuleRoute, ...]], bool],
    ) -> bool:
        """Unregister only after the adapter accepts its complete close policy.

        The callback owns dirty-document decisions, saves, cancellation, and
        native-window cleanup. It must return a strict bool; rejection/failure
        preserves registrations not explicitly released by that callback. New
        admissions are blocked while it runs. This never terminates a process.
        """
        with self._lock:
            if self._exit_thread is not None and self._exit_thread != get_ident():
                raise ProjectSessionError("application_exiting", "Application exit is in progress",
                                          project_id=binding.project_id)
            state = self._state(binding.project_id, binding=binding)
            state.closing = True
            state.closing_thread = get_ident()
            routes = tuple(item.route for item in state.windows.values())
        try:
            accepted = prepare_close(binding, routes)
            if type(accepted) is not bool:
                raise ProjectSessionError("invalid_close_decision", "The close policy must return a bool",
                                          project_id=binding.project_id)
        except BaseException:
            with self._lock:
                state.closing = False
                state.closing_thread = None
            raise
        with self._lock:
            if not accepted:
                state.closing = False
                state.closing_thread = None
                return False
            del self._sessions[binding.project_id]
            del self._roots[_root_key(binding.root)]
            return True
