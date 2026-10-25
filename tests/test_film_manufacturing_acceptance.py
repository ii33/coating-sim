"""Pure harness tests: real optics is forbidden in every test in this module."""
from copy import deepcopy
import json

import pytest

from tools import run_film_manufacturing_acceptance as harness
from coat_sim.core import film_optimization_structure as structure
from coat_sim.core import film_synthesis_manufacturing as repair

pytestmark = pytest.mark.algorithm


@pytest.fixture(autouse=True)
def forbid_real_optics(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Pure harness tests must not dispatch real optics")
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", forbidden)


@pytest.mark.parametrize("family,layers", [("AR", 3), ("HR", 8), ("BPF", 9), ("mixed", 3)])
@pytest.mark.parametrize("mode", harness.MODES)
def test_frozen_fixtures_reproducible_and_no_optical_dispatch(family, layers, mode):
    for seed in harness.SEEDS:
        first = harness.make_fixture(family, mode, seed)
        second = harness.make_fixture(family, mode, seed)
        assert harness.fixture_manifest(first) == harness.fixture_manifest(second)
        assert len(first.design["front_layers"]) == layers
        assert first.design["front_layers"][1]["id"] == first.defect_id
        assert first.design["front_layers"][1]["thickness"]["value"] == 2.
        assert first.request.minimum_nonzero_thickness_nm == 1.
        assert first.request.max_layer_thickness_nm == 300.
        assert first.request.max_layers is None and first.request.max_total_thickness_nm is None
        assert first.request.max_working_set_bytes == 512 * 1024**2
        assert first.request.validation_samples_multiplier == 2
        for objective in first.request.objectives:
            assert objective.wavelength_samples == 5
            assert objective.angles_deg == (0., 15.) and objective.polarization == "unpolarized"
            assert objective.mean.enabled is (mode != "pointwise")
            assert objective.pointwise.enabled is (mode != "mean")
        for material in first.request.frozen_material_snapshot["materials"]:
            for optical in material["optical_sets"]:
                assert len(set(optical["n"])) == 1 and set(optical["k"]) == {0.}
                assert "synthetic" in optical["source"]


def test_source_and_route_topologies_are_separate_and_dynamic():
    fixture = harness.make_fixture("HR", "both", 0)
    original = deepcopy(fixture.design)
    a, b = harness.prepared_routes(fixture)
    assert fixture.design == original
    assert len(a.design["front_layers"]) == 8
    assert len(b.design["front_layers"]) == 6
    assert a.design["front_layers"][1]["thickness"]["value"] == 8.
    assert fixture.defect_id not in b.lower_bounds_nm
    assert set(b.lower_bounds_nm) == {r["id"] for r in b.design["front_layers"]}
    assert len(b.merged_ids) == 1 and b.removed_ids == (fixture.defect_id,)
    for route in (a, b):
        assert all(route.lower_bounds_nm[r["id"]] == max(1., min(r["thickness"]["value"], 8.))
                   for r in route.design["front_layers"])


def test_seed_modes_share_same_physical_perturbation_and_bpf_cavity():
    import numpy as np
    fixture = harness.make_fixture("BPF", "both", 1)
    rng = np.random.default_rng(1)
    for index, row in enumerate(fixture.design["front_layers"]):
        if index == 1:
            continue
        n = 2.2 if row["material_id"] == "high" else np.sqrt(1.5)
        expected = 550 / (4 * n) * rng.uniform(.9, 1.1) * (2 if index == 4 else 1)
        assert row["thickness"]["value"] == expected


def test_audit_memory_includes_all_worst_case_owners_and_never_bypasses_cap():
    fixture = harness.make_fixture("BPF", "both", 0)
    model = harness.audit_memory_model(fixture.request, fixture.design)
    # Nine dense and eight offset samples, two angles, three bands; 15 possible
    # brackets per angle and bounded side, with unmodified maxiter=500.
    assert model["complete_audit_work"]["wavelength_points"] == 3 * 2 * (9 + 8 + 15 * 500)
    components = model["components"]
    assert model["total_bytes"] == sum(components.values())
    assert components["three_compact_score_copies"] == 3 * components["canonical_validation"]
    assert model["total_bytes"] > harness.MEMORY_CAP
    ledger = harness.CellLedger(fixture, lambda: None)
    with pytest.raises(repair.ManufacturingRepairStop) as caught:
        ledger.admit(phase="audit", route="source", design=fixture.design, jacobian=False,
                     estimated_bytes=model["total_bytes"])
    assert caught.value.reason == "resource_guard"
    assert not ledger.admissions[-1]["admitted"] and ledger.reservation is None


def test_exact_grid_reservation_and_separate_source_route_ledgers(monkeypatch):
    fixture = harness.make_fixture("AR", "both", 0)
    ledger = harness.CellLedger(fixture, lambda: None)
    # Synthetic fake kernel verifies accounting only, not a physics evaluation.
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian",
                        lambda design, payload, variables, settings, **kw: {"wavelength_nm": [0] * settings["samples"]})
    ledger.admit(phase="start", route="source", design=fixture.design, jacobian=True, estimated_bytes=1)
    with ledger.instrument(), ledger.dispatch():
        for _angle in (0., 15.):
            structure.simulate_with_thickness_jacobian(fixture.design, {}, ["id"], {"samples": 5})
    report = ledger.report()
    assert report["completed"]["source.start"]["FJ"] == dict(kernel_calls=2, wavelength_points=10, layer_points=30)
    assert report["completed"]["raise_selected.start"]["FJ"]["kernel_calls"] == 0
    assert report["completed"]["source.start"]["F"]["kernel_calls"] == 0
    assert report["caps_respected"]


def test_kernel_without_admission_fails_before_dispatch():
    fixture = harness.make_fixture("AR", "mean", 0)
    ledger = harness.CellLedger(fixture, lambda: None)
    with ledger.instrument(), pytest.raises(RuntimeError, match="Unreserved"):
        structure.simulate_with_thickness_jacobian(fixture.design, {}, [], {"samples": 1})


def test_incomplete_kernel_count_is_not_completed_work(monkeypatch):
    fixture = harness.make_fixture("AR", "mean", 0)
    ledger = harness.CellLedger(fixture, lambda: None)
    def incomplete(*args, **kwargs):
        raise RuntimeError("synthetic interruption")
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", incomplete)
    ledger.admit(phase="start", route="source", design=fixture.design, jacobian=True, estimated_bytes=1)
    with ledger.instrument(), pytest.raises(RuntimeError, match="synthetic interruption"):
        with ledger.dispatch():
            structure.simulate_with_thickness_jacobian(fixture.design, {}, ["id"], {"samples": 5})
    report = ledger.report()
    assert report["attempted_kernel_calls"]["source.start"]["FJ"] == 1
    assert report["incomplete_kernel_calls"]["source.start"]["FJ"] == 1
    assert report["completed"]["source.start"]["FJ"]["kernel_calls"] == 0


def test_live_cancel_adapter_checks_every_time():
    calls: list[None] = []
    def check():
        calls.append(None)
        if len(calls) == 2:
            raise repair.ManufacturingRepairStop("time_limit")
    event = harness.LiveCancelEvent(check)
    assert event.is_set() is False
    with pytest.raises(repair.ManufacturingRepairStop):
        event.is_set()
    assert len(calls) == 2


def test_source_expiry_never_starts_optics_or_routes(monkeypatch):
    fixture = harness.make_fixture("AR", "mean", 0)
    ticks = iter([0., 5.])
    report = harness.run_cell(fixture, clock=lambda: next(ticks, 5.))
    assert report["status"] == "source_incomplete"
    assert report["source_status"] == "not_started:time_limit"
    assert not report["routes"] and report["source"] is None
    assert report["source_calls"]["start"] == 0


def test_dry_run_has_24_cells_and_no_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "run_cell", lambda *a, **k: pytest.fail("Dry-run entered optics"))
    assert harness.main(["--output", str(tmp_path / "plan")]) == 0
    plan = json.loads((tmp_path / "plan/manifest.json").read_text())
    assert len(plan["cells"]) == 24 and plan["optical_calls_executed"] == 0
    assert len({row["cell_id"] for row in plan["cells"]}) == 24
    assert plan["manifest_sha256"] == harness.sha256_json({k: v for k, v in plan.items() if k != "manifest_sha256"})


def test_execution_requires_approval_and_clean_unchanged_source(tmp_path, monkeypatch):
    for key in harness.THREAD_VARIABLES:
        monkeypatch.setenv(key, "1")
    with pytest.raises(ValueError, match="requires --plan"):
        harness.main(["--output", str(tmp_path / "run"), "--execute"])
    plan = harness.make_plan()
    with pytest.raises(ValueError, match="Explicit approval"):
        harness.validate_execution_plan(plan, "wrong")
    dirty = deepcopy(plan)
    dirty["source"]["clean"] = False
    dirty["manifest_sha256"] = harness.sha256_json({k: v for k, v in dirty.items() if k != "manifest_sha256"})
    monkeypatch.setattr(harness, "make_plan", lambda: dirty)
    with pytest.raises(ValueError, match="clean"):
        harness.validate_execution_plan(dirty, dirty["manifest_sha256"])


def test_outputs_cannot_dirty_source_or_overwrite(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        harness._external_output(harness.ROOT / "results")
    (tmp_path / "old.json").write_text("{}")
    with pytest.raises(ValueError, match="never overwrite"):
        harness.main(["--output", str(tmp_path)])


def install_synthetic_kernel(monkeypatch):
    """Exercise orchestration with constant fabricated responses, never TMM."""
    import numpy as np
    calls = []
    def synthetic(design, payload, variables, settings, **kwargs):
        calls.append(dict(ids=tuple(v.layer_id for v in variables), layers=len(design["front_layers"])))
        waves = np.linspace(settings["wavelength_min_nm"], settings["wavelength_max_nm"], settings["samples"])
        count = len(waves)
        return dict(wavelength_nm=waves, R=np.full(count, .2), T=np.full(count, .8),
            jacobian_R=np.zeros((count, len(variables))), jacobian_T=np.zeros((count, len(variables))))
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", synthetic)
    return calls


@pytest.mark.parametrize("mode", harness.MODES)
def test_mock_kernel_end_to_end_retains_source_and_repaired_roles(monkeypatch, mode):
    calls = install_synthetic_kernel(monkeypatch)
    fixture = harness.make_fixture("AR", mode, 0)
    report = harness.run_cell(fixture, clock=lambda: 0.)
    assert report["error"] is None
    assert report["source_status"] == "audited"
    assert report["source_calls"] == dict(start=1, audit=1, completed_start=1, completed_audit=1)
    assert report["status"] == "complete" and report["audit_complete"]
    assert len(report["routes"]) == 2
    assert report["source"]["audit"] is not None
    assert report["spectral_incumbent"]["physical_hash"] == report["source"]["physical_hash"]
    for route in report["routes"]:
        assert route["start"] is not None and route["champion"]["audit"] is not None
        assert route["calls"]["start_calls"] == 1
        assert route["calls"]["audit_calls"] == 1
        assert route["calls"]["refinement_calls"] <= harness.REFINEMENT_CALLS
    assert any(c["layers"] == 1 and len(c["ids"]) == 1 for c in calls)
    assert any(c["layers"] == 3 and len(c["ids"]) == 3 for c in calls)
    assert any(not c["ids"] for c in calls)
    assert report["physical_ledger"]["caps_respected"]


def test_mock_kernel_source_audit_guard_preserves_training_and_skips_routes(monkeypatch):
    calls = install_synthetic_kernel(monkeypatch)
    fixture = harness.make_fixture("BPF", "both", 0)
    report = harness.run_cell(fixture, clock=lambda: 0.)
    assert report["error"] is None
    assert report["source_status"] == "training_complete:resource_guard"
    assert report["status"] == "source_incomplete" and not report["audit_complete"]
    assert report["source"]["training"] is not None and report["source"]["audit"] is None
    assert report["spectral_incumbent"] == report["source"]
    assert report["source_calls"]["audit"] == 0 and not report["routes"]
    assert len(calls) == 6 and all(call["ids"] for call in calls)


def test_approved_manifest_roundtrip_and_material_tamper_fail_closed(monkeypatch):
    for key in harness.THREAD_VARIABLES:
        monkeypatch.setenv(key, "1")
    plan = harness.make_plan()
    plan["source"]["clean"] = True
    plan["manifest_sha256"] = harness.sha256_json({k: v for k, v in plan.items() if k != "manifest_sha256"})
    monkeypatch.setattr(harness, "make_plan", lambda: deepcopy(plan))
    loaded = json.loads(json.dumps(plan))
    assert harness.validate_execution_plan(loaded, loaded["manifest_sha256"]) == plan
    changed = deepcopy(loaded)
    changed["cells"][0]["request"]["frozen_material_snapshot"]["materials"][0]["optical_sets"][0]["n"][0] += .01
    with pytest.raises(ValueError, match="content changed"):
        harness.validate_execution_plan(changed, changed["manifest_sha256"])
    changed["manifest_sha256"] = harness.sha256_json({k: v for k, v in changed.items() if k != "manifest_sha256"})
    with pytest.raises(ValueError, match="cells changed"):
        harness.validate_execution_plan(changed, changed["manifest_sha256"])


def test_mock_kernel_operator_early_guard_does_not_vacuously_claim_audit(monkeypatch):
    install_synthetic_kernel(monkeypatch)
    fixture = harness.make_fixture("AR", "mean", 0)
    def stopped(request, source, **kwargs):
        return repair.ManufacturingRepairResult("resource_guard", source, (), None, source,
            harness.MEMORY_CAP + 1, harness.MEMORY_CAP, 0., source_validated=False)
    monkeypatch.setattr(repair, "repair_manufacturing_pair", stopped)
    report = harness.run_cell(fixture, clock=lambda: 0.)
    assert report["source_status"] == "audited"
    assert report["status"] == "resource_guard"
    assert not report["audit_complete"] and not report["routes"]


def test_outer_retained_memory_can_stop_source_before_first_kernel(monkeypatch):
    calls = install_synthetic_kernel(monkeypatch)
    fixture = harness.make_fixture("AR", "mean", 0)
    report = harness.run_cell(fixture, clock=lambda: 0., external_retained_bytes=lambda: harness.MEMORY_CAP)
    assert report["source_status"] == "not_started:resource_guard"
    assert not calls and not report["routes"]


def test_operator_external_memory_excludes_exact_known_input_owners(monkeypatch):
    install_synthetic_kernel(monkeypatch)
    fixture = harness.make_fixture("AR", "mean", 0)
    observed = []
    original = repair._owned_bytes
    def measured(*objects):
        total = original(*objects)
        if len(objects) == 4 and objects[0] is fixture:
            observed.append(("union", total))
        elif len(objects) == 2 and objects[0] is fixture.request:
            observed.append(("known_inputs", total))
        return total
    monkeypatch.setattr(repair, "_owned_bytes", measured)
    def stopped(request, source, **kwargs):
        # Source phase did not subtract its own training/audit owners.
        assert observed and all(kind == "union" for kind, _ in observed)
        outer = kwargs["external_retained_bytes"]()
        assert [kind for kind, _ in observed[-2:]] == ["union", "known_inputs"]
        assert outer == observed[-2][1] - observed[-1][1] + 1234
        assert outer >= 1234
        return repair.ManufacturingRepairResult("resource_guard", source, (), None, source,
            harness.MEMORY_CAP + 1, harness.MEMORY_CAP, 0., source_validated=False)
    monkeypatch.setattr(repair, "repair_manufacturing_pair", stopped)
    report = harness.run_cell(fixture, clock=lambda: 0., external_retained_bytes=lambda: 1234)
    assert report["error"] is None
    assert report["source_status"] == "audited" and report["status"] == "resource_guard"


def test_registered_protocol_has_no_empty_fj_topologies():
    assert harness.protocol()["requires_nonempty_repair_topologies"] is True
    for family in harness.FAMILIES:
        fixture = harness.make_fixture(family, "both", 0)
        assert fixture.design["front_layers"]
        assert all(route.design["front_layers"] for route in harness.prepared_routes(fixture))


@pytest.mark.parametrize("key", harness.THREAD_VARIABLES)
@pytest.mark.parametrize("bad", [None, "2"])
def test_execution_rejects_missing_or_wrong_preimport_thread_environment(monkeypatch, key, bad):
    for variable in harness.THREAD_VARIABLES:
        monkeypatch.setenv(variable, "1")
    if bad is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad)
    plan: dict[str, object] = {"source": {"clean": True}}
    plan["manifest_sha256"] = harness.sha256_json(plan)
    monkeypatch.setattr(harness, "make_plan", lambda: pytest.fail("Bad thread policy reached fixture preparation"))
    with pytest.raises(ValueError, match=key):
        harness.validate_execution_plan(plan, plan["manifest_sha256"])


def test_thread_policy_is_frozen_environment_not_measured_thread_count():
    harness.validate_thread_environment(dict.fromkeys(harness.THREAD_VARIABLES, "1"))
    policy = harness.protocol()["thread_policy"]
    assert policy["required_preimport_environment"] == dict.fromkeys(harness.THREAD_VARIABLES, "1")
    assert policy["verification"] == "frozen_environment_only_not_independently_measured_blas_threads"
