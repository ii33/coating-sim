"""Small, inert Markdown presentation for project-owned engineering reports.

Only markup emitted here is rendered. Input HTML, links and image addresses
remain text; attachment previews use the separate managed-asset boundary.
"""
from __future__ import annotations

from html import escape
import re

_PREVIEW_LIMIT = 65536


def _inline(text: str) -> str:
    # Tokenize code first so emphasis never rewrites escaped code fragments.
    chunks = re.split(r"(`[^`\n]+`)", text)
    result = []
    for index, chunk in enumerate(chunks):
        if index % 2:
            result.append("<code>" + escape(chunk[1:-1]) + "</code>")
            continue
        value = escape(chunk)
        value = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", value)
        value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", value)
        result.append(value)
    return "".join(result)


def render_markdown(text: str) -> str:
    """Render a bounded Markdown subset without emitting URLs or raw HTML."""
    truncated = len(text) > _PREVIEW_LIMIT
    lines = text[:_PREVIEW_LIMIT].replace("\r\n", "\n").split("\n")
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence[1][0]
            count = len(fence[1])
            code = []
            index += 1
            while index < len(lines) and not re.match(r"^\s*" + re.escape(marker) + "{" + str(count) + r",}\s*$", lines[index]):
                code.append(lines[index])
                index += 1
            output.append("<pre>" + escape("\n".join(code)) + "</pre>")
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading[1])
            output.append(f"<h{level}>" + _inline(heading[2]) + f"</h{level}>")
            index += 1
            continue
        if (index + 1 < len(lines) and "|" in line
                and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])):
            def cells(row):
                return [part.strip() for part in row.strip().strip("|").split("|")][:20]
            header = cells(line)
            output.append('<table border="1" cellspacing="0" cellpadding="4"><tr>'
                          + "".join("<th>" + _inline(cell) + "</th>" for cell in header) + "</tr>")
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                output.append("<tr>" + "".join("<td>" + _inline(cell) + "</td>" for cell in cells(lines[index])) + "</tr>")
                index += 1
            output.append("</table>")
            continue
        bullet = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$", line)
        if bullet:
            ordered = bool(re.match(r"^\s*\d", line))
            tag = "ol" if ordered else "ul"
            output.append("<" + tag + ">")
            while index < len(lines):
                item = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$", lines[index])
                if item is None:
                    break
                output.append("<li>" + _inline(item[1]) + "</li>")
                index += 1
            output.append("</" + tag + ">")
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote.append(_inline(lines[index].lstrip()[1:].lstrip()))
                index += 1
            output.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue
        output.append("<p>" + _inline(line) + "</p>")
        index += 1
    if truncated:
        output.append("<p>预览已截断；编辑器中的原文仍完整保留。</p>")
    return "".join(output)
