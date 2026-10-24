"""Run one frozen multispectral synthesis case through the production job path.

Research runner only: these three objectives do not qualify the four-window
master acceptance protocol. Output is reserved as a new directory under system
Temp. This script uses the frozen local protocol and current solver source; it
never injects an answer design.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_RELATIVE = Path("benchmarks/film_synthesis/20261022/multispectral-v1/protocol.json")
BASELINE_REQUEST_RELATIVE = Path("benchmarks/film_synthesis/20261021/hr-minimax-tio2-franta-v1/request.json")


def _configure_process_before_solver_imports() -> dict[str, Any]:
    """Set BLAS thread caps and process priority before loading solver modules."""
    # Importing this utility is safe here: it only imports standard-library
    # modules at module scope. configure_process applies the shared environment
    # and below-normal priority policy used by the existing benchmark runners.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.run_film_synthesis_benchmarks import configure_process
    return configure_process()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_hashes() -> dict[str, str]:
    paths = sorted((ROOT / "coat_sim").rglob("*.py"))
    paths += [ROOT / "tools" / "run_film_synthesis_benchmarks.py",
              ROOT / "tools" / "run_film_synthesis_acceptance.py",
              Path(__file__).resolve()]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def _head_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                   text=True, stderr=subprocess.STDOUT).strip()


def _load_inputs(protocol_path: Path, case_id: str, seed: int):
    protocol_bytes = protocol_path.read_bytes()
    protocol = json.loads(protocol_bytes)
    cases = protocol.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Protocol has no cases list")
    matches = [case for case in cases if case.get("id") == case_id]
    if len(matches) != 1:
        raise ValueError(f"Unknown or duplicate case ID: {case_id}")
    defaults = protocol["defaults"]
    if seed not in defaults.get("seeds", []):
        raise ValueError(f"Seed {seed} is not frozen in this protocol")
    base_relative = Path(protocol.get("base_frozen_request", BASELINE_REQUEST_RELATIVE.as_posix()))
    if base_relative.as_posix() != BASELINE_REQUEST_RELATIVE.as_posix():
        raise ValueError("Protocol must use the frozen public base request path")
    base_path = (ROOT / base_relative).resolve()
    if not base_path.is_relative_to(ROOT.resolve()):
        raise ValueError("Base frozen request must remain inside the repository")
    base_document = json.loads(base_path.read_text(encoding="utf-8"))
    expected = protocol["base_request_canonical_sha256"]
    actual = _canonical_sha256(base_document)
    if actual != expected:
        raise ValueError(f"Base frozen request canonical SHA-256 mismatch: {actual}")
    snapshot = base_document["request"]["frozen_material_snapshot"]
    rows = deepcopy(snapshot["materials"])
    case = deepcopy(matches[0])
    case["materials"] = deepcopy(protocol["candidate_materials"])
    case["substrate"] = deepcopy(protocol["substrate"])
    case["assume_lossless"] = bool(defaults["assume_unknown_k_zero"])
    return protocol, protocol_bytes, defaults, case, rows, actual


def _reserve_output(path: Path) -> Path:
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    if resolved == temp_root or not resolved.is_relative_to(temp_root):
        raise ValueError("--output must be a new child directory under system Temp")
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def _append_json_line(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()


def _run(args: argparse.Namespace) -> int:
    process_info = _configure_process_before_solver_imports()
    from tools.run_film_synthesis_acceptance import atomic_json, jsonable
    from tools.run_film_synthesis_benchmarks import build_case_request
    from coat_sim.core import film_synthesis_audit, film_synthesis_hr_minimax
    from coat_sim.core.film_synthesis_jobs import (
        build_job_payload, canonical_parameters, deserialize_job_payload,
        execute_job, is_synthesis_result,
    )
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_nominal_acceptance import nominal_targets_pass

    protocol_path = (ROOT / args.protocol).resolve()
    if not protocol_path.is_relative_to(ROOT.resolve()):
        raise ValueError("--protocol must point inside the repository")
    _, protocol_bytes, defaults, case, rows, base_sha = _load_inputs(
        protocol_path, args.case, args.seed)
    output = _reserve_output(Path(args.output))

    initial_head = _head_commit()
    initial_sources = _source_hashes()
    start = time.perf_counter()
    cpu_start = time.process_time()
    cancel = threading.Event()
    stop_monitor = threading.Event()
    last_progress = start
    last_log = start
    latest: dict[str, Any] = {}
    progress_count = 0
    milestones: list[dict[str, Any]] = []
    watchdog_grace = float(defaults["watchdog_grace_seconds"])
    parent_budget = float(case["max_seconds"])
    timings = {"audit_seconds": 0.0, "audit_calls": 0,
               "hr_minimax_seconds": 0.0, "general_minimax_seconds": 0.0}
    first_audit_pass_seconds = None
    progress_path = output / "progress.jsonl"

    def log(kind: str, **fields: Any) -> None:
        nonlocal last_log
        record = {"kind": kind, "case": args.case, "seed": args.seed,
                  "variant": "production", "elapsed_seconds": round(time.perf_counter() - start, 3),
                  **fields}
        _append_json_line(progress_path, record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        last_log = time.perf_counter()

    def wrap_timing(module: Any, name: str, key: str) -> None:
        original: Callable[..., Any] = getattr(module, name)

        def timed(*call_args: Any, **call_kwargs: Any) -> Any:
            began = time.perf_counter()
            try:
                return original(*call_args, **call_kwargs)
            finally:
                timings[key] += time.perf_counter() - began
                if key == "audit_seconds":
                    timings["audit_calls"] += 1

        setattr(module, name, timed)

    # Preserve the existing measurable audit and minimax timing boundaries.
    wrap_timing(film_synthesis_audit, "audit_frozen_synthesis_candidate", "audit_seconds")
    if hasattr(film_synthesis_hr_minimax, "refine_hr_minimax"):
        wrap_timing(film_synthesis_hr_minimax, "refine_hr_minimax", "hr_minimax_seconds")
    from coat_sim.core import film_synthesis_multispectral_minimax as general_minimax
    wrap_timing(general_minimax, "refine_multispectral_minimax", "general_minimax_seconds")

    def on_progress(value: dict[str, Any], **_: Any) -> None:
        nonlocal last_progress, progress_count
        now = time.perf_counter()
        last_progress = now
        progress_count += 1
        latest.update(value)
        stage = str(value.get("stage", ""))
        if stage.endswith("targets_verified") or stage in {"validation", "validation_complete", "completed", "failed"}:
            milestones.append({"stage": stage, "elapsed_seconds": now - start,
                               "evaluations": value.get("evaluations")})
        if now - last_log >= 10 or stage.endswith("targets_verified"):
            log("progress", stage=stage, evaluations=value.get("evaluations"),
                best_error=value.get("best_max_normalized_error"),
                cancel_requested=cancel.is_set())

    def checkpoint_writer(path: str | Path, value: dict[str, Any]) -> None:
        nonlocal first_audit_pass_seconds
        atomic_json(Path(path), value)
        score = value.get("validation_score", {})
        if (first_audit_pass_seconds is None
                and nominal_targets_pass(request, score, independent_audit=True)):
            first_audit_pass_seconds = time.perf_counter() - start

    def monitor() -> None:
        nonlocal last_log
        while not stop_monitor.wait(1.0):
            now = time.perf_counter()
            elapsed = now - start
            if now - last_log >= 10:
                log("heartbeat", stage=latest.get("stage"), evaluations=latest.get("evaluations"),
                    progress_age_seconds=round(now - last_progress, 3),
                    parent_budget_seconds=parent_budget, cancel_requested=cancel.is_set())
            if elapsed >= parent_budget + watchdog_grace and not cancel.is_set():
                cancel.set()
                log("cancellation_requested", reason="parent_budget_watchdog_grace_exceeded", grace_seconds=watchdog_grace)
            if elapsed >= parent_budget + watchdog_grace:
                # The execute service cooperates with this event. The invoking
                # parent process remains responsible for an OS hard watchdog.
                cancel.set()
                if now - last_log >= 1:
                    log("watchdog_grace_elapsed", reason="cooperative_cancel_overrun")

    monitor_thread = threading.Thread(target=monitor, name="multispectral-budget-monitor", daemon=True)
    request_preparation_seconds = 0.0
    execution_seconds = 0.0
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    log("start", max_seconds=parent_budget, watchdog_grace_seconds=watchdog_grace,
        case_kind="exploratory_research_not_four_window_master_acceptance")
    monitor_thread.start()
    try:
        prep_start = time.perf_counter()
        _service, request = build_case_request(case, defaults, rows, args.seed, 1)
        request = replace(request, max_seconds=case["max_seconds"],
                          position_samples=case["position_samples"],
                          validation_samples_multiplier=defaults["validation_samples_multiplier"],
                          acceptable_normalized_error=defaults["acceptable_normalized_error"])
        envelope = film_synthesis_request_to_payload(
            f"{args.case}-production-{args.seed}", request)
        parameters = canonical_parameters(None, {"payload": envelope})
        job = build_job_payload(parameters)
        _, decoded, structures = deserialize_job_payload(job)
        if structures or decoded.seed_designs or decoded.environment.get("front_layers"):
            raise ValueError("Frozen job unexpectedly contains an answer seed or front layers")
        atomic_json(output / "request.json", envelope)
        atomic_json(output / "job.json", job)
        request_preparation_seconds = time.perf_counter() - prep_start
        execution_start = time.perf_counter()
        response = execute_job({"payload": job}, cancel, on_progress,
                               output / "checkpoint.json", checkpoint_writer, str(output))
        execution_seconds = time.perf_counter() - execution_start
        if not is_synthesis_result(response):
            raise ValueError("Production execute_job returned an invalid synthesis result")
        result = response
        atomic_json(output / "result.json", response)
    except Exception as exc:  # Preserve a useful record when the production chain fails.
        error = {"type": type(exc).__name__, "message": str(exc)}
        atomic_json(output / "error.json", error)
        log("error", **error)
    finally:
        stop_monitor.set()
        monitor_thread.join(timeout=3)

    final_head = _head_commit()
    final_sources = _source_hashes()
    source_hashes_match = initial_sources == final_sources
    head_matches = initial_head == final_head
    body = (result or {}).get("result", {})
    objectives = jsonable(getattr(locals().get("request"), "objectives", ()))
    score = body.get("validation_score")
    errors = score.get("errors", []) if isinstance(score, dict) else []
    worst_audited = []
    for index, objective in enumerate(objectives):
        selected = [row for row in errors if row.get("objective_index") == index]
        actual_values = [row.get("actual") for row in selected if isinstance(row.get("actual"), (int, float))]
        worst_audited.append({"objective_index": index, "metric": objective.get("metric"),
            "wavelength_nm": [objective.get("wavelength_min_nm"), objective.get("wavelength_max_nm")],
            "lower_bound": objective.get("lower_bound"), "upper_bound": objective.get("upper_bound"),
            "actual_min": min(actual_values) if actual_values else None,
            "actual_max": max(actual_values) if actual_values else None})

    summary = {
        "case": args.case, "seed": args.seed, "variant": "production",
        "classification": "exploratory_research_not_four_window_master_acceptance",
        "state": body.get("state", "runner_failed"),
        "termination_reason": body.get("termination_reason"),
        "targets_met": body.get("targets_met"), "error": error,
        "total_seconds": time.perf_counter() - start, "cpu_seconds": time.process_time() - cpu_start,
        "parent_budget_seconds": parent_budget, "watchdog_grace_seconds": watchdog_grace,
        "cancel_requested": cancel.is_set(),
        "request_preparation_seconds": request_preparation_seconds,
        "execution_seconds": execution_seconds, **timings,
        "first_audited_success_seconds": first_audit_pass_seconds,
        "evaluations": body.get("evaluations"), "validation_evaluations": body.get("validation_evaluations"),
        "audit_physical_evaluations": body.get("audit_physical_evaluations"),
        "audit_wavelength_points": body.get("audit_wavelength_points"),
        "layers": len((body.get("candidate_design") or {}).get("front_layers", [])),
        "worst_audited": worst_audited, "progress_records": progress_count,
        "milestones": milestones, "base_request_canonical_sha256": base_sha,
        "head_before": initial_head, "head_after": final_head, "head_unchanged": head_matches,
        "runtime_sources_unchanged": source_hashes_match,
    }
    atomic_json(output / "summary.json", summary)
    atomic_json(output / "identity.json", {
        "protocol": str(protocol_path.relative_to(ROOT)),
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "base_request_canonical_sha256": base_sha,
        "head": initial_head, "runtime_source_sha256": _canonical_sha256(initial_sources),
        "runtime_source_hashes": initial_sources, "process": process_info,
    })
    log("final", state=summary["state"], termination_reason=summary["termination_reason"],
        targets_met=summary["targets_met"], total_seconds=summary["total_seconds"],
        audit_seconds=timings["audit_seconds"], hr_minimax_seconds=timings["hr_minimax_seconds"],
        general_minimax_seconds=timings["general_minimax_seconds"],
        evaluations=summary["evaluations"], layers=summary["layers"],
        head_unchanged=head_matches, runtime_sources_unchanged=source_hashes_match)
    return 0 if result is not None and head_matches and source_hashes_match else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, help="Frozen protocol case ID")
    parser.add_argument("--seed", required=True, type=int, help="Protocol-frozen seed")
    parser.add_argument("--output", required=True, type=Path, help="New directory under system Temp")
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_RELATIVE,
                        help="Optional protocol path inside this repository")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return _run(_parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
