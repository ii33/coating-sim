"""Offline dispatch tests; fake CUDA outputs do not establish hardware accuracy."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coat_sim.appearance.gpu import FATAL_COUNTERS, json_hash
from coat_sim.ui import appearance_worker
from tests.test_appearance_result_contract import _job

pytestmark = pytest.mark.contract


def _request(job_dir: Path, precision="mixed"):
    context = {"asset_path": str(job_dir / "source.coatasset"),
               "asset_root": str(job_dir), "asset_sha256": "a" * 64,
               "process_profile_sha256": "b" * 64,
               "process_profile": {"fixture": True}}
    request = {"schema": "coat_sim.appearance_worker_input", "version": 1,
               "job_id": "c" * 32, "input_sha256": "d" * 64,
               "context_sha256": appearance_worker._sha256_bytes(
                   appearance_worker._canonical(context)),
               "context": context, "render_inputs": {"wavelength_nm": [500, 600]},
               "geometry_thickness_mode": "explicit_override",
               "geometry_thickness_mm": 1.0,
               "backend": "gpu_cuda", "precision": precision}
    path = job_dir / "worker_input.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return path, context


def _manifest(job, precision):
    waves = job["wavelength_nm"]
    return {"schema": "coat_sim.gpu_appearance_preview", "schema_version": 1,
            "status": "rendered_unqualified", "input_sha256": json_hash(job),
            "precision": precision, "execution": {"variant": "cuda_ad_rgb"},
            "runtime": {"variant": "cuda_ad_rgb"},
            "wavelength_nm": waves, "resolution": job["resolution"],
            "per_wavelength": [{"index": i, "wavelength_nm": wavelength,
                                "seed": job["resolution"]["seed"] + i,
                                "diagnostics": {"pass": True, **dict.fromkeys(FATAL_COUNTERS, 0)}}
                               for i, wavelength in enumerate(waves)]}


def test_local_gpu_dispatch_accepts_only_complete_diagnostic_result(tmp_path):
    path, context = _request(tmp_path)
    job = _job()
    messages = []

    def render(actual, output, **options):
        assert actual is job and options["precision"] == "mixed"
        assert options["warm_repeat"] is False
        assert options["cancel_event"].is_set() is False
        output.mkdir()
        manifest = _manifest(actual, "mixed")
        for name, data in (("display.png", b"png"), ("radiance_spectrum.npy", b"spectrum"),
                           ("xyz.npy", b"xyz"), ("linear_srgb.npy", b"rgb")):
            (output / name).write_bytes(data)
        (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        options["progress_callback"]({"stage": "rendering", "completed_wavelengths": 1,
                                      "total_wavelengths": len(actual["wavelength_nm"]),
                                      "elapsed_s": 5., "estimated_remaining_s": 5.,
                                      "fraction": .5, "estimate_kind": "rough"})
        return manifest

    with patch.object(appearance_worker, "_appearance_context_from_asset", return_value=context), \
         patch.object(appearance_worker, "_build_job_from_verified_asset", return_value=job), \
         patch("coat_sim.appearance.gpu.render_preview", side_effect=render), \
         patch("coat_sim.appearance.render.render_job", side_effect=AssertionError("CPU fallback")), \
         patch.object(appearance_worker, "_emit", side_effect=lambda *a, **kw: messages.append((a, kw))):
        code = appearance_worker.run(path, tmp_path, tmp_path / "cancel.request")
    assert code == 0
    result = json.loads((tmp_path / "appearance_result.json").read_text())
    assert result["backend"] == "gpu_cuda" and result["precision"] == "mixed"
    assert result["render_input_sha256"] == json_hash(job)
    assert any(args[1] == "rendering" and extras["fraction"] == .5 for args, extras in messages)


@pytest.mark.parametrize("failure", ["rejected", "cancelled"])
def test_local_gpu_failure_or_cancel_never_falls_back_or_publishes(tmp_path, failure):
    path, context = _request(tmp_path)
    job = _job()
    marker = tmp_path / "cancel.request"

    def render(actual, output, **options):
        output.mkdir()
        (output / "radiance_spectrum.npy").write_bytes(b"partial")
        if failure == "cancelled":
            marker.write_text("cancel")
            raise InterruptedError("CUDA cancellation boundary")
        raise RuntimeError("CUDA unavailable")

    with patch.object(appearance_worker, "_appearance_context_from_asset", return_value=context), \
         patch.object(appearance_worker, "_build_job_from_verified_asset", return_value=job), \
         patch("coat_sim.appearance.gpu.render_preview", side_effect=render), \
         patch("coat_sim.appearance.render.render_job", side_effect=AssertionError("CPU fallback")):
        code = appearance_worker.run(path, tmp_path, marker)
    assert code != 0
    assert not (tmp_path / "appearance_result.json").exists()
    assert not (tmp_path / "render" / "display.png").exists()


def test_circular_profile_preserves_radius_and_rejects_curve():
    profile = {"surface": {"kind": "flat"}, "outline": {"kind": "circle", "radius_mm": 150.0}}
    assert appearance_worker._profile_for_adapter(profile) == profile
    assert appearance_worker._profile_for_adapter(profile)["outline"]["radius_mm"] == 150.0
    with pytest.raises(ValueError, match="flat"):
        appearance_worker._profile_for_adapter({**profile, "surface": {"kind": "spherical"}})


def test_real_saved_circular_v7_asset_freezes_without_square_substitution(tmp_path):
    from coat_sim.appearance.circle import BOUNDARY_POLICY
    from coat_sim.core.appearance_freeze_worker import build_frozen_envelope
    from tests.test_appearance_compute_entrypoints import (
        _write_synthetic_v7_asset, _render_inputs,
    )
    import hashlib
    asset = _write_synthetic_v7_asset(tmp_path, circle=True)
    request = {"asset_path": str(asset), "asset_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
               "render_inputs": {**_render_inputs(), "circle_boundary_sampling": BOUNDARY_POLICY},
               "geometry_thickness_mode": "film_design_snapshot", "display_exposure": .01,
               "backend": "gpu_cuda", "precision": "mixed"}
    frozen = build_frozen_envelope(tmp_path, request)
    job = frozen["job"]
    assert job["substrate"]["outline_kind"] == "circle"
    assert job["substrate"]["radius_mm"] == 1.0
    assert job["substrate"]["bounds_mm"] == [-1., 1., -1., 1.]
    assert job["substrate"]["solid_thickness_mm"] == 1.0
    assert job["substrate"]["circle_boundary_sampling"] == BOUNDARY_POLICY
    assert job["front_layers"][0]["thickness_field"]["thickness_nm"][0][0] is None


@pytest.mark.qt
def test_circular_asset_scene_preview_draws_round_outline(tmp_path):
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui.appearance import AppearanceSceneCanvas, AppearanceWindowController
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    import hashlib
    app = QGuiApplication.instance() or QGuiApplication([])
    asset = _write_synthetic_v7_asset(tmp_path, circle=True)
    context = appearance_worker._appearance_context_from_asset(
        asset, tmp_path, hashlib.sha256(asset.read_bytes()).hexdigest())
    controller = AppearanceWindowController(context, root=tmp_path)
    scene = controller.previewScene
    assert scene["substrate"]["outline_kind"] == "circle"
    assert scene["substrate"]["radius_m"] == pytest.approx(.001)
    canvas = AppearanceSceneCanvas()
    canvas.setWidth(800)
    canvas.setHeight(600)
    canvas.sceneData = scene
    vertices = canvas._scene_vertices()
    assert len(vertices["surface"]) >= 60 * 3
    assert len(vertices["outline"]) >= 60 * 6
    canvas.deleteLater()
    controller.deleteLater()
    app.processEvents()
