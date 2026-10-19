"""Offline regression tests for built-in assistant model configuration.

The Qt fixture redirects settings/cache/theme files to ``tmp_path`` and blocks
network requests. No test reads a real credential or user configuration.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QMetaObject, Qt, Q_ARG

from coat_sim.agent.modelclient import context_budget_for_model as resolve_context_budget
from coat_sim.ui import settings
from coat_sim.ui.model_probe import model_records
from coat_sim.ui.provider_catalog import (
    model_configuration_for,
    model_suggestions,
    provider_presets,
)
from tests.test_compute_server_category_ui import _find, _settle, compute_ui as compute_ui
from tests.test_settings_coordinator_contract import FakeNetwork


DEEPSEEK = "https://api.deepseek.com"
OPENAI = "https://api.openai.com/v1"
CUSTOM = "https://gateway.example/team/v1"


def context_budget_for_model(config: Mapping, api_url: str, model: str) -> dict[str, Any]:
    # The public budget API returns a heterogeneous mapping. Check its structured
    # boundary before the tests inspect nested values; no diagnostics are ignored.
    budget = resolve_context_budget(config, api_url, model)
    for field in ("limits", "overrides", "profile"):
        assert isinstance(budget[field], dict)
    return cast(dict[str, Any], budget)


def test_first_start_is_unconfigured_while_offline_catalog_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "new-settings.json")
    fresh = settings.load_ui_settings()
    assert fresh["assistant_api_url"] == ""
    assert fresh["assistant_model"] == ""
    assert any(row["default_model"] for row in provider_presets(include_information=False)
               if row["selectable"])


def test_offline_preset_defaults_and_unknown_model_are_explicit():
    selectable = [row for row in provider_presets(include_information=False)
                  if row["selectable"]]
    assert selectable
    for preset in selectable:
        default = str(preset["default_model"])
        assert default
        assert default in {row["id"] for row in model_suggestions(str(preset["base_url"]))}
        configuration = model_configuration_for(str(preset["base_url"]), default)
        assert configuration["protocol"] == preset["protocol"]
        assert configuration["availability_verified"] is False
        assert configuration["sources"]
        assert configuration["verified_date"]

    unknown = model_configuration_for(CUSTOM, "manually-typed-model")
    assert unknown["context_window_tokens"] == 0
    assert unknown["max_input_tokens"] == 0
    assert unknown["max_output_tokens"] == 0
    assert unknown["availability_verified"] is False
    assert unknown["capabilities_verified"] is False
    assert unknown["capabilities"]
    assert all(value == "unknown" for value in unknown["capabilities"].values())


def test_offline_facts_require_exact_endpoint_and_model_id():
    known = model_configuration_for(DEEPSEEK, "deepseek-flash")
    same_id_other_endpoint = model_configuration_for(CUSTOM, "deepseek-flash")
    unknown_id_same_endpoint = model_configuration_for(DEEPSEEK, "deepseek-flash-unknown")

    assert known["context_window_tokens"] > 0
    assert known["sources"]
    assert same_id_other_endpoint["context_window_tokens"] == 0
    assert unknown_id_same_endpoint["context_window_tokens"] == 0
    assert same_id_other_endpoint["capabilities_verified"] is False
    assert unknown_id_same_endpoint["capabilities_verified"] is False
    # A model-list ID alone does not establish capacity or function-tool support.
    assert all(value == "unknown" for value in same_id_other_endpoint["capabilities"].values())
    listed = model_records({"data": [{"id": "unlisted-model",
                                      "context_length": 9_999_999,
                                      "supports_tools": True}]})
    assert listed[0]["id"] == "unlisted-model"
    unlisted = model_configuration_for(DEEPSEEK, listed[0]["id"])
    assert unlisted["context_window_tokens"] == 0
    assert unlisted["capabilities_verified"] is False


def test_manual_context_override_survives_model_and_endpoint_switching():
    config = {"assistant_model_limits": {DEEPSEEK: {
        "deepseek-flash": {
            "context_budget_version": 2,
            "context_window_tokens": 200_000,
            "reserved_output_tokens": 4_000,
        },
    }}}
    selected = context_budget_for_model(config, DEEPSEEK, "deepseek-flash")
    other_model = context_budget_for_model(config, DEEPSEEK, "deepseek-v4-pro")
    other_endpoint = context_budget_for_model(config, CUSTOM, "deepseek-flash")

    assert selected["limits"]["context_window_tokens"] == 200_000
    assert selected["limits"]["reserved_output_tokens"] == 4_000
    assert other_model["overrides"]["context_window_tokens"] == 0
    assert other_endpoint["overrides"]["context_window_tokens"] == 0
    assert selected["profile"]["context_window_tokens"] > 0


def test_qml_preset_and_catalog_selection_update_draft_without_network(compute_ui):
    scene = compute_ui.open()
    bridge = scene.bridge
    category = _find(scene.window, "settingsAdvancedCategory")
    category.setProperty("expanded", True)
    _settle()

    provider = _find(scene.window, "providerPresetCombo")
    model_field = _find(scene.window, "assistantModelField")
    url_field = _find(scene.window, "assistantUrlField")
    assert bridge.assistantModel == "fixture-model"

    presets = bridge.providerPresets
    deepseek_index = next(index for index, row in enumerate(presets)
                          if row["id"] == "deepseek_official")
    provider.setProperty("currentIndex", deepseek_index)
    assert QMetaObject.invokeMethod(provider, "activated", Qt.ConnectionType.DirectConnection,
                                    Q_ARG(int, deepseek_index))
    _settle()
    deepseek_default = presets[deepseek_index]["default_model"]
    assert bridge.assistantApiUrl == DEEPSEEK
    assert bridge.assistantModel == deepseek_default
    assert model_field.property("text") == deepseek_default
    assert str(url_field.property("editText")).rstrip("/") == DEEPSEEK

    catalog = _find(scene.window, "providerModelCatalog")
    suggested_ids = [row["id"] for row in model_suggestions(DEEPSEEK)]
    alternate = next(model for model in suggested_ids if model != deepseek_default)
    alternate_index = suggested_ids.index(alternate)
    catalog.setProperty("currentIndex", alternate_index)
    assert QMetaObject.invokeMethod(catalog, "activated", Qt.ConnectionType.DirectConnection,
                                    Q_ARG(int, alternate_index))
    _settle()
    assert bridge.assistantModel == alternate
    assert model_field.property("text") == alternate

    bridge.setAssistantConfig(False, DEEPSEEK, "manual-deepseek-model")
    bridge.setAssistantContextProfile(100_000, 2_000, 0, 0, 0)
    assert bridge.applyAssistantProviderPreset("deepseek_official") is True
    assert bridge.assistantModel == "manual-deepseek-model"

    openai_index = next(index for index, row in enumerate(presets)
                        if row["id"] == "openai")
    provider.setProperty("currentIndex", openai_index)
    assert QMetaObject.invokeMethod(provider, "activated", Qt.ConnectionType.DirectConnection,
                                    Q_ARG(int, openai_index))
    _settle()
    assert bridge.assistantApiUrl == OPENAI
    assert bridge.assistantModel == presets[openai_index]["default_model"]
    assert model_field.property("text") == bridge.assistantModel

    bridge.setAssistantConfig(False, DEEPSEEK, "manual-deepseek-model")
    assert bridge.assistantContextBudget["overrides"]["context_window_tokens"] == 100_000
    assert bridge.assistantContextBudget["overrides"]["reserved_output_tokens"] == 2_000
    assert not scene.network.blocked_urls
    compute_ui.network.get.assert_not_called()
    compute_ui.network.post.assert_not_called()
    QCoreApplication.processEvents()


def _visible_status_text(scene, expected: str) -> bool:
    content = scene.window.contentItem()
    pending = [content]
    while pending:
        item = pending.pop()
        if item is None:
            continue
        if item.property("visible") and item.property("text") == expected:
            return True
        pending.extend(item.childItems())
    return False


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "success"])
def test_model_refresh_preserves_last_good_until_success(compute_ui, outcome):
    fake_secret = "catalog-refresh-fixture-secret"
    compute_ui.read_key.return_value = fake_secret
    scene = compute_ui.open()
    bridge = scene.bridge
    category = _find(scene.window, "settingsAdvancedCategory")
    category.setProperty("expanded", True)
    _settle()

    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    bridge.setAssistantContextProfile(100_000, 2_000, 0, 0, 0)
    model_before = bridge.assistantModel
    budget_before = bridge.assistantContextBudget["limits"].copy()
    suggestions_before = bridge.providerModelSuggestions(DEEPSEEK)
    bus = bridge._discovery_bus
    fingerprint = settings.ModelDiscoveryBus.credential_fingerprint(DEEPSEEK, fake_secret)
    bridge._assistant_credential_fingerprint = fingerprint
    bridge._assistant_credential_api_url = DEEPSEEK
    bridge._assistant_key_status_ready = True
    old = {"id": "last-good", "metadata": {}, "evidence": "models_endpoint"}
    bus.publish(DEEPSEEK, [old], expected_fingerprint=fingerprint)
    _settle()
    assert [row["id"] for row in bridge.availableModels] == ["last-good"]
    assert _find(scene.window, "discoveredModelList").property("visible")

    fake_network = FakeNetwork()
    bridge._probe_coordinator._network = fake_network
    bridge._begin_models_probe()
    assert len(fake_network.calls) == 1
    assert fake_network.calls[0][0] == "GET"
    assert [row["id"] for row in bridge.availableModels] == ["last-good"]
    assert [row["id"] for row in bus.models_for_fingerprint(DEEPSEEK, fingerprint)] == ["last-good"]

    reply = fake_network.replies[-1]
    if outcome == "failed":
        reply.status = 503
        reply.data = json.dumps({"error": {"message": "fixture outage"}}).encode()
        reply.finished.emit()
    elif outcome == "cancelled":
        bridge.cancelModelProbe()
        assert reply.aborted
    else:
        reply.status = 200
        # List IDs alone provide no evidence of capacity, reasoning or tools.
        reply.data = json.dumps({"data": [{"id": "newly-listed"}]}).encode()
        reply.finished.emit()
    _settle()

    expected = ["newly-listed"] if outcome == "success" else ["last-good"]
    assert not bridge.modelDiscoveryBusy
    assert [row["id"] for row in bridge.availableModels] == expected
    assert [row["id"] for row in bus.models_for_fingerprint(DEEPSEEK, fingerprint)] == expected
    assert bridge.modelDiscoveryMessage
    assert _visible_status_text(scene, bridge.modelDiscoveryMessage)
    if outcome == "failed":
        assert any(row["http_status"] == 503 for row in bridge.probeResults)

    assert bridge.providerModelSuggestions(DEEPSEEK) == suggestions_before
    assert bridge.assistantModel == model_before
    assert bridge.assistantContextBudget["limits"] == budget_before
    assert bridge.assistantContextBudget["overrides"]["context_window_tokens"] == 100_000
    assert bus.reasoning_for(DEEPSEEK, "newly-listed") is None
    assert model_configuration_for(DEEPSEEK, "newly-listed")["context_window_tokens"] == 0
    assert not scene.network.blocked_urls
    compute_ui.network.get.assert_not_called()
    compute_ui.network.post.assert_not_called()


def test_documented_capacity_metadata_refreshes_auto_fields_and_preserves_manual(compute_ui):
    import json
    import time
    from coat_sim.ui import settings
    from coat_sim.ui.provider_catalog import MODEL_METADATA_TTL_SECONDS
    from tests.test_settings_coordinator_contract import FakeNetwork

    scene = compute_ui.open()
    bridge = scene.bridge
    compute_ui.read_key.return_value = "metadata-fixture-key"
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    bridge.setAssistantContextProfile(100_000, 2_000, 0, 0, 0)
    coordinator = bridge._probe_coordinator
    coordinator._network = network = FakeNetwork()
    _find(scene.window, "settingsAdvancedCategory").setProperty("expanded", True)
    _settle()
    bridge._begin_models_probe()
    reply = network.replies[-1]
    reply.data = json.dumps({"data": [{"id": "deepseek-flash", "context_window": 262_144,
                                      "max_output_tokens": 16_384}]}).encode()
    bridge._on_models_finished(reply)
    budget = bridge.assistantContextBudget
    assert budget["profile"]["context_window_tokens"] == 262_144
    assert budget["limits"]["max_output_tokens"] == 16_384
    assert budget["limits"]["context_window_tokens"] == 100_000
    assert budget["limits"]["reserved_output_tokens"] == 2_000
    assert budget["profile"]["evidence"] == "server_declared_metadata"
    assert "服务端声明容量" in _find(scene.window, "providerModelConfigurationInfo").property("text")
    assert not bridge._reasoning_capabilities()["probe_verified"]
    assert [call[0] for call in network.calls] == ["GET"]
    assert bridge.save()
    saved = settings.load_ui_settings()
    # Credentials and server snapshots never enter ordinary settings/export data.
    assert "provider_metadata" not in json.dumps(saved)
    assert "capacity_metadata" not in json.dumps(saved)
    persisted = context_budget_for_model(saved, DEEPSEEK, "deepseek-flash")
    assert persisted["limits"]["max_output_tokens"] == 393_216
    assert persisted["limits"]["context_window_tokens"] == 100_000
    bus = bridge._discovery_bus
    probe = bus.reasoning_for(DEEPSEEK, "deepseek-flash")
    worker_config = {**saved, "assistant_reasoning_probe": probe}
    assert context_budget_for_model(worker_config, DEEPSEEK, "deepseek-flash")["limits"]["max_output_tokens"] == 16_384
    found = bus._entry_for_fingerprint(DEEPSEEK, bridge._assistant_credential_fingerprint)
    assert found is not None
    key, entry = found
    entry["models_updated_at"] = time.time() - MODEL_METADATA_TTL_SECONDS - 1
    bus._persist(key)
    expired = bridge.assistantContextBudget
    assert expired["profile"]["context_window_tokens"] == 1_048_576
    assert expired["limits"]["context_window_tokens"] == 100_000
    assert expired["limits"]["max_output_tokens"] == 393_216


def test_metadata_schema_validation_does_not_guess_or_accept_invalid_limits(compute_ui):
    import copy
    import time
    from coat_sim.ui.provider_catalog import model_metadata_capacity

    scene = compute_ui.open()
    bridge = scene.bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    for metadata in ({}, {"context_window": True}, {"context_window": -1},
                     {"context_window": "1000000"}, {"max_output_tokens": 3_000_000}):
        assert model_metadata_capacity(DEEPSEEK, "deepseek-flash", metadata) == {}
    assert model_metadata_capacity(CUSTOM, "deepseek-flash", {"context_window": 256_000}) == {}
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_length": 1234, "supports_tools": True}}]) == 0
    assert bridge.assistantContextBudget["profile"]["context_window_tokens"] == 1_048_576
    record = model_metadata_capacity(DEEPSEEK, "deepseek-flash", {"context_window": 262_144})
    config: dict[str, Any] = {"assistant_reasoning_probe": {
        "api_url": DEEPSEEK, "model": "deepseek-flash", "evidence": "server_capacity_only",
        "capacity_metadata": record}}
    for field, value in (("api_url", CUSTOM), ("model", "other"), ("observed_at", float("nan")),
                         ("observed_at", time.time() + 60), ("schema_source", "https://example.invalid")):
        wrong = copy.deepcopy(config)
        wrong["assistant_reasoning_probe"]["capacity_metadata"][field] = value
        assert context_budget_for_model(wrong, DEEPSEEK, "deepseek-flash")["profile"]["context_window_tokens"] == 1_048_576


@pytest.mark.parametrize("failure", ["network", "timeout", "oversized"])
def test_failed_2xx_response_cannot_update_capacity_or_start_completions(compute_ui, failure):
    from PySide6.QtNetwork import QNetworkReply
    scene = compute_ui.open()
    bridge = scene.bridge
    compute_ui.read_key.return_value = "broken-response-fixture-key"
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    coordinator = bridge._probe_coordinator
    coordinator._network = network = FakeNetwork()
    bridge._begin_models_probe()
    reply = network.replies[-1]
    reply.data = json.dumps({"data": [{"id": "deepseek-flash", "context_window": 262_144,
                                      "max_output_tokens": 16_384}]}).encode()
    if failure == "network":
        reply.error = lambda: QNetworkReply.NetworkError.RemoteHostClosedError
    elif failure == "timeout":
        coordinator._model_discovery_timed_out = True
    else:
        coordinator._model_discovery_oversized = True
    reply.finished.emit()
    assert not bridge.modelDiscoveryBusy
    assert bridge.assistantContextBudget["profile"]["context_window_tokens"] == 1_048_576
    assert "provider_metadata" not in bridge._settings.get("assistant_model_limits", {}).get("https://api.deepseek.com/v1", {}).get("deepseek-flash", {})
    assert [call[0] for call in network.calls] == ["GET"]
    assert "失败" in bridge.modelDiscoveryMessage


def test_shared_model_updates_are_scoped_to_each_windows_bound_key(compute_ui):
    first = compute_ui.open().bridge
    second = compute_ui.open().bridge
    for bridge, fingerprint in ((first, "fixture-fingerprint-a"), (second, "fixture-fingerprint-b")):
        bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
        bridge._assistant_credential_api_url = DEEPSEEK
        bridge._assistant_credential_fingerprint = fingerprint
        bridge._assistant_key_status_ready = True
    bus = first._discovery_bus
    assert second._discovery_bus is bus
    bus.publish(DEEPSEEK, [{"id": "only-key-a"}], expected_fingerprint="fixture-fingerprint-a")
    assert [row["id"] for row in first.availableModels] == ["only-key-a"]
    assert second.availableModels == []
    bus.publish(DEEPSEEK, [{"id": "only-key-b"}], expected_fingerprint="fixture-fingerprint-b")
    assert [row["id"] for row in first.availableModels] == ["only-key-a"]
    assert [row["id"] for row in second.availableModels] == ["only-key-b"]


def test_qml_refresh_button_uses_existing_discovery_and_shows_failure(compute_ui):
    scene = compute_ui.open()
    bridge = scene.bridge
    compute_ui.read_key.return_value = "button-fixture-key"
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    _find(scene.window, "settingsAdvancedCategory").setProperty("expanded", True)
    _settle()
    bridge._probe_coordinator._network = network = FakeNetwork()
    button = _find(scene.window, "refreshModelsButton")
    assert button.property("enabled")
    scene.click(button)
    assert bridge.modelDiscoveryBusy
    assert [call[0] for call in network.calls] == ["GET"]
    reply = network.replies[-1]
    reply.status = 503
    reply.finished.emit()
    _settle()
    assert not bridge.modelDiscoveryBusy
    assert "失败" in bridge.modelDiscoveryMessage
    assert _visible_status_text(scene, bridge.modelDiscoveryMessage)
    assert bridge.providerModelSuggestions(DEEPSEEK)
    assert bridge.assistantModel == "deepseek-flash"


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_key_mutation_invalidates_capacity_but_preserves_manual_and_offline(compute_ui, monkeypatch, action):
    monkeypatch.setattr(settings, "save_api_key", Mock())
    monkeypatch.setattr(settings, "delete_api_key", Mock())
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    bridge.setAssistantContextProfile(100_000, 2_000, 0, 0, 0)
    assert bridge.saveAssistantApiKey("credential-bound-fixture-a")
    records = [{"id": "deepseek-flash", "metadata": {"context_window": 262_144, "max_output_tokens": 16_384}}]
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, records) == 1
    assert bridge.assistantContextBudget["profile"]["evidence"] == "server_declared_metadata"
    assert bridge.save()
    if action == "delete":
        assert bridge.clearAssistantApiKey()
    else:
        assert bridge.saveAssistantApiKey("credential-bound-fixture-b")
    budget = bridge.assistantContextBudget
    assert budget["profile"].get("evidence") != "server_declared_metadata"
    assert budget["profile"]["context_window_tokens"] == 1_048_576
    assert budget["limits"]["max_output_tokens"] == 393_216
    assert budget["overrides"]["context_window_tokens"] == 100_000
    assert budget["overrides"]["reserved_output_tokens"] == 2_000
    assert bridge.providerModelSuggestions(DEEPSEEK)
    assert bridge.save()
    reopened = compute_ui.open().bridge
    assert reopened.assistantContextBudget["profile"].get("evidence") != "server_declared_metadata"
    assert reopened.assistantContextBudget["overrides"]["context_window_tokens"] == 100_000
    assert reopened.assistantContextBudget["overrides"]["reserved_output_tokens"] == 2_000


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_key_mutation_invalidates_capacity_in_other_window(compute_ui, monkeypatch, action):
    monkeypatch.setattr(settings, "save_api_key", Mock())
    monkeypatch.setattr(settings, "delete_api_key", Mock())
    first = compute_ui.open().bridge
    first.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert first.saveAssistantApiKey("shared-fixture-a")
    assert first.save()
    second = compute_ui.open().bridge
    second._assistant_credential_api_url = DEEPSEEK
    second._assistant_credential_fingerprint = first._assistant_credential_fingerprint
    second._assistant_key_status_ready = True
    assert second._discovery_bus is first._discovery_bus
    assert first._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_window": 262_144}}]) == 1
    assert second.assistantContextBudget["profile"]["context_window_tokens"] == 262_144
    if action == "delete":
        assert first.clearAssistantApiKey()
    else:
        assert first.saveAssistantApiKey("shared-fixture-b")
    assert second.availableModels == []
    assert second.assistantContextBudget["profile"].get("evidence") != "server_declared_metadata"
    assert second.assistantContextBudget["profile"]["context_window_tokens"] == 1_048_576


def test_runtime_snapshot_rechecks_external_key_change_and_legacy_settings_are_inert(compute_ui, monkeypatch):
    from coat_sim.ui.provider_catalog import normalize_catalog_endpoint, model_metadata_capacity
    store: dict[str, str] = {}
    monkeypatch.setattr(settings, "save_api_key", lambda secret, url: store.__setitem__(normalize_catalog_endpoint(url), secret))
    monkeypatch.setattr(settings, "read_api_key", lambda url, **kwargs: store.get(normalize_catalog_endpoint(url)))
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert bridge.saveAssistantApiKey("external-fixture-a")
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_window": 262_144}}]) == 1
    bus = bridge._discovery_bus
    probe = bus.reasoning_for(DEEPSEEK, "deepseek-flash")
    assert context_budget_for_model({"assistant_reasoning_probe": probe}, DEEPSEEK, "deepseek-flash")["profile"]["context_window_tokens"] == 262_144
    store[normalize_catalog_endpoint(DEEPSEEK)] = "external-fixture-b"
    next_probe = bus.reasoning_for(DEEPSEEK, "deepseek-flash")
    assert context_budget_for_model({"assistant_reasoning_probe": next_probe}, DEEPSEEK, "deepseek-flash")["profile"]["context_window_tokens"] == 1_048_576
    legacy = {"assistant_model_limits": {DEEPSEEK: {"deepseek-flash": {
        "context_budget_version": 2, "context_window_tokens": 100_000,
        "provider_metadata": model_metadata_capacity(DEEPSEEK, "deepseek-flash", {"context_window": 262_144})}}}}
    resolved = context_budget_for_model(legacy, DEEPSEEK, "deepseek-flash")
    assert resolved["profile"]["context_window_tokens"] == 1_048_576
    assert resolved["overrides"]["context_window_tokens"] == 100_000


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_failed_key_mutation_preserves_current_capacity(compute_ui, monkeypatch, action):
    monkeypatch.setattr(settings, "save_api_key", Mock())
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert bridge.saveAssistantApiKey("unchanged-fixture-a")
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_window": 262_144}}]) == 1
    if action == "delete":
        monkeypatch.setattr(settings, "delete_api_key", Mock(side_effect=OSError("fixture unavailable")))
        assert not bridge.clearAssistantApiKey()
    else:
        monkeypatch.setattr(settings, "save_api_key", Mock(side_effect=OSError("fixture unavailable")))
        assert not bridge.saveAssistantApiKey("unchanged-fixture-b")
    assert bridge.assistantContextBudget["profile"]["context_window_tokens"] == 262_144


def test_interrupted_candidate_result_does_not_erase_last_capacity_metadata(compute_ui, monkeypatch):
    monkeypatch.setattr(settings, "save_api_key", Mock())
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert bridge.saveAssistantApiKey("partial-fixture-key")
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_window": 262_144}}]) == 1
    bus = bridge._discovery_bus
    bus.publish(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {}, "evidence": "completion_request"}],
                cancelled=True, expected_fingerprint=bridge._assistant_credential_fingerprint)
    assert bridge.assistantContextBudget["profile"]["context_window_tokens"] == 262_144
    assert bridge.availableModels[0]["metadata"]["context_window"] == 262_144


def test_fresh_bus_reloads_capacity_only_for_same_key_without_renewing_timestamp(compute_ui, monkeypatch):
    monkeypatch.setattr(settings, "save_api_key", Mock())
    secret = "reload-fixture-key-a"
    compute_ui.read_key.return_value = secret
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert bridge.saveAssistantApiKey(secret)
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_window": 262_144}}]) == 1
    original = bridge._discovery_bus.reasoning_for(DEEPSEEK, "deepseek-flash")
    assert original is not None
    fresh_bus = settings.ModelDiscoveryBus()
    reloaded = fresh_bus.reasoning_for(DEEPSEEK, "deepseek-flash")
    assert reloaded is not None
    assert reloaded["capacity_metadata"]["observed_at"] == original["capacity_metadata"]["observed_at"]
    assert context_budget_for_model({"assistant_reasoning_probe": reloaded}, DEEPSEEK, "deepseek-flash")["profile"]["context_window_tokens"] == 262_144
    compute_ui.read_key.return_value = "reload-fixture-key-b"
    other_key = settings.ModelDiscoveryBus().reasoning_for(DEEPSEEK, "deepseek-flash")
    assert other_key is None
    assert context_budget_for_model({"assistant_reasoning_probe": other_key}, DEEPSEEK, "deepseek-flash")["profile"]["context_window_tokens"] == 1_048_576


def test_assistant_display_and_worker_share_credential_bound_capacity(compute_ui, monkeypatch):
    from types import SimpleNamespace
    from coat_sim.ui.assistant import AssistantBridge
    monkeypatch.setattr(settings, "save_api_key", Mock())
    monkeypatch.setattr(settings, "delete_api_key", Mock())
    compute_ui.read_key.return_value = "display-fixture-key"
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert bridge.saveAssistantApiKey("display-fixture-key")
    assert bridge._apply_discovered_model_metadata(DEEPSEEK, [{"id": "deepseek-flash", "metadata": {"context_window": 262_144}}]) == 1
    fake = SimpleNamespace(_config=dict(bridge._settings), activeModel="deepseek-flash", _discovered_models=[],
                           _discovery_bus=bridge._discovery_bus,
                           taskMode="chat", reasoningStrength="auto", _output_audio=False,
                           _reasoning_probe=lambda: bridge._discovery_bus.reasoning_for(DEEPSEEK, "deepseek-flash"))
    assistant = cast(AssistantBridge, fake)
    fake._context_capacity = lambda: AssistantBridge._context_capacity(assistant)
    assert fake._context_capacity() == 262_144
    worker = AssistantBridge._worker_config(assistant)
    assert worker["assistant_context_tokens"] == 262_144
    assert context_budget_for_model(worker, DEEPSEEK, "deepseek-flash")["profile"]["context_window_tokens"] == 262_144
    assert bridge.clearAssistantApiKey()
    assert fake._context_capacity() == 1_048_576
    assert AssistantBridge._worker_config(assistant)["assistant_context_tokens"] == 1_048_576


def test_manual_candidate_fallback_adopts_replaced_key_before_publication(compute_ui, monkeypatch):
    from tests.test_settings_coordinator_contract import Reply
    monkeypatch.setattr(settings, "save_api_key", Mock())
    bridge = compute_ui.open().bridge
    bridge.setAssistantConfig(False, DEEPSEEK, "manually-entered")
    assert bridge.saveAssistantApiKey("fallback-fixture-key-a")
    old_fingerprint = bridge._assistant_credential_fingerprint
    compute_ui.read_key.return_value = "fallback-fixture-key-b"
    coordinator = bridge._probe_coordinator
    coordinator._network = network = FakeNetwork()
    bridge._begin_models_probe()
    assert bridge._assistant_credential_fingerprint != old_fingerprint
    advance = coordinator._next_probe_request
    monkeypatch.setattr(coordinator, "_next_probe_request", Mock())
    reply = network.replies[-1]
    reply.status = 404
    reply.finished.emit()
    assert coordinator._probe_candidate_fallback
    # Supply an accepted synthetic completion reply without touching the budget
    # store or sending any POST. The real completion and finish handlers run.
    coordinator._probe_queue = []
    coordinator._probe_task = {"kind": "model", "model": "manually-entered", "output_parameter": "max_tokens"}
    candidate = Reply(200, {"choices": [{"message": {"content": "OK"}}]})
    coordinator._model_reply = candidate
    monkeypatch.setattr(coordinator, "_next_probe_request", advance)
    bridge._on_models_finished(candidate)
    assert not bridge.modelDiscoveryBusy
    assert [row["id"] for row in bridge.availableModels] == ["manually-entered"]
    assert bridge._assistant_credential_fingerprint == coordinator._probe_fingerprint
    assert bridge._discovery_bus.models_for_fingerprint(DEEPSEEK, old_fingerprint) == []


def test_key_replacement_in_other_window_cancels_late_captured_reply(compute_ui, monkeypatch):
    monkeypatch.setattr(settings, "save_api_key", Mock())
    first = compute_ui.open().bridge
    first.setAssistantConfig(False, DEEPSEEK, "deepseek-flash")
    assert first.saveAssistantApiKey("late-fixture-key-a")
    old_fingerprint = first._assistant_credential_fingerprint
    assert first.save()
    coordinator = first._probe_coordinator
    coordinator._network = network = FakeNetwork()
    first._begin_models_probe(automatic=True)
    reply = network.replies[-1]
    assert first.modelDiscoveryBusy
    second = compute_ui.open().bridge
    assert second.saveAssistantApiKey("late-fixture-key-b")
    assert not first.modelDiscoveryBusy
    reply.data = json.dumps({"data": [{"id": "deepseek-flash", "context_window": 262_144}]}).encode()
    reply.finished.emit()
    assert first._discovery_bus.models_for_fingerprint(DEEPSEEK, old_fingerprint) == []
    assert first.assistantContextBudget["profile"].get("evidence") != "server_declared_metadata"
    assert second.assistantContextBudget["profile"].get("evidence") != "server_declared_metadata"
