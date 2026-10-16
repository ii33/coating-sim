"""Project-local SQLite catalog for named optical film-design documents."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

from coat_sim.core.film_design import validate_design


class FilmDesignStore:
    """Store complete schema-v1 designs, with searchable names and notes.

    The caller supplies an already root-confined database path. A connection is
    opened per operation so window lifetimes and concurrent readers do not keep
    an implicit transaction alive.
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
                CREATE TABLE IF NOT EXISTS film_designs (
                    design_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    note TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS film_designs_updated ON film_designs(updated_at DESC)")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _decode(value):
        try:
            return validate_design(json.loads(value))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValueError("stored film design is corrupt or uses an unsupported schema") from exc

    def list(self):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT design_id,name,note,schema_version,created_at,updated_at "
                "FROM film_designs ORDER BY updated_at DESC, design_id COLLATE BINARY"
            ).fetchall()
        return [dict(row) for row in rows]

    def save(self, design):
        normalized = validate_design(deepcopy(design))
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        payload = json.dumps(normalized, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                    INSERT INTO film_designs(design_id,name,note,schema_version,document_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(design_id) DO UPDATE SET
                        name=excluded.name, note=excluded.note,
                        schema_version=excluded.schema_version,
                        document_json=excluded.document_json,
                        updated_at=excluded.updated_at
                """, (normalized["id"], normalized["name"], normalized["note"],
                      normalized["schema_version"], payload, stamp, stamp))
        return self.load(normalized["id"])

    def load(self, design_id):
        if not isinstance(design_id, str) or not design_id or len(design_id) > 128:
            raise ValueError("design_id must be a non-empty string up to 128 characters")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT document_json FROM film_designs WHERE design_id=? COLLATE BINARY",
                (design_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"saved film design not found: {design_id}")
        return self._decode(row["document_json"])
