"""Cold optimizer opening stays responsive while optional resources load."""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QGuiApplication
import shiboken6

from coat_sim.ui import film_optimization_quick_controller as module
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import window_runtime
from tests.test_film_optimization_target_sampling import _snapshot


pytestmark = pytest.mark.qt


def _wait(app: QGuiApplication, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "timed out waiting for queued Qt result"


def test_slow_probe_does_not_block_open_and_catalog_is_read_once(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _, design = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    reads = []
    original_records = service.materials._records

    def slow_probe():
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(3)
        return "", True

    def counted_records():
        reads.append(threading.get_ident())
        return original_records()

    monkeypatch.setattr(module, "_scipy_runtime_availability", slow_probe)
    monkeypatch.setattr(service.materials, "_records", counted_records)
    started = time.perf_counter()
    controller = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    try:
        assert time.perf_counter() - started < 0.5
        assert controller.loading
        assert not controller.canStart
        assert "加载" in controller.statusText
        controller.startLoading()
        controller.startLoading()
        assert entered.wait(2)
        app.processEvents()
        assert controller.loading
        release.set()
        _wait(app, lambda: not controller.loading)
        assert len(reads) == 1 and reads[0] != main_thread
        assert controller._trf_callback_available
    finally:
        release.set()
        controller.shutdown(1000)
        shiboken6.delete(controller)


def test_slow_material_read_keeps_gui_responsive(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _, design = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_records = service.materials._records

    def slow_records():
        entered.set()
        assert release.wait(3)
        return original_records()

    monkeypatch.setattr(module, "_scipy_runtime_availability", lambda: ("", True))
    monkeypatch.setattr(service.materials, "_records", slow_records)
    controller = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    try:
        controller.startLoading()
        assert entered.wait(2)
        app.processEvents()
        assert controller.loading
        assert not controller.canStart
        release.set()
        _wait(app, lambda: not controller.loading)
        assert controller._structure_material_options
    finally:
        release.set()
        controller.shutdown(1000)
        shiboken6.delete(controller)


def test_qml_window_displays_loading_before_catalog_finishes(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _, design = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_records = service.materials._records

    def slow_records():
        entered.set()
        assert release.wait(3)
        return original_records()

    monkeypatch.setattr(module, "_scipy_runtime_availability", lambda: ("", True))
    monkeypatch.setattr(service.materials, "_records", slow_records)
    controller = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    try:
        with window_runtime(app, controller) as (window, warnings):
            assert "加载" in window.property("statusText")
            controller.startLoading()
            assert entered.wait(2)
            app.processEvents()
            assert window.isVisible() and "加载" in window.property("statusText")
            release.set()
            _wait(app, lambda: not controller.loading)
            assert not warnings
    finally:
        release.set()
        controller.shutdown(1000)
        shiboken6.delete(controller)


def test_close_during_slow_load_and_reopen_ignores_late_result(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _, design = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow_probe():
        entered.set()
        assert release.wait(3)
        return "", True

    monkeypatch.setattr(module, "_scipy_runtime_availability", slow_probe)
    first = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    first.startLoading()
    assert entered.wait(2)
    closed = []
    first.closeConfirmed.connect(lambda: closed.append(True))
    first.requestClose()
    assert closed
    shiboken6.delete(first)
    release.set()
    assert QThreadPool.globalInstance().waitForDone(3000)
    app.processEvents()
    second = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    try:
        second.startLoading()
        _wait(app, lambda: not second.loading)
        assert second._structure_material_error == ""
    finally:
        second.shutdown(1000)
        shiboken6.delete(second)


def test_material_failure_is_visible_and_blocks_start(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _, design = fixture(tmp_path)
    monkeypatch.setattr(module, "_scipy_runtime_availability", lambda: ("", True))

    def fail_read():
        raise OSError("injected catalog failure")

    monkeypatch.setattr(service.materials, "_records", fail_read)
    controller = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    try:
        controller.startLoading()
        _wait(app, lambda: not controller.loading)
        assert "injected catalog failure" in controller.statusText
        assert not controller.canStart
        assert not controller.startOptimization()
    finally:
        controller.shutdown(1000)
        shiboken6.delete(controller)


def test_configuration_failure_finishes_loading_and_can_reopen(tmp_path, monkeypatch):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, _, design = fixture(tmp_path)
    monkeypatch.setattr(module, "_scipy_runtime_availability", lambda: ("", True))
    first = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)

    def fail_restore(*, reset_missing):
        raise OSError("injected configuration failure")

    first._restore_session_configuration = fail_restore
    try:
        first.startLoading()
        _wait(app, lambda: not first.loading)
        assert "injected configuration failure" in first.statusText
        assert not first.canStart
    finally:
        first.shutdown(1000)
        shiboken6.delete(first)
    second = FilmOptimizationQuickController(
        _snapshot(service, design), defer_expensive_load=True)
    try:
        second.startLoading()
        _wait(app, lambda: not second.loading)
        assert not second._initial_load_failed
    finally:
        second.shutdown(1000)
        shiboken6.delete(second)
