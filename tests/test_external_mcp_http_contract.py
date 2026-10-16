"""Actual TCP loopback HTTP/SSE contract tests for ExternalMcpPool.

Every peer is an SDK-native MCP server in a short-lived uvicorn subprocess;
there are no mocked transports, live services, credentials, or provider APIs.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any, Iterator

import pytest

from coat_sim.agent import external_mcp as external

pytestmark = pytest.mark.contract
FIXTURE = Path(__file__).parent / "fixtures" / "external_mcp_http_server.py"
PROVIDER_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COAT_SIM_ASSISTANT_API_KEY",
                "COAT_SIM_FIXTURE_HISTORY", "COAT_SIM_FIXTURE_ATTACHMENT")
SAFE_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME",
            "LANG", "LC_ALL", "PATHEXT")


def event_rows(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def events(log: Path, event: str, **match: Any) -> list[dict[str, Any]]:
    return [row for row in event_rows(log) if row["event"] == event
            and all(row.get(key) == value for key, value in match.items())]


def calls(log: Path, name: str) -> list[dict[str, Any]]:
    return events(log, "call", name=name)


class LoopbackPeer:
    """Own and verify one or more temporary uvicorn fixture processes."""

    def __init__(self, tmp_path: Path, transport: str, *, port: int = 0,
                 log_name: str = "peer.jsonl") -> None:
        self.tmp_path = tmp_path
        self._startup_dir = Path(tempfile.mkdtemp(prefix="loopback-peer-", dir=tmp_path))
        self.transport = transport
        self.requested_port = port
        self.log = tmp_path / log_name
        self._processes: list[subprocess.Popen[bytes]] = []
        self._stderr_handles: dict[subprocess.Popen[bytes], Any] = {}
        self._stderr_paths: dict[subprocess.Popen[bytes], Path] = {}
        self._sequence = 0
        self.port = 0
        self.url = ""
        self.start(port)

    def start(self, port: int | None = None) -> None:
        self._sequence += 1
        ready = self._startup_dir / f"ready-{self._sequence}.json"
        ready.unlink(missing_ok=True)
        env = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
        stderr_path = self._startup_dir / f"fixture-stderr-{self._sequence}.log"
        stderr_stream = stderr_path.open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(FIXTURE.resolve()), "--transport", self.transport,
                 "--port", str(self.requested_port if port is None else port),
                 "--log", str(self.log), "--ready", str(ready)],
                cwd=tempfile.gettempdir(), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=stderr_stream)
        except BaseException:
            stderr_stream.close()
            raise
        self._processes.append(proc)
        self._stderr_handles[proc] = stderr_stream
        self._stderr_paths[proc] = stderr_path
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.exists():
                try:
                    address = json.loads(ready.read_text(encoding="utf-8"))
                    self.port = int(address["port"])
                except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    detail = self._stderr_tail(proc)
                    self.stop_process(proc)
                    raise RuntimeError(f"local MCP fixture published invalid readiness data: "
                                       f"{ready}; stderr={stderr_path}; tail:\n{detail}") from exc
                path = "/mcp" if self.transport == "http" else "/sse"
                self.url = f"http://127.0.0.1:{self.port}{path}"
                return
            code = proc.poll()
            if code is not None:
                detail = self._stderr_tail(proc)
                self.stop_process(proc)
                raise RuntimeError(f"local MCP fixture exited during startup: {code}; "
                                   f"stderr={stderr_path}; tail:\n{detail}")
            time.sleep(.02)
        detail = self._stderr_tail(proc)
        self.stop_process(proc)
        raise TimeoutError(f"local MCP fixture did not bind its loopback port; "
                           f"stderr={stderr_path}; tail:\n{detail}")

    def restart_same_target(self) -> None:
        prior = self._processes[-1]
        try:
            prior.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("previous fixture did not exit after disconnect") from exc
        assert prior.poll() is not None, "previous fixture has not exited"
        self.start(self.port)

    def _stderr_tail(self, proc: subprocess.Popen[bytes]) -> str:
        path = self._stderr_paths.get(proc)
        if path is None or not path.exists():
            return "(no child stderr captured)"
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])

    def stop_process(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        assert proc.poll() is not None, "fixture process still alive after cleanup"
        handle = self._stderr_handles.pop(proc, None)
        if handle is not None:
            handle.close()

    def close(self) -> None:
        for proc in reversed(self._processes):
            self.stop_process(proc)

    def __enter__(self) -> "LoopbackPeer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def config(peer: LoopbackPeer, *, id: str = "http_fixture", **overrides: Any) -> dict[str, Any]:
    return {"id": id, "name": "Loopback fixture", "transport": peer.transport,
            "url": peer.url, "enabled": True, "allow_tools": True,
            "allow_project_data": False, "timeout_s": 5, **overrides}


async def wait_for_event(log: Path, event: str, *, name: str | None = None,
                         count: int = 1, timeout: float = 8) -> list[dict[str, Any]]:
    async def wait() -> list[dict[str, Any]]:
        while True:
            rows = events(log, event, **({"name": name} if name is not None else {}))
            if len(rows) >= count:
                return rows
            await asyncio.sleep(.02)
    return await asyncio.wait_for(wait(), timeout=timeout)


def run(scenario: Any, *, timeout: float = 90) -> Any:
    return asyncio.run(asyncio.wait_for(scenario(), timeout=timeout))


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("gate", [
    {"enabled": False}, {"allow_tools": False},
    {"enabled": "true"}, {"allow_tools": 1},
])
def test_strict_double_gate_has_zero_real_tcp_requests(tmp_path: Path, transport: str,
                                                        gate: dict[str, Any]) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            try:
                await pool.sync([config(peer, **gate)])
                assert (await pool.catalog())["tools"] == []
                assert (await pool.catalog("http_fixture")).get("tools", []) == []
                denied = await pool.execute("http_fixture", "echo", {"value": "synthetic"})
                assert denied["effect"] == "none" and not denied["ok"]
                assert not events(peer.log, "request")
                assert pool.active == {} and pool.uncertain_calls == set()
            finally:
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_real_tcp_catalog_schema_arguments_empty_roots_and_no_credential_reads(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
    for name in PROVIDER_ENV:
        monkeypatch.setenv(name, "synthetic-parent-only")
    with LoopbackPeer(tmp_path, transport) as peer:
        from coat_sim.ui import assistant_credentials
        monkeypatch.setattr(assistant_credentials, "read_mcp_secret",
                            lambda _id: pytest.fail("fixture must not read security credentials"))

        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            try:
                await pool.sync([config(peer)])
                overview = await pool.catalog()
                assert overview["tools"] == [] and not events(peer.log, "request")
                catalog = await pool.catalog("http_fixture", "echo")
                assert [item["name"] for item in catalog["tools"]] == ["echo"]
                assert catalog["tools"][0]["input_schema"] == {
                    "type": "object", "properties": {"value": {}}, "required": ["value"],
                    "additionalProperties": False}
                args = {"value": {"only": "explicit", "count": 2}}
                result = await pool.execute("http_fixture", "echo", args)
                assert result["ok"] and result["effect"] == "completed"
                payload = json.loads(result["result"]["content"][0]["text"])
                assert payload["arguments"] == args and payload["roots"] == []
                assert isinstance(payload["roots_back_channel"], bool)
                roots_callback = pool.active["http_fixture"][0]._session_kwargs["list_roots_callback"]
                assert (await roots_callback(None)).roots == []
                assert calls(peer.log, "echo") == [
                    {"event": "call", "name": "echo", "arguments": args}]
                assert events(peer.log, "startup")[0]["provider_environment_keys"] == []
                paths = [row["path"] for row in events(peer.log, "request")]
                assert paths and all(path in ("/mcp", "/mcp/", "/sse", "/messages", "/messages/") for path in paths)
            finally:
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_business_error_and_real_socket_loss_are_unknown_and_never_replayed(
        tmp_path: Path, transport: str) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            try:
                await pool.sync([config(peer)])
                args = {"value": {"first": 1, "second": "one attempt"}}
                business = await pool.execute("http_fixture", "business_error", args)
                assert not business["ok"] and business["effect"] == "unknown"
                assert len(calls(peer.log, "business_error")) == 1
                assert len(events(peer.log, "request")) >= 1
                pool.begin_turn()
                await pool.sync([config(peer, enabled=False)])
                await pool.sync([config(peer)])
                # Establish a real fresh connection to the same target before
                # retrying the now-guarded operation.
                echo = await pool.execute("http_fixture", "echo", {"value": "reconnected"})
                assert echo["ok"]
                blocked = await pool.execute("http_fixture", "business_error",
                                             {"value": {"second": "one attempt", "first": 1}})
                assert blocked["effect"] == "none" and blocked["prior_effect"] == "unknown"
                assert blocked["dispatch_blocked"]
                assert len(calls(peer.log, "business_error")) == 1

                disconnected = await pool.execute("http_fixture", "disconnect", {"value": "drop"})
                assert not disconnected["ok"] and disconnected["effect"] == "unknown"
                await wait_for_event(peer.log, "disconnect")
                assert len(calls(peer.log, "disconnect")) == 1
                pool.begin_turn()
                await pool.sync([config(peer, enabled=False)])
                await pool.sync([config(peer)])
                # Restart the same URL in our explicitly owned fixture process.
                # The process-bound connection is dead, but the pool's identity
                # barrier must survive the reconnect on the exact same target.
                peer.restart_same_target()
                recovered = await pool.execute("http_fixture", "echo", {"value": "new process"})
                assert recovered["ok"]
                replay = await pool.execute("http_fixture", "disconnect", {"value": "drop"})
                assert replay["effect"] == "none" and replay["dispatch_blocked"]
                assert len(calls(peer.log, "disconnect")) == 1
            finally:
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_post_dispatch_cancel_and_five_second_timeout_preserve_unknown_barrier(
        tmp_path: Path, transport: str, mode: str) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            args = {"value": f"{mode}-synthetic"}
            task: asyncio.Task[Any] | None = None
            try:
                await pool.sync([config(peer)])
                if mode == "cancel":
                    task = asyncio.create_task(pool.execute("http_fixture", "slow", args))
                    await wait_for_event(peer.log, "call", name="slow")
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5)
                    await asyncio.sleep(.1)
                    # The fixture logs if its handler actually received a
                    # cancellation notification. This observation never
                    # changes the client's unknown-effect decision.
                    peer_cancel_observed = bool(events(peer.log, "cancelled", name="slow"))
                    assert isinstance(peer_cancel_observed, bool)
                else:
                    started = time.monotonic()
                    result = await pool.execute("http_fixture", "slow", args)
                    elapsed = time.monotonic() - started
                    assert not result["ok"] and result["effect"] == "unknown"
                    # The real HTTP request was held by the peer until the
                    # configured five-second client timeout elapsed.
                    assert 4.0 <= elapsed <= 8.0
                    await wait_for_event(peer.log, "call", name="slow")
                assert len(calls(peer.log, "slow")) == 1
                pool.begin_turn()
                await pool.sync([config(peer, enabled=False)])
                await pool.sync([config(peer)])
                blocked = await pool.execute("http_fixture", "slow", args)
                assert blocked["effect"] == "none" and blocked["dispatch_blocked"]
                assert blocked["prior_effect"] == "unknown"
                assert len(calls(peer.log, "slow")) == 1
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await pool.close()
        run(scenario, timeout=45)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_pre_dispatch_refusal_is_suppressed_until_next_turn_then_retried_over_tcp(
        tmp_path: Path, transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Choose a loopback port with no listener, then start our fixture on that
    # same target only after the pool's known pre-dispatch failure.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    log = tmp_path / "retry-peer.jsonl"
    waiting = LoopbackPeer.__new__(LoopbackPeer)
    waiting.tmp_path, waiting.transport, waiting.requested_port = tmp_path, transport, port
    waiting._startup_dir = Path(tempfile.mkdtemp(prefix="loopback-peer-", dir=tmp_path))
    waiting.log, waiting._processes, waiting._sequence = log, [], 0
    waiting._stderr_handles, waiting._stderr_paths = {}, {}
    waiting.port, waiting.url = port, f"http://127.0.0.1:{port}{'/mcp' if transport == 'http' else '/sse'}"
    real_connection = external._connection
    attempts: list[str] = []

    def count_real_connection(row: dict[str, Any]) -> Any:
        attempts.append(row["id"])
        return real_connection(row)

    monkeypatch.setattr(external, "_connection", count_real_connection)

    async def scenario() -> None:
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(waiting)])
            first = await pool.execute("http_fixture", "echo", {"value": "retry next turn"})
            assert first["effect"] == "none" and not pool.uncertain_calls
            second = await pool.execute("http_fixture", "echo", {"value": "retry next turn"})
            assert second["effect"] == "none" and attempts == ["http_fixture"]
            assert not log.exists()
            waiting.start(port)
            pool.begin_turn()
            okay = await pool.execute("http_fixture", "echo", {"value": "retry next turn"})
            assert okay["ok"] and attempts == ["http_fixture", "http_fixture"]
            assert len(calls(log, "echo")) == 1
        finally:
            await pool.close()
    try:
        run(scenario)
    finally:
        waiting.close()


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_revocation_while_catalog_waits_for_lock_creates_no_tcp_request(
        tmp_path: Path, transport: str) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            try:
                await pool.sync([config(peer)])
                lock = pool._lock("http_fixture")
                await lock.acquire()
                pending = asyncio.create_task(pool.catalog("http_fixture"))
                for _ in range(100):
                    if getattr(lock, "_waiters", None):
                        break
                    await asyncio.sleep(.01)
                assert getattr(lock, "_waiters", None)
                await pool.sync([config(peer, allow_tools=False)])
                lock.release()
                result = await asyncio.wait_for(pending, timeout=5)
                assert result["tools"] == []
                assert not events(peer.log, "request")
                assert (await pool.execute("http_fixture", "echo", {"value": "deny"}))["effect"] == "none"
                assert not pool.uncertain_calls
            finally:
                if lock.locked():
                    lock.release()
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("operation", ["execute", "catalog"])
def test_active_old_target_queued_operation_never_uses_replacement_or_cached_old_peer(
        tmp_path: Path, transport: str, operation: str) -> None:
    old = LoopbackPeer(tmp_path, transport, log_name="old-peer.jsonl")
    new = LoopbackPeer(tmp_path, transport, log_name="new-peer.jsonl")
    try:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            pending: asyncio.Task[Any] | None = None
            sync_task: asyncio.Task[Any] | None = None
            lock: asyncio.Lock | None = None
            try:
                await pool.sync([config(old)])
                established = await pool.execute("http_fixture", "echo", {"value": "old warm connection"})
                assert established["ok"] and len(calls(old.log, "echo")) == 1
                lock = pool._lock("http_fixture")
                await lock.acquire()
                if operation == "execute":
                    pending = asyncio.create_task(pool.execute(
                        "http_fixture", "echo", {"value": "must reach new target"}))
                else:
                    pending = asyncio.create_task(pool.catalog("http_fixture", "echo"))
                for _ in range(100):
                    if getattr(lock, "_waiters", None):
                        break
                    await asyncio.sleep(.01)
                assert getattr(lock, "_waiters", None)
                sync_task = asyncio.create_task(pool.sync([config(new)]))
                for _ in range(100):
                    row = pool.configs.get("http_fixture")
                    if row and row.get("url") == new.url:
                        break
                    await asyncio.sleep(.01)
                assert pool.configs["http_fixture"]["url"] == new.url
                assert not sync_task.done(), "sync must be waiting to close the locked old client"
                # The target replacement is installed before the queued operation
                # gets the lock; it must not reuse the now-stale active client.
                lock.release()
                value = await asyncio.wait_for(pending, timeout=15)
                await asyncio.wait_for(sync_task, timeout=15)
                if operation == "execute":
                    assert value["ok"] and value["effect"] == "completed"
                    assert len(calls(new.log, "echo")) == 1
                else:
                    assert [tool["name"] for tool in value["tools"]] == ["echo"]
                    assert events(new.log, "list_tools")
                assert len(calls(old.log, "echo")) == 1, "stale client dispatched to old peer"
                assert not pool.errors
            finally:
                if lock is not None and lock.locked():
                    lock.release()
                for task in (pending, sync_task):
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                await pool.close()
        run(scenario)
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("change", ["revoke", "replace"])
def test_config_change_during_real_directory_exchange_prevents_stale_dispatch(
        tmp_path: Path, transport: str, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    old = LoopbackPeer(tmp_path, transport, log_name="directory-old.jsonl")
    new = LoopbackPeer(tmp_path, transport, log_name="directory-new.jsonl")
    async def scenario() -> None:
        pool = external.ExternalMcpPool()
        real_connection = external._connection
        entered, release = asyncio.Event(), asyncio.Event()

        def gated_connection(row: dict[str, Any]) -> Any:
            client, transport_obj, secret = real_connection(row)
            real_list = client.list_tools
            async def gated_list(**kwargs: Any) -> Any:
                rows = await real_list(**kwargs)
                entered.set()
                await release.wait()
                return rows
            client.list_tools = gated_list
            return client, transport_obj, secret

        monkeypatch.setattr(external, "_connection", gated_connection)
        task: asyncio.Task[Any] | None = None
        try:
            await pool.sync([config(old)])
            task = asyncio.create_task(pool.execute("http_fixture", "echo", {"value": "stale"}))
            await asyncio.wait_for(entered.wait(), timeout=12)
            updated = config(old, allow_tools=False) if change == "revoke" else config(new)
            await pool.sync([updated])
            release.set()
            result = await asyncio.wait_for(task, timeout=10)
            assert not result["ok"] and result["effect"] == "none"
            assert not calls(old.log, "echo") and not events(new.log, "request")
            assert not pool.active and not pool.uncertain_calls and not pool.errors
            if change == "replace":
                fresh = await pool.execute("http_fixture", "echo", {"value": "fresh"})
                assert fresh["ok"] and len(calls(new.log, "echo")) == 1
            else:
                denied = await pool.execute("http_fixture", "echo", {"value": "revoked"})
                assert denied["effect"] == "none" and not pool.uncertain_calls
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.close()
    try:
        run(scenario)
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_arguments_are_frozen_before_first_real_connection_await(
        tmp_path: Path, transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            real_ensure = pool._ensure_locked
            entered, release = asyncio.Event(), asyncio.Event()
            async def gated_ensure(identifier: str) -> Any:
                entered.set()
                await release.wait()
                return await real_ensure(identifier)
            monkeypatch.setattr(pool, "_ensure_locked", gated_ensure)
            original = {"value": {"state": "frozen before await"}}
            task: asyncio.Task[Any] | None = None
            try:
                await pool.sync([config(peer)])
                task = asyncio.create_task(pool.execute("http_fixture", "business_error", original))
                await asyncio.wait_for(entered.wait(), timeout=3)
                original["value"]["state"] = "mutated while waiting"
                release.set()
                result = await task
                assert result["effect"] == "unknown"
                assert calls(peer.log, "business_error")[0]["arguments"] == {
                    "value": {"state": "frozen before await"}}
                pool.begin_turn()
                blocked = await pool.execute("http_fixture", "business_error",
                    {"value": {"state": "frozen before await"}})
                assert blocked["dispatch_blocked"]
                assert len(calls(peer.log, "business_error")) == 1
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_inflight_unknown_capacity_is_reserved_across_real_servers(
        tmp_path: Path, transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first = LoopbackPeer(tmp_path, transport, log_name="capacity-first.jsonl")
    second = LoopbackPeer(tmp_path, transport, log_name="capacity-second.jsonl")
    monkeypatch.setattr(external, "MAX_UNCERTAIN_CALLS", 1)
    try:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            task: asyncio.Task[Any] | None = None
            try:
                await pool.sync([config(first), config(second, id="second_fixture")])
                task = asyncio.create_task(pool.execute("http_fixture", "slow", {"value": "first"}))
                await wait_for_event(first.log, "call", name="slow")
                denied = await pool.execute("second_fixture", "echo", {"value": "second"})
                assert denied["code"] == "uncertain_capacity_exceeded" and denied["effect"] == "none"
                assert not events(second.log, "request")
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert len(pool.uncertain_calls) == 1 and not pool._inflight_calls
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await pool.close()
        run(scenario)
    finally:
        first.close()
        second.close()


def test_fixture_readiness_is_atomically_published(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("loopback_fixture_readiness", FIXTURE)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    ready = tmp_path / "ready.json"
    old_payload = {"host": "127.0.0.1", "port": 41001}
    new_payload = {"host": "127.0.0.1", "port": 41002}
    entered_replace = [Event(), Event()]
    allow_replace = [Event(), Event()]
    real_replace = fixture._replace_readiness_file
    replace_count = 0

    def paused_replace(source: Path, destination: Path) -> None:
        nonlocal replace_count
        index = replace_count
        replace_count += 1
        assert source.parent == destination.parent
        assert json.loads(source.read_text(encoding="utf-8")) == new_payload
        entered_replace[index].set()
        if not allow_replace[index].wait(timeout=5):
            raise TimeoutError("readiness replacement was not released by the test")
        real_replace(source, destination)

    monkeypatch.setattr(fixture, "_replace_readiness_file", paused_replace)

    def verify_before_replace(expected_current: dict[str, Any] | None) -> None:
        index = replace_count
        with ThreadPoolExecutor(max_workers=1) as executor:
            publication = executor.submit(fixture.publish_readiness, ready, new_payload)
            try:
                assert entered_replace[index].wait(timeout=5)
                if expected_current is None:
                    assert not ready.exists()
                else:
                    assert json.loads(ready.read_text(encoding="utf-8")) == expected_current
            finally:
                allow_replace[index].set()
            publication.result(timeout=5)
        assert json.loads(ready.read_text(encoding="utf-8")) == new_payload

    verify_before_replace(None)
    ready.write_text(json.dumps(old_payload), encoding="utf-8")
    verify_before_replace(old_payload)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("synthetic readiness replace failure")

    failed_ready = tmp_path / "failed-ready.json"
    monkeypatch.setattr(fixture, "_replace_readiness_file", fail_replace)
    with pytest.raises(OSError, match="synthetic readiness replace failure"):
        fixture.publish_readiness(failed_ready, new_payload)
    assert not failed_ready.exists()
    assert not list(tmp_path.glob(".failed-ready.json.*.tmp"))


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_loopback_peers_in_shared_tmp_path_have_distinct_startup_files(
        tmp_path: Path, transport: str) -> None:
    first = LoopbackPeer(tmp_path, transport, log_name="startup-first.jsonl")
    second = LoopbackPeer(tmp_path, transport, log_name="startup-second.jsonl")
    try:
        assert first._startup_dir != second._startup_dir
        assert first.port != second.port
        assert first._stderr_paths[first._processes[0]] != second._stderr_paths[second._processes[0]]
        assert len(list(tmp_path.glob("loopback-peer-*/ready-*.json"))) == 2
    finally:
        first.close()
        second.close()


def test_invalid_fixture_readiness_propagates_and_reaps_child(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_fixture = tmp_path / "invalid_readiness_fixture.py"
    bad_fixture.write_text(
        "import sys\n"
        "from threading import Event\n"
        "from pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--ready') + 1]).write_text('{', encoding='utf-8')\n"
        "Event().wait()\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "FIXTURE", bad_fixture)
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def capture_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", capture_popen)
    try:
        with pytest.raises(RuntimeError, match="published invalid readiness data"):
            LoopbackPeer(tmp_path, "http")
        assert len(children) == 1
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_credential_field_revision_and_name_change_reconnects_with_current_synthetic_header(
        tmp_path: Path, transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        from coat_sim.ui import assistant_credentials
        secret = {"value": "fixture-secret-v1"}
        reads: list[str] = []
        def synthetic_secret(identifier: str) -> str:
            reads.append(identifier)
            return secret["value"]
        monkeypatch.setattr(assistant_credentials, "read_mcp_secret", synthetic_secret)

        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            pending: asyncio.Task[Any] | None = None
            sync_task: asyncio.Task[Any] | None = None
            lock: asyncio.Lock | None = None
            try:
                first_config = config(peer, credential_field="X-Fixture-Credential",
                                      credential_revision="rev1")
                await pool.sync([first_config])
                first_catalog = await pool.catalog("http_fixture", "echo")
                assert [item["name"] for item in first_catalog["tools"]] == ["echo"]
                assert events(peer.log, "request", x_fixture_credential_v1=True)
                assert pool.active and pool.tools["http_fixture"]

                before = len(events(peer.log, "request"))
                lock = pool._lock("http_fixture")
                await lock.acquire()
                pending = asyncio.create_task(pool.execute(
                    "http_fixture", "echo", {"value": "must use rotated header"}))
                for _ in range(100):
                    if getattr(lock, "_waiters", None):
                        break
                    await asyncio.sleep(.01)
                assert getattr(lock, "_waiters", None)
                secret["value"] = "fixture-secret-v2"
                rotated = config(peer, name="Renamed fixture", credential_field="Authorization",
                                 credential_revision="rev2")
                sync_task = asyncio.create_task(pool.sync([rotated]))
                for _ in range(100):
                    row = pool.configs.get("http_fixture")
                    if row and row.get("credential_revision") == "rev2":
                        break
                    await asyncio.sleep(.01)
                assert pool.configs["http_fixture"]["credential_revision"] == "rev2"
                assert not sync_task.done()
                lock.release()
                sent = await asyncio.wait_for(pending, timeout=15)
                await asyncio.wait_for(sync_task, timeout=15)
                assert sent["ok"] and sent["effect"] == "completed"
                after_rotation = events(peer.log, "request")[before:]
                assert any(row["authorization_v2"] for row in after_rotation), after_rotation
                assert not any(row["x_fixture_credential_v1"] for row in after_rotation)
                assert pool.active["http_fixture"][2] == "fixture-secret-v2"
                assert reads and set(reads) == {"http_fixture"}

                # Credential revisions, header field, and display name are not
                # part of the external call's unknown-effect identity.
                action = {"value": {"effect": "unknown once"}}
                unknown = await pool.execute("http_fixture", "business_error", action)
                assert unknown["effect"] == "unknown"
                before_replay = len(calls(peer.log, "business_error"))
                before_v3 = len(events(peer.log, "request"))
                secret["value"] = "fixture-secret-v3"
                renamed_again = config(peer, name="Display only", credential_field="X-Fixture-Credential",
                                       credential_revision="rev3")
                await pool.sync([renamed_again])
                blocked = await pool.execute("http_fixture", "business_error", action)
                assert blocked["dispatch_blocked"] and blocked["prior_effect"] == "unknown"
                assert len(calls(peer.log, "business_error")) == before_replay == 1
                after_v3 = events(peer.log, "request")[before_v3:]
                assert not any(row["x_fixture_credential_v3"] for row in after_v3)
            finally:
                if lock is not None and lock.locked():
                    lock.release()
                for task in (pending, sync_task):
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_old_pre_dispatch_error_does_not_poison_same_id_new_url(
        tmp_path: Path, transport: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    unused_port = int(sock.getsockname()[1])
    sock.close()
    with LoopbackPeer(tmp_path, transport) as peer:
        old_url = f"http://127.0.0.1:{unused_port}{'/mcp' if transport == 'http' else '/sse'}"
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            try:
                failed = config(peer, url=old_url)
                await pool.sync([failed])
                first = await pool.execute("http_fixture", "echo", {"value": "before replacement"})
                assert first["effect"] == "none" and pool.errors
                # Changing only the connection target under the same ID must
                # discard an error cached for the now-obsolete refused URL.
                await pool.sync([config(peer)])
                replacement = await pool.execute("http_fixture", "echo", {"value": "new URL"})
                assert replacement["ok"] and len(calls(peer.log, "echo")) == 1
            finally:
                await pool.close()
        run(scenario)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_project_paths_and_invalid_json_are_rejected_before_tcp_connect(
        tmp_path: Path, transport: str) -> None:
    with LoopbackPeer(tmp_path, transport) as peer:
        async def scenario() -> None:
            pool = external.ExternalMcpPool()
            circular: list[Any] = []
            circular.append(circular)
            try:
                await pool.sync([config(peer)])
                cases = (
                    {"value": str(external.PROJECT_ROOT / "data" / "private.txt")},
                    {"value": ["data/private.txt"]},
                    {"value": object()},
                    {"value": float("nan")},
                    {"value": {1: "not-json-key"}},
                    {"value": circular},
                )
                for arguments in cases:
                    denied = await pool.execute("http_fixture", "echo", arguments)
                    assert not denied["ok"] and denied["effect"] == "none"
                assert not events(peer.log, "request")
                assert not pool.uncertain_calls
            finally:
                await pool.close()
        run(scenario)
