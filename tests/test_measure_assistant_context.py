"""The offline measurement guard must preserve asyncio's real wake sockets."""
from __future__ import annotations

import asyncio
from asyncio.proactor_events import BaseProactorEventLoop
import socket
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from tools.measure_assistant_context import ROOT, _prohibit_network


pytestmark = pytest.mark.contract


@pytest.mark.parametrize("use_tcp_fallback", [False, True])
def test_guard_preserves_socket_classes_and_real_socketpair(use_tcp_fallback):
    socket_class, socket_type = socket.socket, socket.SocketType
    pair_factory = socket.socketpair
    if use_tcp_fallback:
        fallback = getattr(socket, "_fallback_socketpair", None)
        if fallback is None:
            pytest.skip("This Python exposes only its platform socketpair")
        pair_factory = fallback
    with patch.object(socket, "socketpair", pair_factory), _prohibit_network():
        assert socket.socket is socket_class
        assert socket.SocketType is socket_type
        left, right = socket.socketpair()
        with left, right:
            assert isinstance(left, socket.socket)
            assert isinstance(right, socket.SocketType)
            right.settimeout(2)
            left.sendall(b"wake")
            assert right.recv(4) == b"wake"
            with pytest.raises(AssertionError, match="Network prohibited"):
                left.connect(("127.0.0.1", 12345))


def test_guard_blocks_real_socket_operations_even_for_loopback(monkeypatch):
    attempted = []

    def tripwire(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("Underlying network operation reached")

    # These lower tripwires ensure even a broken guard cannot send test traffic.
    methods = ["bind", "listen", "connect", "connect_ex", "sendto"]
    if hasattr(socket.socket, "sendmsg"):
        methods.append("sendmsg")
    for name in methods:
        monkeypatch.setattr(socket.socket, name, tripwire)
    with _prohibit_network(), socket.socket() as client:
        for host in ("192.0.2.1", "127.0.0.1", "localhost", "::1"):
            for name in ("bind", "connect", "connect_ex"):
                with pytest.raises(AssertionError, match="Network prohibited"):
                    getattr(client, name)((host, 12345))
            with pytest.raises(AssertionError, match="Network prohibited"):
                client.sendto(b"blocked", (host, 12345))
            if hasattr(client, "sendmsg"):
                with pytest.raises(AssertionError, match="Network prohibited"):
                    client.sendmsg([b"blocked"], [], 0, (host, 12345))
        with pytest.raises(AssertionError, match="Network prohibited"):
            client.listen()
    assert not attempted


def test_guard_blocks_dns_before_the_resolver(monkeypatch):
    calls = {
        "getaddrinfo": ("localhost", 12345),
        "gethostbyname": ("example.invalid",),
        "gethostbyname_ex": ("example.invalid",),
        "gethostbyaddr": ("192.0.2.1",),
        "getnameinfo": (("127.0.0.1", 12345), 0),
    }
    attempted = []

    def tripwire(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("Underlying DNS operation reached")

    for name in calls:
        monkeypatch.setattr(socket, name, tripwire)
    with _prohibit_network():
        for name, arguments in calls.items():
            with pytest.raises(AssertionError, match="Network prohibited"):
                getattr(socket, name)(*arguments)
    assert not attempted


def test_guard_blocks_public_async_connection_and_datagram_apis(monkeypatch):
    attempted = []

    async def tripwire(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("Underlying async network operation reached")

    for name in ("create_connection", "create_datagram_endpoint"):
        monkeypatch.setattr(asyncio.BaseEventLoop, name, tripwire)

    async def check():
        loop = asyncio.get_running_loop()
        with _prohibit_network():
            for host in ("192.0.2.1", "127.0.0.1"):
                with pytest.raises(AssertionError, match="Network prohibited"):
                    await loop.create_connection(asyncio.Protocol, host, 12345)
                with pytest.raises(AssertionError, match="Network prohibited"):
                    await loop.create_datagram_endpoint(asyncio.DatagramProtocol,
                                                        remote_addr=(host, 12345))

    asyncio.run(check())
    assert not attempted


def test_guard_covers_windows_iocp_bypass_without_native_network_calls():
    attempted = []

    class StubProactor:
        def connect(self, *args, **kwargs):
            attempted.append((args, kwargs))
            raise AssertionError("IOCP ConnectEx reached")

        def sendto(self, *args, **kwargs):
            attempted.append((args, kwargs))
            raise AssertionError("IOCP WSASendTo reached")

    class WindowsEventsStub(ModuleType):
        IocpProactor = StubProactor

    windows_events = WindowsEventsStub("asyncio.windows_events")
    loop = cast(Any, SimpleNamespace(_debug=False, _proactor=StubProactor()))

    async def check():
        with patch.dict(sys.modules, {"asyncio.windows_events": windows_events}):
            with patch.object(sys, "platform", "win32"), _prohibit_network():
                with socket.socket() as client:
                    with pytest.raises(AssertionError, match="Network prohibited"):
                        await BaseProactorEventLoop.sock_connect(loop, client, ("192.0.2.1", 12345))
                    with pytest.raises(AssertionError, match="Network prohibited"):
                        await BaseProactorEventLoop.sock_sendto(loop, client, b"blocked",
                                                               ("127.0.0.1", 12345))

    asyncio.run(check())
    assert not attempted


def test_socketpair_allowance_is_thread_local_and_restored_on_failure():
    original_pair = socket.socketpair
    original_connect = socket.socket.connect
    observations = []

    def unrelated_worker():
        with socket.socket() as client:
            try:
                client.bind(("127.0.0.1", 0))
            except AssertionError as error:
                observations.append(str(error))

    def failing_pair():
        worker = threading.Thread(target=unrelated_worker, daemon=True)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert observations == ["Network prohibited during offline context measurement"]
        raise OSError("Synthetic socketpair construction failure")

    with patch.object(socket, "socketpair", failing_pair):
        with _prohibit_network():
            with pytest.raises(OSError, match="Synthetic socketpair"):
                socket.socketpair()
            with socket.socket() as client:
                with pytest.raises(AssertionError, match="Network prohibited"):
                    client.bind(("127.0.0.1", 0))
        assert socket.socket.connect is original_connect
    assert socket.socketpair is original_pair


@pytest.mark.parametrize("use_tcp_fallback", [False, True])
def test_guard_supports_worker_loops_and_actual_three_mode_capture(use_tcp_fallback):
    # A subprocess timeout bounds the original Windows/AnyIO startup hang, and
    # verifies the tool's own guard without pytest's separate network guard.
    script = """
import asyncio
import socket
from tools.measure_assistant_context import _prohibit_network, capture_configuration

if USE_TCP_FALLBACK and hasattr(socket, '_fallback_socketpair'):
    socket.socketpair = socket._fallback_socketpair
original_class = socket.socket

def worker():
    assert socket.socket is original_class
    return asyncio.run(asyncio.sleep(0, result='worker ready'))

async def check():
    # This event loop predates the guard; its self-wake send must still work.
    with _prohibit_network():
        assert await asyncio.wait_for(asyncio.to_thread(worker), timeout=5) == 'worker ready'
        # Exercise the actual platform's raw socket APIs, including Windows IOCP.
        loop = asyncio.get_running_loop()
        for operation in ('sock_connect', 'sock_sendto'):
            with socket.socket(type=socket.SOCK_DGRAM if operation == 'sock_sendto' else socket.SOCK_STREAM) as client:
                client.setblocking(False)
                arguments = (client, b'blocked', ('127.0.0.1', 12345)) if operation == 'sock_sendto' else (client, ('127.0.0.1', 12345))
                try:
                    await getattr(loop, operation)(*arguments)
                except AssertionError as error:
                    assert 'Network prohibited' in str(error)
                else:
                    raise AssertionError(operation + ' escaped the guard')
        for mode in ('chat', 'operate', 'research'):
            captured = await capture_configuration(mode)
            assert captured['instructions']
            assert captured['parameters'].function_tools

asyncio.run(check())
# Also construct and close the caller's default loop while the guard is active.
with _prohibit_network():
    assert worker() == 'worker ready'
print('offline capture complete')
""".replace("USE_TCP_FALLBACK", repr(use_tcp_fallback))
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "offline capture complete"
