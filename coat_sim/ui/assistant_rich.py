"""Safe offline rich text and local attachment views for the assistant."""
from __future__ import annotations
from functools import lru_cache
from collections.abc import Callable
import hashlib
import html
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from PySide6.QtCore import QUrl
from coat_sim.core.data_files import data_path, file_info
from coat_sim.ui.assistant_render import (
    display_style, render_math, store_svg, svg_preview,
)

_FILE_VIEWS: dict[tuple, dict] = {}


def _record_svg_issue(error: Exception, identity: str = "") -> None:
    """Only actual rendering failures enter the passive development ledger."""
    try:
        from coat_sim.agent.environment_issues import record_issue
        occurrence = ("preview-svg:" + hashlib.sha256(
            (identity + type(error).__name__ + str(error)).encode("utf-8")).hexdigest()) if identity else ""
        record_issue(error, stage="preview_svg", tool="preview_file", source="ui",
                     error_type=type(error).__name__, occurrence_id=occurrence)
    except Exception:
        pass

_EMBEDS = re.compile(
    r"(?P<fence>^[ \t]*(?P<marker>`{3,}|~{3,})(?P<language>[^\n]*)\n"
    r"(?P<fence_body>.*?)(?P<fence_end>^[ \t]*(?P=marker)[ \t]*(?=\n|\Z)|\Z))"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<svg><svg\b[^>]*>.*?</svg\s*>)"
    r"|(?P<display_bracket>\\\[(?P<bracket_body>.*?)\\\])"
    r"|(?P<display_dollar>(?<!\\)\$\$(?P<dollar_body>.*?)\$\$)"
    r"|(?P<inline_parenthesis>\\\((?P<parenthesis_body>.*?)\\\))"
    r"|(?P<inline_dollar>(?<![\\\d$])\$(?!\$)(?P<inline_body>[^\n$]+?)(?<!\\)\$(?![$\d]))",
    re.I | re.M | re.S,
)


def _colored_tag(value: str) -> str:
    """Allow formatting tags and one safe color property, never general HTML."""
    class Tag(HTMLParser):
        tag = ""
        attributes: list[tuple[str, str | None]] = []
        closing = False

        def handle_starttag(self, tag, attrs):
            self.tag, self.attributes = tag, attrs

        def handle_endtag(self, tag):
            self.tag, self.closing = tag, True

    parser = Tag(convert_charrefs=True)
    try:
        parser.feed(value)
    except (ValueError, AssertionError):
        return html.escape(value)
    tag = {"font": "span", "strong": "b", "em": "i"}.get(parser.tag, parser.tag)
    if tag not in {"span", "b", "i", "u", "s", "sup", "sub"}:
        return html.escape(value)
    if parser.closing:
        return "</" + tag + ">"
    if tag != "span":
        return "<" + tag + ">"
    color = ""
    for key, content in parser.attributes:
        if key == "color":
            color = str(content or "").strip()
        elif key == "style":
            for declaration in str(content or "").split(";"):
                name, separator, candidate = declaration.partition(":")
                if separator and name.strip().lower() == "color":
                    color = candidate.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?|[A-Za-z]{1,24}", color):
        from PySide6.QtGui import QColor
        parsed_color = QColor(color)
        if parsed_color.isValid():
            return '<span style="color:' + parsed_color.name() + '">'
    rgb = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", color, re.I)
    if rgb and all(int(item) <= 255 for item in rgb.groups()):
        return '<span style="color:#' + "".join(f"{int(item):02x}" for item in rgb.groups()) + '">'
    return "<span>"


def _prepare_embeds(text: str, color: str, font_pixels: int) -> tuple[str, dict[str, str], list[str]]:
    slots: dict[str, str] = {}
    files: list[str] = []

    def keep(markup: str) -> str:
        token = f"\x03ASSET{len(slots)}\x04"
        slots[token] = markup
        return token

    def replace(match) -> str:
        original = match.group(0)
        if match.group("code"):
            return original
        if len(slots) >= 64:
            return original
        if match.group("fence"):
            # An unfinished code fence stays literal during streaming.
            if not match.group("fence_end").strip():
                return original
            language = match.group("language").strip().lower()
            body = match.group("fence_body").strip()
            if language in {"math", "latex", "tex"}:
                expression, block, svg = body, True, ""
            elif language in {"svg", "xml", "html"} and re.search(r"<svg\b", body, re.I):
                drawing = re.search(r"<svg\b[^>]*>.*?</svg\s*>", body, re.I | re.S)
                if drawing is None:
                    return original
                expression, block, svg = "", True, drawing.group(0)
            else:
                return original
        elif match.group("svg"):
            expression, block, svg = "", True, original
        else:
            svg = ""
            expression = next((match.group(key) for key in (
                "bracket_body", "dollar_body", "parenthesis_body", "inline_body"
            ) if match.group(key) is not None), "")
            block = bool(match.group("display_bracket") or match.group("display_dollar"))
        if svg:
            source = None
            try:
                content = svg.encode("utf-8")
                source = store_svg(content)
                files.append(str(source))
                image = svg_preview(content)
                target = html.escape(QUrl.fromLocalFile(str(source)).toString(), quote=True)
                return "\n" + keep(f'<a href="{target}">' + image.markup("SVG 图像") + "</a>") + "\n"
            except (OSError, ValueError, RuntimeError, ImportError) as exc:
                _record_svg_issue(exc, svg)
                if source is not None:
                    target = html.escape(QUrl.fromLocalFile(str(source)).toString(), quote=True)
                    return "\n" + keep(f'<p><a href="{target}">SVG 文件（点击打开）</a>：'
                                          + html.escape(str(exc)) + "</p>") + "\n"
                return "\n" + keep("<p>SVG 暂无法预览：" + html.escape(str(exc)) + "</p>") + "\n"
        try:
            image = render_math(expression, color, font_pixels, block)
            markup = image.markup(expression, linked=True)
        except (OSError, ValueError, RuntimeError, ImportError):
            markup = "<code>" + html.escape(original) + "</code>（公式暂无法渲染，保留源码）"
        return ("\n" if block else "") + keep(markup) + ("\n" if block else "")

    return _EMBEDS.sub(replace, str(text)), slots, files

def _destination(value: str) -> str:
    """Normalize a Markdown target without truncating a full local filename."""
    value = str(value).strip()
    if value.startswith("<") and ">" in value:
        value = value[1:value.index(">")]
    elif value[:1] in {"'", '"'} and len(value) > 1:
        end = value.find(value[0], 1)
        if end >= 0:
            value = value[1:end]
    else:
        value = re.sub(r"\s+(['\"]).*\1\s*$", "", value)
    if not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
        value = re.sub(r"\\([()\[\]<>])", r"\1", value)
    if value.startswith("@{") and value.endswith("}"):
        value = value[2:-1].strip()
    elif value.lower().startswith(("@data/", "@data\\")):
        value = value[1:]
    return value


def authorized_file(value: str, *, base_path: str | Path | None = None) -> Path:
    """Model links are confined to data; selected external files are imported first."""
    value = _destination(value)
    if value[:5].lower() == "file:":
        value = QUrl(value).toLocalFile()
    path = Path(value)
    first = value.replace("\\", "/").split("/", 1)[0]
    explicit_data = first.lower() == "data"
    if base_path is not None and not path.is_absolute() and not path.drive and not explicit_data:
        report = authorized_file(str(base_path))
        if not report.is_file():
            raise ValueError("相对资源需要已授权的报告文件")
        value = str(report.parent / path)
    return data_path(value, root_allowed=False)

def safe_href(value: str, *, base_path: str | Path | None = None) -> str:
    value = _destination(value)
    try:
        if urlsplit(value).scheme.lower() in ("http", "https"):
            return value
        return QUrl.fromLocalFile(str(authorized_file(value, base_path=base_path))).toString()
    except (OSError, ValueError):
        return ""

def _markdown_links(value: str, replace: Callable) -> str:
    """Parse complete destinations, retaining balanced parentheses in filenames."""
    output = []
    position = 0
    pattern = re.compile(r"(!?)\[([^\]\n]*)\]\(")
    while match := pattern.search(value, position):
        end = match.end()
        depth = 1
        escaped = False
        while end < len(value) and value[end] != "\n":
            char = value[end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth != 0:
            output.append(value[position:match.end()])
            position = match.end()
            continue
        output.append(value[position:match.start()])
        output.append(replace(bool(match.group(1)), match.group(2), _destination(value[match.end():end])))
        position = end + 1
    output.append(value[position:])
    return "".join(output)


def _inline(value: str, embeds: dict[str, str] | None = None, *, base_path: str = "") -> str:
    slots: dict[str, str] = {}
    def keep(markup: str) -> str:
        token = f"\x01{len(slots)}\x02"
        slots[token] = markup
        return token
    def link(match) -> str:
        target = safe_href(match.group(2), base_path=base_path or None)
        label = html.escape(match.group(1))
        if not target:
            return keep(label + "（路径未授权）")
        return keep(f'<a href="{html.escape(target, quote=True)}">{label}</a>')
    for token, markup in (embeds or {}).items():
        value = value.replace(token, keep(markup))
    tick = chr(96)
    value = re.sub(tick + r"([^" + tick + r"\n]+)" + tick,
                   lambda m: keep("<code>" + html.escape(m.group(1)) + "</code>"), value)
    value = re.sub(r"</?(?:span|font|b|strong|i|em|u|s|sup|sub)\b[^>]*>",
                   lambda m: keep(_colored_tag(m.group(0))), value, flags=re.I)

    def image(match) -> str:
        target = safe_href(match.group(2), base_path=base_path or None)
        if not target:
            return keep(html.escape(match.group(1)) + "（图片路径未授权）")
        if QUrl(target).isLocalFile():
            row = file_view(match.group(2), base_path=base_path or None)
            if row.get("valid") and row.get("image"):
                from PySide6.QtGui import QImageReader
                size = QImageReader(QUrl(str(row["url"])).toLocalFile()).size()
                if size.isValid():
                    scale = min(420 / size.width(), 600 / size.height(), 1.0)
                    width, height = max(1, round(size.width() * scale)), max(1, round(size.height() * scale))
                    return keep(f'<a href="{html.escape(target, quote=True)}"><img '
                                f'src="{html.escape(str(row["url"]), quote=True)}" '
                                f'width="{width}" height="{height}" '
                                f'alt="{html.escape(match.group(1), quote=True)}"></a>')
        return link(match) + " [图片]"

    value = _markdown_links(value, lambda is_image, label, target:
                            (image if is_image else link)(_ReferenceMatch(target, label)))
    value = re.sub(r"@\{([^}]+)\}", lambda m: link(_ReferenceMatch(m.group(1))), value)
    value = re.sub(r"https?://[^\s<>\x01\x02]+", lambda m: link(_ReferenceMatch(m.group(0).rstrip(".,，。；;"))), value)
    value = html.escape(value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    for token, markup in slots.items():
        value = value.replace(token, markup)
    return value

class _ReferenceMatch:
    def __init__(self, value: str, label: str | None = None): self.value, self.label = value, label
    def group(self, index: int) -> str: return self.label if index == 1 and self.label is not None else self.value

def safe_rich_text(text: str, color: str | None = None, font_pixels: int | None = None,
                   *, base_path: str | Path | None = None) -> str:
    """Render offline Markdown, safe colors, formulas, and authorized images."""
    # The streaming worker receives captured UI style; do not query QGuiApplication there.
    default_color, default_pixels = (display_style() if color is None or font_pixels is None
                                     else (str(color), int(font_pixels)))
    if color:
        from PySide6.QtGui import QColor
        candidate = QColor(str(color))
        color = candidate.name() if candidate.isValid() else default_color
    else:
        color = default_color
    pixels = default_pixels if font_pixels is None else max(10, min(64, int(font_pixels)))
    base = str(authorized_file(str(base_path))) if base_path is not None else ""
    resources = []
    for value in referenced_files(str(text)):
        try:
            path = authorized_file(value, base_path=base or None)
            info = path.stat()
            resources.append((str(path), info.st_mtime_ns, info.st_size))
        except (OSError, ValueError):
            continue
    return _safe_rich_text(str(text), color, pixels, base, tuple(resources))


@lru_cache(maxsize=32)
def _safe_rich_text(text: str, color: str, font_pixels: int, base_path: str = "", resources: tuple = ()) -> str:
    prepared, embeds, _files = _prepare_embeds(text, color, font_pixels)
    lines = prepared.splitlines()
    output: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith((chr(96) * 3, "~~~")):
            code: list[str] = []
            i += 1
            marker = line.lstrip()[:3]
            while i < len(lines) and not lines[i].lstrip().startswith(marker):
                code.append(lines[i]); i += 1
            output.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
        elif (i + 1 < len(lines) and "|" in line
              and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[i+1])):
            def cells(row: str) -> list[str]:
                return [cell.strip() for cell in row.strip().strip("|").split("|")]
            header = cells(line)
            output.append('<table border="1" cellpadding="6" cellspacing="0"><tr>'
                          + "".join("<th>" + _inline(c, embeds, base_path=base_path) + "</th>" for c in header) + "</tr>")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                output.append("<tr>" + "".join("<td>" + _inline(c, embeds, base_path=base_path) + "</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            output.append("</table>")
            continue
        elif not line.strip():
            output.append("<br>")
        elif re.match(r"^#{1,6}\s+", line):
            output.append("<p><b>" + _inline(re.sub(r"^#{1,6}\s+", "", line), embeds, base_path=base_path) + "</b></p>")
        elif re.match(r"^\s*[-*]\s+", line):
            output.append("<p>• " + _inline(re.sub(r"^\s*[-*]\s+", "", line), embeds, base_path=base_path) + "</p>")
        else:
            output.append(_inline(line, embeds, base_path=base_path) + "<br>")
        i += 1
    return "".join(output)

def referenced_files(text: str) -> list[str]:
    values = []
    _markdown_links(text, lambda image, label, target: values.append(target) or "")
    values += re.findall(r"@\{([^}]+)\}", text)
    # Inline SVG already links its authorized source in the body.  Do not add
    # a second generated-file thumbnail beneath the same drawing.
    return list(dict.fromkeys(values))

def sanitized_svg(path: Path) -> Path:
    """Qt SVG preview never receives scripts, foreign objects, entities, or external refs."""
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("此 SVG 超过 4 MiB，请选择系统打开")
    with path.open("rb") as stream:
        content = stream.read(4 * 1024 * 1024 + 1)
    return store_svg(content)

def file_view(value: str | dict, *, base_path: str | Path | None = None) -> dict:
    try:
        path = authorized_file(value.get("path", value.get("reference", "")) if isinstance(value, dict) else value,
                               base_path=base_path)
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key in _FILE_VIEWS:
            return dict(_FILE_VIEWS[key])
        row = file_info(path)
        image_url = QUrl.fromLocalFile(str(path))
        image_url.setQuery(f"v={stat.st_mtime_ns}-{stat.st_size}")
        row["url"] = image_url.toString()
        row["image"] = str(row.get("mime", "")).startswith("image/")
        if path.suffix.lower() == ".svg":
            cleaned = sanitized_svg(path)
            try:
                rendered = svg_preview(cleaned.read_bytes(), maximum_width=1200)
                row["url"] = QUrl.fromLocalFile(str(rendered.path)).toString()
                row["image"] = True
                row["preview_format"] = "png"
            except (OSError, ValueError, RuntimeError, ImportError) as exc:
                _record_svg_issue(exc, str(path) + ":" + str(stat.st_mtime_ns) + ":" + str(stat.st_size))
                row["url"] = QUrl.fromLocalFile(str(cleaned)).toString()
                row["image"] = False
                row["preview_format"] = "source"
                row["preview_error"] = str(exc)
        row["valid"] = True
        if len(_FILE_VIEWS) >= 256:
            _FILE_VIEWS.pop(next(iter(_FILE_VIEWS)))
        _FILE_VIEWS[key] = dict(row)
        return row
    except (OSError, ValueError, ET.ParseError) as exc:
        name = value.get("name", value.get("path", "文件")) if isinstance(value, dict) else str(value)
        return {"name": name, "path": "", "url": "", "image": False, "valid": False, "error": str(exc)}
