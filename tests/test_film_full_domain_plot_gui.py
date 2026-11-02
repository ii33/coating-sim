"""Full-domain display never substitutes disjoint target samples for a spectrum."""
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
from threading import Event, get_ident
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import QPoint
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (FilmOptimizationObjective, FilmOptimizationResult,
                                             freeze_film_optimization_request)
from coat_sim.ui.film_candidate_display_controller import target_segments
from coat_sim.ui.film_full_domain_plot_controller import FilmFullDomainPlotController
from coat_sim.ui.film_candidate_display_controller import FilmCandidateDisplayController
from tests.film_structure_fixtures import fixture, request
from tests.test_film_optimization_restore_layout import click, optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def test_targets_are_windowed_and_only_matching_conditions_are_shown():
    bound = FilmObjectiveConstraint(mode='range', target_value=None, upper_bound=.1)
    first = FilmOptimizationObjective(400, 500, 3, angles_deg=(0.,), pointwise=bound)
    second = replace(first, wavelength_min_nm=600, wavelength_max_nm=700)
    different_angle = replace(first, wavelength_min_nm=750, wavelength_max_nm=800, angles_deg=(45.,))
    different_polarization = replace(first, wavelength_min_nm=820, wavelength_max_nm=850, polarization='s')
    different_metric = replace(first, wavelength_min_nm=860, wavelength_max_nm=900, metric='T')
    rows = (first, second, different_angle, different_polarization, different_metric)
    selection = dict(objective_index=0, angle_deg=0., metric='R')
    segments = target_segments(rows, selection)
    assert [(row['wavelength_min_nm'], row['wavelength_max_nm']) for row in segments] == [(400, 500), (600, 700)]
    assert all(row['upper_bound'] == .1 for row in segments)


def test_auto_axis_includes_only_compatible_full_domain_target_segments(app):
    bound_low = FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=.8)
    matching = FilmOptimizationObjective(400, 500, 3, metric='T', pointwise=bound_low)
    different = replace(matching, wavelength_min_nm=600, wavelength_max_nm=700, metric='R',
                        pointwise=replace(bound_low, lower_bound=2.))
    segments = target_segments((matching, different), dict(objective_index=0, angle_deg=0., metric='T'))
    assert len(segments) == 1 and segments[0]['lower_bound'] == .8
    live = FilmCandidateDisplayController()
    try:
        live.updatePlotYAxis(dict(full_domain=True, baseline=[.01, .02], candidate=[.03, .04],
                                  target_segments=segments))
        assert .8 <= cast(Any, live.yAxisMaximum) <= 1.
    finally:
        live.shutdown()


def test_rapid_scope_aba_does_not_revive_pre_control_frame(app):
    from tests.test_film_candidate_display_gui import Controls, drain, packet
    live, controls = FilmCandidateDisplayController(), Controls(held=True)
    try:
        config = live.begin(('run', 1))
        assert live.bind(('run', 1), controls, 'display-job', 'a' * 64)
        live.offer(('run', 1), packet(config, sequence=1))
        drain(app)
        assert live.plotData
        live.setFullDomain(True)
        assert controls.entered.wait(2)
        live.setFullDomain(False)
        assert live.plotData == {}  # Old A matches again, but is before both new controls.
        controls.release.set()
        deadline = time.monotonic() + 4
        while (live._control_inflight or live._awaiting_control) and time.monotonic() < deadline:
            drain(app)
        assert live._minimum_control_sequence == 2
        assert live.plotData == {}
        live.offer(('run', 1), packet(config, sequence=2, control_sequence=2))
        drain(app)
        assert live.plotData
    finally:
        controls.release.set()
        live.shutdown()


def test_formal_preview_is_latest_only_cancelled_and_off_qt_thread(app, monkeypatch):
    import coat_sim.core.film_full_domain_preview as preview
    entered, release = Event(), Event()
    calls = []
    main = get_ident()

    def fake(_frozen, design, _objectives, selection, *, cancel_event):
        calls.append((design['id'], selection['metric'], get_ident()))
        if design['id'] == 'old':
            entered.set()
            assert release.wait(3)
            if cancel_event.is_set():
                raise InterruptedError()
        return dict(wavelength_nm=[400., 700.], actual=[design['value']] * 2,
                    indices=[0, 1], original_point_count=2)

    monkeypatch.setattr(preview, 'full_domain_spectrum', fake)
    plot = FilmFullDomainPlotController()
    selection = dict(objective_index=0, angle_deg=0., metric='R', spectrum_scope='full_domain')
    try:
        plot.request(('old',), {}, dict(id='old', value=.1), dict(id='old-best', value=.2), (), selection)
        assert entered.wait(2)
        for i in range(20):
            plot.request((i,), {}, dict(id='new', value=.3), dict(id='new-best', value=.4), (), selection)
        assert plot.data == {}
        release.set()
        deadline = time.monotonic() + 4
        while not plot.data and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(5)
        assert plot.data['baseline'] == [.3, .3]
        assert plot.data['candidate'] == [.4, .4]
        assert all(thread != main for _, _, thread in calls)
        assert [entry[0] for entry in calls] == ['old', 'new', 'new-best']
        plot.clear()
        assert plot.data == {} and plot.status == ''
    finally:
        release.set()
        plot.shutdown()


def test_enqueued_preview_is_detached_from_later_edits(app, monkeypatch):
    import coat_sim.core.film_full_domain_preview as preview
    entered, release = Event(), Event()
    seen = []

    def fake(frozen, design, _objectives, _selection, *, cancel_event):
        if design['id'] == 'block':
            entered.set()
            assert release.wait(3)
        if cancel_event.is_set():
            raise InterruptedError()
        seen.append((frozen['value'], design['value']))
        return dict(wavelength_nm=[500.], actual=[design['value']], indices=[0], original_point_count=1)

    monkeypatch.setattr(preview, 'full_domain_spectrum', fake)
    plot = FilmFullDomainPlotController()
    selection = dict(objective_index=0, angle_deg=0., metric='R', spectrum_scope='full_domain')
    frozen, original, candidate = {'value': 1}, {'id': 'original', 'value': .1}, {'id': 'best', 'value': .2}
    try:
        plot.request(('first',), {}, {'id': 'block'}, {'id': 'block'}, (), selection)
        assert entered.wait(2)
        plot.request(('next',), frozen, original, candidate, (), selection)
        frozen['value'], original['value'], candidate['value'] = 99, .9, .8
        release.set()
        deadline = time.monotonic() + 4
        while not plot.data and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(5)
        assert seen == [(1, .1), (1, .2)]
        assert plot.data['baseline'] == [.1] and plot.data['candidate'] == [.2]
    finally:
        release.set()
        plot.shutdown()


def test_qml_full_domain_toggle_does_not_show_single_window_as_full(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller) as (window, warnings):
            toggle = window.findChild(QQuickItem, 'filmOptimizationFullDomainToggle')
            assert toggle is not None and toggle.isVisible()
            click(app, window, toggle)
            assert controller.liveDisplay.fullDomain
            assert controller.liveDisplay._selection['spectrum_scope'] == 'full_domain'
            assert controller.plotData == {}
            click(app, window, toggle)
            assert not controller.liveDisplay.fullDomain
            assert 'spectrum_scope' not in controller.liveDisplay._selection
            assert not any('ReferenceError' in warning or 'TypeError' in warning for warning in warnings)


def test_delayed_live_candidate_is_labeled_as_previous_not_latest(app, tmp_path):
    from PySide6.QtCore import QObject
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        live = controller.liveDisplay
        live.setFullDomain(True)
        live._running = True
        live._configuration = {'generation': 'test'}
        live._packet = {'enabled': True, 'selection': dict(live._selection),
                        'current': {'availability': 'available', 'preview_delayed': True,
                                    'spectrum': {'wavelength_nm': [500., 600.], 'actual': [.1, .2]}},
                        'best': None}
        with window_runtime(app, controller) as (window, warnings):
            live.changed.emit()
            app.processEvents()
            from PySide6.QtQml import QQmlEngine, QQmlExpression
            assert window.findChild(QObject, 'filmCandidateDelayedPreviewNotice') is None
            assert window.findChild(QObject, 'filmCandidateLivePreviewStatus') is None
            assert window.findChild(QObject, 'filmOptimizationPlotBaselineTitle') is None
            toggle = window.findChild(QObject, 'filmCandidateLivePreviewToggle')
            assert toggle is not None
            tooltip = QQmlExpression(QQmlEngine.contextForObject(toggle), toggle, 'ToolTip.text')
            assert '有延迟' in tooltip.evaluate()[0] and not tooltip.hasError()
            assert '有延迟' in live.status
            assert live.plotData['current']['preview_delayed'] is True
            canvas = window.findChild(QObject, 'filmOptimizationComparisonCanvas')
            assert canvas is not None and canvas.property('dataSnapshot') == live.plotData
            assert canvas.property('seriesLegend') == '延迟候选 / 优化器最佳'
            assert canvas.property('dataSnapshot')['current']['actual'] == [.1, .2]
            assert not any('ReferenceError' in warning or 'TypeError' in warning for warning in warnings)


def test_real_live_full_domain_worker_reaches_qml_without_joining_target_windows(app, tmp_path):
    import numpy as np
    from coat_sim.core.film_candidate_display import CandidateDisplayObserver
    from coat_sim.core.film_objective_criteria import reduce_objective_responses
    from tests.test_film_candidate_display_gui import Controls, drain

    service, _library, design = fixture(tmp_path)
    objectives = (FilmOptimizationObjective(480., 500., 5), FilmOptimizationObjective(600., 650., 5))
    frozen = freeze_film_optimization_request(service, request(design, objectives,
        bounds=(50., 150.), max_iterations=1, max_evaluations=4))
    responses = [dict(objective_index=i, angle_deg=0., wavelength_nm=np.linspace(o.wavelength_min_nm,
        o.wavelength_max_nm, o.wavelength_samples), actual_values=np.full(o.wavelength_samples, .1))
        for i, o in enumerate(objectives)]
    reduced = reduce_objective_responses(objectives, responses)
    clock = [0.]
    packets: list[dict] = []
    control = dict(enabled=True, objective_index=0, angle_deg=0., metric='R', spectrum_scope='full_domain')
    observer = CandidateDisplayObserver(context_id='qml-real-full-domain', objectives=objectives,
        control_provider=lambda: control, publish_callback=packets.append,
        clock=lambda: clock[0], frozen_snapshot=frozen.frozen_material_snapshot)
    with optimizer(service, design) as (controller, _source, _session):
        live = controller.liveDisplay
        live.setFullDomain(True)
        config = live.begin(('optimization', 1), objectives=objectives)
        live.bind(('optimization', 1), Controls(), 'display-job', 'a' * 64)
        try:
            observer.record(design, weighted_rms_error=reduced.weighted_rms_error,
                max_normalized_error=reduced.max_error, condition_rows=reduced.conditions,
                raw_coverage=reduced.coverage, physical_thickness_nm=[100.], is_best=True)
            deadline = time.monotonic() + 8
            while (not observer._preview or not observer._preview.results) and time.monotonic() < deadline:
                QTest.qWait(10)
            clock[0] = 2.
            observer.refresh()
            assert packets[-1]['best']['availability'] == 'available', observer.last_error
            outer = {'schema_version': 1, 'kind': 'coat_sim.film_live_display', 'job_id': 'display-job',
                     'input_digest': 'a' * 64, 'generation': config['generation'],
                     'control_sequence': 0, 'display': packets[-1]}
            live.offer(('optimization', 1), outer)
            drain(app)
            with window_runtime(app, controller) as (window, warnings):
                canvas = window.findChild(QQuickItem, 'filmOptimizationComparisonCanvas')
                assert canvas is not None
                displayed = canvas.property('dataSnapshot')
                assert displayed['live'] and displayed['full_domain']
                waves = displayed['best']['wavelength_nm']
                assert waves[0] == 480. and waves[-1] == 650.
                assert any(500. < wave < 600. for wave in waves)
                assert not any('ReferenceError' in warning or 'TypeError' in warning for warning in warnings)
        finally:
            observer.finish()


@pytest.mark.parametrize('state', ['completed', 'cancelled'])
def test_formal_preview_uses_frozen_materials_and_computes_gap_for_complete_candidate(app, tmp_path, state):
    service, _library, design = fixture(tmp_path)
    objectives = (FilmOptimizationObjective(480., 500., 5), FilmOptimizationObjective(600., 650., 5))
    frozen = freeze_film_optimization_request(service, request(design, objectives,
        bounds=(50., 150.), max_iterations=1, max_evaluations=4))
    candidate = deepcopy(design)
    candidate['front_layers'][0]['thickness']['value'] = 130.
    result = FilmOptimizationResult(state, 19, candidate, {}, 1., 1., 1., False, (), 1, 1)
    with optimizer(service, design) as (controller, _source, _session):
        controller._frozen_request = frozen
        controller._result = result
        # Any later live-catalog edits must not be read by the preview.
        old_get, old_records = service.materials.get, service.materials._records
        def fail(*_args, **_kwargs):
            raise AssertionError('live catalog accessed')
        service.materials.get = service.materials._records = fail
        try:
            controller.liveDisplay.setFullDomain(True)
            assert controller.plotData == {}
            deadline = time.monotonic() + 12
            while not controller.plotData and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(10)
            data = controller.plotData
            assert data, controller.fullDomainStatus
            assert data['wavelength_nm'][0] == 480. and data['wavelength_nm'][-1] == 650.
            assert any(500. < wave < 600. for wave in data['wavelength_nm'])
            assert len(data['baseline']) == len(data['candidate']) == len(data['wavelength_nm'])
            assert data['baseline'] != data['candidate']
            assert len(data['target_segments']) == 2
            assert controller._result is result and controller._frozen_request is frozen
            controller._result = None
            controller.plotDataChanged.emit()
            assert controller.plotData == {}
        finally:
            service.materials.get, service.materials._records = old_get, old_records


def test_controller_selection_switches_and_close_reject_obsolete_formal_result(app, tmp_path, monkeypatch):
    import coat_sim.core.film_full_domain_preview as preview
    service, _library, design = fixture(tmp_path)
    first_entered, release = Event(), Event()
    calls = []

    def fake(_frozen, _design, _objectives, selection, *, cancel_event):
        calls.append(dict(selection))
        if selection['metric'] == 'R':
            first_entered.set()
            assert release.wait(3)
        if cancel_event.is_set():
            raise InterruptedError()
        return dict(wavelength_nm=[480., 650.], actual=[.2, .3], indices=[0, 1], original_point_count=2)

    monkeypatch.setattr(preview, 'full_domain_spectrum', fake)
    objective = FilmOptimizationObjective(480., 650., 5, angles_deg=(0., 30.))
    with optimizer(service, design) as (controller, _source, _session):
        controller._frozen_request = SimpleNamespace(frozen_material_snapshot={}, design_snapshot=design,
            objectives=(objective,))
        controller._result = SimpleNamespace(best_design=design)
        controller.liveDisplay.setFullDomain(True)
        assert first_entered.wait(2)
        controller.liveDisplay.setMetric('T')
        controller._plot_angle = 30.
        controller.liveDisplay.set_selection(0, 30., 'T')
        assert controller.plotData == {}
        release.set()
        deadline = time.monotonic() + 4
        while not controller.plotData and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(5)
        assert controller.plotData['metric'] == 'T'
        assert calls[0]['metric'] == 'R'
        assert calls[-1]['metric'] == 'T' and calls[-1]['angle_deg'] == 30.
        controller.liveDisplay.setFullDomain(False)
        assert controller._full_domain_plot.data == {}
        controller.liveDisplay.setFullDomain(True)
        controller.shutdown(1000)
        for _ in range(5):
            app.processEvents()
            QTest.qWait(2)
        assert controller._full_domain_plot.data == {}


def test_other_workflow_results_do_not_reuse_old_optimization_spectrum(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller.liveDisplay.setFullDomain(True)
        controller._full_domain_plot.data = {'candidate': [.99], 'wavelength_nm': [550.]}
        controller.liveDisplay._attempt = ('sensitivity', 1)
        controller.plotDataChanged.emit()
        assert controller.plotData == {}
        assert '暂不可用' in controller.fullDomainStatus
        controller.liveDisplay._attempt = None
        controller._manufacturing_controller = SimpleNamespace(_applied=True)
        try:
            controller.plotDataChanged.emit()
            assert controller.plotData == {}
            assert '修复' in controller.fullDomainStatus
        finally:
            controller._manufacturing_controller = None


@pytest.mark.parametrize('synthesis', [False, True])
def test_condition_table_deduplicates_exact_conditions_only_and_retains_worst(app, tmp_path, synthesis):
    from tests.test_film_candidate_display import objective, rows
    service, _library, design = fixture(tmp_path)
    base = rows(objective())[0]
    duplicates = tuple(replace(base, normalized_violation=1. + i / 100., actual=.1 + i / 100.)
                       for i in range(30))
    other_wavelength = replace(base, wavelength_nm=base.wavelength_nm + .00001,
                               normalized_violation=.9, actual=.7)
    other_angle = replace(base, angle_deg=30., normalized_violation=.8, actual=.6)
    other_objective = replace(base, objective_index=1, normalized_violation=.7, actual=.5)
    errors = duplicates + (other_wavelength, other_angle, other_objective)
    result = FilmOptimizationResult('completed', 19, design, {}, 1., 1., 1., False, errors, 1, 1,
                                    validation_per_condition_errors=errors)
    with optimizer(service, design) as (controller, _source, _session):
        controller._result = result
        controller._synthesis_payload = {} if synthesis else None
        shown = controller.conditionErrors
        assert len(shown) == 4
        assert shown[0]['violation'] == '129%'
        assert any(row['actual'] == '70%' for row in shown)
        assert any(row['angle'] == '30°' for row in shown)
        assert any(row['objective'] == 2 for row in shown)
        assert controller._result.per_condition_errors == errors
        assert controller._result.validation_per_condition_errors == errors


def test_real_from_zero_synthesis_preview_uses_selected_substrate_and_zero_layer_baseline(app, tmp_path):
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import canonical_parameters, execute_job
    from coat_sim.ui.film_synthesis_quick_adapter import synthesis_base_request, synthesis_result_view
    from tests.test_film_optimization_needle import _optical
    from tests.test_film_synthesis_settings_assistant_gui import configure

    service, library, design = fixture(tmp_path)
    glass = library.read('acceptance-glass')['record']
    glass['optical_constants'].append(_optical('alternate-glass', 1.8))
    library.write(glass, update=True)
    with optimizer(service, design) as (controller, _source, _session):
        configure(controller)
        assert controller.setSynthesisSubstrateOpticalSet('alternate-glass')
        configuration = controller.exportOptimizationConfiguration()
        configuration['objectives'][0]['pointwise'].update(enabled=True, mode='range',
            lower_bound_percent=0., upper_bound_percent=100., strict_lower=False, strict_upper=False)
        assert controller.loadOptimizationConfiguration(configuration)
        frozen = freeze_film_synthesis_request(service, replace(controller._build_synthesis_request(),
            max_cycles=1, max_evaluations=64, enable_local_margin_refinement=False,
            enable_thickness_reduction=False))
        assert frozen.environment['front_layers'] == []
        assert frozen.environment['substrate']['optical_set_id'] == 'alternate-glass'
        parameters = canonical_parameters(None, {'payload': film_synthesis_request_to_payload('full-domain-synthesis', frozen)})
        payload = execute_job(parameters, Event(), lambda *_a, **_k: None, tmp_path / 'candidate.json',
                              lambda *_a: None, str(tmp_path), search_state_writer=lambda *_a: None)
        view = synthesis_result_view(payload)
        assert view.optimization_result is not None
        controller._frozen_request = synthesis_base_request(frozen)
        controller._result = view.optimization_result
        controller._synthesis_payload = deepcopy(dict(payload))
        controller.liveDisplay.setFullDomain(True)
        deadline = time.monotonic() + 12
        while not controller.plotData and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        data = controller.plotData
        assert data, controller.fullDomainStatus
        assert data['baseline_label'] == '所选表面 N=0 基线'
        assert data['baseline'][0] == pytest.approx(((1.8 - 1.) / (1.8 + 1.)) ** 2)


def test_real_full_domain_qml_visual_at_normal_and_narrow_width(app, tmp_path):
    service, library, design = fixture(tmp_path)
    for identifier in ('acceptance-glass', 'acceptance-film'):
        record = library.read(identifier)['record']
        optical = record['optical_constants'][0]
        optical.update(wavelength_min=400., wavelength_max=1080., wavelengths=[400., 1080.])
        library.write(record, update=True)
    objectives = (FilmOptimizationObjective(400., 500., 9), FilmOptimizationObjective(900., 1080., 9))
    frozen = freeze_film_optimization_request(service, request(design, objectives,
        bounds=(50., 150.), max_iterations=1, max_evaluations=4))
    candidate = deepcopy(design)
    candidate['front_layers'][0]['thickness']['value'] = 130.
    result = FilmOptimizationResult('completed', 19, candidate, {}, 1., 1., 1., False, (), 1, 1)
    qa_dir = Path(os.environ.get('FULL_DOMAIN_QA_DIR', str(tmp_path / 'qa')))
    qa_dir.mkdir(parents=True, exist_ok=True)
    (qa_dir / 'README.txt').write_text('Synthetic fixture, not user configuration. '
        'Real frozen-material full-domain simulation: 400–1080 nm, target windows 400–500 and 900–1080 nm.\n', encoding='utf-8')
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setObjectiveField('wavelength_max_nm', 500.)
        controller.addObjective()
        assert controller.setObjectiveField('wavelength_max_nm', 1080.)
        assert controller.setObjectiveField('wavelength_min_nm', 900.)
        controller.selectObjective(0)
        controller._frozen_request = frozen
        controller._result = result
        controller.liveDisplay.setFullDomain(True)
        with window_runtime(app, controller) as (window, warnings):
            deadline = time.monotonic() + 12
            while not controller.plotData and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(10)
            assert controller.plotData, controller.fullDomainStatus
            assert controller.plotData['wavelength_nm'][0] == 400.
            assert controller.plotData['wavelength_nm'][-1] == 1080.
            for name, size in [('normal', (1100, 760)), ('narrow', (720, 500))]:
                window.resize(*size)
                QTest.mouseMove(window, QPoint(10, 110))
                QTest.qWait(80)
                app.processEvents()
                toggle = window.findChild(QQuickItem, 'filmOptimizationFullDomainToggle')
                auto = window.findChild(QQuickItem, 'filmOptimizationYAxisAuto')
                assert toggle is not None and auto is not None and toggle.isVisible()
                panel = window.findChild(QQuickItem, 'filmOptimizationPlotPanel')
                assert panel is not None
                panel_origin = panel.mapToScene(panel.boundingRect().topLeft())
                assert panel_origin.x() >= 0 and panel_origin.x() + panel.width() <= window.width() + 1
                for object_name in ('filmOptimizationPlotObjectiveBox', 'filmOptimizationPlotAngleBox',
                                    'filmCandidateLivePreviewToggle', 'filmOptimizationFullDomainToggle',
                                    'filmCandidateLiveMetricBox', 'filmOptimizationYAxisAuto',
                                    'filmOptimizationComparisonCanvas'):
                    item = window.findChild(QQuickItem, object_name)
                    assert item is not None and item.isVisible(), object_name
                    origin = item.mapToItem(panel, 0., 0.)
                    assert origin.x() >= -1, object_name
                    assert origin.x() + item.width() <= panel.width() + 1, object_name
                    assert origin.y() >= -1 and origin.y() + item.height() <= panel.height() + 1, object_name
                image = window.grabWindow()
                assert not image.isNull() and image.save(str(qa_dir / f'full-domain-{name}.png'))
            assert not any('ReferenceError' in warning or 'TypeError' in warning for warning in warnings)
