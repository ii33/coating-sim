"""Actual material QML entry, async admission, append-only provenance and export."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")

from PySide6.QtCore import QEvent, QMetaObject, QObject, QPoint, QPointF, QUrl, Signal, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
import pytest

from coat_sim.core import optical_constants
from coat_sim.ui import optical_inversion as ui
from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui.source_materials import MaterialLibraryBridge
from coat_sim.ui.theme_service import ThemeService

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.qt
_LIBRARIES: list[Library] = []


def pump(app, predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for optical inversion")
        QTest.qWait(5)
    app.processEvents()


@pytest.fixture
def app() -> Iterator[QGuiApplication]:
    application = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(application, QGuiApplication)
    family = bundled_font_family()
    if family:
        application.setFont(QFont(family))
    yield application
    for library in _LIBRARIES:
        for controller in library.findChildren(ui.OpticalInversionController):
            controller.invalidate()
            if controller._future is not None:
                pump(application, lambda c=controller: c._future is None)
            controller.deleteLater()
        library.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
    _LIBRARIES.clear()


def form():
    return {"model": "cauchy", "transparent_region": True, "absolute_specular": True,
        "film_thickness_nm": "120", "substrate_n": "1.52", "substrate_k": "",
        "substrate_lossless": True, "substrate_thickness_nm": "1000000", "backside": "incoherent",
        "spectra": [{"quantity": "R", "wavelength_unit": "nm", "value_unit": "fraction",
                     "angle_deg": "0", "polarization": "unpolarized", "source": "Synthetic fixture, not measurement",
                     "text": "400,0.1\n500,0.15\n600,0.2\n700,0.25"}]}


def result(request):
    waves = [400.0, 500.0, 600.0, 700.0]
    return {"ok": True, "status": "completed", "request": deepcopy(request), "model": "cauchy",
        "parameters": {"A": 1.8, "B": 0.01, "C": 0.0001}, "rmse": 0.001,
        "nk": {"wavelength_nm": waves, "n": [1.9, 1.85, 1.83, 1.82], "k": [0.0] * 4},
        "spectra": [{"quantity": "R", "wavelength_nm": waves, "measured_fraction": [.1, .15, .2, .25],
                     "predicted_fraction": [.101, .149, .201, .249], "residual_fraction": [.001, -.001, .001, -.001], "rmse": .001}],
        "warnings": [{"code": "non_unique", "message": "仅R及有限模型不保证唯一解", "severity": "warning"}],
        "diagnostics": {"bound_hits": [], "multistarts": [{"rmse": .001}], "non_unique": True}}


class Library(QObject):
    changed = Signal()
    draftChanged = Signal()

    def __init__(self):
        super().__init__()
        _LIBRARIES.append(self)
        self._draft, self._load_token = {"id": "synthetic"}, 0
        self.loading, self.readOnly, self.builtin = False, False, False
        self.opticalSetDataRevision, self.status, self.records = 0, "", []
        self.fail = False

    def addOpticalSet(self, payload):
        if self.fail:
            self.status = "Injected material add failure"
            return False
        from coat_sim.ui.source_materials import _optical_record_from_editor
        record = _optical_record_from_editor(json.loads(payload))
        self.records.append(record)
        self.opticalSetDataRevision += 1
        self.changed.emit()
        return True


def ready(app, monkeypatch, *, payload=None):
    library = Library()
    controller = ui.OpticalInversionController(library)
    monkeypatch.setattr(ui, "fit_optical_constants", lambda request, **_kw: payload or result(request))
    controller.beginSession()
    assert controller.startFit(json.dumps(form()))
    pump(app, lambda: controller._future is None)
    assert controller.canApply, controller.status
    return library, controller


def test_explicit_measurement_and_unknown_k_boundaries():
    request = ui.request_from_form(form())
    assert request["substrate"]["k"] is None
    assert request["substrate"]["assume_lossless"] is True
    assert request["spectra"][0]["wavelengths"] == [400, 500, 600, 700]
    for changed in ({"absolute_specular": False}, {"transparent_region": False},
                    {"substrate_lossless": False}, {"substrate_k": "0.1"}, {"backside": ""}):
        with pytest.raises(ValueError):
            ui.request_from_form({**form(), **changed})
    request_form = form()
    request_form.update(backside="semi_infinite")
    request_form["spectra"][0]["quantity"] = "T"
    with pytest.raises(ValueError, match="半无限"):
        ui.request_from_form(request_form)
    request_form["into_substrate_confirmed"] = True
    assert ui.request_from_form(request_form)["spectra"][0]["transmission_definition"] == "into_substrate"


def test_new_dataset_preserves_all_provenance_and_never_overwrites(app, monkeypatch):
    library, controller = ready(app, monkeypatch)
    old = {"id": "existing", "name": "Existing"}
    library.records.append(deepcopy(old))
    assert controller.applyAsNew("Inverse candidate", "limited synthetic fit"), controller.status
    assert library.records[0] == old
    saved = library.records[1]
    assert saved["id"] != "existing"
    assert optical_constants.validate_optical_set(saved)
    assert json.loads(saved["source"])["request"]["spectra"][0]["values"] == [.1, .15, .2, .25]
    assert json.loads(saved["notes"])["spectra"][0]["residual_fraction"] == [.001, -.001, .001, -.001]
    assert not controller.canApply


def test_worker_is_async_cancellation_switch_and_late_result_cannot_apply(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    worker_threads = []

    def fit(request, **_kw):
        worker_threads.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return result(request)

    monkeypatch.setattr(ui, "fit_optical_constants", fit)
    library = Library()
    controller = ui.OpticalInversionController(library)
    controller.beginSession()
    assert controller.startFit(json.dumps(form()))
    pump(app, entered.is_set)
    assert controller.busy
    assert worker_threads[0] != threading.get_ident()
    library._draft = {"id": "different"}
    library.draftChanged.emit()
    assert not controller.canApply
    release.set()
    pump(app, lambda: controller._future is None)
    assert not controller.preview
    assert not controller.applyAsNew("Never add")
    assert library.records == []
    controller.beginSession()
    assert controller.startFit(json.dumps(form()))
    controller.invalidate()
    pump(app, lambda: controller._future is None)
    assert not controller.canApply and not controller.preview


def test_preview_change_and_frozen_project_invalidate_admission(app, monkeypatch):
    library, controller = ready(app, monkeypatch)
    controller.inputsChanged()
    assert not controller.canApply
    assert not controller.applyAsNew("stale")
    library._editing_frozen = True
    controller.beginSession()
    assert not controller.startFit(json.dumps(form()))
    assert library.records == []


def test_report_export_cancel_existing_failure_and_reference_hash(app, monkeypatch, tmp_path):
    request = ui.request_from_form(form())
    request["spectra"][0]["source"] = "x" * 21000
    payload = result(request)
    library, controller = ready(app, monkeypatch, payload=payload)
    assert not controller.applyAsNew("Large candidate")
    assert "20000" in controller.status
    assert not controller.exportReport("")
    assert list(tmp_path.iterdir()) == []
    path = tmp_path / "report.json"
    assert controller.exportReport(QUrl.fromLocalFile(str(path)).toString())
    original = path.read_bytes()
    assert json.loads(original) == payload
    assert not controller.exportReport(QUrl.fromLocalFile(str(path)).toString())
    assert path.read_bytes() == original
    # Realistic overlimit is many samples, not an oversized source summary.
    controller._result["request"]["spectra"][0]["source"] = "Synthetic source"
    controller._result["request"]["spectra"][0]["values"] *= 2000
    second = tmp_path / "large-report.json"
    assert controller.exportReport(QUrl.fromLocalFile(str(second)).toString())
    library.fail = True
    assert not controller.applyAsNew("Large candidate")
    assert second.is_file() and "报告保留" in controller.status
    library.fail = False
    assert controller.applyAsNew("Large candidate"), controller.status
    reference = json.loads(library.records[-1]["source"])["report"]
    assert reference["uri"] == QUrl.fromLocalFile(str(second)).toString()
    assert reference["sha256"] == hashlib.sha256(second.read_bytes()).hexdigest()


def test_changed_export_cannot_be_attached_and_bad_fit_cannot_apply(app, monkeypatch, tmp_path):
    request = ui.request_from_form(form())
    request["spectra"][0]["values"] *= 2000
    library, controller = ready(app, monkeypatch, payload=result(request))
    path = tmp_path / "report.json"
    assert controller.exportReport(QUrl.fromLocalFile(str(path)).toString())
    path.write_text("tampered", encoding="utf-8")
    assert not controller.applyAsNew("Do not apply")
    assert "已改变" in controller.status
    assert library.records == []
    controller.inputsChanged()
    monkeypatch.setattr(ui, "fit_optical_constants", lambda *_a, **_kw: {"ok": False, "status": "failed", "message": "No fit"})
    assert controller.startFit(json.dumps(form()))
    pump(app, lambda: controller._future is None)
    assert not controller.canApply and "No fit" in controller.status


def test_actual_core_worker_recovers_synthetic_rt_and_saves_valid_candidate(app):
    import numpy as np
    from coat_sim.core.film_optics import incoherent_plate_rta
    waves = np.linspace(400, 800, 25)
    n = 1.8 + .01 / (waves / 1000) ** 2 + .0001 / (waves / 1000) ** 4
    prediction = incoherent_plate_rta(waves, 1.0, 1.52, 1.0, n[:, None], [120],
                                     substrate_thickness_nm=1000000)
    data = form()
    data["spectra"] = []
    for quantity in ("R", "T"):
        row = deepcopy(form()["spectra"][0])
        row.update(quantity=quantity, source="Synthetic analytic Cauchy/TMM fixture; not measured",
                   text="\n".join(f"{w:.15g},{v:.15g}" for w, v in zip(waves, prediction[quantity], strict=True)))
        data["spectra"].append(row)
    library = Library()
    controller = ui.OpticalInversionController(library)
    controller.beginSession()
    assert controller.startFit(json.dumps(data))
    pump(app, lambda: controller._future is None, timeout=45)
    assert controller.canApply, controller.status
    assert cast(dict[str, Any], controller.preview)["rmse"] < 1e-5
    assert controller.applyAsNew("Synthetic recovered Cauchy"), controller.status
    assert library.records[0]["k"] == [0.0] * len(waves)


def find(window: QQuickWindow, name: str) -> QQuickItem:
    item = window.findChild(QQuickItem, name)
    if item is not None:
        return item
    todo = [window.contentItem()]
    while todo:
        item = todo.pop()
        if item.objectName() == name:
            return item
        todo.extend(item.childItems())
    raise AssertionError(name)


def test_actual_new_dataset_qml_fit_preview_cancel_and_append(app, monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "fit_optical_constants", lambda request, **_kw: result(request))
    library = MaterialLibraryBridge(directory=tmp_path / "materials")
    pump(app, lambda: not library.loading)
    library.newMaterial()
    pump(app, lambda: not library.loading)
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda messages: warnings.extend(error.toString() for error in messages))
    engine.rootContext().setContextProperty("testLibrary", library)
    ThemeService.instance().attach_engine(engine)
    engine.loadData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow { width: 1080; height: 920; visible: true
    MaterialEditor { id: editor; objectName: "testEditor"; parent: Overlay.overlay; library: testLibrary }
    Component.onCompleted: editor.beginNew()
}''', QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/OpticalInversionTest.qml")))
    assert engine.rootObjects(), warnings
    root = engine.rootObjects()[0]
    assert isinstance(root, QQuickWindow)
    window = root
    try:
        QTest.qWait(150)
        button = find(window, "opticalEditorInversionButton")
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        QTest.qWait(150)
        controller = cast(ui.OpticalInversionController, library.opticalInversion)
        assert controller._session
        for name, text in (("inversionFilmThickness", "120"), ("inversionSubstrateN", "1.52"),
                           ("inversionSubstrateThickness", "1000000"), ("inversionSource_0", "Synthetic test only"),
                           ("inversionTable_0", "400,0.1\n500,0.15\n600,0.2\n700,0.25")):
            find(window, name).setProperty("text", text)
        for name in ("inversionAbsoluteSpecular", "inversionSubstrateLossless", "inversionTransparentRegion"):
            find(window, name).setProperty("checked", True)
        find(window, "inversionBackside").setProperty("currentIndex", 1)
        inversion = window.findChild(QObject, "opticalInversionDialog")
        assert inversion
        payload = inversion.metaObject().indexOfMethod("formPayload()")
        assert payload >= 0
        # Operate the actual QML Fit button, preserving signal wiring and async admission.
        fit_button = find(window, "inversionFitButton")
        scroll = find(window, "opticalInversionScroll").property("contentItem")
        fit_scene = fit_button.mapToScene(QPointF(0, 0))
        scroll.setProperty("contentY", max(0, scroll.property("contentY") + fit_scene.y() - 350))
        QTest.qWait(60)
        point = fit_button.mapToScene(QPointF(fit_button.width() / 2, fit_button.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        pump(app, lambda: controller.canApply)
        assert "不代表唯一" in cast(str, controller.qualityText)
        assert find(window, "inversionNkPlot").isVisible()
        before = library.draftData()
        QMetaObject.invokeMethod(inversion, "close")
        QTest.qWait(80)
        assert library.draftData() == before
        editor = window.findChild(QObject, "testEditor")
        assert editor is not None
        QMetaObject.invokeMethod(editor, "openInversion")
        QTest.qWait(60)
        assert controller.startFit(json.dumps(form()))
        pump(app, lambda: controller.canApply)
        find(window, "inversionOutputName").setProperty("text", "Synthetic inverse candidate")
        preview_dir = os.environ.get("COAT_INVERSION_PREVIEW_DIR")
        if preview_dir:
            directory = Path(preview_dir)
            directory.mkdir(parents=True, exist_ok=True)
            scroll.setProperty("contentY", max(0, float(scroll.property("contentHeight")) - 720))
            QTest.qWait(80)
            assert window.grabWindow().save(str(directory / "optical-inversion-preview.png"))
            window.resize(560, 680)
            QTest.qWait(150)
            scroll.setProperty("contentY", 0)
            QTest.qWait(80)
            assert window.grabWindow().save(str(directory / "optical-inversion-input-narrow.png"))
            scroll.setProperty("contentY", max(0, float(scroll.property("contentHeight")) - 520))
            QTest.qWait(80)
            assert window.grabWindow().save(str(directory / "optical-inversion-preview-narrow.png"))
            quality = find(window, "inversionQuality")
            origin = quality.mapToScene(QPointF(0, 0))
            scroll.setProperty("contentY", max(0, float(scroll.property("contentY")) + origin.y() - 100))
            QTest.qWait(80)
            assert window.grabWindow().save(str(directory / "optical-inversion-quality-narrow.png"))
            window.resize(1080, 920)
            QTest.qWait(80)
        apply_button = find(window, "inversionApplyButton")
        assert apply_button.isEnabled()
        point = apply_button.mapToScene(QPointF(apply_button.width() / 2, apply_button.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        QTest.qWait(100)
        records = library.draftData()["optical_constants"]
        assert len(records) == len(before.get("optical_constants", [])) + 1
        assert records[-1]["name"] == "Synthetic inverse candidate"
        assert not list((tmp_path / "materials").glob("*.json"))
        assert not any("ReferenceError" in warning or "TypeError" in warning for warning in warnings), warnings
    finally:
        library.cancelOpticalInversion()
        library.waitForBackgroundTasks()
        window.close()
        window.deleteLater()
        engine.deleteLater()
        library.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
