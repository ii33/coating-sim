"""Actual source apply/undo and persisted typed queue-view routing."""
from copy import deepcopy
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QThreadPool
from PySide6.QtQml import QQmlApplicationEngine
import shiboken6

from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.scheduler.jobs import JobSpec, freeze_spec
from coat_sim.ui.compute_queue import QueueResultViewer, _ManufacturingQueueResult, _read_queue_result
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture
from tests.test_film_manufacturing_jobs import execute, request_fixture
from tests.test_film_manufacturing_quick_controller import HeldPool, PhysicsClient
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene

pytestmark = pytest.mark.qt


class Store:
    def __init__(self, root, row):
        self.root, self.row = root, row

    def read_job(self, _job_id):
        return deepcopy(self.row)


def _bind(root, row, request):
    spec = freeze_spec(None, "film_manufacturing_repair", {"payload": request})
    frozen = JobSpec.parse_frozen(spec)
    row["input_digest"] = frozen.fingerprint
    directory = root / "artifacts/runtime/scheduler/jobs" / row["job_id"]
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text(frozen.canonical_json, encoding="utf-8")


def test_history_binding_no_extra_optics_and_no_wrong_product_route(tmp_path, monkeypatch):
    _, request, job = request_fixture()
    payload = execute(job, tmp_path)
    row = {"job_id": "manufacturing-view", "kind": "film_manufacturing_repair",
           "state": "succeeded", "result": payload}
    _bind(tmp_path, row, request)
    from coat_sim.core import film_manufacturing_jobs as jobs
    monkeypatch.setattr(jobs, "evaluate_frozen_structure_candidate", lambda *a, **k: pytest.fail("extra history optics"))
    loaded, path = _read_queue_result(None, Store(tmp_path, row), row["job_id"])
    assert isinstance(loaded, _ManufacturingQueueResult) and path == ""
    assert loaded.payload == payload
    wrong = deepcopy(row)
    wrong["kind"] = "process"
    with pytest.raises(ValueError, match="identity"):
        _read_queue_result(None, Store(tmp_path, wrong), row["job_id"])
    wrong = deepcopy(row)
    wrong["input_digest"] = "unbound"
    with pytest.raises(ValueError, match="digest"):
        _read_queue_result(None, Store(tmp_path, wrong), row["job_id"])
    cancelled = deepcopy(row)
    cancelled["state"] = "cancelled"
    loaded, _ = _read_queue_result(None, Store(tmp_path, cancelled), row["job_id"])
    assert loaded.payload["state"] == "cancelled"
    assert loaded.payload["result"] == payload["result"]


@pytest.mark.parametrize("automatic", [False, True])
def test_real_source_apply_once_undo_and_stale_material_guard(tmp_path, monkeypatch, automatic):
    service, library, design = fixture(tmp_path)
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    pool, client = HeldPool(), PhysicsClient(tmp_path)
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: pool)
    try:
        assert owner.setObjectiveField("wavelength_min_nm", 550.)
        assert owner.setObjectiveField("wavelength_max_nm", 550.)
        panel = cast(FilmManufacturingQuickController, owner.manufacturingController)
        panel.client_factory = lambda _root: client
        assert panel.prepare(), panel.error
        original = deepcopy(session.snapshot().design)
        original_revision = session.snapshot().design_revision
        if not automatic:
            assert panel.setLayerSelected(cast(list[dict], panel.property("layerRows"))[0]["layer_id"], True)
        options = (("reference_nm","110"),("thickness_tolerance_nm","1"),("optical_tolerance_percentage_points","1")) if automatic else (("reference_nm","110"),("refinement_evaluations_per_route","2"),("max_iterations_per_route","2"),("seconds_per_route","10"))
        for key, text in options:
            assert panel.setOption(key, text)
        assert panel.start(), panel.error
        assert owner.busy and not owner.canStart
        pool.run()
        QCoreApplication.processEvents()
        assert panel.hasResult and not panel.busy, panel.error
        assert session.snapshot().design == original
        assert session.snapshot().design_revision == original_revision
        assert not source.canUndoOptimizedDesign
        assert panel.setSelectedRoute("automatic" if automatic else "raise_selected")
        assert panel.canApply, panel.error
        material = library.read("acceptance-film")["record"]
        altered = deepcopy(material)
        altered["optical_constants"][0]["n"] = [1.3] * len(altered["optical_constants"][0]["n"])
        library.write(altered, update=True)
        assert not panel.applySelectedRoute()
        assert session.snapshot().design == original
        library.write(material, update=True)
        assert panel.canApply
        with monkeypatch.context() as readiness:
            readiness.setattr(owner, "_build_request", lambda **_kwargs: pytest.fail("readiness expanded an optical request"))
            readiness.setattr(service.materials, "get", lambda *_args: pytest.fail("readiness reread the catalog"))
            assert panel.canApply
        assert panel.applySelectedRoute(), panel.error
        assert source.canUndoOptimizedDesign
        assert session.snapshot().design_revision == original_revision + 1
        assert session.snapshot().design != original
        assert {row['layer_id'] for row in cast(list[dict], owner.property('variables'))} == {row['id'] for row in session.snapshot().design['front_layers']}
        assert not panel.applySelectedRoute()
        assert source.undoOptimizedDesign()
        assert session.snapshot().design == original
        assert {row['layer_id'] for row in cast(list[dict], owner.property('variables'))} == {row['id'] for row in original['front_layers']}
        assert not source.canUndoOptimizedDesign
    finally:
        owner.shutdown(1000)
        source.markSourceClosed()
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(owner)
        shiboken6.delete(source)


def test_automatic_topology_apply_undo_and_source_reuse_keep_full_variable_model(tmp_path, monkeypatch):
    from coat_sim.core import film_automatic_repair as automatic

    service, _library, design = fixture(tmp_path)
    template = design['front_layers'][0]
    design['front_layers'] = [{**deepcopy(template), 'id':name,
        'thickness':{'value':thickness,'unit':'nm'}} for name,thickness in
        (('left',40.),('thin',.5),('right',40.))]
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service,edit_session=session)
    owner = FilmOptimizationQuickController(source)
    pool,client = HeldPool(),PhysicsClient(tmp_path)
    monkeypatch.setattr(QThreadPool,'globalInstance',lambda:pool)
    original_candidates = automatic.structural_candidates

    def deletion_branch_first(*args,**kwargs):
        # Exercise the editor's topology handoff with a legal existing
        # candidate; real local optics and the independent guard still run.
        for name,candidate in original_candidates(*args,**kwargs):
            if name == 'delete_thin_layers_merge':
                yield name,candidate
                return

    monkeypatch.setattr(automatic,'structural_candidates',deletion_branch_first)
    replacement = None
    try:
        assert owner.setObjectiveField('wavelength_min_nm',550.)
        assert owner.setObjectiveField('wavelength_max_nm',550.)
        panel = cast(FilmManufacturingQuickController,owner.manufacturingController)
        panel.client_factory = lambda _root:client
        assert panel.prepare(),panel.error
        before = deepcopy(session.snapshot().design)

        def run_automatic():
            for key,value in (('reference_nm','1'),('thickness_tolerance_nm','.1'),('optical_tolerance_percentage_points','5')):
                assert panel.setOption(key,value)
            assert panel.startAutomatic(),panel.error
            pool.run()
            QCoreApplication.processEvents()
            assert panel.hasResult and not panel.busy,panel.error

        run_automatic()
        payload = panel._payload
        assert payload is not None and payload['schema_version'] == 2
        assert len(payload['result']['best']['design']['front_layers']) == 1
        source_layers = payload['result']['source']['design']['front_layers']
        assert source_layers[1]['id'] == 'thin'
        assert source_layers[1]['thickness']['value'] == .5 < payload['result']['lower_limit_nm']
        assert payload['result']['best']['thickness_pass'] is True
        details = {row['route']:row['details'] for row in
                   cast(list[dict],panel.property('comparisonRows'))}
        assert '保留层厚度下限复验：通过。' not in details['original']
        assert '保留层厚度下限复验：通过。' in details['automatic']
        assert panel.canApply,panel.error
        assert panel.applySelectedRoute(),panel.error
        applied = session.snapshot().design
        assert len(applied['front_layers']) == 1
        assert {r['layer_id'] for r in cast(list[dict], owner.property('variables'))} == {r['id'] for r in applied['front_layers']}
        assert source.undoOptimizedDesign()
        QCoreApplication.processEvents()
        assert session.snapshot().design == before
        assert {r['layer_id'] for r in cast(list[dict], owner.property('variables'))} == {'left','thin','right'}
        replacement = FilmDesignQuickController(service=service,edit_session=session)
        assert owner.reuseWindowOpen('',replacement)
        assert owner.manufacturingController is panel
        assert panel.prepare(),panel.error
        run_automatic()
        reused = panel._payload
        assert reused is not None
        assert reused['request']['source_design']['front_layers'] == before['front_layers']
        assert len(reused['result']['best']['design']['front_layers']) == 1
        assert session.snapshot().design == before
    finally:
        owner.shutdown(1000)
        if replacement is not None:
            replacement.markSourceClosed()
            shiboken6.delete(replacement)
        source.markSourceClosed()
        cast(ComputeTarget,owner.computeTarget)._executor.shutdown(wait=True,cancel_futures=True)
        shiboken6.delete(owner)
        shiboken6.delete(source)


def test_optimizer_toolbar_opens_repair_and_preserves_editor_drafts(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (owner, _source, _session):
        configuration = _session.workflow_configuration
        monkeypatch.setattr(
            _session, "workflow_configuration",
            lambda scope: {} if scope == "simulation" else configuration(scope),
        )
        scene = _Scene(owner, "FilmOptimizationWindow.qml")
        try:
            scene.window.setWidth(1500)
            QCoreApplication.processEvents()
            opener = scene.item("filmManufacturingOpenButton")
            scene.click(opener)
            dialog = scene.window.findChild(QObject, "filmManufacturingRepairDialog")
            assert dialog is not None and dialog.property("visible")
            assert owner.manufacturingController.options["reference_nm"] == ""
            assert owner.manufacturingController.setOption("reference_nm", "123")
            from PySide6.QtCore import QMetaObject
            assert QMetaObject.invokeMethod(dialog, "close")
            QCoreApplication.processEvents()
            assert not dialog.property("visible")
            scene.click(opener)
            assert dialog.property("visible")
            assert owner.manufacturingController.options["reference_nm"] == "123"
            assert not owner.manufacturingController.hasResult
        finally:
            scene.close()


def test_readonly_history_uses_shared_geometry_before_show_and_reuses_window(tmp_path):
    _, _request, job = request_fixture()
    payload = execute(job, tmp_path)

    class Viewer(QueueResultViewer):
        def __init__(self):
            # Isolate the production history-window seam from the unrelated
            # simulation MainWindow and queue client's process startup.
            QObject.__init__(self)
            self.engine = QQmlApplicationEngine(self)
            ThemeService.instance().attach_engine(self.engine)
            self.project_context = None
            self._manufacturing_window = None
            self._manufacturing_controller = None
            self._manufacturing_component = None

    viewer = Viewer()
    try:
        viewer._show_manufacturing_result("history-geometry", payload)
        window = viewer._manufacturing_window
        assert window is not None and window.isVisible()
        assert window.property("_coat_sim_geometry_prepared") is True
        area = window.screen().availableGeometry()
        assert area.contains(window.geometry())
        assert viewer._manufacturing_controller is not None
        assert viewer._manufacturing_controller._context is None
        assert not viewer._manufacturing_controller.canApply
        viewer._show_manufacturing_result("history-geometry", payload)
        assert viewer._manufacturing_window is window
    finally:
        if viewer._manufacturing_controller is not None:
            viewer._manufacturing_controller.shutdown()
        if viewer._manufacturing_window is not None:
            viewer._manufacturing_window.hide()
            shiboken6.delete(viewer._manufacturing_window)
        shiboken6.delete(viewer.engine)
        shiboken6.delete(viewer)
