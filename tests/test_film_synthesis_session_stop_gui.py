"""A complete audited local session may finish below the explicit targets."""
from copy import deepcopy
import json
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtTest import QTest

from coat_sim.core import material_business
from coat_sim.core.film_candidate_display import candidate_physical_hash
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.materials import default_material_profile
from coat_sim.scheduler import SchedulerClient
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_synthesis_search_state import phase_label
from tests.test_film_optimization_autoapply import app as app, isolated_case
from tests.test_film_optimization_cancel_spectrum import wait_done
from tests.test_film_optimization_needle import _optical
from tests.test_film_synthesis_gui_review import (
    _optimizer, _wait, _Scene, project_gui as project_gui,
)
from tests.test_film_synthesis_search_gui import REOPEN

pytestmark = [pytest.mark.qt, pytest.mark.contract]
STOP_REASONS = {'local_session_converged_targets_unmet', 'local_session_stalled_targets_unmet',
                'local_session_terminal_converged_best_retained_targets_unmet',
                'local_session_audit_rejected_targets_unmet'}


def test_audit_rejected_session_description_does_not_claim_convergence_or_stagnation():
    view = cast(FilmOptimizationQuickController, SimpleNamespace(_synthesis_payload={'result': {
        'state': 'completed', 'targets_met': False,
        'termination_reason': 'local_session_audit_rejected_targets_unmet'}}))
    view._synthesis_continue_hint = lambda view=view: FilmOptimizationQuickController._synthesis_continue_hint(view)
    text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
    assert '独立审计未通过原门限' in text
    assert '未确认局部收敛' in text and '目标仍未全部达到' in text
    assert '保留完整最佳候选' in text and '没有可续接的结构进度' in text
    assert '已收敛' not in text and '停滞' not in text and '无有效改进' not in text


def _unmet_settings(controller):
    # An explicit strict AR target leaves a useful but unmet early topology.
    # Both materials leave genuine topology space for subsequent user sessions.
    row_id = controller.addSynthesisMaterialRow()
    assert row_id
    assert controller.setSynthesisRowMaterial(row_id, 'review-later-material')
    assert controller.setSynthesisRowOpticalSet(row_id, 'later-set')
    # Clear the small fixture budgets: the production local-session stop must act.
    for field in ('max_cycles', 'max_evaluations', 'max_seconds'):
        assert controller.setSynthesisOption(field, '')
    assert controller.setObjectiveField('wavelength_min_nm', '520')
    assert controller.setObjectiveField('wavelength_max_nm', '580')
    assert controller.setObjectiveField('pointwise.upper_bound_percent', '0.0000001')
    assert controller.setObjectiveField('wavelength_samples', '5')
    request = controller._build_synthesis_request()
    assert request.max_cycles is request.max_evaluations is request.max_seconds is None
    assert not request.objectives[0].mean.enabled
    assert request.objectives[0].pointwise.upper_bound == pytest.approx(0.000000001, rel=1e-15, abs=0.)
    controller.liveDisplay.setEnabled(True)


def _finish_unmet(controller, scene, *, continuing=False):
    previous = controller.queueJobId
    jobs: list[str] = []
    def observe_job():
        if controller.queueJobId and controller.queueJobId != previous:
            jobs.append(controller.queueJobId)
    controller.changed.connect(observe_job)
    try:
        scene.click(scene.item('filmSynthesisContinueSearchButton' if continuing
                               else 'filmOptimizationStartButton'))
        _wait(lambda: bool(jobs) or bool(controller.error))
        assert jobs, (controller.error, controller.statusText, controller.progressText)
    finally:
        controller.changed.disconnect(observe_job)
    _wait(lambda: not controller.busy, seconds=60.)
    QTest.qWait(300)
    payload = controller._synthesis_payload
    assert payload is not None, controller.error
    raw = payload['result']
    assert raw['state'] == 'completed', (raw['state'], raw['termination_reason'], controller.error)
    assert raw['termination_reason'] in STOP_REASONS
    assert raw['targets_met'] is False and raw['validation_complete'] is True
    assert payload['best_candidate_audit']['available'] is True
    assert payload['best_candidate_audit']['targets_met'] is False
    assert payload['best_candidate_audit']['audit_max_normalized_error'] > 0.
    assert controller._candidate_applied, controller.statusText
    assert controller.synthesisPhase == '本轮完成'
    assert controller.canContinueSynthesisSearch, controller.synthesisSearchDisabledReason
    if controller.error:
        # The lightweight project_gui has no application window host. Preserve
        # its real refresh failure; the full desktop path is checked separately.
        assert controller.error == '优化厚度已应用，但光谱刷新未完成：光谱仿真窗口未确认打开'
    else:
        assert ('目标仍未全部达到' in controller.statusText or '目标未达成' in controller.statusText)
        assert '点击“继续优化”可继续搜索新结构' in controller.statusText
    assert ('目标仍未全部达到' in controller.synthesisAuditSummary
            or '目标未达成' in controller.synthesisAuditSummary)
    assert controller.conditionErrors and any(not row['targets_met'] for row in controller.synthesisObjectiveAudit)
    assert controller._result is not None
    assert physical_design_hash(controller._result.best_design) == physical_design_hash(raw['candidate_design'])
    details = scene.item('filmOptimizationBestDetails')
    assert details.isVisible() and details.width() > 100. and details.height() > 0.
    assert '总厚' in details.property('text') and '均值 ' in details.property('text')
    best = controller._live_display._packet['best']
    assert controller._live_display._binding[1] == controller.queueJobId
    thicknesses = [row['thickness']['value'] for side in ('front_layers', 'back_layers')
                   for row in raw['candidate_design'][side]]
    assert best['physical_hash'] == candidate_physical_hash(raw['candidate_design'], thicknesses)
    assert best['statistics']['physical_hash'] == best['physical_hash']
    assert best['max_normalized_error'] == controller._result.max_normalized_error
    assert controller.plotData['candidate']
    return deepcopy(controller._synthesis_search_reference), deepcopy(controller._synthesis_search_record)


def test_real_qml_unmet_stop_cancel_and_two_continues_keep_current_best_and_frontier(app, project_gui, monkeypatch):
    harness, _host, _workspace, _store, _library, _saved = project_gui
    _service, source, controller, scene = _optimizer(harness)
    _unmet_settings(controller)
    reference, record = _finish_unmet(controller, scene)
    assert source.saveCurrent(), source.session.snapshot().error

    # Cancel a fresh continuation before admission. The previous best is never
    # presented as this attempt's candidate, and its durable frontier survives.
    entered, release = Event(), Event()
    verifier = controller._compute_target.admission_verifier
    def held_admission(*args):
        verify, current = verifier(*args)
        def verify_when_released():
            verify()
            entered.set()
            assert release.wait(5.)
        return verify_when_released, current
    with monkeypatch.context() as held:
        held.setattr(controller._compute_target, 'admission_verifier', held_admission)
        try:
            scene.click(scene.item('filmSynthesisContinueSearchButton'))
            _wait(entered.is_set)
            assert controller._live_display._packet == {}
            scene.click(scene.item('filmOptimizationCancelButton'))
        finally:
            release.set()
        _wait(lambda: not controller.busy, seconds=30.)
    assert controller._synthesis_search_reference == reference
    assert controller._live_display._packet == {}
    assert '收敛' not in controller.statusText
    assert controller.synthesisPhase == '本轮完成'
    assert controller.canContinueSynthesisSearch, controller.synthesisSearchDisabledReason

    for index in range(2):
        before = record['checkpoint']
        if index == 0:
            layer = source.currentDesign()['front_layers'][0]
            assert source.setLayerValue('front', layer['id'], 'thickness.value', layer['thickness']['value'] * 1.001)
            edited = deepcopy(source.currentDesign())
        reference, record = _finish_unmet(controller, scene, continuing=True)
        assert record['request'].search_checkpoint['sha256'] == before['sha256']
        if index == 0:
            assert record['request'].seed_designs[0]['front_layers'] == edited['front_layers']
        after = record['checkpoint']
        assert after['cursors'] != before['cursors'] or after['tried'] != before['tried']
        assert controller._synthesis_search_reference['job_id'] == reference['job_id']
        assert source.saveCurrent(), source.session.snapshot().error


def test_unmet_session_saved_project_reopens_in_new_process_and_continues_after_edit(app, project_gui, tmp_path):
    harness, host, workspace, store, library, saved = project_gui
    _service, source, controller, scene = _optimizer(harness)
    _unmet_settings(controller)
    reference, record = _finish_unmet(controller, scene)
    assert source.saveCurrent(), source.session.snapshot().error
    assert controller.shutdown(5000)
    scene.close()
    harness[2].remove(controller)
    harness[3].remove(scene)
    source.markSourceClosed()
    assert host.close_project(workspace.binding, prepare_close=lambda *_: True)
    output = tmp_path / 'reopened-unmet.json'
    data = dict(root=str(store.root), library=str(library.directory), document=saved.document_id,
        settings=str(tmp_path / 'reopened-unmet.ini'), output=str(output), reference=reference,
        checkpoint=record['checkpoint'])
    child = subprocess.run([sys.executable, '-c', REOPEN], input=json.dumps(data),
                           encoding='utf8', capture_output=True, timeout=90.)
    assert child.returncode == 0, child.stdout + '\n' + child.stderr
    reopened = json.loads(output.read_text())
    assert reopened['job'] != reference['job_id'] and reopened['applied'] is True
    assert reopened['phase'] == '本轮完成'
    assert (reopened['checkpoint']['cursors'] != record['checkpoint']['cursors']
            or reopened['checkpoint']['tried'] != record['checkpoint']['tried'])


def test_real_unmet_session_refreshes_existing_project_spectrum_and_same_best_details(app, tmp_path, monkeypatch):
    with isolated_case(tmp_path, monkeypatch) as case:
        profile = default_material_profile('Second synthetic session material', id='review-later-material')
        profile.update(category='film', optical_constants=[_optical('later-set', 1.8)])
        material_business.application_material_library().write(profile)
        case.service.refresh_materials()
        controllers: list[FilmOptimizationQuickController] = []
        scenes: list[_Scene] = []
        try:
            _service, source, controller, scene = _optimizer((case.service, case.source, controllers, scenes))
            _unmet_settings(controller)
            _finish_unmet(controller, scene)
            assert not controller.error, controller.error
            wait_done(case.simulation)
            applied = source.session.snapshot()
            assert case.simulation._design == applied.design
            assert case.simulation._source_design_revision == applied.design_revision
            assert physical_design_hash(case.calls[-1]['design']) == physical_design_hash(applied.design)
            assert physical_design_hash(applied.design) == physical_design_hash(controller._result.best_design)
            assert {row['layer_id'] for row in controller.variables} == {
                row['id'] for row in applied.design['front_layers']}
            assert all(row['enabled'] and row['available'] for row in controller.variables)
        finally:
            for controller in controllers:
                controller.cancelOptimization()
                controller.shutdown(5000)
            for scene in scenes:
                scene.close()
            client = SchedulerClient(case.service.root)
            if client.endpoint.exists():
                client.stop()


@pytest.mark.parametrize('stage', ['local_margin_start', 'local_session_start', 'local_refinement', 'canonical_local'])
def test_new_local_stage_messages_use_existing_local_optimization_phase(stage):
    assert phase_label(stage + ' | current fixed topology') == '局部优化'


def test_retired_historical_synthesis_is_view_only_before_source_or_identity_checks():
    view = cast(FilmOptimizationQuickController, SimpleNamespace())
    for request, message in (
            (SimpleNamespace(search_strategy='legacy', max_working_set_bytes=None), '旧独立路线'),
            (SimpleNamespace(search_strategy='hybrid', max_working_set_bytes=0), '数值护栏')):
        reason = FilmOptimizationQuickController._continuation_source_reason(
            view, {'mode': 'synthesis', 'request': request})
        assert message in reason and '原历史保留' in reason


def test_session_stop_only_promises_continue_when_guard_accepts_reference():
    payload = {'result': {'state': 'completed', 'targets_met': False,
               'termination_reason': 'local_session_converged_targets_unmet'}}
    for reason, expected in (('', '点击“继续优化”可继续搜索新结构'),
                             ('来源已修改', '继续优化暂不可用：来源已修改')):
        view = cast(FilmOptimizationQuickController, SimpleNamespace(
            _synthesis_payload=payload, _synthesis_search_reference={'job_id': 'j'},
            _synthesis_search_reason=lambda reason=reason: reason))
        view._synthesis_continue_hint = lambda view=view: FilmOptimizationQuickController._synthesis_continue_hint(view)
        text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
        assert '局部优化已收敛' in text and '目标仍未全部达到' in text
        assert expected in text


@pytest.mark.parametrize('status,extra,expected', [
    ('paused_local', {}, '局部收敛；本轮暂停'),
    ('paused_no_new_candidate', {}, '本轮没有新候选；本轮暂停'),
    ('paused_unresolved', {}, '本轮暂停，目标未达成'),
    ('declared_frontier_exhausted', {'search_complete': True, 'declared_frontier_exhausted': True},
     '有限搜索策略前沿耗尽'),
])
def test_until_target_status_never_equates_local_or_quantum_pause_with_success(status, extra, expected):
    evidence = {'enabled': True, 'status': status, 'goal_met': False,
                'search_complete': False, 'continuation_checkpoint_available': True, **extra}
    raw = {'state': 'completed', 'targets_met': False, 'frozen_request': {'search_until_target': True},
           'termination_reason': ('continuation_frontier_exhausted' if status == 'declared_frontier_exhausted' else 'local_session_converged_targets_unmet'),
           'performance': {'search': {'until_target': evidence, 'hybrid': {'resource_limited': False,
               'continuation_session': {'frontier_exhausted': status == 'declared_frontier_exhausted'}}}}}
    view = cast(FilmOptimizationQuickController, SimpleNamespace(
        _synthesis_payload={'result': raw}, _synthesis_search_reference={'job_id': 'this-job'},
        _queue_job_id='this-job', _synthesis_search_reason=lambda: ''))
    view._synthesis_continue_hint = lambda view=view: FilmOptimizationQuickController._synthesis_continue_hint(view)
    text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
    assert expected in text
    assert '已达标' not in text
    if status.startswith('paused'):
        assert '搜索尚未完成' in text and '点击“继续优化”' in text
        assert FilmOptimizationQuickController._synthesis_task_state_label(raw) == '本轮暂停，搜索尚未完成'
    else:
        assert '不证明物理上不存在可行方案' in text


def test_until_target_stale_checkpoint_is_not_offered_as_current():
    raw = {'state': 'completed', 'targets_met': False, 'frozen_request': {'search_until_target': True}, 'performance': {'search': {
        'until_target': {'enabled': True, 'status': 'paused_no_new_candidate',
                         'continuation_checkpoint_available': True}}}}
    view = cast(FilmOptimizationQuickController, SimpleNamespace(
        _synthesis_payload={'result': raw}, _synthesis_search_reference={'job_id': 'old-job'},
        _queue_job_id='current-job', _synthesis_search_reason=lambda: ''))
    view._synthesis_continue_hint = lambda view=view: FilmOptimizationQuickController._synthesis_continue_hint(view)
    text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
    assert '没有可续接的结构进度' in text and '点击“继续优化”' not in text


def test_only_proven_declared_frontier_exhaustion_disables_continuation():
    evidence = {'enabled': True, 'status': 'declared_frontier_exhausted',
                'search_complete': True, 'declared_frontier_exhausted': True}
    view = cast(FilmOptimizationQuickController, SimpleNamespace(
        _synthesis_search_reference={'job_id': 'j'}, _queue_job_id='j',
        _synthesis_payload={'result': {'targets_met': False, 'frozen_request': {'search_until_target': True},
            'termination_reason': 'continuation_frontier_exhausted', 'performance': {'search': {
                'until_target': evidence, 'hybrid': {'resource_limited': False,
                    'continuation_session': {'frontier_exhausted': True}}}}}},
        busy=True, _shutting_down=False, session=None))
    reason = FilmOptimizationQuickController._synthesis_search_reason(view)
    assert '有限策略前沿耗尽' in reason and '不证明' in reason
    evidence.update(status='paused_no_new_candidate', search_complete=False,
                    declared_frontier_exhausted=False, continuation_checkpoint_available=True)
    reason = FilmOptimizationQuickController._synthesis_search_reason(view)
    assert reason == '请先等待当前任务结束。'  # ordinary busy guard, not an exhaustion claim


def test_until_target_stalled_or_unverified_terminal_does_not_claim_convergence():
    for reason in ('local_session_stalled_targets_unmet',
                   'local_session_terminal_converged_best_retained_targets_unmet',
                   'local_session_audit_rejected_targets_unmet'):
        raw = {'state': 'completed', 'targets_met': False, 'frozen_request': {'search_until_target': True}, 'termination_reason': reason,
               'performance': {'search': {'until_target': {'enabled': True, 'status': 'paused_local',
                   'continuation_checkpoint_available': True}}}}
        view = cast(FilmOptimizationQuickController, SimpleNamespace(
            _synthesis_payload={'result': raw}, _synthesis_search_reference={'job_id': 'j'},
            _queue_job_id='j', _synthesis_search_reason=lambda: ''))
        view._synthesis_continue_hint = lambda view=view: FilmOptimizationQuickController._synthesis_continue_hint(view)
        text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
        assert '未确认局部收敛' in text and '搜索尚未完成' in text


def test_until_target_real_local_pause_waits_for_user_to_continue(app, project_gui):
    harness, _host, _workspace, _store, _library, _saved = project_gui
    _service, _source, controller, scene = _optimizer(harness)
    _unmet_settings(controller)
    assert controller.setSynthesisOption('search_until_target', True)
    reference, _record = _finish_unmet(controller, scene)
    evidence = controller._synthesis_payload['result']['performance']['search']['until_target']
    assert evidence['enabled'] and not evidence['goal_met'] and not evidence['search_complete']
    assert evidence['status'] == 'paused_local'
    old_job = controller.queueJobId
    QTest.qWait(500)
    assert not controller.busy and controller.queueJobId == old_job  # No automatic restart.
    assert controller.canContinueSynthesisSearch, controller.synthesisSearchDisabledReason
    scene.click(scene.item('filmSynthesisContinueSearchButton'))
    _wait(lambda: controller.queueJobId != old_job, seconds=30.)
    assert controller.queueJobId != old_job, (controller.error, controller.synthesisSearchDisabledReason)
    _wait(lambda: not controller.busy, seconds=60.)
    assert controller.queueJobId != reference['job_id']


def test_until_target_resource_failure_is_explicit_and_not_success():
    raw = {'state': 'failed', 'targets_met': False, 'frozen_request': {'search_until_target': True}, 'performance': {'search': {'until_target': {
        'enabled': True, 'status': 'blocked_resource', 'goal_met': False}}}}
    view = cast(FilmOptimizationQuickController, SimpleNamespace(_synthesis_payload={'result': raw}))
    text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
    assert '资源不足' in text and '目标尚未确认达成' in text
    assert FilmOptimizationQuickController._synthesis_task_state_label(raw) == '搜索因资源不足中断'


def test_until_target_presentation_rejects_forged_success_or_exhaustion_metadata():
    from copy import deepcopy
    from coat_sim.ui.film_optimization_quick_controller import _synthesis_until_evidence
    raw = {'state': 'completed', 'targets_met': False, 'validation_complete': True,
           'frozen_request': {'search_until_target': True}, 'termination_reason': 'search_exhausted',
           'performance': {'search': {'until_target': {'enabled': True, 'status': 'targets_met',
               'goal_met': True, 'search_complete': True}}}}
    facts = {'available': True, 'targets_met': True}
    assert _synthesis_until_evidence(raw, facts)['status'] == 'paused_unresolved'
    assert '已达成' not in FilmOptimizationQuickController._synthesis_task_state_label(raw, facts)
    raw['targets_met'] = True
    assert _synthesis_until_evidence(raw, {'available': True, 'targets_met': False})['status'] == 'paused_unresolved'
    assert FilmOptimizationQuickController._synthesis_task_state_label(raw, facts) == '搜索目标已达成'
    disabled = deepcopy(raw)
    disabled['frozen_request']['search_until_target'] = False
    assert _synthesis_until_evidence(disabled, facts) == {}
    assert '已达成' not in FilmOptimizationQuickController._synthesis_task_state_label(disabled, facts)
    raw['targets_met'] = False
    evidence = raw['performance']['search']['until_target']
    evidence.update(status='declared_frontier_exhausted', declared_frontier_exhausted=True)
    assert _synthesis_until_evidence(raw, facts)['status'] == 'paused_unresolved'
    raw['termination_reason'] = 'continuation_frontier_exhausted'
    raw['performance']['search']['hybrid'] = {'resource_limited': True,
                                               'continuation_session': {'frontier_exhausted': True}}
    assert _synthesis_until_evidence(raw, facts)['status'] == 'paused_unresolved'
    raw['performance']['search']['hybrid']['resource_limited'] = False
    assert _synthesis_until_evidence(raw, facts)['status'] == 'declared_frontier_exhausted'
