"""Bounded paired progress-policy experiments, never a production policy default.

Preflight is read-only. Execution is explicit, serial, and uses the original
material snapshots, full grids, double precision and existing independent audit.
F and FJ budgets are separate vectors, not an invented FLOP equivalence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.run_film_synthesis_acceptance import atomic_json, jsonable, sha256_json, utc_now  # noqa: E402

SUITE = ROOT / "benchmarks/film_synthesis/20261024/representative-suite-current-pointwise.json"
CASE_IDS = ("ar_broad_visible", "hr_narrow_visible", "bpf_narrow_visible", "mixed_multispectral_v1")
POLICY_KEYS = {"accepted_window_steps", "relative_sse_progress_floor",
               "relative_worst_progress_floor", "stagnation_windows"}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != POLICY_KEYS:
        raise ValueError("Explicit policy requires exactly the four declared fields")
    for key in ("accepted_window_steps", "stagnation_windows"):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in POLICY_KEYS - {"accepted_window_steps", "stagnation_windows"}:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f"{key} must be finite and in [0, 1]")
    return dict(value)


def derive_case(source, mode):
    """New BOTH inputs copy bounds, not historical qualification or success."""
    if mode not in ("pointwise", "both"):
        raise ValueError("mode must be pointwise or both")
    result = deepcopy(source)
    # This is a newly authored research input, not a runtime legacy decoder.
    # Archived source bytes remain unchanged and are never passed to load_suite.
    result["objective_progress_policy"] = None
    if mode == "both":
        result["id"] += "_derived_both_v1"
        for objective in result["objectives"]:
            if not objective["pointwise"]["enabled"] or objective["mean"]["enabled"]:
                raise ValueError("Source must retain its registered PW-only modes")
            objective["mean"] = deepcopy(objective["pointwise"])
    result.pop("historical_qualification", None)
    return result


def load_registered_materials(plan):
    """Read pinned catalog evidence for the explicit NEW cases in this plan.

    Do not relax the production runner's deliberate rejection of the archived
    manifest. Its old request contract is not accepted or decoded at runtime.
    Each plan case instead declares the current policy field explicitly and is
    validated by the current GUI adapter when a new frozen request is built.
    """
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    catalog = (ROOT / suite["material_catalog"]).resolve()
    if not catalog.is_relative_to(ROOT.resolve()):
        raise ValueError("Pinned catalog must stay inside the repository")
    content = catalog.read_bytes()
    if hashlib.sha256(content).hexdigest() != plan["catalog_sha256"]:
        raise ValueError("Pinned material catalog changed")
    records = json.loads(content)["materials"]
    rows = [dict(id=row["builtin_material_id"], name=row["material_id"], design_ready=True,
                 optical_sets=deepcopy(row["optical_constants"])) for row in records]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Pinned catalog contains duplicate material identities")
    return suite["defaults"], rows


@dataclass(frozen=True)
class Work:
    kernel_calls: int = 0
    wavelength_points: int = 0
    layer_points: int = 0

    def __add__(self, other):
        return Work(*(getattr(self, key) + getattr(other, key) for key in self.__dataclass_fields__))

    def fits(self, cap):
        return all(getattr(self, key) <= getattr(cap, key) for key in self.__dataclass_fields__)

    def scale(self, factor):
        return Work(*(getattr(self, key) * factor for key in self.__dataclass_fields__))


def grid_work(objectives, layers):
    """Match the current evaluator's complete objective/angle/chunk schedule."""
    limit = min(10001, 1_000_000 // (layers + 3))
    if limit < 1:
        raise ValueError("Unsupported physical layer count")
    calls = points = 0
    for objective in objectives:
        samples = objective["wavelength_samples"]
        angles = len(objective["angles_deg"])
        calls += math.ceil(samples / limit) * angles
        points += samples * angles
    return Work(calls, points, points * max(1, layers))


def audit_upper_work(objectives, multiplier, layers):
    """Conservative complete-audit reservation, using existing extrema maxiter.

    Reserve dense + offset + every possible bracket, on both bounded sides.
    This changes no audit work or convergence setting. Unused reservation is
    released after the complete audit; actual kernel work is recorded separately.
    """
    from coat_sim.core.film_synthesis_audit import EXTREMUM_MAXITER
    total = Work()
    for objective in objectives:
        dense = deepcopy(objective)
        n = objective["wavelength_samples"]
        dense["wavelength_samples"] = 1 if n == 1 else (n - 1) * multiplier + 1
        d = dense["wavelength_samples"]
        total += grid_work((dense,), layers)
        if d > 1:
            offset = {**dense, "wavelength_samples": d - 1}
            total += grid_work((offset,), layers)
        bound = objective["pointwise"]
        sides = (2 if bound["mode"] == "value" else
                 int(bound.get("lower_bound") is not None) + int(bound.get("upper_bound") is not None))
        if bound["enabled"] and d > 1:
            scalar_points = max(0, 2 * d - 3) * sides * EXTREMUM_MAXITER * len(objective["angles_deg"])
            total += Work(scalar_points, scalar_points, scalar_points * max(1, layers))
    return total


def source_identity():
    paths = sorted((ROOT / "coat_sim").rglob("*.py"))
    paths += [Path(__file__), ROOT / "tools/run_film_synthesis_benchmarks.py",
              ROOT / "tools/run_film_synthesis_acceptance.py", SUITE, ROOT / "data/optical_catalog.json"]
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        text=True, capture_output=True).stdout.strip()
    return dict(git_head=head, sha256=sha256_json(hashes), files=hashes)


def make_plan(*, policy, seeds=(0,), sweeps=64, wall_seconds=30., audit_seconds=10.):
    validate_policy(policy)
    if not seeds or len(set(seeds)) != len(seeds) or any(type(seed) is not int or seed not in (0, 1, 2) for seed in seeds):
        raise ValueError("Select distinct registered seeds 0, 1, 2")
    if type(sweeps) is not int or sweeps < 1:
        raise ValueError("sweeps must be a positive integer")
    if (not math.isfinite(wall_seconds) or not math.isfinite(audit_seconds)
            or not 0 < audit_seconds < wall_seconds):
        raise ValueError("Require 0 < reserved audit seconds < total wall seconds")
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    cases = []
    for source in suite["cases"]:
        if source["id"] not in CASE_IDS:
            continue
        layers = source["max_layers"]
        training = grid_work(source["objectives"], layers).scale(sweeps)
        audit = audit_upper_work(source["objectives"], suite["defaults"]["validation_samples_multiplier"], layers).scale(2)
        for mode in ("pointwise", "both"):
            case = derive_case(source, mode)
            cases.append(dict(source_case_id=source["id"], case=case, mode=mode,
                source_case_sha256=sha256_json(source), case_sha256=sha256_json(case),
                derivation=("New current-contract research input adds explicit null policy; original PW goals unchanged" if mode == "pointwise" else
                    "New current-contract research input adds explicit null policy; copy original enabled PW constraint verbatim to enabled mean; all other optics/grid fields unchanged"),
                training_caps={"F": jsonable(training), "FJ": jsonable(training)},
                audit_caps={"F": jsonable(audit), "FJ": jsonable(Work())}))
    return dict(kind="film_progress_paired_plan", schema_version=1, created_utc=utc_now(),
        suite_path=SUITE.relative_to(ROOT).as_posix(), suite_sha256=hashlib.sha256(SUITE.read_bytes()).hexdigest(),
        catalog_sha256=suite["catalog_sha256"], policy=policy, seeds=list(seeds), cases=cases,
        arms={"off": None, "on": policy}, cpu_workers=1, math_threads=1,
        total_wall_seconds=wall_seconds, search_admission_seconds=wall_seconds-audit_seconds,
        reserved_audit_seconds=audit_seconds, max_logical_evaluations=4096,
        runtime=dict(python=sys.version, platform=platform.platform(),
            numpy=version("numpy"), scipy=version("scipy"),
            thread_environment={name: os.environ.get(name) for name in (
                "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "BLIS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")}),
        notes=["Same admission caps, not equal actual physical work or FLOPs",
               "Separate F/FJ kernel calls, wavelength points and layer-points; never combined",
               "Layer-points are an explicit work descriptor, not calibrated CPU equivalence",
               "Per-run reservation allows two worst-case complete audits; no mid-candidate physical-cap stop",
               "Wall cancellation is cooperative and may interrupt an audit; incomplete audit never passes",
               "Derived BOTH goals are new tests, not historical optical qualification",
               "Catalog cases are not used to tune the predeclared policy; no general improvement claim",
               "Development/reference four-window case is not part of this catalog matrix"],
        source=source_identity())


class PhysicalLedger:
    """Serial harness instrumentation; no production settings or optics edits."""
    def __init__(self, row, search_deadline):
        self.caps = {phase: {kind: Work(**cap) for kind, cap in row[key].items()}
                     for phase, key in (("training", "training_caps"), ("audit", "audit_caps"))}
        self.actual = {phase: {kind: Work() for kind in ("F", "FJ")} for phase in self.caps}
        self.attempted_calls = {phase: {kind: 0 for kind in ("F", "FJ")} for phase in self.caps}
        self.phase = "training"
        self.serial_pre_admitted = False
        self.training_objectives = row.get("case", {}).get("objectives")
        self.search_deadline = search_deadline
        self.stop_reason = None
        self.incomplete_kernel_calls = 0
        self.reservation_violations = []
        self.admission_events = []

    def admit(self, phase, kind, work):
        from coat_sim.core.film_auto_synthesis import _Stopped
        reason = ("search_wall_reservation" if phase == "training" and time.monotonic() >= self.search_deadline else
                  "physical_work_cap" if not (self.actual[phase][kind] + work).fits(self.caps[phase][kind]) else None)
        if reason:
            self.stop_reason = reason
            self.admission_events.append(dict(phase=phase, kind=kind, admitted=False, reason=reason, planned=jsonable(work)))
            raise _Stopped("evaluation_limit")
        self.admission_events.append(dict(phase=phase, kind=kind, admitted=True, planned=jsonable(work)))

    def report(self):
        return dict(caps=jsonable(self.caps), completed=jsonable(self.actual), attempted_kernel_calls=self.attempted_calls,
            incomplete_kernel_calls=self.incomplete_kernel_calls, reservation_violations=self.reservation_violations,
            complete=self.incomplete_kernel_calls == 0 and not self.reservation_violations,
            caps_respected=all(self.actual[p][k].fits(self.caps[p][k]) for p in self.caps for k in ("F", "FJ")),
            stop_reason=self.stop_reason, admissions=self.admission_events,
            scope="Current serial structure kernel; calls count actual simulate_with_thickness_jacobian invocations, including F with zero derivative columns")

    @contextmanager
    def instrument(self):
        from coat_sim.core import film_auto_synthesis as synthesis
        from coat_sim.core import film_optimization_structure as structure
        from coat_sim.core import film_synthesis_audit as audit
        from coat_sim.core.film_synthesis_parallel import OrderedEvaluationExecutor
        actual_entry = structure.evaluate_frozen_structure_candidate
        actual_kernel = structure.simulate_with_thickness_jacobian
        actual_audit = audit.audit_frozen_synthesis_candidate
        actual_serial = OrderedEvaluationExecutor.serial

        def entry(request, design, **kwargs):
            if kwargs.get("forward_responses") is not None:
                raise ValueError("Acceptance ledger requires actual CPU physics, not precomputed backend responses")
            objectives = jsonable(request.base_request.objectives)
            if kwargs.get("derived_validation_grid"):
                factor = request.base_request.validation_samples_multiplier
                for objective in objectives:
                    n = objective["wavelength_samples"]
                    objective["wavelength_samples"] = 1 if n == 1 else (n-1)*factor+1
            layers = len(design.get("front_layers", ())) + len(design.get("back_layers", ()))
            kind = "FJ" if kwargs.get("with_jacobian") and kwargs.get("variable_ids") else "F"
            planned = grid_work(objectives, layers)
            before = self.actual[self.phase][kind]
            if self.phase == "training" and not self.serial_pre_admitted:
                self.admit("training", kind, planned)
            value = actual_entry(request, design, **kwargs)
            if self.actual[self.phase][kind] != before + planned:
                self.reservation_violations.append("completed candidate kernel work differs from exact full-grid plan")
            return value

        def serial(executor, callback, task, **kwargs):
            # Admit before the executor enters its physical callback. A rejected
            # candidate must not be counted there as unknown interrupted work.
            if self.training_objectives is None:
                raise ValueError("Serial physical ledger requires frozen training objectives")
            layers = len(task.design.get("front_layers", ())) + len(task.design.get("back_layers", ()))
            kind = "FJ" if task.jacobian and task.design.get("front_layers") else "F"
            self.admit("training", kind, grid_work(self.training_objectives, layers))
            previous, self.serial_pre_admitted = self.serial_pre_admitted, True
            try:
                return actual_serial(executor, callback, task, **kwargs)
            finally:
                self.serial_pre_admitted = previous

        def kernel(design, payload, variables, settings, **kwargs):
            phase, kind = self.phase, "FJ" if variables else "F"
            self.attempted_calls[phase][kind] += 1
            try:
                value = actual_kernel(design, payload, variables, settings, **kwargs)
            except BaseException:
                self.incomplete_kernel_calls += 1
                raise
            points = len(value["wavelength_nm"])
            layers = len(design.get("front_layers", ())) + len(design.get("back_layers", ()))
            self.actual[phase][kind] += Work(1, points, points * max(1, layers))
            return value

        def independent_audit(request, design, **kwargs):
            layers = len(design.get("front_layers", ())) + len(design.get("back_layers", ()))
            upper = audit_upper_work(jsonable(request.objectives), request.validation_samples_multiplier, layers)
            self.admit("audit", "F", upper)
            previous, self.phase = self.phase, "audit"
            try:
                return actual_audit(request, design, **kwargs)
            finally:
                self.phase = previous

        with patch.object(structure, "evaluate_frozen_structure_candidate", entry), \
             patch.object(synthesis, "evaluate_frozen_structure_candidate", entry), \
             patch.object(structure, "simulate_with_thickness_jacobian", kernel), \
             patch.object(OrderedEvaluationExecutor, "serial", serial), \
             patch.object(audit, "audit_frozen_synthesis_candidate", independent_audit):
            yield


def score_metrics(score):
    """Saved raw evidence only. Means for disabled modes remain diagnostic."""
    if score is None:
        return None
    score = jsonable(score)
    per_band = []
    # Audit mean remains fixed dense-grid; extrema/offset are never integration weights.
    for row in score.get("coverage", []):
        waves, values = row["wavelength_nm"], row["actual_values"]
        mean = (values[0] if len(waves) == 1 else
                sum((b-a)*(x+y)/2 for a,b,x,y in zip(waves[:-1],waves[1:],values[:-1],values[1:], strict=True)) / (waves[-1]-waves[0]))
        audited = next((a for a in score.get("audit_coverage", [])
            if a["objective_index"] == row["objective_index"] and a["angle_deg"] == row["angle_deg"]), row)
        per_band.append(dict(objective_index=row["objective_index"], angle_deg=row["angle_deg"],
            sampled_min=min(audited["actual_values"]), sampled_max=max(audited["actual_values"]),
            fixed_grid_mean=mean,
            mean_measure=score.get("audit", {}).get("mean_measure", "original_training_grid")))
    rms = score.get("weighted_rms_error")
    return dict(max_error=score.get("max_error"), weighted_rms_error=rms,
        weighted_sse=(None if rms is None else rms*rms),
        weighted_sse_scope="Square of canonical active-constraint normalized weighted RMS; no sparse feedback guide contribution",
        mean_constraints=[e for e in score.get("errors", []) if e.get("reduction") == "mean"],
        pointwise_max_error=max((e["normalized_violation"] for e in score.get("errors", []) if e.get("reduction", "pointwise") == "pointwise"), default=None),
        per_band=per_band, nominal_pass=score.get("nominal_pass"), solver_acceptable=score.get("solver_acceptable"))


def run_one(service, request, row, plan):
    from coat_sim.core.film_auto_synthesis import synthesize_film_design
    from tools.run_film_synthesis_benchmarks import raw_acceptance, candidate_evidence_matches, verify_cleanup
    if request.cpu_workers != 1 or request.forward_backend != "cpu":
        raise ValueError("Ledger only supports serial CPU execution; no unobserved worker work")
    started = time.monotonic()
    cpu_started = time.process_time()
    ledger = PhysicalLedger(row, started + plan["search_admission_seconds"])
    observed = []
    with ledger.instrument():
        result = synthesize_film_design(service, request,
            progress_callback=lambda event: observed.append(dict(elapsed_seconds=time.monotonic()-started, event=jsonable(event))))
    elapsed = time.monotonic()-started
    design = jsonable(result.candidate_design)
    training, validation = jsonable(result.training_score), jsonable(result.validation_score)
    checked_training = raw_acceptance(jsonable(request), training)
    checked_audit = raw_acceptance(jsonable(request), validation, scope="audit")
    matches = candidate_evidence_matches(design, training, validation)
    work = ledger.report()
    thicknesses = [row["thickness"]["value"] for row in (design or {}).get("front_layers", [])]
    performance = jsonable(result.performance)
    physical_ok = (len(thicknesses) <= request.max_layers and
        all(row["thickness"]["unit"] == "nm" for row in (design or {}).get("front_layers", [])) and
        all(request.minimum_nonzero_thickness_nm <= d <= request.max_layer_thickness_nm for d in thicknesses) and
        sum(thicknesses) <= request.max_total_thickness_nm)
    cleanup = verify_cleanup(performance)
    serial_only = performance.get("submitted_tasks") == 0 and performance.get("unknown_tasks") == 0
    feedback = performance.get("audit_feedback", {})
    observed_training_points = sum(ledger.actual["training"][kind].wavelength_points for kind in ("F", "FJ"))
    expected_training_points = performance.get("training_wavelength_points")
    if expected_training_points is not None:
        expected_training_points += feedback.get("wavelength_points", 0)
    reconciled = dict(serial_only=serial_only,
        training_points_match=observed_training_points == expected_training_points,
        observed_training_points=observed_training_points, expected_training_points=expected_training_points,
        audit_points_match=ledger.actual["audit"]["F"].wavelength_points == result.audit_wavelength_points,
        scope="Parent training physics plus separately recorded feedback points; audit point totals, not evaluator-call counts")
    accounting_valid = work["complete"] and serial_only and reconciled["training_points_match"] and reconciled["audit_points_match"]
    return dict(kind="film_progress_paired_result", result=jsonable(result), observed_progress=observed,
        summary=dict(state=result.state, termination_reason=result.termination_reason, elapsed_seconds=elapsed,
            process_cpu_seconds=time.process_time()-cpu_started,
            cpu_scope="Single serial process including optimizers and audit; not a calibrated F/FJ equivalence",
            training=score_metrics(training), audit=score_metrics(validation), training_raw_bounds=checked_training,
            audit_raw_bounds=checked_audit, candidate_audit_matches=matches, work=work,
            layers=len(thicknesses), total_thickness_nm=sum(thicknesses), minimum_layer_nm=min(thicknesses, default=None),
            explicit_manufacturing_constraints_pass=physical_ok, cleanup=cleanup, accounting_reconciliation=reconciled,
            accounting_valid=accounting_valid,
            audited_success=(result.state == "completed" and result.targets_met and result.validation_complete and matches and physical_ok and cleanup["verified"]
                and checked_training["passed"] is True and checked_audit["passed"] is True
                and accounting_valid and work["caps_respected"]),
            policy_handoff_observed=any(r.stage == "objective_progress_handoff" for r in result.trace),
            execution_scope="Direct frozen production core; not GUI/durable job execution; no continuous optical certificate"))


def execute_plan(plan, output):
    from tools import run_film_synthesis_benchmarks as runner
    from coat_sim.core.film_synthesis_progress import HybridProgressPolicy
    if plan["source"]["sha256"] != source_identity()["sha256"]:
        raise ValueError("Sources changed after predeclaration; regenerate plan before any optics")
    defaults, rows = load_registered_materials(plan)
    output = runner.reserve_output(output)
    atomic_json(output / "plan.json", plan)
    summaries = []
    for index, row in enumerate(plan["cases"]):
        for seed in plan["seeds"]:
            service, baseline = runner.build_case_request(row["case"], defaults, rows, seed, 1)
            baseline = replace(baseline, objective_progress_policy=None, max_seconds=plan["total_wall_seconds"],
                max_evaluations=plan["max_logical_evaluations"], enable_thickness_reduction=False)
            base_hash = sha256_json(baseline)
            # Alternate arm order without tuning any policy or budget.
            order = ("off", "on") if (index + seed) % 2 == 0 else ("on", "off")
            for arm in order:
                if plan["source"]["sha256"] != source_identity()["sha256"]:
                    raise ValueError("Sources changed during paired run; do not pool results")
                request = replace(baseline, objective_progress_policy=(None if arm == "off" else HybridProgressPolicy(**plan["policy"])))
                name = f"{row['case']['id']}-seed{seed}-{arm}"
                atomic_json(output / f"{name}-request.json", dict(base_request_sha256=base_hash,
                    treatment=arm, request_sha256=sha256_json(request), request=jsonable(request)))
                result = run_one(service, request, row, plan)
                result.update(case_id=row["case"]["id"], mode=row["mode"], seed=seed, arm=arm,
                    base_request_sha256=base_hash, request_sha256=sha256_json(request))
                result["sources_unchanged"] = plan["source"]["sha256"] == source_identity()["sha256"]
                atomic_json(output / f"{name}-result.json", result)
                summaries.append({key: result[key] for key in ("case_id", "mode", "seed", "arm", "base_request_sha256", "summary", "sources_unchanged")})
                atomic_json(output / "summary.json", summaries)
                print(json.dumps(dict(case=name, state=result["summary"]["state"],
                    elapsed_seconds=result["summary"]["elapsed_seconds"],
                    audited_success=result["summary"]["audited_success"],
                    handoff=result["summary"]["policy_handoff_observed"],
                    training=result["summary"]["training"], work_complete=result["summary"]["work"]["complete"]),
                    ensure_ascii=False), flush=True)
                if (not result["sources_unchanged"] or result["summary"]["state"] == "failed"
                        or not result["summary"]["accounting_valid"]
                        or not result["summary"]["work"]["caps_respected"]):
                    raise RuntimeError("Invalid/failed cell; inspect preserved result before continuing")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-json", required=True, help="Explicit four-field policy JSON, no hidden threshold defaults")
    parser.add_argument("--seeds", nargs="+", type=int, default=[0])
    parser.add_argument("--sweeps", type=int, default=64)
    parser.add_argument("--wall-seconds", type=float, default=30.)
    parser.add_argument("--audit-seconds", type=float, default=10.)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    plan = make_plan(policy=json.loads(args.policy_json), seeds=args.seeds, sweeps=args.sweeps,
        wall_seconds=args.wall_seconds, audit_seconds=args.audit_seconds)
    if args.execute:
        if args.output is None:
            parser.error("Execution needs a NEW --output directory under system Temp")
        print(execute_plan(plan, args.output))
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
