"""Media eligibility follows roles/current bindings without weakening closures."""
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coat_sim.core.materials import default_material_profile
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_film_session import ProjectFilmEditSession
from coat_sim.core.project_store import ProjectStore
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from tests.test_project_material_catalog import StaticCatalog

pytestmark = pytest.mark.contract


def optical_profile(identity, roles, name=None):
    row = default_material_profile(name or identity, id=identity, optical_only=True)
    row["roles"] = roles
    row["optical_constants"] = [{"id": "visible", "name": "Synthetic visible", "kind": "tabulated",
        "wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 700,
        "wavelengths": [400, 700], "n": [1.4, 1.4], "k": [0, 0], "source": "synthetic"}]
    return row


def test_new_project_medium_options_have_one_air_and_explicit_media_only(tmp_path):
    profiles = [optical_profile("layer", ["film"]), optical_profile("glass", ["substrate"]),
                optical_profile("water", ["medium"])]
    session = ProjectFilmEditSession(ProjectFilmService(ProjectStore.create(tmp_path/"project", "P"),
        global_material_library=StaticCatalog(profiles)))
    options = session.snapshot().material_options
    assert {row["id"] for row in options["medium"]} == {"builtin_ideal_air", "water"}
    assert "builtin_ideal_air_substrate" in {row["id"] for row in options["substrate"]}
    state = FilmDesignQuickController._build_picker_state(options, session.snapshot().design)
    assert {row["id"] for row in state["medium_options"]} == {"builtin_ideal_air", "water"}


def test_frozen_layer_and_base_do_not_gain_medium_usage_and_bound_immersion_survives(tmp_path):
    profiles = [optical_profile("layer", ["film"]), optical_profile("glass", ["substrate"]),
                optical_profile("immersion", ["substrate"])]
    store = ProjectStore.create(tmp_path/"project", "Frozen role regression")
    service = ProjectFilmService(store, global_material_library=StaticCatalog(profiles))
    session = ProjectFilmEditSession(service)
    design = dict(session.snapshot().design)
    design["substrate"].update(material_id="glass", optical_set_id="visible", sides="double")
    design["front_layers"] = [{"id": "one", "material_id": "layer", "optical_set_id": "visible",
                               "thickness": {"value": 50, "unit": "nm"}, "note": ""}]
    design["incident_medium"] = {"kind": "material", "material_id": "immersion", "optical_set_id": "visible"}
    saved = service.create_film(design)
    assert session.load_saved_design(saved.document_id)
    state = session.snapshot()
    assert {row["id"] for row in state.material_options["medium"]} == {"builtin_ideal_air", "immersion"}
    assert {row["id"] for row in service.freeze_design(state.design)["materials"]} == {
        "layer", "glass", "immersion", "builtin_ideal_air"}
    assert session.save_current(), session.snapshot().error
    reopened = ProjectFilmEditSession(ProjectFilmService(ProjectStore.open(store.project_file),
        global_material_library=StaticCatalog([])))
    assert reopened.load_saved_design(saved.document_id)
    assert reopened.snapshot().design["incident_medium"] == design["incident_medium"]
    assert {row["id"] for row in reopened.snapshot().material_options["medium"]} == {
        "builtin_ideal_air", "immersion"}
    assert reopened.service.materials.evaluate("layer", "visible", 550) == 1.4+0j


def test_controller_medium_projection_keeps_ids_and_only_current_non_role_binding():
    def option(identity):
        return {"id": identity, "name": "Shared name", "optical_sets": [{"id": "visible"}]}
    pools = {"film": [option("film")], "substrate": [option("base"), option("immersion")],
             "medium": [option("air")]}
    design = {"incident_medium": {"kind": "material", "material_id": "immersion"},
              "exit_medium": {"kind": "constant", "n": 1}}
    state = FilmDesignQuickController._build_picker_state(pools, design)
    assert [row["id"] for row in state["medium_picker_options"]] == ["air", "immersion"]
    design["incident_medium"] = {"kind": "constant", "n": 1.2}
    state = FilmDesignQuickController._build_picker_state(pools, design)
    assert [row["id"] for row in state["medium_picker_options"]] == ["air"]


def test_existing_substrate_air_binding_keeps_both_identities_and_default_is_one_air(tmp_path):
    from coat_sim.core.material_business import ApplicationMaterialCatalog
    store = ProjectStore.create(tmp_path/"project", "Existing substrate Air")
    service = ProjectFilmService(store, global_material_library=ApplicationMaterialCatalog(tmp_path/"global"))
    session = ProjectFilmEditSession(service)
    initial = session.snapshot()
    assert [row["id"] for row in initial.material_options["medium"]] == ["builtin_ideal_air"]
    design = dict(initial.design)
    binding = {"kind": "material", "material_id": "builtin_ideal_air_substrate",
               "optical_set_id": "ideal_air_n1_k0"}
    design["incident_medium"] = deepcopy(binding)
    saved = service.create_film(design)
    assert session.load_saved_design(saved.document_id)
    state = session.snapshot()
    assert state.design["incident_medium"] == binding
    assert {row["id"] for row in state.material_options["medium"]} == {
        "builtin_ideal_air", "builtin_ideal_air_substrate"}
    assert session.save_current(), session.snapshot().error
    reopened = ProjectFilmEditSession(ProjectFilmService(ProjectStore.open(store.project_file),
        global_material_library=StaticCatalog([])))
    assert reopened.load_saved_design(saved.document_id)
    assert reopened.snapshot().design["incident_medium"] == binding
    assert reopened.service.materials.evaluate("builtin_ideal_air_substrate", "ideal_air_n1_k0", 550) == 1+0j


def test_picker_refresh_preserves_pending_purposes_across_inline_session_callbacks():
    expected = {purpose: [{"id": purpose, "name": purpose, "optical_sets": [{"id": "visible"}]}]
                for purpose in ("film", "substrate", "medium")}
    controller = SimpleNamespace(
        film_options=expected["film"], substrate_options=expected["substrate"],
        medium_options=expected["medium"], _snapshot=SimpleNamespace(design={}),
        _picker_option_sources={},
        _build_picker_state=FilmDesignQuickController._build_picker_state,
        _picker_material=lambda row: row,
    )
    received: dict[str, list[dict[str, Any]]] = {}

    def update(purpose, rows):
        received[purpose] = rows
        # A synchronous notification reinstalls projections before the next call.
        for role in expected:
            setattr(controller, f"{role}_options", [])

    controller.session = SimpleNamespace(update_material_options=update)
    FilmDesignQuickController._refresh_picker_options(cast(Any, controller))
    assert received == expected
