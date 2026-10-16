"""Complete board lists survive editing, snapshots, physics and optimization."""
from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.design_workflow import DesignWorkflowSpec
from coat_sim.core.film_stack import build_film_stack
from coat_sim.core.film_stack_schema import film_stack_spec_data
from coat_sim.core.input_snapshot import (candidate_snapshot, freeze_process,
                                           runtime_from_snapshot)
from coat_sim.core.mask_optimization import optimize_correction_boards
from coat_sim.core.mask_optimization_workflow import prepare_optimization_runtime
from coat_sim.core.schemas import MachineDesign, MaskOptimizationOptions, ProcessStep
from coat_sim.core.simulator import Settings, _blocked, simulate
from coat_sim.ui.mask_optimization import _optimization_masks


def boards(count):
    result = []
    for index, center in enumerate(np.linspace(-8, 8, count)):
        z = 20.0 + 50.0 * index / max(1, count - 1)
        x0, x1 = (float(center) - .35) * z / 100, (float(center) + .35) * z / 100
        result.append({"z_mm": z, "outer": [[x0, -20.], [x1, -20.],
                                                    [x1, 20.], [x0, 20.]], "holes": []})
    return result


def setup_graph(root, count):
    service = CoatService(root)
    source = {"schema_version": 2, "kind": "point", "source_exponent": 1.}
    substrate = {"schema_version": 1, "name": "Disc", "outline": {
        "kind": "circle", "radius_mm": 10.}, "surface": {"kind": "flat"}}
    service.save("source", root / "source.csrc", source)
    service.save("substrate", root / "substrate.csub", substrate)
    for index, board in enumerate(boards(count)):
        service.save("mask", root / f"board-{index}.cmsk", board)
    machine = {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0.,
               "planet_offset_mm": 0., "planet_tilt_deg": 0., "substrate_z_mm": 100.,
               "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 0.,
               "sources": [{"file": "source.csrc", "x_mm": 0., "y_mm": 0., "z_mm": 0.}],
               "masks": [{"file": f"board-{index}.cmsk"} for index in range(count)],
               "monitors": []}
    service.save("machine", root / "machine.cmac", machine)
    recipe = {"schema_version": 7, "name": "All boards", "machine_file": "machine.cmac",
              "substrate_file": "substrate.csub", "steps": [{"source_slot": 1,
                  "target_nm": 1., "rate_nm_s": 1., "target_mode": "mean",
                  "mask_slots": list(range(count))}]}
    service.save("process", root / "process.cproc", recipe)
    return service, machine, recipe, substrate, source


@pytest.mark.parametrize("count", [3, 5, 80])
def test_all_dependencies_survive_save_reopen_and_snapshot(tmp_path, count):
    service, machine, recipe, _substrate, _source = setup_graph(tmp_path, count)
    assert len(MachineDesign.model_validate(machine).masks) == count
    document = service.load("process", tmp_path / "process.cproc")
    assert not document.issues
    assert document.runtime is not None
    assert len(document.runtime["machine"]["masks"]) == count
    snapshot = freeze_process(service, recipe, tmp_path)
    assert len(snapshot["files"]) == count + 4
    for path in tmp_path.glob("*.cmsk"):
        path.unlink()
    rebuilt = runtime_from_snapshot(snapshot)
    assert rebuilt["steps"][0]["mask_slots"] == list(range(count))
    assert [entry["profile"]["z_mm"] for entry in rebuilt["machine"]["masks"]] == [
        board["z_mm"] for board in boards(count)]


def test_large_snapshot_still_rejects_duplicate_missing_unused_files_and_keeps_empty_selection(tmp_path):
    service, _machine, recipe, _substrate, _source = setup_graph(tmp_path, 80)
    recipe["steps"][0]["mask_slots"] = []
    service.save("process", tmp_path / "process.cproc", recipe, overwrite=True)
    assert service.load("process", tmp_path / "process.cproc").data["steps"][0]["mask_slots"] == []
    snapshot = freeze_process(service, recipe, tmp_path)
    assert runtime_from_snapshot(snapshot)["steps"][0]["mask_slots"] == []
    assert len([row for row in snapshot["files"] if row["kind"] == "mask"]) == 80
    duplicate = deepcopy(snapshot)
    duplicate["files"].append(deepcopy(duplicate["files"][-1]))
    with pytest.raises(ValueError, match="重复路径"):
        runtime_from_snapshot(duplicate)
    missing = deepcopy(snapshot)
    missing["files"].pop()
    with pytest.raises(ValueError, match="缺少"):
        runtime_from_snapshot(missing)
    unused = deepcopy(snapshot)
    extra = deepcopy(unused["files"][-1])
    extra["path"] = "designs/unused-board.json"
    unused["files"].append(extra)
    with pytest.raises(ValueError, match="未被工艺引用"):
        runtime_from_snapshot(unused)


@pytest.mark.qt
def test_machine_add_delete_save_and_reopen_beyond_two(tmp_path):
    from PySide6.QtWidgets import QApplication
    from coat_sim.ui.machine_mask import MachineBridge

    app = QApplication.instance() or QApplication([])
    service, _machine, _recipe, _substrate, _source = setup_graph(tmp_path, 5)
    bridge = MachineBridge(service)
    bridge.loadDocument(str(tmp_path / "machine.cmac"))
    for _ in range(3):
        bridge.appendItem("/masks", json.dumps({"file": "board-4.cmsk"}))
    assert len(json.loads(bridge.property("documentJson"))["masks"]) == 8
    assert bridge.canSave, bridge.validationMessage
    bridge.removeItem("/masks", 2)
    bridge.saveDocument()
    assert not bridge.dirty, bridge.status
    reopened = MachineBridge(service)
    reopened.loadDocument(str(tmp_path / "machine.cmac"))
    assert len(json.loads(reopened.property("documentJson"))["masks"]) == 7
    reopened.preparePreview()
    assert len(json.loads(reopened.property("maskPlanesJson"))) == 7
    assert app is not None


@pytest.mark.parametrize("count", [3, 5, 17])
def test_cpu_full_board_union_matches_independent_rectangles_and_order(count):
    layers = boards(count)
    x, y = np.meshgrid(np.linspace(-10, 10, 43), np.linspace(-10, 10, 37))
    expected = np.zeros_like(x, dtype=bool)
    for layer in layers:
        # Independent analytic rectangle intersections, without polygon helpers.
        ix, iy = x * layer["z_mm"] / 100., y * layer["z_mm"] / 100.
        (left, bottom), (right, _), (_, top), _last = layer["outer"]
        expected |= (left <= ix) & (ix <= right) & (bottom <= iy) & (iy <= top)
    np.testing.assert_array_equal(_blocked(np, x, y, layers, 0., 0., 100.), expected)
    np.testing.assert_array_equal(_blocked(np, x, y, list(reversed(layers)), 0., 0., 100.), expected)
    assert np.any(expected != _blocked(np, x, y, layers[:2], 0., 0., 100.))

    settings = Settings(carrier_rpm=0, planet_offset_mm=0, substrate_z_mm=100,
                        substrate_radius_mm=10, source_x_mm=0, grid_size=31, time_steps=1)
    singles = [simulate(settings, layer, backend="CPU") for layer in layers]
    raw_reference = np.minimum.reduce([item["field"] * item["mean_raw"] for item in singles])
    reference = raw_reference / np.nanmean(raw_reference)
    actual = simulate(settings, layers, backend="CPU")
    np.testing.assert_allclose(actual["field"], reference, rtol=1e-14, atol=1e-14, equal_nan=True)
    np.testing.assert_allclose(simulate(settings, tuple(reversed(layers)), backend="CPU")["field"],
                               reference, rtol=1e-14, atol=1e-14, equal_nan=True)
    assert actual["device"] == "CPU"


def test_high_slots_flow_through_workflow_stack_candidate_and_bounds(tmp_path):
    service, _machine, recipe, substrate, source = setup_graph(tmp_path, 5)
    ProcessStep.model_validate(recipe["steps"][0])
    spec = DesignWorkflowSpec.model_validate({"name": "Five boards",
        "sources": [{"design": source}], "substrate": substrate, "masks": boards(5),
        "steps": recipe["steps"]})
    assert len(spec.masks) == 5
    runtime = service.load("process", tmp_path / "process.cproc").runtime
    stack = film_stack_spec_data({"mode": "equal", "name": "Five boards",
        "machine_file": "machine.cmac", "substrate_file": "substrate.csub",
        "sources": [{"source_slot": 1, "alias": "A", "mask_slots": [0, 2, 4]}],
        "sequence": ["A"], "layer_count": 3, "total_thickness_nm": 3.})
    generated = build_film_stack(stack, machine=runtime["machine"])
    assert all(step["mask_slots"] == [0, 2, 4] for step in generated["profile"]["steps"])
    assert [row["slot"] for row in _optimization_masks(runtime)] == list(range(6))

    original = freeze_process(service, recipe, tmp_path)
    steps = deepcopy(original["runtime"]["steps"])
    steps[0]["mask_slots"] = list(range(8))
    candidates = {slot: boards(1)[0] for slot in (4, 5, 6, 7)}
    edited = candidate_snapshot(original, candidates, steps)
    assert len(edited["runtime"]["machine"]["masks"]) == 8
    assert edited["runtime"]["steps"][0]["mask_slots"] == list(range(8))
    assert len(original["runtime"]["machine"]["masks"]) == 5
    for slot in range(4):
        assert edited["runtime"]["machine"]["masks"][slot] == original["runtime"]["machine"]["masks"][slot]


@pytest.mark.parametrize("slots", [[4], [0, 2, 4]])
def test_real_cpu_optimizer_keeps_high_slots_and_every_unselected_board(tmp_path, slots):
    service, _machine, _recipe, _substrate, _source = setup_graph(tmp_path, 5)
    runtime = service.load("process", tmp_path / "process.cproc").runtime
    config = MaskOptimizationOptions.model_validate({"mode": "optimize_loaded",
        "source_slot": 1, "mask_slots": slots, "allow_two_masks": True,
        "mask_bounds": [{"slot": slot, "x_min_mm": -15., "x_max_mm": 15.,
                         "y_min_mm": -25., "y_max_mm": 25.} for slot in slots],
        "backend": "CPU", "fast_cycle": False, "search_grid_size": 21,
        "max_evaluations": 4}).model_dump(exclude_none=True)
    settings = Settings(carrier_rpm=0, grid_size=21, time_steps=2)
    prepared, normalized = prepare_optimization_runtime(runtime, config, settings)
    result = optimize_correction_boards(settings, prepared["profile"], prepared["machine"],
                                       {**config, **normalized})
    assert result.report["mask_slots"] == slots
    assert [entry["slot"] for entry in result.candidate_masks] == slots
    assert result.report["actual_device"] == "CPU"
    assert runtime["machine"] == prepared["machine"]


def test_new_boards_are_contiguous_and_fixed_variable_can_use_any_slot(tmp_path):
    service, _machine, _recipe, _substrate, _source = setup_graph(tmp_path, 5)
    runtime = service.load("process", tmp_path / "process.cproc").runtime
    options = {"mode": "auto_design", "source_slot": 1, "mask_slots": [5, 6, 7],
        "mask_bounds": [{"slot": slot, "z_mm": 45., "x_min_mm": -10., "x_max_mm": 10.,
                        "y_min_mm": -10., "y_max_mm": 10.} for slot in (5, 6, 7)]}
    settings = Settings(grid_size=21, time_steps=2)
    prepared, config = prepare_optimization_runtime(runtime, options, settings)
    assert len(prepared["machine"]["masks"]) == 8
    assert config["new_mask_slots"] == [5, 6, 7]
    assert prepared["profile"]["steps"][0]["mask_slots"] == list(range(8))
    with pytest.raises(ValueError, match="依次安装"):
        prepare_optimization_runtime(runtime, {**options, "mask_slots": [6]}, settings)
    _prepared, fixed = prepare_optimization_runtime(runtime, {"mode": "fixed_plus_variable",
        "variable_mode": "optimize_loaded", "fixed_mask_slots": [4], "mask_slots": [3],
        "source_slot": 1}, settings)
    assert fixed["fixed_mask_slots"] == [4]
    assert fixed["mask_slots"] == [3]
    for value in (-1, True, 2.5):
        with pytest.raises(ValueError):
            prepare_optimization_runtime(runtime, {**options, "mask_slots": [value]}, settings)
    with pytest.raises(ValueError, match="最低点"):
        simulate(replace(settings, substrate_z_mm=100), [{**boards(1)[0], "z_mm": 100}], backend="CPU")


@pytest.mark.qt
def test_qml_selects_fifth_board_and_arbitrary_fixed_variable_roles(tmp_path):
    from pathlib import Path
    from PySide6.QtCore import QCoreApplication, QEvent, QUrl
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtWidgets import QApplication
    from coat_sim.ui.theme_service import ThemeService
    from tests.test_mask_optimization_ui import _items

    app = QApplication.instance() or QApplication([])
    service, _machine, _recipe, _substrate, _source = setup_graph(tmp_path, 5)
    runtime = service.load("process", tmp_path / "process.cproc").runtime
    qml_root = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml"
    engine = QQmlEngine()
    ThemeService.instance().attach_engine(engine)
    component = QQmlComponent(engine)
    source = '''
        import QtQuick
        import QtQuick.Window
        import "." as LocalComponents
        Window {
            id: window
            width: 900; height: 900
            property var themeColors: Theme.snapshot
            property var captured: ({})
            QtObject {
                id: stubOptimizer
                property var optimization: ({busy: false, stage: "", message: "", artifacts: ({})})
                property var optimizationMasks: MASKS
                property int optimizationMaskCount: 5
                property int optimizationSourceCount: 1
                property bool optimizationReady: true
                property var pendingMasks: []
                property string optimizationDefaultSummary: "Five boards"
                property string optimizationAvailability: "Ready"
                property string optimizationDefaultConfigurationId: "test"
                property int optimizationDefaultRevision: 1
                property var optimizationParameters: ({backend: "CPU", settings: {grid_size: 21}})
                function refreshOptimizationDefault() { return true }
                function startOptimization(settings, options) { window.captured = options }
            }
            LocalComponents.MaskOptimizationDialog {
                id: optimizationDialog
                optimizer: stubOptimizer
                hostWindow: window
            }
            Component.onCompleted: optimizationDialog.open()
        }
    '''.replace("MASKS", json.dumps(_optimization_masks(runtime)))
    component.setData(source.encode(), QUrl.fromLocalFile(str(qml_root / "__unbounded_masks_test__.qml")))
    window = component.create()
    assert isinstance(window, QQuickWindow), [error.toString() for error in component.errors()]
    try:
        window.show()
        app.processEvents()

        def item(name):
            result = next((child for child in _items(window) if child.objectName() == name), None)
            assert result is not None, name
            return result

        for slot in range(6):
            item(f"optimizationBoardChoice{slot}")
        item("optimizationBoardChoice2").setProperty("checked", True)
        item("optimizationBoardChoice4").setProperty("checked", True)
        item("startOptimizationButton").clicked.emit()
        captured = window.property("captured").toVariant()
        assert captured["mask_slots"] == [0, 2, 4]
        assert [row["slot"] for row in captured["mask_bounds"]] == [0, 2, 4]
        assert captured["allow_two_masks"] is True

        item("optimizationModeSelector").setProperty("currentIndex", 2)
        item("optimizationFixedBoardSelector").setProperty("currentIndex", 4)
        item("optimizationVariableModeSelector").setProperty("currentIndex", 1)
        app.processEvents()
        variable = item("optimizationVariableBoardSelector")
        assert item("optimizationFixedBoardSelector").property("count") == 5
        assert variable.property("count") == 4
        variable.setProperty("currentIndex", 3)
        assert variable.property("currentValue") == 3
        item("startOptimizationButton").clicked.emit()
        captured = window.property("captured").toVariant()
        assert captured["fixed_mask_slots"] == [4]
        assert captured["mask_slots"] == [3]
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
