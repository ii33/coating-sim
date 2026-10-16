from __future__ import annotations

from datetime import date
from pathlib import Path

from coat_sim.version import (
    BuildIdentity,
    UNKNOWN_VERSION,
    VERSION_FILE,
    format_git_commit_label,
    format_startup_line,
    next_internal_version,
    parse_internal_version,
    read_build_identity,
    read_version,
)


def test_checked_in_version_is_internal_publication_version() -> None:
    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert value != UNKNOWN_VERSION
    assert read_version() == value
    assert parse_internal_version(value) is not None


def test_missing_or_invalid_version_has_explicit_unknown_fallback(tmp_path: Path) -> None:
    assert read_version(tmp_path / "missing.txt") == UNKNOWN_VERSION
    invalid = tmp_path / "version.txt"
    invalid.write_text("dev.20260230.01\n", encoding="utf-8")
    assert read_version(invalid) == UNKNOWN_VERSION


def test_git_revision_is_optional_and_only_accepted_when_full_sha_supplied(tmp_path: Path) -> None:
    version = tmp_path / "version.txt"
    version.write_text("dev.20261014.01\n", encoding="utf-8")
    source_archive = read_build_identity(version, {})
    assert source_archive == BuildIdentity("dev.20261014.01", None)
    assert "not recorded" in format_startup_line(source_archive)
    assert "未记录" in format_git_commit_label(source_archive)

    sha = "A" * 40
    built = read_build_identity(version, {"COAT_SIM_GIT_SHA": sha})
    assert built.git_sha == sha.lower()
    assert format_startup_line(built).endswith(sha.lower())

    malformed = read_build_identity(version, {"COAT_SIM_GIT_SHA": "main"})
    assert malformed.git_sha is None


def test_daily_publication_ordinal_advances_and_resets_on_new_utc_plus_8_date() -> None:
    assert next_internal_version("dev.20261014.01", date(2026, 10, 14)) == "dev.20261014.02"
    assert next_internal_version("dev.20261014.99", date(2026, 10, 15)) == "dev.20261015.01"


def test_bump_rejects_invalid_value_and_ordinal_overflow() -> None:
    try:
        next_internal_version("unknown", date(2026, 10, 14))
    except ValueError:
        pass
    else:
        raise AssertionError("invalid version must not silently reset the publication sequence")

    try:
        next_internal_version("dev.20261014.99", date(2026, 10, 14))
    except ValueError:
        pass
    else:
        raise AssertionError("ordinal overflow must be explicit")
