"""Project-owned BBS attachments; no preview API accepts a filesystem path.

Adapters authorize import/export paths before calling these functions. Model-facing
reads use only a managed attachment descriptor and an explicitly bound store.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import io
import os
from pathlib import Path
import re
from typing import Any
import uuid

from .project_binary_assets import (
    MAX_BBS_ATTACHMENT_BYTES, file_signature, normalize_binary_asset,
    open_asset_source, stream_asset,
)
from .project_dependencies import ProjectError, identifier, portable_path, reject_links

MAX_ATTACHMENTS = 8
MAX_PREVIEW_TEXT_BYTES = 64 * 1024
MAX_ATTACHMENT_NAME_CHARS = 200
MIME_TYPES = frozenset({"application/octet-stream", "application/pdf", "text/plain", "text/csv",
                        "image/png", "image/jpeg", "image/gif", "image/webp"})
_IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".gif": "image/gif", ".webp": "image/webp"}
_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".toml",
                            ".ini", ".cfg", ".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp",
                            ".hpp", ".rs", ".go", ".java", ".sh", ".bat", ".ps1", ".qml", ".css",
                            ".xml", ".sql", ".r", ".m", ".csv", ".tsv"})


def normalize_attachment(value: Any) -> dict:
    fields = {"asset_id", "name", "mime", "size", "sha256"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProjectError("bbs_invalid_attachment", "Invalid attachment fields")
    identifier(value["asset_id"])
    name = portable_path(value["name"])
    if "/" in name or len(name) > MAX_ATTACHMENT_NAME_CHARS:
        raise ProjectError("bbs_invalid_attachment", "Attachment name must be a basename up to 200 characters")
    if not isinstance(value["mime"], str) or value["mime"] not in MIME_TYPES:
        raise ProjectError("bbs_invalid_attachment", "Attachment MIME type is not supported")
    if type(value["size"]) is not int or not 0 < value["size"] <= MAX_BBS_ATTACHMENT_BYTES:
        raise ProjectError("capacity_exceeded", "Attachment must contain 1–32 MiB of bytes")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ProjectError("bbs_invalid_attachment", "Attachment digest must be lowercase SHA-256")
    return dict(value)


def normalize_attachments(values: Any) -> list[dict]:
    if not isinstance(values, list) or len(values) > MAX_ATTACHMENTS:
        raise ProjectError("capacity_exceeded", "A BBS revision supports at most 8 attachments")
    result = [normalize_attachment(value) for value in values]
    if len({row["asset_id"] for row in result}) != len(result):
        raise ProjectError("bbs_invalid_attachment", "A revision cannot repeat an attachment identity")
    return result


def _signature_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


def detect_attachment_mime(name: str, raw: bytes) -> str:
    """Allowlist preview types; opaque formats are never sniffed into HTML/ZIP."""
    suffix = Path(name).suffix.casefold()
    actual = _signature_mime(raw)
    expected = _IMAGE_SUFFIXES.get(suffix, "application/pdf" if suffix == ".pdf" else None)
    if expected is not None:
        if actual != expected:
            raise ProjectError("bbs_mime_mismatch", "Attachment extension does not match its signature")
        return expected
    if suffix in _TEXT_SUFFIXES:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ProjectError("bbs_mime_mismatch", "Text attachments must be valid UTF-8") from exc
        if any(ord(char) < 32 and char not in "\t\n\r\f" for char in text):
            raise ProjectError("bbs_mime_mismatch", "Text attachment contains binary control characters")
        if actual != "application/octet-stream":
            raise ProjectError("bbs_mime_mismatch", "Text attachment contains a binary signature")
        return "text/csv" if suffix == ".csv" else "text/plain"
    return "application/octet-stream"


def validate_attachment_asset(attachment: Any, records: Mapping, data: Mapping) -> dict:
    value = normalize_attachment(attachment)
    identity = value["asset_id"]
    row, document = records.get(identity), data.get(identity)
    if row is None or row["kind"] != "binary_asset" or document is None:
        raise ProjectError("missing_dependency", "Attachment is absent from this project")
    metadata = normalize_binary_asset(document)
    if metadata["role"] != "bbs_attachment":
        raise ProjectError("dependency_conflict", "Attachment does not reference a BBS binary asset")
    if metadata["size"] != value["size"] or metadata["sha256"] != value["sha256"]:
        raise ProjectError("integrity_error", "Attachment reference differs from the owned binary digest/size")
    if metadata["name"] != value["name"] or metadata["mime"] != value["mime"]:
        raise ProjectError("bbs_invalid_attachment", "Attachment name/MIME differs from its owned metadata")
    return metadata


def import_attachment(store, authorized_path: str | Path) -> dict:
    """Trusted path adapter only: copy an authorized file into one project."""
    path = Path(authorized_path)
    # Reject URI-looking strings even if a caller skipped its URL conversion.
    if "://" in str(authorized_path):
        raise ProjectError("unsafe_path", "Attachment import requires an authorized local file")
    signature = file_signature(path)
    if not 0 < signature[3] <= MAX_BBS_ATTACHMENT_BYTES:
        raise ProjectError("capacity_exceeded", "Attachment must contain 1–32 MiB of bytes")
    with open_asset_source({"path": str(path)}) as stream:
        raw = stream.read(MAX_BBS_ATTACHMENT_BYTES + 1)
    if len(raw) != signature[3] or file_signature(path) != signature:
        raise ProjectError("stale_source", "Attachment changed while being inspected")
    if len(raw) > MAX_BBS_ATTACHMENT_BYTES:
        raise ProjectError("capacity_exceeded", "Attachment exceeds 32 MiB")
    identity = uuid.uuid4().hex
    attachment = normalize_attachment({"asset_id": identity, "name": path.name,
        "mime": detect_attachment_mime(path.name, raw), "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()})
    metadata = normalize_binary_asset({"kind": "coat_sim.project_binary_asset", "version": 1,
        "role": "bbs_attachment", "payload_path": f"assets/{uuid.uuid4().hex}/{identity}.bbsasset",
        "size": attachment["size"], "sha256": attachment["sha256"],
        "name": attachment["name"], "mime": attachment["mime"]})
    with store._operation_scope():
        records, data = store._read_all()
        if identity in records:
            raise ProjectError("dependency_conflict", "Generated attachment identity collided")
        records[identity] = {"id": identity, "kind": "binary_asset", "dependencies": [], "path": "", "sha256": ""}
        data[identity] = metadata
        # Publication re-hashes the external source, so replacing it after sniffing
        # cannot publish different bytes under the verified preview MIME.
        store._publish(records, data, assets={identity: {"path": str(path)}})
    return attachment


def read_attachment(store, attachment: Any) -> tuple[dict, bytes]:
    """Resolve and hash owned bytes; path, URL, and other-project input are rejected."""
    with store._operation_scope():
        records, data = store._read_all()
        value = normalize_attachment(attachment)
        metadata = validate_attachment_asset(value, records, data)
        source = store.asset_sources(records, data)[value["asset_id"]]
        output = io.BytesIO()
        stream_asset(source, metadata, output)
        raw = output.getvalue()
        if detect_attachment_mime(value["name"], raw) != value["mime"]:
            raise ProjectError("bbs_mime_mismatch", "Attachment preview MIME differs from the verified bytes")
        store._current()
        return value, raw


def preview_attachment(store, attachment: Any) -> dict:
    """Bounded text or internal decoder input, never an external file/HTTP URL.

    ``bytes`` is private decoder input for the GUI adapter; do not publish this
    internal result as a model response. GUI strips it before emitting a result.
    """
    value, raw = read_attachment(store, attachment)
    result = {"name": value["name"], "mime": value["mime"], "kind": "unsupported", "text": "",
              "truncated": False, "message": "此格式不提供内嵌预览，可导出后打开。"}
    if value["mime"].startswith("text/"):
        text = raw[:MAX_PREVIEW_TEXT_BYTES].decode("utf-8-sig", errors="ignore")
        result.update(kind="text", text=text, truncated=len(raw) > MAX_PREVIEW_TEXT_BYTES, message="")
    elif value["mime"].startswith("image/") or value["mime"] == "application/pdf":
        result.update(kind="image" if value["mime"].startswith("image/") else "pdf", bytes=raw, message="")
    return result


def export_attachment(store, attachment: Any, authorized_destination: str | Path) -> None:
    """Explicit save-dialog/authorized-path export; never overwrite or follow links."""
    _, raw = read_attachment(store, attachment)
    destination = Path(authorized_destination)
    if "://" in str(authorized_destination):
        raise ProjectError("unsafe_path", "Attachment export requires an authorized local file")
    reject_links(destination)
    from coat_sim.managed_paths import assert_not_managed_project_path
    try:
        assert_not_managed_project_path(destination)
    except ValueError as exc:
        raise ProjectError("unsafe_path", "Attachment export cannot write into managed project storage") from exc
    try:
        with destination.open("xb") as stream:
            try:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
    except FileExistsError as exc:
        raise ProjectError("target_exists", "Destination already exists; choose a new filename") from exc
    except OSError as exc:
        raise ProjectError("io_error", "Cannot export attachment") from exc
