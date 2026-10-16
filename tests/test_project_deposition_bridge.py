"""Project-bound MainBridge behavior, with offscreen Qt but no real compute."""
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QUrl

from coat_sim.core.project_compute import ProjectComputeWorkspace
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.main import MainBridge
from coat_sim.ui.project_simulation_resources import ProjectDepositionResources
from tests.test_project_core import setup_project

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class NeverCancelled:
    def is_set(self):
        return False


def bridge_for(store, *, result_view_only=False):
    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    film_workspace = ProjectFilmWorkspace.for_project(host, binding)
    compute = ProjectComputeWorkspace.for_project(host, binding)
    bridge = MainBridge(compute.service, project_context=film_workspace,
                        result_view_only=result_view_only)
    return bridge, film_workspace, compute


def test_project_bridge_loads_only_managed_default_and_roots_artifacts(tmp_path):
    store, _external, process_id, configuration_id = setup_project(tmp_path)
    bridge, film_workspace, compute = bridge_for(store)
    assert bridge.project_context is film_workspace
    assert bridge.service is compute.service
    assert bridge.projectMode and not bridge.resultViewOnly
    assert compute.configuration_id == configuration_id
    assert compute.process_document_id == process_id
    assert int(bridge.fields["grid_size"]) == 21
    assert bridge.paths["process"] == str(compute.process_handle(process_id))

    snapshot = bridge._simulation_args()["process_path"]
    assert snapshot["kind"] == "coat_sim.input_snapshot"
    assert snapshot == store.resolve_document(process_id)["input_snapshot"]
    project_root = Path(store.root).resolve()
    for url in (bridge.processDirectory, bridge.resultsDirectory,
                bridge.assetsDirectory, bridge.pdfSuggestedUrl):
        assert Path(url.toLocalFile()).resolve().is_relative_to(project_root)
    bridge.detach_observers()


def test_process_picker_imports_into_project_and_save_load_configuration_roundtrips(tmp_path):
    store, external, old_process_id, old_configuration_id = setup_project(tmp_path)
    bridge, _film, compute = bridge_for(store)
    original_process_path = bridge.paths["process"]
    assert bridge.loadConfiguration(old_configuration_id)
    bridge.setField("grid_size", "31")
    bridge.saveConfiguration("Project local", True)
    first = compute.configuration_id
    assert store.manifest["default_configuration_id"] == first
    assert compute.configurations()[0]["id"] == first

    bridge.chooseFile("process", QUrl.fromLocalFile(str(external / "recipe.cproc")))
    imported = compute.process_document_id
    assert imported and imported != old_process_id
    assert bridge.paths["process"] == str(compute.process_handle(imported))
    assert bridge._simulation_args()["process_path"] == store.resolve_document(imported)["input_snapshot"]
    assert bridge.saveConfiguration("Imported process", True)
    second = compute.configuration_id
    assert second != first and store.manifest["default_configuration_id"] == second
    assert bridge.loadConfiguration(first)
    assert compute.process_document_id == old_process_id
    assert int(bridge.fields["grid_size"]) == 31
    assert original_process_path
    bridge.detach_observers()


def test_ordinary_configuration_save_owns_fresh_session_not_unsaved_later_input(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    second = store.add_deposition_configuration("Second", process_id, {"settings": {"grid_size": 41}})
    bridge, _film, compute = bridge_for(store)
    try:
        assert compute.configuration_id == second and int(bridge.fields["grid_size"]) == 41
        assert bridge.loadConfiguration(first)
        bridge.setField("grid_size", "31")
        # The ordinary UI path historically passes False, and must still pick
        # this successful save without a separate default-selection action.
        assert bridge.saveConfiguration("Last ordinary save", False)
        assert store.manifest["default_configuration_id"] == first
        bridge.setField("grid_size", "99")  # Unsaved state never chooses opening.
    finally:
        bridge.detach_observers()
    reopened, _new_film, new_compute = bridge_for(ProjectStore.open(store.project_file))
    try:
        assert new_compute.configuration_id == first
        assert int(reopened.fields["grid_size"]) == 31
        assert new_compute.process_document_id == process_id
    finally:
        reopened.detach_observers()


def test_resource_toolbar_save_copy_becomes_next_opening_configuration(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    bridge, film, compute = bridge_for(store)
    resources = ProjectDepositionResources(film, bridge)
    try:
        bridge.setField("grid_size", "37")
        assert resources.saveCopyCurrent()
        copied = compute.configuration_id
        assert copied != first and store.manifest["default_configuration_id"] == copied
        assert store.resolve_document(first)["configuration"]["parameters"]["settings"]["grid_size"] == 21
    finally:
        resources.detach()
        bridge.detach_observers()
    reopened, _new_film, new_compute = bridge_for(ProjectStore.open(store.project_file))
    try:
        assert new_compute.configuration_id == copied
        assert new_compute.process_document_id == process_id
        assert int(reopened.fields["grid_size"]) == 37
    finally:
        reopened.detach_observers()


def test_stale_project_completion_cannot_replace_an_edited_result(tmp_path):
    store, _external, _process_id, _configuration_id = setup_project(tmp_path)
    bridge, _film, compute = bridge_for(store)
    old_result = {"field_nm": np.array([[4.0]]), "mean_nm": 4.0}
    bridge._result = old_result
    bridge._job_id = 1
    bridge._revision = 9
    bridge._started_revision = 8
    operation = compute.capture("late-calculation")
    queue_id = "late-job"
    bridge._project_operations[1] = operation
    bridge._project_queue_ids[1] = queue_id
    bridge._job_cancel_events[1] = NeverCancelled()
    bridge._queued_keys[1] = "job-key"
    bridge._busy = True
    bridge._pending_export_inputs = {"process": {"name": "frozen process"}}
    compute.register_job(queue_id, state="running")
    new_result = {"x": [0.0], "field_nm": np.array([[99.0]]), "step_results": []}

    bridge._finish(1, new_result, 2.0, None)
    assert bridge._result is old_result
    assert bridge.status.startswith("计算已完成，但本次提交后输入已更改")
    assert bridge._pending_export_inputs is None
    assert bridge._busy is False
    assert compute.job_records()[0]["state"] == "stale"
    bridge.detach_observers()


def test_completion_received_during_close_freeze_is_deferred_until_thaw(tmp_path):
    store, _external, _process_id, _configuration_id = setup_project(tmp_path)
    bridge, _film, compute = bridge_for(store)
    bridge._job_id = 1
    bridge._revision = 2
    bridge._started_revision = 2
    operation = compute.capture("frozen-calculation")
    queue_id = "frozen-job"
    bridge._project_operations[1] = operation
    bridge._project_queue_ids[1] = queue_id
    bridge._job_cancel_events[1] = NeverCancelled()
    bridge._queued_keys[1] = "job-key"
    bridge._busy = True
    bridge._pending_export_inputs = None
    prior = bridge._result
    compute.register_job(queue_id, state="running")
    result = {"x": [0.0, 1.0], "field_nm": np.array([[1.0, 2.0], [3.0, 4.0]]),
              "step_results": [], "algorithm_report": {}, "device": "CPU",
              "algorithm": "fast_cycle"}

    compute.freeze_editing(True)
    bridge._finish(1, result, 1.0, None)
    assert bridge._result is prior
    assert bridge._busy
    assert 1 in bridge._deferred_project_finishes

    compute.freeze_editing(False)
    QCoreApplication.processEvents()
    assert bridge._result is result
    assert bridge._busy is False
    assert bridge._deferred_project_finishes == {}
    bridge.detach_observers()


def test_result_view_bridge_does_not_claim_or_rewrite_project_deposition_draft(tmp_path):
    store, _external, process_id, configuration_id = setup_project(tmp_path)
    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    film = ProjectFilmWorkspace.for_project(host, binding)
    compute = ProjectComputeWorkspace.for_project(host, binding)
    compute.load_default()
    before = (compute.configuration_id, compute.process_document_id)
    viewer = MainBridge(compute.service, project_context=film, result_view_only=True)
    assert viewer.resultViewOnly and viewer.projectMode
    assert (compute.configuration_id, compute.process_document_id) == before
    assert compute._draft_provider is None
    assert viewer.loadDefaultConfiguration() is False
    assert viewer.saveConfiguration("Should not write") is False
    assert store.manifest["default_configuration_id"] == configuration_id
    assert compute.process_document_id == process_id
    viewer.detach_observers()


def test_deposition_dirty_draft_survives_view_close_and_clears_only_after_save(tmp_path):
    store, *_ = setup_project(tmp_path)
    bridge, film, compute = bridge_for(store)
    assert not compute.has_dirty_draft
    bridge.setField("grid_size", "37")
    assert compute.has_dirty_draft
    signature = compute.draft_signature()
    bridge.detach_observers()
    assert compute.has_pending_draft and compute.has_dirty_draft
    assert compute.draft_signature() == signature
    reopened = MainBridge(compute.service, project_context=film)
    try:
        assert cast(dict[str, str], reopened.fields)["grid_size"] == "37"
        assert compute.has_dirty_draft and not compute.has_pending_draft
        assert reopened.saveConfiguration("Saved reopened draft")
        assert not compute.has_dirty_draft
    finally:
        reopened.detach_observers()


def test_pending_recovery_applies_only_after_main_fields_are_initialized(tmp_path):
    store, *_ = setup_project(tmp_path)
    bridge, film, compute = bridge_for(store)
    recovery = compute.snapshot()
    recovery["draft"]["fields"]["grid_size"] = "41"
    bridge.detach_observers()
    compute.restore_recovery(recovery)
    assert compute.has_pending_draft
    restored = MainBridge(compute.service, project_context=film)
    try:
        assert cast(dict[str, str], restored.fields)["grid_size"] == "41"
        assert compute.has_dirty_draft
    finally:
        restored.detach_observers()
