"""Immutable packaged-seed material defaults owned by every project.

Scope (explicit): the two independently owned packaged ideal-Air optical materials
(``builtin_ideal_air`` and ``builtin_ideal_air_substrate``, each owning
``ideal_air_n1_k0``, n=1, k=0) are provided here. They are
read from the read-only built-in seed (``builtin_material_profiles``), never from
a user's mutable global material library.

Consequences that callers and tests may rely on:

* A new project draft can bind incident/exit media to ideal Air even when the
  global catalog is empty, edited, or tombstoned.
* This module never writes to the global library, so it cannot resurrect a
  globally deleted Air or overwrite a globally edited one.
* The value is captured into the project's own optical closure when the film is
  saved, after which the project no longer needs any global input.

Nothing else beyond the two ideal-Air records is provisioned; those remain
explicit user/project choices.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# The packaged starter seed is limited to this ordered identity list.
STARTER_MATERIAL_IDS: tuple[str, ...] = ("builtin_ideal_air", "builtin_ideal_air_substrate")

# Picker usage remains separate from category presentation. Each Air is offered
# in its own category and in the incident/exit medium picker.
STARTER_USAGE: dict[str, tuple[str, ...]] = {
    "builtin_ideal_air": ("film", "medium"),
    "builtin_ideal_air_substrate": ("substrate",),
}


def project_starter_materials() -> list[dict[str, Any]]:
    """Return detached packaged-seed starter records in project optical shape.

    A missing or malformed packaged seed yields an empty list rather than
    raising: the seed is a convenience default, not a hard project invariant.
    """
    from .materials import builtin_material_profiles

    wanted = set(STARTER_MATERIAL_IDS)
    try:
        profiles = builtin_material_profiles()
    except (OSError, ValueError, TypeError, KeyError):
        return []
    records: list[dict[str, Any]] = []
    for profile in profiles:
        if profile.get("id") not in wanted:
            continue
        optical_sets = profile.get("optical_constants") or []
        if not optical_sets:
            continue
        records.append({
            "id": profile["id"],
            "name": profile["name"],
            "notes": profile.get("notes", ""),
            "optical_sets": deepcopy(optical_sets),
        })
    return records


def project_starter_usage(records: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Picker usage for the starter records actually present in ``records``."""
    present = {row["id"] for row in records}
    return {identity: roles for identity, roles in STARTER_USAGE.items()
            if identity in present}
