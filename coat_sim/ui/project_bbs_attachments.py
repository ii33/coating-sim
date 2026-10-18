"""Bounded, offline attachment previews for the captured BBS project session."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import re
from threading import Lock
import uuid

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QUrl, Slot
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtQml import qmlEngine
from PySide6.QtQuick import QQuickImageProvider

from coat_sim.core.project_bbs_attachments import (
    MAX_PREVIEW_TEXT_BYTES, export_attachment, import_attachment, preview_attachment,
)
from coat_sim.core.project_dependencies import ProjectError

MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192
PREVIEW_IMAGE_SIDE = 1200
MAX_CACHED_IMAGES = 8
_IMAGE_ID = re.compile(r"[0-9a-f]{32}\Z")


class _PreviewImages:
    """Opaque capabilities map to already decoded images, never disk or URLs."""
    def __init__(self):
        self.lock = Lock()
        self.values: OrderedDict[str, tuple[tuple[str, str], QImage]] = OrderedDict()
        self.closed = False
        self.revoked: set[tuple[str, str]] = set()

    def put(self, binding, image: QImage) -> str:
        identity = uuid.uuid4().hex
        with self.lock:
            self._admit(binding)
            self.values[identity] = ((binding.project_id, binding.generation), image.copy())
            while len(self.values) > MAX_CACHED_IMAGES:
                self.values.popitem(last=False)
        return "image://projectbbs/" + identity

    def _admit(self, binding):
        if self.closed or (binding.project_id, binding.generation) in self.revoked:
            raise ProjectError("project_closed", "Attachment preview session has closed")

    def admit(self, binding):
        with self.lock:
            self._admit(binding)

    def get(self, identity: str) -> QImage:
        if not _IMAGE_ID.fullmatch(identity):
            return QImage()
        with self.lock:
            value = self.values.get(identity)
            return value[1].copy() if value is not None else QImage()

    def forget(self, binding=None):
        with self.lock:
            if binding is None:
                self.closed = True
                self.values.clear()
            else:
                key = (binding.project_id, binding.generation)
                self.revoked.add(key)
                self.values = OrderedDict((identity, value) for identity, value in self.values.items()
                                          if value[0] != key)


class ProjectBbsImageProvider(QQuickImageProvider):
    def __init__(self, cache):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.cache = cache

    def requestImage(self, image_id, size, requested_size):
        image = self.cache.get(image_id)
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        # Never upscale a caller-controlled requested size or allocate from a URL.
        if (not image.isNull() and requested_size.isValid()
                and 0 < requested_size.width() <= PREVIEW_IMAGE_SIDE
                and 0 < requested_size.height() <= PREVIEW_IMAGE_SIDE):
            image = image.scaled(requested_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return image


def _selected_file(url: str) -> Path:
    if not isinstance(url, str) or len(url) > 8192:
        raise ProjectError("unsafe_path", "Select an ordinary local file")
    value = QUrl(url)
    if (not value.isValid() or value.scheme() != "file" or not value.isLocalFile()
            or value.host() or value.hasQuery() or value.hasFragment()):
        raise ProjectError("unsafe_path", "Attachment dialogs accept only local file URLs")
    path = Path(value.toLocalFile())
    if not path.is_absolute():
        raise ProjectError("unsafe_path", "Attachment dialog path must be absolute")
    return path


def _decode_image(raw: bytes) -> QImage:
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    size = reader.size()
    if (not size.isValid() or size.width() > MAX_IMAGE_SIDE or size.height() > MAX_IMAGE_SIDE
            or size.width() * size.height() > MAX_IMAGE_PIXELS):
        raise ProjectError("capacity_exceeded", "Image preview dimensions exceed the safe decoding limit")
    scaled = size.scaled(QSize(PREVIEW_IMAGE_SIDE, PREVIEW_IMAGE_SIDE), Qt.AspectRatioMode.KeepAspectRatio)
    reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        raise ProjectError("bbs_invalid_attachment", "Image cannot be decoded")
    # A decoder which ignores its scaledSize must still return a bounded image.
    if image.width() > PREVIEW_IMAGE_SIDE or image.height() > PREVIEW_IMAGE_SIDE:
        image = image.scaled(PREVIEW_IMAGE_SIDE, PREVIEW_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image


def _pdf_text(raw: bytes, page: int) -> dict:
    # The installed pypdf reader already runs in a resource-limited process.
    # No PDF actions, network requests, embedded programs or OCR are executed.
    from coat_sim.agent.research_fulltext import parse_fulltext
    parsed = parse_fulltext(raw, format="pdf")
    if not parsed.get("ok"):
        return {"kind": "unsupported", "text": "", "page": 0, "page_count": 0,
                "message": "PDF 文字预览不可用（" + str(parsed.get("error", "parse_failed"))
                           + "），可导出后打开。", "truncated": False}
    segments = parsed.get("segments", [])
    if not segments:
        return {"kind": "unsupported", "text": "", "page": 0, "page_count": 0,
                "message": "PDF 没有可预览页面，可导出后打开。", "truncated": False}
    if page >= len(segments):
        raise ProjectError("bbs_invalid_arguments", "PDF preview page is out of range")
    raw_text = str(segments[page].get("text", "")).encode("utf-8")
    text = raw_text[:MAX_PREVIEW_TEXT_BYTES].decode("utf-8", errors="ignore")
    return {"kind": "pdf_text", "text": text, "page": page, "page_count": len(segments),
            "truncated": len(raw_text) > MAX_PREVIEW_TEXT_BYTES,
            "message": "PDF 文字预览；不保留页面版式。" if text else "本页没有文字层；不自动执行 OCR，可导出后打开。"}


class ProjectBbsAttachmentBridge(QObject):
    """Coordinator owns admission, serialization and post-result generation checks.

    ``execute`` must run inside its captured host operation scope. Only the GUI
    picker uses source/destination URLs; model calls never invoke these slots.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.images = _PreviewImages()
        self._engines = set()

    @Slot(QObject, result=bool)
    def install(self, element):
        engine = qmlEngine(element)
        if engine is None:
            return False
        identity = id(engine)
        if identity not in self._engines:
            engine.addImageProvider("projectbbs", ProjectBbsImageProvider(self.images))
            self._engines.add(identity)
            engine.destroyed.connect(lambda: self._engines.discard(identity))
        return True

    def forget(self, binding):
        self.images.forget(binding)

    def clear(self):
        self.images.forget()

    def execute(self, store, binding, operation: str, payload: dict) -> dict:
        self.images.admit(binding)
        if store.manifest["id"] != binding.project_id:
            raise ProjectError("project_identity_mismatch", "Attachment project binding differs")
        if operation == "attachment_import":
            if set(payload) != {"source_url"}:
                raise ProjectError("bbs_invalid_arguments", "Invalid attachment import arguments")
            return {"attachment": import_attachment(store, _selected_file(payload["source_url"]))}
        if operation == "attachment_export":
            if set(payload) != {"attachment", "destination_url"}:
                raise ProjectError("bbs_invalid_arguments", "Invalid attachment export arguments")
            export_attachment(store, payload["attachment"], _selected_file(payload["destination_url"]))
            return {"exported": True}
        if operation != "attachment_preview" or set(payload) - {"attachment", "page"} or "attachment" not in payload:
            raise ProjectError("bbs_invalid_arguments", "Invalid attachment preview arguments")
        page = payload.get("page", 0)
        if type(page) is not int or not 0 <= page < 200:
            raise ProjectError("bbs_invalid_arguments", "Invalid preview page")
        result = preview_attachment(store, payload["attachment"])
        raw = result.pop("bytes", b"")
        result.update(asset_id=payload["attachment"]["asset_id"], image_source="", page=0, page_count=0)
        if result["kind"] == "image":
            result["image_source"] = self.images.put(binding, _decode_image(raw))
        elif result["kind"] == "pdf":
            result.update(_pdf_text(raw, page))
        return result
