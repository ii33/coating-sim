"""Bounded read-only global resource snapshots and explicit exchange publication.

The catalogue is a detached project-shaped graph, never an active project.
Only the five established typed libraries, the read-only material catalogue,
and the established film SQLite library are sources. Typed exchanges publish
one exclusive directory; material exchanges use MaterialLibrary's rollback
transaction. Film exchanges require already-satisfied material dependencies,
so no cross-storage atomicity is implied.
"""
from __future__ import annotations

from contextlib import closing, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PureWindowsPath
import shutil
import sqlite3
import stat
import tempfile
import uuid

from . import data_files
from .design_files import design_kind_from_path
from .material_business import MaterialLibrary, application_material_library
from .materials import default_material_profile, material_library_lock, validate_material_profile
from .project_dependencies import (
    EXTENSIONS, MAX_FILE_BYTES, MAX_PROJECT_BYTES, MAX_PROJECT_DOCUMENTS,
    ProjectError, file_references, identifier, json_bytes, normalize_document,
    parse_json, read_json, reject_links,
)
from .project_store import ProjectStore, _manifest, _rename_directory_no_replace
from .project_simulation_resources import GLOBAL_SIMULATION_FOLDERS, SIMULATION_EXTENSIONS

_NAMESPACE = uuid.UUID("03693c8e-4549-45b1-80fa-0fa2ea466054")
_GLOBAL_ID = uuid.uuid5(_NAMESPACE, "global-library").hex
_GENERATION = uuid.uuid5(_NAMESPACE, "global-generation").hex
MAX_SCAN_ENTRIES = 20000
MAX_SCAN_DEPTH = 32
_STAGING_PREFIX = ".exchange-staging-"


def _identity(source: str) -> str:
    return uuid.uuid5(_NAMESPACE, source).hex


def _ordinary(path: Path, *, directory=False):
    reject_links(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    test = stat.S_ISDIR if directory else stat.S_ISREG
    if not test(info.st_mode):
        raise ProjectError("unsafe_path", "Global resource path has an unexpected file type: " + str(path))
    return info


def _roots() -> dict[str, Path]:
    return {kind: data_files.data_path(folder) for kind, folder in
            {**data_files.DESIGN_FOLDERS, **GLOBAL_SIMULATION_FOLDERS}.items()}


def _typed_paths(roots):
    count = 0

    def visit(directory, depth):
        nonlocal count
        if depth > MAX_SCAN_DEPTH:
            raise ProjectError("capacity_exceeded", "Global resource library is nested too deeply")
        for entry in sorted(directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
            count += 1
            if count > MAX_SCAN_ENTRIES:
                raise ProjectError("capacity_exceeded", "Global resource directory scan exceeds its safety limit")
            reject_links(entry)
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                # Private exchanges and recoverable originals are not active
                # assets. Do not scan archive payloads or spend catalog budget
                # on repeated delete/restore history.
                if not entry.name.startswith(_STAGING_PREFIX) and entry.name != ".asset-recycle":
                    yield from visit(entry, depth + 1)
            elif stat.S_ISREG(info.st_mode):
                kind = design_kind_from_path(entry)
                if kind is None:
                    kind = next((key for key, suffix in SIMULATION_EXTENSIONS.items()
                                 if entry.suffix.casefold() == suffix), None)
                if kind in {*data_files.DESIGN_FOLDERS, *SIMULATION_EXTENSIONS}:
                    yield entry, kind
            elif design_kind_from_path(entry) is not None:
                raise ProjectError("unsafe_path", "Global design is not a regular file: " + str(entry))

    for root in roots.values():
        if _ordinary(root, directory=True) is not None:
            yield from visit(root, 0)


def _optical(profile):
    return normalize_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1,
        "material": {"id": profile["id"], "name": profile["name"],
                     "notes": profile.get("notes", ""),
                     "optical_sets": deepcopy(profile.get("optical_constants", []))},
    })


def _sqlite_rows(path):
    info = _ordinary(path)
    if info is None:
        return
    if info.st_size > MAX_PROJECT_BYTES:
        raise ProjectError("capacity_exceeded", "Global film library exceeds the database safety limit")
    # mode=ro is essential: a read must never create a database or schema.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for kind, table, key, column in (
            ("film", "film_designs", "design_id", "document_json"),
            ("expression_template", "film_expression_templates", "template_id", "expression_json"),
        ):
            if table not in tables:
                continue
            rows = connection.execute(
                f"SELECT {key},length(CAST({column} AS BLOB)) FROM {table} ORDER BY {key} COLLATE BINARY LIMIT ?",
                (MAX_PROJECT_DOCUMENTS + 1,),
            ).fetchall()
            if len(rows) > MAX_PROJECT_DOCUMENTS:
                raise ProjectError("capacity_exceeded", "Global film library has too many resources")
            for business_id, size in rows:
                if not isinstance(business_id, str) or not business_id or size is None:
                    raise ProjectError("invalid_document", "Global film library contains an invalid resource identity")
                if size > MAX_FILE_BYTES:
                    raise ProjectError("capacity_exceeded", "Global film document exceeds its safety limit")
                raw = connection.execute(f"SELECT {column} FROM {table} WHERE {key}=? COLLATE BINARY", (business_id,)).fetchone()[0]
                if not isinstance(raw, str):
                    raise ProjectError("invalid_document", "Global film library document must be JSON text")
                data = parse_json(raw.encode("utf-8"))
                if not isinstance(data, dict) or data.get("id" if kind == "film" else "template_id") != business_id:
                    raise ProjectError("dependency_conflict", "Stored film resource identity differs from its database key")
                # Legacy valid layers may omit IDs; normalization must still be
                # stable across reads rather than allocating fresh random IDs.
                if kind == "film":
                    for field in ("front_layers", "back_layers"):
                        layers = data.get(field, [])
                        if isinstance(layers, list):
                            for index, layer in enumerate(layers):
                                if isinstance(layer, dict) and "id" not in layer:
                                    layer["id"] = _identity(f"film:{business_id}:{field}:{index}")
                yield kind, business_id, normalize_document(kind, data)


def read_global_resources() -> dict:
    """Capture every supported global resource without creating any files."""
    try:
        roots = _roots()
        boundary = data_files.data_path("data")
        records: dict[str, dict] = {}
        documents: dict[str, dict] = {}
        encoded_sizes: dict[str, int] = {}
        source_paths: dict[Path, str] = {}
        total = 0
        asset_sources = {}

        def add(identity, kind, data):
            nonlocal total
            if identity in records:
                raise ProjectError("dependency_conflict", "Global resource identity is duplicated")
            data = normalize_document(kind, data)
            content = json_bytes(data)
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_PROJECT_BYTES or len(records) >= MAX_PROJECT_DOCUMENTS:
                raise ProjectError("capacity_exceeded", "Global resource snapshot exceeds its safety capacity")
            records[identity] = {"id": identity, "kind": kind,
                                 "path": f"objects/{_GENERATION}/{identity}{EXTENSIONS[kind]}",
                                 "sha256": hashlib.sha256(content).hexdigest(), "dependencies": []}
            documents[identity] = data
            encoded_sizes[identity] = len(content)

        for path, kind in _typed_paths(roots):
            identity = _identity("file:" + path.relative_to(boundary).as_posix())
            data, _ = read_json(path)
            add(identity, kind, data)
            source_paths[path] = identity
        for item in application_material_library().catalog_records():
            profile = validate_material_profile(item["record"])
            add(_identity("material_profile:" + profile["id"]), "material_profile", {
                "kind": "coat_sim.project_material_profile", "version": 1, "material": profile})
            add(_identity("optical_material:" + profile["id"]), "optical_material", _optical(profile))
        for kind, business_id, data in _sqlite_rows(data_files.data_path("film_design/library.sqlite3")):
            add(_identity("sqlite:film_design/library.sqlite3:" + kind + ":" + business_id), kind, data)

        for path, identity in source_paths.items():
            rebound = False
            if records[identity]["kind"] == "binary_asset":
                from .project_binary_assets import file_signature, ROLE_SUFFIXES
                metadata = documents[identity]
                payload_path = path.parent / metadata["payload_path"]
                if file_signature(payload_path)[3] != metadata["size"]:
                    raise ProjectError("integrity_error", "Global binary input has the wrong size")
                asset_sources[identity] = {"path": str(payload_path)}
                managed_payload = f"assets/{_GENERATION}/{identity}{ROLE_SUFFIXES[metadata['role']]}"
                if metadata["payload_path"] != managed_payload:
                    metadata["payload_path"] = managed_payload
                    rebound = True
            for owner, field, expected_kind in file_references(records[identity]["kind"], documents[identity]):
                reference = owner[field]
                if not isinstance(reference, str) or (os.name != "nt" and PureWindowsPath(reference).drive):
                    raise ProjectError("unsafe_path", "Global resource reference has an unsupported path")
                target = Path(reference)
                target = Path(os.path.abspath(target if target.is_absolute() else path.parent / target))
                reject_links(target)
                if not any(target.is_relative_to(root) for root in roots.values()):
                    raise ProjectError("unsafe_path", "Global resource reference leaves the typed libraries")
                dependency = source_paths.get(target)
                if dependency is None:
                    raise ProjectError("missing_dependency", "Global resource dependency is missing: " + str(target))
                if records[dependency]["kind"] != expected_kind:
                    raise ProjectError("dependency_conflict", "Global resource dependency has the wrong type")
                filename = dependency + EXTENSIONS[expected_kind]
                if owner[field] != filename:
                    owner[field] = filename
                    rebound = True
            if rebound:
                # Size and digest share one canonical encoding. Only a row
                # whose references changed invalidates this call-local value.
                content = json_bytes(documents[identity])
                size = len(content)
                if size > MAX_FILE_BYTES:
                    raise ProjectError("capacity_exceeded", "Normalized global resource document exceeds its safety capacity")
                total += size - encoded_sizes[identity]
                encoded_sizes[identity] = size
                records[identity]["sha256"] = hashlib.sha256(content).hexdigest()
        materials = ProjectStore._material_index(records, documents)
        for identity, row in records.items():
            row["dependencies"] = ProjectStore._dependencies(identity, records, documents, materials)
        ProjectStore._validate_graph(records, documents)
        if total > MAX_PROJECT_BYTES:
            raise ProjectError("capacity_exceeded", "Normalized global resource snapshot exceeds its safety capacity")
        snapshot = {"manifest": {"kind": "coat_sim.project", "version": 1, "id": _GLOBAL_ID,
                             "name": "全局资源库", "revision": 0,
                             "documents": [records[key] for key in sorted(records)],
                             "default_configuration_id": None},
                "documents": {key: documents[key] for key in sorted(documents)}}
        if asset_sources:
            snapshot["assets"] = asset_sources
        return snapshot
    except ProjectError:
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        raise ProjectError("global_library_error", "Cannot read global resource library: " + str(exc)) from exc


def _validated(snapshot):
    if not isinstance(snapshot, dict) or not {"manifest", "documents"}.issubset(snapshot) or set(snapshot) - {"manifest", "documents", "assets"}:
        raise ProjectError("invalid_document", "Invalid global exchange snapshot")
    manifest = _manifest(snapshot["manifest"])
    records = {row["id"]: row for row in manifest["documents"]}
    data = snapshot["documents"]
    if not isinstance(data, dict) or set(data) != set(records):
        raise ProjectError("invalid_document", "Global exchange documents differ from their manifest")
    total = 0
    for identity, row in records.items():
        normalized = normalize_document(row["kind"], data[identity])
        content = json_bytes(normalized)
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_PROJECT_BYTES:
            raise ProjectError("capacity_exceeded", "Global exchange exceeds its safety capacity")
        if normalized != data[identity] or hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ProjectError("integrity_error", "Global exchange snapshot is not normalized or its digest differs")
    ProjectStore._validate_graph(records, data)
    return records, data


def _check_before(before):
    if read_global_resources() != before:
        raise ProjectError("stale_document", "Global resources changed after exchange preview; preview again")


def _closure(records, data, selected):
    found, visiting = set(), set()
    optics = {data[key]["material"]["id"]: key for key, row in records.items()
              if row["kind"] == "optical_material"}

    def visit(identity):
        if identity in visiting:
            raise ProjectError("dependency_conflict", "Global exchange dependency cycle")
        if identity in found:
            return
        visiting.add(identity)
        for dependency in records[identity]["dependencies"]:
            visit(dependency)
        if records[identity]["kind"] == "material_profile":
            companion = optics.get(data[identity]["material"]["id"])
            if companion is not None:
                visit(companion)
        visiting.remove(identity)
        found.add(identity)

    for identity in selected:
        visit(identity)
    return found


def _export_typed(before, records, data, closure, kind, asset_sources=None, publication_scope=None):
    if any(records[identity]["kind"] not in {*data_files.DESIGN_FOLDERS, *GLOBAL_SIMULATION_FOLDERS, "binary_asset"} for identity in closure):
        raise ProjectError("unsupported_transfer", "Typed design exchange cannot publish a non-file resource dependency")
    folder = _roots()[kind]
    reject_links(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _ordinary(folder, directory=True)
    temporary = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=folder))
    destination = folder / ("exchange-" + uuid.uuid4().hex)
    names = []
    try:
        # A detached export uses a single flat directory even when the target
        # snapshot has multiple project generations.
        for identity in sorted(closure):
            document = deepcopy(data[identity])
            for owner, field, dependency_kind in file_references(records[identity]["kind"], document):
                owner[field] = Path(owner[field]).stem + EXTENSIONS[dependency_kind]
            name = identity + EXTENSIONS[records[identity]["kind"]]
            with (temporary / name).open("xb") as stream:
                stream.write(json_bytes(document))
                stream.flush()
                os.fsync(stream.fileno())
            names.append(name)
            if records[identity]["kind"] == "binary_asset":
                from .project_binary_assets import capture_asset
                source = (asset_sources or {}).get(identity)
                if source is None:
                    raise ProjectError("missing_dependency", "Global export has no binary input source")
                payload = temporary / document["payload_path"]
                payload.parent.mkdir(parents=True, exist_ok=True)
                capture_asset(source, document, payload)
                names.append(document["payload_path"])
        with publication_scope() if publication_scope is not None else nullcontext():
            _check_before(before)
            reject_links(folder)
            _rename_directory_no_replace(temporary, destination)
        return {"directory": str(destination), "files": [str(destination / name) for name in names]}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _material_records(before_records, before_data, records, data, changed, closure):
    current = {before_data[key]["material"]["id"].casefold(): before_data[key]["material"]
               for key, row in before_records.items() if row["kind"] == "material_profile"}
    grouped: dict[str, dict] = {}
    for identity in sorted(changed & closure):
        kind = records[identity]["kind"]
        if kind not in ("material_profile", "optical_material"):
            raise ProjectError("unsupported_transfer", "Material exchange can only publish material records")
        value = data[identity]["material"]
        key = value["id"].casefold()
        existing = current.get(key)
        matching = next((old_id for old_id, row in before_records.items()
                         if row["kind"] == kind and before_data[old_id]["material"]["id"].casefold() == key), None)
        if existing is not None and matching != identity:
            raise ProjectError("dependency_conflict", "Existing global material requires an explicit mapped overwrite")
        grouped.setdefault(key, {})[kind] = value
    proposed = []
    for key, values in grouped.items():
        profile = deepcopy(values.get("material_profile", current.get(key)))
        optical = values.get("optical_material")
        if profile is None:
            if optical is None:
                raise ProjectError("invalid_document", "Material exchange has no profile or optical record")
            profile = default_material_profile(optical["name"], id=optical["id"], optical_only=True)
        if optical is not None:
            if "material_profile" in values and _optical(profile)["material"] != optical:
                raise ProjectError("dependency_conflict", "Material profile and optical exchange records disagree")
            profile.update(name=optical["name"], notes=optical.get("notes", ""),
                           optical_constants=deepcopy(optical["optical_sets"]))
        proposed.append(validate_material_profile(profile))
    return proposed


def _export_materials(before, before_records, before_data, records, data, changed, closure):
    profiles = _material_records(before_records, before_data, records, data, changed, closure)
    if not profiles:
        _check_before(before)
        return {"files": []}
    library = MaterialLibrary(directory=data_files.data_path("materials"))
    reject_links(library.directory)
    with material_library_lock(library.directory):
        _check_before(before)
        originals, _, _, _ = library._catalog_snapshot()
        builtin = {row["id"].casefold() for row in originals}
        files = [str((library.override_directory if profile["id"].casefold() in builtin else library.directory)
                     / (profile["id"] + ".json")) for profile in profiles]
        package = {"kind": "coat_sim.material_library", "records": profiles}
        preview = library.preview_import(package)
        decisions = {row["id"]: "overwrite" if row["conflict"] else "import" for row in preview["rows"]}
        # Only the changed rows are in this transaction. Generic conflict
        # handling already explicitly mapped each pre-existing target UUID.
        library.apply_import(preview["plan"], decisions)
        return {"files": files}


def _export_sqlite(before, before_records, before_data, records, data, changed, closure, kind):
    if any(records[identity]["kind"] not in (kind, "optical_material") for identity in closure):
        raise ProjectError("unsupported_transfer", "Film exchange has unsupported non-optical dependencies")
    material_changes = [identity for identity in changed & closure if records[identity]["kind"] == "optical_material"]
    if material_changes:
        raise ProjectError("unsupported_transfer", "Publish the required optical materials first, then preview the film exchange again; the film database and material files cannot be committed atomically")
    ids = sorted(changed & closure)
    if not ids:
        _check_before(before)
        return {"files": []}
    path = data_files.data_path("film_design/library.sqlite3")
    _ordinary(path)
    # Existing dependencies must match by business identity as well as UUID.
    for identity in closure:
        if records[identity]["kind"] == "optical_material" and (
                identity not in before_records or before_data[identity] != data[identity]):
            raise ProjectError("unsupported_transfer", "Film exchange requires already-saved global optical material records")
    key = "id" if kind == "film" else "template_id"
    for identity in ids:
        business_id = data[identity][key]
        matching = next((old_id for old_id, row in before_records.items() if row["kind"] == kind
                         and before_data[old_id][key] == business_id), None)
        if matching is not None and matching != identity:
            raise ProjectError("dependency_conflict", "Existing global film resource requires an explicit mapped overwrite")
    table, database_key, column = (("film_designs", "design_id", "document_json") if kind == "film"
                                   else ("film_expression_templates", "template_id", "expression_json"))
    _check_before(before)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    try:
        with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _check_before(before)
                connection.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                    {database_key} TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, {column} TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                connection.execute(f"CREATE INDEX IF NOT EXISTS {table}_updated ON {table}(updated_at DESC)")
                stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                for identity in ids:
                    value = data[identity]
                    connection.execute(f"""INSERT INTO {table}
                        ({database_key},name,note,schema_version,{column},created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT({database_key}) DO UPDATE SET
                        name=excluded.name,note=excluded.note,schema_version=excluded.schema_version,
                        {column}=excluded.{column},updated_at=excluded.updated_at""",
                        (value[key], value["name"], value["note"], value["schema_version"],
                         json_bytes(value).decode("utf-8"), stamp, stamp))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
    except Exception:
        if not existed and path.exists() and path.stat().st_size == 0:
            path.unlink()
        raise
    return {"files": [str(path)]}


def write_global_resources(before_snapshot, after_snapshot, changed_ids: list[str], selected_ids: list[str], *, publication_scope=None) -> dict:
    """Publish one root kind after explicit generic conflict handling.

    ``changed_ids`` names real creations/updates. Unchanged material/SQLite
    rows are never re-saved. Typed files are copied as a complete flat closure
    into one new exchange directory, preserving all prior library contents.
    """
    try:
        before_records, before_data = _validated(before_snapshot)
        records, data = _validated(after_snapshot)
        if (not isinstance(selected_ids, list) or not selected_ids or
                len(selected_ids) != len(set(selected_ids)) or
                not isinstance(changed_ids, list) or len(changed_ids) != len(set(changed_ids))):
            raise ProjectError("invalid_selection", "Select one or more unique global exchange roots")
        for identity in selected_ids + changed_ids:
            identifier(identity)
            if identity not in records:
                raise ProjectError("invalid_selection", "Selected exchange resource is absent")
        kinds = {records[identity]["kind"] for identity in selected_ids}
        if len(kinds) != 1:
            raise ProjectError("invalid_selection", "One global exchange can publish only one root resource kind")
        kind = next(iter(kinds))
        closure = _closure(records, data, selected_ids)
        changed = set(changed_ids)
        if changed - closure:
            raise ProjectError("invalid_selection", "Changed resources must belong to the selected closure")
        _check_before(before_snapshot)
        if kind in {*data_files.DESIGN_FOLDERS, *GLOBAL_SIMULATION_FOLDERS}:
            result = _export_typed(before_snapshot, records, data, closure, kind, after_snapshot.get("assets"), publication_scope)
        elif kind in ("material_profile", "optical_material"):
            with publication_scope() if publication_scope is not None else nullcontext():
                result = _export_materials(before_snapshot, before_records, before_data, records, data, changed, closure)
        elif kind in ("film", "expression_template"):
            with publication_scope() if publication_scope is not None else nullcontext():
                result = _export_sqlite(before_snapshot, before_records, before_data, records, data, changed, closure, kind)
        else:
            raise ProjectError("unsupported_transfer", "This resource kind has no global library destination")
        return {**result, "kind": kind, "selected_ids": list(selected_ids), "changed_ids": list(changed_ids)}
    except ProjectError:
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        raise ProjectError("global_library_error", "Cannot publish global resources: " + str(exc)) from exc
