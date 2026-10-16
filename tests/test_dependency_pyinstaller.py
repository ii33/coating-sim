"""Only source terminal installers are supported; retired paths stay inert."""
from pathlib import Path
from unittest.mock import patch
import subprocess

from tools import build_dependency_manager_pyinstaller as build

ROOT = Path(__file__).resolve().parents[1]


def test_packaging_cannot_be_reenabled_by_test_build():
    with patch.object(subprocess, "run") as run:
        for entry in (build.main, build._historical_validation):
            for args in ([], ["--dry-run"], ["--test-build", "--output", "unused"]):
                assert entry(args) == 2
        run.assert_not_called()


def test_obsolete_freezer_specs_are_absent():
    for name in ("freeze_setup.py", "pyinstaller.spec"):
        assert not (ROOT / "tools/dependency_manager" / name).exists()


def test_source_installers_retain_managed_runtime_manifests():
    for name in ("runtime_manifest.json", "git_runtime_manifest.json"):
        assert (ROOT / "tools/dependency_manager" / name).is_file()
    for name in ("install.bat", "install.sh"):
        assert (ROOT / name).is_file()


def test_installer_does_not_require_a_third_party_builder():
    lines = (ROOT / "requirements-dependency-manager-build.txt").read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_windows_acceptance_is_read_only_source_terminal():
    source = (ROOT / "tools/dependency_manager/acceptance_windows.ps1").read_text(encoding="utf-8")
    assert '"install.bat"' in source
    assert "-BootstrapPython $BootstrapPython" in source
    assert "Start-Process" not in source
    assert "deposition-deps" not in source
    assert "--approve" not in source
