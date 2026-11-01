"""Display-only, bounded whole-objective-envelope forward simulation.

This module never changes optimization scores or acceptance evidence. Its
caller must run it off the GUI and optimization threads.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import threading
import time
from typing import Any

import numpy as np

from .film_design_service import FilmDesignService, active_material_references
from .film_synthesis_absorption import sample_synthesis_optical
from .optical_constants import prepare_optical_set

MAX_POINTS = 1024


def full_domain_spectrum(frozen_snapshot, design, objectives, selection, *, cancel_event=None):
    """Return detached preview samples, or raise ValueError/InterruptedError.

    Frozen n/k sources are strictly sampled over the envelope; no endpoint
    holding or joining separate target traces is permitted.  ``design`` is an
    actual candidate, not an optimizer variable vector.
    """
    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('full-domain preview cancelled')
    check()
    if not isinstance(frozen_snapshot, dict):
        raise ValueError('missing frozen material snapshot')
    source = deepcopy(frozen_snapshot)
    digest = source.pop('sha256', None)
    identity = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()
    if (source.get('schema_version') != 1 or source.get('kind') != 'coat_sim.frozen_film_design'
            or digest != identity or not isinstance(source.get('materials'), list)):
        raise ValueError('invalid frozen material snapshot')
    if (not isinstance(selection, dict) or set(selection) != {'objective_index', 'angle_deg', 'metric', 'spectrum_scope'}
            or selection['spectrum_scope'] != 'full_domain' or type(selection['objective_index']) is not int
            or not 0 <= selection['objective_index'] < len(objectives)):
        raise ValueError('invalid full-domain selection')
    objective = objectives[selection['objective_index']]
    angle, metric = selection['angle_deg'], selection['metric']
    if (isinstance(angle, bool) or not isinstance(angle, (int, float))
            or not math.isfinite(angle) or not 0 <= angle < 90
            or angle not in objective.angles_deg or metric not in ('R', 'T')):
        raise ValueError('selection does not match objective context')
    low = min(o.wavelength_min_nm for o in objectives)
    high = max(o.wavelength_max_nm for o in objectives)
    count = 1 if low == high else MAX_POINTS
    waves = np.linspace(low, high, count)
    if count > 1 and np.any(np.diff(waves) <= 0):
        raise ValueError('full-domain grid is not representable')
    records = {row['id']: row for row in source['materials']}
    if len(records) != len(source['materials']):
        raise ValueError('duplicate frozen material')
    for ref in active_material_references(design):
        check()
        record = records.get(ref['material_id'])
        optical = next((row for row in (record or {}).get('optical_sets', ())
                        if row['id'] == ref['optical_set_id']), None)
        if optical is None:
            raise ValueError('active frozen optical source missing')
        sampler = prepare_optical_set(optical, k_sampling_policy='source')
        for wavelength in waves:
            check()
            if design.get('use_k', True):
                sample_synthesis_optical(sampler, float(wavelength),
                    assume_unknown_k_zero=design.get('assume_lossless', False))
            else:
                sampler(float(wavelength), 'nm', extrapolation_policy='error', include_k=False)
    class FrozenLibrary:
        def get(self, identifier):
            if identifier not in records:
                raise ValueError('frozen material missing')
            return deepcopy(records[identifier])
    service: Any = object.__new__(FilmDesignService)
    service.materials = FrozenLibrary()
    settings = dict(wavelength_min_nm=low, wavelength_max_nm=high, samples=count,
        angle_deg=angle, polarization=objective.polarization,
        first_surface=design.get('first_surface', 'front'), extrapolation_policy='error')
    result = service.simulate(deepcopy(design), settings, cancel_event=cancel_event)
    check()
    return dict(wavelength_nm=list(result['wavelength_nm']), actual=list(result[metric]),
                indices=list(range(count)), original_point_count=count)


class LatestPreview:
    """One daemon worker; one latest desired batch, no publication."""
    def __init__(self, frozen_snapshot, objectives, control_is_current=None):
        self.snapshot = deepcopy(frozen_snapshot)
        self.objectives = tuple(objectives)
        self.control_is_current = control_is_current
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.cancel = threading.Event()
        self.desired = {}
        self.metadata = {}
        self.last_current = None
        self.selection = None
        self.key = None
        self.results = {}
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='film-full-domain-preview', daemon=True)
        self.thread.start()

    def request(self, key, selection, designs, metadata=None):
        with self.lock:
            if self.closed:
                return
            if self.key is None or self.key[:5] != key[:5]:
                self.cancel.set()
                self.cancel = threading.Event()
                self.results = {}
                self.last_current = None
            self.key = key
            self.selection = dict(selection)
            self.desired = dict(designs)
            self.metadata = dict(metadata or {})
            self.results = {identity: value for identity, value in self.results.items()
                            if identity in designs}
            self.wake.set()

    def invalidate(self):
        with self.lock:
            self.cancel.set()
            self.key = None
            self.desired = {}
            self.metadata = {}
            self.last_current = None
            self.results = {}
            self.wake.set()

    def get(self, key, physical_hash):
        with self.lock:
            if self.closed or key != self.key:
                return None
            return self.results.get(physical_hash)

    def previous_current(self, key):
        with self.lock:
            if self.closed or self.key is None or self.key[:5] != key[:5]:
                return None
            return self.last_current

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel.set()
            self.desired = {}
            self.metadata = {}
            self.last_current = None
            self.results = {}
            self.wake.set()

    def _run(self):
        while True:
            self.wake.wait()
            with self.lock:
                if self.closed:
                    return
                missing = next(((identity, design) for identity, design in self.desired.items()
                                if identity not in self.results), None)
                if missing is None:
                    self.wake.clear()
                    continue
                identity, design = missing
                key, selection, cancelled = self.key, self.selection, self.cancel
                if key is None:
                    self.wake.clear()
                    continue
                metadata = self.metadata.get(identity)
            class Cancellation:
                def __init__(inner, event, sequence, selected, check_control):
                    inner.event = event
                    inner.sequence = sequence
                    inner.selected = selected
                    inner.check_control = check_control
                    inner.last_check = 0.0
                    inner.valid = True
                def is_set(inner):
                    if inner.event.is_set():
                        return True
                    now = time.monotonic()
                    if inner.check_control is not None and now - inner.last_check >= .1:
                        inner.last_check = now
                        try:
                            inner.valid = inner.check_control(inner.sequence, inner.selected)
                        except Exception:
                            inner.valid = False
                    return not inner.valid
            guard = Cancellation(cancelled, key[1], selection, self.control_is_current)
            spectrum: Any = None
            value: tuple[str, Any, str | None] | None
            try:
                spectrum = full_domain_spectrum(self.snapshot, design, self.objectives,
                                                selection, cancel_event=guard)
                value = ('available', spectrum, None)
            except InterruptedError:
                value = None
            except Exception as exc:
                reason = ' '.join(str(exc).split())[:256] or type(exc).__name__
                value = ('unavailable', None, reason)
            stale = guard.is_set()
            with self.lock:
                valid = (not self.closed and not stale and value is not None and self.key is not None
                         and self.key[:5] == key[:5])
                if valid and identity in self.desired:
                    self.results[identity] = value
                # A slow current can finish after a newer candidate replaced it.
                # Retain one completed spectrum with its own detached summary,
                # never its design, so current updates cannot starve the view.
                if valid and metadata is not None and value is not None and value[0] == 'available':
                    self.last_current = (identity, metadata, value)
                # Invalid external control must not trigger retry spinning.
                if stale and self.cancel is cancelled and not self.closed:
                    self.desired = {}
                    self.metadata = {}
                    self.last_current = None
                    self.results = {}
                    self.wake.clear()
            # Do not retain an obsolete in-flight geometry or spectrum while
            # the idle thread waits for the next request.
            del design, missing, value, spectrum, guard, metadata
