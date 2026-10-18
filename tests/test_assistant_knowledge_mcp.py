"""Strict MCP knowledge envelopes, read-only discovery and explicit project admission."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import allowed_project_tools
from coat_sim.core.api import CoatService
from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.core.project_research import ProjectResearchStore
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from coat_sim.project_management import ProjectManagementService

pytestmark = pytest.mark.contract
TOOLS = {"algorithm_reference_catalog", "algorithm_reference_search", "algorithm_reference_read",
         "project_knowledge_search", "material_data_readiness"}


def call(name, arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


@pytest.fixture
def projects(tmp_path, monkeypatch):
    chosen = ProjectStore.create(tmp_path / "chosen", "Chosen")
    other = ProjectStore.create(tmp_path / "other", "Other")
    monkeypatch.setattr(server, "_project_management_service", lambda: ProjectManagementService(
        authorized_roots=(chosen.root,)))
    return chosen, other


def test_all_new_tools_are_closed_readonly_and_discussion_available():
    catalog = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert TOOLS <= allowed_project_tools("chat")
    for name in TOOLS:
        tool = catalog[name]
        assert tool.input_schema["additionalProperties"] is False
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True


@pytest.mark.parametrize("tool,arguments", [
    ("algorithm_reference_catalog", {"offset": True}),
    ("algorithm_reference_catalog", {"offset": 1.0}),
    ("algorithm_reference_catalog", {"limit": 0}),
    ("algorithm_reference_catalog", {"path": "../../secret"}),
    ("algorithm_reference_search", {"query": 1}),
    ("algorithm_reference_search", {"query": "a", "limit": 51}),
    ("algorithm_reference_read", {"section_id": "flux", "max_chars": "6000"}),
    ("algorithm_reference_read", {"section_id": "flux", "max_chars": 6001}),
    ("project_knowledge_search", {"root": "p.coatproj", "project_id": "p", "include_archived": 1}),
    ("project_knowledge_search", {"root": "p.coatproj", "project_id": "p", "offset": True}),
    ("project_knowledge_search", {"root": "p.coatproj", "project_id": "p", "scan_other_projects": True}),
    ("material_data_readiness", {"identifier": "builtin_zns", "wavelength_min_nm": True, "wavelength_max_nm": 600.0}),
    ("material_data_readiness", {"identifier": "builtin_zns", "wavelength_min_nm": "400", "wavelength_max_nm": 600.0}),
    ("material_data_readiness", {"identifier": "builtin_zns", "wavelength_min_nm": 0.0, "wavelength_max_nm": 600.0}),
    ("material_data_readiness", {"identifier": "builtin_zns", "wavelength_min_nm": 400.0, "wavelength_max_nm": 600.0, "assume_lossless": "yes"}),
    ("material_data_readiness", {"identifier": "builtin_zns", "wavelength_min_nm": 400.0, "wavelength_max_nm": 600.0, "material": {"id": "fake"}}),
])
def test_invalid_mcp_arguments_never_reach_handlers(tool, arguments):
    with patch.object(MCPServer, "call_tool", new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match="invalid_arguments"):
            call(tool, arguments)
        sdk.assert_not_called()


def test_live_registered_algorithm_handler_reads_only_fixed_resource():
    result = call("algorithm_reference_catalog", {"limit": 1})
    assert result.is_error is False
    text = " ".join(content.text for content in result.content if hasattr(content, "text"))
    assert "docs/algorithms.html" in text and "version" in text


def test_authorization_precedes_knowledge_loading(projects):
    chosen, other = projects
    with patch("coat_sim.core.project_knowledge.ProjectKnowledge") as projection:
        with pytest.raises(ToolError, match="project_root_not_authorized"):
            server.project_knowledge_search(str(other.project_file), other.manifest["id"])
        with pytest.raises(ToolError, match="project_identity_mismatch"):
            server.project_knowledge_search(str(chosen.project_file), other.manifest["id"])
        projection.assert_not_called()


def test_union_read_references_route_to_original_tools_and_preserve_project(projects):
    project, _ = projects
    ProjectBbsService(project).create_post("journal", "Optical note", "optical record")
    ProjectResearchStore(project.project_file, project.manifest["id"]).save({
        "title": "Optical paper", "content": "optical source", "parse_status": "parsed"})
    result = server.project_knowledge_search(str(project.project_file), project.manifest["id"], "optical")
    assert len(result["results"]) == 2
    for row in result["results"]:
        assert row["read_arguments"]["root"] == str(project.project_file)
        assert row["read_arguments"]["project_id"] == project.manifest["id"]
        read = getattr(server, row["read_tool"])(**row["read_arguments"])
        assert read["project_id"] == project.manifest["id"]


def test_material_readiness_reads_real_library_without_creating_lock(tmp_path, monkeypatch):
    service = CoatService(root=tmp_path)
    monkeypatch.setattr(server, "service", service)
    from coat_sim.core.material_business import ApplicationMaterialCatalog
    catalog = ApplicationMaterialCatalog(service.material_library().directory)
    identity = catalog.catalog_records()[0]["record"]["id"]
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    result = server.material_data_readiness(identity, 400.0, 700.0)
    assert result["material_id"] == identity
    assert result["source_type"] == "effective_material_library"
    assert result["library_record"]["builtin"] is True
    assert result["untrusted_reference"] is True
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert not (tmp_path / "data/materials/.material-library.lock").exists()
    with pytest.raises(ToolError, match="material_not_found"):
        server.material_data_readiness("invented_material", 400.0, 700.0)
