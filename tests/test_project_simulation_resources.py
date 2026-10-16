"""Managed optical input resources, binary closure and portable archives."""
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any
import zipfile

import pytest

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.simulation_assets import save_simulation_input_asset
from coat_sim.project_package import export_project, import_project
from tests.test_portable_asset_bundle import fixture_snapshot

pytestmark = pytest.mark.contract


def ray(asset=None):
    return {"schema": "coat_sim.geometric_raytrace_ui_document", "schema_version": 1,
        "request": {"schema": "coat_sim.geometric_raytrace", "schema_version": 1,
            "units": {"length": "mm", "wavelength": "nm", "index": "n+i*k"},
            "polarization": "unpolarized", "samples": [], "surfaces": [],
            "spectrum": [{"wavelength_nm": 550., "weight": 1.}]},
        "deposition_asset": None if asset is None else {
            "path": str(asset), "sha256": hashlib.sha256(Path(asset).read_bytes()).hexdigest()}}


def asset(root, name="Input"):
    return Path(save_simulation_input_asset(root, name=name, settings={},
        input_snapshot=fixture_snapshot(), confirmed=True)["asset_path"])


def api(root):
    return ProjectResourceService(ProjectStore.create(root, "Project"))


def test_unbound_new_ray_and_appearance_are_real_saved_resources(tmp_path):
    service = api(tmp_path / "project")
    ray_doc = service.save_simulation_resource("raytrace_request", ray(), name="Ray",
                                              description="Overview", notes="Notes")
    appearance = {"schema": "coat_sim.appearance_configuration", "version": 1,
        "context": None, "draft": {}, "geometry_thickness_mode": "inherited",
        "backend": "cpu", "precision": "native"}
    app_doc = service.save_simulation_resource("appearance_configuration", appearance, name="Appearance")
    assert ray_doc.runtime["payload"] == ray()
    assert app_doc.runtime["payload"] == appearance
    reopened = ProjectResourceService(ProjectStore.open(service.store.project_file))
    assert reopened.load_document(Path(ray_doc.path).stem).data["description"] == "Overview"
    loaded = reopened.load_document(Path(app_doc.path).stem)
    assert loaded.runtime is not None and loaded.runtime["payload"] == appearance
    assert len(reopened.list_documents("raytrace_request")) == 1


def test_save_copies_binary_input_and_archive_contains_only_explicit_closure(tmp_path):
    source = asset(tmp_path / "external")
    service = api(tmp_path / "project")
    document = service.save_simulation_resource("raytrace_request", ray(source), name="Ray")
    identity = Path(document.path).stem
    native = document.runtime["payload"]["deposition_asset"]["path"]
    assert Path(native).is_relative_to(service.root / "assets")
    assert Path(native).read_bytes() == source.read_bytes()
    assert str(source) not in str(document.data)
    source.unlink()
    assert service.load_document(identity).runtime["payload"]["deposition_asset"]["path"] == native
    (service.root / "results" / "unrequested-result.bin").write_bytes(b"private result")
    package = tmp_path / "inputs.cproject"
    export_project(service.store, package)
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert any(name.startswith("assets/") and name.endswith(".coatasset") for name in names)
        assert not any("unrequested-result" in name for name in names)
    restored = import_project(package, tmp_path / "restored")
    copy = ProjectResourceService(restored).load_document(identity)
    assert copy.runtime is not None
    copied_path = Path(copy.runtime["payload"]["deposition_asset"]["path"])
    assert copied_path.is_relative_to(restored.root)
    assert copied_path.read_bytes() == Path(native).read_bytes()


def test_selective_package_transfer_copies_only_selected_binary_dependency_and_cleans_spool(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    source = api(tmp_path / "source")
    one = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "one", "One")), name="One")
    source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "two", "Two")), name="Two")
    package = tmp_path / "source.cproject"
    export_project(source.store, package)
    target = api(tmp_path / "target")
    catalog = target.inspect_resources(package)
    assert not list(tmp_path.glob("coat-resource-plan-*"))
    plan = target.preview_resource_transfer(catalog["catalog_id"], [Path(one.path).stem])
    assert len(plan["dependency_ids"]) == 1
    assert len(list(tmp_path.glob("coat-resource-plan-*"))) == 1
    result = target.apply_resource_transfer(plan["plan_id"])
    assert not list(tmp_path.glob("coat-resource-plan-*"))
    assert len(target.store.manifest["documents"]) == 2
    loaded = target.load_document(result["selected_ids"][0])
    assert loaded.data["name"] == "One"
    assert Path(loaded.runtime["payload"]["deposition_asset"]["path"]).is_relative_to(target.root)


def test_cancel_and_owner_close_clean_private_spool_without_removing_published_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    source = api(tmp_path / "source")
    doc = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    target = api(tmp_path / "target")
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [Path(doc.path).stem])
    target.cancel_resource_transfer(plan["plan_id"])
    assert not list(tmp_path.glob("coat-resource-plan-*"))
    target.preview_resource_transfer(catalog["catalog_id"], [Path(doc.path).stem])
    target.close_resource_exchange()
    assert not list(tmp_path.glob("coat-resource-plan-*"))
    assert Path(doc.runtime["payload"]["deposition_asset"]["path"]).exists()


def test_metadata_save_and_picker_refresh_do_not_rehash_large_inputs(tmp_path, monkeypatch):
    service = api(tmp_path / "project")
    document = service.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    identity = Path(document.path).stem
    def forbid(*_args, **_kwargs):
        raise AssertionError("small metadata refresh must not stream binary inputs")
    monkeypatch.setattr("coat_sim.core.project_binary_assets.stream_asset", forbid)
    assert service.list_documents("raytrace_request")[0]["id"] == identity
    info = service.resource_information(identity)
    service.save_resource_information(identity, {"summary": "Small edit"}, expected_revision=info["token"])


def test_corrupt_or_oversized_asset_failure_leaves_no_manifest_or_payload_change(tmp_path):
    service = api(tmp_path / "project")
    path = tmp_path / "bad.coatasset"
    path.write_bytes(b"invalid archive")
    before = service.store.manifest
    with pytest.raises(ProjectError):
        service.save_simulation_resource("raytrace_request", ray(path))
    assert service.store.manifest == before
    assert not list((service.root / "assets").rglob("*.coatasset"))
    assert not list(tmp_path.glob("coat-project-input-*"))


def test_global_export_reimports_new_simulation_kind_with_owned_dependency(tmp_path, monkeypatch):
    from coat_sim.core import data_files, material_business
    application = tmp_path / "application"
    application.mkdir()
    monkeypatch.setattr(data_files, "PROJECT_ROOT", application)
    monkeypatch.setattr(data_files, "DATA_ROOT", application / "data")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [])
    source = api(tmp_path / "source")
    document = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")), name="Global ray")
    catalog = source.inspect_resources("project")
    plan = source.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem], "global")
    result = source.apply_resource_transfer(plan["plan_id"])
    assert result["files"] and all(Path(path).is_relative_to(application / "data" / "raytrace") for path in result["files"])
    target = api(tmp_path / "target")
    global_catalog = target.inspect_resources("global")
    identity = next(row["id"] for row in global_catalog["rows"] if row["kind"] == "raytrace_request")
    plan = target.preview_resource_transfer(global_catalog["catalog_id"], [identity])
    copied = target.apply_resource_transfer(plan["plan_id"])
    assert target.load_document(copied["selected_ids"][0]).data["name"] == "Global ray"


def test_browse_native_ray_file_copies_declared_dependency(tmp_path):
    import json
    input_asset = asset(tmp_path / "external")
    selected = tmp_path / "request.cray"
    selected.write_text(json.dumps(ray(input_asset)))
    service = api(tmp_path / "project")
    catalog = service.inspect_resources(selected)
    identity = next(row["id"] for row in catalog["rows"] if row["kind"] == "raytrace_request")
    plan = service.preview_resource_transfer(catalog["catalog_id"], [identity])
    result = service.apply_resource_transfer(plan["plan_id"])
    native = service.load_document(result["selected_ids"][0]).runtime["payload"]
    assert Path(native["deposition_asset"]["path"]).is_relative_to(service.root)


def test_appearance_projection_owns_spd_and_preserves_independent_metadata(tmp_path):
    from coat_sim.core.project_simulation_resources import NUMERIC_DRAFT_FIELDS, BOOL_DRAFT_FIELDS, _sha
    input_asset = asset(tmp_path / "external")
    spectrum = tmp_path / "source.csv"
    spectrum.write_text("500,1\n600,2\n")
    profile = fixture_snapshot()["files"][0]["data"]
    substrate = fixture_snapshot()["files"][-1]["data"]
    draft: dict[str, Any] = {field: "1" for field in NUMERIC_DRAFT_FIELDS}
    draft.update({field: False for field in BOOL_DRAFT_FIELDS})
    draft.update(area_kind="relative", environment_kind="relative", environment_spectrum=None,
                 area_spectrum={"path": str(spectrum), "wavelength_nm": [500., 600.], "value": [1., 2.]})
    context = {"schema": "coat_sim.appearance_launch_context", "version": 1,
        "asset_path": str(input_asset), "asset_root": str(tmp_path),
        "asset_sha256": hashlib.sha256(input_asset.read_bytes()).hexdigest(),
        "process_profile": profile, "process_profile_sha256": _sha(profile), "substrate_profile": substrate,
        "captured_step_ids": [], "layer_field_units": "nm", "layer_field_coordinate_frame": "substrate_local"}
    payload: dict[str, Any] = {"schema": "coat_sim.appearance_configuration", "version": 1, "context": context,
        "draft": draft, "geometry_thickness_mode": "overridden", "backend": "cpu", "precision": "native"}
    service = api(tmp_path / "project")
    document = service.save_simulation_resource("appearance_configuration", payload, name="Appearance",
                                               description="Summary", notes="Notes")
    assert len(service.store.manifest["documents"]) == 3
    native = document.runtime["payload"]
    assert native["context"]["asset_root"] == str(service.root)
    assert Path(native["draft"]["area_spectrum"]["path"]).is_relative_to(service.root)
    assert document.data["description"] == "Summary" and document.data["notes"] == "Notes"
    assert str(tmp_path / "external") not in str(document.data)
    changed = deepcopy(payload)
    changed["draft"]["area_spectrum"]["value"] = [99., 99.]
    before = service.store.manifest
    with pytest.raises(ProjectError):
        service.save_simulation_resource("appearance_configuration", changed)
    assert service.store.manifest == before


@pytest.mark.parametrize("attack", ["extra", "size", "digest", "traversal", "unknown_kind", "wrong_role", "wrong_identity", "symlink"])
def test_hostile_asset_package_is_rejected_without_partial_import(tmp_path, attack):
    import json
    import stat
    from coat_sim.core.project_dependencies import json_bytes
    service = api(tmp_path / "source")
    service.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    package = tmp_path / "source.cproject"
    export_project(service.store, package)
    with zipfile.ZipFile(package) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(content["project.coatproj"])
    record = next(row for row in manifest["documents"] if row["kind"] == "binary_asset")
    metadata = json.loads(content[record["path"]])
    old_member = metadata["payload_path"]
    if attack == "extra":
        content["assets/extra.bin"] = b"unrequested"
    elif attack == "size":
        metadata["size"] += 1
    elif attack == "digest":
        metadata["sha256"] = "0" * 64
    elif attack == "traversal":
        metadata["payload_path"] = "assets/../escape.coatasset"
    elif attack == "unknown_kind":
        record["kind"] = "unknown_future_asset_kind"
    elif attack == "wrong_role":
        metadata["role"] = "spd_csv"
    elif attack == "wrong_identity":
        metadata["payload_path"] = "assets/" + "a" * 32 + "/" + "b" * 32 + ".coatasset"
        content[metadata["payload_path"]] = content.pop(old_member)
    content[record["path"]] = json_bytes(metadata)
    record["sha256"] = hashlib.sha256(content[record["path"]]).hexdigest()
    content["project.coatproj"] = json_bytes(manifest)
    with zipfile.ZipFile(package, "w") as archive:
        for name, raw in content.items():
            info = zipfile.ZipInfo(name)
            if attack == "symlink" and name == old_member:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, raw)
    destination = tmp_path / "destination"
    with pytest.raises(ProjectError):
        import_project(package, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".project-package-*"))


def test_close_keeps_reserved_spool_until_apply_finishes_then_cleans(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from coat_sim.core import project_resource_transfer
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    source = api(tmp_path / "source")
    document = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    target = api(tmp_path / "target")
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
    entered, resume = Event(), Event()
    original = project_resource_transfer._merge
    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(project_resource_transfer, "_merge", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(target.apply_resource_transfer, plan["plan_id"])
        try:
            assert entered.wait(5)
            target.close_resource_exchange()
            assert list(tmp_path.glob("coat-resource-plan-*"))
        finally:
            resume.set()
        with pytest.raises(ProjectError):
            pending.result(timeout=5)
    assert not list(tmp_path.glob("coat-resource-plan-*"))
    assert target.store.manifest["documents"] == []


@pytest.mark.parametrize("action", ["save", "transfer"])
def test_slow_asset_staging_does_not_hold_host_and_close_prevents_publication(tmp_path, monkeypatch, action):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from coat_sim.core import project_binary_assets
    from coat_sim.project_session import ProjectSessionError, ProjectSessionHost
    source = api(tmp_path / "source")
    document = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    store = ProjectStore.create(tmp_path / "target", "Target")
    host = ProjectSessionHost(lambda _root: store)
    session = host.open_project(store.root)
    target = ProjectResourceService(store, session_host=host, binding=session.binding)
    if action == "transfer":
        catalog = target.inspect_resources(source.store.project_file)
        plan = target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
        def operation():
            return target.apply_resource_transfer(plan["plan_id"])
    else:
        def operation():
            return target.save_simulation_resource("raytrace_request", document.runtime["payload"])
    entered, resume = Event(), Event()
    original = project_binary_assets.capture_asset
    def delayed(source, metadata, destination):
        if ".project-asset-stage-" in str(destination):
            entered.set()
            assert resume.wait(10)
        return original(source, metadata, destination)
    monkeypatch.setattr(project_binary_assets, "capture_asset", delayed)
    with ThreadPoolExecutor(max_workers=3) as executor:
        pending = executor.submit(operation)
        try:
            assert entered.wait(5)
            assert executor.submit(target.list_documents).result(timeout=2) == []
            assert executor.submit(host.close_project, session.binding,
                prepare_close=lambda _binding, _routes: True).result(timeout=2)
        finally:
            resume.set()
        with pytest.raises((ProjectError, ProjectSessionError)):
            pending.result(timeout=5)
    assert store.manifest["documents"] == []
    assert not list((store.root / "assets").rglob("*.coatasset"))
    assert not list((store.root / "assets").glob(".project-asset-stage-*"))


def test_source_changed_during_asset_staging_rejects_before_final_commit(tmp_path, monkeypatch):
    from coat_sim.core import project_binary_assets
    source, target = api(tmp_path / "source"), api(tmp_path / "target")
    document = source.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "external")))
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [Path(document.path).stem])
    original = project_binary_assets.capture_asset
    def changed(source_capability, metadata, destination):
        captured = original(source_capability, metadata, destination)
        if ".project-asset-stage-" in str(destination):
            source.store.project_file.touch()
        return captured
    monkeypatch.setattr(project_binary_assets, "capture_asset", changed)
    with pytest.raises(ProjectError, match="changed") as error:
        target.apply_resource_transfer(plan["plan_id"])
    assert error.value.code == "stale_source"
    assert target.store.manifest["documents"] == []
    assert not list((target.root / "assets").rglob("*.coatasset"))
    target.cancel_resource_transfer(plan["plan_id"])


def test_final_publication_uses_verified_receipts_without_streaming_old_or_new_assets(tmp_path, monkeypatch):
    from coat_sim.core import project_binary_assets, project_store
    service = api(tmp_path / "target")
    service.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "old", "Old")))
    original = service.store._publish
    original_stream, original_capture = project_binary_assets.stream_asset, project_binary_assets.capture_asset
    def forbid(*_args, **_kwargs):
        raise AssertionError("Final host-locked publication must not hash/copy binary payloads")
    def publish(*args, **kwargs):
        monkeypatch.setattr(project_binary_assets, "stream_asset", forbid)
        monkeypatch.setattr(project_binary_assets, "capture_asset", forbid)
        try:
            return original(*args, **kwargs)
        finally:
            monkeypatch.setattr(project_binary_assets, "stream_asset", original_stream)
            monkeypatch.setattr(project_binary_assets, "capture_asset", original_capture)
    monkeypatch.setattr(service.store, "_publish", publish)
    document = service.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "new", "New")))
    assert document.valid
    previous = service.store.manifest
    monkeypatch.setattr(project_store, "replace_with_retry", lambda *_args: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(ProjectError):
        service.save_simulation_resource("raytrace_request", ray(asset(tmp_path / "failed", "Failed")))
    assert service.store.manifest == previous
    assert len(list((service.root / "assets").rglob("*.coatasset"))) == 2
    assert not list((service.root / "assets").glob(".project-asset-stage-*"))
