"""Real stdio regression for cached-client configuration/lock ownership.

HTTP/SSE equivalents live in test_external_mcp_http_contract.py. This test uses
only the known short-lived synthetic peer and never mocks MCP dispatch.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from coat_sim.agent import external_mcp as external
from coat_sim.core.process_identity import _process_identity

pytestmark = pytest.mark.contract
FIXTURE = Path(__file__).parent / "fixtures" / "external_mcp_stdio_server.py"


def assert_process_dead(pid: int) -> None:
    deadline = time.monotonic() + 5
    while True:
        state, _identity = _process_identity(pid)
        assert state != "unknown", f"cannot determine process state for pid {pid}"
        if state == "dead":
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"process {pid} remained alive after cleanup timeout")
        time.sleep(.02)


def config(log):
    return {"id": "active_config", "name": "Synthetic cached client", "transport": "stdio",
            "command": sys.executable, "args": [str(FIXTURE.resolve()), str(log)],
            "enabled": True, "allow_tools": True, "allow_project_data": False,
            "timeout_s": 5}


def events(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("operation", ["execute", "catalog"])
def test_queued_operation_never_reuses_active_client_of_replaced_target(tmp_path, operation):
    old_log, new_log = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        tasks = []
        lock = pool._lock("active_config")
        try:
            await pool.sync([config(old_log)])
            assert (await pool.catalog("active_config", "echo"))["tools"]
            old_client = pool.active["active_config"][0]
            # Queue the operation before sync's cleanup. sync publishes current
            # config immediately, then its cleanup waits behind this operation.
            await lock.acquire()
            if operation == "execute":
                queued = asyncio.create_task(pool.execute("active_config", "echo", {"value": "new-target-only"}))
            else:
                queued = asyncio.create_task(pool.catalog("active_config", "echo"))
            tasks.append(queued)
            await asyncio.sleep(0)
            sync = asyncio.create_task(pool.sync([{**config(new_log), "name": "Current target"}]))
            tasks.append(sync)
            await asyncio.sleep(0)
            assert pool.configs["active_config"]["args"][-1] == str(new_log)
            assert not sync.done() and not queued.done()
            lock.release()
            result = await asyncio.wait_for(queued, 10)
            await asyncio.wait_for(sync, 10)
            assert result["ok"]
            if operation == "catalog":
                assert result["servers"][0]["name"] == "Current target"
            old_calls = [e for e in events(old_log) if e["event"] == "call"]
            assert old_calls == [], "replacement configuration dispatched to the old peer"
            new_events = events(new_log)
            assert any(e["event"] == "list_tools" for e in new_events), "replacement catalog reused the old peer"
            if operation == "execute":
                assert [e["arguments"] for e in new_events if e["event"] == "call"] == [{"value": "new-target-only"}]
            assert "active_config" in pool.active, "stale sync cleanup closed the current peer"
            assert pool.active["active_config"][0] is not old_client
        finally:
            if lock.locked():
                lock.release()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.close()
        assert pool.active == {} and pool.tools == {}
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_unchanged_sync_does_not_wait_for_a_dispatched_tool(tmp_path):
    log = tmp_path / "unchanged.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        task = None
        try:
            row = config(log)
            await pool.sync([row])
            task = asyncio.create_task(pool.execute("active_config", "slow", {"value": "in-flight"}))
            async def wait_for_dispatch():
                while not any(e["event"] == "call" for e in events(log)):
                    await asyncio.sleep(.02)
            await asyncio.wait_for(wait_for_dispatch(), 8)
            await asyncio.wait_for(pool.sync([row]), 1)
            assert not task.done() and "active_config" in pool.active
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_config_changes_during_stale_client_close_do_not_open_an_old_replacement(tmp_path, monkeypatch):
    old_log, next_log, final_log = (tmp_path / name for name in ("old.jsonl", "next.jsonl", "final.jsonl"))
    async def scenario():
        pool = external.ExternalMcpPool()
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        replacement_sync = None
        lock = pool._lock("active_config")
        try:
            await pool.sync([config(old_log)])
            assert (await pool.catalog("active_config", "echo"))["tools"]
            client = pool.active["active_config"][0]
            original_close = client.close
            async def gated_close():
                entered.set()
                await release.wait()
                await original_close()
            monkeypatch.setattr(client, "close", gated_close)
            # sync publishes a replacement while queued behind execute.
            # Changing config again during execute's stale-client cleanup must
            # abort its pre-dispatch replacement connection.
            await lock.acquire()
            task = asyncio.create_task(pool.execute("active_config", "echo", {"value": "not-for-either-target"}))
            await asyncio.sleep(0)
            replacement_sync = asyncio.create_task(pool.sync([config(next_log)]))
            await asyncio.sleep(0)
            assert pool.configs["active_config"]["args"][-1] == str(next_log)
            lock.release()
            await asyncio.wait_for(entered.wait(), 3)
            await pool.sync([config(final_log)])
            release.set()
            result = await asyncio.wait_for(task, 8)
            await asyncio.wait_for(replacement_sync, 3)
            assert not result["ok"] and result["effect"] == "none"
            assert not next_log.exists() and not final_log.exists()
            assert not pool.active and not pool.uncertain_calls and not pool.errors
        finally:
            release.set()
            if lock.locked():
                lock.release()
            for pending in (task, replacement_sync):
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            await pool.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_replaced_configuration_does_not_keep_old_predispatch_error(tmp_path):
    log = tmp_path / "replacement.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            broken = {**config(log), "command": str(tmp_path / "missing-fixture-executable")}
            await pool.sync([broken])
            first = await pool.execute("active_config", "echo", {"value": "never-dispatched"})
            assert not first["ok"] and first["effect"] == "none"
            assert pool.errors and not pool.uncertain_calls and not log.exists()
            await pool.sync([config(log)])
            result = await pool.execute("active_config", "echo", {"value": "replacement"})
            assert result["ok"] and result["effect"] == "completed", "new target inherited the old connection error"
            assert [e["arguments"] for e in events(log) if e["event"] == "call"] == [{"value": "replacement"}]
        finally:
            await pool.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize("owner", ["client", "transport"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_cleanup_fault_clears_local_state_preserves_unknown_and_propagates_cancel(tmp_path, monkeypatch, owner, failure):
    """Synthetic close faults supplement the separately observed HTTP failures.

    Both wrappers first perform the real known-peer close. They inject only a
    cleanup exception, never replace the transport or MCP tools/call protocol.
    """
    log = tmp_path / "cleanup.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            result = await pool.execute("active_config", "business_error", {"value": "unknown-before-cleanup"})
            assert result["effect"] == "unknown"
            unknown = set(pool.uncertain_calls)
            client, transport, _ = pool.active["active_config"]
            original_client, original_transport = client.close, transport.close
            attempts = []
            closing = {"client": False}
            def raise_fault():
                if failure == "cancel":
                    raise asyncio.CancelledError("synthetic cleanup cancellation")
                raise OSError("synthetic cleanup transport failure")
            async def client_close():
                attempts.append("client")
                closing["client"] = True
                try:
                    await original_client()
                finally:
                    closing["client"] = False
                if owner == "client":
                    raise_fault()
            async def transport_close():
                # The SDK also calls transport.close inside client.close.
                # Inject only at the pool's explicit finally-stage attempt.
                own_attempt = not closing["client"]
                if own_attempt:
                    attempts.append("transport")
                await original_transport()
                if owner == "transport" and own_attempt:
                    raise_fault()
            monkeypatch.setattr(client, "close", client_close)
            monkeypatch.setattr(transport, "close", transport_close)
            if failure == "cancel":
                with pytest.raises(asyncio.CancelledError, match="synthetic cleanup cancellation"):
                    await pool.sync([])
            else:
                await pool.sync([])
            assert attempts == ["client", "transport"]
            assert pool.configs == {} and pool.active == {} and pool._active_configs == {}
            assert pool.tools == {} and pool.errors == {}
            assert pool.uncertain_calls == unknown and len(unknown) == 1
            pid = events(log)[0]["pid"]
            assert_process_dead(pid)
            pool.begin_turn()
            await pool.sync([config(log)])
            blocked = await pool.execute("active_config", "business_error", {"value": "unknown-before-cleanup"})
            assert blocked["dispatch_blocked"] and blocked["prior_effect"] == "unknown"
            assert len([e for e in events(log) if e["event"] == "call"]) == 1
        finally:
            await pool.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))
