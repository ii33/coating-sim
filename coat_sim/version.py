"""Human-readable application version and optional build provenance.

The version text file is the only authoritative application-version value.
Git provenance is accepted only when the build caller supplies a full SHA;
this module never shells out or guesses repository state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import re
from typing import Mapping


VERSION_FILE = Path(__file__).resolve().with_name("version.txt")
UNKNOWN_VERSION = "unknown"
_VERSION_RE = re.compile(r"dev\.(\d{8})\.(\d{2})\Z")
_GIT_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.IGNORECASE)
_UTC_PLUS_8 = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class BuildIdentity:
    version: str
    git_sha: str | None = None


def parse_internal_version(value: str) -> tuple[date, int] | None:
    """Return the UTC+8 publication date and daily ordinal, or ``None``."""
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        published = datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None
    ordinal = int(match.group(2))
    if ordinal < 1:
        return None
    return published, ordinal


def read_version(path: Path = VERSION_FILE) -> str:
    """Read the packaged version; return an explicit fallback if unavailable."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return UNKNOWN_VERSION
    return value if parse_internal_version(value) is not None else UNKNOWN_VERSION


def read_build_identity(
    version_path: Path = VERSION_FILE,
    environ: Mapping[str, str] | None = None,
) -> BuildIdentity:
    """Read version and caller-supplied provenance without invoking Git."""
    environment = os.environ if environ is None else environ
    supplied_sha = environment.get("COAT_SIM_GIT_SHA", "").strip()
    git_sha = supplied_sha.lower() if _GIT_SHA_RE.fullmatch(supplied_sha) else None
    return BuildIdentity(read_version(version_path), git_sha)


def next_internal_version(current: str, today: date | None = None) -> str:
    """Return the next daily publication ordinal for the fixed UTC+8 date."""
    build_date = today or datetime.now(_UTC_PLUS_8).date()
    parsed = parse_internal_version(current)
    if parsed is None:
        raise ValueError(f"current version is not a valid internal version: {current!r}")
    previous_date, previous_ordinal = parsed
    ordinal = previous_ordinal + 1 if previous_date == build_date else 1
    if ordinal > 99:
        raise ValueError(f"daily publication ordinal exceeded 99 for {build_date:%Y%m%d}")
    return f"dev.{build_date:%Y%m%d}.{ordinal:02d}"


def format_startup_line(identity: BuildIdentity) -> str:
    """Format the one-line version record used by the desktop startup log."""
    provenance = identity.git_sha or "not recorded"
    return (f"CoatSim startup · internal version {identity.version} · "
            f"Git commit {provenance}")


def format_git_commit_label(identity: BuildIdentity) -> str:
    """Explain the optional revision value without implying a Git lookup."""
    if identity.git_sha is None:
        return "Git 提交：未记录（源码归档或未提供构建元数据）"
    return f"Git 提交：{identity.git_sha}"


BUILD_IDENTITY = read_build_identity()
APP_VERSION = BUILD_IDENTITY.version
