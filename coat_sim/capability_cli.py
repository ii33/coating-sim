"""JSON capabilities without a desktop: python -m coat_sim.capability_cli."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

from .core.api import CoatService
from .core.capabilities import CapabilityService, CapabilityError

MAX_ARGUMENT_BYTES = 32 * 1024 * 1024


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field: " + key)
        value[key] = item
    return value


def _arguments(raw):
    if len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise CapabilityError("invalid_arguments", "arguments exceed 32 MiB")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (ValueError, TypeError) as exc:
        raise CapabilityError("invalid_arguments", "arguments must be finite JSON with unique fields") from exc
    if not isinstance(value, dict):
        raise CapabilityError("invalid_arguments", "arguments must be a JSON object")
    return value


def main(argv=None, *, service=None, stdin=None, stdout=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Explicit local project root; all domain paths remain confined to it")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="Discover finite operations; grants no new permissions")
    call = commands.add_parser("call")
    call.add_argument("operation")
    call.add_argument("--arguments", help="JSON object; omit to read one object from stdin")
    args = parser.parse_args(argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    # --root is a local filesystem root. A URL must never be interpreted as a
    # local path and silently select the local scheduler for a remote request.
    if args.root is not None and urlsplit(args.root).scheme.lower() in {"http", "https"}:
        operation = "catalog" if args.command == "catalog" else args.operation
        error = CapabilityError(
            "unsupported_queue_route",
            "The capability CLI accepts local filesystem roots only; remote queue routing must be explicitly owned by its caller.",
            effect="none",
        )
        reply = {"ok": False, "operation": operation, "error": error.as_dict()}
        print(json.dumps(reply, ensure_ascii=False, allow_nan=False), file=stdout)
        return 2
    if service is None:
        service = CoatService(root=args.root, restrict_to_root=True)
    capabilities = CapabilityService(service)
    if args.command == "catalog":
        reply = {"ok": True, **capabilities.catalog()}
    else:
        try:
            raw = args.arguments if args.arguments is not None else stdin.read(MAX_ARGUMENT_BYTES + 1)
            reply = capabilities.execute(args.operation, _arguments(raw))
        except CapabilityError as exc:
            reply = {"ok": False, "operation": args.operation, "error": exc.as_dict()}
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False), file=stdout)
    return 0 if reply["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
