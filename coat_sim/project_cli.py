"""Manage a local project without starting the desktop UI.

Run with ``python -m coat_sim.project_cli --help``.  The CLI is intentionally
limited to project persistence operations; it never launches a calculation or
contacts an external service.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, TextIO


MAX_JSON_FILE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_ERROR_MESSAGE_CHARS = 4096


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _read_json_object(path: str, description: str) -> dict[str, Any]:
    """Read one finite JSON object from an explicitly named local file."""
    with Path(path).open("rb") as source:
        raw = source.read(MAX_JSON_FILE_BYTES + 1)
    if len(raw) > MAX_JSON_FILE_BYTES:
        raise ValueError(f"{description} exceeds the {MAX_JSON_FILE_BYTES}-byte limit")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{description} must be finite UTF-8 JSON with unique fields") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} must contain a JSON object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create an empty project")
    create.add_argument("root", help="New project directory")
    create.add_argument("--name", required=True, help="Human-readable project name")

    inspect = commands.add_parser("inspect", help="Read a project manifest")
    inspect.add_argument("project_file", help="Project file (.coatproj)")

    import_document = commands.add_parser("import", help="Import a typed design document")
    import_document.add_argument("project_file", help="Project file (.coatproj)")
    import_document.add_argument("file", help="External .cproc/.cmac/.csub/.csrc/.cmsk/.cfilm file")
    import_document.add_argument(
        "--materials", help="Explicit JSON object mapping optical material IDs to records"
    )

    save = commands.add_parser("save", help="Save an edited project-local design document")
    save.add_argument("project_file", help="Project file (.coatproj)")
    save.add_argument("document_id", help="Existing project document ID")
    save.add_argument("file", help="JSON object containing the edited document")
    save.add_argument("--materials", help="Explicit JSON mapping of required optical material IDs to records")

    create_document = commands.add_parser("create-document", help="Create a managed film or expression template from JSON")
    create_document.add_argument("project_file", help="Project file (.coatproj)")
    create_document.add_argument("kind", choices=("film", "expression_template"))
    create_document.add_argument("file", help="Explicit JSON object containing the working document")
    create_document.add_argument("--materials", help="Explicit JSON mapping of required optical material IDs to records")

    configure = commands.add_parser("configure", help="Add a deposition configuration")
    configure.add_argument("project_file", help="Project file (.coatproj)")
    configure.add_argument("process_id", help="Imported process document ID")
    configure.add_argument("--name", required=True, help="Configuration name")
    configure.add_argument("--parameters", help="Optional JSON object of recipe parameters")
    configure.add_argument(
        "--default", action="store_true", help="Designate this configuration as the project default"
    )

    set_default = commands.add_parser("default", help="Set or clear the designated configuration")
    set_default.add_argument("project_file", help="Project file (.coatproj)")
    set_default.add_argument("configuration_id", nargs="?", help="Configuration ID to designate")
    set_default.add_argument(
        "--clear", action="store_true", help="Clear the designated configuration"
    )

    resolve = commands.add_parser(
        "resolve", help="Resolve one document or the default project graph"
    )
    resolve.add_argument("project_file", help="Project file (.coatproj)")
    resolve.add_argument(
        "document_id", nargs="?", help="Document ID; omit to resolve the designated default"
    )

    export = commands.add_parser("export", help="Export one document and its dependency closure")
    export.add_argument("project_file", help="Project file (.coatproj)")
    export.add_argument("document_id", help="Document ID to export")
    export.add_argument("destination", help="New destination directory")

    package_export = commands.add_parser("export-package", help="Export all saved project inputs as .cproject")
    package_export.add_argument("project_file", help="Project file (.coatproj)")
    package_export.add_argument("destination", help="New .cproject file")
    package_import = commands.add_parser("import-package", help="Import a .cproject into a new independent project")
    package_import.add_argument("file", help="Project package")
    package_import.add_argument("destination", help="New project directory")

    recovery_record = commands.add_parser(
        "recovery-record", help="Record IDs for explicit recovery"
    )
    recovery_record.add_argument("project_file", help="Project file (.coatproj)")
    recovery_record.add_argument("document_ids", nargs="*", help="Existing document IDs to record")

    recovery_read = commands.add_parser("recovery-read", help="Read the explicit recovery record")
    recovery_read.add_argument("project_file", help="Project file (.coatproj)")

    manage = commands.add_parser("manage", help="Strict identity/revision-bound headless project operation")
    manage.add_argument("operation", choices=("create", "inspect", "resolve", "export", "recovery_read",
                                              "import", "save", "create_document", "configure", "set_default", "recovery_record",
                                              "bbs_list", "bbs_read", "bbs_history", "bbs_create", "bbs_reply", "bbs_update", "bbs_archive", "bbs_comment", "bbs_import_attachment"))
    manage.add_argument("arguments", help="Explicit JSON object with root/project_id and mutation operation_id/expected_revision")
    for command in (inspect, import_document, save, create_document, configure, set_default,
                    resolve, export, package_export, recovery_record, recovery_read):
        command.add_argument("--project-id", help="Reject a project file with a different persisted project ID")
        command.add_argument("--expected-revision", type=int, help="Reject a project revision that changed")
    return parser


def _serialize(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError(f"JSON result exceeds the {MAX_OUTPUT_BYTES}-byte output limit")
    return encoded


def _failure(exc: Exception, stderr: TextIO) -> int:
    project_error_code = getattr(exc, "code", None)
    if project_error_code is not None:
        code = str(project_error_code)
    elif isinstance(exc, OSError):
        code = "io_error"
    else:
        code = "invalid_input"
    message = str(exc)[:MAX_ERROR_MESSAGE_CHARS]
    print(_serialize({"error": {"code": code, "message": message}}), file=stderr)
    return 2


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = _build_parser().parse_args(argv)

    # Import only after argument parsing, so --help stays usable in a minimal
    # headless process and does not initialize the project or desktop stack.
    from .core.project_store import ProjectError, ProjectStore

    lease = None
    store = None
    try:
        if args.command not in {"create", "manage", "import-package"}:
            current = ProjectStore.open(args.project_file)
            if args.project_id is not None and args.project_id != current.manifest["id"]:
                raise ProjectError("project_identity_mismatch", "Explicit project ID does not match this project file")
            if args.expected_revision is not None and (args.expected_revision < 0 or
                    args.expected_revision != current.manifest["revision"]):
                raise ProjectError("stale_project", "Expected project revision no longer matches")
            if args.command in {"import", "save", "create-document", "configure", "default", "recovery-record"}:
                from .project_recovery import ProjectOwnerLease
                lease = ProjectOwnerLease.acquire(current.project_file, current.manifest["id"])
                current = ProjectStore.open(current.project_file)
                if args.expected_revision is not None and args.expected_revision != current.manifest["revision"]:
                    raise ProjectError("stale_project", "Expected project revision changed during admission")
        if args.command == "manage":
            from .project_management import ProjectManagementService
            result = ProjectManagementService().execute(args.operation, _read_json_object(args.arguments, "arguments"))
        elif args.command == "create":
            store = ProjectStore.create(args.root, args.name)
            result = {"ok": True, "project": store.manifest}
        elif args.command == "import-package":
            from .project_package import import_project
            store = import_project(args.file, args.destination)
            result = {"ok": True, "project": store.manifest}
        elif args.command == "export-package":
            from .project_package import export_project
            store = current
            result = {"ok": True, **export_project(store, args.destination)}
        elif args.command == "inspect":
            store = ProjectStore.open(args.project_file)
            result = {"ok": True, "project": store.manifest}
        elif args.command == "import":
            materials = _read_json_object(args.materials, "materials") if args.materials else None
            store = ProjectStore.open(args.project_file)
            document_id = store.import_document(args.file, materials=materials)
            result = {"ok": True, "document_id": document_id, "project": store.manifest}
        elif args.command == "save":
            document = _read_json_object(args.file, "document")
            store = ProjectStore.open(args.project_file)
            materials = _read_json_object(args.materials, "materials") if args.materials else None
            store.save_document(args.document_id, document, materials=materials)
            result = {
                "ok": True,
                "document_id": args.document_id,
                "project": store.manifest,
            }
        elif args.command == "create-document":
            document = _read_json_object(args.file, "document")
            materials = _read_json_object(args.materials, "materials") if args.materials else None
            store = ProjectStore.open(args.project_file)
            document_id = store.create_document(args.kind, document, materials=materials)
            result = {"ok": True, "document_id": document_id, "project": store.manifest}
        elif args.command == "configure":
            parameters = (
                _read_json_object(args.parameters, "parameters") if args.parameters else None
            )
            store = ProjectStore.open(args.project_file)
            configuration_id = store.add_deposition_configuration(
                args.name, args.process_id, parameters=parameters
            )
            if args.default:
                store.set_default_configuration(configuration_id)
            result = {
                "ok": True,
                "configuration_id": configuration_id,
                "project": store.manifest,
            }
        elif args.command == "default":
            if args.clear == bool(args.configuration_id):
                raise ValueError("provide a configuration ID or --clear")
            store = ProjectStore.open(args.project_file)
            store.set_default_configuration(None if args.clear else args.configuration_id)
            result = {"ok": True, "project": store.manifest}
        elif args.command == "resolve":
            store = ProjectStore.open(args.project_file)
            graph = (store.resolve_document(args.document_id) if args.document_id
                     else store.resolve_default())
            result = {"ok": True, "document_id": args.document_id, "graph": graph}
        elif args.command == "export":
            store = ProjectStore.open(args.project_file)
            exported = store.export_document(args.document_id, args.destination)
            result = {"ok": True, **exported}
        elif args.command == "recovery-record":
            store = ProjectStore.open(args.project_file)
            store.record_recovery(args.document_ids)
            result = {"ok": True, "recorded_document_ids": args.document_ids}
        elif args.command == "recovery-read":
            store = ProjectStore.open(args.project_file)
            result = {"ok": True, "recovery": store.recover_session()}
        else:  # pragma: no cover - argparse restricts the command set
            raise ValueError("unsupported project command")
        if store is not None:
            result.setdefault("root", str(store.root))
            result.setdefault("project_file", str(store.project_file))
        if store is not None and store.maintenance_warnings:
            result["warnings"] = store.maintenance_warnings
        print(_serialize(result), file=stdout)
        return 0
    except ProjectError as exc:
        return _failure(exc, stderr)
    except ValueError as exc:
        return _failure(exc, stderr)
    except OSError as exc:
        return _failure(exc, stderr)
    finally:
        if lease is not None:
            lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
