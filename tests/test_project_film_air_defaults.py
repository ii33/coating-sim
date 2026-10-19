"""New project draft Air defaults and medium picker identity regressions."""
from copy import deepcopy
import os
from pathlib import Path
from typing import Any, cast

import pytest

from coat_sim.core.film_design_session import AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import builtin_material_profiles
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_film_session import ProjectFilmEditSession
from coat_sim.core.project_store import ProjectStore
from tests.test_project_material_catalog import StaticCatalog

pytestmark = pytest.mark.contract
AIR = {"kind": "material", "material_id": AIR_MATERIAL_ID, "optical_set_id": AIR_OPTICAL_SET_ID}


def air_seed():
    return deepcopy(next(row for row in builtin_material_profiles()
                         if row["id"] == AIR_MATERIAL_ID))


def make_session(tmp_path, *, catalog=True):
    store = ProjectStore.create(tmp_path / "project", "Air regression")
    records = [air_seed()] if catalog else []
    service = ProjectFilmService(store, global_material_library=StaticCatalog(records))
    return ProjectFilmEditSession(service), store


def reopen(store):
    # Reopen with no global catalog: the saved project owns its own closure.
    return ProjectFilmEditSession(
        ProjectFilmService(ProjectStore.open(store.project_file),
                           global_material_library=StaticCatalog([])))


def test_new_project_air_is_bound_and_saved_with_owned_dataset(tmp_path):
    session, store = make_session(tmp_path)
    design = session.snapshot().design
    assert design["incident_medium"] == design["exit_medium"] == AIR
    assert design["substrate"]["material_id"] == ""
    assert not store.manifest["documents"]
    assert session.service.materials.evaluate(AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID, 550) == 1 + 0j
    assert session.save_current(), session.snapshot().error
    saved = next(row for row in store.manifest["documents"] if row["kind"] == "film")

    # The save persisted a project-owned optical closure for ideal Air, not just
    # a service overlay: the record is a real project optical_material document.
    material_rows = [row for row in store.manifest["documents"]
                     if row["kind"] == "optical_material"]
    assert len(material_rows) == 1
    graph = store.resolve_document(material_rows[0]["id"])
    persisted = next(row for row in graph["documents"] if row["id"] == material_rows[0]["id"])
    material = persisted["data"]["material"]
    assert material["id"] == AIR_MATERIAL_ID
    air_set = next(row for row in material["optical_sets"] if row["id"] == AIR_OPTICAL_SET_ID)
    assert air_set["n"] == [1.0, 1.0]
    assert air_set["k"] == [0.0, 0.0]

    # Reopening the saved film with no global catalog reads that project-owned
    # closure from disk (the service starter alone cannot satisfy a saved film's
    # own captured data reference).
    reopened_store = ProjectStore.open(store.project_file)
    reopened = reopen(store)
    assert reopened.load_saved_design(saved["id"])
    assert reopened.snapshot().design["incident_medium"] == reopened.snapshot().design["exit_medium"] == AIR
    assert reopened.service.materials.evaluate(AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID, 550) == 1 + 0j
    reopened_graph = reopened_store.resolve_document(saved["id"])
    reopened_material = next(row for row in reopened_graph["materials"]
                             if row["id"] == AIR_MATERIAL_ID)
    reopened_set = next(row for row in reopened_material["optical_sets"]
                        if row["id"] == AIR_OPTICAL_SET_ID)
    assert reopened_set["n"] == [1.0, 1.0]
    assert reopened_set["k"] == [0.0, 0.0]
    assert reopened.request_new_design()
    assert reopened.snapshot().design["incident_medium"] == AIR


def test_empty_global_catalog_still_binds_packaged_air(tmp_path):
    """A new draft uses the project starter, not a fabricated global record."""
    session, store = make_session(tmp_path, catalog=False)
    assert session.snapshot().design["incident_medium"] == AIR
    assert session.snapshot().design["exit_medium"] == AIR
    assert session.service.materials.evaluate(AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID, 550) == 1 + 0j
    # A genuinely unknown reference is still neither fabricated nor repaired.
    assert session.set_medium_value("incident_medium.material_id", "missing-medium")
    assert session.set_medium_value("incident_medium.optical_set_id", "missing-set")
    broken = deepcopy(session.snapshot().design["incident_medium"])
    assert not session.save_current()
    assert session.snapshot().error
    assert session.snapshot().design["incident_medium"] == broken
    assert not store.manifest["documents"]


def test_edited_global_air_does_not_change_project_default(tmp_path):
    """A user override of global Air cannot downgrade the project default."""
    library = MaterialLibrary(directory=tmp_path / "materials")
    edited = air_seed()
    edited["optical_constants"] = [
        dict(edited["optical_constants"][0], n=[2.0, 2.0])]
    library.save_builtin_override(edited)
    global_air = next(row["record"] for row in library.catalog_records()
                      if row["record"]["id"] == AIR_MATERIAL_ID)
    assert global_air["optical_constants"][0]["n"] == [2.0, 2.0]

    store = ProjectStore.create(tmp_path / "project", "Edited global Air")
    service = ProjectFilmService(store, global_material_library=library)
    session = ProjectFilmEditSession(service)
    assert session.snapshot().design["incident_medium"] == AIR
    assert service.materials.evaluate(AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID, 550) == 1 + 0j
    assert session.save_current(), session.snapshot().error
    # The user's global edit is preserved, not overwritten by the project.
    global_air = next(row["record"] for row in library.catalog_records()
                      if row["record"]["id"] == AIR_MATERIAL_ID)
    assert global_air["optical_constants"][0]["n"] == [2.0, 2.0]
    reopened = ProjectFilmEditSession(ProjectFilmService(
        ProjectStore.open(store.project_file), global_material_library=library))
    assert reopened.snapshot().design["incident_medium"] == AIR


def test_deleted_global_air_is_not_recreated_by_projects(tmp_path):
    """Deleting global Air is durable; a new project uses its own starter."""
    library = MaterialLibrary(directory=tmp_path / "materials")
    library.delete_builtin(AIR_MATERIAL_ID)
    assert AIR_MATERIAL_ID in {row["id"] for row in library.deleted_builtins()}
    assert all(row["record"]["id"] != AIR_MATERIAL_ID for row in library.catalog_records())

    store = ProjectStore.create(tmp_path / "project", "Deleted global Air")
    service = ProjectFilmService(store, global_material_library=library)
    session = ProjectFilmEditSession(service)
    assert session.snapshot().design["incident_medium"] == AIR
    assert session.save_current(), session.snapshot().error
    # Saving the project must not resurrect the tombstoned global builtin.
    assert AIR_MATERIAL_ID in {row["id"] for row in library.deleted_builtins()}
    assert all(row["record"]["id"] != AIR_MATERIAL_ID for row in library.catalog_records())


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
        optical = deepcopy(air_seed()["optical_constants"][0])
        optical.update(id="other-set", n=[1.45, 1.45])
        materials = {"other-medium": {"id": "other-medium", "name": "Other medium", "optical_sets": [optical]}}
    saved = session.service.create_film(design, materials=materials)
    assert session.load_saved_design(saved.document_id)
    for sides in ("double", "single", "double"):
        assert session.set_substrate_value("sides", sides)
        assert session.snapshot().design["incident_medium"] == medium
        assert session.snapshot().design["exit_medium"] == medium
    assert session.save_current(), session.snapshot().error
    reopened = reopen(store)
    assert reopened.load_saved_design(saved.document_id)
    assert reopened.snapshot().design["incident_medium"] == reopened.snapshot().design["exit_medium"] == medium


def test_air_exit_survives_single_double_single_mode(tmp_path):
    session, _ = make_session(tmp_path)
    for sides in ("double", "single", "double", "single"):
        assert session.set_substrate_value("sides", sides)
        assert session.snapshot().design["incident_medium"] == session.snapshot().design["exit_medium"] == AIR
    assert session.save_current(), session.snapshot().error


@pytest.mark.qt
def test_native_project_medium_controls_match_model_and_missing_ids(tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent, QUrl
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression
    from PySide6.QtQuick import QQuickItem, QQuickWindow
    from PySide6.QtTest import QTest
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.theme_service import ThemeService

    session, _ = make_session(tmp_path)
    controller = FilmDesignQuickController(tmp_path, service=session.service, edit_session=session)
    # The fresh project has separate category pools and a single default Air
    # candidate in medium; same display names never combine their identities.
    film_air_ids = [row["id"] for row in cast(list[dict[str, Any]], controller.filmOptions)
                    if row["name"] == "Air"]
    substrate_air_ids = [row["id"] for row in cast(list[dict[str, Any]], controller.substrateOptions)
                         if row["name"] == "Air"]
    medium_air_ids = [row["id"] for row in cast(list[dict[str, Any]], controller.mediumOptions)
                      if row["name"] == "Air"]
    assert film_air_ids == [AIR_MATERIAL_ID]
    assert substrate_air_ids == ["builtin_ideal_air_substrate"]
    assert medium_air_ids == [AIR_MATERIAL_ID]
    assert len([row for row in cast(list[dict[str, Any]], controller.mediumOptions) if row["name"] == "Air"]) == 1
    assert AIR_OPTICAL_SET_ID in {row["id"]
                                  for row in controller.opticalSetsFor(AIR_MATERIAL_ID)}
    assert all(row["id"] != AIR_MATERIAL_ID
               for row in cast(list[dict[str, Any]], controller.substrateOptions))
    assert any(row["id"] == AIR_MATERIAL_ID
               for row in cast(list[dict[str, Any]], controller.filmOptions))
    assert any(row["id"] == "builtin_ideal_air_substrate"
               for row in cast(list[dict[str, Any]], controller.substrateOptions))

    engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine, "Theme")
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/FilmDesignQuick.qml")))
    root = component.createWithInitialProperties({"controller": controller})
    assert isinstance(root, QQuickWindow), [error.toString() for error in component.errors()]
    try:
        root.show()
        tabs = root.findChild(QQuickItem, "filmDesignPageTabs")
        assert tabs is not None
        tabs.setProperty("currentIndex", 2)
        QTest.qWait(150)
        # No managed Air dependency exists yet. A live catalog refresh must
        # retain the packaged Air choice without editing the design.
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
            context = QQmlEngine.contextForObject(material) or engine.rootContext()
            label_expression = QQmlExpression(context, material, "candidateLabel(0)")
            label, undefined = label_expression.evaluate()
            assert not label_expression.hasError(), label_expression.error().toString()
            assert not undefined and label == "Air"
        exit_editor = root.findChild(QQuickItem, "filmDesignMediumEditor_exit_medium")
        incident_editor = root.findChild(QQuickItem, "filmDesignMediumEditor_incident_medium")
        assert exit_editor is not None and incident_editor is not None
        evidence_root = os.environ.get("COAT_AIR_QA_OUTPUT")
        if evidence_root:
            assert root.grabWindow().save(str(Path(evidence_root) / "new-project-air-single.png"))
        # Single side hides exit from computation but keeps its bound Air value.
        assert controller.setSubstrateValue("sides", "double")
        QTest.qWait(30)
        assert exit_editor.property("enabled") is True
        assert controller.setSubstrateValue("sides", "single")
        QTest.qWait(30)
        assert exit_editor.property("enabled") is False
        assert controller.currentDesign()["exit_medium"] == AIR
        assert controller.currentDesign()["incident_medium"] == AIR
        if evidence_root:
            assert root.grabWindow().save(str(Path(evidence_root) / "new-project-air-double.png"))
        assert controller.setMediumValue("incident_medium", "optical_set_id", "missing-set")
        QTest.qWait(30)
        dataset = root.findChild(QQuickItem, "filmDesignMediumDataset_incident_medium")
        assert dataset is not None
        assert dataset.property("currentIndex") == -1
        assert controller.currentDesign()["incident_medium"]["optical_set_id"] == "missing-set"
        assert controller.setMediumValue("incident_medium", "material_id", "missing-medium")
        QTest.qWait(30)
        material = root.findChild(QQuickItem, "filmDesignMediumMaterial_incident_medium")
        assert material is not None
        assert material.property("currentIndex") == -1
        assert not session.save_current()
    finally:
        root.close()
        root.deleteLater()
        controller.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
