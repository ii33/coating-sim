"""Process UI -> frozen JobSpec -> production physics, without any IPC endpoint."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core import film_sensitivity_jobs as jobs
from coat_sim.core.film_sensitivity import (
    film_sensitivity_request_from_payload,
    film_sensitivity_request_to_payload,
)
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import click, optimizer, window_runtime
from tests.test_film_sensitivity_quick_controller import _HeldPool, _PhysicsClient

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def process(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (owner, source, session):
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = owner.sensitivityController
        panel.pool = pool = _HeldPool()
        client = _PhysicsClient()
        panel.client_factory = lambda _root: client
        assert panel.prepare()
        yield SimpleNamespace(panel=panel, owner=owner, source=source, session=session,
                              pool=pool, client=client, design=design)


def configure(process, *, distribution="bounded_uniform", model="independent", count="8", seed="19"):
    panel = process.panel
    layer_id = panel.layerErrors[0]["layer_id"]
    assert panel.setErrorComponent(layer_id, "absolute_nm", "1.5")
    assert panel.setErrorComponent(layer_id, "relative_percent", "2")
    assert panel.setChoice("model", model)
    assert panel.setChoice("semantics", "standard_deviation" if distribution == "gaussian" else "bounded_amplitude")
    assert panel.setChoice("distribution", distribution)
    assert panel.setSamplingOption("sample_count", count)
    assert panel.setSamplingOption("seed", seed)
    return layer_id


def run(process, operation="analyze"):
    assert process.panel.start(operation), process.panel.error
    process.pool.run()
    assert not process.panel.busy and process.panel.hasResult, process.panel.error
    return process.panel._payload


def test_new_panel_requires_explicit_process_data_without_default_or_submission(process):
    panel = process.panel
    assert panel.optimizationPolicy == "nominal_preserving"
    assert panel.robustnessObjective == "finite_scenario_worst"
    assert panel.errorModel == panel.amplitudeSemantics == panel.distribution == ""
    assert panel.sampling == {"sample_count": "", "seed": ""}
    assert panel.layerErrors[0]["magnitude"] == panel.layerErrors[0]["absolute_nm"] == ""
    assert panel.layerErrors[0]["relative_percent"] == ""
    assert not panel.generateProcessScenarios() and not panel.start("optimize")
    assert "工艺" in panel.error
    assert not process.pool.tasks and not process.client.submissions
    configure(process)
    assert not panel.start("analyze") and "场景" in panel.error
    assert not process.pool.tasks


@pytest.mark.parametrize("model", ["independent", "common_mode", "material_common_mode", "linear_drift"])
@pytest.mark.parametrize("distribution", ["bounded_uniform", "gaussian"])
def test_process_choices_freeze_exact_cloud_and_additive_units_in_real_physics(process, model, distribution):
    panel = process.panel
    configure(process, model=model, distribution=distribution, count="24")
    assert panel.generateProcessScenarios(), panel.error
    scenarios = panel._scenarios
    assert panel.generateProcessScenarios() and panel._scenarios == scenarios
    original = deepcopy(process.session.design)
    payload = run(process)
    spec, _job_id, metadata = process.client.submissions[-1]
    request = jobs.deserialize_job_payload(spec["parameters"]["payload"])
    assert request.optimization_policy == "nominal_preserving"
    assert request.error_model == model
    assert request.error_amplitude_semantics == ("standard_deviation" if distribution == "gaussian" else "bounded_amplitude")
    assert request.scenarios == scenarios
    assert request.errors[0].magnitude == 0.
    assert request.errors[0].absolute_nm == 1.5
    assert request.errors[0].relative_fraction == .02
    assert payload["best_analysis"]["linear_conditions"][0]["error_amplitudes_nm"] == pytest.approx([3.5])
    assert len(payload["best_analysis"]["finite_scenarios"]) == 24
    assert metadata["process_scenario_generation"]["distribution"] == distribution
    assert metadata["process_scenario_generation"]["seed"] == 19
    if distribution == "gaussian":
        assert max(abs(row.coefficients[0]) for row in request.scenarios) > 1.
    else:
        assert max(abs(row.coefficients[0]) for row in request.scenarios) <= 1.
    assert process.session.design == original and not process.source.applied
    summary = panel.resultSummary
    assert "nominal_preserving" in summary and "名义" in summary and "有限场景" in summary
    assert "非全局" in summary and "不是制造良率" in summary
    assert panel.setChoice("policy", "robustness_only")
    assert panel.setChoice("semantics", "bounded_amplitude" if distribution == "gaussian" else "standard_deviation")
    assert panel.resultSummary == summary  # Drafts never relabel a frozen result.


@pytest.mark.parametrize("field,value", [("absolute_nm", "nan"), ("absolute_nm", "-1"),
                                         ("relative_percent", "inf"), ("absolute_nm", "1e")])
def test_partial_or_invalid_process_input_is_not_last_committed_value(process, field, value):
    configure(process)
    panel = process.panel
    assert panel.generateProcessScenarios()
    assert panel.setErrorDraft(panel.layerErrors[0]["layer_id"], field, value)
    assert panel.scenarioCount == 0
    assert not panel.start("analyze") and not panel.generateProcessScenarios()
    assert not process.pool.tasks and not process.client.submissions


@pytest.mark.parametrize("field,value", [("sample_count", ""), ("sample_count", "1.5"),
                                         ("sample_count", "0"), ("sample_count", "100001"),
                                         ("seed", ""), ("seed", "-1"), ("seed", "True")])
def test_sampling_requires_explicit_strict_integers(process, field, value):
    configure(process)
    assert process.panel.setSamplingOption(field, value)
    assert not process.panel.generateProcessScenarios()
    assert process.panel.scenarioCount == 0 and process.panel.error
    assert not process.client.submissions


def test_semantics_mismatch_and_nontext_cannot_be_silently_coerced(process):
    panel = process.panel
    layer_id = configure(process)
    assert not panel.setErrorComponent(layer_id, "absolute_nm", True)
    assert not panel.setMagnitude(layer_id, 2.5)
    assert not panel.setSamplingOption("seed", True)
    assert not panel.setOption("max_evaluations", True)
    assert not panel.setLayerEnabled(layer_id, "false")
    assert panel.setChoice("distribution", "gaussian")
    assert not panel.generateProcessScenarios() and "1σ" in panel.error
    assert panel.setChoice("semantics", "standard_deviation")
    assert panel.generateProcessScenarios()
    assert panel.setSamplingOption("seed", "20") and panel.scenarioCount == 0
    assert not panel.start("analyze")


def test_source_change_invalidates_cloud_instead_of_reusing_or_regenerating(process):
    configure(process)
    panel = process.panel
    assert panel.generateProcessScenarios()
    process.session.design["front_layers"][0]["thickness"]["value"] += 1.
    process.session.design_revision += 1
    process.owner.refreshDesign()
    assert not panel.start("analyze")
    assert "来源设计" in panel.error and panel.scenarioCount == 0
    assert not process.pool.tasks
    assert panel.generateProcessScenarios()
    assert run(process)["frozen_request"]["design_snapshot"]["front_layers"] == process.session.design["front_layers"]


def test_linear_analysis_stays_separate_and_policy_is_not_migrated(process):
    panel = process.panel
    configure(process)
    assert panel.setChoice("objective", "linear_sensitivity_norm")
    assert not panel.start("optimize") and "保名义优化只支持" in panel.error
    payload = run(process)
    assert payload["state"] == "completed" and not panel.canApply
    raw = film_sensitivity_request_to_payload(jobs.deserialize_job_payload(
        process.client.submissions[-1][0]["parameters"]["payload"]))
    raw.pop("optimization_policy", None)
    assert film_sensitivity_request_from_payload(raw).optimization_policy == "robustness_only"
    assert panel.setChoice("policy", "robustness_only")
    assert panel.optimizationPolicy == "robustness_only"


def test_initial_nominal_failure_explains_restore_without_target_changes(process):
    panel = process.panel
    configure(process)
    assert process.owner.setObjectiveField("upper_bound_percent", 0.)
    layer_id = panel.layerErrors[0]["layer_id"]
    assert process.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert process.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    before = process.owner._build_request(require_variables=False, fixed_topology=False)
    assert panel.generateProcessScenarios()
    payload = run(process, "optimize")
    assert payload["state"] == "failed" and payload["termination_reason"] == "initial_nominal_infeasible"
    assert "先恢复名义" in panel.resultSummary and not panel.canApply
    assert not panel.applyBest() and not process.source.applied
    assert process.owner._build_request(require_variables=False, fixed_topology=False).objectives == before.objectives


def test_valid_nominal_partial_has_explicit_apply_and_atomic_report(process, tmp_path):
    panel = process.panel
    layer_id = configure(process)
    assert process.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert process.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    assert panel.generateProcessScenarios()
    baseline = run(process)
    nominal = baseline["best_analysis"]["nominal_conditions"][0]["actual"]
    assert process.owner.setObjectiveField("upper_bound_percent", nominal * 100 * 1.00001)
    assert panel.setOption("max_evaluations", "1")
    assert panel.generateProcessScenarios()
    before = deepcopy(process.session.design)
    payload = run(process, "optimize")
    assert payload["state"] == "partial" and payload["best_analysis"]["nominal_targets_met"]
    assert panel.canApply and process.session.design == before and not process.source.applied
    target = tmp_path / "process.json"
    assert panel.saveReport(str(target))
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    panel.load_verified_history(payload, "verified-history")
    assert not panel.canApply and not panel.applyBest()


def activate(window, name, index):
    box = window.findChild(QObject, name)
    assert box is not None
    assert box.setProperty("currentIndex", index)
    assert QMetaObject.invokeMethod(box, "activated", Q_ARG(int, index))


def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None


def test_real_qml_actions_capture_unfinished_typing_and_submit_frozen_process(process, app):
    panel = process.panel
    layer_id = panel.layerErrors[0]["layer_id"]
    with window_runtime(app, process.owner) as (window, warnings):
        click(app, window, window.findChild(QQuickItem, "filmSensitivityOpenButton"))
        legacy = visual_item(window.contentItem(), "filmSensitivityLegacyTab")
        assert legacy is not None
        click(app, window, legacy)
        assert panel.activeWorkflow == "legacy"
        dialog = window.findChild(QObject, "filmSensitivityDialog")
        assert dialog.property("visible")
        for name in ("filmSensitivityPolicyBox", "filmSensitivitySemanticsBox", "filmSensitivityDistributionBox",
                     "filmSensitivitySampleCountField", "filmSensitivitySeedField"):
            assert window.findChild(QObject, name) is not None
        activate(window, "filmSensitivityModelBox", 1)
        activate(window, "filmSensitivitySemanticsBox", 1)
        activate(window, "filmSensitivityDistributionBox", 1)
        absolute = visual_item(window.contentItem(), "filmSensitivityAbsolute_" + layer_id)
        assert absolute is not None
        absolute.forceActiveFocus()
        QTest.qWait(20)
        app.processEvents()
        for key in (Qt.Key.Key_1, Qt.Key.Key_E):
            QTest.keyClick(window, key)
        app.processEvents()
        assert visual_item(window.contentItem(), absolute.objectName()) is absolute
        assert absolute.hasActiveFocus() and panel.layerErrors[0]["absolute_nm"].lower() == "1e"
        assert not panel.start("analyze") and not process.pool.tasks
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window, Qt.Key.Key_2)
        app.processEvents()
        assert panel.layerErrors[0]["absolute_nm"] == "2"
        assert panel.setSamplingOption("sample_count", "8")
        assert panel.setSamplingOption("seed", "7")
        generate = window.findChild(QQuickItem, "filmSensitivityGenerateScenariosButton")
        scroll = window.findChild(QObject, "filmSensitivityConfigurationScroll")
        flickable = scroll.property("contentItem")
        flickable.setProperty("contentY", max(0., generate.y() - flickable.height() / 2.))
        QTest.qWait(20)
        click(app, window, generate)
        assert panel.scenarioCount == 8, panel.error
        click(app, window, window.findChild(QQuickItem, "filmSensitivityAnalyzeButton"))
        assert panel.busy and len(process.pool.tasks) == 1
        process.pool.run()
        assert panel.hasResult and not panel.canApply
        assert process.client.submissions[-1][0]["executor"] == "film_sensitivity"
        assert not window.grabWindow().isNull()
        assert not warnings, warnings


def test_nominal_candidate_qml_apply_requires_existing_confirmation(process, app):
    panel = process.panel
    layer_id = configure(process)
    assert process.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert process.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    assert panel.generateProcessScenarios()
    baseline = run(process)
    nominal = baseline["best_analysis"]["nominal_conditions"][0]["actual"]
    assert process.owner.setObjectiveField("upper_bound_percent", nominal * 100 * 1.00001)
    assert panel.setOption("max_evaluations", "1")
    assert panel.generateProcessScenarios()
    payload = run(process, "optimize")
    assert payload["state"] == "partial" and panel.canApply
    with window_runtime(app, process.owner) as (window, warnings):
        click(app, window, window.findChild(QQuickItem, "filmSensitivityOpenButton"))
        legacy = visual_item(window.contentItem(), "filmSensitivityLegacyTab")
        assert legacy is not None
        click(app, window, legacy)
        assert panel.activeWorkflow == "legacy"
        apply = window.findChild(QQuickItem, "filmSensitivityApplyButton")
        assert apply is not None and apply.isEnabled()
        click(app, window, apply)
        confirm = window.findChild(QObject, "filmSensitivityApplyConfirmDialog")
        assert confirm is not None and confirm.property("visible")
        assert not process.source.applied and panel.canApply
        assert QMetaObject.invokeMethod(confirm, "reject")
        app.processEvents()
        assert not confirm.property("visible") and not process.source.applied
        assert panel.canApply
        click(app, window, apply)
        assert confirm.property("visible") and not process.source.applied
        assert QMetaObject.invokeMethod(confirm, "accept")
        app.processEvents()
        assert process.source.applied and not panel.canApply
        assert not warnings, warnings
