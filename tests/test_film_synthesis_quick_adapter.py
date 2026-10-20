"""Frozen synthesis UI adaptation without changing the numerical protocol."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_optimization_preview import validate_optimization_preview_handoff
from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
from coat_sim.ui.film_synthesis_quick_adapter import (
    SYNTHESIS_DEFAULT_OPTIONS, build_synthesis_request, synthesis_base_request,
    synthesis_options, synthesis_preview_handoff, synthesis_result_view, verify_synthesis_file_references,
)
from tests.test_film_auto_synthesis import setup_problem


def test_default_n0_and_explicit_seed_are_detached_and_keep_real_objectives():
    _service, request = setup_problem(low=550, high=550, samples=1)
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [{"id": "current", "material_id": "low", "optical_set_id": "nk",
                               "thickness": {"value": 112., "unit": "nm"}, "note": ""}]
    base = FilmOptimizationRequest(design, 41, (), request.objectives)
    bare = build_synthesis_request(base, request.candidate_materials, {})
    seeded = build_synthesis_request(base, request.candidate_materials, {}, True)
    assert bare.environment["front_layers"] == [] and bare.seed_designs == ()
    assert seeded.seed_designs[0]["front_layers"] == design["front_layers"]
    assert bare.objectives == base.objectives and bare.design_revision == 41
    assert all(getattr(bare, key) is None for key in SYNTHESIS_DEFAULT_OPTIONS)
    design["front_layers"][0]["thickness"]["value"] = 999.
    assert seeded.seed_designs[0]["front_layers"][0]["thickness"]["value"] == 112.


@pytest.mark.parametrize("values", [{"max_cycles": True}, {"max_layers": "1.2"},
    {"max_evaluations": 0}, {"max_layer_thickness_nm": "nan"}, {"unknown": 1}])
def test_nullable_options_reject_ambiguous_limits(values):
    with pytest.raises((ValueError, TypeError)):
        synthesis_options(values)


def test_blank_options_never_inherit_legacy_finite_budgets():
    assert synthesis_options({key: "" for key in SYNTHESIS_DEFAULT_OPTIONS}) == SYNTHESIS_DEFAULT_OPTIONS
    assert synthesis_options({"max_cycles": "12", "minimum_nonzero_thickness_nm": "0.5"})["max_cycles"] == 12


def test_real_audited_result_cancel_facts_and_frozen_preview(tmp_path):
    service, request = setup_problem(upper=.001, low=550, high=550, samples=1, materials=("low",))
    request = freeze_film_synthesis_request(service, replace(request, max_cycles=1, max_evaluations=12))
    result = synthesize_film_design(None, request)
    assert result.state == "completed" and result.targets_met
    assert result.candidate_design is not None
    cancelled = replace(result, state="cancelled", targets_met=False, termination_reason="cancelled")
    raw = film_synthesis_result_to_payload("adapter-test", cancelled)
    view = synthesis_result_view(raw)
    assert view.payload == raw and view.payload is not raw
    assert view.payload["best_candidate_audit"]["targets_met"] is True
    assert view.optimization_result is not None
    assert not view.optimization_result.targets_met
    assert view.optimization_result.validation_targets_met is True
    proxy = synthesis_base_request(request)
    handoff = synthesis_preview_handoff(proxy, view.optimization_result)
    verify_synthesis_file_references(service, proxy, result.candidate_design)
    # The original live catalog can change after admission; preview stays frozen.
    service.materials.records["low"]["optical_sets"][0]["n"] = [9., 9., 9.]
    with pytest.raises(ValueError, match="low/nk"):
        verify_synthesis_file_references(service, proxy, result.candidate_design)
    preview = validate_optimization_preview_handoff(handoff)
    assert preview["design"]["front_layers"] == result.candidate_design["front_layers"]
    assert preview["optimization_summary"]["targets_met"] is True
    assert preview["optimization_summary"]["synthesis_task_state"] == "cancelled"
    assert preview["optimization_summary"]["best_candidate_audit_targets_met"] is True
    frozen = preview["frozen_material_snapshot"]
    assert next(r for r in frozen["materials"] if r["id"] == "low")["optical_sets"][0]["n"][0] < 2
    assert view.payload["review"]["per_objective"][0]["targets_met"] is True
    print({"state": raw["result"]["state"], "candidate_audit": raw["best_candidate_audit"],
           "evaluations": result.evaluations, "audit_calls": result.audit_physical_evaluations})


def test_typed_adapter_rejects_untyped_results():
    with pytest.raises(ValueError):
        synthesis_result_view({"state": "completed"})
