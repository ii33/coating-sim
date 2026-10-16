"""Qt controller injection smoke; this does not start the project desktop host."""

from typing import Any

import pytest

from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore
from tests.test_project_film_service import bound_design, material


pytestmark = [pytest.mark.contract, pytest.mark.qt]


class MemorySettings:
    def __init__(self):
        self.values: dict[str, Any] = {}

    def value(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def setValue(self, key: str, value: Any) -> None:
        self.values[key] = value

    def sync(self) -> None:
        pass


def test_existing_quick_controller_accepts_project_service_and_saves_same_document(tmp_path):
    qt = pytest.importorskip("PySide6.QtCore")
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController

    first = ProjectFilmService(ProjectStore.create(tmp_path / "first", "First"))
    second = ProjectFilmService(ProjectStore.create(tmp_path / "second", "Second"))
    left = first.create_film(bound_design(), materials={"shared": material(n=2.0)})
    right = second.create_film(bound_design(), materials={"shared": material(n=3.0)})
    left_controller = FilmDesignQuickController(service=first, geometry_settings=MemorySettings())
    right_controller = FilmDesignQuickController(service=second, geometry_settings=MemorySettings())
    try:
        assert left_controller.initializationReady and right_controller.initializationReady
        left_controller.requestOpenFile(qt.QUrl.fromLocalFile(str(left.handle)))
        right_controller.requestOpenFile(qt.QUrl.fromLocalFile(str(right.handle)))
        assert left_controller.currentDesign()["id"] == left.design["id"]
        assert right_controller.currentDesign()["id"] == right.design["id"]
        assert left_controller.setDesignValue("name", "First project edit")
        assert left_controller.saveCurrent()
        assert first.load_document(left.document_id).design["name"] == "First project edit"
        assert second.load_document(right.document_id).design["name"] == right.design["name"]
        assert not left.handle.exists() and not right.handle.exists()
        assert not left_controller.saveFilePath(str(tmp_path / "outside.cfilm"), True)
        assert not (tmp_path / "outside.cfilm").exists()
    finally:
        for controller in (left_controller, right_controller):
            controller.markSourceClosed()
            controller.deleteLater()
        qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.Type.DeferredDelete)
