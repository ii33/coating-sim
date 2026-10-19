"""Read existing fixed CIE data with its original manifest and attribution.

This adapter neither evaluates, interpolates nor changes the numerical tables.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any


def cie_reference_root() -> Path:
    return Path(__file__).absolute().parents[2] / "data" / "film_design" / "cie-illuminants-20261006"


class CieReferenceSnapshot:
    def __init__(self) -> None:
        from coat_sim.agent.research_library import ensure_no_reparse_components
        root = ensure_no_reparse_components(cie_reference_root())
        manifest_path = ensure_no_reparse_components(root / "MANIFEST_SHA256.json")
        if not manifest_path.is_file() or manifest_path.stat().st_size > 256 * 1024:
            raise ValueError("Invalid CIE reference manifest")
        with manifest_path.open("rb") as stream:
            manifest_bytes = stream.read(256 * 1024 + 1)
        if len(manifest_bytes) > 256 * 1024:
            raise ValueError("CIE manifest exceeds budget")
        manifest = json.loads(manifest_bytes)
        self.version = hashlib.sha256(manifest_bytes).hexdigest()
        contents = {}
        for relative in ("data/cie_illuminants.json", "NOTICE_DATA.md"):
            path = ensure_no_reparse_components(root / relative)
            entry = manifest[relative]
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError("CIE reference exceeds budget")
            with path.open("rb") as stream:
                payload = stream.read(1024 * 1024 + 1)
            if (len(payload) != entry["bytes"] or len(payload) > 1024 * 1024
                    or hashlib.sha256(payload).hexdigest() != entry["sha256"]):
                raise ValueError("CIE reference checksum mismatch")
            contents[relative] = payload.decode("utf-8")
        self.sha256 = manifest["data/cie_illuminants.json"]["sha256"]
        data = json.loads(contents["data/cie_illuminants.json"])
        if (data.get("schemaVersion") != 1 or data.get("dataLicense") != "CC-BY-SA-4.0"
                or set(data["datasets"]) != {"A", "D65", "D50", "V", "XYZ1931_2", "E"}):
            raise ValueError("Unsupported CIE reference schema")
        self.datasets: dict[str, Any] = data["datasets"]
        self.packaging = {key: data[key] for key in ("snapshotDate", "dataLicense", "changes", "scope")}
        self.packaging["attribution_notice"] = contents["NOTICE_DATA.md"]

    def summaries(self) -> list[dict[str, Any]]:
        return [{"id": identity, "title": dataset["title"], "wavelength_unit": "nm",
                 "metadata": {key: deepcopy(value) for key, value in dataset.items() if not isinstance(value, list) or len(value) <= 12},
                 "sha256": self.sha256, "manifest_sha256": self.version, "license": "CC-BY-SA-4.0"}
                for identity, dataset in self.datasets.items()]

    def read(self, identity: str, version: str) -> dict[str, Any]:
        if version != self.version or identity not in self.datasets:
            raise ValueError("CIE reference identity/version changed")
        return {"availability": "packaged_data", "sha256": self.sha256, "manifest_sha256": self.version,
                "packaging": deepcopy(self.packaging), "dataset": deepcopy(self.datasets[identity])}
