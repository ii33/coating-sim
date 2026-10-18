"""Offline actual chat-window body visibility, beyond model-only contracts."""
from pathlib import Path
import pytest
from PySide6.QtCore import QObject, QUrl, QCoreApplication, QEvent, QPointF, Property, Signal
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from tests.test_assistant_token_budget_contract import real_bridge as _real_bridge_fixture
from coat_sim.ui.theme_tokens import build_theme_snapshot
from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui.preview_cursor import register_preview_cursor

real_bridge = _real_bridge_fixture
pytestmark = [pytest.mark.qt, pytest.mark.contract]

class _Theme(QObject):
    snapshotChanged = Signal()

    def __init__(self, mode):
        super().__init__()
        self._snapshot = build_theme_snapshot(mode)

    @Property(dict, notify=snapshotChanged)
    def snapshot(self):
        return self._snapshot


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)

@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_real_chat_body_is_painted(real_bridge, monkeypatch, tmp_path, mode):
    bridge, _worker = real_bridge
    monkeypatch.setattr(bridge, 'refreshProjectContexts', lambda: None)
    bundled_font_family()
    register_preview_cursor()
    engine = QQmlEngine()
    theme = _Theme(mode)
    engine.rootContext().setContextProperty('assistant', bridge)
    engine.rootContext().setContextProperty('Theme', theme)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parents[1] / 'coat_sim/ui/qml/AssistantChatWindow.qml')))
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errors()
    try:
        bridge._messages = [
            {'id':'qa-user','role':'user','content':'USER BODY VISIBLE','metadata':{}},
            {'id':'qa-assistant','role':'assistant','content':'ASSISTANT **BODY VISIBLE**\n\n```python\nprint("CODE VISIBLE")\n```','metadata':{}},
            {'id':'qa-error','role':'assistant','content':'ERROR stale_project_binding','metadata':{'status':'error'}},
        ]
        bridge._stable_turns = None
        bridge.transcriptChanged.emit()
        QTest.qWait(100)
        window.resize(860, 1000)
        window.show()
        QTest.qWait(400)
        editors = [obj for obj in visual_items(window.contentItem())
                   if 'QQuickTextEdit' in obj.metaObject().className()
                   and obj.isVisible() and obj.property('renderedSource')]
        sources = [x.property('renderedSource') for x in editors]
        for expected in ('USER BODY VISIBLE', 'ASSISTANT', 'CODE VISIBLE',
                         'ERROR stale_project_binding'):
            assert any(expected in source for source in sources), sources
        # A nonempty model/TextEdit is insufficient: compare the actual scene
        # with each body editor hidden. Headers, footers and avatars cannot
        # satisfy this assertion, and foreground matching background fails it.
        for index, editor in enumerate(editors):
            point = editor.mapToItem(window.contentItem(), QPointF(0, 0))
            dpr = window.devicePixelRatio()
            rect = (round(point.x() * dpr), round(point.y() * dpr),
                    round(editor.width() * dpr), round(editor.height() * dpr))
            assert rect[2] > 0 and rect[3] > 0
            assert point.y() >= 0 and point.y() + editor.height() < window.height()
            visible = window.grabWindow().copy(*rect)
            editor.setOpacity(0)
            QTest.qWait(30)
            hidden = window.grabWindow().copy(*rect)
            editor.setOpacity(1)
            QTest.qWait(30)
            changed = sum(visible.pixel(x, y) != hidden.pixel(x, y)
                          for y in range(visible.height())
                          for x in range(visible.width()))
            assert changed > 20, (mode, index, sources[index], changed)
        assert window.grabWindow().save(str(tmp_path / ('chat-body-'+mode+'.png')))
    finally:
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
