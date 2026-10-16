"""Registered MCP coverage for reading a project-local appearance asset descriptor.

The only asset is a synthetic v7 archive created beneath this test's fresh temp
project with the existing serializer fixture. Snapshot/hash/parser behavior is
tested without a renderer, child process, scheduler daemon, or network.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.storage import AssistantStore
from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core import simulation_assets
from tests.test_appearance_compute_entrypoints import (
    _render_inputs, _write_synthetic_v7_asset,
)


_ALLOWED_TEMP = Path(tempfile.gettempdir())


class AppearanceAssetDescriptorTests(unittest.TestCase):
    def setUp(self):
        if not _ALLOWED_TEMP.is_dir():
            self.skipTest(f"authorized isolated temp directory is unavailable: {_ALLOWED_TEMP}")
        self._old_tempdir = tempfile.tempdir
        self._old_environ = {key: os.environ.get(key) for key in ("TEMP", "TMP", "LOCALAPPDATA")}
        self.scratch = Path(tempfile.mkdtemp(prefix="appearance-descriptor-",
                                             dir=_ALLOWED_TEMP)).resolve()
        for key in ("TEMP", "TMP"):
            os.environ[key] = str(self.scratch)
        os.environ["LOCALAPPDATA"] = str(self.scratch)
        tempfile.tempdir = str(self.scratch)
        self.addCleanup(self._restore_temp_environment)

        self.root = self.scratch / "project"
        self.root.mkdir()
        self.asset_path = _write_synthetic_v7_asset(self.root)
        self.asset_bytes = self.asset_path.read_bytes()
        self.asset_sha256 = hashlib.sha256(self.asset_bytes).hexdigest()
        self.service = CoatService(self.root)
        self.store = ComputeTaskStore(self.root)
        self.assistant = AssistantStore(self.root / "artifacts/assistant/state.sqlite3")

    def _restore_temp_environment(self):
        tempfile.tempdir = self._old_tempdir
        for key, value in self._old_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.scratch.exists():
            shutil.rmtree(self.scratch)

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

    def _no_child_process(self, *args, **kwargs):
        raise AssertionError(f"appearance asset inspection must not start a child: {args!r}")

    @contextmanager
    def _mcp_context(self):
        from coat_sim.mcp import server
        with patch.object(server, "service", self.service), \
                patch("subprocess.Popen", side_effect=self._no_child_process):
            yield server

    def _row_count(self, table):
        with self.store.connection() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _call(self, server, name, arguments):
        return self._mcp_value(asyncio.run(server.mcp.call_tool(name, arguments)))

    def test_registered_descriptor_schema_and_archive_digest(self):
        with self._mcp_context() as server:
            tools = asyncio.run(server.mcp.list_tools())
            tool = next(item for item in tools if item.name == "inspect_appearance_asset")
            schema = getattr(tool, "inputSchema", None)
            if schema is None:
                schema = tool.input_schema
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["required"], ["asset_path"])
            self.assertEqual(set(schema["properties"]), {"asset_path"})

            descriptor = self._call(server, "inspect_appearance_asset", {
                "asset_path": str(self.asset_path),
            })
            self.assertEqual(set(descriptor), {
                "schema_version", "root", "asset_path", "asset_sha256", "asset_bytes",
            })
            self.assertEqual(descriptor["schema_version"], 1)
            self.assertEqual(descriptor["root"], str(self.root.resolve()))
            self.assertEqual(descriptor["asset_path"], str(self.asset_path.resolve()))
            self.assertEqual(descriptor["asset_sha256"], self.asset_sha256)
            self.assertEqual(descriptor["asset_bytes"], len(self.asset_bytes))

            archived = simulation_assets.read_simulation_asset(
                self.asset_path, detail="configuration", include_arrays=False)
            self.assertEqual(archived["configuration"]["process"]["schema_version"], 7)
            self.assertTrue(archived["has_results"])
            self.assertEqual(self._row_count("estimates"), 0)
            self.assertEqual(self._row_count("queue_jobs"), 0)

    def test_scope_data_suffix_and_unknown_arguments_are_rejected_without_compute_writes(self):
        outside_project = self.scratch / "outside-project.coatasset"
        outside_data = self.root / "outside-data.coatasset"
        wrong_suffix = self.root / "data" / "assets" / "synthetic.txt"
        outside_project.write_bytes(self.asset_bytes)
        outside_data.write_bytes(self.asset_bytes)
        wrong_suffix.write_bytes(self.asset_bytes)
        baseline_estimates = self._row_count("estimates")
        baseline_jobs = self._row_count("queue_jobs")

        with self._mcp_context() as server:
            cases = (
                ("outside project", {"asset_path": str(outside_project)}),
                ("outside data", {"asset_path": str(outside_data)}),
                ("wrong suffix", {"asset_path": str(wrong_suffix)}),
                ("unknown argument", {"asset_path": str(self.asset_path),
                                      "include_arrays": True}),
            )
            for name, arguments in cases:
                with self.subTest(case=name), self.assertRaises(ToolError):
                    self._call(server, "inspect_appearance_asset", arguments)

        self.assertEqual(self._row_count("estimates"), baseline_estimates)
        self.assertEqual(self._row_count("queue_jobs"), baseline_jobs)

    def test_descriptor_uses_parsed_snapshot_even_if_original_changes_after_copy(self):
        from coat_sim.core import ray_trace_job
        original_snapshot = ray_trace_job._asset_snapshot
        evidence = {}

        @contextmanager
        def snapshot_then_mutate(path, **kwargs):
            with original_snapshot(path, **kwargs) as snapshot:
                snapshot_path = Path(snapshot["path"])
                snapshot_bytes = snapshot_path.read_bytes()
                evidence["snapshot_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
                evidence["snapshot_bytes"] = len(snapshot_bytes)
                # Independently parse those exact copied bytes before yielding
                # the snapshot record to the production descriptor function.
                parsed = simulation_assets.read_simulation_asset(
                    snapshot_path, detail="configuration", include_arrays=False)
                evidence["parsed_process_schema"] = parsed["configuration"]["process"][
                    "schema_version"]
                # This runs after the original snapshot helper has yielded its
                # private verified copy, but before inspect() consumes it.
                self.asset_path.write_bytes(self.asset_bytes + b"\nchanged after snapshot")
                evidence["source_sha256_after_change"] = hashlib.sha256(
                    self.asset_path.read_bytes()).hexdigest()
                yield snapshot

        with self._mcp_context() as server, patch.object(
                ray_trace_job, "_asset_snapshot", side_effect=snapshot_then_mutate):
            descriptor = self._call(server, "inspect_appearance_asset", {
                "asset_path": str(self.asset_path),
            })

            self.assertEqual(evidence["parsed_process_schema"], 7)
            self.assertEqual(descriptor["asset_sha256"], evidence["snapshot_sha256"])
            self.assertEqual(descriptor["asset_bytes"], evidence["snapshot_bytes"])
            self.assertNotEqual(descriptor["asset_sha256"],
                                evidence["source_sha256_after_change"])

            # The descriptor is only a snapshot receipt, not permission to use
            # a different live source: estimate must compare against the actual
            # asset bytes and reject before creating an estimate or child.
            request = {
                "asset_path": str(self.asset_path),
                "asset_sha256": descriptor["asset_sha256"],
                "render_inputs": _render_inputs(),
            }
            with self.assertRaises(ToolError):
                self._call(server, "estimate_appearance_render", {"request": request})
            self.assertEqual(self._row_count("estimates"), 0)
            self.assertEqual(self._row_count("queue_jobs"), 0)


if __name__ == "__main__":
    unittest.main()
