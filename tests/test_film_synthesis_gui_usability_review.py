"""Independent native-Qt usability regressions for the GUI repair stage.

Private 1100x760 windows, synthetic materials and real local jobs only.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QSettings, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture as optical_fixture
from tests.test_film_optimization_needle import _optical
from tests.test_film_synthesis_gui_review import _Scene, _variant, _wait, _expected_with_synthesis_search_progress

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def usable_gui(tmp_path):
    _service, library, _design = optical_fixture(tmp_path)
    low = library.read("acceptance-film")["record"]
    low["optical_constants"].append(_optical("film-alt", 1.40))
    library.write(low, update=True)
    high = default_material_profile("Review high material", id="review-high")
    high.update(category="film", optical_constants=[
        _optical("high-main", 2.10), _optical("high-alt", 2.30)])
    library.write(high)
    service = FilmDesignService(tmp_path)
    source = FilmDesignQuickController(service=service,
        geometry_settings=QSettings(str(tmp_path / "usability.ini"), QSettings.Format.IniFormat))
    assert source.initializationReady
    assert source.setSubstrateValue("material_id", "acceptance-glass")
    assert source.setSubstrateValue("optical_set_id", "glass-set")
    assert source.setSubstrateValue("sides", "single")
    controllers: list[FilmOptimizationQuickController] = []
    scenes: list[_Scene] = []
    yield service, source, controllers, scenes
    for controller in controllers:
        controller.cancelOptimization()
        controller.shutdown(5000)
    source.markSourceClosed()
    for scene in reversed(scenes):
        scene.close()
    for controller in controllers:
        controller.deleteLater()
    source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    client = SchedulerClient(tmp_path)
    if client.endpoint.exists():
        client.stop()


def _open_optimizer(gui):
    service, source, controllers, scenes = gui
    controller = FilmOptimizationQuickController(source)
    controllers.append(controller)
    _wait(lambda: not controller._initial_loading)
    scene = _Scene(controller, "FilmOptimizationWindow.qml")
    scenes.append(scene)
    scene.click(scene.item("filmOptimizationAlgorithmTab"))
    scene.click(scene.item("filmOptimizationSearchModeBox"))
    QTest.keyClick(scene.window, Qt.Key.Key_Home)
    modes = cast(list[dict[str, Any]], controller.property("searchModeOptions"))
    for _ in range(next(i for i, row in enumerate(modes) if row["id"] == "synthesis")):
        QTest.keyClick(scene.window, Qt.Key.Key_Down)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    _wait(lambda: controller.property("searchMode") == "synthesis")
    # Select a real material for the initial baseline; the repaired row UI is
    # exercised separately below, without changing angle-entry assertions.
    options = cast(list[dict[str, Any]], controller.property("structureMaterialOptions"))
    for row in options:
        assert controller.setStructureMaterialSelected(row["material_id"], row["optical_set_id"],
            row["material_id"] == "acceptance-film" and row["optical_set_id"] == "film-set")
    for name, value in (("max_cycles", "1"), ("max_evaluations", "12"),
                        ("max_layer_thickness_nm", "200")):
        scene.edit(scene.item("filmSynthesis_" + name + "Field"), value)
    scene.click(scene.item("filmOptimizationTargetsTab"))
    for name, value in (("WavelengthMin", "550"), ("WavelengthMax", "550"),
                        ("LowerBound", "0"), ("UpperBound", "5"), ("Tolerance", "0")):
        scene.edit(scene.item("filmOptimization" + name + "Field"), value)
    _wait(lambda: cast(QObject, controller.property("computeTarget")).property("canSubmit"))
    return service, source, controller, scene


def _type_number(scene, name, text, commit):
    field = scene.item(name)
    scene.click(field)
    QTest.keyClick(scene.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    typed = ""
    if not text:
        QTest.keyClick(scene.window, Qt.Key.Key_Backspace)
        assert str(field.property("text")) == ""
    for character in text:
        QTest.keyClick(scene.window, Qt.Key(ord(character.upper())))
        typed += character
        QTest.qWait(15)
        assert str(field.property("text")) == typed
    if commit == "enter":
        QTest.keyClick(scene.window, Qt.Key.Key_Return)
    elif commit == "focus":
        scene.click(scene.item("filmOptimizationAngleStepField"))
    QTest.qWait(30)
    return field


@pytest.mark.parametrize("angle", ["0", "12.5"])
@pytest.mark.parametrize("commit", ["enter", "focus"])
def test_equal_angle_typed_naturally_survives_each_key_and_submission(usable_gui, angle, commit):
    service, source, controller, scene = _open_optimizer(usable_gui)
    # Lower first is a normal edit order. An intermediate reversed range must
    # remain editable so entering the upper endpoint can repair the pair.
    _type_number(scene, "filmOptimizationAngleMinField", angle, commit)
    _type_number(scene, "filmOptimizationAngleMaxField", angle, commit)
    row = cast(dict[str, Any], controller.property("selectedObjective"))
    assert row["angle_min_deg"] == float(angle)
    assert row["angle_max_deg"] == float(angle)
    assert row["angle_sample_count"] == 1
    assert controller.property("canStart"), controller.property("error")
    original = source.session.snapshot()
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.property("queueJobId")))
    job_id = str(controller.property("queueJobId"))
    _wait(lambda: not controller.property("busy"), seconds=30.)
    assert controller._synthesis_payload["best_candidate_audit"]["available"]
    assert controller._synthesis_payload["best_candidate_audit"]["targets_met"]
    assert not controller._synthesis_payload["best_candidate_audit"]["continuous_certificate"]
    spec = json.loads((service.root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json").read_text(encoding="utf-8"))["parameters"]["payload"]["request"]["request"]
    assert spec["objectives"][0]["angles_deg"] == [float(angle)]
    assert spec["candidate_materials"] == [{"material_id": "acceptance-film", "optical_set_id": "film-set"}]
    applied = source.session.snapshot()
    expected = deepcopy(controller._synthesis_payload["result"]["candidate_design"])
    expected["workflow_settings"] = deepcopy(applied.design["workflow_settings"])
    assert applied.design == expected and controller._candidate_applied
    assert applied.design_revision == original.design_revision + 1
    assert source.undoOptimizedDesign()
    assert source.currentDesign() == _expected_with_synthesis_search_progress(original.design, controller)


def _assert_horizontal_click_target(scene: _Scene, item: QQuickItem) -> None:
    # A point inside the window can still be outside its clipped left pane.
    # Inspect rendered coordinates rather than the layout's requested widths.
    QCoreApplication.processEvents()
    QTest.qWait(20)
    assert item.width() > 2. and item.height() > 0., item.objectName()
    for x in (1., item.width() / 2., item.width() - 1.):
        point = QPointF(x, item.height() / 2.)
        screen = item.mapToScene(point)
        assert 0. <= screen.x() < scene.window.width(), (item.objectName(), screen.x())
        ancestor = item.parentItem()
        while ancestor is not None:
            if ancestor.clip():
                position = item.mapToItem(ancestor, point)
                assert 0. <= position.x() < ancestor.width(), (
                    item.objectName(), "clipped horizontal click target", position.x(), ancestor.width())
            ancestor = ancestor.parentItem()


def _click_material_control(scene, name):
    item = scene.item(name)
    _assert_horizontal_click_target(scene, item)
    scene.click(item)


def _choose_combo(scene, name, selected_id):
    item = scene.item(name)
    model = _variant(item.property("model"))
    index = next(i for i, row in enumerate(model) if row["id"] == selected_id)
    if name.startswith("filmSynthesis"):
        _assert_horizontal_click_target(scene, item)
    scene.click(item)
    assert scene.evaluate(scene.item(name), "popup.visible") is True
    QTest.keyClick(scene.window, Qt.Key.Key_Home)
    for _ in range(index):
        QTest.keyClick(scene.window, Qt.Key.Key_Down)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    QTest.qWait(25)
    item = scene.item(name)
    selected_model = _variant(item.property("model"))
    assert selected_model[int(item.property("currentIndex"))]["id"] == selected_id


def _open_physical(gui, mechanism):
    _service, source, _controllers, scenes = gui
    scene = _Scene(source, "FilmDesignQuick.qml")
    scenes.append(scene)
    scene.click(scene.item("filmDesignSidebarGenerator"))
    scene.click(scene.item("filmDesignGeneratorMode"))
    QTest.keyClick(scene.window, Qt.Key.Key_End)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    _wait(lambda: scene.item("filmDesignGeneratorPanel").property("generatorMode") == "physical")
    _choose_combo(scene, "filmPhysicalMechanism", mechanism)
    form = scene.item("filmPhysicalGeneratorForm")
    # Search and select through the actual material picker input. Only two
    # distinct film materials are bound; their optical-set choices differ.
    scene.edit(scene.item("filmPhysicalMaterial_H_input"), "Review high material")
    scene.edit(scene.item("filmPhysicalMaterial_L_input"), "Synthetic acceptance-film")
    _choose_combo(scene, "filmPhysicalDataset_H", "high-alt")
    _choose_combo(scene, "filmPhysicalDataset_L", "film-alt")
    bindings = _variant(form.property("bindings"))
    assert {row["role"] for row in bindings} == {"H", "L"}
    assert {row["material_id"] for row in bindings} == {"review-high", "acceptance-film"}
    return source, scene, form


@pytest.mark.parametrize("mechanism", ["multistack", "coupled_cavity"])
def test_two_actual_film_materials_with_linked_datasets_generate_preview_apply_and_undo(usable_gui, mechanism):
    source, scene, form = _open_physical(usable_gui, mechanism)
    before = source.session.snapshot()
    scene.click(scene.item("filmPhysicalPreview"))
    preview = _variant(form.property("previewData"))
    assert preview.get("ok"), form.property("errorText")
    rows = preview["layers_substrate_order"]
    assert rows and {row["material_id"] for row in rows} == {"review-high", "acceptance-film"}
    for row in rows:
        expected_set = "high-alt" if row["material_id"] == "review-high" else "film-alt"
        assert row["optical_set_id"] == expected_set
        index = 2.30 if expected_set == "high-alt" else 1.40
        assert row["n_at_wavelength"] == pytest.approx(index)
        assert row["physical_thickness_nm"] > 0
        if mechanism == "multistack":
            assert row["physical_thickness_nm"] == pytest.approx(550. / (4. * index))
    if mechanism == "coupled_cavity":
        cavities = [row for row in rows if ".cavities[" in row["section"]]
        assert cavities and all(row["material_id"] == "review-high" for row in cavities)
        assert all(row["optical_set_id"] == "high-alt" for row in cavities)
        for cavity in cavities:
            assert cavity["physical_thickness_nm"] == pytest.approx(550. / (2. * 2.30))
    assert source.session.snapshot().design == before.design
    scene.click(scene.item("filmPhysicalApply"))
    dialog = scene.window.findChild(QObject, "filmPhysicalApplyConfirmation")
    assert dialog is not None
    scene.accept_dialog(dialog)
    _wait(lambda: source.session.snapshot().design_revision == before.design_revision + 1)
    layers = source.currentDesign()["front_layers"]
    assert [(row["material_id"], row["optical_set_id"]) for row in layers] == [
        (row["material_id"], row["optical_set_id"]) for row in rows]
    undo = next(item for item in scene.item("filmDesignGeneratorPanel").findChildren(QQuickItem)
                if str(item.property("text")) == "撤销应用")
    scene.click(undo)
    _wait(lambda: source.currentDesign()["front_layers"] == [])
    assert source.session.snapshot().design == before.design


def _material_rows(controller):
    return cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))


def _set_material_row(scene, row_id, material_id, set_id):
    _choose_combo(scene, "filmSynthesisMaterial_" + row_id, material_id)
    _choose_combo(scene, "filmSynthesisOpticalSet_" + row_id, set_id)


def test_dynamic_material_rows_delete_middle_reorder_persist_and_submit_exact_optical_sets(usable_gui, tmp_path):
    service, source, controller, scene = _open_optimizer(usable_gui)
    scene.click(scene.item("filmOptimizationAlgorithmTab"))
    for row in list(_material_rows(controller)):
        _click_material_control(scene, "filmSynthesisRemove_" + row["id"])
    assert _material_rows(controller) == []
    ids = []
    for _ in range(3):
        before = {row["id"] for row in _material_rows(controller)}
        _click_material_control(scene, "filmSynthesisAddMaterialButton")
        added = [row["id"] for row in _material_rows(controller) if row["id"] not in before]
        assert len(added) == 1
        ids.append(added[0])
    assert len(set(ids)) == 3
    for row_id in ids:
        for prefix in ("filmSynthesisRemove_", "filmSynthesisUp_", "filmSynthesisDown_"):
            _assert_horizontal_click_target(scene, scene.item(prefix + row_id))
    _set_material_row(scene, ids[0], "acceptance-film", "film-set")
    _set_material_row(scene, ids[1], "review-high", "high-alt")
    _set_material_row(scene, ids[2], "acceptance-film", "film-alt")
    assert [(row["id"], row["material_id"], row["optical_set_id"]) for row in _material_rows(controller)] == [
        (ids[0], "acceptance-film", "film-set"), (ids[1], "review-high", "high-alt"),
        (ids[2], "acceptance-film", "film-alt")]
    library = MaterialLibrary(service.root)
    changed_high = library.read("review-high")["record"]
    changed_high["optical_constants"].append(_optical("high-new", 2.50))
    library.write(changed_high, update=True)
    # Opening the real material combo refreshes the current catalog; a newly
    # available set on that same material must be selectable without a restart.
    _choose_combo(scene, "filmSynthesisMaterial_" + ids[1], "review-high")
    _choose_combo(scene, "filmSynthesisOpticalSet_" + ids[1], "high-new")
    assert _material_rows(controller)[1]["optical_set_id"] == "high-new"
    material_name = "filmSynthesisMaterial_" + ids[1]
    _click_material_control(scene, material_name)
    assert scene.evaluate(scene.item(material_name), "popup.visible") is True
    _click_material_control(scene, material_name)
    assert scene.evaluate(scene.item(material_name), "popup.visible") is False
    _choose_combo(scene, "filmSynthesisOpticalSet_" + ids[1], "high-alt")
    _click_material_control(scene, "filmSynthesisRemove_" + ids[1])
    assert [row["id"] for row in _material_rows(controller)] == [ids[0], ids[2]]
    _choose_combo(scene, "filmSynthesisMaterial_" + ids[2], "review-high")
    remaining = _material_rows(controller)
    assert remaining[0]["optical_set_id"] == "film-set"
    assert remaining[1]["optical_set_id"] in {"high-main", "high-alt"}
    assert remaining[1]["optical_set_id"] != "film-alt"
    _choose_combo(scene, "filmSynthesisOpticalSet_" + ids[2], "high-alt")
    _click_material_control(scene, "filmSynthesisUp_" + ids[2])
    expected = [{"id": ids[2], "material_id": "review-high", "optical_set_id": "high-alt"},
                {"id": ids[0], "material_id": "acceptance-film", "optical_set_id": "film-set"}]
    actual = [{key: row[key] for key in expected[0]} for row in _material_rows(controller)]
    assert actual == expected
    for name, value in (("max_cycles", "2"), ("max_evaluations", "100")):
        scene.edit(scene.item("filmSynthesis_" + name + "Field"), value)
    scene.click(scene.item("filmOptimizationTargetsTab"))
    _type_number(scene, "filmOptimizationAngleMinField", "12.5", "enter")
    _type_number(scene, "filmOptimizationAngleMaxField", "12.5", "focus")
    scene.edit(scene.item("filmOptimizationUpperBoundField"), "0.1")
    configuration = controller.exportOptimizationConfiguration()
    assert configuration["synthesis"]["material_rows"] == expected
    path = tmp_path / "ordered-materials.cfilm"
    assert source.saveFilePath(str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved  # The next controller restores from this actual saved design.
    controller.shutdown(5000)
    reopened = FilmDesignQuickController(service=service,
        geometry_settings=QSettings(str(tmp_path / "reopened.ini"), QSettings.Format.IniFormat))
    try:
        assert reopened.session is not None
        assert reopened.session.request_open_file(str(path))
        assert reopened.currentDesign()["front_layers"] == []
        restored = FilmOptimizationQuickController(reopened)
        usable_gui[2].append(restored)
        _wait(lambda: not restored._initial_loading)
        restored_scene = _Scene(restored, "FilmOptimizationWindow.qml")
        usable_gui[3].append(restored_scene)
        assert restored.exportOptimizationConfiguration() == configuration
        assert [{key: row[key] for key in expected[0]} for row in _material_rows(restored)] == expected
        _wait(lambda: cast(QObject, restored.property("computeTarget")).property("canSubmit"))
        assert restored.property("canStart"), restored.property("error")
        before_design = deepcopy(reopened.currentDesign())
        before_revision = reopened.session.snapshot().design_revision
        restored_scene.click(restored_scene.item("filmOptimizationStartButton"))
        _wait(lambda: bool(restored.property("queueJobId")))
        job_id = str(restored.property("queueJobId"))
        _wait(lambda: not restored.property("busy"), seconds=30.)
        payload = restored._synthesis_payload
        assert payload is not None, restored.property("error")
        assert payload["result"]["state"] == "completed", payload
        assert payload["best_candidate_audit"]["targets_met"] is True
        assert payload["best_candidate_audit"]["continuous_certificate"] is False
        spec_path = service.root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))["parameters"]["payload"]["request"]["request"]
        assert spec["objectives"][0]["angles_deg"] == [12.5]
        assert spec["candidate_materials"] == [
            {"material_id": row["material_id"], "optical_set_id": row["optical_set_id"]} for row in expected]
        materials = {row["id"]: row for row in spec["frozen_material_snapshot"]["materials"]}
        high = next(row for row in materials["review-high"]["optical_sets"] if row["id"] == "high-alt")
        low = next(row for row in materials["acceptance-film"]["optical_sets"] if row["id"] == "film-set")
        assert high["n"] == [2.30, 2.30] and low["n"] == [1.23, 1.23]
        applied = reopened.session.snapshot()
        candidate = deepcopy(payload["result"]["candidate_design"])
        candidate["workflow_settings"] = deepcopy(applied.design["workflow_settings"])
        assert applied.design == candidate and restored._candidate_applied
        assert applied.design_revision == before_revision + 1
        variables = cast(list[dict[str, object]], restored.property("variables"))
        assert {row["layer_id"] for row in variables} == {
            row["id"] for row in applied.design["front_layers"]}
        assert all(row["enabled"] for row in variables)
        assert reopened.undoOptimizedDesign()
        assert reopened.currentDesign() == _expected_with_synthesis_search_progress(before_design, restored)
    finally:
        reopened.markSourceClosed()
        reopened.deleteLater()


@pytest.fixture(params=(0.75, 1.0), ids=("9pt", "12pt"))
def angle_font_scale(request):
    theme = ThemeService.instance()
    before = float(theme.current_snapshot["fontScale"])
    theme.configure(font_scale=request.param)
    try:
        yield
    finally:
        theme.configure(font_scale=before)


def test_invalid_angle_drafts_block_mouse_and_shortcut_until_active_equal_correction_is_committed(
        usable_gui, tmp_path, angle_font_scale):
    service, source, controller, scene = _open_optimizer(usable_gui)
    job_root = service.root / "artifacts/runtime/scheduler/jobs"
    original_specs = set(job_root.glob("*/spec.json"))

    def assert_rejected():
        message = str(controller.property("error"))
        assert message
        labels = [item for item in scene.window.findChildren(QQuickItem)
                  if item.isVisible() and str(item.property("text")) == message]
        assert labels, message
        scene.click(labels[0])  # Reveal the actual error inside the 1100x760 viewport.
        assert not controller.property("canStart")
        start = scene.item("filmOptimizationStartButton")
        point = start.mapToScene(QPointF(start.width() / 2., start.height() / 2.)).toPoint()
        QTest.mouseClick(scene.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.keyClick(scene.window, Qt.Key.Key_F6)
        QTest.qWait(100)
        assert not controller.property("busy")
        assert not controller.property("queueJobId")
        assert set(job_root.glob("*/spec.json")) == original_specs

    for draft, commit in (("1e", "enter"), ("", "focus")):
        field = _type_number(scene, "filmOptimizationAngleMinField", draft, commit)
        assert str(field.property("text")) == draft
        assert_rejected()
        scroll = scene.item("filmOptimizationObjectiveEditorScroll")
        assert scroll.property("contentWidth") == scroll.property("availableWidth")
        assert str(scene.item("filmOptimizationAngleMinField").property("text")) == draft
    _type_number(scene, "filmOptimizationAngleMinField", "20", "focus")
    _type_number(scene, "filmOptimizationAngleMaxField", "10", "enter")
    assert str(scene.item("filmOptimizationAngleMinField").property("text")) == "20"
    assert str(scene.item("filmOptimizationAngleMaxField").property("text")) == "10"
    assert_rejected()
    # Leave the correcting text focused and dirty: F6 must commit it before
    # reading the request or persisting configuration, instead of using 0/0.
    field = _type_number(scene, "filmOptimizationAngleMinField", "10", "none")
    assert field.property("activeFocus")
    pending_configuration = controller.exportOptimizationConfiguration()
    pending_objective = pending_configuration["objectives"][0]
    assert pending_objective["angle_min_deg"] == pending_objective["angle_max_deg"] == 10.
    path = tmp_path / "committed-angle.cfilm"
    assert source.saveFilePath(str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved_objective = saved["workflow_settings"]["optimization"]["objectives"][0]
    assert saved_objective["angle_min_deg"] == saved_objective["angle_max_deg"] == 10.
    QTest.keyClick(scene.window, Qt.Key.Key_F6)
    _wait(lambda: bool(controller.property("queueJobId")))
    job_id = str(controller.property("queueJobId"))
    _wait(lambda: not controller.property("busy"), seconds=30.)
    payload = controller._synthesis_payload
    assert payload is not None and payload["best_candidate_audit"]["targets_met"] is True
    spec = json.loads((job_root / job_id / "spec.json").read_text(encoding="utf-8"))["parameters"]["payload"]["request"]["request"]
    assert spec["objectives"][0]["angles_deg"] == [10.]
    assert source.currentDesign()["front_layers"] == []
