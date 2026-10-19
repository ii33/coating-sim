"""Source-based substrate formulas, boundaries, visibility and owned closure."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from coat_sim.core.materials import builtin_material_profiles, material_categories
from coat_sim.core.optical_constants import evaluate_optical_set
from coat_sim.core.optical_substrate_presets import builtin_optical_substrates
from coat_sim.core.material_business import ApplicationMaterialCatalog
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_film_session import ProjectFilmEditSession
from coat_sim.core.project_store import ProjectStore
from tests.test_project_material_catalog import StaticCatalog

pytestmark = pytest.mark.contract
K9 = "film_substrate_cdgm_hk9l"
SILICA = "film_substrate_malitson_silica"


def presets():
    return {row["id"]: row for row in builtin_optical_substrates()}


def test_hk9l_formula_matches_every_in_domain_official_reference_and_unknown_k():
    source = Path(__file__).resolve().parents[1]/"coat_sim/core/resources/optical_substrate_supplement.json"
    supplement = json.loads(source.read_text(encoding="utf-8"))
    group = presets()[K9]["optical_sets"][0]
    checked = 0
    assert group["wavelength_unit"] == "nm"
    assert (group["wavelength_min"], group["wavelength_max"]) == (302, 2325)
    for wavelength, expected in supplement["reference_tables"]["cdgm_hk9l_official_n"]["rows"]:
        if group["wavelength_min"] <= wavelength <= group["wavelength_max"]:
            n, k = evaluate_optical_set(group, wavelength, "nm")
            assert abs(n - expected) < 1e-6, wavelength
            assert k is None
            checked += 1
    assert checked == 19
    assert supplement["sources"]["cdgm_hk9l"]["temperature_C"] == 20
    assert supplement["sources"]["cdgm_hk9l"]["index_reference"] == "source_reported"
    assert "k_data" not in group
    assert "H-K9L" in presets()[K9]["name"]
    assert "517642" in presets()[K9]["notes"]


@pytest.mark.parametrize("identity,group_id,low,high", [
    (K9, "cdgm_hk9l_sellmeier_nm", 302, 2325),
    (SILICA, "malitson_silica_sellmeier_nm", 210, 3710),
])
def test_source_domains_are_closed_and_not_extended(identity, group_id, low, high):
    group = next(row for row in presets()[identity]["optical_sets"] if row["id"] == group_id)
    for wavelength in (low, high):
        assert evaluate_optical_set(group, wavelength, "nm")[1] is None
    for wavelength in (low-0.01, high+0.01):
        with pytest.raises(ValueError):
            evaluate_optical_set(group, wavelength, "nm")
    if identity == K9:
        with pytest.raises(ValueError):
            evaluate_optical_set(group, 2325.42, "nm")


def test_silica_nm_formula_preserves_original_um_identity_values_and_domain():
    groups = {row["id"]: row for row in presets()[SILICA]["optical_sets"]}
    old, new = groups["malitson_silica_sellmeier"], groups["malitson_silica_sellmeier_nm"]
    assert old["wavelength_unit"] == "um"
    assert (old["wavelength_min"], old["wavelength_max"]) == (.21, 3.71)
    for wavelength in (210, 355, 550, 1064, 3710):
        n_old, k_old = evaluate_optical_set(old, wavelength, "nm")
        n_new, k_new = evaluate_optical_set(new, wavelength, "nm")
        assert n_new == pytest.approx(n_old, abs=1e-14)
        assert k_new is k_old is None
    assert "10.1364/JOSA.55.001205" in new["source"]
    assert "各向同性熔融石英" in new["notes"]
    # Existing N-BK7 identity is separate, never an alias of K9.
    nbk7 = presets()["film_substrate_schott_nbk7"]
    assert nbk7["optical_sets"][0]["id"] == "schott_nbk7_sellmeier"
    assert nbk7["optical_sets"][0]["parameters"]["B1"] == 1.039612120


@pytest.mark.parametrize("identity,set_id", [
    (K9, "cdgm_hk9l_sellmeier_nm"),
    (SILICA, "malitson_silica_sellmeier_nm"),
    ("film_substrate_schott_nbk7", "schott_nbk7_sellmeier"),
])
def test_new_project_sees_substrates_and_save_reopen_owns_exact_binding(tmp_path, identity, set_id):
    catalog = ApplicationMaterialCatalog(tmp_path/"nonexistent-global-materials")
    store = ProjectStore.create(tmp_path/"project", "Sourced substrate")
    session = ProjectFilmEditSession(ProjectFilmService(store, global_material_library=catalog))
    assert session.snapshot().design["substrate"]["material_id"] == ""
    options = session.snapshot().material_options["substrate"]
    material = next(row for row in options if row["id"] == identity)
    assert material["name"]
    assert session.set_substrate_value("material_id", identity)
    assert session.set_substrate_value("optical_set_id", set_id)
    if identity != "film_substrate_schott_nbk7":
        assert material["optical_sets"][0]["wavelength_unit"] == "nm"
    before = deepcopy(session.snapshot().design)
    with pytest.raises(ValueError, match="Unknown k"):
        session.service.materials.evaluate(identity, set_id, 587.56)
    numerical_before = session.service.materials.evaluate(identity, set_id, 587.56, assume_lossless=True)
    full_material = session.service.materials.get(identity)
    assert session.save_current(), session.snapshot().error
    film = next(row for row in store.manifest["documents"] if row["kind"] == "film")
    graph = store.resolve_document(film["id"])
    owned = next(row for row in graph["materials"] if row["id"] == identity)
    saved_group = next(row for row in owned["optical_sets"] if row["id"] == set_id)
    assert saved_group == next(row for row in full_material["optical_sets"] if row["id"] == set_id)
    assert {row["id"] for row in graph["materials"]} == {identity, "builtin_ideal_air"}
    reopened_store = ProjectStore.open(store.project_file)
    reopened = ProjectFilmEditSession(ProjectFilmService(reopened_store, global_material_library=StaticCatalog([])))
    assert reopened.load_saved_design(film["id"])
    assert reopened.snapshot().design == before
    assert reopened.service.materials.evaluate(identity, set_id, 587.56, assume_lossless=True) == numerical_before
    row = next(row for row in reopened.snapshot().material_options["substrate"] if row["id"] == identity)
    assert row["name"] == material["name"]
    assert any(group["id"] == set_id for group in row["optical_sets"])
    assert not (tmp_path/"nonexistent-global-materials").exists()


def test_confirmed_container_seeds_are_deleted_without_losing_films_or_substrates():
    from coat_sim.core.material_research import research_material_records
    from coat_sim.core.material_business import ApplicationMaterialCatalog
    removed = {"W", "Mo", "Ta", "Pt", "graphite", "glassy_carbon", "FABMATE", "Cu", "Al2O3", "TiB2_BN"}
    removed = {identity + "_source_container" for identity in removed}
    research = research_material_records()
    assert len(research) == 18
    assert not removed.intersection(row["id"] for row in research)
    profiles = builtin_material_profiles()
    assert len(profiles) == 23
    assert not {"builtin_research_" + identity for identity in removed}.intersection(row["id"] for row in profiles)
    assert not any(row["id"].endswith("_source_container") for row in profiles)
    assert {row["record"]["id"] for row in ApplicationMaterialCatalog(Path(__file__).parent/"absent-fixture-materials").catalog_records()} == {row["id"] for row in profiles}
    assert all(material_categories(row) == ("film",) for row in profiles if row["id"] in
               {"builtin_research_Cu_film", "builtin_research_Al2O3_film"})
    assert material_categories(next(row for row in profiles if row["id"] == "builtin_research_K9_substrate")) == ("substrate",)
    assert material_categories({"name": "BK7", "roles": ["medium"]}) == ()
    assert material_categories({"name": "Air", "roles": ["film", "substrate", "medium"]}) == ("film", "substrate")
