"""Fixed one-shot worker for freezing a verified appearance render request."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from coat_sim.appearance.limits import (
    MAX_APPEARANCE_CONTROL_BYTES, MAX_APPEARANCE_FROZEN_BYTES,
)

MAX_APPEARANCE_JSON_BYTES = MAX_APPEARANCE_CONTROL_BYTES


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _plain_json(value: Any) -> Any:
    """Round-trip through strict JSON so no numpy or custom objects escape."""
    encoded = _canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_APPEARANCE_FROZEN_BYTES:
        raise ValueError("appearance frozen data exceeds the 60 MB limit")
    return json.loads(encoded.decode("utf-8"))


def _cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def build_frozen_envelope(root, request: dict[str, Any], cancel_event=None) -> dict[str, Any]:
    """Re-read a canonical request's asset and construct its immutable job envelope."""
    from .appearance_compute import (
        _normalize_public_request, _root_path,
    )
    from coat_sim.appearance.contract import validate_job
    from coat_sim.ui.appearance_worker import (
        _appearance_context_from_asset,
        _build_job_from_verified_asset,
    )

    root_path = _root_path(root)
    canonical_request = _normalize_public_request(
        root_path, request,
        preserve_legacy_backend=(type(request) is dict and "backend" not in request
                                 and "precision" not in request))
    if _canonical_json(canonical_request) != _canonical_json(request):
        raise ValueError("appearance freeze requires a canonical request")
    if _cancelled(cancel_event):
        raise InterruptedError("appearance freeze cancelled before asset verification")

    asset_path = Path(canonical_request["asset_path"])
    asset_sha256 = canonical_request["asset_sha256"]
    context = _appearance_context_from_asset(
        asset_path, root_path, asset_sha256, cancel_event=cancel_event)
    context = _plain_json(context)
    if _cancelled(cancel_event):
        raise InterruptedError("appearance freeze cancelled after context verification")

    thickness = (canonical_request.get("geometry_thickness_mm")
                 if canonical_request["geometry_thickness_mode"] == "explicit_override"
                 else None)
    job = _build_job_from_verified_asset(
        context["process_profile"], str(asset_path), str(root_path),
        canonical_request["render_inputs"]["wavelength_nm"],
        canonical_request["render_inputs"],
        expected_sha256=asset_sha256,
        geometry_thickness_mm=thickness,
        cancel_event=cancel_event,
    )
    # Validate the raw adapter result, but preserve it byte-for-byte as JSON data.
    validate_job(job, max_wavelength_samples=(
        None if canonical_request.get("backend", "cpu") == "gpu_cuda" else 20_000_000))
    job = _plain_json(job)
    if _cancelled(cancel_event):
        raise InterruptedError("appearance freeze cancelled after job construction")

    envelope = {
        "schema": "coat_sim.appearance_frozen_request",
        "schema_version": 1,
        "root": str(root_path),
        "request": canonical_request,
        "context": context,
        "context_sha256": _sha256_json(context),
        "job": job,
        "job_sha256": _sha256_json(job),
    }
    if len(_canonical_json(envelope).encode("utf-8")) > MAX_APPEARANCE_FROZEN_BYTES:
        raise ValueError("appearance frozen request exceeds the 60 MB limit")
    return envelope


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError("non-finite JSON number: " + value)


def run(input_path: Path, output_path: Path) -> int:
    """Read one bounded request and atomically write one frozen envelope."""
    try:
        input_path = input_path.resolve(strict=True)
        output_parent = output_path.parent.resolve(strict=True)
        if input_path.parent != output_parent:
            raise ValueError("appearance freeze input and output must share a temporary directory")
        if not input_path.is_file() or input_path.stat().st_size > MAX_APPEARANCE_JSON_BYTES:
            raise ValueError("appearance freeze input is missing or exceeds 16 MB")
        raw = input_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_nonfinite)
        if (type(payload) is not dict
                or set(payload) != {"schema", "schema_version", "root", "request"}
                or payload.get("schema") != "coat_sim.appearance_freeze_input"
                or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1):
            raise ValueError("appearance freeze input schema is unsupported")
        envelope = build_frozen_envelope(payload["root"], payload["request"])
        encoded = (_canonical_json(envelope) + "\n").encode("utf-8")
        if len(encoded) > MAX_APPEARANCE_FROZEN_BYTES:
            raise ValueError("appearance freeze output exceeds 60 MB")
        temporary = output_path.with_name(output_path.name + ".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, output_path)
        return 0
    except Exception as exc:
        # Avoid leaking asset/profile contents through the captured child output.
        sys.stderr.write(f"appearance freeze failed: {type(exc).__name__}\n")
        sys.stderr.flush()
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="appearance-freeze-worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    return run(Path(args.input), Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
