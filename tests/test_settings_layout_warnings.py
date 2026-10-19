"""Real settings-page loading and disclosure switching must not emit layout warnings."""
import pytest
from PySide6.QtCore import qInstallMessageHandler
from tests.test_compute_server_category_ui import compute_ui as compute_ui, _find, _settle

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def sections(window):
    pending = [window.contentItem()]
    result = []
    while pending:
        item = pending.pop()
        if item.property("expanded") is not None and item.property("title"):
            result.append(item)
        pending.extend(item.childItems())
    return result


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("advanced", [False, True])
def test_settings_initial_load_and_category_switching_has_no_anchor_layout_warnings(compute_ui, mode, advanced):
    messages = []
    previous = None
    def capture(kind, context, message):
        messages.append(message)
        if previous is not None:
            previous(kind, context, message)
    previous = qInstallMessageHandler(capture)
    try:
        compute_ui.service.configure(mode=mode, font_scale=1.25)
        scene = compute_ui.open()
        for width in (560, 960):
            scene.window.resize(width, 760)
            _settle()
            _find(scene.window, "computeServerSettingsCategory").setProperty("expanded", True)
            if advanced:
                _find(scene.window, "settingsAdvancedCategory").setProperty("expanded", True)
            _settle()
            categories = sections(scene.window)
            assert len(categories) >= (10 if advanced else 3)
            if not advanced:
                categories = [row for row in categories if row.objectName() != "settingsAdvancedCategory"]
            for category in categories:
                category.setProperty("expanded", False)
            _settle()
            for category in reversed(categories):
                category.setProperty("expanded", True)
            _settle()
            field = _find(scene.window, "fontFamilyField")
            assert field.width() > 30 and field.height() > 0
            assert not scene.window.grabWindow().isNull()
        scene.close()
        reopened = compute_ui.open()
        assert _find(reopened.window, "fontFamilyField").width() > 30
        bad = [message for message in messages if "Detected anchors on an item that is managed by a layout" in message
               or ("SettingsWindow.qml" in message and "Binding loop" in message)]
        assert not bad, "\n".join(bad)
        assert not scene.warnings and not reopened.warnings
    finally:
        qInstallMessageHandler(previous)
