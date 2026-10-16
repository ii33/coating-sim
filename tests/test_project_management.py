"""Explicit identity and revision contracts for headless project management."""
from __future__ import annotations

import pytest

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_management import ProjectManagementService
from coat_sim.project_recovery import ProjectOwnerLease
import coat_sim.project_recovery as recovery
from tests.test_project_core import setup_project


pytestmark = pytest.mark.contract


def _call_error(service, operation, arguments, code):
    with pytest.raises(ProjectError) as caught:
        service.execute(operation, arguments)
    assert caught.value.code == code


def test_create_and_inspect_are_bound_to_explicit_project_identity(tmp_path):
    service = ProjectManagementService()
    root = tmp_path / "created"

    created = service.execute("create", {
        "root": str(root), "name": "Explicit", "operation_id": "create-1",
    })
    assert created["ok"] is True
    assert created["root"] == str(root.resolve())
    assert created["project_file"] == str((root / "project.coatproj").resolve())
    assert created["project_id"] == created["project"]["id"]
    assert created["operation_id"] == "create-1"

    inspected = service.execute("inspect", {
        "root": created["project_file"], "project_id": created["project_id"],
    })
    assert inspected["project_id"] == created["project_id"]
    assert inspected["revision"] == created["revision"]

    _call_error(service, "inspect", {"root": created["project_file"], "project_id": "other-id"},
                "project_identity_mismatch")
    _call_error(service, "inspect", {"project_id": created["project_id"]}, "invalid_arguments")


def test_mutations_require_operation_id_revision_and_are_project_scoped(tmp_path, monkeypatch):
    service = ProjectManagementService()
    monkeypatch.setattr(recovery, "project_owner_runtime_root", lambda: tmp_path / "owner-runtime")
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first, _, _, configuration_id = setup_project(first_root, "First")
    second, _, _, second_configuration_id = setup_project(second_root, "Second")

    _call_error(service, "set_default", {
        "root": str(first.project_file), "project_id": first.manifest["id"],
        "configuration_id": None, "expected_revision": first.manifest["revision"],
    }, "invalid_arguments")
    _call_error(service, "set_default", {
        "root": str(first.project_file), "project_id": first.manifest["id"],
        "configuration_id": None, "operation_id": "missing-revision",
    }, "revision_required")
    _call_error(service, "set_default", {
        "root": str(first.project_file), "project_id": second.manifest["id"],
        "configuration_id": None, "operation_id": "wrong-owner",
        "expected_revision": first.manifest["revision"],
    }, "project_identity_mismatch")

    initial = first.manifest["revision"]
    changed = service.execute("set_default", {
        "root": str(first.project_file), "project_id": first.manifest["id"],
        "configuration_id": None, "operation_id": "clear-default-1",
        "expected_revision": initial,
    })
    assert changed["operation_id"] == "clear-default-1"
    assert changed["project_id"] == first.manifest["id"]
    assert changed["revision"] == initial + 1
    assert ProjectStore.open(first.project_file).manifest["default_configuration_id"] is None
    assert ProjectStore.open(second.project_file).manifest["default_configuration_id"] == second_configuration_id
    assert configuration_id in {row["id"] for row in first.manifest["documents"]}

    _call_error(service, "set_default", {
        "root": str(first.project_file), "project_id": first.manifest["id"],
        "configuration_id": None, "operation_id": "stale-write",
        "expected_revision": initial,
    }, "stale_project")


def test_headless_write_refuses_to_bypass_active_project_owner(tmp_path, monkeypatch):
    service = ProjectManagementService()
    monkeypatch.setattr(recovery, "project_owner_runtime_root", lambda: tmp_path / "owner-runtime")
    store, _, _, _ = setup_project(tmp_path, "Owned")
    revision = store.manifest["revision"]
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"], generation="gui-generation")
    try:
        _call_error(service, "set_default", {
            "root": str(store.project_file), "project_id": store.manifest["id"],
            "configuration_id": None, "operation_id": "blocked-write",
            "expected_revision": revision,
        }, "active_project_owner")
        assert ProjectStore.open(store.project_file).manifest["revision"] == revision
    finally:
        lease.close(clean=False)


def test_authorized_root_scope_blocks_project_import_and_export_without_writes(tmp_path):
    from tests.test_input_snapshot import write_json

    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source_inside = selected / "source.csrc"
    source_outside = tmp_path / "outside-source.csrc"
    point = {"schema_version": 2, "kind": "point", "source_exponent": 1.0}
    write_json(source_inside, point)
    write_json(source_outside, point)

    store = ProjectStore.create(selected / "project", "Selected")
    document_id = store.import_document(source_inside)
    external_project = ProjectStore.create(outside / "project", "Outside")
    service = ProjectManagementService(authorized_roots=[selected])

    def snapshot(root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    selected_before = snapshot(store.root)
    external_before = snapshot(external_project.root)
    source_before = source_outside.read_bytes()
    destination = outside / "exported"

    _call_error(service, "inspect", {
        "root": str(external_project.project_file), "project_id": external_project.manifest["id"],
    }, "project_root_not_authorized")
    _call_error(service, "import", {
        "root": str(store.project_file), "project_id": store.manifest["id"],
        "file": str(source_outside), "operation_id": "outside-import",
        "expected_revision": store.manifest["revision"],
    }, "project_root_not_authorized")
    _call_error(service, "export", {
        "root": str(store.project_file), "project_id": store.manifest["id"],
        "document_id": document_id, "destination": str(destination),
    }, "project_root_not_authorized")

    assert snapshot(store.root) == selected_before
    assert snapshot(external_project.root) == external_before
    assert source_outside.read_bytes() == source_before
    assert not destination.exists()


def test_execute_owned_validates_bound_store_and_revision_without_second_lease(tmp_path, monkeypatch):
    from coat_sim.project_recovery import ProjectOwnerLease
    store = ProjectStore.create(tmp_path / "owned", "Owned")
    service = ProjectManagementService(authorized_roots=[store.root])
    identity = store.manifest["id"]
    other = ProjectStore.create(store.root / "other", "Other")
    with ProjectOwnerLease.acquire(store.project_file, identity, owner_runtime_root=tmp_path / "owner-runtime"):
        def second_lease(*_args, **_kwargs):
            raise AssertionError("Owning adapter must not reacquire its lifetime lease")
        monkeypatch.setattr(ProjectOwnerLease, "acquire", second_lease)
        import json
        source = store.root / "source.csrc"
        source.write_text(json.dumps({"schema_version": 2, "kind": "point", "source_exponent": 1.0}))
        result = service.execute_owned("import", {
            "root": str(store.project_file), "project_id": identity, "operation_id": "owned-import",
            "expected_revision": 0, "file": str(source)}, store=store, project_id=identity)
        assert result["revision"] == 1
        assert result["operation_id"] == "owned-import"
        with pytest.raises(ProjectError) as stale:
            service.execute_owned("set_default", {
                "root": str(store.project_file), "project_id": identity, "operation_id": "stale-default",
                "expected_revision": 0, "configuration_id": None}, store=store, project_id=identity)
        assert stale.value.code == "stale_project"
        before_store = {path.relative_to(store.root): path.read_bytes()
                        for path in store.root.rglob("*") if path.is_file()}
        before_other = {path.relative_to(other.root): path.read_bytes()
                        for path in other.root.rglob("*") if path.is_file()}
        with pytest.raises(ProjectError) as wrong:
            service.execute_owned("set_default", {
                "root": str(other.project_file), "project_id": identity, "operation_id": "wrong",
                "expected_revision": 1, "configuration_id": None}, store=store, project_id=identity)
        assert wrong.value.code == "project_identity_mismatch"
        assert before_store == {path.relative_to(store.root): path.read_bytes()
                                for path in store.root.rglob("*") if path.is_file()}
        assert before_other == {path.relative_to(other.root): path.read_bytes()
                                for path in other.root.rglob("*") if path.is_file()}
