"""Chosen-file import admission and terminal library diagnostics."""
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_package import export_project
from coat_sim.ui.project_resource_adapter import ProjectResourceTransferController
from coat_sim.ui.resource_library import ResourceLibraryBridge

pytestmark = [pytest.mark.contract, pytest.mark.qt]


@pytest.fixture
def app():
    value = QGuiApplication.instance() or QGuiApplication([])
    yield value


def settle(controller):
    for _ in range(600):
        QTest.qWait(5)
        if not controller.transferBusy:
            return
    pytest.fail(controller.transferStatus)


@pytest.mark.parametrize("container", ["coatproj", "cproject", "csrc"])
def test_chosen_file_import_inspects_directly_and_preserves_designer_kind(app, tmp_path, container):
    donor = ProjectStore.create(tmp_path / "donor", "Donor")
    donor.create_document("source", {"schema_version": 2, "kind": "point"})
    donor.create_document("substrate", {"schema_version": 1, "name": "Disk",
        "outline": {"kind": "circle", "radius_mm": 10}, "surface": {"kind": "flat"}})
    path = donor.project_file
    if container == "cproject":
        path = tmp_path / "donor.cproject"
        export_project(donor, path)
    elif container == "csrc":
        path = tmp_path / "source.csrc"
        path.write_text('{"schema_version": 2, "kind": "point"}', encoding="utf-8")
    target = ProjectStore.create(tmp_path / "target", "Target")
    service = ProjectResourceService(target)
    calls = []
    original = service.inspect_resources
    def inspect(source):
        calls.append(source)
        return original(source)
    controller = ProjectResourceTransferController()
    controller.service, controller.module_key = service, "source"
    try:
        with patch.object(service, "inspect_resources", side_effect=inspect):
            assert controller.beginFileImport(QUrl.fromLocalFile(str(path)).toString())
            settle(controller)
        assert [Path(value) for value in calls] == [path]
        assert controller.transferMode == "import"
        assert {row["kind"] for row in controller.property("transferCatalog")["rows"]} == {"source"}
        assert target.manifest["documents"] == []
        wrong_id = next((row["id"] for row in original(donor.project_file)["rows"] if row["kind"] == "substrate"))
        assert not controller.previewTransfer([wrong_id], "project")
    finally:
        controller.cancelTransfer()
        service.close_resource_exchange()


@pytest.mark.parametrize("failure", [False, True])
def test_library_finishes_with_partial_or_failure_message(app, failure):
    class Service:
        def inspect_resources(self, source):
            if failure:
                raise ValueError("bad-source.csrc: aperture_kind")
            return {"catalog_id": "diagnostic", "source_type": "global", "source_path": "global",
                "categories": [], "rows": [], "warnings": ["bad-source.csrc: aperture_kind"]}
        def release_resource_catalog(self, identity):
            pass
        def close_resource_exchange(self):
            pass
    controller = ResourceLibraryBridge(service=Service())
    try:
        settle(controller)
        assert not controller.transferBusy
        assert bool(controller.libraryFailed) == failure
        assert "bad-source.csrc" in controller.property("libraryMessage")
        assert "aperture_kind" in controller.property("libraryMessage")
        assert "正在读取" not in controller.property("libraryMessage")
    finally:
        controller.dispose()
