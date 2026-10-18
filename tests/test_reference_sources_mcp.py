"""Source discovery is an inert versioned registry, never a downloader."""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import allowed_project_tools
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.mcp import server

pytestmark = pytest.mark.contract
TOOLS = {"reference_sources_catalog", "reference_sources_detail"}


def call(name, arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_source_registry_tools_are_closed_and_read_only_in_all_modes():
    catalog = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    for mode in ("chat", "operate", "research"):
        assert TOOLS <= allowed_project_tools(mode)
    for name in TOOLS:
        tool = catalog[name]
        assert tool.input_schema["additionalProperties"] is False
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert not ({"path", "url", "download", "install", "destination"}
                    & set(tool.input_schema.get("properties", {})))


@pytest.mark.parametrize("name,arguments", [
    ("reference_sources_catalog", {"query": 42}),
    ("reference_sources_catalog", {"query": "x" * 201}),
    ("reference_sources_catalog", {"category": "all"}),
    ("reference_sources_catalog", {"offline_status": "installed"}),
    ("reference_sources_catalog", {"offset": True}),
    ("reference_sources_catalog", {"offset": 1.0}),
    ("reference_sources_catalog", {"offset": -1}),
    ("reference_sources_catalog", {"offset": 10001}),
    ("reference_sources_catalog", {"limit": 0}),
    ("reference_sources_catalog", {"limit": 51}),
    ("reference_sources_catalog", {"expected_version": "../../file"}),
    ("reference_sources_catalog", {"url": "https://example.invalid"}),
    ("reference_sources_catalog", {"download": True}),
    ("reference_sources_detail", {"source_id": "../local.json"}),
    ("reference_sources_detail", {"source_id": "A"}),
    ("reference_sources_detail", {"source_id": "x" * 81}),
    ("reference_sources_detail", {"source_id": "rii", "expected_version": 42}),
    ("reference_sources_detail", {"source_id": "rii", "accept_license": True}),
])
def test_invalid_registry_envelopes_fail_before_the_sdk(name, arguments):
    with patch.object(MCPServer, "call_tool", new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match="invalid_arguments"):
            call(name, arguments)
        sdk.assert_not_called()


def test_catalog_forwards_filters_and_expected_version(monkeypatch):
    from coat_sim.core import reference_sources

    arguments: dict[str, Any] = {"query": "glass", "category": "substrates", "offline_status": "reference_only",
                 "offset": 2, "limit": 3, "expected_version": "a" * 64}
    calls = []

    def catalog(**kwargs):
        calls.append(kwargs)
        return {"version": "a" * 64, "total": 0, "items": []}

    monkeypatch.setattr(reference_sources, "reference_sources_catalog", catalog)
    assert server.reference_sources_catalog(**arguments)["total"] == 0
    assert calls == [arguments]


def test_detail_preserves_source_version_and_domain_errors(monkeypatch):
    from coat_sim.core import reference_sources

    calls = []

    def detail(source_id, **kwargs):
        calls.append((source_id, kwargs))
        raise ProjectError("reference_sources_changed", "registry version changed")

    monkeypatch.setattr(reference_sources, "reference_sources_detail", detail)
    with pytest.raises(ToolError, match="reference_sources_changed"):
        server.reference_sources_detail("rii", expected_version="a" * 64)
    assert calls == [("rii", {"expected_version": "a" * 64})]


def test_real_registry_pages_and_detail_retain_fixed_version_without_installing(tmp_path, monkeypatch):
    store = tmp_path / "not-installed"
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(store))
    first = server.reference_sources_catalog(limit=1)
    assert first["items"] and len(first["version"]) == 64
    source_id = first["items"][0]["id"]
    detail = server.reference_sources_detail(source_id, expected_version=first["version"])
    assert detail["version"] == first["version"]
    assert detail["source"]["id"] == source_id
    if first["next_offset"] is not None:
        second = server.reference_sources_catalog(offset=first["next_offset"], limit=1,
                                                  expected_version=first["version"])
        assert second["version"] == first["version"]
        assert second["items"][0]["id"] != source_id
    assert not store.exists() and list(tmp_path.iterdir()) == []
