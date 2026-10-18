"""Focused contract tests for the reconstructed optical-catalog importer."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools.import_optical_catalog import (
    append_package_catalog,
    read_json,
    validate_package_catalog,
    write_json_atomically,
)


def _profile(identity, roles):
    return {"id": identity, "roles": roles}


PROFILES = [
    _profile("builtin_film", ["film"]),
    _profile("builtin_substrate", ["substrate"]),
    _profile("builtin_medium", ["medium"]),
]


def _group(identity, *, source="Source record; k not supplied"):
    return {
        "id": identity,
        "name": f"Evidence {identity}",
        "notes": "Reference data; unknown k remains unknown.",
        "source": source,
        "kind": "tabulated",
        "wavelength_unit": "um",
        "wavelength_min": 0.4,
        "wavelength_max": 0.7,
        "wavelengths": [0.4, 0.7],
        "n": [1.5, 1.6],
        "k": None,
    }


def _material(identity, category, groups, *, builtin_id=None):
    return {
        "material_id": identity,
        "builtin_material_id": builtin_id or identity,
        "category": category,
        "optical_constants": groups,
    }


def _package(materials=None, *, dataset_id="optical_test_v1"):
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "materials": materials or [
            _material("builtin_medium", "other", [_group("test_medium_n")])
        ],
    }


def test_real_archived_validation_summary_is_accepted_with_other_categories():
    root = Path(__file__).resolve().parents[1]
    evidence = json.loads((root / "data/optical_catalog_sources/rii_additional_public_20261006_v1/schema_validation.json")
                          .read_text(encoding="utf-8"))
    recorded_summary = evidence["summary"]
    package = {
        "schema_version": 1,
        "dataset_id": recorded_summary["dataset_id"],
        "materials": list(recorded_summary["materials"].values()),
    }
    summary = validate_package_catalog(package)
    assert summary["dataset_id"] == "rii_additional_public_20261006_v1"
    assert summary["material_count"] == 2
    assert summary["group_count"] == 4
    assert summary["groups_by_category"] == {"film": 4}
    assert set(summary["materials"]) == {
        "builtin_research_Al2O3_film", "builtin_research_TiO2_film"
    }


def test_packaged_catalog_itself_satisfies_importer_mapping_contract():
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "data/optical_catalog.json").read_text(encoding="utf-8"))
    summary = validate_package_catalog(catalog)
    assert summary["material_count"] == 17
    assert summary["groups_by_category"]["other"] == 1
    assert summary["materials"]["builtin_ideal_air"]["category"] == "other"


def test_archived_batch_replay_is_a_noop_on_current_catalog():
    root = Path(__file__).resolve().parents[1]
    existing = json.loads((root / "data/optical_catalog.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "data/optical_catalog_sources/rii_additional_public_20261006_v1/schema_validation.json")
                          .read_text(encoding="utf-8"))
    recorded_summary = evidence["summary"]
    package = {
        "schema_version": 1,
        "dataset_id": recorded_summary["dataset_id"],
        "materials": list(recorded_summary["materials"].values()),
    }
    assert append_package_catalog(existing, package) == existing


def test_exact_material_role_mapping_accepts_other_but_rejects_approximate_mapping():
    package = _package()
    summary = validate_package_catalog(package, PROFILES)
    assert summary["groups_by_category"] == {"other": 1}
    assert summary["materials"]["builtin_medium"]["optical_constants"][0]["k"] is None

    wrong_category = deepcopy(package)
    wrong_category["materials"][0]["category"] = "film"
    with pytest.raises(ValueError, match="category does not match"):
        validate_package_catalog(wrong_category, PROFILES)

    wrong_material = deepcopy(package)
    wrong_material["materials"][0]["material_id"] = "nearby_formula_not_a_match"
    with pytest.raises(ValueError, match="material ID does not match"):
        validate_package_catalog(wrong_material, PROFILES)

    unknown = deepcopy(package)
    unknown["materials"][0]["builtin_material_id"] = "builtin_not_found"
    with pytest.raises(ValueError, match="unknown built-in"):
        validate_package_catalog(unknown, PROFILES)


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.update(schema_version=True), "schema_version 1"),
    (lambda p: p.update(dataset_id=""), "dataset_id"),
    (lambda p: p.update(materials=[]), "non-empty materials"),
    (lambda p: p["materials"][0].update(optical_constants=[]), "at least one optical_constants"),
    (lambda p: p["materials"][0]["optical_constants"][0].update(source="  "), "needs a source"),
    (lambda p: p["materials"][0]["optical_constants"][0].update(extra="discard me"), "unsupported fields"),
])
def test_invalid_package_metadata_or_group_is_rejected(mutate, message):
    package = _package()
    mutate(package)
    with pytest.raises(ValueError, match=message):
        validate_package_catalog(package, PROFILES)


def test_package_duplicate_group_ids_are_rejected_across_material_rows():
    package = _package([
        _material("builtin_film", "film", [_group("same_id")]),
        _material("builtin_substrate", "substrate", [_group("same_id")]),
    ])
    with pytest.raises(ValueError, match="duplicate optical group ID"):
        validate_package_catalog(package, PROFILES)


def test_append_preserves_unrelated_rows_metadata_and_unknown_k_without_mutation():
    manual_group = _group("manual_material_data", source="User-maintained source")
    existing = {
        "schema_version": 1,
        "dataset_id": "legacy_root_dataset",
        "datasets": ["legacy_root_dataset", "existing_batch"],
        "repository": {"commit": "fixed"},
        "contract_note": "leave this unrelated text alone",
        "materials": [
            _material("builtin_film", "film", [manual_group]),
        ],
    }
    before = deepcopy(existing)
    package = _package([
        _material("builtin_film", "film", [_group("new_film_group")]),
        _material("builtin_medium", "other", [_group("new_medium_group")]),
    ])

    merged = append_package_catalog(existing, package, PROFILES)

    assert existing == before
    assert merged["dataset_id"] == "legacy_root_dataset"
    assert merged["datasets"] == ["legacy_root_dataset", "existing_batch", "optical_test_v1"]
    assert merged["repository"] == {"commit": "fixed"}
    assert merged["contract_note"] == before["contract_note"]
    film = next(row for row in merged["materials"] if row["builtin_material_id"] == "builtin_film")
    assert [group["id"] for group in film["optical_constants"]] == [
        "manual_material_data", "new_film_group"
    ]
    assert film["optical_constants"][0]["source"] == "User-maintained source"
    assert film["optical_constants"][1]["k"] is None
    medium = next(row for row in merged["materials"] if row["builtin_material_id"] == "builtin_medium")
    assert medium["category"] == "other"
    assert [group["id"] for group in medium["optical_constants"]] == ["new_medium_group"]


def test_reimport_is_idempotent_and_conflicting_group_id_aborts():
    package = _package()
    empty = {"schema_version": 1, "dataset_id": "base", "datasets": ["base"], "materials": []}
    merged = append_package_catalog(empty, package, PROFILES)
    assert append_package_catalog(merged, package, PROFILES) == merged

    conflict = _package()
    conflict["materials"][0]["optical_constants"][0]["n"] = [1.7, 1.8]
    with pytest.raises(ValueError, match="conflicts with existing catalog"):
        append_package_catalog(merged, conflict, PROFILES)


def test_existing_duplicate_group_id_cannot_be_shared_between_materials():
    group = _group("shared_id")
    existing = {"schema_version": 1, "materials": [
        _material("builtin_film", "film", [deepcopy(group)]),
        _material("builtin_medium", "other", [deepcopy(group)]),
    ]}
    with pytest.raises(ValueError, match="repeats an optical group ID"):
        append_package_catalog(existing, _package(), PROFILES)


def test_strict_json_reader_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        read_json(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"x":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON number"):
        read_json(nonfinite)


def test_atomic_json_write_and_symlink_refusal(tmp_path):
    target = tmp_path / "catalog.json"
    target.write_text('{"version":1}\n', encoding="utf-8")
    write_json_atomically(target, {"dataset_id": "next", "k": None})
    assert json.loads(target.read_text(encoding="utf-8")) == {"dataset_id": "next", "k": None}
    assert not list(tmp_path.glob(".catalog.json.*.tmp"))

    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="refusing to replace symlink"):
        write_json_atomically(link, {"unsafe": True})

