"""JSON-only, read-only ITO calculation CLI."""
from __future__ import annotations

import argparse
import json
import sys

from coat_sim.core.ito_electrical import calculate_ito_electrical


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("solve",))
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("ITO scalar input exceeds 1 MiB")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        if "operation" in request and request["operation"] != args.operation:
            raise ValueError("CLI and payload operations disagree")
        request["operation"] = args.operation
        result = calculate_ito_electrical(request)
        json.dump({"ok": True, "result": result}, sys.stdout, ensure_ascii=False, allow_nan=False)
        sys.stdout.write("\n")
        return 0
    except (ValueError, TypeError, ArithmeticError) as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, ensure_ascii=False, allow_nan=False)
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
