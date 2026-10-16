"""Project-scoped deposition configuration and compute ownership.

The compute workspace owns operation fencing and task summaries for one open
project generation. Resource documents and the shared root-restricted
CoatService remain owned by ProjectResourceWorkspace; this layer never creates
a competing process/material draft or consults global design folders.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
from typing import cast

from coat_sim.core.project_default_loading import ProjectDefaultPlan, load_project_default
from coat_sim.core.project_dependencies import ProjectError, normalize_document, reject_links
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import (OperationBinding, ProjectBinding,
                                      ProjectSessionError, ProjectSessionHost)


class ProjectComputeError(ValueError):
    """A stable, UI-safe compute workspace error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ProjectComputeWorkspace:
    """Compute-scoped state bound to one host session generation.

    Resource persistence and the project-root ``CoatService`` are shared with
    ``ProjectResourceWorkspace``. This wrapper is separately registered so
    project-close coordination can freeze compute admission and inspect jobs.
    """

    RESOURCE_KEY = "compute_workspace"
    _TERMINAL = frozenset({"succeeded", "failed", "cancelled", "stale", "detached", "observer_failed"})

    def __init__(self, host: ProjectSessionHost, binding: ProjectBinding):
        from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace

        self.host = host
        self.binding = binding
        self.resource_workspace = ProjectResourceWorkspace.for_project(host, binding)
        self.service = self.resource_workspace.service
        self.store: ProjectStore = self.service.store
        self.root = Path(self.service.root).resolve()
        if self.root != Path(binding.root).resolve():
            raise ProjectComputeError("root_mismatch", "Compute service belongs to a different project root")
        if not getattr(self.service, "restrict_to_root", False):
            raise ProjectComputeError("unrestricted_service", "Project compute requires a root-restricted service")
        self._lock = threading.RLock()
        self._configuration_id: str | None = None
        self._process_document_id: str | None = None
        self._configuration_revisions: dict[str, str] = {}
        self._frozen = False
        self._jobs: dict[str, dict] = {}
        self._draft_provider = None
        self._draft_restorer = None
        self._pending_recovery_draft: dict[str, object] | None = None
        self._draft_baseline_signature: str | None = None
        self._recovered_job_references: list[dict] = []
        self._recovery_restored = False
        self._thaw_callbacks: list = []
        self._operation_configurations: dict[OperationBinding, str | None] = {}
        self._appearance_context_json: bytes | None = None
        from coat_sim.core.project_optical_drafts import ProjectOpticalDrafts
        self._input_drafts = ProjectOpticalDrafts(self)
        host.claim_resource(binding, self.RESOURCE_KEY, self)

    @classmethod
    def for_project(cls, host: ProjectSessionHost,
                    binding: ProjectBinding) -> "ProjectComputeWorkspace":
        with host.operation_scope(binding):
            existing = host.resource_for(binding, cls.RESOURCE_KEY)
            if existing is not None:
                if not isinstance(existing, cls):
                    raise ProjectComputeError("resource_conflict", "The compute workspace resource has another owner")
                return existing
            return cls(host, binding)

    @property
    def configuration_id(self) -> str | None:
        with self._lock:
            return self._configuration_id

    @property
    def process_document_id(self) -> str | None:
        with self._lock:
            return self._process_document_id

    @property
    def editing_frozen(self) -> bool:
        with self._lock:
            return self._frozen

    @property
    def recovery_restored(self) -> bool:
        with self._lock:
            return self._recovery_restored

    @property
    def has_pending_draft(self) -> bool:
        with self._lock:
            return self._pending_recovery_draft is not None or self._input_drafts.pending

    @property
    def has_pending_deposition_draft(self) -> bool:
        with self._lock:
            return self._pending_recovery_draft is not None

    @property
    def has_dirty_draft(self) -> bool:
        """Whether any registered project compute-input module is unsaved."""
        return self.has_dirty_deposition_draft or self._input_drafts.dirty

    @property
    def has_dirty_deposition_draft(self) -> bool:
        """Whether editable deposition state differs from its loaded/saved file."""
        with self._lock:
            baseline = self._draft_baseline_signature
            has_draft = self._draft_provider is not None or self._pending_recovery_draft is not None
        return bool(has_draft and (baseline is None or self._deposition_draft_signature() != baseline))

    def mark_draft_saved(self) -> None:
        """Record a baseline only after successfully loading/saving settings."""
        signature = self._deposition_draft_signature()
        with self._lock:
            self._draft_baseline_signature = signature

    def select_context(self, *, configuration_id: str | None,
                       process_document_id: str | None) -> None:
        """Update the compute selection without editing project documents."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
                self._configuration_id = configuration_id
                self._process_document_id = process_document_id

    def load_default(self) -> ProjectDefaultPlan:
        """Load only the manifest-selected root, preserving explicit empty states."""
        with self.host.operation_scope(self.binding):
            plan = load_project_default(self.store)
            if plan.project_id != self.binding.project_id:
                raise ProjectComputeError("root_mismatch", "Project default belongs to another project")
            with self._lock:
                self._ensure_admission()
                self._configuration_id = plan.configuration_id
                self._process_document_id = plan.process_document_id
                if plan.configuration_id is not None:
                    record = next(row for row in self.store.manifest["documents"]
                                  if row["id"] == plan.configuration_id)
                    self._configuration_revisions[plan.configuration_id] = record["sha256"]
            return plan

    def configurations(self) -> list[dict]:
        """List only managed deposition configuration documents."""
        with self.host.operation_scope(self.binding):
            rows = []
            manifest = self.store.manifest
            for record in manifest["documents"]:
                if record["kind"] != "deposition_configuration":
                    continue
                graph = self.store.resolve_document(record["id"])
                config = graph.get("configuration")
                if not isinstance(config, dict):
                    raise ProjectError("invalid_dependency_graph", "Configuration document did not resolve")
                rows.append({
                    "id": record["id"], "kind": record["kind"],
                    "name": config["name"],
                    "process_document_id": config["process_document_id"],
                    "parameters": deepcopy(config["parameters"]),
                    "path": record["path"],
                    "default": record["id"] == manifest["default_configuration_id"],
                    "project_revision": graph["revision"],
                })
            return rows

    def load_configuration(self, configuration_id: str) -> dict:
        """Resolve a managed configuration and its validated process snapshot."""
        with self.host.operation_scope(self.binding):
            graph = self.store.resolve_document(configuration_id)
            if graph.get("project_id") != self.binding.project_id:
                raise ProjectComputeError("root_mismatch", "Configuration belongs to another project")
            config = graph.get("configuration")
            if not isinstance(config, dict):
                raise ProjectError("dependency_conflict", "Document is not a deposition configuration")
            process_id = config["process_document_id"]
            process_graph = self.store.resolve_document(process_id)
            if process_graph.get("project_id") != self.binding.project_id:
                raise ProjectComputeError("root_mismatch", "Process belongs to another project")
            if not isinstance(process_graph.get("input_snapshot"), dict):
                raise ProjectError("invalid_dependency_graph", "Configuration process has no input snapshot")
            if process_graph.get("revision") != graph.get("revision"):
                raise ProjectError("stale_project", "Configuration and process snapshots were resolved at different revisions")
            result = {"project_id": self.binding.project_id,
                      "project_revision": graph["revision"],
                      "configuration_id": configuration_id,
                      "configuration": deepcopy(config),
                      "process_document_id": process_id,
                      "process_graph": deepcopy(process_graph),
                      "input_snapshot": deepcopy(process_graph["input_snapshot"])}
            with self._lock:
                self._ensure_admission()
                self._configuration_id = configuration_id
                self._process_document_id = process_id
                root_record = next(row for row in graph["documents"]
                                   if row["id"] == configuration_id)
                self._configuration_revisions[configuration_id] = root_record["sha256"]
            return result

    def resolve_process(self, process_document_id: str) -> dict:
        """Resolve a managed process by ID; never follows standalone paths."""
        with self.host.operation_scope(self.binding):
            graph = self.store.resolve_document(process_document_id)
            if graph.get("project_id") != self.binding.project_id:
                raise ProjectComputeError("root_mismatch", "Process belongs to another project")
            process = next((row for row in graph["documents"]
                            if row["id"] == process_document_id), None)
            if process is None or process["kind"] != "process":
                raise ProjectError("dependency_conflict", "Selected document is not a managed process")
            snapshot = graph.get("input_snapshot")
            if not isinstance(snapshot, dict):
                raise ProjectError("invalid_dependency_graph", "Managed process has no input snapshot")
            with self._lock:
                self._ensure_admission()
                self._process_document_id = process_document_id
            return {"project_id": graph["project_id"], "project_revision": graph["revision"],
                    "process_document_id": process_document_id,
                    "process": deepcopy(process["data"]),
                    "input_snapshot": deepcopy(snapshot),
                    "runtime": deepcopy(graph["runtime"]),
                    "graph": deepcopy(graph)}

    def save_configuration(self, process_document_id: str, parameters: Mapping,
                           name: str = "沉积配置", configuration_id: str | None = None,
                           *, make_default: bool = False,
                           description: str | None = None, notes: str | None = None) -> str:
        """Save a managed cdep and its next-session opening root atomically."""
        if not isinstance(parameters, Mapping):
            raise ProjectError("invalid_document", "Configuration parameters must be an object")
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
            self.resolve_process(process_document_id)
            metadata = {key: value for key, value in
                        (("description", description), ("notes", notes)) if value is not None}
            if configuration_id is not None:
                prior = self.store.resolve_document(configuration_id).get("configuration") or {}
                for key in ("description", "notes"):
                    if key not in metadata and key in prior:
                        metadata[key] = prior[key]
            document = normalize_document("deposition_configuration", {
                "kind": "coat_sim.deposition_configuration", "version": 1,
                "name": name, "process_document_id": process_document_id,
                "parameters": dict(parameters), **metadata,
            })
            if configuration_id is None:
                # The shared resource service owns the resource boundary. Prefer
                # its explicit API; retain a direct-store fallback for older
                # compatible ProjectStore adapters.
                save = getattr(self.service, "save_configuration", None)
                if callable(save):
                    result = save(document["name"], process_document_id,
                                  document["parameters"], make_default=make_default, **metadata)
                    identity = result.get("configuration_id", result.get("id")) if isinstance(result, Mapping) else result
                else:
                    identity = self.store.add_deposition_configuration(
                        document["name"], process_document_id, document["parameters"], **metadata)
            else:
                # Managed config edits preserve their stable ID and compare the
                # revision captured at Load, never silently replacing a newer
                # editor's configuration.
                revision = self._configuration_revisions.get(configuration_id)
                if revision is None:
                    raise ProjectError("stale_document", "Load the project configuration before saving it")
                self.service.save_resource(configuration_id, document,
                                           expected_revision=revision)
                identity = configuration_id
            if not isinstance(identity, str) or not identity:
                raise ProjectError("invalid_document", "Configuration save did not return a managed ID")
            with self._lock:
                self._ensure_admission()
                self._configuration_id = identity
                self._process_document_id = process_document_id
                record = next(row for row in self.store.manifest["documents"]
                              if row["id"] == identity)
                self._configuration_revisions[identity] = record["sha256"]
            return identity

    def import_process(self, path: str | os.PathLike[str]) -> str:
        """Copy a selected external process and its complete dependency graph."""
        with self.host.operation_scope(self.binding):
            importer = getattr(self.service, "import_resource", None)
            if callable(importer):
                result = importer(path, expected_kind="process")
                identity = result.get("document_id", result.get("id")) if isinstance(result, Mapping) else result
            else:
                identity = self.store.import_document(path)
            if not isinstance(identity, str) or not identity:
                raise ProjectError("invalid_document", "Process import did not return a managed ID")
            resolved = self.resolve_process(identity)
            if resolved["process_document_id"] != identity:
                raise ProjectError("dependency_conflict", "Imported process did not resolve as managed input")
            with self._lock:
                self._ensure_admission()
                self._process_document_id = identity
                self._configuration_id = None
            return identity

    def process_handle(self, process_document_id: str) -> Path:
        """Return the resource service's stable logical handle, never raw input."""
        with self.host.operation_scope(self.binding):
            self.resolve_process(process_document_id)
            handle = getattr(self.service, "document_handle", None)
            if callable(handle):
                result = Path(handle(process_document_id))
            else:
                row = next(row for row in self.store.manifest["documents"]
                           if row["id"] == process_document_id)
                result = self.root / PurePosixPath(row["path"])
            return self.owned_path(result, allow_missing=True)

    def capture(self, operation_id: str) -> OperationBinding:
        """Fence a calculation, rejecting closed generations and close freezes."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
                operation = self.host.capture_operation(self.binding.project_id, operation_id)
                self._operation_configurations[operation] = self._configuration_id
                return operation

    def accepts(self, operation: OperationBinding) -> bool:
        """Return whether a callback still belongs to the open, unfrozen generation."""
        if not isinstance(operation, OperationBinding) or operation.session != self.binding:
            return False
        try:
            self.host.resolve_operation(operation)
        except (ValueError, RuntimeError):
            return False
        with self._lock:
            if self._frozen:
                return False
        return True

    def metadata(self, operation: OperationBinding, extra: Mapping | None = None) -> dict:
        """Attach immutable project provenance to an admitted queue operation."""
        if not isinstance(operation, OperationBinding) or operation.session != self.binding:
            raise ProjectComputeError("stale_operation", "Operation belongs to another project generation")
        if not self.accepts(operation):
            raise ProjectComputeError("stale_operation", "Project operation is no longer current")
        with self._lock:
            configuration_id = self._operation_configurations.get(
                operation, self._configuration_id)
        provenance = {"project_id": self.binding.project_id,
                      "project_generation": self.binding.generation,
                      "project_root": str(self.root),
                      "configuration_id": configuration_id,
                      "operation_id": operation.operation_id}
        base = {}
        if extra is not None:
            if not isinstance(extra, Mapping):
                raise TypeError("extra metadata must be an object")
            base.update(deepcopy(dict(extra)))
        origin = base.get("origin", {})
        if not isinstance(origin, Mapping):
            raise ValueError("origin metadata must be an object")
        reserved = set(provenance).intersection(origin)
        if reserved:
            raise ValueError("origin cannot override project provenance: " + ", ".join(sorted(reserved)))
        base["origin"] = {**deepcopy(dict(origin)), **provenance}
        return base

    def owned_path(self, path: str | os.PathLike[str], *, allow_missing: bool = False) -> Path:
        """Canonicalize a project-owned path while rejecting traversal and links."""
        try:
            with self.host.operation_scope(self.binding):
                return self._owned_path(path, allow_missing=allow_missing)
        except ProjectSessionError as exc:
            raise ProjectComputeError("stale_project", "Project path belongs to a closed session") from exc

    def _owned_path(self, path: str | os.PathLike[str], *, allow_missing: bool) -> Path:
        raw = Path(path).expanduser()
        if any(part == ".." for part in raw.parts):
            raise ProjectComputeError("unsafe_path", "Project paths cannot contain parent traversal")
        candidate = raw if raw.is_absolute() else self.root / raw
        candidate = Path(os.path.abspath(candidate))
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise ProjectComputeError("path_outside_root", "Path is outside the bound project")
        try:
            reject_links(candidate)
        except ProjectError as exc:
            raise ProjectComputeError(exc.code, str(exc)) from exc
        if not allow_missing and not candidate.exists():
            raise ProjectComputeError("missing_path", "Project-owned path does not exist")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ProjectComputeError("path_outside_root", "Resolved path is outside the bound project")
        if candidate.exists() and not (candidate.is_file() or candidate.is_dir()):
            raise ProjectComputeError("unsafe_path", "Path is not a regular file or directory")
        return resolved

    def destination(self, path: str | os.PathLike[str]) -> Path:
        """Validate a future file destination without permitting outside-root output."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
            target = self.owned_path(path, allow_missing=True)
            if target.exists() and target.is_dir():
                raise ProjectComputeError("invalid_destination", "A file destination cannot be a directory")
            return target

    def import_asset(self, path: str | os.PathLike[str], category: str = "inputs") -> Path:
        """Copy an explicitly selected file into content-addressed project data."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
        if not isinstance(category, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", category):
            raise ProjectComputeError("invalid_category", "Import category must be a short lowercase name")
        source = Path(os.path.abspath(Path(path).expanduser()))
        try:
            reject_links(source)
        except ProjectError as exc:
            raise ProjectComputeError(exc.code, str(exc)) from exc
        try:
            info = source.lstat()
        except OSError as exc:
            raise ProjectComputeError("missing_source", "Selected import file is unavailable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ProjectComputeError("invalid_source", "Only regular files can be imported")
        if source.is_relative_to(self.root):
            return self.owned_path(source, allow_missing=False)
        if info.st_size > 512 * 1024 * 1024:
            raise ProjectComputeError("capacity_exceeded", "Imported file exceeds the 512 MiB safety limit")
        digest = sha256()
        with source.open("rb") as stream:
            content = stream.read(512 * 1024 * 1024 + 1)
        if len(content) > 512 * 1024 * 1024:
            raise ProjectComputeError("capacity_exceeded", "Imported file exceeds the 512 MiB safety limit")
        digest.update(content)
        hex_digest = digest.hexdigest()
        name = source.name.replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(name).suffix.lower()
        stem = Path(name).stem
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-._")[:80] or "asset"
        safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ".bin"
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
            folder = self.owned_path(self.root / "data" / "imported" / category, allow_missing=True)
            folder.mkdir(parents=True, exist_ok=True)
            target = self.destination(folder / f"{hex_digest[:20]}-{safe_stem}{safe_suffix}")
            if target.exists():
                if target.is_file() and sha256(target.read_bytes()).hexdigest() == hex_digest:
                    return target
                raise ProjectComputeError("destination_conflict", "Content-addressed import target has different content")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(target, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            return target

    def freeze_editing(self, frozen: bool) -> None:
        if type(frozen) is not bool:
            raise TypeError("frozen must be a bool")
        with self._lock:
            thawed = self._frozen and not frozen
            self._frozen = frozen
            callbacks = tuple(self._thaw_callbacks) if thawed else ()
        for callback in callbacks:
            callback()

    def register_thaw_callback(self, callback) -> None:
        if not callable(callback):
            raise TypeError("thaw callback must be callable")
        with self._lock:
            if callback not in self._thaw_callbacks:
                self._thaw_callbacks.append(callback)

    def unregister_thaw_callback(self, callback) -> bool:
        with self._lock:
            if callback not in self._thaw_callbacks:
                return False
            self._thaw_callbacks.remove(callback)
            return True

    def retire_operation(self, operation: OperationBinding) -> None:
        with self._lock:
            self._operation_configurations.pop(operation, None)

    def register_draft_provider(self, provider, *, restore=None) -> None:
        if provider is not None and not callable(provider):
            raise TypeError("draft provider must be callable")
        if restore is not None and not callable(restore):
            raise TypeError("draft restore callback must be callable")
        with self._lock:
            if (provider is not None and self._draft_provider is not None
                    and self._draft_provider != provider):
                raise ProjectComputeError("draft_owner_exists", "The project already has a deposition draft owner")
            self._draft_provider = provider
            self._draft_restorer = restore
            pending = deepcopy(self._pending_recovery_draft)
        if pending is not None and restore is not None:
            restore(pending)
            with self._lock:
                self._pending_recovery_draft = None

    def unregister_draft_provider(self, provider) -> bool:
        with self._lock:
            if self._draft_provider != provider:
                return False
        draft = deepcopy(provider())
        with self._lock:
            if self._draft_provider != provider:
                return False
            # Closing a presentation window is not approval to discard its
            # editable project configuration; the next window restores it.
            self._pending_recovery_draft = draft
            self._draft_provider = None
            self._draft_restorer = None
            return True

    def _deposition_draft_signature(self) -> str:
        """Hash deterministic selected-configuration/editable-parameter state."""
        with self._lock:
            provider = self._draft_provider
            pending = deepcopy(self._pending_recovery_draft)
            selection = {"configuration_id": self._configuration_id,
                         "process_document_id": self._process_document_id}
        details: dict[str, object] = provider() if provider is not None else (pending or {})
        payload = {**selection, "parameters": details}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return sha256(encoded).hexdigest()

    def draft_signature(self) -> str:
        """Fence close/recovery review against changes in every input module."""
        payload = [self._deposition_draft_signature(), self._input_drafts.signature()]
        return sha256(json.dumps(payload).encode("utf-8")).hexdigest()

    def register_input_draft_provider(self, module_key, provider) -> None:
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
            self._input_drafts.register(module_key, provider)

    def unregister_input_draft_provider(self, module_key, provider) -> bool:
        return self._input_drafts.unregister(module_key, provider)

    def begin_input_draft_save(self, module_key) -> str:
        """Start an optical save; callers must await its matching completion."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
            return self._input_drafts.begin_save(module_key)

    def complete_input_draft_save(self, module_key, provider, request_id,
                                  success, error="") -> bool:
        return self._input_drafts.complete_save(module_key, provider, request_id, success, error)

    def input_draft_save_status(self, module_key, request_id) -> dict:
        return self._input_drafts.save_status(module_key, request_id)

    def complete_input_draft_restore(self, module_key, provider, request_id,
                                     success, error="") -> bool:
        return self._input_drafts.complete_restore(module_key, provider, request_id, success, error)

    def input_draft_restore_is_current(self, module_key, provider, request_id) -> bool:
        return self._input_drafts.restore_is_current(module_key, provider, request_id)

    def input_draft_restore_status(self, token=None) -> dict:
        return self._input_drafts.restore_status(token)

    @property
    def input_draft_operations_pending(self) -> bool:
        return self._input_drafts.operations_pending

    def close_preflight(self) -> tuple[dict, ...]:
        """Refuse unresolved input operations and report module-owned drafts."""
        return self._input_drafts.preflight()

    def set_appearance_context(self, context: Mapping | None) -> None:
        """Cache detached project-owned ray-trace context for appearance routing."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
                if context is None:
                    self._appearance_context_json = None
                    return
                if not isinstance(context, Mapping):
                    raise TypeError("appearance context must be an object or None")
                try:
                    encoded = json.dumps(dict(context), ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")
                except (TypeError, ValueError) as exc:
                    raise ProjectComputeError("invalid_appearance_context", "Appearance context must be finite JSON") from exc
                self._appearance_context_json = encoded

    def appearance_context(self) -> dict | None:
        """Return detached cached context only while this exact project is open."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
                return (None if self._appearance_context_json is None
                        else json.loads(self._appearance_context_json))

    def snapshot(self) -> dict:
        """Capture explicit, non-executable recovery state for user review."""
        with self.host.operation_scope(self.binding):
            with self._lock:
                provider = self._draft_provider
                pending = deepcopy(self._pending_recovery_draft)
                appearance = (None if self._appearance_context_json is None
                              else json.loads(self._appearance_context_json))
                selection = {"configuration_id": self._configuration_id,
                             "process_document_id": self._process_document_id}
                job_references = [{
                    "job_id": row["job_id"], "module_key": row["module_key"],
                    "state": row["state"], "service_owned": row["service_owned"],
                    "metadata": deepcopy(row["metadata"]),
                } for row in self._jobs.values()]
                job_references.extend(deepcopy(self._recovered_job_references))
            draft: dict[str, object] = provider() if provider is not None else (pending or {})
            payload = {
                "kind": "coat_sim.project_compute_recovery", "version": 1,
                "project_id": self.binding.project_id,
                "project_generation": self.binding.generation,
                "project_root": str(self.root),
                **selection,
                "draft": deepcopy(draft),
                "appearance_context": appearance,
                "job_references": job_references,
            }
            optical_inputs = self._input_drafts.snapshot()
            if optical_inputs:
                payload["optical_inputs"] = optical_inputs
            # Enforce the same finite JSON boundary as persisted project data.
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8")
            return json.loads(encoded)

    def _validated_recovery(self, snapshot: Mapping) -> dict[str, object]:
        """Validate recovery data and current project membership without mutation."""
        if not isinstance(snapshot, Mapping):
            raise ProjectComputeError("invalid_recovery", "Recovery snapshot must be an object")
        try:
            normalized = json.loads(json.dumps(dict(snapshot), ensure_ascii=False,
                                               sort_keys=True, separators=(",", ":"),
                                               allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ProjectComputeError("invalid_recovery", "Recovery snapshot must be finite JSON") from exc
        required = {"kind", "version", "project_id", "project_generation", "project_root",
                    "configuration_id", "process_document_id", "draft",
                    "appearance_context", "job_references"}
        if (not required.issubset(normalized) or set(normalized) - required - {"optical_inputs"}
                or normalized.get("kind") != "coat_sim.project_compute_recovery"
                or normalized.get("version") != 1):
            raise ProjectComputeError("invalid_recovery", "Recovery snapshot shape or version is invalid")
        if (normalized["project_id"] != self.binding.project_id
                or not isinstance(normalized["project_root"], str)
                or Path(normalized["project_root"]).resolve() != self.root):
            raise ProjectComputeError("wrong_project", "Recovery snapshot belongs to another project")
        if (not isinstance(normalized["project_generation"], str)
                or not isinstance(normalized["draft"], Mapping)
                or not isinstance(normalized["job_references"], list)):
            raise ProjectComputeError("invalid_recovery", "Recovery draft or job references are invalid")
        config_id = normalized["configuration_id"]
        process_id = normalized["process_document_id"]
        if ((config_id is not None and not isinstance(config_id, str))
                or (process_id is not None and not isinstance(process_id, str))):
            raise ProjectComputeError("invalid_recovery", "Recovery resource IDs must be strings or null")
        self._validate_recovery_draft(normalized["draft"])
        if "optical_inputs" in normalized:
            self._input_drafts.validate(normalized["optical_inputs"])
        information = normalized["draft"].get("pending_information")
        if information is not None and information["document_id"] != config_id:
            raise ProjectComputeError("invalid_recovery", "Recovery information belongs to another configuration")
        # Validate that any restored selection still belongs to the current
        # manifest. The old generation is provenance only and never a write lease.
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
            manifest = self.store.manifest
            by_id = {row["id"]: row for row in manifest["documents"]}
            if config_id is not None:
                if (config_id not in by_id or by_id[config_id]["kind"] != "deposition_configuration"):
                    raise ProjectComputeError("stale_recovery", "Saved project configuration is no longer present")
                config_graph = self.store.resolve_document(config_id)
                config = config_graph.get("configuration") or {}
                if process_id != config.get("process_document_id"):
                    raise ProjectComputeError("stale_recovery", "Recovery process no longer matches its configuration")
            if process_id is not None:
                if process_id not in by_id or by_id[process_id]["kind"] != "process":
                    raise ProjectComputeError("stale_recovery", "Saved project process is no longer present")
                self.store.resolve_document(process_id)
            appearance = normalized["appearance_context"]
            if appearance is not None and not isinstance(appearance, Mapping):
                raise ProjectComputeError("invalid_recovery", "Appearance context must be an object or null")
            if appearance is not None:
                self._validate_recovery_appearance(appearance)
            appearance_json = (None if appearance is None else
                               json.dumps(dict(appearance), ensure_ascii=False,
                                          sort_keys=True, separators=(",", ":"),
                                          allow_nan=False).encode("utf-8"))
            references = []
            for row in normalized["job_references"]:
                if (not isinstance(row, Mapping) or not isinstance(row.get("job_id"), str)
                        or not isinstance(row.get("module_key"), str)
                        or not isinstance(row.get("state"), str)):
                    raise ProjectComputeError("invalid_recovery", "Job reference is malformed")
                references.append(deepcopy(dict(row)))
            return {"snapshot": normalized, "configuration_id": config_id,
                    "process_document_id": process_id,
                    "appearance_json": appearance_json,
                    "job_references": references,
                    "configuration_sha256": (None if config_id is None
                                               else by_id[config_id]["sha256"])}

    @staticmethod
    def _validate_recovery_draft(draft: Mapping) -> None:
        """Reject malformed values that the Qt restorer could not apply safely."""
        allowed = {"pending_information", "configuration_name", "configuration_description", "configuration_notes", "fields", "device", "algorithm", "algorithm_budget_s", "fast_cycle",
                   "fast_cycle_residual_percent", "requested_time_step_s", "time_basis"}
        if set(draft) - allowed:
            raise ProjectComputeError("invalid_recovery", "Recovery draft contains unsupported fields")
        name = draft.get("configuration_name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 100):
            raise ProjectComputeError("invalid_recovery", "Recovery configuration name is invalid")
        for key in ("configuration_description", "configuration_notes"):
            value = draft.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 20000):
                raise ProjectComputeError("invalid_recovery", "Recovery configuration metadata is invalid")
        information = draft.get("pending_information")
        if information is not None:
            if (not isinstance(information, dict) or set(information) != {
                    "name", "summary", "notes", "document_id", "token", "model_dirty"}
                    or type(information["model_dirty"]) is not bool
                    or (information["document_id"] is not None and not isinstance(information["document_id"], str))
                    or any(not isinstance(information[key], str) or len(information[key]) > limit
                           for key, limit in (("name", 200), ("summary", 20000), ("notes", 20000)))):
                raise ProjectComputeError("invalid_recovery", "Recovery unconfirmed information is invalid")
            metadata = {"name": draft.get("configuration_name", "沉积配置"),
                        "summary": draft.get("configuration_description", ""),
                        "notes": draft.get("configuration_notes", "")}
            token = sha256(json.dumps([information["document_id"] or "", metadata],
                ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if information["token"] != token:
                raise ProjectComputeError("invalid_recovery", "Recovery unconfirmed information token is stale")
        fields = draft.get("fields", {})
        if not isinstance(fields, Mapping):
            raise ProjectComputeError("invalid_recovery", "Recovery fields must be an object")
        visible = {"carrier_rpm", "carrier_phase_deg", "planet_phase_deg",
                   "time_steps", "grid_size", "time_step_s"}
        if (set(fields) - visible
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in fields.items())):
            raise ProjectComputeError("invalid_recovery", "Recovery field values must be editable strings")
        device = draft.get("device")
        if device is not None and (not isinstance(device, str)
                                   or device not in {"CPU", "GPU", "DirectML"}):
            raise ProjectComputeError("invalid_recovery", "Recovery device is unsupported")
        algorithm = draft.get("algorithm")
        if algorithm is not None and (not isinstance(algorithm, str)
                                      or algorithm not in {"fast", "reference", "optimize"}):
            raise ProjectComputeError("invalid_recovery", "Recovery algorithm is unsupported")
        budget = draft.get("algorithm_budget_s")
        if budget is not None and (type(budget) is not int or not 0 <= budget <= 300):
            raise ProjectComputeError("invalid_recovery", "Recovery algorithm budget is invalid")
        fast_cycle = draft.get("fast_cycle")
        if fast_cycle is not None and type(fast_cycle) is not bool:
            raise ProjectComputeError("invalid_recovery", "Recovery fast-cycle choice is invalid")
        threshold = draft.get("fast_cycle_residual_percent")
        if threshold is not None:
            try:
                number = float(threshold)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProjectComputeError("invalid_recovery", "Recovery threshold is invalid") from exc
            if not math.isfinite(number) or not 0.0 <= number <= 100.0:
                raise ProjectComputeError("invalid_recovery", "Recovery threshold is invalid")
        for key in ("requested_time_step_s", "time_basis"):
            value = draft.get(key)
            if value is not None and not isinstance(value, str):
                raise ProjectComputeError("invalid_recovery", f"Recovery {key} must be text")
        time_basis = draft.get("time_basis")
        if time_basis is not None and (not isinstance(time_basis, str)
                                       or time_basis not in {"time_step_s", "time_steps"}):
            raise ProjectComputeError("invalid_recovery", "Recovery time basis is invalid")

    def _validate_recovery_appearance(self, context: Mapping) -> None:
        """Require a valid frozen appearance source owned by this exact project."""
        if (context.get("schema") != "coat_sim.appearance_launch_context"
                or context.get("version") != 1):
            raise ProjectComputeError("invalid_recovery", "Appearance context schema is unsupported")
        for key in ("asset_path", "asset_root", "asset_sha256", "process_profile_sha256"):
            if not isinstance(context.get(key), str) or not context[key]:
                raise ProjectComputeError("invalid_recovery", f"Appearance context is missing {key}")
        asset_digest = context["asset_sha256"]
        profile_digest = context["process_profile_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", asset_digest) or not re.fullmatch(r"[0-9a-f]{64}", profile_digest):
            raise ProjectComputeError("invalid_recovery", "Appearance context contains an invalid SHA-256 digest")
        try:
            asset_root = Path(context["asset_root"]).resolve(strict=True)
            asset_path = self.owned_path(context["asset_path"], allow_missing=False)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProjectComputeError("invalid_recovery", "Appearance source is not project-owned") from exc
        if asset_root != self.root or not asset_path.is_file():
            raise ProjectComputeError("invalid_recovery", "Appearance source is not project-owned")
        if not isinstance(context.get("process_profile"), Mapping) or not isinstance(context.get("substrate_profile"), Mapping):
            raise ProjectComputeError("invalid_recovery", "Appearance process or substrate profile is invalid")
        try:
            profile_json = json.dumps(context["process_profile"], ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":"),
                                      allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProjectComputeError("invalid_recovery", "Appearance process profile is invalid") from exc
        if sha256(profile_json).hexdigest() != profile_digest:
            raise ProjectComputeError("invalid_recovery", "Appearance process profile digest does not match")
        digest = sha256()
        with asset_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != asset_digest:
            raise ProjectComputeError("invalid_recovery", "Appearance asset digest does not match")

    def validate_recovery(self, snapshot: Mapping) -> None:
        """Validate recovery data without changing selection, drafts, or jobs."""
        self._validated_recovery(snapshot)

    def restore_recovery(self, snapshot: Mapping) -> dict:
        """Restore reviewed editable state, never restart or resubmit jobs."""
        # Keep project close from racing between validation and the staged
        # compute-state apply. The public validate method is also safe to call
        # separately before a coordinated multi-workspace recovery.
        with self.host.operation_scope(self.binding):
            validated = self._validated_recovery(snapshot)
            normalized = cast(dict[str, object], validated["snapshot"])
            config_id = cast(str | None, validated["configuration_id"])
            process_id = cast(str | None, validated["process_document_id"])
            appearance_json = cast(bytes | None, validated["appearance_json"])
            references = cast(list[dict], validated["job_references"])
            with self._lock:
                self._ensure_admission()
                prior = (self._configuration_id, self._process_document_id,
                         self._appearance_context_json, deepcopy(self._recovered_job_references),
                         self._recovery_restored, deepcopy(self._configuration_revisions),
                         deepcopy(self._pending_recovery_draft), self._draft_baseline_signature)
                restore, provider = self._draft_restorer, self._draft_provider
            previous_draft = deepcopy(provider()) if restore is not None and provider is not None else None
            with self._lock:
                self._ensure_admission()
                self._configuration_id = config_id
                self._process_document_id = process_id
                self._appearance_context_json = appearance_json
                self._recovered_job_references = references
                self._recovery_restored = True
                if config_id is not None:
                    self._configuration_revisions[config_id] = cast(
                        str, validated["configuration_sha256"])
                restore = self._draft_restorer
                if restore is None:
                    self._pending_recovery_draft = deepcopy(
                        dict(cast(Mapping, normalized["draft"])))
            optical_restore = {"pending": False, "request_ids": {}}
            try:
                if restore is not None:
                    restore(deepcopy(dict(cast(Mapping, normalized["draft"]))))
                if "optical_inputs" in normalized:
                    optical_restore = self._input_drafts.restore(normalized["optical_inputs"])
            except Exception:
                with self._lock:
                    (self._configuration_id, self._process_document_id,
                     self._appearance_context_json, self._recovered_job_references,
                     self._recovery_restored, self._configuration_revisions,
                     self._pending_recovery_draft, self._draft_baseline_signature) = prior
                if previous_draft is not None:
                    try:
                        restore(previous_draft)
                    except Exception as rollback_error:
                        raise ProjectComputeError(
                            "recovery_rollback_failed",
                            "Compute selection was restored, but its presentation draft rollback failed",
                        ) from rollback_error
                raise
            return {"restored": not optical_restore["pending"], "configuration_id": config_id,
                    "process_document_id": process_id,
                    "job_references": len(references), "jobs_resubmitted": False,
                    "optical_pending": optical_restore["pending"],
                    "optical_restore_token": optical_restore.get("token"),
                    "optical_request_ids": optical_restore["request_ids"]}

    def register_job(self, job_id: str, *, module_key: str = "main",
                     service_owned: bool = True, state: str = "queued",
                     metadata: Mapping | None = None) -> None:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be nonempty")
        if type(service_owned) is not bool:
            raise TypeError("service_owned must be a bool")
        with self.host.operation_scope(self.binding):
            with self._lock:
                self._ensure_admission()
                self._jobs[job_id] = {
                    "job_id": job_id, "module_key": module_key,
                    "service_owned": service_owned,
                    "project_id": self.binding.project_id,
                    "generation": self.binding.generation,
                    "state": str(state), "metadata": deepcopy(dict(metadata or {})),
                }

    def update_job(self, job_id: str, state: str, metadata: Mapping | None = None,
                   service_owned: bool | None = None) -> bool:
        if service_owned is not None and type(service_owned) is not bool:
            raise TypeError("service_owned must be a bool or None")
        with self._lock:
            row = self._jobs.get(job_id)
            if row is None:
                return False
            row["state"] = str(state)
            if metadata:
                row["metadata"].update(deepcopy(dict(metadata)))
            if service_owned is not None:
                row["service_owned"] = service_owned
            return True

    def active_jobs(self) -> list[dict]:
        with self._lock:
            return [deepcopy(row) for row in self._jobs.values()
                    if row["state"] not in self._TERMINAL]

    def job_records(self) -> list[dict]:
        with self._lock:
            return deepcopy(list(self._jobs.values()))

    def _ensure_admission(self) -> None:
        if self._frozen:
            raise ProjectComputeError("editing_frozen", "Project compute is frozen for close preflight")
