"""Real frozen optics across nominal/solver UI, CLI and read-only history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_result_to_payload
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.scheduler.result_capability import is_synthesis_result, job_has_viewable_result
from coat_sim.ui.compute_queue import (
    _SynthesisQueueResult, _annotate_result_availability, _read_queue_result,
)
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_result_view
from coat_sim.ui.project_compute_views import _wire_project_queue_results
from tests.film_structure_fixtures import fixture
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene

pytestmark = [pytest.mark.qt, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def _nominal_negative():
    service, request = setup_problem(upper=.0395, low=550., high=550., samples=1)
    request = freeze_film_synthesis_request(service, replace(request,
        search_strategy="hybrid", max_evaluations=3, max_cycles=1))
    return request, synthesize_film_design(None, request)


def _current_row(root=None):
    _request, result = _nominal_negative()
    payload = film_synthesis_result_to_payload("current-surface", result)
    row = {"job_id": "current-job", "kind": "film_synthesis", "state": "succeeded",
           "result": payload,
           "origin": {"project_id": "project-a", "project_root": "/project-a"}}
    if root is not None:
        _bind_row(root, row)
    return row


def _legacy_row(root=None):
    row = _current_row(root)
    payload = row['result']
    payload['schema_version'] = 1
    payload.pop('acceptance_semantics')
    payload['result'].pop('solver_acceptable')
    payload['result'].update(state='completed', targets_met=True, termination_reason='validated_targets_met')
    payload['best_candidate_audit']['targets_met'] = True
    payload['review']['targets_met'] = True
    return row


def _bind_row(root, row):
    from coat_sim.core.film_synthesis_jobs import build_job_payload
    from coat_sim.scheduler.jobs import JobSpec, canonical
    request_payload = {"schema_version": 4, "kind": "coat_sim.film_synthesis_request",
        "request_id": row["result"]["request_id"],
        "request": row["result"]["result"]["frozen_request"], "physical_structures": []}
    spec = JobSpec("film_synthesis", {"payload": build_job_payload({
        "payload": request_payload, "physical_structure_metadata": []})}, {})
    directory = root / "artifacts/runtime/scheduler/jobs" / row["job_id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.json").write_text(canonical(spec.to_dict()), encoding="utf-8")
    row["input_digest"] = spec.fingerprint
    return spec


class _FileStore:
    def __init__(self, path):
        self.path = path
        self.root = path.parent

    def read_job(self, job_id):
        row = json.loads(self.path.read_text(encoding="utf-8"))
        assert job_id == row["job_id"]
        return row


class _Queue(QObject):
    filmOptimizationResultReady = Signal(str, object)
    filmSynthesisResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)
    cancelReceived = Signal(str, object, str)

    def __init__(self, store, source_job, payload):
        super().__init__()
        self.store = store
        self.status = ""
        self._result_viewer = SimpleNamespace(_shown_job=source_job["job_id"],
            _shown_kind="film_synthesis", _shown_synthesis_source_job=deepcopy(source_job),
            _shown_film_result=deepcopy(payload))

    def _require_owned_job(self, job_id):
        return self.store.read_job(job_id)

    def _result_status(self, text):
        self.status = text


def _route_legacy(store, loaded, *, emitted_job_id=None, emitted_payload=None):
    workspace = cast(ProjectFilmWorkspace, object())
    inspected = []

    class Controller:
        project_context = workspace

        def inspectSynthesisQueueResult(self, job_id, payload, *, origin):
            inspected.append((job_id, deepcopy(payload), origin))
            return True

    controller = Controller()
    queue = _Queue(store, loaded.source_job, loaded.payload)
    desktop = SimpleNamespace(open_project_module=lambda *_: None, controller_for=lambda *_: controller)
    _wire_project_queue_results(queue, workspace, SimpleNamespace(desktop=desktop))
    queue.filmSynthesisResultReady.emit(emitted_job_id or loaded.source_job["job_id"],
                                     emitted_payload if emitted_payload is not None else loaded.payload)
    return queue, inspected


def test_cli_actual_point04_vs_point0395_separates_acceptance_and_keeps_request(tmp_path):
    request, _result = _nominal_negative()
    source, output = tmp_path / "request.json", tmp_path / "result.json"
    source.write_text(json.dumps(film_synthesis_request_to_payload("nominal-cli", request)), encoding="utf-8")
    original = source.read_bytes()
    completed = subprocess.run([sys.executable, "-m", "coat_sim.film_synthesis_cli",
        "--request", str(source), "--output", str(output)], cwd=ROOT,
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert is_synthesis_result(payload)
    assert payload["acceptance_semantics"] == "band_criteria_v2"
    assert payload["result"]["targets_met"] is False
    assert payload["result"]["solver_acceptable"] is True
    assert payload["result"]["frozen_request"]["objectives"][0]["pointwise"]["upper_bound"] == .0395
    assert payload["result"]["frozen_request"]["acceptable_normalized_error"] == .001
    assert payload["result"]["training_score"]["errors"][0]["actual"] == pytest.approx(.04)
    acceptance = [json.loads(line) for line in completed.stderr.splitlines()
                  if json.loads(line).get("kind") == "acceptance"]
    assert len(acceptance) == 1
    assert acceptance[0]["targets_met"] is False and acceptance[0]["solver_acceptable"] is True
    assert source.read_bytes() == original


def test_real_qml_strict_upper_checkbox_roundtrips_without_changing_bound_or_ordinary_request(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode("synthesis")
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        try:
            scene.click(scene.item("filmOptimizationTargetsTab"))
            checkbox = scene.item("filmSynthesisStrictUpperBoundCheck")
            original_bound = controller._build_synthesis_request().objectives[0].pointwise.upper_bound
            before = session.snapshot().design
            assert not checkbox.property("checked")
            scene.click(checkbox)
            assert checkbox.property("checked")
            assert controller._build_synthesis_request().objectives[0].pointwise.strict_upper is True
            assert controller._build_synthesis_request().objectives[0].pointwise.upper_bound == original_bound
            saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
            assert saved["objectives"][0]["pointwise"]["strict_upper"] is True
            assert controller.setSearchMode("fixed_topology")
            assert not checkbox.isVisible()
            ordinary = controller._build_request()
            assert not hasattr(ordinary, "strict_upper_bound_objective_indices")
            assert not controller.setSynthesisStrictUpperBound(False)
            assert controller.loadOptimizationConfiguration(saved)
            assert controller._build_synthesis_request().objectives[0].pointwise.strict_upper is True
            assert controller._build_synthesis_request().objectives[0].pointwise.upper_bound == original_bound
            assert session.snapshot().design == before
        finally:
            scene.close()


@pytest.mark.parametrize("upper_percent", [3.95, 0.])
def test_real_qml_physical_candidate_never_turns_tolerance_into_green_nominal_badge(app, tmp_path, upper_percent):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode("synthesis")
        assert controller.setObjectiveField("wavelength_min_nm", "550")
        assert controller.setObjectiveField("wavelength_max_nm", "550")
        assert controller.setObjectiveField("pointwise.upper_bound_percent", str(upper_percent))
        assert controller.setStructureMaterialSelected("acceptance-film", "film-set", True)
        assert controller.setSynthesisOption("max_evaluations", 12 if upper_percent == 0. else 3)
        assert controller.setSynthesisOption("max_cycles", 1)
        # Isolate this exact physical acceptance regression from the optional
        # scheduling quantum; default-hybrid real workers have a separate test.
        assert controller.setSynthesisOption("parallel_batch_size", 1)
        assert controller.setSynthesisOption("max_layers", 1)
        request = controller._build_synthesis_request()
        result = synthesize_film_design(service, request)
        assert result.candidate_design is not None
        assert result.validation_score is not None
        if upper_percent == 0.:
            assert len(result.candidate_design["front_layers"]) == 1
            assert 0. < result.validation_score["errors"][0].actual < .001
        else:
            assert result.candidate_design["front_layers"] == []
        assert result.targets_met is False and result.solver_acceptable is True
        payload = film_synthesis_result_to_payload("nominal-gui", result)
        view = synthesis_result_view(json.loads(json.dumps(payload)))
        before = session.snapshot().design
        controller._on_finished(controller._operation_serial, view, [], None)
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        try:
            QTest.qWait(30)
            assert controller.hasCandidate and not controller.targetsMet
            assert scene.window.property("statusSeverity") == "warning"
            assert "原门限名义验收：未通过" in controller.resultSummary
            assert "求解容差可接受：通过" in controller.resultSummary
            summary = scene.item("filmSynthesisAuditSummary").property("text")
            assert "原门限名义验收未通过" in summary
            assert "求解容差可接受：通过" in summary
            assert "不添加制造余量" in summary
            per_objective = controller.synthesisObjectiveAudit[0]["display"]
            expected_bound = f"= {upper_percent:g}%" if upper_percent == 0. else f"≤ {upper_percent:g}%"
            assert expected_bound in per_objective
            assert "原约束验收：未通过" in per_objective
            assert not controller.canApply
            assert session.snapshot().design == before
        finally:
            scene.close()


def test_current_serialized_history_replays_off_ui_thread_and_keeps_original_file(app, tmp_path, monkeypatch):
    import coat_sim.core.film_synthesis_jobs as jobs
    row = _current_row(tmp_path)
    path = tmp_path / "old-job.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    original = path.read_bytes()
    store = _FileStore(path)
    assert is_synthesis_result(row["result"])
    assert job_has_viewable_result(row)
    listed = deepcopy(row)
    _annotate_result_availability(tmp_path, [listed])
    assert listed["resultAvailable"] is True
    thread_ids = []
    replay = jobs.replay_synthesis_result

    def recorded_replay(value):
        thread_ids.append(threading.get_ident())
        return replay(value)

    monkeypatch.setattr(jobs, "replay_synthesis_result", recorded_replay)
    with ThreadPoolExecutor(max_workers=1) as pool:
        loaded, asset_path = pool.submit(_read_queue_result, None, store, row["job_id"]).result(timeout=15)
    assert isinstance(loaded, _SynthesisQueueResult) and asset_path == ""
    assert thread_ids and thread_ids[0] != threading.get_ident()
    assert loaded.source_job == json.loads(original)
    assert loaded.payload["result"]["targets_met"] is False
    assert loaded.payload["result"]["solver_acceptable"] is True
    assert loaded.payload["physical_replay_verification"]["read_only"] is True
    queue, inspected = _route_legacy(store, loaded)
    assert len(inspected) == 1 and "read-only" in queue.status
    assert inspected[0][1]["best_candidate_audit"]["targets_met"] is False
    assert path.read_bytes() == original


@pytest.mark.parametrize("tamper", ["persisted_source", "job", "payload", "replaced_with_normalized"])
def test_current_history_handoff_rejects_changed_source_cross_job_or_cache(tamper, app, tmp_path):
    row = _current_row(tmp_path)
    path = tmp_path / "old-job.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    store = _FileStore(path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        loaded, _ = pool.submit(_read_queue_result, None, store, row["job_id"]).result(timeout=15)
    assert isinstance(loaded, _SynthesisQueueResult)
    emitted_job, emitted_payload = None, None
    if tamper == "persisted_source":
        row["input_digest"] = "changed-frozen-input"
    elif tamper == "job":
        # Both the persisted and emitted job have a new identity; the cached
        # physical replay remains bound to the original job and must fail.
        row["job_id"] = emitted_job = "different-job"
    elif tamper == "payload":
        emitted_payload = deepcopy(loaded.payload)
        emitted_payload["review"]["warnings"].append("not from the verified cache")
    else:
        # Even replacing the persisted row with the upgraded payload cannot
        # erase the requirement to recheck the original replay source.
        row["result"] = deepcopy(loaded.payload)
    path.write_text(json.dumps(row), encoding="utf-8")
    queue, inspected = _route_legacy(store, loaded, emitted_job_id=emitted_job,
                                     emitted_payload=emitted_payload)
    assert inspected == []
    assert "Unable to open" in queue.status


@pytest.mark.parametrize("bad", [0, 1, "false", None])
def test_strict_upper_parser_never_silently_drops_invalid_flags(bad):
    from coat_sim.ui.film_optimization_quick_controller import _constraint_editor_default, _validate_editor_criteria
    pointwise = _constraint_editor_default(True, 3.)
    pointwise['strict_upper'] = bad
    with pytest.raises(ValueError, match='strict_upper'):
        _validate_editor_criteria(dict(pointwise=pointwise, mean=_constraint_editor_default(False), weight=1.))


def test_ordinary_v2_request_still_rejects_removed_synthesis_field():
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
    request, _result = _nominal_negative()
    ordinary = FilmOptimizationRequest(request.environment, request.design_revision, (), request.objectives)
    payload = serialize_request(ordinary)
    payload["strict_upper_bound_objective_indices"] = [0]
    with pytest.raises(ValueError, match="fields are invalid"):
        deserialize_request(payload)


def test_mcp_schema_describes_strict_operator_and_separate_solver_tolerance():
    from coat_sim.core.mcp_contracts import FILM_SYNTHESIS_PLAN_INPUT_SCHEMA
    properties = FILM_SYNTHESIS_PLAN_INPUT_SCHEMA["properties"]["payload"]["properties"]["request"]["properties"]
    assert 'strict_upper_bound_objective_indices' not in properties
    pointwise = properties['objectives']['items']['properties']['pointwise']['properties']
    assert pointwise['strict_upper']['type'] == 'boolean'
    assert 'strict <' in pointwise['strict_upper']['description']
    assert "does not relax nominal" in properties["acceptable_normalized_error"]["description"]


def test_real_project_inspector_retains_current_replay_read_only(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
    from coat_sim.project_session import ProjectSessionHost
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.core import material_business

    _service, library, _design = fixture(tmp_path / "global-optics")
    monkeypatch.setattr(material_business, "application_material_library", lambda: library)
    store = ProjectStore.create(tmp_path / "project", "Nominal history")
    host = ProjectSessionHost(lambda root: ProjectStore.open(root / MANIFEST_FILENAME))
    workspace = ProjectFilmWorkspace.for_project(host, host.open_project(store.root).binding)
    source = FilmDesignQuickController(service=workspace.service, edit_session=workspace.editor,
                                      project_context=workspace)
    controller = FilmOptimizationQuickController(source)
    scene = None
    try:
        row = _current_row(tmp_path)
        row["origin"] = {"project_id": workspace.binding.project_id,
                         "project_root": str(workspace.binding.root)}
        path = tmp_path / "legacy-project-job.json"
        path.write_text(json.dumps(row), encoding="utf-8")
        file_store = _FileStore(path)
        with ThreadPoolExecutor(max_workers=1) as pool:
            loaded, _ = pool.submit(_read_queue_result, None, file_store, row["job_id"]).result(timeout=15)
        before = workspace.editor.snapshot()
        queue = _Queue(file_store, loaded.source_job, loaded.payload)
        desktop = SimpleNamespace(open_project_module=lambda *_: None, controller_for=lambda *_: controller)
        _wire_project_queue_results(queue, workspace, SimpleNamespace(desktop=desktop))
        queue.filmSynthesisResultReady.emit(row["job_id"], loaded.payload)
        assert "read-only" in queue.status, queue.status
        assert controller.hasCandidate and controller.canSave
        assert not controller.targetsMet and not controller.canApply
        assert controller._result_application_eligible is False
        assert "Read-only history" in str(controller.property("synthesisAuditSummary"))
        assert "只读历史合成候选" in str(controller.property("progressText"))
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        assert scene.window.property("statusSeverity") == "warning"
        assert "求解容差可接受：通过" in scene.item("filmSynthesisAuditSummary").property("text")
        assert not controller.applyCandidate()
        assert not controller.confirmApplyCandidate()
        assert workspace.editor.snapshot().design == before.design
        assert workspace.editor.snapshot().design_revision == before.design_revision
        assert json.loads(path.read_text(encoding="utf-8"))["result"]["result"]["targets_met"] is False
    finally:
        controller.shutdown(1000)
        if scene is not None:
            scene.close()
        controller.deleteLater()
        source.markSourceClosed()
        source.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        host.close_project(workspace.binding, prepare_close=lambda *_: True)


@pytest.mark.parametrize("case", ["nominal_fail", "nominal_pass", "legacy", "wrong_binding", "cancelled", "failed"])
def test_actual_queue_qml_execution_success_is_not_nominal_success(app, tmp_path, case):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    import shiboken6
    from coat_sim.core.film_synthesis_jobs import build_job_payload
    from coat_sim.scheduler.jobs import JobSpec, canonical
    from coat_sim.ui.compute_queue import ComputeQueueView
    from coat_sim.ui.theme_service import ThemeService

    if case == "legacy":
        row = _legacy_row()
        request_payload = {"schema_version": 4, "kind": "coat_sim.film_synthesis_request",
            "request_id": row["result"]["request_id"],
            "request": row["result"]["result"]["frozen_request"], "physical_structures": []}
    else:
        if case == "nominal_fail":
            request, result = _nominal_negative()
        else:
            service, request = setup_problem(upper=.05, low=550., high=550., samples=1)
            result = synthesize_film_design(service, request)
            request = result.frozen_request
        if case in {"cancelled", "failed"}:
            result = replace(result, state=case, targets_met=False, termination_reason=case)
        payload = film_synthesis_result_to_payload("queue-nominal", result)
        row = {"job_id": "current-job", "kind": "film_synthesis", "state": "succeeded",
               "result": payload}
        if case in {"cancelled", "failed"}:
            row["state"] = case
        request_payload = film_synthesis_request_to_payload("queue-nominal", request)
    spec = JobSpec("film_synthesis", {"payload": build_job_payload({
        "payload": request_payload, "physical_structure_metadata": []})}, {})
    directory = tmp_path / "artifacts/runtime/scheduler/jobs" / row["job_id"]
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text(canonical(spec.to_dict()), encoding="utf-8")
    row["input_digest"] = spec.fingerprint if case != "wrong_binding" else "changed-input"
    row.update(summary="Nominal queue optics", stateLabel="执行完成" if row["state"] == "succeeded" else row["state"],
               durationLabel="未知", progress={}, finished_at=datetime.now(timezone.utc).isoformat())
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"] is (case not in {"wrong_binding", "legacy"})
    if case == "nominal_fail":
        assert row["result"]["result"]["state"] == "not_found" and row["state"] == "succeeded"
        assert row["synthesisNominalStatus"] == "failed"
        assert "原门限名义验收：未通过" in row["synthesisAcceptanceSummary"]
        assert "求解容差可接受：通过" in row["synthesisAcceptanceSummary"]
    elif case == "legacy":
        assert row["synthesisNominalStatus"] == "unknown"
        assert row["resultAvailable"] is False
    elif case == "wrong_binding":
        assert row["synthesisNominalStatus"] == "unknown"
        assert "绑定未核实" in row["synthesisAcceptanceSummary"]
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine)
    view = ComputeQueueView(engine, tmp_path)
    view.timer.stop()
    try:
        window = view.window
        assert isinstance(window, QQuickWindow)
        # Populate the actual queue page without launching any scheduler/IPC.
        view._jobs_by_bucket["recent"] = [row]
        view.changed.emit()
        window.show()
        QTest.qWait(60)
        def find(item, name):
            if item.objectName() == name:
                return item
            for child in item.childItems():
                found = find(child, name)
                if found is not None:
                    return found
            return None
        task = find(window.contentItem(), "queueTask_" + row["job_id"])
        assert task is not None
        expected = "success" if case == "nominal_pass" else "error" if case in {"cancelled", "failed"} else "warning"
        assert task.property("taskStateSeverity") == expected
        label = find(task, "queueSynthesisAcceptance")
        assert label is not None and label.isVisible()
        assert label.property("text") == row["synthesisAcceptanceSummary"]
    finally:
        view.shutdown()
        view.pool.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(view.window)
        shiboken6.delete(view.component)
        shiboken6.delete(view.context)
        shiboken6.delete(view)
        shiboken6.delete(engine)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_previous_schema_history_is_rejected_before_replay_and_preserves_file(app, tmp_path, monkeypatch):
    import coat_sim.core.film_synthesis_jobs as jobs
    row = _legacy_row(tmp_path)
    path = tmp_path / 'unsupported-job.json'
    path.write_text(json.dumps(row), encoding='utf-8')
    original = path.read_bytes()
    called = []
    def forbidden(*_args):
        called.append(True)
        raise AssertionError('unsupported history must not enter physical replay')
    # The public replay entry validates the schema; numerical replay must never start.
    monkeypatch.setattr(jobs, '_replay_synthesis_result', forbidden)
    assert not is_synthesis_result(row['result'])
    assert not job_has_viewable_result(row)
    _annotate_result_availability(tmp_path, [row])
    assert row['resultAvailable'] is False and row['synthesisNominalStatus'] == 'unknown'
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError):
            pool.submit(_read_queue_result, None, _FileStore(path), row['job_id']).result(timeout=15)
    assert called == [] and path.read_bytes() == original
