"""Interactive editor for fixed-frame correction-plate masks."""

import colorsys
import copy
from dataclasses import replace
import json
import math
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from coat_sim.core.mask_analysis import (correction_board_blocking_weights,
                           radial_blocking_weights)
from coat_sim.core.machine_profiles import (MACHINE_FIELDS, read_machine_profile,
                              resolve_machine_profile, validate_mask_profile)
from coat_sim.core.simulator import Settings
from coat_sim.core.substrate_profiles import (effective_radius_mm, read_substrate_profile,
                                validate_substrate_profile)
from coat_sim.core.substrate_surface import surface_height_and_normal
from legacy.window_layout import maximize_on_open


def smooth_closed(points, samples_per_segment=5):
    """Return a sampled, closed Catmull-Rom contour through control points."""
    if len(points) < 3:
        raise ValueError("曲线至少需要 3 个控制点")
    result = []
    n = len(points)
    for i in range(n):
        p0, p1 = points[(i - 1) % n], points[i]
        p2, p3 = points[(i + 1) % n], points[(i + 2) % n]
        for j in range(samples_per_segment):
            t = j / samples_per_segment
            t2, t3 = t * t, t * t * t
            xy = []
            for k in (0, 1):
                xy.append(0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t +
                                 (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2 +
                                 (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3))
            result.append(xy)
    # Keep the persisted contour within the simulator's 500-vertex limit.
    if len(result) > 500:
        stride = math.ceil(len(result) / 500)
        result = result[::stride]
    return result


class MaskDesigner:
    def __init__(self, parent, settings=None, initial_path=None, on_saved=None,
                 source_profile=None, initial_mask=None, sources=None,
                 substrate_profile=None):
        self.parent = parent
        settings = settings or Settings(duration_s=60.0)
        if substrate_profile is None:
            substrate_profile = settings.substrate_profile
        if substrate_profile is None:
            substrate_profile = {"schema_version": 1, "name": "预览基片",
                                 "outline": {"kind": "circle",
                                             "radius_mm": settings.substrate_radius_mm},
                                 "surface": {"kind": "flat"}}
        self.substrate_profile = validate_substrate_profile(substrate_profile)
        self.substrate_surface = self.substrate_profile["surface"]
        self.settings = replace(
            settings, substrate_profile=self.substrate_profile,
            substrate_radius_mm=effective_radius_mm(self.substrate_profile))
        self.source_profile = source_profile
        self.display_sources = self._prepare_sources(sources)
        chosen_index = next((i for i, source in enumerate(self.display_sources)
                             if source["profile"] == source_profile), 0)
        self.analysis_source = tk.StringVar(
            value=f"{chosen_index + 1}. {self.display_sources[chosen_index]['name']}")
        self.initial_path = initial_path
        self.on_saved = on_saved
        self.loaded_machine_path = None
        self.dependency_error = None
        self.win = tk.Toplevel(parent)
        self.win.title("修正板设计 · 俯视固定机架 XY")
        screen_width = self.win.winfo_screenwidth()
        screen_height = self.win.winfo_screenheight()
        self.win.geometry(f"{min(1550, max(1, screen_width - 40))}x"
                          f"{min(960, max(1, screen_height - 70))}")
        self.win.minsize(min(1000, max(1, screen_width - 40)),
                         min(650, max(1, screen_height - 70)))
        maximize_on_open(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        self.outer = []
        self.holes = []
        self.current = []
        self.view_center = (self.settings.carrier_x_mm, self.settings.carrier_y_mm)
        self.view_radius = max(300.0, self.settings.planet_offset_mm +
                               self.settings.substrate_radius_mm + 100)
        self.z = tk.StringVar(value=str(self._minimum_substrate_z() * 0.5))
        self.mode = tk.StringVar(value="直线")
        self.kind = tk.StringVar(value="外轮廓")
        self.action = tk.StringVar(value="绘制轮廓")
        self.patch_radius = tk.StringVar(value="12")
        self.translate_x = tk.StringVar(value="0")
        self.translate_y = tk.StringVar(value="0")
        self.scale_x = tk.StringVar(value="1")
        self.scale_y = tk.StringVar(value="1")
        self.show_trajectory = tk.BooleanVar(value=True)
        self.selected_point = None
        self.patch_radius_value = 12.0
        self.trajectory = None
        self.trajectories = []
        self._analysis_after = None
        self._redraw_after = None
        self._analysis_extent_after = None
        self._analysis_wheel_after = None
        self._analysis_wheel_delta = 0
        self._analysis_width_after = None
        self._analysis_width = None
        self._board_result = None
        self._point_result = None
        self._board_generation = 0
        self._board_busy = False
        self._board_queue = queue.Queue()
        self._board_poll_after = None
        self._last_coordinate_update = 0.0
        self.status = tk.StringVar(value="单击画点；至少 3 点后按“闭合当前轮廓”。")
        self.dependency_info = tk.StringVar(value="机器预览：未载入；当前按默认几何预览。")
        self.point_info = tk.StringVar(value="选择“选点分析”，再单击画布选取板面位置。")
        self.trajectory_info = tk.StringVar(value="正在计算所有源的完整一周板面轨迹…")
        self.sampling_info = tk.StringVar(value="")
        self.board_info = tk.StringVar(value="闭合外轮廓后，点击“计算当前板遮挡权重”。")

        controls = ttk.Frame(self.win, padding=(12, 10, 12, 5))
        controls.pack(fill=tk.X)
        first_row = ttk.Frame(controls)
        first_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(first_row, text="画布操作").pack(side=tk.LEFT)
        action_box = ttk.Combobox(first_row, textvariable=self.action,
                                  values=("绘制轮廓", "选点分析"), width=11, state="readonly")
        action_box.pack(side=tk.LEFT, padx=(6, 18))
        action_box.bind("<<ComboboxSelected>>", self._action_changed)
        ttk.Label(first_row, text="绘制方式").pack(side=tk.LEFT)
        mode_box = ttk.Combobox(first_row, textvariable=self.mode, values=("直线", "平滑曲线"),
                                width=12, state="readonly")
        mode_box.pack(side=tk.LEFT, padx=(6, 18))
        ttk.Label(first_row, text="轮廓").pack(side=tk.LEFT)
        kind_box = ttk.Combobox(first_row, textvariable=self.kind, values=("外轮廓", "孔洞"),
                                width=10, state="readonly")
        kind_box.pack(side=tk.LEFT, padx=(6, 0))

        second_row = ttk.Frame(controls)
        second_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(second_row, text="板高 z (mm)").pack(side=tk.LEFT)
        ttk.Entry(second_row, textvariable=self.z, width=9).pack(side=tk.LEFT, padx=(6, 18))
        ttk.Label(second_row, text="局部分析半径 (mm)").pack(side=tk.LEFT)
        ttk.Entry(second_row, textvariable=self.patch_radius, width=8).pack(side=tk.LEFT,
                                                                             padx=(6, 18))
        ttk.Checkbutton(second_row, text="显示所有源完整轨迹圆", variable=self.show_trajectory,
                        command=self.redraw).pack(side=tk.LEFT)

        third_row = ttk.Frame(controls)
        third_row.pack(fill=tk.X)
        ttk.Button(third_row, text="撤销一点", command=self.undo_point).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(third_row, text="闭合当前轮廓", command=self.close_contour).pack(side=tk.LEFT,
                                                                            padx=(0, 8))
        ttk.Button(third_row, text="清空轮廓", command=self.clear_all).pack(side=tk.LEFT)
        ttk.Label(third_row, text="俯视机架 XY · 滚轮缩放 · 单位 mm").pack(
            side=tk.RIGHT, padx=(0, 12))

        transform_row = ttk.Frame(controls)
        transform_row.pack(fill=tk.X, pady=(6, 0))
        for label, variable in (("平移 X / mm", self.translate_x),
                                ("平移 Y / mm", self.translate_y),
                                ("X 缩放", self.scale_x), ("Y 缩放", self.scale_y)):
            ttk.Label(transform_row, text=label).pack(side=tk.LEFT, padx=(8, 3))
            ttk.Entry(transform_row, textvariable=variable, width=7).pack(side=tk.LEFT)
        ttk.Button(transform_row, text="变换轮廓", command=self.transform_geometry).pack(
            side=tk.LEFT, padx=(10, 0))

        content = ttk.Frame(self.win)
        content.pack(fill=tk.BOTH, expand=True, padx=(12, 12), pady=(3, 6))
        analysis_shell = ttk.Frame(content, width=540)
        analysis_shell.pack(side=tk.RIGHT, fill=tk.Y)
        analysis_shell.pack_propagate(False)
        analysis_view = tk.Canvas(analysis_shell, highlightthickness=0,
                                  borderwidth=0)
        analysis_scroll = ttk.Scrollbar(analysis_shell, orient=tk.VERTICAL,
                                         command=analysis_view.yview)
        analysis_view.configure(yscrollcommand=analysis_scroll.set)
        analysis_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        analysis_view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        analysis_panel = ttk.Frame(analysis_view, padding=(12, 4, 8, 4))
        panel_window = analysis_view.create_window((0, 0), window=analysis_panel,
                                                    anchor="nw")
        analysis_panel.bind("<Configure>", self._schedule_analysis_extent)
        analysis_view.bind("<Configure>", lambda event: self._schedule_analysis_width(
            panel_window, event.width))
        self.analysis_view = analysis_view
        ttk.Label(analysis_panel, text="修正板遮挡权重",
                  font=tkfont.nametofont("TkHeadingFont")).pack(anchor="w", pady=(0, 6))
        ttk.Label(analysis_panel, textvariable=self.dependency_info,
                  wraplength=500, justify=tk.LEFT,
                  foreground="#9a3412").pack(anchor="w", pady=(0, 6))
        ttk.Label(analysis_panel, text=("当前板：完整实体轮廓及孔洞的实际拦截比例。"
                                        "选点局部：假设在选点周围放置小圆时的潜在拦截比例。"
                                        "两者都以所选单源无板通量为分母，按完整基片材料半径统计。"
                                        "此处为当前运动时长的预览估算。"),
                  wraplength=500, justify=tk.LEFT).pack(anchor="w", pady=(0, 8))
        source_row = ttk.Frame(analysis_panel)
        source_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(source_row, text="权重计算源").pack(side=tk.LEFT, padx=(0, 8))
        self.analysis_source_box = ttk.Combobox(
            source_row, textvariable=self.analysis_source, state="readonly",
            values=self._source_choices(), width=34)
        self.analysis_source_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.analysis_source_box.bind("<<ComboboxSelected>>", self._source_changed)
        ttk.Label(analysis_panel, textvariable=self.point_info,
                  wraplength=500, justify=tk.LEFT).pack(anchor="w", pady=(0, 6))
        ttk.Label(analysis_panel, textvariable=self.trajectory_info,
                  wraplength=500, justify=tk.LEFT).pack(anchor="w", pady=(0, 6))
        source_legend_frame = ttk.Frame(analysis_panel)
        source_legend_frame.pack(fill=tk.X, pady=(0, 4))
        tree_style = ttk.Style(self.win)
        rowheight = tkfont.nametofont("TkDefaultFont").metrics("linespace") + 12
        tree_style.configure("MaskSources.Treeview",
                             font=tkfont.nametofont("TkDefaultFont"),
                             rowheight=rowheight)
        tree_style.configure("MaskSources.Treeview.Heading",
                             font=tkfont.nametofont("TkDefaultFont"))
        self.source_legend = ttk.Treeview(source_legend_frame,
                                          columns=("source", "center"),
                                          show="headings", height=5,
                                          selectmode="none", style="MaskSources.Treeview")
        self.source_legend.heading("source", text="轨迹颜色 / 蒸发源")
        self.source_legend.heading("center", text="源中心 X/Y mm")
        self.source_legend.column("source", width=255, minwidth=255)
        self.source_legend.column("center", width=190, minwidth=190, anchor="e")
        self.source_legend.grid(row=0, column=0, sticky="nsew")
        source_legend_scroll = ttk.Scrollbar(
            source_legend_frame, orient=tk.VERTICAL, command=self.source_legend.yview)
        source_legend_scroll.grid(row=0, column=1, sticky="ns")
        source_legend_xscroll = ttk.Scrollbar(
            source_legend_frame, orient=tk.HORIZONTAL, command=self.source_legend.xview)
        source_legend_xscroll.grid(row=1, column=0, sticky="ew")
        source_legend_frame.columnconfigure(0, weight=1)
        self.source_legend.configure(yscrollcommand=source_legend_scroll.set,
                                     xscrollcommand=source_legend_xscroll.set)
        self._refresh_source_legend()
        ttk.Separator(analysis_panel).pack(fill=tk.X, pady=(0, 6))
        ttk.Label(analysis_panel, textvariable=self.sampling_info,
                  wraplength=500, justify=tk.LEFT).pack(anchor="w", pady=(0, 4))
        board_row = ttk.Frame(analysis_panel)
        board_row.pack(fill=tk.X, pady=(0, 4))
        self.board_button = ttk.Button(board_row, text="计算当前板遮挡权重",
                                       command=self.calculate_board_weight)
        self.board_button.pack(side=tk.LEFT)
        ttk.Label(board_row, textvariable=self.board_info, wraplength=310,
                  justify=tk.LEFT).pack(side=tk.LEFT, padx=(8, 0), fill=tk.X, expand=True)
        tree_frame = ttk.Frame(analysis_panel)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        tree_style.configure("MaskWeights.Treeview",
                             font=tkfont.nametofont("TkDefaultFont"),
                             rowheight=rowheight)
        tree_style.configure("MaskWeights.Treeview.Heading",
                             font=tkfont.nametofont("TkDefaultFont"))
        self.weights_tree = ttk.Treeview(tree_frame,
                                         columns=("radius", "patch", "board"),
                                         show="headings", height=7,
                                         style="MaskWeights.Treeview")
        self.weights_tree.heading("radius", text="基片半径 / mm")
        self.weights_tree.heading("patch", text="选点局部 / %")
        self.weights_tree.heading("board", text="当前板 / %")
        self.weights_tree.column("radius", anchor="e", minwidth=145, width=150)
        self.weights_tree.column("patch", anchor="e", minwidth=135, width=145)
        self.weights_tree.column("board", anchor="e", minwidth=135, width=145)
        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                                    command=self.weights_tree.yview)
        self.weights_tree.configure(yscrollcommand=tree_scroll.set)
        self.weights_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._bind_analysis_wheel(analysis_panel)

        self.canvas = tk.Canvas(content, background="#f7f9fc", highlightthickness=1,
                                highlightbackground="#aab4c0", cursor="crosshair")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._canvas_click)
        self.canvas.bind("<ButtonPress-3>", self._start_pan)
        self.canvas.bind("<B3-Motion>", self._pan_view)

        footer = ttk.Frame(self.win, padding=(12, 0, 12, 12))
        footer.pack(fill=tk.X)
        actions = ttk.Frame(footer)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="关闭", command=self._close).pack(side=tk.RIGHT)
        self.save_as_button = ttk.Button(actions, text="另存为新文件…", command=self.save_as)
        self.save_as_button.pack(
            side=tk.RIGHT, padx=(0, 8))
        self.save_button = ttk.Button(actions, text="保存到当前文件", command=self.save)
        self.save_button.pack(
            side=tk.RIGHT, padx=(0, 8))
        ttk.Button(actions, text="新建板", command=self.new_machine_board).pack(
            side=tk.RIGHT, padx=(0, 8))
        ttk.Button(actions, text="载入基片预览…", command=self.load_substrate).pack(
            side=tk.LEFT, padx=(7, 0))
        ttk.Button(actions, text="载入机器预览…", command=self.load_machine).pack(
            side=tk.RIGHT, padx=(0, 8))
        ttk.Button(actions, text="打开板文件…", command=self.open_mask).pack(
            side=tk.RIGHT, padx=(0, 8))
        ttk.Label(footer, textvariable=self.status, wraplength=1400).pack(
            anchor="w", fill=tk.X, pady=(5, 0))
        self.canvas.bind("<MouseWheel>", self.zoom)
        self.canvas.bind("<Button-4>", self.zoom)
        self.canvas.bind("<Button-5>", self.zoom)
        self.canvas.bind("<Motion>", self.show_coordinates)
        self.z.trace_add("write", self._board_geometry_changed)
        self.patch_radius.trace_add("write", self._schedule_analysis_refresh)

        if initial_mask is not None:
            self._set_mask(initial_mask)
        elif initial_path:
            self.load(initial_path)
        self._fit_sources_in_view()
        self._refresh_analysis()

    def _prepare_sources(self, sources):
        """Keep source names and fixed machine-frame centers for the drawing."""
        if sources:
            raw = [(Path(item.get("file", "source.json")).stem,
                    item["profile"], item["x_mm"], item["y_mm"])
                   for item in sources]
        else:
            raw = [("当前蒸发源", self.source_profile or {"kind": "point"},
                    self.settings.source_x_mm, self.settings.source_y_mm)]
        prepared = []
        for index, (name, profile, x, y) in enumerate(raw):
            x, y = float(x), float(y)
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("蒸发源中心须为有限的机架 XY 坐标")
            hue = (0.03 + index * 0.6180339887498949) % 1.0
            rgb = colorsys.hsv_to_rgb(hue, 0.78, 0.72)
            color = "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)
            prepared.append({"name": str(name), "x_mm": x, "y_mm": y,
                             "color": color, "profile": copy.deepcopy(profile)})
        return prepared

    def _source_choices(self):
        return tuple(f"{i}. {source['name']}"
                     for i, source in enumerate(self.display_sources, 1))

    def _refresh_source_legend(self):
        for item in self.source_legend.get_children():
            self.source_legend.delete(item)
        for index, source in enumerate(self.display_sources, 1):
            tag = f"source_{index}"
            self.source_legend.tag_configure(tag, foreground=source["color"])
            self.source_legend.insert("", tk.END, values=(
                f"{index}. {source['name']}",
                f"({source['x_mm']:g}, {source['y_mm']:g})"), tags=(tag,))

    def _bind_analysis_wheel(self, parent):
        """Scroll the whole information panel when the pointer is over text."""
        if not isinstance(parent, ttk.Treeview):
            parent.bind("<MouseWheel>", self._scroll_analysis, add="+")
            parent.bind("<Button-4>", self._scroll_analysis, add="+")
            parent.bind("<Button-5>", self._scroll_analysis, add="+")
        for child in parent.winfo_children():
            self._bind_analysis_wheel(child)

    def _scroll_analysis(self, event):
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -int(event.delta / 120) if event.delta else 0
        if delta:
            self._analysis_wheel_delta += delta
            if self._analysis_wheel_after is None:
                self._analysis_wheel_after = self.win.after(45, self._flush_analysis_scroll)
        return "break"

    def _minimum_substrate_z(self):
        import numpy as np
        radii = np.linspace(0.0, self.settings.substrate_radius_mm, 129)
        sag = surface_height_and_normal(
            radii, np.zeros_like(radii), self.settings.substrate_radius_mm,
            self.substrate_surface)[0]
        return self.settings.substrate_z_mm + min(0.0, float(np.min(sag)))

    def _schedule_analysis_width(self, panel_window, width):
        self._analysis_width = (panel_window, width)
        if self._analysis_width_after is None:
            self._analysis_width_after = self.win.after(50, self._flush_analysis_width)

    def _flush_analysis_width(self):
        self._analysis_width_after = None
        if self.analysis_view.winfo_exists() and self._analysis_width is not None:
            panel_window, width = self._analysis_width
            self.analysis_view.itemconfigure(panel_window, width=width)

    def _flush_analysis_scroll(self):
        self._analysis_wheel_after = None
        amount, self._analysis_wheel_delta = self._analysis_wheel_delta, 0
        if amount and self.analysis_view.winfo_exists():
            self.analysis_view.yview_scroll(amount, "units")

    def _schedule_analysis_extent(self, _event=None):
        if self._analysis_extent_after is None:
            self._analysis_extent_after = self.win.after(45, self._update_analysis_extent)

    def _update_analysis_extent(self):
        self._analysis_extent_after = None
        if self.analysis_view.winfo_exists():
            self.analysis_view.configure(scrollregion=self.analysis_view.bbox("all"))

    def _source_changed(self, _event=None):
        self._invalidate_board_weight("已切换计算源，请重新计算当前板权重。")
        self._refresh_analysis()

    def _analysis_context(self):
        choice = self.analysis_source.get()
        choices = self._source_choices()
        index = choices.index(choice) if choice in choices else 0
        source = self.display_sources[index]
        profile = source["profile"]
        settings = replace(
            self.settings,
            source_x_mm=source["x_mm"],
            source_y_mm=source["y_mm"],
            source_exponent=float(profile.get("source_exponent",
                                              self.settings.source_exponent)))
        return settings, copy.deepcopy(profile)

    def _fit_sources_in_view(self):
        """Show all source markers and full trajectory circles on first open."""
        cx, cy = self.settings.carrier_x_mm, self.settings.carrier_y_mm
        reach = self.settings.planet_offset_mm + self.settings.substrate_radius_mm
        points = [(0.0, 0.0), (cx - reach, cy - reach),
                  (cx + reach, cy + reach)]
        points.extend((source["x_mm"], source["y_mm"])
                      for source in self.display_sources)
        points.extend((float(x), float(y)) for x, y in
                      self.outer + self.current +
                      [point for hole in self.holes for point in hole])
        xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
        ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
        self.view_center = ((xmin + xmax) / 2, (ymin + ymax) / 2)
        self.view_radius = max(100.0, max(xmax - xmin, ymax - ymin) * 0.62)

    def to_canvas(self, x, y):
        w, h = max(self.canvas.winfo_width(), 2), max(self.canvas.winfo_height(), 2)
        scale = min(w, h) * 0.46 / self.view_radius
        cx, cy = w / 2, h / 2
        return (cx + (x - self.view_center[0]) * scale,
                cy - (y - self.view_center[1]) * scale)

    def to_world(self, px, py):
        w, h = max(self.canvas.winfo_width(), 2), max(self.canvas.winfo_height(), 2)
        scale = min(w, h) * 0.46 / self.view_radius
        cx, cy = w / 2, h / 2
        return [self.view_center[0] + (px - cx) / scale,
                self.view_center[1] - (py - cy) / scale]

    def redraw(self):
        if not hasattr(self, "canvas"):
            return
        if self._redraw_after is None:
            self._redraw_after = self.win.after(40, self._render_canvas)

    def _render_canvas(self):
        self._redraw_after = None
        if not self.canvas.winfo_exists() or not self.canvas.winfo_ismapped():
            return
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 2), max(c.winfo_height(), 2)
        scale = min(w, h) * 0.46 / self.view_radius
        cx, cy = w / 2, h / 2
        # A readable adaptive grid in machine coordinates.
        raw = self.view_radius / 5
        step = min((1, 2, 5, 10)[-1], 10)
        magnitude = 10 ** math.floor(math.log10(max(raw, 1)))
        step = min((1, 2, 5, 10), key=lambda q: abs(q * magnitude - raw)) * magnitude
        xmin = self.view_center[0] - w / (2 * scale)
        xmax = self.view_center[0] + w / (2 * scale)
        ymin = self.view_center[1] - h / (2 * scale)
        ymax = self.view_center[1] + h / (2 * scale)
        for gx in range(math.floor(xmin / step), math.ceil(xmax / step) + 1):
            x = gx * step
            px, _ = self.to_canvas(x, self.view_center[1])
            c.create_line(px, 0, px, h, fill="#e3e8ef", width=1)
            if gx % max(1, round((5 * step) / step)) == 0:
                c.create_text(px + 3, cy + 10, text=f"{x:g}", anchor="nw", fill="#64748b")
        for gy in range(math.floor(ymin / step), math.ceil(ymax / step) + 1):
            y = gy * step
            _, py = self.to_canvas(self.view_center[0], y)
            c.create_line(0, py, w, py, fill="#e3e8ef", width=1)
            if gy % 5 == 0:
                c.create_text(cx + 5, py + 3, text=f"{y:g}", anchor="nw", fill="#64748b")
        ox, oy = self.to_canvas(0, 0)
        c.create_line(0, oy, w, oy, fill="#718096", width=2)
        c.create_line(ox, 0, ox, h, fill="#718096", width=2)
        c.create_text(w - 12, oy - 8, text="X / mm", anchor="e", fill="#334155")
        c.create_text(ox + 8, 12, text="Y / mm", anchor="nw", fill="#334155")
        self._draw_contour(self.outer, "#d97706", "#fef3c7", "外轮廓")
        for i, hole in enumerate(self.holes, 1):
            self._draw_contour(hole, "#2563eb", "#dbeafe", f"孔洞 {i}")
        if self.current:
            points = self._current_preview()
            self._draw_contour(points, "#c026d3", "#fae8ff", "绘制中", close=False)
            px, py = self.to_canvas(*self.current[0])
            c.create_oval(px - 5, py - 5, px + 5, py + 5, outline="#86198f", width=2)
        self._draw_trajectory()
        self._draw_analysis_point()
        c.create_text(12, 12,
                      text=f"原点 (0,0) | 视图中心 ({self.view_center[0]:.0f},{self.view_center[1]:.0f}) mm | ±{self.view_radius:.0f} mm",
                      anchor="nw", fill="#334155")

    def _current_preview(self):
        if self.mode.get() == "平滑曲线" and len(self.current) >= 3:
            # Preview interpolates the current control points without forcing closure.
            from legacy.mask_designer import smooth_closed
            return smooth_closed(self.current)
        return self.current

    def _draw_contour(self, points, color, fill, label, close=True):
        if not points:
            return
        coords = [self.to_canvas(x, y) for x, y in points]
        if len(coords) >= 2:
            flat = [v for xy in coords for v in xy]
            if close and len(coords) >= 3:
                flat += list(coords[0])
            self.canvas.create_line(*flat, fill=color, width=2.4, smooth=False)
            if close and len(coords) >= 3:
                self.canvas.create_polygon(*[v for xy in coords for v in xy],
                                           fill=fill, stipple="gray25", outline="")
                self.canvas.create_line(*flat, fill=color, width=2.4)
        for i, (px, py) in enumerate(coords):
            self.canvas.create_oval(px - 3, py - 3, px + 3, py + 3,
                                    fill=color, outline="white")
            if not close:
                self.canvas.create_text(px + 5, py - 5, text=str(i + 1), anchor="sw",
                                        fill="#475569")
        if len(coords) >= 2:
            self.canvas.create_text(coords[0][0] + 6, coords[0][1] - 8,
                                    text=label, anchor="sw", fill=color)

    def _all_source_trajectories(self, z_mm):
        """Return every source's full-circle locus in fixed machine XY.

        The locus describes one entire carrier revolution regardless of the
        current preview duration or carrier speed. A zero-radius orbit is a
        point. Actual deposition samples remain governed by the process time.
        """
        fraction = z_mm / self.settings.substrate_z_mm
        radius = fraction * self.settings.planet_offset_mm
        result = []
        for source in self.display_sources:
            x, y = source["x_mm"], source["y_mm"]
            result.append({**source,
                           "center_xy": ((1 - fraction) * x +
                                         fraction * self.settings.carrier_x_mm,
                                         (1 - fraction) * y +
                                         fraction * self.settings.carrier_y_mm),
                           "radius_mm": radius})
        return result

    def _draw_trajectory(self):
        if not self.show_trajectory.get() or not self.trajectories:
            return
        c = self.canvas
        count = len(self.trajectories)
        # Draw all circles first so every source marker remains visible on top.
        for index, trajectory in enumerate(self.trajectories, 1):
            cx, cy = self.to_canvas(*trajectory["center_xy"])
            edge_x, _ = self.to_canvas(
                trajectory["center_xy"][0] + trajectory["radius_mm"],
                trajectory["center_xy"][1])
            radius_px = abs(edge_x - cx)
            if radius_px >= 0.5:
                c.create_oval(cx - radius_px, cy - radius_px,
                              cx + radius_px, cy + radius_px,
                              outline=trajectory["color"], width=2.5,
                              dash=(9, 4) if index % 2 else (4, 4))
            else:
                c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                              fill=trajectory["color"], outline="white")
        for index, source in enumerate(self.trajectories, 1):
            px, py = self.to_canvas(source["x_mm"], source["y_mm"])
            c.create_oval(px - 6, py - 6, px + 6, py + 6,
                          fill=source["color"], outline="white", width=2)
            label = f"{index}. {source['name']}" if count <= 8 else str(index)
            c.create_text(px + 9, py + 7, text=label, anchor="nw",
                          fill=source["color"])

    def _draw_analysis_point(self):
        if self.selected_point is None:
            return
        x, y = self.selected_point
        px, py = self.to_canvas(x, y)
        c = self.canvas
        if self.patch_radius_value is not None:
            edge_x, _ = self.to_canvas(x + self.patch_radius_value, y)
            radius_px = abs(edge_x - px)
            c.create_oval(px - radius_px, py - radius_px,
                          px + radius_px, py + radius_px,
                          outline="#6d28d9", width=2, dash=(5, 3))
        c.create_line(px - 10, py, px + 10, py, fill="#6d28d9", width=3)
        c.create_line(px, py - 10, px, py + 10, fill="#6d28d9", width=3)
        c.create_oval(px - 4, py - 4, px + 4, py + 4,
                      fill="#6d28d9", outline="white")
        c.create_text(px + 12, py - 12, text="分析点", anchor="sw", fill="#6d28d9")

    def _action_changed(self, _event=None):
        if self.action.get() == "选点分析":
            self.status.set("分析模式：单击画布选点，靠近轮廓顶点时会吸附。")
        else:
            self.status.set("绘制模式：单击画布添加轮廓点。")

    def _canvas_click(self, event):
        if self.action.get() == "绘制轮廓":
            self.add_point(event)
            return
        selected = self.to_world(event.x, event.y)
        closest = None
        distance_squared = 12 ** 2
        for point in self.outer + self.current + [p for hole in self.holes for p in hole]:
            px, py = self.to_canvas(*point)
            candidate_distance = (px - event.x) ** 2 + (py - event.y) ** 2
            if candidate_distance <= distance_squared:
                closest = point
                distance_squared = candidate_distance
        if closest is not None:
            selected = closest
        self.selected_point = (float(selected[0]), float(selected[1]))
        self._refresh_analysis()
        x, y = self.selected_point
        suffix = "（已吸附轮廓顶点）" if closest is not None else ""
        self.status.set(f"已选分析点：X={x:.1f} mm，Y={y:.1f} mm{suffix}")

    def _schedule_analysis_refresh(self, *_args):
        if self._analysis_after is not None:
            self.win.after_cancel(self._analysis_after)
        self._analysis_after = self.win.after(300, self._run_analysis_refresh)

    def _board_geometry_changed(self, *_args):
        self._invalidate_board_weight("板高或轮廓已改变，请重新计算当前板权重。")
        self._schedule_analysis_refresh()

    def _invalidate_board_weight(self, message=None):
        self._board_generation += 1
        self._board_result = None
        if message is not None:
            self.board_info.set(message)
        if hasattr(self, "weights_tree"):
            self._render_weights_table()

    def _render_weights_table(self):
        for item in self.weights_tree.get_children():
            self.weights_tree.delete(item)
        result = self._point_result or self._board_result
        if result is None:
            return
        radii = result["radii_mm"]
        patch_weights = (self._point_result["weights_percent"]
                         if self._point_result is not None else None)
        board_weights = (self._board_result["weights_percent"]
                         if self._board_result is not None else None)
        for index, radius in enumerate(radii):
            patch_text = (f"{float(patch_weights[index]):.2f}"
                          if patch_weights is not None and
                          math.isfinite(float(patch_weights[index])) else "—")
            board_text = (f"{float(board_weights[index]):.2f}"
                          if board_weights is not None and
                          math.isfinite(float(board_weights[index])) else "—")
            self.weights_tree.insert("", tk.END, values=(
                f"{float(radius):.1f}", patch_text, board_text))

    def calculate_board_weight(self):
        if self._board_busy:
            return
        try:
            z = float(self.z.get())
            mask = validate_mask_profile({"z_mm": z,
                                          "outer": copy.deepcopy(self.outer),
                                          "holes": copy.deepcopy(self.holes)},
                                         substrate_z_mm=self._minimum_substrate_z())
            settings, profile = self._analysis_context()
            settings.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("当前板无法计算", str(exc), parent=self.win)
            return
        self._board_generation += 1
        generation = self._board_generation
        self._board_busy = True
        self.board_button.configure(state=tk.DISABLED)
        self.board_info.set("正在按当前单源及已闭合板轮廓计算…")
        threading.Thread(target=self._board_weight_worker,
                         args=(generation, settings, mask, profile,
                               copy.deepcopy(self.substrate_surface)),
                         daemon=True).start()
        if self._board_poll_after is None:
            self._board_poll_after = self.win.after(100, self._poll_board_weight)

    def _board_weight_worker(self, generation, settings, mask, profile, surface):
        try:
            result = correction_board_blocking_weights(
                settings, mask, source_profile=profile,
                substrate_surface=surface)
            self._board_queue.put((generation, result, None))
        except Exception as exc:
            self._board_queue.put((generation, None, str(exc)))

    def _poll_board_weight(self):
        self._board_poll_after = None
        while True:
            try:
                generation, result, error = self._board_queue.get_nowait()
            except queue.Empty:
                break
            self._board_busy = False
            self.board_button.configure(state=tk.NORMAL)
            if generation != self._board_generation:
                continue
            if error is not None:
                self.board_info.set(f"计算失败：{error}")
            else:
                self._board_result = result
                self.board_info.set(
                    f"单板权重已计算：{result['duration_s']:g} s / "
                    f"{result['carrier_rpm']:g} rpm；时间 {result['time_samples']}、"
                    f"方位 {result['azimuth_samples']}、源元 {result['source_samples']} 个采样。")
                self._render_weights_table()
        if self._board_busy and self.win.winfo_exists():
            self._board_poll_after = self.win.after(100, self._poll_board_weight)

    def _run_analysis_refresh(self):
        self._analysis_after = None
        self._refresh_analysis()

    def _refresh_analysis(self):
        if self._analysis_after is not None:
            self.win.after_cancel(self._analysis_after)
            self._analysis_after = None
        self._point_result = None
        if self.selected_point is None:
            self.point_info.set("选择“选点分析”，再单击画布选取板面位置。")
        else:
            x, y = self.selected_point
            self.point_info.set(f"选点：X={x:.1f} mm，Y={y:.1f} mm")

        try:
            z = float(self.z.get())
        except ValueError:
            z = None
        minimum_z = self._minimum_substrate_z()
        if z is None or not math.isfinite(z) or not 0 < z < minimum_z:
            self.trajectory = None
            self.trajectories = []
            self.patch_radius_value = None
            self.trajectory_info.set(
                f"板高无效：请输入 0 与基片最低点 {minimum_z:g} mm 之间的数值。")
            self.sampling_info.set("输入有效板高后显示遮挡权重。")
            self._render_weights_table()
            self.redraw()
            return
        try:
            self.trajectories = self._all_source_trajectories(z)
            self.trajectory = self.trajectories[0] if self.trajectories else None
            radius = self.trajectories[0]["radius_mm"]
            locus = (f"共同半径 {radius:.1f} mm。" if radius > 0 else
                     "盘心轨道半径为零，轨迹退化为点。")
            self.trajectory_info.set(
                f"{len(self.trajectories)} 个源的几何中心和完整一周切割轨迹圆；"
                f"板高 z={z:g} mm，{locus}颜色与下表对应。"
                "轨迹仅表示几何范围，实际时段可能不足一周；"
                "遮挡权重只分析上方选择的一个源。")
        except Exception as exc:
            self.trajectory = None
            self.trajectories = []
            self.trajectory_info.set(f"轨迹计算失败：{exc}")

        try:
            radius = float(self.patch_radius.get())
        except ValueError:
            radius = None
        radius_limit = 2 * max(10000.0, self.settings.substrate_radius_mm,
                               self.settings.planet_offset_mm)
        if (radius is None or not math.isfinite(radius) or
                not 0 < radius <= radius_limit):
            self.patch_radius_value = None
            self.sampling_info.set(
                f"局部半径无效：请输入大于 0 且不超过 {radius_limit:g} mm 的数值。")
            self._render_weights_table()
            self.redraw()
            return
        self.patch_radius_value = radius
        if self.selected_point is None:
            self.sampling_info.set(f"局部半径 {radius:g} mm；选择板面点后显示各半径权重。")
            self._render_weights_table()
            self.redraw()
            return
        try:
            settings, profile = self._analysis_context()
            result = radial_blocking_weights(
                settings, z, self.selected_point, radius,
                source_profile=profile,
                substrate_surface=self.substrate_surface)
            self._point_result = result
            time_samples = result.get("time_samples", result.get("time_steps_used", "—"))
            azimuth_samples = result.get("azimuth_samples", "—")
            self.sampling_info.set(
                f"局部半径 {radius:g} mm；时间采样 {time_samples}，"
                f"每环方位采样 {azimuth_samples}。")
        except Exception as exc:
            self.sampling_info.set(f"遮挡权重计算失败：{exc}")
        self._render_weights_table()
        self.redraw()

    def add_point(self, event):
        self.current.append(self.to_world(event.x, event.y))
        self.status.set(f"当前轮廓 {len(self.current)} 个点；闭合时会自动连接末点与起点。")
        self.redraw()

    def close_contour(self):
        if len(self.current) < 3:
            messagebox.showinfo("点数不足", "闭合轮廓至少需要 3 个点。", parent=self.win)
            return
        points = self.current
        if self.mode.get() == "平滑曲线":
            points = smooth_closed(points)
        if len(points) > 500:
            messagebox.showerror("轮廓过复杂", "单个轮廓最多保存 500 个点。", parent=self.win)
            return
        if self.kind.get() == "外轮廓":
            self.outer = points
        else:
            self.holes.append(points)
        self.current = []
        self._invalidate_board_weight("轮廓已改变，请重新计算当前板权重。")
        self.status.set("轮廓已闭合。可继续添加孔洞，或保存修正板 JSON。")
        self.redraw()

    def undo_point(self):
        if self.current:
            self.current.pop()
            self.redraw()

    def clear_all(self):
        self.outer, self.holes, self.current = [], [], []
        self._invalidate_board_weight("轮廓已清空。")
        self.status.set("画布已清空。")
        self.redraw()

    def transform_geometry(self):
        """Scale about the contour bounds' center, then translate in machine XY."""
        points = self.outer + self.current + [p for hole in self.holes for p in hole]
        if not points:
            messagebox.showinfo("没有轮廓", "请先绘制修正板轮廓。", parent=self.win)
            return
        try:
            dx = float(self.translate_x.get())
            dy = float(self.translate_y.get())
            sx = float(self.scale_x.get())
            sy = float(self.scale_y.get())
            if not all(map(math.isfinite, (dx, dy, sx, sy))):
                raise ValueError("平移与缩放值必须为有限数值")
            if not 0.01 <= sx <= 100 or not 0.01 <= sy <= 100:
                raise ValueError("X/Y 缩放比例须在 0.01–100 之间")
            if abs(dx) > 100000 or abs(dy) > 100000:
                raise ValueError("单次平移须在 ±100000 mm 以内")
        except ValueError as exc:
            messagebox.showerror("变换参数无效", str(exc), parent=self.win)
            return
        pivot_x = (min(p[0] for p in points) + max(p[0] for p in points)) / 2
        pivot_y = (min(p[1] for p in points) + max(p[1] for p in points)) / 2
        def change(point):
            return [pivot_x + (point[0] - pivot_x) * sx + dx,
                    pivot_y + (point[1] - pivot_y) * sy + dy]
        self.outer = [change(point) for point in self.outer]
        self.holes = [[change(point) for point in hole] for hole in self.holes]
        self.current = [change(point) for point in self.current]
        if self.selected_point is not None:
            self.selected_point = tuple(change(self.selected_point))
        self._invalidate_board_weight("轮廓已变换，请重新计算当前板权重。")
        self.status.set(f"轮廓已平移 ({dx:g}, {dy:g}) mm，X/Y 缩放 {sx:g}/{sy:g} 倍。")
        self.translate_x.set("0")
        self.translate_y.set("0")
        self.scale_x.set("1")
        self.scale_y.set("1")
        self._refresh_analysis()

    def _start_pan(self, event):
        self._pan_origin = (event.x, event.y, self.view_center)

    def _close(self):
        for task in (self._analysis_after, self._redraw_after,
                     self._analysis_extent_after, self._analysis_wheel_after,
                     self._analysis_width_after, self._board_poll_after):
            if task is not None:
                try:
                    self.win.after_cancel(task)
                except tk.TclError:
                    pass
        self.win.destroy()

    def _pan_view(self, event):
        if not hasattr(self, "_pan_origin"):
            return
        x0, y0, center = self._pan_origin
        scale = min(max(self.canvas.winfo_width(), 2),
                    max(self.canvas.winfo_height(), 2)) * 0.46 / self.view_radius
        self.view_center = (center[0] - (event.x - x0) / scale,
                            center[1] + (event.y - y0) / scale)
        self.redraw()

    def zoom(self, event):
        direction = 0.85 if (event.num == 4 or getattr(event, "delta", 0) > 0) else 1.18
        self.view_radius = min(20000, max(20, self.view_radius * direction))
        self.redraw()
        return "break"

    def show_coordinates(self, event):
        now = time.monotonic()
        if now - self._last_coordinate_update < 0.05:
            return
        self._last_coordinate_update = now
        x, y = self.to_world(event.x, event.y)
        action = "单击选取分析点" if self.action.get() == "选点分析" else "单击添加轮廓点"
        self.status.set(f"光标：X={x:.1f} mm，Y={y:.1f} mm | {action}")

    def load(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self._set_mask(data)
            self.initial_path = str(Path(path).resolve())
            if self.dependency_error:
                self.dependency_info.set("机器预览未载入；按当前几何参数预览。")
            self.dependency_error = None
            self._set_save_state()
            self.status.set(f"已载入：{Path(path).name}；可清空后重画。")
        except Exception as exc:
            messagebox.showerror("读取失败", f"无法读取修正板：{exc}", parent=self.win)

    def open_mask(self):
        options = {"parent": self.win, "title": "打开修正板文件",
                   "filetypes": [("修正板 JSON", "*.json"), ("所有文件", "*.*")]}
        if self.initial_path:
            options["initialdir"] = str(Path(self.initial_path).parent)
        path = filedialog.askopenfilename(**options)
        if path:
            self.load(path)

    def _set_save_state(self):
        state = tk.DISABLED if self.dependency_error else tk.NORMAL
        self.save_button.configure(state=state)
        self.save_as_button.configure(state=state)

    def load_machine(self):
        """Resolve machine dependencies for previews without writing the machine."""
        path = filedialog.askopenfilename(parent=self.win, title="载入机器预览",
                                          filetypes=[("机器 JSON", "*.json")])
        if not path:
            return
        try:
            machine = read_machine_profile(path)
            bundle, errors = resolve_machine_profile(machine, Path(path).resolve().parent)
            if errors:
                raise ValueError("机器级联依赖无效：\n" + "\n".join(errors))
            first_source = bundle["sources"][0]
            first_profile = first_source["profile"]
            geometry = {key: bundle[key] for key in MACHINE_FIELDS}
            settings = replace(
                self.settings, **geometry,
                substrate_profile=self.substrate_profile,
                substrate_radius_mm=effective_radius_mm(self.substrate_profile),
                gear_module_mm=bundle["gear_module_mm"],
                source_x_mm=first_source["x_mm"],
                source_y_mm=first_source["y_mm"],
                source_exponent=first_profile["source_exponent"])
            settings.validate()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.dependency_error = str(exc)
            self.dependency_info.set(f"⚠ {exc}")
            self._set_save_state()
            self.status.set(f"⚠ 机器依赖无效，无法保存：{exc}")
            messagebox.showerror("载入机器失败", str(exc), parent=self.win)
            return
        masks = bundle["masks"]
        if masks:
            number = simpledialog.askinteger(
                "选择修正板", f"载入第几块板？范围 1–{len(masks)}",
                parent=self.win, initialvalue=1, minvalue=1, maxvalue=len(masks))
            if number is None:
                return
            mask_index = number - 1
        else:
            mask_index = None
        if (self.outer or self.holes or self.current) and not messagebox.askyesno(
                "替换画布", "载入机器及其板文件会替换当前画布，继续吗？",
                parent=self.win):
            return
        self.loaded_machine_path = str(Path(path).resolve())
        self.settings = settings
        self.source_profile = first_profile
        self.display_sources = self._prepare_sources(bundle["sources"])
        self.analysis_source_box.configure(values=self._source_choices())
        self.analysis_source.set(self._source_choices()[0])
        self._refresh_source_legend()
        self.selected_point = None
        self.current = []
        if mask_index is None:
            self.outer, self.holes = [], []
            self.initial_path = None
            self.z.set(str(self._minimum_substrate_z() * 0.5))
        else:
            self._set_mask(masks[mask_index]["profile"])
            self.initial_path = str((Path(path).resolve().parent /
                                     masks[mask_index]["file"]).resolve())
        self.dependency_error = None
        self.dependency_info.set(
            f"机器预览：{Path(path).name}；{len(bundle['sources'])} 个源，"
            f"{len(bundle['masks'])} 块板。")
        self._set_save_state()
        self._invalidate_board_weight("机器已载入，点击按钮计算当前板权重。")
        self._fit_sources_in_view()
        self._refresh_analysis()
        board_description = (f"板 {mask_index + 1}" if mask_index is not None
                             else "新板")
        self.status.set(f"已载入机器预览：{Path(path).name}，当前编辑 {board_description}。"
                        "基片预览可另行载入；保存仅写当前板文件。")

    def load_substrate(self):
        """Load a substrate for previews without changing the board or machine."""
        path = filedialog.askopenfilename(parent=self.win, title="载入基片预览",
                                          filetypes=[("基片 JSON", "*.json")])
        if not path:
            return
        try:
            profile = read_substrate_profile(path)
            settings = replace(
                self.settings, substrate_profile=profile,
                substrate_radius_mm=effective_radius_mm(profile))
            settings.validate()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            messagebox.showerror("载入基片失败", str(exc), parent=self.win)
            return
        self.substrate_profile = profile
        self.substrate_surface = profile["surface"]
        self.settings = settings
        self._invalidate_board_weight("基片预览已改变，请重新计算权重。")
        self._refresh_analysis()
        self.status.set(f"已载入基片预览：{Path(path).name}。实际计算使用工艺文件指定的基片。")

    def new_machine_board(self):
        if (self.outer or self.holes or self.current) and not messagebox.askyesno(
                "新建修正板", "新建板会清空当前画布，继续吗？", parent=self.win):
            return
        self.outer, self.holes, self.current = [], [], []
        self.selected_point = None
        self.initial_path = None
        self.z.set(str(self._minimum_substrate_z() * 0.5))
        self._invalidate_board_weight("新板尚无轮廓。")
        self._refresh_analysis()
        self.status.set("新建修正板；绘制并闭合轮廓后保存为独立板文件。")

    def _set_mask(self, data):
        # An imported plate may need its height edited for the current machine.
        data = validate_mask_profile(data)
        self.z.set(str(data["z_mm"]))
        self.outer = data["outer"]
        self.holes = data["holes"]
        self._invalidate_board_weight("轮廓已载入，请重新计算当前板权重。")
        all_points = self.outer + [p for hole in self.holes for p in hole]
        if all_points:
            span = max(max(abs(p[0] - self.view_center[0]),
                           abs(p[1] - self.view_center[1])) for p in all_points)
            self.view_radius = max(self.view_radius, span * 1.2)
        self.redraw()

    def save_as(self):
        self.save(as_new=True)

    def _validated_mask_from_canvas(self):
        outer, holes = self.outer, self.holes
        closes_current = bool(self.current)
        if self.current:
            if not messagebox.askyesno("未闭合的轮廓", "当前轮廓尚未闭合。是否先自动闭合再保存？",
                                       parent=self.win):
                return None
            if len(self.current) < 3:
                messagebox.showinfo("点数不足", "闭合轮廓至少需要 3 个点。", parent=self.win)
                return None
            points = smooth_closed(self.current) if self.mode.get() == "平滑曲线" else self.current
            if len(points) > 500:
                messagebox.showerror("轮廓过复杂", "单个轮廓最多保存 500 个点。", parent=self.win)
                return None
            if self.kind.get() == "外轮廓":
                outer = points
            else:
                holes = [*self.holes, points]
        if len(outer) < 3:
            messagebox.showerror("缺少外轮廓", "先绘制并闭合一个外轮廓。", parent=self.win)
            return None
        try:
            z = float(self.z.get())
            minimum_z = self._minimum_substrate_z()
            if not math.isfinite(z) or not 0 < z < minimum_z:
                raise ValueError(f"板高必须在 0 和基片最低点 {minimum_z:g} mm 之间")
        except ValueError as exc:
            messagebox.showerror("板高无效", str(exc), parent=self.win)
            return None
        try:
            data = validate_mask_profile({"z_mm": z, "outer": outer,
                                          "holes": holes},
                                         substrate_z_mm=self._minimum_substrate_z())
        except ValueError as exc:
            messagebox.showerror("修正板无效", str(exc), parent=self.win)
            return None
        return data, outer, holes, closes_current

    def _finish_canvas_save(self, outer, holes, closes_current):
        if closes_current:
            self.outer, self.holes, self.current = outer, holes, []
            self._invalidate_board_weight("轮廓已闭合，请重新计算当前板权重。")
            self.redraw()

    def save(self, as_new=False):
        if self.dependency_error:
            messagebox.showerror("无法保存", self.dependency_error, parent=self.win)
            return
        prepared = self._validated_mask_from_canvas()
        if prepared is None:
            return
        data, outer, holes, closes_current = prepared
        path = self.initial_path
        if as_new or not path:
            dialog_options = {
                "parent": self.win,
                "title": "另存修正板为新文件" if as_new else "首次保存修正板",
                "defaultextension": ".json",
                "filetypes": [("修正板 JSON", "*.json")],
            }
            if path:
                current_path = Path(path).resolve()
                dialog_options["initialdir"] = str(current_path.parent)
                dialog_options["initialfile"] = current_path.name
            path = filedialog.asksaveasfilename(**dialog_options)
        if not path:
            return
        if as_new and self.initial_path and \
                Path(path).resolve() == Path(self.initial_path).resolve():
            messagebox.showerror(
                "另存为需要新路径",
                "请选择与当前板文件不同的路径；覆盖当前文件请使用“保存到当前文件”。",
                parent=self.win)
            return
        try:
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            resolved_path = str(Path(path).resolve())
            if self.on_saved is not None:
                self.on_saved(resolved_path)
            self._finish_canvas_save(outer, holes, closes_current)
            self.initial_path = resolved_path
            self.status.set(f"已保存到当前板文件：{Path(path).name}")
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.win)


def main():
    root = tk.Tk()
    root.withdraw()
    designer = MaskDesigner(root)
    designer.win.bind(
        "<Destroy>",
        lambda event: root.after_idle(root.destroy)
        if event.widget == designer.win else None,
        add="+")
    root.mainloop()


if __name__ == "__main__":
    main()
