"""Explicit child-process entry points for source and frozen desktop runs."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import runpy
import sys
import time
from typing import Mapping, Sequence


_ROLE_MODULES = {
    "agent-worker": "coat_sim.agent.worker",
    "assistant": "coat_sim.assistant_process",
    "mcp-server": "coat_sim.mcp.server",
    "research-mcp": "coat_sim.mcp.research_server",
    "research-parser": "coat_sim.agent.research_parse_worker",
    "mcp-desktop-endpoint": "coat_sim.mcp.desktop_endpoint",
    "scheduler": "coat_sim.scheduler",
    "settings": "coat_sim.ui.settings",
    "project-runtime": "coat_sim.ui.project_runtime",
    "appearance-freeze": "coat_sim.core.appearance_freeze_worker",
    "appearance-remote": "coat_sim.ui.appearance_remote_worker",
}
_MODULE_ROLES = {module: role for role, module in _ROLE_MODULES.items()}
MAX_QA_APP_STARTS = 8
_QA_COUNTER_BYTES = 16


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def module_command(
    module: str,
    *arguments: str,
    interpreter: str | None = None,
    unbuffered: bool = False,
) -> list[str]:
    """Build a module command without treating the frozen app as Python."""
    args = [str(value) for value in arguments]
    if is_frozen():
        role = _MODULE_ROLES.get(module)
        if role is None:
            raise ValueError(f"No frozen child-process role is registered for {module!r}.")
        if interpreter is not None and Path(interpreter).resolve() != Path(sys.executable).resolve():
            raise ValueError("A frozen child role must use the current application executable.")
        return [sys.executable, "--coat-worker", role, *args]

    command = [interpreter or sys.executable]
    if unbuffered:
        command.append("-u")
    return [*command, "-m", module, *args]


def dispatch_frozen_child(entry_script: str | Path, argv: Sequence[str] | None = None) -> bool:
    """Dispatch only registered frozen roles; return False for the GUI entry."""
    if not is_frozen():
        return False
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "--coat-worker":
        if arguments and arguments[0] in ("-m", "-c", "-u"):
            raise SystemExit("Frozen CoatSim is not a general Python interpreter; use a registered child role.")
        return False
    if len(arguments) < 2:
        raise SystemExit("Missing frozen child-process role.")
    module = _ROLE_MODULES.get(arguments[1])
    if module is None:
        raise SystemExit(f"Unknown frozen child-process role: {arguments[1]}")
    sys.argv = [module, *arguments[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return True


def run_frozen_entry(entry_script: str | Path) -> None:
    """Handle multiprocessing's Windows child arguments before starting Qt."""
    multiprocessing.freeze_support()
    if dispatch_frozen_child(entry_script):
        return
    runpy.run_path(str(entry_script), run_name="__main__")


def consume_qa_start_slot(environ: Mapping[str, str] | None = None) -> bool:
    """Atomically bound desktop-entry starts in the isolated QA process tree."""
    environment = os.environ if environ is None else environ
    counter_name = environment.get("COAT_QA_START_COUNTER")
    limit_text = environment.get("COAT_QA_START_LIMIT")
    if counter_name is None and limit_text is None:
        return True
    if counter_name is None or limit_text is None or sys.platform != "win32":
        return False
    try:
        limit = int(limit_text)
        if not 1 <= limit <= MAX_QA_APP_STARTS:
            return False
        import msvcrt

        deadline = time.monotonic() + 5.0
        with Path(counter_name).open("r+b", buffering=0) as stream:
            locked = False
            while not locked:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.025)
            try:
                raw_count = stream.read(_QA_COUNTER_BYTES).strip()
                count = int(raw_count or b"0")
                if count >= limit:
                    return False
                stream.seek(0)
                stream.write(f"{count + 1:0{_QA_COUNTER_BYTES}d}".encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
                return True
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    except (OSError, ValueError, OverflowError):
        return False
