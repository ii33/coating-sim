"""Cross-platform, item-local magnifying-glass cursor for image previews.

No global cursor override or pointer handler is installed. Text selection,
links, wheel scrolling and window dragging keep their normal event paths.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Property, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickItem, QQuickWindow

_REGISTERED = False
_CURSORS: dict[float, QCursor] = {}


def _target_device_ratio(target: QQuickItem) -> float:
    # QQuickItem.window() on the verified PySide6 6.12.0 build creates a
    # binding-only window/item ownership cycle. Find the actual visual surface
    # without returning a QObject from an item method, including Popup.Window.
    for window in QGuiApplication.allWindows():
        if isinstance(window, QQuickWindow):
            content = window.contentItem()
            if content is target or content.isAncestorOf(target):
                return window.devicePixelRatio()
    return 1.0


def _magnifier(device_ratio: float) -> QCursor:
    ratio = max(1.0, min(4.0, round(float(device_ratio), 2)))
    if ratio not in _CURSORS:
        pixmap = QPixmap(round(32 * ratio), round(32 * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # White outer stroke and dark inner stroke stay legible on either theme.
        for color, width in ((QColor("#ffffff"), 5), (QColor("#193342"), 2.5)):
            painter.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawEllipse(4, 4, 17, 17)
            painter.drawLine(19, 19, 28, 28)
        painter.setPen(QPen(QColor("#193342"), 1.8, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawLine(9, 12, 16, 12)
        painter.drawLine(12, 9, 12, 16)
        painter.end()
        _CURSORS[ratio] = QCursor(pixmap, 12, 12)
    return _CURSORS[ratio]


class PreviewCursor(QObject):
    """QML cursor controller; use active only for an actual previewable image."""
    targetChanged = Signal()
    activeChanged = Signal()
    normalShapeChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._target: QQuickItem | None = None
        self._active = False
        self._normal_shape = Qt.CursorShape.ArrowCursor.value
        self._pending = False
        self._owns_cursor = False

    def _schedule(self) -> None:
        # Qt's TextEdit changes its standard cursor within the same hover
        # event. Apply after that event, once, so the image cursor wins without
        # fighting a normal link/text transition or installing an input overlay.
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._apply)

    def _apply(self) -> None:
        self._pending = False
        target = self._target
        if target is None or QGuiApplication.instance() is None:
            return
        try:
            if self._active:
                target.setCursor(_magnifier(_target_device_ratio(target)))
                self._owns_cursor = True
            elif self._owns_cursor:
                target.setCursor(QCursor(Qt.CursorShape(self._normal_shape)))
                self._owns_cursor = False
        except RuntimeError:
            # A virtualized transcript delegate can be destroyed before the
            # queued apply; never retain/restore a cursor on a deleted item.
            self._target = None
            self._owns_cursor = False

    @Property(QQuickItem, notify=targetChanged)
    def target(self) -> QQuickItem | None:
        return self._target

    @target.setter
    def target(self, value: QQuickItem | None) -> None:
        if self._target is value:
            return
        if self._target is not None and self._owns_cursor:
            try:
                self._target.unsetCursor()
            except RuntimeError:
                pass
        self._target = value
        self._owns_cursor = False
        self.targetChanged.emit()
        self._schedule()

    @Property(bool, notify=activeChanged)
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        if self._active != bool(value):
            self._active = bool(value)
            self.activeChanged.emit()
            self._schedule()

    @Property(int, notify=normalShapeChanged)
    def normalShape(self) -> int:
        return self._normal_shape

    @normalShape.setter
    def normalShape(self, value: int) -> None:
        if self._normal_shape != int(value):
            self._normal_shape = int(value)
            self.normalShapeChanged.emit()
            self._schedule()


def register_preview_cursor() -> None:
    global _REGISTERED
    if not _REGISTERED:
        qmlRegisterType(PreviewCursor, "CoatUi", 1, 0, "PreviewCursor")
        _REGISTERED = True
