import pytest

from coat_sim.ui.control import UiControlError
from coat_sim.ui.project_exit_client import ProjectExitClient


def gate(decisions=None, *, committed=True):
    calls = []
    answers = iter(decisions or ["ready"])
    clock = [10.0]
    def prepare():
        calls.append("prepare")
        return next(answers)
    def commit():
        calls.append("commit")
        return committed
    def cancel():
        calls.append("cancel")
    client = ProjectExitClient("generation", prepare=prepare, commit=commit,
                               cancel=cancel, clock=lambda: clock[0])
    return client, calls, clock


def args(exit_id="exit", permit=""):
    return {"client_generation": "generation", "exit_id": exit_id, "permit_id": permit}


def test_pending_is_not_committed_and_ready_renewal_retains_permit():
    client, calls, clock = gate(["pending", "ready"])
    assert client.dispatch("lifecycle_prepare", args())["state"] == "pending"
    with pytest.raises(UiControlError):
        client.dispatch("lifecycle_commit", args())
    ready = client.dispatch("lifecycle_prepare", args())
    clock[0] += 90
    assert client.dispatch("lifecycle_prepare", args())["permit_id"] == ready["permit_id"]
    clock[0] += 90
    assert not client.expire()
    assert client.dispatch("lifecycle_commit", args(permit=ready["permit_id"]))["accepted"]
    assert client.dispatch("lifecycle_commit", args(permit=ready["permit_id"]))["accepted"]
    assert calls == ["prepare", "prepare", "commit"]


def test_expired_ready_lease_releases_freeze_and_cannot_be_reused():
    client, calls, clock = gate()
    permit = client.dispatch("lifecycle_prepare", args())["permit_id"]
    clock[0] += 121
    assert client.expire()
    assert calls == ["prepare", "cancel"]
    assert client.dispatch("lifecycle_prepare", args())["state"] == "declined"
    with pytest.raises(UiControlError):
        client.dispatch("lifecycle_commit", args(permit=permit))


def test_cancel_pending_and_refused_commit_release_decisions():
    client, calls, _clock = gate(["pending"])
    client.dispatch("lifecycle_prepare", args())
    assert client.dispatch("lifecycle_cancel", args())["cancelled"]
    assert calls == ["prepare", "cancel"]
    client, calls, _clock = gate(committed=False)
    permit = client.dispatch("lifecycle_prepare", args())["permit_id"]
    assert not client.dispatch("lifecycle_commit", args(permit=permit))["accepted"]
    assert calls == ["prepare", "commit", "cancel"]


def test_foreign_generation_exit_and_permit_are_rejected_without_cancellation():
    client, calls, _clock = gate()
    client.dispatch("lifecycle_prepare", args())
    for operation, arguments in (("lifecycle_prepare", args("other")),
                                 ("lifecycle_commit", args(permit="wrong")),
                                 ("lifecycle_cancel", args(permit="wrong")),
                                 ("lifecycle_prepare", {**args(), "client_generation": "stale"})):
        with pytest.raises(UiControlError):
            client.dispatch(operation, arguments)
    assert calls == ["prepare"]


def test_ready_renewal_revalidates_drafts_before_global_commit():
    events = []
    valid = [True]
    def commit() -> bool:
        events.append("commit")
        return True
    client = ProjectExitClient("generation", prepare=lambda: "ready",
                               commit=commit,
                               cancel=lambda: events.append("cancel"),
                               validate_ready=lambda: valid[0])
    args = {"client_generation": "generation", "exit_id": "test"}
    assert client.dispatch("lifecycle_prepare", args)["state"] == "ready"
    valid[0] = False
    result = client.dispatch("lifecycle_prepare", args)
    assert result["state"] == "declined"
    assert result["permit_id"] == ""
    assert events == ["cancel"]
