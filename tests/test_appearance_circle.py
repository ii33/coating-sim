"""CPU-only circle contract/geometry tests; Mitsuba is explicitly mocked."""
from collections import OrderedDict
from copy import deepcopy
import json
import math
from pathlib import Path
from threading import Event
import types
from unittest.mock import patch

import numpy as np
import pytest

from coat_sim.appearance.circle import (
    BOUNDARY_POLICY, circle_cell_area, circle_cell_index, circle_node_mask,
)
from coat_sim.appearance.contract import AppearanceInputError, validate_job
from coat_sim.appearance.process_adapter import build_job_from_v7
from coat_sim.appearance.render import (
    _circle_stack_at_hit, _scene_dict, render_job,
)
from tests.test_appearance_result_contract import _job


def _circle_job(x=None, y=None, *, layers=2, back=False):
    job = _job()
    job["substrate"].update(outline_kind="circle", radius_mm=2.0,
                            solid_thickness_mm=12.0,
                            circle_boundary_sampling=BOUNDARY_POLICY)
    x = np.asarray(np.linspace(-2, 2, 9) if x is None else x, dtype=np.float64)
    y = np.asarray(x if y is None else y, dtype=np.float64)
    valid = circle_node_mask(x, y, 2.0)
    cells = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
    rows = [{"layer_id": f"actual-{il}",
             "material": {"n": 1.9 + il * 0.1, "k": 0.0,
                          "valid_wavelength_nm": [400, 700]},
             "thickness_field": {"unit": "nm", "origin": "captured_process_step",
                                 "x_mm": x.tolist(), "y_mm": y.tolist(),
                                 "thickness_nm": np.where(valid, 100.0 + il, np.nan).tolist(),
                                 "valid_mask": valid.tolist(), "valid_cell_mask": cells.tolist()}}
            for il in range(layers)]
    job["front_layers"] = [] if back else rows
    job["substrate"]["back_surface"]["layers"] = rows if back else []
    return job


class _Transform:
    def __init__(self, matrix=None):
        self.matrix = np.eye(4) if matrix is None else np.asarray(matrix)

    def __matmul__(self, other):
        return _Transform(self.matrix @ other.matrix)

    @staticmethod
    def translate(value):
        matrix = np.eye(4); matrix[:3, 3] = value
        return _Transform(matrix)

    @staticmethod
    def scale(value):
        values = np.full(3, value) if np.ndim(value) == 0 else value
        return _Transform(np.diag([*values, 1.0]))

    @staticmethod
    def rotate(axis, degrees):
        assert axis == [0, 1, 0] and degrees == 180
        return _Transform(np.diag([-1.0, 1.0, -1.0, 1.0]))

    @staticmethod
    def look_at(origin, target, up):
        origin, target, up = map(lambda item: np.asarray(item, dtype=float), (origin, target, up))
        z = target - origin; z /= np.linalg.norm(z)
        x = np.cross(up, z); x /= np.linalg.norm(x)
        y = np.cross(z, x)
        matrix = np.eye(4); matrix[:3, :3] = np.column_stack((x, y, z)); matrix[:3, 3] = origin
        return _Transform(matrix)


def _mock_mitsuba():
    return types.SimpleNamespace(
        ScalarTransform4f=_Transform, set_variant=lambda _: None,
        load_dict=lambda scene: scene,
        render=lambda scene, **_: np.ones((scene["sensor"]["film"]["height"],
                                         scene["sensor"]["film"]["width"], 3), dtype=np.float32))


def test_circle_area_partition_exact_for_uniform_and_nonuniform_grids():
    for x, y in ((np.linspace(-2, 2, 41), np.linspace(-2, 2, 33)),
                 (np.array([-2, -.21, 0, .4, 2]), np.array([-2, -1, .13, 2]))):
        area = sum(circle_cell_area(a, b, c, d, 2.0)
                   for a, b in zip(x, x[1:]) for c, d in zip(y, y[1:]))
        assert area == pytest.approx(math.pi * 4, abs=1e-12)
        assert area * 12 == pytest.approx(math.pi * 4 * 12, abs=2e-11)
    assert circle_cell_area(2, 3, -1, 1, 2) == 0.0
    assert circle_cell_area(-.1, .1, -.1, .1, 2) == pytest.approx(.04)


def test_circle_constant_actual_fields_exact_recovery_and_immutable_cells():
    data = validate_job(_circle_job())
    grid = data["substrate"]["circle_grids"]["front"]
    assert np.all(grid["thickness_cell_nm"][0, grid["active_cell_mask"]] == 100.0)
    assert np.all(grid["thickness_cell_nm"][1, grid["active_cell_mask"]] == 101.0)
    assert not grid["thickness_cell_nm"].flags.writeable
    assert grid["metadata"]["boundary_area_mm2"] > 0
    assert grid["metadata"]["approximation"].endswith("boundary_thickness_is_not_measured")
    assert data["substrate"]["solid_thickness_mm"] == 12.0


def test_circle_partial_corner_mean_uses_captured_samples_only():
    job = _circle_job(layers=1)
    field = job["front_layers"][0]["thickness_field"]
    values = np.asarray(field["thickness_nm"])
    valid = np.asarray(field["valid_mask"])
    yy, xx = np.meshgrid(field["y_mm"], field["x_mm"], indexing="ij")
    values[valid] = 100 + xx[valid] + 2 * yy[valid]
    values[~valid] = 9e20  # ignored exterior sentinel never treated measured
    field["thickness_nm"] = values.tolist()
    data = validate_job(job); grid = data["substrate"]["circle_grids"]["front"]
    for iy, ix in zip(*np.nonzero(grid["active_cell_mask"])):
        local_valid = valid[iy:iy+2, ix:ix+2]
        if np.any(local_valid):
            assert grid["thickness_cell_nm"][0, iy, ix] == pytest.approx(
                np.mean(values[iy:iy+2, ix:ix+2][local_valid]))


@pytest.mark.parametrize("mutation", ["missing_mask", "missing_value", "cells", "extra_exterior", "grid"])
def test_circle_rejects_missing_interior_and_inconsistent_validity(mutation):
    job = _circle_job()
    field = job["front_layers"][0]["thickness_field"]
    if mutation == "missing_mask": field["valid_mask"][4][4] = False
    if mutation == "missing_value": field["thickness_nm"][4][4] = None
    if mutation == "cells": field["valid_cell_mask"][3][3] = False
    if mutation == "extra_exterior": field["valid_mask"][0][0] = True
    if mutation == "grid": job["front_layers"][1]["thickness_field"]["x_mm"][3] += .02
    with pytest.raises(AppearanceInputError): validate_job(job)


def test_circle_boundary_policy_must_be_explicit_but_bare_circle_needs_none():
    job = _circle_job(); del job["substrate"]["circle_boundary_sampling"]
    with pytest.raises(AppearanceInputError, match="explicit circle_boundary_sampling"): validate_job(job)
    job["front_layers"] = []
    assert validate_job(job)["substrate"]["outline_kind"] == "circle"


def test_circle_both_coated_faces_require_identical_grid_and_masks():
    job = _circle_job()
    job["substrate"]["back_surface"]["layers"] = deepcopy(job["front_layers"])
    field = job["substrate"]["back_surface"]["layers"][0]["thickness_field"]
    field["x_mm"][3] += .02
    with pytest.raises(AppearanceInputError, match="share.*grid"): validate_job(job)


def test_circle_nearest_fallback_is_local_deterministic_and_per_layer():
    # No valid corner in the thin cap cell [-.2,.2] x [1.99,1.998].
    x = [-2, -.2, .2, 2]; y = [-2, 0, 1.98, 1.99, 1.998, 2]
    job = _circle_job(x, y)
    data = validate_job(job); grid = data["substrate"]["circle_grids"]["front"]
    fallbacks = grid["metadata"]["nearest_fallbacks"]
    assert fallbacks
    record = next(row for row in fallbacks if row["cell_yx"] == [3, 1])
    assert record["captured_node_yx"] == [2, 1]  # tie selects left, row-major
    assert record["anchor_to_node_distance_mm"] <= record["local_cell_diagonal_mm"]
    assert record["anchor_mm"] == [0.0, 1.99]
    assert grid["thickness_cell_nm"][0, 3, 1] == 100
    assert grid["thickness_cell_nm"][1, 3, 1] == 101
    # Remove the nearby row: the global 2mm grid gap must not excuse a local
    # .4mm diagonal cap fallback reaching ~2mm into the substrate.
    with pytest.raises(AppearanceInputError, match="local grid diagonal"):
        validate_job(_circle_job(x, [-2, 0, 1.99, 1.998, 2]))


def test_circle_world_xy_index_endpoint_ownership_and_bounded_stack_cache():
    data = validate_job(_circle_job())
    grid = data["substrate"]["circle_grids"]["front"]
    x, y = grid["x_mm"], grid["y_mm"]
    assert circle_cell_index(x, y, -2, 0) == (4, 0)
    assert circle_cell_index(x, y, 2, 0) == (4, 7)
    assert circle_cell_index(x, y, 2 + 1e-7, 0) == (4, 7)
    assert circle_cell_index(x, y, 0, 0) == (4, 4)
    with pytest.raises(AppearanceInputError): circle_cell_index(x, y, 2.1, 0)
    cache = OrderedDict()
    indices = (complex(1.9), complex(2.0))
    first = _circle_stack_at_hit(grid, indices, cache, .1, .1)
    assert first == ((complex(1.9), 100.0), (complex(2.0), 101.0))
    assert _circle_stack_at_hit(grid, indices, cache, .2, .2) is first
    with pytest.raises(AppearanceInputError, match="no physical disk area"):
        _circle_stack_at_hit(grid, indices, cache, -1.99, -1.99)
    large = validate_job(_circle_job(np.linspace(-2, 2, 41)))
    grid = large["substrate"]["circle_grids"]["front"]
    for iy, ix in zip(*np.nonzero(grid["active_cell_mask"])):
        _circle_stack_at_hit(grid, indices, cache,
                             (grid["x_mm"][ix] + grid["x_mm"][ix+1]) / 2,
                             (grid["y_mm"][iy] + grid["y_mm"][iy+1]) / 2)
    assert len(cache) == 128


@pytest.mark.parametrize("back", [False, True])
def test_native_circle_scene_caps_normals_physical_thickness_and_interfaces(back):
    data = validate_job(_circle_job(back=back))
    scene = _scene_dict(_mock_mitsuba(), data, 500., 0., 1.)
    assert [key for key, item in scene.items() if isinstance(item, dict) and item.get("type") == "disk"] == ["front_disk", "back_disk"]
    assert scene["front_disk"]["flip_normals"] is True
    assert scene["back_disk"]["flip_normals"] is False
    assert np.array_equal(scene["front_disk"]["to_world"].matrix, np.diag([.002, .002, .002, 1]))
    assert scene["back_disk"]["to_world"].matrix[2, 3] == .012
    side = scene["side_cylinder"]
    assert side["p0"] == [0, 0, 0] and side["p1"] == [0, 0, .012]
    assert side["radius"] == .002 and side["flip_normals"] is False
    assert side["bsdf"]["layer_count"] == 0
    assert side["bsdf"]["substrate_n"] == 1.5
    coated, bare = ("back_disk", "front_disk") if back else ("front_disk", "back_disk")
    assert scene[coated]["bsdf"]["layer_count"] == 2
    assert scene[bare]["bsdf"]["layer_count"] == 0
    payload = json.loads(scene[coated]["bsdf"]["circle_grid_json"])
    assert payload["thickness_cell_nm"][0][4][4] == 100.
    assert payload["thickness_cell_nm"][1][4][4] == 101.
    assert payload["thickness_cell_nm"][0][0][0] is None
    assert not any(key.startswith("side_x") for key in scene)


def test_rectangle_contract_scene_and_cell_arithmetic_unchanged():
    job = _job(); data = validate_job(job)
    assert "outline_kind" not in data["substrate"]  # old normalized structure
    scene = _scene_dict(_mock_mitsuba(), data, 500., 0., 1.)
    assert {"front_0_0", "back_bare", "side_xp", "side_xm", "side_yp", "side_ym"} <= scene.keys()
    assert "front_disk" not in scene
    job["front_layers"] = _circle_job(layers=1)["front_layers"]
    with pytest.raises(AppearanceInputError, match="rectangular substrate"): validate_job(job)


@pytest.mark.parametrize("mutation", ["unknown_k", "absorbing_substrate", "out_of_band"])
def test_circle_preserves_strict_optical_constant_contract(mutation):
    job = _circle_job()
    if mutation == "unknown_k": del job["front_layers"][0]["material"]["k"]
    if mutation == "absorbing_substrate": job["substrate"]["material"]["k"] = .001
    if mutation == "out_of_band": job["front_layers"][0]["material"]["valid_wavelength_nm"] = [550, 700]
    with pytest.raises(AppearanceInputError): validate_job(job)


def test_mock_render_circle_manifest_json_exterior_nulls_area_and_hash(tmp_path):
    job = _circle_job()
    mock = _mock_mitsuba()
    with patch.dict("sys.modules", {"mitsuba": mock, "drjit": types.SimpleNamespace(set_thread_count=lambda count: None)}), \
         patch("coat_sim.appearance.render._register_bsdf"), \
         patch("coat_sim.appearance.render._cmf_table", return_value=np.ones((2, 3))):
        result = render_job(job, tmp_path / "first")
        manifest = result["manifest"]
        assert manifest["circle_geometry"]["model"] == "native_analytic_disk_cylinder"
        assert manifest["circle_geometry"]["disk_area_mm2"] == pytest.approx(math.pi * 4)
        assert manifest["circle_geometry"]["solid_volume_mm3"] == pytest.approx(math.pi * 48)
        assert manifest["circle_geometry"]["polygon_tessellation"] is None
        assert manifest["back_surface"] == "uncoated"
        assert manifest["back_uncoated_model"] == "substrate/air coherent Fresnel interface"
        assert manifest["circle_sampling"]["front"]["boundary_area_mm2"] > 0
        assert manifest["circle_sampling"]["back"]["policy"] is None
        json.loads((tmp_path / "first/manifest.json").read_text())
        # Exterior sentinels do not influence canonical actual captured input.
        second = deepcopy(job); second["front_layers"][0]["thickness_field"]["thickness_nm"][0][0] = 1e99
        assert render_job(second, tmp_path / "second")["manifest"]["input_sha256"] == manifest["input_sha256"]
        second["substrate"]["solid_thickness_mm"] = 13
        assert render_job(second, tmp_path / "third")["manifest"]["input_sha256"] != manifest["input_sha256"]
        with pytest.raises(FileExistsError): render_job(job, tmp_path / "first")


def test_circle_cancelled_mock_render_never_publishes_manifest(tmp_path):
    cancel = Event(); mock = _mock_mitsuba()
    def frame(scene, **kwargs):
        cancel.set()
        return np.ones((2, 2, 3), dtype=np.float32)
    mock.render = frame
    with patch.dict("sys.modules", {"mitsuba": mock, "drjit": types.SimpleNamespace(set_thread_count=lambda count: None)}), \
         patch("coat_sim.appearance.render._register_bsdf"):
        with pytest.raises(InterruptedError): render_job(_circle_job(), tmp_path / "cancelled", cancel_event=cancel)
    assert (tmp_path / "cancelled/radiance_spectrum.npy").exists()
    assert not (tmp_path / "cancelled/manifest.json").exists()


def _adapter_inputs():
    from coat_sim.core.film_design import new_design, validate_design
    from coat_sim.core.film_design_service import simulation_settings
    from coat_sim.core.film_process_import import FROZEN_KIND, FROZEN_SCHEMA_VERSION, _sha256, validate_frozen_design_snapshot
    from coat_sim.core.optical_constants import validate_optical_set
    design = new_design("Circle adapter fixture", "circle-fixture")
    design["substrate"].update(material_id="glass", optical_set_id="glass-n",
                               thickness={"value": 12000., "unit": "um"})
    design["front_layers"] = [{"id": f"actual-{i}", "material_id": f"film-{i}",
                               "optical_set_id": f"film-{i}-n",
                               "thickness": {"value": 999., "unit": "nm"}, "note": "Synthetic fixture"}
                              for i in range(2)]
    def material(identifier, index):
        return {"id": identifier, "name": identifier, "notes": "Synthetic fixture",
                "optical_sets": [validate_optical_set({"id": identifier + "-n", "name": identifier,
                    "kind": "tabulated", "wavelength_unit": "nm", "wavelength_min": 400.,
                    "wavelength_max": 700., "wavelengths": [400., 700.],
                    "n": [index, index], "k": [0., 0.]})]}
    snapshot = {"schema_version": FROZEN_SCHEMA_VERSION, "kind": FROZEN_KIND,
                "design": validate_design(design), "settings": simulation_settings(),
                "materials": [material("glass", 1.5), material("film-0", 1.9), material("film-1", 2.0)]}
    snapshot["sha256"] = _sha256(snapshot)
    snapshot = validate_frozen_design_snapshot(snapshot)
    job = _circle_job()
    profile = {"schema_version": 7, "film_design_snapshot": snapshot,
               "film_design_import": {"snapshot_sha256": snapshot["sha256"], "surface": "front",
                                      "sequence_order": "substrate_outward", "layer_ids": ["actual-0", "actual-1"]},
               "steps": [{"film_layer_ref": {"surface": "front", "layer_id": "actual-0"}},
                         {"film_layer_ref": {"surface": "front", "layer_id": "actual-0"}},
                         {"film_layer_ref": {"surface": "front", "layer_id": "actual-1"}}],
               "substrate_profile": {"schema_version": 1, "name": "Test circle",
                                     "outline": {"kind": "circle", "radius_mm": 2.}, "surface": {"kind": "flat"}}}
    rows, fields = [], {}
    for number, (layer_index, fraction) in enumerate(((0, .6), (0, .4), (1, 1.))):
        raw = job["front_layers"][layer_index]["thickness_field"]
        key = f"actual-step-{number}"
        fields[key] = {key: np.asarray(raw[key]) for key in ("x_mm", "y_mm", "valid_mask", "valid_cell_mask")}
        fields[key]["thickness_nm"] = np.asarray(raw["thickness_nm"]) * fraction
        rows.append({"step_id": key, "step_index": number + 1, "included_in_total": True})
    capture = {"manifest": {"captured": True, "field_units": "nm", "field_dtype": "float64",
                "coordinates": {"frame": "substrate_local_xy", "x_array": "x", "y_array": "y", "unit": "mm"},
                "sequence_order": "process_recipe_order",
                "stack_order": "first_step_is_substrate_adjacent_then_later_steps_overcoat", "fields": rows},
               "fields": fields}
    return profile, capture, {**job, "circle_boundary_sampling": BOUNDARY_POLICY}


def test_circle_v7_adapter_keeps_captured_growth_order_null_exterior_and_inherited_length():
    profile, capture, inputs = _adapter_inputs()
    before = deepcopy(profile)
    job = build_job_from_v7(profile, capture, [500., 600.], inputs)
    assert profile == before
    assert job["substrate"]["solid_thickness_mm"] == 12.
    assert job["substrate"]["solid_thickness_provenance"] == "film_design_snapshot/design_value"
    assert job["substrate"]["outline_kind"] == "circle"
    assert job["front_layers"][0]["thickness_field"]["thickness_nm"][0][0] is None
    assert job["front_layers"][0]["thickness_field"]["thickness_nm"][4][4] == 100.
    assert job["front_layers"][1]["thickness_field"]["thickness_nm"][4][4] == 101.
    assert [row["layer_id"] for row in job["front_layers"]] == ["actual-0", "actual-1"]
    assert "circular slab" in job["provenance"]["geometry_note"]
    json.dumps(job, allow_nan=False)
    assert validate_job(job)["substrate"]["solid_thickness_mm"] == 12.
    override = build_job_from_v7(profile, capture, [500., 600.], inputs, geometry_thickness_mm=13.)
    assert override["substrate"]["solid_thickness_mm"] == 13.
    with pytest.raises(AppearanceInputError):
        build_job_from_v7(profile, capture, [500., 600.], inputs, geometry_thickness_mm=0.)


@pytest.mark.parametrize("mutation", ["curved", "radius", "step_grid", "step_hole"])
def test_circle_adapter_refuses_curvature_radius_and_inconsistent_step_fields(mutation):
    profile, capture, inputs = _adapter_inputs()
    if mutation == "curved": profile["substrate_profile"]["surface"]["kind"] = "spherical"
    if mutation == "radius": profile["substrate_profile"]["outline"]["radius_mm"] = 0
    if mutation == "step_grid":
        capture["fields"]["actual-step-1"]["x_mm"] = capture["fields"]["actual-step-1"]["x_mm"].copy()
        capture["fields"]["actual-step-1"]["x_mm"][3] += .01
    if mutation == "step_hole": capture["fields"]["actual-step-1"]["thickness_nm"][4, 4] = np.nan
    with pytest.raises(AppearanceInputError): build_job_from_v7(profile, capture, [500., 600.], inputs)


def test_circle_registered_bsdf_uses_exact_angles_and_world_xy_on_both_incidence_sides():
    from coat_sim.appearance import render
    data = validate_job(_circle_job())
    scene = _scene_dict(_mock_mitsuba(), data, 500., 0., 1.)
    registered = {}
    class Base:
        def __init__(self, props): pass
    flags = types.SimpleNamespace(DeltaReflection=1, DeltaTransmission=2, FrontSide=4, BackSide=8)
    mi = types.SimpleNamespace(BSDF=Base, BSDFFlags=flags,
                               register_bsdf=lambda name, factory: registered.update({name: factory}))
    with patch.object(render, "_BSDF_REGISTERED", False), \
         patch.object(render, "_interface_angle_luts", side_effect=AssertionError("circle constructor must not eagerly build LUTs")):
        render._register_bsdf(mi)
        bsdf = registered["appearance_coherent_stack"](scene["front_disk"]["bsdf"])
    arrays = tuple(id(value) for value in bsdf.circle_grid.values())
    calls = []
    def rta(wave, substrate, stack, angle, *, incident_from_outside):
        calls.append((wave, substrate, stack, angle, incident_from_outside))
        return {"R": .2, "T": .8, "A": 0.}
    requested_angles = (23.137, 67.321)
    with patch.object(render, "coherent_stack_rta", side_effect=rta), \
         patch.object(render, "interpolated_angle_rta",
                      side_effect=AssertionError("BSDF must not use approximate angle LUT")):
        for angle, direction in zip(requested_angles, (1., -1.)):
            wi_z = direction * math.cos(math.radians(angle))
            si = types.SimpleNamespace(wi=types.SimpleNamespace(z=wi_z),
                                       p=types.SimpleNamespace(x=.0001, y=.0002))
            assert bsdf._rta(si)[:2] == (.2, .8)
    assert calls[0][2] == ((complex(1.9), 100.), (complex(2.), 101.))
    assert calls[0][2] is calls[1][2]
    assert [call[3] for call in calls] == pytest.approx(requested_angles, abs=1e-12)
    assert [call[4] for call in calls] == [True, False]
    assert [(call[0], call[1]) for call in calls] == [(500., 1.5), (500., 1.5)]
    assert arrays == tuple(id(value) for value in bsdf.circle_grid.values())


def test_adapter_scalar_sampling_preserves_unknown_k_approval_and_data_range():
    from coat_sim.core.film_process_import import _sha256
    profile, capture, inputs = _adapter_inputs()
    snapshot = profile["film_design_snapshot"]
    snapshot["materials"][1]["optical_sets"][0]["k"] = None
    snapshot.pop("sha256"); snapshot["sha256"] = _sha256(snapshot)
    profile["film_design_import"]["snapshot_sha256"] = snapshot["sha256"]
    with pytest.raises(AppearanceInputError, match="unknown k"):
        build_job_from_v7(profile, capture, [500., 600.], inputs)
    snapshot["design"]["assume_lossless"] = True
    snapshot.pop("sha256"); snapshot["sha256"] = _sha256(snapshot)
    profile["film_design_import"]["snapshot_sha256"] = snapshot["sha256"]
    job = build_job_from_v7(profile, capture, [500., 600.], inputs)
    assert job["front_layers"][0]["material"]["assume_lossless"] is True
    assert job["front_layers"][0]["material"]["k"] == [0., 0.]
    with pytest.raises(AppearanceInputError, match="strict optical sampling"):
        build_job_from_v7(profile, capture, [500., 750.], inputs)
