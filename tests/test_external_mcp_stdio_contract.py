"""Actual local stdio MCP transport; no live service or model provider."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
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


def config(log: Path, **overrides):
    return {"id": "stdio_fixture", "name": "Offline fixture", "transport": "stdio",
            "command": sys.executable, "args": [str(FIXTURE.resolve()), str(log)],
            "enabled": True, "allow_tools": True, "allow_project_data": False,
            "timeout_s": 5, **overrides}


def events(log: Path):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def calls(log: Path, name: str):
    return [row for row in events(log) if row["event"] == "call" and row["name"] == name]


async def wait_for_call(log: Path, name: str):
    async def wait():
        while not calls(log, name):
            await asyncio.sleep(.02)
    await asyncio.wait_for(wait(), timeout=8)


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), timeout=30))


@pytest.mark.parametrize("gates", [{"enabled": False}, {"allow_tools": False},
                                  {"enabled": "true"}, {"allow_tools": 1}])
def test_each_missing_strict_gate_has_zero_stdio_startups(tmp_path, gates):
    log = tmp_path / "events.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log, **gates)])
            assert (await pool.catalog())["tools"] == []
            specific = await pool.catalog("stdio_fixture")
            assert specific.get("tools", []) == []
            denied = await pool.execute("stdio_fixture", "echo", {"value": "only-explicit"})
            assert denied["effect"] == "none" and not denied["ok"]
            assert not log.exists()
            assert pool.active == {}
        finally:
            await pool.close()
    run(scenario)


def test_real_stdio_catalog_exact_arguments_empty_roots_and_no_provider_environment(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COAT_SIM_ASSISTANT_API_KEY",
                "COAT_SIM_FIXTURE_HISTORY", "COAT_SIM_FIXTURE_ATTACHMENT"):
        monkeypatch.setenv(key, "synthetic-do-not-forward")
    from coat_sim.ui import assistant_credentials
    monkeypatch.setattr(assistant_credentials, "read_mcp_secret",
                        lambda _id: pytest.fail("no external credential was configured"))
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            overview = await pool.catalog()
            assert overview["tools"] == [] and not log.exists()
            catalog = await pool.catalog("stdio_fixture", "echo")
            assert [item["name"] for item in catalog["tools"]] == ["echo"]
            assert catalog["tools"][0]["input_schema"] == {
                "type": "object", "properties": {"value": {}}, "required": ["value"],
                "additionalProperties": False}
            args = {"value": {"message": "explicit only", "count": 2}}
            result = await pool.execute("stdio_fixture", "echo", args)
            assert result["ok"] and result["effect"] == "completed"
            payload = json.loads(result["result"]["content"][0]["text"])
            assert payload["arguments"] == args and payload["roots"] == []
            assert isinstance(payload["roots_back_channel"], bool)
            # On modern stdio the peer cannot ask for roots. In either era the
            # actual configured client callback must expose no project roots.
            roots_callback = pool.active["stdio_fixture"][0]._session_kwargs["list_roots_callback"]
            assert (await roots_callback(None)).roots == []
            assert calls(log, "echo") == [{"event": "call", "name": "echo", "arguments": args}]
            startup = events(log)[0]
            assert startup["cwd"] == tempfile.gettempdir()
            assert startup["provider_environment_keys"] == []
        finally:
            await pool.close()
        assert pool.active == {} and pool.tools == {}
        pid = events(log)[0]["pid"]
        assert_process_dead(pid)
    run(scenario)


def test_project_path_and_non_json_rejection_precede_any_connection(tmp_path):
    log = tmp_path / "events.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            circular = []
            circular.append(circular)
            for args in ({"value": str(external.PROJECT_ROOT / "data" / "private.txt")},
                         {"value": ["data/private.txt"]}, {"value": object()},
                         {"value": float("nan")}, {"value": {1: "not-json-key"}},
                         {"value": circular}):
                denied = await pool.execute("stdio_fixture", "echo", args)
                assert not denied["ok"] and denied["effect"] == "none"
            assert not log.exists()
            assert not pool.uncertain_calls
        finally:
            await pool.close()
    run(scenario)


@pytest.mark.parametrize("name", ["business_error", "disconnect", "slow"],
                         ids=["peer-error", "transport-loss", "dispatch-cancel"])
def test_unknown_dispatch_is_not_replayed_after_new_turn_or_reconnect(tmp_path, name):
    log = tmp_path / "events.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        args = {"value": {"alpha": 1, "beta": "sent-once"}}
        try:
            await pool.sync([config(log)])
            if name == "slow":
                task = asyncio.create_task(pool.execute("stdio_fixture", name, args))
                await wait_for_call(log, name)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            else:
                first = await pool.execute("stdio_fixture", name, args)
                assert not first["ok"] and first["effect"] == "unknown"
            assert len(calls(log, name)) == 1
            pool.begin_turn()
            # A permission toggle closes/reopens the connection, not the guard.
            await pool.sync([config(log, enabled=False)])
            await pool.sync([config(log)])
            # JSON object insertion order cannot disguise the same call.
            blocked = await pool.execute("stdio_fixture", name,
                                         {"value": {"beta": "sent-once", "alpha": 1}})
            assert not blocked["ok"] and blocked["dispatch_blocked"]
            assert blocked["effect"] == "none" and blocked["prior_effect"] == "unknown"
            assert len(calls(log, name)) == 1
            other = await pool.execute("stdio_fixture", "echo", {"value": "read-for-reconciliation"})
            assert other["ok"] and len(calls(log, "echo")) == 1
            if name == "business_error":
                distinct = await pool.execute("stdio_fixture", name, {"value": "different-action"})
                assert distinct["effect"] == "unknown"
                assert len(calls(log, name)) == 2
        finally:
            await pool.close()
        for row in events(log):
            if row["event"] == "startup":
                assert_process_dead(row["pid"])
    run(scenario)


def test_unknown_capacity_fails_closed_without_evicting_existing_guard(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setattr(external, "MAX_UNCERTAIN_CALLS", 1)
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            first = await pool.execute("stdio_fixture", "business_error", {"value": "one"})
            assert first["effect"] == "unknown"
            denied = await pool.execute("stdio_fixture", "echo", {"value": "two"})
            assert denied["effect"] == "none" and denied["code"] == "uncertain_capacity_exceeded"
            pool.begin_turn()
            blocked = await pool.execute("stdio_fixture", "business_error", {"value": "one"})
            assert blocked["dispatch_blocked"] and blocked["prior_effect"] == "unknown"
            assert len(calls(log, "business_error")) == 1 and not calls(log, "echo")
            assert len(pool.uncertain_calls) == 1
        finally:
            await pool.close()
    run(scenario)


def test_revocation_while_catalog_waits_for_lock_cannot_connect(tmp_path):
    log = tmp_path / "events.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            lock = pool._lock("stdio_fixture")
            await lock.acquire()
            pending = asyncio.create_task(pool.catalog("stdio_fixture"))
            await asyncio.sleep(0)
            await pool.sync([config(log, allow_tools=False)])
            lock.release()
            catalog = await asyncio.wait_for(pending, timeout=5)
            assert catalog["tools"] == []
            assert not log.exists()
            denied = await pool.execute("stdio_fixture", "echo", {"value": "one"})
            assert denied["effect"] == "none" and not pool.uncertain_calls
        finally:
            await pool.close()
    run(scenario)


def test_same_id_replacement_target_has_distinct_identity(tmp_path):
    first_log, replacement_log = tmp_path / "first.jsonl", tmp_path / "replacement.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(first_log)])
            assert (await pool.execute("stdio_fixture", "business_error", {"value": "one"}))["effect"] == "unknown"
            await pool.sync([config(replacement_log)])
            assert (await pool.execute("stdio_fixture", "business_error", {"value": "one"}))["effect"] == "unknown"
            assert len(calls(first_log, "business_error")) == len(calls(replacement_log, "business_error")) == 1
            await pool.sync([config(first_log)])
            assert (await pool.execute("stdio_fixture", "business_error", {"value": "one"}))["dispatch_blocked"]
            assert len(calls(first_log, "business_error")) == 1
        finally:
            await pool.close()
    run(scenario)


def test_known_pre_dispatch_connection_failure_retries_only_after_new_turn(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    connection = external._connection
    attempts = []
    def fail_first(row):
        attempts.append(row["id"])
        if len(attempts) == 1:
            raise OSError("synthetic connection failure before dispatch")
        return connection(row)
    monkeypatch.setattr(external, "_connection", fail_first)
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            first = await pool.execute("stdio_fixture", "echo", {"value": "one"})
            assert first["effect"] == "none" and not pool.uncertain_calls and not log.exists()
            assert (await pool.execute("stdio_fixture", "echo", {"value": "one"}))["effect"] == "none"
            assert len(attempts) == 1
            pool.begin_turn()
            assert (await pool.execute("stdio_fixture", "echo", {"value": "one"}))["ok"]
            assert len(attempts) == 2 and len(calls(log, "echo")) == 1
        finally:
            await pool.close()
    run(scenario)


def test_real_stdio_timeout_records_unknown_and_never_replays(tmp_path):
    log = tmp_path / "events.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            await pool.sync([config(log)])
            first = await pool.execute("stdio_fixture", "slow", {"value": "timeout"})
            assert first["effect"] == "unknown" and not first["ok"]
            assert len(calls(log, "slow")) == 1 and not pool._inflight_calls
            pool.begin_turn()
            blocked = await pool.execute("stdio_fixture", "slow", {"value": "timeout"})
            assert blocked["dispatch_blocked"] and blocked["prior_effect"] == "unknown"
            assert len(calls(log, "slow")) == 1
        finally:
            await pool.close()
    run(scenario)


def test_inflight_capacity_is_reserved_across_different_servers(tmp_path, monkeypatch):
    first_log, second_log = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    monkeypatch.setattr(external, "MAX_UNCERTAIN_CALLS", 1)
    async def scenario():
        pool = external.ExternalMcpPool()
        task = None
        try:
            await pool.sync([config(first_log), config(second_log, id="second_fixture")])
            task = asyncio.create_task(pool.execute("stdio_fixture", "slow", {"value": "in-flight"}))
            await wait_for_call(first_log, "slow")
            denied = await pool.execute("second_fixture", "echo", {"value": "two"})
            assert denied["code"] == "uncertain_capacity_exceeded" and denied["effect"] == "none"
            assert not second_log.exists()
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


def test_arguments_are_frozen_before_connection_await(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        ensure = pool._ensure_locked
        entered, release = asyncio.Event(), asyncio.Event()
        async def gated_ensure(identifier):
            entered.set()
            await release.wait()
            return await ensure(identifier)
        monkeypatch.setattr(pool, "_ensure_locked", gated_ensure)
        original = {"value": {"before": "frozen"}}
        task = None
        try:
            await pool.sync([config(log)])
            task = asyncio.create_task(pool.execute("stdio_fixture", "business_error", original))
            await asyncio.wait_for(entered.wait(), timeout=3)
            original["value"]["before"] = "mutated while waiting"
            release.set()
            assert (await task)["effect"] == "unknown"
            assert calls(log, "business_error")[0]["arguments"] == {"value": {"before": "frozen"}}
            pool.begin_turn()
            blocked = await pool.execute("stdio_fixture", "business_error", {"value": {"before": "frozen"}})
            assert blocked["dispatch_blocked"] and len(calls(log, "business_error")) == 1
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.close()
    run(scenario)


@pytest.mark.parametrize("change", ["revoke", "replace"])
def test_configuration_change_during_real_connection_never_dispatches_stale_call(tmp_path, monkeypatch, change):
    old_log, new_log = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    async def scenario():
        pool = external.ExternalMcpPool()
        connection = external._connection
        entered, release = asyncio.Event(), asyncio.Event()
        def gated_connection(row):
            client, transport, secret = connection(row)
            original_list = client.list_tools
            async def gated_list(**kwargs):
                result = await original_list(**kwargs)
                entered.set()
                await release.wait()
                return result
            client.list_tools = gated_list
            return client, transport, secret
        monkeypatch.setattr(external, "_connection", gated_connection)
        task = None
        try:
            await pool.sync([config(old_log)])
            task = asyncio.create_task(pool.execute("stdio_fixture", "echo", {"value": "old-target"}))
            await asyncio.wait_for(entered.wait(), timeout=8)
            updated = config(old_log, allow_tools=False) if change == "revoke" else config(new_log)
            await pool.sync([updated])
            release.set()
            denied = await asyncio.wait_for(task, timeout=5)
            assert not denied["ok"] and denied["effect"] == "none"
            assert not calls(old_log, "echo") and not new_log.exists()
            assert not pool.active and not pool.uncertain_calls and not pool.errors
            # The new explicit configuration is usable; an old failed attempt
            # must not poison the replacement target's connection state.
            if change == "replace":
                assert (await pool.execute("stdio_fixture", "echo", {"value": "new-target"}))["ok"]
                assert len(calls(new_log, "echo")) == 1
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.close()
    run(scenario)
