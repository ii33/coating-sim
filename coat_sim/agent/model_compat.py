"""Ark Chat opaque reasoning round trips using the pinned model extension hooks.

Opaque ciphertext stays in runtime messages; only provider-supplied displayable
thinking text is emitted separately by the runtime.  These hooks are documented in Pydantic AI 2.51's source.
"""
from __future__ import annotations

from typing import Any
from collections.abc import Mapping
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse


def _cache_usage(usage: Any) -> dict[str, Any] | None:
    """Preserve explicit provider cache counts; absent fields are unknown, not zero."""
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        payload = usage
    else:
        dump = getattr(usage, "model_dump", None)
        if not callable(dump):
            return None
        payload = dump(exclude_none=True)
    if not isinstance(payload, Mapping):
        return None
    details = payload.get("prompt_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    input_details = payload.get("input_tokens_details")
    input_details = input_details if isinstance(input_details, Mapping) else {}

    def first_count(values: tuple) -> int | None:
        for value in values:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9_223_372_036_854_775_807:
                return value
        return None

    cached = first_count((details.get("cached_tokens"), input_details.get("cached_tokens"),
                          payload.get("prompt_cache_hit_tokens"),
                          payload.get("cache_read_input_tokens")))
    created = first_count((details.get("cache_creation_tokens"), details.get("cache_write_tokens"),
                           input_details.get("cache_write_tokens"),
                           details.get("cache_creation_input_tokens"),
                           payload.get("cache_creation_input_tokens")))
    if cached is None and created is None:
        return None
    return {"cached_input_tokens": cached, "cache_creation_input_tokens": created,
            "cache_reporting_supported": True}


def _merge_cache_usage(previous: Any, update: dict[str, Any] | None) -> dict[str, Any] | None:
    """A usage-less chunk cannot erase the final usage chunk's cache metadata."""
    if not isinstance(previous, Mapping):
        return update
    result = {"cached_input_tokens": previous.get("cached_input_tokens"),
              "cache_creation_input_tokens": previous.get("cache_creation_input_tokens"),
              "cache_reporting_supported": True}
    if update is not None:
        for key in ("cached_input_tokens", "cache_creation_input_tokens"):
            if update.get(key) is not None:
                result[key] = update[key]
    return result


class ArkStreamedResponse(OpenAIStreamedResponse):
    def _map_provider_details(self, chunk: Any) -> dict[str, Any] | None:
        details = super()._map_provider_details(chunk) or {}
        cache = _merge_cache_usage((self.provider_details or {}).get("coat_cache_usage"),
                                   _cache_usage(getattr(chunk, "usage", None)))
        if cache is not None:
            details["coat_cache_usage"] = cache
        choices = getattr(chunk, "choices", None) or []
        delta = getattr(choices[0], "delta", None) if choices else None
        encrypted = getattr(delta, "encrypted_content", None)
        if encrypted is not None:
            previous = (self.provider_details or {}).get("ark_encrypted_content")
            # Streaming string fields arrive as deltas. Structured opaque
            # values are retained as supplied, without interpreting ciphertext.
            details["ark_encrypted_content"] = (
                previous + encrypted if isinstance(previous, str) and isinstance(encrypted, str)
                else encrypted
            )
        return details or None


class ArkChatModel(OpenAIChatModel):
    @property
    def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
        return ArkStreamedResponse

    def _process_response(self, response: Any) -> Any:
        result = super()._process_response(response)
        cache = _cache_usage(getattr(response, "usage", None))
        if cache is not None:
            result.provider_details = {**(result.provider_details or {}), "coat_cache_usage": cache}
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        encrypted = getattr(message, "encrypted_content", None)
        if encrypted is not None:
            result.provider_details = {**(result.provider_details or {}),
                                       "ark_encrypted_content": encrypted}
        return result

    def _map_model_response(self, message: Any) -> Any:
        mapped = super()._map_model_response(message)
        if mapped is not None and message.provider_url == self._provider.base_url:
            encrypted = (message.provider_details or {}).get("ark_encrypted_content")
            if encrypted is not None:
                mapped["encrypted_content"] = encrypted
        return mapped


class MediaStreamedResponse(ArkStreamedResponse):
    def _map_provider_details(self, chunk):
        details = super()._map_provider_details(chunk) or {}
        choices = getattr(chunk, "choices", None) or []
        delta = getattr(choices[0], "delta", None) if choices else None
        images = getattr(delta, "images", None)
        if images:
            details["coat_images"] = list((self.provider_details or {}).get("coat_images", [])) + list(images)
        audio = getattr(delta, "audio", None)
        if audio:
            fields = audio if isinstance(audio, dict) else audio.model_dump(exclude_none=True)
            previous = (self.provider_details or {}).get("coat_audio", {})
            merged = dict(previous)
            for key in ("data", "transcript"):
                if fields.get(key):
                    merged[key] = (str(previous.get(key, "")) + str(fields[key]))[:45000000]
            details["coat_audio"] = merged
        return details or None


class _SingleCompletionStream:
    """Bridge an opt-in non-streaming audio completion into SDK stream hooks."""
    def __init__(self, chunk):
        self.chunk = chunk
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def __aiter__(self):
        yield self.chunk
    async def close(self):
        pass


class MediaChatModel(ArkChatModel):
    async def _completions_create(self, messages, stream, model_settings, model_request_parameters):
        modalities = model_settings.get("extra_body", {}).get("modalities", [])
        if not stream or "audio" not in modalities:
            return await super()._completions_create(messages, stream, model_settings, model_request_parameters)
        # Common Chat audio APIs require non-streaming WAV output. Preserve
        # the framework's stream lifecycle while requesting one full audio reply.
        from openai.types.chat import ChatCompletionChunk
        completion = await super()._completions_create(messages, False, model_settings, model_request_parameters)
        choices = []
        for choice in completion.choices:
            delta = choice.message.model_dump(exclude_none=True)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [{**call, "index": index} for index, call in enumerate(delta["tool_calls"])]
            audio = delta.get("audio") or {}
            if not delta.get("content") and audio.get("transcript"):
                delta["content"] = audio["transcript"]
            choices.append({"index": choice.index, "delta": delta, "finish_reason": choice.finish_reason})
        chunk = ChatCompletionChunk.model_validate({"id": completion.id, "object": "chat.completion.chunk",
            "created": completion.created, "model": completion.model, "choices": choices,
            "usage": completion.usage.model_dump() if completion.usage else None})
        return _SingleCompletionStream(chunk)

    @property
    def _streamed_response_cls(self):
        return MediaStreamedResponse

    def _process_response(self, response):
        result = super()._process_response(response)
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        images = getattr(message, "images", None)
        if images:
            result.provider_details = {**(result.provider_details or {}), "coat_images": images}
        audio = getattr(message, "audio", None)
        if audio:
            fields = audio if isinstance(audio, dict) else audio.model_dump(exclude_none=True)
            result.provider_details = {**(result.provider_details or {}), "coat_audio": fields}
        return result
