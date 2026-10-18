"""Read-only discovery across one project's existing BBS and research records.

No persistence or global/project inference belongs here. The adapter must admit
the explicit descriptor before constructing this projection. Inventory cursors
make partial scans resumable without pretending a budgeted page is exhaustive.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import zip_longest
from pathlib import Path
import re
import stat
import time
from typing import Any, NoReturn

from .project_bbs import BBS_KIND
from .project_dependencies import ProjectError, identifier, json_bytes, reject_links
from .project_research import ProjectResearchStore
from .project_store import ProjectStore
from .project_paths import managed_document_path

MAX_SCAN_RECORDS = 200
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_SCAN_SECONDS = 3.0
MAX_RESULTS = 50
MAX_QUERY_CHARS = 200
MAX_EXCERPT_CHARS = 400
MAX_INVENTORY = 2000
_FATAL = frozenset({"unsafe_path", "project_identity_mismatch", "stale_project",
                    "project_root_not_authorized", "knowledge_version_changed"})


def _fail(code: str, message: str) -> NoReturn:
    raise ProjectError(code, message)


def _integer(value: Any, name: str, maximum: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("invalid_arguments", f"{name} must be an integer in [{minimum}, {maximum}]")


@dataclass(frozen=True)
class _Entry:
    source: str
    identity: str
    path: Path
    row: dict[str, Any] | None
    signature: tuple[int, ...] | None
    error: str | None = None

    @property
    def size(self) -> int:
        return self.signature[2] if self.signature else 0


def _entry(source: str, identity: str, path: Path, row: dict[str, Any] | None = None) -> _Entry:
    reject_links(path)
    try:
        info = path.lstat()
    except OSError:
        return _Entry(source, identity, path, row, None, "record_unavailable")
    if not stat.S_ISREG(info.st_mode) or (source == "research" and info.st_nlink != 1):
        _fail("unsafe_path", "Knowledge records must follow their existing regular-file storage policy")
    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return _Entry(source, identity, path, row, signature)


def _excerpt(text: str, query: str) -> dict[str, Any]:
    match = re.search(re.escape(query), text, re.IGNORECASE) if query else None
    start = max(0, match.start() - 100) if match else 0
    return {"excerpt": text[start:start + MAX_EXCERPT_CHARS], "excerpt_offset": start,
            "total_chars": len(text)}


class ProjectKnowledge:
    """A per-call projection; existing stores remain the sole state owners."""

    def __init__(self, project_file: str | Path, project_id: str):
        identifier(project_id)
        self.store = ProjectStore.open(project_file)
        if self.store.manifest["id"] != project_id:
            _fail("project_identity_mismatch", "Project ID does not match the explicit descriptor")
        self.project_id = project_id
        self.research = ProjectResearchStore(self.store.project_file, project_id, read_only=True)

    def _inventory(self) -> tuple[list[_Entry], dict[str, Any], str]:
        self.store._current()
        bbs = [_entry("bbs", row["id"], self.store.root / managed_document_path(
            row["path"], row["kind"], row["id"]), row)
            for row in sorted(self.store.manifest["documents"], key=lambda row: row["id"])
            if row["kind"] == BBS_KIND]
        sources: dict[str, Any] = {"bbs": {"state": "ready" if bbs else "empty", "available_records": len(bbs)}}
        evidence: list[_Entry] = []
        try:
            paths = self.research._paths()
            evidence = [_entry("research", path.stem, path) for path in paths]
            state = "ready" if paths else ("empty" if self.research._directory.exists() else "not_saved")
            sources["research"] = {"state": state, "available_records": len(evidence)}
        except (ProjectError, OSError) as exc:
            code = getattr(exc, "code", "research_unavailable")
            if code in _FATAL:
                raise
            sources["research"] = {"state": "unavailable", "available_records": None, "error": code}
        entries = [entry for pair in zip_longest(bbs, evidence) for entry in pair if entry is not None]
        version = hashlib.sha256(json_bytes({"project_fingerprint": self.store._fingerprint,
            "entries": [[entry.source, entry.identity, entry.signature, entry.error] for entry in entries],
            "sources": sources})).hexdigest()
        return entries, sources, version

    def _bbs_result(self, entry: _Entry, query: str, include_archived: bool) -> dict[str, Any] | None:
        assert entry.row is not None
        post = self.store._read_record(entry.row)
        if post["post_id"] != entry.identity:
            _fail("integrity_error", "BBS record identity does not match its manifest entry")
        current = post["history"][-1]
        if not include_archived and current["archived"]:
            return None
        if query.casefold() not in (current["title"] + "\n" + current["body"]).casefold():
            return None
        return {"source_type": "bbs", "id": entry.identity, "title": current["title"],
            "post_type": post["post_type"], "parent_id": post["parent_id"], "author": post["author"],
            "editor": current["editor"], "revision": current["revision"], "archived": current["archived"],
            "updated_at": current["edited_at"], "body_format": current.get("body_format", "plain"),
            "attachment_count": len(current.get("attachments", [])),
            "statement_kind": "unverified_project_note", **_excerpt(current["body"], query),
            "citation": {"project_id": self.project_id, "source_type": "bbs", "post_id": entry.identity,
                         "revision": current["revision"], "document_sha256": entry.row["sha256"]},
            "read_tool": "project_bbs_read", "read_arguments": {
                "root": str(self.store.project_file), "project_id": self.project_id, "post_id": entry.identity},
            "untrusted_reference": True}

    def _research_result(self, entry: _Entry, query: str) -> dict[str, Any] | None:
        record = self.research._load(entry.path)
        haystack = "\n".join(str(record[key] or "") for key in ("title", "doi", "source", "content"))
        if query.casefold() not in haystack.casefold():
            return None
        metadata = self.research._metadata(record)
        return {**metadata, "source_type": "research", "id": entry.identity,
            **_excerpt(record["content"], query), "segment_count": len(record["segments"]),
            "citation": {"project_id": self.project_id, "source_type": "research", "evidence_id": entry.identity,
                         "content_sha256": record["content_sha256"], "sha256": record["sha256"],
                         "doi": record["doi"], "original_url": record["original_url"]},
            "read_tool": "project_research_read", "read_arguments": {
                "root": str(self.store.project_file), "project_id": self.project_id,
                "evidence_id": entry.identity, "offset": 0, "max_chars": 6000},
            "untrusted_reference": True}

    def search(self, query: str = "", offset: int = 0, limit: int = 10,
               include_archived: bool = False, expected_version: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS or "\0" in query:
            _fail("invalid_arguments", "query must contain at most 200 text characters")
        _integer(offset, "offset", MAX_INVENTORY)
        _integer(limit, "limit", MAX_RESULTS, 1)
        if type(include_archived) is not bool:
            _fail("invalid_arguments", "include_archived must be a boolean")
        if expected_version is not None and (not isinstance(expected_version, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_version)):
            _fail("invalid_arguments", "expected_version must be a returned SHA-256 version")
        entries, sources, version = self._inventory()
        if expected_version is not None and expected_version != version:
            _fail("knowledge_version_changed", "Saved knowledge changed; restart at offset zero")
        for source in sources.values():
            source.update(scanned_records=0, matched_records=0, failed_records=0)
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        scanned, used_bytes, position = 0, 0, min(offset, len(entries))
        reason: str | None = None
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        while position < len(entries):
            if scanned >= MAX_SCAN_RECORDS:
                reason = "record_budget"
                break
            if time.monotonic() >= deadline:
                reason = "time_budget"
                break
            entry = entries[position]
            if used_bytes + entry.size > MAX_SCAN_BYTES and entry.size <= MAX_SCAN_BYTES:
                reason = "byte_budget"
                break
            position += 1
            scanned += 1
            sources[entry.source]["scanned_records"] += 1
            try:
                if entry.error:
                    _fail(entry.error, "Knowledge record is unavailable")
                if entry.size > MAX_SCAN_BYTES:
                    _fail("record_budget_exceeded", "Knowledge record exceeds the entire page byte budget")
                # Recheck before an existing bounded storage loader reads bytes.
                if _entry(entry.source, entry.identity, entry.path, entry.row) != entry:
                    _fail("knowledge_version_changed", "Knowledge record changed during discovery")
                used_bytes += entry.size
                row = (self._bbs_result(entry, query, include_archived) if entry.source == "bbs"
                       else self._research_result(entry, query))
                if row is not None:
                    results.append(row)
                    sources[entry.source]["matched_records"] += 1
            except (ProjectError, OSError) as exc:
                code = getattr(exc, "code", "record_unavailable")
                if code in _FATAL:
                    raise
                failures.append({"source_type": entry.source, "id": entry.identity, "code": code})
                sources[entry.source]["failed_records"] += 1
            if len(results) >= limit:
                reason = "result_limit" if position < len(entries) else None
                break
        if self._inventory()[2] != version:
            _fail("knowledge_version_changed", "Saved knowledge changed during discovery; restart at offset zero")
        has_more = position < len(entries)
        unavailable = any(source["state"] == "unavailable" for source in sources.values())
        return {"ok": True, "project_id": self.project_id, "project_file": str(self.store.project_file),
            "project_revision": self.store.manifest["revision"], "version": version, "results": results,
            "query": query, "offset": offset, "limit": limit, "next_offset": position if has_more else None,
            "has_more": has_more, "inventory_records": len(entries), "matches_in_page": len(results),
            "coverage": "partial" if has_more or offset > 0 or failures or unavailable else "complete",
            "sources": sources, "failures": failures, "search_mode": "literal_substring",
            "cursor_kind": "interleaved_inventory_position", "untrusted_reference": True,
            "budget": {"max_scan_records": MAX_SCAN_RECORDS, "max_scan_bytes": MAX_SCAN_BYTES,
                       "max_scan_seconds": MAX_SCAN_SECONDS, "max_excerpt_chars": MAX_EXCERPT_CHARS,
                       "scanned_records": scanned, "scanned_bytes": used_bytes,
                       "stopped_reason": reason, "exhausted": reason in {"record_budget", "byte_budget", "time_budget"}}}
