"""Run-owned prepared optics must preserve the public physical contract."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any, Mapping

import numpy as np
import pytest

from coat_sim.core import film_optimization_structure as structure
from coat_sim.core.film_optimization import FilmOptimizationObjective
from tests.test_film_optimization_structure import _request, _service

pytestmark = pytest.mark.algorithm


def prepared_problem():
    service, design = _service()
    service.materials._by_id["struct_film"]["optical_sets"][0]["k"] = [.015, .025]
    service.materials._by_id["struct_high"]["optical_sets"][0]["k"] = [.03, .01]
    design["front_layers"].append(dict(id="second", material_id="struct_high", optical_set_id="high20",
        thickness=dict(value=65., unit="nm"), note="prepared test"))
    base = _request(design)
    objectives = tuple(FilmOptimizationObjective(470., 680., 5, angles_deg=(0.,30.),
        polarization=pol, metric=metric, mode="range", lower_bound=.15, upper_bound=.85)
        for metric in ("R","T") for pol in ("s","p"))
    request = replace(base, base_request=replace(base.base_request, objectives=objectives))
    frozen = structure.freeze_film_structure_optimization_request(service, request)
    return frozen, deepcopy(design)


def assert_equal(actual: Any, expected: Any):
    if isinstance(expected, Mapping):
        assert set(actual) == set(expected)
        for key in expected:
            assert_equal(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            assert_equal(left, right)
    elif isinstance(expected, (float, np.floating)):
        assert actual == pytest.approx(expected, rel=1e-13, abs=1e-14)
    elif isinstance(expected, np.ndarray):
        np.testing.assert_allclose(actual, expected, rtol=1e-13, atol=1e-14)
    else:
        assert actual == expected


@pytest.mark.parametrize("mutation", ["none", "thickness", "layer_order", "variable_order", "incident", "use_k"])
def test_prepared_and_public_complete_scores_responses_and_jacobians_agree(mutation):
    request, original = prepared_problem()
    context = structure._PreparedStructureEvaluator(request)
    baseline = context.evaluate(original, variable_ids=("stable-host","second"), with_jacobian=True)
    design = deepcopy(original)
    ids: tuple[str, ...] = ("stable-host","second")
    if mutation == "thickness":
        design["front_layers"][0]["thickness"]["value"] = 113.
    elif mutation == "layer_order":
        design["front_layers"].reverse()
    elif mutation == "variable_order":
        ids = tuple(reversed(ids))
    elif mutation == "incident":
        design["incident_medium"] = dict(kind="constant", n=1.15)
    elif mutation == "use_k":
        design["use_k"] = False
    prepared = context.evaluate(design, variable_ids=ids, with_jacobian=True)
    public = structure.evaluate_frozen_structure_candidate(request, design, variable_ids=ids, with_jacobian=True)
    assert_equal(prepared, public)
    if mutation in {"thickness","layer_order","incident","use_k"}:
        assert prepared["responses"][0]["actual_values"] != baseline["responses"][0]["actual_values"]
    assert_equal(context.evaluate(original), structure.evaluate_frozen_structure_candidate(request, original))


@pytest.mark.parametrize("samples", [4, 5])
def test_exact_grid_and_derived_objective_overrides_are_not_stale(samples):
    request, design = prepared_problem()
    context = structure._PreparedStructureEvaluator(request)
    context.evaluate(design, variable_ids=("second",), with_jacobian=True)
    objectives = tuple(replace(o, wavelength_min_nm=470.000001, wavelength_max_nm=679.999999,
        wavelength_samples=samples, angles_deg=(17.,)) for o in request.base_request.objectives)
    changed = replace(request, base_request=replace(request.base_request, objectives=objectives))
    actual = context.evaluate(design, objectives=objectives, variable_ids=("second",), with_jacobian=True)
    expected = structure.evaluate_frozen_structure_candidate(changed, design, variable_ids=("second",), with_jacobian=True)
    assert_equal(actual, expected)
    assert actual["responses"][0]["wavelength_nm"] == np.linspace(470.000001,679.999999,samples).tolist()
    assert_equal(context.evaluate(design, derived_validation_grid=True),
        structure.evaluate_frozen_structure_candidate(request, design, derived_validation_grid=True))
    assert_equal(context.evaluate(design), structure.evaluate_frozen_structure_candidate(request, design))


def test_owned_snapshot_and_request_are_independent_of_external_mutation():
    request, design = prepared_problem()
    context = structure._PreparedStructureEvaluator(request)
    expected = context.evaluate(design, variable_ids=("stable-host",), with_jacobian=True)
    snapshot = request.base_request.frozen_material_snapshot
    assert snapshot is not None
    snapshot["materials"][0]["optical_sets"][0]["n"][0] += .4
    request.base_request.design_snapshot["incident_medium"]["n"] = 1.7
    # Frozen dataclasses do not freeze their nested mappings: the context must
    # own copies rather than trust subsequent caller-owned snapshot mutations.
    assert_equal(context.evaluate(design, variable_ids=("stable-host",), with_jacobian=True), expected)
    with pytest.raises(ValueError, match="digest|SHA|sha256|hash"):
        structure.evaluate_frozen_structure_candidate(request, design)
    with pytest.raises(ValueError, match="digest|SHA|sha256|hash"):
        structure._PreparedStructureEvaluator(request)


def test_public_bad_digest_never_becomes_a_prepared_trust_token():
    request, design = prepared_problem()
    frozen = deepcopy(request.base_request.frozen_material_snapshot)
    assert frozen is not None
    frozen["sha256"] = "0"*64
    invalid = replace(request, base_request=replace(request.base_request, frozen_material_snapshot=frozen))
    for make_call in (lambda:structure.evaluate_frozen_structure_candidate(invalid,design),
                      lambda:structure._PreparedStructureEvaluator(invalid)):
        with pytest.raises(ValueError, match="digest|SHA|sha256|hash"):
            make_call()


def test_private_context_cannot_be_attached_to_any_other_public_request():
    request, design = prepared_problem()
    context = structure._PreparedStructureEvaluator(request)
    for foreign in (request,deepcopy(request)):
        with pytest.raises(ValueError, match="bound private"):
            structure.evaluate_frozen_structure_candidate(foreign,design,_prepared=context)
    def foreign_during_bound(private_request, candidate, **kwargs):
        with pytest.raises(ValueError, match="bound private"):
            structure.evaluate_frozen_structure_candidate(deepcopy(private_request),candidate,**kwargs)
        return structure.evaluate_frozen_structure_candidate(private_request,candidate,**kwargs)
    assert_equal(context.evaluate(design,evaluator=foreign_during_bound),
                 structure.evaluate_frozen_structure_candidate(request,design))
    invalid_context: Any = {}  # Deliberately malformed caller input, never a trusted token.
    with pytest.raises(ValueError, match="invalid private"):
        structure.evaluate_frozen_structure_candidate(request,design,_prepared=invalid_context)


def test_context_reentrancy_is_rejected_and_failure_does_not_poison_next_use():
    request, design = prepared_problem()
    context = structure._PreparedStructureEvaluator(request)
    class Stop(Exception):
        pass
    def recursive(private_request,candidate,**kwargs):
        with pytest.raises(RuntimeError, match="concurrently"):
            context.evaluate(candidate)
        raise Stop
    with pytest.raises(Stop):
        context.evaluate(design,evaluator=recursive)
    assert_equal(context.evaluate(design),structure.evaluate_frozen_structure_candidate(request,design))


def test_warm_material_grid_cache_still_observes_cancellation():
    request, design = prepared_problem()
    context = structure._PreparedStructureEvaluator(request)
    context.evaluate(design,variable_ids=("stable-host",),with_jacobian=True)
    event = Event()
    event.set()
    with pytest.raises(structure._StructureCancelled):
        context.evaluate(design,variable_ids=("stable-host",),with_jacobian=True,cancel_event=event)
    event.clear()
    assert_equal(context.evaluate(design,cancel_event=event),
                 structure.evaluate_frozen_structure_candidate(request,design))
