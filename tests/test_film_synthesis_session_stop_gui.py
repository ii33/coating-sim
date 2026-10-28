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
    text = FilmOptimizationQuickController._synthesis_session_stop_text(view)
    assert '独立审计未通过原门限' in text
    assert '未确认局部收敛' in text and '目标仍未全部达到' in text
    assert '保留完整最佳候选' in text and '继续搜索新结构' in text
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
        assert '目标仍未全部达到' in controller.statusText
        assert '继续搜索新结构' in controller.statusText
    assert '目标仍未全部达到' in controller.synthesisAuditSummary
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
