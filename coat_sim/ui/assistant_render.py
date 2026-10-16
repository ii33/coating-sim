"""Offline, bounded image rendering for assistant formulas and passive SVG.

Rendering is deliberately separate from the chat model and network clients.  No
TeX process, browser, JavaScript, or external SVG resource is used here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import html
import io
import math
from pathlib import Path
import re
import struct
import threading
import uuid
import xml.etree.ElementTree as ET

from coat_sim.core.data_files import data_path
from coat_sim.core.atomic_io import replace_with_retry

_SVG_NS = "http://www.w3.org/2000/svg"
_RENDER_LOCK = threading.RLock()
_MAX_SVG_BYTES = 4 * 1024 * 1024
_MAX_PIXELS = 16 * 1024 * 1024
_MAX_SVG_EXPANDED_ELEMENTS = 100000
_SVG_TAGS = {
    "svg", "g", "defs", "use", "path", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "text", "tspan", "textpath", "title", "desc",
    "clippath", "mask", "lineargradient", "radialgradient", "stop", "style",
}
_SVG_ATTRIBUTES = {
    "id", "class", "x", "y", "x1", "y1", "x2", "y2", "dx", "dy",
    "width", "height", "viewbox", "preserveaspectratio", "transform", "d",
    "cx", "cy", "r", "rx", "ry", "points", "fill", "fill-opacity",
    "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray",
    "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "opacity", "font-family", "font-size", "font-weight", "font-style",
    "text-anchor", "text-decoration", "dominant-baseline", "alignment-baseline",
    "clip-path", "clip-rule", "mask", "offset", "stop-color", "stop-opacity",
    "gradientunits", "gradienttransform", "spreadmethod", "fx", "fy", "fr",
    "style", "color", "display", "visibility", "lengthadjust", "textlength", "href",
}
_CSS_PROPERTIES = {
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-opacity", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "opacity", "font-family", "font-size",
    "font-weight", "font-style", "text-anchor", "text-decoration",
    "dominant-baseline", "alignment-baseline", "clip-path", "clip-rule", "mask",
    "stop-color", "stop-opacity", "color", "display", "visibility",
}


@dataclass(frozen=True)
class RenderedImage:
    path: Path
    width: int
    height: int

    def markup(self, label: str, *, linked: bool = False) -> str:
        from PySide6.QtCore import QUrl

        url = html.escape(QUrl.fromLocalFile(str(self.path)).toString(), quote=True)
        image = (f'<img src="{url}" width="{self.width}" height="{self.height}" '
                 f'alt="{html.escape(label, quote=True)}">')
        return f'<a href="{url}">{image}</a>' if linked else image


def _cache_directory() -> Path:
    """Generated previews remain under the already authorized data tree."""
    cache = data_path("assistant/rendered", root_allowed=False)
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _target(content: bytes, kind: str, suffix: str) -> Path:
    cache = _cache_directory()
    path = cache / (hashlib.sha256(kind.encode("utf-8") + b"\0" + content).hexdigest() + suffix)
    if path.is_symlink() or not path.resolve().is_relative_to(cache.resolve()):
        raise ValueError("渲染缓存文件不能使用外部链接")
    return path


def _write_image(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) == 24:
            width, height = struct.unpack(">II", header[16:24])
            if width > 0 and height > 0 and width * height <= _MAX_PIXELS:
                return width, height
    except OSError:
        pass
    return None


def display_style() -> tuple[str, int]:
    """Current Qt text color and logical font size; cache keys follow the theme."""
    try:
        from PySide6.QtGui import QGuiApplication, QPalette

        app = QGuiApplication.instance()
        if app is not None:
            font = app.font()
            pixels = font.pixelSize()
            if pixels <= 0:
                pixels = round(max(8.0, font.pointSizeF()) * 96.0 / 72.0)
            return app.palette().color(QPalette.ColorRole.WindowText).name(), max(10, min(64, pixels))
    except (ImportError, RuntimeError):
        pass
    return "#202020", 18


@lru_cache(maxsize=256)
def render_math(expression: str, color: str, font_pixels: int, block: bool = False) -> RenderedImage:
    """Render a Mathtext subset to a transparent PNG at twice the logical size."""
    expression = str(expression).strip()
    if not expression or len(expression) > 4096:
        raise ValueError("公式为空或超过 4096 字符")
    depth = 0
    for index, character in enumerate(expression):
        if index and expression[index - 1] == "\\":
            continue
        if character == "{":
            depth += 1
            if depth > 64:
                raise ValueError("公式嵌套超过 64 层")
        elif character == "}":
            depth -= 1
    if re.search(r"\\(?:input|include|write|read|newcommand|renewcommand|def|catcode|usepackage)\b", expression):
        raise ValueError("不支持需要执行或加载外部内容的 TeX 命令")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(color)):
        color = "#202020"
    font_pixels = max(10, min(64, int(font_pixels)))
    key = f"math-v1:{color}:{font_pixels}:{bool(block)}"
    target = _target(expression.encode("utf-8"), key, ".png")
    size = _png_size(target)
    if size is not None:
        return RenderedImage(target, math.ceil(size[0] / 2), math.ceil(size[1] / 2))
    with _RENDER_LOCK:
        import numpy as np
        from PIL import Image
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser

        # The vector pass bounds allocation before requesting a raster image.
        prop = FontProperties(family="DejaVu Sans", size=font_pixels * 72.0 / 96.0)
        value = "$" + expression.replace("\n", " ") + "$"
        vector = MathTextParser("path").parse(value, dpi=96, prop=prop)
        if (not all(math.isfinite(item) and item >= 0 for item in (vector.width, vector.height))
                or vector.width > 4096 or vector.height > 2048
                or vector.width * vector.height * 4 > _MAX_PIXELS):
            raise ValueError("公式排版尺寸过大，请拆成较短的公式")
        parsed = MathTextParser("agg").parse(value, dpi=192, prop=prop)
        alpha = np.asarray(parsed.image, dtype=np.uint8)
        height, width = alpha.shape
        if width < 1 or height < 1 or width * height > _MAX_PIXELS:
            raise ValueError("公式图像尺寸无效")
        rgba = np.empty((height + 8, width + 8, 4), dtype=np.uint8)
        rgba[:, :, :3] = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        rgba[:, :, 3] = 0
        rgba[4:height + 4, 4:width + 4, 3] = alpha
        image = Image.fromarray(rgba)
        # Very wide formulas shrink to the minimum supported chat viewport.
        if image.width > 840:
            image = image.resize((840, max(1, round(image.height * 840 / image.width))), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
        _write_image(target, output.getvalue())
        return RenderedImage(target, math.ceil(image.width / 2), math.ceil(image.height / 2))


def _safe_css_value(value: str) -> bool:
    value = str(value)
    if "\\" in value or any(item in value.lower() for item in ("javascript:", "expression(", "@import", "data:")):
        return False
    for match in re.finditer(r"url\s*\(\s*([^)]*)\)", value, re.I):
        if not re.fullmatch(r"#[A-Za-z_][\w:.-]{0,127}", match.group(1).strip().strip("'\"")):
            return False
    # CSS parsers may accept an unfinished URL token at end of input.  A URL
    # must be a complete, validated local fragment before it reaches Qt.
    remainder = re.sub(r"url\s*\(\s*([^)]*)\)", "", value, flags=re.I)
    if re.search(r"url\s*\(", remainder, re.I):
        return False
    return len(value) <= 16384


def _safe_declarations(value: str) -> str:
    declarations = []
    for declaration in str(value).split(";"):
        name, separator, content = declaration.partition(":")
        name = name.strip().lower()
        content = content.strip()
        if separator and name in _CSS_PROPERTIES and _safe_css_value(content):
            declarations.append(name + ":" + content)
    return ";".join(declarations)


def _local_references(value: str) -> list[str]:
    return [match.group(1).strip().strip("'\"")[1:]
            for match in re.finditer(r"url\s*\(\s*([^)]*)\)", str(value), re.I)]


def _reference_selector(selector: str) -> tuple[str, str, frozenset[str]]:
    """Conservative terminal matching for URL-bearing passive CSS rules."""
    terminal = re.split(r"[\s>+]+", selector.strip())[-1]
    match = re.fullmatch(r"([A-Za-z_][\w-]*|\*)?((?:[.#][A-Za-z_][\w-]*)*)", terminal)
    if match is None or not terminal:
        raise ValueError("SVG 本地引用使用了过于复杂的 CSS 选择器")
    identifiers = re.findall(r"#([A-Za-z_][\w-]*)", match.group(2))
    if len(identifiers) > 1:
        raise ValueError("SVG CSS 选择器不能包含多个标识")
    return (match.group(1) or "*", identifiers[0] if identifiers else "",
            frozenset(re.findall(r"\.([A-Za-z_][\w-]*)", match.group(2))))


def sanitize_svg_bytes(content: bytes) -> bytes:
    """Keep a passive SVG drawing; discard scripts, embedding, and remote refs."""
    if len(content) > _MAX_SVG_BYTES or re.search(br"<!\s*(DOCTYPE|ENTITY)", content, re.I):
        raise ValueError("此 SVG 含外部声明或超过 4 MiB")
    try:
        tree = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("SVG 尚未完整生成或格式无效") from exc
    if tree.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError("文件不是 SVG 图像")
    stack = [(tree, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 20000 or depth > 64:
            raise ValueError("SVG 元素超过 20000 个或层级超过 64 层")
        for child in list(node):
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if tag not in _SVG_TAGS:
                node.remove(child)
            else:
                stack.append((child, depth + 1))
        for key, value in list(node.attrib.items()):
            local = key.rsplit("}", 1)[-1].lower()
            if local in _SVG_ATTRIBUTES and len(value) > 16384:
                raise ValueError("SVG 属性超过 16384 字符，请简化图像")
            if (local not in _SVG_ATTRIBUTES or not _safe_css_value(value)
                    or local == "href" and not re.fullmatch(r"#[A-Za-z_][\w:.-]{0,127}", value)):
                del node.attrib[key]
            elif local == "style":
                node.attrib[key] = _safe_declarations(value)
        if node.tag.rsplit("}", 1)[-1].lower() == "style" and node.text:
            rules = []
            # Only simple drawing selectors are admitted, with safe properties.
            if len(node.text) > 65536:
                raise ValueError("SVG 样式块超过 65536 字符")
            stylesheet = re.sub(r"/\*.*?\*/", "", node.text, flags=re.S)
            for rule in stylesheet.split("}"):
                selector, separator, declarations = rule.partition("{")
                if not separator or "{" in declarations:
                    continue
                selector = selector.strip()
                if re.fullmatch(r"[A-Za-z0-9_*.#,:\s>+\-\[\]=\"']{1,512}", selector):
                    rules.append(selector + "{" + _safe_declarations(declarations) + "}")
            node.text = "\n".join(rules)

    # CSS URL references must be attributed to the nodes to which their rule
    # can apply. Terminal matching ignores ancestor qualifiers conservatively.
    css_references = []
    css_rule_count = 0
    for node in tree.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "style":
            continue
        for rule in str(node.text or "").split("}"):
            selector, separator, declarations = rule.partition("{")
            if not separator:
                continue
            css_rule_count += 1
            if css_rule_count > 256:
                raise ValueError("SVG 样式规则超过 256 条")
            references = _local_references(declarations)
            if references:
                for part in selector.split(","):
                    css_references.append((_reference_selector(part), references))
                    if len(css_references) > 256:
                        raise ValueError("SVG 本地引用样式选择器超过 256 个")

    # Retain every occurrence of href/url, rather than a set: a repeated <use>
    # can expand exponentially even when the graph has no reference cycle.
    root_identifier = "\x00root"
    graph: dict[str, list[str]] = {root_identifier: []}
    base_cost: dict[str, int] = {root_identifier: 0}
    reference_edges = 0
    stack = [(tree, (root_identifier,))]
    while stack:
        node, owners = stack.pop()
        identifier = node.attrib.get("id", "")
        if identifier:
            if identifier in graph:
                raise ValueError("SVG 包含重复的元素标识")
            graph[identifier] = []
            base_cost[identifier] = 0
            owners = owners + (identifier,)
        for owner in owners:
            base_cost[owner] += 1
        references = []
        for key, value in node.attrib.items():
            if key.rsplit("}", 1)[-1].lower() == "href":
                references.append(value[1:])
            references.extend(_local_references(value))
        classes = frozenset(node.attrib.get("class", "").split())
        tag = node.tag.rsplit("}", 1)[-1]
        for (rule_tag, rule_id, rule_classes), rule_references in css_references:
            if (rule_tag in {"*", tag} and (not rule_id or rule_id == identifier)
                    and rule_classes.issubset(classes)):
                references.extend(rule_references)
        reference_edges += len(owners) * len(references)
        if reference_edges > _MAX_SVG_EXPANDED_ELEMENTS:
            raise ValueError("SVG 本地引用依赖边超过 100000 条")
        for owner in owners:
            graph[owner].extend(references)
        stack.extend((child, owners) for child in node)
    visited: set[str] = set()
    heights: dict[str, int] = {}
    expanded_cost: dict[str, int] = {}
    for origin in graph:
        pending = [(origin, False, 0)]
        active: set[str] = set()
        while pending:
            name, leaving, depth = pending.pop()
            if leaving:
                heights[name] = 1 + max((heights.get(child, 0) for child in graph.get(name, ())), default=0)
                if heights[name] > 64:
                    raise ValueError("SVG 本地引用超过 64 层")
                cost = base_cost.get(name, 0)
                for child in graph.get(name, ()):
                    cost += expanded_cost.get(child, 0)
                    if cost > _MAX_SVG_EXPANDED_ELEMENTS:
                        raise ValueError("SVG 本地引用展开后超过 100000 个元素")
                expanded_cost[name] = cost
                active.discard(name)
                visited.add(name)
                continue
            if name in active or depth > 64:
                raise ValueError("SVG 本地引用存在循环或超过 64 层")
            if name in visited:
                continue
            active.add(name)
            pending.append((name, True, depth))
            pending.extend((child, False, depth + 1) for child in graph.get(name, ()))
    ET.register_namespace("", _SVG_NS)
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)


@lru_cache(maxsize=16)
def svg_preview(content: bytes, maximum_width: int = 420) -> RenderedImage:
    """Rasterize only sanitized bytes, independent of Qt's SVG image plugin."""
    cleaned = sanitize_svg_bytes(content)
    maximum_width = max(160, min(1600, int(maximum_width)))
    target = _target(cleaned, f"svg-png-v1:{maximum_width}", ".png")
    size = _png_size(target)
    if size is not None:
        return RenderedImage(target, math.ceil(size[0] / 2), math.ceil(size[1] / 2))
    try:
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError as exc:
        raise ValueError("此运行环境缺少 Qt SVG 渲染库，请使用系统应用打开") from exc
    with _RENDER_LOCK:
        renderer = QSvgRenderer(QByteArray(cleaned))
        if not renderer.isValid():
            raise ValueError("SVG 无法由 Qt 渲染，请使用系统应用打开")
        rectangle = renderer.viewBoxF()
        width, height = float(rectangle.width()), float(rectangle.height())
        if not all(math.isfinite(item) and item > 0 for item in (width, height)):
            raise ValueError("SVG 画布尺寸无效")
        scale = min(maximum_width / width, 1000 / height, 1.0)
        logical_width, logical_height = max(1, round(width * scale)), max(1, round(height * scale))
        pixel_width, pixel_height = logical_width * 2, logical_height * 2
        if pixel_width * pixel_height > _MAX_PIXELS:
            raise ValueError("SVG 预览图像过大")
        image = QImage(pixel_width, pixel_height, QImage.Format.Format_ARGB32_Premultiplied)
        # A light canvas preserves contrast for the common black SVG chart text.
        image.fill(0xFFFFFFFF)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            renderer.render(painter, QRectF(0, 0, pixel_width, pixel_height))
        finally:
            painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise ValueError("SVG 预览图像无法保存")
        _write_image(target, bytes(buffer.data()))
        return RenderedImage(target, logical_width, logical_height)


def store_svg(content: bytes) -> Path:
    """An inline SVG becomes a normal authorized file that can be opened later."""
    cleaned = sanitize_svg_bytes(content)
    target = _target(cleaned, "svg-source-v1", ".svg")
    if not target.exists():
        _write_image(target, cleaned)
    return target
