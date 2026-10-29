"""Offline ordinary-service migration, allowlist and persistent call limits."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coat_sim.agent import external_mcp as external
from coat_sim.agent import mcp_presets as presets


def configured(kind="zhihu", **overrides):
    row = next(row for row in presets.service_seeds() if row["preset_id"] == kind)
    return external.validate_server({**row, "enabled": True, "allow_tools": True,
                                     "allow_paid_calls": True, **overrides})


def test_seeds_are_disabled_ordinary_rows_and_deletion_is_not_recreated():
    from coat_sim.ui.settings import normalized_ui_settings
    values = normalized_ui_settings({})
    rows = cast(list[dict[str, Any]], values["assistant_mcp_servers"])
    assert {row["preset_id"] for row in rows} == {"zhihu", "openalex", "crossref"}
    assert all(not row["enabled"] and not row["allow_tools"] and not row["allow_project_data"] for row in rows)
    assert all(not row["credential_revision"] and not row["cwd"] for row in rows)
    assert all("-c" not in row.get("args", []) for row in rows)
    assert any("--provider" in row.get("args", []) for row in rows)
    values["assistant_mcp_servers"] = []
    assert normalized_ui_settings(values)["assistant_mcp_servers"] == []
    # Migration preserves a colliding user row, with edits, and remains bounded.
    edited = {**rows[0], "name": "My edited service", "url": "https://example.invalid/mcp"}
    migrated = presets.seed_servers([edited], 0)
    assert migrated[0] is edited and len(migrated) == 3
    assert presets.seed_servers(migrated, 1) is migrated


@pytest.mark.parametrize("limit", [-1, 10001, "broken"])
def test_invalid_daily_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        configured(daily_call_limit=limit)


def test_zhihu_policy_also_applies_to_new_row_for_official_endpoint():
    row = configured(preset_id="", id="ordinary_copy")
    assert presets.preset_kind(row) == "zhihu"
    assert presets.tool_allowed(row, "zhihu_search")
    assert not presets.tool_allowed(row, "hot_list")
    assert not presets.tool_allowed(row, "zhida")


def test_budget_is_durable_shared_across_ids_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "USAGE_PATH", tmp_path / "usage.sqlite3")
    row = configured(daily_call_limit=1)
    def attempt(identifier):
        try:
            presets.reserve_call({**row, "id": identifier})
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("one", "two")))
    assert results.count(True) == 1
    with pytest.raises(ValueError, match="上限"):
        presets.reserve_call(row)
    # A new pool/process still sees the same persisted counter; no refund on
    # provider error or cancellation can silently allow another paid request.
    monkeypatch.setattr(presets, "USAGE_PATH", tmp_path)  # directory, not a DB
    with pytest.raises(ValueError, match="存储不可用"):
        presets.reserve_call(row)


def test_paid_consent_and_zero_budget_deny_without_store(tmp_path, monkeypatch):
    path = tmp_path / "usage.sqlite3"
    monkeypatch.setattr(presets, "USAGE_PATH", path)
    with pytest.raises(ValueError, match="计费"):
        presets.reserve_call(configured(allow_paid_calls=False))
    with pytest.raises(ValueError, match="上限为零"):
        presets.reserve_call(configured(daily_call_limit=0))
    assert not path.exists()


class Client:
    def __init__(self):
        self.calls = []
        self.entered = 0
        self.closed = 0
    async def __aenter__(self):
        self.entered += 1
        return self
    async def close(self):
        self.closed += 1
    async def list_tools(self, **_):
        return [SimpleNamespace(name=name, description="untrusted description", annotations=None,
                                inputSchema={"type": "object"})
                for name in ("zhihu_search", "global_search", "hot_list", "zhida")]
    async def call_tool_mcp(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, model_dump=lambda **_: {"content": []})


def test_probe_only_lists_and_never_reserves_or_calls(tmp_path, monkeypatch):
    path = tmp_path / "usage.sqlite3"
    monkeypatch.setattr(presets, "USAGE_PATH", path)
    client = Client()
    monkeypatch.setattr(external, "_connection", lambda _: (client, client, ""))
    row = configured(enabled=False, allow_tools=False, allow_paid_calls=False)
    result = asyncio.run(external.probe_server(row))
    assert result["ok"] and result["tools"] == ["zhihu_search", "global_search"]
    assert "不验证业务额度" in result["message"]
    assert client.calls == [] and client.closed > 0 and not path.exists()


def test_catalog_filter_and_execute_allowlist_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "USAGE_PATH", tmp_path / "usage.sqlite3")
    client = Client()
    monkeypatch.setattr(external, "_connection", lambda _: (client, client, "synthetic-only-secret"))
    async def scenario():
        pool = external.ExternalMcpPool()
        try:
            row = configured(daily_call_limit=1)
            await pool.sync([row])
            blocked = await pool.execute(row["id"], "hot_list", {})
            assert blocked["code"] == "tool_not_allowed" and client.entered == 0
            catalog = await pool.catalog(row["id"])
            assert [tool["name"] for tool in catalog["tools"]] == ["zhihu_search", "global_search"]
            first = await pool.execute(row["id"], "zhihu_search", {"query": "explicit query"})
            second = await pool.execute(row["id"], "global_search", {"query": "different query"})
            assert first["ok"] and second["code"] == "call_budget_denied"
            assert second["effect"] == "none" and len(client.calls) == 1
            await pool.sync([{**row, "enabled": False}])
            assert not (await pool.execute(row["id"], "zhihu_search", {}))["ok"]
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_frozen_seeds_use_registered_role_not_python_script(monkeypatch):
    monkeypatch.setattr(presets.sys, "frozen", True, raising=False)
    rows = presets.service_seeds()
    for row in rows[1:]:
        assert row["args"][:2] == ["--coat-worker", "research-mcp"]
        assert row["args"][2:] == ["--provider", row["preset_id"]]
        assert row["cwd"] == "" and not row["allow_project_data"]


def test_pythonw_seeds_prefer_console_interpreter(tmp_path, monkeypatch):
    pythonw = tmp_path / "pythonw.exe"
    python = tmp_path / "python.exe"
    pythonw.touch()
    python.touch()
    monkeypatch.setattr(presets.sys, "executable", str(pythonw))
    assert all(row["command"] == str(python) for row in presets.service_seeds()[1:])


def test_saved_seed_deletion_and_user_edits_survive_disk_reload(tmp_path, monkeypatch):
    from coat_sim.ui import settings
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "ui_settings.json")
    values = settings.load_ui_settings()
    first = cast(list[dict[str, Any]], values["assistant_mcp_servers"])[0]
    first["name"] = "A user name"
    saved = settings.save_ui_settings(values)
    assert saved["assistant_mcp_seed_version"] == presets.SEED_VERSION
    loaded_rows = cast(list[dict[str, Any]], settings.load_ui_settings()["assistant_mcp_servers"])
    assert loaded_rows[0]["name"] == "A user name"
    saved["assistant_mcp_servers"] = []
    settings.save_ui_settings(saved)
    assert settings.load_ui_settings()["assistant_mcp_servers"] == []
