"""Passive isotropic thin-film optics, with explicitly defined propagation order.

Complex indices use n + i*k and time dependence exp(-i*omega*t). All lengths
are nm. Each coherent stack is ordered from the incident port to the exit
port. R/T are normalized Poynting fluxes; T into an absorbing half-space is
the flux entering that half-space, and A is absorption in the finite layers.
No optical constants, unknown k, extrapolation, or source spectra are inferred.
"""
from __future__ import annotations

import math
import numpy as np


def _waves(value):
    array = np.asarray(value, dtype=float)
    scalar = array.ndim == 0
    if scalar:
        array = array.reshape(1)
    if array.ndim != 1 or array.size == 0 or not np.all(np.isfinite(array)) or np.any(array <= 0):
        raise ValueError("wavelength_nm must be positive finite scalar or 1D array")
    return array, scalar


def _indices(value, count, name="indices"):
    array = np.asarray(value, dtype=complex)
    if array.ndim == 0:
        array = array.reshape(1)
    if array.ndim == 1:
        array = np.broadcast_to(array, (count, array.size))
    if array.ndim != 2 or array.shape[0] != count:
        raise ValueError(f"{name} must be 1D constants or 2D [wavelength, medium]")
    if not np.all(np.isfinite(array)) or np.any(array.real <= 0) or np.any(array.imag < 0):
        raise ValueError(f"{name} requires finite passive indices with n > 0 and k >= 0")
    return array


def _thickness(value, count):
    array = np.asarray(value, dtype=float)
    if array.ndim != 1 or array.size != count or not np.all(np.isfinite(array)) or np.any(array < 0):
        raise ValueError("thickness_nm must contain one finite nonnegative thickness per internal layer")
    return array


def _beta(value, n0, angle_deg):
    if value is None:
        angle = float(angle_deg)
        if not math.isfinite(angle) or abs(angle) >= 90:
            raise ValueError("angle_deg must be finite and strictly between -90 and 90")
        result = n0.real * math.sin(math.radians(angle))
    else:
        raw = np.asarray(value)
        if np.iscomplexobj(raw) and np.any(raw.imag != 0):
            raise ValueError("beta must be real transverse wavevector divided by vacuum wavevector")
        result = np.asarray(raw.real, dtype=float)
        if result.ndim == 0:
            result = np.full(n0.size, result.item())
        if result.shape != n0.shape or not np.all(np.isfinite(result)):
            raise ValueError("beta must be finite scalar or one real value per wavelength")
    if np.any(n0.imag != 0) or np.any(np.abs(result) >= n0.real):
        raise ValueError("incident port must be nonabsorbing with strictly positive incident flux")
    return result


def _longitudinal(n, beta):
    q = np.sqrt(n * n - beta * beta + 0j)
    if q.imag < 0 or (q.imag == 0 and q.real < 0):
        q = -q
    return q


def _interface(ni, nj, qi, qj, polarization):
    if polarization == "s":
        denominator = qi + qj
        if denominator == 0:
            raise ValueError("degenerate zero-flux interface")
        return (qi - qj) / denominator, 2 * qi / denominator
    denominator = nj * nj * qi + ni * ni * qj
    if denominator == 0:
        raise ValueError("degenerate zero-flux interface")
    return (nj * nj * qi - ni * ni * qj) / denominator, 2 * ni * nj * qi / denominator


def _flux(n, q, polarization):
    return float(q.real if polarization == "s" else (q * n.conjugate() / n).real)


def _critical_one(wave, n, d, q, polarization):
    """Analytic q=0 layer limit using normalized, decaying field propagation."""
    y0 = q[0] if polarization == "s" else n[0] ** 2 / q[0]
    exit_evanescent = q[-1] == 0
    if exit_evanescent and polarization == "p":
        field_e, field_h = 0j, 1 + 0j
        ye = 0j
    else:
        ye = q[-1] if polarization == "s" else n[-1] ** 2 / q[-1]
        field_e, field_h = 1 + 0j, ye
    factor = 1 + 0j
    for index in range(len(n) - 2, 0, -1):
        length = 2 * math.pi * d[index - 1] / wave
        phase = np.exp(1j * q[index] * length)
        diagonal = (1 + phase * phase) / 2
        if q[index] == 0:
            upper = -1j * length if polarization == "s" else 0j
            lower = 0j if polarization == "s" else -1j * n[index] ** 2 * length
        else:
            y = q[index] if polarization == "s" else n[index] ** 2 / q[index]
            off_diagonal = (1 - phase * phase) / 2
            upper, lower = off_diagonal / y, off_diagonal * y
        field_e, field_h = (diagonal * field_e + upper * field_h,
                            lower * field_e + diagonal * field_h)
        normalization = max(abs(field_e), abs(field_h))
        if normalization == 0 or not math.isfinite(normalization):
            raise ValueError("singular critical-layer field propagation")
        field_e, field_h = field_e / normalization, field_h / normalization
        factor = factor * phase / normalization
    denominator = y0 * field_e + field_h
    if denominator == 0:
        raise ValueError("singular critical-layer stack")
    r = (y0 * field_e - field_h) / denominator
    t = 2 * y0 * factor / denominator
    reflected = float(abs(r) ** 2)
    transmitted = 0.0 if exit_evanescent else float(abs(t) ** 2 * max(0.0, ye.real) / y0.real)
    absorption = 1 - reflected - transmitted
    if abs(absorption) < 5e-13:
        absorption = 0.0
    return reflected, transmitted, float(absorption)


def _coherent_one(wave, n, d, beta, polarization):
    q = [_longitudinal(item, beta) for item in n]
    if any(item == 0 for item in q[1:-1]):
        return _critical_one(wave, n, d, q, polarization)
    r, t = _interface(n[-2], n[-1], q[-2], q[-1], polarization)
    for index in range(len(n) - 3, -1, -1):
        # Passive branch: exp(i delta) only decays. Underflow to zero is valid;
        # unlike transfer matrices this never forms exp(-i delta).
        phase = np.exp(2j * math.pi * q[index + 1] * d[index] / wave)
        ri, ti = _interface(n[index], n[index + 1], q[index], q[index + 1], polarization)
        denominator = 1 + ri * r * phase * phase
        if denominator == 0:
            raise ValueError("singular coherent stack")
        t = ti * phase * t / denominator
        r = (ri + r * phase * phase) / denominator
    incident_flux = _flux(n[0], q[0], polarization)
    exit_flux = _flux(n[-1], q[-1], polarization)
    reflected = float(abs(r) ** 2)
    transmitted = float(abs(t) ** 2 * max(0.0, exit_flux) / incident_flux)
    absorption = 1.0 - reflected - transmitted
    # Only remove floating-point residuals; never renormalize real absorption.
    if abs(absorption) < 5e-13:
        absorption = 0.0
    if not all(math.isfinite(v) for v in (reflected, transmitted, absorption)):
        raise ValueError("nonfinite optical result")
    return reflected, transmitted, float(absorption)


def _longitudinal_many(n, beta):
    """Vectorized passive longitudinal wavevectors, shape [wavelength, medium]."""
    q = np.sqrt(n * n - beta[:, None] * beta[:, None] + 0j)
    reverse = (q.imag < 0) | ((q.imag == 0) & (q.real < 0))
    q[reverse] *= -1
    return q


def _interface_many(ni, nj, qi, qj, polarization):
    if polarization == "s":
        denominator = qi + qj
        if np.any(denominator == 0):
            raise ValueError("degenerate zero-flux interface")
        return (qi - qj) / denominator, 2 * qi / denominator
    denominator = nj * nj * qi + ni * ni * qj
    if np.any(denominator == 0):
        raise ValueError("degenerate zero-flux interface")
    return ((nj * nj * qi - ni * ni * qj) / denominator,
            2 * ni * nj * qi / denominator)


def _flux_many(n, q, polarization):
    return q.real if polarization == "s" else (q * n.conjugate() / n).real


def _coherent_regular_many(waves, n, d, beta, polarization, q):
    """Evaluate regular rows in a wavelength batch; exact q=0 layers use scalar limit."""
    with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
        r, t = _interface_many(n[:, -2], n[:, -1], q[:, -2], q[:, -1], polarization)
        for index in range(n.shape[1] - 3, -1, -1):
            phase = np.exp(2j * math.pi * q[:, index + 1] * d[index] / waves)
            ri, ti = _interface_many(n[:, index], n[:, index + 1],
                                     q[:, index], q[:, index + 1], polarization)
            phase_squared = phase * phase
            denominator = 1 + ri * r * phase_squared
            if np.any(denominator == 0):
                raise ValueError("singular coherent stack")
            t = ti * phase * t / denominator
            r = (ri + r * phase_squared) / denominator
        incident_flux = _flux_many(n[:, 0], q[:, 0], polarization)
        exit_flux = _flux_many(n[:, -1], q[:, -1], polarization)
        reflected = np.abs(r) ** 2
        transmitted = np.abs(t) ** 2 * np.maximum(0.0, exit_flux) / incident_flux
        absorption = 1.0 - reflected - transmitted
    absorption[np.abs(absorption) < 5e-13] = 0.0
    if not (np.all(np.isfinite(reflected)) and np.all(np.isfinite(transmitted)) and
            np.all(np.isfinite(absorption))):
        raise ValueError("nonfinite optical result")
    return np.column_stack((reflected, transmitted, absorption))


def _coherent_many(waves, n, d, beta, polarization, q=None):
    """Wavelength-batched coherent stack solve with the exact critical-layer fallback."""
    if q is None:
        q = _longitudinal_many(n, beta)
    critical = np.any(q[:, 1:-1] == 0, axis=1) if n.shape[1] > 2 else np.zeros(waves.size, bool)
    if not np.any(critical):
        return _coherent_regular_many(waves, n, d, beta, polarization, q)
    values = np.empty((waves.size, 3), dtype=float)
    regular = ~critical
    if np.any(regular):
        values[regular] = _coherent_regular_many(
            waves[regular], n[regular], d, beta[regular], polarization, q[regular])
    for index in np.flatnonzero(critical):
        values[index] = _coherent_one(waves[index], n[index], d, beta[index], polarization)
    return values


def _polarizations(value):
    if value not in ("s", "p", "unpolarized"):
        raise ValueError("polarization must be s, p or unpolarized")
    return ("s", "p") if value == "unpolarized" else (value,)


def _result(values, waves, scalar, **information):
    array = np.asarray(values, dtype=float)
    result = {key: float(array[0, i]) if scalar else array[:, i].tolist()
              for i, key in enumerate(("R", "T", "A"))}
    result["wavelength_nm"] = float(waves[0]) if scalar else waves.tolist()
    result.update(information)
    return result


def coherent_rta(wavelength_nm, indices, thickness_nm, angle_deg=0,
                 polarization="unpolarized", beta=None):
    """Calculate intrinsic power fractions for a coherent planar stack.

    indices includes both ports (incident -> exit), as 1D constant values or
    2D [wavelength, medium]. thickness_nm is 1D for the internal layers only.
    beta, if supplied, overrides angle_deg and fixes the *real* transverse
    wavevector / vacuum wavevector, including during reverse block evaluation.
    Spectra return Python lists; scalar wavelengths return Python floats.
    Supports passive isotropic n+i*k, propagating real incident media, and
    absorbing or evanescent exits. Gain and absorbing incident ports are rejected.
    """
    waves, scalar = _waves(wavelength_nm)
    n = _indices(indices, waves.size)
    if n.shape[1] < 2:
        raise ValueError("indices must contain incident and exit ports")
    d = _thickness(thickness_nm, n.shape[1] - 2)
    transverse = _beta(beta, n[:, 0], angle_deg)
    polarizations = _polarizations(polarization)
    if scalar:
        # Avoid allocating per-layer one-element arrays for ray-hit queries.
        # The same stable recurrence already handles critical batch rows.
        polarized = [_coherent_one(waves[0], n[0], d, transverse[0], p)
                     for p in polarizations]
        values = np.mean(polarized, axis=0)[None, :]
    else:
        q = _longitudinal_many(n, transverse)
        polarized = [_coherent_many(waves, n, d, transverse, p, q) for p in polarizations]
        values = np.mean(polarized, axis=0)
    return _result(values, waves, scalar, model="coherent", polarization=polarization,
                   transmission_definition="flux entering exit half-space",
                   absorption_definition="finite coherent layers only")


def _port(value, count, name):
    raw = np.asarray(value, dtype=complex)
    if raw.ndim == 0:
        raw = np.full(count, raw.item(), dtype=complex)
    if raw.shape != (count,):
        raise ValueError(f"{name} must be scalar or one index per wavelength")
    return _indices(raw[:, None], count, name)[:, 0]


def incoherent_plate_rta(wavelength_nm, incident_index, substrate_index, exit_index,
                         front_indices=(), front_thickness_nm=(), back_indices=(),
                         back_thickness_nm=(), substrate_thickness_nm=0,
                         angle_deg=0, polarization="unpolarized", beta=None):
    """Two coherent coatings separated by ONE passive incoherent substrate.

    front_indices order: incident -> substrate. back_indices order: substrate
    -> exit. They contain finite coating layers only, excluding all ports.
    The forward and reverse front-block fluxes are combined with the back
    block's fluxes, summing infinitely many substrate round trips. The same
    real beta is used throughout. Substrate absorption uses a one-pass intensity
    survival factor exp(-4*pi*Im(q)*d/lambda). Reverse block R/T are normalized
    travelling-wave fluxes; their 1-R-T is NOT used as local absorbed power for
    an absorbing incident port. Exterior ports remain transparent/propagating.
    substrate_thickness_nm is validated and recorded but cannot create fringes
    in this incoherent model; absorption still depends on thickness. A coherent plate can instead be built
    by including the substrate as a finite layer in coherent_rta.
    """
    waves, scalar = _waves(wavelength_nm)
    count = waves.size
    n0 = _port(incident_index, count, "incident_index")
    ns = _port(substrate_index, count, "substrate_index")
    ne = _port(exit_index, count, "exit_index")
    if np.any(ne.imag != 0):
        raise ValueError("incoherent plate exit port must be transparent")
    dsubstrate = float(substrate_thickness_nm)
    if not math.isfinite(dsubstrate) or dsubstrate < 0:
        raise ValueError("substrate_thickness_nm must be finite and nonnegative")
    front = _indices(front_indices, count, "front_indices")
    back = _indices(back_indices, count, "back_indices")
    df = _thickness(front_thickness_nm, front.shape[1])
    db = _thickness(back_thickness_nm, back.shape[1])
    transverse = _beta(beta, n0, angle_deg)
    if np.any(np.abs(transverse) >= ns.real) or np.any(np.abs(transverse) >= ne.real):
        raise ValueError("incoherent plate requires propagating substrate and exit ports; use coherent model for evanescent coupling")
    polarizations = _polarizations(polarization)
    nf = np.column_stack((n0, front, ns))
    nb = np.column_stack((ns, back, ne))
    qf = _longitudinal_many(nf, transverse)
    qb = _longitudinal_many(nb, transverse)
    qs = _longitudinal_many(ns[:, None], transverse)[:, 0]
    survival = np.exp(-4 * math.pi * qs.imag * dsubstrate / waves)
    polarized = []
    for p in polarizations:
        rf, tf, _ = _coherent_many(waves, nf, df, transverse, p, qf).T
        rr, tr, _ = _coherent_many(waves, nf[:, ::-1], df[::-1], transverse, p,
                                   qf[:, ::-1]).T
        rb, tb, _ = _coherent_many(waves, nb, db, transverse, p, qb).T
        denominator = 1 - rr * rb * survival * survival
        if np.any(denominator <= 0):
            raise ValueError("singular incoherent round-trip intensity sum")
        reflected = rf + tf * tr * rb * survival * survival / denominator
        transmitted = tf * tb * survival / denominator
        absorption = 1 - reflected - transmitted
        absorption[np.abs(absorption) < 5e-13] = 0.0
        polarized.append(np.column_stack((reflected, transmitted, absorption)))
    values = np.mean(polarized, axis=0)
    return _result(values, waves, scalar, model="incoherent_substrate",
                   polarization=polarization, substrate_thickness_nm=dsubstrate,
                   transmission_definition="flux into exterior exit port",
                   absorption_definition="front and back coating layers plus finite substrate absorption")
