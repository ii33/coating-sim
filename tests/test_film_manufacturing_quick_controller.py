"""Manufacturing UI contracts with real optics and real offscreen Qt inputs.

Only IPC is held in memory. Production freezing, worker execution and evidence validation are
used; these tests are not native Windows or physical instrument acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from threading import Event
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QMetaObject, QPointF, QThreadPool, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.core import film_manufacturing_jobs as jobs
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
    h.pool.run()
    assert not h.panel.busy and not h.panel.canApply
    assert not h.client.submissions


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


@pytest.mark.parametrize("mode,scale", [("light", 1.), ("dark", 1.), ("light", 1.25)])
def test_real_qml_inputs_blank_preservation_run_cancel_and_responsive_geometry(harness, app, mode, scale):
    h = harness
    qml = '''import QtQuick
import QtQuick.Controls
import "."
AppWindow {
    id: window
    required property var controller
    width: 1100; height: 760
    FilmManufacturingRepairDialog { id: repair; controller: window.controller }
    Component.onCompleted: repair.open()
}'''
    with window_runtime(app, h.panel, source=qml) as (window, warnings):
        from coat_sim.ui.theme_service import ThemeService
        ThemeService.instance().configure(mode=mode, font_scale=scale, density_scale=scale)
        QTest.qWait(20)
        root = window.findChild(object, "filmManufacturingRepairDialog")
        assert root is not None
        field = _visual(window.contentItem(), "filmManufacturing_reference_nm")
        assert field is not None and field.property("text") == ""
        assert window.grabWindow().save("/tmp/film-manufacturing-dialog-offscreen.png")
        _click(window, field)
        _type(window, "110")
        assert h.panel.options["reference_nm"] == "110"
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        _type(window, "nan")
        QTest.keyClick(window, Qt.Key.Key_Tab)
        assert h.panel.options["reference_nm"] == "nan"
        assert field.property("text") == "nan"
        _click(window, _visual(window.contentItem(), "filmManufacturingRun"))
        assert not h.panel.busy and "有限正数" in h.panel.error
        window.resize(530, 580)
        QTest.qWait(30)
        assert root.property("width") <= 530
        for name in ("Run", "Cancel", "Close", "Save"):
            button = _visual(window.contentItem(), "filmManufacturing" + name)
            right = button.mapToScene(QPointF(button.width(), 0)).x()
            assert 0 <= right <= window.width(), (name, right)
        window.resize(1100, 760)
        QTest.qWait(30)
        assert root.property("width") > 900
        _click(window, _visual(window.contentItem(), "filmManufacturingLayer_" + h.panel.layerRows[0]["layer_id"]))
        assert h.panel.layerRows[0]["selected"] is True
        for name, value in (("reference_nm", "110"), ("refinement_evaluations_per_route", "2"),
                            ("max_iterations_per_route", "2"), ("seconds_per_route", "10")):
            editor = _visual(window.contentItem(), "filmManufacturing_" + name)
            _click(window, editor)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            _type(window, value)
            QTest.keyClick(window, Qt.Key.Key_Tab)
            assert h.panel.options[name] == value
        _click(window, _visual(window.contentItem(), "filmManufacturingRun"))
        assert h.panel.busy
        assert not _visual(window.contentItem(), "filmManufacturingRun").isEnabled()
        _click(window, _visual(window.contentItem(), "filmManufacturingCancel"))
        assert h.panel.busy  # only a request, not a pretend terminal state
        h.pool.run()
        assert not h.panel.busy and not h.client.submissions
        _click(window, _visual(window.contentItem(), "filmManufacturingClose"))
        assert not root.property("visible")
        QMetaObject.invokeMethod(root, "open")
        QTest.qWait(20)
        assert root.property("visible") and h.panel.options["reference_nm"] == "110"
        assert not warnings, warnings


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


def test_raw_qw_source_fenced_and_physical_nm_report(harness):
    h = harness
    h.session.design["front_layers"][0]["thickness"] = {"value": .8, "unit": "qw"}
    h.session.design_revision += 1
    h.owner.refreshDesign()
    assert h.panel.prepare(), h.panel.error
    assert "qw" in h.panel.layerRows[0]["thickness"]
    original = deepcopy(h.session.snapshot().design)
    run(h)
    physical = h.panel._payload["request"]["source_design"]["front_layers"][0]["thickness"]
    assert physical["unit"] == "nm" and physical["value"] != .8
    assert h.session.snapshot().design == original
    assert h.panel.setSelectedRoute("raise_selected") and h.panel.canApply
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
