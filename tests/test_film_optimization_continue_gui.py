"""Cancelled candidates restart as independent, source-bound GUI attempts."""
# ruff: noqa: F811
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.test_film_optimization_cancel_apply_review import (
    app, review, arm, cancelled_result, deliver,  # noqa: F401
)
from coat_sim.core.film_optimization_structure import (
    FilmStructureOptimizationRequest, evaluate_frozen_structure_candidate,
)
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.ui.film_optimization_continue_source import (
    SOURCE_INPUT_KEY, build_source_binding, validate_source_binding,
    verified_history_source,
)

pytestmark = pytest.mark.qt


def retain(review, *, apply=False, same=False):
    arm(review)
    old = cancelled_result(review, same=same)
    score = evaluate_frozen_structure_candidate(
        FilmStructureOptimizationRequest(review.request), old.best_design)['score']
    result = replace(old, raw_coverage=tuple(score['coverage']),
        solver_acceptable=score['solver_acceptable'], targets_met=score['nominal_pass'])
    if apply:
        review.controller.cancelOptimization()
    deliver(review, result)
    assert review.controller.canContinueOptimization, review.controller.continueDisabledReason
    return result


def intercept_start(review, monkeypatch):
    controller = review.controller
    captured = []
    client = SimpleNamespace(transport=None)
    monkeypatch.setattr(controller._compute_target, 'client_for_submit',
        lambda **kwargs: (client, {'id': 'cpu:default', 'backend': 'CPU'}))
    monkeypatch.setattr(controller._compute_target, 'admission_verifier',
        lambda *args: (lambda: None, lambda: None))

    def start(*, submit, worker_factory, edit_epoch):
        controller._operation_serial += 1
        controller._active_operation_id = controller._operation_serial
        controller._busy = True
        task = worker_factory(controller._operation_serial)
        captured.append(task)
        controller._worker = task
    monkeypatch.setattr(controller._run_coordinator, 'start', start)
    return captured


def test_continue_uses_frozen_criteria_new_budget_ids_and_no_duplicate(review, monkeypatch):
    result = retain(review, apply=True)
    controller = review.controller
    prior_revision = review.source.session.snapshot().design_revision
    prior_undo = review.source._optimization_undo_revision
    jobs = intercept_start(review, monkeypatch)
    # Editing optimizer controls must not reconstruct the accepted old task.
    controller._objectives[0]['pointwise']['upper_bound'] = .9
    controller._solver_id = 'powell'
    monkeypatch.setattr(controller, '_build_request', lambda **kwargs: pytest.fail('rebuilt editor'))
    assert controller.continueOptimization({'max_iterations': 4., 'max_evaluations': 17.})
    assert len(jobs) == 1
    task = jobs[0]
    assert task.job_id != 'review-job-1'
    assert task.operation_id == 2
    assert task.request.max_iterations == 4 and task.request.max_evaluations == 17
    assert task.request.objectives == review.request.objectives
    assert task.request.solver == review.request.solver
    assert task.request.design_snapshot == result.best_design
    assert task.request.design_revision == prior_revision
    assert not controller.continueOptimization({'max_iterations': 4, 'max_evaluations': 17})
    assert len(jobs) == 1
    assert controller._result is None and not controller._result_application_eligible
    assert '独立旧作业 review-job-1' in controller.priorRunFallbackText
    assert review.source._optimization_undo_revision == prior_undo
    assert len(review.refreshes) == 1
    controller._busy = False
    controller._worker = None


def test_continue_admission_checks_source_again(review, monkeypatch):
    retain(review)
    jobs = intercept_start(review, monkeypatch)
    assert review.controller.continueOptimization({'max_iterations': 3, 'max_evaluations': 14})
    review.source.markEditingDirty()
    with pytest.raises(InterruptedError, match='来源已改变'):
        jobs[0].remote_admission()
    review.controller._busy = False
    review.controller._worker = None


@pytest.mark.parametrize('same', [False, True])
def test_actual_postapply_version_is_recorded_and_undo_disables(review, same):
    retain(review, apply=True, same=same)
    record = review.controller._continuation_record
    snapshot = review.source.session.snapshot()
    assert record['allowed_revision'] == snapshot.design_revision
    assert record['allowed_epoch'] == review.source.pendingEditEpoch
    if not same:
        assert review.source.undoOptimizedDesign()
        assert not review.controller.canContinueOptimization
        assert '撤销' in review.controller.continueDisabledReason
    else:
        assert snapshot.design_revision == review.base.design_revision


@pytest.mark.parametrize('change, reason', [
    ('pending', '未提交'), ('controller', '会话'), ('materials', '材料'),
])
def test_source_staleness_has_specific_reason(review, change, reason):
    retain(review)
    if change == 'pending':
        review.source.markEditingDirty()
    elif change == 'controller':
        review.controller._source_controller = SimpleNamespace(session=review.source.session)
    else:
        review.service.materials = object()
    assert not review.controller.canContinueOptimization
    assert reason in review.controller.continueDisabledReason


def test_no_candidate_or_structure_does_not_continue(review):
    arm(review)
    deliver(review, None, error={'state': 'cancelled'})
    assert not review.controller.canContinueOptimization
    arm(review, operation_id=2)
    review.controller._attempt_search_mode = 'structure'
    review.controller._retain_cancelled_restart()
    assert not review.controller.canContinueOptimization
    assert '结构优化' in review.controller.continueDisabledReason


def test_empty_new_cancel_keeps_old_fallback_separate_without_second_apply(review, monkeypatch):
    retain(review, apply=True)
    old = review.controller._continuation_record
    intercept_start(review, monkeypatch)
    assert review.controller.continueOptimization({'max_iterations': 3, 'max_evaluations': 14})
    review.controller._busy = False
    review.controller._active_operation_id = None
    review.controller._on_finished(2, None, [], {'state': 'cancelled'})
    assert review.controller._result is None
    assert review.controller._continuation_record is old
    assert review.controller.priorRunFallbackText
    assert review.controller.canContinueOptimization
    assert len(review.refreshes) == 1
    review.controller._worker = None


def test_binding_is_hashed_and_wrong_request_is_rejected(review):
    binding = build_source_binding(review.service, review.base, review.request)
    checked = validate_source_binding(binding, review.request)
    assert checked == binding and checked['managed_film'] is None
    spec = JobSpec('film_optimization', {}, {SOURCE_INPUT_KEY: binding})
    fingerprint = spec.fingerprint
    altered = deepcopy(spec.to_dict())
    altered['inputs'][SOURCE_INPUT_KEY]['source_design_revision'] += 1
    assert JobSpec.parse(altered).fingerprint != fingerprint
    with pytest.raises(ValueError, match='摘要'):
        validate_source_binding(binding, replace(review.request, max_iterations=10))


def test_unbound_history_remains_viewonly(review):
    result = retain(review)
    review.controller._bind_history_continuation('old', review.request, result, {}, None, 'fixed_topology')
    assert not review.controller.canContinueOptimization
    assert '历史' in review.controller.continueDisabledReason


def test_continue_submission_persists_source_and_restart_in_hashed_inputs(review, monkeypatch):
    retain(review)
    jobs = intercept_start(review, monkeypatch)
    submitted = []
    monkeypatch.setattr('coat_sim.scheduler.desktop.submit_local_with_receipt',
        lambda client, spec, **kwargs: submitted.append((deepcopy(spec), kwargs)))
    assert review.controller.continueOptimization({'max_iterations': 5, 'max_evaluations': 19})
    jobs[0].remote_admission()
    spec, receipt = submitted[0]
    binding = spec['inputs'][SOURCE_INPUT_KEY]
    assert binding['source_design_revision'] == review.base.design_revision
    meta = spec['inputs']['film_warm_restart']
    assert meta['mode'] == 'warm_restart' and meta['solver_state_preserved'] is False
    assert meta['source_job_id'] == 'review-job-1'
    assert meta['fresh_budget'] == {'max_iterations': 5, 'max_evaluations': 19}
    assert meta['spent']['evaluations'] == 1
    assert receipt['job_id'] != meta['source_job_id']
    changed = deepcopy(spec)
    changed['inputs']['film_warm_restart']['source_job_id'] = 'different'
    assert JobSpec.parse(changed).fingerprint != JobSpec.parse(spec).fingerprint
    review.controller._busy = False
    review.controller._worker = None


def test_managed_source_identity_and_history_survive_reopen(review, tmp_path, app):
    from coat_sim.core.project_store import ProjectStore
    from coat_sim.core.project_film_service import ProjectFilmService
    from coat_sim.core.film_optimization import freeze_film_optimization_request
    from coat_sim.core.film_optimization_job import serialize_result
    from coat_sim.scheduler import freeze_spec
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from tests.test_project_film_controller_adapter import MemorySettings

    store = ProjectStore.create(tmp_path / 'managed', 'Managed history')
    service = ProjectFilmService(store)
    materials = {row['id']: row for row in review.request.frozen_material_snapshot['materials']}
    doc = service.create_film(review.base.design, materials=materials)
    assert service.managed_film_identity(doc.handle) == {
        'project_id': doc.project_id, 'document_id': doc.document_id, 'revision': doc.revision}
    with pytest.raises(ValueError):
        service.managed_film_identity(tmp_path / (doc.design['id'] + '.cfilm'))

    source = FilmDesignQuickController(service=service, geometry_settings=MemorySettings())
    assert source.session is not None
    assert source.session.request_open_file(str(doc.handle))
    request = freeze_film_optimization_request(service,
        replace(review.request, design_snapshot=source.session.snapshot().design,
            design_revision=source.session.snapshot().design_revision, frozen_material_snapshot=None))
    old = cancelled_result(review)
    result = replace(old, design_revision=request.design_revision)
    score = evaluate_frozen_structure_candidate(FilmStructureOptimizationRequest(request),
        result.best_design)['score']
    result = replace(result, raw_coverage=tuple(score['coverage']), solver_acceptable=score['solver_acceptable'],
        targets_met=score['nominal_pass'])
    binding = build_source_binding(service, source.session.snapshot(), request)
    spec = freeze_spec(service, 'film_optimization', {'request': request})
    spec['inputs'][SOURCE_INPUT_KEY] = binding
    payload = serialize_result(request, result)
    row = {'job_id': 'managed-old', 'kind': 'film_optimization', 'state': 'cancelled',
        'input_digest': JobSpec.parse(spec).fingerprint, 'result': payload}
    path = service.root / 'artifacts/runtime/scheduler/jobs/managed-old/spec.json'
    path.parent.mkdir(parents=True)
    path.write_text(canonical(spec), encoding='utf-8')
    context = verified_history_source(service.root, row, payload)
    project_file = store.project_file
    source.markSourceClosed()
    source.deleteLater()

    reopened = ProjectFilmService(ProjectStore.open(project_file))
    source = FilmDesignQuickController(service=reopened, geometry_settings=MemorySettings())
    assert source.session is not None
    assert source.session.request_open_file(str(doc.handle))
    controller = FilmOptimizationQuickController(source)
    try:
        before = source.session.snapshot()
        controller._bind_history_continuation('managed-old', request, result, payload, context, 'fixed_topology')
        assert controller.canContinueOptimization, controller.continueDisabledReason
        assert not controller._result_application_eligible
        assert source.session.snapshot().design_revision == before.design_revision
        assert source.session.snapshot().design == before.design
        # Saved resource mutation invalidates history independently of raw design identity.
        edited = deepcopy(doc.design)
        edited['name'] += ' changed'
        reopened.save_document(doc.document_id, edited, expected_revision=doc.revision, materials=materials)
        assert not controller.canContinueOptimization
        assert '已保存版本' in str(controller.property('continueDisabledReason'))
        # Spec tampering cannot be excused by matching mutable row metadata.
        changed = deepcopy(spec)
        changed['inputs'][SOURCE_INPUT_KEY]['managed_film']['document_id'] = 'not-the-film'
        path.write_text(canonical(changed), encoding='utf-8')
        with pytest.raises(ValueError, match='不可变'):
            verified_history_source(reopened.root, row, payload)
    finally:
        controller.shutdown(1000)
        controller._compute_target._executor.shutdown(wait=True, cancel_futures=True)
        controller.deleteLater()
        source.markSourceClosed()
        source.deleteLater()


def test_synthesis_cancel_continue_keeps_full_frozen_request_and_editable_closure(review, monkeypatch):
    from coat_sim.core.film_auto_synthesis import FilmSynthesisResult, _proxy, freeze_film_synthesis_request
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
    from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
    from coat_sim.ui.film_synthesis_quick_adapter import (
        build_synthesis_request, synthesis_base_request, synthesis_result_view,
    )
    from tests.test_film_optimization_continuation import _no_physics

    request = freeze_film_synthesis_request(review.service, build_synthesis_request(
        review.request, (FilmNeedleMaterialReference('acceptance-film', 'film-set'),),
        {'max_cycles': 2, 'max_evaluations': 20, 'max_seconds': 30.},
        use_current_seed=True, cpu_workers=1))
    closure = freeze_synthesis_editable_snapshot(review.service, request.environment,
        request.frozen_material_snapshot)
    candidate = deepcopy(request.seed_designs[0])
    candidate['front_layers'][0]['thickness']['value'] = 110.
    score = evaluate_frozen_structure_candidate(_proxy(request), candidate)['score']
    result = FilmSynthesisResult('cancelled', 'cancelled', candidate, request.design_revision,
        False, score, None, False, 1, 2, 0, 0, (), 'Cancelled with candidate', request,
        candidate_provenance={'selection': 'completed_training_at_cancel', 'training_complete': True,
            'frozen_at_cancel': True, 'fully_scored_evaluations': 1, 'validation_matches_candidate': False})
    payload = json.loads(json.dumps(film_synthesis_result_to_payload('synthesis-old', result)))
    payload['review']['cancel_snapshot'] = {'source': 'worker_completed_reply',
        'replay_performed': False, 'candidate_available': True}
    arm(review)
    controller = review.controller
    controller._search_mode = 'synthesis'
    controller._attempt_search_mode = 'synthesis'
    controller._attempt_full_request = request
    controller._frozen_request = synthesis_base_request(request)
    controller._attempt_frozen_material_snapshot = deepcopy(request.frozen_material_snapshot)
    controller._synthesis_editable_snapshot = closure
    controller._attempt_editable_snapshot = closure
    controller.cancelOptimization()
    deliver(review, synthesis_result_view(payload))
    assert controller.canContinueOptimization, (controller.continueDisabledReason,
        controller._cancel_candidate_verified(),
        len(controller._result.per_condition_errors), controller._result.evaluations,
        payload.get('review', {}).get('cancel_snapshot'),
        payload.get('result', {}).get('candidate_provenance'), controller._attempt_source_revision)
    assert len(review.refreshes) == 1
    prior_revision = review.source.session.snapshot().design_revision
    _no_physics(monkeypatch)
    assert controller.canContinueOptimization
    jobs = intercept_start(review, monkeypatch)
    assert controller.continueOptimization({'max_cycles': 3, 'max_evaluations': 16, 'max_seconds': 12.}), controller.error
    full = controller._attempt_full_request
    assert full.warm_start_seed_index == 0
    assert full.seed_designs[0] == candidate
    assert full.objectives == request.objectives
    assert full.candidate_materials == request.candidate_materials
    assert full.frozen_material_snapshot == request.frozen_material_snapshot
    assert full.design_revision == prior_revision
    assert controller._synthesis_editable_snapshot == closure
    assert jobs[0].operation_id == 2 and jobs[0].job_id != 'review-job-1'
    assert len(review.refreshes) == 1
    controller._busy = False
    controller._worker = None


def test_active_resource_rebind_without_revision_change_disables_continue(review):
    retain(review)
    before = review.source.session.snapshot()
    review.source.session._path = str(review.path.with_name('other.cfilm'))
    assert review.source.session.snapshot().design_revision == before.design_revision
    assert not review.controller.canContinueOptimization
    assert '资源' in str(review.controller.property('continueDisabledReason'))


def test_stale_managed_handle_is_not_silently_downgraded_to_anonymous(review, monkeypatch):
    def missing(_path):
        raise ValueError('managed membership changed')
    monkeypatch.setattr(review.service, 'managed_film_identity', missing, raising=False)
    with pytest.raises(ValueError, match='membership changed'):
        build_source_binding(review.service, review.base, review.request)


def test_real_solver_synced_expression_cancel_can_continue_and_applies_once(review, monkeypatch):
    from threading import Event
    from coat_sim.core.film_optimization import (
        FilmOptimizationRequest, freeze_film_optimization_request, optimize_film_design)
    from coat_sim.core.film_optimization_continuation import build_optimization_warm_restart
    from tests.test_film_optimization_continuation import _no_physics

    original = deepcopy(dict(review.base.design))
    symbol = deepcopy(original['front_layers'][0])
    symbol.pop('id')
    original['expression'] = {'text': 'A', 'symbols': {'A': symbol}, 'in_sync': True}
    assert review.source.session.request_replace_design(original)
    review.base = review.source.session.snapshot()
    assert review.base.design['expression']['in_sync'] is True
    review.request = freeze_film_optimization_request(review.service,
        replace(review.request, design_snapshot=review.base.design,
            design_revision=review.base.design_revision, frozen_material_snapshot=None))
    review.controller._load_source(preserve_variables=False)
    cancelled = Event()
    checkpoints = []

    def checkpoint(value):
        checkpoints.append(deepcopy(value))
        cancelled.set()

    result = optimize_film_design(review.service, review.request,
        cancel_event=cancelled, checkpoint_callback=checkpoint)
    assert len(checkpoints) == 1 and result.state == 'cancelled'
    assert result.evaluations == 1 and result.raw_coverage
    assert result.best_design['expression'] == {**original['expression'], 'in_sync': False}
    # Cancel delivery, validation and warm-restart construction must not replay
    # optics. The source's mocked spectrum request remains the only refresh.
    _no_physics(monkeypatch)
    arm(review)
    review.controller.cancelOptimization()
    deliver(review, result)
    assert review.controller._cancel_candidate_verified()
    assert review.controller.canContinueOptimization, review.controller.continueDisabledReason
    actual = review.source.session.snapshot()
    assert actual.design['expression']['in_sync'] is False
    assert actual.design_revision == review.base.design_revision + 1
    assert len(review.refreshes) == 1
    plan = build_optimization_warm_restart(review.request, result,
        design_revision=actual.design_revision, max_iterations=3, max_evaluations=12)
    assert isinstance(plan.request, FilmOptimizationRequest)
    assert plan.request.design_snapshot == result.best_design
    jobs = intercept_start(review, monkeypatch)
    assert review.controller.continueOptimization({'max_iterations': 3, 'max_evaluations': 12})
    assert jobs[0].request.design_snapshot == result.best_design
    assert len(review.refreshes) == 1
    review.controller._busy = False
    review.controller._worker = None
    assert review.source.undoOptimizedDesign()
    assert review.source.session.snapshot().design == review.base.design
    assert not review.source.undoOptimizedDesign()


@pytest.mark.parametrize('modification', ['text', 'symbol_thickness', 'symbol_note', 'extra_expression_field'])
def test_synced_expression_detachment_does_not_allow_other_changes(review, modification):
    original = deepcopy(dict(review.request.design_snapshot))
    symbol = deepcopy(original['front_layers'][0])
    symbol.pop('id')
    original['expression'] = {'text': 'A', 'symbols': {'A': symbol}, 'in_sync': True}
    review.controller._frozen_request = replace(review.request, design_snapshot=original)
    candidate = deepcopy(original)
    candidate['expression']['in_sync'] = False
    assert review.controller._matches_frozen_fixed_topology(candidate)
    if modification == 'text':
        candidate['expression']['text'] = 'AA'
    elif modification == 'symbol_thickness':
        candidate['expression']['symbols']['A']['thickness']['value'] += 1
    elif modification == 'symbol_note':
        candidate['expression']['symbols']['A']['note'] = 'not the original metadata'
    else:
        candidate['expression']['other'] = 'not allowed'
    assert not review.controller._matches_frozen_fixed_topology(candidate)


@pytest.mark.parametrize('text, in_sync', [('', True), ('A', False)])
def test_expression_flag_normalization_is_only_the_real_solver_transition(review, text, in_sync):
    original = deepcopy(dict(review.request.design_snapshot))
    original['expression'] = {'text': text, 'symbols': {}, 'in_sync': in_sync}
    review.controller._frozen_request = replace(review.request, design_snapshot=original)
    candidate = deepcopy(original)
    candidate['expression']['in_sync'] = not in_sync
    assert not review.controller._matches_frozen_fixed_topology(candidate)
