from __future__ import annotations

import pytest
from coat_sim.ui import settings


@pytest.mark.parametrize(('platform', 'version', 'expected'), [
    ('win32', '6.12.0', 'software'),
    ('win32', '6.12.1', 'software'),
    ('win32', '6.11.3', None),
    ('win32', '6.13.0', None),
    ('linux', '6.12.0', None),
    ('darwin', '6.12.0', None),
])
def test_default_presentation_backend_is_limited(monkeypatch, platform, version, expected):
    monkeypatch.delenv('QT_QUICK_BACKEND', raising=False)
    monkeypatch.delenv('QSG_RHI_BACKEND', raising=False)
    monkeypatch.setattr(settings.sys, 'platform', platform)
    monkeypatch.setattr(settings, 'qVersion', lambda: version)
    settings._configure_qt_quick_render_backend()
    assert settings.os.environ.get('QT_QUICK_BACKEND') == expected
    assert 'QSG_RHI_BACKEND' not in settings.os.environ


@pytest.mark.parametrize(('variable', 'explicit'), [
    ('QT_QUICK_BACKEND', 'rhi'), ('QT_QUICK_BACKEND', 'software'),
    ('QT_QUICK_BACKEND', ''), ('QSG_RHI_BACKEND', 'opengl'),
    ('QSG_RHI_BACKEND', 'd3d11'), ('QSG_RHI_BACKEND', ''),
])
def test_explicit_backend_choice_always_wins(monkeypatch, variable, explicit):
    monkeypatch.delenv('QT_QUICK_BACKEND', raising=False)
    monkeypatch.delenv('QSG_RHI_BACKEND', raising=False)
    monkeypatch.setenv(variable, explicit)
    monkeypatch.setenv('QSG_RENDER_LOOP', 'threaded')
    monkeypatch.setattr(settings.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'qVersion', lambda: '6.12.0')
    before = {key: settings.os.environ.get(key) for key in
              ('QT_QUICK_BACKEND', 'QSG_RHI_BACKEND', 'QSG_RENDER_LOOP')}
    settings._configure_qt_quick_render_backend()
    assert {key: settings.os.environ.get(key) for key in before} == before


def test_shared_startup_preserves_loop_and_both_explicit_backends(monkeypatch):
    monkeypatch.setenv('QT_QUICK_BACKEND', 'rhi')
    monkeypatch.setenv('QSG_RHI_BACKEND', 'd3d12')
    monkeypatch.setenv('QSG_RENDER_LOOP', '')
    monkeypatch.setattr(settings.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'qVersion', lambda: '6.12.0')
    settings.configure_qt_dpi_scale({})
    assert settings.os.environ['QT_QUICK_BACKEND'] == 'rhi'
    assert settings.os.environ['QSG_RHI_BACKEND'] == 'd3d12'
    assert settings.os.environ['QSG_RENDER_LOOP'] == ''


def test_shared_startup_applies_software_without_setting_render_loop(monkeypatch):
    for key in ('QT_QUICK_BACKEND', 'QSG_RHI_BACKEND', 'QSG_RENDER_LOOP'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'qVersion', lambda: '6.12.0')
    settings.configure_qt_dpi_scale({})
    assert settings.os.environ['QT_QUICK_BACKEND'] == 'software'
    assert 'QSG_RHI_BACKEND' not in settings.os.environ
    assert 'QSG_RENDER_LOOP' not in settings.os.environ
    assert settings.os.environ['QT_QUICK_CONTROLS_STYLE'] == 'Fusion'
