"""Bearer credentials must not be inherited by compute workers."""

from coat_sim.scheduler.remote_server import _worker_env_without_token


def test_child_environment_removes_bearer_value_without_mutating_parent():
    token = "ephemeral-test-operator-token"
    parent = {"PATH": "test-bin", "SERVICE_TOKEN": token,
              "ANOTHER_COPY": token, "OTHER_TOKEN": "different-value"}
    child = _worker_env_without_token(parent, token)
    assert child == {"PATH": "test-bin", "OTHER_TOKEN": "different-value"}
    assert parent["SERVICE_TOKEN"] == token


def test_cli_consumes_token_environment_before_service_or_probe(tmp_path, monkeypatch):
    from coat_sim.scheduler import remote_server

    token = "ephemeral-test-operator-token"
    monkeypatch.setenv("COAT_REMOTE_TEST_TOKEN", token)
    seen = {}

    class Service:
        def __init__(self, **kwargs):
            seen["token"] = kwargs["token"]
            seen["environment_absent_at_init"] = "COAT_REMOTE_TEST_TOKEN" not in remote_server.os.environ
            self.scheduler = type("Scheduler", (), {"stopping": type("Event", (), {"set": lambda self: None})()})()

        def run(self):
            seen["environment_absent_at_run"] = "COAT_REMOTE_TEST_TOKEN" not in remote_server.os.environ

    monkeypatch.setattr(remote_server, "RemoteComputeService", Service)
    monkeypatch.setattr(remote_server.signal, "signal", lambda *_: None)
    assert remote_server.main(["--data-root", str(tmp_path), "--insecure-loopback",
                               "--token-env", "COAT_REMOTE_TEST_TOKEN"]) == 0
    assert seen == {"token": token, "environment_absent_at_init": True,
                    "environment_absent_at_run": True}
