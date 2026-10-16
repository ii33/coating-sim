"""Queue route contracts using isolated roots and fake scheduler transports."""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coat_sim import capability_cli
from coat_sim.core.api import CoatService
from coat_sim.core.capabilities import CapabilityError, CapabilityService, POLICIES
from coat_sim.core.film_design import new_design
from coat_sim.mcp import server as mcp_server
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.daemon import Scheduler
from coat_sim.scheduler.remote_client import RemoteTransport
from coat_sim.scheduler.store import SchedulerStore
from mcp.server.mcpserver.exceptions import ToolError


class _FakeSchedulerTransport:
    """An in-memory queue keyed by one root; it never starts or contacts a daemon."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple[str, dict]] = []
        self.jobs = {"same-job-id": {"job_id": "same-job-id", "state": "queued"}}

    def request(self, action: str, arguments: dict):
        self.calls.append((action, dict(arguments)))
        job_id = arguments.get("job_id")
        if action == "status":
            return {**self.jobs[job_id], "queue": self.name}
        if action == "cancel":
            self.jobs[job_id] = {"job_id": job_id, "state": "cancelled"}
            return {**self.jobs[job_id], "cancel_requested": True, "queue": self.name}
        if action == "health":
            return {"ok": True}
        raise AssertionError(f"unexpected fake scheduler action: {action}")


class CapabilityQueueRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="capability-queue-route-",
                                       dir=Path(__file__).resolve().parent)
        base = Path(self.temp.name)
        self.root_a = base / "project-a"
        self.root_b = base / "project-b"
        self.root_a.mkdir()
        self.root_b.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _capabilities(root: Path, scheduler):
        service = SimpleNamespace(root=root.resolve())
        return CapabilityService(service, scheduler=scheduler)

    def _cli_subprocess(self, root: Path, *arguments: str, stdin: str | None = None,
                        timeout: int = 45):
        env = os.environ.copy()
        env["TEMP"] = env["TMP"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-m", "coat_sim.capability_cli", "--root", str(root),
             *arguments],
            cwd=Path(__file__).resolve().parents[1], env=env, input=stdin,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=timeout, check=False,
        )

    def test_same_job_id_and_cancel_are_scoped_to_injected_root(self):
        transport_a = _FakeSchedulerTransport("project-a")
        transport_b = _FakeSchedulerTransport("project-b")
        client_a = SchedulerClient(self.root_a, transport=transport_a)
        client_b = SchedulerClient(self.root_b, transport=transport_b)
        capabilities_a = self._capabilities(self.root_a, client_a)
        capabilities_b = self._capabilities(self.root_b, client_b)

        status_a = capabilities_a.call("calculation_job_status", {"job_id": "same-job-id"})
        status_b = capabilities_b.call("calculation_job_status", {"job_id": "same-job-id"})
        self.assertEqual(status_a["queue"], "project-a")
        self.assertEqual(status_b["queue"], "project-b")

        cancelled = capabilities_a.call("cancel_calculation_job", {"job_id": "same-job-id"})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(transport_a.jobs["same-job-id"]["state"], "cancelled")
        self.assertEqual(transport_b.jobs["same-job-id"]["state"], "queued")
        self.assertEqual([action for action, _ in transport_a.calls], ["status", "cancel"])
        self.assertEqual([action for action, _ in transport_b.calls], ["status"])

    def test_mismatched_injected_client_is_rejected_without_dispatch(self):
        transport_b = _FakeSchedulerTransport("project-b")
        client_b = SchedulerClient(self.root_b, transport=transport_b)
        capabilities_a = self._capabilities(self.root_a, client_b)

        for operation, arguments in (
            ("calculation_job_status", {"job_id": "same-job-id"}),
            ("cancel_calculation_job", {"job_id": "same-job-id"}),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(CapabilityError) as caught:
                    capabilities_a.call(operation, arguments)
                self.assertEqual(caught.exception.code, "invalid_queue_route")
                self.assertEqual(caught.exception.effect, "none")
        self.assertEqual(transport_b.calls, [])

    def test_injected_fixture_without_root_keeps_legacy_queue_compatibility(self):
        class RootlessFakeScheduler:
            def status(self, job_id):
                return {"job_id": job_id, "state": "queued"}

        capabilities = self._capabilities(self.root_a, RootlessFakeScheduler())
        result = capabilities.call("calculation_job_status", {"job_id": "same-job-id"})
        self.assertEqual(result["state"], "queued")

    def test_catalog_marks_queue_route_without_expanding_assistant_permissions(self):
        service = SimpleNamespace(root=self.root_a.resolve())
        with patch("coat_sim.scheduler.SchedulerClient",
                   side_effect=AssertionError("catalog discovery must stay lazy")) as create_client, \
             patch("coat_sim.scheduler.client.subprocess.Popen",
                   side_effect=AssertionError("catalog discovery must not start a daemon")):
            default_catalog = CapabilityService(service).catalog()
        create_client.assert_not_called()
        default_operations = {item["name"]: item for item in default_catalog["operations"]}
        queue_names = {
            "calculation_queue_snapshot", "calculation_job_status", "find_calculation_job",
            "calculation_job_result", "cancel_calculation_job",
        }

        self.assertEqual(len(default_operations), 22)
        self.assertEqual(set(default_operations) & queue_names, queue_names)
        for name in queue_names:
            self.assertEqual(default_operations[name]["route"], {
                "selection": "default_local", "target": "local",
                "remote_selection_from_cli": False,
            })
        for name, operation in default_operations.items():
            if name not in queue_names:
                self.assertNotIn("route", operation)
        self.assertFalse(any(any(word in name.lower() for word in ("remote_auth", "submit", "approve"))
                             for name in default_operations))

        remote = RemoteTransport("https://queue.example.invalid")
        injected = CapabilityService(service, scheduler=SchedulerClient(self.root_a, transport=remote))
        injected_operations = {item["name"]: item for item in injected.catalog()["operations"]}
        for name in queue_names:
            self.assertEqual(injected_operations[name]["route"], {
                "selection": "injected", "target": "caller_owned",
                "remote_selection_from_cli": False,
            })
        self.assertNotIn("queue.example.invalid", json.dumps(injected.catalog()))

    def test_mcp_list_tools_route_description_is_repeatable_and_redacted(self):
        try:
            from mcp.server import MCPServer  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"installed MCP SDK does not expose MCPServer: {exc}")

        from coat_sim.mcp import server as mcp_server

        queue_names = {
            "calculation_queue_snapshot", "calculation_job_status", "find_calculation_job",
            "calculation_job_result", "cancel_calculation_job",
        }
        with patch.object(mcp_server, "service", SimpleNamespace(root=self.root_a.resolve())):
            first = asyncio.run(mcp_server.mcp.list_tools())
            second = asyncio.run(mcp_server.mcp.list_tools())

        first_by_name = {tool.name: tool for tool in first}
        second_by_name = {tool.name: tool for tool in second}
        self.assertTrue(queue_names <= first_by_name.keys())
        for name in queue_names:
            first_description = first_by_name[name].description or ""
            second_description = second_by_name[name].description or ""
            self.assertEqual(first_description, second_description)
            self.assertEqual(first_description.count("Queue route:"), 1)
            self.assertIn("cannot select a remote endpoint", first_description)
            self.assertNotIn("queue.example.invalid", first_description)

    def test_cli_rejects_remote_url_root_before_creating_or_starting_local_service(self):
        output = io.StringIO()
        with patch.object(capability_cli, "CoatService",
                          side_effect=AssertionError("remote root reached local service")), \
             patch("coat_sim.scheduler.client.subprocess.Popen",
                   side_effect=AssertionError("remote request tried to start a daemon")):
            exit_code = capability_cli.main(
                ["--root", "https://queue.example.invalid/project", "catalog"],
                stdout=output,
            )

        reply = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "unsupported_queue_route")
        self.assertEqual(reply["error"]["effect"], "none")

    def test_cli_default_queue_query_uses_local_root_with_fake_transport(self):
        transport = _FakeSchedulerTransport("local-project")
        output = io.StringIO()
        local_client = SchedulerClient(self.root_a, transport=transport)

        with patch("coat_sim.scheduler.SchedulerClient", return_value=local_client), \
             patch("coat_sim.scheduler.client.subprocess.Popen",
                   side_effect=AssertionError("test must not start a daemon")):
            exit_code = capability_cli.main(
                ["--root", str(self.root_a), "call", "calculation_job_status",
                 "--arguments", '{"job_id":"same-job-id"}'],
                service=SimpleNamespace(root=self.root_a.resolve()),
                stdout=output,
            )

        reply = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["queue"], "local-project")
        self.assertEqual(transport.calls, [("status", {"job_id": "same-job-id"})])

    def test_cli_preserves_unc_roots(self):
        unc_root = r"\\queuehost\share\project"
        created = []

        def fake_service(*, root, restrict_to_root):
            created.append((root, restrict_to_root))
            return SimpleNamespace(root=self.root_a.resolve())

        output = io.StringIO()
        with patch.object(capability_cli, "CoatService", side_effect=fake_service):
            exit_code = capability_cli.main(["--root", unc_root, "catalog"], stdout=output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(created, [(unc_root, True)])
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_cli_subprocess_and_real_mcp_share_root_success_and_error_contract(self):
        with TemporaryDirectory(prefix="capability-cli-mcp-") as temporary_root:
            parent = Path(temporary_root).resolve()
            root = parent / "project"
            root.mkdir()
            service = CoatService(root=root, restrict_to_root=True)
            self.assertEqual(service.root, root)
            core_names = {
                item["name"] for item in CapabilityService(service).catalog()["operations"]
            }
            self.assertEqual(core_names, set(POLICIES))
            design = new_design()
            with patch.object(mcp_server, "service", service):
                cli_catalog = self._cli_subprocess(root, "catalog")
                self.assertEqual(cli_catalog.returncode, 0, cli_catalog.stderr)
                self.assertEqual(
                    {item["name"] for item in json.loads(cli_catalog.stdout)["operations"]},
                    core_names,
                )

                cli_ok = self._cli_subprocess(
                    root, "call", "validate_film_design", "--arguments",
                    json.dumps({"data": design}, ensure_ascii=False),
                )
                self.assertEqual(cli_ok.returncode, 0, cli_ok.stderr)
                cli_value = json.loads(cli_ok.stdout)
                self.assertTrue(cli_value["ok"])

                mcp_ok = asyncio.run(mcp_server.mcp.call_tool(
                    "validate_film_design", {"data": design}))
                self.assertFalse(getattr(mcp_ok, "is_error", False), str(mcp_ok))
                mcp_value = getattr(mcp_ok, "structured_content", None)
                if mcp_value is None:
                    mcp_value = getattr(mcp_ok, "structuredContent", None)
                self.assertEqual(mcp_value, cli_value["result"])

                bad_arguments = {"path": "../outside.cfilm", "data": design,
                                 "overwrite": False}
                cli_error = self._cli_subprocess(
                    root, "call", "save_film_design", "--arguments",
                    json.dumps(bad_arguments, ensure_ascii=False),
                )
                self.assertEqual(cli_error.returncode, 2, cli_error.stderr)
                cli_error_value = json.loads(cli_error.stdout)
                self.assertFalse(cli_error_value["ok"])

                try:
                    mcp_error = asyncio.run(mcp_server.mcp.call_tool(
                        "save_film_design", bad_arguments))
                except ToolError as exc:
                    mcp_error_text = str(exc)
                else:
                    self.assertTrue(getattr(mcp_error, "is_error", False))
                    blocks = getattr(mcp_error, "content", ()) or ()
                    mcp_error_text = "\n".join(
                        block.text for block in blocks if getattr(block, "type", None) == "text")
                from coat_sim.core.capabilities import decode_capability_error
                decoded = decode_capability_error("save_film_design", mcp_error_text)
                self.assertEqual(decoded, cli_error_value["error"])
                self.assertFalse((parent / "outside.cfilm").exists())

    def test_cli_subprocess_rejects_duplicate_nonfinite_unknown_and_oversized_input(self):
        with TemporaryDirectory(prefix="capability-cli-inputs-") as temporary_root:
            root = Path(temporary_root).resolve()
            cases = (
                (("call", "film_materials", "--arguments",
                  '{"purpose":"film","purpose":"substrate"}'), None,
                 "invalid_arguments"),
                (("call", "save_film_design", "--arguments",
                  '{"path":"data/x.cfilm","data":{},"overwrite":NaN}'), None,
                 "invalid_arguments"),
                (("call", "not_registered", "--arguments", "{}"), None,
                 "unavailable_operation"),
            )
            for arguments, input_text, code in cases:
                with self.subTest(arguments=arguments):
                    result = self._cli_subprocess(root, *arguments, stdin=input_text)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["error"]["code"], code)

            oversized = self._cli_subprocess(
                root, "call", "film_materials", stdin=" " * (32 * 1024 * 1024 + 1))
            self.assertEqual(oversized.returncode, 2, oversized.stderr)
            self.assertEqual(json.loads(oversized.stdout)["error"]["code"],
                             "invalid_arguments")

    def test_capability_core_cli_and_mcp_import_without_ui_or_assistant_runtime(self):
        with TemporaryDirectory(prefix="capability-headless-") as temporary_root:
            root = Path(temporary_root).resolve()
            guard = r'''import builtins, runpy, sys
blocked = ("PySide6", "PySide2", "PyQt5", "PyQt6", "qtpy", "coat_sim.ui",
           "mcp", "fastmcp", "coat_sim.agent.runtime", "coat_sim.agent.modelclient")
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if any(name == prefix or name.startswith(prefix + ".") for prefix in blocked):
        raise AssertionError("forbidden import: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
mode, root = sys.argv[1], sys.argv[2]
if mode == "core":
    import coat_sim.core.capabilities
else:
    sys.argv = ["coat_sim.capability_cli", "--root", root, "catalog"]
    runpy.run_module("coat_sim.capability_cli", run_name="__main__")
assert not any(any(name == prefix or name.startswith(prefix + ".") for prefix in blocked)
               for name in sys.modules)
'''
            env = os.environ.copy()
            env["TEMP"] = env["TMP"] = str(root)
            env["PYTHONIOENCODING"] = "utf-8"
            for mode in ("core", "cli"):
                with self.subTest(mode=mode):
                    result = subprocess.run(
                        [sys.executable, "-c", guard, mode, str(root)],
                        cwd=Path(__file__).resolve().parents[1], env=env,
                        text=True, encoding="utf-8", errors="replace",
                        capture_output=True, timeout=45, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if mode == "cli":
                        self.assertTrue(json.loads(result.stdout)["ok"])

            mcp_guard = r'''import asyncio, builtins, sys
blocked = ("PySide6", "PySide2", "PyQt5", "PyQt6", "qtpy", "coat_sim.ui",
           "coat_sim.agent.runtime", "coat_sim.agent.modelclient")
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if any(name == prefix or name.startswith(prefix + ".") for prefix in blocked):
        raise AssertionError("forbidden import: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import coat_sim.mcp.server as server
async def probe():
    tools = await server.mcp.list_tools()
    assert any(tool.name == "input_schema" for tool in tools)
    result = await server.mcp.call_tool("input_schema", {"kind": "source"})
    assert not result.is_error
asyncio.run(probe())
assert not any(any(name == prefix or name.startswith(prefix + ".") for prefix in blocked)
               for name in sys.modules)
'''
            result = subprocess.run(
                [sys.executable, "-c", mcp_guard],
                cwd=Path(__file__).resolve().parents[1], env=env,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_injected_scheduler_capability_dispatches_to_isolated_daemon_store(self):
        store = SchedulerStore(self.root_a)
        job = store.submit({
            "protocol_version": 1,
            "executor": "probe",
            "parameters": {"delay_s": 1.0, "payload_bytes": 0, "crash": False,
                           "fail_save": False},
            "inputs": {},
        }, job_id="capability-existing-job")
        daemon = object.__new__(Scheduler)
        daemon.root = self.root_a
        daemon.runtime = self.root_a / "artifacts" / "runtime" / "scheduler"
        daemon.store = store
        daemon.condition = threading.Condition(threading.RLock())
        daemon.sequence = store.last_sequence
        daemon.index = {job["job_id"]: deepcopy(job)}
        daemon.event_buffer = deque(maxlen=1024)
        daemon.artifact_index = {}
        daemon.active = None
        daemon.stopping = threading.Event()
        daemon.recovery_error = None

        class DaemonTransport:
            def __init__(self, scheduler):
                self.daemon = scheduler
                self.requests: list[tuple[str, dict]] = []

            def request(self, action, arguments):
                self.requests.append((action, deepcopy(arguments)))
                return self.daemon.dispatch(action, arguments)

        transport = DaemonTransport(daemon)
        client = SchedulerClient(self.root_a, transport=transport)
        capabilities = self._capabilities(self.root_a, client)

        status = capabilities.call("calculation_job_status", {"job_id": job["job_id"]})
        self.assertEqual(status["state"], "queued")
        cancelled = capabilities.call("cancel_calculation_job", {"job_id": job["job_id"]})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(store.status(job["job_id"])["state"], "cancelled")
        self.assertEqual([action for action, _ in transport.requests], ["status", "cancel"])


if __name__ == "__main__":
    unittest.main()
