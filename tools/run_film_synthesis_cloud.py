"""Run a frozen film-synthesis bundle on a CPU host without desktop services."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import hashlib
import importlib.metadata
from importlib import import_module
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import ArrayLike


class _ScalarTrapezoid(Protocol):
    """SciPy boundary for the one-dimensional real spectra used here."""

    def __call__(self, y: ArrayLike, *, x: ArrayLike) -> float | np.float64: ...

THREAD_ENVIRONMENT = {name: "1" for name in (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")}


def _jsonable(value: Any) -> Any:
    def default(item):
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if hasattr(item, "item"):
            return item.item()
        raise TypeError(f"not strict JSON data: {type(item).__name__}")
    return json.loads(json.dumps(value, default=default, allow_nan=False))


def _canonical_hash(value: Any) -> str:
    data = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    data = json.dumps(_jsonable(value), indent=2, sort_keys=True, allow_nan=False)
    temp = path.with_name(path.name + ".partial")
    temp.write_text(data + "\n", encoding="utf-8")
    temp.replace(path)


REQUEST_CONTRACT_VERSION = 2


def _assert_current_request_shape(request: Any) -> None:
    """Reject historical requests before imports, source activation or work."""
    objective_fields = {
        "wavelength_min_nm", "wavelength_max_nm", "wavelength_samples", "angles_deg",
        "polarization", "metric", "pointwise", "mean", "weight", "normalization_scale",
    }
    constraint_fields = {
        "enabled", "mode", "target_value", "lower_bound", "upper_bound", "strict_lower", "strict_upper",
    }
    if (not isinstance(request, dict) or
            any(name in request for name in ("strict_lower_bound_objective_indices",
                                            "strict_upper_bound_objective_indices")) or
            not isinstance(request.get("objectives"), (tuple, list)) or not request["objectives"]):
        raise ValueError("Unsupported legacy request format; current schema 2 requires complete nested mean/pointwise criteria. Historical frozen suites are not migrated.")
    for objective in request["objectives"]:
        if (not isinstance(objective, dict) or set(objective) != objective_fields or
                any(not isinstance(objective[name], dict) or set(objective[name]) != constraint_fields
                    for name in ("mean", "pointwise"))):
            raise ValueError("Unsupported legacy request format; current schema 2 requires complete nested mean/pointwise criteria. Historical frozen suites are not migrated.")


def _current_objectives(request: dict[str, Any]):
    _assert_current_request_shape(request)
    from coat_sim.core.film_objective_payload import objective_from_payload
    return tuple(objective_from_payload(row) for row in request["objectives"])


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("kind") not in {"film_synthesis_pure_cpu_bundle", "film_synthesis_git_requests"} or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported cloud bundle manifest")
    if not isinstance(manifest.get("entries"), list) or not manifest["entries"]:
        raise ValueError("Bundle manifest needs at least one frozen case")
    if (not manifest.get("smoke_bundle")
            and len(manifest["entries"]) != manifest.get("suite_request_count")):
        raise ValueError("Full bundle does not contain every frozen representative-suite case")
    for entry in manifest["entries"]:
        _assert_current_request_shape(entry.get("request") if isinstance(entry, dict) else None)
    return manifest


def _choose(manifest: dict[str, Any], case: str | None) -> list[dict[str, Any]]:
    entries = manifest["entries"]
    if case is None:
        return entries
    selected = [entry for entry in entries if entry["case"] == case]
    if not selected:
        raise ValueError(f"Unknown case {case!r}; available: {', '.join(e['case'] for e in entries)}")
    return selected


def _mode_for_request(request: dict[str, Any], mode: str) -> None:
    if mode not in {"clustered", "v23_matching"}:
        raise ValueError("mode must be clustered or v23_matching")
    if "ar_structure_mode" not in request:
        raise ValueError("Frozen request does not include ar_structure_mode; export from the current source first")
    request["ar_structure_mode"] = mode


def _mean_metrics(request: dict[str, Any], score: Any) -> dict[str, Any]:
    """Fixed dense-grid summaries; offsets/extrema never enter these means."""
    import numpy as np
    trapezoid = cast(_ScalarTrapezoid, import_module("scipy.integrate").trapezoid)
    body = _jsonable(score) if score is not None else {}
    rows = body.get("coverage", []) if isinstance(body, dict) else []
    _current_objectives(request)
    objectives = request["objectives"]
    multiplier = request.get("validation_samples_multiplier", 1)
    groups = []
    for objective_index, objective in enumerate(objectives):
        metric = str(objective.get("metric", "unspecified"))
        lo, hi = float(objective["wavelength_min_nm"]), float(objective["wavelength_max_nm"])
        original_count = int(objective["wavelength_samples"])
        count = original_count if original_count == 1 else (original_count - 1) * multiplier + 1
        waves = [lo] if count == 1 else [float(x) for x in np.linspace(lo, hi, count)]
        for angle in objective["angles_deg"]:
            for polarization in (objective["polarization"],):
                actual_by_wave = {}
                for row in rows:
                    if (row.get("objective_index") == objective_index
                            and row.get("angle_deg") == angle):
                        actual_by_wave = dict(zip(row["wavelength_nm"], row["actual_values"], strict=True))
                        break
                missing = [wave for wave in waves if wave not in actual_by_wave]
                if missing:
                    groups.append({"objective_index": objective_index, "metric": metric,
                        "angle_deg": angle,
                        "polarization": polarization, "available": False,
                        "missing_dense_points": len(missing)})
                    continue
                values = [actual_by_wave[wave] for wave in waves]
                if count == 1:
                    mean_lambda = mean_loglambda = values[0]
                    measure = "discrete_line_actual"
                else:
                    from coat_sim.core.film_objective_criteria import wavelength_measure
                    mean_lambda = float(wavelength_measure(waves, lo, hi) @ np.asarray(values))
                    log_waves = np.log(np.asarray(waves, dtype=float))
                    mean_loglambda = float(trapezoid(values, x=log_waves) / (log_waves[-1] - log_waves[0]))
                    measure = "fixed_dense_grid_trapezoid"
                groups.append({"objective_index": objective_index, "metric": metric,
                    "angle_deg": angle,
                    "polarization": polarization, "available": True,
                    "mean_lambda": mean_lambda, "mean_loglambda": mean_loglambda,
                    "measure": measure, "dense_points": count})
    available = [group for group in groups if group.get("available")]
    by_metric = {}
    for metric in dict.fromkeys(group["metric"] for group in groups):
        metric_groups = [group for group in groups if group["metric"] == metric]
        metric_available = [group for group in metric_groups if group.get("available")]
        metric_complete = bool(metric_groups) and len(metric_available) == len(metric_groups)
        by_metric[metric] = {
            "available": metric_complete,
            "mean_lambda": (sum(group["mean_lambda"] for group in metric_available)
                            / len(metric_available) if metric_complete else None),
            "mean_loglambda": (sum(group["mean_loglambda"] for group in metric_available)
                               / len(metric_available) if metric_complete else None),
            "group_count": len(metric_groups),
            "reason": None if metric_complete else "missing_dense_points",
        }
    metric_kinds = set(by_metric)
    combined_complete = (bool(groups) and len(available) == len(groups)
                         and len(metric_kinds) == 1)
    combined_reason = (None if combined_complete else
        ("mixed_metrics_have_no_combined_physical_mean" if len(metric_kinds) > 1
         else "missing_dense_points"))
    return {"kind": "descriptive_not_acceptance", "grid": "fixed_audit_dense_only",
        "extrema_and_half_step_offsets_included": False,
        "equal_weight_across_angle_polarization_groups": True,
        "mean_lambda": (sum(g["mean_lambda"] for g in available) / len(available)
                        if combined_complete else None),
        "mean_loglambda": (sum(g["mean_loglambda"] for g in available) / len(available)
                           if combined_complete else None),
        "metric": next(iter(metric_kinds)) if len(metric_kinds) == 1 else None,
        "reason": combined_reason,
        "by_metric": by_metric,
        "groups": groups}


def _raw_bounds(request: dict[str, Any], score: Any, *, scope: str) -> dict[str, Any]:
    """Report the production nominal verdict from complete current evidence."""
    from types import SimpleNamespace
    import numpy as np
    from coat_sim.core.film_nominal_acceptance import complete_original_score, nominal_condition_pass
    from coat_sim.core.film_objective_payload import condition_from_payload

    if scope not in {"training", "audit"}:
        raise ValueError("Unknown score grid scope")
    objectives = _current_objectives(request)
    body = _jsonable(score) if score is not None else {}
    if not isinstance(body, dict) or not isinstance(body.get("errors"), list):
        return {"available": False, "passed": None, "condition_count": 0}
    criteria_request = SimpleNamespace(objectives=objectives,
        acceptable_normalized_error=request.get("acceptable_normalized_error", 0.),
        validation_samples_multiplier=request.get("validation_samples_multiplier", 1))
    complete = complete_original_score(criteria_request, body, independent_audit=scope == "audit")
    required: set[tuple[int, float, str, float | None]] = set()
    raw_required = dense_count = offset_count = 0
    for index, objective in enumerate(objectives):
        count = objective.wavelength_samples
        if scope == "audit" and count > 1:
            count = (count - 1) * criteria_request.validation_samples_multiplier + 1
        dense = list(map(float, np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, count)))
        waves = list(dense)
        dense_count += count * len(objective.angles_deg)
        if scope == "audit" and count > 1:
            step = (objective.wavelength_max_nm - objective.wavelength_min_nm) / (count - 1)
            offsets = list(map(float, np.linspace(dense[0] + step / 2, dense[-1] - step / 2, count - 1)))
            waves.extend(offsets)
            offset_count += len(offsets) * len(objective.angles_deg)
        raw_required += len(waves) * len(objective.angles_deg)
        for angle in objective.angles_deg:
            if objective.mean.enabled:
                required.add((index, angle, "mean", None))
            if objective.pointwise.enabled:
                required.update((index, angle, "pointwise", wave) for wave in waves)
    seen: set[tuple[int, float, str, float | None]] = set()
    invalid = []
    violating = []
    for saved in body["errors"]:
        try:
            row = condition_from_payload(saved)
            identity = row.objective_index, row.angle_deg, row.reduction, row.wavelength_nm
            if identity in seen:
                invalid.append("duplicate_condition")
            seen.add(identity)
            objective = objectives[row.objective_index]
            constraint = getattr(objective, row.reduction)
            if not constraint.enabled:
                invalid.append("disabled_condition")
            if not nominal_condition_pass(constraint, row.actual):
                violating.append(saved)
        except (TypeError, ValueError, KeyError, AttributeError, IndexError):
            invalid.append("invalid_current_condition")
    missing = required - seen
    unexpected = seen - required if scope == "training" else set()
    audit_valid = scope != "audit"
    if scope == "audit":
        audit = body.get("audit")
        if isinstance(audit, dict):
            fields = ("dense_points", "offset_points", "unique_condition_points", "physical_evaluations",
                      "wavelength_points", "extra_points", "completed_extrema", "bracket_count")
            audit_valid = (all(type(audit.get(name)) is int and audit[name] >= 0 for name in fields)
                and audit["dense_points"] == dense_count and audit["offset_points"] == offset_count
                and audit["extra_points"] == audit["wavelength_points"] - dense_count
                and audit["completed_extrema"] == audit["bracket_count"]
                and audit.get("continuous_certificate") is False
                and audit.get("rms_measure") == "fixed_dense_grid_only"
                and isinstance(audit.get("objective_status"), list)
                and len(audit["objective_status"]) == len(objectives))
    if not complete:
        invalid.append("incomplete_or_inconsistent_current_criteria_evidence")
    complete = complete and audit_valid
    return {"available": complete, "passed": complete and not violating,
        "scope": "saved_current_criteria_only_no_continuous_certificate", "grid_scope": scope,
        "condition_count": len(body["errors"]), "required_condition_count": len(required),
        "required_raw_point_count": raw_required,
        "missing_required_conditions": len(missing), "unexpected_condition_count": len(unexpected),
        "invalid_conditions": invalid[:8], "violating_conditions": len(violating),
        "audit_metadata_valid": audit_valid, "worst_examples": violating[:8]}


def _loaded_git_paths(root: Path) -> dict[str, str]:
    loaded = {}
    for name, module in list(sys.modules.items()):
        if not (name in {"coat_sim", "tools"} or name.startswith(("coat_sim.", "tools."))):
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            for location in getattr(module, "__path__", ()):
                if not Path(location).resolve().is_relative_to(root):
                    raise ValueError(f"Foreign source package already loaded: {name}")
            continue
        source = Path(filename).resolve()
        if not source.is_relative_to(root):
            raise ValueError(f"Foreign algorithm/tool module already loaded: {name}")
        loaded[name] = source.relative_to(root).as_posix()
    return loaded


def _activate_git_root(root: Path) -> None:
    root = root.resolve()
    _loaded_git_paths(root)
    sys.path[:] = [str(root), *(entry for entry in sys.path if entry != str(root))]


def _source_fingerprint(bundle_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("kind") == "film_synthesis_git_requests":
        _activate_git_root(bundle_root)
        from tools.film_synthesis_git_source import verify_git_source
        # verify_git_source performs runtime validation of this JSON boundary.
        identity = verify_git_source(bundle_root, cast(str, manifest.get("_expected_commit")),
            cast(list[str], manifest.get("git_source_paths")), required_paths=(
                manifest["_manifest_relative_path"], "tools/run_film_synthesis_cloud.py",
                "tools/film_synthesis_git_source.py", "coat_sim/__init__.py", "coat_sim/core/__init__.py",
                "coat_sim/core/film_auto_synthesis.py", "coat_sim/core/film_synthesis_parallel.py"))
        loaded = _loaded_git_paths(bundle_root)
        if any(relative not in identity["source_hashes"] for relative in loaded.values()):
            raise ValueError("Loaded algorithm/tool module is outside verified Git source paths")
        runner_relative = Path(__file__).resolve().relative_to(bundle_root).as_posix()
        if runner_relative != "tools/run_film_synthesis_cloud.py":
            raise ValueError("Actual runner is outside the selected Git checkout")
        identity["loaded_python_sources_verified"] = loaded
        return identity
    hashes = manifest.get("bundle_source_files", {})
    actual = {}
    for relative, expected in hashes.items():
        path = bundle_root / relative
        if not path.is_file():
            raise ValueError(f"Bundle source missing: {relative}")
        actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual[relative] != expected:
            raise ValueError(f"Bundle source changed after export: {relative}")
    digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != manifest.get("bundle_source_sha256"):
        raise ValueError("Bundle source identity differs from frozen manifest")
    return {"bundle_source_sha256": digest, "source_hashes": actual}


class ProcessCPUObserver:
    """Poll this runner's descendants and report observed process CPU lower bounds."""
    def __init__(self):
        self.pid = os.getpid()
        self.rows: dict[tuple[int, str], dict[str, Any]] = {}
        self.error: str | None = None
        self.platform: str | None = None
        self.handles: dict[int, Any] = {}
        if os.name == "nt":
            self._init_windows()
        elif sys.platform.startswith("linux") and Path("/proc").is_dir():
            self.platform = "linux_proc"
        else:
            self.platform = None
            self.error = "process CPU observation unavailable on this platform"

    def _init_windows(self):
        try:
            import ctypes as c
            from ctypes import wintypes as w
            self.c, self.w = c, w
            windows_library = getattr(c, "WinDLL", None)
            if windows_library is None:
                raise OSError("Windows DLL loader unavailable")
            self.kernel = windows_library("kernel32", use_last_error=True)
            class Entry(c.Structure):
                _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("dwDefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                    ("szExeFile", w.WCHAR * 260)]
            self.Entry = Entry
            for name, args, result in (
                ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
                ("Process32FirstW", [w.HANDLE, c.POINTER(Entry)], w.BOOL),
                ("Process32NextW", [w.HANDLE, c.POINTER(Entry)], w.BOOL),
                ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
                ("CloseHandle", [w.HANDLE], w.BOOL),
                ("GetProcessTimes", [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL)):
                method = getattr(self.kernel, name)
                method.argtypes, method.restype = args, result
            self.platform = "windows_get_process_times"
        except Exception as exc:
            self.platform, self.error = None, f"Windows observer initialization failed: {type(exc).__name__}"

    @staticmethod
    def _linux_stat(path: Path):
        try:
            content = path.read_text(encoding="ascii")
            tail = content[content.rfind(")") + 2:].split()
            return {"pid": int(path.parent.name), "ppid": int(tail[1]),
                    "user_ticks": int(tail[11]), "system_ticks": int(tail[12]),
                    "start_ticks": int(tail[19])}
        except (OSError, ValueError, IndexError):
            return None

    def poll(self):
        if self.error:
            return
        if self.platform == "linux_proc":
            processes = {}
            try:
                for path in Path("/proc").glob("[0-9]*/stat"):
                    row = self._linux_stat(path)
                    if row:
                        processes[row["pid"]] = row
            except OSError as exc:
                self.error = f"/proc scan failed: {type(exc).__name__}"
                return
            owned = {self.pid}
            while True:
                expanded = owned | {pid for pid, row in processes.items() if row["ppid"] in owned}
                if expanded == owned:
                    break
                owned = expanded
            system_config = getattr(os, "sysconf", None)
            if system_config is None:
                self.error = "Linux process clock observation unavailable"
                return
            ticks = system_config("SC_CLK_TCK")
            for pid in owned - {self.pid}:
                row = processes.get(pid)
                if not row:
                    continue
                key = (pid, str(row["start_ticks"]))
                self.rows[key] = {"pid": pid, "identity": row["start_ticks"],
                    "kernel_cpu_seconds": row["system_ticks"] / ticks,
                    "user_cpu_seconds": row["user_ticks"] / ticks}
            return
        if self.platform == "windows_get_process_times":
            c, w = self.c, self.w
            snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
            if snapshot == c.c_void_p(-1).value:
                return
            parents = {}
            entry = self.Entry()
            entry.dwSize = c.sizeof(entry)
            try:
                more = self.kernel.Process32FirstW(snapshot, c.byref(entry))
                while more:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    more = self.kernel.Process32NextW(snapshot, c.byref(entry))
            finally:
                self.kernel.CloseHandle(snapshot)
            owned = {self.pid}
            while True:
                expanded = owned | {pid for pid, parent in parents.items() if parent in owned}
                if expanded == owned:
                    break
                owned = expanded
            for pid in owned - {self.pid}:
                if pid not in self.handles:
                    handle = self.kernel.OpenProcess(0x1000, False, pid)
                    if handle:
                        self.handles[pid] = handle
            for pid, handle in self.handles.items():
                creation, exit_time, kernel_time, user_time = (w.FILETIME() for _ in range(4))
                if self.kernel.GetProcessTimes(handle, c.byref(creation), c.byref(exit_time),
                        c.byref(kernel_time), c.byref(user_time)):
                    def value(stamp):
                        return (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime
                    identity = str(value(creation))
                    self.rows[(pid, identity)] = {"pid": pid, "identity": identity,
                        "kernel_cpu_seconds": value(kernel_time) / 1e7,
                        "user_cpu_seconds": value(user_time) / 1e7}

    def finish(self, actual_worker_pids=()):
        self.poll()
        for handle in self.handles.values():
            self.kernel.CloseHandle(handle)
        records = sorted(self.rows.values(), key=lambda row: row["pid"])
        by_pid = {row["pid"]: row for row in records}
        worker_ids = [int(pid) for pid in actual_worker_pids if isinstance(pid, int)]
        return {"available": not bool(self.error), "method": self.platform, "error": self.error,
            "scope": "observed_descendant_process_cpu_lower_bound_polling_may_miss_short_children",
            "actual_worker_pids": worker_ids,
            "worker_process_cpu": [by_pid[pid] for pid in worker_ids if pid in by_pid],
            "processes": records,
            "observed_descendant_cpu_seconds": (sum(r["kernel_cpu_seconds"] + r["user_cpu_seconds"]
                for r in records) if not self.error else None)}


def _design_identity(design: Any) -> str | None:
    if not isinstance(design, dict):
        return None
    physical = {key: value for key, value in design.items() if key not in {"note", "summary"}}
    return _canonical_hash(physical)


def _checkpoint_matches_candidate(record: dict[str, Any], candidate_identity: str | None,
                                  source_sha256: str) -> bool:
    """Accept a first-pass checkpoint only for the same physical design and source."""
    identities = (record.get("candidate_identity_sha256"),
                  record.get("training_identity_sha256"),
                  record.get("audit_identity_sha256"))
    return (candidate_identity is not None
            and all(identity == candidate_identity for identity in identities)
            and record.get("candidate_training_audit_identity_matches") is True
            and record.get("training_raw_pass") is True
            and record.get("audit_raw_pass") is True
            and record.get("source_sha256") == source_sha256)


def _install_cooperative_signal_handlers(cancel_event: threading.Event) -> dict[int, Any]:
    """Convert interrupt/termination signals into cooperative cancellation."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def request_cancel(_signum, _frame):
        cancel_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_cancel)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def run_one(entry: dict[str, Any], *, mode: str, workers: int, output: Path,
            budget_seconds: float | None, manifest: dict[str, Any], bundle_root: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    os.environ.update(THREAD_ENVIRONMENT)
    if str(bundle_root) not in sys.path:
        sys.path.insert(0, str(bundle_root))
    request_json = json.loads(json.dumps(entry["request"]))
    request_json["cpu_workers"] = workers
    request_json["structure_search_seed"] = entry["seed"]
    _mode_for_request(request_json, mode)
    request_hash = _canonical_hash(request_json)
    paired_request = {key: value for key, value in request_json.items() if key != "ar_structure_mode"}
    paired_request_hash = _canonical_hash(paired_request)
    frozen_budget = float(entry["wall_seconds"])
    if not math.isfinite(frozen_budget) or frozen_budget <= 0:
        raise ValueError("Frozen wall budget must be finite and positive")
    if budget_seconds is not None and (not math.isfinite(budget_seconds)
            or budget_seconds <= 0 or budget_seconds > frozen_budget):
        raise ValueError("--budget-seconds may only reduce the finite frozen wall budget")
    wall_budget = float(budget_seconds) if budget_seconds is not None else frozen_budget
    smoke_override = budget_seconds is not None and wall_budget < frozen_budget

    from coat_sim.core.film_auto_synthesis import synthesize_film_design
    from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload, film_synthesis_result_to_payload
    request_payload = {"schema_version": REQUEST_CONTRACT_VERSION, "kind": "coat_sim.film_synthesis_request",
        "request_id": entry["request_id"], "request": request_json, "physical_structures": []}
    request_id, request, structures = film_synthesis_request_from_payload(request_payload)
    if request_id != entry["request_id"] or structures:
        raise ValueError("Frozen cloud request decoder changed request identity or contains dynamic structures")

    # Source verification happens outside the algorithm wall clock. Recheck after
    # execution for the final success predicate; checkpoints reuse this identity.
    source_at_start = _source_fingerprint(bundle_root, manifest)
    cancel_event = threading.Event()
    monitor_stop = threading.Event()
    progress_lock = threading.RLock()
    started = time.monotonic()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    progress_state: dict[str, Any] = {"stage": "starting", "cycle": 0, "evaluations": 0,
        "layer_count": 0, "last_progress_monotonic": started}
    checkpoint_records: list[dict[str, Any]] = []
    cpu_observer = ProcessCPUObserver()

    def append_event(kind, data):
        event = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "kind": kind, **_jsonable(data)}
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()

    def progress(item):
        body = _jsonable(item)
        with progress_lock:
            progress_state.update(body)
            progress_state["last_progress_monotonic"] = time.monotonic()
            append_event("algorithm_progress", body)

    def heartbeat_loop():
        while not monitor_stop.wait(1.0):
            cpu_observer.poll()
            with progress_lock:
                age = max(0.0, time.monotonic() - progress_state["last_progress_monotonic"])
                payload = {key: value for key, value in progress_state.items()
                           if key != "last_progress_monotonic"}
                payload.update({"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "last_progress_age_seconds": age, "stalled": age >= 30.0,
                    "unresponsive": age >= 120.0,
                    "diagnostic_only": True, "worker_pid": os.getpid()})
                _atomic_json(output / "heartbeat.json", payload)
                append_event("heartbeat", payload)

    def checkpoint(design, training, validation, audit):
        elapsed = time.monotonic() - started
        train_check = _raw_bounds(request_json, training, scope="training")
        audit_check = _raw_bounds(request_json, validation, scope="audit")
        candidate_checkpoint_identity = _design_identity(_jsonable(design))
        training_checkpoint_identity = _design_identity(training.get("design")) \
            if isinstance(training, dict) else None
        audit_checkpoint_identity = _design_identity(validation.get("design")) \
            if isinstance(validation, dict) else None
        checkpoint_identity_matches = (candidate_checkpoint_identity is not None
            and candidate_checkpoint_identity == training_checkpoint_identity
            and candidate_checkpoint_identity == audit_checkpoint_identity)
        evidence = {"elapsed_seconds": elapsed,
            "candidate_identity_sha256": candidate_checkpoint_identity,
            "training_identity_sha256": training_checkpoint_identity,
            "audit_identity_sha256": audit_checkpoint_identity,
            "candidate_training_audit_identity_matches": checkpoint_identity_matches,
            "training_raw_pass": train_check.get("passed") is True,
            "training_raw_available": train_check.get("available") is True,
            "audit_raw_pass": audit_check.get("passed") is True,
            "audit_raw_available": audit_check.get("available") is True,
            "source_sha256": source_at_start["bundle_source_sha256"]}
        checkpoint_records.append(evidence)
        record = {"schema_version": 1, "kind": "film_synthesis_cloud_audited_checkpoint",
            "checkpoint_state": "audited", "case": entry["case"], "seed": entry["seed"],
            "mode": mode, "workers": workers, "request_sha256": request_hash,
            "request": request_json, "design": design, "training_score": training,
            "validation_score": validation, "audit": audit, "checkpoint_evidence": evidence,
            "source_identity": source_at_start,
            "wall_budget_seconds": frozen_budget, "applied_wall_budget_seconds": wall_budget,
            "smoke_override": smoke_override, "worker": {"pid": os.getpid(), "platform": sys.platform}}
        _atomic_json(output / "best-audited-checkpoint.json", record)
        append_event("audited_checkpoint", evidence)

    monitor = threading.Thread(target=heartbeat_loop, name="cloud-synthesis-heartbeat", daemon=True)
    timer = threading.Timer(wall_budget, cancel_event.set)
    timer.daemon = True
    prior_handlers = {}
    result = None
    error = None
    monitor.start()
    timer.start()
    prior_handlers = _install_cooperative_signal_handlers(cancel_event)
    try:
        result = synthesize_film_design(None, request, cancel_event=cancel_event,
                                        progress_callback=progress, snapshot_callback=checkpoint)
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        timer.cancel()
        monitor_stop.set()
        monitor.join(timeout=3.0)
        _restore_signal_handlers(prior_handlers)

    elapsed = time.monotonic() - started
    source_after = None
    try:
        source_after = _source_fingerprint(bundle_root, manifest)
    except Exception as exc:
        error = error or {"type": type(exc).__name__, "message": str(exc)}
    source_unchanged = source_after is not None and source_after["bundle_source_sha256"] == source_at_start["bundle_source_sha256"]
    if result is None:
        worker_cpu = cpu_observer.finish()
        failed = {"schema_version": 1, "kind": "film_synthesis_cloud_run",
            "case": entry["case"], "seed": entry["seed"], "mode": mode,
            "state": "runner_failed", "terminal_error": error,
            "started_utc": started_utc, "elapsed_seconds": elapsed,
            "wall_budget_seconds": frozen_budget, "applied_wall_budget_seconds": wall_budget,
            "smoke_override": smoke_override, "request_sha256": request_hash,
            "source_identity": source_at_start, "source_unchanged": source_unchanged,
            "progress_at_return": {key: value for key, value in progress_state.items()
                                    if key != "last_progress_monotonic"},
            "worker_cpu_observation": worker_cpu, "success": False}
        append_event("terminal_error", failed)
        _atomic_json(output / "result.json", failed)
        return failed

    response = film_synthesis_result_to_payload(request_id, result)
    body = response["result"]
    training = body.get("training_score")
    audit_score = body.get("validation_score")
    training_acceptance = _raw_bounds(request_json, training, scope="training")
    audit_acceptance = _raw_bounds(request_json, audit_score, scope="audit")
    candidate_design = body.get("candidate_design")
    candidate_identity = _design_identity(candidate_design)
    train_identity = _design_identity(training.get("design")) if isinstance(training, dict) else None
    audit_identity = _design_identity(audit_score.get("design")) if isinstance(audit_score, dict) else None
    candidate_evidence_matches = (candidate_identity is not None and candidate_identity == train_identity
                                  and candidate_identity == audit_identity)
    performance = body.get("performance", {})
    worker_pids = performance.get("worker_pids", []) if isinstance(performance, dict) else []
    worker_cpu = cpu_observer.finish(worker_pids)
    work_fields = ("cycles", "evaluations", "validation_evaluations", "failed_evaluations",
                   "audit_physical_evaluations", "audit_wavelength_points")
    work_counters = {name: body.get(name) for name in work_fields}
    counters_complete = all(type(value) is int and value >= 0 for value in work_counters.values())
    actual_work_complete = performance.get("actual_work_complete") is True
    work_complete = counters_complete and actual_work_complete
    cleanup = {"execution_state": performance.get("execution_state"),
        "cleanup_complete": performance.get("cleanup_complete") is True,
        "active_worker_pids": performance.get("active_worker_pids", [])}
    cleanup_complete = (cleanup["execution_state"] == "closed"
        and cleanup["cleanup_complete"] and cleanup["active_worker_pids"] == [])
    first_matching = [record["elapsed_seconds"] for record in checkpoint_records
        if _checkpoint_matches_candidate(record, candidate_identity,
            source_at_start["bundle_source_sha256"])]
    first_matching_elapsed = min(first_matching) if first_matching else None
    full_raw_success = (body.get("state") == "completed" and candidate_evidence_matches
        and training_acceptance["available"] and training_acceptance["passed"]
        and audit_acceptance["available"] and audit_acceptance["passed"]
        and source_unchanged and work_complete and cleanup_complete)
    dependency_versions = {name: importlib.metadata.version(name) for name in manifest["dependencies"]}
    payload = {"schema_version": 1, "kind": "film_synthesis_cloud_run",
        "case": entry["case"], "category": entry.get("category"), "seed": entry["seed"],
        "mode": mode, "workers": workers, "started_utc": started_utc,
        "elapsed_seconds": elapsed, "wall_budget_seconds": frozen_budget,
        "applied_wall_budget_seconds": wall_budget, "smoke_override": smoke_override,
        "request_sha256": request_hash, "paired_request_sha256_excluding_mode": paired_request_hash,
        "source_identity": source_at_start, "source_unchanged": source_unchanged,
        "runtime": {"python": sys.version, "platform": sys.platform,
            "thread_environment": THREAD_ENVIRONMENT, "dependencies": manifest["dependencies"],
            "dependency_versions": dependency_versions, "worker_pid": os.getpid(),
            "workers_requested": workers, "worker_performance": performance,
            "worker_cpu_observation": worker_cpu, "cleanup": cleanup,
            "work_counters": work_counters, "work_counters_complete": counters_complete,
            "actual_work_complete": actual_work_complete},
        "progress_at_return": {key: value for key, value in progress_state.items()
                                if key != "last_progress_monotonic"},
        "acceptance": {"training_raw_bounds": training_acceptance,
            "dense_audit_raw_bounds": audit_acceptance,
            "candidate_training_audit_physical_identity_matches": candidate_evidence_matches,
            "first_matching_raw_pass_audit_elapsed_seconds": first_matching_elapsed,
            "overall_raw_success": full_raw_success,
            "confirmation_eligible": full_raw_success and not smoke_override,
            "success_rate_eligible": full_raw_success and not smoke_override,
            "known_limits": "finite sampled conditions only; no continuous certificate"},
        "descriptive_means": _mean_metrics(request_json, audit_score),
        "result_path": str(output / "result.json"),
        "result": body, "terminal_error": error}
    append_event("terminal", {"state": body.get("state"),
        "overall_raw_success": full_raw_success, "elapsed_seconds": elapsed})
    _atomic_json(output / "result.json", payload)
    return payload


def run_bundle(manifest_path: Path, *, case: str | None = None, seed: int | None = None,
               mode: str = "clustered", workers: int | None = None,
               output: Path, budget_seconds: float | None = None, preflight: bool = False,
               repo_root: Path | None = None, expected_commit: str | None = None):
    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    if manifest["kind"] == "film_synthesis_git_requests":
        if repo_root is None or expected_commit is None:
            raise ValueError("Git requests require --repo-root and --expect-commit")
        bundle_root = Path(repo_root).resolve()
        if not manifest_path.is_relative_to(bundle_root):
            raise ValueError("Git frozen request manifest must be inside --repo-root")
        manifest["_manifest_relative_path"] = manifest_path.relative_to(bundle_root).as_posix()
        manifest["_expected_commit"] = expected_commit
    else:
        if repo_root is not None or expected_commit is not None:
            raise ValueError("Git identity arguments require a Git-request manifest")
        bundle_root = manifest_path.parent
    source_identity = _source_fingerprint(bundle_root, manifest)
    entries = _choose(manifest, case)
    if seed is not None:
        entries = [dict(entry, seed=seed) for entry in entries]
    chosen_workers = workers if workers is not None else int(manifest["defaults"]["workers"])
    if chosen_workers < 1 or chosen_workers > 128:
        raise ValueError("workers must be between 1 and 128")
    if preflight:
        if str(bundle_root) not in sys.path:
            sys.path.insert(0, str(bundle_root))
        from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
        rows = []
        for item in entries:
            request = json.loads(json.dumps(item["request"]))
            request["structure_search_seed"] = item["seed"]
            request["cpu_workers"] = chosen_workers
            _mode_for_request(request, mode)
            decoded_id, _, structures = film_synthesis_request_from_payload({
                "schema_version": REQUEST_CONTRACT_VERSION, "kind": "coat_sim.film_synthesis_request",
                "request_id": item["request_id"], "request": request, "physical_structures": []})
            if decoded_id != item["request_id"] or structures:
                raise ValueError("Production request decoder changed identity or found dynamic structures")
            rows.append({"case": item["case"], "seed": item["seed"], "workers": chosen_workers,
                 "mode": mode, "request_sha256": _canonical_hash(request),
                 "paired_request_sha256_excluding_mode": _canonical_hash({
                     key: value for key, value in request.items() if key != "ar_structure_mode"}),
                 "wall_seconds": item["wall_seconds"]})
        if manifest["kind"] == "film_synthesis_git_requests":
            source_identity = _source_fingerprint(bundle_root, manifest)
        return {"preflight_only": True, "bundle_source_sha256": source_identity["bundle_source_sha256"],
                "runs": rows, "cloud_cpu_entrypoint": manifest["execution_contract"]["entrypoint"],
                "freeze_source": manifest["execution_contract"]["freeze_provenance"],
                "source_identity": source_identity}
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    completed = []
    for entry in entries:
        run_dir = output / f"{entry['case']}-seed{entry['seed']}-{mode}"
        try:
            completed.append(run_one(entry, mode=mode, workers=chosen_workers, output=run_dir,
                                     budget_seconds=budget_seconds, manifest=manifest,
                                     bundle_root=bundle_root))
        except Exception as exc:
            if not run_dir.exists():
                run_dir.mkdir(parents=True)
            failure = {"schema_version": 1, "kind": "film_synthesis_cloud_run",
                "case": entry["case"], "seed": entry["seed"], "mode": mode,
                "state": "runner_failed", "terminal_error": {
                    "type": type(exc).__name__, "message": str(exc)},
                "request_sha256": entry.get("request_sha256"), "success": False}
            with (run_dir / "progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "terminal_error", **failure},
                                        sort_keys=True, allow_nan=False) + "\n")
            _atomic_json(run_dir / "result.json", failure)
            completed.append(failure)
    return {"runs": completed, "bundle_source_sha256": source_identity["bundle_source_sha256"],
            "source_identity": source_identity}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Extracted bundle directory's manifest.json")
    parser.add_argument("--repo-root", type=Path, help="Exact Git checkout root for Git-request mode")
    parser.add_argument("--expect-commit", help="Exact 40-character commit SHA required in Git mode")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--mode", choices=("clustered", "v23_matching"), default="clustered")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float,
                        help="Optional smoke override that can only shorten the frozen wall budget")
    parser.add_argument("--preflight", action="store_true", help="Validate frozen request and print hashes without executing")
    args = parser.parse_args(argv)
    result = run_bundle(args.manifest, case=args.case, seed=args.seed, mode=args.mode,
                        workers=args.workers, output=args.output,
                        budget_seconds=args.budget_seconds, preflight=args.preflight,
                        repo_root=args.repo_root, expected_commit=args.expect_commit)
    if result.get("preflight_only"):
        terminal = {"state": "preflight_only", "source_sha256": result.get("bundle_source_sha256"),
            "case_count": len(result.get("runs", [])), "cases": result.get("runs", [])}
    else:
        terminal = {"state": "finished", "output": str(args.output.resolve()),
            "source_sha256": result.get("bundle_source_sha256"),
            "runs": [{"case": run.get("case"), "seed": run.get("seed"),
                "mode": run.get("mode"), "state": (run.get("result") or {}).get("state", run.get("state")),
                "overall_raw_success": (run.get("acceptance") or {}).get("overall_raw_success"),
                "smoke_override": run.get("smoke_override"),
                "request_sha256": run.get("request_sha256"),
                "result_path": run.get("result_path", str(args.output.resolve() /
                    f"{run.get('case')}-seed{run.get('seed')}-{run.get('mode')}/result.json"))}
                for run in result.get("runs", [])]}
    terminal["git_commit"] = result.get("source_identity", {}).get("git_commit")
    print(json.dumps(_jsonable(terminal), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
