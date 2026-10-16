"""Real Qt project ownership and asynchronous compute boundary regressions."""
from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_compute import ProjectComputeWorkspace
from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from coat_sim.ui.project_compute_views import create_project_compute_view, dispose_project_compute_controller
from coat_sim.ui.project_film_host import ProjectFilmDesktop, ProjectFilmNavigation
from coat_sim.ui.theme_service import ThemeService
from tests.test_film_optimization_run_contract import _completed_result
from tests.test_project_film_controller_adapter import MemorySettings
from tests.test_project_film_service import bound_design, material

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def events():
    QCoreApplication.processEvents()
    QTest.qWait(10)
    QCoreApplication.processEvents()


@pytest.fixture
def owners(tmp_path):
    host = ProjectSessionHost(lambda root: ProjectStore.open(root / MANIFEST_FILENAME))
    result = []
    for name, n in (("First", 2.0), ("Second", 3.0)):
        store = ProjectStore.create(tmp_path / name, name)
        workspace = ProjectFilmWorkspace.for_project(host, host.open_project(store.root).binding)
        saved = workspace.service.create_film(bound_design(), materials={"shared": material(n=n)})
        assert workspace.editor.request_load_saved_design(saved.document_id)
        source = FilmDesignQuickController(service=workspace.service, edit_session=workspace.editor,
                                          project_context=workspace, geometry_settings=MemorySettings())
        result.append((workspace, source, saved))
    yield host, result
    for _workspace, source, _saved in result:
        source.markSourceClosed()
        source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def optimizer(workspace, source):
    return FilmOptimizationQuickController(source, project_context=workspace)


def test_optimizer_settings_and_candidate_writeback_stay_in_own_film(owners):
    _host, rows = owners
    left, right = [optimizer(workspace, source) for workspace, source, _saved in rows]
    try:
        right_configuration = rows[1][0].editor.workflow_configuration("optimization")
        assert left.setSolverOption("max_iterations", 17)
        assert rows[0][0].editor.workflow_configuration("optimization")["solver"]["max_iterations"] == 17
        assert rows[1][0].editor.workflow_configuration("optimization") == right_configuration
        workspace, source, saved = rows[0]
        original = source.session.snapshot()
        design = deepcopy(original.design)
        design["name"] = "First optimized"
        result = _completed_result(SimpleNamespace(design_revision=original.design_revision,
                                                  design_snapshot=design))
        left._project_operation = left._project_compute.capture("optimization-test")
        left._operation_serial = 1
        left._operation_edit_epoch = source.pendingEditEpoch
        left._on_finished(1, result, [], None)
        assert source.currentDesign()["name"] == "First optimized"
        assert rows[1][1].currentDesign()["name"] == "Bound project film"
        assert workspace.editor.save_current()
        assert workspace.service.load_document(saved.document_id).design["name"] == "First optimized"
        assert left.saveCandidateToProject()
        assert len(workspace.service.list_saved_designs()) == 2
        assert len(rows[1][0].service.list_saved_designs()) == 1
        assert not left._replace_source(rows[1][1])
    finally:
        for controller in (left, right):
            controller.shutdown()
            controller.deleteLater()


@pytest.mark.parametrize("invalidate", ["dirty", "reopen"])
def test_late_optimizer_completion_never_overwrites_newer_owner(owners, invalidate):
    host, rows = owners
    workspace, source, _saved = rows[0]
    controller = optimizer(workspace, source)
    original = source.session.snapshot()
    controller._project_operation = controller._project_compute.capture("late-optimizer")
    controller._operation_serial = 1
    controller._operation_edit_epoch = source.pendingEditEpoch
    design = deepcopy(original.design)
    design["name"] = "Late candidate"
    result = _completed_result(SimpleNamespace(design_revision=original.design_revision,
                                              design_snapshot=design))
    try:
        if invalidate == "dirty":
            assert source.setDesignValue("name", "Keep my draft")
        else:
            assert host.close_project(workspace.binding, prepare_close=lambda *_args: True)
            reopened = host.open_project(workspace.binding.root)
            assert reopened.binding.generation != workspace.binding.generation
        controller._on_finished(1, result, [], None)
        assert source.currentDesign()["name"] != "Late candidate"
        assert rows[1][1].currentDesign()["name"] == "Bound project film"
    finally:
        controller.shutdown()
        controller.deleteLater()


def test_project_spectrum_uses_frozen_materials_and_saves_local_result(owners):
    _host, rows = owners
    workspace, source, _saved = rows[0]
    spectrum = FilmSimulationQuickController(service=workspace.service, source_controller=source,
                                             project_context=workspace)
    try:
        spectrum.startCalculation()
        for _ in range(200):
            events()
            if not spectrum.busy:
                break
        assert not spectrum.busy and spectrum.hasResult, spectrum.validationError
        paths = list((workspace.service.root / "data/results/film_simulation").glob("*.json"))
        assert len(paths) == 1
        saved = json.loads(paths[0].read_text(encoding="utf-8"))
        assert saved["project_id"] == workspace.binding.project_id
        assert saved["project_generation"] == workspace.binding.generation
        assert saved["result"]["R"] == pytest.approx([1 / 9] * 301)
        target = workspace.service.root / "data/results/spectrum.csv"
        spectrum.exportCsvToPath(str(target))
        assert target.is_file(), spectrum.validationError
        other_target = rows[1][0].service.root / "spectrum.csv"
        spectrum.exportCsvToPath(str(other_target))
        assert not other_target.exists()
    finally:
        spectrum.close()
        spectrum.deleteLater()


def test_compute_navigation_uses_real_project_information_signal_and_owner_filter(owners):
    host, rows = owners
    workspace, source, _saved = rows[0]
    desktop = ProjectFilmDesktop(host, parent=source)
    navigation = ProjectFilmNavigation(desktop, workspace, parent=source)
    changed = []
    navigation.projectNameChanged.connect(lambda: changed.append(navigation.projectName))
    store = cast(ProjectStore, host.store_for(workspace.binding))
    store.update_information({"name": "Renamed First"}, expected_revision=store.manifest["revision"])
    desktop.projectInformationChanged.emit(rows[1][0].binding.project_id)
    assert changed == []
    desktop.projectInformationChanged.emit(workspace.binding.project_id)
    assert changed == ["Renamed First"]
    assert navigation.projectName == "Renamed First"
    assert rows[1][0].name == "Second"


@pytest.mark.parametrize("module_key", ["main", "film_optimization", "ray_trace", "film_appearance", "compute_queue"])
def test_project_compute_factory_loads_actual_qml_and_navigation(owners, module_key):
    host, rows = owners
    workspace, source, _saved = rows[0]
    reservation = host.reserve_module(workspace.binding.project_id, "film_design")
    host.attach_window(reservation.route, SimpleNamespace(_project_film_controller=source))
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine)
    desktop = ProjectFilmDesktop(host, parent=source)
    navigation = ProjectFilmNavigation(desktop, workspace, parent=source)
    window = controller = None
    try:
        window, controller, _context, _component = create_project_compute_view(
            workspace, module_key, engine, navigation=navigation)
        events()
        assert window is not None
        assert window.property("projectNavigation") is navigation
        assert window.findChild(QQuickItem, "projectModuleSelector") is not None
        assert controller.project_context is workspace
        expected_suffix = " — " + navigation.projectName
        assert window.property("title").endswith(expected_suffix)
        assert window.property("caption").endswith(expected_suffix)
        caption = window.findChild(QQuickItem, "windowChromeCaption")
        assert caption is not None and caption.property("text") == window.property("caption")
        if module_key == "main":
            assert window.findChild(QQuickItem, "projectDepositionSaveConfiguration") is not None
            assert controller.projectMode
            toolbar = window.findChild(QQuickItem, "mainToolbar")
            selector = window.findChild(QQuickItem, "projectModuleSelector")
            configuration = window.findChild(QQuickItem, "projectManagedDocumentPicker")
            assert toolbar is not None and selector is not None and configuration is not None
            assert selector.parentItem().parentItem() is toolbar
            assert configuration.parentItem().parentItem() is toolbar
            window.show()
            for width in (1280, 640):
                window.setProperty("width", width)
                events()
                for item in toolbar.childItems():
                    if item.isVisible():
                        assert item.x() >= 0 and item.y() >= 0
                        assert item.x() + item.width() <= toolbar.width() + 1
                        assert item.y() + item.height() <= toolbar.height() + 1
        if module_key == "ray_trace":
            assert controller.workspaceMode == "geometric"
        if module_key == "film_appearance":
            assert controller.workspaceMode == "appearance"
            assert controller.appearanceController is None
    finally:
        if controller is not None:
            dispose_project_compute_controller(controller)
        if window is not None:
            window.setProperty("allowClose", True)
            window.deleteLater()
        if controller is not None:
            controller.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_project_queue_result_title_tracks_rename_without_navigation_behavior(owners):
    from coat_sim.ui.compute_queue import ComputeQueueView, QueueResultViewer

    host, rows = owners
    workspace, _source, _saved = rows[0]
    desktop = ProjectFilmDesktop(host)
    navigation = ProjectFilmNavigation(desktop, workspace)
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine)
    queue = ComputeQueueView(engine, workspace.service.root, project_context=workspace,
                             navigation=navigation)
    viewer = None
    try:
        viewer = QueueResultViewer(queue, workspace.service.root, project_context=workspace)
        queue._result_viewer = viewer
        events()
        window = viewer.window
        assert window.property("projectNavigation") is None
        assert window.property("title") == "计算队列 · 已完成结果 — First"
        assert window.property("caption") == "计算队列 · 已完成结果 — First"
        selector = window.findChild(QQuickItem, "projectModuleSelector")
        assert selector is not None and not selector.property("visible")

        store = host.store_for(workspace.binding)
        store.update_information({"name": "Renamed First"},
                                 expected_revision=store.manifest["revision"])
        desktop.projectInformationChanged.emit(workspace.binding.project_id)
        events()
        assert window.property("title") == "计算队列 · 已完成结果 — Renamed First"
        assert window.property("caption") == "计算队列 · 已完成结果 — Renamed First"
    finally:
        queue.shutdown()
        engine.deleteLater()
        desktop.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_project_queue_filters_origin_and_rejects_foreign_cancel_results(owners):
    from coat_sim.ui.compute_queue import ComputeQueueView
    host, rows = owners
    workspace = rows[0][0]
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine)
    queue = ComputeQueueView(engine, workspace.service.root, project_context=workspace)
    own = {"job_id": "own", "state": "queued", "origin": {
        "project_id": workspace.binding.project_id, "project_root": str(workspace.binding.root)}}
    other = {"job_id": "foreign", "state": "queued", "origin": {
        "project_id": rows[1][0].binding.project_id, "project_root": str(rows[1][0].binding.root)}}
    unbound = {"job_id": "legacy", "state": "queued"}
    queue.store = cast(Any, SimpleNamespace(
        root=workspace.service.root,
        queue_jobs_page=lambda *_args, **_kwargs: {"jobs": [own, other, unbound]},
        snapshot=lambda **_kwargs: {"jobs": [own, other, unbound]},
        read_job=lambda identity: {"own": own, "foreign": other}[identity]))
    try:
        page = queue._read_page("recent", None)
        assert [row["job_id"] for row in page["jobs"]] == ["own"]
        assert [row["job_id"] for row in page["refresh_jobs"]] == ["own"]
        assert queue._require_owned_job("own") == own
        with pytest.raises(ValueError, match="不属于"):
            queue._require_owned_job("foreign")
        assert host.close_project(workspace.binding, prepare_close=lambda *_args: True)
        with pytest.raises(ValueError, match="关闭"):
            queue._require_owned_job("own")
    finally:
        queue.shutdown()
        queue.window.deleteLater()
        queue.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_project_raytrace_copies_external_asset_and_routes_registered_appearance(owners, tmp_path):
    from coat_sim.ui.raytrace import RayTraceWindowController
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    _host, rows = owners
    workspace = rows[0][0]
    external = tmp_path / "external-assets"
    external.mkdir()
    asset = _write_synthetic_v7_asset(external)
    opened: list[str] = []
    ray = RayTraceWindowController(root=workspace.service.root, project_context=workspace,
                                  project_navigation=SimpleNamespace(openProjectModule=opened.append))
    appearance = None
    try:
        ray.loadLayerAsset(str(asset))
        for _ in range(500):
            events()
            if not ray.geometricBusy:
                break
        assert ray.hasLayerAsset and not ray.error, ray.error
        assert ray.appearanceController is None
        assert ray.layerAssetPath != str(asset)
        assert ray.layerAssetPath.startswith(str(workspace.service.root))
        cached = ray._project_compute.appearance_context()
        assert cached["asset_root"] == str(workspace.service.root)
        asset.unlink()
        ray.openAppearance()
        assert opened == ["film_appearance"]
        assert ray.workspaceMode == "geometric"
        appearance = RayTraceWindowController(root=workspace.service.root, project_context=workspace,
                                              project_module_key="film_appearance")
        assert appearance.refreshProjectAppearanceContext()
        assert appearance.appearanceController is not None
        assert appearance.appearanceController.project_context is workspace
        assert appearance.appearanceController._context["asset_path"] == ray.layerAssetPath
        assert rows[1][0].host.resource_for(rows[1][0].binding, "compute_workspace") is None
    finally:
        ray.detach_observers()
        ray.deleteLater()
        if appearance is not None:
            appearance.detach_observers()
            appearance.deleteLater()


def test_project_optimizer_close_detaches_without_cancel_rpc(owners):
    workspace, source, _saved = owners[1][0]
    controller = optimizer(workspace, source)
    cancellations = []
    controller._busy = True
    controller._cancel_event = SimpleNamespace(is_set=lambda: False,
                                               set=lambda: cancellations.append(True))
    try:
        controller.requestClose()
        assert cancellations == []
        assert controller._source_closed
    finally:
        controller.shutdown()
        controller.deleteLater()


def test_project_queue_routes_own_candidate_read_only(owners, monkeypatch):
    import coat_sim.ui.film_optimization_quick_controller as optimization
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    workspace, source, _saved = owners[1][0]
    controller = optimizer(workspace, source)
    original = source.session.snapshot()
    configuration = deepcopy(workspace.editor.workflow_configuration("optimization"))
    candidate_design = deepcopy(original.design)
    candidate_design["name"] = "Historical candidate"
    result = _completed_result(SimpleNamespace(design_revision=original.design_revision,
                                              design_snapshot=candidate_design))
    request = SimpleNamespace(objectives=(FilmOptimizationObjective(400, 700, 3),),
                              frozen_material_snapshot={"materials": [material(n=2)]})
    monkeypatch.setattr(optimization, "_film_result_from_queue", lambda _value: result)
    monkeypatch.setattr(optimization, "deserialize_request", lambda _value: request)
    payload = {"request": {"frozen": True}}
    origin = {"project_id": workspace.binding.project_id,
              "project_root": str(workspace.binding.root)}
    row = {"job_id": "history", "origin": origin, "result": payload}
    calls = []
    desktop = ProjectFilmDesktop(owners[0], parent=source)
    monkeypatch.setattr(desktop, "open_project_module", lambda owner, key: calls.append((owner, key)))
    monkeypatch.setattr(desktop, "controller_for", lambda owner, key: controller)
    navigation = ProjectFilmNavigation(desktop, workspace, parent=source)
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine)
    window, queue, _context, _component = create_project_compute_view(
        workspace, "compute_queue", engine, navigation=navigation)
    queue.store = SimpleNamespace(read_job=lambda _job_id: row)
    try:
        queue.filmOptimizationResultReady.emit("history", deepcopy(payload))
        assert calls == [(workspace, "film_optimization")]
        assert controller.hasCandidate and not controller.canApply
        assert source.session.snapshot().design == original.design
        assert workspace.editor.workflow_configuration("optimization") == configuration
        assert rows_unchanged(owners[1][1][1])
        before = len(calls)
        queue.filmOptimizationResultReady.emit("history", {"request": {"forged": True}})
        assert len(calls) == before
        row["origin"] = {"project_id": owners[1][1][0].binding.project_id,
                         "project_root": str(owners[1][1][0].binding.root)}
        queue.filmOptimizationResultReady.emit("history", deepcopy(payload))
        assert len(calls) == before
    finally:
        queue.shutdown()
        window.deleteLater()
        queue.deleteLater()
        controller.shutdown()
        controller.deleteLater()
        engine.deleteLater()


def rows_unchanged(source):
    return source.currentDesign()["name"] == "Bound project film"


def test_project_input_and_output_reject_symlink_paths(owners, tmp_path):
    from coat_sim.ui.raytrace import RayTraceWindowController
    from coat_sim.core.project_compute import ProjectComputeError
    workspace, _source, _saved = owners[1][0]
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "request.json"
    target.write_text("{}", encoding="utf-8")
    selected_link = tmp_path / "selected.json"
    selected_link.symlink_to(target)
    ray = RayTraceWindowController(root=workspace.service.root, project_context=workspace)
    try:
        ray.loadRequest(str(selected_link))
        for _ in range(200):
            events()
            if not ray.geometricBusy:
                break
        assert ray.error and not ray.hasRequest
        data = workspace.service.root / "data"
        preserved_data = workspace.service.root / "data-before-symlink-probe"
        # Named resources now create data eagerly; preserve the fixture's actual
        # documents while replacing only its directory entry for this attack.
        data.rename(preserved_data)
        try:
            data.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ProjectComputeError):
                _ = ray.defaultDirectory
            assert not (outside / "raytrace").exists()
        finally:
            if data.is_symlink():
                data.unlink()
            preserved_data.rename(data)
    finally:
        ray.detach_observers()
        ray.deleteLater()


def test_appearance_completion_replays_when_exit_preflight_is_cancelled(owners, tmp_path, monkeypatch):
    from PySide6.QtCore import QProcess
    from coat_sim.ui.appearance import AppearanceWindowController
    from coat_sim.ui.raytrace import _layer_asset_summary
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    workspace, _source, _saved = owners[1][0]
    external = tmp_path / "appearance-source"
    external.mkdir()
    context = _layer_asset_summary(_write_synthetic_v7_asset(external), external)["appearance_context"]
    controller = AppearanceWindowController(context, root=workspace.service.root,
                                             project_context=workspace)
    accepted = []
    monkeypatch.setattr(controller, "_accept_result", lambda: accepted.append(True))
    controller._job_id = "freeze-test"
    assert controller._project_compute is not None
    controller._project_operation = controller._project_compute.capture("freeze-test")
    controller._phase = "running"
    try:
        controller._project_compute.freeze_editing(True)
        controller._process_finished(0, QProcess.ExitStatus.NormalExit)
        assert not accepted and not controller.localWorkRunning
        assert controller._pending_project_finish is not None
        controller._project_compute.freeze_editing(False)
        assert accepted == [True]
        assert controller._phase == "complete"
    finally:
        controller.shutdown()
        controller.deleteLater()


def test_managed_raytrace_coating_uses_each_project_material_and_qw_thickness(owners):
    from coat_sim.ui.raytrace import _build_film_design_coating
    output = []
    for workspace, _source, _saved in owners[1]:
        design = bound_design()
        design["front_layers"] = [{"id": "layer", "material_id": "shared", "optical_set_id": "visible",
                                    "thickness": {"unit": "qw", "value": 1.0}}]
        saved = workspace.service.create_film(design)
        n = workspace.service.materials.get("shared")["optical_sets"][0]["n"][0]
        request = {"spectrum": [{"wavelength_nm": 550.0}], "surfaces": [{
            "surface_id": "face", "medium_before": [{"n": 1.0, "k": 0.0}],
            "medium_after": [{"n": n, "k": 0.0}]}]}
        converted, _warnings = _build_film_design_coating(
            workspace.service.root, saved.handle, request, "face", "front", service=workspace.service)
        output.append(converted["surfaces"][0]["coating"])
    assert output[0]["indices"][0][1]["n"] == 2.0
    assert output[1]["indices"][0][1]["n"] == 3.0
    assert output[0]["layers"][0]["thickness_nm"] == pytest.approx(550.0 / 8)
    assert output[1]["layers"][0]["thickness_nm"] == pytest.approx(550.0 / 12)


def test_raytrace_view_completion_defers_during_exit_preflight(owners):
    from coat_sim.ui.raytrace import RayTraceWindowController
    workspace, _source, _saved = owners[1][0]
    controller = RayTraceWindowController(root=workspace.service.root, project_context=workspace)
    compute = controller._project_compute
    assert compute is not None
    controller._project_operation = compute.capture("view-result")
    controller._active_token = 1
    result = {"example": "verified worker result"}
    try:
        compute.freeze_editing(True)
        controller._receive_task(1, "result", {"result": result, "path": "owned-path"}, "")
        assert not controller.hasResult and controller._pending_project_task is not None
        compute.freeze_editing(False)
        assert controller.result == result and controller._pending_project_task is None
    finally:
        controller.detach_observers()
        controller.deleteLater()


def test_project_optimizer_worker_disconnect_does_not_cancel_or_submit(owners):
    from coat_sim.ui.film_optimization_quick_controller import _FilmOptimizationTask
    workspace, source, _saved = owners[1][0]
    compute = ProjectComputeWorkspace.for_project(
        workspace.host, workspace.binding)
    compute.register_job("observer", module_key="film_optimization", service_owned=False, state="submitting")
    calls: list[str] = []
    cancel = SimpleNamespace(set=lambda: calls.append("cancel"), is_set=lambda: False)
    request = SimpleNamespace(frozen_material_snapshot=None)
    worker = _FilmOptimizationTask(source.service, cast(Any, request), SimpleNamespace(), "observer", cast(Any, cancel), 1,
                                   remote_admission=lambda: calls.append("submit"), project_compute=compute)
    worker.detach_observer()
    worker.run()
    assert calls == []
    assert compute.job_records()[-1]["state"] == "detached"
    assert compute.active_jobs() == []


def test_project_optimizer_disconnect_stops_polling_without_cancelling_job(owners):
    import threading
    from coat_sim.ui.film_optimization_quick_controller import _FilmOptimizationTask
    workspace, source, _saved = owners[1][0]
    compute = ProjectComputeWorkspace.for_project(workspace.host, workspace.binding)
    compute.register_job("polling", module_key="film_optimization", service_owned=True)
    entered = threading.Event()
    calls: list[str] = []
    def wait(_job_id, *, should_stop, **_kwargs):
        entered.set()
        assert threading.Event().wait(0.01) is False
        while not should_stop():
            threading.Event().wait(0.01)
        return {"state": "queued"}
    request = SimpleNamespace(frozen_material_snapshot={"materials": []}, objectives=(), design_snapshot={})
    client = SimpleNamespace(wait_for_terminal=wait, result=lambda _job: calls.append("result"))
    cancel = SimpleNamespace(set=lambda: calls.append("cancel"), is_set=lambda: False)
    worker = _FilmOptimizationTask(source.service, cast(Any, request), client, "polling", cast(Any, cancel), 1,
                                   project_compute=compute)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert entered.wait(3)
    worker.detach_observer()
    thread.join(3)
    assert not thread.is_alive() and calls == []
    assert compute.job_records()[-1]["state"] == "detached"


def test_companion_rebinding_preserves_candidate_and_dirty_settings_confirmations(owners):
    workspace, source, _saved = owners[1][0]
    replacement = FilmDesignQuickController(service=workspace.service, edit_session=workspace.editor,
                                            project_context=workspace, geometry_settings=MemorySettings())
    optimization = optimizer(workspace, source)
    simulation = FilmSimulationQuickController(service=workspace.service, source_controller=source,
                                               project_context=workspace)
    result = _completed_result(SimpleNamespace(design_revision=source.session.snapshot().design_revision,
                                              design_snapshot=source.currentDesign()))
    try:
        optimization._result = result
        optimization._source_stale = True
        assert optimization.reuseWindowOpen("", source)
        assert optimization._result is result
        assert optimization.reuseWindowOpen("", replacement)
        assert optimization._pending_reuse_source is replacement
        assert optimization._source_controller is source and optimization._result is result
        optimization.resolveSourceReuse(False)
        assert optimization._source_controller is source and optimization._result is result
        simulation._detach_from_source()
        simulation._configuration_dirty = True
        simulation._params["samples"] = 73
        assert simulation.reuseWindowOpen("", replacement)
        assert simulation.hasPendingOpen and simulation.sampleCountText == "73"
        simulation.resolvePendingAction(False)
        assert simulation._source_controller_ref is None and simulation.sampleCountText == "73"
    finally:
        optimization.shutdown()
        optimization.deleteLater()
        simulation.close()
        simulation.deleteLater()
        replacement.markSourceClosed()
        replacement.deleteLater()


def test_appearance_worker_origin_requires_complete_typed_project_provenance(owners, tmp_path):
    import hashlib
    from coat_sim.scheduler.jobs import canonical
    from coat_sim.ui.appearance_remote_worker import _read_input
    workspace = owners[1][0][0]
    identity = "c" * 32
    value = {"schema": "coat_sim.appearance_remote_worker_input", "version": 1,
             "job_id": identity, "input_sha256": "b" * 64,
             "context_sha256": hashlib.sha256(canonical({}).encode()).hexdigest(),
             "context": {}, "render_inputs": {}, "backend": "cpu", "precision": "native",
             "geometry_thickness_mode": "explicit_override", "geometry_thickness_mm": 1.0,
             "host_id": "remote:fixture", "device_id": "cpu:default"}
    path = tmp_path / "worker_input.json"
    origin = {"project_id": workspace.binding.project_id,
              "project_generation": workspace.binding.generation,
              "project_root": str(workspace.binding.root), "configuration_id": None,
              "operation_id": identity}
    value["origin"] = origin
    path.write_text(canonical(value), encoding="utf-8")
    assert _read_input(path, tmp_path)["origin"] == origin
    for malformed in (None, {**origin, "operation_id": "d" * 32},
                      {key: item for key, item in origin.items() if key != "project_root"},
                      {**origin, "untrusted_payload": {"anything": True}}):
        value["origin"] = malformed
        path.write_text(canonical(value), encoding="utf-8")
        with pytest.raises(ValueError, match="project_origin"):
            _read_input(path, tmp_path)
