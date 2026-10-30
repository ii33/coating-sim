"""Independent reduction UI, explicit apply, and source transaction boundaries."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_reduction import (
    FilmReductionRequest, FilmReductionResult, freeze_film_reduction_request, reduce_film_design,
    film_reduction_result_to_payload,
)
from coat_sim.ui.film_optimization_quick_controller import (
    _film_result_from_queue, _AngleGridPreparationTask,
)
from tests.test_film_optimization_cancel_apply_review import app as app, review as review
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tests.test_film_optimization_run_contract import attempt_harness as attempt_harness, _HeldWorker, _wait, _dispose
from tests.film_structure_fixtures import fixture

pytestmark = pytest.mark.qt


def audited(h):
    objective = FilmOptimizationObjective(550., 552., 3,
        pointwise=FilmObjectiveConstraint(mode='range', lower_bound=0., upper_bound=1.))
    request = freeze_film_reduction_request(h.service,
        FilmReductionRequest(replace(h.request, objectives=(objective,), max_evaluations=8)))
    result = reduce_film_design(h.service, request)
    assert result.summary['candidate_feasible'] and result.summary['reduction_achieved']
    return request, result


def finish(h, request, result, *, error=None, before_completion=None):
    c = h.controller
    assert c.setSearchMode('reduction')
    c._operation_serial = 1
    c._frozen_request = request.base_request
    c._attempt_full_request = request
    c._operation_edit_epoch = h.source.pendingEditEpoch
    c._capture_cancel_attempt(1, 'reduction-job', {}, Event(), search_mode='reduction')
    if before_completion is not None:
        before_completion(c)
    c._on_finished(1, result, [], error)
    return c


@pytest.mark.parametrize('state', ['cancelled', 'failed'])
def test_reduction_cancel_failure_retains_explicit_apply_and_undo(review, state):
    h = review
    request, result = audited(h)
    if state != 'completed':
        result = replace(result, base_result=replace(result.base_result,
            state=state, termination_reason=state), is_final=False)
    c = finish(h, request, result, error='controlled failure' if state == 'failed' else None)
    assert h.source.session.snapshot().design_revision == h.base.design_revision
    assert h.source.session.snapshot().design['front_layers'] == h.base.design['front_layers']
    c._cancel_intent_accepted = c._cancel_intent_requested = True
    assert c._accept_cancel_intent() is False
    assert c._apply_cancel_candidate_once() is False
    assert c.canApply
    assert '原设计' in c.reductionComparison and '独立加密' in c.reductionComparison
    assert c.applyCandidate(), c.error
    assert h.source.session.snapshot().design['front_layers'] == []
    assert not c.applyCandidate()
    assert h.source.canUndoOptimizedDesign
    assert h.source.undoOptimizedDesign()
    assert h.source.session.snapshot().design['front_layers'] == h.base.design['front_layers']
    assert h.path.read_bytes()
    assert not h.saves


@pytest.mark.parametrize('change', ['epoch', 'revision', 'materials', 'request'])
def test_reduction_rejects_changed_source(review, change):
    h = review
    request, result = audited(h)
    def mutate(c):
        if change == 'epoch':
            h.source.markEditingDirty()
        elif change == 'revision':
            assert h.source.session.set_design_value('name', 'changed')
        elif change == 'materials':
            c._attempt_materials_owner = object()
        else:
            c._attempt_full_request = replace(request, mode='thickness')
    c = finish(h, request, result, before_completion=mutate)
    before = deepcopy(h.source.session.snapshot().design)
    assert not c.applyCandidate()
    assert h.source.session.snapshot().design == before


def test_reduction_unverified_or_unimproved_is_read_only(review):
    request, result = audited(review)
    result = replace(result, candidate_verified=False)
    c = finish(review, request, result)
    assert not c.canApply
    assert not c.applyCandidate()
    assert not c.confirmApplyCandidate()


def test_reduction_modes_config_qml_and_frozen_request(app, tmp_path):
    service, _, design = fixture(tmp_path)
    with optimizer(service, design) as (c, source, session):
        assert c.setSearchMode('reduction')
        meta = c.metaObject()
        assert meta.property(meta.indexOfProperty('reductionModeOptions')).typeName() == 'QVariantList'
        assert c.setReductionMode('thickness')
        assert c.setVariableBound('front', design['front_layers'][0]['id'], 'lower_nm', 0.)
        saved = c.exportOptimizationConfiguration()
        request = c._build_reduction_request()
        assert request.mode == 'thickness' and request.base_request.solver == 'baseline'
        assert request.base_request.variables[0].lower_nm == 0.
        assert c.setReductionMode('layer_count')
        assert c.loadOptimizationConfiguration(saved), c.error
        assert c.searchMode == 'reduction' and c.reductionMode == 'thickness'
        with window_runtime(app, c) as (window, warnings):
            from PySide6.QtCore import QObject
            assert window.findChild(QObject, 'filmReductionModeBox') is not None
            assert window.findChild(QObject, 'filmReductionComparisonLabel') is not None
            assert not [w for w in warnings if 'Error' in w]
        frozen = freeze_film_reduction_request(service, request)
        result = reduce_film_design(service, frozen)
        decoded = _film_result_from_queue(film_reduction_result_to_payload(frozen, result))
        assert isinstance(decoded, FilmReductionResult)
        assert decoded.frozen_request.mode == 'thickness'
        assert decoded.metrics == result.metrics


def test_automatic_repair_is_inline_algorithm_mode_and_routes_existing_worker(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject
    service, _, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        session = controller.session
        original_workflow_configuration = session.workflow_configuration
        monkeypatch.setattr(session, 'workflow_configuration',
            lambda scope: {} if scope == 'simulation' else original_workflow_configuration(scope))
        assert any(row['id'] == 'automatic_repair' for row in controller.searchModeOptions)
        assert controller.setSearchMode('automatic_repair')
        repair = controller.manufacturingController
        assert repair._prepared_signature is not None
        assert set(repair.automaticOptions) == {
            'lower_limit_nm', 'optical_tolerance_percentage_points'}
        assert repair.setAutomaticOption('lower_limit_nm', '12')
        assert repair.setAutomaticOption('optical_tolerance_percentage_points', '1')
        assert repair.canStartAutomatic, repr({
            'busy': repair.busy, 'closed': repair._closed,
            'unknown_submission': repair._unknown_submission,
            'owner_busy': controller.busy, 'owner_shutting_down': controller._shutting_down,
            'source_closed': controller._source_closed, 'source_stale': controller._source_stale,
            'repair_stale': repair._stale, 'invalidated': repair._source_invalidated.is_set(),
            'prepared': repair._prepared_signature is not None, 'rows': len(repair._rows),
            'options': repair.options, 'validated_options': repair._validated_options(),
            'error': repair.error,
        })
        configuration = controller.exportOptimizationConfiguration()
        assert configuration['search_mode'] == 'automatic_repair'
        assert configuration['version'] == 3
        legacy_auto = deepcopy(configuration)
        legacy_auto['version'] = 2
        assert not controller.loadOptimizationConfiguration(legacy_auto)
        assert controller.exportOptimizationConfiguration() == configuration

        started = []
        def start_repair():
            started.append('repair')
            return True
        monkeypatch.setattr(repair, 'startAutomatic', start_repair)
        monkeypatch.setattr(controller, '_start_optimization',
                            lambda **_kwargs: pytest.fail('repair must not enter normal optimizer'))
        assert controller.startOptimization()
        assert started == ['repair']

        with window_runtime(app, controller) as (window, warnings):
            tabs = window.findChild(QObject, 'filmOptimizationSetupTabs')
            assert tabs is not None and tabs.setProperty('currentIndex', 2)
            panel = window.findChild(QObject, 'filmManufacturingAlgorithmOptions')
            assert panel is not None and panel.property('visible')
            object_names = [str(item.objectName()) for item in window.findChildren(QObject)]
            repair_names = [n for n in object_names if 'filmManufacturing' in n]
            assert 'filmManufacturingStatus' in object_names, repair_names
            repeater = window.findChild(QObject, 'filmManufacturingParameterRepeater')
            assert repeater is not None and repeater.property('count') == 2, repair_names
            assert window.findChild(QObject, 'filmManufacturingOpenButton') is None
            assert not [warning for warning in warnings if 'Error' in warning or 'TypeError' in warning]


def test_reduction_large_angle_preparation_keeps_envelope(app, tmp_path):
    service, _, design = fixture(tmp_path)
    with optimizer(service, design) as (c, _, __):
        assert c.setSearchMode('reduction')
        assert c.setVariableBound('front', design['front_layers'][0]['id'], 'lower_nm', 0.)
        task = _AngleGridPreparationTask(tuple(c._objectives),
            c._build_reduction_request(prepared_angles=((0.,),)), 'reduction', service, Event(), 1)
        replies = []
        task.signals.finished.connect(lambda *args: replies.append(args))
        task.run()
        assert replies and replies[0][3] is None, replies
        prepared = replies[0][1]
        assert isinstance(prepared.request, FilmReductionRequest)
        assert prepared.spec['parameters']['payload']['request']['kind'] == 'coat_sim.film_reduction_request'


def test_reduction_save_retains_candidate_after_source_drift_and_never_applies(review):
    request, result = audited(review)
    c = finish(review, request, result)
    review.source.markEditingDirty()
    before = deepcopy(review.source.session.snapshot().design)
    assert c.canSave and not c.canApply
    assert c.saveCandidatePath(str(review.path.parent / 'reduction-review.cfilm'))
    assert review.saves
    assert review.source.session.snapshot().design == before


def test_reduction_queue_submission_deduplicates_active_attempt(attempt_harness):
    make_controller, stores, _ = attempt_harness
    c, source = make_controller()
    try:
        assert c.setSearchMode('reduction')
        assert c.setReductionMode('thickness')
        assert c.startOptimization(), c.error
        assert not c.startOptimization()
        _wait(lambda: len(stores[0].submissions) == 1)
        spec, job_id, metadata = stores[0].submissions[0]
        assert spec['parameters']['payload']['request']['kind'] == 'coat_sim.film_reduction_request'
        assert metadata['input_summary']['reduction_mode'] == 'thickness'
        worker = _HeldWorker.instances[-1]
        worker.finish(error={'state': 'cancelled', 'message': 'controlled'})
        _wait(lambda: not c.busy)
        assert not source.applied
    finally:
        _dispose(c)


def test_reduction_uses_optimizer_mode_without_duplicate_design_button(review, app):
    from pathlib import Path
    from tests.test_film_optimization_restore_layout import window_runtime
    h = review
    qml = Path(__file__).resolve().parents[1] / 'coat_sim/ui/qml/FilmDesignQuick.qml'
    assert 'filmDesignReductionButton' not in qml.read_text(encoding='utf-8')
    reduction_index = next(i for i, row in enumerate(h.controller.searchModeOptions)
                           if row['id'] == 'reduction')
    assert h.controller.setSearchMode('reduction')
    with window_runtime(app, h.controller) as (window, _warnings):
        from PySide6.QtCore import QObject
        mode = window.findChild(QObject, 'filmOptimizationSearchModeBox')
        assert mode is not None and mode.property('currentIndex') == reduction_index


def test_reduction_apply_uses_audited_result_without_new_optical_evaluations(review, monkeypatch):
    request, result = audited(review)
    def no_optics(*args, **kwargs):
        raise AssertionError('apply must not start optical simulation')
    monkeypatch.setattr(review.service, 'simulate', no_optics)
    c = finish(review, request, result)
    assert c.hasReductionResult and c._candidate_applied, c.error
    assert not c.applyCandidate()


def test_reduction_no_improvement_kept_for_save_but_never_apply(review):
    request, result = audited(review)
    baseline_request = replace(request, base_request=replace(request.base_request,
        variables=tuple(replace(v, lower_nm=100.) for v in request.base_request.variables)),
        mode='layer_count')
    retained = reduce_film_design(review.service, baseline_request)
    assert not retained.summary['reduction_achieved']
    c = finish(review, baseline_request, retained)
    assert c.canSave and not c.canApply
    assert not c.applyCandidate()


def test_reduction_layer_mode_requires_explicit_deletable_selection(app, tmp_path):
    service, _, design = fixture(tmp_path)
    with optimizer(service, design) as (c, _, __):
        assert c.setSearchMode('reduction') and c.setReductionMode('layer_count')
        layer_id = design['front_layers'][0]['id']
        assert c.setVariableBound('front', layer_id, 'lower_nm', 1.)
        assert not c.canStart
        assert '0 nm' in c._active_configuration_error()
        assert '不可删除' in c.reductionPolicySummary
        assert c.setReductionMode('combined')
        assert c.canStart, c._active_configuration_error()
        assert '仍可尝试减厚' in c.reductionPolicySummary
        assert c.setVariableBound('front', layer_id, 'lower_nm', 0.)
        assert c.setReductionMode('layer_count')
        assert c.canStart, c._active_configuration_error()
        assert '总厚度可以增加' in c.reductionPolicySummary


def test_reduction_entry_mode_consumed_before_next_normal_and_f6_constructor(review, monkeypatch):
    from PySide6.QtCore import QObject
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    h = review
    h.controller.detach_observers()
    controllers = []
    starts = []

    class Opener(QObject):
        current: FilmOptimizationQuickController | None = None

        def open_or_reuse_film_companion(self, module_key, source, *, start=False):
            assert module_key == 'film_optimization' and source is h.source
            if self.current is None:
                self.current = FilmOptimizationQuickController(source)
                controllers.append(self.current)
                controller = self.current

                def record_start() -> bool:
                    starts.append(controller.searchMode)
                    return True

                monkeypatch.setattr(controller, 'startOptimization', record_start)
            if start:
                self.current.startOptimizationFromShortcut()
            return 'test-optimizer'

        def close_current(self):
            assert self.current is not None
            self.current.shutdown(1000)
            self.current = None

    opener = Opener()
    h.source.setParent(opener)
    try:
        assert h.source.openReductionOptimization() == 'test-optimizer'
        assert opener.current is not None and opener.current.searchMode == 'reduction'
        assert h.source._requested_optimizer_search_mode == ''
        opener.close_current()
        # The saved workflow still says reduction. The ordinary entry must
        # explicitly select its own mode rather than inherit that old intent.
        assert h.source.openOptimization() == 'test-optimizer'
        assert opener.current is not None and opener.current.searchMode == 'fixed_topology'
        assert h.source._requested_optimizer_search_mode == ''
        opener.close_current()
        assert h.source.openOptimizationAndStart() == 'test-optimizer'
        assert starts == ['fixed_topology']
        assert h.source._requested_optimizer_search_mode == ''
    finally:
        h.source.setParent(None)
        for controller in controllers:
            controller.shutdown(1000)
            controller._compute_target._executor.shutdown(wait=True, cancel_futures=True)
            controller.deleteLater()


def test_normal_entry_and_f6_preserve_existing_reduction_candidate(review, monkeypatch):
    from PySide6.QtCore import QObject
    h = review
    request, result = audited(h)
    result = replace(result, is_final=False)
    c = finish(h, request, result)
    retained = c._result
    starts = []

    def record_start() -> bool:
        starts.append(True)
        return True

    monkeypatch.setattr(c, 'startOptimization', record_start)

    class Opener(QObject):
        def open_or_reuse_film_companion(self, module_key, source, *, start=False):
            if start:
                assert c.startOptimizationFromShortcut() is False
            return 'existing-optimizer'

    opener = Opener()
    h.source.setParent(opener)
    try:
        assert h.source.openOptimization() == 'existing-optimizer'
        assert c.searchMode == 'reduction' and c._result is retained
        assert h.source._requested_optimizer_search_mode == ''
        assert h.source.openOptimizationAndStart() == 'existing-optimizer'
        assert not starts
        assert c.searchMode == 'reduction' and c._result is retained
        assert c._reduction_result is result and c.canSave
        assert h.source._requested_optimizer_search_mode == ''
        assert h.source.session.snapshot().design_revision == h.base.design_revision
    finally:
        h.source.setParent(None)
