"""Finite-duration periodic quadrature without discarding a layer's tail.

Only the stationary-source/mask, constant-speed product model is supported.
This reduces repeated whole periods; quadrature still needs convergence checks.
"""
from collections import OrderedDict
from dataclasses import replace
import math
import numpy as np
from .simulator import simulate, _NoFluxError
from .source_profiles import source_rate_cache_key


class PeriodicCache:
    """Per-evaluation cache, bounded by array bytes and entry count."""
    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=64):
        self.entries = OrderedDict()
        self.bytes = 0
        self.max_bytes, self.max_entries = max_bytes, max_entries

    def get(self, key):
        found = self.entries.get(key)
        if found is not None:
            self.entries.move_to_end(key)
            return found[0]

    def put(self, key, result):
        size = sum(v.nbytes for v in result.values() if isinstance(v, np.ndarray))
        if size > self.max_bytes:
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.bytes -= previous[1]
        while self.entries and (self.bytes + size > self.max_bytes or
                                len(self.entries) >= self.max_entries):
            _, (_, old_size) = self.entries.popitem(last=False)
            self.bytes -= old_size
        self.entries[key] = result, size
        self.bytes += size


def simulate_finite_periodic(settings, *, mask, source_profile, backend,
                             cancel_event, period_s, canonical_phases,
                             cache, identity, progress=None):
    """Return integral(q complete periods + actual-phase remainder).

    Raw integrals, not normalized maps, are added. The maximum time spacing
    never exceeds the caller's duration/time_steps. Cache entries cannot escape
    the containing process evaluation. A completely dark component falls back
    to the unchanged full integrator rather than fabricating normalized flux.
    """
    duration = settings.duration_s
    if not period_s or duration < period_s or not math.isfinite(period_s):
        result = simulate(settings, mask=mask, source_profile=source_profile,
                          backend=backend, cancel_event=cancel_event, progress=progress)
        return result, settings.time_steps, False
    whole = int(math.floor(duration / period_s))
    residual = max(0., duration - whole * period_s)
    # Round only the floating-point representation of an integer period.
    if math.isclose(duration / period_s, round(duration / period_s), rel_tol=1e-12, abs_tol=1e-12):
        whole, residual = round(duration / period_s), 0.
    dt = duration / settings.time_steps
    cycle_steps = max(1, math.ceil(period_s / dt))
    cycle_settings = replace(settings, duration_s=period_s, time_steps=cycle_steps,
                             carrier_phase_deg=canonical_phases[0],
                             planet_phase_deg=canonical_phases[1])
    rate_key = (source_rate_cache_key(source_profile, settings.source_rate_nm_s)
                if source_profile is not None else None)
    cache_identity = identity, rate_key, settings.beam_attenuation_m_inv
    key = cache_identity, cycle_steps, period_s, tuple(canonical_phases)
    cycle = cache.get(key)
    count = 0
    try:
        if cycle is None:
            cycle = simulate(cycle_settings, mask=mask, source_profile=source_profile,
                             backend=backend, cancel_event=cancel_event,
                             progress=(lambda f: progress(f * .5)) if progress else None)
            count += cycle_steps
            cache.put(key, cycle)
        raw = cycle['field'] * (cycle['mean_raw'] * whole)
        if residual:
            tail_steps = max(1, math.ceil(residual / dt))
            # q whole return periods leave both angular phases unchanged.
            # Repeated physical layer phases can differ by roundoff in the
            # accumulated recipe clock. Cache quantization is <=5e-10 degree
            # and <=5e-11 s; it is not an angular quadrature approximation.
            tail_key = ("tail", cache_identity, tail_steps, round(residual, 10),
                        round(settings.carrier_phase_deg % 360, 9),
                        round(settings.planet_phase_deg % 360, 9))
            tail = cache.get(tail_key)
            if tail is None:
                tail = simulate(replace(settings, duration_s=residual, time_steps=tail_steps),
                                mask=mask, source_profile=source_profile, backend=backend,
                                cancel_event=cancel_event,
                                progress=(lambda f: progress(.5 + f * .5)) if progress else None)
                count += tail_steps
                cache.put(tail_key, tail)
            raw += tail['field'] * tail['mean_raw']
        vacuum_mean = cycle.get("vacuum_mean_raw", cycle["mean_raw"]) * whole
        if residual:
            vacuum_mean += tail.get("vacuum_mean_raw", tail["mean_raw"])
        mean = float(np.nanmean(raw))
        result = dict(cycle)
        if "vacuum_mean_raw" in cycle:
            result.update(vacuum_mean_raw=vacuum_mean, direct_beam_survival=mean / vacuum_mean)
        field = raw / mean
        result.update(field=field, mean_raw=mean,
                      cv_percent=float(np.nanstd(field)*100),
                      pv_percent=float((np.nanmax(field)-np.nanmin(field))*100),
                      min_percent=float(np.nanmin(field)*100),
                      max_percent=float(np.nanmax(field)*100))
        if progress:
            progress(1.)
        return result, count, True
    except _NoFluxError:
        result = simulate(settings, mask=mask, source_profile=source_profile,
                          backend=backend, cancel_event=cancel_event, progress=progress)
        return result, count + settings.time_steps, False
