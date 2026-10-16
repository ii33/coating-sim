"""Older numerical snapshots give actionable, nonmutating editor failures."""

from copy import deepcopy
from typing import cast

import pytest

from coat_sim.core.film_design import validate_design
from coat_sim.core.film_process_import import _sha256, validate_frozen_design_snapshot
from coat_sim.core.project_film_service import ProjectFilmService, ProjectFilmStore
from tests.test_project_film_default_draft import frozen_film_project, workspace_for


pytestmark = pytest.mark.contract


@pytest.mark.parametrize("missing_role", ["symbol", "candidate"])
def test_numerically_valid_snapshot_missing_editable_material_gives_recovery_guidance(
        tmp_path, missing_role):
    store, frozen = frozen_film_project(tmp_path / "project")
    incomplete = deepcopy(frozen)
    reference = {"material_id": missing_role, "optical_set_id": "visible"}
    if missing_role == "symbol":
        incomplete["design"]["expression"] = {
            "text": "S", "in_sync": False,
            "symbols": {"S": {**reference, "thickness": {"value": 1.0, "unit": "qw"},
                              "note": "Older snapshot's editing-only binding"}},
        }
    else:
        incomplete["design"]["workflow_settings"]["optimization"].update(
            version=1, structure_materials=[reference])
    incomplete["design"] = validate_design(incomplete["design"])
    incomplete["sha256"] = _sha256(incomplete)
    assert validate_frozen_design_snapshot(incomplete) == incomplete

    # The old numerical/process contract still accepts this snapshot. Its
    # absent symbol/candidate is not an active optical calculation reference.
    numerical = ProjectFilmService(cast(ProjectFilmStore, store))
    numerical.activate_frozen_draft_materials(incomplete["materials"])
    result = numerical.simulate(incomplete["design"], incomplete["settings"])
    assert result["R"] == pytest.approx([1.0 / 9.0] * 5)

    workspace = workspace_for([store], store)
    editor = workspace.editor
    before = editor.snapshot()
    manifest_before = store.manifest
    assert not editor.initialize_frozen_draft(incomplete)
    after = editor.snapshot()
    assert f"{missing_role}/visible" in after.error
    assert "重新导出" in after.error
    assert "表达式符号" in after.error
    assert "优化候选材料" in after.error
    assert "仍可用于" in after.error
    assert after.design == before.design
    assert after.design_revision == before.design_revision
    assert after.path == before.path
    assert after.dirty == before.dirty
    assert editor._frozen_draft_materials is None
    assert workspace.service._draft_materials is None
    assert store.manifest == manifest_before
