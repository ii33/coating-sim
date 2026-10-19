"""Offline task ceilings exercise the real request preflight and accounting."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coat_sim.agent import runtime
from coat_sim.agent.storage import AssistantStore
from coat_sim.agent.tool_results import TaskLimitReached, task_limits
from coat_sim.ui import settings


@dataclass
class Parameters:
    function_tools: list[Any] = field(default_factory=list)
    output_tools: list[Any] = field(default_factory=list)
    instruction_parts: list[Any] = field(default_factory=list)


@dataclass
class RequestContext:
    messages: list[Any] = field(default_factory=list)
    model_request_parameters: Any = field(default_factory=Parameters)
    model_settings: dict[str, Any] = field(default_factory=dict)
    streaming: bool = False


class Writer:
    quota_config: dict[str, Any] = {}

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, _request: str, **values: Any) -> None:
        self.events.append((event, values))

    def redact(self, value: str) -> str:
        return value


class Store:
    def __init__(self) -> None:
        self.reservations = 0

    def reserve_request(self, _config: Any, _input: int, desired: int, **_kwargs: Any) -> tuple[str, int]:
        self.reservations += 1
        return str(self.reservations), desired

    def settle_request(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def usage_summary(self, _config: Any) -> dict[str, Any]:
        return {}


def meter(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any], *, mode: str = "chat") -> tuple[runtime.BudgetAndTools, Store, Writer]:
    store, writer = Store(), Writer()
    result = object.__new__(runtime.BudgetAndTools)
    result.store, result.writer = cast(AssistantStore, store), writer
    result.config = {"assistant_api_url": "http://localhost:12345/v1", **config}
    result.session, result.request, result.model = "session", "request", "local-model"
    result.role, result.mode, result.strength = "main", mode, "auto"
    result.capabilities = {"output_parameter": "max_tokens"}
    result.shared = {"requests": 0, "used_tokens": 0, "used_input_tokens": 0,
                     "streams": {}, "last_steps": {}}
    result.limits = task_limits(config, mode)
    result.archive = SimpleNamespace(project_history=lambda messages: messages,
                                     project=lambda value, **_kwargs: value)
    monkeypatch.setattr(result, "_current_mode_instruction", lambda: "")
    monkeypatch.setattr(result, "progress", lambda *_args: None)
    async def immediate_response(awaitable: Any, _reservation: str) -> Any:
        return await awaitable
    monkeypatch.setattr(result, "_await_model_response", immediate_response)
    monkeypatch.setattr(runtime, "request_context_estimate", lambda *_args: {
        "input_tokens": 20_000, "estimated_input_tokens": 20_000, "basis": "mock"})
    monkeypatch.setattr(runtime, "model_token_limits", lambda *_args: {})
    monkeypatch.setattr(runtime, "context_budget_fields", lambda *_args: {"effective_input_known": False})
    monkeypatch.setattr(runtime, "request_policy", lambda *_args, **_kwargs: {
        "desired_output": 2_000, "minimum_output": 1_000,
        "maximum_input_tokens": 0, "context_tokens": 0,
        "effective_reasoning": "none", "model_settings": {}})
    monkeypatch.setattr(runtime, "language_instructions", lambda _config: "")
    monkeypatch.setattr(runtime, "development_enabled", lambda _config: False)
    monkeypatch.setattr(runtime, "sanitize_chat_settings", lambda **kwargs: kwargs["model_settings"])
    monkeypatch.setattr(runtime, "observe_success", lambda **_kwargs: None)
    return result, store, writer


async def run_steps(result: runtime.BudgetAndTools, count: int, *, with_tools: bool = False) -> tuple[int, int]:
    calls = tool_calls = 0

    async def handler(_context: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=20_000, output_tokens=1_000),
                               provider_details={}, parts=[], finish_reason="stop")

    for _ in range(count):
        await result.wrap_model_request(SimpleNamespace(run_id="mock"),
                                        request_context=RequestContext(), handler=handler)
        if with_tools:
            async def tool_handler(_args: Any) -> Any:
                nonlocal tool_calls
                tool_calls += 1
                return {"step": tool_calls}
            await result.wrap_tool_execute(SimpleNamespace(),
                call=SimpleNamespace(tool_name="toolkit_status"), tool_def=None,
                args={"step": tool_calls + 1}, handler=tool_handler)
    return calls, tool_calls


async def run_delegations(result: runtime.BudgetAndTools, count: int) -> int:
    calls = 0

    async def handler(_args: Any) -> Any:
        nonlocal calls
        calls += 1
        return {"ok": True, "delegation": calls}

    for step in range(count):
        await result.wrap_tool_execute(SimpleNamespace(),
            call=SimpleNamespace(tool_name="delegate_task"), tool_def=None,
            args={"task": f"distinct-{step}"}, handler=handler)
    return calls


def complete(coroutine: Any) -> Any:
    """Drive the immediate local fake without a Windows network event loop."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    finally:
        coroutine.close()
    raise AssertionError("local fake unexpectedly suspended")


def test_default_chat_model_and_tool_steps_continue_without_task_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    result, store, writer = meter(monkeypatch, {})
    assert task_limits({}, "chat") == {"requests": None, "input_tokens": None,
                                      "total_tokens": None, "tool_calls": None,
                                      "delegations": None}
    assert complete(run_steps(result, 70, with_tools=True)) == (70, 70)
    assert store.reservations == 70
    assert result.shared["used_input_tokens"] == 1_400_000
    assert result.shared["used_tokens"] == 1_470_000
    assert result.shared["tool_calls"] == 70
    assert not [data for event, data in writer.events if event == "diagnostic" and data.get("phase") == "task_limit"]


def test_explicit_task_ceiling_stops_before_next_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    result, store, writer = meter(monkeypatch, {"assistant_task_max_requests": 2})
    assert complete(run_steps(result, 2)) == (2, 0)
    with pytest.raises(TaskLimitReached, match="2"):
        complete(run_steps(result, 1))
    assert store.reservations == 2
    limit = [data for event, data in writer.events if event == "diagnostic" and data.get("phase") == "task_limit"]
    assert len(limit) == 1
    assert limit[0]["effective_limits"]["requests"] == 2
    assert limit[0]["requests"] == 2


def test_explicit_token_ceiling_stops_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    result, store, writer = meter(monkeypatch, {"assistant_task_max_input_tokens": 39_999})
    assert complete(run_steps(result, 1)) == (1, 0)
    with pytest.raises(TaskLimitReached, match="累计输入预算为 40 K"):
        complete(run_steps(result, 1))
    assert store.reservations == 1
    assert any(data.get("phase") == "task_limit" and data.get("input_tokens") == 20_000
               for event, data in writer.events if event == "diagnostic")


def test_explicit_total_ceiling_reports_actual_and_next_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    result, store, writer = meter(monkeypatch, {"assistant_task_max_total_tokens": 41_000})
    assert complete(run_steps(result, 1)) == (1, 0)
    with pytest.raises(TaskLimitReached, match="上限为 41000 Token；已用 21000，下一次请求预估输入 20000"):
        complete(run_steps(result, 1))
    assert store.reservations == 1
    assert any(data.get("phase") == "task_limit" and data["effective_limits"]["total_tokens"] == 41_000
               for event, data in writer.events if event == "diagnostic")


def test_explicit_tool_call_limit_stops_before_tool_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _store, writer = meter(monkeypatch, {"assistant_task_max_tool_calls": 2})
    assert complete(run_steps(result, 2, with_tools=True)) == (2, 2)
    with pytest.raises(TaskLimitReached, match="2 次工具调用上限"):
        complete(run_steps(result, 1, with_tools=True))
    assert result.shared["tool_calls"] == 2
    assert any(data.get("phase") == "task_limit" and data["effective_limits"]["tool_calls"] == 2
               for event, data in writer.events if event == "diagnostic")


def test_repeated_observation_guard_remains_active_without_task_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _store, writer = meter(monkeypatch, {})
    for _ in range(4):
        result._bounded_result("toolkit_status", {}, {"status": "unchanged"})
    with pytest.raises(TaskLimitReached, match="重复结果"):
        result._bounded_result("toolkit_status", {}, {"status": "unchanged"})
    assert any(data.get("phase") == "task_limit" for event, data in writer.events if event == "diagnostic")


def test_parent_and_child_can_delegate_beyond_eight_without_default_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "dispatch_authority", lambda *_args, **_kwargs: {"allowed": True})
    parent, _store, writer = meter(monkeypatch, {}, mode="operate")
    child, _child_store, _child_writer = meter(monkeypatch, {}, mode="operate")
    child.role = "execute_design"
    child.shared = parent.shared
    assert complete(run_delegations(parent, 6)) == 6
    assert complete(run_delegations(child, 6)) == 6
    assert parent.shared["delegations"] == 12
    assert parent.shared["tool_calls"] == 12
    assert not [data for event, data in writer.events if event == "diagnostic" and data.get("phase") == "task_limit"]


def test_explicit_delegation_cap_is_shared_and_prevents_next_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "dispatch_authority", lambda *_args, **_kwargs: {"allowed": True})
    parent, _store, writer = meter(monkeypatch, {"assistant_task_max_delegations": 2}, mode="operate")
    child, _child_store, _child_writer = meter(monkeypatch, {"assistant_task_max_delegations": 2}, mode="operate")
    child.role = "execute_design"
    child.shared = parent.shared
    assert complete(run_delegations(parent, 1)) == 1
    assert complete(run_delegations(child, 1)) == 1
    with pytest.raises(TaskLimitReached, match="显式设置的 2 次上限"):
        complete(run_delegations(parent, 1))
    assert parent.shared["delegations"] == 2
    assert any(data.get("phase") == "task_limit" and data["effective_limits"]["delegations"] == 2
               and data["delegations"] == 2
               for event, data in writer.events if event == "diagnostic")


def test_optional_task_ceilings_survive_settings_serialization(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    saved = settings.save_ui_settings({"assistant_task_max_requests": None,
        "assistant_task_max_input_tokens": 40_000, "assistant_task_max_total_tokens": 0,
        "assistant_task_max_tool_calls": 3, "assistant_task_max_delegations": 4})
    assert saved["assistant_task_max_requests"] is None
    assert saved["assistant_task_max_total_tokens"] is None
    assert task_limits(saved, "operate") == {"requests": None, "input_tokens": 40_000,
                                           "total_tokens": None, "tool_calls": 3, "delegations": 4}
    assert settings.load_ui_settings()["assistant_task_max_input_tokens"] == 40_000
