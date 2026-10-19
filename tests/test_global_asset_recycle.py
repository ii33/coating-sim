"""Only disposable, self-made global assets exercise reversible deletion."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sqlite3

import pytest

from coat_sim.core import data_files, material_business, project_global_resources as resources
from coat_sim.core import global_asset_recycle as recycle
from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_store import FilmDesignStore
from coat_sim.core.film_expression_store import FilmExpressionTemplateStore
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.core.project_dependencies import ProjectError, json_bytes

pytestmark = pytest.mark.contract


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir()
    monkeypatch.setattr(data_files, "PROJECT_ROOT", root)
    monkeypatch.setattr(data_files, "DATA_ROOT", root / "data")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [])
    return root


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))
    return path


def source(root, name="fixture.csrc"):
    return write(root / "data/sources" / name, {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1.0})


def profile(identity="test_material"):
    value = default_material_profile("Synthetic fixture", id=identity)
    value["notes"] = "Self-made deletion test only"
    value["optical_constants"] = [{"id": "test_set", "name": "Synthetic constant", "kind": "tabulated",
        "wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 700,
        "wavelengths": [400, 700], "n": [1.5, 1.5], "k": [0, 0], "source": "Synthetic fixture", "notes": ""}]
    return value


def id_for(snapshot, kind):
    return next(row["id"] for row in snapshot["manifest"]["documents"] if row["kind"] == kind)


def delete_kind(kind):
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, kind)])
    return snapshot, plan, recycle.apply_global_deletion(snapshot, plan)


def test_typed_exact_bytes_roundtrip_survives_module_restart(isolated):
    path = source(isolated)
    path.write_bytes(b"  " + path.read_bytes() + b"\n\n")
    original = path.read_bytes()
    before, plan, receipt = delete_kind("source")
    assert plan["blockers"] == []
    assert not path.exists()
    assert resources.read_global_resources()["documents"] == {}
    assert receipt["scope"] == "global"
    assert receipt["owner_root"] == str(isolated / "data")
    assert receipt["state"] == "active"
    importlib.reload(recycle)
    assert recycle.list_global_deletions() == [receipt]
    restored = recycle.restore_global_deletion(receipt["receipt_id"])
    assert restored["state"] == "restored"
    assert path.read_bytes() == original
    assert resources.read_global_resources() == before
    with pytest.raises(ProjectError, match="no longer active"):
        recycle.restore_global_deletion(receipt["receipt_id"])


def test_archive_directory_entries_are_synced_before_original_rename(isolated, monkeypatch):
    path = source(isolated)
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "source")])
    synced: list[Path] = []
    rename = recycle._rename_directory_no_replace

    def checked_rename(original, archive):
        assert isolated / "data" in synced
        assert archive.parent.parent in synced and original.parent in synced
        assert archive.parent.parent == path.parent / ".asset-recycle"
        return rename(original, archive)

    monkeypatch.setattr(recycle, "_fsync_directory", synced.append)
    monkeypatch.setattr(recycle, "_rename_directory_no_replace", checked_rename)
    receipt = recycle.apply_global_deletion(snapshot, plan)
    assert receipt["state"] == "active" and not path.exists()


def test_archive_directory_fsync_failure_keeps_original(isolated, monkeypatch):
    path = source(isolated)
    original = path.read_bytes()
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "source")])

    def fail_archive_parent(directory):
        if directory == path.parent / ".asset-recycle":
            raise OSError("Injected archive directory persistence failure")

    monkeypatch.setattr(recycle, "_fsync_directory", fail_archive_parent)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "global_library_error"
    assert path.read_bytes() == original
    assert resources.read_global_resources() == snapshot


def test_incoming_reference_blocks_without_cascade(isolated):
    source(isolated)
    write(isolated / "data/machines/rig.cmac", {"schema_version": 5, "carrier_x_mm": 0, "carrier_y_mm": 0,
        "planet_offset_mm": 0, "planet_tilt_deg": 0, "substrate_z_mm": 100, "sun_teeth": 120,
        "planet_teeth": 36, "gear_module_mm": 0,
        "sources": [{"file": "../sources/fixture.csrc", "x_mm": 0, "y_mm": 0, "z_mm": 0}], "masks": [], "monitors": []})
    before = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(before, [id_for(before, "source")])
    assert plan["affected_ids"] == [id_for(before, "source")]
    assert plan["blockers"] == [{"code": "incoming_dependency", "id": id_for(before, "machine"),
                                 "affected_ids": [id_for(before, "source")]}]
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(before, plan)
    assert error.value.code == "deletion_blocked"
    assert resources.read_global_resources() == before


def test_stale_semantically_identical_file_change_is_rejected(isolated):
    path = source(isolated)
    before = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(before, [id_for(before, "source")])
    path.write_bytes(b"\n" + path.read_bytes())
    assert resources.read_global_resources() == before
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(before, plan)
    assert error.value.code == "stale_source"
    assert path.exists()


@pytest.mark.parametrize("mutation", ["source", "blockers", "backend", "selected_ids"])
def test_selection_tampering_never_authorizes_arbitrary_mutation(isolated, mutation):
    path = source(isolated)
    before = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(before, [id_for(before, "source")])
    plan[mutation] = {"source": "../external.csrc", "blockers": [{"code": "fake"}],
                      "backend": "material_file", "selected_ids": ["f" * 32]}[mutation]
    with pytest.raises(ProjectError):
        recycle.apply_global_deletion(before, plan)
    assert path.exists()


def test_two_distinct_assets_are_rejected(isolated):
    source(isolated)
    source(isolated, "another.csrc")
    snapshot = resources.read_global_resources()
    with pytest.raises(ProjectError) as error:
        recycle.prepare_global_deletion(snapshot, list(snapshot["documents"]))
    assert error.value.code == "invalid_selection"


def test_custom_material_paired_projection_exact_noncanonical_filename(isolated):
    path = write(isolated / "data/materials/noncanonical-name.json", profile())
    original = path.read_bytes()
    snapshot = resources.read_global_resources()
    selected = [id_for(snapshot, "optical_material")]
    plan = recycle.prepare_global_deletion(snapshot, selected)
    assert set(plan["affected_ids"]) == set(snapshot["documents"])
    assert plan["action"] == "delete_asset"
    receipt = recycle.apply_global_deletion(snapshot, plan)
    assert not path.exists()
    assert resources.read_global_resources()["documents"] == {}
    recycle.restore_global_deletion(receipt["receipt_id"])
    assert path.read_bytes() == original
    assert resources.read_global_resources() == snapshot


def test_builtin_seed_is_tombstoned_and_restores(isolated, monkeypatch):
    original = profile("builtin_test")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [deepcopy(original)])
    library = MaterialLibrary(directory=isolated / "data/materials")
    snapshot = resources.read_global_resources()
    selected = id_for(snapshot, "material_profile")
    plan = recycle.prepare_global_deletion(snapshot, [selected])
    assert plan["action"] == "delete_builtin"
    assert plan["backend"] == "material_tombstone"
    assert plan["blockers"] == []
    assert len(plan["affected_ids"]) == 2
    receipt = recycle.apply_global_deletion(snapshot, plan)
    marker = library.deleted_directory / "builtin_test.json"
    assert marker.exists()
    assert resources.read_global_resources()["documents"] == {}
    # A seed reload (new process / upgraded catalogue) still honours the marker.
    reloaded = MaterialLibrary(directory=isolated / "data/materials")
    with pytest.raises(ValueError, match="builtin_material_deleted"):
        reloaded.read("builtin_test")
    assert reloaded.read_builtin_original("builtin_test")["name"] == original["name"]
    restored = recycle.restore_global_deletion(receipt["receipt_id"])
    assert restored["state"] == "restored"
    assert not marker.exists()
    assert resources.read_global_resources() == snapshot


def test_builtin_override_is_tombstoned_with_the_override_archived(isolated, monkeypatch):
    original = profile("builtin_test")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [deepcopy(original)])
    library = MaterialLibrary(directory=isolated / "data/materials")
    override = deepcopy(original)
    override["name"] = "User-edited synthetic builtin"
    library.save_builtin_override(override)
    path = library.override_directory / "builtin_test.json"
    exact = path.read_bytes()
    snapshot = resources.read_global_resources()
    selected = id_for(snapshot, "material_profile")
    plan = recycle.prepare_global_deletion(snapshot, [selected])
    assert plan["action"] == "delete_builtin"
    assert plan["backend"] == "material_tombstone"
    assert plan["blockers"] == []
    receipt = recycle.apply_global_deletion(snapshot, plan)
    assert not path.exists()
    assert (library.deleted_directory / "builtin_test.json").exists()
    assert resources.read_global_resources()["documents"] == {}
    # An edit to the deleted builtin is refused until it is explicitly restored.
    with pytest.raises(ValueError, match="builtin_material_deleted"):
        library.save_builtin_override(override)
    recycle.restore_global_deletion(receipt["receipt_id"])
    assert path.read_bytes() == exact
    assert resources.read_global_resources() == snapshot


def test_prepared_tombstone_without_override_rolls_back_after_interruption(isolated, monkeypatch):
    """A crash before delete_builtin leaves a readable, recoverable receipt."""
    original = profile("builtin_test")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [deepcopy(original)])
    library = MaterialLibrary(directory=isolated / "data/materials")
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "material_profile")])
    assert plan["tombstone_sha256"] == material_business.deleted_builtin_marker_sha256("builtin_test")

    def die_before_tombstone(_self, _identifier):
        raise KeyboardInterrupt("simulated process death before delete_builtin")

    with monkeypatch.context() as crash:
        crash.setattr(MaterialLibrary, "delete_builtin", die_before_tombstone)
        with pytest.raises(KeyboardInterrupt):
            recycle.apply_global_deletion(snapshot, plan)
    assert not (library.deleted_directory / "builtin_test.json").exists()
    importlib.reload(recycle)
    summary = recycle.list_global_deletions()
    assert summary[0]["state"] == "prepared"
    assert resources.read_global_resources() == snapshot
    result = recycle.restore_global_deletion(summary[0]["receipt_id"])
    assert result["state"] == "rolled_back"
    assert recycle.list_global_deletions()[0]["state"] == "rolled_back"
    assert resources.read_global_resources() == snapshot
    assert library.read("builtin_test")["record"]["name"] == original["name"]
    with pytest.raises(ProjectError, match="no longer active"):
        recycle.restore_global_deletion(summary[0]["receipt_id"])


def test_prepared_tombstone_with_archived_override_rolls_back_after_interruption(isolated, monkeypatch):
    """A crash after the override move but before delete_builtin is recovered."""
    original = profile("builtin_test")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [deepcopy(original)])
    library = MaterialLibrary(directory=isolated / "data/materials")
    override = deepcopy(original)
    override["name"] = "User-edited synthetic builtin"
    library.save_builtin_override(override)
    path = library.override_directory / "builtin_test.json"
    exact = path.read_bytes()
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "material_profile")])

    def die_before_tombstone(_self, _identifier):
        raise KeyboardInterrupt("simulated process death after the override move")

    with monkeypatch.context() as crash:
        crash.setattr(MaterialLibrary, "delete_builtin", die_before_tombstone)
        with pytest.raises(KeyboardInterrupt):
            recycle.apply_global_deletion(snapshot, plan)
    assert not path.exists()
    assert not (library.deleted_directory / "builtin_test.json").exists()
    importlib.reload(recycle)
    summary = recycle.list_global_deletions()
    assert summary[0]["state"] == "prepared"
    # The override sits in the archive, so the live library still shows the seed.
    assert resources.read_global_resources() != snapshot
    result = recycle.restore_global_deletion(summary[0]["receipt_id"])
    assert result["state"] == "rolled_back"
    assert path.read_bytes() == exact
    assert resources.read_global_resources() == snapshot
    assert recycle.list_global_deletions()[0]["state"] == "rolled_back"


def test_prepared_tombstone_before_override_move_rolls_back_after_interruption(isolated, monkeypatch):
    """A crash after the prepared write but before the rename never overwrites."""
    original = profile("builtin_test")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [deepcopy(original)])
    library = MaterialLibrary(directory=isolated / "data/materials")
    override = deepcopy(original)
    override["name"] = "User-edited synthetic builtin"
    library.save_builtin_override(override)
    path = library.override_directory / "builtin_test.json"
    exact = path.read_bytes()
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "material_profile")])

    def die_before_rename(_original, _archive):
        raise KeyboardInterrupt("simulated process death before the override rename")

    with monkeypatch.context() as crash:
        crash.setattr(recycle, "_rename_directory_no_replace", die_before_rename)
        with pytest.raises(KeyboardInterrupt):
            recycle.apply_global_deletion(snapshot, plan)
    assert path.read_bytes() == exact
    assert not (library.deleted_directory / "builtin_test.json").exists()
    importlib.reload(recycle)
    summary = recycle.list_global_deletions()
    assert summary[0]["state"] == "prepared"
    assert resources.read_global_resources() == snapshot
    result = recycle.restore_global_deletion(summary[0]["receipt_id"])
    assert result["state"] == "rolled_back"
    assert path.read_bytes() == exact
    assert resources.read_global_resources() == snapshot


def test_referenced_builtin_override_is_blocked(isolated, monkeypatch):
    original = profile("builtin_test")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [deepcopy(original)])
    library = MaterialLibrary(directory=isolated / "data/materials")
    library.save_builtin_override({**original, "name": "Edited fixture"})
    film = new_design("Synthetic film", "synthetic_film")
    film["substrate"].update(material_id="builtin_test", optical_set_id="test_set")
    FilmDesignStore(isolated / "data/film_design/library.sqlite3").save(film)
    before = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(before, [id_for(before, "material_profile")])
    assert any(row["code"] == "incoming_dependency" for row in plan["blockers"])
    with pytest.raises(ProjectError):
        recycle.apply_global_deletion(before, plan)
    assert resources.read_global_resources() == before


@pytest.mark.parametrize("kind", ["film", "expression_template"])
def test_sqlite_exact_row_archive_delete_restore_transaction(isolated, kind):
    library = isolated / "data/film_design/library.sqlite3"
    if kind == "film":
        FilmDesignStore(library).save(new_design("Synthetic film", "synthetic_film"))
        table = "film_designs"
    else:
        write(isolated / "data/materials/test_material.json", profile())
        FilmExpressionTemplateStore(library).save({"template_id": "synthetic_expression", "name": "Synthetic expression",
            "expression": {"text": "A", "symbols": {"A": {"material_id": "test_material", "optical_set_id": "test_set",
                "thickness": {"value": 1, "unit": "nm"}}}}})
        table = "film_expression_templates"
    with sqlite3.connect(library) as connection:
        original = connection.execute(f"SELECT * FROM {table}").fetchone()
    before, _, receipt = delete_kind(kind)
    with sqlite3.connect(library) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == []
        stored = json.loads(connection.execute("SELECT receipt_json FROM global_asset_recycle WHERE receipt_id=?", (receipt["receipt_id"],)).fetchone()[0])
        assert stored["row"]["values"] == list(original)
        assert stored["state"] == "active"
    # Simulate interruption before the sidecar mirror was updated after commit.
    sidecar = isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json"
    metadata = json.loads(sidecar.read_text())
    metadata["state"] = "prepared"
    sidecar.write_text(json.dumps(metadata))
    importlib.reload(recycle)
    assert recycle.list_global_deletions()[0]["state"] == "active"
    recycle.restore_global_deletion(receipt["receipt_id"])
    with sqlite3.connect(library) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchone() == original
    assert resources.read_global_resources() == before


def test_restore_never_overwrites_new_file_or_new_material_id(isolated):
    path = source(isolated)
    _, _, receipt = delete_kind("source")
    replacement = source(isolated)
    exact = replacement.read_bytes()
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "restore_conflict"
    assert path.read_bytes() == exact
    assert recycle.list_global_deletions()[0]["state"] == "active"


def test_restore_rejects_absent_dependencies_without_cascade(isolated):
    source(isolated)
    write(isolated / "data/machines/rig.cmac", {"schema_version": 5, "carrier_x_mm": 0, "carrier_y_mm": 0,
        "planet_offset_mm": 0, "planet_tilt_deg": 0, "substrate_z_mm": 100, "sun_teeth": 120,
        "planet_teeth": 36, "gear_module_mm": 0,
        "sources": [{"file": "../sources/fixture.csrc", "x_mm": 0, "y_mm": 0, "z_mm": 0}], "masks": [], "monitors": []})
    _, _, machine_receipt = delete_kind("machine")
    _, _, source_receipt = delete_kind("source")
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(machine_receipt["receipt_id"])
    assert error.value.code == "missing_dependency"
    assert resources.read_global_resources()["documents"] == {}
    recycle.restore_global_deletion(source_receipt["receipt_id"])
    recycle.restore_global_deletion(machine_receipt["receipt_id"])
    assert len(resources.read_global_resources()["documents"]) == 2


def test_symbolic_link_in_archive_or_source_is_rejected(isolated, tmp_path):
    path = source(isolated)
    _, _, receipt = delete_kind("source")
    metadata = json.loads((isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json").read_text())
    archive = isolated / "data" / metadata["archive"]
    held = archive.with_name("preserved-original")
    archive.rename(held)
    external = tmp_path / "external"
    external.write_bytes(b"must not touch")
    archive.symlink_to(external)
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "unsafe_path"
    assert external.read_bytes() == b"must not touch"
    assert held.exists() and not path.exists()


def test_apply_failure_rolls_original_back_and_reports_no_success(isolated, monkeypatch):
    path = source(isolated)
    original = path.read_bytes()
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "source")])
    actual = resources.read_global_resources
    def interrupt_after_move():
        if not path.exists():
            raise ProjectError("test_interruption", "Injected post-deletion read failure")
        return actual()
    monkeypatch.setattr(resources, "read_global_resources", interrupt_after_move)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "test_interruption"
    assert path.read_bytes() == original
    assert recycle.list_global_deletions()[0]["state"] == "rolled_back"


def test_rollback_failure_reports_uncertain_with_recoverable_path(isolated, monkeypatch):
    path = source(isolated)
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "source")])
    actual = resources.read_global_resources
    def create_collision_after_move():
        if not path.exists():
            source(isolated)
            raise ProjectError("test_interruption", "Injected conflicting recreation")
        return actual()
    monkeypatch.setattr(resources, "read_global_resources", create_collision_after_move)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "deletion_uncertain"
    assert isinstance(error.value, recycle.GlobalDeletionUncertain)
    assert error.value.uncertain
    assert error.value.recovery_path
    summary = recycle.list_global_deletions()[0]
    assert summary["state"] == "uncertain"
    assert path.exists()
    assert any((isolated / "data/sources/.asset-recycle").rglob("payload"))


def test_receipt_path_tampering_does_not_escape_global_scope(isolated, tmp_path):
    source(isolated)
    _, _, receipt = delete_kind("source")
    sidecar = isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json"
    metadata = json.loads(sidecar.read_text())
    external = tmp_path / "external.csrc"
    metadata["source"] = str(external)
    sidecar.write_text(json.dumps(metadata))
    with pytest.raises(ProjectError):
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert not external.exists()


def test_global_binary_payload_ownership_is_explicitly_blocked(isolated):
    import hashlib
    asset_id, generation = "a" * 32, "b" * 32
    folder = isolated / "data/raytrace"
    payload = folder / "assets" / generation / (asset_id + ".csv")
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"500,1\n600,2\n")
    write(folder / "input.casset", {"kind": "coat_sim.project_binary_asset", "version": 1,
        "role": "spd_csv", "payload_path": f"assets/{generation}/{asset_id}.csv",
        "size": payload.stat().st_size, "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()})
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "binary_asset")])
    assert plan["blockers"] == [{"code": "unsupported_payload_ownership", "id": id_for(snapshot, "binary_asset")}]
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "deletion_blocked"
    assert payload.read_bytes() == b"500,1\n600,2\n"


def test_archived_original_tampering_is_preserved_and_rejected(isolated):
    path = source(isolated)
    _, _, receipt = delete_kind("source")
    metadata = json.loads((isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json").read_text())
    archive = isolated / "data" / metadata["archive"]
    archive.write_bytes(b"preserve these changed archive bytes")
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "integrity_error"
    assert archive.read_bytes() == b"preserve these changed archive bytes"
    assert not path.exists()


def test_sql_row_metadata_change_stales_plan_even_when_snapshot_unchanged(isolated):
    library = isolated / "data/film_design/library.sqlite3"
    FilmDesignStore(library).save(new_design("Synthetic film", "synthetic_film"))
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "film")])
    with sqlite3.connect(library) as connection:
        connection.execute("UPDATE film_designs SET updated_at='changed-metadata'")
    assert resources.read_global_resources() == snapshot
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "stale_source"
    assert FilmDesignStore(library).load("synthetic_film")["name"] == "Synthetic film"


def test_sql_restore_never_overwrites_recreated_row(isolated):
    library = isolated / "data/film_design/library.sqlite3"
    store = FilmDesignStore(library)
    store.save(new_design("Synthetic film", "synthetic_film"))
    _, _, receipt = delete_kind("film")
    store.save(new_design("Replacement synthetic film", "synthetic_film"))
    with sqlite3.connect(library) as connection:
        replacement = connection.execute("SELECT * FROM film_designs").fetchone()
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "restore_conflict"
    with sqlite3.connect(library) as connection:
        assert connection.execute("SELECT * FROM film_designs").fetchone() == replacement
        assert connection.execute("SELECT COUNT(*) FROM global_asset_recycle").fetchone()[0] == 1


def test_sql_archive_failure_rolls_back_row_and_archive_together(isolated, monkeypatch):
    library = isolated / "data/film_design/library.sqlite3"
    FilmDesignStore(library).save(new_design("Synthetic film", "synthetic_film"))
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "film")])
    with sqlite3.connect(library) as connection:
        original = connection.execute("SELECT * FROM film_designs").fetchone()
    actual = recycle._archive_sql
    def fail_after_archive(connection, receipt):
        actual(connection, receipt)
        raise OSError("Injected failure after transaction archive write")
    monkeypatch.setattr(recycle, "_archive_sql", fail_after_archive)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "global_library_error"
    with sqlite3.connect(library) as connection:
        assert connection.execute("SELECT * FROM film_designs").fetchone() == original
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='global_asset_recycle'").fetchone() is None
    assert recycle.list_global_deletions()[0]["state"] == "rolled_back"


def test_sql_rollback_collision_is_uncertain_and_original_row_stays_archived(isolated, monkeypatch):
    library = isolated / "data/film_design/library.sqlite3"
    store = FilmDesignStore(library)
    store.save(new_design("Synthetic film", "synthetic_film"))
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "film")])
    actual = resources.read_global_resources
    def collide_after_commit():
        with sqlite3.connect(library) as connection:
            present = connection.execute("SELECT 1 FROM film_designs").fetchone()
        if present is None:
            store.save(new_design("Concurrent replacement", "synthetic_film"))
            raise ProjectError("test_interruption", "Injected recreation after commit")
        return actual()
    monkeypatch.setattr(resources, "read_global_resources", collide_after_commit)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "deletion_uncertain"
    assert store.load("synthetic_film")["name"] == "Concurrent replacement"
    assert recycle.list_global_deletions()[0]["state"] == "uncertain"
    with sqlite3.connect(library) as connection:
        archived = json.loads(connection.execute("SELECT receipt_json FROM global_asset_recycle").fetchone()[0])
    assert archived["row"]["values"][1] == "Synthetic film"


def test_restore_postvalidation_failure_returns_file_to_archive(isolated, monkeypatch):
    path = source(isolated)
    original = path.read_bytes()
    _, _, receipt = delete_kind("source")
    actual = resources.read_global_resources
    def fail_once_restored():
        if path.exists():
            raise ProjectError("test_interruption", "Injected restore postvalidation failure")
        return actual()
    monkeypatch.setattr(resources, "read_global_resources", fail_once_restored)
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "test_interruption"
    assert not path.exists()
    assert recycle.list_global_deletions()[0]["state"] == "active"
    monkeypatch.setattr(resources, "read_global_resources", actual)
    recycle.restore_global_deletion(receipt["receipt_id"])
    assert path.read_bytes() == original


def test_prepared_file_receipt_after_interrupted_activation_restores(isolated):
    path = source(isolated)
    original = path.read_bytes()
    _, _, receipt = delete_kind("source")
    sidecar = isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json"
    metadata = json.loads(sidecar.read_text())
    metadata["state"] = "prepared"
    sidecar.write_text(json.dumps(metadata))
    importlib.reload(recycle)
    assert recycle.list_global_deletions()[0]["state"] == "prepared"
    recycle.restore_global_deletion(receipt["receipt_id"])
    assert path.read_bytes() == original


def test_unknown_ids_and_foreign_snapshot_are_rejected(isolated):
    path = source(isolated)
    snapshot = resources.read_global_resources()
    with pytest.raises(ProjectError) as error:
        recycle.prepare_global_deletion(snapshot, ["f" * 32])
    assert error.value.code == "invalid_selection"
    foreign = deepcopy(snapshot)
    foreign["manifest"]["id"] = "f" * 32
    with pytest.raises(ProjectError) as error:
        recycle.prepare_global_deletion(foreign, list(snapshot["documents"]))
    assert error.value.code == "stale_source"
    assert path.exists()


def test_reserved_recycle_subtree_is_not_projected_or_counted(isolated, monkeypatch):
    path = source(isolated)
    snapshot = resources.read_global_resources()
    hidden = path.parent / ".asset-recycle" / ("d" * 32)
    write(hidden / "must-not-be-a-resource.csrc", json.loads(path.read_text()))
    monkeypatch.setattr(resources, "MAX_SCAN_ENTRIES", 3)
    assert resources.read_global_resources() == snapshot
    _, _, receipt = delete_kind("source")
    assert resources.read_global_resources()["documents"] == {}
    recycle.restore_global_deletion(receipt["receipt_id"])
    assert resources.read_global_resources() == snapshot


def test_sqlite_journal_link_is_rejected_before_any_mutation(isolated, tmp_path):
    library = isolated / "data/film_design/library.sqlite3"
    FilmDesignStore(library).save(new_design("Synthetic film", "synthetic_film"))
    snapshot = resources.read_global_resources()
    plan = recycle.prepare_global_deletion(snapshot, [id_for(snapshot, "film")])
    external = tmp_path / "external-journal"
    external.write_bytes(b"untouched")
    library.with_name(library.name + "-journal").symlink_to(external)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "unsafe_path"
    assert external.read_bytes() == b"untouched"


def test_restart_finishes_interrupted_file_restore_acknowledgement(isolated):
    path = source(isolated)
    original = path.read_bytes()
    _, _, receipt = delete_kind("source")
    sidecar = isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json"
    metadata = json.loads(sidecar.read_text())
    metadata["state"] = "restoring"
    sidecar.write_text(json.dumps(metadata))
    (isolated / "data" / metadata["archive"]).rename(path)
    importlib.reload(recycle)
    result = recycle.restore_global_deletion(receipt["receipt_id"])
    assert result["state"] == "restored"
    assert path.read_bytes() == original
    assert recycle.list_global_deletions()[0]["state"] == "restored"


def _synthetic_sql_pair(isolated):
    database = isolated / "data/film_design/library.sqlite3"
    store = FilmDesignStore(database)
    store.save(new_design("Synthetic selected", "selected"))
    store.save(new_design("Synthetic bystander", "bystander"))
    snapshot = resources.read_global_resources()
    selected = next(row["id"] for row in snapshot["manifest"]["documents"]
                    if snapshot["documents"][row["id"]]["id"] == "selected")
    return database, snapshot, [selected]


def _synthetic_trigger(database, event, table="film_designs"):
    with sqlite3.connect(database) as connection:
        condition = (" WHEN " + ("old" if event == "DELETE" else "new") + ".design_id='selected'"
                     if table == "film_designs" else "")
        connection.execute(f"CREATE TRIGGER synthetic_side_effect AFTER {event} ON {table}{condition} "
                           "BEGIN DELETE FROM film_designs WHERE design_id='bystander'; END")


def _business_rows(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT * FROM film_designs ORDER BY design_id").fetchall()


@pytest.mark.parametrize("event", ["DELETE", "INSERT"])
def test_sql_trigger_schema_is_rejected_during_prepare(isolated, event):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    original = _business_rows(database)
    _synthetic_trigger(database, event)
    with pytest.raises(ProjectError) as error:
        recycle.prepare_global_deletion(snapshot, selected)
    assert error.value.code == "unsupported_schema"
    assert _business_rows(database) == original
    assert recycle.list_global_deletions() == []


def test_sql_delete_trigger_added_after_preview_cannot_delete_bystander(isolated):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    plan = recycle.prepare_global_deletion(snapshot, selected)
    original = _business_rows(database)
    _synthetic_trigger(database, "DELETE")
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "unsupported_schema"
    assert _business_rows(database) == original
    assert not any(row["state"] == "active" for row in recycle.list_global_deletions())


@pytest.mark.parametrize("event,table", [("INSERT", "film_designs"), ("UPDATE", "global_asset_recycle")])
def test_sql_restore_trigger_cannot_delete_bystander_or_change_receipt(isolated, event, table):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    receipt = recycle.apply_global_deletion(snapshot, recycle.prepare_global_deletion(snapshot, selected))
    original = _business_rows(database)
    with sqlite3.connect(database) as connection:
        archived = connection.execute("SELECT receipt_json FROM global_asset_recycle").fetchone()[0]
    _synthetic_trigger(database, event, table)
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "unsupported_schema"
    assert _business_rows(database) == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT receipt_json FROM global_asset_recycle").fetchone()[0] == archived
    assert recycle.list_global_deletions()[0]["state"] == "active"


def test_sql_archive_insert_trigger_blocks_without_business_mutation(isolated):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    plan = recycle.prepare_global_deletion(snapshot, selected)
    original = _business_rows(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE global_asset_recycle (receipt_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)")
    _synthetic_trigger(database, "INSERT", "global_asset_recycle")
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "unsupported_schema"
    assert _business_rows(database) == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM global_asset_recycle").fetchone()[0] == 0
    assert not any(row["state"] == "active" for row in recycle.list_global_deletions())


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_sql_trigger_added_after_connection_is_rejected_inside_transaction(isolated, monkeypatch, operation):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    plan = recycle.prepare_global_deletion(snapshot, selected)
    receipt = recycle.apply_global_deletion(snapshot, plan) if operation == "restore" else None
    original = _business_rows(database)
    actual_connect = recycle._connect_sql
    inserted = False
    def connect_then_change_schema(*, writable=False):
        nonlocal inserted
        connection = actual_connect(writable=writable)
        if writable and not inserted:
            inserted = True
            _synthetic_trigger(database, "DELETE" if operation == "apply" else "INSERT")
        return connection
    monkeypatch.setattr(recycle, "_connect_sql", connect_then_change_schema)
    with pytest.raises(ProjectError) as error:
        if operation == "apply":
            recycle.apply_global_deletion(snapshot, plan)
        else:
            assert receipt is not None
            recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "unsupported_schema"
    assert inserted
    assert _business_rows(database) == original
    receipts = recycle.list_global_deletions()
    if operation == "apply":
        assert not any(row["state"] == "active" for row in receipts)
    else:
        assert receipts[0]["state"] == "active"


def test_sql_trigger_added_before_compensation_cannot_mutate_bystander(isolated, monkeypatch):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    plan = recycle.prepare_global_deletion(snapshot, selected)
    original = _business_rows(database)
    actual_read = resources.read_global_resources
    inserted = False
    def fail_after_commit_with_insert_trigger():
        nonlocal inserted
        rows = _business_rows(database)
        if not inserted and not any(row[0] == "selected" for row in rows):
            inserted = True
            _synthetic_trigger(database, "INSERT")
            raise ProjectError("test_interruption", "Injected trigger before compensation")
        return actual_read()
    monkeypatch.setattr(resources, "read_global_resources", fail_after_commit_with_insert_trigger)
    with pytest.raises(ProjectError) as error:
        recycle.apply_global_deletion(snapshot, plan)
    assert error.value.code == "deletion_uncertain"
    assert isinstance(error.value, recycle.GlobalDeletionUncertain)
    assert error.value.uncertain and error.value.recovery_path
    assert _business_rows(database) == [row for row in original if row[0] == "bystander"]
    with sqlite3.connect(database) as connection:
        archived = json.loads(connection.execute("SELECT receipt_json FROM global_asset_recycle").fetchone()[0])
    assert archived["row"]["values"] == list(next(row for row in original if row[0] == "selected"))
    assert archived["state"] == "active"  # Even the SQL warning UPDATE was refused.
    importlib.reload(recycle)
    summary = recycle.list_global_deletions()[0]
    assert summary["state"] == "uncertain"
    assert summary["recoverable"] is False
    assert recycle._load_receipt(summary["receipt_id"])["state"] == "uncertain"
    assert _business_rows(database) == [row for row in original if row[0] == "bystander"]


def test_sql_expression_table_trigger_also_blocks_film_mutation(isolated):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    original = _business_rows(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE film_expression_templates (template_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                           "note TEXT NOT NULL, schema_version INTEGER NOT NULL, expression_json TEXT NOT NULL, "
                           "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    _synthetic_trigger(database, "DELETE", "film_expression_templates")
    with pytest.raises(ProjectError) as error:
        recycle.prepare_global_deletion(snapshot, selected)
    assert error.value.code == "unsupported_schema"
    assert _business_rows(database) == original


def test_uncertain_sidecar_cannot_override_different_sql_archive_content(isolated):
    database, snapshot, selected = _synthetic_sql_pair(isolated)
    receipt = recycle.apply_global_deletion(snapshot, recycle.prepare_global_deletion(snapshot, selected))
    sidecar = isolated / "data/.asset-recycle" / receipt["receipt_id"] / "receipt.json"
    metadata = json.loads(sidecar.read_text())
    metadata["state"] = "uncertain"
    metadata["row"]["values"][1] = "Tampered synthetic archived name"
    sidecar.write_text(json.dumps(metadata))
    original = _business_rows(database)
    with pytest.raises(ProjectError) as error:
        recycle.list_global_deletions()
    assert error.value.code == "invalid_receipt"
    with pytest.raises(ProjectError) as error:
        recycle.restore_global_deletion(receipt["receipt_id"])
    assert error.value.code == "invalid_receipt"
    assert _business_rows(database) == original
