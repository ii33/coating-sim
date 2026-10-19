"""Prepared application exit keeps queued assistant work intact and frozen."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

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


def test_desktop_exit_freezes_countdowns_manual_submissions_and_late_snapshots():
    from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
    row = {"source": "compute", "estimate_id": "estimate", "options": [{"label": "Run"}],
           "optional": True, "deadline_ms": 1, "submitting": False}
    bridge = SimpleNamespace(_application_exit_frozen=True, _closed=False,
        _fetching=True, _decisions={"compute:estimate": row}, _busy_confirmation=set())
    DesktopAssistantBridge._tick_decisions(cast(DesktopAssistantBridge, bridge))
    DesktopAssistantBridge.submitDecision(cast(DesktopAssistantBridge, bridge), "compute:estimate", 0, "")
    DesktopAssistantBridge.confirm(cast(DesktopAssistantBridge, bridge), "estimate", True)
    DesktopAssistantBridge.cancelQueueJob(cast(DesktopAssistantBridge, bridge), "job")
    DesktopAssistantBridge.refreshQueue(cast(DesktopAssistantBridge, bridge))
    DesktopAssistantBridge._receive_snapshot(cast(DesktopAssistantBridge, bridge), {"jobs": [{"job_id": "late"}]}, "")
    assert not bridge._fetching
    assert row["submitting"] is False
    assert DesktopAssistantBridge._compute_release_blocked(cast(DesktopAssistantBridge, bridge), "estimate")


def test_cancelled_exit_restores_desktop_without_dropping_decisions():
    from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
    refreshed = []
    decisions = {"compute:queued": {"estimate_id": "queued"}}
    bridge = SimpleNamespace(_application_exit_frozen=True, _closed=False,
        _decisions=decisions, refreshQueue=lambda: refreshed.append(True))
    DesktopAssistantBridge.set_application_exit_frozen(cast(DesktopAssistantBridge, bridge), False)
    assert not bridge._application_exit_frozen and bridge._decisions is decisions
    assert refreshed == [True]


def test_background_queue_poll_does_not_start_scheduler(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from coat_sim import scheduler
    from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
    calls, snapshots = [], []
    class Client:
        def __init__(self, root):
            pass
        def snapshot_if_running(self, **arguments):
            calls.append(arguments)
            return None
        def snapshot(self, **arguments):
            raise AssertionError("background poll must not start scheduler")
    monkeypatch.setattr(scheduler, "SchedulerClient", Client)
    with ThreadPoolExecutor(max_workers=1) as executor:
        bridge = SimpleNamespace(_closed=False, _fetching=False, _cursor=0,
            _executor=executor, _application_exit_frozen=False,
            snapshotReady=SimpleNamespace(emit=lambda *args: snapshots.append(args)))
        DesktopAssistantBridge.refreshQueue(cast(DesktopAssistantBridge, bridge))
    assert calls == [{"after_event": 0, "limit": 100, "refresh_estimates": False}]
    assert snapshots[0][0]["_scheduler_offline"] and snapshots[0][1] == ""


def test_compute_exit_cancel_releases_services_and_restores_desktop(monkeypatch, tmp_path):
    from threading import Event
    from coat_sim.scheduler import lifecycle
    from coat_sim.ui.assistant_process_host import AssistantProcessHost
    calls: list[Any] = []
    results: list[dict[str, Any]] = []
    class Services:
        def __init__(self, runtime_root):
            assert runtime_root == tmp_path
        def prepare(self, exit_id):
            calls.append("prepare")
            return 1
        def renew(self):
            calls.append("renew")
        def cancel(self):
            calls.append("release")
    monkeypatch.setattr(lifecycle, "SchedulerExitGroup", Services)
    def forbid_commit(*args, **kwargs):
        raise AssertionError("Cancel must not enter GUI/service commit")
    desktop = SimpleNamespace(
        set_application_exit_frozen=lambda value: calls.append(("desktop", value)),
        set_application_exit_waiting=lambda value: None)
    assistant = SimpleNamespace(busy=False,
        set_application_exit_frozen=lambda value: calls.append(("assistant", value)))
    host = SimpleNamespace(_closed=False, _worker=None, _exit={"state": "idle"},
        _runtime_root=tmp_path, _chat_enabled=None,
        controller=SimpleNamespace(bridge=assistant, desktop=desktop, _chat=None),
        registry=SimpleNamespace(prepare_exit_activation=lambda: "client", request_exit_all=forbid_commit),
        _cancel_wait=Event(), exitProgress=SimpleNamespace(emit=lambda value: None),
        exitFinished=SimpleNamespace(emit=results.append),
        computeExitConfirmation=SimpleNamespace(emit=lambda item: item[1].set_result((False, None))))
    AssistantProcessHost.request_exit(cast(AssistantProcessHost, host))
    host._worker.join(timeout=3)
    assert not host._worker.is_alive()
    assert results == [{"state": "declined", "code": "compute_close_declined", "committed_clients": []}]
    AssistantProcessHost._exit_finished(cast(AssistantProcessHost, host), results[0])
    assert calls == [("assistant", True), ("desktop", True), "prepare", "release",
                     ("desktop", False), ("assistant", False)]


def test_exit_waits_for_admitted_desktop_work_before_service_discovery(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from coat_sim.scheduler import lifecycle
    from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
    from coat_sim.ui.assistant_process_host import AssistantProcessHost
    calls: list[Any] = []
    results: list[dict[str, Any]] = []
    started, release = Event(), Event()
    class Services:
        def __init__(self, _root):
            pass
        def prepare(self, exit_id):
            calls.append("discover")
            return 0
        def cancel(self):
            calls.append("release-fence")
    monkeypatch.setattr(lifecycle, "SchedulerExitGroup", Services)
    def admitted_submission():
        started.set()
        assert release.wait(3)
        calls.append("earlier-submission-finished")
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(admitted_submission)
        assert started.wait(1)
        desktop = SimpleNamespace(_closed=False, _executor=executor)
        desktop.set_application_exit_frozen = lambda value: DesktopAssistantBridge.set_application_exit_frozen(cast(DesktopAssistantBridge, desktop), value)
        host = SimpleNamespace(_closed=False, _worker=None, _exit={"state": "idle"},
            _runtime_root=tmp_path, _chat_enabled=None,
            controller=SimpleNamespace(bridge=SimpleNamespace(busy=False,
                set_application_exit_frozen=lambda _value: None), desktop=desktop, _chat=None),
            registry=SimpleNamespace(prepare_exit_activation=lambda: "client",
                request_exit_all=lambda *args, **kwargs: {"state": "completed"}),
            _cancel_wait=Event(), exitProgress=SimpleNamespace(emit=lambda _value: None),
            exitFinished=SimpleNamespace(emit=results.append))
        AssistantProcessHost.request_exit(cast(AssistantProcessHost, host))
        assert calls == [] and not results
        release.set()
        host._worker.join(timeout=3)
        assert not host._worker.is_alive()
        assert calls == ["earlier-submission-finished", "discover"]
        assert results == [{"state": "completed"}]
        assert host._exit_services is not None
        host._exit_services.cancel()


def test_successful_exit_releases_service_fence_after_provider_shutdown():
    from coat_sim.ui.assistant_process_host import AssistantProcessHost
    calls = []
    services = SimpleNamespace(cancel=lambda: calls.append("release-fence"))
    host = SimpleNamespace(_closed=False, _debug=None, _exit_services=services,
        _window_pool=SimpleNamespace(shutdown=lambda **_kwargs: calls.append("window-pool")),
        server=SimpleNamespace(close=lambda: calls.append("ipc")),
        controller=SimpleNamespace(shutdown=lambda: calls.append("provider-and-desktop")))
    AssistantProcessHost.close(cast(AssistantProcessHost, host))
    assert calls == ["window-pool", "ipc", "provider-and-desktop", "release-fence"]
    assert host._exit_services is None and host._closed
