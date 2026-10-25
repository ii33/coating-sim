"""Nominal physical bounds are not the solver's numerical tolerance."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import synthesize_film_design
from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
from coat_sim.core.film_objective_criteria import reduce_objective_responses
from coat_sim.core.film_optimization_scoring import reduction_score
from tests.test_film_synthesis_strict_bounds import _with_strict
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _single_line_score(request, actual):
    responses = tuple(dict(objective_index=i, angle_deg=angle,
        wavelength_nm=[o.wavelength_min_nm], actual_values=[actual])
        for i, o in enumerate(request.objectives) for angle in o.angles_deg)
    reduced = reduce_objective_responses(request.objectives, responses,
        tolerance=request.acceptable_normalized_error)
    return reduction_score(reduced, request.acceptable_normalized_error)


def _set_single_line_audit_actual(request, value, actual):
    from coat_sim.core.film_synthesis_audit import objective_constraint_status
    for response in value['responses']:
        assert len(response['wavelength_nm']) == 1
        response['actual_values'] = [actual]
    reduced = reduce_objective_responses(request.objectives, value['responses'],
        tolerance=request.acceptable_normalized_error)
    value['score'].update(reduction_score(reduced, request.acceptable_normalized_error))
    value['score']['audit_coverage'] = reduced.coverage
    statuses = objective_constraint_status(request.objectives, reduced.conditions,
                                           request.acceptable_normalized_error)
    value['score']['audit']['objective_status'] = statuses
    value['audit']['objective_status'] = statuses
    return value


def test_tolerance_does_not_turn_bare_point04_into_nominal_point0395_pass():
    service, request = setup_problem(upper=.0395)
    request = replace(request, search_strategy="hybrid", max_evaluations=3,
                      max_cycles=1, max_seconds=2)
    result = synthesize_film_design(service, request)
    assert result.candidate_design is not None
    assert result.candidate_design["front_layers"] == []
    assert result.validation_complete
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score["max_error"] == pytest.approx(.0005)
    assert result.validation_score["max_error"] == pytest.approx(.0005)
    assert not result.targets_met
    assert result.state == "not_found"
    assert result.termination_reason != "validated_targets_met"
    assert result.solver_acceptable is True
    payload = film_synthesis_result_to_payload("nominal-negative", result)
    assert payload["best_candidate_audit"]["targets_met"] is False
    assert payload["best_candidate_audit"]["solver_acceptable"] is True
    assert payload["review"]["per_objective"][0]["targets_met"] is False
    assert payload["review"]["per_objective"][0]["solver_acceptable"] is True
    assert request.acceptable_normalized_error == .001
    assert request.objectives[0].pointwise.upper_bound == .0395


def test_explicit_strict_upper_bound_exact_equality_and_neighbours():
    from coat_sim.core.film_nominal_acceptance import nominal_targets_pass
    service, request = setup_problem(upper=.03, low=550, high=550, samples=1)
    request = _with_strict(request, upper=(0,))
    def score(actual):
        return _single_line_score(request, actual)

    assert not nominal_targets_pass(request, score(.03))
    assert nominal_targets_pass(request, score(float(np.nextafter(.03, -np.inf))))
    assert not nominal_targets_pass(request, score(float(np.nextafter(.03, np.inf))))
    inclusive = _with_strict(request)
    assert nominal_targets_pass(inclusive, _single_line_score(inclusive, .03))


def test_stale_tolerance_based_result_is_not_trusted_as_current_history():
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result
    service, request = setup_problem(upper=.0395)
    result = synthesize_film_design(service, replace(request, search_strategy="hybrid",
        max_evaluations=3, max_cycles=1, max_seconds=2))
    payload = film_synthesis_result_to_payload("old-result", result)
    stale = deepcopy(payload)
    stale.pop("acceptance_semantics", None)
    stale["result"]["targets_met"] = True
    stale["result"]["state"] = "completed"
    stale["best_candidate_audit"]["targets_met"] = True
    assert not is_synthesis_result(stale)


@pytest.mark.parametrize("upper", [.0395, .05])
def test_legacy_history_is_rejected_without_silently_relabeling_old_tolerance(upper):
    from coat_sim.core.film_synthesis_jobs import (is_synthesis_result,
        normalize_legacy_synthesis_result)
    service, request = setup_problem(upper=upper)
    result = synthesize_film_design(service, replace(request, search_strategy="hybrid",
        max_evaluations=3, max_cycles=1, max_seconds=2))
    legacy = film_synthesis_result_to_payload("legacy-replay", result)
    legacy.pop("acceptance_semantics")
    legacy["result"].pop("solver_acceptable")
    legacy["schema_version"] = 1
    legacy["result"]["targets_met"] = True
    legacy["result"]["state"] = "completed"
    legacy["result"]["termination_reason"] = "validated_targets_met"
    original = deepcopy(legacy)
    with pytest.raises(ValueError, match="legacy"):
        normalize_legacy_synthesis_result(legacy)
    assert legacy == original
    assert not is_synthesis_result(legacy)
    # Rejection preserves the stored old claims instead of fabricating current
    # physical evidence or a green nominal result from the old tolerance flag.
    assert legacy['result']['targets_met'] is True
    assert legacy['result']['state'] == 'completed'
    assert result.targets_met is (upper == .05)
    assert result.solver_acceptable is True
    assert result.state == ('completed' if upper == .05 else 'not_found')


@pytest.mark.parametrize("mutation", ["actual", "score", "design", "missing_grid", "wrong_target"])
def test_legacy_history_replay_rejects_altered_evidence(mutation):
    from coat_sim.core.film_synthesis_jobs import normalize_legacy_synthesis_result
    service, request = setup_problem(upper=.05)
    payload = film_synthesis_result_to_payload("legacy-tamper", synthesize_film_design(service, request))
    payload.pop("acceptance_semantics")
    if mutation == "actual":
        payload["result"]["training_score"]["errors"][0]["actual"] += .001
    elif mutation == "score":
        payload["result"]["validation_score"]["max_error"] = .002
    elif mutation == "design":
        payload["result"]["candidate_design"]["incident_medium"]["n"] = 1.1
    elif mutation == "missing_grid":
        payload["result"]["validation_score"]["errors"] = payload["result"]["validation_score"]["errors"][:-1]
    else:
        payload["result"]["training_score"]["errors"][0]["upper_bound"] = .06
    with pytest.raises(ValueError, match="legacy"):
        normalize_legacy_synthesis_result(payload)


def test_positive_physical_violation_cannot_hide_in_normalization_underflow():
    from coat_sim.core.film_nominal_acceptance import nominal_targets_pass, solver_acceptable
    _, request = setup_problem(upper=.03, low=550, high=550, samples=1)
    request = replace(request, objectives=(replace(request.objectives[0], normalization_scale=1e308),))
    actual = float(np.nextafter(.03, np.inf))
    normalized = (actual - .03) / 1e308
    assert normalized == 0.
    score = _single_line_score(request, actual)
    assert score['max_error'] == normalized

    assert not nominal_targets_pass(request, score)
    assert solver_acceptable(request, score)


def test_full_original_training_and_independent_audit_are_both_required(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit
    service, request = setup_problem(upper=.0395, low=550, high=550, samples=1)
    original = audit.audit_frozen_synthesis_candidate
    def audit_inside_bound(*args, **kwargs):
        value = original(*args, **kwargs)
        return _set_single_line_audit_actual(args[0], value, .039)
    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", audit_inside_bound)
    result = synthesize_film_design(service, replace(request, max_evaluations=2))
    assert result.validation_complete
    assert result.training_score is not None and result.validation_score is not None
    assert result.validation_score["max_error"] == 0.
    assert result.training_score["max_error"] == pytest.approx(.0005)
    assert not result.targets_met and result.solver_acceptable


def test_sparse_or_incomplete_audit_cannot_certify_nominal_success():
    from coat_sim.core.film_nominal_acceptance import nominal_targets_pass
    service, request = setup_problem(upper=.05)
    result = synthesize_film_design(service, request)
    assert nominal_targets_pass(result.frozen_request, result.training_score)
    assert nominal_targets_pass(result.frozen_request, result.validation_score, independent_audit=True)
    assert result.validation_score is not None
    sparse = deepcopy(dict(result.validation_score))
    sparse["errors"] = sparse["errors"][:1]
    assert not nominal_targets_pass(result.frozen_request, sparse, independent_audit=True)
    incomplete = deepcopy(dict(result.validation_score))
    incomplete["audit"]["completed_extrema"] += 1
    assert not nominal_targets_pass(result.frozen_request, incomplete, independent_audit=True)
    assert not nominal_targets_pass(result.frozen_request, result.training_score, independent_audit=True)


@pytest.mark.parametrize("indices", [(True,), (-1,), (1,), (0, 0), "0"])
def test_nested_strict_upper_flags_validate_without_weakening_optimization_schema(indices):
    from coat_sim.core.film_auto_synthesis import _validated
    _, request = setup_problem(upper=.03)
    with pytest.raises(ValueError, match="strict_upper"):
        _validated(replace(request, objectives=(replace(request.objectives[0],
            pointwise=replace(request.objectives[0].pointwise, strict_upper=indices)),)))


def test_strict_upper_request_roundtrip_and_legacy_rejection():
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_io import (film_synthesis_request_to_payload,
        film_synthesis_request_from_payload)
    service, request = setup_problem(upper=.03)
    frozen = freeze_film_synthesis_request(service, _with_strict(request, upper=(0,)))
    payload = film_synthesis_request_to_payload("strict-upper", frozen)
    assert payload['schema_version'] == 4
    assert film_synthesis_request_from_payload(payload)[1].objectives[0].pointwise.strict_upper is True
    payload['schema_version'] = 1
    payload['request']['strict_upper_bound_objective_indices'] = [0]
    with pytest.raises(ValueError):
        film_synthesis_request_from_payload(payload)


def test_audit_strict_upper_equality_is_not_nominal_even_if_raw_violation_zero():
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    service, request = setup_problem(upper=.05, low=550, high=550, samples=1)
    frozen = freeze_film_synthesis_request(service, request)
    actual = audit_frozen_synthesis_candidate(frozen, frozen.environment)["score"]["errors"][0].actual
    strict = replace(frozen, objectives=(replace(frozen.objectives[0], pointwise=replace(frozen.objectives[0].pointwise, upper_bound=actual, strict_upper=True)),),
        max_evaluations=2)
    result = synthesize_film_design(None, strict)
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score["max_error"] == result.validation_score["max_error"] == 0.
    assert not result.targets_met
    assert result.solver_acceptable is False
    assert result.validation_score["audit"]["objective_status"][0]["targets_met"] is False
    inclusive = synthesize_film_design(None, _with_strict(strict))
    assert inclusive.targets_met and inclusive.solver_acceptable


@pytest.mark.parametrize("field", ["strict_upper_bound_objective_indices", "strict_lower_bound_objective_indices", "solver_acceptable"])
def test_ordinary_optimization_v2_still_rejects_removed_request_fields(field):
    from coat_sim.core.film_optimization import FilmOptimizationRequest, FilmThicknessVariable
    from coat_sim.core.film_optimization_job import serialize_request, deserialize_request
    _, request = setup_problem(upper=.03)
    design = deepcopy(request.environment)
    design['front_layers'] = [dict(id='variable', material_id='low', optical_set_id='nk',
                                    thickness=dict(value=20., unit='nm'))]
    ordinary = FilmOptimizationRequest(design, 0, (FilmThicknessVariable('front', 'variable', 0., 200.),), request.objectives)
    payload = serialize_request(ordinary)
    deserialize_request(payload)
    payload[field] = [0] if field.endswith("indices") else True
    with pytest.raises(ValueError):
        deserialize_request(payload)


def test_nominal_subtolerance_offgrid_failure_is_retained_as_private_feedback():
    from coat_sim.core.film_synthesis_feedback import SparseFeedbackRegistry
    _, request = setup_problem(upper=.03)
    registry = SparseFeedbackRegistry(request)
    actual = .0305
    violation = actual - .03
    from coat_sim.core.film_optimization import FilmConditionError
    error = FilmConditionError(0, 523.75, 0., "unpolarized", "R", actual,
                              None, request.objectives[0].pointwise.lower_bound, .03, violation, violation,
                              wavelength_min_nm=520., wavelength_max_nm=580.)
    assert violation < request.acceptable_normalized_error
    assert registry.record_audit({"errors": (error,)}) == 1
    assert registry.snapshot().points[0].wavelength_nm == 523.75


def test_audited_champion_requires_nominal_training_as_well_as_audit(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit
    service, request = setup_problem(upper=.0395, low=550, high=550, samples=1)
    original = audit.audit_frozen_synthesis_candidate
    def audit_inside_bound(*args, **kwargs):
        value = original(*args, **kwargs)
        return _set_single_line_audit_actual(args[0], value, .039)
    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", audit_inside_bound)
    result = synthesize_film_design(service, replace(request, search_strategy="hybrid",
        max_evaluations=10, max_cycles=1))
    # A zero-layer candidate that passes only the synthetic audit must not beat
    # the real one-layer candidate satisfying BOTH original evidence sets.
    assert result.targets_met and result.state == "completed"
    assert result.candidate_design is not None and result.training_score is not None
    assert len(result.candidate_design["front_layers"]) == 1
    assert result.training_score["max_error"] == 0.
