"""Native Windows integration for coordinated app and scheduler exit."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import time
from threading import Event, Thread
from typing import Any, Callable

import pytest

from coat_sim.assistant_process import AssistantProcessClient, LIFECYCLE_OPERATIONS
from coat_sim.core.process_identity import _process_identity
from coat_sim.core.project_store import ProjectStore
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.lifecycle import registration_path, registry_directory
from coat_sim.ui.control import OPERATIONS, UiControlClient, UiControlError
from tests.test_application_process_smoke import _SUPPORT

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def _live(identity: tuple[int, str]) -> bool:
    state, started = _process_identity(identity[0])
    return state == "alive" and started == identity[1]


def test_native_global_exit_preserves_cancel_then_closes_exact_owned_processes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt" or os.environ.get("COAT_RUN_NATIVE_LIFECYCLE") != "1":
        pytest.skip("Set COAT_RUN_NATIVE_LIFECYCLE=1 on Windows for native desktop acceptance")

    source = Path(__file__).resolve().parents[1]
    support, state = tmp_path / "support", tmp_path / "state"
    runtime, compute_root = tmp_path / "assistant-runtime", state / "compute-root"
    support.mkdir()
    state.mkdir()
    support_code = _SUPPORT.replace(
        "    desktop.DesktopAssistantBridge.refreshQueue = lambda self: None\n", "")
    assert support_code != _SUPPORT, "Smoke support no longer contains the queue-poll suppression"
    support_code = support_code.replace(
        "    desktop.CURSOR_PATH = state / 'desktop-cursor.json'\n",
        "    desktop.CURSOR_PATH = state / 'desktop-cursor.json'\n"
        "    desktop.ROOT = state / 'compute-root'\n")
    support_code = support_code.replace(
        "'review_kind': review_kind, 'close_project_id': close_id,",
        "'review_kind': review_kind, 'shutdown_dialog_visible': bool(dialog and dialog.isVisible()), "
        "'close_project_id': close_id,")
    support_code += "\nif os.environ.get('COAT_LIFECYCLE_ACTIVATION_ADAPTER') == '1':\n" \
        "    import coat_sim.assistant_process as _assistant_process\n" \
        "    _assistant_process._allow_foreground_process = lambda _pid: True\n"
    assert "'shutdown_dialog_visible': bool(dialog and dialog.isVisible())" in support_code
    (support / "sitecustomize.py").write_text(support_code, encoding="utf-8", newline="\n")
    (state / "allow-registration").touch()

    first = ProjectStore.create(tmp_path / "Project one", "Project one")
    second = ProjectStore.create(tmp_path / "Project two", "Project two")
    environment = os.environ.copy()
    for key in ("COAT_UI_PID", "COAT_UI_PARENT_PID", "COAT_UI_READY_FD", "COAT_UI_READY_TOKEN",
                "COAT_UI_READY_ADDRESS", "COAT_UI_READY_FAMILY", "COAT_UI_READY_KEY"):
        environment.pop(key, None)
    environment.update({
        "QT_QPA_PLATFORM": "windows", "QT_QUICK_BACKEND": "software",
        "PYTHONUNBUFFERED": "1",
        "COAT_TEST_APPLICATION_SMOKE": "1", "COAT_TEST_APPLICATION_STATE": str(state),
        "COAT_ASSISTANT_RUNTIME_ROOT": str(runtime),
        "APPDATA": str(state / "roaming"), "LOCALAPPDATA": str(state / "local"),
        "XDG_CONFIG_HOME": str(state / "config"), "XDG_DATA_HOME": str(state / "data"),
        "XDG_CACHE_HOME": str(state / "cache"),
        "PYTHONPATH": os.pathsep.join((str(support), str(source))),
            # This acceptance adapter isolates lifecycle assertions from the
            # Windows foreground-grant policy; native mode never enables it.
            "COAT_LIFECYCLE_ACTIVATION_ADAPTER": "1" if os.environ.get(
                "COAT_LIFECYCLE_ACTIVATION_ADAPTER") == "1" else "0",
    })
    for key, value in environment.items():
        if key in {"QT_QPA_PLATFORM", "QT_QUICK_BACKEND", "PYTHONUNBUFFERED",
                   "COAT_TEST_APPLICATION_SMOKE", "COAT_TEST_APPLICATION_STATE",
                   "COAT_ASSISTANT_RUNTIME_ROOT", "APPDATA", "LOCALAPPDATA",
                   "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "PYTHONPATH"}:
            monkeypatch.setenv(key, value)
    assistant = AssistantProcessClient(runtime, application_root=source, timeout=3)
    extra = {"qa_action", "qa_snapshot"}
    owned: list[subprocess.Popen] = []
    streams = []
    logs: list[Path] = []
    clients: dict[str, UiControlClient] = {}
    project_ids = [first.manifest["id"], second.manifest["id"]]
    scheduler_identity: tuple[int, str] | None = None
    assistant_identity: tuple[int, str] | None = None
    keepalive_stop = Event()
    keepalive_thread: Thread | None = None

    def launch(role: str, args: list[str]) -> subprocess.Popen:
        log = tmp_path / f"{role}-{len(owned)}.log"
        logs.append(log)
        stream = log.open("wb")
        streams.append(stream)
        process = subprocess.Popen([sys.executable, "-B", *args], cwd=source,
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        owned.append(process)
        return process

    def ui_client(process: subprocess.Popen) -> UiControlClient:
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            for path in state.glob("client-runtime-*/artifacts/runtime/ui-*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    pid = value.get("pid")
                    alive, _started = _process_identity(pid) if type(pid) is int else ("unknown", None)
                    if (value.get("role") == "launcher" and alive == "alive"
                            and value.get("parent_pid") in (None, process.pid)):
                        return UiControlClient(value["root"], pid,
                            operations=OPERATIONS | LIFECYCLE_OPERATIONS | extra, timeout=3,
                            initial_endpoint=value)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
            if process.poll() is not None:
                break
            time.sleep(.1)
        raise AssertionError(f"Owned launcher endpoint missing; Popen pid={process.pid}, exit={process.poll()}")

    def launch_window() -> tuple[subprocess.Popen, UiControlClient]:
        process = launch("launcher", ["-m", "coat_sim.ui.launcher", "--software"])
        client = ui_client(process)
        clients[str(process.pid)] = client
        return process, client

    def eventually(read: Callable[[], Any], accept: Callable[[Any], bool], label: str,
                   timeout: float = 50) -> Any:
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            try:
                last = read()
                if accept(last):
                    return last
            except (UiControlError, OSError, StopIteration) as exc:
                last = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
            time.sleep(.1)
        tails = "\n".join(path.read_text(encoding="utf-8", errors="replace")[-12000:]
                            for path in logs if path.exists())
        raise AssertionError(f"{label}: last={last!r}\n{tails}")

    def project_owner(project_id: str) -> tuple[dict, UiControlClient]:
        rows = [row for row in assistant.list_clients() if row["role"] == "project_host" and
                any(item["project_id"] == project_id for item in row["project_sessions"])]
        if len(rows) != 1:
            raise StopIteration(f"Expected one owner for {project_id}, got {rows!r}")
        owner = rows[0]
        client = clients.setdefault(str(owner["pid"]), UiControlClient(
            state / f"client-runtime-{owner['pid']}", owner["pid"],
            operations=OPERATIONS | LIFECYCLE_OPERATIONS | extra, timeout=3))
        return owner, client

    def snapshot(project_id: str) -> tuple[dict, dict, UiControlClient]:
        owner, client = project_owner(project_id)
        return owner, client.request("qa_snapshot"), client

    def open_project(client: UiControlClient, store) -> None:
        assert client.request("qa_action", action="open", root=str(store.project_file))["accepted"]
        result = eventually(lambda: client.request("qa_snapshot"), lambda row: not row["busy"],
                            "launcher project open settles")
        # Denied foreground permission must not masquerade as an open failure.
        # The runtime client still requires an exact visible-owner confirmation.
        assert result["open_error"] == "", result

    def process_retired(identity: tuple[int, str]) -> bool:
        current, started = _process_identity(identity[0])
        return current == "dead" or (current == "alive" and started is not None and started != identity[1])

    def answer_dirty_shutdowns() -> None:
        for project_id in project_ids:
            try:
                _owner, row, client = snapshot(project_id)
                if row.get("pending_shutdown") and row.get("shutdown_dialog_visible"):
                    client.request("qa_action", action="confirm_shutdown")
            except (UiControlError, StopIteration, OSError):
                pass

    try:
        assistant_ready = assistant.ensure_running(timeout=50)
        assistant_identity = (assistant_ready["pid"], assistant_ready["process_start"])
        first_launcher, first_client = launch_window()
        eventually(lambda: first_client.request("qa_snapshot"), lambda row: row["admission_ready"],
                   "first launcher registered")
        open_project(first_client, first)
        owner_a, opened_a, project_a_client = eventually(
            lambda: snapshot(first.manifest["id"]),
            lambda value: len(value[1]["sessions"]) == 1 and bool(value[1]["rows"]),
            "first project host and window")
        identity_a = (owner_a["pid"], owner_a["process_start"])
        lease_a = opened_a["leases"]
        assert lease_a and not lease_a[0]["closed"]

        # Open the real assistant queue against the private compute root. This
        # creates a genuine local scheduler through the normal product flow.
        assistant.request("show_queue")
        scheduler = SchedulerClient(compute_root)
        health = eventually(lambda: scheduler._call("health", start=False),
                            lambda row: bool(row.get("instance")), "private local scheduler starts")
        scheduler_identity = (health["pid"], health["process_start"])
        def keep_scheduler_alive() -> None:
            while not keepalive_stop.wait(4):
                try:
                    scheduler._call("refresh_estimates", start=False)
                except (OSError, ValueError, RuntimeError):
                    return
        keepalive_thread = Thread(target=keep_scheduler_alive,
                                  name="test-private-scheduler-keepalive", daemon=True)
        keepalive_thread.start()
        assert _live(identity_a) and _live(scheduler_identity)
        assert registration_path(compute_root, runtime).is_file()

        # Closing one launcher retires only it; its project and assistant stay alive.
        assert first_client.request("qa_action", action="shutdown")["accepted"]
        assert first_launcher.wait(timeout=25) == 0
        assert assistant.status()["state"] == "ready"
        assert _live(identity_a) and _live(scheduler_identity)
        owner_a, opened_a, project_a_client = snapshot(first.manifest["id"])
        view = next(row for row in opened_a["rows"] if row["visible"])
        closed_window = project_a_client.request("close_window", client_generation=owner_a["generation"],
            project_id=first.manifest["id"], session_generation=view["session_generation"],
            project_root=str(first.root), project_file=str(first.project_file),
            module_key=view["module_key"], instance_id=view["instance_id"])
        assert closed_window["instance_id"] == view["instance_id"]
        assert closed_window["close_review"] == "native_owner"
        assert eventually(lambda: process_retired(identity_a), bool, "last project window retires project")
        eventually(lambda: assistant.list_clients(),
            lambda rows: not any(item["pid"] == identity_a[0] for item in rows),
            "last project window removes the exact owner registration")
        assert _live(scheduler_identity) and assistant.status()["state"] == "ready"

        second_launcher, second_client = launch_window()
        eventually(lambda: second_client.request("qa_snapshot"), lambda row: row["admission_ready"],
                   "second launcher registered")
        open_project(second_client, second)
        _owner_b, clean_b, project_b_client = eventually(
            lambda: snapshot(second.manifest["id"]),
            lambda value: len(value[1]["sessions"]) == 1 and bool(value[1]["rows"]),
            "second project host and window")
        assert project_b_client.request("qa_action", action="dirty",
            project_id=second.manifest["id"], name="Native exit draft") ["accepted"]
        owner_b, dirty_b, project_b_client = snapshot(second.manifest["id"])
        assert dirty_b["dirty"] == [True]
        baseline_drafts, baseline_lease = dirty_b["film_drafts"], dirty_b["leases"]
        identity_b = (owner_b["pid"], owner_b["process_start"])
        assert baseline_drafts[0]["design"]["name"] == "Native exit draft"
        assert _live(identity_b) and _live(scheduler_identity)

        assistant.request("request_exit_all")
        prompted = eventually(lambda: snapshot(second.manifest["id"]),
            lambda value: value[1].get("pending_shutdown")
                          and value[1].get("review_kind") == "shutdown"
                          and value[1].get("shutdown_dialog_visible"),
            "native project shutdown confirmation is visible")
        assert prompted[1]["film_drafts"] == baseline_drafts
        assert prompted[1]["leases"] == baseline_lease
        project_b_client.request("qa_action", action="cancel_shutdown")
        declined = eventually(lambda: assistant.request("exit_status"),
                              lambda row: row["state"] == "declined",
                              "cancelled global exit")
        assert declined["committed_clients"] == []
        cancelled = project_b_client.request("qa_snapshot")
        assert cancelled["film_drafts"] == baseline_drafts
        assert cancelled["leases"] == baseline_lease
        assert cancelled["dirty"] == [True] and not cancelled["frozen"]
        assert _live(identity_b) and _live(scheduler_identity)
        assert second_launcher.poll() is None and _live(assistant_identity)
        assert scheduler._call("health", start=False)["instance"] == health["instance"]

        assistant.request("request_exit_all")
        prompted = eventually(lambda: snapshot(second.manifest["id"]),
            lambda value: value[1].get("pending_shutdown")
                          and value[1].get("review_kind") == "shutdown"
                          and value[1].get("shutdown_dialog_visible"),
            "second native project shutdown confirmation")
        project_b_client.request("qa_action", action="confirm_shutdown")
        assert eventually(lambda: process_retired(assistant_identity), bool,
                          "confirmed exit retires private assistant")
        assert second_launcher.wait(timeout=30) == 0
        assert eventually(lambda: process_retired(identity_b), bool, "confirmed exit retires project")
        assert eventually(lambda: process_retired(scheduler_identity), bool,
                          "confirmed exit retires exact local scheduler")
        assert eventually(lambda: not registration_path(compute_root, runtime).exists(), bool,
                          "scheduler lifecycle registration removed")
        assert not list(registry_directory(runtime).glob("*.json"))
    finally:
        # Graceful cleanup targets only child PIDs and project IDs created above.
        keepalive_stop.set()
        if keepalive_thread is not None:
            keepalive_thread.join(timeout=2)
        for project_id in project_ids:
            try:
                _owner, row, client = snapshot(project_id)
                if row.get("sessions"):
                    client.request("qa_action", action="close_project", project_id=project_id)
                    _owner, row, client = snapshot(project_id)
                    if row.get("close_pending"):
                        client.request("qa_action", action="discard_close", project_id=project_id)
            except (UiControlError, StopIteration, OSError):
                pass
        if assistant_identity is not None and _live(assistant_identity):
            try:
                assistant.request("request_exit_all")
                deadline = time.monotonic() + 35
                while _live(assistant_identity) and time.monotonic() < deadline:
                    answer_dirty_shutdowns()
                    time.sleep(.1)
            except (UiControlError, OSError):
                pass
        for process in owned:
            if process.poll() is None and isinstance(process.args, (list, tuple)) and "coat_sim.ui.launcher" in process.args:
                try:
                    clients[str(process.pid)].request("qa_action", action="shutdown")
                    process.wait(timeout=20)
                except (KeyError, UiControlError, OSError, subprocess.TimeoutExpired):
                    pass
        for stream in streams:
            stream.close()
