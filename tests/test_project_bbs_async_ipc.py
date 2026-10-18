"""Deferred BBS socket replies and responsive runtime lifetime gates."""
from __future__ import annotations

from concurrent.futures import Future
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, Signal, QCoreApplication

from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.ui.control import UiControlError, UiControlServer
from coat_sim.ui.project_application_client import ProjectApplicationClient
from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle
from coat_sim.ui.project_runtime import ProjectRuntime
from tests.test_project_runtime_information import information_owner as _information_owner
from tests.test_project_bbs_ui import _wait

information_owner = _information_owner
pytestmark = [pytest.mark.qt, pytest.mark.contract]


class Bridge(QObject):
    project_runtime: Any
    project_lifecycle: Any
    _closing: bool
    _runtime_shutdown_requested: bool
    launchError = Signal(str)
    shutdownConfirmationRequested = Signal()

    def _uncommitted_project_view_draft(self, *_args):
        return False


@pytest.mark.parametrize("failure", [False, True])
def test_control_relay_defers_future_without_blocking_or_losing_errors(failure):
    QCoreApplication.instance() or QCoreApplication([])
    future: Future[dict] = Future()
    server = UiControlServer(lambda *_: future)
    envelope: dict[str, Any] = {"operation": "project_manage", "arguments": {}, "done": Event(), "expired": False}
    try:
        server._relay.receive(envelope)
        assert not envelope["done"].is_set()
        # A disconnected/expired receiver never cancels an accepted write.
        envelope["expired"] = True
        def finish():
            if failure:
                future.set_exception(UiControlError("stale_bbs_post", "stale"))
            else:
                future.set_result({"post": {"post_id": "committed"}})
        thread = Thread(target=finish)
        thread.start()
        thread.join(2)
        assert envelope["done"].wait(2)
        assert envelope["reply"]["ok"] is not failure
        if failure:
            assert envelope["reply"]["error"]["code"] == "stale_bbs_post"
        else:
            assert envelope["reply"]["result"]["post"]["post_id"] == "committed"
    finally:
        server._relay.deleteLater()


def test_assistant_bbs_io_status_runtime_tick_and_exit_cancel_remain_responsive(information_owner, monkeypatch):
    owner, stores, sessions = information_owner
    binding = sessions[0].binding
    bridge = Bridge()
    bridge.project_runtime = owner
    bridge._closing = False
    bridge._runtime_shutdown_requested = False
    bridge.project_lifecycle = None
    client = ProjectApplicationClient(bridge, SimpleNamespace(dispatch=lambda *_: {}), role="project_host")
    lifecycle = ProjectApplicationLifecycle(bridge, client.generation)
    bridge.project_lifecycle = lifecycle
    entered, release = Event(), Event()
    thread_ids = []
    original = ProjectBbsService.comment
    def slow(service, *args, **kwargs):
        thread_ids.append(get_ident())
        entered.set()
        assert release.wait(4)
        return original(service, *args, **kwargs)
    monkeypatch.setattr(ProjectBbsService, "comment", slow)
    payload = {"root": str(stores[0].project_file), "project_id": binding.project_id,
               "operation_id": "async-comment", "expected_revision": 0,
               "title": "助手评论", "body": "持久工程记录"}
    try:
        future = client.dispatch("project_manage", {"client_generation": client.generation,
            "session_generation": binding.generation, "project_operation": "bbs_comment", "arguments": payload})
        assert isinstance(future, Future) and entered.wait(2)
        assert len(thread_ids) == 1 and thread_ids[0] != get_ident()
        assert client.status("during-io")["project_sessions"][0]["project_id"] == binding.project_id
        assert lifecycle._prepare() == "pending"
        lifecycle._cancel()
        runtime = SimpleNamespace(owner=owner, application=client, _finished=True, _failed=False,
                                  _poll_history=lambda: True)
        ProjectRuntime._tick(cast(ProjectRuntime, runtime))
        ProjectRuntime.request_shutdown(cast(ProjectRuntime, SimpleNamespace(owner=owner, bridge=bridge)))
        assert not bridge._runtime_shutdown_requested
        assert not owner.closeProject(binding.project_id)
        release.set()
        _wait(lambda: not owner._bbs.busy)
        assert future.result()["post"]["author"] == "assistant"
    finally:
        release.set()
        client.stop()
        lifecycle._timer.stop()
        client.deleteLater()
        lifecycle.deleteLater()
