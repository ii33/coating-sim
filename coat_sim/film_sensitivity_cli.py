"""Run frozen thickness sensitivity analysis or robustness optimization without Qt."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sys
from threading import Event

from .core.film_sensitivity import (
    execute_film_sensitivity_request, film_sensitivity_request_from_payload,
    film_sensitivity_result_to_payload,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, help="Request JSON; omitted reads stdin")
    parser.add_argument("--output", type=Path, help="New result JSON; never overwrites; omitted writes stdout")
    args = parser.parse_args(argv)
    cancel = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        if args.output is not None and args.output.exists():
            raise ValueError("output already exists; select a new result path")
        source = args.request.read_text(encoding="utf-8-sig") if args.request else sys.stdin.read()
        request = film_sensitivity_request_from_payload(json.loads(source))
        if request.frozen_material_snapshot is None:
            raise ValueError("CLI requires a complete frozen snapshot; it never reads the live material catalog")
        def progress(value):
            print(json.dumps(asdict(value), ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
        result = execute_film_sensitivity_request(None, request, cancel_event=cancel, progress_callback=progress)
        encoded = json.dumps(dict(ok=result.state not in ("failed", "cancelled"),
            result=film_sensitivity_result_to_payload(result)), ensure_ascii=False, allow_nan=False) + "\n"
        if args.output is None:
            sys.stdout.write(encoded)
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
            print(json.dumps(dict(output=str(args.output), state=result.state), ensure_ascii=False))
        return 130 if result.state == "cancelled" else 2 if result.state == "failed" else 3 if result.state == "partial" else 0
    except (KeyError, TypeError, ValueError, ArithmeticError, OSError) as exc:
        print(json.dumps(dict(ok=False, error=str(exc)), ensure_ascii=False))
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
