"""Managed runtimes follow the selected project without global relocation."""
from typing import Any
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.dependency_manager import git_runtime, runtime
from tools.dependency_manager.runtime_paths import project_root

REGISTERED_PYTHON_PATHS = runtime._registered_python_paths


@pytest.fixture(autouse=True)
def no_registered_python(monkeypatch):
    monkeypatch.setattr(runtime, "_registered_python_paths", lambda version: [])


@pytest.mark.parametrize("module,planner", [(runtime, runtime.plan_runtime), (git_runtime, git_runtime.plan_git_runtime)])
def test_selected_project_outside_home_is_supported(tmp_path, module, planner):
    project = tmp_path / "project"
    project.mkdir()
    with patch.object(Path, "home", return_value=tmp_path / "different-home"):
        plan = planner(project=project, platform_name="win32", machine="AMD64")
    assert plan["status"] == "ready", plan["errors"]
    assert Path(plan["directory"]).parent == project / ".runtime"
    assert plan["project"] == str(project)
    assert not (project / ".runtime").exists()
    assert planner(tmp_path / "escape", project=project, platform_name="win32", machine="AMD64")["status"] == "blocked"


@pytest.mark.parametrize("planner", [runtime.plan_runtime, git_runtime.plan_git_runtime])
def test_project_symlink_escape_and_existing_target_are_blocked(tmp_path, planner):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".runtime").symlink_to(outside, target_is_directory=True)
    assert planner(project=project, platform_name="win32", machine="AMD64")["status"] == "blocked"
    (project / ".runtime").unlink()
    target = project / "existing"
    target.mkdir()
    assert planner(target, project=project, platform_name="win32", machine="AMD64")["status"] == "blocked"


def test_source_defaults_are_project_local_and_frozen_requires_selection(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert runtime.default_runtime_directory() == root / ".runtime/python"
    assert git_runtime.default_git_directory() == root / ".runtime/git"
    with patch("sys.frozen", True, create=True):
        with pytest.raises(ValueError, match="Select a project"):
            project_root()
        assert runtime.default_runtime_directory(tmp_path) == tmp_path / ".runtime/python"


def test_bootstrap_storage_and_installer_diagnostics_are_project_local():
    source = (Path(__file__).resolve().parents[1] / "tools/dependency_manager/bootstrap_windows.ps1").read_text(encoding="utf-8")
    assert '$BootstrapProjectRoot' in source
    assert 'Join-Path $homeDirectory ".runtime"' in source
    assert 'Join-Path $base "python"' in source
    assert 'Join-Path $homeDirectory ".dependency-manager\\logs"' in source
    assert 'SpecialFolder]::LocalApplicationData' not in source
    assert '-TimeoutSeconds 1800 -Capture -Installer' in source
    assert 'Installer log: ' in source
    assert 'parent process exit observed: ' in source
    assert '@("/log", $LogPath)' in source


def prepared_runtime(tmp_path, monkeypatch):
    plan = runtime.plan_runtime(project=tmp_path, platform_name="win32", machine="AMD64")
    monkeypatch.setattr(runtime, "plan_runtime", lambda *args, **kwargs: plan)
    monkeypatch.setattr(runtime, "_verify_system_ucrt", lambda: None)
    monkeypatch.setattr(runtime, "download_runtime", lambda artifact, path, *args: path.write_bytes(b"fixture"))
    return plan


@pytest.mark.parametrize("state,retained", [
    ({"started": True, "completed": False, "stopped": True}, True),
    ({"started": True, "completed": False, "stopped": False}, True),
    ({"started": False, "completed": False, "stopped": True}, False),
    ({"started": True, "completed": True, "stopped": False}, False),
])
def test_installer_resources_retained_only_on_uncertain_lifecycle(tmp_path, monkeypatch, state, retained):
    plan = prepared_runtime(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}
    def run(argv, *args, outcome, temporary_directory, **kwargs):
        outcome.update(state)
        captured.update(argv=argv, temporary=temporary_directory)
        return 130 if state["stopped"] else 1603
    monkeypatch.setattr(runtime, "run_command", run)
    result = runtime.execute_runtime(plan, confirmed=True)
    assert (tmp_path / ".runtime/runtime-install.lock").exists() is retained
    assert captured["temporary"].exists() is retained
    assert captured["temporary"].is_relative_to(tmp_path)
    assert captured["argv"][1:3] == ["/log", result["installer_log"]]
    assert Path(result["installer_log"]).is_relative_to(tmp_path)
    assert bool(result.get("retained_download")) is retained


def test_shared_bootstrap_lock_blocks_python_installer(tmp_path, monkeypatch):
    plan = prepared_runtime(tmp_path, monkeypatch)
    parent = tmp_path / ".runtime"
    parent.mkdir()
    lock = parent / "runtime-install.lock"
    lock.write_text("bootstrap owns this")
    monkeypatch.setattr(runtime, "run_command", lambda *a, **k: pytest.fail("must not start"))
    assert runtime.execute_runtime(plan, confirmed=True)["status"] == "failed"
    assert lock.read_text(encoding="utf-8") == "bootstrap owns this"
    source = (Path(__file__).resolve().parents[1] / "tools/dependency_manager/bootstrap_windows.ps1").read_text(encoding="utf-8")
    assert '$lockPath = Join-Path $parent "runtime-install.lock"' in source


def test_registry_change_during_download_prevents_execution(tmp_path, monkeypatch):
    plan = prepared_runtime(tmp_path, monkeypatch)
    def download(artifact, path, *args):
        path.write_bytes(b"fixture")
        monkeypatch.setattr(runtime, "_registered_python_paths", lambda version: [""])
    monkeypatch.setattr(runtime, "download_runtime", download)
    monkeypatch.setattr(runtime, "run_command", lambda *a, **k: pytest.fail("must not start"))
    result = runtime.execute_runtime(plan, confirmed=True)
    assert "registration appeared" in result["errors"][0]
    assert not list((tmp_path / ".runtime").glob("coat-runtime-*"))


@pytest.mark.parametrize("operation,relative", [("runtime-plan", ".runtime/python"), ("git-plan", ".runtime/git")])
def test_cli_selected_project_reaches_real_planner(tmp_path, capsys, operation, relative):
    import json
    from tools.dependency_manager.cli import main
    main([operation, "--project", str(tmp_path), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert result["project"] == str(tmp_path)
    assert result["directory"] == str(tmp_path / relative)


def test_registry_stale_and_suffixed_tags_still_conflict(monkeypatch):
    import sys
    from types import SimpleNamespace
    helper = REGISTERED_PYTHON_PATHS
    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def open_key(hive, name, *args):
        if name == "Software\\Python\\PythonCore":
            return Key()
        raise FileNotFoundError(name)
    tags = ["3.13-arm64", "3.13t", "3.12", "3.130"]
    registry = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
        KEY_WOW64_32KEY=4, KEY_WOW64_64KEY=8, KEY_READ=16,
        OpenKey=open_key, QueryInfoKey=lambda key: (len(tags), 0, 0),
        EnumKey=lambda key, index: tags[index])
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(sys, "platform", "win32")
    assert helper("3.13.16") == [""]
