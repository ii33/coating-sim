"""New project draft Air defaults and medium picker identity regressions."""
from copy import deepcopy
import os
from pathlib import Path

import pytest

from coat_sim.core.film_design_session import AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID
from coat_sim.core.materials import builtin_material_profiles
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_film_session import ProjectFilmEditSession
from coat_sim.core.project_store import ProjectStore
from tests.test_project_material_catalog import StaticCatalog

pytestmark = pytest.mark.contract
AIR = {"kind": "material", "material_id": AIR_MATERIAL_ID, "optical_set_id": AIR_OPTICAL_SET_ID}


def make_session(tmp_path, *, catalog=True):
    store = ProjectStore.create(tmp_path / "project", "Air regression")
    records = [row for row in builtin_material_profiles() if row["id"] == AIR_MATERIAL_ID]
    service = ProjectFilmService(store, global_material_library=StaticCatalog(records if catalog else []))
    return ProjectFilmEditSession(service), store


def test_new_project_air_is_bound_and_saved_with_owned_dataset(tmp_path):
    session, store = make_session(tmp_path)
    design = session.snapshot().design
    assert design["incident_medium"] == design["exit_medium"] == AIR
    assert design["substrate"]["material_id"] == ""
    assert not store.manifest["documents"]
    assert session.service.materials.evaluate(AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID, 550) == 1 + 0j
    assert session.save_current(), session.snapshot().error
    saved = next(row for row in store.manifest["documents"] if row["kind"] == "film")
    # Reopen with no global catalog: the saved project owns its exact Air closure.
    reopened = ProjectFilmEditSession(ProjectFilmService(ProjectStore.open(store.project_file), global_material_library=StaticCatalog([])))
    assert reopened.load_saved_design(saved["id"])
    assert reopened.snapshot().design["incident_medium"] == reopened.snapshot().design["exit_medium"] == AIR
    assert reopened.service.materials.evaluate(AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID, 550) == 1 + 0j
    assert reopened.request_new_design()
    assert reopened.snapshot().design["incident_medium"] == AIR


@pytest.mark.parametrize("medium", [
    {"kind": "constant", "n": 1.37},
    {"kind": "material", "material_id": "other-medium", "optical_set_id": "other-set"},
])
def test_existing_non_air_media_survive_reopen_and_sides_switch(tmp_path, medium):
    session, store = make_session(tmp_path)
    design = session.service.new_design()
    design["incident_medium"] = deepcopy(medium)
    design["exit_medium"] = deepcopy(medium)
    materials = None
    if medium["kind"] == "material":
        optical = deepcopy(next(row for row in builtin_material_profiles() if row["id"] == AIR_MATERIAL_ID)["optical_constants"][0])
        optical.update(id="other-set", n=[1.45, 1.45])
        materials = {"other-medium": {"id": "other-medium", "name": "Other medium", "optical_sets": [optical]}}
    saved = session.service.create_film(design, materials=materials)
    assert session.load_saved_design(saved.document_id)
    for sides in ("double", "single", "double"):
        assert session.set_substrate_value("sides", sides)
        assert session.snapshot().design["incident_medium"] == medium
        assert session.snapshot().design["exit_medium"] == medium
    assert session.save_current(), session.snapshot().error
    reopened = ProjectFilmEditSession(ProjectFilmService(ProjectStore.open(store.project_file), global_material_library=StaticCatalog([])))
    assert reopened.load_saved_design(saved.document_id)
    assert reopened.snapshot().design["incident_medium"] == reopened.snapshot().design["exit_medium"] == medium


def test_air_exit_survives_single_double_single_mode(tmp_path):
    session, _ = make_session(tmp_path)
    for sides in ("double", "single", "double", "single"):
        assert session.set_substrate_value("sides", sides)
        assert session.snapshot().design["incident_medium"] == session.snapshot().design["exit_medium"] == AIR
    assert session.save_current(), session.snapshot().error


def test_missing_catalog_is_not_fabricated_and_broken_reference_is_not_repaired(tmp_path):
    session, store = make_session(tmp_path, catalog=False)
    assert session.snapshot().design["incident_medium"] == {"kind": "constant", "n": 1.0}
    assert session.set_medium_value("incident_medium.material_id", "missing-medium")
    assert session.set_medium_value("incident_medium.optical_set_id", "missing-set")
    broken = deepcopy(session.snapshot().design["incident_medium"])
    assert not session.save_current()
    assert session.snapshot().error
    assert session.snapshot().design["incident_medium"] == broken
    assert not store.manifest["documents"]


@pytest.mark.qt
def test_native_project_medium_controls_match_model_and_missing_ids(tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent, QUrl
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.theme_service import ThemeService

    session, _ = make_session(tmp_path)
    controller = FilmDesignQuickController(tmp_path, service=session.service, edit_session=session)
    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine, "Theme")
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/FilmDesignQuick.qml")))
    root = component.createWithInitialProperties({"controller": controller})
    assert root is not None, [error.toString() for error in component.errors()]
    try:
        root.show()
        tabs = root.findChild(QQuickItem, "filmDesignPageTabs")
        assert tabs is not None
        tabs.setProperty("currentIndex", 2)
        QTest.qWait(150)
        # No managed Air dependency exists yet. A live catalog refresh must
        # retain the global/draft Air choice without editing the design.
        before_refresh = session.snapshot()
        session.refresh_project_materials()
        QTest.qWait(30)
        assert session.snapshot().design == before_refresh.design
        assert session.snapshot().dirty == before_refresh.dirty
        assert not session.service.project_store.manifest["documents"]
        for key in ("incident_medium", "exit_medium"):
            material = root.findChild(QQuickItem, "filmDesignMediumMaterial_" + key)
            dataset = root.findChild(QQuickItem, "filmDesignMediumDataset_" + key)
            assert material is not None and dataset is not None
            assert material.property("currentValue") == AIR_MATERIAL_ID
            assert dataset.property("currentValue") == AIR_OPTICAL_SET_ID
        evidence_root = os.environ.get("COAT_AIR_QA_OUTPUT")
        if evidence_root:
            assert root.grabWindow().save(str(Path(evidence_root) / "new-project-air-single.png"))
        assert controller.setSubstrateValue("sides", "double")
        QTest.qWait(30)
        if evidence_root:
            assert root.grabWindow().save(str(Path(evidence_root) / "new-project-air-double.png"))
        assert controller.setMediumValue("incident_medium", "optical_set_id", "missing-set")
        QTest.qWait(30)
        dataset = root.findChild(QQuickItem, "filmDesignMediumDataset_incident_medium")
        assert dataset.property("currentIndex") == -1
        assert controller.currentDesign()["incident_medium"]["optical_set_id"] == "missing-set"
        assert controller.setMediumValue("incident_medium", "material_id", "missing-medium")
        QTest.qWait(30)
        material = root.findChild(QQuickItem, "filmDesignMediumMaterial_incident_medium")
        assert material.property("currentIndex") == -1
        assert not session.save_current()
    finally:
        root.close()
        root.deleteLater()
        controller.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QCoreApplication.processEvents()
