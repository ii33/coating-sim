"""Assistant-created global materials must open from the asset library.

The assistant (MCP create_material) writes to the same global material library
directory from a separate process. A material that already appears in the asset
list must still open in the shared material editor even when that editor's own
cached catalog was read before the external write.
"""
from __future__ import annotations

import pytest
from PySide6.QtTest import QTest

from coat_sim.core.material_business import MaterialLibrary
from tests.test_asset_library_actions import global_assets as global_assets
from tests.test_launcher_information_global_resources import global_root as global_root
from tests.test_project_global_resources import profile
from tests.test_project_last_window_close import spin_until
from tests.test_scoped_resource_libraries import settled

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def _library(global_root):
    # Same owner the assistant create_material tool resolves: the global library.
    return MaterialLibrary(directory=global_root / "data" / "materials")


def _material_row(assets, material_id):
    return next(row for row in assets.transferCatalog["rows"]
                if row["kind"] == "material_profile" and row.get("material_id") == material_id)


def test_external_assistant_material_opens_after_list_refresh(global_assets, global_root):
    _window, assets, _engine = global_assets
    _library(global_root).write(profile("seeded"))
    assert assets.browseLibrary()
    settled(assets)
    seeded = _material_row(assets, "seeded")
    assert assets.openMaterialAsset(seeded["id"])
    spin_until(lambda: assets.materialLibrary.loading, lambda busy: not busy, "editor catalog")
    assert assets.materialLibrary.currentId == "seeded"

    # The assistant creates another material out of process, then the list is
    # refreshed (for example by reopening the asset library).
    _library(global_root).write(profile("assistant_new"))
    assert assets.browseLibrary()
    settled(assets)
    row = _material_row(assets, "assistant_new")

    assert assets.openMaterialAsset(row["id"])
    spin_until(lambda: assets.materialLibrary.loading, lambda busy: not busy, "assistant material load")
    QTest.qWait(40)
    assert assets.materialLibrary.currentId == "assistant_new"
    assert assets.materialLibrary.draftData()["id"] == "assistant_new"


def test_open_missing_material_reports_visible_error(global_assets, global_root):
    _window, assets, _engine = global_assets
    _library(global_root).write(profile("present"))
    assert assets.browseLibrary()
    settled(assets)
    row = _material_row(assets, "present")
    # A row whose backing file no longer exists (removed after the list read).
    path = global_root / "data" / "materials" / "present.json"
    assert path.exists()
    path.unlink()
    # The click may schedule one catalog re-read; either way the material must
    # not end up open and the user must see an explicit message.
    assets.openMaterialAsset(row["id"])
    spin_until(lambda: assets.materialLibrary.loading, lambda busy: not busy, "missing material read")
    QTest.qWait(40)
    assert not assets.materialLibrary.currentId
    assert "材料" in assets.deletionMessage
