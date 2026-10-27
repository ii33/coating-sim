"""GUI/controller contracts for the two ordinary typed sensitivity workflows."""
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
from PySide6.QtQml import QQmlExpression, qmlContext
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


class _WorkflowPhysicsClient:
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
        execute = workflow_jobs.execute_job
        payload = execute(frozen.parameters, Event(), lambda _item: None,
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
        pool, client = _HeldPool(), _WorkflowPhysicsClient()
        panel.pool = pool
        panel.client_factory = lambda _root: client
        assert panel.prepare(), panel.error
        yield SimpleNamespace(owner=owner, source=source, session=session, service=service,
                              library=library, design=design, panel=panel, pool=pool, client=client)


def _set_bias(panel, layer_id, text, unit="absolute_nm"):
    assert panel.setTableDraft(layer_id, "bias", text)
    assert panel.setTableUnit(layer_id, "bias", unit)


def _prepare_three_stddev_rows(harness):
    design = harness.session.design
    while len(design["front_layers"]) < 3:
        clone = deepcopy(design["front_layers"][0])
        clone["id"] = f"rho-layer-{len(design['front_layers']) + 1}"
        design["front_layers"].append(clone)
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    panel = harness.panel
    assert panel.prepare(), panel.error
    layer_ids = [row["layer_id"] for row in panel.thicknessErrorRows]
    for layer_id in layer_ids:
        assert panel.setTableUncertaintyKind(layer_id, "stddev")
        assert panel.setTableUnit(layer_id, "uncertainty", "absolute_nm")
        assert panel.setTableDraft(layer_id, "uncertainty_magnitude", "1")
    return layer_ids


def _find_visual_item(root, object_name):
    if root.objectName() == object_name:
        return root
    for child in root.childItems():
        found = _find_visual_item(child, object_name)
        if found is not None:
            return found
    return None


def _scroll_editor_into_view(app, item):
    """Reach the real editor before testing key input in a clipped table."""
    context = qmlContext(item)
    assert context is not None
    expression = QQmlExpression(context, item, """(function () {
        let ancestor = parent;
        while (ancestor !== null) {
            if ("contentY" in ancestor) {
                const point = mapToItem(ancestor.contentItem, 0, 0);
                const maximum = Math.max(0, ancestor.contentHeight - ancestor.height);
                ancestor.contentY = Math.min(maximum, Math.max(0, point.y - 15));
            }
            ancestor = ancestor.parent;
        }
        return true;
    })()""")
    expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    QTest.qWait(20)
    app.processEvents()


def _run_table(harness):
    assert harness.panel.startThicknessErrorTableAnalysis(), harness.panel.tableError
    harness.pool.run()
    assert not harness.panel.busy, harness.panel.tableError
    assert harness.panel.tableHasResult, harness.panel.tableError
    assert JobSpec.parse_frozen(harness.client.submissions[-1][0]).executor == "film_thickness_error_table"
    return harness.panel._table_payload


def _bounded_variable(harness):
    layer_id = harness.design["front_layers"][0]["id"]
    assert harness.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert harness.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    # Keep the source capture current after owner-side edits.
    assert harness.panel.prepare()
    return layer_id


def test_initial_entries_have_no_invented_table_or_ideal_defaults(harness, app):
    panel = harness.panel
    assert all(row["bias_text"] == row["uncertainty_text"] == "" for row in panel.thicknessErrorRows)
    assert all(row["bias_unit"] == row["uncertainty_unit"] == row["uncertainty_kind"] == ""
               for row in panel.thicknessErrorRows)
    assert panel.idealSensitivityMode == ""
    assert panel.idealSensitivityPolicy == "bounded_baseline_degradation"
    assert panel.idealOptions == {"max_iterations": "", "max_evaluations": ""}
    assert len(panel.idealSensitivityLayers) == len(harness.session.design["front_layers"])
    assert all(row["selected"] for row in panel.idealSensitivityLayers)
    assert panel.activeWorkflow == "ideal"
    with window_runtime(app, harness.owner) as (window, warnings):
        click_operation_bar_action(app, window, "filmSensitivityOpenButton")
        assert window.findChild(QObject, "filmSensitivityDialog").property("visible")
        assert window.findChild(QObject, "filmSensitivityWorkflowTabs").property("currentIndex") == 1
        root = window.contentItem()
        assert _find_visual_item(root, "filmSensitivityTableTab") is not None
        assert _find_visual_item(root, "filmSensitivityIdealTab") is not None
        assert _find_visual_item(root, "filmSensitivityLegacyTab") is None
        assert window.findChild(QObject, "filmThicknessErrorTableAnalyzeButton") is not None
        ideal_optimize = window.findChild(QObject, "filmIdealSensitivityOptimizeButton")
        assert ideal_optimize is not None and ideal_optimize.isEnabled()
        assert window.findChild(QObject, "filmSensitivityAnalyzeButton") is None
        click(app, window, _find_visual_item(root, "filmSensitivityIdealTab"))
        assert panel.activeWorkflow == "ideal"
        click(app, window, _find_visual_item(root, "filmSensitivityTableTab"))
        assert panel.activeWorkflow == "table"
        assert not warnings, warnings


def test_default_ideal_tab_is_visible_and_blank_scale_click_cannot_submit(harness, app):
    panel = harness.panel
    assert panel.idealSensitivityMode == ""
    with window_runtime(app, harness.owner) as (window, warnings):
        click_operation_bar_action(app, window, "filmSensitivityOpenButton")
        root = window.contentItem()
        tabs = window.findChild(QObject, "filmSensitivityWorkflowTabs")
        assert tabs.property("currentIndex") == 1
        assert panel.activeWorkflow == "ideal"
        ideal_page = _find_visual_item(root, "filmIdealSensitivityScroll")
        assert ideal_page is not None and ideal_page.isVisible()
        optimize = window.findChild(QObject, "filmIdealSensitivityOptimizeButton")
        assert optimize is not None and optimize.isEnabled()

        click(app, window, optimize)
        assert "请选择绝对或相对" in panel.idealError
        assert panel.idealSensitivityMode == ""
        assert not panel.busy
        assert not harness.pool.tasks and not harness.client.submissions
        assert not warnings, warnings


def test_table_blank_rejects_but_explicit_zero_is_frozen_and_never_applicable(harness):
    panel = harness.panel
    assert not panel.startThicknessErrorTableAnalysis()
    assert "全空" in panel.tableError or "至少填写" in panel.tableError
    assert not harness.pool.tasks and not harness.client.submissions
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    _set_bias(panel, layer_id, "0")
    request = panel._build_thickness_error_request(
        harness.owner._build_request(require_variables=False, fixed_topology=False))
    row = next(item for item in request.rows if item.layer_id == layer_id)
    assert row.bias == 0. and row.bias_unit == "absolute_nm"
    assert _run_table(harness) is not None
    assert not panel.idealCanApply and not panel.applySelectedBest()
    assert "不可应用" in panel.tableResultSummary


def test_signed_percent_and_standard_deviation_convert_to_relative_fraction(harness):
    panel = harness.panel
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    assert panel.setTableDraft(layer_id, "bias", "-2.5")
    assert panel.setTableUnit(layer_id, "bias", "relative_percent")
    assert panel.setTableDraft(layer_id, "uncertainty_magnitude", "0")
    assert panel.setTableUnit(layer_id, "uncertainty", "relative_percent")
    assert panel.setTableUncertaintyKind(layer_id, "stddev")
    request = panel._build_thickness_error_request(
        harness.owner._build_request(require_variables=False, fixed_topology=False))
    row = next(item for item in request.rows if item.layer_id == layer_id)
    assert row.bias == pytest.approx(-.025) and row.bias_unit == "relative_fraction"
    assert row.uncertainty_kind == "stddev"
    assert row.uncertainty_magnitude == 0. and row.uncertainty_unit == "relative_fraction"
    assert request.correlation_matrix is None
    assert panel.tableStandardDeviationLayerIds == [layer_id]


def test_disabled_configured_row_is_frozen_but_not_counted_as_enabled_coverage(harness):
    panel = harness.panel
    rows = panel.thicknessErrorRows
    disabled_id, enabled_id = rows[0]["layer_id"], rows[1]["layer_id"]
    _set_bias(panel, disabled_id, "-2.5", "relative_percent")
    assert panel.setTableLayerEnabled(disabled_id, False)
    _set_bias(panel, enabled_id, "0", "absolute_nm")
    request = panel._build_thickness_error_request(
        harness.owner._build_request(require_variables=False, fixed_topology=False))
    disabled = next(row for row in request.rows if row.layer_id == disabled_id)
    assert not disabled.enabled and disabled.bias == pytest.approx(-.025)
    assert disabled.bias_unit == "relative_fraction"


def test_manual_rho_matrix_keeps_its_initial_ordered_layer_identity(harness):
    panel = harness.panel
    layer_ids = _prepare_three_stddev_rows(harness)
    matrix_text = "[[1,0.5,0],[0.5,1,0.1],[0,0.1,1]]"
    assert panel.setTableCorrelationDraft(matrix_text)
    assert panel._table_correlation_bound_layer_ids == tuple(layer_ids)
    request = panel._build_thickness_error_request(
        harness.owner._build_request(require_variables=False, fixed_topology=False))
    assert request.correlation_layer_ids == tuple(layer_ids)
    assert request.correlation_matrix == ((1., .5, 0.), (.5, 1., .1), (0., .1, 1.))


@pytest.mark.parametrize("change", ["reorder", "row_kind", "disable", "delete"])
def test_manual_rho_rejects_changed_layer_order_or_membership_without_reinterpreting(
        harness, change):
    panel = harness.panel
    layer_ids = _prepare_three_stddev_rows(harness)
    matrix_text = "[[1,0.5,0],[0.5,1,0.1],[0,0.1,1]]"
    assert panel.setTableCorrelationDraft(matrix_text)
    if change == "reorder":
        harness.session.design["front_layers"].reverse()
        harness.session.design_revision += 1
        harness.owner.refreshDesign()
        assert panel.prepare(), panel.error
    elif change == "row_kind":
        assert panel.setTableUncertaintyKind(layer_ids[0], "bounded")
    elif change == "disable":
        assert panel.setTableLayerEnabled(layer_ids[0], False)
    else:
        harness.session.design["front_layers"] = [
            row for row in harness.session.design["front_layers"] if row["id"] != layer_ids[1]]
        harness.session.design_revision += 1
        harness.owner.refreshDesign()
        assert panel.prepare(), panel.error

    current_ids = panel.tableStandardDeviationLayerIds
    assert tuple(current_ids) != tuple(layer_ids)
    assert panel.tableCorrelationJson == matrix_text
    assert panel.tableError.startswith("rho 矩阵身份已失效")
    assert all(layer_id in panel.tableError for layer_id in (*layer_ids, *current_ids))
    assert "清空 rho 矩阵文本" in panel.tableError
    with pytest.raises(ValueError, match="rho 矩阵身份已失效"):
        panel._build_thickness_error_request(
            harness.owner._build_request(require_variables=False, fixed_topology=False))
    assert panel.tableCorrelationJson == matrix_text


def test_explicit_uncorrelated_identity_matrix_is_safe_after_reorder(harness):
    panel = harness.panel
    layer_ids = _prepare_three_stddev_rows(harness)
    matrix_text = "[[1,0.5,0],[0.5,1,0.1],[0,0.1,1]]"
    assert panel.setTableCorrelationDraft(matrix_text)
    assert panel.setTableUncorrelated(True)
    harness.session.design["front_layers"].reverse()
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare(), panel.error
    current_ids = tuple(panel.tableStandardDeviationLayerIds)
    assert current_ids == tuple(reversed(layer_ids))
    assert panel.tableCorrelationJson == matrix_text
    assert not panel.tableError.startswith("rho 矩阵身份已失效")
    request = panel._build_thickness_error_request(
        harness.owner._build_request(require_variables=False, fixed_topology=False))
    assert request.correlation_layer_ids == current_ids
    assert request.correlation_matrix == ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))


@pytest.mark.parametrize("field,value", [("bias", "-"), ("bias", "1e"),
                                          ("uncertainty_magnitude", "nan"),
                                          ("uncertainty_magnitude", "-1")])
def test_invalid_or_half_entered_table_value_never_reuses_a_prior_result(harness, field, value):
    panel = harness.panel
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    if field == "bias":
        _set_bias(panel, layer_id, "1")
        assert panel._build_thickness_error_request(
            harness.owner._build_request(require_variables=False, fixed_topology=False))
        assert panel.setTableDraft(layer_id, "bias", value)
    else:
        assert panel.setTableUncertaintyKind(layer_id, "bounded")
        assert panel.setTableUnit(layer_id, "uncertainty", "absolute_nm")
        assert panel.setTableDraft(layer_id, "uncertainty_magnitude", value)
    assert not panel.startThicknessErrorTableAnalysis()
    assert not harness.pool.tasks and not harness.client.submissions
    assert panel.tableError


def test_table_reorder_and_material_label_change_preserve_drafts_by_layer_id(harness):
    panel = harness.panel
    original = panel.thicknessErrorRows
    first = original[0]
    _set_bias(panel, first["layer_id"], "-1.25", "relative_percent")
    design = harness.session.design
    design["front_layers"].reverse()
    a, b = design["front_layers"][-1], design["front_layers"][-2]
    a["material_id"], b["material_id"] = b["material_id"], a["material_id"]
    a["optical_set_id"], b["optical_set_id"] = b["optical_set_id"], a["optical_set_id"]
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare(), panel.error
    rows = panel.thicknessErrorRows
    assert rows[0]["layer_id"] == original[-1]["layer_id"]
    preserved = next(row for row in rows if row["layer_id"] == first["layer_id"])
    assert preserved["bias_text"] == "-1.25" and preserved["bias_unit"] == "relative_percent"
    assert preserved["layer_label"] != first["layer_label"] or preserved["nominal"] == first["nominal"]


@pytest.mark.parametrize("window_height,font_scale", [(520, 1.0), (760, 1.0), (520, 2.0)])
def test_table_and_ideal_drafts_are_isolated_and_focus_survives_typing(harness, app, window_height, font_scale):
    panel = harness.panel
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    _set_bias(panel, layer_id, "1")
    assert panel.setIdealSensitivityMode("relative")
    assert panel.idealSensitivityMode == "relative"
    assert panel.thicknessErrorRows[0]["bias_text"] == "1"
    with window_runtime(app, harness.owner) as (window, warnings):
        window.setHeight(window_height)
        from coat_sim.ui.theme_service import ThemeService
        ThemeService.instance().configure(font_scale=font_scale)
        app.processEvents()
        click_operation_bar_action(app, window, "filmSensitivityOpenButton")
        click(app, window, _find_visual_item(window.contentItem(), "filmSensitivityTableTab"))
        assert panel.activeWorkflow == "table"
        section = _find_visual_item(window.contentItem(), "filmThicknessLayerOverridesSection")
        assert section is not None
        header = section.findChild(QQuickItem, "themeSectionHeader")
        assert header is not None
        _scroll_editor_into_view(app, header)
        click(app, window, header)
        assert section.property("expanded")
        field = _find_visual_item(window.contentItem(), "filmThicknessBias_" + layer_id)
        assert field is not None, warnings
        _scroll_editor_into_view(app, field)
        click(app, window, field)
        QTest.qWait(15)
        assert field.property("activeFocus")
        QTest.keyClick(window, Qt.Key.Key_1)
        QTest.keyClick(window, Qt.Key.Key_E)
        app.processEvents()
        assert _find_visual_item(window.contentItem(), field.objectName()) is field
        assert field.property("activeFocus")
        assert panel.thicknessErrorRows[0]["bias_text"].endswith("1e")
        assert not warnings, warnings


def test_tab_switch_and_dialog_reopen_preserve_all_workflow_drafts_and_results(harness, app):
    panel = harness.panel

    # Create real physics-backed results for both independent current entries.
    table_layer = panel.thicknessErrorRows[0]["layer_id"]
    _set_bias(panel, table_layer, "1")
    _run_table(harness)
    table_job_id = panel.tableJobId

    _bounded_variable(harness)
    assert panel.setIdealSensitivityMode("absolute")
    # Exercise the current strict policy without degradation allowances.
    assert panel.setIdealSensitivityPolicy("strict_nominal")
    assert panel.setIdealOption("max_iterations", "2")
    assert panel.setIdealOption("max_evaluations", "12")
    assert panel.startIdealSensitivityOptimization(), panel.idealError
    harness.pool.run()
    assert panel.idealHasResult, panel.idealError
    ideal_job_id = panel.idealJobId

    with window_runtime(app, harness.owner) as (window, warnings):
        open_button = window.findChild(QQuickItem, "filmSensitivityOpenButton")
        assert open_button is not None
        click_operation_bar_action(app, window, "filmSensitivityOpenButton")
        root = window.contentItem()
        tabs = window.findChild(QObject, "filmSensitivityWorkflowTabs")
        assert tabs.property("currentIndex") == 1

        for index, name in ((0, "table"), (1, "ideal"), (0, "table")):
            click(app, window, _find_visual_item(root, (
                "filmSensitivityTableTab", "filmSensitivityIdealTab"
            )[index]))
            assert panel.activeWorkflow == name
            assert panel.tableHasResult and panel.tableJobId == table_job_id
            assert panel.idealHasResult and panel.idealJobId == ideal_job_id
            assert panel.jobId == {"table": table_job_id, "ideal": ideal_job_id}[name]
            assert panel.thicknessErrorRows[0]["bias_text"] == "1"
            assert panel.idealSensitivityMode == "absolute"

        # The dialog is a persistent child of the optimization window; hiding
        # and reopening it must retain the explicitly selected tab and state.
        dialog = window.findChild(QObject, "filmSensitivityDialog")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        app.processEvents()
        assert not dialog.property("visible")
        click_operation_bar_action(app, window, "filmSensitivityOpenButton")
        assert dialog.property("visible")
        assert tabs.property("currentIndex") == 0
        assert panel.activeWorkflow == "table"
        assert panel.tableHasResult and panel.idealHasResult
        assert panel.tableJobId == table_job_id and panel.idealJobId == ideal_job_id
        assert panel.jobId == table_job_id
        assert panel.thicknessErrorRows[0]["bias_text"] == "1"
        assert panel.idealSensitivityMode == "absolute"
        assert not warnings, warnings


def test_unknown_submission_cancel_generation_and_late_result_are_fenced(harness):
    panel = harness.panel
    layer_id = panel.thicknessErrorRows[0]["layer_id"]
    _set_bias(panel, layer_id, "1")
    assert panel.startThicknessErrorTableAnalysis()
    first_generation = panel._generation
    assert not panel.startThicknessErrorTableAnalysis()
    panel.cancel()
    harness.pool.run()
    assert not panel.tableHasResult
    assert not harness.client.submissions
    assert panel.startThicknessErrorTableAnalysis()
    current_generation = panel._generation
    assert current_generation > first_generation
    panel._on_finished(first_generation, {"state": "completed"}, "late")
    assert panel._generation == current_generation and not panel.tableHasResult
    panel.cancel()
    harness.pool.run()

    harness.client.unknown = True
    assert panel.startThicknessErrorTableAnalysis()
    job_id = panel.tableJobId
    harness.pool.run()
    assert panel.tableError.startswith("submission_unknown:")
    assert not panel.startThicknessErrorTableAnalysis()
    assert panel.tableJobId == job_id and len(harness.client.submissions) == 1


def test_table_result_history_is_read_only_and_report_failure_preserves_file(harness, tmp_path, monkeypatch):
    panel = harness.panel
    _set_bias(panel, panel.thicknessErrorRows[0]["layer_id"], "1")
    payload = _run_table(harness)
    target = tmp_path / "existing.json"
    target.write_text("original bytes", encoding="utf-8")
    monkeypatch.setattr("coat_sim.ui.film_sensitivity_quick_controller.atomic_film_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    panel.selectWorkflow(0)
    assert not panel.saveReport(str(target), overwrite=True)
    assert target.read_text(encoding="utf-8") == "original bytes"
    assert "disk full" in panel.tableError
    panel.load_verified_history(payload, "table-history-1")
    assert panel.activeWorkflow == "table" and panel.tableJobId == "table-history-1"
    assert panel.tableHasResult and not panel.idealCanApply and not panel.applySelectedBest()
    assert not panel.applySelectedBest()


def test_ideal_requires_explicit_scale_and_selected_columns_and_source_fence(harness):
    panel = harness.panel
    layer_id = _bounded_variable(harness)
    assert not panel.startIdealSensitivityOptimization()
    assert "请选择绝对或相对" in panel.idealError
    assert panel.setIdealSensitivityMode("absolute")
    for row in panel.idealSensitivityLayers:
        assert panel.setIdealSensitivityLayer(row["layer_id"], False)
    assert not panel.startIdealSensitivityOptimization()
    assert "至少选择一个" in panel.idealError
    assert panel.setIdealSensitivityLayer(layer_id, True)
    # Exercise the current strict nominal policy.
    assert panel.setIdealSensitivityPolicy("strict_nominal")
    assert panel.setIdealOption("max_iterations", "2")
    assert panel.setIdealOption("max_evaluations", "12")
    assert panel.startIdealSensitivityOptimization(), panel.idealError
    harness.pool.run()
    assert panel.idealHasResult, panel.idealError
    assert JobSpec.parse_frozen(harness.client.submissions[-1][0]).executor == "film_ideal_sensitivity"
    assert "absolute" in panel.idealResultSummary
    if panel.idealCanApply:
        harness.session.design_revision += 1
        assert not panel.idealCanApply
        assert not panel.applySelectedBest()
        assert not harness.source.applied
