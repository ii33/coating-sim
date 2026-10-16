"""Independent 100-digit boundary checks; CUDA runs only by explicit marker."""
from __future__ import annotations

from dataclasses import replace
import json

import numpy as np
import pytest

from tools.validation.check_tmm_boundary_oracle import (
    ORACLE_SHA256, boundary_batches, numpy_equations, run,
)
from tools.validation.gpu_tmm_probe import _batch
from tools.validation.tmm_boundary_oracle import oracle_rta

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.fixture(autouse=True)
def _high_precision_oracle_available():
    # A module-level importorskip would make full CI collection fail its
    # every-test-file-has-items audit.  The missing optional dependency should
    # skip execution, not erase the collected contract items.
    pytest.importorskip("mpmath")


@pytest.mark.parametrize("polarization", ("s", "p", "unpolarized"))
def test_bare_interface_matches_analytic_fresnel_normal_incidence(polarization):
    batch = replace(_batch("bare", [405, 550, 830], [1.5, 1.], [], 0),
                    polarization=polarization)
    np.testing.assert_allclose(oracle_rta(batch), np.tile([.04, .96, 0.], (3, 1)),
                               rtol=0, atol=2e-15)


def test_nextafter_zero_thickness_is_exact_identity_even_when_recurrence_is_not():
    beta = np.nextafter(1., 0.)
    batch = replace(_batch("zero", [550.], [1.5, 1., 1.5], [0.], beta),
                    polarization="p")
    expected = oracle_rta(batch)
    np.testing.assert_allclose(expected, [[0., 1., 0.]], rtol=0, atol=1e-90)
    original, invalid, *_ = numpy_equations(batch, False)
    assert not invalid.any()
    assert np.max(np.abs(original - expected)) > 2e-12
    candidate, invalid, exact, near, _ = numpy_equations(batch, True)
    assert not invalid.any()
    assert not exact.any()  # exact-zero layer was removed before the solve
    assert not near.any()
    np.testing.assert_allclose(candidate, expected, rtol=0, atol=5e-16)


def test_positive_thickness_neighbor_is_not_eliminated():
    batch = replace(_batch("thin", [550.], [1.5, 1., 1.5], [100.], 1.),
                    polarization="s")
    expected = oracle_rta(batch)
    candidate, invalid, exact, near, series = numpy_equations(batch, True)
    assert not invalid.any() and exact.all() and near.all()
    assert np.asarray(series).item() == 1
    assert expected[0, 0] > .1
    np.testing.assert_allclose(candidate, expected, rtol=0, atol=5e-15)


def test_all_fifteen_boundary_groups_have_independent_cpu_equation_proof():
    batches = boundary_batches()
    assert len(batches) == 45
    assert {batch.polarization for batch in batches} == {"s", "p", "unpolarized"}
    proof = run(backend="numpy")
    assert proof["oracle"]["decimal_digits"] == 100
    assert proof["oracle"]["source_sha256"] == ORACLE_SHA256
    assert proof["summary"]["physical_groups"] == 15
    assert proof["summary"]["original_failed_cases"] > 0
    assert proof["summary"]["candidate_failed_cases"] == 0
    assert not proof["summary"]["real_backend_verified"]
    for case in proof["cases"]:
        assert case["results"]["candidate"]["max_abs_vs_oracle"] <= 2e-12
        assert not case["results"]["candidate"]["actual_backend_kernel_launch_verified"]


def test_extreme_absorption_stays_finite_under_strict_numpy_errors():
    batch = next(batch for batch in boundary_batches()
                 if batch.name == "absorbing_critical_thick_nextafter_exact_p")
    expected = oracle_rta(batch)
    actual, invalid, _, near, series = numpy_equations(batch, True)
    assert not invalid.any() and near.all()
    assert np.asarray(series).sum() >= 1
    assert np.isfinite(actual).all() and np.isfinite(expected).all()
    assert expected[0, 1] == 0.0  # physically negligible, not a hidden NaN
    np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-12)


def test_saved_fixture_checks_oracle_sha_input_hash_and_shape(tmp_path):
    output = tmp_path / "proof.json"
    proof = run(backend="numpy", cases=["internal_nextafter_d0_below_p"], output=output)
    assert output.exists() and output.with_suffix(".npz").exists()
    assert run(backend="numpy", cases=["internal_nextafter_d0_below_p"],
               oracle_fixture=output)["summary"]["candidate_all_pass"]
    data = json.loads(output.read_text(encoding="utf-8"))
    data["oracle"]["source_sha256"] = "0" * 64
    output.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="source SHA256"):
        run(backend="numpy", cases=["internal_nextafter_d0_below_p"],
            oracle_fixture=output)
    data = json.loads(json.dumps(proof))
    data["cases"][0]["input_sha256"] = "0" * 64
    output.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="input hash"):
        run(backend="numpy", cases=["internal_nextafter_d0_below_p"],
            oracle_fixture=output)
    data["cases"][0]["input_sha256"] = proof["cases"][0]["input_sha256"]
    data["cases"][0]["shape"] = [4, 3]
    output.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="shape"):
        run(backend="numpy", cases=["internal_nextafter_d0_below_p"],
            oracle_fixture=output)


@pytest.mark.real_gpu
def test_real_cuda_candidate_boundary_when_available():
    dr = pytest.importorskip("drjit")
    if not dr.has_backend(dr.JitBackend.CUDA):
        pytest.skip("Dr.Jit CUDA backend unavailable")
    proof = run(backend="cuda", precision="float64",
                cases=["internal_nextafter_d0_below_p"])
    assert proof["summary"]["candidate_all_pass"]
    assert proof["summary"]["real_backend_verified"]
