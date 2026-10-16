"""Small bounded real-optics end-to-end deferred DE comparison."""
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from coat_sim.core.film_batch_de import BatchEvaluation, run_batch_de
from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions
from tests.film_performance_fixtures import fixture

def main():
    output = Path(sys.argv[1]).resolve(); output.mkdir(parents=True, exist_ok=True)
    service, design, frozen = fixture(layers=12)
    settings = dict(wavelength_min_nm=400., wavelength_max_nm=800., samples=1001,
                    angle_deg=37., polarization="unpolarized")
    ids = ["layer-0", "layer-1"]
    x0 = [65., 67.]
    def run(accelerated):
        backend = FrozenFilmEvaluator(frozen, settings, options=PerformanceOptions(backend="cpu"))
        trace = []
        def evaluate(points):
            trace.extend(row.copy() for row in points)
            if accelerated:
                result = backend.evaluate_thickness_batch(ids, points)
            else:
                rows = []
                for point in points:
                    candidate = deepcopy(design)
                    for row, value in zip(candidate["front_layers"], point):
                        row["thickness"] = dict(unit="nm", value=float(value))
                    rows.append(service.simulate(candidate, settings))
                result = {key: np.asarray([row[key] for row in rows]) for key in ("R", "T", "A")}
            outcomes = []
            for i, r in enumerate(result["R"]):
                error = np.abs(r - .05)
                rms = float(np.sqrt(np.mean(error * error)))
                outcomes.append(BatchEvaluation((float(error.max()), rms), rms * rms,
                    {key: result[key][i] for key in ("R", "T", "A")}))
            return outcomes
        begin = time.perf_counter()
        outcome = run_batch_de(evaluate, x0, [25., 25.], [160., 160.], max_iterations=3,
                              max_evaluations=48, population_size=12, max_batch_size=12, seed=19)
        return time.perf_counter() - begin, outcome, np.stack(trace)
    # Include and identify the cold SciPy call separately.
    cold, _, _ = run(False)
    raw, fast = [], []
    for _ in range(3):
        seconds, expected, expected_trace = run(False); raw.append(seconds)
        seconds, actual, actual_trace = run(True); fast.append(seconds)
        np.testing.assert_array_equal(actual_trace, expected_trace)
        np.testing.assert_allclose(actual.best.key, expected.best.key, rtol=2e-12, atol=2e-12)
        np.testing.assert_array_equal(actual.best_thickness, expected.best_thickness)
        assert actual.evaluations == expected.evaluations == 48
    result = {"mode": actual.mode, "seed": 19, "candidates_budget_each": 48,
              "samples": 1001, "layers": 12, "cold_service_seconds_including_scipy": cold,
              "service_warm_seconds": raw, "batch_warm_seconds": fast,
              "warm_speedup": statistics.median(raw) / statistics.median(fast),
              "same_proposal_trace": True, "same_best_thickness": True,
              "best_key": actual.best.key, "gpu_tested": False}
    (output / "deferred-de.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
