"""Standalone CLI. Installation requires an approved current plan ID."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .manager import (
    BACKENDS,
    GROUPS,
    PROFILES,
    execute_plan,
    inspect_environment,
    plan_environment,
)
from .mirrors import MIRRORS, ProxyConfig, benchmark_mirrors, redact
from .project_deploy import (
    execute_project_deployment,
    plan_project_deployment,
    read_project_deployment_plan,
)
from .runtime import execute_runtime, plan_runtime
from .project_status import check_project


def discover_project():
    starting = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
    for path in (starting, *starting.parents):
        if (path / "requirements.txt").is_file() and (path / "coat_sim").is_dir():
            return path
    return Path.cwd()


def parser():
    result = argparse.ArgumentParser(description="在线安装器：标准库终端交互；安装需当前计划确认")
    result.add_argument(
        "operation",
        choices=(
            "doctor",
            "inspect",
            "plan",
            "install",
            "repair",
            "benchmark",
            "git-plan",
            "git-install",
            "runtime-plan",
            "runtime-install",
            "launch-plan",
            "launch-create",
            "project-check",
            "project-plan",
            "project-deploy",
            "interactive",
        ),
        nargs="?",
        default="interactive",
    )
    result.add_argument("--project", default=str(discover_project()), help="项目根目录")
    result.add_argument("--venv", help="明确目标venv；默认项目/.venv，绝不安装到系统Python")
    result.add_argument("--groups", default="desktop", help="逗号分隔：" + ", ".join(GROUPS))
    result.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="runtime",
        help="运行依赖配置：runtime不含开发工具；development另含pytest/mpmath/Ruff/mypy",
    )
    result.add_argument("--backend", choices=BACKENDS, default="cpu")
    result.add_argument("--mirror", choices=tuple(MIRRORS), default="pypi")
    result.add_argument("--mirrors", help="测速镜像ID，逗号分隔；默认全部")
    result.add_argument("--proxy-mode", choices=("none", "env", "http"), default="env")
    result.add_argument(
        "--proxy",
        default="",
        help="不含凭据的HTTP CONNECT代理URL；认证用COAT_DEPENDENCY_PROXY环境变量",
    )
    result.add_argument("--python", help="创建venv的正常CPython解释器路径；不使用冻结工具exe")
    result.add_argument("--runtime-dir", help="受管理CPython用户目录")
    result.add_argument(
        "--deployment-dir",
        help="project-plan/deploy：本项目 GitHub/CNB checkout；prepare 只接受新目录，依赖仅装到该目录/.venv",
    )
    result.add_argument("--ref", default="main", help="项目 branch/tag 或完整 commit SHA")
    result.add_argument(
        "--shortcut",
        choices=("none", "desktop", "start-menu", "custom"),
        default="none",
        help="启动快捷方式位置；默认只建项目启动脚本",
    )
    result.add_argument("--shortcut-dir", help="custom 快捷方式目录")
    result.add_argument("--launch-name", default="Coating Simulator", help="启动入口名称")
    result.add_argument("--git-dir", help="受管理私有 MinGit 目录（Windows）")
    result.add_argument("--git", help="完整 Git 可执行文件路径；不更改系统 PATH")
    result.add_argument("--source", choices=("github", "cnb"), default="github")
    result.add_argument(
        "--repository-url", help="CNB 镜像 HTTPS URL（必填）或显式旧开发源；不含凭据"
    )
    result.add_argument(
        "--check-remote", action="store_true", help="project-check 显式查询所选远端；默认离线"
    )
    result.add_argument(
        "--deployment-timeout", type=int, default=300, help="project-plan：Git 获取总超时秒数"
    )
    result.add_argument(
        "--action",
        choices=("install", "repair"),
        default="repair",
        help="plan的动作类型",
    )
    result.add_argument("--plan-id", help="刚审核过的plan_id")
    result.add_argument(
        "--yes",
        action="store_true",
        help="确认当前计划；runtime-install也确认所示CPython许可及每用户安装记录",
    )
    result.add_argument("--json", action="store_true", help="输出单个最终JSON")
    result.add_argument("--events", action="store_true", help="输出进度JSON lines及最终result")
    return result


def _print_human(result):
    if result.get("operation") in ("launch_entries", "git_runtime_plan", "git_runtime_install"):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    dependency = result.get("dependency_plan") or result.get("dependency_result")
    if dependency:
        # Display the reviewed nested environment plan without replacing the
        # deployment plan ID the user must approve.
        result = {
            **result,
            **{
                key: dependency[key]
                for key in ("target", "packages", "actions", "profile")
                if key in dependency
            },
        }
    print(f"{result['operation']}: {result['status']}")
    if result.get("operation") == "project_check":
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "internal_version",
                        "commit",
                        "ref",
                        "dirty",
                        "submodule_dirty",
                        "dirty_scope",
                        "source",
                        "provenance",
                        "remote",
                        "comparison",
                        "environment",
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if result.get("profile"):
        print("依赖配置:", result["profile"])
    if result.get("target"):
        target = result["target"]
        print("目标venv:", target.get("venv", ""))
        print("解释器:", target.get("interpreter", ""))
        print(
            "身份:",
            target.get("python") or "missing",
            "valid=" + str(target.get("valid", False)),
        )
    for row in result.get("packages", []):
        print(
            f"  {row['status']:11} {row['requirement']}  installed={row.get('installed_version') or 'missing'}"
        )
    if "actions" in result:
        for action in result["actions"]:
            print("动作:", action["kind"], json.dumps(action["argv"], ensure_ascii=False))
    if "plan_id" in result:
        print("plan_id:", result["plan_id"])
    for warning in result.get("warnings", []):
        print("提示:", warning)
    for error in result.get("errors", []):
        print("错误:", error)
    if "results" in result:
        for item in result["results"]:
            print(
                item.get("mirror"),
                item.get("latency_ms"),
                "ms",
                item.get("throughput_mbps"),
                "Mbps",
                item.get("error", ""),
            )
        print("推荐(需用户选择):", result.get("recommended_mirror"))
    if result.get("directory"):
        print("受管理目录:", result["directory"])
    if result.get("source"):
        source = result["source"]
        if isinstance(source, dict) and source.get("commit"):
            print("项目来源:", source.get("url", ""))
            print("源代码 commit:", source["commit"])
            print("选择 ref:", source.get("requested_ref", ""))
        else:
            print("官方来源:", source)
    for command in result.get("next_commands", []):
        print(
            "后续命令（尚未执行）:",
            command["purpose"],
            json.dumps(command["argv"], ensure_ascii=False),
        )
    if result.get("license"):
        print("许可:", result["license"])


def main(argv=None):
    args = parser().parse_args(argv)
    if args.operation == "interactive":
        from .interactive import run_interactive

        return run_interactive(args.project)
    if args.operation == "doctor":
        try:
            import ensurepip
            import ssl
            import venv

            if not callable(venv.EnvBuilder) or not ensurepip.version():
                raise RuntimeError("Full CPython with venv and ensurepip is required")
            result = {
                "schema_version": 1,
                "operation": "doctor",
                "status": "success",
                "tool_python": sys.version,
                "frozen": bool(getattr(sys, "frozen", False)),
                "interface": "terminal",
                "third_party_dependencies": [],
                "venv": True,
                "bundled_pip": ensurepip.version(),
                "tls": ssl.OPENSSL_VERSION,
                "errors": [],
                "warnings": [],
            }
        except (ImportError, OSError, RuntimeError) as exc:
            result = {
                "schema_version": 1,
                "operation": "doctor",
                "status": "failed",
                "interface": "terminal",
                "errors": [redact(str(exc))],
                "warnings": [],
            }
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            _print_human(result)
        return 0 if result["status"] == "success" else 2
    cancel_event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *unused: cancel_event.set())

    def emit(event):
        if args.events:
            print(
                json.dumps({"schema_version": 1, **event}, ensure_ascii=False),
                flush=True,
            )
        elif not args.json and event.get("event") == "log":
            print(event["text"], flush=True)

    proxy = None
    try:
        if args.proxy and ("@" in args.proxy or "?" in args.proxy):
            raise ValueError(
                "Proxy credentials/query data must not be placed in argv; use COAT_DEPENDENCY_PROXY"
            )
        proxy = ProxyConfig(
            args.proxy_mode,
            os.environ.get("COAT_DEPENDENCY_PROXY", "")
            if args.proxy_mode == "http" and not args.proxy
            else args.proxy,
        )
        if args.operation != "inspect" and not (
            args.operation == "project-check" and not args.check_remote
        ):
            proxy.validate()
        groups = tuple(filter(None, args.groups.split(",")))
        if args.operation == "inspect":
            result = inspect_environment(
                args.project, args.venv, groups, args.backend, args.profile
            )
        elif args.operation in ("launch-plan", "launch-create"):
            from .launchers import plan_launch_entries, execute_launch_entries

            plan = plan_launch_entries(
                args.project,
                shortcut=args.shortcut,
                output_dir=args.shortcut_dir,
                name=args.launch_name,
            )
            result = (
                plan
                if args.operation == "launch-plan"
                else execute_launch_entries(
                    plan, confirmed=args.yes and args.plan_id == plan.get("plan_id")
                )
            )
        elif args.operation == "project-check":
            result = check_project(
                args.project,
                args.venv,
                groups,
                args.backend,
                args.profile,
                check_remote=args.check_remote,
                source=args.source,
                repository_url=args.repository_url,
                ref=args.ref,
                proxy=proxy,
                cancel_event=cancel_event,
                timeout=args.deployment_timeout,
                git_path=args.git,
            )
        elif args.operation == "benchmark":
            result = benchmark_mirrors(
                (args.mirrors or ",".join(MIRRORS)).split(","), proxy, cancel_event
            )
        elif args.operation in ("project-plan", "project-deploy"):
            if not args.deployment_dir:
                raise ValueError(
                    "project-plan/project-deploy requires an explicit --deployment-dir"
                )
            if args.venv:
                raise ValueError(
                    "Project deployment targets only checkout/.venv; --venv cannot override it"
                )
            if args.operation == "project-plan":
                if not args.json and not args.events:
                    print(
                        "从 GitHub/CNB 获取本项目到新目录，准备依赖计划；此步骤不会安装依赖或启动项目。"
                    )
                result = plan_project_deployment(
                    args.deployment_dir,
                    args.ref,
                    groups,
                    args.backend,
                    args.profile,
                    args.mirror,
                    args.python,
                    proxy,
                    emit=emit,
                    cancel_event=cancel_event,
                    timeout=args.deployment_timeout,
                    source=args.source,
                    repository_url=args.repository_url,
                    git_path=args.git,
                )
            else:
                plan = read_project_deployment_plan(args.deployment_dir)
                result = execute_project_deployment(
                    plan,
                    args.yes and args.plan_id == plan["plan_id"],
                    emit,
                    cancel_event,
                    proxy,
                )
        elif args.operation in ("git-plan", "git-install"):
            from .git_runtime import plan_git_runtime, execute_git_runtime

            plan = plan_git_runtime(args.git_dir, proxy, project=args.project)
            result = (
                plan
                if args.operation == "git-plan"
                else execute_git_runtime(
                    plan,
                    args.yes and args.plan_id == plan.get("plan_id"),
                    emit,
                    cancel_event,
                    proxy,
                )
            )
        elif args.operation.startswith("runtime-"):
            plan = plan_runtime(args.runtime_dir, args.backend, proxy, project=args.project)
            result = (
                plan
                if args.operation == "runtime-plan"
                else execute_runtime(
                    plan,
                    args.yes and args.plan_id == plan["plan_id"],
                    emit,
                    cancel_event,
                    proxy,
                )
            )
        else:
            plan = plan_environment(
                args.project,
                args.venv,
                groups,
                args.backend,
                args.mirror,
                args.action if args.operation == "plan" else args.operation,
                args.python,
                proxy,
                args.profile,
            )
            result = (
                plan
                if args.operation == "plan"
                else execute_plan(
                    plan,
                    args.yes and args.plan_id == plan["plan_id"],
                    emit,
                    cancel_event,
                    proxy,
                )
            )
    except (ValueError, OSError, RuntimeError) as exc:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "failed",
            "errors": [redact(str(exc), proxy)],
            "warnings": [],
        }
    if args.events:
        print(
            json.dumps(
                {"schema_version": 1, "event": "result", "result": result},
                ensure_ascii=False,
            ),
            flush=True,
        )
    elif args.json:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        _print_human(result)
    return (
        0
        if result["status"] in ("success", "ready", "completed")
        else 130
        if result["status"] == "cancelled"
        else 2
    )
