"""Global exit prepares local services and GUI drafts before any commit."""

import pytest

from coat_sim import assistant_process as module
from coat_sim.assistant_process import ApplicationClientRegistry
from tests.test_assistant_process import _client, _install_clients, _patch_status

pytestmark = pytest.mark.contract


class Services:
    def __init__(self, calls):
        self.calls = calls
        self.alive = True

    def prepare(self, exit_id):
        self.calls.append("services:prepare")
        return 0

    def renew(self):
        self.calls.append("services:renew")

    def commit(self):
        self.calls.append("services:commit")

    def pending(self):
        self.calls.append("services:pending")
        pending, self.alive = self.alive, False
        return ["exact-service-generation"] if pending else []

    def cancel(self):
        self.calls.append("services:release")


@pytest.mark.parametrize("declined", [False, True])
def test_services_and_dirty_gui_use_one_prepare_commit_boundary(monkeypatch, declined):
    registry = ApplicationClientRegistry()
    client = _client("editor", 99511)
    _install_clients(registry, client)
    _patch_status(monkeypatch)
    calls: list[str] = []
    services = Services(calls)
    alive = {"editor": True}
    monkeypatch.setattr(module, "_process_identity", lambda _pid:
                        ("alive", client.process_start) if alive["editor"] else ("dead", None))
    monkeypatch.setattr(module.time, "sleep", lambda _duration: None)

    def call(_client, operation, **_arguments):
        calls.append(operation)
        if operation == "lifecycle_prepare":
            return {"generation": client.generation,
                    "state": "declined" if declined else "ready", "permit_id": "permit"}
        if operation == "lifecycle_commit":
            alive["editor"] = False
            return {"generation": client.generation, "accepted": True}
        if operation == "lifecycle_cancel":
            return {"cancelled": True}
        raise AssertionError(operation)

    monkeypatch.setattr(registry, "_call", call)
    result = registry.request_exit_all("exit", services=services)
    assert calls[0] == "services:prepare" and calls[-1] == "services:release"
    if declined:
        assert result["state"] == "declined"
        assert "services:commit" not in calls and "lifecycle_commit" not in calls
        assert "lifecycle_cancel" in calls
    else:
        assert result["state"] == "completed"
        assert calls.index("services:commit") > calls.index("lifecycle_prepare")
        assert calls.index("lifecycle_commit") > calls.index("services:commit")
        assert calls.count("services:pending") == 2
    assert not registry._exiting


def test_service_commit_uncertainty_cannot_report_success(monkeypatch):
    calls: list[str] = []
    services = Services(calls)
    def fail():
        raise RuntimeError("connection dropped after request")
    monkeypatch.setattr(services, "commit", fail)
    registry = ApplicationClientRegistry()
    result = registry.request_exit_all("uncertain", services=services)
    assert result["state"] == "failed" and result["partial_commit"]
    assert calls[-1] == "services:release"
    assert not registry._exiting
