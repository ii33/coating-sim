"""Real GUI setters behind finite compute-input schemas, without execution."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from typing import Any, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from tests import qt_test_guard  # noqa: F401
from PySide6.QtGui import QGuiApplication

from coat_sim.control_errors import UiControlError
from coat_sim.core.api import CoatService
from coat_sim.ui.appearance import AppearanceWindowController
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.main import MainBridge
from coat_sim.ui.project_compute_editor_operations import (
    apply_compute_editor_operation as apply, compute_editor_capabilities, compute_editor_validation,
    MAIN_INPUT_FIELDS, MAIN_FIELDS, APPEARANCE_INPUT_FIELDS, APPEARANCE_FLAGS, APPEARANCE_FIELDS,
)
from coat_sim.ui.raytrace import RayTraceWindowController
from coat_sim.ui.project_compute_snapshot import capture_compute_snapshot
from tests.test_utility_windows_compact_chrome_ui import _appearance_context

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class NoQueue:
    def __getattr__(self, name):
        raise AssertionError("unexpected queue operation: " + name)


@pytest.fixture
def editors(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    monkeypatch.setattr(ComputeTarget, "refresh", lambda *_args: None)
    main = MainBridge(CoatService(root=tmp_path))
    ray = RayTraceWindowController(root=tmp_path, queue_client=cast(Any, NoQueue()))
    appearance = AppearanceWindowController(_appearance_context(), root=tmp_path)
    for owner, methods in ((main, ("calculate", "cancel", "saveConfiguration")),
                           (ray, ("startCalculation", "cancelCalculation", "saveRequest", "loadLayerAsset")),
                           (appearance, ("startRender", "cancelRender", "loadSpectrum", "setBackend"))):
        for method in methods:
            monkeypatch.setattr(owner, method, lambda *_args, _name=method, **_kwargs: pytest.fail(_name))
    ray.setRequestText(json.dumps({
        "schema": "coat_sim.geometric_raytrace", "schema_version": 1,
        "units": {"length": "mm", "wavelength": "nm", "index": "n+i*k"},
        "polarization": "unpolarized",
        "samples": [{"sample_id": "r1", "origin_xz_mm": [0, 0], "direction_xz": [0, 1], "weight": 1}],
        "spectrum": [{"wavelength_nm": 550, "weight": 1}],
        "surfaces": [{"surface_id": "s1", "geometry": {"type": "plane", "z_mm": 10},
            "aperture_radius_mm": 5, "medium_before": [{"n": 1, "k": 0}],
            "medium_after": [{"n": 1.5, "k": 0}]}],
        "detector": {"z_mm": 20, "aperture_radius_mm": 10, "pixel_count": 11},
    }))
    yield main, ray, appearance
    # No pending executor was admitted in this fixture.
    main.detach_observers()
    ray.detach_observers()
    appearance.detach_observers()
    assert app is not None


def test_main_uses_original_setters_and_preserves_invalid_text(editors):
    main, _ray, _appearance = editors
    revision = main._revision
    apply("main", main, "update_window", fields={"carrier_rpm": "invalid intermediate"})
    assert main.fields["carrier_rpm"] == "invalid intermediate"
    assert main._revision == revision + 1
    assert not compute_editor_validation("main", main)["valid"]
    apply("main", main, "update_window", fields={"carrier_rpm": 12.5})
    apply("main", main, "update_window", fields={"fast_threshold_percent": ""})
    assert main.fastThresholdInvalid and main.fastThresholdText == ""
    apply("main", main, "update_window", fields={"algorithm_budget_seconds": 15})
    apply("main", main, "update_window", fields={"algorithm": "reference"})
    assert main.algorithmBudgetSeconds == 15 and main.algorithm == "reference"


@pytest.mark.parametrize("fields", [
    {"unknown": "x"}, {"carrier_rpm": {}}, {"carrier_rpm": float("nan")},
    {"fast_cycle": "false"}, {"algorithm": "arbitrary"}, {"algorithm_budget_seconds": True},
    {"carrier_rpm": "10", "grid_size": "8"}, {"backend": "gpu"},
])
def test_main_bad_requests_do_not_mutate_any_input(editors, fields):
    main, _ray, _appearance = editors
    before = deepcopy(main._project_draft_signature()), main._revision
    with pytest.raises(UiControlError):
        apply("main", main, "update_window", fields=fields)
    assert (main._project_draft_signature(), main._revision) == before


def test_appearance_raw_inputs_flags_and_spectrum_kind(editors):
    _main, _ray, owner = editors
    before_context = deepcopy(owner._context)
    apply("film_appearance", owner, "update_window", fields={"width": ""})
    assert owner.draft["width"] == ""
    assert not compute_editor_validation("film_appearance", owner)["valid"]
    apply("film_appearance", owner, "update_window", fields={"area_enabled": True})
    apply("film_appearance", owner, "update_window", fields={"area_kind": "absolute"})
    assert owner.draft["area_enabled"] is True and owner.draft["area_kind"] == "absolute"
    assert owner._context == before_context


@pytest.mark.parametrize("fields", [
    {"area_enabled": 1}, {"area_kind": "measured"}, {"width": []},
    {"area_spectrum": {"file": "arbitrary"}}, {"backend": "gpu"},
])
def test_appearance_unknown_or_malformed_inputs_are_atomic(editors, fields):
    _main, _ray, owner = editors
    before = owner.projectInputConfiguration()
    with pytest.raises(UiControlError):
        apply("film_appearance", owner, "update_window", fields=fields)
    assert owner.projectInputConfiguration() == before


def test_ray_patches_single_bounded_input_using_original_setter(editors):
    _main, ray, _owner = editors
    before = json.loads(ray.requestText)
    ray._result, ray._result_path = {"result": "old"}, "old-result"
    apply("ray_trace", ray, "update_window", fields={"polarization": "s"})
    assert json.loads(ray.requestText) == before | {"polarization": "s"}
    assert ray._result is None and ray._result_path == ""
    apply("ray_trace", ray, "window_action", action="set_sample_field",
          arguments={"sample_id": "r1", "field": "weight", "value": -1})
    # Original JSON draft controls keep physical invalidity until admission.
    assert json.loads(ray.requestText)["samples"][0]["weight"] == -1
    apply("ray_trace", ray, "window_action", action="set_surface_field",
          arguments={"surface_id": "s1", "field": "geometry", "value": {"type": "plane", "z_mm": 12}})
    assert json.loads(ray.requestText)["surfaces"][0]["geometry"]["z_mm"] == 12


@pytest.mark.parametrize("action,args", [
    ("set_sample_field", {"sample_id": "missing", "field": "weight", "value": 1}),
    ("set_sample_field", {"sample_id": "r1", "field": "origin_xz_mm", "value": [1]}),
    ("set_sample_field", {"sample_id": "r1", "field": "weight", "value": True}),
    ("set_surface_field", {"surface_id": "s1", "field": "geometry", "value": {"type": "plane", "z_mm": 1, "file": "x"}}),
    ("set_detector_field", {"field": "pixel_count", "value": 1.5}),
    ("set_spectrum_field", {"index": True, "field": "weight", "value": 1}),
    ("set_sample_field", {"sample_id": "r1", "field": "weight", "value": 1, "force": True}),
    ("start_calculation", {}), ("save", {}), ("replace_request", {"text": "{}"}),
])
def test_ray_rejects_unpublished_structure_without_partial_mutation(editors, action, args):
    _main, ray, _owner = editors
    before = ray.requestText, ray._current_snapshot(), ray._result
    with pytest.raises(UiControlError):
        apply("ray_trace", ray, "window_action", action=action, arguments=args)
    assert (ray.requestText, ray._current_snapshot(), ray._result) == before


def test_compute_capabilities_do_not_expose_execution_or_saved_resources():
    for module in ("main", "ray_trace", "film_appearance"):
        cap = compute_editor_capabilities(module)
        assert set(cap["editable_fields"]) == set(cap["field_schema"])
        assert set(cap["actions"]) == set(cap["action_schemas"])
        assert not set(cap["actions"]) & {"save", "calculate", "cancel", "start_render", "load"}



def _snapshot(owner, module):
    state, reason = capture_compute_snapshot(owner, None, module)
    assert reason is None and state is not None
    return state


def test_all_main_editable_fields_keep_actual_raw_drafts_readable_and_content_signed(editors):
    main, _ray, _appearance = editors
    assert MAIN_INPUT_FIELDS == main._VISIBLE_FIELDS | {"time_step_s"}
    assert set(compute_editor_capabilities("main")["editable_fields"]) == MAIN_FIELDS
    for field in sorted(MAIN_INPUT_FIELDS | {"fast_threshold_percent"}):
        apply("main", main, "update_window", fields={field: "unfinished-a"})
        main._revision = 0
        first = _snapshot(main, "main")
        apply("main", main, "update_window", fields={field: "unfinished-b"})
        main._revision = 0
        second = _snapshot(main, "main")
        if field == "fast_threshold_percent":
            assert second["summary"]["raw_controls"]["fast_threshold_text"] == "unfinished-b"
            assert second["summary"]["raw_controls"]["fast_threshold_invalid"] is True
        elif field == "time_step_s":
            assert second["summary"]["draft"]["requested_time_step_s"] == "unfinished-b"
        else:
            assert second["summary"]["draft"]["fields"][field] == "unfinished-b"
        assert first["signature"] != second["signature"]
    for field, values, projected in (
        ("algorithm", ("fast", "reference", "optimize"), "algorithm"),
        ("algorithm_budget_seconds", (1, 2), "algorithm_budget_s"),
        ("fast_cycle", (False, True), "fast_cycle"),
    ):
        signatures = set()
        for value in values:
            apply("main", main, "update_window", fields={field: value})
            main._revision = 0
            state = _snapshot(main, "main")
            assert state["summary"]["draft"][projected] == value
            signatures.add(state["signature"]["sha256"])
        assert len(signatures) == len(values)


def test_all_appearance_editable_fields_and_inheritance_are_readable_and_signed(editors):
    _main, _ray, owner = editors
    assert APPEARANCE_INPUT_FIELDS == owner._FIELD_KEYS
    assert set(compute_editor_capabilities("film_appearance")["editable_fields"]) == APPEARANCE_FIELDS
    for field in sorted(APPEARANCE_INPUT_FIELDS):
        apply("film_appearance", owner, "update_window", fields={field: "unfinished-a"})
        first = _snapshot(owner, "film_appearance")
        apply("film_appearance", owner, "update_window", fields={field: "unfinished-b"})
        second = _snapshot(owner, "film_appearance")
        assert second["summary"]["draft"][field] == "unfinished-b"
        assert first["signature"] != second["signature"]
    for field in sorted(APPEARANCE_FLAGS | {"area_kind", "environment_kind"}):
        values = (False, True) if field in APPEARANCE_FLAGS else ("relative", "absolute")
        signatures = set()
        for value in values:
            apply("film_appearance", owner, "update_window", fields={field: value})
            state = _snapshot(owner, "film_appearance")
            assert state["summary"]["draft"][field] == value
            signatures.add(state["signature"]["sha256"])
        assert len(signatures) == 2
    apply("film_appearance", owner, "update_window", fields={"geometry_thickness_mm": "1"})
    first = _snapshot(owner, "film_appearance")
    apply("film_appearance", owner, "window_action", action="reset_geometry_thickness_to_inherited", arguments={})
    second = _snapshot(owner, "film_appearance")
    assert first["summary"]["geometry_thickness_mode"] == "overridden"
    assert second["summary"]["geometry_thickness_mode"] == "inherited"
    assert first["signature"] != second["signature"]


def test_all_ray_patch_fields_bind_the_complete_request_without_computation(editors):
    _main, ray, _appearance = editors
    changes: tuple[tuple[str, dict[str, Any], str, Any], ...] = (
        ("set_sample_field", {"sample_id": "r1"}, "origin_xz_mm", [1, 2]),
        ("set_sample_field", {"sample_id": "r1"}, "direction_xz", [0.1, 0.9]),
        ("set_sample_field", {"sample_id": "r1"}, "weight", 2),
        ("set_surface_field", {"surface_id": "s1"}, "aperture_radius_mm", 6),
        ("set_surface_field", {"surface_id": "s1"}, "geometry", {"type": "plane", "z_mm": 12}),
        ("set_surface_field", {"surface_id": "s1"}, "geometry",
            {"type": "sphere", "center_z_mm": 20, "radius_mm": 10, "branch": "front"}),
        ("set_surface_field", {"surface_id": "s1"}, "geometry",
            {"type": "sphere", "center_z_mm": 21, "radius_mm": 11, "branch": "back"}),
        ("set_spectrum_field", {"index": 0}, "wavelength_nm", 600),
        ("set_spectrum_field", {"index": 0}, "weight", 2),
        ("set_detector_field", {}, "z_mm", 30),
        ("set_detector_field", {}, "aperture_radius_mm", 11),
        ("set_detector_field", {}, "pixel_count", 13),
    )
    for action, target, field, value in changes:
        before = _snapshot(ray, "ray_trace")
        apply("ray_trace", ray, "window_action", action=action,
              arguments={**target, "field": field, "value": value})
        after = _snapshot(ray, "ray_trace")
        assert before["signature"] != after["signature"], (action, field)
        json.dumps(after, allow_nan=False)
    signatures = set()
    for polarization in ("s", "p", "unpolarized"):
        apply("ray_trace", ray, "update_window", fields={"polarization": polarization})
        state = _snapshot(ray, "ray_trace")
        assert state["summary"]["request"]["polarization"] == polarization
        signatures.add(state["signature"]["sha256"])
    assert len(signatures) == 3
