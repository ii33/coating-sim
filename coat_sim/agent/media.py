"""Bounded local inputs and media outputs shared by agent and desktop."""
from __future__ import annotations
import base64
import binascii
import json
import math
import mimetypes
from pathlib import Path
import re
from coat_sim.core.data_files import data_path, file_info, DATA_ROOT, MAX_FILE_BYTES

MCP_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_MCP_IMAGE_MIME_SIGNATURES = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda data: len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    "image/gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
}


class MCPImageContentError(ValueError):
    """A safe-to-display MCP image validation error; never includes image data."""


def image_content_to_binary(content):
    """Convert one legal, bounded MCP ImageContent into framework binary content.

    Validation is intentionally dependency-free: MIME, Base64, size, and file
    signature are checked without decoding or transforming user pixels.
    """
    from mcp.types import ImageContent
    from pydantic_ai.messages import BinaryContent

    if not isinstance(content, ImageContent) or getattr(content, "type", None) != "image":
        raise MCPImageContentError("MCP result contains an invalid image block.")
    mime = getattr(content, "mime_type", None)
    if mime is None:
        mime = getattr(content, "mimeType", None)
    if not isinstance(mime, str) or mime not in _MCP_IMAGE_MIME_SIGNATURES:
        raise MCPImageContentError("MCP image MIME type is not supported.")
    encoded = getattr(content, "data", None)
    if not isinstance(encoded, str) or not encoded:
        raise MCPImageContentError("MCP image data is empty or invalid.")
    # Reject oversized encodings before allocating the decoded byte buffer.
    max_encoded_chars = 4 * ((MCP_IMAGE_MAX_BYTES + 2) // 3)
    if len(encoded) > max_encoded_chars:
        raise MCPImageContentError("MCP image exceeds the 8 MiB limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise MCPImageContentError("MCP image payload is not valid Base64.") from None
    if not data or len(data) > MCP_IMAGE_MAX_BYTES:
        raise MCPImageContentError("MCP image exceeds the 8 MiB limit or is empty.")
    if not _MCP_IMAGE_MIME_SIGNATURES[mime](data):
        raise MCPImageContentError("MCP image bytes do not match the declared MIME type.")
    try:
        return BinaryContent(data, media_type=mime)
    except Exception:
        # Framework validation exceptions can echo input values; expose only a
        # fixed message so image bytes cannot enter diagnostics or tool errors.
        raise MCPImageContentError("MCP image could not be packaged as binary content.") from None

def build_prompt(text: str, attachments: list[dict]):
    from pydantic_ai.messages import BinaryContent
    parts = [text]
    if len(attachments) > 8:
        raise ValueError("每轮最多 8 个附件")
    total = 0
    for item in attachments:
        path = data_path(item["path"], root_allowed=False)
        total += path.stat().st_size
        if total > MAX_FILE_BYTES:
            raise ValueError("本轮附件合计不能超过 32 MiB")
        info = file_info(path); mime = info['mime']
        mime = {'audio/x-wav': 'audio/wav', 'audio/mp3': 'audio/mpeg', 'audio/x-flac': 'audio/flac', 'audio/x-aiff': 'audio/aiff'}.get(mime, mime)
        parts.append(f"[用户显式引用文件：{info['reference']}；文件名：{path.name}]")
        if mime.startswith(('image/', 'audio/')) or mime == 'application/pdf':
            if mime == 'image/svg+xml':
                content = path.read_text(encoding='utf-8-sig')
                if len(content) > 64000:
                    raise ValueError(f"{path.name} 超过 64000 字，请拆分或由文件工具分段读取")
                parts.append(content)
            else:
                parts.append(BinaryContent(path.read_bytes(), media_type=mime))
        elif mime.startswith('video/'):
            raise ValueError("当前 Chat 兼容接口不支持直接传入本地视频；请引用提取的图片或音频")
        else:
            try:
                content = path.read_text(encoding='utf-8-sig')
            except (UnicodeError, OSError):
                raise ValueError(f"{path.name} 不能读取为 UTF-8；请转换为文本、CSV、图片或 PDF") from None
            if len(content) > 64000:
                raise ValueError(f"{path.name} 超过 64000 字，请拆分或由文件工具分段读取")
            parts.append(content)
    return parts if attachments else text

def _text_tokens(text):
    # Project estimate, not a provider tokenizer: CJK/non-ASCII 1 each, ASCII / 4.
    nonascii = sum(ord(c) > 127 for c in text)
    return math.ceil((len(text) - nonascii) / 4) + nonascii

def context_estimate(messages, tools, instructions):
    from pydantic_ai.messages import BinaryContent
    instruction_text = "\n".join(str(getattr(part, "content", part)) for part in (instructions or []))
    totals = {"messages": 0, "tools": 0, "instructions": _text_tokens(instruction_text), "media": 0, "overhead": 64 * len(messages) + 128 * len(tools)}
    history_chars = history_bytes = history_parts = 0
    def measure(value):
        nonlocal history_chars, history_bytes
        if isinstance(value, BinaryContent):
            # Media tokenization is model-specific. Provisioning estimate only.
            totals['media'] += 2048 if value.is_image else max(2048, math.ceil(len(value.data) / 1024) * 8)
            history_bytes += len(value.data)
        elif isinstance(value, str):
            totals['messages'] += _text_tokens(value)
            history_chars += len(value)
            history_bytes += len(value.encode('utf-8'))
        elif isinstance(value, (list, tuple)):
            for item in value: measure(item)
        elif isinstance(value, dict):
            measure(json.dumps(value, ensure_ascii=False, default=str))
    for message in messages:
        for part in message.parts:
            history_parts += 1
            for key in ('content', 'args'):
                if hasattr(part,key): measure(getattr(part,key))
            # Tool identities are sent alongside their arguments and returns.
            totals['messages'] += _text_tokens(str(getattr(part, 'tool_name', '')) + str(getattr(part, 'tool_call_id', '')))
    definitions = [{"name": getattr(tool, 'name', ''), "description": getattr(tool, 'description', ''),
                    "parameters": getattr(tool, 'parameters_json_schema', {})} for tool in tools]
    totals['tools'] = _text_tokens(json.dumps(definitions, ensure_ascii=False, default=str))
    return {**totals, "input_tokens": max(1,sum(totals.values())), "history_messages": len(messages),
            "history_parts": history_parts, "history_chars": history_chars, "history_bytes": history_bytes,
            "basis": "本项目估算；包含消息、完整工具定义与指令，媒体按预留量计算；实际输入以服务器报告为准"}

def persist_output(part, request: str, index: int) -> dict:
    import uuid
    content = part.content
    if len(content.data) > MAX_FILE_BYTES:
        raise ValueError("模型生成附件超过 32 MiB")
    extension = mimetypes.guess_extension(content.media_type) or '.bin'
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request) or any(c in request for c in (':', '..')):
        raise ValueError('附件请求编号无效')
    target = data_path('tmp/assistant/' + request + '/' + uuid.uuid4().hex + extension, root_allowed=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.data)
    return file_info(target)
