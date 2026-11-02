"""Synthetic original-score evidence regressions (no physical simulator)."""
from __future__ import annotations

from collections import UserDict
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from coat_sim.core import film_nominal_acceptance as acceptance
from coat_sim.core.film_nominal_acceptance import (
    complete_original_score, nominal_condition_pass, nominal_targets_pass,
    solver_acceptable, solver_condition_acceptable,
)
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_scoring import reduction_score

pytestmark = pytest.mark.algorithm


def _synthetic_score(*, pointwise=True, strict=False, audit=True):
    """Fixed-grid dense .8; extra offsets .2. No external test data."""
    threshold = .8 if strict else .7
    objective = FilmOptimizationObjective(500., 600., 3, angles_deg=(0., 45.), weight=4.,
        mean=FilmObjectiveConstraint(lower_bound=threshold, strict_lower=strict),
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=.7))
    request = SimpleNamespace(objectives=(objective,), acceptable_normalized_error=.15,
                              validation_samples_multiplier=2)
    dense_waves = (500., 525., 550., 575., 600.) if audit else (500., 550., 600.)
    dense = tuple(dict(objective_index=0, angle_deg=angle, wavelength_nm=dense_waves,
                       actual_values=(.8,) * len(dense_waves)) for angle in objective.angles_deg)
    reduced = reduce_objective_responses(request.objectives, dense, tolerance=request.acceptable_normalized_error)
    score = reduction_score(reduced, request.acceptable_normalized_error)
    if not audit:
        return request, score
    waves = (500., 512.5, 525., 537.5, 550., 562.5, 575., 587.5, 600.)
    vals = (.8, .2, .8, .2, .8, .2, .8, .2, .8)
    extended = tuple(dict(objective_index=0, angle_deg=angle, wavelength_nm=waves,
                          actual_values=vals) for angle in objective.angles_deg)
    extra_reduction = reduce_objective_responses(request.objectives, extended,
                                                 tolerance=request.acceptable_normalized_error)
    extras = tuple(e for e in extra_reduction.conditions
                   if e.reduction == 'pointwise' and e.wavelength_nm not in dense_waves)
    errors = score['errors'] + extras
    score.update(errors=errors, audit_coverage=extra_reduction.coverage,
                 max_error=max(e.normalized_violation for e in errors),
                 weighted_max_error=max(e.weighted_normalized_violation for e in errors),
                 nominal_pass=all(nominal_condition_pass(getattr(objective, e.reduction), e.actual)
                                  for e in errors),
                 solver_acceptable=all(solver_condition_acceptable(
                     getattr(objective, e.reduction), e.actual, request.acceptable_normalized_error,
                     normalization_scale=objective.normalization_scale) for e in errors),
                 condition_count=len(errors),
                 audit=dict(dense_points=10, offset_points=8, unique_condition_points=18,
                            physical_evaluations=1, wavelength_points=18, bracket_count=0,
                            completed_extrema=0, continuous_certificate=False,
                            rms_measure='fixed_dense_grid_only'))
    return request, score


def _plain_score(*, values, pointwise=False, strict=False, tolerance=.15):
    objective = FilmOptimizationObjective(500., 600., 3,
        mean=FilmObjectiveConstraint(lower_bound=.5, strict_lower=strict),
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=.5))
    request = SimpleNamespace(objectives=(objective,), acceptable_normalized_error=tolerance,
                              validation_samples_multiplier=2)
    frames = (dict(objective_index=0, angle_deg=0., wavelength_nm=(500., 550., 600.),
                   actual_values=tuple(values)),)
    reduced = reduce_objective_responses(request.objectives, frames, tolerance=tolerance)
    return request, reduction_score(reduced, tolerance)


def _with_coverage(score, which, row):
    rows = list(score[which])
    rows[0] = row
    return dict(score, **{which: tuple(rows)})


def _with_error(score, index=0, **updates):
    rows = list(score['errors'])
    rows[index] = replace(rows[index], **updates)
    return dict(score, errors=tuple(rows))


def _cases():
    cases = {}
    for label, options in [('normal', dict(audit=False)), ('strict', dict(strict=True)),
                           ('mixed', {}), ('mean_only', dict(pointwise=False))]:
        request, score = _synthetic_score(**options)
        cases[label] = (request, score, options.get('audit', True))
    request, score, audit = cases['mixed']
    dense, extra = score['coverage'][0], score['audit_coverage'][0]
    variants = {
        'coverage_wrong_index': _with_coverage(score, 'coverage', replace(dense, objective_index=1)),
        'coverage_wrong_angle': _with_coverage(score, 'coverage', replace(dense, angle_deg=90.)),
        'coverage_duplicate_group': dict(score, coverage=score['coverage'] + (dense,)),
        'coverage_missing_group': dict(score, coverage=score['coverage'][:1]),
        'coverage_unordered': _with_coverage(score, 'coverage', replace(dense, wavelength_nm=(500., 550., 525., 575., 600.))),
        'coverage_duplicate_wave': _with_coverage(score, 'coverage', replace(dense, wavelength_nm=(500., 525., 525., 575., 600.))),
        'coverage_missing_dense': _with_coverage(score, 'coverage', replace(dense, wavelength_nm=(500., 525., 575., 600.), actual_values=(.8,) * 4)),
        'coverage_nonfinite': _with_coverage(score, 'coverage', replace(dense, actual_values=(.8, .8, float('nan'), .8, .8))),
        'audit_missing_offset': _with_coverage(score, 'audit_coverage', replace(extra, wavelength_nm=extra.wavelength_nm[:1]+extra.wavelength_nm[2:], actual_values=extra.actual_values[:1]+extra.actual_values[2:])),
        'audit_wrong_index': _with_coverage(score, 'audit_coverage', replace(extra, objective_index=1)),
        'audit_wrong_angle': _with_coverage(score, 'audit_coverage', replace(extra, angle_deg=90.)),
        'audit_duplicate_wave': _with_coverage(score, 'audit_coverage', replace(extra, wavelength_nm=(500., 512.5, 512.5) + extra.wavelength_nm[3:])),
        'audit_unordered': _with_coverage(score, 'audit_coverage', replace(extra, wavelength_nm=(500., 525., 512.5) + extra.wavelength_nm[3:])),
        'audit_missing_group': dict(score, audit_coverage=score['audit_coverage'][:1]),
        'audit_dense_mismatch': _with_coverage(score, 'audit_coverage', replace(extra, actual_values=(.9,) + extra.actual_values[1:])),
        'missing_pointwise': dict(score, errors=score['errors'][:-1]),
        'duplicate_pointwise': dict(score, errors=score['errors'] + (score['errors'][-1],)),
        'wrong_error_index': _with_error(score, objective_index=1),
        'wrong_error_angle': _with_error(score, angle_deg=90.),
        'wrong_error_kind': _with_error(score, reduction='not_a_constraint'),
        'negative_error_index': _with_error(score, objective_index=-1),
        'boolean_error_index': _with_error(score, objective_index=True),
        'missing_error_actual': dict(score, errors=({k: v for k, v in asdict(score['errors'][0]).items()
                                                      if k != 'actual'},) + score['errors'][1:]),
        'wrong_pointwise_wavelength': _with_error(score, -1, wavelength_nm=587.),
        'wrong_extra_actual': _with_error(score, -1, actual=.9),
        'forged_mean': _with_error(score, actual=.5),
        'forged_weighted_error': _with_error(score, -1, weighted_normalized_violation=0.),
        'forged_strict': _with_error(score, strict_lower=True),
        'forged_metric': _with_error(score, metric='X'),
        'forged_nominal_flag': dict(score, nominal_pass=True),
        'forged_solver_flag': dict(score, solver_acceptable=True),
        'forged_condition_count': dict(score, condition_count=0),
        'forged_rms': dict(score, weighted_rms_error=.5),
        'forged_max': dict(score, max_error=0.),
        'forged_weighted_max': dict(score, weighted_max_error=0.),
        'forged_audit_points': dict(score, audit=dict(score['audit'], offset_points=0)),
        'forged_audit_certificate': dict(score, audit=dict(score['audit'], continuous_certificate=True)),
        'forged_audit_rms_measure': dict(score, audit=dict(score['audit'], rms_measure='all_points')),
    }
    cases.update({name: (request, altered, audit) for name, altered in variants.items()})
    # These rows all appear individually acceptable; only full evidence validation
    # detects the corruption. They exercise the reject-only guard's other branch.
    passing_request, passing_score, passing_audit = cases['mean_only']
    pass_variants = {
        'pass_missing_coverage': {k: v for k, v in passing_score.items() if k != 'coverage'},
        'pass_missing_audit': {k: v for k, v in passing_score.items() if k != 'audit_coverage'},
        'pass_empty_errors': dict(passing_score, errors=()),
        'pass_forged_actual': _with_error(passing_score, actual=.9),
        'pass_forged_metric': _with_error(passing_score, metric='X'),
        'pass_forged_nominal_flag': dict(passing_score, nominal_pass=False),
        'pass_forged_solver_flag': dict(passing_score, solver_acceptable=False),
        'pass_forged_max': dict(passing_score, max_error=.5),
    }
    cases.update({name: (passing_request, altered, passing_audit)
                  for name, altered in pass_variants.items()})
    req, score = _plain_score(values=(.45, .45, .45))
    cases['solver_tolerance_only'] = (req, score, False)
    req, score = _plain_score(values=(0., 1., 0.))
    cases['mean_not_pointwise'] = (req, score, False)
    req, score = _plain_score(values=(0., 1., 0.), pointwise=True)
    cases['mean_and_pointwise'] = (req, score, False)
    req, score = _plain_score(values=(.5, .5, .5), strict=True)
    cases['strict_boundary'] = (req, score, False)
    return cases


def _results():
    return {name: [complete_original_score(req, score, independent_audit=audit),
                   nominal_targets_pass(req, score, independent_audit=audit),
                   solver_acceptable(req, score, independent_audit=audit)]
            for name, (req, score, audit) in _cases().items()}


def test_synthetic_evidence_decisions_and_tamper_rejection():
    results = _results()
    assert results.pop('normal') == [True, True, True]
    assert results.pop('strict') == [True, False, False]
    assert results.pop('mixed') == [True, False, False]
    assert results.pop('mean_only') == [True, True, True]
    assert results.pop('solver_tolerance_only') == [True, False, True]
    assert results.pop('mean_not_pointwise') == [True, True, True]
    assert results.pop('mean_and_pointwise') == [True, False, False]
    assert results.pop('strict_boundary') == [True, False, False]
    assert results and all(result == [False, False, False] for result in results.values()), results


def test_mapping_implementations_and_list_evidence_are_equivalent():
    req, score = _synthetic_score()
    mapped = UserDict(score)
    for name in ('coverage', 'audit_coverage', 'errors'):
        mapped[name] = [UserDict(asdict(row)) for row in score[name]]
    mapped['audit'] = UserDict(score['audit'])
    assert complete_original_score(req, mapped, independent_audit=True)
    assert not nominal_targets_pass(req, mapped, independent_audit=True)
    assert not solver_acceptable(req, mapped, independent_audit=True)


@pytest.mark.parametrize('function', [acceptance.nominal_targets_pass, acceptance.solver_acceptable])
def test_witnessed_failure_short_circuits_but_passing_rows_require_complete_check(monkeypatch, function):
    bad_request, bad_score = _plain_score(values=(0., 0., 0.))
    good_request, good_score = _plain_score(values=(.8, .8, .8))
    calls = []
    real_complete = acceptance.complete_original_score

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(acceptance, 'complete_original_score', tracked)
    assert not function(bad_request, bad_score)
    assert not calls
    assert function(good_request, good_score)
    assert len(calls) == 1
    # Empty rows cannot prove success and must reach the full validator.
    assert not function(good_request, dict(good_score, errors=()))
    assert len(calls) == 2
    # Passing-looking but forged evidence cannot grant acceptance either.
    assert not function(good_request, _with_error(good_score, actual=.9))
    assert len(calls) == 3


@pytest.mark.parametrize('function', [acceptance.nominal_targets_pass, acceptance.solver_acceptable])
def test_mutated_score_is_rechecked_not_cached(function):
    request, score = _plain_score(values=(.8, .8, .8))
    assert function(request, score)
    original = score['errors']
    score['errors'] = (replace(original[0], actual=0.),)
    assert not function(request, score)
    score['errors'] = original
    assert function(request, score)
    score['coverage'] = ()
    assert not function(request, score)


def test_zero_normalization_is_rejected_without_exception():
    request, score = _plain_score(values=(.8, .8, .8))
    request.objectives = (replace(request.objectives[0], normalization_scale=0.),)
    assert not nominal_targets_pass(request, score)
    assert not solver_acceptable(request, score)


def test_audit_extras_never_reweight_dense_mean_or_rms():
    for pointwise in (False, True):
        req, score = _synthetic_score(pointwise=pointwise)
        assert complete_original_score(req, score, independent_audit=True)
        assert [e.actual for e in score['errors'] if e.reduction == 'mean'] == pytest.approx([.8, .8])
        assert score['weighted_rms_error'] == 0.
        assert nominal_targets_pass(req, score, independent_audit=True) is (not pointwise)


if __name__ == '__main__':
    canonical = {}
    for name in ('normal', 'strict', 'mixed', 'mean_only'):
        _, score, _ = _cases()[name]
        canonical[name] = {key: score[key] for key in
                           ('max_error', 'weighted_max_error', 'weighted_rms_error',
                            'nominal_pass', 'solver_acceptable', 'condition_count')}
        canonical[name]['rows'] = [
            [e.objective_index, e.angle_deg, e.reduction, e.wavelength_nm, e.actual,
             e.normalized_violation, e.weighted_normalized_violation] for e in score['errors']]
    print(json.dumps({'decisions': _results(), 'scores': canonical}, sort_keys=True, allow_nan=False))
