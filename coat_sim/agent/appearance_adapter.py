"""Assistant-side projection of the verified appearance result outputs.

The MCP result validator owns path, size, digest, file-set, and renderer
validation. This module only forwards the already-validated output records to
the assistant's existing project-data artifact upsert path; it does not read
files, compute digests, or construct output paths.
"""
from __future__ import annotations

from typing import Any


def appearance_output_artifacts(
    result: Any, *, expected_job_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return outputs only for a valid typed appearance result for this job."""
    if not isinstance(result, dict) or result.get("kind") != "appearance_render":
        return ()
    from coat_sim.core.appearance_result import is_appearance_render_result

    if not is_appearance_render_result(result):
        return ()
    if expected_job_id is not None and result.get("job_id") != expected_job_id:
        return ()
    outputs = result.get("outputs")
    if not isinstance(outputs, dict):
        return ()
    # The shared typed validator owns the exact output keys and record fields.
    # Preserve each shared record unchanged for the existing artifact upsert.
    return tuple(item for item in outputs.values() if isinstance(item, dict))
