"""Asset-library actions stay in one row and overflow at narrow widths."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QObject
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_asset_library_actions import global_assets as global_assets
from tests.test_launcher_information_global_resources import global_root as global_root


pytestmark = [pytest.mark.qt, pytest.mark.contract]


def test_asset_library_actions_are_single_row_with_live_overflow(global_assets):
    window, _assets, engine = global_assets
    bar = window.findChild(QQuickItem, "resourceLibraryOperationBar")
    assert bar is not None
    action_names = (
        "globalResourcesRefreshButton", "refractiveIndexDatabaseButton",
        "globalResourcesImportButton", "globalResourcesExportButton",
        "assetLibraryDeleteButton", "assetLibraryRecycleButton",
        "globalResourcesMaterialsButton", "assetLibraryNewMaterialButton",
    )
    buttons = [window.findChild(QQuickItem, name) for name in action_names]
    assert all(button is not None for button in buttons)
    theme = engine.rootContext().contextProperty("Theme")
    for font_scale, density_scale in ((1.0, 1.0), (2.0, 1.5)):
        theme._replace_snapshot(build_theme_snapshot("light", font_scale=font_scale,
                                density_scale=density_scale), 1, "light", "light", "qa", "QA", {})
        for width in (680, 760, 1100, 1500):
            window.resize(width, 760)
            QTest.qWait(90)
            assert bar.width() <= window.width()
            assert bar.height() <= max(button.height() for button in buttons) + 1
            overflowed = list(bar.property("overflowItems").toVariant())
            more = window.findChild(QQuickItem, "operationBarOverflowButton")
            assert more is not None
            assert more.isVisible() == bool(overflowed)
            for button in buttons:
                if not button.isVisible() or button in overflowed:
                    continue
                assert button.y() >= -0.5
                assert button.y() + button.height() <= bar.height() + 0.5
                assert button.x() >= -0.5
                assert button.x() + button.width() <= bar.width() + 0.5
            if width == 680:
                assert overflowed
                assert more.x() + more.width() <= bar.width() + 0.5
                assert all(button.x() + button.width() <= more.x() + 0.5
                           for button in buttons if button.isVisible() and button not in overflowed)
                more.clicked.emit()
                QTest.qWait(40)
                assert window.findChild(QObject, "operationBarOverflow_assetLibraryNewMaterialButton") is not None
