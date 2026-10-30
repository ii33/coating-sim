"""Bounded, latest-only feedback snapshots for an active film reduction job.

This channel is display-only. It carries already-scored incumbent metadata and
physical thickness rows; it never evaluates optics or grants apply authority.
"""
from __future__ import annotations

from pathlib import Path
import math
import re
from threading import Condition, Event, Thread
import time
from typing import Any, Mapping

from .io import write_json

LIVE_KIND = "coat_sim.film_reduction_live"
LIVE_FILE = "film-reduction-live.json"
MAX_BYTES = 512 * 1024
MAX_LAYERS = 16_384
PendingSnapshot = tuple[int, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any],
                        Mapping[str, Any], str, int, int, str, str, str, bool]
_JOB = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _finite(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def _metric_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("layer_count", "total_thickness_nm", "min_thickness_nm",
              "max_normalized_error", "weighted_max_normalized_error",
              "weighted_rms_error", "targets_met", "validation_targets_met",
              "validation_max_normalized_error")
    result: dict[str, Any] = {key: value.get(key) for key in fields}
    if type(result["layer_count"]) is not int or result["layer_count"] < 0:
        raise ValueError("invalid reduction live layer count")
    for metric_key in fields[1:6] + ("validation_max_normalized_error",):
        raw = result[metric_key]
        if (not _finite(raw) or isinstance(raw, bool)
                or not isinstance(raw, (int, float)) or raw < 0):
            raise ValueError("invalid reduction live metric")
        result[metric_key] = float(raw)
    for key in ("targets_met", "validation_targets_met"):
        if type(result[key]) is not bool:
            raise ValueError("invalid reduction live target status")
    return result


def build_live_frame(*, job_id: str, input_digest: str, sequence: int,
                     request: Any, source_design: Mapping[str, Any],
                     best_design: Mapping[str, Any], baseline_metrics: Mapping[str, Any],
                     candidate_metrics: Mapping[str, Any], stage: str, state: str,
                     iteration: int, evaluations: int, termination_reason: str = "",
                     message: str = "") -> dict[str, Any]:
    """Build a detached compact frame away from the reduction/evaluation thread."""
    from coat_sim.core.film_reduction import _physical

    source_nm = _physical(request, source_design)
    best_nm = _physical(request, best_design)
    rows = []
    for side in ("front", "back"):
        key = side + "_layers"
        for index, row in enumerate(source_design.get(key, ())):
            identifier = str(row["id"])
            original = float(source_nm[identifier])
            deleted = identifier not in best_nm
            candidate = 0.0 if deleted else float(best_nm[identifier])
            rows.append({"id": identifier, "side": side, "index": index,
                "material_id": str(row.get("material_id", "")),
                "original_nm": original, "best_nm": candidate,
                "best_deleted": deleted,
                "delta_nm": None if deleted else candidate - original})
    if len(rows) > MAX_LAYERS:
        raise ValueError("reduction live layer list exceeds its bounded limit")
    return validate_live_frame({
        "schema_version": 1, "kind": LIVE_KIND, "job_id": job_id,
        "input_digest": input_digest, "sequence": sequence,
        "state": state, "termination_reason": termination_reason,
        "stage": stage, "message": message, "iteration": iteration,
        "evaluations": evaluations,
        "baseline": _metric_summary(baseline_metrics),
        "candidate": _metric_summary(candidate_metrics), "layers": rows,
    }, job_id=job_id, input_digest=input_digest)


def validate_live_frame(value: Any, *, job_id: str | None = None,
                        input_digest: str | None = None,
                        after_sequence: int = -1) -> dict[str, Any]:
    """Validate an exact sidecar frame and return detached JSON-safe data."""
    fields = {"schema_version", "kind", "job_id", "input_digest", "sequence",
        "state", "termination_reason", "stage", "message", "iteration",
        "evaluations", "baseline", "candidate", "layers"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != LIVE_KIND
            or not isinstance(value["job_id"], str) or _JOB.fullmatch(value["job_id"]) is None
            or not isinstance(value["input_digest"], str) or _SHA.fullmatch(value["input_digest"]) is None
            or (job_id is not None and value["job_id"] != job_id)
            or (input_digest is not None and value["input_digest"] != input_digest)
            or type(value["sequence"]) is not int or value["sequence"] < 0
            or type(after_sequence) is not int or after_sequence < -1):
        raise ValueError("invalid film reduction live identity")
    if value["sequence"] <= after_sequence:
        raise ValueError("stale film reduction live sequence")
    if value["state"] not in {"running", "completed", "cancelled", "failed"}:
        raise ValueError("invalid film reduction live state")
    for key, limit in (("stage", 240), ("message", 640), ("termination_reason", 120)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise ValueError("invalid film reduction live text")
    for key in ("iteration", "evaluations"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("invalid film reduction live counter")
    result = dict(value)
    for name in ("baseline", "candidate"):
        metrics = value[name]
        if not isinstance(metrics, Mapping):
            raise ValueError("invalid film reduction live metrics")
        result[name] = _metric_summary(metrics)
    rows = value["layers"]
    if not isinstance(rows, list) or len(rows) > MAX_LAYERS:
        raise ValueError("invalid film reduction live layers")
    checked = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if (not isinstance(row, Mapping) or set(row) != {"id", "side", "index", "material_id",
                "original_nm", "best_nm", "best_deleted", "delta_nm"}
                or not isinstance(row["id"], str) or not row["id"] or len(row["id"]) > 256
                or row["side"] not in ("front", "back")
                or type(row["index"]) is not int or row["index"] < 0
                or not isinstance(row["material_id"], str) or len(row["material_id"]) > 256
                or not _finite(row["original_nm"]) or row["original_nm"] < 0
                or not _finite(row["best_nm"]) or row["best_nm"] < 0
                or type(row["best_deleted"]) is not bool):
            raise ValueError("invalid film reduction live layer row")
        row_key = (row["side"], row["id"])
        if row_key in seen:
            raise ValueError("duplicate film reduction live layer id")
        seen.add(row_key)
        delta = row["delta_nm"]
        if row["best_deleted"]:
            if delta is not None or row["best_nm"] != 0:
                raise ValueError("invalid deleted layer display value")
        elif not _finite(delta) or not math.isclose(
                float(delta), float(row["best_nm"]) - float(row["original_nm"]),
                rel_tol=0.0, abs_tol=1e-8):
            raise ValueError("invalid layer thickness delta")
        checked.append(dict(row))
    if len(checked) != result["baseline"]["layer_count"]:
        raise ValueError("source layer count does not match live rows")
    if len(checked) - sum(row["best_deleted"] for row in checked) != result["candidate"]["layer_count"]:
        raise ValueError("candidate layer count does not match live rows")
    source_total = math.fsum(float(row["original_nm"]) for row in checked)
    best_total = math.fsum(float(row["best_nm"]) for row in checked)
    if (not math.isclose(source_total, result["baseline"]["total_thickness_nm"], rel_tol=1e-10, abs_tol=1e-7)
            or not math.isclose(best_total, result["candidate"]["total_thickness_nm"], rel_tol=1e-10, abs_tol=1e-7)):
        raise ValueError("layer totals do not match live metrics")
    result["layers"] = checked
    return result


class ReductionLivePublisher:
    """Asynchronous one-slot writer; physics callbacks only replace a pointer."""

    def __init__(self, directory: str | Path, job_id: str, input_digest: str,
                 request: Any, *, interval_s: float = 0.25, clock=time.monotonic):
        if (_JOB.fullmatch(job_id) is None or _SHA.fullmatch(input_digest) is None
                or not _finite(interval_s) or interval_s < 0):
            raise ValueError("invalid reduction live publisher identity")
        self.path = Path(directory) / LIVE_FILE
        self.job_id, self.input_digest, self.request = job_id, input_digest, request
        self.interval_s, self.clock = float(interval_s), clock
        self._condition = Condition()
        self._pending: PendingSnapshot | None = None
        self._next_sequence = 0
        self._written_sequence = -1
        self._last_write = -float("inf")
        self._closed = False
        self._terminal_offered = False
        self._flushed = Event()
        self._failed_sequence = -1
        self.snapshots_offered = 0
        self.snapshots_coalesced = 0
        self.snapshots_built = 0
        self.bytes_written = 0
        self.write_count = 0
        self.max_pending_depth = 0
        self.build_seconds = 0.0
        self.write_seconds = 0.0
        self.last_error = ""
        self._thread = Thread(target=self._run, name="film-reduction-feedback", daemon=True)
        self._thread.start()

    def _offer(self, *, source_design, best_design, baseline_metrics, candidate_metrics,
               stage, iteration, evaluations, state, termination_reason="", message="", force=False):
        with self._condition:
            if self._closed or self._terminal_offered:
                return -1
            sequence = self._next_sequence
            self._next_sequence += 1
            if self._pending is not None:
                self.snapshots_coalesced += 1
            self._pending = (sequence, source_design, best_design, baseline_metrics,
                candidate_metrics, str(stage), int(iteration), int(evaluations),
                str(state), str(termination_reason), str(message), bool(force))
            self.snapshots_offered += 1
            self.max_pending_depth = max(self.max_pending_depth, 1)
            if force:
                self._terminal_offered = True
                self._flushed.clear()
            self._condition.notify()
            return sequence

    def offer(self, **kwargs):
        """Queue references whose owner freezes them by replacement, never mutation.

        Core reduction satisfies this contract: proposals deep-copy the design,
        accepted incumbents rebind ``best``/``metric``, and the frozen baseline
        remains untouched. Building an owned snapshot here would add a large
        synchronous deepcopy to the evaluation callback.
        """
        return self._offer(**kwargs)

    def finish(self, **kwargs):
        sequence = self._offer(force=True, **kwargs)
        if sequence < 0:
            raise RuntimeError("reduction feedback terminal snapshot was already closed")
        if not self._flushed.wait(timeout=5.0):
            raise TimeoutError("reduction feedback terminal flush timed out")
        with self._condition:
            if self._written_sequence < sequence:
                raise OSError("reduction feedback terminal flush failed: "
                    + (self.last_error or "write did not complete"))
        return sequence

    @property
    def diagnostic(self):
        with self._condition:
            return self.last_error

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None and self._closed:
                    return
                pending_snapshot = self._pending
                assert pending_snapshot is not None
                wait_for = self.interval_s - (self.clock() - self._last_write)
                if not pending_snapshot[-1] and wait_for > 0:
                    self._condition.wait(timeout=wait_for)
                    continue
                pending, self._pending = pending_snapshot, None
            started = self.clock()
            sequence, source, best, baseline, candidate, stage, iteration, evaluations, state, reason, message, force = pending
            error = ""
            try:
                frame = build_live_frame(job_id=self.job_id, input_digest=self.input_digest,
                    sequence=sequence, request=self.request, source_design=source,
                    best_design=best, baseline_metrics=baseline, candidate_metrics=candidate,
                    stage=stage, state=state, iteration=iteration, evaluations=evaluations,
                    termination_reason=reason, message=message)
                self.build_seconds += max(0.0, self.clock() - started)
                self.snapshots_built += 1
                write_started = self.clock()
                write_json(self.path, frame, max_bytes=MAX_BYTES)
                self.write_seconds += max(0.0, self.clock() - write_started)
                self.bytes_written += self.path.stat().st_size
                self.write_count += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            with self._condition:
                self.last_error = error
                if not error:
                    self._written_sequence = max(self._written_sequence, sequence)
                    self._failed_sequence = -1
                else:
                    self._failed_sequence = sequence
                self._last_write = self.clock()
                if force:
                    self._flushed.set()
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(timeout=0.5)


class UnavailableReductionLivePublisher:
    """Non-fatal display sink when optional live output cannot be initialized."""

    def __init__(self, cause: BaseException):
        self._diagnostic = f"{type(cause).__name__}: {cause}"[:320]

    @property
    def diagnostic(self):
        return self._diagnostic

    def offer(self, **_kwargs):
        return -1

    def finish(self, **_kwargs):
        raise OSError(self._diagnostic)

    def close(self):
        return None
