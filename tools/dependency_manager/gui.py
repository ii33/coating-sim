"""Compatibility-first Tk GUI; tool-owned runtime, never target Qt/venv imports."""

from __future__ import annotations
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from typing import Any
from tkinter import filedialog, messagebox, ttk

from .project_deploy import plan_project_deployment, execute_project_deployment
from .project_status import check_project

from .manager import (
    BACKENDS,
    GROUPS,
    PROFILES,
    execute_plan,
    inspect_environment,
    plan_environment,
)
from .mirrors import MIRRORS, ProxyConfig, benchmark_mirrors, redact
from .runtime import (
    default_runtime_directory,
    execute_runtime,
    plan_runtime,
    runtime_manifest,
)


class DependencyManagerWindow:
    def __init__(self, window, project, venv=None):
        self.window = window
        self.window.title("依赖管理器 · Deposition Simulator")
        self.window.geometry("1100x760")
        self.window.minsize(720, 520)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue[tuple[int, str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.closing = False
        self.generation = 0
        self.active_proxy = None
        self.pending_plan = None
        self.last_result = None
        self.project = tk.StringVar(value=str(Path(project).absolute()))
        self.venv = tk.StringVar(
            value=str(Path(venv or Path(project) / ".venv").absolute())
        )
        self.python = tk.StringVar()
        self.profile = tk.StringVar(value="runtime")
        self.backend = tk.StringVar(value="cpu")
        self.mirror = tk.StringVar(value="pypi")
        self.proxy_mode = tk.StringVar(value="env")
        self.proxy_url = tk.StringVar()
        self.deployment_dir = tk.StringVar(value=str(Path.home() / "deposition-sim"))
        self.deployment_ref = tk.StringVar(value="main")
        self.deployment_source = tk.StringVar(value="github")
        self.repository_url = tk.StringVar()
        self.git_path = tk.StringVar()
        self.git_runtime_dir = tk.StringVar()
        self.shortcut_location = tk.StringVar(value="none")
        self.shortcut_dir = tk.StringVar()
        self.launch_name = tk.StringVar(value="Coating Simulator")
        self.runtime_dir = tk.StringVar(value=str(default_runtime_directory()))
        self.status = tk.StringVar(
            value="请选择项目与目标环境，然后检查；工具不依赖项目环境"
        )
        self.identity = tk.StringVar(value="目标解释器：未检查")
        self.selected = {
            group: tk.BooleanVar(value=group == "desktop") for group in GROUPS
        }
        self.controls = []
        self._build()
        self.window.after(50, self._drain)
        self.window.bind(
            "<Escape>", lambda unused: self.cancel() if self.busy else None
        )
        self.window.bind("<F5>", lambda unused: self.inspect())

    def _control(self, widget):
        self.controls.append(widget)
        return widget

    def _path_row(self, frame, row, label, variable, browse):
        ttk.Label(frame, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self._control(ttk.Entry(frame, textvariable=variable)).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self._control(ttk.Button(frame, text="选择…", command=browse)).grid(
            row=row, column=2, padx=(8, 0), pady=3
        )

    def _build(self):
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        paths = ttk.Frame(frame)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "项目目录", self.project, self.choose_project)
        self._path_row(
            paths, 1, "目标 venv", self.venv, lambda: self._choose_directory(self.venv)
        )
        self._path_row(paths, 2, "创建用 CPython", self.python, self.choose_python)
        self._path_row(paths, 3, "Git 可执行文件（可选）", self.git_path, self.choose_git)
        ttk.Label(frame, textvariable=self.identity, wraplength=900).grid(
            row=1, column=0, sticky="ew", pady=(4, 8)
        )
        self.tabs = ttk.Notebook(frame)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        dependencies = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(dependencies, text="依赖与计划")
        dependencies.columnconfigure(1, weight=1)
        dependencies.rowconfigure(0, weight=1)
        selection = ttk.Frame(dependencies)
        selection.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        ttk.Label(selection, text="项目依赖配置").pack(anchor="w", pady=(0, 4))
        self._control(
            ttk.Combobox(
                selection,
                textvariable=self.profile,
                values=tuple(PROFILES),
                state="readonly",
                width=28,
            )
        ).pack(anchor="w")
        ttk.Label(
            selection,
            text="runtime：分发运行依赖\ndevelopment：另加 pytest、mpmath、Ruff、mypy\n构建工具需独立构建环境",
            wraplength=210,
            justify="left",
        ).pack(anchor="w", pady=(3, 10))
        ttk.Label(selection, text="依赖组").pack(anchor="w", pady=(0, 6))
        for group, label in GROUPS.items():
            self._control(
                ttk.Checkbutton(selection, text=label, variable=self.selected[group])
            ).pack(anchor="w", pady=2)
        ttk.Label(selection, text="计算后端").pack(anchor="w", pady=(12, 4))
        self._control(
            ttk.Combobox(
                selection,
                textvariable=self.backend,
                values=BACKENDS,
                state="readonly",
                width=16,
            )
        ).pack(anchor="w")
        ttk.Label(
            selection,
            text="GPU包不代表驱动或硬件可用\nROCm/XPU初版仅手工配置",
            wraplength=180,
        ).pack(anchor="w", pady=8)
        data = ttk.Frame(dependencies)
        data.grid(row=0, column=1, sticky="nsew")
        data.columnconfigure(0, weight=1)
        data.rowconfigure(0, weight=1)
        self.packages = ttk.Treeview(
            data,
            columns=("requirement", "version", "status"),
            show="headings",
            selectmode="browse",
        )
        for key, label, width in (
            ("requirement", "项目要求", 330),
            ("version", "已安装", 110),
            ("status", "状态", 100),
        ):
            self.packages.heading(key, text=label)
            self.packages.column(
                key, width=width, minwidth=80, stretch=key == "requirement"
            )
        self.packages.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(data, orient="vertical", command=self.packages.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.packages.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(
            data, orient="horizontal", command=self.packages.xview
        )
        horizontal.grid(row=1, column=0, sticky="ew")
        self.packages.configure(xscrollcommand=horizontal.set)
        self.packages.tag_configure("conflict", foreground="#B3261E")
        self.packages.tag_configure("missing", foreground="#8B6000")
        self.plan_text = tk.Text(
            dependencies, height=5, wrap="word", state="disabled", font="TkDefaultFont"
        )
        self.plan_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        sources = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(sources, text="镜像与代理")
        sources.columnconfigure(1, weight=1)
        sources.rowconfigure(6, weight=1)
        ttk.Label(sources, text="本次安装源").grid(row=0, column=0, sticky="w", pady=5)
        self._control(
            ttk.Combobox(
                sources,
                textvariable=self.mirror,
                values=tuple(MIRRORS),
                state="readonly",
            )
        ).grid(row=0, column=1, sticky="ew")
        ttk.Label(
            sources,
            text=" / ".join(item["name"] for item in MIRRORS.values()),
            wraplength=650,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(sources, text="代理模式").grid(row=2, column=0, sticky="w", pady=5)
        self._control(
            ttk.Combobox(
                sources,
                textvariable=self.proxy_mode,
                values=("none", "env", "http"),
                state="readonly",
            )
        ).grid(row=2, column=1, sticky="ew")
        ttk.Label(sources, text="HTTP CONNECT代理").grid(
            row=3, column=0, sticky="w", pady=5
        )
        self._control(ttk.Entry(sources, textvariable=self.proxy_url, show="•")).grid(
            row=3, column=1, sticky="ew"
        )
        ttk.Label(
            sources,
            text="none=直连；env=环境代理；http=显式HTTP CONNECT代理。认证信息仅内存，不保存、不进命令行或日志。https://代理 / SOCKS初版不支持。\n测速经同一代理，样本有界真实下载，吞吐/延迟只是估计；推荐不会自动改源。",
            wraplength=650,
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)
        self._control(
            ttk.Button(sources, text="测速并给出推荐", command=self.benchmark)
        ).grid(row=5, column=0, sticky="w", pady=6)
        self.benchmark_text = tk.Text(
            sources, wrap="word", height=8, state="disabled", font="TkDefaultFont"
        )
        self.benchmark_text.grid(row=6, column=0, columnspan=2, sticky="nsew")

        runtime = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(runtime, text="独立运行时")
        runtime.columnconfigure(1, weight=1)
        manifest = runtime_manifest()
        ttk.Label(
            runtime,
            text="Windows 启动脚本可引导安装完整 Python，项目 .venv 不存在仍可启动。\n创建环境另需正常CPython：上方选择已有解释器，或下方获取固定官方CPython。\nWindows受管理3.13不适用于DirectML；Linux/macOS请选择已有解释器。",
            wraplength=650,
        ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=8)
        self._path_row(
            runtime,
            1,
            "受管理目录",
            self.runtime_dir,
            lambda: self._choose_directory(self.runtime_dir),
        )
        ttk.Label(
            runtime,
            text="固定版本 "
            + manifest["version"]
            + "\n官方来源 "
            + manifest["source"]
            + "\n许可 "
            + manifest["license"],
            wraplength=650,
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)
        self._control(
            ttk.Button(
                runtime, text="检查受管理运行时计划…", command=self.obtain_runtime
            )
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(
            runtime,
            text="仅用户目录安装；SHA-256校验后才执行。不会改PATH/默认关联、创建launcher/快捷方式或管理员提权；完整安装器可能登记每用户卸载记录。\n官方embedded包缺pip，不用于创建项目venv。Windows成品尚需无Python干净机器验收。",
            wraplength=650,
        ).grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)

        deployment = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(deployment, text="获取本项目")
        deployment.columnconfigure(1, weight=1)
        deployment.rowconfigure(7, weight=1)
        ttk.Label(deployment, text="本项目：GitHub ii33/coating-sim / 用户指定 CNB 镜像\n先获取源码并生成计划，再确认安装依赖。目标必须是不存在的新目录；不会更新或覆盖已有项目。", wraplength=650).grid(row=0, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(deployment, text="新项目目录").grid(row=1, column=0, sticky="w")
        self._control(ttk.Entry(deployment, textvariable=self.deployment_dir)).grid(row=1, column=1, sticky="ew")
        ttk.Label(deployment, text="分支 / 标签 / 提交").grid(row=2, column=0, sticky="w")
        self._control(ttk.Entry(deployment, textvariable=self.deployment_ref)).grid(row=2, column=1, sticky="ew")
        self._control(ttk.Button(deployment, text="获取源码并检查部署计划…", command=self.prepare_deployment)).grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(deployment, text="源码平台").grid(row=4, column=0, sticky="w")
        self._control(ttk.Combobox(deployment, textvariable=self.deployment_source, values=("github", "cnb"), state="readonly")).grid(row=4, column=1, sticky="ew")
        ttk.Label(deployment, text="仓库 HTTPS URL（CNB 必填）").grid(row=5, column=0, sticky="w")
        self._control(ttk.Entry(deployment, textvariable=self.repository_url)).grid(row=5, column=1, sticky="ew")
        checks = ttk.Frame(deployment)
        checks.grid(row=6, column=0, columnspan=2, sticky="w")
        self._control(ttk.Button(checks, text="本地版本检查（离线）", command=self.check_project)).pack(side="left")
        self._control(ttk.Button(checks, text="检查并比较所选远端", command=lambda: self.check_project(True))).pack(side="left")
        self.deployment_text = tk.Text(deployment, wrap="word", state="disabled", font="TkDefaultFont")
        self.deployment_text.grid(row=7, column=0, columnspan=2, sticky="nsew")

        git_runtime = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(git_runtime, text="私有 Git")
        git_runtime.columnconfigure(1, weight=1)
        git_runtime.rowconfigure(3, weight=1)
        ttk.Label(git_runtime, text="Windows 无 Git 时安装官方固定 MinGit 到用户私有目录；校验 SHA-256，不改系统 PATH 或注册表。先审核来源、许可和目录。", wraplength=650).grid(row=0, column=0, columnspan=3, sticky="ew")
        self._path_row(git_runtime, 1, "MinGit 目录（空=默认）", self.git_runtime_dir, lambda: self._choose_directory(self.git_runtime_dir))
        self._control(ttk.Button(git_runtime, text="检查私有 Git 安装计划…", command=self.prepare_git_runtime)).grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        self.git_runtime_text = tk.Text(git_runtime, wrap="word", state="disabled", font="TkDefaultFont")
        self.git_runtime_text.grid(row=3, column=0, columnspan=3, sticky="nsew")

        entries = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(entries, text="启动入口")
        entries.columnconfigure(1, weight=1)
        entries.rowconfigure(5, weight=1)
        ttk.Label(entries, text="为顶部项目目录创建绑定 .venv 的启动脚本；快捷方式为可选。不会运行项目或覆盖已有文件。", wraplength=650).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(entries, text="快捷方式位置").grid(row=1, column=0, sticky="w")
        self._control(ttk.Combobox(entries, textvariable=self.shortcut_location, values=("none", "desktop", "start-menu", "custom"), state="readonly")).grid(row=1, column=1, sticky="ew")
        ttk.Label(entries, text="custom 目录").grid(row=2, column=0, sticky="w")
        self._control(ttk.Entry(entries, textvariable=self.shortcut_dir)).grid(row=2, column=1, sticky="ew")
        ttk.Label(entries, text="入口名称").grid(row=3, column=0, sticky="w")
        self._control(ttk.Entry(entries, textvariable=self.launch_name)).grid(row=3, column=1, sticky="ew")
        self._control(ttk.Button(entries, text="检查启动入口计划…", command=self.prepare_launch_entries)).grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        self.launch_text = tk.Text(entries, wrap="word", state="disabled", font="TkDefaultFont")
        self.launch_text.grid(row=5, column=0, columnspan=2, sticky="nsew")

        log_frame = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(log_frame, text="日志")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", state="disabled", font="TkFixedFont")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)
        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 5))
        self._control(ttk.Button(actions, text="检查 (F5)", command=self.inspect)).pack(
            side="left", padx=(0, 6)
        )
        self._control(
            ttk.Button(
                actions, text="安装计划…", command=lambda: self.prepare("install")
            )
        ).pack(side="left", padx=6)
        self._control(
            ttk.Button(
                actions, text="修复计划…", command=lambda: self.prepare("repair")
            )
        ).pack(side="left", padx=6)
        self.cancel_button = ttk.Button(
            actions, text="取消运行", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="right")
        ttk.Label(frame, textvariable=self.status, wraplength=900).grid(
            row=4, column=0, sticky="ew"
        )
        self.window.bind("<Configure>", self._resize)

    def _resize(self, event):
        if event.widget is self.window:
            for widget in self.window.winfo_children():
                for child in widget.winfo_children():
                    if isinstance(child, ttk.Label):
                        child.configure(
                            wraplength=max(400, self.window.winfo_width() - 40)
                        )

    def _choose_directory(self, variable):
        chosen = filedialog.askdirectory(
            parent=self.window, initialdir=variable.get() or str(Path.home())
        )
        if chosen:
            variable.set(chosen)

    def choose_project(self):
        old_default = str(Path(self.project.get()) / ".venv")
        self._choose_directory(self.project)
        if self.venv.get() == old_default:
            self.venv.set(str(Path(self.project.get()) / ".venv"))

    def choose_git(self):
        chosen = filedialog.askopenfilename(parent=self.window, title="选择 Git 可执行文件")
        if chosen:
            self.git_path.set(chosen)

    def choose_python(self):
        chosen = filedialog.askopenfilename(
            parent=self.window, title="选择正常 CPython 解释器（不要选工具exe）"
        )
        if chosen:
            self.python.set(chosen)

    def _inputs(self, validate_proxy=True):
        groups = tuple(
            group for group, selected in self.selected.items() if selected.get()
        )
        proxy = ProxyConfig(self.proxy_mode.get(), self.proxy_url.get())
        if validate_proxy:
            proxy.validate()
        return dict(
            root=self.project.get(),
            venv=self.venv.get(),
            groups=groups,
            backend=self.backend.get(),
            profile=self.profile.get(),
        ), proxy

    def _text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", redact(str(text), self.active_proxy) + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self, function, purpose, proxy=None):
        if self.busy:
            return
        self.busy = True
        self.active_proxy = proxy
        self.active_purpose = purpose
        self.cancel_event = threading.Event()
        generation = self.generation
        for widget in self.controls:
            widget.state(["disabled"])
        self.cancel_button.state(["disabled"] if purpose == "launch_create" else ["!disabled"])
        self.status.set("正在创建启动入口，请等待短事务完成（不能中途取消）" if purpose == "launch_create" else "运行中：" + purpose)

        def emit(event):
            self.events.put((generation, "event", event))

        def worker():
            try:
                result = function(emit, self.cancel_event)
            except Exception as exc:
                result = {
                    "schema_version": 1,
                    "operation": purpose,
                    "status": "failed",
                    "errors": [redact(str(exc), proxy)],
                    "warnings": [],
                }
            self.events.put((generation, "done", (purpose, result)))

        threading.Thread(target=worker, daemon=True).start()

    def inspect(self):
        if self.busy:
            return
        try:
            inputs, proxy = self._inputs(validate_proxy=False)
            self._start(
                lambda emit, cancel: inspect_environment(**inputs), "inspect", proxy
            )
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def prepare(self, action):
        if self.busy:
            return
        try:
            inputs, proxy = self._inputs()
            mirror = self.mirror.get()
            python = self.python.get() or None
            self._start(
                lambda emit, cancel: plan_environment(
                    **inputs, mirror=mirror, action=action, python=python, proxy=proxy
                ),
                "plan",
                proxy,
            )
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def prepare_deployment(self):
        if self.busy:
            return
        try:
            inputs, proxy = self._inputs()
            directory, ref = self.deployment_dir.get(), self.deployment_ref.get()
            mirror, python = self.mirror.get(), self.python.get() or None
            source, url = self.deployment_source.get(), self.repository_url.get() or None
            git_path = self.git_path.get() or None
            self._start(
                lambda emit, cancel: plan_project_deployment(
                    directory, ref=ref, groups=inputs["groups"], backend=inputs["backend"],
                    profile=inputs["profile"], mirror=mirror, python=python, proxy=proxy,
                    emit=emit, cancel_event=cancel, source=source, repository_url=url, git_path=git_path),
                "project_plan", proxy)
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def check_project(self, remote=False):
        if self.busy:
            return
        try:
            inputs, proxy = self._inputs(validate_proxy=remote)
            source, url, ref = self.deployment_source.get(), self.repository_url.get() or None, self.deployment_ref.get()
            directory = inputs.pop("root")
            git_path = self.git_path.get() or None
            self._start(lambda emit, cancel: check_project(
                directory, **inputs, check_remote=remote, source=source, repository_url=url,
                ref=ref, proxy=proxy, cancel_event=cancel, git_path=git_path), "project_check", proxy)
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def prepare_git_runtime(self):
        if self.busy:
            return
        from .git_runtime import plan_git_runtime
        try:
            unused, proxy = self._inputs()
            directory = self.git_runtime_dir.get() or None
            self._start(lambda emit, cancel: plan_git_runtime(directory, proxy), "git_plan", proxy)
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def prepare_launch_entries(self):
        if self.busy:
            return
        from .launchers import plan_launch_entries
        root, shortcut, directory, name = self.project.get(), self.shortcut_location.get(), self.shortcut_dir.get() or None, self.launch_name.get()
        self._start(lambda emit, cancel: plan_launch_entries(root, shortcut=shortcut, output_dir=directory, name=name), "launch_plan", ProxyConfig("none"))

    def benchmark(self):
        if self.busy:
            return
        try:
            unused, proxy = self._inputs()
            self._start(
                lambda emit, cancel: benchmark_mirrors(list(MIRRORS), proxy, cancel),
                "benchmark",
                proxy,
            )
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def obtain_runtime(self):
        if self.busy:
            return
        try:
            unused, proxy = self._inputs()
            directory = self.runtime_dir.get()
            backend = self.backend.get()
            self._start(
                lambda emit, cancel: plan_runtime(directory, backend, proxy),
                "runtime_plan",
                proxy,
            )
        except ValueError as exc:
            self.status.set(redact(str(exc)))

    def cancel(self):
        if self.busy and getattr(self, "active_purpose", None) == "launch_create":
            self.status.set("启动入口创建是短事务，请等待完成后再关闭")
            return
        if self.busy:
            self.cancel_event.set()
            self.status.set("正在取消；已执行安装可能部分完成，请稍后重新检查")

    def close(self):
        if self.busy and getattr(self, "active_purpose", None) == "launch_create":
            self.status.set("启动入口创建是短事务，请等待完成后再关闭")
            return
        if self.busy:
            if not messagebox.askyesno(
                "取消并关闭？",
                "安装可能部分完成。要请求取消并关闭吗？若进程树停止失败，仍需手工检查子进程。",
                parent=self.window,
            ):
                return
            self.closing = True
            self.cancel()
            return
        self.generation += 1
        self.window.destroy()

    def _show_packages(self, result):
        inspected = result.get("inspection", result)
        if "packages" not in inspected:
            return
        if (
            inspected.get("status") == "failed"
            and inspected.get("target", {}).get("exists")
            and not inspected["target"]["valid"]
        ):
            return
        old_y = self.packages.yview()[0]
        selected = self.packages.selection()
        self.packages.delete(*self.packages.get_children())
        for row in inspected["packages"]:
            self.packages.insert(
                "",
                "end",
                iid=row["name"],
                values=(
                    row["requirement"],
                    row.get("installed_version") or "缺失",
                    row["status"],
                ),
                tags=(row["status"],),
            )
        self.packages.yview_moveto(old_y)
        for item in selected:
            if self.packages.exists(item):
                self.packages.selection_set(item)
        target = inspected["target"]
        self.identity.set(
            "目标解释器："
            + target["interpreter"]
            + "\nPython "
            + (target.get("python") or "未创建/损坏")
            + " · 身份 "
            + ("isolated venv" if target["valid"] else "待确认/不可用")
            + " · 依赖配置 "
            + str(inspected.get("profile", "runtime"))
        )

    def _confirm_plan(self, title, text):
        # A long argv/requirement plan must not push native messagebox buttons
        # below a small screen. Keep the text scrollable and actions fixed.
        dialog = tk.Toplevel(self.window)
        dialog.title(title)
        dialog.transient(self.window)
        width = min(850, max(500, self.window.winfo_screenwidth() - 80))
        height = min(600, max(320, self.window.winfo_screenheight() - 100))
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(min(500, width), min(320, height))
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        review = tk.Text(body, wrap="word", font="TkDefaultFont")
        review.insert("end", text)
        review.configure(state="disabled")
        review.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, command=review.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        review.configure(yscrollcommand=scroll.set)
        actions = ttk.Frame(body)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        accepted = [False]

        def decide(value):
            accepted[0] = value
            dialog.destroy()

        ttk.Button(actions, text="取消", command=lambda: decide(False)).pack(
            side="right"
        )
        ttk.Button(actions, text="确认执行此计划", command=lambda: decide(True)).pack(
            side="right", padx=8
        )
        dialog.protocol("WM_DELETE_WINDOW", lambda: decide(False))
        dialog.bind("<Escape>", lambda unused: decide(False))
        dialog.grab_set()
        review.focus_set()
        self.window.wait_window(dialog)
        return accepted[0]

    def _finish(self, purpose, result):
        self.last_result = result
        self._show_packages(result)
        details = "\n".join(result.get("errors", []) + result.get("warnings", []))
        self.status.set(
            result["status"] + (" · " + details.split("\n")[0] if details else "")
        )
        self._append_log(json.dumps(result, ensure_ascii=False))
        if purpose == "git_plan":
            from .git_runtime import execute_git_runtime
            description = json.dumps(result, ensure_ascii=False, indent=2)
            self._text(self.git_runtime_text, description)
            if result["status"] == "ready" and self._confirm_plan("确认官方私有 Git 安装", description):
                proxy = self.active_proxy
                self._start(lambda emit, cancel: execute_git_runtime(result, True, emit, cancel, proxy), "git_install", proxy)
        elif purpose == "git_install":
            self._text(self.git_runtime_text, json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] in ("success", "completed"):
                from .git_runtime import find_managed_git
                selected = find_managed_git(self.git_runtime_dir.get() or None)
                if selected:
                    self.git_path.set(selected)
        elif purpose == "launch_plan":
            from .launchers import execute_launch_entries
            description = json.dumps(result, ensure_ascii=False, indent=2)
            self._text(self.launch_text, description)
            if result["status"] == "ready" and self._confirm_plan("确认创建启动入口", description):
                self._start(lambda emit, cancel: execute_launch_entries(result, confirmed=True), "launch_create", ProxyConfig("none"))
        elif purpose == "launch_create":
            self._text(self.launch_text, json.dumps(result, ensure_ascii=False, indent=2))
        elif purpose == "project_check":
            self._text(self.deployment_text, json.dumps(result, ensure_ascii=False, indent=2))
        elif purpose == "project_plan":
            description = json.dumps(result, ensure_ascii=False, indent=2)
            self._text(self.deployment_text, description)
            if result["status"] == "ready" and self._confirm_plan("确认本项目依赖部署", description):
                proxy = self.active_proxy
                self._start(lambda emit, cancel: execute_project_deployment(
                    result, confirmed=True, emit=emit, cancel_event=cancel, proxy=proxy),
                    "project_deploy", proxy)
        elif purpose == "project_deploy":
            self._text(self.deployment_text, json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] == "success":
                self.project.set(result["directory"])
                self.venv.set(str(Path(result["directory"]) / ".venv"))
        elif purpose == "plan":
            self.pending_plan = result
            summary = "\n".join(
                action["kind"] + ": " + " ".join(action["argv"])
                for action in result["actions"]
            )
            self._text(self.plan_text, summary + "\n" + details)
            if result["status"] != "ready":
                return
            proxy = self.active_proxy
            text = (
                "目标："
                + result["target"]["venv"]
                + "\n解释器："
                + result["target"]["interpreter"]
                + "\n依赖配置："
                + result.get("profile", "runtime")
                + "\n镜像："
                + MIRRORS[result["mirror"]]["url"]
                + "\n代理："
                + json.dumps(result["proxy"], ensure_ascii=False)
                + "\n\n"
                + summary
                + "\n\n取消/失败可能部分安装；不会显式卸载无关包。\n要执行此计划吗？"
            )
            if self._confirm_plan("确认安装/修复计划", text):
                self._start(
                    lambda emit, cancel: execute_plan(
                        result, True, emit, cancel, proxy
                    ),
                    result["action"],
                    proxy,
                )
        elif purpose == "runtime_plan" and result["status"] == "ready":
            proxy = self.active_proxy
            text = (
                "来源："
                + result["artifact"]["url"]
                + "\nSHA-256："
                + result["artifact"]["sha256"]
                + "\n目标："
                + result["directory"]
                + "\n许可："
                + result["license"]
                + "\n\n"
                + details
                + "\n\n确认下载、安装并接受此CPython许可？"
            )
            if self._confirm_plan("确认受管理 CPython", text):
                self._start(
                    lambda emit, cancel: execute_runtime(
                        result, True, emit, cancel, proxy
                    ),
                    "runtime_install",
                    proxy,
                )
        elif purpose == "runtime_install" and result["status"] == "success":
            self.python.set(result["python"])
        elif purpose == "benchmark":
            lines = []
            for item in result.get("results", []):
                lines.append(
                    f"{item.get('mirror')} · {item.get('latency_ms')} ms · {item.get('throughput_mbps')} Mbps · {item.get('download_bytes')} bytes · {item.get('download_host')} ({item.get('sample_origin')}) · {item.get('error') or item.get('sample', '')}"
                )
            self._text(
                self.benchmark_text,
                "\n".join(lines)
                + "\n推荐依据："
                + str(result.get("recommendation_reason", ""))
                + "\n"
                + details,
            )
            recommended = result.get("recommended_mirror")
            if (
                recommended
                and result["status"] == "success"
                and messagebox.askyesno(
                    "测速推荐",
                    f"样本结果推荐 {recommended}，仅为当前网络估计。要选为本次安装源吗？",
                    parent=self.window,
                )
            ):
                self.mirror.set(recommended)

    def _drain(self):
        while True:
            try:
                generation, kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            if kind == "event":
                if data.get("event") == "log":
                    self._append_log(data["text"])
                elif data.get("event") in ("phase", "progress"):
                    self.status.set(
                        "运行中："
                        + str(data.get("phase", ""))
                        + " "
                        + str(data.get("download_bytes", ""))
                    )
            else:
                self.busy = False
                for widget in self.controls:
                    widget.state(["!disabled"])
                self.cancel_button.state(["disabled"])
                if self.closing:
                    self.generation += 1
                    self.window.destroy()
                    return
                self._finish(*data)
        self.window.after(50, self._drain)


def run_gui(project, venv=None):
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except (AttributeError, OSError):
            pass
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        print(
            "GUI requires Python Tk and a desktop display: " + str(exc),
            file=sys.stderr,
        )
        return 2
    DependencyManagerWindow(window, project, venv)
    window.mainloop()
    return 0
