"""Explicit abnormal-session recovery contracts for project owner leases."""
from __future__ import annotations

import json
import shutil
from unittest.mock import patch
from uuid import uuid4

import pytest

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
import coat_sim.project_recovery as recovery
from tests.test_project_core import setup_project


pytestmark = pytest.mark.contract


def test_normal_lease_acquire_and_clean_close_do_not_read_prior_journal(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    owner_runtime = tmp_path / "owner-runtime"
    writer = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="abnormal",
                                       owner_runtime_root=owner_runtime)
    writer.checkpoint({"active_document_id": "doc-1", "scroll": 5})
    writer.close(clean=False)
    journal = store.root / ".project-session.json"
    before = journal.read_bytes()

    with patch.object(recovery, "_read_journal", side_effect=AssertionError("ordinary acquire read journal")):
        ordinary = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="ordinary",
                                             owner_runtime_root=owner_runtime)
        ordinary.close(clean=True)

    # A normal open/close must not consume, rewrite, or clear the abnormal record.
    assert journal.read_bytes() == before
    with patch.object(recovery, "_read_journal", wraps=recovery._read_journal) as read_journal:
        reviewer = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="review",
                                             owner_runtime_root=owner_runtime)
        candidate = reviewer.recovery_candidate()
        reviewer.close(clean=False)
    read_journal.assert_called_once_with(journal)
    assert candidate is not None
    assert candidate["generation"] == "abnormal"
    assert candidate["checkpoint"] == {"active_document_id": "doc-1", "scroll": 5}
    assert journal.read_bytes() == before


def test_explicit_checkpoint_is_finite_and_clean_close_suppresses_recovery(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    owner_runtime = tmp_path / "owner-runtime"
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="gui-1",
                                      owner_runtime_root=owner_runtime)

    with pytest.raises(ProjectError) as caught:
        lease.checkpoint({"not_json": float("inf")})
    assert caught.value.code == "invalid_recovery"
    lease.checkpoint({"document_id": "film-1", "active": True})
    lease.close(clean=True)

    journal_path = store.root / ".project-session.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert {key: value for key, value in journal.items() if key != "updated_at"} == {
        "kind": "coat_sim.session_journal", "version": 1,
        "project_id": store.manifest["id"], "revision": store.manifest["revision"],
        "generation": "gui-1", "clean": True, "checkpoint": {},
    }
    assert isinstance(journal["updated_at"], str) and journal["updated_at"].endswith("+00:00")

    next_owner = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="next",
                                           owner_runtime_root=owner_runtime)
    assert next_owner.recovery_candidate() is None
    next_owner.close(clean=False)


def test_clean_unrecorded_owner_releases_os_lease_without_journal(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Quick open and close")
    owner_runtime = tmp_path / "owner-runtime"
    journal_path = store.root / ".project-session.json"
    first = ProjectOwnerLease.acquire(
        store.project_file,
        store.manifest["id"],
        generation="unrecorded-first",
        owner_runtime_root=owner_runtime,
    )
    assert first.project_id == store.manifest["id"]
    assert first.root == store.root
    assert first._recording is False
    assert not journal_path.exists()

    second = None
    try:
        with pytest.raises(ProjectError) as caught:
            ProjectOwnerLease.acquire(
                store.project_file,
                store.manifest["id"],
                generation="blocked-contender",
                owner_runtime_root=owner_runtime,
            )
        assert caught.value.code == "active_project_owner"
        assert not journal_path.exists()

        with patch.object(first, "checkpoint", side_effect=AssertionError("clean close checkpointed")), \
                patch.object(first, "_write", side_effect=AssertionError("clean close wrote a journal")):
            first.close(clean=True)
        assert first._closed is True
        assert not journal_path.exists()

        second = ProjectOwnerLease.acquire(
            store.project_file,
            store.manifest["id"],
            generation="after-clean-close",
            owner_runtime_root=owner_runtime,
        )
        assert second.root == first.root
        assert second.project_id == first.project_id
        assert second.generation != first.generation
        assert second._recording is False
        assert not journal_path.exists()

        with patch.object(second, "checkpoint", side_effect=AssertionError("clean close checkpointed")), \
                patch.object(second, "_write", side_effect=AssertionError("clean close wrote a journal")):
            second.close(clean=True)
        assert second._closed is True
        assert not journal_path.exists()
    finally:
        if second is not None and not second._closed:
            second.close(clean=False)
        if not first._closed:
            first.close(clean=False)


def test_stale_abnormal_checkpoint_is_rejected_without_overwriting_it(tmp_path):
    store, _, _, _ = setup_project(tmp_path, "Project")
    owner_runtime = tmp_path / "owner-runtime"
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="crashed",
                                      owner_runtime_root=owner_runtime)
    lease.checkpoint({"selected": "old-revision"})
    lease.close(clean=False)
    journal_path = store.root / ".project-session.json"
    prior = journal_path.read_bytes()

    # Commit a real project revision after the simulated abnormal close.
    current = ProjectStore.open(store.project_file)
    current.set_default_configuration(None)
    assert ProjectStore.open(store.project_file).manifest["revision"] == store.manifest["revision"] + 1

    reviewer = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="review",
                                         owner_runtime_root=owner_runtime)
    try:
        with pytest.raises(ProjectError) as caught:
            reviewer.recovery_candidate()
        assert caught.value.code == "stale_recovery"
    finally:
        reviewer.close(clean=False)
    assert journal_path.read_bytes() == prior


def test_copied_root_with_same_persisted_id_cannot_acquire_second_owner(tmp_path):
    original = ProjectStore.create(tmp_path / "original", "Original")
    copied_root = tmp_path / "copy"
    shutil.copytree(original.root, copied_root)
    owner_runtime = tmp_path / "owner-runtime"
    original_before = _snapshot(original.root)
    copy_before = _snapshot(copied_root)

    first = ProjectOwnerLease.acquire(original.project_file, original.manifest["id"],
                                      owner_runtime_root=owner_runtime)
    try:
        with pytest.raises(ProjectError) as caught:
            ProjectOwnerLease.acquire(copied_root / original.project_file.name, original.manifest["id"],
                                      owner_runtime_root=owner_runtime)
        assert caught.value.code == "active_project_owner"
        assert _snapshot(original.root) == original_before
        assert _snapshot(copied_root) == copy_before
    finally:
        first.close(clean=False)


def test_changed_project_id_at_same_canonical_root_still_conflicts(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    owner_runtime = tmp_path / "owner-runtime"
    first = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"],
                                      owner_runtime_root=owner_runtime)
    try:
        reidentified = store.manifest
        reidentified["id"] = uuid4().hex
        store.project_file.write_text(
            json.dumps(reidentified, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        changed = ProjectStore.open(store.project_file)
        before_attempt = _snapshot(store.root)
        assert changed.manifest["id"] != store.manifest["id"]

        with pytest.raises(ProjectError) as caught:
            ProjectOwnerLease.acquire(store.project_file.resolve(), changed.manifest["id"],
                                      owner_runtime_root=owner_runtime)
        assert caught.value.code == "active_project_owner"
        assert _snapshot(store.root) == before_attempt
    finally:
        first.close(clean=False)


def test_custom_project_descriptor_survives_recovery_and_ownership_reopen(tmp_path):
    created = ProjectStore.create(tmp_path / "project", "Custom descriptor")
    root = created.root
    project_file = created.project_file.with_name("research-project.coatproj")
    created.project_file.rename(project_file)
    owner_runtime = tmp_path / "owner-runtime"
    project_id = created.manifest["id"]

    opened = ProjectStore.open(project_file)
    assert opened.root == root
    assert opened.project_file == project_file
    assert not (root / "project.coatproj").exists()

    crashed = ProjectOwnerLease.acquire(opened.project_file, project_id,
                                        generation="custom-descriptor", owner_runtime_root=owner_runtime)
    crashed.checkpoint({"draft": "custom manifest recovery"})
    crashed.close(clean=False)

    recovered_store = ProjectStore.open(project_file)
    assert recovered_store.project_file == project_file
    reviewer = ProjectOwnerLease.acquire(recovered_store.project_file, project_id,
                                         generation="custom-review", owner_runtime_root=owner_runtime)
    assert reviewer.recovery_candidate()["checkpoint"] == {"draft": "custom manifest recovery"}

    alias = root / "alias.coatproj"
    alias.write_bytes(project_file.read_bytes())
    with pytest.raises(ProjectError) as caught:
        ProjectOwnerLease.acquire(alias, project_id, generation="same-root-alias",
                                  owner_runtime_root=owner_runtime)
    assert caught.value.code == "active_project_owner"

    reviewer.close(clean=True)
    clean_reopen = ProjectStore.open(project_file)
    assert clean_reopen.project_file == project_file
    clean_owner = ProjectOwnerLease.acquire(clean_reopen.project_file, project_id,
                                            generation="custom-clean-reopen", owner_runtime_root=owner_runtime)
    # Clean close keeps the reviewed archive available under the same root.
    assert clean_owner.recovery_candidate()["generation"] == "custom-descriptor"
    clean_owner.close(clean=True)
    assert not (root / "project.coatproj").exists()


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_second_crash_and_clean_close_preserve_unreviewed_older_recovery(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Recovery")
    runtime = tmp_path / "owners"
    first = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="first", owner_runtime_root=runtime)
    first.checkpoint({"draft": "first crash"})
    first.close(clean=False)
    second = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="second", owner_runtime_root=runtime)
    second.checkpoint({"draft": "second crash"})
    second.close(clean=False)
    third = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="third", owner_runtime_root=runtime)
    assert [row["generation"] for row in third.recovery_candidates()] == ["first", "second"]
    assert third.recovery_candidate()["checkpoint"] == {"draft": "second crash"}
    third.checkpoint({"draft": "third live draft"})
    assert third.discard_recovery("first") is True
    first_reviewed = next(row for row in third.reviewed_recovery_candidates()
                          if row["generation"] == "first")
    assert first_reviewed["checkpoint"] == {"draft": "first crash"}
    assert json.loads((store.root / ".project-session.json").read_text())["checkpoint"] == {"draft": "third live draft"}
    third.close(clean=True)
    fourth = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="fourth", owner_runtime_root=runtime)
    assert fourth.recovery_candidate()["generation"] == "second"
    assert fourth.discard_recovery("second") is True
    assert fourth.recovery_candidate() is None
    fourth.close(clean=False)


def test_archive_capacity_and_invalid_new_checkpoint_preserve_all_bytes(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Bounded")
    runtime = tmp_path / "owners"
    monkeypatch.setattr(recovery, "MAX_RECOVERY_GENERATIONS", 1)
    first = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="first", owner_runtime_root=runtime)
    first.checkpoint({"draft": "first"})
    first.close(clean=False)
    second = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="second", owner_runtime_root=runtime)
    before = _snapshot(store.root)
    with pytest.raises(ProjectError) as invalid:
        second.checkpoint({"invalid": float("nan")})
    assert invalid.value.code == "invalid_recovery"
    assert _snapshot(store.root) == before
    second.checkpoint({"draft": "second"})
    second.close(clean=False)
    third = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="third", owner_runtime_root=runtime)
    before = _snapshot(store.root)
    with pytest.raises(ProjectError) as full:
        third.checkpoint({"draft": "third"})
    assert full.value.code == "recovery_archive_full"
    assert _snapshot(store.root) == before
    assert [row["generation"] for row in third.recovery_candidates()] == ["first", "second"]
    third.close(clean=False)


def test_discard_serializes_with_inflight_checkpoint_without_resurrecting_candidate(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    store = ProjectStore.create(tmp_path / "project", "Serialized")
    runtime = tmp_path / "owners"
    first = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="first", owner_runtime_root=runtime)
    first.checkpoint({"draft": "first"})
    first.close(clean=False)
    second = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="second", owner_runtime_root=runtime)
    publishing, release, discarding, discarded = Event(), Event(), Event(), Event()
    write_archive = second._write_archive

    def paused_archive(records):
        publishing.set()
        assert release.wait(3)
        write_archive(records)
    monkeypatch.setattr(second, "_write_archive", paused_archive)

    def discard():
        discarding.set()
        result = second.discard_recovery("first")
        discarded.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        checkpoint = pool.submit(second.checkpoint, {"draft": "second"})
        assert publishing.wait(3)
        removal = pool.submit(discard)
        assert discarding.wait(3)
        assert not discarded.is_set()
        release.set()
        checkpoint.result(timeout=3)
        assert removal.result(timeout=3) is True
    assert second.recovery_candidates() == []
    assert json.loads((store.root / ".project-session.json").read_text())["checkpoint"] == {"draft": "second"}
    second.close(clean=False)


def test_reviewed_archive_moves_only_selected_section_and_keeps_siblings(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Reviewed")
    runtime = tmp_path / "owners"
    checkpoint = {
        "kind": "coat_sim.project_recovery_checkpoint", "version": 1,
        "project_id": store.manifest["id"],
        "film": {"draft": "film"}, "resources": {"draft": "resource"},
        "compute": {"draft": "compute"},
    }
    first = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="first",
                                      owner_runtime_root=runtime)
    first.checkpoint(checkpoint)
    first.close(clean=False)

    reviewer = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="reviewer",
                                         owner_runtime_root=runtime)
    try:
        candidate = reviewer.recovery_candidate()
        assert candidate is not None
        archived = reviewer.preserve_reviewed_recovery(
            "first", "resources", expected_revision=store.manifest["revision"])
        assert archived["generation"] == "first"
        remaining = reviewer.recovery_candidate()
        assert remaining is not None
        assert remaining["generation"] == "first"
        # The source no longer includes the selected section; its siblings remain.
        assert "resources" not in remaining["checkpoint"]
        assert remaining["checkpoint"]["film"] == checkpoint["film"]
        assert remaining["checkpoint"]["compute"] == checkpoint["compute"]
        reviewed = reviewer.reviewed_recovery_candidates()
        assert len(reviewed) == 1
        assert reviewed[0]["section"] == "resources"
        assert reviewed[0]["checkpoint"]["resources"] == checkpoint["resources"]
    finally:
        reviewer.close(clean=False)


def test_reviewed_archive_failure_preserves_source_and_generation_guards(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Reviewed")
    runtime = tmp_path / "owners"
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="first",
                                      owner_runtime_root=runtime)
    lease.checkpoint({
        "kind": "coat_sim.project_recovery_checkpoint", "version": 1,
        "project_id": store.manifest["id"], "film": {"draft": "keep"},
    })
    lease.close(clean=False)
    reviewer = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="reviewer",
                                         owner_runtime_root=runtime)
    before = _snapshot(store.root)
    try:
        with pytest.raises(ProjectError) as stale:
            reviewer.preserve_reviewed_recovery("wrong-generation", "film",
                                                expected_revision=store.manifest["revision"])
        assert stale.value.code == "stale_recovery"
        with pytest.raises(ProjectError) as wrong_revision:
            reviewer.preserve_reviewed_recovery("first", "film",
                                                expected_revision=store.manifest["revision"] + 1)
        assert wrong_revision.value.code == "stale_recovery"

        def archive_full(_records):
            raise ProjectError("reviewed_recovery_archive_full", "Reviewed archive is full")
        monkeypatch.setattr(reviewer, "_write_reviewed_archive", archive_full)
        with pytest.raises(ProjectError) as full:
            reviewer.preserve_reviewed_recovery("first", "film",
                                                expected_revision=store.manifest["revision"])
        assert full.value.code == "reviewed_recovery_archive_full"
        assert _snapshot(store.root) == before
        with pytest.raises(ProjectError) as full_compat:
            reviewer.discard_recovery("first")
        assert full_compat.value.code == "reviewed_recovery_archive_full"
        assert _snapshot(store.root) == before
    finally:
        reviewer.close(clean=False)


def test_archiving_prior_archive_generation_preserves_live_draft_and_other_generations(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Reviewed archive")
    runtime = tmp_path / "owners"

    def checkpoint(label):
        return {
            "kind": "coat_sim.project_recovery_checkpoint", "version": 1,
            "project_id": store.manifest["id"],
            "film": {"draft": f"film-{label}"},
            "resources": {"draft": f"resources-{label}"},
        }

    first = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="first",
                                      owner_runtime_root=runtime)
    first.checkpoint(checkpoint("first"))
    first.close(clean=False)
    second = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="second",
                                       owner_runtime_root=runtime)
    second.checkpoint(checkpoint("second"))
    second.close(clean=False)

    reviewer = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="reviewer",
                                         owner_runtime_root=runtime)
    try:
        live_before = json.loads((store.root / ".project-session.json").read_text(encoding="utf-8"))
        result = reviewer.preserve_reviewed_recovery(
            "first", "resources", expected_revision=store.manifest["revision"])
        live_after = json.loads((store.root / ".project-session.json").read_text(encoding="utf-8"))
        assert live_after == live_before
        rows = reviewer.recovery_candidates()
        assert [row["generation"] for row in rows] == ["first", "second"]
        assert rows[0]["checkpoint"]["film"] == {"draft": "film-first"}
        assert "resources" not in rows[0]["checkpoint"]
        assert rows[1]["checkpoint"] == checkpoint("second")
        archived = next(row for row in reviewer.reviewed_recovery_candidates()
                        if row["record_id"] == result["record_id"])
        assert archived["checkpoint"]["resources"] == {"draft": "resources-first"}
    finally:
        reviewer.close(clean=False)
