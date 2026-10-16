"""True CPU film optimization through its Qt entry point and private HTTP."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import time
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from coat_sim.scheduler.remote_client import make_remote_client
from coat_sim.ui import compute_target
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.film_structure_fixtures import fixture, request
from tests.test_film_optimization_needle import _objective
from tests.test_remote_server import running


class _Session:
    def __init__(self, design):
        self.design = design
        self.design_revision = 19
        self.path = ""

    def snapshot(self):
        return SimpleNamespace(design_revision=self.design_revision,
                               design=self.design, path=self.path)

    def subscribe(self, callback):
        self._subscriber = callback
        return lambda: setattr(self, "_subscriber", None)


class _Source:
    def __init__(self, service, design):
        self.service = service
        self.session = _Session(design)
        self.pendingEditEpoch = 3

    def schematicLayers(self, side):
        return [{"id": row["id"], "physicalNmValue": row["thickness"]["value"]}
                for row in self.session.design.get(side + "_layers", [])]


def test_remote_film_qt_real_cpu_result_and_requested_binding(tmp_path):
    app = QApplication.instance() or QApplication([])
    service, _library, design = fixture(tmp_path / "desktop")
    frozen_request = request(design, [_objective()], bounds=(90.0, 110.0),
                             max_iterations=2, max_evaluations=4)
    source = _Source(service, design)
    with running(tmp_path / "private-server") as server:
        client = make_remote_client(
            server.url, tmp_path / "private-client-cache",
            token="ephemeral-test-operator-token", allow_loopback_http=True,
            device_id="cpu:default")

        class Target:
            def client_for_submit(self, *, client_factory=None):
                return client, {"id": "cpu:default", "backend": "CPU", "available": True}

        with patch.object(compute_target, "ComputeTarget", lambda *_args, **_kwargs: Target()):
            controller = FilmOptimizationQuickController(source)
            controller._build_request = lambda **_kwargs: frozen_request
            try:
                started = time.monotonic()
                assert controller.startOptimization()
                assert time.monotonic() - started < 2
                deadline = time.monotonic() + 35
                while controller.busy and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.05)
                app.processEvents()
                assert not controller.busy, controller.error
                assert controller.resultTerminalState == "已完成", controller.error
                assert controller._result is not None
                final = client.status(controller.queueJobId)
                assert final["state"] == "succeeded"
                assert final["requested_device_id"] == "cpu:default"
                # The typed film result has no independently observed backend.
                assert "executed_device_id" not in final
            finally:
                controller.shutdown(1000)
                controller.deleteLater()
                app.processEvents()
