from __future__ import annotations

from datetime import date
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("previous,current,accepted", [
    ("dev.20261019.06", "dev.20261023.01", True),
    ("dev.20261023.09", "dev.20261023.10", True),
    ("dev.20261023.10", "dev.20261023.10", False),
    ("dev.20261023.10", "dev.20261023.09", False),
    ("dev.20261024.01", "dev.20261023.99", False),
    ("unknown", "dev.20261023.01", False),
])
def test_publication_check_requires_real_version_advancement(tmp_path, monkeypatch, capsys,
                                                          previous, current, accepted):
    from tools import app_version

    version = tmp_path / "version.txt"
    version.write_text(current + "\n", encoding="utf-8")
    monkeypatch.setattr(app_version, "VERSION_FILE", version)
    assert app_version.main(["check", "--previous-version", previous]) == (0 if accepted else 1)
    output = capsys.readouterr()
    assert bool(output.err) is not accepted
    assert version.read_text(encoding="utf-8") == current + "\n"


@pytest.mark.parametrize("contents", [None, "unknown\n", "dev.20260230.01\n"])
def test_publication_check_missing_or_invalid_version_is_an_explicit_error(tmp_path, monkeypatch, capsys, contents):
    from tools import app_version

    version = tmp_path / "version.txt"
    if contents is not None:
        version.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(app_version, "VERSION_FILE", version)
    assert app_version.main(["check", "--previous-version", "dev.20261019.06"]) == 1
    assert str(version) in capsys.readouterr().err


def test_packaged_module_uses_its_own_authoritative_version_resource(tmp_path):
    # Model the existing frozen-resource layout, without an installed app or
    # importing the source-tree module as a hidden fallback.
    packaged = tmp_path / "installed" / "coat_sim"
    packaged.mkdir(parents=True)
    shutil.copy2(VERSION_FILE, packaged / "version.txt")
    shutil.copy2(VERSION_FILE.with_name("version.py"), packaged / "version.py")
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('packaged_version',{str(packaged / 'version.py')!r}); "
        "module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; "
        "spec.loader.exec_module(module); print(module.APP_VERSION)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == read_version() != UNKNOWN_VERSION
    (packaged / "version.txt").unlink()
    missing = subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True, text=True)
    assert missing.stdout.strip() == UNKNOWN_VERSION
