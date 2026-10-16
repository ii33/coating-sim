"""Ephemeral loopback MCP HTTP/SSE fixture used only by contract tests.

The peer is a child process so a disconnect scenario can genuinely terminate
its listener after observing a tools/call request. It receives a deliberately
small environment, consumes only synthetic values, and never accesses a live
service, provider, credential store, or project data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
import uvicorn

NAMES = ("echo", "business_error", "slow", "disconnect")
INPUT = {"type": "object", "properties": {"value": {}},
         "required": ["value"], "additionalProperties": False}


def record(log_path: Path, event: str, **values: Any) -> None:
    # One short append per record; the parent test process polls this file.
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **values}, ensure_ascii=False) + "\n")
        stream.flush()


def _replace_readiness_file(source: Path, destination: Path) -> None:
    """Atomically expose a fully written readiness document."""
    os.replace(source, destination)


def publish_readiness(ready_path: Path, payload: dict[str, Any]) -> None:
    """Publish JSON without ever exposing a partially written final file."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=ready_path.parent,
                prefix=f".{ready_path.name}.", suffix=".tmp", delete=False) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
        # The temporary is in the same directory, and it is closed before
        # replace so readers see either the prior complete file or this one.
        _replace_readiness_file(temporary_path, ready_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_app(transport: str, log_path: Path) -> Any:
    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        record(log_path, "list_tools")
        return types.ListToolsResult(tools=[types.Tool(
            name=name, description="Synthetic loopback contract fixture.",
            inputSchema=INPUT) for name in NAMES])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        record(log_path, "call", name=params.name, arguments=params.arguments)
        if params.name == "disconnect":
            # A real process exit after the peer has read tools/call. The client
            # will see a socket loss rather than an in-memory fake exception.
            record(log_path, "disconnect")
            os._exit(23)
        if params.name == "slow":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                record(log_path, "cancelled", name=params.name)
                raise

        roots_back_channel = True
        try:
            roots = [root.model_dump(mode="json")
                     for root in (await ctx.session.list_roots()).roots]
        except Exception as exc:
            if "no back-channel" not in str(exc):
                raise
            roots_back_channel, roots = False, []
        payload = {"arguments": params.arguments, "roots": roots,
                   "roots_back_channel": roots_back_channel}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            isError=params.name == "business_error")

    server = Server("external-mcp-loopback-contract", on_list_tools=list_tools,
                    on_call_tool=call_tool, get_tool_input_schema=lambda _name: INPUT)
    initialization_options = server.create_initialization_options()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        if transport == "http":
            async with session_manager.run():
                yield
        else:
            yield

    if transport == "http":
        session_manager = StreamableHTTPSessionManager(server)
        routes = [Mount("/mcp", app=session_manager.handle_request)]
    elif transport == "sse":
        sse = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> Response:
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await server.run(streams[0], streams[1], initialization_options)
            return Response()

        routes = [Route("/sse", endpoint=handle_sse, methods=["GET"]),
                  Mount("/messages/", app=sse.handle_post_message)]
    else:
        raise ValueError(f"unsupported fixture transport: {transport}")

    starlette = Starlette(routes=routes, lifespan=lifespan)

    class RequestRecorder:
        def __init__(self, app: Any):
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") == "http":
                # Record only whether the request carried one of these
                # explicit synthetic test credentials, never header contents.
                headers = dict(scope.get("headers", ()))
                record(log_path, "request", method=scope.get("method", ""),
                       path=scope.get("path", ""),
                       x_fixture_credential_v1=(headers.get(b"x-fixture-credential")
                                                 == b"fixture-secret-v1"),
                       x_fixture_credential_v3=(headers.get(b"x-fixture-credential")
                                                 == b"fixture-secret-v3"),
                       authorization_v2=(headers.get(b"authorization")
                                         == b"Bearer fixture-secret-v2"))
            await self.app(scope, receive, send)

    return RequestRecorder(starlette)


async def serve(args: argparse.Namespace) -> None:
    log_path, ready_path = Path(args.log), Path(args.ready)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    try:
        publish_readiness(ready_path, {"host": "127.0.0.1", "port": port})
        record(log_path, "startup", pid=os.getpid(), transport=args.transport,
               provider_environment_keys=[name for name in (
                   "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COAT_SIM_ASSISTANT_API_KEY",
                   "COAT_SIM_FIXTURE_HISTORY", "COAT_SIM_FIXTURE_ATTACHMENT") if name in os.environ])
        config = uvicorn.Config(build_app(args.transport, log_path), host="127.0.0.1", port=port,
                                log_level="critical", access_log=False,
                                timeout_graceful_shutdown=1, lifespan="on")
        await uvicorn.Server(config).serve(sockets=[listener])
    finally:
        listener.close()
        record(log_path, "closed", pid=os.getpid())


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("http", "sse"), required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", required=True)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args))


if __name__ == "__main__":
    _main()
