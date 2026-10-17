"""Research MCP adapter protocol and bounded evidence; no live API calls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from coat_sim.mcp.research_server import create_server


class Source:
    def __init__(self):
        self.calls = []

    def search(self, query, limit=6, cursor=None):
        self.calls.append((query, limit, cursor))
        return {"ok": True, "source": "openalex", "results": [
            {"source": "openalex", "id": "https://openalex.org/W123", "title": "Fixture",
             "doi": "10.1234/example", "license": "cc-by", "has_content": {"pdf": True}}]}

    def lookup(self, identifier):
        self.calls.append(identifier)
        return {"ok": True, "source": "openalex", "work": {
            "source": "openalex", "id": "https://openalex.org/W123", "title": "Fixture"}}


def decoded(result):
    return json.loads(result.content[0].text)


def test_list_tools_is_inert_and_crossref_does_not_offer_fulltext():
    async def scenario():
        source = Source()
        server = create_server("openalex", source=source)
        assert {tool.name for tool in await server.list_tools()} == {
            "research_search", "research_lookup", "research_fulltext"}
        assert source.calls == []
        crossref = create_server("crossref", source=source)
        assert {tool.name for tool in await crossref.list_tools()} == {"research_search", "research_lookup"}
        assert source.calls == []
    asyncio.run(scenario())


def test_explicit_selection_no_key_and_invalid_pagination_never_download():
    async def scenario():
        called = []
        source = Source()
        server = create_server("openalex", source=source, fulltext_fetcher=lambda *a, **k: called.append(1))
        reply = decoded(await server.call_tool("research_fulltext", {"identifier": "https://openalex.org/W123"}))
        assert reply["error"] == "select_work_first"
        await server.call_tool("research_search", {"query": "optical film"})
        reply = decoded(await server.call_tool("research_fulltext", {"identifier": "https://openalex.org/W123"}))
        assert reply["error"] == "fulltext_requires_key"
        assert called == []
    asyncio.run(scenario())


def test_fulltext_character_pages_preserve_physical_references_and_cache():
    async def scenario():
        calls = []
        def fetch(record, **kwargs):
            calls.append((record, kwargs))
            return {"ok": True, "sha256": "a" * 64, "license": "cc-by", "parse_status": "parsed",
                    "segments": [{"page": 2, "section": "", "paragraph": 1, "text": "abcdef"},
                                 {"page": 3, "section": "", "paragraph": 2, "text": "ghijkl"}]}
        server = create_server("openalex", api_key="test-secret", source=Source(), fulltext_fetcher=fetch)
        await server.call_tool("research_search", {"query": "optical film"})
        first = decoded(await server.call_tool("research_fulltext", {
            "identifier": "https://openalex.org/W123", "max_chars": 4}))
        assert first["text"] == "abcd"
        assert first["segments"][0]["page"] == 2
        assert first["next_offset"] == 4
        second = decoded(await server.call_tool("research_fulltext", {
            "identifier": "https://openalex.org/W123", "offset": 4, "max_chars": 6}))
        assert second["text"] == "ef\nghi"
        assert [row["page"] for row in second["segments"]] == [2, 3]
        assert second["statement_kind"] == "source_statement"
        assert second["simulation_result"] is False
        assert second["untrusted_reference"] is True
        assert len(calls) == 1
        assert "test-secret" not in json.dumps(second)
    asyncio.run(scenario())


def test_absolute_script_stdio_directory_is_usable_outside_project_and_network_inert(tmp_path):
    async def scenario():
        entry = Path(__file__).resolve().parents[1] / "coat_sim/mcp/research_server.py"
        transport = StdioTransport(command=sys.executable, args=[str(entry), "--provider", "crossref"],
                                   cwd=str(tmp_path), env={}, keep_alive=False)
        async with Client(transport) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {"research_search", "research_lookup"}
    asyncio.run(scenario())


def test_failed_fulltext_reads_reuse_one_server_session_budget(monkeypatch):
    from coat_sim.agent import research_http
    constructors = []
    class BudgetClient:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
    monkeypatch.setattr(research_http, "SafeHttpClient", BudgetClient)
    clients = []
    def fail(record, *, client, **kwargs):
        clients.append(client)
        return {"ok": False, "error": "no_text"}
    async def scenario():
        server = create_server("openalex", api_key="test-secret", source=Source(), fulltext_fetcher=fail)
        await server.call_tool("research_search", {"query": "optical film"})
        for _ in range(4):
            reply = decoded(await server.call_tool("research_fulltext", {"identifier": "https://openalex.org/W123"}))
            assert reply["error"] == "no_text"
    asyncio.run(scenario())
    assert len(constructors) == 1
    assert constructors[0]["max_requests"] == 24
    assert len(clients) == 4 and all(client is clients[0] for client in clients)
