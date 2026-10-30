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


def test_real_qml_sidebar_and_no_configuration(runtime):
    app, controller, window, warnings = runtime
    rail = window.findChild(QObject, "filmDesignSidebarResourceEstimate")
    assert rail is not None and QMetaObject.invokeMethod(rail, "click")
    app.processEvents()
    assert window.property("activeSidebarSection") == "resource"
    assert window.findChild(QObject, "filmResourceEstimatePanel").property("visible")
    before = deepcopy(controller.session.snapshot().design)
    # Start via controller to avoid depending on keyboard-focus timing.
    assert controller.runResourceEstimate()
    wait_until(app, lambda: not controller.resourceEstimateController.busy)
    assert "目标" in controller.resourceEstimateController.status
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
    assert "unknown" in estimator.resultSummary
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
    request = module._prepare_request(design, 19, 8, threading.Event())
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


def test_real_qml_start_audits_current_design_without_writing(runtime, tmp_path):
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
    assert QMetaObject.invokeMethod(
        window.findChild(QObject, "filmDesignSidebarResourceEstimate"), "click"
    )
    assert controller.resourceEstimateController.setLayerLimit("8")
    assert QMetaObject.invokeMethod(window.findChild(QObject, "filmResourceEstimateStart"), "click")
    wait_until(app, lambda: controller.resourceEstimateController.busy, timeout=3000)
    wait_until(app, lambda: not controller.resourceEstimateController.busy, timeout=20000)
    estimator = controller.resourceEstimateController
    assert "completed" in estimator.status, estimator.status
    assert "当前方案资源及审计" in estimator.resultSummary
    assert "已独立审计" in estimator.resultSummary
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
    estimator = controller.resourceEstimateController
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
    request = module._prepare_request(design, 19, 128, threading.Event())
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


def test_conditional_recommendation_visible_in_real_qml(runtime, tmp_path):
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
    assert QMetaObject.invokeMethod(
        window.findChild(QObject, "filmDesignSidebarResourceEstimate"), "click"
    )
    assert controller.resourceEstimateController.setLayerLimit("8")
    assert QMetaObject.invokeMethod(window.findChild(QObject, "filmResourceEstimateStart"), "click")
    wait_until(app, lambda: controller.resourceEstimateController.busy)
    wait_until(app, lambda: not controller.resourceEstimateController.busy, timeout=20000)
    estimator = controller.resourceEstimateController
    text = window.findChild(QObject, "filmResourceEstimateResult").property("text")
    assert "completed" in estimator.status, estimator.status
    assert "条件推荐：依赖用户显式未知 k=0 假设" in text
    assert "本审计以显式未知 k=0 为条件" in text
    assert "acceptance-glass/glass-set" in text
    assert "有限采样审计见证；非连续域证书，也不是实测吸收或制造良率证书" in text
    assert "核心材料冻结：开始 1 次；完成 1 次" in text
    assert controller.session.snapshot().design == before
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings


def test_real_qml_failed_freeze_shows_started_not_completed(runtime, tmp_path):
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
    assert QMetaObject.invokeMethod(
        window.findChild(QObject, "filmDesignSidebarResourceEstimate"), "click"
    )
    assert controller.resourceEstimateController.setLayerLimit("8")
    assert QMetaObject.invokeMethod(window.findChild(QObject, "filmResourceEstimateStart"), "click")
    wait_until(app, lambda: controller.resourceEstimateController.busy)
    wait_until(app, lambda: not controller.resourceEstimateController.busy, timeout=20000)
    estimator = controller.resourceEstimateController
    text = window.findChild(QObject, "filmResourceEstimateResult").property("text")
    assert "failed" in estimator.status
    assert "核心材料冻结：开始 1 次；完成 0 次" in text
    assert "unknown（未取得已验证见证）" in text
    assert "条件推荐：依赖" not in text
    assert controller.session.snapshot().design == before
    assert not any("ReferenceError" in w or "TypeError" in w for w in warnings), warnings
