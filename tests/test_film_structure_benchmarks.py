# Reconstructed regression suite from historical f12 evidence and current contracts.
# This is not a byte-for-byte recovery of the missing target test.
from __future__ import annotations

import builtins
from dataclasses import replace
import faulthandler
import math
import sys
from types import SimpleNamespace
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
TOOLS = WORKSPACE / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import benchmark_film_structure as benchmark


def test_runtime_import_paths_avoid_cp314_wheels_on_python312(monkeypatch):
    import_attempts = []

    def track_import(name, *args, **kwargs):
        if name == "numpy":
            import_attempts.append(name)
            raise ModuleNotFoundError("numpy is not importable", name="numpy")
        return builtins.__import__(name, *args, **kwargs)

    runtime_path = []
    monkeypatch.setattr(benchmark.sys, "path", runtime_path)
    monkeypatch.setattr(benchmark.sys, "version_info", (3, 12, 14))
    monkeypatch.setattr(builtins, "__import__", track_import)

    benchmark._runtime_import_paths()

    bundled = str(WORKSPACE / "build" / "film_solver_deps_20261011")
    assert import_attempts == []
    assert str(WORKSPACE) in runtime_path
    assert bundled not in runtime_path


def test_runtime_import_paths_keep_cp314_bundle_out_when_numpy_imports(monkeypatch):
    original_import = builtins.__import__

    def import_numpy(name, *args, **kwargs):
        if name == "numpy":
            return object()
        return original_import(name, *args, **kwargs)

    runtime_path = []
    monkeypatch.setattr(benchmark.sys, "path", runtime_path)
    monkeypatch.setattr(benchmark.sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(builtins, "__import__", import_numpy)

    benchmark._runtime_import_paths()

    bundled = str(WORKSPACE / "build" / "film_solver_deps_20261011")
    assert bundled not in runtime_path


def test_runtime_import_paths_add_cp314_bundle_only_when_numpy_fails(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def fail_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ModuleNotFoundError("numpy is not importable", name="numpy")
        return original_import(name, *args, **kwargs)

    # The optional wheel bundle is intentionally absent in a source checkout.
    # Build a directory fixture to verify the conditional fallback itself.
    bundled = tmp_path / "build" / "film_solver_deps_20261011"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path)
    runtime_path = []
    monkeypatch.setattr(benchmark.sys, "path", runtime_path)
    monkeypatch.setattr(benchmark.sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(builtins, "__import__", fail_numpy)

    benchmark._runtime_import_paths()

    assert str(bundled) in runtime_path
    assert runtime_path[0] == str(bundled)


def test_frozen_protocol_has_three_fixed_cases_and_finite_shared_budget():
    protocol = benchmark.load_protocol()

    assert [case["id"] for case in protocol["cases"]] == [
        "ar_broadband", "hr_broadband", "narrowband_filter",
    ]
    budget = protocol["solver_and_budget"]["budget_per_method_per_case"]
    assert budget["training_grid_objective_evaluations_max"] == 120
    assert budget["structural_proposals_max"] == 8
    assert budget["continue_after_budget"] is False
    assert protocol["reproducibility"]["seed"] == 20261011
    for case in protocol["cases"]:
        controls = case["method_controls"]
        assert controls["structure_max_added_layers"] == 1
        assert controls["growth_cycles"] == 1
        assert controls["candidate_budget"] == 8
        assert controls["needle_thickness_samples"] == 9
        assert controls["structure_position_samples_per_host_layer"] == 17
        assert controls["legacy_max_added_layers"] in {5, 7}
    assert protocol["manufacturing_limits"] == {
        "per_film_layer_min_nm": 20.0,
        "per_film_layer_max_nm": 350.0,
        "total_physical_thickness_max_nm": 2800.0,
        "total_film_layer_count_max": 16,
        "semantics": protocol["manufacturing_limits"]["semantics"],
    }


def test_frozen_targets_and_holdout_are_separate_and_use_s_and_p():
    protocol = benchmark.load_protocol()
    cases = {row["id"]: row for row in protocol["cases"]}

    ar = cases["ar_broadband"]
    assert ar["training"]["bands"][0]["upper_bound"] == 0.01
    assert ar["training"]["angles_deg"] == [0.0, 30.0]
    assert ar["training"]["polarizations"] == ["s", "p"]
    assert ar["holdout"]["bands"][0]["step_nm"] == 0.25
    assert ar["holdout"]["additional_stress_angles_deg"] == [45.0]

    hr = cases["hr_broadband"]
    assert hr["training"]["bands"][0]["lower_bound"] == 0.99

    narrow = cases["narrowband_filter"]
    assert [band["metric"] for band in narrow["training"]["bands"]] == ["T", "R", "R"]
    assert [band["wavelength_nm"] for band in narrow["training"]["bands"]] == [
        [510.0, 520.0], [470.0, 490.0], [540.0, 560.0],
    ]


def test_objective_builder_preserves_equal_band_angle_polarization_weights():
    protocol = benchmark.load_protocol()
    for case in protocol["cases"]:
        objectives = benchmark.build_objectives(case)
        assert all(row.polarization in {"s", "p"} for row in objectives)
        assert all(len(row.angles_deg) == 1 for row in objectives)
        assert math.isclose(sum(row.weight for row in objectives), 1.0)
        for band_index, band in enumerate(case["training"]["bands"]):
            rows = [row for row in objectives if
                    row.wavelength_min_nm == band["wavelength_nm"][0] and
                    row.wavelength_max_nm == band["wavelength_nm"][1]]
            assert len(rows) == 4
            assert math.isclose(sum(row.weight for row in rows), band["total_band_weight"])
            assert all(row.metric == band["metric"] for row in rows)
            assert all(row.normalization_scale == band["normalization_scale"] for row in rows)


def test_independent_airy_handles_bare_interface_and_quarter_wave_antireflection():
    wavelengths = [550.0]
    bare = benchmark.independent_airy_spectrum(
        [], {}, wavelengths, n_ambient=1.0, n_substrate=1.52,
    )
    expected_bare_r = ((1.0 - 1.52) / (1.0 + 1.52)) ** 2
    assert math.isclose(bare["R"][0], expected_bare_r, rel_tol=0.0, abs_tol=1e-14)
    assert math.isclose(bare["R"][0] + bare["T"][0], 1.0, rel_tol=0.0, abs_tol=1e-14)

    index = math.sqrt(1.52)
    coating = [{"material": "QW", "thickness_nm": 550.0 / (4.0 * index)}]
    quarter_wave = benchmark.independent_airy_spectrum(
        coating, {"QW": index}, wavelengths, n_ambient=1.0, n_substrate=1.52,
    )
    assert quarter_wave["R"][0] < 1e-28
    assert math.isclose(quarter_wave["T"][0], 1.0, rel_tol=0.0, abs_tol=1e-13)


def test_independent_characteristic_matrix_reverses_stored_two_layer_stack():
    protocol = benchmark.load_protocol()
    service = benchmark.make_memory_service(protocol)
    case = protocol["cases"][0]
    design = benchmark.make_initial_design(service, case)
    # Front layers are stored substrate-to-incident: H is nearest the
    # substrate, while L is nearest air. Characteristic matrices multiply in
    # the opposite, incident-to-substrate order.
    design["front_layers"] = [
        {
            "id": "two-layer-H",
            "material_id": "structure_bench_H",
            "optical_set_id": "H_const",
            "thickness": {"value": 47.0, "unit": "nm"},
        },
        {
            "id": "two-layer-L",
            "material_id": "structure_bench_L",
            "optical_set_id": "L_const",
            "thickness": {"value": 83.0, "unit": "nm"},
        },
    ]
    design = service.validate(design)
    wavelengths = [510.0, 570.0, 630.0]
    project = benchmark._response(service, design, 510.0, 630.0, 3, 0.0, "s")
    sequence = benchmark.layer_sequence(design, protocol)
    reference = benchmark.independent_airy_spectrum(
        sequence,
        {
            "L": protocol["materials"]["L"]["n"],
            "H": protocol["materials"]["H"]["n"],
        },
        wavelengths,
        n_substrate=protocol["materials"]["substrate"]["n"],
    )

    # Hard-coded characteristic-matrix values for M_L(83 nm) @ M_H(47 nm),
    # using delta_j=2*pi*n_j*d_j/lambda and the ambient/substrate admittances.
    expected_r = [
        0.06453913652449177,
        0.08738520006484526,
        0.10715737232279555,
    ]
    expected_t = [
        0.9354608634755085,
        0.9126147999351548,
        0.8928426276772041,
    ]
    for actual, expected in zip(reference["R"], expected_r):
        assert math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-13)
    for actual, expected in zip(reference["T"], expected_t):
        assert math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-13)
    for actual, expected in zip(project["R"], reference["R"]):
        assert math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-13)
    for actual, expected in zip(project["T"], reference["T"]):
        assert math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-13)


def test_benchmark_materials_and_design_are_created_only_in_memory():
    protocol = benchmark.load_protocol()
    service = benchmark.make_memory_service(protocol)
    design = benchmark.make_initial_design(service, protocol["cases"][0])

    assert not hasattr(service.materials, "directory")
    assert [row["id"] for row in design["front_layers"]] == ["ar_broadband-initial-1"]
    assert design["substrate"]["material_id"] == "structure_bench_substrate"
    assert len(service.materials._records()) == 3


def test_memory_material_profiles_freeze_both_structure_candidates():
    protocol = benchmark.load_protocol()
    service = benchmark.make_memory_service(protocol)
    records = {row["id"]: row for row in service.materials._records()}

    for material_id in ("structure_bench_L", "structure_bench_H"):
        material = records[material_id]
        assert material["design_ready"] is True
        assert material["roles"] == ["film"]
        assert material["resistance_model"] is not None
        assert material["electron_beam_model"] is not None

    case = protocol["cases"][0]
    design = benchmark.make_initial_design(service, case)
    base = benchmark._base_request(service, design, case, protocol)
    from coat_sim.core.film_optimization_structure import (
        FilmStructureOptimizationRequest,
        freeze_film_structure_optimization_request,
    )

    limits = protocol["manufacturing_limits"]
    controls = case["method_controls"]
    request = FilmStructureOptimizationRequest(
        base_request=replace(base, solver="baseline"),
        candidate_materials=benchmark._candidate_material_references(),
        mode="needle_only",
        max_total_thickness_nm=float(limits["total_physical_thickness_max_nm"]),
        min_thickness_nm=float(limits["per_film_layer_min_nm"]),
        max_thickness_nm=float(limits["per_film_layer_max_nm"]),
        max_total_layers=int(limits["total_film_layer_count_max"]),
        max_added_layers=int(controls["structure_max_added_layers"]),
        position_samples=int(controls["structure_position_samples_per_host_layer"]),
        candidate_budget=int(controls["candidate_budget"]),
        needle_thickness_samples=int(controls["needle_thickness_samples"]),
        growth_steps_nm=tuple(float(value) for value in controls["growth_steps_nm"]),
        growth_phase_fractions=tuple(float(value) for value in controls["growth_phase_fractions"]),
        growth_cycles=int(controls["growth_cycles"]),
        seed=int(protocol["reproducibility"]["seed"]),
    )

    frozen = freeze_film_structure_optimization_request(service, request)

    assert [row.material_id for row in frozen.candidate_materials] == [
        "structure_bench_L", "structure_bench_H",
    ]
    frozen_materials = {
        row["id"]: row for row in frozen.base_request.frozen_material_snapshot["materials"]
    }
    for material_id in ("structure_bench_L", "structure_bench_H"):
        assert frozen_materials[material_id]["design_ready"] is True
        assert len(frozen_materials[material_id]["optical_sets"]) == 1


def test_limit_audit_reports_physical_total_and_each_layer_bounds():
    protocol = benchmark.load_protocol()
    service = benchmark.make_memory_service(protocol)
    design = benchmark.make_initial_design(service, protocol["cases"][2])
    audit = benchmark.audit_limits(design, protocol)

    assert audit["feasible"]
    assert audit["layer_count"] == 3
    assert audit["physical_total_thickness_nm"] == 240.0
    assert audit["per_layer_min_nm"] == 75.0
    assert audit["per_layer_max_nm"] == 90.0


def test_structure_counter_adapter_reads_nested_counters_without_zero_filling():
    result = SimpleNamespace(
        base_result=SimpleNamespace(
            state="completed", termination_reason="target_met", targets_met=False,
            max_normalized_error=0.2, weighted_rms_error=0.1,
            evaluations=9, iterations=4, failed_evaluations=0,
        ),
        counters=SimpleNamespace(
            objective_evaluations=11, score_evaluations=11,
            validation_evaluations=1, jacobian_evaluations=8,
            candidate_count=3, needle_probe_count=2, growth_proposal_count=1,
            local_iterations=5, failed_evaluations=0,
            candidate_budget_exhausted=True, evaluation_budget_exhausted=False,
        ),
    )

    stats = benchmark._run_stats(result, "growth_then_needle")

    assert stats["objective_evaluations"] == 11
    assert stats["forward_equivalent_evaluations"] == 11
    assert stats["score_evaluations"] == 11
    assert stats["validation_evaluations"] == 1
    assert stats["jacobian_evaluations"] == 8
    assert stats["structural_candidate_count"] == 3
    assert stats["candidate_budget_exhausted"] is True

    baseline_only = SimpleNamespace(
        state="completed", termination_reason="completed", targets_met=False,
        max_normalized_error=0.2, weighted_rms_error=0.1,
        evaluations=9, iterations=4, failed_evaluations=0,
        validation_evaluations=1,
    )
    fixed_stats = benchmark._run_stats(baseline_only, "fixed_trf")
    assert fixed_stats["objective_evaluations"] == 9
    assert fixed_stats["validation_evaluations"] == 1
    assert fixed_stats["forward_equivalent_evaluations"] == 10


def test_stage_evidence_requires_trace_order_and_does_not_claim_valley_crossing():
    trace = [
        {"sequence": 2, "stage": "growth", "candidate_kind": "growth", "accepted": True},
        {"sequence": 7, "stage": "needle_probe", "candidate_kind": "needle", "accepted": True},
    ]

    evidence = benchmark._structure_stage_evidence(trace)

    assert evidence["accepted_growth_precedes_accepted_needle"]
    assert "does not establish a cross-valley improvement" in evidence["interpretation"]


def test_derivative_helper_instrument_counts_a_real_jacobian_and_value_only_call(tmp_path):
    protocol = benchmark.load_protocol()
    service = benchmark.make_memory_service(protocol)
    design = benchmark.make_initial_design(service, protocol["cases"][0])
    from coat_sim.core.film_optimization import FilmThicknessVariable
    from coat_sim.core import film_optimization_derivatives

    layer = design["front_layers"][0]
    variable = FilmThicknessVariable("front", layer["id"], 20.0, 350.0)
    settings = {
        "wavelength_min_nm": 550.0,
        "wavelength_max_nm": 550.0,
        "samples": 1,
        "angle_deg": 0.0,
        "polarization": "s",
        "first_surface": "front",
    }
    diagnostic_path = tmp_path / "benchmark_instrumentation_diagnostic.log"
    with diagnostic_path.open("a", encoding="utf-8") as diagnostic:
        diagnostic.write("starting real derivative helper instrumentation test\n")
        diagnostic.flush()
        faulthandler.dump_traceback_later(45, repeat=True, file=diagnostic)
        try:
            with benchmark.capture_jacobian_calls() as counts:
                film_optimization_derivatives.simulate_with_thickness_jacobian(
                    design, service.materials._records(), (variable,), settings,
                )
                film_optimization_derivatives.simulate_with_thickness_jacobian(
                    design, service.materials._records(), (), settings,
                )
        finally:
            faulthandler.cancel_dump_traceback_later()
            diagnostic.write("completed real derivative helper instrumentation test\n")
            diagnostic.flush()

    assert counts["helper_calls"] == 2
    assert counts["jacobian_evaluations"] == 1
    assert counts["value_only_helper_calls"] == 1


def test_finite_width_diagnostic_requests_sse_with_empty_jacobian_variables():
    calls = []

    def strict_evaluator(frozen, design, *, with_jacobian, variable_ids):
        calls.append((frozen, design, with_jacobian, variable_ids))
        assert with_jacobian is True
        assert variable_ids == ()
        return {"smooth_loss": 0.125, "score": {"key": (0.25, 0.5)}}

    evaluated = benchmark._evaluate_finite_width_diagnostic(
        strict_evaluator, "frozen-request", {"candidate": "finite"},
    )

    assert evaluated["smooth_loss"] == 0.125
    assert len(calls) == 1
    assert calls[0][2:] == (True, ())

