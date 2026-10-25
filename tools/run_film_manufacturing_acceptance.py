"""Frozen synthetic real-TMM acceptance protocol; dry-run is the default.

No optical dispatch occurs while preparing or testing the manifest. Execution
requires an explicit approved manifest digest, a matching clean source tree and
--execute. These small synthetic fixtures are not measured material evidence,
a user-case grid reduction, a continuous certificate or a tuning corpus.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import importlib.util
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch
from typing import Any, Literal, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.run_film_synthesis_acceptance import atomic_json, jsonable, sha256_json, utc_now  # noqa: E402
from tools.run_film_progress_acceptance import Work, grid_work, audit_upper_work, score_metrics  # noqa: E402

FAMILIES = ("AR", "HR", "BPF", "mixed")
MODES = ("mean", "pointwise", "both")
SEEDS = (0, 1)
ROUTES = ("raise_selected", "delete_selected_merge")
MEMORY_CAP = 512 * 1024**2
SOURCE_SECONDS = 5.0
ROUTE_SECONDS = 5.0
CELL_SECONDS = 15.0
REFINEMENT_CALLS = 6
MAX_ITERATIONS = 6
THREAD_VARIABLES = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")
TARGETS = {
    "AR": (("R", 520., 580., None, .05),),
    "HR": (("R", 530., 570., .8, None),),
    "BPF": (("R", 500., 520., .8, None), ("R", 600., 620., .8, None),
            ("T", 550., 570., .8, None)),
    "mixed": (("R", 500., 550., .12, None), ("T", 600., 650., .6, None)),
}
SHAPES = {"AR": ("low", "high", "low"), "HR": ("high", "low") * 4,
          "BPF": ("high", "low", "high", "low", "high", "low", "high", "low", "high"),
          "mixed": ("high", "low", "high")}


@dataclass(frozen=True)
class Fixture:
    cell_id: str
    family: str
    mode: str
    seed: int
    request: Any
    design: dict
    defect_id: str


def _test_fixture_factory():
    """Use the named repository fixture itself, not a substitute material set."""
    path = ROOT / "tests/test_film_auto_synthesis.py"
    spec = importlib.util.spec_from_file_location("_manufacturing_fixture_source", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the registered synthetic material fixture")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.setup_problem


def make_fixture(family, mode, seed):
    import numpy as np
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_design import validate_design
    from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
    from coat_sim.core.film_optimization import FilmOptimizationObjective

    if family not in FAMILIES or mode not in MODES or type(seed) is not int or seed not in SEEDS:
        raise ValueError("Only the frozen 4 x 3 x 2 fixture grid is registered")
    service, base = _test_fixture_factory()()
    objectives = []
    for metric, low, high, lower, upper in TARGETS[family]:
        bound = FilmObjectiveConstraint(lower_bound=lower, upper_bound=upper)
        objectives.append(FilmOptimizationObjective(low, high, 5, angles_deg=(0., 15.),
            metric=cast(Literal["R", "T"], metric), polarization="unpolarized", mean=replace(bound, enabled=mode != "pointwise"),
            pointwise=replace(bound, enabled=mode != "mean")))
    cell_id = f"{family.lower()}_{mode}_seed{seed}"
    environment = deepcopy(base.environment)
    environment.update(id=cell_id, name=f"Synthetic manufacturing acceptance {cell_id}")
    # Freeze actual tabulated n/k, domains, settings and resource policy, without
    # invoking any optical solver. No service data is fetched from outside tests.
    request = freeze_film_synthesis_request(service, replace(base, environment=environment,
        objectives=tuple(objectives), minimum_nonzero_thickness_nm=1., max_layer_thickness_nm=300.,
        max_total_thickness_nm=None, max_layers=None, max_cycles=None, max_evaluations=None,
        validation_samples_multiplier=2, max_working_set_bytes=MEMORY_CAP,
        max_seconds=None, cpu_workers=1, forward_backend="cpu", structure_search_seed=seed,
        parallel_batch_size=1, resource_guard_resolution=None))
    assert request.frozen_material_snapshot is not None
    material_rows = {r["id"]: r for r in request.frozen_material_snapshot["materials"]}
    rng = np.random.default_rng(seed)
    design = deepcopy(dict(request.environment))
    rows: list[dict[str, Any]] = []
    for index, material in enumerate(SHAPES[family]):
        optical = next(o for o in material_rows[material]["optical_sets"] if o["id"] == "nk")
        n = optical["n"][0]
        if len(set(optical["n"])) != 1 or set(optical["k"]) != {0.}:
            raise ValueError("Registered material must remain synthetic, nondispersive and known lossless")
        # Only nondefect layers consume the RNG sequence. The first interior
        # contrasting layer is always index 1, never selected by a threshold.
        thickness = 2. if index == 1 else 550. / (4. * n) * float(rng.uniform(.9, 1.1))
        if family == "BPF" and index == 4:
            thickness *= 2.
        rows.append(dict(id=f"{cell_id}_layer_{index:02d}", material_id=material, optical_set_id="nk",
                         thickness=dict(value=thickness, unit="nm"), note="synthetic acceptance fixture"))
    design["front_layers"] = rows
    return Fixture(cell_id, family, mode, seed, request, validate_design(design), rows[1]["id"])


def repair_request(fixture):
    from coat_sim.core.film_synthesis_manufacturing import ManufacturingRepairRequest, manufacturing_source_fingerprint
    return ManufacturingRepairRequest(fixture.request, manufacturing_source_fingerprint(fixture.request, fixture.design),
        (fixture.defect_id,), 8., REFINEMENT_CALLS, MAX_ITERATIONS, ROUTE_SECONDS)


def prepared_routes(fixture):
    from coat_sim.core.film_synthesis_manufacturing import ManufacturingSnapshot, prepare_manufacturing_routes
    # This geometry-only API does not evaluate, validate or claim score evidence.
    return prepare_manufacturing_routes(repair_request(fixture), ManufacturingSnapshot(fixture.design, {}))


def audit_memory_model(request, design):
    """Conservative explicit project model, not an OS/RSS isolation guarantee.

    Reserve all potential extrema samples under the existing maxiter, not just
    a TMM chunk. Allow 2 KiB per retained sample/condition for auditor containers,
    another 2 KiB for canonical validation, and three independent compact score
    copies at 2 KiB each. Dense physics scratch and the fixed 128 MiB reserve are
    additional. This may reject a small fixture under its fixed cap; do not tune
    the estimate or audit settings to force admission. External owners are extra.
    """
    objectives = jsonable(request.objectives)
    layers = len(design["front_layers"])
    upper = audit_upper_work(objectives, request.validation_samples_multiplier, layers)
    dense_points = sum((1 if o.wavelength_samples == 1 else
        (o.wavelength_samples - 1) * request.validation_samples_multiplier + 1) * len(o.angles_deg)
        for o in request.objectives)
    mean_rows = sum(len(o.angles_deg) for o in request.objectives if o.mean.enabled)
    records = upper.wavelength_points + mean_rows
    components = dict(fixed_workspace=128 * 1024**2,
        dense_physics_workspace=512 * dense_points * (layers + 3),
        auditor_accumulation=2048 * records,
        canonical_validation=2048 * records,
        three_compact_score_copies=3 * 2048 * records)
    return dict(total_bytes=sum(components.values()), components=components,
        upper_sample_condition_records=records, complete_audit_work=jsonable(upper),
        is_os_memory_guarantee=False)


def _phase_caps(fixture):
    request = fixture.request
    objectives = jsonable(request.objectives)
    zero = Work()
    result = {}
    for route, design in (("source", fixture.design), *((r.route, r.design) for r in prepared_routes(fixture))):
        layers = len(design["front_layers"])
        if not layers:
            raise ValueError("The fixed acceptance protocol requires nonempty source and repair topologies")
        grid = grid_work(objectives, layers)
        result[f"{route}.start"] = {"F": zero, "FJ": grid}
        if route != "source":
            result[f"{route}.refinement"] = {"F": zero, "FJ": grid.scale(REFINEMENT_CALLS)}
        result[f"{route}.audit"] = {"F": audit_upper_work(objectives, request.validation_samples_multiplier, layers), "FJ": zero}
    return result


def fixture_manifest(fixture):
    from coat_sim.core.film_synthesis_manufacturing import manufacturing_refinement_working_set_bytes
    routes = prepared_routes(fixture)
    request = fixture.request
    return dict(cell_id=fixture.cell_id, family=fixture.family, mode=fixture.mode, seed=fixture.seed,
        request=jsonable(request), source_design=deepcopy(fixture.design), defect_layer_ids=[fixture.defect_id],
        source_fingerprint=repair_request(fixture).source_fingerprint,
        material_sha256=sha256_json(request.frozen_material_snapshot),
        source_design_sha256=sha256_json(fixture.design),
        routes=[dict(route=r.route, design=r.design, lower_bounds_nm=r.lower_bounds_nm,
                     removed_ids=r.removed_ids, merged_ids=r.merged_ids, status=r.status) for r in routes],
        phase_work_caps=jsonable(_phase_caps(fixture)),
        memory_models={name: dict(training_bytes=manufacturing_refinement_working_set_bytes(request, len(d["front_layers"])),
            audit=audit_memory_model(request, d)) for name, d in
            (("source", fixture.design), *((r.route, r.design) for r in routes))})


def protocol():
    from coat_sim.core.film_synthesis_audit import EXTREMUM_MAXITER, EXTREMUM_XATOL
    return dict(schema_version=1, kind="manufacturing_explicit_pair_synthetic_acceptance",
        families=FAMILIES, modes=MODES, seeds=SEEDS, fixture_count=24,
        original_samples_per_band=5, angles_deg=(0., 15.), polarization="unpolarized",
        targets=TARGETS, shapes=SHAPES, reference_wavelength_nm=550.,
        perturbation="numpy.default_rng(seed).uniform(0.9, 1.1), nondefect layers in substrate-outward order",
        defect="explicit first interior contrasting layer, index 1, thickness 2 nm",
        bpf_cavity="index 4, two quarter waves before multiplicative perturbation",
        reference_nm=8., native_minimum_nm=1., upper_nm=300., max_layers=None, max_total_nm=None,
        source_seconds=SOURCE_SECONDS, route_seconds=ROUTE_SECONDS, whole_cell_seconds=CELL_SECONDS,
        refinement_calls_per_route=REFINEMENT_CALLS, start_calls_per_route=1, audits_per_route=1,
        max_iterations_per_route=MAX_ITERATIONS, validation_samples_multiplier=2,
        memory_cap_bytes=MEMORY_CAP, extrema_maxiter=EXTREMUM_MAXITER, extrema_xatol=EXTREMUM_XATOL,
        serial_workers=1, math_threads=1,
        thread_policy=dict(required_preimport_environment={key: "1" for key in THREAD_VARIABLES},
            verification="frozen_environment_only_not_independently_measured_blas_threads"),
        requires_nonempty_repair_topologies=True, audit_memory_model="128 MiB + dense scratch + five 2 KiB worst-case record owners",
        notes=["Real production TMM, synthetic known n/k only; not measured material claims",
               "Five-point fixture grids are explicit new inputs, not reduction of any user case",
               "Unmodified existing independent sampled/extremum audit, not continuous accuracy proof",
               "Separate actual F/FJ kernel, wavelength and layer-point ledgers; candidate calls are not kernels",
               "Unequal physical work is expected after topology repair; no equal-FLOP claim",
               "No post-expiry audit, no threshold tuning and no resource-cap bypass",
               "Source refresh and audit are separately charged before either repair route"])


def source_identity():
    paths = sorted((ROOT / "coat_sim").rglob("*.py"))
    paths += [ROOT / name for name in ("tools/run_film_manufacturing_acceptance.py",
        "tools/run_film_progress_acceptance.py", "tools/run_film_synthesis_acceptance.py",
        "tests/test_film_auto_synthesis.py", "tests/test_film_manufacturing_acceptance.py",
        "docs/film-manufacturing-repair-contract.md", "docs/algorithms.html", "AGENTS.md")]
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, check=True, text=True, capture_output=True).stdout.strip()
    return dict(git_head=git("rev-parse", "HEAD"), sha256=sha256_json(hashes), files=hashes,
                clean=not bool(git("status", "--porcelain", "--untracked-files=all")))


def make_plan():
    cells = [fixture_manifest(make_fixture(family, mode, seed))
             for family in FAMILIES for mode in MODES for seed in SEEDS]
    plan = dict(protocol=protocol(), protocol_sha256=sha256_json(protocol()), cells=cells,
        source=source_identity(), runtime=dict(python=sys.version, platform=platform.platform(),
        numpy=version("numpy"), scipy=version("scipy"),
        thread_environment={key: os.environ.get(key) for key in THREAD_VARIABLES}),
        optical_calls_executed=0)
    return jsonable(dict(**plan, manifest_sha256=sha256_json(plan)))


def validate_thread_environment(environ=None):
    """Require the declared pre-import policy, not a measured thread-count claim.

    Launch Python with these six variables already set. This harness never
    installs a thread controller or claims that BLAS obeyed its environment.
    """
    environ = os.environ if environ is None else environ
    wrong = [key for key in THREAD_VARIABLES if environ.get(key) != "1"]
    if wrong:
        raise ValueError("Execution requires pre-import thread environment = '1': " + ", ".join(wrong))


def validate_execution_plan(plan, approval_digest):
    """Fail closed before optical dispatch if review/source/materials changed."""
    if approval_digest != plan.get("manifest_sha256"):
        raise ValueError("Explicit approval must name this exact frozen manifest SHA-256")
    unsigned = {k: v for k, v in plan.items() if k != "manifest_sha256"}
    if sha256_json(unsigned) != approval_digest:
        raise ValueError("Frozen manifest content changed")
    validate_thread_environment()
    current = make_plan()
    if not current["source"]["clean"] or not plan["source"]["clean"]:
        raise ValueError("Execution requires an entirely clean frozen source tree")
    for key in ("source", "protocol_sha256", "cells", "runtime"):
        if current[key] != plan[key]:
            raise ValueError(f"Frozen {key} changed; review and freeze a new manifest")
    return current


class LiveCancelEvent:
    """Existing event API, but every is_set call runs the live route/global check."""
    def __init__(self, check):
        self.check = check

    def is_set(self):
        self.check()
        return False


class CellLedger:
    """Only actual low-level kernel invocations accrue F/FJ work.

    Whole candidate/audit work is reserved before entry. Each kernel must fit
    that reservation before starting. Failed kernels are attempted/incomplete,
    never invented completed work. Source/A/B and start/refinement/audit remain
    separate; aggregate candidate counters are reported by the operator itself.
    """
    def __init__(self, fixture, check):
        self.fixture = fixture
        self.check = check
        self.caps = _phase_caps(fixture)
        self.actual = {phase: {k: Work() for k in ("F", "FJ")} for phase in self.caps}
        self.attempted = {phase: {k: 0 for k in ("F", "FJ")} for phase in self.caps}
        self.incomplete = {phase: {k: 0 for k in ("F", "FJ")} for phase in self.caps}
        self.admissions = []
        self.reservation = None
        self.peak_bytes = 0

    def admit(self, *, phase, route, design, jacobian, estimated_bytes):
        from coat_sim.core.film_synthesis_manufacturing import ManufacturingRepairStop
        self.check()
        if self.reservation is not None:
            raise RuntimeError("Previous whole-work reservation has not closed")
        name, kind = f"{route}.{phase}", "FJ" if jacobian else "F"
        layers = len(design["front_layers"])
        work = (audit_upper_work(jsonable(self.fixture.request.objectives), self.fixture.request.validation_samples_multiplier, layers) if phase == "audit" else
                grid_work(jsonable(self.fixture.request.objectives), layers))
        self.peak_bytes = max(self.peak_bytes, estimated_bytes)
        admitted = estimated_bytes <= MEMORY_CAP and (self.actual[name][kind] + work).fits(self.caps[name][kind])
        self.admissions.append(dict(phase=name, kind=kind, planned=jsonable(work),
                                   estimated_bytes=estimated_bytes, admitted=admitted))
        if not admitted:
            raise ManufacturingRepairStop("resource_guard" if estimated_bytes > MEMORY_CAP else "shared_budget")
        self.reservation = dict(phase=name, kind=kind, work=work, used=Work(), audit=phase == "audit")

    @contextmanager
    def dispatch(self):
        if self.reservation is None:
            raise RuntimeError("Physical entry requires whole-work admission")
        reservation = self.reservation
        try:
            yield
            if not reservation["audit"] and reservation["used"] != reservation["work"]:
                raise RuntimeError("Complete FJ did not execute its exact full-grid kernel schedule")
        finally:
            self.reservation = None

    @contextmanager
    def instrument(self):
        from coat_sim.core import film_optimization_structure as structure
        original = structure.simulate_with_thickness_jacobian
        def kernel(design, payload, variables, settings, **kwargs):
            self.check()
            r = self.reservation
            if r is None:
                raise RuntimeError("Unreserved optical kernel")
            kind = "FJ" if variables else "F"
            points, layers = settings["samples"], len(design["front_layers"]) + len(design["back_layers"])
            work = Work(1, points, points * max(1, layers))
            if kind != r["kind"] or not (r["used"] + work).fits(r["work"]):
                raise RuntimeError("Actual optical kernel exceeds whole-work reservation")
            self.attempted[r["phase"]][kind] += 1
            try:
                value = original(design, payload, variables, settings, **kwargs)
                if len(value["wavelength_nm"]) != points:
                    raise RuntimeError("Kernel wavelength count differs from reserved settings")
            except BaseException:
                self.incomplete[r["phase"]][kind] += 1
                raise
            self.actual[r["phase"]][kind] += work
            r["used"] += work
            return value
        with patch.object(structure, "simulate_with_thickness_jacobian", kernel):
            yield

    def report(self):
        return dict(caps=jsonable(self.caps), completed=jsonable(self.actual),
            attempted_kernel_calls=self.attempted, incomplete_kernel_calls=self.incomplete,
            admissions=self.admissions, estimated_peak_bytes=self.peak_bytes,
            caps_respected=all(self.actual[p][k].fits(self.caps[p][k]) for p in self.caps for k in ("F", "FJ")),
            scope="Actual serial simulate_with_thickness_jacobian calls; F means zero derivative columns")


def snapshot_report(request, snapshot):
    from coat_sim.core.film_synthesis_audit import objective_constraint_status
    if snapshot is None:
        return None
    def evidence(score):
        if score is None:
            return None
        return dict(score=jsonable(score), metrics=score_metrics(score),
            band_status=objective_constraint_status(request.objectives, score["errors"], request.acceptable_normalized_error))
    return dict(design=jsonable(snapshot.design), physical_hash=snapshot.physical_hash,
                training=evidence(snapshot.training_score), audit=evidence(snapshot.audit_score))


def run_cell(fixture, *, clock=time.monotonic, external_retained_bytes=lambda: 0):
    """Real optics entry; call only after validate_execution_plan succeeds."""
    from coat_sim.core import film_optimization_structure as structure
    from coat_sim.core.film_auto_synthesis import _proxy
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    from coat_sim.core import film_synthesis_manufacturing as operator
    from coat_sim.core.film_optimization_structure import _StructureCancelled

    started = clock()
    source_deadline, cell_deadline = started + SOURCE_SECONDS, started + CELL_SECONDS
    def check():
        if clock() >= cell_deadline:
            raise operator.ManufacturingRepairStop("time_limit")
    def source_check():
        check()
        if clock() >= source_deadline:
            raise operator.ManufacturingRepairStop("time_limit")
    ledger = CellLedger(fixture, check)
    request = fixture.request
    proxy = _proxy(request)
    # These are the only evidence owners retained by the caller. The operator
    # owns its own source/route snapshots and accounts for those independently.
    retained: dict[str, Any] = {"training_value": None, "audit_value": None, "source": None}
    operator_phase = False
    def external_bytes():
        outer = external_retained_bytes()
        if type(outer) is not int or outer < 0:
            raise ValueError("external_retained_bytes must be a nonnegative integer")
        owned = operator._owned_bytes(fixture, proxy, vars(ledger), retained)
        if operator_phase:
            # Both exact objects are certainly in the union above. The operator
            # counts them through its original request/source, including shared
            # nested identities, so only the set difference is external here.
            owned -= operator._owned_bytes(request, source)
        return owned + outer
    def evaluate(design, *, jacobian, cancel_check):
        with ledger.dispatch():
            return structure.evaluate_frozen_structure_candidate(proxy, design,
                variable_ids=tuple(row["id"] for row in design["front_layers"]) if jacobian else (),
                with_jacobian=jacobian, cancel_event=LiveCancelEvent(cancel_check))
    def audit(design, *, cancel_check):
        with ledger.dispatch():
            return audit_frozen_synthesis_candidate(request, design, cancel_event=LiveCancelEvent(cancel_check))
    source = result = None
    source_elapsed = None
    source_status = "not_started"
    source_calls = dict(start=0, audit=0, completed_start=0, completed_audit=0)
    error = None
    with ledger.instrument():
        try:
            source_check()
            ledger.admit(phase="start", route="source", design=fixture.design, jacobian=True,
                estimated_bytes=operator.manufacturing_refinement_working_set_bytes(request, len(fixture.design["front_layers"])) + external_bytes())
            source_calls["start"] = 1
            retained["training_value"] = evaluate(fixture.design, jacobian=True, cancel_check=source_check)
            source_calls["completed_start"] = 1
            source = operator.manufacturing_snapshot(request, retained["training_value"])
            retained.update(training_value=None, source=source)
            source_status = "training_complete"
            source_check()
            ledger.admit(phase="audit", route="source", design=fixture.design, jacobian=False,
                estimated_bytes=audit_memory_model(request, fixture.design)["total_bytes"] + external_bytes())
            source_calls["audit"] = 1
            retained["audit_value"] = audit(fixture.design, cancel_check=source_check)
            source_calls["completed_audit"] = 1
            source = operator.manufacturing_snapshot(request, {"design": source.design, "score": source.training_score},
                                                     audit_value=retained["audit_value"])
            retained.update(audit_value=None, source=source)
            source_status = "audited"
            source_check()
            source_elapsed = clock() - started
            operator_phase = True
            result = operator.repair_manufacturing_pair(repair_request(fixture), source,
                evaluate=evaluate, audit=audit, admit=ledger.admit,
                audit_working_set_bytes=lambda design: audit_memory_model(request, design)["total_bytes"],
                external_retained_bytes=external_bytes, cancel_check=check, clock=clock)
        except operator.ManufacturingRepairStop as stop:
            source_status += ":" + stop.reason
        except _StructureCancelled:
            source_status += ":cancelled"
        except Exception as exc:
            error = dict(type=type(exc).__name__, message=str(exc))
    if source_elapsed is None:
        source_elapsed = clock() - started
    routes = []
    if result is not None:
        for r in result.routes:
            routes.append(dict(route=r.route, status=r.status, lower_bounds_nm=r.lower_bounds_nm,
                removed_ids=r.removed_ids, merged_ids=r.merged_ids,
                calls={name: getattr(r, name) for name in ("start_calls", "refinement_calls", "audit_calls",
                    "completed_training_calls", "completed_audit_calls")},
                wall_seconds=r.elapsed_seconds, start=snapshot_report(request, r.start),
                champion=snapshot_report(request, r.champion), current=snapshot_report(request, r.current),
                diagnostics=jsonable(r.diagnostics)))
    return dict(cell_id=fixture.cell_id, family=fixture.family, mode=fixture.mode, seed=fixture.seed,
        material_sha256=sha256_json(request.frozen_material_snapshot),
        source_fingerprint=repair_request(fixture).source_fingerprint,
        status="error" if error else (result.status if result is not None else "source_incomplete"),
        source_status=source_status, source_calls=source_calls, source=snapshot_report(request, source),
        source_wall_seconds=source_elapsed,
        routes=routes, best_repaired=snapshot_report(request, None if result is None else result.best_repaired),
        spectral_incumbent=snapshot_report(request, source if result is None else result.spectral_incumbent),
        physical_ledger=ledger.report(), whole_cell_wall_seconds=clock() - started,
        effective_memory_limit_bytes=MEMORY_CAP,
        operator_estimated_peak_bytes=None if result is None else result.estimated_peak_bytes,
        audit_complete=False if result is None else len(result.routes) == 2 and all(r.completed_audit_calls == 1 for r in result.routes),
        error=error)


def _external_output(path):
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Keep results outside the source checkout so execution stays clean")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New result directory outside the checkout")
    parser.add_argument("--execute", action="store_true", help="Explicitly dispatch real optics after source/protocol review")
    parser.add_argument("--plan", type=Path, help="Previously frozen clean-source manifest")
    parser.add_argument("--approve-manifest-sha256", help="Exact parent-approved manifest digest")
    args = parser.parse_args(argv)
    out = _external_output(args.output)
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output directory must be new or empty; never overwrite earlier evidence")
    if not args.execute:
        if args.plan or args.approve_manifest_sha256:
            raise ValueError("Plan approval options require --execute")
        plan = make_plan()
        atomic_json(out / "manifest.json", plan)
        print(json.dumps(dict(mode="dry_run", manifest_sha256=plan["manifest_sha256"],
            source_clean=plan["source"]["clean"], cells=len(plan["cells"]), optical_calls_executed=0)))
        return 0
    if not args.plan or not args.approve_manifest_sha256:
        raise ValueError("--execute requires --plan and --approve-manifest-sha256")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    validate_execution_plan(plan, args.approve_manifest_sha256)
    # Admission checked the frozen pre-import environment; no optional runtime
    # thread-controller dependency or independently measured BLAS-thread claim.
    from coat_sim.core.film_synthesis_manufacturing import _owned_bytes
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out / "manifest.json", plan)
    reports: list[dict[str, Any]] = []
    for frozen in plan["cells"]:
        if source_identity() != plan["source"]:
            raise RuntimeError("Source changed during execution; stop before another cell")
        fixture = make_fixture(frozen["family"], frozen["mode"], frozen["seed"])
        if jsonable(fixture_manifest(fixture)) != frozen:
            raise RuntimeError("Fixture changed after freeze")
        report = run_cell(fixture, external_retained_bytes=lambda: _owned_bytes(plan, reports))
        filename = f"{fixture.cell_id}.json"
        atomic_json(out / filename, report)
        reports.append(dict(cell_id=fixture.cell_id, file=filename, status=report["status"],
            source_status=report["source_status"], audit_complete=report["audit_complete"],
            whole_cell_wall_seconds=report["whole_cell_wall_seconds"],
            material_sha256=report["material_sha256"]))
        # Full scores live in independent per-cell files. Do not silently
        # retain earlier cells outside the next cell's memory admission.
        del report
    final_source = source_identity()
    report = dict(created_utc=utc_now(), manifest_sha256=plan["manifest_sha256"],
        source_unchanged=final_source == plan["source"], cells=reports,
        claims="Synthetic finite-grid/extremum evidence only; no measured or all-band improvement claim")
    atomic_json(out / "report.json", report)
    print(json.dumps(dict(mode="executed", cells=len(reports), source_unchanged=report["source_unchanged"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
