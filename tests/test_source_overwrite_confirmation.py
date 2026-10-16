"""Explicit source/substrate Save As approval, stale targets and QML chaining."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/coat-overwrite-test-cache")
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PySide6.QtWidgets import QApplication
from coat_sim.core.api import CoatService
from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge
from coat_sim.ui.theme_service import ThemeService

APP = QApplication.instance() or QApplication([])


class SourceSubstrateOverwriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("PROJECT_ROOT", self.root), ("DATA_ROOT", self.root / "data")):
            patcher = patch("coat_sim.core.data_files." + name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.service = CoatService(self.root, restrict_to_root=False)
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.materialLibrary.waitForBackgroundTasks()
            bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()

    def bridge(self, cls):
        bridge = cls(self.service)
        self.bridges.append(bridge)
        return bridge

    def test_existing_targets_require_explicit_confirmation_and_failed_write_keeps_draft(self):
        for cls, suffix in ((SourceBridge, ".csrc"), (SubstrateBridge, ".csub")):
            with self.subTest(kind=cls.kind):
                bridge = self.bridge(cls)
                path = self.root / (cls.kind + suffix)
                self.service.save(cls.kind, path, bridge.documentData())
                original = path.read_bytes()
                bridge.setField("description", "Edited draft")
                draft = deepcopy(bridge.documentData())
                requests = []
                bridge.overwriteRequested.connect(requests.append)
                self.assertFalse(bridge.saveAsPath(str(path)))
                self.assertEqual(bridge.pendingSaveAs, str(path))
                self.assertEqual(len(requests), 1)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(bridge.confirmSaveAs(False))
                self.assertEqual(bridge.pendingSaveAs, "")
                self.assertEqual(bridge.documentData(), draft)
                self.assertTrue(bridge.dirty)
                self.assertFalse(bridge.confirmSaveAs(True))
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(bridge.saveAsPath(str(path)))
                with patch.object(self.service, "save", side_effect=OSError("disk full")) as failing_save:
                    self.assertFalse(bridge.confirmSaveAs(True))
                    failing_save.assert_called_once()
                self.assertEqual(bridge.pendingSaveAs, "")
                self.assertEqual(bridge.documentData(), draft)
                self.assertEqual(path.read_bytes(), original)
                self.assertTrue(bridge.dirty)
                self.assertFalse(bridge.saveAsPath(str(path)))
                self.assertTrue(bridge.confirmSaveAs(True), bridge.status)
                self.assertFalse(bridge.dirty)
                self.assertEqual(self.service.load(cls.kind, path).data["description"], "Edited draft")

    def test_type_preflight_and_new_open_edit_cancel_invalidate_pending_paths(self):
        source = self.bridge(SourceBridge)
        substrate = self.bridge(SubstrateBridge)
        target = self.root / "existing.csrc"
        self.service.save("source", target, source.documentData())
        wrong_suffix = self.root / "other.csub"
        wrong_json = self.root / "other.json"
        self.service.save("substrate", wrong_suffix, substrate.documentData())
        wrong_json.write_text(json.dumps(substrate.documentData(), ensure_ascii=False),
                              encoding="utf-8")
        requests = []
        source.overwriteRequested.connect(requests.append)
        for path in (wrong_suffix, wrong_json):
            original = path.read_bytes()
            self.assertFalse(source.saveAsPath(str(path)))
            self.assertEqual(source.pendingSaveAs, "")
            self.assertFalse(source.confirmSaveAs(True))
            self.assertEqual(path.read_bytes(), original)
        corrupt = self.root / "corrupt.csrc"
        corrupt.write_text("broken-json", encoding="utf-8")
        self.assertFalse(source.saveAsPath(str(corrupt)))
        self.assertIn("原文件保持不变", source.status)
        self.assertIn("其他文件名", source.status)
        self.assertEqual(corrupt.read_text(), "broken-json")
        self.assertFalse(requests)
        for action in (lambda: source.newDocument(),
                       lambda: source.openPath(str(self.root / "missing.csrc")),
                       lambda: source.setField("description", "changed"),
                       lambda: source.applyDocument({"kind":"point"})):
            self.assertFalse(source.saveAsPath(str(target)))
            self.assertTrue(source.pendingSaveAs)
            action()
            self.assertEqual(source.pendingSaveAs, "")
            self.assertFalse(source.confirmSaveAs(True))
        self.assertEqual(self.service.load("source", target).data["description"], "")

    def test_actual_qml_save_and_continue_waits_for_overwrite_success(self):
        source = self.bridge(SourceBridge)
        target = self.root / "existing.csrc"
        self.service.save("source", target, source.documentData())
        source.setField("source_exponent", "3")
        engine = QQmlApplicationEngine()
        ThemeService.instance().attach_engine(engine, "Theme")
        warnings = []
        engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
        engine.rootContext().setContextProperty("bridge", source)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]/"coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        context = QQmlEngine.contextForObject(window)
        def expression(text):
            expr = QQmlExpression(context, window, text)
            value, _ = expr.evaluate()
            self.assertFalse(expr.hasError(), expr.error())
            return value
        def start_save():
            source.newDocument()
            self.assertEqual(source.pendingAction, "new")
            self.assertTrue(QMetaObject.invokeMethod(window, "saveAndContinue"))
            APP.processEvents()
            expression("saveDialog").setProperty("selectedFile", QUrl.fromLocalFile(str(target)))
            self.assertTrue(QMetaObject.invokeMethod(expression("saveDialog"), "accepted"))
            APP.processEvents()
        try:
            start_save()
            self.assertTrue(expression("saveThenContinue"))
            self.assertTrue(expression("overwriteDialog.visible"))
            self.assertEqual(source.documentData()["source_exponent"], 3)
            self.assertEqual(self.service.load("source", target).data["source_exponent"], 1)
            self.assertTrue(QMetaObject.invokeMethod(expression("overwriteDialog"), "reject"))
            APP.processEvents()
            self.assertEqual(source.pendingSaveAs, "")
            self.assertEqual(source.documentData()["source_exponent"], 3)
            self.assertTrue(expression("discardDialog.visible"))
            source.confirmDiscard(False)
            start_save()
            with patch.object(self.service, "save", side_effect=OSError("disk full")) as failing_save:
                self.assertTrue(QMetaObject.invokeMethod(expression("overwriteDialog"), "accept"))
            APP.processEvents()
            failing_save.assert_called_once()
            self.assertEqual(source.pendingSaveAs, "")
            self.assertEqual(source.pendingAction, "new")
            self.assertTrue(source.dirty)
            self.assertEqual(source.documentData()["source_exponent"], 3)
            self.assertEqual(self.service.load("source", target).data["source_exponent"], 1)
            self.assertTrue(expression("discardDialog.visible"))
            start_save()
            self.assertTrue(QMetaObject.invokeMethod(expression("overwriteDialog"), "accept"))
            APP.processEvents()
            self.assertFalse(expression("saveThenContinue"))
            self.assertEqual(source.pendingAction, "")
            self.assertEqual(source.documentData()["source_exponent"], 1)
            self.assertFalse(source.dirty)
            self.assertEqual(self.service.load("source", target).data["source_exponent"], 3)
            self.assertFalse(warnings, "\n".join(warnings))
        finally:
            window.setProperty("allowClose", True); window.close(); engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
