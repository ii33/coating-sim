"""Explicitly authorized data inputs, OS sandbox execution, safe output export.

Python source is untrusted, unrestricted within its LPAC workspace, and never
executed in the assistant process. AST/import restrictions are not isolation.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import uuid
import xml.etree.ElementTree as ET

from coat_sim.core.data_files import DATA_ROOT, PROJECT_ROOT, data_path, file_info
from coat_sim.agent.windows_lpac import IsolationUnavailable, RUNTIME_CAPABILITIES, run_isolated

RUNTIME_ROOT = PROJECT_ROOT / "artifacts" / "sandbox-runtime"
WORK_ROOT = PROJECT_ROOT / "artifacts" / "sandbox-work"
MAX_CODE_BYTES = 128 * 1024
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_FILES = 40
MAX_INPUT_FILES = 30
_RUNTIME_LOCK = threading.Lock()


def _no_reparse(path: Path, *, boundary: Path):
    """Check before resolve, and reject links/junctions at every component."""
    absolute = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(boundary))
    if not absolute.is_relative_to(boundary):
        raise ValueError("路径不在指定目录中")
    for component in absolute.relative_to(boundary).parts:
        stem = component.split(".", 1)[0].upper()
        if (component.endswith((" ", ".")) or ":" in component
                or stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
            raise ValueError("沙箱拒绝 Windows 设备名、备用流及路径规范化歧义")
    current = absolute
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("沙箱不接受符号链接、目录联接或其他重解析点")
        if current == boundary:
            break
        current = current.parent
    return absolute


def _authorized_input(value: str) -> Path:
    target = data_path(value, root_allowed=False)
    original = Path(value)
    if not original.is_absolute():
        original = (PROJECT_ROOT if original.parts and original.parts[0].lower() == "data" else DATA_ROOT) / original
    _no_reparse(original, boundary=DATA_ROOT)
    if not target.is_file() or target.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("脚本输入须是 data 内不超过 32 MiB 的普通文件")
    return target


def _runtime_readiness() -> dict:
    """Inspect trusted runtime files without starting Python or changing ACLs.

    Windows venv executables are redirectors which create a second process.
    LPAC permits exactly one process, so the real private interpreter is the
    launch target and the trusted bootstrap selects the venv packages itself.
    """
    marker = RUNTIME_ROOT / "runtime.json"
    executable = RUNTIME_ROOT / "base" / "python.exe"
    packages = RUNTIME_ROOT / "venv" / "Lib" / "site-packages"
    checks = {
        "metadata": marker,
        "interpreter": executable,
        "standard_library": RUNTIME_ROOT / "base" / "Lib" / "encodings" / "__init__.py",
        "pygal": packages / "pygal" / "__init__.py",
        "importlib_metadata": packages / "importlib_metadata" / "__init__.py",
        "zipp": packages / "zipp" / "__init__.py",
    }
    errors = []
    for name, path in checks.items():
        try:
            _no_reparse(path, boundary=PROJECT_ROOT)
            if not path.is_file():
                errors.append({"check": name, "path": str(path), "reason": "文件不存在"})
        except (ValueError, OSError) as exc:
            errors.append({"check": name, "path": str(path), "reason": str(exc)})
    metadata = {}
    if not errors:
        try:
            if marker.stat().st_size > 64 * 1024:
                raise ValueError("运行时元数据超过 64 KiB")
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("运行时元数据须为对象")
            match = re.fullmatch(r"(3)\.(\d+)\.\d+(?:[a-zA-Z0-9.+-]*)?", str(metadata.get("python", "")))
            if match is None:
                raise ValueError("运行时元数据中的 Python 版本无效")
            library = executable.parent / f"python{match[1]}{match[2]}.dll"
            _no_reparse(library, boundary=PROJECT_ROOT)
            if not library.is_file():
                errors.append({"check": "python_library", "path": str(library), "reason": "解释器 DLL 不存在"})
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            metadata = {}
            errors.append({"check": "metadata", "path": str(marker), "reason": str(exc)})
    return {
        "ready": not errors, "errors": errors, "executable": str(executable),
        "site_packages": str(packages), "python": metadata.get("python"),
        "arguments": ["-I", "-S", "-B", "-X", "utf8"],
    }


def sandbox_status() -> dict:
    layout = _runtime_readiness()
    available = os.name == "nt" and layout["ready"]
    reason = ("私有文件结构检查通过；LPAC 启动尚需操作系统校验" if available else
              "此平台没有 Windows LPAC；禁止普通 Python 回退" if os.name != "nt" else
              "私有运行时文件检查失败：" + "; ".join(item["path"] + "（" + item["reason"] + "）" for item in layout["errors"]))
    return {
        "available": available, "reason": reason, "stage": "runtime_files",
        "launch_verified": False, "runtime_checks": layout,
        "isolation": "Windows LPAC + Job Object", "network": False,
        "os_capabilities": list(RUNTIME_CAPABILITIES), "registry_access": "HKLM read for OS initialization",
        "runtime": str(RUNTIME_ROOT), "setup": "python tools/setup_sandbox.py",
        "visualization": {
            "library": "Pygal + coat_plot (pure Python, offline SVG)",
            "chart_types": ["line", "bar", "horizontal_bar", "xy", "pie", "radar", "box", "histogram"],
            "scientific_apis": ["heatmap", "contour", "radial_profile", "profile_chart"],
            "grid": "x[N], y[M], values[M][N]; rows=y, columns=x; None/nonfinite values masked",
            "limits": {"grid_points": 65536, "contour_levels": 32, "contour_segments": 100000, "radial_bins": 2000,
                       "svg_bytes": 8 * 1024 * 1024, "svg_elements": 100000},
            "examples": ["save_chart(heatmap(x,y,values), 'heatmap.svg')",
                         "save_chart(contour(x,y,values,levels=8), 'contours.svg')",
                         "rows=radial_profile(x,y,values,bin_width=5); save_chart(profile_chart(rows), 'radial.svg')"],
        },
        "limits": {"timeout_s": [1, 300], "memory_mb": 512, "processes": 1,
                   "input_files": MAX_INPUT_FILES, "input_total_mb": 32,
                   "output_files": MAX_OUTPUT_FILES, "output_total_mb": 32, "stdout_total_mb": 1},
        "instructions": "脚本通过 INPUTS（Path 列表）、INPUT_FILES（data 引用到 Path 的字典）读取显式输入；输出写 OUTPUT_DIR。from coat_plot import chart, heatmap, contour, radial_profile, profile_chart, save_chart；from coat_plot import capabilities 可查实际API。热图和等高线输入x/y一维坐标、values[y][x]，径向结果为radius/min/mean/max/count行。无需pygal.Heatmap或额外安装。available仅表示固定私有文件就绪，不表示已验证LPAC启动；启动错误保留阶段与Windows错误。",
    }


def _safe_svg(payload: bytes) -> bytes:
    """Restrictive passive SVG export: no scripts, embedded media, or URLs."""
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("SVG 超过 8 MiB")
    text = payload.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("SVG 不允许 DTD 或实体声明")
    root = ET.fromstring(text)
    allowed_tags = {"svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
                    "text", "tspan", "title", "desc", "style", "clipPath", "linearGradient", "radialGradient", "stop"}
    allowed_attrs = {"id", "class", "viewBox", "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy",
                     "r", "rx", "ry", "d", "points", "transform", "fill", "stroke", "stroke-width", "opacity",
                     "fill-opacity", "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-dasharray",
                     "font-family", "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline",
                     "alignment-baseline", "dx", "dy", "rotate", "preserveAspectRatio", "clip-path", "offset",
                     "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "style"}
    def local(value):
        return value.rsplit("}", 1)[-1]
    if local(root.tag) != "svg":
        raise ValueError("文件不是 SVG")
    count = 0
    for parent in root.iter():
        count += 1
        if count > 100000:
            raise ValueError("SVG 元素过多")
        for child in list(parent):
            if local(child.tag) not in allowed_tags:
                parent.remove(child)
        for key, value in list(parent.attrib.items()):
            name = local(key)
            if name not in allowed_attrs or chr(92) in value or re.search(r"javascript:|data:|file:|https?:|//|@import|expression\s*\(", value, re.I):
                del parent.attrib[key]
                continue
            if "url(" in value.lower() and re.search(r"url\(\s*['\"]?(?!#)", value, re.I):
                del parent.attrib[key]
        if local(parent.tag) == "style":
            css = parent.text or ""
            if chr(92) in css or re.search(r"@import|javascript:|data:|file:|https?:|//|expression\s*\(", css, re.I):
                parent.text = ""
            elif re.search(r"url\(\s*['\"]?(?!#)", css, re.I):
                parent.text = ""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _collect_outputs(staging: Path, destination: Path) -> tuple[list[dict], list[str]]:
    root = staging / "outputs"
    _no_reparse(root, boundary=staging)
    outputs, warnings = [], []
    total = 0
    allowed = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".csv", ".tsv", ".json", ".txt", ".md", ".pdf"}
    candidates = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            child = Path(directory) / name
            try:
                _no_reparse(child, boundary=staging)
            except ValueError:
                dirs.remove(name)
                warnings.append("忽略重解析目录：" + name)
        for name in names:
            path = Path(directory) / name
            try:
                _no_reparse(path, boundary=staging)
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError("不是普通文件")
                if path.suffix.lower() not in allowed:
                    warnings.append("未导出此文件类型：" + name)
                    continue
                size = path.stat().st_size
                if size > MAX_OUTPUT_BYTES or total + size > MAX_OUTPUT_BYTES or len(candidates) >= MAX_OUTPUT_FILES:
                    raise ValueError("输出超过数量或尺寸限制")
                total += size
                candidates.append(path)
            except (ValueError, OSError) as exc:
                warnings.append(f"忽略输出 {name}：{exc}")
    for path in sorted(candidates):
        try:
            target = destination / path.relative_to(root)
            _no_reparse(target, boundary=DATA_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = path.read_bytes()
            if path.suffix.lower() == ".svg":
                payload = _safe_svg(payload)
            target.write_bytes(payload)
            outputs.append(file_info(target))
        except (ValueError, OSError, ET.ParseError) as exc:
            warnings.append(f"未导出 {path.name}：{exc}")
    return outputs, warnings


def _run(code: str, files: list[str], timeout_s: float, cancel: threading.Event) -> dict:
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("脚本须为非空、最多 128 KiB 的 Python 源码")
    if not isinstance(files, list) or len(files) > MAX_INPUT_FILES or any(not isinstance(value, str) for value in files):
        raise ValueError("files 须为最多 30 个 data 文件路径")
    if isinstance(timeout_s, bool) or not 1 <= float(timeout_s) <= 300:
        raise ValueError("脚本时限须为 1～300 秒")
    status = sandbox_status()
    if not status["available"]:
        raise IsolationUnavailable(status["reason"] + "。可用 tools/setup_sandbox.py 准备可信运行时；禁止普通 Python 降级执行")
    inputs = [_authorized_input(value) for value in files]
    if sum(path.stat().st_size for path in inputs) > MAX_INPUT_BYTES:
        raise ValueError("全部脚本输入合计不能超过 32 MiB")
    _no_reparse(RUNTIME_ROOT, boundary=PROJECT_ROOT)
    for directory, dirs, names in os.walk(RUNTIME_ROOT, followlinks=False):
        for name in dirs + names:
            _no_reparse(Path(directory) / name, boundary=RUNTIME_ROOT)
    run_id = uuid.uuid4().hex
    staging = WORK_ROOT / run_id
    _no_reparse(staging, boundary=PROJECT_ROOT)
    staging.mkdir(parents=True)
    for name in ("inputs", "outputs", "tmp"):
        (staging / name).mkdir()
    manifest = []
    try:
        for index, source in enumerate(inputs):
            filename = f"{index + 1:03d}-" + source.name
            target = staging / "inputs" / filename
            # Copy bytes only: do not copy ACLs, timestamps, alternate streams,
            # or retain source handles in the restricted child.
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            manifest.append({"reference": "data/" + source.relative_to(DATA_ROOT.resolve()).as_posix(),
                             "path": "inputs/" + filename})
        (staging / "inputs.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (staging / "script.py").write_text(code, encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("sandbox_helpers.py"), staging / "coat_plot.py")
        base = RUNTIME_ROOT / "base"
        packages = RUNTIME_ROOT / "venv" / "Lib" / "site-packages"
        # -I -S ignores caller Python configuration and skips site/.pth startup.
        # Replace the path outright: no host environment, registry site paths,
        # current-directory imports, or package startup hooks are inherited.
        (staging / "runner.py").write_text(
            "import sys\n"
            f"sys.path[:]=[{str(staging)!r},{str(packages)!r},{str(base / 'Lib')!r},{str(base / 'DLLs')!r}]\n"
            "import json\nfrom pathlib import Path\n"
            "ROOT=Path.cwd()\n"
            f"sys.prefix=sys.exec_prefix={str(RUNTIME_ROOT / 'venv')!r}\n"
            "_items=json.loads((ROOT/'inputs.json').read_text(encoding='utf-8'))\n"
            "INPUTS=[ROOT/item['path'] for item in _items]\n"
            "INPUT_FILES={item['reference']:ROOT/item['path'] for item in _items}\n"
            "OUTPUT_DIR=ROOT/'outputs'\n"
            "exec(compile((ROOT/'script.py').read_text(encoding='utf-8'),'script.py','exec'),"
            "{'__name__':'__main__','__file__':str(ROOT/'script.py'),'INPUTS':INPUTS,'INPUT_FILES':INPUT_FILES,'OUTPUT_DIR':OUTPUT_DIR})\n",
            encoding="utf-8")
        # Runtime ACL updates are serialized, so one task cannot inherit the
        # temporary grant of another sandbox SID during launch/cleanup.
        while not _RUNTIME_LOCK.acquire(timeout=0.1):
            if cancel.is_set():
                raise asyncio.CancelledError()
        try:
            if cancel.is_set():
                raise asyncio.CancelledError()
            output = run_isolated(base / "python.exe",
                                  ["-I", "-S", "-B", "-X", "utf8", str(staging / "runner.py")],
                                  runtime=RUNTIME_ROOT, staging=staging,
                                  timeout_s=float(timeout_s), cancel=cancel)
        finally:
            _RUNTIME_LOCK.release()
        if cancel.is_set():
            raise asyncio.CancelledError()
        destination = DATA_ROOT / "tmp" / "assistant" / run_id
        _no_reparse(destination, boundary=DATA_ROOT)
        destination.mkdir(parents=True)
        (destination / "script.py").write_text(code, encoding="utf-8")
        exported, warnings = _collect_outputs(staging, destination)
        result = {
            "run_id": run_id, "stdout": output.stdout, "stderr": output.stderr,
            "exit_code": output.exit_code, "elapsed_s": output.elapsed_s,
            "timed_out": output.timed_out, "output_limited": output.output_limited,
            "files": exported, "script": file_info(destination / "script.py"),
            "warnings": warnings,
            "isolation": {"backend": "Windows LPAC", "network": False, "job_object": True,
                          "os_capabilities": list(RUNTIME_CAPABILITIES),
                          "registry_access": "HKLM read for OS initialization",
                          "memory_mb": 512, "max_processes": 1,
                          "files_granted": [item["reference"] for item in manifest],
                          "runtime_access": "read/execute", "workspace_access": "read/write", "fallback": False},
        }
        (destination / "execution.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        # Refuse recursion through a task-created junction. On Windows rmtree
        # removes junctions themselves; explicit unlink/rmdir makes the policy
        # independent of the runtime implementation.
        _remove_workspace(staging)


def _remove_workspace(path: Path):
    absolute = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(WORK_ROOT))
    if absolute == boundary or not absolute.is_relative_to(boundary):
        raise ValueError("工作目录清理路径越界")
    def remove(entry: Path):
        # Exact extended-length Windows names avoid normalization of malicious
        # trailing dots/spaces and reserved device-like task-created names.
        extended_prefix = chr(92) * 2 + "?" + chr(92)
        if os.name == "nt" and not str(entry).startswith(extended_prefix):
            entry = Path(extended_prefix + str(entry))
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            entry.unlink()
        elif getattr(info, "st_file_attributes", 0) & 0x400:
            # Do not enumerate or re-ACL a directory junction's target.
            if stat.S_ISDIR(info.st_mode):
                entry.rmdir()
            else:
                entry.unlink()
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(entry) as items:
                children = [Path(item.path) for item in items]
            for child in children:
                remove(child)
            entry.rmdir()
        else:
            entry.unlink()
    if absolute.exists() or absolute.is_symlink():
        remove(absolute)


async def run_python_script(code: str, files: list[str] | None = None, timeout_s: float = 60) -> dict:
    """MCP/assistant API. Cancellation waits for the OS process to be killed."""
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_run, code, files or [], timeout_s, cancel))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise
