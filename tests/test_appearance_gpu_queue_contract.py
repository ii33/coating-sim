"""GPU queue contract tests without a CUDA runtime or ray tracing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coat_sim.core.appearance_compute import _normalize_public_request
from coat_sim.core.appearance_execution import execute_appearance
from coat_sim.core.appearance_result import (
    _gpu_job_sha256, _sha256_json, is_appearance_render_result,
    validate_appearance_render_result,
)
from coat_sim.appearance.gpu import FATAL_COUNTERS
from coat_sim.scheduler.jobs import JobSpec, canonical
from tests.test_appearance_result_contract import AppearanceResultContractTests, _job


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AppearanceGpuQueueContractTests(unittest.TestCase):
    def test_scheduler_launches_cuda_in_dedicated_interpreter_only(self):
        from coat_sim.scheduler import daemon

        with tempfile.TemporaryDirectory(prefix="appearance-gpu-launch-") as directory:
            root = Path(directory).resolve()
            runtime = root / "build/mitsuba_appearance_env"
            python = (runtime / "Scripts/python.exe" if sys.platform == "win32"
                      else runtime / "bin/python")
            python.parent.mkdir(parents=True)
            python.write_bytes(b"test executable marker")

            launches = []

            def fake_popen(command, **options):
                launches.append((command, options))
                return SimpleNamespace(pid=1234)

            scheduler = daemon.Scheduler.__new__(daemon.Scheduler)
            scheduler.root = root
            scheduler.runtime = root / "artifacts/runtime/scheduler"
            scheduler.owner = "fixture-owner"
            scheduler.guard = SimpleNamespace(attach=lambda process: None)
            scheduler.store = SimpleNamespace(set_worker=lambda *args: None)
            for backend, expected_python in (("cpu", sys.executable),
                                             ("gpu_cuda", str(python.resolve()))):
                spec = JobSpec("appearance_render", {"frozen": {
                    "request": {"backend": backend, "precision": (
                        "native" if backend == "cpu" else "mixed")}}}, {}).to_dict()
                parsed = JobSpec.parse_frozen(spec)
                job_id = "fixture-" + backend
                with patch.object(daemon.subprocess, "Popen", side_effect=fake_popen), \
                        patch.object(daemon, "_process_identity", return_value=("alive", "start")):
                    scheduler.start_job({"job_id": job_id,
                                         "input_digest": parsed.fingerprint}, spec)
                command, options = launches[-1]
                self.assertEqual(command[0], expected_python)
                self.assertIn("env", options) if backend == "gpu_cuda" else self.assertNotIn("env", options)
                if backend == "gpu_cuda":
                    self.assertEqual(options["env"]["PYTHONPATH"],
                                     str(Path(daemon.__file__).resolve().parents[2]))
                    self.assertEqual(options["env"]["PYTHONNOUSERSITE"], "1")

            with patch.object(daemon, "_appearance_gpu_python",
                              side_effect=FileNotFoundError("isolated GPU runtime missing")), \
                    patch.object(daemon.subprocess, "Popen", side_effect=fake_popen):
                with self.assertRaisesRegex(FileNotFoundError, "isolated GPU runtime missing"):
                    scheduler.start_job({"job_id": "fixture-gpu-missing",
                                         "input_digest": parsed.fingerprint}, spec)
            self.assertEqual(len(launches), 2)

    def _gpu_fixture(self, root: Path):
        job_id, result = AppearanceResultContractTests()._fixture(root)
        job_dir = root / "artifacts/runtime/scheduler/jobs" / job_id
        spec_path = job_dir / "spec.json"
        spec = JobSpec.parse_frozen_json(spec_path.read_text(encoding="utf-8"))
        envelope = spec.parameters["frozen"]
        envelope["request"].update(backend="gpu_cuda", precision="mixed")
        spec = JobSpec("appearance_render", {"frozen": envelope}, {}).to_dict()
        spec_path.write_text(canonical(spec), encoding="utf-8")
        result["input_digest"] = hashlib.sha256(canonical(spec).encode()).hexdigest()
        result["request_sha256"] = _sha256_json(envelope["request"])
        result.update(backend="gpu_cuda", precision="mixed",
                      render_status="rendered_unqualified", diagnostics_pass=True)

        output_dir = root / "data/tmp/appearance" / job_id / "render"
        (output_dir / "input-job.json").write_text(json.dumps(envelope["job"]), encoding="utf-8")
        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            schema="coat_sim.gpu_appearance_preview",
            status="rendered_unqualified",
            precision="mixed",
            input_sha256=_gpu_job_sha256(envelope["job"]),
            execution={"variant": "cuda_ad_rgb"},
            runtime={"variant": "cuda_ad_rgb"},
            per_wavelength=[
                {"index": index, "wavelength_nm": wavelength,
                 "seed": envelope["job"]["resolution"]["seed"] + index,
                 "diagnostics": {"pass": True, **{name: 0 for name in FATAL_COUNTERS}}}
                for index, wavelength in enumerate(manifest["wavelength_nm"])],
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result["input_sha256"] = manifest["input_sha256"]

        index_path = job_dir / "appearance-result.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index.update(
            backend="gpu_cuda", precision="mixed",
            render_status="rendered_unqualified", diagnostics_pass=True,
            input_digest=result["input_digest"], request_sha256=result["request_sha256"],
            input_sha256=result["input_sha256"],
            files={path.name: {"sha256": _sha(path), "bytes": path.stat().st_size}
                   for path in output_dir.iterdir()},
        )
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result["sha256"] = _sha(index_path)
        artifact_id = hashlib.sha256(
            (job_id + ":result:" + result["sha256"]).encode()).hexdigest()
        result["artifact_id"] = artifact_id
        result["result_artifacts"] = [{
            "artifact_id": artifact_id, "role": "result", "sha256": result["sha256"],
            "bytes": index_path.stat().st_size, "name": index_path.name,
            "local_path": str(index_path),
        }]
        return job_id, result, output_dir

    def test_public_request_defaults_and_rejects_backend_precision_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="appearance-gpu-request-") as directory:
            root = Path(directory).resolve()
            data = root / "data"
            data.mkdir()
            asset = data / "fixture.coatasset"
            asset.write_bytes(b"synthetic test marker")
            request = {
                "asset_path": str(asset), "asset_sha256": _sha(asset),
                "render_inputs": {
                    "wavelength_nm": [500.0, 600.0], "camera": {},
                    "area_source": {}, "environment_spectrum": {}, "resolution": {},
                },
            }
            cpu = _normalize_public_request(root, request)
            self.assertEqual((cpu["backend"], cpu["precision"]), ("cpu", "native"))
            gpu = _normalize_public_request(root, {**request, "backend": "gpu_cuda"})
            self.assertEqual((gpu["backend"], gpu["precision"]), ("gpu_cuda", "mixed"))
            for backend, precision in (("cpu", "mixed"), ("gpu_cuda", "native"),
                                       ("opencl", "mixed"), ("gpu_cuda", "fp16")):
                with self.subTest(backend=backend, precision=precision):
                    with self.assertRaises(ValueError):
                        _normalize_public_request(root, {
                            **request, "backend": backend, "precision": precision})

    def test_gpu_completion_binds_cuda_manifest_diagnostics_and_job_copy(self):
        with tempfile.TemporaryDirectory(prefix="appearance-gpu-result-") as directory:
            root = Path(directory).resolve()
            job_id, result, output_dir = self._gpu_fixture(root)
            self.assertTrue(is_appearance_render_result(result))
            validate_appearance_render_result(
                root, job_id, result, expected_input_digest=result["input_digest"],
                require_registered_artifact=True)
            manifest_path = output_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["per_wavelength"][0]["diagnostics"]["pass"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_appearance_render_result(root, job_id, result)

    def test_gpu_failure_never_falls_back_to_cpu(self):
        with tempfile.TemporaryDirectory(prefix="appearance-gpu-dispatch-") as directory:
            root = Path(directory).resolve()
            job_id = "gpu-dispatch-fixture"
            job = _job()
            request = {"backend": "gpu_cuda", "precision": "float32",
                       "display_exposure": 0.01}
            envelope = {
                "schema": "coat_sim.appearance_frozen_request", "schema_version": 1,
                "root": str(root), "request": request, "context": {},
                "context_sha256": _sha256_json({}), "job": job,
                "job_sha256": _sha256_json(job),
            }
            failure = RuntimeError("CUDA unavailable")
            with patch("coat_sim.core.appearance_compute.build_frozen_request",
                       return_value=envelope), \
                    patch("coat_sim.scheduler.store.SchedulerStore.status", return_value={
                        "kind": "appearance_render", "input_digest": "d" * 64}), \
                    patch("coat_sim.appearance.gpu.render_preview", side_effect=failure) as gpu, \
                    patch("coat_sim.appearance.render.render_job") as cpu:
                with self.assertRaisesRegex(RuntimeError, "CUDA unavailable"):
                    execute_appearance(root, job_id, {"frozen": envelope}, Event(), None)
            self.assertEqual(gpu.call_count, 1)
            self.assertEqual(gpu.call_args.kwargs["precision"], "float32")
            self.assertFalse(cpu.called)
            self.assertFalse((root / "artifacts/runtime/scheduler/jobs" / job_id /
                              "appearance-result.json").exists())


if __name__ == "__main__":
    unittest.main()
