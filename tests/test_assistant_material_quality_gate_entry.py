"""MCP entry contract for the assistant optical-material quality gate.

The gate runs at the assistant creation boundary (``create_material``) before
the material library persists anything. Invalid data must be rejected with a
visible, actionable tool error and must not create a file; valid reference
tables/formulas and explicit user sparse measurements must persist unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from coat_sim.agent.tool_dispatch import result_value
from coat_sim.core.api import CoatService
from coat_sim.core.materials import default_material_profile, read_material_profile
from coat_sim.mcp import server


SOURCE = "https://example.org/optical/reference-table"


def table_set(identifier="sample", **overrides):
    record = {"id": identifier, "name": identifier, "kind": "tabulated",
              "source": SOURCE, "notes": "bulk sample at 20 C",
              "wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 700,
              "wavelengths": [400, 550, 700], "n": [1.5, 1.49, 1.48], "k": None}
    record.update(overrides)
    return record


def formula_set(identifier="disp"):
    return {"id": identifier, "name": identifier, "kind": "cauchy",
            "source": SOURCE, "notes": "bulk sample at 20 C",
            "wavelength_unit": "um", "wavelength_min": 0.4, "wavelength_max": 0.7,
            "parameters": {"A": 1.45, "B": 0.01, "C": 0.0}}


class AssistantMaterialQualityGateEntry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = CoatService(root=self.root, restrict_to_root=False)
        self.service_patch = patch.object(server, "service", self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    async def call(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, str(result.content))
        return result_value(result)

    async def error(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        self.assertTrue(result.is_error, f"{name} unexpectedly succeeded: {result.content}")
        return str(result.content)

    def stored(self, identifier):
        return read_material_profile(self.root / "data" / "materials" / (identifier + ".json"))

    async def test_invalid_reference_is_rejected_with_visible_reason_and_no_write(self):
        profile = default_material_profile("Ref table", "ref_table")
        profile.update(roles=["film"], optical_constants=[table_set()])
        async with Client(server.mcp) as client:
            message = await self.error(client, "create_material", {"profile": profile})
            self.assertIn("assistant_material_quality_rejected", message)
            self.assertIn("sample", message)
            self.assertFalse((self.root / "data/materials/ref_table.json").exists())

            # Incomplete table evidence is also refused without persisting.
            message = await self.error(client, "create_material", {"profile": profile,
                "evidence": {"sample": {"k_status": "unknown", "reference_row_count": 20,
                                        "conditions": "bulk sample at 20 C"}}})
            self.assertIn("assistant_material_quality_rejected", message)
            self.assertIn("20", message)
            self.assertFalse((self.root / "data/materials/ref_table.json").exists())

    async def test_valid_reference_table_and_formula_persist_with_unknown_k_null(self):
        table = default_material_profile("Ref table", "ref_table")
        table.update(roles=["film"], optical_constants=[table_set()])
        formula = default_material_profile("Ref formula", "ref_formula")
        formula.update(roles=["film"], optical_constants=[formula_set()])
        async with Client(server.mcp) as client:
            table_report = await self.call(client, "create_material", {"profile": table,
                "origin": "assistant_reference",
                "evidence": {"sample": {"k_status": "unknown", "reference_row_count": 3,
                                        "conditions": "bulk sample at 20 C"}}})
            self.assertTrue(table_report["quality"]["valid"])
            self.assertFalse(table_report["quality"]["source_verified_externally"])
            formula_report = await self.call(client, "create_material", {"profile": formula,
                "evidence": {"disp": {"k_status": "unknown", "conditions": "bulk sample at 20 C"}}})
            self.assertTrue(formula_report["quality"]["valid"])
        stored_table = self.stored("ref_table")
        self.assertIsNone(stored_table["optical_constants"][0]["k"])
        self.assertEqual(stored_table["optical_constants"][0]["wavelengths"], [400, 550, 700])
        stored_formula = self.stored("ref_formula")
        self.assertEqual(stored_formula["optical_constants"][0]["parameters"]["A"], 1.45)

    async def test_explicit_user_sparse_measurement_persists_but_reference_cannot(self):
        sparse = default_material_profile("Sparse measured", "sparse_measured")
        sparse.update(roles=["film"], optical_constants=[table_set(
            source="User supplied measurements", notes="sparse measured points",
            wavelengths=[400, 700], n=[1.5, 1.48])])
        async with Client(server.mcp) as client:
            # The same sparse table is refused as a claimed reference table...
            await self.error(client, "create_material", {"profile": sparse,
                "origin": "assistant_reference",
                "evidence": {"sample": {"k_status": "unknown", "reference_row_count": 2,
                                        "conditions": "sparse measured points"}}})
            self.assertFalse((self.root / "data/materials/sparse_measured.json").exists())
            # ...but persists as an explicit user measurement.
            report = await self.call(client, "create_material", {"profile": sparse,
                "origin": "user_measurement", "evidence": {"sample": {"k_status": "unknown"}}})
            self.assertTrue(report["quality"]["valid"])
            self.assertEqual(report["quality"]["datasets"][0]["data_character"],
                             "user_sparse_measurement")
        stored = self.stored("sparse_measured")
        self.assertEqual(stored["optical_constants"][0]["wavelengths"], [400, 700])
        self.assertIsNone(stored["optical_constants"][0]["k"])

    async def test_update_without_new_reference_claim_preserves_existing_record(self):
        profile = default_material_profile("Updatable", "updatable")
        profile.update(roles=["film"], optical_constants=[table_set()])
        async with Client(server.mcp) as client:
            await self.call(client, "create_material", {"profile": profile,
                "origin": "user_measurement", "evidence": {"sample": {"k_status": "unknown"}}})
            revised = deepcopy(profile)
            revised["name"] = "Updatable revised"
            # No origin supplied: this is not a new reference claim, so the gate
            # is not re-run and the existing optical evidence is preserved.
            result = await self.call(client, "update_material", {"profile": revised})
            self.assertNotIn("quality", result)
        stored = self.stored("updatable")
        self.assertEqual(stored["name"], "Updatable revised")
        self.assertEqual(len(stored["optical_constants"]), 1)
        self.assertIsNone(stored["optical_constants"][0]["k"])

    async def test_malformed_category_create_is_rejected_at_entry(self):
        profile = default_material_profile("Bad category", "bad_category")
        profile["category"] = "optical"  # not film / substrate / other
        async with Client(server.mcp) as client:
            message = await self.error(client, "create_material", {"profile": profile})
            self.assertIn("材料分类", message)
            self.assertFalse((self.root / "data/materials/bad_category.json").exists())


if __name__ == "__main__":
    unittest.main()
