"""Adversarial hybrid control-flow and physical-bound review regressions."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import synthesize_film_design
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_refinement import (
    SynthesisStageBudget, refine_synthesis_explorer, refine_synthesis_full_minimax,
)
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_hybrid_pipeline import layer, _guide_value

pytestmark = pytest.mark.algorithm


def _single_layer_problem(*, fixed=False):
    _, base = setup_problem()
    request = replace(base, objectives=(FilmOptimizationObjective(
        550., 550., 1, mode='value', target_value=0.),),
        minimum_nonzero_thickness_nm=1. if fixed else None,
        max_layer_thickness_nm=1. if fixed else None)
    design = deepcopy(base.environment)
    design['front_layers'] = [layer('x', thickness=1.)]
    return request, design


def test_real_sse_all_fixed_physical_bounds_handle_scipy_result_without_nit():
    request, design = _single_layer_problem(fixed=True)
    def evaluate(trial, **kwargs):
        return _guide_value(request, trial, [.1], [0.])
    terminal, diagnostics = refine_synthesis_explorer(request, evaluate(design),
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert terminal['design'] == design
    assert diagnostics['success']
    assert diagnostics['iterations'] == 0


def test_real_hybrid_fixed_layer_bounds_return_result_instead_of_attribute_error():
    service, request = setup_problem(upper=0., low=450., high=700., samples=11)
    request = replace(request, search_strategy='hybrid',
        minimum_nonzero_thickness_nm=10., max_layer_thickness_nm=10.,
        max_total_thickness_nm=20., max_cycles=2, max_evaluations=120,
        acceptable_normalized_error=0.)
    result = synthesize_film_design(service, request)
    assert result.state != 'failed', result.termination_reason
    assert result.candidate_design is not None
    assert all(row['thickness']['value'] == 10.
               for row in result.candidate_design['front_layers'])


def test_sse_stage_boundary_preserves_just_accepted_cached_physical_iterate(monkeypatch):
    request, design = _single_layer_problem()
    used = 0
    def evaluate(trial, **kwargs):
        nonlocal used
        used += 1
        return _guide_value(request, trial,
            [trial['front_layers'][0]['thickness']['value']], [1.])
    initial = _guide_value(request, design, [1.], [1.])
    def check():
        if used >= 1:
            raise SynthesisStageBudget()
    def minimize(fun, x0, **kwargs):
        accepted = np.array([.005])
        fun(accepted)  # Last admitted physical evaluation; complete and cached.
        kwargs['callback'](accepted)  # Acceptance costs no extra optical call.
        return SimpleNamespace(x=accepted, success=True, status=0, nit=1,
                               message='converged')
    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    terminal, diagnostics = refine_synthesis_explorer(request, initial,
        evaluate=evaluate, archive=lambda *a, **k: None, cancel_check=check)
    assert terminal['design']['front_layers'][0]['thickness']['value'] == .5
    assert diagnostics['termination'] == 'stage_budget'
    assert diagnostics['physical_candidates'] == 1


def test_minimax_stage_boundary_diagnostics_preserve_just_accepted_cached_iterate(monkeypatch):
    request, design = _single_layer_problem()
    used = 0
    def evaluate(trial, **kwargs):
        nonlocal used
        used += 1
        return _guide_value(request, trial,
            [.1 * trial['front_layers'][0]['thickness']['value']], [.1])
    initial = _guide_value(request, design, [.1], [.1])
    def check():
        if used >= 1:
            raise SynthesisStageBudget()
    def minimize(fun, x0, **kwargs):
        accepted = np.array([.005, .05])
        kwargs['constraints'][0]['fun'](accepted)
        kwargs['callback'](accepted)
        return SimpleNamespace(x=accepted, success=True, status=0, nit=1,
                               message='converged')
    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, diagnostics = refine_synthesis_full_minimax(request, initial,
        evaluate=evaluate, archive=lambda *a, **k: None, cancel_check=check)
    assert best['design']['front_layers'][0]['thickness']['value'] == .5
    assert diagnostics['terminal_physical_hash'] == physical_design_hash(best['design'])
    assert diagnostics['terminal_max_error'] == pytest.approx(.05)
    assert diagnostics['termination'] == 'stage_budget'


def test_full_minimax_positive_minimum_roundtrip_does_not_lose_initial_physics():
    request, design = _single_layer_problem()
    minimum = 1.5659576579707837
    request = replace(request, minimum_nonzero_thickness_nm=minimum)
    design['front_layers'][0]['thickness']['value'] = minimum
    def evaluate(trial, **kwargs):
        return _guide_value(request, trial,
            [.1 * trial['front_layers'][0]['thickness']['value']], [.1])
    # Compression/other full physical scores do not always carry Jacobians.
    initial = _guide_value(request, design, [.1 * minimum])
    best, diagnostics = refine_synthesis_full_minimax(request, initial,
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert best['design']['front_layers'][0]['thickness']['value'] >= minimum
    assert diagnostics['original_signed_constraints'] == 2
    assert diagnostics['terminal_max_error'] is not None


def test_real_hybrid_positive_minimum_roundtrip_returns_bounded_result():
    service, request = setup_problem(upper=0., low=450., high=700., samples=7)
    request = replace(request, search_strategy='hybrid',
        minimum_nonzero_thickness_nm=1.5659576579707837,
        max_layer_thickness_nm=33.37534176215106,
        max_total_thickness_nm=68.75183673571709,
        max_cycles=1, max_evaluations=70, acceptable_normalized_error=0.)
    result = synthesize_film_design(service, request)
    assert result.state != 'failed', result.termination_reason
    assert result.candidate_design is not None
    ds = [row['thickness']['value'] for row in result.candidate_design['front_layers']]
    assert all(request.minimum_nonzero_thickness_nm <= value <= request.max_layer_thickness_nm
               for value in ds)
    assert sum(ds) <= request.max_total_thickness_nm


def test_sse_positive_minimum_roundtrip_does_not_reject_feasible_initial():
    request, design = _single_layer_problem()
    minimum = 1.5659576579707837
    request = replace(request, minimum_nonzero_thickness_nm=minimum)
    design['front_layers'][0]['thickness']['value'] = minimum
    def evaluate(trial, **kwargs):
        return _guide_value(request, trial,
            [.1 * trial['front_layers'][0]['thickness']['value']], [.1])
    initial = _guide_value(request, design, [.1 * minimum])
    terminal, diagnostics = refine_synthesis_explorer(request, initial,
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert terminal['design']['front_layers'][0]['thickness']['value'] >= minimum
    assert diagnostics['weighted_sse'] is not None


def test_unlimited_evaluation_budget_continues_after_per_stage_quanta():
    service, request = setup_problem(low=550., high=550., samples=1)
    request = replace(request, search_strategy='hybrid', max_evaluations=None,
        max_seconds=None, max_cycles=3, max_layers=2,
        objectives=(FilmOptimizationObjective(550., 550., 1, mode='value', target_value=0.),
                    FilmOptimizationObjective(550., 550., 1, mode='value', target_value=1.)),
        acceptable_normalized_error=0.)
    result = synthesize_film_design(service, request)
    hybrid = result.performance['search']['hybrid']
    assert result.termination_reason == 'cycle_limit'
    assert result.cycles == hybrid['completed_rotations'] == 3
    assert len(hybrid['needle_scans']) == 3
    assert any(row['termination'] == 'stage_budget' for row in hybrid['stages'])


@pytest.mark.parametrize('full_minimax', (False, True))
def test_accepted_cache_stage_fix_does_not_swallow_real_caller_cancellation(monkeypatch, full_minimax):
    from coat_sim.core.film_optimization_structure import _StructureCancelled
    request, design = _single_layer_problem()
    used = 0
    def evaluate(trial, **kwargs):
        nonlocal used
        used += 1
        return _guide_value(request, trial,
            [.1 * trial['front_layers'][0]['thickness']['value']], [.1])
    initial = _guide_value(request, design, [.1], [.1])
    def check():
        if used >= 1:
            raise _StructureCancelled()
    def minimize(fun, x0, **kwargs):
        accepted = np.array([.005, .05]) if full_minimax else np.array([.005])
        if full_minimax:
            kwargs['constraints'][0]['fun'](accepted)
        else:
            fun(accepted)
        kwargs['callback'](accepted)
        raise AssertionError('caller cancellation should have propagated')
    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    refine = refine_synthesis_full_minimax if full_minimax else refine_synthesis_explorer
    with pytest.raises(_StructureCancelled):
        refine(request, initial, evaluate=evaluate,
            archive=lambda *a, **k: None, cancel_check=check)


@pytest.mark.parametrize('refine', (refine_synthesis_explorer, refine_synthesis_full_minimax))
@pytest.mark.parametrize('limit_kind', ('layer_maximum', 'total_maximum'))
def test_scaled_initial_bound_roundtrip_never_admits_infeasible_optics(refine, limit_kind):
    request, design = _single_layer_problem()
    bound = 3.14  # (bound / 100) * 100 is one ULP above the physical bound.
    request = replace(request,
        max_layer_thickness_nm=bound if limit_kind == 'layer_maximum' else None,
        max_total_thickness_nm=bound if limit_kind == 'total_maximum' else None)
    design['front_layers'][0]['thickness']['value'] = bound
    calls = []
    def evaluate(trial, **kwargs):
        d = trial['front_layers'][0]['thickness']['value']
        assert 0. <= d <= bound
        calls.append(d)
        return _guide_value(request, trial, [.1 * d], [.1])
    terminal, diagnostics = refine(request, _guide_value(request, design, [.1 * bound]),
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert calls and calls[0] == bound
    assert terminal['design']['front_layers'][0]['thickness']['value'] <= bound


def test_auto_resource_measurement_is_followed_by_freeze_cancellation_check(monkeypatch):
    from threading import Event
    from coat_sim.core import compute_resources
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_optimization_structure import _StructureCancelled
    service, request = setup_problem()
    cancelled = Event()
    def measure_then_cancel():
        cancelled.set()
        return 8 * 1024**3
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', measure_then_cancel)
    with pytest.raises(_StructureCancelled):
        freeze_film_synthesis_request(service,
            replace(request, max_working_set_bytes=0), cancel_event=cancelled)


def test_auto_zero_headroom_is_an_actual_zero_guard_not_off(monkeypatch):
    from coat_sim.core import compute_resources
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: 0)
    service, request = setup_problem(upper=0., low=450., high=700., samples=7)
    result = synthesize_film_design(service, replace(request, search_strategy='hybrid',
        max_working_set_bytes=0, max_cycles=1, max_evaluations=30,
        acceptable_normalized_error=0.))
    assert result.state != 'failed'
    hybrid = result.performance['search']['hybrid']
    assert hybrid['effective_max_working_set_bytes'] == 0
    assert hybrid['resource_limited']
    assert hybrid['resource_skips'] and all(row['limit_bytes'] == 0 for row in hybrid['resource_skips'])
    assert result.evaluations == 2  # Bare baseline and independent audit retain their existing scope.


@pytest.mark.parametrize('prior_limit', (None, 123456))
def test_editing_off_or_explicit_resource_policy_to_auto_resolves_once(monkeypatch, prior_limit):
    from coat_sim.core import compute_resources
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    service, request = setup_problem()
    calls = []
    def measure():
        calls.append(1)
        return 987654321
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', measure)
    prior = freeze_film_synthesis_request(service, replace(request, max_working_set_bytes=prior_limit))
    assert not calls
    changed = freeze_film_synthesis_request(None, replace(prior, max_working_set_bytes=0))
    replay = freeze_film_synthesis_request(None, changed)
    assert calls == [1]
    assert changed.resource_guard_resolution == replay.resource_guard_resolution
    assert changed.resource_guard_resolution is not None
    assert changed.resource_guard_resolution['effective_bytes'] == 987654321 // 2


def test_auto_resource_resolution_does_not_alias_caller_metadata(monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis
    _, request = setup_problem()
    metadata = dict(version=1, mode='auto', requested_bytes=0,
        effective_bytes=4000, available_bytes=8000, basis='half_available_memory_at_freeze')
    owned = synthesis._validated(replace(request, max_working_set_bytes=0,
        resource_guard_resolution=metadata))
    metadata['effective_bytes'] = 1
    metadata['available_bytes'] = 2
    assert owned.resource_guard_resolution is not None
    assert owned.resource_guard_resolution['effective_bytes'] == 4000
    checked = synthesis._checked_resource_guard_resolution(owned)
    assert checked is not None
    checked['effective_bytes'] = 10
    assert owned.resource_guard_resolution['effective_bytes'] == 4000


@pytest.mark.parametrize('bad', (
    {}, [],
    dict(version=True, mode='auto', requested_bytes=0, effective_bytes=4,
         available_bytes=8, basis='half_available_memory_at_freeze'),
    dict(version=1, mode='auto', requested_bytes=0, effective_bytes=True,
         available_bytes=2, basis='half_available_memory_at_freeze'),
    dict(version=1, mode='auto', requested_bytes=0, effective_bytes=0,
         available_bytes=-1, basis='half_available_memory_at_freeze'),
    dict(version=1, mode='auto', requested_bytes=0, effective_bytes=4,
         available_bytes=8, basis='disabled'),
    dict(version=1, mode='disabled', requested_bytes=0, effective_bytes=4,
         available_bytes=8, basis='half_available_memory_at_freeze'),
    dict(version=1, mode='auto', requested_bytes=0, effective_bytes=4,
         available_bytes=8, basis='half_available_memory_at_freeze', extra='unknown'),
))
def test_resource_resolution_wire_rejects_malformed_or_inconsistent_maps(monkeypatch, bad):
    from coat_sim.core import compute_resources
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_io import (
        FilmSynthesisPayloadError, film_synthesis_request_to_payload,
        film_synthesis_request_from_payload,
    )
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: 8 * 1024**3)
    service, request = setup_problem()
    frozen = freeze_film_synthesis_request(service, replace(request, max_working_set_bytes=0))
    payload = film_synthesis_request_to_payload('malformed-auto-resource', frozen)
    payload['request']['resource_guard_resolution'] = bad
    with pytest.raises(FilmSynthesisPayloadError, match='resource_guard_resolution'):
        film_synthesis_request_from_payload(payload)


@pytest.mark.parametrize('kind', ('caller_cancel', 'whole_request_stop'))
def test_auto_resource_measurement_control_stops_are_not_memory_fallback(monkeypatch, kind):
    from coat_sim.core import compute_resources
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, _Stopped
    from coat_sim.core.film_optimization_structure import _StructureCancelled
    service, request = setup_problem()
    sentinel = _StructureCancelled() if kind == 'caller_cancel' else _Stopped('evaluation_limit')
    def stopped_probe():
        raise sentinel
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', stopped_probe)
    with pytest.raises(type(sentinel)) as captured:
        freeze_film_synthesis_request(service, replace(request, max_working_set_bytes=0))
    assert captured.value is sentinel


@pytest.mark.qt
def test_resource_mode_native_keyboard_refusal_restores_authoritative_policy(tmp_path):
    from PySide6.QtCore import QObject, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import optimizer, window_runtime
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    original = deepcopy(design)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode('synthesis')
        with window_runtime(app, controller) as (window, warnings):
            mode = window.findChild(QObject, 'filmSynthesisResourceGuardModeBox')
            assert mode is not None and mode.property('currentIndex') == 0
            assert not controller.setSynthesisOption('archive_capacity', 0)
            mode.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_Down)  # Native Qt selection and activated signal.
            app.processEvents()
            assert controller.synthesisOptions.get('max_working_set_bytes', 0) == 0
            assert mode.property('currentIndex') == 0  # Refused manual selection cannot lie about policy.
            assert controller.setSynthesisOption('archive_capacity', 2)
            assert controller.setSynthesisOption('max_working_set_bytes', 3 * 1024**3)
            app.processEvents()
            assert mode.property('currentIndex') == 1  # Rollback must not break the live binding.
            assert controller.setSynthesisOption('max_working_set_bytes', None)
            app.processEvents()
            assert mode.property('currentIndex') == 2
            assert not any('ReferenceError' in message or 'TypeError' in message for message in warnings)
        assert session.snapshot().design == original
