"""Cold-process scheduler-worker comparison for frozen synthetic film workloads.

This is a bounded host measurement, not a speed guarantee. Each row is a fresh
Python worker process; the OS page cache is intentionally left warm/uncontrolled.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
OUT = ROOT / "artifacts" / "host-hpc"
REPORT = OUT / "worker-benchmark.json"
LOG = OUT / "worker-benchmark.log"
RAW = OUT / "worker-benchmark-runs"

WORKER = r'''import json, os, sys, time
entry=time.monotonic()
parent_start=float(sys.argv[5])
from tests.film_performance_fixtures import fixture
from coat_sim.core import api
class InMemoryCoatService:
    def __init__(self, root): self.film_design_service=fixture(layers=30)[0]
api.CoatService=InMemoryCoatService
from coat_sim.scheduler import worker
import coat_sim.scheduler.io as scheduler_io
events=[]
original=scheduler_io.write_json
def measured_write(path, value, **kwargs):
    name=os.fspath(path).split(os.sep)[-1]
    start=time.perf_counter()
    try: return original(path, value, **kwargs)
    finally:
        if name in ("best-candidate.json", "film-performance.json", "outcome.json"):
            events.append({"file":name,"seconds":time.perf_counter()-start})
scheduler_io.write_json=measured_write
worker.write_json=measured_write
ready=time.monotonic()
started=time.perf_counter()
code=worker.run(sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4])
ended=time.perf_counter()
with open(os.path.join(sys.argv[1],"artifacts/runtime/scheduler/jobs",sys.argv[2],"timing.json"),"w",encoding="utf-8") as stream:
    json.dump({"process_launch_seconds":ready-parent_start,"worker_run_seconds":ended-started,
               "result_persistence_seconds":sum(e["seconds"] for e in events),
               "persistence_events":events},stream,allow_nan=False)
raise SystemExit(code)
'''

CONFIGS = [
    {"name": "serial-cache-off", "mode": "serial", "cache": "0", "workers": 1},
    {"name": "serial-cache-on", "mode": "serial", "cache": "1", "workers": 1},
    {"name": "deferred-cpu1", "mode": "deferred_batch", "cache": "0", "workers": 1},
    {"name": "deferred-cpu4", "mode": "deferred_batch", "cache": "0", "workers": 4},
]


def _request(service, design, frozen, mode):
    from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable
    return FilmOptimizationRequest(
        design_snapshot=deepcopy(design), design_revision=1,
        variables=(FilmThicknessVariable("front", "layer-0", 45., 120.),),
        objectives=(FilmOptimizationObjective(400., 1000., 601, metric="R", target_value=0.),),
        max_iterations=4, max_evaluations=64, initial_step_fraction=.25,
        min_step_fraction=1e-4, acceptable_normalized_error=0.,
        frozen_material_snapshot=deepcopy(frozen), solver="differential_evolution",
        seed=731, multistart_count=2, population_size=16,
        validation_samples_multiplier=2, execution_mode=mode, batch_size=16)


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    from tests.film_performance_fixtures import fixture
    from coat_sim.scheduler.jobs import canonical, freeze_spec
    from coat_sim.scheduler.store import SchedulerStore

    OUT.mkdir(parents=True, exist_ok=True)
    if REPORT.exists() or RAW.exists():
        raise FileExistsError("refusing to overwrite worker-benchmark outputs")
    RAW.mkdir()
    service, design, frozen = fixture(layers=30)
    source_sha = hashlib.sha256((ROOT / "coat_sim/core/film_optimization.py").read_bytes()).hexdigest()
    doc = {"definition": "Real scheduler.worker.run; synthetic frozen in-memory materials only; no live catalog, provider, or GPU.",
           "caveat": "Each run is a separate cold Python process. The OS page cache is not flushed and may be warm; no cache-cold claim is made.",
           "comparison": "Serial immediate differential evolution, cache off/on, versus explicit deferred_batch with cpu_workers 1/4. Deferred and serial trajectories differ; quality values are descriptive, not an equivalence assertion.",
           "source_film_optimization_sha256": source_sha,
           "request": {"layers": 30, "wavelength_samples": 601, "variable_bounds_nm": [45.,120.], "population_size": 16, "max_evaluations": 64, "max_iterations": 4, "validation_samples_multiplier": 2, "seed": 731, "solver": "differential_evolution"},
           "runs_per_configuration": 3, "configurations": []}
    env_base = dict(os.environ)
    env_base["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), str(TESTS), env_base.get("PYTHONPATH", ""))))
    env_base.update(QT_QPA_PLATFORM="offscreen", QT_OPENGL="software", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                    COAT_SIM_FILM_BACKEND="cpu", CUDA_VISIBLE_DEVICES="")
    with LOG.open("w", encoding="utf-8") as log:
        log.write("Synthetic scheduler worker benchmark; all worker invocations are separate Python processes.\n")
        for config in CONFIGS:
            request = _request(service, design, frozen, config["mode"])
            spec = freeze_spec(service, "film_optimization", {"request": request})
            conf = {"name": config["name"], "execution_mode": config["mode"],
                    "forward_cache_env": config["cache"], "requested_cpu_workers": config["workers"],
                    "runs": []}
            doc["configurations"].append(conf)
            for index in range(1, 4):
                root = RAW / config["name"] / f"run-{index}"
                root.mkdir(parents=True)
                job_id, owner = uuid4().hex, uuid4().hex
                directory = root / "artifacts/runtime/scheduler/jobs" / job_id
                directory.mkdir(parents=True)
                SchedulerStore(root).submit(spec, job_id=job_id)
                (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
                (directory / "launch.json").write_text(json.dumps({"instance": owner}), encoding="utf-8")
                env = dict(env_base, COAT_SIM_FILM_FORWARD_CACHE=config["cache"],
                           COAT_SIM_FILM_CPU_WORKERS=str(config["workers"]))
                if config["mode"] == "deferred_batch": env["COAT_SIM_FILM_BATCH"] = "1"
                else: env.pop("COAT_SIM_FILM_BATCH", None)
                parent_start = time.monotonic()
                began = time.perf_counter()
                completed = subprocess.run(
                    [sys.executable, "-c", WORKER, str(root), job_id, str(os.getpid()), owner,
                     str(parent_start)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
                total = time.perf_counter() - began
                (root / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
                (root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
                if completed.returncode:
                    raise RuntimeError(f"{config['name']} run {index} failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}")
                timing = _json(directory / "timing.json")
                outcome = _json(directory / "outcome.json")
                if outcome.get("state") != "succeeded" or outcome.get("result", {}).get("state") != "completed":
                    raise RuntimeError(f"worker returned non-completed outcome: {outcome.get('error')}")
                result = outcome["result"]
                candidate = result.get("candidate", {})
                quality = candidate.get("quality", {})
                sidecar = directory / "film-performance.json"
                performance = _json(sidecar) if sidecar.exists() else None
                checkpoint = directory / "best-candidate.json"
                checkpoint_count = sum(e["file"] == "best-candidate.json" for e in timing["persistence_events"])
                sizes = {}
                for name in ("spec.json", "outcome.json", "best-candidate.json", "film-performance.json", "progress.json", "launch.json"):
                    path = directory / name
                    if path.exists(): sizes[name] = path.stat().st_size
                # Keep byte-exact original worker artifacts beside the report.
                raw_out = root / "worker-artifacts"
                raw_out.mkdir()
                for name in sizes:
                    shutil.copyfile(directory / name, raw_out / name)
                record = {"run": index, **timing, "total_elapsed_seconds": total,
                          "worker_state": outcome["state"], "optimization_state": result.get("state"),
                          "evaluations": candidate.get("evaluations"), "iterations": candidate.get("iterations"),
                          "checkpoint_count": checkpoint_count, "quality": quality,
                          "actual_backend": performance.get("actual_backend") if performance else None,
                          "actual_backend_source": "film-performance.json" if performance else "canonical serial path emitted no performance sidecar",
                          "actual_cpu_workers": performance.get("cpu_workers") if performance else None,
                          "performance_metadata": performance,
                          "artifact_sizes_bytes": sizes, "artifact_total_bytes": sum(sizes.values()),
                          "raw_artifacts_directory": str(raw_out.relative_to(OUT))}
                conf["runs"].append(record)
                REPORT.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                line = json.dumps({"configuration": config["name"], "run": index,
                                   "total_elapsed_seconds": total,
                                   "worker_run_seconds": timing["worker_run_seconds"],
                                   "result_persistence_seconds": timing["result_persistence_seconds"],
                                   "evaluations": record["evaluations"], "actual_backend": record["actual_backend"]}, ensure_ascii=False)
                print(line, flush=True); log.write(line + "\n"); log.flush()
                # Remove only transient SchedulerStore data; preserved raw files remain.
                shutil.rmtree(root / "artifacts")
                shutil.rmtree(root / "data", ignore_errors=True)
        REPORT.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"WORKER_BENCHMARK_COMPLETE {REPORT}", flush=True)


if __name__ == "__main__":
    main()
