"""Explicit project authorization and discussion policy for evidence tools."""
import asyncio
import hashlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import MAIN_TOOLS, READ_TOOLS, allowed_project_tools
from coat_sim.agent.tool_dispatch import ProjectGateway
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from coat_sim.project_management import ProjectManagementService
from tools.distribution_resources import _validate_package_relative_path


@pytest.fixture
def projects(tmp_path, monkeypatch):
    chosen = ProjectStore.create(tmp_path / "chosen", "Chosen")
    other = ProjectStore.create(tmp_path / "other", "Other")
    monkeypatch.setattr(server, "_project_management_service", lambda: ProjectManagementService(
        authorized_roots=(chosen.root,)))
    return chosen, other


def test_saved_reference_can_be_read_without_changing_design_or_materials(projects):
    project, _ = projects
    before = project.project_file.read_bytes()
    before_materials = sorted((project.root / "data/materials").rglob("*"))
    record = {"title": "Selected reference", "content": "A source claim, not a simulation",
              "doi": "10.1234/example", "original_url": "https://example.org/paper",
              "license": "cc-by", "parse_status": "metadata_only",
              "statement_kind": "source_statement"}
    saved = server.project_research_save(str(project.project_file), project.manifest["id"], record)
    assert saved["ok"] is True
    assert "content" not in saved["record"]
    listed = server.project_research_search(str(project.project_file), project.manifest["id"], "source")
    assert listed["total"] == 1
    read = server.project_research_read(str(project.project_file), project.manifest["id"], saved["evidence_id"])
    assert read["text"] == record["content"]
    assert read["record"]["content_sha256"] == hashlib.sha256(record["content"].encode()).hexdigest()
    assert read["record"]["statement_kind"] == "source_statement"
    assert project.project_file.read_bytes() == before
    assert sorted((project.root / "data/materials").rglob("*")) == before_materials


def test_unknown_project_and_wrong_persisted_id_are_rejected_before_storage(projects):
    chosen, other = projects
    with pytest.raises(ToolError, match="project_root_not_authorized"):
        server.project_research_search(str(other.project_file), other.manifest["id"])
    with pytest.raises(ToolError, match="project_identity_mismatch"):
        server.project_research_search(str(chosen.project_file), other.manifest["id"])
    assert not (other.root / "data/research").exists()
    assert not (chosen.root / "data/research").exists()


def test_discussion_gateway_blocks_save_before_dispatch_and_keeps_reads():
    class Client:
        calls = []
        async def list_tools(self):
            return []
        async def call_tool(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("Discussion must reject before dispatch")
    client = Client()
    gateway = ProjectGateway(client, MAIN_TOOLS, READ_TOOLS, lambda value: value, read_only_mode=True)
    reply = asyncio.run(gateway.execute("project_research_save", {
        "root": "explicit.coatproj", "project_id": "a" * 32, "record": {"content": "must not save"}}))
    assert reply["error"]["code"] == "discussion_read_only"
    assert client.calls == []
    assert {"project_research_search", "project_research_read"} <= allowed_project_tools("chat")
    assert "project_research_save" not in allowed_project_tools("chat")
    assert "project_research_save" in allowed_project_tools("operate")


@pytest.mark.parametrize("path", ["data/research/id/paper.json", "nested/DATA/Research/file.pdf"])
def test_research_evidence_cannot_be_packaged_even_if_force_added(path):
    with pytest.raises(RuntimeError, match="research evidence"):
        _validate_package_relative_path(path, context="fixture")
