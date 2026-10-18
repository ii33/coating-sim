"""Bounded, offline-only access to the shipped reference-source metadata.

This catalogue describes source capabilities and evidence, not installed data.
It does not fetch URLs, read vendor files, infer optical constants, or create
storage. User-supplied paths and catalogue replacements are not public inputs.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, NoReturn
from urllib.parse import urlsplit

from .project_dependencies import ProjectError

REFERENCE_SOURCES_PATH = Path(__file__).resolve().with_name("resources") / "reference_sources.json"
MAX_SOURCE_BYTES = 256 * 1024
MAX_SOURCES = 50
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 12
MAX_RESULTS = 50
CATEGORIES = frozenset({"optical_constants", "thermochemistry", "process", "colorimetry",
                        "substrates", "theoretical", "handbook"})
OFFLINE_STATUSES = frozenset({"open_license", "official_download", "reference_only", "access_required"})
RIGHTS_STATUSES = frozenset({"allowed", "conditional", "unknown", "restricted"})
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
_SUMMARY_FIELDS = ("id", "title", "organization", "category", "priority", "data_kind", "domains",
                   "summary", "offline_status", "access", "rights", "reviewed_at")


def _fail(code: str, message: str) -> NoReturn:
    raise ProjectError(code, message)


def _validate_version(value: Any) -> None:
    if value is not None and (not isinstance(value, str)
                              or re.fullmatch(r"[0-9a-f]{64}", value) is None):
        _fail("invalid_arguments", "expected_version must be a returned SHA-256 version")


def _safe_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or any(
            char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and bool(parts.hostname)
                and not parts.username and not parts.password)
    except ValueError:
        return False


def _bounded_json(value: Any, depth: int = 0) -> None:
    """Reject pathological or accidental payloads before exposing metadata."""
    if depth > 8:
        raise ValueError("metadata nesting exceeds limit")
    if isinstance(value, str):
        if len(value) > 3000 or any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("invalid metadata text")
    elif type(value) is dict:
        if len(value) > 40 or any(not isinstance(key, str) for key in value):
            raise ValueError("invalid metadata object")
        for key, item in value.items():
            _bounded_json(key, depth + 1)
            _bounded_json(item, depth + 1)
    elif type(value) is list:
        if len(value) > MAX_SOURCES:
            raise ValueError("metadata list exceeds limit")
        for item in value:
            _bounded_json(item, depth + 1)
    elif value is not None and type(value) not in (bool, int):
        raise ValueError("unsupported metadata value")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_catalog(data: Any) -> None:
    _bounded_json(data)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("invalid schema version")
    if not isinstance(data.get("catalog_version"), str) or not data["catalog_version"]:
        raise ValueError("missing catalogue version")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data.get("reviewed_at", ""))):
        raise ValueError("invalid review date")
    sources = data.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError("invalid source count")
    seen: set[str] = set()
    for row in sources:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("invalid source identity")
        if not _ID.fullmatch(row["id"]) or row["id"] in seen:
            raise ValueError("invalid or duplicate source identity")
        seen.add(row["id"])
        if any(key not in row for key in _SUMMARY_FIELDS):
            raise ValueError("missing summary field")
        if row["category"] not in CATEGORIES or row["offline_status"] not in OFFLINE_STATUSES:
            raise ValueError("invalid category or offline capability")
        if type(row["priority"]) is not int or not 1 <= row["priority"] <= 3:
            raise ValueError("invalid priority")
        for key in ("title", "organization", "summary", "reviewed_at"):
            if not isinstance(row[key], str) or not row[key]:
                raise ValueError("missing source text")
        for key in ("data_kind", "domains", "limitations", "unknowns", "files", "links"):
            if not isinstance(row.get(key), list):
                raise ValueError("missing source list")
        access, rights = row["access"], row["rights"]
        if not isinstance(access, dict) or not isinstance(rights, dict):
            raise ValueError("missing access or rights")
        for key in ("online_view", "download_available"):
            if type(access.get(key)) is not bool:
                raise ValueError("invalid access flag")
        if access.get("login") not in {"not_required", "required", "route_dependent", "unknown"}:
            raise ValueError("invalid login status")
        if access.get("payment") not in {"free", "paid_or_institutional", "route_dependent", "unknown"}:
            raise ValueError("invalid payment status")
        if not isinstance(access.get("formats"), list) or not isinstance(access.get("api"), list):
            raise ValueError("invalid formats or API list")
        for api in access["api"]:
            if not isinstance(api, dict) or not _safe_url(api.get("url")):
                raise ValueError("invalid API URL")
        for key in ("offline_use", "redistribution"):
            if rights.get(key) not in RIGHTS_STATUSES:
                raise ValueError("invalid rights status")
        if not isinstance(rights.get("license"), str) or not isinstance(rights.get("notes"), list):
            raise ValueError("invalid rights description")
        evidence = rights.get("evidence_urls")
        if not isinstance(evidence, list) or not evidence or not all(_safe_url(url) for url in evidence):
            raise ValueError("missing or unsafe rights evidence")
        for link in row["links"]:
            if not isinstance(link, dict) or not _safe_url(link.get("url")):
                raise ValueError("invalid reference URL")
        for file in row["files"]:
            if not isinstance(file, dict) or not _safe_url(file.get("url")):
                raise ValueError("invalid download URL")
            if file.get("bytes") is not None and (type(file["bytes"]) is not int or file["bytes"] < 0):
                raise ValueError("invalid file size")
        integration = row.get("integration")
        if not isinstance(integration, dict) or integration.get("bundled_numeric_data") is not False:
            raise ValueError("reference catalogue cannot claim to bundle numerical data")
        if integration.get("host_installation") != "not_checked":
            raise ValueError("static reference cannot assert host installation")


def _load() -> tuple[dict[str, Any], str]:
    """Read one fixed regular resource; no cache or user storage is involved."""
    path = REFERENCE_SOURCES_PATH
    try:
        if path.parent.is_symlink() or path.is_symlink():
            _fail("reference_sources_invalid", "Reference catalogue must not be a symbolic link")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SOURCE_BYTES:
                _fail("reference_sources_invalid", "Reference catalogue is not a bounded regular file")
            raw = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise ProjectError("reference_sources_unavailable", "Reference catalogue is unavailable") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        _fail("reference_sources_invalid", "Reference catalogue exceeds its byte limit")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        _validate_catalog(data)
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ProjectError("reference_sources_invalid", "Reference catalogue is invalid") from exc
    return data, hashlib.sha256(raw).hexdigest()


def _metadata(data: dict[str, Any], version: str, expected_version: str | None) -> dict[str, Any]:
    if expected_version is not None and expected_version != version:
        _fail("reference_sources_changed", "Reference catalogue changed; restart the query")
    return {"schema_version": 1, "catalog_version": data["catalog_version"], "version": version,
            "reviewed_at": data["reviewed_at"], "metadata_only": True,
            "host_installation": "not_checked", "untrusted_reference": True}


def reference_sources_catalog(*, query: str = "", category: str | None = None,
                              offline_status: str | None = None, offset: int = 0,
                              limit: int = 20, expected_version: str | None = None) -> dict[str, Any]:
    """List evidence-backed source metadata; all filters are literal and bounded."""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS or any(ord(c) < 32 for c in query):
        _fail("invalid_arguments", "query must be at most 200 characters without control characters")
    terms = query.casefold().split()
    if len(terms) > MAX_QUERY_TERMS:
        _fail("invalid_arguments", "query must contain at most 12 terms")
    for value, choices, name in ((category, CATEGORIES, "category"),
                                 (offline_status, OFFLINE_STATUSES, "offline_status")):
        if value is not None and (not isinstance(value, str) or value not in choices):
            _fail("invalid_arguments", f"Unknown {name}")
    if type(offset) is not int or not 0 <= offset <= 10000:
        _fail("invalid_arguments", "offset must be an integer in [0, 10000]")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        _fail("invalid_arguments", "limit must be an integer in [1, 50]")
    _validate_version(expected_version)
    data, version = _load()
    result = _metadata(data, version, expected_version)
    rows = [row for row in data["sources"]
            if (category is None or row["category"] == category)
            and (offline_status is None or row["offline_status"] == offline_status)
            and all(term in json.dumps(row, ensure_ascii=False).casefold() for term in terms)]
    end = offset + limit
    result.update(total=len(rows), offset=offset, limit=limit,
                  next_offset=end if end < len(rows) else None,
                  items=[{key: deepcopy(row[key]) for key in _SUMMARY_FIELDS}
                         for row in rows[offset:end]])
    return result


def reference_sources_detail(source_id: str, *, expected_version: str | None = None) -> dict[str, Any]:
    """Read one source by a safe catalogue ID, never by a path or remote URL."""
    if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
        _fail("invalid_arguments", "source_id must be a safe returned catalogue ID")
    _validate_version(expected_version)
    data, version = _load()
    result = _metadata(data, version, expected_version)
    for row in data["sources"]:
        if row["id"] == source_id:
            result["source"] = deepcopy(row)
            return result
    _fail("reference_source_not_found", "Reference source was not found")
