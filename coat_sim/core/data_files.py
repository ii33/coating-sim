"""User data and explicitly imported attachments; no GUI dependencies."""
from __future__ import annotations
import base64
import json
import mimetypes
import os
from pathlib import Path
import shutil
import stat
import uuid

from coat_sim.managed_paths import assert_not_managed_project_path

from .atomic_io import replace_with_retry
from .design_files import (design_kind_from_path, legacy_design_kind_from_path,
                           validate_design_path)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
DESIGN_FOLDERS = {"source": "sources", "substrate": "substrates", "mask": "masks",
                  "machine": "machines", "process": "processes"}
METADATA_FIELDS = frozenset({"description", "notes"})
NAMED_METADATA_FIELDS = METADATA_FIELDS | {"name"}
MAX_FILE_BYTES = 32 * 1024 * 1024

def design_directory(kind: str) -> Path:
    folder = data_path(DESIGN_FOLDERS[kind])
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def design_metadata(value: dict, *, include_name: bool = False) -> dict[str, str]:
    result = {}
    for key in METADATA_FIELDS:
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError(f"{key} 须为不超过 20000 字的文字")
        result[key] = text
    if include_name and "name" in value:
        name = value["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("name 须为不超过 120 字的非空名称")
        result["name"] = name.strip()
    return result

def data_path(value: str, *, root_allowed: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("文件路径不能为空")
    # Validate the spelling before resolve() can discard Windows ambiguities.
    # Device names and alternate data streams are not ordinary data files.
    if value.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("不接受 Windows 设备命名空间路径")
    path = Path(value).expanduser()
    if path.drive and not path.is_absolute():
        raise ValueError("不接受盘符相对路径；请填写完整路径或 data 相对路径")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
    }
    for component in path.parts:
        if component == path.anchor or component in (".", ".."):
            continue
        basename = component.split(".", 1)[0].rstrip(" ").upper()
        if (component.endswith((" ", ".")) or basename in reserved
                or any(ord(char) < 32 or char in '<>:"|?*' for char in component)):
            raise ValueError("data 路径不能包含设备名、尾点尾空格、数据流或非法字符")
    if not path.is_absolute():
        path = PROJECT_ROOT / path if path.parts and path.parts[0].lower() == "data" else DATA_ROOT / path
    # Reject lexical escapes before any filesystem resolution (including UNC).
    absolute = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(DATA_ROOT))
    if not absolute.is_relative_to(boundary) or (not root_allowed and absolute == boundary):
        raise ValueError("仅允许访问 data 内的文件，不能操作 data 根目录")
    # Never resolve through links, junctions, or another reparse point. Inspect
    # only the authorized lexical subtree; missing components may be created.
    current = absolute
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (stat.S_ISLNK(info.st_mode)
                                 or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("data 路径不允许符号链接、目录联接或重解析点")
        if current == boundary:
            break
        current = current.parent
    path = absolute.resolve()
    root = boundary.resolve()
    if root != PROJECT_ROOT.resolve() / "data":
        raise ValueError("data 根目录不能指向项目外部")
    if not path.is_relative_to(root) or (not root_allowed and path == root):
        raise ValueError("仅允许访问 data 内的文件，不能操作 data 根目录")
    return path

def file_info(path: Path) -> dict:
    return {"path": str(path), "reference": "data/" + path.relative_to(DATA_ROOT.resolve()).as_posix(),
            "name": path.name, "directory": path.is_dir(),
            "size": path.stat().st_size if path.is_file() else 0,
            "mime": "application/json" if design_kind_from_path(path) else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")}

def _writable_design_kind(path: Path) -> str | None:
    """Identify canonical design destinations and reject retired design names.

    Untyped JSON remains available to general data, material, and film-design
    workflows. In the five conventional design folders it is unambiguously an
    obsolete standalone design path, so the generic writer must reject it too.
    """
    kind = design_kind_from_path(path)
    if kind is not None:
        return kind
    old_kind = legacy_design_kind_from_path(path)
    if old_kind is not None:
        validate_design_path(old_kind, path)
    if path.suffix.lower() == ".json":
        folders = {folder: design_kind for design_kind, folder in DESIGN_FOLDERS.items()}
        relative = path.relative_to(DATA_ROOT.resolve())
        for component in relative.parent.parts:
            if component.lower() in folders:
                validate_design_path(folders[component.lower()], path)
    return None

def _validate_design_payload(kind: str, payload) -> None:
    if kind == "film":
        from .film_design import validate_design
        validate_design(payload)
    else:
        from .schemas import design_data
        design_data(kind, payload)


def _material_record_destination(path: Path) -> bool:
    """True for a material record JSON the material library will read back.

    The generic data writer may still create material JSON, but it must satisfy
    the same schema the library enforces on read. Otherwise an unsupported or
    extra field (for example a non film/substrate/other ``category``) would be
    published with success and then make the whole library unreadable.
    """
    if path.suffix.lower() != ".json":
        return False
    try:
        parts = [part.lower() for part in path.relative_to(DATA_ROOT.resolve()).parts]
    except (ValueError, OSError):
        return False
    if not parts or parts[0] != "materials":
        return False
    return len(parts) == 2 or (len(parts) == 3 and parts[1] == ".overrides")


def _validate_material_payload(payload) -> None:
    from .materials import validate_material_profile
    validate_material_profile(payload)

def list_data_files(path: str = "data", *, recursive: bool = False, limit: int = 200) -> dict:
    folder = data_path(path)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise ValueError("请选择 data 内的目录")
    entries = []
    # Never descend junctions/symlinks. resolve() also guards each returned item.
    def visit(directory):
        for item in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            item = data_path(str(item))
            yield item
            if recursive and item.is_dir():
                yield from visit(item)
    for item in visit(folder):
        if len(entries) >= min(1000, max(1, limit)):
            return {"entries": entries, "truncated": True}
        entries.append(file_info(item))
    return {"entries": entries, "truncated": False}

def read_data_file(path: str, offset: int = 0, max_chars: int = 16000) -> dict:
    target = data_path(path, root_allowed=False)
    info = file_info(target)
    if not target.is_file() or info["size"] > MAX_FILE_BYTES:
        raise ValueError("文件不存在或超过 32 MiB；请先拆分数据")
    if info["mime"].startswith(("image/", "audio/", "video/")):
        return {**info, "preview": info["path"], "content": None}
    text = target.read_text(encoding="utf-8-sig")
    offset = max(0, int(offset)); end = offset + min(64000, max(1, int(max_chars)))
    return {**info, "content": text[offset:end], "offset": offset,
            "total_chars": len(text), "truncated": end < len(text)}



def write_data_file(path: str, content: str, *, overwrite: bool = False, encoding: str = "text") -> dict:
    target = data_path(path, root_allowed=False)
    assert_not_managed_project_path(target)
    kind = _writable_design_kind(target)
    if target.exists() and not overwrite:
        raise ValueError("文件已存在；覆盖须明确指定 overwrite=true")
    if encoding not in ("text", "base64"):
        raise ValueError("encoding 仅支持 text 或 base64")
    payload = content.encode("utf-8") if encoding == "text" else base64.b64decode(content, validate=True)
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("单文件不能超过 32 MiB")
    if kind:
        _validate_design_payload(kind, json.loads(payload.decode("utf-8-sig")))
        if target.exists():
            _validate_design_payload(kind, json.loads(target.read_text(encoding="utf-8-sig")))
    elif _material_record_destination(target):
        _validate_material_payload(json.loads(payload.decode("utf-8-sig")))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_bytes(payload)
        replace_with_retry(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return file_info(target)

def move_data_path(source: str, destination: str) -> dict:
    src = data_path(source, root_allowed=False); dst = data_path(destination, root_allowed=False)
    assert_not_managed_project_path(src, include_descendants=True)
    assert_not_managed_project_path(dst)
    kind = _writable_design_kind(dst)
    if not src.exists() or dst.exists() or dst.is_relative_to(src):
        raise ValueError("源不存在、目标已存在，或目标在源目录内部")
    if src.is_file() and kind:
        if src.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("设计文件超过容量限制")
        _validate_design_payload(kind, json.loads(src.read_text(encoding="utf-8-sig")))
    elif src.is_file() and _material_record_destination(dst):
        _validate_material_payload(json.loads(src.read_text(encoding="utf-8-sig")))
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return file_info(dst)

def delete_data_path(path: str, recursive: bool = False) -> dict:
    target = data_path(path, root_allowed=False)
    assert_not_managed_project_path(target, include_descendants=True)
    if target.is_dir():
        if recursive:
            # Refuse links/junctions before recursive deletion; never follow external paths.
            for directory, dirs, files in os.walk(target, followlinks=False):
                for name in dirs + files:
                    entry = Path(directory) / name
                    if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                        raise ValueError("含符号链接或目录联接，不能递归删除")
                    data_path(str(entry), root_allowed=False)
            shutil.rmtree(target)
        else:
            target.rmdir()
    else:
        target.unlink()
    return {"deleted": str(target)}

def make_data_directory(path: str) -> dict:
    target = data_path(path)
    assert_not_managed_project_path(target)
    target.mkdir(parents=True, exist_ok=True)
    return file_info(target)

def import_attachment(path: str) -> dict:
    """Called only after an explicit desktop file-picker selection."""
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("附件须为不超过 32 MiB 的文件")
    authorized_root = data_path("data")
    if source.is_relative_to(authorized_root):
        return file_info(data_path(str(source), root_allowed=False))
    folder = data_path("attachments/" + uuid.uuid4().hex, root_allowed=False)
    target = data_path(str(folder / source.name), root_allowed=False)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return file_info(target)
