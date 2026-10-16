"""Independent, high-precision thin-film boundary oracle.

This is a reconstructed validation tool, not production TMM.  It uses
mpmath's analytic tangential-field propagation and never calls the project's
scattering recurrence or the Dr.Jit candidate.  Input Python/NumPy floats
are converted exactly, so binary64 ``nextafter`` neighbors stay distinct.
"""
from __future__ import annotations

try:
    import mpmath as mp
except ImportError:  # A verified JSON fixture permits offline GPU use without it.
    mp = None
import numpy as np

from tools.validation.drjit_coherent_tmm import TmmBatch


ORACLE_DECIMAL_DIGITS = 100


def _mp_complex(value: complex) -> mp.mpc:
    """Preserve the exact binary64 components; avoid string rounding."""
    return mp.mpc(mp.mpf(float(value.real)), mp.mpf(float(value.imag)))


def _longitudinal_index(index: mp.mpc, beta: mp.mpf) -> mp.mpc:
    q = mp.sqrt(index * index - beta * beta)
    if mp.im(q) < 0 or (mp.im(q) == 0 and mp.re(q) < 0):
        q = -q
    return q


def _polarized_row(wavelength_nm: float, indices: np.ndarray,
                   thickness_nm: np.ndarray, beta: float,
                   polarization: str) -> tuple[mp.mpf, mp.mpf, mp.mpf]:
    wavelength = mp.mpf(float(wavelength_nm))
    parallel = mp.mpf(float(beta))
    n = [_mp_complex(value) for value in indices]
    q = [_longitudinal_index(value, parallel) for value in n]
    if polarization == "s":
        y0, ye = q[0], q[-1]
    else:
        y0 = n[0] ** 2 / q[0]
        ye = n[-1] ** 2 / q[-1] if q[-1] else None

    # At a p-polarized exit branch point, tangential E of the outgoing wave
    # vanishes while H remains finite.  The limiting exit basis is (E,H)=(0,1).
    exit_zero = q[-1] == 0
    e, h = (mp.mpc(0), mp.mpc(1)) if polarization == "p" and exit_zero else (
        mp.mpc(1), mp.mpc(0) if exit_zero else ye)

    # Propagate tangential (E,H) from exit to entrance.  The analytic sinc
    # makes q=0 a regular limit.  No candidate threshold, truncated series,
    # or interface-scattering recurrence appears here.
    for j in range(len(n) - 2, 0, -1):
        length = 2 * mp.pi * mp.mpf(float(thickness_nm[j - 1])) / wavelength
        delta = q[j] * length
        off_over_q = -1j * length * (mp.sin(delta) / delta if delta else mp.mpf(1))
        diagonal = mp.cos(delta)
        if polarization == "s":
            upper, lower = off_over_q, off_over_q * q[j] ** 2
        else:
            upper = off_over_q * q[j] ** 2 / n[j] ** 2
            lower = off_over_q * n[j] ** 2
        e, h = diagonal * e + upper * h, lower * e + diagonal * h

    denominator = y0 * e + h
    reflection = (y0 * e - h) / denominator
    reflected = abs(reflection) ** 2
    if exit_zero:
        transmitted = mp.mpf(0)
    else:
        transmission = 2 * y0 / denominator
        transmitted = abs(transmission) ** 2 * mp.re(ye) / mp.re(y0)
    absorption = 1 - reflected - transmitted
    return reflected, transmitted, absorption


def oracle_rta(batch: TmmBatch, *, dps: int = ORACLE_DECIMAL_DIGITS) -> np.ndarray:
    """Return unclipped [N,3] R/T/A for s, p or unpolarized input.

    ``dps`` must be at least 100 for proof use.  The calculation is scalar and
    deliberately unsuitable as a performance reference.
    """
    if mp is None:
        raise RuntimeError("mpmath is unavailable; provide a verified --oracle-fixture")
    batch.validate()
    if dps < ORACLE_DECIMAL_DIGITS:
        raise ValueError("boundary oracle requires at least 100 decimal digits")
    rows = []
    with mp.workdps(dps):
        for wavelength, indices, beta in zip(batch.wavelength_nm, batch.indices, batch.beta):
            if batch.polarization == "unpolarized":
                s = _polarized_row(wavelength, indices, batch.thickness_nm, beta, "s")
                p = _polarized_row(wavelength, indices, batch.thickness_nm, beta, "p")
                rows.append(tuple((a + b) / 2 for a, b in zip(s, p)))
            else:
                rows.append(_polarized_row(wavelength, indices, batch.thickness_nm,
                                           beta, batch.polarization))
    return np.asarray(rows, dtype=np.float64)
