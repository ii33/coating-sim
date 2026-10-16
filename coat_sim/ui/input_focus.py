"""Passive, application-local input focus policy for Qt Quick windows.

No input overlay, wheel/key interception or operating-system input injection is
used. Each window owns its observer and timers; editor references stay weak.
"""
from __future__ import annotations

import weakref

from PySide6.QtCore import QEvent, QMetaObject, QObject, QPointF, QTimer, Qt, Slot
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtQuick import QQuickItem, QQuickWindow
from shiboken6 import isValid


def _resolve(reference: weakref.ReferenceType[QQuickItem] | None) -> QQuickItem | None:
    item = reference() if reference is not None else None
    return item if item is not None and isValid(item) else None


def _editable(item: QQuickItem | None) -> bool:
    return bool(item is not None and isValid(item)
                and (item.inherits("QQuickTextInput") or item.inherits("QQuickTextEdit"))
                and not item.property("readOnly") and item.isEnabled())


class WindowInputFocusObserver(QObject):
    """Observe one window without consuming or changing event delivery."""

    def __init__(self, window: QQuickWindow) -> None:
        super().__init__(window)
        self._window = weakref.ref(window)
        self._pressed = False
        self._press_position = QPointF()
        self._dragged = False
        self._pressed_editor: weakref.ReferenceType[QQuickItem] | None = None
        self._selection: weakref.ReferenceType[QQuickItem] | None = None
        self._blur: weakref.ReferenceType[QQuickItem] | None = None
        self._accepted_connection: QMetaObject.Connection | None = None
        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.timeout.connect(self._select)
        self._blur_timer = QTimer(self)
        self._blur_timer.setSingleShot(True)
        self._blur_timer.timeout.connect(self._release_focus)
        window.installEventFilter(self)
        window.activeFocusItemChanged.connect(self._focus_changed)
        self._focus_changed()

    def _active_editor(self) -> QQuickItem | None:
        window = self._window()
        if window is None or not isValid(window):
            return None
        item = window.activeFocusItem()
        return item if _editable(item) else None

    @Slot()
    def _focus_changed(self) -> None:
        self._selection_timer.stop()
        self._selection = None
        if self._accepted_connection:
            QObject.disconnect(self._accepted_connection)
        self._accepted_connection = None
        editor = self._active_editor()
        if editor is None or editor.property("sharedInputFocusManaged"):
            return
        self._selection = weakref.ref(editor)
        if not self._pressed:
            self._selection_timer.start(0)
        accepted = getattr(editor, "accepted", None)
        if accepted is not None:
            self._accepted_connection = accepted.connect(self._accepted)

    @Slot()
    def _select(self) -> None:
        editor = _resolve(self._selection)
        self._selection = None
        if (editor is not None and editor is self._active_editor()
                and not editor.property("inputMethodComposing")):
            QMetaObject.invokeMethod(editor, "selectAll")

    @Slot()
    def _accepted(self) -> None:
        editor = self._active_editor()
        if editor is not None and not editor.property("inputMethodComposing"):
            self._blur = weakref.ref(editor)
            self._blur_timer.start(0)

    @Slot()
    def _release_focus(self) -> None:
        editor = _resolve(self._blur)
        self._blur = None
        if (editor is None or editor is not self._active_editor()
                or editor.property("inputMethodComposing")):
            return
        owner = editor.property("sharedInputFocusOwner")
        parent = editor.parentItem()
        if isinstance(owner, QQuickItem) and isValid(owner):
            owner.setFocus(False)
        elif parent is not None and (parent.inherits("QQuickSpinBox")
                                     or parent.inherits("QQuickComboBox")):
            parent.setFocus(False)
        else:
            editor.setFocus(False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate,
                            QEvent.Type.Hide, QEvent.Type.Close):
            # A native window move/menu can finish outside this Qt surface.
            # Forget that pointer sequence without moving or restoring focus.
            self._pressed = False
            self._pressed_editor = None
            self._selection = None
            self._selection_timer.stop()
        if not isinstance(event, QMouseEvent):
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._dragged = False
            self._press_position = event.position()
            editor = self._active_editor()
            self._pressed_editor = weakref.ref(editor) if editor is not None else None
        elif self._pressed and kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
            delta = event.position() - self._press_position
            threshold = QGuiApplication.styleHints().startDragDistance()
            if delta.manhattanLength() > threshold:
                self._dragged = True
            if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._pressed = False
                if not self._dragged:
                    if self._selection is not None:
                        self._selection_timer.start(0)
                    editor = _resolve(self._pressed_editor)
                    if (editor is not None and editor is self._active_editor()
                            and not editor.contains(editor.mapFromScene(event.position()))):
                        # Native release, validation, button/menu selection and
                        # any focus change happen before this owned timer runs.
                        self._blur = self._pressed_editor
                        self._blur_timer.start(0)
                else:
                    self._selection = None
                self._pressed_editor = None
        return False


class _InputFocusInstaller(QObject):
    """Lazily attach window-owned observers to this application's Qt windows."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (isinstance(watched, QQuickWindow)
                and event.type() in (QEvent.Type.Show, QEvent.Type.FocusIn,
                                     QEvent.Type.MouseButtonPress)):
            observer = getattr(watched, "_coat_input_focus_observer", None)
            if observer is None or not isValid(observer):
                # The window retains the Python event-filter wrapper. The
                # observer itself retains only a weak reference to the window.
                watched.__dict__["_coat_input_focus_observer"] = WindowInputFocusObserver(watched)
        return False


def install_input_focus_policy() -> None:
    """Install once after QApplication construction, before showing windows."""
    app = QGuiApplication.instance()
    if app is None:
        return
    installer = getattr(app, "_coat_input_focus_installer", None)
    if installer is None or not isValid(installer):
        installer = _InputFocusInstaller(app)
        app.__dict__["_coat_input_focus_installer"] = installer
        app.installEventFilter(installer)
