"""Exact-zero local probes are removed and freshly audited before repair apply."""

from copy import deepcopy
from dataclasses import replace
from importlib import import_module
from threading import Event
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_automatic_repair as automatic
from coat_sim.core import film_automatic_repair_jobs as jobs
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_automatic_repair import permissive_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def zero_problem(*, native_min=None, max_layer=None, optical_pp=1.0, thickness=0.5):
    service, request, source = permissive_problem()
    for row, value, material in zip(
        source["front_layers"], (100.0, thickness, 60.0), ("low", "high", "low"), strict=True
    ):
        row["thickness"]["value"] = value
        row["material_id"] = material
    request = replace(
        request, minimum_nonzero_thickness_nm=native_min, max_layer_thickness_nm=max_layer
    )
    payload = jobs.make_live_request_payload(
        service,
        request,
        source,
        reference_nm=10.0 if thickness >= 5.0 else 1.0,
        thickness_tolerance_nm=2.0 if thickness >= 5.0 else 0.1,
        optical_tolerance_percentage_points=optical_pp,
        request_id="automatic-repair-exact-zero-test",
    )
    return payload


def only_zero_directed(monkeypatch, *, controlled=None):
    original_structures = automatic._live_structures

    def structures(*args):
        yield from (
            item
            for item in original_structures(*args)
            if item[0].startswith(automatic.ZERO_COLLAPSE_OPERATOR)
        )

    monkeypatch.setattr(automatic, "_live_structures", structures)
    if controlled is not None:

        def minimize(fun, point, *, constraints, callback, bounds, **_kwargs):
            assert all(lo == 0.0 for lo, _hi in bounds)
            for thickness in controlled:
                endpoint = point.copy()
                endpoint[1] = thickness / max(1.0, 8.0 if point[1] > 0.5 else 0.9)
                fun(endpoint)
                for constraint in constraints:
                    constraint["fun"](endpoint)
                callback(endpoint)
            return SimpleNamespace(
                x=endpoint,
                success=True,
                nit=len(controlled),
                status=0,
                message="controlled endpoint; complete optics remain real",
            )

        monkeypatch.setattr(import_module("scipy.optimize"), "minimize", minimize)


def execute(payload, tmp_path, *, stop_at=None):
    cancel = Event()
    frames: list[dict[str, Any]] = []
    stop_calls: list[int] = []

    def report(frame):
        jobs.validate_live_progress(frame, payload, after_seq=frames[-1]["seq"] if frames else -1)
        frames.append(deepcopy(frame))
        if stop_at is not None and stop_at(frame):
            stop_calls.append(frame["work"]["physical_calls"])
            cancel.set()

    job = jobs.build_job_payload(None, payload)
    result = jobs.execute_job(
        {"payload": job}, cancel, report, tmp_path / "zero-checkpoint.json", lambda *_args: None
    )
    assert jobs.validate_result_payload(result, expected_job=job, replay=False) == result
    return result, frames, stop_calls


def test_exact_zero_is_not_a_retained_thin_defect_and_has_a_real_zero_directed_branch():
    _, request, source = permissive_problem()
    source["front_layers"][1]["thickness"]["value"] = 0.0
    assert automatic.thickness_defects(source, 8.0)["count"] == 0
    assert automatic.deficient_ids(source, 8.0, include_zero=False) == []
    source["front_layers"][1]["thickness"]["value"] = 7.0
    origins = {row["id"]: [row["id"]] for row in source["front_layers"]}
    proposals = list(automatic._live_structures(request, source, origins, source, 10.0, 8.0))
    directed = [
        item for item in proposals if item[0] == automatic.ZERO_COLLAPSE_OPERATOR + "defect"
    ]
    assert len(directed) == 1 and directed[0][1] == source
    assert proposals[0][0] == "compensate_thin_layers"
    assert any(item[0] == "delete_layer_merge" for item in proposals)


def test_continuous_zero_endpoint_is_freshly_canonicalized_audited_and_cancel_safe(
    monkeypatch, tmp_path
):
    payload = zero_problem()
    only_zero_directed(monkeypatch, controlled=[0.0])
    result, frames, calls = execute(payload, tmp_path, stop_at=lambda f: f["champion"] is not None)
    body = result["result"]
    best = body["best"]
    assert best is not None
    assert (
        result["termination_reason"] == "cancelled"
        and result["work"]["physical_calls"] == calls[-1]
    )
    probes = [
        f["current"]
        for f in frames
        if f["current"] is not None
        and f["current"]["evidence_scope"] == "training"
        and any(r["thickness"]["value"] == 0.0 for r in f["current"]["design"]["front_layers"])
    ]
    assert probes and all(p["physical_hash"] != best["physical_hash"] for p in probes)
    assert len(best["design"]["front_layers"]) == 1
    assert best["design"]["front_layers"][0]["thickness"]["value"] == 160.0
    assert best["layers"][0]["origins"] == ["left", "right"]
    assert (
        best["assessment"]["applicable_improvement"] and best["assessment"]["all_requirements_met"]
    )
    assert best["converged"] is False
    assert (
        best["physical_hash"]
        == best["evidence_binding"]["training_hash"]
        == best["evidence_binding"]["audit_hash"]
    )
    records = [r for r in body["trace"] if r.get("kind") == "zero_layer_canonicalization"]
    assert (
        records
        and records[0]["raw_physical_hash"] != records[0]["physical_hash"] == best["physical_hash"]
    )
    assert records[0]["removed_layer_ids"] == ["defect"] and records[0]["merged_ids"] == {
        "right": "left"
    }
    assert (
        jobs.validate_result_payload(
            result, expected_job=jobs.build_job_payload(None, payload), replay=True
        )
        == result
    )


def test_real_slsqp_zero_directed_quadratic_reaches_exact_zero_without_tiny_substitute(
    monkeypatch, tmp_path
):
    payload = zero_problem()
    only_zero_directed(monkeypatch)
    real_evaluate = automatic.evaluate_frozen_structure_candidate
    zero_jacobians: list[np.ndarray] = []

    def observe(proxy, design, **kwargs):
        value = real_evaluate(proxy, design, **kwargs)
        if kwargs.get("with_jacobian") and "defect" in kwargs.get("zero_thickness_layer_ids", ()):
            assert design["front_layers"][1]["material_id"] == "high"
            assert design["front_layers"][0]["material_id"] == "low"
            for row in value["responses"]:
                column = np.asarray(row["jacobian_values_by_layer_id"]["defect"])
                assert column.shape == np.asarray(row["actual_values"]).shape
                assert np.all(np.isfinite(column))
                zero_jacobians.append(column.copy())
        return value

    monkeypatch.setattr(automatic, "evaluate_frozen_structure_candidate", observe)
    result, frames, _calls = execute(payload, tmp_path, stop_at=lambda f: f["champion"] is not None)
    best = result["result"]["best"]
    assert best is not None and len(best["design"]["front_layers"]) == 1
    assert zero_jacobians
    assert any(
        f["current"] is not None
        and f["current"]["evidence_scope"] == "training"
        and any(row["thickness"]["value"] == 0.0 for row in f["current"]["design"]["front_layers"])
        for f in frames
    )
    assert not any(
        0.0 < row["thickness"]["value"] < np.finfo(float).tiny
        for row in best["design"]["front_layers"]
    )


def test_cancel_on_raw_zero_preview_does_not_start_canonical_training_or_audit(
    monkeypatch, tmp_path
):
    payload = zero_problem()
    only_zero_directed(monkeypatch, controlled=[0.0])

    def is_zero(frame):
        return (
            frame["current"] is not None
            and frame["current"]["evidence_scope"] == "training"
            and any(
                r["thickness"]["value"] == 0.0 for r in frame["current"]["design"]["front_layers"]
            )
        )

    result, frames, calls = execute(payload, tmp_path, stop_at=is_zero)
    assert result["termination_reason"] == "cancelled" and result["result"]["best"] is None
    assert result["work"]["physical_calls"] == calls[-1] and result["work"]["audit_calls"] == 1
    assert not any(
        r.get("kind") == "zero_layer_canonicalization" for r in result["result"]["trace"]
    )
    assert frames[-1]["champion"] is None


def test_nonzero_native_minimum_probe_is_never_applicable_but_zero_removal_is(
    monkeypatch, tmp_path
):
    payload = zero_problem(native_min=5.0, optical_pp=100.0, thickness=7.0)
    only_zero_directed(monkeypatch, controlled=[4.0, 0.0])
    result, frames, _calls = execute(payload, tmp_path, stop_at=lambda f: f["champion"] is not None)
    probes = [
        f
        for f in frames
        if f["current"] is not None
        and f["current"]["evidence_scope"] == "training"
        and f["current"]["design"]["front_layers"][1]["thickness"]["value"] == 4.0
    ]
    assert probes and all(f["champion"] is None for f in probes)
    best = result["result"]["best"]
    assert best is not None
    assert all(r["thickness"]["value"] >= 5.0 for r in best["design"]["front_layers"])
    assert best["physical_hash"] == physical_design_hash(best["design"])
    bad = deepcopy(probes[0])
    bad["current"]["operator"] = "compensate_thin_layers"
    with pytest.raises(ValueError, match="native physical"):
        jobs.validate_live_progress(bad, payload)


def test_zero_cleanup_cannot_bypass_native_maximum_through_same_material_merge(
    monkeypatch, tmp_path
):
    payload = zero_problem(native_min=5.0, max_layer=110.0, optical_pp=100.0, thickness=7.0)
    only_zero_directed(monkeypatch, controlled=[0.0])
    result, _frames, _calls = execute(payload, tmp_path)
    assert result["result"]["best"] is None and result["work"]["audit_calls"] == 1
    assert result["termination_reason"] == "frontier_exhausted"


def test_exact_zero_removal_keeps_strict_zero_optical_tolerance(monkeypatch, tmp_path):
    payload = zero_problem(optical_pp=0.0)
    only_zero_directed(monkeypatch, controlled=[0.0])
    result, _frames, _calls = execute(payload, tmp_path)
    assert result["result"]["best"] is None and result["work"]["audit_calls"] == 1
    assert any(r.get("kind") == "zero_layer_canonicalization" for r in result["result"]["trace"])


def test_exact_zero_merge_never_unions_different_optical_data_lineage():
    _, request, source = permissive_problem()
    source["front_layers"][1]["thickness"]["value"] = 0.0
    source["front_layers"][-1]["optical_set_id"] = "other-data"
    mapping = {row["id"]: [row["id"]] for row in source["front_layers"]}
    # Material lookup belongs to the physical evaluator; cleanup only combines
    # identical references, never the material name alone.
    result, lineage = automatic._live_cleanup(request, source, mapping)
    assert len(result.design["front_layers"]) == 2
    assert lineage == {"left": ["left"], "right": ["right"]}


def test_cancel_raw_zero_retains_prior_complete_partial_champion(monkeypatch, tmp_path):
    payload = zero_problem(optical_pp=100.0, thickness=7.0)
    only_zero_directed(monkeypatch, controlled=[7.5, 0.0])

    def raw_zero(frame):
        return (
            frame["current"] is not None
            and frame["current"]["evidence_scope"] == "training"
            and any(
                row["thickness"]["value"] == 0.0
                for row in frame["current"]["design"]["front_layers"]
            )
        )

    result, frames, calls = execute(payload, tmp_path, stop_at=raw_zero)
    best = result["result"]["best"]
    assert result["termination_reason"] == "cancelled" and best is not None
    assert best["design"]["front_layers"][1]["thickness"]["value"] == 7.5
    assert (
        best["assessment"]["applicable_improvement"]
        and not best["assessment"]["thickness_complete"]
    )
    assert result["work"]["physical_calls"] == calls[-1]
    assert frames[-1]["champion"]["physical_hash"] == best["physical_hash"]
    assert frames[-1]["current"]["physical_hash"] != best["physical_hash"]
    assert not any(
        row.get("kind") == "zero_layer_canonicalization" for row in result["result"]["trace"]
    )
