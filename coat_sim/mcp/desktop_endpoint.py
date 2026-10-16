"""Explicit local HTTP entry point; the ordinary server stays stdio by default."""
from __future__ import annotations

import argparse
import hmac


class BearerEndpoint:
    """Require the short-lived desktop credential before any MCP HTTP request."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.authorization = ("Bearer " + token).encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            supplied = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
            if len(supplied) != 1 or not hmac.compare_digest(supplied[0], self.authorization):
                body = b'{"error":"Bearer credential required"}'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode("ascii"))]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start deposition-sim MCP on local loopback only")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--credential-id", required=True, help="Temporary OS credential identifier")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    from coat_sim.mcp.server import mcp, _stop_jobs
    from coat_sim.ui.assistant_credentials import read_mcp_secret
    import uvicorn
    token = read_mcp_secret(args.credential_id)
    if not token:
        parser.error("temporary HTTP credential is missing")
    try:
        app = BearerEndpoint(mcp.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1"), token)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", access_log=False)
    finally:
        _stop_jobs()


if __name__ == "__main__":
    main()
