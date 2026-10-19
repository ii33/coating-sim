"""Validated, path-safe material-library operations and exchange packages."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

from .atomic_io import replace_with_retry
from .materials import (builtin_material_profiles, list_materials, save_material,
                        validate_material_profile, material_completeness,
                        material_library_lock)


MATERIAL_PACKAGE_KIND = "coat_sim.material"
LIBRARY_PACKAGE_KIND = "coat_sim.material_library"
PACKAGE_VERSION = 1
PACKAGE_MAX_BYTES = 64 * 1024 * 1024
PACKAGE_MAX_RECORDS = 1000
PACKAGE_SUFFIXES = {MATERIAL_PACKAGE_KIND: ".cmat",
                    LIBRARY_PACKAGE_KIND: ".cmatl"}
# A deleted builtin writes one tiny marker per id. The marker is the durable
# user decision: seed reload, restart and upgrade must not resurrect it.
DELETED_BUILTIN_KIND = "coat_sim.deleted_builtin"
DELETED_BUILTIN_VERSION = 1


def _reject_reparse(path, label):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (stat.S_ISLNK(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError(f"{label} cannot be a symlink or reparse point")
    return info


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _pretty_json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       indent=2) + "\n").encode("utf-8")


def _fingerprint(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _check_id_set(records, label):
    seen = set()
    for record in records:
        identity = record["id"].casefold()
        if identity in seen:
            raise ValueError(f"duplicate material id ignoring case in {label}: {record['id']}")
        seen.add(identity)


def validate_material_identifier(value):
    """Validate one material id without requiring a full profile."""
    return validate_material_profile({"id": value, "name": "检查"})["id"]


def deleted_builtin_marker(identifier):
    """The exact on-disk tombstone payload for one builtin id.

    One serializer owns the marker bytes so a caller can fingerprint the
    expected tombstone before the file exists (see the recycle receipt).
    """
    return _pretty_json_bytes({"kind": DELETED_BUILTIN_KIND,
                               "version": DELETED_BUILTIN_VERSION,
                               "id": validate_material_identifier(identifier)})


def deleted_builtin_marker_sha256(identifier):
    return hashlib.sha256(deleted_builtin_marker(identifier)).hexdigest()


class MaterialLibrary:
    """Owns user records and builtin overrides without changing saved snapshots.

    A project normally uses ``data/materials/*.json`` for custom materials.
    Editable builtin records live under ``data/materials/.overrides/`` so the
    source catalogue remains available for restore. A deleted builtin writes a
    marker under ``data/materials/.deleted/``; the seed profile is never
    modified, yet the id stays absent until it is explicitly restored.
    ``directory`` is exposed for isolated callers and tests that already own a
    material folder.
    """

    def __init__(self, root=None, *, directory=None):
        if directory is not None:
            self.directory = Path(directory)
        elif root is not None:
            self.directory = Path(root) / "data" / "materials"
        else:
            raise TypeError("provide a project root or material directory")
        self.override_directory = self.directory / ".overrides"
        self.deleted_directory = self.directory / ".deleted"

    def _check_directory(self):
        for path in (self.directory.parent, self.directory,
                     self.override_directory, self.deleted_directory):
            info = _reject_reparse(path, "material library path")
            if info is not None and not stat.S_ISDIR(info.st_mode):
                raise ValueError("material library path must be a directory")


    def _read_record_file(self, path, label="material record"):
        _reject_reparse(path, label)
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > PACKAGE_MAX_BYTES:
            raise ValueError(f"{label} is not a regular file or is too large")
        try:
            raw = Path(path).read_bytes()
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read {label}: {exc}") from exc
        return validate_material_profile(value)

    def _read_tombstone_file(self, path):
        label = "deleted builtin marker"
        _reject_reparse(path, label)
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > PACKAGE_MAX_BYTES:
            raise ValueError(f"{label} is not a regular file or is too large")
        try:
            raw = Path(path).read_bytes()
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read {label}: {exc}") from exc
        if (not isinstance(value, dict) or set(value) != {"kind", "version", "id"}
                or value.get("kind") != DELETED_BUILTIN_KIND
                or value.get("version") != DELETED_BUILTIN_VERSION):
            raise ValueError("deleted builtin marker has unknown or missing fields")
        identifier = validate_material_identifier(value.get("id"))
        if Path(path).name != identifier + ".json":
            raise ValueError("deleted builtin marker filename does not match its id")
        return {"kind": DELETED_BUILTIN_KIND, "version": DELETED_BUILTIN_VERSION,
                "id": identifier}

    def _read_tombstones(self):
        """Return casefolded builtin ids with a durable deletion marker."""
        deleted = {}
        if self.deleted_directory.exists():
            for path in sorted(self.deleted_directory.glob("*.json")):
                record = self._read_tombstone_file(path)
                key = record["id"].casefold()
                if key in deleted:
                    raise ValueError("duplicate deleted builtin marker")
                deleted[key] = record
        return deleted

    def _catalog_state(self):
        """Return (originals, effective builtins, custom, overrides, deleted)."""
        self._check_directory()
        original_builtins = builtin_material_profiles()
        _check_id_set(original_builtins, "builtin catalogue")
        builtin_by_id = {item["id"].casefold(): item for item in original_builtins}
        deleted = self._read_tombstones()

        for path in self.directory.glob("*.json"):
            _reject_reparse(path, "custom material record")
        custom = list_materials(self.directory)
        _check_id_set(custom, "custom catalogue")
        reserved = set(builtin_by_id)
        if any(item["id"].casefold() in reserved or
               item["id"].lower().startswith("builtin_") for item in custom):
            raise ValueError("custom material shadows a builtin identifier")

        overrides = {}
        if self.override_directory.exists():
            for path in sorted(self.override_directory.glob("*.json")):
                _reject_reparse(path, "builtin override")
                record = self._read_record_file(path, "builtin override")
                key = record["id"].casefold()
                if key not in builtin_by_id:
                    raise ValueError("builtin override refers to an unknown builtin id")
                if record["id"] != builtin_by_id[key]["id"]:
                    raise ValueError("builtin override id does not match the original id")
                if path.name != record["id"] + ".json":
                    raise ValueError("builtin override filename does not match its id")
                if key in overrides:
                    raise ValueError("duplicate builtin override id")
                overrides[key] = record

        # A durable tombstone wins over a leftover override so an interrupted
        # delete can never resurrect the seed profile.
        effective_builtins = [deepcopy(overrides.get(item["id"].casefold(), item))
                              for item in original_builtins
                              if item["id"].casefold() not in deleted]
        return original_builtins, effective_builtins, custom, overrides, deleted

    def _catalog_snapshot(self):
        originals, builtins, custom, overrides, _ = self._catalog_state()
        return originals, builtins, custom, overrides

    def _records(self):
        """Return effective builtins and custom records (legacy private shape)."""
        _, builtins, custom, _ = self._catalog_snapshot()
        return builtins, custom

    def catalog_records(self):
        """Return detached effective records with origin and restore metadata."""
        with material_library_lock(self.directory):
            originals, builtins, custom, overrides, _ = self._catalog_state()
            overridden = set(overrides)
            return ([{"record": deepcopy(item), "builtin": True,
                      "overridden": item["id"].casefold() in overridden,
                      "readonly": True}
                     for item in builtins] +
                    [{"record": deepcopy(item), "builtin": False,
                      "overridden": False, "readonly": False}
                     for item in custom])

    def deleted_builtins(self):
        """List tombstoned builtin ids that still exist in the packaged seed."""
        with material_library_lock(self.directory):
            originals, _, _, _, deleted = self._catalog_state()
            rows = []
            for item in originals:
                key = item["id"].casefold()
                if key in deleted:
                    rows.append({"id": item["id"], "name": item["name"]})
            return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))

    def read(self, identifier):
        with material_library_lock(self.directory):
            _, builtins, custom, overrides, deleted = self._catalog_state()
            key = identifier.casefold() if isinstance(identifier, str) else None
            if key is not None and key in deleted:
                raise ValueError("builtin_material_deleted: " + str(identifier))
            for item in builtins:
                if item["id"] == identifier:
                    return {"record": deepcopy(item), "readonly": True,
                            "editable": True, "builtin": True,
                            "overridden": item["id"].casefold() in overrides,
                            "completeness": material_completeness(item)}
            for item in custom:
                if item["id"] == identifier:
                    return {"record": deepcopy(item), "readonly": False,
                            "editable": True, "builtin": False,
                            "overridden": False,
                            "completeness": material_completeness(item)}
            raise ValueError("material_not_found: " + str(identifier))

    def read_builtin_original(self, identifier):
        with material_library_lock(self.directory):
            originals, _, _, _ = self._catalog_snapshot()
            row = next((item for item in originals if item["id"] == identifier), None)
            if row is None:
                raise ValueError("builtin_material_not_found: " + str(identifier))
            return deepcopy(row)

    def list(self, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative; limit must be 1..100")
        records = self.catalog_records()
        rows = []
        for entry in records:
            item = entry["record"]
            rows.append({"id": item["id"], "name": item["name"],
                         "readonly": entry["readonly"],
                         "editable": entry["builtin"] or not entry["readonly"],
                         "builtin": entry["builtin"],
                         "overridden": entry["overridden"],
                         "roles": item.get("roles"),
                         "category": item.get("category"),
                         "optical_set_count": len(item.get("optical_constants", []))})
        return {"total": len(rows), "offset": offset, "items": rows[offset:offset + limit]}

    def write(self, profile, *, update=False):
        record = validate_material_profile(profile)
        self._check_directory()
        with material_library_lock(self.directory):
            return self._write_locked(record, update=update)

    def _write_locked(self, record, *, update):
        _, builtins, custom, _, deleted = self._catalog_state()
        identifier = record["id"]
        if identifier.casefold() in deleted:
            raise ValueError("builtin_material_deleted: restore the builtin before using its id")
        if identifier.lower().startswith("builtin_") or any(
                item["id"].casefold() == identifier.casefold() for item in builtins):
            raise ValueError("builtin_override_required: use save_builtin_override explicitly")
        matching = [item for item in custom if item["id"].casefold() == identifier.casefold()]
        if update and (not matching or matching[0]["id"] != identifier):
            raise ValueError("material_not_found: update requires an existing exact custom identifier")
        if not update and matching:
            raise ValueError("material_already_exists: use update_material explicitly")
        saved = save_material(self.directory, record, overwrite=update)
        return {"record": saved, "readonly": False, "editable": True,
                "builtin": False, "created": not update}

    def save_builtin_override(self, profile):
        record = validate_material_profile(profile)
        self._check_directory()
        with material_library_lock(self.directory):
            originals, _, _, _, deleted = self._catalog_state()
            if record["id"].casefold() in deleted:
                raise ValueError("builtin_material_deleted: restore the builtin before editing it")
            original = next((item for item in originals
                             if item["id"] == record["id"]), None)
            if original is None:
                raise ValueError("builtin_material_not_found: " + record["id"])
            path = self.override_directory / (record["id"] + ".json")
            self._atomic_write(path, _pretty_json_bytes(record))
        return {"record": deepcopy(record), "readonly": False,
                "editable": True, "builtin": True, "overridden": True}

    def delete_builtin(self, identifier):
        """Tombstone one builtin id so seed reload cannot resurrect it."""
        normalized = validate_material_identifier(identifier)
        self._check_directory()
        with material_library_lock(self.directory):
            return self._delete_builtin_locked(normalized)

    def _delete_builtin_locked(self, identifier):
        originals, _, _, overrides, deleted = self._catalog_state()
        original = next((item for item in originals if item["id"] == identifier), None)
        if original is None:
            raise ValueError("builtin_material_not_found: " + identifier)
        key = identifier.casefold()
        if key in deleted:
            raise ValueError("builtin_material_deleted: " + identifier)
        self._atomic_write(self.deleted_directory / (identifier + ".json"),
                           deleted_builtin_marker(identifier))
        had_override = key in overrides
        if had_override:
            path = self.override_directory / (identifier + ".json")
            _reject_reparse(path, "builtin override")
            path.unlink()
        return {"id": identifier, "deleted": True, "had_override": had_override,
                "name": original["name"]}

    def restore_builtin(self, identifier):
        self._check_directory()
        with material_library_lock(self.directory):
            originals, _, _, overrides, deleted = self._catalog_state()
            original = next((item for item in originals if item["id"] == identifier), None)
            if original is None:
                raise ValueError("builtin_material_not_found: " + str(identifier))
            key = identifier.casefold()
            if key in deleted:
                path = self.deleted_directory / (identifier + ".json")
                _reject_reparse(path, "deleted builtin marker")
                path.unlink()
            if key in overrides:
                path = self.override_directory / (identifier + ".json")
                _reject_reparse(path, "builtin override")
                path.unlink()
            return deepcopy(original)

    def copy(self, identifier, new_identifier, name):
        record = self.read(identifier)["record"]
        record.update(id=new_identifier, name=name)
        return self.write(record)

    def _all_effective_records(self):
        with material_library_lock(self.directory):
            builtins, custom = self._records()
            return builtins + custom

    def export_single_package(self, identifier):
        item = self.read(identifier)["record"]
        return {"kind": MATERIAL_PACKAGE_KIND, "version": PACKAGE_VERSION,
                "material": deepcopy(item)}

    def export_library_package(self, include_ids=None):
        records = self._all_effective_records()
        if include_ids is not None:
            if not isinstance(include_ids, (list, tuple)):
                raise ValueError("include_ids must be a list of material ids")
            if len(include_ids) > PACKAGE_MAX_RECORDS:
                raise ValueError("too many selected materials")
            requested = set()
            for identifier in include_ids:
                if not isinstance(identifier, str) or identifier.casefold() in requested:
                    raise ValueError("selected material ids must be unique strings")
                requested.add(identifier.casefold())
            by_id = {item["id"].casefold(): item for item in records}
            unknown = requested - set(by_id)
            if unknown:
                raise ValueError("selected material is no longer in the library")
            records = [item for item in records if item["id"].casefold() in requested]
        if not records:
            raise ValueError("select at least one material to export")
        if len(records) > PACKAGE_MAX_RECORDS:
            raise ValueError(f"material library export exceeds {PACKAGE_MAX_RECORDS} records")
        package = {"kind": LIBRARY_PACKAGE_KIND, "version": PACKAGE_VERSION,
                   "materials": [deepcopy(item) for item in records]}
        if len(_pretty_json_bytes(package)) > PACKAGE_MAX_BYTES:
            raise ValueError("material library export exceeds the package size limit")
        return package

    @staticmethod
    def parse_package_file(path):
        path = Path(path)
        _reject_reparse(path, "material import file")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > PACKAGE_MAX_BYTES:
            raise ValueError("material import file must be a regular file no larger than 64 MiB")
        try:
            value = json.loads(path.read_bytes().decode("utf-8"),
                               object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid material JSON package: {exc}") from exc
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError("material package must be a versioned JSON object")
        if value["version"] != PACKAGE_VERSION:
            raise ValueError(f"unsupported material package version: {value['version']}")
        kind = value.get("kind")
        if kind == MATERIAL_PACKAGE_KIND:
            if set(value) != {"kind", "version", "material"}:
                raise ValueError("single-material package has unknown or missing fields")
            records = [validate_material_profile(value["material"])]
        elif kind == LIBRARY_PACKAGE_KIND:
            if set(value) != {"kind", "version", "materials"}:
                raise ValueError("material-library package has unknown or missing fields")
            if not isinstance(value["materials"], list):
                raise ValueError("materials must be a JSON array")
            if not 1 <= len(value["materials"]) <= PACKAGE_MAX_RECORDS:
                raise ValueError(f"material library package must contain 1..{PACKAGE_MAX_RECORDS} records")
            records = [validate_material_profile(item) for item in value["materials"]]
        else:
            raise ValueError("unsupported material package kind")
        _check_id_set(records, "import package")
        if len(_pretty_json_bytes(value)) > PACKAGE_MAX_BYTES:
            raise ValueError("material package exceeds the 64 MiB size limit")
        return {"kind": kind, "records": records}

    def preview_import(self, package):
        if not isinstance(package, dict) or package.get("kind") not in {
                MATERIAL_PACKAGE_KIND, LIBRARY_PACKAGE_KIND}:
            raise ValueError("invalid material import package")
        records = package.get("records")
        if not isinstance(records, list) or not 1 <= len(records) <= PACKAGE_MAX_RECORDS:
            raise ValueError("material import package must contain 1..1000 records")
        records = [validate_material_profile(item) for item in records]
        _check_id_set(records, "import package")
        with material_library_lock(self.directory):
            originals, builtins, custom, _, deleted = self._catalog_state()
            for record in records:
                if record["id"].casefold() in deleted:
                    raise ValueError("builtin_material_deleted: restore the builtin before importing its id")
            current = {}
            builtin_ids = {item["id"].casefold() for item in originals}
            for record in builtins + custom:
                current[record["id"].casefold()] = record
            unavailable_copy_ids = set(current)
            unavailable_copy_ids.update(item["id"].casefold() for item in records)
            rows = []
            fingerprints = {}
            for record in records:
                identity = record["id"]
                key = identity.casefold()
                existing = current.get(key)
                conflict_type = ("builtin" if key in builtin_ids else "custom") if existing else ""
                copy_id = ""
                if existing:
                    while True:
                        copy_id = "material_" + uuid.uuid4().hex
                        if copy_id.casefold() not in unavailable_copy_ids:
                            unavailable_copy_ids.add(copy_id.casefold())
                            break
                fingerprints[identity] = _fingerprint(existing) if existing else None
                rows.append({"id": identity, "name": record["name"],
                             "conflict": conflict_type, "copy_id": copy_id,
                             "target_id": existing["id"] if existing else identity})
            plan = {"version": 1, "kind": package["kind"],
                    "records": records, "rows": rows,
                    "fingerprints": fingerprints}
        return {"rows": deepcopy(rows), "plan": plan,
                "count": len(records)}

    def apply_import(self, plan, decisions):
        if not isinstance(plan, dict) or plan.get("version") != 1:
            raise ValueError("invalid or expired import preview")
        records, rows = plan.get("records"), plan.get("rows")
        fingerprints = plan.get("fingerprints")
        if (not isinstance(records, list) or not isinstance(rows, list) or
                not isinstance(fingerprints, dict) or len(records) != len(rows)):
            raise ValueError("invalid import preview state")
        records = [validate_material_profile(item) for item in records]
        _check_id_set(records, "import preview")
        if not isinstance(decisions, dict):
            raise ValueError("import decisions must be an object")
        record_ids = {item["id"] for item in records}
        if set(decisions) - record_ids:
            raise ValueError("import decisions contain an unknown material id")

        with material_library_lock(self.directory):
            originals, builtins, custom, _, deleted = self._catalog_state()
            if any(record["id"].casefold() in deleted for record in records):
                raise ValueError("builtin_material_deleted: restore the builtin before importing its id")
            builtin_ids = {item["id"].casefold() for item in originals}
            current = {item["id"].casefold(): item for item in builtins + custom}
            operations = []
            results = {"created": 0, "overwritten": 0,
                       "copied": 0, "skipped": 0}
            used_targets = set()
            for record, row in zip(records, rows):
                if not isinstance(row, dict) or row.get("id") != record["id"]:
                    raise ValueError("import preview no longer matches its records")
                identity, key = record["id"], record["id"].casefold()
                existing = current.get(key)
                expected_fingerprint = fingerprints.get(identity)
                if ((existing is None) != (expected_fingerprint is None) or
                        (existing is not None and _fingerprint(existing) != expected_fingerprint)):
                    raise ValueError("material library changed after import preview; preview again")
                if existing is None:
                    if row.get("conflict") or row.get("copy_id"):
                        raise ValueError("import preview conflict state is invalid")
                    decision = decisions.get(identity, "import")
                    if decision != "import":
                        raise ValueError("non-conflicting materials can only be imported")
                    target = deepcopy(record)
                    target_path = self.directory / (target["id"] + ".json")
                    results["created"] += 1
                else:
                    conflict_type = "builtin" if key in builtin_ids else "custom"
                    if row.get("conflict") != conflict_type:
                        raise ValueError("material library conflict type changed; preview again")
                    decision = decisions.get(identity)
                    if decision == "skip":
                        results["skipped"] += 1
                        continue
                    if decision == "copy":
                        target_id = row.get("copy_id")
                        if (not isinstance(target_id, str) or
                                target_id.casefold() in current or
                                target_id.casefold() in used_targets):
                            raise ValueError("copy target id is no longer available; preview again")
                        target = deepcopy(record)
                        target["id"] = target_id
                        target["name"] = target["name"][:112].rstrip() + " (copy)"
                        target = validate_material_profile(target)
                        target_path = self.directory / (target_id + ".json")
                        results["copied"] += 1
                    elif decision == "overwrite":
                        target = deepcopy(record)
                        target["id"] = existing["id"]
                        target = validate_material_profile(target)
                        target_path = (self.override_directory / (target["id"] + ".json")
                                       if conflict_type == "builtin" else
                                       self.directory / (target["id"] + ".json"))
                        results["overwritten"] += 1
                    else:
                        raise ValueError("choose skip, copy, or overwrite for every conflict")
                if target_path in used_targets:
                    raise ValueError("multiple import rows resolve to the same material id")
                used_targets.add(target_path)
                operations.append((target_path, _pretty_json_bytes(target)))
            if operations:
                self._transactional_replace(operations)
        return results

    def export_single_file(self, identifier, path):
        package = self.export_single_package(identifier)
        return self.write_export_file(package, path)

    def export_library_file(self, path, include_ids=None):
        package = self.export_library_package(include_ids)
        return self.write_export_file(package, path)

    @staticmethod
    def write_export_file(package, path):
        if not isinstance(package, dict) or package.get("kind") not in PACKAGE_SUFFIXES:
            raise ValueError("unsupported material export package")
        payload = _pretty_json_bytes(package)
        if len(payload) > PACKAGE_MAX_BYTES:
            raise ValueError("material package exceeds the 64 MiB size limit")
        target = Path(path)
        _reject_reparse(target, "material export destination")
        if not target.parent.exists() or not target.parent.is_dir():
            raise ValueError("material export destination folder does not exist")
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".material-export-",
                                                 suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _reject_reparse(target, "material export destination")
            replace_with_retry(temporary, target)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return {"path": str(target), "kind": package["kind"],
                "bytes": len(payload)}

    @staticmethod
    def _atomic_write(path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_reparse(path.parent, "material library directory")
        _reject_reparse(path, "material record")
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".material-",
                                                 suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _reject_reparse(path, "material record")
            replace_with_retry(temporary, path)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @classmethod
    def _transactional_replace(cls, operations):
        staged = []
        originals = {}
        committed = []
        try:
            for target, payload in operations:
                target = Path(target)
                _reject_reparse(target, "material record")
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_reparse(target.parent, "material library directory")
                info = _reject_reparse(target, "material record")
                if info is not None and not stat.S_ISREG(info.st_mode):
                    raise ValueError("material destination is not a regular file")
                originals[target] = target.read_bytes() if info is not None else None
                descriptor, name = tempfile.mkstemp(prefix=".material-import-",
                                                     suffix=".tmp", dir=target.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                staged.append((temporary, target))
            for temporary, target in staged:
                _reject_reparse(target, "material record")
                replace_with_retry(temporary, target)
                committed.append(target)
            return
        except Exception as exc:
            rollback_errors = []
            for target in reversed(committed):
                try:
                    previous = originals[target]
                    if previous is None:
                        target.unlink(missing_ok=True)
                    else:
                        cls._atomic_write(target, previous)
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise OSError(f"material import failed ({exc}); rollback failed: " +
                              "; ".join(rollback_errors)) from exc
            raise
        finally:
            for temporary, _ in staged:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class ApplicationMaterialCatalog:
    """Read-only global material view that never creates a writer lock."""
    def __init__(self, directory):
        self._library = MaterialLibrary(directory=directory)

    def catalog_records(self):
        _, builtins, custom, overrides = self._library._catalog_snapshot()
        overridden = set(overrides)
        return ([{"record": deepcopy(item), "builtin": True,
                  "overridden": item["id"].casefold() in overridden, "readonly": True}
                 for item in builtins] +
                [{"record": deepcopy(item), "builtin": False,
                  "overridden": False, "readonly": True} for item in custom])

    def read(self, identifier):
        for item in self.catalog_records():
            record = item["record"]
            if record["id"] == identifier:
                return {**item, "record": deepcopy(record), "editable": True,
                        "completeness": material_completeness(record)}
        raise ValueError("material_not_found: " + str(identifier))


def application_material_library():
    """Construct a read-only view for the application-wide catalog."""
    from .data_files import data_path
    return ApplicationMaterialCatalog(data_path("materials"))
