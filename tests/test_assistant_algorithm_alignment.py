"""Offline capability selection, deferred references and real tool assembly."""
from __future__ import annotations

import asyncio
from dataclasses import fields
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from coat_sim.agent.software_manual import capability_context, read_software_manual
from coat_sim.agent.runtime import READ_TOOLS, MAIN_TOOLS, EXECUTION_TOOLS
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_optimization_job import _SUPPORTED_SOLVERS
from tools.measure_assistant_context import capture_configuration, configuration_report


ROOT = Path(__file__).resolve().parents[1]
TOOLKIT_ENTRYPOINTS = frozenset({"toolkit_status", "toolkit_catalog", "toolkit_action"})
COMMON_TOP_LEVEL_TOOLS = TOOLKIT_ENTRYPOINTS | {
    "read_software_manual", "project_tools", "project_action", "read_archived_tool_result",
    "ask_user_question", "research_library_status",
}
MODE_TOP_LEVEL_TOOLS = {
    "chat": frozenset(),
    "operate": frozenset({"delegate_task"}),
    "research": frozenset({
        "delegate_task", "load_capability", "write_plan", "read_plan", "add_task",
        "update_task_status", "update_task_statuses", "remove_task", "add_subtask",
        "set_dependency", "get_available_tasks",
    }),
}


def test_algorithm_references_are_complete_bounded_and_only_loaded_on_demand():
    chapters = {row["key"] for row in read_software_manual()["chapters"]}
    wanted = {"algorithm_selection", "film_optimization", "film_optimization_parameters"}
    assert wanted <= chapters
    for name in wanted:
        chapter = read_software_manual(name)
        assert not chapter["truncated"]
        assert chapter["total_chars"] < 12000
    methods = read_software_manual("film_optimization")["content"]
    assert all(method in methods for method in _SUPPORTED_SOLVERS)
    parameter_text = read_software_manual("film_optimization_parameters")["content"]
    assert all(field.name in parameter_text for field in fields(FilmOptimizationRequest))
    resident = capability_context()
    assert "不计算镀后光学面型" in resident
    assert "平面膜堆干涉" in resident
    assert "不计算镀后光学面型、波前、光圈、折射率/光谱" not in resident
    assert "多起点 Powell" not in resident
    assert "candidate_budget" not in resident


def test_selection_reference_preserves_distinct_capabilities_and_permissions():
    selection = read_software_manual("algorithm_selection")["content"]
    methods = read_software_manual("film_optimization")["content"]
    assert all(key in selection for key in ("当前设计", "预算", "CPU", "reference/optimize",
                                            "4096", "10000", "needle_only", "growth_only", "combined"))
    assert "当前厚度 MCP estimate/submit 不接收结构请求" in selection
    assert "不保证全局最优" in methods
    assert "材料有效域" not in methods or "校验" in methods
    assert "不会开放新工具" in selection
    assert "候选不自动应用" in selection
    parameters = read_software_manual("film_optimization_parameters")["content"]
    assert "GPU 开关" in parameters
    assert "max_added_layers=1" in parameters and "growth_cycles=1" in parameters
    assert "不能把结构验证当预算外计算" in parameters
    assert "不能把保存版冒称当前草稿" in selection
    assert "project_editor_snapshot" in selection and "expected_snapshot_id" in selection
    assert "project_editor_snapshot" in READ_TOOLS & MAIN_TOOLS
    assert "project_editor_snapshot" in EXECUTION_TOOLS["design"]
    assert "project_editor_snapshot" in EXECUTION_TOOLS["simulation"]
    assert "project_editor_snapshot" in EXECUTION_TOOLS["windows"]


@pytest.mark.parametrize("mode", MODE_TOP_LEVEL_TOOLS)
def test_real_prepared_runtime_tool_surface_includes_capability_injection(mode):
    captured = asyncio.run(capture_configuration(mode))
    report = configuration_report(captured)
    names = set(report["tool_names"])
    # Compare names, not just counts: a privileged replacement is also a regression.
    assert names == COMMON_TOP_LEVEL_TOOLS | MODE_TOP_LEVEL_TOOLS[mode]
    assert report["tool_count"] == len(names), "Top-level tools must not be duplicated"
    if mode == "operate":
        assert "optimization.submission.request" in captured["instructions"]
        assert "不委派子代理重新设计" in captured["instructions"]
    assert "algorithm_selection" in captured["instructions"]
    assert "不能泛用此顺序" in captured["instructions"]
    assert "不使用草稿等内部编辑状态术语" in captured["instructions"]
    assert "不将未保存内容描述成已保存" in captured["instructions"]
    # No complete business catalog or long method reference is always injected.
    assert "candidate_budget" not in captured["instructions"]
    assert "多起点 Powell" not in captured["instructions"]
    ask = next(tool for tool in captured["parameters"].function_tools if tool.name == "ask_user_question")
    assert ask.parameters_json_schema["properties"]["options"]["items"]["properties"] == {}, "Production OpenAI schema customization must be included"
    schemas = {tool.name: tool.parameters_json_schema
               for tool in captured["parameters"].function_tools if tool.name in TOOLKIT_ENTRYPOINTS}
    assert set(schemas) == TOOLKIT_ENTRYPOINTS
    assert schemas["toolkit_status"]["properties"] == {}
    assert set(schemas["toolkit_catalog"]["properties"]) == {"capability", "tool_names"}
    assert schemas["toolkit_catalog"]["properties"]["capability"]["type"] == "string"
    assert schemas["toolkit_catalog"]["properties"]["tool_names"]["anyOf"] == [
        {"items": {"type": "string"}, "type": "array"}, {"type": "null"},
    ]
    action = schemas["toolkit_action"]
    # The compact entry point cannot accept host authority, roles or workspace overrides.
    assert set(action["properties"]) == set(action["required"]) == {"tool_name", "arguments"}
    assert action["properties"]["tool_name"]["type"] == "string"
    assert action["properties"]["arguments"]["type"] == "object"
    assert action["properties"]["arguments"]["additionalProperties"] is True
    assert all(schema["type"] == "object" and schema["additionalProperties"] is False
               for schema in schemas.values())


@pytest.mark.parametrize("mode", MODE_TOP_LEVEL_TOOLS)
@pytest.mark.parametrize(("external_enabled", "library_enabled"), [
    (True, False), (False, True), (True, True),
])
def test_enabled_optional_sources_change_actual_top_level_tools_only(mode, external_enabled, library_enabled):
    captured = asyncio.run(capture_configuration(
        mode, external_enabled=external_enabled, library_enabled=library_enabled))
    report = configuration_report(captured)
    expected = COMMON_TOP_LEVEL_TOOLS | MODE_TOP_LEVEL_TOOLS[mode]
    if external_enabled:
        expected |= {"external_tools"} if mode == "chat" else {"external_tools", "external_action"}
    if library_enabled:
        expected |= {"research_library_search", "research_library_read"}
    assert set(report["tool_names"]) == expected
    assert report["tool_count"] == len(expected)


def test_depth_two_allows_parent_delegation_but_hides_it_inside_child():
    observations = []
    def grandchild_model(messages, info):
        raise AssertionError("A child must not be allowed to delegate recursively")
    grandchild = Agent(FunctionModel(grandchild_model), name="grandchild")
    def child_model(messages, info):
        names = {tool.name for tool in info.function_tools}
        observations.append(("child", names))
        assert "delegate_task" not in names
        assert "parent_only_action" not in names
        assert "Available sub-agents" not in (info.instructions or "")
        return ModelResponse(parts=[TextPart("Read-only child analysis complete")])
    child = Agent(FunctionModel(child_model), name="worker", capabilities=[
        SubAgents(agents=[SubAgent(agent=grandchild, name="grandchild")], agent_folders=None,
                  inherit_tools=False, max_depth=2)])
    calls = 0
    def parent_model(messages, info):
        nonlocal calls
        calls += 1
        names = {tool.name for tool in info.function_tools}
        observations.append(("parent", names))
        assert "delegate_task" in names
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("delegate_task", {
                "agent_name": "worker", "task": "Read the synthetic fixture and summarize it."})])
        return ModelResponse(parts=[TextPart("Done")])
    def parent_only_action() -> str:
        """A synthetic parent-only operation that must never be inherited."""
        raise AssertionError("No mutation is authorized or needed")
    parent = Agent(FunctionModel(parent_model), name="parent", tools=[parent_only_action], capabilities=[
        SubAgents(agents=[SubAgent(agent=child, name="worker")], agent_folders=None,
                  inherit_tools=False, max_depth=2)])
    result = asyncio.run(parent.run("Analyze the synthetic fixture."))
    assert result.output == "Done"
    assert [label for label, _names in observations] == ["parent", "child", "parent"]
