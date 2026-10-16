"""GPU compute controls are bound to the real appearance controller.

This is UI contract coverage only. It does not claim a CUDA device or a
physically qualified GPU render.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from tests import qt_test_guard  # noqa: F401
from PySide6.QtCore import QMetaObject, QObject, Qt
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtTest import QTest

from coat_sim.ui.appearance import AppearanceWindowController
from tests.test_utility_windows_compact_chrome_ui import (
    _appearance_context, _create_page, _find_item,
)


pytestmark = [pytest.mark.qt, pytest.mark.contract]


def _item(window, name):
    item = _find_item(window.contentItem(), name)
    assert item is not None, name
    return item


def test_gpu_compute_controls_follow_controller_selection(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    controller = AppearanceWindowController(_appearance_context(), root=tmp_path)
    runtime = _create_page(app, "appearance", "light", None, controller)
    window = runtime["window"]
    try:
        target_button = _item(window, "computeTargetButton")
        target_popup = window.findChild(QObject, "computeTargetPopup")
        assert target_popup is not None and not target_popup.property("visible")
        QMetaObject.invokeMethod(target_button, "click")
        app.processEvents()
        assert target_popup.property("visible")
        assert target_popup.property("modal")
        assert window.findChild(QObject, "computeHostSelector") is not None
        assert window.findChild(QObject, "computeDeviceSelector") is not None
        probe_requests = []
        monkeypatch.setattr(controller, "_probe_gpu_async", lambda: probe_requests.append(True))
        cuda_probe = _item(window, "computeCudaProbeButton")
        assert cuda_probe.isVisible()
        assert not next(row for row in controller.computeTarget.devices
                        if row["id"] == "cuda:default")["available"]
        QMetaObject.invokeMethod(cuda_probe, "click")
        app.processEvents()
        assert probe_requests == [True]
        assert not next(row for row in controller.computeTarget.devices
                        if row["id"] == "cuda:default")["available"]
        QTest.keyClick(window, Qt.Key.Key_Escape)
        app.processEvents()
        assert not target_popup.property("visible")

        workspace = _item(window, "appearanceWorkspace")
        workspace.setProperty("selectedCategory", "compute")
        app.processEvents()

        # Force a deterministic unavailable-device result; this is a UI
        # fixture, not a probe or GPU execution claim.
        controller._gpu_capability_state = "unavailable"
        controller._gpu_capability_message = "CUDA 不可用：测试设备未连接。"
        controller.changed.emit()

        backend = _item(window, "appearanceBackendSelector")
        precision = _item(window, "appearanceGpuPrecisionSelector")
        capability = _item(window, "appearanceGpuCapabilityMessage")
        warning = _item(window, "appearanceGpuQualificationWarning")
        assert backend.property("currentIndex") == 0
        assert not precision.isVisible()
        assert not capability.isVisible()
        assert not warning.isVisible()

        controller.setBackend("gpu")
        app.processEvents()
        assert backend.property("currentIndex") == 1
        assert precision.isVisible()
        assert capability.isVisible()
        assert "NVIDIA CUDA" in str(capability.property("text"))
        assert "测试设备未连接" in str(capability.property("text"))
        assert warning.isVisible()
        assert "仅供预览" in str(warning.property("text"))
        assert precision.property("currentIndex") == 1
        assert not precision.isEnabled()
        assert not _item(window, "appearanceRunButton").isEnabled()

        controller._gpu_capability_state = "available"
        controller._gpu_capability_message = "NVIDIA CUDA 已通过测试设备执行检测。"
        controller.changed.emit()
        app.processEvents()
        assert precision.isEnabled()

        controller.setPrecision("float32")
        app.processEvents()
        assert precision.property("currentIndex") == 0

        controller.setBackend("cpu")
        app.processEvents()
        assert backend.property("currentIndex") == 0
        assert not precision.isVisible()
        assert not warning.isVisible()

        image_path = tmp_path / "fixture-preview.png"
        image = QImage(1, 1, QImage.Format.Format_ARGB32)
        image.fill(0xFFFFFFFF)
        assert image.save(str(image_path))
        controller._last_result = {
            "backend": "gpu_cuda", "image_path": image_path,
            "width": 1, "height": 1, "wavelength_count": 1,
            "input_sha256": "f" * 64,
        }
        controller.changed.emit()
        workspace.setProperty("previewMode", "render")
        app.processEvents()
        assert controller.backend == "cpu"  # a retained GPU image stays labelled
        assert _item(window, "appearancePreviewGpuWarning").isVisible()
        workspace.setProperty("previewMode", "scene")

        controller._phase = "running"
        controller._stage = "rendering"
        controller._status = "正在渲染波长帧。"
        controller._progress_fraction = 0.5
        controller._eta_seconds = 90.0
        controller.changed.emit()
        app.processEvents()
        progress = _item(window, "appearanceProgressBar")
        assert _item(window, "appearanceProgressPanel").isVisible()
        assert not progress.property("indeterminate")
        assert abs(progress.property("value") - 0.5) < 1e-6
        assert "逐波长渲染" in str(_item(window, "appearanceProgressStage").property("text"))
        assert "1 分 30 秒" in str(_item(window, "appearanceProgressEta").property("text"))
        assert _item(window, "appearanceCancelButton").isEnabled()

        controller._progress_fraction = -1.0
        controller._eta_seconds = -1.0
        controller.changed.emit()
        app.processEvents()
        assert progress.property("indeterminate")
        assert "待估" in str(_item(window, "appearanceProgressEta").property("text"))
        controller._phase = "cancelling"
        controller.changed.emit()
        app.processEvents()
        assert "取消" in str(_item(window, "appearanceProgressEta").property("text"))
        assert not _item(window, "appearanceCancelButton").isEnabled()
    finally:
        controller._phase = "ready"
        controller.changed.emit()
        window.close()
        controller.shutdown()
        app.processEvents()


def test_gpu_progress_and_preview_have_visible_bindings():
    # These controls must stay attached to controller state rather than a
    # timer-driven or fabricated percentage/ETA in QML.
    from pathlib import Path

    qml = (Path(__file__).resolve().parents[1]
           / "coat_sim/ui/qml/AppearanceWorkspace.qml").read_text(encoding="utf-8")
    assert "controller.progressFraction" in qml
    assert "controller.etaSeconds" in qml
    assert "controller.stage" in qml
    assert "controller.cancelRender()" in qml
    assert 'controller.previewBackend === "gpu"' in qml
    assert 'controller.gpuCapabilityMessage' in qml


def test_circle_boundary_approximation_is_visible(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    context = _appearance_context()
    context["substrate_profile"] = {
        "outline": {"kind": "circle", "radius_mm": 5.0},
        "surface": {"kind": "flat"},
    }
    controller = AppearanceWindowController(context, root=tmp_path)
    runtime = _create_page(app, "appearance", "light", None, controller)
    window = runtime["window"]
    try:
        warning = _item(window, "appearanceCircleBoundaryWarning")
        assert warning.isVisible()
        assert "捕获节点" in str(warning.property("text"))
        assert "并非边界实测值" in str(warning.property("text"))
    finally:
        window.close()
        controller.shutdown()
        app.processEvents()
