"""Explicit, frozen GPU forward batches for synthesis; no optimizer state ownership.

The existing FP64 TMM provides spectra. The existing structure evaluator owns
score aggregation; CPU analytic derivatives and independent audit stay intact.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import argparse
import hashlib
import json
import math
import time
from importlib import import_module
from weakref import WeakSet
from typing import Any, Mapping

import numpy as np

from .film_design_service import active_material_references
from .film_optimization_structure import _StructureCancelled, evaluate_frozen_structure_candidate
from .film_performance import FrozenFilmEvaluator, PerformanceOptions
from .film_synthesis_absorption import assumes_unknown_k_zero, sample_synthesis_optical
from .optical_constants import prepare_optical_set

BACKENDS = frozenset({"cpu", "cuda", "rocm", "xpu"})
PROBE_MARKER = "__COAT_FILM_SYNTHESIS_FORWARD__="
_DTYPE = "float64/complex128"
_MAX_CANDIDATES = 64
_MAX_RESULTS = 64 * 1024**2


def validate_forward_device(value: Any, backend: str) -> dict[str, Any] | None:
    """Pure request validation. Never import Torch or inspect a live device."""
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError("forward_backend must be cpu, cuda, rocm or xpu")
    if value is None:
        return None
    if backend == "cpu":
        raise ValueError("CPU forward requires forward_device=None")
    keys = {"runtime", "index", "uuid", "name", "torch_version", "dtype"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("forward_device has invalid identity fields")
    binding = deepcopy(dict(value))
    if binding["runtime"] != backend or binding["dtype"] != _DTYPE:
        raise ValueError("forward_device runtime/dtype differs from the requested backend")
    if type(binding["index"]) is not int or not 0 <= binding["index"] < 1024:
        raise ValueError("forward_device index must be a nonnegative bounded integer")
    for name in ("name", "torch_version"):
        if not isinstance(binding[name], str) or not 1 <= len(binding[name]) <= 256:
            raise ValueError(f"forward_device {name} is invalid")
    if binding["uuid"] is not None and (not isinstance(binding["uuid"], str) or not 1 <= len(binding["uuid"]) <= 256):
        raise ValueError("forward_device uuid must be a string or null")
    return binding


def _runtime(backend: str) -> tuple[Any, Any]:
    # Called only by a worker or an isolated async probe process.
    torch = import_module("torch")
    hip = getattr(torch.version, "hip", None)
    if backend == "cuda" and (hip or not getattr(torch.version, "cuda", None)):
        raise ValueError("NVIDIA CUDA PyTorch build is not installed in this executor")
    if backend == "rocm" and not hip:
        raise ValueError("ROCm PyTorch build is not installed in this executor")
    runtime = getattr(torch, "xpu" if backend == "xpu" else "cuda", None)
    if runtime is None or not runtime.is_available():
        raise ValueError(f"{backend} has no available device in this executor")
    return torch, runtime


def _identity(torch: Any, runtime: Any, backend: str, index: int) -> dict[str, Any]:
    props = runtime.get_device_properties(index)
    raw_uuid = getattr(props, "uuid", None)
    if isinstance(raw_uuid, bytes):
        raw_uuid = raw_uuid.hex()
    return {"runtime": backend, "index": index, "uuid": None if raw_uuid is None else str(raw_uuid),
            "name": str(runtime.get_device_name(index)), "torch_version": str(torch.__version__), "dtype": _DTYPE}


def resolve_forward_device(backend: str, binding: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Select index 0 or verify a prebound identity, then run actual dtype checks."""
    checked = validate_forward_device(binding, backend)
    if backend == "cpu":
        return {"runtime": "cpu", "dtype": _DTYPE}
    from .film_optics_gpu import verify_gpu_dtype
    torch, runtime = _runtime(backend)
    index = 0 if checked is None else checked["index"]
    if index >= runtime.device_count():
        raise ValueError("The bound synthesis forward device no longer exists")
    actual = _identity(torch, runtime, backend, index)
    if checked is not None and actual != checked:
        raise ValueError("Synthesis forward device identity/runtime changed; probe and select it again")
    runtime.set_device(index)
    device = torch.device("xpu" if backend == "xpu" else "cuda", index)
    verify_gpu_dtype(torch, runtime, device)
    return actual


def live_probe(backend: str) -> list[dict[str, Any]]:
    """Live runtime probe; use only inside the explicit isolated probe child."""
    validate_forward_device(None, backend)
    if backend == "cpu":
        return []
    torch, runtime = _runtime(backend)
    result = []
    errors = []
    for index in range(runtime.device_count()):
        binding = _identity(torch, runtime, backend, index)
        try:
            result.append(resolve_forward_device(backend, binding))
        except (ValueError, RuntimeError, OSError, AttributeError, NotImplementedError) as exc:
            errors.append(f"index {index}: {exc}")
    if not result:
        raise ValueError("; ".join(errors) or f"{backend} has no FP64/complex128 capable device")
    return result


@dataclass
class ForwardOutcome:
    design: Mapping[str, Any]
    responses: list[dict[str, Any]]
    index: int
    consumed: bool = False


class SynthesisForward:
    """Bounded resident spectra, accounting and original-order consumption only."""

    def __init__(self, request: Any, proxy: Any, cancel_event: Any = None):
        self.request, self.proxy, self.cancel = request, proxy, cancel_event
        self.options = PerformanceOptions(backend=request.forward_backend, cpu_workers=1,
            max_result_bytes=_MAX_RESULTS, max_prepared_bytes=64 * 1024**2)
        self.cache: OrderedDict[str, FrozenFilmEvaluator] = OrderedDict()
        self.uploaded: WeakSet[FrozenFilmEvaluator] = WeakSet()
        self.samplers: dict[tuple[str, str], Any] = {}
        self.checked: dict[tuple[str, str], set[float]] = {}
        self.identity: dict[str, Any] | None = None
        self.actual_backend: str | None = None
        self.errors: list[str] = []
        self.submitted = self.completed = self.consumed = self.discarded = self.unknown = 0
        self.calls = self.points = self.batches = self.refreshes = self.refresh_attempts = self.refresh_calls = self.refresh_points = 0
        self.verify_cpu_calls = self.verify_cpu_points = self.verify_gpu_calls = self.verify_gpu_points = 0
        self.prepare_seconds = self.upload_seconds = self.compute_seconds = self.download_seconds = 0.
        self.resident_upload_seconds = 0.
        self.batch_records: list[dict[str, Any]] = []
        self.closed = False

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise _StructureCancelled()

    def _strict_sources(self, design: Mapping[str, Any], waves: np.ndarray) -> None:
        records = {m["id"]: m for m in self.request.frozen_material_snapshot["materials"]}
        domains = ", ".join(f"[{o.wavelength_min_nm:g},{o.wavelength_max_nm:g}] nm" for o in self.request.objectives)
        for ref in active_material_references(design):
            pair = (ref["material_id"], ref["optical_set_id"])
            context = f"GPU forward material={pair[0]}, optical_set={pair[1]}; target intervals {domains}"
            if pair not in self.samplers:
                record = records.get(pair[0], {})
                optical = next((s for s in record.get("optical_sets", ()) if s["id"] == pair[1]), None)
                if optical is None:
                    raise ValueError(context + ": active frozen optical source is missing")
                self.samplers[pair] = prepare_optical_set(optical, k_sampling_policy="source")
                self.checked[pair] = set()
            for wave in waves:
                wavelength = float(wave)
                if wavelength in self.checked[pair]:
                    continue
                self.check()
                try:
                    sample_synthesis_optical(self.samplers[pair], wavelength,
                        assume_unknown_k_zero=assumes_unknown_k_zero(self.request))
                except (ValueError, ArithmeticError) as exc:
                    raise ValueError(f"{context}, wavelength={wavelength:g} nm: {exc}") from exc
                self.checked[pair].add(wavelength)

    def _snapshot(self, design: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(self.request.frozen_material_snapshot)
        payload["design"] = deepcopy(dict(design))
        payload.pop("sha256", None)
        payload["sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        return payload

    def _evaluator(self, design: Mapping[str, Any], settings: Mapping[str, Any]) -> FrozenFilmEvaluator:
        if self.identity is None:
            try:
                self.identity = resolve_forward_device(self.request.forward_backend, self.request.forward_device)
            except (ImportError, ValueError, RuntimeError, OSError, AttributeError, NotImplementedError) as exc:
                self.errors.append(str(exc))
                raise ValueError(f"Explicit synthesis {self.request.forward_backend} forward unavailable: {exc}") from exc
        signature = deepcopy(dict(design))
        for layer in signature["front_layers"]:
            layer["thickness"] = {"unit": "nm", "value": 1.0}
        key = json.dumps([signature, dict(settings)], sort_keys=True, ensure_ascii=False, allow_nan=False)
        evaluator = self.cache.pop(key, None)
        if evaluator is None:
            if len(self.cache) >= 4:
                self.cache.popitem(last=False)
            started = time.perf_counter()
            evaluator = FrozenFilmEvaluator(self._snapshot(design), settings, options=self.options)
            self.prepare_seconds += time.perf_counter() - started
        self.cache[key] = evaluator
        return evaluator

    def _metadata(self, metadata: Mapping[str, Any], count: int, samples: int) -> None:
        verification = metadata.get("verification", {})
        size = int(verification.get("wavelength_samples", samples))
        self.verify_cpu_calls += int(verification.get("cpu_completed", 0))
        self.verify_gpu_calls += int(verification.get("gpu_completed", 0))
        self.verify_cpu_points += int(verification.get("cpu_completed", 0)) * size
        self.verify_gpu_points += int(verification.get("gpu_completed_wavelength_points",
            int(verification.get("gpu_completed", 0)) * size))
        # _spectra owns unknown candidate accounting once for the whole failed batch.
        self.upload_seconds += float(metadata.get("thickness_upload_seconds", 0.))
        self.compute_seconds += float(metadata.get("forward_seconds", 0.))
        self.download_seconds += float(metadata.get("download_seconds", 0.))
        self.actual_backend = metadata.get("gpu_runtime") if metadata.get("backend") == "gpu" else metadata.get("backend")
        if metadata.get("backend") != "gpu" or metadata.get("gpu_runtime") != self.request.forward_backend:
            self.calls += count
            self.points += count * samples
            reason = str(metadata.get("fallback_reason") or metadata.get("selection_reason") or "unsupported optical model")
            self.errors.append(reason)
            raise ValueError(f"Explicit synthesis {self.request.forward_backend} forward used {self.actual_backend}: {reason}")

    def _spectra(self, tasks: list[Any]) -> list[dict[str, Any]]:
        self.check()
        design = tasks[0].design
        ids = tuple(layer["id"] for layer in design["front_layers"])
        thickness = np.asarray([[layer["thickness"]["value"] for layer in task.design["front_layers"]]
                                for task in tasks], dtype=float).reshape(len(tasks), len(ids))
        responses = []
        self.submitted += len(tasks)
        self.batches += 1
        try:
            for objective in self.request.objectives:
                waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples)
                self._strict_sources(design, waves)
                for angle in objective.angles_deg:
                    self.check()
                    settings = dict(wavelength_min_nm=objective.wavelength_min_nm,
                        wavelength_max_nm=objective.wavelength_max_nm, samples=objective.wavelength_samples,
                        angle_deg=angle, polarization=objective.polarization, first_surface="front")
                    evaluator = self._evaluator(design, settings)
                    try:
                        response = evaluator.evaluate_thickness_batch(ids, thickness, cancel=self.cancel)
                    except InterruptedError:
                        partial = evaluator._batch.last_metadata if evaluator._batch is not None else {}
                        verification = partial.get("verification", {})
                        size = int(verification.get("wavelength_samples", objective.wavelength_samples))
                        self.verify_cpu_calls += int(verification.get("cpu_completed", 0))
                        self.verify_cpu_points += int(verification.get("cpu_completed", 0)) * size
                        self.verify_gpu_calls += int(verification.get("gpu_completed", 0))
                        self.verify_gpu_points += int(verification.get("gpu_completed_wavelength_points", 0))
                        # While a stack gate is incomplete, its last_execution is
                        # validation work, not a training spectrum invocation.
                        if not verification or verification.get("complete"):
                            work = partial.get("partial_execution", {})
                            self.calls += int(work.get("completed_candidates", 0))
                            self.points += int(work.get("completed_wavelength_points", 0))
                        raise
                    if evaluator not in self.uploaded:
                        self.resident_upload_seconds += float(response["metadata"].get("resident_upload_seconds", 0.))
                        self.uploaded.add(evaluator)
                    self._metadata(response["metadata"], len(tasks), objective.wavelength_samples)
                    self.calls += len(tasks)
                    self.points += len(tasks) * objective.wavelength_samples
                    responses.append(response)
            self.completed += len(tasks)
            self.batch_records.append({"candidates": len(tasks), "conditions": len(responses),
                "actual_backend": self.actual_backend, "prepared_topology_layers": len(ids)})
            return responses
        except InterruptedError as exc:
            self.unknown += len(tasks)
            raise _StructureCancelled() from exc
        except _StructureCancelled:
            self.unknown += len(tasks)
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            self.errors.append(str(exc))
            self.unknown += len(tasks)
            raise ValueError(str(exc)) from exc

    def take(self, outcome: ForwardOutcome) -> Mapping[str, Any]:
        self.check()
        supplied = [{"wavelength_nm": r["wavelength_nm"],
                     **{metric: r[metric][outcome.index] for metric in ("R", "T", "A")}}
                    for r in outcome.responses]
        value = evaluate_frozen_structure_candidate(self.proxy, outcome.design,
            forward_responses=supplied, cancel_event=self.cancel)
        self.check()
        outcome.consumed = True
        self.consumed += 1
        return value

    def single(self, task: Any) -> Mapping[str, Any]:
        response = self._spectra([task])
        return self.take(ForwardOutcome(task.design, response, 0))

    @staticmethod
    def _group_key(task: Any) -> tuple[Any, ...]:
        # IDs/sequence/material are parent-created and preserved. No worker ID allocation.
        return tuple((r["id"], r["material_id"], r["optical_set_id"]) for r in task.design["front_layers"])

    @contextmanager
    def batch(self, tasks: Any, *, admit: Any = None):
        iterator = iter(tasks)
        pending = None
        active: list[ForwardOutcome] = []
        samples = sum(o.wavelength_samples * len(o.angles_deg) for o in self.request.objectives)
        capacity = min(_MAX_CANDIDATES, max(1, _MAX_RESULTS // max(24, samples * 24)))
        if self.request.max_evaluations is not None:
            capacity = 1  # No speculative work may spend a finite candidate budget.

        def ordered():
            nonlocal pending, active
            while True:
                self.check()
                try:
                    first = next(iterator) if pending is None else pending
                except StopIteration:
                    return
                pending = None
                group = [first]
                while len(group) < capacity:
                    try:
                        task = next(iterator)
                    except StopIteration:
                        break
                    if self._group_key(task) != self._group_key(first):
                        pending = task
                        break
                    group.append(task)
                if admit is not None and not admit():
                    yield first, None  # Parent applies its unchanged logical budget guard.
                    return
                responses = self._spectra(group)
                active = [ForwardOutcome(task.design, responses, index) for index, task in enumerate(group)]
                for task, outcome in zip(group, active, strict=True):
                    self.check()
                    yield task, outcome
                active = []
        try:
            yield ordered()
        finally:
            self.discarded += sum(not outcome.consumed for outcome in active)
            active.clear()

    def record_refresh(self, value: Mapping[str, Any]) -> None:
        self.refreshes += 1
        rows = value.get("responses", ())
        limit = min(10001, 1_000_000 // (len(value["design"]["front_layers"]) + 3))
        self.refresh_calls += sum(math.ceil(len(row["wavelength_nm"]) / limit) for row in rows)
        self.refresh_points += sum(len(row["wavelength_nm"]) for row in rows)

    def diagnostics(self) -> dict[str, Any]:
        return {"requested_backend": self.request.forward_backend, "actual_backend": self.actual_backend,
            "runtime": None if self.identity is None else self.identity["runtime"],
            "device_identity": deepcopy(self.identity), "dtype": _DTYPE,
            "device_binding": ("runtime_default" if self.request.forward_device is None else
                               "prebound_verified" if self.identity is not None else "prebound_unverified"),
            "batches": self.batches, "candidates_submitted": self.submitted, "candidates_completed": self.completed,
            "candidates_consumed": self.consumed, "candidates_discarded": self.discarded,
            "unknown_candidates": self.unknown, "actual_work_complete": self.unknown == 0,
            "objective_spectral_calls": None if self.unknown else self.calls,
            "wavelength_points": None if self.unknown else self.points,
            "known_objective_spectral_calls": self.calls, "known_wavelength_points": self.points,
            "cpu_derivative_refresh_attempts": self.refresh_attempts,
            "cpu_derivative_refreshes": self.refreshes,
            "cpu_derivative_work_complete": self.refreshes == self.refresh_attempts,
            "cpu_derivative_physical_calls": self.refresh_calls if self.refreshes == self.refresh_attempts else None,
            "cpu_derivative_wavelength_points": self.refresh_points if self.refreshes == self.refresh_attempts else None,
            "known_cpu_derivative_physical_calls": self.refresh_calls,
            "known_cpu_derivative_wavelength_points": self.refresh_points,
            "verification_cpu_calls": self.verify_cpu_calls, "verification_cpu_points": self.verify_cpu_points,
            "verification_gpu_calls": self.verify_gpu_calls, "verification_gpu_points": self.verify_gpu_points,
            "prepare_seconds": self.prepare_seconds, "upload_seconds": self.upload_seconds,
            "resident_upload_seconds": self.resident_upload_seconds,
            "compute_seconds": self.compute_seconds, "download_seconds": self.download_seconds,
            "errors": list(self.errors), "batch_records": list(self.batch_records), "closed": self.closed}

    def close(self) -> None:
        self.cache.clear()
        self.closed = True


def probe_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument("--backend", choices=sorted(BACKENDS - {"cpu"}), required=True)
    args = parser.parse_args()
    try:
        payload = {"ok": True, "backend": args.backend, "devices": live_probe(args.backend), "error": None}
    except (ImportError, ValueError, RuntimeError, OSError, AttributeError, NotImplementedError) as exc:
        payload = {"ok": False, "backend": args.backend, "devices": [], "error": str(exc)}
    print(PROBE_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    probe_main()
