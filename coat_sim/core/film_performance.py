"""Opt-in, Qt-independent frozen batch evaluation and measured backend selection.

Existing optimizers/queues are unchanged. Callers own evaluation tokens, proposal
IDs, generation, scoring and archival. This module never writes a best design.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import platform
import sys
import time
from threading import RLock

import numpy as np

from .film_optics_batch import FrozenCoherentStack

KERNEL_VERSION = "film-batch-fp64-v2-multivendor"


def environment_key():
    identity = [platform.node(), platform.platform(), sys.version, np.__version__,
                os.cpu_count(), os.environ.get("PROCESSOR_IDENTIFIER", "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


@dataclass(frozen=True)
class PerformanceOptions:
    backend: str = "auto"
    cpu_workers: int = 1
    chunk_bytes: int = 64 * 1024**2
    max_result_bytes: int = 256 * 1024**2
    max_prepared_bytes: int = 256 * 1024**2
    calibration: dict | None = None

    def __post_init__(self):
        if self.backend not in {"auto", "cpu", "gpu", "cuda", "rocm", "xpu"}:
            raise ValueError("backend must be auto, cpu, gpu, cuda, rocm or xpu")
        if type(self.cpu_workers) is not int or not 1 <= self.cpu_workers <= 4:
            raise ValueError("cpu_workers must be integer from 1 to 4")
        if type(self.chunk_bytes) is not int or not 1024 <= self.chunk_bytes <= 256 * 1024**2:
            raise ValueError("chunk_bytes must be integer from 1024 to 256 MiB")
        if type(self.max_result_bytes) is not int or not 1024 <= self.max_result_bytes <= 1024**3:
            raise ValueError("max_result_bytes must be integer from 1024 to 1 GiB")
        if type(self.max_prepared_bytes) is not int or not 1024 <= self.max_prepared_bytes <= 1024**3:
            raise ValueError("max_prepared_bytes must be integer from 1024 to 1 GiB")
        if self.calibration is not None and not isinstance(self.calibration, dict):
            raise ValueError("calibration must be a measured profile dict or None")
        object.__setattr__(self, "calibration", deepcopy(self.calibration))


def _cancelled(cancel):
    return cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel())


class CoherentBatchEvaluator:
    """Reuses a frozen stack and a stack-local resident GPU context if selected."""

    def __init__(self, stack, options=None):
        self.stack = stack
        self.options = options or PerformanceOptions()
        self._gpu = None
        self._gpu_failure = None
        self._lock = RLock()
        self.last_metadata = {}

    def _selection(self, count):
        options = self.options
        workers = options.cpu_workers
        reason = "auto has no matching measured calibration; CPU retained"
        selected = "cpu" if options.backend == "auto" else options.backend
        profile = options.calibration
        if options.backend == "auto" and isinstance(profile, dict):
            if (profile.get("environment_key") == environment_key() and
                    profile.get("kernel_version") == KERNEL_VERSION):
                for row in profile.get("regions", []):
                    if (row.get("min_candidates", 0) <= count <= row.get("max_candidates", -1) and
                            row.get("min_waves", 0) <= self.stack.waves.size <= row.get("max_waves", -1) and
                            row.get("layers") == self.stack.internal_layer_count and
                            row.get("polarization") == self.stack.polarization and
                            row.get("chunk_bytes") == options.chunk_bytes and
                            not np.any(self.stack.critical) and
                            row.get("repetitions", 0) >= 3 and row.get("verified") is True):
                        selected = row.get("backend", "cpu")
                        workers = int(row.get("cpu_workers", 1))
                        if selected not in {"cpu", "gpu", "cuda", "rocm", "xpu"} or not 1 <= workers <= 4:
                            selected, workers = "cpu", 1
                        reason = "matching bounded measured calibration region"
                        break
        if options.backend != "auto":
            reason = "explicit requested backend"
        return selected, workers, reason

    def evaluate(self, thickness_nm, *, cancel=None):
        with self._lock:
            return self._evaluate(thickness_nm, cancel=cancel)

    def _evaluate(self, thickness_nm, *, cancel=None):
        d = np.asarray(thickness_nm, dtype=np.float64)
        if (d.ndim != 2 or d.shape[0] == 0 or d.shape[1] != self.stack.internal_layer_count or
                not np.all(np.isfinite(d)) or np.any(d < 0)):
            raise ValueError("thickness_nm requires finite nonnegative [candidate,layer] nm")
        if d.shape[0] * self.stack.waves.size * 24 > self.options.max_result_bytes:
            raise ValueError("retained batch output exceeds max_result_bytes; submit smaller batches")
        if _cancelled(cancel):
            raise InterruptedError("film batch cancelled")
        selected, workers, reason = self._selection(d.shape[0])
        start = time.perf_counter()
        error = self._gpu_failure
        if self._gpu_failure is not None:
            selected = "cpu"
        if selected in {"gpu", "cuda", "rocm", "xpu"}:
            try:
                from .film_optics_gpu import TorchCoherentBatch, gpu_domain_reason
                reason_domain = gpu_domain_reason(self.stack)
                if reason_domain:
                    raise ValueError(reason_domain)
                if self._gpu is None:
                    self._gpu = TorchCoherentBatch(self.stack, backend=selected)
                if not self._gpu.verified and d.shape[0]:
                    self._gpu.verify(d, chunk_bytes=self.options.chunk_bytes, cancel=cancel)
                values = self._gpu.evaluate(d, chunk_bytes=self.options.chunk_bytes, cancel=cancel)
                self.last_metadata = {"backend": "gpu", "gpu_runtime": self._gpu.backend,
                                      "requested_backend": self.options.backend,
                                      "selection_reason": reason,
                                      "kernel_version": KERNEL_VERSION,
                                      "dtype": "float64/complex128", "jacobian_backend": "cpu_if_supported",
                                      "total_seconds": time.perf_counter() - start,
                                      "resident_upload_seconds": self._gpu.upload_seconds,
                                      "device": self._gpu.device_name,
                                      **self._gpu.last_timing}
                return values
            except (InterruptedError, CancelledError) as exc:
                raise InterruptedError("film batch cancelled") from exc
            except (ImportError, RuntimeError, OSError, ValueError, AttributeError, NotImplementedError) as exc:
                error = str(exc)
                self._gpu_failure = error
                self._gpu = None
                selected = "cpu"
        workers = min(workers, max(1, d.shape[0]))
        workers = min(workers, max(1, self.options.chunk_bytes // 1024))
        if workers > 1 and d.shape[0] * self.stack.waves.size * 48 > self.options.max_result_bytes:
            workers = 1
            reason += "; parallel retained result buffers exceed limit; one CPU worker"
        try:
            values = self._cpu(d, workers, cancel)
        except CancelledError as exc:
            raise InterruptedError("film batch cancelled") from exc
        if _cancelled(cancel):
            raise InterruptedError("film batch cancelled")
        self.last_metadata = {"backend": "cpu", "requested_backend": self.options.backend, "cpu_workers": workers,
                              "selection_reason": reason, "fallback_reason": error,
                              "kernel_version": KERNEL_VERSION, "dtype": "float64/complex128",
                              "jacobian_backend": "cpu_if_supported", "total_seconds": time.perf_counter() - start}
        return values

    def _cpu(self, d, workers, cancel):
        if workers == 1:
            return self.stack.evaluate(d, chunk_bytes=self.options.chunk_bytes, cancel=cancel)
        else:
            # No BLAS calls in this kernel. Total scratch budget is split among
            # at most four NumPy workers; there is no worker x BLAS nesting.
            scratch = max(1024, self.options.chunk_bytes // workers)
            parts = np.array_split(d, workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="film-spectrum") as pool:
                jobs = [pool.submit(self.stack.evaluate, part, chunk_bytes=scratch, cancel=cancel)
                        for part in parts]
                return np.concatenate([job.result() for job in jobs])


class FrozenFilmEvaluator:
    """Stable service API for thickness candidates using one checked material freeze.

    ``layer_ids`` is the explicit ordered variable ID list, independent of row
    position. Noncoherent plates use the complete frozen CPU service unchanged.
    Zero thickness is only allowed through an explicit temporary probe ID list.
    """

    def __init__(self, frozen_snapshot, settings=None, *, options=None):
        from .film_design import validate_design, physical_thickness_nm
        from .film_design_service import simulation_settings, automatic_substrate_handling
        from .film_optimization import _checked_frozen_snapshot
        from .optical_constants import prepare_optical_set
        design = validate_design(deepcopy(frozen_snapshot["design"]))
        self._payload = _checked_frozen_snapshot(frozen_snapshot, design)
        self._design = self._payload["design"]
        configured = dict(self._payload.get("settings", {}) if settings is None else settings)
        configured.setdefault("first_surface", design["first_surface"])
        self._settings = simulation_settings(configured)
        self._lock = RLock()
        self.options = options or PerformanceOptions()
        active_media = (len(design["front_layers"]) + len(design["back_layers"]) + 3)
        estimated_prepared_bytes = self._settings["samples"] * (active_media * 128 + 128)
        if estimated_prepared_bytes > self.options.max_prepared_bytes:
            raise ValueError("estimated frozen optical arrays exceed max_prepared_bytes")
        self.waves = np.frombuffer(np.linspace(self._settings["wavelength_min_nm"], self._settings["wavelength_max_nm"],
                                  self._settings["samples"]).tobytes(), dtype=np.float64)
        self._layers = {row["id"]: row for side in ("front", "back") for row in design[side + "_layers"]}
        records = {row["id"]: row for row in self._payload["materials"]}
        samplers, series = {}, {}
        def sampler(reference):
            key = (reference["material_id"], reference["optical_set_id"])
            if key not in samplers:
                record = next(row for row in records[key[0]]["optical_sets"] if row["id"] == key[1])
                samplers[key] = prepare_optical_set(record, k_sampling_policy="source_only_zero_defaults")
            return samplers[key]
        def sample(reference):
            if reference.get("kind") == "constant":
                return np.full(self.waves.size, reference["n"], dtype=complex)
            key = (reference["material_id"], reference["optical_set_id"])
            if key not in series:
                prepared = sampler(reference)
                kwargs = {"extrapolation_policy": self._settings["extrapolation_policy"],
                          "include_k": design.get("use_k", True)}
                batch = prepared.evaluate_many(self.waves, "nm", **kwargs)
                many = batch[0] if batch is not None else None
                if many is None:
                    many = np.array([complex(*prepared(float(w), "nm", **kwargs)[:2]) for w in self.waves])
                series[key] = np.asarray(many, dtype=complex)
            return series[key]
        self._physical = {}
        for key, row in self._layers.items():
            nref = sampler(row)(design["reference_wavelength_nm"], "nm", include_k=False,
                               extrapolation_policy=self._settings["extrapolation_policy"])[0] if row["thickness"]["unit"] == "qw" else None
            self._physical[key] = physical_thickness_nm(row, design["reference_wavelength_nm"], nref)
        handling = automatic_substrate_handling(design, self._settings["wavelength_max_nm"])
        self.resolved_mode = handling["resolved_mode"]
        self._batch = None
        self.last_metadata = {}
        if self.resolved_mode != "incoherent":
            front, back = design["front_layers"], design["back_layers"]
            ni = sample(design["incident_medium"])
            ne = sample(design["exit_medium"]) if design["substrate"]["sides"] == "double" else None
            if self._settings["first_surface"] == "back":
                if self.resolved_mode == "semi_infinite":
                    raise ValueError("semi-infinite substrate cannot have back incidence")
                front, back, ni, ne = back, front, ne, ni
            ordered = list(reversed(front))
            columns = [ni] + [sample(row) for row in ordered] + [sample(design["substrate"])]
            self._order = [row["id"] for row in ordered]
            baseline = [self._physical[key] for key in self._order]
            if self.resolved_mode == "coherent":
                columns += [sample(row) for row in back] + [ne]
                self._order += [None] + [row["id"] for row in back]
                baseline += [handling["thickness_nm"]] + [self._physical[row["id"]] for row in back]
            self._baseline = np.asarray(baseline, dtype=float)
            stack = FrozenCoherentStack.prepare(self.waves, np.column_stack(columns),
                self._settings["angle_deg"], self._settings["polarization"])
            self._batch = CoherentBatchEvaluator(stack, self.options)

    @property
    def settings(self):
        return deepcopy(self._settings)

    def evaluate_thickness_batch(self, layer_ids, thickness_nm, *, cancel=None, zero_thickness_layer_ids=()):
        with self._lock:
            return self._evaluate_thickness_batch(layer_ids, thickness_nm, cancel=cancel,
                                                  zero_thickness_layer_ids=zero_thickness_layer_ids)

    def _evaluate_thickness_batch(self, layer_ids, thickness_nm, *, cancel=None, zero_thickness_layer_ids=()):
        ids = tuple(layer_ids)
        d = np.asarray(thickness_nm, dtype=float)
        probes = set(zero_thickness_layer_ids)
        if (len(set(ids)) != len(ids) or any(key not in self._layers for key in ids) or
                not probes.issubset(ids) or d.ndim != 2 or d.shape[0] == 0 or d.shape[1] != len(ids) or
                not np.all(np.isfinite(d)) or np.any(d < 0)):
            raise ValueError("invalid variable IDs or finite candidate thickness matrix")
        for i, key in enumerate(ids):
            if key not in probes and np.any(d[:, i] == 0):
                raise ValueError("zero thickness requires explicit temporary probe layer ID")
        if d.shape[0] * self.waves.size * 24 > self.options.max_result_bytes:
            raise ValueError("retained batch output exceeds max_result_bytes")
        if self._batch is not None:
            if any(key not in self._order for key in ids):
                raise ValueError("variable layer is not active in this optical model")
            full = np.tile(self._baseline, (d.shape[0], 1))
            for i, key in enumerate(ids):
                full[:, self._order.index(key)] = d[:, i]
            values = self._batch.evaluate(full, cancel=cancel)
            self.last_metadata = dict(self._batch.last_metadata, material_sha256=self._payload["sha256"],
                                      model=self.resolved_mode)
        else:
            if probes:
                raise ValueError("temporary zero probes for incoherent plates require CPU derivative contract")
            from .film_design_service import FilmDesignService
            class Materials:
                def get(_, key):
                    return deepcopy(next(row for row in self._payload["materials"] if row["id"] == key))
            service = FilmDesignService.__new__(FilmDesignService)
            service.materials = Materials()
            values = np.empty((len(d), self.waves.size, 3), dtype=float)
            for i, row in enumerate(d):
                if _cancelled(cancel):
                    raise InterruptedError("film frozen CPU batch cancelled")
                candidate = deepcopy(self._design)
                for side in ("front", "back"):
                    for layer in candidate[side + "_layers"]:
                        if layer["id"] in ids:
                            layer["thickness"] = {"unit": "nm", "value": float(row[ids.index(layer["id"])])}
                response = service.simulate(candidate, self._settings, cancel_event=cancel)
                values[i] = np.column_stack([response[key] for key in ("R", "T", "A")])
            self.last_metadata = {"backend": "cpu", "requested_backend": self.options.backend, "model": "incoherent",
                                  "fallback_reason": "incoherent plate uses complete frozen CPU service",
                                  "dtype": "float64/complex128", "jacobian_backend": "cpu_if_supported",
                                  "material_sha256": self._payload["sha256"]}
        if _cancelled(cancel):
            raise InterruptedError("film batch cancelled")
        return {"wavelength_nm": self.waves.copy(), "R": values[:, :, 0],
                "T": values[:, :, 1], "A": values[:, :, 2],
                "metadata": deepcopy(self.last_metadata)}


def optimize_film_design_accelerated(service, request, *, options=None,
                                     cancel_event=None, progress_callback=None,
                                     checkpoint_callback=None):
    """Opt-in existing eight-method entry; budgets/archive stay in the old owner.

    Fixed topology only. A capacity-two prepared-spectrum LRU replaces pure
    forward calls; analytic J remains CPU. No simultaneous shared file edits.
    """
    from collections import OrderedDict
    from copy import copy
    from .film_optimization import freeze_film_optimization_request, optimize_film_design
    from .film_design import physical_thickness_nm
    captured = freeze_film_optimization_request(service, request)
    payload = captured.frozen_material_snapshot
    cache = OrderedDict()
    worker = copy(service)
    def simulate(candidate, settings=None, *, cancel_event=None):
        key = json.dumps(settings, sort_keys=True)
        if key not in cache:
            cache[key] = FrozenFilmEvaluator(payload, settings, options=options)
            while len(cache) > 2:
                cache.popitem(last=False)
        evaluator = cache[key]
        cache.move_to_end(key)
        ids, thicknesses = [], []
        active_sides = ("front", "back") if candidate["substrate"]["sides"] == "double" else ("front",)
        for side in active_sides:
            for row in candidate[side + "_layers"]:
                ids.append(row["id"])
                # Unselected QW layers retain the frozen physical conversion.
                thicknesses.append(evaluator._physical[row["id"]] if row["thickness"]["unit"] == "qw" else
                                   physical_thickness_nm(row, candidate["reference_wavelength_nm"], None))
        response = evaluator.evaluate_thickness_batch(ids, np.asarray([thicknesses]), cancel=cancel_event)
        return {"wavelength_nm": response["wavelength_nm"],
                **{key: response[key][0] for key in ("R", "T", "A")}}
    worker.simulate = simulate
    return optimize_film_design(worker, captured, cancel_event=cancel_event,
                                progress_callback=progress_callback,
                                checkpoint_callback=checkpoint_callback)
