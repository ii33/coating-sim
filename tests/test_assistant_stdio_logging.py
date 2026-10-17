"""Real local MCP bootstrap under console-free production logging; no model I/O."""
import asyncio
import io
from pathlib import Path
import sys

import pytest

from coat_sim.project_logging import ProjectLog

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("parent_stderr", ["bounded", "absent"])
def test_builtin_mcp_handshake_does_not_require_parent_fileno(tmp_path, monkeypatch, parent_stderr):
    from coat_sim.agent import runtime

    async def scenario():
        await runtime.shutdown_runtime()
        try:
            client, _ = runtime._business_server()
            transport = runtime._BUSINESS_SERVER[2]
            assert isinstance(transport.log_file, Path)
            async with client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools}
                assert {"calculation_history", "build_film_stack"} <= names
            child_log = runtime.PROJECT_ROOT / "artifacts/logs/mcp-ui.log"
            assert "MCP stdio server starting" in child_log.read_text(encoding="utf-8")
        finally:
            await runtime.shutdown_runtime()

    stream = ProjectLog("assistant-worker", tmp_path) if parent_stderr == "bounded" else None
    if stream is not None:
        with pytest.raises(io.UnsupportedOperation):
            stream.fileno()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(sys, "__stderr__", stream)
    asyncio.run(asyncio.wait_for(scenario(), timeout=40))
