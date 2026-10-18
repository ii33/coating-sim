"""Read-only material evidence and spectral coverage diagnostics.

This module never selects a dataset, fills unknown k, extrapolates, or certifies
production readiness. A declared range describes the input, not a measured
accuracy guarantee. Callers still use the existing solver and permission gates.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import math

from .materials import source_bindable, validate_provenance
from .optical_constants import validate_optical_sets


_TEXT_LIMIT = 2000
_PROCESS_FIELDS = ("resistance_model", "electron_beam_model")


def _positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _bounded_text(value):
    if not isinstance(value, str):
        raise ValueError("material provenance text must be a string")
    return {"text": value[:_TEXT_LIMIT], "truncated": len(value) > _TEXT_LIMIT}


def _range_nm(record):
    scale = 1000.0 if record["wavelength_unit"] == "um" else 1.0
    result = [record["wavelength_min"] * scale, record["wavelength_max"] * scale]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("optical range overflows nanometre coordinates")
    return result


def _within(value, low, high):
    # Match the source evaluator's four-ULP endpoint normalization, not an
    # optical extrapolation allowance or a wavelength measurement tolerance.
    return ((value >= low or abs(value - low) <= 4 * max(math.ulp(value), math.ulp(low)))
            and (value <= high or abs(value - high) <= 4 * max(math.ulp(value), math.ulp(high))))


def _coverage(available, requested):
    if available is None:
        return "missing"
    low, high = available
    if all(_within(value, low, high) for value in requested):
        return "complete"
    if requested[1] < low or requested[0] > high:
        return "none"
    return "partial"


def _issue(code, message):
    return {"code": code, "message": message}


def _dataset_readiness(record, requested, assume_lossless):
    n_range = _range_nm(record)
    k_record = (record if record["kind"] == "tabulated" and record["k"] is not None
                else record.get("k_data"))
    k_range = _range_nm(k_record) if k_record is not None else None
    n_coverage, k_coverage = _coverage(n_range, requested), _coverage(k_range, requested)
    known = [_issue("declared_n_range", "n has a schema-valid declared range; measurement accuracy is not verified.")]
    missing, assumptions = [], []
    if k_range is not None:
        known.append(_issue("declared_k_range", "Explicit k is present only in its own declared range; known k is retained."))
    if n_coverage != "complete":
        missing.append(_issue("n_band_gap", "The selected n dataset does not cover the entire requested band."))
    if k_coverage != "complete":
        missing.append(_issue("k_unknown" if k_range is None else "k_band_gap",
                              "k is unknown for all or part of the requested band; it is not measured zero."))
        if assume_lossless:
            assumptions.append(_issue("lossless_for_missing_k",
                                      "The caller explicitly supplied a lossless assumption for missing k only; existing k is not overridden."))
    source = _bounded_text(record["source"])
    if not record["source"].strip():
        missing.append(_issue("optical_source_missing", "No optical source reference was supplied."))
    if k_record is not None and k_record is not record and not k_record["source"].strip():
        missing.append(_issue("k_source_missing", "The independent k table has no source reference."))
    can_evaluate = n_coverage == "complete" and (k_coverage == "complete" or assume_lossless)
    if n_coverage != "complete":
        status = "missing_n_coverage"
    elif k_coverage != "complete":
        status = "assumption_dependent" if assume_lossless else "requires_lossless_assumption"
    else:
        status = "data_complete"
    joint_range = None
    if k_range is not None:
        low, high = max(n_range[0], k_range[0]), min(n_range[1], k_range[1])
        if low <= high:
            joint_range = [low, high]
    return {
        "id": record["id"], "name": record["name"], "kind": record["kind"],
        "status": status,
        "n_range_nm": n_range, "k_range_nm": k_range, "joint_nk_range_nm": joint_range,
        "n_coverage": n_coverage, "k_coverage": k_coverage,
        "can_evaluate_under_stated_assumptions": can_evaluate,
        "source": source, "notes": _bounded_text(record["notes"]),
        "k_source": _bounded_text(k_record["source"]) if k_record is not None else None,
        "process_parameters": deepcopy(record.get("process_parameters", [])),
        "known": known, "assumptions": assumptions, "missing": missing,
    }


def _process_readiness(material):
    bindable = source_bindable(material)
    models = []
    for field in _PROCESS_FIELDS:
        model = material.get(field)
        missing, assumptions, known = [], [], []
        if model is None:
            status = "unknown"
            provenance = {"reference": "", "notes": ""}
            missing.append(_issue("source_model_missing", "No process source model was supplied."))
        else:
            if not isinstance(model, Mapping):
                raise ValueError("process source model must be an object or null")
            provenance = validate_provenance(model.get("provenance"))
            status = provenance["status"]
            known.append(_issue("source_model_present", "A configurable source model is present; this is not experimental validation."))
            if status != "calibrated":
                assumptions.append(_issue("uncalibrated_source_model", "The source model is a declared assumption or unvalidated configuration."))
                missing.append(_issue("machine_calibration_missing", "No machine-specific spatial calibration is declared."))
            else:
                missing.append(_issue("calibration_not_independently_verified", "Calibration is declared with a reference; its measurement quality and applicability are not verified here."))
        if not bindable:
            missing.append(_issue("not_source_bindable", "This material role cannot be bound as a deposition film source."))
        models.append({
            "model": field, "status": status,
            "calibration_declared": status == "calibrated",
            "calibration_verified": False,
            "source": _bounded_text(provenance["reference"]),
            "notes": _bounded_text(provenance["notes"]),
            "known": known, "assumptions": assumptions, "missing": missing,
        })
    return {
        "source_bindable": bindable, "models": models,
        "absolute_deposition_rate_prediction_available": False,
        "missing": [_issue("absolute_rate_model_unavailable",
                           "Material names, optical data and source defaults do not establish a calibrated power/temperature-to-deposition-rate model.")],
    }


def material_readiness(material, *, wavelength_min_nm, wavelength_max_nm,
                       optical_set_id=None, assume_lossless=False):
    """Describe one material snapshot without accessing files or mutating inputs.

    Both library ``optical_constants`` and film-library ``optical_sets`` are
    accepted; conflicting aliases fail. No dataset is silently selected, even
    when only one exists. ``assume_lossless`` records caller input only, and
    does not authorize changes or change any numerical solver policy.

    ``data_complete`` refers only to n/k coverage. Model accuracy, measured
    conditions, source rights, solver eligibility and production readiness
    are not established by this report. Source excerpts are bounded and marked
    if truncated. Invalid data fails rather than silently omitting a dataset.
    """
    if not isinstance(material, Mapping):
        raise ValueError("material must be an object")
    identity, name = material.get("id"), material.get("name")
    if not isinstance(identity, str) or not identity.strip() or not isinstance(name, str):
        raise ValueError("material requires an explicit id and name")
    if type(assume_lossless) is not bool:
        raise ValueError("assume_lossless must be an explicit bool")
    requested = [_positive_number(wavelength_min_nm, "wavelength_min_nm"),
                 _positive_number(wavelength_max_nm, "wavelength_max_nm")]
    if requested[0] >= requested[1]:
        raise ValueError("wavelength_min_nm must be less than wavelength_max_nm")
    if optical_set_id is not None and (not isinstance(optical_set_id, str) or not optical_set_id):
        raise ValueError("optical_set_id must be a nonempty string or null")
    primary, alternate = material.get("optical_constants"), material.get("optical_sets")
    records = validate_optical_sets(primary if primary is not None else alternate)
    if primary is not None and alternate is not None and records != validate_optical_sets(alternate):
        raise ValueError("optical_constants and optical_sets disagree")
    if optical_set_id is not None and optical_set_id not in {row["id"] for row in records}:
        raise ValueError("optical_set_id does not belong to this material")
    rows = [_dataset_readiness(record, requested, assume_lossless) for record in records]
    selected = next((row for row in rows if row["id"] == optical_set_id), None)
    if not rows:
        status = "no_optical_data"
        missing = [_issue("optical_data_missing", "No calculable optical dataset exists in this material snapshot.")]
    elif selected is None:
        status = "selection_required"
        missing = [_issue("dataset_selection_required", "Select an exact dataset ID after checking its source and deposition conditions.")]
    else:
        status = selected["status"]
        missing = deepcopy(selected["missing"])
    research = material.get("research_data") or {}
    if not isinstance(research, Mapping):
        raise ValueError("research_data must be an object")
    properties, sources = research.get("properties", []), research.get("sources", [])
    if (not isinstance(properties, list) or not isinstance(sources, list)
            or any(not isinstance(row, Mapping) for row in properties)):
        raise ValueError("research properties and sources must be lists")
    roles = material.get("roles", [])
    if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
        raise ValueError("material roles must be a list of strings")
    counts = Counter(str(row.get("status", "unspecified")) for row in properties)
    return {
        "schema_version": 1, "material_id": identity, "name": name,
        "roles": list(roles), "requested_band_nm": requested,
        "selected_optical_set_id": optical_set_id, "optical_status": status,
        "can_evaluate_under_stated_assumptions": (
            selected["can_evaluate_under_stated_assumptions"] if selected else False),
        "datasets": rows,
        "known": deepcopy(selected["known"]) if selected else [],
        "assumptions": deepcopy(selected["assumptions"]) if selected else [],
        "missing": missing,
        "process": _process_readiness(material),
        "research": {"property_count": len(properties), "property_status_counts": dict(counts),
                     "source_record_count": len(sources)},
        "production_readiness": "not_established",
        "range_basis": "declared_dataset_domain_not_independent_measurement_verification",
        "numerical_solver_run": False, "untrusted_reference": True,
    }
