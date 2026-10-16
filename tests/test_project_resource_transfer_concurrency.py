"""Deterministic concurrency contracts for selective project-resource transfer."""

from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_session import ProjectSessionError, ProjectSessionHost
from tests.test_project_core import file_snapshot, setup_project


pytestmark = pytest.mark.contract


def _run_in_thread(call, name):
    """Capture a worker outcome without letting exceptions disappear in a thread."""
    finished = Event()
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as error:  # The test thread asserts the exact outcome.
            outcome["error"] = error
        finally:
            finished.set()

    worker = Thread(target=run, name=name, daemon=True)
    worker.start()
    return worker, finished, outcome


def _join(worker, timeout=10):
    worker.join(timeout)
    assert not worker.is_alive(), f"{worker.name} remained blocked"


def _service_for_host(host, store):
    session = host.open_project(store.root)
    return session.binding, ProjectResourceService(
        store, session_host=host, binding=session.binding)


def _setup_project(path):
    path.mkdir(parents=True, exist_ok=True)
    return setup_project(path)


def test_slow_catalog_loader_does_not_hold_owner_and_cannot_publish_after_close(
        tmp_path, monkeypatch):
    owner_store, _external, _process_id, _default_id = _setup_project(tmp_path / "owner")
    source_store, _external, _source_process, _source_default = _setup_project(tmp_path / "source")
    host = ProjectSessionHost(lambda _root: owner_store)
    binding, service = _service_for_host(host, owner_store)

    loader_entered = Event()
    release_loader = Event()
    original_open = ProjectStore.open

    def slow_open(path, **kwargs):
        if Path(path) == source_store.project_file:
            loader_entered.set()
            if not release_loader.wait(10):
                raise AssertionError("test did not release the slow resource loader")
        return original_open(path, **kwargs)

    monkeypatch.setattr(ProjectStore, "open", slow_open)
    inspect_thread, inspect_done, inspect_outcome = _run_in_thread(
        lambda: service.inspect_resources(source_store.project_file), "slow-resource-inspect")
    list_thread = close_thread = None
    list_done = close_done = None
    list_outcome = close_outcome = {}
    close_allowed_while_loading = False
    list_allowed_while_loading = False
    try:
        assert loader_entered.wait(5), "catalog inspection did not enter its slow loader"
        list_thread, list_done, list_outcome = _run_in_thread(
            service.list_documents, "list-during-resource-inspect")
        list_allowed_while_loading = list_done.wait(3)
        close_thread, close_done, close_outcome = _run_in_thread(
            lambda: host.close_project(
                binding, prepare_close=lambda _binding, _routes: True),
            "close-during-resource-inspect")
        close_allowed_while_loading = close_done.wait(3)
    finally:
        release_loader.set()
        _join(inspect_thread)
        if list_thread is not None:
            _join(list_thread)
        if close_thread is not None:
            _join(close_thread)

    assert list_allowed_while_loading, "slow catalog I/O blocked same-owner list_documents"
    assert "error" not in list_outcome
    assert close_allowed_while_loading, "slow catalog I/O prevented its owner from closing"
    assert close_outcome.get("value") is True
    assert isinstance(inspect_outcome.get("error"), ProjectSessionError)
    assert inspect_outcome["error"].code == "project_closed"
    with service.resource_exchange._token_lock:
        assert service.resource_exchange._catalogs == {}


def test_same_plan_has_one_atomic_apply_reservation(tmp_path, monkeypatch):
    source_store, _external, process_id, _default_id = _setup_project(tmp_path / "source")
    target_store, _external, _target_process, _target_default = _setup_project(tmp_path / "target")
    service = ProjectResourceService(target_store)
    catalog = service.inspect_resources(source_store.project_file)
    preview = service.preview_resource_transfer(catalog["catalog_id"], [process_id])
    before_revision = target_store.manifest["revision"]

    merge_entered = Event()
    release_merge = Event()
    from coat_sim.core import project_resource_transfer as transfer

    original_merge = transfer._merge

    def blocked_merge(*args, **kwargs):
        merge_entered.set()
        if not release_merge.wait(10):
            raise AssertionError("test did not release the reserved transfer")
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(transfer, "_merge", blocked_merge)
    worker, finished, outcome = _run_in_thread(
        lambda: service.apply_resource_transfer(preview["plan_id"]), "first-transfer-apply")
    second_error = None
    try:
        assert merge_entered.wait(5), "first apply never reached merge preparation"
        try:
            service.apply_resource_transfer(preview["plan_id"])
        except ProjectError as apply_error:
            second_error = apply_error
    finally:
        release_merge.set()
        _join(worker)

    assert second_error is not None and second_error.code == "transfer_in_progress"
    assert "error" not in outcome
    assert target_store.manifest["revision"] == before_revision + 1
    with pytest.raises(ProjectError) as error:
        service.apply_resource_transfer(preview["plan_id"])
    assert error.value.code == "expired_plan"


def test_opposite_direction_transfers_on_one_host_do_not_deadlock_or_partially_write(
        tmp_path, monkeypatch):
    store_a, _external_a, process_a, _default_a = _setup_project(tmp_path / "project-a")
    store_b, _external_b, process_b, _default_b = _setup_project(tmp_path / "project-b")
    stores = {store_a.root: store_a, store_b.root: store_b}
    host = ProjectSessionHost(lambda root: stores[Path(root).resolve()])
    _binding_a, service_a = _service_for_host(host, store_a)
    _binding_b, service_b = _service_for_host(host, store_b)

    catalog_a = service_a.inspect_resources("project")
    catalog_b = service_b.inspect_resources("project")
    plan_a_to_b = service_a.preview_resource_transfer(
        catalog_a["catalog_id"], [process_a], target=store_b.project_file)
    plan_b_to_a = service_b.preview_resource_transfer(
        catalog_b["catalog_id"], [process_b], target=store_a.project_file)

    merge_barrier = Barrier(2)
    from coat_sim.core import project_resource_transfer as transfer

    original_merge = transfer._merge

    def synchronized_merge(*args, **kwargs):
        merge_barrier.wait(timeout=8)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(transfer, "_merge", synchronized_merge)
    thread_a, done_a, outcome_a = _run_in_thread(
        lambda: service_a.apply_resource_transfer(plan_a_to_b["plan_id"]), "transfer-a-to-b")
    thread_b, done_b, outcome_b = _run_in_thread(
        lambda: service_b.apply_resource_transfer(plan_b_to_a["plan_id"]), "transfer-b-to-a")
    try:
        assert done_a.wait(10), "A-to-B transfer deadlocked or exceeded its timeout"
        assert done_b.wait(10), "B-to-A transfer deadlocked or exceeded its timeout"
    finally:
        merge_barrier.abort()
        _join(thread_a)
        _join(thread_b)

    outcomes = (outcome_a, outcome_b)
    successful = [item["value"] for item in outcomes if "value" in item]
    errors = [item["error"] for item in outcomes if "error" in item]
    assert successful, "at least one direction should complete despite contention"
    assert all(isinstance(error, ProjectError) for error in errors)
    assert all(error.code in {"stale_source", "stale_target"} for error in errors)

    # Re-open and resolve every managed graph. A winning transfer must land as
    # a complete dependency closure; the losing direction cannot leave debris.
    refreshed_a = ProjectStore.open(store_a.project_file)
    refreshed_b = ProjectStore.open(store_b.project_file)
    for store in (refreshed_a, refreshed_b):
        for row in store.manifest["documents"]:
            assert store.resolve_document(row["id"])["documents"]
    for result in successful:
        target = refreshed_a if Path(result["target_path"]) == refreshed_a.project_file else refreshed_b
        transferred_root = result["id_map"].get(process_a)
        if transferred_root is None:
            transferred_root = result["id_map"][process_b]
        kinds = {row["kind"] for row in target.resolve_document(transferred_root)["documents"]}
        assert {"process", "machine", "substrate", "source", "mask"}.issubset(kinds)


def test_reserved_apply_makes_cancel_and_catalog_release_report_in_progress(
        tmp_path, monkeypatch):
    source_store, _external, process_id, _default_id = _setup_project(tmp_path / "source")
    target_store, _external, _target_process, _target_default = _setup_project(tmp_path / "target")
    service = ProjectResourceService(target_store)
    catalog = service.inspect_resources(source_store.project_file)
    preview = service.preview_resource_transfer(catalog["catalog_id"], [process_id])

    merge_entered = Event()
    release_merge = Event()
    from coat_sim.core import project_resource_transfer as transfer

    original_merge = transfer._merge

    def blocked_merge(*args, **kwargs):
        merge_entered.set()
        if not release_merge.wait(10):
            raise AssertionError("test did not release reserved apply")
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(transfer, "_merge", blocked_merge)
    worker, finished, outcome = _run_in_thread(
        lambda: service.apply_resource_transfer(preview["plan_id"]), "reserved-transfer-apply")
    cancel_error = release_error = None
    try:
        assert merge_entered.wait(5), "apply never established its reservation"
        try:
            service.cancel_resource_transfer(preview["plan_id"])
        except ProjectError as error:
            cancel_error = error
        try:
            service.release_resource_catalog(catalog["catalog_id"])
        except ProjectError as error:
            release_error = error
        with service.resource_exchange._token_lock:
            catalog_remained = catalog["catalog_id"] in service.resource_exchange._catalogs
    finally:
        release_merge.set()
        _join(worker)

    assert cancel_error is not None and cancel_error.code == "transfer_in_progress"
    assert release_error is not None and release_error.code == "transfer_in_progress"
    assert catalog_remained
    assert finished.is_set() and "error" not in outcome
    assert outcome["value"]["id_map"]


def test_cancel_winning_before_apply_expires_plan_without_writes(tmp_path):
    source_store, _external, process_id, _default_id = _setup_project(tmp_path / "source")
    target_store, _external, _target_process, _target_default = _setup_project(tmp_path / "target")
    service = ProjectResourceService(target_store)
    catalog = service.inspect_resources(source_store.project_file)
    preview = service.preview_resource_transfer(catalog["catalog_id"], [process_id])
    before = file_snapshot(target_store.root)

    service.cancel_resource_transfer(preview["plan_id"])

    with pytest.raises(ProjectError) as error:
        service.apply_resource_transfer(preview["plan_id"])
    assert error.value.code == "expired_plan"
    assert file_snapshot(target_store.root) == before

