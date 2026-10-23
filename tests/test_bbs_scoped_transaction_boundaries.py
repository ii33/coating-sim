"""Scoped BBS publication preserves ordinary validation and atomic ownership."""
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from coat_sim.core import project_store
from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.core.project_bbs_attachments import import_attachment
from coat_sim.core.project_binary_assets import AssetReadLimits
from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_store import ProjectStore
from tests.design_test_fixtures import _RECTANGULAR_SOURCE
from tests.test_asset_bbs_read_boundaries import damage_source, hashes

pytestmark = pytest.mark.contract


def test_discussion_publish_rejects_concurrent_unrelated_manifest_entry(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Project")
    peer = ProjectStore.open(store.project_file)
    original_writer = store._writer
    concurrent_ids = []

    @contextmanager
    def interleaved_writer():
        concurrent_ids.append(peer.create_document("source", deepcopy(_RECTANGULAR_SOURCE)))
        with original_writer():
            yield

    monkeypatch.setattr(store, "_writer", interleaved_writer)
    with pytest.raises(ProjectError) as error:
        ProjectBbsService(store).create_post("journal", "Stale", "Must not replace peer publication")
    assert error.value.code == "stale_project"
    current = ProjectStore.open(store.project_file)
    assert [row["id"] for row in current.manifest["documents"]] == concurrent_ids
    assert [row["id"] for row in current.resolve_document(concurrent_ids[0])["documents"]] == concurrent_ids
    assert ProjectBbsService(current).list_posts()["posts"] == []
    assert not (store.root / ".project-write.lock").exists()


def test_discussion_capture_is_stale_after_same_store_unrelated_write(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    bbs = ProjectBbsService(store)
    bbs.create_post("journal", "Original", "Saved")
    records, data, _posts = bbs._load()
    ordinary = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    before = hashes(store)
    with pytest.raises(ProjectError) as error:
        store._publish(records, data, discussion_only=True)
    assert error.value.code == "stale_project"
    assert hashes(store) == before
    assert ordinary in {row["id"] for row in store.manifest["documents"]}


def test_local_publish_isolated_but_targeted_invalid_design_remains_rejected_after_bbs_save(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    source = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    store, bad_path = damage_source(store, source)
    ProjectBbsService(store).create_post("journal", "Independent", "Allowed BBS scope")
    bad_bytes = bad_path.read_bytes()
    # An independent current-schema write has no dependency on the bad source.
    created = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    assert created != source
    assert bad_path.read_bytes() == bad_bytes
    assert store.resolve_document(created)["root_document_id"] == created
    before = hashes(store)
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.save_document(source, json.loads(bad_bytes))
    assert hashes(store) == before
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(source)
    # Whole-project capture includes that document and still rejects it.
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.snapshot_saved_inputs()
    assert hashes(store) == before


@pytest.mark.parametrize("limit", ["documents", "json_bytes"])
def test_discussion_capacity_includes_retained_ordinary_documents(tmp_path, monkeypatch, limit):
    store = ProjectStore.create(tmp_path / "project", "Project")
    source = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    bbs = ProjectBbsService(store)
    bbs.create_post("journal", "Original", "Saved")
    records, data, _posts = bbs._load()
    before = hashes(store)
    if limit == "documents":
        monkeypatch.setattr(project_store, "MAX_PROJECT_DOCUMENTS", len(store.manifest["documents"]))
    else:
        scoped_size = sum(len(json_bytes(document)) for document in data.values())
        retained = next(row for row in store.manifest["documents"] if row["id"] == source)
        retained_size = (store.root / retained["path"]).stat().st_size
        monkeypatch.setattr(project_store, "MAX_PROJECT_BYTES", scoped_size + retained_size - 1)
    with pytest.raises(ProjectError) as error:
        if limit == "documents":
            bbs.create_post("journal", "Exceeds aggregate count", "Must remain unpublished")
        else:
            store._publish(records, data, discussion_only=True)
    assert error.value.code == "capacity_exceeded"
    assert hashes(store) == before


def test_attachment_budget_counts_unreferenced_retained_attachment(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"First owned payload")
    second.write_bytes(b"Second owned payload")
    import_attachment(store, first)
    store.asset_limits = AssetReadLimits(first.stat().st_size + second.stat().st_size - 1)
    before = hashes(store)
    with pytest.raises(ProjectError) as error:
        import_attachment(store, second)
    assert error.value.code == "capacity_exceeded"
    assert hashes(store) == before


def test_failed_discussion_manifest_replace_rolls_back_only_owned_publication(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Project")
    store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    before = hashes(store)
    manifest = deepcopy(store.manifest)

    def fail_replace(*_args, **_kwargs):
        raise OSError("Deterministic manifest replacement failure")

    monkeypatch.setattr(project_store, "replace_with_retry", fail_replace)
    with pytest.raises(ProjectError) as error:
        ProjectBbsService(store).create_post("journal", "Uncommitted", "Must roll back")
    assert error.value.code == "io_error"
    assert store.manifest == manifest
    assert hashes(store) == before
    assert not (store.root / ".project-write.lock").exists()


def test_discussion_rollback_preserves_concurrent_foreign_file(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Project")
    store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    before = store.project_file.read_bytes()
    foreign_files = []

    def concurrent_file_then_fail(*_args, **_kwargs):
        published = list(store.root.rglob("*.cbbs"))
        assert len(published) == 1
        foreign = published[0].parent / "concurrent-user-note.txt"
        foreign.write_text("Independent concurrent bytes", encoding="utf8")
        foreign_files.append(foreign)
        raise OSError("Deterministic failure after a foreign file appeared")

    monkeypatch.setattr(project_store, "replace_with_retry", concurrent_file_then_fail)
    with pytest.raises(ProjectError) as error:
        ProjectBbsService(store).create_post("journal", "Uncommitted", "Must retain foreign file")
    assert error.value.code == "io_error"
    assert store.project_file.read_bytes() == before
    assert foreign_files[0].read_text(encoding="utf8") == "Independent concurrent bytes"
    assert any(row["code"] == "publication_rollback_retained" for row in store.maintenance_warnings)
    assert not (store.root / ".project-write.lock").exists()
