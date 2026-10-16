"""Windows-only real desktop input check for the film titlebar.

Run in an interactive Windows desktop with the application's Qt dependencies:
python tools/verify_film_titlebar_windows.py --output <writable-directory>
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time
import threading
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

if os.name != "nt":
    raise SystemExit("This check requires an interactive Windows desktop.")
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import Qt, QPoint, QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_design import FilmDesignWindow, FilmSimulationWindow
from coat_sim.ui.film_simulation_settings import FilmSimulationSettingsDialog

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]
class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]
class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ctypes.c_ulong), ("data", INPUT_UNION)]

INPUT_MOUSE = 0
MOVE = 0x0001
LEFTDOWN = 0x0002
LEFTUP = 0x0004
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetDoubleClickTime.restype = wintypes.UINT
user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
user32.ClientToScreen.restype = wintypes.BOOL
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
user32.GetLastInputInfo.argtypes = (ctypes.POINTER(LASTINPUTINFO),)
user32.GetLastInputInfo.restype = wintypes.BOOL
_owned_press = threading.Event()
if ctypes.sizeof(INPUT) != (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28):
    raise RuntimeError("INPUT does not match the Windows ABI")

def send(flags, dx=0, dy=0):
    packet = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, 0, flags, 0, 0))
    if user32.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetWindowRect.restype = wintypes.BOOL


def ensure_owned_foreground(hwnd, point=None):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    foreground = user32.GetForegroundWindow()
    # Desktop input is allowed only while this exact owned top-level HWND is foreground.
    if pid.value != os.getpid() or foreground != hwnd:
        raise RuntimeError(f"QA target lost foreground ownership; desktop input aborted: target={hwnd}, target_pid={pid.value}, own_pid={os.getpid()}, foreground={user32.GetForegroundWindow()}")
    if point is not None:
        hit = user32.WindowFromPoint(wintypes.POINT(*point))
        if user32.GetAncestor(hit, 2) != hwnd:
            raise RuntimeError("QA coordinates do not hit the owned target; desktop input aborted")


def run_input(app, hwnd, sequence):
    """Keep release events flowing while Qt is inside a Windows system-move loop."""
    errors = []
    def worker():
        try:
            for action, x, y, delay in sequence:
                ensure_owned_foreground(hwnd, (x, y) if action == "down" else None)
                if action == "move":
                    if not user32.SetCursorPos(x, y):
                        raise ctypes.WinError(ctypes.get_last_error())
                elif action == "down":
                    send(LEFTDOWN)
                    _owned_press.set()
                elif action == "up":
                    send(LEFTUP)
                    _owned_press.clear()
                time.sleep(delay)
        except BaseException as exc:
            errors.append(exc)
        finally:
            # Release only the press issued by this QA worker, even after focus loss.
            if _owned_press.is_set():
                send(LEFTUP)
                _owned_press.clear()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        app.processEvents()
        time.sleep(.005)
    if errors:
        raise errors[0]
    pump(app, .65)

def rect(hwnd):
    value = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (value.left, value.top, value.right, value.bottom)

def pump(app, seconds=.15):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(.01)

def click(app, hwnd, x, y):
    run_input(app, hwnd, [("move", x, y, .04), ("down", x, y, .05), ("up", x, y, .08)])

def drag(app, hwnd, x, y):
    sequence = [("move", x, y, .04), ("down", x, y, .12)]
    sequence.extend(("move", x + 12 * i, y + 8 * i, .04) for i in range(1, 11))
    sequence.append(("up", x + 120, y + 80, .15))
    run_input(app, hwnd, sequence)

def double_click(app, hwnd, x, y):
    delay = max(.06, min(.14, user32.GetDoubleClickTime() / 4000.0))
    run_input(app, hwnd, [("move", x, y, .04), ("down", x, y, .03),
                         ("up", x, y, delay), ("down", x, y, .03), ("up", x, y, .25)])


def title_point(window):
    chrome = window._titlebar_widget
    drag_area = chrome.rootObject().findChild(QObject, "windowChromeTitleDrag")
    local = QPoint(int(drag_area.property("x") + drag_area.property("width") * .45),
                   int(drag_area.property("y") + drag_area.property("height") * .5))
    local = chrome.mapTo(window, local)
    ratio = window.devicePixelRatioF()
    point = wintypes.POINT(round(local.x() * ratio), round(local.y() * ratio))
    if not user32.ClientToScreen(int(window.winId()), ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


def real_zns_design(service):
    zns = next(row for row in service.material_options(purpose="film")
               if row["id"] == "builtin_zns_literature_20261004")
    dataset = next(row for row in zns["optical_sets"] if row["id"] == "ri_zns_amotchkina_n")
    substrate = next(row for row in service.material_options(purpose="substrate")
                     if row["id"] == "film_substrate_schott_nbk7")
    design = service.new_design()
    design["name"] = "ZnS 中文图示本机验收"
    design["use_k"] = False  # Explicit QA calculation assumption, recorded in the result.
    design["substrate"].update(material_id=substrate["id"],
                               optical_set_id=substrate["optical_sets"][0]["id"],
                               mode="semi_infinite", sides="single")
    design["front_layers"] = [{"id":"owned_qa_zns_layer","material_id":zns["id"],
        "optical_set_id":dataset["id"],"thickness":{"value":1.008,"unit":"um"},
        "note":"Actual source data; QA explicitly disables unknown k, without modifying materials."}]
    return service.validate(design)


def capture_content(app, window, output):
    evidence = {}
    if isinstance(window, FilmDesignWindow):
        buttons = [button.text() for button in window.findChildren(QPushButton)]
        if any("导入" in label and "材料" in label for label in buttons):
            raise AssertionError("Designer still has a material-library import button")
        evidence["designer_buttons"] = buttons
        QTest.mouseClick(window.schematic_button, Qt.LeftButton)
        pump(app, .3)
        dialog = window._schematic_dialog
        if dialog is None or not dialog.isVisible():
            raise AssertionError("Real schematic button click did not open the dialog")
        figure = window._schematic_figure
        texts = [artist.get_text() for artist in [*figure.axes[0].texts, figure.axes[0].title]]
        if not any("ZnS" in text and "仅n" in text for text in texts):
            raise AssertionError("Schematic does not contain the real Chinese ZnS dataset")
        from coat_sim.ui.film_design import _film_cjk_font
        from matplotlib.ft2font import FT2Font
        glyphs = FT2Font(str(_film_cjk_font)).get_charmap()
        missing = sorted({c for text in texts for c in text if not c.isspace() and ord(c) not in glyphs})
        if missing:
            raise AssertionError(f"Real schematic has unsupported characters: {missing}")
        figure.savefig(output / "真实ZnS结构示意.png", dpi=160)
        dialog.grab().save(str(output / "真实ZnS示意窗口.png"), "PNG")
        evidence.update(schematic_texts=texts, missing_glyphs=missing, cjk_font=str(_film_cjk_font))
        dialog.close()
    else:
        result = window.calculate()
        evidence["extrapolation_policy"] = result["settings"]["extrapolation_policy"]
        if evidence["extrapolation_policy"] != "endpoint_hold":
            raise AssertionError("Simulation default is not endpoint_hold")
        if not any(row["evaluation"]["k"]["known"] is False for row in result["provenance"]):
            raise AssertionError("Real ZnS unknown k was lost")
        window.figure.savefig(output / "真实ZnS结果图.png", dpi=160)
        window.service.export_csv(output / "真实ZnS结果.csv", result)
        (output / "真实ZnS结果.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        window.grab().save(str(output / "仿真计算内容.png"), "PNG")
        dialog = FilmSimulationSettingsDialog(window.settings(), parent=window)
        dialog.show()
        pump(app, .3)
        dialog.grab().save(str(output / "中文仿真参数.png"), "PNG")
        evidence["settings_values"] = dialog.values()
        evidence["settings_title"] = dialog.windowTitle()
        dialog.close()
    return evidence

def verify_window(app, window, expected_title, output):
    last_input = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(last_input)):
        raise ctypes.WinError(ctypes.get_last_error())
    started_input_time = last_input.dwTime
    window.setGeometry(140, 130, 1080, 720)
    window.showNormal()
    window.show()
    window.raise_(); window.activateWindow(); window.windowHandle().requestActivate()
    pump(app, .5)
    hwnd = int(window.winId())
    if not QTest.qWaitForWindowActive(window.windowHandle(), 2000):
        raise AssertionError("Owned film window did not become active; no input was sent")
    user32.GetLastInputInfo(ctypes.byref(last_input))
    if last_input.dwTime != started_input_time:
        raise RuntimeError("Human desktop input changed during QA activation; no input was sent")
    user32.SetForegroundWindow(hwnd)
    ensure_owned_foreground(hwnd)
    if window.windowTitle() != expected_title:
        raise AssertionError(f"Unexpected taskbar/window title: {window.windowTitle()!r}")

    before = rect(hwnd)
    start_x, start_y = title_point(window)
    drag(app, hwnd, start_x, start_y)
    moved = rect(hwnd)
    if abs(moved[0] - before[0]) < 25 or abs(moved[1] - before[1]) < 15:
        raise AssertionError(f"SendInput titlebar drag did not move {expected_title}: {before} -> {moved}")

    title_x, title_y = title_point(window)
    double_click(app, hwnd, title_x, title_y)
    maximized = window.isMaximized()
    if not maximized:
        raise AssertionError(f"First real titlebar double-click did not maximize {expected_title}")
    maximized_rect = rect(hwnd)
    # Maximizing changes both the client origin and width. Never reuse old coordinates.
    restore_x, restore_y = title_point(window)
    double_click(app, hwnd, restore_x, restore_y)
    restored = not window.isMaximized()
    if not restored:
        raise AssertionError(f"Second real titlebar double-click did not restore {expected_title}")
    restored_rect = rect(hwnd)
    pump(app, .25)

    palette = window._titlebar_host.palette
    state = "active" if window._titlebar_host.active else "inactive"
    chrome = window._titlebar_widget.rootObject()
    ink = QColor(chrome.property("ink")).name(QColor.NameFormat.HexArgb)
    expected_ink = palette[state]["windowText"]
    if ink.casefold() != expected_ink.casefold():
        raise AssertionError(f"Title text color {ink} differs from {state} QPalette {expected_ink}")

    screenshot = output / (expected_title + ".png")
    screen = app.primaryScreen()
    if screen is None or not screen.grabWindow(hwnd).save(str(screenshot), "PNG"):
        raise AssertionError(f"Could not capture owned QA window to {screenshot}")
    return {"title": expected_title, "hwnd": hwnd, "pid": os.getpid(),
            "input_mode": "Windows SendInput", "input_abi_bytes": ctypes.sizeof(INPUT),
            "before_rect": before, "after_drag_rect": moved,
            "drag_delta": [moved[0]-before[0], moved[1]-before[1]],
            "drag_start": [start_x, start_y], "maximize_point": [title_x, title_y],
            "restore_point": [restore_x, restore_y], "maximized_rect": maximized_rect,
            "restored_rect": restored_rect,
            "double_click_transitions": [maximized, restored],
            "palette_state": state, "title_text": ink, "palette_window_text": expected_ink,
            "screenshot": str(screenshot)}


def verify_targeted_qt_window(app, window, expected_title, output):
    """Visible Windows rendering and targeted Qt mouse events; no global input."""
    window.setGeometry(140, 130, 1080, 720)
    window.showNormal()
    pump(app, .3)
    if window.windowTitle() != expected_title:
        raise AssertionError(f"Unexpected actual window title: {window.windowTitle()}")
    bar = window._titlebar_widget
    QTest.mouseDClick(bar, Qt.LeftButton, pos=QPoint(120, 18))
    pump(app, .15)
    maximized = window.isMaximized()
    QTest.mouseDClick(bar, Qt.LeftButton, pos=QPoint(120, 18))
    pump(app, .15)
    restored = not window.isMaximized()
    if not maximized or not restored:
        raise AssertionError("Targeted Qt titlebar double-click did not toggle max/restore")
    host = window._titlebar_host
    state = "active" if host.active else "inactive"
    color = QColor(bar.rootObject().property("ink")).name(QColor.HexArgb)
    if color.casefold() != host.palette[state]["windowText"].casefold():
        raise AssertionError("Targeted Qt titlebar palette does not match the host")
    path = output / (expected_title + ".png")
    if not window.grab().save(str(path), "PNG"):
        raise AssertionError("Owned window screenshot failed")
    return {"title":expected_title,"pid":os.getpid(),"hwnd":int(window.winId()),
            "input_mode":"QTest mouse events addressed to owned Qt widgets",
            "native_drag_verified":False,"double_click_transitions":[maximized,restored],
            "palette_state":state,"title_text":color,
            "palette_window_text":host.palette[state]["windowText"],"screenshot":str(path)}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path.cwd() / "artifacts" / "film_titlebar_native")
    parser.add_argument("--timeout", type=float, default=120,
                        help="Bound this independent QA process only")
    parser.add_argument("--visual-only", action="store_true",
                        help="Use only targeted Qt mouse events and screenshots, without global input")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if app.platformName().lower() in ("offscreen", "minimal"):
        raise SystemExit("A visible Windows platform plugin is required; offscreen runs do not count.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    def timed_out():
        if _owned_press.is_set():
            send(LEFTUP)
        (output / "timeout.json").write_text(json.dumps({"pid":os.getpid(),"timeout_seconds":args.timeout}), encoding="utf-8")
        os._exit(124)
    watchdog = threading.Timer(args.timeout, timed_out)
    watchdog.daemon = True
    watchdog.start()
    service = FilmDesignService(Path(__file__).resolve().parents[1])
    results = []
    windows = [
        (FilmDesignWindow(service=service), "设计"),
        (FilmSimulationWindow(service=service), "仿真计算"),
    ]
    sample = real_zns_design(service)
    for window, _ in windows:
        window.set_design(sample)
    errors = []
    try:
        for window, title in windows:
            try:
                verify = verify_targeted_qt_window if args.visual_only else verify_window
                result = verify(app, window, title, output)
                result["content"] = capture_content(app, window, output)
                results.append(result)
            except BaseException:
                errors.append({"title":title,"traceback":traceback.format_exc()})
                window.grab().save(str(output / (title + "失败.png")), "PNG")
                break  # Focus loss must abort the batch, not activate another target.
            finally:
                window._force_close = True
                window.close()
                pump(app, .2)
    finally:
        watchdog.cancel()
        for window, _ in windows:
            window._force_close = True
            if window.isVisible():
                window.close()
    import PySide6, matplotlib
    report = {"platform": app.platformName(), "sys_executable":sys.executable,
              "PySide6":PySide6.__version__, "matplotlib":matplotlib.__version__,
              "visual_only":args.visual_only,"native_input_verified":not args.visual_only and not errors and len(results)==2,
              "passed":not errors and len(results)==2, "results": results, "errors":errors}
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
