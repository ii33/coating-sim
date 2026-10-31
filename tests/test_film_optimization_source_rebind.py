"""Temporary optimizer source changes preserve live controller lifetimes."""
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication
import shiboken6

from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController
from tests.film_structure_fixtures import fixture
from tests.test_film_sensitivity_workflows_gui import (
    _WorkflowPhysicsClient,
    app as app,
)
from tests.test_film_sensitivity_quick_controller import _HeldPool
from tests.test_film_synthesis_gui_review import _wait
from tests.test_project_compute_controllers import owners as owners
from tests.test_project_film_controller_adapter import MemorySettings

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def rebind(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    assert owner.setObjectiveField("wavelength_min_nm", 550.)
    assert owner.setObjectiveField("wavelength_max_nm", 550.)
    panel = cast(FilmSensitivityQuickController, owner.sensitivityController)
    assert panel.prepare(), panel.error
    for field, value in dict(bias_text="0", bias_unit="absolute_nm", uncertainty_kind="stddev",
                             uncertainty_text="0.25", uncertainty_unit="absolute_nm").items():
        assert panel.setTableDefault(field, value)
    assert panel.setTableUncorrelated(True)
    configuration = deepcopy(owner.exportOptimizationConfiguration())
    replacement = deepcopy(design)
    replacement["front_layers"][0]["id"] = "new-document-layer"
    replacement["front_layers"][0]["thickness"]["value"] = 90.
    new_session = FilmDesignEditSession(service)
    assert new_session.request_replace_design(replacement)
    configuration["variables"][0]["layer_id"] = "new-document-layer"
    configuration["sensitivity_table"]["defaults"]["uncertainty_text"] = "0.75"
    configuration["sensitivity_table"]["layers"][0]["layer_id"] = "new-document-layer"
    assert new_session.set_workflow_configuration("optimization", configuration)
    new_source = FilmDesignQuickController(service=service, edit_session=new_session)
    pool, client = _HeldPool(), _WorkflowPhysicsClient()
    cast(Any, panel).pool = pool
    panel.client_factory = lambda _root: client
    try:
        yield SimpleNamespace(owner=owner, panel=panel, source=source, session=session,
            new_source=new_source, new_session=new_session, pool=pool, client=client)
    finally:
        owner.shutdown(1000)
        source.markSourceClosed()
        new_source.markSourceClosed()
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(owner)
        shiboken6.delete(source)
        shiboken6.delete(new_source)


def test_real_source_rebind_restores_new_table_and_can_run_again_with_same_controller(rebind):
    h = rebind
    old_configuration = h.session.workflow_configuration("optimization")
    assert h.owner.reuseWindowOpen("", h.new_source)
    assert h.owner.sensitivityController is h.panel
    assert not h.panel._closed
    assert not h.owner.error, h.owner.error
    assert h.panel.exportTableConfiguration()["defaults"]["uncertainty_text"] == "0.75"
    assert h.panel.prepare(), h.panel.error
    assert [row["layer_id"] for row in h.panel.thicknessErrorRows] == ["new-document-layer"]
    assert h.panel.setTableDefault("uncertainty_text", "0.5")
    assert h.new_session.workflow_configuration("optimization")["sensitivity_table"]["defaults"]["uncertainty_text"] == "0.5"
    assert h.session.workflow_configuration("optimization") == old_configuration
    assert h.panel.startThicknessErrorTableAnalysis(), h.panel.tableError
    h.pool.run()
    assert not h.panel.busy and h.panel.tableHasResult, h.panel.tableError
    assert len(h.client.submissions) == 1


def test_source_rebind_cancels_old_task_and_fences_old_callbacks_without_reusing_its_event(rebind):
    h = rebind
    assert h.panel.startThicknessErrorTableAnalysis()
    task = h.pool.tasks[-1]
    old_generation, old_event, old_cancel = h.panel._generation, h.panel._disconnected, h.panel._cancel
    assert old_cancel is not None
    assert h.owner.reuseWindowOpen("", h.new_source)
    assert h.owner.sensitivityController is h.panel and not h.panel._closed
    assert old_event.is_set() and old_cancel.is_set()
    assert h.panel._disconnected is not old_event and not h.panel._disconnected.is_set()
    assert h.panel._generation > old_generation and not h.panel.busy
    assert h.panel._context is h.panel._table_context is h.panel._ideal_context is None
    task.run()  # It never reaches admission, even after the new source is open.
    QCoreApplication.processEvents()
    assert not h.client.submissions and not h.panel._closed
    assert h.panel.prepare(), h.panel.error
    assert h.panel.startThicknessErrorTableAnalysis(), h.panel.tableError
    new_generation, new_job = h.panel._generation, h.panel.tableJobId
    new_defaults = deepcopy(h.panel.exportTableConfiguration())
    h.panel._signals.admitted.emit(old_generation)
    h.panel._signals.progress.emit(old_generation, dict(stage="old source", message="stale callback"))
    h.panel._signals.finished.emit(old_generation, dict(state="completed", termination_reason="old source"), "")
    QCoreApplication.processEvents()
    assert h.panel._generation == new_generation and h.panel.busy
    assert h.panel.tableJobId == new_job and not h.panel.tableHasResult
    assert h.panel.exportTableConfiguration() == new_defaults
    assert "stale callback" not in h.panel.tableStatus
    h.pool.run()
    assert not h.panel.busy and h.panel.tableHasResult, h.panel.tableError
    assert len(h.client.submissions) == 1 and old_event.is_set()


def test_real_close_is_permanent_and_does_not_reopen_a_closed_sensitivity_controller(rebind):
    h = rebind
    assert h.owner.reuseWindowOpen("", h.new_source)
    assert h.panel.prepare()
    assert h.panel.startThicknessErrorTableAnalysis()
    generation, event, cancel = h.panel._generation, h.panel._disconnected, h.panel._cancel
    assert cancel is not None
    h.owner.detach_observers()
    assert h.panel._closed and event.is_set() and cancel.is_set()
    configuration = deepcopy(h.panel.exportTableConfiguration())
    h.panel.sourceChanged()
    assert h.panel._closed and h.panel._disconnected is event
    assert not h.panel.prepare()
    assert not h.panel.loadTableConfiguration(configuration)
    assert not h.panel.startThicknessErrorTableAnalysis()
    h.panel._on_finished(generation, dict(state="completed", termination_reason="late"), "")
    assert not h.panel.tableHasResult


@pytest.mark.parametrize("moment", ["before_admission", "before_receipt", "admitted"])
def test_project_source_rebind_detaches_durable_jobs_without_cancel_rpc_and_new_source_runs(owners, app, moment):
    """Real project ownership and physics; controlled transport exposes receipt races."""
    workspace, source, _saved = owners[1][0]
    assert source.session.add_layer("front")
    layer_id = source.currentDesign()["front_layers"][0]["id"]
    assert source.session.set_layer_value("front", layer_id, "material_id", "shared")
    assert source.session.set_layer_value("front", layer_id, "optical_set_id", "visible")
    owner = FilmOptimizationQuickController(source, project_context=workspace)
    replacement = FilmDesignQuickController(service=workspace.service, edit_session=workspace.editor,
        project_context=workspace, geometry_settings=MemorySettings())
    switched: list[bool] = []
    class ReceiptClient(_WorkflowPhysicsClient):
        held = True
        cancellations: list[str]
        def __init__(self):
            super().__init__()
            self.cancellations = []
        def submit(self, spec, *, job_id, metadata):
            if not self.held:
                return super().submit(spec, job_id=job_id, metadata=metadata)
            frozen = JobSpec.parse_frozen(spec)
            self.submissions.append((deepcopy(spec), job_id, deepcopy(metadata)))
            self.rows[job_id] = dict(job_id=job_id, kind=frozen.executor, state="running",
                                    input_digest=frozen.fingerprint)
            if moment == "before_receipt":
                switch()
            return deepcopy(self.rows[job_id])
        def wait_for_terminal(self, job_id, *, should_stop, **_kwargs):
            if not self.held:
                return self.status(job_id)
            if moment == "admitted":
                switch()
            assert should_stop()
            raise InterruptedError("The old source observer was detached; the durable job remains running")
        def cancel(self, job_id):
            self.cancellations.append(job_id)
            return super().cancel(job_id)
    client = ReceiptClient()
    def switch():
        if not switched:
            assert owner.reuseWindowOpen("", replacement), owner.error
            switched.append(True)
    try:
        _wait(lambda: not owner._initial_loading)
        # The project fixture deliberately has an old partial workflow draft.
        # Restore a complete current configuration before starting this test.
        assert owner.loadOptimizationConfiguration(owner.exportOptimizationConfiguration()), owner.error
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = cast(FilmSensitivityQuickController, owner.sensitivityController)
        pool = _HeldPool()
        cast(Any, panel).pool = pool
        panel.client_factory = lambda _root: client
        assert panel.prepare(), panel.error
        for field, value in dict(bias_text="0", bias_unit="absolute_nm", uncertainty_kind="stddev",
                                 uncertainty_text="0.25", uncertainty_unit="absolute_nm").items():
            assert panel.setTableDefault(field, value)
        assert panel.setTableUncorrelated(True)
        assert panel.startThicknessErrorTableAnalysis(), panel.tableError
        old_event, old_cancel = panel._disconnected, panel._cancel
        assert old_cancel is not None
        if moment == "before_admission":
            switch()
        pool.run()
        assert switched and owner.sensitivityController is panel and not panel._closed
        assert old_event.is_set() and not old_cancel.is_set()
        assert panel._disconnected is not old_event and not panel._disconnected.is_set()
        assert not client.cancellations
        old_job = None
        if moment == "before_admission":
            assert not client.submissions
        else:
            old_job = client.submissions[0][1]
            assert client.rows[old_job]["state"] == "running"
        client.held = False
        assert panel.prepare(), panel.error
        assert panel.startThicknessErrorTableAnalysis(), panel.tableError
        pool.run()
        assert not panel.busy and panel.tableHasResult, panel.tableError
        assert owner._source_controller is replacement and not client.cancellations
        if old_job is not None:
            assert client.rows[old_job]["state"] == "running"
    finally:
        owner.shutdown(1000)
        replacement.markSourceClosed()
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(owner)
        shiboken6.delete(replacement)


def test_ideal_draft_persists_by_stable_ids_and_reopens(rebind):
    h = rebind
    panel = h.panel
    assert panel.setIdealSensitivityMode("relative")
    assert panel.setIdealSensitivityPolicy("bounded_baseline_degradation")
    objective = panel.idealDegradationObjectives[0]["objective_id"]
    assert panel.setIdealDegradationBudget(objective, "0.42")
    layer = panel.idealSensitivityLayers[0]["layer_id"]
    assert panel.setIdealSensitivityLayer(layer, False)
    assert panel.setIdealOption("max_iterations", "123")
    draft = h.session.workflow_configuration("optimization")["sensitivity_ideal"]
    assert draft["mode"] == "relative" and draft["budgets"][objective] == "0.42"
    assert draft["selection_explicit"] and layer not in draft["layer_ids"]
    assert draft["options"]["max_iterations"] == "123"
    # A fresh owner represents closing and reopening the optimizer.
    reopened = FilmOptimizationQuickController(h.source)
    try:
        restored = cast(FilmSensitivityQuickController, reopened.sensitivityController)
        assert restored.prepare(), restored.error
        assert restored.exportIdealConfiguration() == draft
        assert cast(list[dict], restored.idealDegradationObjectives)[0]["budget_text"] == "0.42"
    finally:
        reopened.shutdown(1000)
        cast(ComputeTarget, reopened.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(reopened)
