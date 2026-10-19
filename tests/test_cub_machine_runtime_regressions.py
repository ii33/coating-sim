"""Offline machine-asset clearance and actual runtime-instruction regressions."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import Client
from pydantic_ai.models.test import TestModel

from coat_sim.agent import runtime
from coat_sim.agent.tool_dispatch import result_value
from coat_sim.core.simulation_assets import save_simulation_input_asset
from coat_sim.core.simulator import Settings
from coat_sim.mcp import server
from tests.test_cub_interfaces import fixture


class CubMachineAssetReview(unittest.IsolatedAsyncioTestCase):
    async def test_asset_and_json_board_clearance_are_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, process_path, machine, substrate = fixture(root)
            substrate["outline"]["radius_mm"] = 100.
            service.save("substrate", root / "substrate.csub", substrate, overwrite=True)
            machine.update(planet_offset_mm=312., gear_module_mm=4.,
                           planet_tilt_deg=30., substrate_z_mm=100.)
            machine["masks"] = [{"file": "mask.cmsk"}]
            process = service.load("process", process_path).data
            process["steps"][0]["mask_slots"] = [0]
            with patch.object(server, "service", service):
                async with Client(server.mcp) as client:
                    for board_height, expected_valid in ((75., False), (25., True)):
                        with self.subTest(board_height=board_height):
                            mask = {"z_mm": board_height, "holes": [],
                                    "outer": [[-10., -10.], [10., -10.],
                                              [10., 10.], [-10., 10.]]}
                            service.save("mask", root / "mask.cmsk", mask,
                                         overwrite=(root / "mask.cmsk").exists())
                            service.save("machine", root / "machine.cmac", machine, overwrite=True)
                            service.save("process", process_path, process, overwrite=True)
                            frozen = service.freeze_process(process, base_dir=root)
                            before = deepcopy(frozen)
                            asset = save_simulation_input_asset(root,
                                name=f"board {board_height:g}", settings=asdict(Settings()),
                                input_snapshot=frozen, confirmed=True, process=process,
                                machine=service.load("machine", root / "machine.cmac").runtime)
                            reports = []
                            for path in (root / "machine.cmac", asset["asset_path"]):
                                result = await client.call_tool("check_machine_structure", {
                                    "machine_path": str(path), "substrate_path": str(root / "substrate.csub")})
                                self.assertFalse(result.is_error, str(result.content))
                                reports.append(result_value(result))
                            self.assertEqual(reports[0], reports[1])
                            self.assertEqual(reports[1]["valid"], expected_valid)
                            self.assertAlmostEqual(reports[1]["minimum_substrate_z_mm"], 50.)
                            self.assertFalse(reports[1]["modified"])
                            self.assertEqual(frozen, before)

                    # A portable machine must use its own validated board graph,
                    # including when its original source and board files vanish.
                    (root / "source.csrc").unlink()
                    (root / "mask.cmsk").unlink()
                    portable = service.check_machine_structure(
                        machine_path=asset["asset_path"], substrate_data=substrate)
                    self.assertTrue(portable["valid"], str(portable))
                    self.assertEqual(portable, reports[1])

    async def test_machine_asset_without_process_resolves_boards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, process_path, machine, substrate = fixture(root)
            substrate["outline"]["radius_mm"] = 100.
            service.save("substrate", root / "substrate.csub", substrate, overwrite=True)
            mask = {"z_mm": 75., "outer": [[0., 0.], [10., 0.], [0., 10.]]}
            service.save("mask", root / "mask.cmsk", mask)
            machine.update(planet_tilt_deg=30., masks=[{"file": "mask.cmsk"}])
            service.save("machine", root / "machine.cmac", machine, overwrite=True)
            process = service.load("process", process_path).data
            frozen = service.freeze_process(process, base_dir=root)
            frozen.pop("process_path")
            frozen.pop("runtime")
            frozen["files"] = [row for row in frozen["files"] if row["kind"] != "process"]
            frozen["parameters"] = {"machine_path": "designs/machine.cmac"}
            asset = save_simulation_input_asset(root, name="machine only",
                settings={}, input_snapshot=frozen, confirmed=True)
            report = service.check_machine_structure(machine_path=asset["asset_path"],
                                                       substrate_data=substrate)
            self.assertFalse(report["valid"])
            self.assertIn("mask[0]", report["errors"][0])
            self.assertAlmostEqual(report["minimum_substrate_z_mm"], 50.)


class CubRuntimeMachineInstructions(unittest.IsolatedAsyncioTestCase):
    async def test_actual_main_and_creation_children_receive_engineering_resource(self):
        class InstructionsCaptured(Exception):
            pass

        captured = {}
        def capture_agent(model, **kwargs):
            name = kwargs["name"]
            captured[name] = kwargs["instructions"]
            if name == "banniang":
                raise InstructionsCaptured()
            return MagicMock()

        resource = (runtime.PROJECT_ROOT / "coat_sim/agent/resources/machine_engineering.md").read_text(encoding="utf-8")
        external = MagicMock()
        external.sync = AsyncMock()
        external.configs = []
        writer = MagicMock()
        writer.redact.side_effect = lambda value: value
        command = {"config": {"assistant_enabled": True,
                    "assistant_model": "offline-instructions-test",
                    "assistant_api_url": "http://localhost:12345/v1", "task_mode": "operate"},
                   "session_id": "offline-session", "request_id": "offline-request",
                   "api_key": "", "prompt": "Create and check a machine with the supplied geometry"}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(runtime, "STATE_DIRECTORY", Path(directory)))
            stack.enter_context(patch.object(runtime, "HARNESS_DATABASE", Path(directory) / "harness.sqlite3"))
            stack.enter_context(patch.object(runtime, "_business_server", return_value=(MagicMock(), MagicMock())))
            stack.enter_context(patch.object(runtime, "external_pool", return_value=external))
            stack.enter_context(patch("coat_sim.core.backend_memory.successful_backend_memory", return_value={}))
            stack.enter_context(patch.object(runtime, "_chat_model", return_value=TestModel()))
            stack.enter_context(patch.object(runtime, "AsyncOpenAI"))
            stack.enter_context(patch.object(runtime, "Agent", side_effect=capture_agent))
            stack.enter_context(patch.object(runtime, "SubAgent"))
            stack.enter_context(patch.object(runtime, "SubAgents"))
            with self.assertRaises(InstructionsCaptured):
                await runtime.run_task(command, writer)
        for name in ("banniang", "execute_design", "execute_simulation"):
            with self.subTest(agent=name):
                self.assertIn(resource, captured[name])
                self.assertIn("check_machine_structure(machine_data=..., substrate_data=...)", captured[name])
                self.assertIn("保存后用真实 machine_path、substrate_path 再检查", captured[name])
                self.assertIn("保持用户数值", captured[name])
        # Unrelated delegates are not told to call a tool outside their allowlist.
        self.assertNotIn(resource, captured["execute_windows"])
        self.assertNotIn(resource, captured["execute_correction"])


if __name__ == "__main__":
    unittest.main()
