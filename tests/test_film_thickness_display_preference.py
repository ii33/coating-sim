"""A shared thickness presentation preference never quantizes source values."""
from copy import deepcopy
import json
from typing import cast

import pytest
from PySide6.QtCore import Qt
from PySide6.QtQml import QQmlEngine
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.ui.film_display_format import format_film_thickness
from coat_sim.ui.theme_service import ThemeService, configure_theme_from_settings
from tests.test_film_optimization_autoapply import isolated_case
from tests.test_film_synthesis_gui_review import _Scene, _wait
from tests.test_compute_server_category_ui import compute_ui as compute_ui, _find, _settle

pytestmark = pytest.mark.qt


@pytest.fixture(autouse=True)
def restore_display_preferences():
    theme = ThemeService.instance()
    before = theme.film_thickness_decimals
    snapshot = theme.current_snapshot
    mode = theme.current_preference_mode
    theme_id = theme.current_theme_id
    font_family = theme._font_family_override
    theme.set_film_thickness_decimals(3)
    try:
        yield
    finally:
        theme.set_film_thickness_decimals(before)
        theme.configure(mode=mode, theme_package_id=theme_id, font_family=font_family,
                        font_scale=float(snapshot['fontScale']), density_scale=float(snapshot['densityScale']))


@pytest.mark.parametrize('decimals, expected', [(0, '107'), (1, '107'), (2, '106.95'),
    (3, '106.952'), (4, '106.9525'), (5, '106.95247'), (6, '106.952467')])
def test_only_thickness_text_changes(decimals, expected):
    original = 106.952467246
    assert format_film_thickness(original, decimals) == expected
    assert original == 106.952467246
    assert format_film_thickness(1e-12, decimals) != '0'


def test_thickness_rounding_uses_the_same_qt_locale_as_numeric_fields():
    assert format_film_thickness(1.125, 2) == '1.13'


def test_preference_persists_and_updates_existing_engine_without_theme_changes(tmp_path, monkeypatch):
    from coat_sim.ui import settings
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'ui.json')
    initial = settings.save_ui_settings({'film_thickness_decimals': 6, 'assistant_enabled': False})
    configure_theme_from_settings(initial)
    bridge = settings.SettingsBridge()
    theme = ThemeService.instance()
    engine = QQmlEngine()
    handle = theme.attach_engine(engine)
    palette = theme.current_snapshot
    from coat_sim.ui.theme_settings_sync import ThemeSettingsSync
    def apply_settings(values: dict[str, object]) -> None:
        configure_theme_from_settings(values)

    sync = ThemeSettingsSync(settings.SETTINGS_PATH, apply_settings, initial,
                             fallback_ms=100, debounce_ms=10)
    monkeypatch.setattr(bridge, '_schedule_auto_probe', lambda: None)
    try:
        assert bridge.filmThicknessDecimals == 6
        bridge.setFilmThicknessDecimals(2)
        assert bridge.save(), bridge.statusMessage
        assert settings.load_ui_settings()['film_thickness_decimals'] == 2
        assert handle.filmThicknessDecimals == 2
        assert theme.current_snapshot == palette
        # Another process saving the same existing preference file refreshes
        # this engine through the shared read-only settings watcher too.
        settings.save_ui_settings({**settings.load_ui_settings(), 'film_thickness_decimals': 0})
        _wait(lambda: handle.filmThicknessDecimals == 0)
    finally:
        sync.stop()
        sync.deleteLater()
        bridge.deleteLater()
        engine.deleteLater()


def test_main_editor_focus_display_changes_and_export_keep_full_value(tmp_path, monkeypatch):
    from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController
    with isolated_case(tmp_path, monkeypatch) as case:
        source, owner = case.source, case.controller
        design = deepcopy(dict(source.session.snapshot().design))
        exact = 106.952467246
        design['front_layers'][0]['thickness']['value'] = exact
        assert source.session.apply_optimized_design(design, source.session.snapshot().design_revision)
        baseline = source.session.snapshot()
        epoch = source.pendingEditEpoch
        scene = _Scene(source, 'FilmDesignQuick.qml')
        theme = ThemeService.instance()
        try:
            field = scene.item('filmLayerNm_existing-stable-layer')
            _wait(lambda: str(field.property('text')) == '106.952')
            field.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
            QTest.qWait(20)
            assert float(field.property('text')) == exact
            theme.set_film_thickness_decimals(6)
            assert float(field.property('text')) == exact
            scene.item('filmLayerQw_existing-stable-layer').forceActiveFocus(Qt.FocusReason.OtherFocusReason)
            _wait(lambda: str(field.property('text')) == '106.952467')
            theme.set_film_thickness_decimals(2)
            _wait(lambda: str(field.property('text')) == '106.95')
            assert source.pendingEditEpoch == epoch
            assert source.session.snapshot().design_revision == baseline.design_revision
            assert source.currentDesign()['front_layers'][0]['thickness']['value'] == exact
            repair = FilmManufacturingQuickController(owner)
            try:
                assert repair.prepare(), repair.error
                assert cast(list[dict[str, object]], repair.property('layerRows'))[0]['thickness'] == '106.95 nm'
                updates = QSignalSpy(repair.layerRowsChanged)
                theme.set_film_thickness_decimals(3)
                assert updates.count() == 1
                assert cast(list[dict[str, object]], repair.property('layerRows'))[0]['thickness'] == '106.952 nm'
                assert source.currentDesign()['front_layers'][0]['thickness']['value'] == exact
            finally:
                repair.deleteLater()
            path = tmp_path / 'full-precision.cfilm'
            assert source.saveCurrent(), source.uiState
            from coat_sim.core.film_design import save_design
            save_design(path, source.currentDesign())
            assert json.loads(path.read_text('utf8'))['front_layers'][0]['thickness']['value'] == exact
            assert not case.calls  # Display changes do not request new optics.
        finally:
            scene.close()


def test_best_thickness_reformats_but_small_errors_and_packet_stay_exact():
    from coat_sim.ui.film_candidate_display_controller import FilmCandidateDisplayController
    live = FilmCandidateDisplayController()
    live._packet = {'best': dict(layer_count=2, total_thickness_nm=106.952467246,
        max_normalized_error=1e-13, weighted_rms_error=2e-14, physical_hash='a' * 64,
        score=.9, nominal_pass=False), 'scope': 'training'}
    original = deepcopy(live._packet)
    theme = ThemeService.instance()
    try:
        theme.set_film_thickness_decimals(2)
        detail = cast(str, live.property('bestDetailSummary'))
        assert '总厚 106.95 nm' in detail
        assert 'RMS 2e-14' in detail
        assert '归一违规 1e-13' in detail
        assert 'D 106.95 nm' in cast(str, live.property('bestSummary'))
        theme.set_film_thickness_decimals(6)
        assert '总厚 106.952467 nm' in cast(str, live.property('bestDetailSummary'))
        assert live._packet == original
    finally:
        live.shutdown()


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_existing_appearance_settings_exposes_thickness_decimals_at_small_size(compute_ui, tmp_path, mode):
    compute_ui.service.configure(mode=mode, font_scale=1.)
    scene = compute_ui.open()
    try:
        scene.window.resize(560, 640)
        _settle()
        spin = _find(scene.window, 'filmThicknessDisplayDecimals')
        assert spin.isVisible() and spin.width() > 30
        assert (spin.property('from'), spin.property('to')) == (0, 6)
        assert spin.property('value') == 3
        assert not scene.warnings
        image = scene.window.grabWindow()
        if not image.isNull():
            assert image.save(str(tmp_path / ('thickness-settings-' + mode + '.png')))
    finally:
        scene.close()
