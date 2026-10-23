"""Independent review of cancellation-to-draft ownership and atomic application.

Most terminal messages are controlled UI inputs, not numerical acceptance data.
The source controller/session and its single undo transaction remain real.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import os
from threading import Event
import time
from types import SimpleNamespace
from typing import Any, cast

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmOptimizationResult,
    FilmThicknessVariable, freeze_film_optimization_request,
)
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.compute_target import ComputeTarget
from tests.film_structure_fixtures import fixture

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def review(app, tmp_path, monkeypatch):
    service, library, design = fixture(tmp_path)
    path = tmp_path / 'source.cfilm'
    service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=service,
        geometry_settings=QSettings(str(tmp_path / 'geometry.ini'), QSettings.Format.IniFormat))
    assert source.session is not None
    assert source.session.request_open_file(str(path))
    base = source.session.snapshot()
    controller = FilmOptimizationQuickController(source)
    request = freeze_film_optimization_request(service, FilmOptimizationRequest(
        design_snapshot=deepcopy(dict(base.design)), design_revision=base.design_revision,
        variables=(FilmThicknessVariable('front', design['front_layers'][0]['id'], 0., None),),
        objectives=(FilmOptimizationObjective(550., 550., 1, mode='range', upper_bound=.001),),
        max_iterations=2, max_evaluations=8))
    refreshes: list[dict[str, Any]] = []
    saves: list[Any] = []

    def refresh():
        assert source.session is not None
        snap = source.session.snapshot()
        refreshes.append({'design': deepcopy(dict(snap.design)), 'revision': snap.design_revision})
        return True

    monkeypatch.setattr(source, 'openSimulationAndCalculate', refresh)
    monkeypatch.setattr(service, 'save', lambda *args, **kwargs: saves.append((args, kwargs)))
    harness = SimpleNamespace(service=service, library=library, source=source,
        controller=controller, base=base, request=request, path=path,
        disk_sha256=sha256(path.read_bytes()).hexdigest(), refreshes=refreshes, saves=saves)
    try:
        yield harness
    finally:
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


class AcceptedCancelEvent(Event):
    """Held transport delivering the same success callback as RemoteCancelEvent."""

    _remote = False

    def __init__(self, job_id, callback):
        super().__init__()
        self.job_id = job_id
        self.on_cancel_status = callback
        self.cancel_acknowledged = False
        self.defer_ack = False

    def confirm(self):
        self.cancel_acknowledged = True
        self.on_cancel_status(self.job_id, '')

    def set(self):
        super().set()
        if not self.defer_ack:
            self.confirm()


def arm(harness, operation_id=1):
    controller = harness.controller
    controller._operation_serial = operation_id
    controller._active_operation_id = operation_id
    controller._queue_job_id = f'review-job-{operation_id}'
    controller._operation_edit_epoch = harness.source.pendingEditEpoch
    controller._source_revision = harness.base.design_revision
    controller._frozen_request = harness.request
    controller._cancel_event = AcceptedCancelEvent(
        controller._queue_job_id, controller._on_cancel_transport_status)
    controller._capture_cancel_attempt(operation_id, controller._queue_job_id,
        getattr(harness, 'queue_origin', {}), controller._cancel_event)
    controller._busy = True


def cancelled_result(harness, *, same=False):
    from coat_sim.core.film_optimization_structure import (
        FilmStructureOptimizationRequest, evaluate_frozen_structure_candidate,
    )
    candidate = deepcopy(dict(harness.base.design))
    if not same:
        candidate['front_layers'][0]['thickness']['value'] += 10.
    score = evaluate_frozen_structure_candidate(
        FilmStructureOptimizationRequest(harness.request), candidate)['score']
    return FilmOptimizationResult(state='cancelled', design_revision=harness.base.design_revision,
        best_design=candidate,
        best_thickness_nm={row['id']: row['thickness']['value'] for row in candidate['front_layers']},
        max_normalized_error=score['max_error'], weighted_max_normalized_error=score['weighted_max_error'],
        weighted_rms_error=score['weighted_rms_error'], targets_met=False,
        per_condition_errors=tuple(score['errors']), iterations=1, evaluations=1,
        message='Cancelled with a fully scored training candidate', validation_complete=False,
        termination_reason='cancelled')


def deliver(harness, result, *, operation_id=1, error=None, with_snapshot=True):
    if isinstance(result, FilmOptimizationResult) and with_snapshot:
        from coat_sim.ui.film_optimization_quick_controller import _FilmOptimizationTerminal
        result = _FilmOptimizationTerminal(result, {'source': 'worker_completed_reply',
            'replay_performed': False, 'candidate_available': True})
    harness.controller._run_coordinator._on_worker_finished(operation_id, result, [], error)


def assert_no_write(harness, before):
    after = harness.source.session.snapshot()
    assert after.design == before.design
    assert after.design_revision == before.design_revision
    assert after.dirty == before.dirty
    assert not harness.refreshes
    assert not harness.saves
    assert sha256(harness.path.read_bytes()).hexdigest() == harness.disk_sha256


def test_explicit_cancel_commits_one_real_undo_and_refreshes_same_revision(review):
    arm(review)
    result = cancelled_result(review)
    review.controller.cancelOptimization()
    review.controller.cancelOptimization()
    deliver(review, result)
    after = review.source.session.snapshot()
    assert after.design_revision == review.base.design_revision + 1, (
        review.controller.error, review.controller._progress_text,
        {name: getattr(review.controller, name) for name in (
            '_attempt_operation_id', '_terminal_operation_id', '_cancel_intent_requested',
            '_cancel_intent_accepted', '_cancel_intent_consumed', '_result_application_eligible')})
    assert after.design['front_layers'] == result.best_design['front_layers']
    assert after.dirty and review.source.canUndoOptimizedDesign
    assert all(text in review.controller.statusText for text in ('已应用', '未保存', '刷新'))
    assert review.refreshes == [{'design': dict(after.design), 'revision': after.design_revision}]
    assert not review.saves
    assert sha256(review.path.read_bytes()).hexdigest() == review.disk_sha256
    deliver(review, result)
    review.controller.cancelOptimization()
    assert review.source.session.snapshot().design_revision == after.design_revision
    assert len(review.refreshes) == 1
    assert review.source.undoOptimizedDesign()
    assert review.source.session.snapshot().design == review.base.design
    assert not review.source.undoOptimizedDesign()


@pytest.mark.parametrize('kind', ['no_result', 'zero_evaluations', 'invalid_design', 'failed'])
def test_cancel_without_valid_scored_candidate_does_not_change_source(review, kind):
    arm(review)
    result = cancelled_result(review)
    if kind == 'no_result':
        result = None
    elif kind == 'zero_evaluations':
        result = replace(result, evaluations=0)
    elif kind == 'invalid_design':
        candidate = deepcopy(dict(result.best_design))
        candidate['front_layers'][0]['thickness']['value'] = float('nan')
        result = replace(result, best_design=candidate)
    else:
        result = replace(result, state='failed', termination_reason='numerical_failure')
    review.controller.cancelOptimization()
    deliver(review, result, error={'state': 'cancelled'} if result is None else None)
    assert_no_write(review, review.base)


def test_unverified_normal_cancel_message_without_worker_snapshot_cannot_apply(review):
    arm(review)
    result = cancelled_result(review)
    review.controller.cancelOptimization()
    deliver(review, result, with_snapshot=False)
    assert_no_write(review, review.base)
    assert review.controller.hasCandidate


def test_button_cancel_ack_after_terminal_applies_only_after_success_confirmation(review):
    arm(review)
    event = review.controller._cancel_event
    event.defer_ack = True
    result = cancelled_result(review)
    review.controller.cancelOptimization()
    deliver(review, result)
    assert_no_write(review, review.base)
    event.confirm()
    assert review.source.session.snapshot().design_revision == review.base.design_revision + 1
    assert len(review.refreshes) == 1
    event.confirm()
    assert len(review.refreshes) == 1


@pytest.mark.parametrize('conflict', ['revision', 'edit_epoch', 'session', 'project', 'resource', 'provider', 'source_closed'])
def test_cancel_conflict_preserves_candidate_without_overwriting_new_state(review, monkeypatch, conflict):
    arm(review)
    result = cancelled_result(review)
    review.controller.cancelOptimization()
    if conflict == 'revision':
        changed = deepcopy(dict(review.base.design))
        changed['front_layers'][0]['thickness']['value'] = 155.
        assert review.source.session.apply_optimized_design(changed, review.base.design_revision)
    elif conflict == 'edit_epoch':
        review.source._pending_edit_epoch += 1
    elif conflict == 'session':
        from coat_sim.core.film_design_session import FilmDesignEditSession
        review.source.session = FilmDesignEditSession(review.service)
    elif conflict == 'project':
        monkeypatch.setattr(review.controller, '_project_current', lambda: False)
    elif conflict == 'source_closed':
        review.source.closeConfirmed.emit()
    elif conflict == 'provider':
        from coat_sim.core.project_film_service import ProjectOpticalSnapshot
        rows = deepcopy(review.request.frozen_material_snapshot['materials'])
        row = next(row for row in rows if row['id'] == 'acceptance-film')
        row['optical_sets'][0]['n'] = [2.9] * len(row['optical_sets'][0]['n'])
        review.service.materials = ProjectOpticalSnapshot(rows)
        assert review.source.session.snapshot().design_revision == review.base.design_revision
    else:
        row = review.library.read('acceptance-film')['record']
        row['optical_constants'][0]['n'] = [2.7, 2.7]
        review.library.write(row, update=True)
        assert review.source.session.snapshot().design_revision == review.base.design_revision
    before = review.source.session.snapshot()
    deliver(review, result)
    assert_no_write(review, before)
    assert review.controller.hasCandidate
    assert review.controller._result is result
    assert review.controller.error or review.controller.candidateStale


@pytest.mark.parametrize('disposal', ['close', 'shutdown', 'unsolicited', 'detach', 'local_work'])
def test_lifecycle_cancellation_never_creates_explicit_apply_intent(review, disposal):
    arm(review)
    result = cancelled_result(review)
    if disposal == 'close':
        review.controller.requestClose()
    elif disposal == 'shutdown':
        review.controller.shutdown(0)
    elif disposal == 'detach':
        review.controller.detach_observers()
    elif disposal == 'local_work':
        review.controller._worker = SimpleNamespace(remote_admission=lambda: None, admitted=False)
        assert review.controller.localWorkRunning
        review.controller.cancelLocalWork()
    deliver(review, result)
    assert_no_write(review, review.base)


def test_old_operation_cannot_apply_or_consume_new_attempt_cancel(review):
    arm(review, operation_id=1)
    old = cancelled_result(review)
    review.controller.cancelOptimization()
    deliver(review, None, error={'state': 'cancelled'})
    arm(review, operation_id=2)
    review.controller.cancelOptimization()
    deliver(review, old, operation_id=1)
    assert review.controller._busy
    assert review.controller._active_operation_id == 2
    assert_no_write(review, review.base)
    deliver(review, old, operation_id=2)
    assert review.source.session.snapshot().design_revision == review.base.design_revision + 1


def test_same_design_cancel_preserves_dirty_revision_undo_and_refreshes_current_revision_once(review):
    arm(review)
    review.controller.cancelOptimization()
    result = cancelled_result(review, same=True)
    deliver(review, result)
    after = review.source.session.snapshot()
    assert after.design == review.base.design
    assert after.design_revision == review.base.design_revision
    assert after.dirty == review.base.dirty
    assert not review.saves
    assert sha256(review.path.read_bytes()).hexdigest() == review.disk_sha256
    assert review.refreshes == [{'design': dict(after.design), 'revision': after.design_revision}]
    deliver(review, result)
    assert len(review.refreshes) == 1
    assert not review.source.canUndoOptimizedDesign


def test_same_design_cancel_preserves_an_existing_single_undo(review):
    original = review.base
    edited = deepcopy(dict(original.design))
    edited['front_layers'][0]['thickness']['value'] += 5.
    assert review.source.applyOptimizedDesign(edited, original.design_revision, review.source.pendingEditEpoch)
    review.base = review.source.session.snapshot()
    assert review.source.canUndoOptimizedDesign
    review.request = freeze_film_optimization_request(review.service, replace(review.request,
        design_snapshot=review.base.design, design_revision=review.base.design_revision,
        frozen_material_snapshot=None))
    arm(review)
    review.controller.cancelOptimization()
    deliver(review, cancelled_result(review, same=True))
    after = review.source.session.snapshot()
    assert after.design_revision == review.base.design_revision
    assert after.design == review.base.design and after.dirty == review.base.dirty
    assert len(review.refreshes) == 1
    assert review.source.canUndoOptimizedDesign
    assert review.source.undoOptimizedDesign()
    assert review.source.session.snapshot().design == original.design
    assert not review.source.undoOptimizedDesign()


def test_normal_optimization_cancel_rejects_replaced_layer_identity(review):
    arm(review)
    result = cancelled_result(review)
    candidate = deepcopy(dict(result.best_design))
    candidate['front_layers'][0]['id'] = 'new-topology-only-valid-for-synthesis'
    review.controller.cancelOptimization()
    deliver(review, replace(result, best_design=candidate))
    assert_no_write(review, review.base)
    assert review.controller.hasCandidate


@pytest.mark.parametrize('valid_topology', [True, False])
def test_structure_cancel_uses_full_topology_and_checks_hard_bounds_without_claiming_final_audit(review, valid_topology):
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.core.film_optimization_structure import (
        FilmStructureCounters, FilmStructureOptimizationRequest, FilmStructureOptimizationResult,
        evaluate_frozen_structure_candidate, validate_film_structure_candidate_topology,
    )
    request = FilmStructureOptimizationRequest(review.request,
        candidate_materials=(FilmNeedleMaterialReference('acceptance-film', 'film-set'),))
    base_result = cancelled_result(review)
    candidate = deepcopy(dict(base_result.best_design))
    added = deepcopy(candidate['front_layers'][0])
    added.update(id='structure-needle-review', thickness={'value': 20., 'unit': 'nm'})
    candidate['front_layers'].append(added)
    topology = dict(validate_film_structure_candidate_topology(request, candidate))
    topology['cancel_snapshot'] = {'source': 'worker_completed_reply', 'replay_performed': False,
        'candidate_available': True, 'training_complete': True}
    score = evaluate_frozen_structure_candidate(request, candidate)['score']
    if not valid_topology:
        candidate['front_layers'][-1]['thickness']['value'] = request.max_total_thickness_nm + 1.
    base_result = replace(base_result, best_design=candidate,
        best_thickness_nm={row['id']: row['thickness']['value'] for row in candidate['front_layers']},
        max_normalized_error=score['max_error'], weighted_max_normalized_error=score['weighted_max_error'],
        weighted_rms_error=score['weighted_rms_error'], per_condition_errors=tuple(score['errors']))
    result = FilmStructureOptimizationResult(base_result, 'combined', (), FilmStructureCounters(),
        topology, frozen_request=request, is_final=False, candidate_verified=True)
    review.controller._search_mode = 'structure'
    arm(review)
    review.controller.cancelOptimization()
    deliver(review, result)
    assert not result.is_final and not result.base_result.validation_complete
    if valid_topology:
        after = review.source.session.snapshot()
        assert after.design_revision == review.base.design_revision + 1
        assert after.design['front_layers'] == candidate['front_layers']
        assert len(review.refreshes) == 1
        assert review.source.canUndoOptimizedDesign
    else:
        assert_no_write(review, review.base)
        assert review.controller.hasCandidate


@pytest.mark.parametrize('strategy', ['legacy', 'hybrid'])
def test_real_typed_n0_cancel_retains_new_training_topology_without_reauditing(review, monkeypatch, tmp_path, strategy):
    from coat_sim.core import film_synthesis_audit as audit
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import (
        build_job_payload, canonical_parameters, execute_job, is_synthesis_result,
    )
    from coat_sim.ui.film_synthesis_quick_adapter import synthesis_base_request, synthesis_result_view

    environment = deepcopy(dict(review.base.design))
    environment['front_layers'] = []
    strategy_options: dict[str, Any] = {'search_strategy': 'legacy'} if strategy == 'legacy' else {}
    request = freeze_film_synthesis_request(review.service, FilmSynthesisRequest(
        environment, (FilmOptimizationObjective(550., 550., 1, mode='range', upper_bound=.03),),
        (FilmNeedleMaterialReference('acceptance-film', 'film-set'),),
        design_revision=review.base.design_revision, max_cycles=1,
        max_evaluations=None, seed_designs=(deepcopy(environment),), **strategy_options))
    assert request.search_strategy == strategy  # Hybrid exercises the real default.
    cancel = Event()
    audited_designs: list[Any] = []
    checkpoints: list[Any] = []
    original_audit = audit.audit_frozen_synthesis_candidate

    def audited(*args, **kwargs):
        assert not cancel.is_set(), 'Typed cancel response must not trigger another physical audit'
        audited_designs.append(deepcopy(args[1]))
        return original_audit(*args, **kwargs)

    def report(value, **kwargs):
        # The old single-needle finite-growth event is legacy-only. Hybrid's
        # incumbent notification occurs after the complete parent-owned score
        # commit, before its following archive/audit admission.
        if ((strategy == 'legacy' and value.get('stage') == 'zero_layer_trial')
                or (strategy == 'hybrid' and value.get('stage') == 'incumbent'
                    and value.get('layer_count') == 1)):
            cancel.set()

    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', audited)
    parameters = canonical_parameters(None, {'payload': film_synthesis_request_to_payload('ui-cancel-review', request)})
    response = execute_job({'payload': build_job_payload(parameters)}, cancel, report,
        tmp_path / 'checkpoint.json', lambda _path, value: checkpoints.append(deepcopy(value)), str(tmp_path))
    assert is_synthesis_result(response)
    raw = response['result']
    assert cancel.is_set(), 'The real strategy-specific complete-score boundary must request cancellation'
    assert raw['state'] == 'cancelled'
    assert raw['candidate_design'] is not None and len(raw['candidate_design']['front_layers']) == 1
    assert raw['candidate_provenance']['training_complete']
    assert raw['candidate_provenance']['frozen_at_cancel']
    assert raw['candidate_provenance']['fully_scored_evaluations'] > 0
    assert not raw['validation_complete'] and raw['validation_score'] is None
    assert response['best_candidate_audit']['available'] is False
    assert response['best_candidate_audit']['targets_met'] is None
    if strategy == 'legacy':
        assert len(audited_designs) == 1 and not audited_designs[0]['front_layers']
        assert checkpoints, 'The obsolete audited bare candidate exists, but must not replace training best'
        assert raw['candidate_design']['front_layers'] != audited_designs[0]['front_layers']
    else:
        # Hybrid screens its first positive-thickness prior before an audit of
        # the failing bare surface. Cancellation cannot invent an audit merely
        # to make a typed training candidate available to the UI.
        assert audited_designs == [] and checkpoints == []
        assert raw['frozen_request']['search_strategy'] == 'hybrid'
    view = synthesis_result_view(response)
    assert view.optimization_result is not None
    assert view.optimization_result.validation_targets_met is None
    assert not view.optimization_result.targets_met
    review.controller._search_mode = 'synthesis'
    review.request = synthesis_base_request(request)
    review.controller._synthesis_editable_snapshot = freeze_synthesis_editable_snapshot(
        review.service, review.base.design, request.frozen_material_snapshot)
    arm(review)
    review.controller.cancelOptimization()
    deliver(review, view)
    after = review.source.session.snapshot()
    assert after.design_revision == review.base.design_revision + 1
    assert [row['id'] for row in after.design['front_layers']] == [
        row['id'] for row in raw['candidate_design']['front_layers']]
    assert after.design['front_layers'][0]['id'] != review.base.design['front_layers'][0]['id']
    assert len(review.refreshes) == 1
    assert review.source.canUndoOptimizedDesign
    assert not review.saves
    assert sha256(review.path.read_bytes()).hexdigest() == review.disk_sha256


def test_cancel_refresh_delivers_real_spectrum_for_applied_design_only(review, monkeypatch):
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    simulation = FilmSimulationQuickController(root=review.service.root,
        service=review.service, source_controller=review.source)
    calls: list[Any] = []
    simulate = review.service.simulate

    def counted(design, settings=None, *, cancel_event=None):
        calls.append(deepcopy(design))
        return simulate(design, settings, cancel_event=cancel_event)

    monkeypatch.setattr(review.service, 'simulate', counted)
    try:
        assert simulation.setSimulationSettings({'wavelength_min_nm': 550., 'wavelength_max_nm': 551.,
            'samples': 2, 'angle_deg': 0., 'polarization': 'unpolarized'})
        arm(review)
        result = cancelled_result(review)
        review.controller.cancelOptimization()
        deliver(review, result)
        wait_for(lambda: simulation._result is not None and not simulation._busy and not simulation._stale)
        after = review.source.session.snapshot()
        assert simulation._source_design_revision == after.design_revision
        assert simulation._design == after.design
        assert len(calls) == 1 and calls[0] == after.design
        assert simulation._result is not None
        assert simulation._result['wavelength_nm'] == pytest.approx([550., 551.])
    finally:
        simulation.close()
        simulation.deleteLater()


class ReviewQueue(QObject):
    filmOptimizationResultReady = Signal(str, object)
    filmSynthesisResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)
    cancelReceived = Signal(str, object, str)

    def __init__(self, origin):
        super().__init__()
        self.origin = origin
        self.status = ''

    def _require_owned_job(self, job_id):
        return {'job_id': job_id, 'kind': 'film_optimization', 'origin': deepcopy(self.origin)}

    def _result_status(self, message):
        self.status = message


@pytest.fixture
def project_queue(review):
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.ui.project_compute_views import _wire_project_queue_results
    origin = {'project_id': 'review-project', 'project_root': str(review.path.parent),
        'project_generation': 'review-generation', 'configuration_id': 'review-configuration',
        'operation_id': 'review-job-1'}
    workspace = SimpleNamespace(editor=review.source.session)
    review.controller.project_context = workspace
    review.controller._project_compute = SimpleNamespace(editing_frozen=False,
        accepts=lambda _operation: True, update_job=lambda *_args: None,
        unregister_thaw_callback=lambda _callback: None)
    review.controller._project_operation = SimpleNamespace(job_id='review-job-1')
    review.queue_origin = origin
    opened: list[Any] = []
    desktop = SimpleNamespace(
        open_project_module=lambda *args: opened.append(args),
        controller_for=lambda _workspace, _module: review.controller)
    queue = ReviewQueue(origin)
    _wire_project_queue_results(queue, cast(ProjectFilmWorkspace, workspace), SimpleNamespace(desktop=desktop))
    return queue, opened


@pytest.mark.parametrize('timing', ['before_terminal', 'after_terminal'])
def test_matching_project_queue_accepted_cancel_applies_once_even_if_ack_arrives_late(review, project_queue, timing):
    queue, opened = project_queue
    arm(review)
    result = cancelled_result(review)
    if timing == 'after_terminal':
        deliver(review, result)
        assert_no_write(review, review.base)
    queue.cancelReceived.emit('review-job-1', True, '')
    if timing == 'before_terminal':
        deliver(review, result)
    after = review.source.session.snapshot()
    assert after.design_revision == review.base.design_revision + 1
    assert after.design['front_layers'] == result.best_design['front_layers']
    queue.cancelReceived.emit('review-job-1', True, '')
    assert review.source.session.snapshot().design_revision == after.design_revision
    assert len(review.refreshes) == 1
    assert not opened, 'Accepted cancel must only notify an existing matching optimizer'
    assert not review.saves


@pytest.mark.parametrize('rejection', ['other_job', 'wrong_origin', 'transport_error', 'not_accepted', 'old_attempt'])
def test_project_queue_cancel_ack_cannot_authorize_unrelated_or_rejected_apply(review, project_queue, rejection):
    queue, _opened = project_queue
    arm(review)
    result = cancelled_result(review)
    job_id = 'review-job-1'
    response: Any = True
    error = ''
    if rejection == 'other_job':
        job_id = 'historical-job'
    elif rejection == 'wrong_origin':
        queue.origin = {**queue.origin, 'project_id': 'foreign-project'}
    elif rejection == 'transport_error':
        error = 'controlled cancellation failure'
    elif rejection == 'not_accepted':
        response = False
    else:
        deliver(review, result)
        arm(review, operation_id=2)
    queue.cancelReceived.emit(job_id, response, error)
    deliver(review, result, operation_id=2 if rejection == 'old_attempt' else 1)
    assert_no_write(review, review.base)


def test_historical_queue_row_cannot_create_cancel_apply_authority(review, project_queue):
    queue, opened = project_queue
    result = cancelled_result(review)
    review.controller._queue_job_id = 'historical-job'
    review.controller._result = result
    review.controller._operation_edit_epoch = None
    queue.cancelReceived.emit('historical-job', True, '')
    assert_no_write(review, review.base)
    assert review.controller._result is result
    assert not opened


def wait_for(predicate, timeout=5.):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.005)
    assert predicate()
