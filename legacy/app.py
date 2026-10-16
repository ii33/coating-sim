"""Desktop UI: edit freely, validate on focus change, calculate on request."""

import dataclasses
import copy
import math
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib import font_manager, rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import matplotlib.pyplot as plt
import numpy as np

from coat_sim.core.simulator import Settings, simulate, workload
from coat_sim.core.gear_geometry import center_distance_from_module, module_from_center_distance
from coat_sim.core.machine_profiles import MACHINE_FIELDS, MACHINE_LABELS
from coat_sim.core.process_profiles import read_process_profile, resolve_process_profile
from coat_sim.core.process_simulator import process_timing, simulate_process
from coat_sim.core.calculation_history import CalculationHistory, format_elapsed
from legacy.window_layout import maximize_on_open


FIELDS = [
    ("运动", [("carrier_rpm", "大盘转速 (rpm)"),
             ("carrier_phase_deg", "大盘初相 (°)"),
             ("planet_phase_deg", "行星盘初相 (°)")]),
    ("计算", [("time_step_s", "时间步长 (s)"), ("time_steps", "时间步数"),
              ("grid_size", "网格边长")]),
]
MACHINE_SETTINGS_FIELDS = (*MACHINE_FIELDS, "gear_module_mm")
MACHINE_EDIT_FIELDS = MACHINE_SETTINGS_FIELDS
FIELD_LABELS = {key: label for _, fields in FIELDS for key, label in fields}
FIELD_LABELS.update(MACHINE_LABELS)


def choose_fonts(root):
    candidates = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                  "Noto Sans CJK SC", "WenQuanYi Micro Hei", "SimHei")
    installed_tk = set(tkfont.families(root))
    family = next((x for x in candidates if x in installed_tk), None)
    if family is None:
        family = tkfont.nametofont("TkDefaultFont").actual("family")
    if family:
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=family, size=12)
    installed_mpl = {font.name for font in font_manager.fontManager.ttflist}
    plot_family = next((x for x in candidates if x in installed_mpl), None)
    if plot_family:
        rcParams["font.sans-serif"] = [plot_family, "DejaVu Sans"]
        rcParams["axes.unicode_minus"] = False
    return family


class App:
    def __init__(self, root):
        self.root = root
        root.title("行星盘沉积仿真 · 探索性模型")
        root.geometry("1480x900")
        root.minsize(1120, 680)
        maximize_on_open(root)
        self.font_family = choose_fonts(root)
        self.font_size_var = tk.StringVar(value="18")
        self.ui_fonts = {
            "body": tkfont.Font(root=root, family=self.font_family, size=12),
            "field": tkfont.Font(root=root, family=self.font_family, size=12),
            "entry": tkfont.Font(root=root, family=self.font_family, size=13),
            "heading": tkfont.Font(root=root, family=self.font_family, size=17, weight="bold"),
            "status": tkfont.Font(root=root, family=self.font_family, size=13, weight="bold"),
        }
        self.style = ttk.Style(root)
        self._apply_font_size()
        self.default = Settings()
        self.process_profile = None
        self._gear_basis = "radius"
        self._gear_syncing = False
        self._time_basis = "steps"
        self.entries = {}
        self.result_queue = queue.Queue()
        self.running = False
        self.job_id = 0
        self.cancel_event = None
        self.edit_revision = 0
        self.started_revision = 0
        self.history = CalculationHistory()
        self.active_work_index = None
        self.active_device_choice = None
        self.active_export_inputs = None
        self.last_export = None
        self._pending_scroll = 0
        self._scroll_idle = None
        self._scrollregion_idle = None

        main = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(main, width=460)
        right = ttk.Frame(main, padding=8)
        main.add(left, weight=0)
        main.add(right, weight=1)
        self._build_inputs(left)
        self._build_results(right)
        self._refresh_estimate()
        self._poll()

    def _build_inputs(self, parent):
        action_bar = ttk.Frame(parent, padding=(12, 8))
        action_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.estimate = ttk.Label(action_bar, text="预计运算时间：暂无同设备历史",
                                  wraplength=410, font=self.ui_fonts["field"])
        self.estimate.pack(anchor="w", pady=(0, 6))
        buttons = ttk.Frame(action_bar)
        buttons.pack(fill=tk.X)
        self.run_button = ttk.Button(buttons, text="重新计算", command=self._run)
        self.run_button.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel,
                                        state="disabled")
        self.cancel_button.pack(side=tk.LEFT, padx=(7, 0))
        ttk.Separator(parent, orient="horizontal").pack(side=tk.BOTTOM, fill=tk.X)

        canvas = tk.Canvas(parent, highlightthickness=0, width=440,
                           background=self.style.lookup("TFrame", "background"),
                           yscrollincrement=self.ui_font_size + 6)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        panel = ttk.Frame(canvas, padding=16)
        window = canvas.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", self._schedule_scrollregion)
        canvas.bind("<Configure>", lambda e: self._resize_scroll_window(window, e.width))
        self.scroll_canvas = canvas
        self.scroll_panel = panel
        self.root.bind_all("<MouseWheel>", self._mouse_wheel, add="+")
        self.root.bind_all("<Button-4>", self._mouse_wheel, add="+")
        self.root.bind_all("<Button-5>", self._mouse_wheel, add="+")

        ttk.Label(panel, text="输入参数", font=self.ui_fonts["heading"]).pack(anchor="w")
        ttk.Label(panel, text="编辑后离开输入框再校验；点击计算才运行模型。",
                  font=self.ui_fonts["body"], wraplength=390).pack(anchor="w", pady=(4, 8))
        font_row = ttk.Frame(panel)
        font_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(font_row, text="界面字号", font=self.ui_fonts["body"]).pack(side=tk.LEFT)
        self.font_size_choice = ttk.Combobox(
            font_row, textvariable=self.font_size_var,
            values=("10", "11", "12", "13", "14", "16", "18"),
            width=5, state="readonly", font=self.ui_fonts["body"])
        self.font_size_choice.pack(side=tk.LEFT, padx=(8, 4))
        self.font_size_choice.bind("<<ComboboxSelected>>", self._change_font_size)
        for event_name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.font_size_choice.bind(event_name, self._mouse_wheel)
        ttk.Label(font_row, text="pt", font=self.ui_fonts["body"]).pack(side=tk.LEFT)
        ttk.Button(panel, text="打开模块启动器", command=self._open_launcher).pack(
            fill=tk.X, pady=(0, 8), ipady=3)

        machine_group = ttk.LabelFrame(panel, text="单源预览快捷几何", padding=10)
        machine_group.pack(fill=tk.X, pady=4)
        self.detail_sections = {}
        ttk.Label(machine_group,
                  text="下方几何值用于单源相对厚度预览。工艺计算使用所选机器文件。盘心距与模数离开输入框后双向换算。",
                  font=self.ui_fonts["body"], wraplength=390).pack(
                      anchor="w", pady=(4, 0))
        self._add_collapsible_fields(
            machine_group, "machine", "机器几何参数",
            [(key, MACHINE_LABELS[key]) for key in MACHINE_EDIT_FIELDS])

        for section, fields in FIELDS:
            group = ttk.LabelFrame(panel, text=section, padding=10)
            group.pack(fill=tk.X, pady=4)
            self._add_entry_fields(group, fields)

        process_group = ttk.LabelFrame(panel, text="工艺配置", padding=10)
        process_group.pack(fill=tk.X, pady=4)
        self.process_path = tk.StringVar()
        self._process_loaded_path = None
        process_path_row = ttk.Frame(process_group)
        process_path_row.pack(fill=tk.X)
        ttk.Entry(process_path_row, textvariable=self.process_path,
                  font=self.ui_fonts["entry"]).pack(side=tk.LEFT, fill=tk.X,
                                                     expand=True, ipady=3)
        ttk.Button(process_path_row, text="选 JSON", command=self._choose_process).pack(
            side=tk.LEFT, padx=(5, 0))
        self.process_summary = ttk.Label(process_group,
                                         text="未选择工艺，使用默认点源进行相对厚度预览",
                                         font=self.ui_fonts["body"], wraplength=390)
        self.process_summary.pack(anchor="w", pady=(6, 0))
        ttk.Label(process_group,
                  text="工艺分别选择机器与基片文件，每步选择源位次、目标厚度与沉积速率。各模块可从启动器单独打开；留空时使用默认点源做相对预览。",
                  font=self.ui_fonts["body"], wraplength=390).pack(
                      anchor="w", pady=(4, 0))
        self.process_path.trace_add("write", self._process_changed)

        coord_group = ttk.LabelFrame(panel, text="坐标约定", padding=10)
        coord_group.pack(fill=tk.X, pady=5)
        ttk.Label(coord_group,
                  text="固定机架 XYZ；默认原点 (0,0,0) 与载架中心重合，源平面为 z=0。机器中每个源的中心都用机架 XY 坐标表示；修正板轮廓使用同一 XY 坐标。",
                  font=self.ui_fonts["body"], wraplength=390, justify=tk.LEFT).pack(anchor="w")

        mode = ttk.Frame(panel)
        mode.pack(fill=tk.X, pady=(9, 4))
        ttk.Label(mode, text="计算设备", font=self.ui_fonts["body"]).pack(side=tk.LEFT)
        self.device = tk.StringVar(value="自动")
        device_choice = ttk.Combobox(
            mode, textvariable=self.device,
            values=("自动", "CPU", "GPU", "DirectML"), width=12, state="readonly",
            font=self.ui_fonts["body"])
        device_choice.pack(side=tk.RIGHT)
        for event_name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            device_choice.bind(event_name, self._mouse_wheel)
        device_choice.bind("<<ComboboxSelected>>", self._device_selected)
        ttk.Label(panel, text="Windows AMD 核显可选 DirectML；自动模式优先 CUDA / ROCm。",
                  font=self.ui_fonts["body"], wraplength=390).pack(anchor="w", pady=(0, 4))
        self.input_message = ttk.Label(panel, text="", font=self.ui_fonts["body"],
                                       foreground="#b3261e", wraplength=390)
        self.input_message.pack(anchor="w", pady=(0, 8))

    def _add_entry_fields(self, group, fields):
        group.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(fields):
            ttk.Label(group, text=label, font=self.ui_fonts["field"],
                      wraplength=210).grid(
                row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(group, width=11, font=self.ui_fonts["entry"])
            if key == "gear_module_mm":
                initial = self.default.effective_gear_module_mm
            elif key == "time_step_s":
                initial = 1.0 / self.default.time_steps
            else:
                initial = getattr(self.default, key)
            entry.insert(0, str(initial))
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5, ipady=3)
            entry.bind("<KeyRelease>", self._mark_dirty)
            if key in ("gear_module_mm", "planet_offset_mm"):
                entry.bind("<KeyRelease>", lambda _e, k=key: self._set_gear_basis(k), add="+")
            if key in ("time_step_s", "time_steps"):
                entry.bind("<KeyRelease>",
                           lambda _e, k=key: self._set_time_basis(k), add="+")
            entry.bind("<FocusOut>", lambda _e, k=key: self._field_focus_out(k))
            entry.bind("<Return>", lambda e: self.root.focus_set())
            self.entries[key] = entry

    def _set_entry(self, key, value):
        entry = self.entries[key]
        was_disabled = str(entry.cget("state")) == "disabled"
        if was_disabled:
            entry.configure(state="normal")
        entry.delete(0, tk.END)
        entry.insert(0, str(value))
        if was_disabled:
            entry.configure(state="disabled")

    def _set_preview_fields_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for key in MACHINE_EDIT_FIELDS:
            self.entries[key].configure(state=state)

    def _set_gear_basis(self, key):
        self._gear_basis = "module" if key == "gear_module_mm" else "radius"

    def _field_focus_out(self, key):
        if key in ("gear_module_mm", "planet_offset_mm", "sun_teeth", "planet_teeth"):
            self._sync_gear_fields(key)
        if key in ("time_step_s", "time_steps"):
            self._set_time_basis(key)
        self._refresh_estimate()

    def _set_time_basis(self, key):
        self._time_basis = "step" if key == "time_step_s" else "steps"

    def _resolve_time_fields(self, process):
        """Keep global sample count and effective mean step length in sync."""
        if self._time_basis == "step":
            try:
                step = float(self.entries["time_step_s"].get().strip())
            except ValueError as exc:
                raise ValueError("时间步长：请输入有效数值") from exc
            if not math.isfinite(step) or step <= 0:
                raise ValueError("时间步长必须为有限正数")
            if process is not None:
                timing = process_timing(process, time_step_s=step)
                count, effective = timing["time_steps"], timing["time_step_s"]
            else:
                ratio = 1.0 / step
                if not math.isfinite(ratio) or ratio > 200000:
                    raise ValueError("单源预览的时间步数不能超过 200000")
                count = max(1, math.ceil(ratio))
                effective = 1.0 / count
            self._set_entry("time_steps", count)
            self._set_entry("time_step_s", f"{effective:.12g}")
            # The displayed value is the effective T/N, which may be rounded.
            # Use the exact integer count as the next conversion basis.
            self._time_basis = "steps"
        else:
            raw = self.entries["time_steps"].get().strip()
            if not raw.isdecimal():
                raise ValueError("时间步数：请输入有效整数")
            count = int(raw)
            if process is not None:
                effective = process_timing(process, time_steps=count)["time_step_s"]
            else:
                if not 1 <= count <= 200000:
                    raise ValueError("单源预览的时间步数须在 1–200000 之间")
                effective = 1.0 / count
            self._set_entry("time_step_s", f"{effective:.12g}")
        return count

    def _sync_gear_fields(self, changed_key):
        if self._gear_syncing:
            return
        try:
            sun = int(self.entries["sun_teeth"].get().strip())
            planet = int(self.entries["planet_teeth"].get().strip())
            basis = ("module" if changed_key == "gear_module_mm" else
                     "radius" if changed_key == "planet_offset_mm" else self._gear_basis)
            if basis == "module":
                module = float(self.entries["gear_module_mm"].get().strip())
                converted = center_distance_from_module(module, sun, planet)
                destination = "planet_offset_mm"
            else:
                radius = float(self.entries["planet_offset_mm"].get().strip())
                converted = module_from_center_distance(radius, sun, planet)
                destination = "gear_module_mm"
        except (ValueError, OverflowError, TypeError):
            return  # The regular field validation shows the incomplete input.
        self._gear_syncing = True
        try:
            self._set_entry(destination, f"{converted:.12g}")
        finally:
            self._gear_syncing = False

    def _add_collapsible_fields(self, group, name, title, fields):
        label = tk.StringVar(value=f"展开{title} ▾")
        ttk.Button(group, textvariable=label,
                   command=lambda: self._toggle_detail(name)).pack(
                       anchor="w", fill=tk.X, pady=(7, 0))
        details = ttk.Frame(group)
        self._add_entry_fields(details, fields)
        self.detail_sections[name] = (details, label, title)

    def _set_detail_expanded(self, name, expanded):
        details, label, title = self.detail_sections[name]
        if expanded:
            if not details.winfo_manager():
                details.pack(fill=tk.X, pady=(4, 0))
            label.set(f"收起{title} ▴")
        else:
            details.pack_forget()
            label.set(f"展开{title} ▾")

    def _toggle_detail(self, name):
        details = self.detail_sections[name][0]
        self._set_detail_expanded(name, not bool(details.winfo_manager()))

    def _change_font_size(self, _=None):
        self._apply_font_size()

    def _apply_font_size(self):
        size = int(self.font_size_var.get())
        self.ui_font_size = size
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=self.font_family, size=size)
        tkfont.nametofont("TkHeadingFont").configure(
            family=self.font_family, size=size + 4, weight="bold")
        self.ui_fonts["body"].configure(size=size)
        self.ui_fonts["field"].configure(size=size)
        self.ui_fonts["entry"].configure(size=size + 1)
        self.ui_fonts["heading"].configure(size=size + 5)
        self.ui_fonts["status"].configure(size=size + 1)
        self.style.configure(".", font=self.ui_fonts["body"])
        self.style.configure("TLabel", font=self.ui_fonts["body"])
        self.style.configure("TButton", font=self.ui_fonts["body"])
        self.style.configure("TEntry", font=self.ui_fonts["entry"])
        self.style.configure("TCombobox", font=self.ui_fonts["body"])
        self.style.configure("TLabelframe.Label", font=self.ui_fonts["body"])
        if hasattr(self, "scroll_canvas"):
            self.scroll_canvas.configure(yscrollincrement=size + 6)
        rcParams.update({
            "font.size": size,
            "axes.titlesize": size + 2,
            "axes.labelsize": size,
            "xtick.labelsize": max(9, size - 1),
            "ytick.labelsize": max(9, size - 1),
        })
        if hasattr(self, "figure"):
            for axis in self.figure.axes:
                axis.title.set_fontsize(size + 2)
                axis.xaxis.label.set_fontsize(size)
                axis.yaxis.label.set_fontsize(size)
                axis.tick_params(labelsize=max(9, size - 1))
            self.plot.draw_idle()

    def _open_launcher(self):
        launcher = Path(__file__).with_name("launcher.py")
        try:
            subprocess.Popen([sys.executable, str(launcher)], cwd=str(launcher.parent))
        except OSError as exc:
            messagebox.showerror("无法打开模块启动器", str(exc), parent=self.root)

    def _show_history(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("计算历史")
        dialog.geometry("1050x550")
        maximize_on_open(dialog)
        dialog.transient(self.root)
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text="成功计算记录（最近 100 条）",
                  font=self.ui_fonts["heading"]).pack(anchor="w", pady=(0, 8))
        self.style.configure("History.Treeview", font=self.ui_fonts["body"],
                             rowheight=max(28, self.ui_font_size + 14))
        self.style.configure("History.Treeview.Heading", font=self.ui_fonts["body"])
        columns = ("when", "requested", "actual", "work", "elapsed")
        rows = ttk.Treeview(container, columns=columns, show="headings",
                            style="History.Treeview")
        for key, title, width in (("when", "完成时间（UTC）", 220),
                                  ("requested", "请求设备", 110),
                                  ("actual", "实际计算设备", 340),
                                  ("work", "运算量", 140),
                                  ("elapsed", "计算用时", 140)):
            rows.heading(key, text=title)
            rows.column(key, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=rows.yview)
        rows.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        rows.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for item in self.history.recent(100):
            rows.insert("", tk.END, values=(
                item["timestamp_utc"][:19].replace("T", " "),
                item["requested_device"], item["actual_device"],
                f"{item['work_index']:,.2f}", format_elapsed(item["elapsed_s"])))

    def _build_results(self, parent):
        status_row = ttk.Frame(parent)
        status_row.pack(fill=tk.X, pady=(1, 6))
        self.status = ttk.Label(status_row, text="等待计算", font=self.ui_fonts["status"])
        self.status.pack(side=tk.LEFT, anchor="w", fill=tk.X, expand=True)
        ttk.Button(status_row, text="计算历史", command=self._show_history).pack(side=tk.RIGHT)
        self.export_button = ttk.Button(status_row, text="保存结果 PDF…",
                                        command=self._export_pdf, state="disabled")
        self.export_button.pack(side=tk.RIGHT, padx=(0, 8))

        self.metrics = ttk.Label(
            parent, text="计算后显示膜厚均匀性、设备和用时",
            font=self.ui_fonts["body"], justify=tk.LEFT)
        self.metrics.pack(fill=tk.X, pady=(0, 4))
        self.backend_notice = ttk.Label(
            parent, text="", font=self.ui_fonts["body"],
            foreground="#8a4b08", justify=tk.LEFT)
        self.backend_notice.pack(fill=tk.X)
        parent.bind(
            "<Configure>",
            lambda event: self.backend_notice.configure(
                wraplength=max(260, event.width - 30)), add="+")

        plot_panel = ttk.Frame(parent)
        plot_panel.pack(fill=tk.BOTH, expand=True)
        self.figure, self.axis = plt.subplots(figsize=(8, 7), dpi=100)
        self.figure.set_layout_engine("constrained")
        self.axis.set_aspect("equal")
        self.axis.set_title("膜厚分布")
        self.axis.set_xlabel("X / mm")
        self.axis.set_ylabel("Y / mm")
        self.plot = FigureCanvasTkAgg(self.figure, master=plot_panel)
        self.plot.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.plot, plot_panel, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(fill=tk.X)

    def _mouse_wheel(self, event):
        widget = event.widget
        while widget is not None:
            if widget is self.scroll_canvas or widget is self.scroll_panel:
                direction = -1 if (getattr(event, "num", None) == 4 or
                                   getattr(event, "delta", 0) > 0) else 1
                ticks = max(1, abs(getattr(event, "delta", 0)) // 120)
                self._pending_scroll += direction * 2 * ticks
                if self._scroll_idle is None:
                    self._scroll_idle = self.root.after_idle(self._flush_mouse_wheel)
                return "break"
            widget = getattr(widget, "master", None)
        return None

    def _flush_mouse_wheel(self):
        self._scroll_idle = None
        movement = self._pending_scroll
        self._pending_scroll = 0
        if movement:
            self.scroll_canvas.yview_scroll(movement, "units")

    def _resize_scroll_window(self, window, width):
        if int(float(self.scroll_canvas.itemcget(window, "width"))) != width:
            self.scroll_canvas.itemconfigure(window, width=width)
            self._schedule_scrollregion()

    def _schedule_scrollregion(self, _=None):
        if self._scrollregion_idle is None:
            self._scrollregion_idle = self.root.after_idle(self._update_scrollregion)

    def _update_scrollregion(self):
        self._scrollregion_idle = None
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _active_process_profile(self):
        path = self.process_path.get().strip()
        if not path:
            return None
        try:
            profile = read_process_profile(path)
            runtime, errors = resolve_process_profile(profile, Path(path).resolve().parent)
            if errors:
                raise ValueError("；".join(errors))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise ValueError(f"工艺方案：{exc}") from exc
        self.process_profile = runtime
        return runtime

    def _choose_process(self):
        path = filedialog.askopenfilename(title="选择工艺配置",
                                          filetypes=[("工艺 JSON", "*.json")])
        if path:
            self._process_loaded_path = None
            self.process_path.set(path)

    def _process_changed(self, *_):
        if hasattr(self, "input_message"):
            path = self.process_path.get().strip()
            if not path:
                self._process_loaded_path = None
                self.process_profile = None
            elif path != self._process_loaded_path:
                try:
                    profile = self._active_process_profile()
                except (ValueError, OSError, KeyError, TypeError):
                    self._process_loaded_path = None
                    self.process_profile = None
                else:
                    self._process_loaded_path = path
                    self.process_profile = profile
            self._mark_dirty()
            self._refresh_estimate()

    def _mark_dirty(self, _=None):
        self.edit_revision += 1
        if not self.running:
            self.status.configure(text="参数已更改，结果待重新计算")
        self.estimate.configure(text="预计运算时间：离开输入框后更新")

    def _device_selected(self, _=None):
        self._mark_dirty()
        self._refresh_estimate()

    def _parse_entry_values(self, names):
        values = {}
        integer_fields = {"sun_teeth", "planet_teeth", "time_steps", "grid_size"}
        for name in names:
            raw = self.entries[name].get().strip()
            try:
                if name in integer_fields:
                    if not raw.isdecimal():
                        raise ValueError()
                    values[name] = int(raw)
                else:
                    values[name] = float(raw)
            except ValueError as exc:
                if name in MACHINE_EDIT_FIELDS:
                    self._set_detail_expanded("machine", True)
                raise ValueError(f"{FIELD_LABELS[name]}：请输入有效数值") from exc
        return values

    def _reveal_validation_error(self, error):
        detail = str(error)
        if ("齿数" in detail or "模数" in detail or "半径" in detail or "盘心距离" in detail or
                "基片高度" in detail or any(name in detail for name in MACHINE_EDIT_FIELDS)):
            self._set_detail_expanded("machine", True)

    def _settings_inputs(self, machine=None, process=None):
        values = dataclasses.asdict(self.default)
        self._resolve_time_fields(process)
        if process is not None:
            values.update({key: machine[key] for key in MACHINE_SETTINGS_FIELDS})
            values["substrate_radius_mm"] = machine["substrate_radius_mm"]
            values["substrate_profile"] = copy.deepcopy(machine["substrate_profile"])
            calculation_keys = [key for _section, fields in FIELDS
                                for key, _label in fields if key != "time_step_s"]
            values.update(self._parse_entry_values(calculation_keys))
        else:
            parsed = self._parse_entry_values(self.entries)
            values.update({key: value for key, value in parsed.items() if key in values})
            values["substrate_profile"] = None
        values["substrate_surface"] = None
        settings = Settings(**values)
        try:
            settings.validate()
        except ValueError as exc:
            self._reveal_validation_error(exc)
            raise
        return settings

    def _inputs(self):
        process = self._active_process_profile()
        machine = process["machine"] if process is not None else None
        settings = self._settings_inputs(machine, process)
        mask = None
        source_profile = None
        return settings, mask, machine, source_profile, process

    def _refresh_estimate(self, _=None):
        self._set_preview_fields_enabled(not bool(self.process_path.get().strip()))
        try:
            settings, mask, machine, source_profile, process = self._inputs()
            if process is None:
                self.process_summary.configure(
                    text="未选择工艺，使用默认点源计算相对厚度")
            else:
                timing = process_timing(process, time_steps=settings.time_steps)
                self.process_summary.configure(
                    text=(f"工艺：{process['profile']['name']}\n"
                          f"{len(process['steps'])} 道工序 · "
                          f"{len(machine['sources'])} 个源 · "
                          f"{len(machine['masks'])} 块修正板 · "
                          f"{timing['total_duration_s']:.3g} s"))
            index = self._work_index(settings, mask, machine, source_profile, process)
            estimate = self.history.estimate(index, self.device.get())
            if estimate is None:
                self.estimate.configure(
                    text=("预计运算时间：暂无该设备历史\n"
                          f"运算量指数：{index:,.2f}"))
            else:
                seconds, actual_device, count = estimate
                self.estimate.configure(
                    text=(f"预计运算时间：约 {format_elapsed(seconds)}\n"
                          f"历史计算设备：{actual_device}\n"
                          f"参考历史数：{count}\n"
                          f"运算量指数：{index:,.2f}"))
            self.input_message.configure(text="")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            if self.process_path.get().strip():
                self.process_summary.configure(text="工艺方案或其依赖文件无效")
            self.estimate.configure(text="预计运算时间：—")
            self.input_message.configure(text=str(exc))

    @staticmethod
    def _work_index(settings, mask, machine, source_profile, process):
        if process is None:
            return workload(settings, mask, source_profile)
        timing = process_timing(process, time_steps=settings.time_steps)
        effective_mask = [item["profile"] for item in machine["masks"]]
        return sum(workload(dataclasses.replace(settings, time_steps=count), effective_mask,
                            machine["sources"][step["source_slot"] - 1]["profile"])
                   for step, count in zip(process["steps"], timing["step_time_steps"]))

    def _run(self):
        try:
            settings, mask, machine, source_profile, process = self._inputs()
            work_index = self._work_index(settings, mask, machine, source_profile, process)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.input_message.configure(text=str(exc))
            return
        if self.running:
            return
        self.input_message.configure(text="")
        self.running = True
        self.job_id += 1
        self.started_revision = self.edit_revision
        job = self.job_id
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.export_button.configure(state="disabled")
        self.status.configure(text=f"正在计算（输入快照 #{job}）…")
        device = self.device.get()
        self.active_device_choice = device
        self.active_work_index = work_index
        self.active_export_inputs = {
            "settings": copy.deepcopy(settings),
            "machine": copy.deepcopy(machine),
            "process": copy.deepcopy(process["profile"] if process else None),
            "requested_device": device,
        }

        def worker():
            started = time.perf_counter()
            try:
                if process is None:
                    result = simulate(settings, mask, device, cancel_event,
                                      source_profile)
                else:
                    result = simulate_process(settings, process,
                                              backend=device, cancel_event=cancel_event)
                self.result_queue.put((job, result, None,
                                       time.perf_counter() - started))
            except Exception as exc:
                self.result_queue.put((job, None, exc,
                                       time.perf_counter() - started))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.status.configure(text="正在取消…")

    def _poll(self):
        try:
            while True:
                job, result, error, elapsed_s = self.result_queue.get_nowait()
                if job != self.job_id:
                    continue
                self.running = False
                self.run_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if error is not None:
                    self.status.configure(text="计算已取消" if isinstance(error, InterruptedError) else "计算失败")
                    if not isinstance(error, InterruptedError):
                        self.input_message.configure(text=str(error))
                    if self.last_export is not None:
                        self.export_button.configure(state="normal")
                else:
                    history_error = None
                    try:
                        self.history.append(self.active_device_choice, result["device"],
                                            self.active_work_index, elapsed_s)
                    except (OSError, ValueError) as exc:
                        history_error = exc
                    self._show(result, elapsed_s)
                    self.last_export = {
                        **self.active_export_inputs,
                        "result": result,
                        "elapsed_s": elapsed_s,
                    }
                    self.export_button.configure(state="normal")
                    self._refresh_estimate()
                    if history_error is not None:
                        self.input_message.configure(text=f"计算历史保存失败：{history_error}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _show(self, result, elapsed_s):
        self.figure.clear()
        self.axis = self.figure.add_subplot(111)
        x = result["x"]
        y = result.get("y", x)
        is_process = "field_nm" in result
        field = result["field_nm"] if is_process else result["field"]
        image = self.axis.imshow(
            np.ma.masked_invalid(field), origin="lower",
            extent=(x[0], x[-1], y[0], y[-1]), cmap="turbo",
            interpolation="bilinear", aspect="equal")
        self.figure.colorbar(
            image, ax=self.axis,
            label="膜厚 / nm" if is_process else "相对膜厚 / 平均值")
        self.axis.set_title("沉积膜厚分布" if is_process else "单源相对膜厚分布")
        self.axis.set_xlabel("X / mm")
        self.axis.set_ylabel("Y / mm")
        self.plot.draw_idle()
        if is_process:
            lines = [
                f"平均膜厚：{result['mean_nm']:.3f} nm",
                f"膜厚峰谷差：{result['pv_nm']:.3f} nm",
                f"CV：{result['cv_percent']:.3f}%",
            ]
        else:
            lines = [
                f"膜厚峰谷差 / 均值：{result['pv_percent']:.3f}%",
                f"CV：{result['cv_percent']:.3f}%",
            ]
        lines.extend((
            f"计算设备：{result['device']}",
            f"计算用时：{format_elapsed(elapsed_s)}"))
        self.metrics.configure(text="\n".join(lines))
        message = f"计算完成（输入快照 #{self.job_id}）"
        if self.edit_revision != self.started_revision:
            message += "\n输入已更改，请重新计算"
        self.status.configure(text=message)
        notices = (result.get("backend_notices", []) if is_process else
                   [result.get("backend_notice", "")])
        self.backend_notice.configure(
            text="\n".join(notice for notice in notices if notice))
        self._refresh_estimate()

    def _export_pdf(self):
        if self.last_export is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="保存仿真结果 PDF",
            defaultextension=".pdf",
            initialfile=f"deposition_result_{time.strftime('%Y%m%d_%H%M%S')}.pdf",
            filetypes=[("PDF 文档", "*.pdf")])
        if not path:
            return
        try:
            from coat_sim.core.result_pdf import export_result_pdf
            export_result_pdf(path, **self.last_export)
        except Exception as exc:
            messagebox.showerror("PDF 保存失败", str(exc), parent=self.root)
            return
        self.status.configure(text=f"结果 PDF 已保存：{Path(path).name}")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
