"""Stable chat paragraphs and coalesced, off-thread offline rich-text conversion.

Only completed paragraphs enter the rich renderer while streaming. An unfinished
tail remains selectable plain text in bounded pieces; the original source is
always preserved for copying. No network/browser rendering is introduced.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
from typing import Any

from PySide6.QtCore import QObject, Property, Signal, QAbstractListModel, QModelIndex, QTimer, Qt, Slot

from .assistant_render import display_style
from .assistant_rich import safe_rich_text


_TAIL_PIECE = 4096
_RICH_BLOCK_LIMIT = 16384


class RichBlockModel(QAbstractListModel):
    BlockRole = int(Qt.UserRole) + 1

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, Any]] = []

    def roleNames(self):
        return {self.BlockRole: b"blockData"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if role == self.BlockRole and index.isValid() and 0 <= index.row() < len(self.rows):
            return self.rows[index.row()]
        return None

    def update(self, values: list[dict[str, Any]]) -> None:
        self.update_from(0, values)

    def update_from(self, start: int, values: list[dict[str, Any]]) -> None:
        """Replace only the unfinished suffix; completed rows stay untouched."""
        end = start + len(values)
        common = min(end, len(self.rows))
        if end < len(self.rows):
            self.beginRemoveRows(QModelIndex(), end, len(self.rows) - 1)
            del self.rows[end:]
            self.endRemoveRows()
        elif end > len(self.rows):
            self.beginInsertRows(QModelIndex(), len(self.rows), end - 1)
            self.rows.extend(values[len(self.rows) - start:])
            self.endInsertRows()
        for index in range(start, common):
            if self.rows[index] != values[index - start]:
                self.rows[index] = values[index - start]
                item = self.index(index, 0)
                self.dataChanged.emit(item, item, [self.BlockRole])

    def set_row(self, index: int, value: dict[str, Any]) -> None:
        if self.rows[index] != value:
            self.rows[index] = value
            item = self.index(index, 0)
            self.dataChanged.emit(item, item, [self.BlockRole])


def _paragraphs(text: str, final: bool) -> tuple[list[str], str]:
    """Split only at blank lines outside fenced code, display math and SVG.

    Markdown tables, code, SVG and multiline formulas stay together to
    preserve meaning.
    This is a presentation convention, never a physical/statistical algorithm.
    """
    pieces: list[str] = []
    start = 0
    fence = ""
    display = ""
    svg = False
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not stripped[len(marker.group(1)):].strip():
                fence = ""
        elif marker:
            fence = marker.group(1)
        elif svg:
            if re.search(r"</svg\s*>", line, re.I):
                svg = False
        elif display:
            if display in line:
                display = ""
        else:
            svg_start = re.search(r"<svg\b", line, re.I)
            if svg_start and not re.search(r"</svg\s*>", line[svg_start.end():], re.I):
                svg = True
            elif "\\[" in line and "\\]" not in line.split("\\[", 1)[1]:
                display = "\\]"
            elif len(re.findall(r"(?<!\\)\$\$", line)) % 2:
                display = "$$"
        offset += len(line)
        if not stripped and not (fence or display or svg) and line.endswith(("\n", "\r")):
            piece = text[start:offset]
            if piece:
                pieces.append(piece)
            start = offset
    tail = text[start:]
    if final and tail:
        pieces.append(tail)
        tail = ""
    return pieces, tail


def _plain_pieces(text: str):
    """Stable bounded chunks even when a model streams a very long code fence."""
    for offset in range(0, len(text), _TAIL_PIECE):
        yield text[offset:offset + _TAIL_PIECE]


def _display_blocks(text: str):
    """Bound each Qt text document while preserving normal rich paragraphs.

    Long code fences can be repeated around each visible slice. Other oversized
    indivisible Markdown structures stay literal rather than parsing broken
    fragments. Copying always uses RichTextDocument.source, including delimiters.
    """
    if len(text) <= _RICH_BLOCK_LIMIT:
        yield text, True
        return
    opening = re.match(r"^([ \t]*)(`{3,}|~{3,})([^\n]*)\n", text)
    if opening and opening.group(3).strip().lower() not in {"math", "latex", "tex", "svg", "xml", "html"}:
        closing = re.search(r"(?m)^[ \t]*" + re.escape(opening.group(2)) + r"[ \t]*(?:\r?\n)?\s*\Z", text)
        if closing:
            body = text[opening.end():closing.start()]
            offset = 0
            while offset < len(body):
                end = min(len(body), offset + _TAIL_PIECE)
                if end < len(body):
                    newline = body.rfind("\n", offset, end)
                    if newline > offset:
                        end = newline + 1
                yield opening.group(0) + body[offset:end] + "\n" + opening.group(2) + "\n", True
                offset = end
            return
    for piece in _plain_pieces(text):
        yield piece, False


@dataclass(frozen=True)
class _RenderJob:
    document: str
    instance_generation: int
    block: str
    revision: int
    text: str
    color: str
    pixels: int


class RichTextDocument(QObject):
    sourceChanged = Signal()

    def __init__(self, service: "RichRenderService", identity: str):
        super().__init__(service)
        self.service = service
        self.identity = identity
        service._document_generation += 1
        self.instance_generation = service._document_generation
        self.model = RichBlockModel(self)
        self.source = ""
        self._style: tuple[str, int] | None = None
        self._revision = 0
        self._offset = 0
        self._scan_offset = 0
        self._search_offset = 0
        self._fence = ""
        self._display = ""
        self._svg = False
        self._sealed: list[str] = []
        self._rendered: dict[str, str] = {}
        self._rich_candidates: set[int] = set()
        self._requested: set[int] = set()
        self._final = False
        self._tail = ""
        self._published_sealed = 0
        self._published_tail = ""
        # Virtualized QML delegates are destroyed off screen. Keep their last
        # text heights here so rebuilding them does not collapse a long row to
        # a fresh estimate and pull ListView back to an earlier scroll position.
        self._block_heights: dict[str, tuple[str, int, float]] = {}

    @Property(QObject, constant=True)
    def blocks(self):
        return self.model

    @Property(str, constant=True)
    def identityKey(self):
        return self.identity

    @Property(str, notify=sourceChanged)
    def sourceText(self):
        return self.source

    @Property(bool, notify=sourceChanged)
    def hasText(self):
        return bool(self.source)

    def update(self, text: str, *, streaming: bool, style: tuple[str, int]) -> None:
        text = str(text)
        final = not streaming
        if text == self.source and final == self._final and self._style == style:
            return
        changed = text != self.source
        reset = (self._style != style or not text.startswith(self.source)
                 or (self._final and changed))
        if reset:
            self._revision += 1
            self._offset = 0
            self._scan_offset = 0
            self._search_offset = 0
            self._fence = self._display = ""
            self._svg = False
            self._sealed.clear()
            self._rendered.clear()
            self._rich_candidates.clear()
            self._requested.clear()
            self._published_sealed = 0
            self._published_tail = ""
            self._block_heights.clear()
            self.model.update([])
            self.service.discard_pending(self.identity)
        self._style = style
        self.source = text
        self._final = final
        for piece in self._complete_paragraphs(text, final):
            for block, render in _display_blocks(piece):
                index = len(self._sealed)
                self._sealed.append(block)
                if render:
                    self._rich_candidates.add(index)
        self._tail = text[self._offset:]
        self._publish()
        if changed:
            self.sourceChanged.emit()

    @Slot(str)
    def requestRichBlock(self, key: str) -> None:
        """Only blocks whose text editor is visible enter the conversion queue."""
        if not key.startswith("complete:") or self._style is None:
            return
        try:
            index = int(key.split(":", 1)[1])
        except ValueError:
            return
        if index not in self._rich_candidates or index in self._requested:
            return
        self._requested.add(index)
        self.service.enqueue(_RenderJob(self.identity, self.instance_generation, key, self._revision,
                                        self._sealed[index], self._style[0], self._style[1]))

    def _complete_paragraphs(self, text: str, final: bool):
        """Scan each finished line once, including a very long open code fence."""
        while True:
            end = text.find("\n", self._search_offset)
            if end < 0:
                self._search_offset = len(text)
                break
            line = text[self._scan_offset:end + 1]
            stripped = line.strip()
            marker = re.match(r"^(`{3,}|~{3,})", stripped)
            if self._fence:
                if (marker and marker.group(1)[0] == self._fence[0]
                        and len(marker.group(1)) >= len(self._fence)
                        and not stripped[len(marker.group(1)):].strip()):
                    self._fence = ""
            elif marker:
                self._fence = marker.group(1)
            elif self._svg:
                if re.search(r"</svg\s*>", line, re.I):
                    self._svg = False
            elif self._display:
                if self._display in line:
                    self._display = ""
            else:
                svg_start = re.search(r"<svg\b", line, re.I)
                if svg_start and not re.search(r"</svg\s*>", line[svg_start.end():], re.I):
                    self._svg = True
                elif "\\[" in line and "\\]" not in line.split("\\[", 1)[1]:
                    self._display = "\\]"
                elif len(re.findall(r"(?<!\\)\$\$", line)) % 2:
                    self._display = "$$"
            self._scan_offset = end + 1
            self._search_offset = self._scan_offset
            if not stripped and not (self._fence or self._display or self._svg):
                yield text[self._offset:self._scan_offset]
                self._offset = self._scan_offset
        if final and self._offset < len(text):
            yield text[self._offset:]
            self._offset = len(text)
            self._scan_offset = len(text)
            self._search_offset = len(text)

    def _row(self, key: str, text: str, markup: str | None = None) -> dict[str, Any]:
        return {"key": key, "text": text, "html": markup or "", "rich": markup is not None,
                "layoutKey": f"{self._revision}:{key}:{len(text)}:{int(markup is not None)}",
                "lineLengths": [len(line) for line in text.split("\n")]}

    @Slot(str, str, int, result=float)
    def measuredBlockHeight(self, key: str, layout_key: str, width: int) -> float:
        value = self._block_heights.get(key)
        return value[2] if value and value[:2] == (layout_key, width) else -1.0

    @Slot(str, str, int, float)
    def rememberBlockHeight(self, key: str, layout_key: str, width: int, height: float) -> None:
        if key and width > 0 and 0 < height < 10_000_000:
            self._block_heights[key] = (layout_key, width, height)

    def _publish(self) -> None:
        rows = []
        start = self._published_sealed
        for index in range(start, len(self._sealed)):
            key = f"complete:{index}"
            rows.append(self._row(key, self._sealed[index], self._rendered.get(key)))
        stable_tail = 0
        if start == len(self._sealed) and self._tail.startswith(self._published_tail):
            stable_tail = len(self._published_tail) // _TAIL_PIECE
            start += stable_tail
        for index, text in enumerate(_plain_pieces(self._tail[stable_tail * _TAIL_PIECE:]), stable_tail):
            rows.append(self._row(f"tail:{index}", text))
        self.model.update_from(start, rows)
        self._published_sealed = len(self._sealed)
        self._published_tail = self._tail

    def accept(self, job: _RenderJob, markup: str) -> None:
        if (job.document != self.identity
                or job.instance_generation != self.instance_generation
                or job.revision != self._revision):
            return
        index = int(job.block.split(":", 1)[1])
        if index >= len(self._sealed) or self._sealed[index] != job.text:
            return
        self._rendered[job.block] = markup
        self.model.set_row(index, self._row(job.block, job.text, markup))


class PlainTextDocument(RichTextDocument):
    """Reasoning is literal text, with stable 4096-character layout blocks."""

    def update_plain(self, text: str) -> None:
        if text == self.source:
            return
        start = len(self.source) // _TAIL_PIECE if text.startswith(self.source) else 0
        if not start:
            self._revision += 1
            self._block_heights.clear()
        self.source = text
        rows = [self._row(f"plain:{index}", piece) for index, piece in
                enumerate(_plain_pieces(text[start * _TAIL_PIECE:]), start)]
        self.model.update_from(start, rows)
        self.sourceChanged.emit()


class RichRenderService(QObject):
    """One worker, bounded batches, and latest jobs only for each document block."""
    finished = Signal(object)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.documents: dict[str, RichTextDocument] = {}
        self._document_generation = 0
        self._pending: dict[tuple[str, str], _RenderJob] = {}
        self._live_pending: dict[tuple[str, str], _RenderJob] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-rich-text")
        self._working = False
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._dispatch)
        self.finished.connect(self._apply)

    def document(self, identity: str, text: str, *, streaming: bool, color: str = "") -> RichTextDocument:
        document = self.documents.get(identity)
        if document is None:
            document = RichTextDocument(self, identity)
            self.documents[identity] = document
        current_color, pixels = display_style()
        document.update(text, streaming=streaming, style=(color or current_color, max(14, pixels)))
        return document

    def plain_document(self, identity: str, text: str) -> PlainTextDocument:
        document = self.documents.get(identity)
        if document is None:
            document = PlainTextDocument(self, identity)
            self.documents[identity] = document
        document.update_plain(str(text))
        return document

    def discard_pending(self, identity: str) -> None:
        self._pending = {key: job for key, job in self._pending.items() if key[0] != identity}
        self._live_pending = {key: job for key, job in self._live_pending.items() if key[0] != identity}

    def enqueue(self, job: _RenderJob) -> None:
        if self._closed:
            return
        key = (job.document, job.block)
        document = self.documents.get(job.document)
        live = document is not None and not document._final
        target = self._live_pending if live else self._pending
        other = self._pending if live else self._live_pending
        other.pop(key, None)
        target[key] = job
        if not self._working and not self._timer.isActive():
            self._timer.start()

    def retain(self, identities: set[str]) -> None:
        removed = self.documents.keys() - identities
        if not removed:
            return
        self._pending = {key: job for key, job in self._pending.items() if key[0] not in removed}
        self._live_pending = {key: job for key, job in self._live_pending.items() if key[0] not in removed}
        for identity in removed:
            self.documents.pop(identity).deleteLater()

    @Slot()
    def _dispatch(self) -> None:
        if self._working or self._closed or not (self._pending or self._live_pending):
            return
        jobs = []
        size = 0
        # Two insertion-ordered queues avoid sorting the entire remaining
        # history on every small batch, while an active stream gets priority.
        while self._pending or self._live_pending:
            pending = self._live_pending if self._live_pending else self._pending
            job = pending.pop(next(iter(pending)))
            jobs.append(job)
            size += len(job.text)
            if len(jobs) >= 8 or size >= 32768:
                break
        self._working = True
        future = self._pool.submit(self._convert, jobs)

        def completed(future):
            try:
                result = future.result()
            except Exception:
                result = [(job, "") for job in jobs]
            if not self._closed:
                try:
                    self.finished.emit(result)
                except RuntimeError:
                    pass  # Window closed while a bounded conversion was completing.
        future.add_done_callback(completed)

    @staticmethod
    def _convert(jobs):
        result = []
        for job in jobs:
            try:
                markup = safe_rich_text(job.text, color=job.color, font_pixels=job.pixels)
            except Exception:
                markup = ""  # Keep selectable original text when conversion fails.
            result.append((job, markup))
        return result

    @Slot(object)
    def _apply(self, result) -> None:
        self._working = False
        if self._closed:
            return
        for job, markup in result:
            document = self.documents.get(job.document)
            if (document is not None and markup
                    and job.instance_generation == document.instance_generation):
                document.accept(job, markup)
        if self._pending or self._live_pending:
            self._timer.start()

    def close(self) -> None:
        self._closed = True
        self._timer.stop()
        self._pending.clear()
        self._live_pending.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
