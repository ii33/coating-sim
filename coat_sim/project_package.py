"""Portable, validated saved-input project archives; independent of the GUI."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
import zipfile

from coat_sim.core.project_binary_assets import (
    DEFAULT_ASSET_LIMITS, capture_asset, file_signature, normalize_binary_asset, stream_asset,
)
from coat_sim.core.project_dependencies import (
    MAX_FILE_BYTES, MAX_PROJECT_BYTES, MAX_PROJECT_DOCUMENTS, ProjectError,
    json_bytes, normalize_document, parse_json, reject_links,
)
from coat_sim.core.project_store import (
    MANIFEST_FILENAME, ProjectStore, _PreparedProject, _manifest, _rename_directory_no_replace,
)

PROJECT_PACKAGE_SUFFIX = ".cproject"


def _package_path(path: str | Path) -> Path:
    result = Path(os.path.abspath(Path(path).expanduser()))
    reject_links(result)
    if result.suffix.casefold() != PROJECT_PACKAGE_SUFFIX:
        raise ProjectError("invalid_package", "Project archives must use the .cproject extension")
    return result


def _target(path: str | Path) -> Path:
    result = Path(os.path.abspath(Path(path).expanduser()))
    reject_links(result)
    if result.exists():
        raise ProjectError("destination_exists", "Choose a new destination; existing files are never replaced")
    return result


def export_project(store: ProjectStore, destination: str | Path, *, publication_scope=None) -> dict:
    """Export precisely saved JSON and explicitly referenced owned input bytes."""
    destination = _target(_package_path(destination))
    snapshot = store.snapshot_saved_inputs(verify_assets=False)
    manifest = snapshot["manifest"]
    contents = {}
    for row in manifest["documents"]:
        content = json_bytes(snapshot["documents"][row["id"]])
        row["sha256"] = hashlib.sha256(content).hexdigest()
        contents[row["path"]] = content
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_links(destination.parent)
    fd, temporary = tempfile.mkstemp(prefix=".project-package-", dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_FILENAME, json_bytes(manifest))
            for name, content in contents.items():
                archive.writestr(name, content)
            for identity, source in snapshot.get("assets", {}).items():
                metadata = snapshot["documents"][identity]
                with archive.open(metadata["payload_path"], "w") as output:
                    stream_asset(source, metadata, output)
        with open(temporary, "rb+") as stream:
            os.fsync(stream.fileno())
        with publication_scope() if publication_scope is not None else nullcontext():
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
    except FileExistsError as exc:
        raise ProjectError("destination_exists", "Package destination already exists") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"destination": str(destination), "project_id": manifest["id"],
            "revision": manifest["revision"], "document_count": len(contents),
            "asset_count": len(snapshot.get("assets", {}))}


def read_project_package(package: str | Path, *, asset_limits=None) -> dict:
    """Validate a closed whitelist without extracting or creating a project.

    Binary sources are exact-member capabilities. They are streamed for outer
    digest validation here; their role-specific parser runs on the selected
    private copy before any project publication.
    """
    package = _package_path(package)
    limits = DEFAULT_ASSET_LIMITS if asset_limits is None else asset_limits
    try:
        if not stat.S_ISREG(package.lstat().st_mode):
            raise ProjectError("invalid_package", "Project package must be an ordinary archive file")
        before = file_signature(package)
    except OSError as exc:
        raise ProjectError("invalid_package", "Project package is missing or unreadable") from exc
    except ProjectError as exc:
        if exc.code == "unsafe_path":
            raise
        raise ProjectError("invalid_package", "Project package is missing or unreadable") from exc
    if before[3] > MAX_PROJECT_BYTES + MAX_FILE_BYTES + limits.max_total_bytes:
        raise ProjectError("capacity_exceeded", "Project package exceeds configured input capacity")
    try:
        with zipfile.ZipFile(package) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_PROJECT_DOCUMENTS * 2 + 1:
                raise ProjectError("capacity_exceeded", "Too many project package entries")
            names = [entry.filename for entry in entries]
            if len(set(name.casefold() for name in names)) != len(names):
                raise ProjectError("invalid_package", "Duplicate package entries")
            total = 0
            for entry in entries:
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.flag_bits & 1 or entry.is_dir() or mode not in (0, stat.S_IFREG) or
                        entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ProjectError("invalid_package", "Only ordinary unencrypted input files are permitted")
                total += entry.file_size
                if entry.file_size > max(MAX_FILE_BYTES, 512 * 1024 * 1024) or total > MAX_PROJECT_BYTES + MAX_FILE_BYTES + limits.max_total_bytes:
                    raise ProjectError("capacity_exceeded", "Project package expands beyond configured input capacity")
            if MANIFEST_FILENAME not in names:
                raise ProjectError("invalid_package", "Project manifest is missing")
            if archive.getinfo(MANIFEST_FILENAME).file_size > MAX_FILE_BYTES:
                raise ProjectError("capacity_exceeded", "Project manifest exceeds JSON capacity")
            manifest = _manifest(parse_json(archive.read(MANIFEST_FILENAME)))
            documents, assets = {}, {}
            expected = {MANIFEST_FILENAME, *(row["path"] for row in manifest["documents"])}
            json_total, asset_total = 0, 0
            for row in manifest["documents"]:
                if row["path"] not in names:
                    raise ProjectError("invalid_package", "Package managed JSON is missing")
                info = archive.getinfo(row["path"])
                if info.file_size > MAX_FILE_BYTES:
                    raise ProjectError("capacity_exceeded", "Project JSON member exceeds size limit")
                json_total += info.file_size
                if json_total > MAX_PROJECT_BYTES:
                    raise ProjectError("capacity_exceeded", "Package JSON exceeds project input capacity")
                content = archive.read(info)
                if hashlib.sha256(content).hexdigest() != row["sha256"]:
                    raise ProjectError("integrity_error", "Package input digest does not match")
                value = parse_json(content)
                normalized = normalize_document(row["kind"], value)
                if normalized != value:
                    raise ProjectError("invalid_document", "Package managed JSON is not normalized")
                documents[row["id"]] = normalized
                row["sha256"] = hashlib.sha256(json_bytes(normalized)).hexdigest()
                if row["kind"] == "binary_asset":
                    metadata = normalize_binary_asset(normalized)
                    member = metadata["payload_path"]
                    if member in expected or member not in names or archive.getinfo(member).file_size != metadata["size"]:
                        raise ProjectError("invalid_package", "Binary input member is missing, duplicated or has the wrong declared size")
                    expected.add(member)
                    asset_total += metadata["size"]
                    limits.check_total(asset_total)
                    assets[row["id"]] = {"package": str(package), "member": member}
            if set(names) != expected:
                raise ProjectError("invalid_package", "Package must contain exactly managed JSON and declared binary inputs")
        prepared = _PreparedProject(Path(tempfile.gettempdir()), manifest, "", documents, assets)
        prepared.asset_limits = limits
        result = prepared.snapshot_saved_inputs()
        if file_signature(package) != before:
            raise ProjectError("stale_source", "Project package changed during validation")
        return result
    except ProjectError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, KeyError) as exc:
        raise ProjectError("invalid_package", "Unreadable project package") from exc


def import_project(package: str | Path, destination: str | Path, *, asset_limits=None) -> ProjectStore:
    """Validate fully and exclusively publish an independent project copy."""
    destination = _target(destination)
    snapshot = read_project_package(package, asset_limits=asset_limits)
    manifest = deepcopy(snapshot["manifest"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_links(destination.parent)
    staging = Path(tempfile.mkdtemp(prefix=".project-package-", dir=destination.parent))
    root = staging / "project"
    try:
        ProjectStore.create(root, manifest["name"], asset_limits=asset_limits)
        for row in manifest["documents"]:
            target = root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(json_bytes(snapshot["documents"][row["id"]]))
        for identity, source in snapshot.get("assets", {}).items():
            metadata = snapshot["documents"][identity]
            target = root / metadata["payload_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            capture_asset(source, metadata, target)
        manifest["id"] = uuid.uuid4().hex
        (root / MANIFEST_FILENAME).write_bytes(json_bytes(manifest))
        validated = ProjectStore.open(root / MANIFEST_FILENAME, asset_limits=asset_limits)
        validated.snapshot_saved_inputs()
        _rename_directory_no_replace(root, destination)
        return ProjectStore.open(destination / MANIFEST_FILENAME, asset_limits=asset_limits)
    except FileExistsError as exc:
        raise ProjectError("destination_exists", "Project destination appeared during import") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
