"""Real QML and bounded, display-only runtime transport contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from typing import Any, cast
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.film_candidate_display import CandidateDisplayObserver
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, FilmObjectiveCoverage
from coat_sim.core.film_optimization import FilmConditionError, FilmOptimizationObjective, FilmOptimizationResult
from coat_sim.ui.film_candidate_display_controller import FilmCandidateDisplayController
from tests.film_structure_fixtures import fixture
from tests.qt_visual_lookup import find_visual_item
from tests.test_film_optimization_restore_layout import click, optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def packet(config, *, sequence=2, terminal=False, control_sequence=0, job_id="display-job", digest="a" * 64):
    objective = FilmOptimizationObjective(550.0, 560.0, 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.1))
    values: list[dict[str, Any]] = []
    observer = CandidateDisplayObserver(context_id="frozen-display-context", objectives=(objective,),
        control_provider=lambda: {"enabled": True}, publish_callback=values.append,
        clock=iter(range(100)).__next__)
    design: dict[str, Any] = {"front_layers": [{"id": "test-layer", "material_id": "test-material",
                                "thickness": {"value": 80.0, "unit": "nm"}}], "back_layers": []}

    def rows(actual):
        return tuple(FilmConditionError(0, wave, 0.0, "unpolarized", "R", actual, None,
            None, 0.1, max(0.0, actual - 0.1), max(0.0, actual - 0.1),
            wavelength_min_nm=550., wavelength_max_nm=560.) for wave in (550., 555., 560.))

    observer.record(design, weighted_rms_error=0.1, max_normalized_error=0.1,
        condition_rows=rows(0.2), raw_coverage=(FilmObjectiveCoverage(0, 0., (550., 555., 560.), (0.2,) * 3),),
        physical_thickness_nm={"test-layer": 80.0}, is_best=True,
        source="ideal_sensitivity", guard_pass=True, sensitivity_objective=1.0,
        usable_candidate=True)
    changed = deepcopy(design)
    changed["front_layers"][0]["thickness"]["value"] = 90.0
    observer.record(changed, weighted_rms_error=0.0, max_normalized_error=0.0,
        condition_rows=rows(0.05), raw_coverage=(FilmObjectiveCoverage(0, 0., (550., 555., 560.), (0.05,) * 3),),
        physical_thickness_nm={"test-layer": 90.0},
        source="ideal_sensitivity", guard_pass=False, sensitivity_objective=2.0,
        usable_candidate=False)
    core = deepcopy(values[-1])
    core.update(sequence=sequence, terminal=terminal, state="completed" if terminal else "running")
    return {"schema_version": 1, "kind": "coat_sim.film_live_display", "job_id": job_id,
        "input_digest": digest, "generation": config["generation"],
        "control_sequence": control_sequence, "display": core}


class Controls:
    def __init__(self, *, held=False):
        self.calls = []
        self.entered, self.release = Event(), Event()
        if not held:
            self.release.set()

    def film_display_control(self, job_id, **kwargs):
        self.calls.append((job_id, deepcopy(kwargs), get_ident()))
        self.entered.set()
        assert self.release.wait(3)
        return dict(job_id=job_id, sequence=len(self.calls), **kwargs)


def drain(app):
    app.processEvents()
    QTest.qWait(5)
    app.processEvents()


def test_latest_only_mailbox_drops_old_attempts_and_preserves_terminal(app):
    live = cast(Any, FilmCandidateDisplayController())
    try:
        live.setEnabled(True)
        config = live.begin(("ordinary", 1))
        live.bind(("ordinary", 1), Controls(), "display-job", "a" * 64)
        changed = []
        live.changed.connect(lambda: changed.append(1))
        for sequence in range(1, 51):
            live.offer(("ordinary", 1), packet(config, sequence=sequence))
        live.offer(("ordinary", 1), packet(config, sequence=51, terminal=True))
        live.offer(("ordinary", 1), packet(config, sequence=52))
        assert not changed  # A slow GUI queues only one wake, not 52 frames.
        drain(app)
        assert len(changed) == 1 and live._last_sequence == 51
        assert not live.running and len(live.top10) == 2
        new = live.begin(("ordinary", 2))
        live.bind(("ordinary", 2), Controls(), "display-job", "a" * 64)
        live.offer(("ordinary", 1), packet(config, sequence=99, terminal=True))
        live.offer(("ordinary", 2), packet(config, sequence=99))
        drain(app)
        assert not live.top10 and live.running
        live.offer(("ordinary", 2), packet(new, sequence=1))
        drain(app)
        assert len(live.top10) == 2 and live._last_sequence == 1
    finally:
        live.shutdown()


def test_control_rpc_is_off_gui_thread_and_bursts_are_latest_only(app):
    live = cast(Any, FilmCandidateDisplayController())
    client = Controls(held=True)
    main_thread = get_ident()
    try:
        config = live.begin(1)
        live.bind(1, client, "display-job", "a" * 64)
        live.setEnabled(True)
        assert client.entered.wait(1)
        for index in range(100):
            live.setEnabled(bool(index % 2))
            live.setMetric("T" if index % 3 else "R")
        live.setEnabled(False)
        live.setMetric("T")
        assert len(client.calls) == 1
        assert live._control_inflight
        client.release.set()
        for _ in range(100):
            drain(app)
            if not live._control_inflight:
                break
        assert len(client.calls) == 2
        assert all(call[2] != main_thread for call in client.calls)
        assert client.calls[-1][1]["enabled"] is False
        assert client.calls[-1][1]["selection"]["metric"] == "T"
        assert client.calls[-1][1]["generation"] == config["generation"]
        live.end(1)
        live.setEnabled(True)
        drain(app)
        assert len(client.calls) == 2
    finally:
        client.release.set()
        live.shutdown()


def test_score_preferences_freeze_without_reordering_old_top10_and_source_fence(app):
    live = cast(Any, FilmCandidateDisplayController())
    current = [True]
    try:
        live.setEnabled(True)
        config = live.begin("run", source_current=lambda: current[0])
        live.bind("run", Controls(), "display-job", "a" * 64)
        live.offer("run", packet(config))
        drain(app)
        ranked = live.top10
        assert live.rankingDiffers and "guard" in live.bestSummary and "F 1" in live.bestSummary
        assert not live.setScoreField("weight_error", 0)
        assert live.setScoreField("weight_error", 12)
        assert live.scoreChangesPending and live.frozenScoreConfig["weight_error"] == 90
        assert live.top10 == ranked
        live.end("run")
        next_config = live.begin("next", source_current=lambda: current[0])
        assert next_config["score_config"]["weight_error"] == 12
        assert next_config["generation"] != config["generation"]
        live.bind("next", Controls(), "display-job", "a" * 64)
        current[0] = False
        live.offer("next", packet(next_config))
        drain(app)
        assert not live.top10
    finally:
        live.shutdown()


@pytest.mark.parametrize("size", [(1100, 760), (720, 500)])
def test_real_qml_toggle_canvas_legend_top10_and_score_next_run_do_not_touch_result(app, tmp_path, size):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        live = cast(Any, controller._live_display)
        original = deepcopy(session.snapshot().design)
        with window_runtime(app, controller) as (window, warnings):
            window.resize(*size)
            drain(app)
            toggle = window.findChild(QQuickItem, "filmCandidateLivePreviewToggle")
            assert toggle is not None and not toggle.property("checked")
            click(app, window, toggle)
            assert live.enabled
            config = live.begin(("optimization", 1))
            client = Controls()
            live.bind(("optimization", 1), client, "display-job", "a" * 64)
            live.offer(("optimization", 1), packet(config))
            drain(app)
            canvas = window.findChild(QQuickItem, "filmOptimizationComparisonCanvas")
            assert canvas is not None and canvas.property("seriesLegend") == "当前试探 / 优化器最佳"
            assert canvas.height() >= 90
            assert canvas.property("dataSnapshot")["current"]["actual"] == [0.05] * 3
            sidebar_toggle = window.findChild(QQuickItem, "filmOptimizationSidebarToggle")
            click(app, window, sidebar_toggle)
            assert not warnings, warnings
            repeater = window.findChild(QObject, "filmCandidateDisplayTop10")
            assert repeater is not None and repeater.property("count") == 2
            rank = find_visual_item(window, "filmCandidateDisplayRank_0")
            assert rank is not None and "S " in rank.property("text") and "ID " in rank.property("text")
            notice = window.findChild(QQuickItem, "filmCandidateDisplayRankNotice")
            assert "≠" in notice.property("text")
            assert controller._result is None and not controller.canApply
            assert session.snapshot().design == original
            # Closing the detail rail makes the real preview toggle unobscured.
            click(app, window, sidebar_toggle)
            click(app, window, toggle)
            assert not live.enabled
            drain(app)
            assert canvas.property("dataSnapshot") == {}
            assert len(live.top10) == 2
            assert controller._result is None
            assert not window.grabWindow().isNull()
            assert not warnings, warnings


def test_real_qml_preferences_editor_applies_only_to_next_attempt(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        live = cast(Any, controller._live_display)
        config = live.begin("run")
        with window_runtime(app, controller) as (window, warnings):
            window.resize(1200, 900)
            drain(app)
            click(app, window, window.findChild(QQuickItem, "filmOptimizationSidebarToggle"))
            advanced = window.findChild(QQuickItem, "filmCandidateScorePreferencesToggle")
            click(app, window, advanced)
            field = find_visual_item(window, "filmCandidateScore_weight_error")
            assert field is not None
            field.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(window, Qt.Key.Key_2)
            QTest.keyClick(window, Qt.Key.Key_5)
            QTest.keyClick(window, Qt.Key.Key_Return)
            drain(app)
            assert live.scoreConfig["weight_error"] == 25
            assert live.frozenScoreConfig["weight_error"] == config["score_config"]["weight_error"] == 90
            assert live.scoreChangesPending
            assert controller._result is None and not controller.canApply
            assert not warnings, warnings
        live.end("run")
        assert live.begin("next")["score_config"]["weight_error"] == 25


@pytest.mark.parametrize("size", [(1100, 760), (720, 500)])
def test_real_qml_formal_curves_survive_off_waiting_terminal_cancel_and_old_live_frames(app, tmp_path, monkeypatch, size):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        live = cast(Any, controller._live_display)
        original_design = deepcopy(session.snapshot().design)

        def no_simulation(*_args, **_kwargs):
            raise AssertionError("Display routing must not simulate or rebuild a spectrum")

        monkeypatch.setattr(service, "simulate", no_simulation)

        def install_formal(actual):
            rows = tuple(FilmConditionError(0, wave, 0., "unpolarized", "R", actual,
                None, None, 0.1, 0.2, 0.2) for wave in (550., 555., 560.))
            controller._result = FilmOptimizationResult("completed", 19, deepcopy(design),
                {design["front_layers"][0]["id"]: 100.}, 0.2, 0.2, 0.2, False, rows, 3, 7,
                raw_coverage=(FilmObjectiveCoverage(0, 0., (550., 555., 560.), (actual,) * 3),))
            controller._baseline = {(0, 0.): {"actual": [0.7, 0.6, 0.5]}}
            controller.plotDataChanged.emit()
            controller.changed.emit()
            return controller._result, controller.canApply, deepcopy(controller.plotData)

        formal_result, can_apply, formal_plot = install_formal(0.31)
        with window_runtime(app, controller) as (window, warnings):
            window.resize(*size)
            drain(app)
            canvas = window.findChild(QQuickItem, "filmOptimizationComparisonCanvas")
            title = window.findChild(QQuickItem, "filmOptimizationPlotBaselineTitle")
            status = window.findChild(QQuickItem, "filmCandidateLivePreviewStatus")
            toggle = window.findChild(QQuickItem, "filmCandidateLivePreviewToggle")
            assert canvas is not None and title is not None and status is not None and toggle is not None

            def assert_formal(*, waiting=False):
                drain(app)
                assert canvas.property("dataSnapshot") == formal_plot
                assert canvas.property("dataSnapshot")["baseline"] == [0.7, 0.6, 0.5]
                assert canvas.property("seriesLegend") == "原设计 / 候选"
                assert "原设计" in title.property("text") and "当前试探" not in title.property("text")
                if waiting:
                    assert "等待" in status.property("text") and "正式" in status.property("text")
                assert controller._result is formal_result and controller.canApply == can_apply
                assert formal_result.evaluations == 7 and session.snapshot().design == original_design

            def begin(attempt):
                config = live.begin(attempt)
                live.bind(attempt, Controls(), "display-job", "a" * 64)
                return config

            def assert_live():
                drain(app)
                assert canvas.property("dataSnapshot")["current"]["actual"] == [0.05] * 3
                assert canvas.property("dataSnapshot")["best"]["actual"] == [0.2] * 3
                assert canvas.property("seriesLegend") == "当前试探 / 优化器最佳"
                assert "当前试探与优化器最佳" in title.property("text")
                assert controller._result is formal_result and controller.canApply == can_apply

            # A default-off run must never cover already available official curves.
            assert not live.enabled
            config = begin("off-run")
            assert_formal()
            off_frame = packet(config)
            off_frame["display"]["enabled"] = False
            for role in ("current", "best"):
                off_frame["display"][role].update(availability="waiting", spectrum=None)
            live.offer("off-run", off_frame)
            assert_formal()
            assert len(live.top10) == 2
            live.end("off-run")
            assert_formal()
            assert len(live.top10) == 2

            # Waiting or a mismatched selected metric falls back, explicitly labelled.
            click(app, window, toggle)
            config = begin("on-off-run")
            assert_formal(waiting=True)
            live.offer("on-off-run", packet(config))
            assert_live()
            live.setMetric("T")
            assert_formal(waiting=True)
            live.setMetric("R")
            assert_live()
            click(app, window, toggle)
            assert_formal()
            assert len(live.top10) == 2
            live.end("on-off-run")
            assert_formal()

            # Final official results win even with the preview preference left on.
            click(app, window, toggle)
            config = begin("completed-run")
            live.offer("completed-run", packet(config))
            assert_live()
            formal_result, can_apply, formal_plot = install_formal(0.42)
            live.offer("completed-run", packet(config, sequence=3, terminal=True))
            assert_formal()
            assert live.enabled and len(live.top10) == 2
            live.end("completed-run")
            assert_formal()

            # Cancellation and late terminal frames cannot restore the live route.
            config = begin("cancelled-run")
            live.offer("cancelled-run", packet(config))
            assert_live()
            live.end("cancelled-run")
            assert_formal()
            live.offer("cancelled-run", packet(config, sequence=3, terminal=True))
            assert_formal()

            # A new run waits on its own valid frame, not the previous generation.
            new_config = begin("new-run")
            live.offer("cancelled-run", packet(config, sequence=99))
            live.offer("new-run", packet(config, sequence=99))
            assert_formal(waiting=True)
            assert not live.top10
            live.offer("new-run", packet(new_config))
            assert_live()
            live.end("new-run")
            assert_formal()
            assert len(live.top10) == 2
            assert not window.grabWindow().isNull()
            assert not warnings, warnings


def test_actual_ordinary_submission_freezes_display_config_and_never_mutates_physical_spec(app, tmp_path, monkeypatch):
    from coat_sim.ui import film_optimization_quick_controller as module
    from tests.test_film_optimization_run_contract import _HeldWorker, _Store

    stores = []
    def make_store(_root):
        store = _Store()
        stores.append(store)
        return store

    monkeypatch.setattr(module, "SchedulerClient", make_store)
    monkeypatch.setattr(module, "_FilmOptimizationTask", _HeldWorker)
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller._pool.start = lambda worker: worker.admit()
        from coat_sim.ui import film_optimization_run_coordinator as coordinator
        monkeypatch.setattr(coordinator.QThreadPool, "globalInstance", lambda: controller._pool)
        assert controller.setObjectiveField("wavelength_min_nm", 550.0)
        assert controller.setObjectiveField("wavelength_max_nm", 550.0)
        live = cast(Any, controller._live_display)
        live.setEnabled(True)
        assert live.setScoreField("weight_error", "25")
        assert controller.startOptimization(), controller.error
        drain(app)
        submitted_rows = [row for store in stores for row in store.submissions]
        assert submitted_rows, controller.error
        submitted = submitted_rows[-1]
        spec_before = deepcopy(submitted[0])
        frozen = deepcopy(submitted[2]["film_display_config"])
        assert frozen["enabled"] is True and frozen["score_config"]["weight_error"] == 25
        assert len(frozen["generation"]) == 32
        live.setScoreField("weight_error", "50")
        assert submitted[2]["film_display_config"] == frozen
        assert submitted[0] == spec_before and "film_display_config" not in submitted[0]
        _HeldWorker.instances[-1].finish(error={"state": "cancelled", "message": "test complete"})
        drain(app)
        assert not controller.busy
        assert controller.startOptimization(), controller.error
        drain(app)
        next_config = [row for store in stores for row in store.submissions][-1][2]["film_display_config"]
        assert next_config["score_config"]["weight_error"] == 50
        assert next_config["generation"] != frozen["generation"]
        _HeldWorker.instances[-1].finish(error={"state": "cancelled", "message": "test complete"})
        drain(app)


def test_completed_control_ack_rejects_stale_control_and_bad_job_identity(app):
    live = cast(Any, FilmCandidateDisplayController())
    client = Controls()
    try:
        config = live.begin("run")
        live.bind("run", client, "display-job", "a" * 64)
        live.setEnabled(True)
        for _ in range(100):
            drain(app)
            if not live._control_inflight:
                break
        assert live._minimum_control_sequence == 1
        for bad in (packet(config, sequence=20),
                    packet(config, sequence=20, control_sequence=1, job_id="another-job"),
                    packet(config, sequence=20, control_sequence=1, digest="b" * 64)):
            live.offer("run", bad)
        drain(app)
        assert not live.top10
        live.offer("run", packet(config, sequence=2, control_sequence=1))
        drain(app)
        assert len(live.top10) == 2
        live.clear_analysis()
        live.offer("run", packet(config, sequence=99, terminal=True, control_sequence=1))
        drain(app)
        assert not live.hasAttempt and not live.top10
    finally:
        live.shutdown()


def test_worker_forwards_display_as_separate_channel_after_exact_job_binding(app, tmp_path, monkeypatch):
    from coat_sim.ui import film_optimization_quick_controller as module
    from coat_sim.core.film_optimization import freeze_film_optimization_request
    from coat_sim.scheduler.desktop import RemoteCancelEvent
    from coat_sim.scheduler.jobs import JobSpec, freeze_spec

    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setObjectiveField("wavelength_min_nm", 550.0)
        assert controller.setObjectiveField("wavelength_max_nm", 550.0)
        request = freeze_film_optimization_request(service, controller._build_request())
        spec = freeze_spec(None, "film_optimization", {"request": request})
        digest = JobSpec.parse_frozen(spec).fingerprint
        live = cast(Any, controller._live_display)
        live.setEnabled(True)
        config = live.begin(("optimization", 1), objectives=request.objectives)
        client = cast(Any, Controls())
        client.status = lambda job: {"job_id": job, "input_digest": digest, "film_display_config": config}
        task = module._FilmOptimizationTask(service, request, client, "display-job",
            RemoteCancelEvent(client, "display-job"), 1)
        task._display_bind = lambda bound_client, job: live.bind_admitted(("optimization", 1), bound_client, job)
        task._display_callback = lambda value: live.offer(("optimization", 1), value)
        decoded = object()
        monkeypatch.setattr(task, "_decode_result", lambda _payload: decoded)
        finished, progress = [], []
        task.signals.finished.connect(lambda *args: finished.append(args))
        task.signals.progress.connect(lambda *_args: progress.append(1))

        def observe(client_arg, job, scalar_progress=None, *, disconnected=None, display=None):
            assert client_arg is client and job == "display-job" and callable(display)
            display(packet(config, digest=digest, terminal=True))
            return {"result": "separate terminal"}

        monkeypatch.setattr(module, "observe_job", observe)
        task.run()
        drain(app)
        assert finished[0][1] is decoded
        assert live._packet["terminal"] and len(live.top10) == 2
        assert controller._result is None


@pytest.mark.parametrize("new_off,always_stale", [(True, False), (False, False), (False, True)])
def test_stale_control_retries_only_latest_unchanged_intent_once(app, new_off, always_stale):
    class CASControls(Controls):
        def film_display_control(self, job_id, **kwargs):
            self.calls.append((job_id, deepcopy(kwargs), get_ident()))
            self.entered.set()
            assert self.release.wait(3)
            if len(self.calls) == 1 or always_stale:
                raise ValueError("film_display_control_stale")
            return dict(job_id=job_id, sequence=len(self.calls), **kwargs)

    live = cast(Any, FilmCandidateDisplayController())
    client = CASControls(held=True)
    try:
        live.begin("run")
        live.bind("run", client, "display-job", "a" * 64)
        live.setEnabled(True)
        assert client.entered.wait(1)
        if new_off:
            live.setEnabled(False)
        client.release.set()
        for _ in range(100):
            drain(app)
            if not live._control_inflight:
                break
        assert len(client.calls) == 2
        assert [call[1]["enabled"] for call in client.calls] == ([True, False] if new_off else [True, True])
        drain(app)
        assert bool(live.error) is always_stale
    finally:
        client.release.set()
        live.shutdown()


def test_angle_preparation_freezes_score_at_start_not_after_background_preparation(app, tmp_path, monkeypatch):
    from coat_sim.ui import film_optimization_quick_controller as module
    from coat_sim.ui import film_optimization_run_coordinator as coordinator
    from tests.test_film_optimization_run_contract import _HeldWorker, _Store

    store = _Store()
    monkeypatch.setattr(module, "SchedulerClient", lambda _root: store)
    monkeypatch.setattr(module, "_FilmOptimizationTask", _HeldWorker)
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setObjectiveField("wavelength_min_nm", 550.0)
        assert controller.setObjectiveField("wavelength_max_nm", 550.0)
        held: list[Any] = []
        controller._pool.start = held.append
        monkeypatch.setattr(coordinator.QThreadPool, "globalInstance", lambda: controller._pool)
        live = cast(Any, controller._live_display)
        live.setScoreField("weight_error", "25")
        request = controller._build_request(prepared_angles=((0.0,),))
        assert controller._start_angle_preparation(request)
        operation = controller._operation_serial
        assert controller._pending_angle_preparation["display_score_config"]["weight_error"] == 25
        live.setScoreField("weight_error", "50")
        frozen = module.freeze_film_optimization_request(service, request)
        spec = module.freeze_spec(None, "film_optimization", {"request": frozen})
        prepared = module._PreparedAngleGrid(frozen, spec)
        held[0].signals.finished.emit(operation, prepared, None, None)
        drain(app)
        assert len(held) == 2
        held[-1].admit()
        assert store.submissions[-1][2]["film_display_config"]["score_config"]["weight_error"] == 25
        assert live.scoreChangesPending and live.scoreConfig["weight_error"] == 50
        held[-1].finish(error={"state": "cancelled", "message": "test complete"})
        drain(app)


@pytest.mark.parametrize("enabled", [False, True])
def test_real_scheduler_client_terminal_side_channel_reaches_ui_without_polluting_scalar_progress(app, tmp_path, enabled):
    from coat_sim.scheduler.client import SchedulerClient
    from coat_sim.scheduler.desktop import observe_job

    live = cast(Any, FilmCandidateDisplayController())
    try:
        live.setEnabled(enabled)
        config = live.begin("actual-transport")
        final_packet = packet(config, terminal=True)
        if not enabled:
            final_packet["display"]["enabled"] = False
            for role in ("current", "best"):
                final_packet["display"][role].update(availability="waiting", spectrum=None)

        class EventTransport:
            def request(self, action, arguments):
                if action == "snapshot":
                    return {"sequence": 0, "jobs": [{"job_id": "display-job", "state": "running",
                        "input_digest": "a" * 64, "film_display_config": config,
                        "progress": {"iteration": 1}}]}
                if action == "events":
                    return {"sequence": 1, "reset": False,
                        "events": [{"job_id": "display-job", "state": "succeeded", "sequence": 1,
                            "input_digest": "a" * 64, "progress": {"iteration": 2},
                            "result": {"verified": "formal terminal result"}}],
                        "film_displays": [final_packet]}
                raise AssertionError("unexpected RPC " + action)

        client = SchedulerClient(tmp_path, transport=EventTransport())
        live.bind("actual-transport", client, "display-job", "a" * 64)
        scalar: list[dict[str, Any]] = []
        result = observe_job(client, "display-job", scalar.append,
            display=lambda value: live.offer("actual-transport", value), wait_s=0)
        live.end("actual-transport")
        drain(app)
        assert result == {"verified": "formal terminal result"}
        assert scalar == [{"iteration": 1}, {"iteration": 2}]
        assert live._packet.get("terminal") is True
        assert len(live.top10) == 2
        assert bool(live.plotData) is enabled
        assert not (tmp_path / "artifacts/runtime/scheduler/endpoint.json").exists()
    finally:
        live.shutdown()


def test_real_scheduler_client_controls_status_cas_and_rpc_stay_on_single_background_pump(app, tmp_path):
    from coat_sim.scheduler.client import SchedulerClient

    live = cast(Any, FilmCandidateDisplayController())
    try:
        config = live.begin("actual-cas")
        calls = []
        sequence = [0]

        class CASTransport:
            def request(self, action, arguments):
                calls.append((action, deepcopy(arguments), get_ident()))
                if action == "status":
                    return {"job_id": "display-job", "state": "running", "input_digest": "a" * 64,
                            "film_display_config": config, "film_display_control_sequence": sequence[0]}
                assert action == "film_display_control"
                assert arguments["expected_sequence"] == sequence[0]
                assert "sequence" not in arguments
                sequence[0] += 1
                return {"job_id": "display-job", "sequence": sequence[0],
                    "input_digest": "a" * 64, "generation": config["generation"],
                    "enabled": arguments["enabled"], "selection": arguments["selection"]}

        client = SchedulerClient(tmp_path, transport=CASTransport())
        live.bind("actual-cas", client, "display-job", "a" * 64)
        live.setEnabled(True)
        for _ in range(100):
            drain(app)
            if not live._control_inflight:
                break
        live.setEnabled(False)
        for _ in range(100):
            drain(app)
            if not live._control_inflight:
                break
        assert [call[0] for call in calls] == ["status", "film_display_control"] * 2
        assert all(call[2] != get_ident() for call in calls)
        assert sequence[0] == live._minimum_control_sequence == 2
        assert [call[1]["enabled"] for call in calls if call[0] == "film_display_control"] == [True, False]
        assert config["score_config"] == live.frozenScoreConfig
    finally:
        live.shutdown()


@pytest.mark.parametrize("bound,lost_ack", [(False, False), (True, False), (True, True)])
def test_real_remote_admission_binds_authoritative_transformed_digest_in_background(app, tmp_path, bound, lost_ack):
    from coat_sim.core.film_optimization import freeze_film_optimization_request
    from coat_sim.scheduler.client import SchedulerClient
    from coat_sim.scheduler.desktop import observe_job
    from coat_sim.scheduler.jobs import JobSpec, freeze_spec
    from coat_sim.scheduler.remote_client import RemoteServiceError, RemoteTransport
    from coat_sim.scheduler.remote_contract import validate_submission

    service, _library, design = fixture(tmp_path / "materials")
    with optimizer(service, design) as (controller, _source, _session):
        request = freeze_film_optimization_request(service, controller._build_request())
        spec = freeze_spec(None, "film_optimization", {"request": request})
        original = deepcopy(spec)
        original_digest = JobSpec.parse_frozen(spec).fingerprint
        live = cast(Any, controller._live_display)
        live.setEnabled(True)
        config = live.begin("remote", objectives=request.objectives)
        transport = cast(Any, RemoteTransport("https://service.test",
            device_id="cpu:default" if bound else None, pending_root=tmp_path / "pending"))
        blobs: dict[str, bytes] = {}
        admitted: dict[str, Any] = {}
        calls: list[tuple[str, Any, int]] = []

        def upload(body):
            digest = hashlib.sha256(body).hexdigest()
            blobs[digest] = body
            return {"sha256": digest, "bytes": len(body)}

        def respond(method, path, body, **_kwargs):
            calls.append((path, body.get("action"), get_ident()))
            if path == "/api/v1/jobs":
                job_id, parsed, metadata = validate_submission(body, blobs.__getitem__)
                actual_digest = JobSpec.parse_frozen(parsed.to_dict()).fingerprint
                admitted.update(job_id=job_id, input_digest=actual_digest, state="running",
                    film_display_config=metadata["film_display_config"],
                    input_summary={"device_id": transport.device_id,
                                   "external_spec_sha256": body["spec_blob"]["sha256"]})
                if bound:
                    assert parsed.inputs["device_binding"] == {"schema_version": 1, "device_id": "cpu:default"}
                    assert actual_digest != original_digest
                if lost_ack:
                    raise RemoteServiceError("simulated_lost_ack")
                return deepcopy(admitted)
            assert method == "POST" and path == "/api/v1/rpc"
            action = body["action"]
            if action == "status":
                assert body["arguments"]["job_id"] == admitted["job_id"]
                return deepcopy(admitted)
            if action == "snapshot":
                return {"sequence": 0, "jobs": [deepcopy(admitted)]}
            assert action == "events"
            return {"sequence": 1, "reset": False,
                "events": [{**admitted, "state": "succeeded", "sequence": 1, "result": {"verified": True}}],
                "film_displays": [packet(config, job_id=admitted["job_id"],
                    digest=admitted["input_digest"], terminal=True)]}

        transport.upload_blob, transport._json_call = upload, respond
        client = SchedulerClient(tmp_path / "client", transport=transport)

        def run():
            receipt = client.submit(spec, job_id="remote-display", metadata={"film_display_config": config})
            assert live.bind_admitted("remote", client, receipt["job_id"])
            # A local pre-transport hash is not accepted once the admitted identity differs.
            if original_digest != admitted["input_digest"]:
                live.offer("remote", packet(config, job_id="remote-display", digest=original_digest))
                assert live._pending_packet is None
            for bad in (packet(config, job_id="wrong-job", digest=admitted["input_digest"]),
                        packet(config, job_id="remote-display", digest="b" * 64),
                        packet(dict(config, generation="f" * 32), job_id="remote-display",
                               digest=admitted["input_digest"])):
                live.offer("remote", bad)
                assert live._pending_packet is None
            return observe_job(client, "remote-display", display=lambda value: live.offer("remote", value), wait_s=0)

        with ThreadPoolExecutor(max_workers=1) as worker:
            assert worker.submit(run).result(timeout=5) == {"verified": True}
        live.end("remote")
        drain(app)
        assert live._binding[2] == admitted["input_digest"]
        assert live._packet["terminal"] and len(live.top10) == 2 and live.plotData
        assert calls and all(call[2] != get_ident() for call in calls)
        assert spec == original and controller._result is None and not controller.canApply
        assert not (tmp_path / "client/artifacts/runtime/scheduler/endpoint.json").exists()


@pytest.mark.parametrize("invalid", ["job", "digest", "missing_config", "partial_score", "generation", "score", "enabled", "selection"])
def test_admitted_binding_rejects_mismatched_receipts_including_reused_old_generation(app, invalid):
    live = cast(Any, FilmCandidateDisplayController())
    try:
        config = live.begin("new-attempt")
        row = {"job_id": "display-job", "input_digest": "a" * 64, "film_display_config": deepcopy(config)}
        if invalid == "job":
            row["job_id"] = "another-job"
        elif invalid == "digest":
            row["input_digest"] = "not-a-digest"
        elif invalid == "missing_config":
            row.pop("film_display_config")
        elif invalid == "partial_score":
            row["film_display_config"]["score_config"] = {}
        elif invalid == "generation":
            row["film_display_config"]["generation"] = "e" * 32
        elif invalid == "score":
            row["film_display_config"]["score_config"]["weight_error"] = 25
        elif invalid == "enabled":
            row["film_display_config"]["enabled"] = not config["enabled"]
        else:
            row["film_display_config"]["selection"]["metric"] = "T"
        client = cast(Any, Controls())
        client.status = lambda _job: row
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert not worker.submit(live.bind_admitted, "new-attempt", client, "display-job").result(timeout=3)
        live.offer("new-attempt", packet(config, terminal=True))
        drain(app)
        assert live._binding is None and not live.top10 and not client.calls
    finally:
        live.shutdown()


@pytest.mark.parametrize("interruption", ["source_epoch", "cancelled_attempt", "new_attempt"])
def test_admitted_status_race_preserves_source_epoch_cancel_and_new_attempt_fences(app, tmp_path, monkeypatch, interruption):
    from coat_sim.ui import film_optimization_quick_controller as module
    from coat_sim.ui import film_optimization_run_coordinator as coordinator
    from tests.test_film_optimization_run_contract import _HeldWorker, _Store

    store = _Store()
    monkeypatch.setattr(module, "SchedulerClient", lambda _root: store)
    monkeypatch.setattr(module, "_FilmOptimizationTask", _HeldWorker)
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, source, _session):
        controller._pool.start = lambda worker: worker.admit()
        monkeypatch.setattr(coordinator.QThreadPool, "globalInstance", lambda: controller._pool)
        assert controller.setObjectiveField("wavelength_min_nm", 550.0)
        assert controller.setObjectiveField("wavelength_max_nm", 550.0)
        live = cast(Any, controller._live_display)
        live.setEnabled(True)
        assert controller.startOptimization(), controller.error
        drain(app)
        task = _HeldWorker.instances[-1]
        _spec, job_id, metadata = store.submissions[-1]
        config = deepcopy(metadata["film_display_config"])
        attempt = live._attempt
        entered, release = Event(), Event()

        def status(_job):
            entered.set()
            assert release.wait(3)
            return {"job_id": job_id, "input_digest": "a" * 64, "film_display_config": config}

        cast(Any, store).status = status
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(task._display_bind, store, job_id)
            assert entered.wait(1)
            if interruption == "source_epoch":
                source.pendingEditEpoch += 1
            elif interruption == "cancelled_attempt":
                live.end(attempt)
            else:
                live.begin(("optimization", task.operation_id + 1))
            release.set()
            pending.result(timeout=3)
        task._display_callback(packet(config, job_id=job_id, terminal=True))
        drain(app)
        assert not live.top10 and controller._result is None and not controller.canApply
        if interruption != "source_epoch":
            assert live._binding is None
        task.finish(error={"state": "cancelled", "message": "test complete"})
        drain(app)
