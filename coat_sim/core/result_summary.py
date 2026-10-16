"""Small result views for automation; complete audit records remain on disk."""
from __future__ import annotations
from typing import Any
from copy import deepcopy
import math

_OMIT = {"field", "field_nm", "x", "y", "step_results", "effective_settings", "requested_settings"}

def compact_result_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Preserve existing scalar statistics, without recomputing or returning layer tables."""
    out = {}
    for key, value in result.items():
        if key in _OMIT:
            continue
        if hasattr(value, "item") and getattr(value, "shape", ()) == ():
            value = value.item()
        if value is None or isinstance(value, (str, bool, int)):
            out[key] = value[:2000] if isinstance(value, str) else value
        elif isinstance(value, float):
            out[key] = value if math.isfinite(value) else None
        elif isinstance(value, (list, tuple)) and len(value) <= 12 and all(
                item is None or isinstance(item, (str, bool, int, float)) for item in value):
            out[key] = [item[:500] if isinstance(item, str) else
                        (None if isinstance(item, float) and not math.isfinite(item) else item)
                        for item in value]
    out["step_count"] = len(result["step_results"]) if "step_results" in result else result.get("step_count", 0)
    return out


def _audit_value(value: Any) -> Any:
    """Keep nested report metadata JSON-safe without publishing field arrays."""
    import numpy as np

    if isinstance(value, np.ndarray):
        return {"shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, np.generic):
        return _audit_value(value.item())
    if isinstance(value, dict):
        return {str(key): _audit_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def algorithm_result_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Shared compact job/MCP view; the complete trace stays in the asset."""
    metadata = compact_result_metadata(result)
    report = result.get("algorithm_report")
    if isinstance(report, dict):
        metadata["algorithm_report"] = {
            key: _audit_value(value) for key, value in report.items() if key != "trace"
        }
        metadata["algorithm_report"]["trace_count"] = len(report.get("trace", []))
        metadata["algorithm_report"]["trace_detail"] = "read_calculation_result(detail='full')"
    return metadata


def process_summary(process: dict[str, Any] | None) -> dict[str, Any] | None:
    if process is None:
        return None
    return {key: (process[key][:2000] if isinstance(process[key], str) else process[key])
            for key in ("name", "description", "machine_file", "substrate_file") if key in process} | {"step_count": len(process.get("steps", []))} | ({"environment": deepcopy(process["environment"]), "environment_override_count": sum("environment" in step for step in process.get("steps", []))} if "environment" in process else ({"environment_override_count": sum("environment" in step for step in process.get("steps", []))} if any("environment" in step for step in process.get("steps", [])) else {}))
