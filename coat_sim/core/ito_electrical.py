"""Read-only uniform-film electrical algebra. See docs/ito-electrical-contract.md."""
from __future__ import annotations

import math
from collections.abc import Mapping

_LENGTH = {"m": 1., "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "nm": 1e-9}
_UNITS = {"thickness": _LENGTH, "length": _LENGTH, "width": _LENGTH,
          "sheet_resistance": {"ohm/sq": 1.}, "resistivity": {"ohm*m": 1., "ohm*cm": 1e-2},
          "resistance": {"ohm": 1.}}
_CANONICAL = {"thickness": ("nm", 1e-9, "m"), "sheet_resistance": ("ohm/sq", 1., "ohm"),
              "resistivity": ("ohm*cm", 1e-2, "ohm*m")}
_SOURCES = ["https://www.nist.gov/pml/nanoscale-device-characterization-division/popular-links/hall-effect/hall-effect",
            "https://www.nist.gov/pml/nist-technical-note-1297/nist-tn-1297-appendix-law-propagation-uncertainty"]


def _number(value, label, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number) or number < 0 or (not zero and number == 0):
        raise ValueError(f"{label} must be finite and {'nonnegative' if zero else 'positive'}")
    return number


def _object(value, allowed, label):
    if not isinstance(value, Mapping) or set(value) - set(allowed):
        raise ValueError(f"{label} must be an object with only {sorted(allowed)}")
    return value


def _quantity(raw, name):
    raw = _object(raw, {"value", "unit", "provenance", "source", "standard_uncertainty"}, name)
    unit = raw.get("unit")
    if not isinstance(unit, str) or unit not in _UNITS[name]:
        raise ValueError(f"{name}.unit must be one of {list(_UNITS[name])}")
    provenance = raw.get("provenance")
    if provenance not in ("theoretical", "measured", "assumed"):
        raise ValueError(f"{name}.provenance must be theoretical, measured or assumed")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"{name}.source must be nonempty text")
    factor = _UNITS[name][unit]
    value = _number(_number(raw.get("value"), name) * factor, f"{name} SI value")
    uncertainty = None
    if "standard_uncertainty" in raw:
        original = _number(raw["standard_uncertainty"], f"{name}.standard_uncertainty", zero=True)
        uncertainty = _number(original * factor, f"{name} SI uncertainty", zero=original == 0)
    return {"si_value": value, "provenance": provenance, "source": source.strip(),
            "input_value": raw["value"], "input_unit": unit, "_u": uncertainty}


def _derived(value, dependencies, formula, uncertainty=None):
    return {"si_value": _number(value, "derived result"), "provenance": "derived",
            "source": formula, "dependencies": dependencies, "_u": uncertainty}


def _propagate(terms):
    # hypot avoids unnecessary square overflow, but representational overflow is invalid.
    return _number(math.hypot(*terms), "propagated uncertainty", zero=True)


def calculate_ito_electrical(payload: Mapping[str, object]) -> dict:
    """Solve rho=Rs*t or report missing data; never consult a material constant."""
    payload = _object(payload, {"operation", "thickness", "sheet_resistance", "resistivity",
        "rectangle", "uncertainty_assumption", "consistency_relative_tolerance"}, "request")
    if payload.get("operation", "solve") != "solve":
        raise ValueError("operation must be solve")
    assumption = payload.get("uncertainty_assumption")
    if assumption is not None and assumption != "independent":
        raise ValueError("uncertainty_assumption must be independent when supplied")
    tolerance = _number(payload.get("consistency_relative_tolerance", 1e-6), "tolerance", zero=True)
    if tolerance > 1:
        raise ValueError("consistency_relative_tolerance must be in [0,1]")
    quantities = {k: _quantity(payload[k], k) for k in _CANONICAL if k in payload}
    warnings = []
    geometry = None
    if "rectangle" in payload:
        if "sheet_resistance" in quantities:
            raise ValueError("rectangle and sheet_resistance cannot both supply sheet resistance")
        rectangle = _object(payload["rectangle"], {"resistance", "length", "width",
            "electrode_boundary", "contact_assumption"}, "rectangle")
        if rectangle.get("electrode_boundary") != "full_width_opposite_edges":
            raise ValueError("rectangle requires full_width_opposite_edges electrodes")
        if rectangle.get("contact_assumption") != "negligible_or_corrected":
            raise ValueError("rectangle requires negligible_or_corrected contact resistance")
        geometry = {k: _quantity(rectangle.get(k), k) for k in ("resistance", "length", "width")}
        r, length, width = [geometry[k]["si_value"] for k in geometry]
        rs = _number(r * (width / length), "rectangle sheet resistance")
        u = None
        if assumption == "independent" and all(q["_u"] is not None for q in geometry.values()):
            u = _propagate([rs * q["_u"] / q["si_value"] for q in geometry.values()])
        quantities["sheet_resistance"] = _derived(rs, list(geometry), "Rs = R * W / L", u)
        warnings.append("Rectangle result assumes uniform film, full-width opposite-edge electrodes and negligible or corrected lead/contact resistance; not a point-probe correction.")
    knowns = list(quantities)
    unknowns = [k for k in _CANONICAL if k not in quantities]
    status = "insufficient_data"
    consistency = None
    if len(quantities) == 2:
        missing = unknowns[0]
        if missing == "resistivity":
            a, b = quantities["sheet_resistance"], quantities["thickness"]
            value, formula = a["si_value"] * b["si_value"], "rho = Rs * t"
        else:
            denominator = "sheet_resistance" if missing == "thickness" else "thickness"
            a, b = quantities["resistivity"], quantities[denominator]
            value, formula = a["si_value"] / b["si_value"], "t = rho / Rs" if missing == "thickness" else "Rs = rho / t"
        value = _number(value, missing)
        u = None
        if assumption == "independent" and a["_u"] is not None and b["_u"] is not None:
            u = _propagate([value * a["_u"] / a["si_value"], value * b["_u"] / b["si_value"]])
        quantities[missing] = _derived(value, knowns.copy(), formula, u)
        status = "solved"
    elif len(quantities) == 3:
        t, rs, rho = [quantities[k]["si_value"] for k in _CANONICAL]
        predicted = _number(rs * t, "predicted resistivity")
        residual = rho - predicted
        relative = residual / max(rho, predicted)
        consistency = {"residual_ohm_m": residual, "relative_residual": relative,
                       "relative_tolerance": tolerance, "predicted_resistivity_ohm_m": predicted}
        if assumption == "independent" and all(q["_u"] is not None for q in quantities.values()):
            consistency["standard_uncertainty_ohm_m"] = _propagate([
                quantities["resistivity"]["_u"], rs * quantities["thickness"]["_u"],
                t * quantities["sheet_resistance"]["_u"]])
        status = "consistent" if abs(relative) <= tolerance else "inconsistent"
    all_inputs = list(quantities.values()) + (list(geometry.values()) if geometry else [])
    if any(q["_u"] is not None for q in all_inputs) and assumption != "independent":
        warnings.append("Input standard uncertainties retained; propagation requires explicit uncertainty_assumption='independent'.")
    if assumption == "independent":
        warnings.append("First-order standard uncertainties assume independent inputs and zero covariance; shared specimens/calibration can violate this. Missing uncertainties are not treated as zero.")
    if any(q["_u"] is not None and q["_u"] / q["si_value"] >= .1 for q in all_inputs):
        warnings.append("At least one relative standard uncertainty is >=10%; linear propagation may be inaccurate (10% is a project warning convention).")
    for name, quantity in quantities.items():
        unit, factor, si_unit = _CANONICAL[name]
        quantity.update(value=_number(quantity["si_value"] / factor, name), unit=unit, si_unit=si_unit)
        u = quantity.pop("_u")
        if u is not None:
            quantity["standard_uncertainty"] = _number(u / factor, "canonical uncertainty", zero=True)
    if geometry:
        for name, q in geometry.items():
            q["si_unit"] = "ohm" if name == "resistance" else "m"
            u = q.pop("_u")
            if u is not None:
                q["si_standard_uncertainty"] = u
    return {"schema_version": 1, "operation": "solve", "status": status,
            "quantities": quantities, "knowns": knowns, "unknowns": unknowns if status == "insufficient_data" else [],
            "derived": [k for k, q in quantities.items() if q["provenance"] == "derived"],
            "consistency": consistency, "rectangle_inputs": geometry,
            "explanation": ("Need any two independent quantities among thickness, sheet resistance and resistivity. Sheet resistance alone cannot identify thickness; supply a specimen-relevant resistivity or an independent thickness measurement."
                if status == "insufficient_data" else "rho = Rs * t. Thickness inferred using supplied resistivity is conditional, not an independent thickness measurement. All supplied values are preserved; consistency is a numerical check, not experimental acceptance."),
            "meanings": {"thickness": "Physical conducting-layer thickness", "sheet_resistance": "In-plane resistance per square; distinct from arbitrary part resistance", "resistivity": "Material/process-dependent bulk-equivalent electrical resistivity"},
            "model_assumptions": ["Uniform, isotropic, continuous ohmic single conducting layer", "No parallel substrate/layer conduction", "Inputs describe the same specimen and temperature/process conditions", "No universal ITO resistivity is assumed"],
            "warnings": warnings, "references": _SOURCES.copy()}
