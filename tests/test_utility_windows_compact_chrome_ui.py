"""Rendered, isolated QA for utility-window status and top-spacing migrations.

Run once at each scale factor with the workspace interpreter:
    $env:QT_SCALE_FACTOR = '1'
    .\\.conda\\python.exe -c "import runpy; runpy.run_path('tests/test_utility_windows_compact_chrome_ui.py')['test_utility_windows_compact_chrome_ui']()"

The test writes screenshots and a hash manifest under its dedicated build QA
directory. It uses fake decision and queue services and local-only controllers.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("PYTHONUTF8", "1")

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
QML_DIR = ROOT / "coat_sim" / "ui" / "qml"
_scale = os.environ.get("QT_SCALE_FACTOR", "1").replace(".", "_")
EVIDENCE = ROOT / "build" / f"qa_ui_utility_windows_{_scale}_{uuid.uuid4().hex[:8]}"
NETWORK_FACTORY_EVIDENCE: list[dict[str, object]] = []
sys.path.insert(0, str(TESTS))

# This must precede every project import. It isolates QSettings and blocks HTTP
# requests made by each actual QQmlEngine below.
import qt_test_guard  # noqa: E402,F401

from PySide6.QtCore import QObject, Property, QPoint, QPointF, Qt, QUrl, Signal, Slot  # noqa: E402
from PySide6.QtGui import QAccessible, QFont, QFontDatabase, QGuiApplication, QColor, QImage, QWheelEvent  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from coat_sim.ui.appearance import AppearanceWindowController, _canonical, _sha256  # noqa: E402
from coat_sim.ui.raytrace import (RayTraceWindowController, SCHEMA as RAYTRACE_SCHEMA,
                                  SCHEMA_VERSION as RAYTRACE_SCHEMA_VERSION)  # noqa: E402
from coat_sim.ui.theme_service import ThemeEngineBridge  # noqa: E402
from coat_sim.ui.theme_tokens import build_theme_snapshot  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _DecisionFixture(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._decision = {
            "decision_id": "qa-decision-1",
            "question": "Choose a fixture option",
            "options": [
                {"label": "Fixture option A", "description": "Local test choice A"},
                {"label": "Fixture option B", "description": "Local test choice B"},
            ],
            "deadline_ms": 0,
            "remaining_s": 30,
            "optional": False,
            "source": "qa",
            "submitting": False,
            "error": "",
        }
        self.deferred_ids: list[str] = []
        self.submitted: list[tuple[str, int, str]] = []
        self.paused_ids: list[str] = []

    @Property("QVariantMap", notify=changed)
    def decision(self):
        return dict(self._decision)

    def set_decision(self, **values) -> None:
        self._decision = {**self._decision, **values}
        self.changed.emit()

    @Slot(str)
    def deferDecision(self, decision_id: str) -> None:
        self.deferred_ids.append(decision_id)

    @Slot(str)
    def pauseDecisionCountdown(self, decision_id: str) -> None:
        self.paused_ids.append(decision_id)

    @Slot(str, int, str)
    def submitDecision(self, decision_id: str, index: int, supplement: str) -> None:
        self.submitted.append((decision_id, index, supplement))


class _QueueFixture(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._loading = False
        self._status = ""
        self._jobs = [{
            "job_id": "qa-failed-job",
            "state": "failed",
            "stateLabel": "Failed",
            "summary": "Fixture task with a local error",
            "error": "Local fixture task error stays on its task card.",
            "durationLabel": "0 seconds",
            "finishTimeText": "Fixture completion time",
            "expected_finish_at": None,
            "memory_estimate_label": "",
            "progress": {},
            "kind": "qa",
            "resultAvailable": False,
            "resultUnavailableReason": "Fixture result unavailable.",
            "result": None,
            "cancel_requested": False,
        }]
        self.refresh_calls = 0
        self.cancel_calls: list[str] = []
        self.opened_results: list[str] = []
        self.selected_tabs: list[int] = []

    @Property("QVariantList", notify=changed)
    def jobs(self):
        return list(self._jobs)

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(bool, notify=changed)
    def loading(self):
        return self._loading

    @Property(bool, notify=changed)
    def hasMoreJobs(self):
        return False

    @Property("QVariantList", notify=changed)
    def pendingCancelJobIds(self):
        return list(self.cancel_calls)

    def set_state(self, *, loading: bool | None = None,
                  status: str | None = None) -> None:
        if loading is not None:
            self._loading = loading
        if status is not None:
            self._status = status
        self.changed.emit()

    @Slot(int)
    def selectQueueTab(self, index: int) -> None:
        self.selected_tabs.append(index)

    @Slot()
    def refreshQueue(self) -> None:
        self.refresh_calls += 1

    @Slot()
    def loadMoreQueueJobs(self) -> None:
        return

    @Slot(str)
    def cancelQueueJob(self, job_id: str) -> None:
        self.cancel_calls.append(job_id)

    @Slot(str)
    def openQueueResult(self, job_id: str) -> None:
        self.opened_results.append(job_id)


class _ForbiddenQueueClient:
    """A local sentinel: the window QA must never submit or poll a real job."""

    def status(self, *_args, **_kwargs):
        raise AssertionError("utility-window QA attempted a queue status request")

    def submit(self, *_args, **_kwargs):
        raise AssertionError("utility-window QA attempted a queue submission")

    def request_cancel(self, *_args, **_kwargs):
        raise AssertionError("utility-window QA attempted a queue cancellation")


def _find_item(root: QQuickItem, object_name: str) -> QQuickItem | None:
    pending = [root]
    while pending:
        item = pending.pop()
        if item.objectName() == object_name:
            return item
        pending.extend(item.childItems())
    return None


def _find_item_by_text(root: QQuickItem, wanted: str) -> QQuickItem | None:
    pending = [root]
    while pending:
        item = pending.pop()
        try:
            if str(item.property("text")) == wanted:
                return item
        except RuntimeError:
            pass
        pending.extend(item.childItems())
    return None


def _find_object(root, object_name: str):
    matches = root.findChildren(QObject, object_name)
    return matches[0] if matches else None


class _CountingNoNetworkAccessManagerFactory(qt_test_guard.NoNetworkAccessManagerFactory):
    def __init__(self):
        super().__init__()
        self.manager_create_count = 0

    def create(self, parent):
        self.manager_create_count += 1
        return super().create(parent)


def _create_page(app, page: str, mode: str, service, controller=None, *, snapshot=None):
    engine = QQmlEngine()
    factory = _CountingNoNetworkAccessManagerFactory()
    engine.setNetworkAccessManagerFactory(factory)
    snapshot = snapshot or build_theme_snapshot(mode)
    bridge = ThemeEngineBridge(
        snapshot, 0, mode, mode, "utility-window-qa", f"QA {mode}", {}, parent=engine
    )
    context = engine.rootContext()
    context.setContextProperty("Theme", bridge)
    if page == "assistant":
        context.setContextProperty("decisionUi", service)
    elif page == "queue":
        context.setContextProperty("computeQueue", service)
    elif page == "raytrace":
        context.setContextProperty("bridge", controller)

    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_DIR / {
        "assistant": "AssistantDecisionWindow.qml",
        "queue": "ComputeQueueWindow.qml",
        "appearance": "FilmAppearanceWindow.qml",
        "raytrace": "RayTraceWindow.qml",
    }[page])))
    _assert(component.status() == QQmlComponent.Status.Ready,
            f"{page} QML did not compile: "
            + "\n".join(error.toString() for error in component.errors()))
    if page == "appearance":
        window = component.createWithInitialProperties({"controller": controller})
    else:
        window = component.create()
    _assert(window is not None,
            f"{page} window did not create: "
            + "\n".join(error.toString() for error in component.errors()))
    QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
    window.setGeometry(80, 80, int(window.width()), int(window.height()))
    window.showNormal()
    for _ in range(4):
        app.processEvents()
        QTest.qWait(25)
    return {
        "engine": engine,
        "factory": factory,
        "bridge": bridge,
        "snapshot": snapshot,
        "component": component,
        "window": window,
        "controller": controller,
        "service": service,
    }


def _assert_page_top_spacing(runtime, page: str) -> None:
    window = runtime["window"]
    layout_name = {
        "assistant": "assistantDecisionContentLayout",
        "queue": "computeQueueContentLayout",
        "appearance": "appearanceContentLayout",
        "raytrace": "rayTraceContentLayout",
    }[page]
    layout = _find_item(window.contentItem(), layout_name)
    _assert(layout is not None, f"{page} main content layout was not found")
    visible_children = [child for child in layout.childItems()
                        if child.isVisible() and child.width() > 0 and child.height() > 0]
    _assert(visible_children, f"{page} main content has no visible first row")
    expected = float(window.property("contentTopMargin")) + float(runtime["snapshot"]["spaceSm"])
    actual = float(layout.mapToItem(window.contentItem(), QPointF(0, 0)).y())
    _assert(abs(actual - expected) < 1.0,
            f"{page} first content row starts at {actual:.1f}; expected {expected:.1f} logical px")


def _assert_status(runtime, *, severity: str, text_part: str = "",
                   detail_part: str = "") -> None:
    window = runtime["window"]
    root = window.contentItem()
    status = _find_item(root, "windowChromeStatus")
    rendered = _find_item(root, "windowChromeStatusText")
    tooltip = _find_object(window, "windowChromeStatusToolTip")
    _assert(status is not None and rendered is not None,
            "shared status presentation or text was not created")
    _assert(tooltip is None, "shared status presentation must not create a tooltip")
    _assert(not bool(status.property("activeFocusOnTab"))
            and not bool(status.property("focus")),
            "shared status presentation unexpectedly accepts keyboard focus")
    _assert(not any("MouseArea" in str(child.metaObject().className())
                    for child in status.childItems()),
            "shared status presentation unexpectedly contains a mouse handler")
    _assert(str(window.property("statusSeverity")) == severity,
            f"expected severity {severity}, got {window.property('statusSeverity')!r}")
    full = str(window.property("statusDetail")) or str(window.property("statusText"))
    if text_part:
        _assert(bool(status.property("visible")), "non-empty status is hidden")
        _assert(text_part in str(rendered.property("text")),
                f"visible title status missed {text_part!r}: {rendered.property('text')!r}")
    else:
        _assert(not bool(status.property("visible")), "empty status left a title control visible")
    if detail_part:
        _assert(detail_part in full, f"status detail missed {detail_part!r}")
        accessible = QAccessible.queryAccessibleInterface(status)
        accessible_name = accessible.text(QAccessible.Text.Name) if accessible else ""
        _assert(detail_part in str(accessible_name),
                "complete status detail is missing from the accessible title control")


def _assert_header_geometry(runtime, page: str) -> None:
    window = runtime["window"]
    root = window.contentItem()
    caption = _find_item(root, "windowChromeCaption")
    status = _find_item(root, "windowChromeStatus")
    minimize = _find_item(root, "windowChromeMinimizeButton")
    maximize = _find_item(root, "windowChromeMaximizeButton")
    close = _find_item(root, "windowChromeCloseButton")
    _assert(all(item is not None for item in (caption, status, minimize, maximize, close)),
            f"{page} shared title chrome is incomplete")
    _assert(status.width() > 0,
            f"{page} status hit target collapsed at {window.width()} logical px")
    left = lambda item: float(item.mapToItem(root, QPointF(0, 0)).x())
    _assert(left(caption) + caption.width() <= left(status) + 0.5,
            f"{page} caption overlaps status at narrow width")
    _assert(left(status) + status.width() <= left(minimize) - 1,
            f"{page} status overlaps system buttons at narrow width")
    _assert(left(close) + close.width() <= float(window.width()) + 0.5,
            f"{page} close button falls outside the window")


def _save_screenshot(runtime, page: str, mode: str, stage: str) -> dict[str, object]:
    window = runtime["window"]
    image = window.grabWindow()
    _assert(not image.isNull(), f"{page}/{mode}/{stage} screenshot was blank")
    name = f"{page}_{mode}_{stage}.png"
    path = EVIDENCE / name
    _assert(image.save(str(path)), f"could not save screenshot {path}")
    return {"file": name, "pixels": [image.width(), image.height()],
            "logical": [int(window.width()), int(window.height())],
            "dpr": float(window.devicePixelRatio()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _appearance_context() -> dict[str, object]:
    process_profile = {
        "film_design_import": {"surface": "front"},
        "film_design_snapshot": {"design": {"front_layers": [], "substrate": {
            "thickness": {"value": 1.0, "unit": "mm"}}}},
    }
    return {
        "schema": "coat_sim.appearance_launch_context",
        "version": 1,
        "asset_path": str(EVIDENCE / "fixture.coatasset"),
        "asset_root": str(EVIDENCE / "fixture-assets"),
        "asset_sha256": "a" * 64,
        "process_profile": process_profile,
        "process_profile_sha256": _sha256(_canonical(process_profile)),
        "substrate_profile": {
            "outline": {"kind": "rectangle", "width_mm": 10.0, "height_mm": 8.0}
        },
        "captured_step_ids": [],
    }


def _load_capture_font(app) -> str | None:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    families: list[str] = []
    for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        path = fonts / filename
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    preferred = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
                      if name in families), None)
    if preferred:
        app.setFont(QFont(preferred))
    return preferred


def _exercise_page(app, page: str, mode: str, dpr: float,
                   screenshots: list[dict[str, object]]) -> None:
    service = _DecisionFixture() if page == "assistant" else (
        _QueueFixture() if page == "queue" else None)
    controller = None
    if page == "appearance":
        controller = AppearanceWindowController(_appearance_context(),
                                                root=EVIDENCE / "appearance-fixture")
    elif page == "raytrace":
        controller = RayTraceWindowController(
            root=EVIDENCE / "raytrace-fixture", queue_client=_ForbiddenQueueClient())
        controller._request_text = json.dumps({
            "schema": RAYTRACE_SCHEMA,
            "schema_version": RAYTRACE_SCHEMA_VERSION,
        })

    runtime = _create_page(app, page, mode, service, controller)
    runtime["app"] = app
    window = runtime["window"]
    short = "fixture-visible-error-"
    long_error = short * 46
    try:
        _assert(window.isVisible(), f"{page}/{mode} window did not show")
        _assert(abs(float(window.devicePixelRatio()) - dpr) < 0.02,
                f"{page}/{mode} DPR mismatch: expected {dpr}, got {window.devicePixelRatio()}")
        _assert_page_top_spacing(runtime, page)
        screenshots.append(_save_screenshot(runtime, page, mode, "loaded"))

        if page == "assistant":
            _assert_status(runtime, severity="info")
            service.set_decision(error=long_error)
            app.processEvents()
            _assert_status(runtime, severity="error", text_part="提交失败",
                           detail_part=long_error)
            screenshots.append(_save_screenshot(runtime, page, mode, "long_error"))
            window.setWidth(float(window.minimumWidth()))
            app.processEvents()
            QTest.qWait(60)
            _assert_header_geometry(runtime, page)
            _assert_page_top_spacing(runtime, page)
            screenshots.append(_save_screenshot(runtime, page, mode, "narrow_error"))
            submit = _find_item(window.contentItem(), "decisionSubmitButton")
            _assert(submit is not None and submit.isEnabled(),
                    "approval action was lost or disabled after a submit error")
            window.requestActivate()
            QTest.mouseMove(window, QPoint(4, int(window.height()) - 4))
            submit.forceActiveFocus(Qt.FocusReason.TabFocusReason)
            app.processEvents()
            QTest.qWait(120)
            focus_item = window.activeFocusItem()
            focus_name = focus_item.objectName() if focus_item is not None else ""
            _assert(_find_object(window, "windowChromeStatusToolTip") is None,
                    "static WindowChrome status unexpectedly created a tooltip")
            status = _find_item(window.contentItem(), "windowChromeStatus")
            _assert(status is not None and not bool(status.property("activeFocus")),
                    "status presentation unexpectedly retained keyboard focus")
            bounds = submit.mapRectToScene(submit.boundingRect())
            _assert(submit.isVisible() and bounds.width() > 0 and bounds.height() > 0
                    and 0 <= bounds.x() < window.width()
                    and 0 <= bounds.y() < window.height(),
                    f"approval button is outside the visible window: bounds={bounds}, "
                    f"window={window.width()}x{window.height()}, focus={focus_name!r}")
            point = submit.mapToScene(QPointF(submit.width() / 2, submit.height() / 2))
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier,
                             QPoint(round(point.x()), round(point.y())))
            app.processEvents()
            _assert(service.submitted == [("qa-decision-1", 0, "")],
                    f"approval click did not reach the fake decision: {service.submitted!r}; "
                    f"button={bounds}, click=({point.x():.1f},{point.y():.1f}), "
                    f"window={window.width()}x{window.height()}, focus={focus_name!r}")
            service.set_decision(error="")
            app.processEvents()
            _assert_status(runtime, severity="info")
            window.close()
            app.processEvents()
            _assert(not window.isVisible(), "decision window did not hide on close")
            _assert(service.deferred_ids == ["qa-decision-1"],
                    "decision close no longer defers exactly the current fake decision")

        elif page == "queue":
            _assert_status(runtime, severity="info")
            terminal_reason = _find_item(window.contentItem(), "queueTerminalReason")
            _assert(terminal_reason is not None and terminal_reason.isVisible()
                    and "Local fixture task error" in str(terminal_reason.property("text")),
                    "failed task error left its task card")
            service.set_state(loading=True)
            app.processEvents()
            _assert_status(runtime, severity="busy", text_part="正在加载")
            service.set_state(loading=False, status=long_error)
            app.processEvents()
            _assert_status(runtime, severity="info", text_part=short,
                           detail_part=long_error)
            screenshots.append(_save_screenshot(runtime, page, mode, "long_error"))
            window.setWidth(float(window.minimumWidth()))
            app.processEvents()
            QTest.qWait(60)
            _assert_header_geometry(runtime, page)
            _assert_page_top_spacing(runtime, page)
            screenshots.append(_save_screenshot(runtime, page, mode, "narrow_error"))
            service.set_state(status="")
            app.processEvents()
            _assert_status(runtime, severity="info")
            refresh = _find_item_by_text(window.contentItem(), "刷新")
            _assert(refresh is not None and refresh.isEnabled(), "queue refresh action is missing")
            point = refresh.mapToScene(QPointF(refresh.width() / 2, refresh.height() / 2))
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier,
                             QPoint(round(point.x()), round(point.y())))
            app.processEvents()
            _assert(service.refresh_calls == 1,
                    "queue refresh action did not remain bound to the isolated fixture")
            _assert_status(runtime, severity="info")
            window.close()
            app.processEvents()
            _assert(not window.isVisible(), "compute queue window did not close")

        elif page == "appearance":
            _assert_status(runtime, severity="info", text_part="导入")
            controller._phase = "starting"
            controller._status = "Starting isolated local render"
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="busy", text_part="Starting isolated")
            controller._phase = "running"
            controller._status = "Rendering fixture frames"
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="busy", text_part="Rendering fixture")
            controller._phase = "failed"
            controller._status = "Fixture render failed"
            controller._error = long_error
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="error", text_part="渲染失败",
                           detail_part=long_error)
            validation = _find_item(window.contentItem(), "appearanceValidationMessage")
            _assert(validation is not None and validation.isVisible()
                    and long_error in str(validation.property("text")),
                    "appearance field-level failure context disappeared")
            _assert(_find_item(window.contentItem(), "appearanceStatus") is None
                    and _find_item(window.contentItem(), "appearanceFixedActions") is None,
                    "old bottom status/action placeholders remain after layout migration")
            screenshots.append(_save_screenshot(runtime, page, mode, "long_error"))
            window.setWidth(float(window.minimumWidth()))
            app.processEvents()
            QTest.qWait(60)
            _assert_header_geometry(runtime, page)
            _assert_page_top_spacing(runtime, page)
            screenshots.append(_save_screenshot(runtime, page, mode, "narrow_error"))
            controller._phase = "complete"
            controller._status = "Fixture render completed"
            controller._error = ""
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="success", text_part="Fixture render completed")
            controller._phase = "ready"
            controller._status = "Ready for local render"
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="info", text_part="Ready for local render")
            window.close()
            for _ in range(8):
                app.processEvents()
                QTest.qWait(20)
            _assert(not window.isVisible(), "clean appearance close path did not finish")
            _assert(not controller.running, "appearance close left a local render running")

        else:
            controller.setWorkspaceMode("geometric")
            app.processEvents()
            _assert_status(runtime, severity="info", text_part="选择逐层工艺资产")
            controller._phase = "running"
            controller._busy = True
            controller._progress = 0.43
            controller._status = "Sampling fixture rays"
            controller._error = ""
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="busy", text_part="Sampling fixture rays")
            progress = _find_item(window.contentItem(), "rayTraceProgressBar")
            _assert(progress is not None and progress.isVisible()
                    and abs(float(progress.property("value")) - 0.43) < 0.01,
                    "ray-trace progress indicator disappeared or lost its controller value")
            controller._phase = "failed"
            controller._busy = False
            controller._status = "Fixture ray trace failed"
            controller._error = long_error
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="error", text_part=short,
                           detail_part=long_error)
            _assert(_find_item(window.contentItem(), "rayTraceControllerStatus") is None,
                    "old bottom status placeholder remains after title migration")
            screenshots.append(_save_screenshot(runtime, page, mode, "long_error"))
            window.setWidth(float(window.minimumWidth()))
            app.processEvents()
            QTest.qWait(60)
            _assert_header_geometry(runtime, page)
            _assert_page_top_spacing(runtime, page)
            screenshots.append(_save_screenshot(runtime, page, mode, "narrow_error"))
            controller._phase = "complete"
            controller._status = "Fixture trace complete"
            controller._error = ""
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="success", text_part="Fixture trace complete")
            controller._phase = "ready"
            controller._status = "Ready for fixture request"
            controller._error = ""
            controller.changed.emit()
            app.processEvents()
            _assert_status(runtime, severity="info", text_part="Ready for fixture request")
            window.close()
            for _ in range(8):
                app.processEvents()
                QTest.qWait(20)
            _assert(not window.isVisible(), "clean ray-trace close path did not finish")
            _assert(controller._closed, "ray-trace close did not detach its local observers")

        _assert(not runtime["factory"].blocked_urls,
                f"{page} unexpectedly attempted HTTP: {runtime['factory'].blocked_urls!r}")
        NETWORK_FACTORY_EVIDENCE.append({
            "page": page,
            "theme": mode,
            "factory_installed": True,
            "manager_create_count": runtime["factory"].manager_create_count,
            "blocked_urls": list(runtime["factory"].blocked_urls),
        })
    finally:
        if window.isVisible():
            window.hide()
        if page == "appearance" and controller is not None:
            controller.shutdown()
        elif page == "raytrace" and controller is not None:
            controller.detach_observers()
        runtime["window"].deleteLater()
        runtime["component"].deleteLater()
        runtime["engine"].deleteLater()
        app.processEvents()
        QTest.qWait(20)


def test_utility_windows_compact_chrome_ui() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    capture_font = _load_capture_font(app)
    dpr = float(app.primaryScreen().devicePixelRatio())
    requested = float(os.environ.get("QT_SCALE_FACTOR", "1"))
    _assert(abs(dpr - requested) < 0.02,
            f"offscreen DPR mismatch: requested {requested}, got {dpr}")
    screenshots: list[dict[str, object]] = []
    for mode in ("light", "dark"):
        for page in ("assistant", "queue", "appearance", "raytrace"):
            _exercise_page(app, page, mode, dpr, screenshots)

    source_hashes = {
        name: hashlib.sha256((QML_DIR / name).read_bytes()).hexdigest()
        for name in ("AssistantDecisionWindow.qml", "ComputeQueueWindow.qml",
                     "FilmAppearanceWindow.qml", "RayTraceWindow.qml")
    }
    manifest = {
        "dpr": dpr,
        "theme_modes": ["light", "dark"],
        "capture_font": capture_font,
        "settings_root": str(qt_test_guard.QT_TEST_SETTINGS_ROOT),
        "network_factories": NETWORK_FACTORY_EVIDENCE,
        "source_sha256": source_hashes,
        "screenshots": screenshots,
    }
    (EVIDENCE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": str(EVIDENCE), "dpr": dpr,
                      "screenshots": len(screenshots), "capture_font": capture_font,
                      "source_sha256": source_hashes}, ensure_ascii=True))


def test_appearance_window_film_layout_ui() -> None:
    """Render and exercise the actual appearance window, without a worker/job.

    The software backend does not paint this project's custom QSG geometry.
    This test verifies real control/result-image pixels separately from the
    preserved camera gesture and scene-geometry contract; it never claims a
    hardware scenegraph or physical renderer pass.
    """
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    _load_capture_font(app)
    dpr = float(app.primaryScreen().devicePixelRatio())
    screenshots: list[dict[str, object]] = []
    geometry_evidence: list[dict[str, object]] = []

    def flush() -> None:
        app.processEvents()
        QTest.qWait(30)
        app.processEvents()

    def item(runtime, name: str):
        found = _find_item(runtime["window"].contentItem(), name)
        _assert(found is not None, f"appearance layout is missing {name}")
        return found

    def rect(control):
        top_left = control.mapToScene(QPointF(0, 0))
        return (top_left.x(), top_left.y(),
                top_left.x() + control.width(), top_left.y() + control.height())

    def click(runtime, control) -> None:
        point = control.mapToScene(QPointF(control.width() / 2, control.height() / 2))
        _assert(0 <= point.x() < runtime["window"].width()
                and 0 <= point.y() < runtime["window"].height(),
                f"{control.objectName()} click point is outside the window")
        QTest.mouseClick(runtime["window"], Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        flush()

    def camera_distance(controller) -> float:
        pose = controller.previewScene["camera"]
        return sum((p - t) ** 2 for p, t in zip(
            pose["position_m"], pose["target_m"])) ** 0.5

    for mode, density, font in (("light", 1.0, 1.0), ("dark", 1.0, 1.0),
                                ("dark", 1.25, 1.15)):
        label = mode if density == 1.0 else "dark_large_tokens"
        controller = AppearanceWindowController(
            _appearance_context(), root=EVIDENCE / f"appearance-layout-{label}")
        runtime = _create_page(app, "appearance", mode, None, controller,
                               snapshot=build_theme_snapshot(
                                   mode, density_scale=density, font_scale=font))
        runtime["app"] = app
        window = runtime["window"]
        sidebar = item(runtime, "appearanceParametersSidebar")
        scroll = item(runtime, "appearanceScroll_light")
        _assert(scroll is not None, "appearance sidebar has no real ScrollView")
        flickable = scroll.property("contentItem")
        initial_camera = controller.previewScene["camera"]
        try:
            window.requestActivate()
            flush()
            click(runtime, item(runtime, "appearanceCategory_light"))
            for width, height in ((1160, 820), (720, 520)):
                window.setWidth(width)
                window.setHeight(height)
                flush()
                viewport = item(runtime, "appearanceViewport")
                panel = item(runtime, "appearanceResultPanel")
                rail = item(runtime, "appearanceParametersRail")
                toolbar = item(runtime, "appearanceToolbar")
                _assert_header_geometry(runtime, "appearance")
                _assert_page_top_spacing(runtime, "appearance")
                _assert(rect(panel)[2] <= rect(rail)[0] + 0.5,
                        "appearance parameters overlap the central view")
                _assert(rect(toolbar)[3] < rect(panel)[1],
                        "appearance actions are not above the view")
                _assert(viewport.width() >= 240 and viewport.height() >= 200,
                        "appearance view is squeezed below its useful size")
                for name in ("appearanceParametersButton", "appearanceCancelButton",
                             "appearanceRunButton", "appearanceCameraZoomIn",
                             "appearanceCameraZoomOut", "appearanceCameraReset",
                             "appearanceCameraHelp"):
                    control = item(runtime, name)
                    left, top, right, bottom = rect(control)
                    _assert(0 <= left < right <= window.width() + 0.5
                            and 0 <= top < bottom <= window.height() + 0.5,
                            f"{name} leaves the window at {width}x{height}/{label}")
                hud = item(runtime, "appearanceCameraFloatingControls")
                _assert(rect(hud)[2] <= rect(viewport)[2] + 0.5
                        and rect(hud)[0] >= rect(viewport)[0] - 0.5,
                        "camera HUD does not fit the central view")
                # A real screen capture must contain more than one flat color.
                capture = window.grabWindow()
                _assert(not capture.isNull(), "appearance screenshot is blank")
                colors = {capture.pixelColor(x, y).rgba()
                          for y in range(0, capture.height(), 17)
                          for x in range(0, capture.width(), 17)}
                _assert(len(colors) > 12, "appearance controls did not paint real pixels")
                screenshots.append(_save_screenshot(
                    runtime, "appearance_layout", label, f"{width}_expanded"))
                # Collapse keeps a single instance, draft and scroll offset.
                flickable.setProperty("contentY", 24.0)
                flush()
                saved_y = float(flickable.property("contentY"))
                before_draft = controller.draft
                for _ in range(4):
                    item(runtime, "appearanceParametersButton").forceActiveFocus()
                    QTest.keyClick(window, Qt.Key.Key_F4)
                    flush()
                    _assert(not window.property("parametersExpanded")
                            and not sidebar.isVisible() and abs(rail.width() - 44) < 0.5,
                            "F4 did not collapse the current parameter sidebar")
                    _assert(panel.width() > width - 100,
                            "collapsed rail does not return space to the central view")
                    click(runtime, item(runtime, "appearanceParametersExpandButton"))
                    _assert(window.property("parametersExpanded") and sidebar.isVisible(),
                            "collapsed-rail button did not reopen the sidebar")
                    _assert(item(runtime, "appearanceParametersSidebar") is sidebar,
                            "sidebar was recreated across collapse")
                    _assert(abs(float(flickable.property("contentY")) - saved_y) < 0.5,
                            "collapse lost the sidebar scroll offset")
                    _assert(controller.draft == before_draft,
                            "collapse unexpectedly changed the controller draft")
                flickable.setProperty("contentY", 0.0)
                flush()
                click(runtime, item(runtime, "appearanceParametersCollapseButton"))
                _assert(not window.property("parametersExpanded"),
                        "sidebar's own collapse button is disconnected")
                screenshots.append(_save_screenshot(
                    runtime, "appearance_layout", label, f"{width}_collapsed"))
                click(runtime, item(runtime, "appearanceParametersButton"))
                _assert(window.property("parametersExpanded"),
                        "toolbar's parameter button is disconnected")

            # A focused editor follows its existing loss-of-focus commit boundary.
            click(runtime, item(runtime, "appearanceCategory_camera"))
            _assert(window.property("parametersExpanded"), "category navigation closed the rail")
            camera_x = item(runtime, "appearanceVector_camera_x")
            click(runtime, camera_x)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(window, Qt.Key.Key_9)
            QTest.keyClick(window, Qt.Key.Key_F4)
            flush()
            _assert(not window.property("parametersExpanded")
                    and float(controller.draft["camera_x"]) == 9.0,
                    "F4 hiding a focused field lost its editingFinished commit")
            click(runtime, item(runtime, "appearanceParametersExpandButton"))
            _assert(item(runtime, "appearanceVector_camera_x") is camera_x,
                    "reopening parameters recreated its editor")
            click(runtime, item(runtime, "appearanceCameraReset"))

            # Actual view buttons update the original camera owner.
            baseline_distance = camera_distance(controller)
            click(runtime, item(runtime, "appearanceCameraZoomIn"))
            zoomed = camera_distance(controller)
            _assert(zoomed < baseline_distance, "camera zoom-in button is disconnected")
            click(runtime, item(runtime, "appearanceCameraZoomOut"))
            _assert(camera_distance(controller) > zoomed,
                    "camera zoom-out button is disconnected")
            click(runtime, item(runtime, "appearanceCameraReset"))
            _assert(controller.previewScene["camera"] == initial_camera,
                    "camera reset no longer restores its launch pose")

            frame = item(runtime, "appearanceSceneFrame")
            center = frame.mapToScene(QPointF(frame.width() * 0.5, frame.height() * 0.6))
            point = QPoint(round(center.x()), round(center.y()))
            before = controller.draft
            QTest.mousePress(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, point)
            QTest.mouseMove(window, point + QPoint(22, 10), 20)
            QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier, point + QPoint(22, 10))
            flush()
            _assert(controller.draft != before and not controller._camera_gesture_active,
                    "scene orbit gesture did not commit the camera draft")
            before_target = controller.previewScene["camera"]["target_m"]
            QTest.mousePress(window, Qt.MouseButton.RightButton,
                             Qt.KeyboardModifier.NoModifier, point)
            QTest.mouseMove(window, point + QPoint(14, -8), 20)
            QTest.mouseRelease(window, Qt.MouseButton.RightButton,
                               Qt.KeyboardModifier.NoModifier, point + QPoint(14, -8))
            flush()
            _assert(controller.previewScene["camera"]["target_m"] != before_target,
                    "scene pan gesture no longer changes the target")
            distance = camera_distance(controller)
            wheel = QWheelEvent(QPointF(point), QPointF(window.mapToGlobal(point)),
                                QPoint(0, 0), QPoint(0, 120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.ScrollUpdate, False)
            QGuiApplication.sendEvent(window, wheel)
            QTest.qWait(180)
            flush()
            _assert(camera_distance(controller) < distance
                    and not controller._camera_gesture_active,
                    "scene wheel zoom did not commit after its debounce")
            click(runtime, item(runtime, "appearanceCameraReset"))

            # ThemeSection folds retain field objects and imported SPD data.
            click(runtime, item(runtime, "appearanceCategory_light"))
            scroll = item(runtime, "appearanceScroll_light")
            flickable = scroll.property("contentItem")
            area_section = item(runtime, "appearanceAreaLightSection")
            camera_section = item(runtime, "appearanceCameraSection")
            camera_section.setProperty("expanded", False)
            area_section.setProperty("expanded", True)
            flickable.setProperty("contentY", 0.0)
            flush()
            area_flag = item(runtime, "appearanceAreaEnabled")
            click(runtime, area_flag)
            _assert(controller.draft["area_enabled"], "area-light enable action is disconnected")
            _assert("面光源关闭" not in str(item(runtime, "appearanceAreaLightIndicator").property("text")),
                    "area-light view indicator stopped following the draft")
            csv_path = EVIDENCE / f"appearance-{label}-spectrum.csv"
            csv_path.write_text("wavelength_nm,value\n380,1\n780,1\n", encoding="utf-8")
            controller.loadSpectrum(QUrl.fromLocalFile(str(csv_path)).toString(), "area")
            flush()
            _assert(controller.canStart, "valid imported SPD no longer enables the render action")
            # The original FileDialog entry remains reachable and target-specific.
            import_button = item(runtime, "appearanceLoadAreaSpectrumButton")
            # Use the sidebar's existing Flickable to bring the real button into view.
            flickable.setProperty("contentY", float(flickable.property("contentY"))
                                  + max(0.0, rect(import_button)[3] - rect(scroll)[3] + 12))
            flush()
            click(runtime, import_button)
            dialog = _find_object(window, "appearanceSpectrumDialog")
            _assert(dialog is not None and dialog.property("visible")
                    and window.property("spectrumTarget") == "area",
                    "area-light SPD import no longer opens its original dialog")
            dialog.setProperty("visible", False)
            flush()
            area_section.setProperty("expanded", False)
            area_section.setProperty("expanded", True)
            flush()
            _assert(item(runtime, "appearanceAreaEnabled") is area_flag
                    and controller.draft["area_spectrum"]["path"] == str(csv_path),
                    "light-source fold recreated controls or discarded SPD")

            environment_section = item(runtime, "appearanceEnvironmentSection")
            area_section.setProperty("expanded", False)
            environment_section.setProperty("expanded", True)
            flickable.setProperty("contentY", 0.0)
            flush()
            environment_flag = item(runtime, "appearanceEnvironmentEnabled")
            flickable.setProperty("contentY", float(flickable.property("contentY"))
                                  + max(0.0, rect(environment_flag)[3] - rect(scroll)[3] + 12))
            flush()
            click(runtime, environment_flag)
            _assert(controller.draft["environment_enabled"]
                    and "已启用" in str(item(runtime, "appearanceEnvironmentIndicator").property("text")),
                    "environment enable action/indicator is disconnected")
            environment_import = item(runtime, "appearanceLoadEnvironmentSpectrumButton")
            flickable.setProperty("contentY", float(flickable.property("contentY"))
                                  + max(0.0, rect(environment_import)[3] - rect(scroll)[3] + 12))
            flush()
            click(runtime, environment_import)
            _assert(dialog.property("visible") and window.property("spectrumTarget") == "environment",
                    "environment SPD import opened with the wrong target")
            dialog.setProperty("visible", False)
            controller.loadSpectrum(QUrl.fromLocalFile(str(csv_path)).toString(), "environment")
            flush()
            _assert(controller.canStart and controller.draft["environment_spectrum"],
                    "environment spectrum import no longer survives layout changes")

            # Use a local synthetic result fixture, then check its actual pixels.
            preview_path = EVIDENCE / f"appearance-{label}-preview.png"
            preview = QImage(32, 24, QImage.Format.Format_RGB32)
            preview.fill(QColor("#29a05a"))
            _assert(preview.save(str(preview_path)), "could not save the local preview fixture")
            controller._last_result = {
                "image_path": str(preview_path), "width": 32, "height": 24,
                "wavelength_count": 2, "input_sha256": controller._draft_digest(),
            }
            controller.changed.emit()
            flush()
            click(runtime, item(runtime, "appearanceRenderTab"))
            image = item(runtime, "appearancePreviewImage")
            for _ in range(10):
                if float(image.property("progress")) >= 1.0:
                    break
                flush()
            _assert(window.property("previewMode") == "render" and image.isVisible(),
                    "completed-result view no longer opens")
            capture = window.grabWindow()
            midpoint = image.mapToScene(QPointF(image.width() / 2, image.height() / 2))
            pixel = capture.pixelColor(round(midpoint.x() * dpr), round(midpoint.y() * dpr))
            _assert(abs(pixel.red() - 0x29) <= 1 and abs(pixel.green() - 0xa0) <= 1,
                    f"result Image painted {pixel.name()}, expected the fixture result")
            controller.setField("seed", "17")
            flush()
            _assert(item(runtime, "appearanceStaleBadge").isVisible(),
                    "changing parameters lost the stale-result indication")
            controller._phase = "failed"
            controller._error = "Fixture render failed; keep the existing preview."
            controller.changed.emit()
            flush()
            _assert(image.isVisible() and controller.hasPreview,
                    "failed rerender discarded the previous completed image")
            item(runtime, "appearanceParametersButton").forceActiveFocus()
            flush()
            screenshots.append(_save_screenshot(runtime, "appearance_layout", label, "result_kept"))
            click(runtime, item(runtime, "appearanceSceneTab"))
            _assert(item(runtime, "appearanceScenePreview").isVisible(),
                    "switching back lost the geometry view")
            canvas = item(runtime, "appearanceSceneCanvas")
            geometry_evidence.append({
                "theme": label, "canvas_logical": [canvas.width(), canvas.height()],
                "vertex_counts": {key: len(value) for key, value in canvas._scene_vertices().items()},
                "software_custom_geometry_pixels": "not asserted; requires supported scenegraph backend",
            })
            _assert(not runtime["factory"].blocked_urls,
                    "appearance layout attempted network access")
            _assert(controller._process is None and not controller.running,
                    "layout QA unexpectedly launched a real render")
        finally:
            window.hide()
            controller.shutdown()
            window.deleteLater()
            runtime["component"].deleteLater()
            runtime["engine"].deleteLater()
            flush()

    manifest = {
        "dpr": dpr, "screenshots": screenshots, "geometry": geometry_evidence,
        "source_sha256": {name: hashlib.sha256((QML_DIR / name).read_bytes()).hexdigest()
                          for name in ("FilmAppearanceWindow.qml", "AppearanceScenePreview.qml")},
        "render_fixture": "local synthetic image, not an optical renderer result",
    }
    (EVIDENCE / "appearance-layout-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"appearance_layout_evidence": str(EVIDENCE),
                      "dpr": dpr, "screenshots": len(screenshots)}, ensure_ascii=True))
