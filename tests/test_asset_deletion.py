"""Reversible deletion of self-made project assets only; no global fixture I/O."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_material_library import ProjectMaterialLibrary
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.materials import default_material_profile
from coat_sim.project_session import ProjectSessionError, ProjectSessionHost

pytestmark = pytest.mark.contract


def source(name="Source"):
    return {"schema_version": 2, "kind": "point", "source_exponent": 1., "name": name}


@pytest.fixture
def owned(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    return store, host, binding, ProjectResourceService(store, session_host=host, binding=binding)


def plan(service, identity):
    catalog = service.inspect_resources("project")
    return service.preview_resource_deletion(catalog["catalog_id"], [identity])


def test_project_delete_is_confirmed_scoped_recoverable_and_persists_index(owned, tmp_path):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    other = ProjectStore.create(tmp_path / "other", "Other")
    other_id = other.create_document("source", source("Other"))
    other_bytes = other.project_file.read_bytes()
    row = store.manifest["documents"][0]
    old_bytes = (store.root / row["path"]).read_bytes()
    before = store.project_file.read_bytes()
    preview = plan(service, identity)
    assert preview["scope"] == "project" and preview["project_id"] == service.project_id
    assert preview["session_generation"] == service.binding.generation and preview["can_apply"]
    assert store.project_file.read_bytes() == before
    receipt = service.apply_resource_deletion(preview["plan_id"])
    assert store.manifest["documents"] == []
    assert (store.root / row["path"]).read_bytes() == old_bytes  # Frozen inputs are not unlinked.
    assert Path(receipt["recovery_path"]).is_dir() and receipt["recoverable"]
    assert other.project_file.read_bytes() == other_bytes and other.manifest["documents"][0]["id"] == other_id
    with pytest.raises(ProjectError, match="失效"):
        service.apply_resource_deletion(preview["plan_id"])

    # No in-memory token is needed to find or restore the durable archive.
    fresh_store = ProjectStore.open(store.project_file)
    fresh = ProjectResourceService(fresh_store)
    assert [row["receipt_id"] for row in fresh.list_resource_deletions()] == [receipt["receipt_id"]]
    restored = fresh.restore_resource_deletion(receipt["receipt_id"])
    assert restored["restored"] and fresh_store.manifest["documents"][0]["id"] == identity
    assert fresh.load_document(identity).data["name"] == "Source"
    assert fresh.list_resource_deletions() == []
    after = fresh_store.project_file.read_bytes()
    with pytest.raises(ProjectError) as conflict:
        fresh.restore_resource_deletion(receipt["receipt_id"])
    assert conflict.value.code == "destination_exists" and fresh_store.project_file.read_bytes() == after


def test_material_profile_and_optical_are_explicit_one_asset_components(owned):
    store, _host, _binding, service = owned
    library = ProjectMaterialLibrary(service, global_library=SimpleNamespace(catalog_records=lambda: []))
    profile = default_material_profile("Custom", id="custom")
    library.write(profile)
    records = store.manifest["documents"]
    profile_id = next(row["id"] for row in records if row["kind"] == "material_profile")
    optical_id = next(row["id"] for row in records if row["kind"] == "optical_material")
    optical = plan(service, optical_id)
    assert not optical["can_apply"] and optical["blockers"][0]["code"] == "paired_material"
    preview = plan(service, profile_id)
    assert preview["can_apply"] and set(preview["affected_ids"]) == {profile_id, optical_id}
    assert len(preview["rows"]) == 2 and sum(row["same_asset_component"] for row in preview["rows"]) == 1
    receipt = service.apply_resource_deletion(preview["plan_id"])
    assert store.manifest["documents"] == []
    service.restore_resource_deletion(receipt["receipt_id"])
    assert {row["id"] for row in store.manifest["documents"]} == {profile_id, optical_id}


def test_referenced_source_blocks_without_cascade_or_archive_creation(owned):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    machine = {"schema_version": 5, "carrier_x_mm": 0, "carrier_y_mm": 0, "planet_offset_mm": 0,
               "planet_tilt_deg": 0, "substrate_z_mm": 100, "sun_teeth": 120, "planet_teeth": 36,
               "gear_module_mm": 0, "sources": [{"file": identity + ".csrc", "x_mm": 0, "y_mm": 0, "z_mm": 0}],
               "masks": [], "monitors": []}
    machine_id = store.create_document("machine", machine)
    before = store.project_file.read_bytes()
    preview = plan(service, identity)
    assert not preview["can_apply"]
    assert any(row["code"] == "referenced_asset" and row["resource_id"] == machine_id for row in preview["blockers"])
    with pytest.raises(ProjectError) as rejected:
        service.apply_resource_deletion(preview["plan_id"])
    assert rejected.value.code == "deletion_blocked"
    assert store.project_file.read_bytes() == before and not (store.root / "imports" / ".asset-recycle").exists()


@pytest.mark.parametrize("invalid", [[], ["missing"], ["a", "b"], "not-a-list", [True], ["../asset"]])
def test_unknown_or_superset_selection_rejects_without_changes(owned, invalid):
    store, _host, _binding, service = owned
    store.create_document("source", source())
    before = store.project_file.read_bytes()
    catalog = service.inspect_resources("project")
    with pytest.raises(ProjectError):
        service.preview_resource_deletion(catalog["catalog_id"], invalid)
    assert store.project_file.read_bytes() == before


@pytest.mark.parametrize("failure", ["cancel", "catalog_closed", "owner_closed", "frozen", "changed"])
def test_plan_owner_version_and_close_fences_are_zero_mutation(owned, failure):
    store, host, binding, service = owned
    identity = store.create_document("source", source())
    preview = plan(service, identity)
    if failure == "cancel":
        service.cancel_resource_deletion(preview["plan_id"])
    elif failure == "catalog_closed":
        service.close_resource_exchange()
    elif failure == "owner_closed":
        assert host.close_project(binding, prepare_close=lambda *_: True)
    elif failure == "frozen":
        service._gate.frozen = True
    else:
        store.update_information({"notes": "new revision"}, expected_revision=store.manifest["revision"])
    before = store.project_file.read_bytes()
    with pytest.raises((ProjectError, ProjectSessionError)) as rejected:
        service.apply_resource_deletion(preview["plan_id"])
    assert isinstance(rejected.value, (ProjectError, ProjectSessionError))
    assert rejected.value.code == {"cancel": "expired_plan", "catalog_closed": "project_closed",
                                   "owner_closed": "project_closed", "frozen": "project_editing_frozen",
                                   "changed": "stale_source"}[failure]
    assert store.project_file.read_bytes() == before
    assert len(store.manifest["documents"]) == 1


def test_external_catalog_or_other_service_cannot_delete_owner(owned, tmp_path):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    other = ProjectStore.create(tmp_path / "other", "Other")
    other.create_document("source", source("Other"))
    external = service.inspect_resources(other.project_file)
    before = store.project_file.read_bytes(), other.project_file.read_bytes()
    with pytest.raises(ProjectError) as rejected:
        service.preview_resource_deletion(external["catalog_id"], [identity])
    assert rejected.value.code == "wrong_scope"
    preview = plan(service, identity)
    fork = service.fork_editor()
    with pytest.raises(ProjectError) as expired:
        fork.apply_resource_deletion(preview["plan_id"])
    assert expired.value.code == "expired_plan"
    assert (store.project_file.read_bytes(), other.project_file.read_bytes()) == before


@pytest.mark.parametrize("state", ["dirty", "native_retained", "active_related", "recovery", "jobs"])
def test_live_related_editor_recovery_and_task_blockers_are_rechecked(owned, state):
    store, host, binding, service = owned
    identity = store.create_document("source", source())
    preview = plan(service, identity)
    bridge = SimpleNamespace(_path=service.document_handle(identity), _dirty=state == "dirty")
    controller = SimpleNamespace(bridge=bridge, window=object() if state == "active_related" else None,
                                 _pending_ui={} if state == "native_retained" else None,
                                 _close_pending_ui=None, _pending_information_edit=None)
    resource = SimpleNamespace(controllers={"source": controller},
                               _pending_recovery={"mask": {"dirty": True}} if state == "recovery" else {})
    host.claim_resource(binding, "resource_workspace", resource)
    if state == "jobs":
        host.claim_resource(binding, "compute_workspace", SimpleNamespace(active_jobs=lambda: [{"id": "job-1"}], has_pending_draft=False))
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError) as rejected:
        service.apply_resource_deletion(preview["plan_id"])
    assert rejected.value.code == "deletion_blocked" and store.project_file.read_bytes() == before
    newer = plan(service, identity)
    assert not newer["can_apply"] and newer["blockers"]
    assert any(row.get("module") == "source" or row.get("job_ids") == ["job-1"] or row["code"] == "unsaved_recovery"
               for row in newer["blockers"])


def test_clean_library_details_are_not_a_service_editor_blocker(owned):
    store, host, binding, service = owned
    identity = store.create_document("source", source())
    # The library itself owns no resource editor controller. An unrelated clean
    # source presentation is not mistaken for a dependency of this selection.
    unrelated = store.create_document("source", source("Other"))
    controller = SimpleNamespace(bridge=SimpleNamespace(_path=service.document_handle(unrelated), _dirty=False),
                                 window=object(), _pending_ui=None, _close_pending_ui=None, _pending_information_edit=None)
    host.claim_resource(binding, "resource_workspace", SimpleNamespace(controllers={"source": controller}, _pending_recovery={}))
    assert plan(service, identity)["can_apply"]


def test_archive_write_or_publish_failure_never_removes_original(owned):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    before = store.project_file.read_bytes()
    preview = plan(service, identity)
    with patch.object(store, "_publish", side_effect=OSError("publication failed")):
        with pytest.raises(OSError, match="publication failed"):
            service.apply_resource_deletion(preview["plan_id"])
    assert store.project_file.read_bytes() == before and store.manifest["documents"][0]["id"] == identity
    assert service.list_resource_deletions() == []  # Prepared copy is not a false deletion.
    assert list((store.root / "imports" / ".asset-recycle").glob("*/receipt.json"))


@pytest.mark.parametrize("target", ["os.fsync", "_fsync_directory"])
def test_archive_fsync_failure_is_before_any_project_publication(owned, target):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    before = store.project_file.read_bytes()
    preview = plan(service, identity)
    with patch("coat_sim.core.asset_deletion." + target, side_effect=OSError("archive unavailable")):
        with pytest.raises(OSError, match="archive unavailable"):
            service.apply_resource_deletion(preview["plan_id"])
    assert store.project_file.read_bytes() == before and len(store.manifest["documents"]) == 1
    assert service.list_resource_deletions() == []


def test_recovery_directory_entries_are_synced_before_manifest_removal(owned):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    preview = plan(service, identity)
    original_publish = store._publish
    synced: list[Path] = []

    def checked_publish(records, documents, **kwargs):
        base = store.root / "imports" / ".asset-recycle"
        assert len(synced) == 3 and synced[0].parent == base
        assert synced[1:] == [base, base.parent]
        assert (synced[0] / "receipt.json").is_file()
        return original_publish(records, documents, **kwargs)

    with patch("coat_sim.core.asset_deletion._fsync_directory", side_effect=synced.append), \
            patch.object(store, "_publish", side_effect=checked_publish):
        service.apply_resource_deletion(preview["plan_id"])
    assert store.manifest["documents"] == []


def test_corrupt_or_foreign_archive_never_restores(owned, tmp_path):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    receipt = service.apply_resource_deletion(plan(service, identity)["plan_id"])
    path = Path(receipt["recovery_path"]) / "receipt.json"
    import json
    value = json.loads(path.read_text())
    value["owner_id"] = "foreign"
    path.write_bytes(json_bytes(value))
    before = store.project_file.read_bytes()
    assert service.list_resource_deletions() == []
    with pytest.raises(ProjectError) as rejected:
        service.restore_resource_deletion(receipt["receipt_id"])
    assert rejected.value.code == "invalid_archive" and store.project_file.read_bytes() == before


def test_restore_missing_dependency_or_publication_failure_keeps_archive_and_current_graph(owned):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    receipt = service.apply_resource_deletion(plan(service, identity)["plan_id"])
    before = store.project_file.read_bytes()
    archive = (Path(receipt["recovery_path"]) / "receipt.json").read_bytes()
    with patch.object(store, "_publish", side_effect=OSError("restore failed")):
        with pytest.raises(OSError, match="restore failed"):
            service.restore_resource_deletion(receipt["receipt_id"])
    assert store.project_file.read_bytes() == before
    assert (Path(receipt["recovery_path"]) / "receipt.json").read_bytes() == archive
    assert service.restore_resource_deletion(receipt["receipt_id"])["restored"]


def test_archive_symlink_is_rejected_without_following_it(owned, tmp_path):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    target = tmp_path / "outside"
    target.mkdir()
    try:
        (store.root / "imports" / ".asset-recycle").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError):
        service.apply_resource_deletion(plan(service, identity)["plan_id"])
    assert store.project_file.read_bytes() == before and list(target.iterdir()) == []


def test_restore_requires_still_present_dependencies_before_atomic_publication(owned):
    store, _host, _binding, service = owned
    source_id = store.create_document("source", source())
    machine = {"schema_version": 5, "carrier_x_mm": 0, "carrier_y_mm": 0, "planet_offset_mm": 0,
               "planet_tilt_deg": 0, "substrate_z_mm": 100, "sun_teeth": 120, "planet_teeth": 36,
               "gear_module_mm": 0, "sources": [{"file": source_id + ".csrc", "x_mm": 0, "y_mm": 0, "z_mm": 0}],
               "masks": [], "monitors": []}
    machine_id = store.create_document("machine", machine)
    machine_receipt = service.apply_resource_deletion(plan(service, machine_id)["plan_id"])
    source_receipt = service.apply_resource_deletion(plan(service, source_id)["plan_id"])
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError) as rejected:
        service.restore_resource_deletion(machine_receipt["receipt_id"])
    assert rejected.value.code == "missing_dependency" and store.project_file.read_bytes() == before
    assert len(service.list_resource_deletions()) == 2
    service.restore_resource_deletion(source_receipt["receipt_id"])
    service.restore_resource_deletion(machine_receipt["receipt_id"])
    assert {row["id"] for row in store.manifest["documents"]} == {source_id, machine_id}


def test_default_configuration_is_explicitly_protected(tmp_path):
    from tests.test_project_core import setup_project
    store, _external, _process, default_id = setup_project(tmp_path)
    service = ProjectResourceService(store)
    before = store.project_file.read_bytes()
    preview = plan(service, default_id)
    assert not preview["can_apply"]
    assert any(row["code"] == "default_asset" for row in preview["blockers"])
    with pytest.raises(ProjectError) as rejected:
        service.apply_resource_deletion(preview["plan_id"])
    assert rejected.value.code == "deletion_blocked" and store.project_file.read_bytes() == before


def test_global_service_scope_tokens_and_recycle_interface_use_only_current_library(tmp_path, monkeypatch):
    from coat_sim.core import data_files, material_business
    from coat_sim.core.global_resource_library import GlobalResourceLibrary
    root = tmp_path / "global"
    root.mkdir()
    monkeypatch.setattr(data_files, "PROJECT_ROOT", root)
    monkeypatch.setattr(data_files, "DATA_ROOT", root / "data")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [])
    file = root / "data/sources/source.csrc"
    file.parent.mkdir(parents=True)
    original = json_bytes(source())
    file.write_bytes(original)
    service = GlobalResourceLibrary()
    try:
        catalog = service.inspect_resources()
        identity = catalog["rows"][0]["id"]
        preview = service.preview_resource_deletion(catalog["catalog_id"], [identity])
        assert preview["scope"] == "global" and preview["can_apply"]
        assert preview["rows"][0]["id"] == identity and len(preview["snapshot_sha256"]) == 64
        receipt = service.apply_resource_deletion(preview["plan_id"])
        assert not file.exists() and service.list_resource_deletions()
        assert service.restore_resource_deletion(receipt["receipt_id"])
        assert file.read_bytes() == original
        with pytest.raises(ProjectError) as expired:
            service.apply_resource_deletion(preview["plan_id"])
        assert expired.value.code == "expired_plan"
        other = ProjectStore.create(tmp_path / "other", "Other")
        other.create_document("source", source("Other"))
        external = service.inspect_resources(other.project_file)
        before = other.project_file.read_bytes()
        with pytest.raises(ProjectError) as rejected:
            service.preview_resource_deletion(external["catalog_id"], [identity])
        assert rejected.value.code == "wrong_scope" and other.project_file.read_bytes() == before
    finally:
        service.close()


@pytest.mark.parametrize("operation", ["delete", "restore"])
def test_same_store_concurrent_publication_cannot_be_overwritten_by_old_selection(owned, operation):
    store, _host, _binding, service = owned
    identity = store.create_document("source", source())
    preview = plan(service, identity)
    receipt = service.apply_resource_deletion(preview["plan_id"]) if operation == "restore" else None
    original_publish = store._publish
    added = []

    def publish_after_another_save(records, documents, **kwargs):
        # Same-object mutation is stronger than a different ProjectStore's
        # on-disk CAS: it updates this object's current fingerprint as well.
        with patch.object(store, "_publish", original_publish):
            added.append(store.create_document("source", source("Concurrent saved input")))
        return original_publish(records, documents, **kwargs)

    with patch.object(store, "_publish", side_effect=publish_after_another_save):
        with pytest.raises(ProjectError) as rejected:
            if operation == "delete":
                service.apply_resource_deletion(preview["plan_id"])
            else:
                assert receipt is not None
                service.restore_resource_deletion(receipt["receipt_id"])
    assert rejected.value.code == "stale_project" and len(added) == 1
    ids = {row["id"] for row in store.manifest["documents"]}
    assert added[0] in ids and (identity in ids) == (operation == "delete")
    assert service.load_document(added[0]).data["name"] == "Concurrent saved input"


def test_only_exact_clean_embedded_material_details_can_be_registered_as_library_owner(owned):
    store, host, binding, service = owned
    library = ProjectMaterialLibrary(service, global_library=SimpleNamespace(catalog_records=lambda: []))
    library.write(default_material_profile("Custom", id="custom"))
    identity = next(row["id"] for row in store.manifest["documents"] if row["kind"] == "material_profile")
    window = object()
    resource = SimpleNamespace(controllers={}, windows={"materials": window}, _pending_recovery={})
    controller = SimpleNamespace(bridge=SimpleNamespace(_saved_id="custom", _dirty=False),
                                 window=window, _pending_ui=None, _close_pending_ui=None,
                                 _pending_information_edit=None, owner_binding=binding,
                                 resource_workspace=resource, service=service.fork_editor())
    resource.controllers["materials"] = controller
    host.claim_resource(binding, "resource_workspace", resource)
    reservation = host.reserve_module(binding.project_id, "project_resources")
    host.attach_window(reservation.route, window)
    assert not plan(service, identity)["can_apply"]
    service.bind_asset_library_view_owner(controller)
    assert plan(service, identity)["can_apply"]
    controller.bridge._dirty = True
    assert not plan(service, identity)["can_apply"]  # Registration never exempts unsaved text.
    controller.bridge._dirty = False
    controller.owner_binding = SimpleNamespace(project_id=binding.project_id, generation="wrong", root=binding.root)
    with pytest.raises(ProjectError) as rejected:
        service.bind_asset_library_view_owner(controller)
    assert rejected.value.code == "wrong_scope" and not plan(service, identity)["can_apply"]
    controller.owner_binding = binding
    service.bind_asset_library_view_owner(controller)
    preview = plan(service, identity)
    host.release_window(reservation.route)
    replacement = host.reserve_module(binding.project_id, "project_resources")
    host.attach_window(replacement.route, object())
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError) as stale:
        service.apply_resource_deletion(preview["plan_id"])
    assert stale.value.code == "deletion_blocked" and store.project_file.read_bytes() == before


def test_project_recycle_history_does_not_expand_root_scope_or_saved_input_exports(owned, tmp_path):
    import zipfile
    from coat_sim.project_package import export_project
    from coat_sim.ui.launcher_project_files import project_deletion_plan
    store, _host, _binding, service = owned
    removed = store.create_document("source", source("Removed"))
    active = store.create_document("source", source("Active"))
    receipt = service.apply_resource_deletion(plan(service, removed)["plan_id"])
    assert Path(receipt["recovery_path"]).is_relative_to(store.root / "imports" / ".asset-recycle")
    preview = project_deletion_plan(str(store.project_file), store.manifest["id"])
    assert Path(preview["directoryPath"]) == store.root
    package = tmp_path / "saved.cproject"
    result = export_project(store, package)
    assert result["document_count"] == 1
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert not any(".asset-recycle" in name or "receipt" in name for name in names)
        assert len(names) == 2
    assert {row["id"] for row in store.snapshot_saved_inputs()["manifest"]["documents"]} == {active}


@pytest.mark.parametrize("selected_projection", ["profile", "optical", "both"])
def test_global_preview_displays_every_component_of_one_physical_material(tmp_path, monkeypatch, selected_projection):
    from coat_sim.core import data_files, material_business
    from coat_sim.core.global_resource_library import GlobalResourceLibrary
    from coat_sim.core.material_business import MaterialLibrary
    root = tmp_path / "application"
    root.mkdir()
    monkeypatch.setattr(data_files, "PROJECT_ROOT", root)
    monkeypatch.setattr(data_files, "DATA_ROOT", root / "data")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [])
    MaterialLibrary(directory=root / "data/materials").write(default_material_profile("One asset", id="one"))
    service = GlobalResourceLibrary()
    try:
        catalog = service.inspect_resources()
        ids = {row["kind"]: row["id"] for row in catalog["rows"]}
        selected = ([ids["material_profile"]] if selected_projection == "profile" else
                    [ids["optical_material"]] if selected_projection == "optical" else sorted(ids.values()))
        preview = service.preview_resource_deletion(catalog["catalog_id"], selected)
        assert preview["can_apply"] and set(preview["affected_ids"]) == set(ids.values())
        assert {row["id"] for row in preview["rows"]} == set(ids.values())
        assert sum(row["selected"] for row in preview["rows"]) == len(selected)
        receipt = service.apply_resource_deletion(preview["plan_id"])
        assert receipt["recoverable"] and service.inspect_resources()["rows"] == []
        assert service.restore_resource_deletion(receipt["receipt_id"])["restored"]
    finally:
        service.close()


def test_project_binary_recycle_preserves_frozen_bytes_and_restores_in_dependency_order(owned, tmp_path):
    from tests.test_project_simulation_resources import asset, ray
    store, _host, _binding, service = owned
    saved = service.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")), name="Ray")
    ray_id = Path(saved.path).stem
    binary_id = next(row["id"] for row in store.manifest["documents"] if row["kind"] == "binary_asset")
    payload = store.binary_asset_path(binary_id)
    frozen = payload.read_bytes()
    assert not plan(service, binary_id)["can_apply"]
    ray_receipt = service.apply_resource_deletion(plan(service, ray_id)["plan_id"])
    binary_receipt = service.apply_resource_deletion(plan(service, binary_id)["plan_id"])
    assert store.manifest["documents"] == [] and payload.read_bytes() == frozen
    service.restore_resource_deletion(binary_receipt["receipt_id"])
    service.restore_resource_deletion(ray_receipt["receipt_id"])
    assert store.binary_asset_path(binary_id).read_bytes() == frozen
    assert Path(service.load_document(ray_id).runtime["payload"]["deposition_asset"]["path"]).read_bytes() == frozen
