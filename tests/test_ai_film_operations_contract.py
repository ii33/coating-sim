"""Assistant-facing contract for the registered, typed film operation path.

The service exercise uses a temporary project root, synthetic materials and a
protocol-shaped in-process scheduler. Runtime routing tests use the real
BudgetAndTools and ProjectGateway with a fake MCP peer; no model or worker
daemon is started.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import (
    BudgetAndTools,
    EXECUTION_TOOLS,
    MAIN_TOOLS,
    READ_TOOLS,
    SUBMIT_TOOLS,
)
from coat_sim.agent.storage import AssistantStore
from coat_sim.agent.tool_dispatch import ProjectGateway
from coat_sim.core.api import CoatService
# Load imported factory bindings before any package-level test patch.
from coat_sim.scheduler import submission as _scheduler_submission
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
)
from coat_sim.core.film_optimization_job import (
    execute_film_optimization_job,
    serialize_request,
)
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.scheduler.client import SchedulerClient as _SchedulerClient
from coat_sim.scheduler.store import SchedulerStore


class _SyntheticSchedulerTransport:
    """Protocol-shaped local scheduler backed by its real persistent store."""

    def __init__(self, root):
        self.scheduler_store = SchedulerStore(root)
        self.submissions = []

    def request(self, action, arguments):
        arguments = deepcopy(arguments)
        if action == "refresh_estimates":
            return {"refreshed": True}
        if action == "submit":
            self.submissions.append(arguments)
            return self.scheduler_store.submit(**arguments)
        if action == "status":
            return self.scheduler_store.status(**arguments)
        if action == "cancel":
            return self.scheduler_store.cancel(**arguments)
        if action == "find_request":
            receipt = self.scheduler_store.queue.find_request(**arguments)
            job = (self.scheduler_store.status(receipt["job_id"])
                   if receipt and receipt.get("job_id") else None)
            return {"job": job}
        if action == "result":
            job = self.scheduler_store.status(**arguments)
            result = job.get("result")
            from coat_sim.scheduler.result_capability import job_has_viewable_result
            if not job_has_viewable_result(job):
                if job.get("state") in ("failed", "cancelled"):
                    raise ValueError("job_result_unavailable: No verified best candidate is available.")
                if job.get("state") == "succeeded" and "result" not in job:
                    raise ValueError("job_result_missing: successful queue record has no result payload")
                if job.get("state") == "succeeded":
                    raise ValueError("job_result_invalid_shape: expected a result object")
                raise ValueError("job_not_complete: " + str(job.get("state")))
            return result
        raise AssertionError(f"unexpected scheduler action: {action}")


class _Tool:
    def __init__(self, name, input_schema):
        self.name = name
        self.description = "synthetic MCP contract fixture"
        self.input_schema = input_schema


_FILM_ESTIMATE_SCHEMA = {
    "type": "object", "properties": {"request": {"type": "object"}},
    "required": ["request"], "additionalProperties": False,
}
_FILM_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "request": {"type": "object"}, "client_request_id": {"type": ["string", "null"]},
    },
    "required": ["request"], "additionalProperties": False,
}
_CONFIRM_SCHEMA = {
    "type": "object", "properties": {
        "estimate_id": {"type": "string"}, "session_id": {"type": "string"},
        "request_id": {"type": "string"},
    }, "required": ["estimate_id"], "additionalProperties": False,
}


class _FakeClient:
    def __init__(self, *, submit_cancel_once=False):
        self.calls = []
        self.submit_cancel_once = submit_cancel_once
        self.tools = [
            _Tool("estimate_film_optimization", _FILM_ESTIMATE_SCHEMA),
            _Tool("submit_film_optimization", _FILM_SUBMIT_SCHEMA),
            _Tool("request_compute_confirmation", _CONFIRM_SCHEMA),
            _Tool("apply_film_optimization", {"type": "object", "properties": {}}),
        ]

    async def list_tools(self):
        return self.tools

    async def call_tool_mcp(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "submit_film_optimization" and self.submit_cancel_once:
            self.submit_cancel_once = False
            raise asyncio.CancelledError()
        if name == "submit_film_optimization":
            value = {"state": "prepared", "estimate_id": "estimate-fixture",
                     "needs_release_request": True, "estimated_s": None}
        elif name == "request_compute_confirmation":
            value = {"state": "awaiting_confirmation", "estimate_id": arguments["estimate_id"],
                     "release_policy": "manual", "needs_user_confirmation": True}
        else:
            value = {"estimate_id": "estimate-read-fixture", "estimated_s": None}
        return SimpleNamespace(structured_content=value, content=[], is_error=False)


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
    """Initialize only the fields used by the real tool wrapper."""

    def __init__(self, *, role="main", client=None):
        self.role = role
        self.mode = "operate"  # These fixtures exercise the explicitly operational path.
        self.config = {"task_mode": "operate"}
        self.client = client
        self.shared = {"tool_calls": 0, "gateways": {}, "gateway_guard": {}}
        self.limits = {"tool_calls": 40}
        self.tool_lock = asyncio.Lock()
        self.session = "synthetic-assistant-session"
        self.request = "synthetic-assistant-request"
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

    def _record_issue(self, *_args, **_kwargs):
        return None


class AssistantRuntimeRoutingContractTests(unittest.TestCase):
    def test_film_tools_are_role_scoped_and_runtime_injects_stable_submit_identity(self):
        self.assertIn("estimate_film_optimization", READ_TOOLS)
        self.assertIn("estimate_film_optimization", MAIN_TOOLS)
        self.assertIn("submit_film_optimization", MAIN_TOOLS)
        self.assertIn("submit_film_optimization", SUBMIT_TOOLS)
        self.assertIn("estimate_film_optimization", EXECUTION_TOOLS["simulation"])
        self.assertIn("submit_film_optimization", EXECUTION_TOOLS["simulation"])
        self.assertNotIn("submit_film_optimization", EXECUTION_TOOLS["design"])
        self.assertNotIn("submit_film_optimization", EXECUTION_TOOLS["correction"])
        self.assertNotIn("apply_film_optimization", MAIN_TOOLS)
        self.assertNotIn("apply_film_optimization", EXECUTION_TOOLS["simulation"])

        client = _FakeClient()
        meter = _RuntimeHarness(client=client)
        gateway = ProjectGateway(client, MAIN_TOOLS, READ_TOOLS,
                                 lambda value: str(value),
                                 shared=meter.shared["gateway_guard"])
        main_catalog = asyncio.run(gateway.catalog())
        main_tool_names = {item["name"] for item in main_catalog["tools"]}
        self.assertIn("estimate_film_optimization", main_tool_names)
        self.assertIn("submit_film_optimization", main_tool_names)
        self.assertNotIn("apply_film_optimization", main_tool_names)

        async def project_action(envelope):
            return await gateway.execute(envelope["tool_name"], envelope["arguments"])

        call = SimpleNamespace(tool_name="project_action", tool_call_id="model-call-7")
        result = asyncio.run(meter.wrap_tool_execute(
            None, call=call, tool_def=None,
            args={"tool_name": "submit_film_optimization", "arguments": {
                "request": {"kind": "coat_sim.film_optimization_request", "schema_version": 1},
            }}, handler=project_action))

        self.assertEqual(result["state"], "awaiting_confirmation")
        self.assertEqual(len(client.calls), 2)
        tool_name, arguments = client.calls[0]
        self.assertEqual(tool_name, "submit_film_optimization")
        self.assertEqual(arguments["client_request_id"],
                         "synthetic-assistant-request:model-call-7")
        release_name, release_arguments = client.calls[1]
        self.assertEqual(release_name, "request_compute_confirmation")
        self.assertEqual(release_arguments, {
            "estimate_id": "estimate-fixture",
            "session_id": "synthetic-assistant-session",
            "request_id": "synthetic-assistant-request",
        })
        self.assertEqual(meter.ledger.intents[0][2:],
                         ("submit_film_optimization", "synthetic-assistant-request:model-call-7"))

        malformed = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="model-call-9"),
            tool_def=None, args={"tool_name": "submit_film_optimization", "arguments": {
                "request": {"kind": "coat_sim.film_optimization_request", "schema_version": 1},
                "approve": True,
            }}, handler=project_action))
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["error"]["code"], "invalid_arguments")
        self.assertEqual(malformed["error"]["effect"], "none")
        self.assertEqual(len(client.calls), 2)

        estimate_first = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="read-call-1"),
            tool_def=None, args={"tool_name": "estimate_film_optimization", "arguments": {
                "request": {"kind": "coat_sim.film_optimization_request", "schema_version": 1},
            }}, handler=project_action))
        estimate_second = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="read-call-2"),
            tool_def=None, args={"tool_name": "estimate_film_optimization", "arguments": {
                "request": {"kind": "coat_sim.film_optimization_request", "schema_version": 1},
            }}, handler=project_action))
        self.assertEqual(estimate_first, estimate_second)
        self.assertEqual([name for name, _ in client.calls].count("estimate_film_optimization"), 2)

        unavailable = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="project_action", tool_call_id="model-call-8"),
            tool_def=None, args={"tool_name": "apply_film_optimization", "arguments": {}},
            handler=project_action))
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["error"]["code"], "unavailable_tool")
        self.assertEqual(len(client.calls), 4)

    def test_simulation_delegate_allows_exact_film_pair_and_other_domains_deny_submit(self):
        client = _FakeClient()
        meter = _RuntimeHarness(role="execute_simulation", client=client)
        request = {"kind": "coat_sim.film_optimization_request", "schema_version": 1}
        estimate = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="estimate_film_optimization", tool_call_id="estimate-call"),
            tool_def=None, args={"request": request}, handler=lambda _arguments: None))
        self.assertEqual(estimate["estimate_id"], "estimate-read-fixture")
        self.assertEqual(client.calls[0][0], "estimate_film_optimization")
        self.assertNotIn("client_request_id", client.calls[0][1])

        submit = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="submit_film_optimization", tool_call_id="submit-call"),
            tool_def=None, args={"request": request}, handler=lambda _arguments: None))
        self.assertEqual(submit["state"], "awaiting_confirmation")
        self.assertEqual(client.calls[1][0], "submit_film_optimization")
        self.assertEqual(client.calls[1][1]["client_request_id"],
                         "synthetic-assistant-request:submit-call")
        self.assertEqual(client.calls[2][0], "request_compute_confirmation")
        self.assertEqual(client.calls[2][1]["session_id"], meter.session)
        self.assertEqual(client.calls[2][1]["request_id"], meter.request)
        simulation_gateway = meter.shared["gateways"]["execute_simulation"]
        simulation_catalog = asyncio.run(simulation_gateway.catalog())
        simulation_tool_names = {item["name"] for item in simulation_catalog["tools"]}
        self.assertIn("estimate_film_optimization", simulation_tool_names)
        self.assertIn("submit_film_optimization", simulation_tool_names)
        self.assertNotIn("apply_film_optimization", simulation_tool_names)

        for role in ("execute_design", "execute_correction"):
            denied_client = _FakeClient()
            denied = _RuntimeHarness(role=role, client=denied_client)
            result = asyncio.run(denied.wrap_tool_execute(
                None, call=SimpleNamespace(tool_name="submit_film_optimization", tool_call_id="denied"),
                tool_def=None, args={"request": request}, handler=lambda _arguments: None))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "unavailable_tool")
            self.assertFalse(denied_client.calls)

    def test_dispatched_submit_cancellation_stays_unknown_across_new_tool_call_id(self):
        client = _FakeClient(submit_cancel_once=True)
        meter = _RuntimeHarness(role="execute_simulation", client=client)
        arguments = {"request": {
            "kind": "coat_sim.film_optimization_request", "schema_version": 1,
        }}

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(meter.wrap_tool_execute(
                None, call=SimpleNamespace(tool_name="submit_film_optimization", tool_call_id="call-1"),
                tool_def=None, args=arguments,
                handler=lambda _arguments: None))

        guard = meter.shared["gateway_guard"]
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(guard["unknown"]), 1)
        first_request_id = client.calls[0][1]["client_request_id"]

        blocked = asyncio.run(meter.wrap_tool_execute(
            None, call=SimpleNamespace(tool_name="submit_film_optimization", tool_call_id="call-2"),
            tool_def=None, args=arguments,
            handler=lambda _arguments: None))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["effect"], "unknown")
        self.assertTrue(blocked["dispatch_blocked"])
        self.assertEqual(blocked["client_request_id"], first_request_id)
        self.assertEqual(len(client.calls), 1)


class RegisteredFilmServiceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assistant-film-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CoatService(self.root)
        self.compute_store = ComputeTaskStore(self.root)
        self.transport = _SyntheticSchedulerTransport(self.root)
        library = MaterialLibrary(self.root)

        def optical(identifier, refractive_index):
            return {
                "id": identifier, "name": identifier, "kind": "tabulated",
                "notes": "Synthetic assistant contract fixture",
                "source": "Test fixture only", "wavelength_unit": "nm",
                "wavelength_min": 500.0, "wavelength_max": 600.0,
                "wavelengths": [500.0, 600.0],
                "n": [refractive_index, refractive_index], "k": [0.0, 0.0],
            }

        for identifier, category, data in (
            ("assistant-glass", "substrate", optical("assistant-glass-set", 1.5)),
            ("assistant-film", "film", optical("assistant-film-set", 1.23)),
        ):
            record = default_material_profile("Synthetic " + identifier, id=identifier)
            record.update(category=category, optical_constants=[data])
            library.write(record)

        self.design = self.service.new_film_design()
        self.design["substrate"].update(
            material_id="assistant-glass", optical_set_id="assistant-glass-set")
        self.design["front_layers"] = [{
            "id": "assistant-layer", "material_id": "assistant-film",
            "optical_set_id": "assistant-film-set",
            "thickness": {"value": 100.0, "unit": "nm"},
            "note": "Synthetic assistant contract layer",
        }]
        self.request = serialize_request(FilmOptimizationRequest(
            design_snapshot=deepcopy(self.design), design_revision=23,
            variables=(FilmThicknessVariable("front", "assistant-layer", 80.0, 120.0),),
            objectives=(FilmOptimizationObjective(
                wavelength_min_nm=500.0, wavelength_max_nm=600.0,
                wavelength_samples=3, angles_deg=(0.0, 45.0),
                polarization="unpolarized", metric="R", mode="value", target_value=0.1,
            ),),
            max_iterations=2, max_evaluations=8,
        ))
        self.assistant = AssistantStore(self.root / "artifacts/assistant/state.sqlite3")
        self.session_id = self.assistant.new_session("synthetic assistant film contract")
        self._record_operate_event("operate-origin-1")

    def _record_operate_event(self, request_id):
        self.assistant.set_ui_session_mode(self.session_id, "operate")
        self.assistant.bind_ui_request(self.session_id, request_id, "operate")
        self.assistant.record_event(self.session_id, request_id, "progress",
                                    {"stage": "received", "mode": "operate"})
        return request_id

    def _client_factory(self, root=None, **_kwargs):
        return _SchedulerClient(root or self.root, transport=self.transport)

    def _patch_services(self):
        from coat_sim.mcp import server
        return (
            patch.object(server, "service", self.service),
            patch("coat_sim.scheduler.SchedulerClient", side_effect=self._client_factory),
            patch("coat_sim.scheduler.submission.SchedulerClient", side_effect=self._client_factory),
        )

    @staticmethod
    def _structured(result):
        value = getattr(result, "structured_content", None)
        if value is not None:
            return value
        for block in getattr(result, "content", ()) or ():
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    return json.loads(text)
                except (TypeError, ValueError):
                    pass
        return result

    def _call(self, name, arguments):
        from coat_sim.mcp import server
        return self._structured(asyncio.run(server.mcp.call_tool(name, arguments)))

    def test_registered_typed_path_preserves_snapshot_release_job_result_and_cancel(self):
        from coat_sim.mcp import server

        patches = self._patch_services()
        with patches[0], patches[1], patches[2]:
            tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

            def schema(tool):
                return getattr(tool, "input_schema")

            estimate_schema = schema(tools["estimate_film_optimization"])
            submit_schema = schema(tools["submit_film_optimization"])
            self.assertEqual(estimate_schema["required"], ["request"])
            self.assertEqual(set(estimate_schema["properties"]), {"request"})
            self.assertFalse(estimate_schema["additionalProperties"])
            self.assertEqual(submit_schema["required"], ["request"])
            self.assertEqual(set(submit_schema["properties"]), {"request", "client_request_id"})
            self.assertFalse(submit_schema["additionalProperties"])
            self.assertFalse(any("apply_film" in name for name in tools))

            with self.assertRaises(ToolError):
                asyncio.run(server.mcp.call_tool("submit_film_optimization", {
                    "request": self.request, "approve": True,
                }))
            with self.compute_store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM estimates").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0], 0)

            client_request_id = "assistant-film-stable-request"
            estimate = self._call("estimate_film_optimization", {"request": self.request})
            estimate_id = estimate["estimate_id"]
            stored = self.compute_store.read_estimate(estimate_id)
            frozen_request = stored["parameters"]["request"]
            frozen_hash = frozen_request["frozen_material_snapshot"]["sha256"]
            self.assertEqual(stored["kind"], "film_optimization")
            self.assertEqual(frozen_request["design_revision"], 23)
            self.assertRegex(frozen_hash, r"^[0-9a-f]{64}$")
            self.assertIsNone(estimate["estimated_s"])

            prepared = self._call("submit_film_optimization", {
                "request": self.request, "client_request_id": client_request_id,
            })
            self.assertEqual(prepared["state"], "prepared")
            self.assertEqual(prepared["estimate_id"], estimate_id)
            self.assertEqual(prepared["client_request_id"], client_request_id)
            self.assertTrue(prepared["needs_release_request"])
            self.assertFalse(self.transport.submissions)

            pending = server.request_compute_confirmation(
                estimate_id, self.session_id, "operate-origin-1")
            self.assertEqual(pending["state"], "awaiting_confirmation")
            self.assertEqual(pending["release_policy"], "countdown")
            self.assertFalse(pending["needs_user_confirmation"])
            self.assertFalse(self.transport.submissions)
            self.assertEqual(self.compute_store.read_estimate(estimate_id)["state"], "pending")

            # This is the existing trusted confirmation callback in an isolated
            # service test; the MCP submit arguments cannot supply this decision.
            job = server.confirm_and_submit_compute(
                estimate_id, True, service_root=self.root)
            self.assertEqual(job["state"], "queued")
            self.assertEqual(job["estimate_id"], estimate_id)
            self.assertEqual(len(self.transport.submissions), 1)
            submitted_request = self.transport.submissions[0]["spec"]["parameters"]["payload"]["request"]
            self.assertEqual(submitted_request["design_revision"], 23)
            self.assertEqual(submitted_request["frozen_material_snapshot"]["sha256"], frozen_hash)
            self.assertEqual(submitted_request["max_iterations"], 2)
            self.assertEqual(submitted_request["max_evaluations"], 8)
            self.assertNotIn("mode", submitted_request)
            self.assertNotIn("approval", submitted_request)
            self.assertNotIn("worker", submitted_request)

            found = self._call("find_calculation_job", {"client_request_id": client_request_id})
            self.assertEqual(found["job"]["job_id"], job["job_id"])
            self.assertEqual(self._call("calculation_job_status", {
                "job_id": job["job_id"],
            })["state"], "queued")
            with self.assertRaisesRegex(ToolError, "job_not_complete: queued"):
                self._call("calculation_job_result", {"job_id": job["job_id"]})

            result = execute_film_optimization_job(
                self.service.film_design_service,
                self.transport.submissions[0]["spec"]["parameters"]["payload"],
                cancel_event=Event(),
            )
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["request"]["design_revision"], 23)
            self.assertEqual(result["request"]["frozen_material_snapshot"]["sha256"], frozen_hash)
            self.assertEqual(result["partial_result"]["kind"], "best_candidate")
            self.assertTrue(result["partial_result"]["best_so_far"])
            self.assertTrue(result["partial_result"]["candidate_verified"])
            targets_met = result["candidate"]["quality"]["targets_met"]
            self.assertIs(type(targets_met), bool)
            self.assertEqual(result["partial_result"]["quality"]["value"],
                             result["candidate"]["quality"]["value"])
            self.assertIn("validation_targets_met", result["solver_validation"])

            self.transport.scheduler_store.transition(
                job["job_id"], state="succeeded", result=result,
                progress={"fraction": 1.0, "stage": "complete"})
            terminal = self._call("calculation_job_status", {"job_id": job["job_id"]})
            recovered = self._call("calculation_job_result", {"job_id": job["job_id"]})
            self.assertEqual(terminal["state"], "succeeded")
            self.assertEqual(terminal["result"]["state"], "completed")
            self.assertEqual(recovered["kind"], result["kind"])
            self.assertEqual(recovered["state"], result["state"])
            self.assertEqual(recovered["partial_result"], result["partial_result"])
            self.assertIs(recovered["candidate"]["quality"]["targets_met"], targets_met)
            self.assertEqual(recovered["request"]["design_revision"], 23)
            self.assertEqual(recovered["request"]["frozen_material_snapshot"]["sha256"], frozen_hash)
            self.assertFalse(self.transport.submissions[0]["spec"]["parameters"]["payload"].get("apply"))
            self.assertEqual(self.design["front_layers"][0]["thickness"]["value"], 100.0)
            self.assertEqual(self.request["design_snapshot"]["front_layers"][0]["thickness"]["value"], 100.0)

            # A distinct request identity over the same frozen payload creates
            # an independent receipt. Cancellation remains only a scheduler request.
            cancel_origin = self._record_operate_event("operate-origin-cancel")
            cancel_receipt = self._call("submit_film_optimization", {
                "request": self.request, "client_request_id": "assistant-film-cancel-request",
            })
            cancel_pending = server.request_compute_confirmation(
                cancel_receipt["estimate_id"], self.session_id, cancel_origin)
            self.assertEqual(cancel_pending["release_policy"], "countdown")
            cancel_job = server.confirm_and_submit_compute(
                cancel_receipt["estimate_id"], True, service_root=self.root)
            self.transport.scheduler_store.transition(
                cancel_job["job_id"], state="running",
                started_at=datetime.now(timezone.utc).isoformat(),
                progress={"fraction": 0.25, "stage": "running"})
            cancel_ack = self._call("cancel_calculation_job", {"job_id": cancel_job["job_id"]})
            self.assertEqual(cancel_ack["job_id"], cancel_job["job_id"])
            self.assertEqual(cancel_ack["state"], "running")
            self.assertTrue(cancel_ack["cancel_requested"])
            terminal_cancel = self._call("calculation_job_status", {"job_id": cancel_job["job_id"]})
            self.assertEqual(terminal_cancel["state"], "running")
            self.transport.scheduler_store.transition(
                cancel_job["job_id"], state="cancelled", error_code="cancelled",
                error="Synthetic worker observed cancellation")
            terminal_cancel = self._call("calculation_job_status", {"job_id": cancel_job["job_id"]})
            self.assertEqual(terminal_cancel["state"], "cancelled")
            self.assertTrue(terminal_cancel["cancel_requested"])
            self.assertEqual(terminal_cancel["error"], "Synthetic worker observed cancellation")
            with self.assertRaisesRegex(ToolError, "job_result_unavailable"):
                self._call("calculation_job_result", {"job_id": cancel_job["job_id"]})


if __name__ == "__main__":
    unittest.main()
