r"""Isolated Qt Quick regression coverage for machine/settings window chrome.

Run once per DPI in a fresh process, for example:
    $env:QT_SCALE_FACTOR = '1.5'
    .\.conda\python.exe -c "import runpy; runpy.run_path('tests/test_machine_settings_compact_chrome_ui.py')['test_machine_settings_compact_chrome_ui']()"

Each run writes screenshots, a manifest, and machine fixture files only below
its unique build/qa_ui_machine_settings_* directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("PYTHONUTF8", "1")

ROOT = Path(__file__).resolve().parents[1]
QML_DIR = ROOT / "coat_sim" / "ui" / "qml"
RUN_ID = uuid4().hex[:10]
EVIDENCE = ROOT / "build" / f"qa_ui_machine_settings_{RUN_ID}"
TEMP_ROOT = EVIDENCE / "temp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
for _key in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_key] = str(TEMP_ROOT)
sys.path.insert(0, str(ROOT / "tests"))

# This guard must precede imports that construct QSettings or a Qt engine.
import qt_test_guard  # noqa: E402,F401

from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QPoint, QPointF,
                            Qt, QUrl)  # noqa: E402
from PySide6.QtGui import QAccessible, QFont, QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from unittest.mock import patch  # noqa: E402

from coat_sim.core.api import CoatService  # noqa: E402
from coat_sim.ui import machine_mask  # noqa: E402
from coat_sim.ui import settings  # noqa: E402
from coat_sim.ui.settings import SettingsBridge  # noqa: E402
from coat_sim.ui.theme_service import ThemeService  # noqa: E402
from qt_test_guard import NoNetworkAccessManagerFactory  # noqa: E402


def _find_item(root, name: str):
    match = root.findChild(QQuickItem, name)
    if match is not None:
        return match
    pending = [root.contentItem()] if hasattr(root, "contentItem") else []
    while pending:
        item = pending.pop()
        if item.objectName() == name:
            return item
        pending.extend(item.childItems())
    return None


def _scene_origin(item: QQuickItem, root: QQuickItem) -> QPointF:
    return item.mapToItem(root, QPointF(0, 0))


def _click(app, window, item: QQuickItem) -> None:
    center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    point = QPoint(round(center.x()), round(center.y()))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point, 20)
    app.processEvents()


def _accessible_name(item: QQuickItem) -> str:
    interface = QAccessible.queryAccessibleInterface(item)
    return str(interface.text(QAccessible.Text.Name)) if interface else ""


def _assert_static_status(window, expected_name: str) -> None:
    status = _find_item(window, "windowChromeStatus")
    assert status is not None and status.isVisible()
    assert not bool(status.property("activeFocusOnTab"))
    assert not bool(status.property("focus"))
    assert not any("MouseArea" in str(child.metaObject().className())
                   for child in status.childItems())
    assert window.findChild(QObject, "windowChromeStatusToolTip") is None
    assert _accessible_name(status) == expected_name


def _screenshot(window, filename: str) -> dict[str, object]:
    image = window.grabWindow()
    assert not image.isNull(), f"could not capture {filename}"
    path = EVIDENCE / filename
    assert image.save(str(path)), f"could not save {path}"
    return {"path": str(path), "width": image.width(), "height": image.height(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _move_mouse_off_chrome_tooltips(window, app) -> None:
    # The offscreen pointer starts at (0, 0), which is over the caption and can
    # leave its full-text tooltip covering the action bar in an otherwise
    # normal-state screenshot. Move into the lower content area, away from
    # caption and status hit targets at every tested window size.
    point = QPoint(round(window.width() * 0.5), round(window.height() * 0.82))
    QTest.mouseMove(window, point, 20)
    QTest.qWait(80)
    app.processEvents()


def _new_machine_fixture(service: CoatService, path: Path) -> dict[str, object]:
    resource_name = "机器实验 <HL>& 独立名称"
    service.save("source", path.parent / "source.csrc", {
        "schema_version": 2,
        "kind": "point",
        "device_type": "electron_beam",
        "source_exponent": 1.0,
    })
    document: dict[str, object] = {
        "schema_version": 5,
        # Resource identity is independent of the storage filename. Keep the
        # markup-like characters literal to exercise plain-text caption data.
        "name": resource_name,
        "carrier_x_mm": 0.0,
        "carrier_y_mm": 0.0,
        "planet_offset_mm": 312.0,
        "planet_tilt_deg": 0.0,
        "substrate_z_mm": 580.0,
        "sun_teeth": 120,
        "planet_teeth": 36,
        "gear_module_mm": 4.0,
        "sources": [{"file": "source.csrc", "x_mm": 0.0, "y_mm": 0.0, "z_mm": 10.0}],
        "masks": [],
        "monitors": [],
    }
    service.save("machine", path, document)
    return document


def _assert_chrome_bounds(window, app) -> None:
    status = _find_item(window, "windowChromeStatus")
    close = _find_item(window, "windowChromeCloseButton")
    assert status is not None and close is not None
    status_pos = _scene_origin(status, window.contentItem())
    close_pos = _scene_origin(close, window.contentItem())
    assert status_pos.x() + status.width() <= close_pos.x() + 1, (
        status_pos.x(), status.width(), close_pos.x())
    assert status.isVisible() and close.isVisible()


def _machine_pass(app, theme_service, dpr: str, result: dict[str, object], factories: list) -> None:
    fixture_dir = EVIDENCE / "machine_fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    service = CoatService(root=fixture_dir)
    path = fixture_dir / "machine.cmac"
    _new_machine_fixture(service, path)

    bridge = machine_mask.MachineBridge(service)
    engine = QQmlApplicationEngine()
    factory = NoNetworkAccessManagerFactory()
    engine.setNetworkAccessManagerFactory(factory)
    factories.append(factory)
    theme_service.attach_engine(engine)
    engine.rootContext().setContextProperty("bridge", bridge)
    warnings: list[str] = []
    engine.warnings.connect(lambda errors: warnings.extend(str(error) for error in errors))
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "MachineWindow.qml")))
    roots = engine.rootObjects()
    assert len(roots) == 1, "MachineWindow.qml failed: " + "\n".join(warnings)
    window = roots[0]
    engine._qa_bridge = bridge
    window.resize(1100, 760)
    bridge.loadDocument(str(path))
    window.showNormal()
    QTest.qWait(120)
    app.processEvents()

    actions = _find_item(window, "machineActions")
    workspace = _find_item(window, "machineWorkspace")
    assert actions is not None and workspace is not None
    assert abs(float(actions.y()) - float(window.property("contentTopMargin"))) <= 1
    assert actions.y() < 46, "old hard-coded top gap remains"
    assert window.property("statusText") == bridge.status
    assert window.property("statusSeverity") == "info"
    expected_caption = "机器结构 — 机器实验 <HL>& 独立名称"
    assert bridge.documentPath == str(path.resolve())
    assert window.property("caption") == expected_caption
    assert path.name not in str(window.property("caption"))
    assert " *" not in str(window.property("caption"))
    _assert_chrome_bounds(window, app)
    _move_mouse_off_chrome_tooltips(window, app)
    result["machine_wide_light"] = _screenshot(window, f"machine-wide-light-{dpr}.png")

    # A real validation failure is exposed as an error while the full normal
    # bridge status and validation message remain available together.
    bridge.setMetadata("notes", "unsaved chrome status draft")
    bridge.setValue("/planet_teeth", "-1")
    app.processEvents()
    assert not bridge.canSave and bridge.validationMessage
    assert window.property("statusSeverity") == "error"
    assert window.property("statusText") == bridge.validationMessage
    expected_detail = bridge.status + "\n" + bridge.validationMessage
    assert window.property("statusDetail") == expected_detail
    assert window.property("caption") == expected_caption + " *"
    assert path.name not in str(window.property("caption"))

    full_tip = f"错误：{expected_detail}"
    _assert_static_status(window, full_tip)

    theme_service.set_theme("dark")
    window.resize(920, 620)
    QTest.qWait(80)
    app.processEvents()
    assert window.width() == 920 and window.height() == 620
    assert workspace.x() >= 0 and workspace.x() + workspace.width() <= window.width() + 1
    result["machine_narrow_dark_error"] = _screenshot(window, f"machine-narrow-dark-error-{dpr}.png")

    # Restore valid input, exercise the existing overwrite request/confirm path,
    # and verify both reject and accept outcomes against the real fixture file.
    bridge.setValue("/planet_teeth", "36")
    bridge.setMetadata("notes", "saved machine overwrite draft")
    app.processEvents()
    assert bridge.canSave and bridge.dirty
    original_bytes = path.read_bytes()
    bridge.saveDocumentAs(str(path))
    dialog = window.findChild(QObject, "machineOverwriteDialog")
    assert dialog is not None and bool(dialog.property("visible"))
    bridge.confirmSaveAs(False)
    dialog.setProperty("visible", False)
    app.processEvents()
    assert bridge.dirty and path.read_bytes() == original_bytes

    bridge.saveDocumentAs(str(path))
    assert bool(dialog.property("visible"))
    bridge.confirmSaveAs(True)
    dialog.setProperty("visible", False)
    app.processEvents()
    assert not bridge.dirty
    assert json.loads(path.read_text(encoding="utf-8"))["notes"] == "saved machine overwrite draft"
    assert window.property("statusText") == bridge.status
    assert not factory.blocked_urls

    # Older machine documents may legitimately have no resource name. Keep
    # their file path on the bridge while the title uses the type placeholder.
    legacy_path = fixture_dir / "machine_legacy_unnamed.cmac"
    legacy_data = json.loads(path.read_text(encoding="utf-8"))
    legacy_data.pop("name", None)
    service.save("machine", legacy_path, legacy_data)
    bridge.loadDocument(str(legacy_path))
    app.processEvents()
    legacy_caption = "机器结构 — 未命名机器"
    assert "name" not in json.loads(legacy_path.read_text(encoding="utf-8"))
    information_controller = bridge.property("informationController")
    assert isinstance(information_controller, QObject)
    resource_information = information_controller.property("resourceInformation")
    assert isinstance(resource_information, dict)
    assert resource_information["name"] == ""
    assert bridge.documentPath == str(legacy_path.resolve())
    assert window.property("caption") == legacy_caption
    assert legacy_path.name not in str(window.property("caption"))

    close = _find_item(window, "windowChromeCloseButton")
    assert close is not None
    _click(app, window, close)
    assert not window.isVisible(), "machine chrome close control did not close the window"
    window.close()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    result["machine_warnings"] = warnings
    result["machine_network_factory"] = {"installed": True, "blocked_urls": factory.blocked_urls}


def _settings_pass(app, theme_service, dpr: str, result: dict[str, object], factories: list) -> None:
    settings_path = TEMP_ROOT / "settings" / "ui_settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path_patch = patch.object(settings, "SETTINGS_PATH", settings_path)
    settings_path_patch.start()
    engine = None
    bridge = None
    window = None
    try:
        assert settings.SETTINGS_PATH == settings_path
        engine = QQmlApplicationEngine()
        factory = NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(factory)
        factories.append(factory)
        bridge = SettingsBridge()
        # Even if a developer's local project config enables a remote provider,
        # keep this isolated UI exercise strictly offline.
        bridge.setAssistantConfig(False, "", "")
        engine.rootContext().setContextProperty("bridge", bridge)
        theme_service.set_theme("light")
        theme_service.attach_engine(engine)
        warnings: list[str] = []
        engine.warnings.connect(lambda errors: warnings.extend(str(error) for error in errors))
        engine.load(QUrl.fromLocalFile(str(QML_DIR / "SettingsWindow.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, "SettingsWindow.qml failed: " + "\n".join(warnings)
        window = roots[0]
        engine._qa_bridge = bridge
        window.resize(960, 760)
        window.showNormal()
        app.processEvents()

        QTest.qWait(100)
        app.processEvents()

        assert window.property("statusText") == bridge.statusMessage
        assert window.property("statusDetail") == bridge.statusMessage
        assert window.property("statusSeverity") == "info"
        scroll = _find_item(window, "settingsScroll")
        assert scroll is not None
        assert abs(float(scroll.y()) - float(window.property("contentTopMargin"))) <= 1
        assert scroll.y() < 40, "old hard-coded settings top gap remains"
        _assert_chrome_bounds(window, app)
        _move_mouse_off_chrome_tooltips(window, app)
        result["settings_wide_light"] = _screenshot(window, f"settings-wide-light-{dpr}.png")

        long_os_error = "synthetic isolated settings write failure " + "detail-" * 60
        with patch.object(settings, "save_ui_settings",
                          side_effect=OSError(long_os_error)):
            assert not bridge.save()
        app.processEvents()
        expected = bridge.statusMessage
        assert expected.startswith("保存失败：") and long_os_error in expected
        assert window.property("statusText") == expected
        assert window.property("statusDetail") == expected
        assert window.property("statusSeverity") == "info", (
            "SettingsBridge exposes no typed severity; UI must not infer one from message text")
        full_tip = "状态：" + expected
        _assert_static_status(window, full_tip)

        theme_service.set_theme("dark")
        window.resize(560, 420)
        QTest.qWait(80)
        app.processEvents()
        assert window.width() == 560 and window.height() == 420
        assert scroll.x() >= 0 and scroll.x() + scroll.width() <= window.width() + 1
        result["settings_narrow_dark_error"] = _screenshot(
            window, f"settings-narrow-dark-error-{dpr}.png")

        # Saving from the QML form must capture a focused font-family draft;
        # onEditingFinished alone may not have run before a keyboard shortcut.
        font_field = _find_item(window, "fontFamilyField")
        assert font_field is not None
        content_loader = _find_item(window, "settingsContentLoader")
        assert content_loader is not None
        form = content_loader.property("item")
        assert form is not None
        previous_font_family = bridge.property("fontFamily")
        assert isinstance(previous_font_family, str)
        chosen_font_family = "Save While Focused Family"
        font_field.setProperty("text", chosen_font_family)
        font_field.forceActiveFocus()
        app.processEvents()
        assert bool(font_field.property("activeFocus"))
        form.saveSettingsDraft()
        assert bridge.fontFamily == chosen_font_family
        assert bridge.save()
        assert settings.load_ui_settings()["font_family"] == chosen_font_family
        reloaded_bridge = SettingsBridge()
        try:
            assert reloaded_bridge.fontFamily == chosen_font_family
        finally:
            reloaded_bridge.closeModelProbe()
            reloaded_bridge.closeAssistantSettingsBootstrap()
            reloaded_bridge.closeMcpConnections()
            reloaded_bridge.deleteLater()
            app.processEvents()
        bridge.setFontFamily(previous_font_family)
        assert bridge.save()

        # Preserve and verify the existing reset, save/restart hints, and all
        # three cleanup actions driven by the page's onClosing handler.
        bridge.restoreDefaults()
        assert bridge.statusMessage == "已恢复默认值；点击“保存”后生效。"
        assert window.property("statusText") == bridge.statusMessage
        current_scale = round(float(os.environ.get("QT_SCALE_FACTOR", "1")) * 100)
        bridge.setDpiScalePercent(75 if current_scale != 75 else 100)
        assert bridge.save()
        assert bridge.restartRequired
        assert "重新打开程序后生效" in bridge.statusMessage
        assert window.property("statusText") == bridge.statusMessage
        assert window.property("statusSeverity") == "info"

        probe_was_closed = bridge._probe_coordinator._probe_closed
        credential_generation = bridge._credential_generation
        close = _find_item(window, "windowChromeCloseButton")
        assert close is not None
        _click(app, window, close)
        assert not window.isVisible(), "settings chrome close control did not close the window"
        assert bridge._probe_coordinator._probe_closed and not probe_was_closed
        assert bridge._credential_generation > credential_generation
        assert bridge._mcp_probe_cancel.is_set()
        assert not factory.blocked_urls
        result["settings_network_factories"] = [
            {"installed": True, "blocked_urls": factory.blocked_urls}]
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
        if bridge is not None:
            bridge.closeModelProbe()
            bridge.closeAssistantSettingsBootstrap()
            bridge.closeMcpConnections()
        if engine is not None:
            engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        settings_path_patch.stop()


def test_machine_settings_compact_chrome_ui() -> dict[str, object]:
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    from coat_sim.ui.fonts import bundled_font_family

    font_family = bundled_font_family()
    if font_family:
        app.setFont(QFont(font_family))
    theme_service = ThemeService.instance()
    theme_service.set_theme("light")
    dpr = os.environ.get("QT_SCALE_FACTOR", "1")
    label = dpr.replace(".", "p") + "x"
    factories: list = []
    result: dict[str, object] = {
        "dpr": dpr,
        "evidence_dir": str(EVIDENCE),
        "temporary_settings_isolation": str(qt_test_guard.QT_TEST_SETTINGS_ROOT),
        "screenshots": {},
    }
    try:
        _machine_pass(app, theme_service, label, result, factories)
        _settings_pass(app, theme_service, label, result, factories)
        assert factories and all(not factory.blocked_urls for factory in factories)
        result["network_factories_total"] = len(factories)
        result["source_hashes"] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "coat_sim/ui/qml/MachineWindow.qml",
                         ROOT / "coat_sim/ui/qml/SettingsWindow.qml", Path(__file__).resolve())
        }
        manifest = EVIDENCE / "result.json"
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Machine/Settings compact chrome passed at DPR {dpr}; evidence={EVIDENCE}")
        return result
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        (EVIDENCE / "failure.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    test_machine_settings_compact_chrome_ui()
