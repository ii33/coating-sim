"""Standalone SQLite persistence for reusable film-stack expressions."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import uuid

from coat_sim.core.film_design import expand_expression, new_design, validate_design

TEMPLATE_SCHEMA_VERSION = 1


def _normalize_template(template):
    if not isinstance(template, Mapping):
        raise ValueError("expression template must be an object")
    allowed = {"schema_version", "template_id", "name", "note", "summary", "expression"}
    unknown = set(template) - allowed
    if unknown:
        raise ValueError("unknown expression template fields: " + ", ".join(sorted(unknown)))
    version = template.get("schema_version", TEMPLATE_SCHEMA_VERSION)
    if type(version) is not int or version != TEMPLATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported expression template schema_version {version!r}")

    template_id = template.get("template_id") or uuid.uuid4().hex
    if not isinstance(template_id, str) or not template_id.strip() or len(template_id) > 128:
        raise ValueError("template_id must be non-empty text up to 128 characters")
    name = template.get("name")
    if not isinstance(name, str) or len(name) > 120 or not name.strip():
        raise ValueError("expression template name must be non-empty text up to 120 characters")
    note = template.get("note", "")
    if not isinstance(note, str) or len(note) > 20000:
        raise ValueError("expression template note must be text up to 20000 characters")

    expression = template.get("expression")
    if not isinstance(expression, Mapping) or set(expression) - {"text", "symbols", "in_sync"}:
        raise ValueError("expression must contain text and symbols")
    if "text" not in expression or "symbols" not in expression:
        raise ValueError("expression must contain text and symbols")
    if "in_sync" in expression and not isinstance(expression["in_sync"], bool):
        raise ValueError("expression in_sync must be a boolean when supplied")

    draft = new_design(name="Expression template validation", design_id="expression-template-validation")
    draft["expression"] = {"text": expression["text"], "symbols": expression["symbols"], "in_sync": True}
    normalized_expression = validate_design(draft)["expression"]
    expand_expression(normalized_expression["text"], normalized_expression["symbols"])
    result = {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "template_id": template_id.strip(),
        "name": name.strip(),
        "note": note,
        "expression": {
            "text": normalized_expression["text"],
            "symbols": normalized_expression["symbols"],
        },
    }
    if "summary" in template:
        summary = template["summary"]
        if not isinstance(summary, str) or len(summary) > 20000:
            raise ValueError("expression template summary must be text up to 20000 characters")
        result["summary"] = summary
    return result


class FilmExpressionTemplateStore:
    """Store named expression templates separately from complete film designs.

    The templates share the design library SQLite file but use a distinct table;
    the existing ``film_designs`` schema and saved rows remain unchanged.
    """

    def __init__(self, database_path):
        self.path = Path(database_path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("film-design database may not be a symbolic link")
        connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS film_expression_templates (
                    template_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    note TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    expression_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS film_expression_templates_updated "
                "ON film_expression_templates(updated_at DESC)"
            )
            return connection
        except Exception:
            connection.close()
            raise

    def list(self):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT template_id,name,note,schema_version,created_at,updated_at "
                "FROM film_expression_templates "
                "ORDER BY updated_at DESC, template_id COLLATE BINARY"
            ).fetchall()
        return [dict(row) for row in rows]

    def save(self, template):
        normalized = _normalize_template(template)
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        payload = json.dumps(normalized, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                    INSERT INTO film_expression_templates(
                        template_id,name,note,schema_version,expression_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(template_id) DO UPDATE SET
                        name=excluded.name, note=excluded.note,
                        schema_version=excluded.schema_version,
                        expression_json=excluded.expression_json,
                        updated_at=excluded.updated_at
                """, (normalized["template_id"], normalized["name"], normalized["note"],
                      normalized["schema_version"], payload, stamp, stamp))
        return self.load(normalized["template_id"])

    def load(self, template_id):
        if not isinstance(template_id, str) or not template_id or len(template_id) > 128:
            raise ValueError("template_id must be non-empty text up to 128 characters")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT expression_json FROM film_expression_templates "
                "WHERE template_id=? COLLATE BINARY", (template_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"saved expression template not found: {template_id}")
        try:
            raw = json.loads(row["expression_json"])
            return _normalize_template(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("stored expression template is corrupt or unsupported") from exc
