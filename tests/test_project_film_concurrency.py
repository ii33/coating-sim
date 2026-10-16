"""Deterministic concurrency contract for project-film draft activation."""

from contextlib import contextmanager
from threading import Event, Thread, current_thread, local

import pytest

from coat_sim.project_session import ProjectSessionError
from tests.test_project_film_default_draft import frozen_film_project, workspace_for


pytestmark = pytest.mark.contract


class _LockOrderProbe(RuntimeError):
    """Short-circuit a lock inversion instead of leaving a deadlocked thread."""


class _ObservedRLock:
    def __init__(self, lock, thread_name, attempted):
        self._lock = lock
        self._thread_name = thread_name
        self._attempted = attempted

    def acquire(self, *args, **kwargs):
        if current_thread().name == self._thread_name:
            self._attempted.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        return self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


def _track_initializer_host_scope(monkeypatch, workspace):
    active = Event()
    depth = local()
    original_scope = workspace.host.operation_scope

    @contextmanager
    def observed_scope(binding, *, close_policy=False):
        with original_scope(binding, close_policy=close_policy) as store_view:
            tracks_initializer = current_thread().name == "frozen-initializer"
            if tracks_initializer:
                depth.value = getattr(depth, "value", 0) + 1
                active.set()
            try:
                yield store_view
            finally:
                if tracks_initializer:
                    depth.value -= 1
                    if depth.value == 0:
                        active.clear()

    monkeypatch.setattr(workspace.host, "operation_scope", observed_scope)
    return active


def test_save_and_frozen_activation_use_editor_before_host_lock(tmp_path, monkeypatch):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor

    create_entered = Event()
    initializer_lock_attempted = Event()
    initializer_holds_host = _track_initializer_host_scope(monkeypatch, workspace)
    inversion_would_block = Event()
    original_create_film = workspace.service.create_film

    monkeypatch.setattr(
        editor, "_lock",
        _ObservedRLock(editor._lock, "frozen-initializer", initializer_lock_attempted))

    def gated_create_film(*args, **kwargs):
        # save_current reaches this only after taking the editor lock. Let the
        # initializer reach its first editor-lock attempt before proceeding.
        create_entered.set()
        if not initializer_lock_attempted.wait(5):
            raise AssertionError("initializer never attempted the editor lock")
        return original_create_film(*args, **kwargs)

    @contextmanager
    def guarded_operation_scope(binding, *, close_policy=False):
        if (current_thread().name == "film-save" and
                initializer_holds_host.is_set()):
            # Under the old host->editor order, the initializer has already
            # entered this scope and is blocked on the editor lock held by the
            # saver. The saver would now block acquiring this host scope. Raise
            # a sentinel here so the regression test itself cannot hang.
            inversion_would_block.set()
            raise _LockOrderProbe("editor/host lock inversion")
        with tracked_operation_scope(binding, close_policy=close_policy) as store_view:
            yield store_view

    monkeypatch.setattr(workspace.service, "create_film", gated_create_film)
    tracked_operation_scope = workspace.host.operation_scope
    monkeypatch.setattr(workspace.host, "operation_scope", guarded_operation_scope)
    results: dict[str, object] = {}

    def save():
        try:
            results["save"] = editor.save_current()
        except BaseException as exc:  # Capture probe errors from the old ordering.
            results["save_error"] = exc

    def initialize():
        try:
            results["initialize"] = workspace.initialize_frozen_draft(frozen)
        except BaseException as exc:
            results["initialize_error"] = exc

    save_thread = Thread(target=save, name="film-save", daemon=True)
    initializer_thread = Thread(target=initialize, name="frozen-initializer", daemon=True)
    save_thread.start()
    try:
        assert create_entered.wait(5), "save never entered project film creation"
        initializer_thread.start()
        save_thread.join(10)
        initializer_thread.join(10)
        assert not save_thread.is_alive(), "save remained blocked"
        assert not initializer_thread.is_alive(), "initializer remained blocked"
    finally:
        # The waits above are bounded and the sentinel prevents the historical
        # inversion from becoming a real deadlock. Join any started worker so
        # a failing assertion cannot leak threads into the rest of pytest.
        if initializer_thread.ident is not None:
            initializer_thread.join(10)
        save_thread.join(10)

    assert not inversion_would_block.is_set(), "initializer held host while waiting for editor"
    assert "save_error" not in results
    assert results.get("save") is True
    assert "initialize_error" not in results
    assert results.get("initialize") is False
    assert editor.snapshot().path is not None
    assert not editor.snapshot().dirty


def test_direct_initializer_holds_project_admission_through_material_refresh(
        tmp_path, monkeypatch):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor

    activation_returned = Event()
    release_initializer = Event()
    initializer_holds_host = _track_initializer_host_scope(monkeypatch, workspace)
    close_lock_attempted = Event()
    close_callback_entered = Event()
    original_activate = workspace.service.activate_frozen_draft_materials
    original_host_lock = workspace.host._lock
    monkeypatch.setattr(
        workspace.host, "_lock",
        _ObservedRLock(original_host_lock, "close-project", close_lock_attempted))

    def gated_activate(records):
        original_activate(records)
        activation_returned.set()
        if not release_initializer.wait(5):
            raise AssertionError("test did not release frozen initialization")

    monkeypatch.setattr(workspace.service, "activate_frozen_draft_materials", gated_activate)
    results: dict[str, object] = {}

    def initialize():
        try:
            results["initialize"] = editor.initialize_frozen_draft(frozen)
        except BaseException as exc:
            results["initialize_error"] = exc

    def close():
        def prepare_close(_binding, _routes):
            close_callback_entered.set()
            return True

        try:
            results["close"] = workspace.host.close_project(
                workspace.binding, prepare_close=prepare_close)
        except BaseException as exc:
            results["close_error"] = exc

    initializer_thread = Thread(target=initialize, name="frozen-initializer", daemon=True)
    close_thread = Thread(target=close, name="close-project", daemon=True)
    initializer_thread.start()
    try:
        assert activation_returned.wait(5), "frozen materials were not activated"
        active_during_refresh = initializer_holds_host.is_set()
        close_thread.start()
        assert close_lock_attempted.wait(5), "close never attempted project admission"
        if active_during_refresh:
            # The close thread has reached the host lock. Since the initializer
            # still owns its complete host scope, close cannot enter its policy
            # callback until initialization leaves that scope.
            assert not close_callback_entered.is_set()
        else:
            # On the old direct API the activation service scope has ended;
            # allow the project close to overtake the still-paused initializer.
            assert close_callback_entered.wait(5), "close did not overtake released admission"
        release_initializer.set()
        initializer_thread.join(10)
        close_thread.join(10)
        assert not initializer_thread.is_alive(), "initializer remained blocked"
        assert not close_thread.is_alive(), "project close remained blocked"
    finally:
        release_initializer.set()
        initializer_thread.join(10)
        if close_thread.ident is not None:
            close_thread.join(10)

    assert active_during_refresh, "initializer released host admission before refreshing materials"
    assert "initialize_error" not in results
    assert results.get("initialize") is True
    assert "close_error" not in results
    assert results.get("close") is True
    assert close_callback_entered.is_set()
    assert workspace.service._draft_materials is not None


@pytest.mark.parametrize(
    ("admission_gate", "expected_code"),
    [
        ("closing", "project_closing"),
        ("application_exit", "application_exiting"),
        ("closed", "project_closed"),
        ("reopened_generation", "stale_session"),
    ],
)
def test_direct_initializer_rejects_unavailable_admission_before_overlay(
        tmp_path, admission_gate, expected_code):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor
    before = editor.snapshot()
    caught = {}

    def initialize_during_close(_binding, _routes):
        try:
            editor.initialize_frozen_draft(frozen)
        except ProjectSessionError as exc:
            caught["error"] = exc
        return False

    if admission_gate == "closing":
        assert not workspace.host.close_project(
            workspace.binding, prepare_close=initialize_during_close)
    elif admission_gate == "application_exit":
        with workspace.host.exit_admission_freeze():
            try:
                editor.initialize_frozen_draft(frozen)
            except ProjectSessionError as exc:
                caught["error"] = exc
    else:
        assert workspace.host.close_project(
            workspace.binding, prepare_close=lambda _binding, _routes: True)
        if admission_gate == "reopened_generation":
            workspace.host.open_project(store.root)
        try:
            editor.initialize_frozen_draft(frozen)
        except ProjectSessionError as exc:
            caught["error"] = exc

    assert isinstance(caught.get("error"), ProjectSessionError)
    assert caught["error"].code == expected_code
    assert editor.snapshot() == before
    assert workspace.service._draft_materials is None
