"""Display ranges/statistics use existing full curves and the same champion."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem

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
