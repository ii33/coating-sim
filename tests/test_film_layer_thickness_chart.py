"""Real Qt presentation of an audited multi-surface, large-stack feedback frame."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tests.test_film_optimization_running_scroll import item

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.mark.parametrize("layer_count", [65, 100])
def test_layer_chart_large_double_sided_scroll_and_mode_switch(app, tmp_path, layer_count):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode('reduction')
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(1100)
            window.setHeight(850)
            chart = window.findChild(QObject, 'filmOptimizationLayerThicknessChart')
            canvas = window.findChild(QObject, 'filmOptimizationComparisonCanvas')
            assert chart.property('visible') and not canvas.property('visible')
            assert not chart.property('hasFrame')
            rows = [dict(id=f'layer-{i}', side='front' if i < 40 else 'back',
                         index=i if i < 40 else i - 40, material_id='synthetic',
                         original_nm=50. + i, best_nm=45. + i, delta_nm=-5.,
                         best_deleted=False) for i in range(layer_count)]
            metrics = dict(layer_count=layer_count, total_thickness_nm=5330., max_normalized_error=.3,
                           weighted_rms_error=.1, targets_met=True, validation_targets_met=True)
            frame = dict(layers=rows, baseline=metrics, candidate=metrics, state='running',
                         evaluations=3, iteration=2, termination_reason='')
            controller._reduction_feedback_snapshot = deepcopy(frame)
            scale = max(row['original_nm'] for row in rows)
            controller._reduction_feedback_layers_cache = controller._make_reduction_feedback_layers(frame, scale)
            controller._reduction_feedback_bar_scale_nm = scale
            controller.changed.emit()
            controller.reductionFeedbackChanged.emit()
            QTest.qWait(150)
            viewport = window.findChild(QObject, 'filmLayerThicknessViewport')
            assert chart.property('hasFrame') and len(chart.property('rows')) == layer_count
            assert viewport.property('contentWidth') >= layer_count * 58
            assert viewport.property('contentWidth') > viewport.property('width')
            assert item(window, 'filmLayerThicknessBar_1_layer-0').property('visible') is False
            assert item(window, 'filmLayerThicknessBar_2_layer-0').property('height') > 0
            assert window.grabWindow().save(str(tmp_path / 'front-65.png'))
            viewport.setProperty('contentX', viewport.property('contentWidth') - viewport.property('width'))
            QTest.qWait(100)
            assert window.grabWindow().save(str(tmp_path / 'back-65.png'))
            assert viewport.property('contentX') > 0
            assert controller.setSearchMode('fixed_topology')
            QTest.qWait(50)
            assert not chart.property('visible') and canvas.property('visible')
            assert not [line for line in warnings if 'ReferenceError' in line or 'TypeError' in line]


def test_repair_projection_joins_ids_and_labels_new_layers_without_hash_leaks():
    from coat_sim.ui.film_manufacturing_quick_adapter import layer_change_rows

    original = {"layers": [{"id": "old", "side": "back", "material_id": "m", "thickness_nm": 8.}]}
    current = {"layers": [{"id": "new", "side": "back", "material_id": "m",
                           "thickness_nm": 4., "origins": ["secret-internal-id"]}]}
    rows = {row["id"]: row for row in layer_change_rows(original, current, None)}
    assert rows["old"]["index"] == 1 and not rows["old"]["is_new"]
    assert rows["old"]["current_deleted"] and rows["old"]["best_nm"] is None
    assert rows["new"]["index"] == 1 and rows["new"]["is_new"]
    assert rows["new"]["original_nm"] is None and rows["new"]["best_nm"] is None
    assert "secret-internal-id" not in rows["new"]["status"]
