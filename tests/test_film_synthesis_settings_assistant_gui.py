"""From-zero defaults, real substrate selection and assistant current-context parity."""
from copy import deepcopy
import json

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.film_auto_synthesis import _proxy, freeze_film_synthesis_request
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload, prepare_film_synthesis_payload
from coat_sim.core.materials import default_material_profile
from coat_sim.ui.project_editor_operations import _optimization_action, _optimization_update
from coat_sim.ui.project_editor_snapshot import _optimization_submission
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_needle import _optical
from tests.test_film_optimization_restore_layout import optimizer, window_runtime, click

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def configure(controller):
    assert controller.setSearchMode('synthesis')
    assert controller.setObjectiveField('wavelength_min_nm', 550.)
    assert controller.setObjectiveField('wavelength_max_nm', 550.)


def test_new_defaults_and_explicit_disabled_choices_survive_save_reopen(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        assert controller.synthesisAssumeUnknownKZero
        assert controller.synthesisOptions['needle_pre_local']
        assert controller._build_synthesis_request().needle_pre_local
        assert controller._build_synthesis_request().environment['assume_lossless']
        assert controller.setSynthesisAssumeUnknownKZero(False)
        assert controller.setSynthesisOption('needle_pre_local', False)
        saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
        assert 'objective_progress_editor' not in saved['synthesis']
        assert 'objective_progress_policy' not in saved['synthesis']['options']
    with optimizer(service, design, saved) as (restored, _source, _session):
        assert not restored.synthesisAssumeUnknownKZero
        assert restored.synthesisOptions['needle_pre_local'] is False
        assert restored.exportOptimizationConfiguration() == saved


def test_real_substrate_and_dataset_enter_frozen_physics_without_editing_source(app, tmp_path):
    service, library, design = fixture(tmp_path)
    glass = library.read('acceptance-glass')['record']
    glass['optical_constants'].append(_optical('alternate-glass', 1.8))
    library.write(glass, update=True)
    other = default_material_profile('Other synthetic substrate', id='other-glass')
    other.update(category='substrate', optical_constants=[_optical('other-set', 2.)])
    library.write(other)
    with optimizer(service, design) as (controller, _source, session):
        configure(controller)
        original = deepcopy(session.snapshot().design)
        assert controller.setSynthesisSubstrateOpticalSet('alternate-glass')
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert request.environment['substrate']['optical_set_id'] == 'alternate-glass'
        actual = evaluate_frozen_structure_candidate(_proxy(request), request.environment)
        assert actual['responses'][0]['actual_values'][0] == pytest.approx(((1.8-1)/(1.8+1))**2)
        assert controller.setSynthesisSubstrateMaterial('other-glass')
        assert controller.synthesisSubstrate['optical_set_id'] == 'other-set'
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert request.environment['substrate']['material_id'] == 'other-glass'
        assert request.environment['substrate']['sides'] == original['substrate']['sides']
        assert request.environment['incident_medium'] == original['incident_medium']
        assert request.environment['exit_medium'] == original['exit_medium']
        assert session.snapshot().design['substrate'] == original['substrate']
        saved = controller.exportOptimizationConfiguration()
        before = deepcopy(saved)
        assert not controller.setSynthesisSubstrateReference('other-glass', 'missing')
        assert controller.exportOptimizationConfiguration() == before
    with optimizer(service, design, saved) as (controller, _source, _session):
        assert controller.synthesisSubstrate['material_id'] == 'other-glass'
        assert controller._build_synthesis_request().environment['substrate']['optical_set_id'] == 'other-set'


def test_current_window_payload_and_guarded_actions_match_gui_without_freezing_on_read(app, tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        _optimization_update(controller, {'search_mode': 'synthesis'})
        configure(controller)
        _optimization_action(controller, session, 'set_synthesis_option', {'name': 'needle_pre_local', 'value': False})
        _optimization_action(controller, session, 'set_synthesis_substrate',
                             {'material_id': 'acceptance-glass', 'optical_set_id': 'glass-set'})
        original_freeze = service.freeze_design
        monkeypatch.setattr(service, 'freeze_design', lambda *_a, **_k: pytest.fail('snapshot may not freeze materials'))
        current = _optimization_submission(controller)
        assert current['available'] and current['executor'] == 'film_synthesis'
        assert current['requires_plan_freeze']
        assert current == _optimization_submission(controller)
        monkeypatch.setattr(service, 'freeze_design', original_freeze)
        frozen, _ = prepare_film_synthesis_payload(service, current['request'])
        _, request, _ = film_synthesis_request_from_payload(frozen)
        assert request.needle_pre_local is False
        assert request.environment['assume_lossless'] is True
        assert request.candidate_materials
        assert request.environment['substrate']['material_id'] == 'acceptance-glass'


def test_retired_ui_data_cannot_execute_or_erase_other_explicit_choices(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        assert controller.setSynthesisAssumeUnknownKZero(False)
        assert controller.setSynthesisOption('needle_pre_local', False)
        saved = controller.exportOptimizationConfiguration()
    saved['synthesis']['objective_progress_editor'] = {'enabled': True, 'fields': {'old': 'opaque'}}
    saved['synthesis']['options']['objective_progress_policy'] = {'retired': 'not executed'}
    before = deepcopy(saved)
    with optimizer(service, design, saved) as (controller, _source, session):
        assert controller.synthesisOptions['needle_pre_local'] is False
        assert controller.synthesisAssumeUnknownKZero is False
        assert '已移除' in controller.configurationRestoreNotice
        assert not hasattr(controller, 'setSynthesisProgressPolicyEnabled')
        assert 'objective_progress_policy' not in controller.exportOptimizationConfiguration()['synthesis']['options']
        assert session.configuration_writes == []
        assert session.workflow_configuration('optimization') == before
    assert saved == before


def test_substrate_only_catalog_refresh_notifies_without_changing_selection(app, tmp_path):
    service, library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        revision = controller.synthesisMaterialCatalogRevision
        selected = controller.synthesisSubstrate['optical_set_id']
        glass = library.read('acceptance-glass')['record']
        glass['optical_constants'].append(_optical('added-substrate-set', 1.7))
        library.write(glass, update=True)
        assert controller.refreshSynthesisMaterials()
        assert controller.synthesisMaterialCatalogRevision > revision
        assert controller.synthesisSubstrate['optical_set_id'] == selected
        assert 'added-substrate-set' in {row['id'] for row in controller.synthesisSubstrate['optical_set_options']}


def test_real_qml_materials_are_at_the_top_and_native_dataset_selection_updates_model(app, tmp_path):
    service, library, design = fixture(tmp_path)
    glass = library.read('acceptance-glass')['record']
    glass['optical_constants'].append(_optical('alternate-glass', 1.8))
    library.write(glass, update=True)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(920)
            window.setHeight(650)
            tab = window.findChild(QQuickItem, 'filmOptimizationAlgorithmTab')
            assert tab is not None
            click(app, window, tab)
            substrate = window.findChild(QQuickItem, 'filmSynthesisSubstrateMaterialField')
            optical = window.findChild(QQuickItem, 'filmSynthesisSubstrateOpticalSetField')
            rows = window.findChild(QObject, 'filmSynthesisMaterialRows')
            assert substrate is not None and optical is not None and rows is not None
            assert substrate.isVisible() and optical.isVisible()
            assert substrate.mapToScene(substrate.boundingRect().topLeft()).y() < 500
            assert window.findChild(QObject, 'filmSynthesisProgressPolicyToggle') is None
            click(app, window, optical)
            QTest.keyClick(window, Qt.Key.Key_End)
            QTest.keyClick(window, Qt.Key.Key_Return)
            QTest.qWait(25)
            app.processEvents()
            assert controller.synthesisSubstrate['optical_set_id'] == 'alternate-glass'
            assert not any('ReferenceError' in row or 'TypeError' in row for row in warnings), warnings


@pytest.mark.parametrize('cancelled', [False, True])
def test_selected_substrate_applies_and_refreshes_same_design_on_finish_or_cancel(app, tmp_path, monkeypatch, cancelled):
    from dataclasses import replace
    from threading import Event
    from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import canonical_parameters, execute_job
    from coat_sim.ui.film_synthesis_quick_adapter import synthesis_base_request, synthesis_result_view
    from tests import test_film_optimization_cancel_spectrum as spectrum
    from tests.test_film_optimization_autoapply import isolated_case
    from tests.test_film_optimization_cancel_apply_review import arm, deliver

    original_fixture = spectrum.fixture

    def fixture_with_dataset(root):
        service, library, design = original_fixture(root)
        glass = library.read('acceptance-glass')['record']
        glass['optical_constants'].append(_optical('alternate-glass', 1.8))
        library.write(glass, update=True)
        return service, library, design

    monkeypatch.setattr(spectrum, 'fixture', fixture_with_dataset)
    with isolated_case(tmp_path, monkeypatch) as case:
        configure(case.controller)
        assert case.controller.setSynthesisSubstrateOpticalSet('alternate-glass')
        configuration = case.controller.exportOptimizationConfiguration()
        configuration['objectives'][0]['mean']['enabled'] = False
        configuration['objectives'][0]['pointwise'].update(enabled=True, mode='range',
            lower_bound_percent=0., upper_bound_percent=100., strict_lower=False, strict_upper=False)
        assert case.controller.loadOptimizationConfiguration(configuration), case.controller.error
        request = freeze_film_synthesis_request(case.service, replace(case.controller._build_synthesis_request(),
            max_cycles=1, max_evaluations=64, enable_local_margin_refinement=False, enable_thickness_reduction=False))
        case.controller._synthesis_editable_snapshot = freeze_synthesis_editable_snapshot(
            case.service, case.base.design, request.frozen_material_snapshot)
        case.request = synthesis_base_request(request)
        event = Event()

        def progress(value, **_kwargs):
            if cancelled and value.get('stage') == 'audit_incumbent':
                event.set()

        parameters = canonical_parameters(None, {'payload': film_synthesis_request_to_payload('selected-substrate', request)})
        payload = execute_job(parameters, event, progress, tmp_path / 'candidate.json',
                              lambda *_args: None, str(tmp_path), search_state_writer=lambda *_args: None)
        view = synthesis_result_view(payload)
        assert view.optimization_result is not None
        assert view.optimization_result.state == ('cancelled' if cancelled else 'completed')
        arm(case)
        if cancelled:
            case.controller.cancelOptimization()
        deliver(case, view)
        applied = case.source.session.snapshot()
        assert case.controller._candidate_applied, case.controller.statusText
        assert applied.design['substrate']['optical_set_id'] == 'alternate-glass'
        assert applied.design['substrate']['sides'] == case.base.design['substrate']['sides']
        spectrum.wait_done(case.simulation)
        assert case.simulation._design == applied.design
        assert case.calls[-1]['design'] == applied.design
        assert case.source.canUndoOptimizedDesign
        assert case.source.undoOptimizedDesign()
        assert case.source.session.snapshot().design['substrate'] == case.base.design['substrate']
