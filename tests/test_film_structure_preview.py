"""Structure-to-preview boundary tests; all optical records stay in memory."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
from typing import get_type_hints

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import benchmark_film_structure as benchmark

from coat_sim.core.film_optimization import FilmOptimizationResult
from coat_sim.core.film_design import validate_design
from coat_sim.core.film_optimization_preview import (
    create_optimization_candidate_handoff, create_optimization_preview_handoff,
    validate_optimization_preview_handoff,
)
from coat_sim.core.film_optimization_structure import (
    FilmStructureCounters, FilmStructureOptimizationRequest,
    FilmStructureOptimizationResult, freeze_film_structure_optimization_request,
)


def _fixture(*, state="completed", validation=True, verified=True):
    protocol = benchmark.load_protocol()
    service = benchmark.make_memory_service(protocol)
    case = protocol["cases"][0]
    design = benchmark.make_initial_design(service, case)
    base = replace(benchmark._base_request(service, design, case, protocol), solver="baseline")
    request = freeze_film_structure_optimization_request(service, FilmStructureOptimizationRequest(
        base_request=base, candidate_materials=benchmark._candidate_material_references(),
        min_thickness_nm=20.0, max_thickness_nm=350.0,
        max_total_thickness_nm=2800.0, max_total_layers=16,
    ))
    candidate = deepcopy(design)
    candidate["front_layers"].append({
        "id": "structure-needle-preview", "material_id": "structure_bench_H",
        "optical_set_id": "H_const", "thickness": {"unit": "nm", "value": 40.0},
    })
    base_result = FilmOptimizationResult(
        state=state, design_revision=base.design_revision, best_design=candidate,
        best_thickness_nm={candidate["front_layers"][0]["id"]: 100.0,
                           "structure-needle-preview": 40.0},
        max_normalized_error=0.2, weighted_max_normalized_error=0.2,
        weighted_rms_error=0.1, targets_met=False, per_condition_errors=(),
        iterations=1, evaluations=2, validation_complete=validation,
    )
    result = FilmStructureOptimizationResult(
        base_result=base_result, mode=request.mode, trace=(), counters=FilmStructureCounters(),
        topology_audit={"split_groups": {}}, frozen_request=request,
        added_needle_layer_ids=("structure-needle-preview",), candidate_verified=verified,
    )
    return request, result


def test_explicit_frozen_new_material_can_form_an_immutable_preview():
    request, result = _fixture()
    original = deepcopy(request.base_request.design_snapshot)
    frozen = deepcopy(request.base_request.frozen_material_snapshot)
    original_candidate = deepcopy(result.best_design)
    handoff = create_optimization_preview_handoff(request, result)
    replay = validate_optimization_preview_handoff(handoff)
    assert replay["design"]["front_layers"][-1]["material_id"] == "structure_bench_H"
    assert replay["optimization_summary"]["structure_mode"] == "combined"
    assert replay["frozen_material_snapshot"]["design"] == validate_design(deepcopy(result.best_design))
    assert result.best_design == original_candidate
    assert request.base_request.design_snapshot == original
    assert request.base_request.frozen_material_snapshot == frozen
    handoff["design"]["front_layers"][-1]["thickness"]["value"] = 99.0
    assert result.best_design["front_layers"][-1]["thickness"]["value"] == 40.0


@pytest.mark.parametrize("state", ["cancelled", "failed"])
def test_verified_incomplete_candidate_is_only_user_requested_preview(state):
    request, result = _fixture(state=state, validation=False)
    with pytest.raises(ValueError, match="validation"):
        create_optimization_preview_handoff(request, result)
    handoff = create_optimization_candidate_handoff(request, result)
    assert handoff["optimization_summary"]["validation_complete"] is False
    assert handoff["optimization_summary"]["structure_state"] == state
    assert validate_optimization_preview_handoff(handoff)["optimization_summary"]["state"] == "best_candidate"


@pytest.mark.parametrize("create", [create_optimization_candidate_handoff,
                                   create_optimization_preview_handoff])
def test_unscored_candidate_cannot_be_previewed(create):
    request, result = _fixture(verified=False)
    with pytest.raises(ValueError, match="verified"):
        create(request, result)


@pytest.mark.parametrize("verified", [1, "yes"])
def test_preview_verification_flag_requires_an_actual_boolean(verified):
    request, result = _fixture(verified=verified)
    with pytest.raises(ValueError, match="verified"):
        create_optimization_candidate_handoff(request, result)


def test_historical_candidate_cannot_create_an_automatic_preview():
    request, result = _fixture()
    result = replace(result, is_final=False)
    with pytest.raises(ValueError, match="final"):
        create_optimization_preview_handoff(request, result)
    assert create_optimization_candidate_handoff(request, result)["optimization_summary"]["state"] == "best_candidate"


def test_public_preview_signatures_resolve_the_structure_types():
    for function in (create_optimization_candidate_handoff, create_optimization_preview_handoff):
        hints = get_type_hints(function)
        assert FilmStructureOptimizationRequest in hints["request"].__args__
        assert FilmStructureOptimizationResult in hints["result"].__args__


def test_preview_rejects_material_outside_explicit_frozen_candidates():
    request, result = _fixture()
    bad = deepcopy(result.best_design)
    bad["front_layers"][-1]["material_id"] = "not-frozen"
    result = replace(result, base_result=replace(result.base_result, best_design=bad))
    with pytest.raises(ValueError):
        create_optimization_candidate_handoff(request, result)


def test_preview_rejects_changed_source_and_physical_limit():
    request, result = _fixture()
    with pytest.raises(ValueError, match="frozen request"):
        create_optimization_candidate_handoff(replace(request, seed=1), result)
    bad = deepcopy(result.best_design)
    bad["front_layers"][-1]["thickness"]["value"] = 400.0
    with pytest.raises(ValueError):
        create_optimization_candidate_handoff(
            request, replace(result, base_result=replace(result.base_result, best_design=bad)))


def test_legacy_fixed_topology_still_rejects_new_material():
    request, result = _fixture()
    with pytest.raises(ValueError, match="material reference"):
        create_optimization_candidate_handoff(request.base_request, result.base_result)


@pytest.mark.parametrize("host_selected", [True, False])
def test_conserving_internal_insertion_preserves_old_host_and_new_segment(host_selected):
    request, result = _fixture()
    if not host_selected:
        request = replace(request, base_request=replace(request.base_request, variables=()))
    candidate = deepcopy(result.best_design)
    left, needle = candidate["front_layers"]
    right = deepcopy(left)
    left["thickness"]["value"] = 40.0
    right["id"] = "structure-split-preview"
    right["thickness"]["value"] = 60.0
    candidate["front_layers"] = [left, needle, right]
    groups = {left["id"]: {"layer_ids": [left["id"], right["id"]],
                           "fractions": [0.4, 0.6]}}
    result = replace(result, frozen_request=request, topology_audit={"split_groups": groups},
                     base_result=replace(result.base_result, best_design=candidate))
    handoff = create_optimization_candidate_handoff(request, result)
    assert [row["id"] for row in handoff["design"]["front_layers"]] == [
        left["id"], needle["id"], right["id"]]
    assert sum(row["thickness"]["value"] for row in (left, right)) == 100.0


def test_preview_rejects_non_layer_design_changes():
    request, result = _fixture()
    candidate = deepcopy(result.best_design)
    candidate["reference_wavelength_nm"] += 1.0
    with pytest.raises(ValueError):
        create_optimization_candidate_handoff(request, replace(
            result, base_result=replace(result.base_result, best_design=candidate)))
