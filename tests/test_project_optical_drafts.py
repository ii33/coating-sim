"""Real project persistence with Qt-free asynchronous optical draft providers."""
from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from coat_sim.core.project_compute import ProjectComputeError, ProjectComputeWorkspace
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_optical_recovery import digest, information_token
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from tests.test_project_simulation_resources import asset, ray

pytestmark = pytest.mark.contract


def workspace(root):
    store = ProjectStore.create(root, "Optical drafts")
    host = ProjectSessionHost(lambda _root: store)
    return ProjectComputeWorkspace.for_project(host, host.open_project(root).binding)


def raw(module="ray_trace") -> dict[str, Any]:
    state: dict[str, Any] = ({"request_text": json.dumps(ray()["request"]), "deposition_asset": None}
             if module == "ray_trace" else {
                 "schema": "coat_sim.appearance_configuration", "version": 1,
                 "context": None, "draft": {}, "geometry_thickness_mode": "inherited",
                 "backend": "cpu", "precision": "native"})
    return {"module_key": module, "document_id": None, "revision": None,
            "information": {"name": "Optical draft", "description": "Overview", "notes": "Notes"},
            "model_dirty": True, "dirty": True, "state": state,
            "pending_information": None, "operation": None}


class Provider:
    """Explicitly separate accepting work from publishing its actual outcome."""

    def __init__(self, compute, module="ray_trace", record=None):
        self.compute, self.module = compute, module
        self.record: dict[str, Any] = deepcopy(record or raw(module))
        self.pending_restore: dict[str, Any] | None = None
        self.restore_calls: list[dict[str, Any]] = []
        self.fail_admission = False
        self.save_return: str | bool | None = None
        self.destroyed = False
        self._latest_request_id = ""
        self._restore_request_id = ""

    def input_draft_operation_id(self) -> str:
        return self._latest_request_id

    def snapshot_input_draft(self):
        if self.destroyed:
            raise AssertionError("destroyed provider")
        return deepcopy(self.record)

    def save_input_draft(self):
        if self.save_return is not None:
            return self.save_return
        request = uuid4().hex
        self._latest_request_id = request
        self.record["operation"] = {"request_id": request, "kind": "save"}
        return request

    def finish_save(self, request, *, success=True, error=""):
        if self.record["operation"] and self.record["operation"]["request_id"] == request:
            self.record["operation"] = None
            if success:
                self.record["dirty"] = self.record["model_dirty"] = False
        return self.compute.complete_input_draft_save(self.module, self, request, success, error)

    def restore_input_draft(self, record):
        if self.fail_admission:
            return False
        request = uuid4().hex
        self._latest_request_id = self._restore_request_id = request
        self.pending_restore = deepcopy(record)
        self.restore_calls.append(deepcopy(record))
        self.record["operation"] = {"request_id": request, "kind": "restore"}
        return request

    def finish_restore(self, *, success=True, error=""):
        assert self.pending_restore is not None
        request = self._restore_request_id
        if success and not self.compute.input_draft_restore_is_current(self.module, self, request):
            success, error = False, "Newer draft or operation kept; restore completion is stale"
        if success:
            self.record = deepcopy(self.pending_restore)
        if self.record["operation"] is not None and self.record["operation"]["request_id"] == request:
            self.record["operation"] = None
        return self.compute.complete_input_draft_restore(self.module, self, request, success, error)


def resign(record):
    record["token"] = digest({key: value for key, value in record.items() if key not in {"token", "sha256"}})
    record["sha256"] = digest({key: value for key, value in record.items() if key != "sha256"})


def test_module_dirty_preflight_and_destroyed_provider_retains_detached_draft(tmp_path):
    compute = workspace(tmp_path / "project")
    assert not compute.has_dirty_draft
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    assert compute.has_dirty_draft
    assert compute.close_preflight()[0]["module_key"] == "ray_trace"
    before = compute.draft_signature()
    provider.record["state"]["request_text"] = "incomplete {"
    assert compute.draft_signature() != before
    snapshot = compute.snapshot()
    assert snapshot["optical_inputs"][0]["state"]["request_text"] == "incomplete {"
    assert compute.unregister_input_draft_provider("ray_trace", provider)
    provider.destroyed = True
    assert compute.has_dirty_draft and compute.has_pending_draft
    assert not compute.has_dirty_deposition_draft and not compute.has_pending_deposition_draft
    assert compute.snapshot() == snapshot
    replacement = Provider(compute)
    compute.register_input_draft_provider("ray_trace", replacement)
    assert compute.has_pending_draft
    assert replacement.finish_restore()
    assert replacement.record["state"]["request_text"] == "incomplete {"
    assert not compute.has_pending_draft


def test_save_acceptance_wrong_id_failure_and_started_commit_are_distinct(tmp_path):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    request = compute.begin_input_draft_save("ray_trace")
    assert not compute.input_draft_save_status("ray_trace", request)["done"]
    assert compute.begin_input_draft_save("ray_trace") == request
    assert not compute.complete_input_draft_save("ray_trace", provider, uuid4().hex, True)
    with pytest.raises(ProjectError, match="finish"):
        compute.close_preflight()
    with pytest.raises(ProjectError, match="finish"):
        compute.unregister_input_draft_provider("ray_trace", provider)
    assert provider.finish_save(request, success=False, error="disk full")
    result = compute.input_draft_save_status("ray_trace", request)
    assert result["done"] and not result["success"] and result["error"] == "disk full"
    assert compute.has_dirty_draft
    assert not compute.complete_input_draft_save("ray_trace", provider, request, True)
    second = compute.begin_input_draft_save("ray_trace")
    assert second != request
    assert provider.finish_save(second)
    assert not compute.has_dirty_draft
    assert compute.close_preflight() == ()


@pytest.mark.parametrize("accepted", [True, False])
def test_boolean_save_acceptance_never_satisfies_completion(tmp_path, accepted):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    provider.save_return = accepted
    with pytest.raises(ProjectError) as error:
        compute.begin_input_draft_save("ray_trace")
    assert error.value.code == "save_not_started"
    assert compute.has_dirty_draft


def test_recovery_is_unsaved_copy_and_information_remains_independent(tmp_path):
    compute = workspace(tmp_path / "project")
    document = compute.service.save_simulation_resource("raytrace_request", ray(), name="Saved")
    identity = Path(document.path).stem
    provider = Provider(compute)
    provider.record.update(document_id=identity, revision=compute.service._record(identity)["sha256"])
    provider.record["pending_information"] = {
        "name": "Unconfirmed name", "summary": "Unconfirmed summary", "notes": "Unconfirmed notes",
        "document_id": identity, "token": information_token(identity, provider.record["information"]),
        "model_dirty": False}
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    original = compute.store.project_file.read_bytes()
    original_document = compute.service.load_document(identity).data
    provider.record = raw()
    receipt = compute.restore_recovery(checkpoint)
    assert not receipt["restored"] and receipt["optical_pending"]
    assert compute.input_draft_restore_status()["pending"]
    assert compute.has_pending_draft
    candidate = provider.pending_restore
    assert candidate is not None
    assert candidate["document_id"] is None and candidate["revision"] is None
    assert candidate["dirty"] and candidate["model_dirty"]
    assert candidate["pending_information"]["name"] == "Unconfirmed name"
    assert candidate["information"]["name"] == "Optical draft"
    assert candidate["pending_information"]["token"] == information_token(None, candidate["information"])
    assert provider.finish_restore()
    status = compute.input_draft_restore_status(receipt["optical_restore_token"])
    assert not status["pending"] and status["success"] and not status["error"]
    provider.record["pending_information"] = None  # Cancel edits, never write the model.
    assert provider.record["information"]["name"] == "Optical draft"
    assert compute.has_dirty_draft
    assert compute.store.project_file.read_bytes() == original
    assert compute.service.load_document(identity).data == original_document


def test_failed_restore_keeps_prior_input_and_detached_candidate(tmp_path):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    provider.record["state"]["request_text"] = "prior live draft"
    prior = deepcopy(provider.record)
    compute.restore_recovery(checkpoint)
    assert provider.finish_restore(success=False, error="input corrupted during prepare")
    assert provider.record == prior
    status = compute.input_draft_restore_status()
    assert not status["pending"] and not status["success"]
    assert compute.has_pending_draft
    assert compute._input_drafts.held["ray_trace"]["state"] == checkpoint["optical_inputs"][0]["state"]


@pytest.mark.parametrize("field,value", [
    ("module_key", "film"), ("kind", "appearance_configuration"),
    ("document_id", "bad"), ("revision", "bad"), ("token", "0" * 64),
    ("sha256", "0" * 64), ("host", {"password": "forbidden"}),
])
def test_recovery_strict_identity_token_checksum_unknown_fields(tmp_path, field, value):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    changed = deepcopy(checkpoint)
    changed["optical_inputs"][0][field] = value
    if field not in {"token", "sha256"}:
        resign(changed["optical_inputs"][0])
    before = deepcopy(provider.record)
    with pytest.raises(ProjectError):
        compute.restore_recovery(changed)
    assert provider.record == before and provider.restore_calls == []


def test_owned_asset_reference_roundtrip_excludes_native_host_path(tmp_path):
    compute = workspace(tmp_path / "project")
    source = asset(tmp_path / "external")
    document = compute.service.save_simulation_resource("raytrace_request", ray(source), name="Owned input")
    native = document.runtime["payload"]["deposition_asset"]
    provider = Provider(compute)
    provider.record["state"]["deposition_asset"] = deepcopy(native)
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    ref = checkpoint["optical_inputs"][0]["state"]["deposition_asset"]
    assert ref["path"].endswith(".casset") and "/" not in ref["path"]
    assert str(source) not in json.dumps(checkpoint["optical_inputs"])
    assert str(compute.root) not in json.dumps(checkpoint["optical_inputs"])
    compute.restore_recovery(checkpoint)
    assert provider.pending_restore is not None
    assert provider.pending_restore["state"]["deposition_asset"] == native
    provider.finish_restore()
    changed = deepcopy(checkpoint)
    changed["optical_inputs"][0]["state"]["deposition_asset"]["path"] = "../escape.casset"
    resign(changed["optical_inputs"][0])
    with pytest.raises(ProjectError):
        compute.validate_recovery(changed)


def test_external_asset_is_not_silently_journalled_or_copied(tmp_path):
    compute = workspace(tmp_path / "project")
    source = asset(tmp_path / "external")
    provider = Provider(compute)
    provider.record["state"]["deposition_asset"] = ray(source)["deposition_asset"]
    compute.register_input_draft_provider("ray_trace", provider)
    before = compute.store.manifest
    with pytest.raises(ProjectError) as error:
        compute.snapshot()
    assert error.value.code == "unowned_recovery_asset"
    assert compute.store.manifest == before
    assert not list((compute.root / "assets").rglob("*.coatasset"))


def test_optical_and_deposition_baselines_are_independent_and_legacy_is_optional(tmp_path):
    compute = workspace(tmp_path / "project")
    drafts = {"fields": {"grid_size": "31"}}
    compute.register_draft_provider(lambda: deepcopy(drafts))
    compute.mark_draft_saved()
    old_checkpoint = compute.snapshot()
    assert "optical_inputs" not in old_checkpoint
    provider = Provider(compute, "appearance")
    compute.register_input_draft_provider("appearance", provider)
    assert compute.has_dirty_draft
    assert not compute.has_dirty_deposition_draft
    request = compute.begin_input_draft_save("appearance")
    provider.finish_save(request)
    assert not compute.has_dirty_draft
    compute.validate_recovery(old_checkpoint)
    assert compute.restore_recovery(old_checkpoint)["restored"]


def test_duplicate_module_and_stale_saved_revision_fail_before_restore(tmp_path):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    duplicate = deepcopy(checkpoint)
    duplicate["optical_inputs"] *= 2
    with pytest.raises(ProjectError, match="duplicate"):
        compute.restore_recovery(duplicate)
    document = compute.service.save_simulation_resource("raytrace_request", ray())
    identity = Path(document.path).stem
    provider.record.update(document_id=identity, revision=compute.service._record(identity)["sha256"])
    saved = compute.snapshot()
    compute.service.save_simulation_resource("raytrace_request", ray(), document_id=identity,
        expected_revision=provider.record["revision"], name="Another writer")
    with pytest.raises(ProjectError) as error:
        compute.restore_recovery(saved)
    assert error.value.code == "stale_recovery" and not provider.restore_calls


def test_multi_module_failure_waits_for_rollback_and_preserves_each_prior_draft(tmp_path):
    compute = workspace(tmp_path / "project")
    ray_provider, appearance = Provider(compute), Provider(compute, "appearance")
    compute.register_input_draft_provider("ray_trace", ray_provider)
    compute.register_input_draft_provider("appearance", appearance)
    checkpoint = compute.snapshot()
    ray_provider.record["state"]["request_text"] = "prior ray draft"
    appearance.record["information"]["name"] = "Prior appearance draft"
    prior_ray, prior_appearance = deepcopy(ray_provider.record), deepcopy(appearance.record)
    receipt = compute.restore_recovery(checkpoint)
    assert receipt["optical_pending"]
    appearance.finish_restore()
    assert appearance.record["information"]["name"] == "Optical draft"
    ray_provider.finish_restore(success=False, error="ray input changed")
    assert compute.input_draft_restore_status()["pending"]  # Rollback is asynchronous too.
    assert appearance.record["operation"]["kind"] == "restore"
    with pytest.raises(ProjectError):
        compute.close_preflight()
    appearance.finish_restore()
    assert ray_provider.record == prior_ray
    assert appearance.record == prior_appearance
    status = compute.input_draft_restore_status()
    assert not status["pending"] and not status["success"] and "ray input changed" in status["error"]
    assert set(compute._input_drafts.held) == {"appearance", "ray_trace"}


def test_failed_provider_registration_does_not_retain_disposable_owner(tmp_path):
    compute = workspace(tmp_path / "project")
    original = Provider(compute)
    compute.register_input_draft_provider("ray_trace", original)
    compute.unregister_input_draft_provider("ray_trace", original)
    rejected = Provider(compute)
    rejected.fail_admission = True
    with pytest.raises(ProjectError) as error:
        compute.register_input_draft_provider("ray_trace", rejected)
    assert error.value.code == "restore_not_started"
    assert "ray_trace" not in compute._input_drafts.providers
    assert compute.has_pending_draft


def test_real_commit_failure_keeps_old_managed_file_and_draft(tmp_path, monkeypatch):
    compute = workspace(tmp_path / "project")
    document = compute.service.save_simulation_resource("raytrace_request", ray(), name="Original")
    identity = Path(document.path).stem
    provider = Provider(compute)
    provider.record.update(document_id=identity, revision=compute.service._record(identity)["sha256"])
    compute.register_input_draft_provider("ray_trace", provider)
    old_descriptor = compute.store.project_file.read_bytes()
    old_data = compute.service.load_document(identity).data
    request = compute.begin_input_draft_save("ray_trace")
    def fail_publish(*_args, **_kwargs):
        raise OSError("disk full before publication")
    monkeypatch.setattr(compute.store, "_publish", fail_publish)
    with pytest.raises(OSError) as error:
        compute.service.save_simulation_resource("raytrace_request", ray(), document_id=identity,
            expected_revision=provider.record["revision"], name="Unsaved replacement")
    provider.finish_save(request, success=False, error=str(error.value))
    assert compute.store.project_file.read_bytes() == old_descriptor
    assert compute.service.load_document(identity).data == old_data
    assert compute.has_dirty_draft
    assert not compute.input_draft_save_status("ray_trace", request)["close_ready"]


def test_late_completion_from_closed_generation_cannot_satisfy_barrier(tmp_path):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    request = compute.begin_input_draft_save("ray_trace")
    assert compute.host.close_project(compute.binding, prepare_close=lambda *_args: True)
    fresh = ProjectComputeWorkspace.for_project(compute.host, compute.host.open_project(compute.root).binding)
    assert not compute.complete_input_draft_save("ray_trace", provider, request, True)
    assert not fresh.has_dirty_draft and "optical_inputs" not in fresh.snapshot()


def test_project_close_review_includes_optical_draft_and_awaits_save_completion(tmp_path, monkeypatch):
    from coat_sim.ui import project_close_coordinator
    scheduled: list[Callable[[], None]] = []
    def single_shot(delay: int, callback: Callable[[], None]) -> None:
        assert delay == 0
        scheduled.append(callback)
    monkeypatch.setattr(project_close_coordinator, "QTimer", SimpleNamespace(singleShot=single_shot))
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    requested: list[str] = []
    adapter = SimpleNamespace(host=compute.host, desktop=SimpleNamespace(_views={}), _stopped=False,
        projectCloseConfirmationRequested=SimpleNamespace(emit=requested.append))
    close = project_close_coordinator.ProjectCloseCoordinator(adapter)
    project_id = compute.binding.project_id
    assert not close.request(project_id)
    assert requested == [project_id]
    assert close.is_pending(compute.binding) and compute.has_dirty_draft
    provider.record["information"]["notes"] = "A later input edit"
    with pytest.raises(ProjectError) as error:
        close.confirm(project_id)
    assert error.value.code == "stale_close"
    assert len(compute.host.sessions()) == 1
    assert not close.is_pending(compute.binding) and len(scheduled) == 1
    request = compute.begin_input_draft_save("ray_trace")
    assert not compute.input_draft_save_status("ray_trace", request)["done"]
    with pytest.raises(ProjectError) as error:
        close.request(project_id)
    assert error.value.code == "pending_operation"
    assert len(compute.host.sessions()) == 1 and compute.has_dirty_draft
    assert requested == [project_id] and not close.is_pending(compute.binding)
    assert provider.finish_save(request, success=False, error="Failed save")
    result = compute.input_draft_save_status("ray_trace", request)
    assert result["done"] and not result["success"] and not result["close_ready"]
    assert not close.request(project_id)
    assert requested == [project_id, project_id] and close.is_pending(compute.binding)
    scheduled.pop(0)()
    assert not scheduled and requested == [project_id, project_id]
    assert len(compute.host.sessions()) == 1 and compute.has_dirty_draft


def test_captured_unsaved_input_survives_source_deletion_recovery_and_first_save(tmp_path):
    compute = workspace(tmp_path / "project")
    source = asset(tmp_path / "external")
    captured = compute.service.capture_simulation_asset("deposition_asset", source)
    source.unlink()
    assert [row["kind"] for row in compute.store.manifest["documents"]] == ["binary_asset"]
    provider = Provider(compute)
    provider.record["state"]["deposition_asset"] = {"path": captured["path"], "sha256": captured["sha256"]}
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    assert checkpoint["optical_inputs"][0]["document_id"] is None
    assert checkpoint["optical_inputs"][0]["state"]["deposition_asset"]["path"] == captured["document_id"] + ".casset"
    provider.record = raw()
    compute.restore_recovery(checkpoint)
    assert provider.finish_restore()
    request = compute.begin_input_draft_save("ray_trace")
    document = compute.service.save_simulation_resource("raytrace_request", {
        **ray(), "request": json.loads(provider.record["state"]["request_text"]),
        "deposition_asset": provider.record["state"]["deposition_asset"]}, name="First saved recovered copy")
    identity = Path(document.path).stem
    provider.record.update(document_id=identity, revision=compute.service._record(identity)["sha256"])
    provider.finish_save(request)
    assert compute.input_draft_save_status("ray_trace", request)["close_ready"]
    assert len(compute.service.list_documents("raytrace_request")) == 1
    assert len([row for row in compute.store.manifest["documents"] if row["kind"] == "binary_asset"]) == 1
    assert Path(document.runtime["payload"]["deposition_asset"]["path"]).exists()


def test_appearance_recovery_preserves_incomplete_numeric_text_and_owned_spectrum(tmp_path):
    from coat_sim.core.project_simulation_resources import BOOL_DRAFT_FIELDS, NUMERIC_DRAFT_FIELDS
    from tests.test_portable_asset_bundle import fixture_snapshot
    compute = workspace(tmp_path / "project")
    source = asset(tmp_path / "external")
    captured = compute.service.capture_simulation_asset("deposition_asset", source)
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    spd = compute.service.capture_simulation_asset("spd_csv", spectrum)
    source.unlink()
    spectrum.unlink()
    fixture = fixture_snapshot()
    process, substrate = fixture["files"][0]["data"], fixture["files"][-1]["data"]
    provider = Provider(compute, "appearance")
    draft: dict[str, Any] = {field: "1" for field in NUMERIC_DRAFT_FIELDS}
    draft.update({field: False for field in BOOL_DRAFT_FIELDS})
    draft.update(area_kind="relative", environment_kind="relative", environment_spectrum=None,
                 area_spectrum={"path": spd["path"], "wavelength_nm": [500.0, 600.0], "value": [1.0, 2.0]})
    draft["camera_x"] = "-"
    provider.record["state"].update(draft=draft, context={
        "schema": "coat_sim.appearance_launch_context", "version": 1,
        "asset_path": captured["path"], "asset_root": str(compute.root),
        "asset_sha256": captured["sha256"], "process_profile": process,
        "process_profile_sha256": digest(process), "substrate_profile": substrate,
        "captured_step_ids": [], "layer_field_units": "nm", "layer_field_coordinate_frame": "substrate_local"})
    compute.register_input_draft_provider("appearance", provider)
    checkpoint = compute.snapshot()
    persisted = checkpoint["optical_inputs"][0]["state"]
    assert persisted["context"]["asset_root"] == "."
    assert persisted["draft"]["camera_x"] == "-"
    assert persisted["draft"]["area_spectrum"]["sha256"] == spd["sha256"]
    compute.restore_recovery(checkpoint)
    assert provider.pending_restore is not None
    assert provider.pending_restore["state"]["draft"]["camera_x"] == "-"
    assert provider.pending_restore["state"]["draft"]["area_spectrum"]["path"] == spd["path"]
    provider.finish_restore()
    assert provider.record["dirty"] and provider.record["document_id"] is None


def test_close_freeze_prevents_new_draft_and_save_admission(tmp_path):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    compute.freeze_editing(True)
    with pytest.raises(ProjectComputeError) as error:
        compute.begin_input_draft_save("ray_trace")
    assert error.value.code == "editing_frozen"
    with pytest.raises(ProjectComputeError) as error:
        compute.register_input_draft_provider("appearance", Provider(compute, "appearance"))
    assert error.value.code == "editing_frozen"
    assert provider.record["operation"] is None
    compute.freeze_editing(False)


def test_restore_status_token_cannot_be_replaced_by_later_held_window_restore(tmp_path):
    compute = workspace(tmp_path / "project")
    provider = Provider(compute)
    compute.register_input_draft_provider("ray_trace", provider)
    checkpoint = compute.snapshot()
    receipt = compute.restore_recovery(checkpoint)
    old_request = provider.record["operation"]["request_id"]
    provider.finish_restore()
    compute.unregister_input_draft_provider("ray_trace", provider)
    replacement = Provider(compute)
    compute.register_input_draft_provider("ray_trace", replacement)
    assert compute.input_draft_restore_status()["pending"]
    original = compute.input_draft_restore_status(receipt["optical_restore_token"])
    assert not original["pending"] and original["success"]
    assert not compute.complete_input_draft_restore("ray_trace", provider, old_request, True)
    replacement.finish_restore()


@pytest.mark.parametrize("change", ["text", "information", "operation", "detach"])
def test_late_other_module_failure_keeps_newer_successful_module_state(tmp_path, change):
    compute = workspace(tmp_path / "project")
    first, other = Provider(compute), Provider(compute, "appearance")
    compute.register_input_draft_provider("ray_trace", first)
    compute.register_input_draft_provider("appearance", other)
    checkpoint = compute.snapshot()
    first.record["state"]["request_text"] = "PRIOR LIVE RAY"
    receipt = compute.restore_recovery(checkpoint)
    first.finish_restore()
    if change in {"text", "detach"}:
        first.record["state"]["request_text"] = "NEW USER EDIT AFTER FIRST RESTORE"
    elif change == "information":
        first.record["pending_information"] = {
            "name": "New unconfirmed information", "summary": "", "notes": "",
            "document_id": None, "token": information_token(None, first.record["information"]),
            "model_dirty": True}
    else:
        # A newer operation completed with exactly the same input content.
        first._latest_request_id = uuid4().hex
    expected = deepcopy(first.record)
    if change == "detach":
        assert compute.unregister_input_draft_provider("ray_trace", first)
        first.destroyed = True
    other.finish_restore(success=False, error="Other module late failure")
    status = compute.input_draft_restore_status(receipt["optical_restore_token"])
    assert not status["pending"] and not status["success"]
    assert "partial recovery" in status["error"]
    assert len(first.restore_calls) == 1
    if change == "detach":
        assert compute._input_drafts.held["ray_trace"] == expected
    else:
        assert first.record == expected


def test_late_rollback_completion_cannot_overwrite_newer_input(tmp_path):
    compute = workspace(tmp_path / "project")
    first, other = Provider(compute), Provider(compute, "appearance")
    compute.register_input_draft_provider("ray_trace", first)
    compute.register_input_draft_provider("appearance", other)
    checkpoint = compute.snapshot()
    first.record["state"]["request_text"] = "PRIOR LIVE RAY"
    receipt = compute.restore_recovery(checkpoint)
    first.finish_restore()
    other.finish_restore(success=False, error="Other module failed")
    rollback_request = first._restore_request_id
    assert first.record["operation"]["request_id"] == rollback_request
    first.record["state"]["request_text"] = "NEW USER EDIT DURING ROLLBACK"
    assert not compute.input_draft_restore_is_current("ray_trace", first, rollback_request)
    first.finish_restore()
    assert first.record["state"]["request_text"] == "NEW USER EDIT DURING ROLLBACK"
    assert first.record["operation"] is None
    status = compute.input_draft_restore_status(receipt["optical_restore_token"])
    assert not status["pending"] and not status["success"]
    assert "stale" in status["error"]
    assert not compute.complete_input_draft_restore("ray_trace", first, rollback_request, True)


def test_lazy_provider_registration_waits_for_batch_without_joining_its_rollback(tmp_path):
    compute = workspace(tmp_path / "project")
    first, original = Provider(compute), Provider(compute, "appearance")
    compute.register_input_draft_provider("ray_trace", first)
    compute.register_input_draft_provider("appearance", original)
    checkpoint = compute.snapshot()
    assert compute.unregister_input_draft_provider("appearance", original)
    receipt = compute.restore_recovery(checkpoint)
    replacement = Provider(compute, "appearance")
    with pytest.raises(ProjectError) as error:
        compute.register_input_draft_provider("appearance", replacement)
    assert error.value.code == "pending_operation"
    assert "appearance" not in compute._input_drafts.providers
    first.finish_restore(success=False, error="Ray prepare failed")
    status = compute.input_draft_restore_status(receipt["optical_restore_token"])
    assert not status["pending"] and not status["success"]
    compute.register_input_draft_provider("appearance", replacement)
    assert replacement.finish_restore()
    assert replacement.record["dirty"] and replacement.record["document_id"] is None
