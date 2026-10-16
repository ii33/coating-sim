"""Contract tests for bounded current-editor resource edits (bridge doubles)."""
from copy import deepcopy
import json
from typing import Any

import pytest

from coat_sim.control_errors import UiControlError
from coat_sim.ui.project_resource_editor_operations import (
    RESOURCE_EDIT_MODULES,
    apply_resource_editor_operation,
    resource_editor_capabilities,
)


class FakeEditor:
    def __init__(self):
        self._draft: dict[str, Any] = {}
        self._fields = {"name": "source", "kind": "point", "device_type": "custom",
                        "source_exponent": "1", "sampling_order": ""}
        self._emission = {"kind": "uniform", "reference_rate_nm_s": "10"}
        self._apertures = [{"kind": "rectangle", "length_mm": "20"}]
        self._aperture_index = 0
        self._material = None
        self._selected = -1
        self._points = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
        self._data: dict[str, Any] = {
            "name": "editor", "description": "", "notes": "", "z_mm": 300.0,
            "outer": [[0, 0], [10, 0], [10, 10]], "holes": [],
            "sources": [{"file": "keep.source", "x_mm": 0, "y_mm": 0, "z_mm": 0}],
            "monitors": [{"kind": "crystal", "name": "QCM", "x_mm": 0, "y_mm": 0, "z_mm": 0}],
            "steps": [{"source_slot": 1, "target_nm": 100.0, "rate_nm_s": 1.0,
                       "target_mode": "mean", "mask_slots": []}],
        }
        self._dirty = False
        self._revision = 0
        self.status = ""
        self.calls = []
        self._loading = False
        self._operation_busy = False
        self._null_models = set()
        self._source_preview = {"sources": [{"slot": 1, "valid": True, "default_rate_nm_s": 1.0}]}
        self.maskCount = 1

    def _edit(self):
        self._dirty = True
        self._revision += 1

    def setField(self, key, value):
        self.calls.append(("setField", key, value))
        if key in self._draft:
            self._draft[key] = value
        else:
            self._fields[key] = value
        self._edit()

    def setEmissionField(self, key, value):
        self.calls.append(("setEmissionField", key, value))
        self._emission[key] = value
        self._edit()

    def setApertureField(self, key, value):
        self.calls.append(("setApertureField", key, value))
        self._apertures[self._aperture_index][key] = value
        self._edit()

    def addAperture(self):
        self.calls.append(("addAperture",))
        self._apertures.append(dict(self._apertures[self._aperture_index]))
        self._aperture_index = len(self._apertures) - 1
        self._edit()

    def removeAperture(self):
        self.calls.append(("removeAperture",))
        self._apertures.pop(self._aperture_index)
        self._aperture_index = min(self._aperture_index, len(self._apertures) - 1)
        self._edit()

    def _captured_material_model(self, kind):
        self.calls.append(("captured", kind))
        raise ValueError("missing captured model")

    def updateSelectedPoint(self, x, y):
        self.calls.append(("updateSelectedPoint", x, y))
        self._points[self._selected] = [float(x), float(y)]
        self._edit()
        return True

    def addPoint(self, x, y):
        self.calls.append(("addPoint", x, y))
        index = self._selected + 1 if self._selected >= 0 else len(self._points)
        self._points.insert(index, [float(x), float(y)])
        self._selected = index
        self._edit()
        return True

    def deleteSelectedPoint(self):
        self.calls.append(("deleteSelectedPoint",))
        self._points.pop(self._selected)
        self._selected = min(self._selected, len(self._points) - 1)
        self._edit()

    def setValue(self, pointer, raw):
        self.calls.append(("setValue", pointer, raw))
        value = json.loads(raw)
        updated = deepcopy(self._data)
        if pointer.startswith("/sources/") or pointer.startswith("/monitors/"):
            _, collection, index, field = pointer.split("/")
            updated[collection][int(index)][field] = value
        else:
            updated[pointer[1:]] = value
        self._data = updated
        self._edit()

    def setMetadata(self, key, value):
        self.calls.append(("setMetadata", key, value))
        self._data[key] = value
        self._edit()

    def appendItem(self, pointer, raw):
        self.calls.append(("appendItem", pointer, raw))
        self._data[pointer[1:]].append(json.loads(raw))
        self._edit()

    def removeItem(self, pointer, index):
        self.calls.append(("removeItem", pointer, index))
        self._data[pointer[1:]].pop(index)
        self._edit()

    def rotatePolygon(self, pointer, cx, cy, angle):
        self.calls.append(("rotatePolygon", pointer, cx, cy, angle))
        self._data = deepcopy(self._data)
        self._edit()

    def transformMask(self, dx, dy, sx, sy):
        self.calls.append(("transformMask", dx, dy, sx, sy))
        self._data = deepcopy(self._data)
        self._edit()

    def setStep(self, index, field, value):
        self.calls.append(("setStep", index, field, value))
        self._data["steps"][index][field] = value
        self._edit()

    def chooseStepSource(self, index, slot):
        self.calls.append(("chooseStepSource", index, slot))
        self._data["steps"][index]["source_slot"] = slot
        self._edit()

    def setStepMask(self, index, slot, enabled):
        self.calls.append(("setStepMask", index, slot, enabled))
        self._edit()

    def addStep(self):
        self.calls.append(("addStep",))
        self._data["steps"].append({"source_slot": 1, "target_nm": 100.0, "rate_nm_s": 1.0,
                                    "target_mode": "mean", "mask_slots": []})
        self._edit()

    def deleteStep(self, index):
        self.calls.append(("deleteStep", index))
        self._data["steps"].pop(index)
        self._edit()

    def moveStep(self, index, delta):
        self.calls.append(("moveStep", index, delta))
        self._edit()

    def applyEnvironment(self, index, record):
        self.calls.append(("applyEnvironment", index, deepcopy(record)))
        return {"ok": True, "message": ""}


def _run(module, editor, operation, **kwargs):
    apply_resource_editor_operation(module, editor, operation, **kwargs)


def test_resource_capabilities_are_narrow_and_publish_gui_units():
    assert RESOURCE_EDIT_MODULES == {"source", "substrate", "machine", "mask", "process", "materials"}
    source = resource_editor_capabilities("source")
    assert "setValue" not in source["actions"]
    assert source["action_arguments"]["set_aperture_field"] == ["field", "index", "value"]
    assert source["action_schemas"]["set_emission_field"]["field"]["type"] == "string"
    assert source["field_schema"]["source_exponent"]["type"] == "string"
    assert source["action_schemas"]["set_emission_field"]
    from coat_sim.ui.project_resource_editor_operations import _source_emission_schema
    assert _source_emission_schema()["reference_rate_nm_s"]["unit"].startswith("Å/s")
    materials = resource_editor_capabilities("materials")
    assert "resistance_source_exponent" in materials["editable_fields"]
    assert "resistance_provenance_status" in materials["editable_fields"]
    assert "edit_dataset" in materials["unsupported_actions"]
    assert "lazy" in " ".join(source["implicit_gui_effects"])
    assert resource_editor_capabilities("process")["action_schemas"]["set_step_field"]["value"]["unitWhenField"]["rate_angstrom_s"] == "Å/s"


def test_update_is_one_field_and_preserves_raw_invalid_scalar_text():
    editor = FakeEditor()
    _run("source", editor, "update_window", fields={"source_exponent": "not a number"})
    assert editor._fields["source_exponent"] == "not a number"
    assert editor._dirty and editor._revision == 1
    assert editor.calls == [("setField", "source_exponent", "not a number")]

    before = (deepcopy(editor._fields), editor._dirty, editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("source", editor, "update_window", fields={"name": "x", "notes": "y"})
    with pytest.raises(UiControlError):
        _run("source", editor, "update_window", fields={"unexpected": "x"})
    assert (editor._fields, editor._dirty, editor._revision, editor.calls) == before


def test_source_kind_material_preflight_and_selected_aperture_fence():
    editor = FakeEditor()
    editor._material = {"id": "captured"}
    before = deepcopy(editor._fields)
    with pytest.raises(UiControlError):
        _run("source", editor, "window_action", action="set_emission_field",
             arguments={"field": "kind", "value": "electron_beam"})
    assert editor._fields == before and editor._emission["kind"] == "uniform"
    assert editor._dirty is False

    editor._material = None
    before_calls = list(editor.calls)
    with pytest.raises(UiControlError):
        _run("source", editor, "window_action", action="set_aperture_field",
             arguments={"index": 1, "field": "length_mm", "value": "bad"})
    assert editor.calls == before_calls and editor._apertures[0]["length_mm"] == "20"
    _run("source", editor, "window_action", action="set_aperture_field",
         arguments={"index": 0, "field": "length_mm", "value": "raw draft"})
    assert editor._apertures[0]["length_mm"] == "raw draft"


def test_substrate_point_updates_require_existing_selection_without_selecting():
    editor = FakeEditor()
    editor._selected = 1
    _run("substrate", editor, "window_action", action="set_point",
         arguments={"index": 1, "x": 2.25, "y": 3.5})
    assert editor._points[1] == [2.25, 3.5]
    before = (deepcopy(editor._points), editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("substrate", editor, "window_action", action="remove_point",
             arguments={"index": 0})
    assert (editor._points, editor._revision, editor.calls) == before
    with pytest.raises(UiControlError):
        _run("substrate", editor, "window_action", action="set_point",
             arguments={"index": 1, "x": True, "y": 3})
    assert (editor._points, editor._revision, editor.calls) == before


def test_machine_operations_use_only_fixed_paths_and_reject_wrong_types():
    editor = FakeEditor()
    _run("machine", editor, "update_window", fields={"carrier_x_mm": 2.5})
    assert editor.calls[-1][:2] == ("setValue", "/carrier_x_mm")
    _run("machine", editor, "window_action", action="set_source_field",
         arguments={"index": 0, "field": "z_mm", "value": 12.0})
    assert editor.calls[-1][1] == "/sources/0/z_mm"
    before = (deepcopy(editor._data), editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("machine", editor, "window_action", action="set_source_field",
             arguments={"index": 0, "field": "file", "value": 4.0})
    with pytest.raises(UiControlError):
        _run("machine", editor, "update_window", fields={"sun_teeth": 12.0})
    assert (editor._data, editor._revision, editor.calls) == before


def test_mask_actions_build_only_bounded_contour_paths_and_preflight_transform():
    editor = FakeEditor()
    points = [[1, 1], [8, 1], [8, 8]]
    _run("mask", editor, "window_action", action="set_contour",
         arguments={"contour": "outer", "index": 0, "points": points})
    assert editor.calls[-1][1] == "/outer"
    _run("mask", editor, "window_action", action="add_hole", arguments={"points": points})
    assert len(editor._data["holes"]) == 1
    before = (deepcopy(editor._data), editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("mask", editor, "window_action", action="set_contour",
             arguments={"contour": "holes", "index": 4, "points": points})
    with pytest.raises(UiControlError):
        _run("mask", editor, "window_action", action="transform_mask",
             arguments={"dx": float("nan"), "dy": 0.0, "sx": 1.0, "sy": 1.0})
    assert (editor._data, editor._revision, editor.calls) == before
    _run("mask", editor, "window_action", action="transform_mask",
         arguments={"dx": 1.0, "dy": 0.0, "sx": 1.0, "sy": 1.0})
    assert editor.calls[-1][0] == "transformMask"


def test_process_action_preflight_blocks_compute_and_accepts_atomic_environment_shape():
    editor = FakeEditor()
    _run("process", editor, "window_action", action="set_step_field",
         arguments={"index": 0, "field": "rate_nm_s", "value": "bad draft"})
    assert editor._data["steps"][0]["rate_nm_s"] == "bad draft"
    before = (deepcopy(editor._data), editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("process", editor, "window_action", action="set_environment",
             arguments={"index": 0, "record": {"unexpected": 5}})
    with pytest.raises(UiControlError):
        _run("process", editor, "window_action", action="start_optimization", arguments={})
    assert (editor._data, editor._revision, editor.calls) == before
    _run("process", editor, "window_action", action="set_environment",
         arguments={"index": -1, "record": {"transport_model": "disabled"}})
    assert editor.calls[-1][0] == "applyEnvironment"


def test_materials_scalar_fields_are_editable_but_nested_dataset_actions_are_not():
    editor = FakeEditor()
    editor._draft = {"name": "old", "resistance_source_exponent": "1",
                     "resistance_provenance_status": "unvalidated"}
    _run("materials", editor, "update_window", fields={"resistance_source_exponent": "raw invalid"})
    assert editor._draft["resistance_source_exponent"] == "raw invalid"
    before = (deepcopy(editor._draft), editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("materials", editor, "update_window", fields={"id": "immutable"})
    with pytest.raises(UiControlError):
        _run("materials", editor, "window_action", action="edit_dataset", arguments={})
    assert (editor._draft, editor._revision, editor.calls) == before


def test_bad_text_length_nul_numeric_overflow_and_gas_rows_are_atomic():
    editor = FakeEditor()
    before = (deepcopy(editor._fields), editor._dirty, editor._revision, list(editor.calls))
    for value in ("x" * 20001, "bad\x00text"):
        with pytest.raises(UiControlError):
            _run("source", editor, "update_window", fields={"notes": value})
    with pytest.raises(UiControlError):
        _run("machine", editor, "update_window", fields={"carrier_x_mm": 10 ** 10000})
    with pytest.raises(UiControlError):
        _run("process", editor, "window_action", action="set_environment",
             arguments={"index": -1, "record": {"gas_components": [
                 {"species": "Ar", "unbounded": 1}]}})
    with pytest.raises(UiControlError):
        _run("process", editor, "window_action", action="set_environment",
             arguments={"index": -1, "record": {"gas_components": [
                 {"species": "Ar", "partial_pressure_pa": True}]}})
    for record in (
        {"effective_cross_section_source": "\ud800"},
        {"gas_components": [{"species": "\ud800"}]},
        {"gas_components": [{"species": "Ar", "cross_section_source": "\ud800"}]},
    ):
        with pytest.raises(UiControlError):
            _run("process", editor, "window_action", action="set_environment",
                 arguments={"index": -1, "record": record})
    assert (editor._fields, editor._dirty, editor._revision, editor.calls) == before


def test_machine_row_shape_and_material_null_model_fields_reject_without_mutation():
    editor = FakeEditor()
    editor._data["sources"] = ["not a source row"]
    editor._data["monitors"] = [{"name": "missing fields"}]
    before = (deepcopy(editor._data), editor._revision, list(editor.calls))
    for action, arguments in (
        ("set_source_field", {"index": 0, "field": "x_mm", "value": 3.0}),
        ("set_monitor_field", {"index": 0, "field": "name", "value": "new"}),
    ):
        with pytest.raises(UiControlError):
            _run("machine", editor, "window_action", action=action, arguments=arguments)
    assert (editor._data, editor._revision, editor.calls) == before

    editor._draft = {
        "name": "Air", "resistance_source_exponent": "",
        "resistance_provenance_status": "unknown",
    }
    editor._null_models = {"resistance"}
    editor._loading = False
    editor._operation_busy = False
    before = (deepcopy(editor._draft), editor._revision, list(editor.calls))
    with pytest.raises(UiControlError):
        _run("materials", editor, "update_window",
             fields={"resistance_source_exponent": "new value"})
    with pytest.raises(UiControlError):
        _run("materials", editor, "update_window",
             fields={"resistance_provenance_status": "theoretical"})
    assert (editor._draft, editor._revision, editor.calls) == before


def test_existing_source_and_substrate_bridges_keep_gui_invalid_text_and_point_selection():
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge

    app = QGuiApplication.instance() or QGuiApplication([])
    source = SourceBridge()
    initial_revision = source._draft_revision
    _run("source", source, "update_window", fields={"source_exponent": "draft: invalid"})
    assert source._fields["source_exponent"] == "draft: invalid"
    assert source._dirty and source._draft_revision == initial_revision + 1
    _run("source", source, "window_action", action="set_emission_field",
         arguments={"field": "reference_rate_nm_s", "value": "draft: invalid rate"})
    assert source._emission["reference_rate_nm_s"] == "draft: invalid rate"
    before = (dict(source._fields), source.dirty, source._draft_revision)
    with pytest.raises(UiControlError):
        _run("source", source, "update_window", fields={"notes": "\ud800"})
    assert (source._fields, source.dirty, source._draft_revision) == before

    substrate = SubstrateBridge()
    substrate.selectPoint(0)
    selected_before = substrate.selectedIndex
    initial_revision = substrate._draft_revision
    _run("substrate", substrate, "window_action", action="set_point",
         arguments={"index": 0, "x": 5.0, "y": 6.0})
    assert substrate._points[0] == [5.0, 6.0]
    assert substrate.selectedIndex == selected_before == 0
    assert substrate._dirty and substrate._draft_revision == initial_revision + 1
    app.processEvents()


def test_real_optical_only_material_null_models_and_surrogate_text_are_rejected():
    from PySide6.QtGui import QGuiApplication
    from coat_sim.core.materials import default_material_profile
    from coat_sim.ui.source_materials import MaterialLibraryBridge

    app = QGuiApplication.instance() or QGuiApplication([])
    editor = MaterialLibraryBridge(directory=None)
    material = default_material_profile("Optical only", id="test_optical_only", optical_only=True)
    editor._load_draft(material)
    before = (dict(editor._draft), editor.dirty, set(editor._null_models))
    with pytest.raises(UiControlError):
        _run("materials", editor, "update_window",
             fields={"resistance_source_exponent": "1.5"})
    with pytest.raises(UiControlError):
        _run("materials", editor, "update_window", fields={"notes": "\ud800"})
    assert (editor._draft, editor.dirty, editor._null_models) == before
    assert editor.draftData()["resistance_model"] is None
    app.processEvents()


def test_existing_document_bridge_machine_and_mask_setters_are_used_in_memory():
    from PySide6.QtGui import QGuiApplication
    from coat_sim.core.machine_profiles import new_machine_profile
    from coat_sim.ui.machine_mask import DocumentBridge, MaskBridge

    app = QGuiApplication.instance() or QGuiApplication([])
    machine = DocumentBridge("machine")
    machine.newDocument(json.dumps(new_machine_profile(), ensure_ascii=False))
    _run("machine", machine, "update_window", fields={"carrier_x_mm": 7.5})
    assert machine._data["carrier_x_mm"] == 7.5 and machine.dirty
    assert not machine.documentPath

    mask = MaskBridge()
    mask.newDocument(json.dumps({"name": "draft mask", "z_mm": 300.0,
                                 "outer": [[0, 0], [10, 0], [10, 10]], "holes": []}))
    points = [[1, 1], [9, 1], [9, 9], [1, 9]]
    _run("mask", mask, "window_action", action="set_contour",
         arguments={"contour": "outer", "index": 0, "points": points})
    assert mask._data["outer"] == points and mask.dirty
    assert not mask.documentPath
    app.processEvents()


def test_machine_and_mask_idempotent_setter_is_accepted_without_dirtying():
    from coat_sim.core.machine_profiles import new_machine_profile
    from coat_sim.ui.machine_mask import DocumentBridge, MaskBridge
    machine = DocumentBridge("machine")
    machine.initializeDocument(json.dumps(new_machine_profile()))
    _run("machine", machine, "update_window", fields={"carrier_x_mm": 0.0})
    assert machine._data["carrier_x_mm"] == 0 and not machine.dirty
    mask = MaskBridge()
    mask.initializeDocument(json.dumps({"z_mm": 300.0, "outer": [[0, 0], [10, 0], [10, 10]], "holes": []}))
    _run("mask", mask, "update_window", fields={"z_mm": 300.0})
    assert mask._data["z_mm"] == 300 and not mask.dirty
