"""Analytic thickness derivatives for the supported coherent film domain.

The optimizer's variables are physical layer thicknesses in nm, even when a
layer's stored design thickness is expressed as quarter waves (QW). Material
indices, their sampling conventions, and the stored-to-physical base thickness
conversion follow :mod:`film_design_service` exactly.

This first implementation supports a single front coating on a semi-infinite
substrate. It differentiates the same coherent reflection recurrence used by
``film_optics.coherent_rta``. Structures that enter a separate critical-layer
or incoherent calculation branch are rejected explicitly.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import math
from threading import RLock
from typing import Any, cast

import numpy as np

from .film_design import physical_thickness_nm, validate_design
from .film_design_service import simulation_settings
from .film_optics import (
    _beta,
    _flux_many,
    _interface_many,
    _longitudinal_many,
    coherent_rta,
)
from .optical_constants import prepare_optical_set


def _variable_fields(variable):
    if isinstance(variable, Mapping):
        try:
            return (variable["side"], variable["layer_id"],
                    variable["lower_nm"], variable["upper_nm"])
        except KeyError as exc:
            raise ValueError("each thickness variable needs side, layer_id, lower_nm and upper_nm") from exc
    try:
        return (variable.side, variable.layer_id,
                variable.lower_nm, variable.upper_nm)
    except AttributeError as exc:
        raise ValueError("each thickness variable needs side, layer_id, lower_nm and upper_nm") from exc


def _variable_id(variable):
    return _variable_fields(variable)[1]


def _checked_material_rows(materials, design):
    """Accept frozen rows or a digest-checked frozen film-design payload."""
    if isinstance(materials, Mapping):
        payload = materials
        if payload.get("kind") == "coat_sim.frozen_film_design":
            if payload.get("schema_version") != 1:
                raise ValueError("frozen material snapshot version is invalid")
            digest = payload.get("sha256")
            original = {key: value for key, value in payload.items() if key != "sha256"}
            try:
                identity = hashlib.sha256(json.dumps(
                    original, sort_keys=True, ensure_ascii=False, allow_nan=False,
                ).encode("utf-8")).hexdigest()
            except (TypeError, ValueError) as exc:
                raise ValueError("frozen material snapshot is not valid JSON data") from exc
            if digest != identity:
                raise ValueError("frozen material snapshot digest does not match")
            # The checked snapshot carries the original design, while an
            # optimizer passes a candidate with updated layer thicknesses.
            # Validate the captured design's schema but allow those candidate
            # thicknesses to differ; every candidate material reference is
            # checked against the frozen rows below as it is sampled.
            validate_design(payload.get("design"))
            rows = payload.get("materials")
        elif "materials" in payload:
            rows = payload["materials"]
        else:
            # Also accept a stable-ID mapping of frozen material records.
            rows = list(payload.values())
    else:
        rows = materials
    if (not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or
            any(not isinstance(row, Mapping) or not isinstance(row.get("id"), str)
                for row in rows)):
        raise ValueError("materials must contain frozen material records with string IDs")
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("frozen materials contain duplicate material IDs")
    return result


_MAX_PREPARED_SERIES_BYTES = 32 * 1024**2
_MAX_PREPARED_SERIES_ENTRIES = 64


def _source_sampler(rows, reference):
    key = (reference["material_id"], reference["optical_set_id"])
    material = rows.get(key[0])
    if material is None:
        raise ValueError("frozen materials do not contain material ID " + str(key[0]))
    record = next((row for row in material.get("optical_sets", [])
                   if row.get("id") == key[1]), None)
    if record is None:
        raise ValueError(f"frozen material {key[0]} does not contain optical set {key[1]}")
    return prepare_optical_set(record, k_sampling_policy="source_only_zero_defaults")


def _sample_index(sampler, key, wave, *, use_k, extrapolation_policy):
    n, k, _details = sampler(float(wave), "nm", extrapolation_policy=extrapolation_policy,
                             include_k=use_k)
    if k is None:
        raise ValueError(f"{key[0]}/{key[1]} has no calculable k sample")
    return complex(n, k if use_k else 0.)


def _sample_index_series(sampler, key, waves, *, use_k, extrapolation_policy):
    """Use the existing vector sampler, preserving scalar failure diagnostics."""
    evaluate_many = getattr(sampler, "evaluate_many", None)
    if callable(evaluate_many):
        try:
            batch = evaluate_many(waves, "nm", extrapolation_policy=extrapolation_policy,
                                  include_k=use_k)
        except (ValueError, ArithmeticError):
            # Recover the same first failing source query as the scalar path.
            # If all scalar queries succeed, preserve the original vector error.
            for wave in waves:
                _sample_index(sampler, key, wave, use_k=use_k,
                              extrapolation_policy=extrapolation_policy)
            raise
        if batch is not None and batch[0] is not None:
            return np.asarray(batch[0], dtype=complex)
    return np.asarray([_sample_index(sampler, key, wave, use_k=use_k,
                       extrapolation_policy=extrapolation_policy) for wave in waves], dtype=complex)


class _PreparedMaterialData:
    """Run-owned, independently checked optical data; never a candidate cache.

    Ordinary public material mappings are still verified on every simulation.
    Only an actual instance carries this private capability. Its sources have
    no mutable aliases to constructor inputs; cached arrays are bytes-backed
    read-only. Angle, polarization, topology and variable columns are not
    cached here: the existing per-call checks and recurrence still own them.
    """

    def __init__(self, materials: Any, design: Mapping[str, Any]):
        self.__rows = _checked_material_rows(deepcopy(materials), design)
        self.__samplers: dict[tuple[str, str], Any] = {}
        self.__series: OrderedDict[tuple[Any, ...], np.ndarray] = OrderedDict()
        self.__series_bytes = 0
        self.__lock = RLock()

    def _sampler(self, reference):
        key = (reference["material_id"], reference["optical_set_id"])
        with self.__lock:
            if key not in self.__samplers:
                self.__samplers[key] = _source_sampler(self.__rows, reference)
            return self.__samplers[key]

    def _index_series(self, reference, waves, *, use_k, extrapolation_policy):
        pair = (reference["material_id"], reference["optical_set_id"])
        waves = np.asarray(waves, dtype=np.float64)
        # Oversized arrays are evaluated normally, simply not retained. Cache
        # capacity is a memory policy, never a wavelength or search-work limit.
        retained_bytes = waves.nbytes + waves.size * np.dtype(np.complex128).itemsize
        cacheable = (retained_bytes <= _MAX_PREPARED_SERIES_BYTES
                     and _MAX_PREPARED_SERIES_ENTRIES > 0)
        key = (pair, waves.shape, waves.tobytes(), use_k, extrapolation_policy) if cacheable else None
        with self.__lock:
            if key is not None and key in self.__series:
                self.__series.move_to_end(key)
                return self.__series[key]
            values = _sample_index_series(self._sampler(reference), pair, waves,
                use_k=use_k, extrapolation_policy=extrapolation_policy)
            values = np.frombuffer(np.asarray(values, dtype=np.complex128).tobytes(), dtype=np.complex128)
            if key is not None:
                while self.__series and (len(self.__series) >= _MAX_PREPARED_SERIES_ENTRIES
                        or self.__series_bytes + retained_bytes > _MAX_PREPARED_SERIES_BYTES):
                    old_key, old_values = self.__series.popitem(last=False)
                    self.__series_bytes -= len(old_key[2]) + old_values.nbytes
                self.__series[key] = values
                self.__series_bytes += retained_bytes
            return values


def _analytic_one_polarization(waves, indices, thickness_nm, layer_ids,
                               variable_columns, angle_deg, polarization):
    """Differentiate the same scalar recurrence with two linear layer sweeps.

    For p=exp(alpha*d), z=p*p, alpha=2*pi*i*q/lambda and D=1+ri*r*z,
    each inward layer maps (r,t) to F=(ri+r*z)/D, G=ti*p*t/D.
    Store its state Jacobian [[a,0],[c,e]] and selected local thickness
    partials [b,f]. An outward sweep carries [[A,0],[B,E]]; each selected
    final derivative is [A*b, B*b+E*f], then B'=B*a+E*c using OLD E.
    Work/storage are O(wavelengths*(layers+variables)), rather than propagating
    every variable through every layer. Power projections retain the original
    flux definitions and d|amplitude|^2/dd=2*Re(conj(amplitude)*derivative).
    """
    wavelength_count = waves.size
    variable_count = len(variable_columns)
    beta = _beta(None, indices[:, 0], angle_deg)
    q = _longitudinal_many(indices, beta)

    # Preserve the original critical-layer domain: the value solver's separate
    # normalized-field branch is not represented by this analytic recurrence.
    if indices.shape[1] > 2 and np.any(q[:, 1:-1] == 0):
        raise ValueError("analytic thickness Jacobian does not support an exact internal critical layer (q=0)")

    r, t = _interface_many(
        indices[:, -2], indices[:, -1], q[:, -2], q[:, -1], polarization,
    )
    layer_count = indices.shape[1] - 2
    if variable_count:
        transfer_a = np.empty((wavelength_count, layer_count), dtype=complex)
        transfer_c = np.empty_like(transfer_a)
        transfer_e = np.empty_like(transfer_a)
        local_b = np.zeros((wavelength_count, variable_count), dtype=complex)
        local_f = np.zeros_like(local_b)

    # Traverse exit to incident, exactly as the original power-value recurrence.
    for index in range(indices.shape[1] - 3, -1, -1):
        layer_index = index + 1
        position = layer_index - 1
        phase = np.exp(2j * math.pi * q[:, layer_index] * thickness_nm[position] / waves)
        phase_squared = phase * phase
        ri, ti = _interface_many(
            indices[:, index], indices[:, layer_index], q[:, index], q[:, layer_index], polarization,
        )
        denominator = 1.0 + ri * r * phase_squared
        if np.any(denominator == 0):
            raise ValueError("singular coherent stack in analytic thickness Jacobian")
        numerator_r = ri + r * phase_squared
        numerator_t = ti * phase * t
        if variable_count:
            denominator_squared = denominator * denominator
            a = phase_squared * (1.0 - ri * ri) / denominator_squared
            transfer_a[:, position] = a
            transfer_c[:, position] = -numerator_t * ri * phase_squared / denominator_squared
            transfer_e[:, position] = ti * phase / denominator
            column = variable_columns.get(layer_ids[position])
            if column is not None:
                alpha = 2j * math.pi * q[:, layer_index] / waves
                local_b[:, column] = 2.0 * alpha * r * a
                local_f[:, column] = (numerator_t * alpha * (1.0 - ri * r * phase_squared)
                                      / denominator_squared)
        r = numerator_r / denominator
        t = numerator_t / denominator

    incident_flux = _flux_many(indices[:, 0], q[:, 0], polarization)
    exit_flux = _flux_many(indices[:, -1], q[:, -1], polarization)
    transmission_scale = np.maximum(0.0, exit_flux) / incident_flux
    jacobian_r = np.zeros((wavelength_count, variable_count), dtype=float)
    jacobian_t = np.zeros_like(jacobian_r)
    if variable_count:
        chain_a = np.ones(wavelength_count, dtype=complex)
        chain_b = np.zeros(wavelength_count, dtype=complex)
        chain_e = np.ones(wavelength_count, dtype=complex)
        for position, identifier in enumerate(layer_ids):
            column = variable_columns.get(identifier)
            if column is not None:
                dr = chain_a * local_b[:, column]
                dt = chain_b * local_b[:, column] + chain_e * local_f[:, column]
                jacobian_r[:, column] = 2.0 * np.real(np.conjugate(r) * dr)
                jacobian_t[:, column] = 2.0 * transmission_scale * np.real(np.conjugate(t) * dt)
            # RHS evaluation preserves old E for B; no in-place overwrite.
            chain_b = chain_b * transfer_a[:, position] + chain_e * transfer_c[:, position]
            chain_a = chain_a * transfer_a[:, position]
            chain_e = chain_e * transfer_e[:, position]
    return jacobian_r, jacobian_t


def _prepare_thickness_jacobian_inputs(
    design, materials, variables, settings, *, zero_thickness_layer_ids=(),
    wavelengths_nm=None,
):
    """Simulate a frozen single-front coherent design and its thickness Jacobian.

    Parameters
    ----------
    design:
        Validated film design snapshot. Stable layer IDs identify rows in
        ``front_layers``; user-authored order is preserved for ID lookup while
        the optical stack follows the service's reversed front-layer order.
    materials:
        Frozen material rows, a stable-ID mapping of such rows, or a
        digest-checked ``coat_sim.frozen_film_design`` payload. Only captured
        optical-set records are sampled; no live material catalog is read.
    variables:
        Sequence of ``FilmThicknessVariable``-compatible records or mappings
        with ``side``, ``layer_id``, ``lower_nm`` and ``upper_nm`` fields. The
        Jacobian columns follow this sequence exactly and differentiate with
        respect to physical thickness in nm, regardless of stored nm/um/QW.
    settings:
        The same simulation-settings mapping accepted by
        ``FilmDesignService.simulate``.
    zero_thickness_layer_ids:
        Legacy structure-probing compatibility path. Every listed ID must name
        one front layer stored in nm with an exact numeric zero. The public
        design validator now accepts zero-thickness films directly; the
        temporary placeholder in this path does not change optical evaluation.
    wavelengths_nm:
        Optional exact positive, finite, nondecreasing one-dimensional original
        wavelength array. Its length and endpoints must match settings. When
        omitted, the existing linspace path is unchanged. When supplied, both
        material sampling and the returned R/T and Jacobians use this same
        detached float64 array, without reconstructing chunk interior points.

    Returns the checked ndarray recurrence inputs. Both public forward/Jacobian
    and private scalar-objective probes share these exact checks and arrays.
    The Jacobians have shape [wavelength, variable] and units fraction/nm.
    This entry point intentionally supports only coherent front coatings on
    a single-sided, semi-infinite substrate. It rejects double-sided/back-side
    configurations and exact internal q=0 critical-layer cases.
    """
    if (not isinstance(zero_thickness_layer_ids, Sequence) or
            isinstance(zero_thickness_layer_ids, (str, bytes))):
        raise ValueError("zero_thickness_layer_ids must be a sequence of unique front-layer IDs")
    zero_ids = list(zero_thickness_layer_ids)
    if any(not isinstance(identifier, str) or not identifier for identifier in zero_ids):
        raise ValueError("zero_thickness_layer_ids must contain nonempty string IDs")
    if len(set(zero_ids)) != len(zero_ids):
        raise ValueError("zero_thickness_layer_ids must not contain duplicates")

    if zero_ids:
        if not isinstance(design, Mapping):
            raise ValueError("design must be a mapping")
        # Preserve the legacy structure-probe path without changing its caller
        # input. The public validator also accepts these exact zero values.
        from copy import deepcopy
        probe_design = deepcopy(dict(design))
        front = probe_design.get("front_layers")
        back = probe_design.get("back_layers", [])
        if not isinstance(front, list) or not isinstance(back, list):
            raise ValueError("zero-thickness probes require front/back layer lists")
        for identifier in zero_ids:
            front_matches = [row for row in front
                             if isinstance(row, Mapping) and row.get("id") == identifier]
            back_matches = [row for row in back
                            if isinstance(row, Mapping) and row.get("id") == identifier]
            if len(front_matches) != 1 or back_matches:
                raise ValueError(
                    f"zero-thickness layer ID must identify exactly one front layer: {identifier}")
            layer = cast(dict[str, Any], front_matches[0])
            thickness = layer.get("thickness")
            if not isinstance(thickness, Mapping) or thickness.get("unit") != "nm":
                raise ValueError(f"zero-thickness layer must use nm units: {identifier}")
            value = thickness.get("value")
            if (isinstance(value, bool) or not isinstance(value, (int, float, np.number)) or
                    not math.isfinite(float(value)) or float(value) != 0.0):
                raise ValueError(
                    f"zero-thickness layer must store an exact finite numeric zero: {identifier}")
            # Keep the placeholder shape and all material references intact;
            # validate_design remains the schema/material authority.
            layer["thickness"] = {"value": 1.0, "unit": "nm"}
        design = validate_design(probe_design)
    else:
        design = validate_design(design)
    if not isinstance(settings, Mapping):
        raise ValueError("settings must be a simulation-settings mapping")
    normalized_input = dict(settings)
    normalized_input.setdefault("first_surface", design.get("first_surface", "front"))
    normalized = simulation_settings(normalized_input)

    substrate = design["substrate"]
    if substrate["sides"] != "single":
        raise ValueError("analytic thickness Jacobian currently supports only single-sided substrates")
    if normalized["first_surface"] != "front":
        raise ValueError("analytic thickness Jacobian does not support back-surface incidence")
    if substrate.get("mode") not in ("auto", "coherent"):
        raise ValueError("analytic thickness Jacobian requires coherent substrate handling")

    prepared_materials = materials if type(materials) is _PreparedMaterialData else None
    rows_by_id = None if prepared_materials is not None else _checked_material_rows(materials, design)
    use_k = design.get("use_k", True)
    if wavelengths_nm is None:
        waves = np.linspace(normalized["wavelength_min_nm"],
                            normalized["wavelength_max_nm"], normalized["samples"])
    else:
        if not isinstance(wavelengths_nm, (Sequence, np.ndarray)) or isinstance(wavelengths_nm, (str, bytes)):
            raise ValueError("explicit wavelengths_nm must be a one-dimensional real array")
        raw_waves = np.asarray(wavelengths_nm)
        if (raw_waves.ndim != 1 or raw_waves.dtype.kind not in "iuf"
                or any(isinstance(value, (bool, np.bool_)) for value in wavelengths_nm)):
            raise ValueError("explicit wavelengths_nm must contain real numbers, not bool/complex values")
        waves = np.array(raw_waves, dtype=np.float64, copy=True)
        if (waves.shape != (normalized["samples"],) or not np.all(np.isfinite(waves))
                or np.any(waves <= 0) or np.any(waves[1:] < waves[:-1])):
            raise ValueError("explicit wavelengths_nm must match samples and be finite, positive and nondecreasing")
        if (waves[0] != normalized["wavelength_min_nm"]
                or (len(waves) > 1 and waves[-1] != normalized["wavelength_max_nm"])):
            raise ValueError("explicit wavelengths_nm endpoints differ from simulation settings")

    variable_list = list(variables)
    variable_columns = {}
    for column, variable in enumerate(variable_list):
        side, layer_id, lower, upper = _variable_fields(variable)
        if side != "front":
            raise ValueError("analytic thickness Jacobian variables must select front layers")
        if not isinstance(layer_id, str) or not layer_id:
            raise ValueError("thickness variable layer_id must be a nonempty stable ID")
        if layer_id in variable_columns:
            raise ValueError("thickness variable layer IDs must be unique")
        try:
            lower, upper = float(lower), float(upper)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("thickness variable bounds must be finite nm values") from exc
        if not math.isfinite(lower) or not math.isfinite(upper) or lower < 0 or upper < lower:
            raise ValueError("thickness variable bounds must be finite, nonnegative, and ordered")
        variable_columns[layer_id] = column

    layers_by_id = {layer["id"]: layer for layer in design["front_layers"]}
    missing = set(variable_columns) - layers_by_id.keys()
    if missing:
        raise ValueError("thickness variable layer IDs are not in front_layers: " + ", ".join(sorted(missing)))

    prepared = {}

    def sampler(reference):
        key = (reference["material_id"], reference["optical_set_id"])
        if prepared_materials is not None:
            return prepared_materials._sampler(reference)
        if key not in prepared:
            prepared[key] = _source_sampler(rows_by_id, reference)
        return prepared[key]

    def evaluate(reference, wavelength_nm, purpose="spectrum"):
        key = (reference["material_id"], reference["optical_set_id"])
        return _sample_index(sampler(reference), key, wavelength_nm, use_k=use_k,
                             extrapolation_policy=normalized["extrapolation_policy"])

    # This call has one immutable wavelength grid, k policy and material set.
    # Repeated films share the sampled index vector; thickness/Jacobian columns
    # remain independent. No array is changed after it enters this local cache.
    sampled_series = {}

    def index_series(reference):
        key = (reference["material_id"], reference["optical_set_id"])
        if key not in sampled_series:
            if prepared_materials is not None:
                sampled_series[key] = prepared_materials._index_series(reference, waves,
                    use_k=use_k, extrapolation_policy=normalized["extrapolation_policy"])
            else:
                sampled_series[key] = _sample_index_series(sampler(reference), key, waves,
                    use_k=use_k, extrapolation_policy=normalized["extrapolation_policy"])
        return sampled_series[key]

    def medium(reference):
        if reference.get("kind", "constant") == "constant":
            return np.full(waves.size, reference["n"], dtype=complex)
        return index_series(reference)

    incident_reference = design["incident_medium"]
    incident_indices = medium(incident_reference)
    substrate_indices = index_series(substrate)
    if np.any(incident_indices.imag != 0):
        raise ValueError("analytic thickness Jacobian requires a transparent incident port")

    ordered_layers = list(reversed(design["front_layers"]))
    ordered_ids = []
    layer_columns = []
    thickness_values_nm = []
    for layer in ordered_layers:
        ordered_ids.append(layer["id"])
        layer_columns.append(index_series(layer))
        n_reference = None
        if layer["thickness"]["unit"] == "qw":
            n_reference = evaluate(layer, design["reference_wavelength_nm"], purpose="reference").real
        thickness_values_nm.append(physical_thickness_nm(
            layer, design["reference_wavelength_nm"], n_reference,
        ))
    if zero_ids:
        ordered_id_positions = {identifier: index for index, identifier in enumerate(ordered_ids)}
        missing_zero_ids = set(zero_ids) - ordered_id_positions.keys()
        if missing_zero_ids:
            raise ValueError("zero-thickness layer IDs are not in front_layers: " +
                             ", ".join(sorted(missing_zero_ids)))
        for identifier in zero_ids:
            thickness_values_nm[ordered_id_positions[identifier]] = 0.0

    indices = np.column_stack([incident_indices, *layer_columns, substrate_indices])
    thickness_nm = np.asarray(thickness_values_nm, dtype=float)
    angle = normalized["angle_deg"]
    polarization = normalized["polarization"]

    return waves, indices, thickness_nm, ordered_ids, variable_columns, angle, polarization


def _thickness_jacobian_arrays(
    waves, indices, thickness_nm, ordered_ids, variable_columns, angle, polarization,
):
    """Return both finite union-column Jacobians without a second value solve.

    Transmission is differentiated with the actual incident/exit flux, never
    inferred as 1-R. Keep both finite checks even for an R-only or T-only probe.
    """
    # Differentiate first so an exact critical layer is rejected before the
    # value solver enters its separate normalized-field implementation.
    polarizations = ("s", "p") if polarization == "unpolarized" else (polarization,)
    jacobians = [
        _analytic_one_polarization(
            waves, indices, thickness_nm, ordered_ids, variable_columns,
            angle, one_polarization,
        )
        for one_polarization in polarizations
    ]
    jacobian_r = np.mean([item[0] for item in jacobians], axis=0)
    jacobian_t = np.mean([item[1] for item in jacobians], axis=0)
    if not np.all(np.isfinite(jacobian_r)) or not np.all(np.isfinite(jacobian_t)):
        raise ValueError("nonfinite analytic thickness Jacobian")

    return jacobian_r, jacobian_t


def _simulate_thickness_jacobian_arrays(
    design, materials, variables, settings, *, wavelengths_nm=None,
):
    """Private ndarray-only FD probe channel; no power solve or list conversion."""
    prepared = _prepare_thickness_jacobian_inputs(
        design, materials, variables, settings, wavelengths_nm=wavelengths_nm,
    )
    jacobian_r, jacobian_t = _thickness_jacobian_arrays(*prepared)
    return prepared[0], jacobian_r, jacobian_t


def simulate_with_thickness_jacobian(
    design, materials, variables, settings, *, zero_thickness_layer_ids=(),
    wavelengths_nm=None,
):
    """Return canonical coherent R/T and analytic thickness Jacobians as lists.

    Columns differentiate physical nm in the supplied variable order. The exact
    optional grid and all domain checks are shared with the private ndarray-only
    probe path by :func:`_prepare_thickness_jacobian_inputs`. This public complete
    evaluation still runs ``coherent_rta`` for canonical power values.
    """
    prepared = _prepare_thickness_jacobian_inputs(
        design, materials, variables, settings,
        zero_thickness_layer_ids=zero_thickness_layer_ids, wavelengths_nm=wavelengths_nm,
    )
    jacobian_r, jacobian_t = _thickness_jacobian_arrays(*prepared)
    waves, indices, thickness_nm, _ids, _columns, angle, polarization = prepared
    # Reuse the optical solver for the returned power values so all coherent
    # branch handling and flux definitions are precisely the same as service.
    response = coherent_rta(
        waves, indices, thickness_nm, angle_deg=angle, polarization=polarization,
    )

    return {
        "wavelength_nm": waves.tolist(),
        "R": np.asarray(response["R"], dtype=float).reshape(-1).tolist(),
        "T": np.asarray(response["T"], dtype=float).reshape(-1).tolist(),
        "jacobian_R": jacobian_r.tolist(),
        "jacobian_T": jacobian_t.tolist(),
    }

