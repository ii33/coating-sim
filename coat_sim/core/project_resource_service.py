"""Headless, explicitly bound resource editing and managed transaction boundary.

The numerical CoatService remains reusable. All resource paths/references are
resolved by manifest identity and never fall back globally. Film material
pickers may expose a detached read-only application catalog; the project owns
only the explicitly captured optical records when a film is saved.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
import hashlib
from pathlib import Path, PurePosixPath
from threading import RLock, get_ident
from typing import Any, Iterator, TYPE_CHECKING, cast

from .api import CoatService, Document, ValidationResult
from .design_files import design_kind_from_path
from .input_snapshot import canonical_json_bytes, runtime_from_snapshot
from .machine_profiles import validate_mask_profile
from .project_dependencies import (EXTENSIONS, ProjectError, file_references,
                                   identifier, normalize_document, reject_links)
from .project_store import ProjectStore
from .project_paths import (project_default_directory,
                            project_document_handle)

if TYPE_CHECKING:
    from coat_sim.project_session import ProjectBinding, ProjectSessionHost

RESOURCE_KINDS = frozenset({"source", "substrate", "mask", "machine", "process", "material_profile", "raytrace_request", "appearance_configuration"})


@dataclass
class ProjectMutationGate:
    frozen: bool = False
    close_save_threads: set[int] = dataclass_field(default_factory=set)


class ProjectResourceService(CoatService):
    """CoatService-compatible project document editor with strict managed I/O."""
    is_project_service = True

    def __init__(self, store: ProjectStore, *, session_host: ProjectSessionHost | None = None,
                 binding: ProjectBinding | None = None, gate: ProjectMutationGate | None = None):
        if (session_host is None) != (binding is None):
            raise ValueError("Project host and binding must be supplied together")
        if session_host is not None:
            assert binding is not None
            if session_host.store_for(binding) is not store:
                raise ProjectError("dependency_conflict", "Resource store differs from its project binding")
        super().__init__(store.root, restrict_to_root=True)
        self.store = store
        self.project_store = store
        self.project_id = store.manifest["id"]
        self._session_host = session_host
        self.binding = binding
        self._gate = gate or ProjectMutationGate()
        self._resource_lock = RLock()
        self._loaded_revisions: dict[str, str] = {}
        self._project_film_service: Any = None

    @property
    def resource_exchange(self):
        """Lazy, headless owner of resource browsing and selective transfer plans."""
        with self._resource_lock:
            if not hasattr(self, "_resource_exchange"):
                from .project_resource_transfer import ProjectResourceExchange
                self._resource_exchange = ProjectResourceExchange(self)
            return self._resource_exchange

    def close_resource_exchange(self) -> None:
        with self._resource_lock:
            self._asset_deletion_closed = True
            exchange = getattr(self, "_resource_exchange", None)
            if exchange is not None:
                exchange.close()

    @property
    def resource_deletions(self):
        with self._resource_lock:
            if not hasattr(self, "_resource_deletions"):
                from .asset_deletion import AssetDeletionService
                self._resource_deletions = AssetDeletionService(self)
            return self._resource_deletions

    def preview_resource_deletion(self, catalog_id: str, selected_ids: list[str]) -> dict:
        return self.resource_deletions.preview(catalog_id, selected_ids)

    def apply_resource_deletion(self, plan_id: str) -> dict:
        return self.resource_deletions.apply(plan_id)

    def cancel_resource_deletion(self, plan_id: str) -> None:
        self.resource_deletions.cancel(plan_id)

    def list_resource_deletions(self) -> list[dict]:
        return self.resource_deletions.list_receipts()

    def restore_resource_deletion(self, receipt_id: str) -> dict:
        return self.resource_deletions.restore(receipt_id)

    def bind_asset_library_view_owner(self, controller) -> None:
        """Identify this route's embedded material presentation, never dirty edits."""
        with self._operation():
            if controller is None:
                self._asset_library_view_owner = None
                return
            from .asset_deletion import library_view_owner_matches
            if not library_view_owner_matches(self, controller):
                raise ProjectError("wrong_scope", "材料详情不属于此工程资产库窗口。")
            self._asset_library_view_owner = controller

    def inspect_resources(self, source="project") -> dict:
        return self.resource_exchange.inspect_resources(source)

    def preview_resource_transfer(self, catalog_id: str, selected_ids: list[str],
                                  target="project") -> dict:
        return self.resource_exchange.preview_resource_transfer(catalog_id, selected_ids, target)

    def apply_resource_transfer(self, plan_id: str, decisions=None) -> dict:
        return self.resource_exchange.apply_resource_transfer(plan_id, decisions)

    def cancel_resource_transfer(self, plan_id: str) -> None:
        self.resource_exchange.cancel_resource_transfer(plan_id)

    def release_resource_catalog(self, catalog_id: str) -> None:
        self.resource_exchange.release_resource_catalog(catalog_id)

    def fork_editor(self) -> ProjectResourceService:
        return type(self)(self.store, session_host=self._session_host, binding=self.binding, gate=self._gate)

    @contextmanager
    def _operation(self, *, mutate: bool = False) -> Iterator[None]:
        if self._session_host is not None:
            assert self.binding is not None
            with self._session_host.operation_scope(self.binding, close_policy=True):
                with self._resource_lock:
                    if mutate and self._gate.frozen and get_ident() not in self._gate.close_save_threads:
                        raise ProjectError("project_editing_frozen", "Project edits are paused for a close decision")
                    yield
        else:
            with self._resource_lock:
                if mutate and self._gate.frozen and get_ident() not in self._gate.close_save_threads:
                    raise ProjectError("project_editing_frozen", "Project edits are paused for a close decision")
                yield

    @contextmanager
    def coordinated_save(self):
        """Permit only this synchronous close-save thread through the edit freeze."""
        with self._operation():
            thread = get_ident()
            already = thread in self._gate.close_save_threads
            self._gate.close_save_threads.add(thread)
            try:
                yield
            finally:
                if not already:
                    self._gate.close_save_threads.discard(thread)

    @property
    def film_design_service(self):
        if self._session_host is not None:
            from .project_film_session import ProjectFilmWorkspace
            assert self.binding is not None
            return ProjectFilmWorkspace.for_project(self._session_host, self.binding).service
        if self._project_film_service is None:
            from .project_film_service import ProjectFilmService, ProjectFilmStore
            self._project_film_service = ProjectFilmService(cast(ProjectFilmStore, self.store))
        return self._project_film_service

    def default_directory(self, kind: str) -> Path:
        directory = project_default_directory(self.root, kind)
        # Older projects are not migrated just to show a file dialog. They
        # retain a valid chooser root until the typed layout exists.
        return directory if directory.is_dir() else self.root

    def refresh_bound_film_materials(self) -> bool:
        """Refresh only an existing film workspace in this project binding."""
        if self._session_host is None or self.binding is None:
            return False
        workspace = self._session_host.resource_for(self.binding, "film_workspace")
        if workspace is None:
            return False
        return bool(workspace.refresh_materials())

    def _record(self, document_id: str) -> dict:
        identifier(document_id)
        row = next((row for row in self.store.manifest["documents"] if row["id"] == document_id), None)
        if row is None:
            raise ProjectError("missing_document", "Project document is missing: " + document_id)
        return row

    def document_handle(self, document_id: str) -> Path:
        with self._operation():
            row = self._record(document_id)
            return project_document_handle(self.root, document_id, row["kind"])

    def _identity_for_path(self, path: Path | str, *, expected_kind: str | None = None) -> str | None:
        target = Path(path).expanduser().absolute()
        reject_links(target)
        for row in self.store.manifest["documents"]:
            handles = {
                project_document_handle(self.root, row["id"], row["kind"]),
                self.root / row["path"],
            }
            if target not in handles:
                continue
            if expected_kind is not None and row["kind"] != expected_kind:
                raise ProjectError("dependency_conflict", "Managed document has the wrong resource kind")
            return row["id"]
        return None

    def is_managed_path(self, path: Path | str) -> bool:
        with self._operation():
            return self._identity_for_path(path) is not None

    def list_documents(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._operation():
            rows = []
            for row in self.store.manifest["documents"]:
                if kind is not None and row["kind"] != kind:
                    continue
                if row["kind"] in {"raytrace_request", "appearance_configuration", "binary_asset"}:
                    # Picker and metadata refresh reads only the small managed
                    # JSON envelope. Large binary validation belongs to open.
                    data = self.store._read_record(row)
                else:
                    graph = self.store.resolve_document(row["id"])
                    data = next(item["data"] for item in graph["documents"] if item["id"] == row["id"])
                from .resource_information import resource_display_name
                name = resource_display_name(row["kind"], data)
                rows.append({"id": row["id"], "kind": row["kind"], "name": name,
                             "revision": row["sha256"], "path": str(self.document_handle(row["id"]))})
            return rows

    def resource_information(self, document_id: str) -> dict:
        from .resource_information import resource_display_name, resource_information
        with self._operation():
            row = self._record(document_id)
            data = self.store._read_record(row)
            return resource_information(row["kind"], data, token=row["sha256"],
                                        fallback_name=resource_display_name(row["kind"], data))

    def save_resource_information(self, document_id: str, information: Mapping[str, Any], *,
                                  expected_revision: str | None = None) -> dict:
        from .resource_information import with_resource_information
        with self._operation(mutate=True):
            row = self._record(document_id)
            data = self.store._read_record(row)
            value = with_resource_information(row["kind"], data, information)
            self.save_resource(document_id, value, expected_revision=expected_revision or row["sha256"])
            return self.resource_information(document_id)

    def resolve_default(self) -> dict | None:
        with self._operation():
            return self.store.resolve_default()

    def resolve_document(self, document_id: str) -> dict:
        with self._operation():
            return self.store.resolve_document(document_id)

    def load_document(self, document_id: str) -> Document:
        with self._operation():
            row = self._record(document_id)
            graph = self.store.resolve_document(document_id)
            data = next(item["data"] for item in graph["documents"] if item["id"] == document_id)
            runtime, issues = self._managed_runtime(row["kind"], data)
            self._loaded_revisions[document_id] = row["sha256"]
            return Document(row["kind"], str(self.document_handle(document_id)), deepcopy(data), issues, runtime)

    def import_resource(self, path: Path | str, expected_kind: str | None = None) -> str:
        target = Path(path).expanduser().absolute()
        reject_links(target)
        kind = design_kind_from_path(target)
        if kind not in RESOURCE_KINDS or (expected_kind is not None and kind != expected_kind):
            raise ProjectError("dependency_conflict", "External resource suffix does not match the requested kind")
        with self._operation(mutate=True):
            return self.store.import_document(target)

    def capture_simulation_asset(self, role: str, path: Path | str, *, expected_sha256: str | None = None,
                                 cancel_event=None) -> dict:
        from .project_simulation_resources import capture_simulation_asset
        return capture_simulation_asset(self, role, path, expected_sha256=expected_sha256,
                                        cancel_event=cancel_event)

    def save_simulation_resource(self, kind: str, payload: Mapping[str, Any], *,
                                 document_id: str | None = None,
                                 expected_revision: str | None = None, name: str | None = None,
                                 asset_sources=None, description: str | None = None,
                                 notes: str | None = None) -> Document:
        from .project_simulation_resources import save_simulation_resource
        return save_simulation_resource(self, kind, payload, document_id=document_id,
            expected_revision=expected_revision, name=name, asset_sources=asset_sources,
            description=description, notes=notes)

    def create_resource(self, kind: str, data: Mapping[str, Any]) -> str:
        if kind not in RESOURCE_KINDS:
            raise ProjectError("invalid_document", "Unsupported project resource kind")
        with self._operation(mutate=True):
            return self.store.create_document(kind, data)

    def save_resource(self, document_id: str, data: Mapping[str, Any],
                      expected_revision: str | None = None) -> None:
        with self._operation(mutate=True):
            expected = expected_revision or self._loaded_revisions.get(document_id)
            self.store.save_documents({document_id: data}, expected_revisions=
                                      None if expected is None else {document_id: expected})
            self._loaded_revisions[document_id] = self._record(document_id)["sha256"]

    def save_configuration(self, name: str, process_id: str, parameters=None, *, make_default=False,
                           description: str | None = None, notes: str | None = None) -> str:
        with self._operation(mutate=True):
            document = {"kind": "coat_sim.deposition_configuration", "version": 1, "name": name,
                        "process_document_id": process_id, "parameters": {} if parameters is None else parameters}
            if description is not None:
                document["description"] = description
            if notes is not None:
                document["notes"] = notes
            identity = self.store.create_document("deposition_configuration", document,
                                                  make_default=make_default)
            return identity

    def save_process_configuration(self, name: str, process_data: Mapping[str, Any], parameters=None,
                                   *, process_id: str | None = None, make_default=True,
                                   description: str | None = None, notes: str | None = None) -> tuple[str, str]:
        with self._operation(mutate=True):
            expected = self._loaded_revisions.get(process_id) if process_id else None
            return self.store.save_deposition_configuration(name, process_data, parameters,
                process_document_id=process_id, expected_revision=expected, make_default=make_default,
                description=description, notes=notes)

    def select_default(self, configuration_id: str | None) -> None:
        with self._operation(mutate=True):
            self.store.set_default_configuration(configuration_id)

    def load(self, kind: str, path) -> Document:
        if isinstance(path, Mapping) and path.get("kind") == "coat_sim.input_snapshot":
            if kind != "process":
                raise ProjectError("dependency_conflict", "Only a process can load an input snapshot")
            runtime = runtime_from_snapshot(path)
            return Document(kind, str(path["process_path"]), runtime["profile"], (), runtime)
        with self._operation():
            identity = self._identity_for_path(path, expected_kind=kind)
        if identity is None:
            identity = self.import_resource(path, expected_kind=kind)
        return self.load_document(identity)

    def save(self, kind: str, path, data: Mapping[str, Any], *, overwrite=False) -> Document:
        with self._operation(mutate=True):
            identity = self._identity_for_path(path, expected_kind=kind)
            if identity is not None:
                if not overwrite:
                    raise FileExistsError("Managed resource exists; explicit update is required")
                self.save_resource(identity, data)
            else:
                # Save As creates a managed copy, never overwrites an external path.
                identity = self.create_resource(kind, data)
            return self.load_document(identity)

    def _reference_identity(self, reference: str | Path, expected: str | None = None) -> str:
        value = str(reference)
        if PurePosixPath(value).name != value or "\\" in value:
            raise ProjectError("unsafe_path", "Resource dependency must be an explicit managed UUID filename")
        row = next((row for row in self.store.manifest["documents"]
                    if value == row["id"] + EXTENSIONS[row["kind"]]), None)
        if row is None or (expected is not None and row["kind"] != expected):
            raise ProjectError("missing_dependency", "Typed project resource dependency is missing: " + value)
        return row["id"]

    def relative_reference(self, owner_path, target_path) -> str:
        # Flat references are identity-based and also work for a not-yet-saved owner.
        with self._operation():
            identity = self._identity_for_path(target_path)
        if identity is None:
            identity = self.import_resource(target_path)
        return self.document_handle(identity).name

    def resolve_reference(self, owner_path, reference) -> str:
        with self._operation():
            return str(self.document_handle(self._reference_identity(reference)))

    def _managed_runtime(self, kind: str, data: Mapping[str, Any]) -> tuple[dict | None, tuple[str, ...]]:
        if kind in {"raytrace_request", "appearance_configuration"}:
            from .project_simulation_resources import native_simulation_payload
            return {"payload": native_simulation_payload(self.store, kind, data)}, ()
        if kind == "machine":
            runtime = deepcopy(dict(data))
            for collection, expected in (("sources", "source"), ("masks", "mask")):
                for entry in runtime[collection]:
                    identity = self._reference_identity(entry["file"], expected)
                    entry["profile"] = self.load_document(identity).data
                    if expected == "mask":
                        entry["profile"] = validate_mask_profile(entry["profile"],
                                                                 substrate_z_mm=runtime["substrate_z_mm"])
            return runtime, ()
        if kind == "process":
            snapshot = self._snapshot(data)
            runtime = runtime_from_snapshot(snapshot)
            runtime["machine_path"] = str(self.document_handle(self._reference_identity(data["machine_file"], "machine")))
            runtime["substrate_path"] = str(self.document_handle(self._reference_identity(data["substrate_file"], "substrate")))
            return runtime, ()
        return None, ()

    def _snapshot(self, process: Mapping[str, Any]) -> dict:
        process = normalize_document("process", process)
        records: dict[str, dict] = {}
        def capture(identity: str, expected: str):
            row = self._record(identity)
            if row["kind"] != expected:
                raise ProjectError("dependency_conflict", "Snapshot dependency kind mismatch")
            name = identity + EXTENSIONS[expected]
            if name in records:
                return
            graph = self.store.resolve_document(identity)
            data = next(item["data"] for item in graph["documents"] if item["id"] == identity)
            records[name] = {"kind": expected, "path": "designs/" + name, "data": deepcopy(data),
                             "sha256": hashlib.sha256(canonical_json_bytes(data)).hexdigest()}
            for owner, field, child in file_references(expected, data):
                capture(self._reference_identity(owner[field], child), child)
        for owner, field, expected in file_references("process", process):
            capture(self._reference_identity(owner[field], expected), expected)
        records["draft.cproc"] = {"kind": "process", "path": "designs/draft.cproc", "data": process,
                                  "sha256": hashlib.sha256(canonical_json_bytes(process)).hexdigest()}
        return {"kind": "coat_sim.input_snapshot", "version": 1,
                "process_path": "designs/draft.cproc", "files": list(records.values())}

    def freeze_process(self, data, *, base_dir=None):
        with self._operation():
            snapshot = self._snapshot(data)
            snapshot["runtime"] = runtime_from_snapshot(snapshot)
            return snapshot

    def _dependencies(self, kind, data, path):
        return self._managed_runtime(kind, data)

    def validate(self, kind: str, data=None, *, path=None) -> ValidationResult:
        try:
            with self._operation():
                if data is None:
                    identity = self._identity_for_path(path, expected_kind=kind)
                    if identity is None:
                        raise ProjectError("missing_document", "Validation never imports an external resource")
                    document = self.load_document(identity)
                    return ValidationResult(kind, document.valid, document.data, document.issues)
                normalized = normalize_document(kind, data)
                self._managed_runtime(kind, normalized)
                return ValidationResult(kind, True, normalized, ())
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return ValidationResult(kind, False, None, (str(exc),))

    def material_library(self):
        from .project_material_library import ProjectMaterialLibrary
        return ProjectMaterialLibrary(self)

    def material_library_bridge(self, parent=None, purpose="film"):
        # Qt remains lazy: ordinary core/CLI imports do not need PySide6.
        from coat_sim.ui.project_material_library import ProjectMaterialLibraryBridge
        return ProjectMaterialLibraryBridge(self, parent=parent, purpose=purpose)
