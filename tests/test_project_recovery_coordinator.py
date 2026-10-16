"""An abnormal journal is restored only by explicit section review/approval."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QObject

from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.project_recovery_coordinator import ProjectRecoveryCoordinator
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class Adapter(ProjectRuntimeOwner):
    """Typed launcher double exercising the real recovery slot without windows."""

    def __init__(self, root):
        QObject.__init__(self)
        self.host = ProjectSessionHost(lambda root: ProjectStore.open(root / MANIFEST_FILENAME))
        self.session = self.host.open_project(root)
        self._leases = {self.session.binding.project_id: ProjectOwnerLease.acquire(
            root / MANIFEST_FILENAME, self.session.binding.project_id)}
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._stopped = False
        self._busy = False
        self.errors: list[str] = []
        self._error = ""

    def _set_error(self, message):
        self._error = str(message)
        self.errors.append(self._error)
        self.projectsChanged.emit()

    def close(self, clean=True):
        self._stopped = True
        self._executor.shutdown(wait=True)
        for lease in self._leases.values():
            lease.close(clean=clean)


def test_normal_open_keeps_default_until_explicit_film_recovery(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    store = ProjectStore.create(tmp_path / "project", "Recovery")
    original = Adapter(store.root)
    coordinator = ProjectRecoveryCoordinator(original)
    film = ProjectFilmWorkspace.for_project(original.host, original.session.binding)
    assert film.editor.set_design_value("name", "Recover this draft")
    project_id = original.session.binding.project_id
    checkpoint = coordinator.capture(project_id)
    original._leases[project_id].checkpoint(checkpoint)
    abnormal_generation = original._leases[project_id].generation
    coordinator.stop()
    original.close(clean=False)
    before = store.project_file.read_bytes() if store.project_file.exists() else None
    reopened = Adapter(store.root)
    recovery = ProjectRecoveryCoordinator(reopened)
    current = ProjectFilmWorkspace.for_project(reopened.host, reopened.session.binding)
    assert current.editor.snapshot().design["name"] != "Recover this draft"
    candidate = recovery.candidates(project_id)[0]
    assert candidate["generation"] == abnormal_generation
    assert candidate["sections"] == ["film"]
    assert recovery.review(project_id, candidate["generation"], "film", candidate["revision"])
    assert current.editor.snapshot().design["name"] != "Recover this draft"
    assert recovery.restore(project_id)
    assert current.editor.snapshot().design["name"] == "Recover this draft"
    assert current.editor.snapshot().dirty
    if before is not None:
        assert store.project_file.read_bytes() == before
    recovery.stop()
    reopened.close()


def test_candidate_list_selects_older_generation_and_archived_copy_restores_explicitly(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    store = ProjectStore.create(tmp_path / "project", "Recovery")

    first_owner = Adapter(store.root)
    first_recovery = ProjectRecoveryCoordinator(first_owner)
    project_id = first_owner.session.binding.project_id
    first_film = ProjectFilmWorkspace.for_project(first_owner.host, first_owner.session.binding)
    assert first_film.editor.set_design_value("name", "Older draft")
    first_owner._leases[project_id].checkpoint(first_recovery.capture(project_id))
    first_generation = first_owner._leases[project_id].generation
    first_recovery.stop()
    first_owner.close(clean=False)

    second_owner = Adapter(store.root)
    second_recovery = ProjectRecoveryCoordinator(second_owner)
    second_film = ProjectFilmWorkspace.for_project(second_owner.host, second_owner.session.binding)
    assert second_film.editor.set_design_value("name", "Newer draft")
    second_owner._leases[project_id].checkpoint(second_recovery.capture(project_id))
    second_generation = second_owner._leases[project_id].generation
    second_recovery.stop()
    second_owner.close(clean=False)

    current_owner = Adapter(store.root)
    current_recovery = ProjectRecoveryCoordinator(current_owner)
    current_film = ProjectFilmWorkspace.for_project(current_owner.host, current_owner.session.binding)
    current_name = current_film.editor.snapshot().design["name"]
    candidates = current_recovery.candidates(project_id)
    assert [row["generation"] for row in candidates] == [first_generation, second_generation]
    assert len(candidates) == 2
    assert [row["sections"] for row in candidates] == [["film"], ["film"]]
    older = candidates[0]
    assert current_recovery.review(project_id, older["generation"], "film", older["revision"])
    assert current_recovery.restore(project_id)
    assert current_film.editor.snapshot().design["name"] == "Older draft"

    current_recovery.archive_reviewed(project_id, older["generation"], "film", older["revision"])
    rows_after_archive = current_recovery.candidates(project_id)
    reviewed = next(row for row in rows_after_archive if row["reviewed"])
    assert reviewed["generation"] == older["generation"]
    assert reviewed["sections"] == ["film"]
    assert current_film.editor.snapshot().design["name"] == "Older draft"

    # The reviewed copy remains visible and requires its own explicit review/restore.
    assert current_recovery.review(project_id, reviewed["generation"], "film",
                                   reviewed["revision"], reviewed["recordId"])
    assert current_film.editor.snapshot().design["name"] == "Older draft"
    assert current_recovery.restore(project_id)
    assert current_film.editor.snapshot().design["name"] == "Older draft"
    assert current_film.editor.snapshot().design["name"] != current_name
    assert next(row for row in current_recovery.candidates(project_id)
                if row["reviewed"] and row["recordId"] == reviewed["recordId"])
    current_recovery.stop()
    current_owner.close()


@pytest.mark.parametrize("success", [True, False])
def test_optical_recovery_waits_for_actual_completion_and_keeps_candidate_on_failure(tmp_path, success):
    from copy import deepcopy
    from coat_sim.core.project_compute import ProjectComputeWorkspace
    from tests.test_project_optical_drafts import Provider
    QCoreApplication.instance() or QCoreApplication([])
    store = ProjectStore.create(tmp_path / "project", "Optical recovery")
    original = Adapter(store.root)
    old_recovery = ProjectRecoveryCoordinator(original)
    old_compute = ProjectComputeWorkspace.for_project(original.host, original.session.binding)
    old_provider = Provider(old_compute)
    old_compute.register_input_draft_provider("ray_trace", old_provider)
    project_id = original.session.binding.project_id
    original._leases[project_id].checkpoint(old_recovery.capture(project_id))
    generation = original._leases[project_id].generation
    old_recovery.stop()
    original.close(clean=False)

    current = Adapter(store.root)
    recovery = ProjectRecoveryCoordinator(current)
    current.recovery = recovery
    compute = ProjectComputeWorkspace.for_project(current.host, current.session.binding)
    provider = Provider(compute)
    provider.record["state"]["request_text"] = "prior current draft"
    prior = deepcopy(provider.record)
    compute.register_input_draft_provider("ray_trace", provider)
    candidate = next(row for row in recovery.candidates(project_id) if row["generation"] == generation)
    assert recovery.review(project_id, generation, "compute", candidate["revision"])
    assert not ProjectRuntimeOwner.restoreRecovery(current, project_id)
    assert "正在校验" in current._error and "已恢复" not in current._error
    assert not recovery.restore(project_id)  # Pending acceptance cannot dispatch twice.
    assert project_id in recovery._pending_compute_restores
    recovery.checkpoint()
    assert project_id in recovery._pending_compute_restores and not current.errors
    provider.finish_restore(success=success, error="asset changed" if not success else "")
    recovery.checkpoint()
    assert project_id not in recovery._pending_compute_restores
    if success:
        assert provider.record["document_id"] is None and provider.record["dirty"]
        assert provider.record["state"]["request_text"] != prior["state"]["request_text"]
        assert not current.errors
        assert "已恢复" in current._error
    else:
        assert provider.record == prior
        assert any("asset changed" in error for error in current.errors)
        assert any(row["generation"] == generation for row in recovery.candidates(project_id))
    recovery.stop()
    current.close()


def test_old_optical_settle_does_not_clear_new_review_or_overwrite_its_status(tmp_path):
    from coat_sim.core.project_compute import ProjectComputeWorkspace
    from tests.test_project_optical_drafts import Provider
    QCoreApplication.instance() or QCoreApplication([])
    store = ProjectStore.create(tmp_path / "project", "Recovery selection")
    original = Adapter(store.root)
    old = ProjectRecoveryCoordinator(original)
    first = ProjectComputeWorkspace.for_project(original.host, original.session.binding)
    first.register_input_draft_provider("ray_trace", Provider(first))
    project_id = original.session.binding.project_id
    original._leases[project_id].checkpoint(old.capture(project_id))
    generation = original._leases[project_id].generation
    old.stop()
    original.close(clean=False)

    current = Adapter(store.root)
    recovery = ProjectRecoveryCoordinator(current)
    compute = ProjectComputeWorkspace.for_project(current.host, current.session.binding)
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    candidate = recovery.candidates(project_id)[0]
    recovery.review(project_id, generation, "compute", candidate["revision"])
    assert not recovery.restore(project_id)
    provider.finish_restore()
    # A fresh review can be selected before the periodic completion display.
    recovery.review(project_id, generation, "compute", candidate["revision"])
    newer_review = recovery._review[project_id]
    current._error = "A newly selected recovery candidate"
    recovery.checkpoint()
    assert recovery._review[project_id] is newer_review
    assert current._error == "A newly selected recovery candidate"
    assert project_id not in recovery._pending_compute_restores
    recovery.stop()
    current.close()


def test_launcher_close_reopen_clears_completed_optical_restore_before_checkpoint(tmp_path, monkeypatch):
    from coat_sim.core.project_compute import ProjectComputeWorkspace
    from tests.test_launcher_project_adapter import completed
    from tests.test_project_optical_drafts import Provider
    QCoreApplication.instance() or QCoreApplication([])
    store = ProjectStore.create(tmp_path / "project", "Reopen recovery")
    original = Adapter(store.root)
    old = ProjectRecoveryCoordinator(original)
    first = ProjectComputeWorkspace.for_project(original.host, original.session.binding)
    first.register_input_draft_provider("ray_trace", Provider(first))
    project_id = original.session.binding.project_id
    original._leases[project_id].checkpoint(old.capture(project_id))
    generation = original._leases[project_id].generation
    old.stop()
    original.close(clean=False)

    picker = ProjectRuntimeOwner(default_loader=lambda _store: SimpleNamespace(state="empty_project"))
    monkeypatch.setattr(picker.desktop, "open_project", lambda root: picker.host.open_project(root))
    try:
        assert picker.openProject(str(store.project_file))
        completed(picker)
        session = picker.host.sessions()[0]
        compute = ProjectComputeWorkspace.for_project(picker.host, session.binding)
        provider = Provider(compute)
        compute.register_input_draft_provider("ray_trace", provider)
        candidate = picker.recovery.candidates(project_id)[0]
        picker.recovery.review(project_id, generation, "compute", candidate["revision"])
        assert not picker.restoreRecovery(project_id)
        provider.finish_restore()
        assert picker.recovery.restore_pending(project_id)
        assert not picker.closeProject(project_id)
        assert picker.confirmCloseProject(project_id)
        assert project_id not in picker.recovery._pending_compute_restores
        assert not picker.recovery.restore_pending(project_id)
        assert picker.openProject(str(store.project_file))
        completed(picker)
        assert picker.host.sessions()[0].binding.generation != session.binding.generation
        picker.recovery.checkpoint()
        assert project_id in picker.recovery.pending
        picker.recovery.pending[project_id].result(timeout=5)
        assert not picker.recovery.restore_pending(project_id)
        error_text = picker.property("projectError")
        assert isinstance(error_text, str)
        assert "token is no longer available" not in error_text
    finally:
        picker.stop()
        picker.deleteLater()
        QCoreApplication.processEvents()


def test_stale_status_is_generation_fenced_and_one_failure_does_not_starve_other_projects(tmp_path):
    from coat_sim.core.project_compute import ProjectComputeWorkspace
    QCoreApplication.instance() or QCoreApplication([])
    first = ProjectStore.create(tmp_path / "first", "First")
    adapter = Adapter(first.root)
    recovery = ProjectRecoveryCoordinator(adapter)
    project_id = adapter.session.binding.project_id
    ProjectComputeWorkspace.for_project(adapter.host, adapter.session.binding)
    review: tuple = ({}, {}, "compute", "old", 1, "")
    recovery._review[project_id] = review
    recovery._pending_compute_restores[project_id] = (review, "missing-token", "old-generation")
    assert not recovery.restore_pending(project_id)
    assert project_id not in recovery._review

    second = ProjectStore.create(tmp_path / "second", "Second")
    second_session = adapter.host.open_project(second.root)
    second_id = second_session.binding.project_id
    adapter._leases[second_id] = ProjectOwnerLease.acquire(second.project_file, second_id)
    film = ProjectFilmWorkspace.for_project(adapter.host, second_session.binding)
    assert film.editor.set_design_value("name", "Other project still checkpoints")
    recovery._review[project_id] = review
    recovery._pending_compute_restores[project_id] = (
        review, "missing-token", adapter.session.binding.generation)
    recovery.checkpoint()
    assert not recovery.restore_pending(project_id)
    assert any("token is no longer available" in error for error in adapter.errors)
    assert second_id in recovery.pending
    recovery.pending[second_id].result(timeout=5)
    recovery.stop()
    adapter.close()
