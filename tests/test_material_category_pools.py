"""Independent material names and ordinary Air records in both categories."""
from copy import deepcopy

import pytest

from coat_sim.core.materials import (builtin_material_profiles, default_material_profile,
    material_categories, material_category, source_bindable, validate_material_profile)
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.project_material_library import ProjectMaterialLibrary
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore

pytestmark = pytest.mark.contract


def test_two_air_records_have_independent_identity_and_owned_optics():
    air = [row for row in builtin_material_profiles() if row["name"] == "Air"]
    assert {(row["id"], material_category(row)) for row in air} == {
        ("builtin_ideal_air", "film"), ("builtin_ideal_air_substrate", "substrate")}
    assert all(row["optical_only"] and not source_bindable(row) for row in air)
    assert air[0]["optical_constants"] == air[1]["optical_constants"]
    air[0]["optical_constants"][0]["n"][0] = 2
    assert air[1]["optical_constants"][0]["n"][0] == 1


@pytest.mark.parametrize("project", [False, True])
def test_same_name_is_allowed_across_categories_and_rejected_within_category(tmp_path, project):
    library = (ProjectMaterialLibrary(ProjectResourceService(ProjectStore.create(tmp_path / "project", "P")))
        if project else MaterialLibrary(directory=tmp_path / "materials"))
    film = default_material_profile("Shared local name", id="film_identity")
    film["category"] = "film"
    substrate = default_material_profile("Shared local name", id="substrate_identity")
    substrate["category"] = "substrate"
    library.write(film)
    library.write(substrate)
    duplicate = default_material_profile(" shared LOCAL name ", id="duplicate")
    duplicate["category"] = "film"
    with pytest.raises(ValueError, match="同一材料分类"):
        library.write(duplicate)
    assert library.read("film_identity")["record"]["category"] == "film"
    assert library.read("substrate_identity")["record"]["category"] == "substrate"


def test_historical_other_record_is_retained_without_gaining_source_role():
    profile = default_material_profile("Historical", id="historical")
    profile["category"] = "other"
    profile["roles"] = ["component"]
    original = deepcopy(profile)
    assert material_category(profile) is None
    assert material_categories(profile) == ()
    assert not source_bindable(profile)
    assert profile == original
    assert validate_material_profile(profile)["category"] == "other"
    profile["roles"] = ["medium"]
    assert material_category(profile) is None


def test_air_deletion_and_override_are_independent_normal_library_operations(tmp_path):
    library = MaterialLibrary(directory=tmp_path / "materials")
    film = library.read("builtin_ideal_air")["record"]
    substrate = library.read("builtin_ideal_air_substrate")["record"]
    substrate["notes"] = "Independent substrate edit"
    library.save_builtin_override(substrate)
    assert library.read("builtin_ideal_air")["record"] == film
    library.delete_builtin("builtin_ideal_air_substrate")
    assert library.read("builtin_ideal_air")["record"] == film
    assert "builtin_ideal_air_substrate" not in {row["record"]["id"] for row in library.catalog_records()}


@pytest.mark.parametrize("project", [False, True])
def test_import_names_use_the_same_category_fence_and_fail_atomically(tmp_path, project):
    library = (ProjectMaterialLibrary(ProjectResourceService(ProjectStore.create(tmp_path / "project", "P")))
        if project else MaterialLibrary(directory=tmp_path / "materials"))
    film = default_material_profile("Shared name", id="film_identity")
    film["category"] = "film"
    library.write(film)
    substrate = default_material_profile("Shared name", id="substrate_identity")
    substrate["category"] = "substrate"
    package = {"kind": "coat_sim.material_library", "records": [substrate]}
    library.apply_import(library.preview_import(package)["plan"], {})
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    valid = default_material_profile("New entry", id="new_entry")
    valid["category"] = "film"
    duplicate = default_material_profile(" shared NAME ", id="duplicate")
    duplicate["category"] = "film"
    package["records"] = [valid, duplicate]
    plan = library.preview_import(package)["plan"]
    with pytest.raises(ValueError, match="同一材料分类"):
        library.apply_import(plan, {})
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_restore_cannot_create_a_duplicate_name_or_remove_the_tombstone(tmp_path):
    library = MaterialLibrary(directory=tmp_path / "materials")
    library.delete_builtin("builtin_ideal_air")
    replacement = default_material_profile("Air", id="replacement")
    replacement["category"] = "film"
    library.write(replacement)
    with pytest.raises(ValueError, match="同一材料分类"):
        library.restore_builtin("builtin_ideal_air")
    assert "builtin_ideal_air" in {row["id"] for row in library.deleted_builtins()}
    assert library.read("replacement")["record"]["name"] == "Air"


def test_catalog_category_is_separate_from_medium_picker_usage(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    service = ProjectResourceService(store)
    library = ProjectMaterialLibrary(service)
    medium = default_material_profile("Historical medium", id="medium_identity")
    medium["roles"] = ["medium"]
    library.write(medium)
    catalog = service.inspect_resources("project")
    row = next(row for row in catalog["rows"] if row["kind"] == "material_profile")
    assert row["material_category"] is None
    assert row["material_categories"] == []
    assert row["material_uses"] == ["medium"]
