"""Synthetic deterministic regression for PL-1/2; no real project data."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event, Thread, current_thread

import pytest

from coat_sim.core.project_dependencies import json_bytes
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectError, ProjectStore

pytestmark = pytest.mark.contract


def source(name):
    return {"schema_version": 2, "kind": "point", "device_type": "custom",
            "source_exponent": 1., "name": name}


def consistent(snapshot):
    records = {row["id"]: row for row in snapshot["manifest"]["documents"]}
    assert set(records) == set(snapshot["documents"])
    for key, data in snapshot["documents"].items():
        assert records[key]["sha256"] == sha256(json_bytes(data)).hexdigest()
    assert set(snapshot.get("assets", {})) == {
        key for key, row in records.items() if row["kind"] == "binary_asset"}


def interleave(store, first, second, monkeypatch, *, capture_method="_read_operation_inputs"):
    """Pause A after capture and start B at the exact common-lock boundary."""
    captured, resume, attempted = Event(), Event(), Event()
    read = getattr(store, capture_method)
    lock = store._operation_lock
    first_thread: list[Thread] = []

    class ObservedLock:
        def __enter__(self):
            if first_thread and current_thread() is not first_thread[0]:
                attempted.set()
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    def paused_read():
        result = read()
        if current_thread() is first_thread[0] and not captured.is_set():
            captured.set()
            assert resume.wait(5)
        return result

    def run_first():
        first_thread.append(current_thread())
        return first()

    monkeypatch.setattr(store, "_operation_lock", ObservedLock())
    monkeypatch.setattr(store, capture_method, paused_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run_first)
        try:
            assert captured.wait(5)
            b = pool.submit(second)
            assert attempted.wait(5)
        finally:
            resume.set()
        return a.result(timeout=5), b.result(timeout=5)


@pytest.mark.parametrize("forked", [False, True])
def test_same_store_create_create_preserves_both_successes(tmp_path, monkeypatch, forked):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    a = ProjectResourceService(store)
    b = a.fork_editor()
    def first():
        return a.create_resource("source", source("A")) if forked else store.create_document("source", source("A"))
    def second():
        return b.create_resource("source", source("B")) if forked else store.create_document("source", source("B"))
    ids = interleave(store, first, second, monkeypatch)
    saved = ProjectStore.open(store.project_file).snapshot_saved_inputs()
    consistent(saved)
    assert set(ids) == set(saved["documents"])
    assert {doc["name"] for doc in saved["documents"].values()} == {"A", "B"}
    assert saved["manifest"]["revision"] == 2


def test_same_store_save_save_preserves_both_successes(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    a, b = [store.create_document("source", source(name)) for name in ("A", "B")]
    interleave(store, lambda: store.save_document(a, source("A edited")),
               lambda: store.save_document(b, source("B edited")), monkeypatch)
    saved = ProjectStore.open(store.project_file).snapshot_saved_inputs()
    consistent(saved)
    assert {doc["name"] for doc in saved["documents"].values()} == {"A edited", "B edited"}


@pytest.mark.parametrize("mutation", ["create", "delete", "edit"])
def test_snapshot_serializes_entire_capture(tmp_path, monkeypatch, mutation):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    identity = store.create_document("source", source("old"))
    def change():
        if mutation == "create":
            return store.create_document("source", source("new"))
        if mutation == "delete":
            return store.remove_document(identity)
        return store.save_document(identity, source("edited"))
    old, _ = interleave(store, store.snapshot_saved_inputs, change, monkeypatch, capture_method="_read_all")
    consistent(old)
    assert old["manifest"]["revision"] == 1
    assert old["documents"][identity]["name"] == "old"
    consistent(ProjectStore.open(store.project_file).snapshot_saved_inputs())


def test_prepared_records_reject_changed_original_generation(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    records, documents = store._read_all()
    added = store.create_document("source", source("new"))
    with pytest.raises(ProjectError) as error:
        store._publish(records, documents)
    assert error.value.code == "stale_project"
    assert added in store.snapshot_saved_inputs()["documents"]


def test_reentrant_snapshot_change_rejects_instead_of_mixing(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    store.create_document("source", source("old"))
    original = store._read_all
    entered = False
    def read():
        nonlocal entered
        result = original()
        if not entered:
            entered = True
            store.create_document("source", source("new"))
        return result
    monkeypatch.setattr(store, "_read_all", read)
    with pytest.raises(ProjectError) as error:
        store.snapshot_saved_inputs()
    assert error.value.code == "stale_project"


def test_observers_run_outside_store_lock(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    calls = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def callback():
            calls.append(pool.submit(lambda: store.manifest).result(timeout=5))
        unsubscribe = store.observe_changes(callback)
        store.create_document("source", source("new"))
        unsubscribe()
    assert len(calls) == 1
    assert calls[0]["revision"] == 1
    assert not store.maintenance_warnings


def test_snapshot_asset_metadata_and_payload_stay_in_one_generation(tmp_path, monkeypatch):
    from pathlib import Path
    from coat_sim.core.project_simulation_resources import capture_simulation_asset
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    source_file = tmp_path / "spectrum.csv"
    payload = b"wavelength_nm,value\n500,1\n600,2\n"
    source_file.write_bytes(payload)
    asset = capture_simulation_asset(ProjectResourceService(store), "spd_csv", source_file)
    identity = asset["document_id"]
    saved, _ = interleave(store, store.snapshot_saved_inputs,
                           lambda: store.remove_document(identity), monkeypatch, capture_method="_read_all")
    consistent(saved)
    assert saved["documents"][identity]["sha256"] == sha256(payload).hexdigest()
    assert Path(saved["assets"][identity]["path"]).read_bytes() == payload
    assert not ProjectStore.open(store.project_file).snapshot_saved_inputs()["documents"]


def test_snapshot_checks_external_descriptor_after_asset_work(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "synthetic")
    store.create_document("source", source("old"))
    original = store.asset_sources
    def assets(*args, **kwargs):
        result = original(*args, **kwargs)
        ProjectStore.open(store.project_file).create_document("source", source("external"))
        return result
    monkeypatch.setattr(store, "asset_sources", assets)
    with pytest.raises(ProjectError) as error:
        store.snapshot_saved_inputs()
    assert error.value.code == "stale_project"
