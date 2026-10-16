"""Pure result-contract tests; no Mitsuba render, daemon, or user service is started."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coat_sim.appearance.contract import validate_job
from coat_sim.core.appearance_result import (
    INDEX_SCHEMA, INDEX_SCHEMA_VERSION, is_appearance_render_result,
    validate_appearance_render_result,
    verified_output_references,
)
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.result_capability import job_has_viewable_result


def _job():
    return {
        "schema_version": 1,
        "coordinate_unit": "mm",
        "wavelength_nm": [500.0, 600.0],
        "resolution": {"width": 2, "height": 2, "spp": 1, "seed": 3},
        "substrate": {
            "outline_kind": "rectangle", "bounds_mm": [-2, 2, -2, 2],
            "solid_thickness_mm": 1.0,
            "solid_thickness_provenance": "explicit_geometry_input",
            "material": {"n": 1.5, "k": 0.0, "valid_wavelength_nm": [400, 700]},
            "back_surface": {"layers": [], "outside_medium": "air"},
        },
        "front_layers": [],
        "camera": {"position_mm": [0, 0, -10], "target_mm": [0, 0, 0],
                   "up": [0, 1, 0], "fov_y_deg": 40},
        "area_source": {
            "shape": "rectangle", "position_mm": [0, 0, 10],
            "normal": [0, 0, -1], "up": [0, 1, 0], "size_mm": [8, 8],
            "spectrum": {"wavelength_nm": [400, 700], "value": [1.0, 1.0],
                         "kind": "relative"},
        },
        "environment_spectrum": {"wavelength_nm": [400, 700], "value": [0.0, 0.0],
                                  "kind": "relative"},
        "provenance": {"process_asset_sha256": "a" * 64, "asset": "memory-test"},
    }


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_sha(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _temporary_directory():
    from tempfile import TemporaryDirectory
    return TemporaryDirectory(dir=Path(__file__).resolve().parent)


class AppearanceResultContractTests(unittest.TestCase):
    def _fixture(self, root, *, changes=None, provenance=None):
        job = _job()
        normalized = validate_job(job)
        request = {
            "asset_path": str(root / "data" / "test.coatasset"),
            "asset_sha256": "a" * 64,
            "render_inputs": {
                "wavelength_nm": [500.0, 600.0],
                "camera": job["camera"],
                "area_source": job["area_source"],
                "environment_spectrum": job["environment_spectrum"],
                "resolution": job["resolution"],
            },
            "geometry_thickness_mode": "explicit_override",
            "geometry_thickness_mm": 1.0,
            "display_exposure": 0.02,
        }
        context = {"schema": "coat_sim.appearance_launch_context", "version": 1,
                   "asset_sha256": "a" * 64}
        envelope = {
            "schema": "coat_sim.appearance_frozen_request", "schema_version": 1,
            "root": str(root), "request": request, "context": context,
            "context_sha256": _json_sha(context), "job": job,
            "job_sha256": _json_sha(job),
        }
        spec = JobSpec("appearance_render", {"frozen": envelope}, {}).to_dict()
        input_digest = hashlib.sha256(canonical(spec).encode("utf-8")).hexdigest()
        job_id = "appearance-contract-job"
        job_dir = root / "artifacts" / "runtime" / "scheduler" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "spec.json").write_text(canonical(spec), encoding="utf-8")

        output_dir = root / "data" / "tmp" / "appearance" / job_id / "render"
        output_dir.mkdir(parents=True)
        np.save(output_dir / "radiance_spectrum.npy",
                np.ones((2, 2, 2), dtype=np.float32))
        np.save(output_dir / "xyz.npy", np.ones((2, 2, 3), dtype=np.float32))
        np.save(output_dir / "linear_srgb.npy", np.ones((2, 2, 3), dtype=np.float32))
        png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
               + struct.pack(">II", 2, 2) + bytes([8, 2, 0, 0, 0]) + b"\0\0\0\0")
        (output_dir / "display.png").write_bytes(png)
        input_sha256 = "b" * 64
        manifest_provenance = normalized["provenance"] if provenance is None else provenance
        manifest = {
            "schema_version": 1, "input_sha256": input_sha256,
            "resolution": normalized["resolution"],
            "wavelength_nm": normalized["wavelength_nm"].tolist(),
            "input_provenance": manifest_provenance,
            "outputs": {"display_exposure": 0.02},
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        files = {path.name: {"sha256": _sha(path), "bytes": path.stat().st_size}
                 for path in output_dir.iterdir()}
        index = {
            "schema": INDEX_SCHEMA, "schema_version": INDEX_SCHEMA_VERSION,
            "job_id": job_id, "input_digest": input_digest,
            "request_sha256": _json_sha(request),
            "context_sha256": _json_sha(context), "job_sha256": _json_sha(job),
            "input_sha256": input_sha256, "resolution": normalized["resolution"],
            "wavelength_nm": normalized["wavelength_nm"].tolist(),
            "display_exposure": 0.02, "files": files,
        }
        if changes:
            index.update(changes)
        index_path = job_dir / "appearance-result.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        index_sha = _sha(index_path)
        artifact_id = hashlib.sha256(
            (job_id + ":result:" + index_sha).encode("utf-8")).hexdigest()
        result = {
            "kind": "appearance_render", "schema_version": 1,
            "job_id": job_id, "input_digest": input_digest,
            "request_sha256": _json_sha(request),
            "context_sha256": _json_sha(context), "job_sha256": _json_sha(job),
            "input_sha256": input_sha256, "resolution": index["resolution"],
            "wavelength_nm": index["wavelength_nm"], "display_exposure": index["display_exposure"],
            "result_file": str(index_path), "sha256": index_sha,
            "artifact_id": artifact_id,
            "result_artifacts": [{"artifact_id": artifact_id, "role": "result",
                                  "sha256": index_sha, "bytes": index_path.stat().st_size,
                                  "name": index_path.name, "local_path": str(index_path)}],
        }
        return job_id, result

    def test_result_shape_predicate_rejects_non_dict_inputs(self):
        for value in (None, False, True, [], ["result"], "result"):
            with self.subTest(value=value):
                self.assertFalse(is_appearance_render_result(value))

    def test_registered_result_revalidates_spec_and_all_five_files(self):
        with _temporary_directory() as temporary:
            root = Path(temporary)
            job_id, result = self._fixture(root)
            verified = validate_appearance_render_result(
                root, job_id, result, expected_input_digest=result["input_digest"],
                require_registered_artifact=True)
            self.assertEqual(verified["job_id"], job_id)
            self.assertTrue(job_has_viewable_result({"state": "succeeded", "result": result}))
            self.assertFalse(job_has_viewable_result({"state": "cancelled", "result": result}))
            self.assertFalse(job_has_viewable_result({"state": "failed", "result": result}))

    def test_resolution_wavelength_exposure_and_provenance_bind_to_frozen_job(self):
        cases = (
            ({"resolution": {"width": 1, "height": 2, "spp": 1, "seed": 3}}, None),
            ({"wavelength_nm": [510.0, 610.0]}, None),
            ({"display_exposure": 0.5}, None),
            ({}, {"process_asset_sha256": "f" * 64}),
        )
        for changes, provenance in cases:
            with self.subTest(changes=changes, provenance=provenance), _temporary_directory() as temporary:
                root = Path(temporary)
                job_id, result = self._fixture(root, changes=changes, provenance=provenance)
                if changes:
                    result["resolution"] = changes.get("resolution", result["resolution"])
                    result["wavelength_nm"] = changes.get("wavelength_nm", result["wavelength_nm"])
                    result["display_exposure"] = changes.get("display_exposure", result["display_exposure"])
                    # Recompute the outer digest anchor; the frozen-spec comparison must still reject.
                    index_path = Path(result["result_file"])
                    result["sha256"] = _sha(index_path)
                    artifact = result["result_artifacts"][0]
                    artifact["sha256"] = result["sha256"]
                    artifact["bytes"] = index_path.stat().st_size
                    artifact["artifact_id"] = hashlib.sha256(
                        (job_id + ":result:" + result["sha256"]).encode()).hexdigest()
                with self.assertRaises(ValueError):
                    validate_appearance_render_result(root, job_id, result,
                                                      require_registered_artifact=True)

    def test_modified_output_bytes_or_unregistered_index_are_not_viewable(self):
        with _temporary_directory() as temporary:
            root = Path(temporary)
            job_id, result = self._fixture(root)
            (root / "data" / "tmp" / "appearance" / job_id / "render" / "display.png").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "completion index"):
                validate_appearance_render_result(root, job_id, result,
                                                  require_registered_artifact=True)
        with _temporary_directory() as temporary:
            root = Path(temporary)
            job_id, result = self._fixture(root)
            result.pop("result_artifacts")
            result.pop("artifact_id")
            self.assertTrue(job_has_viewable_result({"state": "succeeded", "result": result}) is False)
            with self.assertRaisesRegex(ValueError, "anchored"):
                validate_appearance_render_result(root, job_id, result,
                                                  require_registered_artifact=True)

    def test_output_references_are_fixed_to_completion_index(self):
        with _temporary_directory() as temporary:
            root = Path(temporary)
            job_id, result = self._fixture(root)
            index = validate_appearance_render_result(root, job_id, result)
            references = verified_output_references(root, job_id, index)
            self.assertEqual(set(references), {
                "display", "manifest", "radiance_spectrum", "xyz", "linear_srgb",
            })
            self.assertEqual(references["display"]["media_type"], "image/png")
            self.assertEqual(references["manifest"]["media_type"], "application/json")
            self.assertEqual(references["radiance_spectrum"]["media_type"], "application/x-npy")
            self.assertTrue(references["display"]["path"].endswith("display.png"))
            result["outputs"] = references
            validate_appearance_render_result(root, job_id, result)

            forged = dict(result)
            forged["outputs"] = {key: dict(value) for key, value in references.items()}
            forged["outputs"]["display"]["path"] = str(root / "outside.png")
            with self.assertRaisesRegex(ValueError, "output references"):
                validate_appearance_render_result(root, job_id, forged)


if __name__ == "__main__":
    unittest.main()
