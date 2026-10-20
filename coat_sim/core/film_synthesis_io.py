"""Versioned JSON boundary for GUI-independent film synthesis execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, replace
import math
from typing import Any, Mapping, cast

from .film_auto_synthesis import (FilmSynthesisRequest, _validated,
                                  freeze_film_synthesis_request)
from .film_optimization import FilmOptimizationObjective
from .film_optimization_needle import FilmNeedleMaterialReference, _candidate_payload_from_materials
from .film_physical_structures import structure_spec_from_mapping, structure_spec_to_mapping
from .film_physical_structures import generate_physical_structure
from .optical_constants import UNITS_TO_UM
from .film_synthesis_absorption import annotate_synthesis_design, synthesis_absorption_assumption

REQUEST_KIND = "coat_sim.film_synthesis_request"
RESULT_KIND = "coat_sim.film_synthesis_result"
SCHEMA_VERSION = 1


class FilmSynthesisPayloadError(ValueError):
    """Malformed or unsupported execution payload."""


def _normalize_specs(structures):
    if not isinstance(structures, (list, tuple)) or any(not isinstance(item, Mapping) for item in structures):
        raise FilmSynthesisPayloadError("physical_structures must be an array of objects")
    try:
        return tuple(structure_spec_to_mapping(structure_spec_from_mapping(item)) for item in structures)
    except (KeyError, TypeError, ValueError) as exc:
        raise FilmSynthesisPayloadError(f"invalid physical structure spec: {exc}") from exc


def film_synthesis_request_from_payload(payload: Mapping[str, Any], *, require_frozen: bool = True) -> tuple[str, FilmSynthesisRequest, tuple[Mapping[str, Any], ...]]:
    """Parse a frozen request envelope and canonicalize its structure specs."""
    if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version", "kind", "request_id", "request", "physical_structures"}:
        raise FilmSynthesisPayloadError("request must contain exactly schema_version, kind, request_id, request, and physical_structures")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise FilmSynthesisPayloadError("unsupported synthesis request schema_version")
    if payload["kind"] != REQUEST_KIND:
        raise FilmSynthesisPayloadError("invalid synthesis request kind")
    request_id = payload["request_id"]
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise FilmSynthesisPayloadError("request_id must be a nonempty string of at most 128 characters")
    raw = payload["request"]
    if not isinstance(raw, Mapping):
        raise FilmSynthesisPayloadError("request must be an object")
    allowed = {item.name for item in fields(FilmSynthesisRequest)}
    if set(raw) - allowed:
        raise FilmSynthesisPayloadError("request contains unknown fields")
    data = deepcopy(dict(raw))
    try:
        data["objectives"] = tuple(FilmOptimizationObjective(**item) for item in data["objectives"])
        data["candidate_materials"] = tuple(FilmNeedleMaterialReference(**item)
                                             for item in data["candidate_materials"])
        data["seed_designs"] = tuple(deepcopy(item) for item in data.get("seed_designs", ()))
        request = _validated(FilmSynthesisRequest(**data))
    except (KeyError, TypeError, ValueError) as exc:
        raise FilmSynthesisPayloadError(f"invalid synthesis request: {exc}") from exc
    if require_frozen and request.frozen_material_snapshot is None:
        raise FilmSynthesisPayloadError("CLI requires a complete frozen material snapshot")
    return request_id, request, _normalize_specs(payload["physical_structures"])


def prepare_film_synthesis_payload(service: Any, payload: Mapping[str, Any], *, progress_callback=None):
    """Freeze and expand all structure specs into real seed designs for the CLI.

    A supplied service is read-only catalog access used when freezing an
    assistant plan. Offline CLI replay passes `None` and must already contain
    every referenced material in its frozen snapshot.
    """
    request_id, request, structures = film_synthesis_request_from_payload(payload, require_frozen=False)
    if request.frozen_material_snapshot is None:
        if service is None:
            raise FilmSynthesisPayloadError("CLI requires a complete frozen material snapshot")
        request = freeze_film_synthesis_request(service, request)
    frozen_snapshot = cast(Mapping[str, Any], request.frozen_material_snapshot)
    if structures:
        bindings: list[FilmNeedleMaterialReference] = []
        for mapping in structures:
            spec = structure_spec_from_mapping(mapping)
            bindings.extend(FilmNeedleMaterialReference(item.material_id, item.optical_set_id)
                            for item in spec.bindings)
        if service is not None:
            snapshot = _candidate_payload_from_materials(service, frozen_snapshot, bindings)
            request = replace(request, frozen_material_snapshot=snapshot)
            frozen_snapshot = snapshot
        else:
            available = {(material.get("id"), optical.get("id"))
                         for material in frozen_snapshot.get("materials", ())
                         for optical in material.get("optical_sets", ())}
            if any((ref.material_id, ref.optical_set_id) not in available for ref in bindings):
                raise FilmSynthesisPayloadError("physical structure materials must be present in the frozen snapshot")
    seeds = list(request.seed_designs)
    metadata = []
    for index, mapping in enumerate(structures):
        spec = structure_spec_from_mapping(mapping)
        generated = generate_physical_structure(request.environment, spec, frozen_snapshot,
            assume_unknown_k_zero=request.environment["assume_lossless"])
        seeds.append(generated.candidate_design)
        metadata.append({"index": index, "mechanism": spec.mechanism,
                         "name": spec.name, "metadata": generated.metadata})
        if progress_callback is not None:
            progress_callback({"stage": "physical_structure", "cycle": 0, "evaluations": 0,
                "layer_count": generated.metadata.get("layer_count", 0),
                "best_max_normalized_error": None,
                "message": "physical structure seed generated"})
    request = _validated(replace(request, seed_designs=tuple(seeds)))
    # Run the synthesis freeze validator after all physical seeds are present.
    # When a snapshot already exists, the core validates and reuses it without
    # consulting a live catalog; this makes plan and execution reject the same
    # out-of-domain or infeasible seed before search begins.
    try:
        request = freeze_film_synthesis_request(
            None if request.frozen_material_snapshot is not None else service, request)
    except (KeyError, TypeError, ValueError) as exc:
        raise FilmSynthesisPayloadError(f"strict synthesis validation failed: {exc}") from exc
    return film_synthesis_request_to_payload(request_id, request), tuple(metadata)


def film_synthesis_request_to_payload(request_id: str, request: FilmSynthesisRequest,
                                     physical_structures=()) -> dict[str, Any]:
    """Serialize a frozen core request and canonical physical-structure specs."""
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise FilmSynthesisPayloadError("request_id must be a nonempty string of at most 128 characters")
    request = _validated(request)
    if request.frozen_material_snapshot is None:
        raise FilmSynthesisPayloadError("only frozen requests can be serialized for execution")
    return {"schema_version": SCHEMA_VERSION, "kind": REQUEST_KIND,
            "request_id": request_id, "request": asdict(request),
            "physical_structures": list(_normalize_specs(physical_structures))}


def film_synthesis_result_to_payload(request_id: str, result: Any, *, audit: Mapping[str, Any] | None = None,
                                     physical_structure_metadata=()) -> dict[str, Any]:
    """Encode results with review evidence, material provenance and limits."""
    body = asdict(result)
    frozen = result.frozen_request
    if result.candidate_design is not None:
        body["candidate_design"] = annotate_synthesis_design(frozen, result.candidate_design)
    snapshot = frozen.frozen_material_snapshot or {}
    materials = {item.get("id"): item for item in snapshot.get("materials", ())}
    layers = []
    if result.candidate_design is not None:
        for index, layer in enumerate(result.candidate_design.get("front_layers", ())):
            material = materials.get(layer.get("material_id"), {})
            optical: Mapping[str, Any] = next((row for row in material.get("optical_sets", ())
                            if row.get("id") == layer.get("optical_set_id")), {})
            n_unit = optical.get("wavelength_unit")
            factor = 1000.0 * UNITS_TO_UM[n_unit] if n_unit in UNITS_TO_UM else None
            n_domain_nm = ([cast(float, optical.get("wavelength_min")) * factor,
                            cast(float, optical.get("wavelength_max")) * factor]
                           if factor is not None else None)
            k_data = optical.get("k_data")
            if optical.get("kind") == "tabulated" and optical.get("k") is not None:
                k_unit = n_unit
                k_domain = n_domain_nm
                k_source = optical.get("source")
                k_notes = optical.get("notes")
            elif isinstance(k_data, Mapping):
                k_unit = k_data.get("wavelength_unit")
                k_factor = 1000.0 * UNITS_TO_UM[k_unit] if k_unit in UNITS_TO_UM else None
                k_domain = ([cast(float, k_data.get("wavelength_min")) * k_factor,
                             cast(float, k_data.get("wavelength_max")) * k_factor]
                            if k_factor is not None else None)
                k_source, k_notes = k_data.get("source"), k_data.get("notes")
            else:
                k_unit, k_domain, k_source, k_notes = None, None, None, None
            layers.append({"index": index, "id": layer.get("id"),
                "thickness_nm": layer.get("thickness", {}).get("value"),
                "material_id": layer.get("material_id"), "material_name": material.get("name"),
                "optical_set_id": layer.get("optical_set_id"), "optical_set_name": optical.get("name"),
                "n_wavelength_domain_nm": n_domain_nm, "k_wavelength_domain_nm": k_domain,
                "n_wavelength_domain_original": {"min": optical.get("wavelength_min"),
                    "max": optical.get("wavelength_max"), "unit": n_unit},
                "k_wavelength_domain_original": ({"min": k_data.get("wavelength_min"),
                    "max": k_data.get("wavelength_max"), "unit": k_unit} if isinstance(k_data, Mapping) else
                    {"min": optical.get("wavelength_min"), "max": optical.get("wavelength_max"), "unit": k_unit}
                    if k_unit else None),
                "material_source": {"n_source": optical.get("source"), "n_notes": optical.get("notes"),
                    "k_source": k_source, "k_notes": k_notes,
                    "source_url": optical.get("source_url"), "source_hash": optical.get("source_hash"),
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "snapshot_sha256": snapshot.get("sha256"),
                    "source_content_sha256": snapshot.get("source_content_sha256"),
                    "snapshot_source_url": snapshot.get("source_url"),
                    "upstream_commit": snapshot.get("upstream_commit")}})
    validation = result.validation_score or {}
    training = result.training_score or {}
    audit_metadata = validation.get("audit") or {}
    absorption = synthesis_absorption_assumption(frozen)
    used_absorption = (synthesis_absorption_assumption(frozen, result.candidate_design)
                       if result.candidate_design is not None else None)
    absorption["used_affected_materials"] = (used_absorption["affected_materials"]
                                           if used_absorption is not None else [])
    absorption["conditional_on_assumption"] = bool(absorption["used_affected_materials"])
    absorption["claim"] = (used_absorption["claim"] if used_absorption is not None else
                           "No candidate absorption claim is available; pool assumptions describe inputs only.")
    def finite_score(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    training_max = finite_score(training.get("max_error"))
    audit_max = finite_score(validation.get("max_error"))
    epsilon = finite_score(frozen.acceptable_normalized_error)
    audit_available = (result.candidate_design is not None and result.validation_complete
                       and audit_max is not None)
    physical_targets_met = (training_max <= epsilon and audit_max <= epsilon
                            if audit_available and training_max is not None and epsilon is not None
                            else None)
    best_candidate_audit = {
        "available": audit_available,
        "targets_met": physical_targets_met,
        "training_max_normalized_error": training_max,
        "audit_max_normalized_error": audit_max if audit_available else None,
        "acceptable_normalized_error": epsilon,
        "continuous_certificate": audit_metadata.get("continuous_certificate") is True,
        "absorption_assumption": deepcopy(absorption),
    }
    warnings = []
    if absorption["used_affected_materials"]:
        warnings.append("Unknown source absorption was explicitly assumed k=0; target status is conditional sampled-model evidence, not measured absorption validation.")
    if not result.validation_complete:
        warnings.append("No complete independent physical audit is available for this candidate.")
    if result.state != "completed":
        warnings.append("The synthesis task did not complete successfully; inspect best_candidate_audit for candidate quality.")
    elif physical_targets_met is False:
        warnings.append("The best independently audited candidate does not meet every acceptance threshold.")
    if audit_metadata and audit_metadata.get("continuous_certificate") is False:
        warnings.append("The audit samples the stated domain and is not a continuous-spectrum certificate.")
    objectives = tuple(frozen.objectives)
    per_objective = deepcopy(audit_metadata.get("objective_status", []))
    if (not audit_available and result.candidate_design is not None
            and result.candidate_provenance.get("selection") == "completed_training_at_cancel"):
        # Present saved training evidence without claiming an independent audit.
        saved_errors = body.get("training_score", {}).get("errors", [])
        for index, objective in enumerate(objectives):
            conditions = [row for row in saved_errors if row["objective_index"] == index]
            if not conditions:
                continue
            worst = max(conditions, key=lambda row: row["normalized_violation"])
            per_objective.append({"objective_index": index, "metric": objective.metric,
                "wavelength_min_nm": objective.wavelength_min_nm,
                "wavelength_max_nm": objective.wavelength_max_nm,
                "discrete_line": objective.wavelength_min_nm == objective.wavelength_max_nm,
                "score_scope": "training", "audit_available": False, "targets_met": None,
                "worst_wavelength_nm": worst["wavelength_nm"], "worst_angle_deg": worst["angle_deg"],
                "worst_actual": worst["actual"], "max_normalized_error": None,
                "training_max_normalized_error": worst["normalized_violation"],
                "training_targets_met": worst["normalized_violation"] <= frozen.acceptable_normalized_error})
    review = {"absorption_assumption": deepcopy(absorption), "termination": {"state": result.state, "reason": result.termination_reason},
        "targets_met": result.targets_met,
        "per_objective": per_objective,
        "candidate_provenance": deepcopy(dict(result.candidate_provenance)),
        "audit_domain": {"objective_windows": [
            {"objective_index": index, "wavelength_min_nm": o.wavelength_min_nm,
             "wavelength_max_nm": o.wavelength_max_nm,
             "discrete_line": o.wavelength_min_nm == o.wavelength_max_nm}
            for index, o in enumerate(objectives)],
            "outer_extent_nm_for_reference_only": [min(o.wavelength_min_nm for o in objectives),
                max(o.wavelength_max_nm for o in objectives)],
            "continuous_span_claimed": False, "metadata": deepcopy(audit_metadata)},
        "limits_nm_and_counts": {"minimum_nonzero_thickness_nm": frozen.minimum_nonzero_thickness_nm,
            "max_layer_thickness_nm": frozen.max_layer_thickness_nm,
            "max_total_thickness_nm": frozen.max_total_thickness_nm, "max_layers": frozen.max_layers,
            "max_cycles": frozen.max_cycles, "max_evaluations": frozen.max_evaluations},
        "cost": {"cycles": result.cycles, "evaluations": result.evaluations,
            "validation_evaluations": result.validation_evaluations,
            "failed_evaluations": result.failed_evaluations,
            "audit_physical_evaluations": result.audit_physical_evaluations,
            "audit_wavelength_points": result.audit_wavelength_points},
        "layers": layers, "warnings": warnings}
    return {"schema_version": SCHEMA_VERSION, "kind": RESULT_KIND, "request_id": request_id,
            "ok": result.state != "failed", "result": body, "review": review,
            "best_candidate_audit": best_candidate_audit,
            "absorption_assumption": absorption,
            "audit": deepcopy(dict(audit)) if audit is not None else None,
            "physical_structure_metadata": deepcopy(list(physical_structure_metadata))}
