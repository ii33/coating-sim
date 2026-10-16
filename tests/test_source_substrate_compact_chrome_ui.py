"""Real-engine checks for the Source and Substrate compact chrome migration.

Each worker runs in a fresh Qt process so QT_SCALE_FACTOR is applied before
QGuiApplication construction. Evidence and disposable document fixtures live
only beneath build/qa_ui_source_substrate_<run-id>/.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
PAGE_SPECS = {
    "source": ("SourceWindow.qml", ".csrc", 640, 540, "源样品 <HL>& 独立名称"),
    "substrate": ("SubstrateWindow.qml", ".csub", 1000, 650, "基片样品 <HL>& 独立名称"),
}


def _worker_main() -> int:
    """Run a scale-isolated Qt test worker, initialized before Qt imports."""
    run_root = Path(os.environ["SOURCE_SUBSTRATE_QA_ROOT"]).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(run_root)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Fusion")
    os.environ.setdefault("XDG_CACHE_HOME", str(run_root / "cache"))
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    import qt_test_guard  # noqa: F401  (must precede application imports)
    return _run_qt_tests(run_root)


def _run_qt_tests(run_root: Path) -> int:
    import hashlib
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QSettings, QUrl
    from PySide6.QtGui import QAccessible, QFont, QFontDatabase
    from PySide6.QtQuick import QQuickItem, QQuickWindow
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest

    from coat_sim.core.api import CoatService
    from coat_sim.ui.fonts import bundled_font_family
    from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge
    from coat_sim.ui.theme_service import ThemeService
    from qt_test_guard import NoNetworkAccessManagerFactory

    settings_root = run_root / "settings"
    settings_root.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings_root))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.SystemScope, str(settings_root))
    app = QApplication.instance() or QApplication([])
    QQuickStyle.setStyle("Fusion")

    registered_fonts = []
    for font_path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/segoeui.ttf")):
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id >= 0:
                registered_fonts.extend(QFontDatabase.applicationFontFamilies(font_id))
    family = bundled_font_family()
    if family:
        app.setFont(QFont(family))
    elif registered_fonts:
        preferred = next((name for name in registered_fonts if "Microsoft YaHei" in name), registered_fonts[0])
        app.setFont(QFont(preferred))

    bridge_types = {"source": SourceBridge, "substrate": SubstrateBridge}
    theme = ThemeService.instance()
    qml_warnings: list[str] = []
    network_factories = []
    bridges = []
    engines = []
    manifests: list[dict] = []

    def pump(seconds: float = 0.12) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)

    def wait_until(predicate, description: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            QTest.qWait(20)
        raise AssertionError(f"timed out waiting for {description}")

    def expr(window, expression: str):
        context = QQmlEngine.contextForObject(window)
        value = QQmlExpression(context, window, expression)
        result, _ = value.evaluate()
        if value.hasError():
            raise AssertionError(f"QML expression {expression!r}: {value.error()}")
        return result

    def item(window: QQuickWindow, object_name: str):
        found = window.findChild(QObject, object_name)
        if found is None:
            # Some QML delegates are visually parented rather than QObject
            # parented. Walk the actual visual tree as the second lookup path.
            def walk(node):
                if node.objectName() == object_name:
                    return node
                if isinstance(node, QQuickItem):
                    for child in node.childItems():
                        match = walk(child)
                        if match is not None:
                            return match
                return None
            found = walk(window.contentItem())
        if found is None:
            raise AssertionError(f"missing rendered QML object {object_name!r}")
        return found

    def make_page(page_key: str, mode: str, width: int | None = None):
        file_name, suffix, min_width, min_height, resource_name = PAGE_SPECS[page_key]
        service_root = run_root / "service"
        service_root.mkdir(parents=True, exist_ok=True)
        service = CoatService(root=service_root, restrict_to_root=False)
        bridge = bridge_types[page_key](service)
        bridges.append(bridge)
        # Save a fixture with an explicit resource name that deliberately does
        # not match its on-disk filename. Literal markup-like text guards the
        # QML plain-text caption contract too.
        fixture = run_root / "fixtures" / (page_key + suffix)
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture_data = bridge.documentData()
        fixture_data["name"] = resource_name
        service.save(page_key, fixture, fixture_data)
        if not bridge.openPath(str(fixture)):
            raise AssertionError(f"could not load prepared {page_key} fixture: {bridge.status}")

        theme.configure(mode=mode, theme_package_id="", font_scale=1.0, density_scale=1.0)
        engine = QQmlApplicationEngine()
        network_factory = NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(network_factory)
        network_factories.append(network_factory)
        theme.attach_engine(engine, "Theme")
        engine.warnings.connect(lambda messages: qml_warnings.extend(str(m.toString()) for m in messages))
        engine.rootContext().setContextProperty("bridge", bridge)
        page_path = ROOT / "coat_sim" / "ui" / "qml" / file_name
        engine.load(QUrl.fromLocalFile(str(page_path)))
        if not engine.rootObjects():
            raise AssertionError(f"{file_name} failed to load:\n" + "\n".join(qml_warnings))
        window: QQuickWindow = engine.rootObjects()[0]
        engines.append(engine)
        window.showNormal()
        window.resize(width or 1100, 760)
        wait_until(lambda: window.isExposed() or window.isVisible(), f"{page_key} window show")
        pump(0.3)
        if page_key == "source":
            loader = item(window, "sourceFormLoader")
            try:
                wait_until(lambda: loader.property("item") is not None, "source form Loader", timeout=15.0)
            except AssertionError as exc:
                raise AssertionError(
                    str(exc) + "; status=" + repr(expr(window, "sourceFormLoader.status"))
                    + "; source=" + repr(expr(window, "sourceFormLoader.source"))
                    + "; warnings=" + repr(qml_warnings)) from exc
        return service, bridge, engine, window, fixture

    class CompactChromeUiTests(unittest.TestCase):
        def test_source_and_substrate_compact_chrome_status_layout_themes_and_lifecycle(self):
            scale_requested = float(os.environ.get("QT_SCALE_FACTOR", "1"))
            primary = app.primaryScreen()
            dpr = primary.devicePixelRatio() if primary is not None else app.devicePixelRatio()
            self.assertAlmostEqual(dpr, scale_requested, delta=0.08,
                                   msg=f"Qt DPR {dpr} does not match requested {scale_requested}")

            for page_key in ("source", "substrate"):
                file_name, suffix, min_width, min_height, resource_name = PAGE_SPECS[page_key]
                with self.subTest(page=page_key):
                    service, bridge, engine, window, fixture = make_page(page_key, "light")
                    checks = ["initial-loaded-fixture", "light-theme"]
                    screenshots = []
                    try:
                        self.assertEqual(window.property("statusText"), bridge.status or
                                         ("当前模型有效，可保存。" if page_key == "source" else "参数有效，可保存。"))
                        self.assertEqual(window.property("statusSeverity"), "info")
                        self.assertEqual(window.property("statusDetail"), "")
                        caption_prefix = "蒸发源" if page_key == "source" else "基片"
                        expected_caption = f"{caption_prefix} — {resource_name}"
                        self.assertEqual(bridge.currentPath, str(fixture.resolve()))
                        self.assertEqual(bridge.displayName, resource_name)
                        self.assertEqual(window.property("caption"), expected_caption)
                        self.assertNotIn(fixture.name, window.property("caption"))
                        self.assertIsNone(window.findChild(QObject, "statusBar"),
                                          "the bottom status placeholder must be removed")

                        if page_key == "source":
                            action = item(window, "sourceActionArea")
                            workspace = item(window, "sourceWorkspaceViewport")
                            self.assertAlmostEqual(action.y(), window.property("contentTopMargin"), delta=0.1)
                            self.assertLessEqual(workspace.y() + workspace.height(), window.height() - 11)
                            self.assertGreater(workspace.height(), 100)
                        else:
                            action = item(window, "substrateActionArea")
                            workspace = item(window, "substrateMainWorkspace")
                            self.assertAlmostEqual(action.y(), window.property("contentTopMargin"), delta=0.1)
                            self.assertLessEqual(workspace.y() + workspace.height(), window.height() - 11)
                            self.assertGreater(workspace.height(), 100)
                        operation_bar = item(window, "sourceOperationBar") if page_key == "source" else action
                        operation_accessible = QAccessible.queryAccessibleInterface(operation_bar)
                        self.assertIsNotNone(operation_accessible)
                        self.assertEqual(operation_accessible.text(QAccessible.Text.Name), "操作栏")
                        self.assertIsNone(window.findChild(QObject, page_key + "AssetBundleLoader"))
                        self.assertIsNone(window.findChild(QObject, page_key + "AssetBundleDialog"))
                        self.assertFalse(any("资产包" in str(child.property("text") or "")
                                             for child in window.findChildren(QQuickItem)))
                        checks.extend(["compact-top-and-reclaimed-bottom-space",
                                       "accessible-operation-bar-without-legacy-bundle-entry"])

                        # A loaded, editable document keeps the independent
                        # resource name in its title while the file path stays
                        # available from the bridge.
                        bridge.setField("description", "loaded fixture edited")
                        self.assertTrue(bridge.dirty)
                        self.assertEqual(window.property("caption"), expected_caption + " *")
                        self.assertNotIn(fixture.name, window.property("caption"))
                        checks.append("loaded-and-dirty-caption")

                        # Exercise the real validation status binding. The long
                        # diagnostic is test data placed in the existing bridge
                        # status slot; no new production error protocol is added.
                        invalid_key = "source_exponent" if page_key == "source" else "radius_mm"
                        invalid_value = "30" if page_key == "source" else "-1"
                        bridge.setField(invalid_key, invalid_value)
                        self.assertFalse(bridge.valid, bridge.error)
                        self.assertEqual(window.property("statusSeverity"), "error")
                        self.assertEqual(window.property("statusText"), bridge.error)
                        self.assertEqual(window.property("statusDetail"), bridge.error)
                        real_error = bridge.error
                        bridge._error = "验证错误：" + ("中英文长诊断 Nominal substrate geometry / Sampled nominal height；" * 24)
                        bridge.changed.emit()
                        pump()
                        status_control = item(window, "windowChromeStatus")
                        tooltip = window.findChild(QObject, "windowChromeStatusToolTip")
                        self.assertIsNotNone(status_control)
                        self.assertIsNone(tooltip, "WindowChrome status must not create a tooltip")
                        self.assertFalse(status_control.property("activeFocusOnTab"))
                        self.assertFalse(status_control.property("focus"))
                        self.assertFalse(any("MouseArea" in str(child.metaObject().className())
                                             for child in status_control.childItems()))
                        accessible = QAccessible.queryAccessibleInterface(status_control)
                        self.assertIsNotNone(accessible)
                        accessible_name = accessible.text(QAccessible.Text.Name)
                        self.assertEqual(accessible_name, "错误：" + bridge.error)
                        self.assertIn("Sampled nominal height", accessible_name)
                        self.assertEqual(window.property("statusText"), bridge.error)
                        error_image = window.grabWindow()
                        self.assertFalse(error_image.isNull(), "no rendered long-error tooltip image")
                        error_path = run_root / f"{page_key}_long_error_dpr{scale_requested:g}.png"
                        self.assertTrue(error_image.save(str(error_path)))
                        screenshots.append(str(error_path.relative_to(ROOT)))
                        pump(0.15)
                        checks.append("real-validation-and-full-accessible-status")
                        bridge._error = real_error
                        bridge.setField(invalid_key, "1" if page_key == "source" else "150")
                        self.assertTrue(bridge.valid, bridge.error)
                        pump()
                        self.assertEqual(window.property("statusSeverity"), "info")

                        # Save via the real QML FileDialog acceptance path to an
                        # existing file distinct from the document's current
                        # path (the bridge intentionally rejects same-path
                        # Save As before it considers overwrite approval).
                        overwrite_path = fixture.parent / (page_key + "_overwrite" + suffix)
                        service.save(page_key, overwrite_path, bridge.documentData())
                        source_bytes = overwrite_path.read_bytes()
                        save_dialog_name = "sourceSaveDialog" if page_key == "source" else "substrateSaveDialog"
                        save_dialog = item(window, save_dialog_name)
                        selected_url = QUrl.fromLocalFile(str(overwrite_path))
                        self.assertTrue(save_dialog.setProperty("selectedFile", selected_url),
                                        f"{save_dialog_name}.selectedFile is not writable")
                        self.assertEqual(Path(save_dialog.property("selectedFile").toLocalFile()).resolve(),
                                         overwrite_path.resolve())
                        overwrite_requests: list[str] = []
                        bridge.overwriteRequested.connect(
                            lambda url, requests=overwrite_requests: requests.append(url.toString()))
                        self.assertTrue(QMetaObject.invokeMethod(save_dialog, "accepted"))
                        pump()
                        overwrite = item(window, page_key + "OverwriteDialog")
                        self.assertTrue(overwrite.property("visible"),
                                        "accepted Save dialog did not raise overwrite; pending="
                                        + repr(bridge.pendingSaveAs) + "; requested="
                                        + repr(overwrite_requests) + "; selected="
                                        + repr(save_dialog.property("selectedFile").toString())
                                        + "; status=" + repr(bridge.status)
                                        + "; warnings=" + repr(qml_warnings))
                        self.assertTrue(overwrite_requests, "overwriteRequested was not emitted")
                        self.assertEqual(overwrite_path.read_bytes(), source_bytes,
                                         "overwrite must remain pending until explicit acceptance")
                        self.assertTrue(QMetaObject.invokeMethod(overwrite, "accept"))
                        pump()
                        self.assertFalse(bridge.dirty)
                        self.assertEqual(service.load(page_key, overwrite_path).data["description"],
                                         "loaded fixture edited")
                        checks.append("save-dialog-and-explicit-overwrite")

                        # Change the actual surface/source selector state via
                        # the page bridge, then ensure the page still renders.
                        bridge.setField("description", "saved and switched")
                        self.assertTrue(bridge.dirty)
                        self.assertIn("saved and switched", bridge.documentData()["description"])
                        checks.append("page-switch-dirty-state-retained")

                        # At the original declared minimum widths the main body
                        # remains inside the page after its top/bottom chrome.
                        window.setProperty("width", min_width)
                        window.setProperty("height", max(min_height, 700))
                        pump(0.35)
                        self.assertGreaterEqual(window.width(), min_width)
                        self.assertLessEqual(workspace.y() + workspace.height(), window.height() - 11)
                        if page_key == "source":
                            self.assertAlmostEqual(action.y(), window.property("contentTopMargin"), delta=0.1)
                        checks.append(f"narrow-at-original-minimum-{min_width}x{max(min_height,700)}")

                        # Theme changes update the existing engine and root, not
                        # a reconstructed page. Capture both palettes at the
                        # narrow edge, then restore the regular review width.
                        for mode in ("light", "dark"):
                            if theme.current_mode != mode:
                                self.assertTrue(theme.set_theme(mode), f"could not switch theme to {mode}")
                            pump(0.25)
                            self.assertEqual(window.property("themePaletteMode"), mode)
                            image = window.grabWindow()
                            self.assertFalse(image.isNull(), f"no rendered image for {page_key}/{mode}")
                            path = run_root / f"{page_key}_{mode}_dpr{scale_requested:g}_narrow.png"
                            self.assertTrue(image.save(str(path)), f"could not save screenshot: {path}")
                            screenshots.append(str(path.relative_to(ROOT)))
                        checks.extend(["light-and-dark-live-theme-switch", "real-window-grab-png"])
                        window.resize(1100, 760)
                        pump(0.2)

                        # Source preserves its existing cancel and save-then-
                        # close confirmation flow. Substrate currently has no
                        # dirty close confirmation; closing the saved window is
                        # therefore the applicable preserved behavior.
                        if page_key == "source":
                            bridge.setField("description", "cancel close keeps this draft")
                            self.assertFalse(window.close())
                            pump()
                            discard = expr(window, "discardDialog")
                            self.assertTrue(discard.property("visible"))
                            cancel = item(window, "sourceCancelDiscardButton")
                            self.assertTrue(QMetaObject.invokeMethod(cancel, "click"))
                            pump()
                            self.assertTrue(window.isVisible())
                            self.assertTrue(bridge.dirty)
                            bridge.setField("description", "save close fixture")
                            self.assertFalse(window.close())
                            pump()
                            save_continue = item(window, "sourceSaveAndContinueButton")
                            self.assertTrue(QMetaObject.invokeMethod(save_continue, "click"))
                            pump()
                            save_dialog = item(window, "sourceSaveDialog")
                            close_path = run_root / "fixtures" / "source_close_flow.csrc"
                            save_dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(close_path)))
                            self.assertTrue(QMetaObject.invokeMethod(save_dialog, "accepted"))
                            wait_until(lambda owner=window, editor=bridge: not owner.isVisible() or not editor.dirty,
                                       "source save-then-close completion")
                            self.assertFalse(bridge.dirty)
                            self.assertEqual(service.load("source", close_path).data["description"],
                                             "save close fixture")
                            legacy_path = run_root / "fixtures" / "source_legacy_unnamed.csrc"
                            legacy_data = bridge.documentData()
                            legacy_data.pop("name", None)
                            service.save("source", legacy_path, legacy_data)
                            self.assertTrue(bridge.openPath(str(legacy_path)), bridge.status)
                            self.assertNotIn("name", service.load("source", legacy_path).data)
                            self.assertEqual(bridge.displayName, "未命名")
                            self.assertEqual(bridge.currentPath, str(legacy_path.resolve()))
                            self.assertEqual(window.property("caption"), "蒸发源 — 未命名")
                            self.assertNotIn(legacy_path.name, window.property("caption"))
                            checks.extend(["close-cancel-preserves-draft", "save-then-close-persists-file",
                                           "unnamed-legacy-source-placeholder-retains-path"])
                        else:
                            self.assertTrue(bridge.dirty)
                            bridge.setField("description", "saved and closed")
                            substrate_close_path = run_root / "fixtures" / "substrate_clean_close.csub"
                            self.assertTrue(bridge.saveAsPath(str(substrate_close_path)), bridge.status)
                            self.assertFalse(bridge.dirty)
                            self.assertEqual(service.load("substrate", substrate_close_path).data["description"],
                                             "saved and closed")
                            self.assertTrue(window.close())
                            checks.append("substrate-save-then-clean-close")

                        manifests.append({
                            "page": page_key,
                            "qml": str((ROOT / "coat_sim" / "ui" / "qml" / file_name).relative_to(ROOT)),
                            "minimum_width_logical_px": min_width,
                            "tested_window_widths_logical_px": [1100, min_width],
                            "minimum_height_logical_px": min_height,
                            "requested_scale_factor": scale_requested,
                            "observed_device_pixel_ratio": dpr,
                            "theme_modes": ["light", "dark"],
                            "font_family": app.font().family(),
                            "registered_windows_fonts": registered_fonts,
                            "checks": checks,
                            "screenshots": screenshots,
                            "network_blocked_urls": list(network_factories[-1].blocked_urls),
                        })
                    finally:
                        if page_key == "source":
                            window.setProperty("allowClose", True)
                        window.close()
                        bridge.materialLibrary.waitForBackgroundTasks()
                        engine.deleteLater()
                        bridge.deleteLater()
                        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                        app.processEvents()

            self.assertFalse(qml_warnings, "\n".join(qml_warnings))
            for factory in network_factories:
                self.assertFalse(factory.blocked_urls, "unexpected network access: " + repr(factory.blocked_urls))
            self.assertEqual(len(manifests), 2)
            for manifest in manifests:
                path = ROOT / manifest["qml"]
                manifest["qml_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path = run_root / f"manifest_dpr{os.environ.get('QT_SCALE_FACTOR','1')}.json"
            manifest_path.write_text(json.dumps({
                "run_root": str(run_root.relative_to(ROOT)),
                "requested_dpr": os.environ.get("QT_SCALE_FACTOR", "1"),
                "results": manifests,
                "qml_warnings": qml_warnings,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CompactChromeUiTests))
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    if "--worker" in sys.argv:
        return _worker_main()

    qa_root = ROOT / "build" / ("qa_ui_source_substrate_" + uuid4().hex[:12])
    qa_root.mkdir(parents=True, exist_ok=False)
    logs = []
    for scale in ("1", "1.5"):
        run_root = qa_root / ("dpr_" + scale.replace(".", "_"))
        run_root.mkdir()
        env = os.environ.copy()
        env["SOURCE_SUBSTRATE_QA_ROOT"] = str(run_root)
        env["QT_SCALE_FACTOR"] = scale
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QT_QUICK_BACKEND"] = "software"
        env["QT_QUICK_CONTROLS_STYLE"] = "Fusion"
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                cwd=str(ROOT), env=env, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=180,
            )
            exit_code = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            exit_code = 124
            output += "\nQt worker exceeded the 180 second timeout.\n"
        log_path = run_root / "worker.log"
        log_path.write_text(output, encoding="utf-8")
        logs.append((scale, exit_code, log_path))
        print(f"DPR {scale}: exit={exit_code}; log={log_path.relative_to(ROOT)}")
        if output:
            print(output)
        if exit_code:
            print(f"QA evidence root: {qa_root.relative_to(ROOT)}")
            return exit_code
    summary = {
        "run_root": str(qa_root.relative_to(ROOT)),
        "workers": [{"dpr": scale, "exit_code": code, "log": str(path.relative_to(ROOT))}
                    for scale, code, path in logs],
    }
    (qa_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("QA summary:", qa_root.relative_to(ROOT) / "summary.json")
    return 0


class SourceSubstrateCompactChromeEntrypointTests(unittest.TestCase):
    """Expose the nested scale workers to both pytest and unittest discovery."""

    def test_offscreen_workers_pass_at_both_device_scales(self):
        self.assertEqual(main(), 0)


if __name__ == "__main__":
    raise SystemExit(main())
