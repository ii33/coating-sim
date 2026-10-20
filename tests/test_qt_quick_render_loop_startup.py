from __future__ import annotations

import pytest
from coat_sim.ui import settings


@pytest.mark.parametrize(('platform', 'version', 'expected'), [
    ('win32', '6.12.0', '1'),
    ('win32', '6.12.1', '1'),
    ('win32', '6.11.3', None),
    ('win32', '6.13.0', None),
    ('linux', '6.12.0', None),
    ('darwin', '6.12.0', None),
])
def test_frame_notification_default_is_limited(monkeypatch, platform, version, expected):
    monkeypatch.delenv('QT_D3D_NO_VBLANK_THREAD', raising=False)
    monkeypatch.setattr(settings.sys, 'platform', platform)
    monkeypatch.setattr(settings, 'qVersion', lambda: version)
    settings._configure_qt_quick_frame_updates()
    assert settings.os.environ.get('QT_D3D_NO_VBLANK_THREAD') == expected


@pytest.mark.parametrize('explicit', ['0', '1', '', 'custom'])
def test_explicit_notification_setting_always_wins(monkeypatch, explicit):
    monkeypatch.setenv('QT_D3D_NO_VBLANK_THREAD', explicit)
    monkeypatch.setattr(settings.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'qVersion', lambda: '6.12.0')
    settings._configure_qt_quick_frame_updates()
    assert settings.os.environ['QT_D3D_NO_VBLANK_THREAD'] == explicit


@pytest.mark.parametrize('graphics', [
    {},
    {'QT_QUICK_BACKEND': 'software'},
    {'QT_D3D_ADAPTER_INDEX': '1'},
    {'QT_D3D_ADAPTER_INDEX': '0'},
    {'QT_D3D_ADAPTER_INDEX': ''},
    {'QT_QUICK_BACKEND': '', 'QSG_RHI_BACKEND': ''},
    {'QSG_RHI_BACKEND': 'd3d11', 'QSG_RENDER_LOOP': 'threaded', 'QSG_NO_VSYNC': '0'},
    {'QSG_RHI_BACKEND': 'opengl', 'QSG_RENDER_LOOP': 'basic', 'QSG_NO_VSYNC': '1'},
])
def test_frame_notification_does_not_change_graphics_choices(monkeypatch, graphics):
    names = ('QT_QUICK_BACKEND', 'QSG_RHI_BACKEND', 'QSG_RENDER_LOOP', 'QSG_NO_VSYNC', 'QT_D3D_ADAPTER_INDEX')
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in graphics.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('QT_D3D_NO_VBLANK_THREAD', raising=False)
    monkeypatch.setattr(settings.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'qVersion', lambda: '6.12.0')
    settings.configure_qt_dpi_scale({})
    assert settings.os.environ['QT_D3D_NO_VBLANK_THREAD'] == '1'
    assert {name: settings.os.environ[name] for name in names if name in settings.os.environ} == graphics


def test_shared_startup_has_no_forced_software_backend(monkeypatch):
    names = ('QT_QUICK_BACKEND', 'QSG_RHI_BACKEND', 'QSG_RENDER_LOOP', 'QSG_NO_VSYNC', 'QT_D3D_ADAPTER_INDEX')
    for name in (*names, 'QT_D3D_NO_VBLANK_THREAD'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'qVersion', lambda: '6.12.0')
    settings.configure_qt_dpi_scale({})
    assert settings.os.environ['QT_D3D_NO_VBLANK_THREAD'] == '1'
    assert all(name not in settings.os.environ for name in names)
    assert settings.os.environ['QT_QUICK_CONTROLS_STYLE'] == 'Fusion'
