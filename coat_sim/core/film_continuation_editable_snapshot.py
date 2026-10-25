"""Compact, integrity-bound editable material closure for durable warm restarts.

The numerical snapshot already owns active n/k arrays. This delta references
identical active sets and stores only additional/different editable records.
No live provider reads, optical simulation or schema migration is performed.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .film_process_import import _sha256, validate_frozen_design_snapshot

KIND = "coat_sim.film_continuation_editable_delta"
SCHEMA_VERSION = 1


def encode_editable_snapshot_delta(active_snapshot: Mapping[str, Any],
                                   full_editable_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Encode an exact editable snapshot without repeating active optical sets."""
    active = validate_frozen_design_snapshot(active_snapshot)
    full = validate_frozen_design_snapshot(full_editable_snapshot)
    if ({k: v for k, v in active.items() if k not in {"materials", "sha256"}}
            != {k: v for k, v in full.items() if k not in {"materials", "sha256"}}):
        raise ValueError("editable continuation snapshot differs from its active design/settings")
    active_sets = {(m["id"], o["id"]): o for m in active["materials"] for o in m["optical_sets"]}
    materials = []
    for material in full["materials"]:
        record = {key: deepcopy(value) for key, value in material.items() if key != "optical_sets"}
        sets = []
        for optical in material["optical_sets"]:
            original = active_sets.get((material["id"], optical["id"]))
            sets.append({"active_set_id": optical["id"]} if original == optical
                        else {"value": deepcopy(optical)})
        materials.append({"record": record, "optical_sets": sets})
    return {"schema_version": SCHEMA_VERSION, "kind": KIND,
            "active_sha256": active["sha256"], "full_sha256": full["sha256"],
            "materials": materials}


def restore_editable_snapshot_delta(active_snapshot: Mapping[str, Any],
                                    delta: Mapping[str, Any]) -> dict[str, Any]:
    """Restore and strictly validate the exact saved editable closure."""
    active = validate_frozen_design_snapshot(active_snapshot)
    if (not isinstance(delta, Mapping) or set(delta) != {
            "schema_version", "kind", "active_sha256", "full_sha256", "materials"}
            or type(delta.get("schema_version")) is not int
            or delta["schema_version"] != SCHEMA_VERSION or delta.get("kind") != KIND
            or delta.get("active_sha256") != active["sha256"]
            or not isinstance(delta.get("materials"), list)):
        raise ValueError("editable continuation delta kind/version/base is invalid")
    active_sets = {(m["id"], o["id"]): o for m in active["materials"] for o in m["optical_sets"]}
    materials = []
    for item in delta["materials"]:
        if (not isinstance(item, Mapping) or set(item) != {"record", "optical_sets"}
                or not isinstance(item["record"], Mapping) or "optical_sets" in item["record"]
                or not isinstance(item["record"].get("id"), str)
                or not isinstance(item["optical_sets"], list)):
            raise ValueError("editable continuation material delta is invalid")
        material = deepcopy(dict(item["record"]))
        sets = []
        for optical in item["optical_sets"]:
            if not isinstance(optical, Mapping):
                raise ValueError("editable continuation optical delta is invalid")
            if set(optical) == {"active_set_id"} and isinstance(optical["active_set_id"], str):
                key = (material["id"], optical["active_set_id"])
                if key not in active_sets:
                    raise ValueError("editable continuation delta references an unknown active optical set")
                sets.append(deepcopy(active_sets[key]))
            elif set(optical) == {"value"} and isinstance(optical["value"], Mapping):
                sets.append(deepcopy(dict(optical["value"])))
            else:
                raise ValueError("editable continuation optical delta is invalid")
        material["optical_sets"] = sets
        materials.append(material)
    restored = deepcopy(active)
    restored["materials"] = materials
    restored["sha256"] = _sha256(restored)
    if restored["sha256"] != delta["full_sha256"]:
        raise ValueError("editable continuation full snapshot digest does not match")
    return validate_frozen_design_snapshot(restored)
