"""Run frozen film synthesis requests without GUI or live material-catalog access."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
from threading import Event, Lock, Thread
import time
from typing import Any

from .core.film_synthesis_io import (
    RESULT_KIND, SCHEMA_VERSION,
    film_synthesis_request_from_payload, film_synthesis_result_to_payload,
    prepare_film_synthesis_payload,
)


def _json_default(value: Any):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_json_default) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_exclusive(path: Path, payload: Any) -> None:
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_json_default) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _parallel_batch_argument(value: str) -> int | None:
    if value.strip().lower() == "auto":
        return None
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("parallel batch size must be a positive integer or auto") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("parallel batch size must be a positive integer or auto")
    return count


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _execute(request_id, envelope, cancel: Event, events: queue.Queue, checkpoint: Path | None):
    from .core.film_auto_synthesis import synthesize_film_design

    canonical, structure_metadata = prepare_film_synthesis_payload(
        None, envelope, progress_callback=events.put)
    _, request, _ = film_synthesis_request_from_payload(canonical)

    def progress(value):
        events.put(asdict(value))

    def snapshot(candidate_design, training_score, validation_score, audit):
        # The core invokes this only after its explicit candidate audit.
        record = {"schema_version": SCHEMA_VERSION, "kind": RESULT_KIND, "request_id": request_id,
                  "checkpoint_state": "audited", "design": candidate_design,
                  "training_score": training_score, "validation_score": validation_score,
                  "audit": audit, "updated_at": _timestamp()}
        if checkpoint is not None:
            _atomic_json(checkpoint, record)

    result = synthesize_film_design(None, request, cancel_event=cancel,
                                    progress_callback=progress, snapshot_callback=snapshot)
    return result, structure_metadata


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "targets_met reports original nominal bounds; solver_acceptable reports the separate "
        "acceptable_normalized_error tolerance, not a target relaxation or manufacturing margin. "
        "Synthesis-only strict_upper_bound_objective_indices selects < instead of inclusive <=."))
    parser.add_argument("--request", type=Path, help="Frozen request JSON; omitted reads stdin")
    parser.add_argument("--output", type=Path, help="New result JSON, never overwrite; omitted writes stdout")
    parser.add_argument("--checkpoint", type=Path, help="Atomically updated best audited-candidate snapshot")
    parser.add_argument("--cancel-file", type=Path, help="Existing file requests cooperative cancellation")
    parser.add_argument("--plan", action="store_true", help="Freeze and expand seeds, print a CLI-ready request, and do not search")
    parser.add_argument("--parallel-batch-size", type=_parallel_batch_argument, default=argparse.SUPPRESS,
        metavar="auto|N", help="Override the hybrid logical batch, not promised concurrent workers; AUTO freezes requested/available CPU, with actual memory/candidate/budget admission")
    parser.add_argument("--heartbeat-interval", type=float, default=2.0)
    parser.add_argument("--stalled-after-seconds", type=float, default=30.0)
    parser.add_argument("--unresponsive-after-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    for name in ("heartbeat_interval", "stalled_after_seconds", "unresponsive_after_seconds"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")

    cancel = Event()
    thread = None
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        raw = args.request.read_text(encoding="utf-8-sig") if args.request else sys.stdin.read()
        envelope = json.loads(raw)
        if hasattr(args, "parallel_batch_size"):
            # Validate the original kind/fields first; an override cannot turn
            # an unrelated or malformed request into an accepted synthesis.
            film_synthesis_request_from_payload(envelope, require_frozen=False)
            envelope["request"] = dict(envelope["request"], parallel_batch_size=args.parallel_batch_size)
        if args.plan:
            if args.output is not None or args.checkpoint is not None or args.cancel_file is not None:
                raise ValueError("--plan cannot be combined with output, checkpoint, or cancel-file options")
            canonical, structure_metadata = prepare_film_synthesis_payload(None, envelope)
            response = {"ok": True, "request": canonical,
                        "physical_structure_metadata": list(structure_metadata)}
            sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            sys.stdout.flush()
            return 0
        request_id, _, _ = film_synthesis_request_from_payload(envelope, require_frozen=False)
        paths = [path for path in (args.request, args.output, args.checkpoint, args.cancel_file) if path is not None]
        resolved = [path.resolve() for path in paths]
        if len(set(resolved)) != len(resolved):
            raise ValueError("request, output, checkpoint, and cancel-file paths must be different")
        if args.output is not None and args.output.exists():
            raise FileExistsError(f"output already exists; select a new result path: {args.output}")
        if args.checkpoint is not None and args.checkpoint.exists():
            try:
                existing_checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("existing checkpoint is not valid JSON; select a new checkpoint path") from exc
            if (existing_checkpoint.get("kind") != RESULT_KIND or
                    existing_checkpoint.get("request_id") != request_id or
                    existing_checkpoint.get("checkpoint_state") != "audited"):
                raise ValueError("existing checkpoint does not belong to this request; select a new path")
        events: queue.Queue = queue.Queue()
        completed: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                completed.put((True, _execute(request_id, envelope, cancel, events, args.checkpoint)))
            except BaseException as exc:
                completed.put((False, exc))

        thread = Thread(target=worker, name="film-synthesis-worker", daemon=False)
        thread.start()
        sequence = 0
        lock = Lock()
        latest = {"stage": "starting", "cycle": 0, "evaluations": 0,
                  "layer_count": 0, "best_max_normalized_error": None}
        last_progress = time.monotonic()
        last_heartbeat = last_progress
        last_activity = last_progress
        last_signature = None
        error = None
        finished = None

        def emit(value):
            nonlocal sequence
            with lock:
                sequence += 1
                value = {"sequence": sequence, "timestamp": _timestamp(), **value}
                print(json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default),
                      file=sys.stderr, flush=True)

        while finished is None:
            if args.cancel_file is not None and args.cancel_file.exists():
                cancel.set()
            try:
                while True:
                    latest = events.get_nowait()
                    last_activity = time.monotonic()
                    signature = (latest.get("stage"), latest.get("cycle"), latest.get("evaluations"),
                                 latest.get("layer_count"), latest.get("best_max_normalized_error"))
                    if signature != last_signature:
                        last_signature = signature
                        last_progress = time.monotonic()
                    emit({"kind": "progress", **latest, "cancel_requested": cancel.is_set()})
            except queue.Empty:
                pass
            now = time.monotonic()
            age = now - last_progress
            if thread.is_alive() and now - last_heartbeat >= args.heartbeat_interval:
                emit({"kind": "heartbeat", **latest, "algorithm_progress_age_seconds": round(age, 3),
                      "worker_activity_age_seconds": round(now - last_activity, 3),
                      "stalled": age >= args.stalled_after_seconds,
                      "unresponsive": now - last_activity >= args.unresponsive_after_seconds,
                      "cancel_requested": cancel.is_set()})
                last_heartbeat = now
            try:
                finished = completed.get(timeout=min(args.heartbeat_interval, 0.25))
            except queue.Empty:
                continue

        try:
            while True:
                latest = events.get_nowait()
                last_activity = time.monotonic()
                emit({"kind": "progress", **latest, "cancel_requested": cancel.is_set()})
        except queue.Empty:
            pass

        ok, value = finished
        if not ok:
            raise value
        result, structure_metadata = value
        payload = film_synthesis_result_to_payload(request_id, result,
            audit={"physical_evaluations": result.audit_physical_evaluations,
                   "wavelength_points": result.audit_wavelength_points},
            physical_structure_metadata=structure_metadata)
        if args.output is None:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                        default=_json_default) + "\n")
            sys.stdout.flush()
        else:
            _write_exclusive(args.output, payload)
        emit({"kind": "acceptance", "acceptance_semantics": payload["acceptance_semantics"],
              "task_state": payload["result"]["state"],
              "targets_met": payload["result"]["targets_met"],
              "solver_acceptable": payload["result"]["solver_acceptable"],
              "best_candidate_audit": payload["best_candidate_audit"],
              "note": "Original nominal bounds and solver tolerance are separate; no manufacturing margin is added."})
        return 130 if result.state == "cancelled" else 2 if result.state == "failed" else 0
    except Exception as exc:
        failure = {"schema_version": SCHEMA_VERSION, "kind": RESULT_KIND,
                   "request_id": locals().get("request_id"), "ok": False,
                   "error": {"type": type(exc).__name__, "message": str(exc)}}
        print(json.dumps(failure, ensure_ascii=False), file=sys.stdout, flush=True)
        return 2
    finally:
        if thread is not None and thread.is_alive():
            cancel.set()
            thread.join()
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
