"""Existing-worker adapter: bounded frozen forward cache, unchanged optimizer.

Install only after the job owner has validated/frozen its request. This module
does not submit a job, choose a solver, reserve evaluations or archive candidates.
Metadata belongs to the worker's existing progress channel, not result schema v1.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import copy, deepcopy
from dataclasses import replace
import json
import time
from threading import RLock
from types import MethodType

import numpy as np

from .film_design import physical_thickness_nm, validate_design
from .film_design_service import FilmDesignService, automatic_substrate_handling, simulation_settings
from .film_optimization import _checked_frozen_snapshot
from .film_performance import FrozenFilmEvaluator, PerformanceOptions


ADAPTER_VERSION = "worker-frozen-forward-v1"


class _SnapshotMaterials:
    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}

    def get(self, material_id):
        if material_id not in self.rows:
            raise ValueError(f"frozen worker material missing: {material_id}")
        return deepcopy(self.rows[material_id])

    def _records(self, material_ids=None):
        keys = self.rows if material_ids is None else material_ids
        return [deepcopy(self.rows[key]) for key in keys if key in self.rows]


def _topology(design):
    """All optical invariants; thicknesses may vary, informational text may not matter."""
    return (
        design["reference_wavelength_nm"], design.get("use_k", True),
        json.dumps(design["incident_medium"], sort_keys=True),
        json.dumps(design["exit_medium"], sort_keys=True),
        json.dumps(design["substrate"], sort_keys=True),
        tuple((side, row["id"], row["material_id"], row["optical_set_id"])
              for side in ("front", "back") for row in design[side + "_layers"]),
    )


class _ForwardCache:
    def __init__(self, service, snapshot, options, callback):
        self.service = service
        self.snapshot = _checked_frozen_snapshot(snapshot, validate_design(snapshot["design"]))
        # Even a transparent fallback cannot consult a changed live catalog.
        self.service.materials = _SnapshotMaterials(self.snapshot["materials"])
        self.design = self.snapshot["design"]
        self.topology = _topology(self.design)
        self.base_rows = {row["id"]: row for side in ("front", "back")
                          for row in self.design[side + "_layers"]}
        self.options = options or PerformanceOptions(backend="cpu", cpu_workers=1)
        if not isinstance(self.options, PerformanceOptions):
            raise ValueError("options must be PerformanceOptions")
        self.callback = callback
        if callback is not None and not callable(callback):
            raise TypeError("performance_callback must be callable")
        self.lock = RLock()
        self.cache = OrderedDict()
        self.canonical = service.simulate
        # copy() preserves an instance attribute's original bound-method owner.
        # Fall back through this private service so self.materials stays frozen.
        if (isinstance(self.canonical, MethodType)
                and not isinstance(self.canonical.__self__, type)
                and self.canonical.__self__ is not self.service):
            self.canonical = MethodType(self.canonical.__func__, self.service)
        self.is_canonical = getattr(self.canonical, "__func__", None) is FilmDesignService.simulate
        self.stats = {
            "adapter_version": ADAPTER_VERSION,
            "policy": "default_cpu_fixed_topology_forward_cache",
            "requested_backend": self.options.backend,
            "actual_backend": "not_started",
            "dtype": "float64/complex128" if self.is_canonical else "service_defined",
            "cpu_workers": 1, "capacity": 2,
            "forward_calls": 0, "cached_forward_calls": 0, "cache_hits": 0,
            "prepared_builds": 0, "cache_evictions": 0, "fallback_calls": 0,
            "failed_forward_calls": 0, "fallback_reasons": [],
            "cancelled_forward_calls": 0,
            "forward_seconds": 0., "prepare_seconds": 0.,
            "jacobian_backend": "unchanged_cpu_if_supported",
            "solver_semantics": "unchanged",
        }
        # No new GPU behavior in an existing production worker before target
        # device acceptance. A manual GPU option is an explicit CPU fallback.
        if self.options.backend == "gpu":
            self._reason("worker GPU path awaits device acceptance; CPU retained")
        self.options = replace(self.options, backend="cpu", cpu_workers=1, calibration=None)
        self._emit()

    def _emit(self):
        if self.callback is not None:
            self.callback(deepcopy(self.stats))

    def _reason(self, reason):
        reason = str(reason)[:400]
        if reason not in self.stats["fallback_reasons"] and len(self.stats["fallback_reasons"]) < 8:
            self.stats["fallback_reasons"].append(reason)

    def _fallback(self, candidate, settings, cancel_event, reason):
        self.stats["fallback_calls"] += 1
        self.stats["actual_backend"] = "cpu" if self.is_canonical else "service_defined"
        self._reason(reason)
        return self.canonical(candidate, settings, cancel_event=cancel_event)

    def simulate(self, candidate, settings=None, *, cancel_event=None):
        with self.lock:
            self.stats["forward_calls"] += 1
            begin = time.perf_counter()
            try:
                if not self.is_canonical:
                    return self._fallback(candidate, settings, cancel_event,
                                          "custom simulate implementation uses original service")
                design = validate_design(candidate)
                source_settings = {} if settings is None else dict(settings)
                source_settings.setdefault("first_surface", design.get("first_surface", "front"))
                configured = simulation_settings(source_settings)
                if _topology(design) != self.topology:
                    return self._fallback(design, source_settings, cancel_event,
                                          "topology or material/optical invariant changed")
                if configured["light_source"] != {"kind": "flat"}:
                    return self._fallback(design, source_settings, cancel_event,
                                          "non-flat source uses original full-spectrum service")
                handling = automatic_substrate_handling(design, configured["wavelength_max_nm"])
                if handling["resolved_mode"] == "incoherent":
                    return self._fallback(design, source_settings, cancel_event,
                                          "incoherent substrate uses original CPU service")
                count = configured["samples"]
                media = len(design["front_layers"]) + len(design["back_layers"]) + 3
                if count * (media * 128 + 128) > self.options.max_prepared_bytes:
                    return self._fallback(design, source_settings, cancel_event,
                                          "prepared array estimate exceeds cache resource limit")
                if count * 24 > self.options.max_result_bytes:
                    return self._fallback(design, source_settings, cancel_event,
                                          "retained forward array exceeds cache resource limit")
                key = json.dumps(configured, sort_keys=True, allow_nan=False)
                if key in self.cache:
                    prepared = self.cache[key]
                    self.cache.move_to_end(key)
                    self.stats["cache_hits"] += 1
                else:
                    preparation = time.perf_counter()
                    try:
                        # Derived step_nm is zero on a valid single-wavelength
                        # grid, so normalized settings are not parseable twice.
                        prepared = FrozenFilmEvaluator(self.snapshot, source_settings, options=self.options)
                    except (ValueError, ArithmeticError, KeyError) as exc:
                        # Sampling/preparation precedes a forward solve. Let the
                        # original frozen service accept/reject its domain; never
                        # restart the optimizer or refund a candidate evaluation.
                        return self._fallback(design, source_settings, cancel_event,
                                              f"prepared setup uses original CPU service: {exc}")
                    finally:
                        self.stats["prepare_seconds"] += time.perf_counter() - preparation
                    self.cache[key] = prepared
                    self.stats["prepared_builds"] += 1
                    while len(self.cache) > 2:
                        self.cache.popitem(last=False)
                        self.stats["cache_evictions"] += 1
                active_sides = ("front", "back") if design["substrate"]["sides"] == "double" else ("front",)
                ids, thicknesses = [], []
                for side in active_sides:
                    for row in design[side + "_layers"]:
                        layer_id = row["id"]
                        ids.append(layer_id)
                        if row["thickness"]["unit"] == "qw":
                            base = self.base_rows[layer_id]
                            if base["thickness"]["unit"] != "qw":
                                return self._fallback(design, source_settings, cancel_event,
                                                      "new QW unit requires original reference sampling")
                            value = (prepared._physical[layer_id] / base["thickness"]["value"]
                                     * row["thickness"]["value"])
                        else:
                            value = physical_thickness_nm(row, design["reference_wavelength_nm"], None)
                        thicknesses.append(value)
                self.stats["actual_backend"] = "cpu"
                response = prepared.evaluate_thickness_batch(ids, np.asarray([thicknesses]), cancel=cancel_event)
                self.stats["cached_forward_calls"] += 1
                self.stats["actual_backend"] = response["metadata"]["backend"]
                self.stats["cpu_workers"] = response["metadata"].get("cpu_workers", 1)
                # Only worker objective/replay evaluation uses this proxy; full
                # public simulation/export still uses canonical FilmDesignService.
                return {"wavelength_nm": response["wavelength_nm"],
                        **{channel: response[channel][0] for channel in ("R", "T", "A")}}
            except InterruptedError:
                self.stats["cancelled_forward_calls"] += 1
                raise
            except Exception:
                self.stats["failed_forward_calls"] += 1
                raise
            finally:
                self.stats["forward_seconds"] += time.perf_counter() - begin
                self._emit()


def create_worker_forward_service(service, frozen_snapshot, *, options=None, performance_callback=None):
    """Return a private worker service whose pure forward calls reuse a small LRU.

    ``service.materials`` MUST already be the job's frozen records. Request
    validation, budget, candidate IDs, scoring, archive, cancellation and solver
    selection remain in execute_film_optimization_job/optimize_film_design.
    This helper adds no result/checkpoint fields and performs no queue dispatch.
    """
    worker = copy(service)
    cache = _ForwardCache(copy(service), frozen_snapshot, options, performance_callback)
    worker.simulate = cache.simulate
    worker._film_forward_cache_adapter = cache
    return worker


__all__ = ["ADAPTER_VERSION", "create_worker_forward_service"]
