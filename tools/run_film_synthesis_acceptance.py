"""Long-running film synthesis acceptance experiments.

Run with the project venv:
  .venv/Scripts/python.exe tools/run_film_synthesis_acceptance.py --repo-root .

Synthetic controls and source-backed n/k experiments are kept in separate report
sections. A completed finite-grid audit is numerical evidence, not a continuous
spectral certificate or a paper reproduction.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
from typing import Any, Mapping, TypedDict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def sha256_json(value: Any) -> str:
    blob = json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(blob).hexdigest()


class InMemoryMaterials:
    def __init__(self, rows):
        self.rows = {r["id"]: deepcopy(r) for r in rows}

    def _records(self):
        return deepcopy(list(self.rows.values()))

    def get(self, material_id):
        return deepcopy(self.rows[material_id])


class AcceptanceService:
    """Same narrow read-only material contract used by core tests."""
    def __init__(self, rows):
        from coat_sim.core.film_design import validate_design
        from coat_sim.core.film_design_service import FilmDesignService
        self.validate = validate_design
        self._freeze_design = FilmDesignService.freeze_design.__get__(self, type(self))
        self.materials = InMemoryMaterials(rows)

    def freeze_design(self, design, settings=None):
        return self._freeze_design(design, settings)


def synthetic_optical(material_id: str, n: float, *, low_nm=300.0, high_nm=14000.0):
    waves = [low_nm, 450.0, 550.0, 1000.0, 3000.0, 5000.0, 8000.0, 12000.0, high_nm]
    waves = sorted(set(w for w in waves if low_nm <= w <= high_nm))
    return dict(id="nk", name=material_id, kind="tabulated", notes="Synthetic acceptance fixture; not real material data.",
        source="Synthetic lossless oracle explicitly sets k=0; no literature or process claim.", wavelength_unit="nm",
        wavelength_min=low_nm, wavelength_max=high_nm, wavelengths=waves, n=[float(n)] * len(waves), k=[0.0] * len(waves))


def synthetic_service():
    from coat_sim.core.film_design import new_design
    rows = [
        dict(id="glass", name="synthetic substrate", design_ready=True, optical_sets=[synthetic_optical("glass", 1.5)]),
        dict(id="match", name="synthetic quarter-wave match", design_ready=True, optical_sets=[synthetic_optical("match", math.sqrt(1.5))]),
        dict(id="low", name="synthetic low-index", design_ready=True, optical_sets=[synthetic_optical("low", 1.3)]),
        dict(id="high", name="synthetic high-index", design_ready=True, optical_sets=[synthetic_optical("high", 2.2)]),
    ]
    service = AcceptanceService(rows)
    env = new_design(design_id="acceptance-synthetic")
    env["substrate"].update(material_id="glass", optical_set_id="nk")
    return service, env, rows


def real_material_rows():
    from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
    store = RefractiveIndexLocalStore(packaged_store_root())
    requested = [
        ("ZnS", "ZnS Querry", "main/ZnS/nk/Querry.yml"),
        ("GST-a", "Ge2Sb2Te5", "main/Ge2Sb2Te5/nk/Frantz-amorphous.yml"),
        ("GST-c", "Ge2Sb2Te5", "main/Ge2Sb2Te5/nk/Frantz-crystal.yml"),
    ]
    rows = []
    records = {}
    for mid, query, source_path in requested:
        hit = next((r for r in store.search(query, limit=100)["entries"] if r["source_path"] == source_path), None)
        if hit is None:
            raise RuntimeError(f"packaged RII entry missing: {source_path}")
        imported = store.convert_entry(hit["entry_id"], optical_set_id="nk", name=mid)
        optical = imported["optical_set"]
        optical = dict(optical)
        optical["id"] = "nk"
        row = dict(id=mid, name=mid, design_ready=True, optical_sets=[optical])
        rows.append(row)
        records[mid] = {
            "entry_id": hit["entry_id"], "source_path": source_path,
            "source_sha256": hit.get("source_sha256"), "optical_record": optical,
            "import_provenance": imported.get("provenance"),
            "conversion_warnings": imported.get("warnings", []),
            "production_validation": imported.get("production_validation"),
        }
    return rows, records


def make_request(environment, objectives, refs, *, cycles=None, evaluations=None,
                 max_layers=None, growth=(0.5, 1.0, 2.0), stagnation=5,
                 acceptable=1e-3, seed_designs=()):
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    return FilmSynthesisRequest(environment=environment, objectives=tuple(objectives),
        candidate_materials=tuple(FilmNeedleMaterialReference(m, "nk") for m in refs),
        max_cycles=cycles, max_evaluations=evaluations, max_layers=max_layers,
        growth_quarter_wave_fractions=tuple(growth), stagnation_cycles=stagnation,
        acceptable_normalized_error=acceptable, seed_designs=tuple(seed_designs))


def layer_count(design):
    return len((design or {}).get("front_layers", ()))


class AcceptanceProgressState(TypedDict):
    sequence: int
    last_progress: dict[str, Any] | None
    last_emit: float


def run_case(name, label, service, request, output_dir, cancel_event, heartbeat_state, events_file, repo_root):
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
    event_lock = threading.Lock()
    state: AcceptanceProgressState = {"sequence": 0, "last_progress": None, "last_emit": time.monotonic()}

    def log_event(kind, payload):
        with event_lock:
            state["sequence"] += 1
            record = {"sequence": state["sequence"], "kind": kind, "utc": utc_now(), "case": name, **jsonable(payload)}
            with events_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
            # A wall-clock heartbeat is not new activity from the core.
            if kind != "heartbeat":
                state["last_emit"] = time.monotonic()

    frozen = freeze_film_synthesis_request(service, request)
    atomic_json(output_dir / f"{name}.request.json", {
        "case": name, "data_class": label, "started_utc": utc_now(),
        "request_sha256": sha256_json(frozen), "request": frozen,
    })
    log_event("progress", {"stage": "request_frozen", "evaluations": 0,
        "layer_count": 0, "best_max_normalized_error": None})

    def progress(p):
        state["last_progress"] = jsonable(p)
        log_event("progress", state["last_progress"])

    def snapshot(design, training_score, validation_score, audit):
        snap = {"case": name, "utc": utc_now(), "design": design,
            "training_score": training_score, "validation_score": validation_score, "audit": audit}
        atomic_json(output_dir / f"{name}.best-audited.json", snap)
        log_event("checkpoint", {"layer_count": layer_count(design),
            "best_max_normalized_error": validation_score.get("max_error"),
            "checkpoint": f"{name}.best-audited.json"})

    heartbeat_stop = threading.Event()
    def heartbeat_loop():
        while not heartbeat_stop.wait(5.0):
            p = state.get("last_progress") or {}
            log_event("heartbeat", {"stage": p.get("stage", "waiting_for_core"),
                "evaluations": p.get("evaluations"), "cycle": p.get("cycle"),
                "layer_count": p.get("layer_count"),
                "best_max_normalized_error": p.get("best_max_normalized_error"),
                "core_progress_age_s": round(time.monotonic() - state["last_emit"], 3)})

    heartbeat = threading.Thread(target=heartbeat_loop, name=f"heartbeat-{name}", daemon=True)
    heartbeat.start()
    started = time.monotonic()
    started_utc = utc_now()
    try:
        result = synthesize_film_design(service, frozen, cancel_event=cancel_event,
                                        progress_callback=progress, snapshot_callback=snapshot)
        source_physical_check = None
        if label.startswith("source_backed") and result.candidate_design is not None:
            spectra = []
            for lo, hi in ((3000., 5000.), (8000., 12000.)):
                samples = 161
                frozen_physics = service.freeze_design(result.candidate_design, dict(
                    wavelength_min_nm=lo, wavelength_max_nm=hi, samples=samples))
                physical = __import__("coat_sim.core.film_design_service", fromlist=["simulate_frozen"]).simulate_frozen(
                    str(repo_root), frozen_physics)
                spectra.append({"wavelength_min_nm": lo, "wavelength_max_nm": hi,
                    "samples": samples, "wavelengths_nm": [lo + i*(hi-lo)/(samples-1) for i in range(samples)],
                    "R": physical["R"], "T": physical["T"], "A": physical["A"]})
            source_physical_check = {"kind": "independent_frozen_TMM_RTA",
                "data_class": label, "spectra": spectra,
                "metrics_note": "R/T/A are all retained; low R alone is not interpreted as high T."}
            atomic_json(output_dir / f"{name}.physical-RTA.json", source_physical_check)
        result_json = {
            "schema_version": 1, "case": name, "data_class": label,
            "started_utc": started_utc, "finished_utc": utc_now(),
            "elapsed_seconds": time.monotonic() - started,
            "request_sha256": sha256_json(frozen), "request": frozen, "result": result,
            "source_physical_check": source_physical_check,
            "summary": {"state": result.state, "termination_reason": result.termination_reason,
                "targets_met": result.targets_met, "layers": layer_count(result.candidate_design),
                "cycles": result.cycles, "evaluations": result.evaluations,
                "validation_evaluations": result.validation_evaluations,
                "failed_evaluations": result.failed_evaluations,
                "audit_physical_evaluations": result.audit_physical_evaluations,
                "audit_wavelength_points": result.audit_wavelength_points,
                "best_training_max_error": (result.training_score or {}).get("max_error"),
                "best_validation_max_error": (result.validation_score or {}).get("max_error")},
        }
        atomic_json(output_dir / f"{name}.result.json", result_json)
        log_event("terminal", result_json["summary"])
        return result_json
    except Exception as exc:
        failure = {"case": name, "data_class": label, "state": "failed_exception",
            "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
            "request_sha256": sha256_json(frozen), "request": frozen}
        atomic_json(output_dir / f"{name}.result.json", failure)
        log_event("exception", {"error": failure["error"]})
        return failure
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="project root containing coat_sim/")
    parser.add_argument("--out", type=Path, default=None, help="report directory (default: repo/artifacts/film_synthesis_acceptance_<UTC>)")
    parser.add_argument("--scenario", choices=("all", "controls", "dual-ar", "dual-passband-control", "dual-passbands", "six-line", "real"), default="all")
    parser.add_argument("--dual-max-cycles", type=int, default=None, help="optional explicit caller stop; omitted means core stagnation stop")
    parser.add_argument("--dual-max-evaluations", type=int, default=None, help="optional explicit caller stop; omitted means unrestricted")
    parser.add_argument("--samples", type=int, default=61)
    parser.add_argument("--cancel-file", type=Path, default=None,
                        help="create this file to request cooperative cancellation (default: OUT/CANCEL)")
    parser.add_argument("--dual-ar-seed", type=Path, default=None,
                        help="seed the dual-window continuation from a prior best-audited checkpoint JSON")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    if not (repo_root / "coat_sim" / "core" / "film_auto_synthesis.py").is_file():
        parser.error(f"not a coating-sim repository root: {repo_root}")
    sys.path.insert(0, str(repo_root))
    out = args.out or repo_root / "artifacts" / ("film_synthesis_acceptance_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    events_file = out / "events.jsonl"
    cancel_event = threading.Event()
    cancel_path = args.cancel_file.resolve() if args.cancel_file else out / "CANCEL"
    stop_state = {"stop": False}
    cancel_watch_stop = threading.Event()

    def watch_cancel_file():
        while not cancel_watch_stop.wait(1.0):
            if cancel_path.exists():
                cancel_event.set()
                return

    cancel_watcher = threading.Thread(target=watch_cancel_file, name="acceptance-cancel-watch", daemon=True)
    cancel_watcher.start()
    try:
        signal.signal(signal.SIGINT, lambda *_: cancel_event.set())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: cancel_event.set())
    except ValueError:
        pass

    from coat_sim.core.film_design import new_design
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    from coat_sim.core.film_physical_structures import (
        FilmBraggStackSection, FilmStructureMaterialBinding, FilmStructureSpec,
        FilmOpticalLayerSpec, FilmCoupledCavitySection, generate_physical_structure,
        dual_window_ar_objectives, dual_passband_objectives, six_line_ar_objectives,
    )
    from coat_sim.core.film_design_service import simulate_frozen

    cases = []
    service, env, syn_rows = synthetic_service()
    single = make_request(env, (FilmOptimizationObjective(550, 550, 1, metric="R", mode="range", upper_bound=.001),),
                          ("match",), cycles=4, evaluations=300, max_layers=4, stagnation=3)
    n0 = make_request(env, (FilmOptimizationObjective(550, 550, 1, metric="R", mode="range", upper_bound=.05),),
                      ("match",), cycles=4, evaluations=100, max_layers=4, stagnation=3)
    refl_req = make_request(env, (FilmOptimizationObjective(550, 550, 1, metric="R", mode="range", lower_bound=.9),),
                            ("low", "high"), cycles=2, evaluations=100, max_layers=32, stagnation=3)
    refl_frozen = __import__("coat_sim.core.film_auto_synthesis", fromlist=["freeze_film_synthesis_request"]).freeze_film_synthesis_request(service, refl_req)
    spec = FilmStructureSpec("multistack", (FilmStructureMaterialBinding("H", "high", "nk"),
        FilmStructureMaterialBinding("L", "low", "nk")),
        (FilmBraggStackSection(550, pairs=8, high_role="H", low_role="L", first_role="L"),),
        name="known synthetic DBR seed", validation_wavelengths_nm=(550,))
    bragg_seed = generate_physical_structure(env, spec, refl_frozen.frozen_material_snapshot).candidate_design
    refl_req = replace(refl_req, seed_designs=(bragg_seed,))
    if args.scenario in ("all", "controls"):
        cases.extend([
            ("synthetic_n0_control", "synthetic_explicit_k0_control", service, n0),
            ("synthetic_single_layer_ar", "synthetic_explicit_k0_known_solution", service, single),
            ("synthetic_bragg_reflector_seed", "synthetic_explicit_k0_known_solution", service, refl_req),
        ])
    if args.scenario in ("all", "dual-ar"):
        seed_designs: tuple[Mapping[str, Any], ...] = ()
        if args.dual_ar_seed is not None:
            seed_payload = json.loads(args.dual_ar_seed.read_text(encoding="utf-8"))
            seed_design = seed_payload.get("design") or seed_payload.get("result", {}).get("candidate_design")
            if not isinstance(seed_design, dict):
                parser.error("--dual-ar-seed must point to a checkpoint containing design")
            seed_designs = (seed_design,)
        req = make_request(env, dual_window_ar_objectives(max_reflectance=.01, samples_per_window=args.samples),
            ("low", "high"), cycles=args.dual_max_cycles, evaluations=args.dual_max_evaluations,
            max_layers=64, stagnation=6, seed_designs=seed_designs)
        cases.append(("synthetic_dual_window_ar_3_5_8_12um",
            "synthetic_explicit_k0_research_inspired_only" + ("_seeded_continuation" if seed_designs else ""), service, req))
    if args.scenario in ("all", "dual-passband-control"):
        from coat_sim.core.film_optimization import FilmOptimizationObjective
        control_rows = [
            dict(id="substrate", name="synthetic ns=1", design_ready=True, optical_sets=[synthetic_optical("substrate", 1.0)]),
            dict(id="low", name="synthetic n=1.45", design_ready=True, optical_sets=[synthetic_optical("low", 1.45)]),
            dict(id="high", name="synthetic n=2.2", design_ready=True, optical_sets=[synthetic_optical("high", 2.2)]),
        ]
        control_service = AcceptanceService(control_rows)
        control_env = new_design(design_id="known-coupled-dual-passband-control")
        control_env["substrate"].update(material_id="substrate", optical_set_id="nk")
        control_env["use_k"] = True
        mirrors = FilmCoupledCavitySection(
            substrate_mirror=FilmBraggStackSection(5000., pairs=4, high_role="H", low_role="L", first_role="L"),
            cavities=(FilmOpticalLayerSpec("H", 5000., optical_thickness_waves=.5),
                      FilmOpticalLayerSpec("H", 5000., optical_thickness_waves=.5)),
            coupling_mirrors=(FilmBraggStackSection(5000., pairs=1, high_role="H", low_role="L", first_role="H", terminal_role="H"),),
            incident_mirror=FilmBraggStackSection(5000., pairs=4, high_role="H", low_role="L", first_role="H"))
        control_spec = FilmStructureSpec("coupled_cavity",
            (FilmStructureMaterialBinding("H", "high", "nk"), FilmStructureMaterialBinding("L", "low", "nk")),
            (mirrors,), name="known coupled dual-passband synthetic control",
            validation_wavelengths_nm=(4400., 4500., 4709., 4712., 4950., 5000., 5050., 5326., 5329., 5600., 5700.))
        control_seed = generate_physical_structure(control_env, control_spec,
            __import__("coat_sim.core.film_auto_synthesis", fromlist=["freeze_film_synthesis_request"]).freeze_film_synthesis_request(
                control_service, make_request(control_env, (FilmOptimizationObjective(5000., 5000., 1, metric="T", mode="range", lower_bound=0.),),
                    ("low", "high"), cycles=1, evaluations=20, max_layers=32)).frozen_material_snapshot)
        control_objectives = (
            FilmOptimizationObjective(4709., 4712., 4, metric="T", mode="range", lower_bound=.99),
            FilmOptimizationObjective(5326., 5329., 4, metric="T", mode="range", lower_bound=.99),
            FilmOptimizationObjective(4950., 5050., 11, metric="T", mode="range", upper_bound=.06),
            FilmOptimizationObjective(4400., 4500., 11, metric="T", mode="range", upper_bound=.06),
            FilmOptimizationObjective(5600., 5700., 11, metric="T", mode="range", upper_bound=.06),
        )
        control_req = make_request(control_env, control_objectives, ("low", "high"),
            cycles=1, evaluations=100, max_layers=32, stagnation=1, seed_designs=(control_seed.candidate_design,))
        atomic_json(out / "dual_passband_control_structure_seed.json", {"spec": control_spec,
            "metadata": control_seed.metadata, "candidate_design": control_seed.candidate_design,
            "interpretation": "Known synthetic 21-layer coupled-cavity dual-resonance positive control. It exercises seed generation, frozen request, core search, and independent audit; does not solve the broad IR dual-window objective."})
        cases.append(("synthetic_known_coupled_dual_passband_seed_control", "synthetic_n1_1p45_2p2_seeded_known_solution_not_search_discovery", control_service, control_req))
    if args.scenario in ("all", "dual-passbands"):
        req = make_request(env, dual_passband_objectives(min_transmittance=.80, max_stop_transmittance=.15,
            stopbands_nm=((2200., 2800.), (5200., 7800.), (12200., 14000.)), samples_per_band=args.samples),
            ("low", "high"), cycles=args.dual_max_cycles, evaluations=args.dual_max_evaluations,
            max_layers=64, stagnation=6)
        cases.append(("synthetic_dual_passbands_with_explicit_stopbands", "synthetic_explicit_k0_project_thresholds", service, req))
    if args.scenario in ("all", "six-line"):
        req = make_request(env, six_line_ar_objectives(max_reflectance=.02),
            ("low", "high"), cycles=args.dual_max_cycles, evaluations=args.dual_max_evaluations,
            max_layers=48, stagnation=6)
        cases.append(("synthetic_six_discrete_lines", "synthetic_explicit_k0_project_thresholds_not_2019_reproduction", service, req))
    if args.scenario in ("all", "real"):
        real_rows, provenance = real_material_rows()
        real_service = AcceptanceService(real_rows)
        real_env = new_design(design_id="acceptance-real-nk-not-paper-reproduction")
        real_env["substrate"].update(material_id="ZnS", optical_set_id="nk")
        real_env["use_k"] = True
        waves = (3000., 4000., 5000., 8000., 10000., 12000.)
        base_targets = []
        baseline = []
        for wave in waves:
            frozen = real_service.freeze_design(real_env, dict(wavelength_min_nm=wave, wavelength_max_nm=wave, samples=1))
            response = simulate_frozen(str(repo_root), frozen)
            value = float(response["R"][0])
            baseline.append({"wavelength_nm": wave, "bare_substrate_R": value})
            base_targets.append(FilmOptimizationObjective(wave, wave, 1, metric="R", mode="range",
                upper_bound=max(0.0, value - .005)))
        real_req = make_request(real_env, base_targets, ("ZnS", "GST-a", "GST-c"),
            cycles=args.dual_max_cycles, evaluations=args.dual_max_evaluations,
            max_layers=48, stagnation=6)
        real_frozen = __import__("coat_sim.core.film_auto_synthesis", fromlist=["freeze_film_synthesis_request"]).freeze_film_synthesis_request(real_service, real_req)
        real_seed_spec = FilmStructureSpec("multistack",
            (FilmStructureMaterialBinding("H", "GST-a", "nk"), FilmStructureMaterialBinding("L", "ZnS", "nk")),
            (FilmBraggStackSection(4000., pairs=2, high_role="H", low_role="L", first_role="L"),
             FilmBraggStackSection(10000., pairs=2, high_role="H", low_role="L", first_role="L")),
            name="source-backed GST-a/ZnS two-window multistack seed (not literature reproduction)",
            validation_wavelengths_nm=waves)
        real_seed = generate_physical_structure(real_env, real_seed_spec, real_frozen.frozen_material_snapshot)
        real_req = replace(real_req, seed_designs=(real_seed.candidate_design,))
        atomic_json(out / "real_nk_structure_seed.json", {"spec": real_seed_spec, "metadata": real_seed.metadata,
            "candidate_design": real_seed.candidate_design,
            "interpretation": "Explicit GST-a/ZnS 2-pair sections centered at 4 and 10 um; valid physical seed supplied to search, not an N=0 discovery or paper reproduction."})
        cases.append(("real_nk_six_point_baseline_minus_0p005_seeded", "source_backed_RII_nk_non_process_matched_not_paper_reproduction_explicit_multistack_seed", real_service, real_req))
        atomic_json(out / "real_nk_provenance.json", {"snapshot": provenance, "bare_baseline": baseline,
            "target_rule": "At each declared discrete wavelength require R <= bare-ZnS-substrate R - 0.005 absolute; project acceptance rule, not a paper threshold.",
            "caveat": "ZnS Querry is not CVD substrate/process-matched data; GST Frantz is not a matched phase/device stack. Real sourced n/k numerical model, not a paper reproduction."})

    implementation_paths = ["coat_sim/core/film_auto_synthesis.py",
        "coat_sim/core/film_physical_structures.py", "coat_sim/core/film_synthesis_audit.py",
        "tools/run_film_synthesis_acceptance.py"]
    implementation_hashes = {}
    for relative in implementation_paths:
        file_path = repo_root / relative
        if file_path.is_file():
            implementation_hashes[relative] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    report = {"schema_version": 1, "started_utc": utc_now(), "repo_root": str(repo_root),
        "runner_file": str(Path(__file__).resolve()), "core_import_root": str(repo_root),
        "implementation_sha256": implementation_hashes,
        "git_head": None, "scenario": args.scenario, "dual_max_cycles": args.dual_max_cycles,
        "dual_max_evaluations": args.dual_max_evaluations,
        "acceptance_policy": {"finite_grid_only": True, "unsearched_gaps_not_pass": True,
            "small_budget_failure_is_not_infeasibility": True,
            "synthetic_and_source_backed_evidence_are_separate": True},
        "results": []}
    try:
        import subprocess
        report["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
    except Exception:
        pass
    atomic_json(out / "run.json", report)
    for name, label, case_service, request in cases:
        if cancel_event.is_set():
            break
        result = run_case(name, label, case_service, request, out, cancel_event, stop_state, events_file, repo_root)
        report["results"].append({"case": name, "data_class": label, "summary": result.get("summary", {k: result.get(k) for k in ("state", "error")})})
        report["updated_utc"] = utc_now()
        atomic_json(out / "run.json", report)
    cancel_watch_stop.set()
    cancel_watcher.join(timeout=1)
    report["finished_utc"] = utc_now()
    report["cancelled"] = cancel_event.is_set()
    report["cancel_file"] = str(cancel_path)
    atomic_json(out / "run.json", report)
    print(json.dumps({"output": str(out), "cases": report["results"], "cancelled": report["cancelled"]}, ensure_ascii=False, indent=2))
    return 130 if report["cancelled"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
