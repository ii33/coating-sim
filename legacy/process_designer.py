"""Standalone editor for a process file that selects machine and substrate."""

import os
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from coat_sim.core.machine_profiles import read_machine_profile, resolve_machine_profile
from coat_sim.core.process_profiles import (
    PROCESS_SCHEMA_VERSION, TARGET_MODE_LABELS, read_process_profile,
    resolve_process_profile, validate_process_profile, write_process_profile,
)
from coat_sim.core.substrate_profiles import read_substrate_profile
from legacy.window_layout import maximize_on_open


class ProcessDesigner:
    """Edit process steps plus paths to independent machine and substrate files."""

    def __init__(self, parent, initial_path=None, on_saved=None):
        self.parent = parent
        self.on_saved = on_saved
        self.path = str(Path(initial_path).resolve()) if initial_path else None
        self.steps = [{"source_slot": 1, "target_nm": 100.0,
                       "rate_nm_s": 1.0, "target_mode": "mean"}]
        self.runtime = None
        self.errors = []
        self.source_options = {}
        self.monitor_options = {}
        self.source_signature = None
        self.editor_error = ""
        self.editing_index = None
        self.loading = False
        self.refreshing = False
        self.scroll_pending = 0
        self.scroll_after = None
        self.resize_after = None
        self.extent_after = None
        self.editor_after = None
        self.dependency_after = None

        self.win = tk.Toplevel(parent)
        self.win.title("工艺设计器")
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        width, height = min(1380, max(920, sw - 70)), min(850, max(620, sh - 90))
        self.win.geometry(f"{width}x{height}+{max(0, (sw-width)//2)}+{max(0, (sh-height)//2)}")
        self.win.minsize(min(950, width), min(620, height))
        self.win.resizable(True, True)
        maximize_on_open(self.win)
        style = ttk.Style(self.win)
        from tkinter import font as tkfont
        font = tkfont.nametofont("TkDefaultFont")
        style.configure("Process.Treeview", font=font,
                        rowheight=max(32, font.metrics("linespace") + 8))
        style.configure("Process.Treeview.Heading", font=font, padding=(6, 3))

        self.name_var = tk.StringVar(value="新工艺")
        self.machine_var = tk.StringVar()
        self.substrate_var = tk.StringVar()
        self.path_var = tk.StringVar(value=self.path or "当前文件：未保存")
        self.machine_error_var = tk.StringVar()
        self.substrate_error_var = tk.StringVar()
        self.status_var = tk.StringVar(value="工艺保存机器、基片文件路径和镀膜步骤。")
        self.source_var = tk.StringVar(value="1")
        self.target_var = tk.StringVar(value="100")
        self.rate_var = tk.StringVar(value="1")
        self.mode_var = tk.StringVar(value=TARGET_MODE_LABELS["mean"])
        self.monitor_var = tk.StringVar()
        self.mode_options = {label: key for key, label in TARGET_MODE_LABELS.items()}

        footer = ttk.Frame(self.win, padding=(10, 5))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var, wraplength=790).pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(footer, text="关闭", command=self.win.destroy).pack(side=tk.RIGHT)
        self.save_button = ttk.Button(footer, text="保存到当前文件",
                                      command=self.save)
        self.save_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.save_as_button = ttk.Button(footer, text="另存为新文件…",
                                         command=self.save_as)
        self.save_as_button.pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(footer, text="打开工艺…", command=self.open_profile).pack(
            side=tk.RIGHT, padx=(0, 8))

        host = ttk.Frame(self.win)
        host.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(host, highlightthickness=0,
                           background=style.lookup("TFrame", "background"))
        scroll = ttk.Scrollbar(host, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = ttk.Frame(canvas, padding=8)
        self.canvas = canvas
        self.body = body
        self.body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", self._schedule_extent)
        canvas.bind("<Configure>", self._schedule_resize)
        self.win.bind("<MouseWheel>", self._wheel, add="+")
        self.win.bind("<Button-4>", self._wheel, add="+")
        self.win.bind("<Button-5>", self._wheel, add="+")

        ttk.Label(body, text="步骤按机器中的源与监测器位次引用；调整机器顺序后请核对。",
                  foreground="#475569").pack(anchor="w", pady=(0, 5))
        metadata = ttk.LabelFrame(body, text="工艺文件", padding=6)
        metadata.pack(fill=tk.X, pady=(0, 6))
        metadata.columnconfigure(1, weight=1)
        ttk.Label(metadata, text="工艺名称").grid(row=0, column=0, sticky="w")
        ttk.Entry(metadata, textvariable=self.name_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(metadata, text="机器文件").grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Entry(metadata, textvariable=self.machine_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))
        ttk.Button(metadata, text="选择…", command=self.choose_machine).grid(
            row=1, column=2, padx=(8, 0), pady=(3, 0))
        self.machine_error_label = ttk.Label(
            metadata, textvariable=self.machine_error_var,
            foreground="#b91c1c", wraplength=1000)
        self.machine_error_label.grid(row=2, column=0, columnspan=3, sticky="w")
        self.machine_error_label.grid_remove()
        ttk.Label(metadata, text="基片文件").grid(row=3, column=0, sticky="w", pady=(3, 0))
        ttk.Entry(metadata, textvariable=self.substrate_var).grid(
            row=3, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))
        ttk.Button(metadata, text="选择…", command=self.choose_substrate).grid(
            row=3, column=2, padx=(8, 0), pady=(3, 0))
        self.substrate_error_label = ttk.Label(
            metadata, textvariable=self.substrate_error_var,
            foreground="#b91c1c", wraplength=1000)
        self.substrate_error_label.grid(row=4, column=0, columnspan=3, sticky="w")
        self.substrate_error_label.grid_remove()
        ttk.Label(metadata, textvariable=self.path_var, foreground="#475569").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(3, 0))

        steps_box = ttk.LabelFrame(body, text="镀膜步骤", padding=6)
        steps_box.pack(fill=tk.X, pady=(0, 6))
        steps_box.columnconfigure(0, weight=1)
        columns = ("number", "source", "mode", "target", "rate", "monitor")
        self.tree = ttk.Treeview(steps_box, columns=columns, show="headings",
                                 selectmode="browse", height=4,
                                 style="Process.Treeview")
        for key, label, width in (
            ("number", "序号", 70), ("source", "源位次", 240),
            ("mode", "厚度基准", 180), ("target", "目标 / nm", 150),
            ("rate", "速率 / nm/s", 160), ("monitor", "监测器位次", 220)
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=width,
                             stretch=key in ("source", "monitor"))
        self.tree.grid(row=0, column=0, sticky="ew")
        step_scroll = ttk.Scrollbar(steps_box, orient=tk.VERTICAL,
                                    command=self.tree.yview)
        step_scroll.grid(row=0, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(steps_box, orient=tk.HORIZONTAL,
                                 command=self.tree.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=step_scroll.set,
                            xscrollcommand=hscroll.set)
        self.tree.tag_configure("error", foreground="#b91c1c")
        self.tree.bind("<<TreeviewSelect>>", self._select_step)

        editor = ttk.LabelFrame(body, text="选中步骤参数", padding=6)
        editor.pack(fill=tk.X)
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(3, weight=1)
        ttk.Label(editor, text="源位次").grid(row=0, column=0, sticky="w", pady=2)
        self.source_box = ttk.Combobox(editor, textvariable=self.source_var)
        self.source_box.grid(row=0, column=1, sticky="ew", padx=(8, 14), pady=2)
        ttk.Label(editor, text="目标厚度 / nm").grid(row=0, column=2, sticky="w", pady=2)
        ttk.Entry(editor, textvariable=self.target_var).grid(
            row=0, column=3, sticky="ew", padx=(8, 0), pady=2)
        ttk.Label(editor, text="厚度基准").grid(row=1, column=0, sticky="w", pady=2)
        self.mode_box = ttk.Combobox(editor, textvariable=self.mode_var,
                                     values=list(self.mode_options), state="readonly")
        self.mode_box.grid(row=1, column=1, sticky="ew", padx=(8, 14), pady=2)
        ttk.Label(editor, text="沉积速率 / nm/s").grid(row=1, column=2,
                                                    sticky="w", pady=2)
        ttk.Entry(editor, textvariable=self.rate_var).grid(
            row=1, column=3, sticky="ew", padx=(8, 0), pady=2)
        ttk.Label(editor, text="监测器位次").grid(row=2, column=0, sticky="w", pady=2)
        self.monitor_box = ttk.Combobox(editor, textvariable=self.monitor_var)
        self.monitor_box.grid(row=2, column=1, columnspan=3, sticky="ew",
                              padx=(8, 0), pady=2)
        controls = ttk.Frame(body)
        controls.pack(fill=tk.X, pady=(5, 6))
        ttk.Button(controls, text="添加步骤", command=self.add_step).pack(side=tk.LEFT)
        ttk.Button(controls, text="删除选中", command=self.delete_step).pack(
            side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="上移", command=lambda: self.move_step(-1)).pack(
            side=tk.LEFT, padx=(18, 0))
        ttk.Button(controls, text="下移", command=lambda: self.move_step(1)).pack(
            side=tk.LEFT, padx=(8, 0))

        self.name_var.trace_add("write", self._name_changed)
        for variable in (self.machine_var, self.substrate_var):
            variable.trace_add("write", self._schedule_dependency_refresh)
        for variable in (self.source_var, self.target_var, self.rate_var,
                         self.mode_var, self.monitor_var):
            variable.trace_add("write", self._schedule_editor_update)
        self._update_identity()
        self._refresh_rows(select=0)
        if self.path:
            try:
                self._load_profile(read_process_profile(self.path), self.path)
            except (OSError, ValueError) as exc:
                self.status_var.set(f"打开工艺失败：{exc}")
                self.path = None
                self.path_var.set("当前文件：未保存")
        self.refresh_dependencies()

    def _schedule_extent(self, _event=None):
        if self.extent_after is None:
            self.extent_after = self.win.after(45, self._update_extent)

    def _update_extent(self):
        self.extent_after = None
        if self.canvas.winfo_exists():
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _schedule_resize(self, event):
        if self.resize_after is not None:
            self.win.after_cancel(self.resize_after)
        self.resize_after = self.win.after(
            50, lambda width=event.width: self._resize(width))

    def _resize(self, width):
        self.resize_after = None
        if self.canvas.winfo_exists():
            self.canvas.itemconfigure(self.body_window, width=width)

    def _wheel(self, event):
        widget = self.win.winfo_containing(event.x_root, event.y_root)
        current = widget
        while current is not None:
            if current is self.tree:
                return None
            if current in (self.body, self.canvas):
                break
            current = getattr(current, "master", None)
        if current is None:
            return None
        delta = getattr(event, "delta", 0)
        self.scroll_pending += (-3 if getattr(event, "num", None) == 4 or
                                delta > 0 else 3)
        if self.scroll_after is None:
            self.scroll_after = self.win.after(45, self._flush_scroll)
        return "break"

    def _flush_scroll(self):
        self.scroll_after = None
        amount, self.scroll_pending = self.scroll_pending, 0
        if amount and self.canvas.winfo_exists():
            self.canvas.yview_scroll(amount, "units")

    def _update_identity(self):
        name = self.name_var.get().strip() or "未命名工艺"
        self.win.title(f"{name[:40]} · 工艺设计器")

    def _name_changed(self, *_):
        self._update_identity()
        if not self.loading:
            self._update_save_state()

    def _schedule_dependency_refresh(self, *_):
        if self.loading:
            return
        if self.dependency_after is not None:
            self.win.after_cancel(self.dependency_after)
        self.dependency_after = self.win.after(240, self.refresh_dependencies)

    def _draft(self):
        return {"schema_version": PROCESS_SCHEMA_VERSION,
                "name": self.name_var.get().strip(),
                "machine_file": self.machine_var.get().strip(),
                "substrate_file": self.substrate_var.get().strip(),
                "steps": [dict(step) for step in self.steps]}

    def _relative_dependency_file(self, entered, process_dir, label):
        entered = entered.strip()
        if not entered:
            raise ValueError(f"请先选择{label}文件")
        target_path = Path(entered).expanduser()
        if not target_path.is_absolute():
            target_path = (Path(self.path).parent if self.path else Path.cwd()) / target_path
        try:
            return os.path.relpath(target_path.resolve(), Path(process_dir).resolve())
        except ValueError as exc:
            raise ValueError(f"{label}与工艺必须保存在同一磁盘卷，无法创建相对引用") from exc

    def _source_label(self, slot):
        return next((label for label, value in self.source_options.items()
                     if value == slot), f"⚠ 第 {slot} 位源已失效")

    def _monitor_label(self, slot):
        return next((label for label, value in self.monitor_options.items()
                     if value == slot), f"⚠ 第 {slot} 位监测器已失效")

    def _step_error(self, step):
        if self.runtime is None or self.runtime.get("machine") is None:
            return "机器未载入"
        machine = self.runtime["machine"]
        sources = machine.get("sources", [])
        slot = step["source_slot"]
        if slot > len(sources) or sources[slot - 1].get("profile") is None:
            return f"源位次 {slot} 无效"
        if step["target_mode"] == "monitor":
            slot = step["monitor_slot"]
            if slot > len(machine.get("monitors", [])):
                return f"监测器位次 {slot} 无效"
        return ""

    def _refresh_rows(self, select=None):
        self.refreshing = True
        try:
            self.tree.delete(*self.tree.get_children())
            for index, step in enumerate(self.steps):
                issue = self._step_error(step)
                source = self._source_label(step["source_slot"])
                if issue:
                    source = f"⚠ {source} · {issue}"
                monitor = (self._monitor_label(step["monitor_slot"])
                           if step["target_mode"] == "monitor" else "—")
                self.tree.insert("", tk.END, iid=str(index),
                                 values=(index + 1, source,
                                         TARGET_MODE_LABELS[step["target_mode"]],
                                         f"{step['target_nm']:g}",
                                         f"{step['rate_nm_s']:g}", monitor),
                                 tags=("error",) if issue else ())
            if select is not None and 0 <= select < len(self.steps):
                self.tree.selection_set(str(select))
                self.tree.focus(str(select))
                self.tree.see(str(select))
        finally:
            self.refreshing = False
        if select is not None and 0 <= select < len(self.steps):
            self._load_step(select)

    def _load_step(self, index):
        self.loading = True
        try:
            step = self.steps[index]
            self.source_var.set(self._source_label(step["source_slot"]))
            self.target_var.set(f"{step['target_nm']:g}")
            self.rate_var.set(f"{step['rate_nm_s']:g}")
            self.mode_var.set(TARGET_MODE_LABELS[step["target_mode"]])
            self.monitor_var.set(self._monitor_label(step.get("monitor_slot", 1)))
            self.editing_index = index
            self.editor_error = ""
            self.monitor_box.configure(
                state="normal" if step["target_mode"] == "monitor" else "disabled")
        finally:
            self.loading = False
        self._update_save_state()

    def _select_step(self, _event=None):
        if self.refreshing or self.loading:
            return
        selected = self.tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if index == self.editing_index:
            return
        try:
            self._commit_editor()
        except ValueError as exc:
            if self.editing_index is not None:
                self.refreshing = True
                self.tree.selection_set(str(self.editing_index))
                self.refreshing = False
            self.status_var.set(f"当前步骤输入无效：{exc}")
            return
        self._load_step(index)

    def _read_editor(self):
        source_text = self.source_var.get().strip()
        source_slot = self.source_options.get(source_text)
        if source_slot is None:
            source_slot = int(source_text) if source_text.isdecimal() else None
        mode = self.mode_options.get(self.mode_var.get())
        if mode is None:
            raise ValueError("请选择厚度基准")
        draft = {"source_slot": source_slot, "target_nm": self.target_var.get(),
                 "rate_nm_s": self.rate_var.get(), "target_mode": mode}
        if mode == "monitor":
            monitor_text = self.monitor_var.get().strip()
            monitor_slot = self.monitor_options.get(monitor_text)
            if monitor_slot is None:
                monitor_slot = int(monitor_text) if monitor_text.isdecimal() else None
            draft["monitor_slot"] = monitor_slot
        candidate = {"schema_version": PROCESS_SCHEMA_VERSION,
                     "name": "临时步骤", "machine_file": "machine.json",
                     "substrate_file": "substrate.json",
                     "steps": [draft]}
        return validate_process_profile(candidate)["steps"][0]

    def _schedule_editor_update(self, *_):
        if self.loading:
            return
        if self.editor_after is not None:
            self.win.after_cancel(self.editor_after)
        self.editor_after = self.win.after(180, self._apply_editor)

    def _apply_editor(self):
        self.editor_after = None
        if self.editing_index is None or self.editing_index >= len(self.steps):
            return
        self.monitor_box.configure(
            state="normal" if self.mode_options.get(self.mode_var.get()) == "monitor"
            else "disabled")
        try:
            step = self._read_editor()
        except ValueError as exc:
            self.editor_error = str(exc)
            self.status_var.set(f"当前步骤输入无效：{exc}")
            self._update_save_state()
            return
        self.editor_error = ""
        if step != self.steps[self.editing_index]:
            self.steps[self.editing_index] = step
            self._refresh_rows(select=self.editing_index)
            self.status_var.set("步骤已在编辑器中更新；保存后写入文件。")
        self._update_save_state()

    def _commit_editor(self):
        if self.editor_after is not None:
            self.win.after_cancel(self.editor_after)
            self.editor_after = None
        if self.editing_index is None or self.editing_index >= len(self.steps):
            return
        step = self._read_editor()
        self.editor_error = ""
        if step != self.steps[self.editing_index]:
            self.steps[self.editing_index] = step
            self._refresh_rows(select=self.editing_index)

    def choose_machine(self):
        options = {"parent": self.win, "title": "选择机器配置",
                   "filetypes": [("机器 JSON", "*.json"), ("所有文件", "*.*")]}
        if self.machine_var.get().strip():
            options["initialdir"] = str(Path(self.machine_var.get()).parent)
        path = filedialog.askopenfilename(**options)
        if path:
            self.machine_var.set(str(Path(path).resolve()))
            self.refresh_dependencies()

    def choose_substrate(self):
        options = {"parent": self.win, "title": "选择基片配置",
                   "filetypes": [("基片 JSON", "*.json"), ("所有文件", "*.*")]}
        if self.substrate_var.get().strip():
            options["initialdir"] = str(Path(self.substrate_var.get()).parent)
        path = filedialog.askopenfilename(**options)
        if path:
            self.substrate_var.set(str(Path(path).resolve()))
            self.refresh_dependencies()

    def refresh_dependencies(self):
        self.dependency_after = None
        errors = []
        runtime = None
        draft = self._draft()
        if not draft["name"]:
            errors.append("name: 请输入工艺名称")
            draft["name"] = "未命名工艺"
        if not draft["steps"]:
            errors.append("steps: 工艺至少需要一道步骤")
            draft["steps"] = [{"source_slot": 1, "target_nm": 100.0,
                               "rate_nm_s": 1.0, "target_mode": "mean"}]
        base_dir = Path(self.path).parent if self.path else Path.cwd()
        for field, variable, label in (("machine_file", self.machine_var, "机器"),
                                       ("substrate_file", self.substrate_var, "基片")):
            try:
                draft[field] = self._relative_dependency_file(variable.get(), base_dir, label)
            except ValueError as exc:
                errors.append(f"{field}: {exc}")
        if not any(issue.startswith(("machine_file:", "substrate_file:"))
                   for issue in errors):
            try:
                runtime, dependency_errors = resolve_process_profile(
                    draft, base_dir)
                errors.extend(dependency_errors)
            except (OSError, ValueError, TypeError) as exc:
                errors.append(str(exc))
        else:
            # A missing file does not hide independent errors or machine slots.
            if self.machine_var.get().strip() and "machine_file" in draft:
                machine_path = (base_dir / draft["machine_file"]).resolve()
                try:
                    machine_profile = read_machine_profile(machine_path)
                    machine, machine_errors = resolve_machine_profile(
                        machine_profile, machine_path.parent)
                    runtime = {"machine": machine}
                    errors.extend(f"machine_file → {issue}" for issue in machine_errors)
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(f"machine_file: {exc}")
            if self.substrate_var.get().strip() and "substrate_file" in draft:
                substrate_path = (base_dir / draft["substrate_file"]).resolve()
                try:
                    read_substrate_profile(substrate_path)
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(f"substrate_file: {exc}")
        self.runtime, self.errors = runtime, errors
        machine = runtime.get("machine") if runtime else None
        if machine:
            sources = machine.get("sources", [])
            monitors = machine.get("monitors", [])
            new_signature = tuple((str(source.get("file", "")),
                                   source.get("x_mm"), source.get("y_mm"))
                                  for source in sources)
            if self.source_signature and self.source_signature != new_signature and \
                    sorted(self.source_signature) == sorted(new_signature):
                self.status_var.set("⚠ 机器源位次已重排；请逐道核对工序源位次后保存。")
            self.source_signature = new_signature
            self.source_options = {
                f"{index}. {source['profile'].get('name', Path(source['file']).stem) if source.get('profile') else '⚠ 源文件无效'}":
                index for index, source in enumerate(sources, 1)}
            self.monitor_options = {
                f"{index}. {monitor['name']}（{'晶控' if monitor['kind'] == 'crystal' else '光控'}）":
                index for index, monitor in enumerate(monitors, 1)}
        else:
            self.source_options = {}
            self.monitor_options = {}
        self.source_box.configure(values=list(self.source_options))
        self.monitor_box.configure(values=list(self.monitor_options))
        machine_issues = [issue for issue in errors if issue.startswith("machine_file")]
        substrate_issues = [issue for issue in errors if issue.startswith("substrate_file")]
        self.machine_error_var.set("；".join(machine_issues))
        self.substrate_error_var.set("；".join(substrate_issues))
        if machine_issues:
            self.machine_error_label.grid()
        else:
            self.machine_error_label.grid_remove()
        if substrate_issues:
            self.substrate_error_label.grid()
        else:
            self.substrate_error_label.grid_remove()
        self._refresh_rows(select=self.editing_index)
        self._update_save_state()

    def _update_save_state(self):
        issues = [issue for issue in self.errors if not issue.startswith("name:")]
        name = self.name_var.get().strip()
        if not 1 <= len(name) <= 100:
            issues.insert(0, "请输入 1–100 个字符的工艺名称")
        if self.editor_error:
            issues.append(self.editor_error)
        if not self.steps:
            issues.append("工艺至少需要一道步骤")
        if any(self._step_error(step) for step in self.steps):
            issues.append("有步骤引用失效")
        enabled = not issues
        self.save_button.configure(state="normal" if enabled else "disabled")
        self.save_as_button.configure(state="normal" if enabled else "disabled")
        if issues and not self.status_var.get().startswith("⚠ 机器源位次"):
            self.status_var.set(f"无法保存：{issues[0]}")
        elif enabled and self.status_var.get().startswith("无法保存："):
            self.status_var.set("文件与步骤已就绪，可以保存。")

    def add_step(self):
        try:
            self._commit_editor()
            step = self._read_editor()
        except ValueError as exc:
            messagebox.showerror("步骤无效", str(exc), parent=self.win)
            return
        if len(self.steps) >= 100:
            messagebox.showerror("步骤过多", "最多支持 100 道步骤", parent=self.win)
            return
        self.steps.append(step)
        self._refresh_rows(select=len(self.steps) - 1)
        self.refresh_dependencies()

    def delete_step(self):
        selected = self.tree.selection()
        if not selected:
            return
        index = int(selected[0])
        del self.steps[index]
        self.editing_index = None
        self._refresh_rows(select=min(index, len(self.steps) - 1))
        self.refresh_dependencies()

    def move_step(self, direction):
        selected = self.tree.selection()
        if not selected:
            return
        try:
            self._commit_editor()
        except ValueError as exc:
            messagebox.showerror("步骤无效", str(exc), parent=self.win)
            return
        index = int(selected[0])
        other = index + direction
        if not 0 <= other < len(self.steps):
            return
        self.steps[index], self.steps[other] = self.steps[other], self.steps[index]
        self.editing_index = None
        self._refresh_rows(select=other)
        self.refresh_dependencies()

    def _load_profile(self, profile, path):
        self.loading = True
        try:
            self.path = str(Path(path).resolve())
            machine_path = Path(profile["machine_file"])
            if not machine_path.is_absolute():
                machine_path = Path(self.path).parent / machine_path
            substrate_path = Path(profile["substrate_file"])
            if not substrate_path.is_absolute():
                substrate_path = Path(self.path).parent / substrate_path
            self.name_var.set(profile["name"])
            self.machine_var.set(str(machine_path.resolve()))
            self.substrate_var.set(str(substrate_path.resolve()))
            self.path_var.set(f"当前文件：{self.path}")
            self.steps = [dict(step) for step in profile["steps"]]
            self.editing_index = None
            self.source_signature = None
        finally:
            self.loading = False
        self._update_identity()
        self.refresh_dependencies()
        self._refresh_rows(select=0)

    def open_profile(self):
        options = {"parent": self.win, "title": "打开工艺文件",
                   "filetypes": [("工艺 JSON", "*.json"), ("所有文件", "*.*")]}
        if self.path:
            options["initialdir"] = str(Path(self.path).parent)
        path = filedialog.askopenfilename(**options)
        if not path:
            return
        try:
            profile = read_process_profile(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.win)
            return
        self._load_profile(profile, path)
        if self.on_saved:
            self.on_saved(self.path, profile)
        self.status_var.set(f"已打开工艺“{profile['name']}”：{Path(path).name}")

    def _save_to(self, path):
        try:
            self._commit_editor()
            self.refresh_dependencies()
            if self.errors or self.editor_error:
                raise ValueError("\n".join(self.errors) or self.editor_error)
            profile = self._draft()
            destination = Path(path).resolve().parent
            profile["machine_file"] = self._relative_dependency_file(
                self.machine_var.get(), destination, "机器")
            profile["substrate_file"] = self._relative_dependency_file(
                self.substrate_var.get(), destination, "基片")
            profile = write_process_profile(path, profile)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("无法保存工艺", str(exc), parent=self.win)
            return
        self.path = str(Path(path).resolve())
        self.path_var.set(f"当前文件：{self.path}")
        self._update_identity()
        self.status_var.set(f"已保存工艺“{profile['name']}”：{Path(self.path).name}")
        if self.on_saved:
            self.on_saved(self.path, profile)

    def save(self):
        """Write the active file directly; only a new document needs a path."""
        if not self.path:
            self.save_as()
        else:
            self._save_to(self.path)

    def save_as(self):
        """Always choose a new file path, preserving both dependency targets."""
        options = {"parent": self.win, "title": "另存工艺为新文件",
                   "defaultextension": ".json",
                   "filetypes": [("工艺 JSON", "*.json"), ("所有文件", "*.*")],
                   "initialfile": Path(self.path).name if self.path else "process.json"}
        if self.path:
            options["initialdir"] = str(Path(self.path).parent)
        path = filedialog.asksaveasfilename(**options)
        if path:
            if self.path and Path(path).resolve() == Path(self.path).resolve():
                messagebox.showerror("另存为需要新路径",
                                     "请选择与当前文件不同的路径；覆盖当前文件请使用“保存到当前文件”。",
                                     parent=self.win)
                return
            self._save_to(path)


def main():
    if sys.platform == "win32":
        # Enable native-resolution Tk text before the first window is created.
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    root.withdraw()
    from tkinter import font as tkfont
    candidates = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                  "Noto Sans CJK SC", "WenQuanYi Micro Hei", "SimHei")
    installed = set(tkfont.families(root))
    family = next((candidate for candidate in candidates if candidate in installed),
                  tkfont.nametofont("TkDefaultFont").actual("family"))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(family=family, size=18)
    tkfont.nametofont("TkHeadingFont").configure(
        family=family, size=18, weight="bold")
    style = ttk.Style(root)
    body_font = tkfont.nametofont("TkDefaultFont")
    for widget_style in (".", "TLabel", "TButton", "TEntry", "TCombobox",
                         "TLabelframe.Label"):
        style.configure(widget_style, font=body_font)
    editor = ProcessDesigner(root)
    editor.win.bind("<Destroy>",
                    lambda event: root.destroy() if event.widget is editor.win else None,
                    add="+")
    root.mainloop()


if __name__ == "__main__":
    main()
