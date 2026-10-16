"""Headless routing contracts; these do not claim integrated desktop behavior."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from coat_sim.project_session import ProjectSessionError, ProjectSessionHost


pytestmark = pytest.mark.contract


class FakeStore:
    def __init__(self, root, project_id, name="Empty project"):
        self.root = Path(root)
        self._manifest = {"id": project_id, "name": name, "documents": [],
                          "default_configuration_id": None, "revision": 0}

    @property
    def manifest(self):
        return deepcopy(self._manifest)


def host_with_projects(tmp_path):
    stores = {tmp_path / "one": FakeStore(tmp_path / "one", "project-one"),
              tmp_path / "two": FakeStore(tmp_path / "two", "project-two")}
    host = ProjectSessionHost(lambda root: stores[root])
    first = host.open_project(tmp_path / "one")
    second = host.open_project(tmp_path / "two")
    return host, first, second, stores


def assert_error(code, call):
    with pytest.raises(ProjectSessionError) as error:
        call()
    assert error.value.code == code
    return error.value


def test_empty_project_has_film_entry_without_opening_ui_or_resolving_recovery(tmp_path):
    store = FakeStore(tmp_path / "one", "project-one")
    host = ProjectSessionHost(lambda root: store)
    session = host.open_project(store.root)
    assert session.entry_module == "film_design"
    assert host.routes(session.binding.project_id) == ()
    assert host.store_for(session.binding) is store


def test_repeat_open_keeps_owner_store_and_working_state(tmp_path):
    calls = []

    def factory(root):
        calls.append(root)
        return FakeStore(root, "project-one")

    host = ProjectSessionHost(factory)
    first = host.open_project(tmp_path / "one")
    route = host.reserve_module("project-one", "film_design").route
    second = host.open_project(tmp_path / "one" / ".")
    assert second is first
    assert calls == [tmp_path / "one"]
    assert host.routes("project-one") == (route,)


def test_distinct_roots_with_copied_id_need_explicit_reidentification(tmp_path):
    host = ProjectSessionHost(lambda root: FakeStore(root, "copied-id"))
    first = host.open_project(tmp_path / "one")
    error = assert_error("project_identity_conflict", lambda: host.open_project(tmp_path / "two"))
    assert error.project_id == "copied-id"
    assert host.sessions() == (first,)


def test_two_projects_same_module_and_pending_open_are_independent(tmp_path):
    host, first, second, _ = host_with_projects(tmp_path)
    first_open = host.reserve_module(first.binding.project_id, "film_design")
    second_open = host.reserve_module(second.binding.project_id, "film_design")
    assert first_open.created and second_open.created
    assert first_open.route != second_open.route
    first_window, second_window = object(), object()
    host.attach_window(first_open.route, first_window)
    host.attach_window(second_open.route, second_window)
    assert host.window_for(first_open.route) is first_window
    assert host.window_for(second_open.route) is second_window


def test_reentrant_request_sees_published_pending_reservation(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)
    initial = host.reserve_module(first.binding.project_id, "source")
    repeated = host.reserve_module(first.binding.project_id, "source")
    assert initial.created and initial.window is None
    assert not repeated.created and repeated.route is initial.route
    assert repeated.window is None
    window = object()
    host.attach_window(initial.route, window)
    reopened = host.reserve_module(first.binding.project_id, "source")
    assert not reopened.created and reopened.window is window


def test_threaded_pending_reservation_publishes_only_one_owner(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)
    barrier = Barrier(8)

    def reserve(_):
        barrier.wait()
        return host.reserve_module(first.binding.project_id, "machine")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(item.created for item in results) == 1
    assert len({item.route for item in results}) == 1


def test_concurrent_opens_reconcile_same_root_after_loading(tmp_path):
    barrier = Barrier(2)

    def factory(root):
        barrier.wait()
        return FakeStore(root, "project-one")

    host = ProjectSessionHost(factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(host.open_project, [tmp_path / "one"] * 2))
    assert results[0] is results[1]
    assert len(host.sessions()) == 1


def test_concurrent_conflicting_ids_cannot_replace_same_root_owner(tmp_path):
    barrier = Barrier(2)

    def open_with_id(identity):
        def factory(root):
            barrier.wait()
            return FakeStore(root, identity)
        return factory

    # The shared factory identifies each loading thread without global roots.
    from threading import current_thread
    identities: dict[str, str] = {}

    def factory(root):
        identity = identities[current_thread().name]
        return open_with_id(identity)(root)

    host = ProjectSessionHost(factory)

    def run(identity):
        identities[current_thread().name] = identity
        try:
            return host.open_project(tmp_path / "one")
        except ProjectSessionError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["one", "two"]))
    assert len(host.sessions()) == 1
    errors = [item for item in results if isinstance(item, ProjectSessionError)]
    assert len(errors) == 1 and errors[0].code == "root_identity_conflict"


def test_attach_is_idempotent_but_cannot_replace_a_live_window(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)
    route = host.reserve_module(first.binding.project_id, "mask").route
    window = object()
    host.attach_window(route, window)
    host.attach_window(route, window)
    assert_error("window_already_attached", lambda: host.attach_window(route, object()))
    assert host.window_for(route) is window


def test_failed_open_can_retry_without_old_completion_or_release_erasing_it(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)
    old = host.reserve_module(first.binding.project_id, "film_design").route
    assert host.release_window(old)
    fresh = host.reserve_module(first.binding.project_id, "film_design").route
    assert fresh.route_id != old.route_id
    assert_error("stale_route", lambda: host.attach_window(old, object()))
    assert not host.release_window(old)
    assert host.routes(first.binding.project_id) == (fresh,)


def test_denied_dirty_close_preserves_project_windows_and_operations(tmp_path):
    host, first, _, stores = host_with_projects(tmp_path)
    route = host.reserve_module(first.binding.project_id, "film_design").route
    window = object()
    host.attach_window(route, window)
    operation = host.capture_operation(first.binding.project_id, "save-one")
    seen = []

    def deny(binding, routes):
        seen.append((binding, routes))
        return False

    assert not host.close_project(first.binding, prepare_close=deny)
    assert seen == [(first.binding, (route,))]
    assert host.window_for(route) is window
    assert host.resolve_operation(operation) is stores[first.binding.root]


def test_failed_close_policy_restores_admission(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)

    def fail(binding, routes):
        raise RuntimeError("save failed")

    with pytest.raises(RuntimeError, match="save failed"):
        host.close_project(first.binding, prepare_close=fail)
    assert host.capture_operation(first.binding.project_id, "retry").session == first.binding


@pytest.mark.parametrize("decision", [None, 1, "yes"])
def test_close_policy_requires_strict_boolean_decision(tmp_path, decision):
    host, first, _, _ = host_with_projects(tmp_path)
    assert_error("invalid_close_decision", lambda: host.close_project(
        first.binding, prepare_close=lambda binding, routes: decision))
    assert host.store_for(first.binding) is not None


def test_close_gates_new_work_but_other_project_stays_available(tmp_path):
    host, first, second, _ = host_with_projects(tmp_path)
    route = host.reserve_module(first.binding.project_id, "film_design").route
    window = object()
    host.attach_window(route, window)

    def allow(binding, routes):
        assert_error("project_closing", lambda: host.reserve_module(binding.project_id, "source"))
        assert_error("project_closing", lambda: host.capture_operation(binding.project_id, "late"))
        assert_error("project_closing", lambda: host.open_project(binding.root))
        assert host.reserve_module(second.binding.project_id, "source").created
        assert host.release_window(routes[0])
        return True

    assert host.close_project(first.binding, prepare_close=allow)
    assert host.sessions() == (second,)
    # Opaque windows are never closed/destroyed by this headless host.
    assert window is not None


def test_late_async_result_after_close_reopen_same_id_never_targets_new_session(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)
    old = host.reserve_module(first.binding.project_id, "film_design").route
    operation = host.capture_operation(first.binding.project_id, "optimize")
    assert host.close_project(first.binding, prepare_close=lambda binding, routes: True)
    fresh_session = host.open_project(first.binding.root)
    assert fresh_session.binding.generation != first.binding.generation
    fresh = host.reserve_module(first.binding.project_id, "film_design").route
    assert_error("stale_session", lambda: host.attach_window(old, object()))
    assert_error("stale_session", lambda: host.resolve_operation(operation))
    assert not host.release_window(old)
    assert host.routes(first.binding.project_id) == (fresh,)


def test_captured_operation_is_unaffected_by_later_focus_selection(tmp_path):
    host, first, second, stores = host_with_projects(tmp_path)
    foreground_project = first.binding.project_id
    operation = host.capture_operation(foreground_project, "import-one")
    foreground_project = second.binding.project_id
    host.reserve_module(foreground_project, "film_design")
    assert host.resolve_operation(operation) is stores[first.binding.root]
    assert operation.session.project_id == first.binding.project_id


def test_launcher_presentation_detach_has_no_registry_lifetime_effect(tmp_path):
    host, first, second, _ = host_with_projects(tmp_path)
    route = host.reserve_module(first.binding.project_id, "film_design").route
    # Presentation clients hold a host reference but do not own its state.
    launcher_presentation = {"host": host}
    launcher_presentation.clear()
    assert host.sessions() == (first, second)
    assert host.routes(first.binding.project_id) == (route,)


@pytest.mark.parametrize("module", ["settings", "about", "launcher", "unknown", None, []])
def test_application_scoped_and_unknown_modules_are_rejected(tmp_path, module):
    host, first, _, _ = host_with_projects(tmp_path)
    assert_error("invalid_module", lambda: host.reserve_module(first.binding.project_id, module))


def test_missing_project_and_empty_operation_do_not_infer_a_target(tmp_path):
    host, _, _, _ = host_with_projects(tmp_path)
    assert_error("project_closed", lambda: host.capture_operation("missing", "save"))
    assert_error("invalid_identity", lambda: host.capture_operation("", "save"))
    assert_error("invalid_identity", lambda: host.capture_operation("project-one", ""))


def test_bindings_are_immutable(tmp_path):
    host, first, _, _ = host_with_projects(tmp_path)
    with pytest.raises(FrozenInstanceError):
        first.binding.project_id = "project-two"


def test_loading_failure_and_store_root_mismatch_publish_nothing(tmp_path):
    def fail(root):
        raise ValueError("invalid manifest")
    host = ProjectSessionHost(fail)
    with pytest.raises(ValueError, match="invalid manifest"):
        host.open_project(tmp_path / "one")
    assert host.sessions() == ()
    host = ProjectSessionHost(lambda root: FakeStore(tmp_path / "elsewhere", "one"))
    assert_error("root_mismatch", lambda: host.open_project(tmp_path / "one"))
    assert host.sessions() == ()


def test_requested_link_path_is_not_hidden_from_store_validation(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    requested = []

    def validating_factory(root):
        requested.append(root)
        if root.is_symlink():
            raise ValueError("linked project root rejected")
        return FakeStore(root, "project-one")

    host = ProjectSessionHost(validating_factory)
    host.open_project(real)
    with pytest.raises(ValueError, match="linked project root rejected"):
        host.open_project(link)
    assert requested == [real, link]


def test_import_is_independent_of_qt_and_uncommitted_project_store():
    result = subprocess.run([sys.executable, "-c", (
        "import sys; import coat_sim.project_session; "
        "assert not any(name.startswith('PySide6') for name in sys.modules); "
        "assert 'coat_sim.core.project_store' not in sys.modules"
    )], capture_output=True, text=True, check=False,
        cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr


def test_actual_project_store_protocol_when_core_is_available(tmp_path):
    project_store = pytest.importorskip("coat_sim.core.project_store")
    first_store = project_store.ProjectStore.create(tmp_path / "one", "One")
    second_store = project_store.ProjectStore.create(tmp_path / "two", "Two")
    host = ProjectSessionHost(lambda root: project_store.ProjectStore.open(root / project_store.MANIFEST_FILENAME))
    first = host.open_project(first_store.root)
    second = host.open_project(second_store.root)
    assert first.binding.project_id == first_store.manifest["id"]
    assert second.binding.project_id == second_store.manifest["id"]
    assert project_store.ProjectStore.open(first_store.project_file).resolve_default() is None
    operation = host.capture_operation(first.binding.project_id, "preview")
    assert host.resolve_operation(operation).manifest["id"] == first_store.manifest["id"]
