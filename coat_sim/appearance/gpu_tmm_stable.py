"""Device-stable TMM used by CUDA appearance rendering.

PROJECT CONVENTIONS: near-q threshold 1e-3*max(1,|n|), small-|delta|
threshold 1e-3, eighth-order sinc polynomial. These are experiment settings,
not certified production tolerances. Both regular/field paths stay on-device.
"""
from __future__ import annotations

from dataclasses import replace
import numpy as np

from .gpu_tmm import DrjitTmm, TmmBatch


NEAR_Q_RELATIVE = 1e-3
SMALL_DELTA = 1e-3


class StableDrjitTmm(DrjitTmm):
    """Exact zero-thickness elimination plus a device near-critical field path."""

    def upload(self, batch: TmmBatch):
        batch.validate()
        # Only exact original thickness zeros are physically absent layers.
        # Never treat phase ~= 1 or a positive thickness rounded to zero as this
        # simplification. Shared stack specialization has no per-ray host loop.
        keep = batch.thickness_nm != 0
        columns = np.r_[0, np.flatnonzero(keep) + 1, batch.indices.shape[1] - 1]
        effective = replace(batch, indices=batch.indices[:, columns],
                            thickness_nm=batch.thickness_nm[keep])
        data = super().upload(effective)
        data["removed_exact_zero_layers"] = int(np.count_nonzero(~keep))
        data["original_layer_count"] = int(batch.thickness_nm.size)
        return data

    def _field_candidate(self, w, n, d, q, p):
        dr, C = self.dr, self.C
        U = dr.uint32_array_t(self.F)
        y0 = q[0] if p == "s" else n[0] * n[0] / self._safe(q[0])
        exit_zero = self._zero(q[-1])
        if p == "p":
            ye = dr.select(exit_zero, C(0), n[-1] * n[-1] / self._safe(q[-1]))
            field_e, field_h = dr.select(exit_zero, C(0), C(1)), dr.select(exit_zero, C(1), ye)
        else:
            ye, field_e, field_h = q[-1], C(1), q[-1]
        factor, invalid, series_count = C(1), self.B(False), U(0)
        for j in range(len(n) - 2, 0, -1):
            length = 2 * np.pi * d[j - 1] / w
            delta = q[j] * length
            phase = dr.exp(C(0, 1) * delta)  # Passive, never a growing exponential.
            small = dr.abs(delta) <= SMALL_DELTA
            series_count = series_count + dr.select(small, U(1), U(0))
            # Guard BEFORE sin/cos: a masked-away cos(large imaginary delta)
            # could overflow even when its final blend is unused.
            guarded = dr.select(small, delta, C(0))
            delta2 = guarded * guarded
            sinc = 1 + delta2 * (-1 / 6 + delta2 * (1 / 120 + delta2 * (-1 / 5040 + delta2 / 362880)))
            small_off_over_q = C(0, -1) * phase * length * sinc
            large_off = (1 - phase * phase) / 2
            large_off_over_q = large_off / self._safe(q[j])
            off_over_q = dr.select(small, small_off_over_q, large_off_over_q)
            diagonal = dr.select(small, phase * dr.cos(guarded), (1 + phase * phase) / 2)
            q2 = q[j] * q[j]
            if p == "s":
                upper, lower = off_over_q, off_over_q * q2
            else:
                n2 = n[j] * n[j]
                upper, lower = off_over_q * q2 / n2, off_over_q * n2
            new_e = diagonal * field_e + upper * field_h
            new_h = lower * field_e + diagonal * field_h
            normalization = dr.maximum(dr.abs(new_e), dr.abs(new_h))
            good = dr.isfinite(normalization) & (normalization > 0)
            invalid |= ~good
            normalization = dr.select(good, normalization, self.F(1))
            field_e, field_h = new_e / normalization, new_h / normalization
            factor = factor * phase / normalization
        denominator = y0 * field_e + field_h
        invalid |= self._zero(denominator) | self._zero(q[0])
        r = (y0 * field_e - field_h) / self._safe(denominator)
        t = 2 * y0 * factor / self._safe(denominator)
        values = self._powers(r, t, y0.real, ye.real, invalid)
        return (*values, series_count)

    def solve_device(self, uploaded):
        dr = self.dr
        if dr.flag(dr.JitFlag.FastMath):
            raise RuntimeError("stable candidate requires FastMath=False during trace/eval")
        w, n, d, beta = (uploaded[key] for key in ("w", "n", "d", "beta"))
        q = []
        for item in n:
            value = dr.sqrt(item * item - beta * beta)
            flip = (value.imag < 0) | ((value.imag == 0) & (value.real < 0))
            q.append(dr.select(flip, -value, value))
        near, exact = self.B(False), self.B(False)
        for item, value in zip(n[1:-1], q[1:-1]):
            near |= dr.abs(value) <= NEAR_Q_RELATIVE * dr.maximum(1, dr.abs(item))
            exact |= self._zero(value)
        polarizations = ("s", "p") if uploaded["polarization"] == "unpolarized" else (uploaded["polarization"],)
        results, series_count = [], dr.uint32_array_t(self.F)(0)
        for p in polarizations:
            regular = self._regular(w, n, d, q, p)
            field = self._field_candidate(w, n, d, q, p)
            results.append(tuple(dr.select(near, f, r) for f, r in zip(field[:4], regular)))
            series_count = dr.select(near, field[4], dr.uint32_array_t(self.F)(0))
        values = tuple(sum(row[j] for row in results) / len(results) for j in range(3))
        invalid = results[0][3]
        for row in results[1:]:
            invalid |= row[3]
        invalid |= ~dr.isfinite(values[0]) | ~dr.isfinite(values[1]) | ~dr.isfinite(values[2])
        values = tuple(dr.select(invalid, self.F(np.nan), value) for value in values)
        # First five entries preserve the original probe's API. Extra entries
        # expose near-critical selection and small-delta layer-lane counts.
        return (*values, invalid, exact, near, series_count)
