"""Evidence gate for optical datasets authored by the assistant.

This is separate from the general material schema: a user's sparse measurements
remain legitimate. A successful result checks the supplied evidence structure;
it does not claim to have independently downloaded the cited publication.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from urllib.parse import urlparse

from coat_sim.core.optical_constants import validate_optical_sets


def _source_link(value: str) -> bool:
    for match in re.finditer(r"https?://[^\s<>()]+", value):
        parsed = urlparse(match.group())
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return True
    return bool(re.search(r"\bdoi:\s*10\.\d{4,9}/\S+", value, re.I))


def validate_assistant_material_quality(
    profile: Mapping,
    *,
    origin: str,
    evidence: Mapping[str, Mapping] | None = None,
) -> dict:
    """Assess assistant creation before persistence, without modifying *profile*.

    ``origin`` is ``assistant_reference`` or ``user_measurement``. Evidence is
    keyed by optical set ID. For a reference table, ``reference_row_count`` is
    the total row count of the cited original table, not an interpolation
    target. ``k_status`` is ``measured`` or ``unknown``. The caller must supply
    the cited source and acquisition/measurement conditions in each set's
    persisted ``source`` and ``notes`` or ``process_parameters`` respectively.
    ``evidence[set_id].conditions`` must name those persisted conditions.
    """
    if origin not in ("assistant_reference", "user_measurement"):
        raise ValueError("origin must be assistant_reference or user_measurement")
    if not isinstance(profile, Mapping):
        raise ValueError("profile must be an object")
    records = validate_optical_sets(profile.get("optical_constants"))
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be an object keyed by optical set ID")
    errors: list[str] = []
    summaries: list[dict] = []
    if not records:
        errors.append("optical_constants: no optical dataset was supplied")
    for record in records:
        identity = record["id"]
        item = evidence.get(identity, {})
        if not isinstance(item, Mapping):
            errors.append(f"{identity}: evidence must be an object")
            continue
        kind = record["kind"]
        k_is_present = record.get("k") is not None if kind == "tabulated" else record.get("k_data") is not None
        k_status = item.get("k_status")
        if k_status not in ("measured", "unknown"):
            errors.append(f"{identity}: explicit k_status measured or unknown is required")
        elif (k_status == "measured") != k_is_present:
            errors.append(f"{identity}: k_status conflicts with supplied k data; unknown k must stay null")
        if k_is_present and kind != "tabulated" and not record["k_data"]["source"].strip():
            errors.append(f"{identity}: independent k data requires its own source")
        if origin == "assistant_reference":
            if not _source_link(record["source"]):
                errors.append(f"{identity}: a traceable source URL or DOI is required")
            conditions = item.get("conditions")
            persisted_conditions = " ".join(
                [record["notes"]] + [str(part.get("value", "")) for part in record.get("process_parameters", [])]
            )
            if (not isinstance(conditions, str) or not conditions.strip()
                    or conditions.strip().casefold() not in persisted_conditions.casefold()):
                errors.append(f"{identity}: source material state or measurement conditions are required")
            if kind == "tabulated":
                count = item.get("reference_row_count")
                if type(count) is not int or count < 2:
                    errors.append(f"{identity}: original reference table row count is required")
                elif count != len(record["wavelengths"]):
                    errors.append(f"{identity}: imported table has {len(record['wavelengths'])} rows, source table has {count}")
        summaries.append({
            "id": identity,
            "kind": kind,
            "wavelength_unit": record["wavelength_unit"],
            "wavelength_min": record["wavelength_min"],
            "wavelength_max": record["wavelength_max"],
            "rows": len(record["wavelengths"]) if kind == "tabulated" else None,
            "k_status": k_status,
            "data_character": "user_sparse_measurement" if origin == "user_measurement" and kind == "tabulated"
                              else "reference_table" if kind == "tabulated" else "dispersion_formula",
        })
    return {"valid": not errors, "errors": errors, "datasets": summaries,
            "source_verified_externally": False}
