"""Read local RII snapshots or explicitly build one from an already-downloaded directory.

Run ``python -m coat_sim.refractiveindex_cli --help``. No command downloads
anything; only the explicitly selected ``build`` command writes the store.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", help="Local reference-store directory; defaults to the shared per-user store")
    parser.add_argument("--snapshot-id", help="Pin an installed immutable version for a read operation")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("status", help="Read installation state, version and coverage; never creates storage")
    search = commands.add_parser("search", help="Read a page of local index entries; empty query browses")
    search.add_argument("--query", default="")
    search.add_argument("--catalog", choices=("nk", "n2", "all"), default="nk")
    search.add_argument("--shelf")
    search.add_argument("--book")
    search.add_argument("--capability", choices=("supported", "selection_required", "unsupported", "invalid"))
    cataloged = search.add_mutually_exclusive_group()
    cataloged.add_argument("--cataloged", dest="cataloged", action="store_true", default=None)
    cataloged.add_argument("--uncataloged", dest="cataloged", action="store_false")
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--limit", type=int, default=50)
    detail = commands.add_parser("detail", help="Read entry metadata and bounded inert YAML source text")
    detail.add_argument("entry_id")
    detail.add_argument("--raw-limit", type=int, default=20000)
    preview = commands.add_parser("preview", help="Read bounded display points; does not create import data")
    preview.add_argument("entry_id")
    preview.add_argument("--block-indices", nargs="+", type=int)
    preview.add_argument("--max-points", type=int, default=200)
    convert = commands.add_parser("convert", help="Read a validated full-resolution optical-set candidate; no material write")
    convert.add_argument("entry_id")
    convert.add_argument("--optical-set-id", required=True)
    convert.add_argument("--name")
    convert.add_argument("--block-selection", nargs="+", type=int)
    build = commands.add_parser("build", help="WRITE: construct a local snapshot from existing source files; never downloads")
    build.add_argument("--source-dir", required=True, help="Already-downloaded upstream source directory")
    build.add_argument("--upstream-commit", required=True, help="Exact fixed upstream Git commit")
    build.add_argument("--source-url", help="Exact official commit tree URL as provenance only; never fetched")
    build.add_argument("--no-activate", action="store_true", help="Build a version without changing the active snapshot")
    return parser


def _local_path(value: str, label: str) -> Path:
    if urlsplit(value).scheme.lower() in {"http", "https", "ftp", "file", "s3"}:
        raise ValueError(f"{label} must be a local filesystem path; URLs are not fetched")
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
    # Keep symlinks visible to the core's path-safety checks.
    return Path(value).expanduser()


def main(argv=None, *, stdout=None) -> int:
    args = _parser().parse_args(argv)
    stdout = stdout if stdout is not None else sys.stdout
    operation = args.operation
    effect = "write" if operation == "build" else "read"
    try:
        from .core.refractiveindex_local import (
            RefractiveIndexLocalStore, build_from_directory, default_store_root, writable_store_root,
        )

        store_root = _local_path(args.store, "store") if args.store is not None else (writable_store_root() if operation == "build" else default_store_root())
        if operation == "build":
            if args.snapshot_id is not None:
                raise ValueError("--snapshot-id selects an existing read version and cannot be used with build")
            result = build_from_directory(_local_path(args.source_dir, "source-dir"), store_root,
                upstream_commit=args.upstream_commit, source_url=args.source_url,
                activate=not args.no_activate)
        else:
            store = RefractiveIndexLocalStore(store_root, snapshot_id=args.snapshot_id)
            if operation == "status":
                result = store.status()
            elif operation == "search":
                result = store.search(args.query, catalog=args.catalog, shelf=args.shelf,
                    book=args.book, capability=args.capability, cataloged=args.cataloged,
                    offset=args.offset, limit=args.limit)
            elif operation == "detail":
                result = store.get_entry(args.entry_id, raw_limit=args.raw_limit)
            elif operation == "preview":
                result = store.preview(args.entry_id, block_indices=args.block_indices,
                    max_points=args.max_points)
            else:
                result = store.convert_entry(args.entry_id, optical_set_id=args.optical_set_id,
                    name=args.name, block_selection=args.block_selection)
        reply = {"ok": True, "operation": operation, "effect": effect, "result": result}
        code = 0
    except (OSError, ValueError, ImportError) as exc:
        reply = {"ok": False, "operation": operation, "error": {
            "code": getattr(exc, "code", "dependency_unavailable" if isinstance(exc, ImportError)
                            else "invalid_arguments"),
            "message": str(exc), "effect": "unknown" if effect == "write" else "none"}}
        code = 2
    except KeyboardInterrupt:
        reply = {"ok": False, "operation": operation, "error": {
            "code": "cancelled", "message": "Operation interrupted",
            "effect": "unknown" if effect == "write" else "none"}}
        code = 130
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False), file=stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
