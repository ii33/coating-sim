"""Real Quick loading and asynchronous read-only resource evaluation boundaries."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
from pathlib import Path
import threading
import time

import pytest
from PySide6.QtCore import QObject, QMetaObject, QSettings, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
import shiboken6

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService
from coat_sim.ui import film_resource_estimate_controller as module

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.qt, pytest.mark.contract]


def wait_until(app, predicate, timeout=3000):
    until = time.monotonic() + timeout / 1000
    while time.monotonic() < until and not predicate():
        app.processEvents()
        QTest.qWait(10)
    assert predicate()


@pytest.fixture
def runtime(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    service = FilmDesignService(tmp_path, restrict_to_root=False)
    controller = FilmDesignQuickController(
        tmp_path,
        service=service,
        geometry_settings=QSettings(str(tmp_path / "geometry.ini"), QSettings.Format.IniFormat),
    )
    engine = QQmlEngine()
    ThemeService.instance().attach_engine(engine, "Theme")
    warnings: list[str] = []
    engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
    component = QQmlComponent(
        engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml"))
    )
    window = component.createWithInitialProperties({"controller": controller})
    assert isinstance(window, QQuickWindow), component.errorString()
    window.show()
    app.processEvents()
    yield app, controller, window, warnings
    assert not controller._resource_estimator().busy
    window.setProperty("allowClose", True)
    window.hide()
    shiboken6.delete(window)
    shiboken6.delete(component)
    shiboken6.delete(engine)
    shiboken6.delete(controller)
    app.processEvents()


def select_film(controller):
    estimator = controller.resourceEstimateController
    assert estimator.setProbeMaxEvaluations("0")  # isolate the pre-existing audit checks
    estimator.refreshMaterials()
    row = estimator.addMaterialRow()
    assert estimator.setRowMaterial(row, "acceptance-film"), estimator.materialError
    return estimator


def test_design_sidebar_removed_and_no_configuration(runtime):
    app, controller, window, warnings = runtime
    # Resource assessment has moved to the optimizer and is no longer a sidebar page.
    assert window.findChild(QObject, "filmDesignSidebarResourceEstimate") is None
    before = deepcopy(controller.session.snapshot().design)
    # Start via controller to avoid depending on keyboard-focus timing.
    assert not controller.runResourceEstimate()
    assert "候选材料" in controller.resourceEstimateController.status
    assert controller.session.snapshot().design == before
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_cancel_rerun_stale_and_no_writeback(runtime, monkeypatch):
    app, controller, window, warnings = runtime
    entered = threading.Event()
    release = threading.Event()

    def controlled_run(task):
        entered.set()
        while not release.is_set() and not task.cancel.is_set():
            time.sleep(0.005)
        result: dict[str, object] = {
            "state": "cancelled" if task.cancel.is_set() else "completed",
            "message": "test",
            "bounds": [],
            "screenings": [],
            "candidate_reports": [],
            "pareto_witnesses": [],
            "feasibility_state": "unknown",
            "work": {},
        }
        task.signals.finished.emit(task.token, result, None)

    monkeypatch.setattr(module._Task, "run", controlled_run)
    estimator = controller.resourceEstimateController
    monkeypatch.setattr(controller.service, "material_options", lambda kind: [{"id": "m", "name": "M", "optical_sets": [{"id": "s", "name": "S"}]}])
    estimator.refreshMaterials()
    assert estimator.setSubstrateMaterial("m")
    row = estimator.addMaterialRow()
    assert estimator.setRowMaterial(row, "m")
    before = deepcopy(controller.session.snapshot().design)
    assert estimator.start()
    assert entered.wait(1)
    assert not estimator.start()
    estimator.cancel()
    assert "取消" in estimator.status
    wait_until(app, lambda: not estimator.busy)
    assert controller.session.snapshot().design == before
    entered.clear()
    assert estimator.start()
    assert entered.wait(1)
    current_status = estimator.status
    estimator._on_finished(estimator._token - 1, {"state": "completed", "message": "late"}, None)
    assert estimator.busy and estimator.status == current_status
    assert controller.session.set_design_value("name", "changed during evaluation")
    app.processEvents()
    assert estimator.stale
    release.set()
    wait_until(app, lambda: not estimator.busy)
    assert estimator.stale and "过期" in estimator.status
    assert estimator.resultSummary
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_invalid_limit_never_starts(runtime):
    app, controller, _window, _warnings = runtime
    estimator = controller.resourceEstimateController
    assert not estimator.setLayerLimit("0")
    assert not estimator.limitValid and not estimator.start()
    assert estimator.setLayerLimit("256") and estimator.limitValid


def test_real_core_request_uses_saved_targets_and_does_not_mutate(runtime, tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration
    from coat_sim.core.film_resource_estimation import estimate_film_resources

    app, controller, _window, _warnings = runtime
    service, _library, design = fixture(tmp_path / "materials")
    config = saved_configuration(service, design)
    objective = config["objectives"][0]
    objective.update(
        wavelength_min_nm=500.0,
        wavelength_max_nm=500.0,
        wavelength_samples=1,
        wavelength_step_nm=1.0,
        actual_wavelength_step_nm=0.0,
    )
    design["workflow_settings"] = {"optimization": config}
    before = deepcopy(design)
    request = module._prepare_request(design, 19, 8, threading.Event(), config["synthesis"]["material_rows"], probe_max_evaluations=0)
    assert request.synthesis_request.selected_surfaces == ("front",)
    assert request.synthesis_request.environment["front_layers"] == []
    assert request.candidate_designs[0]["front_layers"] == before["front_layers"]
    assert request.synthesis_request.candidate_materials[0].material_id == "acceptance-film"
    result = estimate_film_resources(service, request)
    assert result.input_status == "ready", result.message
    assert result.state == "completed", result.message
    assert result.candidate_reports and result.candidate_reports[0]["state"] == "audited"
    assert result.estimate_state == "not_calibrated" and not result.continuous_certificate
    assert "unknown" in module._format_result(__import__("dataclasses").asdict(result))
    assert design == before


def test_real_core_audits_current_design_without_writing(runtime, tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration

    app, controller, window, warnings = runtime
    service, _library, design = fixture(tmp_path / "qml-materials")
    config = saved_configuration(service, design)
    config["objectives"][0].update(
        wavelength_min_nm=500.0,
        wavelength_max_nm=500.0,
        wavelength_samples=1,
        wavelength_step_nm=1.0,
        actual_wavelength_step_nm=0.0,
    )
    design["workflow_settings"] = {"optimization": config}
    controller.service.materials = service.materials
    assert controller.session.request_replace_design(design), controller.error
    app.processEvents()
    before = deepcopy(controller.session.snapshot().design)
    select_film(controller)
    assert controller.resourceEstimateController.setLayerLimit("8")
    assert controller.runResourceEstimate()
    wait_until(app, lambda: controller.resourceEstimateController.busy, timeout=3000)
    wait_until(app, lambda: not controller.resourceEstimateController.busy, timeout=20000)
    estimator = controller.resourceEstimateController
    assert "评估完成" in estimator.status, estimator.status
    assert "当前结构" in estimator.resultSummary
    assert "审计" in estimator.resultSummary
    assert controller.session.snapshot().design == before
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_material_source_change_during_run_marks_result_stale(runtime, tmp_path, monkeypatch):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration
    from coat_sim.core import film_resource_estimation as core

    app, controller, _window, _warnings = runtime
    service, library, design = fixture(tmp_path / "changed-material")
    config = saved_configuration(service, design)
    config["objectives"][0].update(
        wavelength_min_nm=500.0,
        wavelength_max_nm=500.0,
        wavelength_samples=1,
        wavelength_step_nm=1.0,
        actual_wavelength_step_nm=0.0,
    )
    design["workflow_settings"] = {"optimization": config}
    controller.service.materials = service.materials
    assert controller.session.request_replace_design(design)
    app.processEvents()
    original = core.estimate_film_resources

    def mutate_after_freeze(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.state == "completed", result.message
        material = library.read("acceptance-film")["record"]
        material["optical_constants"][0]["n"] = [1.31, 1.31]
        library.write(material, update=True)
        return result

    monkeypatch.setattr(core, "estimate_film_resources", mutate_after_freeze)
    estimator = select_film(controller)
    assert estimator.setLayerLimit("8") and estimator.start()
    wait_until(app, lambda: not estimator.busy, timeout=20000)
    assert estimator.stale and "材料源在评估期间变化" in estimator.status, estimator.status


def test_inapplicable_reason_and_no_witness_are_not_certificates():
    output = module._format_result(
        {
            "bounds": [
                {
                    "kind": "hr_layer_dp",
                    "applicability": "not_applicable",
                    "reason": "mvp_requires_single_front_half_space",
                    "value_layers": None,
                }
            ],
            "candidate_reports": [],
            "pareto_witnesses": [],
            "screenings": [],
            "work": {},
        }
    )
    assert "不适用" in output and "单面正面半空间" in output
    assert "unknown" in output and "不是严格证书" in output and "非连续域证书" in output


def test_joint_faces_and_explicit_k_policy_are_inherited(runtime, tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration

    _app, _controller, _window, _warnings = runtime
    service, _library, design = fixture(tmp_path / "joint")
    design["substrate"]["sides"] = "double"
    design["back_layers"] = [dict(deepcopy(design["front_layers"][0]), id="back-layer")]
    config = saved_configuration(service, design)
    config["synthesis"]["selected_surfaces"] = ["front", "back"]
    config["synthesis"]["assume_unknown_k_zero"] = False
    config["synthesis"]["options"]["max_layers"] = 3
    config["synthesis"]["options"]["minimum_nonzero_thickness_nm"] = 1.5
    config["synthesis"]["options"]["max_layer_thickness_nm"] = 180.0
    design["workflow_settings"] = {"optimization": config}
    before = deepcopy(design)
    request = module._prepare_request(design, 19, 128, threading.Event(), config["synthesis"]["material_rows"])
    synthesis = request.synthesis_request
    assert synthesis.selected_surfaces == ("front", "back")
    assert (
        synthesis.environment["front_layers"] == [] and synthesis.environment["back_layers"] == []
    )
    assert synthesis.environment["assume_lossless"] is False
    assert request.search_budget_reference["max_layers"] == 3
    assert request.search_budget_reference["max_total_thickness_nm"] is None
    assert synthesis.max_layers is None  # evaluation window is not the optimization cap
    assert synthesis.minimum_nonzero_thickness_nm == 1.5
    assert synthesis.max_layer_thickness_nm == 180.0
    assert request.candidate_designs[0]["back_layers"] == before["back_layers"]
    assert design == before


def test_recommendation_conditions_are_per_node_and_freeze_attempts_separate():
    base = {
        "bounds": [],
        "screenings": [],
        "candidate_reports": [],
        "work": {"source_freeze_attempts": 1, "source_freeze_passes": 0},
    }
    conditional = {
        "candidate_index": 0,
        "layer_count": 1,
        "total_thickness_nm": 50.0,
        "conditional_on_assumption": True,
        "absorption_assumption": {
            "conditional_on_assumption": True,
            "used_affected_materials": [{"material_id": "glass", "optical_set_id": "unknown-k"}],
        },
        "claim_level": "sampled_witness",
        "continuous_certificate": False,
        "source_digest": "source-1",
    }
    known = {
        **conditional,
        "candidate_index": 1,
        "conditional_on_assumption": False,
        "absorption_assumption": {"conditional_on_assumption": False},
        "source_digest": "source-2",
    }
    missing = {**conditional, "candidate_index": 2, "absorption_assumption": {}}
    output = module._format_result({**base, "recommended_budget": [conditional, known, missing]})
    assert "条件推荐：依赖用户显式未知 k=0 假设；受影响材料：glass/unknown-k" in output
    assert "该见证未使用未知 k=0 假设" in output
    assert "假设状态 unknown；不可视作无条件推荐" in output
    assert output.count("有限采样审计见证；非连续域证书，也不是实测吸收或制造良率证书") == 3
    assert "核心材料冻结：开始 1 次；完成 0 次" in output


def test_conditional_recommendation_in_technical_details(runtime, tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration

    app, controller, window, warnings = runtime
    service, library, design = fixture(tmp_path / "conditional")
    glass = library.read("acceptance-glass")["record"]
    glass["optical_constants"][0]["k"] = None
    library.write(glass, update=True)
    config = saved_configuration(service, design)
    config["synthesis"]["assume_unknown_k_zero"] = True
    objective = config["objectives"][0]
    objective.update(
        wavelength_min_nm=500.0,
        wavelength_max_nm=500.0,
        wavelength_samples=1,
        wavelength_step_nm=1.0,
        actual_wavelength_step_nm=0.0,
    )
    objective["pointwise"].update(
        mode="range", target_value_percent=None, lower_bound_percent=0.0, upper_bound_percent=100.0
    )
    design["workflow_settings"] = {"optimization": config}
    controller.service.materials = service.materials
    assert controller.session.request_replace_design(design), controller.error
    app.processEvents()
    before = deepcopy(controller.session.snapshot().design)
    select_film(controller)
    assert controller.resourceEstimateController.setLayerLimit("8")
    assert controller.runResourceEstimate()
    wait_until(app, lambda: controller.resourceEstimateController.busy)
    wait_until(app, lambda: not controller.resourceEstimateController.busy, timeout=20000)
    estimator = controller.resourceEstimateController
    text = estimator.technicalDetails
    assert "评估完成" in estimator.status, estimator.status
    assert "条件推荐：依赖用户显式未知 k=0 假设" in text
    assert "本审计以显式未知 k=0 为条件" in text
    assert "acceptance-glass/glass-set" in text
    assert "有限采样审计见证；非连续域证书，也不是实测吸收或制造良率证书" in text
    assert "核心材料冻结：开始 1 次；完成 1 次" in text
    assert controller.session.snapshot().design == before
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_failed_freeze_shows_started_not_completed(runtime, tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration

    app, controller, window, warnings = runtime
    service, library, design = fixture(tmp_path / "strict-unknown-k")
    glass = library.read("acceptance-glass")["record"]
    glass["optical_constants"][0]["k"] = None
    library.write(glass, update=True)
    config = saved_configuration(service, design)
    config["synthesis"]["assume_unknown_k_zero"] = False
    config["objectives"][0].update(
        wavelength_min_nm=500.0,
        wavelength_max_nm=500.0,
        wavelength_samples=1,
        wavelength_step_nm=1.0,
        actual_wavelength_step_nm=0.0,
    )
    design["workflow_settings"] = {"optimization": config}
    controller.service.materials = service.materials
    assert controller.session.request_replace_design(design), controller.error
    app.processEvents()
    before = deepcopy(controller.session.snapshot().design)
    select_film(controller)
    assert controller.resourceEstimateController.setLayerLimit("8")
    assert controller.runResourceEstimate()
    wait_until(app, lambda: controller.resourceEstimateController.busy)
    wait_until(app, lambda: not controller.resourceEstimateController.busy, timeout=20000)
    estimator = controller.resourceEstimateController
    text = estimator.technicalDetails
    assert "评估未完成" in estimator.status
    assert "核心材料冻结：开始 1 次；完成 0 次" in text
    assert "unknown（未取得已验证见证）" in text
    assert "条件推荐：依赖" not in text
    assert controller.session.snapshot().design == before
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_independent_material_draft_reaches_task_and_rejects_empty_missing(runtime, monkeypatch):
    app, controller, window, warnings = runtime
    records = [{"id": "one", "name": "One", "optical_sets": [{"id": "a", "name": "A"}]},
               {"id": "two", "name": "Two", "optical_sets": [{"id": "b", "name": "B"}]}]
    monkeypatch.setattr(controller.service, "material_options", lambda kind: deepcopy(records))
    estimator = controller.resourceEstimateController
    estimator.refreshMaterials()
    assert estimator.setSubstrateMaterial("one")
    before = deepcopy(controller.session.snapshot().design)
    assert not estimator.start() and "至少" in estimator.status
    row = estimator.addMaterialRow()
    assert estimator.setRowMaterial(row, "two")
    captured = []
    def capture(task):
        captured.extend(deepcopy(task.material_rows))
        task.signals.finished.emit(task.token, {"state": "completed", "work": {}}, None)
    monkeypatch.setattr(module._Task, "run", capture)
    assert estimator.start()
    wait_until(app, lambda: not estimator.busy)
    assert [(r["material_id"], r["optical_set_id"]) for r in captured] == [("two", "b")]
    assert controller.session.snapshot().design == before
    assert estimator.materialRows[0]["material_id"] == "two"  # sidebar reopening
    records.pop()
    estimator.refreshMaterials()
    assert not estimator.start() and "不可用" in estimator.status
    assert estimator.removeMaterialRow(row)
    assert not estimator.start() and "至少" in estimator.status
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_explicit_material_rows_override_synthesis_config(tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration
    service, _library, design = fixture(tmp_path / "independent")
    config = saved_configuration(service, design)
    design["workflow_settings"] = {"optimization": config}
    before = deepcopy(design)
    rows = [{"material_id": "different", "optical_set_id": "set"}]
    request = module._prepare_request(design, 1, 8, threading.Event(), rows)
    assert [(x.material_id, x.optical_set_id) for x in request.synthesis_request.candidate_materials] == [("different", "set")]
    assert design == before
    with pytest.raises(ValueError, match="候选材料"):
        module._prepare_request(design, 1, 8, threading.Event(), [])


def test_core_assessment_reacts_to_independent_material_pool(tmp_path):
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration
    from tests.test_film_optimization_needle import _optical
    from coat_sim.core.materials import default_material_profile
    from coat_sim.core.film_resource_estimation import estimate_film_resources
    service, library, design = fixture(tmp_path / "pool")
    other = default_material_profile("Other film", id="other-film")
    other.update(category="film", optical_constants=[_optical("other-set", 2.0)])
    library.write(other)
    config = saved_configuration(service, design)
    config["objectives"][0].update(wavelength_min_nm=500.0, wavelength_max_nm=500.0,
                                    wavelength_samples=1, wavelength_step_nm=1.0,
                                    actual_wavelength_step_nm=0.0)
    design["workflow_settings"] = {"optimization": config}
    def run(mid, sid):
        request = module._prepare_request(design, 1, 8, threading.Event(),
                                          [{"material_id": mid, "optical_set_id": sid}], probe_max_evaluations=0)
        return estimate_film_resources(service, request)
    included = run("acceptance-film", "film-set")
    excluded = run("other-film", "other-set")
    assert included.state == excluded.state == "completed"
    assert included.source_digest != excluded.source_digest
    assert included.candidate_reports[0]["within_material_pool"] is True
    assert excluded.candidate_reports[0]["within_material_pool"] is False


def test_optimizer_assessment_render_shared_lifecycle_and_draft(runtime, tmp_path, monkeypatch):
    """Exercise the actual Quick window, not just string-level controller checks."""
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import window_runtime, click
    from PySide6.QtQuick import QQuickItem

    app, source, _design_window, warnings = runtime
    service, _library, design = fixture(tmp_path / 'optimizer-materials')
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    owner = FilmOptimizationQuickController(source)
    entered, release = threading.Event(), threading.Event()
    captured = []

    def held_task(task):
        captured.append((deepcopy(task.design), task.limit, deepcopy(task.substrate_reference)))
        entered.set()
        while not release.is_set() and not task.cancel.is_set():
            time.sleep(.005)
        task.signals.finished.emit(task.token, {'state': 'cancelled' if task.cancel.is_set() else 'completed',
                                               'message': 'test', 'work': {}}, None)

    monkeypatch.setattr(module._Task, 'run', held_task)
    try:
        assert owner.setSearchMode('resource_assessment')
        estimator = source.resourceEstimateController
        estimator.refreshMaterials()
        assert estimator.substrateSelection['material_id'] == 'acceptance-glass'
        row = estimator.addMaterialRow()
        assert estimator.setRowMaterial(row, 'acceptance-film')
        assert owner.setObjectiveField('wavelength_min_nm', 540.0)
        assert owner.setObjectiveField('wavelength_max_nm', 540.0)
        before = deepcopy(source.session.snapshot().design)
        with window_runtime(app, owner) as (window, qml_warnings):
            window.resize(560, 500)
            tab = window.findChild(QQuickItem, 'filmOptimizationAlgorithmTab')
            click(app, window, tab)
            app.processEvents()
            panel = window.findChild(QObject, 'filmResourceAssessmentAlgorithmOptions')
            assert panel is not None and panel.property('visible')
            assert window.findChild(QObject, 'resourceSubstrateMaterial') is not None
            assert window.findChild(QObject, 'resourceSubstrateOpticalSet') is not None
            assert not owner.canApply and not owner.canSave
            assert not owner.applyCandidate() and not owner.confirmApplyCandidate()
            limit = window.findChild(QQuickItem, 'filmResourceEstimateLayerLimit')
            assert limit is not None
            limit.forceActiveFocus()
            limit.setProperty('text', '13')
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, entered.is_set)
            assert captured[0][1] == 13
            assert captured[0][2] == {'material_id': 'acceptance-glass', 'optical_set_id': 'glass-set'}
            app.processEvents()
            assert owner.busy and not owner.startOptimization() and not owner.canApply
            assert not owner.setSearchMode('fixed_topology')
            assert captured[0][0]['workflow_settings']['optimization']['objectives'][0]['wavelength_min_nm'] == 540.0
            owner.cancelOptimization()
            wait_until(app, lambda: not estimator.busy)
            assert source.session.snapshot().design == before
            result = window.findChild(QObject, 'filmResourceEstimateResult')
            details = window.findChild(QObject, 'filmResourceEstimateTechnicalDetails')
            assert result is not None
            from PySide6.QtQml import QQmlExpression
            expr = QQmlExpression(QQmlEngine.contextForObject(result), result, 'wrapMode !== Text.NoWrap')
            assert expr.evaluate()[0] and not expr.hasError()
            assert details is not None and not details.property('visible')
            # Dense representative result, showing line wrapping in the actual scroll area.
            estimator._result = module._format_summary({'state': 'completed', 'selected_surfaces': ['front'], 'candidate_reports': [{
                'state': 'audited', 'targets_met': False, 'layer_count': 25, 'total_thickness_nm': 2345.67,
                'surfaces': {'front': {'active': True, 'layer_count': 25, 'total_thickness_nm': 2345.67},
                             'back': {'active': True, 'layer_count': 3, 'total_thickness_nm': 450.}},
                'audit': {'objective_status': [{'objective_index': i, 'metric': 'R',
                    'wavelength_min_nm': 400., 'wavelength_max_nm': 700.,
                    'pointwise': {'enabled': True, 'mode': 'range', 'worst_actual': .04,
                        'lower_bound': 0., 'upper_bound': .001, 'worst_angle_deg': 0.,
                        'worst_wavelength_nm': 550., 'targets_met': False}}
                    for i in range(5)]}}], 'work': {'probe_outcomes': [
                    {'seed': i, 'state': 'completed', 'evaluations': 256,
                     'requested_max_evaluations': 256, 'physical_evaluations': 200,
                     'candidates_retained': 3} for i in range(2)],
                    'physical_evaluations': 401, 'wavelength_points': 120701}})
            estimator.changed.emit()
            scroll = QQmlExpression(QQmlEngine.contextForObject(result), result, """(function () {
                let ancestor = parent;
                while (ancestor !== null) {
                    if ("contentY" in ancestor) {
                        const point = mapToItem(ancestor.contentItem, 0, 0);
                        ancestor.contentY = Math.min(Math.max(0, ancestor.contentHeight - ancestor.height),
                                                    Math.max(0, point.y + 180));
                    }
                    ancestor = ancestor.parent;
                }
            })()""")
            scroll.evaluate()
            assert not scroll.hasError(), scroll.error().toString()
            QTest.qWait(50)
            image = window.grabWindow()
            if not image.isNull():
                assert image.save(str(tmp_path / 'resource-assessment-narrow.png'))
            bottom = QQmlExpression(QQmlEngine.contextForObject(result), result, """(function () {
                let ancestor = parent;
                while (ancestor !== null) {
                    if ("contentY" in ancestor)
                        ancestor.contentY = Math.max(0, ancestor.contentHeight - ancestor.height);
                    ancestor = ancestor.parent;
                }
            })()""")
            bottom.evaluate()
            assert not bottom.hasError(), bottom.error().toString()
            QTest.qWait(50)
            image = window.grabWindow()
            if not image.isNull():
                assert image.save(str(tmp_path / 'resource-assessment-narrow-bottom.png'))
            assert not qml_warnings, qml_warnings
        assert not warnings, warnings
    finally:
        release.set()
        owner.shutdown(1000)
        shiboken6.delete(owner)


def test_closing_optimizer_cancels_active_resource_assessment(runtime, tmp_path, monkeypatch):
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from tests.film_structure_fixtures import fixture

    app, source, _window, _warnings = runtime
    service, _library, design = fixture(tmp_path / 'close')
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    owner = FilmOptimizationQuickController(source)
    entered = threading.Event()
    cancellation_observed = threading.Event()

    def held(task):
        entered.set()
        while not task.cancel.wait(.005):
            pass
        cancellation_observed.set()
        task.signals.finished.emit(task.token, {'state': 'cancelled', 'message': 'test', 'work': {}}, None)

    monkeypatch.setattr(module._Task, 'run', held)
    try:
        assert owner.setSearchMode('resource_assessment')
        estimator = source.resourceEstimateController
        estimator.refreshMaterials()
        row = estimator.addMaterialRow()
        assert estimator.setRowMaterial(row, 'acceptance-film')
        assert owner.startOptimization()
        assert entered.wait(1)
        owner.requestClose()
        assert cancellation_observed.wait(1)
        wait_until(app, lambda: not estimator.busy)
        assert '取消' in estimator.status
    finally:
        owner.shutdown(1000)
        shiboken6.delete(owner)


def test_active_objective_editor_flushes_before_assessment_start(runtime, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtQuick import QQuickItem
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import window_runtime

    app, source, _design_window, _warnings = runtime
    service, _library, design = fixture(tmp_path / 'objective-draft')
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    owner = FilmOptimizationQuickController(source)
    captured = []

    def capture(task):
        captured.append(deepcopy(task.design))
        task.signals.finished.emit(task.token, {'state': 'completed', 'work': {}}, None)

    monkeypatch.setattr(module._Task, 'run', capture)
    try:
        assert owner.setSearchMode('resource_assessment')
        estimator = source.resourceEstimateController
        estimator.refreshMaterials()
        row = estimator.addMaterialRow()
        assert estimator.setRowMaterial(row, 'acceptance-film')
        with window_runtime(app, owner) as (window, warnings):
            field = window.findChild(QQuickItem, 'filmOptimizationWavelengthMinField')
            assert field is not None
            field.forceActiveFocus()
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            for key in (Qt.Key.Key_5, Qt.Key.Key_3, Qt.Key.Key_5):
                QTest.keyClick(window, key)
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, lambda: bool(captured))
            wait_until(app, lambda: not estimator.busy)
            assert captured[0]['workflow_settings']['optimization']['objectives'][0]['wavelength_min_nm'] == 535
            assert not warnings, warnings
    finally:
        owner.shutdown(1000)
        shiboken6.delete(owner)


def test_probe_settings_validation_and_frozen_worker_inputs(runtime, tmp_path, monkeypatch):
    from tests.film_structure_fixtures import fixture

    app, source, _window, _warnings = runtime
    service, _library, design = fixture(tmp_path / 'probe-draft')
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    estimator = source.resourceEstimateController
    estimator.refreshMaterials()
    row = estimator.addMaterialRow()
    assert estimator.setRowMaterial(row, 'acceptance-film')
    assert estimator.probeMaxEvaluations == '256'
    assert estimator.probeSeeds == '0,1'
    assert not estimator.setProbeMaxEvaluations('-1')
    assert not estimator.probeSettingsValid and not estimator.start()
    assert not estimator.setProbeMaxEvaluations('1.5')
    assert not estimator.setProbeMaxEvaluations('1')
    assert estimator.setProbeMaxEvaluations('2')
    assert estimator.setProbeMaxEvaluations('0')  # explicit disabled probe
    assert estimator.probeSettingsValid
    assert not estimator.setProbeSeeds('not-a-seed')
    assert not estimator.setProbeSeeds('2,2')
    assert not estimator.setProbeSeeds('')
    assert estimator.setProbeSeeds('4294967295')
    assert not estimator.setProbeSeeds('4294967296')
    assert not estimator.probeSettingsValid and not estimator.start()
    assert estimator.setProbeSeeds('2,3')
    assert estimator.setProbeMaxEvaluations('7')
    before = deepcopy(source.session.snapshot().design)
    captured = []
    entered, release = threading.Event(), threading.Event()

    def hold(task):
        captured.append((task.probe_max_evaluations, task.probe_seeds))
        entered.set()
        while not release.is_set() and not task.cancel.is_set():
            time.sleep(.005)
        task.signals.finished.emit(task.token, {'state': 'cancelled', 'message': 'test', 'work': {}}, None)

    monkeypatch.setattr(module._Task, 'run', hold)
    try:
        assert estimator.start()
        assert entered.wait(1)
        assert captured == [(7, (2, 3))]
        from types import SimpleNamespace
        estimator._on_progress(estimator._token, SimpleNamespace(message='探测中', physical_evaluations=5, wavelength_points=60))
        assert '物理光谱计算 5 次' in estimator.status and '波长点 60 个' in estimator.status
        estimator._on_progress(estimator._token, SimpleNamespace(message='准备中'))
        assert '物理光谱计算 0 次' not in estimator.status
        assert not estimator.setProbeMaxEvaluations('8')
        assert not estimator.setProbeSeeds('4')
        assert source.session.snapshot().design == before
    finally:
        release.set()
        wait_until(app, lambda: not estimator.busy)
    assert estimator.setProbeMaxEvaluations('8')
    assert estimator.stale


def test_real_qml_probe_drafts_flush_before_start(runtime, tmp_path, monkeypatch):
    from PySide6.QtQuick import QQuickItem
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import click, window_runtime

    app, source, _window, _warnings = runtime
    service, _library, design = fixture(tmp_path / 'probe-qml')
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    owner = FilmOptimizationQuickController(source)
    captured = []

    def capture(task):
        captured.append((task.probe_max_evaluations, task.probe_seeds))
        task.signals.finished.emit(task.token, {'state': 'completed', 'work': {}}, None)

    monkeypatch.setattr(module._Task, 'run', capture)
    try:
        assert owner.setSearchMode('resource_assessment')
        estimator = source.resourceEstimateController
        estimator.refreshMaterials()
        row = estimator.addMaterialRow()
        assert estimator.setRowMaterial(row, 'acceptance-film')
        with window_runtime(app, owner) as (window, warnings):
            click(app, window, window.findChild(QQuickItem, 'filmOptimizationAlgorithmTab'))
            budget = window.findChild(QQuickItem, 'resourceProbeMaxEvaluations')
            seeds = window.findChild(QQuickItem, 'resourceProbeSeeds')
            assert budget is not None and seeds is not None
            budget.forceActiveFocus()
            budget.setProperty('text', '0')
            seeds.forceActiveFocus()  # commit the budget through normal focus loss
            seeds.setProperty('text', '5,9')
            assert QMetaObject.invokeMethod(window, 'startFromButton')
            wait_until(app, lambda: bool(captured))
            wait_until(app, lambda: not estimator.busy)
            assert captured == [(0, (5, 9))]
            assert not warnings, warnings
    finally:
        owner.shutdown(1000)
        shiboken6.delete(owner)


def test_real_bounded_probe_from_ui_reports_actual_effort(runtime, tmp_path, monkeypatch):
    from coat_sim.core import film_resource_estimation as core
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import saved_configuration

    app, source, _window, _warnings = runtime
    service, _library, design = fixture(tmp_path / 'real-probe')
    config = saved_configuration(service, design)
    config['objectives'][0].update(wavelength_min_nm=500., wavelength_max_nm=500.,
                                   wavelength_samples=1, wavelength_step_nm=1.,
                                   actual_wavelength_step_nm=0.)
    design['workflow_settings'] = {'optimization': config}
    source.service.materials = service.materials
    assert source.session.request_replace_design(design)
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    owner = FilmOptimizationQuickController(source)
    assert owner.setSearchMode('resource_assessment')
    before = deepcopy(source.session.snapshot().design)
    estimator = select_film(source)
    assert estimator.setProbeMaxEvaluations('4')
    assert estimator.setProbeSeeds('0')
    results = []
    original = core.estimate_film_resources

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(core, 'estimate_film_resources', capture)
    assert owner.startOptimization()
    wait_until(app, lambda: not estimator.busy, timeout=60000)
    assert len(results) == 1 and results[0].state == 'completed', estimator.status
    work = results[0].work
    assert work['probe_runs'] == 1 and len(work['probe_outcomes']) == 1
    outcome = work['probe_outcomes'][0]
    assert outcome['seed'] == 0 and outcome['requested_max_evaluations'] == 4
    summary = estimator.resultSummary
    assert f"{outcome['evaluations']} / 4" in summary
    assert f"物理光谱计算 {outcome['physical_evaluations']} 次" in summary
    assert '不代表搜索穷尽' in summary and '没有经过校准' in summary
    assert source.session.snapshot().design == before
    # Display the genuine bounded run in the optimizer window at narrow width.
    from PySide6.QtQuick import QQuickItem
    from tests.test_film_optimization_restore_layout import click, window_runtime
    try:
        assert not estimator.stale, estimator.status
        with window_runtime(app, owner) as (window, warnings):
            window.resize(560, 500)
            click(app, window, window.findChild(QQuickItem, 'filmOptimizationAlgorithmTab'))
            result_item = window.findChild(QObject, 'filmResourceEstimateResult')
            assert result_item is not None and result_item.property('text') == summary
            from PySide6.QtQml import QQmlExpression
            scroll = QQmlExpression(QQmlEngine.contextForObject(result_item), result_item, """(function () {
                let ancestor = parent;
                while (ancestor !== null) {
                    if ("contentY" in ancestor)
                        ancestor.contentY = Math.max(0, ancestor.contentHeight - ancestor.height);
                    ancestor = ancestor.parent;
                }
            })()""")
            scroll.evaluate()
            assert not scroll.hasError(), scroll.error().toString()
            QTest.qWait(50)
            image = window.grabWindow()
            if not image.isNull():
                assert image.save(str(tmp_path / 'real-probe-narrow.png'))
            assert not warnings, warnings
    finally:
        owner.shutdown(1000)
        shiboken6.delete(owner)
