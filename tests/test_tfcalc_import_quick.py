"""Synthetic Quick import, cancellation, focus and shared-session integration."""
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QUrl, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from coat_sim.ui.theme_service import ThemeService

pytestmark = pytest.mark.qt
_application = QGuiApplication.instance() or QGuiApplication([])
assert isinstance(_application, QGuiApplication)
APP: QGuiApplication = _application
ROOT = Path(__file__).parents[1]


@pytest.fixture
def controller(tmp_path):
    library = MaterialLibrary(tmp_path)
    for identifier, role, indices in (("synthetic_glass", "substrate", [("glass", 1.5)]),
                                     ("synthetic_film", "film", [("high", 2.0), ("low", 1.4)])):
        record = default_material_profile(identifier, id=identifier)
        record.update(category=role, optical_constants=[
            {"id": name, "name": name, "kind": "tabulated", "source": "synthetic test oracle",
             "notes": "Not production optical data", "wavelength_unit": "nm",
             "wavelength_min": 400, "wavelength_max": 800,
             "wavelengths": [400, 800], "n": [index, index], "k": [0, 0]}
            for name, index in indices])
        library.write(record)
    service = FilmDesignService(tmp_path)
    value = FilmDesignQuickController(tmp_path, service=service,
                                      geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    yield value
    value.markSourceClosed()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "synthetic.tfd"
    path.write_text("VERSION*1*\nENVIRON*uninterpreted*reference*\nFORMULA2*1*(AB)^8*\n"
                    "LAYERS*2*\nLAYER*1*alpha*1.25*80.5*unsupported*\nLAYER*2*beta*0.75*91.25*\n"
                    "LAYERS2*0*\nEOF*\n", encoding="utf-8")
    return path


def options(controller):
    return {"source_table": "primary", "thickness_field": 4, "thickness_unit": "nm",
            "source_order": "substrate_outward", "target_side": "front",
            "reference_wavelength_nm": controller.currentDesign()["reference_wavelength_nm"],
            "preserve_environment_confirmed": True,
            "mapping": {"alpha": {"material_id": "synthetic_film", "optical_set_id": "high"},
                        "beta": {"material_id": "synthetic_film", "optical_set_id": "low"}}}


def preview(controller, source):
    assert controller.inspectTfcalcFile(QUrl.fromLocalFile(str(source)))
    result = controller.previewTfcalcImport(options(controller))
    assert result["ok"], result
    return result


def test_preview_cancel_confirm_roundtrip_and_existing_paired_simulation(controller, source, tmp_path):
    original_file = source.read_bytes()
    existing_path = tmp_path / "before.cfilm"
    assert controller.saveFilePath(str(existing_path), False)
    controller.setDesignValue("name", "dirty retained name")
    before = controller.currentDesign()
    revision = controller.session.snapshot().design_revision
    paired = FilmSimulationQuickController(tmp_path, service=controller.service, source_controller=controller)
    try:
        result = preview(controller, source)
        assert controller.currentDesign() == before
        assert controller.requestApplyTfcalcImport(result["token"])
        assert controller.currentDesign() == before
        assert paired_design(paired) == before
        controller.resolvePendingAction(False)
        assert controller.currentDesign() == before
        assert controller.session.snapshot().path == str(existing_path)
        assert controller.requestApplyTfcalcImport(result["token"])
        controller.resolvePendingAction(True)
        after = controller.currentDesign()
        assert [row["thickness"]["value"] for row in after["front_layers"]] == [80.5, 91.25]
        assert after["name"] == "dirty retained name"
        assert after["expression"]["text"] == ""
        assert "FORMULA2*1*(AB)^8*" in after["note"]
        assert controller.session.snapshot().path is None and controller.dirty
        assert controller.session.snapshot().design_revision == revision + 1
        assert paired._source_session is controller.session
        assert paired_design(paired) == after
        assert source.read_bytes() == original_file
        assert controller.saveFilePath(str(tmp_path / "imported.cfilm"), False)
        assert controller.service.load(tmp_path / "imported.cfilm") == after
    finally:
        paired.close()


@pytest.mark.parametrize("invalidate", ["cancel", "reopen", "failed_reopen", "disabled", "editing", "closed", "workflow"])
def test_stale_previews_are_rejected_without_polluting_design(controller, source, tmp_path, invalidate):
    before = controller.currentDesign()
    result = preview(controller, source)
    if invalidate == "cancel":
        controller.clearTfcalcImport()
    elif invalidate == "reopen":
        controller.inspectTfcalcFile(QUrl.fromLocalFile(str(source)))
    elif invalidate == "failed_reopen":
        controller.inspectTfcalcFile(QUrl.fromLocalFile(str(tmp_path / "missing.tfd")))
    elif invalidate == "disabled":
        controller.setTfcalcPluginEnabled(False)
    elif invalidate == "editing":
        controller.markEditingDirty()
    elif invalidate == "closed":
        controller.markSourceClosed()
    else:
        controller.setWorkflowConfiguration("simulation", {"samples": 17})
        before = controller.currentDesign()
    assert not controller.requestApplyTfcalcImport(result["token"])
    assert controller.currentDesign() == before


def test_import_inputs_changed_during_pending_confirmation_cancel_old_request(controller, source):
    before = controller.currentDesign()
    result = preview(controller, source)
    assert controller.requestApplyTfcalcImport(result["token"])
    controller.invalidateTfcalcPreview()
    controller.resolvePendingAction(True)
    assert controller.currentDesign() == before


@pytest.mark.parametrize("inputs", [{"unexpected": True}, None])
def test_repeated_preview_cancels_pending_old_confirmation(controller, source, inputs):
    before = controller.currentDesign()
    result = preview(controller, source)
    assert controller.requestApplyTfcalcImport(result["token"])
    repeated = controller.previewTfcalcImport(options(controller) if inputs is None else inputs)
    assert repeated["ok"] is (inputs is None)
    controller.resolvePendingAction(True)
    assert controller.currentDesign() == before


def test_global_reference_wavelength_change_discloses_and_retains_qw_record(controller, source):
    controller.setSubstrateValue("sides", "double")
    controller.addLayer("back")
    identity = controller.back_layers.ids()[0]
    controller.setLayerValue("back", identity, "material_id", "synthetic_film")
    controller.setLayerValue("back", identity, "optical_set_id", "high")
    controller.setLayerValue("back", identity, "thickness.unit", "qw")
    before = controller.currentDesign()
    before_physical_nm = controller.schematicLayers("back")[0]["physicalNmValue"]
    assert controller.inspectTfcalcFile(QUrl.fromLocalFile(str(source)))
    inputs = options(controller)
    inputs["reference_wavelength_nm"] = 600
    result = controller.previewTfcalcImport(inputs)
    assert result["ok"]
    assert controller.requestApplyTfcalcImport(result["token"])
    controller.resolvePendingAction(True)
    assert controller.currentDesign()["back_layers"] == before["back_layers"]
    assert controller.currentDesign()["reference_wavelength_nm"] == 600
    assert controller.schematicLayers("back")[0]["physicalNmValue"] == pytest.approx(before_physical_nm * 600 / 550)


def evaluate(engine, scope, code):
    expression = QQmlExpression(engine.rootContext(), scope, code)
    result = expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    return result[0] if isinstance(result, tuple) else result


def paired_design(controller: FilmSimulationQuickController) -> dict:
    # Reading the Qt property also tests the public meta-object boundary; the
    # PySide Property decorator is not a Python str descriptor in type stubs.
    encoded = controller.property("designJson")
    assert isinstance(encoded, str)
    return json.loads(encoded)


def visual_find(window: QQuickWindow, object_name: str) -> QQuickItem | None:
    content = window.contentItem()
    assert content is not None
    pending = [content]
    while pending:
        item = pending.pop()
        if item.objectName() == object_name:
            return item
        pending.extend(item.childItems())
    return None


def test_real_qml_open_mapping_preview_cancel_and_geometry(controller, source, tmp_path):
    engine = QQmlApplicationEngine()
    ThemeService.instance().set_theme("light")
    ThemeService.instance().attach_engine(engine, "Theme")
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")))
    window = component.createWithInitialProperties({"controller": controller})
    assert isinstance(window, QQuickWindow), "\n".join(err.toString() for err in component.errors())
    try:
        window.show()
        QTest.qWait(50)
        windows_before = len(APP.allWindows())
        before = controller.currentDesign()
        controller.requestOpenFile(QUrl.fromLocalFile(str(source)))
        QTest.qWait(50)
        dialog = window.findChild(QObject, "tfcalcImportDialog")
        assert dialog is not None and dialog.property("opened")
        assert len(APP.allWindows()) == windows_before
        assert controller.currentDesign() == before
        # All selection changes run the actual QML handlers, including QVariant
        # conversion of the option object used by the preview button.
        material_index = next(index for index, row in enumerate(controller.filmOptions)
                              if row["id"] == "synthetic_film")
        for alias, set_id in (("alpha", "high"), ("beta", "low")):
            picker = visual_find(window, "tfcalcMaterial_" + alias)
            assert picker is not None and picker.property("enabled")
            evaluate(engine, picker, f"currentIndex = {material_index}; activated({material_index})")
            QTest.qWait(20)
            dataset = visual_find(window, "tfcalcDataset_" + alias)
            assert dataset is not None
            set_index = next(index for index, row in enumerate(controller.opticalSetsForFilm("synthetic_film"))
                             if row["id"] == set_id)
            evaluate(engine, dataset, f"currentIndex = {set_index}; activated({set_index})")
        controller_before = controller.currentDesign()
        preserve = dialog.findChild(QObject, "tfcalcPreserveEnvironment")
        assert preserve is not None
        preserve.setProperty("checked", True)
        evaluate(engine, dialog, "controller.previewTfcalcImport(parameters())")
        QTest.qWait(30)
        assert controller.tfcalcImportState["ready"], controller.tfcalcImportState
        assert controller.currentDesign() == controller_before
        button = dialog.findChild(QObject, "tfcalcApplyButton")
        assert button is not None
        assert button.property("enabled")
        assert controller.requestApplyTfcalcImport(controller.tfcalcImportState["token"])
        confirmation = window.findChild(QObject, "filmDesignReplacementConfirmation")
        assert confirmation is not None
        evaluate(engine, confirmation, "reject()")
        assert controller.currentDesign() == before
        # Explicit source reopen updates state without accumulating a window.
        controller.requestOpenFile(QUrl.fromLocalFile(str(source)))
        QTest.qWait(40)
        assert len(APP.allWindows()) == windows_before
        reopened_parameters = evaluate(engine, dialog, "parameters()")
        if hasattr(reopened_parameters, "toVariant"):
            reopened_parameters = reopened_parameters.toVariant()
        assert reopened_parameters["mapping"] == {}
        assert reopened_parameters["preserve_environment_confirmed"] is False
        for alias in ("alpha", "beta"):
            picker = visual_find(window, "tfcalcMaterial_" + alias)
            dataset = visual_find(window, "tfcalcDataset_" + alias)
            assert picker is not None and dataset is not None
            assert picker.property("currentIndex") == 0
            assert dataset.property("currentIndex") == 0
        for width, height in ((720, 640), (1100, 760)):
            window.setProperty("minimumWidth", 320)
            window.setProperty("minimumHeight", 320)
            window.resize(width, height)
            QTest.qWait(60)
            assert dialog.property("width") <= width
            assert dialog.property("height") <= height
            footer = dialog.findChild(QQuickItem, "tfcalcApplyButton")
            assert footer is not None
            assert footer.width() > 0
            image = window.grabWindow()
            assert not image.isNull()
            assert image.save(str(tmp_path / f"tfcalc-synthetic-{width}.png"))
        evaluate(engine, dialog, "close()")
        QTest.qWait(40)
        assert not controller.tfcalcImportState["loaded"]
        assert controller.currentDesign() == before
    finally:
        window.setProperty("allowClose", True)
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()


def test_qml_open_flushes_uncommitted_name_before_capturing_import_base(controller, source):
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine, "Theme")
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")))
    window = component.createWithInitialProperties({"controller": controller})
    assert isinstance(window, QQuickWindow), "\n".join(err.toString() for err in component.errors())
    try:
        window.show()
        QTest.qWait(50)
        name = window.findChild(QQuickItem, "filmDesignNameField")
        assert name is not None
        name.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        for key in (Qt.Key.Key_N, Qt.Key.Key_E, Qt.Key.Key_W):
            QTest.keyClick(window, key)
        assert name.property("text") == "new"
        assert controller.currentDesign()["name"] != "new"
        evaluate(engine, window, "openDocumentFile(" + json.dumps(QUrl.fromLocalFile(str(source)).toString()) + ")")
        QTest.qWait(70)
        assert controller.currentDesign()["name"] == "new"
        assert controller._tfcalc_base_design["name"] == "new"
        result = controller.previewTfcalcImport(options(controller))
        assert result["ok"], result
        assert controller.requestApplyTfcalcImport(result["token"])
        controller.resolvePendingAction(True)
        QTest.qWait(50)
        assert controller.currentDesign()["name"] == "new"
        assert len(controller.currentDesign()["front_layers"]) == 2
    finally:
        window.setProperty("allowClose", True)
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()


def test_qml_material_aliases_are_literal_plain_text_and_dictionary_keys(controller, source):
    source.write_text("VERSION*1*\nLAYERS*2*\nLAYER*1*__proto__*1*80*\n"
                      "LAYER*2*<b>synthetic</b>*1*90*\nEOF*\n", encoding="utf-8")
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine, "Theme")
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")))
    window = component.createWithInitialProperties({"controller": controller})
    assert isinstance(window, QQuickWindow), "\n".join(err.toString() for err in component.errors())
    try:
        window.show()
        controller.requestOpenFile(QUrl.fromLocalFile(str(source)))
        QTest.qWait(50)
        dialog = window.findChild(QObject, "tfcalcImportDialog")
        assert dialog is not None
        evaluate(engine, dialog, 'setMapping("__proto__", "synthetic_film", "high"); setMapping("<b>synthetic</b>", "synthetic_film", "low")')
        encoded = evaluate(engine, dialog, "JSON.stringify(parameters().mapping)")
        assert set(json.loads(encoded)) == {"__proto__", "<b>synthetic</b>"}
        for alias in ("__proto__", "<b>synthetic</b>"):
            label = visual_find(window, "tfcalcAlias_" + alias)
            assert label is not None and label.property("text") == alias
            assert evaluate(engine, label, "Number(textFormat)") == Qt.TextFormat.PlainText.value
        preserve = dialog.findChild(QObject, "tfcalcPreserveEnvironment")
        assert preserve is not None
        preserve.setProperty("checked", True)
        evaluate(engine, dialog, "controller.previewTfcalcImport(parameters())")
        assert controller.tfcalcImportState["ready"], controller.tfcalcImportState
    finally:
        window.setProperty("allowClose", True)
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
