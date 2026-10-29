"""Temporary all-layer analysis remains isolated until explicit Apply."""
from __future__ import annotations

from copy import deepcopy
import time

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, QSettings
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.materials import default_material_profile
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_needle import _optical

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def _controller(tmp_path, *, two_sides=True, parent=None):
    service, library, design = fixture(tmp_path)
    alternate = default_material_profile("Synthetic replacement film", id="replacement-film")
    alternate.update(category="film", optical_constants=[_optical("replacement-set", 1.71)])
    library.write(alternate)
    service = FilmDesignService(tmp_path)
    design["front_layers"][0]["thickness"]["value"] = 100.123456
    if two_sides:
        design["substrate"]["sides"] = "double"
        design["back_layers"] = [{
            "id": "back-stable-layer", "material_id": "acceptance-film",
            "optical_set_id": "film-set", "thickness": {"value": 80.654321, "unit": "nm"},
            "interactive_group_id": 7,
        }]
        design["front_layers"][0]["interactive_group_id"] = 7
    path = tmp_path / "interactive-source.cfilm"
    service.save(path, design)
    source = FilmDesignQuickController(root=tmp_path, service=service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat),
        parent=parent)
    assert source.session is not None
    assert source.session.request_open_file(str(path))
    controller = source.interactiveAnalysisController
    return service, source, controller


def _close(source, controller):
    controller.clearInteractiveAnalysis()
    controller.shutdown()
    shiboken6.delete(source)
    QCoreApplication.processEvents()


def _wait_preview(controller, *, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if (not controller.interactiveAnalysisBusy
                and controller._interactive_last_valid_generation
                == controller._interactive_preview_generation):
            return
        QTest.qWait(10)
    assert not controller.interactiveAnalysisBusy
    assert controller._interactive_last_valid_generation == controller._interactive_preview_generation
    assert controller.interactiveAnalysisError == ""


def test_all_layer_preview_digit_reset_and_cancel_preserve_exact_source(app, tmp_path):
    _service, source, controller = _controller(tmp_path)
    try:
        before = source.session.snapshot()
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        rows = controller.interactiveAnalysisRows
        assert [(row["side"], row["layer_id"]) for row in rows] == [
            ("front", "existing-stable-layer"), ("back", "back-stable-layer")]
        assert all(row["active"] and row["editable"] for row in rows)
        assert rows[0]["original_text"] == "100.123"
        assert rows[1]["original_text"] == "80.654"
        assert controller._interactive_base_physical_nm[("front", "existing-stable-layer")] == pytest.approx(100.123456)
        assert source.session.snapshot().design_revision == before.design_revision
        assert source.session.snapshot().design == before.design
        original_curve = [row["R"] for row in controller.interactiveAnalysisSpectrum]
        simulation = FilmSimulationQuickController(
            root=tmp_path, service=_service, design=deepcopy(before.design), source_controller=source)
        assert simulation._interactive_preview_active
        assert simulation.hasResult and not simulation.canExport
        assert "临时交互分析预览" in simulation._summary

        # A positional tenth-nanometre increment uses the editable display
        # value only for this changed row; opening alone keeps source precision.
        assert controller.adjustInteractiveDigit("front", "existing-stable-layer", -1, 1)
        _wait_preview(controller)
        assert controller._interactive_values[("front", "existing-stable-layer")] == pytest.approx(100.223)
        assert not controller.setInteractiveAnalysisMode("optical_qw")
        assert controller.interactiveAnalysisMode == "physical_nm"
        changed_curve = [row["R"] for row in controller.interactiveAnalysisSpectrum]
        assert changed_curve != original_curve
        assert source.session.snapshot().design == before.design

        controller.resetInteractiveAnalysis()
        _wait_preview(controller)
        assert not controller.interactiveAnalysisDirty
        assert controller._interactive_values == {}
        controller.clearInteractiveAnalysis()
        assert not simulation._interactive_preview_active
        assert not simulation.hasResult
        assert source.session.snapshot().design_revision == before.design_revision
        assert source.session.snapshot().design == before.design
    finally:
        if "simulation" in locals():
            shiboken6.delete(simulation)
        _close(source, controller)


def test_batch_zero_and_group_coefficient_are_temporary_then_apply_atomically(app, tmp_path):
    _service, source, controller = _controller(tmp_path)
    try:
        before = source.session.snapshot()
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        assert not controller.applyInteractiveBatch({
            "filter": {"side": "", "material_id": "acceptance-film", "group_id": "7"},
            "changes": {"thickness_value": "-1", "group_id": "99999999999999999", "clear_group": False},
        })
        assert controller._interactive_values == {}
        assert controller._interactive_group_ids == controller._interactive_base_group_ids
        assert controller.applyInteractiveBatch({
            "filter": {"side": "", "material_id": "acceptance-film", "group_id": "7"},
            "changes": {"thickness_value": "0", "group_id": "3", "clear_group": False},
        })
        _wait_preview(controller)
        assert set(controller._interactive_values.values()) == {0.0}
        assert set(controller._interactive_group_ids.values()) == {3}
        assert source.session.snapshot().design == before.design
        assert controller.applyInteractiveAnalysis()
        applied = source.session.snapshot()
        assert applied.design_revision == before.design_revision + 1
        for layer in applied.design["front_layers"] + applied.design["back_layers"]:
            assert layer["thickness"]["value"] == 0.0
            assert layer["interactive_group_id"] == 3
        assert source.canUndoOptimizedDesign
        assert source.undoOptimizedDesign()
        assert source.session.snapshot().design["front_layers"] == before.design["front_layers"]
        baseline_after_undo = deepcopy(source.session.snapshot().design)

        # The group multiplier always uses the captured base thickness, not
        # the last preview's modified value.
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        assert controller.setInteractiveAnalysisMode("group_coefficient")
        _wait_preview(controller)
        assert controller.setInteractiveGroupFactor("3", "0.8") is False  # no group 3 after undo
        assert controller.setInteractiveGroupFactor("7", "0.8")
        _wait_preview(controller)
        assert controller.setInteractiveGroupFactor("7", "1.1")
        _wait_preview(controller)
        candidate = controller._interactive_preview_design()
        assert candidate["front_layers"][0]["thickness"]["value"] == pytest.approx(100.123456 * 1.1)
        assert candidate["back_layers"][0]["thickness"]["value"] == pytest.approx(80.654321 * 1.1)
        assert source.session.snapshot().design == baseline_after_undo
        assert controller.applyInteractiveAnalysis()
        applied = source.session.snapshot().design
        assert applied["front_layers"][0]["thickness"]["value"] == pytest.approx(110.1358016)
        assert applied["back_layers"][0]["thickness"]["value"] == pytest.approx(88.7197531)
        assert applied["front_layers"][0]["interactive_group_id"] == 7
    finally:
        _close(source, controller)


def test_qw_and_angle_modes_commit_only_their_requested_state(app, tmp_path):
    _service, source, controller = _controller(tmp_path, two_sides=False)
    try:
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        assert controller.setInteractiveAnalysisMode("optical_qw")
        _wait_preview(controller)
        row = controller.interactiveAnalysisRows[0]
        assert row["unit"] == "QW" and row["editable"]
        assert controller.setInteractiveDigit("front", "existing-stable-layer", -1, "5")
        _wait_preview(controller)
        assert controller.applyInteractiveAnalysis()
        saved = source.session.snapshot()
        qw_thickness = saved.design["front_layers"][0]["thickness"]["value"]
        assert qw_thickness != 100.123456
        revision = saved.design_revision

        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        assert controller.setInteractiveAnalysisMode("incident_angle")
        _wait_preview(controller)
        assert controller.setInteractiveAngle("32.5")
        _wait_preview(controller)
        before_design = deepcopy(source.session.snapshot().design)
        assert not controller.applyInteractiveBatch({
            "filter": {"side": "front"},
            "changes": {"material_id": "replacement-film",
                        "optical_set_id": "replacement-set", "group_id": "8"},
        })
        assert controller._interactive_preview_design() == before_design
        assert controller.interactiveAnalysisCanApply
        assert source.session.snapshot().design_revision == revision
        assert controller.applyInteractiveAnalysis()
        final = source.session.snapshot()
        assert final.design_revision == revision
        assert final.design["front_layers"][0]["thickness"]["value"] == qw_thickness
        assert final.design["front_layers"][0]["material_id"] == before_design["front_layers"][0]["material_id"]
        assert final.design["front_layers"][0].get("interactive_group_id") == before_design["front_layers"][0].get("interactive_group_id")
        assert final.design["workflow_settings"]["simulation"]["parameters"]["angle_deg"] == 32.5
    finally:
        _close(source, controller)


def test_simulation_settings_change_fences_angle_apply(app, tmp_path):
    _service, source, controller = _controller(tmp_path, two_sides=False)
    try:
        before = source.session.snapshot()
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        assert controller.setInteractiveAnalysisMode("incident_angle")
        _wait_preview(controller)
        assert controller.setInteractiveAngle("32.5")
        _wait_preview(controller)
        before_design = deepcopy(source.session.snapshot().design)
        assert not controller.applyInteractiveBatch({
            "filter": {"side": "front"},
            "changes": {"material_id": "replacement-film",
                        "optical_set_id": "replacement-set", "group_id": "8"},
        })
        assert controller._interactive_preview_design() == before_design
        assert controller.interactiveAnalysisCanApply
        current = source.session.workflow_configuration("simulation")
        parameters = deepcopy(current.get("parameters", {}))
        parameters["angle_deg"] = 25.0
        current["parameters"] = parameters
        assert source.session.set_workflow_configuration("simulation", current)
        assert source.session.snapshot().design_revision == before.design_revision
        assert not controller.interactiveAnalysisCanApply
        assert not controller.applyInteractiveAnalysis()
        assert source.session.workflow_configuration("simulation")["parameters"]["angle_deg"] == 25.0
    finally:
        _close(source, controller)


def test_batch_material_replacement_preserves_unedited_thickness_and_applies(app, tmp_path):
    _service, source, controller = _controller(tmp_path, two_sides=False)
    try:
        before = source.session.snapshot()
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        replacement = next(row for row in controller.interactiveReplacementOptions[1:]
                           if row["material_id"] == "replacement-film")
        assert controller.applyInteractiveBatch({
            "filter": {"side": "front", "material_id": "acceptance-film"},
            "changes": {"material_id": replacement["material_id"],
                        "optical_set_id": replacement["optical_set_id"]},
        })
        _wait_preview(controller)
        preview = controller._interactive_preview_design()
        layer = preview["front_layers"][0]
        assert layer["material_id"] == "replacement-film"
        assert layer["optical_set_id"] == "replacement-set"
        assert layer["thickness"] == before.design["front_layers"][0]["thickness"]
        assert source.session.snapshot().design == before.design
        assert controller.applyInteractiveAnalysis()
        applied = source.session.snapshot().design["front_layers"][0]
        assert applied["material_id"] == "replacement-film"
        assert applied["optical_set_id"] == "replacement-set"
        assert applied["thickness"] == before.design["front_layers"][0]["thickness"]
    finally:
        _close(source, controller)


def test_source_revision_change_fences_temporary_apply(app, tmp_path):
    _service, source, controller = _controller(tmp_path, two_sides=False)
    try:
        assert controller.startInteractiveAnalysis()
        _wait_preview(controller)
        assert controller.adjustInteractiveDigit("front", "existing-stable-layer", 0, 1)
        _wait_preview(controller)
        assert source.session.set_design_value("name", "newer-source")
        newer = deepcopy(source.session.snapshot().design)
        assert controller.interactiveAnalysisCanApply is False
        assert not controller.applyInteractiveAnalysis()
        assert source.session.snapshot().design == newer
    finally:
        _close(source, controller)


def test_analysis_sidebar_controls_are_present_and_cancel_clears_preview(app, tmp_path):
    from tests.test_film_synthesis_gui_review import _Scene

    class Launcher(QObject):
        def __init__(self):
            super().__init__()
            self.requested = None
            self.simulation = None

        def open_or_reuse_film_companion(self, name, source_controller, *, start=False):
            self.requested = (name, start)
            self.simulation = FilmSimulationQuickController(
                root=tmp_path, service=source_controller.service,
                design=source_controller.currentDesign(), source_controller=source_controller)
            return "simulation-window"

    launcher = Launcher()
    _service, source, controller = _controller(tmp_path, parent=launcher)
    scene = _Scene(source, "FilmDesignQuick.qml")
    try:
        scene.click(scene.item("filmDesignSidebarInteractive"))
        _wait_preview(controller)
        assert controller.interactiveAnalysisActive
        before_collapse = deepcopy(source.session.snapshot().design)
        scene.click(scene.item("filmDesignSidebarToggle"))
        assert not controller.interactiveAnalysisActive
        assert source.session.snapshot().design == before_collapse
        scene.click(scene.item("filmDesignSidebarToggle"))
        scene.click(scene.item("filmInteractiveStartButton"))
        _wait_preview(controller)
        assert controller.interactiveAnalysisActive
        assert scene.item("filmInteractiveBatchReplacement")
        assert scene.item("filmInteractiveOpenSimulation")
        scene.click(scene.item("filmInteractiveOpenSimulation"))
        assert launcher.requested == ("film_simulation", False)
        simulation = launcher.simulation
        assert simulation is not None
        assert simulation._interactive_preview_active
        assert not simulation.canExport
        scene.edit(scene.item(
            "filmInteractiveDigit_front_existing-stable-layer_-1"), "2")
        _wait_preview(controller)
        assert controller._interactive_values[("front", "existing-stable-layer")] == pytest.approx(100.223)
        scene.click(scene.item(
            "filmInteractiveDigitUp_front_existing-stable-layer_-2"))
        _wait_preview(controller)
        assert controller._interactive_values[("front", "existing-stable-layer")] == pytest.approx(100.233)
        replacement = next(index for index, row in enumerate(controller.interactiveReplacementOptions)
                           if row["material_id"] == "replacement-film")
        scene.item("filmInteractiveBatchReplacement").setProperty("currentIndex", replacement)
        scene.edit(scene.item("filmInteractiveBatchThickness"), "0")
        scene.edit(scene.item("filmInteractiveBatchGroup"), "3")
        scene.click(scene.item("filmInteractiveBatchApply"))
        _wait_preview(controller)
        assert set(controller._interactive_values.values()) == {0.0}
        assert set(controller._interactive_group_ids.values()) == {3}
        scene.click(scene.item("filmInteractiveAnalysisApply"))
        assert not controller.interactiveAnalysisActive
        applied = source.session.snapshot().design
        assert all(layer["thickness"]["value"] == 0.0
                   and layer["interactive_group_id"] == 3
                   and layer["material_id"] == "replacement-film"
                   and layer["optical_set_id"] == "replacement-set"
                   for layer in applied["front_layers"] + applied["back_layers"])
    finally:
        scene.close()
        if launcher.simulation is not None:
            shiboken6.delete(launcher.simulation)
        _close(source, controller)
