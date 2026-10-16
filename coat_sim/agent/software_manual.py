"""Read-only software capability reference shared by MCP and all agents."""
from __future__ import annotations

from pathlib import Path
import re

MANUAL_PATH = Path(__file__).resolve().parent / "resources" / "software_manual.md"


def read_software_manual(section: str = "index", max_chars: int = 12000) -> dict:
    """Query a fixed project manual by chapter key; no arbitrary paths are read."""
    text = MANUAL_PATH.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^## ([a-z][a-z_0-9]*)\s*[·:：-]\s*(.+)$", text, re.MULTILINE))
    chapters = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chapters[match.group(1)] = {"title": match.group(2).strip(), "content": text[match.start():end].strip()}
    header = text[:matches[0].start()].strip() if matches else ""
    if section == "index":
        return {"section": "index", "version": header[:1000], "chapters": [{"key": key, "title": value["title"]} for key, value in chapters.items()], "source": "software_manual.md"}
    if section != "all" and section not in chapters:
        raise ValueError("未知软件手册章节，请先查询 index；可用：" + ", ".join(chapters))
    content = text if section == "all" else chapters[section]["content"]
    limit = min(24000, max(1000, int(max_chars)))
    return {"section": section, "version": header[:1000], "content": content[:limit], "total_chars": len(content), "truncated": len(content) > limit, "source": "software_manual.md"}


def capability_context() -> str:
    """Small authoritative boundary available even when chat has no business tools."""
    try:
        overview = read_software_manual("overview", 2400)["content"]
        limits = read_software_manual("limitations", 2400)["content"]
        return "\n[本软件已实现能力与边界]\n" + overview + "\n" + limits + "\n详细接口可查询 read_software_manual 对应章节。只生成软件支持的结构；超出范围须明确说明不适用于本软件，不能作为可保存/可仿真的方案。"
    except (OSError, ValueError):
        return "\n本软件只支持固定太阳轮理想行星盘的几何蒸发镀膜模型。任意机械设备、其他齿轮传动、溅射、设备控制、光学面型均不能在此软件实现。生成设计前核对 input_schema；手册当前不可用时说明限制，禁止编造支持。"
