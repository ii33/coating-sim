"""Direct film-result calculation and artifact publication.

This module keeps the result receipt and the JSON/CSV publication boundary
independent from MCP and desktop UI adapters.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from coat_sim.core.atomic_io import replace_with_retry


def film_artifact_path(service: Any, path: str | os.PathLike[str] | None) -> Path:
    """Resolve a result JSON path and keep it directly under ``data/results``."""
    if path is None:
        path = f"data/results/film_{uuid4().hex}.json"
    target = service._path(path)
    results = (Path(service.root) / "data" / "results").resolve()
    if target.parent != results or target.suffix.lower() != ".json":
        raise ValueError("output_path must be a .json file directly inside data/results")
    return target


def atomic_film_json(path: Path, value: dict[str, Any], *, overwrite: bool) -> None:
    """Publish a JSON artifact atomically, refusing races when overwrite is false."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".film-result-", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            replace_with_retry(temporary, path)
        elif os.name == "nt":
            # On Windows os.rename is an atomic same-volume move which fails
            # when the destination already exists. It preserves no-clobber
            # semantics without requiring hard-link creation privileges.
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def simulate_film_result(
    service: Any,
    data: dict[str, Any],
    settings: dict[str, Any] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
    *,
    publish_json: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Validate, calculate, export, and return the established film receipt.

    ``publish_json`` is an adapter seam for entrypoints that need to retain
    control of the atomic JSON writer (for example, MCP callers with legacy
    monkeypatches). It receives ``(path, payload, overwrite=...)``.
    """
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be boolean")

    json_path = film_artifact_path(service, output_path)
    csv_path = json_path.with_suffix(".csv")
    if not overwrite and (json_path.exists() or csv_path.exists()):
        raise ValueError("film result artifact already exists; set overwrite=true to replace it")

    design = service.validate_film_design(data)
    result = service.simulate_film_design(design, settings=settings)
    normalized_settings = result.get("settings", settings or {})
    identity = {
        "design": result.get("design", design),
        "settings": normalized_settings,
        "provenance": result.get("provenance", []),
    }
    digest = _canonical_sha256(identity)
    payload = {
        "design": design,
        "settings": normalized_settings,
        "result": result,
        "input_sha256": digest,
    }
    payload["artifact_payload_sha256"] = _canonical_sha256(payload)

    service.export_film_csv(csv_path, result, overwrite=overwrite)
    publisher = publish_json or atomic_film_json
    try:
        publisher(json_path, payload, overwrite=overwrite)
    except Exception as exc:
        raise ValueError(
            f"CSV artifact saved at {csv_path}; JSON publication failed: {exc}"
        ) from exc

    def band(key: str) -> dict[str, float]:
        values = result[key]
        return {"min": min(values), "mean": sum(values) / len(values), "max": max(values)}

    wavelength = result["wavelength_nm"]
    return {
        "ok": True,
        "summary": {
            "samples": len(wavelength),
            "wavelength_range_nm": [min(wavelength), max(wavelength)],
            "R": band("R"),
            "T": band("T"),
            "A": band("A"),
        },
        "artifact_paths": {"json": str(json_path), "csv": str(csv_path)},
        "warnings": result.get("warnings", []),
        "input_sha256": digest,
        "artifact_payload_sha256": payload["artifact_payload_sha256"],
    }

