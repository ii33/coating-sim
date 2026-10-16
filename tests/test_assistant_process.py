"""Application-registry lifecycle contracts for the shared assistant process."""
from __future__ import annotations

import json
import hmac
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace

import pytest

from coat_sim import assistant_process as assistant_process_module
from coat_sim.assistant_process import AssistantProcessClient, ApplicationClientRegistry, _Client
from coat_sim.ui.control import UiControlError


pytestmark = pytest.mark.contract


def _client(identity: str, pid: int) -> _Client:
    return _Client(
        client_id=identity,
        endpoint={"pid": pid, "root": "/runtime", "role": "launcher"},
        generation=f"generation-{identity}",
        process_start=f"start-{pid}",
        role="launcher",
        projects=[],
    )


def _install_clients(registry: ApplicationClientRegistry, *clients: _Client) -> None:
    registry._clients.update({client.client_id: client for client in clients})


def _patch_status(monkeypatch):
    monkeypatch.setattr(ApplicationClientRegistry, "_status", classmethod(lambda _cls, _client: {}))


class _AuthenticatedLifecycleEndpoint:
    """Tiny real HTTP endpoint used to exercise UiControlClient authentication."""

    def __init__(self, root, *, pid, process_start, generation, role="launcher",
                 project_sessions=None, response_overrides=None):
        self.root = Path(root).resolve()
        self.pid = pid
        self.process_start = process_start
        self.generation = generation
        self.role = role
        self.project_sessions = list(project_sessions or [])
        self.response_overrides = dict(response_overrides or {})
        self.token = "test-token-" + "a" * 40
        self.nonces: list[str | None] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                pass

            def do_POST(self):
                if (self.path != "/call" or not hmac.compare_digest(
                        self.headers.get("X-Coat-Token", ""), owner.token)):
                    self.send_error(403)
                    return
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    arguments = request.get("arguments", {})
                    nonce = arguments.get("nonce")
                    owner.nonces.append(nonce)
                    result = {
                        "application": "coat_sim", "nonce": nonce,
                        "pid": owner.pid, "process_start": owner.process_start,
                        "generation": owner.generation, "role": owner.role,
                        "project_sessions": owner.project_sessions,
                    }
                    result.update(owner.response_overrides)
                    reply = {"ok": True, "result": result}
                except Exception as exc:  # pragma: no cover - test endpoint diagnostic
                    reply = {"ok": False, "error": {"code": "test_endpoint_error", "message": str(exc)}}
                payload = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = {
            "protocol": 1, "host": "127.0.0.1", "port": self.server.server_port,
            "token": self.token, "pid": self.pid, "root": str(self.root),
            "role": self.role, "window_type": None, "parent_pid": None,
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def test_exit_prepares_every_client_before_committing_any(monkeypatch):
    registry = ApplicationClientRegistry()
    first, second = _client("first", 1101), _client("second", 1102)
    _install_clients(registry, first, second)
    _patch_status(monkeypatch)
    states: dict[int, tuple[str, str | None]] = {first.endpoint["pid"]: ("alive", first.process_start),
              second.endpoint["pid"]: ("alive", second.process_start)}
    monkeypatch.setattr(assistant_process_module, "_process_identity", lambda pid: states[pid])
    calls = []

    def call(client, operation, **arguments):
        calls.append((client.client_id, operation))
        if operation == "lifecycle_prepare":
            return {"generation": client.generation, "state": "ready",
                    "permit_id": f"permit-{client.client_id}"}
        if operation == "lifecycle_commit":
            states[client.endpoint["pid"]] = ("dead", None)
            return {"generation": client.generation, "accepted": True}
        if operation == "lifecycle_cancel":
            return {"generation": client.generation, "cancelled": True}
        raise AssertionError(operation)

    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    result = registry.request_exit_all("exit-1")

    assert result == {"state": "completed", "committed_clients": ["first", "second"]}
    assert calls == [
        ("first", "lifecycle_prepare"),
        ("second", "lifecycle_prepare"),
        ("first", "lifecycle_prepare"),  # refresh ready permit before commits
        ("second", "lifecycle_prepare"),
        ("first", "lifecycle_commit"),
        ("second", "lifecycle_commit"),
    ]
    assert registry._exiting is False


def test_declined_client_cancels_prepared_permits_without_partial_commit(monkeypatch):
    registry = ApplicationClientRegistry()
    first, second = _client("first", 1201), _client("second", 1202)
    _install_clients(registry, first, second)
    _patch_status(monkeypatch)
    monkeypatch.setattr(assistant_process_module, "_process_identity",
                        lambda pid: ("alive", f"start-{pid}"))
    calls = []

    def call(client, operation, **arguments):
        calls.append((client.client_id, operation, arguments.get("permit_id")))
        if operation == "lifecycle_prepare":
            return {"generation": client.generation,
                    "state": "ready" if client is first else "declined",
                    "permit_id": "permit-first" if client is first else ""}
        if operation == "lifecycle_cancel":
            return {"cancelled": True}
        raise AssertionError(f"unexpected {operation}")

    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    result = registry.request_exit_all("exit-declined")

    assert result == {"state": "declined", "code": "client_close_declined",
                      "client_id": "second", "committed_clients": []}
    assert calls == [
        ("first", "lifecycle_prepare", None),
        ("second", "lifecycle_prepare", None),
        ("first", "lifecycle_cancel", "permit-first"),
        ("second", "lifecycle_cancel", ""),
    ]
    assert registry._exiting is False


def test_wrong_generation_challenge_fails_closed_and_reports_no_commit(monkeypatch):
    registry = ApplicationClientRegistry()
    client = _client("first", 1301)
    _install_clients(registry, client)
    _patch_status(monkeypatch)
    monkeypatch.setattr(assistant_process_module, "_process_identity",
                        lambda pid: ("alive", client.process_start))
    calls = []

    def call(_client, operation, **arguments):
        calls.append((operation, arguments.get("permit_id")))
        if operation == "lifecycle_prepare":
            return {"generation": "different-generation", "state": "ready", "permit_id": "bad-permit"}
        if operation == "lifecycle_cancel":
            return {"cancelled": True}
        raise AssertionError(f"unexpected {operation}")

    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    result = registry.request_exit_all("exit-stale")

    assert result["state"] == "failed"
    assert result["code"] == "stale_permit"
    assert result["committed_clients"] == []
    assert result["partial_commit"] is False
    assert calls == [("lifecycle_prepare", None), ("lifecycle_cancel", "")]
    assert registry._exiting is False


def test_commit_failure_is_explicitly_reported_as_partial(monkeypatch):
    registry = ApplicationClientRegistry()
    first, second = _client("first", 1401), _client("second", 1402)
    _install_clients(registry, first, second)
    _patch_status(monkeypatch)
    states: dict[int, tuple[str, str | None]] = {first.endpoint["pid"]: ("alive", first.process_start),
              second.endpoint["pid"]: ("alive", second.process_start)}
    monkeypatch.setattr(assistant_process_module, "_process_identity", lambda pid: states[pid])
    calls = []

    def call(client, operation, **arguments):
        calls.append((client.client_id, operation))
        if operation == "lifecycle_prepare":
            return {"generation": client.generation, "state": "ready",
                    "permit_id": f"permit-{client.client_id}"}
        if operation == "lifecycle_commit" and client is first:
            states[client.endpoint["pid"]] = ("dead", None)
            return {"generation": client.generation, "accepted": True}
        if operation == "lifecycle_commit":
            raise UiControlError("shutdown_refused", "Second client refused its prepared exit")
        if operation == "lifecycle_cancel":
            return {"cancelled": True}
        raise AssertionError(operation)

    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    result = registry.request_exit_all("exit-partial")

    assert result["state"] == "failed"
    assert result["code"] == "shutdown_refused"
    assert result["committed_clients"] == ["first"]
    assert result["partial_commit"] is True
    assert ("first", "lifecycle_prepare") in calls
    assert ("second", "lifecycle_prepare") in calls
    assert calls.index(("second", "lifecycle_prepare")) < calls.index(("first", "lifecycle_commit"))
    assert calls[-2:] == [("second", "lifecycle_commit"), ("second", "lifecycle_cancel")]
    assert registry._exiting is False


def test_ready_permits_are_renewed_while_another_client_is_pending(monkeypatch):
    registry = ApplicationClientRegistry()
    first, second = _client("first", 1501), _client("second", 1502)
    _install_clients(registry, first, second)
    _patch_status(monkeypatch)
    states: dict[int, tuple[str, str | None]] = {first.endpoint["pid"]: ("alive", first.process_start),
              second.endpoint["pid"]: ("alive", second.process_start)}
    monkeypatch.setattr(assistant_process_module, "_process_identity", lambda pid: states[pid])
    clock = {"now": 0.0}
    monkeypatch.setattr(assistant_process_module, "time", SimpleNamespace(
        monotonic=lambda: clock["now"],
        sleep=lambda duration: clock.__setitem__("now", clock["now"] + max(10.0, duration)),
    ))
    pending_attempts = {"second": 0}
    calls = []

    def call(client, operation, **arguments):
        calls.append((client.client_id, operation))
        if operation == "lifecycle_prepare":
            if client is first:
                return {"generation": first.generation, "state": "ready", "permit_id": "permit-first"}
            pending_attempts["second"] += 1
            if pending_attempts["second"] < 3:
                return {"generation": second.generation, "state": "pending"}
            return {"generation": second.generation, "state": "ready", "permit_id": "permit-second"}
        if operation == "lifecycle_commit":
            states[client.endpoint["pid"]] = ("dead", None)
            return {"generation": client.generation, "accepted": True}
        if operation == "lifecycle_cancel":
            return {"cancelled": True}
        raise AssertionError(operation)

    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    result = registry.request_exit_all("exit-with-renewal")

    assert result == {"state": "completed", "committed_clients": ["first", "second"]}
    assert calls == [
        ("first", "lifecycle_prepare"),
        ("second", "lifecycle_prepare"),
        ("second", "lifecycle_prepare"),
        ("first", "lifecycle_prepare"),  # renew before the second poll
        ("second", "lifecycle_prepare"),
        ("first", "lifecycle_prepare"),  # final renewal before first commit
        ("second", "lifecycle_prepare"),
        ("first", "lifecycle_commit"),
        ("second", "lifecycle_commit"),
    ]


def test_registry_authenticates_clients_and_stale_unregister_preserves_current_lease(tmp_path):
    state, process_start = assistant_process_module._process_identity(os.getpid())
    assert state == "alive" and process_start
    project = {"project_id": "project-1", "root": str(tmp_path / "project"),
               "project_file": str(tmp_path / "project" / "Study.coatproj"),
               "generation": "project-generation", "name": "Study"}
    endpoint = _AuthenticatedLifecycleEndpoint(
        tmp_path / "client-runtime", pid=os.getpid(), process_start=process_start,
        generation="client-generation", project_sessions=[project],
    )
    registry = ApplicationClientRegistry()
    try:
        registered = registry.register_client(
            endpoint.endpoint, generation="client-generation", process_start=process_start,
        )
        assert registered["state"] == "alive"
        assert registered["role"] == "launcher"
        assert registered["pid"] == os.getpid()
        assert registered["project_sessions"] == [project]
        assert set(registered) == {
            "client_id", "pid", "generation", "process_start", "role", "state", "project_sessions",
        }
        assert endpoint.token not in json.dumps(registered)
        assert endpoint.nonces and all(isinstance(nonce, str) and nonce for nonce in endpoint.nonces)

        assert registry.unregister_client(registered["client_id"], "stale-generation") is False
        listed = registry.list_clients()
        assert len(listed) == 1 and listed[0]["client_id"] == registered["client_id"]
        assert endpoint.token not in json.dumps(listed)
        assert registry.unregister_client(registered["client_id"], "client-generation") is True
        assert registry.list_clients() == []
    finally:
        endpoint.close()


@pytest.mark.parametrize(("field", "bad_value"), [
    ("nonce", "stale-nonce"),
    ("generation", "replacement-generation"),
    ("process_start", "replacement-process"),
])
def test_registry_rejects_failed_client_identity_challenge(tmp_path, field, bad_value):
    state, process_start = assistant_process_module._process_identity(os.getpid())
    assert state == "alive" and process_start
    endpoint = _AuthenticatedLifecycleEndpoint(
        tmp_path / field, pid=os.getpid(), process_start=process_start,
        generation="client-generation", response_overrides={field: bad_value},
    )
    registry = ApplicationClientRegistry()
    try:
        with pytest.raises(UiControlError) as caught:
            registry.register_client(
                endpoint.endpoint, generation="client-generation", process_start=process_start,
            )
        assert caught.value.code == "process_identity_mismatch"
        assert registry.list_clients() == []
        assert len(endpoint.nonces) == 1
    finally:
        endpoint.close()


@pytest.mark.qt
@pytest.mark.skipif(os.name == "nt", reason="The offscreen subprocess integration has only been validated on POSIX.")
def test_real_offscreen_assistant_is_singleton_and_outlives_client_parent(tmp_path):
    """Launch two real module processes; one owns the UI and the other exits quietly."""
    root = Path(__file__).resolve().parents[1]
    sandbox = tmp_path / "assistant-test-support"
    sandbox.mkdir()
    runtime = tmp_path / "runtime"
    isolated_state = tmp_path / "isolated-state"
    isolated_state.mkdir()
    cache = isolated_state / "cache"
    cache.mkdir()
    (sandbox / "sitecustomize.py").write_text(
        """import os
from pathlib import Path

if os.environ.get('COAT_TEST_ASSISTANT_QA') == '1':
    state = Path(os.environ['COAT_TEST_ASSISTANT_STATE']).resolve()
    import coat_sim.ui.settings as settings
    settings.SETTINGS_PATH = state / 'ui_settings.json'
    configuration = settings.normalized_ui_settings({
        'assistant_enabled': True,
        'assistant_api_url': '',
        'assistant_model': '',
        'assistant_notification_sound': False,
    })
    settings.load_ui_settings = lambda: dict(configuration)
    settings.save_ui_settings = lambda values: dict(values)

    import coat_sim.agent.storage as storage
    _store_init = storage.AssistantStore.__init__
    _default_store_path = storage.STATE_PATH
    def _isolated_store_init(self, path=_default_store_path):
        selected = state / 'assistant.sqlite3' if Path(path) == _default_store_path else path
        _store_init(self, selected)
    storage.AssistantStore.__init__ = _isolated_store_init

    import coat_sim.ui.assistant_desktop as desktop
    desktop.CURSOR_PATH = state / 'desktop-cursor.json'
    desktop.QSoundEffect = None
    desktop.DesktopAssistantBridge.refreshQueue = lambda self: None
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({
        "QT_QPA_PLATFORM": "offscreen",
        "COAT_TEST_ASSISTANT_QA": "1",
        "COAT_TEST_ASSISTANT_STATE": str(isolated_state),
        "COAT_ASSISTANT_RUNTIME_ROOT": str(runtime),
        "XDG_CACHE_HOME": str(cache),
        "QT_QUICK_BACKEND": "software",
        "PYTHONFAULTHANDLER": "1",
        "PYTHONPATH": os.pathsep.join((str(sandbox), str(root), environment.get("PYTHONPATH", ""))),
    })
    command = [sys.executable, "-m", "coat_sim.assistant_process", "--runtime-root", str(runtime)]
    log_paths = [tmp_path / "assistant-one.log", tmp_path / "assistant-two.log"]
    logs = [path.open("wb") for path in log_paths]
    processes = []
    winner = None
    client = AssistantProcessClient(runtime, application_root=root, timeout=2.0)

    def request_exit_and_wait(process, timeout=20.0):
        try:
            client.request("request_exit_all")
        except Exception:
            pass
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    try:
        # These are independent real module starts, with the same per-user lock.
        first = subprocess.Popen(command, cwd=root, env=environment,
                                 stdout=logs[0], stderr=subprocess.STDOUT)
        processes.append(first)
        second = subprocess.Popen(command, cwd=root, env=environment,
                                  stdout=logs[1], stderr=subprocess.STDOUT)
        processes.append(second)

        deadline = time.monotonic() + 45.0
        winner = None
        loser = None
        while time.monotonic() < deadline:
            exited = [process for process in processes if process.poll() is not None]
            alive = [process for process in processes if process.poll() is None]
            if len(exited) == 1 and len(alive) == 1:
                loser, winner = exited[0], alive[0]
                assert loser.returncode == 0, log_paths[processes.index(loser)].read_text(
                    encoding="utf-8", errors="replace")
                break
            assert all(process.returncode in (None, 0) for process in exited), "assistant start crashed"
            time.sleep(0.05)
        assert winner is not None and loser is not None, "two starts did not settle to one owner"

        status = None
        while time.monotonic() < deadline:
            try:
                status = client.status()
                break
            except UiControlError as exc:
                if exc.code != "assistant_unavailable":
                    raise
                time.sleep(0.1)
        assert status is not None, "the offscreen assistant endpoint never became ready"
        assert status["application"] == "coat_sim"
        assert status["pid"] == winner.pid
        assert status["enabled"] is True
        assert status["bubble_created"] is True
        assert status["chat_created"] is True
        assert status["generation"]

        # ensure_running is a client operation here. Its caller may exit without
        # taking ownership of, or stopping, the independently running UI.
        helper = subprocess.run(
            [sys.executable, "-c", (
                "import json,sys; from coat_sim.assistant_process import AssistantProcessClient; "
                "client=AssistantProcessClient(sys.argv[1], application_root=sys.argv[2], timeout=2); "
                "print(json.dumps(client.ensure_running(timeout=3)))"
            ), str(runtime), str(root)],
            cwd=root, env=environment, capture_output=True, text=True,
            timeout=10, check=False,
        )
        assert helper.returncode == 0, helper.stderr
        from_parent = json.loads(helper.stdout)
        assert from_parent["pid"] == status["pid"]
        assert from_parent["generation"] == status["generation"]
        assert client.status()["pid"] == status["pid"]

        # A later launch loses the same OS-held lock and leaves the exact
        # process generation untouched; no process-name scan or termination is used.
        duplicate = subprocess.run(command, cwd=root, env=environment,
                                   capture_output=True, text=True, timeout=10, check=False)
        assert duplicate.returncode == 0, duplicate.stderr
        after_duplicate = client.status()
        assert after_duplicate["pid"] == status["pid"]
        assert after_duplicate["generation"] == status["generation"]

        exit_request = client.request("request_exit_all")
        assert exit_request["state"] == "own_confirmation"
        assert winner.wait(timeout=20.0) == 0
        with pytest.raises(UiControlError) as stopped:
            client.status()
        assert stopped.value.code == "assistant_unavailable"
    finally:
        if processes:
            live = next((process for process in processes if process.poll() is None), None)
            if live is not None:
                request_exit_and_wait(live)
        for process in processes:
            if process.poll() is None:
                request_exit_and_wait(process)
        for stream in logs:
            stream.close()


def test_exit_wait_reports_delay_and_continues_until_exact_process_exits(monkeypatch):
    registry = ApplicationClientRegistry()
    client = _client("slow", 1701)
    _install_clients(registry, client)
    _patch_status(monkeypatch)
    alive = {"value": True}
    monkeypatch.setattr(assistant_process_module, "_process_identity",
                        lambda _pid: ("alive", client.process_start) if alive["value"] else ("dead", None))
    ticks = {"value": 0.0}
    monkeypatch.setattr(assistant_process_module.time, "monotonic", lambda: ticks["value"])
    monkeypatch.setattr(assistant_process_module.time, "sleep", lambda _seconds: ticks.update(value=ticks["value"] + 10))

    def call(_client, operation, **_arguments):
        if operation == "lifecycle_prepare":
            return {"state": "ready", "permit_id": "slow-permit", "generation": client.generation}
        return {"accepted": True, "generation": client.generation}
    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    progress = []

    def update(value):
        progress.append(value)
        if value.get("code") == "client_exit_delayed":
            alive["value"] = False
    result = registry.request_exit_all("slow-exit", on_progress=update)
    assert result["state"] == "completed"
    assert any(value.get("code") == "client_exit_delayed" for value in progress)
    assert registry._exiting is False


def test_explicit_abandon_wait_does_not_cancel_already_committed_exit(monkeypatch):
    from threading import Event
    registry = ApplicationClientRegistry()
    client = _client("slow", 1801)
    _install_clients(registry, client)
    _patch_status(monkeypatch)
    monkeypatch.setattr(assistant_process_module, "_process_identity", lambda _pid: ("alive", client.process_start))
    calls = []

    def call(_client, operation, **_arguments):
        calls.append(operation)
        if operation == "lifecycle_prepare":
            return {"state": "ready", "permit_id": "slow-permit", "generation": client.generation}
        return {"accepted": True, "generation": client.generation}
    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    abandon = Event()
    abandon.set()
    result = registry.request_exit_all("abandon-exit", cancel_wait=abandon)
    assert result["state"] == "wait_abandoned"
    assert result["committed_clients"] == [client.client_id]
    assert result["pending_clients"] == [client.client_id]
    assert "lifecycle_cancel" not in calls
    assert registry._exiting is False
