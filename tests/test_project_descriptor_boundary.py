"""Explicit arbitrary-name project descriptors retain their chosen write target."""
from io import StringIO
import json
from unittest.mock import patch
import zipfile

import pytest

from coat_sim.core.film_design import new_design
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectError, ProjectStore
from coat_sim.project_cli import main
from coat_sim.project_management import ProjectManagementService
from coat_sim.project_package import export_project, import_project, read_project_package

pytestmark = pytest.mark.contract


def renamed_project(tmp_path, filename="任意研究名称.coatproj"):
    created = ProjectStore.create(tmp_path / "project", "Named study")
    target = created.root / filename
    created.project_file.rename(target)
    return ProjectStore.open(target)


@pytest.mark.parametrize("filename", ["任意研究名称.coatproj", "Research-study.COATPROJ"])
def test_arbitrary_descriptor_name_survives_open_save_and_unrelated_cwd(tmp_path, monkeypatch, filename):
    store = renamed_project(tmp_path, filename)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    identity = store.create_document("film", new_design("Saved by actual file"))
    assert store.project_file.name == filename
    assert not (store.root / MANIFEST_FILENAME).exists()
    reopened = ProjectStore.open(store.project_file)
    assert reopened.project_file == store.project_file
    assert reopened.manifest == store.manifest
    assert reopened.resolve_document(identity)["documents"][0]["data"]["name"] == "Saved by actual file"


def test_same_directory_descriptors_are_selected_explicitly_and_never_scanned(tmp_path):
    first = renamed_project(tmp_path)
    alternate = first.root / "different.coatproj"
    alternate.write_bytes(first.project_file.read_bytes())
    second = ProjectStore.open(alternate)
    first_bytes = first.project_file.read_bytes()
    identity = second.create_document("film", new_design("Alternate"))
    assert first.project_file.read_bytes() == first_bytes
    assert ProjectStore.open(first.project_file).manifest["documents"] == []
    assert ProjectStore.open(alternate).manifest["documents"][0]["id"] == identity
    with pytest.raises(ProjectError) as caught:
        ProjectStore.open(first.root)
    assert caught.value.code == "invalid_project_file"


def test_named_descriptor_stale_and_failed_writes_preserve_target(tmp_path):
    current = renamed_project(tmp_path)
    stale = ProjectStore.open(current.project_file)
    current.create_document("film", new_design("First"))
    before = current.project_file.read_bytes()
    with pytest.raises(ProjectError) as caught:
        stale.create_document("film", new_design("Stale"))
    assert caught.value.code == "stale_project"
    assert current.project_file.read_bytes() == before
    with patch("coat_sim.core.project_store.replace_with_retry", side_effect=OSError("synthetic failure")):
        with pytest.raises(ProjectError) as caught:
            current.create_document("film", new_design("Failed"))
    assert caught.value.code == "io_error"
    assert current.project_file.read_bytes() == before
    assert not (current.root / MANIFEST_FILENAME).exists()


def test_management_accepts_actual_file_and_owned_service_rejects_same_parent_alias(tmp_path):
    store = renamed_project(tmp_path)
    service = ProjectManagementService(authorized_roots=[store.root])
    arguments = {"root": str(store.project_file), "project_id": store.manifest["id"]}
    inspected = service.execute("inspect", arguments)
    assert inspected["root"] == str(store.root)
    assert inspected["project_file"] == str(store.project_file)
    alias = store.root / "other.coatproj"
    alias.write_bytes(store.project_file.read_bytes())
    with pytest.raises(ProjectError) as caught:
        service.execute_owned("inspect", {**arguments, "root": str(alias)},
                              store=store, project_id=store.manifest["id"])
    assert caught.value.code == "project_identity_mismatch"
    for path in (store.root, store.root / "project.json"):
        with pytest.raises(ProjectError) as caught:
            service.execute_owned("inspect", {**arguments, "root": str(path)},
                                  store=store, project_id=store.manifest["id"])
        assert caught.value.code == "invalid_project_file"


def test_cli_inspects_named_file_and_reports_descriptor(tmp_path):
    store = renamed_project(tmp_path)
    output, error = StringIO(), StringIO()
    assert main(["inspect", str(store.project_file)], stdout=output, stderr=error) == 0
    assert json.loads(output.getvalue())["project_file"] == str(store.project_file)
    assert not error.getvalue()
    for path in (store.root, store.root / "project.json"):
        output, error = StringIO(), StringIO()
        assert main(["inspect", str(path)], stdout=output, stderr=error) == 2
        assert json.loads(error.getvalue())["error"]["code"] == "invalid_project_file"


def test_package_normalizes_only_archive_descriptor_name(tmp_path):
    store = renamed_project(tmp_path)
    identity = store.create_document("film", new_design("Packaged"))
    package = tmp_path / "study.cproject"
    export_project(store, package)
    with zipfile.ZipFile(package) as archive:
        assert MANIFEST_FILENAME in archive.namelist()
        assert store.project_file.name not in archive.namelist()
    restored = import_project(package, tmp_path / "restored")
    assert restored.project_file == restored.root / MANIFEST_FILENAME
    assert restored.manifest["id"] != store.manifest["id"]
    assert restored.resolve_document(identity)["documents"][0]["data"]["name"] == "Packaged"
    for path in (tmp_path / "mistaken.coatproj", tmp_path / "mistaken.zip"):
        with pytest.raises(ProjectError) as caught:
            export_project(store, path)
        assert caught.value.code == "invalid_package"
        assert not path.exists()


@pytest.mark.parametrize("entrypoint", ["read", "import", "catalog"])
def test_package_import_rejects_directory_before_creating_destination(tmp_path, entrypoint):
    package = tmp_path / "directory.cproject"
    package.mkdir()
    destination = tmp_path / "restored"
    with pytest.raises(ProjectError) as caught:
        if entrypoint == "read":
            read_project_package(package)
        elif entrypoint == "import":
            import_project(package, destination)
        else:
            service = ProjectResourceService(ProjectStore.create(tmp_path / "owner", "Owner"))
            service.inspect_resources(package)
    assert caught.value.code == "invalid_package"
    assert not destination.exists()
    assert not list(tmp_path.glob(".project-package-*"))
