"""Project package saved-state isolation and hostile archive regression tests."""
import json
import stat
import zipfile

import pytest

from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectError, ProjectStore
from coat_sim.project_package import export_project, import_project
from tests.test_project_core import setup_project


def test_roundtrip_complete_saved_graph_excludes_runtime_and_new_identity(tmp_path):
    store, _, _, config = setup_project(tmp_path)
    (store.root / "credentials.txt").write_text("not for export")
    store.record_recovery([config])
    package = tmp_path / "project.cproject"
    result = export_project(store, package)
    assert result["document_count"] == len(store.manifest["documents"])
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {MANIFEST_FILENAME, *(row["path"] for row in store.manifest["documents"])}
        assert json.loads(archive.read(MANIFEST_FILENAME)) == store.manifest
    restored = import_project(package, tmp_path / "restored")
    assert restored.project_file == restored.root / MANIFEST_FILENAME
    assert restored.project_file.is_file()
    assert not (restored.root / "project.json").exists()
    assert ProjectStore.open(restored.project_file).manifest == restored.manifest
    assert restored.manifest["id"] != store.manifest["id"]
    assert restored.manifest["default_configuration_id"] == config
    assert restored.snapshot_saved_inputs()["documents"] == store.snapshot_saved_inputs()["documents"]
    restored_graph, original_graph = restored.resolve_default(), store.resolve_default()
    assert restored_graph is not None and original_graph is not None
    assert restored_graph["runtime"] == original_graph["runtime"]
    assert not (restored.root / "recovery.json").exists()
    assert not (restored.root / "credentials.txt").exists()


def test_empty_project_and_no_overwrite(tmp_path):
    store = ProjectStore.create(tmp_path / "source", "Empty")
    package = tmp_path / "empty.cproject"
    export_project(store, package)
    with pytest.raises(ProjectError, match="destination"):
        export_project(store, package)
    restored = import_project(package, tmp_path / "copy")
    assert restored.resolve_default() is None
    with pytest.raises(ProjectError, match="destination"):
        import_project(package, restored.root)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/windows", "objects/extra.json",
                                  "PROJECT.JSON", "PROJECT.COATPROJ"])
def test_extra_and_traversal_entries_rejected_before_publication(tmp_path, name):
    store = ProjectStore.create(tmp_path / "source", "Empty")
    package = tmp_path / "bad.cproject"
    export_project(store, package)
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr(name, b"x")
    with pytest.raises(ProjectError):
        import_project(package, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.glob(".project-package-*"))


def test_legacy_manifest_only_package_is_rejected_without_publication(tmp_path):
    store = ProjectStore.create(tmp_path / "source", "Legacy archive")
    package = tmp_path / "legacy.cproject"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("project.json", store.project_file.read_bytes())
    destination = tmp_path / "copy"
    with pytest.raises(ProjectError) as error:
        import_project(package, destination)
    assert error.value.code == "invalid_package"
    assert not destination.exists()
    assert not list(tmp_path.glob(".project-package-*"))


def test_descriptor_extension_is_never_used_as_package_output(tmp_path):
    store = ProjectStore.create(tmp_path / "source", "P")
    descriptor_before = store.project_file.read_bytes()
    for destination in (store.project_file, tmp_path / "archive.coatproj"):
        with pytest.raises(ProjectError) as error:
            export_project(store, destination)
        assert error.value.code == "invalid_package"
    assert store.project_file.read_bytes() == descriptor_before
    assert not (tmp_path / "archive.coatproj").exists()


def test_missing_package_source_is_structured_and_never_publishes_destination(tmp_path):
    destination = tmp_path / "copy"
    with pytest.raises(ProjectError) as error:
        import_project(tmp_path / "missing.cproject", destination)
    assert error.value.code == "invalid_package"
    assert not destination.exists()
    assert not list(tmp_path.glob(".project-package-*"))


def test_linked_package_source_is_rejected_without_reading_or_publication(tmp_path):
    store = ProjectStore.create(tmp_path / "source", "P")
    package = tmp_path / "source.cproject"
    export_project(store, package)
    before = package.read_bytes()
    link = tmp_path / "linked.cproject"
    link.symlink_to(package)
    destination = tmp_path / "copy"
    with pytest.raises(ProjectError) as error:
        import_project(link, destination)
    assert error.value.code == "unsafe_path"
    assert package.read_bytes() == before
    assert not destination.exists()
    assert not list(tmp_path.glob(".project-package-*"))


def test_symlink_member_rejected(tmp_path):
    store = ProjectStore.create(tmp_path / "source", "Empty")
    package = tmp_path / "bad.cproject"
    export_project(store, package)
    with zipfile.ZipFile(package, "a") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../outside")
    with pytest.raises(ProjectError):
        import_project(package, tmp_path / "copy")


def test_corrupt_nondefault_document_rejected(tmp_path):
    store, _, process_id, selected = setup_project(tmp_path)
    store.add_deposition_configuration("Nondefault", process_id)
    store.save_document(selected, store.resolve_document(selected)["configuration"])
    package = tmp_path / "bad.cproject"
    export_project(store, package)
    with zipfile.ZipFile(package) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    last = store.manifest["documents"][-1]["path"]
    contents[last] += b" "
    with zipfile.ZipFile(package, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    with pytest.raises(ProjectError, match="digest"):
        import_project(package, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_capacity_guard_checks_declared_uncompressed_size(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "source", "Empty")
    package = tmp_path / "large.cproject"
    export_project(store, package)
    monkeypatch.setattr("coat_sim.project_package.MAX_FILE_BYTES", 10)
    with pytest.raises(ProjectError) as caught:
        import_project(package, tmp_path / "copy")
    assert caught.value.code == "capacity_exceeded"
    assert not (tmp_path / "copy").exists()


def test_stale_store_export_does_not_publish(tmp_path):
    store = ProjectStore.create(tmp_path / "source", "Empty")
    from coat_sim.core.project_dependencies import json_bytes
    changed = store.manifest
    changed["revision"] += 1
    store.project_file.write_bytes(json_bytes(changed))
    package = tmp_path / "stale.cproject"
    with pytest.raises(ProjectError) as caught:
        export_project(store, package)
    assert caught.value.code == "stale_project"
    assert not package.exists()


def test_cli_package_roundtrip(tmp_path):
    import io
    import json
    from coat_sim.project_cli import main
    store = ProjectStore.create(tmp_path / "source", "CLI")
    package = tmp_path / "cli.cproject"
    output, errors = io.StringIO(), io.StringIO()
    assert main(["export-package", str(store.project_file), str(package)], stdout=output, stderr=errors) == 0
    output = io.StringIO()
    assert main(["import-package", str(package), str(tmp_path / "copy")], stdout=output, stderr=errors) == 0
    assert json.loads(output.getvalue())["project"]["id"] != store.manifest["id"]
    assert errors.getvalue() == ""


def test_hash_valid_nondefault_process_with_invalid_source_slot_is_rejected(tmp_path):
    import hashlib
    from coat_sim.core.project_dependencies import json_bytes, parse_json
    store, _, _, _ = setup_project(tmp_path)
    package = tmp_path / "invalid-runtime.cproject"
    export_project(store, package)
    with zipfile.ZipFile(package) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = parse_json(contents[MANIFEST_FILENAME])
    manifest["default_configuration_id"] = None
    process = next(row for row in manifest["documents"] if row["kind"] == "process")
    data = parse_json(contents[process["path"]])
    data["steps"][0]["source_slot"] = 99
    contents[process["path"]] = json_bytes(data)
    process["sha256"] = hashlib.sha256(contents[process["path"]]).hexdigest()
    contents[MANIFEST_FILENAME] = json_bytes(manifest)
    with zipfile.ZipFile(package, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    with pytest.raises(ProjectError) as caught:
        import_project(package, tmp_path / "copy")
    assert caught.value.code == "invalid_dependency_graph"
    assert not (tmp_path / "copy").exists()


def test_export_publication_race_never_replaces_winner(tmp_path, monkeypatch):
    import coat_sim.project_package as packages
    store = ProjectStore.create(tmp_path / "source", "Race")
    target = tmp_path / "backup.cproject"
    original_target = packages._target
    def race(path):
        result = original_target(path)
        result.write_bytes(b"concurrent writer")
        return result
    monkeypatch.setattr(packages, "_target", race)
    with pytest.raises(ProjectError) as caught:
        packages.export_project(store, target)
    assert caught.value.code == "destination_exists"
    assert target.read_bytes() == b"concurrent writer"
    assert not list(tmp_path.glob(".project-package-*"))
