"""Manufacturing UI contracts with real optics and real offscreen Qt inputs.

Only IPC is held in memory. Production freezing, worker execution and evidence validation are
used; these tests are not native Windows or physical instrument acceptance.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import json
import os
from threading import Event
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QThreadPool, Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.core import film_manufacturing_jobs as jobs
from coat_sim.core import film_automatic_repair_jobs as automatic_jobs
from coat_sim.core.film_design_service import simulation_settings
from coat_sim.scheduler.jobs import JobSpec
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


class HeldPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def waitForDone(self, _timeout):
        return True

    def run(self):
        self.tasks[-1].run()


class PhysicsClient:
    def __init__(self, root):
        self.root, self.rows, self.submissions, self.progress = root, {}, [], []
        self.executors = [jobs.EXECUTOR]
        self.cancel_stage = ""
        self.tamper = self.unknown = False

    def capabilities(self):
        return {"executors": self.executors}

    def submit(self, spec, *, job_id, metadata):
        parsed = JobSpec.parse_frozen(spec)
        self.submissions.append(deepcopy(spec))
        if self.unknown:
            raise OSError("synthetic missing ACK")
        cancel = Event()

        def report(value):
            self.progress.append(value)
            if self.cancel_stage and value.get("stage") == self.cancel_stage:
                cancel.set()

        payload = jobs.execute_job(parsed.parameters, cancel, report,
                                   self.root / "checkpoint.json", lambda _p, _v: None)
        if self.tamper and payload.get("result"):
            payload["result"]["source"]["training_score"]["nominal_pass"] = "passed"
        state = {"completed": "succeeded", "cancelled": "cancelled", "failed": "failed"}[payload["state"]]
        self.rows[job_id] = {"job_id": job_id, "kind": parsed.executor,
                            "input_digest": parsed.fingerprint, "state": state, "result": payload}
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
def harness(app, tmp_path, monkeypatch):
    service, library, design = fixture(tmp_path)
    with optimizer(service, design) as (owner, source, session):
        workflow_configuration = session.workflow_configuration
        monkeypatch.setattr(
            session, "workflow_configuration",
            lambda scope: {} if scope == "simulation" else workflow_configuration(scope),
        )
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = owner.manufacturingController
        pool, client = HeldPool(), PhysicsClient(tmp_path)
        monkeypatch.setattr(QThreadPool, "globalInstance", lambda: pool)
        panel.client_factory = lambda _root: client
        assert panel.prepare(), panel.error
        yield SimpleNamespace(owner=owner, panel=panel, source=source, session=session,
                              service=service, library=library, design=design,
                              pool=pool, client=client, tmp_path=tmp_path)


@pytest.fixture
def legacy_v2_harness(harness, monkeypatch):
    """Exercise preserved v2 jobs through the real worker, not the new live mode."""
    monkeypatch.setattr(automatic_jobs, "make_live_request_payload", automatic_jobs.make_request_payload)
    return harness


def configure(h):
    panel = h.panel
    assert panel.setLayerSelected(panel.layerRows[0]["layer_id"], True)
    for key, value in (("reference_nm", "110"), ("refinement_evaluations_per_route", "2"),
                       ("max_iterations_per_route", "2"), ("seconds_per_route", "10")):
        assert panel.setOption(key, value)


def run(h):
    configure(h)
    assert h.panel.start(), h.panel.error
    h.pool.run()
    assert not h.panel.busy, h.panel.error
    assert h.panel.hasResult, h.panel.error
    return h.panel._payload


def test_explicit_empty_drafts_and_stable_layer_selection(harness):
    h = harness
    assert all(value == "" for value in h.panel.options.values())
    assert not any(row["selected"] for row in h.panel.layerRows)
    assert not h.panel.start()
    assert "明确填写" in h.panel.error
    configure(h)
    old_id = h.panel.layerRows[0]["layer_id"]
    h.session.design["front_layers"].append({**deepcopy(h.design["front_layers"][0]), "id": "new-id"})
    h.session.design_revision += 1
    assert h.panel.prepare()
    assert h.panel.layerRows[0]["layer_id"] == old_id and h.panel.layerRows[0]["selected"]
    assert h.panel.layerRows[1]["selected"] is False
    assert h.panel.options["reference_nm"] == "110"


def test_invalid_input_retained_and_no_hidden_unlimited(harness):
    h = harness
    configure(h)
    for invalid in ("", "nan", "inf", "-1", "1.2"):
        assert h.panel.setOption("max_iterations_per_route", invalid)
        assert not h.panel.start()
        assert h.panel.options["max_iterations_per_route"] == invalid
        assert not h.client.submissions


@pytest.mark.parametrize("name", ("refinement_evaluations_per_route", "max_iterations_per_route"))
@pytest.mark.parametrize("literal", ("2.0000000000000001", "9007199254740993.1",
    "1.00000000000000000000001", "1e-9999999", "1e309", "1e99999999999999999", "sNaN"))
def test_integer_quota_rejects_exact_nonintegral_or_nonfinite_drafts(harness, name, literal):
    h = harness
    configure(h)
    assert h.panel.setOption(name, literal)
    assert not h.panel.finishOptionEdit(name)
    assert not h.panel.start()
    assert h.panel.options[name] == literal
    assert not h.client.submissions


@pytest.mark.parametrize("name", ("refinement_evaluations_per_route", "max_iterations_per_route"))
@pytest.mark.parametrize("literal,expected", (("2", 2), ("2.0", 2), ("2e0", 2),
    ("1e2", 100), ("0002", 2), ("9007199254740993", 9007199254740993),
    ("9.007199254740993e15", 9007199254740993)))
def test_integer_quota_preserves_exact_value_and_valid_scientific_notation(harness, name, literal, expected):
    h = harness
    configure(h)
    assert h.panel.setOption(name, literal)
    assert h.panel.finishOptionEdit(name), h.panel.error
    parsed = h.panel._validated_options()[name]
    assert type(parsed) is int and parsed == expected
    assert h.panel.options[name] == literal
    assert not h.client.submissions


def test_real_worker_original_a_b_full_evidence_and_report_roundtrip(harness):
    h = harness
    original = deepcopy(h.session.snapshot().design)
    payload = run(h)
    assert payload["state"] == "completed"
    assert h.session.snapshot().design == original
    assert h.panel.selectedRoute == "" and not h.panel.canApply
    assert len(h.panel.comparisonRows) == 3
    for row in h.panel.comparisonRows:
        assert "D 总厚度" in row["details"]
        assert "独立审计" in row["details"]
        assert "诊断固定网格均值" in row["details"]
        assert "采样最小" in row["details"] and "采样最大" in row["details"]
        assert "solver 分类" in row["details"]
    report = h.tmp_path / "complete.json"
    assert h.panel.saveReport(str(report))
    assert json.loads(report.read_text()) == payload
    assert not h.panel.saveReport(str(report))  # explicit overwrite, never implicit
    assert h.panel.saveReport(str(report), True)
    assert h.panel.setSelectedRoute("raise_selected")
    assert h.panel.canApply, h.panel.error


def test_apply_is_explicit_audited_even_when_original_target_fails(harness, monkeypatch):
    h = harness
    assert h.owner.setObjectiveField("pointwise.upper_bound_percent", 0.)
    assert h.panel.prepare()
    run(h)
    assert h.panel._payload["result"]["routes"][0]["champion"]["audit_score"]["nominal_pass"] is False
    calls: list[tuple[object, ...]] = []

    def capture_apply(*args: object) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(h.source, "applySynthesizedDesign", capture_apply, raising=False)
    assert h.panel.setSelectedRoute("raise_selected")
    assert h.panel.canApply
    assert "未通过原始光学目标" in h.panel.selectedRouteWarning
    assert not calls
    assert h.panel.applySelectedRoute(), h.panel.error
    assert len(calls) == 1 and len(calls[0]) == 4
    assert calls[0][1:3] == (h.panel._context["revision"], h.panel._context["epoch"])
    assert calls[0][3] == h.panel._context["editable_snapshot"]
    assert not h.panel.applySelectedRoute() and len(calls) == 1


def test_pending_edit_goals_material_and_revision_fences(harness):
    h = harness
    run(h)
    assert h.panel.setSelectedRoute("raise_selected")
    assert h.panel.canApply
    h.source._pending_edit_epoch = 9
    # Real source interface is the epoch property; changing the context also
    # independently proves no direct apply can skip the revision fence.
    h.panel._context["epoch"] += 1
    assert not h.panel.canApply and not h.panel.applySelectedRoute()
    h.panel._context["epoch"] -= 1
    assert h.owner.setObjectiveField("wavelength_max_nm", 560.)
    assert not h.panel.canApply


def test_cancel_before_admission_and_source_change_late_result(harness):
    h = harness
    configure(h)
    assert h.panel.start()
    h.panel.cancel()
    assert h.panel.busy  # intent is not terminal completion
    h.pool.run()
    assert not h.panel.busy and not h.client.submissions
    assert not h.panel.canApply
    assert h.panel.prepare()
    assert h.panel.start(), h.panel.error
    h.panel.sourceChanged()
    assert not h.panel.prepare()  # Busy old attempts cannot be refreshed into applicability.
    h.pool.run()
    assert not h.panel.busy and not h.panel.canApply
    assert not h.client.submissions
    assert not h.panel.hasResult
    assert h.panel.prepare(), h.panel.error
    assert h.panel.canStartAutomatic


def test_tamper_error_unknown_submission_and_detach_fences(harness):
    h = harness
    h.client.tamper = True
    configure(h)
    assert h.panel.start()
    h.pool.run()
    assert h.panel.error and not h.panel.hasResult
    h.client.tamper = False
    assert h.panel.start()
    worker = h.pool.tasks[-1]
    h.panel.detach()
    worker.run()
    assert not h.panel.hasResult and not h.panel.canApply


def test_verified_history_has_no_apply_context(harness):
    h = harness
    payload = run(h)
    h.panel.load_verified_history(payload, "persisted-id")
    assert h.panel.jobId == "persisted-id" and h.panel.hasResult
    assert h.panel.setSelectedRoute("raise_selected")
    assert not h.panel.canApply and not h.panel.applySelectedRoute()


def _visual(root, name):
    if root.objectName() == name:
        return root
    for child in root.childItems():
        match = _visual(child, name)
        if match is not None:
            return match
    return None


def _type(window, text):
    for character in text:
        QTest.keyClick(window, Qt.Key(ord(character.upper())))


def _click(window, item):
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.qWait(10)


@pytest.mark.parametrize("mode,scale", [("light", 1.0), ("dark", 1.0), ("light", 1.25)])
def test_real_qml_inputs_blank_preservation_run_cancel_and_responsive_geometry(
    harness, app, mode, scale
):
    h = harness
    assert h.owner.setSearchMode("automatic_repair")
    with window_runtime(app, h.owner) as (window, warnings):
        from coat_sim.ui.theme_service import ThemeService

        window.resize(1100, 1100)
        ThemeService.instance().configure(mode=mode, font_scale=scale, density_scale=scale)
        tabs = window.findChild(object, "filmOptimizationSetupTabs")
        assert tabs is not None and tabs.setProperty("currentIndex", 2)
        QTest.qWait(20)
        root = window.findChild(object, "filmManufacturingAlgorithmOptions")
        assert root is not None and root.property("visible")
        assert window.findChild(object, "filmManufacturingRepairDialog") is None
        field = _visual(window.contentItem(), "filmManufacturing_lower_limit_nm")
        assert field is not None and field.property("text") == ""
        assert window.grabWindow().save(str(h.tmp_path / "manufacturing-inline-two-inputs.png"))
        _click(window, field)
        _type(window, "nan")
        assert h.panel.automaticOptions["lower_limit_nm"] == "nan"
        QTest.keyClick(window, Qt.Key.Key_Tab)
        assert h.panel.automaticOptions["lower_limit_nm"] == "nan"
        assert field.property("text") == "nan"
        assert not h.panel.finishAutomaticOptionEdit("lower_limit_nm")
        start = _visual(window.contentItem(), "filmOptimizationStartButton")
        assert start is not None and not start.isEnabled()
        assert "有限正数" in h.panel.error
        window.resize(720, 580)
        QTest.qWait(30)
        assert root.property("width") <= window.width()
        for name in ("filmOptimizationStartButton", "filmOptimizationCancelButton"):
            button = _visual(window.contentItem(), name)
            assert button is not None
            right = button.mapToScene(QPointF(button.width(), 0)).x()
            assert 0 <= right <= window.width(), (name, right)
        window.resize(1100, 1100)
        QTest.qWait(30)
        assert root.property("width") > 250
        for name, value in (("lower_limit_nm", "109"),
                            ("optical_tolerance_percentage_points", "0.1")):
            editor = _visual(window.contentItem(), "filmManufacturing_" + name)
            _click(window, editor)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            _type(window, value)
            QTest.keyClick(window, Qt.Key.Key_Tab)
            assert h.panel.automaticOptions[name] == value
        _click(window, _visual(window.contentItem(), "filmOptimizationStartButton"))
        assert h.panel.busy, repr({
            "can_start": h.owner.canStart,
            "can_start_auto": h.panel.canStartAutomatic,
            "panel_error": h.panel.error,
            "owner_error": h.owner.error,
            "options": h.panel._options,
            "legacy": h.panel._legacy_mode,
            "prepared": h.panel._prepared_signature is not None,
            "stale": h.panel._stale,
            "panel_flags": (h.panel.busy, h.panel._closed, h.panel._unknown_submission,
                            h.panel.owner.busy, h.panel.owner._shutting_down,
                            h.panel.owner._source_closed, h.panel.owner._source_stale,
                            h.panel._source_invalidated.is_set(), bool(h.panel._rows)),
            "validated": h.panel._validated_options(),
        })
        assert not _visual(window.contentItem(), "filmOptimizationStartButton").isEnabled()
        _click(window, _visual(window.contentItem(), "filmOptimizationCancelButton"))
        assert h.panel.busy  # only a request, not a pretend terminal state
        h.pool.run()
        assert not h.panel.busy and not h.client.submissions
        assert h.owner.setSearchMode("fixed_topology")
        QTest.qWait(20)
        assert not root.property("visible")
        assert h.owner.setSearchMode("automatic_repair")
        QTest.qWait(20)
        assert root.property("visible")
        assert h.panel.automaticOptions["lower_limit_nm"] == "109"
        assert not warnings, warnings


@contextmanager
def _readable_offscreen_font():
    """Only test screenshots need a font on Windows' empty offscreen font DB."""
    from coat_sim.ui.theme_service import ThemeService

    theme = ThemeService.instance()
    previous_family = theme._font_family_override
    loaded = []
    try:
        if os.name == 'nt' and not QFontDatabase.families():
            font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
            assert font_id >= 0
            loaded.append(font_id)
            theme.configure(font_family='Microsoft YaHei')
        yield
    finally:
        theme.configure(font_family=previous_family)
        for font_id in loaded:
            QFontDatabase.removeApplicationFont(font_id)


@pytest.mark.parametrize("scale", [1.0, 1.4])
@pytest.mark.parametrize("legacy_drafts", [False, True])
def test_two_algorithm_inputs_real_qml_mapping_readability_and_audited_result(
    harness, app, scale, legacy_drafts
):
    h = harness
    if legacy_drafts:
        assert h.panel.setLayerSelected(h.panel.layerRows[0]["layer_id"], True)
        assert h.panel.setOption("refinement_evaluations_per_route", "2")
        assert h.panel._legacy_mode
    assert h.owner.setSearchMode("automatic_repair")
    with _readable_offscreen_font(), window_runtime(app, h.owner) as (window, warnings):
        from coat_sim.ui.theme_service import ThemeService

        window.resize(1100, 1200)
        ThemeService.instance().configure(mode="light", font_scale=scale, density_scale=1.0)
        tabs = window.findChild(object, "filmOptimizationSetupTabs")
        assert tabs is not None and tabs.setProperty("currentIndex", 2)
        QTest.qWait(30)
        assert set(h.panel.automaticOptions) == {
            "lower_limit_nm",
            "optical_tolerance_percentage_points",
        }
        panel = window.findChild(object, "filmManufacturingAlgorithmOptions")
        assert panel is not None and panel.property("visible")
        assert window.findChild(object, "filmManufacturingRepairDialog") is None
        original = deepcopy(h.session.snapshot().design)
        for name, literal in (
            ("lower_limit_nm", "109"),
            ("optical_tolerance_percentage_points", "1"),
        ):
            field = _visual(window.contentItem(), "filmManufacturing_" + name)
            label = _visual(window.contentItem(), "filmManufacturingLabel_" + name)
            help_text = _visual(window.contentItem(), "filmManufacturingHelp_" + name)
            assert field is not None and label is not None and help_text is not None
            assert help_text.width() > 100 and help_text.height() >= help_text.implicitHeight() - 1
            _click(window, field)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            _type(window, literal)
            QTest.keyClick(window, Qt.Key.Key_Tab)
        for name in (
            "filmManufacturing_reference_nm",
            "filmManufacturing_thickness_tolerance_nm",
            "refinement_evaluations_per_route",
            "max_iterations_per_route",
            "seconds_per_route",
        ):
            assert _visual(window.contentItem(), name) is None
        assert not h.panel._legacy_mode
        assert window.grabWindow().save(str(h.tmp_path / f"auto-repair-inline-readable-{scale}.png"))
        _click(window, _visual(window.contentItem(), "filmOptimizationStartButton"))
        assert h.panel.busy, repr({
            "can_start": h.owner.canStart,
            "can_start_auto": h.panel.canStartAutomatic,
            "panel_error": h.panel.error,
            "owner_error": h.owner.error,
            "options": h.panel._options,
            "legacy": h.panel._legacy_mode,
            "prepared": h.panel._prepared_signature is not None,
            "stale": h.panel._stale,
        })
        h.pool.run()
        QCoreApplication.processEvents()
        assert h.panel.hasResult and not h.panel.busy, h.panel.error
        payload = h.panel._payload
        assert payload["schema_version"] == 3
        request = payload["request"]
        assert (
            request["reference_nm"],
            request["thickness_tolerance_nm"],
            request["optical_tolerance_percentage_points"],
        ) == (109.0, 0.0, 1.0)
        assert "defect_layer_ids" not in request and "seconds_per_route" not in request
        best = payload["result"]["best"]
        assert best is not None and best["optical_guard"]["passed"]
        assert best["optical_guard"]["tolerance_fraction"] == 0.01
        assert all(r["thickness"]["value"] >= 109.0 for r in best["design"]["front_layers"])
        assert h.session.snapshot().design == original
        assert h.panel.selectedRoute == "automatic"
        assert not warnings, warnings


def test_no_original_goal_has_explicit_default_comparison_scope_and_zero_tolerances(harness):
    h = harness
    h.owner._objectives = []
    assert h.panel.prepare(), h.panel.error
    assert not h.panel._has_original_targets
    base = h.panel._build_base_request()
    defaults = simulation_settings()
    assert [
        (
            o.metric,
            o.wavelength_min_nm,
            o.wavelength_max_nm,
            o.wavelength_samples,
            o.angles_deg,
            o.polarization,
        )
        for o in base.objectives
    ] == [
        (
            metric, defaults["wavelength_min_nm"], defaults["wavelength_max_nm"],
            defaults["samples"], (defaults["angle_deg"],), defaults["polarization"],
        )
        for metric in ("R", "T")
    ]
    for key, text in (
        ("reference_nm", "10"),
        ("thickness_tolerance_nm", "0"),
        ("optical_tolerance_percentage_points", "0"),
    ):
        assert h.panel.setOption(key, text)
        assert h.panel.finishOptionEdit(key), h.panel.error
    assert h.panel._validated_options() == {
        "reference_nm": 10.0,
        "thickness_tolerance_nm": 0.0,
        "optical_tolerance_percentage_points": 0.0,
    }


def _configure_automatic(h, *, target="110", thickness_tolerance="1", optical_tolerance="1"):
    for name, literal in (
        ("reference_nm", target),
        ("thickness_tolerance_nm", thickness_tolerance),
        ("optical_tolerance_percentage_points", optical_tolerance),
    ):
        assert h.panel.setOption(name, literal)
    assert h.panel.startAutomatic(), h.panel.error


def test_legacy_v2_cancel_retains_audited_result_for_review_without_apply(legacy_v2_harness):
    h = legacy_v2_harness
    original = deepcopy(h.session.snapshot().design)
    h.client.cancel_stage = "repair_audited_best"
    _configure_automatic(h)
    h.pool.run()
    assert h.panel.hasResult and not h.panel.busy, h.panel.error
    payload = h.panel._payload
    assert payload["schema_version"] == 2 and payload["state"] == "cancelled"
    assert payload["result"]["best"] is not None
    assert payload["result"]["best"]["optical_guard"]["passed"]
    assert h.panel.comparisonRows and not h.panel.canApply
    assert h.session.snapshot().design == original
    report = h.tmp_path / 'cancelled-automatic-report.json'
    assert h.panel.saveReport(str(report))
    assert json.loads(report.read_text(encoding='utf8')) == payload
    h.panel.load_verified_history(payload,h.panel.jobId)
    assert h.panel.hasResult and not h.panel.canApply


def test_legacy_v2_real_curve_guard_pass_with_failed_original_goal_is_review_only_and_cannot_apply(
    legacy_v2_harness,
):
    h = legacy_v2_harness
    assert h.owner.setObjectiveField("pointwise.upper_bound_percent", 0.)
    assert h.owner.setSolverOption("max_evaluations", 192)
    assert h.panel.prepare(), h.panel.error
    original = deepcopy(h.session.snapshot().design)
    # Isolate original-goal acceptance with a curve allowance that covers the
    # single-material film's real R oscillation; no candidate can meet R <= 0.
    _configure_automatic(h, target="110", thickness_tolerance="1", optical_tolerance="5")
    h.pool.run()
    assert h.panel.hasResult and not h.panel.busy, h.panel.error
    payload = h.panel._payload
    context = h.panel._context
    assert payload is not None and context is not None
    assert payload["schema_version"] == 2 and payload["request"]["original_targets_present"]
    body = payload["result"]
    reviewed = body["reviewed"]
    assert body["source"]["training_score"]["nominal_pass"] is False
    assert body["best"] is None and reviewed is not None
    assert reviewed["optical_guard"]["passed"] is True
    assert reviewed["training_score"]["nominal_pass"] is False
    assert reviewed["audit_score"]["nominal_pass"] is False
    assert not h.panel.canApply and not h.panel.applySelectedRoute()
    assert not h.owner._manufacturing_can_apply(payload, context, reviewed)
    assert not h.owner._apply_manufacturing_candidate(payload, context, reviewed)
    assert not h.source.applied and h.session.snapshot().design == original
    # Fault injection promotes the real TMM review snapshot without changing
    # either original score. Both GUI defenses must reject that false role.
    body["best"] = reviewed
    body["routes"][0]["champion"] = reviewed
    assert h.panel.setSelectedRoute("automatic")
    assert not h.panel.canApply and not h.panel.applySelectedRoute()
    assert not h.owner._manufacturing_can_apply(payload, context, reviewed)
    assert not h.owner._apply_manufacturing_candidate(payload, context, reviewed)
    assert not h.source.applied and h.session.snapshot().design == original


def test_automatic_repeat_uses_new_frozen_source_and_old_terminal_cannot_replace_it(harness):
    h = harness
    first_source = deepcopy(h.session.snapshot().design)
    _configure_automatic(h)
    first_worker = h.pool.tasks[-1]
    first_terminal = []
    first_worker.signals.finished.connect(lambda *args: first_terminal.append(args))
    h.pool.run()
    assert h.panel.hasResult and first_terminal
    first = deepcopy(h.panel._payload)
    assert first["request"]["source_design"]["front_layers"][0]["thickness"]["value"] == 100.0
    assert h.session.snapshot().design == first_source
    h.session.design["front_layers"][0]["thickness"]["value"] = 105.0
    h.session.design_revision += 1
    h.owner.refreshDesign()
    assert h.panel.prepare(), h.panel.error
    current = deepcopy(h.session.snapshot().design)
    _configure_automatic(h)
    active = h.pool.tasks[-1]
    assert active.operation_id != first_worker.operation_id
    h.panel._run_coordinator._on_worker_finished(*first_terminal[0])
    assert h.panel.busy
    assert h.pool.tasks[-1] is active
    h.pool.run()
    second = deepcopy(h.panel._payload)
    assert (
        second["schema_version"] == 3
        and second["request"]["source_design"]["front_layers"][0]["thickness"]["value"] == 105.0
    )
    assert second["request"]["source_fingerprint"] != first["request"]["source_fingerprint"]
    assert h.session.snapshot().design == current
    h.panel._run_coordinator._on_worker_finished(*first_terminal[0])
    assert h.panel._payload == second


def test_automatic_cancel_before_admission_can_restart_without_reusing_old_frames(harness):
    h = harness
    original = deepcopy(h.session.snapshot().design)
    _configure_automatic(h)
    h.panel.cancel()
    h.pool.run()
    assert not h.panel.busy and not h.panel.canApply and not h.client.submissions
    assert h.panel.prepare(), h.panel.error
    _configure_automatic(h)
    h.pool.run()
    assert h.panel.hasResult and h.panel._payload["schema_version"] == 3, h.panel.error
    assert len(h.client.submissions) == 1
    assert h.panel._payload["result"]["best"] is not None
    assert h.session.snapshot().design == original


@pytest.mark.parametrize(
    "settings,can_calculate",
    [
        (
            dict(wavelength_min_nm=450., wavelength_max_nm=700., step_nm=5.,
                 angle_deg=27., polarization="p", extrapolation_policy="error"),
            True,
        ),
        (
            dict(wavelength_min_nm=400., wavelength_max_nm=700., samples=31,
                 angle_deg=17., polarization="s", extrapolation_policy="endpoint_hold"),
            True,
        ),
        (
            dict(wavelength_min_nm=400., wavelength_max_nm=700., samples=31,
                 angle_deg=17., polarization="s", extrapolation_policy="error"),
            False,
        ),
    ],
    ids=["current-scope", "existing-endpoint-hold", "existing-error-policy"],
)
def test_legacy_v2_no_original_goal_uses_current_simulation_scope_and_existing_material_policy(
    legacy_v2_harness, settings, can_calculate,
):
    h = legacy_v2_harness
    h.source.settings = deepcopy(settings)
    h.owner._objectives = []
    assert h.panel.prepare(), h.panel.error
    original = deepcopy(h.session.snapshot().design)
    if can_calculate:
        ordinary = h.service.simulate(original, settings)
        assert len(ordinary["R"]) == simulation_settings(settings)["samples"]
    else:
        with pytest.raises(ValueError):
            h.service.simulate(original, settings)
    _configure_automatic(h, target="1", thickness_tolerance="0", optical_tolerance="0")
    h.pool.run()
    if not can_calculate:
        assert not h.panel.hasResult and not h.panel.canApply
        assert h.panel.error
        assert h.session.snapshot().design == original
        return
    assert h.panel.hasResult, h.panel.error
    payload = h.panel._payload
    assert (
        payload["schema_version"] == 2 and payload["request"]["original_targets_present"] is False
    )
    assert payload["request"]["comparison_settings"] == simulation_settings(settings)
    request, _ = automatic_jobs.request_from_payload(payload["request"])
    configured = simulation_settings(settings)
    assert [
        (o.metric, o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples,
         o.angles_deg, o.polarization)
        for o in request.objectives
    ] == [
        (metric, configured["wavelength_min_nm"], configured["wavelength_max_nm"],
         configured["samples"], (configured["angle_deg"],), configured["polarization"])
        for metric in ("R", "T")
    ]
    best = payload["result"]["best"]
    assert best is not None and best["optical_guard"]["passed"]
    assert best["optical_guard"]["maximum_absolute_change"] == 0.0
    assert {row["metric"] for row in best["optical_guard"]["records"]} == {"R", "T"}
    assert payload["work"]["physical_calls"] > 0
    baseline = payload["result"]["source"]
    assert baseline["audit_score"]["audit_coverage"]
    coverage = {
        (row["objective_index"], row["angle_deg"]): row
        for row in baseline["training_score"]["coverage"]
    }
    for row in best["optical_guard"]["records"]:
        original_curve = coverage[(row["objective_index"], row["angle_deg"])]
        assert row["wavelength_nm"] == original_curve["wavelength_nm"]
        assert row["reference_values"] == original_curve["actual_values"]
        assert row["candidate_values"] == row["reference_values"]
        assert row["reference_values"] == pytest.approx(
            ordinary[row["metric"]], rel=2e-11, abs=2e-13
        )
        if row["metric"] == "T":
            assert max(row["reference_values"]) > 0.0
    assert all("不适用" in row["details"] for row in h.panel.comparisonRows)
    assert h.session.snapshot().design == original


def test_legacy_v2_session_simulation_scope_precedes_live_defaults_and_change_fences_old_apply(
    legacy_v2_harness, monkeypatch,
):
    h = legacy_v2_harness
    h.source.settings = dict(wavelength_min_nm=400., wavelength_max_nm=700., samples=31,
                             angle_deg=0., polarization="unpolarized",
                             extrapolation_policy="endpoint_hold")
    current = dict(parameters=dict(wavelength_min_nm=450., wavelength_max_nm=700., step_nm=25.,
                                   angle_deg=21., polarization="p", extrapolation_policy="error"))
    old_configuration = h.session.workflow_configuration
    monkeypatch.setattr(
        h.session, "workflow_configuration",
        lambda scope: deepcopy(current) if scope == "simulation" else old_configuration(scope),
    )
    h.owner._objectives = []
    assert h.panel.prepare(), h.panel.error
    original = deepcopy(h.session.snapshot().design)
    revision = h.session.design_revision
    _configure_automatic(h, target="1", thickness_tolerance="0", optical_tolerance="0")
    h.pool.run()
    assert h.panel.hasResult and h.panel.canApply, h.panel.error
    first = h.panel._payload
    assert first["request"]["comparison_settings"] == simulation_settings(current["parameters"])
    first_job = h.panel.jobId
    current["parameters"]["angle_deg"] = 33.
    assert h.session.design_revision == revision
    # Even without the normal session notification, explicit Apply performs the
    # full settings fence. The cheap QML readiness property avoids re-expanding
    # large saved light-source arrays until the session emits its change event.
    assert not h.panel.applySelectedRoute()
    h.session._subscriber(SimpleNamespace(categories=frozenset({"workflow_settings", "ui"})))
    assert not h.panel.canApply
    assert not h.source.applied and h.session.snapshot().design == original
    assert h.panel.prepare(), h.panel.error
    _configure_automatic(h, target="1", thickness_tolerance="0", optical_tolerance="0")
    h.pool.run()
    assert h.panel.hasResult and h.panel.canApply, h.panel.error
    assert h.panel.jobId != first_job
    assert h.panel._payload["request"]["comparison_settings"] == simulation_settings(
        current["parameters"]
    )
    assert h.session.snapshot().design == original


def test_unknown_submission_is_not_resubmitted(harness):
    h = harness
    configure(h)
    h.client.unknown = True
    assert h.panel.start()
    h.pool.run()
    job_id = h.panel.jobId
    assert "submission_unknown" in h.panel.error
    assert not h.panel.start()
    assert len(h.client.submissions) == 1 and h.panel.jobId == job_id


@pytest.mark.parametrize('automatic', [False, True])
def test_raw_qw_source_fenced_and_physical_nm_report(harness, automatic):
    h = harness
    h.session.design["front_layers"][0]["thickness"] = {"value": .8, "unit": "qw"}
    h.session.design_revision += 1
    h.owner.refreshDesign()
    assert h.panel.prepare(), h.panel.error
    assert "qw" in h.panel.layerRows[0]["thickness"]
    original = deepcopy(h.session.snapshot().design)
    if automatic:
        _configure_automatic(h)
        h.pool.run()
        assert h.panel.hasResult,h.panel.error
    else:
        run(h)
    physical = h.panel._payload["request"]["source_design"]["front_layers"][0]["thickness"]
    assert physical["unit"] == "nm" and physical["value"] != .8
    assert h.session.snapshot().design == original
    assert h.panel.setSelectedRoute('automatic' if automatic else "raise_selected") and h.panel.canApply
    h.session.design["front_layers"][0]["thickness"]["value"] = .9
    # An external same-revision mutation is rejected by explicit Apply's full
    # raw-source fence, not by repeatedly expanding requests during QML reads.
    assert not h.panel.applySelectedRoute()


def test_no_audit_always_unknown_not_passed_and_history_window(harness, app):
    h = harness
    payload = run(h)
    from coat_sim.ui.film_manufacturing_quick_adapter import snapshot_text
    original = deepcopy(payload["result"]["source"])
    original["audit_score"] = None
    text = snapshot_text(original, payload["request"]["synthesis"]["request"]["objectives"])
    assert "未知/未通过" in text and "独立审计" in text
    h.panel.load_verified_history(payload, "read-only-history")
    qml = 'import QtQuick\nimport "."\nFilmManufacturingResultWindow {}'
    with window_runtime(app, h.panel, source=qml) as (window, warnings):
        assert not h.panel.canApply
        assert window.property("titleBase") == "工艺薄层修复 · 只读历史报告"
        assert not warnings, warnings


def test_close_request_rejected_by_owner_does_not_permanently_detach(harness):
    h = harness
    configure(h)
    assert h.panel.start()
    from tests.test_film_optimization_run_contract import _completed_result
    ordinary = _completed_result(h.owner._build_request(require_variables=False, fixed_topology=False))
    h.owner._result = ordinary
    h.owner._candidate_saved = h.owner._candidate_applied = False
    prompts = []
    h.owner.closeConfirmationRequested.connect(lambda: prompts.append(True))
    h.owner.requestClose()
    assert prompts == [True]
    h.owner.resolveClose(False)
    assert h.owner._result is ordinary
    assert not h.panel._closed and h.panel.busy
    h.pool.run()
    assert not h.panel.busy and not h.panel.canApply
    assert h.panel.prepare()  # owner rejected its ordinary-candidate close
    assert h.panel.start(), h.panel.error
    h.pool.run()
    assert h.panel.hasResult
