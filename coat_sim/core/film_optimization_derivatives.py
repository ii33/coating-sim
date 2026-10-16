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

from collections.abc import Mapping, Sequence
import hashlib
import json
import math

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


def _analytic_one_polarization(waves, indices, thickness_nm, layer_ids,
                               variable_columns, angle_deg, polarization):
    """Return analytic dR/dd and dT/dd by forward differentiation.

    ``r`` and ``t`` are differentiated through each scalar Möbius update in
    the coherent stack recurrence. Only propagation phase depends on layer
    thickness. For p_j = exp(2*pi*i*q_j*d_j/lambda),

        dp_j/dd_j = (2*pi*i*q_j/lambda) p_j.

    For D = 1 + r_i*r*p_j**2 and N_r = r_i + r*p_j**2,

        dr_new = (dN_r*D - N_r*dD) / D**2.

    The transmitted amplitude update is differentiated by the same quotient
    rule. Power derivatives then use d|z|^2/dd = 2 Re(conj(z)*dz/dd).
    """
    wavelength_count = waves.size
    variable_count = len(variable_columns)
    beta = _beta(None, indices[:, 0], angle_deg)
    q = _longitudinal_many(indices, beta)

    # film_optics uses a separate normalized-field solution at an exact
    # internal q=0. Its derivative is not represented by this recurrence.
    if indices.shape[1] > 2 and np.any(q[:, 1:-1] == 0):
        raise ValueError("analytic thickness Jacobian does not support an exact internal critical layer (q=0)")

    r, t = _interface_many(
        indices[:, -2], indices[:, -1], q[:, -2], q[:, -1], polarization,
    )
    dr = np.zeros((wavelength_count, variable_count), dtype=complex)
    dt = np.zeros((wavelength_count, variable_count), dtype=complex)

    # Traverse from the exit interface toward the incident medium, exactly as
    # film_optics._coherent_regular_many does.
    for index in range(indices.shape[1] - 3, -1, -1):
        layer_index = index + 1
        layer_position = layer_index - 1
        phase = np.exp(2j * math.pi * q[:, layer_index] * thickness_nm[layer_position] / waves)
        phase_squared = phase * phase
        ri, ti = _interface_many(
            indices[:, index], indices[:, layer_index],
            q[:, index], q[:, layer_index], polarization,
        )

        dphase = np.zeros((wavelength_count, variable_count), dtype=complex)
        column = variable_columns.get(layer_ids[layer_position])
        if column is not None:
            dphase[:, column] = phase * (2j * math.pi * q[:, layer_index] / waves)
        dphase_squared = 2.0 * phase[:, None] * dphase

        denominator = 1.0 + ri * r * phase_squared
        if np.any(denominator == 0):
            raise ValueError("singular coherent stack in analytic thickness Jacobian")
        ddenominator = ri[:, None] * (
            dr * phase_squared[:, None] + r[:, None] * dphase_squared
        )

        numerator_r = ri + r * phase_squared
        dnumerator_r = dr * phase_squared[:, None] + r[:, None] * dphase_squared
        dr = (dnumerator_r * denominator[:, None] -
              numerator_r[:, None] * ddenominator) / denominator[:, None]**2

        numerator_t = ti * phase * t
        dnumerator_t = ti[:, None] * (
            dphase * t[:, None] + phase[:, None] * dt
        )
        dt = (dnumerator_t * denominator[:, None] -
              numerator_t[:, None] * ddenominator) / denominator[:, None]**2

        r = numerator_r / denominator
        t = numerator_t / denominator

    incident_flux = _flux_many(indices[:, 0], q[:, 0], polarization)
    exit_flux = _flux_many(indices[:, -1], q[:, -1], polarization)
    transmission_scale = np.maximum(0.0, exit_flux) / incident_flux
    jacobian_r = 2.0 * np.real(np.conjugate(r)[:, None] * dr)
    jacobian_t = (2.0 * transmission_scale[:, None] *
                  np.real(np.conjugate(t)[:, None] * dt))
    return jacobian_r, jacobian_t


def simulate_with_thickness_jacobian(
    design, materials, variables, settings, *, zero_thickness_layer_ids=(),
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
        Private structure-probing escape hatch. Every listed ID must name one
        front layer stored in nm with an actual numeric thickness of exactly
        zero. Such rows are temporarily given a schema-valid placeholder only
        while the design is validated; the optical stack and derivative are
        then evaluated with a physical thickness of exactly 0 nm. The normal
        public design validator is unchanged, and callers must not persist a
        zero-thickness probe as a design.

    Returns a mapping with wavelength_nm, R, T, jacobian_R and jacobian_T.
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

    design_input = design
    if zero_ids:
        if not isinstance(design, Mapping):
            raise ValueError("design must be a mapping")
        # The regular schema requires positive persisted thicknesses. Validate
        # a deep copy with legal placeholders, then overwrite only the
        # numerical thickness array below. No caller-owned mapping is changed.
        from copy import deepcopy
        probe_design = deepcopy(dict(design))
        front = probe_design.get("front_layers")
        back = probe_design.get("back_layers", [])
        if not isinstance(front, list) or not isinstance(back, list):
            raise ValueError("zero-thickness probes require front/back layer lists")
        all_rows = [row for row in [*front, *back] if isinstance(row, Mapping)]
        for identifier in zero_ids:
            front_matches = [row for row in front
                             if isinstance(row, Mapping) and row.get("id") == identifier]
            back_matches = [row for row in back
                            if isinstance(row, Mapping) and row.get("id") == identifier]
            if len(front_matches) != 1 or back_matches:
                raise ValueError(
                    f"zero-thickness layer ID must identify exactly one front layer: {identifier}")
            layer = front_matches[0]
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

    rows_by_id = _checked_material_rows(materials, design)
    use_k = design.get("use_k", True)
    waves = np.linspace(normalized["wavelength_min_nm"],
                        normalized["wavelength_max_nm"], normalized["samples"])

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

    def evaluate(reference, wavelength_nm, purpose="spectrum"):
        key = (reference["material_id"], reference["optical_set_id"])
        if key not in prepared:
            material = rows_by_id.get(key[0])
            if material is None:
                raise ValueError("frozen materials do not contain material ID " + str(key[0]))
            record = next((row for row in material.get("optical_sets", [])
                           if row.get("id") == key[1]), None)
            if record is None:
                raise ValueError(f"frozen material {key[0]} does not contain optical set {key[1]}")
            prepared[key] = prepare_optical_set(
                record, k_sampling_policy="source_only_zero_defaults",
            )
        n, k, _details = prepared[key](
            float(wavelength_nm), "nm",
            extrapolation_policy=normalized["extrapolation_policy"],
            include_k=use_k,
        )
        if k is None:
            raise ValueError(f"{key[0]}/{key[1]} has no calculable k sample")
        if not use_k:
            return complex(n, 0.0)
        return complex(n, k)

    def index_series(reference):
        return np.asarray([evaluate(reference, wave) for wave in waves], dtype=complex)

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
    thickness_nm = []
    for layer in ordered_layers:
        ordered_ids.append(layer["id"])
        layer_columns.append(index_series(layer))
        n_reference = None
        if layer["thickness"]["unit"] == "qw":
            n_reference = evaluate(layer, design["reference_wavelength_nm"], purpose="reference").real
        thickness_nm.append(physical_thickness_nm(
            layer, design["reference_wavelength_nm"], n_reference,
        ))
    if zero_ids:
        ordered_id_positions = {identifier: index for index, identifier in enumerate(ordered_ids)}
        missing_zero_ids = set(zero_ids) - ordered_id_positions.keys()
        if missing_zero_ids:
            raise ValueError("zero-thickness layer IDs are not in front_layers: " +
                             ", ".join(sorted(missing_zero_ids)))
        for identifier in zero_ids:
            thickness_nm[ordered_id_positions[identifier]] = 0.0

    indices = np.column_stack([incident_indices, *layer_columns, substrate_indices])
    thickness_nm = np.asarray(thickness_nm, dtype=float)
    angle = normalized["angle_deg"]
    polarization = normalized["polarization"]

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

