"""Independent machine editor: geometry and references to peer design files."""

import copy
import math
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from coat_sim.core.gear_geometry import center_distance_from_module, module_from_center_distance
from coat_sim.core.machine_profiles import (MACHINE_FIELDS, MACHINE_LABELS, new_machine_profile,
                              new_monitor, read_machine_profile,
                              resolve_machine_profile, validate_machine_profile,
                              write_machine_profile)
from legacy.window_layout import maximize_on_open


class MachineDesigner:
    def __init__(self, parent=None, settings=None, initial_path=None, on_saved=None,
                 initial_profile=None, embedded=False):
        self.win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.win.title("机器设计器")
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{min(1460, sw - 60)}x{min(900, sh - 80)}")
        self.win.minsize(min(1080, sw - 30), min(650, sh - 40))
        maximize_on_open(self.win)
        self.on_saved = on_saved
        self.current_path = str(Path(initial_path).resolve()) if initial_path else None
        self._loading = False
        self._status_after = None
        self._gear_guard = False
        self.status = tk.StringVar(master=self.win, value="")
        if initial_profile is None and self.current_path:
            try:
                initial_profile = read_machine_profile(self.current_path)
            except (OSError, ValueError) as exc:
                self.current_path = None
                messagebox.showerror("读取机器失败", str(exc), parent=self.win)
        profile = copy.deepcopy(initial_profile or new_machine_profile(settings))
        self.sources = copy.deepcopy(profile["sources"])
        self.masks = copy.deepcopy(profile["masks"])
        self.monitors = copy.deepcopy(profile["monitors"])
        self.geometry = {key: tk.StringVar(master=self.win, value=str(profile[key]))
                         for key in MACHINE_FIELDS}
        self.module = tk.StringVar(master=self.win,
                                   value=str(profile["gear_module_mm"]))
        self.source_file = tk.StringVar(master=self.win, value="")
        self.source_x = tk.StringVar(master=self.win, value="0")
        self.source_y = tk.StringVar(master=self.win, value="0")
        self.mask_file = tk.StringVar(master=self.win, value="")
        self.monitor_name = tk.StringVar(master=self.win, value="")
        self.monitor_kind = tk.StringVar(master=self.win, value="crystal")
        self.monitor_xyz = {
            key: tk.StringVar(master=self.win, value=default)
            for key, default in (("x_mm", "0"), ("y_mm", "0"), ("z_mm", "100"))
        }
        self._view_after = None
        self._wheel_pending = 0
        self._wheel_after = None
        self._build()
        self._refresh_lists()
        self._bind_changes()
        self._schedule_status()

    @property
    def _base_dir(self):
        return Path(self.current_path).parent if self.current_path else Path.cwd()

    def _build(self):
        shell = ttk.Frame(self.win, padding=10)
        shell.pack(fill=tk.BOTH, expand=True)
        bar = ttk.Frame(shell)
        bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(bar, text="机器设计", font="TkHeadingFont").pack(side=tk.LEFT)
        ttk.Button(bar, text="打开…", command=self.open_profile).pack(side=tk.LEFT, padx=(12, 0))
        self.file_label = ttk.Label(bar, text=self.current_path or "未保存机器")
        self.file_label.pack(side=tk.LEFT, padx=(18, 0), fill=tk.X, expand=True)
        body = ttk.Panedwindow(shell, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        controls = ttk.Frame(body)
        views = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(controls, weight=3)
        body.add(views, weight=2)
        self.scroll_canvas = tk.Canvas(controls, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(controls, orient=tk.VERTICAL,
                               command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        content = ttk.Frame(self.scroll_canvas, padding=(4, 4, 8, 4))
        self.scroll_window = self.scroll_canvas.create_window(
            (0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: self.scroll_canvas.configure(
            scrollregion=self.scroll_canvas.bbox("all")))
        self.scroll_canvas.bind("<Configure>", lambda event:
                                self.scroll_canvas.itemconfigure(
                                    self.scroll_window, width=event.width))
        self.scroll_canvas.bind("<Enter>", self._bind_wheel)
        self.scroll_canvas.bind("<Leave>", self._unbind_wheel)
        self.geometry_tab = ttk.LabelFrame(content, text="机器几何", padding=10)
        self.geometry_tab.pack(fill=tk.X, pady=(0, 8))
        equipment = ttk.LabelFrame(content, text="设备配置", padding=8)
        equipment.pack(fill=tk.X)
        self.sources_tab = ttk.LabelFrame(equipment, text="蒸发源安装", padding=8)
        self.sources_tab.pack(fill=tk.X, pady=(0, 8))
        self.masks_tab = ttk.LabelFrame(equipment, text="修正板", padding=8)
        self.masks_tab.pack(fill=tk.X, pady=(0, 8))
        self.monitors_tab = ttk.LabelFrame(equipment, text="监测器", padding=8)
        self.monitors_tab.pack(fill=tk.X)
        self._build_geometry()
        self._build_sources()
        self._build_masks()
        self._build_monitors()
        ttk.Label(views, text="机架坐标示意 · 单位 mm").pack(anchor="w", pady=(0, 4))
        top_frame = ttk.LabelFrame(views, text="俯视图", padding=4)
        top_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.top_view = tk.Canvas(top_frame, background="#ffffff", highlightthickness=0,
                                  width=380, height=250)
        self.top_view.pack(fill=tk.BOTH, expand=True)
        side_frame = ttk.LabelFrame(views, text="侧视图", padding=4)
        side_frame.pack(fill=tk.BOTH, expand=True)
        self.side_view = tk.Canvas(side_frame, background="#ffffff", highlightthickness=0,
                                   width=380, height=250)
        self.side_view.pack(fill=tk.BOTH, expand=True)
        for canvas in (self.top_view, self.side_view):
            canvas.bind("<Configure>", lambda _event: self._schedule_view())
        ttk.Label(views, text="蓝：载架与行星轨道  红：蒸发源  绿：监测器  紫：修正板",
                  wraplength=370).pack(anchor="w", pady=(5, 0))
        status_box = ttk.LabelFrame(shell, text="文件依赖与保存状态", padding=8)
        status_box.pack(fill=tk.X, pady=(8, 0))
        self.status_label = ttk.Label(status_box, textvariable=self.status,
                                      justify=tk.LEFT, wraplength=1000)
        self.status_label.pack(anchor="w", fill=tk.X)
        footer = ttk.Frame(shell)
        footer.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(footer, text="机器引用源和修正板文件；基片由工艺选择。").pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        self.save_as_button = ttk.Button(footer, text="另存为…", command=self.save_as)
        self.save_as_button.pack(side=tk.RIGHT)
        self.save_button = ttk.Button(footer, text="保存", command=self.save)
        self.save_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.win.bind("<FocusIn>", lambda event: self._schedule_status()
                      if event.widget is self.win else None, add="+")

    def _field(self, parent, row, label, variable, *, width=20):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=5)
        return entry

    def _bind_wheel(self, _event=None):
        if not getattr(self, "_wheel_bound", False):
            self.win.bind_all("<MouseWheel>", self._wheel_scroll, add="+")
            self._wheel_bound = True

    def _unbind_wheel(self, _event=None):
        # The handler checks pointer bounds, so entering a child control does
        # not interrupt scrolling the surrounding editor.
        pass

    def _wheel_scroll(self, event):
        if event.widget in (self.source_list, self.mask_list, self.monitor_list):
            return None
        canvas = self.scroll_canvas
        x, y = event.x_root, event.y_root
        if not (canvas.winfo_rootx() <= x < canvas.winfo_rootx() + canvas.winfo_width()
                and canvas.winfo_rooty() <= y < canvas.winfo_rooty() + canvas.winfo_height()):
            return None
        self._wheel_pending += max(-5, min(5, round(-event.delta / 120))) or (
            -1 if event.delta > 0 else 1)
        if self._wheel_after is None:
            self._wheel_after = self.win.after_idle(self._flush_wheel)
        return "break"

    def _flush_wheel(self):
        self._wheel_after = None
        steps, self._wheel_pending = self._wheel_pending, 0
        if steps:
            self.scroll_canvas.yview_scroll(steps, "units")

    def _schedule_view(self):
        if self._view_after is not None:
            self.win.after_cancel(self._view_after)
        self._view_after = self.win.after(70, self._draw_views)

    def _draw_views(self):
        self._view_after = None
        top, side = self.top_view, self.side_view
        top.delete("all")
        side.delete("all")
        try:
            draft = self._draft()
            for widget, form, key in (
                (self.source_list, self._source_form, "sources"),
                (self.mask_list, self._mask_form, "masks"),
                (self.monitor_list, self._monitor_form, "monitors"),
            ):
                index = self._selected(widget)
                if index is not None:
                    draft[key][index] = form()
            profile = validate_machine_profile(draft)
        except (ValueError, TypeError, OverflowError):
            for canvas in (top, side):
                canvas.create_text(canvas.winfo_width()/2, canvas.winfo_height()/2,
                                   text="填写有效机器参数后显示视图", fill="#667085")
            return
        try:
            bundle, _errors = resolve_machine_profile(profile, self._base_dir)
        except (ValueError, TypeError, OverflowError):
            bundle = {"masks": []}
        board_points = [point for entry in bundle["masks"]
                        if entry["profile"] is not None
                        for point in entry["profile"]["outer"]]
        cx, cy = profile["carrier_x_mm"], profile["carrier_y_mm"]
        orbit = profile["planet_offset_mm"]
        sources, monitors = profile["sources"], profile["monitors"]
        tw, th = max(top.winfo_width(), 200), max(top.winfo_height(), 180)
        extent = max(100.0, abs(cx)+orbit, abs(cy)+orbit,
                     *(abs(row[axis]) for row in (*sources, *monitors)
                       for axis in ("x_mm", "y_mm")),
                     *(abs(axis) for point in board_points for axis in point))
        scale = 0.42 * min(tw, th) / extent
        tx = lambda x: tw/2 + x*scale
        ty = lambda y: th/2 - y*scale
        top.create_line(18, th/2, tw-18, th/2, fill="#cbd5e1", dash=(3, 3))
        top.create_line(tw/2, 18, tw/2, th-18, fill="#cbd5e1", dash=(3, 3))
        top.create_text(tw-14, th/2-12, text="X", fill="#64748b")
        top.create_text(tw/2+12, 14, text="Y", fill="#64748b")
        top.create_oval(tx(cx-orbit), ty(cy+orbit), tx(cx+orbit), ty(cy-orbit),
                        outline="#2563eb", width=2, dash=(5, 3))
        top.create_oval(tx(cx)-4, ty(cy)-4, tx(cx)+4, ty(cy)+4,
                        outline="#2563eb", fill="#ffffff", width=2)
        top.create_text(tx(cx)+8, ty(cy)-10, text="载架中心", anchor="sw", fill="#1d4ed8")
        for index, entry in enumerate(bundle["masks"], 1):
            board = entry["profile"]
            if board is None:
                continue
            points = [coordinate for x, y in board["outer"]
                      for coordinate in (tx(x), ty(y))]
            top.create_polygon(*points, fill="", outline="#7c3aed", width=2)
            for hole in board["holes"]:
                hole_points = [coordinate for x, y in hole
                               for coordinate in (tx(x), ty(y))]
                top.create_polygon(*hole_points, fill="", outline="#a78bfa",
                                   width=1, dash=(3, 2))
            if points:
                top.create_text(points[0]+6, points[1]-5, text=f"板{index}",
                                anchor="sw", fill="#6d28d9")
        for index, row in enumerate(sources, 1):
            x, y = tx(row["x_mm"]), ty(row["y_mm"])
            top.create_oval(x-5, y-5, x+5, y+5, fill="#dc2626", outline="")
            top.create_text(x+7, y-7-(index%2)*9, text=f"源{index}", anchor="sw",
                            fill="#991b1b")
        for index, row in enumerate(monitors, 1):
            x, y = tx(row["x_mm"]), ty(row["y_mm"])
            top.create_rectangle(x-4, y-4, x+4, y+4, fill="#16a34a", outline="")
            top.create_text(x+7, y+5+(index%2)*9, text=f"监{index}", anchor="nw",
                            fill="#166534")
        sw, sh = max(side.winfo_width(), 200), max(side.winfo_height(), 180)
        z_values = [0.0, profile["substrate_z_mm"]]
        z_values.extend(row["z_mm"] for row in monitors)
        z_min, z_max = min(z_values), max(z_values)
        if z_max == z_min:
            z_max += 1.0
        z_pad = max((z_max-z_min)*0.1, 10.0)
        z_min -= z_pad
        z_max += z_pad
        x_extent = max(100.0, abs(cx)+orbit,
                       *(abs(row["x_mm"]) for row in (*sources, *monitors)),
                       *(abs(point[0]) for point in board_points))
        sx = lambda x: sw/2 + x * (0.42*sw/x_extent)
        sz = lambda z: sh-22 - (z-z_min)*(sh-44)/(z_max-z_min)
        side.create_line(sw/2, 14, sw/2, sh-16, fill="#cbd5e1", dash=(3, 3))
        side.create_line(20, sz(0), sw-20, sz(0), fill="#94a3b8")
        side.create_text(23, sz(0)-5, text="源平面 z=0", anchor="sw", fill="#475569")
        substrate_y = sz(profile["substrate_z_mm"])
        side.create_line(20, substrate_y, sw-20, substrate_y,
                         fill="#2563eb", dash=(5, 3), width=2)
        side.create_text(23, substrate_y-5, text="基片安装高度", anchor="sw",
                         fill="#1d4ed8")
        side.create_oval(sx(cx)-4, substrate_y-4, sx(cx)+4, substrate_y+4,
                         outline="#2563eb", width=2)
        for index, row in enumerate(sources, 1):
            x, y = sx(row["x_mm"]), sz(0)
            side.create_polygon(x-5, y+3, x+5, y+3, x, y-7,
                                fill="#dc2626", outline="")
            side.create_text(x+7, y+5+(index%2)*9, text=f"源{index}",
                             anchor="nw", fill="#991b1b")
        for index, row in enumerate(monitors, 1):
            x, y = sx(row["x_mm"]), sz(row["z_mm"])
            side.create_rectangle(x-4, y-4, x+4, y+4, fill="#16a34a", outline="")
            side.create_text(x+7, y+5+(index%2)*9, text=f"监{index}",
                             anchor="nw", fill="#166534")
        for index, entry in enumerate(bundle["masks"], 1):
            board = entry["profile"]
            if board is None:
                continue
            xs = [point[0] for point in board["outer"]]
            x1, x2 = sx(min(xs)), sx(max(xs))
            y = sz(board["z_mm"])
            side.create_line(x1, y, x2, y, fill="#7c3aed", width=4)
            side.create_text(x2+5, y-(index%2)*10, text=f"板{index}",
                             anchor="w", fill="#6d28d9")

    def _build_geometry(self):
        tab = self.geometry_tab
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="固定机架 XY，源平面 z=0；工艺文件选择基片。"
                  "轨道半径与齿轮模数会相互换算。",
                  wraplength=800).grid(row=0, column=0, columnspan=3,
                                        sticky="w", pady=(0, 12))
        row = 1
        for key in MACHINE_FIELDS:
            self._field(tab, row, MACHINE_LABELS[key], self.geometry[key])
            row += 1
        self._field(tab, row, MACHINE_LABELS["gear_module_mm"], self.module)
        row += 1
        ttk.Label(tab, text="路径相对于机器文件。保存机器时检查每个源和每块修正板。"
                  "源安装坐标由机器保存，源文件本身没有 X/Y。",
                  wraplength=800).grid(row=row, column=0, columnspan=3,
                                        sticky="w", pady=(16, 0))

    def _build_sources(self):
        tab = self.sources_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="顺序就是工艺中的源位次（从 1 开始）；重排会提醒检查工艺。",
                  wraplength=700).grid(row=0, column=0, columnspan=2, sticky="w",
                                        pady=(0, 10))
        list_frame = ttk.Frame(tab)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.source_list = tk.Listbox(list_frame, exportselection=False, height=7)
        self.source_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        source_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                      command=self.source_list.yview)
        source_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.source_list.configure(yscrollcommand=source_scroll.set)
        self.source_list.bind("<<ListboxSelect>>", self._source_selected)
        form = ttk.Frame(tab)
        form.grid(row=1, column=1, sticky="n")
        form.columnconfigure(1, weight=1)
        self._field(form, 0, "源文件", self.source_file, width=38)
        ttk.Button(form, text="选择…", command=lambda: self._browse_dependency(
            self.source_file, "蒸发源 JSON")).grid(row=0, column=2, padx=6)
        self._field(form, 1, "中心 X / mm", self.source_x)
        self._field(form, 2, "中心 Y / mm", self.source_y)
        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        for text, action in (("添加", self.add_source), ("更新选中", self.update_source),
                             ("删除", self.remove_source),
                             ("上移", lambda: self.move_source(-1)),
                             ("下移", lambda: self.move_source(1))):
            ttk.Button(buttons, text=text, command=action).pack(side=tk.LEFT, padx=(0, 5))

    def _build_masks(self):
        tab = self.masks_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="最多两块板；机器只引用独立修正板 JSON。"
                  "板的高度与轮廓由修正板设计器维护。",
                  wraplength=700).grid(row=0, column=0, columnspan=2, sticky="w",
                                        pady=(0, 10))
        list_frame = ttk.Frame(tab)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.mask_list = tk.Listbox(list_frame, exportselection=False, height=5)
        self.mask_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        mask_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                    command=self.mask_list.yview)
        mask_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.mask_list.configure(yscrollcommand=mask_scroll.set)
        self.mask_list.bind("<<ListboxSelect>>", self._mask_selected)
        form = ttk.Frame(tab)
        form.grid(row=1, column=1, sticky="n")
        form.columnconfigure(1, weight=1)
        self._field(form, 0, "板文件", self.mask_file, width=38)
        ttk.Button(form, text="选择…", command=lambda: self._browse_dependency(
            self.mask_file, "修正板 JSON")).grid(row=0, column=2, padx=6)
        buttons = ttk.Frame(form)
        buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        for text, action in (("添加", self.add_mask), ("更新选中", self.update_mask),
                             ("删除", self.remove_mask)):
            ttk.Button(buttons, text=text, command=action).pack(side=tk.LEFT, padx=(0, 5))

    def _build_monitors(self):
        tab = self.monitors_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="监测器固定在机架 XYZ，可填写任意有限坐标；工艺按位次选择。"
                  "位于 z≤0 的监测器对向 +Z 发射的蒸发源接收通量为零。"
                  "可添加、修改、删除或调整顺序。",
                  wraplength=700).grid(row=0, column=0, columnspan=2, sticky="w",
                                        pady=(0, 10))
        list_frame = ttk.Frame(tab)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.monitor_list = tk.Listbox(list_frame, exportselection=False, height=7)
        self.monitor_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        monitor_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                       command=self.monitor_list.yview)
        monitor_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.monitor_list.configure(yscrollcommand=monitor_scroll.set)
        self.monitor_list.bind("<<ListboxSelect>>", self._monitor_selected)
        form = ttk.Frame(tab)
        form.grid(row=1, column=1, sticky="n")
        form.columnconfigure(1, weight=1)
        self._field(form, 0, "名称", self.monitor_name)
        ttk.Label(form, text="类型").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=self.monitor_kind, state="readonly",
                     values=("crystal", "optical"), width=18).grid(
                         row=1, column=1, sticky="ew", padx=(10, 0), pady=5)
        for row, key in enumerate(("x_mm", "y_mm", "z_mm"), 2):
            self._field(form, row, key.replace("_mm", "").upper() + " / mm",
                        self.monitor_xyz[key])
        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        for text, action in (("添加", self.add_monitor), ("更新选中", self.update_monitor),
                             ("删除", self.remove_monitor),
                             ("上移", lambda: self.move_monitor(-1)),
                             ("下移", lambda: self.move_monitor(1))):
            ttk.Button(buttons, text=text, command=action).pack(side=tk.LEFT, padx=(0, 5))

    def _bind_changes(self):
        for key, variable in self.geometry.items():
            if key in ("planet_offset_mm", "sun_teeth", "planet_teeth"):
                variable.trace_add("write", lambda *_args, changed=key:
                                   self._gear_changed(changed))
            else:
                variable.trace_add("write", self._schedule_status)
        self.module.trace_add("write", lambda *_args: self._gear_changed("gear_module_mm"))
        for variable in (self.source_file, self.source_x,
                         self.source_y, self.mask_file, self.monitor_name,
                         self.monitor_kind, *self.monitor_xyz.values()):
            variable.trace_add("write", self._schedule_status)

    def _gear_changed(self, changed):
        if self._gear_guard:
            return
        try:
            sun = int(self.geometry["sun_teeth"].get())
            planet = int(self.geometry["planet_teeth"].get())
            if sun <= 0 or planet <= 0:
                return
            if changed == "gear_module_mm":
                module = float(self.module.get())
                updated = self.geometry["planet_offset_mm"]
                value = center_distance_from_module(module, sun, planet)
            else:
                distance = float(self.geometry["planet_offset_mm"].get())
                updated = self.module
                value = module_from_center_distance(distance, sun, planet)
            self._gear_guard = True
            updated.set(f"{value:.12g}")
        except (ValueError, ZeroDivisionError):
            pass
        finally:
            self._gear_guard = False
            self._schedule_status()

    def _browse_dependency(self, variable, label):
        path = filedialog.askopenfilename(
            parent=self.win, title=f"选择{label}", initialdir=str(self._base_dir),
            filetypes=[(label, "*.json"), ("所有文件", "*.*")])
        if path:
            try:
                variable.set(os.path.relpath(Path(path).resolve(), self._base_dir))
            except ValueError:
                messagebox.showerror("路径无效", "依赖文件须位于可用相对路径表示的位置。",
                                     parent=self.win)

    def _selected(self, listbox):
        selection = listbox.curselection()
        return selection[0] if selection else None

    def _source_selected(self, _event=None):
        index = self._selected(self.source_list)
        if index is None or index >= len(self.sources):
            return
        self._loading = True
        entry = self.sources[index]
        self.source_file.set(entry["file"])
        self.source_x.set(str(entry["x_mm"]))
        self.source_y.set(str(entry["y_mm"]))
        self._loading = False
        self._schedule_status()

    def _mask_selected(self, _event=None):
        index = self._selected(self.mask_list)
        if index is None or index >= len(self.masks):
            return
        self._loading = True
        self.mask_file.set(self.masks[index]["file"])
        self._loading = False
        self._schedule_status()

    def _monitor_selected(self, _event=None):
        index = self._selected(self.monitor_list)
        if index is None or index >= len(self.monitors):
            return
        self._loading = True
        entry = self.monitors[index]
        self.monitor_name.set(entry["name"])
        self.monitor_kind.set(entry["kind"])
        for key in self.monitor_xyz:
            self.monitor_xyz[key].set(str(entry[key]))
        self._loading = False
        self._schedule_status()

    def _source_form(self):
        return {"file": self.source_file.get().strip(),
                "x_mm": float(self.source_x.get().strip()),
                "y_mm": float(self.source_y.get().strip())}

    def _mask_form(self):
        return {"file": self.mask_file.get().strip()}

    def _monitor_form(self):
        return new_monitor(self.monitor_kind.get(), self.monitor_name.get(),
                           **{key: float(variable.get().strip())
                              for key, variable in self.monitor_xyz.items()})

    def _refresh_lists(self, source=None, mask=None, monitor=None):
        if source is None:
            source = self._selected(self.source_list)
        if mask is None:
            mask = self._selected(self.mask_list)
        if monitor is None:
            monitor = self._selected(self.monitor_list)
        for widget, rows, formatter in (
            (self.source_list, self.sources,
             lambda i, row: f"{i+1}. {row['file']} · ({row['x_mm']:g}, {row['y_mm']:g}) mm"),
            (self.mask_list, self.masks,
             lambda i, row: f"{i+1}. {row['file']}"),
            (self.monitor_list, self.monitors,
             lambda i, row: f"{i+1}. {row['name']} · {row['kind']} · "
                            f"({row['x_mm']:g},{row['y_mm']:g},{row['z_mm']:g}) mm"),
        ):
            widget.delete(0, tk.END)
            for index, row in enumerate(rows):
                widget.insert(tk.END, formatter(index, row))
        for widget, index, rows in ((self.source_list, source, self.sources),
                                    (self.mask_list, mask, self.masks),
                                    (self.monitor_list, monitor, self.monitors)):
            if index is not None and 0 <= index < len(rows):
                widget.selection_set(index)
                widget.see(index)
        self._schedule_status()

    def _replace_selected(self, kind, *, silent=False):
        widget = {
            "source": self.source_list,
            "mask": self.mask_list,
            "monitor": self.monitor_list,
        }[kind]
        if self._selected(widget) is None:
            if not silent:
                messagebox.showwarning("请选择条目", "先在列表中选择要更新的条目。", parent=self.win)
            return False
        try:
            self._commit_forms()
        except (ValueError, TypeError, OverflowError) as exc:
            if not silent:
                messagebox.showerror("条目无效", str(exc), parent=self.win)
            return False
        return True

    def add_source(self):
        try:
            item = self._source_form()
            validate_machine_profile(self._draft(sources=[*self.sources, item]))
        except (ValueError, TypeError, OverflowError) as exc:
            messagebox.showerror("无法添加源", str(exc), parent=self.win)
            return
        self.sources.append(item)
        self._refresh_lists(source=len(self.sources)-1)

    def update_source(self):
        self._replace_selected("source")

    def remove_source(self):
        index = self._selected(self.source_list)
        if index is None:
            return
        if len(self.sources) == 1:
            messagebox.showwarning("无法删除", "机器至少保留一个蒸发源。", parent=self.win)
            return
        if messagebox.askyesno("删除蒸发源", "删除会改变后续源位次；请检查引用此机器的工艺。",
                               parent=self.win):
            del self.sources[index]
            self._refresh_lists(source=min(index, len(self.sources)-1))

    def move_source(self, delta):
        index = self._selected(self.source_list)
        if index is None or not 0 <= index + delta < len(self.sources):
            return
        if not messagebox.askyesno("调整源位次", "源位次将改变；请检查引用此机器的工艺步骤。",
                                   parent=self.win):
            return
        self.sources[index], self.sources[index+delta] = (
            self.sources[index+delta], self.sources[index])
        self._refresh_lists(source=index+delta)

    def add_mask(self):
        try:
            item = self._mask_form()
            validate_machine_profile(self._draft(masks=[*self.masks, item]))
        except (ValueError, TypeError) as exc:
            messagebox.showerror("无法添加板", str(exc), parent=self.win)
            return
        self.masks.append(item)
        self._refresh_lists(mask=len(self.masks)-1)

    def update_mask(self):
        self._replace_selected("mask")

    def remove_mask(self):
        index = self._selected(self.mask_list)
        if index is not None:
            del self.masks[index]
            self._refresh_lists(mask=min(index, len(self.masks)-1))

    def add_monitor(self):
        try:
            item = self._monitor_form()
            validate_machine_profile(self._draft(monitors=[*self.monitors, item]))
        except (ValueError, TypeError, OverflowError) as exc:
            messagebox.showerror("无法添加监测器", str(exc), parent=self.win)
            return
        self.monitors.append(item)
        self._refresh_lists(monitor=len(self.monitors)-1)

    def update_monitor(self):
        self._replace_selected("monitor")

    def remove_monitor(self):
        index = self._selected(self.monitor_list)
        if index is None:
            return
        if messagebox.askyesno("删除监测器", "删除会改变后续监测器位次；请检查工艺步骤。",
                               parent=self.win):
            del self.monitors[index]
            self._refresh_lists(monitor=min(index, len(self.monitors)-1))

    def move_monitor(self, delta):
        index = self._selected(self.monitor_list)
        if index is None or not 0 <= index + delta < len(self.monitors):
            return
        if not messagebox.askyesno("调整监测器位次", "监测器位次将改变；请检查工艺步骤。",
                                   parent=self.win):
            return
        self.monitors[index], self.monitors[index+delta] = (
            self.monitors[index+delta], self.monitors[index])
        self._refresh_lists(monitor=index+delta)

    def _draft(self, *, sources=None, masks=None, monitors=None):
        values = {}
        for key in MACHINE_FIELDS:
            raw = self.geometry[key].get().strip()
            number = float(raw)
            values[key] = int(number) if key in ("sun_teeth", "planet_teeth") and number.is_integer() else number
        return {"schema_version": 5, **values,
                "gear_module_mm": float(self.module.get().strip()),
                "sources": copy.deepcopy(self.sources if sources is None else sources),
                "masks": copy.deepcopy(self.masks if masks is None else masks),
                "monitors": copy.deepcopy(self.monitors if monitors is None else monitors)}

    def _schedule_status(self, *_args):
        if self._loading:
            return
        if self._status_after is not None:
            self.win.after_cancel(self._status_after)
        self._status_after = self.win.after(100, self._refresh_status)
        self._schedule_view()

    def _refresh_status(self):
        self._status_after = None
        try:
            draft = self._draft()
            for widget, form, key in (
                (self.source_list, self._source_form, "sources"),
                (self.mask_list, self._mask_form, "masks"),
                (self.monitor_list, self._monitor_form, "monitors"),
            ):
                index = self._selected(widget)
                if index is not None:
                    draft[key][index] = form()
            profile = validate_machine_profile(draft)
            _, errors = resolve_machine_profile(profile, self._base_dir)
        except (ValueError, TypeError, OverflowError) as exc:
            errors = [str(exc)]
        for widget, category, rows in (
            (self.source_list, "sources", self.sources),
            (self.mask_list, "masks", self.masks),
        ):
            for index in range(len(rows)):
                failed = any(error.startswith(f"{category}[{index+1}].file:")
                             for error in errors)
                widget.itemconfigure(index, foreground="#b91c1c" if failed else "#1f2937")
        if errors:
            self.status.set("无法保存：\n" + "\n".join("• " + error for error in errors[:6])
                            + (f"\n另有 {len(errors)-6} 项错误" if len(errors) > 6 else ""))
            self.save_button.state(["disabled"])
            self.save_as_button.state(["disabled"])
        else:
            self.status.set(str(len(self.sources)) + " 个源和 "
                            + str(len(self.masks)) + " 块修正板文件均有效，可保存。")
            self.save_button.state(["!disabled"])
            self.save_as_button.state(["!disabled"])

    def _commit_forms(self):
        draft = self._draft()
        selections = (self._selected(self.source_list),
                      self._selected(self.mask_list),
                      self._selected(self.monitor_list))
        for widget, form, key in (
            (self.source_list, self._source_form, "sources"),
            (self.mask_list, self._mask_form, "masks"),
            (self.monitor_list, self._monitor_form, "monitors"),
        ):
            index = self._selected(widget)
            if index is not None:
                draft[key][index] = form()
        normalized = validate_machine_profile(draft)
        self.sources = normalized["sources"]
        self.masks = normalized["masks"]
        self.monitors = normalized["monitors"]
        self._refresh_lists(*selections)

    def open_profile(self):
        path = filedialog.askopenfilename(parent=self.win, title="打开机器 JSON",
            initialdir=str(self._base_dir), filetypes=[("机器 JSON", "*.json")])
        if not path:
            return
        try:
            profile = read_machine_profile(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.win)
            return
        self.current_path = str(Path(path).resolve())
        self._load_profile(profile)

    def _load_profile(self, profile):
        self._loading = True
        for key in MACHINE_FIELDS:
            self.geometry[key].set(str(profile[key]))
        self.module.set(str(profile["gear_module_mm"]))
        self.sources = copy.deepcopy(profile["sources"])
        self.masks = copy.deepcopy(profile["masks"])
        self.monitors = copy.deepcopy(profile["monitors"])
        self.source_file.set("")
        self.mask_file.set("")
        self.monitor_name.set("")
        self.file_label.configure(text=self.current_path or "未保存机器")
        self._loading = False
        self._refresh_lists()

    def _rebase_files(self, profile, target):
        old_base, new_base = self._base_dir, Path(target).resolve().parent
        rebased = copy.deepcopy(profile)
        def relative(path):
            absolute = (old_base / path).resolve()
            return os.path.relpath(absolute, new_base)
        for item in (*rebased["sources"], *rebased["masks"]):
            item["file"] = relative(item["file"])
        return rebased

    def save(self):
        self._save(as_new=False)

    def save_as(self):
        self._save(as_new=True)

    def _save(self, *, as_new):
        try:
            self._commit_forms()
            profile = validate_machine_profile(self._draft())
            _, errors = resolve_machine_profile(profile, self._base_dir)
            if errors:
                raise ValueError("\n".join(errors))
        except (ValueError, TypeError, OverflowError) as exc:
            messagebox.showerror("无法保存机器", str(exc), parent=self.win)
            self._refresh_status()
            return
        path = self.current_path
        if as_new or path is None:
            selected = filedialog.asksaveasfilename(
                parent=self.win, title="另存机器" if as_new else "保存机器",
                initialdir=str(self._base_dir),
                initialfile=Path(path).name if path else "machine.json",
                defaultextension=".json", filetypes=[("机器 JSON", "*.json")])
            if not selected:
                return
            selected = str(Path(selected).resolve())
            if as_new and path and selected == str(Path(path).resolve()):
                messagebox.showerror("另存为失败", "请选择不同于当前文件的新路径。",
                                     parent=self.win)
                return
            path = selected
            try:
                profile = self._rebase_files(profile, path)
            except ValueError:
                messagebox.showerror(
                    "另存为失败", "机器及其依赖文件必须位于同一磁盘卷，无法创建相对引用。",
                    parent=self.win)
                return
        try:
            saved = write_machine_profile(path, profile)
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.win)
            return
        self.current_path = str(Path(path).resolve())
        self._load_profile(saved)
        if self.on_saved is not None:
            self.on_saved(self.current_path, saved)

    def _close(self):
        if self._status_after is not None:
            self.win.after_cancel(self._status_after)
        if self._view_after is not None:
            self.win.after_cancel(self._view_after)
        if self._wheel_after is not None:
            self.win.after_cancel(self._wheel_after)
        if getattr(self, "_wheel_bound", False):
            self.win.unbind_all("<MouseWheel>")
        self.win.destroy()


if __name__ == "__main__":
    MachineDesigner().win.mainloop()
