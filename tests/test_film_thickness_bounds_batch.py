"""Batch edits validate all final bounds and commit once without selecting layers."""
from copy import deepcopy

import pytest

from coat_sim.ui.film_thickness_bounds_batch import plan_thickness_bounds
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import app as app, optimizer

pytestmark = pytest.mark.qt


@pytest.fixture
def case(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    original = design["front_layers"][0]
    design["front_layers"] = [dict(deepcopy(original), id=f"batch-layer-{i}") for i in range(3)]
    with optimizer(service, design) as values:
        controller, _source, session = values
        for i, row in enumerate(controller.variables):
            assert controller.setVariableBound("front", row["layer_id"], "lower_nm", i + 1)
            assert controller.setVariableBound("front", row["layer_id"], "upper_nm", (i + 1) * 10)
        controller.setVariableEnabled("front", "batch-layer-1", False)
        session.configuration_writes.clear()
        yield values


def test_all_layers_commit_once_keep_unrelated_configuration_and_selection(case):
    c, source, session = case
    before = deepcopy(c.exportOptimizationConfiguration())
    revision = session.snapshot().design_revision
    assert c.applyVariableBoundsBatch("all", "0", "40"), c.error
    assert len(session.configuration_writes) == 1
    after = deepcopy(c.exportOptimizationConfiguration())
    assert [r["enabled"] for r in after["variables"]] == [True, False, True]
    assert all((r["lower_nm"], r["upper_nm"]) == (0., 40.) for r in after["variables"])
    after.pop("variables")
    before.pop("variables")
    assert after == before
    assert session.snapshot().design_revision == revision and not source.applied


def test_selected_scope_and_independent_fields_then_single_layer_edit(case):
    c, _source, session = case
    before = deepcopy(c.variables)
    assert c.applyVariableBoundsBatch("selected", "", "50"), c.error
    assert c.variables[1] == before[1]
    assert [r["lower_nm"] for r in c.variables] == [1., 2., 3.]
    assert [r["upper_nm"] for r in c.variables] == [50., 20., 50.]
    assert c.applyVariableBoundsBatch("selected", "0", ""), c.error
    assert [(r["lower_nm"], r["upper_nm"]) for r in c.variables] == [(0., 50.), (2., 20.), (0., 50.)]
    assert c.setVariableBound("front", "batch-layer-0", "upper_nm", "55.1234")
    assert c.variables[0]["upper_nm"] == 55.123
    assert len(session.configuration_writes) == 3


@pytest.mark.parametrize("lower,upper", [("", ""), ("  ", "\t"), ("", "10")])
def test_empty_or_unchanged_edit_does_not_invalidate_or_write(case, lower, upper):
    c, _source, session = case
    # The last case targets only the first layer, whose upper already equals 10.
    if upper:
        c.setVariableEnabled("front", "batch-layer-2", False)
        session.configuration_writes.clear()
    before = deepcopy(c.variables)
    marker = object()
    c._result = marker
    assert c.applyVariableBoundsBatch("selected", lower, upper)
    assert c.variables == before and c._result is marker
    assert not session.configuration_writes
    c._result = None


@pytest.mark.parametrize("scope,lower,upper", [
    ("all", "-1", ""), ("all", "nan", ""), ("all", "", "inf"),
    ("all", "-0.0001", ""), ("all", "", "-0.0001"),
    ("all", "abc", ""), ("all", "20", "10"), ("all", "0", "0"),
    ("all", "", "2"), ("all", "11", ""), ("unknown", "0", "50"),
])
def test_invalid_or_any_final_conflict_is_atomic(case, scope, lower, upper):
    c, _source, session = case
    before = deepcopy(c.variables)
    marker = object()
    c._result = marker
    assert not c.applyVariableBoundsBatch(scope, lower, upper)
    assert c.error and c.variables == before and c._result is marker
    assert not session.configuration_writes
    c._result = None
    assert c.applyVariableBoundsBatch("all", "0", "40"), c.error
    assert not c.error and len(session.configuration_writes) == 1


def test_no_selection_and_running_or_pending_editor_do_not_commit(case):
    c, _source, session = case
    for row in c.variables:
        c.setVariableEnabled(row["side"], row["layer_id"], False)
    session.configuration_writes.clear()
    assert not c.applyVariableBoundsBatch("selected", "0", "50")
    assert not session.configuration_writes
    assert c.applyVariableBoundsBatch("all", "0", "50")
    before = deepcopy(c.variables)
    count = len(session.configuration_writes)
    c._busy = True
    assert not c.applyVariableBoundsBatch("all", "1", "60")
    c._busy = False
    c._record_pending_edit_error("variable:front:batch-layer-0:lower_nm", "unfinished single layer input")
    assert not c.applyVariableBoundsBatch("all", "1", "60")
    assert c.variables == before and len(session.configuration_writes) == count
    c._clear_pending_edit_error("variable:front:batch-layer-0:lower_nm")


def test_configuration_save_reopen_and_new_topology_use_current_ids(case):
    c, _source, session = case
    assert c.applyVariableBoundsBatch("all", "0", "60")
    saved = deepcopy(c.exportOptimizationConfiguration())
    with optimizer(c.service, session.design, saved) as (reopened, _source2, _session2):
        assert reopened.variables == c.variables
    session.design["front_layers"] = [dict(deepcopy(session.design["front_layers"][0]), id="synthesized-current-layer")]
    session.design_revision += 1
    c.refreshDesign()
    assert [r["layer_id"] for r in c.variables] == ["synthesized-current-layer"]
    assert all(r["enabled"] for r in c.variables)
    assert c.applyVariableBoundsBatch("all", "0", "75"), c.error
    assert [(r["layer_id"], r["lower_nm"], r["upper_nm"]) for r in c.variables] == [("synthesized-current-layer", 0., 75.)]


def test_planner_includes_both_sides_without_mutating_rows():
    rows = [dict(side="front", layer_id="same", lower_nm=1., upper_nm=10., enabled=True),
            dict(side="back", layer_id="same", lower_nm=2., upper_nm=None, enabled=False)]
    before = deepcopy(rows)
    planned = plan_thickness_bounds(rows, "all", 0., None)
    assert [(r.side, r.layer_id, r.lower_nm, r.upper_nm) for r in planned] == [("front", "same", 0., 10.), ("back", "same", 0., None)]
    assert rows == before
