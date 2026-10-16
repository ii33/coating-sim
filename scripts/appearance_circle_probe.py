"""Opt-in real Mitsuba scalar_rgb probe for the native circular closed body.

Run from a checkout with its isolated renderer Python, e.g.
  python scripts/appearance_circle_probe.py
  python scripts/appearance_circle_probe.py --job frozen-circle-job.json
No saved assets are changed and no render/output directory is created.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coat_sim.appearance.contract import validate_job
from coat_sim.appearance.render import _add_circle_body, _register_bsdf


def synthetic_job():
    waves = [500., 600.]
    spectrum = {"wavelength_nm": waves, "value": [1., 1.], "kind": "relative"}
    x = np.linspace(-150., 150., 7)
    xx, yy = np.meshgrid(x, x)
    valid = xx ** 2 + yy ** 2 <= 150. ** 2 * (1 + 1e-12)
    cells = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
    thickness = np.full(valid.shape, None, dtype=object)
    thickness[valid] = 100.
    return {"schema_version": 1, "coordinate_unit": "mm", "wavelength_nm": waves,
            "resolution": {"width": 2, "height": 2, "spp": 1, "seed": 0},
            "substrate": {"outline_kind": "circle", "radius_mm": 150.,
                          "bounds_mm": [-150., 150., -150., 150.],
                          "solid_thickness_mm": 12., "solid_thickness_provenance": "explicit_geometry_input",
                          "material": {"n": 1.5, "k": 0., "valid_wavelength_nm": [400., 700.]},
                          "back_surface": {"layers": [], "outside_medium": "air"},
                          "circle_boundary_sampling": "captured_node_mean_with_local_nearest_fallback"},
            "front_layers": [{"layer_id": "synthetic-captured-layer", "material": {
                "n": 1.9, "k": 0., "valid_wavelength_nm": [400., 700.]}, "thickness_field": {
                "unit": "nm", "origin": "captured_process_step", "x_mm": x.tolist(), "y_mm": x.tolist(),
                "thickness_nm": thickness.tolist(), "valid_mask": valid.tolist(), "valid_cell_mask": cells.tolist()}}],
            "camera": {"position_mm": [0., 0., -500.], "target_mm": [0., 0., 0.],
                       "up": [0., 1., 0.], "fov_y_deg": 40.},
            "area_source": {"shape": "rectangle", "position_mm": [0., 0., -500.],
                            "normal": [0., 0., 1.], "up": [0., 1., 0.], "size_mm": [300., 300.],
                            "spectrum": spectrum}, "environment_spectrum": spectrum}


def run(job):
    import mitsuba as mi
    mi.set_variant("scalar_rgb")
    data = validate_job(job)
    assert data["substrate"].get("outline_kind") == "circle"
    _register_bsdf(mi)
    body = {"type": "scene"}
    _add_circle_body(body, mi, data, float(data["wavelength_nm"][0]), 0)
    scene = mi.load_dict(body)
    radius = data["substrate"]["radius_mm"] * 1e-3
    t = data["substrate"]["solid_thickness_mm"] * 1e-3
    offset = max(radius, t)
    cases = [("front", [0, 0, -offset], [0, 0, 1], [0, 0, -1], offset),
             ("back", [0, 0, t + offset], [0, 0, -1], [0, 0, 1], offset),
             ("side", [radius + offset, 0, t / 2], [-1, 0, 0], [1, 0, 0], offset),
             ("inside_to_back", [0, 0, t / 2], [0, 0, 1], [0, 0, 1], t / 2),
             ("inside_to_front", [0, 0, t / 2], [0, 0, -1], [0, 0, -1], t / 2),
             ("inside_to_side", [0, 0, t / 2], [1, 0, 0], [1, 0, 0], radius)]
    verified = []
    for label, origin, direction, expected_normal, expected_distance in cases:
        si = scene.ray_intersect(mi.Ray3f(mi.Point3f(*origin), mi.Vector3f(*direction)))
        assert bool(si.is_valid()), label
        assert np.allclose(np.asarray(si.n), expected_normal, rtol=0, atol=2e-6), (label, si.n)
        assert np.isclose(float(si.t), expected_distance, rtol=2e-5, atol=1e-8), (label, si.t)
        # Probe the project delta interface including the circle world-XY
        # field lookup. It must accept both front and back incidence.
        sample, weight = si.bsdf().sample(mi.BSDFContext(), si, .999999,
                                         mi.Point2f(.3, .7), True)
        assert np.isfinite(np.asarray(weight)).all(), label
        verified.append(label)
    # Square-corner rays miss the circular body entirely.
    miss = scene.ray_intersect(mi.Ray3f(mi.Point3f(radius * .9, radius * .9, -offset), mi.Vector3f(0, 0, 1)))
    assert not bool(miss.is_valid()), "circle must not become a square"
    # The side primitive itself has no endcaps.
    cylinder = mi.load_dict(body["side_cylinder"])
    axial = cylinder.ray_intersect(mi.Ray3f(mi.Point3f(0, 0, -offset), mi.Vector3f(0, 0, 1)))
    assert not bool(axial.is_valid()), "native cylinder must remain open"
    assert len(scene.shapes()) == 3, "closed body has exactly two disks and one open side"
    return {"status": "passed", "variant": mi.variant(), "mitsuba": mi.__version__,
            "geometry": "native_analytic_disk_cylinder", "shape_count": 3,
            "checks": verified + ["outside_square_corner_misses", "cylinder_has_no_caps"],
            "circle_sampling": {face: data["substrate"]["circle_grids"][face]["metadata"]
                                for face in ("front", "back")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8")) if args.job else synthetic_job()
    print(json.dumps(run(job), ensure_ascii=False, indent=2, allow_nan=False))
