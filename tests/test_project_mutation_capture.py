"""Scoped mutation captures preserve raw unrelated inputs, never validate by omission."""
import hashlib

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_operation_scope import OpaqueDocument, ScopedRecords, capture_inputs, prepare_publication
from coat_sim.core.project_store import ProjectStore
from tests.test_project_core import material, setup_project

pytestmark = pytest.mark.contract


def _rewrite(store, identity, document=None, *, content=None, dependencies=None, kind=None):
    """Simulate an already-persisted document, updating its real manifest hash."""
    manifest = store.manifest
    row = next(row for row in manifest["documents"] if row["id"] == identity)
    encoded = json_bytes(document) if content is None else content
    (store.root / row["path"]).write_bytes(encoded)
    row["sha256"] = hashlib.sha256(encoded).hexdigest()
    if dependencies is not None:
        row["dependencies"] = dependencies
    if kind is not None:
        row["kind"] = kind
    manifest_bytes = json_bytes(manifest)
    store.project_file.write_bytes(manifest_bytes)
    store._manifest = manifest
    store._fingerprint = hashlib.sha256(manifest_bytes).hexdigest()


def _kind(store, kind):
    return next(row["id"] for row in store.manifest["documents"] if row["kind"] == kind)


def _new_film(records, data, *, material_id=None):
    film = new_design("Scoped draft")
    if material_id:
        film["front_layers"] = [{"id": "layer", "material_id": material_id,
            "optical_set_id": "set", "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    return ProjectStore._add_document(records, data, "film", validate_design(film))


def _unsupported_source(store):
    identity = _kind(store, "source")
    source = store._read_record(store._index()[identity])
    source["unsupported_test_field"] = 6
    _rewrite(store, identity, source)
    return identity, source


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_capture_full_manifest_detached_raw_and_no_publication(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    source_id, raw = _unsupported_source(store)
    before = _snapshot(store.root)
    records, data = capture_inputs(store)
    assert isinstance(records, ScopedRecords)
    assert set(records) == set(store._index()) == set(data) == set(records.canonical_digests)
    assert records.base_records == store._index()
    assert records.fingerprint == store._fingerprint
    assert data[source_id] == raw
    data[source_id]["name"] = "only in RAM"
    records[source_id]["dependencies"].append("a" * 32)
    assert records.base_records[source_id]["dependencies"] == []
    assert _snapshot(store.root) == before
    with pytest.raises(ProjectError):
        store.snapshot_saved_inputs()


def test_prepare_new_film_keeps_unrelated_unsupported_source_and_all_original_rows(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    source_id, raw = _unsupported_source(store)
    records, data = capture_inputs(store)
    film_id = _new_film(records, data)
    before = _snapshot(store.root)
    strict, documents, retained = prepare_publication(store, records, data)
    assert set(strict) == set(documents) == {film_id}
    assert retained == records.base_records
    assert retained[source_id] == store._index()[source_id]
    assert data[source_id] == raw
    assert _snapshot(store.root) == before


def test_prepare_unchanged_and_deleted_film_does_not_normalize_unsupported_sources(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    film_id = store.save_film_document(new_design("Empty film"))
    _unsupported_source(store)
    records, data = capture_inputs(store)
    strict, documents, retained = prepare_publication(store, records, data)
    assert strict == documents == {}
    assert retained == records.base_records
    del records[film_id], data[film_id]
    strict, documents, retained = prepare_publication(store, records, data)
    assert strict == documents == {}
    assert film_id not in retained
    assert set(retained) == set(records)


@pytest.mark.parametrize("document", [
    {"schema_version": 3, "kind": "point"},
    {"schema_version": True, "kind": "point"},
    {"schema_version": 2, "kind": "future_shape"},
])
def test_unsupported_unrelated_source_does_not_block_current_film(tmp_path, document):
    store, _, _, _ = setup_project(tmp_path)
    _rewrite(store, _kind(store, "source"), document)
    records, data = capture_inputs(store)
    film = _new_film(records, data)
    strict, _, retained = prepare_publication(store, records, data)
    assert set(strict) == {film}
    assert _kind(store, "source") in retained


@pytest.mark.parametrize("content", [b"{", b'{"schema_version":2,"schema_version":2}',
                                       b'{"schema_version":2,"x":NaN}', b'{"x":1e999}', b"[]"])
def test_unparseable_json_is_display_only_opaque_not_a_valid_empty_document(tmp_path, content):
    store, _, _, _ = setup_project(tmp_path)
    _rewrite(store, _kind(store, "source"), content=content)
    records, data = capture_inputs(store)
    source = _kind(store, "source")
    assert isinstance(data[source], OpaqueDocument)
    assert records.canonical_digests[source].startswith("opaque:")
    film = _new_film(records, data)
    strict, _, retained = prepare_publication(store, records, data)
    assert set(strict) == {film}
    assert source in retained
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data, required_ids=[source])
    assert error.value.code == "reference_unconfirmed"


def test_capture_hash_mismatch_and_nonordinary_path_keep_safety_errors(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    row = store._index()[_kind(store, "source")]
    path = store.root / row["path"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ProjectError) as error:
        capture_inputs(store)
    assert error.value.code == "integrity_error"
    path.unlink()
    path.mkdir()
    with pytest.raises(ProjectError) as error:
        capture_inputs(store)
    assert error.value.code == "unsafe_path"


@pytest.mark.parametrize("field,value", [("path", "objects/" + "a" * 32 + "/" + "b" * 32 + ".csrc"),
                                         ("sha256", "a" * 64), ("dependencies", ["b" * 32]),
                                         ("kind", "mask")])
def test_existing_manifest_rows_cannot_be_rewritten_by_operation_caller(tmp_path, field, value):
    store, _, _, _ = setup_project(tmp_path)
    records, data = capture_inputs(store)
    records[_kind(store, "source")][field] = value
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code in {"stale_document", "dependency_conflict"}


def test_prepare_detects_data_change_by_canonical_digest_not_shared_object(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    records, data = capture_inputs(store)
    source_id = _kind(store, "source")
    data[source_id]["source_exponent"] = 3.
    strict, documents, retained = prepare_publication(store, records, data)
    assert source_id in strict and source_id not in retained
    assert documents[source_id]["source_exponent"] == 3.
    assert {_kind(store, kind) for kind in ("machine", "process", "deposition_configuration")} <= set(strict)
    assert set(strict) == set(records)
    assert set(retained) == set(records) - {source_id}


def test_changed_machine_requires_full_forward_closure_including_unsupported_source(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    _unsupported_source(store)
    records, data = capture_inputs(store)
    data[_kind(store, "machine")]["name"] = "Changed machine"
    with pytest.raises(ProjectError):
        prepare_publication(store, records, data)


def test_deleted_target_does_not_require_unsupported_old_forward_closure(tmp_path):
    store, _, process_id, _ = setup_project(tmp_path)
    store.set_default_configuration(None)
    # Configuration has no incoming reference after the default is cleared.
    configuration = _kind(store, "deposition_configuration")
    _unsupported_source(store)
    records, data = capture_inputs(store)
    del records[configuration], data[configuration]
    strict, _, retained = prepare_publication(store, records, data)
    assert strict == {}
    assert configuration not in retained
    assert process_id in retained


def test_delete_never_cascades_even_if_referencing_record_also_removed(tmp_path):
    store, _, _, configuration = setup_project(tmp_path)
    store.set_default_configuration(None)
    records, data = capture_inputs(store)
    process = _kind(store, "process")
    for key in (configuration, process):
        del records[key], data[key]
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "dependency_conflict"


def test_hidden_typed_incoming_edge_cannot_evade_manifest_deletion_guard(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    source_id = _kind(store, "source")
    machine = _kind(store, "machine")
    _rewrite(store, machine, store._read_record(store._index()[machine]),
             dependencies=[key for key in store._index()[machine]["dependencies"] if key != source_id])
    records, data = capture_inputs(store)
    del records[source_id], data[source_id]
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "dependency_conflict"


def test_possible_referrer_with_unreadable_reference_fields_fails_closed(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    machine = _kind(store, "machine")
    document = store._read_record(store._index()[machine])
    document["sources"] = "unconfirmed"
    _rewrite(store, machine, document)
    records, data = capture_inputs(store)
    source_id = _kind(store, "source")
    data[source_id]["source_exponent"] = 3.
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "reference_unconfirmed"


def test_embedded_process_film_snapshot_is_not_a_managed_film_reference(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    process = _kind(store, "process")
    raw = store._read_record(store._index()[process])
    raw["film_snapshot"] = {"not": "a managed document reference"}
    _rewrite(store, process, raw)
    records, data = capture_inputs(store)
    film = _new_film(records, data)
    strict, _, retained = prepare_publication(store, records, data)
    assert set(strict) == {film}
    assert process in retained


def test_optical_material_logical_id_conflicts_are_not_hidden_in_retained_rows(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    records, data = capture_inputs(store)
    for spelling in ("Glass", "glass"):
        ProjectStore._add_document(records, data, "optical_material", {
            "kind": "coat_sim.project_optical_material", "version": 1, "material": material(spelling)})
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "dependency_conflict"


def test_changing_optical_set_validates_existing_film_incoming_closure(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    records, data = capture_inputs(store)
    optical = ProjectStore._add_document(records, data, "optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material("film")})
    film = _new_film(records, data, material_id="film")
    store._publish(records, data)
    _unsupported_source(store)
    records, data = capture_inputs(store)
    data[optical]["material"]["optical_sets"][0]["n"] = [2.2, 2.2]
    strict, _, retained = prepare_publication(store, records, data)
    assert set(strict) == {optical, film}
    assert film in retained and optical not in retained
    data[optical]["material"]["optical_sets"] = []
    with pytest.raises(ProjectError):
        prepare_publication(store, records, data)


def test_captured_generation_staleness_is_rejected(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    records, data = capture_inputs(store)
    store.save_film_document(new_design("Concurrent"))
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "stale_project"


def test_explicit_unsupported_film_cleanup_does_not_normalize_target(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    film = store.save_film_document(new_design("Disposable test asset"))
    _rewrite(store, film, content=b"not JSON")
    records, data = capture_inputs(store)
    assert isinstance(data[film], OpaqueDocument)
    del records[film], data[film]
    assert prepare_publication(store, records, data) == ({}, {}, {})


def test_required_unchanged_target_still_validates_its_forward_closure(tmp_path):
    store, _, _, configuration = setup_project(tmp_path)
    _unsupported_source(store)
    records, data = capture_inputs(store)
    with pytest.raises(ProjectError):
        prepare_publication(store, records, data, required_ids=[configuration])


def test_unknown_version_possible_referrer_is_not_silently_skipped(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    machine = _kind(store, "machine")
    document = store._read_record(store._index()[machine])
    document["schema_version"] = 999
    _rewrite(store, machine, document)
    records, data = capture_inputs(store)
    data[_kind(store, "source")]["source_exponent"] = 3.
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "reference_unconfirmed"


def test_explicit_opaque_optical_cleanup_does_not_invent_a_logical_identity(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    optical = store.write_documents([("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1,
        "material": material("Disposable")})])[0]
    _rewrite(store, optical, content=b"not JSON")
    records, data = capture_inputs(store)
    del records[optical], data[optical]
    assert prepare_publication(store, records, data) == ({}, {}, {})


def test_opaque_target_cannot_be_republished_as_display_metadata(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    film = store.save_film_document(new_design("Unsupported"))
    _rewrite(store, film, content=b"bad JSON")
    records, data = capture_inputs(store)
    display_only = dict(data[film])
    data[film] = display_only
    with pytest.raises(ProjectError):
        prepare_publication(store, records, data)


def test_explicit_current_target_keeps_missing_dependency_error(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    records, data = capture_inputs(store)
    _new_film(records, data, material_id="missing")
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "missing_dependency"


def test_same_kind_incoming_ancestors_are_revisited_after_new_affected_nodes(tmp_path):
    from coat_sim.core.project_bbs import ProjectBbsService
    store = ProjectStore.create(tmp_path / "project", "P")
    service = ProjectBbsService(store)
    topic = service.create_post("journal", "Topic", "Body")["post_id"]
    later_ancestor = service.create_post("journal", "Unsupported nested reply", "Body")["post_id"]
    reply = service.reply(topic, "Reply", "Body", expected_post_revision=1)["post_id"]
    nested = store._read_record(store._index()[later_ancestor])
    nested["parent_id"] = reply
    _rewrite(store, later_ancestor, nested, dependencies=[reply])
    records, data = capture_inputs(store)
    data[topic]["history"][0]["title"] = "Changed topic"
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "dependency_conflict"


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_PROJECT_BYTES"])
def test_capture_safety_capacity_is_not_relaxed_for_unsupported_documents(tmp_path, monkeypatch, limit):
    from coat_sim.core import project_operation_scope
    store, _, _, _ = setup_project(tmp_path)
    _unsupported_source(store)
    monkeypatch.setattr(project_operation_scope, limit, 1)
    with pytest.raises(ProjectError) as error:
        capture_inputs(store)
    assert error.value.code == "capacity_exceeded"


def test_unknown_future_kind_requires_an_explicit_reference_capability_contract(tmp_path, monkeypatch):
    from coat_sim.core import project_operation_scope
    store = ProjectStore.create(tmp_path / "project", "P")
    monkeypatch.setattr(project_operation_scope, "KINDS", project_operation_scope.KINDS | {"future_kind"})
    with pytest.raises(ProjectError) as error:
        capture_inputs(store)
    assert error.value.code == "reference_unconfirmed"


def test_capture_linked_managed_json_is_rejected_even_if_digest_matches(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    row = store._index()[_kind(store, "source")]
    path = store.root / row["path"]
    outside = tmp_path / "outside.csrc"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ProjectError) as error:
        capture_inputs(store)
    assert error.value.code == "unsafe_path"


@pytest.mark.parametrize("unsupported_film", [False, True])
def test_removing_film_does_not_require_unrelated_opaque_optical_identity(tmp_path, unsupported_film):
    store = ProjectStore.create(tmp_path / "project", "P")
    optical = store.write_documents([("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1,
        "material": material("Unrelated")})])[0]
    film = store.save_film_document(new_design("Empty disposable film"))
    _rewrite(store, optical, content=b"unsupported optical test data")
    if unsupported_film:
        _rewrite(store, film, content=b"unsupported film test data")
    records, data = capture_inputs(store)
    del records[film], data[film]
    strict, strict_data, retained = prepare_publication(store, records, data)
    assert strict == strict_data == {}
    assert set(retained) == {optical}
    assert retained[optical] == store._index()[optical]


def test_new_film_referencing_opaque_optical_identity_is_still_rejected(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    optical = store.write_documents([("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1,
        "material": material("Unconfirmed")})])[0]
    _rewrite(store, optical, content=b"unsupported optical test data")
    records, data = capture_inputs(store)
    _new_film(records, data, material_id="Unconfirmed")
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "reference_unconfirmed"


def test_required_capture_roots_validate_same_content_target_and_dependencies(tmp_path):
    store, _, _, configuration = setup_project(tmp_path)
    _unsupported_source(store)
    records, data = capture_inputs(store)
    records.required_ids.add(configuration)
    assert all(records.canonical_digests[key] == hashlib.sha256(json_bytes(data[key])).hexdigest()
               for key in records)
    with pytest.raises(ProjectError) as error:
        prepare_publication(store, records, data)
    assert error.value.code == "reference_unconfirmed"


def test_required_capture_roots_and_explicit_required_ids_are_combined(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    first = store.save_film_document(new_design("First unchanged target"))
    second = store.save_film_document(new_design("Second unchanged target"))
    records, data = capture_inputs(store)
    records.required_ids.add(first)
    strict, strict_data, retained = prepare_publication(store, records, data, required_ids=[second])
    assert set(strict) == set(strict_data) == set(retained) == {first, second}
