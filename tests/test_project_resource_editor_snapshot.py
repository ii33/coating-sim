from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
import json

import pytest

from coat_sim.ui import project_resource_editor_operations as edits

from coat_sim.ui.project_resource_snapshot import capture_resource_snapshot


class Window:
    def __init__(self, **props):
        self.props = props
        self.children = {}

    def property(self, name):
        return self.props.get(name)

    def findChild(self, _type, name):
        return self.children.get(name)


class Controller:
    def __init__(self, module, bridge, window=None):
        self.module_key = module
        self.bridge = bridge
        self.window = window
        self._disposed = False
        self._opening = False
        self._transfer_busy = False
        self._pending_information_edit = None
        self._pending_ui = None
        self.pending_reads = 0

    def snapshot_pending_edits(self):
        self.pending_reads += 1
        return None


class NoService:
    def __getattr__(self, name):
        raise AssertionError(f"service operation called: {name}")


def _bridge(**attrs):
    bridge = SimpleNamespace(_dirty=True, _draft_revision=9)
    for key, value in attrs.items():
        setattr(bridge, key, value)
    # These APIs are forbidden for snapshot capture; failing methods make any
    # accidental use visible while keeping the adapter tests independent of I/O.
    bridge.documentData = lambda: (_ for _ in ()).throw(AssertionError("documentData called"))
    bridge.draftData = lambda *a, **k: (_ for _ in ()).throw(AssertionError("draftData called"))
    bridge.save = lambda *a, **k: (_ for _ in ()).throw(AssertionError("save called"))
    bridge.refresh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("refresh called"))
    bridge.service = NoService()
    return bridge


def _capture(module, bridge, window=None):
    window = window or Window()
    controller = Controller(module, bridge, window)
    state, reason = capture_resource_snapshot(controller, window, module)
    assert reason is None
    assert controller.pending_reads == 1
    return state


def test_source_snapshot_copies_all_apertures_and_safe_material_metadata():
    material = {
        "id": "mat-1", "name": "Al", "description": "metal", "notes": "measured",
        "roles": ["source"], "category": "metal",
        "electron_beam_model": {"defaults": {"reference_rate_nm_s": 1.4},
                                "provenance": {"status": "measured", "reference": "paper"}},
        "optical_constants": [{"id": "opt-1", "name": "visible", "kind": "table",
                               "wavelength_nm": [400, 500, 600], "n": [1.2, 1.3, 1.4],
                               "k": [2.2, 2.3, 2.4]}],
        "password": "must never escape", "network_settings": {"token": "hidden"},
    }
    bridge = _bridge(
        _fields={"name": "Source A", "description": "desc", "notes": "note",
                 "device_type": "custom", "kind": "point", "source_exponent": "1",
                 "network_settings": "hidden"},
        _emission={"kind": "uniform", "reference_rate_nm_s": "4", "extra": "hidden"},
        _emission_present=True,
        _apertures=[{"kind": "rectangle", "length_mm": "20", "width_mm": "10", "credential": "x"}
                    for _ in range(44)],
        _aperture_index=2, _material=material, _material_library=SimpleNamespace(loading=False, _pending=None),
    )
    original = deepcopy(material)
    state = _capture("source", bridge)
    assert state["summary"]["fields"]["name"] == "Source A"
    assert len(state["collections"]["apertures"]) == 44
    assert "credential" not in state["collections"]["apertures"][0]
    assert len(state["collections"]["materials"]) == 1
    safe_material = state["collections"]["materials"][0]
    assert safe_material["optical_sets"][0]["n_sample_count"] == 3
    assert "n" not in safe_material["optical_sets"][0]
    assert "password" not in str(state)
    assert "network_settings" not in str(state)
    assert bridge._material == original
    before = state["signature"]["sha256"]
    bridge._material["optical_constants"][0]["n"][1] = 9.7
    newer = _capture("source", bridge)
    assert newer["signature"]["sha256"] != before  # catches same-length optical edits


def test_substrate_snapshot_uses_raw_control_points_without_sampling():
    bridge = _bridge(
        _fields={"name": "Substrate", "outline_kind": "curve", "surface_kind": "asphere",
                 "radius_mm": "3", "roc_mm": "600", "conic_k": "0", "notes": "existing"},
        _points=[[float(i), float(i + 1)] for i in range(55)], _closed=True,
        _material={"id": "mat", "name": "Glass", "optical_constants": []},
        _material_library=SimpleNamespace(loading=False, _pending=None),
    )
    bridge.sample_substrate_curve = lambda *a: (_ for _ in ()).throw(AssertionError("sampling called"))
    state = _capture("substrate", bridge)
    assert len(state["collections"]["points"]) == 55
    assert state["collections"]["points"][-1] == {"point_index": 54, "x": 54.0, "y": 55.0}
    assert "outlinePoints" not in str(state)


def test_process_imported_source_map_is_bound_without_exposing_frozen_film_data():
    bridge = _bridge(_data={"name": "Process", "steps": [{"source_slot": 1, "rate_nm_s": 1}],
        "film_design_import": {"source_slots_by_material_id": {"m": 1}, "source_label": "Captured film"}})
    first = _capture("process", bridge)
    bridge._data["film_design_import"]["source_slots_by_material_id"]["m"] = 2
    second = _capture("process", bridge)
    assert first["summary"] == second["summary"]
    assert first["collections"] == second["collections"]
    assert first["signature"]["sha256"] != second["signature"]["sha256"]
    assert "source_slots_by_material_id" not in str(second)


def test_material_null_model_semantics_and_mask_invalidation_markers_are_bound():
    material = _bridge(_draft={"name": "same", "resistance_source_exponent": ""},
                       _null_models=set(), _optical_only=False)
    first = _capture("materials", material)
    material._optical_only, material._null_models = True, {"resistance"}
    second = _capture("materials", material)
    assert first["summary"]["draft"] == second["summary"]["draft"]
    assert second["summary"]["optical_only"]
    assert second["summary"]["null_models"] == ["resistance"]
    assert first["signature"]["sha256"] != second["signature"]["sha256"]
    mask = _bridge(_data={"z_mm": 2, "outer": [[0, 0], [1, 0], [1, 1]], "holes": []},
                   _cad_revision=1, _cad_provenance={"source": "captured"},
                   _weights={"present": True}, _point_weights={}, _field={},
                   _request_id=2, _point_request_id=3, _field_request_id=4)
    original = _capture("mask", mask)
    mask._cad_revision += 1
    mask._cad_provenance, mask._weights = {}, {}
    mask._request_id += 1
    invalidated = _capture("mask", mask)
    assert original["collections"] == invalidated["collections"]
    assert invalidated["summary"]["analysis_state"]["radial_available"] is False
    assert original["signature"]["sha256"] != invalidated["signature"]["sha256"]


def test_machine_snapshot_contains_complete_install_and_monitor_lists():
    bridge = _bridge(_data={
        "schema_version": 5, "name": "Machine", "carrier_x_mm": 1.0, "planet_teeth": 36,
        "sources": [{"file": f"s{i}.csrc", "x_mm": i, "y_mm": 0, "z_mm": 1,
                    "arbitrary_extra": "private"} for i in range(43)],
        "masks": [{"file": f"m{i}.cmsk"} for i in range(42)],
        "monitors": [{"name": f"Monitor {i}", "kind": "crystal", "x_mm": i,
                      "y_mm": 0, "z_mm": 1} for i in range(41)],
        "credentials": {"api_key": "not allowed"}, "network_settings": {"host": "hidden"},
    })
    state = _capture("machine", bridge)
    assert {key: len(value) for key, value in state["collections"].items()} == {
        "sources": 43, "masks": 42, "monitors": 41,
    }
    assert state["collections"]["sources"][-1]["file"] == "s42.csrc"
    assert "credentials" not in str(state)
    assert "network_settings" not in str(state)
    assert "arbitrary_extra" not in str(state)


def test_mask_snapshot_flattens_outer_and_holes_and_blocks_native_geometry_draft():
    bridge = _bridge(_data={"name": "Mask", "description": "kept", "z_mm": 123,
                            "outer": [[0, 0], [1, 0], [1, 1]],
                            "holes": [[[0.2, 0.2], [0.4, 0.2], [0.4, 0.4]], [[2, 2]]],
                            "credential": "never"})
    state = _capture("mask", bridge)
    points = state["collections"]["contour_points"]
    assert len(points) == 7
    assert points[0] == {"contour": "outer", "hole_index": -1, "point_index": 0, "x": 0, "y": 0}
    assert points[3]["hole_index"] == 0 and points[3]["point_index"] == 0
    assert points[-1]["hole_index"] == 1 and points[-1]["point_index"] == 0
    assert "credential" not in str(state)
    window = Window(draftPointer="/outer", draftPoints=[[2, 3]])
    assert capture_resource_snapshot(Controller("mask", bridge, window), window, "mask") == (
        None, "native_geometry_draft")


def test_process_snapshot_contains_complete_steps_and_existing_source_rows():
    steps = [{"source_slot": i + 1, "target_nm": 100 + i, "rate_nm_s": 2.0,
              "target_mode": "mean", "mask_slots": [0], "environment": {"password": "hidden"},
              "unknown_extra": "hidden"} for i in range(48)]
    sources = [{"slot": i + 1, "file": f"s{i}.csrc", "name": f"S{i}", "valid": True,
                "default_rate_nm_s": 1.2, "credential": "hidden"} for i in range(45)]
    bridge = _bridge(_data={"schema_version": 7, "name": "Process", "machine_file": "m.cmac",
                            "substrate_file": "s.csub", "description": "saved",
                            "steps": steps, "credentials": {"token": "hidden"}},
                     _source_preview={"sources": sources})
    state = _capture("process", bridge)
    assert len(state["collections"]["steps"]) == 48
    assert len(state["collections"]["sources"]) == 45
    assert state["collections"]["steps"][-1]["source_slot"] == 48
    assert state["collections"]["steps"][0]["environment"] == {}
    assert "unknown_extra" not in str(state)
    assert "credential" not in str(state)


def test_materials_snapshot_is_metadata_only_and_does_not_mutate_catalog_cache():
    catalog = [{"id": f"m{i}", "key": f"user:m{i}", "name": f"Material {i}",
                "description": "catalog description", "roles": ["film"],
                "sourceBindable": True, "substrateBindable": False, "searchText": "hidden?",
                "arbitrary_extra": {"token": "private"}} for i in range(57)]
    optical = [{"id": "set-a", "name": "wide", "kind": "table",
                "wavelength_nm": list(range(2500)), "n": [1.5] * 2500,
                "k": [0.0] * 2500, "k_data": {"wavelength_min": 200, "wavelength_max": 2500,
                                                    "wavelength_unit": "nm", "k": [0.0] * 2500}}]
    bridge = _bridge(_draft={"id": "draft-1", "name": "Mat", "description": "d" * 500,
                             "notes": "n" * 700, "electron_beam_reference_rate_nm_s": "12.4",
                             "credential": "hidden"}, _entry_cache=catalog, _optical_sets=optical,
                     _optical_set_data_revision=71)
    before_catalog, before_optical = deepcopy(catalog), deepcopy(optical)
    state = _capture("materials", bridge)
    assert len(state["collections"]["catalog"]) == 57
    assert state["summary"]["optical_set_data_revision"] == 71
    set_row = state["collections"]["optical_sets"][0]
    assert set_row["wavelength_count"] == 2500
    assert set_row["n_sample_count"] == 2500 and set_row["k_sample_count"] == 2500
    assert set_row["k_status"] == "available"
    assert "n" not in set_row and "k" not in set_row and "wavelength_nm" not in set_row
    assert "searchText" not in str(state) and "arbitrary_extra" not in str(state)
    assert state["content_truncated"] is True
    assert bridge._entry_cache == before_catalog and bridge._optical_sets == before_optical
    first_digest = state["signature"]["sha256"]
    bridge._optical_sets[0]["n"][500] = 1.7
    assert _capture("materials", bridge)["signature"]["sha256"] != first_digest


def test_fences_cover_disposed_pending_edits_loading_and_material_popup():
    bridge = _bridge(_fields={"name": "x"}, _material_library=SimpleNamespace(loading=False, _pending=None))
    window = Window()
    controller = Controller("source", bridge, window)
    controller._disposed = True
    assert capture_resource_snapshot(controller, window, "source") == (None, "disposed")

    controller._disposed = False
    controller._pending_information_edit = {"name": "unsaved"}
    assert capture_resource_snapshot(controller, window, "source") == (None, "pending_information_edit")

    controller._pending_information_edit = None
    bridge._pending_document_replace = ("open", "path")
    assert capture_resource_snapshot(controller, window, "source") == (None, "pending_confirmation")

    bridge._pending_document_replace = None
    library = bridge._material_library
    library.loading = True
    assert capture_resource_snapshot(controller, window, "source") == (None, "loading")

    library.loading = False
    popup = SimpleNamespace(property=lambda name: True if name == "visible" else None)
    window.children["materialEditor"] = popup
    assert capture_resource_snapshot(controller, window, "source") == (None, "modal_editor_draft")


def test_all_existing_draft_modals_are_fenced_without_opening_them():
    def visible_dialog():
        return SimpleNamespace(property=lambda name: True if name == "visible" else None)

    process = _bridge(_data={"schema_version": 7, "name": "P", "steps": []},
                      _source_preview={"sources": []})
    for name in ("processEnvironmentDialog", "filmDesignImportDialog", "filmStackDialog"):
        window = Window()
        window.children[name] = visible_dialog()
        assert capture_resource_snapshot(Controller("process", process, window),
                                         window, "process") == (None, "modal_editor_draft")

    mask = _bridge(_data={"z_mm": 12, "outer": [], "holes": []})
    mask_window = Window()
    mask_window.children["maskOptimizationDialog"] = visible_dialog()
    assert capture_resource_snapshot(Controller("mask", mask, mask_window),
                                     mask_window, "mask") == (None, "modal_editor_draft")

    source = _bridge(_fields={"name": "S"}, _material_library=SimpleNamespace(loading=False, _pending=None))
    source_window = Window()
    library = SimpleNamespace(_dirty=True)
    source_window.children["materialLibraryDialog"] = SimpleNamespace(
        property=lambda name: {"visible": True, "library": library}.get(name))
    assert capture_resource_snapshot(Controller("source", source, source_window),
                                     source_window, "source") == (None, "nested_material_draft")


def test_material_cache_and_signature_are_complete_beyond_page_slices():
    rows = [{"id": str(i), "name": f"m{i}", "roles": ["film"], "builtin": False,
             "description": f"description {i}"} for i in range(135)]
    bridge = _bridge(_draft={"id": "d", "name": "M"}, _entry_cache=rows,
                     _optical_sets=[], _optical_set_data_revision=4)
    state = _capture("materials", bridge)
    assert len(state["collections"]["catalog"]) == 135
    assert set(state["signature"]) == {"module", "sha256"}
    assert state["collections"]["catalog"][-1]["id"] == "134"
    first_digest = state["signature"]["sha256"]
    rows[-1]["description"] = "same length!"
    state2 = _capture("materials", bridge)
    assert state2["signature"]["sha256"] != first_digest


def test_non_json_optical_state_fails_closed_without_calling_repr():
    calls = []

    class UnsafeValue:
        def __repr__(self):
            calls.append("repr")
            raise AssertionError("repr must not be called")

    bridge = _bridge(_draft={"id": "d", "name": "M"}, _entry_cache=[],
                     _optical_sets=[{"id": "set", "name": "n", "kind": "table",
                                     "n": [UnsafeValue()]}])
    window = Window()
    result = capture_resource_snapshot(Controller("materials", bridge, window), window, "materials")
    assert result == (None, "invalid_editor_state")
    assert calls == []


def test_substrate_nested_material_draft_fence_uses_existing_dialog_names_and_instance():
    bridge = _bridge(_fields={"name": "Substrate"}, _points=[],
                     _material_library=SimpleNamespace(loading=False, _pending=None))
    library = SimpleNamespace(_dirty=True)
    for dialog_name in ("substrateMaterialLibraryDialog", "materialLibraryDialog"):
        window = Window()
        window.children[dialog_name] = SimpleNamespace(
            property=lambda name: {"visible": True, "library": library}.get(name))
        assert capture_resource_snapshot(Controller("substrate", bridge, window),
                                         window, "substrate") == (None, "nested_material_draft")

    window = Window(materialLibraryDialogInstance=SimpleNamespace(
        property=lambda name: {"visible": True, "library": library}.get(name)))
    assert capture_resource_snapshot(Controller("substrate", bridge, window),
                                     window, "substrate") == (None, "nested_material_draft")


def test_mask_reads_only_existing_qobject_controls_without_changing_values():
    from PySide6.QtCore import QObject

    class QtWindow(QObject):
        pass

    window = QtWindow()
    control_values = {
        "tool_index": ("currentIndex", 3),
        "shift_x_mm": ("text", "12.5"), "shift_y_mm": ("text", "-2"),
        "scale_x": ("text", "1.25"), "scale_y": ("text", "0.75"),
        "mask_slot": ("value", 4), "source_slot": ("value", 5),
        "angular_radius_mm": ("value", 1.5), "patch_radius_mm": ("text", "7.2"),
        "auto_weight_y": ("checked", True),
        "weight_y_min": ("text", "0.1"), "weight_y_max": ("text", "0.9"),
        "heat_weight_mode": ("currentIndex", 2),
        "rotation_polygon": ("currentIndex", 1),
        "rotation_center_x_mm": ("text", "12"), "rotation_center_y_mm": ("text", "13"),
        "rotation_angle_deg": ("text", "14"),
    }
    controls = {}
    for key, (prop, value) in control_values.items():
        control = QObject(window)
        control.setObjectName("coatcontrol:" + key)
        control.setProperty(prop, value)
        control.setProperty("enabled", True)
        controls[key] = control
    extra = QObject(window)
    extra.setObjectName("coatcontrol:credential")
    extra.setProperty("text", "must stay out")

    bridge = _bridge(_data={"name": "Mask", "outer": [], "holes": []})
    state = _capture("mask", bridge, window)
    projected = state["summary"]["controls"]
    assert set(projected) == set(control_values)
    for key, (prop, value) in control_values.items():
        assert projected[key] == {"property": prop, "value": value, "enabled": True}
        assert controls[key].property(prop) == value
    assert "credential" not in str(state)


def test_mask_and_process_compute_busy_fences_return_unavailable():
    for busy_key in ("_radial_busy", "_field_busy", "_point_busy", "_cad_busy"):
        bridge = _bridge(_data={"name": "Mask", "outer": [], "holes": []})
        setattr(bridge, busy_key, True)
        window = Window()
        assert capture_resource_snapshot(Controller("mask", bridge, window),
                                         window, "mask") == (None, "busy")

    bridge = _bridge(_data={"name": "Mask", "outer": [], "holes": []},
                     _optimization_coordinator=SimpleNamespace(_optimization={"busy": True}))
    window = Window()
    assert capture_resource_snapshot(Controller("mask", bridge, window), window, "mask") == (
        None, "busy")

    bridge = _bridge(_data={"name": "Process", "steps": []}, _optimization={"busy": True})
    window = Window()
    assert capture_resource_snapshot(Controller("process", bridge, window), window, "process") == (
        None, "busy")



def test_substrate_unapplied_point_controls_are_separate_from_geometry_and_bind_signature():
    from PySide6.QtCore import QObject
    window = QObject()
    control = QObject(window)
    control.setObjectName("coatcontrol:point_x_mm")
    control.setProperty("text", "4.5")
    control.setProperty("enabled", True)
    bridge = _bridge(_fields={"name": "Substrate"}, _points=[[0, 0]], _closed=True)
    first = _capture("substrate", bridge, window)
    assert first["summary"]["controls"]["point_x_mm"]["value"] == "4.5"
    control.setProperty("text", "7.5")
    second = _capture("substrate", bridge, window)
    assert first["signature"] != second["signature"]
    assert second["collections"]["points"] == first["collections"]["points"]
    assert bridge._points == [[0, 0]]


def test_material_import_preview_and_restore_confirmation_withhold_preimport_draft():
    for plan in ({}, {"rows": [{"id": "pending"}]}):
        bridge = _bridge(_draft={"id": "old", "name": "Before import"}, _entry_cache=[],
                         _optical_sets=[], _import_preview_plan=plan)
        window = Window()
        assert capture_resource_snapshot(Controller("materials", bridge, window), window,
                                         "materials") == (None, "pending_confirmation")
        assert bridge._import_preview_plan is plan
    bridge._import_preview_plan = None
    for name in ("materialLibraryImportPreviewDialog", "restoreBuiltinDialog", "materialLibraryDeleteDialog"):
        window = Window()
        window.children[name] = SimpleNamespace(property=lambda key: key == "visible")
        assert capture_resource_snapshot(Controller("materials", bridge, window), window,
                                         "materials") == (None, "pending_confirmation")
    for module in ("source", "substrate"):
        window = Window()
        bridge = _bridge(_fields={"name": "Existing resource"},
                         _material_library=SimpleNamespace(loading=False, _pending=None,
                                                           _import_preview_plan={}))
        assert capture_resource_snapshot(Controller(module, bridge, window), window, module) == (
            None, "pending_confirmation")


def test_existing_nested_information_facade_pending_edits_are_not_silently_ignored():
    pending = {"name": "Unconfirmed material name", "notes": "staged user note"}
    for module in ("source", "substrate", "materials"):
        library = SimpleNamespace(loading=False, _pending=None,
            _information_controller=SimpleNamespace(_pending_information_edit=pending))
        bridge = (library if module == "materials" else
                  _bridge(_fields={"name": "Existing resource"}, _material_library=library))
        window = Window()
        assert capture_resource_snapshot(Controller(module, bridge, window), window, module) == (
            None, "pending_confirmation")
        assert library._information_controller._pending_information_edit is pending


def test_source_and_material_rate_text_units_do_not_claim_persisted_nm_per_second():
    source = _bridge(_fields={"name": "Source"},
                     _emission={"reference_rate_nm_s": "10", "valid_rate_min_nm_s": "2"},
                     _preview_rate="10", _apertures=[],
                     _material={"name": "Captured", "resistance_model": {
                         "defaults": {"reference_rate_nm_s": 1.0}, "provenance": {}}})
    state = _capture("source", source)
    assert state["summary"]["emission"]["reference_rate_nm_s"] == "10"
    assert state["summary"]["field_units"]["emission.reference_rate_nm_s"] == "angstrom_per_second"
    assert state["summary"]["field_units"]["preview_rate"] == "angstrom_per_second"
    captured = state["collections"]["materials"][0]
    assert captured["model_rate_unit"] == "nm_per_second"
    assert captured["models"]["resistance_model"]["defaults"]["reference_rate_nm_s"] == 1.0
    material = _bridge(_draft={"id": "mat", "name": "Material",
                               "resistance_reference_rate_nm_s": "10"},
                       _entry_cache=[], _optical_sets=[])
    state = _capture("materials", material)
    assert state["summary"]["draft"]["resistance_reference_rate_nm_s"] == "10"
    assert state["summary"]["field_units"]["draft.resistance_reference_rate_nm_s"] == "angstrom_per_second"


def test_resource_document_handles_bind_identity_without_resolving_or_exposing_paths():
    for module in ("source", "substrate", "machine", "mask", "process"):
        bridge = _bridge(_fields={"name": "Same"}, _data={"name": "Same"},
                         _path=Path("/private/one/same.json"))
        first = _capture(module, bridge)
        assert first["summary"]["document_identity"]["filename"] == "same.json"
        assert "/private/" not in str(first)
        bridge._path = Path("/private/two/same.json")
        second = _capture(module, bridge)
        assert first["signature"] != second["signature"]
        assert first["summary"]["document_identity"] != second["summary"]["document_identity"]
        assert bridge._path == Path("/private/two/same.json")

    class UnexpectedPath:
        def __str__(self):
            raise AssertionError("arbitrary conversion called")

    bridge._path = UnexpectedPath()
    window = Window()
    assert capture_resource_snapshot(Controller("process", bridge, window), window, "process") == (
        None, "invalid_editor_state")


def test_mask_cad_import_modals_withhold_unapplied_options_and_geometry():
    bridge = _bridge(_data={"name": "Existing mask", "outer": [[0, 0]], "holes": []})
    for name in ("maskImportReplaceConfirm", "maskImportOptionsDialog", "maskCadImportFileDialog"):
        window = Window()
        window.children[name] = SimpleNamespace(property=lambda key: key == "visible")
        assert capture_resource_snapshot(Controller("mask", bridge, window), window, "mask") == (
            None, "modal_editor_draft")
        assert bridge._data["outer"] == [[0, 0]]
        window.children.clear()
        assert _capture("mask", bridge, window)["collections"]["contour_points"][0]["x"] == 0


def test_source_raw_rate_labels_match_display_units_without_changing_maximum_rate_unit():
    qml = (Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/SourceEmissionForm.qml").read_text()
    for label in ("参考速率", "有效速率下限", "有效速率上限"):
        assert label + " / Å/s" in qml
    assert "设备有效最大沉积速率 / nm/s" in qml


# Exhaustive contract: every exposed raw scalar and structural action target
# is readable and signed independently from revision, dirty and validation.
_RAW_FIELD_CONTRACTS = (
    ("source", "_fields", "fields", edits.SOURCE_FIELDS),
    ("source", "_emission", "emission", edits.SOURCE_EMISSION_FIELDS),
    ("substrate", "_fields", "fields", edits.SUBSTRATE_FIELDS),
    ("materials", "_draft", "draft", edits._materials_fields()),
)


@pytest.mark.parametrize("module,attribute,projected,field", [
    (module, attribute, projected, field)
    for module, attribute, projected, fields in _RAW_FIELD_CONTRACTS for field in sorted(fields)
])
def test_every_editable_raw_resource_scalar_is_readable_and_signed(module, attribute, projected, field):
    bridge = _bridge(**{attribute: {field: "unfinished-a"}})
    first = _capture(module, bridge)
    getattr(bridge, attribute)[field] = "unfinished-b"
    second = _capture(module, bridge)
    assert first["summary"][projected][field] == "unfinished-a"
    assert second["summary"][projected][field] == "unfinished-b"
    assert first["signature"] != second["signature"]
    assert first["summary"]["revision"] == second["summary"]["revision"]
    assert first["summary"]["dirty"] == second["summary"]["dirty"]


@pytest.mark.parametrize("module,collection,field", [
    ("source", "apertures", field) for field in sorted(edits.APERTURE_FIELDS)
] + [
    ("machine", "sources", field) for field in ("x_mm", "y_mm", "z_mm")
] + [
    ("machine", "monitors", field) for field in sorted(edits.MONITOR_FIELDS)
] + [
    ("process", "steps", field) for field in
    ("target_nm", "rate_nm_s", "target_mode", "monitor_slot", "source_slot", "mask_slots")
])
def test_every_editable_resource_row_scalar_is_readable_and_signed(module, collection, field):
    before = [0] if field == "mask_slots" else "unfinished-a" if module == "source" else 1
    after = [1] if field == "mask_slots" else "unfinished-b" if module == "source" else 2
    if module == "source":
        bridge = _bridge(_apertures=[{field: before}], _aperture_index=0)
        row = bridge._apertures[0]
    else:
        bridge = _bridge(_data={collection: [{field: before}]})
        row = bridge._data[collection][0]
    first = _capture(module, bridge)
    row[field] = after
    second = _capture(module, bridge)
    assert second["collections"][collection][0][field] == after
    assert first["signature"] != second["signature"]


@pytest.mark.parametrize("module,field", [
    (module, field) for module, fields in (
        ("machine", edits.MACHINE_GEOMETRY_FIELDS | edits.MACHINE_METADATA_FIELDS),
        ("mask", edits.MASK_FIELDS), ("process", edits.PROCESS_FIELDS)) for field in sorted(fields)
])
def test_every_editable_json_document_scalar_is_readable_and_signed(module, field):
    bridge = _bridge(_data={field: 1})
    first = _capture(module, bridge)
    bridge._data[field] = 2
    second = _capture(module, bridge)
    assert second["summary"][field] == 2
    assert first["signature"] != second["signature"]


@pytest.mark.parametrize("module", ["machine", "mask", "process"])
def test_complete_retained_json_evidence_distinguishes_invalid_states_and_stays_private(module):
    # Unknown retained state remains private; it still binds an optimistic token.
    # No lossy projection, validation/status revision or UI normalization is used.
    bridge = _bridge(_data={"name": "unchanged"})
    variants: list[Any] = [None, {}, [], float("nan"), float("inf"), -float("inf")]
    states = [_capture(module, bridge)]
    for value in variants:
        bridge._data["private_extension"] = value
        states.append(_capture(module, bridge))
    assert len({state["signature"]["sha256"] for state in states}) == len(states)
    for state in states:
        assert "private_extension" not in str(state)
        json.dumps(state, allow_nan=False)
    bridge._data = {"name": "unchanged", "private_extension": {"a": 1, "b": 2}}
    first = _capture(module, bridge)
    bridge._data = {"private_extension": {"b": 2, "a": 1}, "name": "unchanged"}
    assert first == _capture(module, bridge)


def test_process_nonfinite_step_drafts_remain_readable_with_distinct_json_safe_tokens():
    bridge = _bridge(_data={"steps": [{"target_nm": 1}]})
    signatures = set()
    for value, marker in ((float("nan"), "NaN"), (float("inf"), "Infinity"), (-float("inf"), "-Infinity")):
        bridge._data["steps"][0]["target_nm"] = value
        state = _capture("process", bridge)
        row = state["collections"]["steps"][0]
        assert row["target_nm"] is None
        assert row["nonfinite_numbers"] == {"target_nm": marker}
        signatures.add(state["signature"]["sha256"])
        json.dumps(state, allow_nan=False)
    assert len(signatures) == 3


@pytest.mark.parametrize("index", [-1, 0])
def test_complete_environment_records_and_gas_components_are_readable_and_signed(index):
    from coat_sim.ui.project_resource_snapshot import _ENVIRONMENT_FIELDS, _GAS_COMPONENT_FIELDS
    record: dict[str, Any] = {key: 1 for key in _ENVIRONMENT_FIELDS}
    record["gas_components"] = [{key: 1 for key in _GAS_COMPONENT_FIELDS}]
    data = {"steps": [{"environment": record}]} if index == 0 else {"environment": record}
    bridge = _bridge(_data=data)
    for row, keys in ((record, _ENVIRONMENT_FIELDS), (record["gas_components"][0], _GAS_COMPONENT_FIELDS)):
        for key in sorted(keys):
            first = _capture("process", bridge)
            row[key] = 2
            second = _capture("process", bridge)
            env = second["collections"]["steps"][0]["environment"] if index == 0 else second["summary"]["environment"]
            assert (env if row is record else env["gas_components"][0])[key] == 2
            assert first["signature"] != second["signature"]
    absent = _capture("process", _bridge(_data={"steps": [{}]}))
    empty = _capture("process", _bridge(_data={"environment": {}, "steps": [{}]}))
    assert absent["summary"]["environment_present"] is False
    assert empty["summary"]["environment_present"] is True
    assert absent["signature"] != empty["signature"]


@pytest.mark.parametrize("module,attribute,collection", [
    ("substrate", "_points", "points"), ("mask", "_data", "contour_points")
])
def test_geometry_actions_bind_coordinates_topology_and_order(module, attribute, collection):
    points = [[0, 0], [1, 0], [1, 1]]
    bridge = _bridge(**{attribute: points if module == "substrate" else {"outer": points, "holes": []}})
    states = [_capture(module, bridge)]
    points[1][0] = 2
    states.append(_capture(module, bridge))
    points.reverse()
    states.append(_capture(module, bridge))
    points.pop()
    states.append(_capture(module, bridge))
    assert len({state["signature"]["sha256"] for state in states}) == 4
    assert len(states[-1]["collections"][collection]) == 2


def test_process_preview_and_mask_machine_context_are_bound_without_public_raw_data():
    process = _bridge(_data={"steps": []}, _source_preview={"sources": [], "masks": []})
    first = _capture("process", process)
    process._source_preview["masks"].append({"slot": 0, "private_geometry": [[0, 0]]})
    second = _capture("process", process)
    assert second["summary"]["mask_count"] == 1
    assert first["signature"] != second["signature"]
    process._source_preview["masks"][0]["private_geometry"][0][0] = 1
    third = _capture("process", process)
    assert second["summary"] == third["summary"]
    assert second["signature"] != third["signature"]
    assert "private_geometry" not in str(third)
    mask = _bridge(_data={"z_mm": 1}, _machine={"sources": [{"x_mm": 1}], "masks": []})
    first = _capture("mask", mask)
    mask._machine["sources"][0]["x_mm"] = 2
    second = _capture("mask", mask)
    assert first["summary"] == second["summary"]
    assert first["signature"] != second["signature"]


@pytest.mark.parametrize("attribute", [
    "_cad_provenance", "_weights", "_point_weights", "_field",
    "_request_id", "_point_request_id", "_field_request_id",
])
def test_each_mask_analysis_and_provenance_marker_independently_invalidates_token(attribute):
    bridge = _bridge(_data={"z_mm": 1}, _cad_revision=7, _cad_provenance={"source": "one"},
        _weights={}, _point_weights={}, _field={}, _request_id=1, _point_request_id=1, _field_request_id=1)
    first = _capture("mask", bridge)
    setattr(bridge, attribute, {"source": "two"} if attribute == "_cad_provenance" else
            {"available": True} if attribute in {"_weights", "_point_weights", "_field"} else 2)
    second = _capture("mask", bridge)
    assert first["signature"] != second["signature"]
    assert first["summary"]["cad_revision"] == second["summary"]["cad_revision"]
    assert first["summary"]["cad_provenance_present"] == second["summary"]["cad_provenance_present"]
