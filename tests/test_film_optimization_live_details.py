"""Display ranges/statistics use existing full curves and the same champion."""
from copy import deepcopy
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core import film_candidate_display as display
from coat_sim.core.film_objective_criteria import reduce_objective_responses
from coat_sim.ui.film_candidate_display_controller import FilmCandidateDisplayController
from tests.test_film_candidate_display import harness, design
from tests.test_film_candidate_display_gui import Controls, packet, drain
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def test_statistics_use_full_original_grid_even_when_plot_is_decimated(monkeypatch):
    from tests.test_film_candidate_display import objective
    observer, packets, _clock, _control = harness(objective(5))
    target = observer.objectives[0]
    actual = np.array([.1, .8, .2, .6, .3])
    reduction = reduce_objective_responses((target,), [dict(objective_index=0, angle_deg=0.,
        wavelength_nm=np.linspace(500., 600., 5), actual_values=actual)])
    monkeypatch.setattr(display, 'MAX_SPECTRUM_POINTS', 2)
    observer.record(design(), weighted_rms_error=reduction.weighted_rms_error,
        max_normalized_error=reduction.max_error, condition_rows=reduction.conditions,
        raw_coverage=reduction.coverage, physical_thickness_nm=[100.], is_best=True)
    best = packets[-1]['best']
    assert best['spectrum']['actual'] == [.1, .3]
    assert best['statistics']['mean'] == pytest.approx(.45)
    assert best['statistics']['maximum'] == .8
    assert best['statistics']['maximum_wavelength_nm'] == 525.
    assert best['statistics']['physical_hash'] == best['physical_hash']
    assert best['condition_count'] == 5 and not target.mean.enabled
    forged = deepcopy(packets[-1])
    forged['best']['statistics']['physical_hash'] = '0' * 64
    assert display.validate_candidate_display_packet(forged) is None


def test_auto_range_hysteresis_manual_percent_bounds_and_missing_frames(app):
    live = FilmCandidateDisplayController()
    low = dict(live=True, current=dict(actual=[.003, .0035]), best=dict(actual=[.002, .0039]), upper_bound=.004)
    try:
        live.updatePlotYAxis(low)
        assert live.yAxisMinimum == 0. and live.yAxisMaximum == pytest.approx(.005)
        live.updatePlotYAxis(dict(live=True, current=dict(actual=[.008])))
        assert live.yAxisMaximum == pytest.approx(.01)
        live.updatePlotYAxis(low)
        assert live.yAxisMaximum == pytest.approx(.01)
        live.updatePlotYAxis(low)
        assert live.yAxisMaximum == pytest.approx(.005)
        live.updatePlotYAxis({})
        assert live.yAxisMaximum == pytest.approx(.005)
        live.setYAxisAuto(False)
        assert live.setYAxisBoundsPercent('0.1', '0.7')
        live.updatePlotYAxis(dict(live=True, current=dict(actual=[.9])))
        assert (live.yAxisMinimum, live.yAxisMaximum) == pytest.approx((.001, .007))
        assert not live.setYAxisBoundsPercent('1', '0')
        assert live.yAxisMaximum == pytest.approx(.007)
        live.setYAxisAuto(True)
        live.updatePlotYAxis(low)
        assert live.yAxisMaximum == pytest.approx(.005)
    finally:
        live.shutdown()


def test_qml_details_bind_best_not_trial_or_display_top_rank(app, tmp_path):
    service, _library, source_design = fixture(tmp_path)
    with optimizer(service, source_design) as (controller, _source, _session):
        live = controller.liveDisplay
        live.setEnabled(True)
        config = live.begin(('details', 1))
        live.bind(('details', 1), Controls(), 'display-job', 'a' * 64)
        live.offer(('details', 1), packet(config))
        drain(app)
        assert live._packet['best']['total_thickness_nm'] == 80.
        assert live._packet['current']['total_thickness_nm'] == 90.
        with window_runtime(app, controller) as (window, warnings):
            details = window.findChild(QObject, 'filmOptimizationBestDetails')
            assert details is not None
            text = details.property('text')
            assert '80 nm' in text and '90 nm' not in text
            assert '20%' in text and '5%' not in text
            assert 'RMS 0.1' in text and '均值仅为统计' in text
            toggle = window.findChild(QQuickItem, 'filmOptimizationYAxisAuto')
            assert toggle is not None
            row = toggle.parentItem()
            assert row is not None
            assert toggle.mapToItem(row, 0., 0.).x() + toggle.width() <= row.width() + 1.
            assert not any('ReferenceError' in w or 'TypeError' in w for w in warnings)
            image = window.grabWindow()
            if not image.isNull():
                assert image.save(str(tmp_path / 'live-details.png'))


@pytest.mark.parametrize('mode', ['fixed_topology', 'synthesis'])
@pytest.mark.parametrize('rebind', [False, True])
@pytest.mark.parametrize('cancel_between', [False, True])
def test_real_qml_repeated_run_retains_this_attempt_best_details(
        app, tmp_path, monkeypatch, mode, rebind, cancel_between):
    from tests.test_film_optimization_autoapply import isolated_case
    from tests.test_film_synthesis_gui_review import _Scene, _wait
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController

    with isolated_case(tmp_path, monkeypatch) as case:
        controller = case.controller
        assert controller.setSearchMode(mode)
        assert controller.setObjectiveField('wavelength_min_nm', '550')
        assert controller.setObjectiveField('wavelength_max_nm', '550')
        assert controller.setObjectiveField('pointwise.upper_bound_percent', '0.1')
        if mode == 'synthesis':
            assert controller.setStructureMaterialSelected('acceptance-film', 'film-set', True)
            assert controller.setSynthesisOption('max_cycles', '1')
            assert controller.setSynthesisOption('max_evaluations', '60')
        controller.liveDisplay.setEnabled(True)
        scene = _Scene(controller, 'FilmOptimizationWindow.qml')
        replacement = None
        try:
            previous_generation = None
            for attempt in range(3 if mode == 'synthesis' else 2):
                if rebind and attempt == 1:
                    replacement = FilmDesignQuickController(service=case.service,
                        edit_session=case.source.session, project_context=case.workspace)
                    assert controller.reuseWindowOpen('', replacement)
                if cancel_between and attempt == 1:
                    entered, release = Event(), Event()
                    admission_verifier = controller._compute_target.admission_verifier

                    def held_admission(*args, verifier=admission_verifier,
                                       entered_event=entered, release_event=release):
                        verify, current = verifier(*args)

                        def verify_when_released():
                            verify()
                            entered_event.set()
                            assert release_event.wait(5.)

                        return verify_when_released, current

                    with monkeypatch.context() as held:
                        held.setattr(controller._compute_target, 'admission_verifier', held_admission)
                        try:
                            scene.click(scene.item('filmOptimizationStartButton'))
                            _wait(entered.is_set)
                            # The new attempt has no candidate. Never present
                            # the previous run's genuine winner as this run's.
                            assert controller._live_display._packet == {}
                            assert '等待' in scene.item('filmOptimizationBestDetails').property('text')
                            scene.click(scene.item('filmOptimizationCancelButton'))
                        finally:
                            release.set()
                        _wait(lambda: not controller.busy, seconds=45.)
                        assert controller._live_display._packet == {}
                previous_job = controller.queueJobId
                scene.click(scene.item('filmSynthesisContinueSearchButton' if attempt == 2
                                       else 'filmOptimizationStartButton'))
                _wait(lambda previous=previous_job: controller.queueJobId != previous)
                _wait(lambda: not controller.busy, seconds=45.)
                QTest.qWait(300)
                details = scene.item('filmOptimizationBestDetails')
                live = controller._live_display
                assert live._configuration['generation'] != previous_generation
                previous_generation = live._configuration['generation']
                assert live._binding is not None and live._binding[1] == controller.queueJobId
                best = live._packet['best']
                assert best['statistics']['physical_hash'] == best['physical_hash']
                assert controller._result is not None
                assert best['total_thickness_nm'] == pytest.approx(sum(
                    row['thickness']['value'] for side in ('front_layers', 'back_layers')
                    for row in controller._result.best_design[side]))
                assert best['max_normalized_error'] == controller._result.max_normalized_error
                assert details.isVisible(), (attempt, controller.statusText)
                assert details.width() > 100., (attempt, details.width())
                assert details.height() > 0.
                assert '总厚' in details.property('text'), (attempt, details.property('text'))
                assert '均值 ' in details.property('text')
        finally:
            scene.close()
            if replacement is not None:
                replacement.markSourceClosed()
