"""Source-build development access stays within this project."""
from __future__ import annotations

from pathlib import Path

import pytest

import launcher
from coat_sim.agent import development, runtime


@pytest.mark.parametrize("flags,release,frozen,compiled,enabled", [
    ([], False, False, False, True),
    (["--no-development"], False, False, False, False),
    (["--development"], True, False, False, False),
    ([], False, True, False, False),
    ([], False, False, True, False),
])
def test_source_launch_default_and_release_gates(flags, release, frozen, compiled, enabled):
    argv = ["launcher.py", *flags]
    env = {"COAT_DEVELOPMENT": "1"}
    assert launcher._configure_development_mode(
        argv, env, release_build=release, frozen=frozen, compiled=compiled) is enabled
    assert env["COAT_DEVELOPMENT"] == ("1" if enabled else "0")
    assert argv == ["launcher.py"]


def test_default_source_and_log_reads_are_project_only(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "coat_sim").mkdir(parents=True)
    (root / "artifacts" / "logs").mkdir(parents=True)
    (root / "tests").mkdir()
    source = root / "coat_sim" / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    log = root / "artifacts" / "logs" / "assistant.log"
    log.write_text("diagnostic\n", encoding="utf-8")
    secret = root / "coat_sim" / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")
    project_data = root / "data.json"
    project_data.write_text("{}", encoding="utf-8")
    outside = tmp_path / "other.py"
    outside.write_text("OTHER = 1", encoding="utf-8")
    monkeypatch.setattr(development, "ROOT", root)
    monkeypatch.setattr(development, "_READ_ACCESS_UNTIL", 0.0)
    monkeypatch.setenv("COAT_DEVELOPMENT", "1")
    monkeypatch.delenv("COAT_RELEASE_BUILD", raising=False)

    assert "VALUE = 1" in development.developer_files(str(source), "read")["content"]
    assert "diagnostic" in development.developer_files(str(log), "read")["content"]
    names = {entry["name"] for entry in development.developer_files("coat_sim", "list")["entries"]}
    assert "sample.py" in names and ".env" not in names
    for blocked in (secret, project_data, outside):
        with pytest.raises(PermissionError):
            development.developer_files(str(blocked), "read")
    development.developer_access("grant", duration_s=30)
    assert development.developer_files(str(project_data), "read")["content"] == "{}"
    for blocked in (secret, outside):
        with pytest.raises(PermissionError):
            development.developer_files(str(blocked), "read")
    development.developer_access("revoke")


def test_release_gate_and_discussion_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(development, "ROOT", tmp_path)
    monkeypatch.setenv("COAT_DEVELOPMENT", "1")
    monkeypatch.setenv("COAT_RELEASE_BUILD", "1")
    with pytest.raises(PermissionError):
        development.developer_files(str(Path(__file__)), "stat")
    assert "developer_files" not in runtime.allowed_project_tools("chat", development=True)
    assert "developer_checks" not in runtime.allowed_project_tools("chat", development=True)
