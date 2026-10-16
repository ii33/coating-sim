"""Prepared application exit keeps queued assistant work intact and frozen."""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from coat_sim.ui.assistant import AssistantBridge

pytestmark = pytest.mark.contract


def test_exit_freeze_blocks_gui_debug_and_automatic_admissions(monkeypatch):
    from coat_sim.agent import development
    monkeypatch.setattr(development, "require_development", lambda: None)
    messages: list[str] = []
    queue = [{"id": "queued-one", "prompt": "Preserve this"}]
    bridge = cast(AssistantBridge, SimpleNamespace(_application_exit_frozen=True,
                             _pending_messages=queue, _set_status=messages.append))
    assert AssistantBridge._prepare_message(bridge, "New message") == (None, False)
    assert AssistantBridge._start_message(bridge, {"prompt": "Queued"}) is False
    AssistantBridge._drain_queue(bridge)
    assert bridge._pending_messages is queue and queue[0]["id"] == "queued-one"
    assert AssistantBridge.steerMessage(bridge, "New steering") is False
    assert AssistantBridge.sendDecisionSupplement(bridge, "New supplement") is False
    assert AssistantBridge.debug_submit(bridge, "Debug request")["accepted"] is False
    assert AssistantBridge.debug_steer(bridge, "Debug steering")["accepted"] is False
    assert messages


def test_cancelled_exit_unfreezes_without_discarding_queued_messages(monkeypatch):
    import coat_sim.ui.assistant as assistant
    scheduled = []
    monkeypatch.setattr(assistant.QTimer, "singleShot", lambda _delay, callback: scheduled.append(callback))
    queue = [{"id": "queued-one"}]
    def callback():
        pass
    bridge = cast(AssistantBridge, SimpleNamespace(_application_exit_frozen=True, _closed=False,
                             _pending_messages=queue, _drain_queue=callback))
    AssistantBridge.set_application_exit_frozen(bridge, False)
    assert bridge._application_exit_frozen is False
    assert bridge._pending_messages is queue
    assert scheduled == [callback]
