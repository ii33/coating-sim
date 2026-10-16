"""Dr.Jit batch TMM for CUDA appearance rendering.

This is a direct port of coat_sim.core.film_optics' passive scattering
recurrence and exact critical-layer field limit, not a Mitsuba variant switch.
All input/output arrays are batched; only the fixed layer and polarization
loops execute in Python. Dr.Jit is intentionally an optional, lazy import.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any

import numpy as np


@dataclass(frozen=True)
class TmmBatch:
    name: str
    wavelength_nm: np.ndarray  # [N], float64, nm
    indices: np.ndarray        # [N,L+2], complex128, incident -> exit
    thickness_nm: np.ndarray  # [L], float64, nm
    beta: np.ndarray           # [N], float64, real k_parallel/k_vacuum
    polarization: str = "unpolarized"
    metadata: dict | None = None

    def validate(self) -> None:
        w, n, d, b = self.wavelength_nm, self.indices, self.thickness_nm, self.beta
        if w.ndim != 1 or not w.size or not np.all(np.isfinite(w)) or np.any(w <= 0):
            raise ValueError("wavelength_nm must be positive finite [N]")
        if n.ndim != 2 or n.shape[0] != w.size or n.shape[1] < 2:
            raise ValueError("indices must be [N,L+2], including both ports")
        if not np.all(np.isfinite(n)) or np.any(n.real <= 0) or np.any(n.imag < 0):
            raise ValueError("indices require finite passive n+i*k, n>0 and k>=0")
        if d.shape != (n.shape[1] - 2,) or not np.all(np.isfinite(d)) or np.any(d < 0):
            raise ValueError("thickness_nm must be finite nonnegative [L]")
        if b.shape != w.shape or not np.all(np.isfinite(b)) or np.iscomplexobj(b):
            raise ValueError("beta must be finite real [N]")
        if np.any(n[:, 0].imag != 0) or np.any(np.abs(b) >= n[:, 0].real):
            raise ValueError("incident port must be transparent with strictly positive flux")
        if self.polarization not in ("s", "p", "unpolarized"):
            raise ValueError("polarization must be s, p or unpolarized")


class DrjitTmm:
    """Use explicit cuda/llvm non-AD Float/Complex2f types at one precision.

    No per-ray host loop, host optical arithmetic, LUT, clipping, or balance
    renormalization is present in ``solve_device``. Both regular and q=0
    branches are traced with safe denominators and blended by a device mask.
    This conservative dual-branch probe deliberately prioritizes auditability;
    its timing includes that overhead and does not bound an optimized BSDF.
    """

    def __init__(self, backend: str = "cuda", precision: str = "float64"):
        if backend not in ("cuda", "llvm") or precision not in ("float64", "float32"):
            raise ValueError("expected cuda/llvm and float64/float32")
        self.dr = importlib.import_module("drjit")
        self.module = importlib.import_module("drjit." + backend)
        backend_enum = getattr(self.dr.JitBackend, "CUDA" if backend == "cuda" else "LLVM")
        if not self.dr.has_backend(backend_enum):
            raise RuntimeError(f"Dr.Jit {backend} backend is unavailable")
        self.backend, self.precision = backend, precision
        self.F = getattr(self.module, "Float64" if precision == "float64" else "Float")
        self.C = getattr(self.module, "Complex2f64" if precision == "float64" else "Complex2f")
        self.B = self.dr.mask_t(self.F)
        self.numpy_float = np.float64 if precision == "float64" else np.float32
        # An explicit array backend is evidence of the requested arithmetic;
        # no Mitsuba variant is used to select or advertise the TMM precision.
        if self.dr.backend_v(self.F) != backend_enum:
            raise RuntimeError("requested Dr.Jit array backend does not match")

    def upload(self, batch: TmmBatch) -> dict[str, Any]:
        batch.validate()
        dr, F, C = self.dr, self.F, self.C
        w = F(np.ascontiguousarray(batch.wavelength_nm, dtype=self.numpy_float))
        b = F(np.ascontiguousarray(batch.beta, dtype=self.numpy_float))
        n = tuple(C(F(np.ascontiguousarray(batch.indices[:, j].real, dtype=self.numpy_float)),
                    F(np.ascontiguousarray(batch.indices[:, j].imag, dtype=self.numpy_float)))
                  for j in range(batch.indices.shape[1]))
        d = tuple(F(float(self.numpy_float(value))) for value in batch.thickness_nm)
        dr.make_opaque(w, b, n, d)
        dr.eval(w, b, n, d)
        return {"w": w, "n": n, "d": d, "beta": b,
                "polarization": batch.polarization}

    def _zero(self, z):
        return (z.real == 0) & (z.imag == 0)

    def _safe(self, z):
        return self.dr.select(self._zero(z), self.C(1), z)

    def _interface(self, ni, nj, qi, qj, polarization):
        if polarization == "s":
            denominator = qi + qj
            numerator, transmission = qi - qj, 2 * qi
        else:
            denominator = nj * nj * qi + ni * ni * qj
            numerator = nj * nj * qi - ni * ni * qj
            transmission = 2 * ni * nj * qi
        return numerator / self._safe(denominator), transmission / self._safe(denominator), self._zero(denominator)

    def _flux(self, n, q, polarization):
        return q.real if polarization == "s" else (q * self.dr.conj(n) / n).real

    def _powers(self, r, t, incident_flux, exit_flux, invalid):
        dr = self.dr
        invalid |= ~dr.isfinite(incident_flux) | (incident_flux <= 0)
        safe_incident = dr.select(incident_flux > 0, incident_flux, self.F(1))
        reflected = dr.abs(r) ** 2
        transmitted = dr.abs(t) ** 2 * dr.maximum(0, exit_flux) / safe_incident
        absorption = 1 - reflected - transmitted
        # Match the production residual rule exactly, including in Float32.
        absorption = dr.select(dr.abs(absorption) < 5e-13, self.F(0), absorption)
        invalid |= ~dr.isfinite(reflected) | ~dr.isfinite(transmitted) | ~dr.isfinite(absorption)
        return (reflected, transmitted, absorption, invalid)

    def _regular(self, w, n, d, q, p):
        dr = self.dr
        r, t, invalid = self._interface(n[-2], n[-1], q[-2], q[-1], p)
        for j in range(len(n) - 3, -1, -1):
            phase = dr.exp(self.C(0, 2 * np.pi) * q[j + 1] * d[j] / w)
            ri, ti, bad_interface = self._interface(n[j], n[j + 1], q[j], q[j + 1], p)
            phase2 = phase * phase
            denominator = 1 + ri * r * phase2
            invalid |= bad_interface | self._zero(denominator)
            safe = self._safe(denominator)
            t = ti * phase * t / safe
            r = (ri + r * phase2) / safe
        return self._powers(r, t, self._flux(n[0], q[0], p),
                            self._flux(n[-1], q[-1], p), invalid)

    def _critical(self, w, n, d, q, p):
        dr, C = self.dr, self.C
        y0 = q[0] if p == "s" else n[0] * n[0] / self._safe(q[0])
        exit_zero = self._zero(q[-1])
        if p == "p":
            ye = dr.select(exit_zero, C(0), n[-1] * n[-1] / self._safe(q[-1]))
            field_e = dr.select(exit_zero, C(0), C(1))
            field_h = dr.select(exit_zero, C(1), ye)
        else:
            ye, field_e, field_h = q[-1], C(1), q[-1]
        factor, invalid = C(1), self.B(False)
        for j in range(len(n) - 2, 0, -1):
            length = 2 * np.pi * d[j - 1] / w
            phase = dr.exp(C(0, 1) * q[j] * length)
            diagonal = (1 + phase * phase) / 2
            off = (1 - phase * phase) / 2
            qzero = self._zero(q[j])
            y = q[j] if p == "s" else n[j] * n[j] / self._safe(q[j])
            if p == "s":
                upper = dr.select(qzero, C(0, -1) * length, off / self._safe(y))
                lower = dr.select(qzero, C(0), off * y)
            else:
                upper = dr.select(qzero, C(0), off / self._safe(y))
                lower = dr.select(qzero, C(0, -1) * n[j] * n[j] * length, off * y)
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
        safe = self._safe(denominator)
        r = (y0 * field_e - field_h) / safe
        t = 2 * y0 * factor / safe
        # _critical_one works in tangential E/H amplitudes, whose p admittance
        # differs from the travelling electric-amplitude flux in _regular.
        result = self._powers(r, t, y0.real, ye.real, invalid)
        return (result[0], dr.select(exit_zero, self.F(0), result[1]),
                result[2], result[3])

    def solve_device(self, uploaded: dict[str, Any]):
        dr = self.dr
        if dr.flag(dr.JitFlag.FastMath):
            raise RuntimeError("probe requires explicit JitFlag.FastMath=False around tracing AND evaluation")
        w, n, d, beta = (uploaded[key] for key in ("w", "n", "d", "beta"))
        q = []
        for item in n:
            value = dr.sqrt(item * item - beta * beta)
            flip = (value.imag < 0) | ((value.imag == 0) & (value.real < 0))
            q.append(dr.select(flip, -value, value))
        critical = self.B(False)
        for value in q[1:-1]:
            critical |= self._zero(value)
        polarizations = ("s", "p") if uploaded["polarization"] == "unpolarized" else (uploaded["polarization"],)
        results = []
        for p in polarizations:
            regular = self._regular(w, n, d, q, p)
            limit = self._critical(w, n, d, q, p)
            results.append(tuple(dr.select(critical, c, r) for c, r in zip(limit, regular)))
        values = tuple(sum(row[j] for row in results) / len(results) for j in range(3))
        invalid = results[0][3]
        for row in results[1:]:
            invalid |= row[3]
        invalid |= ~dr.isfinite(values[0]) | ~dr.isfinite(values[1]) | ~dr.isfinite(values[2])
        values = tuple(dr.select(invalid, self.F(np.nan), v) for v in values)
        return (*values, invalid, critical)

    def to_numpy(self, output):
        """Call after explicit eval/sync when used in benchmark regions."""
        return (np.column_stack([np.asarray(v, dtype=np.float64) for v in output[:3]]),
                np.asarray(output[3], dtype=bool), np.asarray(output[4], dtype=bool))

    def evaluate(self, batch: TmmBatch):
        dr = self.dr
        with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
            data = self.upload(batch)
            dr.sync_thread()
            output = self.solve_device(data)
            dr.eval(output)
            dr.sync_thread()
            return self.to_numpy(output)
