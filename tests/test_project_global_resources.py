"""Global exchange boundaries, stable read snapshots, and real publication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from coat_sim.core import data_files, material_business, project_global_resources as global_resources
from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_store import FilmDesignStore
from coat_sim.core.film_expression_store import FilmExpressionTemplateStore
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.core.project_dependencies import EXTENSIONS, ProjectError, json_bytes, normalize_document
from coat_sim.core.project_store import ProjectStore

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


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def typed_graph(root):
    data = root / "data"
    source = {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1.0}
    substrate = {"schema_version": 1, "name": "Disk", "outline": {"kind": "circle", "radius_mm": 10},
                 "surface": {"kind": "flat"}}
    mask = {"z_mm": 50, "outer": [[-5, -5], [5, -5], [5, 5], [-5, 5]], "holes": []}
    machine = {"schema_version": 5, "carrier_x_mm": 0, "carrier_y_mm": 0, "planet_offset_mm": 0,
               "planet_tilt_deg": 0, "substrate_z_mm": 100, "sun_teeth": 120, "planet_teeth": 36,
               "gear_module_mm": 0, "sources": [{"file": "../sources/lamp.csrc", "x_mm": 0, "y_mm": 0, "z_mm": 0}],
               "masks": [{"file": "../masks/board.cmsk"}], "monitors": []}
    process = {"schema_version": 7, "name": "Recipe", "machine_file": "../machines/rig.cmac",
               "substrate_file": "../substrates/disk.csub", "steps": [{"source_slot": 1, "target_nm": 1,
               "rate_nm_s": 1, "target_mode": "mean", "mask_slots": []}]}
    for path, value in (("sources/lamp.csrc", source), ("substrates/disk.csub", substrate),
                        ("masks/board.cmsk", mask), ("machines/rig.cmac", machine), ("processes/recipe.cproc", process)):
        write(data / path, value)
    return process


def optical_material(identity="glass", n=1.5):
    return {"kind": "coat_sim.project_optical_material", "version": 1, "material": {
        "id": identity, "name": "Glass", "notes": "fixture", "optical_sets": [{
            "id": "set", "name": "Constant", "kind": "tabulated", "wavelength_unit": "nm",
            "wavelength_min": 400, "wavelength_max": 700, "wavelengths": [400, 700],
            "n": [n, n], "k": [0, 0], "source": "Synthetic test", "notes": "fixture"}]}}


def profile(identity="glass"):
    value = default_material_profile("Glass", id=identity)
    value["notes"] = "fixture"
    value["description"] = "Preserve full process fields"
    value["optical_constants"] = optical_material(identity)["material"]["optical_sets"]
    return value


def after_snapshot(before, additions=(), updates=None):
    value = deepcopy(before)
    rows = {row["id"]: row for row in value["manifest"]["documents"]}
    created = []
    for kind, data in additions:
        identity = uuid.uuid4().hex
        rows[identity] = {"id": identity, "kind": kind, "path": "", "sha256": "", "dependencies": []}
        value["documents"][identity] = normalize_document(kind, data)
        created.append(identity)
    for identity, data in (updates or {}).items():
        value["documents"][identity] = normalize_document(rows[identity]["kind"], data)
    for identity, row in rows.items():
        row["path"] = "objects/" + "f" * 32 + "/" + identity + EXTENSIONS[row["kind"]]
        row["sha256"] = hashlib.sha256(json_bytes(value["documents"][identity])).hexdigest()
    materials = ProjectStore._material_index(rows, value["documents"])
    for identity, row in rows.items():
        row["dependencies"] = ProjectStore._dependencies(identity, rows, value["documents"], materials)
    value["manifest"]["documents"] = list(rows.values())
    return value, created


def by_kind(snapshot, kind):
    return [row["id"] for row in snapshot["manifest"]["documents"] if row["kind"] == kind]


def test_empty_read_creates_no_directories_or_sqlite(isolated):
    before = list(isolated.iterdir())
    snapshot = global_resources.read_global_resources()
    assert snapshot["documents"] == {}
    assert snapshot["manifest"]["name"] == "全局资源库"
    assert list(isolated.iterdir()) == before
    assert global_resources.read_global_resources() == snapshot


def test_read_normalizes_bounded_typed_graph_and_stable_uuid_refs(isolated):
    typed_graph(isolated)
    # Legacy .json and files outside typed libraries are not resources.
    write(isolated / "data/sources/old.json", {"unreadable": True})
    write(isolated / "data/arbitrary/hidden.csrc", {"unreadable": True})
    before = files(isolated)
    first = global_resources.read_global_resources()
    assert len(first["documents"]) == 5
    assert first == global_resources.read_global_resources()
    assert files(isolated) == before
    records = {row["id"]: row for row in first["manifest"]["documents"]}
    process_id = by_kind(first, "process")[0]
    machine_id = by_kind(first, "machine")[0]
    assert first["documents"][process_id]["machine_file"] == machine_id + ".cmac"
    assert records[process_id]["dependencies"] == sorted([machine_id, by_kind(first, "substrate")[0]])
    assert len(records[machine_id]["dependencies"]) == 2
    ProjectStore._validate_graph(records, first["documents"])


def test_snapshot_encodes_each_row_once_and_reencodes_only_rebound_rows(isolated, monkeypatch):
    typed_graph(isolated)
    write(isolated / "data/materials/glass.json", profile())
    encoded = []
    original = global_resources.json_bytes

    def count(value):
        encoded.append(deepcopy(value))
        return original(value)

    monkeypatch.setattr(global_resources, "json_bytes", count)
    snapshot = global_resources.read_global_resources()
    # Five file rows + profile/optics each encode once. Only machine and
    # process references change, so those two rows need a second encoding.
    assert len(snapshot["documents"]) == 7 and len(encoded) == 9
    for row in snapshot["manifest"]["documents"]:
        assert row["sha256"] == hashlib.sha256(json_bytes(snapshot["documents"][row["id"]])).hexdigest()
    encoded.clear()
    assert global_resources.read_global_resources() == snapshot
    assert len(encoded) == 9  # Every fresh capture still rereads/revalidates.


def test_rebound_binary_payload_uses_its_final_canonical_digest_and_source(isolated, monkeypatch):
    identity, generation = "a" * 32, "b" * 32
    folder = isolated / "data/raytrace/exchange"
    payload = folder / "assets" / generation / (identity + ".csv")
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"500,1\n600,2\n")
    write(folder / "input.casset", {"kind": "coat_sim.project_binary_asset", "version": 1,
        "role": "spd_csv", "payload_path": f"assets/{generation}/{identity}.csv",
        "size": payload.stat().st_size, "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()})
    encoded = []
    original = global_resources.json_bytes

    def count(value):
        encoded.append(deepcopy(value))
        return original(value)

    monkeypatch.setattr(global_resources, "json_bytes", count)
    snapshot = global_resources.read_global_resources()
    row = snapshot["manifest"]["documents"][0]
    assert len(encoded) == 2
    metadata = snapshot["documents"][row["id"]]
    assert Path(metadata["payload_path"]).stem == row["id"] != identity
    assert row["sha256"] == hashlib.sha256(json_bytes(metadata)).hexdigest()
    assert snapshot["assets"][row["id"]] == {"path": str(payload)}


@pytest.mark.parametrize("limit", ["file", "total"])
def test_rebound_canonical_sizes_still_enforce_final_capacity(isolated, monkeypatch, limit):
    typed_graph(isolated)
    initial_sizes = [len(json_bytes(normalize_document(kind, json.loads(path.read_text()))))
                     for path, kind in global_resources._typed_paths(global_resources._roots())]
    snapshot = global_resources.read_global_resources()
    final_sizes = [len(json_bytes(value)) for value in snapshot["documents"].values()]
    if limit == "file":
        assert max(final_sizes) > max(initial_sizes)
        monkeypatch.setattr(global_resources, "MAX_FILE_BYTES", max(initial_sizes))
    else:
        assert sum(final_sizes) > sum(initial_sizes)
        monkeypatch.setattr(global_resources, "MAX_PROJECT_BYTES", sum(initial_sizes))
    with pytest.raises(ProjectError) as error:
        global_resources.read_global_resources()
    assert error.value.code == "capacity_exceeded"


def test_global_preview_rechecks_changed_source_and_never_changes_saved_target(isolated, tmp_path):
    from coat_sim.core.project_resource_service import ProjectResourceService
    typed_graph(isolated)
    store = ProjectStore.create(tmp_path / "owner", "Owner")
    service = ProjectResourceService(store)
    before = files(store.root)
    catalog = service.inspect_resources("global")
    selected = next(row["id"] for row in catalog["rows"] if row["kind"] == "process")
    plan = service.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert set(row["kind"] for row in plan["rows"]) == {"process", "machine", "source", "substrate", "mask"}
    assert files(store.root) == before
    path = isolated / "data/sources/lamp.csrc"
    source = json.loads(path.read_text())
    source["source_exponent"] = 2
    write(path, source)
    with pytest.raises(ProjectError) as error:
        service.apply_resource_transfer(plan["plan_id"])
    assert error.value.code == "stale_source"
    assert files(store.root) == before
    assert store.manifest["revision"] == 0


def test_validated_snapshot_reuses_the_bytes_used_for_capacity(isolated, monkeypatch):
    typed_graph(isolated)
    snapshot = global_resources.read_global_resources()
    calls = 0
    original = global_resources.json_bytes

    def count(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(global_resources, "json_bytes", count)
    records, documents = global_resources._validated(snapshot)
    assert documents == snapshot["documents"]
    assert set(records) == set(documents)
    assert calls == len(documents)


def test_read_material_profile_and_optics_are_detached_and_no_writer_lock(isolated):
    write(isolated / "data/materials/glass.json", profile())
    before = files(isolated)
    snapshot = global_resources.read_global_resources()
    assert len(snapshot["documents"]) == 2
    assert files(isolated) == before
    assert not (isolated / "data/materials/.material-library.lock").exists()
    assert snapshot == global_resources.read_global_resources()
    snapshot["documents"][by_kind(snapshot, "material_profile")[0]]["material"]["name"] = "Changed"
    assert global_resources.read_global_resources()["documents"] != snapshot["documents"]


def test_read_existing_sqlite_uses_readonly_mode_and_material_closure(isolated, monkeypatch):
    write(isolated / "data/materials/glass.json", profile())
    design = new_design("Film", design_id="film-id")
    design["front_layers"] = [{"id": "layer", "material_id": "glass", "optical_set_id": "set",
                                "thickness": {"value": 100, "unit": "nm"}, "note": ""}]
    database = isolated / "data/film_design/library.sqlite3"
    FilmDesignStore(database).save(design)
    FilmExpressionTemplateStore(database).save({"template_id": "expression-id", "name": "Expression",
        "expression": {"text": "A", "symbols": {"A": {"material_id": "glass", "optical_set_id": "set",
            "thickness": {"value": 100, "unit": "nm"}}}}})
    before = files(isolated)
    connect = sqlite3.connect
    connections = []

    def record(*args, **kwargs):
        connections.append((args, kwargs))
        return connect(*args, **kwargs)

    monkeypatch.setattr(global_resources.sqlite3, "connect", record)
    snapshot = global_resources.read_global_resources()
    assert all("?mode=ro" in str(args[0]) and kwargs.get("uri") for args, kwargs in connections)
    assert files(isolated) == before
    material_id = by_kind(snapshot, "optical_material")[0]
    for kind in ("film", "expression_template"):
        identity = by_kind(snapshot, kind)[0]
        row = next(row for row in snapshot["manifest"]["documents"] if row["id"] == identity)
        assert row["dependencies"] == [material_id]


@pytest.mark.parametrize("problem,code", [("outside", "unsafe_path"), ("missing", "missing_dependency"), ("symlink", "unsafe_path")])
def test_read_refuses_unsafe_or_incomplete_typed_graph(isolated, problem, code):
    typed_graph(isolated)
    machine_path = isolated / "data/machines/rig.cmac"
    machine = json.loads(machine_path.read_text())
    if problem == "outside":
        path = isolated / "outside.csrc"
        write(path, {"schema_version": 2, "kind": "point", "device_type": "custom"})
        machine["sources"][0]["file"] = str(path)
    elif problem == "missing":
        machine["sources"][0]["file"] = "../sources/missing.csrc"
    else:
        (isolated / "data/sources/link.csrc").symlink_to(isolated / "data/sources/lamp.csrc")
    write(machine_path, machine)
    with pytest.raises(ProjectError) as error:
        global_resources.read_global_resources()
    assert error.value.code == code


def test_read_fails_closed_on_scan_limit(isolated, monkeypatch):
    typed_graph(isolated)
    monkeypatch.setattr(global_resources, "MAX_SCAN_ENTRIES", 1)
    with pytest.raises(ProjectError) as error:
        global_resources.read_global_resources()
    assert error.value.code == "capacity_exceeded"


def test_typed_export_publishes_one_complete_directory_and_preserves_existing(isolated):
    typed_graph(isolated)
    before = global_resources.read_global_resources()
    previous = files(isolated)
    selected = by_kind(before, "process")
    result = global_resources.write_global_resources(before, before, [], selected)
    destination = Path(result["directory"])
    assert destination.parent == isolated / "data/processes"
    assert destination.name.startswith("exchange-")
    assert len(result["files"]) == 5
    assert all(Path(path).exists() for path in result["files"])
    assert all(files(isolated)[path] == payload for path, payload in previous.items())
    saved_process = json.loads((destination / (selected[0] + ".cproc")).read_text())
    assert (destination / saved_process["machine_file"]).exists()
    reread = global_resources.read_global_resources()
    assert len(reread["documents"]) == 10
    assert reread == global_resources.read_global_resources()


def test_typed_failed_publication_removes_staging_and_exposes_no_exchange(isolated, monkeypatch):
    typed_graph(isolated)
    before = global_resources.read_global_resources()
    previous = files(isolated)

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(global_resources, "_rename_directory_no_replace", fail)
    with pytest.raises(ProjectError):
        global_resources.write_global_resources(before, before, [], by_kind(before, "process"))
    assert files(isolated) == previous
    assert not list((isolated / "data/processes").glob("*exchange*"))
    assert not list((isolated / "data/processes").glob(".exchange*"))


def test_stale_global_snapshot_is_rejected_before_mutation(isolated):
    typed_graph(isolated)
    before = global_resources.read_global_resources()
    source_path = isolated / "data/sources/lamp.csrc"
    source = json.loads(source_path.read_text())
    source["source_exponent"] = 2
    write(source_path, source)
    previous = files(isolated)
    with pytest.raises(ProjectError) as error:
        global_resources.write_global_resources(before, before, [], by_kind(before, "process"))
    assert error.value.code == "stale_document"
    assert files(isolated) == previous


def test_material_profile_and_companion_are_one_existing_library_transaction(isolated):
    before = global_resources.read_global_resources()
    record = profile()
    envelope = {"kind": "coat_sim.project_material_profile", "version": 1, "material": record}
    after, created = after_snapshot(before, [("material_profile", envelope), ("optical_material", optical_material())])
    result = global_resources.write_global_resources(before, after, created, [created[0]])
    assert result["files"] == [str(isolated / "data/materials/glass.json")]
    assert MaterialLibrary(directory=isolated / "data/materials").read("glass")["record"] == normalize_document("material_profile", envelope)["material"]
    assert len(global_resources.read_global_resources()["documents"]) == 2


def test_optical_only_export_creates_explicit_optical_only_profile(isolated):
    before = global_resources.read_global_resources()
    after, created = after_snapshot(before, [("optical_material", optical_material())])
    global_resources.write_global_resources(before, after, created, created)
    record = json.loads((isolated / "data/materials/glass.json").read_text())
    assert record["optical_only"] is True
    assert record["resistance_model"] is None
    assert record["electron_beam_model"] is None
    assert len(record["optical_constants"]) == 1


def test_material_export_only_changes_named_rows_and_no_implicit_overwrite(isolated):
    write(isolated / "data/materials/glass.json", profile())
    write(isolated / "data/materials/other.json", profile("other"))
    before = global_resources.read_global_resources()
    previous = (isolated / "data/materials/other.json").read_bytes()
    identity = next(key for key in by_kind(before, "optical_material") if before["documents"][key]["material"]["id"] == "glass")
    optics = deepcopy(before["documents"][identity])
    optics["material"]["name"] = "New Glass"
    after, _ = after_snapshot(before, updates={identity: optics})
    global_resources.write_global_resources(before, after, [identity], [identity])
    assert (isolated / "data/materials/other.json").read_bytes() == previous
    record = json.loads((isolated / "data/materials/glass.json").read_text())
    assert record["description"] == "Preserve full process fields"
    assert record["name"] == "New Glass"
    before = global_resources.read_global_resources()
    # Duplicate optical business identity is rejected during graph validation.
    with pytest.raises(ProjectError):
        after, created = after_snapshot(before, [("optical_material", optical_material())])
        global_resources.write_global_resources(before, after, created, created)


def test_film_export_with_reused_materials_uses_existing_sqlite_schema(isolated):
    write(isolated / "data/materials/glass.json", profile())
    before = global_resources.read_global_resources()
    design = new_design("Portable", design_id="portable")
    design["front_layers"] = [{"id": "layer", "material_id": "glass", "optical_set_id": "set",
                                "thickness": {"value": 80, "unit": "nm"}, "note": ""}]
    after, created = after_snapshot(before, [("film", design)])
    result = global_resources.write_global_resources(before, after, created, created)
    database = isolated / "data/film_design/library.sqlite3"
    assert result["files"] == [str(database)]
    assert FilmDesignStore(database).load("portable")["name"] == "Portable"
    assert len(by_kind(global_resources.read_global_resources(), "film")) == 1


def test_template_export_writes_reusable_template_table(isolated):
    before = global_resources.read_global_resources()
    template = {"template_id": "plain", "name": "Unbound", "expression": {"text": "A", "symbols": {
        "A": {"material_id": "", "optical_set_id": "", "thickness": {"value": 80, "unit": "nm"}}}}}
    after, created = after_snapshot(before, [("expression_template", template)])
    global_resources.write_global_resources(before, after, created, created)
    saved = FilmExpressionTemplateStore(isolated / "data/film_design/library.sqlite3").load("plain")
    assert saved["name"] == "Unbound"


def test_film_requiring_new_materials_is_explicitly_unsupported_without_partial_files(isolated):
    before = global_resources.read_global_resources()
    design = new_design("Portable", design_id="portable")
    design["front_layers"] = [{"id": "layer", "material_id": "glass", "optical_set_id": "set",
                                "thickness": {"value": 80, "unit": "nm"}, "note": ""}]
    after, created = after_snapshot(before, [("optical_material", optical_material()), ("film", design)])
    with pytest.raises(ProjectError) as error:
        global_resources.write_global_resources(before, after, created, [created[1]])
    assert error.value.code == "unsupported_transfer"
    assert not (isolated / "data").exists()


def test_mixed_root_kinds_are_invalid_selection_without_writes(isolated):
    typed_graph(isolated)
    before = global_resources.read_global_resources()
    previous = files(isolated)
    with pytest.raises(ProjectError) as error:
        global_resources.write_global_resources(before, before, [], by_kind(before, "process") + by_kind(before, "source"))
    assert error.value.code == "invalid_selection"
    assert files(isolated) == previous


def test_material_multirecord_failure_rolls_back_the_first_write(isolated, monkeypatch):
    before = global_resources.read_global_resources()
    after, created = after_snapshot(before, [("optical_material", optical_material("glass")),
                                             ("optical_material", optical_material("other"))])
    replace = material_business.replace_with_retry
    attempts = 0

    def fail_second(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("Synthetic second material publication failure")
        return replace(source, target)

    monkeypatch.setattr(material_business, "replace_with_retry", fail_second)
    with pytest.raises(ProjectError):
        global_resources.write_global_resources(before, after, created, created)
    assert not list((isolated / "data/materials").glob("*.json"))
    assert global_resources.read_global_resources() == before


def test_sqlite_multiple_root_failure_is_rolled_back(isolated, monkeypatch):
    before = global_resources.read_global_resources()
    after, created = after_snapshot(before, [("film", new_design("First", design_id="first")),
                                             ("film", new_design("Second", design_id="second"))])
    connect = sqlite3.connect

    class FailSecondInsert:
        def __init__(self, connection):
            self.connection = connection
            self.inserts = 0

        def execute(self, statement, *args):
            if statement.lstrip().startswith("INSERT"):
                self.inserts += 1
                if self.inserts == 2:
                    raise sqlite3.OperationalError("Synthetic second film failure")
            return self.connection.execute(statement, *args)

        def close(self):
            return self.connection.close()

    def wrapped(path, *args, **kwargs):
        connection = connect(path, *args, **kwargs)
        return connection if kwargs.get("uri") else FailSecondInsert(connection)

    monkeypatch.setattr(global_resources.sqlite3, "connect", wrapped)
    with pytest.raises(ProjectError):
        global_resources.write_global_resources(before, after, created, created)
    assert global_resources.read_global_resources() == before
    assert not (isolated / "data/film_design/library.sqlite3").exists()


def test_missing_legacy_film_layer_ids_are_deterministic(isolated):
    database = isolated / "data/film_design/library.sqlite3"
    design = FilmDesignStore(database).save(new_design("Legacy", design_id="legacy"))
    design["front_layers"] = [{"material_id": "", "optical_set_id": "",
                               "thickness": {"value": 100, "unit": "nm"}, "note": ""}]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE film_designs SET document_json=?", (json.dumps(design),))
    first = global_resources.read_global_resources()
    assert first == global_resources.read_global_resources()
    film = first["documents"][by_kind(first, "film")[0]]
    assert len(film["front_layers"][0]["id"]) == 32


def test_corrupt_sqlite_document_is_reported_without_changing_database(isolated):
    database = isolated / "data/film_design/library.sqlite3"
    FilmDesignStore(database).save(new_design("Valid", design_id="valid"))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE film_designs SET document_json='{}'")
    previous = files(isolated)
    with pytest.raises(ProjectError) as error:
        global_resources.read_global_resources()
    assert error.value.code == "dependency_conflict"
    assert files(isolated) == previous
