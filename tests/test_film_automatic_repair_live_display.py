"""V3 live repair: real TMM/audit, real editor transactions and private QML.

Most cases use a deterministic queued client with controlled proposals/local
endpoints and production physical evidence. The HTTP case uses the actual
SchedulerClient, daemon and worker without either control.
"""
from copy import deepcopy
from dataclasses import replace
from importlib import import_module
from threading import Event
from types import SimpleNamespace
from typing import Any, Callable, cast

import pytest
from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QSignalSpy
import shiboken6

from coat_sim.core import film_automatic_repair as automatic
from coat_sim.core import film_automatic_repair_jobs as live_jobs
from coat_sim.core import film_manufacturing_jobs as dispatch
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.film_optimization import FilmThicknessVariable, optimize_film_design
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from tests.film_structure_fixtures import fixture
from tests.test_film_manufacturing_quick_controller import HeldPool
from tests.test_film_synthesis_gui_review import _Scene, _wait
from tests.test_scheduler_service import stop_service

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


class StreamingPhysicsClient:
    """ACK first, then execute the real worker while forwarding every frame."""

    def __init__(self, root):
        self.root = root
        self.jobs: dict[str, Any] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: dict[str, Event] = {}
        self.progress: list[dict[str, Any]] = []
        self.after_frame: Callable[[dict[str, Any]], None] = lambda _frame: None

    def capabilities(self):
        return {"executors": [dispatch.EXECUTOR]}

    def submit(self, spec, *, job_id, metadata):
        parsed = JobSpec.parse_frozen(spec)
        self.jobs[job_id] = parsed
        self.events[job_id] = Event()
        self.rows[job_id] = dict(job_id=job_id, kind=parsed.executor,
                                input_digest=parsed.fingerprint, state="queued")
        return deepcopy(self.rows[job_id])

    def status(self, job_id):
        return deepcopy(self.rows[job_id])

    def wait_for_terminal(self, job_id, *, on_update, should_stop, wait_s):
        parsed = self.jobs[job_id]

        def report(frame):
            self.progress.append(deepcopy(frame))
            on_update({"progress": frame})
            QCoreApplication.processEvents()
            self.after_frame(frame)

        payload = dispatch.execute_job(parsed.parameters, self.events[job_id], report,
            self.root / (job_id + ".json"), lambda _path, _value: None)
        self.rows[job_id].update(state={"completed":"succeeded", "cancelled":"cancelled",
                                       "failed":"failed"}[payload["state"]], result=payload)
        return self.status(job_id)

    def cancel(self, job_id):
        self.events[job_id].set()
        return dict(job_id=job_id, cancel_requested=True)


@pytest.fixture
def live_gui(app, tmp_path, monkeypatch):
    service, library, design = fixture(tmp_path)
    template = design["front_layers"][0]
    design["front_layers"] = [dict(deepcopy(template), id=identifier,
        thickness=dict(value=thickness, unit="nm"))
        for identifier, thickness in (("left",100.), ("thin",7.), ("right",60.))]
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    panel = cast(FilmManufacturingQuickController, owner.manufacturingController)
    pool = HeldPool()
    client = StreamingPhysicsClient(tmp_path)
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: pool)
    panel.client_factory = lambda _root: client
    assert owner.setObjectiveField("wavelength_min_nm", 550.)
    assert owner.setObjectiveField("wavelength_max_nm", 550.)
    assert owner.setObjectiveField("pointwise.upper_bound_percent", 100.)
    assert panel.prepare(), panel.error
    h = SimpleNamespace(service=service, library=library, session=session, source=source,
        owner=owner, panel=panel, pool=pool, client=client, replacements=[], proposals=[7.5, 7.9, 6.], frontier=None)

    def proposals(_request, _base, _origins, frozen_source, _target, _floor):
        if h.frontier is not None:
            yield from h.frontier(frozen_source)
            return
        for index, thickness in enumerate(h.proposals):
            candidate = deepcopy(frozen_source)
            candidate["front_layers"][1]["thickness"]["value"] = thickness
            yield "controlled_partial_" + str(index), candidate, {
                row["id"]: [row["id"]] for row in frozen_source["front_layers"]}

    def local_endpoint(_fun, point, *, callback, constraints, **_kwargs):
        for constraint in constraints:
            constraint["fun"](point)
        callback(point)
        return SimpleNamespace(x=point.copy(), success=False, nit=1, status=9,
                               message="controlled nonstationary endpoint")

    optimize = import_module("scipy.optimize")
    monkeypatch.setattr(automatic, "_live_structures", proposals)
    monkeypatch.setattr(optimize, "minimize", local_endpoint)
    try:
        yield h
    finally:
        owner.shutdown(1000)
        source.markSourceClosed()
        for replacement in h.replacements:
            replacement.markSourceClosed()
            shiboken6.delete(replacement)
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(owner)
        shiboken6.delete(source)


def start_live(h):
    for key, value in (("reference_nm", "10"), ("thickness_tolerance_nm", "2"),
                       ("optical_tolerance_percentage_points", "1")):
        assert h.panel.setAutomaticOption(key, value)
    assert h.panel.startAutomatic(), h.panel.error


def thin_value(design):
    return next(row["thickness"]["value"] for row in design["front_layers"] if row["id"] == "thin")


@pytest.mark.parametrize("old_applied_main_result", [False, True])
def test_real_qml_user_cancel_applies_latest_complete_partial_once_and_undo_restores_source(
    live_gui, old_applied_main_result,
):
    h = live_gui
    h.proposals = [7.5, 7.9, 7.95]
    simulation = FilmSimulationQuickController(service=h.service, source_controller=h.source)
    simulation.setParameter("wavelength_min_nm", 540.)
    simulation.setParameter("wavelength_max_nm", 560.)
    simulation.setParameter("step_nm", 10.)
    assert h.panel.prepare(), h.panel.error
    main_request = h.owner._build_request(require_variables=False, fixed_topology=False)
    old_result = optimize_film_design(h.service, replace(main_request,
        variables=tuple(FilmThicknessVariable("front", row["id"], 0., None)
                        for row in h.session.snapshot().design["front_layers"]),
        max_iterations=1, max_evaluations=8))
    if old_applied_main_result:
        h.owner._result = old_result
        h.owner._candidate_applied = h.owner._candidate_saved = True
    before = deepcopy(h.session.snapshot().design)
    revision = h.session.snapshot().design_revision
    applied = QSignalSpy(h.source.optimizationApplied)
    plot_changes = QSignalSpy(h.owner.plotDataChanged)
    cancelled: list[int] = []
    observed = []
    scene = _Scene(h.owner, "FilmOptimizationWindow.qml")
    try:
        scene.click(scene.item("filmManufacturingOpenButton"))

        def after_frame(frame):
            if frame.get("stage") != "repair_live":
                return
            if frame.get("champion") is not None:
                observed.append(thin_value(frame["champion"]["design"]))
            current = frame.get("current")
            if (current is not None and thin_value(current["design"]) == 7.95
                    and not str(current.get("evidence_scope", "")).startswith("audit") and not cancelled):
                assert thin_value(frame["champion"]["design"]) == 7.9
                assert h.session.snapshot().design == before
                cancelled.append(frame["work"]["physical_calls"])
                scene.click(scene.item("filmManufacturingCancel"))

        h.client.after_frame = after_frame
        start_live(h)
        worker = h.pool.tasks[-1]
        h.pool.run()
        QCoreApplication.processEvents()
        assert not h.panel.error, h.panel.error
        assert cancelled and 7.5 in observed and 7.9 in observed, dict(
            observed=observed, status=h.panel.statusText,
            frames=[(f.get("stage"), f.get("seq"), f.get("current", {}).get("evidence_scope")
                     if f.get("current") else None, bool(f.get("champion"))) for f in h.client.progress])
        payload = h.panel._payload
        assert payload is not None and payload["schema_version"] == 3
        assert payload["state"] == "cancelled"
        best = payload["result"]["best"]
        assert best is not None and thin_value(best["design"]) == 7.9
        assert best["assessment"]["applicable_improvement"] is True
        assert best["assessment"]["thickness_complete"] is False
        assert best["assessment"]["all_requirements_met"] is False
        assert best["converged"] is False
        assert payload["result"]["work"]["physical_calls"] == cancelled[0]
        assert thin_value(h.session.snapshot().design) == 7.9
        assert h.session.snapshot().design_revision == revision + 1
        assert applied.count() == 1 and plot_changes.count() >= 1
        expected = h.service.simulate(h.session.snapshot().design, dict(
            wavelength_min_nm=550., wavelength_max_nm=550., samples=1))
        plot = cast(dict, h.owner.property("plotData"))
        assert plot["wavelength_nm"] == [550.]
        assert plot["candidate"] == pytest.approx(expected["R"])
        _wait(lambda: simulation.hasResult and not simulation.busy)
        assert simulation._design == h.session.snapshot().design
        expected_simulation = h.service.simulate(h.session.snapshot().design,
            cast(dict, simulation.property("settings")))
        assert simulation._result is not None
        assert simulation._result["R"] == pytest.approx(expected_simulation["R"])
        # A still-unapplied main optimizer proposal retains its own plot.
        h.owner._result = old_result
        h.owner._candidate_applied = h.owner._candidate_saved = False
        protected = cast(dict, h.owner.property("plotData"))
        assert protected["candidate"] == list(old_result.raw_coverage[0].actual_values)
        h.owner._candidate_applied = h.owner._candidate_saved = True
        assert h.source.canUndoOptimizedDesign
        assert {row["layer_id"] for row in cast(list[dict], h.owner.property("variables"))} == {"left", "thin", "right"}
        late = next(frame for frame in reversed(h.client.progress)
            if frame.get("current") is not None and thin_value(frame["current"]["design"]) == 7.95)
        worker.signals.progress.emit(worker.operation_id, late)
        worker.signals.finished.emit(worker.operation_id, payload, None, None)
        worker.signals.finished.emit(worker.operation_id, payload, None, None)
        QCoreApplication.processEvents()
        assert h.session.snapshot().design_revision == revision + 1 and applied.count() == 1
        assert cast(dict, h.owner.property("plotData"))["candidate"] == pytest.approx(expected["R"])
        assert h.source.undoOptimizedDesign()
        QCoreApplication.processEvents()
        assert h.session.snapshot().design == before
        assert not h.source.canUndoOptimizedDesign
        worker.signals.progress.emit(worker.operation_id, late)
        worker.signals.finished.emit(worker.operation_id, payload, None, None)
        QCoreApplication.processEvents()
        assert h.session.snapshot().design == before
        _wait(lambda: simulation.hasResult and not simulation.busy)
        assert simulation._design == before
        assert simulation._result is not None
        assert simulation._result["R"] == pytest.approx(h.service.simulate(before,
            cast(dict, simulation.property("settings")))["R"])
    finally:
        scene.close()
        simulation.close()
        if simulation._executor is not None:
            simulation._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(simulation)


@pytest.mark.parametrize("proposals", [[7.], [6.]])
def test_user_cancel_without_an_effective_improvement_never_replaces_source(live_gui, proposals):
    h = live_gui
    h.proposals = proposals
    before = deepcopy(h.session.snapshot().design)
    revision = h.session.snapshot().design_revision

    def stop(frame):
        if frame.get("stage") == "repair_live" and frame.get("current") is not None:
            h.panel.cancel()

    h.client.after_frame = stop
    start_live(h)
    h.pool.run()
    assert h.session.snapshot().design == before
    assert h.session.snapshot().design_revision == revision
    assert not h.source.canUndoOptimizedDesign


@pytest.mark.parametrize("change", ["edit", "source_close", "project_source_switch",
                                    "goals", "simulation_scope", "material"])
def test_invalidation_cancel_never_applies_audited_partial_to_old_or_new_source(live_gui, change, tmp_path):
    h = live_gui
    original = deepcopy(h.session.snapshot().design)
    expected_source = [original]
    changed: list[bool] = []
    replacement_sessions = []

    def invalidate(frame):
        if frame.get("stage") != "repair_live" or frame.get("champion") is None or changed:
            return
        changed.append(True)
        if change == "edit":
            h.source.markEditingDirty()
        elif change == "source_close":
            h.source.markSourceClosed()
        elif change == "project_source_switch":
            service, _library, design = fixture(tmp_path / "other-project")
            session = FilmDesignEditSession(service)
            assert session.request_replace_design(design)
            replacement = FilmDesignQuickController(service=service, edit_session=session)
            h.replacements.append(replacement)
            replacement_sessions.append((session, deepcopy(session.snapshot().design)))
            assert h.owner.reuseWindowOpen("", replacement)
        elif change == "goals":
            assert h.owner.setObjectiveField("pointwise.upper_bound_percent", 0.)
            expected_source[0] = deepcopy(h.session.snapshot().design)
            h.panel.cancel()
        elif change == "simulation_scope":
            assert h.session.set_workflow_configuration("simulation", {"parameters":{"angle_deg":33.}})
            expected_source[0] = deepcopy(h.session.snapshot().design)
        else:
            record = deepcopy(h.library.read("acceptance-film")["record"])
            record["optical_constants"][0]["n"] = [1.3] * len(record["optical_constants"][0]["n"])
            h.library.write(record, update=True)
            h.panel.cancel()

    h.client.after_frame = invalidate
    start_live(h)
    h.pool.run()
    assert changed and h.session.snapshot().design == expected_source[0]
    assert not h.source.canUndoOptimizedDesign
    for session, initial in replacement_sessions:
        assert session.snapshot().design == initial


def test_live_identity_and_sequence_reject_old_or_corrupted_packets_without_replacing_preview(live_gui):
    h = live_gui
    checked: list[bool] = []

    def inspect(frame):
        if frame.get("stage") != "repair_live" or frame.get("current") is None or checked:
            return
        checked.append(True)
        request = h.client.jobs[h.panel.jobId].parameters["payload"]["request"]
        canonical = live_jobs.validate_live_progress(frame, request, after_seq=frame["seq"] - 1)
        assert canonical["seq"] == frame["seq"]
        assert canonical["current"]["physical_hash"] == automatic.physical_design_hash(frame["current"]["design"])
        with pytest.raises(ValueError):
            live_jobs.validate_live_progress(frame, request, after_seq=frame["seq"])
        rows = deepcopy(h.panel.property("liveLayerRows"))
        for field in ("request_id", "request_hash", "source_fingerprint", "source_hash"):
            wrong = deepcopy(frame)
            wrong[field] = "foreign"
            with pytest.raises(ValueError):
                live_jobs.validate_live_progress(wrong, request, after_seq=-1)
            h.panel._on_progress(h.pool.tasks[-1].operation_id, wrong)
            assert h.panel.property("liveLayerRows") == rows
        wrong = deepcopy(frame)
        wrong["current"]["physical_hash"] = "0" * 64
        with pytest.raises(ValueError):
            live_jobs.validate_live_progress(wrong, request, after_seq=-1)
        h.panel._run_coordinator._on_worker_progress(h.pool.tasks[-1].operation_id - 1, frame)
        assert h.panel.property("liveLayerRows") == rows
        h.panel.sourceChanged()

    h.client.after_frame = inspect
    start_live(h)
    h.pool.run()
    assert checked and not h.source.canUndoOptimizedDesign


def test_manual_application_uses_the_confirmed_audited_hash_and_stops_the_live_round_once(live_gui, monkeypatch):
    h = live_gui
    before = deepcopy(h.session.snapshot().design)
    revision = h.session.snapshot().design_revision
    confirmation: list[str] = []
    committed: list[bool] = []

    def confirm_then_apply(frame):
        best = frame.get("champion") if frame.get("stage") == "repair_live" else None
        if best is None or committed:
            return
        value = thin_value(best["design"])
        if value == 7.5 and not confirmation:
            original_predicate = live_jobs.applicable_improvement
            parsed = []
            def counted(*args, **kwargs):
                parsed.append(True)
                return original_predicate(*args, **kwargs)
            with monkeypatch.context() as reads:
                reads.setattr(live_jobs, "applicable_improvement", counted)
                for _ in range(10):
                    assert h.panel.canApply
                    assert h.panel.selectedRouteWarning
                assert not parsed, "QML readiness reparsed already-validated optical arrays"
            assert h.panel.prepareApplication(), h.panel.error
            confirmation.append(h.panel.pendingApplicationHash)
            assert confirmation[0] == best["physical_hash"]
        elif value == 7.9 and confirmation:
            assert h.panel.pendingApplicationHash == confirmation[0]
            original_predicate = live_jobs.applicable_improvement
            rechecked = []
            def counted_final(*args, **kwargs):
                rechecked.append(True)
                return original_predicate(*args, **kwargs)
            with monkeypatch.context() as final:
                final.setattr(live_jobs, "applicable_improvement", counted_final)
                assert h.panel.applyPreparedCandidate(confirmation[0]), h.panel.error
                assert rechecked, "explicit Apply skipped its final evidence validation"
            committed.append(True)

    h.client.after_frame = confirm_then_apply
    start_live(h)
    h.pool.run()
    assert confirmation and committed
    assert thin_value(h.session.snapshot().design) == 7.5
    assert h.session.snapshot().design_revision == revision + 1
    assert not h.panel.applyPreparedCandidate(confirmation[0])
    assert h.session.snapshot().design_revision == revision + 1
    assert h.source.undoOptimizedDesign() and h.session.snapshot().design == before


def test_live_layer_rows_follow_ids_and_lineage_through_reorder_insert_merge_and_cancel(live_gui):
    h = live_gui
    before = deepcopy(h.session.snapshot().design)
    observed = set()

    def frontier(source):
        left, thin, right = deepcopy(source["front_layers"])
        thin["thickness"]["value"] = 7.5
        reordered = dict(deepcopy(source), front_layers=[right, thin, left])
        base = {row["id"]:[row["id"]] for row in source["front_layers"]}
        yield "reordered", reordered, deepcopy(base)
        inserted = deepcopy(reordered)
        inserted["front_layers"][1]["thickness"]["value"] = 7.9
        inserted["front_layers"].insert(0, dict(deepcopy(left), id="added",
            thickness=dict(value=8.5, unit="nm")))
        yield "inserted", inserted, dict(deepcopy(base), added=[])
        merged = deepcopy(source)
        merged["front_layers"] = [dict(deepcopy(left), thickness=dict(value=160., unit="nm")),
                                  dict(deepcopy(thin), thickness=dict(value=7.9, unit="nm"))]
        yield "merged", merged, dict(left=["left", "right"], thin=["thin"])

    h.frontier = frontier

    def inspect(frame):
        current = frame.get("current") if frame.get("stage") == "repair_live" else None
        if current is None or current["evidence_scope"] != "training":
            return
        rows = {row["row_id"]:row for row in cast(list[dict], h.panel.property("liveLayerRows"))}
        operator = current["operator"]
        if operator == "reordered":
            assert rows["right"]["original_nm"] == rows["right"]["current_nm"] == 60.
            assert rows["left"]["original_nm"] == rows["left"]["current_nm"] == 100.
            assert rows["thin"]["original_nm"] == 7. and rows["thin"]["current_nm"] == 7.5
        elif operator == "inserted":
            assert rows["added"]["original_nm"] is None and rows["added"]["current_nm"] == 8.5
            assert rows["added"]["current_origins"] == []
        elif operator == "merged":
            assert rows["left"]["current_nm"] == 160.
            assert set(rows["left"]["current_origins"]) == {"left", "right"}
            assert "结构" in rows["left"]["current_delta"]
            assert rows["right"]["original_nm"] == 60. and rows["right"]["current_nm"] is None
            assert frame["champion"]["design"]["front_layers"][0]["id"] == "added"
            h.panel.cancel()
        observed.add(operator)

    h.client.after_frame = inspect
    start_live(h)
    h.pool.run()
    assert not h.panel.error, h.panel.error
    assert {"reordered", "inserted", "merged"} <= observed, h.panel.statusText
    applied = h.session.snapshot().design
    assert len(applied["front_layers"]) == 4 and applied["front_layers"][0]["id"] == "added"
    assert {row["layer_id"] for row in cast(list[dict], h.owner.property("variables"))} == {row["id"] for row in applied["front_layers"]}
    assert h.source.undoOptimizedDesign() and h.session.snapshot().design == before


def test_actual_http_daemon_worker_qt_cancel_applies_audited_candidate_once(app, tmp_path):
    """Actual SchedulerClient HTTP/daemon/worker; no controlled client or solver."""
    service, _library, design = fixture(tmp_path)
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    panel = cast(FilmManufacturingQuickController, owner.manufacturingController)
    client = SchedulerClient(tmp_path)
    panel.client_factory = lambda _root: client
    scene = _Scene(owner, "FilmOptimizationWindow.qml")
    cancelled: list[int] = []
    try:
        assert owner.setObjectiveField("wavelength_min_nm", 500.)
        assert owner.setObjectiveField("wavelength_max_nm", 600.)
        assert owner.setObjectiveField("pointwise.upper_bound_percent", 100.)
        assert panel.prepare(), panel.error
        before = deepcopy(session.snapshot().design)
        revision = session.snapshot().design_revision
        applied = QSignalSpy(source.optimizationApplied)
        scene.click(scene.item("filmManufacturingOpenButton"))

        def on_progress():
            frame = panel._live_frame
            if panel.busy and frame is not None and frame.get("champion") is not None and not cancelled:
                cancelled.append(frame["seq"])
                scene.click(scene.item("filmManufacturingCancel"))

        panel.changed.connect(on_progress)
        for key, value in (("reference_nm", "110"), ("thickness_tolerance_nm", "1"),
                           ("optical_tolerance_percentage_points", "1")):
            assert panel.setAutomaticOption(key, value)
        assert panel.startAutomatic(), panel.error
        _wait(lambda: not panel.busy, seconds=45.)
        assert not panel.error, panel.error
        assert cancelled and panel._payload is not None
        payload = panel._payload
        row = client.status(panel.jobId)
        assert row["state"] == "cancelled", row["state"]
        assert payload["schema_version"] == 3 and payload["state"] == "cancelled"
        best = payload["result"]["best"]
        assert live_jobs.applicable_improvement(best, payload["result"]["source"], payload["request"])
        assert session.snapshot().design == best["design"]
        assert session.snapshot().design_revision == revision + 1 and applied.count() == 1
        assert source.canUndoOptimizedDesign
        plot = cast(dict, owner.property("plotData"))
        expected = service.simulate(session.snapshot().design, dict(
            wavelength_min_nm=plot["wavelength_nm"][0], wavelength_max_nm=plot["wavelength_nm"][-1],
            samples=len(plot["wavelength_nm"])))
        assert plot["candidate"] == pytest.approx(expected["R"])
        assert source.undoOptimizedDesign() and session.snapshot().design == before
    finally:
        scene.close()
        owner.shutdown(1000)
        source.markSourceClosed()
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        stop_service(client)
        shiboken6.delete(owner)
        shiboken6.delete(source)
