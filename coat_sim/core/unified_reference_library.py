"""One bounded, read-only discovery surface over existing offline owners.

No downloads, material writes, user-directory scans or persistent index. Source
records remain distinct: metadata availability is never full-text availability.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from .project_dependencies import ProjectError

_DEFAULT = object()
MAX_RESULTS = 10
MAX_READ_CHARS = 6000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _integer(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ProjectError("invalid_arguments", f"{name} must be an integer in [{low}, {high}]")
    return value


class UnifiedReferenceLibrary:
    """Host-selected public references; legacy private mirrors stay separately gated.

    One instance pins the existing owners for one call/request. The optional
    research provider is host supplied, never a model-specified root or URL.
    """

    source_kind = "builtin_public"

    def __init__(self, research: Any = _DEFAULT) -> None:
        from coat_sim.agent.research_library import BuiltinResearchLibrary
        from .materials import builtin_material_profiles
        from .reference_sources import reference_sources_catalog
        from .refractiveindex_local import RefractiveIndexLocalStore, default_store_root, packaged_store_root

        self.failures: list[dict[str, str]] = []
        self.research: Any = None
        self.rii: Any = None
        self.materials: dict[str, dict[str, Any]] = {}
        self.registry: dict[str, Any] = {}
        self.cie: Any = None
        self.rii_packaged = False
        self.rii_status: dict[str, Any] = {"status": "missing", "counts": {}}
        try:
            self.research = BuiltinResearchLibrary() if research is _DEFAULT else research
        except (OSError, ValueError) as exc:
            self._failure("research", exc)
        try:
            # This fixed packaged owner excludes custom materials and overrides.
            self.materials = {row["id"]: row for row in builtin_material_profiles()}
        except (OSError, ValueError) as exc:
            self._failure("materials", exc)
        try:
            self.rii = RefractiveIndexLocalStore(default_store_root())
            self.rii_status = self.rii.status()
            self.rii_packaged = self.rii.root == packaged_store_root()
        except (OSError, ValueError) as exc:
            self._failure("rii", exc)
            self.rii_status = {"status": "unavailable", "counts": {}}
        try:
            self.registry = reference_sources_catalog(limit=50)
        except (OSError, ValueError) as exc:
            self._failure("sources", exc)
        try:
            from .offline_reference_assets import CieReferenceSnapshot
            self.cie = CieReferenceSnapshot()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._failure("cie", exc)
        self.version = getattr(self.research, "version", None)
        self.document_count = getattr(self.research, "document_count", 0)
        self.coverage_note = ("Unified existing offline references; source-specific availability and rights apply. "
                              + getattr(self.research, "coverage_note", "Packaged research unavailable."))
        self.inventory_version = _hash({
            "research": getattr(self.research, "manifest_sha256", self.version),
            "materials": self.materials, "sources": self.registry.get("version"),
            "rii": self.rii_status, "cie": self.cie.version if self.cie else None, "failures": self.failures})

    def _failure(self, source: str, exc: Exception) -> None:
        self.failures.append({"source": source, "error": getattr(exc, "code", "source_unavailable")})

    def _check_version(self, expected_version: str | None) -> None:
        if expected_version is not None:
            if not isinstance(expected_version, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_version):
                raise ProjectError("invalid_arguments", "expected_version must be a returned inventory SHA-256")
            if expected_version != self.inventory_version:
                raise ProjectError("library_version_changed", "Offline inventory changed; restart discovery")

    def status(self) -> dict[str, Any]:
        return {"ok": True, "inventory_version": self.inventory_version,
                "read_only": True, "untrusted_reference": True,
                "sources": {
                    "research": {"availability": "packaged_text" if self.research else "unavailable",
                                 "documents": self.document_count, "version": self.version},
                    "materials": {"availability": "packaged_material_data", "records": len(self.materials)},
                    "rii": {"availability": ("packaged_data" if self.rii_packaged else "installed_data")
                            if self.rii_status["status"] == "ready" else self.rii_status["status"],
                            **deepcopy(self.rii_status)},
                    "cie": {"availability": "packaged_data" if self.cie else "unavailable",
                            "datasets": len(self.cie.datasets) if self.cie else 0,
                            "version": self.cie.version if self.cie else None},
                    "sources": {"availability": "reference_only_not_builtin", "metadata_only": True,
                                "records": self.registry.get("total", 0)}},
                "failures": deepcopy(self.failures), "incomplete": bool(self.failures),
                "scope": "Packaged text/materials, installed RII and source metadata. No private files, downloads or remote full text."}

    @staticmethod
    def _optical_summary(record: dict[str, Any]) -> list[dict[str, Any]]:
        fields = ("id", "name", "kind", "wavelength_unit", "wavelength_min", "wavelength_max", "source")
        return [{key: deepcopy(item[key]) for key in fields if key in item}
                for item in record.get("optical_constants", [])]

    def search(self, query: str, offset: int = 0, limit: int = 6,
               expected_version: str | None = None) -> dict[str, Any]:
        if (not isinstance(query, str) or not query.strip() or len(query) > 200
                or len(query.split()) > 12 or any(ord(c) < 32 for c in query)):
            raise ProjectError("invalid_arguments", "query must contain 1-200 characters and at most 12 terms")
        _integer(offset, 0, 1000000, "offset")
        _integer(limit, 1, MAX_RESULTS, "limit")
        self._check_version(expected_version)
        terms = query.casefold().split()
        groups: list[list[dict[str, Any]]] = [[], [], [], [], []]
        failures = deepcopy(self.failures)
        incomplete = bool(failures)
        for record in self.materials.values():
            summary = {"id": record["id"], "name": record["name"], "roles": record.get("roles", []),
                       "description": record.get("description", ""), "optical_sets": self._optical_summary(record)}
            if all(term in _json(summary).casefold() for term in terms):
                token = f"material/{_hash(record)}/{record['id']}"
                groups[0].append(self._row(record["name"], token, "packaged_material_data", summary,
                                           record.get("description", "")[:700]))
        counts = [len(groups[0]), 0, 0, 0, 0]
        if self.research is not None:
            result = self.research.search(query, offset=0, limit=10)
            if result.get("ok"):
                counts[1] = result["total_matches"]
                incomplete = incomplete or result.get("incomplete", False)
                if result.get("incomplete"):
                    failures.append({"source": "research", "error": result.get("truncation_reason") or "incomplete_scan"})
            else:
                failures.append({"source": "research", "error": result.get("error", "unavailable")})
                incomplete = True
        if self.rii is not None and self.rii_status["status"] == "ready":
            try:
                counts[2] = self.rii.search(query, catalog="all", limit=1)["total"]
            except (OSError, ValueError) as exc:
                failures.append({"source": "rii", "error": getattr(exc, "code", "unavailable")})
                incomplete = True
        registry_rows = [row for row in self.registry.get("items", [])
                         if all(term in _json(row).casefold() for term in terms)]
        counts[3] = len(registry_rows)
        if self.cie:
            for summary in self.cie.summaries():
                if all(term in _json(summary).casefold() for term in terms):
                    token = f"cie/{self.cie.version}/{summary['id']}"
                    groups[4].append(self._row(summary["title"], token, "packaged_data", summary,
                                               "Existing CIE table; includes original attribution, units, domains and change notices."))
        counts[4] = len(groups[4])
        # Deterministic interleaving prevents a large source hiding other owners.
        # This is discovery order, not scientific relevance or semantic ranking.
        positions: list[tuple[int, int]] = []
        sum_index = 0
        for rank in range(max(counts, default=0)):
            for group, count in enumerate(counts):
                if rank < count:
                    if offset <= sum_index < offset + limit:
                        positions.append((group, rank))
                    sum_index += 1
        selected = {group: [rank for family, rank in positions if family == group] for group in range(5)}
        rows: dict[tuple[int, int], dict[str, Any]] = {}
        for group in (0, 4):
            for rank in selected[group]:
                rows[group, rank] = groups[group][rank]
        if selected[1]:
            start = selected[1][0]
            result = self.research.search(query, offset=start, limit=len(selected[1]))
            if not result.get("ok") or result.get("incomplete"):
                incomplete = True
                failures.append({"source": "research", "error": result.get("error") or "incomplete_scan"})
            for index, item in enumerate(result.get("results", []), start):
                source = item["source"]
                token = f"text/{getattr(self.research, 'manifest_sha256', self.version)}/{source['path']}"
                rows[1, index] = self._row(item["title"], token, "packaged_text", source, item["excerpt"])
        if selected[2]:
            start = selected[2][0]
            try:
                result = self.rii.search(query, catalog="all", offset=start, limit=len(selected[2]))
                for index, item in enumerate(result["entries"], start):
                    token = f"rii/{self.rii.snapshot_id}/{item['entry_id']}"
                    row = self._row(item["name"], token, "packaged_data" if self.rii_packaged else "installed_data",
                                    {**item, "license": self.rii_status.get("license"),
                                     "snapshot_id": self.rii.snapshot_id, "wavelength_unit": "um",
                                     "band_note": "Read exact block ranges and quantity units before selection."}, "")
                    row["selection"] = {"tool": "refractiveindex_convert", "arguments": {
                        "entry_id": item["entry_id"], "snapshot_id": self.rii.snapshot_id},
                        "requires": "Explicit optical_set_id and block selection when ambiguous; candidate only, no import."}
                    rows[2, index] = row
            except (OSError, ValueError) as exc:
                incomplete = True
                failures.append({"source": "rii", "error": getattr(exc, "code", "unavailable")})
        for rank in selected[3]:
            item = registry_rows[rank]
            token = f"source/{self.registry['version']}/{item['id']}"
            rows[3, rank] = self._row(item["title"], token, "reference_only_not_builtin", item, item["summary"][:700])
        next_offset = offset + len(positions)
        return {"ok": True, "query": query, "version": self.version,
                "inventory_version": self.inventory_version, "source_kind": self.source_kind,
                "coverage_note": self.coverage_note, "included_files": self.document_count,
                "offset": offset, "total_matches": sum(counts), "limit": limit,
                "next_offset": next_offset if next_offset < sum(counts) else None,
                "has_more": next_offset < sum(counts), "incomplete": incomplete,
                "truncated_scan": incomplete, "failures": failures,
                "results": [rows[position] for position in positions if position in rows],
                "order": "Interleaved source-local literal matches; no relevance ranking.",
                "untrusted_reference": True}

    @staticmethod
    def _row(title: str, path: str, availability: str, metadata: dict[str, Any], excerpt: str) -> dict[str, Any]:
        return {"title": title, "source": {**deepcopy(metadata), "path": path, "availability": availability},
                "excerpt": excerpt, "read_tool": "library_read", "read_arguments": {"path": path}}

    def read(self, path: str, offset: int = 0, max_chars: int = MAX_READ_CHARS) -> dict[str, Any]:
        from .reference_sources import reference_sources_detail
        from .refractiveindex_local import RefractiveIndexLocalStore, store_root_for_snapshot
        _integer(offset, 0, 10000000, "offset")
        _integer(max_chars, 1, MAX_READ_CHARS, "max_chars")
        if not isinstance(path, str) or not 1 <= len(path) <= 700:
            raise ProjectError("invalid_arguments", "path must be a returned bounded library identifier")
        parts = path.split("/", 2)
        if len(parts) != 3:
            # Compatibility: existing manifest-relative text IDs remain valid.
            if self.research is not None:
                return self.research.read(path, offset=offset, max_chars=max_chars)
            raise ProjectError("library_entry_not_found", "Library identifier was not found")
        kind, version, identity = parts
        if kind == "text":
            if self.research is None or version != getattr(self.research, "manifest_sha256", self.version):
                raise ProjectError("library_version_changed", "Packaged research changed or is unavailable")
            return self.research.read(identity, offset=offset, max_chars=max_chars)
        data: dict[str, Any]
        if kind == "material":
            record = self.materials.get(identity)
            if record is None or version != _hash(record):
                raise ProjectError("library_version_changed", "Packaged material changed or is unavailable")
            data = {"availability": "packaged_material_data", "sha256": version, "record": record}
        elif kind == "rii":
            from .refractiveindex_local import packaged_store_root
            owner = RefractiveIndexLocalStore(store_root_for_snapshot(version), snapshot_id=version)
            data = {"availability": "packaged_data" if owner.root == packaged_store_root() else "installed_data",
                    **owner.get_entry(identity, raw_limit=20000)}
        elif kind == "cie":
            from .offline_reference_assets import CieReferenceSnapshot
            data = CieReferenceSnapshot().read(identity, version)
        elif kind == "source":
            data = {"availability": "reference_only_not_builtin", "data_bundled": False,
                    **reference_sources_detail(identity, expected_version=version)}
        else:
            if self.research is not None:
                return self.research.read(path, offset=offset, max_chars=max_chars)
            raise ProjectError("library_entry_not_found", "Library identifier was not found")
        text = _json(data)
        if len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise ProjectError("library_result_too_large", "Reference projection exceeds 2 MiB; use its original detail tool")
        end = min(len(text), offset + max_chars)
        return {"ok": True, "source": {"path": path, "availability": data["availability"], "version": version},
                "content": text[offset:end], "offset": offset, "next_offset": end if end < len(text) else None,
                "total_chars": len(text), "untrusted_reference": True}
