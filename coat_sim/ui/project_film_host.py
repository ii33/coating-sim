"""Opt-in Qt presentation for project-owned film designer/simulation modules.

Run ``python -m coat_sim.ui.project_film_host --project FILE.coatproj``. This is a film
adapter, not the application launcher or a cross-process project coordinator.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlContext
from shiboken6 import isValid

from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.project_session import ModuleRoute, ProjectSessionHost
from .film_quick_controller import FilmDesignQuickController
from .theme_service import ThemeService


QML_ROOT = Path(__file__).parent / "qml"
RESOURCE_MODULES = frozenset({"process", "machine", "source", "mask", "substrate", "materials"})
COMPUTE_MODULES = frozenset({"film_optimization", "main", "ray_trace", "film_appearance", "compute_queue"})


class ProjectFilmNavigation(QObject):
    """Small QML facade that retains project identity independently of a view."""

    projectNameChanged = Signal()

    def __init__(self, desktop: ProjectFilmDesktop, workspace: ProjectFilmWorkspace,
                 parent: QObject | None = None):
        super().__init__(parent or desktop)
        self.desktop = desktop
        self.workspace = workspace
        self.desktop.projectInformationChanged.connect(self._project_information_changed)

    @Slot(str)
    def _project_information_changed(self, project_id: str) -> None:
        if project_id != self.workspace.binding.project_id:
            return
        try:
            self.workspace.host.store_for(self.workspace.binding)
        except ValueError:
            return
        self.projectNameChanged.emit()

    @Property(str, notify=projectNameChanged)
    def projectName(self) -> str:
        return self.workspace.name

    @Property(list, constant=True)
    def projectModules(self) -> list[dict]:
        return [dict(row) for row in self.workspace.module_options]

    @Slot(str, result=str)
    def openProjectModule(self, module_key: str) -> str:
        return self.desktop.open_project_module(self.workspace, module_key)

    @Slot(result=bool)
    def closeProject(self) -> bool:
        return self.desktop.close_project(self.workspace)

    @Slot()
    def openProjectManagement(self) -> None:
        self.desktop.managementRequested.emit(self.workspace.binding.project_id)

    @Slot(str, str, result=bool)
    def openResourceDocument(self, module_key: str, document_id: str) -> bool:
        """Open an exact managed document through this navigation's owner."""
        return self.desktop.open_resource_document(self.workspace, module_key, document_id)

    @Slot(result=bool)
    def saveCurrent(self) -> bool:
        return self.workspace.editor.save_current()


class ProjectFilmDesktop(QObject):
    """A lazy desktop client of an explicit headless project/session host."""

    moduleError = Signal(str)
    managementRequested = Signal(str)
    projectInformationChanged = Signal(str)

    def __init__(self, host: ProjectSessionHost, parent: QObject | None = None,
                 *, geometry_settings=None):
        super().__init__(parent)
        self.host = host
        self.geometry_settings = geometry_settings
        self._views: dict[ModuleRoute, tuple[Any, ...]] = {}
        self.project_closer: Any | None = None
        self._close_events: set[ModuleRoute] = set()
        self._close_filters: dict[ModuleRoute, QObject] = {}
        self._authorized_project_closes: set[Any] = set()

    @contextmanager
    def authorized_project_close(self, binding):
        """Only the coordinator's current close scope bypasses interception."""
        self._authorized_project_closes.add(binding)
        try:
            yield
        finally:
            self._authorized_project_closes.discard(binding)

    def close_project(self, workspace: ProjectFilmWorkspace) -> bool:
        if workspace.host is not self.host or self.project_closer is None:
            return False
        # store_for checks the entire immutable generation, not only project ID.
        self.host.store_for(workspace.binding)
        return bool(self.project_closer.request(workspace.binding.project_id))

    def open_project(self, root: Path | str) -> ProjectFilmWorkspace:
        session = self.host.open_project(root)
        workspace = ProjectFilmWorkspace.for_project(self.host, session.binding)
        self.open_project_module(workspace, session.entry_module)
        return workspace

    def open_project_module(self, workspace: ProjectFilmWorkspace, module_key: str) -> str:
        if workspace.host is not self.host:
            raise ValueError("Project workspace belongs to a different desktop host")
        if self.project_closer is not None and self.project_closer.is_pending(workspace.binding):
            raise ValueError("此工程正在等待关闭确认，请先返回保存或取消关闭。")
        from .project_asset_materials import asset_publication_busy
        if asset_publication_busy(self.host, workspace.binding):
            raise ValueError("工程资产正在提交，请等待明确结果后再打开工程窗口。")
        if module_key == "materials":
            legacy = next((route for route in self.host.routes(workspace.binding.project_id)
                           if route.module_key == "materials"), None)
            if legacy is None:
                identity = self.open_project_module(workspace, "project_resources")
                asset_owner = self.controller_for(workspace, "project_resources")
                if asset_owner is not None:
                    asset_owner.openMaterialSection()
                return identity
        reservation = workspace.reserve_module(module_key)
        route = reservation.route
        if not reservation.created:
            if reservation.window is not None:
                if module_key in {"film_simulation", "film_optimization"}:
                    source = self.controller_for(workspace, "film_design")
                    if source is None:
                        self.open_project_module(workspace, "film_design")
                        source = self.controller_for(workspace, "film_design")
                    existing_controller = getattr(reservation.window, "_project_film_controller", None)
                    if (source is None or existing_controller is None
                            or getattr(existing_controller, "project_context", None) is not workspace
                            or getattr(existing_controller, "service", None) is not workspace.service):
                        raise ValueError("Project companion requires its current owning designer")
                    if not existing_controller.reuseWindowOpen("", source):
                        self.moduleError.emit(str(
                            getattr(existing_controller, "errorText", None)
                            or getattr(existing_controller, "validationError", None)
                            or "当前伴随窗口暂不能切换到重新打开的设计窗口。"))
                if module_key == "film_appearance":
                    existing_controller = getattr(reservation.window, "_project_film_controller", None)
                    refresh = getattr(existing_controller, "refreshProjectAppearanceContext", None)
                    if callable(refresh):
                        refresh()
                reservation.window.show()
                reservation.window.raise_()
                reservation.window.requestActivate()
            return route.route_id
        controller: Any = None
        window: Any = None
        engine = context = component = None
        try:
            engine = QQmlApplicationEngine(self)
            ThemeService.instance().attach_engine(engine, "Theme")
            context = QQmlContext(engine.rootContext(), engine)
            if module_key == "film_design":
                controller = _ProjectFilmDesignController(
                    service=workspace.service, edit_session=workspace.editor,
                    project_context=workspace, parent=self,
                    geometry_settings=self.geometry_settings)
                component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_ROOT / "FilmDesignQuick.qml")))
                from .project_simulation_resources import ProjectFilmResources
                resources = ProjectFilmResources(workspace, controller, controller)
                controller._project_input_resources = resources
                window = component.createWithInitialProperties(
                    {"controller": controller, "projectResources": resources}, context)
            elif module_key == "film_simulation":
                from .film_simulation_quick_controller import FilmSimulationQuickController
                source = self.controller_for(workspace, "film_design")
                if source is None:
                    self.open_project_module(workspace, "film_design")
                    source = self.controller_for(workspace, "film_design")
                if source is None:
                    raise RuntimeError("Project designer construction is still pending")
                controller = FilmSimulationQuickController(
                    service=workspace.service, source_controller=source,
                    project_context=workspace, parent=self)
                context.setContextProperty("FilmSimulation", controller)
                component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_ROOT / "FilmSimulationQuick.qml")))
                navigation = ProjectFilmNavigation(self, workspace, controller)
                context.setContextProperty("ProjectFilmNavigation", navigation)
                from .project_simulation_resources import ProjectFilmResources
                resources = ProjectFilmResources(workspace, source, controller)
                controller._project_input_resources = resources
                window = component.createWithInitialProperties(
                    {"projectNavigation": navigation, "projectResources": resources}, context)
            elif module_key == "project_resources":
                from .project_resources import ProjectResourcesBridge
                navigation = ProjectFilmNavigation(self, workspace, engine)
                controller = ProjectResourcesBridge(workspace, self)
                context.setContextProperty("bridge", controller)
                component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_ROOT / "ResourceLibraryWindow.qml")))
                window = component.createWithInitialProperties({"projectNavigation": navigation}, context)
                if window is not None:
                    controller.window = window
                    from .icons import window_icon
                    window.setIcon(window_icon(module_key))
                    from .window_geometry import prepare_window_geometry
                    prepare_window_geometry(window, module_key, settings=self.geometry_settings)
            elif module_key in RESOURCE_MODULES:
                from .project_resource_adapter import create_project_resource_view
                navigation = ProjectFilmNavigation(self, workspace, engine)
                window, controller, context, component = create_project_resource_view(
                    workspace, module_key, engine, parent=self,
                    navigation=navigation, geometry_settings=self.geometry_settings)
            elif module_key in COMPUTE_MODULES:
                from .project_compute_views import create_project_compute_view
                if module_key == "film_optimization" and self.controller_for(workspace, "film_design") is None:
                    self.open_project_module(workspace, "film_design")
                    if self.controller_for(workspace, "film_design") is None:
                        raise RuntimeError("Project designer construction is still pending")
                navigation = ProjectFilmNavigation(self, workspace, engine)
                window, controller, context, component = create_project_compute_view(
                    workspace, module_key, engine, parent=self,
                    navigation=navigation, geometry_settings=self.geometry_settings)
            else:
                raise ValueError("Unknown project desktop module")
            if window is None:
                raise RuntimeError("\n".join(error.toString() for error in component.errors()))
            window._project_film_controller = controller
            self.host.attach_window(route, window)
            self._views[route] = (window, controller, engine, context, component)
            from .project_window_close import ProjectWindowCloseFilter
            self._close_filters[route] = ProjectWindowCloseFilter(self, route, window)
            if module_key == "film_design":
                window.simulationRequested.connect(
                    lambda _design, owner=workspace: self.open_project_module(owner, "film_simulation"))
            window.visibleChanged.connect(lambda *_args, owner=route: self._release_hidden(owner))
            window.destroyed.connect(lambda *_args, owner=route: self._release_destroyed(owner))
            window.show()
            return route.route_id
        except BaseException as exc:
            self.host.release_window(route)
            if window is not None:
                window.deleteLater()
            if controller is not None:
                self._dispose_controller(route, controller)
                if route.module_key not in RESOURCE_MODULES:
                    controller.deleteLater()
            if engine is not None:
                engine.deleteLater()
            self.moduleError.emit(str(exc))
            raise

    def open_resource_document(self, workspace: ProjectFilmWorkspace,
                               module_key: str, document_id: str) -> bool:
        """Open or create a resource only within the supplied project binding."""
        from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
        if workspace.host is not self.host:
            raise ValueError("Project resource request belongs to another desktop host")
        if module_key not in RESOURCE_MODULES or module_key == "materials":
            raise ValueError("Project companion is not an editable resource module")
        resources = ProjectResourceWorkspace.for_project(self.host, workspace.binding)
        if resources.binding != workspace.binding or resources.store is not self.host.store_for(workspace.binding):
            raise ValueError("Project resource owner differs from its session binding")

        expected = {"process": "process", "machine": "machine", "source": "source",
                    "substrate": "substrate", "mask": "mask"}[module_key]
        identity = str(document_id or "")
        if identity:
            row = next((item for item in resources.store.manifest["documents"]
                        if item["id"] == identity), None)
            if row is None or row["kind"] != expected:
                raise ValueError("Target document is absent or has the wrong project resource kind")

        self.open_project_module(workspace, module_key)
        controller = resources.controllers.get(module_key)
        if (controller is None or controller.resource_workspace is not resources or
                controller.project_context is not workspace):
            raise ValueError("Project resource route lost its explicit owner")
        window = controller.window
        if identity:
            if controller.currentDocumentId == identity:
                if window is not None and isValid(window):
                    window.show()
                    window.raise_()
                    window.requestActivate()
                return True
            return bool(controller.openDocument(identity))
        return bool(controller.newDocument())

    def controller_for(self, workspace: ProjectFilmWorkspace, module_key: str):
        # The registry is the owner even if another presentation facade opened
        # this project first. Never silently construct an unpaired simulation.
        with self.host.operation_scope(workspace.binding):
            if module_key == "materials":
                from .project_asset_materials import existing_material_view
                _route, _window, controller = existing_material_view(self.host, workspace.binding)
                return controller
            for route in self.host.routes(workspace.binding.project_id):
                if route.module_key != module_key:
                    continue
                window = self.host.window_for(route)
                controller = getattr(window, "_project_film_controller", None)
                if controller is not None:
                    if (getattr(controller, "project_context", None) is not workspace
                            or (module_key in {"film_design", "film_simulation"}
                                and getattr(controller, "service", None) is not workspace.service)):
                        raise ValueError("Registered film controller has a different project owner")
                    return controller
        return None

    def _release_hidden(self, route: ModuleRoute) -> None:
        view = self._views.get(route)
        if view is None or view[0].isVisible() or route not in self._close_events:
            return
        self._close_events.discard(route)
        close_filter = self._close_filters.pop(route, None)
        if close_filter is not None:
            view[0].removeEventFilter(close_filter)
            close_filter.deleteLater()
        self._views.pop(route, None)
        self.host.release_window(route)
        window, controller, engine, _context, _component = view
        self._dispose_controller(route, controller)
        window.deleteLater()
        if route.module_key not in RESOURCE_MODULES:
            controller.deleteLater()
        engine.deleteLater()

    def _release_destroyed(self, route: ModuleRoute) -> None:
        self._close_events.discard(route)
        close_filter = self._close_filters.pop(route, None)
        if close_filter is not None and isValid(close_filter):
            close_filter.deleteLater()
        view = self._views.pop(route, None)
        self.host.release_window(route)
        if view is None:
            return
        _window, controller, engine, _context, _component = view
        if isValid(controller):
            self._dispose_controller(route, controller)
            if route.module_key not in RESOURCE_MODULES:
                controller.deleteLater()
        if isValid(engine):
            engine.deleteLater()

    def open_or_reuse_film_companion(self, module_key: str, source, *, start: bool = False) -> str:
        workspace = getattr(source, "project_context", None)
        if workspace is None or workspace.host is not self.host:
            raise ValueError("Film companion requires an explicitly bound project source")
        if getattr(source, "service", None) is not workspace.service:
            raise ValueError("Film companion source/service project ownership differs")
        if module_key not in {"film_simulation", "film_optimization"}:
            raise ValueError("Unknown project film companion")
        identity = self.open_project_module(workspace, module_key)
        if start:
            target = self.controller_for(workspace, module_key)
            if module_key == "film_optimization":
                target.startOptimization()
            else:
                target.startCalculation()
        return identity

    @staticmethod
    def _dispose_controller(route: ModuleRoute, controller) -> None:
        resources = getattr(controller, "_project_input_resources", None)
        if resources is not None:
            resources.detach()
            controller._project_input_resources = None
        if route.module_key == "film_design":
            controller.markSourceClosed()
        elif route.module_key == "film_simulation":
            controller.close()
        elif route.module_key == "project_resources":
            controller.dispose()
        elif route.module_key in RESOURCE_MODULES:
            from .project_resource_adapter import dispose_project_resource_controller
            dispose_project_resource_controller(controller)
        elif route.module_key in COMPUTE_MODULES:
            from .project_compute_views import dispose_project_compute_controller
            dispose_project_compute_controller(controller)


class _ProjectFilmDesignController(FilmDesignQuickController):
    """Expose the in-project manager from the project-owned designer toolbar."""

    @Slot()
    def openProjectManagement(self) -> None:
        desktop = self.parent()
        workspace = self.project_context
        if (workspace is None or self._source_closed
                or not isinstance(desktop, ProjectFilmDesktop)
                or workspace.host is not desktop.host):
            return
        desktop.managementRequested.emit(workspace.binding.project_id)


def main(argv: list[str] | None = None) -> int:
    from .project_runtime import main as runtime_main
    return runtime_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
