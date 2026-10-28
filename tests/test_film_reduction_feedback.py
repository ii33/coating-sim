"""Reduction-only terminal feedback; real session/undo plus bounded small optics."""
from copy import deepcopy
from dataclasses import replace

import pytest
from PySide6.QtCore import QObject

from coat_sim.core.film_reduction import (
    FilmReductionRequest, freeze_film_reduction_request, reduce_film_design,
)
from tests.test_film_reduction_ui import audited, finish
from tests.test_film_optimization_cancel_apply_review import app as app, review as review
from tests.test_film_optimization_restore_layout import window_runtime
from tests.test_film_optimization_run_contract import (
    attempt_harness as attempt_harness, _wait, _dispose,
)

pytestmark = pytest.mark.qt


def test_completed_qualified_reduction_autoapplies_once_and_can_undo(review):
    h = review
    request, result = audited(h)
    c = finish(h, request, result)
    assert c._candidate_applied and not c.canApply
    assert h.source.session.snapshot().design['front_layers'] == []
    assert '合格改善已应用' in c.statusText and '层数 1 → 0' in c.statusText
    assert '总厚' in c.statusText and '未保存' in c.statusText
    revision = h.source.session.snapshot().design_revision
    assert len(h.refreshes) == 1 and not h.saves
    c._on_finished(1, result, [], None)
    assert h.source.session.snapshot().design_revision == revision
    assert len(h.refreshes) == 1 and not h.saves
    assert h.source.undoOptimizedDesign()
    assert h.source.session.snapshot().design['front_layers'] == h.base.design['front_layers']
    assert h.path.read_bytes()


def test_fast_completed_without_improvement_is_explicit_and_preserves_design(review):
    h = review
    request, _ = audited(h)
    thickness = h.base.design['front_layers'][0]['thickness']['value']
    fixed = replace(request.base_request.variables[0], lower_nm=thickness, upper_nm=thickness + 1.)
    request = freeze_film_reduction_request(h.service, FilmReductionRequest(
        replace(request.base_request, variables=(fixed,)), 'thickness'))
    result = reduce_film_design(h.service, request)
    assert result.summary['candidate_feasible'] and not result.summary['reduction_achieved']
    c = finish(h, request, result)
    assert '未找到合格改善' in c.statusText and '原设计未改变' in c.statusText
    assert '终止原因' in c.statusText and '层数 1 → 1' in c.statusText
    assert h.source.session.snapshot().design_revision == h.base.design_revision
    assert not c.canApply and not h.refreshes and not h.saves


def test_infeasible_source_is_not_reported_as_success_or_applied(review):
    h = review
    request, _ = audited(h)
    thickness = h.base.design['front_layers'][0]['thickness']['value']
    variable = replace(request.base_request.variables[0], lower_nm=thickness, upper_nm=thickness + 1.)
    from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
    objective = replace(request.base_request.objectives[0],
        pointwise=FilmObjectiveConstraint(mode='range', lower_bound=0., upper_bound=1e-6))
    request = freeze_film_reduction_request(h.service, FilmReductionRequest(
        replace(request.base_request, variables=(variable,), objectives=(objective,)), 'thickness'))
    result = reduce_film_design(h.service, request)
    assert result.summary['source_feasible'] is False
    assert result.summary['candidate_feasible'] is False
    c = finish(h, request, result)
    assert '未找到同时通过完整原目标与独立复核' in c.statusText
    assert '原设计未改变' in c.statusText and not c.canApply
    assert h.source.session.snapshot().design_revision == h.base.design_revision
    assert not h.refreshes and not h.saves


@pytest.mark.parametrize('state,error,expected', [
    ('failed', 'ValueError: controlled material failure', '减量优化失败'),
    ('cancelled', None, '减量优化已取消'),
    ('completed', 'observer decode failure', '减量优化失败'),
])
def test_terminal_reason_and_candidate_are_visible_without_autoapply(review, state, error, expected):
    request, result = audited(review)
    result = replace(result, base_result=replace(result.base_result, state=state,
        message='controlled terminal reason'), is_final=state == 'completed')
    c = finish(review, request, result, error=error)
    assert expected in c.statusText
    assert (error or 'controlled terminal reason') in c.statusText
    assert review.source.session.snapshot().design_revision == review.base.design_revision
    assert not c._candidate_applied and not review.refreshes


@pytest.mark.parametrize('error', [
    {'state': 'submission_failed', 'message': 'controlled admission failure'},
    {'state': 'submission_unknown', 'job_id': 'known-id'},
    {'state': 'cancelled', 'message': 'cancelled before audit'},
    'ValueError: controlled worker exception',
    None,
])
def test_no_candidate_terminal_is_never_silent(review, error):
    request, _ = audited(review)
    c = finish(review, request, None, error=error)
    assert c.statusText and c.resultTerminalState != '已完成'
    assert ('未改变' in c.statusText or '未完成' in c.statusText)
    assert not c.canApply and not c.hasCandidate
    assert review.source.session.snapshot().design_revision == review.base.design_revision
    if error and isinstance(error, str):
        assert error in c.statusText


def test_cancel_intent_and_partial_completed_candidate_do_not_autoapply(review):
    request, result = audited(review)
    result = replace(result, is_final=False)
    c = finish(review, request, result)
    assert '阶段候选未自动应用' in c.statusText
    assert not c._candidate_applied and not review.refreshes
    result = replace(result, is_final=True)
    c._cancel_intent_accepted = True
    c._on_finished(1, result, [], None)
    assert '未自动应用' in c.statusText
    assert not c._candidate_applied and not review.refreshes


def test_apply_failure_is_reported_and_keeps_original(review, monkeypatch):
    request, result = audited(review)
    monkeypatch.setattr(review.source, 'applyReducedDesign', lambda *args: False)
    before = []
    c = finish(review, request, result, before_completion=lambda _c:
               before.append(deepcopy(review.source.session.snapshot().design)))
    assert c.error and c.error in c.statusText and '原设计未改变' in c.statusText
    assert review.source.session.snapshot().design == before[0] and not review.refreshes


def test_refresh_exception_reports_applied_design_without_swallowing_error(review, monkeypatch):
    request, result = audited(review)
    def failed_refresh():
        raise OSError('controlled refresh failure')
    monkeypatch.setattr(review.source, 'openSimulationAndCalculate', failed_refresh)
    c = finish(review, request, result)
    assert c._candidate_applied and review.source.canUndoOptimizedDesign
    assert review.source.session.snapshot().design['front_layers'] == []
    assert 'OSError: controlled refresh failure' in c.statusText
    assert '候选已应用' in c.statusText and not review.saves


def test_sidebar_real_qml_shows_no_improvement_and_failure(review, app):
    request, result = audited(review)
    result = replace(result, summary={**result.summary, 'reduction_achieved': False})
    c = finish(review, request, result)
    with window_runtime(app, c) as (window, warnings):
        window.setProperty('detailsExpanded', True)
        label = window.findChild(QObject, 'filmOptimizationSidebarStatus')
        assert label is not None
        assert '未找到合格改善' in label.property('text')
        c._error = 'controlled visible failure'
        c.changed.emit()
        assert 'controlled visible failure' in label.property('text')
        assert not [line for line in warnings if 'ReferenceError' in line or 'TypeError' in line]


def test_start_failure_and_duplicate_submission_are_visible(attempt_harness, monkeypatch):
    from tests.test_film_optimization_run_contract import _HeldWorker
    make_controller, stores, _request = attempt_harness
    c, _source = make_controller()
    try:
        assert c.setSearchMode('reduction')
        monkeypatch.setattr(c, '_active_configuration_error', lambda **kwargs: 'controlled invalid bounds')
        assert not c.startOptimization()
        assert 'controlled invalid bounds' in c.statusText and not stores
        monkeypatch.setattr(c, '_active_configuration_error', lambda **kwargs: '')
        assert c.startOptimization(), c.error
        assert not c.startOptimization()
        assert c.busy and len(stores[-1].submissions) == 1
        assert '未完成' not in c.statusText
        _HeldWorker.instances[-1].finish(error='controlled worker failure')
        _wait(lambda: not c.busy)
        assert 'controlled worker failure' in c.statusText
        assert c.startOptimization(), c.error
        assert c.busy and 'controlled worker failure' not in c.statusText
        assert len(_HeldWorker.instances) == 2
        _HeldWorker.instances[-1].finish()
        _wait(lambda: not c.busy)
        assert '没有返回有效候选' in c.statusText
    finally:
        _dispose(c)
