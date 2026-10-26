"""Terminal candidates update the bound draft and its real spectrum together."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
import os
from threading import Event

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import build_job_payload, canonical_parameters, execute_job
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_base_request, synthesis_result_view
from tests.test_film_optimization_cancel_apply_review import arm, cancelled_result, deliver
from tests.test_film_optimization_cancel_spectrum import spectrum_case, wait_done
from tests.film_structure_fixtures import fixture

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@contextmanager
def isolated_case(tmp_path, monkeypatch):
    from coat_sim.core import material_business
    _service, library, _design = fixture(tmp_path / 'synthetic-catalog')
    monkeypatch.setattr(material_business, 'application_material_library', lambda: library)
    with spectrum_case(tmp_path / 'case', monkeypatch, route='project') as case:
        yield case


def synthesis_terminal(case, tmp_path, *, cancel=False):
    environment = deepcopy(dict(case.base.design))
    environment['front_layers'] = []
    request = freeze_film_synthesis_request(case.service, FilmSynthesisRequest(
        environment, (FilmOptimizationObjective(550., 550., 1,
            pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.001)),),
        (FilmNeedleMaterialReference('acceptance-film', 'film-set'),),
        design_revision=case.base.design_revision, max_cycles=1, max_evaluations=60))
    case.controller._search_mode = 'synthesis'
    case.controller._synthesis_editable_snapshot = freeze_synthesis_editable_snapshot(
        case.service, case.base.design, request.frozen_material_snapshot)
    case.request = synthesis_base_request(request)
    event = Event()
    def progress(value, **kwargs):
        if cancel and value.get('stage') == 'incumbent' and value.get('layer_count') == 1:
            event.set()
    parameters = canonical_parameters(None, {'payload': film_synthesis_request_to_payload('autoapply', request)})
    payload = execute_job({'payload': build_job_payload(parameters)}, event, progress,
        tmp_path / 'checkpoint.json', lambda *_args: None, str(tmp_path))
    result = synthesis_result_view(payload)
    assert result.optimization_result is not None
    assert result.optimization_result.state == ('cancelled' if cancel else 'completed')
    return result


@pytest.mark.parametrize('mode', ['fixed_topology', 'synthesis'])
@pytest.mark.parametrize('cached', [False, True])
def test_completed_candidate_updates_project_draft_and_fresh_spectrum(app, tmp_path, monkeypatch, mode, cached):
    with isolated_case(tmp_path, monkeypatch) as case:
        if cached:
            case.simulation.requestSourceRefresh()
            wait_done(case.simulation)
        result = (synthesis_terminal(case, tmp_path) if mode == 'synthesis' else
            replace(cancelled_result(case), state='completed', termination_reason='completed'))
        arm(case)
        deliver(case, result)
        after = case.source.session.snapshot()
        assert after.design_revision == case.base.design_revision + 1, case.controller.statusText
        wait_done(case.simulation)
        assert case.simulation._design == after.design
        assert case.simulation._source_design_revision == after.design_revision
        assert len(case.calls) == 1 + int(cached)
        assert case.calls[-1]['design'] == after.design
        assert case.source.canUndoOptimizedDesign
        assert case.controller._candidate_applied
        if mode == 'synthesis':
            assert after.design['front_layers'][0]['id'] != case.base.design['front_layers'][0]['id']


def test_user_cancelled_synthesis_applies_verified_best_and_refreshes(app, tmp_path, monkeypatch):
    with isolated_case(tmp_path, monkeypatch) as case:
        result = synthesis_terminal(case, tmp_path, cancel=True)
        arm(case)
        case.controller.cancelOptimization()
        deliver(case, result)
        after = case.source.session.snapshot()
        assert after.design_revision == case.base.design_revision + 1, case.controller.statusText
        wait_done(case.simulation)
        assert case.simulation._design == after.design
        assert len(case.calls) == 1
        assert case.controller._candidate_applied


def test_completed_unmet_candidate_status_reports_actual_autoapply(app, tmp_path, monkeypatch):
    with isolated_case(tmp_path, monkeypatch) as case:
        result = replace(cancelled_result(case), state='completed', targets_met=False)
        arm(case)
        deliver(case, result)
        wait_done(case.simulation)
        after = case.source.session.snapshot()
        assert after.design_revision == case.base.design_revision + 1
        assert case.controller._candidate_applied and not case.controller._result.targets_met
        assert case.simulation._design == after.design
        assert all(text in case.controller.statusText for text in ('未全部达标', '已应用', '未保存', '刷新'))
        assert len(case.calls) == 1


@pytest.mark.parametrize('cancel', [False, True])
def test_numeric_cleanup_fallback_notice_survives_terminal_autoapply(app, tmp_path, monkeypatch, cancel):
    with isolated_case(tmp_path, monkeypatch) as case:
        result = synthesis_terminal(case, tmp_path, cancel=cancel)
        payload = deepcopy(dict(result.payload))
        payload['result']['performance']['search']['numeric_zero_fallback'] = dict(
            unpublished_better_trials=1, returned_training_complete=True,
            new_independent_audit_claimed=False)
        result = replace(result, payload=payload)
        arm(case)
        if cancel:
            case.controller.cancelOptimization()
        deliver(case, result)
        wait_done(case.simulation)
        assert case.controller._candidate_applied
        assert '未完成清理复算' in case.controller.statusText
        assert '已应用此前完整候选' in case.controller.statusText
        assert len(case.calls) == 1


@pytest.mark.parametrize('mode', ['fixed_topology', 'synthesis'])
@pytest.mark.parametrize('conflict', ['revision', 'edit_epoch', 'session', 'project', 'old_operation'])
def test_completed_candidate_never_overwrites_changed_target(app, tmp_path, monkeypatch, mode, conflict):
    with isolated_case(tmp_path, monkeypatch) as case:
        result = (synthesis_terminal(case, tmp_path) if mode == 'synthesis' else
            replace(cancelled_result(case), state='completed'))
        arm(case)
        if conflict == 'revision':
            changed = deepcopy(dict(case.base.design))
            changed['front_layers'][0]['thickness']['value'] = 155.
            assert case.source.session.apply_optimized_design(changed, case.base.design_revision)
        elif conflict == 'edit_epoch':
            case.source._pending_edit_epoch += 1
        elif conflict == 'session':
            from coat_sim.core.film_design_session import FilmDesignEditSession
            case.source.session = FilmDesignEditSession(case.service)
        elif conflict == 'project':
            monkeypatch.setattr(case.controller, '_project_current', lambda: False)
        before = case.source.session.snapshot()
        deliver(case, result, operation_id=0 if conflict == 'old_operation' else 1)
        after = case.source.session.snapshot()
        assert after.design == before.design
        assert after.design_revision == before.design_revision
        assert not case.calls


@pytest.mark.parametrize('kind', ['no_candidate', 'failed_candidate'])
def test_failed_terminal_never_writes_or_refreshes(app, tmp_path, monkeypatch, kind):
    with isolated_case(tmp_path, monkeypatch) as case:
        arm(case)
        result = None if kind == 'no_candidate' else replace(cancelled_result(case), state='failed')
        deliver(case, result, error={'state': 'failed', 'message': 'synthetic failure'})
        assert case.source.session.snapshot().design == case.base.design
        assert not case.calls


def test_real_qml_start_to_project_apply_and_spectrum(app, tmp_path, monkeypatch):
    from tests.test_film_synthesis_gui_review import _Scene, _wait
    from coat_sim.scheduler.client import SchedulerClient
    with isolated_case(tmp_path, monkeypatch) as case:
        controller = case.controller
        assert controller.setSearchMode('synthesis')
        assert controller.setObjectiveField('wavelength_min_nm', '550')
        assert controller.setObjectiveField('wavelength_max_nm', '550')
        assert controller.setObjectiveField('pointwise.upper_bound_percent', '0.1')
        assert controller.setStructureMaterialSelected('acceptance-film', 'film-set', True)
        assert controller.setSynthesisOption('max_cycles', '1')
        assert controller.setSynthesisOption('max_evaluations', '60')
        scenes = []
        client = SchedulerClient(case.workspace.binding.root)
        try:
            for obj, filename in [(case.source, 'FilmDesignQuick.qml'),
                                  (case.simulation, 'FilmSimulationQuick.qml'),
                                  (controller, 'FilmOptimizationWindow.qml')]:
                scenes.append(_Scene(obj, filename))
            scenes[-1].click(scenes[-1].item('filmOptimizationStartButton'))
            _wait(lambda: bool(controller.queueJobId))
            _wait(lambda: not controller.busy, seconds=30.)
            assert controller._candidate_applied, controller.statusText
            wait_done(case.simulation)
            after = case.source.session.snapshot()
            assert after.design_revision == case.base.design_revision + 1
            assert case.simulation._design == after.design
            assert case.simulation._source_design_revision == after.design_revision
            assert len(case.calls) == 1
        finally:
            for scene in reversed(scenes):
                scene.close()
            if client.endpoint.exists():
                client.stop()
