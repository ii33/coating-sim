"""Bounded restoration re-audit: real optics except marked collision injection."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
from coat_sim.core.film_synthesis_audit import AuditCandidateDominated, audit_frozen_synthesis_candidate
from coat_sim.core.film_synthesis_continuation import continue_synthesis_request
from coat_sim.core.film_optimization_structure import _StructureCancelled
from tests.test_film_synthesis_session_stop import unmet_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.fixture
def audited_problem():
    service, request = unmet_problem(acceptable_normalized_error=1e-8)
    frozen = freeze_film_synthesis_request(service, request)
    design = deepcopy(frozen.seed_designs[0])
    design['front_layers'][0]['thickness']['value'] = 10.
    return frozen, design


def test_fresh_dense_witness_rejects_but_full_audit_confirms_bound(audited_problem):
    request, design = audited_problem
    events = []
    with pytest.raises(AuditCandidateDominated) as exc:
        audit_frozen_synthesis_candidate(request, design, dense_max_error_limit=0.,
            progress_callback=lambda item: events.append(dict(item)))
    assert exc.value.dense_max_error > 0
    assert {item['stage'] for item in events} == {'audit_dense'}
    assert events[-1]['physical_evaluations'] == 1 and events[-1]['wavelength_points'] > 0
    full = audit_frozen_synthesis_candidate(request, design)
    assert full['score']['max_error'] >= exc.value.dense_max_error
    assert full['audit']['physical_evaluations'] > 1


@pytest.mark.parametrize('difference', [0., 1e-10])
def test_tie_and_guarded_near_tie_get_full_audit(audited_problem, difference):
    request, design = audited_problem
    with pytest.raises(AuditCandidateDominated) as exc:
        audit_frozen_synthesis_candidate(request, design, dense_max_error_limit=0.)
    dense = exc.value.dense_max_error
    full = audit_frozen_synthesis_candidate(request, design)
    compared = audit_frozen_synthesis_candidate(request, design,
        dense_max_error_limit=dense - difference)
    assert compared == full
    assert compared['audit']['offset_points'] > 0


def test_mean_target_cannot_use_dense_rejection(audited_problem):
    request, design = audited_problem
    first = request.objectives[0]
    mean = replace(first.mean, enabled=True, lower_bound=0., upper_bound=1.)
    request = replace(request, objectives=(replace(first, mean=mean),))
    with pytest.raises(ValueError, match='pointwise-only'):
        audit_frozen_synthesis_candidate(request, design, dense_max_error_limit=0.)
    assert audit_frozen_synthesis_candidate(request, design)['audit']['offset_points'] > 0


def test_cancel_at_dense_progress_wins_over_screen(audited_problem):
    request, design = audited_problem
    cancel = Event()
    def progress(item):
        if item['stage'] == 'audit_dense' and item['physical_evaluations'] == 1:
            cancel.set()
    with pytest.raises(_StructureCancelled):
        audit_frozen_synthesis_candidate(request, design, dense_max_error_limit=0.,
            cancel_event=cancel, progress_callback=progress)


def test_limited_restoration_is_not_reported_as_completed():
    service, request = unmet_problem(acceptable_normalized_error=1e-8)
    first = synthesize_film_design(service, request)
    assert first.candidate_design is not None and first.search_checkpoint is not None
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        first.search_checkpoint, current_design=first.candidate_design, max_evaluations=2))
    assert result.state != 'completed' and not result.targets_met
    assert result.termination_reason == 'evaluation_limit' and result.evaluations <= 2
    assert result.search_checkpoint is not None


@pytest.mark.parametrize('dense_value,scalar_value', [(0.9, 0.9 - 1e-12), (0.9, 0.9 + 1e-12)])
def test_duplicate_dense_and_extremum_keep_dense_witness_and_matching_coverage(
        audited_problem, monkeypatch, dense_value, scalar_value):
    """Artificial transform forces repeated lambda to expose merge semantics."""
    from importlib import import_module
    optimize = import_module('scipy.optimize')
    from coat_sim.core.film_objective_criteria import reduce_objective_responses
    from coat_sim.core.film_optimization_scoring import reduction_score

    request, design = audited_problem
    objective = request.objectives[0]
    center = (objective.wavelength_min_nm + objective.wavelength_max_nm) / 2
    def force_center(fun, *, method, bounds, options):
        value = fun(.5)
        return SimpleNamespace(success=True, fun=value)
    monkeypatch.setattr(optimize, 'minimize_scalar', force_center)
    def transform(objectives, value, index):
        value = deepcopy(value)
        for row in value['responses']:
            waves = np.asarray(row['wavelength_nm'])
            actual = np.full(len(waves), .1)
            if index is None:
                actual[waves == center] = dense_value
            elif len(waves) == 1 and waves[0] == center:
                actual[0] = scalar_value
            row['actual_values'] = actual
            row['extremum_values'] = actual
        value['score'] = reduction_score(reduce_objective_responses(objectives, value['responses']),
                                         request.acceptable_normalized_error)
        return value
    result = audit_frozen_synthesis_candidate(request, design, response_transform=transform)
    errors = [e for e in result['score']['errors'] if e.wavelength_nm == center]
    assert len(errors) == 1
    expected = dense_value  # Canonical dense coverage owns an exact collision.
    assert errors[0].actual == expected
    coverage = result['score']['audit_coverage'][0]
    actual = dict(zip(coverage.wavelength_nm, coverage.actual_values, strict=True))
    assert actual[center] == expected
    assert result['score']['max_error'] >= errors[0].normalized_violation
    from coat_sim.core.film_nominal_acceptance import complete_original_score
    assert complete_original_score(request, result['score'], independent_audit=True)


def test_only_restored_losers_are_screened_and_counted(monkeypatch):
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    from coat_sim.core.film_synthesis_continuation import seal_search_state

    service, request = unmet_problem(acceptable_normalized_error=1e-8)
    first = synthesize_film_design(service, request)
    assert first.candidate_design is not None and first.search_checkpoint is not None
    loser = deepcopy(first.candidate_design)
    loser['front_layers'][0]['thickness']['value'] = 10.
    state = deepcopy(dict(first.search_checkpoint))
    state['pool'] = [dict(design=loser, provenance=dict(family='controlled_old'),
                          parent_hash=None, operator='evaluated', insertion_index=1)]
    state['pending'] = []
    state = seal_search_state(state)
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        state, current_design=first.candidate_design, max_evaluations=4))
    records = result.performance['search']['restoration_audit_screen']
    assert len(records) == 1 and records[0]['validation_complete'] is False
    assert records[0]['dense_max_error'] > records[0]['incumbent_max_error']
    assert result.evaluations == 4 and result.failed_evaluations == 0
    assert result.validation_evaluations == 2 and result.audit_physical_evaluations >= 2
    assert result.candidate_design is not None
    assert physical_design_hash(result.candidate_design) == physical_design_hash(first.candidate_design)
    assert not any(row.stage == 'validation_complete' and row.physical_hash == physical_design_hash(loser)
                   for row in result.trace)


@pytest.mark.parametrize('scalar_delta', [-1e-12, 1e-12])
def test_repeated_auxiliary_point_retains_worse_and_matches_coverage(audited_problem, monkeypatch, scalar_delta):
    from importlib import import_module
    optimize = import_module('scipy.optimize')
    from coat_sim.core.film_nominal_acceptance import complete_original_score
    from coat_sim.core.film_objective_criteria import reduce_objective_responses
    from coat_sim.core.film_optimization_scoring import reduction_score

    request, design = audited_problem
    objective = request.objectives[0]
    count = ((objective.wavelength_samples - 1) * request.validation_samples_multiplier + 1)
    step = (objective.wavelength_max_nm - objective.wavelength_min_nm) / (count - 1)
    target = objective.wavelength_min_nm + step / 2
    def force_midpoint(fun, *, method, bounds, options):
        return SimpleNamespace(success=True, fun=fun(.5))
    monkeypatch.setattr(optimize, 'minimize_scalar', force_midpoint)
    def transform(objectives, value, index):
        value = deepcopy(value)
        for row in value['responses']:
            waves = np.asarray(row['wavelength_nm'])
            actual = np.full(len(waves), .1)
            if index is not None:
                actual[waves == target] = .9 + (scalar_delta if len(waves) == 1 else 0.)
            row['actual_values'] = actual
            row['extremum_values'] = actual
        value['score'] = reduction_score(reduce_objective_responses(objectives, value['responses']),
                                         request.acceptable_normalized_error)
        return value
    result = audit_frozen_synthesis_candidate(request, design, response_transform=transform)
    error = next(e for e in result['score']['errors'] if e.wavelength_nm == target)
    expected = max(.9, .9 + scalar_delta)
    coverage = result['score']['audit_coverage'][0]
    assert error.actual == expected
    assert dict(zip(coverage.wavelength_nm, coverage.actual_values, strict=True))[target] == expected
    assert complete_original_score(request, result['score'], independent_audit=True)
