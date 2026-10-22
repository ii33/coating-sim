"""Real QML resource-policy controls preserve auto/manual/off and source state."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QObject, QMetaObject, Qt, Q_ARG
from PySide6.QtGui import QGuiApplication

from coat_sim.ui.film_synthesis_quick_adapter import SYNTHESIS_SEARCH_DEFAULTS, synthesis_options
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.mark.parametrize('value,expected', ((0, 0), ('0', 0), (None, None), ('', None),
    ('  ', None), ('3221225472', 3221225472), (1610612736, 1610612736)))
def test_resource_options_keep_auto_manual_off_distinct(value, expected):
    assert SYNTHESIS_SEARCH_DEFAULTS['max_working_set_bytes'] == 0
    assert synthesis_options({'max_working_set_bytes': value})['max_working_set_bytes'] == expected


@pytest.mark.parametrize('value', (False, True, -1, '-1', 'nan', '1.5', 'auto', 'off'))
def test_resource_options_reject_ambiguous_or_invalid_values(value):
    with pytest.raises((ValueError, TypeError)):
        synthesis_options({'max_working_set_bytes': value})


def test_real_qml_auto_manual_off_controls_and_correction_preserve_source(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    original = deepcopy(design)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode('synthesis')
        for row in controller.structureMaterialOptions:
            assert controller.setStructureMaterialSelected(row['material_id'], row['optical_set_id'], True)
        with window_runtime(app, controller) as (window, warnings):
            mode = window.findChild(QObject, 'filmSynthesisResourceGuardModeBox')
            field = window.findChild(QObject, 'filmSynthesisResourceGuardBytesField')
            assert mode is not None and field is not None
            assert mode.property('currentIndex') == 0
            assert controller._build_synthesis_request().max_working_set_bytes == 0

            assert controller.setSynthesisOption('max_working_set_bytes', '3221225472')
            app.processEvents()
            assert mode.property('currentIndex') == 1
            assert field.property('text') == '3221225472'
            assert QMetaObject.invokeMethod(mode, 'activated', Qt.ConnectionType.DirectConnection, Q_ARG(int, 2))
            app.processEvents()
            assert mode.property('currentIndex') == 2
            assert controller.synthesisOptions['max_working_set_bytes'] is None
            assert controller._build_synthesis_request().max_working_set_bytes is None
            assert QMetaObject.invokeMethod(mode, 'activated', Qt.ConnectionType.DirectConnection, Q_ARG(int, 1))
            app.processEvents()
            assert mode.property('currentIndex') == 1
            assert controller.synthesisOptions['max_working_set_bytes'] == 3221225472

            assert not controller.setSynthesisOption('max_working_set_bytes', '-1')
            assert not controller.canStart
            assert QMetaObject.invokeMethod(mode, 'activated', Qt.ConnectionType.DirectConnection, Q_ARG(int, 0))
            app.processEvents()
            assert mode.property('currentIndex') == 0
            assert controller.synthesisOptions['max_working_set_bytes'] == 0
            assert controller.canStart
            assert not any('ReferenceError' in message or 'TypeError' in message for message in warnings), warnings
        assert session.snapshot().design == original


@pytest.mark.parametrize('value,index', ((0, 0), (3 * 1024**3, 1), (None, 2)))
def test_resource_configuration_roundtrip_keeps_null_off_not_auto_fallback(app, tmp_path, value, index):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode('synthesis')
        assert controller.setSynthesisOption('max_working_set_bytes', value)
        saved = controller.exportOptimizationConfiguration()
        assert saved['synthesis']['options']['max_working_set_bytes'] == value
        assert controller.setSynthesisOption('max_working_set_bytes', 0 if value is None else None)
        assert controller.loadOptimizationConfiguration(saved)
        with window_runtime(app, controller) as (window, warnings):
            mode = window.findChild(QObject, 'filmSynthesisResourceGuardModeBox')
            assert mode is not None and mode.property('currentIndex') == index
            assert controller.synthesisOptions['max_working_set_bytes'] == value
            assert not any('ReferenceError' in message or 'TypeError' in message for message in warnings), warnings


def test_resource_mode_selection_rolls_back_when_other_pending_edit_refuses_change(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode('synthesis')
        assert controller.setSynthesisOption('max_working_set_bytes', 0)
        with window_runtime(app, controller) as (window, warnings):
            mode = window.findChild(QObject, 'filmSynthesisResourceGuardModeBox')
            assert mode is not None and mode.property('currentIndex') == 0
            assert not controller.setSynthesisOption('archive_capacity', '0')
            mode.setProperty('currentIndex', 2)  # Native activation also changes selection before its signal.
            assert QMetaObject.invokeMethod(mode, 'activated', Qt.ConnectionType.DirectConnection, Q_ARG(int, 2))
            app.processEvents()
            assert mode.property('currentIndex') == 0
            assert controller.synthesisOptions['max_working_set_bytes'] == 0
            assert not controller.canStart
            assert controller.setSynthesisOption('archive_capacity', '32')
            assert controller.setSynthesisOption('max_working_set_bytes', None)
            app.processEvents()
            assert mode.property('currentIndex') == 2  # The restored binding remains live.
            assert not any('ReferenceError' in message or 'TypeError' in message for message in warnings), warnings


@pytest.mark.parametrize('value', [0, 1, '1'])
def test_archive_ui_minimum_matches_two_protected_core_champions(value):
    from coat_sim.ui.film_synthesis_quick_adapter import synthesis_options
    with pytest.raises(ValueError, match='archive_capacity.*>= 2'):
        synthesis_options({'archive_capacity': value})
    assert synthesis_options({'archive_capacity': 2})['archive_capacity'] == 2
