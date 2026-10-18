"""Atomic persistence of explicit project-local input document graphs.

No GUI, global data directory, active-project singleton or calculation is used.
Each mutation publishes a complete managed-JSON generation. Jobs continue to
own their separate frozen input snapshots; project edits keep stable IDs.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from threading import RLock
from typing import Any, TypeVar, cast
import unicodedata
import uuid
import weakref

from .atomic_io import replace_with_retry
from .project_dependencies import (
    EXTENSIONS, KINDS, MAX_FILE_BYTES, MAX_PROJECT_BYTES, MAX_PROJECT_DOCUMENTS, ProjectError,
    document_material_references, external_document, file_references, identifier, json_bytes,
    normalize_document, normalize_optical_material, portable_path, read_json,
    reject_links,
)
from .project_paths import (PROJECT_TYPED_DIRECTORY_BY_KIND, ensure_project_layout,
                            managed_document_path, safe_resource_filename)

PROJECT_KIND = "coat_sim.project"
PROJECT_VERSION = 1
MANIFEST_FILENAME = "project.coatproj"
PROJECT_FILE_SUFFIX = ".coatproj"
RECOVERY_FILENAME = "recovery.json"
_DEFAULT_UNCHANGED = object()


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _new_id():
    return uuid.uuid4().hex


def _path_root(root) -> Path:
    path = Path(os.path.abspath(Path(root).expanduser()))
    reject_links(path)
    return path.resolve()


def project_file_root(project_file: str | Path) -> Path:
    """Validate the explicit descriptor path and return its canonical root.

    This lightweight boundary checks the dedicated suffix, ordinary-file type and
    every path component for links. ``ProjectStore.open`` additionally parses
    and validates the descriptor and designated dependency graph. A directory,
    legacy JSON manifest or project ZIP is never an opening alias.
    """
    path = Path(os.path.abspath(Path(project_file).expanduser()))
    reject_links(path)
    if path.suffix.casefold() != PROJECT_FILE_SUFFIX:
        raise ProjectError("invalid_project_file", "Select a .coatproj project descriptor file")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectError("missing_dependency", "Project file is missing: " + str(path)) from exc
    except OSError as exc:
        raise ProjectError("io_error", "Cannot read project file: " + str(path)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProjectError("invalid_project_file", "Project descriptor must be an ordinary file: " + str(path))
    return path.parent.resolve()


def _manifest(value):
    if (not isinstance(value, dict) or set(value) - {
            "kind", "version", "id", "name", "revision", "documents", "default_configuration_id",
            "summary", "notes"} or not {"kind", "version", "id", "name", "revision", "documents",
                                        "default_configuration_id"} <= set(value)):
        raise ProjectError("invalid_manifest", "Invalid project manifest fields")
    if value.get("kind") != PROJECT_KIND:
        raise ProjectError("invalid_manifest", "Invalid project manifest kind")
    if type(value.get("version")) is not int or value["version"] != PROJECT_VERSION:
        raise ProjectError("unsupported_version", "Unsupported project manifest version; explicit migration is required")
    identifier(value.get("id"))
    name = value.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ProjectError("invalid_manifest", "Project name must be 1–100 characters")
    for field in ("summary", "notes"):
        if field in value and (not isinstance(value[field], str) or len(value[field]) > 20000):
            raise ProjectError("invalid_manifest", field + " must be text up to 20000 characters")
    if type(value.get("revision")) is not int or value["revision"] < 0:
        raise ProjectError("invalid_manifest", "Project revision must be a non-negative integer")
    documents = value.get("documents")
    if not isinstance(documents, list) or len(documents) > MAX_PROJECT_DOCUMENTS:
        raise ProjectError("invalid_manifest", "Invalid project document list")
    identities, paths = {}, set()
    for row in documents:
        if not isinstance(row, dict) or set(row) != {"id", "kind", "path", "sha256", "dependencies"}:
            raise ProjectError("invalid_manifest", "Invalid project document record")
        identity = identifier(row["id"])
        if identity in identities:
            raise ProjectError("dependency_conflict", "Duplicate project document ID")
        if not isinstance(row["kind"], str) or row["kind"] not in KINDS:
            raise ProjectError("invalid_manifest", "Unknown project document kind")
        relative = managed_document_path(row["path"], row["kind"], identity)
        folded_path = unicodedata.normalize("NFKC", relative).casefold()
        if folded_path in paths:
            raise ProjectError("dependency_conflict", "Duplicate project-relative path")
        paths.add(folded_path)
        digest = row["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProjectError("invalid_manifest", "Invalid project document digest")
        dependencies = row["dependencies"]
        if (not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies)
                or len(dependencies) != len(set(dependencies))):
            raise ProjectError("invalid_manifest", "Invalid project dependency IDs")
        for dependency in dependencies:
            identifier(dependency)
        identities[identity] = row
    for row in documents:
        if any(dependency not in identities for dependency in row["dependencies"]):
            raise ProjectError("missing_dependency", "Project manifest references an absent document")
    default = value["default_configuration_id"]
    if default is not None:
        identifier(default)
        if default not in identities or identities[default]["kind"] != "deposition_configuration":
            raise ProjectError("dependency_conflict", "Default root must be a project deposition configuration")
    return deepcopy(value)


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Publish a directory atomically without replacing a concurrent target.

    Windows os.rename already refuses an existing target. Linux/macOS expose
    explicit exclusive rename flags; unsupported hosts/filesystems fail closed.
    """
    if os.name == "nt":
        os.rename(source, destination)
        return
    import ctypes
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith("linux"):
            function = library.renameat2
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        elif sys.platform == "darwin":
            function = library.renamex_np
            function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            result = function(os.fsencode(source), os.fsencode(destination), 4)
        else:
            raise ProjectError("unsupported_atomic_export", "Host has no supported exclusive directory rename")
    except (AttributeError, OSError) as exc:
        raise ProjectError("unsupported_atomic_export", "Exclusive directory rename is unavailable") from exc
    if result != 0:
        number = ctypes.get_errno()
        if number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise ProjectError("unsupported_atomic_export", "Filesystem does not support exclusive directory rename")
        raise OSError(number, os.strerror(number), str(destination))


def _rollback_file_signature(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _rollback_published_directory(directory: Path, identity: tuple, files: dict[str, tuple]) -> bool:
    """Remove only still-owned publication bytes; preserve concurrent changes.

    Publication owns the directory it exclusively renamed, not files another
    writer may subsequently add or replace. A retained directory is harmless
    because the descriptor has not been published and later saves skip it.
    """
    try:
        reject_links(directory)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            return False
    except FileNotFoundError:
        return True
    except (OSError, ProjectError):
        return False
    for name, signature in files.items():
        target = directory / name
        try:
            current_directory = directory.lstat()
            if (current_directory.st_dev, current_directory.st_ino) != identity:
                return False
            info = target.lstat()
            if stat.S_ISREG(info.st_mode) and _rollback_file_signature(info) == signature:
                target.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue  # Failed cleanup must never hide the transaction failure.
    try:
        info = directory.lstat()
        if (info.st_dev, info.st_ino) != identity:
            return False
        directory.rmdir()  # A foreign file or changed owned file keeps it nonempty.
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


class _CapturedRecords(dict):
    """Working membership paired with the descriptor from which it was read."""
    def __init__(self, records, fingerprint):
        super().__init__(records)
        self.fingerprint = fingerprint


_StoreMethod = TypeVar("_StoreMethod", bound=Callable[..., Any])


def _store_operation(method: _StoreMethod) -> _StoreMethod:
    @wraps(method)
    def serialized(self: "ProjectStore", *args: Any, **kwargs: Any) -> Any:
        with self._operation_scope():
            return method(self, *args, **kwargs)
    return cast(_StoreMethod, serialized)


class ProjectStore:
    """A captured persisted project revision; reopen after a stale-write error."""
    def __init__(self, root: Path, manifest: dict, fingerprint: str, *,
                 project_file: Path | None = None, asset_limits=None):
        self._operation_lock = RLock()
        self._operation_depth = 0
        self._notification_pending = False
        self._root = root
        self._project_file = project_file if project_file is not None else root / MANIFEST_FILENAME
        self._manifest = manifest
        self._fingerprint = fingerprint
        self._maintenance_warnings: list[dict[str, str]] = []
        self._asset_validation_cache: dict[str, tuple] = {}
        self._asset_publication_receipts: dict[str, tuple] = {}
        self._asset_receipt_lock = RLock()
        self._change_observer_lock = RLock()
        self._change_observers: set[weakref.ReferenceType] = set()
        from .project_binary_assets import DEFAULT_ASSET_LIMITS, AssetReadLimits
        if asset_limits is not None and not isinstance(asset_limits, AssetReadLimits):
            raise ProjectError("invalid_limits", "Use AssetReadLimits for project binary input safety limits")
        self.asset_limits = DEFAULT_ASSET_LIMITS if asset_limits is None else asset_limits

    @contextmanager
    def _operation_scope(self):
        # Host/service locks precede this lock. Never invoke observers while it
        # is held: observers may acquire a host lock on another thread.
        notify = False
        try:
            with self._operation_lock:
                self._operation_depth += 1
                try:
                    yield
                finally:
                    self._operation_depth -= 1
                    if self._operation_depth == 0 and self._notification_pending:
                        self._notification_pending = False
                        notify = True
        finally:
            if notify:
                self._notify_changes()

    def observe_changes(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Observe committed publications weakly; callers marshal any UI work.

        The callback runs after the store writer lock is released. It is a
        best-effort notification, never part of the publication transaction.
        """
        reference = (weakref.WeakMethod(callback) if getattr(callback, "__self__", None) is not None
                     else weakref.ref(callback))
        with self._change_observer_lock:
            self._change_observers.add(reference)

        def unsubscribe() -> None:
            with self._change_observer_lock:
                self._change_observers.discard(reference)

        return unsubscribe

    def _notify_changes(self) -> None:
        with self._operation_lock:
            if self._operation_depth:
                self._notification_pending = True
                return
        with self._change_observer_lock:
            observers = tuple(self._change_observers)
        for reference in observers:
            callback = reference()
            if callback is None:
                with self._change_observer_lock:
                    self._change_observers.discard(reference)
                continue
            try:
                callback()
            except Exception:
                # A committed save must never be reported as failed because a
                # presentation observer has already closed or cannot refresh.
                self._maintenance_warnings.append({"code": "change_observer_failed",
                    "message": "Saved successfully; a change observer could not refresh"})

    @property
    def root(self) -> Path:
        return self._root

    @property
    def project_file(self) -> Path:
        """The sole authoritative descriptor, separate from package archives."""
        return self._project_file

    @property
    @_store_operation
    def maintenance_warnings(self) -> list[dict[str, str]]:
        return deepcopy(self._maintenance_warnings)

    @property
    @_store_operation
    def manifest(self) -> dict:
        return deepcopy(self._manifest)

    @classmethod
    def create(cls, root, name: str, *, summary: str | None = None, notes: str | None = None,
               asset_limits=None):
        root = _path_root(root)
        value = _manifest({"kind": PROJECT_KIND, "version": PROJECT_VERSION,
                           "id": _new_id(), "name": name, "revision": 0,
                           "documents": [], "default_configuration_id": None,
                           **({"summary": summary} if summary is not None else {}),
                           **({"notes": notes} if notes is not None else {})})
        created = False
        try:
            root.mkdir(parents=True, exist_ok=False)
            created = True
            ensure_project_layout(root)
            content = json_bytes(value)
            fd, filename = tempfile.mkstemp(prefix=".project-create-", suffix=".tmp", dir=root)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            replace_with_retry(filename, root / MANIFEST_FILENAME)
        except FileExistsError as exc:
            raise ProjectError("destination_exists", "Project destination already exists") from exc
        except OSError as exc:
            if created:
                shutil.rmtree(root, ignore_errors=True)
            raise ProjectError("io_error", "Could not create project") from exc
        return cls(root, value, _digest(content), asset_limits=asset_limits)

    @classmethod
    def open(cls, project_file, *, asset_limits=None):
        """Open one explicitly named ``.coatproj`` file; never infer a directory."""
        root = project_file_root(project_file)
        project_file = root / Path(project_file).name
        value, content = read_json(project_file)
        store = cls(root, _manifest(value), _digest(content), project_file=project_file, asset_limits=asset_limits)
        # Recovery is intentionally never read here, even if corrupt.
        store.resolve_default()
        return store

    @_store_operation
    def update_information(self, information: Mapping, *, expected_revision: int | None = None) -> dict:
        """Atomically change project display metadata, retaining membership/ID.

        This is descriptor-only I/O; ordinary resource bytes are never copied.
        Empty optional fields are explicit values and missing old fields stay
        absent until edited. A stale whole-project revision rejects the update.
        """
        if not isinstance(information, Mapping) or set(information) - {"name", "summary", "notes"}:
            raise ProjectError("invalid_manifest", "Project information accepts name, summary and notes only")
        captured_fingerprint = self._fingerprint
        proposed = deepcopy(self._manifest)
        for field, value in information.items():
            if (not isinstance(value, str) or len(value) > (100 if field == "name" else 20000) or
                    (field == "name" and not value.strip())):
                raise ProjectError("invalid_manifest", "Invalid project " + field)
            proposed[field] = value.strip() if field == "name" else value
        proposed = _manifest(proposed)
        temporary = None
        changed = False
        with self._writer():
            if self._fingerprint != captured_fingerprint:
                raise ProjectError("stale_project", "Project changed while its information was prepared")
            if expected_revision is not None and (type(expected_revision) is not int or
                                                   expected_revision != self._manifest["revision"]):
                raise ProjectError("stale_project", "Project changed since its information was loaded")
            if proposed != self._manifest:
                proposed["revision"] += 1
                content = json_bytes(proposed)
                try:
                    fd, filename = tempfile.mkstemp(prefix=".project-information-", suffix=".tmp", dir=self.root)
                    temporary = Path(filename)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    replace_with_retry(temporary, self.project_file)
                    self._manifest, self._fingerprint = proposed, _digest(content)
                    changed = True
                    temporary = None
                except OSError as exc:
                    raise ProjectError("io_error", "Project information could not be published") from exc
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            result = {"id": self._manifest["id"], "name": self._manifest["name"],
                    "summary": self._manifest.get("summary", ""), "notes": self._manifest.get("notes", ""),
                    "revision": self._manifest["revision"]}
        if changed:
            self._notify_changes()
        return result

    def _index(self):
        return {row["id"]: row for row in self._manifest["documents"]}

    def _current(self):
        value, content = read_json(self.project_file)
        if _digest(content) != self._fingerprint:
            raise ProjectError("stale_project", "Project changed since it was opened; reopen before saving")
        return _manifest(value)

    def _read_record(self, row):
        target = self.root / managed_document_path(row["path"], row["kind"], row["id"])
        data, content = read_json(target)
        if _digest(content) != row["sha256"]:
            raise ProjectError("integrity_error", "Project document digest does not match: " + row["id"])
        normalized = normalize_document(row["kind"], data)
        if normalized != data:
            raise ProjectError("invalid_document", "Persisted project document is not normalized: " + row["id"])
        return normalized

    @_store_operation
    def _read_all(self):
        self._current()
        records = self._index()
        data, total = {}, 0
        for identity, row in records.items():
            data[identity] = self._read_record(row)
            total += len(json_bytes(data[identity]))
            if total > MAX_PROJECT_BYTES:
                raise ProjectError("capacity_exceeded", "Managed project input JSON exceeds the safety capacity limit")
        self._validate_graph(records, data)
        return _CapturedRecords(deepcopy(records), self._fingerprint), data

    @_store_operation
    def snapshot_saved_inputs(self, *, verify_assets=True) -> dict:
        """Return a validated, detached snapshot of every saved input document.

        Includes non-default designs, excludes recovery and runtime state. A
        second fingerprint check rejects concurrent publication during capture.
        """
        fingerprint, manifest = self._fingerprint, self.manifest
        records, data = self._read_all()
        for identity, row in records.items():
            if row["kind"] == "process":
                self.resolve_document(identity)
        self._current()
        snapshot = {"manifest": manifest, "documents": data}
        sources = self.asset_sources(records, data, verify=verify_assets)
        if sources:
            snapshot["assets"] = sources
        self._current()
        if self._fingerprint != fingerprint:
            raise ProjectError("stale_project", "Project changed during snapshot capture")
        return snapshot

    @_store_operation
    def asset_sources(self, records=None, data=None, *, verify=False, overrides=None) -> dict:
        from .project_binary_assets import normalize_binary_asset, stream_asset
        if records is None or data is None:
            records, data = self._read_all()
        sources = {}
        total = 0
        supplied = {} if overrides is None else overrides
        for identity, row in records.items():
            if row["kind"] != "binary_asset":
                continue
            metadata = normalize_binary_asset(data[identity])
            if PurePosixPath(metadata["payload_path"]).stem != identity:
                raise ProjectError("dependency_conflict", "Binary payload filename must match its managed identity")
            total += metadata["size"]
            self.asset_limits.check_total(total)
            source = supplied.get(identity, {"path": str(self.root / metadata["payload_path"])})
            if verify:
                stream_asset(source, metadata)
            sources[identity] = deepcopy(source)
        return sources

    @contextmanager
    def stage_asset_publications(self, records, data, assets=None, *, existing_spool_bytes=0,
                                 reservation_scope=None):
        """Prepare large owned inputs without holding a UI/session-host lock.

        Commit consumes only verified same-filesystem private receipts, making
        the final writer boundary exclusive rename plus descriptor publication.
        """
        from .project_binary_assets import capture_asset, file_signature, ROLE_SUFFIXES
        sources = self.asset_sources(records, data, overrides=assets)
        new_ids = [identity for identity in sources
                   if not (self.root / data[identity]["payload_path"]).exists()]
        if not new_ids:
            yield sources
            return
        stage_bytes = sum(data[key]["size"] for key in new_ids)
        self.asset_limits.check_total(existing_spool_bytes + stage_bytes)
        # The exchange reserves cumulatively before any private copy. Its
        # reservation lock is released during I/O and final service admission.
        with reservation_scope(stage_bytes) if reservation_scope is not None else nullcontext():
            directory = self.root / "assets"
            reject_links(directory)
            directory.mkdir(parents=True, exist_ok=True)
            registered = []
            with tempfile.TemporaryDirectory(prefix=".project-asset-stage-", dir=directory) as raw:
                try:
                    for identity in new_ids:
                        metadata = data[identity]
                        path = Path(raw) / (identity + ROLE_SUFFIXES[metadata["role"]])
                        sources[identity] = capture_asset(sources[identity], metadata, path)
                        with self._asset_receipt_lock:
                            self._asset_publication_receipts[str(path)] = (
                                metadata["sha256"], metadata["size"], file_signature(path))
                        registered.append(str(path))
                    yield sources
                finally:
                    with self._asset_receipt_lock:
                        for path in registered:
                            self._asset_publication_receipts.pop(path, None)

    @_store_operation
    def binary_asset_path(self, document_id: str) -> Path:
        from .project_binary_assets import file_signature, stream_asset, validate_asset_content
        row = self._index().get(document_id)
        if row is None or row["kind"] != "binary_asset":
            raise ProjectError("missing_dependency", "Managed binary input is absent")
        metadata = self._read_record(row)
        path = self.root / metadata["payload_path"]
        signature = file_signature(path)
        cached = self._asset_validation_cache.get(document_id)
        expected = (row["sha256"], signature)
        if cached != expected:
            stream_asset({"path": str(path)}, metadata)
            validate_asset_content(path, metadata)
            self._asset_validation_cache[document_id] = expected
        return path

    @staticmethod
    def _material_index(records, data):
        result = {}
        folded = {}
        for identity, row in records.items():
            if row["kind"] != "optical_material" or identity not in data:
                continue
            material = data[identity]["material"]
            key = material["id"]
            if key.casefold() in folded:
                raise ProjectError("dependency_conflict", "Duplicate project optical material ID: " + key)
            folded[key.casefold()] = identity
            result[key] = identity
        return result

    @staticmethod
    def _dependencies(identity, records, data, material_index):
        row, document = records[identity], data[identity]
        found = []
        handles = {key + EXTENSIONS[record["kind"]]: key for key, record in records.items()}
        for owner, field, expected_kind in file_references(row["kind"], document):
            reference = portable_path(owner[field])
            path = reference
            dependency = handles.get(reference)
            if dependency is None:
                raise ProjectError("missing_dependency", "Project file reference is absent: " + path)
            if records[dependency]["kind"] != expected_kind:
                raise ProjectError("dependency_conflict", "Project dependency kind mismatch: " + path)
            if dependency not in found:
                found.append(dependency)
        if row["kind"] == "deposition_configuration":
            dependency = document["process_document_id"]
            if dependency not in records or records[dependency]["kind"] != "process":
                raise ProjectError("missing_dependency", "Configuration process document is absent or has the wrong kind")
            found.append(dependency)
        if row["kind"] in ("film", "expression_template"):
            for material_id, set_id in document_material_references(row["kind"], document):
                dependency = material_index.get(material_id)
                if dependency is None:
                    raise ProjectError("missing_dependency", "Project optical material is missing: " + material_id)
                material = data[dependency]["material"]
                if set_id not in {optical["id"] for optical in material["optical_sets"]}:
                    raise ProjectError("missing_dependency", "Project optical set is missing: " + material_id + "/" + set_id)
                if dependency not in found:
                    found.append(dependency)
        return sorted(found)

    @classmethod
    def _validate_graph(cls, records, data):
        materials = cls._material_index(records, data)
        template_ids: set[str] = set()
        profile_ids: set[str] = set()
        for identity, document in data.items():
            if records[identity]["kind"] == "material_profile":
                key = document["material"]["id"].casefold()
                if key in profile_ids:
                    raise ProjectError("dependency_conflict", "Duplicate project material profile ID")
                profile_ids.add(key)
            if records[identity]["kind"] == "expression_template":
                template_id = document["template_id"]
                if template_id in template_ids:
                    raise ProjectError("dependency_conflict", "Duplicate project expression template ID: " + template_id)
                template_ids.add(template_id)
        for identity in data:
            if cls._dependencies(identity, records, data, materials) != sorted(records[identity]["dependencies"]):
                raise ProjectError("dependency_conflict", "Manifest dependency IDs do not match document references: " + identity)
        for identity, document in data.items():
            kind = records[identity]["kind"]
            if kind == "binary_asset":
                if PurePosixPath(document["payload_path"]).stem != identity:
                    raise ProjectError("dependency_conflict", "Binary payload identity differs from its document")
            elif kind in {"raytrace_request", "appearance_configuration"}:
                from .project_simulation_resources import simulation_asset_references
                for owner, field, role, _slot in simulation_asset_references(kind, document):
                    dependency = PurePosixPath(owner[field]).stem
                    asset = data.get(dependency)
                    if asset is None or records[dependency]["kind"] != "binary_asset" or asset["role"] != role:
                        raise ProjectError("dependency_conflict", "Simulation binary dependency has the wrong role")
                    expected = owner.get("sha256", owner.get("asset_sha256"))
                    if expected is not None and expected != asset["sha256"]:
                        raise ProjectError("integrity_error", "Simulation input reference differs from its binary digest")

    @_store_operation
    def resolve_document(self, document_id: str) -> dict:
        self._current()
        identifier(document_id)
        records = self._index()
        if document_id not in records:
            raise ProjectError("missing_document", "Project document ID does not exist")
        data, visiting, total = {}, set(), 0

        def visit(identity):
            nonlocal total
            if identity in visiting:
                raise ProjectError("dependency_conflict", "Project dependency cycle")
            if identity in data:
                return
            visiting.add(identity)
            data[identity] = self._read_record(records[identity])
            total += len(json_bytes(data[identity]))
            if total > MAX_PROJECT_BYTES:
                raise ProjectError("capacity_exceeded", "Resolved project input JSON exceeds the safety capacity limit")
            for dependency in records[identity]["dependencies"]:
                visit(dependency)
            visiting.remove(identity)

        visit(document_id)
        self._validate_graph(records, data)
        rows = [{**deepcopy(records[key]), "data": deepcopy(document)} for key, document in data.items()]
        result = {"project_id": self._manifest["id"], "revision": self._manifest["revision"],
                  "root_document_id": document_id, "documents": rows,
                  "materials": sorted([deepcopy(document["material"]) for key, document in data.items()
                                       if records[key]["kind"] == "optical_material"],
                                      key=lambda material: material["id"])}
        assets = self.asset_sources({key: records[key] for key in data}, data)
        if assets:
            result["assets"] = assets
        process_ids = [key for key in data if records[key]["kind"] == "process"]
        # Reuse the current runtime validator in RAM; it does not calculate.
        if len(process_ids) == 1:
            from .input_snapshot import canonical_json_bytes, runtime_from_snapshot
            process_id = process_ids[0]
            snapshot_rows = []
            for key, document in data.items():
                if records[key]["kind"] in {"process", "machine", "source", "mask", "substrate"}:
                    snapshot_rows.append({"kind": records[key]["kind"],
                                          "path": "designs/" + key + EXTENSIONS[records[key]["kind"]],
                                          "data": deepcopy(document),
                                          "sha256": _digest(canonical_json_bytes(document))})
            snapshot = {"kind": "coat_sim.input_snapshot", "version": 1,
                        "process_path": "designs/" + process_id + EXTENSIONS["process"],
                        "files": snapshot_rows}
            try:
                result["runtime"] = runtime_from_snapshot(snapshot)
            except (ValueError, KeyError, TypeError) as exc:
                raise ProjectError("invalid_dependency_graph", str(exc)) from exc
            result["input_snapshot"] = snapshot
        if records[document_id]["kind"] == "deposition_configuration":
            result["configuration"] = deepcopy(data[document_id])
        return result

    @_store_operation
    def resolve_default(self) -> dict | None:
        default = self._manifest["default_configuration_id"]
        if default is None:
            self._current()
            return None
        return self.resolve_document(default)

    @contextmanager
    def _writer(self):
        lock = self.root / ".project-write.lock"
        reject_links(lock)
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProjectError("project_locked", "Project has an active or interrupted writer lock") from exc
        except OSError as exc:
            raise ProjectError("io_error", "Could not lock project") from exc
        try:
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(json_bytes({"kind": "coat_sim.project_writer", "version": 1,
                                             "project_id": self._manifest["id"], "revision": self._manifest["revision"],
                                             "pid": os.getpid(), "writer_id": _new_id(),
                                             "created_at": datetime.now(timezone.utc).isoformat()}))
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise ProjectError("io_error", "Could not record project writer identity") from exc
            self._current()
            yield
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                # Cleanup cannot turn a committed save into an apparent failed
                # save, or hide the original failure of a rolled-back write.
                self._maintenance_warnings.append({
                    "code": "writer_lock_cleanup_failed",
                    "message": "Writer lock could not be removed; inspect ownership before manual recovery",
                    "path": str(lock),
                })

    @_store_operation
    def _publish(self, records, data, *, default=None, change_default=False, assets=None,
                 expected_fingerprint: str | None = None) -> dict[str, str]:
        result = self._publish_transaction(records, data, default=default, change_default=change_default,
            assets=assets, expected_fingerprint=expected_fingerprint)
        self._notify_changes()
        return result

    @_store_operation
    def _publish_transaction(self, records, data, *, default=None, change_default=False, assets=None,
                             expected_fingerprint: str | None = None) -> dict[str, str]:
        if len(records) > MAX_PROJECT_DOCUMENTS:
            raise ProjectError("capacity_exceeded", "Project has too many managed input documents")
        captured_fingerprint = getattr(records, "fingerprint", self._fingerprint)
        if captured_fingerprint != self._fingerprint:
            raise ProjectError("stale_project", "Project changed after its documents were captured")
        if expected_fingerprint is not None and expected_fingerprint != captured_fingerprint:
            raise ProjectError("stale_project", "Project changed after the confirmed asset operation")
        # Capture dependency identities before moving all working versions.
        materials = self._material_index(records, data)
        dependencies = {key: self._dependencies(key, records, data, materials) for key in records}
        generation = _new_id()
        revision_directory = f"r{self._manifest['revision'] + 1:08d}"
        # Never merge into an existing category revision, including leftovers
        # from an interrupted writer or unrelated user folders.
        categories = sorted({PROJECT_TYPED_DIRECTORY_BY_KIND[row["kind"]] for row in records.values()})
        for category in categories:
            reject_links(self.root / category)
        attempt, base_revision = 1, revision_directory
        while any((self.root / category / revision_directory).exists() or any(
                child.name.casefold() == revision_directory.casefold()
                for child in (self.root / category).iterdir())
                  for category in categories if (self.root / category).is_dir()):
            attempt += 1
            revision_directory = base_revision + "-" + str(attempt)
        occupied: dict[str, set[str]] = {}
        from .resource_information import resource_display_name
        changed_records = deepcopy(records)
        changed_data = deepcopy(data)
        for identity, row in changed_records.items():
            category = PROJECT_TYPED_DIRECTORY_BY_KIND[row["kind"]]
            filename = safe_resource_filename(resource_display_name(row["kind"], data[identity]), row["kind"],
                                              occupied=occupied.setdefault(category, set()))
            row["path"] = f"{category}/{revision_directory}/{filename}"
            row["dependencies"] = dependencies[identity]
            for owner, field, _ in file_references(row["kind"], changed_data[identity]):
                # Typed references are stable UUID handles, independent of a
                # display name, category, and immutable storage revision.
                target = PurePosixPath(owner[field]).stem
                if target not in dependencies[identity]:
                    raise ProjectError("dependency_conflict", "Ambiguous dependency while rebasing project")
                owner[field] = target + EXTENSIONS[changed_records[target]["kind"]]
        self._validate_graph(changed_records, changed_data)
        content = {key: json_bytes(document) for key, document in changed_data.items()}
        if any(len(encoded) > MAX_FILE_BYTES for encoded in content.values()):
            raise ProjectError("capacity_exceeded", "Managed project JSON document exceeds size limit")
        if sum(map(len, content.values())) > MAX_PROJECT_BYTES:
            raise ProjectError("capacity_exceeded", "Managed project input JSON exceeds the safety capacity limit")
        for key, row in changed_records.items():
            row["sha256"] = _digest(content[key])
        proposed = deepcopy(self._manifest)
        proposed["revision"] += 1
        proposed["documents"] = list(changed_records.values())
        if change_default:
            proposed["default_configuration_id"] = default
        proposed = _manifest(proposed)
        # Validate every process closure with existing slot/geometry rules before
        # commit. This does not require any source file and starts no calculation.
        preview = _PreparedProject(self.root, proposed, self._fingerprint, changed_data)
        for identity, row in changed_records.items():
            if row["kind"] in ("process", "deposition_configuration"):
                preview.resolve_document(identity)
        asset_sources = self.asset_sources(changed_records, changed_data, overrides=assets)
        stage = self.root / "objects" / generation
        stage_created = False
        published_assets: list[Path] = []
        published_directories: list[tuple[Path, tuple, dict[str, tuple]]] = []
        temporary = None
        with self._writer():
            try:
                if self._fingerprint != captured_fingerprint:
                    raise ProjectError("stale_project", "Project changed while its save was prepared")
                reject_links(stage)
                stage.mkdir(parents=False, exist_ok=False)
                stage_created = True
                previous = self._index()
                from .project_binary_assets import capture_asset, stream_asset
                for identity, source in asset_sources.items():
                    metadata = changed_data[identity]
                    target = self.root / metadata["payload_path"]
                    reject_links(target)
                    if target.exists():
                        old = previous.get(identity)
                        if old is not None and old["sha256"] == changed_records[identity]["sha256"]:
                            from .project_binary_assets import file_signature
                            if file_signature(target)[3] != metadata["size"]:
                                raise ProjectError("integrity_error", "Owned binary input size changed")
                        else:
                            stream_asset({"path": str(target)}, metadata)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    reject_links(target.parent)
                    source_path = source.get("path")
                    with self._asset_receipt_lock:
                        receipt = self._asset_publication_receipts.get(source_path)
                    if receipt is not None:
                        from .project_binary_assets import file_signature
                        if receipt != (metadata["sha256"], metadata["size"], file_signature(source_path)):
                            raise ProjectError("integrity_error", "Prepared binary input changed before publication")
                        _rename_directory_no_replace(Path(source_path), target)
                        published_assets.append(target)
                        continue
                    fd, filename = tempfile.mkstemp(prefix=".asset-publish-", dir=target.parent)
                    os.close(fd)
                    temporary_asset = Path(filename)
                    temporary_asset.unlink()
                    try:
                        capture_asset(source, metadata, temporary_asset)
                        if os.name == "nt":
                            os.rename(temporary_asset, target)
                        else:
                            os.link(temporary_asset, target)
                        published_assets.append(target)
                    finally:
                        temporary_asset.unlink(missing_ok=True)
                for identity, row in changed_records.items():
                    target = stage / row["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    old = previous.get(identity)
                    # Unchanged immutable input JSON can share disk blocks. A
                    # host without hard-link support falls back to a byte copy.
                    if old is not None and old["sha256"] == row["sha256"]:
                        source = self.root / old["path"]
                        reject_links(source)
                        try:
                            os.link(source, target)
                            continue
                        except OSError:
                            pass
                    with target.open("xb") as stream:
                        stream.write(content[identity])
                        stream.flush()
                        os.fsync(stream.fileno())
                for category in categories:
                    publication_target = self.root / category / revision_directory
                    staged_directory = stage / category / revision_directory
                    info = staged_directory.lstat()
                    directory_identity = (info.st_dev, info.st_ino)
                    owned_files = {
                        PurePosixPath(row["path"]).name:
                        _rollback_file_signature((stage / row["path"]).lstat())
                        for row in changed_records.values()
                        if PROJECT_TYPED_DIRECTORY_BY_KIND[row["kind"]] == category
                    }
                    reject_links(publication_target)
                    publication_target.parent.mkdir(parents=True, exist_ok=True)
                    reject_links(publication_target.parent)
                    _rename_directory_no_replace(staged_directory, publication_target)
                    published_directories.append((publication_target, directory_identity, owned_files))
                manifest_content = json_bytes(proposed)
                fd, name = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=self.root)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(manifest_content)
                    stream.flush()
                    os.fsync(stream.fileno())
                replace_with_retry(temporary, self.project_file)
                self._manifest, self._fingerprint = proposed, _digest(manifest_content)
                temporary = None
                shutil.rmtree(stage, ignore_errors=True)
                # Each service may own a different operation lock. Return this
                # writer's immutable revision receipt before another writer
                # can replace the shared store's current manifest.
                return {key: row["sha256"] for key, row in changed_records.items()}
            except (OSError, ProjectError) as exc:
                for directory, identity, files in published_directories:
                    if not _rollback_published_directory(directory, identity, files):
                        self._maintenance_warnings.append({
                            "code": "publication_rollback_retained",
                            "message": "Uncommitted resource directory retained to preserve concurrent changes",
                            "path": str(directory),
                        })
                for asset in published_assets:
                    asset.unlink(missing_ok=True)
                    try:
                        asset.parent.rmdir()
                    except OSError:
                        pass
                if stage_created:
                    shutil.rmtree(stage, ignore_errors=True)
                if isinstance(exc, ProjectError):
                    raise
                raise ProjectError("io_error", "Project transaction could not be published") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    @_store_operation
    def import_document(self, path, materials: Mapping | None = None) -> str:
        records, data = self._read_all()
        root_path = Path(os.path.abspath(Path(path).expanduser()))
        reject_links(root_path)
        if materials is None:
            materials = {}
        if not isinstance(materials, Mapping):
            raise ProjectError("invalid_document", "Explicit material input must be an ID-to-record mapping")
        supplied = {key: normalize_optical_material(value) for key, value in materials.items()}
        if any(key != value["id"] for key, value in supplied.items()):
            raise ProjectError("dependency_conflict", "Explicit material mapping keys must match material IDs")
        pending_generation = _new_id()
        captured: dict[Path, str] = {}
        bytes_read = 0

        def add(kind, document):
            identity = _new_id()
            if identity in records:
                raise ProjectError("dependency_conflict", "Generated project document identity collided")
            records[identity] = {"id": identity, "kind": kind,
                                 "path": f"objects/{pending_generation}/{identity}{EXTENSIONS[kind]}",
                                 "sha256": "0" * 64, "dependencies": []}
            data[identity] = document
            return identity

        def capture(target, expected=None):
            nonlocal bytes_read
            reject_links(target)
            target = target.resolve()
            if target in captured:
                identity = captured[target]
                if expected is not None and records[identity]["kind"] != expected:
                    raise ProjectError("dependency_conflict", "One external file cannot have conflicting dependency kinds")
                return identity
            kind, document, byte_count = external_document(target, expected)
            bytes_read += byte_count
            if bytes_read > MAX_PROJECT_BYTES or len(records) >= MAX_PROJECT_DOCUMENTS:
                raise ProjectError("capacity_exceeded", "External dependency graph exceeds project capacity")
            identity = add(kind, document)
            captured[target] = identity
            for owner, field, child_kind in file_references(kind, document):
                reference = Path(owner[field])
                dependency = capture(reference if reference.is_absolute() else target.parent / reference, child_kind)
                owner[field] = dependency + EXTENSIONS[records[dependency]["kind"]]
            if kind == "film":
                self._ensure_material_dependencies(records, data, kind, document, supplied)
            return identity

        identity = capture(root_path)
        # Existing film dependencies may refer to prior-generation material rows;
        # material links are ID-based, and file links are per-capture generation.
        self._publish(records, data)
        return identity

    @staticmethod
    def _add_document(records, data, kind, document):
        identity = _new_id()
        if identity in records:
            raise ProjectError("dependency_conflict", "Generated project document identity collided")
        records[identity] = {"id": identity, "kind": kind,
                             "path": f"objects/{_new_id()}/{identity}{EXTENSIONS[kind]}",
                             "sha256": "0" * 64, "dependencies": []}
        data[identity] = document
        return identity

    @classmethod
    def _ensure_material_dependencies(cls, records, data, kind, document, materials):
        if materials is None:
            materials = {}
        if not isinstance(materials, Mapping):
            raise ProjectError("invalid_document", "Explicit material input must be an ID-to-record mapping")
        supplied = {key: normalize_optical_material(value) for key, value in materials.items()}
        if any(key != value["id"] for key, value in supplied.items()):
            raise ProjectError("dependency_conflict", "Explicit material mapping keys must match material IDs")
        material_index = cls._material_index(records, data)
        for material_id, set_id in document_material_references(kind, document):
            explicit = supplied.get(material_id)
            existing_id = material_index.get(material_id)
            if existing_id is not None and explicit is not None and data[existing_id]["material"] != explicit:
                raise ProjectError("dependency_conflict", "Project material ID has different content: " + material_id)
            if existing_id is None:
                if explicit is None:
                    raise ProjectError("missing_dependency", "Document requires an explicit project optical material: " + material_id)
                if material_id.casefold() in {key.casefold() for key in material_index}:
                    raise ProjectError("dependency_conflict", "Project material IDs differ only by case")
                existing_id = cls._add_document(records, data, "optical_material", {
                    "kind": "coat_sim.project_optical_material", "version": 1, "material": explicit})
                material_index[material_id] = existing_id
            if set_id not in {row["id"] for row in data[existing_id]["material"]["optical_sets"]}:
                raise ProjectError("missing_dependency", "Document optical set is missing: " + material_id + "/" + set_id)

    @_store_operation
    def create_document(self, kind: str, document: Mapping, *, materials: Mapping | None = None,
                        make_default: bool = False) -> str:
        """Create a managed typed draft with explicit project-local dependencies."""
        if kind not in KINDS:
            raise ProjectError("invalid_document", "Unsupported managed document kind")
        records, data = self._read_all()
        normalized = normalize_document(kind, document)
        if kind in ("film", "expression_template"):
            self._ensure_material_dependencies(records, data, kind, normalized, materials)
        elif materials is not None:
            raise ProjectError("invalid_document", "Optical resource transfer requires a film or expression template")
        identity = self._add_document(records, data, kind, normalized)
        self._rebind_file_references(records, data, [identity])
        if make_default and kind != "deposition_configuration":
            raise ProjectError("dependency_conflict", "Only a deposition configuration can be the default")
        # A successful explicit configuration save owns the next opening root.
        # Keep make_default as a compatibility argument, never an opt-out.
        self._publish(records, data, default=identity, change_default=kind == "deposition_configuration")
        return identity

    @staticmethod
    def _saved_configuration_target(records, saved_ids, explicit=_DEFAULT_UNCHANGED):
        configurations = {identity for identity in saved_ids
                          if records[identity]["kind"] == "deposition_configuration"}
        if explicit is not _DEFAULT_UNCHANGED:
            if explicit not in configurations:
                raise ProjectError("dependency_conflict", "Opening root must be a configuration saved in this transaction")
            return explicit, True
        if len(configurations) == 1:
            return next(iter(configurations)), True
        # An unordered multi-configuration batch has no last business item.
        return None, False

    @_store_operation
    def write_documents(self, new_documents=(), *, updates=None, expected_revisions=None) -> list[str]:
        """Publish explicit managed creations and updates in a single transaction."""
        records, data = self._read_all()
        updates = {} if updates is None else updates
        expected = {} if expected_revisions is None else expected_revisions
        if not isinstance(updates, Mapping) or not isinstance(expected, Mapping) or set(expected) - set(updates):
            raise ProjectError("invalid_document", "Invalid managed transaction updates/revisions")
        for identity, document in updates.items():
            identifier(identity)
            if identity not in records:
                raise ProjectError("missing_document", "Project document ID does not exist")
            if identity in expected and records[identity]["sha256"] != expected[identity]:
                raise ProjectError("stale_document", "Managed document changed since it was loaded")
            kind = records[identity]["kind"]
            normalized = normalize_document(kind, document)
            if kind == "expression_template" and normalized["template_id"] != data[identity]["template_id"]:
                raise ProjectError("dependency_conflict", "Saving a template cannot change its template ID")
            data[identity] = normalized
        created = []
        for kind, document in new_documents:
            if kind not in KINDS:
                raise ProjectError("invalid_document", "Unsupported managed document kind")
            created.append(self._add_document(records, data, kind, normalize_document(kind, document)))
        if not created and not updates:
            return []
        self._rebind_file_references(records, data, [*created, *updates])
        default, change_default = self._saved_configuration_target(records, [*created, *updates])
        self._publish(records, data, default=default, change_default=change_default)
        return created

    @_store_operation
    def remove_document(self, document_id: str, *, expected_revision: str | None = None) -> None:
        """Remove an unreferenced logical document; immutable old generations remain."""
        identifier(document_id)
        records, data = self._read_all()
        if document_id not in records:
            raise ProjectError("missing_document", "Project document ID does not exist")
        if expected_revision is not None and records[document_id]["sha256"] != expected_revision:
            raise ProjectError("stale_document", "Managed document changed since it was loaded")
        if self._manifest["default_configuration_id"] == document_id or any(
                document_id in row["dependencies"] for row in records.values()):
            raise ProjectError("dependency_conflict", "Referenced or default documents cannot be removed")
        del records[document_id]
        del data[document_id]
        self._publish(records, data)

    @staticmethod
    def _rebind_file_references(records, data, identities):
        names = {key + EXTENSIONS[row["kind"]]: key for key, row in records.items()}
        aliases: dict[str, list[str]] = {}
        for key, row in records.items():
            aliases.setdefault(PurePosixPath(row["path"]).name, []).append(key)
        for filename, keys in aliases.items():
            if len(keys) == 1:
                names.setdefault(filename, keys[0])
        for identity in identities:
            for owner, field, expected in file_references(records[identity]["kind"], data[identity]):
                filename = portable_path(owner[field])
                target = names.get(filename)
                if target is None or records[target]["kind"] != expected:
                    raise ProjectError("missing_dependency", "Save requires an unambiguous typed project-local dependency handle")
                owner[field] = target + EXTENSIONS[records[target]["kind"]]

    @_store_operation
    def save_documents(self, updates: Mapping[str, Mapping], *,
                       expected_revisions: Mapping[str, str] | None = None,
                       default_configuration_id=_DEFAULT_UNCHANGED) -> None:
        """Atomically validate and save multiple managed drafts and default choice.

        Expected revisions are document hashes, not whole-project revision numbers.
        Rejected edits or invalid descendant runtime leave the manifest unchanged.
        """
        if not isinstance(updates, Mapping) or not updates:
            raise ProjectError("invalid_document", "A save transaction needs document updates")
        records, data = self._read_all()
        expected = {} if expected_revisions is None else dict(expected_revisions)
        if set(expected) - set(updates):
            raise ProjectError("invalid_document", "Expected revision has no matching update")
        for identity, document in updates.items():
            identifier(identity)
            if identity not in records:
                raise ProjectError("missing_document", "Project document ID does not exist")
            if identity in expected and records[identity]["sha256"] != expected[identity]:
                raise ProjectError("stale_document", "Managed document changed since this draft was loaded")
            kind = records[identity]["kind"]
            normalized = normalize_document(kind, document)
            if kind == "expression_template" and normalized["template_id"] != data[identity]["template_id"]:
                raise ProjectError("dependency_conflict", "Saving a template cannot change its template ID")
            data[identity] = normalized
        self._rebind_file_references(records, data, updates)
        default, change_default = self._saved_configuration_target(records, updates, default_configuration_id)
        self._publish(records, data, default=default, change_default=change_default)

    @_store_operation
    def save_deposition_configuration(self, name: str, process_document: Mapping, parameters=None, *,
                                      process_document_id: str | None = None,
                                      expected_revision: str | None = None,
                                      make_default: bool = True, description: str | None = None,
                                      notes: str | None = None) -> tuple[str, str]:
        """Atomically save a process, configuration and latest-saved opening root."""
        records, data = self._read_all()
        normalized = normalize_document("process", process_document)
        if process_document_id is None:
            process_document_id = self._add_document(records, data, "process", normalized)
        else:
            identifier(process_document_id)
            if process_document_id not in records or records[process_document_id]["kind"] != "process":
                raise ProjectError("missing_document", "Configuration needs an existing project process")
            if expected_revision is not None and records[process_document_id]["sha256"] != expected_revision:
                raise ProjectError("stale_document", "Process changed since this draft was loaded")
            data[process_document_id] = normalized
        document = normalize_document("deposition_configuration", {
            "kind": "coat_sim.deposition_configuration", "version": 1, "name": name,
            "process_document_id": process_document_id, "parameters": {} if parameters is None else parameters,
            **({"description": description} if description is not None else {}),
            **({"notes": notes} if notes is not None else {})})
        configuration = self._add_document(records, data, "deposition_configuration", document)
        self._rebind_file_references(records, data, [process_document_id])
        self._publish(records, data, default=configuration, change_default=True)
        return configuration, process_document_id

    @_store_operation
    def add_deposition_configuration(self, name: str, process_document_id: str, parameters=None, *,
                                     description: str | None = None, notes: str | None = None) -> str:
        records, data = self._read_all()
        document = normalize_document("deposition_configuration", {
            "kind": "coat_sim.deposition_configuration", "version": 1, "name": name,
            "process_document_id": process_document_id, "parameters": {} if parameters is None else parameters,
            **({"description": description} if description is not None else {}),
            **({"notes": notes} if notes is not None else {})})
        identity = self._add_document(records, data, "deposition_configuration", document)
        self._publish(records, data, default=identity, change_default=True)
        return identity

    @_store_operation
    def save_document(self, document_id: str, document: Mapping, *, materials: Mapping | None = None) -> None:
        """Save a current working document, retaining its ID and every ancestor.

        File references must identify existing managed documents by their stable
        UUID filename. Import external files first; saving never follows them.
        """
        identifier(document_id)
        records, data = self._read_all()
        if document_id not in records:
            raise ProjectError("missing_document", "Project document ID does not exist")
        kind = records[document_id]["kind"]
        normalized = normalize_document(kind, document)
        if kind == "expression_template" and normalized["template_id"] != data[document_id]["template_id"]:
            raise ProjectError("dependency_conflict", "Saving a template cannot change its template ID")
        if kind in ("film", "expression_template"):
            self._ensure_material_dependencies(records, data, kind, normalized, materials)
        elif materials is not None:
            raise ProjectError("invalid_document", "Explicit optical resource transfer requires a film or expression template")
        data[document_id] = normalized
        self._rebind_file_references(records, data, [document_id])
        default, change_default = self._saved_configuration_target(records, [document_id])
        previous = self._index()
        if (set(records) == set(previous) and data == {key: self._read_record(row) for key, row in previous.items()}
                and (not change_default or self._manifest["default_configuration_id"] == default)):
            return
        self._publish(records, data, default=default, change_default=change_default)

    @_store_operation
    def set_default_configuration(self, configuration_id: str | None) -> None:
        records, data = self._read_all()
        if configuration_id is not None:
            identifier(configuration_id)
            if configuration_id not in records or records[configuration_id]["kind"] != "deposition_configuration":
                raise ProjectError("dependency_conflict", "Default root must be an existing deposition configuration")
        if configuration_id == self._manifest["default_configuration_id"]:
            return
        self._publish(records, data, default=configuration_id, change_default=True)

    def export_document(self, document_id: str, destination) -> dict:
        graph = self.resolve_document(document_id)
        destination = _path_root(destination)
        if destination.exists():
            raise ProjectError("destination_exists", "Export destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        reject_links(destination.parent)
        stage = Path(tempfile.mkdtemp(prefix=".project-export-", dir=destination.parent))
        try:
            rows = []
            for row in graph["documents"]:
                filename = row["id"] + EXTENSIONS[row["kind"]]
                with (stage / filename).open("xb") as stream:
                    stream.write(json_bytes(row["data"]))
                rows.append({"id": row["id"], "kind": row["kind"], "path": filename,
                             "dependencies": row["dependencies"]})
            for identity, source in graph.get("assets", {}).items():
                from .project_binary_assets import capture_asset
                metadata = next(row["data"] for row in graph["documents"] if row["id"] == identity)
                payload = stage / metadata["payload_path"]
                payload.parent.mkdir(parents=True, exist_ok=True)
                capture_asset(source, metadata, payload)
            envelope = {"kind": "coat_sim.project_export", "version": 1,
                        "root_document_id": document_id, "documents": rows}
            (stage / "export.json").write_bytes(json_bytes(envelope))
            # The final publication primitive also refuses a destination that
            # appears after this convenient early check.
            if destination.exists():
                raise ProjectError("destination_exists", "Export destination appeared during publication")
            _rename_directory_no_replace(stage, destination)
            return {"destination": str(destination), "root_document_id": document_id,
                    "documents": rows}
        except FileExistsError as exc:
            raise ProjectError("destination_exists", "Export destination appeared during publication") from exc
        except OSError as exc:
            raise ProjectError("io_error", "Could not export project dependency closure") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    @_store_operation
    def record_recovery(self, document_ids) -> None:
        if (not isinstance(document_ids, list) or any(not isinstance(item, str) for item in document_ids)
                or len(document_ids) != len(set(document_ids))):
            raise ProjectError("invalid_recovery", "Recovery document IDs must be a unique list")
        records = self._index()
        for identity in document_ids:
            identifier(identity)
            if identity not in records:
                raise ProjectError("missing_document", "Recovery document ID does not exist")
        value = {"kind": "coat_sim.project_recovery", "version": 1,
                 "project_id": self._manifest["id"], "revision": self._manifest["revision"],
                 "document_ids": document_ids}
        with self._writer():
            target = self.root / RECOVERY_FILENAME
            reject_links(target)
            fd, filename = tempfile.mkstemp(prefix=".recovery-", suffix=".tmp", dir=self.root)
            temporary = Path(filename)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(json_bytes(value))
                    stream.flush()
                    os.fsync(stream.fileno())
                replace_with_retry(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

    @_store_operation
    def recover_session(self) -> dict | None:
        """Explicit abnormal-recovery metadata read; never replaces saved data."""
        self._current()
        path = self.root / RECOVERY_FILENAME
        reject_links(path)
        if not path.exists():
            return None
        value, _ = read_json(path)
        if (set(value) != {"kind", "version", "project_id", "revision", "document_ids"} or
                value.get("kind") != "coat_sim.project_recovery" or type(value.get("version")) is not int or value["version"] != 1):
            raise ProjectError("invalid_recovery", "Invalid project recovery record")
        if (type(value["revision"]) is not int or value["project_id"] != self._manifest["id"]
                or value["revision"] != self._manifest["revision"]):
            raise ProjectError("stale_recovery", "Recovery record does not match current project revision")
        ids = value["document_ids"]
        if (not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
                or len(ids) != len(set(ids)) or any(identity not in self._index() for identity in ids)):
            raise ProjectError("invalid_recovery", "Recovery record has unknown or duplicate documents")
        return {**deepcopy(value), "graphs": [self.resolve_document(identity) for identity in ids]}


class _PreparedProject(ProjectStore):
    """An in-memory transaction preview using the ordinary resolution boundary."""
    def __init__(self, root, manifest, fingerprint, documents, assets=None):
        super().__init__(root, manifest, fingerprint)
        self._prepared_documents = documents
        self._prepared_assets = {} if assets is None else assets

    def asset_sources(self, records=None, data=None, *, verify=False, overrides=None):
        return super().asset_sources(records, data, verify=verify,
                                    overrides=self._prepared_assets if overrides is None else overrides)

    def _current(self):
        return self.manifest

    def _read_record(self, row):
        return deepcopy(self._prepared_documents[row["id"]])
