"""Run frozen representative suites through the GUI request/durable job chain.

Example (the output directory must not exist and must be below system Temp):
  .venv/Scripts/python.exe tools/run_film_synthesis_benchmarks.py --suite benchmarks/film_synthesis/20261024/representative-suite-current-pointwise.json --case ar_v_visible --seed 0 --workers 2 --output TEMP/new-benchmark

No application service, user project, scheduler store or test fixture is opened.
Wall budgets come exclusively from the frozen suite. Cancellation is cooperative.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Direct script execution needs the current repository import path above.
from tools.run_film_synthesis_acceptance import AcceptanceService, atomic_json, jsonable, sha256_json, utc_now  # noqa: E402

class AuditedCheckpoint(TypedDict):
    elapsed_seconds: float
    design_sha256: str


SCHEMA_VERSION = 1
THREAD_ENVIRONMENT = {name: "1" for name in (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")}


def configure_process() -> dict[str, Any]:
    """Only this process and its future children inherit these settings."""
    os.environ.update(THREAD_ENVIRONMENT)
    priority = "unavailable_non_windows"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        windows_library = getattr(ctypes, "WinDLL", None)
        if windows_library is None:
            raise OSError("Windows DLL loader unavailable")
        kernel = windows_library("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.SetPriorityClass.argtypes = [w.HANDLE, w.DWORD]
        kernel.GetPriorityClass.argtypes = [w.HANDLE]
        kernel.GetPriorityClass.restype = w.DWORD
        handle = kernel.GetCurrentProcess()
        if not kernel.SetPriorityClass(handle, 0x4000):
            last_error = getattr(ctypes, "get_last_error", None)
            if last_error is None:
                raise OSError("Cannot set benchmark process BelowNormal")
            raise OSError(last_error(), "Cannot set benchmark process BelowNormal")
        priority = "below_normal" if kernel.GetPriorityClass(handle) == 0x4000 else "verification_failed"
        if priority != "below_normal":
            raise RuntimeError("Benchmark process priority verification failed")
    return {"pid": os.getpid(), "priority": priority,
            "thread_environment": {name: os.environ[name] for name in THREAD_ENVIRONMENT}}


def source_identity(repo_root: Path = ROOT) -> dict[str, Any]:
    paths = sorted((repo_root / "coat_sim").rglob("*.py"))
    paths += [repo_root / "tools" / name for name in (
        "run_film_synthesis_benchmarks.py", "run_film_synthesis_acceptance.py",
        "run_film_synthesis_cloud.py")]
    hashes = {path.relative_to(repo_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths}
    return {"source_sha256": sha256_json(hashes), "source_hashes": hashes}


def _inside_repo(repo_root: Path, relative: str) -> Path:
    path = (repo_root / relative).resolve()
    if not path.is_relative_to(repo_root.resolve()):
        raise ValueError("Suite catalog must be inside the current repository")
    return path


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def load_suite(path: Path, repo_root: Path = ROOT):
    content = path.read_bytes()
    suite = json.loads(content)
    if type(suite.get("schema_version")) is not int or suite["schema_version"] != 2:
        raise ValueError("Expected representative suite schema_version 2")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Suite needs nonempty cases")
    identifiers = [case.get("id") for case in cases]
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in identifiers):
        raise ValueError("Case IDs must be safe path components")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate case ID")
    from tools.run_film_synthesis_cloud import _assert_current_request_shape
    for case in cases:
        _assert_current_request_shape(case)
    catalog_path = _inside_repo(repo_root, suite["material_catalog"])
    catalog_bytes = catalog_path.read_bytes()
    catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
    if suite.get("catalog_sha256") not in (None, catalog_hash):
        raise ValueError("Frozen suite catalog_sha256 differs from current catalog")
    catalog = json.loads(catalog_bytes)
    rows = [dict(id=row["builtin_material_id"], name=row["material_id"], design_ready=True,
                 optical_sets=deepcopy(row["optical_constants"])) for row in catalog["materials"]]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Catalog contains duplicate builtin material IDs")
    return suite, rows, {"suite_sha256": hashlib.sha256(content).hexdigest(), "catalog_sha256": catalog_hash}


def build_case_request(case, defaults, rows, seed, workers):
    """Adapt manifest fields to actual GUI contracts, then freeze real optics."""
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_design import new_design
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_objective_payload import objective_from_payload
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.ui.film_synthesis_quick_adapter import build_synthesis_request
    if type(seed) is not int or seed < 0:
        raise ValueError("Seed must be a nonnegative integer")
    if type(workers) is not int or workers < 1:
        raise ValueError("Benchmark workers must be a positive integer")
    if type(case.get("assume_lossless")) is not bool:
        raise ValueError("Case must explicitly declare assume_lossless")
    environment = new_design(name=case["id"], design_id="benchmark-" + case["id"])
    environment["substrate"].update(case["substrate"])
    environment["substrate"]["mode"] = "semi_infinite"
    from tools.run_film_synthesis_cloud import _assert_current_request_shape
    _assert_current_request_shape(case)
    objectives = tuple(objective_from_payload(item) for item in case["objectives"])
    base = FilmOptimizationRequest(environment, 0, (), objectives,
        acceptable_normalized_error=defaults.get("acceptable_normalized_error", 0),
        validation_samples_multiplier=defaults.get("validation_samples_multiplier", 5))
    references = tuple(FilmNeedleMaterialReference(**item) for item in case["materials"])
    options = {"enable_thickness_reduction": case["enable_thickness_reduction"],
        "objective_progress_policy": case["objective_progress_policy"],
        "max_layers": case.get("max_layers"), "max_cycles": case.get("max_cycles"),
        "max_evaluations": case.get("max_evaluations"),
        "minimum_nonzero_thickness_nm": case.get("min_thickness_nm"),
        "max_layer_thickness_nm": case.get("max_thickness_nm"),
        "max_total_thickness_nm": case.get("max_total_thickness_nm")}
    request = build_synthesis_request(base, references, options, use_current_seed=False,
        assume_unknown_k_zero=case["assume_lossless"], cpu_workers=workers, forward_backend="cpu")
    request = replace(request, structure_search_seed=seed)
    service = AcceptanceService(rows)
    return service, freeze_film_synthesis_request(service, request)


def compile_preflight(suite_path, case_id=None, seed=None, workers=None, repo_root=None):
    """Read-only production request/spec preflight; never run the optimizer.

    case_id and seed accept a scalar or a list. Omitted values select the suite.
    Returned frozen requests and JobSpecs are JSON objects ready for inspection.
    """
    from coat_sim.scheduler.jobs import JobSpec
    from coat_sim.ui.film_synthesis_quick_adapter import synthesis_spec
    repo_root = Path(repo_root or ROOT).resolve()
    suite_path = Path(suite_path).resolve()
    suite, rows, hashes = load_suite(suite_path, repo_root)
    defaults = suite.get("defaults", {})
    if defaults.get("math_threads", 1) != 1 or defaults.get("process_priority", "below_normal") != "below_normal":
        raise ValueError("Runner requires frozen math_threads=1 and process_priority=below_normal")
    wanted = None if case_id is None else set(case_id if isinstance(case_id, (tuple, list)) else [case_id])
    if wanted is not None and wanted - {case["id"] for case in suite["cases"]}:
        raise ValueError(f"Unknown case IDs: {sorted(wanted - {case['id'] for case in suite['cases']})}")
    seeds = defaults.get("seeds", [0, 1, 2]) if seed is None else (seed if isinstance(seed, (tuple, list)) else [seed])
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("Seeds must be nonempty and unique")
    workers = defaults.get("workers", 2) if workers is None else workers
    runs = []
    for case in suite["cases"]:
        if wanted is not None and case["id"] not in wanted:
            continue
        wall_seconds = _positive_number(case.get("wall_seconds", defaults.get("wall_seconds", 120)), "wall_seconds")
        for case_seed in seeds:
            service, frozen = build_case_request(case, defaults, rows, case_seed, workers)
            parsed = JobSpec.parse_frozen(synthesis_spec(service, frozen))
            # Force the exact durable worker decoder before any benchmark starts.
            from coat_sim.core.film_synthesis_jobs import deserialize_job_payload
            _, decoded, structures = deserialize_job_payload(parsed.parameters["payload"])
            if structures or jsonable(decoded) != jsonable(frozen):
                raise ValueError("Durable job roundtrip changed the frozen request")
            runs.append({"case": case["id"], "category": case.get("category"), "seed": case_seed,
                "workers": workers, "wall_seconds": wall_seconds,
                "qualification": deepcopy(case.get("qualification")),
                "request_sha256": sha256_json(frozen), "frozen_request": jsonable(frozen),
                "job_spec": parsed.to_spec().to_dict(), "job_fingerprint": parsed.fingerprint})
    return {"schema_version": SCHEMA_VERSION, "kind": "film_synthesis_benchmark_preflight",
        "suite_path": str(suite_path), "suite_id": suite.get("suite_id"),
        "compiled_utc": utc_now(), "workers": workers, "suite": suite,
        **hashes, **source_identity(repo_root), "runs": runs}


def reserve_output(path: Path) -> Path:
    path = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if path == temp_root or not path.is_relative_to(temp_root):
        raise ValueError("--output must be a NEW directory below system Temp")
    # mkdir without exist_ok makes concurrent or terminal result reuse impossible.
    path.mkdir(parents=True, exist_ok=False)
    return path


def _physical_design(design):
    """Use the core validator; only top-level presentation annotations may differ."""
    from coat_sim.core.film_design import validate_design
    normalized = validate_design(jsonable(design))
    return {key: value for key, value in normalized.items() if key not in {"note", "summary"}}


def candidate_evidence_matches(design, training, validation):
    """Bind both score records to the returned physical design, including layer IDs."""
    try:
        expected = _physical_design(design)
        return (isinstance(training, dict) and isinstance(validation, dict)
            and expected == _physical_design(training.get("design"))
            and expected == _physical_design(validation.get("design")))
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False


def raw_acceptance(request, score, *, scope="training"):
    """Use the current production criteria verifier for both benchmark routes."""
    from tools.run_film_synthesis_cloud import _raw_bounds
    verdict = _raw_bounds(request, score, scope=scope)
    return {**verdict,
        "missing_condition_count": verdict.get("missing_required_conditions", 0),
        "violating_condition_count": verdict.get("violating_conditions", 0)}


def verify_cleanup(performance):
    from coat_sim.core.process_identity import _process_identity
    records = []
    for worker in performance.get("workers", []):
        for key, stamp_key in (("actual_worker_pid", "creation_time"), ("wrapper_pid", "wrapper_creation_time")):
            pid = worker.get(key)
            if not isinstance(pid, int):
                continue
            status, identity = _process_identity(pid)
            expected = worker.get(stamp_key)
            expected = f"windows:{expected}" if expected is not None and os.name == "nt" else expected
            # A different creation identity proves this PID has been reused.
            gone = status == "dead" or (status == "alive" and expected is not None and identity != expected)
            records.append({"pid": pid, "status": status, "identity": identity,
                            "expected_identity": expected, "owned_process_gone": gone})
    declared = (performance.get("execution_state") == "closed"
        and performance.get("cleanup_complete") is True and performance.get("active_worker_pids") == [])
    return {"verified": declared and all(row["owned_process_gone"] for row in records),
            "executor_declared_closed": declared, "process_checks": records}


class WindowsCPUObserver:
    """Retain query handles for this runner's observed descendants, never others.

    Polling may miss short-lived children. CPU totals explicitly remain observed
    lower bounds, distinct from worker wall compute time in executor diagnostics.
    """
    def __init__(self):
        self.handles = {}
        self.priorities = {}
        self.error = None
        if os.name != "nt":
            self.error = "Windows process CPU observer unavailable on this platform"
            return
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        windows_library = getattr(c, "WinDLL", None)
        if windows_library is None:
            raise OSError("Windows DLL loader unavailable")
        self.kernel = windows_library("kernel32", use_last_error=True)
        class Entry(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                ("szExeFile", w.WCHAR * 260)]
        self.Entry = Entry
        for name, args, result in (
            ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
            ("Process32FirstW", [w.HANDLE, c.POINTER(Entry)], w.BOOL),
            ("Process32NextW", [w.HANDLE, c.POINTER(Entry)], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("CloseHandle", [w.HANDLE], w.BOOL),
            ("GetPriorityClass", [w.HANDLE], w.DWORD),
            ("GetProcessTimes", [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL)):
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = args, result

    def poll(self):
        if self.error:
            return
        c = self.c
        snap = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snap == c.c_void_p(-1).value:
            return
        parents = {}
        entry = self.Entry()
        entry.dwSize = c.sizeof(entry)
        try:
            more = self.kernel.Process32FirstW(snap, c.byref(entry))
            while more:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                more = self.kernel.Process32NextW(snap, c.byref(entry))
        finally:
            self.kernel.CloseHandle(snap)
        owned = {os.getpid()}
        while True:
            expanded = owned | {pid for pid, parent in parents.items() if parent in owned}
            if owned == expanded:
                break
            owned = expanded
        for pid in owned - {os.getpid()} - self.handles.keys():
            handle = self.kernel.OpenProcess(0x1000, False, pid)
            if handle:
                self.handles[pid] = handle
                self.priorities[pid] = self.kernel.GetPriorityClass(handle)

    def finish(self):
        rows = []
        if self.handles:
            c, w = self.c, self.w
            for pid, handle in self.handles.items():
                creation, exit_time, kernel_time, user_time = (w.FILETIME() for _ in range(4))
                try:
                    if self.kernel.GetProcessTimes(handle, c.byref(creation), c.byref(exit_time),
                        c.byref(kernel_time), c.byref(user_time)):
                        def value(stamp):
                            return (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime
                        rows.append({"pid": pid, "creation_time": value(creation),
                            "kernel_seconds": value(kernel_time) / 1e7, "user_seconds": value(user_time) / 1e7,
                            "priority_class_when_observed": self.priorities.get(pid)})
                finally:
                    self.kernel.CloseHandle(handle)
            self.handles.clear()
        return {"available": not bool(self.error), "reason": self.error,
            "scope": "observed_descendant_lifetime_CPU_lower_bound_polling_may_miss_short_children",
            "processes": rows, "observed_descendant_cpu_seconds":
                sum(row["kernel_seconds"] + row["user_seconds"] for row in rows) if not self.error else None}


def execute_run(run, directory, provenance, cancel, stop_all):
    from coat_sim.core.film_synthesis_jobs import execute_job, is_synthesis_result
    from coat_sim.scheduler.jobs import JobSpec
    directory.mkdir()
    parsed = JobSpec.parse_frozen(run["job_spec"])
    atomic_json(directory / "request.json", {"request_sha256": run["request_sha256"], "request": run["frozen_request"]})
    atomic_json(directory / "job-spec.json", run["job_spec"])
    started = time.monotonic()
    cpu_started = time.process_time()
    lock = threading.Lock()
    sequence = 0
    deadline_hit = threading.Event()
    audited_success_checkpoints: list[AuditedCheckpoint] = []
    observer = WindowsCPUObserver()
    stop_monitor = threading.Event()
    def event(kind, payload):
        nonlocal sequence
        with lock:
            sequence += 1
            record = {**jsonable(payload), "sequence": sequence, "utc": utc_now(),
                "elapsed_seconds": time.monotonic() - started, "kind": kind}
            with (directory / "progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
    def save_checkpoint(path, value):
        atomic_json(Path(path), value)
        training_pass = raw_acceptance(run["frozen_request"], value.get("training_score"))["passed"]
        audit_pass = raw_acceptance(run["frozen_request"], value.get("validation_score"), scope="audit")["passed"]
        evidence_matches = candidate_evidence_matches(value.get("design"), value.get("training_score"), value.get("validation_score"))
        if (value.get("checkpoint_state") == "audited" and training_pass is True
                and audit_pass is True and evidence_matches):
            audited_success_checkpoints.append({"elapsed_seconds": time.monotonic() - started,
                "design_sha256": sha256_json(_physical_design(value["design"]))})
        event("checkpoint", {"checkpoint": Path(path).name, "checkpoint_state": value.get("checkpoint_state"),
            "training_raw_pass": training_pass, "audit_raw_pass": audit_pass, "candidate_audit_matches": evidence_matches})
    startup = {"schema_version": SCHEMA_VERSION, "kind": "film_synthesis_benchmark_startup",
        "started_utc": utc_now(), "case": run["case"], "seed": run["seed"],
        "wall_seconds": run["wall_seconds"], "request_sha256": run["request_sha256"],
        "wall_budget_scope": "starts_after_compile_import_and_request_spec_writes_before_execute_job; includes_startup_and_cleanup; cooperative_cancel",
        "cross_tool_wall_times_comparable": False,
        "job_fingerprint": parsed.fingerprint, "protocol_version": parsed.protocol_version, **provenance}
    atomic_json(directory / "startup.json", startup)
    event("startup", startup)
    def monitor():
        while not stop_monitor.is_set():
            try:
                observer.poll()
            except Exception as exc:
                observer.error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() - started >= run["wall_seconds"] and not deadline_hit.is_set():
                deadline_hit.set()
                cancel.set()
                event("cancellation_requested", {"reason": "frozen_suite_wall_budget"})
            if stop_all.is_set():
                cancel.set()
            stop_monitor.wait(.25)
    thread = threading.Thread(target=monitor, name="benchmark-budget-and-cpu", daemon=True)
    thread.start()
    response = None
    error = None
    try:
        response = execute_job(parsed.parameters, cancel,
            lambda payload, **kwargs: event("heartbeat" if payload.get("heartbeat") else "progress", payload),
            directory / "best-candidate.json", save_checkpoint, str(directory))
        if not is_synthesis_result(response):
            raise ValueError("Production execute_job returned an invalid synthesis result")
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        event("error", error)
    finally:
        stop_monitor.set()
        thread.join(timeout=3)
        cpu_observation = observer.finish()
    elapsed = time.monotonic() - started
    # A rejected response is diagnostic data only. In particular, its claimed
    # terminal state, passing scores and cleanup must not certify this run.
    accepted_response = response if error is None else {}
    body = (accepted_response or {}).get("result", {})
    performance = body.get("performance", {})
    cleanup = verify_cleanup(performance)
    training = raw_acceptance(run["frozen_request"], body.get("training_score"))
    audit = raw_acceptance(run["frozen_request"], body.get("validation_score"), scope="audit")
    evidence_matches = candidate_evidence_matches(body.get("candidate_design"), body.get("training_score"), body.get("validation_score"))
    raw_success = (evidence_matches and training["passed"] is True and audit["passed"] is True
        and (accepted_response or {}).get("best_candidate_audit", {}).get("available") is True
        and (accepted_response or {}).get("best_candidate_audit", {}).get("targets_met") is True)
    final_design_hash = sha256_json(_physical_design(body["candidate_design"])) if evidence_matches else None
    matching_success = [row["elapsed_seconds"] for row in audited_success_checkpoints
        if row["design_sha256"] == final_design_hash]
    first_success_seconds = (min(matching_success) if matching_success else elapsed) if raw_success else None
    source_after = source_identity()
    sources_unchanged = source_after["source_sha256"] == provenance["source_sha256"]
    summary = {"state": body.get("state", "runner_failed"), "termination_reason": body.get("termination_reason"),
        "elapsed_seconds": elapsed, "wall_budget_seconds": run["wall_seconds"],
        "wall_budget_reached": deadline_hit.is_set(), "cooperative_cancel_requested": cancel.is_set(),
        "cooperative_cancel_overrun_seconds": max(0., elapsed - run["wall_seconds"]),
        "targets_met": body.get("targets_met", False), "training_raw_bounds": training, "audit_raw_bounds": audit,
        "independent_audit_available": (accepted_response or {}).get("best_candidate_audit", {}).get("available", False),
        "strict_raw_targets_met": raw_success, "candidate_audit_matches": evidence_matches,
        "first_audited_success_seconds": first_success_seconds,
        "first_audited_success_scope": "first_raw_passing_checkpoint_matching_terminal_design_or_terminal_elapsed",
        "layers": len((body.get("candidate_design") or {}).get("front_layers", [])),
        "work_counters": {name: body.get(name) for name in ("cycles", "evaluations", "validation_evaluations",
            "failed_evaluations", "audit_physical_evaluations", "audit_wavelength_points")},
        "actual_training_physics": {name: performance.get(name) for name in (
            "training_physical_evaluations", "training_wavelength_points", "training_evaluator_invocations",
            "worker_training_physical_evaluations", "worker_training_wavelength_points", "unknown_tasks")},
        "rank_trace": [{key: row.get(key) for key in ("stage", "cycle", "evaluations", "candidate_rank")}
            for row in body.get("trace", []) if row.get("candidate_rank")],
        "parent_cpu_seconds": time.process_time() - cpu_started,
        "descendant_cpu_observation": cpu_observation,
        "cleanup": cleanup, "sources_unchanged": sources_unchanged}
    result = {"schema_version": SCHEMA_VERSION, "kind": "film_synthesis_benchmark_result",
        "case": run["case"], "category": run.get("category"), "seed": run["seed"],
        "request_sha256": run["request_sha256"], "job_fingerprint": parsed.fingerprint,
        "finished_utc": utc_now(), "summary": summary, "response": response, "error": error,
        "source_sha256_after": source_after["source_sha256"]}
    atomic_json(directory / "result.json", result)
    event("terminal", {"summary": summary, "error": error})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--workers", type=int, default=None, help="Default: frozen suite workers (2)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true", help="Compile production requests/specs only")
    args = parser.parse_args(argv)
    process = configure_process()
    preflight = compile_preflight(args.suite, args.cases, args.seeds, args.workers)
    output = reserve_output(args.output)
    atomic_json(output / "preflight.json", preflight)
    provenance = {key: preflight[key] for key in ("suite_id", "suite_sha256", "catalog_sha256", "source_sha256", "source_hashes")}
    provenance.update(process=process, python=sys.version, executable=sys.executable,
        workers_override=args.workers, worker_policy="CPU; one math thread per process; process BelowNormal")
    atomic_json(output / "startup.json", {"started_utc": utc_now(), **provenance})
    print(json.dumps({"event": "preflight_complete", "runs": len(preflight["runs"]), "output": str(output)}, ensure_ascii=False), flush=True)
    if args.preflight:
        atomic_json(output / "result.json", {"schema_version": SCHEMA_VERSION, "kind": "film_synthesis_benchmark_summary",
            "state": "preflight_completed", "run_count": len(preflight["runs"]), **provenance})
        return 0
    stop_all = threading.Event()
    active: list[threading.Event | None] = [None]
    def on_signal(signum, frame):
        stop_all.set()
        if active[0] is not None:
            active[0].set()
    old_handlers = {number: signal.signal(number, on_signal) for number in (signal.SIGINT, signal.SIGTERM)}
    results = []
    try:
        for run in preflight["runs"]:
            if stop_all.is_set():
                break
            active[0] = threading.Event()
            result = execute_run(run, output / f"{run['case']}-seed-{run['seed']}", provenance, active[0], stop_all)
            results.append({"case": run["case"], "seed": run["seed"], "path": f"{run['case']}-seed-{run['seed']}/result.json",
                "summary": result["summary"], "error": result["error"]})
            print(json.dumps({"event": "run_complete", "case": run["case"], "seed": run["seed"],
                "state": result["summary"]["state"], "strict_raw_targets_met": result["summary"]["strict_raw_targets_met"]}), flush=True)
            atomic_json(output / "summary.partial.json", {"runs": results})
            if result["error"] or not result["summary"]["cleanup"]["verified"] or not result["summary"]["sources_unchanged"]:
                stop_all.set()
    finally:
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
    complete = len(results) == len(preflight["runs"])
    valid = complete and all(not row["error"] and row["summary"]["cleanup"]["verified"]
        and row["summary"]["sources_unchanged"] for row in results)
    atomic_json(output / "result.json", {"schema_version": SCHEMA_VERSION, "kind": "film_synthesis_benchmark_summary",
        "finished_utc": utc_now(), "state": "completed" if valid else "partial_or_invalid",
        "complete": complete, "valid_execution": valid, "planned_runs": len(preflight["runs"]),
        "strict_raw_success_count": sum(row["summary"]["strict_raw_targets_met"] for row in results),
        "runs": results, **provenance})
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
