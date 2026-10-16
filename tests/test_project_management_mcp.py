"""MCP exposes explicit bounded project operations without foreground routing."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.core.api import CoatService
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from coat_sim.project_management import ProjectManagementService
from coat_sim.project_recovery import ProjectOwnerLease

pytestmark = pytest.mark.contract


@pytest.fixture
def bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "service", CoatService(tmp_path))
    monkeypatch.setattr(AssistantProcessClient, "list_clients", lambda _self: [])
    return tmp_path


def _value(raw):
    if isinstance(raw, tuple):
        return raw[1]
    if isinstance(raw, dict):
        return raw
    value = getattr(raw, "structured_content", None)
    if value is not None:
        return value
    return json.loads(raw.content[0].text)


def test_mcp_catalog_and_real_tool_call_preserve_explicit_identity(bounded):
    created = _value(asyncio.run(server.mcp.call_tool("project_manage", {
        "operation": "create", "root": str(bounded / "project"),
        "operation_id": "create-one", "parameters": {"name": "One"}})))
    project_id = created["project_id"]
    inspected = _value(asyncio.run(server.mcp.call_tool("project_inspect", {
        "root": created["project_file"], "project_id": project_id})))
    assert inspected["project_id"] == project_id
    assert server.project_resolve(created["project_file"], project_id)["graph"] is None
    with pytest.raises(ToolError, match="project_identity_mismatch"):
        server.project_inspect(created["project_file"], "other-project")
    catalog = server.project_management_catalog()
    assert catalog["identity_fields"] == ["root", "project_id"]
    assert "expected_revision" in catalog["mutation_fields"]


def test_mcp_rejects_outside_root_identity_override_and_active_gui_owner(bounded, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    with pytest.raises(ToolError, match="project_root_not_authorized"):
        server.project_manage("create", str(outside), operation_id="create-outside", parameters={"name": "Outside"})
    assert not outside.exists()
    with pytest.raises(ToolError, match="identity/revision"):
        server.project_manage("create", str(bounded / "project"), operation_id="bad", parameters={"root": str(outside), "name": "Bad"})
    store = ProjectStore.create(bounded / "project", "Active")
    with ProjectOwnerLease.acquire(store.project_file, store.manifest["id"]):
        with pytest.raises(ToolError, match="active_project_owner"):
            server.project_manage("set_default", str(store.project_file), project_id=store.manifest["id"],
                                  operation_id="default-one", expected_revision=0,
                                  parameters={"configuration_id": None})
    assert ProjectStore.open(store.project_file).manifest["revision"] == 0


def test_mcp_strict_revision_and_agent_allowlists(bounded):
    from coat_sim.agent.runtime import MAIN_TOOLS, READ_TOOLS, EXECUTION_TOOLS, COORDINATOR_TOOLS
    read = {"project_management_catalog", "project_list", "project_inspect", "project_resolve"}
    assert read <= READ_TOOLS
    assert read <= COORDINATOR_TOOLS
    assert "project_manage" in MAIN_TOOLS and "project_manage" in EXECUTION_TOOLS["design"]
    assert "project_manage" not in READ_TOOLS
    store = ProjectStore.create(bounded / "project", "Strict")
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("project_manage", {
            "operation": "set_default", "root": str(store.project_file), "project_id": store.manifest["id"],
            "operation_id": "bool-revision", "expected_revision": True, "parameters": {"configuration_id": None}}))
    assert ProjectStore.open(store.project_file).manifest["revision"] == 0


def test_authorized_root_collection_is_covariant_and_keeps_registered_scope(bounded, tmp_path, monkeypatch):
    selected = ProjectStore.create(tmp_path.parent / (tmp_path.name + "-selected"), "Selected")
    unavailable = ProjectStore.create(tmp_path.parent / (tmp_path.name + "-unavailable"), "Unavailable")
    monkeypatch.setattr(AssistantProcessClient, "list_clients", lambda _self: [
        {"state": "alive", "project_sessions": [{"root": str(selected.root)}]},
        {"state": "unavailable", "project_sessions": [{"root": str(unavailable.root)}]},
    ])
    original = ProjectManagementService.__init__

    def initialize(self, authorized_roots: list[Path | str] | tuple[Path | str, ...] | None = None):
        assert isinstance(authorized_roots, tuple)
        original(self, authorized_roots)

    monkeypatch.setattr(ProjectManagementService, "__init__", initialize)
    assert server.project_inspect(str(selected.project_file), selected.manifest["id"])["project_id"] == selected.manifest["id"]
    with pytest.raises(ToolError, match="project_root_not_authorized"):
        server.project_inspect(str(unavailable.project_file), unavailable.manifest["id"])


def test_project_mcp_runs_when_qt_imports_are_denied(tmp_path):
    import os
    import subprocess
    import sys
    source = Path(__file__).resolve().parents[1]
    code = '''import importlib.abc, json, sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("PySide6"):
            raise AssertionError("Project MCP must not import Qt")
sys.meta_path.insert(0, NoQt())
from coat_sim.mcp import server
from coat_sim.core.api import CoatService
from coat_sim.assistant_process import AssistantProcessClient
server.service = CoatService(sys.argv[1])
AssistantProcessClient.list_clients = lambda self: []
created = server.project_manage("create", sys.argv[1] + "/project", operation_id="headless", parameters={"name":"Headless"})
inspected = server.project_inspect(created["project_file"], created["project_id"])
assert inspected["project_id"] == created["project_id"]
print(json.dumps({"ok": True, "qt_loaded": any(name.startswith("PySide6") for name in sys.modules)}))
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=source,
                            env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True,
                            text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True, "qt_loaded": False}
