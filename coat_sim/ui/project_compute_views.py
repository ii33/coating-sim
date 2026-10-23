"""Lazy Qt views for explicit project-owned compute controllers.

The desktop owns the route reservation, engine, navigation, and final disposal.
This factory never selects an active project or constructs a global service.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlComponent, QQmlContext

from coat_sim.core.project_compute import ProjectComputeWorkspace
from coat_sim.core.project_film_session import ProjectFilmWorkspace

QML_ROOT = Path(__file__).parent / "qml"


def _wire_project_queue_results(queue, workspace: ProjectFilmWorkspace, navigation) -> None:
    """Route only a verified selected result into this workspace's reserved view."""
    def target(module_key: str):
        desktop = getattr(navigation, "desktop", None)
        if desktop is None:
            raise ValueError("Project result routing requires its desktop navigation")
        desktop.open_project_module(workspace, module_key)
        controller = desktop.controller_for(workspace, module_key)
        if controller is None or getattr(controller, "project_context", None) is not workspace:
            raise ValueError("Result destination has a different project owner")
        return controller

    def existing_target(module_key: str):
        desktop = getattr(navigation, "desktop", None)
        if desktop is None:
            return None
        controller = desktop.controller_for(workspace, module_key)
        if (controller is None
                or getattr(controller, "project_context", None) is not workspace):
            return None
        return controller

    def film_result(job_id, payload):
        try:
            row = queue._require_owned_job(job_id)
            if not isinstance(row, dict) or payload != row.get("result"):
                raise ValueError("Film result differs from its project queue record")
            controller = target("film_optimization")
            if not controller.inspectQueueResult(job_id, payload, origin=row["origin"]):
                raise ValueError(controller.errorText)
            queue._result_status("已打开本项目历史候选；没有自动应用到当前设计。")
        except Exception as exc:
            queue._result_status("无法打开项目候选：" + str(exc))

    def synthesis_result(job_id, payload):
        try:
            row = queue._require_owned_job(job_id)
            from coat_sim.scheduler.result_capability import is_synthesis_result
            viewer = getattr(queue, "_result_viewer", None)
            from coat_sim.ui.compute_queue import _synthesis_result_bound_to_job
            replay_source = getattr(viewer, "_shown_synthesis_source_job", None)
            # Every historical synthesis selection, not only legacy records,
            # must have come through the physical background reader. Re-read
            # the source row and immutable JobSpec before routing the cache.
            same_source = (isinstance(row, dict) and isinstance(replay_source, dict)
                and viewer is not None and viewer._shown_job == job_id
                and viewer._shown_kind == "film_synthesis"
                and replay_source == row and viewer._shown_film_result == payload
                and _synthesis_result_bound_to_job(getattr(queue.store, "root", None),
                    row, row.get("result")))
            if not same_source or not is_synthesis_result(payload):
                raise ValueError("Synthesis result differs from its verified typed project queue record")
            controller = target("film_optimization")
            inspect = getattr(controller, "inspectSynthesisQueueResult", None)
            if not callable(inspect) or not inspect(job_id, payload, origin=row["origin"]):
                raise ValueError(getattr(controller, "errorText", "Synthesis result could not be opened"))
            queue._result_status("Opened this project's historical synthesis candidate in read-only mode.")
        except Exception as exc:
            queue._result_status("Unable to open the project synthesis candidate: " + str(exc))

    def raytrace_result(job_id, result_path):
        try:
            row = queue._require_owned_job(job_id)
            from coat_sim.scheduler.result_capability import is_geometric_raytrace_result
            if not isinstance(row, dict) or not is_geometric_raytrace_result(row.get("result")):
                raise ValueError("Queue row does not contain a typed ray-trace result")
            compute = queue._project_compute
            path = compute.owned_path(result_path)
            downloads = compute.owned_path("artifacts/runtime/scheduler/downloads")
            if not path.is_relative_to(downloads) or not path.is_file():
                raise ValueError("Ray-trace result is outside this project's download cache")
            viewer = queue._result_viewer
            if (viewer is None or viewer._shown_job != job_id
                    or viewer._shown_raytrace_path != str(path)):
                raise ValueError("Ray-trace artifact differs from the verified selected result")
            controller = target("ray_trace")
            if controller.busy:
                raise ValueError("请先完成当前光追操作，再查看队列结果。")
            controller.loadResult(str(path))
            queue._result_status("已在本项目光追窗口中打开所选历史结果。")
        except Exception as exc:
            queue._result_status("无法打开项目光追结果：" + str(exc))

    def cancel_received(job_id, result, error):
        if error or result is not True:
            return
        try:
            row = queue._require_owned_job(job_id)
            if not isinstance(row, dict) or not isinstance(row.get("origin"), dict):
                return
            controller = existing_target("film_optimization")
            accept = getattr(controller, "onProjectQueueCancelAccepted", None)
            if callable(accept):
                accept(job_id, result, row["origin"])
        except Exception:
            # A closed or stale project view has no live observer to receive
            # the cancellation intent; the queue remains authoritative.
            return

    queue.filmOptimizationResultReady.connect(film_result)
    queue.filmSynthesisResultReady.connect(synthesis_result)
    queue.rayTraceResultReady.connect(raytrace_result)
    queue.cancelReceived.connect(cancel_received)


def _design_source(workspace: ProjectFilmWorkspace):
    with workspace.host.operation_scope(workspace.binding):
        for route in workspace.host.routes(workspace.binding.project_id):
            if route.module_key != "film_design":
                continue
            window = workspace.host.window_for(route)
            controller = getattr(window, "_project_film_controller", None)
            expected_editor = workspace.editor
            if (controller is not None
                    and getattr(controller, "session", None) is expected_editor
                    and getattr(controller, "project_context", None) is workspace):
                return controller
    raise ValueError("Open this project's film designer before its optimization window")


def create_project_compute_view(workspace: ProjectFilmWorkspace, module_key: str, engine, parent=None,
                                *, navigation=None, geometry_settings=None):
    """Return ``(window, controller, context, component)`` for one reserved route.

    ``geometry_settings`` is accepted for the desktop's uniform factory contract;
    the desktop applies the shared window geometry manager after construction.
    ``main`` requires its own engine because its image-provider name is fixed.
    """
    compute = ProjectComputeWorkspace.for_project(workspace.host, workspace.binding)
    context = QQmlContext(engine.rootContext(), engine)
    properties = {"projectNavigation": navigation}
    controller: Any
    resources: Any
    if module_key == "compute_queue":
        from .compute_queue import ComputeQueueView
        controller = ComputeQueueView(
            engine, compute.root, project_context=workspace, navigation=navigation)
        if navigation is not None:
            _wire_project_queue_results(controller, workspace, navigation)
        if parent is not None:
            controller.setParent(parent)
        return controller.window, controller, controller.context, controller.component
    if module_key == "main":
        from .main import MainBridge
        controller = MainBridge(compute.service, project_context=workspace)
        if parent is not None:
            controller.setParent(parent)
        engine.addImageProvider("thickness", controller.image_provider)
        context.setContextProperty("bridge", controller)
        from .project_simulation_resources import ProjectDepositionResources
        resources = ProjectDepositionResources(workspace, controller, controller)
        controller._project_input_resources = resources
        properties["projectResources"] = resources
        qml_file = "MainWindow.qml"
        if navigation is not None:
            controller.showQueueRequested.connect(
                lambda: navigation.openProjectModule("compute_queue"))
    elif module_key == "film_optimization":
        from .film_optimization_quick_controller import FilmOptimizationQuickController
        controller = FilmOptimizationQuickController(
            _design_source(workspace), parent=parent, project_context=workspace)
        properties["controller"] = controller
        from .project_simulation_resources import ProjectFilmResources
        resources = ProjectFilmResources(workspace, _design_source(workspace), controller)
        controller._project_input_resources = resources
        properties["projectResources"] = resources
        qml_file = "FilmOptimizationWindow.qml"
    elif module_key in {"ray_trace", "film_appearance"}:
        from .raytrace import RayTraceWindowController
        controller = RayTraceWindowController(
            root=compute.root, parent=parent, project_context=workspace,
            project_module_key=module_key, project_navigation=navigation)
        context.setContextProperty("bridge", controller)
        properties["projectModuleKey"] = module_key
        qml_file = "RayTraceWindow.qml"
        if module_key == "film_appearance":
            controller.refreshProjectAppearanceContext()
        from .project_raytrace_resources import ProjectManagedSimulationResources
        resources = ProjectManagedSimulationResources(workspace, controller, module_key, controller)
        controller._project_input_resources = resources
        resources.resourceOperationFinished.connect(controller.onProjectInputOperationFinished)
        properties["projectResources"] = resources
    else:
        raise ValueError("Unsupported project compute module: " + str(module_key))
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_ROOT / qml_file)))
    window = component.createWithInitialProperties(properties, context)
    if window is None:
        dispose_project_compute_controller(controller)
        controller.deleteLater()
        context.deleteLater()
        component.deleteLater()
        raise RuntimeError("\n".join(error.toString() for error in component.errors()))
    return window, controller, context, component


def dispose_project_compute_controller(controller) -> None:
    """Detach a presentation client without cancelling service-owned jobs.

    An appearance subprocess cannot outlive its Qt process owner. The desktop
    must resolve its explicit cancel/wait close decision before disposing it.
    The controller's existing QML close flow covers an ordinary window close.
    """
    resources = getattr(controller, "_project_input_resources", None)
    if resources is not None:
        resources.detach()
        controller._project_input_resources = None
    detach = getattr(controller, "detach_observers", None)
    if callable(detach):
        detach()
    compute = getattr(controller, "_project_compute", None)
    thaw = getattr(controller, "_on_project_unfrozen", None)
    if compute is not None and callable(thaw):
        compute.unregister_thaw_callback(thaw)
    coordinator = getattr(controller, "_run_coordinator", None)
    if coordinator is not None:
        coordinator.detach_service_observer()
    from .compute_queue import ComputeQueueView
    if isinstance(controller, ComputeQueueView):
        controller.shutdown()
