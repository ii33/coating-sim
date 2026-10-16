"""Full-screen module launcher with a resolution-aware illustrated backdrop."""

import ctypes
import math
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox

from PIL import Image, ImageDraw, ImageFilter, ImageTk


MODULES = (
    ("膜厚仿真", "app.py", "选择工艺并计算膜厚"),
    ("工艺", "process_designer.py", "安排镀膜步骤"),
    ("机器", "machine_designer.py", "配置机器结构"),
    ("蒸发源", "source_designer.py", "设计蒸发源"),
    ("基片", "substrate_designer.py", "设计基片"),
    ("修正板", "mask_designer.py", "绘制修正板"),
)
BACKGROUND = Path(__file__).parent / "assert" / "launcher.png"
FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                   "Noto Sans CJK SC", "WenQuanYi Micro Hei", "SimHei")


def _enable_windows_dpi():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def _mix(first, second, amount):
    a = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
    b = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def _set_window_pos(hwnd, x, y, flags):
    set_pos = ctypes.windll.user32.SetWindowPos
    set_pos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    set_pos.restype = ctypes.c_int
    if not set_pos(hwnd, None, x, y, 0, 0, flags):
        raise OSError("无法更新启动器窗口位置或边框")


def _remove_windows_caption(root):
    """Keep a managed Windows taskbar window while drawing our own top edge."""
    user32 = ctypes.windll.user32
    hwnd = int(root.wm_frame(), 16)
    get_style = user32.GetWindowLongPtrW
    set_style = user32.SetWindowLongPtrW
    get_style.argtypes = (ctypes.c_void_p, ctypes.c_int)
    get_style.restype = ctypes.c_ssize_t
    set_style.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
    set_style.restype = ctypes.c_ssize_t
    style = get_style(hwnd, -16)  # GWL_STYLE
    if not style:
        raise OSError("无法读取启动器窗口样式")
    if style & 0x00C00000:  # WS_CAPTION
        if not set_style(hwnd, -16, style & ~0x00C00000):
            raise OSError("无法移除启动器原生标题栏")
        flags = 0x0001 | 0x0002 | 0x0004 | 0x0020
        _set_window_pos(hwnd, 0, 0, flags)


class Launcher:
    def __init__(self, root):
        self.root = root
        self.root.title("行星盘沉积仿真")
        self.root.attributes("-fullscreen", True)
        self.fullscreen = True
        self.root.minsize(840, 520)
        installed = set(tkfont.families(root))
        self.family = next((name for name in FONT_CANDIDATES if name in installed),
                           tkfont.nametofont("TkDefaultFont").actual("family"))
        self.title_font = tkfont.Font(root=root, family=self.family, weight="bold", size=-36)
        self.note_font = tkfont.Font(root=root, family=self.family, size=-17)
        self.button_font = tkfont.Font(root=root, family=self.family, weight="bold", size=-22)
        self.detail_font = tkfont.Font(root=root, family=self.family, size=-15)
        self.control_font = tkfont.Font(root=root, family=self.family, size=-25)
        self.source_image = Image.open(BACKGROUND).convert("RGBA")
        self.canvas = tk.Canvas(root, bg="#e9f3f7", highlightthickness=0,
                                bd=0, relief="flat")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.backdrop_id = self.canvas.create_image(0, 0, anchor="nw")
        self.backdrop_photo = None
        self.buttons = []
        self.controls = {}
        self.hovered = None
        self.pressed = None
        self.focused = -1
        self.last_launch = 0.0
        self.render_after = None
        self.animation_after = None
        self.resume_mode = None
        self.drag_origin = None
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Configure>", self._queue_render, add="+")
        self.canvas.bind("<Tab>", self._on_tab)
        self.canvas.bind("<Return>", self._on_keyboard_launch)
        self.canvas.bind("<space>", self._on_keyboard_launch)
        self.root.bind("<F11>", self._toggle_fullscreen, add="+")
        self.root.bind("<Escape>", self._leave_fullscreen, add="+")
        self.root.bind("<Map>", self._on_map, add="+")
        self.canvas.focus_set()
        self._queue_render()

    def _on_map(self, event):
        if (event.widget is self.root and self.resume_mode is not None
                and self.root.state() == "normal"):
            mode, self.resume_mode = self.resume_mode, None
            self.fullscreen = mode == "fullscreen"
            if self.fullscreen:
                self.root.after_idle(lambda: self.root.attributes("-fullscreen", True))
            else:
                self.root.after_idle(self._hide_windowed_titlebar)
            self._queue_render()

    def _hide_windowed_titlebar(self):
        if self.fullscreen or self.root.state() == "iconic":
            return
        if sys.platform == "win32":
            try:
                _remove_windows_caption(self.root)
            except (AttributeError, OSError, ValueError):
                return
        else:
            self.root.overrideredirect(True)

    def _toggle_fullscreen(self, _event=None):
        self.fullscreen = not self.fullscreen
        if self.fullscreen and self.root.overrideredirect():
            self.root.overrideredirect(False)
        self.root.attributes("-fullscreen", self.fullscreen)
        if not self.fullscreen:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            width, height = int(sw * .82), int(sh * .82)
            self.root.geometry(f"{width}x{height}+{(sw-width)//2}+{(sh-height)//2}")
            self._hide_windowed_titlebar()
            self.root.after(80, self._hide_windowed_titlebar)
        self._queue_render()

    def _leave_fullscreen(self, _event=None):
        if self.fullscreen:
            self._toggle_fullscreen()

    def _queue_render(self, event=None):
        if event is not None and event.widget is not self.canvas:
            return
        if self.render_after is not None:
            self.root.after_cancel(self.render_after)
        self.render_after = self.root.after(90, self._render)

    def _frost(self, image, box, radius, veil_alpha):
        left, top, right, bottom = box
        region = image.crop(box).filter(ImageFilter.GaussianBlur(max(5, radius)))
        mask = Image.new("L", region.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, region.width - 1, region.height - 1),
            radius=max(10, radius * 2), fill=255)
        image.paste(region, (left, top), mask)
        veil = Image.new("RGBA", region.size, (246, 251, 254, 0))
        veil.putalpha(mask.point(lambda value: value * veil_alpha // 255))
        image.alpha_composite(veil, (left, top))
        ImageDraw.Draw(image).rounded_rectangle(
            (left, top, right - 1, bottom - 1),
            radius=max(10, radius * 2), outline=(255, 255, 255, 185),
            width=max(1, round(image.height / 900)))

    def _background(self, width, height):
        source_width, source_height = self.source_image.size
        scale = max(width / source_width, height / source_height)
        scaled_width = max(width, math.ceil(source_width * scale))
        scaled_height = max(height, math.ceil(source_height * scale))
        scaled = self.source_image.resize((scaled_width, scaled_height),
                                          Image.Resampling.LANCZOS)
        # Prioritize the pointing hand to the right of the menu on narrow views.
        panel_left = round(.045 * width)
        desired_panel_right = round(.45 * width)
        hand_gap = max(round(.04 * width), round(25 * height / 720))
        finger_scaled_x = .515 * scaled_width
        max_crop_for_hand = max(0, round(finger_scaled_x - desired_panel_right - hand_gap))
        crop_x = min(round(.77 * (scaled_width - width)), max_crop_for_hand)
        crop_y = round(.39 * scaled_height - .39 * height)
        crop_x = max(0, min(scaled_width - width, crop_x))
        crop_y = max(0, min(scaled_height - height, crop_y))
        image = scaled.crop((crop_x, crop_y, crop_x + width, crop_y + height))
        fingertip_x = finger_scaled_x - crop_x
        panel_right = min(desired_panel_right, round(fingertip_x - hand_gap),
                          width - 12)
        panel_right = max(panel_left + round(.30 * width), panel_right)
        panel_top, panel_bottom = round(.08 * height), round(.93 * height)
        blur_radius = max(7, round(height / 90))
        self._frost(image, (panel_left, panel_top, panel_right, panel_bottom),
                    blur_radius, 72)
        unit = height / 720
        size, gap, margin = round(42 * unit), round(8 * unit), round(18 * unit)
        controls_left = width - margin - 3 * size - 2 * gap
        self._frost(image, (controls_left - round(8 * unit),
                            margin - round(7 * unit), width - margin + round(8 * unit),
                            margin + size + round(7 * unit)),
                    max(5, round(5 * unit)), 65)
        return image, (panel_left, panel_top, panel_right, panel_bottom)

    def _render(self):
        self.render_after = None
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 500 or height < 360:
            return
        if self.animation_after is not None:
            self.root.after_cancel(self.animation_after)
            self.animation_after = None
        background, panel = self._background(width, height)
        self.backdrop_photo = ImageTk.PhotoImage(background, master=self.root)
        self.canvas.itemconfigure(self.backdrop_id, image=self.backdrop_photo)
        self.canvas.coords(self.backdrop_id, 0, 0)
        self.canvas.delete("ui")
        self.buttons = []
        self.controls = {}
        self.hovered = None
        unit = height / 720
        left, top, right, bottom = panel
        padding = max(round(22 * unit), round(.027 * width))
        content_left, content_right = left + padding, right - padding
        title_size = min(round(37 * unit),
                         max(20, (content_right - content_left) // 9))
        self.title_font.configure(size=-title_size)
        self.note_font.configure(size=-max(15, round(16 * unit)))
        self.button_font.configure(size=-max(19, round(22 * unit)))
        self.detail_font.configure(size=-max(13, round(14 * unit)))
        self.control_font.configure(size=-max(21, round(24 * unit)))
        self.canvas.create_text(content_left, top + round(69 * unit),
                                text="行星盘沉积仿真", anchor="w", font=self.title_font,
                                fill="#203b4e", tags="ui")
        self.canvas.create_text(content_left, top + round(113 * unit),
                                text="选择模块开始", anchor="w", font=self.note_font,
                                fill="#567384", tags="ui")
        first_top = top + round(146 * unit)
        row_height, gap = round(55 * unit), round(9 * unit)
        show_details = content_right - content_left >= round(420 * unit)
        for index, (title, filename, description) in enumerate(MODULES):
            y0 = first_top + index * (row_height + gap)
            box = (content_left, y0, content_right, y0 + row_height)
            halo = self.canvas.create_rectangle(
                box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3,
                outline="#a1cfdb", width=1, state="hidden", tags="ui")
            outline = self.canvas.create_rectangle(
                *box, outline="#708f9d", width=max(1, round(unit)),
                fill="", tags="ui")
            center_y = (box[1] + box[3]) / 2
            title_x = box[0] + round(19 * unit)
            label = self.canvas.create_text(
                title_x, center_y, text=title, anchor="w", font=self.button_font,
                fill="#244757", tags="ui")
            detail = None
            detail_x = box[0] + round(159 * unit)
            if show_details:
                detail = self.canvas.create_text(
                    detail_x, center_y, text=description, anchor="w",
                    font=self.detail_font, fill="#6a8795", tags="ui")
            self.buttons.append({"box": box, "filename": filename,
                                 "halo": halo, "outline": outline,
                                 "label": label, "detail": detail,
                                 "title_x": title_x, "detail_x": detail_x,
                                 "center_y": center_y, "hover": 0.0,
                                 "target": 0.0, "pulse": 0.0})
        self.canvas.create_text(content_left, bottom - round(24 * unit),
                                text="F11 切换全屏", anchor="w", font=self.detail_font,
                                fill="#6c8a9a", tags="ui")
        self._draw_controls(width, height)
        if (self.canvas.winfo_width(), self.canvas.winfo_height()) != (width, height):
            self._queue_render()

    def _draw_controls(self, width, height):
        unit = height / 720
        size, gap, margin = round(42 * unit), round(8 * unit), round(18 * unit)
        top = margin
        for position, (action, symbol) in enumerate(
                (("minimize", "−"), ("restore", "□"), ("close", "×"))):
            left = width - margin - (3 - position) * size - (2 - position) * gap
            box = (left, top, left + size, top + size)
            self.canvas.create_rectangle(*box, outline="#6b8a99",
                                         width=max(1, round(unit)), fill="", tags="ui")
            self.canvas.create_text((box[0] + box[2]) / 2,
                                    (box[1] + box[3]) / 2, text=symbol,
                                    font=self.control_font, fill="#315365", tags="ui")
            self.controls[action] = box

    def _hit(self, x, y):
        for action, box in self.controls.items():
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                return "control", action
        for index, button in enumerate(self.buttons):
            box = button["box"]
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                return "button", index
        return None

    def _set_hover(self, index):
        if index == self.hovered:
            return
        self.hovered = index
        for button_index, button in enumerate(self.buttons):
            button["target"] = 1.0 if button_index == index else 0.0
        self._schedule_animation()

    def _on_motion(self, event):
        hit = self._hit(event.x, event.y)
        self.canvas.configure(cursor="hand2" if hit else "")
        self._set_hover(hit[1] if hit and hit[0] == "button" else None)

    def _on_double_click(self, event):
        if (event.y < round(self.canvas.winfo_height() * .27)
                and self._hit(event.x, event.y) is None):
            self.drag_origin = None
            self.pressed = None
            self._toggle_fullscreen()
            return "break"

    def _on_leave(self, _event):
        self.canvas.configure(cursor="")
        self._set_hover(None)

    def _on_press(self, event):
        self.pressed = self._hit(event.x, event.y)
        self.drag_origin = None
        if (self.pressed is None and not self.fullscreen and
                event.y < round(self.canvas.winfo_height() * .27)):
            self.drag_origin = (event.x_root - self.root.winfo_x(),
                                event.y_root - self.root.winfo_y())
        if self.pressed and self.pressed[0] == "button":
            button = self.buttons[self.pressed[1]]
            button["pulse"] = 1.0
            self._schedule_animation()

    def _on_drag(self, event):
        if self.drag_origin is not None and not self.fullscreen:
            x = event.x_root - self.drag_origin[0]
            y = event.y_root - self.drag_origin[1]
            if sys.platform == "win32":
                try:
                    _set_window_pos(int(self.root.wm_frame(), 16), x, y,
                                    0x0001 | 0x0004 | 0x0010)
                except (OSError, ValueError):
                    self.drag_origin = None
            else:
                self.root.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _on_release(self, event):
        if self.drag_origin is not None:
            self.drag_origin = None
            self.pressed = None
            return
        pressed, self.pressed = self.pressed, None
        if pressed is None or pressed != self._hit(event.x, event.y):
            return
        if pressed[0] == "button":
            self.focused = pressed[1]
            self.root.after(130, lambda index=pressed[1]: self._launch(index))
        elif pressed[1] == "close":
            self.root.destroy()
        elif pressed[1] == "restore":
            self._toggle_fullscreen()
        elif pressed[1] == "minimize":
            mode = "fullscreen" if self.fullscreen else "windowed"
            if self.fullscreen:
                self.root.attributes("-fullscreen", False)
                self.fullscreen = False
            if self.root.overrideredirect():
                self.root.overrideredirect(False)
            self.root.iconify()
            self.resume_mode = mode

    def _on_tab(self, _event):
        if not self.buttons:
            return "break"
        self.focused = (self.focused + 1) % len(self.buttons)
        self._set_hover(self.focused)
        return "break"

    def _on_keyboard_launch(self, _event):
        if 0 <= self.focused < len(self.buttons):
            self.buttons[self.focused]["pulse"] = 1.0
            self._schedule_animation()
            self.root.after(130, lambda index=self.focused: self._launch(index))
        return "break"

    def _schedule_animation(self):
        if self.animation_after is None:
            self.animation_after = self.root.after(16, self._animate)

    def _animate(self):
        self.animation_after = None
        active = False
        unit = max(1.0, self.canvas.winfo_height() / 720)
        for button in self.buttons:
            button["hover"] += (button["target"] - button["hover"]) * .28
            if abs(button["target"] - button["hover"]) < .015:
                button["hover"] = button["target"]
            pulse = button["pulse"]
            button["pulse"] = max(0.0, pulse - .11)
            amount = min(1.0, button["hover"] + pulse * .65)
            self.canvas.itemconfigure(button["outline"],
                                      outline=_mix("#708f9d", "#148db2", amount),
                                      width=max(1, round(unit * (1 + amount))))
            self.canvas.itemconfigure(button["label"],
                                      fill=_mix("#244757", "#086785", amount))
            if button["detail"] is not None:
                self.canvas.itemconfigure(button["detail"],
                                          fill=_mix("#6a8795", "#278ca8", amount))
            self.canvas.coords(button["label"], button["title_x"],
                               button["center_y"] + round(pulse * 2 * unit))
            if button["detail"] is not None:
                self.canvas.coords(button["detail"], button["detail_x"],
                                   button["center_y"] + round(pulse * 2 * unit))
            self.canvas.itemconfigure(button["halo"],
                                      state="normal" if amount > .04 else "hidden")
            if button["hover"] != button["target"] or button["pulse"] > 0:
                active = True
        if active:
            self._schedule_animation()

    def _launch(self, index):
        now = time.monotonic()
        if now - self.last_launch < .35:
            return
        self.last_launch = now
        script = Path(__file__).with_name(self.buttons[index]["filename"])
        try:
            subprocess.Popen([sys.executable, str(script)], cwd=str(script.parent))
        except OSError as exc:
            messagebox.showerror("无法打开模块", str(exc), parent=self.root)


def main():
    _enable_windows_dpi()
    root = tk.Tk()
    try:
        Launcher(root)
    except (OSError, ValueError) as exc:
        messagebox.showerror("启动器背景加载失败", str(exc), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
