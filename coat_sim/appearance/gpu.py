#!/usr/bin/env python3
"""Opt-in full-image CUDA appearance renderer.

Use an existing Mitsuba/Dr.Jit interpreter. No packages are installed and no CPU
fallback is taken. Input is an actual schema-1 frozen appearance job, not a spec
wrapper. See docs/gpu-preview-contract.md for approximations and refusal rules.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
import subprocess
from pathlib import Path
import platform
import sys
import time
import traceback

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from coat_sim.appearance.contract import AppearanceInputError, validate_job
from coat_sim.appearance.render import _scene_dict, display_rgb, write_png
from coat_sim.appearance.progress import RenderProgress
from coat_sim.appearance.gpu_tmm_stable import StableDrjitTmm, NEAR_Q_RELATIVE, SMALL_DELTA

TOLERANCE = {"float32": 2e-5, "mixed": 2e-9}  # PROJECT preview conventions.
COUNTERS = ("active_hits", "kernel_invalid", "nonfinite", "passivity_failure",
            "balance_failure", "geometry_failure", "direction_failure", "repair_bound_failure", "roundoff_repaired",
            "raw_negative", "raw_above_one", "tir_transmission_failure",
            "near_critical", "zero_thickness_hit", "bare_tir")
FATAL_COUNTERS = ("kernel_invalid", "nonfinite", "passivity_failure",
                  "balance_failure", "geometry_failure", "direction_failure", "repair_bound_failure", "tir_transmission_failure")
STAT_NAMES = ("minimum_R", "minimum_T", "minimum_A", "maximum_R", "maximum_T", "maximum_A",
              "max_raw_balance_error", "max_R_repair", "max_T_repair", "max_A_repair")
XYZ_TO_RGB = np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415],
                       [0.0557, -0.2040, 1.0570]], dtype=np.float32)


class PreviewRejected(RuntimeError):
    pass


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _write_input_job(path: Path, job: dict) -> None:
    """Preserve every captured field without pretty-JSON amplification on disk."""
    path.write_bytes((json.dumps(job, sort_keys=True, separators=(",", ":"),
                                allow_nan=False) + "\n").encode("utf-8"))


def prepare_faces(data):
    """Host-once immutable cells; no optical calculation or nominal substitution."""
    faces = {}
    circle = data["substrate"].get("outline_kind") == "circle"
    for name in ("front", "back"):
        layers = data[name + "_layers"]
        if not layers:
            faces[name] = {"layers": [], "grid": None}
            continue
        x, y = layers[0]["x_mm"], layers[0]["y_mm"]
        if any(not np.array_equal(row[axis], layers[0][axis])
               for row in layers for axis in ("x_mm", "y_mm")):
            raise AppearanceInputError("all layers on a face must share the actual captured grid")
        if circle:
            grid = data["substrate"]["circle_grids"][name]
            d, active = grid["thickness_cell_nm"], grid["active_cell_mask"]
        else:
            d = np.stack([(row["thickness_nm"][:-1, :-1] + row["thickness_nm"][:-1, 1:] +
                           row["thickness_nm"][1:, :-1] + row["thickness_nm"][1:, 1:]) / 4
                          for row in layers])
            active = np.ones(d.shape[1:], dtype=bool)
        # Exterior GPU slots hold zero but are never a valid physical hit.
        d = np.where(active[None, ...], d, 0.0)
        faces[name] = {"layers": layers, "grid": {"x_mm": x, "y_mm": y,
                        "thickness_cell_nm": d, "active_cell_mask": active,
                        "exact_zero": d == 0}}
    faces["bare"] = {"layers": [], "grid": None}
    return faces


def repair_powers_numpy(values, precision):
    """Test oracle for declared repair only; never called on ray-hit data."""
    values = np.asarray(values, dtype=np.float64)
    tau = TOLERANCE[precision]
    finite = np.isfinite(values).all(axis=-1)
    good = finite & (values >= -tau).all(axis=-1) & (values <= 1 + tau).all(axis=-1)
    good &= np.abs(values.sum(axis=-1) - 1) <= 3 * tau
    r = np.clip(values[..., 0], 0, 1)
    t = np.minimum(np.maximum(values[..., 1], 0), 1 - r)
    fixed = np.stack((r, t, 1 - r - t), axis=-1)
    return fixed, good


def image_metrics(actual, reference, *, color_space="xyz"):
    """Un-denoised linear metrics; no random-path correspondence assumption."""
    actual, reference = np.asarray(actual, np.float64), np.asarray(reference, np.float64)
    if actual.shape != reference.shape or actual.ndim != 3 or actual.shape[-1] != 3:
        raise ValueError("image comparison requires matching HxWx3 arrays")
    if not np.isfinite(actual).all() or not np.isfinite(reference).all():
        raise PreviewRejected("nonfinite linear image in comparison")
    delta = actual - reference
    if color_space == "xyz":
        luminance = reference[..., 1]
        luminance_rule = "CIE XYZ Y channel"
    elif color_space == "linear_srgb":
        luminance = np.einsum("...j,j->...", reference, [.2126, .7152, .0722])
        luminance_rule = "linear sRGB Y = .2126 R + .7152 G + .0722 B"
    else:
        raise ValueError("color_space must be xyz or linear_srgb")
    scale = max(float(np.sqrt(np.mean(luminance ** 2))), 1e-12)
    highlight = luminance >= np.quantile(luminance, .95)
    edge_strength = np.zeros(luminance.shape)
    if luminance.shape[0] > 1:
        edge_strength[1:] += np.abs(np.diff(luminance, axis=0))
    if luminance.shape[1] > 1:
        edge_strength[:, 1:] += np.abs(np.diff(luminance, axis=1))
    edges = edge_strength >= np.quantile(edge_strength, .9)
    def region(mask):
        return {"pixels": int(mask.sum()), "rmse": float(np.sqrt(np.mean(delta[mask] ** 2))),
                "normalized_rmse": float(np.sqrt(np.mean(delta[mask] ** 2)) / scale)}
    return {"max_abs": float(np.max(np.abs(delta))), "p99_abs": float(np.quantile(np.abs(delta), .99)),
            "rmse": float(np.sqrt(np.mean(delta ** 2))), "normalized_rmse": float(np.sqrt(np.mean(delta ** 2)) / scale),
            "normalization_reference_luminance_rms": scale, "luminance_rule": luminance_rule,
            "color_space": color_space,
            "channel_mean_actual": actual.mean(axis=(0, 1)).tolist(),
            "channel_mean_reference": reference.mean(axis=(0, 1)).tolist(),
            "highlight_reference_top_5pct": region(highlight), "edge_reference_top_10pct": region(edges)}


def preview_scene_dict(mi, data, bsdfs):
    """Reuse production camera/source/body transforms without scalar BSDFs."""
    bare_data = dict(data)
    bare_data["front_layers"], bare_data["back_layers"] = [], []
    bare_data["substrate"] = dict(data["substrate"])
    bare_data["substrate"]["back_surface"] = {"status": "uncoated", "outside_medium": "air"}
    scene = _scene_dict(mi, bare_data, data["wavelength_nm"][0],
                        data["environment_values"][0], data["area_source"]["spectrum_values"][0])
    for name, shape in scene.items():
        if isinstance(shape, dict) and "bsdf" in shape:
            face = "front" if name.startswith("front") else "back" if name.startswith("back") else "bare"
            shape["bsdf"] = bsdfs[face]
    return scene


class PreviewDeviceTmm(StableDrjitTmm):
    """Vector-thickness adapter; exact-zero masks originate before quantization."""

    def _field_candidate(self, w, n, d, q, p, zero):
        dr, C, F = self.dr, self.C, self.F
        y0 = q[0] if p == "s" else n[0] * n[0] / self._safe(q[0])
        exit_zero = self._zero(q[-1])
        if p == "p":
            ye = dr.select(exit_zero, C(0), n[-1] * n[-1] / self._safe(q[-1]))
            e, h = dr.select(exit_zero, C(0), C(1)), dr.select(exit_zero, C(1), ye)
        else:
            ye, e, h = q[-1], C(1), q[-1]
        factor, invalid = C(1), self.B(False)
        for j in range(len(n) - 2, 0, -1):
            length = 2 * np.pi * d[j - 1] / w
            delta = q[j] * length
            phase = dr.exp(C(0, 1) * delta)
            small = dr.abs(delta) <= SMALL_DELTA
            guarded = dr.select(small, delta, C(0))
            z2 = guarded * guarded
            sinc = 1 + z2 * (-1 / 6 + z2 * (1 / 120 + z2 * (-1 / 5040 + z2 / 362880)))
            off_q = dr.select(small, C(0, -1) * phase * length * sinc,
                              (1 - phase * phase) / (2 * self._safe(q[j])))
            diagonal = dr.select(small, phase * dr.cos(guarded), (1 + phase * phase) / 2)
            if p == "s":
                upper, lower = off_q, off_q * q[j] * q[j]
            else:
                n2 = n[j] * n[j]
                upper, lower = off_q * q[j] * q[j] / n2, off_q * n2
            ne, nh = diagonal * e + upper * h, lower * e + diagonal * h
            norm = dr.maximum(dr.abs(ne), dr.abs(nh))
            good = dr.isfinite(norm) & (norm > 0)
            invalid |= ~good & ~zero[j - 1]
            norm = dr.select(good, norm, F(1))
            # d==0 means no physical layer; skip even normalization exactly.
            e = dr.select(zero[j - 1], e, ne / norm)
            h = dr.select(zero[j - 1], h, nh / norm)
            factor = dr.select(zero[j - 1], factor, factor * phase / norm)
        den = y0 * e + h
        invalid |= self._zero(den) | self._zero(q[0])
        return self._powers((y0 * e - h) / self._safe(den),
                            2 * y0 * factor / self._safe(den), y0.real, ye.real, invalid)

    def solve_preview(self, w, n, d, beta, ci, zero):
        dr = self.dr
        if dr.flag(dr.JitFlag.FastMath):
            raise RuntimeError("preview requires FastMath=False during tracing and execution")
        q0 = n[0] * ci  # Transparent incident port: stable even at grazing.
        q = [q0]
        for item in n[1:]:
            value = dr.sqrt((item - beta) * (item + beta))
            flip = (value.imag < 0) | ((value.imag == 0) & (value.real < 0))
            value = dr.select(flip, -value, value)
            # Equal transparent indices share q0, including an identity stack.
            equal = (item.real == n[0].real) & (item.imag == 0)
            q.append(dr.select(equal, q0, value))
        # The transparent exit port uses the same Snell quantity as the TIR
        # test. Computing it from rounded beta independently can disagree.
        eta = n[0].real / n[-1].real
        sin2_t = eta * eta * dr.maximum(0, 1 - ci * ci)
        exit_q = n[-1] * dr.sqrt(self.C(1 - sin2_t, 0))
        q[-1] = dr.select(n[-1].real == n[0].real, q0, exit_q)
        near, any_zero, all_zero = self.B(False), self.B(False), self.B(True)
        for item, value, absent in zip(n[1:-1], q[1:-1], zero):
            near |= dr.abs(value) <= NEAR_Q_RELATIVE * dr.maximum(1, dr.abs(item))
            any_zero |= absent
            all_zero &= absent
        values = []
        for p in ("s", "p"):
            regular = self._regular(w, n, d, q, p)
            field = self._field_candidate(w, n, d, q, p, zero)
            values.append(tuple(dr.select(near | any_zero, f, r) for f, r in zip(field, regular)))
        powers = tuple((values[0][j] + values[1][j]) * .5 for j in range(3))
        invalid = values[0][3] | values[1][3]
        # Exact all-zero layers have the bare transparent port interface.
        eta = n[0].real / n[-1].real
        sin2_t = eta * eta * dr.maximum(0, 1 - ci * ci)
        matched = n[0].real == n[-1].real
        tir = (sin2_t >= 1) & ~matched
        ct = dr.select(matched, ci, dr.sqrt(dr.maximum(0, 1 - sin2_t)))
        rs = (n[0].real * ci - n[-1].real * ct) / (n[0].real * ci + n[-1].real * ct)
        rp = (n[-1].real * ci - n[0].real * ct) / (n[-1].real * ci + n[0].real * ct)
        br = dr.select(tir, self.F(1), .5 * (rs * rs + rp * rp))
        br = dr.select(n[0].real == n[-1].real, self.F(0), br)
        bt = 1 - br
        powers = tuple(dr.select(all_zero, bare, value)
                       for value, bare in zip(powers, (br, bt, self.F(0))))
        invalid = dr.select(all_zero, self.B(False), invalid)
        return (*powers, invalid, near, any_zero, all_zero & tir, tir)


def _load_runtime():
    mi, dr = importlib.import_module("mitsuba"), importlib.import_module("drjit")
    if "cuda_ad_rgb" not in mi.variants():
        raise RuntimeError("Mitsuba cuda_ad_rgb variant is unavailable; no CPU fallback")
    if not dr.has_backend(dr.JitBackend.CUDA):
        raise RuntimeError("Dr.Jit CUDA backend is unavailable; no CPU fallback")
    # Compute CMF once before selecting GPU, never switch variant mid-scene.
    mi.set_variant("scalar_spectral")
    return mi, dr


def _ordered_float64_keys(values):
    bits = np.asarray(values, dtype=np.float64).view(np.uint64)
    sign = np.uint64(1 << 63)
    return np.where((bits & sign) != 0, ~bits, bits ^ sign).astype(np.uint64)


def _decode_ordered_float64_keys(keys):
    keys = np.asarray(keys, dtype=np.uint64)
    sign = np.uint64(1 << 63)
    bits = np.where((keys & sign) != 0, keys ^ sign, ~keys).astype(np.uint64)
    return bits.view(np.float64)


def _build_gpu_bsdfs(mi, dr, data, precision):
    solver = PreviewDeviceTmm("cuda", "float32" if precision == "float32" else "float64")
    F, C, B = solver.F, solver.C, solver.B
    U = dr.uint32_array_t(F)
    K, D = dr.uint64_array_t(F), dr.float64_array_t(F)
    dtype = np.float32 if precision == "float32" else np.float64
    tau = TOLERANCE[precision]
    state = {"wave": U([0]), "counters": U(np.zeros(len(COUNTERS), dtype=np.uint32)),
             # CUDA has no Float64 atomic min/max in Dr.Jit1.4. Ordered
             # UInt64 keys preserve every Float64 diagnostic bit exactly.
             "stats": K(_ordered_float64_keys([math.inf] * 3 + [-math.inf] * 3 + [0] * 4)),
             "stats_ordered_uint64": True,
             "wavelength": F(data["wavelength_nm"]),
             "substrate_n": F(data["substrate"]["material"]["n_values"])}
    dr.make_opaque(state)
    faces = prepare_faces(data)
    uploaded = []
    for face in faces.values():
        grid = face["grid"]
        if grid is None:
            face["device"] = None
            continue
        original = grid["thickness_cell_nm"]
        quantized = original.astype(dtype)
        for axis_name in ("x_mm", "y_mm"):
            axis = grid[axis_name].astype(dtype)
            if not np.isfinite(axis).all() or not np.all(np.diff(axis) > 0):
                raise PreviewRejected("captured grid axis collapses or overflows selected preview precision")
        if np.any((original > 0) & (quantized == 0)) or not np.isfinite(quantized).all():
            raise PreviewRejected("positive thickness underflows or overflows selected preview precision")
        face["device"] = {"x": F(grid["x_mm"]), "y": F(grid["y_mm"]),
                          "nx": grid["x_mm"].size, "ny": grid["y_mm"].size,
                          "active": B(grid["active_cell_mask"].ravel()),
                          "d": tuple(F(row.ravel()) for row in quantized),
                          "zero": tuple(B(row.ravel()) for row in grid["exact_zero"]),
                          "n": tuple((F(row["material"]["n_values"]), F(row["material"]["k_values"]))
                                     for row in face["layers"])}
        for row in face["layers"]:
            k = row["material"]["k_values"]
            if np.any((k > 0) & (k.astype(dtype) == 0)):
                raise PreviewRejected("positive film absorption underflows selected precision")
        dr.make_opaque(face["device"])
        uploaded.append(face["device"])
    dr.eval(state, uploaded)
    dr.sync_thread()

    def count(name, mask, active):
        dr.scatter_reduce(dr.ReduceOp.Add, state["counters"], U(mask), U(COUNTERS.index(name)), active=active)

    def stat(index, value, active, op):
        bits = dr.reinterpret_array(K, D(value))
        sign = K(1 << 63)
        ordered = dr.select((bits & sign) != 0, ~bits, bits ^ sign)
        dr.scatter_reduce(op, state["stats"], ordered, U(index), active=active)

    class GpuCoherentPreviewBSDF(mi.BSDF):
        def __init__(self, props, face):
            mi.BSDF.__init__(self, props)
            self.face = face
            reflection = mi.BSDFFlags.DeltaReflection | mi.BSDFFlags.FrontSide | mi.BSDFFlags.BackSide
            transmission = (mi.BSDFFlags.DeltaTransmission | mi.BSDFFlags.FrontSide |
                            mi.BSDFFlags.BackSide | mi.BSDFFlags.NonSymmetric)
            self.m_components, self.m_flags = [reflection, transmission], reflection | transmission

        def _optics(self, si, active):
            wave = dr.gather(U, state["wave"], U(0))
            w = dr.gather(F, state["wavelength"], wave)
            ns = dr.gather(F, state["substrate_n"], wave)
            inside = F(dr.detach(si.wi.z)) < 0
            ni, nt = dr.select(inside, ns, F(1)), dr.select(inside, F(1), ns)
            # Match existing 89.999deg optical cap. No acos and no beta/n
            # cancellation for the incident port; directions retain real wi.
            ci = dr.minimum(1, dr.maximum(abs(F(dr.detach(si.wi.z))), F(math.cos(math.radians(89.999)))))
            beta = ni * dr.sqrt(dr.maximum(0, 1 - ci * ci))
            device = self.face["device"]
            geometry_bad = (~dr.isfinite(F(dr.detach(si.wi.x))) |
                            ~dr.isfinite(F(dr.detach(si.wi.y))) |
                            ~dr.isfinite(F(dr.detach(si.wi.z))))
            nrows, ds, zs = [], [], []
            if device is not None:  # Constant face topology, never a lane branch.
                coordinates = [F(dr.detach(si.p.x)) * 1000, F(dr.detach(si.p.y)) * 1000]
                cell_axes = []
                for axis_name, size, coordinate in zip(("x", "y"), (device["nx"], device["ny"]), coordinates):
                    axis = device[axis_name]
                    lo, hi = dr.gather(F, axis, U(0)), dr.gather(F, axis, U(size - 1))
                    tolerance = dr.maximum(1, dr.maximum(abs(lo), abs(hi))) * (8 * np.finfo(np.float32).eps)
                    geometry_bad |= ~dr.isfinite(coordinate) | (coordinate < lo - tolerance) | (coordinate > hi + tolerance)
                    coordinate = dr.minimum(hi, dr.maximum(lo, coordinate))
                    # lower_bound of (axis <= hit) minus one, clipped endpoints.
                    upper = dr.binary_search(0, size,
                            lambda index: dr.gather(F, axis, dr.minimum(index, U(size - 1))) <= coordinate)
                    cell_axes.append(dr.minimum(U(size - 2), dr.maximum(U(1), upper) - 1))
                index = cell_axes[1] * (device["nx"] - 1) + cell_axes[0]
                geometry_bad |= ~dr.gather(B, device["active"], index)
                for (nr, nk), thickness, zero in zip(device["n"], device["d"], device["zero"]):
                    nrows.append(C(dr.gather(F, nr, wave), dr.gather(F, nk, wave)))
                    ds.append(dr.gather(F, thickness, index))
                    zs.append(dr.gather(B, zero, index))
            # Input layer order is substrate -> outward. Inside keeps order.
            n = (C(ni), *(dr.select(inside, a, b) for a, b in zip(nrows, reversed(nrows))), C(nt))
            d = tuple(dr.select(inside, a, b) for a, b in zip(ds, reversed(ds)))
            zero = tuple(dr.select(inside, a, b) for a, b in zip(zs, reversed(zs)))
            R, T, A, invalid, near, absent, bare_tir, tir = solver.solve_preview(w, n, d, beta, ci, zero)
            finite = dr.isfinite(R) & dr.isfinite(T) & dr.isfinite(A)
            passivity_bad = (R < -tau) | (T < -tau) | (A < -tau) | (R > 1 + tau) | (T > 1 + tau) | (A > 1 + tau)
            balance = abs(R + T + A - 1)
            balance_bad = balance > 3 * tau
            tir_bad = tir & (T != 0)
            # Genuine coated absorbing TIR still permits absorption, but T=0.
            r = dr.minimum(1, dr.maximum(0, R))
            t = dr.minimum(dr.maximum(0, T), 1 - r)
            a = 1 - r - t
            repair_bad = (abs(r - R) > F(tau)) | (abs(t - T) > F(2*tau)) | (abs(a - A) > F(6*tau))
            good = finite & ~invalid & ~passivity_bad & ~balance_bad & ~geometry_bad & ~tir_bad & ~repair_bad
            count("active_hits", B(True), active)
            for name, mask in (("kernel_invalid", invalid), ("nonfinite", ~finite),
                    ("passivity_failure", passivity_bad), ("balance_failure", balance_bad),
                    ("geometry_failure", geometry_bad), ("repair_bound_failure", repair_bad), ("roundoff_repaired", (r != R) | (t != T) | (a != A)),
                    ("raw_negative", (R < 0) | (T < 0) | (A < 0)),
                    ("raw_above_one", (R > 1) | (T > 1) | (A > 1)),
                    ("tir_transmission_failure", tir_bad), ("near_critical", near),
                    ("zero_thickness_hit", absent), ("bare_tir", bare_tir)):
                count(name, mask, active)
            for j, (raw, fixed) in enumerate(zip((R, T, A), (r, t, a))):
                stat(j, raw, active & finite, dr.ReduceOp.Min)
                stat(j + 3, raw, active & finite, dr.ReduceOp.Max)
                stat(j + 7, abs(raw - fixed), active & finite, dr.ReduceOp.Max)
            stat(6, balance, active & finite, dr.ReduceOp.Max)
            return mi.Float(dr.select(good, r, F(0))), mi.Float(dr.select(good, t, F(0))), ni / nt, good

        def sample(self, ctx, si, sample1, sample2, active=True):
            bs = mi.BSDFSample3f()
            R, T, eta, good = self._optics(si, active)
            active = active & mi.Bool(good)
            allow_r = ctx.is_enabled(mi.BSDFFlags.DeltaReflection, 0)
            allow_t = ctx.is_enabled(mi.BSDFFlags.DeltaTransmission, 1)
            if allow_r and allow_t:  # Scalar BSDF context, not per-ray data.
                selected_r, selected_t = sample1 < R, (sample1 >= R) & (sample1 < R + T)
                pdf = dr.select(selected_r, R, T)
                weight = mi.Float(1)
            elif allow_r:
                selected_r, selected_t, pdf, weight = R > 0, mi.Bool(False), mi.Float(1), R
            elif allow_t:
                selected_r, selected_t, pdf, weight = mi.Bool(False), T > 0, mi.Float(1), T
            else:
                selected_r, selected_t, pdf, weight = mi.Bool(False), mi.Bool(False), mi.Float(0), mi.Float(0)
            selected_r &= active
            # Use the actual geometric direction for Snell, like production.
            wi_x, wi_y, wi_z = (F(dr.detach(value)) for value in (si.wi.x, si.wi.y, si.wi.z))
            sin2_t = eta * eta * dr.maximum(0, 1 - wi_z * wi_z)
            can_t = (sin2_t < 1) | (eta == 1)
            count("direction_failure", (T > 0) & ~can_t, active)
            selected_t &= active & mi.Bool(can_t)
            ct = dr.select(eta == 1, abs(wi_z), dr.sqrt(dr.maximum(0, 1 - sin2_t)))
            direction = mi.Vector3f(mi.Float(-eta * wi_x), mi.Float(-eta * wi_y),
                                   mi.Float(dr.select(wi_z >= 0, -ct, ct)))
            bs.wo = dr.select(selected_r, mi.reflect(si.wi), direction)
            outgoing_finite = dr.isfinite(bs.wo.x) & dr.isfinite(bs.wo.y) & dr.isfinite(bs.wo.z)
            count("direction_failure", B(dr.detach(~outgoing_finite)), active)
            bs.pdf = dr.select(selected_r | selected_t, pdf, 0)
            bs.sampled_component = dr.select(selected_r, mi.UInt32(0), mi.UInt32(1))
            bs.sampled_type = dr.select(selected_r, mi.UInt32(int(mi.BSDFFlags.DeltaReflection)),
                                       mi.UInt32(int(mi.BSDFFlags.DeltaTransmission)))
            bs.eta = dr.select(selected_r, 1, mi.Float(1 / eta))  # Mitsuba eta_t / eta_i convention.
            if ctx.mode == mi.TransportMode.Radiance:
                weight *= dr.select(selected_t, mi.Float(eta * eta), 1)
            return bs, mi.Color3f(dr.select(selected_r | selected_t, weight, 0))

        def eval(self, ctx, si, wo, active=True):
            return mi.Color3f(0)

        def pdf(self, ctx, si, wo, active=True):
            return mi.Float(0)

        def eval_pdf(self, ctx, si, wo, active=True):
            return mi.Color3f(0), mi.Float(0)

        def to_string(self):
            return "GpuCoherentPreviewBSDF[resident=" + precision + "]"

    # Passing concrete BSDF objects into load_dict avoids a global factory and
    # keeps every invocation's resident buffers owned by its live scene.
    bsdfs = {name: GpuCoherentPreviewBSDF(mi.Properties(), face) for name, face in faces.items()}
    return bsdfs, state, solver


def _reset_stats(dr, state, wave):
    U, F = type(state["wave"]), type(state["stats"])
    dr.scatter(state["wave"], U(wave), U(0))
    dr.scatter(state["counters"], U(np.zeros(len(COUNTERS), dtype=np.uint32)), dr.arange(U, len(COUNTERS)))
    initial = [math.inf] * 3 + [-math.inf] * 3 + [0] * 4
    values = _ordered_float64_keys(initial) if state.get("stats_ordered_uint64") else initial
    dr.scatter(state["stats"], F(values), dr.arange(U, len(STAT_NAMES)))
    dr.eval(state["wave"], state["counters"], state["stats"])
    dr.sync_thread()


def _read_stats(state):
    counts = np.asarray(state["counters"])
    stats = (_decode_ordered_float64_keys(np.asarray(state["stats"]))
             if state.get("stats_ordered_uint64") else np.asarray(state["stats"], dtype=np.float64))
    result = {name: int(value) for name, value in zip(COUNTERS, counts)}
    result.update({name: float(value) if np.isfinite(value) else None for name, value in zip(STAT_NAMES, stats)})
    result["pass"] = not any(result[name] for name in FATAL_COUNTERS)
    return result


def _cold_jit_observed(history):
    """Installed Dr.Jit reports cache_hit as integer 0/1 as well as bool."""
    return any(row.get("cache_hit") == 0 for row in history)


def _kernel_record(dr):
    """Support both Dr.Jit 1.2 list API and newer context-manager API."""
    if hasattr(dr.JitFlag, "KernelHistory"):
        dr.set_flag(dr.JitFlag.KernelHistory, True)
        return None
    context = dr.kernel_history()
    context.__enter__()
    return context


def _finish_kernel_record(dr, context):
    if context is None:
        history = dr.kernel_history()
    else:
        context.__exit__(None, None, None)
        history = context
    rows = []
    for entry in history:
        if isinstance(entry, dict):
            items = entry.items()
        else:
            items = ((name, getattr(entry, name)) for name in
                     ("type", "backend", "execution_time", "codegen_time", "compile_time", "cache_hit", "hash")
                     if hasattr(entry, name))
        row = {}
        for key, value in items:
            if isinstance(value, (str, int, float, bool)) or value is None:
                row[key] = value
            elif key in ("backend", "type"):
                row[key] = str(value)
        rows.append(row)
    return rows


def compare_reference(data, output, reference, effective_job):
    reference = Path(reference)
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    expected_resolution = data["resolution"]
    actual_resolution = manifest["resolution"]
    if any(actual_resolution[k] != expected_resolution[k] for k in ("width", "height")):
        raise PreviewRejected("CPU reference resolution differs")
    if actual_resolution["spp"] < expected_resolution["spp"]:
        raise PreviewRejected("CPU reference SPP is below preview SPP")
    if manifest["wavelength_nm"] != data["wavelength_nm"].tolist():
        raise PreviewRejected("CPU reference wavelengths differ")
    if "exact_per_hit_coherent_tmm" != manifest.get("angular_optics", {}).get("method"):
        raise PreviewRejected("reference must use exact-per-hit CPU coherent TMM")
    # The CPU manifest input hash is its own canonical format. Require a saved
    # schema-1 reference input beside it, then compare all physical content.
    input_path = reference / "input-job.json"
    if not input_path.is_file():
        raise PreviewRejected("CPU reference requires input-job.json for same-physical-input verification")
    ref_job = json.loads(input_path.read_text(encoding="utf-8"))
    left, right = deepcopy(effective_job), deepcopy(ref_job)
    left["resolution"].pop("spp", None); right["resolution"].pop("spp", None)
    left["resolution"].pop("seed", None); right["resolution"].pop("seed", None)
    if json_hash(left) != json_hash(right):
        raise PreviewRejected("CPU reference physical job differs (only seed/SPP may differ)")
    return {"reference_directory": str(reference), "reference_spp": actual_resolution["spp"],
            "reference_seed": actual_resolution["seed"], "denoised": False,
            "xyz": image_metrics(np.load(output / "xyz.npy"), np.load(reference / "xyz.npy")),
            "linear_srgb": image_metrics(np.load(output / "linear_srgb.npy"), np.load(reference / "linear_srgb.npy"), color_space="linear_srgb"),
            "qualification": "comparison_only; inspect noise floor and color/highlight/edge errors"}


def render_preview(job, output_dir, *, precision="float32", reference=None,
                   exposure=.01, warm_repeat=True, cancel_event=None,
                   progress_callback=None):
    whole_start = time.perf_counter()
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("GPU appearance render cancelled before validation")
    if precision not in TOLERANCE:
        raise ValueError("precision must be float32 or mixed")
    if not math.isfinite(exposure) or exposure <= 0:
        raise ValueError("exposure must be finite and positive")
    data = validate_job(job, max_wavelength_samples=None)
    estimated_wavelength_samples = (data["resolution"]["width"] * data["resolution"]["height"] *
                                    data["resolution"]["spp"] * len(data["wavelength_nm"]))
    budget_warnings = (["GPU preview exceeds the legacy 20 million scalar wavelength sample budget; "
                       "aggregate budget disabled explicitly, other validation limits retained"]
                       if estimated_wavelength_samples > 20_000_000 else [])
    if data["resolution"]["seed"] + len(data["wavelength_nm"]) - 1 > 0xffffffff:
        raise ValueError("base_seed + wavelength_index exceeds Mitsuba UInt32 seed range")
    # UInt32 atomic counters are reset each wave and each path has max_depth=12.
    if data["resolution"]["width"] * data["resolution"]["height"] * data["resolution"]["spp"] * 12 >= 2**32:
        raise ValueError("per-wave diagnostics could overflow UInt32 counters")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    progress = RenderProgress(len(data["wavelength_nm"]), progress_callback)
    progress.emit("preflight")
    manifest = {"schema": "coat_sim.gpu_appearance_preview", "schema_version": 1,
                "status": "running", "created_utc": datetime.now(timezone.utc).isoformat(),
                "input_sha256": json_hash(job), "wavelength_nm": data["wavelength_nm"].tolist(),
                "resolution": data["resolution"], "precision": precision,
                "estimated_wavelength_samples": estimated_wavelength_samples,
                "max_wavelength_samples": None,
                "legacy_max_wavelength_samples": 20_000_000,
                "execution_budget_warnings": budget_warnings,
                "geometry_precision": "float32", "film_precision": "float32",
                "optical_precision": "float32" if precision == "float32" else "float64",
                "diagnostic_statistic_precision": "float64",
                "diagnostic_statistic_representation": "ordered_uint64_of_float64",
                "diagnostic_statistics_are_gates": False,
                "fatal_and_repair_bound_comparison_precision": "float32" if precision == "float32" else "float64",
                "diagnostic_counter_precision": "uint32",
                "device_gather_index_precision": "uint32",
                "execution": {"variant": "cuda_ad_rgb", "workers": 1, "wavelength_order": "sequential_input_index",
                              "resident_geometry_and_thickness": True, "resident_material_spectra": True,
                              "per_ray_host_work": False, "fastmath": False},
                "preview_roundoff_tolerance": TOLERANCE[precision],
                "preview_repair_bound": {key: float((np.float32 if precision == "float32" else np.float64)(factor*TOLERANCE[precision]))
                                         for key, factor in (("R", 1), ("T", 2), ("A", 6))},
                "transport": "path max_depth=12 rr_depth=5; unpolarized coherent delta R/T/A; absorption termination; radiance eta_i_over_t squared",
                "input_provenance": data["provenance"], "input_warnings": [*data["input_warnings"], *budget_warnings],
                "back_surface": data["substrate"]["back_surface"]["status"],
                "circle_sampling": {face: data["substrate"].get("circle_grids", {}).get(face, {}).get("metadata") for face in ("front", "back")},
                "per_wavelength": [], "timing": {}}
    _write_input_job(output / "input-job.json", job)
    spectrum, saved_waves = None, 0
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("GPU appearance render cancelled before CUDA initialization")
        t = time.perf_counter()
        mi, dr = _load_runtime()
        cmf = np.asarray([mi.cie1931_xyz(float(w)) for w in data["wavelength_nm"]], dtype=np.float64)
        mi.set_variant("cuda_ad_rgb")
        manifest["runtime"] = {"python": sys.version, "platform": platform.platform(),
                               "mitsuba": mi.__version__, "drjit": dr.__version__, "variant": mi.variant()}
        try:
            gpu_info = subprocess.run(["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total",
                                       "--format=csv,noheader"], capture_output=True, text=True, timeout=10, check=False)
            manifest["runtime"]["nvidia_smi"] = {"exit_code": gpu_info.returncode, "stdout": gpu_info.stdout.strip(), "stderr": gpu_info.stderr.strip()}
        except (OSError, subprocess.TimeoutExpired) as error:
            manifest["runtime"]["nvidia_smi"] = {"unavailable": str(error)}
        manifest["runtime"]["CUDA_VISIBLE_DEVICES"] = os.environ.get("CUDA_VISIBLE_DEVICES")
        manifest["timing"]["runtime_startup_s"] = time.perf_counter() - t
        with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
            t = time.perf_counter()
            progress.emit("uploading")
            bsdfs, state, solver = _build_gpu_bsdfs(mi, dr, data, precision)
            scene = mi.load_dict(preview_scene_dict(mi, data, bsdfs))
            params = mi.traverse(scene)
            for key in ("area.emitter.radiance.value", "environment.radiance.value"):
                if key not in params:
                    raise RuntimeError("required persistent source parameter missing: " + key)
            dr.sync_thread()
            manifest["timing"]["upload_and_scene_build_s"] = time.perf_counter() - t
            h, w = data["resolution"]["height"], data["resolution"]["width"]
            spectrum = np.lib.format.open_memmap(output / "radiance_spectrum.npy", mode="w+", dtype=np.float32,
                                                 shape=(len(data["wavelength_nm"]), h, w))
            equality = 0.0
            for i, wavelength in enumerate(data["wavelength_nm"]):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("GPU appearance render cancelled at wavelength boundary")
                progress.emit("rendering")
                params["area.emitter.radiance.value"] = dr.opaque(mi.Float, float(data["area_source"]["spectrum_values"][i]))
                params["environment.radiance.value"] = dr.opaque(mi.Float, float(data["environment_values"][i]))
                params.update()
                _reset_stats(dr, state, i)
                seed = data["resolution"]["seed"] + i
                context = _kernel_record(dr)
                start = time.perf_counter()
                image_device = mi.render(scene, spp=data["resolution"]["spp"], seed=seed)
                dr.eval(image_device, state["counters"], state["stats"])
                dr.sync_thread()
                render_time = time.perf_counter() - start
                history = _finish_kernel_record(dr, context)
                image = np.asarray(image_device, dtype=np.float32)
                diagnostics = _read_stats(state)
                wave_record = {"index": i, "wavelength_nm": float(wavelength), "seed": seed,
                               "synchronized_render_s": render_time, "diagnostics": diagnostics,
                               "kernel_history": history}
                manifest["per_wavelength"].append(wave_record)
                if not diagnostics["pass"]:
                    raise PreviewRejected(f"GPU optics diagnostics rejected wavelength index {i}: " +
                                          ", ".join(f"{name}={diagnostics[name]}" for name in FATAL_COUNTERS if diagnostics[name]))
                if image.shape != (h, w, 3) or not np.isfinite(image).all():
                    raise PreviewRejected("GPU produced invalid linear film")
                equality = max(equality, float(np.max(abs(image[..., 0] - image[..., 1]))),
                               float(np.max(abs(image[..., 1] - image[..., 2]))))
                spectrum[i] = image[..., 0]
                saved_waves += 1
                if i == 0:
                    manifest["timing"]["first_call_s"] = render_time
                    manifest["timing"]["cold_jit_observed"] = _cold_jit_observed(history)
                    manifest["timing"]["first_call_label"] = "cold_first_call; cache state recorded, uncached JIT is not assumed"
                    if warm_repeat:
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError("GPU appearance render cancelled before warm repeat")
                        _reset_stats(dr, state, i)
                        context = _kernel_record(dr)
                        start = time.perf_counter()
                        warm = mi.render(scene, spp=data["resolution"]["spp"], seed=seed)
                        dr.eval(warm, state["counters"], state["stats"]); dr.sync_thread()
                        manifest["timing"]["warm_first_wave_s"] = time.perf_counter() - start
                        manifest["warm_kernel_history"] = _finish_kernel_record(dr, context)
                        manifest["warm_diagnostics"] = _read_stats(state)
                        if not manifest["warm_diagnostics"]["pass"]:
                            raise PreviewRejected("warm repeat failed optics diagnostics")
                progress.advance()
                print(json.dumps({"wave": i + 1, "waves": len(data["wavelength_nm"]),
                                  "render_s": render_time, "diagnostics_pass": True}), flush=True)
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("GPU appearance render cancelled after final wavelength")
            spectrum.flush()
            t = time.perf_counter()
            progress.emit("integrating")
            xyz = np.trapezoid(spectrum[..., None] * cmf[:, None, None, :], x=data["wavelength_nm"], axis=0).astype(np.float32)
            rgb = np.einsum("...j,ij->...i", xyz, XYZ_TO_RGB, optimize=True).astype(np.float32)
            if not np.isfinite(xyz).all() or not np.isfinite(rgb).all():
                raise PreviewRejected("nonfinite integrated XYZ/RGB")
            np.save(output / "xyz.npy", xyz); np.save(output / "linear_srgb.npy", rgb)
            write_png(output / "display.png", display_rgb(rgb, exposure))
            manifest["film_rgb_equality_max_abs"] = equality
            manifest["timing"]["integrate_and_write_s"] = time.perf_counter() - t
            manifest["timing"]["all_saved_wave_render_s"] = sum(row["synchronized_render_s"] for row in manifest["per_wavelength"])
            manifest["status"] = "rendered_unqualified"
            if reference:
                manifest["comparison"] = compare_reference(data, output, reference, job)
                manifest["status"] = "rendered_compared_unqualified"
            manifest["outputs"] = {"spectrum": "radiance_spectrum.npy", "xyz": "xyz.npy", "linear_srgb": "linear_srgb.npy",
                                   "display": "display.png", "display_exposure": exposure, "denoised": False}
            manifest["timing"]["whole_walltime_s"] = time.perf_counter() - whole_start
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("GPU appearance render cancelled before completion")
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            progress.emit("complete")
            return manifest
    except Exception as error:
        manifest["status"] = "rejected"
        manifest["error"] = str(error)
        manifest["traceback"] = traceback.format_exc(chain=True)
        if spectrum is not None:
            manifest["partial_spectrum"] = {"file": "radiance_spectrum.npy", "completed_wave_count": saved_waves,
                                            "complete": False, "usable_as_completed_render": False}
        manifest["timing"]["whole_walltime_s"] = time.perf_counter() - whole_start
        # Never leave a viewable completed image after numerical rejection.
        for name in ("display.png", "xyz.npy", "linear_srgb.npy"):
            (output / name).unlink(missing_ok=True)
        (output / "rejected-manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        raise
    finally:
        if spectrum is not None:
            try:
                spectrum.flush()
            finally:
                spectrum._mmap.close()


def probe_cuda():
    """Force a real CUDA device allocation; import availability alone is insufficient."""
    mi, dr = _load_runtime()
    mi.set_variant("cuda_ad_rgb")
    with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
        value = dr.opaque(mi.Float, 1.0)
        dr.eval(value)
        dr.sync_thread()
        if not np.isfinite(np.asarray(value)).all():
            raise RuntimeError("CUDA device returned a nonfinite capability probe")
    return {"available": True, "backend": "gpu_cuda", "variant": mi.variant(),
            "mitsuba": mi.__version__, "drjit": dr.__version__}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--precision", choices=tuple(TOLERANCE), default="float32")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--spp", type=int, help="explicit preview override; original/effective hashes saved")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--exposure", type=float, default=.01)
    parser.add_argument("--no-warm-repeat", action="store_true")
    parser.add_argument("--probe", action="store_true", help="verify CUDA device execution")
    args = parser.parse_args(argv)
    if args.probe:
        try:
            print(json.dumps(probe_cuda(), ensure_ascii=False))
            return 0
        except Exception as error:
            print(json.dumps({"available": False, "backend": "gpu_cuda", "reason": str(error)}, ensure_ascii=False))
            return 2
    if args.job is None or args.output is None:
        parser.error("--job and --output are required for rendering")
    job = json.loads(args.job.read_text(encoding="utf-8"))
    original_hash = json_hash(job)
    original_resolution = deepcopy(job.get("resolution"))
    for key in ("spp", "width", "height"):
        value = getattr(args, key)
        if value is not None:
            job["resolution"][key] = value
    try:
        result = render_preview(job, args.output, precision=args.precision, reference=args.reference,
                                exposure=args.exposure, warm_repeat=not args.no_warm_repeat)
    except Exception as error:
        traceback.print_exc(file=sys.stderr, chain=True)
        print(json.dumps({"status": "rejected", "error": str(error), "output": str(args.output)}), file=sys.stderr)
        return 1
    result["original_input_sha256"] = original_hash
    result["original_resolution"] = original_resolution
    result["resolution_overrides"] = {key: getattr(args, key) for key in ("spp", "width", "height") if getattr(args, key) is not None}
    (args.output / "manifest.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output), "whole_walltime_s": result["timing"]["whole_walltime_s"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
