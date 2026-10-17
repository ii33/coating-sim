"""Explicit, append-only project research evidence; never executable instructions.

This sidecar is separate from design inputs and project revisions. Callers must
admit the explicit descriptor through their authorization service before use.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Literal, NoReturn, overload
from urllib.parse import urlsplit
from uuid import uuid4

from .project_dependencies import ProjectError, identifier, json_bytes, parse_json, reject_links
from .project_store import ProjectStore

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_LIBRARY_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 1000
MAX_TEXT_CHARS = 2 * 1024 * 1024
MAX_SEGMENTS = 10000
MAX_READ_CHARS = 6000
MAX_RESULTS = 50
_FIELDS = frozenset({"evidence_id", "project_id", "title", "content", "doi", "original_url",
    "source_url", "sha256", "retrieved_at", "acquired_at", "license", "parse_status", "segments",
    "source", "source_id", "id", "statement_kind", "untrusted_reference", "format", "byte_count",
    "page_count", "warning", "error", "ok", "content_sha256", "sha256_basis"})


def _fail(code: str, message: str) -> NoReturn:
    raise ProjectError(code, message)


@overload
def _text(value: Any, field: str, maximum: int, *, nullable: Literal[False] = False) -> str: ...


@overload
def _text(value: Any, field: str, maximum: int, *, nullable: Literal[True]) -> str | None: ...


def _text(value: Any, field: str, maximum: int, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        _fail("invalid_evidence", field + " must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProjectError("invalid_evidence", field + " must be valid UTF-8 text") from exc
    return value


def _integer(value: Any, field: str, maximum: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("invalid_evidence", field + " is outside its integer bounds")
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail("invalid_evidence", field + " must be lowercase SHA-256")
    return value


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize(record: Mapping[str, Any], project_id: str) -> dict[str, Any]:
    if not isinstance(record, Mapping) or set(record) - _FIELDS:
        _fail("invalid_evidence", "Unknown evidence fields")
    if record.get("project_id", project_id) != project_id:
        _fail("project_identity_mismatch", "Evidence belongs to a different project")
    if record.get("untrusted_reference", True) is not True:
        _fail("invalid_evidence", "Research evidence must remain untrusted reference data")
    kind = record.get("statement_kind", "source_statement")
    if not isinstance(kind, str) or kind not in {"source_statement", "assistant_inference"}:
        _fail("invalid_evidence", "Simulation results require the existing verified run result tools")
    identity = identifier(record.get("evidence_id", uuid4().hex))
    segments = record.get("segments", [])
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        _fail("invalid_evidence", "Evidence segments exceed the bounded list")
    normalized_segments: list[dict[str, Any]] = []
    segment_chars, segment_bytes = 0, 0
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) - {"page", "section", "paragraph", "text"}:
            _fail("invalid_evidence", "Invalid evidence segment")
        page, paragraph = segment.get("page"), segment.get("paragraph")
        if page is not None:
            _integer(page, "page", 1000000, minimum=1)
        if paragraph is not None:
            _integer(paragraph, "paragraph", 1000000, minimum=1)
        clean_segment: dict[str, Any] = {"page": page,
            "section": _text(segment.get("section"), "section", 2000, nullable=True),
            "paragraph": paragraph, "text": _text(segment.get("text"), "text", MAX_TEXT_CHARS)}
        segment_chars += len(clean_segment["text"]) + bool(normalized_segments)
        segment_bytes += len(json_bytes(clean_segment))
        if segment_chars > MAX_TEXT_CHARS or segment_bytes > MAX_RECORD_BYTES:
            _fail("capacity_exceeded", "Combined research segments exceed the record limit")
        normalized_segments.append(clean_segment)
    joined = "\n".join(segment["text"] for segment in normalized_segments)
    content = _text(record.get("content", joined), "content", MAX_TEXT_CHARS)
    if normalized_segments and content != joined:
        _fail("invalid_evidence", "Content must exactly match newline-joined source segments")
    content_hash = _digest(content.encode("utf-8"))
    if "content_sha256" in record and _hash(record["content_sha256"], "content_sha256") != content_hash:
        _fail("evidence_integrity", "Evidence text digest does not match")
    sha256 = _hash(record.get("sha256", content_hash), "sha256")
    basis = record.get("sha256_basis", "source_bytes" if "sha256" in record else "utf8_content")
    if (not isinstance(basis, str) or basis not in {"source_bytes", "utf8_content"}
            or (basis == "utf8_content" and sha256 != content_hash)):
        _fail("invalid_evidence", "Invalid source digest basis")
    original_url = record.get("original_url", record.get("source_url"))
    _text(original_url, "original_url", 4096, nullable=True)
    if original_url is not None:
        try:
            parts = urlsplit(original_url)
            valid = (parts.scheme in {"http", "https"} and parts.hostname and not parts.username
                     and not parts.password and not parts.query and not parts.fragment
                     and not any(char.isspace() or ord(char) < 32 for char in original_url))
        except ValueError:
            valid = False
        if not valid:
            _fail("invalid_evidence", "Use a public HTTP(S) source URL without credentials, query or fragment")
    if "source_url" in record and record["source_url"] != original_url:
        _fail("invalid_evidence", "Conflicting source URLs")
    retrieved = record.get("retrieved_at", record.get("acquired_at", datetime.now(timezone.utc).isoformat()))
    _text(retrieved, "retrieved_at", 80)
    try:
        timestamp = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
        if timestamp.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise ProjectError("invalid_evidence", "retrieved_at requires an ISO timestamp with timezone") from exc
    if "acquired_at" in record and record["acquired_at"] != retrieved:
        _fail("invalid_evidence", "Conflicting acquisition timestamps")
    status = record.get("parse_status", "not_parsed")
    if not isinstance(status, str) or status not in {"not_parsed", "parsed", "no_text", "failed", "metadata_only"}:
        _fail("invalid_evidence", "Unknown parsing status")
    doi = _text(record.get("doi"), "doi", 500, nullable=True)
    if doi is not None and (not re.fullmatch(r"(?:https://doi\.org/)?10\.\d{4,9}/\S+", doi, re.I)):
        _fail("invalid_evidence", "Invalid DOI syntax; bibliographic matches are not DOI verification")
    source_id = _text(record.get("source_id", record.get("id")), "source_id", 500, nullable=True)
    if "id" in record and record["id"] != source_id:
        _fail("invalid_evidence", "Conflicting upstream source identities")
    result = {"evidence_id": identity, "project_id": project_id,
        "title": _text(record.get("title", ""), "title", 500), "content": content,
        "doi": doi, "original_url": original_url, "sha256": sha256, "sha256_basis": basis,
        "content_sha256": content_hash, "retrieved_at": retrieved,
        "license": _text(record.get("license"), "license", 2000, nullable=True),
        "parse_status": status, "segments": normalized_segments,
        "source": _text(record.get("source", ""), "source", 160),
        "source_id": source_id,
        "statement_kind": kind, "untrusted_reference": True}
    for field, maximum in (("format", 40), ("warning", 1000), ("error", 160)):
        if field in record:
            result[field] = _text(record[field], field, maximum, nullable=True)
    for field, maximum in (("byte_count", 16 * 1024 * 1024), ("page_count", 1000000)):
        if field in record:
            result[field] = None if record[field] is None else _integer(record[field], field, maximum)
    if "ok" in record and type(record["ok"]) is not bool:
        _fail("invalid_evidence", "ok must be boolean")
    if "ok" in record:
        result["ok"] = record["ok"]
    if len(json_bytes(result)) > MAX_RECORD_BYTES:
        _fail("capacity_exceeded", "Evidence record exceeds its size limit")
    return result


def _ordinary(path: Path) -> os.stat_result:
    reject_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail("unsafe_path", "Research files must be ordinary unlinked files")
    return info


def _signature(path: Path) -> tuple[int, int]:
    reject_links(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        _fail("unsafe_path", "Research storage ancestors must be ordinary directories")
    return info.st_dev, info.st_ino


def _publish(path: Path, raw: bytes) -> None:
    """Publish a fully flushed immutable file atomically without any replacement."""
    fd, name = tempfile.mkstemp(prefix=".evidence-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        reject_links(path)
        # Unlike replace(), link() refuses an existing destination on all hosts.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


class ProjectResearchStore:
    """Bound one descriptor and persisted project ID; no current-project lookup.

    Append-only records do not alter the .coatproj revision. Authorization of
    the project root remains the caller's responsibility, not a model argument.
    """

    def __init__(self, project_file: str | Path, project_id: str, *, read_only: bool = False):
        identifier(project_id)
        if type(read_only) is not bool:
            _fail("invalid_arguments", "read_only must be an application boolean")
        store = ProjectStore.open(project_file)
        if store.manifest["id"] != project_id:
            _fail("project_identity_mismatch", "Project ID does not match the explicit descriptor")
        self.project_file, self.project_id = store.project_file, project_id
        self._root, self._read_only = store.root, read_only
        self._directory = store.root / "data" / "research" / project_id
        self._root_identity = _signature(store.root)

    def _validate(self) -> None:
        if _signature(self._root) != self._root_identity:
            _fail("project_identity_mismatch", "Project directory has been replaced")
        current = ProjectStore.open(self.project_file)
        if current.manifest["id"] != self.project_id:
            _fail("project_identity_mismatch", "Project descriptor identity changed")
        reject_links(self._directory)
        for path in (self._root / "data", self._root / "data" / "research", self._directory):
            if path.exists():
                _signature(path)

    def _paths(self) -> list[Path]:
        self._validate()
        if not self._directory.exists():
            return []
        paths = []
        with os.scandir(self._directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_RECORDS + 10:
                    _fail("capacity_exceeded", "Research directory exceeds its entry limit")
                if entry.name.startswith("."):
                    continue
                if not re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                    _fail("unsafe_path", "Unexpected research storage entry")
                paths.append(Path(entry.path))
        if len(paths) > MAX_RECORDS:
            _fail("capacity_exceeded", "Research library exceeds its record limit")
        return sorted(paths)

    def _load(self, path: Path) -> dict[str, Any]:
        info = _ordinary(path)
        if info.st_size > MAX_RECORD_BYTES + 1024:
            _fail("capacity_exceeded", "Research record exceeds its size limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
                _fail("unsafe_path", "Research file changed while opening")
            raw = stream.read(MAX_RECORD_BYTES + 1025)
        if len(raw) > MAX_RECORD_BYTES + 1024:
            _fail("capacity_exceeded", "Research record exceeds its size limit")
        envelope = parse_json(raw)
        if (not isinstance(envelope, dict) or set(envelope) != {"kind", "version", "record", "record_sha256"}
                or envelope["kind"] != "coat_sim.project_research" or type(envelope["version"]) is not int
                or envelope["version"] != 1 or not isinstance(envelope["record"], dict)):
            _fail("invalid_evidence", "Unsupported research record format")
        record = envelope["record"]
        if envelope["record_sha256"] != _digest(json_bytes(record)):
            _fail("evidence_integrity", "Research record digest does not match")
        normalized = _normalize(record, self.project_id)
        if record != normalized or record["evidence_id"] != path.stem:
            _fail("evidence_integrity", "Research record identity or canonical form does not match")
        return record

    @contextmanager
    def _writer(self):
        self._validate()
        for path in (self._root / "data", self._root / "data" / "research", self._directory):
            reject_links(path)
            path.mkdir(exist_ok=True)
            _signature(path)
        identity = _signature(self._directory)
        lock = self._directory / ".write.lock"
        reject_links(lock)
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProjectError("research_locked", "Research library has an active or interrupted writer") from exc
        owned = os.fstat(fd)
        try:
            self._validate()
            if _signature(self._directory) != identity:
                _fail("unsafe_path", "Research directory changed while locking")
            yield identity
        finally:
            os.close(fd)
            reject_links(self._directory)
            if _signature(self._directory) == identity:
                try:
                    current = lock.lstat()
                    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                        lock.unlink()
                except FileNotFoundError:
                    pass

    def save(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Explicitly append one record; duplicate IDs never overwrite evidence."""
        if self._read_only:
            _fail("research_read_only", "This research store does not permit writes")
        normalized = _normalize(record, self.project_id)
        envelope = {"kind": "coat_sim.project_research", "version": 1, "record": normalized,
                    "record_sha256": _digest(json_bytes(normalized))}
        raw = json_bytes(envelope)
        with self._writer() as identity:
            paths = self._paths()
            if len(paths) >= MAX_RECORDS or sum(_ordinary(path).st_size for path in paths) + len(raw) > MAX_LIBRARY_BYTES:
                _fail("capacity_exceeded", "Research library capacity exceeded")
            destination = self._directory / (normalized["evidence_id"] + ".json")
            if _signature(self._directory) != identity:
                _fail("unsafe_path", "Research directory changed before publication")
            self._validate()
            try:
                _publish(destination, raw)
            except FileExistsError as exc:
                raise ProjectError("evidence_exists", "Evidence ID already exists; append a new record") from exc
        return deepcopy(normalized)

    @staticmethod
    def _metadata(record: dict[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in record.items() if key not in {"content", "segments"}}

    def list(self, query: str = "", offset: int = 0, limit: int = 10) -> dict[str, Any]:
        """List bounded summaries from this project only, without creating files."""
        _text(query, "query", 240)
        _integer(offset, "offset", MAX_RECORDS)
        _integer(limit, "limit", MAX_RESULTS, minimum=1)
        records = []
        total_bytes = 0
        for path in self._paths():
            total_bytes += _ordinary(path).st_size
            if total_bytes > MAX_LIBRARY_BYTES:
                _fail("capacity_exceeded", "Research library exceeds its byte limit")
            record = self._load(path)
            haystack = "\n".join(str(record[key] or "") for key in ("title", "doi", "source", "content"))
            if query.casefold() in haystack.casefold():
                records.append(record)
        records.sort(key=lambda row: (row["retrieved_at"], row["evidence_id"]), reverse=True)
        results = [{**self._metadata(row), "excerpt": row["content"][:500],
                    "segment_count": len(row["segments"])} for row in records[offset:offset + limit]]
        end = offset + len(results)
        return {"ok": True, "project_id": self.project_id, "results": results, "total": len(records),
                "offset": offset, "next_offset": end if end < len(records) else None,
                "has_more": end < len(records), "untrusted_reference": True}

    def read(self, evidence_id: str, offset: int = 0, max_chars: int = MAX_READ_CHARS) -> dict[str, Any]:
        """Read a character page; fragment provenance retains physical source locations."""
        identifier(evidence_id)
        _integer(offset, "offset", MAX_TEXT_CHARS)
        _integer(max_chars, "max_chars", MAX_READ_CHARS, minimum=1)
        self._validate()
        path = self._directory / (evidence_id + ".json")
        try:
            record = self._load(path)
        except FileNotFoundError as exc:
            raise ProjectError("evidence_not_found", "Evidence is absent from this project") from exc
        end = min(len(record["content"]), offset + max_chars)
        fragments = []
        start = 0
        for segment in record["segments"]:
            stop = start + len(segment["text"])
            if start < end and stop > offset:
                lo, hi = max(offset, start) - start, min(end, stop) - start
                fragments.append({**segment, "text": segment["text"][lo:hi], "segment_offset": lo})
            start = stop + 1
        return {"ok": True, "project_id": self.project_id, "record": self._metadata(record),
                "text": record["content"][offset:end], "segments": fragments, "offset": offset,
                "next_offset": end if end < len(record["content"]) else None,
                "has_more": end < len(record["content"]), "untrusted_reference": True}
