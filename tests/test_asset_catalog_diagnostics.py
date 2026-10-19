"""Partial asset reads preserve strict current schemas and original files."""
from copy import deepcopy
from typing import Any, cast
import hashlib

import pytest

from coat_sim.core.project_dependencies import json_bytes, normalize_document
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from tests.design_test_fixtures import _RECTANGULAR_SOURCE
from tests import test_project_global_resources as global_fixtures
from tests.test_project_global_resources import typed_graph, write, files

isolated_fixture = global_fixtures.isolated

pytestmark = pytest.mark.contract


def obsolete_source():
    source = deepcopy(_RECTANGULAR_SOURCE)
    emission = cast(dict[str, Any], source["emission_model"])
    emission.pop("apertures")
    emission.update(aperture_kind="disk", aperture_radius_mm=5,
        aperture_length_mm=10, aperture_width_mm=10, aperture_offset_x_mm=0,
        aperture_offset_y_mm=0)
    return source


def test_global_bad_source_and_dependents_are_diagnosed_without_conversion(isolated_fixture, tmp_path):
    typed_graph(isolated_fixture)
    bad = isolated_fixture / "data/sources/lamp.csrc"
    write(bad, obsolete_source())
    before = files(isolated_fixture)
    target = ProjectStore.create(tmp_path / "target", "Target")
    api = ProjectResourceService(target)
    catalog = api.inspect_resources("global")
    assert {row["kind"] for row in catalog["rows"]} == {"substrate", "mask"}
    assert any(str(bad) in error and "aperture_kind" in error for error in catalog["warnings"])
    assert any("rig.cmac" in error for error in catalog["warnings"])
    assert any("recipe.cproc" in error for error in catalog["warnings"])
    selected = next(row["id"] for row in catalog["rows"] if row["kind"] == "substrate")
    plan = api.preview_resource_transfer(catalog["catalog_id"], [selected])
    api.apply_resource_transfer(plan["plan_id"])
    assert len(target.manifest["documents"]) == 1
    assert files(isolated_fixture) == before
    with pytest.raises(ValueError, match="aperture_kind"):
        normalize_document("source", obsolete_source())


def test_project_bad_source_is_isolated_and_saved_bytes_are_untouched(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    bad_id = source.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    valid_id = source.create_document("source", {"schema_version": 2, "kind": "point", "source_exponent": 1})
    manifest = source.manifest
    row = next(row for row in manifest["documents"] if row["id"] == bad_id)
    bad = source.root / row["path"]
    bad.write_bytes(json_bytes(obsolete_source()))
    row["sha256"] = hashlib.sha256(bad.read_bytes()).hexdigest()
    source.project_file.write_bytes(json_bytes(manifest))
    before = files(source.root)
    target = ProjectStore.create(tmp_path / "target", "Target")
    api = ProjectResourceService(target)
    catalog = api.inspect_resources(source.project_file)
    assert [row["id"] for row in catalog["rows"]] == [valid_id]
    assert any(str(bad) in error and "aperture_kind" in error for error in catalog["warnings"])
    plan = api.preview_resource_transfer(catalog["catalog_id"], [valid_id])
    api.apply_resource_transfer(plan["plan_id"])
    assert files(source.root) == before


def test_all_invalid_project_rows_finish_as_empty_diagnostic_catalog(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    source.create_document("source", {"schema_version": 2, "kind": "point"})
    row = source.manifest["documents"][0]
    bad = source.root / row["path"]
    bad.write_bytes(b"{invalid")
    target = ProjectStore.create(tmp_path / "target", "Target")
    catalog = ProjectResourceService(target).inspect_resources(source.project_file)
    assert catalog["rows"] == [] and len(catalog["warnings"]) == 1
    assert str(bad) in catalog["warnings"][0]


def test_bad_source_inside_saved_default_does_not_hide_independent_project_assets(tmp_path):
    from tests.test_project_core import setup_project
    (tmp_path / "source").mkdir()
    source, _external, _process_id, default_id = setup_project(tmp_path / "source")
    manifest = source.manifest
    row = next(row for row in manifest["documents"] if row["kind"] == "source")
    bad = source.root / row["path"]
    bad.write_bytes(json_bytes(obsolete_source()))
    row["sha256"] = hashlib.sha256(bad.read_bytes()).hexdigest()
    source.project_file.write_bytes(json_bytes(manifest))
    before = files(source.root)
    target = ProjectStore.create(tmp_path / "target", "Target")
    catalog = ProjectResourceService(target).inspect_resources(source.project_file)
    assert {row["kind"] for row in catalog["rows"]} == {"substrate", "mask"}
    assert any("aperture_kind" in warning and str(bad) in warning for warning in catalog["warnings"])
    assert default_id not in {row["id"] for row in catalog["rows"]}
    assert files(source.root) == before


def test_explicit_bad_file_reports_its_path_without_accepting_legacy_fields(tmp_path):
    bad = tmp_path / "bad.csrc"
    write(bad, obsolete_source())
    before = bad.read_bytes()
    target = ProjectStore.create(tmp_path / "target", "Target")
    with pytest.raises(ValueError) as error:
        ProjectResourceService(target).inspect_resources(bad)
    assert str(bad) in str(error.value) and "aperture_kind" in str(error.value)
    assert bad.read_bytes() == before and target.manifest["documents"] == []
