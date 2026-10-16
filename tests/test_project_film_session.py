"""Project-owned drafts, explicit import, and revision/lifetime integration."""

from copy import deepcopy
import json

import pytest

from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionError, ProjectSessionHost
from tests.test_project_film_service import bound_design, material


pytestmark = pytest.mark.contract


def workspace_at(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    return ProjectFilmWorkspace.for_project(host, binding), store


def test_workspace_owns_one_lazy_editor_and_rejects_second_owner(tmp_path):
    workspace, _store = workspace_at(tmp_path)
    assert workspace._editor is None
    assert ProjectFilmWorkspace.for_project(workspace.host, workspace.binding) is workspace
    assert workspace.editor is workspace.editor
    with pytest.raises(ProjectSessionError, match="already has an owner"):
        ProjectFilmWorkspace(workspace.host, workspace.binding)
    assert workspace.host.routes(workspace.binding.project_id) == ()


def test_new_draft_save_creates_then_updates_one_managed_document(tmp_path):
    workspace, store = workspace_at(tmp_path)
    editor = workspace.editor
    assert editor.set_design_value("name", "First")
    assert editor.save_current()
    first = editor.snapshot()
    assert first.path and not first.dirty
    row = next(row for row in store.manifest["documents"] if row["kind"] == "film")
    assert first.path == str(workspace.service.document_handle(row["id"]))
    assert editor.set_design_value("name", "Second")
    assert editor.save_current()
    assert workspace.service.load_document(row["id"]).design["name"] == "Second"
    assert len([row for row in store.manifest["documents"] if row["kind"] == "film"]) == 1
    assert not (store.root / "documents").exists()
    assert not list(store.root.rglob("*.sqlite3"))


def test_saved_selection_keeps_managed_handle_for_ordinary_save(tmp_path):
    workspace, _store = workspace_at(tmp_path)
    saved = workspace.service.create_film(bound_design(), materials={"shared": material()})
    assert workspace.editor.request_load_saved_design(saved.document_id)
    assert workspace.editor.snapshot().path == str(saved.handle)
    assert workspace.editor.set_design_value("note", "Selected managed film")
    assert workspace.editor.save_current()
    assert workspace.service.load_document(saved.document_id).design["note"] == "Selected managed film"


def test_explicit_import_copies_closure_and_refreshes_existing_editor_picker(tmp_path):
    workspace, store = workspace_at(tmp_path)
    editor = workspace.editor
    source = tmp_path / "external.cfilm"
    source.write_text(json.dumps(bound_design()), encoding="utf-8")
    assert editor.request_import_film(str(source), materials={"shared": material()})
    snapshot = editor.snapshot()
    assert snapshot.path and not snapshot.dirty
    assert snapshot.material_options["substrate"][0]["id"] == "shared"
    source.unlink()
    assert editor.save_current()
    assert len([row for row in store.manifest["documents"] if row["kind"] == "film"]) == 1


def test_cancel_import_preserves_draft_and_does_not_publish(tmp_path):
    workspace, store = workspace_at(tmp_path)
    editor = workspace.editor
    assert editor.set_design_value("name", "Keep draft")
    source = tmp_path / "external.cfilm"
    source.write_text(json.dumps(bound_design()), encoding="utf-8")
    before = store.manifest
    assert not editor.request_import_film(str(source), materials={"shared": material()})
    assert editor.snapshot().pending_confirmation["action"] == "project_import_film"
    assert not editor.resolve_pending_action(False)
    assert editor.snapshot().design["name"] == "Keep draft"
    assert editor.snapshot().dirty
    assert store.manifest == before


def test_missing_import_resources_preserve_active_draft(tmp_path):
    workspace, store = workspace_at(tmp_path)
    editor = workspace.editor
    before = deepcopy(editor.snapshot().design)
    source = tmp_path / "external.cfilm"
    source.write_text(json.dumps(bound_design()), encoding="utf-8")
    assert not editor.request_import_film(str(source))
    assert editor.snapshot().error
    assert editor.snapshot().design == before
    assert store.manifest["documents"] == []


def test_external_save_cannot_hide_staleness_of_owned_editor(tmp_path):
    workspace, _store = workspace_at(tmp_path)
    saved = workspace.service.create_film()
    editor = workspace.editor
    assert editor.request_load_saved_design(saved.document_id)
    changed = deepcopy(saved.design)
    changed["name"] = "External update"
    workspace.service.save_document(saved.document_id, changed, expected_revision=saved.revision)
    assert editor.set_design_value("name", "Old editor update")
    assert not editor.save_current()
    assert editor.snapshot().dirty
    assert "changed" in editor.snapshot().error
    assert workspace.service.load_document(saved.document_id).design["name"] == "External update"


def test_closed_generation_rejects_retained_service_and_new_workspace_is_independent(tmp_path):
    workspace, store = workspace_at(tmp_path)
    old_editor = workspace.editor
    assert old_editor.save_current()
    assert workspace.host.close_project(workspace.binding, prepare_close=lambda _binding, _routes: True)
    with pytest.raises(ProjectSessionError) as error:
        workspace.service.create_film()
    assert error.value.code == "project_closed"
    reopened = workspace.host.open_project(store.root)
    fresh = ProjectFilmWorkspace.for_project(workspace.host, reopened.binding)
    assert fresh is not workspace and fresh.editor is not old_editor
    with pytest.raises(ProjectSessionError) as error:
        workspace.service.list_saved_designs()
    assert error.value.code == "stale_session"


def test_explicit_close_policy_can_save_owned_draft_before_accepting_close(tmp_path):
    workspace, store = workspace_at(tmp_path)
    editor = workspace.editor
    assert editor.save_current()
    saved_id = workspace.service.list_saved_designs()[0]["design_id"]
    assert editor.set_design_value("name", "Saved during close decision")

    def prepare_close(_binding, _routes):
        assert editor.save_current()
        return True

    assert workspace.host.close_project(workspace.binding, prepare_close=prepare_close)
    reopened = ProjectStore.open(store.project_file)
    graph = reopened.resolve_document(saved_id)
    assert next(row for row in graph["documents"] if row["id"] == saved_id)["data"]["name"] == "Saved during close decision"


def test_project_close_prompt_describes_in_memory_draft_retention(tmp_path):
    workspace, _store = workspace_at(tmp_path)
    editor = workspace.editor
    assert editor.set_design_value("name", "Unsaved draft")
    assert not editor.request_close()
    assert "退出程序后不会保留" in editor.snapshot().pending_confirmation["message"]
    assert editor.resolve_pending_action(True)
    assert editor.snapshot().dirty
    assert editor.snapshot().design["name"] == "Unsaved draft"
