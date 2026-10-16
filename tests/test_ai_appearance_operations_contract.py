"""Assistant-facing boundary for local appearance render operations.

Runtime routing tests exercise the real BudgetAndTools and ProjectGateway with
an in-process MCP peer. The registered-service test uses a synthetic v7 asset,
isolated project root, the repository's SchedulerStore test transport, and the
existing CPU renderer for a tiny synthetic result. It does not start a scheduler
daemon. Run the service integration only after the shared appearance contract is
frozen.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
# Load imported factory bindings before any package-level test patch.
from coat_sim.scheduler import submission as _scheduler_submission

from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import (
    BudgetAndTools,
    EXECUTION_TOOLS,
    MAIN_TOOLS,
    READ_TOOLS,
    SUBMIT_TOOLS,
)
from coat_sim.agent.storage import AssistantStore
from coat_sim.agent.appearance_adapter import appearance_output_artifacts
from coat_sim.agent.tool_dispatch import ProjectGateway


_REQUEST = {
    "asset_path": "data/assets/synthetic-appearance.coatasset",
    "asset_sha256": "a" * 64,
    "render_inputs": {
        "wavelength_nm": [500.0, 600.0],
        "camera": {},
        "area_source": {},
        "environment_spectrum": {},
        "resolution": {"width": 1, "height": 1, "spp": 1, "seed": 0},
    },
}
_OUTPUT_MEDIA_TYPES = {
    "display": "image/png",
    "manifest": "application/json",
    "radiance_spectrum": "application/x-npy",
    "xyz": "application/x-npy",
    "linear_srgb": "application/x-npy",
}


def _appearance_result(*, job_id="appearance-job-fixture"):
    render_dir = Path(tempfile.gettempdir()) / f"assistant-appearance-{job_id}" / "render"
    basenames = {
        "display": "display.png",
        "manifest": "manifest.json",
        "radiance_spectrum": "radiance_spectrum.npy",
        "xyz": "xyz.npy",
        "linear_srgb": "linear_srgb.npy",
    }
    return {
        "kind": "appearance_render",
        "schema_version": 1,
        "job_id": job_id,
        "input_digest": "b" * 64,
        "request_sha256": "b" * 64,
        "context_sha256": "b" * 64,
        "job_sha256": "b" * 64,
        "input_sha256": "b" * 64,
        "resolution": {"width": 1, "height": 1},
        "wavelength_nm": [500.0, 600.0],
        "display_exposure": 0.01,
        "result_file": str(render_dir / "result.json"),
        "sha256": "b" * 64,
        "outputs": {
            name: {
                "path": str(render_dir / basenames[name]),
                "sha256": "b" * 64,
                "bytes": 16,
                "media_type": media_type,
            }
            for name, media_type in _OUTPUT_MEDIA_TYPES.items()
        },
    }

_ESTIMATE_SCHEMA = {
    "type": "object", "properties": {"request": {"type": "object"}},
    "required": ["request"], "additionalProperties": False,
}
_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "request": {"type": "object"},
        "client_request_id": {"type": ["string", "null"]},
    },
    "required": ["request"], "additionalProperties": False,
}
_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "estimate_id": {"type": "string"},
        "session_id": {"type": "string"},
        "request_id": {"type": "string"},
    },
    "required": ["estimate_id"], "additionalProperties": False,
}
_INSPECT_SCHEMA = {
    "type": "object", "properties": {"asset_path": {"type": "string"}},
    "required": ["asset_path"], "additionalProperties": False,
}
_JOB_RESULT_SCHEMA = {
    "type": "object", "properties": {"job_id": {"type": "string"}},
    "required": ["job_id"], "additionalProperties": False,
}


class _Tool:
    def __init__(self, name, input_schema):
        self.name = name
        self.description = "synthetic appearance operation fixture"
        self.input_schema = input_schema


class _FakeClient:
    def __init__(self, *, cancel_submit_once=False, result_payload=None,
                 inspection_payload=None, job_status=None):
        self.calls = []
        self.cancel_submit_once = cancel_submit_once
        self.result_payload = deepcopy(result_payload)
        self.inspection_payload = deepcopy(inspection_payload) or {
            "schema_version": 1,
            "root": "C:/fixture/project",
            "asset_path": "C:/fixture/project/data/assets/synthetic.coatasset",
            "asset_sha256": "a" * 64,
            "asset_bytes": 128,
        }
        self.job_status = deepcopy(job_status) or {
            "job_id": "appearance-job-fixture", "kind": "appearance_render",
            "state": "succeeded",
        }
        self.tools = [
            _Tool("inspect_appearance_asset", _INSPECT_SCHEMA),
            _Tool("estimate_appearance_render", _ESTIMATE_SCHEMA),
            _Tool("submit_appearance_render", _SUBMIT_SCHEMA),
            _Tool("request_compute_confirmation", _CONFIRM_SCHEMA),
            _Tool("calculation_job_result", _JOB_RESULT_SCHEMA),
            _Tool("apply_appearance_render", {"type": "object", "properties": {}}),
        ]

    async def list_tools(self):
        return self.tools

    async def call_tool_mcp(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "inspect_appearance_asset":
            value = self.inspection_payload
        elif name == "calculation_job_status":
            value = self.job_status
        elif name == "calculation_job_result":
            value = self.result_payload
        if name == "submit_appearance_render" and self.cancel_submit_once:
            self.cancel_submit_once = False
            raise asyncio.CancelledError()
        if name == "submit_appearance_render":
            value = {"state": "prepared", "estimate_id": "appearance-estimate-fixture",
                     "needs_release_request": True, "estimated_s": None}
        elif name == "request_compute_confirmation":
            value = {"state": "awaiting_confirmation",
                     "estimate_id": arguments["estimate_id"],
                     "release_policy": "manual", "needs_user_confirmation": True}
        elif name == "estimate_appearance_render":
            value = {"estimate_id": "appearance-estimate-read-fixture",
                     "estimated_s": None, "sample_count": 0}
        return SimpleNamespace(structured_content=value, content=[], is_error=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _RegisteredServerPeer:
    """In-process peer that dispatches through the actual registered MCP server."""

    def __init__(self, server):
        self.server = server
        self.calls = []

    async def list_tools(self):
        return await self.server.mcp.list_tools()

    async def call_tool_mcp(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return await self.server.mcp.call_tool(name, arguments)


class _Ledger:
    def __init__(self):
        self.intents = []
        self.results = []
        self.jobs = []

    def intent(self, *args):
        self.intents.append(args)

    def result(self, *args):
        self.results.append(args)

    def update_job(self, *args):
        self.jobs.append(args)


class _RuntimeHarness(BudgetAndTools):
    """Initialize only the fields consumed by the actual tool wrapper."""

    def __init__(self, *, role="main", client=None):
        self.role = role
        self.mode = "operate"  # These fixtures exercise the explicitly operational path.
        self.config = {"task_mode": "operate"}
        self.client = client
        self.shared = {"tool_calls": 0, "gateways": {}, "gateway_guard": {}}
        self.limits = {"tool_calls": 40}
        self.tool_lock = asyncio.Lock()
        self.session = "synthetic-appearance-session"
        self.request = "synthetic-appearance-turn"
        self._ui_temporary = tempfile.TemporaryDirectory(prefix="runtime-ui-authority-")
        self.store = AssistantStore(Path(self._ui_temporary.name) / "state.sqlite3")
        with self.store._db() as database:
            database.execute("INSERT INTO conversations VALUES(?,?,?)", (self.session, "Synthetic UI run", 0))
        self.store.set_ui_session_mode(self.session, "operate")
        self.store.bind_ui_request(self.session, self.request, "operate")
        self.ledger = _Ledger()
        self.last_poll = {}
        self.writer = SimpleNamespace(redact=lambda value: str(value))
        self.gui_calculations = set()
        self.artifacts = []
        self.archive = SimpleNamespace(project=lambda value, **_kwargs: deepcopy(value))

    def progress(self, *_args, **_kwargs):
        return None

    async def _await_tool(self, awaitable, *_args, **_kwargs):
        return await awaitable

    def _observe_tool(self, *_args, **_kwargs):
        return None

    def _bounded_result(self, _name, _arguments, result):
        return result

    def _register_background(self, *_args, **_kwargs):
        return None

    def _upsert_artifact(self, candidate, *, step_id=""):
        self.artifacts.append((deepcopy(candidate), step_id))
        return None

    def _record_issue(self, *_args, **_kwargs):
        return None


class AssistantAppearanceRuntimeContractTests(unittest.TestCase):
    def test_registered_schema_and_fixed_role_permissions(self):
        self.assertIn("estimate_appearance_render", READ_TOOLS)
        self.assertIn("inspect_appearance_asset", READ_TOOLS)
        self.assertNotIn("inspect_appearance_asset", SUBMIT_TOOLS)
        self.assertIn("inspect_appearance_asset", EXECUTION_TOOLS["simulation"])
        self.assertNotIn("inspect_appearance_asset", EXECUTION_TOOLS["design"])
        self.assertNotIn("inspect_appearance_asset", EXECUTION_TOOLS["correction"])
        self.assertIn("estimate_appearance_render", MAIN_TOOLS)
        self.assertIn("submit_appearance_render", MAIN_TOOLS)
        self.assertIn("submit_appearance_render", SUBMIT_TOOLS)
        self.assertIn("estimate_appearance_render", EXECUTION_TOOLS["simulation"])
        self.assertIn("submit_appearance_render", EXECUTION_TOOLS["simulation"])
        for role in ("design", "correction", "windows"):
            self.assertNotIn("submit_appearance_render", EXECUTION_TOOLS[role])
        self.assertNotIn("apply_appearance_render", MAIN_TOOLS)
        self.assertNotIn("apply_appearance_render", EXECUTION_TOOLS["simulation"])

        from coat_sim.mcp import server
        registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        estimate = registered["estimate_appearance_render"]
        submit = registered["submit_appearance_render"]

        def schema(tool):
            return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")

        estimate_schema, submit_schema = schema(estimate), schema(submit)
        self.assertEqual(estimate_schema["required"], ["request"])
        self.assertEqual(set(estimate_schema["properties"]), {"request"})
        self.assertFalse(estimate_schema["additionalProperties"])
        self.assertEqual(submit_schema["required"], ["request"])
        self.assertEqual(set(submit_schema["properties"]), {"request", "client_request_id"})
        self.assertFalse(submit_schema["additionalProperties"])

    def test_main_path_injects_identity_release_origin_and_preflights_approval(self):
        client = _FakeClient()
        meter = _RuntimeHarness(client=client)
        gateway = ProjectGateway(client, MAIN_TOOLS, READ_TOOLS, str,
                                 shared=meter.shared["gateway_guard"])

        async def project_action(envelope):
            return await gateway.execute(envelope["tool_name"], envelope["arguments"])

        catalog = asyncio.run(gateway.catalog(["inspect_appearance_asset",
                                               "estimate_appearance_render",
                                               "submit_appearance_render",
                                               "apply_appearance_render"]))
        available = {tool["name"] for tool in catalog["tools"]}
        self.assertEqual(available, {"inspect_appearance_asset", "estimate_appearance_render",
                                     "submit_appearance_render"})
        self.assertIn("apply_appearance_render", catalog["unavailable"])

        result = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="appearance-call-1"),
            tool_def=None,
            args={"tool_name": "submit_appearance_render", "arguments": {"request": _REQUEST}},
            handler=project_action))
        self.assertEqual(result["state"], "awaiting_confirmation")
        self.assertEqual([name for name, _ in client.calls], [
            "submit_appearance_render", "request_compute_confirmation"])
        submit_name, submit_arguments = client.calls[0]
        self.assertEqual(submit_name, "submit_appearance_render")
        self.assertEqual(submit_arguments["request"], _REQUEST)
        self.assertEqual(submit_arguments["client_request_id"],
                         "synthetic-appearance-turn:appearance-call-1")
        confirmation_name, confirmation_arguments = client.calls[1]
        self.assertEqual(confirmation_name, "request_compute_confirmation")
        self.assertEqual(confirmation_arguments, {
            "estimate_id": "appearance-estimate-fixture",
            "session_id": "synthetic-appearance-session",
            "request_id": "synthetic-appearance-turn",
        })
        self.assertEqual(meter.ledger.intents[0][2:], (
            "submit_appearance_render", "synthetic-appearance-turn:appearance-call-1"))

        malformed = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="appearance-call-2"),
            tool_def=None,
            args={"tool_name": "submit_appearance_render", "arguments": {
                "request": _REQUEST, "approve": True,
            }}, handler=project_action))
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["error"]["code"], "invalid_arguments")
        self.assertEqual(malformed["error"]["effect"], "none")
        self.assertEqual(len(client.calls), 2)

        for suffix in ("estimate-1", "estimate-2"):
            estimate = asyncio.run(meter.wrap_tool_execute(
                None, call=SimpleNamespace(tool_name="project_action", tool_call_id=suffix),
                tool_def=None,
                args={"tool_name": "estimate_appearance_render", "arguments": {
                    "request": _REQUEST,
                }}, handler=project_action))
            self.assertIsNone(estimate["estimated_s"])
        estimate_calls = [call for call in client.calls if call[0] == "estimate_appearance_render"]
        self.assertEqual(len(estimate_calls), 2)
        self.assertTrue(all("client_request_id" not in args for _, args in estimate_calls))

    def test_simulation_delegate_can_submit_and_other_domains_are_denied(self):
        client = _FakeClient()
        meter = _RuntimeHarness(role="execute_simulation", client=client)
        result = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="submit_appearance_render", tool_call_id="sim-call"),
            tool_def=None, args={"request": _REQUEST}, handler=lambda _args: None))
        self.assertEqual(result["state"], "awaiting_confirmation")
        self.assertEqual(client.calls[0][1]["client_request_id"],
                         "synthetic-appearance-turn:sim-call")
        self.assertEqual(client.calls[1][1]["session_id"], meter.session)
        self.assertEqual(client.calls[1][1]["request_id"], meter.request)
        gateway = meter.shared["gateways"]["execute_simulation"]
        catalog = asyncio.run(gateway.catalog(["inspect_appearance_asset",
                                               "estimate_appearance_render",
                                               "submit_appearance_render",
                                               "apply_appearance_render"]))
        names = {tool["name"] for tool in catalog["tools"]}
        self.assertEqual(names, {"inspect_appearance_asset", "estimate_appearance_render",
                                 "submit_appearance_render"})

        for role in ("execute_design", "execute_correction", "execute_windows"):
            denied_client = _FakeClient()
            denied = _RuntimeHarness(role=role, client=denied_client)
            blocked = asyncio.run(denied.wrap_tool_execute(
                None, call=SimpleNamespace(tool_name="submit_appearance_render", tool_call_id="denied"),
                tool_def=None, args={"request": _REQUEST}, handler=lambda _args: None))
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "unavailable_tool")
            self.assertFalse(denied_client.calls)

    def test_dispatched_cancellation_blocks_replay_after_new_tool_call_id(self):
        client = _FakeClient(cancel_submit_once=True)
        meter = _RuntimeHarness(role="execute_simulation", client=client)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(meter.wrap_tool_execute(
                None, call=SimpleNamespace(tool_name="submit_appearance_render", tool_call_id="call-old"),
                tool_def=None, args={"request": _REQUEST}, handler=lambda _args: None))
        self.assertEqual(len(client.calls), 1)
        first_id = client.calls[0][1]["client_request_id"]
        self.assertEqual(len(meter.shared["gateway_guard"]["unknown"]), 1)

        blocked = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="submit_appearance_render", tool_call_id="call-new"),
            tool_def=None, args={"request": _REQUEST}, handler=lambda _args: None))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["effect"], "unknown")
        self.assertTrue(blocked["dispatch_blocked"])
        self.assertEqual(blocked["client_request_id"], first_id)
        self.assertEqual(len(client.calls), 1)


class AssistantAppearanceOutputRuntimeTests(unittest.TestCase):
    def test_inspect_uses_read_only_gateway_and_keeps_returned_digest(self):
        inspection = {
            "schema_version": 1,
            "root": "C:/fixture/project",
            "asset_path": "C:/fixture/project/data/assets/synthetic.coatasset",
            "asset_sha256": "c" * 64,
            "asset_bytes": 128,
        }
        client = _FakeClient(inspection_payload=inspection)
        gateway = ProjectGateway(client, MAIN_TOOLS, READ_TOOLS, str)
        result = asyncio.run(gateway.execute("inspect_appearance_asset", {
            "asset_path": "data/assets/synthetic.coatasset",
        }))
        self.assertEqual(result, inspection)
        self.assertEqual(client.calls, [("inspect_appearance_asset", {
            "asset_path": "data/assets/synthetic.coatasset",
        })])

    def test_result_wrapper_maps_only_shared_verified_outputs(self):
        from coat_sim.core import appearance_result

        result_payload = _appearance_result()
        client = _FakeClient(result_payload=result_payload)
        meter = _RuntimeHarness(client=client)
        gateway = ProjectGateway(client, MAIN_TOOLS, READ_TOOLS, str,
                                 shared=meter.shared["gateway_guard"])

        async def project_action(envelope):
            return await gateway.execute(envelope["tool_name"], envelope["arguments"])

        self.assertTrue(appearance_result.is_appearance_render_result(result_payload))
        result = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="result-call"),
            tool_def=None,
            args={"tool_name": "calculation_job_result", "arguments": {
                "job_id": "appearance-job-fixture",
            }}, handler=project_action))

        self.assertEqual(result["outputs"], result_payload["outputs"])
        self.assertEqual([candidate for candidate, _step in meter.artifacts],
                         list(result_payload["outputs"].values()))
        self.assertEqual(client.calls, [("calculation_job_result", {
            "job_id": "appearance-job-fixture",
        })])

    def test_invalid_or_wrong_job_outputs_are_not_attached_and_other_files_stay_generic(self):
        from coat_sim.core import appearance_result

        invalid_appearance = _appearance_result()
        invalid_appearance["files"] = [{"path": "data/tmp/untrusted.png"}]
        client = _FakeClient(result_payload=invalid_appearance)
        meter = _RuntimeHarness(client=client)
        gateway = ProjectGateway(client, MAIN_TOOLS, READ_TOOLS, str,
                                 shared=meter.shared["gateway_guard"])

        async def project_action(envelope):
            return await gateway.execute(envelope["tool_name"], envelope["arguments"])

        with patch.object(appearance_result, "is_appearance_render_result", return_value=False):
            asyncio.run(meter.wrap_tool_execute(
                None, call=SimpleNamespace(tool_name="project_action", tool_call_id="bad-result"),
                tool_def=None, args={"tool_name": "calculation_job_result", "arguments": {
                    "job_id": "appearance-job-fixture",
                }}, handler=project_action))
        self.assertEqual(meter.artifacts, [])

        wrong_job = _appearance_result(job_id="different-job")
        self.assertEqual(appearance_output_artifacts(
            wrong_job, expected_job_id="appearance-job-fixture"), ())

        generic_files = [{"path": "data/tmp/ordinary.json", "reference": "data/tmp/ordinary.json"}]
        generic_client = _FakeClient(result_payload={"kind": "ordinary_result",
                                                      "files": generic_files})
        generic_meter = _RuntimeHarness(client=generic_client)
        generic_gateway = ProjectGateway(generic_client, MAIN_TOOLS, READ_TOOLS, str,
                                         shared=generic_meter.shared["gateway_guard"])

        async def generic_action(envelope):
            return await generic_gateway.execute(envelope["tool_name"], envelope["arguments"])

        asyncio.run(generic_meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="generic-result"),
            tool_def=None, args={"tool_name": "calculation_job_result", "arguments": {
                "job_id": "ordinary-job",
            }}, handler=generic_action))
        self.assertEqual([candidate for candidate, _step in generic_meter.artifacts], generic_files)

    def test_background_completion_preserves_outputs_and_upserts_them(self):
        from coat_sim.agent.runtime import _background_completion_context
        from coat_sim.core import appearance_result

        result_payload = _appearance_result()
        client = _FakeClient(result_payload=result_payload)
        meter = _RuntimeHarness(client=client)
        store = SimpleNamespace(request_was_stopped=lambda _session, _request: False)
        notification = {
            "job_id": "appearance-job-fixture",
            "origin": {"session_id": meter.session, "request_id": meter.request},
        }
        self.assertTrue(appearance_result.is_appearance_render_result(result_payload))
        context = asyncio.run(_background_completion_context(
            notification, client=client, meter=meter, store=store, session=meter.session))

        self.assertEqual(context["result"]["outputs"], result_payload["outputs"])
        self.assertEqual([candidate for candidate, _step in meter.artifacts],
                         list(result_payload["outputs"].values()))
        self.assertEqual([name for name, _arguments in client.calls], [
            "calculation_job_status", "calculation_job_result",
        ])


class RegisteredAppearanceServiceContractTests(unittest.TestCase):
    """Run only after the shared asset-fingerprint change has been frozen."""

    def setUp(self):
        from tests.test_appearance_compute_entrypoints import (
            _SyntheticSchedulerTransport,
            _render_inputs,
            _write_synthetic_v7_asset,
        )
        from coat_sim.core.api import CoatService
        from coat_sim.core.compute_tasks import ComputeTaskStore

        self.temp = tempfile.TemporaryDirectory(prefix="assistant-appearance-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.asset_path = _write_synthetic_v7_asset(self.root)
        self.asset_identity = None
        self.request = None
        self.service = CoatService(self.root)
        self.compute_store = ComputeTaskStore(self.root)
        self.transport = _SyntheticSchedulerTransport(self.root)
        self.assistant = AssistantStore(self.root / "artifacts/assistant/state.sqlite3")
        self._render_inputs = _render_inputs
        self.session_id = self.assistant.new_session("synthetic appearance assistant test")
        self.request_id = "synthetic-appearance-request"
        self.assistant.set_ui_session_mode(self.session_id, "operate")
        self.assistant.bind_ui_request(self.session_id, self.request_id, "operate")
        self.assistant.record_event(self.session_id, self.request_id, "progress",
                                    {"stage": "received", "mode": "operate"})
        self.allowed_freeze_commands = []

    @staticmethod
    def _structured(value):
        result = getattr(value, "structured_content", None)
        if result is not None:
            return result
        for block in getattr(value, "content", ()) or ():
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    return json.loads(text)
                except (TypeError, ValueError):
                    pass
        return value

    def _freeze_popen(self, args, *positional, **kwargs):
        command = tuple(str(value) for value in args)
        allowed_module = ("-m", "coat_sim.core.appearance_freeze_worker")
        allowed_frozen = ("--coat-worker", "appearance-freeze")
        is_freeze = (len(command) == 7 and command[3:4] == ("--input",)
                     and command[5:6] == ("--output",)
                     and (command[1:3] == allowed_module or command[1:3] == allowed_frozen))
        if not is_freeze:
            raise AssertionError("only the fixed appearance freeze worker is allowed in this fixture")
        self.assertTrue(Path(command[4]).is_relative_to(self.root)
                        or Path(command[4]).is_relative_to(Path(tempfile.gettempdir())))
        self.allowed_freeze_commands.append(command)
        return _REAL_POPEN(args, *positional, **kwargs)

    def _server_patches(self):
        from coat_sim.mcp import server
        from coat_sim.scheduler.client import SchedulerClient

        def client_factory(root=None, **_kwargs):
            return SchedulerClient(root or self.root, transport=self.transport)

        return (
            patch.object(server, "service", self.service),
            patch("subprocess.Popen", side_effect=self._freeze_popen),
            patch("coat_sim.scheduler.SchedulerClient", side_effect=client_factory),
            patch("coat_sim.scheduler.submission.SchedulerClient", side_effect=client_factory),
        )

    def _call(self, name, arguments):
        from coat_sim.mcp import server
        raw = asyncio.run(server.mcp.call_tool(name, arguments))
        return self._structured(raw)

    def _runtime_peer(self, server):
        peer = _RegisteredServerPeer(server)
        meter = _RuntimeHarness(client=peer)
        meter.session = self.session_id
        meter.request = self.request_id
        meter.store = self.assistant
        gateway = ProjectGateway(peer, MAIN_TOOLS, READ_TOOLS, str,
                                 shared=meter.shared["gateway_guard"])
        return peer, meter, gateway

    @staticmethod
    def _runtime_call(meter, gateway, name, arguments, tool_call_id):
        async def project_action(envelope):
            return await gateway.execute(envelope["tool_name"], envelope["arguments"])

        return asyncio.run(meter.wrap_tool_execute(
            None,
            call=SimpleNamespace(tool_name="project_action", tool_call_id=tool_call_id),
            tool_def=None,
            args={"tool_name": name, "arguments": arguments},
            handler=project_action,
        ))

    def _request_from_inspection(self):
        identity = self._call("inspect_appearance_asset", {
            "asset_path": str(self.asset_path),
        })
        self.assertEqual(set(identity), {
            "schema_version", "root", "asset_path", "asset_sha256", "asset_bytes",
        })
        self.assertEqual(identity["schema_version"], 1)
        self.assertEqual(Path(identity["root"]).resolve(), self.root)
        self.assertEqual(Path(identity["asset_path"]).resolve(), self.asset_path.resolve())
        self.assertRegex(identity["asset_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(identity["asset_bytes"], self.asset_path.stat().st_size)
        self.asset_identity = identity
        self.request = {
            "asset_path": identity["asset_path"],
            "asset_sha256": identity["asset_sha256"],
            "render_inputs": self._render_inputs(),
        }
        return identity

    def _count(self, table):
        with self.compute_store.connection() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_registered_service_estimate_manual_release_find_status_cancel_and_result(self):
        from coat_sim.mcp import server

        patches = self._server_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

            def schema(tool):
                return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")

            inspect_schema = schema(registered["inspect_appearance_asset"])
            estimate_schema = schema(registered["estimate_appearance_render"])
            submit_schema = schema(registered["submit_appearance_render"])
            self.assertEqual(inspect_schema["required"], ["asset_path"])
            self.assertEqual(set(inspect_schema["properties"]), {"asset_path"})
            self.assertFalse(inspect_schema["additionalProperties"])
            self.assertEqual(estimate_schema["required"], ["request"])
            self.assertEqual(set(estimate_schema["properties"]), {"request"})
            self.assertFalse(estimate_schema["additionalProperties"])
            self.assertEqual(submit_schema["required"], ["request"])
            self.assertEqual(set(submit_schema["properties"]), {"request", "client_request_id"})
            self.assertFalse(submit_schema["additionalProperties"])

            identity = self._request_from_inspection()
            self.assertEqual(self.request["asset_path"], identity["asset_path"])
            self.assertEqual(self.request["asset_sha256"], identity["asset_sha256"])
            self.assertEqual(self._count("estimates"), 0)
            self.assertEqual(self._count("queue_jobs"), 0)
            self.assertEqual(self.allowed_freeze_commands, [])

            for invalid_path in (
                str(self.root / "outside.coatasset"),
                str(self.root / "data" / "not-an-asset.txt"),
            ):
                with self.subTest(invalid_asset_path=invalid_path):
                    with self.assertRaises(ToolError):
                        asyncio.run(server.mcp.call_tool("inspect_appearance_asset", {
                            "asset_path": invalid_path,
                        }))
            self.assertEqual(self._count("estimates"), 0)
            self.assertEqual(self._count("queue_jobs"), 0)
            self.assertEqual(self.allowed_freeze_commands, [])

            for invalid in (
                {"request": self.request, "approve": True},
                {"request": {**self.request, "route": "remote"}},
                {"request": {**self.request, "worker": "local"}},
            ):
                with self.subTest(extra=list(invalid)):
                    with self.assertRaises(ToolError):
                        asyncio.run(server.mcp.call_tool("submit_appearance_render", invalid))
            self.assertEqual(self._count("estimates"), 0)
            self.assertEqual(self._count("queue_jobs"), 0)
            self.assertFalse(self.transport.submissions)
            self.assertEqual(self.allowed_freeze_commands, [])

            bad_digest = {**self.request, "asset_sha256": "0" * 64}
            with self.assertRaises(ToolError):
                asyncio.run(server.mcp.call_tool(
                    "estimate_appearance_render", {"request": bad_digest}))
            self.assertEqual(self._count("estimates"), 0)
            self.assertEqual(self.allowed_freeze_commands, [])

            estimate = self._call("estimate_appearance_render", {"request": self.request})
            self.assertEqual(estimate["kind"], "appearance_render")
            self.assertIsNone(estimate["estimated_s"])
            self.assertEqual(estimate["sample_count"], 0)
            estimate_id = estimate["estimate_id"]
            stored = self.compute_store.read_estimate(estimate_id)
            frozen = stored["parameters"]["frozen"]
            self.assertEqual(frozen["request"]["asset_sha256"], identity["asset_sha256"])
            self.assertEqual(frozen["context"]["asset_sha256"], identity["asset_sha256"])
            self.assertEqual(stored["input_files"].get(str(self.asset_path.resolve())),
                             identity["asset_sha256"])
            self.assertEqual(stored["input_digest"], estimate["input_digest"])
            self.assertEqual(len(self.allowed_freeze_commands), 1)

            client_request_id = "appearance-assistant-stable-id"
            prepared = self._call("submit_appearance_render", {
                "request": self.request, "client_request_id": client_request_id,
            })
            self.assertEqual(prepared["state"], "prepared")
            self.assertTrue(prepared["needs_release_request"])
            self.assertEqual(prepared["estimate_id"], estimate_id)
            self.assertEqual(prepared["client_request_id"], client_request_id)
            self.assertEqual(prepared["input_digest"], estimate["input_digest"])
            self.assertFalse(self.transport.submissions)
            self.assertEqual(len(self.allowed_freeze_commands), 2)

            changed_request = deepcopy(self.request)
            changed_request["display_exposure"] = 0.02
            with self.assertRaises(ToolError):
                asyncio.run(server.mcp.call_tool("submit_appearance_render", {
                    "request": changed_request,
                    "client_request_id": client_request_id,
                }))
            self.assertFalse(self.transport.submissions)
            self.assertEqual(len(self.allowed_freeze_commands), 3)

            pending = self._call("request_compute_confirmation", {
                "estimate_id": estimate_id,
                "session_id": self.session_id,
                "request_id": self.request_id,
            })
            self.assertEqual(pending["state"], "awaiting_confirmation")
            self.assertEqual(pending["release_policy"], "countdown")
            self.assertFalse(self.transport.submissions)

            # This trusted service callback stands in for the explicitly
            # synthetic local test event; it is not a model-supplied approval.
            queued = server.confirm_and_submit_compute(
                estimate_id, True, service_root=self.root)
            self.assertIn(queued["state"], ("queued", "running"))
            job_id = queued["job_id"]
            self.assertEqual(len(self.transport.submissions), 1)

            found = self._call("find_calculation_job", {"client_request_id": client_request_id})
            job = found.get("job") if isinstance(found, dict) else None
            self.assertIsInstance(job, dict)
            self.assertEqual(job["job_id"], job_id)
            status = self._call("calculation_job_status", {"job_id": job_id})
            self.assertIn(status["state"], ("queued", "running"))

            cancel = self._call("cancel_calculation_job", {"job_id": job_id})
            self.assertTrue(cancel.get("cancel_requested"))
            terminal = self._call("calculation_job_status", {"job_id": job_id})
            self.assertEqual(terminal["state"], "cancelled")
            with self.assertRaises(ToolError):
                asyncio.run(server.mcp.call_tool("calculation_job_result", {"job_id": job_id}))
            persisted_request = self.compute_store.find_request(client_request_id)
            self.assertEqual(persisted_request["job_id"], job_id)
            self.assertEqual(len(self.allowed_freeze_commands), 3)

    def test_registered_service_manual_release_real_outputs_and_tamper_rejection(self):
        from threading import Event

        from coat_sim.mcp import server
        from coat_sim.core.appearance_execution import execute_appearance
        from coat_sim.scheduler.daemon import Scheduler as SchedulerDaemon

        patches = self._server_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            peer, meter, gateway = self._runtime_peer(server)
            identity = self._runtime_call(
                meter, gateway, "inspect_appearance_asset",
                {"asset_path": str(self.asset_path)}, "appearance-inspect-real")
            self.assertEqual(set(identity), {
                "schema_version", "root", "asset_path", "asset_sha256", "asset_bytes",
            })
            self.assertEqual(Path(identity["root"]).resolve(), self.root)
            self.assertEqual(Path(identity["asset_path"]).resolve(), self.asset_path.resolve())
            self.assertEqual(identity["asset_bytes"], self.asset_path.stat().st_size)
            self.asset_identity = identity
            self.request = {
                "asset_path": identity["asset_path"],
                "asset_sha256": identity["asset_sha256"],
                "render_inputs": self._render_inputs(),
            }

            estimate = self._runtime_call(
                meter, gateway, "estimate_appearance_render",
                {"request": self.request}, "appearance-estimate-real")
            self.assertEqual(estimate["kind"], "appearance_render")
            self.assertIsNone(estimate["estimated_s"])
            self.assertEqual(estimate["sample_count"], 0)
            stored = self.compute_store.read_estimate(estimate["estimate_id"])
            frozen = stored["parameters"]["frozen"]
            self.assertEqual(frozen["request"]["asset_sha256"],
                             identity["asset_sha256"])
            self.assertEqual(stored["input_files"].get(str(self.asset_path.resolve())),
                             identity["asset_sha256"])

            submit_call_id = "appearance-submit-real"
            client_request_id = f"{self.request_id}:{submit_call_id}"
            prepared = self._runtime_call(
                meter, gateway, "submit_appearance_render",
                {"request": self.request}, submit_call_id)
            self.assertEqual(prepared["state"], "awaiting_confirmation")
            self.assertEqual(prepared["estimate_id"], estimate["estimate_id"])
            self.assertEqual(prepared["release_policy"], "countdown")
            self.assertFalse(prepared["needs_user_confirmation"])
            self.assertFalse(self.transport.submissions)
            self.assertEqual(peer.calls, [
                ("inspect_appearance_asset", {"asset_path": str(self.asset_path)}),
                ("estimate_appearance_render", {"request": self.request}),
                ("submit_appearance_render", {
                    "request": self.request, "client_request_id": client_request_id,
                }),
                ("request_compute_confirmation", {
                    "estimate_id": estimate["estimate_id"],
                    "session_id": self.session_id,
                    "request_id": self.request_id,
                }),
            ])
            self.assertEqual(meter.session, self.session_id)
            self.assertEqual(meter.request, self.request_id)

            queued = server.confirm_and_submit_compute(
                estimate["estimate_id"], True, service_root=self.root)
            job_id = queued["job_id"]
            self.assertEqual(len(self.transport.submissions), 1)
            found = self._runtime_call(
                meter, gateway, "find_calculation_job",
                {"client_request_id": client_request_id}, "appearance-find-real")
            self.assertEqual(found["job"]["job_id"], job_id)
            status = self._runtime_call(
                meter, gateway, "calculation_job_status",
                {"job_id": job_id}, "appearance-status-real")
            self.assertIn(status["state"], ("queued", "running"))
            with self.assertRaises(ToolError):
                self._call("calculation_job_result", {"job_id": job_id})

            mitsuba_site = (Path(__file__).resolve().parents[1] / "build" /
                            "mitsuba_appearance_env" / "Lib" / "site-packages")
            if not mitsuba_site.is_dir():
                self.skipTest("vendored Mitsuba runtime is unavailable")
            import sys
            if str(mitsuba_site) not in sys.path:
                sys.path.insert(0, str(mitsuba_site))
            try:
                import mitsuba  # noqa: F401
            except Exception as exc:
                self.skipTest(f"vendored Mitsuba cannot initialize here: {exc}")

            submitted = self.transport.submissions[0]
            self.transport.scheduler_store.transition(
                job_id, state="running", progress={"fraction": 0.01, "stage": "running"})
            rendered = execute_appearance(
                self.root, job_id, submitted["spec"]["parameters"], Event(), lambda _value: None)
            registered_result = SchedulerDaemon.register_artifacts(
                SimpleNamespace(root=self.root), job_id, rendered)
            self.transport.scheduler_store.transition(
                job_id, state="succeeded", result=registered_result,
                progress={"fraction": 1.0, "stage": "complete"})

            terminal = self._call("calculation_job_status", {"job_id": job_id})
            self.assertEqual(terminal["state"], "succeeded")
            result = self._runtime_call(
                meter, gateway, "calculation_job_result",
                {"job_id": job_id}, "appearance-result-real")
            self.assertEqual(result["kind"], "appearance_render")
            self.assertEqual(result["job_id"], job_id)
            outputs = result["outputs"]
            self.assertEqual(set(outputs), {
                "display", "manifest", "radiance_spectrum", "xyz", "linear_srgb",
            })
            expected_names = {
                "display": "display.png",
                "manifest": "manifest.json",
                "radiance_spectrum": "radiance_spectrum.npy",
                "xyz": "xyz.npy",
                "linear_srgb": "linear_srgb.npy",
            }
            for name, item in outputs.items():
                with self.subTest(output=name):
                    self.assertEqual(set(item), {"path", "sha256", "bytes", "media_type"})
                    output_path = Path(item["path"]).resolve(strict=True)
                    self.assertTrue(output_path.is_relative_to(self.root / "data"))
                    self.assertEqual(output_path.name, expected_names[name])
                    content = output_path.read_bytes()
                    self.assertEqual(item["bytes"], len(content))
                    self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
                    self.assertIsInstance(item["media_type"], str)
                    self.assertTrue(item["media_type"])
            self.assertEqual([candidate for candidate, _step in meter.artifacts],
                             list(outputs.values()))

            from coat_sim.core import data_files
            artifact_events = []
            meter.writer.emit = lambda event, request, **payload: artifact_events.append(
                (event, request, payload))
            with patch.object(data_files, "PROJECT_ROOT", self.root), \
                    patch.object(data_files, "DATA_ROOT", self.root / "data"):
                for candidate, _step in meter.artifacts:
                    BudgetAndTools._upsert_artifact(
                        meter, candidate, step_id="appearance-result-real")

            shared_files = meter.shared["files"]
            output_paths = {item["path"] for item in outputs.values()}
            self.assertEqual(len(shared_files), 5)
            self.assertEqual({item["path"] for item in shared_files}, output_paths)
            files_by_path = {item["path"]: item for item in shared_files}
            for reference in outputs.values():
                info = files_by_path[reference["path"]]
                self.assertEqual(info["size"], reference["bytes"])
                self.assertEqual(info["reference"],
                                 "data/" + Path(info["path"]).relative_to(self.root / "data").as_posix())
            self.assertEqual(len(artifact_events), 5)
            self.assertEqual({event for event, _request, _payload in artifact_events},
                             {"artifact"})
            self.assertEqual({request for _event, request, _payload in artifact_events},
                             {self.request_id})
            self.assertEqual({payload["file"]["path"]
                              for _event, _request, payload in artifact_events}, output_paths)
            self.assertTrue(all(payload["source"] == "main"
                                and payload["step_id"] == "appearance-result-real"
                                for _event, _request, payload in artifact_events))

            display_path = Path(outputs["display"]["path"])
            display_path.write_bytes(display_path.read_bytes() + b"tamper")
            with self.assertRaises(ToolError):
                self._call("calculation_job_result", {"job_id": job_id})
            final_status = self._call("calculation_job_status", {"job_id": job_id})
            self.assertEqual(final_status["state"], "succeeded")
            self.assertEqual(len(self.transport.submissions), 1)


_REAL_POPEN = subprocess.Popen


if __name__ == "__main__":
    unittest.main()
