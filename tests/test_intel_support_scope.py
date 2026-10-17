"""Task support presentation, using private state and no GPU execution."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, cast

import pytest
from PySide6.QtCore import QMetaObject, QObject, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
import shiboken6

from coat_sim.core import compute_device_probe as probe
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.theme_service import ThemeService


pytestmark = [pytest.mark.qt, pytest.mark.contract]
QML = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml"


class _LocalClient:
    def __init__(self, root):
        self.root = root

    def capabilities(self):
        return {"protocol_version": 1, "executors": ["process_recipe", "film_optimization"]}


def _rows(monkeypatch, *, available=True):
    monkeypatch.setattr(probe, "_run_probe", lambda *_args: {
        "available": available, "name": "Intel(R) Graphics", "reason": "" if available else "未安装运行时"})
    return probe.probe_local_compute_targets()


def _target(tmp_path, task, monkeypatch, rows):
    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    monkeypatch.setattr(ComputeTarget, "_probe_local_optional", staticmethod(
        lambda generation: (generation, rows, None, "optional")))
    return ComputeTarget(tmp_path, task, client_factory=_LocalClient)


def _settle(app, predicate):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def _dispose(target):
    target._executor.shutdown(wait=True, cancel_futures=True)
    shiboken6.delete(target)


def test_successful_runtime_probe_does_not_claim_complete_intel_acceptance(monkeypatch):
    rows = _rows(monkeypatch)
    assert rows[0]["backend"] == "CPU" and rows[0]["support_level"] == "supported"
    directml = rows[2]
    assert directml["available"] and directml["runtime_probe"] == "passed"
    assert directml["support_level"] == "experimental" and not directml["physical_id"]
    assert "遮板边界" in directml["support_note"]
    assert "不保证绑定 Intel" in directml["support_note"]
    assert "基础探测通过不代表完整任务验收" in directml["support_note"]


def test_directml_selection_keeps_limitations_and_rejects_cpu_only_algorithm(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    target = _target(tmp_path, "deposition", monkeypatch, _rows(monkeypatch))
    try:
        _settle(app, lambda: len(target.devices) == 3)
        target.setDeviceId("directml:default")
        assert target.canSubmit and target.deviceId == "directml:default"
        assert "不保证绑定 Intel" in cast(str, target.message)
        _client, selected = target.client_for_submit(algorithm="fast")
        assert selected["backend"] == "DirectML"
        target.setAlgorithm("reference")
        assert not target.canSubmit and target.deviceId == "directml:default"
        target.setAlgorithm("fast")
        assert target.canSubmit and "遮板边界" in cast(str, target.message)
    finally:
        _dispose(target)


def test_failed_intel_runtime_does_not_switch_cpu_preference(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    target = _target(tmp_path, "deposition", monkeypatch, _rows(monkeypatch, available=False))
    try:
        _settle(app, lambda: len(target.devices) == 3)
        target.setDeviceId("directml:default")
        assert target.deviceId == "cpu:default" and target.canSubmit
        directml = next(row for row in cast(list[dict[str, Any]], target.devices)
                        if row["id"] == "directml:default")
        assert not directml["available"] and directml["reason"] == "未安装运行时"
    finally:
        _dispose(target)


@pytest.mark.parametrize("task,required", (
    ("deposition", ("平面数值已有限验证", "遮板边界", "不保证选中 Intel")),
    ("film_optimization", ("CPU", "FP64/complex128", "两次超时", "尚未通过任务验收")),
    ("appearance", ("CPU", "Intel 核显外观 GPU 不可用", "没有 Intel 渲染后端")),
))
def test_support_summary_is_rendered_in_bounded_scrollable_device_popup(
        tmp_path, monkeypatch, task, required):
    app = QGuiApplication.instance() or QGuiApplication([])
    target = _target(tmp_path, task, monkeypatch, _rows(monkeypatch))
    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.5, density_scale=1.5)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    warnings: list[str] = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    component = QQmlComponent(engine)
    component.setData(b'''import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    required property var computeTarget
    width: 360; height: 260
    ComputeTargetSelector { x: 8; y: 8; target: computeTarget }
}''', QUrl.fromLocalFile(str(QML / "_intel_scope_test.qml")))
    window = None
    try:
        assert not component.isError(), component.errorString()
        window = component.createWithInitialProperties({"computeTarget": target})
        assert isinstance(window, QQuickWindow), component.errorString()
        window.show()
        QTest.qWait(40)
        button = window.findChild(QObject, "computeTargetButton")
        assert button is not None
        assert QMetaObject.invokeMethod(button, "click")
        app.processEvents()
        popup = window.findChild(QObject, "computeTargetPopup")
        label = window.findChild(QQuickItem, "computeTargetSupportSummary")
        scroll = window.findChild(QQuickItem, "computeTargetPopupScroll")
        assert popup is not None and popup.property("visible")
        assert label is not None and label.isVisible() and label.height() > 0
        assert all(text in label.property("text") for text in required)
        assert float(popup.property("x")) + float(popup.property("width")) <= window.width() + .5
        assert float(popup.property("y")) + float(popup.property("height")) <= window.height() + .5
        assert scroll is not None and scroll.property("contentHeight") > scroll.height()
        image = window.grabWindow()
        assert not image.isNull()
        assert image.save(str(tmp_path / f"intel-support-{task}.png"))
        QTest.keyClick(window, Qt.Key.Key_Escape)
        app.processEvents()
        assert not popup.property("visible")
        assert not warnings, warnings
    finally:
        if window is not None:
            cast(QQuickWindow, window).hide()
            shiboken6.delete(window)
        shiboken6.delete(component)
        shiboken6.delete(engine)
        theme.configure(mode="light", font_scale=1., density_scale=1.)
        _dispose(target)
        app.processEvents()
