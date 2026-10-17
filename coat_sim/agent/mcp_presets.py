"""Ordinary MCP service seeds and conservative client-side call policy.

Seeds contain no secrets and do not connect. Local adapters are project code,
not official provider MCP endpoints. Usage is local operational state only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
USAGE_PATH = ROOT / "artifacts" / "external-mcp-usage.sqlite3"
SEED_VERSION = 1
ZHIHU_URL = "https://developer.zhihu.com/api/mcp/v1"
TOOLS = {
    "zhihu": ("zhihu_search", "global_search"),
    "openalex": ("research_search", "research_lookup", "research_fulltext"),
    "crossref": ("research_search", "research_lookup"),
}


def preset_kind(config: Mapping[str, Any]) -> str:
    kind = config.get("preset_id", "")
    if config.get("url", "").rstrip("/") == ZHIHU_URL:
        return "zhihu"
    return str(kind) if kind in TOOLS else ""


def service_seeds() -> list[dict[str, Any]]:
    """Return disabled ordinary rows; callers preserve existing IDs and edits."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").is_file():
        executable = executable.with_name("python.exe")
    rows = []
    for provider, label, field, limit in (
        ("zhihu", "知乎搜索", "Authorization", 6),
        ("openalex", "OpenAlex（项目适配器）", "COAT_RESEARCH_API_KEY", 6),
        ("crossref", "Crossref（项目适配器）", "COAT_RESEARCH_MAILTO", 12),
    ):
        launch: list[str] = []
        if provider != "zhihu":
            if getattr(sys, "frozen", False):
                from coat_sim.process_entry import module_command
                launch = module_command("coat_sim.mcp.research_server", "--provider", provider)
            else:
                launch = [str(executable), str(ROOT / "coat_sim" / "mcp" / "research_server.py"),
                          "--provider", provider]
        rows.append({"id": "preset_" + provider, "preset_id": provider, "name": label,
            "transport": "http" if provider == "zhihu" else "stdio",
            "url": ZHIHU_URL if provider == "zhihu" else "",
            "command": launch[0] if launch else "",
            "args": launch[1:],
            "cwd": "", "enabled": False, "allow_tools": False,
            "allow_project_data": False, "credential_field": field,
            "credential_revision": "", "timeout_s": 30,
            "daily_call_limit": limit, "allow_paid_calls": False})
    return rows


def seed_servers(rows: list[dict[str, Any]], version: object) -> list[dict[str, Any]]:
    if isinstance(version, int) and not isinstance(version, bool) and version == SEED_VERSION:
        return rows
    identifiers = {row["id"] for row in rows}
    # Preserve all existing rows, including a user-created row with a seed ID.
    return rows + [row for row in service_seeds() if row["id"] not in identifiers][:max(0, 20 - len(rows))]


def service_description(config: Mapping[str, Any]) -> str:
    kind = preset_kind(config)
    return {
        "zhihu": "知乎官方 MCP · 仅站内/全网搜索；限免用完可能从余额扣费。参考费率：全网12元/千次、站内20元/千次，以官网为准：https://developer.zhihu.com/docs?key=billing_price 。连接检查只初始化并列工具，不验证业务额度。",
        "openalex": "项目 API 适配器，非官方 MCP。API key 可留空作受限元数据查询；全文需要 key。仅发送明确 query/ID，不自动上传项目或聊天。",
        "crossref": "项目 API 适配器，非官方 MCP。公共元数据无需 key；凭据框仅填写用户自愿提供的联系邮箱（mailto），不自动读取系统邮箱。模糊匹配不等于 DOI 已核实。",
    }.get(kind, "")


def tool_allowed(config: Mapping[str, Any], name: str) -> bool:
    kind = preset_kind(config)
    return not kind or name in TOOLS[kind]


def reserve_call(config: Mapping[str, Any]) -> None:
    """Charge one possible dispatch atomically, including unknown outcomes.

    A failed/locked usage store denies the dispatch. Catalog probes never call
    this function. The cap is a request cap, not provider billing reconciliation.
    """
    kind = preset_kind(config)
    if not kind:
        return
    if kind == "zhihu" and config.get("allow_paid_calls") is not True:
        raise ValueError("请先在服务编辑中确认可能计费，并设置每日调用上限。")
    limit = config.get("daily_call_limit", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("此服务每日调用上限为零，未派发调用。")
    # All rows for the same provider share a cap counter across IDs/workers.
    scope = hashlib.sha256(json.dumps(kind).encode()).hexdigest()
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(USAGE_PATH, timeout=0.2)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS usage (scope TEXT PRIMARY KEY, day TEXT NOT NULL, count INTEGER NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT day,count FROM usage WHERE scope=?", (scope,)).fetchone()
            used = row[1] if row and row[0] == day else 0
            if used >= limit:
                raise ValueError("已达此服务每日调用上限；未派发调用。")
            db.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?)", (scope, day, used + 1))
    except (sqlite3.Error, OSError) as exc:
        raise ValueError("调用预算存储不可用；未派发调用。") from exc
