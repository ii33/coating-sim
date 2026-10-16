"""Offline equation/contract tests and explicit real-CUDA opt-in validation.

The NumPy adapter tests equations only. It is not GPU evidence, and does not
exercise Dr.Jit's compiler, typed array bindings, device or kernel profiling.
"""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import builtins
import subprocess
import sys

import numpy as np
import pytest

from tools.validation.drjit_coherent_tmm import DrjitTmm
from tools.validation.gpu_tmm_probe import (
    GATES, _batch, appearance_batches, compare, direction_quantization,
    errors, input_hash, physics_checks, quantized_batch, reference, run,
    serialize_history, synthetic_batches, hardware, _mitsuba_variant_in_child,
)

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


class NumpyEquationAdapter:
    """Only the arithmetic vocabulary consumed by the isolated equations."""
    JitFlag = SimpleNamespace(FastMath="FastMath")
    select = staticmethod(np.where)
    sqrt = staticmethod(np.sqrt)
    exp = staticmethod(np.exp)
    conj = staticmethod(np.conj)
    abs = staticmethod(np.abs)
    isfinite = staticmethod(np.isfinite)
    maximum = staticmethod(np.maximum)

    @staticmethod
    def flag(_flag):
        return False


def numpy_equations(batch):
    runtime = object.__new__(DrjitTmm)
    runtime.dr = NumpyEquationAdapter()
    runtime.C = lambda real=0, imag=0: np.asarray(real, dtype=np.complex128) + 1j * np.asarray(imag, dtype=np.float64)
    runtime.F = lambda value: np.asarray(value, dtype=np.float64)
    runtime.B = lambda value: np.full(batch.wavelength_nm.shape, value, dtype=bool)
    data = {"w": batch.wavelength_nm, "n": tuple(batch.indices[:, j] for j in range(batch.indices.shape[1])),
            "d": tuple(batch.thickness_nm), "beta": batch.beta, "polarization": batch.polarization}
    output = runtime.solve_device(data)
    return np.column_stack(output[:3]), output[3], output[4]


def test_all_isolated_equations_match_production_reference():
    for batch in synthetic_batches():
        expected = reference(batch)
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            actual, invalid, critical = numpy_equations(batch)
        assert not np.any(invalid), batch.name
        np.testing.assert_allclose(actual, expected, atol=3e-12, rtol=0, err_msg=batch.name)
        if "internal_critical" in batch.name:
            assert np.any(critical)


def test_fp32_beta_grazing_input_flux_collapse_is_reported():
    batch = _batch("grazing", [550], [1, 1.5], [], np.sin(np.deg2rad(89.999)))
    quantized, legal = quantized_batch(batch, "float32")
    assert not legal[0]
    assert quantized.wavelength_nm.size == 0
    actual = np.full((1, 3), np.nan)
    result = compare(batch, actual, reference(batch), "float32", np.array([True]), np.array([False]))
    assert result["quantized_inputs_illegal_rows"] == 1
    assert result["physics"]["nonfinite_values"] == 3
    assert not result["candidate_gate_pass"]


def test_fp32_degree_neighbors_can_cross_tir_boundary():
    cases = [b for b in synthetic_batches() if "angle_neighbors" in b.name and b.polarization == "s"]
    result = reference(cases[0])
    waves = 86
    assert result[0, 1] > 5e-4
    assert result[2 * waves, 1] == 0
    assert abs(result[0, 1] - 5.9843e-4) < 1e-8


def test_error_contract_is_absolute_and_unclamped():
    expected = np.array([[0., 1., 0.], [.3, .2, .5]])
    actual = expected.copy()
    actual[0] = [-2e-8, 1 + 2e-8, 0]
    report = errors(actual, expected)
    assert report["R"]["max_abs"] == 2e-8
    assert not any("relative" in field for row in report.values() for field in row)
    physical = physics_checks(actual)
    assert physical["production_passivity_violating_values"] == 2
    assert physical["max_balance_abs"] == 0  # Constructed balance alone proves nothing.
    assert not physical["production_gate_pass"]
    assert GATES["production_lower_bound"] == -1e-9
    assert GATES["production_upper_bound"] == 1 + 1e-8
    assert GATES["production_balance_absolute"] == 2e-7


def test_direction_quantization_is_separate_fp64_acos_pipeline():
    batch = _batch("angle", [550], [1.5, 1], [], 1.5 * np.sin(np.deg2rad(41.8103143)))
    diagnostic = direction_quantization(batch, reference(batch))
    assert "widen FP64 before acos" in diagnostic["pipeline"]
    assert diagnostic["max_cosine_input_abs"] > 0
    assert diagnostic["RTA_absolute_error"]["T"]["max_abs"] > 0


def test_input_hash_covers_order_polarization_and_thickness():
    batch = _batch("basic", [550], [1, 2, 1.5], [60], 0)
    assert input_hash(batch) != input_hash(replace(batch, indices=batch.indices[:, ::-1]))
    assert input_hash(batch) != input_hash(replace(batch, polarization="s"))
    assert input_hash(batch) != input_hash(replace(batch, thickness_nm=np.array([61.])))


def test_reference_only_run_records_no_gpu_claim():
    args = SimpleNamespace(no_synthetic=False, appearance_job=None, case=["zero_layer_exit_critical_exact_s"],
                           reference_only=True, backends=["cuda"], precisions=["float64", "float32"],
                           no_benchmark=True, repeats=1, batch_sizes=[8], llvm_threads=None)
    report = run(args)
    assert report["summary"]["GPU_verification_status"] == "not run"
    assert report["summary"]["evaluated_result_count"] == 0
    assert report["cases"][0]["results"] == []
    assert report["hardware"]["mitsuba_variant_probe"]["status"] == "not_requested_reference_only"
    json.dumps(report, allow_nan=False)


def test_native_variant_probe_isolated_from_parent_and_failure_is_reported(monkeypatch):
    seen = {}
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "mitsuba" or name.startswith("mitsuba."):
            pytest.fail("Mitsuba must not be imported in the pytest process")
        return original_import(name, *args, **kwargs)

    def failed_child(command, **kwargs):
        seen["command"] = command
        seen["home"] = kwargs["env"]["HOME"]
        assert Path(seen["home"]).is_dir()
        assert kwargs["env"]["XDG_CACHE_HOME"].startswith(seen["home"])
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(command, -6, b"", b"native init failed\n")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subprocess, "run", failed_child)
    variant, diagnostic = _mitsuba_variant_in_child()
    assert variant is None
    assert diagnostic == {"status": "child_failed", "returncode": -6,
                          "stderr": "native init failed\n"}
    assert seen["command"][:3] == [sys.executable, "-I", "-B"]
    assert not Path(seen["home"]).exists()


def test_reference_only_hardware_never_probes_mitsuba(monkeypatch):
    monkeypatch.setattr("tools.validation.gpu_tmm_probe._mitsuba_variant_in_child",
                        lambda: pytest.fail("reference-only must not start native variant probe"))
    report = hardware(probe_variant=False)
    assert report["mitsuba_variant"] is None
    assert report["mitsuba_variant_probe"] == {"status": "not_requested_reference_only"}


def test_native_variant_probe_rejects_missing_variant_key(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, b"{}\n", b""))
    variant, diagnostic = _mitsuba_variant_in_child()
    assert variant is None
    assert diagnostic["status"] == "invalid_output"
    assert "variant payload" in diagnostic["error"]


def test_appearance_envelope_rejected_without_synthetic_substitution(tmp_path):
    path = tmp_path / "enveloped.json"
    path.write_text(json.dumps({"job": {"schema_version": 1}}))
    with pytest.raises(ValueError, match="schema_version"):
        appearance_batches(path)


def test_appearance_materials_thickness_samples_and_both_orders_are_actual(tmp_path):
    from tests.test_appearance_result_contract import _job
    job = _job()
    job["front_layers"] = []
    for layer_id, nn, kk, depths in (("actual-ZnS", [2.35, 2.28], [.03, .02], [[50, 60], [70, 80]]),
                                       ("actual-YbF3", [1.52, 1.49], None, [[20, 22], [24, 26]])):
        material = {"n": nn, "wavelength_nm": [500., 600.], "valid_wavelength_nm": [500., 600.]}
        if kk is None:
            material["assume_lossless"] = True
        else:
            material["k"] = kk
        job["front_layers"].append({"layer_id": layer_id, "material": material,
            "thickness_field": {"unit": "nm", "origin": "captured_process_step", "x_mm": [-2, 2], "y_mm": [-2, 2],
                                "thickness_nm": depths, "valid_mask": [[True, True], [True, True]], "valid_cell_mask": [[True]]}})
    path = tmp_path / "actual.json"
    path.write_text(json.dumps(job))
    cases, metadata = appearance_batches(path)
    assert metadata["material_inputs"]["front_substrate_outward"][1]["assume_lossless"]
    front_samples = [row for row in metadata["samples"] if row["face"] == "front"]
    assert len(front_samples) == 3
    assert front_samples[1]["total_thickness_nm"] == 70
    assert front_samples[2]["total_thickness_nm"] == 106
    outward = next(row for row in cases if "minimum_total" in row.name and "substrate_to_outside" in row.name and row.polarization == "s" and "critical" not in row.name)
    inward = next(row for row in cases if "minimum_total" in row.name and "outside_to_substrate" in row.name and row.polarization == "s")
    np.testing.assert_allclose(outward.thickness_nm, [50, 20])
    np.testing.assert_allclose(inward.thickness_nm, [20, 50])
    np.testing.assert_allclose(outward.indices[0], [1.5, 2.35+.03j, 1.52, 1])
    np.testing.assert_allclose(inward.indices[0], [1, 1.52, 2.35+.03j, 1.5])
    for batch in cases:
        assert np.isfinite(reference(batch)).all()


def test_kernel_history_serialization_accepts_current_and_legacy_entries():
    entry = {"backend": "CUDA", "type": "JIT", "execution_time": .25,
             "source": ".f64 .f64 .f32", "cache_hit": False}
    rows = serialize_history([entry, SimpleNamespace(**entry)])
    assert rows[0]["source_precision_tokens"][".f64"] == 2
    assert rows[0]["execution_time"] == .25
    assert rows[0] == rows[1]


@pytest.mark.real_gpu
@pytest.mark.parametrize("precision", ["float64", "float32"])
def test_real_cuda_typed_kernel_precision_and_branch_contract(precision):
    pytest.importorskip("drjit")
    runtime = DrjitTmm("cuda", precision)
    # A real CUDA result is essential. Float32 is allowed to FAIL the candidate
    # qualification gate: this test confirms that rejection is surfaced.
    for batch in synthetic_batches():
        actual, invalid, critical = runtime.evaluate(batch)
        report = compare(batch, actual, reference(batch), precision, invalid, critical)
        if precision == "float64":
            assert report["candidate_gate_pass"], (batch.name, report)
        else:
            assert actual.shape == (batch.wavelength_nm.size, 3)
            if np.any(~quantized_batch(batch, precision)[1]):
                assert not report["candidate_gate_pass"]
