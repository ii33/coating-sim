"""Focused controller and QML contracts for optimization targets/configuration."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
import numpy as np
from PySide6.QtCore import QEventLoop, QObject, QPointF, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QValidator
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow

from coat_sim.ui import film_optimization_quick_controller as module
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_optimization_quick_controller import (
    FilmOptimizationQuickController,
    FilmOptimizationSourceSnapshot,
)
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, optimize_film_design,
)
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_run_contract import _Session, _Source


ROOT = Path(__file__).resolve().parents[1]

def _text_property(owner: QObject, name: str) -> str:
    value = owner.property(name)
    assert isinstance(value, str), (name, value)
    return value


def _rows_property(owner: QObject, name: str) -> list[dict[str, object]]:
    value = owner.property(name)
    assert isinstance(value, list) and all(isinstance(row, dict) for row in value)
    return value



def _snapshot(service, design, *, layers=None):
    design = deepcopy(design)
    rows = layers if layers is not None else design.get("front_layers", [])
    return FilmOptimizationSourceSnapshot(
        service=service,
        design=design,
        design_revision=19,
        path="",
        layers_by_side={
            "front": [{
                "id": row["id"],
                "physicalNmValue": row["thickness"]["value"],
                "name": f"Synthetic {row['id']}",
            } for row in rows],
            "back": [],
        },
    )


def _dispose(controller):
    controller.shutdown(1000)
    controller.deleteLater()


class _WorkflowSession(_Session):
    """Small session stub that records config writes without document I/O."""

    def __init__(self, design, revision, configuration=None):
        super().__init__(design, revision)
        self._configuration = deepcopy(configuration or {})
        self.configuration_writes = []

    def workflow_configuration(self, scope):
        assert scope == "optimization"
        return deepcopy(self._configuration)

    def set_workflow_configuration(self, scope, configuration):
        assert scope == "optimization"
        self._configuration = deepcopy(configuration)
        self.configuration_writes.append(deepcopy(configuration))
        return True

    def replace_document(self, design, revision, configuration):
        self.design = deepcopy(design)
        self.design_revision = revision
        self._configuration = deepcopy(configuration)
        self._subscriber(SimpleNamespace(
            categories=frozenset({"design", "workflow_settings"})))


def test_endpoint_inclusive_sampling_preserves_requested_basis_and_actual_step():
    row = module._objective_default(0)
    assert row["wavelength_min_nm"] == 400
    assert row["wavelength_max_nm"] == 700
    assert row["wavelength_samples"] == 31
    resolved = module._resolve_objective_sampling(row)
    assert resolved["wavelength_samples"] == 31
    assert resolved["actual_wavelength_step_nm"] == pytest.approx(10)

    step_requested = deepcopy(row)
    step_requested.update(wavelength_step_nm=7, sampling_basis="step")
    resolved = module._resolve_objective_sampling(step_requested)
    assert resolved["wavelength_samples"] == 44
    assert resolved["wavelength_step_nm"] == 7
    assert resolved["actual_wavelength_step_nm"] == pytest.approx(300 / 43)

    larger_than_band = deepcopy(row)
    larger_than_band.update(wavelength_step_nm=500, sampling_basis="step")
    resolved = module._resolve_objective_sampling(larger_than_band)
    assert resolved["wavelength_samples"] == 2
    assert resolved["actual_wavelength_step_nm"] == pytest.approx(300)

    single = deepcopy(row)
    single.update(wavelength_min_nm=550, wavelength_max_nm=550)
    resolved = module._resolve_objective_sampling(single)
    assert resolved["wavelength_samples"] == 1
    assert resolved["actual_wavelength_step_nm"] == 0


def test_range_angles_use_ceil_and_endpoint_inclusive_grid():
    row = module._objective_default(0)
    row.update(angle_sampling_mode="range", angle_min_deg=2.0,
               angle_max_deg=50.0, angle_step_deg=7.3)
    resolved = module._resolve_angle_sampling(row)
    angles = FilmOptimizationQuickController._angles_for_row(row)
    assert resolved["angle_sample_count"] == 8
    assert resolved["actual_angle_step_deg"] == pytest.approx(48.0 / 7)
    assert angles == pytest.approx(tuple(np.linspace(2.0, 50.0, 8)))
    assert angles[0] == 2.0
    assert angles[-1] == 50.0


def test_adjacent_float_grid_points_remain_valid_across_power_of_two_boundary():
    immediately_below = math.nextafter(32.0, 0.0)
    two_below = math.nextafter(immediately_below, 0.0)

    for low, count in ((immediately_below, 2), (two_below, 3)):
        wavelength_row = module._objective_default(0)
        wavelength_row.update(wavelength_min_nm=low, wavelength_max_nm=32.0,
                              wavelength_samples=count, sampling_basis="samples")
        resolved_wavelength = module._resolve_objective_sampling(wavelength_row)
        wavelength_grid = np.linspace(low, 32.0, count)
        assert resolved_wavelength["wavelength_samples"] == count
        assert np.all(np.diff(wavelength_grid) > 0)

        angle_row = module._objective_default(0)
        angle_row.update(
            angle_sampling_mode="range", angle_min_deg=low, angle_max_deg=32.0,
            angle_step_deg=(32.0 - low) / (count - 1) * 2.0,
        )
        if count == 3:
            angle_row["angle_step_deg"] = (32.0 - low) / 2.0
        resolved_angle = module._resolve_angle_sampling(angle_row)
        angle_grid = FilmOptimizationQuickController._angles_for_row(angle_row)
        assert resolved_angle["angle_sample_count"] == count
        assert all(right > left for left, right in zip(angle_grid, angle_grid[1:], strict=False))


def test_range_controller_angles_participate_in_core_worst_condition_score(
        tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        row = controller._objectives[0]
        row.update(
            wavelength_min_nm=500.0, wavelength_max_nm=501.0,
            wavelength_samples=2, sampling_basis="samples",
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=30.0, angle_step_deg=30.0,
            mode="value", target_value_percent=40.0,
        )
        controller._max_evaluations = 1
        request = controller._build_request()
        assert request.objectives[0].angles_deg == (0.0, 30.0)

        def angle_dependent_simulation(_design, settings):
            waves = np.linspace(
                settings["wavelength_min_nm"], settings["wavelength_max_nm"],
                settings["samples"],
            )
            reflectance = 0.4 if settings["angle_deg"] == 0.0 else 0.9
            return {"wavelength_nm": waves, "R": np.full(waves.shape, reflectance),
                    "T": np.full(waves.shape, 1.0 - reflectance)}

        monkeypatch.setattr(service, "simulate", angle_dependent_simulation, raising=False)
        multi_angle = optimize_film_design(service, request)
        single_request = replace(
            request,
            objectives=(replace(request.objectives[0], angles_deg=(0.0,)),),
        )
        single_angle = optimize_film_design(service, single_request)

        assert len(multi_angle.per_condition_errors) == 4
        assert {row.angle_deg for row in multi_angle.per_condition_errors} == {0.0, 30.0}
        assert multi_angle.max_normalized_error == pytest.approx(0.5)
        assert single_angle.max_normalized_error == pytest.approx(0.0)
        assert multi_angle.max_normalized_error > single_angle.max_normalized_error
    finally:
        _dispose(controller)


def test_large_solver_budgets_round_trip_through_qvariant_qml_and_configuration(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("controller", controller)
    component = QQmlComponent(engine)
    component.setData(
        b"import QtQml 2.15\nQtObject {\n"
        b" property var iterationsEcho: controller.maxIterations\n"
        b" property var evaluationsEcho: controller.maxEvaluations\n"
        b" property var validationEcho: controller.validationSamplesMultiplier\n"
        b" property var multistartEcho: controller.multistartCount\n"
        b" property var populationEcho: controller.populationSize\n"
        b" property string budgetText: String(controller.maxIterations)\n"
        b" property double budgetNumber: Number(controller.maxIterations)\n"
        b" property string budgetType: typeof controller.maxIterations\n"
        b"}\n",
        QUrl(),
    )
    try:
        root = component.create()
        assert root is not None, "\n".join(error.toString() for error in component.errors())
        sentinel = 2_147_483_649
        for option in ("max_iterations", "max_evaluations",
                       "validation_samples_multiplier", "multistart_count",
                       "population_size"):
            assert controller.setSolverOption(option, str(sentinel))
        app.processEvents()
        assert controller.maxIterations == sentinel
        assert controller.maxEvaluations == sentinel
        assert controller.validationSamplesMultiplier == sentinel
        assert controller.multistartCount == sentinel
        assert controller.populationSize == sentinel
        for property_name in ("iterationsEcho", "evaluationsEcho", "validationEcho",
                              "multistartEcho", "populationEcho"):
            assert root.property(property_name) == sentinel

        assert root.property("budgetText") == str(sentinel)
        assert root.property("budgetNumber") == sentinel
        assert root.property("budgetType") == "number"

        configuration = controller.exportOptimizationConfiguration()
        assert configuration["solver"]["max_iterations"] == sentinel
        persisted_design = deepcopy(design)
        persisted_design.setdefault("workflow_settings", {})["optimization"] = configuration
        configuration_path = tmp_path / "large-budget.cfilm"
        service.save(configuration_path, persisted_design)
        stored_configuration = service.load(configuration_path)["workflow_settings"]["optimization"]
        assert stored_configuration == configuration
        assert controller.loadOptimizationConfiguration(stored_configuration)
        assert controller.maxIterations == sentinel
        assert controller.maxEvaluations == sentinel
        assert controller.validationSamplesMultiplier == sentinel
        assert controller.multistartCount == sentinel
        assert controller.populationSize == sentinel

        assert not controller.setSolverOption("max_iterations", str(
            module._MAX_QML_SAFE_INTEGER + 1))
        assert "Qt/QML 可精确表示" in _text_property(controller, "error")

        # A var echo alone can unwrap a Python object on return while the QML
        # TextField still formats the wrapper. Check the real visible fields.
        from coat_sim.ui.theme_service import ThemeService

        ThemeService.instance().attach_engine(engine)
        ui_component = QQmlComponent(engine)
        ui_component.loadUrl(QUrl.fromLocalFile(str(
            ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml")))
        window = ui_component.createWithInitialProperties({"controller": controller})
        assert isinstance(window, QQuickWindow), ui_component.errors()
        try:
            window.show()
            algorithm_tab = window.findChild(QQuickItem, "filmOptimizationAlgorithmTab")
            assert algorithm_tab is not None
            algorithm_tab.setProperty("checked", True)
            window.contentItem().ensurePolished()
            assert not window.grabWindow().isNull()
            app.processEvents()
            for field_name in ("filmOptimizationMaxIterationsField",
                               "filmOptimizationMaxEvaluationsField",
                               "filmOptimizationValidationMultiplierField"):
                field = window.findChild(QQuickItem, field_name)
                assert field is not None and field.isVisible(), field_name
                text = str(field.property("text"))
                assert "PyObjectWrapper" not in text
                assert "".join(character for character in text if character.isdigit()) == str(sentinel)
                field.forceActiveFocus()
                app.processEvents()
                assert field.property("text") == str(sentinel)
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()

    finally:
        engine.deleteLater()
        _dispose(controller)
        app.processEvents()


def test_growth_only_structure_request_needs_no_candidate_materials(tmp_path):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        controller._search_mode = "structure"
        controller._structure_mode = "growth_only"
        for row in controller._structure_material_options:
            row["selected"] = False
        request = controller._build_structure_request()
        assert request.mode == "growth_only"
        assert request.candidate_materials == ()
    finally:
        _dispose(controller)


def test_angle_range_zero_width_and_old_nonuniform_points_round_trip(tmp_path):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        assert controller.setObjectiveField("angle_sampling_mode", "range")
        assert controller.setObjectiveField("angle_max_deg", 30)
        assert controller.setObjectiveField("angle_step_deg", 15)
        assert controller.setObjectiveField("angle_sampling_mode", "points")
        assert controller._build_request(require_variables=False).objectives[0].angles_deg == (
            0.0, 15.0, 30.0)
        assert controller.setObjectiveField("angle_sampling_mode", "range")
        assert controller.setObjectiveField("angle_max_deg", 17.5)
        assert controller.setObjectiveField("angle_min_deg", 17.5)
        assert controller._build_request(require_variables=False).objectives[0].angles_deg == (17.5,)

        old = controller.exportOptimizationConfiguration()
        legacy = deepcopy(old)
        objective = legacy["objectives"][0]
        objective.pop("angle_sampling_mode")
        objective.pop("angle_min_deg")
        objective.pop("angle_max_deg")
        objective.pop("angle_step_deg")
        objective.pop("actual_angle_step_deg")
        objective.pop("angle_sample_count")
        objective["angles_text"] = "0, 13.5, 47"
        assert controller.loadOptimizationConfiguration(legacy)
        assert controller._objectives[0]["angle_sampling_mode"] == "points"
        assert controller._build_request(require_variables=False).objectives[0].angles_deg == (
            0.0, 13.5, 47.0)
    finally:
        _dispose(controller)


def test_queue_inspection_restores_explicit_multi_angle_plot_choices(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    binding = SimpleNamespace(project_id="project-1", root=tmp_path)
    controller.project_context = SimpleNamespace(binding=binding)
    monkeypatch.setattr(controller, "_project_compute", SimpleNamespace(
        capture=lambda _job_id: object(),
        unregister_thaw_callback=lambda _callback: True,
    ))
    request = FilmOptimizationRequest(
        design_snapshot=design, design_revision=1, variables=(),
        objectives=(FilmOptimizationObjective(
            wavelength_min_nm=500.0, wavelength_max_nm=600.0,
            wavelength_samples=3, angles_deg=(0.0, 13.5, 47.0),
        ),),
    )
    monkeypatch.setattr(module, "_film_result_from_queue", lambda _payload: object())
    monkeypatch.setattr(module, "deserialize_request", lambda _request: request)
    try:
        assert controller.inspectQueueResult(
            "job-1", {"request": {}},
            origin={"project_id": "project-1", "project_root": str(tmp_path)},
        )
        assert [row["value"] for row in _rows_property(controller, "plotAngles")] == [0.0, 13.5, 47.0]
    finally:
        _dispose(controller)


def test_angle_range_crosses_former_361_angle_policy_without_blocking(tmp_path):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        row = deepcopy(controller.exportOptimizationConfiguration())
        objective = row["objectives"][0]
        objective.update(angle_sampling_mode="range", angle_min_deg=0,
                         angle_max_deg=80, angle_step_deg=0.2)
        assert controller.loadOptimizationConfiguration(row)
        assert module._angle_sample_count(controller._objectives[0]) == 401
        assert controller.canStart
        assert len(controller._build_request(require_variables=False).objectives[0].angles_deg) == 401
    finally:
        _dispose(controller)


def test_large_range_plot_options_are_bounded_without_building_request_grid(
        tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        controller.detach_observers()
        old_admission_gate = controller._admission_stopped
        assert old_admission_gate.is_set()
        assert controller._replace_source(_snapshot(service, design))
        assert old_admission_gate.is_set()
        assert not controller._admission_stopped.is_set()
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=1e-4)
        monkeypatch.setattr(
            FilmOptimizationQuickController, "_angles_for_row",
            staticmethod(lambda _row: pytest.fail("plot options expanded the full grid")))
        options = _rows_property(controller, "plotAngles")
        assert len(options) == module._PLOT_ANGLE_OPTION_LIMIT
        assert options[0]["value"] == 0.0
        assert options[-1]["value"] == 80.0
        assert "完整 800001 个角度样本" in _text_property(controller, "plotAnglesSummary")
    finally:
        _dispose(controller)


def test_large_range_to_points_switch_does_not_expand_editable_text(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=0.01)
        original = deepcopy(controller._objectives[0])
        monkeypatch.setattr(
            FilmOptimizationQuickController, "_angles_for_row",
            staticmethod(lambda _row: pytest.fail("mode switch expanded the full grid")))
        assert not controller.setObjectiveField("angle_sampling_mode", "points")
        assert controller._objectives[0] == original
        assert "界面编辑文本限制" in _text_property(controller, "error")
        assert not controller._pending_edit_error
        assert controller.canStart
    finally:
        _dispose(controller)


def test_large_grid_preparation_runs_in_coordinator_and_is_cancellable(
        tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    started = threading.Event()

    class BlockingPreparationTask(module.QRunnable):
        def __init__(self, _rows, _template, _mode, _service, cancel_event, operation_id):
            super().__init__()
            self.cancel_event = cancel_event
            self.operation_id = operation_id
            self.signals = module._OptimizationSignals()

        def run(self):
            started.set()
            self.cancel_event.wait(2)
            self.signals.finished.emit(
                self.operation_id, None, None,
                {"state": "cancelled", "message": "cancelled before submit"})

    try:
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=1e-4)
        monkeypatch.setattr(module, "_AngleGridPreparationTask", BlockingPreparationTask)
        monkeypatch.setattr(
            controller._compute_target, "client_for_submit",
            lambda **_kwargs: pytest.fail("cancelled preparation reached queue submission"))
        assert controller.startOptimization()
        assert started.wait(1)
        assert controller.busy
        assert controller.localWorkRunning
        controller.cancelLocalWork()
        deadline = time.monotonic() + 2
        while controller.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert not controller.busy
        assert "已取消" in _text_property(controller, "error")
        assert controller.queueJobId == ""
    finally:
        _dispose(controller)


@pytest.mark.parametrize("disposal", ["detach", "shutdown"])
def test_project_owned_angle_preparation_is_cancelled_on_disposal(
        tmp_path, monkeypatch, disposal):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    project_compute = SimpleNamespace(
        editing_frozen=False,
        capture=lambda job_id: SimpleNamespace(job_id=job_id),
        accepts=lambda _operation: True,
        metadata=lambda _operation, value: value,
        register_thaw_callback=lambda _callback: None,
        unregister_thaw_callback=lambda _callback: None,
    )
    from coat_sim.core.project_compute import ProjectComputeWorkspace
    monkeypatch.setattr(
        ProjectComputeWorkspace, "for_project",
        classmethod(lambda _cls, _host, _binding: project_compute))
    context = SimpleNamespace(
        service=service, editor=None, host=object(), binding=object())
    controller = FilmOptimizationQuickController(
        _snapshot(service, design), project_context=context)
    started = threading.Event()
    finished = threading.Event()
    captured = {}

    class BlockingPreparationTask(module.QRunnable):
        def __init__(self, _rows, _template, _mode, _service, cancel_event,
                     operation_id):
            super().__init__()
            self.cancel_event = cancel_event
            self.operation_id = operation_id
            self.signals = module._OptimizationSignals()
            captured["cancel_event"] = cancel_event

        def run(self):
            started.set()
            self.cancel_event.wait(2)
            try:
                self.signals.finished.emit(
                    self.operation_id, None, None,
                    {"state": "cancelled", "message": "cancelled on disposal"})
            finally:
                finished.set()

    monkeypatch.setattr(module, "_AngleGridPreparationTask", BlockingPreparationTask)
    monkeypatch.setattr(
        controller._compute_target, "client_for_submit",
        lambda **_kwargs: pytest.fail("disposed project prep reached queue submit"))
    try:
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=1e-4)
        assert controller.startOptimization()
        assert started.wait(1)
        app.processEvents()
        assert controller.localWorkRunning
        assert not captured["cancel_event"].is_set()
        if disposal == "detach":
            controller.detach_observers()
            assert captured["cancel_event"].is_set()
            assert controller._admission_stopped.is_set()
        else:
            assert controller.shutdown(1000)
            assert captured["cancel_event"].is_set()
            assert controller._admission_stopped.is_set()
        assert finished.wait(2)
        app.processEvents()
        assert controller.queueJobId == ""
    finally:
        _dispose(controller)


def test_stale_edit_epoch_discards_prepared_grid_before_queue_submission(
        tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    started = threading.Event()
    release = threading.Event()

    class DelayedPreparationTask(module.QRunnable):
        def __init__(self, _rows, _template, _mode, _service,
                     _cancel_event, operation_id):
            super().__init__()
            self.operation_id = operation_id
            self.signals = module._OptimizationSignals()

        def run(self):
            started.set()
            release.wait(2)
            self.signals.finished.emit(
                self.operation_id, module._PreparedAngleGrid(None, {}), None, None)

    try:
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=1e-4)
        epoch = {"value": 7}
        monkeypatch.setattr(controller, "_source_edit_epoch", lambda: epoch["value"])
        monkeypatch.setattr(module, "_AngleGridPreparationTask", DelayedPreparationTask)
        monkeypatch.setattr(
            controller._compute_target, "client_for_submit",
            lambda **_kwargs: pytest.fail("stale preparation reached queue submission"))
        assert controller.startOptimization()
        assert started.wait(1)
        epoch["value"] = 8
        release.set()
        deadline = time.monotonic() + 2
        while controller.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert not controller.busy
        assert "原设计在准备角度网格期间发生编辑" in _text_property(controller, "error")
        assert controller.queueJobId == ""
    finally:
        release.set()
        _dispose(controller)


def test_prepared_grid_button_flow_freezes_and_submits_off_gui_thread(
        tmp_path, monkeypatch):
    import shiboken6

    app = QGuiApplication.instance() or QGuiApplication([])
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from coat_sim.ui.theme_service import ThemeService

    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    engine.addImportPath(str(ROOT / "coat_sim/ui/qml"))
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    component = QQmlComponent(engine)
    component.loadUrl(QUrl.fromLocalFile(str(
        ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml")))
    window = None
    main_thread = threading.get_ident()
    observed_threads = {}
    submitted = []

    class Store:
        transport = None

        def capabilities(self):
            return {"protocol_version": 1, "executors": ["film_optimization"]}

        def submit(self, spec, *, job_id, metadata):
            observed_threads["submit"] = threading.get_ident()
            submitted.append((spec, job_id, metadata))

        def cancel(self, _job_id):
            return {"cancel_requested": True}

    store = Store()

    class FinishingFilmTask(module.QRunnable):
        def __init__(self, _service, request, _store, task_job_id, cancel_event,
                     operation_id, *, remote_admission=None, project_compute=None):
            super().__init__()
            self.request = request
            self.job_id = task_job_id
            self.cancel_event = cancel_event
            self.operation_id = operation_id
            self.remote_admission = remote_admission
            self.signals = module._OptimizationSignals()
            self.admitted = remote_admission is None

        def run(self):
            self.remote_admission()
            self.admitted = True
            self.signals.jobIdChanged.emit(self.operation_id, self.job_id)
            self.signals.finished.emit(
                self.operation_id, None, [],
                {"state": "cancelled", "message": "test task stopped after admission"})

    def mocked_materializer(row, *args, **kwargs):
        observed_threads["angles"] = threading.get_ident()
        return (float(row["angle_min_deg"]), float(row["angle_max_deg"]))

    def mocked_freeze(_service, request):
        observed_threads["freeze"] = threading.get_ident()
        return request

    def mocked_spec(_service, _kind, parameters):
        observed_threads["spec"] = threading.get_ident()
        return {"request": parameters["request"], "serialized": "mocked"}

    monkeypatch.setattr(module, "_angles_for_row_cancellable", mocked_materializer)
    monkeypatch.setattr(module, "freeze_film_optimization_request", mocked_freeze)
    monkeypatch.setattr(module, "freeze_spec", mocked_spec)
    monkeypatch.setattr(module, "_FilmOptimizationTask", FinishingFilmTask)
    monkeypatch.setattr(
        controller._compute_target, "client_for_submit",
        lambda **_kwargs: (store, {}))
    try:
        controller._compute_target._ready = True
        controller._compute_target._devices = [{
            "id": "cpu:default", "available": True, "backend": "CPU"}]
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=80.0 / 100000)
        assert module._angle_sample_count(controller._objectives[0]) == 100001
        assert component.status() != QQmlComponent.Status.Error, "\n".join(
            error.toString() for error in component.errors())
        window = component.createWithInitialProperties({"controller": controller})
        assert window is not None, "\n".join(
            error.toString() for error in component.errors())
        assert isinstance(window, QQuickWindow)
        window.resize(900, 700)
        window.show()
        window.contentItem().ensurePolished()
        window.grabWindow()
        app.processEvents()
        QTest.qWait(50)
        app.processEvents()
        button = window.findChild(QQuickItem, "filmOptimizationStartButton")
        assert button is not None and button.isEnabled()
        button_center = button.mapToScene(
            QPointF(button.width() / 2, button.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(round(button_center.x()), round(button_center.y())))
        deadline = time.monotonic() + 3
        while controller.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        # The actual QML button commits any focused editor, then callLater starts.
        # Give the queued call a short event-loop turn when it hasn't begun yet.
        deadline = time.monotonic() + 1
        while not observed_threads and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert not controller.busy
        assert observed_threads["angles"] != main_thread
        assert observed_threads["freeze"] != main_thread
        assert observed_threads["spec"] != main_thread
        assert observed_threads["submit"] != main_thread
        assert submitted
        assert submitted[0][1] == controller.queueJobId
        assert submitted[0][0]["request"].objectives[0].angles_deg == (0.0, 80.0)
        assert controller.queueJobId
    finally:
        if isinstance(window, QQuickWindow) and shiboken6.isValid(window):
            window.setProperty("allowClose", True)
            window.hide()
            shiboken6.delete(window)
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(
            wait=True, cancel_futures=True)
        shiboken6.delete(controller)
        shiboken6.delete(component)
        shiboken6.delete(engine)
        app.processEvents()


def test_local_cancel_during_async_submit_retries_after_exact_job_admission(
        tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    submit_started = threading.Event()
    release_submit = threading.Event()
    submit_finished = threading.Event()
    cancel_calls = []

    class Store:
        transport = None

        def capabilities(self):
            return {"protocol_version": 1, "executors": ["film_optimization"]}

        def submit(self, _spec, *, job_id, metadata):
            self.job_id = job_id
            self.metadata = metadata
            submit_started.set()
            assert release_submit.wait(2)
            self.job_created = True
            submit_finished.set()

        def cancel(self, job_id):
            if not getattr(self, "job_created", False):
                raise RuntimeError("queue row does not exist yet")
            cancel_calls.append(job_id)
            if len(cancel_calls) == 1:
                raise RuntimeError("synthetic transient cancellation failure")
            return {"cancel_requested": True}

    store = Store()

    class WaitingFilmTask(module.QRunnable):
        def __init__(self, _service, _request, _store, job_id, cancel_event,
                     operation_id, *, remote_admission=None, project_compute=None):
            super().__init__()
            self.job_id = job_id
            self.cancel_event = cancel_event
            self.operation_id = operation_id
            self.remote_admission = remote_admission
            self.signals = module._OptimizationSignals()
            self.admitted = False

        def run(self):
            warning = self.remote_admission()
            self.admitted = True
            self.signals.jobIdChanged.emit(self.operation_id, self.job_id)
            if warning:
                self.signals.progress.emit(
                    self.operation_id,
                    module.FilmOptimizationProgress(
                        state="running", fraction=0.0, iteration=0, evaluations=0,
                        best_max_normalized_error=None, message=warning))
            self.cancel_event.wait(2)
            state = "cancelled" if self.cancel_event.is_set() else "failed"
            self.signals.finished.emit(
                self.operation_id, None, [],
                {"state": state, "message": "test finished after cancellation"})

    monkeypatch.setattr(module, "_angles_for_row_cancellable",
                        lambda *_args, **_kwargs: (0.0, 30.0))
    monkeypatch.setattr(module, "freeze_film_optimization_request", lambda _service, request: request)
    monkeypatch.setattr(module, "freeze_spec", lambda _service, _kind, params: params)
    monkeypatch.setattr(module, "_FilmOptimizationTask", WaitingFilmTask)
    monkeypatch.setattr(controller._compute_target, "client_for_submit",
                        lambda **_kwargs: (store, {}))
    try:
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=30.0, angle_step_deg=30.0 / 100000)
        assert controller.startOptimization()
        deadline = time.monotonic() + 3
        while not submit_started.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert submit_started.is_set()

        controller.cancelOptimization()
        assert not cancel_calls, "pre-admission cancellation must not hit a missing queue row"

        warning_loop = QEventLoop()
        warning_timeout = QTimer()
        warning_timeout.setSingleShot(True)
        warning_timeout.timeout.connect(warning_loop.quit)
        warning_visible = False

        def finish_when_cancel_warning_is_visible():
            nonlocal warning_visible
            warning_visible = (
                "首次取消请求失败" in _text_property(controller, "progressText")
                and "RuntimeError: synthetic transient cancellation failure"
                in _text_property(controller, "error"))
            if warning_visible:
                warning_loop.quit()

        # cancel_calls is written before the worker catches the exception and
        # emits its queued progress. Wait for the GUI-visible changed state,
        # with the timer serving only as a watchdog for missing delivery.
        controller.changed.connect(finish_when_cancel_warning_is_visible)
        try:
            release_submit.set()
            assert submit_finished.wait(1)
            finish_when_cancel_warning_is_visible()
            if not warning_visible:
                warning_timeout.start(1000)
                warning_loop.exec()
        finally:
            warning_timeout.stop()
            controller.changed.disconnect(finish_when_cancel_warning_is_visible)

        assert cancel_calls == [store.job_id]
        assert "首次取消请求失败" in _text_property(controller, "progressText")
        assert "RuntimeError: synthetic transient cancellation failure" in _text_property(controller, "error")

        # The automatic post-submit attempt failed transiently; an explicit
        # retry against the same accepted ID must still be possible.
        controller.cancelOptimization()
        deadline = time.monotonic() + 2
        while controller.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert cancel_calls == [store.job_id, store.job_id]
        assert not controller.busy
        assert controller._result_terminal_state == "已取消"
        assert not _text_property(controller, "error")
        assert controller.shutdown(1000), "the cancelled worker must finish before teardown"
    finally:
        release_submit.set()
        _dispose(controller)


def test_cancel_after_worker_result_emit_but_before_gui_delivery_never_submits(
        tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    started = threading.Event()
    release = threading.Event()
    emitted = threading.Event()

    class LateSuccessPreparationTask(module.QRunnable):
        def __init__(self, _rows, _template, _mode, _service,
                     cancel_event, operation_id):
            super().__init__()
            self.cancel_event = cancel_event
            self.operation_id = operation_id
            self.signals = module._OptimizationSignals()

        def run(self):
            started.set()
            release.wait(2)
            self.signals.finished.emit(
                self.operation_id, module._PreparedAngleGrid(None, {}), None, None)
            emitted.set()

    try:
        controller._objectives[0].update(
            angle_sampling_mode="range", angle_min_deg=0.0,
            angle_max_deg=80.0, angle_step_deg=80.0 / 100000)
        monkeypatch.setattr(module, "_AngleGridPreparationTask", LateSuccessPreparationTask)
        monkeypatch.setattr(
            controller._compute_target, "client_for_submit",
            lambda **_kwargs: pytest.fail("cancelled result delivery reached queue setup"))
        assert controller.startOptimization()
        assert started.wait(1)
        release.set()
        assert emitted.wait(1)
        # The worker has emitted success, but Qt has not delivered it to the
        # GUI coordinator slot yet. A last-moment user cancel must still win.
        assert controller.busy
        controller.cancelOptimization()
        deadline = time.monotonic() + 2
        while controller.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert not controller.busy
        assert "已取消" in _text_property(controller, "error")
    finally:
        release.set()
        _dispose(controller)


@pytest.mark.parametrize("field,value", [
    ("wavelength_samples", 1),
    ("wavelength_samples", 31.5),
    ("wavelength_step_nm", 0),
    ("wavelength_step_nm", -1),
    ("wavelength_step_nm", float("nan")),
    ("wavelength_step_nm", float("inf")),
    ("wavelength_max_nm", 399),
])
def test_invalid_sampling_edit_preserves_last_legal_row_and_blocks_start(
        tmp_path, field, value):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        original = deepcopy(controller._objectives[0])
        assert original["wavelength_samples"] == 31
        assert not controller.setObjectiveField(field, value)
        assert controller._objectives[0] == original
        assert controller._pending_edit_error
        assert not controller.canStart
    finally:
        _dispose(controller)


def test_controller_switches_sampling_basis_recomputes_band_and_supports_single_wave(
        tmp_path):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        assert controller.setObjectiveField("wavelength_step_nm", 7)
        row = controller._objectives[0]
        assert row["sampling_basis"] == "step"
        assert row["wavelength_samples"] == 44
        assert row["actual_wavelength_step_nm"] == pytest.approx(300 / 43)

        assert controller.setObjectiveField("wavelength_max_nm", 800)
        row = controller._objectives[0]
        assert row["sampling_basis"] == "step"
        assert row["wavelength_samples"] == 59
        assert row["actual_wavelength_step_nm"] == pytest.approx(400 / 58)

        assert controller.setObjectiveField("wavelength_min_nm", 800)
        row = controller._objectives[0]
        assert row["wavelength_samples"] == 1
        assert row["actual_wavelength_step_nm"] == 0
        assert "单波长" in _text_property(controller, "samplingSummary")

        assert controller.setObjectiveField("wavelength_min_nm", 700)
        row = controller._objectives[0]
        assert row["wavelength_samples"] >= 2
        assert row["sampling_basis"] == "step"
    finally:
        _dispose(controller)


@pytest.mark.parametrize("count", [44, 60])
def test_new_source_defaults_to_all_available_layers_not_first_eight(tmp_path, count):
    service, _library, design = fixture(tmp_path)
    template = deepcopy(design["front_layers"][0])
    layers = []
    for index in range(count):
        row = deepcopy(template)
        row["id"] = f"stable-layer-{index:02d}"
        layers.append(row)
    design["front_layers"] = layers
    controller = FilmOptimizationQuickController(_snapshot(service, design, layers=layers))
    try:
        config = controller.exportOptimizationConfiguration()
        assert len(config["variables"]) == count
        assert sum(row["enabled"] for row in config["variables"]) == count
        assert f"膜层 {count} 层，选中 {count} 个厚度变量" in _text_property(controller, "resourceRiskSummary")
    finally:
        _dispose(controller)


def test_duplicate_objective_has_independent_identity_and_editable_copy(tmp_path):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        for field, value in (("angles_text", "0, 30, 45"),
                             ("polarization", "p"), ("weight", 2.75),
                             ("lower_bound_percent", 1.5),
                             ("upper_bound_percent", 9.0)):
            assert controller.setObjectiveField(field, value)
        original = deepcopy(controller._objectives[0])
        assert controller.duplicateSelectedObjective()
        copied = controller._objectives[1]
        assert copied["id"] != original["id"]
        assert copied["name"] == original["name"] + " 副本"
        for field in ("angles_text", "polarization", "weight",
                      "lower_bound_percent", "upper_bound_percent"):
            assert copied[field] == original[field]
        assert controller.setObjectiveField("angles_text", "12")
        assert controller.setObjectiveField("weight", 8)
        assert controller._objectives[0] == original
        assert controller._objectives[1]["angles_text"] == "12"
        assert controller._objectives[1]["weight"] == 8
    finally:
        _dispose(controller)


def test_configuration_roundtrip_persists_user_edits_without_load_echo(tmp_path):
    service, _library, design = fixture(tmp_path)
    session = _WorkflowSession(design, 19)
    source = _Source(service, session)
    first = FilmOptimizationQuickController(source)
    try:
        layer_id = first._variables[0]["layer_id"]
        first.setVariableEnabled("front", layer_id, False)
        assert first.setVariableBound("front", layer_id, "lower_nm", 60)
        assert first.setVariableBound("front", layer_id, "upper_nm", 240)
        assert first.setSolver("simulated_annealing")
        assert first.setSolverOption("max_evaluations", 321)
        assert first.setObjectiveField("wavelength_step_nm", 7)
        saved = first.exportOptimizationConfiguration()
        assert session.workflow_configuration("optimization") == saved
        assert session.configuration_writes
        write_count = len(session.configuration_writes)
    finally:
        _dispose(first)

    # A fresh controller consumes the session value but must not write it back.
    second = FilmOptimizationQuickController(_Source(service, session))
    try:
        session.configuration_writes.clear()
        assert second.exportOptimizationConfiguration() == saved
        assert second._solver_id == "simulated_annealing"
        objective = second._objectives[0]
        assert objective["sampling_basis"] == "step"
        assert objective["wavelength_samples"] == 44
        assert objective["actual_wavelength_step_nm"] == pytest.approx(300 / 43)
        variable = second._variables[0]
        assert variable["layer_id"] == layer_id
        assert variable["enabled"] is False
        assert variable["lower_nm"] == 60
        assert variable["upper_nm"] == 240
        assert session.configuration_writes == []
        assert write_count >= 5
    finally:
        _dispose(second)


def test_invalid_layer_id_rejects_configuration_atomically(tmp_path):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        before = controller.exportOptimizationConfiguration()
        invalid = deepcopy(before)
        invalid["objectives"][0]["name"] = "must not partially load"
        invalid["variables"][0]["layer_id"] = "no-such-stable-layer"
        assert not controller.loadOptimizationConfiguration(invalid)
        assert controller.exportOptimizationConfiguration() == before
        assert "膜层不存在" in _text_property(controller, "error")
    finally:
        _dispose(controller)


@pytest.mark.parametrize("objective_count,validation_multiplier", [
    (3, 2),
    (1, 20),
])
def test_aggregate_training_and_holdout_old_thresholds_do_not_block_start(
        tmp_path, objective_count, validation_multiplier):
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        configuration = controller.exportOptimizationConfiguration()
        base = deepcopy(configuration["objectives"][0])
        configuration["objectives"] = []
        for index in range(objective_count):
            objective = deepcopy(base)
            objective.update(
                id=f"resource-target-{index}", wavelength_samples=10001,
                sampling_basis="samples")
            configuration["objectives"].append(objective)
        configuration["solver"]["validation_samples_multiplier"] = validation_multiplier
        assert controller.loadOptimizationConfiguration(configuration)
        # Cross the former 20,000 training / 200,000 holdout policies without
        # running an optical simulation. Explicit positive budgets still apply.
        request = controller._build_request(require_variables=False)
        training_conditions = sum(row.wavelength_samples * len(row.angles_deg)
                                  for row in request.objectives)
        validation_conditions = sum(
            ((row.wavelength_samples - 1) * request.validation_samples_multiplier + 1)
            * len(row.angles_deg) for row in request.objectives)
        assert (training_conditions > 20000) == (objective_count == 3)
        assert (validation_conditions > 200000) == (validation_multiplier == 20)
        assert request.max_evaluations > 0
        assert controller.canStart
    finally:
        _dispose(controller)


def _source_pair(service, design):
    first_session = _WorkflowSession(design, 19)
    other_design = deepcopy(design)
    other_design["id"] = "different-design-id"
    second_session = _WorkflowSession(other_design, 20)
    return _Source(service, first_session), _Source(service, second_session)


def test_reuse_bridge_same_source_and_busy_different_source(tmp_path):
    service, _library, design = fixture(tmp_path)
    first_source, second_source = _source_pair(service, design)
    controller = FilmOptimizationQuickController(first_source)
    try:
        assert controller.reuseWindowOpen("", first_source)
        assert controller.session is first_source.session
        controller._busy = True
        assert not controller.reuseWindowOpen("", second_source)
        assert controller.session is first_source.session
    finally:
        controller._busy = False
        _dispose(controller)


def test_busy_document_replacement_refreshes_new_saved_configuration_before_shortcut(
        tmp_path):
    service, _library, design = fixture(tmp_path)
    session = _WorkflowSession(design, 19)
    source = _Source(service, session)
    controller = FilmOptimizationQuickController(source)
    starts = []
    controller.startOptimization = lambda: starts.append((
        controller._design["id"], controller._objectives[0]["name"],
        deepcopy(session.workflow_configuration("optimization")))) or True
    try:
        assert controller.setObjectiveField("name", "旧文档目标")
        old_config = controller.exportOptimizationConfiguration()
        new_design = deepcopy(design)
        new_design["id"] = "replaced-document-id"
        new_design["front_layers"][0]["id"] = "replacement-stable-layer"
        new_config = deepcopy(old_config)
        new_config["objectives"][0]["name"] = "新文档保存的目标"
        new_config["objectives"][0]["wavelength_step_nm"] = 7
        new_config["objectives"][0]["sampling_basis"] = "step"
        new_config["objectives"][0]["wavelength_samples"] = 44
        new_config["objectives"][0]["actual_wavelength_step_nm"] = 300 / 43
        new_config["variables"][0]["layer_id"] = "replacement-stable-layer"

        controller._busy = True
        session.replace_document(new_design, 20, new_config)
        assert controller._source_document_replaced
        assert controller._source_stale
        assert controller._objectives[0]["name"] == "旧文档目标"
        # Simulate the worker's terminal state without running an optimization.
        controller._busy = False
        session.configuration_writes.clear()
        assert controller.startOptimizationFromShortcut()

        assert starts and starts[0][0] == "replaced-document-id"
        assert starts[0][1] == "新文档保存的目标"
        assert starts[0][2] == new_config
        assert controller._variables[0]["layer_id"] == "replacement-stable-layer"
        assert session.workflow_configuration("optimization") == new_config
        assert session.configuration_writes == []
        assert not controller._source_document_replaced
    finally:
        controller._busy = False
        _dispose(controller)


@pytest.mark.parametrize("discard_candidate", [False, True])
def test_unapplied_candidate_confirmation_defers_shortcut_until_source_decision(
        tmp_path, discard_candidate):
    service, _library, design = fixture(tmp_path)
    first_source, second_source = _source_pair(service, design)
    controller = FilmOptimizationQuickController(first_source)
    requested = []
    starts = []
    controller.sourceReuseConfirmationRequested.connect(lambda: requested.append(True))
    # Isolate the source-routing gate from a real optimization attempt.
    controller.startOptimization = lambda: starts.append(controller.session) or True
    try:
        controller._result = SimpleNamespace()
        assert controller.reuseWindowOpen("", second_source)
        assert requested == [True]
        assert controller.session is first_source.session
        assert controller.startOptimizationFromShortcut()
        assert controller._pending_reuse_start
        assert starts == []  # Confirmation is still pending; old source cannot run.

        controller.resolveSourceReuse(discard_candidate)
        if discard_candidate:
            assert controller.session is second_source.session
            assert starts == [second_source.session]
        else:
            assert controller.session is first_source.session
            assert starts == []
        assert controller._pending_reuse_source is None
        assert not controller._pending_reuse_start
    finally:
        _dispose(controller)


def test_optimization_window_loads_sampling_fields_and_duplicate_button(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    from coat_sim.ui.theme_service import ThemeService

    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    engine.addImportPath(str(ROOT / "coat_sim/ui/qml"))
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    component = QQmlComponent(engine)
    component.loadUrl(QUrl.fromLocalFile(str(
        ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml")))
    try:
        assert component.status() != QQmlComponent.Status.Error, "\n".join(
            error.toString() for error in component.errors())
        window = component.createWithInitialProperties({"controller": controller})
        assert window is not None, "\n".join(
            error.toString() for error in component.errors())
        assert isinstance(window, QObject)
        for object_name in (
                "filmOptimizationSamplesField",
                "filmOptimizationWavelengthStepField",
                "filmOptimizationSamplingSummary",
                "filmOptimizationAngleSamplingModeBox",
                "filmOptimizationAngleMinField",
                "filmOptimizationAngleMaxField",
                "filmOptimizationAngleStepField",
                "filmOptimizationDuplicateObjectiveButton"):
            assert window.findChild(QQuickItem, object_name) is not None
        window.deleteLater()
        app.processEvents()
    finally:
        _dispose(controller)
        engine.deleteLater()
        app.processEvents()


def test_optimization_controls_and_tabs_fit_shared_default_minimum_window(tmp_path):
    import shiboken6

    app = QGuiApplication.instance() or QGuiApplication([])
    from coat_sim.ui.theme_service import ThemeService

    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    engine.addImportPath(str(ROOT / "coat_sim/ui/qml"))
    service, _library, design = fixture(tmp_path)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    component = QQmlComponent(engine)
    component.loadUrl(QUrl.fromLocalFile(str(
        ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml")))
    window = None
    try:
        window = component.createWithInitialProperties({"controller": controller})
        assert window is not None, "\n".join(error.toString() for error in component.errors())
        assert isinstance(window, QQuickWindow)
        assert window.minimumWidth() == 720
        assert window.minimumHeight() == 500
        window.resize(window.minimumWidth(), window.minimumHeight())
        window.show()
        window.contentItem().ensurePolished()
        window.grabWindow()
        app.processEvents()
        from PySide6.QtTest import QTest
        QTest.qWait(50)
        app.processEvents()
        toolbar = window.findChild(QQuickItem, "filmOptimizationToolbar")
        assert toolbar is not None
        toolbar_origin = toolbar.mapToItem(window.contentItem(), QPointF(0, 0))
        toolbar_end = toolbar.mapToItem(
            window.contentItem(), QPointF(toolbar.width(), toolbar.height()))
        assert toolbar_origin.x() >= 0 and toolbar_origin.y() >= 0
        assert toolbar_end.x() <= window.width() + 1
        assert toolbar_end.y() <= window.height() + 1
        assert window.findChild(QQuickItem, "filmOptimizationFooterPanel") is None
        for object_name in (
                "filmOptimizationComputeTarget", "filmOptimizationProgress",
                "filmOptimizationStartButton", "filmOptimizationCancelButton",
                "filmOptimizationApplyButton", "filmOptimizationSaveButton"):
            item = window.findChild(QQuickItem, object_name)
            assert item is not None, object_name
            origin = item.mapToItem(toolbar, QPointF(0, 0))
            end = item.mapToItem(toolbar, QPointF(item.width(), item.height()))
            if origin.x() < 0:
                # A narrow shared toolbar keeps the original action alive and
                # exposes the same enabled/text state in its overflow menu.
                bar = window.findChild(QQuickItem, "filmOptimizationOperationBar")
                assert bar is not None
                overflow = bar.property("overflowItems").toVariant()
                assert item in overflow, object_name
            else:
                assert origin.y() >= 0, object_name
                assert end.x() <= toolbar.width() + 1, (object_name, end.x(), toolbar.width())
                assert end.y() <= toolbar.height() + 1, object_name
        assert window.findChild(QQuickItem, "filmOptimizationConstraintsTab") is not None
        constraint_tab = window.findChild(QQuickItem, "filmOptimizationConstraintsTab")
        assert constraint_tab is not None
        tab_origin = constraint_tab.mapToItem(window.contentItem(), QPointF(0, 0))
        tab_end = constraint_tab.mapToItem(
            window.contentItem(), QPointF(constraint_tab.width(), constraint_tab.height()))
        assert tab_origin.x() >= 0 and tab_origin.y() >= 0
        assert tab_end.x() <= window.width() + 1
        assert tab_end.y() <= window.height() + 1

        angle_step_field = window.findChild(QQuickItem, "filmOptimizationAngleStepField")
        assert angle_step_field is not None
        validator = angle_step_field.property("validator")
        assert isinstance(validator, QValidator)
        validation = validator.validate("1e-15", 5)
        assert isinstance(validation, tuple) and len(validation) == 3
        assert validation[0] == QValidator.State.Acceptable
        assert controller.setObjectiveField("angle_min_deg", 0.0)
        assert controller.setObjectiveField("angle_max_deg", 1e-8)
        assert controller.setObjectiveField("angle_step_deg", "1e-15")
        assert controller._objectives[0]["angle_sample_count"] == 10_000_001
    finally:
        if window is not None and shiboken6.isValid(window):
            window.setProperty("allowClose", True)
            cast(QQuickWindow, window).hide()
            shiboken6.delete(window)
        controller.shutdown(1000)
        cast(ComputeTarget, controller.computeTarget)._executor.shutdown(
            wait=True, cancel_futures=True)
        shiboken6.delete(controller)
        shiboken6.delete(component)
        shiboken6.delete(engine)
        app.processEvents()


def test_full_optimizer_configuration_survives_real_cfilm_save_open(tmp_path):
    """Exercise the actual session/file boundary, beyond the no-echo stub."""
    from PySide6.QtCore import QSettings
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController

    app = QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    sources = [FilmDesignQuickController(
        root=tmp_path, service=service,
        geometry_settings=QSettings(str(tmp_path / f"source-{index}.ini"), QSettings.IniFormat))
        for index in range(2)]
    first = second = None
    try:
        assert sources[0].session.request_replace_design(design)
        app.processEvents()
        first = FilmOptimizationQuickController(sources[0])
        revision = sources[0].session.snapshot().design_revision
        assert first.setObjectiveField("wavelength_step_nm", 7)
        assert first.setObjectiveField("angles_text", "0, 30, 45")
        assert first.setObjectiveField("polarization", "p")
        assert first.setObjectiveField("weight", 2.5)
        assert first.duplicateSelectedObjective()
        assert first.setObjectiveField("metric", "T")
        assert first.setSolver("simulated_annealing")
        assert first.setSolverOption("max_evaluations", 123)
        assert first.setSolverOption("seed", 4294967295)
        assert first.setStructureOption("max_total_layers", 129)
        assert first.setStructureOption("growth_steps_nm", "0.5, 3, 12")
        assert first.setStructureOption("growth_phase_fractions", "0.25, 1, 2")
        layer_id = first.variables[0]["layer_id"]
        first.setVariableEnabled("front", layer_id, False)
        assert first.setVariableBound("front", layer_id, "lower_nm", 60)
        assert first.setVariableBound("front", layer_id, "upper_nm", 240)
        saved = first.exportOptimizationConfiguration()
        assert sources[0].session.snapshot().design_revision == revision
        assert sources[0].session.snapshot().dirty
        path = tmp_path / "complete-optimizer.cfilm"
        assert sources[0].session.save_file(str(path))
        assert service.load(path)["workflow_settings"]["optimization"] == saved

        assert sources[1].session.request_open_file(str(path))
        app.processEvents()
        second = FilmOptimizationQuickController(sources[1])
        assert second.exportOptimizationConfiguration() == saved
        assert not sources[1].session.snapshot().dirty
        assert len(second.objectives) == 2
        assert second.objectives[0]["id"] != second.objectives[1]["id"]
        assert second._build_request(require_variables=False).design_snapshot[
            "workflow_settings"]["optimization"] == saved
    finally:
        for optimizer in (first, second):
            if optimizer is not None:
                _dispose(optimizer)
        for source in sources:
            source.markSourceClosed()
            source.deleteLater()
        app.processEvents()
