"""Stdlib-only managed-project boundary for generic filesystem adapters."""
from __future__ import annotations

import os
from pathlib import Path


def assert_not_managed_project_path(path: Path, *, include_descendants: bool = False) -> None:
    """Keep generic file tools outside managed project identity/history.

    A descriptor's presence is sufficient, even if malformed: parsing it here
    would let a corrupt or partially replaced descriptor disable protection.
    This helper deliberately has no DATA_ROOT dependency so other authorized
    filesystem adapters can apply the same boundary. It is not a filesystem
    sandbox against a local administrator or arbitrary native code execution.
    """
    path = Path(os.path.abspath(path))
    message = "Managed project files require their project service; generic file mutation is not permitted"
    if path.suffix.casefold() in {".coatproj", ".cbbs"}:
        raise ValueError(message)
    parent = path if path.is_dir() else path.parent
    while True:
        if parent.is_dir():
            with os.scandir(parent) as entries:
                for index, entry in enumerate(entries):
                    if index >= 10000:
                        raise ValueError("Project boundary scan exceeds its safe capacity")
                    if Path(entry.name).suffix.casefold() == ".coatproj":
                        raise ValueError(message)
        if parent == parent.parent:
            break
        parent = parent.parent
    if include_descendants and path.is_dir():
        count = 0
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                count += 1
                if count > 10000:
                    raise ValueError("Project boundary scan exceeds its safe capacity")
                if Path(name).suffix.casefold() in {".coatproj", ".cbbs"}:
                    raise ValueError(message)
                child = Path(directory) / name
                if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                    raise ValueError("Project boundary scan cannot traverse links or junctions")
