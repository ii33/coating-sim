"""Headless, weak notifications happen only after committed store publication."""
from __future__ import annotations

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import get_ident

import pytest

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore

pytestmark = pytest.mark.contract


def source(name="Saved"):
    return {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1., "name": name}


def test_commits_notify_only_exact_store_after_writer_lock_release(tmp_path):
    first, second = (ProjectStore.create(tmp_path / name, name) for name in ("First", "Second"))
    events = []

    def committed():
        assert not (first.root / ".project-write.lock").exists()
        events.append(first.manifest["revision"])

    unsubscribe = first.observe_changes(committed)
    second.create_document("source", source())
    assert events == []
    identity = first.create_document("source", source())
    assert events == [1] and first.snapshot_saved_inputs()["documents"][identity]["name"] == "Saved"
    first.update_information({"name": "Renamed"}, expected_revision=1)
    assert events == [1, 2]
    first.update_information({"name": "Renamed"}, expected_revision=2)
    assert events == [1, 2]  # No-op information updates are not commits.
    unsubscribe()
    first.create_document("source", source("After unsubscribe"))
    assert events == [1, 2]


def test_failed_save_emits_no_commit_notification(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "First", "First")
    events = []

    def committed():
        events.append(True)

    store.observe_changes(committed)
    monkeypatch.setattr(store, "_publish_transaction", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ProjectError("io_error", "injected failure before publication")))
    with pytest.raises(ProjectError):
        store.create_document("source", source())
    assert events == [] and store.manifest["revision"] == 0
    assert store.manifest["documents"] == []


def test_observer_exception_cannot_turn_committed_save_into_failure(tmp_path):
    store = ProjectStore.create(tmp_path / "First", "First")

    def broken():
        raise RuntimeError("closed presentation")

    store.observe_changes(broken)
    identity = store.create_document("source", source())
    assert store.snapshot_saved_inputs()["documents"][identity]["name"] == "Saved"
    assert store.manifest["revision"] == 1
    assert any(row["code"] == "change_observer_failed" for row in store.maintenance_warnings)


def test_bound_observers_do_not_keep_owner_alive(tmp_path):
    store = ProjectStore.create(tmp_path / "First", "First")

    class Owner:
        def committed(self):
            pytest.fail("collected observer must never run")

    owner = Owner()
    reference = weakref.ref(owner)
    unsubscribe = store.observe_changes(owner.committed)
    del owner
    gc.collect()
    assert reference() is None
    store.create_document("source", source())
    assert not store._change_observers
    unsubscribe()


def test_worker_commit_notifications_are_headless_and_run_after_publication(tmp_path):
    store = ProjectStore.create(tmp_path / "First", "First")
    events = []
    main = get_ident()

    def committed():
        assert not (store.root / ".project-write.lock").exists()
        events.append((get_ident(), store.manifest["revision"]))

    store.observe_changes(committed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        identity = pool.submit(store.create_document, "source", source()).result()
    assert len(events) == 1 and events[0][0] != main and events[0][1] == 1
    assert store.snapshot_saved_inputs()["documents"][identity]["name"] == "Saved"
