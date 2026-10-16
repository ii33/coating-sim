"""Explicit headless project management shared by AI, MCP and CLI adapters.

Every existing-project operation binds a .coatproj file and persisted ID at admission.
The historical ``root`` request field carries that file; only creation takes
a new directory. Response ``root`` remains the canonical resource directory.
Mutations reject an active desktop owner, rather than bypassing working drafts.
No GUI, foreground-project state or implicit project environment is consulted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease

READ_OPERATIONS = frozenset({"inspect", "resolve", "export", "recovery_read"})
WRITE_OPERATIONS = frozenset({"import", "save", "create_document", "configure", "set_default", "recovery_record"})
PROJECT_OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS | {"create"}
_FIELDS = {
    "create": {"root", "name", "operation_id"},
    "inspect": set(), "resolve": {"document_id"}, "export": {"document_id", "destination"},
    "recovery_read": set(), "import": {"file", "materials"},
    "save": {"document_id", "document", "materials"},
    "create_document": {"kind", "document", "materials"},
    "configure": {"name", "process_id", "parameters"},
    "set_default": {"configuration_id"}, "recovery_record": {"document_ids"},
}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 32768:
        raise ProjectError("invalid_arguments", f"{name} must be an explicit nonempty string")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectError("invalid_arguments", f"{name} must be an explicit JSON object")
    return value


class ProjectManagementService:
    """Persistence-only operations; active owners must be addressed separately."""

    def __init__(self, authorized_roots: list[Path | str] | tuple[Path | str, ...] | None = None):
        self._authorized_roots = (None if authorized_roots is None else
                                  tuple(Path(root).expanduser().resolve() for root in authorized_roots))

    def _authorize_path(self, value: str | Path) -> None:
        if self._authorized_roots is None:
            return
        path = Path(value).expanduser().resolve()
        if not any(path == root or root in path.parents for root in self._authorized_roots):
            raise ProjectError("project_root_not_authorized", "Choose this project or file in the project manager before using AI tools")

    def execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in PROJECT_OPERATIONS or not isinstance(arguments, dict):
            raise ProjectError("invalid_operation", "Unknown project management operation")
        allowed = _FIELDS[operation] | ({"root", "project_id", "operation_id", "expected_revision"}
                                        if operation != "create" else set())
        unknown = set(arguments) - allowed
        if unknown:
            raise ProjectError("invalid_arguments", "Unknown project argument: " + sorted(unknown)[0])
        target = Path(_text(arguments.get("root"), "root")).expanduser().absolute()
        self._authorize_path(target)
        for field in ("file", "destination"):
            if arguments.get(field):
                self._authorize_path(_text(arguments[field], field))
        if operation == "create":
            identity = _text(arguments.get("operation_id"), "operation_id")
            store = ProjectStore.create(target, _text(arguments.get("name"), "name"))
            return self._result(store, identity)
        project_id = _text(arguments.get("project_id"), "project_id")
        store = ProjectStore.open(target)
        if store.manifest["id"] != project_id:
            raise ProjectError("project_identity_mismatch", "Explicit project ID does not match this root")
        if operation in WRITE_OPERATIONS:
            identity = _text(arguments.get("operation_id"), "operation_id")
            expected = arguments.get("expected_revision")
            if type(expected) is not int or expected < 0:
                raise ProjectError("revision_required", "Mutations require a nonnegative expected_revision")
            with ProjectOwnerLease.acquire(store.project_file, project_id):
                # Capture revision after ownership admission. ProjectStore's
                # commit fingerprint also protects against noncooperating races.
                store = ProjectStore.open(store.project_file)
                if store.manifest["revision"] != expected:
                    raise ProjectError("stale_project", "Expected project revision no longer matches")
                extra = self._mutate(store, operation, arguments)
                return self._result(store, identity, **extra)
        if operation == "inspect":
            return self._result(store)
        if operation == "resolve":
            document_id = arguments.get("document_id")
            graph = store.resolve_document(_text(document_id, "document_id")) if document_id else store.resolve_default()
            return self._result(store, graph=graph, document_id=document_id)
        if operation == "export":
            exported = store.export_document(_text(arguments.get("document_id"), "document_id"),
                                             _text(arguments.get("destination"), "destination"))
            return self._result(store, **exported)
        if operation == "recovery_read":
            return self._result(store, recovery=store.recover_session())
        raise AssertionError(operation)

    def execute_owned(self, operation: str, arguments: dict[str, Any], *,
                      store: ProjectStore, project_id: str) -> dict[str, Any]:
        """Use the caller's already-owned store under its session admission gate.

        This is a trusted in-process adapter seam, never a transport payload
        flag. The caller must hold ProjectOwnerLease for this exact project,
        validate session generation and freeze/reject dirty draft replacement.
        """
        if operation not in PROJECT_OPERATIONS or operation == "create" or not isinstance(arguments, dict):
            raise ProjectError("invalid_operation", "Operation is not valid for an existing owned project")
        allowed = _FIELDS[operation] | {"root", "project_id", "operation_id", "expected_revision"}
        unknown = set(arguments) - allowed
        if unknown:
            raise ProjectError("invalid_arguments", "Unknown project argument: " + sorted(unknown)[0])
        target = Path(_text(arguments.get("root"), "root")).expanduser().absolute()
        identity = _text(arguments.get("project_id"), "project_id")
        self._authorize_path(target)
        # Parse the explicit file before comparing its parent identity. A
        # same-directory alias, directory or ZIP cannot bypass admission.
        selected = ProjectStore.open(target)
        if (identity != project_id or store.manifest["id"] != identity
                or selected.manifest["id"] != identity
                or selected.project_file != store.project_file):
            raise ProjectError("project_identity_mismatch", "Explicit target does not match the owning store")
        for field in ("file", "destination"):
            if arguments.get(field):
                self._authorize_path(_text(arguments[field], field))
        store._current()  # Reject a replaced manifest before the owner's action.
        if operation in WRITE_OPERATIONS:
            operation_id = _text(arguments.get("operation_id"), "operation_id")
            expected = arguments.get("expected_revision")
            if type(expected) is not int or expected < 0:
                raise ProjectError("revision_required", "Mutations require a nonnegative expected_revision")
            if store.manifest["revision"] != expected:
                raise ProjectError("stale_project", "Expected project revision no longer matches")
            return self._result(store, operation_id, **self._mutate(store, operation, arguments))
        # Read/export use the same bounded service contract and saved state.
        return self.execute(operation, arguments)

    @staticmethod
    def _result(store: ProjectStore, operation_id: str = "", **extra: Any) -> dict[str, Any]:
        manifest = store.manifest
        return {"ok": True, "project_id": manifest["id"], "root": str(store.root),
                "project_file": str(store.project_file),
                "revision": manifest["revision"], "operation_id": operation_id,
                "project": manifest, **extra}

    @staticmethod
    def _mutate(store: ProjectStore, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        if operation == "import":
            return {"document_id": store.import_document(_text(args.get("file"), "file"), materials=args.get("materials"))}
        if operation == "save":
            document_id = _text(args.get("document_id"), "document_id")
            store.save_document(document_id, _object(args.get("document"), "document"), materials=args.get("materials"))
            return {"document_id": document_id}
        if operation == "create_document":
            return {"document_id": store.create_document(_text(args.get("kind"), "kind"),
                                                          _object(args.get("document"), "document"), materials=args.get("materials"))}
        if operation == "configure":
            return {"configuration_id": store.add_deposition_configuration(
                _text(args.get("name"), "name"), _text(args.get("process_id"), "process_id"), args.get("parameters"))}
        if operation == "set_default":
            configuration_id = args.get("configuration_id")
            if configuration_id is not None:
                _text(configuration_id, "configuration_id")
            store.set_default_configuration(configuration_id)
            return {}
        if operation == "recovery_record":
            store.record_recovery(args.get("document_ids"))
            return {"recorded_document_ids": args.get("document_ids")}
        raise AssertionError(operation)
