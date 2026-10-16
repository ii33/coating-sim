"""Standalone editor for a location-free evaporation-source JSON."""

import math
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from coat_sim.core.source_profiles import (read_source_profile, source_samples,
                             validate_source_profile, write_source_profile)
from legacy.window_layout import maximize_on_open

DEVICE_TYPES = {
    "其他 / 自定义": "custom", "电阻舟": "resistance_boat",
    "坩埚": "crucible", "电子束蒸发源": "electron_beam",
}
GEOMETRY_TYPES = {"点源": "point", "线源": "line",
                  "圆形面源": "disk", "环形面源": "annulus"}
DEVICE_NAMES = {value: key for key, value in DEVICE_TYPES.items()}
GEOMETRY_NAMES = {value: key for key, value in GEOMETRY_TYPES.items()}
PRESETS = {
    "resistance_boat": ("line", {"length_mm": "40", "angle_deg": "0"}),
    "crucible": ("disk", {"radius_mm": "12"}),
    "electron_beam": ("disk", {"radius_mm": "8"}),
}


class SourceDesigner:
    def __init__(self, parent=None, settings=None, initial_path=None, on_saved=None,
                 initial_profile=None, embedded=False):
        self.win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.win.title("蒸发源设计器")
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{min(1120, sw - 60)}x{min(740, sh - 90)}")
        self.win.minsize(min(780, sw - 30), min(540, sh - 40))
        maximize_on_open(self.win)
        self.on_saved = on_saved
        self.current_path = str(Path(initial_path).resolve()) if initial_path else None
        self._preview_after = None
        self.status = tk.StringVar(master=self.win, value="源形状以局部原点为中心；安装位置在机器设计器中设置。")
        if initial_profile is None and self.current_path:
            try:
                initial_profile = read_source_profile(self.current_path)
            except (OSError, ValueError) as exc:
                self.current_path = None
                self.status.set(f"读取失败：{exc}")
        profile = initial_profile or {"kind": "point", "device_type": "custom",
                                      "source_exponent": 1.0}
        self.device = tk.StringVar(master=self.win,
            value=DEVICE_NAMES.get(profile.get("device_type"), "其他 / 自定义"))
        self.kind = tk.StringVar(master=self.win,
            value=GEOMETRY_NAMES.get(profile.get("kind"), "点源"))
        self.exponent = tk.StringVar(master=self.win,
            value=str(profile.get("source_exponent", 1.0)))
        self.length = tk.StringVar(master=self.win,
            value=str(profile.get("length_mm", 40)))
        self.angle = tk.StringVar(master=self.win,
            value=str(profile.get("angle_deg", 0)))
        self.radius = tk.StringVar(master=self.win,
            value=str(profile.get("radius_mm", 12)))
        self.inner = tk.StringVar(master=self.win,
            value=str(profile.get("inner_radius_mm", 6)))
        self.outer = tk.StringVar(master=self.win,
            value=str(profile.get("outer_radius_mm", 15)))
        self.preview_text = tk.StringVar(master=self.win, value="")

        body = ttk.Frame(self.win, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body, width=340)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        left.pack_propagate(False)
        ttk.Label(left, text="独立源设计", font="TkHeadingFont").pack(anchor="w", pady=(0, 12))
        ttk.Button(left, text="打开源文件…", command=self.open_profile).pack(
            anchor="w", pady=(0, 12))
        ttk.Label(left, text="源文件保存设备类型、形状、尺寸和角指数；不保存 X/Y 坐标。",
                  wraplength=325, justify=tk.LEFT).pack(anchor="w", pady=(0, 12))
        group = ttk.LabelFrame(left, text="发射几何", padding=10)
        group.pack(fill=tk.X)
        group.columnconfigure(1, weight=1)
        self._combo(group, 0, "设备类型", self.device, tuple(DEVICE_TYPES),
                    self._device_changed)
        self._combo(group, 1, "几何类型", self.kind, tuple(GEOMETRY_TYPES),
                    self._kind_changed)
        self._entry(group, 2, "源角指数 n", self.exponent)
        self.shape = ttk.LabelFrame(left, text="形状尺寸", padding=10)
        self.shape.pack(fill=tk.X, pady=(10, 0))
        self.shape.columnconfigure(1, weight=1)
        self.shape_widgets = []
        for row, (label, variable) in enumerate((
            ("线长 / mm", self.length), ("线方向 / °", self.angle),
            ("圆半径 / mm", self.radius), ("内半径 / mm", self.inner),
            ("外半径 / mm", self.outer),
        )):
            widgets = self._entry(self.shape, row, label, variable)
            self.shape_widgets.append(widgets)
        self._show_shape()
        ttk.Label(left, text="n=0 表示近似各向同性；n=1 表示余弦发射。"
                  "设备类型只给出示例尺寸，不代表已标定。",
                  wraplength=325, justify=tk.LEFT).pack(anchor="w", pady=(12, 0))

        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(right, text="从 +Z 向下俯视 · 局部 XY",
                  font="TkHeadingFont").pack(anchor="w", pady=(0, 8))
        self.canvas = tk.Canvas(right, background="#f7f9fc",
                                highlightthickness=1, highlightbackground="#aab4c0")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._schedule_preview)
        ttk.Label(right, textvariable=self.preview_text,
                  wraplength=600).pack(anchor="w", pady=(7, 0))

        footer = ttk.Frame(self.win, padding=(12, 0, 12, 12))
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status,
                  wraplength=600).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(footer, text="另存为…", command=self.save_as).pack(side=tk.RIGHT)
        ttk.Button(footer, text="保存", command=self.save).pack(side=tk.RIGHT, padx=8)
        for variable in (self.exponent, self.length, self.angle, self.radius,
                         self.inner, self.outer):
            variable.trace_add("write", self._schedule_preview)
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self._schedule_preview()

    def _combo(self, parent, row, label, variable, values, callback):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        widget = ttk.Combobox(parent, textvariable=variable, values=values,
                              state="readonly", width=20)
        widget.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=5)
        widget.bind("<<ComboboxSelected>>", callback)

    def _entry(self, parent, row, label, variable):
        text = ttk.Label(parent, text=label)
        entry = ttk.Entry(parent, textvariable=variable, width=18)
        text.grid(row=row, column=0, sticky="w", pady=5)
        entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=5)
        return text, entry

    def _device_changed(self, _event=None):
        preset = PRESETS.get(DEVICE_TYPES[self.device.get()])
        if preset:
            kind, values = preset
            self.kind.set(GEOMETRY_NAMES[kind])
            variables = {"length_mm": self.length, "angle_deg": self.angle,
                         "radius_mm": self.radius}
            for name, value in values.items():
                variables[name].set(value)
            self.status.set("已填入示例尺寸；请按设备实际尺寸调整。")
        self._show_shape()
        self._schedule_preview()

    def _kind_changed(self, _event=None):
        self._show_shape()
        self._schedule_preview()

    def _show_shape(self):
        kind = GEOMETRY_TYPES[self.kind.get()]
        active = {"point": (), "line": (0, 1), "disk": (2,),
                  "annulus": (3, 4)}[kind]
        for row, widgets in enumerate(self.shape_widgets):
            for widget in widgets:
                if row in active:
                    widget.grid()
                else:
                    widget.grid_remove()

    def _number(self, variable, label):
        try:
            value = float(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"{label}须为数值") from exc
        if not math.isfinite(value):
            raise ValueError(f"{label}须为有限数值")
        return value

    def _profile(self):
        kind = GEOMETRY_TYPES[self.kind.get()]
        data = {"kind": kind, "device_type": DEVICE_TYPES[self.device.get()],
                "source_exponent": self._number(self.exponent, "源角指数")}
        if kind == "line":
            data.update(length_mm=self._number(self.length, "线长"),
                        angle_deg=self._number(self.angle, "线方向"))
        elif kind == "disk":
            data["radius_mm"] = self._number(self.radius, "圆半径")
        elif kind == "annulus":
            data.update(inner_radius_mm=self._number(self.inner, "内半径"),
                        outer_radius_mm=self._number(self.outer, "外半径"))
        return validate_source_profile(data)

    def _schedule_preview(self, *_args):
        if self._preview_after is not None:
            self.win.after_cancel(self._preview_after)
        self._preview_after = self.win.after(90, self._draw_preview)

    def _draw_preview(self):
        self._preview_after = None
        canvas = self.canvas
        if not canvas.winfo_ismapped():
            return
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 2), max(canvas.winfo_height(), 2)
        cx, cy = width / 2, height / 2
        try:
            profile = self._profile()
            points, _weights = source_samples(
                SimpleNamespace(source_x_mm=0.0, source_y_mm=0.0), profile)
        except (ValueError, KeyError) as exc:
            self.preview_text.set(f"参数有误：{exc}")
            return
        reach = {"point": 0, "line": profile.get("length_mm", 0) / 2,
                 "disk": profile.get("radius_mm", 0),
                 "annulus": profile.get("outer_radius_mm", 0)}[profile["kind"]]
        scale = 0.38 * min(width, height) / max(20.0, reach * 1.35)
        def xy(x, y):
            return cx + x * scale, cy - y * scale
        canvas.create_line(16, cy, width - 16, cy, fill="#94a3b8", arrow=tk.LAST)
        canvas.create_line(cx, height - 16, cx, 16, fill="#94a3b8", arrow=tk.LAST)
        canvas.create_text(width - 18, cy - 8, text="+X", anchor="se")
        canvas.create_text(cx + 8, 18, text="+Y", anchor="nw")
        kind = profile["kind"]
        if kind == "line":
            half = profile["length_mm"] / 2
            angle = math.radians(profile["angle_deg"])
            dx, dy = half * math.cos(angle), half * math.sin(angle)
            canvas.create_line(*xy(-dx, -dy), *xy(dx, dy), fill="#2563eb", width=5)
        elif kind in ("disk", "annulus"):
            radius = profile["radius_mm"] if kind == "disk" else profile["outer_radius_mm"]
            radius *= scale
            canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                               fill="#dbeafe", outline="#2563eb", width=2)
            if kind == "annulus":
                radius = profile["inner_radius_mm"] * scale
                canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                                   fill="#f7f9fc", outline="#2563eb", width=2)
        for x, y in points:
            px, py = xy(x, y)
            canvas.create_oval(px-3, py-3, px+3, py+3, fill="#1d4ed8", outline="")
        canvas.create_line(cx-7, cy, cx+7, cy, fill="#b91c1c", width=2)
        canvas.create_line(cx, cy-7, cx, cy+7, fill="#b91c1c", width=2)
        self.preview_text.set(f"{GEOMETRY_NAMES[kind]} · {len(points)} 个离散源元；"
                              "红十字为局部原点，机器指定安装位置。")

    def _close(self):
        if self._preview_after is not None:
            self.win.after_cancel(self._preview_after)
        self.win.destroy()

    def open_profile(self):
        path = filedialog.askopenfilename(
            parent=self.win, title="打开蒸发源 JSON",
            initialdir=str(Path(self.current_path).parent) if self.current_path
                       else str(Path.cwd()),
            filetypes=[("蒸发源 JSON", "*.json")])
        if not path:
            return
        try:
            profile = read_source_profile(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.win)
            return
        self.current_path = str(Path(path).resolve())
        self.device.set(DEVICE_NAMES[profile["device_type"]])
        self.kind.set(GEOMETRY_NAMES[profile["kind"]])
        self.exponent.set(str(profile["source_exponent"]))
        self.length.set(str(profile.get("length_mm", 40)))
        self.angle.set(str(profile.get("angle_deg", 0)))
        self.radius.set(str(profile.get("radius_mm", 12)))
        self.inner.set(str(profile.get("inner_radius_mm", 6)))
        self.outer.set(str(profile.get("outer_radius_mm", 15)))
        self._show_shape()
        self.status.set(f"已打开：{self.current_path}")
        self._schedule_preview()

    def save(self):
        self._save(as_new=False)

    def save_as(self):
        self._save(as_new=True)

    def _save(self, *, as_new):
        try:
            profile = self._profile()
        except (ValueError, KeyError) as exc:
            messagebox.showerror("源参数无效", str(exc), parent=self.win)
            return
        path = self.current_path
        if as_new or path is None:
            selected = filedialog.asksaveasfilename(
                parent=self.win, title="另存蒸发源" if as_new else "保存蒸发源",
                initialdir=str(Path(path).parent) if path else None,
                initialfile=Path(path).name if path else "source.json",
                defaultextension=".json", filetypes=[("蒸发源 JSON", "*.json")])
            if not selected:
                return
            selected = str(Path(selected).resolve())
            if as_new and path and selected == str(Path(path).resolve()):
                messagebox.showerror("另存为失败", "请选择不同于当前文件的新路径。",
                                     parent=self.win)
                return
            path = selected
        try:
            write_source_profile(path, profile)
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.win)
            return
        self.current_path = str(Path(path).resolve())
        self.status.set(f"已保存：{self.current_path}")
        if self.on_saved is not None:
            self.on_saved(self.current_path, profile)


if __name__ == "__main__":
    SourceDesigner().win.mainloop()
