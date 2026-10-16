"""Offline cub contracts: real MCP transport, CPU only, no QApplication or models."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("MPLBACKEND", "Agg")
from mcp import Client
from coat_sim.core.api import CoatService, canonical_compute_parameters
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core.materials import default_material_profile
from coat_sim.core.result_summary import algorithm_result_metadata
from coat_sim.core.simulator import Settings
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler import SchedulerClient
from coat_sim.agent.runtime import MAIN_TOOLS, READ_TOOLS, EXECUTION_TOOLS
from coat_sim.agent.tool_dispatch import ProjectGateway, result_value
from coat_sim.mcp import server


def fixture(root):
    service = CoatService(root=root)
    service.save("source", root / "source.csrc", {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1.})
    substrate = {"schema_version": 1, "name": "cub test", "outline": {"kind": "circle", "radius_mm": 10.}, "surface": {"kind": "flat"}}
    service.save("substrate", root / "substrate.csub", substrate)
    machine = {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0., "planet_offset_mm": 0., "planet_tilt_deg": 0., "substrate_z_mm": 100., "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 0., "sources": [{"file": "source.csrc", "x_mm": 0., "y_mm": 0., "z_mm": 0.}], "masks": [], "monitors": []}
    service.save("machine", root / "machine.cmac", machine)
    process = {"schema_version": 6, "name": "cub CPU contract", "machine_file": "machine.cmac", "substrate_file": "substrate.csub", "steps": [{"source_slot": 1, "target_nm": 1., "rate_nm_s": 1., "target_mode": "mean", "mask_slots": []}]}
    path = root / "process.cproc"
    service.save("process", path, process)
    return service, path, machine, substrate


def optics(identifier="sample", unit="nm"):
    wave = [400., 800.] if unit == "nm" else [.4, .8]
    return {"id": identifier, "name": identifier, "kind": "tabulated", "wavelength_unit": unit,
            "wavelength_min": wave[0], "wavelength_max": wave[1], "wavelengths": wave, "n": [1.5, 1.6], "k": None}


def test_algorithm_job_metadata_counts_trace_without_publishing_it():
    result = {"algorithm_report": {"algorithm": "optimize", "status": "completed",
              "evaluations": 10_000, "trace": [{"evaluation": i} for i in range(10_000)]}}
    metadata = algorithm_result_metadata(result)
    report = metadata["algorithm_report"]
    assert report["algorithm"] == "optimize"
    assert report["evaluations"] == report["trace_count"] == 10_000
    assert "trace" not in report
    assert len(json.dumps(metadata)) < 4096


class GatewayClient:
    """Normalize the installed SDK list envelope; every call still uses real MCP transport."""
    def __init__(self, client):
        self.client = client

    async def list_tools(self):
        result = await self.client.list_tools()
        return result.tools if hasattr(result, "tools") else result

    async def call_tool_mcp(self, name, arguments):
        return await self.client.call_tool(name, arguments)

def connected_client(method):
    async def wrapped(self):
        asyncio.get_running_loop().set_debug(False)
        async with Client(server.mcp, read_timeout_seconds=12) as self.client:
            await method(self)
    wrapped.__name__ = method.__name__
    return wrapped


class CubMCPContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service, self.path, self.machine, self.substrate = fixture(self.root)
        self.service_patch = patch.object(server, "service", self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        # MCP queue calls start a detached scheduler for this temporary root.
        # Its idle claim loop can recreate runtime files while rmtree runs, so
        # join the fixture-owned daemon before removing its storage.  unittest
        # runs cleanups in reverse registration order.
        self.addCleanup(self.stop_owned_scheduler)

    def stop_owned_scheduler(self):
        process = SchedulerClient._processes.get(str(self.root))
        if process is None:
            return
        client = SchedulerClient(self.root)
        client.stop()
        self.assertIsNotNone(process.poll(), "fixture scheduler still writes to its temporary root")
        self.assertFalse(client.endpoint.exists(), "fixture scheduler endpoint remains after stop")

    async def call(self, name, arguments):
        result = await asyncio.wait_for(self.client.call_tool(name, arguments), 15)
        self.assertFalse(result.is_error, str(result.content))
        return result_value(result)

    async def error(self, name, arguments):
        result = await asyncio.wait_for(self.client.call_tool(name, arguments), 15)
        self.assertTrue(result.is_error, str(result.content))
        return str(result.content)

    @connected_client
    async def test_registration_ai_allowlists_and_errors(self):
        tools = await self.client.list_tools()
        tools = tools.tools if hasattr(tools, "tools") else tools
        names = {tool.name for tool in tools}
        print("CUB_REAL_MCP_TOOLS=" + str(len(names)) + "; AI_MAIN_TOOLS=" + str(len(MAIN_TOOLS)) + "; AI_READ_TOOLS=" + str(len(READ_TOOLS)), flush=True)
        expected = {"list_material_library", "read_material", "create_material", "copy_material", "update_material", "validate_material_optics", "export_simulation_asset", "check_machine_structure"}
        self.assertLessEqual(expected, names)
        self.assertLessEqual(expected, MAIN_TOOLS)
        for group in ("design", "simulation"):
            self.assertLessEqual(expected, EXECUTION_TOOLS[group])
        self.assertNotIn("delete_material", names)
        submit = next(tool for tool in tools if tool.name == "submit_process_recipe")
        self.assertIn("algorithm", submit.input_schema["properties"])
        self.assertIn("algorithm_options", submit.input_schema["properties"])
        await self.error("submit_process_recipe", {"settings": {"grid_size": 21, "time_steps": 4}, "process_path": str(self.path), "algorithm": "invalid"})
        await self.error("submit_process_recipe", {"settings": {"grid_size": 21, "time_steps": 4}, "process_path": str(self.path), "algorithm": "reference", "backend": "GPU"})

    @connected_client
    async def test_material_atomicity_roles_optical_domains_and_builtin(self):
        listing = await self.call("list_material_library", {"limit": 1})
        self.assertEqual(len(listing["items"]), 1)
        builtin = listing["items"][0]["id"]
        original = await self.call("read_material", {"identifier": builtin})
        await self.error("update_material", {"profile": original["record"]})
        copied = await self.call("copy_material", {"identifier": builtin, "new_identifier": "cub_copy", "name": "cub copy"})
        self.assertEqual(copied["record"].get("roles"), original["record"].get("roles"))
        profile = default_material_profile("cub custom", "cub_custom")
        profile.update(roles=["film"], optical_constants=[optics(), optics("second", "um")])
        await self.call("create_material", {"profile": profile})
        path = self.root / "data/materials/cub_custom.json"
        before = path.read_bytes()
        bad = deepcopy(profile)
        bad["optical_constants"][0]["wavelength_unit"] = "angstrom"
        await self.error("update_material", {"profile": bad})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob("*.tmp")))
        self.assertEqual((await self.call("read_material", {"identifier": builtin}))["record"], original["record"])
        validation = await self.call("validate_material_optics", {"datasets": profile["optical_constants"], "wavelength": 600., "wavelength_unit": "nm"})
        self.assertTrue(all(row["k"] is None for row in validation["evaluations"]))
        await self.error("validate_material_optics", {"datasets": profile["optical_constants"], "wavelength": 900., "wavelength_unit": "nm"})

    @connected_client
    async def test_real_queue_all_algorithm_paths_and_frozen_report(self):
        for algorithm in ("fast", "reference", "optimize"):
            options = {"budget_s": 3.}
            if algorithm != "fast":
                options["max_evaluations"] = 1
            if algorithm == "reference":
                options.update(sampling_policy="requested", periodic_integration=False)
            args = {"settings": {"grid_size": 21, "time_steps": 4, "carrier_rpm": 0.}, "process_path": str(self.path), "backend": "CPU", "fast_cycle": False, "algorithm": algorithm, "algorithm_options": options}
            receipt = await self.call("submit_process_recipe", args)
            store = ComputeTaskStore(self.root)
            stored = store.read_estimate(receipt["estimate_id"])
            self.assertEqual(stored["parameters"]["algorithm"], algorithm)
            store.request(receipt["estimate_id"])
            job = server.confirm_and_submit_compute(receipt["estimate_id"], True)
            deadline = time.monotonic() + 18
            while job["state"] in ("queued", "running") and time.monotonic() < deadline:
                await asyncio.sleep(.05)
                job = server.calculation_job_status(job["job_id"])
            self.assertEqual(job["state"], "succeeded", str(job))
            report = job["result"]["metadata"]["algorithm_report"]
            self.assertEqual(report["algorithm"], algorithm)
            self.assertEqual(report["trace_count"], 1)
            self.assertNotIn("trace", report)
            self.assertLess(len(json.dumps(job)), 128 * 1024)
            # The public status deliberately omits private filesystem paths.
            # Check the durable frozen scheduler spec at its SQLite boundary.
            self.assertNotIn("input_snapshot_file", job)
            with store.connection() as connection:
                row = connection.execute("SELECT spec FROM scheduler_specs WHERE job_id=?",
                                         (job["job_id"],)).fetchone()
            self.assertIsNotNone(row)
            frozen = JobSpec.parse_frozen_json(row["spec"])
            self.assertEqual(frozen.fingerprint, job["input_digest"])
            self.assertEqual(frozen.parameters["algorithm_options"], options)
            self.assertIn("algorithm=" + algorithm, job["summary"])
            full = await self.call("read_calculation_result", {"run_id": job["result"]["run_id"], "detail": "full"})
            self.assertEqual(full["metadata"]["algorithm_report"]["evaluations"], 1)

    @connected_client
    async def test_machine_pre_post_review_keeps_values(self):
        machine = deepcopy(self.machine)
        machine.update(planet_offset_mm=312., gear_module_mm=4., substrate_z_mm=1000.)
        before = deepcopy(machine)
        result = await self.call("check_machine_structure", {"machine_data": machine, "substrate_data": self.substrate})
        self.assertTrue(result["valid"], str(result))
        self.assertAlmostEqual(result["absolute_spin_per_carrier"], 1 + 120 / 36)
        self.assertEqual(machine, before)
        machine["planet_offset_mm"] = 300.
        result = await self.call("check_machine_structure", {"machine_data": machine})
        self.assertFalse(result["valid"])
        result = await self.call("check_machine_structure", {"machine_path": str(self.root / "machine.cmac"), "substrate_path": str(self.root / "substrate.csub")})
        self.assertTrue(result["valid"], str(result))
        self.assertIsNotNone(result["minimum_substrate_z_mm"])

    @connected_client
    async def test_cancellation_and_legacy_identity(self):
        args = {"settings": {"grid_size": 21, "time_steps": 4, "carrier_rpm": 0.}, "process_path": str(self.path), "backend": "CPU"}
        from coat_sim.core.compute_estimation import canonical_parameters
        self.assertEqual(canonical_compute_parameters(self.service, "process", args), canonical_parameters(self.service, "process", args))
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            self.service.simulate_recipe(args["settings"], process_path=self.path, backend="CPU", cancel_event=event, algorithm="reference", algorithm_options={"budget_s": 1., "max_evaluations": 1, "sampling_policy": "requested"})
        receipt = await self.call("submit_process_recipe", args)
        ComputeTaskStore(self.root).request(receipt["estimate_id"])
        refused = server.confirm_and_submit_compute(receipt["estimate_id"], False)
        self.assertEqual(refused["state"], "declined")


    @connected_client
    async def test_asset_export_gateway_permissions_and_material_snapshot(self):
        from coat_sim.core import data_files
        from coat_sim.core.simulation_assets import save_simulation_input_asset
        profile = default_material_profile("snapshot original", "cub_snapshot")
        profile.update(roles=["film"], optical_constants=[optics()])
        await self.call("create_material", {"profile": profile})
        source = self.service.load("source", self.root / "source.csrc").data
        source["material"] = profile
        self.service.save("source", self.root / "source.csrc", source, overwrite=True)
        process = self.service.load("process", self.path).data
        frozen = self.service.freeze_process(process, base_dir=self.root)
        asset = save_simulation_input_asset(self.root, name="cub snapshot", settings=asdict(Settings()),
            input_snapshot=frozen, confirmed=True, process=process,
            machine=self.service.load("machine", self.root / "machine.cmac").runtime)
        profile["name"] = "updated library"
        await self.call("update_material", {"profile": profile})
        content = await self.call("read_simulation_asset", {"path": asset["asset_path"], "detail": "full"})
        embedded = next(row["data"]["material"] for row in content["designs"] if row["kind"] == "source")
        self.assertEqual(embedded["name"], "snapshot original")
        self.assertIsNone(embedded["optical_constants"][0]["k"])
        destination = self.root / "data/exported"
        arguments = {"path": asset["asset_path"], "destination": str(destination)}
        with patch.object(data_files, "PROJECT_ROOT", self.root), patch.object(data_files, "DATA_ROOT", self.root / "data"):
            denied = ProjectGateway(GatewayClient(self.client), READ_TOOLS, READ_TOOLS, lambda text: text)
            refusal = await denied.execute("export_simulation_asset", arguments)
            self.assertEqual(refusal["error"]["code"], "unavailable_tool")
            self.assertFalse(destination.exists())
            gateway = ProjectGateway(GatewayClient(self.client), MAIN_TOOLS, READ_TOOLS, lambda text: text)
            exported = await gateway.execute("export_simulation_asset", arguments)
            self.assertTrue(exported.get("exported"), str(exported))
            self.assertTrue(all(Path(path).is_file() for path in exported["files"]))
            before = {path: Path(path).read_bytes() for path in exported["files"]}
            await self.error("export_simulation_asset", arguments)
            self.assertEqual(before, {path: Path(path).read_bytes() for path in exported["files"]})
            for members in (["../escape.json"], ["private.py"]):
                await self.error("export_simulation_asset", {**arguments, "destination": str(self.root / "data/invalid"), "members": members})
            self.assertFalse((self.root / "data/invalid").exists())
            await self.error("export_simulation_asset", {**arguments, "destination": str(self.root / "outside-data")})
            self.assertFalse((self.root / "outside-data").exists())
        custom_path = self.root / "data/materials/cub_snapshot.json"
        unchanged = custom_path.read_bytes()
        profile["name"] = "failed replacement"
        with patch("coat_sim.core.materials.os.replace", side_effect=OSError("injected publication failure")):
            with self.assertRaises(OSError):
                self.service.material_library().write(profile, update=True)
        self.assertEqual(custom_path.read_bytes(), unchanged)
        self.assertFalse(list(custom_path.parent.glob("*.tmp")))

if __name__ == "__main__":
    unittest.main()
