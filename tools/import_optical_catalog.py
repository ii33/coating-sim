"""Validate and append an optical-data package to the built-in catalogue.

This is a reconstruction from the package contract and archived validation
evidence in ``data/optical_catalog_sources``; it is not asserted to be the
original importer implementation.

Packages are deliberately narrow: each optical group is routed to an exact
built-in material ID and role. Importing appends groups and records the source
dataset ID while preserving unrelated catalogue metadata and existing groups.
The command-line interface previews by default; pass ``--apply`` to atomically
replace the catalogue after successful validation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = ROOT / "data" / "optical_catalog.json"
MAX_PACKAGE_BYTES = 64 * 1024 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def read_json(path: str | Path) -> Any:
    """Read strict UTF-8 JSON, rejecting duplicate keys and NaN/Infinity."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"JSON input must be a regular non-symlink file: {source}")
    if source.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError(f"JSON input exceeds {MAX_PACKAGE_BYTES} bytes")
    try:
        with source.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream, object_pairs_hook=_reject_duplicate_keys,
                             parse_constant=_reject_nonfinite)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {source}: {exc}") from exc


def _builtin_profile_index(profiles: list[Mapping[str, Any]] | None = None) -> dict[str, Mapping[str, Any]]:
    if profiles is None:
        from coat_sim.core.materials import builtin_material_profiles
        profiles = builtin_material_profiles()

    by_id: dict[str, Mapping[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, Mapping):
            raise ValueError("built-in material profile is not an object")
        identity = profile.get("id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("built-in material profile has no ID")
        if identity in by_id:
            raise ValueError(f"duplicate built-in material profile ID: {identity}")
        by_id[identity] = profile
    return by_id


def _profile_material_id(profile: Mapping[str, Any]) -> str:
    research = profile.get("research_data")
    if isinstance(research, Mapping) and isinstance(research.get("id"), str):
        return research["id"]
    return str(profile["id"])


def _profile_category(profile: Mapping[str, Any]) -> str:
    from coat_sim.core.materials import material_category
    return material_category(profile)


def _validate_group_records(value: Any, *, label: str) -> list[dict[str, Any]]:
    from coat_sim.core.optical_constants import validate_optical_sets

    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must contain at least one optical_constants record")
    allowed = {"id", "name", "notes", "source", "kind", "wavelength_unit",
               "wavelength_min", "wavelength_max", "process_parameters",
               "k_data", "wavelengths", "n", "k", "parameters",
               "formula_number", "coefficients"}
    for index, group in enumerate(value):
        if not isinstance(group, Mapping):
            raise ValueError(f"{label}.optical_constants[{index}] must be an object")
        unknown = set(group) - allowed
        if unknown:
            raise ValueError(f"{label}.optical_constants[{index}] has unsupported fields: "
                             + ", ".join(sorted(map(str, unknown))))
        if not isinstance(group.get("notes"), str) or not group["notes"].strip():
            raise ValueError(f"{label}.optical_constants[{index}] needs source notes")
        if not isinstance(group.get("source"), str) or not group["source"].strip():
            raise ValueError(f"{label}.optical_constants[{index}] needs a source")
    try:
        return validate_optical_sets(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid optical groups for {label}: {exc}") from exc


def validate_package_catalog(
    package_catalog: Any,
    profiles: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a validated summary for a package's material/group mapping.

    A package uses schema version 1 and contains ``dataset_id`` plus a
    ``materials`` list. Every package material must identify exactly one
    current built-in profile using its built-in ID, research/material ID and
    category (``film``, ``substrate`` or ``other``). ``other`` is intentionally
    supported for optical media such as the ideal-air placeholder.
    """
    if not isinstance(package_catalog, Mapping):
        raise ValueError("optical catalog package must be a JSON object")
    if type(package_catalog.get("schema_version")) is not int or package_catalog["schema_version"] != 1:
        raise ValueError("optical catalog package must use schema_version 1")
    dataset_id = package_catalog.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip() or len(dataset_id) > 160:
        raise ValueError("optical catalog package needs a non-empty dataset_id")
    raw_materials = package_catalog.get("materials")
    if not isinstance(raw_materials, list) or not raw_materials:
        raise ValueError("optical catalog package needs a non-empty materials list")
    if len(raw_materials) > 1000:
        raise ValueError("optical catalog package exceeds 1000 material records")

    builtin_by_id = _builtin_profile_index(profiles)
    materials: dict[str, dict[str, Any]] = {}
    material_ids: set[str] = set()
    group_ids: set[str] = set()
    groups_by_category: Counter[str] = Counter()
    group_count = 0
    material_fields = {"material_id", "builtin_material_id", "category", "optical_constants"}

    for index, raw in enumerate(raw_materials):
        label = f"materials[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} must be an object")
        unknown = set(raw) - material_fields
        if unknown:
            raise ValueError(f"{label} has unsupported fields: " + ", ".join(sorted(map(str, unknown))))
        builtin_id = raw.get("builtin_material_id")
        material_id = raw.get("material_id")
        category = raw.get("category")
        if not isinstance(builtin_id, str) or not builtin_id:
            raise ValueError(f"{label}.builtin_material_id must be a non-empty string")
        profile = builtin_by_id.get(builtin_id)
        if profile is None:
            raise ValueError(f"unknown built-in optical material: {builtin_id!r}")
        if builtin_id in materials:
            raise ValueError(f"duplicate built-in material in package: {builtin_id}")
        expected_material_id = _profile_material_id(profile)
        expected_category = _profile_category(profile)
        if material_id != expected_material_id:
            raise ValueError(f"material ID does not match built-in profile {builtin_id}: "
                             f"expected {expected_material_id!r}")
        if category not in ("film", "substrate", "other") or category != expected_category:
            raise ValueError(f"material category does not match built-in profile {builtin_id}: "
                             f"expected {expected_category!r}")
        groups = _validate_group_records(raw.get("optical_constants"), label=label)
        for group in groups:
            group_id = group["id"]
            if group_id in group_ids:
                raise ValueError(f"duplicate optical group ID in package: {group_id}")
            group_ids.add(group_id)
            groups_by_category[category] += 1
        material_ids.add(material_id)
        group_count += len(groups)
        materials[builtin_id] = {
            "material_id": material_id,
            "builtin_material_id": builtin_id,
            "category": category,
            "optical_constants": groups,
        }

    if len(material_ids) != len(materials):
        raise ValueError("package has duplicate material IDs")
    return {
        "dataset_id": dataset_id,
        "material_count": len(materials),
        "group_count": group_count,
        "groups_by_category": dict(sorted(groups_by_category.items())),
        "materials": materials,
    }


def append_package_catalog(
    existing_catalog: Any,
    package_catalog: Any,
    profiles: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create an append-only merged catalogue; inputs are never mutated.

    An identical already-imported group is idempotently retained. A group ID
    collision with different content is an error, as are material-role
    conflicts. Root metadata and all unrelated materials/groups are copied
    through unchanged. The package's dataset ID is added to the optional
    ``datasets`` inventory without replacing the original root ``dataset_id``.
    """
    if not isinstance(existing_catalog, Mapping):
        raise ValueError("existing optical catalog must be a JSON object")
    if type(existing_catalog.get("schema_version")) is not int or existing_catalog["schema_version"] != 1:
        raise ValueError("existing optical catalog must use schema_version 1")
    existing_rows = existing_catalog.get("materials")
    if not isinstance(existing_rows, list):
        raise ValueError("existing optical catalog needs a materials list")
    profile_index = _builtin_profile_index(profiles)
    incoming = validate_package_catalog(package_catalog, list(profile_index.values()))

    result = deepcopy(dict(existing_catalog))
    result["materials"] = deepcopy(existing_rows)
    by_builtin: dict[str, dict[str, Any]] = {}
    all_group_ids: dict[str, dict[str, Any]] = {}

    for index, raw in enumerate(result["materials"]):
        if not isinstance(raw, Mapping):
            raise ValueError(f"existing materials[{index}] must be an object")
        builtin_id = raw.get("builtin_material_id")
        material_id = raw.get("material_id")
        category = raw.get("category")
        if not isinstance(builtin_id, str) or builtin_id not in profile_index:
            raise ValueError(f"existing catalog has unknown built-in material: {builtin_id!r}")
        if builtin_id in by_builtin:
            raise ValueError(f"existing catalog has duplicate material row: {builtin_id}")
        profile = profile_index[builtin_id]
        if material_id != _profile_material_id(profile) or category != _profile_category(profile):
            raise ValueError(f"existing catalog material mapping is inconsistent: {builtin_id}")
        groups = _validate_group_records(raw.get("optical_constants"),
                                         label=f"existing materials[{index}]")
        # Keep the original JSON representation while using validated groups
        # to reject malformed values and build the collision index.
        row = result["materials"][index]
        if not isinstance(row, dict):
            row = dict(row)
            result["materials"][index] = row
        by_builtin[builtin_id] = row
        for group in groups:
            prior = all_group_ids.get(group["id"])
            if prior is not None:
                raise ValueError(f"existing catalog repeats an optical group ID: {group['id']}")
            all_group_ids[group["id"]] = group

    for builtin_id, package_row in incoming["materials"].items():
        existing_row = by_builtin.get(builtin_id)
        if existing_row is None:
            existing_row = deepcopy(package_row)
            incoming_groups = existing_row["optical_constants"]
            existing_row["optical_constants"] = []
            result["materials"].append(existing_row)
            by_builtin[builtin_id] = existing_row
        elif (existing_row.get("material_id") != package_row["material_id"]
              or existing_row.get("category") != package_row["category"]):
            raise ValueError(f"package conflicts with existing catalog mapping: {builtin_id}")
        else:
            incoming_groups = package_row["optical_constants"]

        target_groups = existing_row.get("optical_constants")
        if not isinstance(target_groups, list):
            raise ValueError(f"existing optical groups must be a list: {builtin_id}")
        existing_group_by_id = {group.get("id"): group for group in target_groups
                                 if isinstance(group, Mapping)}
        for group in incoming_groups:
            prior_global = all_group_ids.get(group["id"])
            if prior_global is not None:
                if prior_global != group:
                    raise ValueError(f"optical group ID conflicts with existing catalog: {group['id']}")
                if group["id"] not in existing_group_by_id:
                    raise ValueError(f"optical group ID is already assigned to another material: {group['id']}")
                # Re-import of an identical group is a no-op.
                continue
            target_groups.append(deepcopy(group))
            all_group_ids[group["id"]] = deepcopy(group)
            existing_group_by_id[group["id"]] = group

    datasets = result.get("datasets", [])
    if not isinstance(datasets, list) or any(not isinstance(item, str) for item in datasets):
        raise ValueError("existing catalog datasets must be a list of strings")
    if incoming["dataset_id"] not in datasets:
        result["datasets"] = [*datasets, incoming["dataset_id"]]
    return result


def write_json_atomically(path: str | Path, value: Any) -> None:
    """Atomically replace a regular JSON file in its existing directory."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"refusing to replace symlink: {target}")
    if not target.parent.is_dir():
        raise ValueError(f"output directory does not exist: {target.parent}")
    serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                                   dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _group_ids(catalog: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for material in catalog.get("materials", []):
        if isinstance(material, Mapping):
            for group in material.get("optical_constants", []):
                if isinstance(group, Mapping) and isinstance(group.get("id"), str):
                    result.add(group["id"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="validated package JSON")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH,
                        help="existing built-in optical_catalog.json")
    parser.add_argument("--apply", action="store_true",
                        help="atomically write the merged catalogue (default is preview only)")
    args = parser.parse_args(argv)

    try:
        package = read_json(args.package)
        existing = read_json(args.catalog)
        validated = validate_package_catalog(package)
        merged = append_package_catalog(existing, package)
        before_groups = _group_ids(existing)
        after_groups = _group_ids(merged)
        added_groups = sorted(after_groups - before_groups)
        before_datasets = existing.get("datasets", [])
        added_dataset = validated["dataset_id"] not in before_datasets
        if args.apply:
            write_json_atomically(args.catalog, merged)
        print(json.dumps({
            "dataset_id": validated["dataset_id"],
            "package_material_count": validated["material_count"],
            "package_group_count": validated["group_count"],
            "added_group_count": len(added_groups),
            "added_group_ids": added_groups,
            "dataset_record_added": added_dataset,
            "catalog_written": bool(args.apply),
        }, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2  # argparse.error exits; this is only for static type checkers.


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke checks
    raise SystemExit(main())
