"""Independent editor for the physical substrate outline and surface."""

import math
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

from coat_sim.core.substrate_profiles import (effective_radius_mm, new_substrate_profile,
                                outline_polygon_points, read_substrate_profile,
                                sample_curve_points, validate_substrate_profile,
                                write_substrate_profile)
from coat_sim.core.substrate_surface import surface_height_and_normal
from legacy.window_layout import maximize_on_open


OUTLINE_KINDS = {"圆形": "circle", "矩形（长宽）": "rectangle",
                 "自定义多边形": "polygon", "闭合曲线": "curve"}
SURFACE_KINDS = {"平面": "flat", "球面": "spherical", "非球面": "asphere"}


class SubstrateDesigner:
    """Edit one self-contained substrate JSON file, without loading a machine."""

    def __init__(self, parent=None, initial_path=None, on_saved=None):
        self.win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.win.title("基片设计器")
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{min(1180, sw - 60)}x{min(780, sh - 80)}")
        self.win.minsize(min(800, sw - 30), min(540, sh - 40))
        self.win.resizable(True, True)
        maximize_on_open(self.win)
        self.path = None
        self.on_saved = on_saved
        self._draw_after = None
        self._dirty = False
        self._loading = False
        self.profile = new_substrate_profile()
        self.name = tk.StringVar(master=self.win)
        self.outline_kind = tk.StringVar(master=self.win)
        self.surface_kind = tk.StringVar(master=self.win)
        self.radius = tk.StringVar(master=self.win)
        self.width = tk.StringVar(master=self.win)
        self.height = tk.StringVar(master=self.win)
        self.roc = tk.StringVar(master=self.win)
        self.conic = tk.StringVar(master=self.win)
        self.a4 = tk.StringVar(master=self.win)
        self.a6 = tk.StringVar(master=self.win)
        self.a8 = tk.StringVar(master=self.win)
        self.vertex_x = tk.StringVar(master=self.win)
        self.vertex_y = tk.StringVar(master=self.win)
        self.tool = tk.StringVar(master=self.win, value="选择/拖动")
        self.closed = True
        self._drag_item = None
        self._view = None
        self.status = tk.StringVar(master=self.win, value="局部 XY 俯视轮廓；高度 h(0)=0，+Z 指向远离蒸发源的一侧。")

        toolbar = ttk.Frame(self.win, padding=(12, 10))
        toolbar.pack(fill=tk.X)
        for label, action in (("新建", self.new), ("打开…", self.open),
                              ("保存", self.save), ("另存为…", self.save_as)):
            ttk.Button(toolbar, text=label, command=action).pack(side=tk.LEFT, padx=(0, 7))
        ttk.Label(toolbar, text="基片文件独立保存；工艺方案选择基片文件。",
                  foreground="#536375").pack(side=tk.RIGHT)

        body = ttk.PanedWindow(self.win, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        controls = ttk.Frame(body, width=445)
        preview_panel = ttk.Frame(body)
        body.add(controls, weight=1)
        body.add(preview_panel, weight=2)

        self.tabs = ttk.Notebook(controls)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        outline_tab = ttk.Frame(self.tabs, padding=8)
        surface_tab = ttk.Frame(self.tabs, padding=8)
        vertex_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(outline_tab, text="轮廓参数")
        self.tabs.add(surface_tab, text="球面 / 非球面")
        self.tabs.add(vertex_tab, text="顶点与曲线")
        self.vertex_tab = vertex_tab

        common = ttk.LabelFrame(outline_tab, text="基本参数", padding=10)
        common.pack(fill=tk.X, pady=(0, 8))
        common.columnconfigure(1, weight=1)
        self._entry(common, 0, "名称", self.name)
        ttk.Label(common, text="轮廓").grid(row=1, column=0, sticky="w", pady=5)
        outline_box = ttk.Combobox(common, textvariable=self.outline_kind,
                                   values=list(OUTLINE_KINDS), state="readonly")
        outline_box.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=5)
        outline_box.bind("<<ComboboxSelected>>", self._type_changed)
        ttk.Label(common, text="曲面").grid(row=2, column=0, sticky="w", pady=5)
        surface_box = ttk.Combobox(common, textvariable=self.surface_kind,
                                   values=list(SURFACE_KINDS), state="readonly")
        surface_box.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=5)
        surface_box.bind("<<ComboboxSelected>>", self._type_changed)
        self.radius_label, self.radius_entry = self._entry(common, 3, "圆半径 / mm", self.radius)
        self.width_label, self.width_entry = self._entry(common, 4, "长 X / mm", self.width)
        self.height_label, self.height_entry = self._entry(common, 5, "宽 Y / mm", self.height)
        ttk.Label(outline_tab,
                  text="圆形适用于平面、球面与非球面；矩形、多边形和曲线只支持平面。"
                       "切换轮廓后在右侧查看形状；多边形或曲线可在“顶点与曲线”页或画布编辑。",
                  wraplength=410, justify=tk.LEFT).pack(anchor="w", pady=10)

        surface_frame = ttk.LabelFrame(surface_tab, text="参数化曲面", padding=10)
        surface_frame.pack(fill=tk.X, pady=(0, 8))
        surface_frame.columnconfigure(1, weight=1)
        self.roc_label, self.roc_entry = self._entry(surface_frame, 0, "曲率半径 R / mm", self.roc)
        self.conic_label, self.conic_entry = self._entry(surface_frame, 1, "圆锥常数 k", self.conic)
        self.poly_fields = [self._entry(surface_frame, row, title, variable)
                            for row, (title, variable) in enumerate((
                                ("A4 / mm⁻³", self.a4), ("A6 / mm⁻⁵", self.a6),
                                ("A8 / mm⁻⁷", self.a8)), start=2)]
        ttk.Label(surface_frame,
                  text="h(r) = (r²/R)/(1+√[1−(1+k)r²/R²]) + A4r⁴ + A6r⁶ + A8r⁸",
                  wraplength=420, justify=tk.LEFT).grid(row=5, column=0, columnspan=2,
                                                         sticky="w", pady=(8, 0))

        polygon_frame = ttk.LabelFrame(vertex_tab, text="自定义平面轮廓顶点 / mm", padding=10)
        polygon_frame.pack(fill=tk.BOTH, expand=True)
        self.polygon_frame = polygon_frame
        tree_row = ttk.Frame(polygon_frame)
        tree_row.pack(fill=tk.BOTH, expand=True)
        self.vertices = ttk.Treeview(tree_row, columns=("x", "y"), show="headings",
                                     height=8, selectmode="browse")
        self.vertices.heading("x", text="X / mm")
        self.vertices.heading("y", text="Y / mm")
        self.vertices.column("x", width=130, anchor=tk.E)
        self.vertices.column("y", width=130, anchor=tk.E)
        vertex_scroll = ttk.Scrollbar(tree_row, orient=tk.VERTICAL,
                                      command=self.vertices.yview)
        self.vertices.configure(yscrollcommand=vertex_scroll.set)
        self.vertices.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vertex_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.vertices.bind("<<TreeviewSelect>>", self._vertex_selected)
        edit_row = ttk.Frame(polygon_frame)
        edit_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(edit_row, text="X").pack(side=tk.LEFT)
        ttk.Entry(edit_row, textvariable=self.vertex_x, width=11).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(edit_row, text="Y").pack(side=tk.LEFT)
        ttk.Entry(edit_row, textvariable=self.vertex_y, width=11).pack(side=tk.LEFT, padx=(4, 0))
        actions = ttk.Frame(polygon_frame)
        actions.pack(fill=tk.X, pady=(8, 0))
        for label, action in (("添加", self._add_vertex), ("更新选中", self._update_vertex),
                              ("删除选中", self._delete_vertex)):
            ttk.Button(actions, text=label, command=action).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(polygon_frame,
                  text="按表格顺序连接控制点；曲线每段取 5 个 Catmull–Rom 采样点。"
                       "删线会打开轮廓，闭合后才能保存；曲线自交按奇偶规则填充。",
                  wraplength=420).pack(anchor="w", pady=(8, 0))

        ttk.Label(preview_panel, text="基片俯视轮廓与径向截面",
                  font="TkHeadingFont").pack(anchor="w", pady=(2, 8))
        tool_row = ttk.Frame(preview_panel)
        tool_row.pack(fill=tk.X, pady=(0, 7))
        for label in ("选择/拖动", "添加点", "删除点", "删除线", "闭合"):
            ttk.Radiobutton(tool_row, text=label, value=label, variable=self.tool).pack(
                side=tk.LEFT, padx=(0, 5))
        ttk.Button(tool_row, text="闭合轮廓", command=self._close_outline).pack(side=tk.RIGHT)
        self.canvas = tk.Canvas(preview_panel, background="#fbfcfe",
                                highlightthickness=1, highlightbackground="#aab4c0")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._schedule_draw)
        self.canvas.bind("<Button-1>", self._canvas_click)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        ttk.Label(self.win, textvariable=self.status, anchor="w",
                  padding=(12, 5)).pack(fill=tk.X)

        for variable in (self.name, self.outline_kind, self.surface_kind,
                         self.radius, self.width, self.height, self.roc,
                         self.conic, self.a4, self.a6, self.a8):
            variable.trace_add("write", self._changed)
        self.win.bind("<Control-s>", lambda _event: self.save())
        self.win.bind("<Control-Shift-S>", lambda _event: self.save_as())
        if initial_path:
            try:
                self._load(read_substrate_profile(initial_path), Path(initial_path))
            except ValueError as exc:
                messagebox.showerror("基片文件无法加载", str(exc), parent=self.win)
                self._load(new_substrate_profile(), None)
        else:
            self._load(new_substrate_profile(), None)

    @staticmethod
    def _entry(parent, row, label, variable):
        label_widget = ttk.Label(parent, text=label)
        entry = ttk.Entry(parent, textvariable=variable)
        label_widget.grid(row=row, column=0, sticky="w", pady=5)
        entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=5, ipady=2)
        return label_widget, entry

    def _load(self, profile, path):
        self._loading = True
        self.profile = validate_substrate_profile(profile)
        self.path = path
        self.name.set(self.profile["name"])
        outline = self.profile["outline"]
        surface = self.profile["surface"]
        self.outline_kind.set(next(label for label, kind in OUTLINE_KINDS.items()
                                   if kind == outline["kind"]))
        self.surface_kind.set(next(label for label, kind in SURFACE_KINDS.items()
                                   if kind == surface["kind"]))
        self.radius.set(str(outline.get("radius_mm", effective_radius_mm(self.profile))))
        self.width.set(str(outline.get("width_mm", 300.0)))
        self.height.set(str(outline.get("height_mm", 200.0)))
        self.roc.set(str(surface.get("radius_mm", 400.0)))
        self.conic.set(str(surface.get("conic_k", 0.0)))
        for key, variable in (("a4", self.a4), ("a6", self.a6), ("a8", self.a8)):
            variable.set(str(surface.get(key, 0.0)))
        for item in self.vertices.get_children():
            self.vertices.delete(item)
        editable_points = outline.get("points_mm", outline.get(
            "control_points_mm", [[-100, -100], [100, -100],
                                  [100, 100], [-100, 100]]))
        for x, y in editable_points:
            self.vertices.insert("", tk.END, values=(format(x, ".8g"), format(y, ".8g")))
        self.closed = True
        self._loading = False
        self._dirty = False
        self._type_changed()
        self._update_title()

    def _update_title(self):
        filename = str(self.path) if self.path else "未命名"
        self.win.title(f"基片设计器 · {filename}{' *' if self._dirty else ''}")

    def _changed(self, *_args):
        if self._loading:
            return
        self._dirty = True
        self._update_title()
        self._schedule_draw()

    def _type_changed(self, _event=None):
        outline = OUTLINE_KINDS.get(self.outline_kind.get(), "circle")
        surface = SURFACE_KINDS.get(self.surface_kind.get(), "flat")
        for widget in (self.radius_label, self.radius_entry):
            (widget.grid if outline == "circle" else widget.grid_remove)()
        for widget in (self.width_label, self.width_entry,
                       self.height_label, self.height_entry):
            (widget.grid if outline == "rectangle" else widget.grid_remove)()
        self.tabs.tab(self.vertex_tab, state="normal" if outline in ("polygon", "curve")
                      else "disabled")
        if _event is not None and outline in ("polygon", "curve"):
            self.tabs.select(self.vertex_tab)
        elif outline not in ("polygon", "curve") and self.tabs.select() == str(self.vertex_tab):
            self.tabs.select(0)
        for widget in (self.roc_label, self.roc_entry):
            (widget.grid if surface != "flat" else widget.grid_remove)()
        for widget in (self.conic_label, self.conic_entry,
                       *(item for pair in self.poly_fields for item in pair)):
            (widget.grid if surface == "asphere" else widget.grid_remove)()
        self._schedule_draw()

    def _vertex_selected(self, _event=None):
        selected = self.vertices.selection()
        if selected:
            x, y = self.vertices.item(selected[0], "values")
            self.vertex_x.set(x)
            self.vertex_y.set(y)

    def _vertex_values(self):
        try:
            x, y = float(self.vertex_x.get()), float(self.vertex_y.get())
        except ValueError as exc:
            raise ValueError("顶点 X、Y 请输入数值") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("顶点 X、Y 必须为有限数值")
        return x, y

    def _add_vertex(self):
        try:
            x, y = self._vertex_values()
        except ValueError as exc:
            messagebox.showerror("顶点错误", str(exc), parent=self.win)
            return
        selected = self.vertices.selection()
        index = self.vertices.index(selected[0]) + 1 if selected else tk.END
        self.vertices.insert("", index, values=(format(x, ".8g"), format(y, ".8g")))
        self._changed()

    def _update_vertex(self):
        selected = self.vertices.selection()
        if not selected:
            return
        try:
            x, y = self._vertex_values()
        except ValueError as exc:
            messagebox.showerror("顶点错误", str(exc), parent=self.win)
            return
        self.vertices.item(selected[0], values=(format(x, ".8g"), format(y, ".8g")))
        self._changed()

    def _delete_vertex(self):
        selected = self.vertices.selection()
        if selected:
            self.vertices.delete(selected[0])
            self._changed()

    def _control_points(self):
        return [[float(x), float(y)] for item in self.vertices.get_children()
                for x, y in [self.vertices.item(item, "values")]]

    def _set_control_points(self, points, selected_index=None):
        for item in self.vertices.get_children():
            self.vertices.delete(item)
        for x, y in points:
            self.vertices.insert("", tk.END,
                                 values=(format(x, ".9g"), format(y, ".9g")))
        if selected_index is not None and points:
            item = self.vertices.get_children()[min(selected_index, len(points) - 1)]
            self.vertices.selection_set(item)
            self.vertices.see(item)
        self._changed()

    def _close_outline(self):
        if OUTLINE_KINDS.get(self.outline_kind.get()) not in ("polygon", "curve"):
            self.status.set("圆形和矩形始终闭合；顶点闭合工具用于多边形和曲线。")
            return
        if len(self.vertices.get_children()) < 3:
            self.status.set("至少需要 3 个控制点才能闭合轮廓。")
            return
        self.closed = True
        self._changed()
        self.status.set("已闭合轮廓；保存时会检查交叉和面积。")

    def _canvas_to_local(self, x, y):
        if self._view is None:
            return None
        cx, cy, scale, top_height = self._view
        if y < 0 or y > top_height or scale <= 0:
            return None
        return (x - cx) / scale, (cy - y) / scale

    def _nearest_vertex(self, x, y):
        if self._view is None:
            return None
        cx, cy, scale, _ = self._view
        points = self._control_points()
        if not points:
            return None
        distances = [math.hypot(x - (cx + px * scale),
                                y - (cy - py * scale)) for px, py in points]
        index = min(range(len(points)), key=distances.__getitem__)
        return index if distances[index] <= 13 else None

    def _nearest_edge(self, x, y):
        if self._view is None or not self.closed:
            return None
        cx, cy, scale, _ = self._view
        points = self._control_points()
        if len(points) < 3:
            return None
        best_index, best_distance = None, 13.0
        for index, (ax, ay) in enumerate(points):
            bx, by = points[(index + 1) % len(points)]
            ax, ay = cx + ax * scale, cy - ay * scale
            bx, by = cx + bx * scale, cy - by * scale
            dx, dy = bx - ax, by - ay
            fraction = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) /
                                     max(dx * dx + dy * dy, 1e-12)))
            distance = math.hypot(x - (ax + fraction * dx),
                                  y - (ay + fraction * dy))
            if distance < best_distance:
                best_index, best_distance = index, distance
        return best_index

    def _canvas_click(self, event):
        if OUTLINE_KINDS.get(self.outline_kind.get()) not in ("polygon", "curve"):
            return
        local = self._canvas_to_local(event.x, event.y)
        if local is None:
            return
        tool = self.tool.get()
        if tool == "闭合":
            self._close_outline()
            return
        points = self._control_points()
        if tool == "添加点":
            selected = self.vertices.selection()
            index = self.vertices.index(selected[0]) + 1 if selected else len(points)
            points.insert(index, list(local))
            self._set_control_points(points, index)
            return
        if tool == "删除线":
            index = self._nearest_edge(event.x, event.y)
            if index is None:
                self.status.set("请在闭合轮廓的一条控制边上单击。")
                return
            # Rotate the chain so the selected closing edge is omitted.
            points = points[index + 1:] + points[:index + 1]
            self.closed = False
            self._set_control_points(points)
            self.status.set("所选线段已删除；轮廓现在打开。可继续编辑，再点击“闭合轮廓”。")
            return
        index = self._nearest_vertex(event.x, event.y)
        if index is None:
            return
        item = self.vertices.get_children()[index]
        if tool == "删除点":
            self.vertices.delete(item)
            self._changed()
            return
        self.vertices.selection_set(item)
        self.vertices.see(item)
        self._drag_item = item

    def _canvas_drag(self, event):
        if self._drag_item is None or self.tool.get() != "选择/拖动":
            return
        local = self._canvas_to_local(event.x, event.y)
        if local is None:
            return
        self.vertices.item(self._drag_item,
                           values=(format(local[0], ".9g"), format(local[1], ".9g")))
        self.vertex_x.set(format(local[0], ".9g"))
        self.vertex_y.set(format(local[1], ".9g"))
        self._changed()

    def _canvas_release(self, _event):
        self._drag_item = None

    @staticmethod
    def _number(variable, label):
        try:
            value = float(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"{label}：请输入数值") from exc
        if not math.isfinite(value):
            raise ValueError(f"{label}：必须为有限数值")
        return value

    def _parse(self):
        outline_kind = OUTLINE_KINDS[self.outline_kind.get()]
        if outline_kind == "circle":
            outline = {"kind": "circle", "radius_mm": self._number(self.radius, "圆半径")}
        elif outline_kind == "rectangle":
            outline = {"kind": "rectangle", "width_mm": self._number(self.width, "矩形长"),
                       "height_mm": self._number(self.height, "矩形宽")}
        else:
            if not self.closed:
                raise ValueError("轮廓尚未闭合；请选择“闭合轮廓”后保存")
            points = self._control_points()
            outline = ({"kind": "polygon", "points_mm": points}
                       if outline_kind == "polygon" else
                       {"kind": "curve", "control_points_mm": points})
        kind = SURFACE_KINDS[self.surface_kind.get()]
        surface = {"kind": kind}
        if kind != "flat":
            surface["radius_mm"] = self._number(self.roc, "曲率半径")
        if kind == "asphere":
            for key, variable, label in (("conic_k", self.conic, "圆锥常数"),
                                         ("a4", self.a4, "A4"),
                                         ("a6", self.a6, "A6"),
                                         ("a8", self.a8, "A8")):
                surface[key] = self._number(variable, label)
        return validate_substrate_profile({"schema_version": 1,
                                           "name": self.name.get(),
                                           "outline": outline,
                                           "surface": surface})

    def _schedule_draw(self, _event=None):
        if self._draw_after is None:
            self._draw_after = self.win.after(65, self._draw)

    def _draw(self):
        self._draw_after = None
        if not self.canvas.winfo_exists():
            return
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 120 or height < 120:
            return
        kind = OUTLINE_KINDS.get(self.outline_kind.get(), "circle")
        try:
            profile = self._parse()
            self.status.set("有效基片配置。保存后可由工艺方案引用。")
        except (ValueError, KeyError) as exc:
            profile = None
            self.status.set(f"配置尚不能保存：{exc}")
        try:
            if kind == "circle":
                radius = self._number(self.radius, "圆半径")
                if radius <= 0:
                    raise ValueError("圆半径须为正数")
                points = None
                controls = []
            elif kind == "rectangle":
                half_w = self._number(self.width, "矩形长") / 2
                half_h = self._number(self.height, "矩形宽") / 2
                if half_w <= 0 or half_h <= 0:
                    raise ValueError("矩形长宽须为正数")
                points = [[-half_w, -half_h], [half_w, -half_h],
                          [half_w, half_h], [-half_w, half_h]]
                controls = []
                radius = math.hypot(half_w, half_h)
            else:
                controls = self._control_points()
                if kind == "curve" and self.closed and len(controls) >= 3:
                    points = sample_curve_points(controls)
                else:
                    points = controls
                radius = max((math.hypot(x, y) for x, y in points), default=1.0)
                radius = max(radius, 1.0)
        except (ValueError, TypeError) as exc:
            canvas.create_text(width / 2, height / 2, text=str(exc), fill="#a33333",
                               width=width - 30)
            self._view = None
            return
        top_height = height * 0.64
        scale = min((width - 75) / (2.2 * radius),
                    (top_height - 70) / (2.2 * radius))
        cx, cy = width / 2, top_height / 2 + 5
        self._view = (cx, cy, scale, top_height)
        canvas.create_line(20, cy, width - 20, cy, fill="#d5dde6")
        canvas.create_line(cx, 20, cx, top_height - 20, fill="#d5dde6")
        outline_color = "#185b98" if profile is not None else "#a33333"
        is_closed = self.closed or kind in ("circle", "rectangle")
        if kind == "circle":
            extent = radius * scale
            canvas.create_oval(cx - extent, cy - extent, cx + extent, cy + extent,
                               fill="#dbeafe" if profile is not None else "#fce8e5",
                               outline=outline_color, width=2)
        elif is_closed and len(points) >= 3:
            coords = [coord for x, y in points
                      for coord in (cx + x * scale, cy - y * scale)]
            if kind == "curve":
                # Tk's polygon fill need not match the simulation's even-odd
                # rule on a self-crossing curve; show the sampled boundary.
                canvas.create_line(coords + coords[:2], fill=outline_color, width=2)
            else:
                canvas.create_polygon(coords,
                                      fill="#dbeafe" if profile is not None else "#fce8e5",
                                      outline=outline_color, width=2)
        elif points and len(points) >= 2:
            coords = [coord for x, y in points
                      for coord in (cx + x * scale, cy - y * scale)]
            canvas.create_line(coords, fill="#a33333", width=2)
        if kind in ("polygon", "curve"):
            if kind == "curve" and len(controls) >= 2:
                # The dashed control chain identifies the edge deleted by the
                # line tool; the smooth outline is the actual sampled boundary.
                chain = controls + ([controls[0]] if is_closed else [])
                coords = [coord for x, y in chain
                          for coord in (cx + x * scale, cy - y * scale)]
                canvas.create_line(coords, fill="#7b91a8", dash=(4, 4))
            selected = self.vertices.selection()
            selected_index = (self.vertices.index(selected[0]) if selected else -1)
            for index, (x, y) in enumerate(controls):
                px, py = cx + x * scale, cy - y * scale
                canvas.create_oval(px - 6, py - 6, px + 6, py + 6,
                                   fill="#d45435" if index == selected_index else "#f5ad56",
                                   outline="#8b4b30")
                canvas.create_text(px + 8, py - 8, anchor="sw", text=str(index + 1),
                                   fill="#455a68")
        canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                           fill="#cb4e35", outline="")
        canvas.create_text(15, top_height - 20, anchor="w",
                           text=f"轮廓外接半径约 {radius:g} mm · +X → · +Y ↑"
                                + (" · 草稿未闭合" if not is_closed else ""),
                           fill="#354960")
        canvas.create_line(15, top_height, width - 15, top_height, fill="#bfcbd7")
        if profile is None:
            canvas.create_text(width / 2, top_height + (height - top_height) / 2,
                               text="当前草稿仍可编辑；修正错误并闭合后即可保存。",
                               fill="#a33333", width=width - 30)
            return
        radius = effective_radius_mm(profile)
        xs = np.linspace(-radius, radius, 181)
        hh = surface_height_and_normal(xs, np.zeros_like(xs), radius,
                                       profile["surface"])[0]
        y0, y1 = top_height + 30, height - 35
        min_h, max_h = float(np.nanmin(hh)), float(np.nanmax(hh))
        span = max(max_h - min_h, 1e-8)
        xscale = (width - 50) / (2 * radius)
        yscale = (y1 - y0) / span
        coords = [coord for x, h in zip(xs, hh)
                  for coord in (width / 2 + x * xscale, y1 - (h - min_h) * yscale)]
        canvas.create_line(coords, fill="#c35a3d", width=2, smooth=True)
        canvas.create_text(16, top_height + 8, anchor="w",
                           text=f"径向截面 h(r) / mm：{min_h:.5g} ～ {max_h:.5g}",
                           fill="#354960")

    def new(self):
        self._load(new_substrate_profile(), None)
        self.status.set("已新建基片；首次保存将选择路径。")

    def open(self):
        path = filedialog.askopenfilename(parent=self.win, title="打开基片文件",
                                           filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self._load(read_substrate_profile(path), Path(path))
            self.status.set(f"已加载：{path}")
        except ValueError as exc:
            messagebox.showerror("基片文件无法加载", str(exc), parent=self.win)

    def _write(self, path):
        try:
            profile = self._parse()
            write_substrate_profile(path, profile)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存基片", str(exc), parent=self.win)
            return False
        self.profile = profile
        self.path = Path(path)
        self._dirty = False
        self._update_title()
        self.status.set(f"已保存：{self.path}")
        if self.on_saved:
            self.on_saved(str(self.path), profile)
        return True

    def save(self):
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self):
        path = filedialog.asksaveasfilename(parent=self.win, title="基片另存为",
                                            defaultextension=".json",
                                            initialfile=(self.path.name if self.path else "substrate.json"),
                                            filetypes=[("JSON", "*.json")])
        if not path:
            return False
        if self.path is not None and Path(path).resolve() == self.path.resolve():
            messagebox.showerror("基片另存为", "请选择与当前文件不同的路径。", parent=self.win)
            return False
        return self._write(path)


def main():
    designer = SubstrateDesigner()
    designer.win.mainloop()


if __name__ == "__main__":
    main()
