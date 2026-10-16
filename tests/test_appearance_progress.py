"""Progress adapter coverage for the isolated and scheduled appearance paths."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coat_sim.core.appearance_execution import _report_render_progress
from coat_sim.ui import appearance_worker


class AppearanceProgressAdapterTests(unittest.TestCase):
    def test_scheduler_progress_preserves_metrics_and_maps_queue_fraction(self):
        reports = []
        base = {
            "stage": "rendering", "completed_wavelengths": 2,
            "total_wavelengths": 4, "elapsed_s": 12.0,
            "estimated_remaining_s": 12.0, "fraction": 0.5,
            "estimate_kind": "measured_rate",
        }

        _report_render_progress(base, reports.append)
        _report_render_progress({**base, "stage": "integrating"}, reports.append)
        _report_render_progress({**base, "stage": "complete", "fraction": 1.0}, reports.append)

        self.assertEqual([row["fraction"] for row in reports], [0.475, 0.9, 0.98])
        for row, stage in zip(reports, ("rendering", "integrating", "complete")):
            self.assertEqual(row["stage"], stage)
            self.assertEqual(row["completed_wavelengths"], 2)
            self.assertEqual(row["total_wavelengths"], 4)
            self.assertEqual(row["elapsed_s"], 12.0)
            self.assertEqual(row["estimated_remaining_s"], 12.0)
            self.assertEqual(row["estimate_kind"], "measured_rate")

    def test_ui_worker_formats_eta_and_forwards_callback_without_new_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="appearance-progress-") as temp:
            job_dir = Path(temp)
            input_path = job_dir / "worker_input.json"
            context = {"asset_path": str(job_dir / "asset.json"),
                       "asset_root": str(job_dir), "asset_sha256": "a" * 64,
                       "process_profile_sha256": "b" * 64,
                       "process_profile": {"fixture": True}}
            request = {
                "schema": "coat_sim.appearance_worker_input", "version": 1,
                "job_id": "c" * 32, "input_sha256": "d" * 64,
                "context_sha256": appearance_worker._sha256_bytes(
                    appearance_worker._canonical(context)),
                "context": context, "render_inputs": {"wavelength_nm": [500.0, 600.0]},
                "geometry_thickness_mode": "explicit_override",
                "geometry_thickness_mm": 0.1,
            }
            input_path.write_text(json.dumps(request), encoding="utf-8")
            messages = []

            class _FakeMmap:
                def close(self):
                    pass

            class _FakeSpectrum:
                _mmap = _FakeMmap()

            manifest = {
                "input_sha256": "e" * 64,
                "resolution": {"width": 1, "height": 1},
                "wavelength_nm": [500.0, 600.0],
            }

            def render(job, output, **kwargs):
                self.assertIn("progress_callback", kwargs)
                output.mkdir()
                (output / "display.png").write_bytes(b"png")
                (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                (output / "radiance_spectrum.npy").write_bytes(b"spectrum")
                (output / "xyz.npy").write_bytes(b"xyz")
                (output / "linear_srgb.npy").write_bytes(b"rgb")
                kwargs["progress_callback"]({
                    "stage": "rendering", "completed_wavelengths": 1,
                    "total_wavelengths": 2, "elapsed_s": 3.0,
                    "estimated_remaining_s": 3.0, "fraction": 0.5,
                    "estimate_kind": "measured_rate",
                })
                return {"manifest": manifest, "radiance_spectrum": _FakeSpectrum()}

            with patch.object(appearance_worker, "_appearance_context_from_asset",
                              return_value=context), \
                    patch.object(appearance_worker, "_build_job_from_verified_asset",
                                 return_value={"test": True}), \
                    patch("coat_sim.appearance.render.render_job", side_effect=render), \
                    patch.object(appearance_worker, "_emit",
                                 side_effect=lambda kind, stage, message, **extra:
                                 messages.append((kind, stage, message, extra))):
                result = appearance_worker.run(input_path, job_dir, job_dir / "cancel.request")

            self.assertEqual(result, 0)
            progress = next(row for row in messages if row[1] == "rendering"
                            and row[2].startswith("波长 "))
            self.assertEqual(progress[2], "波长 1/2 · 已用 3 秒 · 预计剩余 约 3 秒")
            self.assertEqual(progress[3]["estimate_kind"], "measured_rate")
            self.assertTrue((job_dir / "appearance_result.json").is_file())
            self.assertEqual({path.name for path in job_dir.iterdir()}, {
                "worker_input.json", "render", "appearance_result.json"})
            self.assertEqual({path.name for path in (job_dir / "render").iterdir()}, {
                "display.png", "manifest.json", "radiance_spectrum.npy",
                "xyz.npy", "linear_srgb.npy"})

    def test_unknown_eta_is_presented_as_an_estimate_not_zero(self):
        with patch.object(appearance_worker, "_emit") as emit:
            appearance_worker._emit_render_progress({
                "stage": "rendering", "completed_wavelengths": 0,
                "total_wavelengths": 4, "elapsed_s": 0.0,
                "estimated_remaining_s": None, "fraction": 0.0,
                "estimate_kind": "warming_up",
            })
        self.assertIn("预计剩余 估算中", emit.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
