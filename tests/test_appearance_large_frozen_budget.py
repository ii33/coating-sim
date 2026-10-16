"""Bounded large-field appearance handoff; no CUDA or large test allocations."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from io import StringIO

from coat_sim.appearance.limits import (
    MAX_APPEARANCE_CONTROL_BYTES, MAX_APPEARANCE_FROZEN_BYTES,
    MAX_APPEARANCE_SPEC_BYTES,
)
from coat_sim.appearance.gpu import _write_input_job, json_hash
from coat_sim.core import appearance_compute, appearance_freeze_worker, appearance_result
from coat_sim.scheduler import daemon, jobs
from coat_sim.scheduler.client import SchedulerClient
from tests.test_appearance_result_contract import AppearanceResultContractTests


class AppearanceLargeFrozenBudgetTests(unittest.TestCase):
    def test_control_budget_stays_small_and_frozen_budget_fits_rpc(self):
        self.assertEqual(MAX_APPEARANCE_CONTROL_BYTES, 16_000_000)
        self.assertEqual(MAX_APPEARANCE_FROZEN_BYTES, 60_000_000)
        self.assertEqual(MAX_APPEARANCE_SPEC_BYTES, 60_000_000)
        self.assertLess(MAX_APPEARANCE_SPEC_BYTES, daemon.MAX_RPC_BODY_BYTES)

    def test_frozen_field_budget_is_separate_and_byte_bounded(self):
        obj = {"captured_field": "厚" * 100}
        encoded_size = len(appearance_freeze_worker._canonical_json(obj).encode("utf-8"))
        with patch.object(appearance_freeze_worker, "MAX_APPEARANCE_FROZEN_BYTES", encoded_size):
            self.assertEqual(appearance_freeze_worker._plain_json(obj), obj)
        with patch.object(appearance_freeze_worker, "MAX_APPEARANCE_FROZEN_BYTES", encoded_size - 1):
            with self.assertRaisesRegex(ValueError, "frozen data exceeds"):
                appearance_freeze_worker._plain_json(obj)

    def test_control_input_is_rejected_before_loading_or_starting_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, output_path = root / "input.json", root / "output.json"
            input_path.write_bytes(b"x" * 101)
            with patch.object(appearance_freeze_worker, "MAX_APPEARANCE_JSON_BYTES", 100), \
                    patch.object(Path, "read_bytes", side_effect=AssertionError("read before size check")), \
                    redirect_stderr(StringIO()):
                self.assertEqual(appearance_freeze_worker.run(input_path, output_path), 1)
            self.assertFalse(output_path.exists())
            with patch.object(appearance_compute, "MAX_APPEARANCE_JSON_BYTES", 100), \
                    patch.object(appearance_compute.subprocess, "run",
                                 side_effect=AssertionError("child started before size check")):
                with self.assertRaisesRegex(ValueError, "freeze input exceeds"):
                    appearance_compute._freeze_in_child(root, {"request": "x" * 101})

    def test_frozen_spec_rejects_first_byte_over_budget(self):
        spec = jobs.JobSpec("appearance_render", {"frozen": {"captured": "x" * 100}}, {}).to_dict()
        size = len(jobs.canonical(spec).encode("utf-8"))
        with patch.object(jobs, "MAX_APPEARANCE_SPEC_BYTES", size):
            self.assertEqual(jobs.JobSpec.parse_frozen(spec).executor, "appearance_render")
        with patch.object(jobs, "MAX_APPEARANCE_SPEC_BYTES", size - 1):
            with self.assertRaisesRegex(ValueError, "appearance_frozen_spec_exceeds"):
                jobs.JobSpec.parse_frozen(spec)

    def test_loopback_client_checks_real_compact_wire_before_daemon_start(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SchedulerClient(Path(directory))
            spec = jobs.JobSpec("appearance_render", {"frozen": {"captured": "x" * 100}}, {}).to_dict()
            wire = json.dumps({"protocol_version": 1, "action": "submit",
                               "arguments": {"spec": spec, "job_id": "budget-fixture"}},
                              ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
            with patch.object(daemon, "MAX_RPC_BODY_BYTES", len(wire) - 1), \
                    patch.object(client, "ensure_started") as started:
                with self.assertRaisesRegex(ValueError, "request_body_too_large"):
                    client._call("submit", spec=spec, job_id="budget-fixture")
                started.assert_not_called()

    def test_result_reader_rejects_oversize_spec_before_loading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id, result = AppearanceResultContractTests()._fixture(root)
            path = root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
            with patch.object(appearance_result, "MAX_APPEARANCE_SPEC_BYTES", path.stat().st_size - 1):
                with self.assertRaisesRegex(ValueError, "frozen job spec exceeds"):
                    appearance_result.validate_appearance_render_result(root, job_id, result)

    def test_gpu_job_copy_retains_all_fields_without_pretty_json_amplification(self):
        job = {"layers": [{"step": i, "field_nm": [float(j) / 10 for j in range(100)]}
                          for i in range(54)], "材料": "ZnS"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-job.json"
            _write_input_job(path, job)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, job)
            self.assertEqual(json_hash(saved), json_hash(job))
            self.assertEqual(path.stat().st_size,
                             len((json.dumps(job, sort_keys=True, separators=(",", ":"))
                                  + "\n").encode("utf-8")))
            appearance_result._gpu_input_job(path, job)


if __name__ == "__main__":
    unittest.main()
