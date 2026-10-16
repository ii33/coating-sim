"""AI provider protocol gates and Chat payloads, verified without live requests."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from coat_sim.agent import runtime
from coat_sim.agent import media
from coat_sim.core.data_files import MAX_FILE_BYTES
from coat_sim.agent.modelclient import official_openai_model_profile, reasoning_capabilities, request_policy
from coat_sim.agent.provider_protocol import (
    ProviderProtocolError,
    sanitize_chat_settings,
    validate_request,
)


OPENAI_URL = "https://api.openai.com/v1"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
LUNA = "gpt-6-luna"
SOL = "gpt-6.1-sol"


def test_model_protocol_facts_are_exact_and_cached_probes_cannot_override_them() -> None:
    assert official_openai_model_profile(OPENAI_URL, LUNA)["chat_function_tools"] == "none_only"
    assert official_openai_model_profile(OPENAI_URL, SOL)["chat_function_tools"] == "responses_only"
    assert official_openai_model_profile("https://gateway.example/v1", LUNA) is None
    assert official_openai_model_profile(OPENAI_CHAT_URL, LUNA)["chat_function_tools"] == "none_only"
    assert official_openai_model_profile(OPENAI_CHAT_URL + "////", LUNA) is not None
    assert official_openai_model_profile("https://api.openai.com/v1/responses", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com/v2/chat/completions", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com/custom/chat/completions", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com/v1/unknown", LUNA) is None
    assert official_openai_model_profile("http://api.openai.com/v1", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com:8443/v1", LUNA) is None
    assert official_openai_model_profile("https://user@api.openai.com/v1", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com/v1?key=value", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com/v1#fragment", LUNA) is None
    assert official_openai_model_profile("https://api.openai.com/v1/", LUNA) is not None
    assert official_openai_model_profile("https://api.openai.com////", LUNA) is not None
    assert official_openai_model_profile("https://api.openai.com/chat/completions///", LUNA) is not None
    assert official_openai_model_profile(OPENAI_URL, "Gpt-6-luna") is None
    assert official_openai_model_profile(OPENAI_URL, " gpt-6-luna") is None

    contradictory_probe = {
        "api_url": OPENAI_URL,
        "model": LUNA,
        "verified": True,
        "evidence": "invalid_control+validated_requests",
        "strength_map": {
            "low": {"actual_effort": "low", "request_fields": {"reasoning_effort": "low"}},
            "none": {"actual_effort": "none", "request_fields": {"reasoning_effort": "none"}},
        },
        "output_parameter": "max_completion_tokens",
    }
    capabilities = reasoning_capabilities(OPENAI_URL, LUNA, contradictory_probe)
    assert capabilities["supported_strengths"] == ["auto", "none", "low", "medium", "high", "xhigh", "max"]
    assert capabilities["official_model_profile"]["chat_function_tools"] == "none_only"

    sol_capabilities = reasoning_capabilities(OPENAI_URL, SOL, {**contradictory_probe, "model": SOL})
    with pytest.raises(ValueError):
        request_policy(sol_capabilities, "chat", "none", "main")

    chat_url_capabilities = reasoning_capabilities(OPENAI_CHAT_URL, SOL)
    assert chat_url_capabilities["provider"] == "openai"
    assert chat_url_capabilities["official_model_profile"]["chat_function_tools"] == "responses_only"
    assert "low" in chat_url_capabilities["supported_strengths"]


def test_chat_tool_gate_checks_new_tools_and_historical_tool_parts() -> None:
    with pytest.raises(ProviderProtocolError) as caught:
        validate_request(
            api_url=OPENAI_URL,
            model=SOL,
            effective_reasoning="low",
            model_request_parameters=ModelRequestParameters(function_tools=[object()]),
            messages=[],
            model_settings={"openai_reasoning_effort": "low"},
        )
    assert caught.value.code == "responses_required"
    assert caught.value.required_protocol == "responses"

    history = [ModelRequest(parts=[ToolCallPart("lookup", {"q": "x"}, tool_call_id="call-1")])]
    with pytest.raises(ProviderProtocolError, match="Responses API"):
        validate_request(
            api_url=OPENAI_URL,
            model=SOL,
            effective_reasoning="low",
            model_request_parameters=ModelRequestParameters(),
            messages=history,
            model_settings={"openai_reasoning_effort": "low"},
        )

    returned_history = [ModelRequest(parts=[ToolReturnPart("lookup", "done", tool_call_id="call-1")])]
    with pytest.raises(ProviderProtocolError):
        validate_request(
            api_url=OPENAI_URL,
            model=SOL,
            effective_reasoning="low",
            model_request_parameters=ModelRequestParameters(),
            messages=returned_history,
            model_settings={"openai_reasoning_effort": "low"},
        )

    retry_history = [ModelRequest(parts=[RetryPromptPart(
        content="invalid tool arguments", tool_name="lookup", tool_call_id="call-1",
    )])]
    with pytest.raises(ProviderProtocolError):
        validate_request(
            api_url=OPENAI_URL,
            model=SOL,
            effective_reasoning="low",
            model_request_parameters=ModelRequestParameters(),
            messages=retry_history,
            model_settings={"openai_reasoning_effort": "low"},
        )

    with pytest.raises(ProviderProtocolError) as native:
        validate_request(
            api_url=OPENAI_URL,
            model=LUNA,
            effective_reasoning="none",
            model_request_parameters=ModelRequestParameters(native_tools=[object()]),
            messages=[],
            model_settings={"openai_reasoning_effort": "none"},
        )
    assert native.value.required_protocol == "responses"


def test_luna_chat_tools_need_final_none_reasoning_and_conflicts_fail_closed() -> None:
    parameters = ModelRequestParameters(function_tools=[object()])
    with pytest.raises(ProviderProtocolError) as caught:
        validate_request(
            api_url=OPENAI_URL,
            model=LUNA,
            effective_reasoning="low",
            model_request_parameters=parameters,
            messages=[],
            model_settings={"openai_reasoning_effort": "low"},
        )
    assert caught.value.code == "responses_required"

    validate_request(
        api_url=OPENAI_URL,
        model=LUNA,
        effective_reasoning="none",
        model_request_parameters=parameters,
        messages=[],
        model_settings={"openai_reasoning_effort": "none"},
    )

    with pytest.raises(ProviderProtocolError) as conflict:
        validate_request(
            api_url=OPENAI_URL,
            model=LUNA,
            effective_reasoning="none",
            model_request_parameters=ModelRequestParameters(),
            messages=[],
            model_settings={"openai_reasoning_effort": "none", "extra_body": {"reasoning_effort": "high"}},
        )
    assert conflict.value.code == "request_settings_conflict"


@pytest.mark.parametrize("override", [
    {"tools": []},
    {"tools": None},
    {"tool_choice": None},
    {"model": LUNA},
    {"messages": []},
    {"stream": False},
])
def test_raw_chat_envelope_overrides_fail_even_when_empty_or_null(override: dict[str, Any]) -> None:
    with pytest.raises(ProviderProtocolError) as caught:
        validate_request(
            api_url=OPENAI_URL,
            model=LUNA,
            effective_reasoning="none",
            model_request_parameters=ModelRequestParameters(),
            messages=[],
            model_settings={"openai_reasoning_effort": "none", "extra_body": override},
        )
    assert caught.value.code == "request_settings_conflict"


def test_modality_gate_keeps_image_and_pdf_but_rejects_audio_video_and_unknown_image() -> None:
    parameters = ModelRequestParameters()
    accepted = [ModelRequest(parts=[UserPromptPart(content=[
        "inspect", BinaryContent(data=b"%PDF-test", media_type="application/pdf"),
        BinaryContent(data=b"png", media_type="image/png"),
    ])])]
    validate_request(api_url=OPENAI_URL, model=LUNA, effective_reasoning="none",
                     model_request_parameters=parameters, messages=accepted, model_settings={})

    # Tool-return JSON may describe a file but is not binary provider input.
    tool_json = [ModelRequest(parts=[ToolReturnPart("describe", {"mime_type": "audio/wav"},
                                                   tool_call_id="call-1")])]
    validate_request(api_url=OPENAI_URL, model=LUNA, effective_reasoning="none",
                     model_request_parameters=parameters, messages=tool_json, model_settings={})

    for media_type in ("audio/wav", "video/mp4", "image/svg+xml", "application/octet-stream"):
        messages = [ModelRequest(parts=[UserPromptPart(content=[
            BinaryContent(data=b"payload", media_type=media_type),
        ])])]
        with pytest.raises(ProviderProtocolError) as caught:
            validate_request(api_url=OPENAI_URL, model=LUNA, effective_reasoning="none",
                             model_request_parameters=parameters, messages=messages, model_settings={})
        assert caught.value.code == "unsupported_modality"

    with pytest.raises(ProviderProtocolError) as audio_output:
        validate_request(api_url=OPENAI_URL, model=LUNA, effective_reasoning="none",
                         model_request_parameters=parameters, messages=[],
                         model_settings={"extra_body": {"modalities": ["text", "audio"]}})
    assert audio_output.value.code == "unsupported_modality"


def test_sanitizer_removes_only_exact_known_gpt6_sampling_fields() -> None:
    settings = {
        "temperature": 0.2,
        "top_p": 0.4,
        "openai_top_logprobs": 2,
        "openai_logprobs": True,
        "extra_body": {"temperature": 0.7, "top_p": 0.8, "logprobs": True, "custom_flag": "kept"},
    }
    safe = sanitize_chat_settings(api_url=OPENAI_URL, model=SOL, effective_reasoning="high",
                                  model_settings=settings)
    assert "temperature" not in safe and "top_p" not in safe
    assert "openai_top_logprobs" not in safe and "openai_logprobs" not in safe
    assert safe["extra_body"] == {"custom_flag": "kept"}
    assert settings["extra_body"]["custom_flag"] == "kept"

    none_safe = sanitize_chat_settings(api_url=OPENAI_URL, model=LUNA, effective_reasoning="none",
                                       model_settings=settings)
    assert none_safe["temperature"] == 0.2
    assert none_safe["extra_body"]["temperature"] == 0.7

    ark = sanitize_chat_settings(api_url="https://ark.cn-beijing.volces.com/api/v3", model="gpt-6-sol",
                                 effective_reasoning="high", model_settings=settings)
    assert ark == settings


@dataclass
class _Parameters:
    function_tools: list[Any] = field(default_factory=list)
    output_tools: list[Any] = field(default_factory=list)
    native_tools: list[Any] = field(default_factory=list)
    instruction_parts: list[Any] = field(default_factory=list)


@dataclass
class _RequestContext:
    messages: list[Any] = field(default_factory=lambda: [ModelRequest(parts=[UserPromptPart(content="hello")])])
    model_request_parameters: Any = field(default_factory=_Parameters)
    model_settings: dict[str, Any] = field(default_factory=dict)
    streaming: bool = False
    model_id: str | None = None


class _ReservationReached(Exception):
    pass


class _Writer:
    quota_config: dict[str, Any] = {}

    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_issue(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def redact(self, value: str) -> str:
        return value


class _StopAtReservation:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def reserve_request(self, *args: Any, **kwargs: Any) -> tuple[str, int]:
        self.calls.append((args, kwargs))
        raise _ReservationReached

    def abandon_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _meter(monkeypatch: pytest.MonkeyPatch, *, model: str = SOL, api_url: str = OPENAI_URL,
           estimated_input: int = 500) -> tuple[Any, _StopAtReservation]:
    config = {
        "assistant_api_url": api_url,
        "assistant_model": model,
        "assistant_model_limits": {api_url: {model: {
            "context_budget_version": 2,
            "context_window_tokens": 10_000,
            "reserved_output_tokens": 1_500,
            "compression_reserve_tokens": 0,
            "history_keep_tokens": 0,
            "compaction_threshold_percent": 0,
            "max_input_tokens": 0,
            "legacy_values": {},
        }}}
    }
    store = _StopAtReservation()
    meter = object.__new__(runtime.BudgetAndTools)
    meter.store = store
    meter.config = config
    meter.session = "unit-session"
    meter.request = "unit-request"
    meter.model = model
    meter.writer = _Writer()
    meter.ledger = None
    meter.client = None
    meter.role = "main"
    meter.mode = "operate"
    meter.strength = "auto"
    meter.capabilities = reasoning_capabilities(api_url, model)
    meter.shared = {"requests": 0, "used_tokens": 0, "used_input_tokens": 0,
                    "streams": {}, "last_steps": {}}
    meter.limits = {"requests": 20, "input_tokens": 2_000_000,
                    "total_tokens": 2_000_000}
    meter.archive = SimpleNamespace(project_history=lambda messages: messages)
    meter.started = 0.0
    monkeypatch.setattr(runtime, "request_context_estimate", lambda _messages, _parameters: {
        "input_tokens": estimated_input,
        "estimated_input_tokens": estimated_input,
        "basis": "local protocol test",
    })
    monkeypatch.setattr(runtime, "language_instructions", lambda _config: "")
    monkeypatch.setattr(runtime, "development_enabled", lambda _config: False)
    return meter, store


def _drive_to_reservation(coroutine: Any) -> Any:
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    coroutine.close()
    pytest.fail("runtime preflight awaited before reaching the fake reservation boundary")


def test_runtime_protocol_rejection_happens_before_reservation_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meter, store = _meter(monkeypatch, model=SOL)
    context = _RequestContext(model_request_parameters=_Parameters(function_tools=[object()]))
    fake_transport_calls = 0

    async def fake_handler(_context: Any) -> Any:
        nonlocal fake_transport_calls
        fake_transport_calls += 1

    with pytest.raises(ProviderProtocolError) as caught:
        _drive_to_reservation(meter.wrap_model_request(
            SimpleNamespace(run_id="protocol-rejection"), request_context=context, handler=fake_handler,
        ))
    assert caught.value.required_protocol == "responses"
    assert store.calls == []
    assert fake_transport_calls == 0


@pytest.mark.parametrize("raw_url", [
    "https://api.openai.com",
    "https://api.openai.com/",
    "https://api.openai.com////",
    "https://api.openai.com/v1",
    "https://api.openai.com/v1////",
    "https://api.openai.com/chat/completions",
    "https://api.openai.com/chat/completions////",
    "https://api.openai.com/v1/chat/completions",
    "https://api.openai.com/v1/chat/completions////",
])
def test_runtime_openai_url_aliases_keep_protocol_gate_before_reservation(
    monkeypatch: pytest.MonkeyPatch, raw_url: str,
) -> None:
    assert runtime.provider_base_url(raw_url) == OPENAI_URL
    capabilities = reasoning_capabilities(raw_url, SOL)
    assert capabilities["official_model_profile"]["chat_function_tools"] == "responses_only"
    meter, store = _meter(monkeypatch, model=SOL, api_url=raw_url)
    assert meter.capabilities["official_model_profile"]["chat_function_tools"] == "responses_only"
    context = _RequestContext(model_request_parameters=_Parameters(function_tools=[object()]))
    fake_transport_calls = 0

    async def fake_handler(_context: Any) -> Any:
        nonlocal fake_transport_calls
        fake_transport_calls += 1

    with pytest.raises(ProviderProtocolError) as caught:
        _drive_to_reservation(meter.wrap_model_request(
            SimpleNamespace(run_id="openai-url-alias-protocol-rejection"),
            request_context=context,
            handler=fake_handler,
        ))
    assert caught.value.code == "responses_required"
    assert caught.value.required_protocol == "responses"
    assert store.calls == []
    assert fake_transport_calls == 0


def test_runtime_rejects_conflicting_request_context_reasoning_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meter, store = _meter(monkeypatch, model=LUNA)
    context = _RequestContext(model_settings={"extra_body": {"reasoning_effort": "none"}})
    fake_transport_calls = 0

    async def fake_handler(_context: Any) -> Any:
        nonlocal fake_transport_calls
        fake_transport_calls += 1

    with pytest.raises(ProviderProtocolError) as caught:
        _drive_to_reservation(meter.wrap_model_request(
            SimpleNamespace(run_id="settings-conflict"), request_context=context, handler=fake_handler,
        ))
    assert caught.value.code == "request_settings_conflict"
    assert store.calls == []
    assert fake_transport_calls == 0


def test_runtime_over_budget_rejection_happens_before_reservation_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meter, store = _meter(monkeypatch, model=SOL, estimated_input=20_000)
    fake_transport_calls = 0

    async def fake_handler(_context: Any) -> Any:
        nonlocal fake_transport_calls
        fake_transport_calls += 1

    with pytest.raises(RuntimeError):
        _drive_to_reservation(meter.wrap_model_request(
            SimpleNamespace(run_id="over-budget"), request_context=_RequestContext(), handler=fake_handler,
        ))
    assert store.calls == []
    assert fake_transport_calls == 0


def _completion_response() -> dict[str, Any]:
    return {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "created": 1,
        "model": LUNA,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                      "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
    }


def test_fake_transport_posts_chat_with_image_and_pdf_shapes() -> None:
    async def scenario() -> None:
        captured: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx2.Response(200, json=_completion_response())

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            sdk = AsyncOpenAI(api_key="local-fake-key", base_url=OPENAI_URL,
                              http_client=http_client, max_retries=0)
            model = runtime._chat_model(LUNA, reasoning_capabilities(OPENAI_URL, LUNA), sdk)
            messages = [ModelRequest(parts=[UserPromptPart(content=[
                "Describe these files",
                BinaryContent(data=b"pdf-body", media_type="application/pdf"),
                BinaryContent(data=b"png-body", media_type="image/png"),
            ])])]
            response = await model.request(
                messages,
                {"openai_reasoning_effort": "none", "temperature": 0.1,
                 "extra_body": {"custom_flag": "kept"}},
                ModelRequestParameters(),
            )
            assert response.parts

        assert len(captured) == 1
        body = captured[0]
        assert body["messages"][0]["role"] == "user"
        content = body["messages"][0]["content"]
        assert any(item.get("type") == "file"
                   and item.get("file", {}).get("filename", "").endswith(".pdf")
                   and item.get("file", {}).get("file_data", "").startswith("data:application/pdf;base64,")
                   for item in content), content
        assert any(item.get("type") == "image_url"
                   and item.get("image_url", {}).get("url", "").startswith("data:image/png;base64,")
                   for item in content)
        assert body["custom_flag"] == "kept"
        assert body["temperature"] == 0.1

    asyncio.run(scenario())


def test_non_none_reasoning_strips_sampling_from_actual_fake_chat_request() -> None:
    async def scenario() -> None:
        captured: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx2.Response(200, json=_completion_response())

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            sdk = AsyncOpenAI(api_key="local-fake-key", base_url=OPENAI_URL,
                              http_client=http_client, max_retries=0)
            model = runtime._chat_model(SOL, reasoning_capabilities(OPENAI_URL, SOL), sdk)
            settings = sanitize_chat_settings(
                api_url=OPENAI_URL,
                model=SOL,
                effective_reasoning="high",
                model_settings={
                    "openai_reasoning_effort": "high",
                    "temperature": 0.1,
                    "top_p": 0.8,
                    "openai_top_logprobs": 3,
                    "extra_body": {"custom_flag": "kept", "temperature": 0.6, "top_p": 0.4},
                },
            )
            await model.request(
                [ModelRequest(parts=[UserPromptPart(content="hello")])],
                settings,
                ModelRequestParameters(),
            )

        body = captured[0]
        assert body["reasoning_effort"] == "high"
        assert "temperature" not in body
        assert "top_p" not in body
        assert "top_logprobs" not in body
        assert body["custom_flag"] == "kept"

    asyncio.run(scenario())


def test_luna_none_function_tools_round_trip_through_real_chat_request_shapes() -> None:
    async def scenario() -> None:
        captured: list[dict[str, Any]] = []
        call_id = "call-luna-1"

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            if len(captured) == 1:
                return httpx2.Response(200, json={
                    "id": "chatcmpl-tool",
                    "object": "chat.completion",
                    "created": 1,
                    "model": LUNA,
                    "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": None,
                        "tool_calls": [{"id": call_id, "type": "function", "function": {
                            "name": "lookup", "arguments": "{\"query\":\"x\"}",
                        }}],
                    }, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
                })
            return httpx2.Response(200, json=_completion_response())

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            sdk = AsyncOpenAI(api_key="local-fake-key", base_url=OPENAI_URL,
                              http_client=http_client, max_retries=0)
            model = runtime._chat_model(LUNA, reasoning_capabilities(OPENAI_URL, LUNA), sdk)
            tool = ToolDefinition(
                name="lookup",
                description="Look up a test record",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
            parameters = ModelRequestParameters(function_tools=[tool])
            messages = [ModelRequest(parts=[UserPromptPart(content="Find x")])]
            validate_request(api_url=OPENAI_URL, model=LUNA, effective_reasoning="none",
                             model_request_parameters=parameters, messages=messages,
                             model_settings={"openai_reasoning_effort": "none"})
            tool_response = await model.request(
                messages,
                {"openai_reasoning_effort": "none"},
                parameters,
            )
            tool_part = next(part for part in tool_response.parts if isinstance(part, ToolCallPart))
            assert tool_part.tool_call_id == call_id
            returned = ModelRequest(parts=[ToolReturnPart("lookup", "record x", tool_call_id=call_id)])
            final = await model.request(
                [*messages, tool_response, returned],
                {"openai_reasoning_effort": "none"},
                ModelRequestParameters(),
            )
            assert final.parts

        assert len(captured) == 2
        first_tools = captured[0]["tools"]
        assert first_tools[0]["type"] == "function"
        assert first_tools[0]["function"]["name"] == "lookup"
        assert first_tools[0]["function"]["parameters"]["properties"]["query"]["type"] == "string"
        second_messages = captured[1]["messages"]
        assert [message["role"] for message in second_messages] == ["user", "assistant", "tool"]
        assert second_messages[1]["tool_calls"][0]["id"] == call_id
        assert second_messages[2]["tool_call_id"] == call_id
        assert second_messages[2]["content"] == "record x"

    asyncio.run(scenario())


def test_unlisted_openai_chat_model_keeps_legacy_function_tool_path() -> None:
    # Static protocol rules are intentionally limited to the exact verified GPT-6 profiles.
    validate_request(
        api_url=OPENAI_URL,
        model="gpt-4o",
        effective_reasoning=None,
        model_request_parameters=ModelRequestParameters(function_tools=[object()]),
        messages=[],
        model_settings={},
    )


class _GatedSSEStream(httpx2.AsyncByteStream):
    def __init__(self, *, wait_before_first: bool) -> None:
        self.wait_before_first = wait_before_first
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    @staticmethod
    def _frame(content: str | None) -> bytes:
        chunk = {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": LUNA,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                          "finish_reason": None}],
        }
        return f"data: {json.dumps(chunk)}\r\n\r\n".encode("utf-8")

    async def __aiter__(self):
        self.started.set()
        # OpenAIChatModel.request_stream reads a first chunk before its context
        # manager yields. Prime it with a role-only chunk, then gate user text.
        yield self._frame(None)
        if self.wait_before_first:
            await self.gate.wait()
        if self.closed.is_set():
            return
        yield self._frame("first token")
        await self.gate.wait()
        if not self.closed.is_set():
            yield b"data: [DONE]\r\n\r\n"

    async def aclose(self) -> None:
        self.closed.set()
        self.gate.set()


async def _start_stream(stream_source: _GatedSSEStream):
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream_source)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk = AsyncOpenAI(api_key="local-fake-key", base_url=OPENAI_URL,
                      http_client=http_client, max_retries=0)
    model = runtime._chat_model(LUNA, reasoning_capabilities(OPENAI_URL, LUNA), sdk)
    context = model.request_stream(
        [ModelRequest(parts=[UserPromptPart(content="hello")])],
        {"openai_reasoning_effort": "none"},
        ModelRequestParameters(),
    )
    return http_client, context


@pytest.mark.parametrize("wait_before_first", [True, False], ids=["before-first-token", "after-first-token"])
def test_fake_stream_can_cancel_before_or_after_first_token(wait_before_first: bool) -> None:
    async def scenario() -> None:
        source = _GatedSSEStream(wait_before_first=wait_before_first)
        http_client, stream_context = await _start_stream(source)
        pending = None
        try:
            async with stream_context as streamed:
                iterator = streamed.__aiter__()
                if wait_before_first:
                    pending = asyncio.create_task(anext(iterator))
                    await asyncio.wait_for(source.started.wait(), timeout=3)
                    await asyncio.wait_for(streamed.cancel(), timeout=3)
                    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=3)
                    assert streamed.get().state == "interrupted"
                else:
                    first_text = None
                    while first_text is None:
                        event = await asyncio.wait_for(anext(iterator), timeout=3)
                        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                            first_text = event.part.content
                        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                            first_text = event.delta.content_delta
                    assert first_text == "first token"
                    await asyncio.wait_for(streamed.cancel(), timeout=3)
                    assert streamed.get().state == "interrupted"
                assert source.closed.is_set()
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=3)
            await asyncio.wait_for(http_client.aclose(), timeout=3)

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


class _SyntheticPath:
    def __init__(self, path: Any, reported_size: int) -> None:
        self._path = path
        self._reported_size = reported_size
        self.name = path.name

    def stat(self) -> SimpleNamespace:
        return SimpleNamespace(st_size=self._reported_size)

    def read_bytes(self) -> bytes:
        return self._path.read_bytes()

    def read_text(self, **kwargs: Any) -> str:
        return self._path.read_text(**kwargs)


def _use_synthetic_media_paths(monkeypatch: pytest.MonkeyPatch, paths: dict[str, Any],
                               *, mime: str = "text/plain") -> list[str]:
    seen: list[str] = []

    def data_path(value: str, *, root_allowed: bool = False) -> Any:
        assert root_allowed is False
        assert value in paths
        seen.append(value)
        return paths[value]

    monkeypatch.setattr(media, "data_path", data_path)
    monkeypatch.setattr(media, "file_info", lambda _path: {"mime": mime, "reference": "synthetic"})
    return seen


def test_attachment_count_and_aggregate_size_boundaries_use_only_synthetic_paths(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backing = tmp_path / "small.txt"
    backing.write_text("tiny", encoding="utf-8")
    logical_path = "synthetic:small.txt"

    seen = _use_synthetic_media_paths(monkeypatch, {
        logical_path: _SyntheticPath(backing, 1),
    })
    result = media.build_prompt("prompt", [{"path": logical_path}] * 8)
    assert len(result) == 17
    assert len(seen) == 8

    seen.clear()
    with pytest.raises(ValueError):
        media.build_prompt("prompt", [{"path": logical_path}] * 9)
    assert seen == []

    first = "synthetic:half-a.txt"
    second = "synthetic:half-b.txt"
    _use_synthetic_media_paths(monkeypatch, {
        first: _SyntheticPath(backing, MAX_FILE_BYTES // 2),
        second: _SyntheticPath(backing, MAX_FILE_BYTES - MAX_FILE_BYTES // 2),
    })
    assert len(media.build_prompt("prompt", [{"path": first}, {"path": second}])) == 5

    _use_synthetic_media_paths(monkeypatch, {
        first: _SyntheticPath(backing, MAX_FILE_BYTES // 2),
        second: _SyntheticPath(backing, MAX_FILE_BYTES - MAX_FILE_BYTES // 2 + 1),
    })
    with pytest.raises(ValueError):
        media.build_prompt("prompt", [{"path": first}, {"path": second}])


def test_attachment_text_limit_accepts_64000_and_rejects_64001(tmp_path: Any,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    backing = tmp_path / "text.txt"
    backing.write_text("a" * 64_000, encoding="utf-8")
    logical_path = "synthetic:text.txt"
    _use_synthetic_media_paths(monkeypatch, {logical_path: _SyntheticPath(backing, 1)})
    accepted = media.build_prompt("prompt", [{"path": logical_path}])
    assert accepted[-1] == "a" * 64_000

    backing.write_text("a" * 64_001, encoding="utf-8")
    with pytest.raises(ValueError):
        media.build_prompt("prompt", [{"path": logical_path}])
