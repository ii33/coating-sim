"""Preflight protocol and modality checks for explicitly verified providers."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic_ai.messages import (
    BinaryContent,
    NativeToolCallPart,
    NativeToolReturnPart,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from coat_sim.agent.modelclient import official_openai_model_profile


class ProviderProtocolError(RuntimeError):
    """A request cannot safely be sent through the selected model protocol."""

    def __init__(self, code: str, message: str, *, model: str,
                 required_protocol: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.model = model
        self.required_protocol = required_protocol


def _message_parts(messages: Sequence[Any]) -> list[Any]:
    parts: list[Any] = []
    for message in messages:
        message_parts = getattr(message, "parts", None)
        if message_parts is None and isinstance(message, Mapping):
            message_parts = message.get("parts")
        if isinstance(message_parts, Sequence) and not isinstance(message_parts, (str, bytes)):
            parts.extend(message_parts)
    return parts


def _media_type(value: Any) -> str | None:
    if not isinstance(value, BinaryContent):
        return None
    media_type = getattr(value, "media_type", None)
    if not isinstance(media_type, str):
        return None
    return media_type.split(";", 1)[0].strip().lower()


def _url_media_kind(value: Any) -> str | None:
    name = type(value).__name__.lower()
    return {
        "imageurl": "image",
        "audiourl": "audio",
        "videourl": "video",
        "documenturl": "document",
    }.get(name)


def _contains_media(value: Any, kind: str) -> bool:
    if value is None:
        return False
    media_type = _media_type(value)
    if media_type and media_type.startswith(kind + "/"):
        return True
    if _url_media_kind(value) == kind:
        return True
    if bool(getattr(value, f"is_{kind}", False)):
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_media(item, kind) for item in value)
    return False


def _unsupported_media_type(value: Any, allowed_images: Sequence[str]) -> str | None:
    url_kind = _url_media_kind(value)
    if url_kind == "audio":
        return "audio/url"
    if url_kind == "video":
        return "video/url"
    if url_kind == "document":
        url = getattr(value, "url", "")
        return None if isinstance(url, str) and url.split("?", 1)[0].lower().endswith(".pdf") else "application/document-url"
    # URL-backed images are mapped by the SDK as image_url. Their MIME type is
    # intentionally not guessed from arbitrary URL strings.
    if url_kind == "image":
        return None
    media_type = _media_type(value)
    if media_type:
        if media_type.startswith("audio/") or media_type.startswith("video/"):
            return media_type
        if media_type.startswith("image/") and media_type not in allowed_images:
            return media_type
        if media_type.startswith("application/") and media_type != "application/pdf":
            return media_type
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            unsupported = _unsupported_media_type(item, allowed_images)
            if unsupported:
                return unsupported
    return None


def _has_function_history(parts: Sequence[Any]) -> bool:
    for part in parts:
        if isinstance(part, (ToolCallPart, ToolReturnPart)):
            return True
        if isinstance(part, RetryPromptPart) and part.tool_name is not None:
            return True
    return False


def _has_native_tool_history(parts: Sequence[Any]) -> bool:
    return any(isinstance(part, (NativeToolCallPart, NativeToolReturnPart)) for part in parts)


def _has_audio_output(model_settings: Mapping[str, Any]) -> bool:
    extra = model_settings.get("extra_body")
    if not isinstance(extra, Mapping):
        return False
    modalities = extra.get("modalities")
    if isinstance(modalities, str):
        has_audio_modality = modalities.lower() == "audio"
    elif isinstance(modalities, Sequence):
        has_audio_modality = any(isinstance(item, str) and item.lower() == "audio" for item in modalities)
    else:
        has_audio_modality = False
    return has_audio_modality or "audio" in extra


def _settings_conflict(model: str, key: str) -> ProviderProtocolError:
    return ProviderProtocolError(
        "request_settings_conflict",
        f"The {key} override conflicts with the resolved settings for {model}; the request was stopped before sending.",
        model=model,
    )


def validate_request(*, api_url: str, model: str, effective_reasoning: str | None,
                     model_request_parameters: Any, messages: Sequence[Any],
                     model_settings: Mapping[str, Any]) -> None:
    """Reject known GPT-6 Chat protocol/modality mismatches before transport I/O.

    Only exact GPT-6 model IDs at the official OpenAI API base have static rules.
    Unknown endpoints, model IDs, and provider entitlements remain outside this gate.
    """
    profile = official_openai_model_profile(api_url, model)
    if profile is None:
        return

    extra = model_settings.get("extra_body")
    extra = extra if isinstance(extra, Mapping) else {}
    expected_reasoning = effective_reasoning
    for settings in (model_settings, extra):
        for key in ("openai_reasoning_effort", "reasoning_effort"):
            value = settings.get(key)
            if value is not None and value != expected_reasoning:
                raise _settings_conflict(model, key)

    # Raw body fields can bypass the typed model, messages, stream, and tool
    # parameters inspected below. Presence itself is unsafe, even for None or [].
    for key in ("tools", "tool_choice", "model", "messages", "stream"):
        if key in extra:
            raise _settings_conflict(model, f"extra_body {key}")

    parts = _message_parts(messages)
    if not profile["audio_supported"] and (
        _has_audio_output(model_settings)
        or any(_contains_media(getattr(part, "content", None), "audio") for part in parts)
    ):
        raise ProviderProtocolError(
            "unsupported_modality",
            f"Audio is not supported for {model}; the request was stopped before sending.",
            model=model,
        )
    if not profile["video_supported"] and any(
        _contains_media(getattr(part, "content", None), "video") for part in parts
    ):
        raise ProviderProtocolError(
            "unsupported_modality",
            f"Video input is not supported for {model}; the request was stopped before sending.",
            model=model,
        )
    allowed_images = profile.get("image_media_types", ())
    for part in parts:
        unsupported = _unsupported_media_type(getattr(part, "content", None), allowed_images)
        if unsupported:
            raise ProviderProtocolError(
                "unsupported_modality",
                f"This media type is not supported for {model}; the request was stopped before sending.",
                model=model,
            )

    native_tools = (getattr(model_request_parameters, "native_tools", None)
                    or getattr(model_request_parameters, "builtin_tools", None) or ())
    builtin_tools_present = bool(native_tools) or _has_native_tool_history(parts)
    function_tools_present = bool(
        getattr(model_request_parameters, "function_tools", None)
        or getattr(model_request_parameters, "output_tools", None)
        or _has_function_history(parts)
        or extra.get("tools")
    )
    if builtin_tools_present or (function_tools_present and profile["chat_function_tools"] == "responses_only"):
        raise ProviderProtocolError(
            "responses_required",
            f"This operation for {model} requires the Responses API; this runtime only supports Chat Completions, so it was stopped before sending.",
            model=model,
            required_protocol="responses",
        )
    if function_tools_present and profile["chat_function_tools"] == "none_only" and expected_reasoning != "none":
        raise ProviderProtocolError(
            "responses_required",
            f"Chat function calling for {model} requires reasoning_effort=none; this request needs the Responses API and was stopped before sending.",
            model=model,
            required_protocol="responses",
        )


def sanitize_chat_settings(*, api_url: str, model: str, effective_reasoning: str | None,
                           model_settings: Mapping[str, Any]) -> dict[str, Any]:
    """Remove restricted GPT-6 sampling fields without changing other providers."""
    result = dict(model_settings)
    profile = official_openai_model_profile(api_url, model)
    if profile is None:
        return result

    extra = result.get("extra_body")
    if isinstance(extra, Mapping):
        extra = dict(extra)
        # The typed model setting is authoritative after validate_request has
        # rejected any conflicting raw override.
        extra.pop("reasoning_effort", None)
        if effective_reasoning != "none":
            for key in ("temperature", "top_p", "top_logprobs", "logprobs"):
                extra.pop(key, None)
        result["extra_body"] = extra

    if effective_reasoning != "none":
        for key in ("temperature", "top_p", "openai_top_logprobs", "top_logprobs",
                    "openai_logprobs", "logprobs"):
            result.pop(key, None)
    return result
