"""Actual managed ray/appearance bars and request-ID guarded file operations."""
from copy import deepcopy
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtQuick import QQuickItem
from shiboken6 import isValid

from tests.test_project_film_desktop import desktop as desktop
from tests.test_project_film_desktop import events
from tests.test_project_input_toolbar_resources import activate_document, view_for

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def wait_resource(resources):
    for _ in range(500):
        events()
        if not resources.resourceBusy:
            return
    raise AssertionError(resources.status)


def wait_bridge(bridge):
    for _ in range(500):
        events()
        resources = getattr(bridge, "_project_input_resources", None)
        if not bridge.busy and not (resources is not None and resources.resourceBusy):
            return
    raise AssertionError(bridge.status)


def optical_view(desktop, key):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    client.open_project_module(workspace, key)
    events()
    window, bridge, *_rest = view_for(client, workspace, key)
    return client, workspace, window, bridge, bridge._project_input_resources


@pytest.mark.parametrize("key", ["ray_trace", "film_appearance"])
def test_real_managed_optical_input_new_metadata_save_copy_open_and_narrow(desktop, key):
    client, workspace, window, bridge, resources = optical_view(desktop, key)
    assert window.findChild(QQuickItem, "projectResourceControls") is not None
    assert not resources.canExportDocument
    assert len(window.findChildren(QQuickItem, "projectManagedDocumentPicker")) == 1
    assert resources.newDocument()
    information = resources.resourceInformation
    assert resources.updateResourceInformation("Saved optical input", "Input overview", "Input notes", information["token"])
    outcomes = []
    resources.resourceOperationFinished.connect(lambda *args: outcomes.append(args))
    assert resources.saveCurrent()
    save_id = resources.resourceOperationId
    assert save_id and resources.resourceBusy
    assert not resources.saveCurrent() and not resources.newDocument()
    assert not resources.cancelResourceOperation()
    wait_resource(resources)
    identity = resources.currentDocumentId
    assert identity and not resources.dirty, resources.status
    assert outcomes[-1] == (save_id, "save", True, "")
    document = resources.service.load_document(identity)
    assert document.data["name"] == "Saved optical input"
    assert document.data["description"] == "Input overview"
    assert document.data["notes"] == "Input notes"
    assert not any(field in document.data for field in ("result", "last_result", "job_id"))
    if key == "film_appearance":
        assert document.runtime["payload"]["context"] is None
        assert document.runtime["payload"]["draft"] == {}
        assert bridge._appearance is None
    assert resources.saveCopyCurrent()
    wait_resource(resources)
    assert resources.currentDocumentId != identity
    assert len(resources.documents) == 2
    assert resources.openDocument(identity)
    wait_resource(resources)
    assert resources.currentDocumentId == identity and not resources.dirty
    assert resources.resourceInformation["summary"] == "Input overview"
    second_root = next(root for root in desktop[1] if root != workspace.service.root)
    other = client.open_project(second_root)
    client.open_project_module(other, key)
    assert client.controller_for(other, key)._project_input_resources.documents == []
    assert not client.controller_for(other, key)._project_input_resources.openDocument(identity)
    window.show()
    for width in (1280, 720):
        window.setProperty("width", width)
        events()
        bar = window.findChild(QQuickItem, "projectResourceControls")
        for item in bar.childItems():
            if item.isVisible() and item.width() > 0:
                assert item.x() >= -1 and item.x() + item.width() <= bar.width() + 1


def test_failed_and_late_save_never_replace_newer_ray_draft(desktop, monkeypatch):
    _client, _workspace, window, bridge, resources = optical_view(desktop, "ray_trace")
    assert resources.newDocument() and resources.saveCurrent()
    wait_resource(resources)
    original = resources.currentDocumentId
    assert resources.saveCopyCurrent()
    wait_resource(resources)
    target = resources.currentDocumentId
    assert resources.openDocument(original)
    wait_resource(resources)
    bridge.setRequestText(bridge._request_text.replace("550.0", "560.0"))
    before = bridge._request_text
    original_save = resources.service.save_simulation_resource
    monkeypatch.setattr(resources.service, "save_simulation_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("Injected save failure")))
    assert resources.saveCurrent()
    wait_resource(resources)
    assert resources.currentDocumentId == original
    assert bridge._request_text == before and resources.dirty
    assert "Injected save failure" in resources.resourceOperationError

    started, release = Event(), Event()
    def delayed_save(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original_save(*args, **kwargs)
    monkeypatch.setattr(resources.service, "save_simulation_resource", delayed_save)
    activate_document(window, resources, target)
    dialog = window.findChild(QObject, "projectResourceReplaceDialog")
    assert dialog.property("visible")
    replacement_id = resources.replacementRequestId
    assert not resources.confirmReplace("save")
    assert started.wait(5)
    save_id = resources.resourceOperationId
    bridge.setRequestText(bridge._request_text.replace("560.0", "570.0"))
    assert not resources.confirmReplace("cancel")
    release.set()
    wait_resource(resources)
    assert resources.currentDocumentId == original and resources.dirty
    assert "570.0" in bridge._request_text
    assert resources.replacementRequestId == replacement_id
    assert dialog.property("visible")
    assert resources.resourceOperationId == save_id
    assert resources.confirmReplace("discard")
    wait_resource(resources)
    assert resources.currentDocumentId == target


def test_cancelled_late_open_and_destroyed_save_owner_are_safe(desktop, monkeypatch):
    client, workspace, window, bridge, resources = optical_view(desktop, "ray_trace")
    assert resources.newDocument() and resources.saveCurrent()
    wait_resource(resources)
    original = resources.currentDocumentId
    assert resources.saveCopyCurrent()
    wait_resource(resources)
    current = resources.currentDocumentId
    load = resources.service.load_document
    started, release = Event(), Event()
    def delayed_load(identity):
        if identity == original:
            started.set()
            assert release.wait(10)
        return load(identity)
    monkeypatch.setattr(resources.service, "load_document", delayed_load)
    assert resources.openDocument(original)
    assert started.wait(5)
    assert resources.cancelResourceOperation()
    release.set()
    for _ in range(10):
        events()
    assert resources.currentDocumentId == current
    monkeypatch.setattr(resources.service, "load_document", load)

    save = resources.service.save_simulation_resource
    started.clear()
    release.clear()
    def delayed_save(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return save(*args, **kwargs)
    monkeypatch.setattr(resources.service, "save_simulation_resource", delayed_save)
    before_count = len(resources.documents)
    assert resources.saveCopyCurrent()
    assert started.wait(5)
    assert not window.close()  # A dispatched commit cannot be cancelled by Close.
    window.deleteLater()  # Simulate forced presentation teardown.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    events()
    assert resources._detached and isValid(resources)
    assert resources.parent() is None
    release.set()
    for _ in range(500):
        events()
        if not isValid(resources):
            break
    assert not isValid(resources)
    assert len([row for row in workspace.service.project_store.manifest["documents"] if row["kind"] == "raytrace_request"]) == before_count + 1
    assert client.controller_for(workspace, "ray_trace") is None


def test_appearance_input_captures_real_asset_and_spd_reopens_without_originals(desktop, tmp_path):
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    _client, _workspace, _window, bridge, resources = optical_view(desktop, "film_appearance")
    asset_path = _write_synthetic_v7_asset(bridge.root)
    bridge.loadLayerAsset(str(asset_path))
    wait_bridge(bridge)
    appearance = bridge._appearance
    assert appearance is not None, bridge.status
    spectrum = tmp_path / "input-spd.csv"
    spectrum.write_text("wavelength_nm,value\n380,1\n780,1\n", encoding="utf-8")
    appearance.loadSpectrum(str(spectrum), "environment")
    wait_resource(resources)
    appearance.setFlag("environment_enabled", True)
    appearance.setField("camera_z", "-8")
    appearance.setField("spp", "12")
    draft = deepcopy(appearance._draft)
    # Selection already freezes external bytes, before the first config Save.
    asset_path.unlink()
    spectrum.unlink()
    assert resources.saveCurrent()
    wait_resource(resources)
    assert not resources.resourceOperationError, resources.status
    identity = resources.currentDocumentId
    assert identity and not resources.dirty
    assert Path(appearance._context["asset_path"]) != asset_path
    assert Path(appearance._draft["environment_spectrum"]["path"]).is_relative_to(bridge.root / "assets")
    assert resources.saveCopyCurrent()
    wait_resource(resources)
    assert not resources.resourceOperationError, resources.status
    assert resources.openDocument(identity)
    wait_resource(resources)
    assert not resources.resourceOperationError, resources.status
    assert bridge._appearance._draft["camera_z"] == "-8"
    assert bridge._appearance._draft["spp"] == draft["spp"]
    assert bridge._appearance._draft["environment_spectrum"]["value"] == [1., 1.]
    assert Path(bridge._appearance._context["asset_path"]).is_file()


def test_held_optical_draft_reopens_identity_and_recovery_is_unsaved_copy(desktop):
    client, workspace, window, bridge, resources = optical_view(desktop, "ray_trace")
    assert resources.newDocument() and resources.saveCurrent()
    wait_resource(resources)
    original = resources.currentDocumentId
    bridge.setRequestText("incomplete { preserve raw text")
    info = resources.resourceInformation
    assert resources.stageInformationEdit("Unconfirmed title", "Pending overview", "Pending notes", info["token"])
    compute = bridge._project_compute
    before = resources.snapshot_input_draft()
    checkpoint = compute.snapshot()
    assert compute.has_dirty_draft
    assert any(row["module_key"] == "ray_trace" for row in compute.close_preflight())
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    events()
    assert client.controller_for(workspace, "ray_trace") is None
    client.open_project_module(workspace, "ray_trace")
    events()
    _window, reopened, *_rest = view_for(client, workspace, "ray_trace")
    replacement = reopened._project_input_resources
    wait_resource(replacement)
    assert replacement.currentDocumentId == original
    assert reopened._request_text == before["state"]["request_text"]
    assert replacement.pendingInformationEdit["name"] == "Unconfirmed title"
    assert replacement.resourceInformation["name"] != "Unconfirmed title"
    replacement.clearInformationEdit()
    assert replacement.dirty
    result = compute.restore_recovery(checkpoint)
    assert result["optical_pending"]
    wait_resource(replacement)
    assert not compute.input_draft_restore_status()["pending"]
    assert compute.input_draft_restore_status()["success"]
    assert replacement.currentDocumentId == "" and replacement.dirty
    assert reopened._request_text == before["state"]["request_text"]
    assert replacement.pendingInformationEdit["name"] == "Unconfirmed title"
    replacement.clearInformationEdit()
    assert replacement.dirty  # Recovered copy stays unsaved when the buffer is cancelled.
    assert replacement.service.load_document(original).data["name"] != "Unconfirmed title"


def test_failed_restore_keeps_live_snapshot_and_reentrant_completion_keeps_new_edit(desktop, monkeypatch):
    from coat_sim.ui import project_raytrace_resources as managed
    _client, _workspace, window, bridge, resources = optical_view(desktop, "ray_trace")
    assert resources.newDocument() and resources.saveCurrent()
    wait_resource(resources)
    original = resources.currentDocumentId
    assert resources.saveCopyCurrent()
    wait_resource(resources)
    target = resources.currentDocumentId
    assert resources.openDocument(original)
    wait_resource(resources)
    live = resources.snapshot_input_draft()
    candidate = deepcopy(live)
    candidate["state"]["request_text"] = "candidate only {"
    candidate["dirty"] = candidate["model_dirty"] = True
    monkeypatch.setattr(managed, "_prepare_restored_input", lambda *_args:
        (_ for _ in ()).throw(ValueError("Injected restore failure")))
    assert isinstance(resources.restore_input_draft(candidate), str)
    assert resources.snapshot_input_draft()["state"] == live["state"]
    wait_resource(resources)
    assert "Injected restore failure" in resources.resourceOperationError
    assert resources.snapshot_input_draft()["state"] == live["state"]
    bridge.setRequestText(bridge._request_text.replace("550.0", "560.0"))
    activate_document(window, resources, target)
    def edit_on_completion(_request_id, kind, success, _error):
        if kind == "save" and success:
            bridge.setRequestText(bridge._request_text.replace("560.0", "580.0"))
    resources.resourceOperationFinished.connect(edit_on_completion)
    assert not resources.confirmReplace("save")
    wait_resource(resources)
    assert resources.currentDocumentId == original and resources.dirty
    assert "580.0" in bridge._request_text
    assert resources.replacementRequestId


def test_input_import_cancel_boundary_keeps_published_input_and_gui_responsive(desktop, tmp_path, monkeypatch):
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    _client, _workspace, _window, bridge, resources = optical_view(desktop, "ray_trace")
    assert resources.newDocument()
    source = _write_synthetic_v7_asset(tmp_path / "external")
    capture = resources.service.capture_simulation_asset
    started, release = Event(), Event()
    def before_publish(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return capture(*args, **kwargs)
    monkeypatch.setattr(resources.service, "capture_simulation_asset", before_publish)
    bridge.loadLayerAsset(str(source))
    assert started.wait(5) and resources.resourceBusy
    assert not resources.cancelResourceOperation()
    release.set()
    wait_resource(resources)
    assert bridge._asset is None
    assert not resources.service.list_documents("binary_asset")
    started.clear()
    release.clear()
    def after_publish(*args, **kwargs):
        result = capture(*args, **kwargs)
        started.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(resources.service, "capture_simulation_asset", after_publish)
    bridge.loadLayerAsset(str(source))
    assert started.wait(5)
    ticks = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    for _ in range(5):
        events()
    timer.stop()
    assert len(ticks) >= 5
    assert not resources.cancelResourceOperation()
    release.set()
    wait_resource(resources)
    assert len(resources.service.list_documents("binary_asset")) == 1
    assert bridge._asset is not None and Path(bridge._asset["path"]).is_relative_to(bridge.root / "assets")
    assert "输入已导入工程" in resources.status and "配置未保存" in resources.status
    assert resources.currentDocumentId == "" and resources.dirty
    source.unlink()
    assert resources.saveCurrent()
    wait_resource(resources)
    assert not resources.resourceOperationError


def test_appearance_recovery_preserves_invalid_fields_owned_spd_and_pending_information(desktop, tmp_path):
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    _client, _workspace, _window, bridge, resources = optical_view(desktop, "film_appearance")
    source = _write_synthetic_v7_asset(tmp_path / "external")
    bridge.loadLayerAsset(str(source))
    wait_resource(resources)
    assert bridge._appearance is not None, resources.status
    spectrum = tmp_path / "external-spd.csv"
    spectrum.write_text("wavelength_nm,value\n380,1\n780,2\n", encoding="utf-8")
    bridge._appearance.loadSpectrum(str(spectrum), "area")
    wait_resource(resources)
    bridge._appearance.setField("camera_x", "unfinished -")
    info = resources.resourceInformation
    assert resources.stageInformationEdit("Pending appearance", "Unconfirmed overview", "Unconfirmed note", info["token"])
    source.unlink()
    spectrum.unlink()
    compute = bridge._project_compute
    checkpoint = compute.snapshot()
    resources.clearInformationEdit()
    bridge._appearance.setField("camera_x", "3")
    result = compute.restore_recovery(checkpoint)
    assert result["optical_pending"]
    wait_resource(resources)
    assert compute.input_draft_restore_status()["success"], resources.status
    assert bridge._appearance._draft["camera_x"] == "unfinished -"
    assert bridge._appearance._draft["area_spectrum"]["value"] == [1., 2.]
    assert resources.currentDocumentId == "" and resources.dirty
    assert resources.pendingInformationEdit["name"] == "Pending appearance"
    resources.clearInformationEdit()
    assert resources.dirty


def test_inherited_deposition_output_is_captured_without_polluting_main_dirty(desktop):
    from coat_sim.ui.raytrace import _layer_asset_summary
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    client.open_project_module(workspace, "main")
    _main_window, main, *_rest = view_for(client, workspace, "main")
    deposition = main._project_input_resources
    assert not deposition.dirty
    compute = main._project_compute
    source = _write_synthetic_v7_asset(compute.root)
    context = _layer_asset_summary(source, compute.root)["appearance_context"]
    compute.set_appearance_context(context)
    client.open_project_module(workspace, "film_appearance")
    _window, bridge, *_rest = view_for(client, workspace, "film_appearance")
    resources = bridge._project_input_resources
    assert resources.resourceBusy
    before = deepcopy(bridge._appearance._draft)
    wait_resource(resources)
    assert not resources.resourceOperationError, resources.status
    assert bridge._appearance._draft == before
    assert Path(bridge._appearance._context["asset_path"]).is_relative_to(compute.root / "assets")
    assert resources.currentDocumentId == "" and resources.dirty
    assert not deposition.dirty and compute.has_dirty_draft
    assert len(resources.service.list_documents("binary_asset")) == 1
    assert resources.service.list_documents("appearance_configuration") == []
    source.unlink()
    checkpoint = compute.snapshot()
    compute.validate_recovery(checkpoint)
    assert checkpoint["optical_inputs"][0]["module_key"] == "appearance"
