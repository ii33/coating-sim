"""Owned two-project native target; no user state or provider access."""

from pathlib import Path
import json
import os
import sys

def main():
    root, state = map(Path, sys.argv[1:3])
    sys.path.insert(0, str(root))
    from coat_sim.project_logging import install_process_log

    install_process_log("project", state)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQuick import QQuickWindow
    from coat_sim.ui.launcher import LauncherBridge
    from coat_sim.ui.control import UiControlServer, UiControlError
    from coat_sim.assistant_process import LIFECYCLE_OPERATIONS
    from coat_sim.core.process_identity import _process_identity

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    generation = "native-activation-target"
    start = _process_identity(os.getpid())[1]
    windows = {}
    projects = []
    for index, key in enumerate(("project-a", "project-b")):
        w = QQuickWindow()
        w.setTitle("Native Activation " + key)
        w.setGeometry(180 + index * 30, 170 + index * 30, 420, 260)
        w.show()
        windows[key] = w
        projects.append(
            {
                "project_id": key,
                "generation": "session-" + key,
                "root": str(state / key),
                "project_file": str(state / key / (key + ".coatproj")),
                "name": key,
            }
        )
    requests: list[dict] = []


    def rows():
        return [
            {
                "project_id": key,
                "session_generation": "session-" + key,
                "module_key": "film_design",
                "window_type": "film_design",
                "instance_id": "same-instance",
                "title": w.title(),
                "open": True,
                "visible": w.isVisible(),
                "hwnd": int(w.winId()),
            }
            for key, w in windows.items()
        ]


    def dispatch(op, args):
        if op == "lifecycle_status":
            return {
                "application": "coat_sim",
                "pid": os.getpid(),
                "process_start": start,
                "generation": generation,
                "role": "project_host",
                "nonce": args.get("nonce"),
                "project_sessions": projects,
            }
        if op == "list_windows":
            return {"windows": rows()}
        key = args.get("project_id")
        w = windows.get(key)
        if op == "window_action":
            if args.get("action") == "shutdown":
                QTimer.singleShot(0, app.quit)
                return {"accepted": True}
            assert w is not None
            if args["action"] == "minimize":
                w.showMinimized()
            elif args["action"] == "cover":
                LauncherBridge._activate_window(
                    windows["project-b" if key == "project-a" else "project-a"]
                )
            return {"accepted": True}
        if op == "open_window":
            assert (
                w is not None
                and args.get("session_generation") == "session-" + key
                and args.get("instance_id") == "same-instance"
            )
            requests.append(
                {
                    k: args.get(k)
                    for k in ("project_id", "session_generation", "instance_id", "module_key")
                }
            )
            LauncherBridge._activate_window(w)
            (state / "target-requests.json").write_text(json.dumps(requests), encoding="utf-8")
            return {"accepted": True, "instance_id": "same-instance"}
        raise UiControlError("invalid_operation", "Not a native test operation")


    server = UiControlServer(
        dispatch, root=state / "target-runtime", role="project_host", operations=LIFECYCLE_OPERATIONS
    )
    endpoint = server.start()
    (state / "target-ready.json").write_text(
        json.dumps(
            {"endpoint": endpoint, "generation": generation, "process_start": start, "windows": rows()}
        ),
        encoding="utf-8",
    )
    app.aboutToQuit.connect(server.close)
    QTimer.singleShot(35000, app.quit)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
