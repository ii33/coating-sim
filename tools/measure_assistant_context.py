"""Measure assembled assistant inputs offline, without credentials or provider calls.

The production run_task builds the actual agents and capabilities. A local
FunctionModel captures the prepared request at the model boundary and raises
before producing a response. All state is isolated in a temporary directory.
The report contains sizes and names, never the instruction text or tool data.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel

# As in tools/app_version.py, file-path invocation needs the repository root
# bootstrapped before project imports.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coat_sim.agent import runtime  # noqa: E402
from coat_sim.agent.media import _text_tokens  # noqa: E402
from coat_sim.agent.research_library import TextFileResearchLibrary  # noqa: E402
from coat_sim.agent.software_manual import capability_context, read_software_manual  # noqa: E402
from coat_sim.agent.tool_dispatch import ProjectGateway  # noqa: E402


class _Captured(Exception):
    """Stop at the local prepared-model boundary, before any response/tool call."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def measure(value):
    text = value if isinstance(value, str) else _json(value)
    return {"characters": len(text), "utf8_bytes": len(text.encode("utf-8")),
            "project_rule_estimated_tokens": _text_tokens(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


async def capture_configuration(mode="chat", *, external_enabled=False, library_enabled=False,
                                development=False, source_texts=None):
    """Capture true instructions/schema, including capability and meter injection.

    Optional source_texts is only for an explicit offline baseline comparison;
    callers supply text already read from a known repository revision.
    """
    captured: dict[str, Any] = {}
    def capture(messages, info):
        captured.update(instructions=info.instructions or "", messages=messages,
                        parameters=info.model_request_parameters)
        raise _Captured()

    async def stream_capture(messages, info):
        capture(messages, info)
        yield "unreachable"

    external = MagicMock()
    external.sync = AsyncMock()
    external.configs = [{"id": "offline-fixture"}] if external_enabled else []
    writer = MagicMock()
    writer.redact.side_effect = lambda value: value
    writer.quota_config = {}
    writer.consume_decision_answers.return_value = []
    writer.pending_messages.return_value = []
    writer.consume_compaction.return_value = False
    mcp = MagicMock()
    mcp.__aenter__ = AsyncMock()
    mcp.__aexit__ = AsyncMock(return_value=False)
    config = {"assistant_enabled": True, "assistant_model": "offline-context-measurement",
              "assistant_api_url": "http://localhost:12345/v1", "task_mode": mode,
              "assistant_system_language": "zh-CN", "assistant_tone": "standard",
              "developer_enabled": development}
    production_model = runtime._chat_model(config["assistant_model"],
        runtime.reasoning_capabilities(config["assistant_api_url"], config["assistant_model"]), MagicMock())
    command = {"config": config, "session_id": "offline-session-" + mode,
               "request_id": "offline-request-" + mode, "api_key": "", "prompt": "你好"}
    original_read = Path.read_text
    def read_source(path, *args, **kwargs):
        if source_texts is not None and path in source_texts:
            return source_texts[path]
        return original_read(path, *args, **kwargs)
    with tempfile.TemporaryDirectory(prefix="assistant-context-") as directory, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"COAT_DEVELOPMENT": "1" if development else "0", "COAT_RELEASE_BUILD": "0"}))
        stack.enter_context(patch.object(runtime, "STATE_DIRECTORY", Path(directory)))
        stack.enter_context(patch.object(runtime, "HARNESS_DATABASE", Path(directory) / "harness.sqlite3"))
        stack.enter_context(patch.object(runtime, "_business_server", return_value=(MagicMock(), mcp)))
        stack.enter_context(patch.object(runtime, "external_pool", return_value=external))
        stack.enter_context(patch("coat_sim.core.backend_memory.successful_backend_memory", return_value={}))
        stack.enter_context(patch.object(runtime, "read_api_key", side_effect=AssertionError("Credential reads prohibited")))
        stack.enter_context(patch.object(runtime, "_chat_model", return_value=FunctionModel(capture, stream_function=stream_capture, profile=production_model.profile)))
        stack.enter_context(patch.object(runtime, "AsyncOpenAI"))
        stack.enter_context(patch.object(Path, "read_text", read_source))
        stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("Network prohibited")))
        try:
            await runtime.run_task(command, writer,
                research_library=TextFileResearchLibrary(documents={}) if library_enabled else None,
                allow_research_library_context=library_enabled)
        except _Captured:
            pass
    if not captured:
        raise AssertionError("The real runtime did not reach the offline model boundary")
    captured["production_model"] = production_model
    return captured


def configuration_report(captured):
    parameters = captured["parameters"]
    tools = [*parameters.function_tools, *parameters.output_tools]
    # Reuse the actual production model and profile that prepared this capture.
    # This schema-only mapping does not make any provider request.
    mapper: OpenAIChatModel = captured["production_model"]
    wire = [mapper._map_tool_definition(tool, {}) for tool in tools]
    return {"instructions": measure(captured["instructions"]), "tool_count": len(tools),
            "tool_names": [tool.name for tool in tools], "tools_openai_json": measure(wire),
            "instruction_parts": [measure(part.content) for part in parameters.instruction_parts or []]}


async def report(*, discussion_policy=True):
    configurations = {}
    for mode in ("chat", "operate", "research"):
        configurations[mode] = configuration_report(await capture_configuration(mode))
    for label, options in (("chat_with_external_mcp", {"external_enabled": True}),
                           ("chat_with_research_library", {"library_enabled": True}),
                           ("chat_with_development", {"development": True})):
        configurations[label] = configuration_report(await capture_configuration(**options))
    # Listing registered tools only; no handler, worker, calculation or provider.
    from coat_sim.mcp import server
    tools = await server.mcp.list_tools()
    class CatalogPeer:
        async def list_tools(self):
            return tools
    gateway = ProjectGateway(CatalogPeer(), set(runtime.MAIN_TOOLS), runtime.READ_TOOLS, lambda value: value)
    schemas = list((await gateway.schemas()).values())
    catalog = await gateway.catalog()
    discussion_catalog = None
    if discussion_policy:
        discussion_gateway = ProjectGateway(CatalogPeer(), set(runtime.DISCUSSION_TOOLS), runtime.READ_TOOLS,
                                            str, read_only_mode=True)
        discussion_schemas = list((await discussion_gateway.schemas()).values())
        discussion_catalog = {"tool_count": len(discussion_schemas),
                              "compact_catalog": measure(await discussion_gateway.catalog()),
                              "all_discussion_schemas_not_resident": measure(discussion_schemas)}
    components = {"agent_persona": measure((ROOT / "agent.md").read_text(encoding="utf-8")),
                  "machine_engineering": measure((ROOT / "coat_sim/agent/resources/machine_engineering.md").read_text(encoding="utf-8")),
                  "capability_context": measure(capability_context())}
    for key in ("algorithm_selection", "film_optimization", "film_optimization_parameters"):
        try:
            chapter = read_software_manual(key)
        except ValueError:
            continue  # A baseline can predate these reference chapters.
        components["on_demand_manual_" + key] = measure(chapter["content"])
    skills = {path.parent.name: measure(path.read_text(encoding="utf-8"))
              for path in sorted(runtime.SKILLS_DIRECTORY.glob("*/SKILL.md"))}
    return {"method": "Real run_task + local FunctionModel with production OpenAI profile/schema transformer at the prepared request boundary; fresh temporary state, zh-CN, standard tone, empty backend history; no credentials, provider requests or network",
            "token_basis": "Project estimate only: ceil(ASCII characters / 4) + non-ASCII characters. Not model tokenizer or billed usage. Tool JSON uses compact UTF-8 with actual pinned OpenAI profile customization and mapping. Provider framing, messages/history, outputs, cache behavior and dynamic library/MCP data are separate.",
            "dependency_versions": {name: version(name) for name in ("pydantic-ai-slim", "pydantic-ai-harness")},
            "source_sizes_and_hashes": {relative: measure((ROOT / relative).read_text(encoding="utf-8")) for relative in (
                "agent.md", "coat_sim/agent/runtime.py", "coat_sim/agent/software_manual.py",
                "coat_sim/agent/tool_dispatch.py", "coat_sim/agent/resources/machine_engineering.md",
                "coat_sim/agent/resources/software_manual.md", "coat_sim/mcp/server.py")},
            "configurations": configurations, "components": components,
            "on_demand": {"business_tool_count": len(schemas), "compact_catalog": measure(catalog),
                          "all_business_schemas_not_resident": measure(schemas), "skill_files_not_resident": skills,
                          "discussion_mode": discussion_catalog},
            "dynamic_exclusions": ["user messages and history/summary", "tool returns and archives", "backend success memory", "recovery ledger", "loaded skill bodies", "mutable planning reminder", "external MCP schemas/results", "research library excerpts", "media", "provider billing/framing"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-ref", help="Replay runtime, persona, manual and skill sources from a local Git commit; no checkout changes")
    options = parser.parse_args()
    if options.baseline_ref:
        revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", options.baseline_ref + "^{commit}"], cwd=ROOT, text=True).strip()
        paths = ["agent.md", "coat_sim/agent/resources/machine_engineering.md",
                 "coat_sim/agent/resources/software_manual.md",
                 *[str(path.relative_to(ROOT)) for path in sorted(runtime.SKILLS_DIRECTORY.glob("*/SKILL.md"))]]
        sources = {ROOT / path: subprocess.check_output(
            ["git", "show", revision + ":" + path], cwd=ROOT, text=True) for path in paths}
        source = subprocess.check_output(
            ["git", "show", revision + ":coat_sim/agent/runtime.py"], cwd=ROOT, text=True)
        exec(compile(source, runtime.__file__, "exec"), runtime.__dict__)
        original_read = Path.read_text
        def read_baseline(path, *args, **kwargs):
            return sources[path] if path in sources else original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", read_baseline):
            result = asyncio.run(report(discussion_policy="DISCUSSION_TOOLS =" in source))
        result["baseline_revision"] = revision
        result["baseline_scope"] = "Runtime, persona, manual and skill source replay with installed dependencies and current registered MCP declarations filtered by baseline runtime allowlists; not a full historical environment reconstruction"
    else:
        result = asyncio.run(report())
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if options.output:
        options.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
