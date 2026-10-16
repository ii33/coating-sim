"""Offline provider catalog backed by ``provider_catalog_sources.json``.

Catalog IDs are documented suggestions, not proof of account entitlement or
successful inference. The separate discovery cache contains only runtime
results. Provider endpoints are matched by scheme, host, and complete path.
"""
from __future__ import annotations

from copy import deepcopy
from html import unescape
import json
from pathlib import Path
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
    raw = raw if matched else {}
    return {
        "context_window_tokens": _catalog_token_limit(raw.get("context_window_tokens")),
        "max_input_tokens": _catalog_token_limit(raw.get("max_input_tokens")),
        "max_output_tokens": _catalog_token_limit(raw.get("max_output_tokens")),
        "source": tuple(str(item) for item in raw.get("sources", ()) if isinstance(item, str)),
        "date": str(catalog.get("verified_at", "")),
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
        "verified_date": CATALOG_VERIFIED_DATE,
        "selectable": selectable,
        "probe_allowed": selectable,
        # Preserve the settings page's existing automatic read / bounded
        # candidate fallback after a URL or saved-key change. Selecting a
        # preset itself does not call setAssistantConfig or initiate a probe.
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
