import csv
import copy
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

from coat_sim.agent.runtime import EXECUTION_TOOLS, MAIN_TOOLS, READ_TOOLS
from coat_sim.agent.tool_dispatch import ProjectGateway
from coat_sim.mcp import server
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.api import CoatService
from coat_sim.core.film_materials import FilmMaterialLibrary
from mcp.server.mcpserver.exceptions import ToolError


class _Service:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.calls = []

    def _path(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("path_outside_root")
        return path

    def new_film_design(self):
        return new_design()

    def film_material_options(self, purpose="film"):
        return [{"id": "m1", "name": "Material", "readonly": True, "design_ready": True,
                 "notes": "metadata", "optical_sets": [{"id": "o1", "name": "Optical",
                 "kind": "tabulated", "wavelength_unit": "nm", "wavelength_min": 400,
                 "wavelength_max": 700, "source": "fixture", "notes": "ok",
                 "n": [1.5, 1.6], "k": [0.0, 0.01]}]}]

    def validate_film_design(self, data):
        self.calls.append("validate")
        return dict(data)

    def load_film_design(self, path):
        return {"name": "loaded"}

    def save_film_design(self, path, data, *, overwrite=False):
        return {"path": str(self._path(path)), "overwrite": overwrite}

    def expand_film_expression(self, expression, symbols):
        return [{"expression": expression, "symbols": symbols}]

    def simulate_film_design(self, data, settings=None):
        self.calls.append("simulate")
        return {"design": data, "settings": {"samples": 2, "wavelength_min_nm": 400,
                "wavelength_max_nm": 700}, "provenance": [{"id": "m1"}],
                "wavelength_nm": [400.0, 700.0], "R": [0.1, 0.2], "T": [0.8, 0.7],
                "A": [0.1, 0.1], "source_spectrum": [1.0, 1.0],
                "reflected_spectrum": [0.1, 0.2], "transmitted_spectrum": [0.8, 0.7],
                "absorbed_spectrum": [0.1, 0.1], "warnings": []}

    def export_film_csv(self, path, result, *, overwrite=False):
        target = self._path(path)
        if target.exists() and not overwrite:
            raise ValueError("already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = ("wavelength_nm", "R", "T", "A", "source_spectrum",
                   "reflected_spectrum", "transmitted_spectrum", "absorbed_spectrum")
        with target.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(zip(*(result[key] for key in columns)))
        return str(target)


class FilmDesignMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = _Service(self.temp.name)
        self.patch = patch.object(server, "service", self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_schema_and_material_metadata(self):
        schema = server.film_design_schema()
        self.assertEqual(schema["schema"]["properties"]["schema_version"]["const"], 1)
        self.assertEqual(schema["example"]["layer_order"], "substrate_outward")
        substrate = schema["schema"]["properties"]["substrate"]["properties"]
        self.assertEqual(substrate["thickness"]["properties"]["unit"]["enum"], ["nm", "um", "mm"])
        self.assertEqual(substrate["sides"]["enum"], ["single", "double"])
        self.assertEqual(substrate["mode"]["enum"], ["auto", "semi_infinite", "incoherent", "coherent"])
        medium = schema["schema"]["properties"]["incident_medium"]["oneOf"]
        self.assertEqual({x["properties"]["kind"].get("const") for x in medium}, {"constant", "material"})
        layer_unit = schema["schema"]["properties"]["front_layers"]["items"]["properties"]["thickness"]["properties"]["unit"]["enum"]
        self.assertEqual(layer_unit, ["nm", "um", "qw"])
        materials = server.film_materials()
        optical = materials["materials"][0]["optical_sets"][0]
        self.assertEqual(optical["wavelength_min"], 400)
        self.assertNotIn("n", optical)
        self.assertNotIn("k", optical)

    def test_canonical_design_validation_contract(self):
        for unit in ("nm", "um", "mm"):
            for mode in ("semi_infinite", "incoherent", "coherent"):
                design = new_design()
                design["substrate"]["thickness"]["unit"] = unit
                design["substrate"]["sides"] = "double"
                design["substrate"]["mode"] = mode
                design["incident_medium"] = {"kind": "material", "material_id": "m", "optical_set_id": "o"}
                validate_design(design)
        invalid = new_design()
        invalid["front_layers"] = [{"material_id": "m", "optical_set_id": "o",
                                    "thickness": {"value": 1, "unit": "mm"}}]
        with self.assertRaises(ValueError):
            validate_design(invalid)

    def test_film_windows_and_calculation_gate(self):
        choices = set(get_args(server.UiWindowType))
        self.assertTrue({"film_design", "film_simulation"} <= choices)
        with patch.object(server, "_ui_call", return_value={"ok": True}) as call:
            result = server.window_action("film_simulation", "calculate")
            self.assertEqual(result, {"ok": True})
            call.assert_called_once_with("window_action", window_type="film_simulation",
                                         action="calculate", arguments=None)
            with self.assertRaises(ToolError):
                server.window_action("main", "calculate")
            self.assertEqual(call.call_count, 1)

    def test_ui_control_client_is_lazy_and_preserves_domain_error_code(self):
        created = []

        class FakeUiControlError(Exception):
            code = "window_unavailable"

        class FakeUiControlClient:
            def __init__(self):
                created.append(self)

            def request(self, operation, **arguments):
                if operation == "fail":
                    raise FakeUiControlError("fixture failure")
                return {"operation": operation, **arguments}

        class FakeControlModule(types.ModuleType):
            UiControlClient = FakeUiControlClient
            UiControlError = FakeUiControlError

        fake_control = FakeControlModule("coat_sim.ui.control")
        original_ui = server._ui
        try:
            server._ui = None
            with patch.dict(sys.modules, {"coat_sim.ui.control": fake_control}):
                self.assertEqual(server._ui_call("first", value=1),
                                 {"operation": "first", "value": 1})
                self.assertEqual(server._ui_call("second"), {"operation": "second"})
                self.assertEqual(len(created), 1)
                with self.assertRaises(ToolError) as caught:
                    server._ui_call("fail")
                self.assertIn("window_unavailable", str(caught.exception))
        finally:
            server._ui = original_ui

    def test_gateway_uses_registered_film_tools_and_keeps_allowlist_boundary(self):
        class RawMcpClient:
            def __init__(self):
                self.calls = []

            async def list_tools(self):
                return await server.mcp.list_tools()

            async def call_tool_mcp(self, name, arguments):
                self.calls.append((name, copy.deepcopy(arguments)))
                try:
                    return await server.mcp.call_tool(name, arguments)
                except ToolError as exc:
                    return SimpleNamespace(
                        is_error=True, structured_content=None,
                        content=[SimpleNamespace(type="text", text=str(exc))],
                    )

        client = RawMcpClient()
        film_tools = {
            "film_materials", "validate_film_design", "load_film_design",
            "save_film_design", "expand_film_expression", "simulate_film_design",
        }
        read_only = film_tools - {"save_film_design", "simulate_film_design"}
        gateway = ProjectGateway(client, film_tools, read_only, str)
        design = new_design()

        async def exercise():
            materials = await gateway.execute("film_materials", {})
            self.assertTrue(materials["materials"])
            validated = await gateway.execute("validate_film_design", {"data": design})
            self.assertTrue(validated["ok"])
            saved = await gateway.execute("save_film_design", {
                "path": "data/designs/gateway.cfilm", "data": design,
            })
            self.assertTrue(saved["ok"])
            loaded = await gateway.execute("load_film_design", {
                "path": "data/designs/gateway.cfilm",
            })
            self.assertEqual(loaded["design"]["name"], "loaded")
            simulation = await gateway.execute("simulate_film_design", {
                "data": design, "settings": {"samples": 2},
                "output_path": "data/results/gateway.json",
            })
            self.assertTrue(simulation["ok"])
            json_path = Path(simulation["artifact_paths"]["json"])
            csv_path = Path(simulation["artifact_paths"]["csv"])
            self.assertTrue(json_path.is_relative_to(Path(self.temp.name, "data", "results")))
            self.assertTrue(json_path.is_file() and csv_path.is_file())

            before_invalid = len(client.calls)
            invalid = await gateway.execute("simulate_film_design", {
                "data": design, "output_path": "data/results/not-created.json",
                "unexpected": True,
            })
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["error"]["code"], "invalid_arguments")
            self.assertEqual(len(client.calls), before_invalid)

            denied = await gateway.execute("create_default_workflow", {})
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["error"]["code"], "unavailable_tool")
            self.assertEqual(len(client.calls), before_invalid)

        asyncio.run(exercise())

    def test_design_tools_delegate_and_preserve_save_receipt(self):
        design = self.fake.new_film_design()
        self.assertTrue(server.validate_film_design(design)["ok"])
        self.assertEqual(server.load_film_design("data/design.cfilm")["design"]["name"], "loaded")
        saved = server.save_film_design("data/design.cfilm", design)
        self.assertEqual(saved["path"], str(Path(self.temp.name, "data", "design.cfilm")))
        self.assertEqual(server.expand_film_expression("2A", {"A": {}})["layers"][0]["expression"], "2A")

    def test_simulate_is_direct_and_writes_full_json_and_csv(self):
        receipt = server.simulate_film_design(self.fake.new_film_design(), {"samples": 2})
        json_path = Path(receipt["artifact_paths"]["json"])
        csv_path = Path(receipt["artifact_paths"]["csv"])
        self.assertTrue(json_path.is_relative_to(Path(self.temp.name, "data", "results")))
        self.assertTrue(json_path.is_file() and csv_path.is_file())
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["result"]["R"], [0.1, 0.2])
        self.assertEqual(saved["settings"]["samples"], 2)
        self.assertIn("input_sha256", saved)
        self.assertIn("artifact_payload_sha256", saved)
        self.assertEqual(self.fake.calls, ["validate", "simulate"])
        with self.assertRaises(Exception):
            server.simulate_film_design(self.fake.new_film_design(), output_path="outside.json")
        self.assertEqual(self.fake.calls, ["validate", "simulate"])

    def test_input_digest_tracks_inputs_and_artifact_digest_tracks_full_output(self):
        design = self.fake.new_film_design()
        first = server.simulate_film_design(design, output_path="data/results/one.json")
        self.fake.simulate_film_design = lambda data, settings=None: {
            **_Service.simulate_film_design(self.fake, data, settings), "R": [0.3, 0.4]}
        second = server.simulate_film_design(design, output_path="data/results/two.json")
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertNotEqual(first["artifact_payload_sha256"], second["artifact_payload_sha256"])

    def test_json_publication_failure_preserves_csv_and_reports_path(self):
        target = Path(self.temp.name, "data", "results", "partial.json")
        with patch.object(server, "_atomic_film_json", side_effect=OSError("disk failure")):
            with self.assertRaises(ToolError) as raised:
                server.simulate_film_design(self.fake.new_film_design(), output_path=str(target))
        self.assertTrue(target.with_suffix(".csv").is_file())
        self.assertIn(str(target.with_suffix(".csv")), str(raised.exception))

    def test_real_api_mcp_film_workflow_with_project_fixture(self):
        project_root = Path(os.environ.get("COAT_SIM_TEST_PROJECT", Path(__file__).resolve().parents[1]))
        example_path = project_root / "examples" / "film_design_nbk7_plate.json"
        design = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertIs(design["assume_lossless"], True)
        with tempfile.TemporaryDirectory() as root:
            api = CoatService(root=root)
            self.assertEqual(api.film_design_service.root, Path(root).resolve())
            api.film_design_service.materials = FilmMaterialLibrary(project_root)
            validated = api.validate_film_design(design)
            saved = api.save_film_design("data/designs/nbk7.cfilm", validated)
            self.assertEqual(saved["path"], str(Path(root, "data", "designs", "nbk7.cfilm")))
            loaded = api.load_film_design("data/designs/nbk7.cfilm")
            self.assertEqual(loaded["id"], validated["id"])
            read_service = api.film_design_service
            with patch.object(server, "service", api):
                mats = server.film_materials("substrate")
                self.assertTrue(mats["materials"])
                sets = [optical for material in mats["materials"] for optical in material["optical_sets"]]
                self.assertTrue(any({"wavelength_unit", "wavelength_min", "wavelength_max", "source"} <= set(item) for item in sets))
                receipt = server.simulate_film_design(validated,
                    {"wavelength_min_nm": 550, "wavelength_max_nm": 551, "samples": 3},
                    output_path="data/results/nbk7.json")
                with self.assertRaises(ToolError):
                    server.simulate_film_design(validated, output_path="data/results/nbk7.json")
            result = json.loads(Path(receipt["artifact_paths"]["json"]).read_text(encoding="utf-8"))["result"]
            self.assertTrue(result["provenance"])
            self.assertIn("source_units", result)
            for reflectance, transmission, absorption in zip(result["R"], result["T"], result["A"]):
                self.assertAlmostEqual(reflectance + transmission + absorption, 1.0, places=8)
            self.assertEqual(read_service.root, Path(root).resolve())

    def test_runtime_whitelists_readers_and_mutators_separately(self):
        read = {"film_design_schema", "film_materials", "validate_film_design",
                "load_film_design", "expand_film_expression"}
        write = {"save_film_design", "simulate_film_design"}
        self.assertTrue(read <= READ_TOOLS)
        self.assertTrue(read <= MAIN_TOOLS)
        self.assertTrue(write <= MAIN_TOOLS)
        self.assertTrue(read | {"save_film_design"} <= EXECUTION_TOOLS["design"])
        self.assertIn("simulate_film_design", EXECUTION_TOOLS["simulation"])
        self.assertFalse(write & READ_TOOLS)


if __name__ == "__main__":
    unittest.main()
