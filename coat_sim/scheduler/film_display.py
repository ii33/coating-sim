"""Ephemeral, bounded film display transport, separate from durable job events.

The existing scheduler job directory owns two replace-in-place JSON snapshots.
They are observations/control only, never optimizer requests or result authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from .io import write_json

FILM_DISPLAY_EXECUTORS = frozenset({
    "film_optimization", "film_synthesis", "film_sensitivity", "film_ideal_sensitivity",
})
MAX_DISPLAY_BYTES = 128 * 1024
MAX_CONTROL_BYTES = 4096
CONFIG_KEY = "film_display_config"
CONTROL_KIND = "coat_sim.film_display_control"
PACKET_KIND = "coat_sim.film_live_display"
_JOB = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")


def _selection(value):
    try:
        angle_finite = (isinstance(value, dict) and isinstance(value.get("angle_deg"), (int, float))
                        and math.isfinite(value["angle_deg"]))
    except OverflowError:
        angle_finite = False
    if (not isinstance(value, dict) or set(value) != {"objective_index", "angle_deg", "metric"}
            or type(value["objective_index"]) is not int or not 0 <= value["objective_index"] < 1_000_000
            or isinstance(value["angle_deg"], bool) or not isinstance(value["angle_deg"], (int, float))
            or not angle_finite or not 0 <= value["angle_deg"] < 90
            or value["metric"] not in ("R", "T")):
        raise ValueError("invalid_film_display_selection")
    return {"objective_index": value["objective_index"],
            "angle_deg": float(value["angle_deg"]), "metric": value["metric"]}


def prepare_display_configuration(value=None):
    """Freeze display preferences only; never alter the physical request hash."""
    from coat_sim.core.film_candidate_display import score_config_to_payload
    if value is None:
        value = {"schema_version": 1, "generation": uuid4().hex, "score_config": {},
                 "enabled": False, "selection": {"objective_index": 0, "angle_deg": 0., "metric": "R"}}
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "generation", "score_config", "enabled", "selection"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["generation"], str) or _GENERATION.fullmatch(value["generation"]) is None
            or type(value["enabled"]) is not bool):
        raise ValueError("invalid_film_display_configuration")
    result = {"schema_version": 1, "generation": value["generation"],
              "score_config": score_config_to_payload(value["score_config"]),
              "enabled": value["enabled"], "selection": _selection(value["selection"])}
    if len(json.dumps(result, allow_nan=False).encode()) > MAX_CONTROL_BYTES:
        raise ValueError("film_display_configuration_too_large")
    return result


def display_configuration_for_job(row):
    """Old jobs derive one stable generation in both daemon and worker."""
    value = row.get(CONFIG_KEY)
    if value is None:
        identity = str(row.get("job_id", "")) + "\0" + str(row.get("input_digest", ""))
        value = {"schema_version": 1,
                 "generation": hashlib.sha256(("legacy-film-display\0" + identity).encode()).hexdigest()[:32],
                 "score_config": {}, "enabled": False,
                 "selection": {"objective_index": 0, "angle_deg": 0., "metric": "R"}}
    return prepare_display_configuration(value)


def _read_bounded(path, limit):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("film_display_symlink_not_owned")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("film_display_packet_too_large")
    return json.loads(data)


def _validated_control(value, *, job_id, input_digest, generation):
    required = {"schema_version", "kind", "job_id", "input_digest", "generation",
                "sequence", "enabled", "selection"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != CONTROL_KIND or value["job_id"] != job_id
            or value["input_digest"] != input_digest or value["generation"] != generation
            or type(value["enabled"]) is not bool or type(value["sequence"]) is not int
            or value["sequence"] < 0):
        raise ValueError("film_display_control_identity_mismatch")
    return {"sequence": value["sequence"], "enabled": value["enabled"],
            "selection": _selection(value["selection"])}


def validate_live_display_packet(value, *, job_id=None, input_digest=None, generation=None, context_id=None):
    from coat_sim.core.film_candidate_display import validate_candidate_display_packet
    try:
        if (not isinstance(value, dict) or set(value) != {
                "schema_version", "kind", "job_id", "input_digest", "generation", "control_sequence", "display"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != PACKET_KIND
                or not isinstance(value["job_id"], str) or _JOB.fullmatch(value["job_id"]) is None
                or not isinstance(value["input_digest"], str) or _SHA.fullmatch(value["input_digest"]) is None
                or not isinstance(value["generation"], str) or _GENERATION.fullmatch(value["generation"]) is None
                or type(value["control_sequence"]) is not int or value["control_sequence"] < 0):
            return None
        for key, expected in (("job_id", job_id), ("input_digest", input_digest), ("generation", generation)):
            if expected is not None and value[key] != expected:
                return None
        display = validate_candidate_display_packet(value["display"])
        if (display is None or display["scope"] != "training"
                or (context_id is not None and display["context_id"] != context_id)):
            return None
        if not display["enabled"] and any(
                display[role] is not None and display[role]["spectrum"] is not None for role in ("current", "best")):
            return None
        result = {**{key: value[key] for key in value if key != "display"}, "display": display}
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DISPLAY_BYTES:
            return None
        return json.loads(encoded)
    except (TypeError, ValueError, KeyError, OverflowError):
        return None


class WorkerDisplaySession:
    """One worker's optional display observer; bad transport never stops optics."""

    def __init__(self, directory, job_id, input_digest, configuration, *, clock=time.monotonic):
        self.directory = Path(directory)
        self.job_id, self.input_digest = job_id, input_digest
        self.configuration = prepare_display_configuration(configuration)
        self.generation = self.configuration["generation"]
        self.clock = clock
        self._control = {"sequence": 0, "enabled": self.configuration["enabled"],
                         "selection": dict(self.configuration["selection"])}
        self._last_check = -math.inf
        self._control_stamp = None
        self._control_valid = False
        self.observer = None
        self.dropped_packets = 0
        self.published_packets = 0
        self.published_bytes = 0

    def control(self):
        now = self.clock()
        if now - self._last_check >= .1:
            self._last_check = now
            try:
                path = self.directory / "film-display-control.json"
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self._control_stamp or not self._control_valid:
                    value = _read_bounded(path, MAX_CONTROL_BYTES)
                    control = _validated_control(value, job_id=self.job_id,
                        input_digest=self.input_digest, generation=self.generation)
                    if (control["sequence"] < self._control["sequence"]
                            or (control["sequence"] == self._control["sequence"] and control != self._control)):
                        raise ValueError("film_display_control_stale")
                    self._control = control
                    self._control_stamp = stamp
                    self._control_valid = True
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                self._control_valid = False
        return {"enabled": self._control_valid and self._control["enabled"], **self._control["selection"]}

    def publish(self, packet):
        try:
            raw = {"schema_version": 1, "kind": PACKET_KIND, "job_id": self.job_id,
                   "input_digest": self.input_digest, "generation": self.generation,
                   "control_sequence": self._control["sequence"], "display": packet}
            value = validate_live_display_packet(raw, job_id=self.job_id,
                input_digest=self.input_digest, generation=self.generation)
            if value is None:
                self.dropped_packets += 1
                return
            write_json(self.directory / "film-display.json", value, max_bytes=MAX_DISPLAY_BYTES)
            self.published_packets += 1
            self.published_bytes += len(json.dumps(value, allow_nan=False).encode())
        except (OSError, ValueError, TypeError):
            self.dropped_packets += 1

    def observer_for_request(self, request):
        try:
            return self._observer_for_request(request)
        except Exception:
            self.dropped_packets += 1
            return None

    def _observer_for_request(self, request):
        from coat_sim.core.film_candidate_display import CandidateDisplayObserver
        if self.observer is None:
            context = {"input_digest": self.input_digest, "generation": self.generation,
                       "score_config": self.configuration["score_config"], "scope": "training",
                       "objectives": [asdict(row) for row in request.objectives],
                       "strict_lower_bound_objective_indices": list(getattr(request, "strict_lower_bound_objective_indices", ())),
                       "strict_upper_bound_objective_indices": list(getattr(request, "strict_upper_bound_objective_indices", ()))}
            context_id = hashlib.sha256(json.dumps(context, allow_nan=False, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest()
            self.observer = CandidateDisplayObserver(context_id=context_id, objectives=request.objectives,
                score_config=self.configuration["score_config"], control_provider=self.control,
                publish_callback=self.publish, clock=self.clock,
                strict_lower_bound_objective_indices=getattr(request, "strict_lower_bound_objective_indices", ()),
                strict_upper_bound_objective_indices=getattr(request, "strict_upper_bound_objective_indices", ()))
        return self.observer

    def finish(self, state):
        if self.observer is not None:
            try:
                self.observer.finish(state=state)
            except Exception:
                self.dropped_packets += 1


class FilmDisplayRegistry:
    """Scheduler-memory latest slots; never insert spectra into queue/events DB."""

    def __init__(self, root):
        self.root = Path(root)
        self._states: dict[str, dict[str, Any]] = {}
        self.revision = 0

    def _state(self, row):
        job_id = row["job_id"]
        if job_id not in self._states:
            config = display_configuration_for_job(row)
            directory = self.root / "artifacts/runtime/scheduler/jobs" / job_id
            state = {"configuration": config, "input_digest": row["input_digest"],
                "sequence": 0, "enabled": config["enabled"], "selection": config["selection"],
                "packet_sequence": 0, "latest": None, "stamp": None, "context_id": None,
                "control_available": True, "active": row.get("state") in ("queued", "running")}
            try:
                restored = _validated_control(_read_bounded(directory / "film-display-control.json", MAX_CONTROL_BYTES),
                    job_id=job_id, input_digest=row["input_digest"], generation=config["generation"])
                state.update(restored)
            except FileNotFoundError:
                # Before first launch no control snapshot is expected. A worker
                # already launched may remember a higher sequence: never reset
                # that unknown epoch or falsely acknowledge a lower control.
                state["control_available"] = not (directory / "launch.json").exists()
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                state["control_available"] = False
            self._states[job_id] = state
        return self._states[job_id]

    def start(self, row, directory):
        if row.get("kind") not in FILM_DISPLAY_EXECUTORS:
            return
        try:
            state = self._state(row)
            if not state["control_available"]:
                return
            self._write_control(row["job_id"], state, Path(directory))
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass  # Display setup cannot prevent a submitted calculation.

    def _write_control(self, job_id, state, directory):
        value = {"schema_version": 1, "kind": CONTROL_KIND, "job_id": job_id,
                 "input_digest": state["input_digest"], "generation": state["configuration"]["generation"],
                 "sequence": state["sequence"], "enabled": state["enabled"], "selection": state["selection"]}
        write_json(directory / "film-display-control.json", value, max_bytes=MAX_CONTROL_BYTES)

    def control(self, row, *, input_digest, generation, enabled, selection, expected_sequence):
        if (row.get("kind") not in FILM_DISPLAY_EXECUTORS or row.get("state") not in ("queued", "running")
                or row.get("cancel_requested")):
            raise ValueError("film_display_control_not_active")
        if type(enabled) is not bool:
            raise ValueError("invalid_film_display_enabled")
        chosen = _selection(selection)
        state = self._state(row)
        if not state["control_available"]:
            raise ValueError("film_display_control_unavailable")
        if input_digest != state["input_digest"] or generation != state["configuration"]["generation"]:
            raise ValueError("film_display_control_identity_mismatch")
        if type(expected_sequence) is not int or expected_sequence < 0:
            raise ValueError("invalid_film_display_sequence")
        if expected_sequence != state["sequence"]:
            if expected_sequence < state["sequence"] and enabled == state["enabled"] and chosen == state["selection"]:
                return {"job_id": row["job_id"], "generation": generation, "input_digest": input_digest,
                        "sequence": state["sequence"], "enabled": enabled, "selection": chosen}
            raise ValueError("film_display_control_stale")
        directory = self.root / "artifacts/runtime/scheduler/jobs" / row["job_id"]
        next_state = {**state, "sequence": state["sequence"] + 1,
                      "enabled": enabled, "selection": chosen, "latest": None}
        if directory.is_dir():
            self._write_control(row["job_id"], next_state, directory)
        self._states[row["job_id"]] = next_state
        self.revision += 1
        return {"job_id": row["job_id"], "generation": generation, "input_digest": input_digest,
                "sequence": next_state["sequence"], "enabled": enabled, "selection": chosen}

    def poll(self, row, directory):
        if row.get("kind") not in FILM_DISPLAY_EXECUTORS:
            return
        try:
            state = self._state(row)
            path = Path(directory) / "film-display.json"
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == state["stamp"]:
                return
            state["stamp"] = stamp
            packet = validate_live_display_packet(_read_bounded(path, MAX_DISPLAY_BYTES), job_id=row["job_id"],
                input_digest=state["input_digest"], generation=state["configuration"]["generation"], context_id=state["context_id"])
            if (packet is not None and packet["control_sequence"] == state["sequence"]
                    and packet["display"]["sequence"] > state["packet_sequence"]
                    and packet["display"]["enabled"] == state["enabled"]
                    and packet["display"]["selection"] == state["selection"]):
                state["context_id"] = packet["display"]["context_id"]
                state["latest"] = packet
                state["packet_sequence"] = packet["display"]["sequence"]
                self.revision += 1
                return True
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass
        return False

    def attach_job(self, row):
        result = deepcopy(row)
        if row.get("kind") in FILM_DISPLAY_EXECUTORS:
            try:
                result[CONFIG_KEY] = display_configuration_for_job(row)
            except (ValueError, TypeError, KeyError):
                pass
        state = self._states.get(row.get("job_id"))
        if row.get("kind") in FILM_DISPLAY_EXECUTORS:
            try:
                if state is None and row.get("state") in ("queued", "running"):
                    state = self._state(row)
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                state = None
            result["film_display_control_sequence"] = state["sequence"] if state else 0
            result["film_display_control_available"] = bool(state and state["control_available"])
        if state and state["latest"] is not None:
            result["film_display"] = deepcopy(state["latest"])
        return result

    def attach_events(self, result):
        result = deepcopy(result)
        events = result.get("events", [])
        last = {row.get("job_id"): index for index, row in enumerate(events)}
        for index in last.values():
            events[index] = self.attach_job(events[index])
        # Display-only changes do not manufacture durable events or sequences.
        wanted = set(last) | {key for key, state in self._states.items() if state["active"]}
        result["film_displays"] = [deepcopy(state["latest"]) for key, state in self._states.items()
                                   if key in wanted and state["latest"] is not None]
        if isinstance(result.get("snapshot"), dict):
            result["snapshot"] = self.attach_snapshot(result["snapshot"])
        return result

    def attach_snapshot(self, result):
        result = deepcopy(result)
        result["jobs"] = [self.attach_job(row) for row in result.get("jobs", [])]
        return result

    def discard(self, job_id):
        self._states.pop(job_id, None)

    def finish(self, row):
        state = self._states.get(row.get("job_id"))
        if state is not None:
            state["active"] = row.get("state") not in ("succeeded", "failed", "cancelled")
