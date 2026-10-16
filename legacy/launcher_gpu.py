"""Qt Quick launcher; each design module remains an independent process."""

import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
BACKGROUND = ROOT / "assert" / "launcher.png"
QML_FILE = ROOT / "launcher.qml"
MODULES = (
    ("膜厚仿真", "app.py", "选择工艺并计算膜厚"),
    ("工艺", "process_designer.py", "安排镀膜步骤"),
    ("机器", "machine_designer.py", "配置机器结构"),
    ("蒸发源", "source_designer.py", "设计蒸发源"),
    ("基片", "substrate_designer.py", "设计基片"),
    ("修正板", "mask_designer.py", "绘制修正板"),
)


def _fatal(message):
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, message, "行星盘沉积仿真 · 启动器", 0x10)
        except (AttributeError, OSError):
            pass


def main():
    software_mode = "--software" in sys.argv
    graphics_info = "--graphics-info" in sys.argv
    if software_mode:
        # Qt Quick's raster adaptation is a last-resort fallback for bad drivers.
        os.environ["QT_QUICK_BACKEND"] = "software"
    if graphics_info:
        os.environ["QSG_INFO"] = "1"
    qt_args = [arg for arg in sys.argv
               if arg not in ("--software", "--graphics-info")]

    try:
        from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
        from PySide6.QtGui import QGuiApplication, QImageReader
        from PySide6.QtQml import QQmlApplicationEngine
    except (ImportError, OSError) as exc:
        _fatal("GPU 启动器需要 PySide6-Essentials 6.8 或更新版本。\n"
               "请在本项目的 Python 环境安装：\n"
               "python -m pip install 'PySide6-Essentials>=6.8,<7'\n\n"
               f"详细错误：{exc}")
        return 2

    if not BACKGROUND.is_file() or not QML_FILE.is_file():
        _fatal("GPU 启动器缺少背景图或界面文件：\n"
               f"{BACKGROUND}\n{QML_FILE}")
        return 2

    reader = QImageReader(str(BACKGROUND))
    background_size = reader.size()
    if not background_size.isValid():
        _fatal(f"无法读取启动器背景图：{BACKGROUND}\n{reader.errorString()}")
        return 2

    class LauncherBridge(QObject):
        launchError = Signal(str)

        def __init__(self):
            super().__init__()
            self._last_launch = 0.0

        @Property("QVariantList", constant=True)
        def modules(self):
            return [{"title": title, "detail": detail}
                    for title, _filename, detail in MODULES]

        @Property(QUrl, constant=True)
        def backgroundUrl(self):
            return QUrl.fromLocalFile(str(BACKGROUND))

        @Property(int, constant=True)
        def backgroundWidth(self):
            return background_size.width()

        @Property(int, constant=True)
        def backgroundHeight(self):
            return background_size.height()

        @Property(bool, constant=True)
        def softwareMode(self):
            return software_mode

        @Slot(int)
        def launch(self, index):
            if not 0 <= index < len(MODULES):
                return
            now = time.monotonic()
            if now - self._last_launch < 0.35:
                return
            self._last_launch = now
            script = ROOT / MODULES[index][1]
            if not script.is_file():
                self.launchError.emit(f"找不到模块：{script}")
                return
            try:
                subprocess.Popen([sys.executable, str(script)], cwd=str(ROOT))
            except OSError as exc:
                self.launchError.emit(f"无法打开{MODULES[index][0]}：{exc}")

    app = QGuiApplication(qt_args)
    bridge = LauncherBridge()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("launcher", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_FILE)))
    if not engine.rootObjects():
        _fatal("GPU 启动器界面未能加载。请检查 PySide6-Essentials 版本，"
               "并在终端使用 --graphics-info 查看 Qt 的图形后端。")
        return 2
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
