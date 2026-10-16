"""Correlate a stable error code with one local, redacted occurrence.

The recorder is best effort: diagnostics never replace the original error.
It records error text and source locations, never tool arguments or histories.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlsplit, urlunsplit


def new_error_id() -> str:
    return uuid.uuid4().hex[:16]


def safe_error_text(value: str) -> str:
    text = str(value)[:16000]
    text = re.sub(r"(?i)\b(?:sk|ak)-[A-Za-z0-9_-]{8,}", "[凭据已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [凭据已隐藏]", text)
    text = re.sub(r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|secret|password|authorization)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", r"\1[凭据已隐藏]", text)
    text = re.sub(r"(?is)\b(?:input_value|request_body|prompt|code|script)\s*=.*$", "[调用内容省略]", text)
    text = re.sub(r"(?is)\bbody:\s*\{.*$", "body: [响应正文省略]", text)
    def clean_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group(0))
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
        except ValueError:
            return "[服务器地址已隐藏]"
    return re.sub(r"https?://[^\s\"'<>]+", clean_url, text).strip()[:3000]


def record_error(code: str, message: str, *, occurrence_id: str,
                 source: str = "", root: Path | None = None) -> None:
    try:
        if root is None:
            from .data_files import data_path
            folder = data_path("tmp/diagnostics")
        else:
            folder = Path(root) / "data" / "tmp" / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(folder / "errors.sqlite3", timeout=1)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS errors (occurrence_id TEXT PRIMARY KEY, "
                           "code TEXT NOT NULL, message TEXT NOT NULL, source TEXT NOT NULL, seen_at TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO errors VALUES (?,?,?,?,?)",
                           (occurrence_id, str(code)[:160], safe_error_text(message),
                            str(source)[:500], datetime.now(timezone.utc).isoformat()))
                db.execute("DELETE FROM errors WHERE rowid NOT IN (SELECT rowid FROM errors ORDER BY rowid DESC LIMIT 2000)")
    except (OSError, ValueError, sqlite3.Error):
        pass
