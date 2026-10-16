"""Headless project compute ownership, fencing, recovery and path contracts."""
from copy import deepcopy
from hashlib import sha256
import json
import os

import pytest

from coat_sim.core.project_compute import ProjectComputeError, ProjectComputeWorkspace
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.project_session import ProjectSessionError, ProjectSessionHost
from tests.test_project_core import setup_project

pytestmark = pytest.mark.contract


def compute_for(store):
    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    return ProjectComputeWorkspace.for_project(host, binding)


def test_workspace_reuses_one_project_service_and_resolves_default_process_snapshot(tmp_path):
    store, _external, process_id, configuration_id = setup_project(tmp_path)
    compute = compute_for(store)
    assert ProjectComputeWorkspace.for_project(compute.host, compute.binding) is compute
    assert compute.service.root == store.root
    assert compute.service.restrict_to_root is True
    assert compute.service.store is store

    plan = compute.load_default()
    assert plan.configured and plan.configuration_id == configuration_id
    assert plan.process_document_id == process_id
    rows = compute.configurations()
    assert len(rows) == 1
    assert rows[0]["id"] == configuration_id and rows[0]["default"]
    selected = compute.load_configuration(configuration_id)
    assert selected["input_snapshot"]["kind"] == "coat_sim.input_snapshot"
    assert selected["input_snapshot"] == store.resolve_document(process_id)["input_snapshot"]
    assert selected["configuration"]["parameters"]["settings"]["grid_size"] == 21
    process_handle = compute.process_handle(process_id)
    expected_directory = compute.service.default_directory("process")
    assert process_handle == compute.service.document_handle(process_id)
    assert process_handle.parent == expected_directory
    assert process_handle.name == f"{process_id}.cproc"
    assert process_handle.suffix == ".cproc"
    assert process_handle.is_relative_to(store.root)
    assert not process_handle.exists()  # Logical chooser handle; bytes live under objects/<generation>.


def test_project_config_save_updates_stable_identity_and_detects_stale_editor(tmp_path):
    store, _external, process_id, configuration_id = setup_project(tmp_path)
    compute = compute_for(store)
    loaded = compute.load_configuration(configuration_id)
    edited = deepcopy(loaded["configuration"]["parameters"])
    edited["settings"]["grid_size"] = 31
    updated = compute.save_configuration(
        process_id, edited, "Edited", configuration_id=configuration_id, make_default=True)
    assert updated == configuration_id
    assert store.manifest["default_configuration_id"] == configuration_id
    assert compute.load_configuration(configuration_id)["configuration"]["name"] == "Edited"

    stale = compute.load_configuration(configuration_id)
    changed = deepcopy(stale["configuration"])
    changed["name"] = "External writer"
    store.save_document(configuration_id, changed)
    with pytest.raises(ProjectError) as error:
        compute.save_configuration(process_id, edited, "Stale", configuration_id=configuration_id)
    assert error.value.code == "stale_document"


def test_project_import_and_content_addressed_assets_never_escape_or_overwrite(tmp_path):
    store, external, _process, _configuration = setup_project(tmp_path)
    compute = compute_for(store)
    imported_process = compute.import_process(external / "recipe.cproc")
    assert imported_process in {row["id"] for row in store.manifest["documents"]}
    assert compute.process_document_id == imported_process
    assert compute.resolve_process(imported_process)["input_snapshot"]["kind"] == "coat_sim.input_snapshot"

    asset = tmp_path / "palette.bin"
    asset.write_bytes(b"palette bytes")
    first = compute.import_asset(asset, "appearance")
    second = compute.import_asset(asset, "appearance")
    assert first == second and first.read_bytes() == asset.read_bytes()
    assert first.is_relative_to(store.root / "data" / "imported" / "appearance")
    assert compute.import_asset(first, "appearance") == first

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ProjectComputeError, match="outside"):
        compute.owned_path(outside / "export.pdf", allow_missing=True)
    with pytest.raises(ProjectComputeError, match="outside"):
        compute.destination(outside / "export.pdf")
    if hasattr(os, "symlink"):
        link = store.root / "linked-data"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("filesystem does not support symlinks")
        with pytest.raises(ProjectComputeError, match="links"):
            compute.owned_path(link / "file.bin", allow_missing=True)


def test_generation_fencing_provenance_freeze_jobs_and_thaw(tmp_path):
    store, _external, _process_id, _configuration_id = setup_project(tmp_path)
    compute = compute_for(store)
    compute.load_default()
    op = compute.capture("calculate-1")
    metadata = compute.metadata(op, {"summary": "recipe run", "origin": {"surface": "desktop"}})
    assert metadata["origin"] == {
        "surface": "desktop", "project_id": compute.binding.project_id,
        "project_generation": compute.binding.generation,
        "project_root": str(store.root), "configuration_id": compute.configuration_id,
        "operation_id": "calculate-1",
    }
    with pytest.raises(ValueError, match="cannot override"):
        compute.metadata(op, {"origin": {"project_id": "forged"}})

    drafts = {"values": {"grid_size": "31"}}
    compute.register_draft_provider(lambda: deepcopy(drafts))
    signature = compute.draft_signature()
    drafts["values"]["grid_size"] = "41"
    assert compute.draft_signature() != signature
    thawed = []
    compute.register_thaw_callback(lambda: thawed.append(True))
    compute.register_job("job-1", state="running", metadata=metadata)
    assert compute.active_jobs()[0]["job_id"] == "job-1"
    assert compute.update_job("job-1", "running", service_owned=False)
    assert compute.active_jobs()[0]["service_owned"] is False
    assert compute.update_job("job-1", "running", service_owned=True)
    compute.freeze_editing(True)
    assert not compute.accepts(op)
    with pytest.raises(ProjectComputeError, match="frozen"):
        compute.capture("blocked")
    with pytest.raises(ProjectComputeError, match="frozen"):
        compute.import_asset(store.root / "missing")
    compute.freeze_editing(False)
    assert compute.accepts(op) and thawed == [True]
    compute.update_job("job-1", "succeeded")
    assert compute.active_jobs() == []

    assert compute.host.close_project(compute.binding,
                                      prepare_close=lambda _binding, _routes: True)
    reopened = compute.host.open_project(store.root)
    fresh = ProjectComputeWorkspace.for_project(compute.host, reopened.binding)
    assert fresh.binding.generation != compute.binding.generation
    with pytest.raises(ProjectSessionError):
        compute.capture("old-wrapper")
    assert not fresh.accepts(op)


def test_recovery_restores_draft_and_appearance_context_without_resubmitting_jobs(tmp_path):
    store, _external, process_id, configuration_id = setup_project(tmp_path)
    compute = compute_for(store)
    compute.load_configuration(configuration_id)
    drafts = {"fields": {"grid_size": "31"}, "device": "CPU",
              "algorithm": "fast", "algorithm_budget_s": 0,
              "fast_cycle": True, "fast_cycle_residual_percent": 5.0,
              "requested_time_step_s": "0.025", "time_basis": "time_step_s"}
    compute.register_draft_provider(lambda: deepcopy(drafts))
    asset = store.root / "data" / "imported" / "appearance" / "bundle.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"frozen asset")
    process_profile = {"kind": "process"}
    process_digest = sha256(json.dumps(process_profile, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    appearance = {
        "schema": "coat_sim.appearance_launch_context", "version": 1,
        "asset_path": str(asset), "asset_root": str(store.root),
        "asset_sha256": sha256(asset.read_bytes()).hexdigest(),
        "process_profile": process_profile,
        "process_profile_sha256": process_digest,
        "substrate_profile": {"outline": {"kind": "rectangle"}},
    }
    compute.set_appearance_context(appearance)
    compute.register_job("known-job", module_key="ray_trace", state="succeeded",
                         metadata={"run_id": "known-run"})
    recovery = compute.snapshot()
    assert recovery["draft"] == drafts
    assert recovery["job_references"][0]["job_id"] == "known-job"

    assert compute.host.close_project(compute.binding,
                                      prepare_close=lambda _binding, _routes: True)
    reopened = compute.host.open_project(store.root)
    restored = ProjectComputeWorkspace.for_project(compute.host, reopened.binding)
    recovered_drafts: list[dict] = []
    restored.register_draft_provider(lambda: {}, restore=recovered_drafts.append)
    restored.validate_recovery(recovery)
    assert restored.configuration_id is None and restored.process_document_id is None
    invalid_recovery = deepcopy(recovery)
    invalid_recovery["draft"]["algorithm_budget_s"] = 301
    with pytest.raises(ProjectComputeError, match="budget"):
        restored.validate_recovery(invalid_recovery)
    assert restored.configuration_id is None and recovered_drafts == []
    receipt = restored.restore_recovery(recovery)
    assert receipt["jobs_resubmitted"] is False
    assert receipt["job_references"] == 1
    assert restored.configuration_id == configuration_id
    assert restored.process_document_id == process_id
    assert restored.appearance_context() == appearance
    assert recovered_drafts == [drafts]
    assert restored.active_jobs() == []
    assert restored.job_records() == []


def test_recovery_rejects_wrong_project_and_stale_resource_ids(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first_store, _external, _process_id, configuration_id = setup_project(tmp_path / "a")
    second_store, *_ = setup_project(tmp_path / "b")
    first = compute_for(first_store)
    first.load_default()
    recovery = first.snapshot()
    second = compute_for(second_store)
    with pytest.raises(ProjectComputeError, match="another project"):
        second.restore_recovery(recovery)
    invalid = deepcopy(recovery)
    invalid["configuration_id"] = "not-a-project-configuration"
    invalid["project_id"] = second.binding.project_id
    invalid["project_root"] = str(second.root)
    with pytest.raises(ProjectComputeError, match="no longer present"):
        second.restore_recovery(invalid)


def test_recovery_restorer_failure_rolls_back_compute_and_mutated_draft(tmp_path):
    store, *_ = setup_project(tmp_path)
    compute = compute_for(store)
    draft = {"fields": {"carrier_rpm": "25"}}
    def restore(value):
        draft.clear()
        draft.update(deepcopy(value))
        if value["fields"]["carrier_rpm"] == "99":
            raise RuntimeError("presentation rejected recovery")
    compute.register_draft_provider(lambda: deepcopy(draft), restore=restore)
    before = compute.snapshot()
    recovery = deepcopy(before)
    recovery["draft"]["fields"]["carrier_rpm"] = "99"
    recovery["job_references"] = [{"job_id": "historical", "module_key": "main", "state": "succeeded"}]
    with pytest.raises(RuntimeError, match="presentation rejected"):
        compute.restore_recovery(recovery)
    assert compute.snapshot() == before
    assert not compute.recovery_restored
