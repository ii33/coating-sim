"""Explicit, headless recovery snapshots for one project-owned film draft.

Snapshots are detached finite JSON values. Validation is read-only; restoration
is a separate approved operation and always creates an unsaved draft copy.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping

from .film_design import normalize_expression, validate_design
from .film_design_service import simulation_settings
from .film_design_session import FilmDesignEditSession
from .film_process_import import (FROZEN_KIND, FROZEN_SCHEMA_VERSION,
                                  validate_frozen_design_snapshot)
from .project_dependencies import (ProjectError, document_material_references,
                                   normalize_optical_material)


RECOVERY_KIND = "coat_sim.project_film_recovery"
RECOVERY_SCHEMA_VERSION = 1
_RECOVERY_FIELDS = frozenset({
    "kind", "schema_version", "project_id", "dirty",
    "managed_document_id", "managed_document_revision", "origin",
    "expression_text", "film", "sha256",
})
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")


def _information_token(document_id: str | None, design: Mapping[str, Any]) -> str:
    values = {"name": str(design.get("name", "")), "summary": str(design.get("summary", "")),
              "notes": str(design.get("note", ""))}
    return hashlib.sha256(json.dumps([document_id or "", values],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _freeze_editable_design(service: Any, design: Mapping[str, Any]) -> dict[str, Any]:
    """Capture editable closure without requiring every selector to be bound.

    Process-import snapshots are numerical inputs, so their normal producer
    expects active references to resolve. A recovery checkpoint must also
    represent a valid editor draft with intentionally blank selectors.
    """
    normalized = validate_design(service.validate(design))
    required: dict[str, set[str]] = {}
    for material_id, set_id in document_material_references("film", normalized):
        required.setdefault(material_id, set()).add(set_id)

    records = []
    for material_id, set_ids in sorted(required.items()):
        # The project-scoped provider is the only source. A nonempty dangling
        # ID remains an error; blank selectors simply are not dependencies.
        record = normalize_optical_material(service.materials.get(material_id))
        available = {row["id"] for row in record["optical_sets"]}
        if not set_ids.issubset(available):
            missing = ", ".join(sorted(set_ids - available))
            raise ProjectError(
                "missing_dependency",
                f"Project optical data is missing: {material_id}/{missing}",
            )
        records.append(record)

    frozen = {
        "schema_version": FROZEN_SCHEMA_VERSION,
        "kind": FROZEN_KIND,
        "design": normalized,
        "settings": simulation_settings({"first_surface": normalized["first_surface"]}),
        "materials": records,
    }
    frozen["sha256"] = _digest(frozen)
    return validate_frozen_design_snapshot(frozen)


def _digest(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "sha256"}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _detached_json(value: Any) -> Any:
    """Return a JSON-only copy, rejecting non-string keys and non-finite data."""
    def check(item: Any, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError("film recovery data is nested too deeply")
        if item is None or isinstance(item, (str, bool)):
            return
        if type(item) is int:
            return
        if type(item) is float:
            if item != item or item in (float("inf"), float("-inf")):
                raise ValueError("film recovery data must contain finite JSON numbers")
            return
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("film recovery object keys must be strings")
            for child in item.values():
                check(child, depth + 1)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                check(child, depth + 1)
            return
        raise ValueError("film recovery data must contain finite JSON values")

    check(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    return json.loads(encoded)


def _workspace_identity(workspace: Any) -> str:
    try:
        project_id = workspace.binding.project_id
        service_id = workspace.service.project_id
    except (AttributeError, TypeError) as exc:
        raise ProjectError("invalid_recovery", "A bound project film workspace is required") from exc
    if (not isinstance(project_id, str) or not _ID_RE.fullmatch(project_id)
            or service_id != project_id):
        raise ProjectError("project_identity_mismatch", "Project film workspace identity is invalid")
    return project_id


def _editor(workspace: Any) -> Any:
    editor = getattr(workspace, "editor", None)
    if editor is None or not callable(getattr(editor, "snapshot", None)):
        raise ProjectError("invalid_recovery", "Workspace does not expose a film edit session")
    return editor


def _managed_document(workspace: Any, editor: Any,
                      path: str | None) -> tuple[str | None, str | None, Any | None]:
    if path is None:
        return None, None, None
    service = workspace.service
    document_id = service._handle_id(path)
    document = service.load_document(document_id)
    loaded_revision = service._loaded_revisions.get(document_id)
    if not isinstance(loaded_revision, str) or loaded_revision != document.revision:
        raise ProjectError("stale_recovery", "Saved film changed after this draft was loaded")
    return document_id, loaded_revision, document


def _current_token_locked(workspace: Any, editor: Any) -> dict[str, Any]:
    """Read a replacement guard while editor and host operation locks are held."""
    session = editor.snapshot()
    document_id = document_revision = None
    if session.path is not None:
        service = workspace.service
        document_id = service._handle_id(session.path)
        current = service.load_document(document_id)
        document_revision = current.revision
    return {
        "project_id": _workspace_identity(workspace),
        "design_revision": session.design_revision,
        "session_revision": session.revision,
        "managed_document_id": document_id,
        "managed_document_revision": document_revision,
        "expression_busy": bool(session.expression_busy or editor._active_expression is not None),
        "pending_confirmation": session.pending_confirmation is not None,
    }


def film_recovery_current_token(workspace: Any) -> dict[str, Any]:
    """Return the live project/editor revision token for a recovery prompt.

    Pass the returned revision values to :func:`restore_film_recovery` after
    the user approves. A later edit, document switch, or saved-document change
    then prevents replacement of the draft they actually reviewed.
    """
    editor = _editor(workspace)
    with editor._lock:
        with workspace.host.operation_scope(workspace.binding):
            return deepcopy(_current_token_locked(workspace, editor))


def snapshot_film_recovery(workspace: Any) -> dict[str, Any]:
    """Capture one validated, finite-JSON film draft and its complete closure."""
    editor = _editor(workspace)
    with editor._lock:
        with workspace.host.operation_scope(workspace.binding):
            project_id = _workspace_identity(workspace)
            session = editor.snapshot()
            if (session.expression_busy or editor._active_expression is not None):
                raise ProjectError("pending_operation", "Wait for the film expression operation to finish before recovery")
            if session.pending_confirmation is not None:
                raise ProjectError("pending_confirmation", "Resolve the pending film confirmation before recovery")

            document_id, document_revision, _document = _managed_document(
                workspace, editor, session.path)
            expression_text = session.expression_text
            film = _freeze_editable_design(workspace.service, session.design)

            origin = None
            saved_origin = getattr(editor, "_recovery_origin", None)
            origin_design_id = getattr(editor, "_recovery_origin_design_id", None)
            if (isinstance(saved_origin, Mapping)
                    and origin_design_id == session.design.get("id")):
                origin = deepcopy(dict(saved_origin))
            elif document_id is not None and document_revision is not None:
                origin = {"document_id": document_id,
                          "document_revision": document_revision}

            result = {
                "kind": RECOVERY_KIND,
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "project_id": project_id,
                "dirty": session.dirty,
                "managed_document_id": document_id,
                "managed_document_revision": document_revision,
                "origin": origin,
                "expression_text": expression_text,
                "film": film,
            }
            pending_information = getattr(editor, "_pending_information_edit", None)
            if pending_information is not None:
                information = deepcopy(pending_information)
                information["model_dirty"] = session.dirty
                result["pending_information"] = information
                result["dirty"] = True
            result["sha256"] = _digest(result)

    # A capture is only returned if the public validator accepts the same
    # detached value. This also detects a saved document that changed while
    # the snapshot was being assembled.
    detached = _detached_json(result)
    return validate_film_recovery(workspace, detached)


def _validate_document_identity(document_id: Any, revision: Any, *, label: str,
                                optional: bool) -> tuple[str | None, str | None]:
    if optional and document_id is None and revision is None:
        return None, None
    if (not isinstance(document_id, str) or not _ID_RE.fullmatch(document_id)
            or not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision)):
        raise ProjectError("invalid_recovery", f"Invalid {label} film document identity")
    return document_id, revision


def _validate_film_closure(film: Mapping[str, Any]) -> dict[str, Any]:
    try:
        frozen = validate_frozen_design_snapshot(film)
        design = frozen["design"]
        required: dict[str, set[str]] = {}
        for material_id, set_id in document_material_references("film", design):
            required.setdefault(material_id, set()).add(set_id)

        rows = frozen.get("materials")
        if not isinstance(rows, list):
            raise ValueError("film recovery materials are invalid")
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            normalized = normalize_optical_material(row)
            if normalized != row:
                raise ValueError("film recovery material records are not canonical")
            by_id[row["id"]] = row
        if set(by_id) != set(required):
            raise ValueError("film recovery material closure does not exactly match its design")
        for material_id, set_ids in required.items():
            available = {row["id"] for row in by_id[material_id]["optical_sets"]}
            if not set_ids.issubset(available):
                raise ValueError("film recovery is missing a referenced optical dataset")
        return frozen
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ProjectError):
            raise
        raise ProjectError("invalid_recovery", "Film recovery design or material closure is invalid") from exc


def _validate_film_recovery_locked(workspace: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    project_id = _workspace_identity(workspace)
    value = _detached_json(snapshot)
    if (not isinstance(value, dict) or not _RECOVERY_FIELDS.issubset(value)
            or set(value) - _RECOVERY_FIELDS - {"pending_information"}):
        raise ProjectError("invalid_recovery", "Film recovery snapshot fields are invalid")
    if (value.get("kind") != RECOVERY_KIND
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != RECOVERY_SCHEMA_VERSION):
        raise ProjectError("invalid_recovery", "Film recovery snapshot kind or version is invalid")
    if value.get("project_id") != project_id:
        raise ProjectError("project_identity_mismatch", "Film recovery belongs to a different project")
    if type(value.get("dirty")) is not bool:
        raise ProjectError("invalid_recovery", "Film recovery dirty flag is invalid")
    digest = value.get("sha256")
    if (not isinstance(digest, str) or not _REVISION_RE.fullmatch(digest)
            or _digest(value) != digest):
        raise ProjectError("invalid_recovery", "Film recovery checksum is invalid")

    _validate_document_identity(value.get("managed_document_id"),
                                value.get("managed_document_revision"),
                                label="managed", optional=True)
    origin = value.get("origin")
    if origin is not None:
        if not isinstance(origin, dict) or set(origin) != {"document_id", "document_revision"}:
            raise ProjectError("invalid_recovery", "Film recovery origin is invalid")
        _validate_document_identity(origin.get("document_id"),
                                    origin.get("document_revision"),
                                    label="origin", optional=False)

    expression_text = value.get("expression_text")
    if not isinstance(expression_text, str) or len(expression_text) > 20000:
        raise ProjectError("invalid_recovery", "Film recovery expression text is invalid")
    film = value.get("film")
    if not isinstance(film, Mapping):
        raise ProjectError("invalid_recovery", "Film recovery payload is invalid")
    frozen = _validate_film_closure(film)
    if normalize_expression(expression_text) != frozen["design"]["expression"]["text"]:
        raise ProjectError("invalid_recovery", "Film recovery expression text does not match its design")

    information = value.get("pending_information")
    if information is not None:
        if (not isinstance(information, dict) or set(information) != {
                "name", "summary", "notes", "document_id", "token", "model_dirty"}
                or information["document_id"] != value["managed_document_id"]
                or type(information["model_dirty"]) is not bool or value["dirty"] is not True
                or any(not isinstance(information[key], str) or len(information[key]) > limit
                       for key, limit in (("name", 200), ("summary", 20000), ("notes", 20000)))
                or information["token"] != _information_token(value["managed_document_id"], frozen["design"])):
            raise ProjectError("invalid_recovery", "Unconfirmed film information recovery is invalid or stale")

    document_id = value["managed_document_id"]
    document_revision = value["managed_document_revision"]
    if document_id is not None:
        try:
            source = workspace.service.load_document(document_id)
        except (ProjectError, OSError, ValueError) as exc:
            raise ProjectError("stale_recovery", "The saved film for this recovery is no longer available") from exc
        if (source.project_id != project_id
                or source.revision != document_revision
                or source.design.get("id") != frozen["design"].get("id")):
            raise ProjectError("stale_recovery", "The saved film revision for this recovery has changed")

    value["film"] = frozen
    return value


def validate_film_recovery(workspace: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach a recovery value without mutating editor or project."""
    with workspace.host.operation_scope(workspace.binding):
        return _validate_film_recovery_locked(workspace, snapshot)


def restore_film_recovery(
    workspace: Any,
    snapshot: Mapping[str, Any],
    *,
    approved: bool,
    expected_current_design_revision: int,
    expected_current_session_revision: int,
    expected_current_document_id: str | None = None,
    expected_current_document_revision: str | None = None,
) -> bool:
    """Replace the live draft only after explicit approval and a matching token.

    Restores are always unsaved copies. The source document identity is kept as
    provenance, while the next Save creates a new managed document instead of
    reusing a potentially unrelated handle.
    """
    if approved is not True:
        return False
    if type(expected_current_design_revision) is not int:
        return False
    if type(expected_current_session_revision) is not int:
        return False
    if ((expected_current_document_id is None) !=
            (expected_current_document_revision is None)):
        return False
    if expected_current_document_id is not None:
        if (not isinstance(expected_current_document_id, str)
                or not _ID_RE.fullmatch(expected_current_document_id)
                or not isinstance(expected_current_document_revision, str)
                or not _REVISION_RE.fullmatch(expected_current_document_revision)):
            return False

    try:
        editor = _editor(workspace)
        with editor._lock:
            with workspace.host.operation_scope(workspace.binding):
                token = _current_token_locked(workspace, editor)
                session = editor.snapshot()
                if (session.expression_busy or editor._active_expression is not None
                        or session.pending_confirmation is not None):
                    editor._set_error("请先完成当前膜系表达式操作或确认，再恢复内容。")
                    return False
                if (token["design_revision"] != expected_current_design_revision
                        or token["session_revision"] != expected_current_session_revision
                        or (expected_current_document_id is not None and
                            (token["managed_document_id"] != expected_current_document_id
                             or token["managed_document_revision"] != expected_current_document_revision))):
                    editor._set_error("当前膜系设计已变化；请重新检查恢复确认。")
                    return False

                value = _validate_film_recovery_locked(workspace, snapshot)
                film = value["film"]
                materials = {row["id"]: deepcopy(row) for row in film["materials"]}
                design = deepcopy(film["design"])
                # The normalized frozen schema is authoritative, while the
                # separately captured field retains the editor's exact text.
                design["expression"]["text"] = value["expression_text"]

                source_id = value["managed_document_id"]
                source_revision = value["managed_document_revision"]
                origin = value["origin"] or (
                    {"document_id": source_id, "document_revision": source_revision}
                    if source_id is not None else None)
                if origin is None:
                    message = "已恢复项目膜系设计；此内容尚未写入项目。"
                else:
                    message = ("已恢复项目膜系设计（来源文档 "
                               f"{origin['document_id']}，保存版本 {origin['document_revision']}）；"
                               "此副本尚未保存，保存时将新建项目膜系文档。")

                # Capture immutable/reassignable references so an exceptional
                # adapter refresh cannot leave half of a recovery activated.
                old_editor = {
                    name: getattr(editor, name)
                    for name in ("_design", "_derived", "_catalog_options", "_material_options",
                                 "_design_revision", "_path", "_dirty", "_message", "_error",
                                 "_generated_undo_state", "_frozen_draft_materials",
                                 "_prepared_optical_cache", "_reference_index_cache", "_pending_information_edit")
                }
                old_service = (workspace.service._draft_materials, workspace.service.materials)
                old_origin = getattr(editor, "_recovery_origin", None)
                old_origin_design = getattr(editor, "_recovery_origin_design_id", None)
                try:
                    workspace.service.activate_frozen_draft_materials(materials.values())
                    editor._frozen_draft_materials = materials
                    editor._refresh_project_materials()
                    editor._recovery_origin = deepcopy(origin)
                    editor._recovery_origin_design_id = design["id"] if origin is not None else None
                    information = deepcopy(value.get("pending_information"))
                    if information is not None:
                        # Recovery keeps the existing unsaved-copy boundary.
                        # Retarget only the sidecar guard to that reviewed copy;
                        # its text is still unconfirmed and never enters design.
                        information.update(document_id=None, model_dirty=True,
                                           token=_information_token(None, design))
                    editor._pending_information_edit = information
                    FilmDesignEditSession._replace_design(
                        editor, design, path=None, message=message, dirty=True)
                    return True
                except Exception:
                    workspace.service._draft_materials, workspace.service.materials = old_service
                    for name, previous in old_editor.items():
                        setattr(editor, name, previous)
                    if old_origin is None:
                        editor.__dict__.pop("_recovery_origin", None)
                    else:
                        editor._recovery_origin = old_origin
                    if old_origin_design is None:
                        editor.__dict__.pop("_recovery_origin_design_id", None)
                    else:
                        editor._recovery_origin_design_id = old_origin_design
                    raise
    except (ProjectError, ValueError, TypeError, OSError, AttributeError, RuntimeError) as exc:
        try:
            editor = _editor(workspace)
            with editor._lock:
                editor._set_error(str(exc))
        except Exception:
            pass
        return False


__all__ = [
    "RECOVERY_KIND", "RECOVERY_SCHEMA_VERSION", "film_recovery_current_token",
    "restore_film_recovery", "snapshot_film_recovery", "validate_film_recovery",
]
