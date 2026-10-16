"""Standard-library terminal installer, reusing the checked project transactions."""

from __future__ import annotations

import json
import signal
import sys
import tempfile
from pathlib import Path
import threading

from .compute_recommendation import END_USER_GROUPS, recommend_environment
from .git_runtime import discover_git, execute_git_runtime, plan_git_runtime
from .launchers import execute_launch_entries, plan_launch_entries
from .manager import execute_plan, inspect_environment, plan_environment
from .mirrors import MIRRORS, ProxyConfig, redact
from .project_deploy import execute_project_deployment, plan_project_deployment
from .python_discovery import discover_python
from .runtime import execute_runtime, plan_runtime
from .runtime_paths import unlinked_local_path

CNB_URL = "https://cnb.cool/nonereason/coating-sim"


def project_directory(root):
    """An install container and its app checkout have separate ownership."""
    root = Path(root).absolute()

    def is_project(path):
        return (
            (path / "requirements.txt").is_file()
            and (path / "coat_sim").is_dir()
            and (path / "launcher.py").is_file()
        )

    app = root if is_project(root) else root / "app"
    if not unlinked_local_path(root) or not unlinked_local_path(app):
        raise ValueError("安装目录必须是本地真实目录，不能经过链接或重解析点")
    if app.exists() and not is_project(app):
        raise ValueError("app 目录已有未知内容；请选择其他安装目录，不会覆盖")
    return app


def run_interactive(project=None, *, input_fn=input, output=print, cancel_event=None):
    cancel = cancel_event or threading.Event()
    proxy = ProxyConfig("env")

    def say(message):
        output(redact(str(message), proxy))

    def ask(prompt, default=""):
        if cancel.is_set():
            raise KeyboardInterrupt
        return input_fn(prompt).strip() or default

    def yes(prompt):
        return ask(prompt + " [y/N]：").lower() in ("y", "yes", "是")

    def emit(event):
        say(event.get("text") or event.get("phase") or json.dumps(event, ensure_ascii=False))

    def operation(function, *args, **kwargs):
        # Core operations own subprocess cleanup. Set their cancellation event
        # instead of unwinding past a live pip/Git child on Ctrl+C.
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGINT, lambda *unused: cancel.set())
        try:
            return function(*args, **kwargs)
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def show(result):
        say("状态：" + result.get("status", "unknown"))
        details = result.get("dependency_plan") or result
        target = details.get("target") or {}
        if target:
            say(
                f"环境：{target.get('venv', '')}；Python {target.get('python', '')} {target.get('platform', '')}"
            )
        source = result.get("source") or {}
        if source:
            say(
                f"源码：{source.get('url', result.get('repository_url', ''))} @ {source.get('commit', 'unknown')}"
            )
        artifact = result.get("artifact") or {}
        if artifact:
            say(
                f"官方来源：{artifact.get('url', '')}\nSHA256：{artifact.get('sha256', '')}\n安装位置：{result.get('directory', '')}\n许可：{result.get('license', '见官方许可')}"
            )
        if "selected_backend" in result:
            identity = result.get("python") or {}
            say(
                f"Python：{identity.get('python', '?')} / {identity.get('platform', '?')}；计算后端：{result['selected_backend']}"
            )
            say(
                "设备："
                + ", ".join(
                    str(row.get("name", row.get("model", row.get("vendor", "?"))))
                    for row in result.get("visible_gpus", [])
                )
            )
            say(
                "可用性证据："
                + str(result.get("backend_usable", {}))
                + "；"
                + str(result.get("reason", ""))
            )
        if "actions" in details:
            say(
                f"依赖：{profile} / {', '.join(END_USER_GROUPS)}；{len(details.get('packages', []))} 个包，{len(details['actions'])} 个执行步骤；镜像 {mirror}"
            )
        for message in result.get("errors", []) + result.get("warnings", []):
            say(message)
        logs = root / ".dependency-manager" / "logs"
        if unlinked_local_path(logs):
            try:
                logs.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="installer-",
                    suffix=".json",
                    dir=logs,
                    delete=False,
                ) as handle:
                    handle.write(redact(json.dumps(result, ensure_ascii=False, indent=2), proxy))
                say("详细记录：" + handle.name)
            except OSError as exc:
                say("无法保存诊断记录：" + str(exc))

    def ready(plan):
        show(plan)
        return plan.get("status") == "ready"

    def success(result):
        show(result)
        return result.get("status") in ("success", "completed")

    try:
        say("在线安装器 · 仅使用 Python 标准库；Ctrl+C 取消")
        default = str(Path(project or Path.cwd() / "coating-sim").absolute())
        root = Path(ask(f"安装目录 [{default}]：", default)).expanduser().absolute()
        app = project_directory(root)
        source = ask("源码来源：1 CNB（默认），2 GitHub [1]：", "1")
        if source not in ("1", "2"):
            raise ValueError("源码来源请输入 1 或 2")
        provider = "cnb" if source == "1" else "github"
        mirror = "tuna" if yes("启用镜像加速（仅清华 PyPI，不改 Git URL）？") else "pypi"
        profile = "development" if yes("安装开发环境（含测试与静态检查工具）？") else "runtime"
        say(
            f"安装目录：{root}\n源码：{app}\n依赖环境：{app / '.venv'}\n镜像：{MIRRORS[mirror]['url']}"
        )
        existing = (app / ".venv").exists() or (app / ".venv").is_symlink()
        python, identity = None, None
        if existing:
            inspected = inspect_environment(app, app / ".venv", END_USER_GROUPS, "cpu", profile)
            identity = inspected.get("target")
            say("已有 venv 将保持原解释器；创建用 Python 不会替换它。")
        else:
            found = discover_python("cpu", project=root, cancel_event=cancel)
            if found.get("status") == "cancelled":
                raise KeyboardInterrupt
            python = found.get("selected_python")
            identity = next(
                (
                    row.get("identity")
                    for row in found.get("candidates", [])
                    if row["path"] == python
                ),
                None,
            )
            say("已有 Python：" + (python or "未发现兼容解释器"))
            plan = plan_runtime(backend="cpu", proxy=proxy, project=root)
            if plan.get("status") == "ready":
                show(plan)
            elif not python:
                show(plan)
            if plan.get("status") == "ready" and yes(
                "确认上述官方来源、SHA256、目录与许可，安装独立 Python？已有兼容 Python 可跳过"
            ):
                result = operation(execute_runtime, plan, True, emit, cancel, proxy)
                if not success(result):
                    return 130 if cancel.is_set() else 2
                python = result["python"]
                found = discover_python("cpu", explicit=python, project=root, cancel_event=cancel)
                identity = next(
                    (
                        row.get("identity")
                        for row in found.get("candidates", [])
                        if row["path"] == python
                    ),
                    None,
                )
            if not python:
                say(
                    "请先安装官方完整 CPython（含 venv/pip），再运行本脚本。Linux/macOS 本工具不自动修改系统包或安全设置。https://www.python.org/downloads/"
                )
                return 2
        recommendation = recommend_environment(identity, cancel_event=cancel)
        if cancel.is_set() or recommendation.get("status") == "cancelled":
            raise KeyboardInterrupt
        show(recommendation)
        backend = recommendation.get("selected_backend", "cpu")
        if existing:
            say("按上述硬件建议生成依赖计划；保留既有解释器，包冲突由计划检查。")
        if app.exists():
            plan = plan_environment(
                app,
                app / ".venv",
                END_USER_GROUPS,
                backend,
                mirror,
                "install",
                python,
                proxy,
                profile,
            )
            execute = execute_plan
        else:
            git = discover_git(project=root)
            if not git:
                git_plan = plan_git_runtime(proxy=proxy, project=root)
                if not ready(git_plan):
                    return 2
                if not yes("缺少 Git；确认上述校验计划，在安装目录内安装官方私有 Git？"):
                    return 130
                result = operation(execute_git_runtime, git_plan, True, emit, cancel, proxy)
                if not success(result):
                    return 130 if cancel.is_set() else 2
                git = result["git"]
            # Only create the container. Core deployment still requires a new app.
            root.mkdir(parents=True, exist_ok=True)
            plan = operation(
                plan_project_deployment,
                str(app),
                groups=END_USER_GROUPS,
                backend=backend,
                profile=profile,
                mirror=mirror,
                python=python,
                proxy=proxy,
                emit=emit,
                cancel_event=cancel,
                source=provider,
                repository_url=CNB_URL if provider == "cnb" else None,
                git_path=git,
            )
            execute = execute_project_deployment
        if cancel.is_set():
            raise KeyboardInterrupt
        if not ready(plan):
            return 2
        launch_path = app / (
            "launch-coating.cmd" if sys.platform == "win32" else "launch-coating.sh"
        )
        say(f"安装后创建启动脚本：{launch_path}（已有文件不覆盖；不创建桌面快捷方式）")
        if not yes("确认上述源码版本、目录、完整依赖及启动脚本计划，开始安装？"):
            say("已取消依赖安装；已准备的源码或独立运行时保留，不删除任何现有文件。")
            return 130
        result = operation(
            execute, plan, confirmed=True, emit=emit, cancel_event=cancel, proxy=proxy
        )
        if not success(result):
            return 130 if cancel.is_set() or result.get("status") == "cancelled" else 2
        inspected = inspect_environment(app, app / ".venv", END_USER_GROUPS, backend, profile)
        actual = inspected.get("target") or {}
        if actual.get("valid"):
            verified = recommend_environment(actual, manual_backend=backend, cancel_event=cancel)
            show(verified)
            if backend != "cpu":
                usable = verified.get("backend_usable", {}).get(backend) is True
                say(
                    "GPU 数值计算验证："
                    + (
                        "通过"
                        if usable
                        else "未通过；依赖安装成功不代表 GPU 可用，可选择 CPU 重新生成计划。未修改驱动。"
                    )
                )
        else:
            show(inspected)
            say("依赖事务已完成，但目标解释器身份未验证；不宣称 GPU 可用。")
        launch_plan = plan_launch_entries(app, name="launch-coating")
        if launch_plan.get("status") == "ready":
            launch_result = execute_launch_entries(launch_plan, confirmed=True)
            if success(launch_result):
                say("启动脚本：" + str(launch_path))
            else:
                say("依赖已安装，但启动脚本创建失败，请查看上面的诊断。")
        else:
            show(launch_plan)
            say("启动脚本未创建；已有文件保持原样。")
        say(f"安装完成：{app}；可使用 {app / '.venv'} 中的 Python 启动 launcher.py。")
        return 0
    except (KeyboardInterrupt, EOFError):
        cancel.set()
        say("已取消；已有文件保留。中断安装可能留下部分环境，可重新运行检查。")
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        say("安装失败：" + str(exc))
        return 2
