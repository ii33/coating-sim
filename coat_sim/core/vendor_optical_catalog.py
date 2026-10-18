"""Offline vendor AGF reader; no downloads, database writes, or implied k data.

AGF formula numbers are *not* RII formula numbers. Only independently mapped
dispersion formulas 1, 2, 3, 6, 12 and 13 are converted. Vendor data remains untrusted text.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import stat
from typing import Any

import numpy as np
from numpy.polynomial import Polynomial

from .optical_constants import evaluate_optical_set, prepare_optical_set, validate_optical_set

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_LINE_CHARS = 65536
MAX_ENTRY_CHARS = 128 * 1024
ND_ADVISORY_TOLERANCE = 1e-4  # Project warning threshold, not a manufacturer specification.
AGF_FORMAT_REFERENCE = (
    "https://optics.ansys.com/hc/en-us/articles/"
    "46891219767827-Zemax-AGF-material-catalog-file-format"
)
FORMULA_REFERENCES = {
    1: "https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v261/en/"
       "OpticStudio_User_Guide/OpticStudio_Help/topics/The_Schott_Formula.html",
    2: "https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v261/en/"
       "OpticStudio_User_Guide/OpticStudio_Help/topics/The_Sellmeier_1_Formula.html",
}
_EXTENDED_REFERENCE_ROOT = (
    "https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v26103/en/"
    "OpticStudio_User_Guide/OpticStudio_Help/topics/"
)
FORMULA_REFERENCES.update({
    3: _EXTENDED_REFERENCE_ROOT + "The_Herzberger_Formula.html",
    6: _EXTENDED_REFERENCE_ROOT + "The_Sellmeier_3_Formula.html",
    12: _EXTENDED_REFERENCE_ROOT + "The_Extended_2_Formula.html",
    13: _EXTENDED_REFERENCE_ROOT + "The_Extended_3_Formula.html",
})
SUPPORTED_FORMULAS = (1, 2, 3, 6, 12, 13)
_COEFFICIENT_COUNTS = {1: 6, 2: 6, 3: 6, 6: 8, 12: 8, 13: 9}
_POLYNOMIAL_EXPONENTS = {
    1: (2, -2, -4, -6, -8),
    12: (2, -2, -4, -6, -8, 4, 6),
    13: (2, 4, -2, -4, -6, -8, -10, -12),
}
FORMULA_NAMES = {
    1: "Schott", 2: "Sellmeier 1", 3: "Herzberger", 4: "Sellmeier 2",
    5: "Conrady", 6: "Sellmeier 3", 7: "Handbook of Optics 1",
    8: "Handbook of Optics 2", 9: "Sellmeier 4", 10: "Extended",
    11: "Sellmeier 5", 12: "Extended 2", 13: "Extended 3",
}
_REFERENCE_WARNINGS = [
    "AGF n and wavelength use the source's relative-air reference convention; "
    "no absolute/vacuum-index or pressure correction is applied.",
    "TD reference temperature describes the reference air, not necessarily the "
    "glass measurement temperature. No thermo-optic correction is applied.",
    "IT is internal transmittance at its recorded thickness, not surface or "
    "coated-stack transmittance. No extinction coefficient is inferred; k is unknown.",
    "A bulk vendor glass is not a measured deposited-film optical model.",
    "Numerical positive-real domain screening and nd agreement are not a certificate "
    "of measurement accuracy, material suitability, or validity between all samples.",
]


class VendorCatalogError(ValueError):
    """Stable error code shared by future UI/CLI adapters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, label: str, maximum: int = 2000, *, required: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or "\0" in value
            or (required and not value.strip())):
        raise VendorCatalogError("INVALID_METADATA", f"Invalid {label}")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _numbers(tokens: list[str], label: str) -> list[float]:
    if len(tokens) > 100:
        raise ValueError(f"Too many numeric fields in {label}")
    try:
        values = [float(token.replace("D", "E").replace("d", "e")) for token in tokens]
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid numeric {label}") from exc
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"Non-finite numeric {label}")
    return values


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise VendorCatalogError("INVALID_QUERY", f"{label} must be an integer in [{low}, {high}]")
    return value


def vendor_format_capabilities() -> list[dict[str, Any]]:
    """Explicit extension boundary; PDF charts and proprietary files are not curves."""
    return [
        {"format": "agf", "readable": True, "supported_formula_numbers": list(SUPPORTED_FORMULAS)},
        {"format": "pdf", "readable": False, "reason": "REFERENCE_DOCUMENT_ONLY",
         "message": "Vendor PDFs (including Merck mixtures) require source-specific review; "
                    "no curve digitization or numerical values are inferred."},
        {"format": "bgf", "readable": False, "reason": "UNSUPPORTED_BINARY_FORMAT"},
    ]


def _parse_entry(raw: str, start_line: int, ordinal: int, source_hash: str) -> dict[str, Any]:
    fields: dict[str, list[dict[str, Any]]] = {}
    previous_code = ""
    for number, line in enumerate(raw.splitlines(), start_line):
        cells = line.lstrip("\ufeff").strip().split(maxsplit=1)
        if not cells or cells[0].startswith("!"):
            continue
        if previous_code in ("CD", "TD") and cells[0][0] in "+-.0123456789":
            fields[previous_code][-1].setdefault("continuation_lines", []).append(
                {"line": number, "value": line.strip()})
            continue
        code = cells[0].upper()
        value = cells[1] if len(cells) == 2 else ""
        fields.setdefault(code, []).append({"line": number, "value": value})
        previous_code = code
    entry: dict[str, Any] = {
        "entry_id": "agf_" + hashlib.sha256(f"{source_hash}:{ordinal}".encode()).hexdigest()[:24],
        "ordinal": ordinal, "source_line": start_line, "raw_entry": raw,
        "entry_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "fields": fields, "grade": "", "formula_number": None, "formula_name": None,
        "coefficients": None, "wavelength_range_um": None,
        "reference_temperature_c": None, "thermal_coefficients": None,
        "reference_index_medium": "relative_to_reference_air",
        "wavelength_reference": "relative_to_reference_air",
        "internal_transmittance": [], "reference_warnings": [], "errors": [],
        "data_quality": "not_checked",
        "conversion": {"status": "unsupported", "code": "UNSUPPORTED_FORMULA", "reason": ""},
    }

    def one(code: str, *, required: bool = True) -> list[str] | None:
        rows = fields.get(code, [])
        if not rows and not required:
            return None
        if len(rows) != 1:
            raise ValueError(f"Exactly one {code} line is required")
        tokens = rows[0]["value"].split()
        for continuation in rows[0].get("continuation_lines", []):
            tokens.extend(continuation["value"].split())
        return tokens

    try:
        nm = one("NM")
        assert nm is not None
        if len(nm) < 5:
            raise ValueError("NM needs grade, formula number, MIL number, nd and vd")
        entry["grade"] = nm[0]
        formula = int(nm[1])
        if formula < 1 or str(formula) != nm[1]:
            raise ValueError("NM dispersion formula must be a positive integer")
        entry["formula_number"] = formula
        entry["formula_name"] = FORMULA_NAMES.get(formula, f"Unknown AGF {formula}")
        nd, vd = _numbers(nm[3:5], "NM nd/vd")
        entry["nm"] = {"mil_number": nm[2], "nd": nd, "vd": vd,
                       "extra_tokens": nm[5:]}
    except (ValueError, OverflowError) as exc:
        entry["errors"].append(str(exc))
        nm_rows = fields.get("NM", [])
        entry["grade"] = (nm_rows[0]["value"].split() or ["Unnamed"])[0] if nm_rows else "Unnamed"

    for code, key in (("CD", "coefficients"), ("LD", "wavelength_range_um"),
                      ("TD", "thermal_coefficients")):
        try:
            tokens = one(code, required=code != "TD")
            if tokens is None or (code == "TD" and not tokens):
                entry["reference_warnings"].append("Missing/empty TD: reference temperature is unknown.")
                continue
            values = _numbers(tokens, code)
            if code == "CD" and not 1 <= len(values) <= 10:
                raise ValueError("CD requires 1–10 coefficients")
            if code == "LD" and (len(values) != 2 or not 0 < values[0] < values[1]):
                raise ValueError("LD needs positive increasing wavelength bounds in micrometres")
            if code == "TD":
                if len(values) != 7 or values[6] <= -273.15:
                    raise ValueError("TD needs six coefficients and a physical reference temperature")
                entry["reference_temperature_c"] = values[6]
                entry[key] = dict(zip(("D0", "D1", "D2", "E0", "E1", "Ltk"), values[:6], strict=True))
            else:
                entry[key] = values
        except ValueError as exc:
            entry["errors"].append(str(exc))

    previous_wave = 0.0
    for row in fields.get("IT", []):
        point: dict[str, Any] = {"source_line": row["line"], "raw_value": row["value"],
                                "status": "invalid"}
        try:
            values = _numbers(row["value"].split(), "IT")
            if len(values) != 3:
                raise ValueError("IT needs wavelength_um, internal_transmittance and thickness_mm")
            wave, transmission, thickness = values
            point.update(wavelength_um=wave, internal_transmittance=transmission,
                         thickness_mm=thickness)
            if wave <= 0 or not 0 <= transmission <= 1 or thickness <= 0:
                raise ValueError("IT requires positive wavelength/thickness and transmission in [0, 1]")
            if wave <= previous_wave:
                raise ValueError("IT wavelengths must increase in source order")
            previous_wave = wave
            point["status"] = "reference_only"
        except ValueError as exc:
            point["reason"] = str(exc)
            entry["reference_warnings"].append(f"IT line {row['line']}: {exc}")
        entry["internal_transmittance"].append(point)
    if len(entry["internal_transmittance"]) > 100:
        entry["reference_warnings"].append("IT exceeds the AGF 100-point convention; preserved only.")
    return entry


def _check_domain(entry: dict[str, Any], record: dict[str, Any]) -> None:
    """Screen stationary points and a fixed interior grid independently of preview.

    The derivative numerator is a polynomial in x=lambda_um**2. Root finding is
    numerical, not a formal positivity proof or a vendor accuracy guarantee.
    """
    formula, c = entry["formula_number"], entry["coefficients"]
    low, high = record["wavelength_min"], record["wavelength_max"]
    x = Polynomial([0., 1.])
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            if formula in _POLYNOMIAL_EXPONENTS:
                derivative_terms = [(float(exponent // 2 - 1), amplitude * (exponent // 2))
                                    for amplitude, exponent in zip(
                                        c[1:_COEFFICIENT_COUNTS[formula]],
                                        _POLYNOMIAL_EXPONENTS[formula], strict=True)
                                    if amplitude != 0]
                shift = int(-min([0., *(power for power, _ in derivative_terms)]))
                coefficients = [0.] * (1 + max([0, *(int(power) + shift for power, _ in derivative_terms)]))
                for power, amplitude in derivative_terms:
                    coefficients[int(power) + shift] += amplitude
                derivative = Polynomial(coefficients)
            elif formula in (2, 6):
                pairs = [(c[i], c[i+1]) for i in range(0, _COEFFICIENT_COUNTS[formula], 2)
                         if c[i] != 0 and c[i+1] != 0]
                derivative = Polynomial([0.])
                for i, (amplitude, resonance) in enumerate(pairs):
                    term = Polynomial([-amplitude * resonance])
                    for j, (_, other_pole) in enumerate(pairs):
                        if j != i:
                            term *= (x - other_pole)**2
                    derivative += term
            else:
                _, b, cc, d, e, f = c[:6]
                derivative = -b*(x - .028) - 2*cc + Polynomial([d, 2*e, 3*f])*(x - .028)**3
            # Rescale x around the geometric midpoint for the root solver.
            center = low * high
            if not math.isfinite(center) or center <= 0:
                raise ValueError("Cannot safely scale the source wavelength domain")
            derivative = derivative(Polynomial([0., center]))
            scale = float(np.max(np.abs(derivative.coef)))
            roots: Any = (derivative / scale).roots() if scale else []
            if not np.all(np.isfinite(roots)):
                raise ValueError("Non-finite stationary points in dispersion domain check")
            checkpoints = [low, high]
            for root in roots:
                real, imaginary = float(np.real(root)), float(np.imag(root))
                if real > 0 and abs(imaginary) <= 1e-7 * max(1., abs(real)):
                    wave = math.sqrt(real * center)
                    if low < wave < high:
                        checkpoints.append(wave)
            # A source-wide grid is fixed, never chosen by the preview caller.
            checkpoints.extend(math.exp(value) for value in np.linspace(math.log(low), math.log(high), 65)[1:-1])
            evaluate = prepare_optical_set(record)
            for wave in checkpoints:
                evaluate(wave, "um")
    except (ValueError, OverflowError, FloatingPointError, np.linalg.LinAlgError) as exc:
        raise VendorCatalogError("OPTICAL_DOMAIN_INVALID", "Dispersion is not safely positive/finite "
                                 f"over its declared LD range: {exc}") from exc


def _candidate(entry: dict[str, Any], identifier: str, name: str) -> dict[str, Any]:
    formula = entry["formula_number"]
    if entry["errors"]:
        raise VendorCatalogError("INVALID_AGF_RECORD", "; ".join(entry["errors"]))
    if formula not in SUPPORTED_FORMULAS:
        raise VendorCatalogError("UNSUPPORTED_FORMULA", f"AGF formula {formula} is reference-only; "
                                 f"mapped AGF formulas are {SUPPORTED_FORMULAS}")
    c = entry["coefficients"]
    expected_count = _COEFFICIENT_COUNTS[formula]
    if len(c) < expected_count or any(value != 0 for value in c[expected_count:]):
        raise VendorCatalogError("UNSUPPORTED_COEFFICIENT_LAYOUT",
                                 f"AGF {formula} needs {expected_count} coefficients, "
                                 "optionally followed by zero padding")
    c = c[:expected_count]
    if formula in _POLYNOMIAL_EXPONENTS:
        mapped = [c[0]]
        for amplitude, exponent in zip(c[1:], _POLYNOMIAL_EXPONENTS[formula], strict=True):
            mapped.extend((amplitude, float(exponent)))
        rii_formula = 3
    elif formula in (2, 6):
        mapped, rii_formula = [0.0, *c], 2
    else:  # AGF 3 and RII 7 use the same six Herzberger coefficients and L.
        mapped, rii_formula = c, 7
    low, high = entry["wavelength_range_um"]
    try:
        record = validate_optical_set({
            "id": identifier, "name": name, "kind": "rii_formula",
            "formula_number": rii_formula, "coefficients": mapped,
            "wavelength_unit": "um", "wavelength_min": low, "wavelength_max": high,
        })
        _check_domain(entry, record)
        return record
    except VendorCatalogError:
        raise
    except (ValueError, OverflowError) as exc:
        raise VendorCatalogError("OPTICAL_VALIDATION_FAILED", str(exc)) from exc


class VendorOpticalCatalog:
    """In-memory, immutable-by-copy catalog snapshot loaded from a selected local file.

    Instances are returned by ``load_vendor_catalog`` / ``parse_agf``. No method
    fetches a URL or changes a file, material, source, or active optical set.
    """

    def __init__(self, metadata: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        self._metadata = deepcopy(metadata)
        self._entries = deepcopy(entries)
        self._by_id = {entry["entry_id"]: entry for entry in self._entries}
        for entry in self._entries:
            try:
                record = _candidate(entry, entry["entry_id"], entry["grade"][:120])
                if record["wavelength_min"] <= .5875618 <= record["wavelength_max"]:
                    calculated_nd, _ = evaluate_optical_set(record, .5875618, "um")
                    delta = calculated_nd - entry["nm"]["nd"]
                    entry["data_quality"] = ("source_inconsistent" if abs(delta) > ND_ADVISORY_TOLERANCE
                                             else "no_inconsistency_detected")
                    entry["nd_consistency"] = {
                        "wavelength_um": .5875618, "source_nm_nd": entry["nm"]["nd"],
                        "calculated_nd": calculated_nd, "difference": delta,
                        "advisory_tolerance": ND_ADVISORY_TOLERANCE,
                        "policy": "project_advisory_only_not_vendor_specification",
                        "status": "warning" if abs(delta) > ND_ADVISORY_TOLERANCE else "within_tolerance",
                    }
                    if abs(delta) > ND_ADVISORY_TOLERANCE:
                        entry["reference_warnings"].append(
                            f"NM nd differs from CD dispersion at 0.5875618 um by {delta:.8g}; "
                            "source values are preserved, not reconciled. Review the vendor source.")
                entry["conversion"] = {
                    "status": "supported", "code": None, "reason": "n only; k remains unknown",
                    "target_kind": "rii_formula", "target_formula_number": record["formula_number"],
                    "formula_reference": FORMULA_REFERENCES[entry["formula_number"]],
                    "requires_reference_air_approximation": True,
                    "domain_screening": "stationary_points_plus_65_log_wavelength_samples",

                }
            except (VendorCatalogError, ValueError, OverflowError) as exc:
                code = exc.code if isinstance(exc, VendorCatalogError) else "OPTICAL_EVALUATION_FAILED"
                entry["conversion"] = {
                    "status": "unsupported" if code.startswith("UNSUPPORTED") else "invalid",
                    "code": code, "reason": str(exc),
                }

    def status(self) -> dict[str, Any]:
        return {**deepcopy(self._metadata), "entry_count": len(self._entries),
                "conversion_counts": dict(Counter(e["conversion"]["status"] for e in self._entries)),
                "formula_counts": dict(Counter(str(e["formula_number"]) for e in self._entries)),
                "read_only": True}

    def search(self, query: str = "", *, limit: int = 100, offset: int = 0,
               capability: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > 200:
            raise VendorCatalogError("INVALID_QUERY", "query must be at most 200 characters")
        limit = _integer(limit, 1, 1000, "limit")
        offset = _integer(offset, 0, MAX_ENTRIES, "offset")
        if capability not in (None, "supported", "unsupported", "invalid"):
            raise VendorCatalogError("INVALID_QUERY", "Unknown capability filter")
        needle = query.casefold()
        matches = [e for e in self._entries if
                   (capability is None or e["conversion"]["status"] == capability)
                   and (needle in e["grade"].casefold() or
                        any(needle in row["value"].casefold() for row in e["fields"].get("GC", [])))]
        keys = ("entry_id", "grade", "formula_number", "formula_name", "wavelength_range_um",
                "reference_temperature_c", "data_quality", "reference_warnings", "conversion")
        return {"source_sha256": self._metadata["source_sha256"], "total": len(matches),
                "offset": offset, "entries": [deepcopy({k: e[k] for k in keys})
                                                for e in matches[offset:offset + limit]]}

    def _entry(self, entry_id: str) -> dict[str, Any]:
        if not isinstance(entry_id, str) or entry_id not in self._by_id:
            raise VendorCatalogError("ENTRY_NOT_FOUND", "Entry is not in this catalog snapshot")
        return self._by_id[entry_id]

    def detail(self, entry_id: str) -> dict[str, Any]:
        return {"catalog": deepcopy(self._metadata), "entry": deepcopy(self._entry(entry_id)),
                "warnings": list(_REFERENCE_WARNINGS), "untrusted_reference": True}

    def _temperature(self, entry: dict[str, Any], temperature_c: float | None) -> None:
        if temperature_c is None:
            return
        if (isinstance(temperature_c, bool) or not isinstance(temperature_c, (int, float))
                or not math.isfinite(temperature_c)):
            raise VendorCatalogError("INVALID_QUERY", "temperature_c must be a finite number")
        if temperature_c != entry["reference_temperature_c"]:
            raise VendorCatalogError("UNSUPPORTED_TEMPERATURE",
                                     "Only the recorded reference temperature is supported; no TD correction")

    def prepare_import(self, entry_id: str, *, optical_set_id: str | None = None,
                       name: str | None = None, temperature_c: float | None = None,
                       allow_reference_air_approximation: bool = False) -> dict[str, Any]:
        """Return a detached candidate plus provenance; never import or select it.

        Provenance, including raw entry text, is also retained in the standard
        optical_set notes, so existing material persistence does not discard it.
        """
        entry = self._entry(entry_id)
        if entry["conversion"]["status"] != "supported":
            raise VendorCatalogError(entry["conversion"]["code"], entry["conversion"]["reason"])
        self._temperature(entry, temperature_c)
        candidate = _candidate(entry, optical_set_id if optical_set_id is not None else entry_id,
                               name if name is not None else entry["grade"][:120])
        if type(allow_reference_air_approximation) is not bool:
            raise VendorCatalogError("INVALID_QUERY", "allow_reference_air_approximation must be boolean")
        if not allow_reference_air_approximation:
            raise VendorCatalogError("REFERENCE_FRAME_CONFIRMATION_REQUIRED",
                                     "AGF n and wavelength are relative to reference air; the film model "
                                     "uses absolute n and vacuum wavelength. Explicitly accept the "
                                     "uncorrected reference-air approximation before preparing an import.")
        candidate["process_parameters"] = [
            {"name": "vendor_reference_frame", "value": "relative n and wavelength in reference air", "unit": ""},
            {"name": "reference_air_approximation", "value": "explicitly accepted; source values uncorrected", "unit": ""},
        ]
        provenance = {**deepcopy(self._metadata), "entry_id": entry_id,
                      "grade": entry["grade"], "source_line": entry["source_line"],
                      "entry_sha256": entry["entry_sha256"], "raw_entry": entry["raw_entry"],
                      "agf_formula_number": entry["formula_number"],
                      "formula_reference": FORMULA_REFERENCES[entry["formula_number"]],
                    "requires_reference_air_approximation": True,
                    "domain_screening": "stationary_points_plus_65_log_wavelength_samples",

                      "reference_temperature_c": entry["reference_temperature_c"],
                      "reference_index_medium": entry["reference_index_medium"],
                      "wavelength_reference": entry["wavelength_reference"],
                      "k_status": "unknown", "internal_transmittance_policy": "reference_only",
                      "reference_air_approximation": True,
                      "reference_frame_policy": "explicit_user_choice_uncorrected_air_as_vacuum_approximation",
                      "nd_consistency": deepcopy(entry.get("nd_consistency")),
                      "data_quality": entry["data_quality"],
                      "warnings": [*_REFERENCE_WARNINGS, *entry["reference_warnings"]]}
        candidate["source"] = (f"{self._metadata['manufacturer']} | {self._metadata['catalog_version']} | "
                               f"{self._metadata['source_url']} | sha256:{self._metadata['source_sha256']}")
        candidate["notes"] = "Vendor AGF provenance (untrusted source text):\n" + _json(provenance)
        if len(candidate["notes"]) > 20000:
            raise VendorCatalogError("PROVENANCE_TOO_LARGE", "Raw provenance exceeds optical_set notes limit; "
                                     "keep this entry reference-only rather than truncating it")
        try:
            candidate = validate_optical_set(candidate)
        except (ValueError, OverflowError) as exc:
            raise VendorCatalogError("OPTICAL_VALIDATION_FAILED", str(exc)) from exc
        return {"optical_set": candidate, "provenance": provenance, "k": None,
                "writes_material_library": False, "changes_active_selection": False}

    def preview(self, entry_id: str, *, max_points: int = 200,
                temperature_c: float | None = None) -> dict[str, Any]:
        max_points = _integer(max_points, 2, 1000, "max_points")
        entry = self._entry(entry_id)
        if entry["conversion"]["status"] != "supported":
            raise VendorCatalogError(entry["conversion"]["code"], entry["conversion"]["reason"])
        self._temperature(entry, temperature_c)
        candidate = _candidate(entry, entry_id, entry["grade"][:120])
        low, high = candidate["wavelength_min"], candidate["wavelength_max"]
        waves = [low + (high - low) * i / (max_points - 1) for i in range(max_points)]
        waves[0], waves[-1] = low, high
        try:
            points = [[w, *evaluate_optical_set(candidate, w, "um")] for w in waves]
        except (ValueError, OverflowError) as exc:
            raise VendorCatalogError("OPTICAL_EVALUATION_FAILED", str(exc)) from exc
        return {"entry_id": entry_id, "source_sha256": self._metadata["source_sha256"],
                "columns": ["wavelength_um", "n", "k"], "points": points,
                "calculated": True, "sampling_policy": "linear_wavelength_for_display_only",
                "data_quality": entry["data_quality"],
                "reference_temperature_c": entry["reference_temperature_c"],
                "reference_index_medium": entry["reference_index_medium"],
                "wavelength_reference": entry["wavelength_reference"],
                "internal_transmittance": deepcopy(entry["internal_transmittance"]),
                "warnings": [*_REFERENCE_WARNINGS, *entry["reference_warnings"]]}


def parse_agf(text: str, *, manufacturer: str, catalog_version: str,
              source_url: str = "", license_note: str = "",
              source_filename: str = "") -> VendorOpticalCatalog:
    """Parse UTF-8 text without guessing a manufacturer, version, units or k."""
    if not isinstance(text, str):
        raise VendorCatalogError("INVALID_AGF", "AGF input must be text")
    try:
        data = text.encode("utf-8")
    except UnicodeError as exc:
        raise VendorCatalogError("INVALID_ENCODING", "AGF text is not valid Unicode") from exc
    return _parse(data, text, encoding="utf-8", manufacturer=manufacturer,
                  catalog_version=catalog_version, source_url=source_url,
                  license_note=license_note, source_filename=source_filename)


def _parse(data: bytes, text: str, *, encoding: str, manufacturer: str,
           catalog_version: str, source_url: str, license_note: str,
           source_filename: str) -> VendorOpticalCatalog:
    if len(data) > MAX_FILE_BYTES:
        raise VendorCatalogError("SOURCE_LIMIT", "AGF source exceeds 16 MiB")
    if "\0" in text:
        raise VendorCatalogError("INVALID_AGF", "AGF source contains binary/NUL content")
    metadata: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "format": "agf",
                "manufacturer": _text(manufacturer, "manufacturer", 200, required=True),
                "catalog_version": _text(catalog_version, "catalog_version", 200, required=True),
                "source_url": _text(source_url, "source_url"),
                "license_note": _text(license_note, "license_note", 4000),
                "source_filename": _text(source_filename, "source_filename", 1000),
                "source_sha256": hashlib.sha256(data).hexdigest(), "source_bytes": len(data),
                "encoding": encoding, "format_reference": AGF_FORMAT_REFERENCE,
                "metadata_origin": "caller_supplied_not_independently_verified"}
    lines = text.splitlines(keepends=True)
    if any(len(line) > MAX_LINE_CHARS for line in lines):
        raise VendorCatalogError("SOURCE_LIMIT", "AGF line exceeds 65536 characters")
    starts = [i for i, line in enumerate(lines) if line.lstrip("\ufeff").strip().split(maxsplit=1)
              and line.lstrip("\ufeff").strip().split(maxsplit=1)[0].upper() == "NM"]
    if not starts:
        raise VendorCatalogError("INVALID_AGF", "AGF contains no NM material records")
    if len(starts) > MAX_ENTRIES:
        raise VendorCatalogError("SOURCE_LIMIT", "AGF exceeds 10000 entries")
    metadata["raw_header"] = "".join(lines[:starts[0]])
    metadata["catalog_comments"] = [line.strip()[2:].strip() for line in lines[:starts[0]]
                                    if line.strip().upper().startswith("CC ")]
    entries = []
    for ordinal, (start, end) in enumerate(zip(starts, [*starts[1:], len(lines)], strict=True)):
        raw = "".join(lines[start:end])
        if len(raw) > MAX_ENTRY_CHARS:
            raise VendorCatalogError("SOURCE_LIMIT", "An AGF entry exceeds 128 Ki characters")
        entries.append(_parse_entry(raw, start + 1, ordinal, metadata["source_sha256"]))
    return VendorOpticalCatalog(metadata, entries)


def load_vendor_catalog(path: str | Path, *, manufacturer: str, catalog_version: str,
                        source_url: str = "", license_note: str = "",
                        source_format: str | None = None,
                        encoding: str | None = None) -> VendorOpticalCatalog:
    """Read one explicitly selected local AGF, never a software install or URL.

    UTF-8 and BOM-marked UTF-16 are detected. Legacy ANSI decoding requires an
    explicit ``encoding='cp1252'`` or ``'latin-1'`` choice to avoid guessing text.
    """
    if not isinstance(path, (str, Path)) or "\0" in str(path):
        raise VendorCatalogError("INVALID_SOURCE", "Select a local AGF file path")
    source = Path(path)
    chosen_format = source.suffix.lstrip(".").lower() if source_format is None else source_format
    if chosen_format != "agf":
        raise VendorCatalogError("UNSUPPORTED_FORMAT", f"Format {chosen_format!r} is reference-only; "
                                 "select a local AGF, not a PDF, BGF, archive, or executable")
    if encoding not in (None, "utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"):
        raise VendorCatalogError("INVALID_ENCODING", "Unsupported explicit text encoding")
    try:
        if not stat.S_ISREG(source.stat().st_mode):
            raise VendorCatalogError("INVALID_SOURCE", "Select a regular local AGF file")
        if source.stat().st_size > MAX_FILE_BYTES:
            raise VendorCatalogError("SOURCE_LIMIT", "AGF source exceeds 16 MiB")
        with source.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise VendorCatalogError("SOURCE_READ_FAILED", "Cannot read selected local AGF") from exc
    if len(data) > MAX_FILE_BYTES:
        raise VendorCatalogError("SOURCE_LIMIT", "AGF source grew beyond 16 MiB")
    codec = encoding or ("utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig")
    try:
        text = data.decode(codec)
    except UnicodeError as exc:
        raise VendorCatalogError("INVALID_ENCODING", "Cannot decode AGF; select its documented encoding") from exc
    return _parse(data, text, encoding=codec, manufacturer=manufacturer,
                  catalog_version=catalog_version, source_url=source_url,
                  license_note=license_note, source_filename=source.name)
