"""Closed read-only RII tools preserve the configured owner's snapshot boundary."""
import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent.runtime import allowed_project_tools
from coat_sim.mcp import server

pytestmark = pytest.mark.contract

SNAPSHOT = "a" * 40 + "-" + "b" * 16 + "-v1"
ENTRY = "rii_" + "c" * 24
TOOLS = {"refractiveindex_status", "refractiveindex_search", "refractiveindex_detail",
         "refractiveindex_preview", "refractiveindex_convert"}


def call(name, arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


@pytest.fixture
def owner(tmp_path, monkeypatch):
    from coat_sim.core import refractiveindex_local

    calls: list[tuple[Any, ...]] = []
    configured = tmp_path / "configured-reference-store"

    class Store:
        def __init__(self, store_root, snapshot_id=None):
            calls.append(("open", store_root, snapshot_id))

        def __getattr__(self, method):
            assert method in {"status", "search", "get_entry", "preview", "convert_entry"}

            def invoke(**arguments):
                calls.append((method, arguments))
                return {"snapshot_id": SNAPSHOT, "method": method, "arguments": arguments}

            return invoke

    monkeypatch.setattr(refractiveindex_local, "default_store_root", lambda: configured)
    monkeypatch.setattr(refractiveindex_local, "RefractiveIndexLocalStore", Store)
    return calls, configured


def test_local_tools_are_closed_readonly_and_available_in_discussion():
    catalog = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert TOOLS <= allowed_project_tools("chat")
    assert TOOLS <= allowed_project_tools("operate")
    assert TOOLS <= allowed_project_tools("research", background=True)
    assert {name for name in catalog if name.startswith("refractiveindex_")} == TOOLS
    for name in TOOLS:
        tool = catalog[name]
        assert tool.input_schema["additionalProperties"] is False
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert not ({"store", "store_root", "source_dir", "path", "build", "download", "import"}
                    & set(tool.input_schema.get("properties", {})))


@pytest.mark.parametrize("name,arguments", [
    ("refractiveindex_status", {"store_root": "/other"}),
    ("refractiveindex_status", {"snapshot_id": "../../other"}),
    ("refractiveindex_search", {"query": 42}),
    ("refractiveindex_search", {"query": "x" * 201}),
    ("refractiveindex_search", {"catalog": "k"}),
    ("refractiveindex_search", {"capability": "lossless"}),
    ("refractiveindex_search", {"cataloged": 1}),
    ("refractiveindex_search", {"offset": True}),
    ("refractiveindex_search", {"offset": 1.0}),
    ("refractiveindex_search", {"offset": 1000001}),
    ("refractiveindex_search", {"limit": 0}),
    ("refractiveindex_search", {"limit": 101}),
    ("refractiveindex_search", {"shelf": ""}),
    ("refractiveindex_detail", {"entry_id": "main/SiO2/nk/Malitson.yml"}),
    ("refractiveindex_detail", {"entry_id": ENTRY, "raw_limit": -1}),
    ("refractiveindex_detail", {"entry_id": ENTRY, "raw_limit": 100001}),
    ("refractiveindex_detail", {"entry_id": ENTRY, "raw_limit": False}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "max_points": 1}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "max_points": 1001}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "block_indices": []}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "block_indices": [True]}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "block_indices": [-1]}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "block_indices": [1, 1]}),
    ("refractiveindex_preview", {"entry_id": ENTRY, "block_indices": list(range(65))}),
    ("refractiveindex_convert", {"entry_id": ENTRY, "optical_set_id": ""}),
    ("refractiveindex_convert", {"entry_id": ENTRY, "optical_set_id": "n", "block_selection": [0.0]}),
    ("refractiveindex_convert", {"entry_id": ENTRY, "optical_set_id": "n", "block_selection": [0, 0]}),
    ("refractiveindex_convert", {"entry_id": ENTRY, "optical_set_id": "n", "profile": {}}),
    ("refractiveindex_convert", {"entry_id": ENTRY, "optical_set_id": "n", "save": True}),
])
def test_bad_envelopes_are_rejected_before_sdk_coercion(name, arguments):
    with patch.object(MCPServer, "call_tool", new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match="invalid_arguments"):
            call(name, arguments)
        sdk.assert_not_called()


@pytest.mark.parametrize("name,method,arguments", [
    ("refractiveindex_status", "status", {}),
    ("refractiveindex_search", "search", {
        "query": "SiO2", "catalog": "all", "shelf": "main", "book": "SiO2",
        "capability": "supported", "cataloged": True, "offset": 10, "limit": 2}),
    ("refractiveindex_detail", "get_entry", {"entry_id": ENTRY, "raw_limit": 0}),
    ("refractiveindex_preview", "preview", {"entry_id": ENTRY, "block_indices": [0, 2], "max_points": 10}),
    ("refractiveindex_convert", "convert_entry", {
        "entry_id": ENTRY, "optical_set_id": "selected", "name": "Exact selection", "block_selection": [1]}),
])
def test_handlers_forward_exact_snapshot_and_arguments_to_configured_owner(owner, name, method, arguments):
    calls, configured = owner
    result = getattr(server, name)(snapshot_id=SNAPSHOT, **arguments)
    assert result["snapshot_id"] == SNAPSHOT
    assert calls == [("open", configured, SNAPSHOT), (method, arguments)]
    assert not configured.exists()


def test_real_missing_status_is_read_only_and_uses_trusted_configuration(tmp_path, monkeypatch):
    missing = tmp_path / "never-created" / "reference-store"
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(missing))
    before = list(tmp_path.rglob("*"))
    result = server.refractiveindex_status()
    assert result["status"] == "missing"
    assert list(tmp_path.rglob("*")) == before
    assert not missing.exists()


def test_core_errors_keep_their_domain_code(owner, monkeypatch):
    from coat_sim.core import refractiveindex_local

    class SourceChanged(ValueError):
        code = "SOURCE_INTEGRITY_ERROR"

    def refuse(*args, **kwargs):
        raise SourceChanged("source hash mismatch")

    monkeypatch.setattr(refractiveindex_local, "RefractiveIndexLocalStore", refuse)
    with pytest.raises(ToolError, match="SOURCE_INTEGRITY_ERROR"):
        server.refractiveindex_detail(ENTRY)


@pytest.mark.parametrize("extra_bytes,allowed", [(0, True), (1, False)])
def test_full_conversion_response_has_exact_one_mib_json_boundary(monkeypatch, extra_bytes, allowed):
    result = {"optical_set": {"notes": ""}, "provenance": {"snapshot_id": SNAPSHOT}}
    overhead = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    result["optical_set"]["notes"] = "x" * (1024 * 1024 - overhead + extra_bytes)
    calls = []

    def converted(*args, **kwargs):
        calls.append("convert")
        return result

    monkeypatch.setattr(server, "_refractiveindex_call", converted)
    if allowed:
        assert server.refractiveindex_convert(ENTRY, "selected") is result
    else:
        with pytest.raises(ToolError, match="REFRACTIVEINDEX_RESULT_TOO_LARGE"):
            server.refractiveindex_convert(ENTRY, "selected")
    assert calls == ["convert"]
    assert len(result["optical_set"]["notes"]) == 1024 * 1024 - overhead + extra_bytes


def test_conversion_limit_counts_utf8_bytes_without_silently_truncating(monkeypatch):
    result = {"optical_set": {"notes": "中" * (400 * 1024)}}
    monkeypatch.setattr(server, "_refractiveindex_call", lambda *args, **kwargs: result)
    with pytest.raises(ToolError, match="not truncated or sampled"):
        server.refractiveindex_convert(ENTRY, "selected")
    assert len(result["optical_set"]["notes"]) == 400 * 1024
