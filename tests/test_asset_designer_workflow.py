"""Explicit legacy readers preserve portable inputs after design UI migration."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, QUrl, QCoreApplication, QEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtWidgets import QApplication
from tests.test_process_optimization_modes import fixture
from coat_sim.core.simulator import Settings
from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge
from coat_sim.ui.machine_mask import MachineBridge, MaskBridge
from coat_sim.ui.process import ProcessBridge
from coat_sim.ui.main import MainBridge
from coat_sim.ui.asset_browser import AssetBrowserBridge
from coat_sim.scheduler import SchedulerClient
from tests.test_scheduler_service import stop_service

app = QApplication.instance() or QApplication([])
if not isinstance(app, QApplication):
    raise RuntimeError('Asset designer tests require a QApplication singleton')
APP: QApplication = app
ROOT = Path(__file__).resolve().parents[1]

class AssetDesignerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridges = []
        self.addCleanup(self.stop_owned_scheduler)
        for name, value in [('PROJECT_ROOT',self.root),('DATA_ROOT',self.root/'data')]:
            p = patch('coat_sim.core.data_files.'+name, value)
            p.start()
            self.addCleanup(p.stop)
        self.service, self.path = fixture(self.root, mask_count=1, source_count=1)
        settings = Settings(grid_size=21,time_steps=8)
        snapshot = self.service.freeze_process(self.service.load('process',self.path).data,base_dir=self.root)
        result = self.service.simulate_recipe(settings,process_path=snapshot,backend='CPU')
        receipt = self.service.prepare_simulation_asset(result=result,settings=settings,
            machine=snapshot['runtime']['machine'],process=snapshot['runtime']['profile'],input_snapshot=snapshot,
            elapsed_s=.1,requested_device='CPU')
        self.package = Path(receipt['temporary_bundle_path'])
        self.run_id = receipt['run_id']

    def stop_owned_scheduler(self):
        for bridge in self.bridges:
            detach = getattr(bridge, 'detach_observers', None)
            if detach is not None:
                detach()
            target = getattr(bridge, '_compute_target', None)
            if target is not None:
                # A running capability probe can start this test's daemon.
                # Finish it before checking the endpoint or removing its root.
                target._poll.stop()
                target._executor.shutdown(wait=True, cancel_futures=True)
        if (self.root / 'artifacts/runtime/scheduler/endpoint.json').is_file():
            stop_service(SchedulerClient(self.root))
        for bridge in self.bridges:
            bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.bridges.clear()

    def test_legacy_reader_exports_inputs_that_each_designer_can_open(self):
        pairs = [('source',SourceBridge),('substrate',SubstrateBridge),('mask',MaskBridge),('machine',MachineBridge),('process',ProcessBridge)]
        for kind, cls in pairs:
            bridge=cls(self.service)
            self.bridges.append(bridge)
            # Exercise the retained compatibility adapter explicitly. Ordinary
            # design windows no longer expose a package-browser QML entry.
            def open_exported(path, editor=bridge):
                if isinstance(editor, (MachineBridge, MaskBridge)):
                    editor.loadDocument(str(path))
                else:
                    editor.openPath(QUrl.fromLocalFile(str(path)))
            browser=AssetBrowserBridge(self.service,kind,open_exported,bridge)
            self.assertTrue(browser.inspect(QUrl.fromLocalFile(str(self.package))),browser.status)
            designs = browser.property('designs')
            assert isinstance(designs, list)
            index=next(i for i,r in enumerate(designs) if r['kind']==kind)
            self.assertIn('{',browser.preview(index))
            destination=self.root/('export-'+kind)
            browser.export(QUrl.fromLocalFile(str(destination)),index,False,True)
            self.assertNotIn('失败',browser.property('status'))
            target=destination/designs[index]['path']
            self.assertTrue(target.is_file(),browser.status)
            self.assertTrue(self.service.load(kind,target).valid)
            self.assertEqual(Path(bridge._path).resolve(), target.resolve())
            self.assertFalse(bridge.dirty)
            self.assertFalse(list(destination.rglob('*.npz')))
            requested: list[bool] = []
            browser.overwriteRequested.connect(lambda requests=requested: requests.append(True))
            browser.export(QUrl.fromLocalFile(str(destination)),index,False,True)
            self.assertEqual(requested,[True])
            browser.confirmExport(False)
        main=MainBridge(self.service)
        self.bridges.append(main)
        main.openAsset(QUrl.fromLocalFile(str(self.package)))
        self.assertTrue(main.hasResult,main.status)
        saved=self.service.save_simulation_asset(self.run_id,'Inputs only',confirmed=True,include_results=False)
        main.openAsset(QUrl.fromLocalFile(saved['asset_path']))
        self.assertIn('不含结果',main.property('status'))
        self.assertTrue(self.service.load('process',saved['asset_path']).valid)
        browser=AssetBrowserBridge(self.service,'process',lambda _path: None,main)
        self.assertTrue(browser.inspect(QUrl.fromLocalFile(str(self.package))),browser.status)
        out=self.root/'all-with-results'
        browser.export(QUrl.fromLocalFile(str(out)),-1,True,False)
        self.assertTrue(list(out.rglob('*.npz')),browser.status)

    def test_all_six_windows_load_without_ordinary_legacy_package_dialog(self):
        for cls,qml in [(MainBridge,'MainWindow'),(ProcessBridge,'ProcessWindow'),(MachineBridge,'MachineWindow'),
                        (MaskBridge,'MaskWindow'),(SourceBridge,'SourceWindow'),(SubstrateBridge,'SubstrateWindow')]:
            bridge=cls(self.service)
            self.bridges.append(bridge)
            engine=QQmlApplicationEngine()
            engine.rootContext().setContextProperty('bridge',bridge)
            if cls is MainBridge:
                engine.addImageProvider('thickness',bridge.image_provider)
            engine.load(QUrl.fromLocalFile(str(ROOT/'coat_sim/ui/qml'/f'{qml}.qml')))
            self.assertTrue(engine.rootObjects(),qml)
            window=engine.rootObjects()[0]
            assert isinstance(window, QQuickWindow)
            self.assertFalse(any('AssetBundle' in child.objectName()
                                 for child in window.findChildren(QObject)))
            source=(ROOT/'coat_sim/ui/qml'/f'{qml}.qml').read_text(encoding='utf-8')
            self.assertNotIn('AssetBundleDialog',source)
            self.assertNotIn('AssetBundleLoader',source)
            window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
