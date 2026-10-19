"""Offline v2 budget contract through real Qt bridges and fake worker transport."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any, cast

import pytest

from tests import qt_test_guard  # noqa: F401  # isolate Qt before application imports
from tests.test_assistant_lifecycle_contract import FakeDiscoveryBus, FakeWorker
from coat_sim.agent.modelclient import (
    context_budget_for_model, model_token_limits, reasoning_capabilities, request_policy,
)
from coat_sim.agent.runtime import context_budget_fields, context_compaction_limits
from coat_sim.agent.storage import AssistantStore
from coat_sim.ui import assistant, assistant_credentials, settings
from coat_sim.ui.theme_service import ThemeService


pytestmark = [pytest.mark.contract, pytest.mark.qt]
API_URL = "https://example.invalid/v1"
MODEL = "offline-budget-model"


def budget_config():
    return settings.normalized_ui_settings({
        "assistant_enabled": True,
        "assistant_api_url": API_URL,
        "assistant_model": MODEL,
        "assistant_context_budget_version": 2,
        "assistant_model_limits": {API_URL: {MODEL: {
            "context_budget_version": 2,
            "context_window_tokens": 100_000,
            "reserved_output_tokens": 12_000,
            "compression_reserve_tokens": 2_000,
            "history_keep_tokens": 8_000,
            "compaction_threshold_percent": 63,
            "max_input_tokens": 80_000,
            "legacy_values": {"context_tokens": 99_000, "max_output_tokens": 9_000},
        }}},
    })


@pytest.fixture
def real_bridge(tmp_path, monkeypatch):
    """Keep the config path real; isolate storage, settings, themes and credentials."""
    config = budget_config()
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "ui_settings.json")
    monkeypatch.setattr(settings, "load_ui_settings", lambda: deepcopy(config))
    monkeypatch.setattr(assistant, "load_ui_settings", lambda: deepcopy(config))
    monkeypatch.setattr(ThemeService, "PACKAGE_ROOT", tmp_path / "themes")
    monkeypatch.setattr(ThemeService, "_instance", None)
    monkeypatch.setattr(assistant, "model_discovery_bus", FakeDiscoveryBus)
    monkeypatch.setattr(settings, "model_discovery_bus", FakeDiscoveryBus)

    def no_credentials(*_args, **_kwargs):
        raise AssertionError("offline UI budget tests must not access credentials")

    monkeypatch.setattr(settings, "read_api_key", no_credentials)
    monkeypatch.setattr(assistant_credentials, "read_api_key", no_credentials)
    connect = sqlite3.connect

    def isolated_connect(database, *args, **kwargs):
        assert Path(database).resolve().is_relative_to(tmp_path.resolve()), database
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", isolated_connect)
    monkeypatch.setattr(assistant, "AssistantStore", lambda: AssistantStore(tmp_path / "state.sqlite3"))
    # No material operation belongs to this flow; make its default destination private too.
    from coat_sim.core import material_business

    monkeypatch.setattr(material_business, "application_material_library",
                        lambda: material_business.ApplicationMaterialCatalog(tmp_path / "materials"))
    bridge = assistant.AssistantBridge()
    worker = FakeWorker(8100)
    bridge._run_coordinator.worker = worker
    monkeypatch.setattr(bridge, "_ensure_worker", lambda: None)
    try:
        yield bridge, worker
        assert not (tmp_path / "ui_settings.json").exists()
        assert not (tmp_path / "materials").exists()
    finally:
        bridge.shutdown()
        bridge._storage_pool.shutdown(wait=True, cancel_futures=True)
        bridge._prefs._shutdown_key_lookup()
        bridge._prefs._shutdown_probe_budget()
        bridge._prefs._shutdown_mcp()


def context_record(**overrides):
    return {
        "source": "main", "model": MODEL, "api_url": API_URL,
        "total_capacity": 100_000, "input_budget": 78_000,
        "current_tokens": 12_345, "actual": False, "basis": "offline estimate",
        **overrides,
    }


def usage_rows(store):
    with store._db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM usage ORDER BY id")]


def test_real_settings_and_empty_context_read_v2_capacity(real_bridge):
    bridge, _worker = real_bridge
    assert isinstance(bridge._prefs, settings.SettingsBridge)
    limits = model_token_limits(bridge._config, API_URL, MODEL)
    assert "context_tokens" not in limits
    assert limits["context_window_tokens"] == 100_000
    assert bridge._prefs.assistantContextTokens == 100_000
    assert bridge._prefs.assistantMaxInputTokens == 80_000
    assert bridge._prefs.assistantMaxOutputTokens == 0  # unknown hard output cap
    assert bridge._prefs.assistantCompactionThresholdPercent == 63
    assert cast(dict[str, object], bridge._prefs.assistantContextBudget)["budget_tokens"] == 78_000
    assert bridge._context_capacity() == 100_000
    reading = bridge.contextUsage
    assert reading["capacity"] == 100_000
    assert reading["input_budget"] == reading["used"] == 0
    assert reading["known"] is False


def test_new_message_reaches_worker_with_real_v2_config(real_bridge):
    bridge, worker = real_bridge
    original = deepcopy(bridge._config)
    assert bridge.sendMessage("offline token-budget regression")
    command, = worker.stdin.commands()
    assert command["op"] == "run"
    assert command["prompt"] == "offline token-budget regression"
    assert command["request_id"] == bridge._request_id
    assert command["session_id"] == bridge._session
    assert command["config"]["assistant_context_tokens"] == 100_000
    assert command["config"]["assistant_model_limits"] == original["assistant_model_limits"]
    assert bridge._config == original


def test_idle_manual_compaction_reaches_worker_and_repeat_is_gated(real_bridge):
    bridge, worker = real_bridge
    original = deepcopy(bridge._config)
    assert bridge.compactContext()
    command, = worker.stdin.commands()
    assert command["op"] == "compact"
    assert command["request_id"] == bridge._compaction_request
    assert command["session_id"] == bridge._session
    assert command["config"]["assistant_context_tokens"] == 100_000
    assert command["config"]["assistant_model_limits"] == original["assistant_model_limits"]
    assert bridge.compacting
    assert bridge.compactContext() is False
    assert len(worker.stdin.commands()) == 1
    bridge._on_worker_event({"process": worker.pid, "payload": {
        "event": "context_compaction", "request_id": command["request_id"],
        "state": "completed", "before_tokens": 40_000, "after_tokens": 15_000,
    }})
    assert not bridge.compacting
    assert bridge.compactContext()
    assert len(worker.stdin.commands()) == 2
    assert bridge._config == original


def test_busy_manual_compaction_uses_active_request_config(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    assert bridge.sendMessage("active")
    monkeypatch.setattr(bridge, "_worker_config", lambda: pytest.fail("busy compact must use active config"))
    assert bridge.compactContext()
    run, compact = worker.stdin.commands()
    assert compact == {"op": "compact", "request_id": run["request_id"], "session_id": bridge._session,
                       "assistant_project_target": None}


def test_runtime_context_budget_and_estimate_survive_ui_read(real_bridge):
    bridge, _worker = real_bridge
    policy = request_policy(reasoning_capabilities(API_URL, MODEL), "chat", "auto", "main",
                            limits=model_token_limits(bridge._config, API_URL, MODEL))
    fields = context_budget_fields(bridge._config, MODEL, policy)
    bridge._context = context_record(**fields)
    before = deepcopy(bridge._context)
    reading = bridge.contextUsage
    assert reading["capacity"] == 100_000
    assert reading["input_budget"] == 78_000
    assert reading["output_reserved"] == 12_000
    assert reading["used"] == 12_345
    assert reading["ratio"] == pytest.approx(12_345 / 78_000)
    assert reading["known"] is True
    assert reading["actual"] is False
    assert reading["basis"] == "offline estimate"
    assert bridge._context == before


@pytest.mark.parametrize("capacity_fields, expected", [
    ({"total_capacity": 90_000, "capacity": 80_000}, 90_000),
    ({"total_capacity": 0, "capacity": 80_000}, 0),
    ({"total_capacity": None, "capacity": 80_000}, 0),
    ({"capacity": 80_000}, 80_000),
    ({"capacity": 0}, 0),
])
def test_reported_capacity_is_lazy_and_preserves_zero(real_bridge, monkeypatch, capacity_fields, expected):
    bridge, _worker = real_bridge
    bridge._context = context_record()
    bridge._context.pop("total_capacity")
    bridge._context.update(capacity_fields)
    monkeypatch.setattr(bridge, "_context_capacity", lambda: pytest.fail("reported capacity must not recompute"))
    assert bridge.contextUsage["capacity"] == expected


def test_unreported_capacity_uses_real_v2_fallback(real_bridge):
    bridge, _worker = real_bridge
    bridge._context = context_record()
    bridge._context.pop("total_capacity")
    assert bridge.contextUsage["capacity"] == 100_000


@pytest.mark.parametrize("input_budget", [0, None, "missing"])
def test_zero_or_missing_request_budget_remains_unknown(real_bridge, monkeypatch, input_budget):
    bridge, _worker = real_bridge
    bridge._context = context_record()
    if input_budget == "missing":
        bridge._context.pop("input_budget")
    else:
        bridge._context["input_budget"] = input_budget
    before = deepcopy(bridge._context)
    # A reported capacity alone must never invent a request input budget.
    monkeypatch.setattr(assistant, "model_token_limits", lambda *_: pytest.fail("must not fabricate budget"))
    reading = bridge.contextUsage
    assert reading["capacity"] == 100_000
    assert reading["used"] == 12_345
    assert reading["input_budget"] == reading["ratio"] == 0
    assert reading["known"] is False
    assert "上限未知" in reading["label"]
    assert bridge._context == before


def test_unknown_runtime_scheduler_fallback_is_not_displayed_as_request_hard_limit(real_bridge):
    bridge, _worker = real_bridge
    bridge._config["assistant_model_limits"] = {}
    fields = context_budget_fields(bridge._config, MODEL, {})
    assert fields["scheduler_input_budget"] == 62_720
    assert fields["fallback_input_tokens"] == 64_000
    bridge._context = context_record(**fields)
    reading = bridge.contextUsage
    assert reading["capacity"] == 0
    assert reading["input_budget"] == 0
    assert reading["known"] is False
    assert reading["fallback_used"] is True
    assert reading["scheduler_input_budget"] == 62_720


def test_known_request_input_budget_does_not_require_known_total_capacity(real_bridge):
    bridge, _worker = real_bridge
    bridge._context = context_record(total_capacity=0, input_budget=50_000)
    reading = bridge.contextUsage
    assert reading["capacity"] == 0
    assert reading["input_budget"] == 50_000
    assert reading["known"] is True
    assert reading["ratio"] == pytest.approx(12_345 / 50_000)


def test_unknown_model_zero_capacity_remains_valid(real_bridge):
    bridge, worker = real_bridge
    bridge._config["assistant_model_limits"] = {}
    bridge._prefs._settings["assistant_model_limits"] = {}
    assert bridge._context_capacity() == 0
    assert bridge._prefs.assistantContextTokens == 0
    assert bridge.contextUsage["capacity"] == 0
    assert bridge.contextUsage["known"] is False
    assert bridge.sendMessage("unknown-capacity model")
    assert worker.stdin.commands()[0]["config"]["assistant_context_tokens"] == 0


@pytest.mark.parametrize("identity", [{"model": "different-model"}, {"api_url": "https://other.invalid/v1"},
                                      {"source": "summary"}, {"api_url": ""}])
def test_context_from_different_identity_is_not_reused(real_bridge, identity):
    bridge, _worker = real_bridge
    bridge._context = context_record(**identity)
    assert bridge.contextUsage["capacity"] == 100_000
    assert bridge.contextUsage["used"] == 0
    assert bridge.contextUsage["known"] is False


def test_missing_v2_capacity_key_is_not_silently_unknown(real_bridge, monkeypatch):
    bridge, _worker = real_bridge
    monkeypatch.setattr(assistant, "model_token_limits", lambda *_: {})
    monkeypatch.setattr(settings, "model_token_limits", lambda *_: {})
    with pytest.raises(KeyError, match="context_window_tokens"):
        bridge._context_capacity()
    with pytest.raises(KeyError, match="context_window_tokens"):
        _ = bridge._prefs.assistantContextTokens


def test_worker_budget_preserves_output_and_auto_compaction_semantics(real_bridge):
    bridge, worker = real_bridge
    assert bridge.sendMessage("verify shared budget")
    config = worker.stdin.commands()[0]["config"]
    limits = model_token_limits(config, API_URL, MODEL)
    policy = request_policy(reasoning_capabilities(API_URL, MODEL), "chat", "auto", "main", limits=limits)
    thresholds = context_compaction_limits(config, API_URL, MODEL, policy)
    assert policy["context_tokens"] == 100_000  # valid internal adapter field
    assert policy["desired_output"] == 12_000
    assert policy["max_output_tokens"] == 0
    assert thresholds["available_input"] == 78_000
    assert thresholds["trigger"] == 49_140
    assert thresholds["target"] == 35_100
    assert thresholds["history_keep_tokens"] == 8_000
    assert thresholds["summary_output_tokens"] == 2_000
    assert thresholds["effective_input_known"] is True


def test_million_default_and_unknown_scheduler_budget_are_unchanged():
    known = context_compaction_limits({}, "https://api.deepseek.com/v1", "deepseek-flash")
    assert known["context_budget"]["limits"]["context_window_tokens"] == 1_000_000
    assert known["threshold_percent"] == 80
    assert known["trigger"] == known["available_input"] * 80 // 100
    unknown = context_budget_for_model({}, API_URL, MODEL)
    assert model_token_limits({}, API_URL, MODEL)["context_window_tokens"] == 0
    assert unknown["fallback_input_tokens"] == 64_000
    assert unknown["budget_tokens"] == 62_720
    assert unknown["budget_known"] is False


def test_capacity_reads_submission_and_compaction_do_not_rewrite_usage(real_bridge):
    bridge, _worker = real_bridge
    for estimated, input_tokens, output_tokens in ((False, 123, 45), (True, 300, 80)):
        reservation, _output = bridge._store.reserve_request(bridge._config, input_tokens, output_tokens)
        bridge._store.settle_request(reservation, bridge._session, MODEL, input_tokens, output_tokens,
                                    estimated=estimated, cached_input_tokens=0 if not estimated else None)
    before = usage_rows(bridge._store)
    bridge._usage_summary = bridge._store.usage_summary(bridge._config)
    bridge._session_usage = bridge._store.session_usage(bridge._session)
    summary = bridge.tokenSummary
    assert "有 usage 时按服务器返回值统计" in summary
    assert "按保守估算记账" in summary
    assert bridge._prefs.assistantContextTokens == 100_000
    _ = bridge.contextUsage
    assert bridge.compactContext()
    assert bridge.sendMessage("preserve accounted history")
    assert usage_rows(bridge._store) == before
    assert bridge.tokenSummary == summary
    assert bridge._config["assistant_model_limits"][API_URL][MODEL]["legacy_values"] == {
        "context_tokens": 99_000, "max_output_tokens": 9_000,
    }


def test_completed_turn_omits_automatic_budget_footer_but_keeps_usage(real_bridge):
    bridge, _worker = real_bridge
    usage = {"input_tokens": 123, "output_tokens": 45,
             "cached_input_tokens": 12, "estimated_requests": 0}
    message: dict[str, Any] = {"id": "synthetic-round", "role": "assistant", "content": "已完成用户任务。",
               "request_id": "synthetic-request", "metadata": {
                   "status": "completed", "elapsed_s": 2.5, "usage": usage}}
    bridge._messages.append(message)
    bridge._stable_turns = None
    view = bridge.turns[-1]
    assert view["footer"] == "完成 · 2.5 秒"
    assert bridge._copy_message_turn(message)["footer"] == "完成 · 2.5 秒"
    assert message["metadata"]["usage"] == usage
    assert "Token" in bridge.tokenSummary


def test_context_usage_tooltip_distinguishes_budget_from_capacity():
    qml_path = Path(assistant.__file__).parent / "qml" / "AssistantChatWindow.qml"
    source = qml_path.read_text(encoding="utf-8")
    assert "assistant.contextUsage.known" in source
    assert "有效输入预算来自当前请求的运行时读数。" in source
    assert "尚无当前请求的有效输入预算读数；总窗口容量见上方。" in source
    assert "分项是请求前估算；服务器实际值可能不同，不以分项相加冒充实际消耗。" in source
    assert "上下文上限来自设置或当前模型探测。" not in source
    assert "当前服务没有提供上下文上限；可在设置中填写。" not in source
