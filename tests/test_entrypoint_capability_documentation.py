"""Keep the public capability matrix aligned with registered typed tools.

Discovery only: no tool handler, provider, scheduler, renderer or UI is run.
The existing typed-operation tests own argument and lifecycle behavior.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import unittest

from coat_sim.agent.runtime import EXECUTION_TOOLS, MAIN_TOOLS, READ_TOOLS
from coat_sim.core.appearance_compute import APPEARANCE_TOOL_ARGUMENTS
from coat_sim.core.capabilities import POLICIES
from coat_sim.core.film_compute import FILM_TOOL_ARGUMENTS
from coat_sim.mcp import server


DOCUMENT = Path(__file__).resolve().parents[1] / "docs/entrypoint-capabilities.md"


class EntryPointCapabilityDocumentationTests(unittest.TestCase):
    def documented_tools(self, label):
        rows = [line for line in DOCUMENT.read_text(encoding="utf-8").splitlines()
                if line.startswith("| " + label + " |")]
        self.assertEqual(len(rows), 1, "Capability matrix row must be unique")
        return set(re.findall(r"`([a-z][a-z0-9_]*)`", rows[0]))

    def assert_typed_surface_documented(self, label, arguments):
        names = set(arguments)
        documented = self.documented_tools(label)
        self.assertLessEqual(names, documented)
        registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        self.assertLessEqual(names, set(registered))
        self.assertLessEqual(names, MAIN_TOOLS)
        self.assertLessEqual(names, EXECUTION_TOOLS["simulation"])
        self.assertFalse(names & set(POLICIES),
                         "Typed compute tools are outside the finite shared CLI catalog")
        for name in names:
            schema = getattr(registered[name], "input_schema", None)
            if schema is None:
                schema = getattr(registered[name], "inputSchema", None)
            if not isinstance(schema, dict):
                raise AssertionError("Registered typed tool must expose an input schema")
            expected = arguments[name].model_json_schema()
            self.assertEqual(set(schema["properties"]), set(expected["properties"]))
            self.assertEqual(set(schema["required"]), set(expected["required"]))
            self.assertFalse(schema["additionalProperties"])
            if name.startswith("submit_"):
                self.assertNotIn(name, READ_TOOLS)
                for role in ("design", "correction", "windows"):
                    self.assertNotIn(name, EXECUTION_TOOLS[role])
            else:
                self.assertIn(name, READ_TOOLS)

    def test_film_thickness_tools_are_documented_as_registered_separate_surface(self):
        self.assert_typed_surface_documented("膜系光学优化", FILM_TOOL_ARGUMENTS)

    def test_appearance_tools_are_documented_as_registered_separate_surface(self):
        self.assert_typed_surface_documented("外观帧渲染", APPEARANCE_TOOL_ARGUMENTS)


if __name__ == "__main__":
    unittest.main()
