"""True CPU deposition through the Qt entry point and private HTTP service."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import time
from unittest.mock import patch

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from coat_sim.core.api import CoatService
from coat_sim.core import data_files
from coat_sim.scheduler.remote_client import make_remote_client
from coat_sim.ui import main, settings
from tests.design_test_fixtures import copy_typed_examples
from tests.test_remote_server import running


def test_remote_main_real_loopback_cpu_submit_result_and_attestation(tmp_path):
    app = QApplication.instance() or QApplication([])
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir()
    copy_typed_examples(desktop_root)

    class LocalDiscoveryOnly:
        def __init__(self, root):
            assert Path(root) == desktop_root
        def capabilities(self):
            # The test's local host row never starts a local daemon.
            return {"protocol_version": 1, "executors": ["process_recipe"]}

    with (patch.object(data_files, "PROJECT_ROOT", desktop_root),
          patch.object(data_files, "DATA_ROOT", desktop_root / "data"),
          patch.object(settings, "SETTINGS_PATH", desktop_root / "config/ui_settings.json"),
          patch.object(main, "SchedulerClient", LocalDiscoveryOnly)):
        service = CoatService(desktop_root)
        bridge = main.MainBridge(service)
        try:
            bridge.chooseFile("process", QUrl.fromLocalFile(
                str(desktop_root / "examples/process_example.cproc")))
            bridge.setDevice("CPU")
            bridge._values.update(grid_size="21", time_steps="12")
            bridge._time_basis = "time_steps"
            bridge._fast_cycle = False
            with running(tmp_path / "private-server") as remote_service:
                client = make_remote_client(
                    remote_service.url, tmp_path / "private-client-cache",
                    token="ephemeral-test-operator-token", allow_loopback_http=True,
                    device_id="cpu:default")
                target = {"id": "cpu:default", "backend": "CPU", "available": True}
                with patch.object(bridge._compute_target, "client_for_submit",
                                  return_value=(client, target)):
                    started = time.monotonic()
                    bridge.calculate()
                    assert time.monotonic() - started < 2
                deadline = time.monotonic() + 40
                while bridge.busy and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.05)
                app.processEvents()
                assert not bridge.busy, bridge.status
                assert bridge.hasResult, bridge.status
                rows = client.snapshot()["jobs"]
                assert len(rows) == 1
                final = client.status(rows[0]["job_id"])
                assert final["state"] == "succeeded"
                assert final["requested_device_id"] == "cpu:default"
                assert final["executed_device_id"] == "cpu:default"
        finally:
            bridge.detach_observers()
            bridge._compute_target._executor.shutdown(wait=True, cancel_futures=True)
