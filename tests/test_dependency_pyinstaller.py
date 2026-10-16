"""The retired packaging entry must never start a bundler."""
from unittest.mock import patch
from tools import build_dependency_manager_pyinstaller as build


def test_packaging_cannot_be_reenabled_by_test_build():
    with patch.object(build.subprocess, "run") as run:
        assert build.main(["--test-build", "--output", "unused"]) == 2
        run.assert_not_called()


def test_retained_resource_specs_include_managed_git_manifest():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ("freeze_setup.py", "pyinstaller.spec"):
        source = (root / "tools/dependency_manager" / name).read_text(encoding="utf-8")
        assert "tools/dependency_manager/git_runtime_manifest.json" in source
    assert (root / "tools/dependency_manager/git_runtime_manifest.json").is_file()
