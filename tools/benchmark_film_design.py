"""Bounded local benchmark; uses real ZnS, unchanged 301-point inputs and k setting."""
from __future__ import annotations

import time
_IMPORT_START = time.perf_counter()
import argparse
import cProfile
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import pstats
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coat_sim.core import film_design_service as film_service
from coat_sim.core import optical_constants
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_materials import FilmMaterialLibrary
_IMPORT_SECONDS = time.perf_counter() - _IMPORT_START


def real_zns_design(service, layer_count=1):
    zns = next(row for row in service.material_options(purpose="film")
               if row["id"] == "builtin_zns_literature_20261004")
    optical = next(row for row in zns["optical_sets"] if row["id"] == "ri_zns_amotchkina_n")
    substrate = next(row for row in service.material_options(purpose="substrate")
                     if row["id"] == "film_substrate_schott_nbk7")
    design = service.new_design()
    design["use_k"] = False  # Same explicit setting before/after; this source contains n only.
    design["substrate"].update(material_id=substrate["id"],
                                optical_set_id=substrate["optical_sets"][0]["id"], mode="semi_infinite")
    design["front_layers"] = [{"id": f"qa_zns_{i}", "material_id": zns["id"],
        "optical_set_id": optical["id"], "thickness": {"value": 1.008, "unit": "um"},
        "note": ""} for i in range(layer_count)]
    return service.validate(design)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    summary_path = output / f"{args.phase}_summary.json"
    if summary_path.exists():
        raise FileExistsError(f"Benchmark already recorded: {summary_path}")
    service = FilmDesignService(ROOT)
    started = time.perf_counter()
    design = real_zns_design(service)
    setup_seconds = time.perf_counter() - started
    settings = {"wavelength_min_nm": 400, "wavelength_max_nm": 700,
                "samples": 301, "step_nm": 1, "extrapolation_policy": "endpoint_hold"}
    report = {"sys_executable": sys.executable, "phase": args.phase,
              "module_import_seconds": _IMPORT_SECONDS, "material_option_setup_seconds": setup_seconds,
              "samples": 301, "step_nm": 1, "use_k": design["use_k"],
              "description": "Repeated real ZnS dataset; 1008 nm per layer. Fresh-process first call and immediate repeats; OS caches are not flushed. Stage instrumentation has overhead. Report/assembly is the unaccounted remainder, including validation and source postprocessing.",
              "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [ROOT/'coat_sim/core/film_design_service.py', ROOT/'coat_sim/core/optical_constants.py']},
              "cases": []}
    def persist():
        summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    persist()
    for count in (1, 10, 50, 100):
        case_design = dict(design)
        case_design["front_layers"] = [dict(design["front_layers"][0], id=f"qa_zns_{i}") for i in range(count)]
        case = {"layers": count, "runs": []}; report["cases"].append(case)
        for repeat in range(2):
            timings = {"material_read_seconds": 0.0, "dispersion_seconds": 0.0, "optical_solver_seconds": 0.0}
            calls = {}
            def wrap(original, category, name):
                def measured(*a, **kw):
                    begin = time.perf_counter()
                    try:
                        return original(*a, **kw)
                    finally:
                        timings[category] += time.perf_counter() - begin
                        calls[name] = calls.get(name, 0) + 1
                return measured
            with ExitStack() as stack:
                targets = [(FilmMaterialLibrary, "_records", "material_read_seconds"),
                           (film_service, "coherent_rta", "optical_solver_seconds"),
                           (film_service, "incoherent_plate_rta", "optical_solver_seconds")]
                if hasattr(film_service, "prepare_optical_set"):
                    targets += [(film_service, "prepare_optical_set", "dispersion_seconds"),
                                (optical_constants, "_evaluate_normalized_optical_set", "dispersion_seconds")]
                else:
                    targets += [(film_service, "evaluate_optical_set_with_details", "dispersion_seconds")]
                for namespace, name, category in targets:
                    original = getattr(namespace, name)
                    stack.enter_context(patch.object(namespace, name, wrap(original, category, name)))
                begin = time.perf_counter()
                result = service.simulate(case_design, settings)
                total = time.perf_counter() - begin
            timings["report_and_other_assembly_seconds"] = max(0.0, total - sum(timings.values()))
            run = {"repeat": repeat, "temperature": "first" if count == 1 and repeat == 0 else "warm",
                   "total_seconds": total, **timings, "calls": calls}
            case["runs"].append(run); persist()
            print(json.dumps({"phase": args.phase, "layers": count, **run}, ensure_ascii=False), flush=True)
        (output / f"{args.phase}_{count}_layers.json").write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    profile = cProfile.Profile()
    profile.runcall(service.simulate, design, settings)
    stats = pstats.Stats(profile)
    top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:25]
    report["profile_top_cumulative"] = [{"file": key[0], "line": key[1], "function": key[2],
        "calls": value[1], "self_seconds": value[2], "cumulative_seconds": value[3]} for key, value in top]
    persist()
    print("BENCHMARK_COMPLETE", str(summary_path), flush=True)


if __name__ == "__main__":
    main()
