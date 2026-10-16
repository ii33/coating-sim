"""Offline checks for settings model discovery and reasoning preferences."""

import ast
import inspect
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
import textwrap

import pytest

import coat_sim.ui.settings as settings
from coat_sim.agent.modelclient import (
    DEFAULT_COMPACTION_THRESHOLD_PERCENT,
    context_budget_for_model,
    model_token_limits,
    normalized_model_limits,
    provider_for_endpoint,
    reasoning_capabilities,
    request_policy,
    task_options,
)
from coat_sim.agent.runtime import UNKNOWN_INPUT_BUDGET, context_compaction_limits
from coat_sim.agent.tone_policy import ASSISTANT_TONES, normalized_tone, tone_instructions
from coat_sim.ui.model_probe import MODEL_REQUEST_LIMIT, candidate_models, completion_body, model_records, reasoning_result
from coat_sim.ui.provider_catalog import (
    MODEL_DISCOVERY_STATUSES,
    main_preset_count,
    model_suggestions,
    profile_for_url,
    provider_presets,
)
from tests.test_ai_settings_probe_contract import (
    API_URL, FINGERPRINT, FAKE_SECRET, _Future, _callback_bridge, _credential_result,
)


def test_settings_bootstrap_retries_cache_snapshot_when_file_signature_changes(monkeypatch):
    endpoint = "https://mock-provider.example/v1"
    old_signature = (1, 10, 100, 1000, 20)
    new_signature = (1, 11, 101, 1001, 21)
    signatures = iter((old_signature, new_signature, new_signature, new_signature))
    snapshots = iter(({"old": {"models": []}}, {"new": {"models": []}}))
    reads = []
    monkeypatch.setattr(settings, "read_api_key", lambda *_args, **_kwargs: "fake-key")
    monkeypatch.setattr(settings.ModelDiscoveryBus, "disk_signature",
                        lambda: next(signatures))
    monkeypatch.setattr(settings.ModelDiscoveryBus, "_read_disk",
                        lambda: (reads.append(True), next(snapshots))[1])

    result = settings._read_settings_bootstrap_snapshot(endpoint)

    assert result[3] == {"new": {"models": []}}
    assert result[4] == new_signature
    assert len(reads) == 2


def test_settings_bootstrap_discards_cache_after_bounded_unstable_reads(monkeypatch):
    endpoint = "https://mock-provider.example/v1"
    signatures = iter((index, index + 1, index + 2, index + 3, index + 4, index + 5)
                      for index in range(settings.MODEL_CACHE_SNAPSHOT_ATTEMPTS * 2))
    flattened = iter(value for pair in signatures for value in pair)
    reads = []
    monkeypatch.setattr(settings, "read_api_key", lambda *_args, **_kwargs: "fake-key")
    monkeypatch.setattr(settings.ModelDiscoveryBus, "disk_signature", lambda: next(flattened))
    monkeypatch.setattr(settings.ModelDiscoveryBus, "_read_disk",
                        lambda: (reads.append(True), {"unstable": {"models": []}})[1])

    result = settings._read_settings_bootstrap_snapshot(endpoint)

    assert result[3] == {}
    assert result[4] is None
    assert len(reads) == settings.MODEL_CACHE_SNAPSHOT_ATTEMPTS


def test_settings_bootstrap_serializes_keyring_reads_across_workers(tmp_path, monkeypatch):
    endpoint = "https://mock-provider.example/v1"
    monkeypatch.setattr(settings, "MODEL_CACHE_PATH", tmp_path / "model-cache.json")

    class FakeKeyring:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.guard = Lock()

        def read_api_key(self, _api_url, *, strict=False):
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.04)
            with self.guard:
                self.active -= 1
            return "fake-parallel-key"

    keyring = FakeKeyring()
    monkeypatch.setattr(settings, "read_api_key", keyring.read_api_key)
    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshots = list(executor.map(
            settings._read_settings_bootstrap_snapshot, (endpoint, endpoint)))

    assert all(snapshot[0] for snapshot in snapshots)
    assert keyring.max_active == 1


def test_model_cache_key_contains_provider_and_normalized_endpoint():
    openai = "https://api.openai.com/v1/"
    compatible_a = "https://gateway.example/team-a/v1"
    compatible_b = "https://gateway.example/team-b/v1"

    assert provider_for_endpoint(openai) == "openai"
    assert settings.ModelDiscoveryBus.cache_key(openai) == "openai::https://api.openai.com/v1"
    assert settings.ModelDiscoveryBus.cache_key(compatible_a) != settings.ModelDiscoveryBus.cache_key(compatible_b)
    assert settings.ModelDiscoveryBus.cache_key(openai) != settings.ModelDiscoveryBus.cache_key(compatible_a)


def test_v2_endpoint_cache_is_loaded_into_provider_scoped_v3_shape(tmp_path, monkeypatch):
    endpoint = "https://api.openai.com/v1"
    old_entry = {
        "credential_fingerprint": "a" * 64,
        "models": [{"id": "gpt-5", "metadata": {}}],
        "reasoning": {},
        "models_updated_at": 100.0,
        "models_retry_after": 0,
    }
    cache_path = tmp_path / "assistant_model_cache.json"
    cache_path.write_text(json.dumps({"version": 2, "endpoints": {endpoint: old_entry}}), encoding="utf-8")
    monkeypatch.setattr(settings, "MODEL_CACHE_PATH", cache_path)

    loaded = settings.ModelDiscoveryBus._read_disk()

    key = settings.ModelDiscoveryBus.cache_key(endpoint)
    assert list(loaded) == [key]
    assert loaded[key]["provider"] == "openai"
    assert loaded[key]["api_url"] == endpoint
    assert loaded[key]["models"][0]["id"] == "gpt-5"


def test_v3_cache_rejects_provider_endpoint_mismatch(tmp_path, monkeypatch):
    endpoint = "https://api.deepseek.com/v1"
    key = settings.ModelDiscoveryBus.cache_key(endpoint)
    entry = {
        "provider": "deepseek",
        "api_url": endpoint,
        "credential_fingerprint": "b" * 64,
        "models": [{"id": "deepseek-flash", "metadata": {}}],
        "reasoning": {},
        "models_updated_at": 100.0,
        "models_retry_after": 0,
    }
    cache_path = tmp_path / "assistant_model_cache.json"
    cache_path.write_text(json.dumps({"version": settings.MODEL_CACHE_VERSION,
                                      "endpoints": {key: entry}}), encoding="utf-8")
    monkeypatch.setattr(settings, "MODEL_CACHE_PATH", cache_path)

    assert settings.ModelDiscoveryBus._read_disk()[key]["provider"] == "deepseek"
    entry["provider"] = "openai"
    cache_path.write_text(json.dumps({"version": settings.MODEL_CACHE_VERSION,
                                      "endpoints": {key: entry}}), encoding="utf-8")
    assert settings.ModelDiscoveryBus._read_disk() == {}


def test_ark_fallback_candidates_put_hand_entered_model_first_and_stay_bounded():
    candidates = candidate_models("https://ark.cn-beijing.volces.com/api/plan/v3", "manual-model")

    assert candidates[0] == "manual-model"
    assert len(candidates) == MODEL_REQUEST_LIMIT == 12
    assert len(candidates) == len(set(candidates))


def test_provider_catalog_scopes_models_and_discovery_to_exact_endpoint_profile():
    ark_standard = "https://ark.cn-beijing.volces.com/api/v3"
    ark_plan = "https://ark.cn-beijing.volces.com/api/plan/v3"
    ark_coding = "https://ark.cn-beijing.volces.com/api/coding/v3"

    assert profile_for_url(ark_standard)["id"] == "volcengine_ark_cn_beijing"
    assert profile_for_url(ark_plan)["id"] == "volcengine_ark_agent_plan"
    assert profile_for_url(ark_coding)["id"] == "volcengine_ark_coding_plan"
    assert profile_for_url(ark_plan + "/chat/completions")["id"] == "volcengine_ark_agent_plan"
    agent_plan = profile_for_url(ark_plan)
    assert agent_plan["models_discovery"]["status"] == "alternate_api"
    assert agent_plan["selectable"] is True
    assert agent_plan["probe_allowed"] is True
    assert agent_plan["default_auto_probe"] is True
    assert profile_for_url("https://api.deepseek.com")["models_discovery"]["url"] == "https://api.deepseek.com/models"
    assert len(model_suggestions(ark_plan)) == 15  # primary + additional + preview, not truncated to probe cap
    assert len(candidate_models(ark_plan, "manual-model")) == MODEL_REQUEST_LIMIT == 12
    assert "deepseek-chat" not in candidate_models("https://api.deepseek.com", "")
    assert candidate_models("https://gateway.example/team/v1", "manual-only") == ["manual-only"]
    assert candidate_models(ark_coding, "manual-model") == []
    assert model_suggestions(ark_coding) == []

    coding = next(row for row in provider_presets() if row["id"] == "volcengine_ark_coding_plan")
    assert coding["selectable"] is False
    assert coding["probe_allowed"] is False
    assert MODEL_DISCOVERY_STATUSES == ("confirmed", "unconfirmed", "unsupported", "alternate_api")
    assert main_preset_count() == 22
    assert len(provider_presets(include_information=False)) == 22
    assert len(provider_presets()) == 23  # 22 compatible presets plus one info-only MiniMax endpoint


def test_persisted_provider_source_json_keeps_the_full_official_catalog():
    source_path = Path(__file__).parents[1] / "coat_sim/ui/provider_catalog_sources.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    rows = source["presets"]
    assert source["verified_at"] == "2026-10-07"
    assert len(rows) == 22
    assert len({row["id"] for row in rows}) == 22
    assert all(row["protocol"] == "openai_chat_completions" for row in rows)
    assert sum(row.get("selectable") is False for row in rows) == 4

    by_id = {row["id"]: row for row in rows}
    assert by_id["volcengine_ark_agent_plan"]["selectable"] is True
    assert "automatically probe subscription candidates" in by_id["volcengine_ark_agent_plan"]["notes"]
    assert "Responses" in by_id["openai"]["notes"]
    assert "原生 Messages" in by_id["anthropic"]["notes"]
    assert "Enterprise 条目不应宣称普通 key 可用" in by_id["groq"]["notes"]
    assert "不是照展示名推断" in by_id["mistral"]["notes"]
    assert all(row.get("sources") for row in rows)
    assert all(0 < len(row.get("display_hint", "").strip()) <= 120 for row in rows)
    assert all("\ufffd" not in row["display_hint"] for row in rows)
    assert all(any("\u4e00" <= char <= "\u9fff" for char in row["display_hint"])
               for row in rows)

    for profile in provider_presets(include_information=False):
        display = settings._provider_info_text(profile)
        assert profile["display_hint"] in display
        assert profile["notes"] not in display
        assert not any(source in display for source in profile["sources"])

    openai = next(row for row in provider_presets(include_information=False)
                  if row["id"] == "openai")
    info_text = settings._provider_info_text(openai)
    assert openai["display_hint"] in info_text
    assert openai["notes"] not in info_text
    assert not any(source in info_text for source in openai["sources"])
    assert openai["usage_restriction"] == openai["display_hint"]

    info = next(row for row in provider_presets() if row["id"] == "minimax_intl_anthropic_info")
    assert info["selectable"] is False and info["probe_allowed"] is False
    assert info["display_hint"] in info["usage_restriction"]


def test_compaction_threshold_is_endpoint_model_scoped_and_preserves_effective_boundary(tmp_path, monkeypatch):
    endpoint_a = "https://gateway.example/team-a/v1"
    endpoint_b = "https://gateway.example/team-b/v1"
    old_limits = normalized_model_limits({endpoint_a: {"model-x": {"context_tokens": 10_000}}})
    assert old_limits[endpoint_a]["model-x"]["compaction_threshold_percent"] == 0
    assert DEFAULT_COMPACTION_THRESHOLD_PERCENT == 70
    default_budget = context_budget_for_model(
        {"assistant_model_limits": old_limits, "assistant_context_budget_version": 2},
        endpoint_a, "model-x")
    assert default_budget["overrides"]["compaction_threshold_percent"] == 0
    assert default_budget["limits"]["compaction_threshold_percent"] == 70
    assert default_budget["trigger_tokens"] == default_budget["budget_tokens"] * 70 // 100
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    saved = settings.save_ui_settings({"assistant_model_limits": {
        endpoint_a: {"model-x": {"context_tokens": 10_000,
                                  "compaction_threshold_percent": 63}}}})
    assert saved["assistant_model_limits"][endpoint_a]["model-x"]["compaction_threshold_percent"] == 63
    assert settings.load_ui_settings()["assistant_model_limits"][endpoint_a]["model-x"][
        "compaction_threshold_percent"] == 63

    config = {"assistant_model_limits": {
        endpoint_a: {"model-x": {"max_input_tokens": 8_000, "context_tokens": 10_000,
                                  "compaction_threshold_percent": 100}},
        endpoint_b: {"model-x": {"context_tokens": 20_000, "compaction_threshold_percent": 25}},
    }}
    policy = {"desired_output": 2_000}
    limits_a = model_token_limits(config, endpoint_a, "model-x")
    limits_b = model_token_limits(config, endpoint_b, "model-x")
    assert limits_a["compaction_threshold_percent"] == 100
    assert limits_b["compaction_threshold_percent"] == 25

    at_boundary = context_compaction_limits(config, endpoint_a, "model-x", policy)
    assert at_boundary["available_input"] == 6_976  # min(input cap, context minus auto output), less compression reserve
    assert at_boundary["trigger"] == at_boundary["available_input"]
    assert at_boundary["threshold_percent"] == 100

    config["assistant_model_limits"][endpoint_a]["model-x"]["compaction_threshold_percent"] = 1
    one_percent = context_compaction_limits(config, endpoint_a, "model-x", policy)
    assert one_percent["trigger"] == one_percent["available_input"] // 100
    assert one_percent["trigger"] == 69

    config["assistant_model_limits"][endpoint_a]["model-x"]["compaction_threshold_percent"] = 40
    unknown_capacity = context_compaction_limits(config, endpoint_a, "unknown-model", policy)
    expected_unknown_budget = UNKNOWN_INPUT_BUDGET - 1_280
    assert unknown_capacity["available_input"] == expected_unknown_budget
    assert unknown_capacity["trigger"] == expected_unknown_budget * 70 // 100

    for invalid in (0, 101, True):
        normalized = normalized_model_limits({endpoint_a: {"model-x": {
            "compaction_threshold_percent": invalid}}})
        raw_percent = normalized[endpoint_a]["model-x"]["compaction_threshold_percent"]
        assert type(raw_percent) is type(invalid) and raw_percent == invalid
        budget = context_budget_for_model(
            {"assistant_model_limits": normalized, "assistant_context_budget_version": 2},
            endpoint_a, "model-x")
        if invalid == 0:
            assert budget["validation_errors"] == []
            assert budget["limits"]["compaction_threshold_percent"] == 70
        else:
            assert budget["validation_errors"]
            assert budget["overrides"]["compaction_threshold_percent"] == invalid

    qml = (Path(__file__).parents[1] / "coat_sim/ui/qml/SettingsWindow.qml").read_text(encoding="utf-8")
    assert "id: contextCompactionThresholdSpin" in qml
    assert "from: 0" in qml and "to: 100" in qml
    assert "onValueModified: window.saveContextProfile()" in qml


def test_one_million_context_uses_eighty_percent_auto_threshold_on_init_and_reset(tmp_path, monkeypatch):
    endpoint = "https://api.deepseek.com/v1"
    model = "deepseek-flash"
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "ui_settings.json")

    initial = context_budget_for_model(
        {"assistant_api_url": endpoint, "assistant_model": model}, endpoint, model)
    assert initial["profile"]["context_window_tokens"] == 1_000_000
    assert initial["overrides"]["compaction_threshold_percent"] == 0
    assert initial["defaults"]["compaction_threshold_percent"] == 80
    assert initial["limits"]["compaction_threshold_percent"] == 80
    assert initial["trigger_tokens"] == initial["budget_tokens"] * 80 // 100
    runtime = context_compaction_limits(
        {"assistant_api_url": endpoint, "assistant_model": model}, endpoint, model)
    assert runtime["threshold_percent"] == 80
    assert runtime["trigger"] == runtime["available_input"] * 80 // 100

    # The registry's 1.05M context-window records use the same narrow tier.
    nearby = context_budget_for_model({}, "https://api.openai.com/v1", "gpt-6-astra")
    assert nearby["profile"]["context_window_tokens"] == 1_050_000
    assert nearby["defaults"]["compaction_threshold_percent"] == 80
    assert nearby["limits"]["compaction_threshold_percent"] == 80

    binary_context = context_budget_for_model({
        "assistant_model_limits": {
            "https://gateway.example/v1": {"binary-million": {
                "context_window_tokens": 1_048_576,
                "context_budget_version": 2,
            }},
        },
        "assistant_context_budget_version": 2,
    }, "https://gateway.example/v1", "binary-million")
    assert binary_context["limits"]["context_window_tokens"] == 1_048_576
    assert binary_context["defaults"]["compaction_threshold_percent"] == 80
    assert binary_context["limits"]["compaction_threshold_percent"] == 80
    assert binary_context["trigger_tokens"] == binary_context["budget_tokens"] * 80 // 100

    # Gemini's input hard cap does not establish context capacity; leave the
    # unknown-context default alone instead of inferring from input alone.
    binary_million = context_budget_for_model(
        {}, "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash")
    assert binary_million["profile"]["context_window_tokens"] == 0
    assert binary_million["profile"]["max_input_tokens"] == 1_048_576
    assert binary_million["defaults"]["compaction_threshold_percent"] == 70
    assert binary_million["limits"]["compaction_threshold_percent"] == 70
    assert binary_million["trigger_tokens"] == binary_million["budget_tokens"] * 70 // 100

    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication.instance() or QGuiApplication([])
    bridge = settings.SettingsBridge()
    try:
        bridge.setAssistantConfig(False, endpoint, model)
        created = bridge._context_profile_record(create=True)
        assert created is not None
        assert created[2]["compaction_threshold_percent"] == 0
        assert bridge.assistantContextBudget["limits"]["compaction_threshold_percent"] == 80

        bridge.setAssistantContextProfile(0, 0, 0, 0, 63)
        manual = bridge.assistantContextBudget
        assert manual["defaults"]["compaction_threshold_percent"] == 80
        assert manual["limits"]["compaction_threshold_percent"] == 63
        assert manual["trigger_tokens"] == manual["budget_tokens"] * 63 // 100

        assert bridge.resetAssistantContextProfile()
        reset = bridge.assistantContextBudget
        assert reset["overrides"]["compaction_threshold_percent"] == 0
        assert reset["defaults"]["compaction_threshold_percent"] == 80
        assert reset["limits"]["compaction_threshold_percent"] == 80
        assert reset["trigger_tokens"] == reset["budget_tokens"] * 80 // 100
    finally:
        bridge.closeModelProbe()
        bridge.closeAssistantSettingsBootstrap()
        bridge.closeMcpConnections()


def test_reasoning_preference_persists_and_maps_to_request_field(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "ui_settings.json")
    saved = settings.save_ui_settings({
        "assistant_task_mode": "research",
        "assistant_reasoning_strength": "high",
    })
    loaded = json.loads(settings.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert saved["assistant_reasoning_strength"] == loaded["assistant_reasoning_strength"] == "high"
    assert task_options(loaded) == ("research", "high")

    capabilities = reasoning_capabilities("https://api.openai.com/v1", "gpt-5.4")
    policy = request_policy(capabilities, "research", "high", "main")
    assert policy["model_settings"]["openai_reasoning_effort"] == "high"


def test_assistant_tone_catalog_and_legacy_values_normalize_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "ui_settings.json")

    saved = settings.save_ui_settings({"assistant_tone": "intimate"})
    assert saved["assistant_tone"] == "intimate"
    assert settings.load_ui_settings()["assistant_tone"] == "intimate"
    assert settings.save_ui_settings({"assistant_tone": "lively"})["assistant_tone"] == "standard"
    assert settings.load_ui_settings()["assistant_tone"] == "standard"
    assert settings.save_ui_settings({"assistant_tone": "serious"})["assistant_tone"] == "standard"
    assert set(ASSISTANT_TONES).issubset({"standard", "intimate"})
    assert normalized_tone("intimate") == "intimate"
    assert normalized_tone("lively") == "standard"
    assert normalized_tone("unrecognized-tone") == "standard"
    assert tone_instructions({"assistant_tone": "intimate"}) != tone_instructions({"assistant_tone": "standard"})
    assert tone_instructions({"assistant_tone": "serious"}) == tone_instructions({"assistant_tone": "standard"})

    qml = (Path(__file__).parents[1] / "coat_sim/ui/qml/SettingsWindow.qml").read_text(encoding="utf-8")
    tone_combo = qml.split("id: assistantToneCombo", 1)[1].split("\n                                }", 1)[0]
    ui_values = set(re.findall(r'value: "([^"]+)"', tone_combo))
    assert set(ASSISTANT_TONES).issubset(ui_values)


def test_unknown_model_cannot_claim_unverified_reasoning_strength():
    capabilities = reasoning_capabilities("https://gateway.example/v1", "private-model")
    assert capabilities["supported_strengths"] == ["auto"]
    assert capabilities["reasoning_supported"] is False
    with pytest.raises(ValueError, match="未确认支持"):
        request_policy(capabilities, "chat", "high", "main")


def test_known_provider_reasoning_capabilities_remain_distinct():
    deepseek = reasoning_capabilities("https://api.deepseek.com/v1", "deepseek-flash")
    ark = reasoning_capabilities("https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2.1-pro")
    assert deepseek["provider"] == "deepseek"
    assert deepseek["supported_strengths"] == ["auto", "low", "high", "max"]
    assert ark["provider"] == "ark"
    assert ark["supported_strengths"] == ["auto", "low", "medium", "high"]


def test_mock_provider_model_response_and_verified_effort_reach_request_payload():
    endpoint = "https://gateway.example/team/v1"
    models = model_records({"data": [
        {"id": "private-model", "context_length": 100_000},
        {"id": "another-model"},
    ]})
    assert [row["id"] for row in models] == ["another-model", "private-model"]

    probe = reasoning_result(endpoint, "private-model", ["low", "high"], [],
                             {"requests": 3, "input_tokens": 9, "output_tokens": 6}, models, "max_tokens")
    capabilities = reasoning_capabilities(endpoint, "private-model", probe)
    policy = request_policy(capabilities, "operate", "high", "main")
    body = completion_body("private-model", "max_tokens", {
        "reasoning_effort": policy["model_settings"]["openai_reasoning_effort"]})
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 32


def test_provider_auto_discovery_requires_enabled_assistant_and_saved_key():
    # The probe starts after an endpoint-bound credential refresh, not directly
    # in _auto_discover. Exercise both real boundaries without a real keyring.
    for overrides in ({"enabled": False}, {"has_key": False},
                      {"fingerprint": None}, {"fresh": True}):
        bridge = _callback_bridge(**overrides)
        refreshes = []
        bridge._credential_future = None
        bridge._credential_request = None
        bridge._assistant_key_status_ready = True
        bridge._assistant_credential_api_url = API_URL
        bridge._has_assistant_key = overrides.get("has_key", True)
        bridge._assistant_credential_fingerprint = overrides.get("fingerprint", FINGERPRINT)
        bridge._refresh_bound_key = lambda **kwargs: refreshes.append(kwargs)
        bridge._probe_coordinator._auto_discover()
        assert refreshes == []
        assert bridge._probe_coordinator._network.calls == []

    for overrides in ({"enabled": False}, {"has_key": False},
                      {"fingerprint": None}, {"fresh": True},
                      {"request_generation": 6},
                      {"request_url": "https://stale-fixture.invalid/v1"}):
        bridge = _callback_bridge(**overrides)
        settings.SettingsBridge._on_bound_key_lookup_ready(bridge)
        assert bridge._probe_coordinator._network.calls == []
        assert bridge._discovery_bus.claims == []

    refresh_source = inspect.getsource(settings.SettingsProbeCoordinator._start_probe)
    assert "self._dependencies.local_anonymous_endpoint" in refresh_source
    assert "self._dependencies.read_api_key(self.assistantApiUrl, strict=True)" in refresh_source
    assert "_queue_model_candidate_fallback" in inspect.getsource(settings.SettingsProbeCoordinator._on_models_finished)
    assert "candidate_models" in inspect.getsource(settings.SettingsProbeCoordinator._queue_model_candidate_fallback)


def test_agent_plan_auto_discovery_is_get_only_but_manual_candidate_fallback_remains():
    bridge = _callback_bridge()
    refreshes, records, finished = [], [], []
    bridge._credential_future = None
    bridge._credential_request = None
    bridge._assistant_key_status_ready = True
    bridge._assistant_credential_api_url = API_URL
    bridge._assistant_credential_secret = FAKE_SECRET
    bridge._assistant_credential_fingerprint = FINGERPRINT
    bridge._has_assistant_key = True
    bridge._refresh_bound_key = lambda **kwargs: refreshes.append(kwargs)
    bridge._probe_coordinator._auto_discover()
    assert refreshes == [{"force": True, "reason": "auto_probe"}]
    assert bridge._probe_coordinator._network.calls == []

    bridge._credential_future = _Future(_credential_result())
    bridge._credential_request = (7, API_URL)
    settings.SettingsBridge._on_bound_key_lookup_ready(bridge)
    assert [call[0] for call in bridge._probe_coordinator._network.calls] == ["GET"]
    assert bridge._probe_coordinator._network.calls[0][1].url().toString().endswith("/models")
    bridge._probe_coordinator._record_probe = lambda *args: records.append(args)
    bridge._probe_coordinator._finish_probe = lambda *args, **kwargs: finished.append((args, kwargs))
    settings.SettingsProbeCoordinator._queue_model_candidate_fallback(
        bridge._probe_coordinator, "fixture response")
    assert [call[0] for call in bridge._probe_coordinator._network.calls] == ["GET"]
    assert not bridge._probe_coordinator._probe_candidate_fallback
    assert finished

    # Explicit manual fallback still creates candidate requests and uses POST.
    # The network object captures the request; it cannot open a socket.
    bridge._probe_coordinator._probe_automatic = False
    bridge._probe_coordinator._probe_base = "https://api.openai.com/v1"
    bridge._probe_coordinator._probe_model = "gpt-6-luna"
    queued = []
    bridge._probe_coordinator._next_probe_request = lambda: queued.extend(bridge._probe_coordinator._probe_queue)
    settings.SettingsProbeCoordinator._queue_model_candidate_fallback(
        bridge._probe_coordinator, "manual fixture")
    assert bridge._probe_coordinator._probe_candidate_fallback
    assert queued and all(task["kind"] == "model" for task in queued)
    assert len(queued) <= MODEL_REQUEST_LIMIT
    settings.SettingsProbeCoordinator._send_probe_request(bridge._probe_coordinator, queued[0])
    assert [call[0] for call in bridge._probe_coordinator._network.calls] == ["GET", "POST"]
    assert bridge._probe_coordinator._network.calls[-1][1].url().toString().endswith("/chat/completions")
    posted = json.loads(bridge._probe_coordinator._network.calls[-1][2])
    assert posted["model"] == queued[0]["model"]
    assert posted[queued[0]["output_parameter"]] > 0

    auto = inspect.getsource(settings.SettingsProbeCoordinator._auto_discover)
    begin = inspect.getsource(settings.SettingsProbeCoordinator._begin_models_probe)
    fallback = inspect.getsource(settings.SettingsProbeCoordinator._queue_model_candidate_fallback)
    finish = inspect.getsource(settings.SettingsProbeCoordinator._on_models_finished)
    next_request = inspect.getsource(settings.SettingsProbeCoordinator._next_probe_request)

    assert "default_auto_probe" in auto
    assert "models_endpoint_for_url" in begin
    assert "_queue_model_candidate_fallback" not in begin
    assert "_queue_model_candidate_fallback" in finish
    assert "if self._probe_automatic" in fallback
    assert "candidate POSTs are manual-only" in fallback
    assert "candidate_models" in fallback
    assert "MODEL_REQUEST_LIMIT" in next_request

    for method in (settings.SettingsBridge.saveAssistantApiKey,
                   settings.SettingsBridge.setAssistantConfig,
                   settings.SettingsBridge.save,
                   settings.SettingsBridge.reloadAssistantSettings,
                   settings.SettingsBridge.resumeModelProbe):
        assert "_schedule_auto_probe" in inspect.getsource(method)
    assert "_probe_coordinator._begin_models_probe()" in inspect.getsource(settings.SettingsBridge.discoverModels)


def test_reasoning_combobox_keeps_value_role_and_save_binding():
    qml = (Path(__file__).parents[1] / "coat_sim/ui/qml/SettingsWindow.qml").read_text(encoding="utf-8")
    reasoning_combo = qml.split('id: reasoningCombo', 1)[1].split("\n                            }", 1)[0]
    assert 'textRole: "text"' in reasoning_combo
    assert 'valueRole: "value"' in reasoning_combo
    assert "bridge.setAssistantTaskOptions(taskModeCombo.currentValue, currentValue)" in reasoning_combo
    save_button = qml.split('objectName: "saveSettingsButton"', 1)[1].split("\n        }", 1)[0]
    assert "form.saveSettingsDraft()" in save_button
    assert 'reasoningCombo.currentValue || "auto"' in qml
    assert "bridge.setAssistantTaskOptions(" in qml
    assert "reasoning: reasoningCombo.currentValue" not in qml
    assert "至多 12 个候选模型" in qml
    assert "模型缓存按服务商、地址和凭据区分" in qml
    assert "列表刷新不会发送聊天请求" not in qml
    provider_combo = qml.split("id: providerPresetCombo", 1)[1].split("\n                        }", 1)[0]
    assert "bridge.providerPresets" in provider_combo
    assert "assistantUrlField.editText = preset.base_url" in provider_combo
    assert "bridge.setAssistantConfig" not in provider_combo
    assert "bridge.discoverModels" not in provider_combo
    assert 'id: providerModelCatalog' in qml
    assert 'bridge.providerModelSuggestions(assistantUrlField.text)' in qml
    for object_name in ("settingsScroll", "assistantModelField", "refreshModelsButton",
                        "discoveredModelList", "discoveredModelDelegate", "reasoningCombo",
                        "saveSettingsButton"):
        assert f'objectName: "{object_name}"' in qml
