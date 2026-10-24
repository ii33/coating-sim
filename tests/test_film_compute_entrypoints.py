"""Synthetic registered-MCP tests for typed film compute entrypoints.

The fixtures use the real request decoder, freezer, estimate/confirmation and
scheduler stores, MCP registry, and optimizer. A protocol-shaped in-process
transport avoids starting a daemon or network endpoint.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import tempfile
import unittest
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.storage import AssistantStore
from coat_sim.core.api import CoatService
# Load imported factory bindings before any package-level test patch.
from coat_sim.scheduler import submission as _scheduler_submission
from coat_sim.core.compute_tasks import ComputeTaskStore, digest
from coat_sim.core.film_compute import (
    canonical_film_parameters,
    estimate_film,
    submit_film,
)
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
)
from coat_sim.core.film_optimization_job import (
    deserialize_request,
    execute_film_optimization_job,
    serialize_request,
    validate_checkpoint,
)
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.scheduler.client import SchedulerClient as _SchedulerClient
from coat_sim.scheduler.store import SchedulerStore


class _SyntheticSchedulerTransport:
    """Protocol-shaped local peer backed by the real persistent SchedulerStore.

    Action/result shapes follow Scheduler.dispatch in scheduler/daemon.py:
    submit/status/cancel return durable job rows, find_request wraps a job under
    ``job``, and result requires Scheduler's viewable-result capability check.
    """

    def __init__(self, root: Path):
        self.root = root
        self.scheduler_store = SchedulerStore(root)
        self.calls: list[tuple[str, dict]] = []
        self.submissions: list[dict] = []

    def request(self, action, arguments):
        arguments = deepcopy(arguments)
        self.calls.append((action, arguments))
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
        raise AssertionError(f"unexpected scheduler protocol action: {action}")


class FilmComputeEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="film-compute-entrypoints-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CoatService(self.root)
        self.store = ComputeTaskStore(self.root)
        self.transport = _SyntheticSchedulerTransport(self.root)
        self._scheduler_patches = None

        library = MaterialLibrary(self.root)

        def optical(identifier, refractive_index):
            return {
                "id": identifier,
                "name": identifier,
                "kind": "tabulated",
                "notes": "Synthetic entrypoint fixture",
                "source": "Test fixture only",
                "wavelength_unit": "nm",
                "wavelength_min": 500.0,
                "wavelength_max": 600.0,
                "wavelengths": [500.0, 600.0],
                "n": [refractive_index, refractive_index],
                "k": [0.0, 0.0],
            }

        for identifier, category, data in (
            ("entry-glass", "substrate", optical("entry-glass-set", 1.5)),
            ("entry-film", "film", optical("entry-film-set", 1.23)),
        ):
            record = default_material_profile("Synthetic " + identifier, id=identifier)
            record.update(category=category, optical_constants=[data])
            library.write(record)

        self.design = self.service.new_film_design()
        self.design["substrate"].update(
            material_id="entry-glass", optical_set_id="entry-glass-set")
        self.design["front_layers"] = [{
            "id": "entry-layer",
            "material_id": "entry-film",
            "optical_set_id": "entry-film-set",
            "thickness": {"value": 100.0, "unit": "nm"},
            "note": "Synthetic entrypoint layer",
        }]
        self.request = serialize_request(FilmOptimizationRequest(
            design_snapshot=deepcopy(self.design),
            design_revision=23,
            variables=(FilmThicknessVariable("front", "entry-layer", 80.0, 120.0),),
            objectives=(FilmOptimizationObjective(pointwise=FilmObjectiveConstraint(mode="value", target_value=0.1, lower_bound=None, upper_bound=None), wavelength_min_nm=500.0, wavelength_max_nm=600.0, wavelength_samples=3, angles_deg=(0.0, 45.0), polarization="unpolarized", metric="R"),),
            max_iterations=2,
            max_evaluations=8,
        ))

        self.assistant = AssistantStore(self.root / "artifacts/assistant/state.sqlite3")
        self.session_id = self.assistant.new_session("synthetic typed compute")
        self.request_id = "synthetic-operate-request"
        self.assistant.set_ui_session_mode(self.session_id, "operate")
        self.assistant.bind_ui_request(self.session_id, self.request_id, "operate")
        self.assistant.record_event(
            self.session_id, self.request_id, "progress",
            {"stage": "received", "mode": "operate"},
        )

    def _client_factory(self, root=None, **_kwargs):
        return _SchedulerClient(root or self.root, transport=self.transport)

    @contextmanager
    def _scheduler_context(self):
        """Patch both imported factories used by estimates, releases and submit."""
        from coat_sim.mcp import server
        with ExitStack() as stack:
            stack.enter_context(patch.object(server, "service", self.service))
            stack.enter_context(patch(
                "subprocess.Popen",
                side_effect=AssertionError(
                    "synthetic MCP tests must not start a scheduler or worker process"),
            ))
            stack.enter_context(patch(
                "coat_sim.scheduler.SchedulerClient", side_effect=self._client_factory))
            stack.enter_context(patch(
                "coat_sim.scheduler.submission.SchedulerClient",
                side_effect=self._client_factory))
            yield

    def _row_count(self, table="estimates"):
        with self.store.connection() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _record_operate_request(self, request_id):
        self.assistant.set_ui_session_mode(self.session_id, "operate")
        self.assistant.bind_ui_request(self.session_id, request_id, "operate")
        self.assistant.record_event(
            self.session_id, request_id, "progress",
            {"stage": "received", "mode": "operate"},
        )

    @staticmethod
    def _call_mcp(name, arguments):
        from coat_sim.mcp import server
        value = asyncio.run(server.mcp.call_tool(name, arguments))
        structured = getattr(value, "structuredContent", None)
        if structured is None:
            structured = getattr(value, "structured_content", None)
        if structured is not None:
            return structured
        # Some SDK tool handlers return JSON as a text content block when the
        # function has no explicit output schema; decode that real MCP form.
        for block in getattr(value, "content", ()) or ():
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    return json.loads(text)
                except (TypeError, ValueError):
                    continue
        return value

    def _structure_request(self):
        from coat_sim.core.film_optimization_structure import (
            FilmStructureOptimizationRequest,
            film_structure_request_to_payload,
        )
        return film_structure_request_to_payload(FilmStructureOptimizationRequest(
            base_request=deserialize_request(self.request),
        ))

    def test_real_mcp_registry_exposes_strict_typed_tools_and_rejects_extra_fields(self):
        from coat_sim.mcp import server

        with self._scheduler_context():
            tools = asyncio.run(server.mcp.list_tools())
            by_name = {tool.name: tool for tool in tools}
            estimate_tool = by_name["estimate_film_optimization"]
            submit_tool = by_name["submit_film_optimization"]

            def schema(tool):
                value = getattr(tool, "inputSchema", None)
                if value is None:
                    value = getattr(tool, "input_schema")
                return value

            estimate_schema = schema(estimate_tool)
            submit_schema = schema(submit_tool)
            self.assertFalse(estimate_schema["additionalProperties"])
            self.assertEqual(estimate_schema["required"], ["request"])
            self.assertEqual(set(estimate_schema["properties"]), {"request"})
            self.assertFalse(submit_schema["additionalProperties"])
            self.assertEqual(submit_schema["required"], ["request"])
            self.assertEqual(
                set(submit_schema["properties"]), {"request", "client_request_id"})

            before = self._row_count()
            with self.assertRaises(ToolError):
                asyncio.run(server.mcp.call_tool(
                    "submit_film_optimization",
                    {"request": self.request, "approve": True},
                ))
            self.assertEqual(self._row_count(), before)
            self.assertEqual(self.transport.submissions, [])
            with self.assertRaises(AssertionError):
                self.transport.request("synthetic_unknown_action", {})

    def test_estimate_is_canonical_unbound_and_submit_reuses_only_exact_estimate(self):
        with self._scheduler_context():
            canonical = canonical_film_parameters(
                self.service, {"request": self.request})
            self.assertEqual(set(canonical), {"request"})
            estimate = estimate_film(self.service, self.request)
            self.assertIsNone(estimate["estimated_s"])
            self.assertEqual(estimate["sample_count"], 0)
            self.assertIsNone(self.store.find_request("client-before-submit"))

            stored = self.store.read_estimate(estimate["estimate_id"])
            self.assertEqual(stored["kind"], "film_optimization")
            self.assertEqual(stored["parameters"], json.loads(json.dumps(canonical)))
            self.assertEqual(stored["input_digest"], digest({
                "kind": "film_optimization", "parameters": canonical, "input_files": {}}))
            frozen = canonical["request"]["frozen_material_snapshot"]
            self.assertRegex(frozen["sha256"], r"^[0-9a-f]{64}$")

            prepared = submit_film(
                self.service, self.request, "client-before-submit")
            self.assertEqual(prepared["state"], "prepared")
            self.assertEqual(prepared["estimate_id"], estimate["estimate_id"])
            self.assertEqual(prepared["client_request_id"], "client-before-submit")
            self.assertEqual(self.store.read_estimate(estimate["estimate_id"])["state"], "estimated")
            self.assertEqual(self.store.find_request("client-before-submit")["estimate_id"],
                             estimate["estimate_id"])
            self.assertEqual(self.transport.submissions, [])
            self.assertEqual(self._row_count("queue_jobs"), 0)

    def test_unbound_estimate_claim_is_atomic_under_bounded_concurrency(self):
        with self._scheduler_context():
            estimate = estimate_film(self.service, self.request)
            values = canonical_film_parameters(self.service, {"request": self.request})
            identity = digest({
                "kind": "film_optimization", "parameters": values, "input_files": {}})
            contenders = [f"concurrent-owner-{index}" for index in range(8)]
            barrier = Barrier(len(contenders))

            def claim(client_request_id):
                barrier.wait(timeout=10)
                return client_request_id, self.store.claim_unbound_estimate(
                    client_request_id, identity)

            with ThreadPoolExecutor(max_workers=len(contenders)) as pool:
                results = list(pool.map(claim, contenders))

            winners = [(client_id, estimate_id) for client_id, estimate_id in results
                       if estimate_id is not None]
            self.assertEqual(len(winners), 1, results)
            winner_id, claimed_estimate_id = winners[0]
            self.assertEqual(claimed_estimate_id, estimate["estimate_id"])
            self.assertEqual(self.store.read_estimate(claimed_estimate_id)["state"], "estimated")
            self.assertEqual(self.store.find_request(winner_id)["estimate_id"], claimed_estimate_id)
            for client_id, claimed in results:
                if client_id != winner_id:
                    self.assertIsNone(claimed)
                    self.assertIsNone(self.store.find_request(client_id))

            # Exact retry resolves to the durable alias; changing the digest for
            # the same ID remains a conflict after the claim is committed.
            self.assertEqual(
                self.store.claim_unbound_estimate(winner_id, identity), claimed_estimate_id)
            with self.assertRaisesRegex(ValueError, "request_id_conflict"):
                self.store.claim_unbound_estimate(winner_id, identity + "-changed")
            self.assertEqual(
                self.store.read_estimate(claimed_estimate_id)["state"], "estimated")

    def test_operate_confirmation_and_accepted_submit_are_idempotent(self):
        from coat_sim.mcp import server

        client_request_id = "film-entrypoint-stable-client"
        with self._scheduler_context():
            # Exercise the registered MCP tool, not a fake peer or direct wrapper.
            estimate_result = asyncio.run(server.mcp.call_tool(
                "estimate_film_optimization", {"request": self.request}))
            receipt = getattr(estimate_result, "structuredContent", None)
            if receipt is None:
                receipt = getattr(estimate_result, "structured_content", None)
            if receipt is None:
                receipt = estimate_result
            estimate_id = receipt["estimate_id"]

            prepared = asyncio.run(server.mcp.call_tool(
                "submit_film_optimization", {
                    "request": self.request,
                    "client_request_id": client_request_id,
                }))
            prepared_value = getattr(prepared, "structuredContent", None)
            if prepared_value is None:
                prepared_value = getattr(prepared, "structured_content", None)
            if prepared_value is None:
                prepared_value = prepared
            self.assertEqual(prepared_value["estimate_id"], estimate_id)
            self.assertEqual(prepared_value["state"], "prepared")
            self.assertFalse(self.transport.submissions)

            pending = server.request_compute_confirmation(
                estimate_id, self.session_id, self.request_id)
            self.assertEqual(pending["state"], "awaiting_confirmation")
            self.assertEqual(pending["release_policy"], "countdown")
            self.assertFalse(pending["needs_user_confirmation"])
            self.assertIsNone(pending["estimated_s"])
            self.assertEqual(pending["sample_count"], 0)
            self.assertFalse(self.transport.submissions)

            # Only this trusted UI/server callback supplies the acceptance.
            job = server.confirm_and_submit_compute(
                estimate_id, True, service_root=self.root)
            self.assertEqual(job["state"], "queued")
            self.assertEqual(job["estimate_id"], estimate_id)
            self.assertEqual(len(self.transport.submissions), 1)
            submission = self.transport.submissions[0]
            self.assertEqual(submission["metadata"]["estimate_id"], estimate_id)
            spec = submission["spec"]
            self.assertEqual(spec["executor"], "film_optimization")
            payload = spec["parameters"]["payload"]
            frozen_request = payload["request"]
            self.assertEqual(frozen_request["design_revision"], 23)
            self.assertEqual(
                frozen_request["frozen_material_snapshot"]["sha256"],
                self.store.read_estimate(estimate_id)["parameters"]["request"][
                    "frozen_material_snapshot"]["sha256"],
            )
            self.assertNotIn("mode", frozen_request)
            self.assertNotIn("worker", frozen_request)
            self.assertNotIn("approval", frozen_request)

            found = self._call_mcp("find_calculation_job", {
                "client_request_id": client_request_id})
            self.assertEqual(found["job"]["job_id"], job["job_id"])
            self.assertEqual(found["job"]["state"], "queued")
            queued = self._call_mcp("calculation_job_status", {
                "job_id": job["job_id"]})
            self.assertEqual(queued["state"], "queued")
            with self.assertRaisesRegex(ToolError, "job_not_complete: queued"):
                self._call_mcp("calculation_job_result", {"job_id": job["job_id"]})

            # A real synthetic executor run proves scheduler/job state remains
            # distinct from optimizer terminal state and candidate target flags.
            checkpoints: list[dict] = []
            result = execute_film_optimization_job(
                self.service.film_design_service, payload, cancel_event=Event(),
                checkpoint_callback=checkpoints.append)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["kind"], "coat_sim.film_optimization_result")
            self.assertIs(type(result["candidate"]["quality"]["targets_met"]), bool)
            self.assertIn("validation_targets_met", result["solver_validation"])
            self.assertIs(type(result["solver_validation"]["validation_complete"]), bool)
            self.assertEqual(result["candidate"]["design_revision"], 23)
            self.assertTrue(checkpoints)
            verified_checkpoint = validate_checkpoint(
                self.service.film_design_service, checkpoints[-1])
            self.assertEqual(verified_checkpoint, checkpoints[-1])

            self.transport.scheduler_store.transition(
                job["job_id"], state="succeeded", result=result,
                progress={"fraction": 1.0, "stage": "complete"})
            succeeded = self._call_mcp("calculation_job_status", {
                "job_id": job["job_id"]})
            self.assertEqual(succeeded["state"], "succeeded")
            self.assertEqual(succeeded["result"]["state"], "completed")
            recovered_result = self._call_mcp("calculation_job_result", {
                "job_id": job["job_id"]})
            self.assertEqual(
                set(recovered_result), set(result),
                f"MCP keys={sorted(recovered_result)} executor keys={sorted(result)}")
            self.assertEqual(recovered_result["request"]["design_revision"], 23)
            self.assertEqual(
                recovered_result["request"]["frozen_material_snapshot"]["sha256"],
                frozen_request["frozen_material_snapshot"]["sha256"],
            )
            self.assertIs(type(recovered_result["candidate"]["quality"]["targets_met"]), bool)
            self.assertIs(
                type(recovered_result["solver_validation"]["validation_targets_met"]), bool)

            repeated = submit_film(self.service, self.request, client_request_id)
            self.assertEqual(repeated["job_id"], job["job_id"])
            self.assertEqual(len(self.transport.submissions), 1)

            changed = deepcopy(self.request)
            changed["max_evaluations"] += 1
            with self.assertRaisesRegex(ValueError, "request_id_conflict"):
                submit_film(self.service, changed, client_request_id)
            self.assertEqual(len(self.transport.submissions), 1)

    def test_default_client_id_is_a_fixed_hash_of_the_frozen_business_request(self):
        with self._scheduler_context():
            values = canonical_film_parameters(
                self.service, {"request": self.request})
            expected = "film:" + digest({
                "kind": "film_optimization", "parameters": values, "input_files": {}})
            first = submit_film(self.service, self.request)
            second = submit_film(self.service, deepcopy(self.request))
            self.assertEqual(first["client_request_id"], expected)
            self.assertEqual(second["client_request_id"], expected)
            self.assertEqual(first["estimate_id"], second["estimate_id"])
            self.assertEqual(first["state"], "prepared")
            self.assertEqual(second["state"], "prepared")
            self.assertFalse(self.transport.submissions)

    def test_unknown_fields_bad_budget_checksum_and_structure_request_never_write(self):
        from coat_sim.core.film_optimization_structure import (
            FilmStructureOptimizationRequest,
            film_structure_request_to_payload,
        )

        values = canonical_film_parameters(self.service, {"request": self.request})
        bad_checksum = deepcopy(values["request"])
        bad_checksum["frozen_material_snapshot"]["sha256"] = "0" * 64
        bad_extra = deepcopy(self.request)
        bad_extra["worker"] = "gpu"
        bad_mode = deepcopy(self.request)
        bad_mode["mode"] = "research"
        bad_approval = deepcopy(self.request)
        bad_approval["approval"] = True
        bad_budget = deepcopy(self.request)
        bad_budget["max_evaluations"] = 0
        structure = film_structure_request_to_payload(FilmStructureOptimizationRequest(
            base_request=deserialize_request(self.request), mode="growth_only"))

        before = self._row_count()
        for name, request in (
            ("worker field", bad_extra),
            ("mode field", bad_mode),
            ("approval field", bad_approval),
            ("bad budget", bad_budget),
            ("bad frozen checksum", bad_checksum),
            ("structure request", structure),
        ):
            with self.subTest(case=name), self.assertRaises(ValueError):
                canonical_film_parameters(self.service, {"request": request})
        self.assertEqual(self._row_count(), before)
        self.assertEqual(self._row_count("queue_jobs"), 0)
        self.assertEqual(self.transport.submissions, [])

    def test_declined_request_is_not_revived_and_new_ids_cannot_claim_pending_or_approved(self):
        from coat_sim.mcp import server

        with self._scheduler_context():
            declined = estimate_film(self.service, self.request)
            prepared = submit_film(self.service, self.request, "declined-client")
            self._record_operate_request("declined-request")
            pending = server.request_compute_confirmation(
                declined["estimate_id"], self.session_id, "declined-request")
            self.assertEqual(pending["state"], "awaiting_confirmation")
            server.confirm_and_submit_compute(
                declined["estimate_id"], False, service_root=self.root)
            with self.assertRaises(ValueError):
                submit_film(self.service, self.request, "declined-client")
            self.assertEqual(
                self.store.read_estimate(declined["estimate_id"])["state"], "declined")
            self.assertFalse(self.transport.submissions)

            # New request IDs may create a fresh estimate, but may not adopt an
            # existing request once it has crossed into pending/approved.
            for suffix, state in (("pending", "pending"), ("approved", "approved")):
                request = deepcopy(self.request)
                request["max_evaluations"] += 1 if suffix == "pending" else 2
                estimated = estimate_film(self.service, request)
                old_id = "owner-" + suffix
                old_receipt = submit_film(self.service, request, old_id)
                origin_request = "origin-" + suffix
                self._record_operate_request(origin_request)
                if state == "pending":
                    server.request_compute_confirmation(
                        old_receipt["estimate_id"], self.session_id,
                        origin_request)
                else:
                    # Trusted store decision creates an approved receipt without
                    # running the scheduler, isolating ID adoption semantics.
                    self.store.request(
                        old_receipt["estimate_id"], session_id=self.session_id,
                        request_id=origin_request)
                    self.store.decide(old_receipt["estimate_id"], True)
                self.assertEqual(
                    self.store.read_estimate(old_receipt["estimate_id"])["state"], state)
                input_digest = self.store.read_estimate(
                    old_receipt["estimate_id"])["input_digest"]
                self.assertIsNone(self.store.claim_unbound_estimate(
                    "unbound-claim-after-" + suffix, input_digest))
                replacement = submit_film(self.service, request, "new-owner-" + suffix)
                self.assertNotEqual(replacement["estimate_id"], estimated["estimate_id"])
                self.assertEqual(
                    self.store.read_estimate(old_receipt["estimate_id"])["state"], state)
            self.assertFalse(self.transport.submissions)

    def test_mcp_cancel_records_request_then_worker_persists_terminal_cancelled(self):
        from coat_sim.mcp import server

        with self._scheduler_context():
            estimate = estimate_film(self.service, self.request)
            prepared = submit_film(
                self.service, self.request, "cancel-client-request")
            self._record_operate_request("cancel-operate-origin")
            pending = server.request_compute_confirmation(
                estimate["estimate_id"], self.session_id, "cancel-operate-origin")
            self.assertEqual(pending["state"], "awaiting_confirmation")
            self.assertEqual(prepared["estimate_id"], estimate["estimate_id"])
            queued = server.confirm_and_submit_compute(
                estimate["estimate_id"], True, service_root=self.root)
            self.assertEqual(queued["state"], "queued")

            self.transport.scheduler_store.transition(
                queued["job_id"], state="running",
                started_at=datetime.now(timezone.utc).isoformat(),
                progress={"fraction": 0.25, "stage": "running"})
            cancelled = self._call_mcp("cancel_calculation_job", {
                "job_id": queued["job_id"]})
            self.assertEqual(cancelled["job_id"], queued["job_id"])
            self.assertEqual(cancelled["state"], "running")
            self.assertTrue(cancelled["cancel_requested"])
            durable = self.transport.scheduler_store.status(queued["job_id"])
            self.assertEqual(durable["state"], "running")
            self.assertTrue(durable["cancel_requested"])

            # The bounded fake worker observes cancellation and records the
            # terminal scheduler state without inventing an optimizer candidate.
            self.transport.scheduler_store.transition(
                queued["job_id"], state="cancelled", error_code="cancelled",
                error="Synthetic worker observed cancellation")
            terminal = self._call_mcp("calculation_job_status", {
                "job_id": queued["job_id"]})
            self.assertEqual(terminal["state"], "cancelled")
            self.assertTrue(terminal["cancel_requested"])
            self.assertIsNone(terminal["result"])
            with self.assertRaisesRegex(ToolError, "job_result_unavailable"):
                self._call_mcp("calculation_job_result", {
                    "job_id": queued["job_id"]})
            self.assertEqual(len(self.transport.submissions), 1)


if __name__ == "__main__":
    unittest.main()
