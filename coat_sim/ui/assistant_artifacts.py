"""Canonical identities for authorized assistant file cards, without Qt imports.

File events are authoritative candidates, not proof that a file exists.  Every
candidate is rechecked against the data boundary before it becomes a card.
Identity is the authorized path, never a content hash, timestamp, or preview URL:
editing a report updates the same card instead of inventing a new artifact.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import html
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from coat_sim.core.data_files import data_path, file_info


_DERIVED_VIEW_FIELDS = frozenset({"url", "image", "valid", "error", "preview_error", "preview_format"})
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_ERROR_LINE = re.compile(
    r"(?:Traceback|(?:WinError|errno)\s*\d*|FileNotFoundError|PermissionError|"
    r"系统找不到|找不到指定|不存在|路径未授权|无法(?:读取|打开|访问)|"
    r"不(?:支持|接受)|非法(?:字符|路径)|路径.*(?:错误|失败)|"
    r"(?:示例|样例|例如|举例|sample|example)\s*[:：])", re.I,
)


def _path_spelling(value: str) -> str:
    """Decode local file URLs once; never reinterpret arbitrary prose as a path."""
    value = html.unescape(value.strip())
    if value.startswith("@{") and value.endswith("}"):
        value = value[2:-1].strip()
    elif re.match(r"^@data[/\\]", value, re.I):
        value = value[1:]
    if not value or any(ord(character) < 32 for character in value):
        raise ValueError("文件引用不能为空或包含控制字符")
    if "…" in value or any(part == "..." for part in re.split(r"[\\/]", value)):
        raise ValueError("省略的路径不是完整文件引用")
    if value[:5].lower() == "file:":
        parts = urlsplit(value)
        if parts.query or parts.fragment:
            raise ValueError("文件引用不能包含查询参数或片段")
        hostname = unquote(parts.netloc)
        value = unquote(parts.path)
        if hostname and hostname.lower() != "localhost":
            # Both file:///C:/... and file://C:/... are common clipboard forms.
            if re.fullmatch(r"[A-Za-z]:", hostname):
                value = hostname + value
            else:
                value = "//" + hostname + value
        if os.name == "nt" and re.match(r"^/[A-Za-z]:[\\/]", value):
            value = value[1:]
    elif not _DRIVE_PATH.match(value) and urlsplit(value).scheme:
        raise ValueError("产出卡片仅接受本地文件引用")
    if not value or any(ord(character) < 32 for character in value):
        raise ValueError("文件引用包含控制字符")
    return value


def _authorized_path(value: str, base_path: str | Path | None = None) -> Path:
    spelling = _path_spelling(value)
    path = Path(spelling)
    # Explicit data references always retain their project-relative meaning.
    # Other relative targets in a report resolve beside that report.
    if base_path is not None and not path.is_absolute() and not path.drive:
        first = path.parts[0] if path.parts else ""
        is_data_reference = first.lower() == "data"
        if not is_data_reference:
            base = data_path(_path_spelling(str(base_path)), root_allowed=False)
            if not base.is_file():
                raise ValueError("相对文件引用需要已授权的报告文件")
            path = base.parent / path
    authorized = data_path(str(path), root_allowed=False)
    if not authorized.is_file():
        raise ValueError("产出卡片仅显示现存普通文件")
    return authorized


def existing_artifact(value: str | Mapping, *, base_path: str | Path | None = None) -> dict | None:
    """Normalize a candidate, or return None for missing/unauthorized/error rows.

    Extra event metadata survives, while derived preview state is discarded so
    a saved modification receives a fresh view.  This helper does not grant
    access, read file contents, create directories, or mutate user files.
    """
    if isinstance(value, Mapping):
        if value.get("valid") is False or value.get("error"):
            return None
        spelling = value.get("path") or value.get("reference")
        metadata = {key: item for key, item in value.items() if key not in _DERIVED_VIEW_FIELDS}
    else:
        spelling, metadata = value, {}
    if not isinstance(spelling, str):
        return None
    try:
        path = _authorized_path(spelling, base_path)
        info = file_info(path)
        # normcase follows Windows path semantics; it deliberately preserves
        # case on macOS and Linux instead of assuming a case-insensitive disk.
        key = os.path.normcase(str(path))
        return {**metadata, **info, "artifact_id": key, "modified_ns": path.stat().st_mtime_ns}
    except (OSError, ValueError, TypeError):
        return None


def artifact_identity(value: str | Mapping, *, base_path: str | Path | None = None) -> str | None:
    """The stable path identity of an existing authorized candidate."""
    row = existing_artifact(value, base_path=base_path)
    return str(row["artifact_id"]) if row is not None else None


def merge_artifact_files(*groups: Iterable[str | Mapping], base_path: str | Path | None = None) -> list[dict]:
    """Merge one task's events/references; the last version replaces the first.

    Card order stays at the first occurrence of a path.  Persist each task's
    metadata independently; latest_artifact_groups controls only which task
    presents the current card, leaving earlier prose and attachments intact.
    """
    merged: dict[str, dict] = {}
    for group in groups:
        for value in group or ():
            row = existing_artifact(value, base_path=base_path)
            if row is not None:
                identity = row["artifact_id"]
                merged[identity] = {**merged.get(identity, {}), **row}
    return list(merged.values())


def latest_artifact_groups(groups: Iterable[Iterable[str | Mapping]]) -> list[list[dict]]:
    """Show one current card per path, owned by its latest task occurrence.

    Pass assistant artifact groups in chronological order, excluding user
    attachments. This changes presentation only: it does not remove references
    from stored messages, rewrite previous outputs, or delete any files.
    Different directories retain distinct cards even with identical names.
    """
    normalized = [merge_artifact_files(group) for group in groups]
    seen: set[str] = set()
    result: list[list[dict]] = [[] for _ in normalized]
    for index in range(len(normalized) - 1, -1, -1):
        for row in normalized[index]:
            key = row["artifact_id"]
            if key not in seen:
                result[index].append(row)
                seen.add(key)
    return result


def _reference_lines(text: str) -> Iterable[str]:
    """Skip samples, quoted diagnostics, and code; inspect visible prose only."""
    fence: str | None = None
    for line in str(text).splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None or line.lstrip().startswith(">") or _ERROR_LINE.search(line):
            continue
        # Indented code blocks are not file declarations either.
        if line.startswith("    ") or line.startswith("\t"):
            continue
        yield re.sub(r"(`+).*?\1", "", line)


def referenced_artifacts(text: str, *, base_path: str | Path | None = None) -> list[dict]:
    """Conservatively collect explicit references, never bare path fragments.

    Supported declarations are Markdown links/images, @{path}, and @data/path.
    All candidates must exist and be authorized.  General Markdown rendering
    may support additional syntax independently; an unrecognized declaration
    stays in the message body and does not fabricate a file card.
    """
    candidates: list[str] = []
    for line in _reference_lines(text):
        # Angle brackets support names with spaces; one balanced parentheses
        # pair covers common filenames. Optional quoted Markdown titles are
        # excluded from the target rather than interpreted as part of a path.
        links = re.finditer(
            r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|((?:[^()\n]|\([^()\n]*\))+?))\s*\)", line,
        )
        for match in links:
            target = (match.group(1) or match.group(2) or "").strip()
            if match.group(1) is None:
                target = re.sub(r"\s+[\"'].*[\"']$", "", target).strip()
            candidates.append(target)
        candidates.extend(re.findall(r"@\{([^}\n]+)\}", line))
        candidates.extend(match.group(1).rstrip(".,，。；;！!？?") for match in re.finditer(
            r"(?<![\w@])@(data[/\\][^\s<>\[\]{}\"']+)", line,
        ))
    return merge_artifact_files(candidates, base_path=base_path)
