"""Install the trusted private Python environment used by the LPAC runner.

Run with the project's existing Python. This copies only its standard runtime,
creates an isolated venv, and installs pinned pure-Python visualization wheels.
It never executes assistant-generated scripts and never grants sandbox ACLs.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "artifacts" / "sandbox-runtime"


def reject_reparse(path: Path):
    current = path.absolute()
    while current != current.parent:
        if current.exists() and (getattr(current.lstat(), "st_file_attributes", 0) & 0x400 or current.is_symlink()):
            raise RuntimeError("安装路径不允许符号链接或目录联接")
        current = current.parent


def pe_imports(path: Path):
    """Read direct PE imports without loading native code."""
    data = path.read_bytes()
    if data[:2] != b"MZ" or len(data) < 64:
        return []
    header = struct.unpack_from("<I", data, 60)[0]
    if data[header:header + 4] != b"PE\0\0":
        return []
    section_count = struct.unpack_from("<H", data, header + 6)[0]
    optional_size = struct.unpack_from("<H", data, header + 20)[0]
    optional = header + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    directory = optional + (112 if magic == 0x20B else 96)
    import_rva = struct.unpack_from("<I", data, directory + 8)[0]
    sections = []
    for index in range(section_count):
        offset = optional + optional_size + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, offset + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))
    def physical(rva):
        for start, length, raw in sections:
            if start <= rva < start + length:
                return raw + rva - start
        raise ValueError("PE RVA not in section")
    if not import_rva:
        return []
    cursor = physical(import_rva)
    results = []
    while cursor + 20 <= len(data):
        descriptor = struct.unpack_from("<IIIII", data, cursor)
        if not any(descriptor):
            break
        position = physical(descriptor[3])
        end = data.index(b"\0", position)
        results.append(data[position:end].decode("ascii").lower())
        cursor += 20
    return results


def copy_runtime(source: Path, target: Path):
    target.mkdir()
    ignore = shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "test", "tests", "idlelib", "tkinter", "turtledemo")
    shutil.copytree(source / "Lib", target / "Lib", ignore=ignore)
    if (source / "DLLs").is_dir():
        shutil.copytree(source / "DLLs", target / "DLLs", ignore=ignore)
    for path in source.glob("*.dll"):
        shutil.copyfile(path, target / path.name)
    for name in ("python.exe", "pythonw.exe"):
        shutil.copyfile(source / name, target / name)
    if (source / "LICENSE.txt").is_file():
        shutil.copyfile(source / "LICENSE.txt", target / "LICENSE.txt")
    search_directories = [source, source / "DLLs", source / "Library" / "bin"]
    available = {path.name.lower(): path for folder in search_directories if folder.exists()
                 for path in folder.iterdir() if path.suffix.lower() == ".dll"}
    queue = list(target.glob("*.exe")) + list(target.rglob("*.dll")) + list(target.rglob("*.pyd"))
    seen = set()
    while queue:
        binary = queue.pop()
        if binary in seen:
            continue
        seen.add(binary)
        for name in pe_imports(binary):
            dependency = available.get(name)
            if dependency is None or (target / dependency.name).exists():
                continue
            copied = target / dependency.name
            shutil.copyfile(dependency, copied)
            queue.append(copied)
    # Conda sitecustomize and pth can refer to the original environment.
    for name in ("sitecustomize.py", "usercustomize.py"):
        (target / "Lib" / name).unlink(missing_ok=True)
    for pth in (target / "Lib").glob("*.pth"):
        pth.unlink()
    return target / "python.exe"


def install_wheels(wheelhouse: Path, site_packages: Path):
    """Pure wheel installation; reject scripts/native wheels and zip traversal."""
    site_packages.mkdir(parents=True, exist_ok=True)
    packages = []
    for wheel in sorted(wheelhouse.glob("*.whl")):
        if not wheel.name.endswith("-none-any.whl"):
            raise RuntimeError("可视化环境只安装纯 Python wheel：" + wheel.name)
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                parts = Path(info.filename).parts
                if Path(info.filename).is_absolute() or ".." in parts:
                    raise RuntimeError("wheel 含不安全路径")
                relative = Path(info.filename)
                if parts[0].endswith(".data"):
                    if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                        # CLI scripts, headers, docs are unnecessary for charts.
                        # Never execute package entry points during installation.
                        continue
                    relative = Path(*parts[2:])
                target = site_packages / relative
                if not target.resolve().is_relative_to(site_packages.resolve()):
                    raise RuntimeError("wheel 路径越界")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
            packages.append(wheel.name)
    if not packages:
        raise RuntimeError("没有下载到依赖 wheel")
    return packages


def setup():
    if os.name != "nt":
        raise RuntimeError("此私有环境仅用于 Windows LPAC；其他系统禁止脚本降级")
    reject_reparse(DESTINATION)
    if (DESTINATION / "runtime.json").is_file():
        # A marker alone cannot establish that the interpreter or its modules
        # survived a move/partial installation. This check never starts code.
        required = [DESTINATION / "base" / "python.exe",
                    DESTINATION / "base" / "Lib" / "encodings" / "__init__.py",
                    *(DESTINATION / "venv" / "Lib" / "site-packages" / package / "__init__.py"
                      for package in ("pygal", "importlib_metadata", "zipp"))]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("私有运行时缺少文件：" + "; ".join(missing))
        print("私有 Python 文件已经就绪（LPAC 启动由任务调用时校验）：" + str(DESTINATION))
        return
    if DESTINATION.exists():
        raise RuntimeError("发现不完整环境，请确认后手动移除 artifacts/sandbox-runtime 再安装")
    build = ROOT / "artifacts" / ("sandbox-build-" + uuid.uuid4().hex)
    build.mkdir(parents=True)
    try:
        print("复制独立 Python 标准运行环境…", flush=True)
        base_python = copy_runtime(Path(sys.base_prefix), build / "base")
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith("PYTHON"):
                environment.pop(key, None)
        print("建立内部 Python 虚拟环境…", flush=True)
        subprocess.run([str(base_python), "-I", "-m", "venv", "--copies", "--without-pip", str(build / "venv")],
                       check=True, env=environment, cwd=build)
        wheelhouse = build / "wheels"
        wheelhouse.mkdir()
        print("安装轻量离线 SVG 图表库 Pygal…", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
                        "--dest", str(wheelhouse), "-r", str(ROOT / "requirements-sandbox.txt")],
                       check=True, env=environment, cwd=build)
        packages = install_wheels(wheelhouse, build / "venv" / "Lib" / "site-packages")
        # Rewrite the base path before renaming the build to its final location.
        configuration = build / "venv" / "pyvenv.cfg"
        lines = configuration.read_text(encoding="utf-8").splitlines()
        final_base = DESTINATION / "base"
        lines = ["home = " + str(final_base) if line.startswith("home =") else line
                 for line in lines]
        lines = ["executable = " + str(final_base / "python.exe") if line.startswith("executable =") else line
                 for line in lines]
        lines = ["command = private trusted setup (no generated scripts)" if line.startswith("command =") else line
                 for line in lines]
        configuration.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (build / "runtime.json").write_text(json.dumps({
            "python": sys.version.split()[0], "source": "trusted project CPython standard runtime copy",
            "packages": packages, "isolation": "Windows LPAC + Job Object", "network": False,
            "setup_only": True, "schema": 2,
            "launch_method": "private-interpreter-single-process",
            "executable": "base/python.exe", "site_packages": "venv/Lib/site-packages",
            "site_initialization": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        build.rename(DESTINATION)
        print("安装完成：" + str(DESTINATION), flush=True)
    except BaseException:
        # build is a freshly generated and verified child of artifacts.
        if build.exists() and build.resolve().is_relative_to((ROOT / "artifacts").resolve()):
            shutil.rmtree(build)
        raise


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    setup()
