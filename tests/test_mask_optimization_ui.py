"""Default-closure and offscreen UI contracts for project mask optimization."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
import os
import json
import sys
from time import monotonic

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from tests import qt_test_guard

import pytest
from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QPoint, QPointF,
                            QUrl, Qt, qInstallMessageHandler)
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtCore import QThread, Signal
from shiboken6 import isValid

from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.project_session import ProjectSessionHost
from coat_sim.project_session import ProjectSessionError
from coat_sim.ui.project_resource_adapter import ProjectResourceController, dispose_project_resource_controller
from coat_sim.ui.project_film_host import ProjectFilmDesktop, ProjectFilmNavigation
from coat_sim.ui.theme_service import ThemeService
from coat_sim.ui.mask_optimization import resolve_default_mask_optimization_inputs
from tests.test_input_snapshot import fixture
from tests.test_project_core import setup_project
from tests.test_project_module_selector import Navigation

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class _SnapshotController:
    def __init__(self, value):
        self.value = value
        self.commits = 0
        self.dirty = False
        self.display_name = "snapshot"

    def snapshot(self):
        return deepcopy(self.value)

    def commit_pending_edits(self):
        self.commits += 1
        raise AssertionError("default resolver must not commit or blur editor drafts")


def _bound_project(store):
    host = ProjectSessionHost(lambda root: store if Path(root) == store.root else ProjectStore.open(root / MANIFEST_FILENAME))
    session = host.open_project(store.root)
    resources = ProjectResourceWorkspace.for_project(host, session.binding)
    film = ProjectFilmWorkspace.for_project(host, session.binding)
    return host, session.binding, resources, film


def _draft(document_id, *, dirty=False, pending_ui=None, nested=None, data=None):
    return {"module_key": "source", "document_id": document_id, "revision": None,
            "dirty": dirty, "data": data, "editor_state": {},
            "pending_ui": pending_ui, "nested_material": nested}


def test_default_closure_blocks_pending_ui_and_nested_material_without_commit(tmp_path):
    store, _external, _process, _configuration = setup_project(tmp_path)
    host, binding, workspace, _film = _bound_project(store)
    service = workspace.service
    graph = load_project_default(store).graph
    assert graph is not None
    source_id = next(row["id"] for row in graph["documents"] if row["kind"] == "source")

    pending = _SnapshotController(_draft(
        source_id, pending_ui={"object_name": "sourceZField0", "text": "47", "numeric_value_edited": True}))
    workspace.controllers["source"] = pending
    with pytest.raises(Exception, match="未保存修改") as error:
        resolve_default_mask_optimization_inputs(service)
    assert getattr(error.value, "code", None) == "dirty_default_dependency"
    assert pending.commits == 0

    nested = _SnapshotController(_draft(
        source_id, nested={"dirty": True, "pending_ui": None}))
    workspace.controllers["source"] = nested
    with pytest.raises(Exception, match="未保存修改") as error:
        resolve_default_mask_optimization_inputs(service)
    assert getattr(error.value, "code", None) == "dirty_default_dependency"
    assert nested.commits == 0
    workspace.controllers.pop("source")

    # A dirty resource from another project is not visible through this binding.
    other = ProjectStore.create(tmp_path / "other", "Other")
    other_host, other_binding, other_workspace, _other_film = _bound_project(other)
    other_workspace.controllers["source"] = _SnapshotController({"drafts": {
        "source": _draft("00000000-0000-4000-8000-000000000000", dirty=True)}})
    resolved = resolve_default_mask_optimization_inputs(service)
    assert resolved.project_id == binding.project_id
    assert other_binding.project_id != binding.project_id
    assert pending.commits == nested.commits == 0


def test_unrelated_project_dirty_draft_and_saved_nonclosure_draft_do_not_block(tmp_path):
    store, external, process_id, _configuration = setup_project(tmp_path)
    other_process_id = store.import_document(external / "recipe.cproc")
    host, binding, workspace, _film = _bound_project(store)
    unrelated = _SnapshotController({
        "module_key": "process", "document_id": other_process_id, "revision": None,
        "dirty": True, "data": {"name": "Other process"}, "editor_state": {},
        "pending_ui": None, "nested_material": None})
    workspace.controllers["process"] = unrelated
    second_store = ProjectStore.create(tmp_path / "second", "Second")
    _second_host, second_binding, second_workspace, _second_film = _bound_project(second_store)
    second_workspace.controllers["process"] = _SnapshotController(
        {"module_key": "process", "document_id": process_id, "revision": None,
         "dirty": True, "data": {"name": "Foreign"}, "editor_state": {},
         "pending_ui": None, "nested_material": None})
    inputs = resolve_default_mask_optimization_inputs(workspace.service)
    assert inputs.project_id == binding.project_id
    assert second_binding.project_id != binding.project_id


def test_empty_no_default_cross_project_and_stale_binding_fail_strictly(tmp_path):
    empty = ProjectStore.create(tmp_path / "empty", "Empty")
    host, binding, workspace, _film = _bound_project(empty)
    with pytest.raises(Exception) as error:
        resolve_default_mask_optimization_inputs(workspace.service)
    assert getattr(error.value, "code", None) == "empty_project"

    external = tmp_path / "external"
    external.mkdir()
    fixture(external)
    unconfigured = ProjectStore.create(tmp_path / "unconfigured", "Unconfigured")
    unconfigured.import_document(external / "recipe.cproc")
    _host, _binding, resources, _film = _bound_project(unconfigured)
    with pytest.raises(Exception) as error:
        resolve_default_mask_optimization_inputs(resources.service)
    assert getattr(error.value, "code", None) == "no_default"

    (tmp_path / "other").mkdir()
    other, _other_external, _other_process, _other_config = setup_project(tmp_path / "other")
    with pytest.raises(Exception) as error:
        with pytest.MonkeyPatch.context() as patcher:
            # The resolver imports the function locally, so patch its defining module.
            patcher.setattr("coat_sim.core.project_default_loading.load_project_default",
                            lambda _store: load_project_default(other))
            resolve_default_mask_optimization_inputs(workspace.service)
    assert getattr(error.value, "code", None) == "dependency_conflict"

    stale_service = resources.service
    stale_service.binding = replace(stale_service.binding, generation="ended-session")
    with pytest.raises(ProjectSessionError):
        resolve_default_mask_optimization_inputs(stale_service)


def test_missing_default_dependency_fails_before_optimizer_submission(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    store, _external, _process, _configuration = setup_project(project_root)
    graph = load_project_default(store).graph
    assert graph is not None
    source_id = next(row["id"] for row in graph["documents"] if row["kind"] == "source")
    source_row = next(row for row in store.manifest["documents"] if row["id"] == source_id)
    missing_object = store.root / source_row["path"]
    assert missing_object.is_file()
    missing_object.unlink()
    host, _binding, workspace, _film = _bound_project(store)

    with pytest.raises(Exception) as error:
        resolve_default_mask_optimization_inputs(workspace.service)
    assert getattr(error.value, "code", None) in {
        "integrity_error", "invalid_dependency_graph", "missing_dependency"}

    from coat_sim.ui.mask_optimization import MaskOptimizationCoordinator
    coordinator = MaskOptimizationCoordinator(workspace.service)
    worker_calls = []
    monkeypatch.setattr("coat_sim.ui.process._OptimizationWorker",
                        lambda *args, **kwargs: worker_calls.append((args, kwargs)))
    coordinator.startOptimization({}, {})
    assert not worker_calls
    assert not coordinator.get_optimization()["busy"]
    message = coordinator.get_optimizationAvailability()
    assert "missing" in message.lower() or ".csrc" in message


def test_detached_optimization_worker_finishes_after_coordinator_deletion(tmp_path, qt_app, monkeypatch):
    from coat_sim.ui.mask_optimization import MaskOptimizationCoordinator
    from coat_sim.ui.process import (
        _DETACHED_PROCESS_WORKERS,
        _DETACHED_PROCESS_WORKERS_LOCK,
    )

    store, _external, _process, _configuration = setup_project(tmp_path)
    _host, _binding, workspace, _film = _bound_project(store)
    coordinator = MaskOptimizationCoordinator(workspace.service)
    assert coordinator.get_optimizationReady()
    monkeypatch.setattr("coat_sim.ui.process._OptimizationWorker", _CapturingWorker)
    monkeypatch.setattr(workspace.service, "estimate_mask_optimization",
                        lambda settings, snapshot, options=None: {
                            "estimated_s": 1.0, "basis": "test", "memory_warning": False})
    _CapturingWorker.started_event.clear()
    _CapturingWorker.release_event.clear()
    qt_errors = []
    python_errors = []
    old_message_handler = qInstallMessageHandler(
        lambda _kind, _context, message: qt_errors.append(str(message)))
    old_excepthook = sys.excepthook
    monkeypatch.setattr(
        sys, "excepthook",
        lambda error_type, error, _traceback: python_errors.append(
            f"{error_type.__name__}: {error}"))
    worker = None
    try:
        coordinator.startOptimization({}, {})
        assert _CapturingWorker.started_event.wait(1)
        worker = _CapturingWorker.last
        assert worker is not None
        coordinator.detach_observers()
        assert not worker.cancel_event.is_set(), "detaching observers must not cancel submitted work"
        coordinator.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(coordinator)

        # Let the detached job finish only after its QObject observer has gone.
        _CapturingWorker.release_event.set()
        assert worker.wait(2000)
        qt_app.processEvents()
        QTest.qWait(20)
        qt_app.processEvents()
        deadline = monotonic() + 2
        while monotonic() < deadline:
            with _DETACHED_PROCESS_WORKERS_LOCK:
                still_reaping = id(worker) in _DETACHED_PROCESS_WORKERS
            if not still_reaping:
                break
            qt_app.processEvents()
            QTest.qWait(10)
        assert not still_reaping, "detached QThread reaper did not release its worker"
        qt_app.processEvents()
        QTest.qWait(20)
        qt_app.processEvents()
    finally:
        _CapturingWorker.release_event.set()
        if worker is not None:
            worker.wait(2000)
        qInstallMessageHandler(old_message_handler)
        sys.excepthook = old_excepthook
        if isValid(coordinator):
            coordinator.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()

    runtime_errors = [message for message in qt_errors
                      if "RuntimeError" in message or "Traceback" in message]
    assert not runtime_errors, "late finished callback emitted a Qt runtime error:\n" + "\n".join(runtime_errors)
    assert not python_errors, "late finished callback raised in Python:\n" + "\n".join(python_errors)


class _CapturingWorker(QThread):
    progress = Signal(dict)
    resultReady = Signal(object)
    failed = Signal(str)
    started_event = Event()
    release_event = Event()
    last: _CapturingWorker | None = None

    def __init__(self, service, snapshot, settings, options, parent=None, *, estimate=None):
        super().__init__(parent)
        self.cancel_event = Event()
        self.settings = deepcopy(settings)
        self.options = deepcopy(options)
        self.snapshot = deepcopy(snapshot)
        self.started_event = type(self).started_event
        self.release_event = type(self).release_event
        type(self).last = self

    def run(self):
        self.started_event.set()
        self.release_event.wait(4)


@pytest.fixture
def qt_app():
    app = QGuiApplication.instance() or QGuiApplication([])
    return app


def _items(window):
    result = list(window.findChildren(QQuickItem))
    root = window.contentItem()
    stack = [] if root is None else [root]
    seen = {id(item) for item in result}
    while stack:
        item = stack.pop()
        if id(item) not in seen:
            seen.add(id(item))
            result.append(item)
        stack.extend(item.childItems())
    return result


def _item(window, name):
    for item in _items(window):
        if item.objectName() == name:
            return item
    found = window.findChild(QQuickItem, name)
    if found is not None:
        return found
    return window.findChild(QObject, name)


def _click(app, window, item):
    assert item is not None and item.isVisible() and item.isEnabled()
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier,
                     QPoint(round(point.x()), round(point.y())))
    app.processEvents()


def _type_into_quick(window, item, text):
    item.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for character in text:
        key = (Qt.Key.Key_0 + int(character) if character.isdigit() else
               Qt.Key.Key_Period if character == "." else
               Qt.Key.Key_Minus if character == "-" else None)
        assert key is not None, f"unsupported test key {character!r}"
        QTest.keyClick(window, key)


def test_theme_field_deferred_callbacks_are_safe_after_field_deletion(qt_app):
    qml_root = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml"
    engine = QQmlEngine()
    engine.setNetworkAccessManagerFactory(qt_test_guard.NoNetworkAccessManagerFactory())
    engine.rootContext().setContextProperty("Theme", {"snapshot": {
        "fontFamily": "Sans Serif", "fontSizeBody": 12,
        "fontWeightNormal": 400, "spaceSm": 8, "controlHeightMd": 36,
        "radiusSm": 3, "borderWidth": 1, "focusRingWidth": 2,
        "colorText": "#17212B", "colorDisabledText": "#687786",
        "colorTextMuted": "#526170", "colorSelection": "#D8EAFE",
        "colorWindow": "#F3F6FA", "colorSurface": "#FFFFFF",
        "colorSurfaceRaised": "#F8FAFC", "colorBorder": "#D4DDE6",
        "colorBorderStrong": "#7C8B9A", "colorFocusRing": "#095DB2",
        "colorDisabledSurface": "#E7ECF2",
    }})
    component = QQmlComponent(engine)
    component.setData(b"""
        import QtQuick
        import \"theme_controls\" as ThemeControls
        Item {
            width: 240; height: 80
            ThemeControls.ThemeField {
                objectName: \"doomedThemeField\"
                width: 220
                numericDisplayValue: 1.2345
            }
        }
    """, QUrl.fromLocalFile(str(qml_root / "__mask_theme_field_delete_test__.qml")))
    assert component.isReady(), [error.toString() for error in component.errors()]
    root = component.create()
    assert isinstance(root, QQuickItem), [error.toString() for error in component.errors()]
    window = QQuickWindow()
    window.resize(240, 80)
    root.setParentItem(window.contentItem())
    messages = []
    old_handler = qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(str(message)))
    try:
        window.show()
        qt_app.processEvents()
        field = next((item for item in root.childItems()
                      if item.objectName() == "doomedThemeField"), None)
        assert field is not None
        field.forceActiveFocus()
        field.setProperty("numericDisplayValue", 2.71828)
        field.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(field)
        QTest.qWait(20)
        qt_app.processEvents()
    finally:
        qInstallMessageHandler(old_handler)
        window.close()
        root.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
    runtime_errors = [message for message in messages
                      if "TypeError" in message or "Unable to assign" in message]
    assert not runtime_errors, "delayed QML field callbacks failed after deletion:\n" + "\n".join(runtime_errors)


def test_standalone_mask_window_accepts_empty_optimization_artifacts(tmp_path, qt_app):
    from coat_sim.core.api import CoatService
    from coat_sim.ui.machine_mask import MaskBridge

    bridge = MaskBridge(CoatService(root=tmp_path))
    assert bridge.property("optimization") == {}
    engine = QQmlEngine()
    engine.setNetworkAccessManagerFactory(qt_test_guard.NoNetworkAccessManagerFactory())
    ThemeService.instance().attach_engine(engine)
    launcher = QObject()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("launcher", launcher)
    component = QQmlComponent(
        engine, QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]
                                       / "coat_sim/ui/qml/MaskWindow.qml")))
    messages = []
    old_handler = qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(str(message)))
    window: QQuickWindow | None = None
    try:
        created_window = component.createWithInitialProperties({"initialMaximized": False})
        assert isinstance(created_window, QQuickWindow), "\n".join(
            [error.toString() for error in component.errors()])
        window = created_window
        window.resize(960, 640)
        window.show()
        qt_app.processEvents()
        QTest.qWait(25)
        qt_app.processEvents()
        dialog = window.findChild(QObject, "maskOptimizationDialog")
        assert dialog is not None
        assert not bool(dialog.property("visible"))
    finally:
        qInstallMessageHandler(old_handler)
        if window is not None:
            window.close()
            window.deleteLater()
        bridge.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
    runtime_errors = [message for message in messages
                      if "TypeError" in message or "artifacts" in message.lower()]
    assert not runtime_errors, "standalone mask QML accessed absent optimization artifacts:\n" + "\n".join(runtime_errors)


def _scroll_into_view(app, item):
    ancestor = item.parentItem()
    while ancestor is not None:
        content_y = ancestor.property("contentY")
        content_height = ancestor.property("contentHeight")
        viewport_height = ancestor.property("height")
        if (content_y is not None and content_height is not None and viewport_height is not None
                and float(content_height) > float(viewport_height)):
            item_y = item.mapToItem(ancestor, QPointF(0, 0)).y()
            maximum = max(0.0, float(content_height) - float(viewport_height))
            target = max(0.0, min(float(content_y) + item_y, maximum))
            ancestor.setProperty("contentY", target)
            app.processEvents()
            QTest.qWait(20)
            app.processEvents()
            return
        ancestor = ancestor.parentItem()
    QTest.qWait(20)
    app.processEvents()


def _load_project_window(engine, film, workspace, key, navigation):
    engine.setNetworkAccessManagerFactory(qt_test_guard.NoNetworkAccessManagerFactory())
    controller = ProjectResourceController(film, workspace, key, navigation=navigation)
    workspace.controllers[key] = controller
    engine.rootContext().setContextProperty("bridge", controller.bridge)
    qml_path = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml" / {
        "mask": "MaskWindow.qml", "machine": "MachineWindow.qml"}[key]
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml_path)))
    window = component.createWithInitialProperties({
        "projectNavigation": navigation,
        "projectResources": controller,
        "initialMaximized": False,
    })
    assert isinstance(window, QQuickWindow), "\n".join(error.toString() for error in component.errors())
    controller.window = window
    workspace.windows[key] = window
    window.show()
    return window, controller, component


def test_project_mask_small_window_default_fields_stale_reparse_cancel_and_machine_resources(tmp_path, qt_app, monkeypatch):
    store, _external, process_id, _old_config = setup_project(tmp_path)
    base = {"settings": {"grid_size": 23, "time_steps": 3, "carrier_rpm": 27.5,
                          "carrier_phase_deg": 37.25, "planet_phase_deg": 14.5,
                          "source_exponent": 2.25, "source_rate_nm_s": 1.2},
            "backend": "GPU", "fast_cycle": False,
            "fast_cycle_residual_percent": 33.5}
    first_config = store.add_deposition_configuration("Mask default A", process_id, base)
    store.set_default_configuration(first_config)
    host, binding, workspace, film = _bound_project(store)
    assert workspace.initialize_default(load_project_default(store))

    qml_messages = []
    old_message_handler = qInstallMessageHandler(
        lambda _kind, _context, message: qml_messages.append(str(message)))
    engine = QQmlEngine()
    ThemeService.instance().attach_engine(engine)
    navigation = Navigation("Mask optimization test")
    mask_window, mask_controller, _component = _load_project_window(
        engine, film, workspace, "mask", navigation)
    mask_window.resize(760, 560)
    mask_window.show()
    qt_app.processEvents()
    QTest.qWait(50)
    qt_app.processEvents()

    machine_window = None
    machine_controller = None
    machine_engine = None
    worker = None
    try:
        button = _item(mask_window, "maskOptimizationButton")
        assert button is not None and button.isVisible() and button.isEnabled()
        assert _item(mask_window, "newMachineButton") is None
        assert not any(item.property("text") == "新建机器" for item in _items(mask_window))
        # Small-window render includes the open Dialog, its scroller and the actions.
        _click(qt_app, mask_window, button)
        dialog = _item(mask_window, "maskOptimizationDialog")
        assert dialog is not None and dialog.property("visible")
        status = _item(mask_window, "optimizationStatusLabel")
        assert status is not None
        assert "" == status.property("text") or "default" not in str(status.property("text"))
        assert _item(mask_window, "optimizationDefaultSummary").property("text").startswith("默认工艺：")
        time_field = _item(mask_window, "optimizationTimeStepField")
        carrier_rpm = _item(mask_window, "optimizationCarrierRpmField")
        assert time_field is not None and carrier_rpm is not None
        assert carrier_rpm.property("text") == "27.5"
        assert _item(mask_window, "optimizationGridSizeField").property("text") == "23"
        assert _item(mask_window, "optimizationCarrierPhaseField").property("text") == "37.25"
        assert _item(mask_window, "optimizationPlanetPhaseField").property("text") == "14.5"
        assert str(_item(mask_window, "optimizationBackendSelector").property("currentText")) == "GPU"
        assert _item(mask_window, "optimizationAlgorithmSelector").property("currentIndex") == 0
        assert _item(mask_window, "optimizationThresholdField").property("text") == "33.5"
        bounds_override = _item(mask_window, "optimizationBoundXMinField0")
        assert bounds_override is not None and bounds_override.isEnabled()
        _type_into_quick(mask_window, bounds_override, "-6")
        qt_app.processEvents()
        assert bounds_override.property("text") == "-6"
        mask_controller.bridge._optimization_coordinator._optimization_progress(
            {"stage": "optimize", "evaluations": 1})
        qt_app.processEvents()
        assert isValid(bounds_override)
        assert _item(mask_window, "optimizationBoundXMinField0") is bounds_override
        assert bounds_override.property("text") == "-6", "progress must not rebuild user-edited mask bounds"

        # Preserve an explicit override; untouched fields track a newly selected default.
        rpm_override = _item(mask_window, "optimizationCarrierRpmField")
        assert rpm_override is not None
        _scroll_into_view(qt_app, rpm_override)
        _type_into_quick(mask_window, rpm_override, "39.5")
        qt_app.processEvents()
        assert rpm_override.property("text") == "39.5"
        changed = deepcopy(base)
        changed_settings = changed.get("settings")
        assert isinstance(changed_settings, dict)
        changed_settings.update(carrier_rpm=42.25, carrier_phase_deg=91.5, grid_size=31)
        changed.update(backend="CPU", fast_cycle=True, fast_cycle_residual_percent=62.0,
                       algorithm="reference", algorithm_options={
                           "sampling_policy": "automatic", "periodic_integration": True,
                           "max_evaluations": 6})
        second_config = store.add_deposition_configuration("Mask default B", process_id, changed)
        store.set_default_configuration(second_config)

        # A changed default is never silently paired with the old dialog values.
        start = _item(mask_window, "startOptimizationButton")
        assert start.isEnabled()
        _scroll_into_view(qt_app, start)
        _click(qt_app, mask_window, start)
        status = _item(mask_window, "optimizationStatusLabel")
        assert "默认配置已变化" in str(status.property("text"))
        assert not start.isEnabled()
        refresh = _item(mask_window, "refreshOptimizationDefaultButton")
        _scroll_into_view(qt_app, refresh)
        _click(qt_app, mask_window, refresh)
        assert _item(mask_window, "optimizationCarrierRpmField").property("text") == "39.5"
        assert _item(mask_window, "optimizationCarrierPhaseField").property("text") == "91.5"
        assert _item(mask_window, "optimizationGridSizeField").property("text") == "31"
        assert str(_item(mask_window, "optimizationBackendSelector").property("currentText")) == "CPU"
        assert "reference" in str(_item(mask_window, "optimizationScoringNotice").property("text"))
        assert "algorithm_options" in str(_item(mask_window, "optimizationScoringNotice").property("text"))
        reset = _item(mask_window, "resetOptimizationParametersButton")
        _scroll_into_view(qt_app, reset)
        _click(qt_app, mask_window, reset)
        assert _item(mask_window, "optimizationCarrierRpmField").property("text") == "42.25"

        _CapturingWorker.started_event.clear()
        _CapturingWorker.release_event.clear()
        monkeypatch.setattr("coat_sim.ui.process._OptimizationWorker", _CapturingWorker)
        monkeypatch.setattr(workspace.service, "estimate_mask_optimization",
                            lambda settings, snapshot, options=None: {
                                "estimated_s": 3.0, "basis": "test", "memory_warning": False})
        start = _item(mask_window, "startOptimizationButton")
        _scroll_into_view(qt_app, start)
        _click(qt_app, mask_window, start)
        assert _CapturingWorker.started_event.wait(1), str(_item(mask_window, "optimizationStatusLabel").property("text"))
        worker = _CapturingWorker.last
        assert worker is not None
        assert worker.settings["carrier_rpm"] == 42.25
        assert worker.settings["carrier_phase_deg"] == 91.5
        assert worker.settings["planet_phase_deg"] == 14.5
        assert worker.settings["grid_size"] == 31
        assert worker.settings["time_steps"] == 3, "unmodified configured sample count remains exact"
        assert worker.settings["source_exponent"] == 2.25
        assert worker.settings["source_rate_nm_s"] == 1.2
        assert worker.options["backend"] == "CPU"
        assert worker.options["fast_cycle"] is True
        assert worker.options["fast_cycle_residual_percent"] == 62.0
        assert worker.options["search_grid_size"] == 31
        assert "algorithm" not in worker.options and "algorithm_options" not in worker.options

        # A user-edited sampling step overrides the stored exact step count.
        _CapturingWorker.release_event.set()
        assert worker.wait(2000)
        qt_app.processEvents()
        QTest.qWait(20)
        qt_app.processEvents()
        time_field = _item(mask_window, "optimizationTimeStepField")
        _type_into_quick(mask_window, time_field, "0.2")
        qt_app.processEvents()
        assert time_field.property("text") == "0.2"
        assert _item(mask_window, "maskOptimizationDialog").property("timeStepEdited") is True
        _CapturingWorker.started_event.clear()
        _CapturingWorker.release_event.clear()
        _click(qt_app, mask_window, _item(mask_window, "startOptimizationButton"))
        assert _CapturingWorker.started_event.wait(1)
        worker = _CapturingWorker.last
        assert worker is not None
        expected_steps = workspace.service.time_fields(
            process_path=workspace.service.document_handle(process_id), time_step_s=0.2)["time_steps"]
        assert worker.settings["time_steps"] == expected_steps
        assert worker.options["backend"] == "CPU"

        scroll = _item(mask_window, "maskOptimizationScroll")
        assert scroll.property("contentHeight") > scroll.height()
        cancel = _item(mask_window, "cancelOptimizationButton")
        _scroll_into_view(qt_app, cancel)
        assert cancel.isVisible() and cancel.isEnabled()
        mask_controller.bridge.setEditingFrozen(True)
        assert not worker.cancel_event.is_set(), "freezing the window does not cancel submitted work"
        current_worker = _CapturingWorker.last
        mask_controller.bridge.startOptimization({}, {})
        assert _CapturingWorker.last is current_worker
        assert not worker.cancel_event.is_set(), "frozen state rejects new work without cancelling the active job"
        mask_controller.bridge.setEditingFrozen(False)
        _click(qt_app, mask_window, cancel)
        assert worker.cancel_event.is_set()
        _CapturingWorker.release_event.set()
        assert worker.wait(2000)
        qt_app.processEvents()
        QTest.qWait(20)
        qt_app.processEvents()

        image = mask_window.grabWindow()
        assert not image.isNull()
        assert image.save(str(tmp_path / "mask-optimization-small-window.png"))

        machine_engine = QQmlEngine()
        ThemeService.instance().attach_engine(machine_engine)
        machine_window, machine_controller, _machine_component = _load_project_window(
            machine_engine, film, workspace, "machine", Navigation("Machine small test"))
        machine_window.resize(760, 560)
        machine_window.show()
        qt_app.processEvents()
        source_pickers = [item for item in _items(machine_window)
                          if item.objectName() == "machineSourceDependencyPicker"]
        mask_pickers = [item for item in _items(machine_window)
                        if item.objectName() == "machineMaskDependencyPicker"]
        assert len(source_pickers) == len(mask_pickers) == 2
        assert all(picker.isVisible() and picker.property("currentIndex") >= 0
                   for picker in source_pickers + mask_pickers)
        machine_data = machine_controller.bridge._data
        for kind, pickers, slots in (("source", source_pickers, machine_data["sources"]),
                                     ("mask", mask_pickers, machine_data["masks"])):
            rows = machine_controller.resourcesForKind(kind)
            for picker in pickers:
                slot = int(str(picker.property("pointer")).split("/")[2])
                reference = slots[slot]["file"]
                identity = machine_controller.service._reference_identity(reference, kind)
                assert picker.property("currentReference") == reference
                assert rows[picker.property("currentIndex")]["id"] == identity
        assert _item(machine_window, "sourceFileField0") is None
        assert _item(machine_window, "maskFileField0") is None
        assert _item(machine_window, "importSourceButton").property("text") == "导入"
        assert _item(machine_window, "importMaskButton").property("text") == "导入"
        assert _item(machine_window, "operationBarLabel") is None
        for object_name, kind_label in (
                ("deleteSourceButton", "蒸发源"), ("deleteMaskButton", "修正板")):
            buttons = [item for item in _items(machine_window)
                       if item.objectName() == object_name]
            assert len(buttons) == 2
            names = set()
            for delete_button in buttons:
                assert delete_button.property("text") == "×"
                accessible = QAccessible.queryAccessibleInterface(delete_button)
                assert accessible is not None
                name = accessible.text(QAccessible.Text.Name)
                assert name.startswith("删除" + kind_label + " ")
                names.add(name)
            assert names == {f"删除{kind_label} 1", f"删除{kind_label} 2"}
        machine_qml = (Path(__file__).resolve().parents[1] /
                       "coat_sim/ui/qml/MachineWindow.qml").read_text(encoding="utf-8")
        assert 'ToolTip.text: "删除蒸发源 " + (sourceSlot.slotIndex + 1)' in machine_qml
        assert 'ToolTip.text: "删除修正板 " + (maskSlot.slotIndex + 1)' in machine_qml
        assert not any(item.property("objectName") == "machineAssetBundleDialog"
                       for item in _items(machine_window))
        for kind, pickers in (("source", source_pickers), ("mask", mask_pickers)):
            action_names = {"source": {"importSourceButton", "sourceEditorActionButton"},
                            "mask": {"importMaskButton", "maskEditorActionButton",
                                     "deleteMaskButton"}}[kind]
            for picker in pickers:
                row = picker.parentItem()
                for action in row.childItems():
                    if action.objectName() not in action_names:
                        continue
                    row_point = action.mapToItem(row, QPointF(0, 0))
                    assert row_point.x() >= -1
                    assert row_point.x() + action.width() <= row.width() + 1, (
                        f"{kind} dependency action {action.objectName()} overflows its resource row")
        for picker in source_pickers + mask_pickers:
            point = picker.mapToScene(QPointF(0, 0))
            assert point.x() >= -1
            assert point.x() + picker.width() <= machine_window.width() + 1

        # The project import operation copies an external dependency into the
        # project without changing the external file, and preserves physical
        # machine fields such as source height.
        source_z = _item(machine_window, "sourceZField0")
        assert source_z is not None
        _type_into_quick(machine_window, source_z, "47")
        QTest.keyClick(machine_window, Qt.Key.Key_Return)
        qt_app.processEvents()
        assert machine_controller.bridge._data["sources"][0]["z_mm"] == 47.0
        external_source = tmp_path / "external-new-source.csrc"
        external_source.write_text(json.dumps({
            "schema_version": 2, "kind": "point", "device_type": "custom",
            "source_exponent": 3.0,
        }), encoding="utf-8")
        source_bytes = external_source.read_bytes()
        machine_controller.bridge.setDependencyFile(
            "/sources/0/file", QUrl.fromLocalFile(str(external_source)).toString())
        assert not isValid(source_z), "the edited QML field was replaced with the updated machine row"
        QTest.qWait(15)
        qt_app.processEvents()
        assert external_source.read_bytes() == source_bytes
        assert machine_controller.bridge._data["sources"][0]["z_mm"] == 47.0
        imported_reference = machine_controller.bridge._data["sources"][0]["file"]
        imported_id = machine_controller.service._reference_identity(imported_reference, "source")
        assert imported_id
        imported_row = next(row for row in store.manifest["documents"] if row["id"] == imported_id)
        imported_object = store.root / imported_row["path"]
        assert imported_object.is_file() and imported_object != external_source
        assert json.loads(imported_object.read_text(encoding="utf-8"))["source_exponent"] == 3.0
        machine_controller.bridge.saveDocument()
        machine_controller.bridge.loadDocument(machine_controller.bridge.documentPath)
        assert machine_controller.bridge._data["sources"][0]["z_mm"] == 47.0
    finally:
        # Only assess messages emitted while the live UI is bound. Some legacy
        # bridge properties are nulled during QObject destruction by design.
        qInstallMessageHandler(old_message_handler)
        runtime_qml_errors = [message for message in qml_messages
                              if "TypeError" in message or "Unable to assign" in message]
        if worker is not None and isValid(worker) and worker.isRunning():
            _CapturingWorker.release_event.set()
            worker.wait(2000)
        if machine_window is not None and isValid(machine_window):
            machine_window.setProperty("projectCloseAuthorized", True)
            machine_window.close()
            dispose_project_resource_controller(machine_controller, discard=True)
            machine_window.deleteLater()
        if machine_engine is not None and isValid(machine_engine):
            machine_engine.deleteLater()
        if isValid(mask_window):
            mask_window.setProperty("projectCloseAuthorized", True)
            mask_window.close()
            dispose_project_resource_controller(mask_controller, discard=True)
            mask_window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
    assert not runtime_qml_errors, "QML bindings emitted errors:\n" + "\n".join(runtime_qml_errors)


def test_empty_project_optimization_dialog_opens_with_visible_error(tmp_path, qt_app):
    store = ProjectStore.create(tmp_path / "empty", "Empty project")
    host, _binding, workspace, film = _bound_project(store)
    workspace.initialize_default(load_project_default(store))
    messages = []
    old_handler = qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(str(message)))
    engine = QQmlEngine()
    ThemeService.instance().attach_engine(engine)
    window, controller, _component = _load_project_window(
        engine, film, workspace, "mask", Navigation("Empty mask test"))
    window.resize(720, 480)
    window.show()
    qt_app.processEvents()
    try:
        button = _item(window, "maskOptimizationButton")
        assert button is not None and button.isVisible() and button.isEnabled()
        _click(qt_app, window, button)
        assert _item(window, "maskOptimizationDialog").property("visible")
        availability = _item(window, "optimizationAvailabilityLabel")
        assert availability is not None and "项目为空" in str(availability.property("text"))
        assert not _item(window, "startOptimizationButton").isEnabled()
        assert not controller.bridge.optimizationReady
    finally:
        qInstallMessageHandler(old_handler)
        window.setProperty("projectCloseAuthorized", True)
        window.close()
        dispose_project_resource_controller(controller, discard=True)
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
    runtime_errors = [message for message in messages
                      if "TypeError" in message or "Unable to assign" in message]
    assert not runtime_errors, "empty-project error view emitted QML errors:\n" + "\n".join(runtime_errors)


def test_result_candidates_keep_their_origin_and_both_new_boards_can_be_saved(tmp_path):
    store, _external, _process, _configuration = setup_project(tmp_path)
    host, binding, workspace, _film = _bound_project(store)
    from coat_sim.ui.mask_optimization import MaskOptimizationCoordinator
    coordinator = MaskOptimizationCoordinator(workspace.service)
    inputs = coordinator._inputs
    assert inputs is not None
    graph = load_project_default(store).graph
    assert graph is not None
    mask_rows = [row for row in graph["documents"] if row["kind"] == "mask"]
    original_profiles = {row["id"]: deepcopy(workspace.service.load_document(row["id"]).data)
                         for row in mask_rows}
    first = deepcopy(mask_rows[0]["data"])
    second = deepcopy(first)
    first["name"] = "Original candidate name"
    second.pop("name", None)
    first["description"] = "generated candidate description"
    first["notes"] = "retain candidate notes"
    second["description"] = "another generated description"
    second["notes"] = "retain candidate notes"
    second["z_mm"] = float(second.get("z_mm", 0)) + 2
    coordinator._optimization_result({"simulation": {}, "pending_masks": [
        {"slot": 0, "profile": first}, {"slot": 1, "profile": second}],
        "result_path": str(tmp_path / "result.json"), "report_path": "",
        "report": {"best_verified": {"cv_percent": 1.0}, "group_reports": []},
        "artifacts": {}}, inputs)
    coordinator.refreshDefault()
    assert coordinator._result_inputs is inputs
    assert "结果所属配置 " + inputs.configuration_id in coordinator.get_optimization()["message"]
    coordinator.saveOptimizedMaskNamed("First candidate.cmsk", 0)
    count_after_first = len(workspace.service.list_documents("mask"))
    assert count_after_first == len(mask_rows) + 1
    coordinator.saveOptimizedMaskNamed("Second candidate.cmsk", 1)
    masks = workspace.service.list_documents("mask")
    assert len(masks) == len(mask_rows) + 2
    original_ids = {row["id"] for row in mask_rows}
    saved = [row for row in masks if row["id"] not in original_ids]
    assert len(saved) == 2
    saved_profiles = [workspace.service.load_document(row["id"]).data for row in saved]
    assert {row["name"] for row in saved} == {"First candidate", "Second candidate"}
    assert {profile["name"] for profile in saved_profiles} == {"First candidate", "Second candidate"}
    assert {profile["description"] for profile in saved_profiles} == {
        "generated candidate description", "another generated description"}
    assert {profile["notes"] for profile in saved_profiles} == {"retain candidate notes"}
    assert sorted(profile["z_mm"] for profile in saved_profiles) == sorted(
        [first["z_mm"], second["z_mm"]])
    from coat_sim.core.project_resource_service import ProjectResourceService
    reopened = ProjectResourceService(ProjectStore.open(store.project_file))
    assert {profile["name"] for profile in (
        reopened.load_document(row["id"]).data for row in saved)} == {"First candidate", "Second candidate"}
    for identity, original in original_profiles.items():
        assert reopened.load_document(identity).data == original
    assert coordinator._pending_masks[0]["profile"] == first
    assert coordinator._pending_masks[1]["profile"] == second
    assert coordinator.get_optimization()["result_configuration_id"] == inputs.configuration_id
    assert binding.project_id == inputs.project_id
    workspace.service._gate.frozen = True
    before_frozen_save = len(masks)
    coordinator.saveOptimizedMaskNamed("blocked.cmsk", 0)
    coordinator.startOptimization({}, {})
    assert len(workspace.service.list_documents("mask")) == before_frozen_save
    assert not coordinator.get_optimizationReady()


def test_project_dependency_route_targets_exact_document_preserves_same_doc_dirty_and_rejects_foreign_or_missing(tmp_path, qt_app):
    first_root = tmp_path / "first"
    first_root.mkdir()
    store, external, _process, _configuration = setup_project(first_root)
    host, binding, workspace, film = _bound_project(store)
    assert workspace.initialize_default(load_project_default(store))
    desktop = ProjectFilmDesktop(host)
    navigation = ProjectFilmNavigation(desktop, film)
    wrong_controller = None
    try:
        desktop.open_project_module(film, "source")
        source_controller = desktop.controller_for(film, "source")
        assert source_controller is not None
        source_id = source_controller.currentDocumentId
        source_controller.bridge.setField("source_exponent", "7.25")
        assert source_controller.dirty

        desktop.open_project_module(film, "machine")
        machine_controller = desktop.controller_for(film, "machine")
        assert machine_controller is not None
        machine = machine_controller.bridge._data
        source_identity = machine_controller.service._reference_identity(
            machine["sources"][0]["file"], "source")
        assert source_identity == source_id
        assert machine_controller.openDependencyEditor("/sources/0/file")
        assert desktop.controller_for(film, "source") is source_controller
        assert source_controller.currentDocumentId == source_identity
        assert source_controller.dirty
        assert source_controller.bridge._fields["source_exponent"] == "7.25"

        mask_ids = [machine_controller.service._reference_identity(row["file"], "mask")
                    for row in machine["masks"]]
        assert len(set(mask_ids)) == 2
        assert machine_controller.openDependencyEditor("/masks/0/file")
        mask_controller = desktop.controller_for(film, "mask")
        assert mask_controller is not None and mask_controller.currentDocumentId == mask_ids[0]
        assert machine_controller.openDependencyEditor("/masks/1/file")
        assert desktop.controller_for(film, "mask") is mask_controller
        assert mask_controller.currentDocumentId == mask_ids[1]
        mask_controller.bridge.setValue("/z_mm", json.dumps(321))
        assert mask_controller.dirty
        # Routing succeeds as a request, but the retained mask draft must not
        # change until the existing replacement confirmation is accepted.
        assert machine_controller.openDependencyEditor("/masks/0/file")
        assert mask_controller.currentDocumentId == mask_ids[1] and mask_controller.dirty
        assert mask_controller.bridge._pending_document_replace is not None
        assert mask_controller.bridge.confirmDocumentReplace(True)
        assert mask_controller.currentDocumentId == mask_ids[0]
        assert not mask_controller.dirty

        original_source_reference = machine["sources"][1]["file"]
        machine["sources"][1]["file"] = "missing.csrc"
        assert not machine_controller.openDependencyEditor("/sources/1/file")
        assert ("导入" in machine_controller.status or "复制" in machine_controller.status
                or "missing" in machine_controller.status)
        machine["sources"][1]["file"] = original_source_reference

        default_configuration_id = store.manifest["default_configuration_id"]
        recipe_path = external / "recipe.cproc"
        recipe_bytes = recipe_path.read_bytes()
        mask_coordinator = mask_controller.bridge._optimization_coordinator
        mask_coordinator._optimization.update(
            project_id=binding.project_id,
            artifacts={"process_path": str(recipe_path)},
        )
        revision_before_wrong_path = store.manifest["revision"]
        assert not mask_controller.openOptimizedProcess(str(external / "different.cproc"))
        assert store.manifest["revision"] == revision_before_wrong_path
        assert mask_controller.openOptimizedProcess(str(recipe_path))
        process_controller = desktop.controller_for(film, "process")
        assert process_controller is not None and process_controller.currentDocumentId
        assert process_controller.currentDocumentId != _process
        assert store.manifest["default_configuration_id"] == default_configuration_id
        assert store.manifest["revision"] > revision_before_wrong_path
        assert recipe_path.read_bytes() == recipe_bytes

        foreign_root = tmp_path / "foreign"
        foreign_root.mkdir()
        foreign_store, _foreign_external, _foreign_process, _foreign_configuration = setup_project(
            foreign_root)
        foreign_session = host.open_project(foreign_store.root)
        foreign_workspace = ProjectResourceWorkspace.for_project(host, foreign_session.binding)
        foreign_film = ProjectFilmWorkspace.for_project(host, foreign_session.binding)
        assert foreign_workspace.initialize_default(load_project_default(foreign_store))
        wrong_controller = ProjectResourceController(
            foreign_film, foreign_workspace, "machine", navigation=navigation)
        assert not wrong_controller.openDependencyEditor("/sources/0/file")
        status = str(wrong_controller.property("status"))
        assert ("其他项目" in status or "不一致" in status or "同项目" in status)
        assert binding.project_id != foreign_session.binding.project_id

        old_navigation = machine_controller.project_navigation
        old_window = machine_controller.window
        old_view = next(view for route, view in desktop._views.items()
                        if route.module_key == "machine")
        old_engine = old_view[2]
        machine_controller.bridge.setValue("/carrier_rpm", json.dumps(77.0))
        assert machine_controller.dirty
        old_window.setProperty("projectCloseAuthorized", True)
        old_window.close()
        QTest.qWait(30)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
        desktop.open_project_module(film, "machine")
        reopened_machine = desktop.controller_for(film, "machine")
        assert reopened_machine is not None
        assert reopened_machine is machine_controller, "the retained controller is reused across presentation reopen"
        assert reopened_machine.dirty and reopened_machine.bridge._data["carrier_rpm"] == 77.0
        assert reopened_machine.project_navigation is not old_navigation
        assert reopened_machine.project_navigation.workspace is film
        new_view = next(view for route, view in desktop._views.items()
                        if route.module_key == "machine")
        assert new_view[2] is not old_engine
        assert not isValid(old_engine)
        assert reopened_machine.openDependencyEditor("/sources/0/file")
    finally:
        if wrong_controller is not None:
            dispose_project_resource_controller(wrong_controller, discard=True)
        for _route, view in tuple(desktop._views.items()):
            window = view[0]
            window.setProperty("projectCloseAuthorized", True)
            window.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
        desktop.deleteLater()
