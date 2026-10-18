"""Versioned, offline RII reference data. Never a material-library writer.

Only ``build_from_directory`` writes. Readers pin one immutable snapshot and
return data-only projections; a caller must explicitly import a projection.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
from typing import Any

from .optical_constants import evaluate_optical_set, validate_optical_set

SCHEMA_VERSION = 1
OFFICIAL_REPOSITORY = "https://github.com/polyanskiy/refractiveindex.info-database"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SOURCE_FILES = 10000
MAX_DATA_BLOCKS = 64
CAPABILITIES = ("supported", "selection_required", "unsupported", "invalid")
_SNAPSHOT = re.compile(r"[0-9a-f]{40}-[0-9a-f]{16}-v1\Z")
_ENTRY = re.compile(r"rii_[0-9a-f]{24}\Z")


class RiiLocalError(ValueError):
    """Stable, non-network error boundary for GUI, CLI and assistant tools."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def default_store_root() -> Path:
    """Resolve the trusted process configuration without creating directories."""
    override = os.environ.get("COAT_SIM_REFRACTIVEINDEX_STORE")
    if override:
        root = Path(override)
        if not root.is_absolute():
            raise RiiLocalError("INVALID_STORE_PATH", "RII store override must be an absolute path")
        return root
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        if not base.is_absolute():
            base = Path.home() / "AppData" / "Local"
    else:
        configured = os.environ.get("XDG_DATA_HOME")
        base = Path(configured) if configured else Path.home() / ".local" / "share"
        if not base.is_absolute():
            base = Path.home() / ".local" / "share"
    return base / "CoatSim" / "reference-databases" / "refractiveindex"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise RiiLocalError("INVALID_YAML", "YAML data nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RiiLocalError("INVALID_YAML", "Non-finite YAML values are not supported")
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    raise RiiLocalError("INVALID_YAML", "YAML must contain JSON-compatible data with string keys")


def _yaml(data: bytes) -> Any:
    """SafeLoader plus bounded events and duplicate-key rejection; no aliases."""
    if len(data) > MAX_FILE_BYTES:
        raise RiiLocalError("SOURCE_LIMIT", "A source YAML file exceeds 16 MiB")
    try:
        yaml_text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise RiiLocalError("INVALID_YAML", "RII source YAML must use UTF-8") from exc
    try:
        yaml = importlib.import_module("yaml")
    except ImportError as exc:
        raise RiiLocalError("DEPENDENCY_MISSING", "PyYAML is required for local RII data") from exc
    try:
        depth = count = 0
        for event in yaml.parse(yaml_text, Loader=yaml.SafeLoader):
            count += 1
            if count > 200000:
                raise RiiLocalError("SOURCE_LIMIT", "Too many YAML events")
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
                raise RiiLocalError("INVALID_YAML", "YAML aliases and anchors are not accepted")
            if getattr(event, "tag", None):
                raise RiiLocalError("INVALID_YAML", "Explicit YAML tags are not accepted")
            if isinstance(event, (yaml.events.SequenceStartEvent, yaml.events.MappingStartEvent)):
                depth += 1
                if depth > 32:
                    raise RiiLocalError("SOURCE_LIMIT", "YAML nesting exceeds 32 levels")
            elif isinstance(event, (yaml.events.SequenceEndEvent, yaml.events.MappingEndEvent)):
                depth -= 1

        base_loader: Any = yaml.SafeLoader

        class UniqueSafeLoader(base_loader):
            pass

        def mapping(loader: Any, node: Any) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if not isinstance(key, str) or key in result:
                    raise RiiLocalError("INVALID_YAML", "Duplicate or non-string YAML mapping key")
                result[key] = loader.construct_object(value_node, deep=True)
            return result

        UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        loader = UniqueSafeLoader(yaml_text)
        try:
            return _plain(loader.get_single_data())
        finally:
            loader.dispose()
    except RiiLocalError:
        raise
    except (yaml.YAMLError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise RiiLocalError("INVALID_YAML", "Cannot safely parse source YAML") from exc


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise RiiLocalError("UNSAFE_SOURCE_PATH", "Invalid source-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".") for part in value.split("/")):
        raise RiiLocalError("UNSAFE_SOURCE_PATH", "Source path must remain within its root")
    if ":" in path.parts[0]:
        raise RiiLocalError("UNSAFE_SOURCE_PATH", "Drive-qualified paths are not accepted")
    return path.as_posix()


def _read_file(root: Path, relative: str, *, expected_hash: str | None = None) -> bytes:
    try:
        return _read_source_bytes(root, relative, expected_hash=expected_hash)
    except OSError as exc:
        raise RiiLocalError("SOURCE_READ_FAILED", "Cannot read a local snapshot source file") from exc


def _read_source_bytes(root: Path, relative: str, *, expected_hash: str | None = None) -> bytes:
    relative = _relative(relative)
    path = root / relative
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise RiiLocalError("UNSAFE_SOURCE_PATH", "Symlink sources are not accepted")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise RiiLocalError("SOURCE_LIMIT", f"Missing or oversized source file: {relative}")
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise RiiLocalError("SOURCE_LIMIT", "Source file grew beyond the byte limit")
    if expected_hash is not None and _digest(data) != expected_hash:
        raise RiiLocalError("SOURCE_INTEGRITY_ERROR", "Source bytes no longer match the indexed snapshot")
    return data


def _index_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOTAL_BYTES:
        raise RiiLocalError("INVALID_STORE", "Invalid local index file")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            total += len(block)
            if total > MAX_TOTAL_BYTES:
                raise RiiLocalError("INVALID_STORE", "Index grew beyond the byte limit")
            digest.update(block)
    return digest.hexdigest()


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise RiiLocalError("INVALID_QUERY", f"{label} must be an integer from {low} to {high}")
    return value


def _selection(value: Any, count: int) -> list[int]:
    if value is None:
        return list(range(count))
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_DATA_BLOCKS:
        raise RiiLocalError("INVALID_BLOCK_SELECTION", "Select one or more DATA block indices")
    indices = [_integer(item, 0, count - 1, "block index") for item in value]
    if len(set(indices)) != len(indices):
        raise RiiLocalError("INVALID_BLOCK_SELECTION", "Block indices must be distinct")
    return sorted(indices)


def _blocks(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        raise RiiLocalError("INVALID_SOURCE_DATA", "A dataset YAML must contain a mapping")
    blocks = document.get("DATA")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_DATA_BLOCKS:
        raise RiiLocalError("INVALID_SOURCE_DATA", "DATA must contain 1–64 blocks")
    if any(not isinstance(block, dict) or not isinstance(block.get("type"), str) for block in blocks):
        raise RiiLocalError("INVALID_SOURCE_DATA", "Each DATA block needs a textual type")
    return blocks


def _numbers(value: Any, label: str) -> list[float]:
    if not isinstance(value, str):
        raise RiiLocalError("INVALID_SOURCE_DATA", f"{label} must be a numeric text field")
    cells = value.split(maxsplit=64)
    if len(cells) > 64:
        raise RiiLocalError("SOURCE_LIMIT", f"Too many numeric fields in {label}")
    try:
        numbers = [float(cell) for cell in cells]
    except (ValueError, OverflowError) as exc:
        raise RiiLocalError("INVALID_SOURCE_DATA", f"Invalid number in {label}") from exc
    if any(not math.isfinite(number) for number in numbers):
        raise RiiLocalError("INVALID_SOURCE_DATA", f"Non-finite number in {label}")
    return numbers


def _table(block: Mapping[str, Any]) -> list[list[float]]:
    widths = {"tabulated n": 2, "tabulated k": 2, "tabulated nk": 3, "tabulated n2": 2}
    kind = block.get("type")
    if kind not in widths:
        raise RiiLocalError("UNSUPPORTED_DATA_TYPE", f"Unsupported table type: {kind}")
    data = block.get("data")
    if not isinstance(data, str):
        raise RiiLocalError("INVALID_SOURCE_DATA", "Tabulated DATA needs a numeric text block")
    rows: list[list[float]] = []
    for line in io.StringIO(data):
        if not line.strip():
            continue
        if len(rows) >= 100000:
            raise RiiLocalError("SOURCE_LIMIT", "Table exceeds 100000 source rows")
        cells = line.split(maxsplit=widths[kind])
        if len(cells) != widths[kind]:
            raise RiiLocalError("INVALID_SOURCE_DATA", "Table has an invalid column count")
        rows.append(_numbers(line, "table row"))
    if not rows:
        raise RiiLocalError("INVALID_SOURCE_DATA", "Table has no numeric rows")
    return rows


def _formula(block: Mapping[str, Any]) -> tuple[int, list[float], list[float]]:
    match = re.fullmatch(r"formula ([1-9])", str(block.get("type", "")))
    if match is None:
        raise RiiLocalError("UNSUPPORTED_DATA_TYPE", f"Unsupported formula: {block.get('type')}")
    domain = _numbers(block.get("wavelength_range"), "wavelength_range")
    if len(domain) != 2:
        raise RiiLocalError("INVALID_SOURCE_DATA", "Formula wavelength_range needs two endpoints")
    return int(match[1]), _numbers(block.get("coefficients"), "coefficients"), domain


def _project(document: dict[str, Any], indices: list[int], *, identifier: str, name: str,
             source: str = "", notes: str = "") -> dict[str, Any]:
    blocks = _blocks(document)
    chosen = [blocks[index] for index in indices]
    n_blocks = [block for block in chosen if block["type"] in ("tabulated n", "tabulated nk")
                or re.fullmatch(r"formula [1-9]", block["type"])]
    k_blocks = [block for block in chosen if block["type"] == "tabulated k"]
    if any(block["type"] == "tabulated n2" for block in chosen):
        raise RiiLocalError("NONLINEAR_N2_REFERENCE_ONLY", "Nonlinear n2 is not linear n or extinction k")
    if len(n_blocks) > 1:
        raise RiiLocalError("BLOCK_SELECTION_REQUIRED", "Multiple n representations require explicit selection")
    if not n_blocks:
        code = "MISSING_N" if k_blocks else "UNSUPPORTED_DATA_TYPE"
        raise RiiLocalError(code, "Selected blocks do not contain a supported n representation")
    if len(k_blocks) > 1 or len(chosen) != len(n_blocks) + len(k_blocks):
        raise RiiLocalError("UNSUPPORTED_DATA_TYPE", "Selected DATA block combination is not supported")
    n_block = n_blocks[0]
    candidate: dict[str, Any] = {
        "id": identifier, "name": name, "source": source, "notes": notes,
        "wavelength_unit": "um",
    }
    if n_block["type"].startswith("formula "):
        number, coefficients, domain = _formula(n_block)
        candidate.update(kind="rii_formula", formula_number=number, coefficients=coefficients,
                         wavelength_min=domain[0], wavelength_max=domain[1])
        if k_blocks:
            k_rows = _table(k_blocks[0])
            candidate["k_data"] = {
                "wavelength_unit": "um", "wavelength_min": k_rows[0][0],
                "wavelength_max": k_rows[-1][0], "wavelengths": [row[0] for row in k_rows],
                "k": [row[1] for row in k_rows], "source": source, "notes": "Original independent RII k table",
            }
    else:
        rows = _table(n_block)
        k = [row[2] for row in rows] if n_block["type"] == "tabulated nk" else None
        if k_blocks:
            if k is not None:
                raise RiiLocalError("BLOCK_SELECTION_REQUIRED", "Multiple k representations require selection")
            k_rows = _table(k_blocks[0])
            if [row[0] for row in k_rows] != [row[0] for row in rows]:
                raise RiiLocalError("INDEPENDENT_TABLE_GRIDS", "Independent n/k grids cannot be merged without resampling")
            k = [row[1] for row in k_rows]
        candidate.update(kind="tabulated", wavelength_min=rows[0][0], wavelength_max=rows[-1][0],
                         wavelengths=[row[0] for row in rows], n=[row[1] for row in rows], k=k)
    try:
        return validate_optical_set(candidate)
    except ValueError as exc:
        raise RiiLocalError("OPTICAL_VALIDATION_FAILED", str(exc)) from exc


def _has_k(candidate: Mapping[str, Any]) -> bool:
    return candidate.get("k") is not None or candidate.get("k_data") is not None


def _conversion(document: dict[str, Any]) -> dict[str, Any]:
    try:
        blocks = _blocks(document)
    except RiiLocalError as exc:
        return {"status": "invalid", "reason_codes": [exc.code], "reason": str(exc), "choices": []}
    selections = [list(range(len(blocks)))]
    selections.extend([index] for index in range(len(blocks)) if [index] not in selections)
    choices: list[dict[str, Any]] = []
    full_error: RiiLocalError | None = None
    for order, indices in enumerate(selections):
        try:
            candidate = _project(document, indices, identifier="rii_preview", name="RII projection")
            choices.append({"block_selection": indices, "has_n": True, "has_k": _has_k(candidate)})
        except RiiLocalError as exc:
            if order == 0:
                full_error = exc
    if full_error is None:
        return {"status": "supported", "reason_codes": [], "reason": "", "choices": choices}
    invalid_codes = {"INVALID_SOURCE_DATA", "OPTICAL_VALIDATION_FAILED"}
    status = "selection_required" if choices else ("invalid" if full_error.code in invalid_codes else "unsupported")
    return {"status": status, "reason_codes": [full_error.code], "reason": str(full_error), "choices": choices}


def _block_summary(block: dict[str, Any], index: int) -> dict[str, Any]:
    result: dict[str, Any] = {"index": index, "type": block.get("type"), "range_um": None,
                              "row_count": None, "coefficients": None}
    try:
        if str(block.get("type", "")).startswith("tabulated "):
            rows = _table(block)
            result.update(range_um=[min(row[0] for row in rows), max(row[0] for row in rows)], row_count=len(rows))
        else:
            _number, coefficients, domain = _formula(block)
            result.update(range_um=domain, coefficients=coefficients)
    except RiiLocalError as exc:
        result["error"] = {"code": exc.code, "message": str(exc)}
    return result


def _catalog(document: Any, catalog: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(nodes: Any, context: dict[str, Any], dividers: list[str], depth: int) -> None:
        if depth > 12 or not isinstance(nodes, list):
            raise RiiLocalError("INVALID_CATALOG", "Invalid or excessively nested classification")
        divider = ""
        for node in nodes:
            if not isinstance(node, dict):
                raise RiiLocalError("INVALID_CATALOG", "Classification nodes must be mappings")
            if "DIVIDER" in node:
                divider = str(node["DIVIDER"])
                continue
            here = dict(context)
            for key in ("SHELF", "BOOK", "PAGE"):
                if key in node:
                    if not isinstance(node[key], str):
                        raise RiiLocalError("INVALID_CATALOG", "Classification identifiers must be text")
                    here[key.lower()] = node[key]
                    here[key.lower() + "_name"] = str(node.get("name", ""))
            branch = dividers + ([divider] if divider else [])
            if "PAGE" in node:
                if not all(key in here for key in ("shelf", "book", "page")):
                    raise RiiLocalError("INVALID_CATALOG", "PAGE is missing its shelf or book")
                result.append({**here, "catalog": catalog, "order": len(result), "dividers": branch,
                               "original_path": node.get("data"), "source_path": _relative(node.get("data"))})
            if "content" in node:
                walk(node["content"], here, branch, depth + 1)

    walk(document, {}, [], 0)
    return result


def _cancel(check: Callable[[], bool] | None) -> None:
    if check is not None and check():
        raise RiiLocalError("CANCELLED", "Local database build was cancelled; the active snapshot is unchanged")


def _validate_manifest(manifest: Any, snapshot_id: str) -> dict[str, Any]:
    """Validate everything status/activation uses before changing CURRENT."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported manifest schema")
    if manifest.get("snapshot_id") != snapshot_id or manifest.get("upstream_commit") != snapshot_id[:40]:
        raise ValueError("Snapshot identity mismatch")
    if manifest.get("source_url") != f"{OFFICIAL_REPOSITORY}/tree/{snapshot_id[:40]}":
        raise ValueError("Invalid fixed source URL")
    if manifest.get("license") != "CC0-1.0" or manifest.get("upstream_commit_verified") is not False:
        raise ValueError("Invalid license or unsupported provenance assertion")
    if not isinstance(manifest.get("verification"), str):
        raise ValueError("Missing verification description")
    for key in ("source_content_sha256", "index_sha256", "license_sha256"):
        value = manifest.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("Invalid manifest hash")
    files = manifest.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_SOURCE_FILES:
        raise ValueError("Invalid source file manifest")
    paths: set[str] = set()
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("Invalid source file record")
        path = _relative(item["path"])
        if path in paths or path != item["path"]:
            raise ValueError("Noncanonical or duplicate source path")
        paths.add(path)
        if type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_FILE_BYTES:
            raise ValueError("Invalid source size")
        total_bytes += item["bytes"]
        if not isinstance(item["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None:
            raise ValueError("Invalid source hash")
    if total_bytes > MAX_TOTAL_BYTES or "LICENSE" not in paths or "database/catalog-nk.yml" not in paths:
        raise ValueError("Invalid source totals")
    content_hash = _digest(_json(files).encode("utf-8"))
    if content_hash != manifest["source_content_sha256"] or content_hash[:16] != snapshot_id[41:57]:
        raise ValueError("Source manifest hash mismatch")
    counts = manifest.get("counts")
    keys = ("source_files", "source_bytes", "catalog_occurrences", "indexed_entries", "nk", "n2", "cataloged", "uncatalogued")
    if not isinstance(counts, dict) or any(type(counts.get(key)) is not int or counts[key] < 0 for key in keys):
        raise ValueError("Invalid counts")
    if counts["source_files"] != len(files) or counts["source_bytes"] != total_bytes:
        raise ValueError("Source totals mismatch")
    total = counts["indexed_entries"]
    if counts["nk"] + counts["n2"] != total or counts["cataloged"] + counts["uncatalogued"] != total:
        raise ValueError("Entry totals mismatch")
    support = counts.get("conversion_status")
    if not isinstance(support, dict) or any(key not in CAPABILITIES or type(value) is not int or value < 0
                                          for key, value in support.items()) or sum(support.values()) != total:
        raise ValueError("Invalid conversion counts")
    return manifest


def build_from_directory(source_dir: str | Path, store_root: str | Path, *, upstream_commit: str,
                         source_url: str | None = None, activate: bool = True,
                         cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Explicitly copy/index local data and atomically activate; no network I/O.

    The supplied commit is provenance declared by the operator. Content hashes
    establish local identity, not proof that an arbitrary folder is upstream.
    """
    if not isinstance(upstream_commit, str) or re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is None:
        raise RiiLocalError("INVALID_COMMIT", "upstream_commit must be a full lowercase Git SHA")
    canonical_url = f"{OFFICIAL_REPOSITORY}/tree/{upstream_commit}"
    if source_url not in (None, canonical_url):
        raise RiiLocalError("INVALID_SOURCE_URL", "Source URL must be the fixed official commit tree URL")
    if type(activate) is not bool:
        raise RiiLocalError("INVALID_QUERY", "activate must be boolean")
    source = Path(source_dir).resolve()
    supplied_root = Path(store_root)
    if supplied_root.is_symlink():
        raise RiiLocalError("UNSAFE_STORE_PATH", "Store root must not be a symlink")
    root = supplied_root.resolve()
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise RiiLocalError("UNSAFE_SOURCE_PATH", "Source and store directories must not overlap")
    if not source.is_dir() or (Path(source_dir).is_symlink()):
        raise RiiLocalError("UNSAFE_SOURCE_PATH", "Source must be a local non-symlink directory")
    license_bytes = _read_file(source, "LICENSE")
    if not license_bytes.startswith(b"CC0 1.0 Universal"):
        raise RiiLocalError("LICENSE_MISMATCH", "A source CC0 1.0 Universal LICENSE is required")
    _read_file(source, "database/catalog-nk.yml")
    files = [source / "LICENSE"]
    if (source / "README.md").is_file():
        files.append(source / "README.md")
    for path in (source / "database").rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise RiiLocalError("UNSAFE_SOURCE_PATH", "Source contains a symlink or non-regular file")
        if path.is_file():
            files.append(path)
        if len(files) > MAX_SOURCE_FILES:
            raise RiiLocalError("SOURCE_LIMIT", "Snapshot exceeds the source-file limit")
    if sum(path.stat().st_size for path in files) > MAX_TOTAL_BYTES:
        raise RiiLocalError("SOURCE_LIMIT", "Snapshot exceeds 256 MiB")
    _cancel(cancel_check)
    versions = root / "versions"
    if versions.is_symlink() or (root / "CURRENT.json").is_symlink():
        raise RiiLocalError("UNSAFE_STORE_PATH", "Store internals must not be symlinks")
    root.mkdir(parents=True, exist_ok=True)
    versions.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=versions))
    try:
        mirror = staging / "source"
        mirror.mkdir()
        file_records: list[dict[str, Any]] = []
        copied_bytes = 0
        for path in sorted(files):
            _cancel(cancel_check)
            relative = path.relative_to(source).as_posix()
            data = _read_file(source, relative)
            copied_bytes += len(data)
            if copied_bytes > MAX_TOTAL_BYTES:
                raise RiiLocalError("SOURCE_LIMIT", "Actual source bytes exceed the snapshot limit")
            destination = mirror / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            file_records.append({"path": relative, "bytes": len(data), "sha256": _digest(data)})
        content_hash = _digest(_json(file_records).encode("utf-8"))
        snapshot_id = f"{upstream_commit}-{content_hash[:16]}-v1"
        classification: dict[str, list[dict[str, Any]]] = {}
        occurrences = 0
        for kind in ("nk", "n2"):
            relative = f"database/catalog-{kind}.yml"
            if not (mirror / relative).is_file():
                continue
            for alias in _catalog(_yaml(_read_file(mirror, relative)), kind):
                path = alias["source_path"]
                if not (mirror / "database" / "data" / path).is_file():
                    raise RiiLocalError("MISSING_CATALOG_SOURCE", f"Classification points to missing data: {path}")
                classification.setdefault(path, []).append(alias)
                occurrences += 1
        connection = sqlite3.connect(staging / "index.sqlite3")
        counts: Counter[str] = Counter()
        support_counts: Counter[str] = Counter()
        try:
            connection.executescript("""
                CREATE TABLE entries(entry_id TEXT PRIMARY KEY, source_path TEXT UNIQUE NOT NULL,
                    catalog TEXT NOT NULL, cataloged INTEGER NOT NULL, capability TEXT NOT NULL,
                    summary_json TEXT NOT NULL, search_text TEXT NOT NULL);
                CREATE TABLE classifications(entry_id TEXT NOT NULL, shelf TEXT NOT NULL, book TEXT NOT NULL);
                CREATE INDEX by_catalog ON entries(catalog,capability,cataloged);
                CREATE INDEX by_classification ON classifications(shelf,book,entry_id);
            """)
            for item in file_records:
                _cancel(cancel_check)
                relative = item["path"]
                if not relative.startswith("database/data/") or Path(relative).suffix not in (".yml", ".yaml"):
                    continue
                document = _yaml(_read_file(mirror, relative))
                if not isinstance(document, dict) or "DATA" not in document:
                    continue
                source_path = relative.removeprefix("database/data/")
                aliases = classification.get(source_path, [])
                block_list = document.get("DATA")
                data_types = [block.get("type") for block in block_list if isinstance(block, dict)] if isinstance(block_list, list) else []
                kind = "n2" if any(value == "tabulated n2" for value in data_types) else "nk"
                if aliases and any(alias["catalog"] != kind for alias in aliases):
                    raise RiiLocalError("INVALID_CATALOG", "Catalog quantity does not match DATA quantity")
                entry_id = "rii_" + _digest(source_path.encode("utf-8"))[:24]
                name = str(aliases[0]["book_name"] + " / " + aliases[0]["page_name"]) if aliases else source_path
                conversion = _conversion(document)
                summary = {"entry_id": entry_id, "name": name, "source_path": source_path,
                           "catalog": kind, "cataloged": bool(aliases), "classifications": aliases,
                           "data_types": data_types, "conversion": conversion, "source_sha256": item["sha256"]}
                searchable = _json({"name": name, "path": source_path, "classifications": aliases,
                                    "references": document.get("REFERENCES"), "comments": document.get("COMMENTS"),
                                    "conditions": document.get("CONDITIONS"), "properties": document.get("PROPERTIES")}).casefold()
                connection.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?)", (
                    entry_id, source_path, kind, int(bool(aliases)), conversion["status"], _json(summary), searchable))
                connection.executemany("INSERT INTO classifications VALUES (?,?,?)", (
                    (entry_id, alias["shelf"], alias["book"]) for alias in aliases))
                counts[kind] += 1
                counts["cataloged" if aliases else "uncatalogued"] += 1
                support_counts[conversion["status"]] += 1
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RiiLocalError("INDEX_INTEGRITY_ERROR", "New local index failed SQLite integrity check")
        finally:
            connection.close()
        indexed_paths: set[str] = set()
        with sqlite3.connect(staging / "index.sqlite3") as check:
            indexed_paths.update(row[0] for row in check.execute("SELECT source_path FROM entries"))
        if set(classification) - indexed_paths:
            raise RiiLocalError("INVALID_CATALOG", "Some classified files lack indexable DATA")
        manifest = {
            "schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id, "upstream_commit": upstream_commit,
            "source_url": canonical_url, "upstream_commit_verified": False,
            "verification": "local_content_hashes_only; upstream_commit_declared_by_operator",
            "license": "CC0-1.0", "license_sha256": _digest(license_bytes),
            "source_content_sha256": content_hash, "files": file_records,
            "index_sha256": _index_digest(staging / "index.sqlite3"),
            "counts": {"source_files": len(file_records), "source_bytes": sum(item["bytes"] for item in file_records),
                       "catalog_occurrences": occurrences, "indexed_entries": counts["nk"] + counts["n2"],
                       "nk": counts["nk"], "n2": counts["n2"], "cataloged": counts["cataloged"],
                       "uncatalogued": counts["uncatalogued"], "conversion_status": dict(support_counts)},
        }
        (staging / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        _cancel(cancel_check)
        final = versions / snapshot_id
        if final.exists():
            existing = RefractiveIndexLocalStore(root, snapshot_id=snapshot_id)
            if existing._manifest.get("source_content_sha256") != content_hash:
                raise RiiLocalError("SNAPSHOT_CONFLICT", "Existing snapshot identity does not match this content")
            shutil.rmtree(staging)
        else:
            os.replace(staging, final)
        result = RefractiveIndexLocalStore(root, snapshot_id=snapshot_id).status()
        result["activated"] = activate
        _cancel(cancel_check)
        if activate:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(_json({"schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id}))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, root / "CURRENT.json")
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        return result
    except RiiLocalError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise RiiLocalError("BUILD_FAILED", f"Local database build failed: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


class RefractiveIndexLocalStore:
    """Read-only, snapshot-pinned local reference database."""

    def __init__(self, store_root: str | Path, snapshot_id: str | None = None) -> None:
        self.root = Path(store_root)
        self.snapshot_id: str | None = snapshot_id
        self._manifest: dict[str, Any] = {}
        self._version: Path | None = None
        if snapshot_id is None:
            current = self.root / "CURRENT.json"
            if not current.exists():
                return
            try:
                if current.is_symlink() or current.stat().st_size > 4096:
                    raise ValueError("Invalid current pointer")
                pointer = json.loads(current.read_text(encoding="utf-8"))
                snapshot_id = pointer["snapshot_id"]
                if pointer.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("Unsupported pointer schema")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RiiLocalError("INVALID_STORE", "Cannot read the local snapshot pointer") from exc
        if not isinstance(snapshot_id, str) or not _SNAPSHOT.fullmatch(snapshot_id):
            raise RiiLocalError("INVALID_SNAPSHOT", "Invalid local snapshot identity")
        self.snapshot_id = snapshot_id
        self._version = self.root / "versions" / snapshot_id
        try:
            if self._version.is_symlink() or (self.root / "versions").is_symlink():
                raise ValueError("Symlink version directory")
            manifest_bytes = _read_file(self._version, "manifest.json")
            manifest = _validate_manifest(json.loads(manifest_bytes), snapshot_id)
            index = self._version / "index.sqlite3"
            if _index_digest(index) != manifest["index_sha256"]:
                raise ValueError("SQLite hash mismatch")
            self._manifest = manifest
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RiiLocalError("INVALID_STORE", "Snapshot is missing, damaged or incompatible") from exc

    def status(self) -> dict[str, Any]:
        if self._version is None:
            return {"status": "missing", "snapshot_id": None, "counts": {}, "read_only": True}
        return {"status": "ready", "snapshot_id": self.snapshot_id,
                "upstream_commit": self._manifest["upstream_commit"], "source_url": self._manifest["source_url"],
                "upstream_commit_verified": self._manifest["upstream_commit_verified"],
                "verification": self._manifest["verification"], "license": self._manifest["license"],
                "counts": deepcopy(self._manifest["counts"]), "read_only": True}

    def _connect(self) -> sqlite3.Connection:
        if self._version is None:
            raise RiiLocalError("STORE_MISSING", "No local RII snapshot has been installed")
        try:
            index = self._version / "index.sqlite3"
            if _index_digest(index) != self._manifest["index_sha256"]:
                raise RiiLocalError("SOURCE_INTEGRITY_ERROR", "Index bytes no longer match this snapshot")
            connection = sqlite3.connect(index.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            return connection
        except (sqlite3.Error, OSError) as exc:
            raise RiiLocalError("INVALID_STORE", "Cannot open local RII index read-only") from exc

    def search(self, query: str = "", *, catalog: str = "nk", shelf: str | None = None,
               book: str | None = None, capability: str | None = None, cataloged: bool | None = None,
               offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > 200:
            raise RiiLocalError("INVALID_QUERY", "Search query must be at most 200 characters")
        if catalog not in ("nk", "n2", "all") or capability not in (None, *CAPABILITIES):
            raise RiiLocalError("INVALID_QUERY", "Invalid catalog or capability filter")
        for value in (shelf, book):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
                raise RiiLocalError("INVALID_QUERY", "Classification filters must be short nonempty strings")
        if cataloged is not None and type(cataloged) is not bool:
            raise RiiLocalError("INVALID_QUERY", "cataloged must be boolean or None")
        offset = _integer(offset, 0, 1000000, "offset")
        limit = _integer(limit, 1, 100, "limit")
        clauses: list[str] = []
        parameters: list[Any] = []
        for token in query.casefold().split():
            clauses.append("instr(search_text,?)>0")
            parameters.append(token)
        for column, filter_value in (("catalog", None if catalog == "all" else catalog), ("capability", capability),
                                     ("cataloged", None if cataloged is None else int(cataloged))):
            if filter_value is not None:
                clauses.append(column + "=?")
                parameters.append(filter_value)
        if shelf is not None or book is not None:
            filters = ["c.entry_id=entries.entry_id"]
            for column, value in (("shelf", shelf), ("book", book)):
                if value is not None:
                    filters.append("c." + column + "=?")
                    parameters.append(value)
            clauses.append("EXISTS (SELECT 1 FROM classifications c WHERE " + " AND ".join(filters) + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        connection = self._connect()
        try:
            total = connection.execute("SELECT count(*) FROM entries" + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT summary_json FROM entries" + where + " ORDER BY source_path LIMIT ? OFFSET ?",
                                      [*parameters, limit, offset]).fetchall()
        except sqlite3.Error as exc:
            raise RiiLocalError("INVALID_STORE", "Local RII search failed") from exc
        finally:
            connection.close()
        return {"snapshot_id": self.snapshot_id, "query": query, "total": total, "offset": offset,
                "limit": limit, "has_more": offset + len(rows) < total,
                "entries": [json.loads(row[0]) for row in rows]}

    def _entry(self, entry_id: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        if not isinstance(entry_id, str) or not _ENTRY.fullmatch(entry_id):
            raise RiiLocalError("INVALID_ENTRY_ID", "Invalid RII entry identity")
        connection = self._connect()
        try:
            row = connection.execute("SELECT summary_json FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        except sqlite3.Error as exc:
            raise RiiLocalError("INVALID_STORE", "Cannot read the local RII entry") from exc
        finally:
            connection.close()
        if row is None:
            raise RiiLocalError("ENTRY_NOT_FOUND", "RII entry does not exist in this snapshot")
        summary = json.loads(row[0])
        if self._version is None:
            raise RiiLocalError("STORE_MISSING", "No snapshot selected")
        raw = _read_file(self._version / "source", "database/data/" + summary["source_path"],
                         expected_hash=summary["source_sha256"])
        document = _yaml(raw)
        if not isinstance(document, dict):
            raise RiiLocalError("INVALID_SOURCE_DATA", "Dataset must be a YAML mapping")
        return summary, document, raw

    def get_entry(self, entry_id: str, *, raw_limit: int = 20000) -> dict[str, Any]:
        raw_limit = _integer(raw_limit, 0, 100000, "raw_limit")
        summary, document, raw = self._entry(entry_id)
        text = raw.decode("utf-8-sig")
        blocks = document.get("DATA", [])
        references = str(document.get("REFERENCES", ""))
        comments = str(document.get("COMMENTS", ""))
        return {**summary, "snapshot_id": self.snapshot_id, "upstream_commit": self._manifest["upstream_commit"],
                "source_url": self._source_url(summary["source_path"]), "license": "CC0-1.0",
                "references": references[:20000], "comments": comments[:20000],
                "references_truncated": len(references) > 20000, "comments_truncated": len(comments) > 20000,
                "conditions": document.get("CONDITIONS", {}), "properties": document.get("PROPERTIES", {}),
                "blocks": [_block_summary(block, index) for index, block in enumerate(blocks) if isinstance(block, dict)]
                if isinstance(blocks, list) else [],
                "raw_yaml": {"text": text[:raw_limit], "truncated": len(text) > raw_limit, "total_chars": len(text)},
                "untrusted_reference": True}

    def _source_url(self, source_path: str) -> str:
        from urllib.parse import quote
        return f"{OFFICIAL_REPOSITORY}/blob/{self._manifest['upstream_commit']}/database/data/{quote(source_path, safe='/')}"

    def preview(self, entry_id: str, *, block_indices: Sequence[int] | None = None,
                max_points: int = 200) -> dict[str, Any]:
        max_points = _integer(max_points, 2, 1000, "max_points")
        summary, document, _raw = self._entry(entry_id)
        blocks = _blocks(document)
        indices = _selection(block_indices, len(blocks))
        result = []
        for index in indices:
            block = blocks[index]
            item: dict[str, Any] = {"index": index, "type": block["type"], "quantity": None,
                                    "status": "ok", "points": [], "columns": [], "source_point_count": None,
                                    "display_point_count": 0, "sampled": False, "calculated": False,
                                    "range_um": None, "error": None}
            try:
                if block["type"].startswith("tabulated "):
                    rows = _table(block)
                    quantity = block["type"].removeprefix("tabulated ")
                    columns = ["wavelength_um", "n", "k"] if quantity == "nk" else ["wavelength_um", quantity]
                    count = len(rows)
                    positions = list(range(count)) if count <= max_points else [
                        (i * (count - 1)) // (max_points - 1) for i in range(max_points)]
                    item.update(quantity="n,k" if quantity == "nk" else quantity, columns=columns,
                                points=[rows[position] for position in positions], source_point_count=count,
                                sampled=count > max_points, range_um=[min(row[0] for row in rows), max(row[0] for row in rows)])
                else:
                    candidate = _project(document, [index], identifier="rii_preview", name="RII preview")
                    low, high = candidate["wavelength_min"], candidate["wavelength_max"]
                    wavelengths = [low + (high - low) * i / (max_points - 1) for i in range(max_points)]
                    wavelengths[0], wavelengths[-1] = low, high
                    points = [[wave, evaluate_optical_set(candidate, wave, "um")[0]] for wave in wavelengths]
                    item.update(quantity="n", columns=["wavelength_um", "n"], points=points,
                                calculated=True, sampled=True, range_um=[low, high])
                item["display_point_count"] = len(item["points"])
            except (RiiLocalError, ValueError) as exc:
                code = exc.code if isinstance(exc, RiiLocalError) else "OPTICAL_EVALUATION_FAILED"
                item.update(status="unsupported" if code in ("UNSUPPORTED_DATA_TYPE", "MISSING_N") else "invalid",
                            points=[], error={"code": code, "message": str(exc)})
            result.append(item)
        return {"snapshot_id": self.snapshot_id, "entry_id": entry_id, "source_sha256": summary["source_sha256"],
                "blocks": result, "conversion": summary["conversion"], "untrusted_reference": True,
                "sampling_policy": "source_order_even_indices_for_tables; linear_wavelength_for_formula_display"}

    def convert_entry(self, entry_id: str, *, optical_set_id: str, name: str | None = None,
                      block_selection: Sequence[int] | None = None) -> dict[str, Any]:
        summary, document, _raw = self._entry(entry_id)
        blocks = _blocks(document)
        indices = _selection(block_selection, len(blocks))
        excluded = [index for index in range(len(blocks)) if index not in indices]
        selected_name = name if name is not None else summary["name"][:120]
        references = str(document.get("REFERENCES", ""))
        comments = str(document.get("COMMENTS", ""))
        provenance = {"snapshot_id": self.snapshot_id, "entry_id": entry_id, "source_sha256": summary["source_sha256"],
                      "source_path": summary["source_path"], "upstream_commit": self._manifest["upstream_commit"],
                      "source_url": self._source_url(summary["source_path"]), "license": "CC0-1.0",
                      "block_selection": indices, "excluded_blocks": excluded}
        source = provenance["source_url"] + "\n" + references
        notes = (f"RII snapshot={self.snapshot_id}; source_sha256={summary['source_sha256']}; "
                 f"blocks={indices}; excluded_blocks={excluded}. Conditions are source-specific; production validation not established.\n"
                 + comments + "\nCONDITIONS: " + _json(document.get("CONDITIONS", {})))
        if len(source) > 20000 or len(notes) > 20000:
            raise RiiLocalError("PROVENANCE_TOO_LARGE", "Source provenance exceeds the optical-set text contract; no silent truncation")
        candidate = _project(document, indices, identifier=optical_set_id, name=selected_name, source=source, notes=notes)
        missing = [] if _has_k(candidate) else ["Extinction coefficient k is not supplied; losslessness is not assumed"]
        return {"optical_set": candidate, "provenance": provenance, "conditions": document.get("CONDITIONS", {}),
                "missing": missing, "assumptions": [], "warnings": ["Unselected DATA blocks were not imported"] if excluded else [],
                "production_validation": "not_established", "untrusted_reference": True}
