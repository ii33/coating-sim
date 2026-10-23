"""Independent sensitivity UI integration using real frozen optical execution.

Only the scheduler transport is held in memory. Submitted specifications go
through the production freezer and executor, and terminal replies are physically
replayed by the real controller before they can be displayed or applied.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from threading import Event
from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core import film_sensitivity_jobs as jobs
from coat_sim.core.film_sensitivity import film_sensitivity_request_to_payload
from coat_sim.scheduler.jobs import JobSpec
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import click, optimizer, window_runtime

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
    """No IPC: production frozen worker with real physics and durable-shaped rows."""
    def __init__(self):
        self.submissions = []
        self.rows = {}
        self.progress = []
        self.checkpoints = []
        self.cancel_stage = ""
        self.unknown = False
        self.tamper = False
        self.executors = ["film_sensitivity"]

    def capabilities(self):
        return {"protocol_version": 1, "executors": self.executors}

    def submit(self, spec, *, job_id, metadata):
        parsed = JobSpec.parse_frozen(spec)
        self.submissions.append((deepcopy(spec), job_id, deepcopy(metadata)))
        if self.unknown:
            raise OSError("held lost submission acknowledgement")
        cancel = Event()

        def report(item):
            self.progress.append(deepcopy(item))
            if item["stage"] == "film_sensitivity:" + self.cancel_stage:
                cancel.set()

        payload = jobs.execute_job(parsed.parameters, cancel, report, "memory-checkpoint",
                                   lambda _path, value: self.checkpoints.append(deepcopy(value)))
        if self.tamper:
            payload["best_analysis"]["robustness_value"] += 1.0
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
        assert owner.setObjectiveField("wavelength_min_nm", 550.0)
        assert owner.setObjectiveField("wavelength_max_nm", 550.0)
        sensitivity = owner.sensitivityController
        native_pool = sensitivity.pool
        pool, client = _HeldPool(), _PhysicsClient()
        sensitivity.pool = pool
        sensitivity.client_factory = lambda _root: client
        assert sensitivity.prepare(), sensitivity.error
        # These legacy regression cases explicitly exercise the old pure
        # sensitivity mode; the new process UI has no calibrated defaults.
        assert sensitivity.setChoice("policy", "robustness_only")
        assert sensitivity.setChoice("objective", "linear_sensitivity_norm")
        assert sensitivity.setChoice("model", "independent")
        assert sensitivity.setChoice("semantics", "bounded_amplitude")
        for row in sensitivity.layerErrors:
            assert sensitivity.setMagnitude(row["layer_id"], "1")
        yield SimpleNamespace(owner=owner, source=source, session=session, service=service,
                              library=library, design=design, sensitivity=sensitivity,
                              pool=pool, client=client, native_pool=native_pool)


def _finite_bounds(harness):
    layer_id = harness.design["front_layers"][0]["id"]
    assert harness.owner.setVariableBound("front", layer_id, "lower_nm", 60.0)
    assert harness.owner.setVariableBound("front", layer_id, "upper_nm", 145.0)
    return layer_id


def _start(harness, operation="analyze"):
    assert harness.sensitivity.start(operation), harness.sensitivity.error
    harness.pool.run()
    assert not harness.sensitivity.busy
    return harness.sensitivity._payload


def test_preparation_reloads_current_complete_design_preserves_errors_by_layer_id(harness):
    panel = harness.sensitivity
    original = panel.layerErrors[0]["layer_id"]
    assert panel.setMagnitude(original, "2.75")
    added = {**deepcopy(harness.design["front_layers"][0]), "id": "added-current-layer"}
    harness.session.design["front_layers"].append(added)
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare(), panel.error
    assert [r["layer_id"] for r in panel.layerErrors] == [original, "added-current-layer"]
    # The added layer has no process assumption until this legacy case
    # explicitly supplies its historical 1 nm test input.
    assert panel.setMagnitude("added-current-layer", "1")
    assert [r["magnitude"] for r in panel.layerErrors] == ["2.75", "1"]
    rows = panel.layerErrors
    rows[0]["magnitude"] = "999"
    assert panel.layerErrors[0]["magnitude"] == "2.75"
    payload = _start(harness)
    assert payload["frozen_request"]["design_snapshot"]["front_layers"] == harness.session.design["front_layers"]
    assert len(payload["best_analysis"]["linear_conditions"][0]["derivative_per_nm"]) == 2


def test_empty_design_preparation_fails_without_nominal_or_source_mutation(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    with optimizer(service, design) as (owner, _source, session):
        before = deepcopy(session.design)
        assert not owner.sensitivityController.prepare()
        assert "膜层" in owner.sensitivityController.error
        assert session.design == before


@pytest.mark.parametrize("unit,input_value,expected", [("absolute_nm", "2.5", 2.5),
                                                        ("relative_fraction", "2.5", .025)])
def test_units_preserve_original_nominal_targets_and_real_replay(harness, unit, input_value, expected):
    panel = harness.sensitivity
    nominal = harness.owner._build_request(require_variables=False, fixed_topology=False)
    original = deepcopy(harness.session.design)
    assert panel.setChoice("unit", unit)
    assert panel.setMagnitude(panel.layerErrors[0]["layer_id"], input_value)
    assert panel.generateScenarios()
    payload = _start(harness)
    request = jobs.deserialize_job_payload(harness.client.submissions[0][0]["parameters"]["payload"])
    assert request.errors[0].magnitude == expected and request.errors[0].unit == unit
    assert request.objectives == nominal.objectives
    assert request.acceptable_normalized_error == nominal.acceptable_normalized_error
    assert harness.session.design == original and not harness.source.applied
    row = payload["best_analysis"]["linear_conditions"][0]
    assert row["error_amplitudes_nm"] == [2.5]
    assert "不是" in panel.resultSummary
    assert not panel.canApply
    assert len(panel.scenarioResults) == 2


@pytest.mark.parametrize("model,expected", [
    ("independent", [(-1., 0.), (1., 0.), (0., -1.), (0., 1.)]),
    ("common_mode", [(-1., -1.), (1., 1.)]),
])
def test_explicit_scenario_generation_distinguishes_common_and_independent(harness, model, expected):
    panel = harness.sensitivity
    harness.session.design["front_layers"].append(
        {**deepcopy(harness.design["front_layers"][0]), "id": "another-layer"})
    harness.owner.refreshDesign()
    assert panel.prepare()
    for row in panel.layerErrors:
        assert panel.setMagnitude(row["layer_id"], "1")
    assert panel.setChoice("model", model)
    assert panel.scenarioCount == 0
    assert panel.generateScenarios()
    assert [r.coefficients for r in panel._scenarios] == expected
    assert panel.setChoice("objective", "finite_scenario_expected")
    assert panel.scenarioCount == 0  # An edit invalidates previously generated assumptions.
    assert panel.generateScenarios()
    payload = _start(harness)
    assert len(payload["best_analysis"]["finite_scenarios"]) == len(expected)
    assert payload["robustness_objective"] == "finite_scenario_expected"


def test_optimize_requires_explicit_finite_bounds_before_any_submission(harness):
    assert not harness.sensitivity.start("optimize")
    assert "bounded" in harness.sensitivity.error.lower()
    assert not harness.pool.tasks and not harness.client.submissions
    _finite_bounds(harness)
    assert harness.sensitivity.setOption("max_evaluations", "2")
    payload = _start(harness, "optimize")
    assert payload["state"] == "partial" and payload["termination_reason"] == "evaluation_limit"
    assert harness.sensitivity.canApply and not harness.source.applied


@pytest.mark.parametrize("change", ["cancel", "revision", "pending_edit", "project"])
def test_cancel_or_source_project_change_before_admission_submits_nothing(harness, change):
    panel = harness.sensitivity
    if change == "project":
        compute = _ProjectCompute()
        harness.owner._project_compute = compute
    assert panel.start("analyze")
    if change == "cancel":
        panel.cancel()
    elif change == "revision":
        harness.session.design_revision += 1
    elif change == "pending_edit":
        harness.source.pendingEditEpoch += 1
    else:
        compute.current = False
    harness.pool.run()
    assert not panel.busy and not panel.hasResult
    assert "before admission" in panel.error
    assert not harness.client.submissions and not harness.source.applied
    harness.owner._project_compute = None


def test_repeat_start_generation_fencing_and_frozen_request_ownership(harness):
    panel = harness.sensitivity
    assert panel.start("analyze")
    first_generation = panel._generation
    assert not panel.start("analyze")
    assert not panel.setMagnitude(panel.layerErrors[0]["layer_id"], "9")
    captured = deepcopy(film_sensitivity_request_to_payload(harness.pool.tasks[-1].request))
    harness.pool.run()
    first = deepcopy(panel._payload)
    assert panel.start("analyze")
    assert panel._generation == first_generation + 1
    panel._on_progress(first_generation, {"stage": "STALE", "message": "wrong attempt"})
    panel._on_finished(first_generation, first, "old error")
    panel._on_admitted(first_generation)
    assert panel.busy and not panel.hasResult and not panel.error
    assert "STALE" not in panel.status
    assert film_sensitivity_request_to_payload(harness.pool.tasks[0].request) == captured
    harness.pool.run()
    assert panel.hasResult and len(harness.client.submissions) == 2


def test_unknown_submission_keeps_same_id_and_never_reposts(harness):
    panel = harness.sensitivity
    harness.client.unknown = True
    assert panel.start("analyze")
    job_id = panel.jobId
    harness.pool.run()
    assert panel.error.startswith("submission_unknown:") and not panel.hasResult
    assert not panel.start("analyze")
    assert panel.jobId == job_id and len(harness.client.submissions) == 1
    assert "不会自动重复提交" in panel.error


def test_unsupported_scheduler_and_tampered_physics_never_become_results(harness):
    harness.client.executors = []
    assert harness.sensitivity.start("analyze")
    harness.pool.run()
    assert "does not support" in harness.sensitivity.error
    assert not harness.client.submissions
    harness.client.executors = ["film_sensitivity"]
    harness.client.tamper = True
    assert harness.sensitivity.start("analyze")
    harness.pool.run()
    assert not harness.sensitivity.hasResult and not harness.sensitivity.canApply
    assert "replay" in harness.sensitivity.error or "baseline" in harness.sensitivity.error
    assert not harness.source.applied


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "partial"])
def test_real_terminal_states_remain_distinct_never_auto_apply(harness, terminal):
    panel = harness.sensitivity
    _finite_bounds(harness)
    if terminal == "failed":
        assert panel.setMagnitude(panel.layerErrors[0]["layer_id"], "200")
        assert panel.generateScenarios()
    elif terminal == "cancelled":
        harness.client.cancel_stage = "baseline_complete"
    else:
        assert panel.setOption("max_evaluations", "2")
    before = deepcopy(harness.session.design)
    payload = _start(harness, "optimize")
    assert payload is not None, panel.error
    assert payload["state"] == terminal and payload["is_final"]
    assert panel.hasResult and panel.canApply == (terminal == "partial")
    assert not harness.source.applied and harness.session.design == before
    if terminal != "partial":
        assert not panel.applyBest()


class _ProjectCompute:
    def __init__(self):
        self.current = True
        self.editing_frozen = False
        self.updates = []

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


@pytest.mark.parametrize("conflict", ["revision", "pending_edit", "material", "project", "project_frozen", "source_closed"])
def test_explicit_apply_rejects_changed_binding_as_one_atomic_candidate(harness, conflict):
    panel = harness.sensitivity
    _finite_bounds(harness)
    assert panel.setOption("max_evaluations", "2")
    if conflict.startswith("project"):
        harness.owner._project_compute = _ProjectCompute()
    payload = _start(harness, "optimize")
    assert payload is not None and panel.canApply
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
    assert not panel.applyBest()
    assert panel.hasResult and panel.error
    assert not harness.source.applied and harness.session.design == before
    harness.owner._project_compute = None


def test_explicit_apply_uses_captured_versions_once_and_keeps_full_report(harness):
    panel = harness.sensitivity
    _finite_bounds(harness)
    assert panel.setOption("max_evaluations", "2")
    payload = _start(harness, "optimize")
    assert panel.applyBest(), panel.error
    assert len(harness.source.applied) == 1
    candidate, revision, edit_epoch = harness.source.applied[0]
    assert candidate == payload["best_design"]
    assert revision == payload["design_revision"] and edit_epoch == harness.source.pendingEditEpoch
    assert not panel.canApply and not panel.applyBest()
    assert panel._payload == payload


def test_report_exact_json_requires_explicit_overwrite_and_local_url(harness, tmp_path):
    panel = harness.sensitivity
    payload = _start(harness)
    path = tmp_path / "independent-report.json"
    path.write_text("do not replace", encoding="utf-8")
    requests: list[str] = []
    panel.overwriteRequested.connect(requests.append)
    assert not panel.saveReport(str(path))
    assert requests == [str(path)] and path.read_text(encoding="utf-8") == "do not replace"
    assert panel.saveReport(str(path), True), panel.error
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert path.read_text(encoding="utf-8").endswith("\n")
    other = tmp_path / "new-report.txt"
    assert panel.saveReportUrl(QUrl.fromLocalFile(str(other)))
    assert not other.exists()
    assert json.loads(other.with_suffix(".json").read_text(encoding="utf-8")) == payload
    assert not panel.saveReportUrl(QUrl("https://example.test/report.json"))
    assert "本地" in panel.error


def test_embedded_panel_opens_current_request_and_uses_theme_controls(harness, app):
    with window_runtime(app, harness.owner) as (window, warnings):
        button = window.findChild(QQuickItem, "filmSensitivityOpenButton")
        dialog = window.findChild(QObject, "filmSensitivityDialog")
        assert button is not None and dialog is not None
        click(app, window, button)
        assert dialog.property("visible")
        for name in ("filmSensitivityUnitBox", "filmSensitivityModelBox", "filmSensitivityObjectiveBox",
                     "filmSensitivityAnalyzeButton", "filmSensitivityOptimizeButton", "filmSensitivityApplyButton"):
            assert window.findChild(QObject, name) is not None, name
        assert not window.findChild(QQuickItem, "filmSensitivityApplyButton").isEnabled()
        assert not window.grabWindow().isNull()
        assert not warnings, warnings


@pytest.mark.parametrize("change", ["cancel", "revision", "pending_edit", "project", "disconnect"])
def test_cancel_while_capability_check_is_pending_still_prevents_admission(harness, change):
    panel = harness.sensitivity
    capabilities = harness.client.capabilities

    if change == "project":
        harness.owner._project_compute = _ProjectCompute()

    def finish_capability_after_cancel():
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

    harness.client.capabilities = finish_capability_after_cancel
    assert panel.start("analyze")
    harness.pool.run()
    assert not harness.client.submissions
    assert not panel.hasResult and not panel.busy
    harness.owner._project_compute = None


def test_closed_observer_ignores_late_completion_and_submits_nothing(harness):
    panel = harness.sensitivity
    assert panel.start("analyze")
    generation = panel._generation
    panel.detach()
    assert panel._cancel.is_set()
    harness.pool.run()
    status = panel.status
    panel._on_progress(generation, {"stage": "late-after-close"})
    panel._on_finished(generation, None, "late error")
    assert panel.status == status and not panel.hasResult and not harness.client.submissions
    assert not panel.prepare() and not panel.start("analyze")


@pytest.mark.parametrize("field,value", [("magnitude", "nan"), ("magnitude", "-1"),
                                         ("max_evaluations", "0"), ("max_iterations", "2.5")])
def test_invalid_panel_inputs_reject_before_any_submission(harness, field, value):
    panel = harness.sensitivity
    if field == "magnitude":
        assert panel.setMagnitude(panel.layerErrors[0]["layer_id"], value)
    else:
        assert panel.setOption(field, value)
    assert not panel.start("analyze")
    assert panel.error and not harness.client.submissions and not harness.pool.tasks


def test_original_wavelength_angle_polarization_grid_is_not_thinned(harness):
    owner = harness.owner
    assert owner.setObjectiveField("wavelength_min_nm", 500.)
    assert owner.setObjectiveField("wavelength_max_nm", 600.)
    assert owner.setObjectiveField("wavelength_step_nm", 7.)
    assert owner.confirmObjectiveAngleRange("0", "30", "10")
    assert owner.setObjectiveField("polarization", "p")
    nominal = owner._build_request(require_variables=False, fixed_topology=False)
    payload = _start(harness)
    captured = payload["frozen_request"]["objectives"][0]
    assert captured["wavelength_samples"] == nominal.objectives[0].wavelength_samples == 16
    assert captured["angles_deg"] == [0., 10., 20., 30.]
    assert captured["polarization"] == "p"
    assert len(payload["best_analysis"]["linear_conditions"]) == 64


def test_history_report_window_has_no_apply_binding_and_preserves_exact_payload(harness, app, tmp_path):
    panel = harness.sensitivity
    _finite_bounds(harness)
    assert panel.setOption("max_evaluations", "2")
    payload = _start(harness, "optimize")
    assert panel.canApply
    panel.load_verified_history(payload, "history-job-1")
    assert panel.hasResult and not panel.canApply and not panel.applyBest()
    assert panel.jobId == "history-job-1" and "只读" in panel.status
    qml = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/FilmSensitivityResultWindow.qml"
    with window_runtime(app, panel, source=qml.read_text(encoding="utf-8")) as (window, warnings):
        assert "只读" in window.title()
        assert window.findChild(QObject, "filmSensitivityApplyButton") is None
        assert not window.grabWindow().isNull()
        assert not warnings, warnings
    target = tmp_path / "history.json"
    assert panel.saveReport(str(target))
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert not harness.source.applied


def test_real_source_apply_is_one_unsaved_undo_transaction(app, tmp_path, monkeypatch):
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
        layer_id = design["front_layers"][0]["id"]
        assert owner.setVariableBound("front", layer_id, "lower_nm", 60.)
        assert owner.setVariableBound("front", layer_id, "upper_nm", 145.)
        before = source.session.snapshot()
        panel = cast(FilmSensitivityQuickController, owner.sensitivityController)
        client, pool = _PhysicsClient(), _HeldPool()
        monkeypatch.setattr(panel, "pool", pool)
        panel.client_factory = lambda _root: client
        assert panel.prepare() and panel.setOption("max_evaluations", "24")
        assert panel.setChoice("policy", "robustness_only")
        assert panel.setChoice("objective", "linear_sensitivity_norm")
        assert panel.setChoice("model", "independent")
        assert panel.setChoice("semantics", "bounded_amplitude")
        for row in cast(list[dict[str, Any]], panel.layerErrors):
            assert panel.setMagnitude(row["layer_id"], "1")
        assert panel.start("optimize"), panel.error
        pool.run()
        payload = panel._payload
        assert payload is not None and panel.canApply, panel.error
        assert payload["best_design"] != before.design
        assert source.session.snapshot().design == before.design
        assert panel.applyBest(), panel.error
        after = source.session.snapshot()
        assert after.design_revision == before.design_revision + 1
        assert after.design == payload["best_design"] and after.dirty
        assert source.canUndoOptimizedDesign
        assert path.read_bytes() == original_bytes
        assert not panel.applyBest()
        assert source.undoOptimizedDesign()
        assert source.session.snapshot().design == before.design
        assert not source.undoOptimizedDesign()
        assert path.read_bytes() == original_bytes
    finally:
        owner.shutdown(1000)
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        owner.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()


def test_lost_ack_with_persisted_matching_receipt_reconciles_without_repost(harness):
    panel = harness.sensitivity
    submit = harness.client.submit

    def submit_then_lose_ack(*args, **kwargs):
        submit(*args, **kwargs)
        raise OSError("Acknowledgement lost after real frozen execution")

    harness.client.submit = submit_then_lose_ack
    payload = _start(harness)
    assert payload is not None and payload["state"] == "completed", panel.error
    assert len(harness.client.submissions) == 1 and not panel._unknown_submission
    assert panel.jobId in harness.client.rows


def test_real_qthreadpool_worker_delivers_replayed_result_to_qt_owner(harness, app, monkeypatch):
    import time

    panel = harness.sensitivity
    monkeypatch.setattr(panel, "pool", harness.native_pool)
    assert panel.start("analyze")
    deadline = time.monotonic() + 10.
    while panel.busy and time.monotonic() < deadline:
        QTest.qWait(10)
        app.processEvents()
    assert harness.native_pool.waitForDone(1000)
    app.processEvents()
    assert not panel.busy and panel.hasResult, panel.error
    assert panel._payload["state"] == "completed" and len(harness.client.submissions) == 1
    assert not harness.source.applied
