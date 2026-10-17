"""Frozen, vectorized CPU evaluation of coherent thin-film candidates.

This module keeps the passive ``n + i*k`` convention and scalar critical-layer
limit from :mod:`film_optics`, while precomputing all wavelength-only quantities
once for repeated thickness sweeps.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

import numpy as np

from . import film_optics as _optics


def _readonly(value, dtype):
    """Return an independent array backed by immutable bytes."""
    result = np.array(value, dtype=dtype, copy=True, order="C")
    return np.frombuffer(result.tobytes(order="C"), dtype=result.dtype).reshape(result.shape)


def _is_cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if isinstance(cancel, (bool, np.bool_)):
        return bool(cancel)
    if callable(cancel):
        return bool(cancel())
    for name in ("is_set", "is_cancelled", "cancelled"):
        value = getattr(cancel, name, None)
        if callable(value):
            value = value()
        if value is not None:
            return bool(value)
    raise TypeError("cancel must be a callable, boolean, or cancellation token")


def _check_cancelled(cancel):
    if _is_cancelled(cancel):
        raise CancelledError("film-optics batch evaluation was cancelled")


@dataclass(frozen=True, slots=True)
class FrozenCoherentStack:
    """Precomputed wavelength-side data for many coherent thickness candidates.

    Arrays use wavelength-major layouts: ``waves`` and ``transverse`` have
    shape ``(W,)``; ``indices`` and ``q`` have shape ``(W, M)``; each interface
    coefficient in ``interfaces`` has shape ``(W, M-1)`` in incident-to-exit
    boundary order. Arrays are independent, bytes-backed read-only
    float64/complex128 copies; callers cannot restore write access.
    ``fluxes[pol]`` contains incident and exit port flux arrays, each ``(W,)``.
    """

    waves: np.ndarray
    indices: np.ndarray
    transverse: np.ndarray
    q: np.ndarray
    critical: np.ndarray
    polarization: str
    interfaces: Mapping[str, tuple[np.ndarray, np.ndarray]]
    fluxes: Mapping[str, tuple[np.ndarray, np.ndarray]]

    @classmethod
    def prepare(cls, wavelength_nm, indices, angle_deg=0,
                polarization="unpolarized", beta=None) -> "FrozenCoherentStack":
        """Validate and freeze the stack's wavelength-dependent quantities.

        ``indices`` follows :func:`film_optics.coherent_rta`: constants may be
        one-dimensional by medium or dispersive with shape ``(W, M)``. The
        incident port must be transparent and propagating; internal and exit
        media may absorb or be evanescent.
        """
        waves, _scalar = _optics._waves(wavelength_nm)
        n = _optics._indices(indices, waves.size)
        if n.shape[1] < 2:
            raise ValueError("indices must contain incident and exit ports")
        transverse = _optics._beta(beta, n[:, 0], angle_deg)
        polarizations = _optics._polarizations(polarization)
        q = _optics._longitudinal_many(n, transverse)
        critical = (np.any(q[:, 1:-1] == 0, axis=1)
                    if n.shape[1] > 2 else np.zeros(waves.size, dtype=bool))
        regular = ~critical

        interfaces = {}
        fluxes = {}
        for pol in polarizations:
            reflectance = np.empty((waves.size, n.shape[1] - 1), dtype=np.complex128)
            transmittance = np.empty_like(reflectance)
            # Critical wavelengths use the normalized analytic field limit in
            # evaluate(); their ordinary interface coefficients can be 0/0.
            reflectance[:] = 0
            transmittance[:] = 0
            for boundary in range(n.shape[1] - 1):
                if np.any(regular):
                    ri, ti = _optics._interface_many(
                        n[regular, boundary], n[regular, boundary + 1],
                        q[regular, boundary], q[regular, boundary + 1], pol)
                    reflectance[regular, boundary] = ri
                    transmittance[regular, boundary] = ti
            incident_flux = _optics._flux_many(n[:, 0], q[:, 0], pol)
            exit_flux = _optics._flux_many(n[:, -1], q[:, -1], pol)
            interfaces[pol] = (_readonly(reflectance, np.complex128),
                               _readonly(transmittance, np.complex128))
            fluxes[pol] = (_readonly(incident_flux, np.float64),
                           _readonly(exit_flux, np.float64))

        return cls(
            waves=_readonly(waves, np.float64),
            indices=_readonly(n, np.complex128),
            transverse=_readonly(transverse, np.float64),
            q=_readonly(q, np.complex128),
            critical=_readonly(critical, np.bool_),
            polarization=polarization,
            interfaces=MappingProxyType(interfaces),
            fluxes=MappingProxyType(fluxes),
        )

    @property
    def beta(self) -> np.ndarray:
        """Alias for ``transverse`` (real transverse wavevector / k0)."""
        return self.transverse

    @property
    def polarizations(self) -> tuple[str, ...]:
        return tuple(self.interfaces)

    @property
    def internal_layer_count(self) -> int:
        return self.indices.shape[1] - 2

    def wavelength_slice(self, start: int, stop: int) -> "FrozenCoherentStack":
        """Share immutable prepared arrays for a contiguous wavelength interval.

        No refreezing, optical recomputation or copy of resident stack arrays.
        Each wavelength retains its complete layer/polarization recurrence;
        callers concatenate spectra before any wavelength-wide reduction.
        """
        if (any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer))
                for v in (start, stop)) or not 0 <= start < stop <= self.waves.size):
            raise ValueError("wavelength slice requires 0 <= integer start < stop <= wavelength count")
        selection = slice(start, stop)
        return FrozenCoherentStack(
            waves=self.waves[selection], indices=self.indices[selection],
            transverse=self.transverse[selection], q=self.q[selection],
            critical=self.critical[selection], polarization=self.polarization,
            interfaces=MappingProxyType({p: (pair[0][selection], pair[1][selection])
                                         for p, pair in self.interfaces.items()}),
            fluxes=MappingProxyType({p: (pair[0][selection], pair[1][selection])
                                     for p, pair in self.fluxes.items()}))

    def evaluate(self, thickness_nm, *, chunk_bytes=64 * 1024**2, cancel=None):
        """Evaluate candidates with thickness shape ``(C, M-2)``.

        The result has shape ``(C, W, 3)`` with the last axis ordered as
        reflectance, transmittance, and absorption. Candidate and wavelength
        axes are vectorized together. ``chunk_bytes`` sets an empirical scratch
        estimate, not a hard memory guarantee; it excludes the required full
        output and frozen wavelength-side arrays. A single candidate/wavelength
        point is the minimum tile. ``cancel`` may be a bool, zero-argument
        predicate, or an object exposing ``is_set()``, ``is_cancelled`` or
        ``cancelled``. Cancellation raises :class:`concurrent.futures.CancelledError`
        between tiles and is checked in long layer/critical-point loops and
        immediately before return.
        """
        thickness = np.asarray(thickness_nm, dtype=np.float64)
        layer_count = self.internal_layer_count
        if (thickness.ndim != 2 or thickness.shape[1] != layer_count or
                thickness.shape[0] == 0 or not np.all(np.isfinite(thickness)) or
                np.any(thickness < 0)):
            raise ValueError(
                "thickness_nm must be finite nonnegative [candidate, internal layer] data")
        if (isinstance(chunk_bytes, (bool, np.bool_)) or
                not isinstance(chunk_bytes, (int, np.integer)) or chunk_bytes <= 0):
            raise ValueError("chunk_bytes must be a positive integer")

        _check_cancelled(cancel)
        candidate_count = thickness.shape[0]
        wavelength_count = self.waves.size
        output = np.empty((candidate_count, wavelength_count, 3), dtype=np.float64)

        # The recurrence creates several complex candidate-by-wave temporaries.
        # 256 bytes per tile point is an empirical estimate, not a hard bound.
        max_tile_points = max(1, int(chunk_bytes) // 256)
        candidate_tile = min(
            candidate_count, max_tile_points,
            max(1, int(math.sqrt(max_tile_points * candidate_count / wavelength_count))))
        wavelength_tile = min(
            wavelength_count, max(1, max_tile_points // candidate_tile))

        for start in range(0, candidate_count, candidate_tile):
            stop = min(candidate_count, start + candidate_tile)
            candidate_block = thickness[start:stop]
            for wave_start in range(0, wavelength_count, wavelength_tile):
                _check_cancelled(cancel)
                wave_stop = min(wavelength_count, wave_start + wavelength_tile)
                wave_indices = np.arange(wave_start, wave_stop)
                regular_tile = wave_indices[~self.critical[wave_indices]]
                critical_tile = wave_indices[self.critical[wave_indices]]
                accumulated = np.zeros(
                    (stop - start, wave_stop - wave_start, 3), dtype=np.float64)

                for pol in self.polarizations:
                    values = np.empty_like(accumulated)
                    if regular_tile.size:
                        local_regular = regular_tile - wave_start
                        values[:, local_regular, :] = self._evaluate_regular(
                            candidate_block, regular_tile, pol, cancel=cancel)
                    if critical_tile.size:
                        for local_candidate, row in enumerate(candidate_block):
                            for wave_index in critical_tile:
                                _check_cancelled(cancel)
                                values[local_candidate, wave_index - wave_start] = (
                                    _optics._critical_one(
                                        self.waves[wave_index], self.indices[wave_index], row,
                                        self.q[wave_index], pol))
                    accumulated += values
                output[start:stop, wave_start:wave_stop] = (
                    accumulated / len(self.polarizations))
        _check_cancelled(cancel)
        return output

    def _evaluate_regular(self, thickness, wave_indices, polarization, *, cancel=None):
        """Run the scalar recurrence over candidate x regular-wavelength axes."""
        waves = self.waves[wave_indices]
        q = self.q[wave_indices]
        n = self.indices[wave_indices]
        ri_all, ti_all = self.interfaces[polarization]
        ri_all, ti_all = ri_all[wave_indices], ti_all[wave_indices]
        incident_flux, exit_flux = self.fluxes[polarization]
        incident_flux = incident_flux[wave_indices]
        exit_flux = exit_flux[wave_indices]

        with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
            r = np.broadcast_to(ri_all[:, -1], (thickness.shape[0], waves.size)).copy()
            t = np.broadcast_to(ti_all[:, -1], (thickness.shape[0], waves.size)).copy()
            for boundary in range(n.shape[1] - 3, -1, -1):
                if boundary % 16 == 0:
                    _check_cancelled(cancel)
                phase = np.exp(2j * np.pi * q[:, boundary + 1][None, :] *
                               thickness[:, boundary, None] / waves[None, :])
                ri = ri_all[:, boundary][None, :]
                ti = ti_all[:, boundary][None, :]
                phase_squared = phase * phase
                denominator = 1 + ri * r * phase_squared
                if np.any(denominator == 0):
                    raise ValueError("singular coherent stack")
                t = ti * phase * t / denominator
                r = (ri + r * phase_squared) / denominator
            reflected = np.abs(r) ** 2
            transmitted = (np.abs(t) ** 2 * np.maximum(0.0, exit_flux)[None, :] /
                           incident_flux[None, :])
            absorption = 1.0 - reflected - transmitted
        absorption[np.abs(absorption) < 5e-13] = 0.0
        if not (np.all(np.isfinite(reflected)) and np.all(np.isfinite(transmitted)) and
                np.all(np.isfinite(absorption))):
            raise ValueError("nonfinite optical result")
        return np.stack((reflected, transmitted, absorption), axis=-1)


__all__ = ["FrozenCoherentStack"]
