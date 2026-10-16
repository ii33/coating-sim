"""Ephemeral offline MCP peer for the external-client contract tests only.

Logs test-generated observations to a caller-supplied temporary file. It never
reads project files, credentials, attachment data, or third-party services.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


LOG = Path(sys.argv[1])
NAMES = ("echo", "business_error", "slow", "disconnect")
INPUT = {"type": "object", "properties": {"value": {}},
         "required": ["value"], "additionalProperties": False}


def record(event: str, **values) -> None:
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **values}) + "\n")


async def list_tools(ctx, params):
    record("list_tools")
    return types.ListToolsResult(tools=[types.Tool(name=name,
        description="Local contract fixture; no external work.", inputSchema=INPUT)
        for name in NAMES])


async def call_tool(ctx, params):
    record("call", name=params.name, arguments=params.arguments)
    if params.name == "disconnect":
        os._exit(9)
    if params.name == "slow":
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            record("cancelled", name=params.name)
            raise
    roots_back_channel = True
    try:
        roots = [root.model_dump(mode="json") for root in (await ctx.session.list_roots()).roots]
    except Exception as exc:
        if "no back-channel" not in str(exc):
            raise
        # MCP's modern stdio era has no server-initiated roots back-channel.
        # Record that limit explicitly; the client callback is checked locally.
        roots_back_channel, roots = False, []
    value = {"arguments": params.arguments,
             "roots": roots, "roots_back_channel": roots_back_channel}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value))],
                                isError=params.name == "business_error")


async def main() -> None:
    record("startup", pid=os.getpid(), cwd=os.getcwd(),
           provider_environment_keys=[name for name in (
               "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COAT_SIM_ASSISTANT_API_KEY",
               "COAT_SIM_FIXTURE_HISTORY", "COAT_SIM_FIXTURE_ATTACHMENT") if name in os.environ])
    server = Server("external-mcp-offline-fixture", on_list_tools=list_tools,
                    on_call_tool=call_tool, get_tool_input_schema=lambda _name: INPUT)
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        record("closed")


if __name__ == "__main__":
    asyncio.run(main())
