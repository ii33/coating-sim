"""Synthetic registered-MCP coverage for shared appearance compute.

The only asset in this module is created by the project serializer beneath a
fresh temporary project root. SchedulerClient calls are redirected to a real
SQLite SchedulerStore through its local request protocol; no daemon/network or
user asset is used. The only permitted child process is the fixed
``coat_sim.core.appearance_freeze_worker`` module.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.storage import AssistantStore
from coat_sim.core.api import CoatService
# Load imported factory bindings before any package-level test patch.
from coat_sim.scheduler import submission as _scheduler_submission
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core import film_process_import, simulation_assets
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import simulation_settings
from coat_sim.core.film_process_import import (
    FROZEN_KIND, FROZEN_SCHEMA_VERSION, build_film_process_import,
    validate_frozen_design_snapshot,
)
from coat_sim.core.optical_constants import validate_optical_set
from coat_sim.core.process_profiles import validate_process_profile
from coat_sim.scheduler.client import SchedulerClient as _SchedulerClient
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler.store import SchedulerStore


_ALLOWED_TEMP = Path(tempfile.gettempdir())
_FIELD_MANIFEST = {
    "schema_version": 1,
    "captured": True,
    "field_dtype": "float64",
    "field_units": "nm",
    "coordinates": {"frame": "substrate_local_xy", "x_array": "x",
                    "y_array": "y", "unit": "mm"},
    "material_coordinate": {"axis": "substrate_normal",
                             "origin": "original_substrate_front",
                             "positive_direction": "outward", "unit": "nm"},
    "layer_value_semantics":
        "per_step_growth_increment; cumulative_ordered_sum_is_interface_offset",
    "sequence_order": "process_recipe_order",
    "stack_order": "first_step_is_substrate_adjacent_then_later_steps_overcoat",
    "contributor_semantics": "included_in_total_field",
    "valid_mask": {"semantics": "finite_nodes_are_valid; outside_domain_is_nan",
                   "array_source": "layer_field_*"},
    "contribution_source_slot": None,
}


def _synthetic_optical_set(identifier: str, refractive_index: float) -> dict:
    return validate_optical_set({
        "id": identifier,
        "name": "Synthetic " + identifier,
        "kind": "tabulated",
        "wavelength_unit": "nm",
        "wavelength_min": 500.0,
        "wavelength_max": 600.0,
        "wavelengths": [500.0, 600.0],
        "n": [refractive_index, refractive_index],
        "k": [0.0, 0.0],
    })


def _write_synthetic_v7_asset(root: Path, *, circle: bool = False) -> Path:
    """Create a genuine saved v7 process asset using the official package writer."""
    substrate_profile = {
        "schema_version": 1,
        "name": "Synthetic appearance substrate",
        "surface": {"kind": "flat"},
        "outline": ({"kind": "circle", "radius_mm": 1.0} if circle else
                    {"kind": "rectangle", "width_mm": 2.0, "height_mm": 2.0}),
    }
    design = new_design("Synthetic appearance asset", "appearance-asset-fixture")
    design["substrate"].update({
        "material_id": "appearance-glass",
        "optical_set_id": "appearance-glass-index",
        "thickness": {"value": 1.0, "unit": "mm"},
    })
    design["front_layers"] = [{
        "id": "appearance-layer",
        "material_id": "appearance-film",
        "optical_set_id": "appearance-film-index",
        "thickness": {"value": 100.0, "unit": "nm"},
        "note": "Synthetic test-only captured layer",
    }]
    design = validate_design(design)
    snapshot = {
        "schema_version": FROZEN_SCHEMA_VERSION,
        "kind": FROZEN_KIND,
        "design": design,
        "settings": simulation_settings(),
        "materials": [
            {"id": "appearance-glass", "name": "Synthetic glass",
             "optical_sets": [_synthetic_optical_set("appearance-glass-index", 1.5)],
             "notes": "Synthetic test fixture only"},
            {"id": "appearance-film", "name": "Synthetic film",
             "optical_sets": [_synthetic_optical_set("appearance-film-index", 1.9)],
             "notes": "Synthetic test fixture only"},
        ],
    }
    snapshot["sha256"] = film_process_import._sha256(snapshot)
    snapshot = validate_frozen_design_snapshot(snapshot)

    machine = {"sources": [{"file": "synthetic-source.csource",
                            "profile": {"kind": "point"}}]}
    imported = build_film_process_import(
        snapshot, "front", {"appearance-film": 1}, machine)
    process = validate_process_profile({
        "schema_version": 7,
        "name": "Synthetic appearance process",
        "machine_file": "synthetic-machine.cmachine",
        "substrate_file": "synthetic-substrate.csubstrate",
        **{key: imported[key] for key in (
            "steps", "film_design_snapshot", "film_design_import")},
    })

    x = np.asarray([-1.0, 0.0, 1.0] if circle else [-1.0, 1.0], dtype=np.float64)
    y = x.copy()
    field = (np.where(x[None, :] ** 2 + y[:, None] ** 2 <= 1.0, 100.0, np.nan)
             if circle else np.full((2, 2), 100.0, dtype=np.float64))
    step_id = "synthetic-appearance-step"
    manifest = {
        **_FIELD_MANIFEST,
        "field_shape": list(field.shape),
        "fields": [{
            "step_id": step_id,
            "array_key": "layer_field_00001",
            "step_index": 1,
            "source_slot": 1,
            "target_nm": 100.0,
            "included_in_total": True,
            "completed": True,
            "layer_id": "appearance-layer",
            "surface": "front",
            "material_id": "appearance-film",
        }],
    }
    result = {
        "field": field,
        "field_nm": field,
        "x": x,
        "y": y,
        "layer_fields": {step_id: field.copy()},
        "layer_field_manifest": manifest,
        "step_results": [{
            "step_id": step_id,
            "step_index": 1,
            "source_slot": 1,
            "target_nm": 100.0,
            "completed": True,
            "layer_id": "appearance-layer",
            "surface": "front",
            "material_id": "appearance-film",
        }],
        "effective_settings": {"substrate_profile": substrate_profile},
        "device": "CPU",
        "algorithm": "synthetic test fixture",
    }
    with patch("coat_sim.core.result_visualization.render_result_visualizations",
               return_value={"files": []}):
        prepared = simulation_assets.prepare_simulation_asset(
            root, result=result,
            settings={"substrate_profile": substrate_profile},
            machine=machine, process=process, elapsed_s=0.0,
            requested_device="CPU", record_history=False)
    asset_path = root / "data" / "assets" / "synthetic-appearance.coatasset"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    simulation_assets.save_simulation_asset(
        root, prepared["run_id"], "synthetic appearance", confirmed=True,
        path=asset_path, overwrite=False, include_results=True)
    return asset_path


def _render_inputs() -> dict:
    return {
        "wavelength_nm": [500.0, 600.0],
        "camera": {"position_mm": [0.0, 0.0, -20.0],
                   "target_mm": [0.0, 0.0, 0.0],
                   "up": [0.0, 1.0, 0.0], "fov_y_deg": 35.0},
        "area_source": {"shape": "rectangle",
                        "position_mm": [0.0, 0.0, 30.0],
                        "normal": [0.0, 0.0, -1.0],
                        "up": [0.0, 1.0, 0.0],
                        "size_mm": [20.0, 20.0],
                        "spectrum": {"wavelength_nm": [500.0, 600.0],
                                     "value": [1.0, 1.0], "kind": "relative"}},
        "environment_spectrum": {"wavelength_nm": [500.0, 600.0],
                                 "value": [0.0, 0.0], "kind": "relative"},
        "resolution": {"width": 1, "height": 1, "spp": 1, "seed": 0},
    }


class _SyntheticSchedulerTransport:
    """Protocol-shaped local peer backed by the real persistent SchedulerStore."""

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
            submitted = self.scheduler_store.submit(**arguments)
            spec = JobSpec.parse_frozen(arguments["spec"])
            spec_path = (self.root / "artifacts" / "runtime" / "scheduler" /
                         "jobs" / submitted["job_id"] / "spec.json")
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            if not spec_path.exists():
                # Scheduler.start_job persists the exact frozen spec before it
                # starts a worker. Reproduce that file protocol without a daemon.
                spec_path.write_text(spec.canonical_json, encoding="utf-8")
            elif JobSpec.parse_frozen_json(spec_path.read_text(encoding="utf-8")).fingerprint != spec.fingerprint:
                raise AssertionError("synthetic queue spec changed for an existing job")
            return submitted
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
            from coat_sim.scheduler.result_capability import (
                job_has_viewable_result, validate_appearance_render_result,
            )
            if not job_has_viewable_result(job):
                if job.get("state") in ("failed", "cancelled"):
                    raise ValueError("job_result_unavailable: no verified result is available")
                if job.get("state") == "succeeded" and "result" not in job:
                    raise ValueError("job_result_missing: successful job has no result payload")
                if job.get("state") == "succeeded":
                    raise ValueError("job_result_invalid_shape: expected a result object")
                raise ValueError("job_not_complete: " + str(job.get("state")))
            if result.get("kind") == "appearance_render":
                validate_appearance_render_result(
                    self.root, job["job_id"], result,
                    expected_input_digest=job.get("input_digest"),
                    require_registered_artifact=True)
            return result
        raise AssertionError(f"unexpected scheduler protocol action: {action}")


class AppearanceComputeEntrypointTests(unittest.TestCase):
    def setUp(self):
        if not _ALLOWED_TEMP.is_dir():
            self.skipTest(f"authorized isolated temp directory is unavailable: {_ALLOWED_TEMP}")
        self._old_tempdir = tempfile.tempdir
        self._old_environ = {key: os.environ.get(key) for key in ("TEMP", "TMP", "LOCALAPPDATA")}
        self.scratch = Path(tempfile.mkdtemp(prefix="appearance-entrypoints-",
                                             dir=_ALLOWED_TEMP)).resolve()
        for key in ("TEMP", "TMP"):
            os.environ[key] = str(self.scratch)
        os.environ["LOCALAPPDATA"] = str(self.scratch)
        tempfile.tempdir = str(self.scratch)
        self.addCleanup(self._restore_temp_environment)

        self.root = self.scratch / "project"
        self.root.mkdir()
        self.asset_path = _write_synthetic_v7_asset(self.root)
        self.asset_sha256 = hashlib.sha256(self.asset_path.read_bytes()).hexdigest()
        self.request = {
            "asset_path": str(self.asset_path),
            "asset_sha256": self.asset_sha256,
            "render_inputs": _render_inputs(),
        }
        self.service = CoatService(self.root)
        self.compute_store = ComputeTaskStore(self.root)
        self.transport = _SyntheticSchedulerTransport(self.root)
        self._allowed_freeze_children: list[tuple[str, ...]] = []
        self.assistant = AssistantStore(self.root / "artifacts/assistant/state.sqlite3")
        self.session_id = self.assistant.new_session("synthetic appearance request")
        self.request_id = "synthetic-appearance-operate-request"
        self.assistant.set_ui_session_mode(self.session_id, "operate")
        self.assistant.bind_ui_request(self.session_id, self.request_id, "operate")
        self.assistant.record_event(self.session_id, self.request_id, "progress",
                                    {"stage": "received", "mode": "operate"})

    def _restore_temp_environment(self):
        tempfile.tempdir = self._old_tempdir
        for key, value in self._old_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # This is exactly the one fresh scratch root created by this test case.
        if self.scratch.exists():
            shutil.rmtree(self.scratch)

    def _client_factory(self, root=None, **_kwargs):
        return _SchedulerClient(root or self.root, transport=self.transport)

    def _guarded_popen(self, args, *positional, **kwargs):
        command = tuple(str(value) for value in args)
        is_python_freeze = (len(command) == 7 and command[1:3] == (
            "-m", "coat_sim.core.appearance_freeze_worker") and
            command[3] == "--input" and command[5] == "--output")
        is_frozen_freeze = (len(command) == 7 and command[1:3] == (
            "--coat-worker", "appearance-freeze") and
            command[3] == "--input" and command[5] == "--output")
        if not (is_python_freeze or is_frozen_freeze):
            raise AssertionError(f"unexpected child process is forbidden in this test: {command!r}")
        if (is_python_freeze or is_frozen_freeze) and (
                not Path(command[4]).is_relative_to(self.scratch) or
                not Path(command[6]).is_relative_to(self.scratch)):
            raise AssertionError("freeze child inputs/outputs must stay in this test's fresh scratch root")
        self._allowed_freeze_children.append(command)
        return _REAL_POPEN(args, *positional, **kwargs)

    def _scheduler_context(self, *, service=None):
        from coat_sim.mcp import server
        stack = ExitStack()
        stack.enter_context(patch.object(server, "service", service or self.service))
        stack.enter_context(patch("subprocess.Popen", side_effect=self._guarded_popen))
        stack.enter_context(patch("coat_sim.scheduler.SchedulerClient",
                                  side_effect=self._client_factory))
        stack.enter_context(patch("coat_sim.scheduler.submission.SchedulerClient",
                                  side_effect=self._client_factory))
        return stack

    def _row_count(self, table="estimates"):
        with self.compute_store.connection() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _call_mcp(self, name, arguments):
        from coat_sim.mcp import server
        return self._mcp_value(asyncio.run(server.mcp.call_tool(name, arguments)))

    def _estimate_and_prepare(self, client_request_id):
        estimate = self._call_mcp("estimate_appearance_render", {
            "request": self.request})
        prepared = self._call_mcp("submit_appearance_render", {
            "request": self.request,
            "client_request_id": client_request_id,
        })
        self.assertEqual(prepared["estimate_id"], estimate["estimate_id"])
        self.assertEqual(prepared["state"], "prepared")
        self.assertTrue(prepared["needs_release_request"])
        self.assertEqual(prepared["next_action"], "request_compute_confirmation")
        self.assertEqual(self.transport.submissions, [])
        return estimate, prepared

    def _release_manually(self, estimate_id, origin_request_id):
        from coat_sim.mcp import server
        self.assistant.set_ui_session_mode(self.session_id, "operate")
        self.assistant.bind_ui_request(self.session_id, origin_request_id, "operate")
        self.assistant.record_event(
            self.session_id, origin_request_id, "progress",
            {"stage": "received", "mode": "operate"})
        pending = server.request_compute_confirmation(
            estimate_id, self.session_id, origin_request_id)
        self.assertEqual(pending["state"], "awaiting_confirmation")
        self.assertEqual(pending["release_policy"], "countdown")
        self.assertFalse(pending["needs_user_confirmation"])
        self.assertIsNone(pending["estimated_s"])
        self.assertEqual(pending["sample_count"], 0)
        self.assertEqual(self.transport.submissions, [])
        accepted = server.confirm_and_submit_compute(
            estimate_id, True, service_root=self.root)
        self.assertEqual(accepted["state"], "queued")
        return accepted

    @staticmethod
    def _mcp_value(value):
        structured = getattr(value, "structuredContent", None)
        if structured is None:
            structured = getattr(value, "structured_content", None)
        if structured is not None:
            return structured
        for block in getattr(value, "content", ()) or ():
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    return json.loads(text)
                except (TypeError, ValueError):
                    pass
        return value

    def test_registered_mcp_tools_are_strict_and_rejections_do_not_write(self):
        from coat_sim.mcp import server

        with self._scheduler_context():
            tools = asyncio.run(server.mcp.list_tools())
            by_name = {tool.name: tool for tool in tools}
            estimate_tool = by_name["estimate_appearance_render"]
            submit_tool = by_name["submit_appearance_render"]

            def schema(tool):
                result = getattr(tool, "inputSchema", None)
                return result if result is not None else tool.input_schema

            estimate_schema, submit_schema = schema(estimate_tool), schema(submit_tool)
            self.assertFalse(estimate_schema["additionalProperties"])
            self.assertEqual(estimate_schema["required"], ["request"])
            self.assertEqual(set(estimate_schema["properties"]), {"request"})
            self.assertFalse(submit_schema["additionalProperties"])
            self.assertEqual(submit_schema["required"], ["request"])
            self.assertEqual(set(submit_schema["properties"]), {"request", "client_request_id"})

            before = self._row_count()
            invalid_arguments = (
                {"request": self.request, "approve": True},
                {"request": {**self.request, "worker": "local"}},
                {"request": {**self.request, "route": "remote"}},
                {"request": {**self.request, "asset_sha256": "0" * 64}},
                {"request": {**self.request, "asset_path": str(
                    self.root / "outside.coatasset")}},
                {"request": {**self.request, "render_inputs": {
                    **self.request["render_inputs"], "output_path": "forbidden"}}},
                {"request": {**self.request, "oversized": "x" * 16_000_001}},
            )
            for arguments in invalid_arguments:
                with self.subTest(arguments=sorted(arguments)):
                    with self.assertRaises(ToolError):
                        asyncio.run(server.mcp.call_tool("submit_appearance_render", arguments))
                    self.assertEqual(self._row_count(), before)
            self.assertEqual(self.transport.submissions, [])

    def test_real_v7_asset_freeze_child_and_unknown_runtime_estimate(self):
        from coat_sim.core.appearance_compute import _normalize_public_request, validate_frozen
        from coat_sim.core.appearance_freeze_worker import build_frozen_envelope
        from coat_sim.mcp import server

        content = simulation_assets.read_simulation_asset(
            self.asset_path, detail="full", include_arrays=True)
        self.assertTrue(content["has_results"])
        self.assertEqual(content["configuration"]["process"]["schema_version"], 7)
        self.assertEqual(content["result_metadata"]["layer_field_manifest"]["captured"], True)
        self.assertEqual(set(content["arrays"]["layer_fields"]), {"synthetic-appearance-step"})

        # Run the same fixed worker logic in-process first so fixture failures
        # retain a useful traceback; the registered MCP call below separately
        # proves the allowlisted isolated-child entrypoint.
        canonical = _normalize_public_request(self.root.resolve(), self.request,
                                              service=self.service)
        direct_envelope = build_frozen_envelope(self.root, canonical)
        self.assertEqual(direct_envelope["job"]["resolution"], canonical["render_inputs"]["resolution"])

        with self._scheduler_context():
            raw = asyncio.run(server.mcp.call_tool(
                "estimate_appearance_render", {"request": self.request}))
            receipt = self._mcp_value(raw)
            self.assertIsNone(receipt["estimated_s"])
            self.assertEqual(receipt["sample_count"], 0)
            stored = self.compute_store.read_estimate(receipt["estimate_id"])
            frozen = validate_frozen(self.service, stored["parameters"]["frozen"])
            self.assertEqual(frozen["request"]["asset_sha256"], self.asset_sha256)
            self.assertEqual(frozen["job"]["resolution"], self.request["render_inputs"]["resolution"])
            self.assertEqual(frozen["job"]["substrate"]["solid_thickness_mm"], 1.0)
            self.assertEqual(len(self._allowed_freeze_children), 1)
            self.assertEqual(self.transport.submissions, [])

    def test_asset_source_change_after_estimate_is_rejected_without_queue_write(self):
        with self._scheduler_context():
            estimate = self._call_mcp("estimate_appearance_render", {
                "request": self.request})
            self.asset_path.write_bytes(self.asset_path.read_bytes() + b"changed")
            with self.assertRaises(ToolError):
                self._call_mcp("submit_appearance_render", {
                    "request": self.request,
                    "client_request_id": "appearance-source-changed",
                })
            self.assertEqual(
                self.compute_store.read_estimate(estimate["estimate_id"])["state"],
                "estimated")
            self.assertEqual(self._row_count("queue_jobs"), 0)
            self.assertEqual(self.transport.submissions, [])

    def test_default_client_id_is_stable_and_changed_request_conflicts(self):
        with self._scheduler_context():
            first = self._call_mcp("submit_appearance_render", {
                "request": self.request})
            second = self._call_mcp("submit_appearance_render", {
                "request": deepcopy(self.request)})
            self.assertEqual(first["state"], "prepared")
            self.assertEqual(second["state"], "prepared")
            self.assertTrue(first["client_request_id"].startswith("appearance:"))
            self.assertEqual(first["client_request_id"], second["client_request_id"])
            self.assertEqual(first["estimate_id"], second["estimate_id"])
            before = self._row_count()

            changed = deepcopy(self.request)
            changed["render_inputs"]["resolution"]["seed"] = 1
            with self.assertRaisesRegex(ToolError, "request_id_conflict"):
                self._call_mcp("submit_appearance_render", {
                    "request": changed,
                    "client_request_id": first["client_request_id"],
                })
            self.assertEqual(self._row_count(), before)
            self.assertEqual(self._row_count("queue_jobs"), 0)
            self.assertEqual(self.transport.submissions, [])

    def test_registered_mcp_manual_release_real_render_and_verified_result(self):
        from coat_sim.mcp import server
        from coat_sim.core.appearance_execution import execute_appearance
        from coat_sim.scheduler.daemon import Scheduler as SchedulerDaemon

        with self._scheduler_context():
            estimate, _prepared = self._estimate_and_prepare(
                "appearance-success-client")
            job = self._release_manually(
                estimate["estimate_id"], "appearance-success-chat")
            self.assertEqual(len(self.transport.submissions), 1)
            submitted = self.transport.submissions[0]
            self.assertEqual(submitted["metadata"]["estimate_id"], estimate["estimate_id"])
            self.assertEqual(submitted["spec"]["executor"], "appearance_render")
            self.assertEqual(set(submitted["spec"]["parameters"]), {"frozen"})
            frozen = submitted["spec"]["parameters"]["frozen"]
            self.assertNotIn("worker", frozen["request"])
            self.assertNotIn("route", frozen["request"])
            self.assertNotIn("approval", frozen["request"])

            found = self._call_mcp("find_calculation_job", {
                "client_request_id": "appearance-success-client"})
            self.assertEqual(found["job"]["job_id"], job["job_id"])
            self.assertEqual(found["job"]["state"], "queued")
            queued = self._call_mcp("calculation_job_status", {"job_id": job["job_id"]})
            self.assertEqual(queued["state"], "queued")
            with self.assertRaisesRegex(ToolError, "job_not_complete: queued"):
                self._call_mcp("calculation_job_result", {"job_id": job["job_id"]})

            # The local synthetic peer persists running/succeeded state in the
            # real queue DB. Run the actual executor and renderer in-process;
            # Popen remains hard-guarded to the fixed freeze child only.
            mitsuba_site = (Path(__file__).resolve().parents[1] / "build" /
                            "mitsuba_appearance_env" / "Lib" / "site-packages")
            if not mitsuba_site.is_dir():
                self.skipTest("vendored Mitsuba runtime is unavailable")
            if str(mitsuba_site) not in sys.path:
                sys.path.insert(0, str(mitsuba_site))
            try:
                import mitsuba  # noqa: F401
            except Exception as exc:
                self.skipTest(f"vendored Mitsuba cannot initialize here: {exc}")

            self.transport.scheduler_store.transition(
                job["job_id"], state="running",
                progress={"fraction": 0.01, "stage": "running"})
            progress = []
            result = execute_appearance(
                self.root, job["job_id"], submitted["spec"]["parameters"],
                Event(), progress.append)
            self.assertEqual(result["kind"], "appearance_render")
            self.assertEqual(result["resolution"], {"width": 1, "height": 1, "spp": 1,
                                                       "seed": 0})
            self.assertEqual(result["wavelength_nm"], [500.0, 600.0])
            self.assertTrue(progress)

            # This is the same narrow result-file registration performed by
            # Scheduler.register_artifacts, without constructing a Scheduler.
            result = SchedulerDaemon.register_artifacts(
                SimpleNamespace(root=self.root), job["job_id"], result)
            self.transport.scheduler_store.transition(
                job["job_id"], state="succeeded", result=result,
                progress={"fraction": 1.0, "stage": "complete"})
            terminal = self._call_mcp("calculation_job_status", {
                "job_id": job["job_id"]})
            self.assertEqual(terminal["state"], "succeeded")
            self.assertEqual(terminal["result"]["kind"], "appearance_render")
            recovered = self._call_mcp("calculation_job_result", {
                "job_id": job["job_id"]})
            self.assertEqual(recovered, result)
            self.assertEqual(recovered["job_id"], job["job_id"])
            self.assertEqual(recovered["resolution"], {"width": 1, "height": 1, "spp": 1,
                                                          "seed": 0})
            self.assertEqual(recovered["wavelength_nm"], [500.0, 600.0])
            self.assertEqual(len(recovered["input_digest"]), 64)
            self.assertTrue(recovered["result_artifacts"])
            self.assertEqual(recovered["result_artifacts"][0]["role"], "result")

            self.assertEqual(len(self.transport.submissions), 1)

    def test_mcp_running_cancel_stays_nonterminal_until_worker_observes_it(self):
        with self._scheduler_context():
            estimate, _prepared = self._estimate_and_prepare(
                "appearance-cancel-client")
            job = self._release_manually(
                estimate["estimate_id"], "appearance-cancel-chat")
            self.transport.scheduler_store.transition(
                job["job_id"], state="running",
                progress={"fraction": 0.2, "stage": "rendering"})

            retry = self._call_mcp("submit_appearance_render", {
                "request": self.request,
                "client_request_id": "appearance-cancel-client",
            })
            self.assertEqual(retry["job_id"], job["job_id"])
            self.assertEqual(retry["state"], "running")
            self.assertEqual(len(self.transport.submissions), 1)

            cancel_result = self._call_mcp("cancel_calculation_job", {
                "job_id": job["job_id"]})
            self.assertEqual(cancel_result["state"], "running")
            self.assertTrue(cancel_result["cancel_requested"])
            live = self._call_mcp("calculation_job_status", {
                "job_id": job["job_id"]})
            self.assertEqual(live["state"], "running")
            self.assertTrue(live["cancel_requested"])
            output = self.root / "data" / "tmp" / "appearance" / job["job_id"] / "render"
            self.assertFalse(output.exists())

            # No candidate/output is fabricated: the fake worker records only
            # the terminal cancellation observed at a render boundary.
            self.transport.scheduler_store.transition(
                job["job_id"], state="cancelled", error_code="cancelled",
                error="Synthetic worker observed cancellation")
            terminal = self._call_mcp("calculation_job_status", {
                "job_id": job["job_id"]})
            self.assertEqual(terminal["state"], "cancelled")
            self.assertTrue(terminal["cancel_requested"])
            self.assertIsNone(terminal["result"])
            with self.assertRaisesRegex(ToolError, "job_result_unavailable"):
                self._call_mcp("calculation_job_result", {"job_id": job["job_id"]})
            self.assertFalse(output.exists())
            self.assertEqual(len(self.transport.submissions), 1)


_REAL_POPEN = subprocess.Popen


if __name__ == "__main__":
    unittest.main()
