"""Private numerical machinery for the one current ideal sensitivity product."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
from typing import Any, Mapping

import numpy as np

from .film_design import validate_design
from .film_candidate_display import (
    CandidateDisplayObserver, finish_candidate_display, record_candidate,
)
from .film_objective_criteria import FilmObjectiveCoverage, active_constraints, reduce_objective_responses
from .film_objective_payload import condition_from_payload
from .optical_constants import prepare_optical_set
from .film_optimization import (
    FilmOptimizationObjective,
    FilmThicknessVariable,
    _checked_frozen_snapshot,
    _wavelength_chunks,
)
from .film_optimization_derivatives import (
    _PreparedMaterialData,
    _simulate_thickness_jacobian_arrays,
    simulate_with_thickness_jacobian,
)
from .film_optimization_needle import _normalize_objectives, _physical_layer_thickness

from . import film_ideal_sensitivity as _public
from .film_ideal_sensitivity import (
    FilmIdealSensitivityRequest, FilmIdealSensitivityResult,
    FilmIdealSensitivityAnalysis, FilmIdealSensitivityProgress,
    FilmIdealDegradationRow, FilmIdealDegradationGuard,
    FilmIdealSensitivityBaselineCondition, FilmIdealSensitivityBaselineEvidence,
    SCHEMA_VERSION,
)
import inspect
import sys

MAX_RETAINED_RESULT_BYTES = 256 * 1024**2
MAX_WORKING_BYTES = 1024**3
MAX_CACHE_BYTES = 64 * 1024**2
BLOCK_SAMPLES = 4096

def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{label} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def _sequence(value, label):
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)


def _json_native(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("ideal sensitivity data must be finite JSON data") from exc


def _validate_physical(request):
    if not isinstance(request, FilmIdealSensitivityRequest):
        raise ValueError("request must be a FilmIdealSensitivityRequest")
    if not isinstance(request.design_snapshot, Mapping):
        raise ValueError("design_snapshot must be a mapping")
    design = validate_design(deepcopy(dict(request.design_snapshot)))
    if (
        design["substrate"]["sides"] != "single"
        or design.get("first_surface", "front") != "front"
        or design["substrate"].get("mode") not in ("auto", "coherent")
    ):
        raise ValueError("ideal sensitivity supports only single-sided coherent front coatings")
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if (
        type(request.solver_objective_ftol) not in (int, float)
        or request.solver_objective_ftol != 1e-10
    ):
        raise ValueError(
            "solver_objective_ftol is fixed at 1e-10 for the normalized solver objective"
        )
    if request.sensitivity_mode not in ("absolute", "relative"):
        raise ValueError("sensitivity_mode must explicitly be absolute or relative")
    objectives = _normalize_objectives(_sequence(request.objectives, "objectives"))
    if not objectives:
        raise ValueError("at least one optical objective is required")
    layer_ids = tuple(row["id"] for row in design["front_layers"])
    sensitivity_ids = (
        layer_ids
        if request.sensitivity_layer_ids is None
        else _sequence(request.sensitivity_layer_ids, "sensitivity_layer_ids")
    )
    if any(
        not isinstance(identifier, str) or identifier not in layer_ids
        for identifier in sensitivity_ids
    ) or len(set(sensitivity_ids)) != len(sensitivity_ids):
        raise ValueError("sensitivity_layer_ids must uniquely select existing front stable IDs")
    if layer_ids and not sensitivity_ids:
        raise ValueError("sensitivity_layer_ids must select at least one front layer")
    variables = _sequence(request.variables, "variables")
    seen = set()
    for variable in variables:
        if not isinstance(variable, FilmThicknessVariable):
            raise ValueError("variables must contain FilmThicknessVariable values")
        if (
            variable.side != "front"
            or variable.layer_id not in layer_ids
            or variable.layer_id in seen
        ):
            raise ValueError("variables must uniquely select existing front layer IDs")
        seen.add(variable.layer_id)
        low, high = _finite(variable.lower_nm, "lower_nm"), _finite(variable.upper_nm, "upper_nm")
        if low < 0 or high < low or not math.isfinite(high - low):
            raise ValueError("bounds must be finite nonnegative ordered nm values with finite span")
    for label in ("max_iterations", "max_evaluations"):
        value = getattr(request, label)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{label} must be a positive integer or null")
    if request.frozen_material_snapshot is not None:
        _checked_frozen_snapshot(request.frozen_material_snapshot, design)
    return replace(
        request,
        design_snapshot=design,
        objectives=objectives,
        variables=variables,
        sensitivity_layer_ids=tuple(sensitivity_ids),
    )


def _estimate_physical_resources(
    request: FilmIdealSensitivityRequest,
) -> dict[str, int]:
    """Conservative independent retained/Jacobian/SLSQP memory estimates, no Hessian tensor."""
    request = _validate_physical(request)
    points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    blocks = sum(
        ((o.wavelength_samples + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES) * len(o.angles_deg)
        for o in request.objectives
    )
    assert request.sensitivity_layer_ids is not None
    sensitivity = len(request.sensitivity_layer_ids)
    columns = len(set(request.sensitivity_layer_ids) | {v.layer_id for v in request.variables})
    free = sum(v.lower_nm < _finite(v.upper_nm, "upper_nm") for v in request.variables)
    eq = sum((1 if name == "mean" else o.wavelength_samples) * len(o.angles_deg)
             for o in request.objectives for name, c in active_constraints(o)
             if c.mode == "value")
    iq = sum((1 if name == "mean" else o.wavelength_samples) * len(o.angles_deg)
             * (int(c.lower_bound is not None) + int(c.upper_bound is not None))
             for o in request.objectives for name, c in active_constraints(o)
             if c.mode == "range")
    m, n = eq + iq, free
    # Include current SciPy's double workspace, normals/multipliers and a
    # conservative bound on historical SLSQP workspaces. This is optimizer
    # workspace, not a fabricated physical P x K x V Hessian.
    modern = max(
        1,
        n * (n + 1) // 2
        + 3 * m * n
        - (m + 5 * n + 7) * eq
        + 9 * m
        + 8 * n * n
        + 35 * n
        + eq * eq
        + 28
        + (2 * n * (n + 1) if iq == 0 else 0),
    )
    legacy = (
        (3 * (n + 1) + m) * (n + 2)
        + (n + 1 - eq + 1) * (max(0, iq) + 2 * n + 2)
        + 2 * m
        + 12 * n
        + 32
    )
    solver_bytes = 8 * (max(modern, legacy, 1) + max(1, m) * n + 3 * max(1, m + 2 * n + 2))
    # Both enabled reductions can retain P point rows plus A mean rows. Raw
    # values remain separate evidence even if only the A mean rows are enabled.
    analysis_bytes = points * 1544 + 512
    jacobian_bytes = 8 * points * (columns + 2)
    cache_entry = analysis_bytes + jacobian_bytes
    block_points = max(min(BLOCK_SAMPLES, o.wavelength_samples) for o in request.objectives)
    # The recurrence stores transfer states for all fixed physical layers, not
    # only selected derivative columns. Account for those complex arrays and
    # the run-owned prepared optical-series cache independently of full J.
    forward_workspace = block_points * (
        256 * (len(request.design_snapshot["front_layers"]) + 2) + 256 * (columns + 1) + 1024
    )
    prepared_optical_cache = 32 * 1024**2
    # Scalars retain only global condition scales/normalized weights/energies,
    # not a full-grid probe J or nominal analysis. Include two simultaneous
    # block-normalized arrays for multiply/divide/square temporaries and a
    # row reduction. Both union-column block Jacobians are conservatively
    # included here too, although forward_workspace already allows for them.
    extended_bytes = np.dtype(np.longdouble).itemsize
    probe_workspace = extended_bytes * (
        3 * points + block_points * (2 * sensitivity + 1)
    ) + 16 * block_points * columns
    working_bytes = (
        solver_bytes
        + forward_workspace
        + prepared_optical_cache
        + min(MAX_CACHE_BYTES, 32 * cache_entry)
        + 3 * cache_entry  # Conservative ordinary-candidate retained/build margin.
        + probe_workspace
    )
    return dict(
        condition_points=points,
        condition_blocks_per_evaluation=blocks,
        sensitivity_columns=sensitivity,
        derivative_columns=columns,
        free_variables=free,
        equality_constraints=eq,
        inequality_constraints=iq,
        estimated_analysis_bytes=analysis_bytes,
        estimated_retained_result_bytes=2 * analysis_bytes
        + len(_json_native({
            name: value for name, value in asdict(request).items()
            if name not in ("schema_version", "policy", "objective_ids", "budgets", "kernel_identity")
        }).__repr__()) * 4,
        estimated_jacobian_bytes=jacobian_bytes,
        estimated_forward_workspace_bytes=forward_workspace,
        estimated_prepared_optical_cache_bytes=prepared_optical_cache,
        estimated_probe_workspace_bytes=probe_workspace,
        estimated_probe_retained_jacobian_bytes=0,
        estimated_slsqp_workspace_bytes=solver_bytes,
        estimated_working_bytes=working_bytes,
        objective_probes_per_gradient=2 * free,
        raw_blocks_per_gradient=2 * free * blocks,
    )


def _check_frozen_material_domain(request):
    """Require source n and any existing source k over every original sample.

    Missing source k follows the ordinary spectrum/Jacobian calculation's zero
    default without rewriting frozen data. Keep the strict source sampler so an
    existing k table outside its domain cannot enter that default branch.
    Explicit use_k=False remains a user-authored idealization.
    """
    frozen = request.frozen_material_snapshot
    if frozen is None:
        raise ValueError("complete frozen materials are required")
    records = {row["id"]: row for row in frozen["materials"]}
    references = [*request.design_snapshot["front_layers"], request.design_snapshot["substrate"]]
    incident = request.design_snapshot["incident_medium"]
    if incident.get("kind") == "material":
        references.append(incident)
    samplers = {}
    for reference in references:
        key = (reference["material_id"], reference["optical_set_id"])
        if key in samplers:
            continue
        material = records.get(key[0], {})
        optical = next(
            (row for row in material.get("optical_sets", []) if row["id"] == key[1]), None
        )
        if optical is None:
            raise ValueError(f"missing frozen optical source {key[0]}/{key[1]}")
        samplers[key] = prepare_optical_set(optical, k_sampling_policy="source")
    use_k = request.design_snapshot["use_k"]
    for objective in request.objectives:
        waves = np.linspace(
            objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples
        )
        for key, sampler in samplers.items():
            try:
                series = sampler.evaluate_many(
                    waves, "nm", extrapolation_policy="error", include_k=use_k
                )
                if series is not None and series[0] is not None:
                    continue
                for wave in waves:
                    sampler(
                        float(wave), "nm", extrapolation_policy="error", include_k=use_k
                    )
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(
                    f"frozen optical source {key[0]}/{key[1]} cannot cover original "
                    f"{objective.wavelength_min_nm:g}-{objective.wavelength_max_nm:g} nm band: {exc}"
                ) from exc


def _freeze_physical_inputs(
    service: Any, request: FilmIdealSensitivityRequest
) -> FilmIdealSensitivityRequest:
    request = _validate_physical(request)
    _public._check_resources(request)
    design = request.design_snapshot
    if request.frozen_material_snapshot is None:
        if service is None or not callable(getattr(service, "freeze_design", None)):
            raise ValueError("a film service is required to freeze materials")
        first = request.objectives[0]
        payload = service.freeze_design(
            design,
            dict(
                wavelength_min_nm=first.wavelength_min_nm,
                wavelength_max_nm=first.wavelength_max_nm,
                samples=first.wavelength_samples,
                angle_deg=first.angles_deg[0],
                polarization=first.polarization,
                first_surface="front",
            ),
        )
    else:
        payload = request.frozen_material_snapshot
    payload = _checked_frozen_snapshot(payload, design)
    result = _validate_physical(
        replace(
            request,
            design_snapshot=deepcopy(payload["design"]),
            frozen_material_snapshot=deepcopy(payload),
        )
    )
    _check_frozen_material_domain(result)
    return result


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


class _FDDomainInsufficient(ValueError):
    pass


@dataclass
class _Evaluation:
    analysis: FilmIdealSensitivityAnalysis
    values: np.ndarray
    nominal_jacobian: np.ndarray
    conditions: tuple
    raw_coverage: tuple[FilmObjectiveCoverage, ...] = ()


def _request_identity(request):
    return hashlib.sha256(
        json.dumps(
            _public.film_ideal_sensitivity_request_to_payload(request),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class _Evaluator:
    def __init__(self, request, cancel_event=None, *, enforce_budget=True):
        _public._check_resources(request)
        _check_frozen_material_domain(request)
        self.request, self.cancel = request, cancel_event
        self.enforce_budget = enforce_budget
        self.identity = _request_identity(request)
        self.snapshot = request.frozen_material_snapshot
        self.materials = _PreparedMaterialData(self.snapshot, request.design_snapshot)
        self.columns = tuple(
            dict.fromkeys(
                request.sensitivity_layer_ids + tuple(v.layer_id for v in request.variables)
            )
        )
        self.derivative_variables = tuple(
            FilmThicknessVariable("front", i, 0.0, 0.0) for i in self.columns
        )
        self.sensitivity_columns = [self.columns.index(i) for i in request.sensitivity_layer_ids]
        self.variable_columns = [self.columns.index(v.layer_id) for v in request.variables]
        self.cache = OrderedDict()
        self.entry_bytes = (
            _public.estimate_film_ideal_sensitivity_resources(request)["estimated_analysis_bytes"]
            + _public.estimate_film_ideal_sensitivity_resources(request)["estimated_jacobian_bytes"]
        )
        self.evaluations = self.forward = self.jacobian = self.probes = self.cache_hits = (
            self.failures
        ) = 0
        self.gradients = 0
        # Public forward/jacobian counters retain their paired condition-block
        # budget meaning. This private counter records the actually requested
        # redundant canonical value solves, not analytic recurrence work.
        self.requested_coherent_value_solves = 0
        points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
        self.scales = np.empty(points, dtype=np.longdouble)
        self.weights = np.empty(points, dtype=np.longdouble)
        start = 0
        for objective in request.objectives:
            end = start + objective.wavelength_samples * len(objective.angles_deg)
            self.scales[start:end] = objective.normalization_scale
            self.weights[start:end] = objective.weight
            start = end
        # Identical full-condition ordering and normalization to complete
        # evaluate(): never normalize each objective or each chunk separately.
        self.weights /= np.max(self.weights)
        self.weights /= np.sum(self.weights)

    def check(self, *, block=False):
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled()
        if (
            block
            and self.enforce_budget
            and self.request.max_evaluations is not None
            and self.forward >= self.request.max_evaluations
        ):
            raise _Budget()

    def physical(self, design):
        return {
            row["id"]: _physical_layer_thickness(row, design, self.snapshot)
            for row in design["front_layers"]
        }

    def _candidate_key(self, design):
        physical = self.physical(design)
        if any(not math.isfinite(value) or value < 0 for value in physical.values()):
            raise ValueError("physical thicknesses must be finite and nonnegative")
        if any(
            not v.lower_nm <= physical[v.layer_id] <= v.upper_nm for v in self.request.variables
        ):
            raise ValueError("candidate violates explicit physical thickness bounds")
        return physical, (
            self.identity,
            np.asarray(
                [physical[row["id"]] for row in design["front_layers"]], dtype="<f8"
            ).tobytes(),
        )

    def evaluate(self, design, *, probe=False):
        self.check()
        physical, key = self._candidate_key(design)
        if probe:
            self.probes += 1
        if key in self.cache:
            self.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        self.evaluations += 1
        values, jacobians = [], []
        condition_rows: list[tuple[int, float, float, FilmOptimizationObjective]] = []
        try:
            for i, objective in enumerate(self.request.objectives):
                waves = np.linspace(
                    objective.wavelength_min_nm,
                    objective.wavelength_max_nm,
                    objective.wavelength_samples,
                )
                for angle in objective.angles_deg:
                    for chunk in _wavelength_chunks(waves, BLOCK_SAMPLES):
                        self.check(block=True)
                        self.forward += 1
                        self.jacobian += 1
                        self.requested_coherent_value_solves += 1
                        response = simulate_with_thickness_jacobian(
                            design,
                            self.materials,
                            self.derivative_variables,
                            dict(
                                wavelength_min_nm=float(chunk[0]),
                                wavelength_max_nm=float(chunk[-1]),
                                samples=len(chunk),
                                angle_deg=angle,
                                polarization=objective.polarization,
                                first_surface="front",
                            ),
                            wavelengths_nm=chunk,
                        )
                        self.check()
                        returned_waves = np.asarray(response.get("wavelength_nm"), dtype=np.float64)
                        if (
                            returned_waves.shape != chunk.shape
                            or returned_waves.tobytes()
                            != np.asarray(chunk, dtype=np.float64).tobytes()
                        ):
                            raise ValueError(
                                "physical forward changed the exact original wavelength grid"
                            )
                        actual = np.asarray(response[objective.metric], dtype=float)
                        jac = np.asarray(response["jacobian_" + objective.metric], dtype=float)
                        if (
                            actual.shape != chunk.shape
                            or jac.shape != (len(chunk), len(self.columns))
                            or not np.all(np.isfinite(actual))
                            or not np.all(np.isfinite(jac))
                        ):
                            raise ValueError("physical forward returned invalid response/Jacobian")
                        values.append(actual)
                        jacobians.append(jac)
                        condition_rows.extend((i, float(w), float(angle), objective) for w in chunk)
            nominal, jac = np.concatenate(values), np.concatenate(jacobians)
            conditions = tuple(condition_rows)
            scales = np.asarray([c[3].normalization_scale for c in conditions], dtype=np.longdouble)
            weights = np.asarray([c[3].weight for c in conditions], dtype=np.longdouble)
            # Normalize weights before multiplication to avoid overflowing sum(w).
            weights /= np.max(weights)
            weights /= np.sum(weights)
            amplitude = (
                np.ones(len(self.sensitivity_columns))
                if self.request.sensitivity_mode == "absolute"
                else np.asarray([physical[i] for i in self.request.sensitivity_layer_ids])
            )
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                normalized = (
                    np.asarray(jac[:, self.sensitivity_columns], dtype=np.longdouble)
                    * amplitude
                    / scales[:, None]
                )
                extended_value = np.sum(weights * np.sum(normalized * normalized, axis=1))
                objective_value = float(extended_value)
            if objective_value == 0.0 and np.any(normalized != 0.0):
                raise ValueError(
                    "ideal sensitivity objective underflows; a nonzero derivative cannot be claimed as zero"
                )
            if not math.isfinite(objective_value):
                raise ValueError("ideal sensitivity mean-square objective is not finite")
            reduced = _reduce_grid(self.request, nominal, jac[:, self.variable_columns])
            analysis = FilmIdealSensitivityAnalysis(
                objective_value,
                reduced.nominal_pass,
                reduced.max_error,
                reduced.weighted_rms_error,
                reduced.conditions,
                tuple(map(float, nominal)),
            )
            result = _Evaluation(analysis, nominal, jac[:, self.variable_columns], conditions,
                                 raw_coverage=reduced.coverage)
            self.check()
        except (ValueError, ArithmeticError, FloatingPointError):
            self.failures += 1
            raise
        if self.entry_bytes <= MAX_CACHE_BYTES:
            self.cache[key] = result
            while len(self.cache) > 32 or len(self.cache) * self.entry_bytes > MAX_CACHE_BYTES:
                self.cache.popitem(last=False)
        return result

    def scalar_objective(self, design):
        """Evaluate a complete FD probe without making it a candidate.

        Reuse only an exact complete-candidate cache hit. New scalar probes
        never add, replace or evict entries, including after cancellation.
        """
        self.check()
        physical, key = self._candidate_key(design)
        self.probes += 1
        if key in self.cache:
            self.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key].analysis.objective_value
        self.evaluations += 1
        amplitude = (
            np.ones(len(self.sensitivity_columns))
            if self.request.sensitivity_mode == "absolute"
            else np.asarray([physical[i] for i in self.request.sensitivity_layer_ids])
        )
        energy = np.empty(len(self.weights), dtype=np.longdouble)
        nonzero = False
        start = 0
        try:
            for objective in self.request.objectives:
                waves = np.linspace(
                    objective.wavelength_min_nm,
                    objective.wavelength_max_nm,
                    objective.wavelength_samples,
                )
                for angle in objective.angles_deg:
                    for chunk in _wavelength_chunks(waves, BLOCK_SAMPLES):
                        self.check(block=True)
                        self.forward += 1
                        self.jacobian += 1
                        returned_waves, jac_r, jac_t = _simulate_thickness_jacobian_arrays(
                            design,
                            self.materials,
                            self.derivative_variables,
                            dict(
                                wavelength_min_nm=float(chunk[0]),
                                wavelength_max_nm=float(chunk[-1]),
                                samples=len(chunk),
                                angle_deg=angle,
                                polarization=objective.polarization,
                                first_surface="front",
                            ),
                            wavelengths_nm=chunk,
                        )
                        self.check()
                        if (returned_waves.shape != chunk.shape
                                or returned_waves.tobytes()
                                != np.asarray(chunk, dtype=np.float64).tobytes()):
                            raise ValueError(
                                "physical forward changed the exact original wavelength grid"
                            )
                        # Check BOTH metrics and ALL union columns, including
                        # columns not selected by this objective's F.
                        if any(jac.shape != (len(chunk), len(self.columns))
                               or not np.all(np.isfinite(jac)) for jac in (jac_r, jac_t)):
                            raise ValueError("physical forward returned invalid response/Jacobian")
                        jac = jac_r if objective.metric == "R" else jac_t
                        end = start + len(chunk)
                        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                            normalized = (
                                np.asarray(jac[:, self.sensitivity_columns], dtype=np.longdouble)
                                * amplitude / self.scales[start:end, None]
                            )
                            energy[start:end] = np.sum(normalized * normalized, axis=1)
                        nonzero = nonzero or bool(np.any(normalized != 0.0))
                        start = end
            # Only this one full-condition reduction matches the complete
            # path's arithmetic order. Summing scalar block F values does not.
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                objective_value = float(np.sum(self.weights * energy))
            if objective_value == 0.0 and nonzero:
                raise ValueError(
                    "ideal sensitivity objective underflows; a nonzero derivative cannot be claimed as zero"
                )
            if not math.isfinite(objective_value):
                raise ValueError("ideal sensitivity mean-square objective is not finite")
            self.check()
        except (ValueError, ArithmeticError, FloatingPointError):
            self.failures += 1
            raise
        return objective_value


def _three_point_derivative(f0, f1, f2, x0, x1, x2):
    """Lagrange three-point derivative at x0 with true representable offsets."""
    a, b = x1 - x0, x2 - x0
    if a == 0 or b == 0 or a == b:
        raise _FDDomainInsufficient(
            "fd_domain_insufficient: repeated finite-difference coordinates"
        )
    # Difference form cancels a constant exactly and avoids the large f0 weight.
    derivative = (b * ((f1 - f0) / a) - a * ((f2 - f0) / b)) / (b - a)
    if not math.isfinite(derivative):
        raise ValueError("finite-difference objective gradient is nonfinite")
    return derivative


def _reduce_grid(request, values, jacobian=None):
    """Feed complete raw grids to the shared acceptance/reduction contract."""
    values = np.asarray(values, dtype=float)
    if values.shape != (_point_count(request),) or not np.all(np.isfinite(values)):
        raise ValueError("analysis must retain every finite original raw grid response")
    layers = tuple(v.layer_id for v in request.variables) if jacobian is not None else ()
    if jacobian is not None:
        jacobian = np.asarray(jacobian, dtype=float)
        if jacobian.shape != (len(values), len(layers)):
            raise ValueError("raw nominal Jacobian shape differs from the original grid")
    frames, offset = [], 0
    for index, objective in enumerate(request.objectives):
        waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                            objective.wavelength_samples)
        for angle in objective.angles_deg:
            end = offset + len(waves)
            frame = dict(objective_index=index, angle_deg=angle,
                         wavelength_nm=waves, actual_values=values[offset:end])
            if jacobian is not None:
                frame["jacobian_values_by_layer_id"] = {
                    layer: jacobian[offset:end, column] for column, layer in enumerate(layers)
                }
            frames.append(frame)
            offset = end
    return reduce_objective_responses(request.objectives, frames, layer_ids=layers)


def _strict_nominal_constraint_rows(evaluation, request, free_indices, spans):
    reduced = _reduce_grid(request, evaluation.values, evaluation.nominal_jacobian)
    inequalities, iq_jac, equalities, eq_jac = [], [], [], []
    seen_values = set()
    side_counts: dict[int, int] = {}
    for value, jacobian, index in zip(reduced.signed_bound_values,
                                     reduced.signed_bound_jacobian,
                                     reduced.signed_bound_condition_indices, strict=True):
        condition = reduced.conditions[index]
        objective = request.objectives[condition.objective_index]
        constraint = getattr(objective, condition.reduction)
        derivative = jacobian[free_indices] * spans
        if constraint.mode == "value":
            if index not in seen_values:
                equalities.append(value)
                eq_jac.append(derivative)
                seen_values.add(index)
            continue
        side_index = side_counts.get(index, 0)
        side_counts[index] = side_index + 1
        lower_side = constraint.lower_bound is not None and side_index == 0
        boundary = constraint.lower_bound if lower_side else constraint.upper_bound
        strict = constraint.strict_lower if lower_side else constraint.strict_upper
        if strict:
            moved = np.nextafter(boundary, np.inf if lower_side else -np.inf)
            value += abs(moved - boundary) / objective.normalization_scale
        inequalities.append(-value)
        iq_jac.append(-derivative)
    n = len(free_indices)
    return (np.asarray(inequalities), np.asarray(iq_jac).reshape(-1, n),
            np.asarray(equalities), np.asarray(eq_jac).reshape(-1, n))


def _candidate_design(design, variables, values):
    result = deepcopy(design)
    selected = {v.layer_id: float(value) for v, value in zip(variables, values, strict=True)}
    for row in result["front_layers"]:
        if row["id"] in selected:
            row["thickness"] = dict(value=selected[row["id"]], unit="nm")
    if isinstance(result.get("expression"), dict):
        result["expression"]["in_sync"] = False
    return result


def _record(cls, data, label):
    if not isinstance(data, Mapping) or set(data) - {f.name for f in fields(cls)}:
        raise ValueError(f"{label} has unknown fields or is not an object")
    try:
        return cls(**dict(data))
    except TypeError as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def _analysis_from_payload(data):
    if data is None:
        return None
    result = _record(FilmIdealSensitivityAnalysis, data, "analysis")
    conditions = tuple(
        condition_from_payload(row) for row in result.nominal_conditions
    )
    if type(result.nominal_targets_met) is not bool:
        raise ValueError("nominal_targets_met must be a boolean")
    for name in ("objective_value", "nominal_max_normalized_error", "nominal_weighted_rms_error"):
        if _finite(getattr(result, name), name) < 0:
            raise ValueError(f"{name} must be nonnegative")
    return replace(result, nominal_conditions=conditions,
                   full_grid_values=tuple(_finite(v, "full_grid_values")
                                          for v in result.full_grid_values))


def _optical_invariants(design):
    result = deepcopy(dict(design))
    for row in result["front_layers"]:
        row.pop("thickness")
    if isinstance(result.get("expression"), dict):
        result["expression"].pop("in_sync", None)
    return result


def _compare(expected, actual, path="result"):
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            raise ValueError(f"{path} replay fields differ")
        for key in expected:
            _compare(expected[key], actual[key], path + "." + key)
    elif isinstance(expected, (tuple, list)) and isinstance(actual, (tuple, list)):
        if len(expected) != len(actual):
            raise ValueError(f"{path} replay dimensions differ")
        for i, (a, b) in enumerate(zip(expected, actual, strict=True)):
            _compare(a, b, path + f"[{i}]")
    elif isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual) or expected != actual:
            raise ValueError(f"{path} replay flag differs")
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        # Squared derivative objectives can be far below 1e-12. An optical
        # response absolute tolerance must not allow orders-of-magnitude score
        # forgery merely because both scores are small.
        absolute_tolerance = 0.0 if path.endswith("objective_value") else 1e-12
        if not math.isclose(
            _finite(expected, path), _finite(actual, path), rel_tol=1e-9, abs_tol=absolute_tolerance
        ):
            raise ValueError(f"{path} physical replay differs")
    elif expected != actual:
        raise ValueError(f"{path} replay value differs")


def _validate_analysis_shape(analysis, request):
    if analysis is None:
        return
    reduced = _reduce_grid(request, analysis.full_grid_values)
    _compare([asdict(row) for row in analysis.nominal_conditions],
             [asdict(row) for row in reduced.conditions], "nominal_conditions")
    if analysis.nominal_targets_met is not reduced.nominal_pass:
        raise ValueError("nominal acceptance flag differs from the shared enabled checker")
    _compare(analysis.nominal_max_normalized_error, reduced.max_error,
             "nominal_max_normalized_error")
    _compare(analysis.nominal_weighted_rms_error, reduced.weighted_rms_error,
             "nominal_weighted_rms_error")


def _kernel_identity():
    # Fail closed across numerical implementation changes, not just materials.
    from . import film_optimization_derivatives, film_optics, optical_constants
    from . import film_design, film_design_service, film_objective_criteria

    h = hashlib.sha256()
    for module in (
        _public,
        sys.modules[__name__],
        film_optimization_derivatives,
        film_optics,
        optical_constants,
        film_design,
        film_design_service,
        film_objective_criteria,
    ):
        h.update(module.__name__.encode())
        try:
            h.update(inspect.getsource(module).encode())
        except (OSError, TypeError) as exc:
            raise ValueError("cannot establish physical kernel identity") from exc
    h.update(np.__version__.encode())
    return h.hexdigest()


def _evidence_digest(evidence):
    data = asdict(evidence)
    data.pop("digest")
    data["values_bits"] = np.asarray(evidence.values, dtype="<f8").tobytes().hex()
    return hashlib.sha256(
        json.dumps(
            _json_native(data), sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def _baseline_evidence(request, evaluation):
    if request.kernel_identity is None:
        raise ValueError("baseline requires a frozen kernel identity")
    conditions = tuple(
        FilmIdealSensitivityBaselineCondition(
            request.objective_ids[i],
            i,
            wave,
            np.asarray(wave, dtype="<f8").tobytes().hex(),
            angle,
            np.asarray(angle, dtype="<f8").tobytes().hex(),
            o.polarization,
            o.metric,
        )
        for i, wave, angle, o in evaluation.conditions
    )
    evidence = FilmIdealSensitivityBaselineEvidence(
        _request_identity(request),
        request.kernel_identity,
        "<f8",
        "fraction",
        conditions,
        tuple(float(x) for x in evaluation.values),
        "",
    )
    if len(evidence.values) != _point_count(request) or not all(
        math.isfinite(x) for x in evidence.values
    ):
        raise ValueError("baseline must be complete and finite before locking")
    return replace(evidence, digest=_evidence_digest(evidence))


def _point_count(request):
    return sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)


def _guard_directions(request):
    directions = []
    for objective in request.objectives:
        lower = upper = False
        for _name, constraint in active_constraints(objective):
            lower |= constraint.mode == "value" or constraint.lower_bound is not None
            upper |= constraint.mode == "value" or constraint.upper_bound is not None
        directions.append("both" if lower and upper else "lower" if lower else "upper")
    return tuple(directions)


def _sides(request, index):
    if len(request.guard_directions) != len(request.objectives):
        raise ValueError("degradation guard requires frozen objective directions")
    direction = request.guard_directions[index]
    return direction in ("lower", "both"), direction in ("upper", "both")


def _guard(evaluation, request, baseline_evidence):
    if baseline_evidence is None:
        raise ValueError("degradation guard requires the immutable frozen baseline")
    if request.policy == "strict_nominal":
        return FilmIdealDegradationGuard(
            evaluation.analysis.nominal_targets_met, 0.0, None, None, None, None, ()
        )
    budgets = {b.objective_id: b.delta / 100.0 for b in request.budgets}
    pp = {b.objective_id: b.delta for b in request.budgets}
    rows = []
    passes = True
    for index, identifier in enumerate(request.objective_ids):
        delta = budgets[identifier]
        lower, upper = _sides(request, index)
        samples = []
        for actual, baseline, (i, wave, angle, o) in zip(
            evaluation.values, baseline_evidence.values, evaluation.conditions, strict=True
        ):
            if i != index:
                continue
            # Closed comparisons, not an optimizer ftol and not a nominal epsilon.
            passed = (not lower or actual >= baseline - delta) and (
                not upper or actual <= baseline + delta
            )
            passes = passes and bool(passed)
            consumed = max(
                0.0,
                float(baseline - actual) if lower else 0.0,
                float(actual - baseline) if upper else 0.0,
            )
            excess = max(
                0.0,
                float((baseline - delta) - actual) if lower else 0.0,
                float(actual - (baseline + delta)) if upper else 0.0,
            )
            samples.append((consumed, excess, wave, angle, o.polarization))
        consumed, _excess, wave, angle, pol = max(samples, key=lambda item: item[0])
        rows.append(
            FilmIdealDegradationRow(
                identifier,
                pp[identifier],
                delta,
                consumed,
                max(item[1] for item in samples),
                wave,
                angle,
                pol,
            )
        )
    worst = max(rows, key=lambda row: row.consumed_fraction)
    return FilmIdealDegradationGuard(
        passes,
        worst.consumed_fraction,
        worst.objective_id,
        worst.wavelength_nm,
        worst.angle_deg,
        worst.polarization,
        tuple(rows),
    )


def _constraint_rows(evaluation, request, free_indices, spans, baseline_evidence):
    if request.policy == "strict_nominal":
        return _strict_nominal_constraint_rows(evaluation, request, free_indices, spans)
    if baseline_evidence is None:
        raise ValueError("constraint bounds require the immutable frozen baseline")
    deltas = {b.objective_id: b.delta / 100.0 for b in request.budgets}
    inequalities, jacobians = [], []
    for actual, baseline, derivative, (i, _wave, _angle, o) in zip(
        evaluation.values,
        baseline_evidence.values,
        evaluation.nominal_jacobian[:, free_indices],
        evaluation.conditions,
        strict=True,
    ):
        delta = deltas[request.objective_ids[i]]
        lower, upper = _sides(request, i)
        derivative = derivative * spans / o.normalization_scale
        if lower:
            inequalities.append((actual - (baseline - delta)) / o.normalization_scale)
            jacobians.append(derivative)
        if upper:
            inequalities.append(((baseline + delta) - actual) / o.normalization_scale)
            jacobians.append(-derivative)
    n = len(free_indices)
    return (
        np.asarray(inequalities),
        np.asarray(jacobians).reshape(-1, n),
        np.empty(0),
        np.empty((0, n)),
    )


class _ConstraintRowsCache:
    """One exact point only; no writable aliases and no stale failed construction."""

    def __init__(self):
        self.key = self.rows = None

    def get(self, identity, z, build):
        key = (identity, np.asarray(z, dtype="<f8").tobytes())
        if key == self.key:
            return self.rows
        # Drop the old matrices before constructing another full constraint set.
        self.key = self.rows = None
        rows = tuple(
            np.frombuffer(np.asarray(row, dtype=float).tobytes(), dtype=float).reshape(
                np.asarray(row).shape
            )
            for row in build()
        )
        self.key, self.rows = key, rows
        return rows


def _bounded_stencil(value, lower, upper, *, step_scale=1.0):
    """Use the largest legal physical 3-point template, with deterministic ties."""
    value, lower, upper = float(value), float(lower), float(upper)
    step_scale = _finite(step_scale, "step_scale")
    if step_scale <= 0:
        raise ValueError("step_scale must be finite and positive")
    if not (
        math.isfinite(value)
        and math.isfinite(lower)
        and math.isfinite(upper)
        and 0 <= lower <= value <= upper
    ):
        raise _FDDomainInsufficient(
            "fd_domain_insufficient: invalid physical finite-difference domain"
        )
    hbase = np.cbrt(np.finfo(float).eps) * max(1.0, abs(value)) * step_scale
    if not math.isfinite(hbase) or hbase <= 0:
        raise _FDDomainInsufficient(
            "fd_domain_insufficient: no finite positive finite-difference step"
        )
    left, right = value - lower, upper - value
    candidates = []
    for order, h, a, b in (
        (0, min(hbase, left, right), -1.0, 1.0),
        (1, min(hbase, right / 2.0), 1.0, 2.0),
        (2, min(hbase, left / 2.0), -1.0, -2.0),
    ):
        x1, x2 = value + a * h, value + b * h
        if (
            h > 0
            and math.isfinite(x1)
            and math.isfinite(x2)
            and lower <= x1 <= upper
            and lower <= x2 <= upper
            and x1 != value
            and x2 != value
            and x1 != x2
        ):
            candidates.append((h, -order, x1, x2))
    if not candidates:
        raise _FDDomainInsufficient(
            "fd_domain_insufficient: cannot fit two distinct legal physical probes"
        )
    _, _, x1, x2 = max(candidates)
    return x1, x2


def _execute(
    service,
    request,
    *,
    operation,
    cancel_event=None,
    progress_callback=None,
    checkpoint_callback=None,
    candidate_observer=None,
):
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must provide is_set()")
    request = _public._freeze_materials(service, request)
    evaluator = _Evaluator(request, cancel_event)
    initial_design = deepcopy(request.design_snapshot)
    physical = evaluator.physical(initial_design)
    if any(not v.lower_nm <= physical[v.layer_id] <= v.upper_nm for v in request.variables):
        raise ValueError("initial physical thickness is outside explicit variable bounds")
    baseline = best = best_design = baseline_guard = best_guard = baseline_evidence = None
    iterations = 0
    resolved_maxiter = None
    scipy_version = None
    solver_scale = None

    def result(state, reason, message="", *, final=True):
        if final and operation == "optimize":
            finish_candidate_display(candidate_observer, state=state)
        return FilmIdealSensitivityResult(
            state,
            reason,
            request.design_revision,
            deepcopy(best_design),
            {} if best_design is None else evaluator.physical(best_design),
            request,
            baseline,
            best,
            iterations,
            evaluator.evaluations,
            evaluator.forward,
            evaluator.jacobian,
            evaluator.identity,
            evaluator.probes,
            evaluator.cache_hits,
            evaluator.failures,
            evaluator.gradients,
            resolved_maxiter,
            operation,
            final,
            message,
            "raw_forward_jacobian_condition_blocks",
            scipy_version,
            solver_scale,
            None
            if baseline is None or solver_scale is None
            else baseline.objective_value / solver_scale,
            None if best is None or solver_scale is None else best.objective_value / solver_scale,
            schema_version=SCHEMA_VERSION,
            policy=request.policy,
            baseline_guard=baseline_guard,
            best_guard=best_guard,
            baseline_evidence=baseline_evidence,
        )

    def emit(stage, state="running", message=""):
        if progress_callback is not None:
            progress_callback(
                FilmIdealSensitivityProgress(
                    stage,
                    state,
                    iterations,
                    evaluator.evaluations,
                    evaluator.forward,
                    evaluator.jacobian,
                    evaluator.probes,
                    evaluator.cache_hits,
                    None if best is None else best.objective_value,
                    message,
                )
            )

    def observe_candidate(design, evaluation, *, guard_pass=None):
        # Only complete ordinary trials enter display, even on a normal cache hit.
        # Objective-gradient finite differences never call this boundary.
        if operation != "optimize" or type(candidate_observer) is not CandidateDisplayObserver:
            return
        analysis = evaluation.analysis
        record_candidate(candidate_observer, design,
            weighted_rms_error=analysis.nominal_weighted_rms_error,
            max_normalized_error=analysis.nominal_max_normalized_error,
            condition_rows=analysis.nominal_conditions, raw_coverage=evaluation.raw_coverage,
            physical_thickness_nm=evaluator.physical(design),
            is_best=analysis is best, iterations=iterations, evaluations=evaluator.evaluations,
            source="ideal_sensitivity", guard_pass=guard_pass,
            sensitivity_objective=analysis.objective_value,
            normalized_sensitivity_objective=(None if solver_scale is None
                else analysis.objective_value / solver_scale),
            usable_candidate=(guard_pass is True and baseline is not None
                and analysis.objective_value <= baseline.objective_value))

    def accept(design, evaluation):
        nonlocal best, best_design, best_guard
        analysis = evaluation.analysis
        guard = _guard(evaluation, request, baseline_evidence)
        if guard.guard_pass and (best is None or analysis.objective_value < best.objective_value):
            if baseline is not None and analysis.objective_value > baseline.objective_value:
                raise ValueError("incumbent objective cannot be worse than its baseline")
            best, best_design, best_guard = analysis, deepcopy(design), guard
            if checkpoint_callback is not None:
                checkpoint_callback(
                    _public.film_ideal_sensitivity_result_to_payload(
                        result("partial", "checkpoint", final=False)
                    )
                )
        observe_candidate(design, evaluation, guard_pass=guard.guard_pass)
        return guard

    try:
        emit("baseline")
        base_evaluation = evaluator.evaluate(initial_design)
        evaluator.check()
        candidate_evidence = _baseline_evidence(request, base_evaluation)
        evaluator.check()
        baseline_evidence = candidate_evidence
        # Cache lifetime starts after the first complete baseline lock. No
        # constraint arrays are constructed or reusable before this point.
        baseline = base_evaluation.analysis
        baseline_guard = _guard(base_evaluation, request, baseline_evidence)
        if operation == "analyze":
            # An analysis is not an optimization incumbent and may explicitly
            # describe a nominal-infeasible design.
            best, best_design, best_guard = baseline, initial_design, baseline_guard
            return result("completed", "analysis_complete")
        if request.policy == "strict_nominal" and not baseline.nominal_targets_met:
            observe_candidate(initial_design, base_evaluation, guard_pass=baseline_guard.guard_pass)
            return result(
                "failed",
                "initial_nominal_infeasible",
                "Initial design fails unchanged original nominal bounds; no feasible candidate is claimed.",
            )
        accept(initial_design, base_evaluation)
        free_indices = [i for i, v in enumerate(request.variables) if v.lower_nm < v.upper_nm]
        if not free_indices:
            return result(
                "failed",
                "all_variables_fixed",
                "No free physical thickness variable; analysis only, no optimization performed.",
            )
        import scipy  # type: ignore[import-untyped]

        scipy_version = scipy.__version__
        solver_scale = baseline.objective_value
        if solver_scale == 0.0:
            solver_scale = None
            return result(
                "completed",
                "zero_sensitivity_objective",
                "The feasible initial local derivative objective is exactly zero; this is not a finite-error or manufacturing-yield certificate.",
            )
        from scipy.optimize import minimize  # type: ignore[import-untyped]
        from scipy.optimize._slsqp_py import _minimize_slsqp  # type: ignore[import-untyped]

        resolved_maxiter = (
            request.max_iterations
            if request.max_iterations is not None
            else int(inspect.signature(_minimize_slsqp).parameters["maxiter"].default)
        )
        variables = [request.variables[i] for i in free_indices]
        lows = np.asarray([v.lower_nm for v in variables])
        highs = np.asarray([v.upper_nm for v in variables])
        spans = highs - lows
        initial_values = np.asarray([physical[v.layer_id] for v in variables])
        initial_z = (initial_values - lows) / spans
        initial_z = np.where(
            (initial_values > lows) & (initial_z == 0), np.nextafter(0.0, 1.0), initial_z
        )
        initial_z = np.where(
            (initial_values < highs) & (initial_z == 1), np.nextafter(1.0, 0.0), initial_z
        )
        last_key, last_data = None, None
        last_guard_pass = None
        constraint_cache = _ConstraintRowsCache()

        def evaluate_z(z):
            nonlocal last_key, last_data, last_guard_pass
            evaluator.check()
            z = np.asarray(z, dtype=float)
            if (
                z.shape != lows.shape
                or not np.all(np.isfinite(z))
                or np.any(z < 0)
                or np.any(z > 1)
            ):
                raise ValueError("solver trial is outside normalized physical bounds")
            key = z.tobytes()
            if last_key == key and last_data is not None:
                observe_candidate(last_data[0], last_data[1], guard_pass=last_guard_pass)
                return last_data
            values = np.where(z <= 0.5, lows + z * spans, highs - (1.0 - z) * spans)
            values = np.where(z == 0, lows, np.where(z == 1, highs, values))
            values = np.where(z == initial_z, initial_values, values)
            design = _candidate_design(initial_design, variables, values)
            evaluation = evaluator.evaluate(design)
            last_guard_pass = accept(design, evaluation).guard_pass
            last_key, last_data = key, (design, evaluation, values)
            return last_data

        def objective(z):
            value = evaluate_z(z)[1].analysis.objective_value / solver_scale
            if not math.isfinite(value):
                raise ValueError(
                    "fixed initial-objective conditioning overflows; trial rejected without clipping"
                )
            return value

        def gradient(z):
            design, evaluation, values = evaluate_z(z)
            answer = np.empty(len(variables))
            for col, (value, low, high) in enumerate(zip(values, lows, highs, strict=True)):
                evaluator.check()
                x1, x2 = _bounded_stencil(value, low, high)
                probes = []
                for probe_value in (x1, x2):
                    changed = values.copy()
                    changed[col] = probe_value
                    probe = _candidate_design(design, variables, changed)
                    probe_objective = evaluator.scalar_objective(probe)
                    # Probes can be nominal-infeasible; they are never retained
                    # as available candidates, even if their objective is lower.
                    normalized_probe = probe_objective / solver_scale
                    if not math.isfinite(normalized_probe):
                        raise ValueError(
                            "fixed initial-objective conditioning overflows at a finite-difference probe; no clipping"
                        )
                    probes.append(normalized_probe)
                normalized_center = evaluation.analysis.objective_value / solver_scale
                if not math.isfinite(normalized_center):
                    raise ValueError(
                        "fixed initial-objective conditioning overflows at the gradient center"
                    )
                answer[col] = (
                    _three_point_derivative(
                        normalized_center, probes[0], probes[1], value, x1, x2
                    )
                    * spans[col]
                )
                if not math.isfinite(answer[col]):
                    raise ValueError(
                        "fixed initial-objective gradient conditioning overflows; no partial gradient is delivered"
                    )
            evaluator.check()
            evaluator.gradients += 1
            return answer  # No half-computed gradient escapes on budget/cancel.

        def constraints(z, equality=False, derivative=False):
            data = evaluate_z(z)[1]
            rows = constraint_cache.get(
                evaluator.identity,
                z,
                lambda: _constraint_rows(data, request, free_indices, spans, baseline_evidence),
            )
            return rows[(2 if equality else 0) + int(derivative)]

        first_rows = constraint_cache.get(
            evaluator.identity,
            initial_z,
            lambda: _constraint_rows(
                base_evaluation, request, free_indices, spans, baseline_evidence
            ),
        )
        cons = []
        if len(first_rows[0]):
            cons.append(
                dict(
                    type="ineq",
                    fun=lambda z: constraints(z),
                    jac=lambda z: constraints(z, derivative=True),
                )
            )
        if len(first_rows[2]):
            cons.append(
                dict(
                    type="eq",
                    fun=lambda z: constraints(z, equality=True),
                    jac=lambda z: constraints(z, equality=True, derivative=True),
                )
            )

        del first_rows  # Do not retain the replaced baseline constraint matrices.

        def callback(z):
            nonlocal iterations
            iterations += 1
            evaluate_z(z)
            emit("search")

        options = dict(ftol=request.solver_objective_ftol)
        if request.max_iterations is not None:
            options["maxiter"] = request.max_iterations
        emit("search")
        solved = minimize(
            objective,
            initial_z,
            jac=gradient,
            bounds=[(0.0, 1.0)] * len(variables),
            constraints=cons,
            method="SLSQP",
            callback=callback,
            options=options,
        )
        evaluator.check()
        final_data = evaluate_z(solved.x)[1]
        if not _guard(final_data, request, baseline_evidence).guard_pass:
            return result(
                "partial",
                "solver_guard_rejected",
                "Final trial fails the exact policy guard; the last complete guard-feasible incumbent is retained.",
            )
        if best is not None and best.objective_value == baseline.objective_value:
            return result(
                "completed" if solved.success else "partial",
                "no_improvement",
                "No objective decrease: the unchanged full-grid guard-feasible baseline is retained.",
            )
        if solved.success:
            return result(
                "completed",
                "slsqp_converged",
                "Local derivative-sensitivity search stopped; no global or finite-error certificate.",
            )
        return result(
            "partial",
            "iteration_limit" if solved.status == 9 else "slsqp_stopped",
            str(solved.message),
        )
    except _Cancelled:
        return result(
            "cancelled",
            "cancelled",
            "Cancelled; only a complete policy-guard-feasible incumbent is retained.",
        )
    except _Budget:
        return result(
            "partial" if best is not None else "failed",
            "evaluation_limit",
            "Raw forward/Jacobian condition-block budget exhausted; no partial analysis or gradient is retained.",
        )
    except _FDDomainInsufficient as exc:
        return result("failed", "fd_domain_insufficient", str(exc))
    except (ValueError, ArithmeticError, FloatingPointError) as exc:
        return result("failed", "numerical_failure", str(exc))
