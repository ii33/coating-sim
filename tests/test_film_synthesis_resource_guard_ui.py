"""Retired synthesis UI controls cannot revive a legacy route or resource cap."""
from copy import deepcopy
import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from coat_sim.ui.film_synthesis_quick_adapter import SYNTHESIS_SEARCH_DEFAULTS, synthesis_options
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt

@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])

def test_new_options_are_explicitly_unlimited_and_hybrid():
    options = synthesis_options({})
    assert SYNTHESIS_SEARCH_DEFAULTS['max_working_set_bytes'] is None
    assert options['max_working_set_bytes'] is None
    assert options['search_strategy'] == 'hybrid'
    assert options['search_until_target'] is False
    assert synthesis_options({'search_until_target': True})['search_until_target'] is True
    for bad in ('true', 1, None):
        with pytest.raises(ValueError):
            synthesis_options({'search_until_target': bad})
    for removed in ({'search_strategy': 'legacy'}, {'max_working_set_bytes': 0},
                    {'max_working_set_bytes': 3 * 1024**3}):
        with pytest.raises(ValueError):
            synthesis_options(removed)

def test_retired_options_migrate_on_restore_and_do_not_reappear(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    original = deepcopy(design)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode('synthesis')
        saved = controller.exportOptimizationConfiguration()
        saved['synthesis']['options'].update(search_strategy='legacy', max_working_set_bytes=12345,
                                              search_until_target=True)
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisOptions['search_strategy'] == 'hybrid'
        assert controller.synthesisOptions['max_working_set_bytes'] is None
        assert controller.synthesisOptions['search_until_target'] is True
        assert '旧合成路线和数值护栏' in controller.configurationRestoreNotice
        assert not controller.setSynthesisOption('search_strategy', 'legacy')
        # A retired setter cannot create a draft value even on direct invocation.
        assert controller.synthesisOptions['search_strategy'] == 'hybrid'
        with window_runtime(app, controller) as (window, warnings):
            assert window.findChild(QObject, 'filmSynthesisResourceGuardModeBox') is None
            assert window.findChild(QObject, 'filmSynthesisResourceGuardBytesField') is None
            assert not any('ReferenceError' in message or 'TypeError' in message for message in warnings), warnings
        assert session.snapshot().design == original

@pytest.mark.parametrize('value', [0, 1, '1'])
def test_archive_ui_minimum_matches_two_protected_core_champions(value):
    with pytest.raises(ValueError, match='archive_capacity.*>= 2'):
        synthesis_options({'archive_capacity': value})
    assert synthesis_options({'archive_capacity': 2})['archive_capacity'] == 2
