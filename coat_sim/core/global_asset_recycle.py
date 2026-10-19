"""Reversible, one-physical-asset mutations of the application-wide library.

Plans are detached capabilities only after they have been re-created at apply.
Files are exclusively renamed on their own filesystem; SQLite keeps the exact
row and deletion in one transaction. No path is ever permanently deleted.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from . import data_files
from .material_business import MaterialLibrary, deleted_builtin_marker_sha256
from .materials import material_library_lock
from .project_dependencies import EXTENSIONS, MAX_FILE_BYTES, MAX_PROJECT_BYTES, ProjectError, json_bytes, normalize_document, reject_links
from . import project_global_resources as resources
from .project_store import _rename_directory_no_replace

_ARCHIVE_TABLE = "global_asset_recycle"
_RECEIPT_KIND = "coat_sim.global_asset_recycle"
_SQL = {"film": ("film_designs", "design_id"),
        "expression_template": ("film_expression_templates", "template_id")}
_SQL_COLUMNS = {"film": ["design_id", "name", "note", "schema_version", "document_json", "created_at", "updated_at"],
                "expression_template": ["template_id", "name", "note", "schema_version", "expression_json", "created_at", "updated_at"]}
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")


class GlobalDeletionUncertain(ProjectError):
    """The operation could not safely roll back; originals remain recoverable."""
    def __init__(self, receipt, cause):
        super().__init__("deletion_uncertain", "Recycle operation needs recovery at " + receipt["recovery_path"] + ": " + str(cause))
        self.receipt_id = receipt["receipt_id"]
        self.recovery_path = receipt["recovery_path"]
        self.recovery_paths = [receipt["recovery_path"]]
        if "archive" in receipt:
            self.recovery_paths.append(str(Path(receipt["owner_root"]) / receipt["archive"]))
        self.uncertain = True


def _translate_errors(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ProjectError:
            raise
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            raise ProjectError("global_library_error", "Global recycle operation failed: " + str(exc)) from exc
    return call


def _digest(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _global_path(value):
    try:
        return data_files.data_path(value)
    except ValueError as exc:
        raise ProjectError("unsafe_path", "Invalid global recycle path: " + str(exc)) from exc


def _root():
    return _global_path("data")


def _index():
    return _global_path(".asset-recycle")


def _path(relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ProjectError("unsafe_path", "Invalid archive-relative path")
    try:
        result = data_files.data_path(relative, root_allowed=False)
    except ValueError as exc:
        raise ProjectError("unsafe_path", "Invalid global archive path: " + str(exc)) from exc
    reject_links(result)
    return result


def _relative(path):
    reject_links(path)
    return path.relative_to(_root()).as_posix()


def _ordinary(path, *, directory=False):
    reject_links(path)
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ProjectError("unsafe_path", "Recycle paths must be ordinary files or directories: " + str(path))
    return info


def _capture(path):
    before = _ordinary(path)
    if before.st_size > MAX_FILE_BYTES:
        raise ProjectError("capacity_exceeded", "Recycle file exceeds the document safety limit")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ProjectError("stale_source", "Resource changed while opening")
        content = stream.read(MAX_FILE_BYTES + 1)
    after = _ordinary(path)
    def signature(item):
        return [item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns]
    if len(content) > MAX_FILE_BYTES or signature(before) != signature(after):
        raise ProjectError("stale_source", "Resource changed while reading")
    return {"signature": signature(after), "sha256": hashlib.sha256(content).hexdigest()}


def _same_archived(actual, expected):
    # rename changes ctime, but must preserve the inode, bytes, mode and mtime.
    return actual["signature"][:-1] == expected["signature"][:-1] and actual["sha256"] == expected["sha256"]


def _check_before(snapshot):
    _check_sql_paths()
    records, documents = resources._validated(snapshot)
    if snapshot.get("manifest", {}).get("id") != resources._GLOBAL_ID or resources.read_global_resources() != snapshot:
        raise ProjectError("stale_source", "Global resources changed; prepare deletion again")
    return records, documents


def _sqlite_path():
    return _global_path("film_design/library.sqlite3")


def _reject_sql_triggers(connection):
    # A trigger can mutate unrelated rows/tables while DELETE, INSERT or a
    # receipt UPDATE runs. A selected-row archive cannot reverse those effects.
    # Reject all triggers rather than trying to infer a trigger body's safety.
    for schema in ("main", "temp"):
        if connection.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ProjectError("unsupported_schema", "Global recycling does not support databases with SQL triggers")


def _begin_sql_mutation(connection):
    connection.execute("BEGIN IMMEDIATE")
    # Recheck after acquiring the write reservation: another connection could
    # have installed a trigger after _connect_sql's early schema inspection.
    _reject_sql_triggers(connection)


def _sql_row(connection, kind, business_id):
    _reject_sql_triggers(connection)
    table, key = _SQL[kind]
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    if columns != _SQL_COLUMNS[kind]:
        raise ProjectError("unsupported_schema", "Global film table has an unsupported schema")
    row = connection.execute(f"SELECT * FROM {table} WHERE {key}=? COLLATE BINARY", (business_id,)).fetchone()
    if row is None:
        raise ProjectError("stale_source", "Global film row disappeared")
    values = list(row)
    if any(value is not None and type(value) not in (str, int, float) for value in values):
        raise ProjectError("unsupported_schema", "Global film row contains unsupported SQL values")
    return {"columns": columns, "values": values}


def _check_sql_paths():
    path = _sqlite_path()
    reject_links(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        reject_links(sidecar)
        if sidecar.exists():
            _ordinary(sidecar)
    return path


def _connect_sql(*, writable=False):
    path = _check_sql_paths()
    _ordinary(path)
    # Never let a restore create a replacement empty database.
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=10)
    if writable:
        try:
            _reject_sql_triggers(connection)
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
    return connection


def _incoming(records, affected, default):
    blocked = []
    for identity, row in records.items():
        hits = sorted(set(row["dependencies"]) & set(affected))
        if identity not in affected and hits:
            blocked.append({"code": "incoming_dependency", "id": identity, "affected_ids": hits})
    if default in affected:
        blocked.append({"code": "default_resource", "id": default})
    return blocked


@_translate_errors
def prepare_global_deletion(snapshot, selected_ids):
    """Resolve one physical asset, report blockers, and fingerprint its source."""
    records, documents = _check_before(snapshot)
    if not isinstance(selected_ids, (list, tuple)) or not selected_ids or len(selected_ids) > 2:
        raise ProjectError("invalid_selection", "Choose exactly one physical global asset")
    if any(not isinstance(identity, str) or identity not in records for identity in selected_ids) or len(set(selected_ids)) != len(selected_ids):
        raise ProjectError("invalid_selection", "Global deletion IDs must be known and unique")
    selected = sorted(selected_ids)
    first = selected[0]
    kind = records[first]["kind"]
    affected = selected[:]
    plan = {"scope": "global", "owner_root": str(_root()), "selected_ids": selected,
            "affected_ids": affected, "blockers": [], "backend": "protected", "action": "delete_asset",
            "snapshot_sha256": _digest(snapshot), "kind": kind}
    material_kinds = {"material_profile", "optical_material"}
    if kind in material_kinds:
        business_id = documents[first]["material"]["id"]
        affected = sorted(identity for identity, row in records.items() if row["kind"] in material_kinds
                          and documents[identity]["material"]["id"] == business_id)
        if not set(selected) <= set(affected):
            raise ProjectError("invalid_selection", "Choose one material and its paired projection only")
        plan["affected_ids"] = affected
        library = MaterialLibrary(directory=data_files.data_path("materials"))
        if library.directory != data_files.data_path("materials"):
            raise ProjectError("unsafe_path", "Material library is outside the global scope")
        item = next((item for item in library.catalog_records() if item["record"]["id"] == business_id), None)
        if item is None:
            raise ProjectError("stale_source", "Material no longer exists")
        plan["business_id"] = business_id
        if item["builtin"]:
            # A builtin is user-deletable: the seed file stays intact and a
            # durable tombstone makes the id stay absent. Any existing override
            # is archived so restore returns exactly the prior state.
            plan["action"] = "delete_builtin"
            plan["backend"] = "material_tombstone"
            plan["tombstone"] = "materials/.deleted/" + business_id + ".json"
            # The tombstone payload is deterministic, so the prepared receipt can
            # carry the expected fingerprint before the marker exists. A process
            # interruption between the prepared write and delete_builtin then
            # stays readable and recoverable instead of failing validation.
            plan["tombstone_sha256"] = deleted_builtin_marker_sha256(business_id)
            if item["overridden"]:
                source = library.override_directory / (business_id + ".json")
                plan.update(source=_relative(source), fingerprint=_capture(source))
        else:
            matches = [path for path in library.directory.glob("*.json")
                       if library._read_record_file(path)["id"] == business_id]
            if len(matches) != 1:
                raise ProjectError("stale_source", "Custom material file is not uniquely owned")
            source = matches[0]
            plan.update(backend="material_file", action="delete_asset",
                        source=_relative(source), fingerprint=_capture(source))
    elif len(selected) != 1:
        raise ProjectError("invalid_selection", "Choose exactly one physical global asset")
    elif kind in _SQL:
        business_id = documents[first]["id" if kind == "film" else "template_id"]
        expected_id = resources._identity("sqlite:film_design/library.sqlite3:" + kind + ":" + business_id)
        if first != expected_id:
            raise ProjectError("invalid_selection", "Unknown global SQL identity")
        with closing(_connect_sql()) as connection:
            row = _sql_row(connection, kind, business_id)
        plan.update(backend="sqlite", business_id=business_id, row=row)
    elif kind == "binary_asset":
        plan["blockers"].append({"code": "unsupported_payload_ownership", "id": first})
    elif kind in {*data_files.DESIGN_FOLDERS, *resources.GLOBAL_SIMULATION_FOLDERS}:
        matches = [path for path, found_kind in resources._typed_paths(resources._roots())
                   if found_kind == kind and resources._identity("file:" + _relative(path)) == first]
        if len(matches) != 1:
            raise ProjectError("stale_source", "Global typed file is not uniquely owned")
        plan.update(backend="typed_file", source=_relative(matches[0]), fingerprint=_capture(matches[0]))
    else:
        raise ProjectError("unsupported_deletion", "Global resource kind has no reversible deletion backend")
    plan["blockers"].extend(_incoming(records, plan["affected_ids"], snapshot["manifest"]["default_configuration_id"]))
    return plan


def _expected_after(snapshot, plan):
    result = deepcopy(snapshot)
    affected = set(plan["affected_ids"])
    if plan["action"] == "revert_builtin_override":
        original = MaterialLibrary(directory=data_files.data_path("materials")).read_builtin_original(plan["business_id"])
        by_kind = {"material_profile": normalize_document("material_profile", {
            "kind": "coat_sim.project_material_profile", "version": 1, "material": original}),
                   "optical_material": resources._optical(original)}
        for row in result["manifest"]["documents"]:
            if row["id"] in affected:
                result["documents"][row["id"]] = by_kind[row["kind"]]
                row["sha256"] = _digest(by_kind[row["kind"]])
    else:
        result["manifest"]["documents"] = [row for row in result["manifest"]["documents"] if row["id"] not in affected]
        for identity in affected:
            result["documents"].pop(identity)
            result.get("assets", {}).pop(identity, None)
    return result


def _fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_receipt(receipt):
    folder = _index() / receipt["receipt_id"]
    _ordinary(folder, directory=True)
    destination = folder / "receipt.json"
    reject_links(destination)
    if destination.exists():
        _ordinary(destination)
    temporary = folder / ("receipt-" + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(json_bytes(receipt))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    _fsync_directory(folder)


def _public(receipt):
    result = {key: deepcopy(receipt[key]) for key in (
        "scope", "owner_root", "receipt_id", "recovery_path", "affected_ids", "action", "backend", "state", "created_at")}
    # Only an acknowledged active receipt is an ordinary Undo candidate.
    # Interrupted/uncertain records retain their paths for explicit recovery.
    result["recoverable"] = receipt["state"] == "active"
    return result


def _uncertain(receipt, exc):
    receipt["state"] = "uncertain"
    try:
        _write_receipt(receipt)
    except (OSError, ProjectError):
        pass
    if receipt["backend"] == "sqlite":
        try:
            with closing(_connect_sql(writable=True)) as connection:
                with connection:
                    _begin_sql_mutation(connection)
                    _set_sql_receipt(connection, receipt)
        except (OSError, sqlite3.Error, ProjectError):
            pass
    return GlobalDeletionUncertain(receipt, exc)


def _new_receipt(snapshot, plan, expected):
    identity = uuid.uuid4().hex
    folder = _index() / identity
    folder.mkdir(mode=0o700)
    _fsync_directory(folder.parent)
    records = {row["id"]: row for row in snapshot["manifest"]["documents"]}
    affected = set(plan["affected_ids"])
    dependencies = {dependency for key in affected for dependency in records[key]["dependencies"]} - affected
    return {"kind": _RECEIPT_KIND, "version": 1, "scope": "global", "owner_root": str(_root()),
            "receipt_id": identity, "recovery_path": str(folder), "affected_ids": plan["affected_ids"],
            "action": plan["action"], "backend": plan["backend"], "resource_kind": plan["kind"],
            "created_at": datetime.now(timezone.utc).isoformat(), "state": "prepared",
            "before_rows": [records[key] for key in sorted(affected)],
            "dependency_rows": [records[key] for key in sorted(dependencies)],
            "after_rows": [row for row in expected["manifest"]["documents"] if row["id"] in affected],
            **{key: deepcopy(plan[key]) for key in ("source", "fingerprint", "business_id", "row",
                                                    "tombstone", "tombstone_sha256") if key in plan}}


def _insert_row(connection, receipt):
    _reject_sql_triggers(connection)
    table, _ = _SQL[receipt["resource_kind"]]
    row = receipt["row"]
    if row["columns"] != _SQL_COLUMNS[receipt["resource_kind"]]:
        raise ProjectError("invalid_receipt", "Archive has an unsupported SQL schema")
    columns = ",".join(row["columns"])
    placeholders = ",".join("?" for _ in row["columns"])
    connection.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", row["values"])


def _archive_sql(connection, receipt):
    _reject_sql_triggers(connection)
    connection.execute(f"CREATE TABLE IF NOT EXISTS {_ARCHIVE_TABLE} (receipt_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)")
    connection.execute(f"INSERT INTO {_ARCHIVE_TABLE}(receipt_id,receipt_json) VALUES(?,?)",
                       (receipt["receipt_id"], json_bytes(receipt).decode("utf-8")))


def _set_sql_receipt(connection, receipt):
    _reject_sql_triggers(connection)
    connection.execute(f"UPDATE {_ARCHIVE_TABLE} SET receipt_json=? WHERE receipt_id=?",
                       (json_bytes(receipt).decode("utf-8"), receipt["receipt_id"]))


@_translate_errors
def apply_global_deletion(snapshot, selection):
    """Revalidate a plan, archive exactly its original bytes/row, then check CAS."""
    if not isinstance(selection, dict):
        raise ProjectError("invalid_selection", "Invalid global deletion selection")
    _index().mkdir(parents=True, exist_ok=True)
    _fsync_directory(_index().parent)
    with ExitStack() as stack:
        stack.enter_context(material_library_lock(_index()))
        if selection.get("backend") in ("material_file", "material_tombstone"):
            stack.enter_context(material_library_lock(data_files.data_path("materials")))
        plan = prepare_global_deletion(snapshot, selection.get("selected_ids"))
        if plan != selection:
            raise ProjectError("stale_source", "Deletion selection changed; prepare again")
        if plan["blockers"]:
            raise ProjectError("deletion_blocked", "Global asset has protected state or incoming dependencies")
        expected = _expected_after(snapshot, plan)
        receipt = _new_receipt(snapshot, plan, expected)
        moved = False
        tombstoned = False
        committed = False
        try:
            if plan["backend"] == "material_tombstone":
                library = MaterialLibrary(directory=data_files.data_path("materials"))
                # Record the intended archive path in the durable prepared
                # receipt before any mutation, so a crash at any point leaves a
                # receipt that can resolve the override's location.
                source = archive_folder = None
                if plan.get("source"):
                    source = _path(plan["source"])
                    archive_folder = source.parent / ".asset-recycle" / receipt["receipt_id"]
                    reject_links(archive_folder)
                    receipt["archive"] = _relative(archive_folder / "payload")
                _write_receipt(receipt)
                if source is not None:
                    assert archive_folder is not None
                    archive_folder.mkdir(mode=0o700, parents=True)
                    _fsync_directory(archive_folder.parent)
                    _fsync_directory(source.parent)
                    if _capture(source) != plan["fingerprint"]:
                        raise ProjectError("stale_source", "Builtin override changed after deletion preview")
                    _check_before(snapshot)
                    _rename_directory_no_replace(source, _path(receipt["archive"]))
                    moved = True
                    _fsync_directory(source.parent)
                    _fsync_directory(archive_folder)
                    if not _same_archived(_capture(_path(receipt["archive"])), plan["fingerprint"]):
                        raise ProjectError("stale_source", "Builtin override changed during archival")
                library.delete_builtin(plan["business_id"])
                tombstoned = True
                marker = _path(plan["tombstone"])
                if not marker.exists():
                    raise ProjectError("global_library_error", "Builtin deletion marker was not written")
                if _capture(marker)["sha256"] != receipt["tombstone_sha256"]:
                    raise ProjectError("integrity_error", "Builtin deletion marker differs from its expected payload")
                if resources.read_global_resources() != expected:
                    raise ProjectError("stale_source", "Global library changed during deletion")
                receipt["state"] = "active"
                _write_receipt(receipt)
            elif plan["backend"] in ("typed_file", "material_file"):
                source = _path(plan["source"])
                archive_folder = source.parent / ".asset-recycle" / receipt["receipt_id"]
                reject_links(archive_folder)
                archive_folder.mkdir(mode=0o700, parents=True)
                receipt["archive"] = _relative(archive_folder / "payload")
                _write_receipt(receipt)
                # Persist the two new archive-directory entries before a
                # durable rename can remove the original active pathname.
                _fsync_directory(archive_folder.parent)
                _fsync_directory(source.parent)
                if _capture(source) != plan["fingerprint"]:
                    raise ProjectError("stale_source", "Resource file changed after deletion preview")
                _check_before(snapshot)
                _rename_directory_no_replace(source, _path(receipt["archive"]))
                moved = True
                _fsync_directory(source.parent)
                _fsync_directory(archive_folder)
                if not _same_archived(_capture(_path(receipt["archive"])), plan["fingerprint"]):
                    raise ProjectError("stale_source", "Resource changed during archival")
                if resources.read_global_resources() != expected:
                    raise ProjectError("stale_source", "Global library changed during deletion")
                receipt["state"] = "active"
                _write_receipt(receipt)
            else:
                _write_receipt(receipt)
                with closing(_connect_sql(writable=True)) as connection:
                    _begin_sql_mutation(connection)
                    try:
                        _check_before(snapshot)
                        if _sql_row(connection, plan["kind"], plan["business_id"]) != plan["row"]:
                            raise ProjectError("stale_source", "Film row changed after deletion preview")
                        receipt["state"] = "active"
                        _archive_sql(connection, receipt)
                        table, key = _SQL[plan["kind"]]
                        cursor = connection.execute(f"DELETE FROM {table} WHERE {key}=? COLLATE BINARY", (plan["business_id"],))
                        if cursor.rowcount != 1:
                            raise ProjectError("stale_source", "Film row disappeared during deletion")
                        connection.commit()
                        committed = True
                    except BaseException:
                        connection.rollback()
                        raise
                if resources.read_global_resources() != expected:
                    raise ProjectError("stale_source", "Global library changed during deletion")
                _write_receipt(receipt)
            return _public(receipt)
        except Exception as exc:
            try:
                if tombstoned:
                    marker = _path(receipt["tombstone"])
                    if marker.exists():
                        marker.unlink()
                        _fsync_directory(marker.parent)
                if moved:
                    source, archive = _path(receipt["source"]), _path(receipt["archive"])
                    if not _same_archived(_capture(archive), receipt["fingerprint"]):
                        raise ProjectError("integrity_error", "Archived original changed; retain it for recovery")
                    _rename_directory_no_replace(archive, source)
                    _fsync_directory(source.parent)
                    _fsync_directory(archive.parent)
                if committed:
                    with closing(_connect_sql(writable=True)) as connection:
                        with connection:
                            _begin_sql_mutation(connection)
                            _insert_row(connection, receipt)
                            receipt["state"] = "rolled_back"
                            _set_sql_receipt(connection, receipt)
                receipt["state"] = "rolled_back"
                _write_receipt(receipt)
            except Exception as rollback_error:
                raise _uncertain(receipt, rollback_error) from exc
            if isinstance(exc, ProjectError):
                raise
            raise ProjectError("global_library_error", "Cannot recycle global asset: " + str(exc)) from exc


def _decode_receipt(raw):
    if len(raw) > MAX_FILE_BYTES * 2 + 1024 * 1024:
        raise ProjectError("capacity_exceeded", "Recycle metadata exceeds its safety limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProjectError("invalid_receipt", "Duplicate archive metadata key")
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        json_bytes(result)
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProjectError("invalid_receipt", "Invalid recycle metadata") from exc


def _validate_receipt(receipt, identity):
    if (not isinstance(identity, str) or not _HEX_ID.fullmatch(identity) or not isinstance(receipt, dict)
            or receipt.get("kind") != _RECEIPT_KIND or receipt.get("version") != 1
            or receipt.get("scope") != "global" or receipt.get("owner_root") != str(_root())
            or receipt.get("receipt_id") != identity or receipt.get("recovery_path") != str(_index() / identity)
            or receipt.get("backend") not in {"typed_file", "material_file", "sqlite", "material_tombstone"}
            or receipt.get("action") not in {"delete_asset", "revert_builtin_override", "delete_builtin"}
            or receipt.get("state") not in {"prepared", "active", "uncertain", "restoring", "restored", "rolled_back"}
            or not isinstance(receipt.get("created_at"), str)):
        raise ProjectError("invalid_receipt", "Recycle receipt is not owned by this global library")
    affected = receipt.get("affected_ids")
    if (not isinstance(affected, list) or not 1 <= len(affected) <= 2
            or any(not isinstance(item, str) or not _HEX_ID.fullmatch(item) for item in affected)
            or len(set(affected)) != len(affected)):
        raise ProjectError("invalid_receipt", "Invalid recycle affected IDs")
    for field in ("before_rows", "dependency_rows", "after_rows"):
        rows = receipt.get(field)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ProjectError("invalid_receipt", "Invalid recycle dependency records")
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"id", "kind", "path", "sha256", "dependencies"}
                    or row["kind"] not in EXTENSIONS or not isinstance(row["id"], str)
                    or not _HEX_ID.fullmatch(row["id"]) or not isinstance(row["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                    or not isinstance(row["dependencies"], list)
                    or any(not isinstance(item, str) or not _HEX_ID.fullmatch(item) for item in row["dependencies"])):
                raise ProjectError("invalid_receipt", "Invalid recycle resource record")
    before = {row["id"]: row for row in receipt["before_rows"]}
    if len(before) != len(affected) or set(before) != set(affected):
        raise ProjectError("invalid_receipt", "Recycle row identities differ from affected IDs")
    kind = receipt.get("resource_kind")
    if not isinstance(kind, str) or kind not in {row["kind"] for row in before.values()}:
        raise ProjectError("invalid_receipt", "Recycle kind differs from its rows")
    if receipt["backend"] == "sqlite":
        if kind not in _SQL or receipt["action"] != "delete_asset" or len(affected) != 1:
            raise ProjectError("invalid_receipt", "Invalid SQL recycle kind")
        business_id = receipt.get("business_id")
        if not isinstance(business_id, str) or affected != [resources._identity("sqlite:film_design/library.sqlite3:" + kind + ":" + business_id)]:
            raise ProjectError("invalid_receipt", "Invalid SQL recycle identity")
        row = receipt.get("row")
        if (not isinstance(row, dict) or set(row) != {"columns", "values"} or row["columns"] != _SQL_COLUMNS[kind]
                or not isinstance(row["values"], list) or len(row["values"]) != len(row["columns"])
                or row["values"][0] != business_id):
            raise ProjectError("invalid_receipt", "Invalid SQL archive row")
    elif receipt["backend"] == "material_tombstone":
        if receipt["action"] != "delete_builtin":
            raise ProjectError("invalid_receipt", "Invalid builtin tombstone action")
        material_id = receipt.get("business_id")
        if not isinstance(material_id, str) or set(affected) != {
                resources._identity("material_profile:" + material_id),
                resources._identity("optical_material:" + material_id)}:
            raise ProjectError("invalid_receipt", "Invalid paired material archive identity")
        library = MaterialLibrary(directory=data_files.data_path("materials"))
        if receipt.get("tombstone") != "materials/.deleted/" + material_id + ".json":
            raise ProjectError("unsafe_path", "Builtin tombstone path is outside its owned location")
        if (not isinstance(receipt.get("tombstone_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt["tombstone_sha256"])):
            raise ProjectError("invalid_receipt", "Invalid builtin tombstone fingerprint")
        if "archive" in receipt or "source" in receipt:
            source = _path(receipt.get("source"))
            archive = _path(receipt.get("archive"))
            if source != library.override_directory / (material_id + ".json"):
                raise ProjectError("unsafe_path", "Builtin override archive is outside its owned path")
            if archive != source.parent / ".asset-recycle" / identity / "payload" or ".asset-recycle" in source.parts:
                raise ProjectError("unsafe_path", "Recycle payload path is outside its owned location")
            fingerprint = receipt.get("fingerprint")
            if (not isinstance(fingerprint, dict) or set(fingerprint) != {"signature", "sha256"}
                    or not isinstance(fingerprint["signature"], list) or len(fingerprint["signature"]) != 6
                    or any(type(item) is not int for item in fingerprint["signature"])
                    or not isinstance(fingerprint["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint["sha256"])):
                raise ProjectError("invalid_receipt", "Invalid archive byte fingerprint")
    else:
        source = _path(receipt.get("source"))
        archive = _path(receipt.get("archive"))
        if archive != source.parent / ".asset-recycle" / identity / "payload" or ".asset-recycle" in source.parts:
            raise ProjectError("unsafe_path", "Recycle payload path is outside its owned location")
        if receipt["backend"] == "typed_file":
            if (len(affected) != 1 or receipt["action"] != "delete_asset" or kind == "binary_asset"
                    or not any(source.is_relative_to(root) for root in resources._roots().values())
                    or source.suffix.casefold() != EXTENSIONS[kind]
                    or affected != [resources._identity("file:" + receipt["source"]) ]):
                raise ProjectError("unsafe_path", "Typed archive source is outside the global typed libraries")
        else:
            material_id = receipt.get("business_id")
            if not isinstance(material_id, str) or set(affected) != {
                    resources._identity("material_profile:" + material_id), resources._identity("optical_material:" + material_id)}:
                raise ProjectError("invalid_receipt", "Invalid paired material archive identity")
            library = MaterialLibrary(directory=data_files.data_path("materials"))
            if receipt["action"] == "revert_builtin_override":
                if source != library.override_directory / (material_id + ".json"):
                    raise ProjectError("unsafe_path", "Builtin override archive is outside its owned path")
            elif source.parent != library.directory or source.suffix != ".json":
                raise ProjectError("unsafe_path", "Custom material archive is outside the global material library")
        fingerprint = receipt.get("fingerprint")
        if (not isinstance(fingerprint, dict) or set(fingerprint) != {"signature", "sha256"}
                or not isinstance(fingerprint["signature"], list) or len(fingerprint["signature"]) != 6
                or any(type(item) is not int for item in fingerprint["signature"])
                or not isinstance(fingerprint["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint["sha256"])):
            raise ProjectError("invalid_receipt", "Invalid archive byte fingerprint")
    return receipt


def _merge_uncertain_sidecar(receipt):
    path = _index() / receipt["receipt_id"] / "receipt.json"
    try:
        _ordinary(path)
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES * 2 + 1024 * 1024 + 1)
    except FileNotFoundError:
        return receipt
    sidecar = _validate_receipt(_decode_receipt(raw), receipt["receipt_id"])
    if sidecar["state"] == "uncertain":
        sql_content = {key: value for key, value in receipt.items() if key != "state"}
        sidecar_content = {key: value for key, value in sidecar.items() if key != "state"}
        if sql_content != sidecar_content:
            raise ProjectError("invalid_receipt", "Uncertain recovery metadata differs from its SQL archive")
        # A newly installed trigger may make even the SQL uncertainty UPDATE
        # unsafe. Preserve the durable sidecar warning without replacing any
        # SQL-owned payload, or letting ordinary stale mirror states win.
        receipt = deepcopy(receipt)
        receipt["state"] = "uncertain"
    return receipt


def _sql_receipts():
    path = _sqlite_path()
    if not path.exists():
        return {}
    with closing(_connect_sql()) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_ARCHIVE_TABLE,)).fetchone()
        if table is None:
            return {}
        rows = connection.execute(f"SELECT receipt_id,length(CAST(receipt_json AS BLOB)) FROM {_ARCHIVE_TABLE} LIMIT 20001").fetchall()
        if len(rows) > 20000:
            raise ProjectError("capacity_exceeded", "Too many global recycle receipts")
        result, total = {}, 0
        for identity, size in rows:
            if type(size) is not int or not 0 < size <= MAX_FILE_BYTES * 2 + 1024 * 1024:
                raise ProjectError("capacity_exceeded", "Recycle metadata exceeds its safety limit")
            total += size
            if total > MAX_PROJECT_BYTES:
                raise ProjectError("capacity_exceeded", "Recycle catalogue exceeds its safety limit")
            raw = connection.execute(f"SELECT receipt_json FROM {_ARCHIVE_TABLE} WHERE receipt_id=?", (identity,)).fetchone()[0]
            result[identity] = _merge_uncertain_sidecar(_validate_receipt(_decode_receipt(raw), identity))
        return result


def _load_receipt(identity):
    if not isinstance(identity, str) or not _HEX_ID.fullmatch(identity):
        raise ProjectError("invalid_receipt", "Invalid recycle receipt ID")
    # The SQL copy is committed atomically with its row and wins over a mirror
    # left prepared by a process interruption after commit. Read one receipt
    # directly so a large archive history never prevents a targeted restore.
    if _sqlite_path().exists():
        with closing(_connect_sql()) as connection:
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_ARCHIVE_TABLE,)).fetchone()
            if table is not None:
                row = connection.execute(f"SELECT length(CAST(receipt_json AS BLOB)) FROM {_ARCHIVE_TABLE} WHERE receipt_id=?", (identity,)).fetchone()
                if row is not None:
                    if type(row[0]) is not int or not 0 < row[0] <= MAX_FILE_BYTES * 2 + 1024 * 1024:
                        raise ProjectError("capacity_exceeded", "Recycle metadata exceeds its safety limit")
                    raw = connection.execute(f"SELECT receipt_json FROM {_ARCHIVE_TABLE} WHERE receipt_id=?", (identity,)).fetchone()[0]
                    return _merge_uncertain_sidecar(_validate_receipt(_decode_receipt(raw), identity))
    path = _index() / identity / "receipt.json"
    try:
        _ordinary(path)
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES * 2 + 1024 * 1024 + 1)
    except FileNotFoundError as exc:
        raise ProjectError("missing_receipt", "Global recycle receipt was not found") from exc
    return _validate_receipt(_decode_receipt(raw), identity)


@_translate_errors
def list_global_deletions():
    """Return bounded, detached summaries, including interrupted operations."""
    receipts = {}
    total = 0
    folder = _index()
    if folder.exists():
        _ordinary(folder, directory=True)
        entries = list(folder.iterdir())
        if len(entries) > 20001:
            raise ProjectError("capacity_exceeded", "Too many global recycle entries")
        for entry in entries:
            reject_links(entry)
            if _HEX_ID.fullmatch(entry.name):
                path = entry / "receipt.json"
                _ordinary(entry, directory=True)
                if path.exists():
                    info = _ordinary(path)
                    total += info.st_size
                    if total > MAX_PROJECT_BYTES:
                        raise ProjectError("capacity_exceeded", "Recycle catalogue exceeds its safety limit")
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_FILE_BYTES * 2 + 1024 * 1024 + 1)
                    receipts[entry.name] = _validate_receipt(_decode_receipt(raw), entry.name)
    receipts.update(_sql_receipts())
    return [_public(receipt) for receipt in sorted(receipts.values(), key=lambda item: (item["created_at"], item["receipt_id"]), reverse=True)]


def _restore_preflight(receipt):
    _check_sql_paths()
    current = resources.read_global_resources()
    records = {row["id"]: row for row in current["manifest"]["documents"]}
    expected = {row["id"]: row for row in receipt["after_rows"]}
    for identity in receipt["affected_ids"]:
        if records.get(identity) != expected.get(identity):
            raise ProjectError("restore_conflict", "A resource with this identity changed or was recreated")
    for dependency in receipt["dependency_rows"]:
        if records.get(dependency["id"]) != dependency:
            raise ProjectError("missing_dependency", "A restore dependency is absent or changed: " + dependency["id"])
    return current


def _expected_restore(current, receipt):
    rows = {row["id"]: row for row in current["manifest"]["documents"]}
    rows.update({row["id"]: row for row in receipt["before_rows"]})
    return [rows[key] for key in sorted(rows)]


def _prepared_before_state(receipt):
    """Confirm the live resources still equal the pre-deletion receipt rows."""
    current = resources.read_global_resources()
    records = {row["id"]: row for row in current["manifest"]["documents"]}
    for row in receipt["before_rows"] + receipt["dependency_rows"]:
        if records.get(row["id"]) != row:
            raise ProjectError("restore_conflict",
                               "Interrupted deletion has changed resources or dependencies")
    return current


def _recover_prepared_tombstone(receipt):
    """Finalize a builtin tombstone receipt that never became active.

    A crash can stop after the durable ``prepared`` receipt is written but
    before the tombstone marker or the override archive is finalised. Nothing
    was acknowledged as deleted, so move any archived override back to a free
    original path, confirm the pre-deletion resources, and mark the operation
    ``rolled_back``. An existing marker means the deletion is present and is
    left to the normal restore path; an occupied source or a changed archive
    stays visible instead of being overwritten.
    """
    marker = _path(receipt["tombstone"])
    if marker.exists():
        return False
    if "archive" not in receipt:
        _prepared_before_state(receipt)
        receipt["state"] = "rolled_back"
        _write_receipt(receipt)
        return True
    source, archive = _path(receipt["source"]), _path(receipt["archive"])
    if archive.exists():
        if source.exists():
            raise ProjectError("restore_conflict", "The original override path is occupied")
        if not _same_archived(_capture(archive), receipt["fingerprint"]):
            raise ProjectError("integrity_error", "Archived override changed; preserve it for manual recovery")
        _rename_directory_no_replace(archive, source)
        _fsync_directory(source.parent)
        _fsync_directory(archive.parent)
        if not _same_archived(_capture(source), receipt["fingerprint"]):
            raise ProjectError("integrity_error", "Restored override changed; retain all recovery paths")
    else:
        if not source.exists():
            raise ProjectError("restore_conflict", "Archived override is missing; preserve the receipt")
        if not _same_archived(_capture(source), receipt["fingerprint"]):
            raise ProjectError("integrity_error", "Original override changed; preserve it for manual recovery")
    _prepared_before_state(receipt)
    receipt["state"] = "rolled_back"
    _write_receipt(receipt)
    return True


def _finish_interrupted_file_restore(receipt):
    if receipt["backend"] == "sqlite":
        return False
    if receipt["backend"] == "material_tombstone":
        marker = _path(receipt["tombstone"])
        if marker.exists():
            return False
        if "archive" in receipt:
            source = _path(receipt["source"])
            if not source.exists() or not _same_archived(_capture(source), receipt["fingerprint"]):
                return False
        current = resources.read_global_resources()
        records = {row["id"]: row for row in current["manifest"]["documents"]}
        for row in receipt["before_rows"] + receipt["dependency_rows"]:
            if records.get(row["id"]) != row:
                raise ProjectError("restore_conflict", "Interrupted restore has changed resources or dependencies")
        receipt["state"] = "restored"
        _write_receipt(receipt)
        return True
    source, archive = _path(receipt["source"]), _path(receipt["archive"])
    if archive.exists() or not source.exists() or not _same_archived(_capture(source), receipt["fingerprint"]):
        return False
    # A process may have stopped after the exclusive rename but before the
    # metadata acknowledgement. The same archived inode is already back.
    current = resources.read_global_resources()
    records = {row["id"]: row for row in current["manifest"]["documents"]}
    for row in receipt["before_rows"] + receipt["dependency_rows"]:
        if records.get(row["id"]) != row:
            raise ProjectError("restore_conflict", "Interrupted restore has changed resources or dependencies")
    receipt["state"] = "restored"
    _write_receipt(receipt)
    return True


@_translate_errors
def restore_global_deletion(receipt_id):
    """Restore a durable receipt without replacing files, rows or dependencies."""
    receipt = _load_receipt(receipt_id)
    if receipt["state"] in {"restored", "rolled_back"}:
        raise ProjectError("already_restored", "Global deletion is no longer active")
    with ExitStack() as stack:
        stack.enter_context(material_library_lock(_index()))
        if receipt["backend"] in ("material_file", "material_tombstone"):
            stack.enter_context(material_library_lock(data_files.data_path("materials")))
        receipt = _load_receipt(receipt_id)
        if receipt["state"] in {"restored", "rolled_back"}:
            raise ProjectError("already_restored", "Global deletion is no longer active")
        if receipt["state"] == "prepared" and receipt["backend"] == "material_tombstone":
            if _recover_prepared_tombstone(receipt):
                return _public(receipt)
        if _finish_interrupted_file_restore(receipt):
            return _public(receipt)
        current = _restore_preflight(receipt)
        expected_rows = _expected_restore(current, receipt)
        moved = False
        committed = False
        marker_removed = False
        previous_state = receipt["state"]
        try:
            if receipt["backend"] == "sqlite":
                with closing(_connect_sql(writable=True)) as connection:
                    _begin_sql_mutation(connection)
                    try:
                        if resources.read_global_resources() != current:
                            raise ProjectError("stale_source", "Global resources changed during restore")
                        _insert_row(connection, receipt)
                        receipt["state"] = "restored"
                        _set_sql_receipt(connection, receipt)
                        connection.commit()
                        committed = True
                    except BaseException:
                        connection.rollback()
                        raise
            elif receipt["backend"] == "material_tombstone":
                marker = _path(receipt["tombstone"])
                if not marker.exists():
                    raise ProjectError("restore_conflict", "Builtin deletion marker is missing")
                if _capture(marker)["sha256"] != receipt["tombstone_sha256"]:
                    raise ProjectError("integrity_error", "Builtin deletion marker changed; preserve it for manual recovery")
                if "archive" in receipt:
                    source, archive = _path(receipt["source"]), _path(receipt["archive"])
                    if source.exists():
                        raise ProjectError("restore_conflict", "The original override path is occupied")
                    if not _same_archived(_capture(archive), receipt["fingerprint"]):
                        raise ProjectError("integrity_error", "Archived override changed; preserve it for manual recovery")
                receipt["state"] = "restoring"
                _write_receipt(receipt)
                if resources.read_global_resources() != current:
                    raise ProjectError("stale_source", "Global resources changed during restore")
                marker.unlink()
                marker_removed = True
                _fsync_directory(marker.parent)
                if "archive" in receipt:
                    _rename_directory_no_replace(archive, source)
                    moved = True
                    _fsync_directory(source.parent)
                    _fsync_directory(archive.parent)
            else:
                source, archive = _path(receipt["source"]), _path(receipt["archive"])
                if source.exists():
                    raise ProjectError("restore_conflict", "The original file path is occupied")
                if not _same_archived(_capture(archive), receipt["fingerprint"]):
                    raise ProjectError("integrity_error", "Archived file changed; preserve it for manual recovery")
                if receipt["backend"] == "material_file":
                    saved = MaterialLibrary(directory=data_files.data_path("materials"))._read_record_file(archive)
                    if saved["id"] != receipt["business_id"]:
                        raise ProjectError("integrity_error", "Archived material ID differs from its receipt")
                receipt["state"] = "restoring"
                _write_receipt(receipt)
                if resources.read_global_resources() != current:
                    raise ProjectError("stale_source", "Global resources changed during restore")
                _rename_directory_no_replace(archive, source)
                moved = True
                _fsync_directory(source.parent)
                _fsync_directory(archive.parent)
            actual = resources.read_global_resources()
            if moved and not _same_archived(_capture(_path(receipt["source"])), receipt["fingerprint"]):
                raise ProjectError("stale_source", "Restored file changed during validation")
            if actual["manifest"]["documents"] != expected_rows or actual.get("assets") != current.get("assets"):
                raise ProjectError("stale_source", "Global resources changed or restored content is invalid")
            receipt["state"] = "restored"
            _write_receipt(receipt)
            return _public(receipt)
        except Exception as exc:
            try:
                if moved:
                    if not _same_archived(_capture(_path(receipt["source"])), receipt["fingerprint"]):
                        raise ProjectError("integrity_error", "Restored original changed; retain all recovery paths")
                    _rename_directory_no_replace(_path(receipt["source"]), _path(receipt["archive"]))
                    _fsync_directory(_path(receipt["source"]).parent)
                    _fsync_directory(_path(receipt["archive"]).parent)
                if marker_removed:
                    marker = _path(receipt["tombstone"])
                    if not marker.exists():
                        MaterialLibrary(directory=data_files.data_path("materials")).delete_builtin(receipt["business_id"])
                        _fsync_directory(marker.parent)
                if committed:
                    with closing(_connect_sql(writable=True)) as connection:
                        with connection:
                            _begin_sql_mutation(connection)
                            kind = receipt["resource_kind"]
                            if _sql_row(connection, kind, receipt["business_id"]) != receipt["row"]:
                                raise ProjectError("restore_conflict", "Restored SQL row changed; cannot roll back")
                            table, key = _SQL[kind]
                            connection.execute(f"DELETE FROM {table} WHERE {key}=? COLLATE BINARY", (receipt["business_id"],))
                            receipt["state"] = previous_state
                            _set_sql_receipt(connection, receipt)
                receipt["state"] = previous_state
                _write_receipt(receipt)
            except Exception as rollback_error:
                raise _uncertain(receipt, rollback_error) from exc
            if isinstance(exc, (ProjectError, FileExistsError)):
                if isinstance(exc, FileExistsError):
                    raise ProjectError("restore_conflict", "The original file path is occupied") from exc
                raise
            raise ProjectError("global_library_error", "Cannot restore global asset: " + str(exc)) from exc
