"""Save As dialogs keep explicit overwrite authority tied to a typed target."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_QUICK_BACKEND','software')
from pathlib import Path
import json
import tempfile
import unittest
from PySide6.QtWidgets import QApplication
from coat_sim.ui.machine_mask import MachineBridge, MaskBridge
from tests.test_input_snapshot import fixture

APP=QApplication.instance() or QApplication([])

class DesignerTypedSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.service,_=fixture(self.root)

    def test_machine_and_mask_typed_save_confirmation_and_cancellation(self):
        for cls,kind,source in [(MachineBridge,'machine','machines/machine.cmac'),(MaskBridge,'mask','parts/mask0.cmsk')]:
            bridge=cls(self.service);bridge.loadDocument(str(self.root/source))
            self.addCleanup(bridge.deleteLater)
            extension={'machine':'.cmac','mask':'.cmsk'}[kind]
            path=(self.root/source).parent/('save-target'+extension)
            data=json.loads(bridge.documentJson)
            self.service.save(kind,path,data)
            original=path.read_bytes()
            calls=[];bridge.overwriteRequested.connect(lambda url:calls.append(url.toLocalFile()))
            bridge.setMetadata('description','new description')
            bridge.saveDocumentAs(str(path))
            self.assertEqual([Path(value).resolve() for value in calls], [path.resolve()])
            self.assertEqual(path.read_bytes(),original)
            bridge.confirmSaveAs(False);self.assertEqual(path.read_bytes(),original)
            bridge.confirmSaveAs(True);self.assertEqual(path.read_bytes(),original)
            bridge.saveDocumentAs(str(path));bridge.confirmSaveAs(True)
            self.assertEqual(json.loads(path.read_text())['description'],'new description')
            self.assertEqual(bridge.documentPath,str(path))

    def test_edits_and_open_clear_pending_overwrite(self):
        bridge=MaskBridge(self.service);self.addCleanup(bridge.deleteLater)
        source=self.root/'parts/mask0.cmsk';bridge.loadDocument(str(source))
        target=self.root/'parts/target.cmsk';self.service.save('mask',target,json.loads(bridge.documentJson))
        original=target.read_bytes()
        bridge.saveDocumentAs(str(target));bridge.setMetadata('notes','newer edit')
        bridge.confirmSaveAs(True);self.assertEqual(target.read_bytes(),original)
        bridge.saveDocumentAs(str(target));bridge.loadDocument(str(source))
        bridge.confirmSaveAs(True);self.assertEqual(target.read_bytes(),original)

    def test_extensionless_default_and_wrong_type_rejected_before_prompt(self):
        bridge=MaskBridge(self.service);self.addCleanup(bridge.deleteLater)
        bridge.loadDocument(str(self.root/'parts/mask0.cmsk'))
        bridge.saveDocumentAs(str(self.root/'mask-output'))
        self.assertTrue((self.root/'mask-output.cmsk').is_file())
        calls=[];bridge.overwriteRequested.connect(lambda url:calls.append(url.toLocalFile()))
        wrong=self.root/'wrong.csrc';wrong.write_text((self.root/'parts/source.csrc').read_text())
        wrong_bytes=wrong.read_bytes()
        bridge.saveDocumentAs(str(wrong));self.assertEqual(calls,[])
        self.assertIn('.cmsk',bridge.status)
        self.assertIn('.csrc',bridge.status)
        self.assertEqual(wrong.read_bytes(),wrong_bytes)
        wrong_payload=self.root/'wrong-payload.cmsk';wrong_payload.write_bytes(wrong_bytes)
        bridge.saveDocumentAs(str(wrong_payload));self.assertEqual(calls,[])
        self.assertIn('修正板须包含',bridge.status)
        self.assertEqual(wrong_payload.read_bytes(),wrong_bytes)
        corrupt=self.root/'corrupt.coatmask';corrupt.write_text('broken')
        bridge.saveDocumentAs(str(corrupt));self.assertEqual(calls,[])
        self.assertEqual(corrupt.read_text(),'broken');self.assertIn('其他文件名',bridge.status)

    def test_dialog_short_defaults_and_legacy_filters(self):
        qml=Path(__file__).parents[1]/'coat_sim/ui/qml'
        for filename,short,long in [('SourceWindow.qml','csrc','coatsource'),('SubstrateWindow.qml','csub','coatsubstrate'),('MaskWindow.qml','cmsk','coatmask'),('MachineWindow.qml','cmac','coatmachine'),('ProcessWindow.qml','cproc','coatprocess')]:
            text=(qml/filename).read_text()
            self.assertIn('*.'+short, text)
            self.assertNotIn('*.'+long, text)
            self.assertNotIn('*.json', text)
            self.assertIn('defaultSuffix: "'+short+'"',text)
