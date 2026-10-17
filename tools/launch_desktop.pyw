"""Cross-platform desktop bootstrap with persistent startup diagnostics.

launcher.pyw is the public desktop entry. Developer CLI and MCP workers
continue to use launcher.py and console Python with redirected pipes.
"""
from __future__ import annotations
from datetime import datetime
import faulthandler
import os
import runpy
import subprocess
from pathlib import Path
import sys
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST_ROLES = {
    "main": "沉积仿真.exe", "launcher": "光学生产仿真.exe",
    "source": "蒸发源.exe", "substrate": "基片.exe", "process": "工艺.exe",
    "machine": "机器结构.exe", "mask": "修正板.exe", "settings": "设置.exe",
    "about": "关于与帮助.exe",
}


def _desktop_module() -> str | None:
    """Return the requested known module; let launcher.py report invalid input."""
    try:
        position = sys.argv.index("--module")
    except ValueError:
        return "launcher"
    if position + 1 >= len(sys.argv):
        return None
    module = sys.argv[position + 1].strip().lower()
    return module if module in HOST_ROLES else None


def _alert(message: str):
    """Use the shared Qt Quick stack for startup errors on every OS."""
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        app = QGuiApplication.instance() or QGuiApplication([])
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("startupError", message)
        engine.loadData(b'import QtQuick\nimport QtQuick.Window\nWindow {\n    visible: true; width: 640; height: 240\n    title: "Application startup failed"\n    color: "#20252d"\n    Text {\n        anchors.fill: parent; anchors.margins: 24\n        text: startupError; textFormat: Text.PlainText\n        color: "#f2f4f8"; font.pixelSize: 16\n        wrapMode: Text.Wrap\n    }\n    onClosing: Qt.quit()\n}\n')
        if engine.rootObjects():
            app.exec()
    except Exception:
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def _restart_with_desktop_host() -> bool:
    """Use a branded PE copy on Windows; keep the same Qt/Python runtime."""
    if (sys.platform != "win32" or getattr(sys, "frozen", False)
            or os.environ.get("COAT_SIM_PORTABLE_ROOT")):
        return False
    module = _desktop_module()
    if module is None:
        return False
    expected = PROJECT_ROOT / ".conda" / HOST_ROLES[module]
    if Path(sys.executable).resolve() == expected.resolve():
        return False
    # Also guard against unusual interpreter launchers that report another
    # executable path. This marker is limited to our own exact destination.
    if os.environ.get("COAT_SIM_DESKTOP_HOST") == str(expected):
        return False
    builder = runpy.run_path(str(PROJECT_ROOT / "tools" / "build_desktop_host.py"))
    host = builder["build_host"](PROJECT_ROOT, module=module)
    entry = Path(sys.argv[0]).resolve()
    if not entry.is_file():
        entry = PROJECT_ROOT / "launcher.pyw"
    environment = os.environ.copy()
    environment["COAT_SIM_DESKTOP_HOST"] = str(host)
    host_process = subprocess.Popen(
        [str(host), str(entry), *sys.argv[1:]], cwd=os.getcwd(), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if os.environ.get("COAT_UI_READY_ADDRESS"):
        try:
            from coat_sim.ui.ready_channel import report_startup_event
            handed_off = report_startup_event("handoff", process_pid=os.getpid(),
                                              host_pid=host_process.pid, consume=False)
        except Exception:
            handed_off = False
        if not handed_off:
            try:
                host_process.terminate()
            except OSError:
                pass
            raise RuntimeError("desktop host startup handoff could not be reported")
    print("Desktop restarted with application host:", host)
    return True


def main() -> int:
    # This counter is present only in the isolated QA environment and is
    # inherited by child processes. Ordinary desktop sessions are unaffected.
    sys.path.insert(0, str(PROJECT_ROOT))
    from coat_sim.process_entry import consume_qa_start_slot
    if not consume_qa_start_slot():
        if sys.stderr is not None:
            print("CoatSim QA desktop-start budget exhausted or unavailable.", file=sys.stderr)
        return 78
    try:
        from coat_sim.project_logging import ProjectLog
        log = ProjectLog("desktop", PROJECT_ROOT)
        log_path = log.path
    except OSError as exc:
        _alert("无法建立启动日志：" + str(exc))
        return 2
    # pythonw has no console streams. Install real file streams before loading
    # Qt, modules, or libraries that write diagnostics or use faulthandler.
    sys.stdout = sys.stderr = log
    for name in ("__stdout__", "__stderr__"):
        setattr(sys, name, log)
    print("Desktop bootstrap:", datetime.now().astimezone().isoformat(timespec="seconds"), "PID:", os.getpid(), flush=True)
    print("Python:", sys.executable, flush=True)
    try:
        if _restart_with_desktop_host():
            print("Desktop bootstrap handed off to branded host", flush=True)
            log.flush()
            return 0
    except Exception as exc:
        if _desktop_module() == "launcher":
            # Do not silently start an unbranded launcher process: its
            # task-manager identity would violate the desktop entry contract.
            message = ("无法准备“光学生产仿真”桌面宿主，启动已停止。\n"
                       f"原因：{exc}\n诊断日志：{log_path}")
            print(message)
            traceback.print_exc()
            try:
                from coat_sim.ui.ready_channel import report_startup_event
                report_startup_event("error", error=message)
            except Exception:
                pass
            _alert(message)
            return 2
        traceback.print_exc()
        print("Branded module host unavailable; continuing with current interpreter.")
    # CPython's Windows vectored handler also sees recoverable COM/SEH
    # exceptions. Walking every Python thread there can itself fault (as in
    # desktop-20261002-194701-48048.log). Keep normal exception logs and WER;
    # native stack dumping on Windows is an explicit diagnostic opt-in.
    faulthandler.disable()
    if sys.platform != "win32" or os.environ.get("COAT_NATIVE_TRACE") == "1":
        try:
            faulthandler.enable(file=log, all_threads=sys.platform != "win32")
        except (RuntimeError, OSError):
            pass
    else:
        faulthandler.disable()
        print("Native crash diagnostics: Windows Error Reporting; UI action breadcrumbs enabled.")
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))
    print("Desktop application startup:", datetime.now().isoformat(timespec="seconds"))
    print("Python:", sys.executable)
    try:
        from launcher import main as launch
        result = launch()
        exit_code = int(result or 0)
        if exit_code:
            _alert("程序启动失败，请查看诊断日志：\n" + str(log_path))
        return exit_code
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        if exit_code:
            traceback.print_exc()
            _alert("程序启动失败，请查看诊断日志：\n" + str(log_path))
        return exit_code
    except BaseException:
        traceback.print_exc()
        _alert("程序启动失败，请查看诊断日志：\n" + str(log_path))
        return 1
    finally:
        log.flush()


if __name__ == "__main__":
    raise SystemExit(main())
