"""Representative independent GUI runtime for theme sync integration tests."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtWidgets import QApplication
    from coat_sim.ui import settings
    settings.SETTINGS_PATH = args.settings
    from coat_sim.ui.theme_service import ThemeService, configure_theme_from_settings
    ThemeService.PACKAGE_ROOT = args.settings.parent / "themes"
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    settings.apply_font_settings(app=app)
    service = configure_theme_from_settings()
    # Repeated startup/attach calls must not create duplicate subscriptions.
    observer = service._settings_sync
    assert observer is not None
    assert configure_theme_from_settings()._settings_sync is observer
    owners = []
    kinds = ["design", "optimization", "simulation", "assets"] if args.role.startswith("project") else [args.role]
    for kind in kinds:
        engine = QQmlEngine()
        bridge = service.attach_engine(engine)
        component = QQmlComponent(engine)
        source = b'import QtQuick\nimport QtQuick.Controls\nimport "."\nAppWindow { width: 300; height: 220; visible: true; property string liveMode: Theme.mode; property string liveFamily: Theme.snapshot.fontFamily; property real livePointSize: Theme.snapshot.fontSizeBody; property color liveSurface: Theme.snapshot.colorWindow; Text { text: "Theme sync"; color: Theme.snapshot.colorText; font.family: Theme.snapshot.fontFamily; font.pointSize: Theme.snapshot.fontSizeBody } }'
        component.setData(source, QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/ThemeSyncHarness.qml")))
        window = component.create()
        assert window is not None, component.errorString()
        owners.append((kind, engine, bridge, component, window))
    last = ""
    def publish():
        nonlocal last
        if args.stop.exists():
            app.quit()
            return
        payload = {"pid": os.getpid(), "role": args.role,
                   "windows": {kind: {"mode": window.property("liveMode"),
                        "family": window.property("liveFamily"), "points": window.property("livePointSize"),
                        "surface": window.property("liveSurface").name(), "revision": bridge.revision, "theme_id": bridge.themeId}
                       for kind, _engine, bridge, _component, window in owners},
                   "font_family": app.font().family(), "font_points": app.font().pointSizeF(),
                   "reads": observer.read_count, "applies": observer.apply_count, "error": observer.last_error}
        encoded = json.dumps(payload, sort_keys=True)
        if encoded != last:
            temporary = args.state.with_suffix(".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            from coat_sim.core.atomic_io import replace_with_retry
            replace_with_retry(temporary, args.state)
            last = encoded
    timer = QTimer()
    timer.timeout.connect(publish)
    timer.start(30)
    publish()
    result = app.exec()
    observer.stop()
    for _kind, _engine, _bridge, _component, window in owners:
        assert isinstance(window, QQuickWindow)
        window.setProperty("allowClose", True)
        window.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
