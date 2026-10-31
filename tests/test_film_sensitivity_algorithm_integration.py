"""The algorithm selector routes the common lifecycle to ideal sensitivity."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QObject, QThreadPool
from coat_sim.scheduler.jobs import JobSpec
from tests.test_film_sensitivity_workflows_gui import _bounded_variable, app as app, harness as harness
from tests.test_film_optimization_restore_layout import window_runtime

pytestmark = pytest.mark.qt


def _configure(h):
    _bounded_variable(h)
    assert h.owner.setSearchMode("ideal_sensitivity")
    assert h.panel.setIdealSensitivityMode("absolute")
    assert h.panel.setIdealSensitivityPolicy("strict_nominal")
    assert h.panel.setIdealOption("max_iterations", "2")
    assert h.panel.setIdealOption("max_evaluations", "12")


def test_selected_algorithm_routes_real_worker_and_keeps_other_modes(harness):
    h = harness
    before = {row["id"] for row in h.owner.searchModeOptions}
    assert {"fixed_topology", "structure", "synthesis", "reduction", "automatic_repair", "ideal_sensitivity"} <= before
    _configure(h)
    assert h.owner.startOptimization(), h.panel.idealError
    assert h.owner.busy
    assert not h.owner.canStart
    h.pool.run()
    assert not h.owner.busy
    assert JobSpec.parse_frozen(h.client.submissions[-1][0]).executor == "film_ideal_sensitivity"
    assert h.panel.idealHasResult
    assert h.owner.hasCandidate


def test_drafts_survive_algorithm_switch_and_window_reopen(harness, app):
    h = harness
    _configure(h)
    assert h.owner.setSearchMode("fixed_topology")
    assert h.owner.setSearchMode("ideal_sensitivity")
    assert h.panel.idealSensitivityMode == "absolute"
    assert h.panel.idealSensitivityPolicy == "strict_nominal"
    assert h.panel.idealOptions["max_evaluations"] == "12"
    for _ in range(2):
        with window_runtime(app, h.owner) as (window, warnings):
            assert window.findChild(QObject, "filmOptimizationSearchModeBox") is not None
            assert h.panel.idealOptions["max_evaluations"] == "12"
            assert not warnings, warnings


def test_common_cancel_reaches_sensitivity_worker(harness):
    h = harness
    _configure(h)
    assert h.owner.startOptimization(), h.panel.idealError
    cancel = h.panel._cancel
    assert cancel is not None and not cancel.is_set()
    h.owner.cancelOptimization()
    assert cancel.is_set()
    h.pool.run()
    assert not h.owner.busy


def test_common_apply_confirmation_and_real_source_undo(app, tmp_path):
    from typing import cast
    import shiboken6
    from coat_sim.core.film_design_session import FilmDesignEditSession
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController
    from coat_sim.ui.compute_target import ComputeTarget
    from tests.film_structure_fixtures import fixture
    from tests.test_film_sensitivity_quick_controller import _HeldPool
    from tests.test_film_sensitivity_workflows_gui import _WorkflowPhysicsClient

    service, _, design = fixture(tmp_path)
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    pool, client = _HeldPool(), _WorkflowPhysicsClient()
    try:
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        layer = design["front_layers"][0]["id"]
        assert owner.setVariableBound("front", layer, "lower_nm", 60.)
        assert owner.setVariableBound("front", layer, "upper_nm", 145.)
        assert owner.setSearchMode("ideal_sensitivity")
        panel = cast(FilmSensitivityQuickController, owner.sensitivityController)
        panel.pool = cast(QThreadPool, pool)
        panel.client_factory = lambda _root: client
        assert panel.setIdealSensitivityMode("absolute")
        for row in cast(list[dict], panel.idealDegradationObjectives):
            assert panel.setIdealDegradationBudget(row["objective_id"], "100")
        assert panel.setIdealOption("max_iterations", "2")
        assert panel.setIdealOption("max_evaluations", "12")
        before = deepcopy(session.snapshot().design)
        assert owner.startOptimization(), panel.idealError
        pool.run()
        assert owner.canApply, panel.idealError
        prompts: list[str] = []
        owner.applyConfirmationRequested.connect(prompts.append)
        assert not owner.applyCandidate()
        assert prompts and session.snapshot().design == before
        # A table dialog may have selected analysis; common apply must still
        # target the selected optimization algorithm, not that display tab.
        assert panel.selectWorkflow(0)
        assert owner.confirmApplyCandidate(), panel.idealError
        assert source.canUndoOptimizedDesign
        assert not owner.confirmApplyCandidate()
        assert source.undoOptimizedDesign()
        assert session.snapshot().design == before
    finally:
        owner.shutdown(1000)
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(owner)
        shiboken6.delete(source)


def test_cancelled_worker_retains_completed_incumbent_without_apply(harness):
    from tests.test_film_sensitivity_quick_controller import _PhysicsClient
    h = harness
    _configure(h)
    client = _PhysicsClient()
    client.cancel_stage = "search"
    h.panel.client_factory = lambda _root: client
    assert h.owner.startOptimization(), h.panel.idealError
    h.pool.run()
    payload = h.panel._ideal_payload
    assert payload is not None and payload["state"] == "cancelled"
    assert payload["best_design"] is not None
    assert h.owner.hasCandidate
    assert not h.owner.canApply


def test_common_apply_rejects_stale_source(harness):
    h = harness
    _configure(h)
    assert h.owner.startOptimization(), h.panel.idealError
    h.pool.run()
    assert h.panel.idealHasResult
    h.session.design_revision += 1
    assert not h.owner.canApply
    assert not h.owner.confirmApplyCandidate()
    assert not h.source.applied
