"""Only temporary, self-owned projects and an explicit trash stub are changed."""
from __future__ import annotations

from pathlib import Path
from threading import Event
import time
from typing import Any, cast

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.ui.launcher_project_files import copy_saved_project, export_saved_project, trash_project
from coat_sim.ui.launcher_projects import ProjectLauncherAdapter
from coat_sim.ui.recent_projects import RecentProjects

pytestmark = [pytest.mark.contract, pytest.mark.qt]


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("coat_sim.project_recovery.project_owner_runtime_root", lambda: tmp_path / "owners")
    return ProjectStore.create(tmp_path / "Study", "Study", summary="Summary", notes="Notes")


def test_copy_and_export_reuse_saved_input_package_and_refuse_overwrite(project, tmp_path):
    descriptor, identity = str(project.project_file), project.manifest["id"]
    (project.root / "results" / "private-run.txt").write_text("not an input")
    exported = export_saved_project(descriptor, identity, str(tmp_path / "study.cproject"))
    assert Path(exported["destination"]).is_file()
    copied = copy_saved_project(descriptor, identity, str(tmp_path / "Copied"))
    store = ProjectStore.open(copied["descriptorFile"])
    assert store.manifest["id"] != identity
    assert store.manifest["summary"] == "Summary"
    assert not (store.root / "results" / "private-run.txt").exists()
    for action, target in ((export_saved_project, tmp_path / "study.cproject"),
                           (copy_saved_project, tmp_path / "Copied")):
        with pytest.raises(ProjectError, match="destination|Destination|existing|exists"):
            action(descriptor, identity, str(target))
    assert project.project_file.is_file()


@pytest.mark.parametrize("action", ["copy", "export", "trash"])
def test_every_file_action_refuses_an_active_owner_without_changing_files(project, tmp_path, action):
    calls = []
    with ProjectOwnerLease.acquire(project.project_file, project.manifest["id"]):
        with pytest.raises(ProjectError) as caught:
            if action == "trash":
                trash_project(str(project.project_file), project.manifest["id"], move_to_trash=lambda path: calls.append(path))
            else:
                function = copy_saved_project if action == "copy" else export_saved_project
                function(str(project.project_file), project.manifest["id"], str(tmp_path / "new.cproject"))
        assert caught.value.code == "active_project_owner"
    assert not calls and project.project_file.is_file()


def test_trash_moves_the_whole_standard_directory_while_holding_lease(project, tmp_path):
    trash = tmp_path / "controlled-trash"
    def mover(path):
        with pytest.raises(ProjectError) as caught:
            ProjectOwnerLease.acquire(project.project_file, project.manifest["id"])
        assert caught.value.code == "active_project_owner"
        Path(path).rename(trash)
        return True, str(trash)
    result = trash_project(str(project.project_file), project.manifest["id"], move_to_trash=mover)
    assert result["trashPath"] == str(trash)
    assert not project.root.exists() and (trash / project.project_file.name).is_file()


def test_trash_failure_keeps_source_and_never_falls_back_to_permanent_delete(project):
    with pytest.raises(ProjectError) as caught:
        trash_project(str(project.project_file), project.manifest["id"], move_to_trash=lambda _path: (False, ""))
    assert caught.value.code == "trash_failed"
    assert project.project_file.is_file()


@pytest.mark.parametrize("unsafe", ["extra_file", "extra_directory", "nested_project", "symlink", "missing_layout", "home"])
def test_ambiguous_or_dangerous_directories_never_reach_the_trash_mover(project, tmp_path, monkeypatch, unsafe):
    if unsafe == "extra_file":
        (project.root / "unrelated.txt").write_text("keep")
    elif unsafe == "extra_directory":
        (project.root / "other-work").mkdir()
    elif unsafe == "nested_project":
        ProjectStore.create(project.root / "results" / "Other", "Other")
    elif unsafe == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_text("keep")
        try:
            (project.root / "results" / "link").symlink_to(outside)
        except OSError:
            pytest.skip("No symlink support")
    elif unsafe == "missing_layout":
        (project.root / "objects").rmdir()
    else:
        monkeypatch.setattr(Path, "home", lambda: project.root)
    with pytest.raises(ProjectError):
        trash_project(str(project.project_file), project.manifest["id"],
                      move_to_trash=lambda _path: pytest.fail("unsafe scope reached trash"))
    assert project.project_file.is_file()


def test_wrong_identity_never_moves_a_replaced_project(project):
    with pytest.raises(ProjectError) as caught:
        trash_project(str(project.project_file), "0" * 32,
                      move_to_trash=lambda _path: pytest.fail("wrong identity reached trash"))
    assert caught.value.code == "project_identity_mismatch"
    assert project.project_file.is_file()


def _settle(app, adapter):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.processEvents()
        adapter._complete_pending()
        adapter._complete_history()
        if not adapter.projectBusy and not adapter.recentBusy:
            return
        time.sleep(0.005)
    pytest.fail("launcher action did not settle")


def test_adapter_removes_history_only_after_confirmed_trash_success(project, tmp_path, app, monkeypatch):
    history = RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    history.record(project.project_file, "Study")
    adapter = cast(Any, ProjectLauncherAdapter(recent_projects=history))
    try:
        _settle(app, adapter)
        assert adapter.projectEntries[0]["projectId"] == project.manifest["id"]
        def failure(*_args):
            raise ProjectError("trash_failed", "Could not recycle")
        monkeypatch.setattr("coat_sim.ui.launcher_projects.trash_project", failure)
        assert adapter.trashRecentProject(str(project.project_file), project.manifest["id"])
        _settle(app, adapter)
        assert adapter.projectEntries and project.project_file.is_file()
        assert adapter.projectError == "Could not recycle"
        def success(descriptor, identity):
            def mover(path):
                Path(path).rename(tmp_path / "trash")
                return True, str(tmp_path / "trash")
            return trash_project(descriptor, identity, move_to_trash=mover)
        monkeypatch.setattr("coat_sim.ui.launcher_projects.trash_project", success)
        assert adapter.trashRecentProject(str(project.project_file), project.manifest["id"])
        _settle(app, adapter)
        assert not adapter.projectEntries and (tmp_path / "trash" / "project.coatproj").is_file()
        assert "回收站" in adapter.projectFileMessage
        assert not hasattr(adapter, "host") and not hasattr(adapter, "_leases")
    finally:
        adapter.stop()


def test_a_changed_directory_is_refused_before_the_os_move(project, monkeypatch):
    import coat_sim.ui.launcher_project_files as module
    original = module._safe_tree
    checks = 0
    def changing_tree(store):
        nonlocal checks
        result = original(store)
        checks += 1
        if checks == 1:
            (store.root / "results" / "late.txt").write_text("keep")
        return result
    monkeypatch.setattr(module, "_safe_tree", changing_tree)
    with pytest.raises(ProjectError) as caught:
        trash_project(str(project.project_file), project.manifest["id"],
                      move_to_trash=lambda _path: pytest.fail("changed tree reached trash"))
    assert caught.value.code == "stale_project_directory"
    assert project.project_file.is_file() and (project.root / "results" / "late.txt").is_file()


def test_stale_dialog_project_identity_is_not_rebound_to_a_refreshed_row(project, tmp_path, app):
    history = RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    history.record(project.project_file, "Study")
    adapter = cast(Any, ProjectLauncherAdapter(recent_projects=history))
    try:
        _settle(app, adapter)
        assert not adapter.trashRecentProject(str(project.project_file), "0" * 32)
        assert not adapter.projectBusy and adapter.projectEntries and project.project_file.is_file()
    finally:
        adapter.stop()


def test_active_selected_project_is_retained_when_delete_is_refused(project, tmp_path, app):
    history = RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    history.record(project.project_file, "Study")
    adapter = cast(Any, ProjectLauncherAdapter(recent_projects=history))
    try:
        _settle(app, adapter)
        adapter._selected_file = str(project.project_file)
        with ProjectOwnerLease.acquire(project.project_file, project.manifest["id"]):
            assert adapter.trashRecentProject(str(project.project_file), project.manifest["id"])
            _settle(app, adapter)
        assert adapter.projectEntries[0]["active"]
        assert "关闭此工程" in adapter.projectError
        assert project.project_file.is_file()
    finally:
        adapter.stop()


def test_duplicate_delete_is_rejected_until_the_first_result_settles(project, tmp_path, app, monkeypatch):
    history = RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    history.record(project.project_file, "Study")
    adapter = cast(Any, ProjectLauncherAdapter(recent_projects=history))
    release, started = Event(), Event()
    calls = []
    def pending(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        raise ProjectError("trash_failed", "Could not recycle")
    monkeypatch.setattr("coat_sim.ui.launcher_projects.trash_project", pending)
    try:
        _settle(app, adapter)
        descriptor, identity = str(project.project_file), project.manifest["id"]
        assert adapter.trashRecentProject(descriptor, identity)
        assert started.wait(2)
        assert not adapter.trashRecentProject(descriptor, identity)
        assert len(calls) == 1
        release.set()
        _settle(app, adapter)
        assert adapter.projectEntries and project.project_file.is_file()
        assert adapter.projectError == "Could not recycle"
    finally:
        release.set()
        adapter.stop()


@pytest.mark.parametrize("protected", ["temp", "app", "cwd", "standard", "ancestor", "system"])
def test_protected_locations_never_reach_the_trash_mover(project, monkeypatch, protected):
    import coat_sim.ui.launcher_project_files as module
    if protected == "temp":
        monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(project.root))
    elif protected == "app":
        monkeypatch.setattr(module, "__file__", str(project.root / "coat_sim/ui/launcher_project_files.py"))
    elif protected == "cwd":
        monkeypatch.chdir(project.root)
    elif protected == "standard":
        monkeypatch.setattr(module.QStandardPaths, "writableLocation", lambda _kind: str(project.root))
    elif protected == "ancestor":
        monkeypatch.setattr(Path, "home", lambda: project.root / "other-user-data")
    else:
        if module.os.name != "nt":
            pytest.skip("Windows system-directory boundary")
        monkeypatch.setenv("SystemRoot", str(project.root))
    with pytest.raises(ProjectError) as caught:
        trash_project(str(project.project_file), project.manifest["id"],
                      move_to_trash=lambda _path: pytest.fail("protected location reached trash"))
    assert caught.value.code == "unsafe_project_directory"
    assert project.project_file.is_file()


def test_drive_root_is_rejected_before_any_tree_or_trash_operation(project):
    from types import SimpleNamespace
    from coat_sim.ui.launcher_project_files import _safe_tree
    root = Path(project.root.anchor)
    with pytest.raises(ProjectError) as caught:
        _safe_tree(cast(Any, SimpleNamespace(root=root, project_file=root / "project.coatproj")))
    assert caught.value.code == "unsafe_project_directory"
    assert project.project_file.is_file()
