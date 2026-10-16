"""Discussion labels and explicit /plan alias retain the stored chat identity."""
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from coat_sim.ui.assistant import AssistantBridge

ROOT = Path(__file__).resolve().parents[1]


def test_plan_and_legacy_chat_route_to_same_read_only_mode():
    calls: list[str] = []
    bridge = SimpleNamespace(setTaskMode=calls.append)
    assert AssistantBridge._command(cast(AssistantBridge, bridge), "/plan") == ("", True)
    assert AssistantBridge._command(cast(AssistantBridge, bridge), "/chat explain this stack") == ("explain this stack", False)
    assert calls == ["chat", "chat"]


def test_operate_and_research_remain_explicit_distinct_choices():
    calls: list[str] = []
    bridge = SimpleNamespace(setTaskMode=calls.append)
    assert AssistantBridge._command(cast(AssistantBridge, bridge), "/operate save") == ("save", False)
    assert AssistantBridge._command(cast(AssistantBridge, bridge), "/research") == ("", True)
    assert calls == ["operate", "research"]


def test_completions_describe_discussion_without_changing_storage_identity():
    values = AssistantBridge.completions(cast(AssistantBridge, SimpleNamespace()), "/plan")
    assert len(values) == 1
    assert values[0]["insert"] == "/plan "
    assert "只读" in values[0]["detail"]
    for name in ("AssistantChatWindow.qml", "SettingsWindow.qml"):
        source = (ROOT / "coat_sim/ui/qml" / name).read_text(encoding="utf-8")
        assert '{text: "讨论", value: "chat"}' in source
        assert '{text: "聊天", value: "chat"}' not in source


def test_stop_discussion_does_not_claim_to_cancel_existing_calculation():
    status: list[str] = []
    events: list[Any] = []
    commands: list[dict] = []
    bridge = SimpleNamespace(busy=True, taskMode="chat", _request_id="read-only-turn",
        _run_coordinator=SimpleNamespace(begin_cancel=lambda: True), _pause_queue=lambda: None,
        _set_status=status.append, _record_event=lambda *value: events.append(value),
        _write_worker=commands.append)
    AssistantBridge.cancelTask(cast(AssistantBridge, bridge))
    assert "已有计算保持运行" in status[0]
    assert commands == [{"op": "cancel", "request_id": "read-only-turn"}]
    assert events[0][1]["text"] == status[0]
