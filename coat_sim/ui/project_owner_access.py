"""Explicit runtime ownership lookup for shared presentation adapters."""
from __future__ import annotations
from typing import Any


def project_owner(bridge: Any) -> Any:
    # Legacy embedders/tests may still supply the old adapter interface. A real
    # desktop router always defines project_runtime, including None in launchers.
    return getattr(bridge, "project_runtime", getattr(bridge, "project_launcher", None))
