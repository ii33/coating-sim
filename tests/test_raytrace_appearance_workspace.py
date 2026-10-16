"""Appearance-first UI boundary tests, using a real synthetic saved asset.

The lifecycle worker is deliberately a cancellation fixture, never renderer or
GPU evidence. No scheduler daemon, user asset, or network is accessed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from tests import qt_test_guard  # noqa: F401
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QFont
from PySide6.QtTest import QTest

from coat_sim.ui.raytrace import RayTraceWindowController
from coat_sim.core.geometric_raytrace import trace
from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset

pytestmark = [pytest.mark.qt, pytest.mark.contract]


class NoQueue:
    def __getattr__(self, name):
        raise AssertionError(f"UI-only fixture unexpectedly accessed queue: {name}")


def _wait(app, condition, timeout=5):
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        app.processEvents()
        QTest.qWait(10)
    assert condition(), "Qt operation did not reach its expected boundary"


@pytest.fixture
def workspace(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    controller = RayTraceWindowController(root=tmp_path, queue_client=NoQueue())
    yield app, controller
    if controller.appearanceRunning:
        controller.appearanceController.cancelRender()
        _wait(app, lambda: not controller.appearanceRunning)
    controller.detach_observers()
    app.processEvents()


def _load_asset(app, controller):
    path = _write_synthetic_v7_asset(controller.root)
    controller.loadLayerAsset(str(path))
    _wait(app, lambda: not controller.geometricBusy)
    assert not controller.error, controller.error
    assert controller.appearanceController is not None
    return path


def _configure_cancel_worker(appearance, root):
    # Test lifecycle with an actual QProcess, never physical rendering.
    fixture = (
        "import argparse,pathlib,time; p=argparse.ArgumentParser(); "
        "p.add_argument('--input'); p.add_argument('--job-dir'); "
        "p.add_argument('--cancel-marker'); a=p.parse_args(); "
        "marker=pathlib.Path(a.cancel_marker); "
        "exec('while not marker.exists():\\n time.sleep(.02)')"
    )
    appearance._worker_command = [sys.executable, "-c", fixture]
    spectrum = root / "fixture-spd.csv"
    spectrum.write_text("wavelength_nm,value\n500,1\n600,1\n")
    appearance.loadSpectrum(str(spectrum), "environment")
    appearance.setFlag("environment_enabled", True)
    for key, value in (("wavelength_start_nm", "500"),
                       ("wavelength_end_nm", "600"),
                       ("wavelength_step_nm", "100")):
        appearance.setField(key, value)
    assert appearance.canStart, appearance.validationMessage


def test_empty_workspace_is_a_normal_state(workspace):
    _, controller = workspace
    assert controller.workspaceMode == "appearance"
    assert controller.phase == "empty"
    assert controller.appearanceController is None
    for _ in range(3):
        assert not controller.requestValid
        assert not controller.canRun
        assert controller.validationMessage == ""
        assert controller.surfaceIds == []
        assert controller.error == ""
    controller.setWorkspaceMode("geometric")
    assert controller.error == ""
    controller.startCalculation()
    assert "请求" in controller.error


def test_saved_asset_opens_real_appearance_without_request_json(workspace):
    app, controller = workspace
    path = _load_asset(app, controller)
    appearance = controller.appearanceController
    assert controller.workspaceMode == "appearance"
    assert controller.requestText == ""
    assert not controller.hasRequest
    assert controller.canOpenAppearance
    assert appearance.geometryThicknessMode == "film_design_snapshot"
    assert float(appearance.draft["geometry_thickness_mm"]) == 1
    assert Path(appearance._context["asset_path"]) == path
    assert appearance._context["captured_step_ids"] == ["synthetic-appearance-step"]
    assert not appearance.canStart  # A real scalar SPD is still required.
    assert "光谱" in appearance.validationMessage
    assert appearance._process is None
    assert not controller.documentDirty


def test_modes_reuse_draft_asset_and_controller(workspace):
    app, controller = workspace
    _load_asset(app, controller)
    appearance = controller.appearanceController
    appearance.setField("spp", "16")
    controller.newRequest()
    assert controller.workspaceMode == "geometric"
    assert controller.hasLayerAsset
    request = controller.requestText
    for _ in range(3):
        controller.openAppearance()
        assert controller.workspaceMode == "appearance"
        assert controller.appearanceController is appearance
        assert appearance.draft["spp"] == "16"
        controller.setWorkspaceMode("geometric")
        assert controller.requestText == request
    controller.setWorkspaceMode("invalid")
    assert controller.workspaceMode == "geometric"


def test_failed_asset_reload_preserves_working_appearance(workspace):
    app, controller = workspace
    path = _load_asset(app, controller)
    appearance = controller.appearanceController
    appearance.setField("camera_x", "1.25")
    controller.loadLayerAsset(str(controller.root / "missing.coatasset"))
    _wait(app, lambda: not controller.geometricBusy)
    assert controller.phase == "failed"
    assert "不存在" in controller.error
    assert controller.appearanceController is appearance
    assert controller.layerAssetPath == str(path)
    assert appearance.draft["camera_x"] == "1.25"


def test_new_valid_asset_reuses_controller_and_user_parameters(workspace):
    app, controller = workspace
    path = _load_asset(app, controller)
    appearance = controller.appearanceController
    appearance.setField("spp", "32")
    replacement = path.with_name("replacement-appearance.coatasset")
    replacement.write_bytes(path.read_bytes())
    controller.loadLayerAsset(str(replacement))
    _wait(app, lambda: not controller.geometricBusy)
    assert controller.error == ""
    assert controller.appearanceController is appearance
    assert appearance.draft["spp"] == "32"
    assert appearance._context["asset_path"] == str(replacement)


def test_geometric_asset_capability_limit_does_not_crash_workspace(workspace):
    app, controller = workspace
    _load_asset(app, controller)
    old = controller.appearanceController
    replacement = dict(controller._asset)
    replacement["appearance_context"] = dict(old._context)
    replacement["appearance_context"]["substrate_profile"] = {
        "outline": {"kind": "circle", "radius_mm": 2}}
    controller._receive_task(controller._active_token, "asset", replacement, "")
    assert controller.hasLayerAsset
    assert controller.appearanceController is None
    assert not controller.canOpenAppearance
    assert "平面" in controller.appearanceAvailabilityMessage
    assert controller.error == ""


def test_uniform_request_does_not_attach_unused_appearance_asset(workspace, monkeypatch):
    app, controller = workspace
    _load_asset(app, controller)
    appearance = controller.appearanceController
    controller.newRequest()
    controller.generateSourceSamples("point", "5", "0", "-100", "3")
    submissions = []

    class LocalQueueFixture:
        def submit(self, spec):
            submissions.append(spec)
            return {"job_id": "uniform-geometric-fixture"}

        def status(self, job_id):
            return {"state": "queued", "progress": {"stage": "fixture"}}

    def unexpected_artifact_lookup(*args):
        raise AssertionError("uniform request must not look up a captured-field artifact")

    monkeypatch.setattr("coat_sim.ui.raytrace._find_registered_deposition_bundle",
                        unexpected_artifact_lookup)
    controller._queue_client = LocalQueueFixture()
    controller.startCalculation()
    _wait(app, lambda: controller.queueJobId == "uniform-geometric-fixture")
    assert submissions[0]["inputs"] == {}
    assert controller.error == ""
    assert controller.hasLayerAsset and controller.appearanceController is appearance


def test_malformed_geometric_json_is_separate_from_empty_appearance(workspace):
    _, controller = workspace
    controller.setRequestText("{invalid")
    assert controller.error == ""
    controller.setWorkspaceMode("geometric")
    assert not controller.requestValid
    assert "JSON" in controller.error
    controller.setRequestText("")
    assert controller.validationMessage == ""
    assert controller.error == ""


def test_running_cancel_close_waits_for_own_child_process(workspace):
    app, controller = workspace
    path = _load_asset(app, controller)
    appearance = controller.appearanceController
    _configure_cancel_worker(appearance, controller.root)
    confirmations, closed = [], []
    controller.closeConfirmationRequested.connect(lambda: confirmations.append(True))
    controller.closeReady.connect(lambda: closed.append(True))
    appearance.startRender()
    _wait(app, lambda: appearance.phase == "running")
    assert controller.busy and controller.appearanceRunning
    assert controller.phase == "running"
    controller.loadLayerAsset(str(controller.root / "missing.coatasset"))
    controller.newRequest()
    assert controller.layerAssetPath == str(path)
    assert not controller.hasRequest
    controller.setWorkspaceMode("geometric")
    assert controller.appearanceRunning  # Navigation cannot cancel a renderer.
    controller._error = ""  # Clear the deliberately rejected replacement.
    controller.openAppearance()
    assert appearance.errorText == ""
    controller.requestClose()
    assert confirmations == [True] and not closed
    controller.resolveClose(False)
    assert not appearance._cancel_path.exists()
    controller.resolveClose(True)
    assert appearance.phase == "cancelling" and not closed
    _wait(app, lambda: bool(closed))
    assert not controller.appearanceRunning
    assert closed == [True]
    assert not appearance.hasPreview  # Cancellation fixture is not render proof.


def test_closing_then_reopening_constructs_clean_workspace(workspace, tmp_path):
    app, controller = workspace
    _load_asset(app, controller)
    closed = []
    controller.closeReady.connect(lambda: closed.append(True))
    controller.requestClose()
    assert closed == [True]
    controller.detach_observers()
    reopened = RayTraceWindowController(root=tmp_path, queue_client=NoQueue())
    try:
        assert reopened.workspaceMode == "appearance"
        assert reopened.phase == "empty" and reopened.error == ""
        assert reopened.appearanceController is None
    finally:
        reopened.detach_observers()


def test_actual_qml_optical_workspace_layout(tmp_path):
    """Capture real controls at the process DPR, with a genuine saved asset.

    Qt software Quick cannot rasterize custom QSGGeometryNode slab geometry;
    those pixels and physical rendering remain outside this test's claim.
    """
    from tests.test_utility_windows_compact_chrome_ui import (
        _create_page, _find_item, _find_object,
    )
    root = Path(__file__).resolve().parents[1]
    scale = os.environ.get("QT_SCALE_FACTOR", "1")
    evidence = root / "build" / "qa_raytrace_remake" / f"scale_{scale}"
    evidence.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Noto Sans CJK SC"))
    dpr = app.primaryScreen().devicePixelRatio()
    assert abs(dpr - float(scale)) < .02
    screenshots, checks = [], []

    def flush():
        app.processEvents()
        QTest.qWait(35)
        app.processEvents()

    def find(window, name):
        item = _find_item(window.contentItem(), name)
        assert item is not None, name
        return item

    def rect(item):
        origin = item.mapToScene(QPointF(0, 0))
        return (origin.x(), origin.y(), origin.x() + item.width(),
                origin.y() + item.height())

    def click(window, item):
        p = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        assert item.isVisible() and 0 <= p.x() < window.width()
        assert 0 <= p.y() < window.height()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(round(p.x()), round(p.y())))
        flush()

    def capture(window, theme, state):
        image = window.grabWindow()
        assert not image.isNull()
        filename = f"raytrace_{theme}_{int(window.width())}x{int(window.height())}_{state}.png"
        path = evidence / filename
        assert image.save(str(path))
        assert len({image.pixelColor(x, y).rgba()
                    for x in range(0, image.width(), 19)
                    for y in range(0, image.height(), 19)}) > 12
        screenshots.append({"file": filename, "state": state,
                            "logical": [window.width(), window.height()],
                            "pixels": [image.width(), image.height()],
                            "dpr": dpr, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    for theme in ("light", "dark"):
        project = tmp_path / theme
        project.mkdir()
        controller = RayTraceWindowController(root=project, queue_client=NoQueue())
        runtime = _create_page(app, "raytrace", theme, None, controller)
        window = runtime["window"]
        try:
            window.requestActivate()
            for width, height in ((1160, 820), (720, 520)):
                window.resize(width, height)
                flush()
                window.setProperty("appearanceSidebarExpanded", True)
                flush()
                empty = find(window, "rayTraceAppearanceEmptyState")
                rail = find(window, "rayTraceAppearanceWorkflowSidebar")
                primary = find(window, "rayTraceAppearanceChooseAssetButton")
                assert controller.error == "" and primary.isEnabled()
                assert empty.height() >= 350
                assert rect(primary)[2] <= rect(rail)[0]
                assert rect(rail)[2] <= width + .5
                nav = find(window, "rayTraceEmptyCategoryNavigation")
                assert rect(nav)[1] <= rect(rail)[1] + 70
                stack = find(window, "rayTraceEmptyCategoryStack")
                for index, category in enumerate(("asset", "light", "camera", "compute")):
                    button = find(window, "rayTraceEmptyCategory_" + category)
                    click(window, button)
                    assert window.property("selectedCategory") == category
                    assert stack.property("currentIndex") == index
                    visible = [find(window, "rayTraceEmptyCategoryPanel_" + name).isVisible()
                               for name in ("asset", "light", "camera", "compute")]
                    assert visible == [candidate == index for candidate in range(4)]
                click(window, find(window, "rayTraceEmptyCategory_asset"))
                asset_action = find(window, "rayTraceAppearanceSidebarAssetButton")
                assert rect(asset_action)[1] <= rect(stack)[1] + 85
                capture(window, theme, "empty")
                primary.forceActiveFocus()
                flush()
                QTest.keyClick(window, Qt.Key.Key_F4)
                flush()
                assert not window.property("appearanceSidebarExpanded")
                assert abs(rail.width() - 44) < .5
                capture(window, theme, "empty_collapsed")
                checks.append(f"{theme}/{width}: empty info, asset action, F4, rail bounds")

            # The ordinary asset action opens its real local FileDialog. Escape
            # rejects it and returns focus to the still-visible empty action.
            click(window, find(window, "rayTraceAppearanceChooseAssetButton"))
            dialog = _find_object(window, "rayTraceLayerAssetDialog")
            assert dialog is not None and dialog.property("visible")
            dialog_window = next((candidate for candidate in app.allWindows()
                                  if candidate.isVisible() and candidate is not window
                                  and "逐层" in candidate.title()), window)
            QTest.keyClick(dialog_window, Qt.Key.Key_Escape)
            flush()
            assert not dialog.property("visible")
            _wait(app, lambda: window.activeFocusItem() is
                  find(window, "rayTraceAppearanceChooseAssetButton"), timeout=2)

            path = _load_asset(app, controller)
            flush()
            workspace = find(window, "rayTraceAppearanceWorkspace")
            appearance = controller.appearanceController
            for width, height in ((1160, 820), (720, 520)):
                window.resize(width, height)
                workspace.setProperty("parametersExpanded", True)
                flush()
                panel = find(window, "appearanceResultPanel")
                rail = find(window, "appearanceParametersRail")
                toolbar = find(window, "appearanceToolbar")
                viewport = find(window, "appearanceViewport")
                assert rect(panel)[2] <= rect(rail)[0] + .5
                assert rect(toolbar)[3] < rect(panel)[1]
                assert viewport.width() >= 240 and viewport.height() >= 260
                assert toolbar.height() <= 70
                assert not find(window, "rayTraceContentToolbar").isVisible()
                nav = find(window, "appearanceCategoryNavigation")
                assert rect(nav)[1] <= rect(rail)[1] + 160
                assert workspace.property("selectedCategory") == "asset"
                for name in ("appearanceSelectAssetButton", "appearanceWorkspaceMode",
                             "appearanceParametersButton", "appearanceRunButton",
                             "appearanceCancelButton", "appearanceCameraReset"):
                    control = find(window, name)
                    left, top, right, bottom = rect(control)
                    assert 0 <= left < right <= width + .5, (name, rect(control))
                    assert 0 <= top < bottom <= height + .5, (name, rect(control))
                capture(window, theme, "asset_loaded")
                category_stack = find(window, "appearanceCategoryStack")
                for index, category in enumerate(("asset", "light", "camera", "compute")):
                    click(window, find(window, "appearanceCategory_" + category))
                    assert workspace.property("selectedCategory") == category
                    assert category_stack.property("currentIndex") == index
                    visible = [find(window, "appearanceScroll_" + name).isVisible()
                               for name in ("asset", "light", "camera", "compute")]
                    assert visible == [candidate == index for candidate in range(4)]
                    capture(window, theme, "category_" + category)
                click(window, find(window, "appearanceCategory_asset"))
                # Exercise a genuine overflowing page, rather than assigning
                # an out-of-bounds offset to the short asset/camera pages.
                click(window, find(window, "appearanceCategory_light"))
                light_scroll = find(window, "appearanceScroll_light")
                light_flick = light_scroll.property("contentItem")
                assert float(light_flick.property("contentHeight")) > light_scroll.height() + 24
                light_flick.setProperty("contentY", 24.0)
                flush()
                light_y = float(light_flick.property("contentY"))
                click(window, find(window, "appearanceCategory_compute"))
                click(window, find(window, "appearanceCategory_light"))
                assert find(window, "appearanceScroll_light") is light_scroll
                assert abs(float(light_flick.property("contentY")) - light_y) < .5
                find(window, "appearanceParametersButton").forceActiveFocus()
                flush()
                QTest.keyClick(window, Qt.Key.Key_F4)
                flush()
                assert not workspace.property("parametersExpanded")
                assert abs(rail.width() - 44) < .5
                capture(window, theme, "asset_collapsed")
                click(window, find(window, "appearanceParametersExpandButton"))
                assert workspace.property("parametersExpanded")
                assert abs(float(light_flick.property("contentY")) - light_y) < .5
                click(window, find(window, "appearanceCategory_asset"))
                checks.append(f"{theme}/{width}: asset scene, single toolbar, no pane overlap, F4")

            # Repeated mode navigation keeps the very same mounted workspace,
            # editors and transient sidebar state; edits reach the real owner.
            window.resize(1160, 820)
            workspace.setProperty("parametersExpanded", True)
            flush()
            click(window, find(window, "appearanceCategory_camera"))
            camera_scroll = find(window, "appearanceScroll_camera")
            camera_flick = camera_scroll.property("contentItem")
            camera_flick.setProperty("contentY", 0.0)
            flush()
            saved_camera_y = float(camera_flick.property("contentY"))
            camera = find(window, "appearanceVector_camera_x")
            click(window, camera)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(window, Qt.Key.Key_9)
            QTest.keyClick(window, Qt.Key.Key_F4)
            flush()
            assert appearance.draft["camera_x"] == "9"
            assert not workspace.property("parametersExpanded")
            saved_camera_y = float(camera_flick.property("contentY"))
            for _ in range(3):
                controller.setWorkspaceMode("geometric")
                flush()
                controller.openAppearance()
                flush()
                assert find(window, "rayTraceAppearanceWorkspace") is workspace
                assert find(window, "appearanceVector_camera_x") is camera
                assert workspace.property("selectedCategory") == "camera"
                assert find(window, "appearanceScroll_camera") is camera_scroll
                assert abs(float(camera_flick.property("contentY")) - saved_camera_y) < .5
                assert not workspace.property("parametersExpanded")
            # Failure preserves the current real asset/context and input owner.
            controller.loadLayerAsset(str(project / "missing.coatasset"))
            _wait(app, lambda: not controller.geometricBusy)
            flush()
            assert controller.appearanceController is appearance
            assert controller.layerAssetPath == str(path)
            assert window.property("statusSeverity") == "error"
            capture(window, theme, "asset_reload_error")
            controller._error = ""
            controller.changed.emit()

            # Exercise the retained geometric model with a tiny actual trace,
            # then load its validated result through the public async entry.
            controller.newRequest()
            assert controller.generateSourceSamples("point", "5", "0", "-100", "3") == ""
            result_path = project / "geometric-result.json"
            result_path.write_text(json.dumps(trace(json.loads(controller.requestText))))
            controller.loadResult(str(result_path))
            _wait(app, lambda: not controller.geometricBusy)
            assert controller.hasResult and len(controller.displayPaths) == 5
            for width, height in ((1160, 820), (720, 520)):
                window.resize(width, height)
                flush()
                window.setProperty("sidebarExpanded", True)
                flush()
                view = find(window, "rayTraceViewPanel")
                rail = find(window, "rayTraceInputPanel")
                paths = find(window, "rayTracePathCanvas")
                stack = find(window, "rayTraceResultTabsPanel")
                click(window, find(window, "rayTracePathsTab"))
                assert rect(view)[2] <= rect(rail)[0] + .5
                assert paths.width() >= 260 and paths.height() >= 220
                assert stack.width() >= view.width() - 32
                assert rect(stack)[0] >= rect(view)[0] and rect(stack)[2] <= rect(view)[2]
                assert rect(find(window, "rayTracePathSummary"))[3] <= rect(view)[3] - 4
                heading = find(window, "rayTracePathHeading")
                wave = find(window, "rayTraceDisplayWavelength")
                assert rect(heading)[2] <= rect(wave)[0]
                for name in ("rayTraceSourceCount", "rayTraceSourceX",
                             "rayTraceSourceZ", "rayTraceSourceExtent"):
                    assert find(window, name).width() >= 64
                assert not find(window, "rayTraceAdvancedJsonSection").property("expanded")
                for name in ("rayTraceWorkspaceMode", "rayTraceRequestMenuButton",
                             "rayTraceResultMenuButton", "rayTraceRunButton"):
                    control = find(window, name)
                    assert rect(control)[2] <= width + .5 and rect(control)[3] <= height + .5
                capture(window, theme, "geometric_paths")
                click(window, find(window, "rayTraceSpectrumTab"))
                assert stack.property("currentIndex") == 1 and not paths.isVisible()
                capture(window, theme, "geometric_energy")
                click(window, find(window, "rayTraceDetectorTab"))
                assert stack.property("currentIndex") == 2
                histogram = find(window, "rayTraceDetectorHistogramCanvas")
                assert histogram.isVisible() and histogram.height() >= 220
                capture(window, theme, "geometric_detector")
                controller.setWorkspaceMode("appearance")
                flush()
                controller.setWorkspaceMode("geometric")
                flush()
                assert stack.property("currentIndex") == 2
            # Running/Close/Return/cancel-close use the actual embedded buttons
            # and parent window, with a clearly identified cancellation fixture.
            controller._saved_snapshot = controller._current_snapshot()
            controller.openAppearance()
            window.resize(1160, 820)
            workspace.setProperty("parametersExpanded", True)
            _configure_cancel_worker(appearance, project)
            flush()
            click(window, find(window, "appearanceRunButton"))
            _wait(app, lambda: appearance.phase == "running")
            flush()
            assert window.property("statusSeverity") == "busy"
            capture(window, theme, "running_lifecycle_fixture")
            window.close()
            flush()
            close_dialog = _find_object(window, "rayTraceCloseDialog")
            assert close_dialog is not None and close_dialog.property("visible")
            capture(window, theme, "close_running_fixture")
            click(window, find(window, "rayTraceCloseReturnButton"))
            assert window.isVisible() and appearance.running
            window.close()
            flush()
            click(window, find(window, "rayTraceCloseDiscardButton"))
            _wait(app, lambda: not window.isVisible())
            assert not appearance.running and controller._closed
            reopened = RayTraceWindowController(root=project, queue_client=NoQueue())
            reopen_runtime = _create_page(app, "raytrace", theme, None, reopened)
            reopen_window = reopen_runtime["window"]
            try:
                reopen_window.resize(720, 520)
                flush()
                assert reopened.workspaceMode == "appearance"
                assert reopened.error == "" and reopened.appearanceController is None
                capture(reopen_window, theme, "reopened_empty")
                reopen_window.close()
                _wait(app, lambda: not reopen_window.isVisible())
            finally:
                reopen_window.hide()
                reopened.detach_observers()
                reopen_window.deleteLater()
                reopen_runtime["component"].deleteLater()
                reopen_runtime["engine"].deleteLater()
                flush()
            checks.append(f"{theme}: same-instance mode round trip, focused edit commit, failed reload retains asset, actual geometric result")
            checks.append(f"{theme}: real asset dialog Escape/focus and cancellation-only QProcess Run/Close/Return/cancel-close")
            assert not runtime["factory"].blocked_urls
        finally:
            window.hide()
            controller.detach_observers()
            window.deleteLater()
            runtime["component"].deleteLater()
            runtime["engine"].deleteLater()
            flush()
    files = ("coat_sim/ui/raytrace.py", "coat_sim/ui/appearance.py", "coat_sim/ui/qml/RayTraceWindow.qml",
             "coat_sim/ui/qml/FilmAppearanceWindow.qml", "coat_sim/ui/qml/AppearanceWorkspace.qml")
    (evidence / "manifest.json").write_text(json.dumps({
        "dpr": dpr, "backend": "Qt offscreen/software", "checks": checks,
        "source_sha256": {file: hashlib.sha256((root / file).read_bytes()).hexdigest() for file in files},
        "screenshots": screenshots,
        "limits": ["custom QSG slab pixels unsupported by Qt software backend",
                   "local scalar renderer runtime missing", "no physical/GPU render asserted"],
    }, indent=2, ensure_ascii=False) + "\n")
