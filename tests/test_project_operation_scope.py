"""Headless contracts for atomic project-operation admission and close."""

from pathlib import Path
from threading import Event, Thread

import pytest

from coat_sim.project_session import ProjectSessionError, ProjectSessionHost


pytestmark = pytest.mark.contract


class _FakeStore:
    def __init__(self, root: Path):
        self.root = root
        self._manifest = {"id": "project-one", "name": "Project one"}
        self.mutations: list[str] = []

    @property
    def manifest(self):
        return dict(self._manifest)


def _opened_host(tmp_path):
    store = _FakeStore(tmp_path / "one")
    host = ProjectSessionHost(lambda _root: store)
    session = host.open_project(store.root)
    return host, session.binding, store


def _assert_error(code, call):
    with pytest.raises(ProjectSessionError) as error:
        call()
    assert error.value.code == code


def _enter_scope(host, binding):
    with host.operation_scope(binding):
        pass


def test_operation_scope_yields_owned_store_and_supports_reentrant_nesting(tmp_path):
    host, binding, store = _opened_host(tmp_path)

    with host.operation_scope(binding) as scoped_store:
        assert scoped_store is store
        scoped_store.mutations.append("outer")
        with host.operation_scope(binding) as nested_store:
            assert nested_store is store
            nested_store.mutations.append("inner")

    assert store.mutations == ["outer", "inner"]


def test_old_binding_cannot_enter_scope_after_close_and_reopen(tmp_path):
    host, old_binding, _store = _opened_host(tmp_path)
    assert host.close_project(old_binding, prepare_close=lambda _binding, _routes: True)
    new_session = host.open_project(old_binding.root)
    assert new_session.binding.generation != old_binding.generation

    _assert_error("stale_session", lambda: _enter_scope(host, old_binding))


def test_operation_scope_is_rejected_during_close_callback(tmp_path):
    host, binding, _store = _opened_host(tmp_path)
    observed = []

    def prepare_close(_binding, _routes):
        _assert_error("project_closing", lambda: _enter_scope(host, binding))
        observed.append("rejected")
        return False

    assert not host.close_project(binding, prepare_close=prepare_close)
    assert observed == ["rejected"]
    # A denied close restores admission once its callback returns.
    with host.operation_scope(binding) as scoped_store:
        assert scoped_store is not None


def test_close_policy_scope_is_callback_thread_only_and_denial_restores_admission(tmp_path):
    host, binding, store = _opened_host(tmp_path)
    attempt_started = Event()
    attempt_finished = Event()
    worker_errors = []
    worker_threads = []

    def prepare_close(_binding, _routes):
        def use_close_policy_scope_from_other_thread():
            attempt_started.set()
            try:
                with host.operation_scope(binding, close_policy=True):
                    worker_errors.append("unexpectedly admitted")
            except ProjectSessionError as error:
                worker_errors.append(error.code)
            finally:
                attempt_finished.set()

        # The callback thread is admitted while its explicit close decision runs.
        with host.operation_scope(binding, close_policy=True) as scoped_store:
            assert scoped_store is store
            worker = Thread(target=use_close_policy_scope_from_other_thread, daemon=True)
            worker_threads.append(worker)
            worker.start()
            assert attempt_started.wait(timeout=2), "other-thread scope attempt did not start"

        # The worker can now acquire the registry lock, but remains outside the
        # callback's thread and must still be rejected while closing is pending.
        assert attempt_finished.wait(timeout=2), "other-thread scope attempt did not finish"
        worker.join(timeout=2)
        assert not worker.is_alive(), "other-thread scope attempt did not exit"
        return False

    assert not host.close_project(binding, prepare_close=prepare_close)
    assert worker_errors == ["project_closing"]
    assert worker_threads and not worker_threads[0].is_alive()

    # A denied close clears the closing gate for ordinary operations.
    with host.operation_scope(binding) as scoped_store:
        assert scoped_store is store


def test_mutation_scope_keeps_concurrent_close_out_of_callback_until_exit(tmp_path):
    host, binding, store = _opened_host(tmp_path)
    scope_entered = Event()
    release_scope = Event()
    close_call_started = Event()
    close_callback_entered = Event()
    close_result = []
    thread_errors = []

    def mutate_in_scope():
        try:
            with host.operation_scope(binding) as scoped_store:
                assert scoped_store is store
                scoped_store.mutations.append("commit")
                scope_entered.set()
                if not release_scope.wait(timeout=3):
                    raise AssertionError("test did not release operation scope")
        except BaseException as exc:  # surfaced in the joining test thread
            thread_errors.append(exc)

    def close_project():
        close_call_started.set()

        def prepare_close(_binding, _routes):
            close_callback_entered.set()
            return True

        try:
            close_result.append(host.close_project(binding, prepare_close=prepare_close))
        except BaseException as exc:  # surfaced in the joining test thread
            thread_errors.append(exc)

    operation_thread = Thread(target=mutate_in_scope, daemon=True)
    close_thread = Thread(target=close_project, daemon=True)
    operation_thread.start()
    assert scope_entered.wait(timeout=2), "operation scope did not start"

    close_thread.start()
    assert close_call_started.wait(timeout=2), "close call did not start"
    try:
        # The close policy cannot run while the mutation holds the host lock.
        assert not close_callback_entered.wait(timeout=0.1)
        assert operation_thread.is_alive()
    finally:
        release_scope.set()

    operation_thread.join(timeout=2)
    close_thread.join(timeout=2)
    assert not operation_thread.is_alive(), "operation thread did not finish"
    assert not close_thread.is_alive(), "close thread did not finish"
    assert thread_errors == []
    assert close_callback_entered.is_set()
    assert close_result == [True]
    assert store.mutations == ["commit"]
