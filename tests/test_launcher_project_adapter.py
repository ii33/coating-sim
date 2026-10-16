"""Project picker admission without a display or legacy module side effects."""

from pathlib import Path
import shutil
from threading import Event
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QGuiApplication

from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner


pytestmark = pytest.mark.contract


def completed(adapter, timeout=5):
    deadline = time.monotonic() + timeout
    while adapter.projectBusy and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert not adapter.projectBusy
    QCoreApplication.processEvents()


@pytest.fixture
def picker():
    QCoreApplication.instance() or QCoreApplication([])
    adapter = ProjectRuntimeOwner(
        default_loader=lambda store: SimpleNamespace(state="empty_project"))
    # The actual film presentation has separate real-QML tests. Here preserve
    # the actual session host and isolate asynchronous picker admission.
    def admit(root: str) -> Any:
        return adapter.host.open_project(root)

    adapter.desktop.open_project = admit  # type: ignore[method-assign,assignment]
    yield adapter
    adapter.stop()
    adapter.deleteLater()
    QCoreApplication.processEvents()


def test_create_open_select_are_project_scoped_and_repeated_open_reuses_owner(picker, tmp_path):
    first = tmp_path / "First"
    second = tmp_path / "Second"
    assert picker.createProject("First", str(first))
    assert not picker.openProject(str(second))
    assert picker.projectBusy
    completed(picker)
    assert picker.projectError == ""
    first_id = picker.selectedProjectId
    assert first_id
    assert picker.projectEntries == [dict(projectId=first_id, name="First",
                                          root=str(first), descriptorFile=str(first / MANIFEST_FILENAME), active=True,
                                          defaultState="empty_project",
                                          defaultFilmSnapshot=False,
                                          defaultFilmDraft=False)]
    assert picker.createProject("Second", str(second))
    completed(picker)
    second_id = picker.selectedProjectId
    assert second_id != first_id
    assert len(picker.projectEntries) == 2
    assert picker.openProject(str(first / MANIFEST_FILENAME))
    assert picker.selectedProjectId == first_id
    assert len(picker.host.sessions()) == 2
    assert picker.activateProject(second_id)
    assert picker.selectedProjectId == second_id
    assert picker.host.store_for(picker.host.sessions()[0].binding) is not picker.host.store_for(
        picker.host.sessions()[1].binding)


def test_errors_preserve_existing_owner_and_reject_copied_identity(picker, tmp_path):
    first = tmp_path / "First"
    assert picker.createProject("First", str(first))
    completed(picker)
    first_id = picker.selectedProjectId
    assert not picker.openProject(str(tmp_path / "missing.coatproj"))
    completed(picker)
    assert picker.projectError
    assert picker.selectedProjectId == first_id
    assert len(picker.host.sessions()) == 1
    clone = tmp_path / "Clone"
    shutil.copytree(first, clone)
    assert picker.openProject(str(clone / MANIFEST_FILENAME))
    completed(picker)
    assert "project ID" in picker.projectError
    assert picker.selectedProjectId == first_id
    assert len(picker.host.sessions()) == 1
    assert not picker.activateProject("nonexistent")
    assert len(picker.host.sessions()) == 1


def test_invalid_input_and_existing_destination_do_not_replace_project(picker, tmp_path):
    assert not picker.createProject("", str(tmp_path / "new"))
    assert not picker.openProject("")
    existing = tmp_path / "occupied"
    existing.mkdir()
    assert picker.createProject("Project", str(existing))
    completed(picker)
    assert picker.projectError
    assert not (existing / MANIFEST_FILENAME).exists()
    assert picker.projectEntries == []


def test_folder_dialog_file_url_preserves_escaped_directory(picker, tmp_path):
    root = tmp_path / "Study with spaces"
    url = QUrl.fromLocalFile(str(root)).toString()
    assert picker.createProject("Study", url)
    completed(picker)
    assert picker.projectError == ""
    assert (root / MANIFEST_FILENAME).is_file()
    assert cast(Any, picker.projectEntries)[0]["root"] == str(root)


def test_arbitrary_descriptor_name_and_normalized_file_url_reuse_one_owner(picker, tmp_path):
    store = ProjectStore.create(tmp_path / "Study # 100% 中国", "Study")
    descriptor = store.project_file.with_name("optics plan # 100%.coatproj")
    store.project_file.rename(descriptor)
    assert picker.openProject(QUrl.fromLocalFile(str(descriptor)).toString())
    completed(picker)
    assert picker.projectError == ""
    project_id = picker.selectedProjectId
    lease = picker._leases[project_id]
    bound = cast(ProjectStore, picker.host.store_for(picker.host.sessions()[0].binding))
    assert bound.project_file == descriptor
    assert picker.projectEntries[0]["descriptorFile"] == str(descriptor)
    assert picker.openProject(str(descriptor.parent / "." / descriptor.name))
    assert not picker.projectBusy
    assert picker.selectedProjectId == project_id
    assert picker._leases[project_id] is lease
    assert len(picker.host.sessions()) == 1
    assert not (store.root / MANIFEST_FILENAME).exists()


def test_wrong_file_and_alternate_same_root_descriptor_never_activate_owner(picker, tmp_path):
    store = ProjectStore.create(tmp_path / "Study", "Study")
    assert picker.openProject(str(store.project_file))
    completed(picker)
    project_id = picker.selectedProjectId
    for path in (store.root, store.root / "project.json", store.root / "bundle.cproject"):
        assert not picker.openProject(str(path))
        assert picker.projectError
        assert picker.selectedProjectId == project_id
        assert not picker.projectBusy
    alternate = store.root / "another.coatproj"
    shutil.copyfile(store.project_file, alternate)
    assert not picker.openProject(str(alternate))
    assert "另一个工程文件" in picker.projectError
    assert len(picker.host.sessions()) == 1
    assert picker.selectedProjectId == project_id


def test_invalid_descriptor_content_never_admits_an_owner(picker, tmp_path):
    descriptor = tmp_path / "invalid.coatproj"
    descriptor.write_text("not JSON", encoding="utf-8")
    assert picker.openProject(str(descriptor))
    completed(picker)
    assert picker.projectError
    assert picker.projectEntries == []


def test_linked_descriptor_cannot_use_existing_owner_fast_path(picker, tmp_path):
    store = ProjectStore.create(tmp_path / "Study", "Study")
    assert picker.openProject(str(store.project_file))
    completed(picker)
    alias = store.root / "alias.coatproj"
    try:
        alias.symlink_to(store.project_file)
    except OSError:
        pytest.skip("Symlinks are unavailable on this host")
    assert not picker.openProject(str(alias))
    assert picker.projectError
    assert len(picker.host.sessions()) == 1


def test_named_descriptor_refresh_recovery_and_package_export_keep_selected_file(picker, tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client

    opened_runtimes = []

    def open_runtime(project_file):
        store = ProjectStore.open(project_file)
        opened_runtimes.append(store)
        return store

    monkeypatch.setattr(project_runtime_client, "open_project_runtime", open_runtime)
    store = ProjectStore.create(tmp_path / "Study", "Study")
    descriptor = store.project_file.with_name("custom-study.coatproj")
    store.project_file.rename(descriptor)
    seen = []
    def load_default(selected):
        seen.append(selected.project_file)
        return SimpleNamespace(state="empty_project")

    picker._default_loader = load_default
    assert picker.openProject(str(descriptor))
    completed(picker)
    assert picker.projectError == ""
    assert seen == [descriptor]
    project_id = picker.selectedProjectId
    assert picker.recoveryCandidates(project_id) == []
    assert picker.projectError == ""
    package = tmp_path / "named.cproject"
    assert picker.exportProjectPackage(project_id, str(package))
    completed(picker)
    assert package.is_file() and "已导出" in picker.projectError
    restored = tmp_path / "Restored"
    assert picker.importProjectPackage(str(package), str(restored))
    completed(picker)
    assert "独立工程窗口打开" in picker.projectError
    assert (restored / MANIFEST_FILENAME).is_file()
    assert len(opened_runtimes) == 1
    assert opened_runtimes[0].manifest["id"] != project_id
    assert picker.selectedProjectId == project_id
    assert len(picker.host.sessions()) == 1
    assert not (store.root / MANIFEST_FILENAME).exists()


def test_standalone_project_film_entry_delegates_named_descriptor_to_runtime(tmp_path, monkeypatch):
    from coat_sim.ui import project_film_host, project_runtime

    store = ProjectStore.create(tmp_path / "Study", "Study")
    descriptor = store.project_file.with_name("custom-study.coatproj")
    store.project_file.rename(descriptor)
    opened = []

    def runtime_main(argv):
        assert argv[0] == "--project"
        path = Path(argv[1])
        if path != descriptor:
            raise ValueError("expected an explicit project descriptor")
        opened.append(ProjectStore.open(path).project_file)
        return 17

    monkeypatch.setattr(project_runtime, "main", runtime_main)
    assert project_film_host.main(["--project", str(descriptor)]) == 17
    assert opened == [descriptor]
    with pytest.raises(ValueError):
        project_film_host.main(["--project", str(store.root)])
    assert opened == [descriptor]


def test_stop_fences_late_open_result(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    store = ProjectStore.create(tmp_path / "Study", "Study")
    started = Event()
    release = Event()

    def slow_load(project_file):
        started.set()
        assert release.wait(5)
        return ProjectStore.open(project_file)

    adapter = ProjectRuntimeOwner(
        store_loader=slow_load,
        default_loader=lambda _store: SimpleNamespace(state="empty_project"))
    try:
        assert adapter.openProject(str(store.project_file))
        assert started.wait(5)
        adapter.stop()
        assert not adapter.projectBusy
        release.set()
        for _ in range(10):
            QCoreApplication.processEvents()
            time.sleep(0.01)
        assert adapter.host.sessions() == ()
    finally:
        release.set()
        adapter.stop()
        adapter.deleteLater()


def test_real_project_entry_opens_designer_and_reuses_same_window(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    adapter = ProjectRuntimeOwner(
        default_loader=lambda _store: SimpleNamespace(state="empty_project"))
    try:
        assert adapter.createProject("Live", str(tmp_path / "Live"))
        completed(adapter)
        assert not adapter.projectError
        project_id = str(adapter.selectedProjectId)
        routes = adapter.host.routes(project_id)
        assert len(routes) == 1 and routes[0].module_key == "film_design"
        window = adapter.host.window_for(routes[0])
        assert window is not None
        assert adapter.openProject(str(tmp_path / "Live" / MANIFEST_FILENAME))
        assert not adapter.projectBusy
        assert adapter.host.window_for(routes[0]) is window
        assert adapter.activateProject(project_id)
        assert adapter.host.window_for(routes[0]) is window
        assert len(adapter.desktop._views) == 1
        from coat_sim.core.project_film_session import ProjectFilmWorkspace

        first_session = adapter.host.sessions()[0]
        first_workspace = ProjectFilmWorkspace.for_project(adapter.host, first_session.binding)
        first_controller = adapter.desktop.controller_for(first_workspace, "film_design")
        assert adapter.createProject("Second", str(tmp_path / "Second"))
        completed(adapter)
        assert not adapter.projectError
        second_session = adapter.host.sessions()[1]
        second_workspace = ProjectFilmWorkspace.for_project(adapter.host, second_session.binding)
        second_controller = adapter.desktop.controller_for(second_workspace, "film_design")
        assert second_controller is not first_controller
        assert first_controller.setDesignValue("name", "First only")
        assert second_controller.currentDesign()["name"] != "First only"
        assert len(adapter.desktop._views) == 2
    finally:
        for _route, view in tuple(adapter.desktop._views.items()):
            view[0].setProperty("allowClose", True)
            view[0].close()
        adapter.stop()
        adapter.deleteLater()
        QCoreApplication.processEvents()


def test_configured_project_seeds_pathless_film_draft_without_disk_write(tmp_path):
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from tests.test_project_film_default_draft import file_snapshot, frozen_film_project

    QGuiApplication.instance() or QGuiApplication([])
    store, frozen = frozen_film_project(tmp_path / "Configured")
    before = file_snapshot(store.root)
    adapter = ProjectRuntimeOwner()
    try:
        assert adapter.openProject(str(store.project_file))
        completed(adapter)
        assert adapter.projectError == ""
        assert file_snapshot(store.root) == before
        session = adapter.host.sessions()[0]
        workspace = ProjectFilmWorkspace.for_project(adapter.host, session.binding)
        draft = workspace.editor.snapshot()
        assert draft.path is None and draft.dirty
        assert draft.design["id"] == frozen["design"]["id"]
        assert draft.design["workflow_settings"]["simulation"] == frozen["settings"]
        assert cast(Any, adapter.projectEntries)[0]["defaultFilmDraft"] is True
        controller = adapter.desktop.controller_for(workspace, "film_design")
        assert controller.currentDesign()["id"] == frozen["design"]["id"]
        assert adapter.activateProject(session.binding.project_id)
        assert workspace.editor.snapshot().design == draft.design
        assert file_snapshot(store.root) == before
    finally:
        for _route, view in tuple(adapter.desktop._views.items()):
            view[0].setProperty("allowClose", True)
            view[0].close()
        adapter.stop()
        adapter.deleteLater()
        QCoreApplication.processEvents()


def test_failed_frozen_draft_rolls_back_admission_and_releases_lease_uncleanly(tmp_path, monkeypatch):
    QGuiApplication.instance() or QGuiApplication([])
    store = ProjectStore.create(tmp_path / "Study", "Study")
    acquired = []
    closed = []
    original_acquire = ProjectOwnerLease.acquire
    original_close = ProjectOwnerLease.close

    def acquire(*args, **kwargs):
        lease = original_acquire(*args, **kwargs)
        acquired.append(lease)
        return lease

    def close(lease, *, clean=True):
        closed.append((lease, clean))
        return original_close(lease, clean=clean)

    monkeypatch.setattr(ProjectOwnerLease, "acquire", acquire)
    monkeypatch.setattr(ProjectOwnerLease, "close", close)
    adapter = ProjectRuntimeOwner(default_loader=lambda _store: SimpleNamespace(
        state="ready", film_design_snapshot={"invalid": True}))
    try:
        assert adapter.openProject(str(store.project_file))
        completed(adapter)
        assert adapter.projectError
        assert adapter.host.sessions() == ()
        assert adapter._leases == {}
        assert len(acquired) == 1
        assert closed == [(acquired[0], False)]
        assert acquired[0]._closed
        assert not adapter.desktop._views
        assert not adapter.activateProject("failed-project")
        assert adapter.projectError
        assert not adapter.desktop._views
        assert adapter.projectEntries == []
    finally:
        adapter.stop()
        adapter.deleteLater()
        QCoreApplication.processEvents()


def test_recovery_candidates_are_explicit_and_include_generation_time_and_sections(picker, tmp_path):
    store = ProjectStore.create(tmp_path / "Study", "Study")
    first = ProjectRuntimeOwner(
        default_loader=lambda _store: SimpleNamespace(state="empty_project"))

    def admit_first(root: Path | str) -> ProjectFilmWorkspace:
        session = first.host.open_project(root)
        return ProjectFilmWorkspace.for_project(first.host, session.binding)

    first.desktop.open_project = admit_first  # type: ignore[method-assign]
    try:
        assert first.openProject(str(store.project_file))
        completed(first)
        sessions = first.host.sessions()
        assert sessions
        project_id = sessions[0].binding.project_id
        checkpoint = {
            "kind": "coat_sim.project_recovery_checkpoint", "version": 1,
            "project_id": project_id, "film": {"draft": "recovered"},
            "resources": {"draft": "resources"},
        }
        first._leases[project_id].checkpoint(checkpoint)
        first._leases[project_id].close(clean=False)
    finally:
        first.stop()
        first.deleteLater()

    assert picker.openProject(str(store.project_file))
    completed(picker)
    sessions = picker.host.sessions()
    assert sessions
    project_id = sessions[0].binding.project_id
    rows = picker.recoveryCandidates(project_id)
    assert len(rows) == 1
    assert rows[0]["generation"]
    assert rows[0]["revision"] == store.manifest["revision"]
    assert rows[0]["updatedAt"].endswith("+00:00")
    assert rows[0]["sections"] == ["film", "resources"]
    assert rows[0]["stale"] is False
    assert rows[0]["reviewed"] is False


def test_registration_gate_blocks_new_owner_but_preserves_existing_activation(picker, tmp_path):
    first = tmp_path / "First"
    assert picker.createProject("First", str(first))
    completed(picker)
    picker.set_admission_guard(lambda: (False, "Connecting"))
    assert picker.openProject(str(first / MANIFEST_FILENAME))
    assert not picker.createProject("Second", str(tmp_path / "Second"))
    assert picker.projectError == "Connecting"
    assert not (tmp_path / "Second").exists()
    assert len(picker.host.sessions()) == 1


def test_registration_gate_is_rechecked_after_store_io(picker, tmp_path):
    root = tmp_path / "First"
    assert picker.createProject("First", str(root))
    picker.set_admission_guard(lambda: (False, "Registration expired"))
    completed(picker)
    assert picker.projectError == "Registration expired"
    assert not picker.host.sessions()
    # The newly created file remains available, but no writable owner is admitted.
    assert ProjectStore.open(root / MANIFEST_FILENAME).manifest["name"] == "First"


def test_project_package_import_export_uses_async_worker_and_new_identity(picker, tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client

    opened_runtimes = []

    def open_runtime(project_file):
        store = ProjectStore.open(project_file)
        opened_runtimes.append(store)
        return store

    monkeypatch.setattr(project_runtime_client, "open_project_runtime", open_runtime)
    original = tmp_path / "Original"
    assert picker.createProject("Original", str(original))
    completed(picker)
    original_id = picker.selectedProjectId
    package = tmp_path / "portable.cproject"
    assert picker.exportProjectPackage(original_id, str(package))
    assert picker.projectBusy
    completed(picker)
    assert package.exists()
    assert "已导出" in picker.projectError
    restored = tmp_path / "Restored"
    assert picker.importProjectPackage(str(package), str(restored))
    completed(picker)
    assert "独立工程窗口打开" in picker.projectError
    assert len(opened_runtimes) == 1
    assert opened_runtimes[0].manifest["id"] != original_id
    assert picker.selectedProjectId == original_id
    assert len(picker.projectEntries) == 1
    assert ProjectStore.open(original / MANIFEST_FILENAME).manifest["id"] == original_id


def test_project_package_admission_guard_rejects_without_io(picker, tmp_path):
    picker.set_admission_guard(lambda: (False, "frozen"))
    assert not picker.importProjectPackage(str(tmp_path / "missing.cproject"), str(tmp_path / "new"))
    assert picker.projectError == "frozen"
    assert not picker.projectBusy
    assert not (tmp_path / "new").exists()


def test_project_package_dirty_preflight_prevents_silent_saved_only_export(picker, tmp_path, monkeypatch):
    assert picker.createProject("Dirty", str(tmp_path / "source"))
    completed(picker)
    monkeypatch.setattr(picker.project_closer, "_preflight", lambda *args, **kwargs: (True, "dirty"))
    destination = tmp_path / "backup.cproject"
    assert not picker.exportProjectPackage(picker.selectedProjectId, str(destination))
    assert "先保存" in picker.projectError
    assert not destination.exists()
    assert not picker.projectBusy
