"""Startup PID ownership, redirector handoffs and adversarial identity claims."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coat_sim.ui import ready_channel, startup_identity
from coat_sim.ui.startup_identity import _ProcessIdentity


pytestmark = pytest.mark.contract


def _ready(pid):
    return {"status": "ready", "endpoint": {
        "protocol": 1, "host": "127.0.0.1", "port": 12345, "token": "t" * 32,
        "pid": pid, "root": "/application", "role": "project", "window_type": None,
        "parent_pid": None}}


class _Processes:
    def __init__(self):
        self.records = {1: _ProcessIdentity(1, 1), 10: _ProcessIdentity(1000, 100), 20: _ProcessIdentity(2000, 200),
                        30: _ProcessIdentity(3000, 300), 40: _ProcessIdentity(4000, 400),
                        90: _ProcessIdentity(9000, 20)}
        self.by_pid = {record.pid: handle for handle, record in self.records.items()}
        self.tree = {1000: 1, 2000: 1000, 3000: 2000, 4000: 3000, 9000: 1}
        self.closed = []
        self.opened = []
        self.aliases = {}

    def identity(self, handle):
        return self.records[self.aliases.get(handle, handle)]

    def open(self, pid):
        self.opened.append(pid)
        if pid not in self.by_pid:
            raise PermissionError("process identity inaccessible")
        handle = 10000 + len(self.opened)
        self.aliases[handle] = self.by_pid[pid]
        return handle

    def close(self, handle):
        self.closed.append(handle)

    def parents(self):
        return self.tree.copy()

    def peer_pid(self, connection):
        return connection.pid


@pytest.fixture
def identity(monkeypatch):
    api = _Processes()
    monkeypatch.setattr(startup_identity, "_WindowsProcesses", lambda: api)
    verifier = startup_identity.WindowsStartupIdentity(SimpleNamespace(pid=1000, _handle=10))
    try:
        yield verifier, api
    finally:
        verifier.close()
        # The borrowed Popen handle remains the caller's property.
        assert 10 not in api.closed


@pytest.mark.parametrize("handle,pid", [(10, 1000), (20, 2000), (40, 4000)])
def test_exact_process_or_verified_redirector_descendant_is_accepted(identity, handle, pid):
    verifier, _api = identity
    assert verifier.authorize(_ready(pid), handle) == pid


def test_endpoint_pid_cannot_impersonate_a_different_pipe_peer(identity):
    verifier, _api = identity
    with pytest.raises(ValueError, match="pipe sender identity"):
        verifier.authorize(_ready(1000), 20)


def test_authenticated_but_unrelated_pipe_sender_is_rejected(identity):
    verifier, _api = identity
    with pytest.raises(ValueError, match="ancestry identity"):
        verifier.authorize(_ready(9000), 90)


@pytest.mark.parametrize("case", ["stale_child", "reused_parent", "after_parent_exit",
                                  "dead_peer", "parent_cycle", "missing_parent"])
def test_stale_unknown_or_spoofed_ancestry_fails_closed(identity, case):
    verifier, api = identity
    if case == "stale_child":
        api.records[20] = _ProcessIdentity(2000, 50)
    elif case == "reused_parent":
        api.records[20] = _ProcessIdentity(2000, 500)
    elif case == "after_parent_exit":
        api.records[20] = _ProcessIdentity(2000, 200, 250)
    elif case == "dead_peer":
        api.records[30] = _ProcessIdentity(3000, 300, 310)
    elif case == "parent_cycle":
        api.tree[2000] = 3000
    elif case == "missing_parent":
        del api.tree[2000]
    with pytest.raises(ValueError, match="ancestry identity"):
        verifier.authorize(_ready(3000), 30)


def test_inaccessible_ancestor_does_not_relax_ownership(identity):
    verifier, api = identity
    del api.by_pid[2000]
    with pytest.raises(PermissionError):
        verifier.authorize(_ready(3000), 30)


def test_pid_reuse_does_not_replace_original_popen_handle(identity):
    verifier, api = identity
    api.records[11] = _ProcessIdentity(1000, 500)
    api.by_pid[1000] = 11
    with pytest.raises(ValueError, match="ancestry identity"):
        verifier.authorize(_ready(1000), 11)
    assert 1000 not in api.opened


def test_popen_pid_must_match_its_retained_handle(identity):
    with pytest.raises(ValueError, match="handle identity mismatch"):
        startup_identity.WindowsStartupIdentity(SimpleNamespace(pid=9999, _handle=10))


def test_parent_chain_is_bounded(identity):
    verifier, api = identity
    for i in range(2, 12):
        api.records[100 + i] = _ProcessIdentity(i, 200 + i)
        api.by_pid[i] = 100 + i
        api.tree[i] = i - 1 if i > 2 else 1000
    with pytest.raises(ValueError, match="ancestry identity"):
        verifier.authorize(_ready(11), 111)


def test_redirector_bootstrap_hands_off_only_to_verified_host(identity):
    verifier, api = identity
    handoff = {"status": "handoff", "process_pid": 2000, "host_pid": 3000}
    assert verifier.authorize(handoff, 20) == 2000
    # The bootstrap may exit after the ACK. Host ownership survives its exit,
    # including another venv redirector at the host itself.
    api.records[10] = _ProcessIdentity(1000, 100, 350)
    api.records[20] = _ProcessIdentity(2000, 200, 350)
    assert verifier.authorize(_ready(4000), 40) == 4000
    retained = list(verifier._owned_handles)
    verifier.close()
    assert retained and all(handle in api.closed for handle in retained)


@pytest.mark.parametrize("process_pid,host_pid", [(1000, 3000), (2000, 9000), (2000, 2000)])
def test_handoff_rejects_spoofed_sender_or_unrelated_host(identity, process_pid, host_pid):
    verifier, _api = identity
    with pytest.raises(ValueError, match="identity"):
        verifier.authorize({"status": "handoff", "process_pid": process_pid,
                            "host_pid": host_pid}, 20)


def test_explicit_host_handoff_rejects_reused_host_pid_and_other_child(identity):
    verifier, api = identity
    verifier.authorize({"status": "handoff", "process_pid": 2000, "host_pid": 3000}, 20)
    api.records[31] = _ProcessIdentity(3000, 500)
    api.by_pid[3000] = 31
    for handle, pid in [(31, 3000), (20, 2000)]:
        with pytest.raises(ValueError, match="ancestry identity"):
            verifier.authorize(_ready(pid), handle)


def test_closed_identity_verifier_rejects_late_event(identity):
    verifier, _api = identity
    verifier.close()
    with pytest.raises(ValueError, match="closed"):
        verifier.authorize(_ready(2000), 20)


class _Connection:
    def __init__(self, pid, event):
        self.pid, self.event = pid, event
        self.acks = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def recv_bytes(self, _limit):
        return json.dumps(self.event).encode()

    def send_bytes(self, value):
        self.acks.append(value)


class _Listener:
    def __init__(self, connections):
        self.connections = iter(connections)
        self.closed = False

    def accept(self):
        return next(self.connections)

    def close(self):
        self.closed = True


def _channel_result(connections, *, family="AF_PIPE", exited=False):
    channel = ready_channel.UiReadyChannel.__new__(ready_channel.UiReadyChannel)
    channel.family = family
    listener = _Listener(connections)
    channel._listener = cast(Any, listener)
    channel._temporary_directory = None
    channel._closed = False
    release = threading.Event()
    if exited:
        release.set()
    process = SimpleNamespace(pid=1000, _handle=10, wait=lambda: release.wait(3) and 0)
    try:
        return channel.wait(process, timeout=2)
    finally:
        release.set()
        channel.close()
        assert listener.closed


def test_channel_returns_actual_redirected_pid_and_acknowledges(identity):
    connection = _Connection(2000, _ready(2000))
    event = _channel_result([connection])
    assert event == {**_ready(2000), "handoff_host_pid": 2000}
    assert connection.acks == [b"accepted"]
    assert "_verified_process_pid" not in event


def test_channel_rejects_pipe_sender_spoof_and_does_not_acknowledge(identity):
    connection = _Connection(9000, _ready(2000))
    event = _channel_result([connection])
    assert event["status"] == "channel_error"
    assert connection.acks == []


def test_wire_cannot_supply_internal_verified_pid(identity):
    event = {**_ready(9000), "_verified_process_pid": 9000}
    connection = _Connection(9000, event)
    assert _channel_result([connection])["status"] == "channel_error"
    assert connection.acks == []


def test_windows_channel_host_handoff_and_early_bootstrap_exit(identity):
    handoff = _Connection(2000, {"status": "handoff", "process_pid": 2000, "host_pid": 3000})
    ready = _Connection(4000, _ready(4000))
    event = _channel_result([handoff, ready], exited=True)
    assert event == {**_ready(4000), "handoff_host_pid": 4000}
    assert handoff.acks == ready.acks == [b"accepted"]


def test_duplicate_handoff_is_rejected(identity):
    first = _Connection(2000, {"status": "handoff", "process_pid": 2000, "host_pid": 3000})
    second = _Connection(3000, {"status": "handoff", "process_pid": 3000, "host_pid": 4000})
    assert _channel_result([first, second])["status"] == "channel_error"


@pytest.mark.parametrize("pid,expected", [(1000, "ready"), (2000, "channel_error")])
def test_posix_retains_exact_pid_rule(identity, pid, expected):
    connection = _Connection(pid, _ready(pid))
    assert _channel_result([connection], family="AF_UNIX")["status"] == expected
    assert connection.acks == []


@pytest.mark.parametrize("ack", [b"accepted", b"wrong", None])
def test_windows_report_waits_for_verification_ack(monkeypatch, ack):
    connection = _Connection(2000, {})
    monkeypatch.setattr(connection, "poll", lambda timeout: ack is not None, raising=False)
    monkeypatch.setattr(connection, "recv_bytes", lambda _limit: ack)
    monkeypatch.setattr(ready_channel, "Client", lambda *a, **kw: connection)
    monkeypatch.setenv(ready_channel.ADDRESS_ENV, "test-pipe")
    monkeypatch.setenv(ready_channel.FAMILY_ENV, "AF_PIPE")
    monkeypatch.setenv(ready_channel.KEY_ENV, base64.urlsafe_b64encode(b"k" * 32).decode())
    assert ready_channel.report_startup_event("ready", endpoint=_ready(2000)["endpoint"]) is (
        ack == b"accepted")
    assert ready_channel.ADDRESS_ENV not in os.environ


@pytest.mark.skipif(os.name != "nt", reason="requires the actual Windows venv redirector and AF_PIPE")
def test_real_windows_venv_redirector_startup(tmp_path):
    """No GUI or installed dependencies; catch the real Popen/worker PID split."""
    root = Path(__file__).resolve().parents[1]
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                   check=True, timeout=60)
    python = venv / "Scripts" / "python.exe"
    channel = ready_channel.UiReadyChannel()
    environment = {**os.environ, **channel.child_environment(), "PYTHONPATH": str(root)}
    source = ("import os; from coat_sim.ui.ready_channel import report_startup_event; "
              f"endpoint = {_ready(1)['endpoint']!r}; endpoint['pid'] = os.getpid(); "
              "assert report_startup_event('ready', endpoint=endpoint)")
    process = subprocess.Popen([str(python), "-c", source], cwd=root, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        event = channel.wait(process, timeout=30)
        stdout, stderr = process.communicate(timeout=15)
        assert event["status"] == "ready", (event, stdout, stderr)
        assert process.returncode == 0, stderr
        assert event["endpoint"]["pid"] != process.pid, "venv must exercise the redirector PID split"
        assert event["handoff_host_pid"] == event["endpoint"]["pid"]
    finally:
        channel.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
