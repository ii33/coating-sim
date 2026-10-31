"""GUI/controller contracts for current bounded ideal sensitivity."""
from __future__ import annotations

from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core import film_sensitivity_workflow_jobs as workflow_jobs
from coat_sim.scheduler.jobs import JobSpec
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import (
    click, click_operation_bar_action, optimizer, window_runtime,
)
from tests.test_film_sensitivity_quick_controller import _HeldPool

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


class _PhysicsClient:
    """The UI uses its normal worker path; this transports to real local physics."""

    def __init__(self):
        self.executors = ["film_ideal_sensitivity", "film_thickness_error_table"]
        self.submissions = []
        self.rows = {}
        self.unknown = False

    def capabilities(self):
        return {"protocol_version": 1, "executors": list(self.executors)}

    def submit(self, spec, *, job_id, metadata):
        frozen = JobSpec.parse_frozen(spec)
        self.submissions.append((deepcopy(spec), job_id, deepcopy(metadata)))
        if self.unknown:
            raise OSError("held lost submission acknowledgement")
        payload = workflow_jobs.execute_job(
            frozen.parameters, Event(), lambda _item: None,
            "memory-checkpoint", lambda *_args: None)
        state = {"completed": "succeeded", "partial": "succeeded",
                 "failed": "failed", "cancelled": "cancelled"}[payload["state"]]
        self.rows[job_id] = {"job_id": job_id, "kind": frozen.executor, "state": state,
                             "input_digest": frozen.fingerprint, "result": payload}
        return deepcopy(self.rows[job_id])

    def status(self, job_id):
        if job_id not in self.rows:
            raise ValueError("job_not_found")
        return deepcopy(self.rows[job_id])

    def wait_for_terminal(self, job_id, *, on_update, should_stop, wait_s):
        return self.status(job_id)

    def cancel(self, job_id):
        return {"job_id": job_id, "cancel_requested": True}


@pytest.fixture
def harness(app, tmp_path):
    service, library, design = fixture(tmp_path)
    design["front_layers"].append({**deepcopy(design["front_layers"][0]),
                                   "id": "existing-stable-layer-2"})
    with optimizer(service, design) as (owner, source, session):
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = owner.sensitivityController
        pool, client = _HeldPool(), _PhysicsClient()
        panel.pool = pool
        panel.client_factory = lambda _root: client
        assert panel.prepare(), panel.error
        yield SimpleNamespace(owner=owner, source=source, session=session, service=service,
                              library=library, design=design, panel=panel, pool=pool, client=client)


def _bounded_variable(harness):
    layer_id = harness.session.design["front_layers"][0]["id"]
    assert harness.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert harness.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    assert harness.panel.prepare(), harness.panel.error
    return layer_id


def _find_visual_item(root, object_name):
    if root is not None and root.objectName() == object_name:
        return root
    if root is not None:
        for child in root.childItems():
            found = _find_visual_item(child, object_name)
            if found is not None:
                return found
    return None



def _dialog_button(root, answer):
    """Locate an actual visible standard button without accepting the dialog API."""
    labels = {"yes": {"yes", "是"}, "no": {"no", "否"}}[answer]
    text = root.property("text")
    if (hasattr(root, "clicked") and root.isVisible() and isinstance(text, str)
            and text.replace("&", "").strip().lower() in labels):
        return root
    for child in root.childItems():
        found = _dialog_button(child, answer)
        if found is not None:
            return found
    return None

def _run_ideal(harness, *, mode="absolute", budget="0.1", max_iterations="4",
               max_evaluations="60"):
    panel = harness.panel
    assert panel.setIdealSensitivityMode(mode)
    for row in panel.idealDegradationObjectives:
        assert panel.setIdealDegradationBudget(row["objective_id"], budget)
    assert panel.setIdealOption("max_iterations", max_iterations)
    assert panel.setIdealOption("max_evaluations", max_evaluations)
    assert panel.startIdealSensitivityOptimization(), panel.idealError
    harness.pool.run()
    assert panel.idealHasResult, panel.idealError
    return panel._ideal_payload


def test_bounded_default_requires_each_budget_and_keeps_id_associations(harness):
    panel = harness.panel
    assert panel.idealSensitivityPolicy == "bounded_baseline_degradation"
    assert panel.idealSensitivityMode == ""
    assert len(panel.idealDegradationObjectives) == 1
    first = panel.idealDegradationObjectives[0]
    assert first["objective_id"] == harness.owner._objectives[0]["id"]
    assert "550 nm" in first["display"] and "R" in first["display"]
    with window_runtime(QGuiApplication.instance(), harness.owner) as (window, warnings):
        click_operation_bar_action(QGuiApplication.instance(), window, "filmSensitivityOpenButton")
        policy_box = window.findChild(QObject, "filmIdealSensitivityPolicyBox")
        budget_field = _find_visual_item(
            window.contentItem(), "filmIdealDegradationBudget_" + first["objective_id"])
        unit_note = _find_visual_item(window.contentItem(), "filmIdealDegradationUnitsNote")
        assert policy_box is not None and policy_box.property("currentIndex") == 1
        assert budget_field is not None and budget_field.property("text") == ""
        assert unit_note is not None
        assert "绝非膜厚相对百分比" in unit_note.property("text")
        assert "0.1 pp = 0.001 R/T fraction" in unit_note.property("text")
        assert not warnings, warnings

    assert not panel.startIdealSensitivityOptimization()
    assert "请选择绝对或相对" in panel.idealError
    assert panel.setIdealSensitivityMode("absolute")
    assert not panel.startIdealSensitivityOptimization()
    assert "百分点 pp" in panel.idealError and "空白不会按零" in panel.idealError
    assert not harness.pool.tasks and not harness.client.submissions

    # Explicit zero is a real frozen allowance, not an omitted value.
    assert panel.setIdealDegradationBudget(first["objective_id"], "0")
    base = harness.owner._build_request(require_variables=False, fixed_topology=False)
    request = panel._build_ideal_request(base)
    assert request.schema_version == 3
    assert request.policy == "bounded_baseline_degradation"
    assert request.objective_ids == (first["objective_id"],)
    assert [(b.objective_id, b.delta, b.unit) for b in request.budgets] == [
        (first["objective_id"], 0., "percentage_point")]

    # Add a second current target, enter an independently stored 0.1 pp, then
    # reorder the owner rows. Values follow stable IDs, never the display index.
    harness.owner.addObjective()
    assert harness.owner.setObjectiveField("wavelength_min_nm", 550.)
    assert harness.owner.setObjectiveField("wavelength_max_nm", 550.)
    assert panel.prepare(), panel.error
    rows = panel.idealDegradationObjectives
    second_id = harness.owner._objectives[1]["id"]
    assert rows[0]["objective_id"] == first["objective_id"]
    assert rows[1]["objective_id"] == second_id
    assert panel.setIdealDegradationBudget(second_id, "0.1")
    harness.owner._objectives.reverse()
    assert panel.prepare(), panel.error
    reordered = panel.idealDegradationObjectives
    assert [row["objective_id"] for row in reordered] == [second_id, first["objective_id"]]
    assert [row["budget_text"] for row in reordered] == ["0.1", "0"]
    base = harness.owner._build_request(require_variables=False, fixed_topology=False)
    request = panel._build_ideal_request(base)
    assert request.objective_ids == (second_id, first["objective_id"])
    assert [(b.objective_id, b.delta) for b in request.budgets] == [(second_id, .1), (first["objective_id"], 0.)]
    assert request.budgets[0].delta / 100. == pytest.approx(.001)  # 0.1 pp = 0.001 R/T fraction

    assert panel.setIdealDegradationBudget(second_id, "NaN")
    assert not panel.startIdealSensitivityOptimization()
    assert "有限非负数" in panel.idealError
    assert not harness.pool.tasks and not harness.client.submissions


def test_real_qml_scale_selection_then_blank_budget_cannot_submit(harness, app):
    panel = harness.panel
    with window_runtime(app, harness.owner) as (window, warnings):
        assert harness.owner.setSearchMode("ideal_sensitivity")
        click(app, window, _find_visual_item(window.contentItem(), "filmOptimizationAlgorithmTab"))
        mode = window.findChild(QQuickItem, "filmIdealSensitivityModeBox")
        assert mode is not None and mode.isVisible()
        click(app, window, mode)
        QTest.keyClick(window, Qt.Key.Key_Home)
        QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Return)
        app.processEvents()
        assert panel.idealSensitivityMode == "absolute"
        assert all(row["budget_text"] == "" for row in panel.idealDegradationObjectives)
        optimize = window.findChild(QQuickItem, "filmOptimizationStartButton")
        assert optimize is not None and optimize.isEnabled()
        click_operation_bar_action(app, window, "filmOptimizationStartButton")
        QTest.qWait(20)
        assert "百分点 pp" in panel.idealError and "空白不会按零" in panel.idealError
        assert not panel.busy and not harness.pool.tasks and not harness.client.submissions
        assert not warnings, warnings


def test_policy_and_budget_drafts_do_not_touch_parameter_table(harness):
    panel = harness.panel
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    assert panel.setTableDraft(layer_id, "bias", "1.25")
    before = deepcopy(panel.thicknessErrorRows)
    objective_id = panel.idealDegradationObjectives[0]["objective_id"]
    assert panel.setIdealDegradationBudget(objective_id, "0.2")
    assert panel.setIdealSensitivityPolicy("strict_nominal")
    assert panel.thicknessErrorRows == before
    assert panel.idealSensitivityPolicy == "strict_nominal"
    assert panel.setIdealSensitivityPolicy("bounded_baseline_degradation")
    assert panel.idealDegradationObjectives[0]["budget_text"] == "0.2"
    assert panel.thicknessErrorRows == before


def test_nominal_false_guard_pass_candidate_requires_explicit_confirm_and_is_one_shot(harness, app):
    panel = harness.panel
    # A real small physical case: nominal R >= 99% is intentionally not met,
    # while the 0.1 pp guard permits a sensitivity-improving spectral decline.
    original = deepcopy(harness.session.design)
    layer = deepcopy(original["front_layers"][0])
    layer["thickness"]["value"] = 80.
    harness.session.design["front_layers"] = [layer]
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert harness.owner.setObjectiveField("pointwise.upper_bound_percent", 100.)
    assert harness.owner.setObjectiveField("pointwise.lower_bound_percent", 99.)
    assert harness.panel.prepare(), harness.panel.error
    _bounded_variable(harness)
    payload = _run_ideal(harness, budget="0.1", max_iterations="12", max_evaluations="60")

    from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload
    result = workflow_result_from_payload(payload)
    assert result.schema_version == 3
    assert result.policy == "bounded_baseline_degradation"
    assert result.state in {"completed", "partial"}, result.termination_reason
    assert result.baseline_analysis is not None and not result.baseline_analysis.nominal_targets_met
    assert result.best_analysis is not None and not result.best_analysis.nominal_targets_met
    assert result.best_guard is not None and result.guard_pass
    assert result.usable_candidate
    row = result.best_guard.rows[0]
    assert row.delta_percentage_point == pytest.approx(.1)
    assert row.delta_fraction == pytest.approx(.001)
    assert panel.idealCanApply
    summary = panel.idealApplyConfirmationSummary
    assert "初态原名义目标：未通过" in summary
    assert "最终原名义目标：未通过" in summary
    assert "最大定向退化" in summary and "pp" in summary and "额度内" in summary
    assert "guard 额度通过不代表原名义目标通过" in summary

    # The integrated window confirmation shows both independent statuses and
    # applies only after the user accepts. Give this focused test source the
    # same one-entry apply/undo behavior as the production source controller.
    original_design = deepcopy(harness.session.design)
    undo_state = {}

    def apply_to_session(candidate, revision, edit_epoch):
        if (revision != harness.session.design_revision
                or edit_epoch != harness.source.pendingEditEpoch):
            return False
        undo_state["design"] = deepcopy(harness.session.design)
        harness.session.design = deepcopy(candidate)
        harness.session.design_revision += 1
        undo_state["applied_revision"] = harness.session.design_revision
        harness.source.applied.append((deepcopy(candidate), revision, edit_epoch))
        harness.session._subscriber(SimpleNamespace(categories=frozenset({"design"})))
        return True

    def undo_session_once():
        if (not undo_state or harness.session.design_revision != undo_state["applied_revision"]):
            return False
        harness.session.design = deepcopy(undo_state["design"])
        harness.session.design_revision += 1
        harness.session._subscriber(SimpleNamespace(categories=frozenset({"design"})))
        undo_state.clear()
        return True

    harness.source.applyOptimizedDesign = apply_to_session
    harness.source.undoOptimizedDesign = undo_session_once
    with window_runtime(app, harness.owner) as (window, warnings):
        assert harness.owner.setSearchMode("ideal_sensitivity")
        apply = window.findChild(QQuickItem, "filmSensitivityCommonApply")
        assert apply is not None and apply.isEnabled()
        click_operation_bar_action(app, window, "filmSensitivityCommonApply")
        confirm = window.findChild(QObject, "filmOptimizationApplyConfirmation")
        label = confirm
        assert confirm is not None and confirm.property("visible")
        assert "初态原名义目标：未通过" in label.property("message")
        assert "最终原名义目标：未通过" in label.property("message")
        assert not harness.source.applied
        no_button = _dialog_button(window.contentItem(), "no")
        assert no_button is not None
        click(app, window, no_button)
        assert not confirm.property("visible") and not harness.source.applied
        assert panel.idealCanApply
        click_operation_bar_action(app, window, "filmSensitivityCommonApply")
        assert confirm.property("visible")
        yes_button = _dialog_button(window.contentItem(), "yes")
        assert yes_button is not None
        click(app, window, yes_button)
        assert not confirm.property("visible")
        assert harness.source.applied
        assert not panel.idealCanApply
        assert harness.source.undoOptimizedDesign()
        assert harness.session.design == original_design
        assert not harness.source.undoOptimizedDesign()
        assert len(harness.source.applied) == 1
        assert not warnings, warnings


def test_source_edit_after_bounded_result_still_rejects_apply(harness):
    _bounded_variable(harness)
    payload = _run_ideal(harness, budget="100")
    assert payload is not None
    assert harness.panel.idealCanApply
    harness.session.design_revision += 1
    assert not harness.panel.idealCanApply
    assert not harness.panel.applySelectedBest()
    assert not harness.source.applied


def test_equal_F_no_improvement_is_explicit_even_at_evaluation_limit(harness):
    _bounded_variable(harness)
    payload = _run_ideal(harness, budget="100", max_iterations="4", max_evaluations="2")

    from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload
    result = workflow_result_from_payload(payload)
    assert result.schema_version == 3
    assert result.termination_reason == "evaluation_limit"
    assert result.improvement_status == "no_improvement"
    assert result.best_analysis.objective_value == result.baseline_analysis.objective_value
    assert "敏感度改善状态：no_improvement" in harness.panel.idealResultSummary
