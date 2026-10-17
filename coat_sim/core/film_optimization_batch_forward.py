"""Bounded in-process spectra for the durable deferred-DE execution mode."""
from collections import OrderedDict
from dataclasses import replace

import numpy as np

from .film_performance import FrozenFilmEvaluator, PerformanceOptions


class OptimizationBatchForward:
    """Own prepared optical buffers, never budgets or the incumbent archive."""

    def __init__(self, frozen, objectives, variable_ids, *, options=None, callback=None, execution_device=None):
        if execution_device is not None:
            from .film_xpu_execution import options_for_device
            options = options_for_device(execution_device)
        self.options = options or PerformanceOptions()
        if not isinstance(self.options, PerformanceOptions):
            raise TypeError("performance_options must be PerformanceOptions")
        self.frozen = frozen
        self.objectives = objectives
        self.variable_ids = variable_ids
        self.callback = callback
        self.cache = OrderedDict()
        self.conditions = []
        self.total_samples = 0
        for objective in objectives:
            for angle in objective.angles_deg:
                settings = dict(wavelength_min_nm=objective.wavelength_min_nm,
                                wavelength_max_nm=objective.wavelength_max_nm,
                                samples=objective.wavelength_samples,
                                angle_deg=angle, polarization=objective.polarization,
                                first_surface=frozen["design"].get("first_surface", "front"))
                self.conditions.append(settings)
                self.total_samples += objective.wavelength_samples
        self.capacity = min(8, len(self.conditions), self.options.max_prepared_bytes // 1024)
        # The sum of cached prepared-array estimates stays within the budget.
        self.prepared_options = replace(self.options, max_prepared_bytes=max(
            1024, self.options.max_prepared_bytes // self.capacity))
        self.batch_limit = self.options.max_result_bytes // max(1, self.total_samples * 24)
        if self.batch_limit < 1:
            raise ValueError("one complete objective spectrum exceeds max_result_bytes")
        self.calls = 0

    def responses(self, points, *, cancel=None):
        if len(points) > self.batch_limit:
            raise ValueError("objective batch exceeds max_result_bytes")
        responses = []
        for settings in self.conditions:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("optimization batch cancelled")
            key = tuple(settings.items())
            evaluator = self.cache.pop(key, None)
            if evaluator is None:
                if len(self.cache) >= self.capacity:
                    self.cache.popitem(last=False)
                from .film_xpu_execution import StrictXpuOptions, make_strict_evaluator
                evaluator = (make_strict_evaluator(self.frozen, settings, self.prepared_options)
                             if isinstance(self.prepared_options, StrictXpuOptions) else
                             FrozenFilmEvaluator(self.frozen, settings, options=self.prepared_options))
            self.cache[key] = evaluator
            response = evaluator.evaluate_thickness_batch(self.variable_ids, points, cancel=cancel)
            responses.append(response)
            self.calls += 1
            if self.callback is not None:
                self.callback(dict(response["metadata"], execution_mode="deferred_batch",
                                   actual_backend=response["metadata"].get("backend"),
                                   batch_candidates=len(points), batch_forward_calls=self.calls,
                                   result_transport="in_process_numpy"))
        return responses

    @staticmethod
    def candidate_responses(responses, index):
        from .film_optimization import _wavelength_chunks
        for response in responses:
            offset = 0
            for waves in _wavelength_chunks(response["wavelength_nm"]):
                end = offset + len(waves)
                yield {"wavelength_nm": waves, "R": response["R"][index, offset:end],
                       "T": response["T"][index, offset:end]}
                offset = end
