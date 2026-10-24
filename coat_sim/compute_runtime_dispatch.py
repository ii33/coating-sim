"""Opt-in managed interpreter selection at non-GUI process boundaries.

Source/development installations without a manifest keep their existing route.
An existing invalid manifest or missing requested role never falls back to an
external/current interpreter. The full read-only validator can perform many file
reads and must not be called directly on the Qt UI thread.
"""
from __future__ import annotations

from pathlib import Path

from coat_sim.compute_runtime_manifest import (
    RuntimeBindingMissing, load_compute_runtime_manifest,
)


def installed_role_python(installation_root: Path, role: str) -> str | None:
    """Return a bound private interpreter, or None only for an absent manifest.

    Selection means binding consistency only, never CUDA/DirectML/renderer
    readiness. Existing numerical probes remain the readiness owners.
    """
    try:
        manifest = load_compute_runtime_manifest(installation_root)
    except RuntimeBindingMissing:
        return None
    # Keep this outside the catch: an absent role in an existing manifest is a
    # broken request, not permission to execute in the current Python instead.
    return str(manifest.select_python(role))
