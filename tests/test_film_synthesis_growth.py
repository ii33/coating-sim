"""Physical regressions for zero-layer growth and arbitrary starting sequences."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any, Mapping

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def layer(identifier, material, thickness, unit="nm"):
    return dict(id=identifier, material_id=material, optical_set_id="nk",
                thickness=dict(value=thickness, unit=unit), note="physical test fixture")


def seed(request, rows):
    design = deepcopy(request.environment)
    design["front_layers"] = rows
    return design


def report(name, result):
    print(dict(case=name, state=result.state, reason=result.termination_reason,
               max_error=None if result.validation_score is None else result.validation_score["max_error"],
               logical_evaluations=result.evaluations, audit_physical_evaluations=result.audit_physical_evaluations,
               audit_wavelength_points=result.audit_wavelength_points, cycles=result.cycles))


def test_zero_layer_stationary_power_gets_a_legal_finite_trial_below_quarter_wave():
    service, request = setup_problem(upper=.02, low=550, high=550, samples=1, materials=("low",))
    request = replace(request, max_layer_thickness_nm=70., max_total_thickness_nm=70.,
                      growth_quarter_wave_fractions=(1.,), max_layers=1)
    result = synthesis.synthesize_film_design(service, request)
    report("bounded_zero_layer", result)
    assert result.targets_met, result.termination_reason
    probes = [t for t in result.trace if t.stage == "needle_probe"]
    assert probes
    assert probes[0].sensitivity is not None
    assert abs(probes[0].sensitivity) < 1e-15
    assert any(t.stage == "zero_layer_trial" for t in result.trace)
    assert result.candidate_design is not None
    rows = result.candidate_design["front_layers"]
    assert len(rows) == 1 and rows[0]["thickness"]["value"] == pytest.approx(70.)
    direct = simulate_frozen(".", service.freeze_design(result.candidate_design,
        dict(wavelength_min_nm=550., wavelength_max_nm=550., samples=1)))
    print(dict(case="bounded_zero_layer_direct", R=direct["R"], T=direct["T"], A=direct["A"]))
    assert direct["R"][0] <= .02


def test_arbitrary_seven_layer_seed_normalizes_units_and_freezes_seed_only_material():
    service, request = setup_problem(materials=("low",))
    extra = deepcopy(service.materials.records["high"])
    extra["id"] = "seed-only"
    service.materials.records["seed-only"] = extra
    rows = [layer("arbitrary-0", "low", 1., "qw"), layer("arbitrary-1", "seed-only", .08, "um")]
    rows.extend(layer(f"arbitrary-{i}", "high" if i % 2 else "low", 41. + i) for i in range(2, 7))
    original = seed(request, rows)
    request = replace(request, seed_designs=(original,))
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    original["front_layers"][0]["thickness"]["value"] = 99.
    assert len(frozen.seed_designs[0]["front_layers"]) == 7
    assert all(r["thickness"]["unit"] == "nm" for r in frozen.seed_designs[0]["front_layers"])
    assert frozen.seed_designs[0]["front_layers"][0]["thickness"]["value"] == pytest.approx(550 / (4 * np.sqrt(1.5)))
    assert frozen.seed_designs[0]["front_layers"][1]["thickness"]["value"] == 80.
    assert frozen.frozen_material_snapshot is not None
    assert "seed-only" in {r["id"] for r in frozen.frozen_material_snapshot["materials"]}
    service.materials.records.clear()
    replay = synthesis.freeze_film_synthesis_request(service, frozen)
    assert replay.seed_designs == frozen.seed_designs
    value = synthesis.evaluate_frozen_structure_candidate(synthesis._proxy(replay), replay.seed_designs[0])
    print(dict(case="seven_layer_seed", layers=7, max_error=value["score"]["max_error"],
               physical_evaluations=1, wavelength_points=5, frozen_replay=True))
    assert np.isfinite(value["score"]["max_error"])


def test_seed_replacement_is_tried_before_deleting_a_worse_initial_layer():
    service, request = setup_problem(upper=.001, low=550, high=550, samples=1, materials=("low",))
    start = seed(request, [layer("replace-me", "high", 550 / (4 * np.sqrt(1.5)))])
    result = synthesis.synthesize_film_design(service, replace(request, seed_designs=(start,), max_layers=1))
    report("seed_replacement", result)
    assert result.targets_met
    assert result.candidate_design is not None
    assert result.candidate_design["front_layers"][0]["material_id"] == "low"
    assert any(t.stage == "material_replacement" for t in result.trace)
    assert result.cycles == 0


@pytest.mark.parametrize("bad", ["environment", "maximum", "minimum", "zero", "unknown_k", "missing_material"])
def test_seed_validation_rejects_bad_physics_or_constraints(bad):
    service, request = setup_problem(materials=("low",))
    start = seed(request, [layer("seed", "high", 100.)])
    if bad == "environment":
        start["incident_medium"]["n"] = 1.1
    elif bad == "maximum":
        request = replace(request, max_total_thickness_nm=99.)
    elif bad == "minimum":
        request = replace(request, minimum_nonzero_thickness_nm=101.)
    elif bad == "zero":
        start["front_layers"][0]["thickness"]["value"] = 0.
    elif bad == "unknown_k":
        service.materials.records["high"]["optical_sets"][0].pop("k")
    else:
        start["front_layers"][0]["material_id"] = ""
    with pytest.raises(ValueError):
        synthesis.freeze_film_synthesis_request(service, replace(request, seed_designs=(start,)))
    print(dict(case="invalid_seed_" + bad, rejected=True, physical_evaluations=0))


def test_complex_phase_cap_checks_interior_material_knot_and_both_angles():
    service, request = setup_problem(low=450., high=700., samples=2, materials=("low",))
    service.materials.records["low"]["optical_sets"][0].update(n=[1.2, 3., 1.4], k=[.01, .2, .04])
    request = replace(request, objectives=(replace(request.objectives[0], angles_deg=(0., 57.)),),
                      validation_samples_multiplier=2)
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    cap = synthesis._phase_step_limits_nm(frozen)[("low", "nk")]
    assert cap <= 550 / (8 * abs(3 + .2j)) * (1 + 1e-14)
    for wave, n, k in ((450., 1.2, .01), (550., 3., .2), (700., 1.4, .04)):
        for angle in (0., 57.):
            q = np.sqrt(complex(n, k) ** 2 - np.sin(np.radians(angle)) ** 2)
            assert abs(2 * np.pi * q * cap / wave) <= np.pi / 4 * (1 + 1e-14)
    print(dict(case="complex_phase_cap", step_cap_nm=cap, phase_bound_rad=np.pi/4,
               material_knot_nm=550., angles=[0., 57.], physical_spectrum_evaluations=0))


def test_accepted_seed_thickness_refinement_respects_physical_step_cap(monkeypatch):
    service, request = setup_problem(upper=0., low=550., high=550., samples=1, materials=("low",))
    start = seed(request, [layer("move", "low", 10.)])
    request = replace(request, seed_designs=(start,), max_layers=1, max_cycles=1, max_evaluations=100)
    recent: list[Mapping[str, Any]] = []
    accepted = []
    original = synthesis.evaluate_frozen_structure_candidate
    def evaluate(*args, **kwargs):
        value = original(*args, **kwargs)
        recent[:] = [deepcopy(value["design"])]
        return value
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", evaluate)
    def progress(p):
        if p.stage == "refinement" and p.cycle == 0:
            accepted.append(recent[0]["front_layers"][0]["thickness"]["value"])
    result = synthesis.synthesize_film_design(service, request, progress_callback=progress)
    report("phase_capped_refinement", result)
    cap = 550 / (8 * np.sqrt(1.5))
    assert accepted
    assert max(abs(b - a) for a, b in zip([10.] + accepted, accepted, strict=False)) <= cap * (1 + 1e-12)
    assert result.training_score is not None
    assert result.training_score["max_error"] < .04


def test_plateau_is_not_an_infeasibility_or_implicit_two_cycle_stop():
    service, request = setup_problem(upper=0., low=550., high=550., samples=1, materials=("high",))
    request = replace(request, max_cycles=3, max_evaluations=1000, stagnation_cycles=1,
                      growth_quarter_wave_fractions=(1.,), position_samples=0)
    result = synthesis.synthesize_film_design(service, request)
    report("plateau_continues", result)
    assert result.cycles == 3 and result.termination_reason == "cycle_limit"
    assert any(t.stage == "search_plateau" for t in result.trace)
    assert result.validation_complete and not result.targets_met


def test_audited_checkpoint_is_copied_and_cancellation_keeps_audited_best():
    service, request = setup_problem(upper=.05)
    event = Event()
    snapshots = []
    def checkpoint(design, training, validation, audit):
        snapshots.append((deepcopy(design), deepcopy(training), deepcopy(validation), deepcopy(audit)))
        design["front_layers"].append({"not": "a real layer"})
        event.set()
    result = synthesis.synthesize_film_design(service, request, cancel_event=event,
                                              snapshot_callback=checkpoint)
    report("audited_checkpoint_cancel", result)
    assert result.state == "cancelled" and result.validation_complete and not result.targets_met
    assert result.candidate_design is not None
    assert result.candidate_design["front_layers"] == []
    assert len(snapshots) == 1 and snapshots[0][3]["continuous_certificate"] is False
    assert result.audit_physical_evaluations >= 1 and result.audit_wavelength_points >= 17


def test_optical_thickness_replacement_recovers_high_order_solution_after_bad_raw_swap(monkeypatch):
    service, request = setup_problem(upper=0., low=550., high=550., samples=1, materials=("low",))
    bad = deepcopy(service.materials.records["high"])
    bad["id"] = "n13"
    bad["optical_sets"][0]["n"] = [1.3] * 3
    service.materials.records["n13"] = bad
    d = 11 * 550 / (4 * 1.3)
    start = seed(request, [layer("high-order", "n13", d)])
    request = replace(request, seed_designs=(start,), max_layers=1,
                      acceptable_normalized_error=1e-10)
    evaluated = []
    real_evaluate = synthesis.evaluate_frozen_structure_candidate

    def record_physical_evaluation(*args, **kwargs):
        value = real_evaluate(*args, **kwargs)
        evaluated.append(deepcopy(value))
        return value

    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", record_physical_evaluation)
    result = synthesis.synthesize_film_design(service, request)
    report("optical_thickness_swap", result)
    # This target is exactly R <= 0, not an approximate antireflection goal.
    # The recovered machine-small positive response is solver-acceptable,
    # but must never be mislabeled as satisfying the user's zero bound.
    assert result.state == "not_found" and not result.targets_met
    assert result.solver_acceptable is True
    assert result.validation_score is not None
    assert 0. < result.validation_score["max_error"] < 1e-20
    replacement: list[float] = []
    for trace in result.trace:
        if trace.stage == "material_replacement":
            assert trace.max_normalized_error is not None
            replacement.append(trace.max_normalized_error)
    assert replacement[0] > .02 and min(replacement) < 1e-20
    assert result.candidate_design is not None
    expected_thickness = 11 * 550 / (4 * np.sqrt(1.5))
    high_order = [value for value in evaluated if len(value["design"]["front_layers"]) == 1
        and value["design"]["front_layers"][0]["material_id"] == "low"
        and value["design"]["front_layers"][0]["thickness"]["value"] == pytest.approx(expected_thickness)]
    # Without a nominal pass the search legitimately continues to a thinner
    # quarter-wave champion. Prove the original high-order replacement itself
    # was actually evaluated and recovered, instead of constraining the winner.
    assert high_order and min(value["score"]["max_error"] for value in high_order) < 1e-20


def test_training_success_audit_failure_continues_true_zero_layer_growth():
    service, request = setup_problem(upper=.05, low=450., high=700., samples=2, materials=("low",))
    service.materials.records["glass"]["optical_sets"][0]["n"] = [1.5, 3., 1.5]
    request = replace(request, max_cycles=1, max_layers=1, max_evaluations=30,
                      growth_quarter_wave_fractions=(1.,))
    result = synthesis.synthesize_film_design(service, request)
    report("audit_failure_still_grows", result)
    assert result.cycles == 1
    assert any(t.stage == "zero_layer_trial" for t in result.trace)
    assert result.audit_physical_evaluations > 1
    if result.targets_met:
        assert result.validation_score is not None
        assert result.validation_score["max_error"] <= request.acceptable_normalized_error


def test_unknown_seed_k_error_names_material_set_wavelength_and_target_domain():
    service, request = setup_problem(materials=("low",))
    service.materials.records["high"]["optical_sets"][0].pop("k")
    request = replace(request, seed_designs=(seed(request, [layer("seed", "high", 100.)]),))
    with pytest.raises(ValueError) as error:
        synthesis.freeze_film_synthesis_request(service, request)
    message = str(error.value)
    assert "material=high" in message and "optical_set=nk" in message
    assert "wavelength=520 nm" in message and "[520, 580] nm" in message
    print(dict(case="unknown_k_context", error=message, physical_evaluations=0))


def test_synthesis_order_prioritizes_worst_violation_then_manufacture_after_pass():
    def candidate(maximum, rms, count=1, total=10.):
        return dict(design=dict(front_layers=[dict(thickness=dict(value=total/count)) for _ in range(count)]),
                    score=dict(max_error=maximum, weighted_rms_error=rms))
    def rank(c):
        return synthesis._synthesis_candidate_rank(c, .001)
    assert rank(candidate(.02, 100.)) < rank(candidate(.03, 1e-20))
    assert rank(candidate(.02, .1)) < rank(candidate(.02, .2))
    assert rank(candidate(.0009, .0009, 1, 100.)) < rank(candidate(0., 0., 2, 20.))
    assert rank(candidate(.0009, .0009, 2, 50.)) < rank(candidate(0., 0., 2, 100.))
    print(dict(case="maximum_first_order_contract", physical_evaluations=0,
               worst_violation_before_weighted_rms=True, manufacture_only_after_all_pass=True))


def test_proposal_ids_reserve_all_layers_and_each_other_deterministically():
    _, request = setup_problem()
    design = seed(request, [layer("auto-2-0-0", "low", 20.),
                            layer("auto-2-0-0-1", "low", 20.),
                            layer("split-2-1-0", "high", 20.)])
    design["back_layers"] = [layer("growth-2-0-0", "high", 20.)]
    ids = synthesis._proposal_layer_ids(design, "auto-2-0-0", "split-2-1-0", "growth-2-0-0", "auto-2-0-0")
    assert ids == ("auto-2-0-0-2", "split-2-1-0-1", "growth-2-0-0-1", "auto-2-0-0-3")
    assert ids == synthesis._proposal_layer_ids(deepcopy(design), "auto-2-0-0", "split-2-1-0", "growth-2-0-0", "auto-2-0-0")
    print(dict(case="collision_id_contract", generated=ids, physical_evaluations=0))


def test_previous_generated_ids_continue_with_real_probes_growth_and_frozen_replay(monkeypatch):
    service, request = setup_problem(low=550., high=550., samples=1, materials=("low", "high"))
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=None, lower_bound=.99999)),),
                      max_cycles=2, max_evaluations=1500, position_samples=1,
                      growth_quarter_wave_fractions=(1.,))
    names = ("auto-2-0-0", "split-2-1-0", "growth-2-0-0",
             "auto-2-0-0-1", "split-2-1-0-1", "growth-2-0-0-1")
    rows = [layer(name, "low" if i % 2 == 0 else "high",
                  550 / (4 * (np.sqrt(1.5) if i % 2 == 0 else 2.2))) for i, name in enumerate(names)]
    request = synthesis.freeze_film_synthesis_request(service, replace(request, seed_designs=(seed(request, rows),)))
    original_insert = synthesis._insert_at_site
    collisions = []
    generated = []
    def checked_insert(design, site, reference, thickness, needle_id, split_id, **kwargs):
        existing = {r["id"] for side in ("front_layers", "back_layers") for r in design[side]}
        assert needle_id not in existing and split_id not in existing and needle_id != split_id
        candidate = original_insert(design, site, reference, thickness, needle_id, split_id, **kwargs)
        identifiers = [r["id"] for side in ("front_layers", "back_layers") for r in candidate[side]]
        assert len(identifiers) == len(set(identifiers))
        generated.append(needle_id)
        if needle_id.startswith("auto-2-0-0-") or split_id.startswith("split-2-1-0-") or needle_id.startswith("growth-2-0-0-"):
            collisions.append((needle_id, split_id))
        return candidate
    monkeypatch.setattr(synthesis, "_insert_at_site", checked_insert)
    first = synthesis.synthesize_film_design(service, request)
    report("generated_id_continuation", first)
    assert first.state != "failed" and first.validation_complete and first.cycles == 2
    assert any(t.stage == "needle_probe" and t.cycle == 2 for t in first.trace)
    assert any(t.stage == "growth_trial" and t.cycle == 2 for t in first.trace)
    assert collisions
    service.materials.records.clear()
    second = synthesis.synthesize_film_design(None, first.frozen_request)
    report("generated_id_frozen_replay", second)
    assert second.state == first.state and second.termination_reason == first.termination_reason
    assert second.candidate_design == first.candidate_design and second.trace == first.trace
    assert second.evaluations == first.evaluations
    print(dict(case="generated_id_collisions_resolved", proposals=len(generated), collisions=collisions))


def test_saved_seven_layer_dual_ir_seed_retains_audited_candidate_on_bounded_replay():
    # Exact seven-layer geometry from the latest acceptance artifact:
    # dual_ar_seeded_frozenhelper/...request.json. Its n/k are explicitly
    # synthetic lossless constants, not real material provenance.
    service, request = setup_problem(materials=("low", "high"))
    for material, index in (("glass", 1.5), ("low", 1.3), ("high", 2.2)):
        optical = service.materials.records[material]["optical_sets"][0]
        optical.update(wavelength_min=300., wavelength_max=14000.,
                       wavelengths=[300., 450., 550., 1000., 3000., 5000., 8000., 12000., 14000.],
                       n=[index] * 9, k=[0.] * 9)
    geometry = (("growth-10-0-0", "low", 416.029208054748),
                ("growth-3-1-0", "high", 49.488387668843245),
                ("growth-17-2-0", "low", 741.0524887949658),
                ("auto-19-13-1", "high", 29.416411512915907),
                ("split-19-13-1", "low", 288.1544964162034),
                ("growth-15-4-0", "low", 354.3583770681443),
                ("growth-11-7-0", "low", 353.37015543768706))
    objectives = tuple(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=.01), wavelength_min_nm=lo, wavelength_max_nm=hi, wavelength_samples=61) for lo, hi in ((3000., 5000.), (8000., 12000.)))
    request = replace(request, objectives=objectives, max_cycles=2, max_evaluations=400,
                      seed_designs=(seed(request, [layer(*row) for row in geometry]),))
    first = synthesis.synthesize_film_design(service, request)
    report("saved_seven_layer_ir_bounded", first)
    assert first.state != "failed" and first.validation_complete and first.failed_evaluations == 0
    assert first.candidate_design and first.audit_physical_evaluations > 0
    second = synthesis.synthesize_film_design(None, first.frozen_request)
    report("saved_seven_layer_ir_frozen_replay", second)
    assert second.state == first.state and second.candidate_design == first.candidate_design
    assert second.evaluations == first.evaluations and second.trace == first.trace


def test_actual_audited_checkpoint_restore_then_deleted_branch_regrowth_keeps_ids(tmp_path):
    import json
    import queue
    from coat_sim.film_synthesis_cli import _execute
    from coat_sim.core.film_synthesis_io import RESULT_KIND, film_synthesis_request_to_payload
    service, request = setup_problem(low=550., high=550., samples=1, materials=("low", "high"))
    names = ("growth-2-0-0", "growth-2-0-0-1", "checkpoint-old-2",
             "checkpoint-old-3", "checkpoint-old-4", "checkpoint-old-5")
    rows = [layer(name, "low" if i % 2 == 0 else "high",
                  550 / (4 * (np.sqrt(1.5) if i % 2 == 0 else 2.2))) for i, name in enumerate(names)]
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=None, lower_bound=.99999)),),
                      seed_designs=(seed(request, rows),), max_cycles=1, max_evaluations=500,
                      position_samples=1, growth_quarter_wave_fractions=(1.,))
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    checkpoint_path = tmp_path / "best-audited.json"
    first, _ = _execute("id-restore-regression", film_synthesis_request_to_payload("id-restore-regression", frozen),
                        Event(), queue.Queue(), checkpoint_path)
    record = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert record["kind"] == RESULT_KIND and record["checkpoint_state"] == "audited"
    assert record["validation_score"] and record["audit"]["continuous_certificate"] is False
    restored_design = record["design"]
    before = deepcopy(restored_design)
    assert {r["id"] for r in restored_design["front_layers"]} == set(names)
    restored = replace(first.frozen_request, seed_designs=(restored_design,), max_cycles=2)
    continued = synthesis.synthesize_film_design(None, restored)
    report("actual_checkpoint_restore", continued)
    assert continued.state != "failed" and continued.validation_complete and continued.cycles == 2
    assert restored_design == before  # Restoring never renumbers or mutates the checkpoint.
    # Simulate a real deletion, rescore it, then grow again while the old root
    # and its -1 row remain. Only the new row gets -2; retained rows keep IDs.
    deleted = deepcopy(restored_design)
    deleted["front_layers"] = [r for r in deleted["front_layers"] if r["id"] != "checkpoint-old-5"]
    proxy = synthesis._proxy(first.frozen_request)
    after_deletion = synthesis.evaluate_frozen_structure_candidate(proxy, deleted)
    retained_ids = [r["id"] for r in deleted["front_layers"]]
    generated_id, split_id = synthesis._proposal_layer_ids(deleted, "growth-2-0-0", "unused")
    assert generated_id == "growth-2-0-0-2"
    grown = synthesis._insert_at_site(deleted, dict(kind="interface", interface=0),
                                     FilmNeedleMaterialReference("high", "nk"), 20., generated_id, split_id)
    after_growth = synthesis.evaluate_frozen_structure_candidate(proxy, grown)
    assert [r["id"] for r in grown["front_layers"]][1:] == retained_ids
    assert grown["front_layers"][1:] == deleted["front_layers"]
    assert len({r["id"] for r in grown["front_layers"]}) == len(grown["front_layers"])
    print(dict(case="delete_then_regrow_preserves_identifiers", deleted_id="checkpoint-old-5",
               added_id=generated_id, retained_ids=retained_ids,
               deletion_max_error=after_deletion["score"]["max_error"],
               growth_max_error=after_growth["score"]["max_error"], physical_evaluations=2, wavelength_points=2))


def test_run_wide_ids_reserve_other_seeds_archives_and_discarded_proposals(monkeypatch):
    service, request = setup_problem(low=550., high=550., samples=1, materials=("low", "high"))
    roots = ("auto-1-0-0", "split-1-0-0", "growth-1-0-0")
    first_seed = seed(request, [layer(name, "high", 62.5) for name in roots])
    second_seed = seed(request, [layer(name + "-1", "high", 62.5) for name in roots])
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=None, lower_bound=.99999)),),
                      seed_designs=(first_seed, second_seed), max_cycles=1, max_evaluations=600,
                      position_samples=0, growth_quarter_wave_fractions=(1.,))
    initial_ids = {r["id"] for d in request.seed_designs for r in d["front_layers"]}
    original_insert = synthesis._insert_at_site
    generated = []
    def checked_insert(design, site, reference, thickness, needle_id, split_id, **kwargs):
        assert needle_id not in initial_ids and split_id not in initial_ids
        generated.append((needle_id, split_id))
        return original_insert(design, site, reference, thickness, needle_id, split_id, **kwargs)
    monkeypatch.setattr(synthesis, "_insert_at_site", checked_insert)
    result = synthesis.synthesize_film_design(service, request)
    report("run_wide_seed_id_reserve", result)
    assert result.state != "failed" and result.cycles == 1 and result.validation_complete
    assert any(ids[0] == "auto-1-0-0-2" for ids in generated)
    assert any(ids[0] == "growth-1-0-0-2" for ids in generated)
    # The allocator keeps earlier (including discarded) proposal IDs reserved
    # even when another branch does not contain those rows.
    reserved = set(initial_ids)
    unrelated = deepcopy(request.environment)
    earlier = synthesis._proposal_layer_ids(unrelated, "auto-1-0-0", "split-1-0-0", reserved_ids=reserved)
    later = synthesis._proposal_layer_ids(unrelated, "auto-1-0-0", "split-1-0-0", reserved_ids=reserved)
    assert earlier == ("auto-1-0-0-2", "split-1-0-0-2")
    assert later == ("auto-1-0-0-3", "split-1-0-0-3")
    assert unrelated == request.environment
    print(dict(case="run_wide_archive_reserve_contract", earlier=earlier, later=later,
               initial_ids=sorted(initial_ids), physical_evaluations=0))
