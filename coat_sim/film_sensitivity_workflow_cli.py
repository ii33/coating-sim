"""Run independent frozen ideal-sensitivity or thickness-error-table requests."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import signal
import sys
from threading import Event

from .core.film_result_service import atomic_film_json
from .core.film_sensitivity_workflow_jobs import (
    _api, workflow_for_request, workflow_request_from_payload,
    workflow_result_to_payload,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, help="Frozen request JSON; omitted reads stdin")
    parser.add_argument("--output", type=Path, help="New result JSON; never overwrites; omitted writes stdout")
    args = parser.parse_args(argv)
    cancel = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        if args.output is not None and args.output.exists():
            raise ValueError("output already exists; select a new report path")
        source = args.request.read_text(encoding="utf-8-sig") if args.request else sys.stdin.read()
        request = workflow_request_from_payload(json.loads(source))
        kind = workflow_for_request(request)
        if request.frozen_material_snapshot is None:
            raise ValueError("CLI requires frozen materials; it never reads the live material catalog")

        def progress(value):
            body = asdict(value) if is_dataclass(value) and not isinstance(value, type) else dict(value)
            print(json.dumps(body, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)

        result = getattr(_api(kind), "execute_" + kind + "_request")(
            None, request, cancel_event=cancel, progress_callback=progress)
        payload = {"ok": result.state == "completed", "result": workflow_result_to_payload(result)}
        if args.output is None:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        else:
            atomic_film_json(args.output, payload, overwrite=False)
            print(json.dumps({"output": str(args.output), "state": result.state}, ensure_ascii=False))
        return (130 if result.state == "cancelled" else 2 if result.state == "failed"
                else 3 if result.state == "partial" else 0)
    except (KeyError, TypeError, ValueError, ArithmeticError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
