"""Optical record validation, safe references, async latest-selection and real QML."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/coat-optical-test-cache")
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QUrl
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from coat_sim.core import materials, optical_constants as optics
from coat_sim.core.schemas import MaterialRecord, SourceDesign, SubstrateDesign
from pydantic import TypeAdapter
from coat_sim.core.substrate_profiles import new_substrate_profile
from coat_sim.ui import source_materials
from coat_sim.ui.material_research import markdown_to_safe_html, prepare_reference_sections, safe_source_url
from coat_sim.ui.source_materials import MaterialLibraryBridge

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


def wait_library(lib, timeout=10):
    end = time.monotonic() + timeout
    while lib.loading and time.monotonic() < end:
        APP.processEvents(); time.sleep(.005)
    APP.processEvents()
    if lib.loading:
        raise AssertionError("Material worker did not complete")


def model(kind="cauchy", unit="um", **overrides):
    row = dict(id="test", name="Sample", notes="measured conditions", source="test", kind=kind,
               wavelength_unit=unit, wavelength_min=.4, wavelength_max=.8,
               parameters={"A":1.5,"B":.004,"C":0} if kind == "cauchy" else
               {"B1":.6961663,"B2":.4079426,"B3":.8974794,"C1":.0684043**2,"C2":.1162414**2,"C3":9.896161**2})
    row.update(overrides)
    return row


class OpticalCoreTests(unittest.TestCase):
    def test_missing_k_units_roundtrip_and_no_physics_inference(self):
        table = optics.import_optical_text("wavelength,n\n400,1.6\n800,1.5",name="Measured",wavelength_unit="nm")
        self.assertIsNone(table["k"])
        self.assertEqual(optics.evaluate_optical_set(table,.6,"um"),(1.55,None))
        for category in ("film","substrate","other"):
            material = materials.default_material_profile("Example")
            material.update(category=category,optical_constants=[table,model()])
            validated = materials.validate_material_profile(material)
            typed = MaterialRecord.model_validate(validated).model_dump(exclude_none=True)
            self.assertEqual(materials.validate_material_profile(typed),validated)
            self.assertEqual(materials.source_bindable(material),category=="film")
            self.assertEqual(materials.substrate_bindable(material),category=="substrate")
            self.assertEqual(materials.material_completeness(material)["thermodynamic_rate_available"],False)

    def test_unit_conversion_endpoints_allow_only_roundoff(self):
        table = optics.import_optical_text("0.4 1.5\n0.7 1.6",name="Endpoint",wavelength_unit="um")
        self.assertEqual(optics.evaluate_optical_set(table,700,"nm"),(1.6,None))
        self.assertEqual(optics.evaluate_optical_set(table,400,"nm"),(1.5,None))
        with self.assertRaises(ValueError): optics.evaluate_optical_set(table,700.00001,"nm")

    def test_tabulated_import_strict_validation(self):
        for text in ("400,1.5\n400,1.6","800,1.5\n400,1.6","400,nan\n800,1.5",
                     "400,1.5,\n800,1.6,0","400,1.5,0\n800,1.6", "400,1.5,-1\n800,1.6,0"):
            with self.subTest(text=text),self.assertRaises(ValueError):
                optics.import_optical_text(text,name="Data",wavelength_unit="nm")
        with self.assertRaises(ValueError):
            optics.import_optical_text("400 1.5\n800 1.6",name="Data",wavelength_unit="")
        table=optics.import_optical_text("# explicit data\n400 1.5 0\n800 1.6 .1",name="Data",wavelength_unit="nm")
        self.assertEqual(optics.evaluate_optical_set(table,400,"nm"),(1.5,0))
        with self.assertRaises(ValueError):optics.evaluate_optical_set(table,399,"nm")
        table["n"][0]=True
        with self.assertRaises(ValueError):optics.validate_optical_set(table)

    def test_models_domains_singularities_and_unknown_k(self):
        n,k=optics.evaluate_optical_set(model(),500,"nm")
        self.assertAlmostEqual(n,1.516);self.assertIsNone(k)
        n,k=optics.evaluate_optical_set(model("sellmeier"),.5876,"um")
        self.assertAlmostEqual(n,1.45846,places=4);self.assertIsNone(k)
        for bad in (model(parameters={"A":1.5,"B":float("inf"),"C":0}),
                    model(wavelength_min=0),model(wavelength_min=1),model(parameters={"A":-1,"B":0,"C":0}),
                    model("sellmeier",wavelength_min=.01),model("sellmeier",wavelength_max=10),
                    dict(model(),k=0)):
            with self.subTest(bad=bad),self.assertRaises(ValueError):optics.validate_optical_set(bad)
        with self.assertRaises(ValueError):optics.evaluate_optical_set(model(),.9,"um")

    def test_typed_optics_require_explicit_units_and_reject_bool_before_coercion(self):
        bad_sets = [model(), model()]
        bad_sets[0].pop('wavelength_unit')
        bad_sets[1]['parameters']['A'] = True
        for bad in bad_sets:
            material = materials.default_material_profile('Strict optics')
            material['optical_constants'] = [bad]
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MaterialRecord.model_validate(material)
            with self.subTest(path='source',bad=bad), self.assertRaises(ValueError):
                TypeAdapter(SourceDesign).validate_python({'kind':'point','material':material})
            substrate = new_substrate_profile()
            material['category'] = 'substrate'
            substrate['material'] = material
            with self.subTest(path='substrate',bad=bad), self.assertRaises(ValueError):
                SubstrateDesign.model_validate(substrate)

    def test_legacy_profile_and_evidence_survive_new_fields(self):
        row=materials.builtin_material_profiles()[0]
        typed=MaterialRecord.model_validate(row).model_dump()
        self.assertEqual(materials.validate_material_profile(typed),row)
        edited=deepcopy(row);edited['optical_constants']=[model()]
        self.assertEqual(materials.validate_material_profile(edited)['research_data'],row['research_data'])
        legacy=materials.default_material_profile("Legacy")
        self.assertTrue(materials.source_bindable(legacy));self.assertFalse(materials.substrate_bindable(legacy))
        self.assertNotIn('optical_constants',legacy)

    def test_markdown_safe_offline_and_bounded(self):
        html=markdown_to_safe_html('# Heading\n\n**bold**\n\n| λ | n |\n|---|---|\n|400|1.5|\n\n<script>alert(1)</script>\n\n![image](https://example.org/a.png) [unsafe](file:///etc/passwd)')
        self.assertIn('<h1>',html);self.assertIn('<table',html);self.assertIn('<b>bold</b>',html)
        self.assertNotIn('<script>',html);self.assertNotIn('<img',html);self.assertNotIn('href="file:',html)
        self.assertEqual(safe_source_url('javascript:alert(1)'),"")
        research=materials.builtin_material_profiles()[0]['research_data']
        sections=prepare_reference_sections(None,research)
        self.assertTrue(any(row['html'] for row in sections))
        self.assertTrue(all(len(row['body'])<=12000 for row in sections))


class OpticalLibraryUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.lib=MaterialLibraryBridge(directory=Path(self.tmp.name));wait_library(self.lib)
    def tearDown(self):
        self.lib.cancelPendingLoad();self.lib.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents()
    def test_async_latest_selection_cancel_and_thread_isolation(self):
        rows=self.lib.entries[:3]; gate=threading.Event(); seen=[]
        original=source_materials._prepare_material
        def slow(row):
            seen.append(threading.get_ident());gate.wait(3);return original(row)
        with patch.object(source_materials,'_prepare_material',slow):
            self.assertTrue(self.lib.selectMaterial(rows[0]['key']))
            self.assertTrue(self.lib.selectMaterial(rows[1]['key']))
            gate.set();wait_library(self.lib)
            self.assertEqual(self.lib.currentKey,rows[1]['key'])
        self.assertTrue(all(t != threading.get_ident() for t in seen))
        before=self.lib.draftData();gate.clear()
        with patch.object(source_materials,'_prepare_material',slow):
            self.lib.selectMaterial(rows[2]['key']);self.lib.cancelPendingLoad();gate.set()
            for _ in range(20):APP.processEvents();time.sleep(.01)
        self.assertEqual(self.lib.draftData(),before)
    def test_multiple_datasets_save_copy_and_failed_import_preserve(self):
        self.lib.newMaterial();self.lib.setField('name','Substrate');self.lib.setCategory('substrate')
        self.assertTrue(self.lib.addDispersionModel('Cauchy','cauchy','um','.4','.8','{"A":1.5,"B":0,"C":0}','Notes'))
        path=Path(self.tmp.name)/'nk.csv';path.write_text('400,1.5,0\n800,1.6,0.1')
        self.assertTrue(self.lib.importOpticalFile(str(path),'Measurement','nm','Measured'))
        before=self.lib.draftData()
        path.write_text('400,nan\n800,1.6');self.assertFalse(self.lib.importOpticalFile(str(path),'Invalid','nm',''))
        self.assertEqual(self.lib.draftData(),before)
        self.assertTrue(self.lib.save());wait_library(self.lib)
        self.lib.setRoleFilter('film')
        self.assertNotIn(self.lib.currentKey, [row['key'] for row in self.lib.filteredEntries])
        self.lib.setRoleFilter('substrate')
        self.assertIn(self.lib.currentKey, [row['key'] for row in self.lib.filteredEntries])
        self.assertEqual(materials.read_material_profile(Path(self.tmp.name)/(self.lib.currentId+'.json'))['optical_constants'],before['optical_constants'])
        self.assertTrue(self.lib.copyMaterial());wait_library(self.lib)
        self.assertEqual(self.lib.draftData()['optical_constants'],before['optical_constants'])
        self.assertTrue(self.lib.dirty);self.assertFalse(self.lib.canUseMaterial)
    def test_source_picker_emits_only_film_and_substrate_purpose(self):
        selected=[];self.lib.materialSelected.connect(selected.append)
        nonfilm=next(row for row in self.lib.entries if row['category']=='substrate')
        self.assertFalse(self.lib.selectSourceMaterial(nonfilm['key']))
        film=self.lib.filmEntries[0];self.lib.selectSourceMaterial(film['key']);wait_library(self.lib)
        self.assertEqual(len(selected),1);self.assertTrue(materials.source_bindable(selected[0]))
        substrate=MaterialLibraryBridge(directory=Path(self.tmp.name),purpose='substrate')
        try:
            wait_library(substrate);substrate.prepareForSubstrate();wait_library(substrate)
            self.assertTrue(substrate.substrateBindable);self.assertTrue(substrate.canUseMaterial)
        finally:
            substrate.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents()
    def test_use_button_fast_snapshot_is_immutable_under_all_library_edits(self):
        from coat_sim.ui.source_substrate import SourceBridge
        from coat_sim.core.api import CoatService
        source = SourceBridge(CoatService(root=Path(self.tmp.name), restrict_to_root=False))
        lib = source.materialLibrary
        try:
            wait_library(lib)
            ito = next(row for row in lib.entries if row['name'] == 'ITO')
            self.assertTrue(lib.selectMaterial(ito['key'])); wait_library(lib)
            self.assertTrue(lib.copyMaterial()); wait_library(lib)
            self.assertTrue(lib.addDispersionModel('Snapshot optics', 'cauchy', 'um', '.4', '.8', '{"A":1.5,"B":0,"C":0}', 'initial'))
            original_validate = materials.validate_material_profile
            def no_full_evidence_on_commit(value):
                self.assertNotIn('research_data', value)
                self.assertNotIn('reference_data', value)
                self.assertNotIn('optical_constants', value)
                return original_validate(value)
            with patch.object(materials, 'validate_material_profile', no_full_evidence_on_commit):
                self.assertTrue(lib.useMaterial())
            before = source.documentData()['material']
            optical_id = lib.opticalSets[0]['id']
            lib.setField('name', 'Edited after use')
            lib.setField('notes', 'Changed notes')
            self.assertTrue(lib.editOpticalMetadata(optical_id, 'Renamed', 'Changed optical notes'))
            lib.removeOpticalSet(optical_id)
            self.assertEqual(source.documentData()['material'], before)
            detached = lib.draftData()
            detached['research_data']['name'] = 'Caller mutation'
            self.assertNotEqual(lib.researchData['name'], 'Caller mutation')
        finally:
            lib.cancelPendingLoad(); source.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete); APP.processEvents()

    def test_actual_qml_optical_and_reference_dialog_controls(self):
        from coat_sim.ui.source_substrate import SourceBridge
        from coat_sim.core.api import CoatService
        from coat_sim.ui.theme_service import ThemeService
        source=SourceBridge(CoatService(root=Path(self.tmp.name),restrict_to_root=False));wait_library(source.materialLibrary)
        engine=QQmlApplicationEngine();warnings=[]
        engine.warnings.connect(lambda rows:warnings.extend(row.toString() for row in rows))
        engine.rootContext().setContextProperty('bridge',source)
        ThemeService.instance().attach_engine(engine, 'Theme')
        engine.load(QUrl.fromLocalFile(str(ROOT/'coat_sim/ui/qml/SourceWindow.qml')))
        self.assertTrue(engine.rootObjects());window=engine.rootObjects()[0]
        try:
            source.materialLibrary.prepareForSource()
            self.assertTrue(QMetaObject.invokeMethod(window, 'openMaterialLibrary'))
            deadline=time.monotonic()+8.0
            controller=engine.incubationController()
            dialog=None
            while time.monotonic()<deadline:
                APP.processEvents()
                if controller is not None:
                    controller.incubateFor(2)
                dialog=window.findChild(QObject,'materialLibraryDialog')
                if dialog is not None and dialog.property('visible'):
                    break
                QTest.qWait(10)
            self.assertIsNotNone(dialog, 'lazy material library dialog was not created')
            self.assertTrue(dialog.property('visible'))
            wait_library(source.materialLibrary)
            reference_button=dialog.findChild(QObject,'materialReferenceDetailsButton')
            self.assertIsNotNone(reference_button)
            self.assertTrue(QMetaObject.invokeMethod(reference_button,'clicked'))
            APP.processEvents()
            refs=dialog.findChild(QObject,'materialReferenceDetailsDialog')
            self.assertIsNotNone(refs)
            self.assertTrue(refs.property('visible'))
            self.assertTrue(QMetaObject.invokeMethod(refs,'close'))
            source.materialLibrary.newMaterial();APP.processEvents()
            add_button=dialog.findChild(QObject,'addOpticalSetButton')
            self.assertIsNotNone(add_button)
            self.assertTrue(QMetaObject.invokeMethod(add_button,'clicked'));APP.processEvents()
            editor=dialog.findChild(QObject,'materialEditor')
            self.assertIsNotNone(editor)
            self.assertTrue(editor.property('visible'))
            self.assertFalse(warnings,'\n'.join(warnings))
        finally:
            window.setProperty('allowClose',True);window.close();engine.deleteLater();source.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents()

if __name__=='__main__':unittest.main()
