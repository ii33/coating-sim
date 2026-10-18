"""Packaged public references are available offline; private routes stay explicit."""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, Mock

import pytest

from coat_sim.agent import research_library as library, worker


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "package" / "resources" / "research_library"
    root.mkdir(parents=True)
    payload = b"# Optical research\nITO thin film optical constants\n"
    (root / "optics.md").write_bytes(payload)
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "version": "test-1", "coverage_note": "Selected public notes only.",
        "documents": [{"path": "optics.md", "sha256": hashlib.sha256(payload).hexdigest()}]}))
    monkeypatch.setattr(library, "builtin_research_library_root", lambda: root)
    monkeypatch.setattr(worker, "_research_library_sidecar_path", lambda: tmp_path / "absent.json")
    return root


def test_fresh_defaults_search_read_without_private_context(bundle, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, state, message, metadata = worker._configured_research_library({
        "assistant_toolkit_allow_model_context": False})
    assert state == "connected_builtin_read_only"
    assert metadata is not None
    assert metadata["source_kind"] == "builtin_public"
    tools = {t.__name__: t for t in library.assistant_tools(provider, allow_model_context=True)}
    result = tools["research_library_search"]("ITO")
    assert result["ok"] and result["results"] and result["version"] == "test-1"
    assert tools["research_library_read"]("optics.md")["ok"]
    assert library.status_tool(provider, allow_model_context=True)()["source_kind"] == "builtin_public"


def test_external_route_retains_separate_context_gate(bundle, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    (root / "secret.md").write_text("PRIVATE")
    provider, state, _, _ = worker._configured_research_library({"assistant_research_library_root": str(root)})
    assert provider is not None
    assert state == "connected_read_only" and provider.source_kind == "external_private"
    assert library.assistant_tools(provider, allow_model_context=False) == []
    assert library.status_tool(provider, allow_model_context=False)()["status"] == "context_not_authorized"


@pytest.mark.parametrize("root", ["relative", "/nonexistent-optics-library", 42])
def test_invalid_explicit_root_never_falls_back(bundle, root):
    provider, state, _, _ = worker._configured_research_library({"assistant_research_library_root": root})
    assert provider is None and state == "unreadable"


def test_missing_builtin_is_installation_error(bundle):
    (bundle / "optics.md").unlink()
    provider, state, message, _ = worker._configured_research_library({})
    assert provider is None and state == "builtin_missing"
    assert "重新安装" in message
    result = library.status_tool(provider, readiness={"status": state})()
    assert "重新安装" in result["setup"]
    assert "assistant_research_library_root" not in result["setup"]


@pytest.mark.parametrize("path", ["../optics.md", "/etc/passwd", "C:\\secret.md", "unlisted.md", "manifest.json"])
def test_model_paths_cannot_escape_or_expand_manifest(bundle, path):
    (bundle / "unlisted.md").write_text("UNLISTED")
    provider = library.BuiltinResearchLibrary()
    tools = {t.__name__: t for t in library.assistant_tools(provider, allow_model_context=True)}
    assert not tools["research_library_read"](path)["ok"]
    assert not provider.search("UNLISTED")["results"]


def test_corrupted_content_does_not_reach_model(bundle):
    provider = library.BuiltinResearchLibrary()
    (bundle / "optics.md").write_text("CORRUPTED")
    assert provider.search("CORRUPTED")["error"] == "builtin_missing"
    assert not provider.read("optics.md")["ok"]


def test_worker_forwards_builtin_kind_and_readiness(bundle):
    run = AsyncMock()
    asyncio.run(worker._run_task_with_research_library({"config": {}}, Mock(), run))
    assert run.call_args.kwargs["allow_research_library_context"] is True
    readiness = run.call_args.kwargs["research_library_readiness"]
    assert readiness["source_kind"] == "builtin_public"
    assert readiness["status"] == "connected_builtin_read_only"


def test_manifest_cannot_name_traversal_or_symlink(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["documents"][0]["path"] = "../escape.md"
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    assert worker._configured_research_library({})[1] == "builtin_missing"


def test_live_revocation_withholds_public_and_private_results(bundle):
    tools = library.assistant_tools(library.BuiltinResearchLibrary(), allow_model_context=True,
                                   live_grant=lambda: False)
    assert tools[0]("ITO")["error"] == "permission_revoked"


def test_resource_path_is_relative_to_installed_or_frozen_module(tmp_path, monkeypatch):
    module = tmp_path / "installed" / "coat_sim" / "agent" / "research_library.py"
    monkeypatch.setattr(library, "__file__", str(module))
    monkeypatch.chdir(tmp_path)
    assert library.builtin_research_library_root() == module.parent / "resources" / "research_library"


def test_symlink_in_manifest_cannot_expand_access(bundle, tmp_path):
    outside = tmp_path / "private.md"
    outside.write_text("PRIVATE")
    (bundle / "optics.md").unlink()
    try:
        (bundle / "optics.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert worker._configured_research_library({})[1] == "builtin_missing"


def test_shipped_corpus_is_readable_without_local_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "_research_library_sidecar_path", lambda: tmp_path / "absent.json")
    monkeypatch.chdir(tmp_path)
    provider, state, _, metadata = worker._configured_research_library({})
    assert provider is not None and metadata is not None
    assert state == "connected_builtin_read_only" and metadata["included_files"] > 0
    manifest = json.loads((library.builtin_research_library_root() / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["documents"]:
        assert provider.read(entry["path"])["ok"]
    result = provider.search("ITO")
    assert result["ok"] and result["results"] and not result["incomplete"]
