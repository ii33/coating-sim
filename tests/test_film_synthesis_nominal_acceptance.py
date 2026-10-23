"""Nominal physical bounds are not the solver's numerical tolerance."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import synthesize_film_design
from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


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
    assert request.objectives[0].upper_bound == .0395


def test_explicit_strict_upper_bound_exact_equality_and_neighbours():
    from coat_sim.core.film_nominal_acceptance import nominal_targets_pass
    from coat_sim.core.film_optimization import FilmConditionError
    service, request = setup_problem(upper=.03, low=550, high=550, samples=1)
    request = replace(request, strict_upper_bound_objective_indices=(0,))
    def score(actual):
        violation = max(0., actual - .03)
        return {"max_error": violation, "errors": (FilmConditionError(
            0, 550., 0., "unpolarized", "R", actual, None, None, .03,
            violation, violation),)}
    assert not nominal_targets_pass(request, score(.03))
    assert nominal_targets_pass(request, score(float(np.nextafter(.03, -np.inf))))
    assert not nominal_targets_pass(request, score(float(np.nextafter(.03, np.inf))))
    assert nominal_targets_pass(replace(request, strict_upper_bound_objective_indices=()), score(.03))


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
def test_legacy_history_is_read_only_replayed_and_relabels_old_tolerance(upper):
    from coat_sim.core.film_synthesis_jobs import (is_synthesis_result,
        normalize_legacy_synthesis_result)
    service, request = setup_problem(upper=upper)
    result = synthesize_film_design(service, replace(request, search_strategy="hybrid",
        max_evaluations=3, max_cycles=1, max_seconds=2))
    legacy = film_synthesis_result_to_payload("legacy-replay", result)
    legacy.pop("acceptance_semantics")
    legacy["result"].pop("solver_acceptable")
    legacy["result"]["frozen_request"].pop("strict_upper_bound_objective_indices")
    legacy["result"]["targets_met"] = True
    legacy["result"]["state"] = "completed"
    legacy["result"]["termination_reason"] = "validated_targets_met"
    original = deepcopy(legacy)
    current = normalize_legacy_synthesis_result(legacy)
    assert legacy == original
    assert is_synthesis_result(current)
    assert current["legacy_compatibility"]["read_only"] is True
    assert current["legacy_compatibility"]["physical_replay_performed"] is True
    assert current["result"]["targets_met"] is (upper == .05)
    assert current["result"]["solver_acceptable"] is True
    assert current["result"]["state"] == ("completed" if upper == .05 else "not_found")


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
    from coat_sim.core.film_optimization import FilmConditionError
    _, request = setup_problem(upper=.03, low=550, high=550, samples=1)
    request = replace(request, objectives=(replace(request.objectives[0], normalization_scale=1e308),))
    actual = float(np.nextafter(.03, np.inf))
    normalized = (actual - .03) / 1e308
    assert normalized == 0.
    score = {"max_error": normalized, "errors": (FilmConditionError(
        0, 550., 0., "unpolarized", "R", actual, None, None, .03, normalized, normalized),)}
    assert not nominal_targets_pass(request, score)
    assert solver_acceptable(request, score)


def test_full_original_training_and_independent_audit_are_both_required(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit
    service, request = setup_problem(upper=.0395, low=550, high=550, samples=1)
    original = audit.audit_frozen_synthesis_candidate
    def audit_inside_bound(*args, **kwargs):
        value = original(*args, **kwargs)
        value["score"]["errors"] = tuple(replace(row, actual=.039,
            normalized_violation=0., weighted_normalized_violation=0.) for row in value["score"]["errors"])
        value["score"]["max_error"] = 0.
        return value
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
def test_strict_upper_indices_validate_without_weakening_optimization_schema(indices):
    from coat_sim.core.film_auto_synthesis import _validated
    _, request = setup_problem(upper=.03)
    with pytest.raises(ValueError, match="strict_upper"):
        _validated(replace(request, strict_upper_bound_objective_indices=indices))


def test_strict_upper_request_roundtrip_and_legacy_default():
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_io import (film_synthesis_request_to_payload,
        film_synthesis_request_from_payload)
    service, request = setup_problem(upper=.03)
    frozen = freeze_film_synthesis_request(service, replace(request, strict_upper_bound_objective_indices=(0,)))
    payload = film_synthesis_request_to_payload("strict-upper", frozen)
    assert film_synthesis_request_from_payload(payload)[1].strict_upper_bound_objective_indices == (0,)
    payload["request"].pop("strict_upper_bound_objective_indices")
    assert film_synthesis_request_from_payload(payload)[1].strict_upper_bound_objective_indices == ()


def test_audit_strict_upper_equality_is_not_nominal_even_if_raw_violation_zero():
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    service, request = setup_problem(upper=.05, low=550, high=550, samples=1)
    frozen = freeze_film_synthesis_request(service, request)
    actual = audit_frozen_synthesis_candidate(frozen, frozen.environment)["score"]["errors"][0].actual
    strict = replace(frozen, objectives=(replace(frozen.objectives[0], upper_bound=actual),),
        strict_upper_bound_objective_indices=(0,), max_evaluations=2)
    result = synthesize_film_design(None, strict)
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score["max_error"] == result.validation_score["max_error"] == 0.
    assert not result.targets_met
    assert result.solver_acceptable is False
    assert result.validation_score["audit"]["objective_status"][0]["targets_met"] is False
    inclusive = synthesize_film_design(None, replace(strict, strict_upper_bound_objective_indices=()))
    assert inclusive.targets_met and inclusive.solver_acceptable


@pytest.mark.parametrize("field", ["strict_upper_bound_objective_indices", "strict_lower_bound_objective_indices", "solver_acceptable"])
def test_ordinary_optimization_v1_still_rejects_synthesis_only_fields(field):
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_optimization_job import serialize_request, deserialize_request
    _, request = setup_problem(upper=.03)
    ordinary = FilmOptimizationRequest(request.environment, 0, (), request.objectives)
    payload = serialize_request(ordinary)
    deserialize_request(payload)
    payload[field] = [0] if field.endswith("indices") else True
    with pytest.raises(ValueError):
        deserialize_request(payload)


def test_nominal_subtolerance_offgrid_failure_is_retained_as_private_feedback():
    from coat_sim.core.film_synthesis_feedback import SparseFeedbackRegistry
    from coat_sim.core.film_optimization import FilmConditionError
    _, request = setup_problem(upper=.03)
    registry = SparseFeedbackRegistry(request)
    actual = .0305
    violation = actual - .03
    error = FilmConditionError(0, 523.75, 0., "unpolarized", "R", actual,
                              None, None, .03, violation, violation)
    assert violation < request.acceptable_normalized_error
    assert registry.record_audit({"errors": (error,)}) == 1
    assert registry.snapshot().points[0].wavelength_nm == 523.75


def test_audited_champion_requires_nominal_training_as_well_as_audit(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit
    service, request = setup_problem(upper=.0395, low=550, high=550, samples=1)
    original = audit.audit_frozen_synthesis_candidate
    def audit_inside_bound(*args, **kwargs):
        value = original(*args, **kwargs)
        value["score"]["errors"] = tuple(replace(row, actual=.039,
            normalized_violation=0., weighted_normalized_violation=0.) for row in value["score"]["errors"])
        value["score"]["max_error"] = 0.
        return value
    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", audit_inside_bound)
    result = synthesize_film_design(service, replace(request, search_strategy="hybrid",
        max_evaluations=10, max_cycles=1))
    # A zero-layer candidate that passes only the synthetic audit must not beat
    # the real one-layer candidate satisfying BOTH original evidence sets.
    assert result.targets_met and result.state == "completed"
    assert result.candidate_design is not None and result.training_score is not None
    assert len(result.candidate_design["front_layers"]) == 1
    assert result.training_score["max_error"] == 0.
