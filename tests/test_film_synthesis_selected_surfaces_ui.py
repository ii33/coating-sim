"""Physical-face selection in the synthesis UI and assistant contract."""
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

import pytest
from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.ui import film_synthesis_quick_adapter as adapter
from coat_sim.ui.film_synthesis_search_state import settings_bundle, settings_reason
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@dataclass
class CapturedRequest:
    environment: dict
    objectives: tuple
    candidate_materials: tuple
    settings: dict


def capture_constructor(environment, objectives, candidate_materials, **settings):
    return CapturedRequest(environment, objectives, candidate_materials, settings)


@pytest.mark.parametrize("surfaces", [("front",), ("back",), ("front", "back"), ("back", "front")])
def test_request_is_one_joint_task_with_only_selected_stacks_cleared(monkeypatch, tmp_path, surfaces):
    _service, _library, design = fixture(tmp_path)
    design['substrate']['sides'] = 'double'
    design['first_surface'] = 'back'
    design['expression'] = {'text': 'H', 'symbols': {}, 'in_sync': True}
    design['back_layers'] = [dict(deepcopy(design['front_layers'][0]), id='fixed-back')]
    before = deepcopy(design)
    monkeypatch.setattr(adapter, 'FilmSynthesisRequest', capture_constructor)
    monkeypatch.setattr(adapter, '_validated', lambda request: request)
    base = FilmOptimizationRequest(design, 41, (), ())
    request = adapter.build_synthesis_request(base, (), {}, True, selected_surfaces=surfaces)
    captured = cast(CapturedRequest, request)
    assert captured.settings['selected_surfaces'] == tuple(side for side in ('front', 'back') if side in surfaces)
    for side in ('front', 'back'):
        assert captured.environment[side + '_layers'] == ([] if side in surfaces else before[side + '_layers'])
    assert captured.environment['expression'] == before['expression']
    assert captured.settings['seed_designs'][0]['front_layers'] == before['front_layers']
    assert captured.settings['seed_designs'][0]['back_layers'] == before['back_layers']
    for key in ('substrate', 'first_surface', 'incident_medium', 'exit_medium'):
        assert captured.environment[key] == before[key]
    assert design == before


def test_qml_checkbox_state_empty_block_and_config_roundtrip(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design['substrate']['sides'] = 'double'
    design['back_layers'] = [dict(deepcopy(design['front_layers'][0]), id='back-kept')]
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode('synthesis')
        before = deepcopy(session.snapshot().design)
        with window_runtime(app, controller) as (window, warnings):
            tabs = window.findChild(QObject, 'filmOptimizationSetupTabs')
            tabs.setProperty('currentIndex', 2)
            app.processEvents()
            front = window.findChild(QObject, 'filmSynthesisFrontSurfaceCheck')
            back = window.findChild(QObject, 'filmSynthesisBackSurfaceCheck')
            error = window.findChild(QObject, 'filmSynthesisSurfaceError')
            assert front.property('checked') and not back.property('checked')
            assert back.property('enabled')
            assert QMetaObject.invokeMethod(back, 'click')
            app.processEvents()
            assert front.property('checked') and back.property('checked')
            saved = controller.exportOptimizationConfiguration()
            assert saved['synthesis']['selected_surfaces'] == ['front', 'back']
            assert QMetaObject.invokeMethod(front, 'click')
            app.processEvents()
            assert not front.property('checked') and back.property('checked')
            assert QMetaObject.invokeMethod(back, 'click')
            app.processEvents()
            assert error.property('visible') and not controller.canStart
            with pytest.raises(ValueError, match='至少一个'):
                controller._build_synthesis_request()
            assert controller.loadOptimizationConfiguration(saved)
            app.processEvents()
            assert controller.synthesisSelectedSurfaces == ['front', 'back']
            old = deepcopy(saved)
            del old['synthesis']['selected_surfaces']
            assert controller.loadOptimizationConfiguration(old)
            assert controller.synthesisSelectedSurfaces == ['front']
            assert session.snapshot().design == before
            assert not any('ReferenceError' in w or 'TypeError' in w for w in warnings), warnings


def test_single_surface_normalizes_saved_back_and_disables_checkbox(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design['back_layers'] = [dict(deepcopy(design['front_layers'][0]), id='dormant-back')]
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode('synthesis')
        saved = controller.exportOptimizationConfiguration()
        saved['synthesis']['selected_surfaces'] = ['back']
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisSelectedSurfaces == []
        assert '已取消背面选择' in controller.configurationRestoreNotice
        assert session.snapshot().design['back_layers'] == design['back_layers']
        with window_runtime(app, controller) as (window, _warnings):
            back = window.findChild(QObject, 'filmSynthesisBackSurfaceCheck')
            assert not back.property('enabled') and not back.property('checked')
            assert not controller.canStart
            assert controller.setSynthesisSurfaceSelected('front', True)


def test_continuation_settings_detect_surface_change_and_legacy_front(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design['substrate']['sides'] = 'double'
    with optimizer(service, design) as (controller, _source, _session):
        before = settings_bundle(controller.exportOptimizationConfiguration())
        ref = {'settings': deepcopy(before)}
        del ref['settings']['selected_surfaces']
        assert settings_reason(ref, before) == ''
        assert controller.setSynthesisSurfaceSelected('back', True)
        assert '合成表面' in settings_reason(ref, settings_bundle(controller.exportOptimizationConfiguration()))


def test_mcp_surface_schema_is_optional_and_shared_by_plan_and_submit():
    from coat_sim.core.mcp_contracts import FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA
    request = FILM_SYNTHESIS_PLAN_INPUT_SCHEMA['properties']['payload']['properties']['request']
    field = request['properties']['selected_surfaces']
    assert 'selected_surfaces' not in request['required']
    assert field['default'] == ['front'] and field['minItems'] == 1 and field['uniqueItems']
    assert field['items']['enum'] == ['front', 'back']
    assert FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA['properties']['payload'] is FILM_SYNTHESIS_PLAN_INPUT_SCHEMA['properties']['payload']


def test_switching_double_to_single_normalizes_selection_without_touching_stacks(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design['substrate']['sides'] = 'double'
    design['back_layers'] = [dict(deepcopy(design['front_layers'][0]), id='dormant-back')]
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode('synthesis')
        assert controller.setSynthesisSurfaceSelected('back', True)
        assert controller.setSynthesisSurfaceSelected('front', False)
        saved = controller.exportOptimizationConfiguration()
        single = deepcopy(design)
        single['substrate']['sides'] = 'single'
        session.replace_document(single, 20, saved)
        assert controller.synthesisSelectedSurfaces == []
        assert not controller.synthesisBackAvailable and not controller.canStart
        assert '已取消背面选择' in controller.configurationRestoreNotice
        assert session.snapshot().design['back_layers'] == design['back_layers']


@pytest.mark.parametrize('surfaces', [(), ('front', 'front'), ('unknown',), ('back',)])
def test_adapter_rejects_invalid_or_unavailable_surface_selection(tmp_path, surfaces):
    _service, _library, design = fixture(tmp_path)
    base = FilmOptimizationRequest(design, 1, (), ())
    with pytest.raises(ValueError):
        adapter.build_synthesis_request(base, (), {}, selected_surfaces=surfaces)
