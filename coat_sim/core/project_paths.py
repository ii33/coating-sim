"""Typed project-local chooser paths, independent of GUI and persisted objects."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
import unicodedata

from .project_dependencies import EXTENSIONS, ProjectError, identifier, reject_links


PROJECT_TYPED_DIRECTORY_BY_KIND = MappingProxyType({
    "source": "data/sources",
    "substrate": "data/substrates",
    "mask": "data/masks",
    "machine": "data/machines",
    "process": "data/processes",
    "film": "data/films",
    "expression_template": "data/expression_templates",
    "material_profile": "data/materials",
    "optical_material": "data/materials",
    "deposition_configuration": "data/configurations",
    "raytrace_request": "data/raytrace",
    "appearance_configuration": "data/appearance",
    "binary_asset": "data/assets",
    "project_bbs_post": "data/bbs",
})

PROJECT_LAYOUT_DIRECTORIES = (
    "objects",
    "data/sources",
    "data/substrates",
    "data/masks",
    "data/machines",
    "data/processes",
    "data/films",
    "data/expression_templates",
    "data/raytrace",
    "data/appearance",
    "data/materials",
    "data/configurations",
    "data/assets",
    "data/bbs",
    "imports",
    "exports",
    "assets",
    "results",
)


def ensure_project_layout(root: str | Path) -> dict[str, Path]:
    """Create and return the stable typed chooser folders of a project.

    Managed JSON lives in immutable r<revision> children of these categories.
    UUID chooser handles remain logical references rather than physical files.
    Existing legacy object generations are retained and accepted when opening.
    """
    project_root = Path(root).expanduser().absolute()
    reject_links(project_root)
    try:
        project_root.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise ProjectError("io_error", "Project root is not a directory: " + str(project_root)) from exc
    result: dict[str, Path] = {}
    for relative in PROJECT_LAYOUT_DIRECTORIES:
        target = project_root.joinpath(*PurePosixPath(relative).parts)
        reject_links(target)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise ProjectError("io_error", "Project layout path is not a directory: " + str(target)) from exc
        if not target.is_dir():
            raise ProjectError("io_error", "Project layout path is not a directory: " + str(target))
        result[relative] = target
    return result


def project_default_directory(root: str | Path, kind: str) -> Path:
    """Return the typed, project-relative default directory for a document."""
    try:
        relative = PROJECT_TYPED_DIRECTORY_BY_KIND[kind]
    except (KeyError, TypeError) as exc:
        raise ProjectError("invalid_document", "Unsupported project resource kind: " + str(kind)) from exc
    return Path(root) / Path(*PurePosixPath(relative).parts)


def project_document_handle(root: str | Path, document_id: str, kind: str) -> Path:
    """Return a stable UUID handle, independent of a resource name/revision."""
    identity = identifier(document_id)
    if kind not in PROJECT_TYPED_DIRECTORY_BY_KIND:
        raise ProjectError("invalid_document", "Unsupported project resource kind: " + str(kind))
    return project_default_directory(root, kind) / (identity + EXTENSIONS[kind])


def legacy_document_handle(root: str | Path, document_id: str, kind: str) -> Path:
    """Return the pre-layout logical handle accepted for existing projects."""
    identity = identifier(document_id)
    if kind not in PROJECT_TYPED_DIRECTORY_BY_KIND:
        raise ProjectError("invalid_document", "Unsupported project resource kind: " + str(kind))
    return Path(root) / "documents" / (identity + EXTENSIONS[kind])


def safe_resource_filename(name: str, kind: str, *, occupied: set[str] | None = None) -> str:
    """Make a bounded portable basename; names never supply path components.

    NFKC also covers Windows superscript device aliases. Collision comparison
    uses normalized casefold on every host, including case-sensitive Linux.
    """
    if kind not in PROJECT_TYPED_DIRECTORY_BY_KIND:
        raise ProjectError("invalid_document", "Unsupported project resource kind")
    if not isinstance(name, str):
        raise ProjectError("invalid_document", "Resource name must be text")
    try:
        name.encode("utf-8")
    except UnicodeError as exc:
        raise ProjectError("invalid_document", "Resource name must be valid UTF-8 text") from exc
    value = unicodedata.normalize("NFKC", name)
    value = "".join("_" if ord(c) < 32 or ord(c) == 127 or c in '/\\<>:\"|?*' else c for c in value)
    value = value.strip(" .") or "Unnamed " + kind
    # Bound the UTF-8 filename as well as its character count. This is a file
    # safety limit, not a change to the original stored display name.
    value = value[:80]
    while len(value.encode("utf-8")) > 180:
        value = value[:-1]
    value = value.rstrip(" .") or "Unnamed " + kind
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        prefix + str(index) for prefix in ("COM", "LPT") for index in range(1, 10)}
    if value.split(".", 1)[0].rstrip(" ").upper() in devices:
        value = "_" + value
    extension = EXTENSIONS[kind]
    occupied = set() if occupied is None else occupied
    candidate, number = value + extension, 1
    while unicodedata.normalize("NFKC", candidate).casefold() in occupied:
        number += 1
        candidate = value + f" ({number})" + extension
    occupied.add(unicodedata.normalize("NFKC", candidate).casefold())
    return candidate


def managed_document_path(value: str, kind: str, identity: str) -> str:
    """Validate current named paths or retained version-1 UUID object paths."""
    from .project_dependencies import portable_path
    relative = portable_path(value)
    parts = PurePosixPath(relative).parts
    if len(parts) == 3 and parts[0] == "objects":
        identifier(parts[1])
        if parts[2] != identifier(identity) + EXTENSIONS[kind]:
            raise ProjectError("invalid_manifest", "Legacy document path differs from its identity/kind")
        return relative
    directory = PurePosixPath(PROJECT_TYPED_DIRECTORY_BY_KIND[kind]).parts
    if (len(parts) != len(directory) + 2 or parts[:len(directory)] != directory or
            re.fullmatch(r"r[0-9]{8,}(?:-[1-9][0-9]*)?", parts[-2]) is None or
            not parts[-1].endswith(EXTENSIONS[kind])):
        raise ProjectError("invalid_manifest", "Document path must be a typed named project revision")
    return relative
