"""Readable managed resources retain UUID identity and descriptor atomicity."""
from pathlib import PurePosixPath
from unittest.mock import patch

import pytest

from coat_sim.core.project_dependencies import EXTENSIONS, ProjectError, json_bytes, portable_path
from coat_sim.core.project_paths import safe_resource_filename
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore, _manifest, _rename_directory_no_replace
from coat_sim.project_package import export_project, import_project
from tests.test_project_core import by_kind, file_snapshot, setup_project

pytestmark = pytest.mark.contract


def source(name):
    return {"schema_version": 2, "kind": "point", "name": name}


def row(store, identity):
    return next(value for value in store.manifest["documents"] if value["id"] == identity)


@pytest.mark.parametrize("name,expected", [
    ("实验 蒸发源", "实验 蒸发源.csrc"), ("CON", "_CON.csrc"),
    ("com¹.log", "_com1.log.csrc"), ("CON .txt", "_CON .txt.csrc"), ("LPT9", "_LPT9.csrc"),
    ("../escape\\outside:*?", "_escape_outside___.csrc"),
    (" . ", "Unnamed source.csrc"), ("name. ", "name.csrc"),
])
def test_safe_name_is_a_portable_basename(name, expected):
    actual = safe_resource_filename(name, "source")
    assert actual == expected
    assert "/" not in actual and "\\" not in actual
    assert portable_path(actual) == actual


def test_case_unicode_and_truncation_collisions_are_numbered_without_ids():
    occupied: set[str] = set()
    values = [safe_resource_filename(name, "source", occupied=occupied)
              for name in ("Mirror", "mirror", "Ｍｉｒｒｏｒ", "é", "e\u0301", "a" * 120, "a" * 119 + "b")]
    assert values[:5] == ["Mirror.csrc", "mirror (2).csrc", "Mirror (3).csrc", "é.csrc", "é (2).csrc"]
    assert values[5:] == ["a" * 80 + ".csrc", "a" * 80 + " (2).csrc"]
    assert len(safe_resource_filename("镜" * 120, "source").encode("utf-8")) <= 190


def test_named_revisions_rename_and_noop_preserve_identity_and_old_bytes(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    identity = store.create_document("source", source("Copper"))
    first = row(store, identity)
    assert first["path"] == "data/sources/r00000001/Copper.csrc"
    old_bytes = (store.root / first["path"]).read_bytes()
    value = store.resolve_document(identity)["documents"][0]["data"]
    store.save_document(identity, value)
    assert row(store, identity) == first
    value["name"] = "Renamed"
    store.save_document(identity, value)
    second = row(store, identity)
    assert second["id"] == first["id"]
    assert second["path"] == "data/sources/r00000002/Renamed.csrc"
    assert (store.root / first["path"]).read_bytes() == old_bytes
    assert ProjectStore.open(store.project_file).resolve_document(identity)["documents"][0]["data"] == value


def test_duplicate_names_keep_suffixes_across_revisions_and_categories(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    first = store.create_document("source", source("Same"))
    second = store.create_document("source", source("same"))
    before = {key: PurePosixPath(row(store, key)["path"]).name for key in (first, second)}
    assert before == {first: "Same.csrc", second: "same (2).csrc"}
    document = store.resolve_document(first)["documents"][0]["data"]
    document["notes"] = "one edit"
    store.save_document(first, document)
    assert {key: PurePosixPath(row(store, key)["path"]).name for key in (first, second)} == before


def test_existing_revision_and_case_variant_are_not_merged_or_overwritten(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    unrelated = store.root / "data/sources/R00000001"
    unrelated.mkdir()
    keep = unrelated / "Copper.csrc"
    keep.write_text("unrelated", encoding="utf-8")
    identity = store.create_document("source", source("Copper"))
    assert row(store, identity)["path"] == "data/sources/r00000001-2/Copper.csrc"
    assert keep.read_text(encoding="utf-8") == "unrelated"


def test_failed_descriptor_publish_preserves_rename_old_graph_and_all_unrelated_files(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    identity = by_kind(store.resolve_default(), "source")["id"]
    document = store.resolve_document(identity)["documents"][0]["data"]
    document["name"] = "Renamed source"
    keep = store.root / "data/sources/unrelated.txt"
    keep.write_text("keep", encoding="utf-8")
    before = file_snapshot(store.root)
    with patch("coat_sim.core.project_store.replace_with_retry", side_effect=PermissionError("fixture")):
        with pytest.raises(ProjectError) as error:
            store.save_document(identity, document)
    assert error.value.code == "io_error"
    assert file_snapshot(store.root) == before
    assert ProjectStore.open(store.project_file).manifest == store.manifest


@pytest.mark.parametrize("concurrent_change", ["add_file", "edit_file", "replace_file", "replace_directory"])
def test_failed_publish_rollback_preserves_concurrent_directory_changes(tmp_path, concurrent_change):
    store = ProjectStore.create(tmp_path / "project", "P")
    identity = store.create_document("source", source("Original"))
    before_manifest = store.manifest
    before_files = file_snapshot(store.root)
    document = store.resolve_document(identity)["documents"][0]["data"]
    document["name"] = "Renamed"
    directory = store.root / "data/sources/r00000002"
    owned = directory / "Renamed.csrc"
    foreign = directory / "user-note.txt"
    preserved = foreign if concurrent_change in {"add_file", "replace_directory"} else owned
    expected_content = b"concurrent user content"

    def concurrent_change_then_failure(*_args, **_kwargs):
        nonlocal expected_content
        assert owned.is_file()
        if concurrent_change == "replace_directory":
            directory.rename(directory.with_name("moved-by-another-process"))
            directory.mkdir()
        if concurrent_change == "replace_file":
            # Identical bytes still belong to the replacing writer's inode.
            expected_content = owned.read_bytes()
            replacement = directory / "replacement.tmp"
            replacement.write_bytes(expected_content)
            replacement.replace(owned)
        else:
            preserved.write_bytes(expected_content)
        raise PermissionError("descriptor temporarily locked")

    with patch("coat_sim.core.project_store.replace_with_retry", side_effect=concurrent_change_then_failure):
        with pytest.raises(ProjectError) as error:
            store.save_document(identity, document)
    assert error.value.code == "io_error"
    assert preserved.read_bytes() == expected_content
    assert store.manifest == before_manifest == ProjectStore.open(store.project_file).manifest
    assert all((store.root / path).read_bytes() == content for path, content in before_files.items())
    assert any(warning["code"] == "publication_rollback_retained" for warning in store.maintenance_warnings)
    if concurrent_change == "add_file":
        assert not owned.exists()  # Still-owned transaction bytes are cleaned up.
        store.save_document(identity, document)
        assert row(store, identity)["path"] == "data/sources/r00000002-2/Renamed.csrc"
        assert foreign.read_bytes() == expected_content


def test_concurrent_revision_destination_is_never_removed_on_failed_publish(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    def race(source_path, target):
        target.mkdir()
        (target / "keep.txt").write_text("concurrent user file", encoding="utf-8")
        _rename_directory_no_replace(source_path, target)
    with patch("coat_sim.core.project_store._rename_directory_no_replace", side_effect=race):
        with pytest.raises(ProjectError):
            store.create_document("source", source("Copper"))
    assert store.manifest["documents"] == []
    assert (store.root / "data/sources/r00000001/keep.txt").read_text() == "concurrent user file"


def test_symlink_category_is_rejected_before_directory_scan_or_publish(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    category = store.root / "data/sources"
    category.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    category.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectError) as error:
        store.create_document("source", source("Copper"))
    assert error.value.code == "unsafe_path"
    assert list(outside.iterdir()) == []


def test_legacy_objects_are_rejected_without_migration_or_deletion(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    manifest = store.manifest
    for record in manifest["documents"]:
        old = store.root / record["path"]
        record["path"] = f"objects/{'a' * 32}/{record['id']}{EXTENSIONS[record['kind']]}"
        target = store.root / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old.read_bytes())
    store.project_file.write_bytes(json_bytes(manifest))
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(store.project_file)
    assert error.value.code == "invalid_manifest"
    assert file_snapshot(store.root) == before


def test_named_physical_path_resolves_and_stable_dependencies_survive_package_and_recovery(tmp_path):
    store, external, _, configuration = setup_project(tmp_path)
    api = ProjectResourceService(store)
    graph = store.resolve_default()
    for record in graph["documents"]:
        assert api.is_managed_path(store.root / record["path"])
    machine = by_kind(graph, "machine")
    for entry in machine["data"]["sources"]:
        assert entry["file"].endswith(".csrc") and len(PurePosixPath(entry["file"]).stem) == 32
    store.record_recovery([configuration])
    recovered = store.recover_session()
    assert recovered["graphs"][0]["runtime"] == graph["runtime"]
    package = tmp_path / "named.cproject"
    export_project(store, package)
    restored = import_project(package, tmp_path / "restored")
    restored_default = restored.resolve_default()
    assert restored_default is not None
    assert restored_default["runtime"] == graph["runtime"]
    assert [r["path"] for r in restored.manifest["documents"]] == [r["path"] for r in store.manifest["documents"]]


def test_manifest_rejects_wrong_category_and_normalized_case_collision(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    first = store.create_document("source", source("Mirror"))
    second = store.create_document("source", source("Other"))
    value = store.manifest
    value["documents"][0]["path"] = "data/masks/r00000001/Mirror.csrc"
    with pytest.raises(ProjectError):
        _manifest(value)
    value = store.manifest
    value["documents"][1]["path"] = value["documents"][0]["path"].replace("Mirror", "ＭＩＲＲＯＲ")
    with pytest.raises(ProjectError) as error:
        _manifest(value)
    assert error.value.code == "dependency_conflict"
    assert first != second


def test_project_information_is_descriptor_only_atomic_and_optional(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    assert "summary" not in store.manifest and "notes" not in store.manifest
    before = store.manifest
    files = file_snapshot(store.root)
    with patch.object(store, "_publish", side_effect=AssertionError("must not rewrite resources")):
        result = store.update_information({"name": " Updated ", "summary": "Independent overview", "notes": "备注"},
                                          expected_revision=before["revision"])
    assert result == {"id": before["id"], "name": "Updated", "summary": "Independent overview", "notes": "备注",
                      "revision": before["revision"] + 1}
    assert store.manifest["documents"] == before["documents"]
    assert store.manifest["default_configuration_id"] == before["default_configuration_id"]
    assert all((store.root / path).read_bytes() == content for path, content in files.items()
               if path != store.project_file.name)
    assert ProjectStore.open(store.project_file).manifest == store.manifest
    descriptor = store.project_file.read_bytes()
    store.update_information({"name": "Updated"}, expected_revision=result["revision"])
    assert store.project_file.read_bytes() == descriptor


@pytest.mark.parametrize("information", [{"name": " "}, {"name": "x" * 101}, {"summary": None},
                                          {"notes": "x" * 20001}, {"unknown": "x"}])
def test_invalid_project_information_preserves_every_byte(tmp_path, information):
    store = ProjectStore.create(tmp_path / "project", "P")
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.update_information(information)
    assert error.value.code == "invalid_manifest"
    assert file_snapshot(store.root) == before


def test_project_information_stale_and_publish_failure_are_safe(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P", summary="overview", notes="notes")
    stale = ProjectStore.open(store.project_file)
    store.update_information({"name": "New"}, expected_revision=0)
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError, match="changed"):
        stale.update_information({"summary": "stale"})
    with pytest.raises(ProjectError, match="changed"):
        store.update_information({"notes": "stale"}, expected_revision=0)
    with patch("coat_sim.core.project_store.replace_with_retry", side_effect=PermissionError("fixture")):
        with pytest.raises(ProjectError) as error:
            store.update_information({"notes": "new"}, expected_revision=1)
    assert error.value.code == "io_error"
    assert file_snapshot(store.root) == before
    assert store.manifest == ProjectStore.open(store.project_file).manifest


def test_resource_catalog_information_is_public_detached_and_read_only(tmp_path):
    from coat_sim.core.film_design import new_design
    from coat_sim.core.materials import default_material_profile
    store = ProjectStore.create(tmp_path / "project", "P")
    source_id = store.create_document("source", {**source("Copper"), "description": "source overview", "notes": "source notes"})
    film = new_design("Film")
    film.update(summary="film overview", note="film notes")
    film_id = store.create_document("film", film)
    profile = default_material_profile("Material", id="profile")
    profile.update(description="material overview", notes="material notes")
    material_id = store.create_document("material_profile", {"kind": "coat_sim.project_material_profile", "version": 1,
                                                              "material": profile})
    api = ProjectResourceService(store)
    before = file_snapshot(store.root)
    with patch("coat_sim.core.project_global_resources.read_global_resources", return_value=store.snapshot_saved_inputs()):
        catalog = api.inspect_resources("global")
    entries = {entry["id"]: entry for entry in catalog["rows"]}
    assert entries[source_id]["summary"] == "source overview" and entries[source_id]["notes"] == "source notes"
    assert entries[film_id]["summary"] == "film overview" and entries[film_id]["notes"] == "film notes"
    assert entries[material_id]["summary"] == "material overview" and entries[material_id]["notes"] == "material notes"
    assert all(entry["read_only"] is True for entry in entries.values())
    entries[source_id]["notes"] = "detached edit"
    assert api.inspect_resources()["rows"][0]["notes"] == "source notes"
    assert file_snapshot(store.root) == before


def test_invalid_utf8_name_is_a_typed_failure_without_mutation(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.create_document("source", source("bad\ud800name"))
    assert error.value.code in {"invalid_document", "invalid_json"}
    assert file_snapshot(store.root) == before
