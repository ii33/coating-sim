"""Bounded, inert text access to the one shipped algorithm reference.

A source path is deliberately not part of the public API. HTML is parsed, never
rendered, executed, or followed. A content hash pins sequential page reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import stat
from typing import Any, NoReturn
from urllib.parse import urlsplit

from .project_dependencies import ProjectError, reject_links

REFERENCE_PATH = Path(__file__).resolve().parents[2] / "docs" / "algorithms.html"
SOURCE = "docs/algorithms.html"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SECTIONS = 1000
MAX_READ_CHARS = 6000
MAX_RESULTS = 50
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 12
_BLOCKS = frozenset({"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6",
                     "li", "ul", "ol", "pre", "table", "tr", "blockquote", "figcaption"})
_HIDDEN = frozenset({"script", "style", "template", "noscript", "iframe", "object"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _fail(code: str, message: str) -> NoReturn:
    raise ProjectError(code, message)


def _integer(value: Any, name: str, *, maximum: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("invalid_arguments", f"{name} must be an integer in [{minimum}, {maximum}]")


def _version(value: Any) -> None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
        _fail("invalid_arguments", "expected_version must be a returned SHA-256 version")


def _clean(parts: list[str]) -> str:
    text = "".join(parts).replace("\r", "")
    return "\n".join(line for row in text.splitlines() if (line := re.sub(r"[\t \xa0]+", " ", row).strip()))


def _safe_href(value: str) -> bool:
    if (not value or len(value) > 2048 or value.startswith("//")
            or any(ord(char) < 32 or char.isspace() for char in value)):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in {"", "http", "https"} and not parts.username and not parts.password
    except ValueError:
        return False


@dataclass
class _Section:
    section_id: str
    parts: list[str] = field(default_factory=list)
    heading: list[str] = field(default_factory=list)
    title: str = ""
    text: str = ""


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[_Section] = []
        self.active: list[_Section] = []
        self.section_stack: list[_Section | None] = []
        self.hidden: list[str] = []
        self.heading_tag: str | None = None
        self.title_parts: list[str] = []
        self.in_title = False
        self.pill_parts: list[str] | None = None
        self.pills: list[str] = []
        self.links: list[str | None] = []

    def _append(self, text: str) -> None:
        for section in self.active:
            section.parts.append(text)
            if self.heading_tag and not section.title:
                section.heading.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden:
            if tag in _HIDDEN:
                self.hidden.append(tag)
            return
        if tag in _HIDDEN:
            self.hidden.append(tag)
            return
        values = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "span" and "pill" in (values.get("class") or "").split():
            self.pill_parts = []
        if tag == "section":
            identity = values.get("id")
            section = None
            if identity:
                if (len(identity) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", identity)
                        or any(item.section_id == identity for item in self.sections)):
                    _fail("reference_invalid", "Algorithm section IDs must be unique safe anchors")
                if len(self.sections) >= MAX_SECTIONS:
                    _fail("reference_capacity", "Algorithm reference exceeds its section budget")
                section = _Section(identity)
                self.sections.append(section)
                self.active.append(section)
            self.section_stack.append(section)
        if tag in _BLOCKS or tag == "br":
            self._append("\n")
        elif tag in {"td", "th"}:
            self._append(" | ")
        elif tag == "sup":
            self._append("^(")
        elif tag == "sub":
            self._append("_(")
        if tag in _HEADINGS:
            self.heading_tag = tag
        if tag == "a":
            target = values.get("href") or ""
            self.links.append(target if _safe_href(target) else None)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "title":
            self.in_title = False
        if tag == "span" and self.pill_parts is not None:
            self.pills.append(_clean(self.pill_parts))
            self.pill_parts = None
        if tag in {"sup", "sub"}:
            self._append(")")
        if tag == "a" and self.links:
            target = self.links.pop()
            if target:
                self._append(f" ({target})")
        if tag == self.heading_tag:
            for section in self.active:
                if not section.title:
                    section.title = _clean(section.heading)
            self.heading_tag = None
        if tag in _BLOCKS:
            self._append("\n")
        if tag == "section" and self.section_stack:
            finished = self.section_stack.pop()
            if finished is not None:
                self.active.remove(finished)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        self._append(data)
        if self.in_title:
            self.title_parts.append(data)
        if self.pill_parts is not None:
            self.pill_parts.append(data)


@lru_cache(maxsize=2)
def _parse(raw: bytes) -> tuple[dict[str, Any], tuple[_Section, ...]]:
    parser = _ReferenceParser()
    try:
        parser.feed(raw.decode("utf-8"))
        parser.close()
    except (UnicodeError, RecursionError) as exc:
        raise ProjectError("reference_invalid", "Algorithm reference is not parseable UTF-8 HTML") from exc
    if not parser.sections or parser.section_stack or parser.hidden:
        _fail("reference_invalid", "Algorithm reference has missing or unclosed sections")
    for section in parser.sections:
        section.text = _clean(section.parts)
        section.title = section.title or section.section_id
        section.parts.clear()
        section.heading.clear()
    version_label = next((value for value in parser.pills if value.startswith("版本")), None)
    date_label = next((value for value in parser.pills if value.startswith("更新")), "")
    date = re.search(r"\d{4}-\d{2}-\d{2}", date_label)
    metadata = {"source": SOURCE, "version": hashlib.sha256(raw).hexdigest(),
                "document_title": _clean(parser.title_parts)[:500],
                "document_version": version_label[:1000] if version_label else None,
                "document_date": date.group() if date else None,
                "source_bytes": len(raw), "untrusted_reference": True,
                "projection": "inert_html_text_with_links; SVG labels only, no diagram rendering"}
    return metadata, tuple(parser.sections)


def _load(expected_version: str | None) -> tuple[dict[str, Any], tuple[_Section, ...]]:
    _version(expected_version)
    try:
        reject_links(REFERENCE_PATH)
        info = REFERENCE_PATH.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("reference_unsafe", "Algorithm reference must be an ordinary unlinked file")
        if info.st_size > MAX_SOURCE_BYTES:
            _fail("reference_capacity", "Algorithm reference exceeds its byte budget")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(REFERENCE_PATH, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
                _fail("reference_unsafe", "Algorithm reference changed while opening")
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            _fail("reference_capacity", "Algorithm reference exceeds its byte budget")
    except FileNotFoundError as exc:
        raise ProjectError("reference_unavailable", "Shipped algorithm reference is missing") from exc
    except OSError as exc:
        raise ProjectError("reference_unavailable", "Shipped algorithm reference cannot be read") from exc
    metadata, sections = _parse(raw)
    if expected_version is not None and metadata["version"] != expected_version:
        _fail("reference_version_changed", "Algorithm reference changed; restart at offset zero")
    return metadata, sections


def _summary(section: _Section) -> dict[str, Any]:
    return {"section_id": section.section_id, "title": section.title[:500],
            "title_truncated": len(section.title) > 500,
            "source": SOURCE, "citation": f"{SOURCE}#{section.section_id}",
            "total_chars": len(section.text)}


def algorithm_reference_catalog(offset: int = 0, limit: int = 40,
                                expected_version: str | None = None) -> dict[str, Any]:
    _integer(offset, "offset", maximum=MAX_SECTIONS)
    _integer(limit, "limit", minimum=1, maximum=MAX_RESULTS)
    metadata, sections = _load(expected_version)
    rows = [_summary(section) for section in sections[offset:offset + limit]]
    end = offset + len(rows)
    return {**metadata, "sections": rows, "total": len(sections), "offset": offset,
            "limit": limit, "next_offset": end if end < len(sections) else None,
            "has_more": end < len(sections)}


def algorithm_reference_search(query: str, offset: int = 0, limit: int = 10,
                               expected_version: str | None = None) -> dict[str, Any]:
    _integer(offset, "offset", maximum=MAX_SECTIONS)
    _integer(limit, "limit", minimum=1, maximum=MAX_RESULTS)
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or "\0" in query:
        _fail("invalid_arguments", "query must be nonempty text of at most 200 characters")
    terms = query.casefold().split()
    if len(terms) > MAX_QUERY_TERMS:
        _fail("invalid_arguments", "query permits at most 12 whitespace-separated keywords")
    metadata, sections = _load(expected_version)
    matching = [section for section in sections if all(
        term in (section.section_id + "\n" + section.title + "\n" + section.text).casefold() for term in terms)]
    rows = []
    for section in matching[offset:offset + limit]:
        found = re.search(re.escape(terms[0]), section.text, re.IGNORECASE)
        start = max(0, found.start() - 100) if found else 0
        rows.append({**_summary(section), "excerpt": section.text[start:start + 400],
                     "excerpt_offset": start})
    end = offset + len(rows)
    return {**metadata, "results": rows, "query": query, "total": len(matching),
            "offset": offset, "limit": limit, "next_offset": end if end < len(matching) else None,
            "has_more": end < len(matching), "search_mode": "all_literal_keywords"}


def algorithm_reference_read(section_id: str, offset: int = 0, max_chars: int = MAX_READ_CHARS,
                             expected_version: str | None = None) -> dict[str, Any]:
    if not isinstance(section_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", section_id):
        _fail("invalid_arguments", "section_id must be an exact catalog anchor, never a path")
    _integer(offset, "offset", maximum=MAX_SOURCE_BYTES)
    _integer(max_chars, "max_chars", minimum=1, maximum=MAX_READ_CHARS)
    metadata, sections = _load(expected_version)
    section = next((item for item in sections if item.section_id == section_id), None)
    if section is None:
        _fail("reference_section_not_found", "Unknown algorithm section; read the catalog first")
    end = min(len(section.text), offset + max_chars)
    return {**metadata, **_summary(section), "text": section.text[offset:end], "offset": offset,
            "next_offset": end if end < len(section.text) else None,
            "has_more": end < len(section.text), "max_chars": max_chars}
