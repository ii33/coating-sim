"""Machine-local successful backend observations, independent of the GUI."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import platform
from pathlib import Path
import sqlite3
import sys
import time

MEMORY_PATH = Path(__file__).resolve().parents[2] / "history" / "backend-memory.sqlite3"


def _environment_key() -> str:
    identity = "|".join((platform.node(), platform.system(), platform.machine(),
                         str(Path(sys.prefix).resolve()), platform.python_version()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@contextmanager
def _connect():
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(MEMORY_PATH, timeout=10)
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS backend_success(
            environment TEXT NOT NULL, backend TEXT NOT NULL, device TEXT NOT NULL,
            successes INTEGER NOT NULL, last_success REAL NOT NULL,
            PRIMARY KEY(environment,backend,device))""")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def record_backend_success(backend: str, device: str) -> None:
    """Record only after the actual field calculation has completed."""
    if backend not in ("GPU", "DirectML", "CPU"):
        raise ValueError("未知计算后端")
    with _connect() as db:
        db.execute("""INSERT INTO backend_success VALUES(?,?,?,?,?)
            ON CONFLICT(environment,backend,device) DO UPDATE SET
            successes=successes+1,last_success=excluded.last_success""",
            (_environment_key(), backend, str(device)[:500], 1, time.time()))


def successful_backend_memory() -> dict:
    """Observed successes are evidence, never a replacement for a live probe."""
    with _connect() as db:
        rows = db.execute("""SELECT backend,device,successes,last_success
            FROM backend_success WHERE environment=? ORDER BY last_success DESC""",
            (_environment_key(),)).fetchall()
    return {"automatic_order": ["GPU", "DirectML", "CPU"],
            "known_successes": [dict(zip(("backend", "device", "successes", "last_success"), row))
                                for row in rows[:10]],
            "scope": "current_machine_and_python_environment",
            "live_probe_required": True}
