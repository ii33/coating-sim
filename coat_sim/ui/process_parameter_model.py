"""Lossless display and conservative parsing for material process values."""
from __future__ import annotations

import re


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_UNIT = r"(?:nm/min|Å/s|Torr|°C|℃|°F|℉|degC|degF|K|C|F|W)"
_VALUE_WITH_UNIT = re.compile(rf"^(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})$", re.IGNORECASE)


def display_process_parameter_value(value: object, unit: object) -> str:
    """Join stored value and unit for editing without duplicating a suffix."""
    raw_value = "" if value is None else str(value)
    raw_unit = "" if unit is None else str(unit)
    if not raw_unit:
        return raw_value

    suffix = raw_value[-len(raw_unit):] if raw_value else ""
    if suffix.casefold() == raw_unit.casefold():
        prefix = raw_value[:-len(raw_unit)]
        if not prefix or prefix[-1].isspace() or prefix[-1].isdigit() or prefix[-1] in ".+-−":
            return raw_value
    return f"{raw_value} {raw_unit}".strip()


def parse_process_parameter_value(text: object) -> dict[str, str]:
    """Split only a numeric value with a supported unit; keep prose intact."""
    value = "" if text is None else str(text).strip()
    match = _VALUE_WITH_UNIT.fullmatch(value)
    if match:
        return {"value": match.group("value"), "unit": match.group("unit")}
    return {"value": value, "unit": ""}


def project_process_parameter_rows(rows: object) -> dict[str, list[dict[str, str]]]:
    """Create the two-column editing view plus metadata for lossless save-back."""
    visible_rows: list[dict[str, str]] = []
    originals: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return {"rows": visible_rows, "originals": originals}

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = "" if row.get("name") is None else str(row.get("name", ""))
        value = "" if row.get("value") is None else str(row.get("value", ""))
        unit = "" if row.get("unit") is None else str(row.get("unit", ""))
        display_value = display_process_parameter_value(value, unit)
        visible_rows.append({"name": name, "value": display_value})
        originals.append({"name": name, "value": value, "unit": unit,
                          "display_value": display_value})
    return {"rows": visible_rows, "originals": originals}


def serialize_process_parameter_rows(rows: object, originals: object) -> list[dict[str, str]]:
    """Restore unchanged legacy pairs and conservatively parse edited values."""
    if not isinstance(rows, list):
        return []
    original_rows = originals if isinstance(originals, list) else []
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        visible_name = "" if row.get("name") is None else str(row.get("name", ""))
        visible_value = "" if row.get("value") is None else str(row.get("value", ""))
        original = original_rows[index] if index < len(original_rows) else None
        name = (str(original.get("name", ""))
                if isinstance(original, dict) and visible_name == str(original.get("name", ""))
                else visible_name.strip())
        unchanged_display = (isinstance(original, dict)
                             and visible_value == str(original.get("display_value", "")))
        if unchanged_display:
            parts = {"value": str(original.get("value", "")),
                     "unit": str(original.get("unit", ""))}
        else:
            parts = parse_process_parameter_value(visible_value)
        clean = {"name": name,
                 "value": str(parts.get("value", "") or ""),
                 "unit": str(parts.get("unit", "") or "")}
        if not unchanged_display:
            clean["value"] = clean["value"].strip()
            clean["unit"] = clean["unit"].strip()
        if any(clean.values()):
            result.append(clean)
    return result
