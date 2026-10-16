"""Bounded, explicit compatibility probes; this module performs no network I/O.

The tables are candidate names, not claims of account access or capability.
Only a user-triggered successful request enters the current discovery list.
Sources: api-docs.deepseek.com/api/list-models/;
docs.volcengine.com/docs/ark/agent-plan-enterprise-opencode;
developers.openai.com/api/docs/guides/reasoning.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from coat_sim.ui.provider_catalog import probe_models

PROBE_OUTPUT_TOKENS = 32
PROBE_REQUEST_TIMEOUT_MS = 15_000
PROBE_TOTAL_TIMEOUT_MS = 120_000
MODEL_REQUEST_LIMIT = 12
REASONING_REQUEST_LIMIT = 14
NATIVE_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
INVALID_PROBE_VALUE = "__coat_invalid_probe_value__"
STATUS_TEXT = {
    "ok": "可用", "unavailable_model": "模型不可用", "unsupported_parameter": "参数不支持",
    "auth": "鉴权或权限失败", "rate_limit": "限流/额度不足", "endpoint": "接口不支持",
    "unknown": "未确认", "timeout": "超时", "cancelled": "已取消",
}


def normalize_api_url(value: str) -> str:
    """Preserve provider paths, especially the Agent Plan billing prefix."""
    parts = urlsplit(value.strip().rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def candidate_models(api_url: str, current_model: str) -> list[str]:
    return probe_models(api_url, current_model, MODEL_REQUEST_LIMIT)


def output_parameter(api_url: str, model: str) -> str:
    host = (urlsplit(api_url).hostname or "").lower()
    return "max_completion_tokens" if host == "api.openai.com" or model.lower().startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")) else "max_tokens"


def completion_body(model: str, parameter: str, fields: dict | None = None) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": "Reply OK."}], "stream": False, parameter: PROBE_OUTPUT_TOKENS}
    if fields:
        body.update(fields)
    return body


def model_records(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    records = {}
    for item in payload["data"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            name = item["id"].strip()[:120]
            records.setdefault(name, {"id": name, "metadata": {key: value for key, value in item.items() if key != "id"}, "evidence": "models_endpoint"})
            if len(records) >= 500:
                break
    return [records[name] for name in sorted(records)]


def error_details(payload: object) -> tuple[str, str, str]:
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return "", "", str(error)[:1500]
    return str(error.get("code", ""))[:120], str(error.get("param", ""))[:120], str(error.get("message", ""))[:1500]


def classify_reply(status: int | None, payload: object, *, timeout: bool = False, network_error: bool = False) -> str:
    if timeout:
        return "timeout"
    code, param, message = error_details(payload)
    text = (code + " " + param + " " + message).lower()
    if status is not None and 200 <= status < 300 and not network_error:
        if isinstance(payload, dict) and isinstance(payload.get("choices"), list) and payload["choices"]:
            return "ok"
        return "unknown"
    if status == 429 or any(word in text for word in ("insufficient_quota", "quota_exceeded", "rate_limit")):
        return "rate_limit"
    if status in (401, 403):
        if any(word in text for word in ("modelnotopen", "model_not_found", "model_not_available")):
            return "unavailable_model"
        return "auth"
    if status == 404:
        return "unavailable_model" if "model" in text and any(word in text for word in ("not found", "not_found", "not exist", "invalidmodel", "not available")) else "endpoint"
    if status == 400 and "model" in text and any(word in text for word in ("not found", "not_found", "not exist", "invalidmodel", "not available")):
        return "unavailable_model"
    if status in (400, 422) and any(word in text for word in ("unsupported", "not supported", "unknown parameter", "unrecognized parameter", "unexpected keyword")):
        return "unsupported_parameter"
    return "unknown"


def validates_field(status: int | None, payload: object, field: str) -> bool:
    """Require a rejection of this field's value, never a generic body error."""
    if status not in (400, 422):
        return False
    code, param, message = error_details(payload)
    text = (code + " " + param + " " + message).lower()
    param = param.lower()
    exact_param = param in (field, "thinking") if field == "thinking.type" else param == field
    if param and not exact_param:
        return False
    if any(word in text for word in ("ignored", "silently ignore", "被忽略", "不会生效")):
        return False
    value_error = any(word in text for word in ("unsupported value", "not supported value", "invalid value", "supported values", "allowed values", "one of", "enum", "permitted", "expected", "must be", "取值", "必须"))
    field_error = any(word in text for word in ("unknown parameter", "unrecognized", "unexpected keyword", "unsupported parameter", "parameter is not supported", "parameter not supported"))
    if field_error:
        return False
    if ("unsupported" in text or "not supported" in text) and not value_error:
        return False
    if exact_param:
        return value_error or any(word in text for word in ("invalid", "不合法", "无效"))
    # Without an explicit error.param, require field and value rejection in
    # the same short clause. Never treat an echoed request body as evidence.
    field_pattern = (r"thinking(?:[.\[\]/\s'\"])+type" if field == "thinking.type"
                     else re.escape(field))
    field_pattern = r"(?<!\w)" + field_pattern + r"(?!\w)"
    after = (r"(?:\b(?:invalid(?:\s+value)?|not\s+valid|must\s+be|expected|allowed(?:\s+values)?|"
             r"supported\s+values|one\s+of|enum|permitted)\b|取值|必须|不合法|无效)")
    before = r"\b(?:invalid(?:\s+(?:value|parameter))?|unsupported\s+value)\b"
    for clause in re.split(r"[;\n]|\.(?=\s)", message.lower()):
        if len(clause) > 500 or any(name in clause for name in (
                "max_tokens", "max_completion_tokens", "temperature", "messages", "tools")):
            continue
        if (re.search(field_pattern + r"[^;\n]{0,100}" + after, clause)
                or re.search(before + r"[\s:'\"`]{0,20}" + field_pattern, clause)
                or re.search(r"unsupported\s+value[^;\n]{0,100}\b(?:for|of|in)\s+(?:(?:the\s+)?parameter\s+)?[\"'`]*" + field_pattern, clause)):
            return True
    return False


def output_parameter_rejected(payload: object, parameter: str) -> bool:
    code, param, message = error_details(payload)
    text = (code + " " + param + " " + message).lower()
    return parameter in text and any(word in text for word in ("unsupported", "not supported", "unknown", "unrecognized", "use", "instead"))


def metadata_efforts(records: list[dict], model: str) -> list[str]:
    for row in records:
        if row.get("id") != model:
            continue
        metadata = row.get("metadata", {})
        effort = metadata.get("effort", {}) if isinstance(metadata, dict) else {}
        levels = effort.get("supported_levels", []) if isinstance(effort, dict) else []
        valid = [level for level in NATIVE_EFFORTS if level in levels]
        if valid:
            return valid
    return list(NATIVE_EFFORTS)


def reported_usage(payload: object) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict) or not any(key in usage for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")):
        return {"input_tokens": 0, "output_tokens": 0, "unreported_requests": 1}
    def number(*keys):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0
    return {"input_tokens": number("prompt_tokens", "input_tokens"), "output_tokens": number("completion_tokens", "output_tokens"), "unreported_requests": 0}


def reasoning_result(api_url: str, model: str, efforts: list[str], thinking: list[str], usage: dict, records: list[dict], parameter: str) -> dict:
    native = [value for value in NATIVE_EFFORTS if value in efforts]
    mapping = {value: {"actual_effort": value, "request_fields": {"reasoning_effort": value}} for value in native}
    if not native:
        for value in ("disabled", "enabled"):
            if value in thinking:
                mapping["thinking_" + value] = {"actual_effort": value, "request_fields": {"thinking": {"type": value}}}
    return {"api_url": normalize_api_url(api_url), "model": model, "verified": bool(mapping), "reasoning_probed": True,
            "options": ["auto", *mapping], "strength_map": mapping, "native_options": native,
            "thinking_options": thinking if not native else [], "output_parameter": parameter,
            "evidence": "invalid_control+validated_requests" if mapping else "unconfirmed",
            "usage": dict(usage), "results": list(records), "native_schema": 1}
