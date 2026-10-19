"""Offline provider catalog backed by ``provider_catalog_sources.json``.

Catalog IDs are documented suggestions, not proof of account entitlement or
successful inference. The separate discovery cache contains only runtime
results. Provider endpoints are matched by scheme, host, and complete path.
"""
from __future__ import annotations

from copy import deepcopy
from html import unescape
import json
import math
import time
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit


_SOURCE_PATH = Path(__file__).with_name("provider_catalog_sources.json")
_SOURCE_DATA = json.loads(_SOURCE_PATH.read_text(encoding="utf-8"))
CATALOG_VERIFIED_DATE = str(_SOURCE_DATA["verified_at"])
MODEL_DISCOVERY_STATUSES = ("confirmed", "unconfirmed", "unsupported", "alternate_api")
_CONTEXT_CAPACITY_CATALOG = _SOURCE_DATA.get("context_capacity_catalog", {})
CONTEXT_CAPACITY_CATALOG_VERSION = int(_CONTEXT_CAPACITY_CATALOG.get("version", 0) or 0)

_CONFIRMED_MODELS_LIST = {
    "deepseek_official", "moonshot_kimi_cn", "minimax_cn", "minimax_intl",
    "siliconflow_cn", "openai", "google_gemini", "xai", "mistral", "groq", "openrouter",
}
_ALTERNATE_MODELS_LIST = {"volcengine_ark_agent_plan"}
_PREVIEW_STATUSES = {"preview", "public_preview"}
_RESTRICTED_STATUSES = {"enterprise"}


def normalize_catalog_endpoint(value: str) -> str:
    """Canonicalize an API base URL without conflating provider paths."""
    parts = urlsplit(str(value).strip().rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def context_capacity_profile_for(api_url: str, model: str) -> dict[str, object]:
    """Return exact offline hard limits for one normalized endpoint/model pair.

    A zero hard limit means unknown or no independently documented hard cap.
    Catalog capacity is not an entitlement or availability claim.
    """
    catalog = _CONTEXT_CAPACITY_CATALOG
    try:
        endpoint = normalize_catalog_endpoint(api_url)
    except (TypeError, ValueError):
        endpoint = ""
    model_id = model.strip() if isinstance(model, str) else ""
    raw = catalog.get("profiles", {}).get(endpoint, {}).get(model_id) if endpoint and model_id else None
    matched = isinstance(raw, dict)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "context_window_tokens": _catalog_token_limit(raw.get("context_window_tokens")),
        "max_input_tokens": _catalog_token_limit(raw.get("max_input_tokens")),
        "max_output_tokens": _catalog_token_limit(raw.get("max_output_tokens")),
        "source": tuple(str(item) for item in raw.get("sources", ()) if isinstance(item, str)),
        "date": str(raw.get("verified_at", catalog.get("verified_at", ""))),
        "version": CONTEXT_CAPACITY_CATALOG_VERSION,
        "match": {
            "endpoint": endpoint,
            "model": model_id,
            "rule": str(catalog.get("match_rule", "exact endpoint and case-sensitive model ID")),
            "matched": matched,
        },
        "rounding_note": str(catalog.get("rounding_note", "")),
        "notes": str(raw.get("notes", "")),
        "unknown_reason": "" if matched else _capacity_unknown_reason(endpoint, catalog),
    }


def _catalog_token_limit(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _capacity_unknown_reason(endpoint: str, catalog: dict[str, object]) -> str:
    host = (urlsplit(endpoint).hostname or "").lower() if endpoint else ""
    reasons = catalog.get("unknown_reasons", {})
    if "volces.com" in host:
        return str(reasons.get("volcengine_ark", ""))
    if host == "openrouter.ai":
        return str(reasons.get("openrouter", ""))
    if host == "api.siliconflow.cn":
        return str(reasons.get("siliconflow", ""))
    if host == "api.anthropic.com":
        return str(reasons.get("anthropic_compatible", ""))
    return str(reasons.get("other_models", ""))


def _model_categories(record: dict[str, object]) -> dict[str, tuple[str, ...]]:
    return {
        "primary": tuple(str(item) for item in record.get("models", ())),
        "additional": tuple(str(item) for item in record.get("additional", ())),
        "preview": tuple(str(item) for item in record.get("preview", ())),
        "deprecated": tuple(str(item) for item in record.get("deprecated", ())),
    }


def _discovery_record(record: dict[str, object], base_url: str, selectable: bool) -> dict[str, str]:
    preset_id = str(record["id"])
    if not selectable:
        status = "unsupported"
        notes = "此套餐用途或协议不适用于本应用。"
        endpoint = ""
    elif preset_id in _ALTERNATE_MODELS_LIST:
        status = "alternate_api"
        notes = "另有控制面模型目录 API，鉴权方式不同；当前客户端不调用。"
        endpoint = ""
    elif preset_id in _CONFIRMED_MODELS_LIST:
        status = "confirmed"
        notes = "官方文档确认模型列表接口；运行时账号权限仍需以实际结果为准。"
        endpoint = base_url.rstrip("/") + "/models"
        if preset_id == "deepseek_official":
            endpoint = "https://api.deepseek.com/models"
    else:
        status = "unconfirmed"
        notes = "当前 Chat Completions 地址的模型列表接口未核实。"
        endpoint = ""
    return {"status": status, "method": "GET" if endpoint else "", "url": endpoint, "notes": notes}


def _to_profile(record: dict[str, object]) -> dict[str, object]:
    base_url = str(record["url"])
    selectable = bool(record.get("selectable", True))
    statuses = {str(key): str(value) for key, value in record.get("statuses", {}).items()}
    notes = unescape(str(record.get("notes", "")))
    display_hint = unescape(str(record.get("display_hint", "")).strip())
    categories = _model_categories(record)
    probe_ids = []
    for category in ("primary", "additional"):
        for model_id in categories[category]:
            model_status = statuses.get(model_id, "").lower()
            if any(label in model_status for label in _PREVIEW_STATUSES | _RESTRICTED_STATUSES):
                continue
            probe_ids.append(model_id)
    sources = tuple(unescape(str(source)) for source in record.get("sources", ()))
    discovery = _discovery_record(record, base_url, selectable)
    preset_id = str(record["id"])
    if preset_id.startswith("volcengine_ark_"):
        provider = "ark"
    else:
        provider = preset_id.split("_", 1)[0]
    return {
        "id": preset_id,
        "name": str(record["name"]),
        "provider": provider,
        "base_url": base_url,
        "protocol": str(record["protocol"]),
        "display_hint": display_hint,
        "models": categories,
        "model_statuses": statuses,
        "probe_model_ids": tuple(probe_ids) if selectable else (),
        "models_discovery": discovery,
        "usage_restriction": display_hint,
        "notes": notes,
        "capabilities": {
            "chat_completions": True,
            "model_list": discovery["status"] == "confirmed",
            "tool_use": "see_notes",
            "native_protocol": False,
        },
        "sources": sources,
        "verified_date": str(record.get("verified_at", CATALOG_VERIFIED_DATE)),
        "default_model": str(record.get("default_model") or next(iter(probe_ids), "")) if selectable else "",
        "default_reasoning_strength": str(record.get("default_reasoning_strength", "auto")),
        "model_details": deepcopy(record.get("model_details", {})),
        "selectable": selectable,
        "probe_allowed": selectable,
        # Preserve the settings page's existing automatic read / bounded
        # candidate fallback after a URL or saved-key change. Selecting a
        # preset updates the settings draft; all probes retain the existing gates.
        "default_auto_probe": selectable,
    }


_MAIN_PROFILES = tuple(_to_profile(row) for row in _SOURCE_DATA["presets"])
if len(_MAIN_PROFILES) != 22:
    raise ValueError(f"Provider source catalog must contain 22 presets; found {len(_MAIN_PROFILES)}")

# This is a protocol-information item only. It is intentionally outside the
# 22 OpenAI-compatible presets because the app does not implement Messages API.
_INFO_PROFILES = ({
    "id": "minimax_intl_anthropic_info",
    "name": "MiniMax 国际站 · Anthropic 兼容（仅信息；本应用不支持）",
    "provider": "minimax",
    "base_url": "https://api.minimax.io/anthropic",
    "protocol": "anthropic_messages",
    "models": {"primary": (), "additional": (), "preview": (), "deprecated": ()},
    "model_statuses": {}, "probe_model_ids": (),
    "display_hint": "仅作 Anthropic base URL 说明；本应用不支持 Messages 协议，此项不可选。",
    "models_discovery": {"status": "unsupported", "method": "", "url": "",
                          "notes": "当前客户端没有 Anthropic Messages 原生适配。"},
    "usage_restriction": "仅作 Anthropic base URL 说明；本应用不支持 Messages 协议，此项不可选。",
    "notes": "官方 Anthropic-compatible base URL；当前客户端没有该协议适配。",
    "capabilities": {"chat_completions": False, "model_list": False,
                     "tool_use": "unsupported", "native_protocol": False},
    "sources": ("https://platform.minimax.io/docs/api-reference/text-anthropic-api",),
    "verified_date": CATALOG_VERIFIED_DATE,
    "selectable": False, "probe_allowed": False, "default_auto_probe": False,
},)


def provider_presets(include_information: bool = True) -> list[dict[str, object]]:
    """Return all 22 OpenAI-compatible presets and optional protocol info rows."""
    rows = list(_MAIN_PROFILES)
    if include_information:
        rows.extend(_INFO_PROFILES)
    return deepcopy(rows)


def main_preset_count() -> int:
    return len(_MAIN_PROFILES)


def profile_for_url(api_url: str) -> dict[str, object] | None:
    try:
        endpoint = normalize_catalog_endpoint(api_url)
    except (TypeError, ValueError):
        return None
    for profile in (*_MAIN_PROFILES, *_INFO_PROFILES):
        if normalize_catalog_endpoint(str(profile["base_url"])) == endpoint:
            return deepcopy(profile)
    return None


def model_suggestions(api_url: str) -> list[dict[str, str]]:
    profile = profile_for_url(api_url)
    if profile is None or not profile.get("selectable"):
        return []
    result = []
    statuses = profile.get("model_statuses", {})
    for category in ("primary", "additional", "preview"):
        for model_id in profile["models"].get(category, ()):
            status = str(statuses.get(model_id, ""))
            display_category = "preview" if "preview" in status.lower() else category
            result.append({"id": str(model_id), "category": display_category, "status": status})
    return result


MODEL_METADATA_TTL_SECONDS = 7 * 24 * 3600
DEEPSEEK_MODELS_SCHEMA = "https://api-docs.deepseek.com/api/list-models/"


def model_metadata_capacity(api_url: str, model: str, metadata: object,
                            *, observed_at: float | None = None) -> dict[str, object]:
    """Read only documented fields from an exact provider's Models schema.

    These are server-declared limits, not a successful inference/capability test.
    Other providers' similarly named fields are deliberately not guessed.
    """
    profile = profile_for_url(api_url)
    if not profile or profile["id"] != "deepseek_official" or not isinstance(metadata, dict):
        return {}
    values = {}
    for field, key in (("context_window_tokens", "context_window"), ("max_output_tokens", "max_output_tokens")):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2_000_000:
            values[field] = value
    if not values:
        return {}
    return {"api_url": normalize_catalog_endpoint(api_url), "model": model,
            "observed_at": time.time() if observed_at is None else observed_at, "limits": values,
            "schema_source": DEEPSEEK_MODELS_SCHEMA, "evidence": "server_declared_metadata"}


def capacity_profile_with_metadata(profile: dict[str, object], api_url: str, model: str,
                                   metadata: object) -> dict[str, object]:
    """Overlay fresh, endpoint/model-bound server declarations onto auto caps."""
    if not isinstance(metadata, dict) or metadata.get("evidence") != "server_declared_metadata":
        return profile
    if (metadata.get("api_url") != normalize_catalog_endpoint(api_url)
            or metadata.get("model") != model or metadata.get("schema_source") != DEEPSEEK_MODELS_SCHEMA):
        return profile
    timestamp = metadata.get("observed_at")
    if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(timestamp) or not 0 <= time.time() - timestamp < MODEL_METADATA_TTL_SECONDS):
        return profile
    raw = metadata.get("limits")
    if not isinstance(raw, dict):
        return profile
    checked = model_metadata_capacity(api_url, model, {
        "context_window": raw.get("context_window_tokens"),
        "max_output_tokens": raw.get("max_output_tokens"),
    })
    if not checked:
        return profile
    result = dict(profile)
    limits = checked["limits"]
    assert isinstance(limits, dict)
    result.update(limits)
    result["source"] = (models_endpoint_for_url(api_url, normalize_catalog_endpoint(api_url)), DEEPSEEK_MODELS_SCHEMA)
    result["date"] = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    result["evidence"] = "server_declared_metadata"
    result["unknown_reason"] = ""
    result["match"] = {"endpoint": normalize_catalog_endpoint(api_url), "model": model,
                       "matched": True, "rule": "exact endpoint/model and documented Models metadata schema"}
    result["notes"] = "服务端按官方 Models schema 声明的容量；不是推理、工具或账号权限验证。7 天后回到离线文档预置。"
    return result


class ModelConfiguration(TypedDict):
    model: str
    protocol: str
    context_window_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    capabilities: dict[str, str]
    notes: str
    sources: list[str]
    verified_date: str
    availability_verified: bool
    capabilities_verified: bool


def _source_urls(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, (list, tuple)) else []


def model_configuration_for(api_url: str, model: str) -> ModelConfiguration:
    """Documented defaults independent of account discovery; zero caps are unknown."""
    profile = profile_for_url(api_url) or {}
    capacity = context_capacity_profile_for(api_url, model)
    all_details = profile.get("model_details", {})
    raw_details = all_details.get(model, {}) if isinstance(all_details, dict) else {}
    details = raw_details if isinstance(raw_details, dict) else {}
    capabilities = {field: "unknown" for field in ("tool_use", "image_input", "audio_input", "video_input")}
    raw_capabilities = details.get("capabilities", {})
    if isinstance(raw_capabilities, dict):
        capabilities.update({key: value for key, value in raw_capabilities.items()
                             if isinstance(key, str) and isinstance(value, str)})
    match = capacity["match"]
    matched = isinstance(match, dict) and match.get("matched") is True
    return {
        "model": model,
        "protocol": str(profile.get("protocol", "openai_chat_completions")),
        "context_window_tokens": _catalog_token_limit(capacity["context_window_tokens"]),
        "max_input_tokens": _catalog_token_limit(capacity["max_input_tokens"]),
        "max_output_tokens": _catalog_token_limit(capacity["max_output_tokens"]),
        "capabilities": capabilities,
        "notes": str(details.get("notes", capacity.get("notes", ""))),
        "sources": list(dict.fromkeys(_source_urls(capacity["source"]) + _source_urls(details.get("sources"))
                                     + _source_urls(profile.get("sources")))),
        "verified_date": str(details.get("verified_at", capacity["date"] if matched else profile.get("verified_date", ""))),
        "availability_verified": False,
        "capabilities_verified": False,
    }


def models_endpoint_for_url(api_url: str, normalized_base: str) -> str:
    profile = profile_for_url(api_url)
    discovery = profile.get("models_discovery", {}) if profile is not None else {}
    explicit = str(discovery.get("url", "")).strip()
    if explicit:
        return explicit
    return normalized_base.rstrip("/") + "/models"


def probe_models(api_url: str, current_model: str, limit: int) -> list[str]:
    """Return the manual model plus all eligible exact-profile IDs, then cap requests."""
    profile = profile_for_url(api_url)
    if profile is not None and not profile.get("probe_allowed"):
        return []
    names = [str(current_model).strip()]
    if profile is not None:
        names.extend(str(item) for item in profile.get("probe_model_ids", ()))
    unique = list(dict.fromkeys(name for name in names if name))
    return unique[:max(0, int(limit))]
