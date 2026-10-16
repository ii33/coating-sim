"""AI-02 runtime cancellation and budget regressions using only local fakes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    PartStartEvent,
    TextPart,
    UserPromptPart,
)

from coat_sim.agent import media, runtime
from coat_sim.agent.modelclient import reasoning_capabilities
from coat_sim.agent.tool_dispatch import ProjectGateway

pytestmark = pytest.mark.contract


class _Writer:
    quota_config: dict[str, Any] = {}
    event_store = None

    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.pending = list(pending or [])
        self.applied: list[str] = []
        self.compaction_checks = 0
        self.consume_compaction_value = False

    def emit(self, event: str, _request: str, **values: Any) -> None:
        self.events.append((event, values))

    def redact(self, value: str) -> str:
        return value

    def consume_decision_answers(self, _request: str) -> list[dict[str, Any]]:
        return []

    def pending_messages(self, _request: str) -> list[dict[str, Any]]:
        return list(self.pending)

    def apply_steering(self, _request: str, identity: str) -> None:
        self.pending = [item for item in self.pending if item["message_id"] != identity]
        self.applied.append(identity)

    def reject_steering(self, _request: str, identity: str, _text: str) -> None:
        self.pending = [item for item in self.pending if item["message_id"] != identity]

    def consume_compaction(self, _request: str) -> bool:
        self.compaction_checks += 1
        return self.consume_compaction_value


@dataclass
class _Parameters:
    function_tools: list[Any] = field(default_factory=list)
    output_tools: list[Any] = field(default_factory=list)
    native_tools: list[Any] = field(default_factory=list)
    instruction_parts: list[Any] = field(default_factory=list)


@dataclass
class _RequestContext:
    messages: list[Any]
    model_request_parameters: Any = field(default_factory=_Parameters)
    model_settings: dict[str, Any] = field(default_factory=dict)
    streaming: bool = False
    model_id: str | None = None


def _budget_meter(monkeypatch: pytest.MonkeyPatch, writer: _Writer) -> tuple[Any, Any]:
    class Store:
        def __init__(self) -> None:
            self.reservations: list[Any] = []

        def reserve_request(self, *args: Any, **kwargs: Any) -> tuple[str, int]:
            self.reservations.append((args, kwargs))
            return "unexpected-reservation", 1

    store = Store()
    meter = object.__new__(runtime.BudgetAndTools)
    meter.store = store
    meter.config = {"assistant_api_url": "http://localhost:12345/v1"}
    meter.session = "offline-session"
    meter.request = "offline-budget"
    meter.model = "offline-model"
    meter.writer = writer
    meter.ledger = None
    meter.client = None
    meter.role = "main"
    meter.mode = "chat"
    meter.strength = "auto"
    meter.capabilities = reasoning_capabilities("http://localhost:12345/v1", "offline-model")
    meter.shared = {"requests": 0, "used_tokens": 0, "used_input_tokens": 0,
                    "streams": {}, "last_steps": {}}
    meter.limits = {"requests": 20, "input_tokens": 200000, "total_tokens": 200000}
    meter.archive = SimpleNamespace(project_history=lambda messages: messages)
    monkeypatch.setattr(runtime, "request_context_estimate", lambda *_args, **_kwargs: {
        "input_tokens": 200,
        "estimated_input_tokens": 200,
        "basis": "offline regression",
    })
    monkeypatch.setattr(runtime, "context_budget_fields", lambda *_args, **_kwargs: {
        "effective_input_known": True,
        "input_budget": 100,
    })
    monkeypatch.setattr(runtime, "model_token_limits", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime, "request_policy", lambda *_args, **_kwargs: {
        "desired_output": 64,
        "minimum_output": 1,
        "maximum_input_tokens": 0,
        "context_tokens": 0,
        "effective_reasoning": "none",
        "model_settings": {},
    })
    monkeypatch.setattr(runtime, "language_instructions", lambda _config: "")
    return meter, store


def test_budget_rejection_keeps_pending_steering_text_and_binary_attachment_in_closed_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "evidence.pdf"
    attachment.write_bytes(b"%PDF-local-fixture")
    item = {"message_id": "steer-1", "prompt": "Keep the attached evidence in view.",
            "attachments": [{"path": "synthetic:evidence.pdf"}]}
    writer = _Writer([item])
    monkeypatch.setattr(media, "data_path", lambda value, **_kwargs: attachment
                        if value == "synthetic:evidence.pdf" else pytest.fail("unexpected path"))
    monkeypatch.setattr(media, "file_info", lambda _path: {
        "mime": "application/pdf", "reference": "synthetic:evidence.pdf",
    })

    # ConversationControl and the budget meter share the same message list, as
    # the actual Agent request boundary does. No provider handler is reachable.
    context = _RequestContext(messages=[ModelRequest(parts=[UserPromptPart("original task")])])
    control = runtime.ConversationControl(writer, "offline-budget")
    asyncio.run(control.before_model_request(SimpleNamespace(), context))
    meter, store = _budget_meter(monkeypatch, writer)
    handler_calls = 0

    async def fake_handler(_context: Any) -> Any:
        nonlocal handler_calls
        handler_calls += 1

    with pytest.raises(RuntimeError, match="超过扣除输出和压缩预留"):
        asyncio.run(meter.wrap_model_request(
            SimpleNamespace(run_id="offline-budget"), request_context=context, handler=fake_handler,
        ))

    assert writer.applied == ["steer-1"]
    assert writer.pending == []
    assert store.reservations == []
    assert handler_calls == 0

    closed = runtime._closed_history(context.messages, "limited", "offline-budget", "offline-session")
    serialized = ModelMessagesTypeAdapter.dump_json(closed)
    restored = ModelMessagesTypeAdapter.validate_json(serialized)
    parts = [part for message in restored for part in message.parts]
    user_content = [part.content for part in parts if isinstance(part, UserPromptPart)]
    prompt_text = "\n".join(value for content in user_content
                             for value in (content if isinstance(content, list) else [content])
                             if isinstance(value, str))
    binaries = [value for content in user_content for value in (content if isinstance(content, list) else [])
                if isinstance(value, BinaryContent)]
    assert "original task" in prompt_text
    assert "Keep the attached evidence in view." in prompt_text
    assert len(binaries) == 1
    assert binaries[0].data == b"%PDF-local-fixture"
    assert any((getattr(message, "metadata", None) or {}).get("coat_turn_closed") == "limited"
               for message in restored)


class _FakeStore:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.saves: list[dict[str, Any]] = []

    def history_state(self, _session: str) -> dict[str, Any]:
        return {}

    def load_history(self, _session: str) -> str | None:
        return None

    def save_history(self, _session: str, history: str, *, request_id: str, state: str) -> None:
        self.saves.append({"history": history, "request_id": request_id, "state": state})


class _FakeSteps:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def list_runs(self, **_kwargs: Any) -> list[Any]:
        return []

    async def latest_snapshot(self, **_kwargs: Any) -> Any:
        return None


class _FakeLedger:
    def __init__(self) -> None:
        pass

    def unsettled(self, _session: str) -> list[Any]:
        return []


class _FakeExternal:
    configs: list[Any] = []

    async def sync(self, _configs: Any) -> None:
        return None

    def begin_turn(self) -> None:
        return None


def test_run_task_cancellation_persists_streamed_text_and_skips_post_terminal_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAgent:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, prompt: str, **kwargs: Any) -> Any:
            assert prompt == "cancel after this response starts"

            async def events():
                yield PartStartEvent(index=0, part=TextPart("partial text received before stop"))

            await kwargs["event_stream_handler"](SimpleNamespace(run_id="cancel-request"), events())
            raise asyncio.CancelledError

    writer = _Writer()
    writer.consume_compaction_value = True
    store = _FakeStore()
    monkeypatch.setattr(runtime, "STATE_DIRECTORY", tmp_path / "assistant")
    monkeypatch.setattr(runtime, "HARNESS_DATABASE", tmp_path / "harness.sqlite3")
    monkeypatch.setattr(runtime, "AssistantStore", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(runtime, "SqliteStepStore", _FakeSteps)
    monkeypatch.setattr(runtime, "JobLedger", _FakeLedger)
    monkeypatch.setattr(runtime, "_business_server", lambda: (None, None))
    monkeypatch.setattr(runtime, "external_pool", _FakeExternal)
    monkeypatch.setattr(runtime, "_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "AsyncOpenAI", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "Agent", FakeAgent)
    monkeypatch.setattr(runtime, "request_policy", lambda *_args, **_kwargs: {
        "desired_output": 64,
        "minimum_output": 1,
        "maximum_input_tokens": 0,
        "context_tokens": 0,
        "effective_reasoning": "none",
        "model_settings": {},
    })
    monkeypatch.setattr(runtime, "model_token_limits", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime, "context_compaction_limits", lambda *_args, **_kwargs: {
        "trigger": 500, "keep_tokens": 100, "target": 250, "retry_growth": 64,
        "summary_output_tokens": 128,
    })
    monkeypatch.setattr(runtime, "_summary_strategy", lambda *_args, **_kwargs: object())
    compact_calls: list[Any] = []
    monkeypatch.setattr(runtime, "compact_now", lambda *args, **kwargs: compact_calls.append((args, kwargs)))
    monkeypatch.setattr("coat_sim.core.backend_memory.successful_backend_memory", lambda: {})
    command = {
        "config": {
            "assistant_enabled": True,
            "assistant_api_url": "http://localhost:12345/v1",
            "assistant_model": "offline-model",
            "task_mode": "chat",
        },
        "session_id": "offline-session",
        "request_id": "cancel-request",
        "api_key": "",
        "prompt": "cancel after this response starts",
    }

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.run_task(command, writer))

    final = store.saves[-1]
    assert final["state"] == "cancelled"
    assert final["request_id"] == "cancel-request"
    history = ModelMessagesTypeAdapter.validate_json(final["history"])
    all_parts = [part for message in history for part in message.parts]
    text = "\n".join(part.content for part in all_parts if isinstance(part, TextPart))
    prompts = "\n".join(part.content for part in all_parts if isinstance(part, UserPromptPart))
    assert "cancel after this response starts" in prompts
    assert "partial text received before stop" in text
    assert any((getattr(message, "metadata", None) or {}).get("coat_turn_closed") == "cancelled"
               for message in history)
    assert writer.compaction_checks == 0
    assert compact_calls == []


def test_project_action_unknown_name_is_rejected_before_tool_dispatch(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def list_tools(self) -> list[Any]:
            return [SimpleNamespace(name="read_material", description="read", input_schema={
                "type": "object", "properties": {}, "required": [], "additionalProperties": False,
            })]

        async def call_tool_mcp(self, name: str, arguments: Any) -> Any:
            self.calls.append((name, arguments))
            return {"unexpected": True}

    client = Client()
    gateway = ProjectGateway(client, {"read_material"}, set(), lambda value: value)
    writer = _Writer()
    meter = object.__new__(runtime.BudgetAndTools)
    meter.shared = {"tool_calls": 0, "gateway_guard": {}}
    meter.limits = {"tool_calls": 10}
    meter.config = {"assistant_api_url": "http://localhost:12345/v1", "task_mode": "chat"}
    meter.mode = "chat"
    meter.archive = SimpleNamespace(project=lambda value, **_kwargs: value)

    async def local_project_action(arguments: dict[str, Any]) -> Any:
        return await gateway.execute(arguments.get("tool_name", ""), arguments.get("arguments", {}))

    result = asyncio.run(meter.wrap_tool_execute(
        SimpleNamespace(),
        call=SimpleNamespace(tool_name="project_action", tool_call_id="unknown-tool"),
        tool_def=SimpleNamespace(name="project_action"),
        args={"tool_name": "unregistered_mutation", "arguments": {"path": "synthetic"}},
        handler=local_project_action,
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "discussion_read_only"
    assert client.calls == []

