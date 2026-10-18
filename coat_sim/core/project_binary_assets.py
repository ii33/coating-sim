"""Strict, streaming, project-owned binary input capabilities.

These limits bound input reads and temporary disk use. They do not limit a
physical model's component count, rays, masks, samples, or calculations.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import csv
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from .project_dependencies import ProjectError, identifier, portable_path, reject_links

_CHUNK = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
ROLE_SUFFIXES = {"deposition_asset": ".coatasset", "spd_csv": ".csv", "bbs_attachment": ".bbsasset"}
MAX_DEPOSITION_ASSET_BYTES = 512 * 1024 * 1024
MAX_SPD_BYTES = 2_000_000
MAX_BBS_ATTACHMENT_BYTES = 32 * 1024 * 1024


def role_byte_limit(role: str) -> int:
    return {"deposition_asset": MAX_DEPOSITION_ASSET_BYTES, "spd_csv": MAX_SPD_BYTES,
            "bbs_attachment": MAX_BBS_ATTACHMENT_BYTES}[role]


DEFAULT_ASSET_BUDGET_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class AssetReadLimits:
    """Explicit safety-I/O budget; callers may configure total disk usage.

    Role-specific limits retain the existing asset/SPD reader's maxima. There
    is no separate arbitrary asset-count cap; managed-document limits apply.
    """
    max_total_bytes: int = DEFAULT_ASSET_BUDGET_BYTES

    def __post_init__(self):
        if type(self.max_total_bytes) is not int or self.max_total_bytes < 1:
            raise ProjectError("invalid_limits", "Asset input budget must be a positive integer byte count")

    def check_total(self, actual: int):
        if actual > self.max_total_bytes:
            raise ProjectError("capacity_exceeded", f"Asset input/spool bytes {actual} exceed configured safety budget {self.max_total_bytes}")


DEFAULT_ASSET_LIMITS = AssetReadLimits()


def normalize_binary_asset(value: Mapping) -> dict:
    fields = {"kind", "version", "role", "payload_path", "size", "sha256"}
    if isinstance(value, Mapping) and value.get("role") == "bbs_attachment":
        fields |= {"name", "mime"}
    if (not isinstance(value, Mapping) or set(value) != fields or
            value.get("kind") != "coat_sim.project_binary_asset" or
            type(value.get("version")) is not int or value["version"] != 1):
        raise ProjectError("invalid_document", "Invalid project binary input envelope")
    role = value["role"]
    if not isinstance(role, str) or role not in ROLE_SUFFIXES:
        raise ProjectError("invalid_document", "Unsupported binary input role")
    path = portable_path(value["payload_path"])
    parts = PurePosixPath(path).parts
    if len(parts) != 3 or parts[0] != "assets":
        raise ProjectError("unsafe_path", "Binary inputs must use assets/<generation>/<UUID>.<role suffix>")
    identifier(parts[1])
    filename = PurePosixPath(parts[2])
    identifier(filename.stem)
    if filename.suffix != ROLE_SUFFIXES[role]:
        raise ProjectError("unsafe_path", "Binary input suffix differs from its declared role")
    size = value["size"]
    maximum = role_byte_limit(role)
    if type(size) is not int or not 0 < size <= maximum:
        raise ProjectError("capacity_exceeded", f"Binary input size must be 1–{maximum} bytes for {role}")
    if not isinstance(value["sha256"], str) or not _SHA.fullmatch(value["sha256"]):
        raise ProjectError("invalid_document", "Binary input SHA-256 must be lowercase hex")
    if role == "bbs_attachment":
        from .project_bbs_attachments import normalize_attachment
        normalize_attachment({"asset_id": filename.stem, "name": value["name"], "mime": value["mime"],
                              "size": size, "sha256": value["sha256"]})
    return dict(value)


def file_signature(path: str | Path) -> tuple:
    target = Path(os.path.abspath(Path(path).expanduser()))
    reject_links(target)
    try:
        info = target.lstat()
    except OSError as exc:
        raise ProjectError("missing_dependency", "Binary input is unavailable: " + str(target)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProjectError("unsafe_path", "Binary input must be an ordinary file: " + str(target))
    return (str(target), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def open_asset_source(source: Mapping):
    if not isinstance(source, Mapping):
        raise ProjectError("invalid_document", "Invalid binary input source capability")
    if set(source) == {"path"}:
        path = Path(source["path"])
        before = file_signature(path)
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != before[1:3]:
                raise ProjectError("unsafe_path", "Binary input changed identity while opening")
            yield stream
        if file_signature(path) != before:
            raise ProjectError("stale_source", "Binary input changed while reading")
    elif set(source) == {"package", "member"}:
        package = Path(source["package"])
        before = file_signature(package)
        member = portable_path(source["member"])
        with zipfile.ZipFile(package) as archive:
            info = archive.getinfo(member)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if (info.is_dir() or info.flag_bits & 1 or mode not in (0, stat.S_IFREG) or
                    info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ProjectError("invalid_package", "Binary input member must be an ordinary unencrypted file")
            with archive.open(info) as stream:
                yield stream
        if file_signature(package) != before:
            raise ProjectError("stale_source", "Project package changed while reading binary input")
    else:
        raise ProjectError("invalid_document", "Binary input source must be one owned file or exact package member")


def stream_asset(source: Mapping, metadata: Mapping, output=None) -> None:
    metadata = normalize_binary_asset(metadata)
    actual = 0
    digest = hashlib.sha256()
    try:
        with open_asset_source(source) as stream:
            while True:
                block = stream.read(min(_CHUNK, metadata["size"] - actual + 1))
                if not block:
                    break
                actual += len(block)
                if actual > metadata["size"]:
                    raise ProjectError("integrity_error", "Binary input exceeds its declared size")
                digest.update(block)
                if output is not None:
                    output.write(block)
    except ProjectError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise ProjectError("io_error", "Cannot read binary input") from exc
    if actual != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
        raise ProjectError("integrity_error", "Binary input size or SHA-256 differs from its declared value")


def read_scalar_spectrum(path: str | Path) -> dict:
    target = Path(path)
    signature = file_signature(target)
    if not 0 < signature[3] <= MAX_SPD_BYTES:
        raise ProjectError("capacity_exceeded", "Scalar spectrum CSV exceeds the existing 2 MB read limit")
    waves: list[float] = []
    values: list[float] = []
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as stream:
            for line_number, raw in enumerate(stream, 1):
                text = raw.strip()
                if not text or text.startswith("#"):
                    continue
                row = next(csv.reader([raw]))
                if len(row) < 2:
                    raise ProjectError("invalid_document", f"Spectrum CSV line {line_number} needs wavelength and value")
                try:
                    wave, value = float(row[0]), float(row[1])
                except (TypeError, ValueError) as exc:
                    if not waves and any(token in text.lower() for token in ("wavelength", "nm", "value", "spd")):
                        continue
                    raise ProjectError("invalid_document", f"Spectrum CSV line {line_number} is not numeric") from exc
                if not math.isfinite(wave) or not math.isfinite(value) or value < 0 or (waves and wave <= waves[-1]):
                    raise ProjectError("invalid_document", "Spectrum samples must be finite, nonnegative, and strictly increasing by wavelength")
                waves.append(wave)
                values.append(value)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ProjectError("invalid_document", "Cannot parse scalar spectrum CSV") from exc
    if len(waves) < 2:
        raise ProjectError("invalid_document", "Scalar spectrum needs at least two samples")
    if file_signature(target) != signature:
        raise ProjectError("stale_source", "Spectrum input changed while reading")
    return {"wavelength_nm": waves, "value": values}


def validate_asset_content(path: str | Path, metadata: Mapping) -> None:
    metadata = normalize_binary_asset(metadata)
    if metadata["role"] == "bbs_attachment":
        # Generic attachments are never executed or decompressed. Preview
        # decoders validate a separate MIME allowlist after digest verification.
        return
    if metadata["role"] == "spd_csv":
        read_scalar_spectrum(path)
    else:
        from .simulation_assets import read_simulation_asset
        try:
            read_simulation_asset(path, detail="summary", include_arrays=False)
        except (OSError, ValueError) as exc:
            raise ProjectError("invalid_document", "Invalid deposition input asset: " + str(exc)) from exc


def capture_asset(source: Mapping, metadata: Mapping, destination: Path) -> dict:
    """Copy and validate the same private bytes; never parse a moving source."""
    reject_links(destination)
    try:
        with destination.open("xb") as stream:
            stream_asset(source, metadata, stream)
            stream.flush()
            os.fsync(stream.fileno())
        validate_asset_content(destination, metadata)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {"path": str(destination)}


def metadata_for_file(path: str | Path, role: str, payload_path: str, *, expected_sha256=None) -> dict:
    if role not in ROLE_SUFFIXES:
        raise ProjectError("invalid_document", "Unsupported binary input role")
    target = Path(os.path.abspath(Path(path).expanduser()))
    before = file_signature(target)
    maximum = role_byte_limit(role)
    if target.suffix.casefold() != ROLE_SUFFIXES[role] or not 0 < before[3] <= maximum:
        raise ProjectError("invalid_document", "Selected binary input has the wrong suffix or exceeds its role limit")
    digest = hashlib.sha256()
    actual = 0
    with target.open("rb") as stream:
        while True:
            block = stream.read(_CHUNK)
            if not block:
                break
            actual += len(block)
            if actual > maximum:
                raise ProjectError("capacity_exceeded", "Binary input exceeds its role read limit")
            digest.update(block)
    if file_signature(target) != before or actual != before[3]:
        raise ProjectError("stale_source", "Binary input changed during fingerprint capture")
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise ProjectError("integrity_error", "Selected binary input differs from the saved input digest")
    return normalize_binary_asset({"kind": "coat_sim.project_binary_asset", "version": 1,
        "role": role, "payload_path": payload_path, "size": actual, "sha256": digest.hexdigest()})
