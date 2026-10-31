"""End-to-end Quick UI prediction against packaged, sourced optical constants."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

import pytest
from PySide6.QtCore import QMetaObject, QObject
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
import shiboken6

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.materials import default_material_profile
from coat_sim.core.optical_substrate_presets import builtin_optical_substrates
from coat_sim.core import film_resource_prediction as predictor
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.test_film_optimization_restore_layout import click, saved_configuration, window_runtime
from tests.test_film_resource_estimate_ui import wait_until

pytest_plugins = ["tests.test_film_resource_estimate_ui"]
pytestmark = pytest.mark.qt
ROOT = Path(__file__).resolve().parents[1]


def _real_source_design(tmp_path):
    service = FilmDesignService(tmp_path, restrict_to_root=False)
    library = service.materials.library
    catalog = json.loads((ROOT / 'data/optical_catalog.json').read_text(encoding='utf-8'))
    for source_id, test_id, set_id in (
        ('SiO2_film', 'real_sio2_test', 'ri_sio2_rodriguez_de_marcos'),
        ('TiO2_film', 'real_tio2_test', 'ri_tio2_jolivet_amorphous'),
    ):
        source = next(row for row in catalog['materials'] if row['material_id'] == source_id)
        optical = deepcopy(next(row for row in source['optical_constants'] if row['id'] == set_id))
        record = default_material_profile(source_id, id=test_id)
        record.update(category='film', optical_constants=[optical])
        library.write(record)
    source = next(row for row in builtin_optical_substrates() if row['id'] == 'film_substrate_schott_nbk7')
    glass = default_material_profile('测试副本 SCHOTT N-BK7', id='real_nbk7_test')
    glass.update(category='substrate', optical_constants=deepcopy(source['optical_sets']))
    library.write(glass)
    design = service.new_design()
    design['substrate'].update(material_id='real_nbk7_test', optical_set_id='schott_nbk7_sellmeier')
    config = saved_configuration(service, design)
    objective = config['objectives'][0]
    objective.update(wavelength_min_nm=400., wavelength_max_nm=700., wavelength_samples=301,
                     sampling_basis='step', wavelength_step_nm=1., actual_wavelength_step_nm=1.,
                     angle_min_deg=0., angle_max_deg=0., angle_sample_count=1,
                     polarization='unpolarized', metric='R')
    objective['pointwise'].update(enabled=True, mode='range', lower_bound_percent=0.,
                                   upper_bound_percent=.5, strict_lower=False, strict_upper=False)
    objective['mean']['enabled'] = False
    config['synthesis']['assume_unknown_k_zero'] = True
    config['synthesis']['selected_surfaces'] = ['front']
    config['synthesis']['options']['minimum_nonzero_thickness_nm'] = None
    config['synthesis']['options']['max_layer_thickness_nm'] = None
    design['workflow_settings'] = {'optimization': config}
    return service, library, design


def test_packaged_model_real_sources_quick_gui_and_cancellation(runtime, tmp_path, monkeypatch):
    app, source, _window, _warnings = runtime
    service, library, design = _real_source_design(tmp_path / 'real-optics')
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    owner = FilmOptimizationQuickController(source)
    entered, release = threading.Event(), threading.Event()
    frozen_calls = []
    real_freeze = predictor.freeze_film_prediction_request
    def traced_freeze(service, request, **kwargs):
        frozen_calls.append(request.synthesis_request.objectives[0].wavelength_samples)
        return real_freeze(service, request, **kwargs)
    monkeypatch.setattr(predictor, 'freeze_film_prediction_request', traced_freeze)
    try:
        assert owner.setSearchMode('resource_assessment')
        estimator = source.resourceEstimateController
        estimator.refreshMaterials()
        for material in ('real_sio2_test', 'real_tio2_test'):
            row = estimator.addMaterialRow()
            assert estimator.setRowMaterial(row, material), estimator.materialError
        assert estimator.mode == 'quick_prediction'
        with window_runtime(app, owner) as (window, warnings):
            window.resize(360, 500)
            click(app, window, window.findChild(QQuickItem, 'filmOptimizationAlgorithmTab'))
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, lambda: estimator.busy, timeout=5000)
            wait_until(app, lambda: not estimator.busy, timeout=20000)
            summary = estimator.resultSummary
            assert '中心：' in summary and '联合区间' in summary, (estimator.status, summary, estimator.technicalDetails)
            assert '依赖未知 k 按零假设' in summary and '已知 k 保留，未被改写' in summary
            assert '角点' in summary and '实际膜系' in summary
            details = estimator.technicalDetails
            assert '"estimate_state": "estimated"' in details and '"feature_domain": "registered_real_nk"' in details
            assert '"model_id"' in details and '"joint_interval"' in details
            assert '冻结输入摘要：未知' not in details and '"model_sha256"' in details
            assert '材料源在完成时已复核' in estimator.status, estimator.status
            assert frozen_calls == [301, 301]  # inference and UI source recheck use the same sparse freezer
            result = window.findChild(QObject, 'filmResourceEstimateResult')
            assert result is not None
            expression = QQmlExpression(QQmlEngine.contextForObject(result), result, '''(function() {
                let p = parent; while (p !== null) { if ("contentY" in p) {
                    const pos = mapToItem(p.contentItem, 0, 0); p.contentY = Math.max(0, pos.y - 10); }
                    p = p.parent; } })()''')
            expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            QTest.qWait(100)
            image = window.grabWindow()
            assert not image.isNull() and image.width() == 360 and image.save('/tmp/coating-real-prediction-360.png')
            toggle = window.findChild(QQuickItem, 'filmResourceEstimateDetailsToggle')
            technical = window.findChild(QQuickItem, 'filmResourceEstimateTechnicalDetails')
            assert toggle is not None and technical is not None and not technical.isVisible()
            toggle.setProperty('checked', True)
            app.processEvents()
            assert technical.isVisible() and '"model_id"' in technical.property('text')
            window.resize(560, 500)
            QTest.qWait(150)
            expression = QQmlExpression(QQmlEngine.contextForObject(technical), technical, '''(function() {
                let p = parent; while (p !== null) { if ("contentY" in p) {
                    const pos = mapToItem(p.contentItem, 0, 0); p.contentY = Math.max(0, pos.y - 10); }
                    p = p.parent; } })()''')
            expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            QTest.qWait(100)
            image = window.grabWindow()
            assert not image.isNull() and image.width() == 560 and image.save('/tmp/coating-real-prediction-details-560.png')
            assert not warnings, warnings
            assert estimator.setMode('necessary_diagnostics') and estimator.stale
            assert estimator.setMode('synthesis_verification') and estimator.stale
            assert estimator.setMode('quick_prediction') and estimator.stale

            # Hold only at the real predictor boundary so cancel is deterministic;
            # the real predictor consumes the actual cancellation event after release.
            real_predict = predictor.predict_film_resources
            def hold(service, request, **kwargs):
                entered.set()
                assert release.wait(10)
                return real_predict(service, request, **kwargs)
            monkeypatch.setattr(predictor, 'predict_film_resources', hold)
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, entered.is_set, timeout=5000)
            assert estimator.busy and not estimator.setMode('necessary_diagnostics')
            owner.cancelOptimization()
            release.set()
            wait_until(app, lambda: not estimator.busy, timeout=20000)
            assert '取消' in estimator.status and '未完成' in estimator.resultSummary
            monkeypatch.setattr(predictor, 'predict_film_resources', real_predict)

            # Change n and k independently in temporary copies, never the shipped catalog.
            original = library.read('real_sio2_test')['record']
            for component in ('n', 'k'):
                changed = deepcopy(original)
                changed['optical_constants'][0][component][0] += .01
                library.write(changed, update=True)
                assert QMetaObject.invokeMethod(window, 'startFromButton')
                wait_until(app, lambda: estimator.busy, timeout=5000)
                wait_until(app, lambda: not estimator.busy, timeout=20000)
                assert '未取得资源预估' in estimator.resultSummary, (component, estimator.status, estimator.resultSummary)
                assert '具体原因' in estimator.resultSummary
            library.write(original, update=True)

            # Recheck must independently catch a real source change after inference.
            def mutate_after_prediction(service, request, **kwargs):
                result = real_predict(service, request, **kwargs)
                assert result.estimate_state == 'estimated'
                changed = deepcopy(original)
                changed['optical_constants'][0]['n'][0] += .01
                library.write(changed, update=True)
                return result
            monkeypatch.setattr(predictor, 'predict_film_resources', mutate_after_prediction)
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, lambda: estimator.busy, timeout=5000)
            wait_until(app, lambda: not estimator.busy, timeout=20000)
            assert estimator.stale and '材料源在评估期间变化' in estimator.status
            monkeypatch.setattr(predictor, 'predict_film_resources', real_predict)
            library.write(original, update=True)
            assert not warnings, warnings
    finally:
        release.set()
        if source.resourceEstimateController.busy:
            source.resourceEstimateController.cancel()
            wait_until(app, lambda: not source.resourceEstimateController.busy, timeout=20000)
        owner.shutdown(1000)
        shiboken6.delete(owner)


@pytest.mark.parametrize('substrate_id,optical_id', [
    pytest.param('film_substrate_schott_nbk7', 'schott_nbk7_sellmeier', id='native-nbk7'),
    pytest.param('film_substrate_malitson_silica', 'malitson_silica_sellmeier_nm', id='native-silica-nm'),
])
def test_native_builtin_materials_gui_prediction(runtime, substrate_id, optical_id):
    """Exercise the shipped material picker and model without creating catalog copies."""
    from PySide6.QtCore import Q_ARG

    app, source, _window, _warnings = runtime
    service = source.service
    design = service.new_design()
    design['substrate'].update(material_id=substrate_id, optical_set_id=optical_id)
    config = saved_configuration(service, design)
    objective = config['objectives'][0]
    objective.update(wavelength_min_nm=400., wavelength_max_nm=700., wavelength_samples=301,
                     sampling_basis='step', wavelength_step_nm=1., actual_wavelength_step_nm=1.,
                     angle_min_deg=0., angle_max_deg=0., angle_sample_count=1,
                     polarization='unpolarized', metric='R')
    objective['pointwise'].update(enabled=True, mode='range', lower_bound_percent=0.,
                                   upper_bound_percent=.5, strict_lower=False, strict_upper=False)
    objective['mean']['enabled'] = False
    config['synthesis']['assume_unknown_k_zero'] = True
    config['synthesis']['selected_surfaces'] = ['front']
    config['synthesis']['options']['minimum_nonzero_thickness_nm'] = None
    config['synthesis']['options']['max_layer_thickness_nm'] = None
    design['workflow_settings'] = {'optimization': config}
    assert source.session.request_replace_design(design)
    assert not list(service.materials.library.directory.glob('*.json'))
    silica = service.materials.get('builtin_research_SiO2_film')
    silica_set = next(row for row in silica['optical_sets'] if row['id'] == 'ri_sio2_rodriguez_de_marcos')
    assert any(value > 0 for value in silica_set['k'])  # known k is real, not silently zeroed
    owner = FilmOptimizationQuickController(source)
    try:
        assert owner.setSearchMode('resource_assessment')
        estimator = source.resourceEstimateController
        with window_runtime(app, owner) as (window, warnings):
            tabs = window.findChild(QObject, 'filmOptimizationSetupTabs')
            assert tabs is not None and tabs.setProperty('currentIndex', 2)
            QTest.qWait(60)

            def find_item(name):
                queue = [window.contentItem()]
                while queue:
                    item = queue.pop()
                    if item.objectName() == name:
                        return item
                    queue.extend(item.childItems())
                return None

            def choose(name, identifier):
                wait_until(app, lambda: find_item(name) is not None, timeout=3000)
                combo = find_item(name)
                assert combo is not None, name
                options = combo.property('model')
                index = next(i for i, option in enumerate(options) if option['id'] == identifier)
                assert combo.setProperty('currentIndex', index)
                assert QMetaObject.invokeMethod(combo, 'activated', Q_ARG(int, index))
                app.processEvents()

            choose('resourceSubstrateMaterial', substrate_id)
            choose('resourceSubstrateOpticalSet', optical_id)
            for material, optical in (
                ('builtin_research_SiO2_film', 'ri_sio2_rodriguez_de_marcos'),
                ('builtin_research_TiO2_film', 'ri_tio2_jolivet_amorphous'),
            ):
                identity = estimator.addMaterialRow()
                app.processEvents()
                choose('resourceMaterial_' + identity, material)
                choose('resourceOpticalSet_' + identity, optical)
            assert [(row['material_id'], row['optical_set_id']) for row in estimator.materialRows] == [
                ('builtin_research_SiO2_film', 'ri_sio2_rodriguez_de_marcos'),
                ('builtin_research_TiO2_film', 'ri_tio2_jolivet_amorphous')]
            assert estimator.substrateSelection['material_id'] == substrate_id
            assert estimator.substrateSelection['optical_set_id'] == optical_id
            previous = estimator._token
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, lambda: estimator._token > previous and not estimator.busy, timeout=20000)
            assert '中心：' in estimator.resultSummary and '联合区间' in estimator.resultSummary, (
                estimator.status, estimator.resultSummary, estimator.technicalDetails)
            assert '依赖未知 k 按零假设' in estimator.resultSummary
            assert '已知 k 保留，未被改写' in estimator.resultSummary
            assert '"estimate_state": "estimated"' in estimator.technicalDetails
            assert '"feature_domain": "registered_real_nk"' in estimator.technicalDetails
            model_hash = hashlib.sha256(predictor._MODEL_PATH.read_bytes()).hexdigest()
            assert model_hash in estimator.technicalDetails
            assert '材料源在完成时已复核' in estimator.status
            assert not warnings, warnings
            assert not list(service.materials.library.directory.glob('*.json'))
    finally:
        if source.resourceEstimateController.busy:
            source.resourceEstimateController.cancel()
            wait_until(app, lambda: not source.resourceEstimateController.busy, timeout=20000)
        owner.shutdown(1000)
        shiboken6.delete(owner)
