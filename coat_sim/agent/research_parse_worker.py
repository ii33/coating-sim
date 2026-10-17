"""Private resource-limited parser worker. Input is bytes, output bounded JSON.

Invoked by research_fulltext with isolated Python; never import from app startup.
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import zlib
from typing import Any
from xml.parsers import expat

MAX_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_CHARS = 500_000
MAX_SEGMENTS = 2000
MAX_PAGES = 200
MAX_NODES = 50_000
MEMORY_BYTES = 768 * 1024 * 1024
_JOB: Any = None


class ParseRejected(ValueError):
    """A safe public error code; never includes document content."""


def _set_limits() -> None:
    """Fail closed if OS parser memory/CPU limits cannot be installed."""
    global _JOB
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    dll_factory = getattr(ctypes, "WinDLL", None)
    if dll_factory is None:
        raise ParseRejected("parser_isolation_unavailable")
    kernel = dll_factory("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimit()
    # PROCESS_TIME | ACTIVE_PROCESS | PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    limits.BasicLimitInformation.LimitFlags = 0x2 | 0x8 | 0x100 | 0x2000
    limits.BasicLimitInformation.PerProcessUserTimeLimit = 12 * 10_000_000
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    limits.ProcessMemoryLimit = MEMORY_BYTES
    if (not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits),
                                                     ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        raise ParseRejected("parser_isolation_unavailable")
    # Keep the handle open until process exit (closing it terminates the job).
    _JOB = job


def _parse_pdf(payload: bytes) -> dict[str, Any]:
    if not payload.startswith(b"%PDF-"):
        raise ParseRejected("invalid_pdf_magic")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ParseRejected("pdf_parser_unavailable") from exc
    reader = PdfReader(io.BytesIO(payload), strict=True)
    if reader.is_encrypted:
        raise ParseRejected("encrypted_pdf")
    if len(reader.pages) > MAX_PAGES:
        raise ParseRejected("page_limit")
    segments = []
    chars = 0
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        chars += len(text)
        if chars > MAX_CHARS:
            raise ParseRejected("text_limit")
        # Blank pages are retained, so physical page positions cannot shift.
        segments.append({"page": number, "section": None, "paragraph": None, "text": text})
    has_text = any(item["text"] for item in segments)
    return {"ok": True, "parse_status": "parsed" if has_text else "no_text",
            "segments": segments, "page_count": len(segments),
            "warning": None if has_text else "no_text_layer_no_ocr"}


def _decode_tei(payload: bytes, declared_gzip: bool = False) -> tuple[bytes, bool]:
    """Bound decompression inside the resource-limited worker, including members."""
    compressed = declared_gzip or payload.startswith(b"\x1f\x8b")
    if not compressed:
        return payload, False
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as stream:
            decoded = stream.read(MAX_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ParseRejected("invalid_gzip_document") from exc
    if len(decoded) > MAX_BYTES:
        raise ParseRejected("decompressed_document_too_large")
    return decoded, True


def _parse_tei(payload: bytes) -> dict[str, Any]:
    parser = expat.ParserCreate(namespace_separator="}")
    path: list[str] = []
    sections: list[str] = []
    segments: list[dict[str, Any]] = []
    capture: list[str] | None = None
    capture_kind: str | None = None
    capture_depth = 0
    nodes = 0
    chars = 0
    returned_chars = 0
    tei_depth: int | None = None
    tei_seen = False

    def in_document_text() -> bool:
        if tei_depth is None:
            return False
        relative = path[tei_depth:]
        # Standard TEI text/body and the documented older Grobid text/div.
        return len(relative) >= 2 and relative[0] == "text" and relative[1] in {"body", "div"}

    def reject(*_args: Any) -> Any:
        raise ParseRejected("xml_declaration_forbidden")

    def start(name: str, _attrs: dict[str, str]) -> None:
        nonlocal capture, capture_kind, capture_depth, nodes, tei_depth, tei_seen
        tag = name.rsplit("}", 1)[-1].lower()
        nodes += 1
        if nodes > MAX_NODES or len(path) >= 96:
            raise ParseRejected("xml_structure_limit")
        path.append(tag)
        if tei_depth is None:
            if not tei_seen and path in (["tei"], ["html", "body", "tei"]):
                tei_depth = len(path)
                tei_seen = True
            elif not tei_seen and path in (["html"], ["html", "body"]):
                return
            else:
                raise ParseRejected("invalid_tei_root")
        elif tag == "tei":
            raise ParseRejected("invalid_tei_root")
        if tag == "div" and in_document_text():
            sections.append("")
        if in_document_text() and tag in {"head", "p"} and capture is None:
            capture = []
            capture_kind = tag
            capture_depth = len(path)

    def text(value: str) -> None:
        nonlocal chars
        chars += len(value)
        if chars > MAX_CHARS:
            raise ParseRejected("text_limit")
        if tei_depth is None and value.strip():
            raise ParseRejected("invalid_tei_root")
        if capture is not None:
            capture.append(value)

    def end(_name: str) -> None:
        nonlocal capture, capture_kind, returned_chars, tei_depth
        tag = path[-1]
        if capture is not None and len(path) == capture_depth:
            value = " ".join("".join(capture).split())
            if capture_kind == "head" and sections:
                if len(value) > 500:
                    raise ParseRejected("section_limit")
                sections[-1] = value
            elif capture_kind == "p" and value:
                if len(segments) >= MAX_SEGMENTS:
                    raise ParseRejected("segment_limit")
                section = " / ".join(item for item in sections if item) or None
                if section is not None and len(section) > 1000:
                    raise ParseRejected("section_limit")
                returned_chars += len(value) + len(section or "")
                if returned_chars > MAX_CHARS:
                    raise ParseRejected("text_limit")
                segments.append({"page": None, "section": section,
                    "paragraph": len(segments) + 1, "text": value})
            capture = None
            capture_kind = None
        if tag == "div" and in_document_text():
            sections.pop()
        if tei_depth == len(path):
            tei_depth = None
        path.pop()

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.CharacterDataHandler = text
    parser.EndElementHandler = end
    parser.Parse(payload, True)
    if not tei_seen:
        raise ParseRejected("invalid_tei_root")
    return {"ok": True, "parse_status": "parsed" if segments else "no_text",
            "segments": segments, "page_count": None,
            "warning": None if segments else "no_tei_body_paragraphs"}


def main() -> None:
    try:
        try:
            _set_limits()
        except Exception as exc:
            raise ParseRejected("parser_isolation_unavailable") from exc
        payload = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ParseRejected("document_too_large")
        if len(sys.argv) not in {2, 3} or sys.argv[1] not in {"pdf", "tei"}:
            raise ParseRejected("unsupported_format")
        declared_gzip = len(sys.argv) == 3 and sys.argv[2] == "gzip"
        if len(sys.argv) == 3 and (not declared_gzip or sys.argv[1] != "tei"):
            raise ParseRejected("unsupported_content_encoding")
        if sys.argv[1] == "pdf":
            result = _parse_pdf(payload)
        else:
            payload, compressed = _decode_tei(payload, declared_gzip)
            result = _parse_tei(payload)
            result.update(decoded_byte_count=len(payload), content_encoding="gzip" if compressed else "identity")
    except ParseRejected as exc:
        result = {"ok": False, "error": str(exc), "parse_status": "failed"}
    except MemoryError:
        result = {"ok": False, "error": "parse_memory_limit", "parse_status": "failed"}
    except Exception:
        result = {"ok": False, "error": "invalid_document", "parse_status": "failed"}
    # Encode incrementally; never build an arbitrarily large JSON string first.
    output = bytearray()
    for part in json.JSONEncoder(ensure_ascii=True).iterencode(result):
        encoded = part.encode("ascii")
        if len(output) + len(encoded) > MAX_OUTPUT_BYTES:
            output = bytearray(b'{"ok":false,"error":"parse_output_too_large","parse_status":"failed"}')
            break
        output.extend(encoded)
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
