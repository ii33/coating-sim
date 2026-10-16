"""Isolated preview contracts; NumPy equation adapter is explicitly not GPU proof."""
from copy import deepcopy
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.appearance.contract import validate_job
from coat_sim.appearance.render import coherent_stack_rta
from tests.test_appearance_circle import _circle_job, _mock_mitsuba
from tests.test_appearance_result_contract import _job
from tests.test_gpu_tmm_probe import NumpyEquationAdapter
from tools.validation.gpu_appearance_preview import (
    COUNTERS, FATAL_COUNTERS, TOLERANCE, PreviewDeviceTmm, PreviewRejected,
    _cold_jit_observed, _ordered_float64_keys, _decode_ordered_float64_keys,
    _read_stats, image_metrics, json_hash, prepare_faces, preview_scene_dict,
    render_preview, repair_powers_numpy,
)

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


class PreviewNumpyMath(NumpyEquationAdapter):
    cos = staticmethod(np.cos)


def numpy_preview(n, d, ci, zero=None, waves=None):
    n, d, ci = np.asarray(n, complex), np.asarray(d, float), np.asarray(ci, float)
    n = np.broadcast_to(n, (ci.size, n.shape[-1]))
    d = np.broadcast_to(d, (ci.size, n.shape[-1] - 2))
    runtime = object.__new__(PreviewDeviceTmm)
    runtime.dr = PreviewNumpyMath()
    runtime.C = lambda real=0, imag=0: np.asarray(real, complex) + 1j * np.asarray(imag, float)
    runtime.F = lambda value: np.asarray(value, float)
    runtime.B = lambda value: np.full(ci.shape, value, bool)
    w = np.full(ci.shape, 550.) if waves is None else np.asarray(waves, float)
    beta = n[:, 0].real * np.sqrt(np.maximum(0, 1 - ci * ci))
    z = d == 0 if zero is None else np.asarray(zero, bool)
    with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
        out = runtime.solve_preview(w, tuple(n[:, j] for j in range(n.shape[1])),
                                    tuple(d[:, j] for j in range(d.shape[1])), beta, ci,
                                    tuple(z[:, j] for j in range(z.shape[1])))
    return np.column_stack(out[:3]), out


def test_preview_vector_zero_layers_are_identity_at_internal_criticality():
    # A single batch interleaves exactly absent and physically present layers.
    ci = np.sqrt(1 - (np.array([1., 1., 1., 1.]) / 1.5) ** 2)
    d = np.array([[0.], [100.], [0.], [270.]])
    powers, out = numpy_preview([1.5, 1., 1.5], d, ci)
    np.testing.assert_array_equal(powers[[0, 2]], [[0, 1, 0], [0, 1, 0]])
    assert not np.any(out[3])
    assert np.all(out[4]) and np.array_equal(out[5], [True, False, True, False])
    assert np.all(powers[:, 0] >= 0) and np.all(powers[:, 2] >= -1e-14)
    assert powers[1, 0] > 0 and powers[3, 0] > 0


def test_positive_tiny_thickness_is_not_an_identity_mask():
    powers, out = numpy_preview([1.5, 1., 1.5], [1e-100], [np.sqrt(1 - (1/1.5)**2)])
    assert not out[5][0]
    assert np.isfinite(powers).all()


@pytest.mark.parametrize("inside", [False, True])
def test_non_symmetric_absorbing_stack_preserves_actual_order(inside):
    outward = [(2.3 + .03j, 137.), (1.38 + .002j, 93.)]
    incident, exit_n = (1.7, 1.) if inside else (1., 1.7)
    stack = outward if inside else list(reversed(outward))
    angle = 27.
    powers, out = numpy_preview([incident, *(n for n, _ in stack), exit_n],
                               [d for _, d in stack], [np.cos(np.deg2rad(angle))])
    expected = coherent_stack_rta(550, 1.7, outward, angle, incident_from_outside=not inside)
    np.testing.assert_allclose(powers[0], [expected[k] for k in ("R", "T", "A")], atol=2e-14)
    assert not np.any(out[3]) and powers[0, 2] > 0


def test_bare_tir_exact_and_grazing_incident_flux_finite():
    powers, out = numpy_preview([1.5, 1.], [], [.5, .1, 1e-6])
    np.testing.assert_array_equal(powers, np.tile([1, 0, 0], (3, 1)))
    assert not np.any(out[3]) and np.all(out[6])
    powers, out = numpy_preview([1., 1.5], [], [np.cos(np.deg2rad(89.999))])
    assert np.isfinite(powers).all() and powers[0, 1] > 0
    assert not np.any(out[3])


@pytest.mark.parametrize("precision", list(TOLERANCE))
def test_preview_repairs_have_declared_bounds_and_never_qualify_large_errors(precision):
    tau = TOLERANCE[precision]
    values = np.array([[-tau/2, 1+tau/2, 0], [1+tau/2, -tau/2, 0],
                       [.3, .7+tau/2, -tau/2], [.3, .3, .4]])
    fixed, good = repair_powers_numpy(values, precision)
    assert good.all() and (fixed >= 0).all() and (fixed <= 1).all()
    delta = abs(fixed - values)
    assert (delta[:, 0] <= tau).all()
    assert (delta[:, 1] <= 2*tau).all()
    assert (delta[:, 2] <= 6*tau).all()
    bad = np.array([[-2*tau, 1+2*tau, 0], [.3, .3, .3], [np.nan, 1, 0]])
    _, good = repair_powers_numpy(bad, precision)
    assert not good.any()


def test_circle_uses_same_captured_cells_and_original_zero_mask():
    data = validate_job(_circle_job(layers=2))
    prepared = prepare_faces(data)
    expected = data["substrate"]["circle_grids"]["front"]
    mask = expected["active_cell_mask"]
    np.testing.assert_array_equal(prepared["front"]["grid"]["thickness_cell_nm"][:, mask], expected["thickness_cell_nm"][:, mask])
    np.testing.assert_array_equal(prepared["front"]["grid"]["active_cell_mask"], mask)
    assert prepared["back"]["grid"] is None
    assert prepared["bare"]["grid"] is None


def test_rectangular_actual_means_and_shared_grid_requirement():
    job = _circle_job(layers=2)
    job["substrate"]["outline_kind"] = "rectangle"; job["substrate"].pop("radius_mm")
    for row in job["front_layers"]:
        field = row["thickness_field"]
        field["valid_mask"] = np.ones((9, 9), bool).tolist()
        field["thickness_nm"] = np.arange(81).reshape(9, 9).tolist()
        field["valid_cell_mask"] = np.ones((8, 8), bool).tolist()
    data = validate_job(job)
    face = prepare_faces(data)["front"]["grid"]
    assert face["thickness_cell_nm"][0, 0, 0] == 5
    data["front_layers"][1]["x_mm"] = data["front_layers"][1]["x_mm"].copy()
    data["front_layers"][1]["x_mm"][3] += .01
    with pytest.raises(Exception, match="share"):
        prepare_faces(data)


@pytest.mark.parametrize("circle", [False, True])
def test_scene_closed_solid_reuses_camera_source_with_gpu_objects(circle):
    data = validate_job(_circle_job() if circle else _job())
    bsdfs = {name: object() for name in ("front", "back", "bare")}
    scene = preview_scene_dict(_mock_mitsuba(), data, bsdfs)
    body = {name: shape for name, shape in scene.items() if isinstance(shape, dict) and "bsdf" in shape}
    assert len(body) == (3 if circle else 6)
    front = body["front_disk" if circle else "front_0_0"]
    back = body["back_disk" if circle else "back_bare"]
    assert front["bsdf"] is bsdfs["front"] and back["bsdf"] is bsdfs["back"]
    assert all(shape["bsdf"] is bsdfs["bare"] for name, shape in body.items() if name.startswith("side"))
    assert scene["sensor"]["fov"] == data["camera"]["fov_y_deg"]
    assert scene["sensor"]["film"]["width"] == data["resolution"]["width"]
    assert scene["integrator"] == {"type": "path", "max_depth": 12, "rr_depth": 5}
    if circle:
        assert body["side_cylinder"]["p1"][2] == .012
        assert front["flip_normals"] and not back["flip_normals"]


def test_image_metrics_keep_linear_errors_and_highlight_edge_regions():
    reference = np.zeros((4, 5, 3)); reference[2:] = [2, 1, .5]
    actual = reference.copy(); actual[3, 2, 1] += .1
    report = image_metrics(actual, reference)
    assert report["max_abs"] == pytest.approx(.1)
    assert report["highlight_reference_top_5pct"]["pixels"] > 0
    assert report["edge_reference_top_10pct"]["pixels"] > 0
    assert report["channel_mean_actual"][1] > report["channel_mean_reference"][1]
    with pytest.raises(ValueError, match="matching"):
        image_metrics(actual, reference[:2])
    with pytest.raises(PreviewRejected, match="nonfinite"):
        image_metrics(actual * np.nan, reference)


@pytest.mark.parametrize("fatal", FATAL_COUNTERS)
def test_all_invalid_counters_refuse_success(fatal):
    state = {"counters": np.zeros(len(COUNTERS), np.uint32), "stats": np.zeros(10)}
    state["counters"][COUNTERS.index(fatal)] = 1
    assert not _read_stats(state)["pass"]


def test_runtime_failure_leaves_rejection_diagnostics_and_no_display(tmp_path, monkeypatch):
    def unavailable():
        raise ImportError("deliberately absent CUDA runtime")
    monkeypatch.setattr("tools.validation.gpu_appearance_preview._load_runtime", unavailable)
    output = tmp_path / "preview"
    with pytest.raises(ImportError, match="deliberately"):
        render_preview(_job(), output)
    manifest = json.loads((output / "rejected-manifest.json").read_text())
    assert manifest["status"] == "rejected" and manifest["execution"]["workers"] == 1
    assert manifest["diagnostic_statistic_precision"] == "float64"
    assert manifest["diagnostic_statistic_representation"] == "ordered_uint64_of_float64"
    assert manifest["diagnostic_counter_precision"] == "uint32"
    assert manifest["device_gather_index_precision"] == "uint32"
    assert "ImportError" in manifest["traceback"]
    assert manifest["diagnostic_statistics_are_gates"] is False
    assert not (output / "manifest.json").exists()
    assert not (output / "display.png").exists()


def test_device_hit_code_has_no_float_numpy_or_eval_readback():
    source = Path("coat_sim/appearance/gpu.py").read_text()
    tree = ast.parse(source)
    cls = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "GpuCoherentPreviewBSDF")
    for method in (node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in ("_optics", "sample")):
        for node in ast.walk(method):
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Name) and node.func.id in ("float", "bool")), method.name
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr in ("eval", "sync_thread", "asarray", "searchsorted")), method.name
    assert "dr.scatter_reduce" in source and "dr.binary_search" in source
    assert "bs.eta = dr.select(selected_r, 1, mi.Float(1 / eta))" in source


@pytest.mark.real_gpu
def test_real_gpu_bare_context_weights_and_eta():
    if os.environ.get("COAT_SIM_REAL_GPU") != "1":
        pytest.skip("set COAT_SIM_REAL_GPU=1 in the installed GPU renderer interpreter")
    import mitsuba as mi
    import drjit as dr
    from tools.validation.gpu_appearance_preview import _build_gpu_bsdfs, _reset_stats
    mi.set_variant("cuda_ad_rgb")
    data = validate_job(_job())
    with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
        bsdfs, state, _ = _build_gpu_bsdfs(mi, dr, data, "mixed")
        for mode in (mi.TransportMode.Radiance, mi.TransportMode.Importance):
            for inside in (False, True):
                _reset_stats(dr, state, 0)
                si = mi.SurfaceInteraction3f()
                si.wi = mi.Vector3f(0, 0, -1 if inside else 1)
                si.p = mi.Point3f(0, 0, 0)
                ctx = mi.BSDFContext(mode)
                ctx.component = 1
                bs, weight = bsdfs["bare"].sample(ctx, si, mi.Float(.5), mi.Point2f(.3, .7), mi.Bool(True))
                dr.eval(bs, weight, state["counters"], state["stats"]); dr.sync_thread()
                eta = 1.5 if not inside else 1/1.5
                assert float(bs.eta[0]) == pytest.approx(eta)
                assert float(bs.wo.z[0]) == (-1 if not inside else 1)
                expected = .96 * ((1/eta)**2 if mode == mi.TransportMode.Radiance else 1)
                assert float(weight.x[0]) == pytest.approx(expected, rel=2e-6)
                assert _read_stats(state)["pass"]


@pytest.mark.real_gpu
def test_real_gpu_complete_tiny_image(tmp_path):
    if os.environ.get("COAT_SIM_REAL_GPU") != "1":
        pytest.skip("set COAT_SIM_REAL_GPU=1 in the installed GPU renderer interpreter")
    job = _job(); job["resolution"].update(width=8, height=6, spp=4)
    result = render_preview(job, tmp_path / "gpu", precision="float32")
    assert result["status"] == "rendered_unqualified"
    assert len(result["per_wavelength"]) == 2
    assert all(row["diagnostics"]["pass"] for row in result["per_wavelength"])
    assert sum(row["diagnostics"]["active_hits"] for row in result["per_wavelength"]) > 0
    assert np.isfinite(np.load(tmp_path / "gpu" / "xyz.npy")).all()
    assert (tmp_path / "gpu" / "display.png").is_file()


def test_linear_rgb_roi_uses_luminance_not_green_channel():
    reference = np.zeros((2, 2, 3)); reference[0, 0] = [4, 0, 0]
    actual = reference.copy(); actual[0, 0, 0] += .1
    report = image_metrics(actual, reference, color_space="linear_srgb")
    assert report["normalization_reference_luminance_rms"] == pytest.approx(.2126 * 4 / 2)
    assert report["highlight_reference_top_5pct"]["pixels"] == 1
    assert report["highlight_reference_top_5pct"]["rmse"] > 0



def test_cuda_extrema_use_uint64_keys_with_separate_uint32_indices_and_counters():
    source = Path("coat_sim/appearance/gpu.py").read_text()
    assert "U = dr.uint32_array_t(F)" in source
    assert "K, D = dr.uint64_array_t(F), dr.float64_array_t(F)" in source
    assert '"stats": K(' in source
    assert 'dr.scatter_reduce(op, state["stats"], ordered, U(index)' in source
    assert 'abs(r - R) > F(tau)' in source
    assert 'dr.scatter_reduce(dr.ReduceOp.Add, state["counters"], U(mask)' in source
    tree = ast.parse(source)
    search = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "binary_search")
    assert isinstance(search.args[0], ast.Constant) and search.args[0].value == 0
    assert isinstance(search.args[1], ast.Name) and search.args[1].id == "size"


def test_ordered_uint64_extrema_preserve_float64_bits_and_order():
    values = np.array([-np.inf, -1e300, -1, -np.nextafter(0., 1.), -0.,
                       0., np.nextafter(0., 1.), 1, 1e300, np.inf], np.float64)
    keys = _ordered_float64_keys(values)
    decoded = _decode_ordered_float64_keys(keys)
    np.testing.assert_array_equal(decoded.view(np.uint64), values.view(np.uint64))
    assert np.all(keys[1:] > keys[:-1])
    stats = values[[2, 6, 5, 8, 7, 4, 6, 6, 6, 6]]
    state = {"stats": _ordered_float64_keys(stats), "stats_ordered_uint64": True,
             "counters": np.zeros(len(COUNTERS), np.uint32)}
    read = _read_stats(state)
    assert read["minimum_R"] == -1 and read["minimum_T"] == np.nextafter(0., 1.)
    assert read["maximum_R"] == 1e300 and read["pass"]


@pytest.mark.parametrize("history, expected", [([{ "cache_hit": 0}], True),
    ([{"cache_hit": False}], True), ([{"cache_hit": 1}], False),
    ([{"cache_hit": True}], False), ([{}], False), ([], False)])
def test_cold_jit_cache_history_accepts_integer_zero(history, expected):
    assert _cold_jit_observed(history) is expected


@pytest.mark.real_gpu
def test_real_gpu_coated_nonuniform_grid_and_complete_tiny_image(tmp_path):
    if os.environ.get("COAT_SIM_REAL_GPU") != "1":
        pytest.skip("set COAT_SIM_REAL_GPU=1 in the installed GPU renderer interpreter")
    import mitsuba as mi
    import drjit as dr
    from tools.validation.gpu_appearance_preview import _build_gpu_bsdfs, _reset_stats
    axis = np.array([-2., -1.75, -1., -.15, 0., .1, .7, 1.6, 2.])
    job = _circle_job(x=axis, y=axis, layers=2)
    job["resolution"].update(width=8, height=6, spp=4)
    job["area_source"]["position_mm"] = [0., 0., 30.]  # Outside 12 mm solid.
    for layer in job["front_layers"]:
        field = layer["thickness_field"]
        values, valid = np.asarray(field["thickness_nm"], object), np.asarray(field["valid_mask"], bool)
        values[~valid] = None
        field["thickness_nm"] = values.tolist()
    mi.set_variant("cuda_ad_rgb")
    with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
        bsdfs, state, _ = _build_gpu_bsdfs(mi, dr, validate_job(job), "mixed")
        _reset_stats(dr, state, 0)
        si = mi.SurfaceInteraction3f()
        si.p, si.wi = mi.Point3f(0, 0, 0), mi.Vector3f(0, 0, 1)
        bs, weight = bsdfs["front"].sample(mi.BSDFContext(), si, mi.Float(.25), mi.Point2f(.3, .7), mi.Bool(True))
        dr.eval(bs, weight, state["counters"], state["stats"]); dr.sync_thread()
        assert _read_stats(state)["active_hits"] == 1  # Actually sampled coated face.
        assert _read_stats(state)["pass"] and np.isfinite(np.asarray(weight)).all()
    result = render_preview(job, tmp_path / "coated-gpu", precision="float32")
    assert result["status"] == "rendered_unqualified"
    assert all(row["diagnostics"]["pass"] for row in result["per_wavelength"])
    assert sum(row["diagnostics"]["active_hits"] for row in result["per_wavelength"]) > 0
    assert np.isfinite(np.load(tmp_path / "coated-gpu" / "xyz.npy")).all()
    assert (tmp_path / "coated-gpu" / "display.png").is_file()
