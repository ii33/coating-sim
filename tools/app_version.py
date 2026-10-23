#!/usr/bin/env python
"""Check or bump the human-readable CoatSim internal publication version."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ruff E402 is intentional: the script must work when launched by file path.
from coat_sim.version import (  # noqa: E402
    BUILD_IDENTITY,
    VERSION_FILE,
    next_internal_version,
    parse_internal_version,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="show the packaged version and optional Git SHA")
    check = subparsers.add_parser("check", help="validate the authoritative version file")
    check.add_argument("--previous-version", help="require advancement beyond the last published version")
    subparsers.add_parser("bump", help="advance the UTC+8 daily publication ordinal")
    args = parser.parse_args(argv)

    if args.command == "show":
        print(f"{BUILD_IDENTITY.version} (Git {BUILD_IDENTITY.git_sha or 'not recorded'})")
        return 0

    if args.command == "check":
        try:
            current = VERSION_FILE.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            print(f"Cannot read {VERSION_FILE}: {exc}", file=sys.stderr)
            return 1
        parsed = parse_internal_version(current)
        if parsed is None:
            print(f"Invalid internal version in {VERSION_FILE}: {current!r}", file=sys.stderr)
            return 1
        if args.previous_version is not None:
            previous = parse_internal_version(args.previous_version)
            if previous is None:
                print(f"Invalid previous internal version: {args.previous_version!r}", file=sys.stderr)
                return 1
            if parsed <= previous:
                print(f"Internal version must advance beyond {args.previous_version}: {current}", file=sys.stderr)
                return 1
        print(f"Internal version is valid: {current}")
        return 0

    if args.command == "bump":
        try:
            current = VERSION_FILE.read_text(encoding="utf-8").strip()
            bumped = next_internal_version(current)
            VERSION_FILE.write_text(bumped + "\n", encoding="utf-8", newline="\n")
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"Cannot bump internal version: {exc}", file=sys.stderr)
            return 1
        print(f"Internal version: {current} -> {bumped}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
