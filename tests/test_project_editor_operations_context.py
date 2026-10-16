"""Exact registered non-film owners, live snapshots and stricter write fences."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest
from PySide6.QtCore import QObject, QMetaObject, Qt, Q_ARG

from coat_sim.control_errors import UiControlError
from coat_sim.ui.compute_target import ComputeTarget
from tests.test_project_editor_context import client_for, read
from tests.test_project_film_desktop import desktop as desktop, events

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def edit(live, *, fields=None, action=None, arguments=None, **changes):
    before = read(live)
    assert before["available"], before
    payload = live.args | {"instance_id": before["instance_id"],
                           "expected_snapshot_id": before["snapshot_id"]}
    if fields is not None:
        operation = "update_window"
        payload["fields"] = fields
    else:
        operation = "window_action"
        payload.update(action=action, arguments=arguments or {})
    return live.client.dispatch(operation, payload | changes)


@pytest.mark.parametrize("module,fields", [
    ("source", {"name": "Source current edit"}),
    ("substrate", {"name": "Substrate current edit"}),
    ("machine", {"carrier_x_mm": 12.5}),
    ("mask", {"z_mm": 250}),
    ("process", {"name": "Process current edit"}),
    ("materials", {"name": "Material current edit"}),
    ("main", {"carrier_rpm": "12.5"}),
    ("ray_trace", {"polarization": "p"}),
])
def test_exact_registered_owner_edits_without_film_editor_or_persistence(desktop, module, fields, monkeypatch):
    monkeypatch.setattr(ComputeTarget, "refresh", lambda *_args: None)
    live = client_for(desktop, module)
    try:
        if module == "ray_trace":
            live.controller.newRequest()
        before_store = deepcopy(next(iter(live.stores.values())).manifest)
        assert live.workspace._editor is None
        result = edit(live, fields=fields)
        assert result["accepted"] and result["dirty"] is True
        assert result["snapshot"]["available"]
        assert result["snapshot_id"] == read(live)["snapshot_id"]
        assert result["validation"] is not None
        assert live.workspace._editor is None
        assert next(iter(live.stores.values())).manifest == before_store
        assert len(desktop[0]._views) == 1
    finally:
        live.client.stop()


def test_invalid_raw_gui_text_is_accepted_with_actual_validation(desktop):
    live = client_for(desktop, "source")
    try:
        result = edit(live, fields={"source_exponent": ""})
        assert result["accepted"] and not result["validation"]["valid"]
        assert result["snapshot"]["summary"]["fields"]["source_exponent"] == ""
        assert live.controller.bridge._fields["source_exponent"] == ""
    finally:
        live.client.stop()


def test_main_invalid_threshold_text_is_visible_and_invalidates_write_token(desktop, monkeypatch):
    monkeypatch.setattr(ComputeTarget, "refresh", lambda *_args: None)
    live = client_for(desktop, "main")
    try:
        revision = live.controller._revision
        first = edit(live, fields={"fast_threshold_percent": ""})
        second = edit(live, fields={"fast_threshold_percent": "unfinished"})
        assert first["accepted"] and second["accepted"]
        assert not first["validation"]["valid"] and not second["validation"]["valid"]
        assert live.controller._revision == revision  # Retain original GUI semantics.
        assert first["snapshot_id"] != second["snapshot_id"]
        controls = second["snapshot"]["summary"]["raw_controls"]
        assert controls == {"fast_threshold_text": "unfinished", "fast_threshold_invalid": True}
        with pytest.raises(UiControlError) as error:
            edit(live, fields={"carrier_rpm": "19"}, expected_snapshot_id=first["snapshot_id"])
        assert error.value.code == "stale_snapshot"
        assert live.controller.fields["carrier_rpm"] != "19"
    finally:
        live.client.stop()


def test_readable_information_modal_is_not_write_permission(desktop):
    live = client_for(desktop, "source")
    try:
        dialog = live.window.findChild(QObject, "projectResourceInformationDialog")
        assert dialog is not None
        assert QMetaObject.invokeMethod(dialog, "open", Qt.ConnectionType.DirectConnection)
        events()
        before = deepcopy(live.controller.bridge._fields), live.controller.bridge.dirty, live.controller.bridge._draft_revision
        assert read(live)["available"] and dialog.property("editing") is False
        with pytest.raises(UiControlError) as error:
            edit(live, fields={"name": "Must not edit behind modal"})
        assert error.value.code == "pending_confirmation"
        assert (live.controller.bridge._fields, live.controller.bridge.dirty, live.controller.bridge._draft_revision) == before
        assert read(live)["available"]
        assert QMetaObject.invokeMethod(dialog, "close", Qt.ConnectionType.DirectConnection)
        events()
        assert edit(live, fields={"name": "After modal closed"})["accepted"]
    finally:
        live.client.stop()


@pytest.mark.parametrize("flag", ["_editing_frozen", "_read_only"])
def test_readable_bridge_flags_cannot_masquerade_as_write_eligibility(desktop, flag):
    live = client_for(desktop, "source")
    try:
        bridge = live.controller.bridge
        before = deepcopy(bridge._fields), bridge.dirty, bridge._draft_revision
        setattr(bridge, flag, True)
        assert read(live)["available"]
        with pytest.raises(UiControlError) as error:
            edit(live, fields={"name": "Must not edit"})
        assert error.value.code in {"project_frozen", "read_only_window"}
        assert (bridge._fields, bridge.dirty, bridge._draft_revision) == before
    finally:
        setattr(live.controller.bridge, flag, False)
        live.client.stop()


def test_snapshot_and_identity_guards_reject_new_resource_edits(desktop):
    live = client_for(desktop, "machine")
    try:
        first = read(live)
        before = deepcopy(live.controller.bridge._data)
        for change, code in (({"instance_id": "stale"}, "stale_window"),
                             ({"expected_snapshot_id": "0" * 64}, "stale_snapshot"),
                             ({"project_root": "/wrong"}, "project_identity_mismatch"),
                             ({"client_generation": "stale"}, "stale_client")):
            with pytest.raises(UiControlError) as error:
                edit(live, fields={"carrier_x_mm": 99}, **change)
            assert error.value.code == code
            assert live.controller.bridge._data == before
        assert first["snapshot_id"] == read(live)["snapshot_id"]
    finally:
        live.client.stop()


def test_substrate_live_drag_remains_readable_but_blocks_external_edit(desktop):
    live = client_for(desktop, "substrate")
    try:
        owner = live.controller.bridge
        owner._drag_index = 0
        before = deepcopy(owner._fields), deepcopy(owner._points), owner.dirty, owner._draft_revision
        assert read(live)["available"]
        with pytest.raises(UiControlError) as error:
            edit(live, fields={"outline_kind": "rectangle"})
        assert error.value.code == "native_geometry_draft"
        assert (owner._fields, owner._points, owner.dirty, owner._draft_revision) == before
        assert owner._drag_index == 0
    finally:
        live.controller.bridge._drag_index = -1
        live.client.stop()


def test_material_dataset_modal_and_new_save_are_not_hidden_edits(desktop):
    live = client_for(desktop, "materials")
    try:
        before = deepcopy(live.controller.bridge._draft)
        with pytest.raises(UiControlError) as error:
            edit(live, action="save")
        assert error.value.code == "unsupported_action"
        with pytest.raises(UiControlError) as error:
            edit(live, action="edit_dataset", arguments={"id": "x"})
        assert error.value.code == "unsupported_action"
        assert live.controller.bridge._draft == before
        snapshot = read(live)
        primary = live.window.property("editorInstance")
        assert primary is not None
        assert QMetaObject.invokeMethod(primary, "openSetEditor", Q_ARG("QVariant", ""))
        events()
        dataset = live.window.findChild(QObject, "materialEditor")
        assert dataset is not None
        assert QMetaObject.invokeMethod(dataset, "beginNew", Qt.ConnectionType.DirectConnection)
        events()
        assert dataset.property("visible")
        optical_before = deepcopy(live.controller.bridge._optical_sets)
        with pytest.raises(UiControlError) as error:
            live.client.dispatch("update_window", live.args | {
                "instance_id": snapshot["instance_id"], "expected_snapshot_id": snapshot["snapshot_id"],
                "fields": {"name": "Must not apply behind dataset modal"}})
        assert error.value.code == "editor_unavailable"
        assert live.controller.bridge._draft == before
        assert live.controller.bridge._optical_sets == optical_before
        assert dataset.property("visible")
        assert QMetaObject.invokeMethod(dataset, "close", Qt.ConnectionType.DirectConnection)
        events()
    finally:
        live.client.stop()


def test_existing_project_appearance_owner_edits_without_reopening_context(desktop, tmp_path, monkeypatch):
    from coat_sim.ui.appearance import AppearanceWindowController
    from coat_sim.ui.raytrace import _layer_asset_summary
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    monkeypatch.setattr(ComputeTarget, "refresh", lambda *_args: None)
    live = client_for(desktop, "film_appearance")
    external = tmp_path / "appearance-fixture"
    external.mkdir()
    context = _layer_asset_summary(_write_synthetic_v7_asset(external), external)["appearance_context"]
    owner = AppearanceWindowController(context, root=live.workspace.service.root,
        parent=live.controller, project_context=live.workspace)
    live.controller._appearance = owner
    try:
        monkeypatch.setattr(live.controller, "refreshProjectAppearanceContext", lambda: pytest.fail("context refresh"))
        monkeypatch.setattr(owner, "startRender", lambda: pytest.fail("render"))
        frozen_context = deepcopy(owner._context)
        before_store = deepcopy(next(iter(live.stores.values())).manifest)
        result = edit(live, fields={"spp": "17"})
        assert result["accepted"] and result["dirty"]
        assert result["snapshot"]["summary"]["draft"]["spp"] == "17"
        assert result["snapshot_id"] == read(live)["snapshot_id"]
        assert owner._context == frozen_context
        assert next(iter(live.stores.values())).manifest == before_store
        assert live.workspace._editor is None
        assert len(desktop[0]._views) == 1
    finally:
        live.client.stop()



def test_process_original_nonfinite_step_drafts_return_usable_distinct_snapshots(desktop):
    live = client_for(desktop, "process")
    try:
        assert edit(live, action="add_step")["accepted"]
        tokens = set()
        for text, marker in (("NaN", "NaN"), ("1e999", "Infinity"), ("-1e999", "-Infinity")):
            result = edit(live, action="set_step_field",
                          arguments={"index": 0, "field": "target_nm", "value": text})
            assert result["accepted"] and result["snapshot"]["available"]
            assert result["snapshot_id"]
            assert not result["validation"]["valid"]
            tokens.add(result["snapshot_id"])
            page = read(live, detail="steps", expected_snapshot_id=result["snapshot_id"])
            assert page["page"]["items"][0]["nonfinite_numbers"] == {"target_nm": marker}
            json.dumps(result, allow_nan=False)
        assert len(tokens) == 3
    finally:
        live.client.stop()
