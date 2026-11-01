"""Display defaults are per-window preferences, not per-run overrides."""
import pytest
from PySide6.QtGui import QGuiApplication
from coat_sim.ui.film_candidate_display_controller import FilmCandidateDisplayController

pytestmark = pytest.mark.qt

@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])

def test_live_and_auto_axis_default_on_but_respect_manual_off_across_runs(app):
    live = FilmCandidateDisplayController()
    try:
        assert live.enabled is True
        assert live.yAxisAuto is True
        live.setEnabled(False)
        live.setYAxisAuto(False)
        first = live.begin(('default-test', 1))
        second = live.begin(('default-test', 2))
        assert first['enabled'] is False and second['enabled'] is False
        assert live.enabled is False and live.yAxisAuto is False
    finally:
        live.shutdown()


def test_progress_fields_are_stable_and_unknown_not_zero(app, tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import optimizer
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        fields = controller.progressFields
        assert fields['mode_name'] == '迭代'
        assert fields['count'] == '0' and fields['evaluations'] == '0'
        assert fields['best_violation'] == ''
        controller._progress_iteration = 123456
        controller._progress_evaluations = 987654
        controller._progress_best_error = 0.0125
        fields = controller.progressFields
        assert fields['count'] == '123456' and fields['evaluations'] == '987654'
        assert fields['best_violation'] == '1.25%'
        controller._synthesis_payload = {'result': {'cycles': None, 'evaluations': 42},
                                         'work_counters': {'work_counters_complete': False}}
        fields = controller.progressFields
        assert fields['mode_name'] == '轮次' and fields['count'] == ''
        assert fields['evaluations'] == '42' and '未知项未按 0 报告' in fields['note']
        controller._synthesis_payload = None
