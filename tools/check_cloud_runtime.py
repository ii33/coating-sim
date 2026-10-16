"""Offline startup dependency checks. Never start Qt, import the app, or read keys."""
from __future__ import annotations

import importlib
from importlib import metadata
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.version_info < (3, 10):
        print("错误：需要 Python 3.10+；Linux 部署建议 Python 3.12。")
        return 2
    try:
        from packaging.requirements import Requirement
    except ImportError:
        print("错误：缺少 packaging（Matplotlib 的依赖）；请先安装 requirements.txt。")
        return 2

    failures = []
    warnings = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            version = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            if requirement.name.lower().replace("_", "-") == "pyside6-addons":
                warnings.append("未安装 PySide6-Addons：气泡提示音降级为系统提示音，其他功能可继续启动。")
                continue
            failures.append(f"未安装：{requirement.name}")
            continue
        if not requirement.specifier.contains(version):
            failures.append(f"版本不符合要求：{requirement.name} {version}，需要 {requirement.specifier}")

    # Import native libraries to catch missing Qt/system libraries as well as wheels.
    # Do not initialize QGuiApplication, keyring backends, or assistant network clients.
    # Matplotlib/ezdxf imports can create user caches; their versions are checked
    # above without importing them during this noninteractive preflight.
    for module in ("numpy", "PIL.Image", "PySide6.QtCore",
                   "PySide6.QtGui", "PySide6.QtQml", "PySide6.QtQuick",
                   "PySide6.QtSvg", "PySide6.QtNetwork"):
        try:
            importlib.import_module(module)
        except (ImportError, OSError) as exc:
            failures.append(f"无法导入 {module}：{exc}")
    for warning in warnings:
        print("警告：" + warning)
    if failures:
        for failure in failures:
            print("错误：" + failure)
        print("Python 依赖：请在项目环境运行 python -m pip install -r requirements.txt。")
        print("共享库/Qt 插件依赖：请参照 README 的 Linux 系统库与故障排查。")
        return 2
    print(f"Python {sys.version.split()[0]}：必要依赖版本与主要本地库导入检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
