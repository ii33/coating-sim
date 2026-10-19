"""Pure provider and native-effort capability rules; no network calls."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
from collections.abc import Mapping

from coat_sim.ui.provider_catalog import context_capacity_profile_for, capacity_profile_with_metadata

TASK_MODES = ("chat", "operate", "research")
NATIVE_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
REASONING_STRENGTHS = ("auto", *NATIVE_EFFORTS, "thinking_disabled", "thinking_enabled")
_NATIVE_EFFORTS = frozenset(NATIVE_EFFORTS)
_ARK_MODELS = frozenset({
    "doubao-seed-2.1-pro", "doubao-seed-2.1-lite", "doubao-seed-evolving",
    "doubao-seed-2-1-pro-260915", "doubao-seed-2-1-pro-260628",
    "doubao-seed-2-1-lite-260915", "doubao-seed-2-1-turbo-260628",
})
_DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
_OPENAI_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_OPENAI_GPT6_MODEL_PROFILES = {
    "gpt-6-astra": {
        "reasoning_efforts": ("low", "medium", "high", "xhigh", "max"),
        "chat_function_tools": "responses_only",
        "audio_supported": False,
        "video_supported": False,
        "image_media_types": ("image/jpeg", "image/png", "image/webp", "image/gif"),
    },
    "gpt-6.1-sol": {
        "reasoning_efforts": ("low", "medium", "high", "xhigh", "max"),
        "chat_function_tools": "responses_only",
        "audio_supported": False,
        "video_supported": False,
        "image_media_types": ("image/jpeg", "image/png", "image/webp", "image/gif"),
    },
    "gpt-6-sol": {
        "reasoning_efforts": ("none", "low", "medium", "high", "xhigh", "max"),
        "chat_function_tools": "none_only",
        "audio_supported": False,
        "video_supported": False,
        "image_media_types": ("image/jpeg", "image/png", "image/webp", "image/gif"),
    },
    "gpt-6-luna": {
        "reasoning_efforts": ("none", "low", "medium", "high", "xhigh", "max"),
        "chat_function_tools": "none_only",
        "audio_supported": False,
        "video_supported": False,
        "image_media_types": ("image/jpeg", "image/png", "image/webp", "image/gif"),
    },
}
_EFFORT_RANK = {value: index for index, value in enumerate(NATIVE_EFFORTS)}
_EFFORT_RANK.update(thinking_disabled=0, thinking_enabled=3)


def task_options(config: dict) -> tuple[str, str]:
    mode = str(config.get("task_mode", config.get("assistant_task_mode", "chat")))
    strength = str(config.get("reasoning_strength", config.get("assistant_reasoning_strength", "auto")))
    if mode not in TASK_MODES or strength not in REASONING_STRENGTHS:
        raise ValueError("任务模式或推理强度无效。")
    return mode, strength


def _normalized_url(value: str) -> str:
    parts = urlsplit(value.strip().rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path or "/v1", "", ""))


def provider_for_endpoint(api_url: str) -> str:
    """Return the provider family implied by an endpoint's host.

    Custom OpenAI-compatible endpoints intentionally share the ``compatible``
    family; their normalized endpoint path still gives each gateway its own
    identity.
    """
    try:
        host = (urlsplit(api_url.strip()).hostname or "").lower()
    except (AttributeError, ValueError):
        host = ""
    if host == "api.openai.com":
        return "openai"
    if host == "api.deepseek.com":
        return "deepseek"
    if host == "ark.cn-beijing.volces.com":
        return "ark"
    return "compatible"


MAX_MODEL_TOKENS = 2_000_000
DEFAULT_COMPACTION_THRESHOLD_PERCENT = 70
MILLION_CONTEXT_COMPACTION_THRESHOLD_PERCENT = 80
MILLION_CONTEXT_TOKEN_CAPACITIES = frozenset((1_000_000, 1_048_576, 1_050_000))
CONTEXT_BUDGET_VERSION = 2
CONTEXT_BUDGET_FIELDS = (
    "context_window_tokens", "reserved_output_tokens", "compression_reserve_tokens",
    "history_keep_tokens", "compaction_threshold_percent",
)
_LEGACY_CONTEXT_FIELDS = (
    "max_input_tokens", "max_output_tokens", "context_tokens", "compaction_threshold_percent",
)


def _default_compaction_threshold_percent(context_window_tokens: int) -> int:
    """Choose the auto trigger for explicit decimal/binary one-million capacities."""
    return (MILLION_CONTEXT_COMPACTION_THRESHOLD_PERCENT
            if context_window_tokens in MILLION_CONTEXT_TOKEN_CAPACITIES
            else DEFAULT_COMPACTION_THRESHOLD_PERCENT)


def _safe_endpoint(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip().rstrip("/"))
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            return ""
        return _normalized_url(value)
    except ValueError:
        return ""


def official_openai_model_profile(api_url: str, model: str) -> dict[str, object] | None:
    """Return static protocol facts for an exact official endpoint/model pair."""
    if not isinstance(api_url, str) or not isinstance(model, str):
        return None
    try:
        raw_endpoint = api_url.strip()
        endpoint = urlsplit(raw_endpoint)
        path = endpoint.path.rstrip("/")
        if path.endswith("/chat/completions"):
            path = path[:-len("/chat/completions")]
        if not path:
            path = "/v1"
        if (endpoint.scheme.lower() != "https"
                or (endpoint.hostname or "").lower() != "api.openai.com"
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment
                or "?" in raw_endpoint or "#" in raw_endpoint
                or path != "/v1"
                or endpoint.port not in (None, 443)):
            return None
    except ValueError:
        return None
    profile = _OPENAI_GPT6_MODEL_PROFILES.get(model)
    return dict(profile) if profile is not None else None


def _token_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _auto_record(*, legacy_values: Mapping | None = None, max_input_tokens: object = 0) -> dict[str, object]:
    result: dict[str, object] = {
        "context_budget_version": CONTEXT_BUDGET_VERSION,
        **{field: 0 for field in CONTEXT_BUDGET_FIELDS},
        "max_input_tokens": max_input_tokens,
        "legacy_values": dict(legacy_values or {}),
    }
    return result


def migrate_model_context_overrides(values: object, *, endpoint: str = "",
                                   model: str = "") -> dict[str, dict[str, dict[str, object]]]:
    """Migrate the full settings object to endpoint/model-scoped v2 overrides.

    Raw legacy values are retained per record. The returned map is idempotent:
    once a row carries ``context_budget_version == 2``, reset-to-auto zeros are
    never repopulated from its archived v1 fields.
    """
    if not isinstance(values, Mapping):
        return {}
    selected_endpoint = _safe_endpoint(endpoint) or _safe_endpoint(values.get("assistant_api_url"))
    selected_model = model.strip() if isinstance(model, str) and model.strip() else (
        values.get("assistant_model", "").strip()
        if isinstance(values.get("assistant_model"), str) else "")
    raw_rows = values.get("assistant_model_limits", {})
    if not isinstance(raw_rows, Mapping):
        raw_rows = {}
    selected_raw_is_v2 = False
    if selected_endpoint and selected_model:
        for raw_address, raw_models in raw_rows.items():
            if not isinstance(raw_address, str) or not isinstance(raw_models, Mapping):
                continue
            if (_safe_endpoint(raw_address) or raw_address) != selected_endpoint:
                continue
            for raw_name, raw_record in raw_models.items():
                if isinstance(raw_name, str) and raw_name.strip() == selected_model:
                    selected_raw_is_v2 = selected_raw_is_v2 or (
                        isinstance(raw_record, Mapping)
                        and raw_record.get("context_budget_version") == CONTEXT_BUDGET_VERSION
                    )
    result: dict[str, dict[str, dict[str, object]]] = {}
    for address, models in raw_rows.items():
        if not isinstance(address, str) or not isinstance(models, Mapping):
            continue
        endpoint = _safe_endpoint(address) or address
        rows = result.setdefault(endpoint, {})
        for name, raw_record in models.items():
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
                continue
            model_key = name.strip()
            if not isinstance(raw_record, Mapping):
                migrated_invalid = _auto_record(legacy_values={"invalid_record": raw_record})
                migrated_invalid["migration_error"] = "旧模型限制记录不是对象；原始数据已保留。"
                if model_key != name:
                    migrated_invalid["legacy_record_name"] = name
                rows.setdefault(model_key, migrated_invalid)
                continue
            record = dict(raw_record)
            if record.get("context_budget_version") == CONTEXT_BUDGET_VERSION:
                # Do not sanitize out-of-range values to zero: budget validation
                # must report them while preserving the user's repairable data.
                migrated = _auto_record(
                    legacy_values=record.get("legacy_values") if isinstance(record.get("legacy_values"), Mapping) else {},
                    max_input_tokens=record.get("max_input_tokens", 0),
                )
                for field in CONTEXT_BUDGET_FIELDS:
                    migrated[field] = record.get(field, 0)
                for key, value in record.items():
                    if key not in (*CONTEXT_BUDGET_FIELDS, "max_input_tokens", "legacy_values", "provider_metadata"):
                        migrated[key] = value
                if model_key != name:
                    migrated["legacy_record_name"] = name
                if model_key in rows and rows[model_key] != migrated:
                    rows[model_key].setdefault("legacy_collisions", []).append(migrated)
                else:
                    rows[model_key] = migrated
                continue
            legacy = dict(record)
            migrated = _auto_record(legacy_values=legacy, max_input_tokens=record.get("max_input_tokens", 0))
            migrated["context_window_tokens"] = record.get("context_tokens", 0)
            migrated["reserved_output_tokens"] = record.get("max_output_tokens", 0)
            migrated["compression_reserve_tokens"] = record.get("compression_reserve_tokens", 0)
            migrated["history_keep_tokens"] = record.get("history_keep_tokens", 0)
            migrated["compaction_threshold_percent"] = record.get("compaction_threshold_percent", 0)
            migrated["migrated_from_version"] = 1
            if model_key != name:
                migrated["legacy_record_name"] = name
            if model_key in rows and rows[model_key] != migrated:
                rows[model_key].setdefault("legacy_collisions", []).append(migrated)
            else:
                rows[model_key] = migrated

    # The pre-v2 global context was only ever a fallback for the currently
    # selected model. Migrate it once, never fan it out to future model choices.
    migration_marker = values.get("assistant_context_budget_version", 0)
    migration_complete = _token_integer(migration_marker) and migration_marker >= CONTEXT_BUDGET_VERSION
    if not migration_complete:
        legacy_context = values.get("assistant_context_tokens", 0)
        if selected_endpoint and selected_model and legacy_context not in (0, None, ""):
            existing = result.get(selected_endpoint, {}).get(selected_model)
            if existing is None:
                record = _auto_record(legacy_values={"assistant_context_tokens": legacy_context})
                record["context_window_tokens"] = legacy_context
                record["migrated_from_version"] = 1
                result.setdefault(selected_endpoint, {})[selected_model] = record
            elif not isinstance(existing, Mapping):
                pass
            else:
                raw_existing = existing.get("legacy_values", {})
                if isinstance(raw_existing, dict):
                    raw_existing.setdefault("assistant_context_tokens", legacy_context)
                raw_context = existing.get("context_window_tokens", 0)
                if (not selected_raw_is_v2 and (raw_context is None or raw_context == ""
                        or (_token_integer(raw_context) and raw_context == 0))):
                    existing["context_window_tokens"] = legacy_context
    return result


def normalized_model_limits(values: object) -> dict[str, dict[str, dict[str, object]]]:
    """Compatibility wrapper for endpoint/model maps; full migration takes config."""
    return migrate_model_context_overrides({
        "assistant_model_limits": values,
        "assistant_context_budget_version": CONTEXT_BUDGET_VERSION,
    })


def _positive_limit(value: object) -> int:
    return value if _token_integer(value) and 0 < value <= MAX_MODEL_TOKENS else 0


def _field_value(record: Mapping, field: str) -> object:
    return record.get(field, 0)


def context_budget_for_model(config: Mapping, api_url: str, model: str,
                             *, fallback_input_tokens: int = 64000) -> dict[str, object]:
    """Resolve one shared settings/runtime context budget without network access."""
    fallback = fallback_input_tokens if _positive_limit(fallback_input_tokens) else 64000
    profile = context_capacity_profile_for(api_url, model)
    endpoint = _safe_endpoint(api_url)
    model_id = model.strip() if isinstance(model, str) else ""
    migrated = migrate_model_context_overrides(config)
    record = migrated.get(endpoint, {}).get(model_id, {})
    if not isinstance(record, Mapping):
        record = {}
    probe = config.get("assistant_reasoning_probe")
    metadata = probe.get("capacity_metadata") if isinstance(probe, Mapping) else None
    profile = capacity_profile_with_metadata(profile, api_url, model_id, metadata)

    overrides = {field: _field_value(record, field) for field in CONTEXT_BUDGET_FIELDS}
    errors: list[str] = []
    if record.get("migration_error"):
        errors.append(str(record["migration_error"]))
    if record.get("legacy_collisions"):
        errors.append("规范化后的服务商地址包含冲突的旧模型记录；请先恢复或移除冲突记录。")

    def checked_override(field: str, maximum: int = MAX_MODEL_TOKENS) -> int:
        raw = overrides[field]
        if not _token_integer(raw) or raw < 0 or raw > maximum:
            label = {
                "context_window_tokens": "模型上下文窗口",
                "reserved_output_tokens": "预留输出",
                "compression_reserve_tokens": "压缩预留",
                "history_keep_tokens": "压缩保留历史",
            }.get(field, "模型预算")
            errors.append(f"{label}必须是 0（自动）或 1 到 {maximum} Token 之间的整数。")
            return 0
        return raw

    context_override = checked_override("context_window_tokens")
    output_override = checked_override("reserved_output_tokens")
    reserve_override = checked_override("compression_reserve_tokens")
    history_override = checked_override("history_keep_tokens")
    threshold_override = overrides["compaction_threshold_percent"]
    if not _token_integer(threshold_override) or not 0 <= threshold_override <= 100:
        errors.append("自动压缩阈值必须是 0%（自动）或 1% 到 100% 之间的整数百分比。")
        threshold_override = 0

    provider_context = _positive_limit(profile.get("context_window_tokens"))
    provider_input = _positive_limit(profile.get("max_input_tokens"))
    hard_output = _positive_limit(profile.get("max_output_tokens"))
    legacy_input_raw = record.get("max_input_tokens", 0)
    if not _token_integer(legacy_input_raw) or not 0 <= legacy_input_raw <= MAX_MODEL_TOKENS:
        errors.append("旧版独立输入硬限必须是 0 到 2,000,000 之间的整数 Token。")
        legacy_input = 0
    else:
        legacy_input = legacy_input_raw
    context = context_override or provider_context
    if provider_context and context_override and context_override > provider_context:
        errors.append(f"上下文窗口不能超过已核验硬限 {provider_context} Token。")
    input_candidates = [value for value in (provider_input, legacy_input) if value]
    hard_input = min(input_candidates) if input_candidates else 0
    # An input-only hard cap does not establish the model's context-window tier.
    default_threshold = _default_compaction_threshold_percent(context)

    if hard_output and output_override and output_override > hard_output:
        errors.append(f"预留输出不能超过已核验硬限 {hard_output} Token。")

    # The automatic values are explicit project heuristics, not measurements.
    source_boundaries = [value for value in (context, hard_input) if value]
    heuristic_capacity = min(source_boundaries) if source_boundaries else fallback
    automatic_output = min(
        hard_output or MAX_MODEL_TOKENS,
        32768,
        max(1, heuristic_capacity // 16),
    )
    output = output_override or automatic_output
    if context and output >= context:
        errors.append("预留输出必须小于上下文窗口。")

    hard_bases = []
    if context:
        hard_bases.append(max(0, context - output))
    if hard_input:
        hard_bases.append(hard_input)
    budget_known = bool(hard_bases)
    base_budget = min(hard_bases) if hard_bases else fallback
    automatic_reserve = min(8192, max(1024, base_budget // 50), max(0, base_budget // 4))
    reserve = reserve_override or automatic_reserve
    if budget_known and reserve >= base_budget:
        errors.append("压缩预留必须小于扣除输出预留后的适用输入容量。")
    budget = max(0, base_budget - reserve)
    percent = threshold_override or default_threshold
    trigger = budget * percent // 100
    target = min(budget * 45 // 100, max(0, trigger - 1))
    automatic_history = min(32768, budget // 5, max(0, target - 1))
    history = history_override or automatic_history
    if history_override and history >= budget:
        errors.append("压缩保留历史必须小于有效输入预算。")
    if history_override and history >= trigger:
        errors.append("压缩保留历史必须小于自动压缩触发值。")
    if history_override and history >= target:
        errors.append("压缩保留历史必须小于压缩后总目标。")
    summary_output = min(output, reserve, max(0, target - history), 8192)
    if target <= 0 or budget <= 0:
        errors.append("当前容量没有足够空间触发并完成安全压缩。")
    if summary_output <= 0:
        errors.append("当前压缩预留和历史目标无法为摘要输出保留正数 Token。")

    limits: dict[str, object] = {
        "context_window_tokens": context,
        "reserved_output_tokens": output,
        "compression_reserve_tokens": reserve,
        "history_keep_tokens": history,
        "compaction_threshold_percent": percent,
        "max_input_tokens": hard_input,
        "max_output_tokens": hard_output,
        "provider_max_input_tokens": provider_input,
        "legacy_max_input_tokens": legacy_input,
    }
    return {
        "limits": limits,
        "overrides": overrides,
        "defaults": {
            "context_window_tokens": provider_context,
            "reserved_output_tokens": automatic_output,
            "compression_reserve_tokens": automatic_reserve,
            "history_keep_tokens": automatic_history,
            "compaction_threshold_percent": default_threshold,
        },
        "budget_tokens": budget,
        "budget_known": budget_known,
        "fallback_input_tokens": fallback if not budget_known else 0,
        "trigger_tokens": trigger,
        "target_tokens": target,
        "history_keep_tokens": history,
        "summary_output_tokens": summary_output,
        "fallback_used": not budget_known,
        "validation_errors": errors,
        "profile": profile,
    }


def model_token_limits(config: Mapping, api_url: str, model: str) -> dict[str, object]:
    """Return the selected endpoint/model's resolved v2 limits and hard caps."""
    return dict(context_budget_for_model(config, api_url, model)["limits"])


def _validated_probe(api_url: str, model: str, probe: object) -> dict | None:
    """Only endpoint/model-bound results and one verified request field pass."""
    if not isinstance(probe, dict) or probe.get("evidence") == "server_capacity_only":
        return None
    try:
        if (_normalized_url(str(probe.get("api_url", ""))) != _normalized_url(api_url)
                or probe.get("model") != model.strip()):
            return None
    except (ValueError, TypeError):
        return None
    parameter = probe.get("output_parameter")
    parameter = parameter if parameter in ("max_tokens", "max_completion_tokens") else None
    if probe.get("verified") is not True:
        return {"strength_map": {}, "request_fields": {}, "native_options": [],
                "reasoning_probed": probe.get("reasoning_probed") is True,
                "output_parameter": parameter}
    if probe.get("evidence") != "invalid_control+validated_requests":
        return None
    mapping, request_fields = {}, {}
    raw_mapping = probe.get("strength_map")
    if not isinstance(raw_mapping, dict):
        return None
    for strength in REASONING_STRENGTHS[1:]:
        record = raw_mapping.get(strength)
        if not isinstance(record, dict):
            continue
        fields = record.get("request_fields")
        if (not isinstance(fields, dict) or len(fields) != 1
                or not set(fields) <= {"reasoning_effort", "thinking"}):
            continue
        if "reasoning_effort" in fields:
            value = fields["reasoning_effort"]
            if strength not in _NATIVE_EFFORTS or value != strength:
                continue
            clean = {"reasoning_effort": value}
        else:
            thinking = fields["thinking"]
            if (not isinstance(thinking, dict) or set(thinking) != {"type"}
                    or thinking["type"] not in ("enabled", "disabled")
                    or strength != "thinking_" + thinking["type"]):
                continue
            value = thinking["type"]
            clean = {"thinking": {"type": value}}
        if record.get("actual_effort") != value:
            continue
        mapping[strength] = value
        request_fields[strength] = clean
    return {"strength_map": mapping, "request_fields": request_fields,
            "native_options": [value for value in NATIVE_EFFORTS if value in mapping],
            "output_parameter": parameter}


def reasoning_capabilities(api_url: str, model: str, probe: object = None) -> dict:
    """Live/cached probes preserve every confirmed native value without aliasing."""
    provider = provider_for_endpoint(api_url)
    original_model = model.strip()
    model = original_model.lower()
    mapping = {}
    official_profile = official_openai_model_profile(api_url, original_model)
    if provider == "openai":
        if official_profile is not None:
            mapping = {value: value for value in official_profile["reasoning_efforts"]}
        elif (model.startswith(_OPENAI_PREFIXES) and "-chat" not in model
                and not model.startswith("o1-mini") and "-pro" not in model
                and "deep-research" not in model):
            mapping = {value: value for value in ("low", "medium", "high")}
    elif provider == "deepseek":
        if model in _DEEPSEEK_MODELS:
            mapping = {value: value for value in ("low", "high", "max")}
    elif provider == "ark":
        if model in _ARK_MODELS:
            mapping = {value: value for value in ("low", "medium", "high")}
    # Exact official model facts cannot be broadened or narrowed by a stale
    # cached probe. Probes remain available for every other endpoint/model.
    confirmed = None if official_profile is not None else _validated_probe(api_url, original_model, probe)
    fields = {}
    parameter = "max_completion_tokens" if provider == "openai" else "max_tokens"
    if confirmed is not None:
        mapping = confirmed["strength_map"]
        fields = confirmed["request_fields"]
        parameter = confirmed.get("output_parameter") or parameter
        detail = ("显示全部通过请求验证的原生档位；按所选值发送。参数接受不证明内部推理效果。"
                  if mapping else "本次思考探测尚未确认参数支持，保留自动。"
                  if confirmed.get("reasoning_probed", True) else
                  "已确认模型与输出参数；等待自动探测固定思考档位。")
    else:
        detail = ("这些档位来自官方文档；自动探测会补齐当前接口实际接受的原生值。"
                  if mapping else "固定思考档位尚未确认；将根据当前服务自动探测。")
    return {"reasoning_supported": bool(mapping), "provider": provider,
            "supported_strengths": ["auto", *mapping], "strength_map": mapping,
            "request_fields": fields, "probe_verified": confirmed is not None and bool(mapping),
            "detail": detail, "output_parameter": parameter,
            "official_model_profile": official_profile}


def _budget_bucket(effort: str) -> str:
    if effort in ("none", "minimal", "low", "disabled"):
        return "low"
    if effort in ("medium", "enabled"):
        return "medium"
    return "high"


def request_policy(capabilities: dict, mode: str, strength: str, role: str,
                   limits: Mapping | None = None) -> dict:
    """Native effort and project output-budget buckets are separate quantities."""
    mapping = capabilities.get("strength_map", {})
    supported = bool(capabilities.get("reasoning_supported") and mapping)
    if strength != "auto" and (not supported or strength not in mapping):
        raise ValueError("当前服务与模型未确认支持所选原生档位，请选择“自动”。")
    selected = strength
    if selected == "auto":
        target = ("low" if role == "summary" or (role == "main" and mode != "research") or mode == "chat"
                  else "high" if role == "analyst" or mode == "research" else "medium")
        selected = min(mapping, key=lambda key: abs(_EFFORT_RANK[key] - _EFFORT_RANK[target])) if supported else target
    settings = {"parallel_tool_calls": False}
    if supported:
        effective = mapping[selected]
        bucket = _budget_bucket(effective)
        desired = {"low": 8192, "medium": 16384, "high": 32768}[bucket]
        minimum = {"low": 2048, "medium": 4096, "high": 16384}[bucket]
        fields = capabilities.get("request_fields", {}).get(selected)
        if fields:
            if "reasoning_effort" in fields:
                settings["openai_reasoning_effort"] = fields["reasoning_effort"]
            else:
                settings["extra_body"] = {"thinking": dict(fields["thinking"])}
        else:
            settings["openai_reasoning_effort"] = effective
            if capabilities["provider"] in ("ark", "deepseek"):
                settings["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        effective = "provider_default"
        desired = 16384 if mode == "research" or capabilities["provider"] == "ark" else 8192
        minimum = 4096 if mode == "research" else 1024
    bounds = limits or {}
    # In v2, max_output_tokens is the provider's hard output cap while the
    # user's selected/resolved generation allowance is reserved_output_tokens.
    # Keep the old shape for callers not yet migrated at an integration edge.
    has_reserved_output = "reserved_output_tokens" in bounds
    output_limit = bounds.get("reserved_output_tokens") if has_reserved_output else bounds.get("max_output_tokens", 0)
    input_limit = bounds.get("max_input_tokens", 0)
    context_limit = bounds.get("context_window_tokens", bounds.get("context_tokens", 0))
    if isinstance(output_limit, int) and not isinstance(output_limit, bool) and 0 < output_limit <= MAX_MODEL_TOKENS:
        desired = min(desired, output_limit) if role == "summary" else output_limit
        minimum = min(minimum, desired)
    hard_output_limit = bounds.get("max_output_tokens", 0) if has_reserved_output else 0
    if (isinstance(hard_output_limit, int) and not isinstance(hard_output_limit, bool)
            and hard_output_limit > 0 and desired > hard_output_limit):
        raise ValueError(f"预留输出 {desired} Token 超过模型已核验硬上限 {hard_output_limit} Token。")
    # Input/context enforcement needs the actual assembled request at the runtime boundary.
    return {"effective_reasoning": effective, "selected_strength": selected,
            "desired_output": desired, "minimum_output": minimum, "model_settings": settings,
            "maximum_input_tokens": input_limit if isinstance(input_limit, int) and not isinstance(input_limit, bool) and 0 < input_limit <= MAX_MODEL_TOKENS else 0,
            "context_tokens": context_limit if isinstance(context_limit, int) and not isinstance(context_limit, bool) and 0 < context_limit <= MAX_MODEL_TOKENS else 0,
            "max_output_tokens": hard_output_limit if isinstance(hard_output_limit, int) and not isinstance(hard_output_limit, bool) else 0}
