"""Current sensitivity UI safety contracts using real frozen optical execution.

Only scheduler transport is held in memory. Both current workflows use the
production freezer/executor and physical replay before display or application.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.core import film_sensitivity_workflow_jobs as jobs
from coat_sim.scheduler.jobs import JobSpec
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


class _HeldPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def waitForDone(self, _timeout):
        return True

    def run(self):
        self.tasks[-1].run()


class _PhysicsClient:
    """No IPC: real current worker with durable-shaped receipt/status rows."""
    def __init__(self):
        self.submissions, self.rows, self.progress, self.checkpoints = [], {}, [], []
        self.cancel_stage = ""
        self.unknown = self.tamper = False
        self.executors = ["film_thickness_error_table", "film_ideal_sensitivity"]

    def capabilities(self):
        return {"protocol_version": 1, "executors": list(self.executors)}

    def submit(self, spec, *, job_id, metadata):
        parsed = JobSpec.parse_frozen(spec)
        self.submissions.append((deepcopy(spec), job_id, deepcopy(metadata)))
        if self.unknown:
            raise OSError("held lost submission acknowledgement")
        cancel = Event()

        def report(item):
            self.progress.append(deepcopy(item))
            if self.cancel_stage and item["stage"].endswith(":" + self.cancel_stage):
                cancel.set()

        payload = jobs.execute_job(parsed.parameters, cancel, report, "memory-checkpoint",
                                   lambda _path, value: self.checkpoints.append(deepcopy(value)))
        if self.tamper:
            if parsed.executor == "film_thickness_error_table":
                condition = payload["analysis"]["conditions"][0]
                condition["bias_shift"] += 1.
                condition["linear_bias_value"] = condition["nominal_value"] + condition["bias_shift"]
            else:
                payload["best_analysis"]["objective_value"] += 1.
        state = {"completed": "succeeded", "partial": "succeeded",
                 "failed": "failed", "cancelled": "cancelled"}[payload["state"]]
        self.rows[job_id] = {"job_id": job_id, "kind": parsed.executor, "state": state,
                            "input_digest": parsed.fingerprint, "result": payload}
        return deepcopy(self.rows[job_id])

    def status(self, job_id):
        if job_id not in self.rows:
            raise ValueError("job_not_found")
        return deepcopy(self.rows[job_id])

    def wait_for_terminal(self, job_id, *, on_update, should_stop, wait_s):
        if self.progress:
            on_update({"progress": self.progress[-1]})
        return self.status(job_id)

    def cancel(self, job_id):
        return {"job_id": job_id, "cancel_requested": True}


@pytest.fixture
def harness(app, tmp_path):
    service, library, design = fixture(tmp_path)
    with optimizer(service, design) as (owner, source, session):
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = owner.sensitivityController
        native_pool = panel.pool
        pool, client = _HeldPool(), _PhysicsClient()
        panel.pool, panel.client_factory = pool, lambda _root: client
        assert panel.prepare(), panel.error
        layer_id = panel.thicknessErrorRows[0]["layer_id"]
        assert panel.setTableDraft(layer_id, "bias", "1")
        assert panel.setTableUnit(layer_id, "bias", "absolute_nm")
        yield SimpleNamespace(owner=owner, source=source, session=session, service=service,
                              library=library, design=design, panel=panel,
                              pool=pool, client=client, native_pool=native_pool)


def _finite_bounds(harness):
    layer_id = harness.session.snapshot().design["front_layers"][0]["id"]
    assert harness.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert harness.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    assert harness.panel.prepare(), harness.panel.error
    return layer_id


def _start(harness):
    assert harness.panel.startThicknessErrorTableAnalysis(), harness.panel.tableError
    harness.pool.run()
    assert not harness.panel.busy
    return harness.panel._table_payload


def _start_ideal(harness, *, evaluations="2", policy="strict_nominal", configure_bounds=True):
    if configure_bounds:
        _finite_bounds(harness)
    panel = harness.panel
    assert panel.setIdealSensitivityMode("absolute")
    assert panel.setIdealSensitivityPolicy(policy)
    if policy == "bounded_baseline_degradation":
        for row in panel.idealDegradationObjectives:
            assert panel.setIdealDegradationBudget(row["objective_id"], "100")
    assert panel.setIdealOption("max_iterations", "20")
    assert panel.setIdealOption("max_evaluations", evaluations)
    assert panel.startIdealSensitivityOptimization(), panel.idealError
    harness.pool.run()
    assert not panel.busy and panel.idealHasResult, panel.idealError
    return panel._ideal_payload


def test_preparation_reloads_complete_design_preserves_drafts_by_layer_id(harness):
    panel = harness.panel
    original = panel.thicknessErrorRows[0]["layer_id"]
    assert panel.setTableDraft(original, "bias", "2.75")
    added = {**deepcopy(harness.design["front_layers"][0]), "id": "added-current-layer"}
    harness.session.design["front_layers"].append(added)
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare(), panel.error
    rows = panel.thicknessErrorRows
    assert [r["layer_id"] for r in rows] == [original, "added-current-layer"]
    assert [r["bias_text"] for r in rows] == ["2.75", ""]
    rows[0]["bias_text"] = "999"
    assert panel.thicknessErrorRows[0]["bias_text"] == "2.75"
    payload = _start(harness)
    assert payload["frozen_request"]["design_snapshot"]["front_layers"] == harness.session.design["front_layers"]
    assert len(payload["analysis"]["layer_summaries"]) == 1  # Only explicitly configured error rows.
    assert len(panel.idealSensitivityLayers) == 2


def test_empty_design_preparation_fails_without_source_mutation(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    with optimizer(service, design) as (owner, _source, session):
        before = deepcopy(session.design)
        assert not owner.sensitivityController.prepare()
        assert "膜层" in owner.sensitivityController.error
        assert "膜层" in owner.sensitivityController.idealError
        assert session.design == before


@pytest.mark.parametrize("unit,input_value,expected", [("absolute_nm", "2.5", 2.5),
                                                      ("relative_percent", "2.5", .025)])
def test_table_units_preserve_nominal_targets_and_real_replay(harness, unit, input_value, expected):
    panel = harness.panel
    nominal = harness.owner._build_request(require_variables=False, fixed_topology=False)
    original = deepcopy(harness.session.design)
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    assert panel.setTableUnit(layer_id, "bias", unit)
    assert panel.setTableDraft(layer_id, "bias", input_value)
    payload = _start(harness)
    request = jobs.deserialize_job_payload(harness.client.submissions[0][0]["parameters"]["payload"])
    assert request.rows[0].bias == expected
    assert request.rows[0].bias_unit == ("absolute_nm" if unit == "absolute_nm" else "relative_fraction")
    assert request.objectives == nominal.objectives
    assert harness.session.design == original and not harness.source.applied
    assert payload["schema_version"] == 2  # Current table result carries nested band criteria.
    assert "不可应用" in panel.tableResultSummary
    assert not panel.idealCanApply and not panel.applySelectedBest()


@pytest.mark.parametrize("index", [-1, 2, 3, True, "1"])
def test_only_two_current_workflow_indices_are_accepted(harness, index):
    panel = harness.panel
    assert panel.activeWorkflow == "ideal"
    assert not panel.selectWorkflow(index)
    assert panel.activeWorkflow == "ideal"
    for removed in ("layerErrors", "setChoice", "generateProcessScenarios", "generateScenarios",
                    "start", "applyBest", "scenarioResults", "_legacy_context", "_legacy_job_id"):
        assert not hasattr(panel, removed), removed


class _ProjectCompute:
    def __init__(self):
        self.current, self.editing_frozen, self.updates = True, False, []

    def capture(self, job_id):
        return {"job_id": job_id, "project": "captured-project"}

    def metadata(self, operation, metadata):
        return {**metadata, "origin": {"project": operation["project"]}}

    def accepts(self, _operation):
        return self.current

    def register_job(self, *_args, **_kwargs):
        return None

    def update_job(self, job_id, state):
        self.updates.append((job_id, state))


@pytest.mark.parametrize("change", ["cancel", "revision", "pending_edit", "project"])
def test_cancel_or_source_project_change_before_admission_submits_nothing(harness, change):
    panel = harness.panel
    if change == "project":
        harness.owner._project_compute = _ProjectCompute()
    assert panel.startThicknessErrorTableAnalysis()
    if change == "cancel":
        panel.cancel()
    elif change == "revision":
        harness.session.design_revision += 1
    elif change == "pending_edit":
        harness.source.pendingEditEpoch += 1
    else:
        harness.owner._project_compute.current = False
    harness.pool.run()
    assert not panel.busy and not panel.tableHasResult
    assert "before admission" in panel.tableError
    assert not harness.client.submissions and not harness.source.applied
    harness.owner._project_compute = None


def test_repeat_start_generation_fencing_and_frozen_request_ownership(harness):
    panel = harness.panel
    assert panel.startThicknessErrorTableAnalysis()
    generation = panel._generation
    assert not panel.startThicknessErrorTableAnalysis()
    assert not panel.setTableDraft(panel.thicknessErrorRows[0]["layer_id"], "bias", "9")
    captured = deepcopy(jobs.workflow_request_to_payload(harness.pool.tasks[-1].request))
    harness.pool.run()
    first = deepcopy(panel._table_payload)
    assert panel.startThicknessErrorTableAnalysis()
    assert panel._generation == generation + 1
    panel._on_progress(generation, {"stage": "STALE", "message": "wrong attempt"})
    panel._on_finished(generation, first, "old error")
    panel._on_admitted(generation)
    assert panel.busy and not panel.tableHasResult and not panel.tableError
    assert "STALE" not in panel.tableStatus
    assert jobs.workflow_request_to_payload(harness.pool.tasks[0].request) == captured
    harness.pool.run()
    assert panel.tableHasResult and len(harness.client.submissions) == 2


def test_unknown_submission_keeps_same_id_and_never_reposts(harness):
    panel = harness.panel
    harness.client.unknown = True
    assert panel.startThicknessErrorTableAnalysis()
    job_id = panel.jobId
    harness.pool.run()
    assert panel.tableError.startswith("submission_unknown:") and not panel.tableHasResult
    assert not panel.startThicknessErrorTableAnalysis()
    assert panel.jobId == job_id and len(harness.client.submissions) == 1
    assert "不会自动重复提交" in panel.tableError


def test_unsupported_scheduler_and_tampered_physics_never_become_results(harness):
    panel = harness.panel
    harness.client.executors = []
    assert panel.startThicknessErrorTableAnalysis()
    harness.pool.run()
    assert "does not support" in panel.tableError
    assert not harness.client.submissions
    harness.client.executors = ["film_thickness_error_table"]
    harness.client.tamper = True
    assert panel.startThicknessErrorTableAnalysis()
    harness.pool.run()
    assert not panel.tableHasResult and not panel.idealCanApply
    assert "replay" in panel.tableError and not harness.source.applied


def test_partial_ideal_candidate_never_auto_applies(harness):
    before = deepcopy(harness.session.design)
    payload = _start_ideal(harness)
    assert payload["state"] == "partial" and payload["termination_reason"] == "evaluation_limit"
    assert payload["is_final"] and payload["schema_version"] == 3
    assert harness.panel.idealCanApply
    assert not harness.source.applied and harness.session.design == before


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_real_ideal_terminal_states_keep_reports_but_never_apply(harness, terminal):
    if terminal == "failed":
        assert harness.owner.setObjectiveField("pointwise.upper_bound_percent", 100.)
        assert harness.owner.setObjectiveField("pointwise.lower_bound_percent", 99.)
    else:
        harness.client.cancel_stage = "baseline"
    before = deepcopy(harness.session.design)
    payload = _start_ideal(harness)
    assert payload["state"] == terminal and payload["is_final"]
    assert not harness.panel.idealCanApply and not harness.panel.applySelectedBest()
    assert not harness.source.applied and harness.session.design == before


def test_current_ideal_analysis_history_never_becomes_optimization_candidate(harness):
    from coat_sim.core.film_ideal_sensitivity import (
        analyze_film_ideal_sensitivity, film_ideal_sensitivity_result_to_payload,
    )

    panel = harness.panel
    _finite_bounds(harness)
    assert panel.setIdealSensitivityMode("absolute")
    assert panel.setIdealSensitivityPolicy("strict_nominal")
    base = harness.owner._build_request(require_variables=False, fixed_topology=False)
    request = jobs.freeze_workflow_request(harness.service, panel._build_ideal_request(base))
    result = analyze_film_ideal_sensitivity(None, request)
    payload = jobs.validate_workflow_result(
        film_ideal_sensitivity_result_to_payload(result), expected_request=request, replay=True)
    assert result.operation == "analyze" and result.state == "completed"
    panel.load_verified_history(payload, "analysis-history")
    assert panel.idealHasResult and not panel.idealCanApply
    assert not panel.applySelectedBest() and not harness.source.applied


@pytest.mark.parametrize("conflict", ["revision", "pending_edit", "material", "project", "project_frozen", "source_closed"])
def test_explicit_ideal_apply_rejects_changed_binding_atomically(harness, conflict):
    panel = harness.panel
    if conflict.startswith("project"):
        harness.owner._project_compute = _ProjectCompute()
    payload = _start_ideal(harness)
    assert payload is not None and panel.idealCanApply
    if conflict == "revision":
        harness.session.design_revision += 1
    elif conflict == "pending_edit":
        harness.source.pendingEditEpoch += 1
    elif conflict == "material":
        original = harness.library.read("acceptance-film")["record"]
        original["optical_constants"][0]["n"] = [2.2, 2.2]
        harness.library.write(original, update=True)
    elif conflict == "project":
        harness.owner._project_compute.current = False
    elif conflict == "project_frozen":
        harness.owner._project_compute.editing_frozen = True
    else:
        harness.owner._source_closed = True
    before = deepcopy(harness.session.design)
    assert not panel.applySelectedBest()
    assert panel.idealHasResult and panel.idealError
    assert not harness.source.applied and harness.session.design == before
    harness.owner._project_compute = None


def test_explicit_ideal_apply_uses_captured_versions_once_and_keeps_report(harness):
    payload = _start_ideal(harness)
    panel = harness.panel
    assert panel.applySelectedBest(), panel.idealError
    assert len(harness.source.applied) == 1
    candidate, revision, epoch = harness.source.applied[0]
    assert candidate == payload["best_design"]
    assert revision == payload["design_revision"] and epoch == harness.source.pendingEditEpoch
    assert not panel.idealCanApply and not panel.applySelectedBest()
    assert panel._ideal_payload == payload


def test_report_exact_json_requires_explicit_overwrite_and_local_url(harness, tmp_path):
    panel = harness.panel
    payload = _start(harness)
    path = tmp_path / "independent-report.json"
    path.write_text("do not replace", encoding="utf-8")
    requests: list[str] = []
    panel.overwriteRequested.connect(requests.append)
    assert not panel.saveReport(str(path))
    assert requests == [str(path)] and path.read_text(encoding="utf-8") == "do not replace"
    assert panel.saveReport(str(path), True), panel.tableError
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert path.read_text(encoding="utf-8").endswith("\n")
    other = tmp_path / "new-report.txt"
    assert panel.saveReportUrl(QUrl.fromLocalFile(str(other)))
    assert not other.exists()
    assert json.loads(other.with_suffix(".json").read_text(encoding="utf-8")) == payload
    assert not panel.saveReportUrl(QUrl("https://example.test/report.json"))
    assert "本地" in panel.tableError


@pytest.mark.parametrize("change", ["cancel", "revision", "pending_edit", "project", "disconnect"])
def test_capability_check_race_prevents_admission(harness, change):
    panel = harness.panel
    capabilities = harness.client.capabilities
    if change == "project":
        harness.owner._project_compute = _ProjectCompute()

    def discover_after_change():
        if change == "cancel":
            panel.cancel()
        elif change == "revision":
            harness.session.design_revision += 1
        elif change == "pending_edit":
            harness.source.pendingEditEpoch += 1
        elif change == "project":
            harness.owner._project_compute.current = False
        else:
            panel._disconnected.set()
        return capabilities()

    harness.client.capabilities = discover_after_change
    assert panel.startThicknessErrorTableAnalysis()
    harness.pool.run()
    assert not harness.client.submissions and not panel.tableHasResult and not panel.busy
    harness.owner._project_compute = None


def test_closed_observer_ignores_late_completion_and_submits_nothing(harness):
    panel = harness.panel
    assert panel.startThicknessErrorTableAnalysis()
    generation = panel._generation
    panel.detach()
    assert panel._cancel.is_set()
    harness.pool.run()
    status = panel.tableStatus
    panel._on_progress(generation, {"stage": "late-after-close"})
    panel._on_finished(generation, None, "late error")
    assert panel.tableStatus == status and not panel.tableHasResult and not harness.client.submissions
    assert not panel.prepare() and not panel.startThicknessErrorTableAnalysis()


@pytest.mark.parametrize("field,value", [("bias", "nan"), ("bias", "1e"),
                                         ("max_evaluations", "0"), ("max_iterations", "2.5")])
def test_invalid_current_inputs_reject_before_any_submission(harness, field, value):
    panel = harness.panel
    if field == "bias":
        assert panel.setTableDraft(panel.thicknessErrorRows[0]["layer_id"], field, value)
        assert not panel.startThicknessErrorTableAnalysis()
        assert panel.tableError
    else:
        _finite_bounds(harness)
        assert panel.setIdealSensitivityMode("absolute")
        assert panel.setIdealSensitivityPolicy("strict_nominal")
        assert panel.setIdealOption(field, value)
        assert not panel.startIdealSensitivityOptimization()
        assert panel.idealError
    assert not harness.client.submissions and not harness.pool.tasks


def test_original_wavelength_angle_polarization_grid_is_not_thinned(harness):
    owner = harness.owner
    assert owner.setObjectiveField("wavelength_min_nm", 500.)
    assert owner.setObjectiveField("wavelength_max_nm", 600.)
    assert owner.setObjectiveField("wavelength_step_nm", 7.)
    assert owner.confirmObjectiveAngleRange("0", "30", "10")
    assert owner.setObjectiveField("polarization", "p")
    nominal = owner._build_request(require_variables=False, fixed_topology=False)
    assert harness.panel.prepare()
    payload = _start(harness)
    captured = payload["frozen_request"]["objectives"][0]
    assert captured["wavelength_samples"] == nominal.objectives[0].wavelength_samples == 16
    assert captured["angles_deg"] == [0., 10., 20., 30.]
    assert captured["polarization"] == "p"
    assert len(payload["analysis"]["conditions"]) == 64


def test_current_history_has_no_apply_binding_and_preserves_payload(harness, app, tmp_path):
    panel = harness.panel
    payload = _start_ideal(harness)
    assert panel.idealCanApply
    panel.load_verified_history(payload, "history-job-1")
    assert panel.idealHasResult and not panel.idealCanApply and not panel.applySelectedBest()
    assert panel.jobId == "history-job-1" and "只读" in panel.idealStatus
    qml = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/FilmSensitivityResultWindow.qml"
    with window_runtime(app, panel, source=qml.read_text(encoding="utf-8")) as (window, warnings):
        assert "只读" in window.title()
        assert window.findChild(QObject, "filmIdealSensitivityApplyButton") is None
        assert not window.grabWindow().isNull()
        assert not warnings, warnings
    target = tmp_path / "history.json"
    assert panel.saveReport(str(target))
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert not harness.source.applied


@pytest.mark.parametrize("kind,schema", [("coat_sim.film_sensitivity_result", 1),
                                        ("coat_sim.film_ideal_sensitivity_result", 1)])
def test_unsupported_history_is_rejected_without_mutating_current_state(harness, kind, schema):
    panel = harness.panel
    payload = _start(harness)
    before = deepcopy(panel._table_payload)
    old = deepcopy(payload)
    old["kind"], old["schema_version"] = kind, schema
    with pytest.raises(ValueError, match="unsupported sensitivity format"):
        panel.load_verified_history(old, "withdrawn-history")
    assert panel._table_payload == before
    assert panel.jobId != "withdrawn-history"
    assert not panel.applySelectedBest()


def test_real_source_ideal_apply_is_one_unsaved_undo_transaction(app, tmp_path, monkeypatch):
    from coat_sim.ui.compute_target import ComputeTarget
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController

    service, _library, design = fixture(tmp_path)
    path = tmp_path / "source.cfilm"
    service.save(path, design)
    original_bytes = path.read_bytes()
    source = FilmDesignQuickController(root=tmp_path, service=service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat))
    assert source.session is not None and source.session.request_open_file(str(path))
    owner = FilmOptimizationQuickController(source)
    try:
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = cast(FilmSensitivityQuickController, owner.sensitivityController)
        client, pool = _PhysicsClient(), _HeldPool()
        monkeypatch.setattr(panel, "pool", pool)
        panel.client_factory = lambda _root: client
        assert panel.prepare()
        context = SimpleNamespace(panel=panel, owner=owner, session=source.session, pool=pool)
        _finite_bounds(context)
        before = source.session.snapshot()
        payload = _start_ideal(context, evaluations="60", policy="bounded_baseline_degradation",
                               configure_bounds=False)
        assert panel.idealCanApply, panel.idealError
        assert payload["best_design"] != before.design
        # Editing the ideal draft is persisted, but does not alter the physical design.
        pending = source.session.snapshot().design
        assert {k: v for k, v in pending.items() if k != "workflow_settings"} == {
            k: v for k, v in before.design.items() if k != "workflow_settings"}
        assert panel.applySelectedBest(), panel.idealError
        after = source.session.snapshot()
        assert after.design_revision == before.design_revision + 1
        assert after.design == payload["best_design"] and after.dirty
        assert source.canUndoOptimizedDesign and path.read_bytes() == original_bytes
        assert not panel.applySelectedBest()
        assert source.undoOptimizedDesign()
        assert source.session.snapshot().design == pending
        assert not source.undoOptimizedDesign() and path.read_bytes() == original_bytes
    finally:
        owner.shutdown(1000)
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        owner.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


def test_lost_ack_matching_receipt_reconciles_without_repost(harness):
    submit = harness.client.submit

    def submit_then_lose_ack(*args, **kwargs):
        submit(*args, **kwargs)
        raise OSError("Acknowledgement lost after real frozen execution")

    harness.client.submit = submit_then_lose_ack
    payload = _start(harness)
    assert payload is not None and payload["state"] == "completed", harness.panel.tableError
    assert len(harness.client.submissions) == 1 and not harness.panel._unknown_submission
    assert harness.panel.jobId in harness.client.rows


def test_real_qthreadpool_delivers_replayed_current_result_to_qt_owner(harness, app, monkeypatch):
    import time

    panel = harness.panel
    monkeypatch.setattr(panel, "pool", harness.native_pool)
    assert panel.startThicknessErrorTableAnalysis()
    deadline = time.monotonic() + 10.
    while panel.busy and time.monotonic() < deadline:
        QTest.qWait(10)
        app.processEvents()
    assert harness.native_pool.waitForDone(1000)
    app.processEvents()
    assert not panel.busy and panel.tableHasResult, panel.tableError
    assert panel._table_payload["state"] == "completed" and len(harness.client.submissions) == 1
    assert not harness.source.applied
