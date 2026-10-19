"""Asset-library actions stay in one row and overflow at narrow widths."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QMetaObject, QObject
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_asset_library_actions import (
    global_assets as global_assets, seed_global_material, seed_project_material,
)
from tests.test_launcher_information_global_resources import global_root as global_root, seed_source
from tests.test_project_last_window_close import desktop as desktop, spin_until
from tests.test_scoped_resource_libraries import open_library, settled


pytestmark = [pytest.mark.qt, pytest.mark.contract]


def test_asset_library_actions_are_single_row_with_live_overflow(global_assets, global_root):
    window, assets, engine = global_assets
    seed_global_material(global_root)
    seed_source(global_root)
    assert assets.browseLibrary()
    settled(assets)
    window.setProperty("categoryKey", "material_profile")
    QTest.qWait(40)
    bar = window.findChild(QQuickItem, "resourceLibraryOperationBar")
    assert bar is not None
    action_names = (
        "globalResourcesRefreshButton",
        "globalResourcesImportButton", "globalResourcesExportButton",
        "assetLibraryDeleteButton", "assetLibraryNewMaterialButton",
    )
    buttons = [window.findChild(QQuickItem, name) for name in action_names]
    assert all(button is not None for button in buttons)
    assert window.findChild(QQuickItem, "assetLibraryRecycleButton") is None
    assert window.findChild(QQuickItem, "globalResourcesMaterialsButton") is None
    assert window.findChild(QObject, "assetDeletionHistoryDialog") is None
    assert window.findChild(QObject, "assetRestoreDialog") is None
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
            if width == 680 and font_scale == 2.0:
                assert overflowed
                assert more.x() + more.width() <= bar.width() + 0.5
                assert all(button.x() + button.width() <= more.x() + 0.5
                           for button in buttons if button.isVisible() and button not in overflowed)
                more.clicked.emit()
                QTest.qWait(40)
                assert any(window.findChild(QObject, "operationBarOverflow_" + button.objectName())
                           is not None for button in overflowed)


def test_import_button_opens_file_picker_without_choice_menu(global_assets):
    window, _assets, _engine = global_assets
    window.resize(1100, 760)
    QTest.qWait(40)
    picker = window.findChild(QObject, "globalResourcesImportDialog")
    button = window.findChild(QQuickItem, "globalResourcesImportButton")
    assert picker is not None and button is not None
    expression = QQmlExpression(QQmlEngine.contextForObject(picker), picker,
                                "FileDialog.DontUseNativeDialog")
    value = expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    if isinstance(value, tuple):
        value = value[0]
    dont_use_native = int(value.toInt() if hasattr(value, "toInt") else value)
    assert picker.setProperty("options", dont_use_native)
    button.clicked.emit()
    QTest.qWait(40)
    assert picker.property("visible")
    assert window.findChild(QObject, "assetImportFileAction") is None
    assert window.findChild(QObject, "projectResourcesImportGlobalButton") is None
    assert window.findChild(QObject, "assetImportMaterialFileAction") is None
    assert QMetaObject.invokeMethod(picker, "reject")
    QTest.qWait(20)
    assert not picker.property("visible")


def test_material_new_and_save_are_adjacent_on_the_material_page_in_both_scopes(
        global_assets, global_root, desktop):
    window, assets, _engine = global_assets
    record = seed_global_material(global_root)
    seed_source(global_root)
    assert assets.browseLibrary()
    settled(assets)
    window.setProperty("categoryKey", "source")
    QTest.qWait(30)
    global_new = window.findChild(QQuickItem, "assetLibraryNewMaterialButton")
    global_save = window.findChild(QQuickItem, "assetLibrarySaveMaterialButton")
    assert global_new is not None and global_save is not None
    assert not global_new.isVisible() and not global_save.isVisible()

    window.setProperty("categoryKey", "material_profile")
    identity = next(row["id"] for row in assets.transferCatalog["rows"]
                    if row["kind"] == "material_profile" and row["material_id"] == record["id"])
    assert assets.openMaterialAsset(identity)
    spin_until(lambda: assets.materialLibrary.loading, lambda busy: not busy, "global material editor")
    assert global_new.isVisible() and global_save.isVisible()

    project = desktop.open()
    project_row = seed_project_material(desktop, project)
    _instance, _route, (project_window, project_assets, *_rest) = open_library(desktop, project)
    project_window.setProperty("categoryKey", "material_profile")
    project_new = project_window.findChild(QQuickItem, "assetLibraryNewMaterialButton")
    project_save = project_window.findChild(QQuickItem, "assetLibrarySaveMaterialButton")
    assert project_new is not None and project_save is not None
    assert project_new.isVisible() and not project_save.isVisible()
    assert project_assets.openMaterialAsset(project_row["id"])
    spin_until(lambda: project_assets.materialLibrary.loading, lambda busy: not busy, "project material editor")
    assert project_new.isVisible() and project_save.isVisible()
    assert project_window.findChild(QQuickItem, "assetLibraryRecycleButton") is None
    assert project_window.findChild(QQuickItem, "globalResourcesMaterialsButton") is None

    for target in (window, project_window):
        target.resize(1600, max(800, target.minimumHeight()))
        QTest.qWait(80)
        bar = target.findChild(QQuickItem, "resourceLibraryOperationBar")
        save = target.findChild(QQuickItem, "assetLibrarySaveMaterialButton")
        new = target.findChild(QQuickItem, "assetLibraryNewMaterialButton")
        assert bar is not None and save is not None and new is not None
        assert save.isVisible() and new.isVisible()
        gap = new.x() - (save.x() + save.width())
        assert abs(gap - bar.property("spacing")) <= 1.0, (gap, bar.property("spacing"))
