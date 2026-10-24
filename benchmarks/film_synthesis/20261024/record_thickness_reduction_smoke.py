from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from importlib import import_module

from tests.test_film_synthesis_thickness import physical_fixture, invoke
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate

real_minimize = import_module("scipy.optimize").minimize

rows = []
for family in ("HR", "AR", "mixed_RT"):
    for mode in ("mean", "pointwise", "both"):
        for capped in (False, True):
            service, request, initial, evaluate, calls = physical_fixture(family, mode)

            def minimize(*args, capped=capped, **kwargs):
                if not capped:
                    kwargs["constraints"] = kwargs["constraints"][:-1]
                return real_minimize(*args, **kwargs)

            start = perf_counter()
            with patch("scipy.optimize.minimize", minimize):
                best, detail = invoke(request, initial, evaluate, max_iterations=24)
            elapsed = perf_counter() - start
            audit = audit_frozen_synthesis_candidate(request, best["design"])
            row = dict(
                family=family,
                criteria=mode,
                initial_cost_sublevel_enabled=capped,
                layer_count_before=len(initial["design"]["front_layers"]),
                layer_count_after=detail["layer_count"],
                initial_total_nm=detail["initial_total_thickness_nm"],
                final_total_nm=detail["final_total_thickness_nm"],
                reduction_nm=detail["reduction_nm"],
                physical_calls=detail["physical_candidates"],
                elapsed_seconds=elapsed,
                training_nominal_pass=best["score"]["nominal_pass"],
                dense_extremum_audit_nominal_pass=audit["score"]["nominal_pass"],
                dense_extremum_audit_max_error=audit["score"]["max_error"],
                audit_physical_evaluations=audit["audit"]["physical_evaluations"],
                audit_wavelength_points=audit["audit"]["wavelength_points"],
                solver_termination=detail["termination"],
                terminal_total_nm=detail["terminal_total_thickness_nm"],
                boundary_nextafter=detail["boundary_nextafter_evaluations"],
                boundary_bisection=detail["boundary_bisection_evaluations"],
            )
            rows.append(row)
            print(json.dumps(row), flush=True)
result = dict(
    recorded_at_utc=datetime.now(timezone.utc).isoformat(),
    base_commit="281ce5f",
    source_sha256=hashlib.sha256(
        Path("coat_sim/core/film_synthesis_thickness.py").read_bytes()
    ).hexdigest(),
    suite="nine fixed topology synthetic-material production-TMM smoke cases; not from-zero search comparison",
    control="same current primitive, only initial-cost sublevel removed from scipy minimize constraints by test wrapper",
    max_iterations=24,
    grid="9 wavelengths per band, angles 0 and 15 degrees, unpolarized",
    audit="existing audit_frozen_synthesis_candidate, default multiplier 4 plus shifted midpoints/extrema; separate from training budget",
    rows=rows,
)
Path("benchmarks/film_synthesis/20261024/thickness-reduction-smoke.json").write_text(
    json.dumps(result, indent=2, allow_nan=False) + "\n"
)
