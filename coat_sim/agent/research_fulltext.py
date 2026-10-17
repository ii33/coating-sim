"""Bounded downloads and isolated parsing of explicitly selected research sources.

No persistent writes, account setup, OCR, model calls or automatic follow-up URLs.
Returned text is untrusted evidence, never an instruction to the assistant.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from coat_sim import process_entry

from .research_http import ResearchHttpError, SafeHttpClient

MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_PARSE_SECONDS = 15.0
OPENALEX_CONTENT_HOST = "content.openalex.org"


def _public_url(url: str) -> str:
    """Do not persist credentials, signed query strings, or URL fragments."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _failure(error: str, **metadata: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "parse_status": "not_parsed",
            "untrusted_reference": True, **metadata}


def parse_fulltext(payload: bytes, *, format: str,
                   content_encoding: str = "identity",
                   timeout_s: float = MAX_PARSE_SECONDS) -> dict[str, Any]:
    """Parse supplied bytes in a worker with OS resource limits and a wall timeout.

    PDF requires the BSD-licensed pypdf package in this Python environment. TEI
    parsing uses Expat with DTD/entity declarations prohibited. The worker is a
    parser isolation boundary with bounded resources, not a general code sandbox.
    """
    if format not in {"pdf", "tei"}:
        return _failure("unsupported_format")
    if content_encoding not in {"", "identity", "gzip"} or (format == "pdf" and content_encoding == "gzip"):
        return _failure("unsupported_content_encoding")
    if not isinstance(payload, bytes) or not payload:
        return _failure("empty_document")
    if len(payload) > MAX_DOWNLOAD_BYTES:
        return _failure("document_too_large")
    if format == "pdf" and payload.startswith(b"\x1f\x8b"):
        return _failure("unsupported_content_encoding")
    if format == "pdf" and not payload.startswith(b"%PDF-"):
        return _failure("invalid_pdf_magic")
    if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= MAX_PARSE_SECONDS:
        return _failure("invalid_timeout")
    # Do not inherit API keys, project context, Python paths or app configuration.
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
           if name in os.environ}
    if process_entry.is_frozen():
        command = process_entry.module_command("coat_sim.agent.research_parse_worker", format)
    else:
        worker = Path(__file__).with_name("research_parse_worker.py").resolve()
        command = [sys.executable, "-I", "-B", str(worker), format]
    if content_encoding == "gzip":
        command.append("gzip")
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        # A bounded temporary input avoids pipe writer/reader deadlock. The
        # stdout reader allocates at most the declared cap plus one byte, even
        # if a faulty parser writes forever. Never communicate()/read to EOF.
        with tempfile.TemporaryFile(prefix="coating-research-") as input_file:
            input_file.write(payload)
            input_file.seek(0)
            process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env, **options)
            output_parts: list[bytes] = []
            oversized = threading.Event()
            read_failed = threading.Event()

            def read_output() -> None:
                try:
                    if process.stdout is None:
                        read_failed.set()
                        return
                    data = process.stdout.read(MAX_OUTPUT_BYTES + 1)
                    if len(data) > MAX_OUTPUT_BYTES:
                        oversized.set()
                        process.kill()
                    output_parts.append(data)
                except OSError:
                    read_failed.set()

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            timed_out = False
            try:
                process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            reader.join(timeout=1)
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()
            if oversized.is_set():
                return _failure("parse_output_too_large", parse_status="failed")
            if timed_out:
                return _failure("parse_timeout", parse_status="failed")
            if reader.is_alive() or read_failed.is_set() or not output_parts:
                return _failure("parser_failed", parse_status="failed")
            output = output_parts[0]
    except OSError:
        return _failure("parser_unavailable")
    if process.returncode != 0:
        return _failure("parser_failed", parse_status="failed")
    if len(output) > MAX_OUTPUT_BYTES:
        return _failure("parse_output_too_large", parse_status="failed")
    try:
        result = json.loads(output)
    except (UnicodeError, ValueError):
        return _failure("invalid_parser_response", parse_status="failed")
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return _failure("invalid_parser_response", parse_status="failed")
    return {**result, "untrusted_reference": True}


def fetch_fulltext(source: Mapping[str, Any], *, format: str = "pdf",
                   api_key: str | None = None,
                   client: SafeHttpClient | None = None) -> dict[str, Any]:
    """Fetch one explicitly selected source's declared PDF or TEI link.

    The source must contain content_urls["pdf"/"grobid_xml"/"tei"]. Missing
    has_content/link is reported without guessing endpoints or searching again.
    OpenAlex's content host requires a user key, sent only in a Bearer header.
    This function never persists the bytes or adds evidence to a project library.
    """
    if not isinstance(source, Mapping) or format not in {"pdf", "tei"}:
        return _failure("invalid_source")
    links = source.get("content_urls")
    content_key = ("pdf" if format == "pdf" else "grobid_xml"
                   if isinstance(links, Mapping) and links.get("grobid_xml") else "tei")
    available = source.get("has_content")
    if isinstance(available, Mapping) and available.get(content_key) is False:
        return _failure("fulltext_unavailable")
    if not isinstance(links, Mapping):
        return _failure("fulltext_unavailable")
    url = links.get(content_key) or (links.get("tei") if format == "tei" else None)
    if not isinstance(url, str) or not url or len(url) > 4096:
        return _failure("fulltext_unavailable")
    try:
        parsed_url = urlsplit(url)
        host = parsed_url.hostname
        if (parsed_url.scheme != "https" or not host or parsed_url.username is not None
                or parsed_url.password is not None):
            return _failure("invalid_content_url")
        clean_url = _public_url(url)
    except ValueError:
        return _failure("invalid_content_url")
    headers = {"Accept": "application/pdf" if format == "pdf" else "application/xml"}
    if host.lower() == OPENALEX_CONTENT_HOST:
        if not isinstance(api_key, str) or not api_key.strip():
            return _failure("fulltext_key_required", source_url=clean_url)
        if len(api_key) > 2048 or any(char.isspace() for char in api_key):
            return _failure("invalid_api_key")
        # Strip upstream queries on this endpoint so credentials cannot be duplicated
        # in URLs/provenance. The selected server-generated path is retained.
        url = clean_url
        headers["Authorization"] = "Bearer " + api_key
    metadata: dict[str, Any] = {"format": format, "source_url": clean_url,
                                "untrusted_reference": True}
    for field in ("id", "doi"):
        value = source.get(field)
        if isinstance(value, str) and len(value) <= 500:
            metadata[field] = value
    license_value = source.get("license")
    location = source.get("best_oa_location")
    if license_value is None and isinstance(location, Mapping):
        license_value = location.get("license")
    metadata["license"] = (license_value if isinstance(license_value, str)
                            and len(license_value) <= 1000 else None)
    try:
        http = client or SafeHttpClient(max_bytes=MAX_DOWNLOAD_BYTES, max_requests=4,
            max_retries=1, max_redirects=2, total_timeout_s=30,
            allowed_hosts={OPENALEX_CONTENT_HOST} if host.lower() == OPENALEX_CONTENT_HOST else None)
        response = http.get_bytes(
            url, headers=headers, max_bytes=MAX_DOWNLOAD_BYTES)
    except ResearchHttpError as exc:
        return _failure(exc.code, http_status=exc.status, usage=exc.headers, **metadata)
    if response.status != 200:
        return _failure("http_status", http_status=response.status, **metadata)
    payload = response.body
    if not isinstance(payload, bytes) or len(payload) > MAX_DOWNLOAD_BYTES:
        return _failure("document_too_large", **metadata)
    metadata.update(sha256=hashlib.sha256(payload).hexdigest(),
                    acquired_at=datetime.now(timezone.utc).isoformat(),
                    byte_count=len(payload), source_url=_public_url(response.url),
                    usage=response.headers)
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip"} or (format == "pdf" and encoding == "gzip"):
        return _failure("unsupported_content_encoding", **metadata)
    parsed = parse_fulltext(payload, format=format, content_encoding=encoding)
    return {**metadata, **parsed}
