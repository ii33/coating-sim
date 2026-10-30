"""Default-hybrid GUI requests execute real stdio workers without a scheduler.

The bounded cancellation is a user interruption, not a convergence assertion.
Physics, production parallel-work admission, and transport remain unmodified.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import pickle
import threading

import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_auto_synthesis import _proxy, freeze_film_synthesis_request
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, deserialize_job_payload, execute_job,
)
from coat_sim.core.film_synthesis_parallel import MIN_PARALLEL_WORK
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.scheduler.jobs import JobSpec
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tools.run_film_synthesis_acceptance import atomic_json, synthetic_service

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def test_default_hybrid_gui_typed_job_runs_two_real_workers_then_cancels(
        app, tmp_path, monkeypatch):
    _unused_service, _environment, optical_rows = synthetic_service()
    match = deepcopy(next(row for row in optical_rows if row["id"] == "match"))
    for index in range(1, 4):
        extra = deepcopy(match)
        extra.update(id=f"extra-{index}", name=f"synthetic extra {index}")
        optical_rows.append(extra)
    library = MaterialLibrary(tmp_path)
    for optical in optical_rows:
        record = default_material_profile("Hybrid GUI " + optical["id"], id=optical["id"])
        record.update(category="substrate" if optical["id"] == "glass" else "film",
                      optical_constants=deepcopy(optical["optical_sets"]))
        library.write(record)
    service = FilmDesignService(tmp_path)
    design = service.new_design()
    design["substrate"].update(material_id="glass", optical_set_id="nk")
    original = deepcopy(design)

    with optimizer(service, design) as (controller, source, session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        assert controller.setSearchMode("synthesis")
        # Do not select a strategy: this must exercise the real GUI default.
        selected = {"match", "low", "high", "extra-1", "extra-2", "extra-3"}
        for row in controller.structureMaterialOptions:
            if row["material_id"] in selected and row["optical_set_id"] == "nk":
                assert controller.setStructureMaterialSelected(
                    row["material_id"], row["optical_set_id"], True)
                selected.remove(row["material_id"])
        assert not selected
        for field, value in (
            ("wavelength_min_nm", 450), ("wavelength_max_nm", 900),
            ("wavelength_samples", 4001), ("angle_max_deg", 60),
            ("angle_step_deg", 30), ("pointwise.upper_bound_percent", 100),
            ("pointwise.lower_bound_percent", 99.9999),
        ):
            assert controller.setObjectiveField(field, value)
        assert controller.setSynthesisCpuWorkers(2)
        assert controller.setSynthesisOption("parallel_batch_size", 4)
        assert controller.setSynthesisOption("max_evaluations", 32)
        assert controller.setSynthesisOption("max_seconds", 60)
        assert controller.setSynthesisOption("max_cycles", 1)
        assert controller.canStart and not controller._active_configuration_error()
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert request.search_strategy == "hybrid"
        assert request.cpu_workers == 2 and request.parallel_batch_size == 4
        assert request.objectives[0].wavelength_samples == 4001
        assert len(request.objectives[0].angles_deg) == 3
        assert len(request.candidate_materials) == 6
        envelope = film_synthesis_request_to_payload("default-hybrid-gui-real", request)
        job = build_job_payload(canonical_parameters(None, {"payload": envelope}))
        spec = JobSpec("film_synthesis", {"payload": job}, {})
        frozen_spec = JobSpec.parse_frozen(json.loads(json.dumps(spec.to_dict())))
        assert frozen_spec.fingerprint == spec.fingerprint
        identity_before = frozen_spec.canonical_json
        request_id, queued, metadata = deserialize_job_payload(frozen_spec.parameters["payload"])
        assert request_id == "default-hybrid-gui-real" and metadata == []
        assert film_synthesis_request_to_payload(request_id, queued) == envelope
        assert queued.cpu_workers == 2 and queued.parallel_batch_size == 4
        assert queued.selected_surfaces == ("front",)
        # Worker-pool identity includes its canonical physical-side selection;
        # that internal binding is distinct from the frozen external job identity above.
        expected_identity = hashlib.sha256(
            pickle.dumps((_proxy(queued), queued.selected_surfaces),
                         protocol=pickle.HIGHEST_PROTOCOL)).hexdigest()
        cancellation = threading.Event()
        reports = []

        def report(value, **_kwargs):
            reports.append(deepcopy(value))
            if value.get("stage") == "structure_proposal":
                # Hybrid eagerly finishes the independent physical source batch
                # before serial adoption emits this progress boundary.
                cancellation.set()

        payload = execute_job(frozen_spec.parameters, cancellation, report,
                              tmp_path / "checkpoint.json", atomic_json, str(tmp_path))
        result = payload["result"]
        perf = result["performance"]
        assert cancellation.is_set()
        assert any(row.get("stage") == "structure_proposal" for row in reports)
        assert result["state"] == result["termination_reason"] == "cancelled"
        assert result["targets_met"] is False
        assert result["validation_complete"] is False
        assert result["validation_evaluations"] == result["audit_physical_evaluations"] == 0
        assert result["evaluations"] <= 32
        assert perf["backend"] == "headless_process"
        assert perf["requested_workers"] == perf["effective_workers"] == 2
        assert len(set(perf["worker_pids"])) == 2
        assert perf["worker_training_physical_evaluations"] > 0
        assert perf["worker_cpu_seconds"] > 0
        assert perf["cleanup_complete"] is True and perf["active_worker_pids"] == []
        assert perf["frozen_request_identity"] == expected_identity
        assert all(row["frozen_request_identity"] == expected_identity for row in perf["workers"])
        assert all(set(row["thread_environment"].values()) == {"1"} for row in perf["workers"])
        assert perf["submitted_tasks"] == perf["completed_tasks"] + perf["unknown_tasks"]
        assert perf["completed_tasks"] == perf["consumed_tasks"] + perf["discarded_tasks"]
        assert perf["unknown_tasks"] == 0
        assert any(row["phase"] == "hybrid_prior" and row["parallel_selected"]
                   and row["candidate_count"] == 4
                   and row["estimated_work"] >= MIN_PARALLEL_WORK
                   and row["effective_workers"] == 2 for row in perf["batch_decisions"])
        hybrid = perf["search"]["hybrid"]
        assert hybrid["frozen_parallel_batch_size"] == 4
        assert hybrid["stages"][0]["admitted_evaluation_quantum"] >= 5
        batches = hybrid["parallel_batches"]
        assert len(batches) == 1 and batches[0]["reserved_slots"] == 4
        for batch in batches:
            # Physical consumption and proposal adoption are distinct. Do not
            # call eager results unperformed merely because cancellation follows.
            assert batch["reserved_slots"] == (batch["consumed_slots"]
                + batch["dispatched_unused_slots"] + batch["released_unsubmitted_slots"])
            assert batch["completed_results"] == batch["consumed_slots"]
        assert frozen_spec.canonical_json == identity_before
        assert JobSpec.parse_frozen(frozen_spec.to_spec().to_dict()).fingerprint == spec.fingerprint
        assert session.snapshot().design == original and session.snapshot().design_revision == 19
        assert source.applied == []

        controller._synthesis_payload = payload
        summary = controller.synthesisExecutionSummary
        assert "实际工作进程数：2" in summary and "记录的 PID 数：2" in summary
        assert "冻结逻辑批次：4" in summary and "阶段准入并发上限：2" in summary
        assert "实际工作进程数：4" not in summary and "实际工作进程数：19" not in summary
        with window_runtime(app, controller) as (window, warnings):
            display = window.findChild(QObject, "filmSynthesisExecutionSummary")
            assert display is not None and display.property("text") == summary
            assert display.metaObject().indexOfProperty("readOnly") == -1  # Label, not an editor.
            assert session.snapshot().design == original and source.applied == []
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings
        print("default-hybrid GUI execution evidence:", json.dumps({
            "fingerprint": spec.fingerprint, "frozen_request_identity": expected_identity,
            "state": result["state"], "termination_reason": result["termination_reason"],
            "targets_met": result["targets_met"], "logical_evaluations": result["evaluations"],
            "requested_workers": perf["requested_workers"], "effective_workers": perf["effective_workers"],
            "worker_pids": perf["worker_pids"], "worker_cpu_seconds": perf["worker_cpu_seconds"],
            "worker_physical_evaluations": perf["worker_training_physical_evaluations"],
            "cleanup_complete": perf["cleanup_complete"], "batches": batches,
            "batch_decisions": perf["batch_decisions"],
        }, sort_keys=True))
