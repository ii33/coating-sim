"""Fast assessment must never silently become a synthesis/optimization run."""
from dataclasses import replace

import pytest

from coat_sim.core import film_resource_estimation as core
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_resource_probe import FAMILIES

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize('family,objectives', FAMILIES, ids=[row[0] for row in FAMILIES])
def test_default_fast_assessment_cannot_invoke_solver_or_candidate_audit(monkeypatch, family, objectives):
    def forbidden(*args, **kwargs):
        pytest.fail('Fast assessment must not run synthesis or candidate audits')
    monkeypatch.setattr(core, 'synthesize_film_design', forbidden)
    monkeypatch.setattr(core, 'audit_frozen_synthesis_candidate', forbidden)
    service, base = setup_problem()
    base = replace(base, objectives=objectives)
    result = core.estimate_film_resources(service, core.FilmResourceEstimateRequest(base, (base.environment,)))
    assert result.state == 'completed', result.message
    assert result.work['assessment_mode'] == 'fast_screen'
    assert result.work['probe_runs'] == result.work['audits_started'] == 0
    assert result.work['probe_evaluations'] == 0
    assert not result.candidate_reports and not result.pareto_witnesses
    assert result.recommended_budget is None
    assert result.estimate_state == 'not_calibrated'
    assert result.feasibility_state == 'unknown'
    assert not result.continuous_certificate


def test_explicit_audit_without_synthesis_is_still_supported():
    service, base = setup_problem()
    result = core.estimate_film_resources(service, core.FilmResourceEstimateRequest(
        base, (base.environment,), audit_candidates=True))
    assert result.state == 'completed', result.message
    assert result.work['probe_runs'] == 0
    assert result.work['audits_completed'] == 1
    assert result.candidate_reports
    assert result.work['assessment_mode'] == 'verification'
