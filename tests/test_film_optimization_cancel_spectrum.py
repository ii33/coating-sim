"""Real source-opener/companion routes coalesce only automatic spectrum refreshes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import os
from threading import Event, current_thread, main_thread
import time
from types import SimpleNamespace
from typing import Any, cast

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QSettings
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtQml import QQmlApplicationEngine

from coat_sim.core.film_design_service import FilmDesignService, active_material_references
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    freeze_film_optimization_request,
)
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from coat_sim.ui.launcher import LauncherBridge, WindowHandle, WindowOpenRequest
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_cancel_apply_review import arm, cancelled_result, deliver

pytestmark = pytest.mark.qt


class ReadyExecutor(ThreadPoolExecutor):
    """Real tiny TMM completes before add_done_callback, a legal Future schedule."""

    def submit(self, *args, **kwargs):
        future = super().submit(*args, **kwargs)
        future.result(timeout=5.)
        assert future.done()
        return future


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def wait_done(simulation):
    deadline = time.monotonic() + 5.
    while simulation._busy and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert not simulation._busy and simulation._result is not None and not simulation._stale


@contextmanager
def spectrum_case(tmp_path, monkeypatch, *, route='launcher', ready=True):
    original_service, library, design = fixture(tmp_path / 'materials')
    engine = QQmlApplicationEngine()
    windows: list[QWindow] = []
    project_routes: list[Any] = []
    workspace: Any = None
    if route == 'project':
        from coat_sim.core.project_film_session import ProjectFilmWorkspace
        from coat_sim.core.project_store import ProjectStore
        from coat_sim.project_session import ProjectSessionHost
        from coat_sim.ui.project_film_host import ProjectFilmDesktop
        store = ProjectStore.create(tmp_path / 'project', 'Spectrum coalescing')
        host = ProjectSessionHost(lambda _root: store)
        workspace = ProjectFilmWorkspace.for_project(host, host.open_project(store.root).binding)
        materials = {}
        for ref in active_material_references(design):
            record = original_service.materials.get(ref['material_id'])
            materials[ref['material_id']] = {key: record[key] for key in (
                'id', 'name', 'optical_sets', 'design_ready', 'notes') if key in record}
        saved = workspace.service.create_film(design, materials=materials)
        assert workspace.editor.load_saved_design(saved.document_id)
        desktop: Any = ProjectFilmDesktop(host)
        service: Any = workspace.service
        source = FilmDesignQuickController(service=service, parent=desktop,
            edit_session=workspace.editor, project_context=workspace)
    else:
        desktop = LauncherBridge(engine, with_launcher=False,
            window_settings=QSettings(str(tmp_path / 'launcher.ini'), QSettings.Format.IniFormat))
        service = original_service
        source = FilmDesignQuickController(root=tmp_path, service=service, parent=desktop,
            geometry_settings=QSettings(str(tmp_path / 'source.ini'), QSettings.Format.IniFormat))
        assert source.session is not None and source.session.request_replace_design(design)
    simulation = FilmSimulationQuickController(root=service.root if route == 'project' else tmp_path,
        service=service, source_controller=source)
    optimization = FilmOptimizationQuickController(source)
    window = QWindow()
    windows.append(window)
    handle = WindowHandle('existing-spectrum', 'film_simulation', window, simulation, None, None,
        open_payload=source)
    if route == 'project':
        for key, controller in [('film_design', source), ('film_simulation', simulation)]:
            owned_window = window if key == 'film_simulation' else QWindow()
            if owned_window is not window:
                windows.append(owned_window)
            cast(Any, owned_window)._project_film_controller = controller
            reservation = workspace.reserve_module(key)
            workspace.host.attach_window(reservation.route, owned_window)
            project_routes.append(reservation.route)
    elif route == 'pending':
        request = WindowOpenRequest(handle.instance_id, handle.key, payload=source)
        desktop._open_requests[handle.instance_id] = (SimpleNamespace(key=handle.key), request)
        desktop._pending_qml[handle.instance_id] = SimpleNamespace(bridge=simulation, on_ready=object())
    else:
        desktop._windows[handle.instance_id] = handle
    calls: list[Any] = []
    gate = Event()
    original_simulate = FilmDesignService.simulate

    def counted(worker_service, candidate, settings=None, *, cancel_event=None):
        if settings and settings.get('samples') == 2:
            calls.append({'design': deepcopy(candidate), 'settings': deepcopy(settings)})
            if not ready and len(calls) == 1:
                assert gate.wait(5.), 'test must release the held first real TMM'
        return original_simulate(worker_service, candidate, settings, cancel_event=cancel_event)

    monkeypatch.setattr(FilmDesignService, 'simulate', counted)
    executor = ReadyExecutor(max_workers=1) if ready else ThreadPoolExecutor(max_workers=1)
    simulation._executor = executor
    try:
        assert simulation.setSimulationSettings({'wavelength_min_nm': 550., 'wavelength_max_nm': 551.,
            'samples': 2, 'angle_deg': 0., 'polarization': 'unpolarized'})
        assert source.session is not None
        base = source.session.snapshot()
        frozen_request = freeze_film_optimization_request(service, FilmOptimizationRequest(base.design,
            base.design_revision,
            (FilmThicknessVariable('front', design['front_layers'][0]['id'], 0., None),),
            (FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=None, upper_bound=.001)),),
            max_iterations=2, max_evaluations=8))
        harness = SimpleNamespace(source=source, controller=optimization, base=base, request=frozen_request,
            simulation=simulation, calls=calls, gate=gate, desktop=desktop, handle=handle,
            service=service, library=library, workspace=workspace)
        if route == 'project':
            assert optimization._project_compute is not None
            operation = optimization._project_compute.capture('review-job-1')
            optimization._project_operation = operation
            harness.queue_origin = optimization._project_compute.metadata(operation)['origin']
        yield harness
    finally:
        gate.set()
        optimization.shutdown(1000)
        cast(ComputeTarget, optimization.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        simulation.close()
        executor.shutdown(wait=True, cancel_futures=True)
        if route == 'project':
            for reserved in project_routes:
                workspace.host.release_window(reserved)
        else:
            desktop._windows.clear()
            desktop._open_requests.clear()
            desktop._pending_qml.clear()
        for owned_window in windows:
            owned_window.close()
            owned_window.deleteLater()
        optimization.deleteLater()
        simulation.deleteLater()
        source.deleteLater()
        desktop.deleteLater()
        engine.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize('route', ['launcher', 'pending', 'project'])
def test_completed_future_cancel_uses_one_real_tmm_through_real_companion_route(app, tmp_path, monkeypatch, route):
    with spectrum_case(tmp_path, monkeypatch, route=route) as case:
        arm(case)
        result = cancelled_result(case)
        case.controller.cancelOptimization()
        deliver(case, result)
        assert case.simulation._busy or case.simulation._result is not None, (
            case.controller.error, case.controller.statusText, case.simulation._error,
            case.simulation._status, case.source.session.snapshot().design_revision,
            case.controller._cancel_intent_accepted, case.controller._cancel_intent_consumed)
        wait_done(case.simulation)
        assert len(case.calls) == 1
        after = case.source.session.snapshot()
        assert case.simulation._source_design_revision == after.design_revision
        assert case.simulation._design == after.design
        assert case.source.openSimulationAndCalculate()
        assert len(case.calls) == 1


def test_busy_cancel_refresh_joins_the_inflight_real_tmm(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch, ready=False) as case:
        arm(case)
        case.controller.cancelOptimization()
        deliver(case, cancelled_result(case))
        assert case.simulation._busy and not case.simulation._pending_recompute
        case.gate.set()
        wait_done(case.simulation)
        assert len(case.calls) == 1


@pytest.mark.parametrize('already_fresh', [False, True])
def test_same_design_cancel_computes_once_or_reuses_the_existing_fresh_spectrum(app, tmp_path, monkeypatch, already_fresh):
    with spectrum_case(tmp_path, monkeypatch) as case:
        if already_fresh:
            case.simulation.requestCalculation()
            wait_done(case.simulation)
        arm(case)
        case.controller.cancelOptimization()
        deliver(case, cancelled_result(case, same=True))
        wait_done(case.simulation)
        assert len(case.calls) == 1
        assert case.source.session.snapshot().design_revision == case.base.design_revision


def test_manual_calculate_still_forces_a_fresh_tmm(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch) as case:
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        case.simulation.requestCalculation()
        wait_done(case.simulation)
        assert len(case.calls) == 2
        case.simulation.requestSourceRefresh()
        assert len(case.calls) == 2


@pytest.mark.parametrize('change', ['same_uuid_nk', 'provider', 'settings'])
def test_automatic_refresh_recomputes_changed_optical_or_simulation_input(app, tmp_path, monkeypatch, change):
    with spectrum_case(tmp_path, monkeypatch) as case:
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        revision = case.source.session.snapshot().design_revision
        if change == 'same_uuid_nk':
            row = case.library.read('acceptance-film')['record']
            row['optical_constants'][0]['n'] = [2.7, 2.7]
            case.library.write(row, update=True)
        elif change == 'provider':
            from coat_sim.core.project_film_service import ProjectOpticalSnapshot
            rows = deepcopy(case.request.frozen_material_snapshot['materials'])
            row = next(item for item in rows if item['id'] == 'acceptance-film')
            row['optical_sets'][0]['n'] = [2.8, 2.8]
            case.service.materials = ProjectOpticalSnapshot(rows)
        else:
            assert case.simulation.setSimulationSettings({'wavelength_min_nm': 551., 'wavelength_max_nm': 552.,
                'samples': 2, 'angle_deg': 0., 'polarization': 'unpolarized'})
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        assert len(case.calls) == 2
        assert case.source.session.snapshot().design_revision == revision


def test_deferred_companion_start_reuses_the_completed_source_refresh(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch) as case:
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        request = WindowOpenRequest(case.handle.instance_id, 'film_simulation', payload=case.source)
        case.desktop._apply_deferred_open_intent(case.handle.instance_id, request, start=True)
        assert len(case.calls) == 1


def test_closed_source_never_reuses_a_previous_fresh_refresh(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch) as case:
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        case.source.markSourceClosed()
        case.simulation.requestSourceRefresh()
        assert len(case.calls) == 1


def test_busy_material_change_queues_one_recompute_for_the_new_input(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch, ready=False) as case:
        case.simulation.requestSourceRefresh()
        row = case.library.read('acceptance-film')['record']
        row['optical_constants'][0]['n'] = [2.7, 2.7]
        case.library.write(row, update=True)
        case.simulation.requestSourceRefresh()
        assert case.simulation._pending_recompute
        case.gate.set()
        wait_done(case.simulation)
        assert len(case.calls) == 2


def test_unknown_material_identity_does_not_swallow_busy_source_refresh(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch, ready=False) as case:
        provider = case.service.materials

        class RecordsOnlyMaterials:
            def _records(self):
                return provider._records()

        case.service.materials = RecordsOnlyMaterials()
        case.simulation.requestSourceRefresh()
        assert case.simulation._active_source_refresh_identity is None
        case.simulation.requestSourceRefresh()
        assert case.simulation._pending_recompute
        case.gate.set()
        wait_done(case.simulation)
        assert len(case.calls) == 2


def test_project_frozen_material_identity_is_not_relabelled_by_live_edit(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch, route='project') as case:
        freeze = case.service.freeze_design
        get = case.service.materials.get

        def edited_get(material_id):
            record = deepcopy(get(material_id))
            if material_id == 'acceptance-film':
                record['optical_sets'][0]['n'] = [2.7, 2.7]
            return record

        def freeze_then_edit(*args, **kwargs):
            snapshot = freeze(*args, **kwargs)
            monkeypatch.setattr(case.service.materials, 'get', edited_get)
            return snapshot

        monkeypatch.setattr(case.service, 'freeze_design', freeze_then_edit)
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        monkeypatch.setattr(case.service, 'freeze_design', freeze)
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        assert len(case.calls) == 2


def test_busy_merged_refresh_retries_when_real_worker_material_differs(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch, ready=False) as case:
        records = case.service.materials._records
        reads = []

        def worker_records(*args, **kwargs):
            rows = deepcopy(records(*args, **kwargs))
            if current_thread() is main_thread():
                return rows
            reads.append(True)
            if len(reads) == 1:
                for row in rows:
                    if row['id'] == 'acceptance-film':
                        row['optical_sets'][0]['n'] = [2.7, 2.7]
            return rows

        monkeypatch.setattr(case.service.materials, '_records', worker_records)
        case.simulation.requestSourceRefresh()
        case.simulation.requestSourceRefresh()
        assert case.simulation._active_source_refresh_merged
        case.gate.set()
        wait_done(case.simulation)
        assert len(case.calls) == 2
        assert case.simulation._completed_source_refresh_identity is not None


def test_changed_source_binding_does_not_refresh_old_session(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch) as case:
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        monkeypatch.setattr(case.source, 'session', object())
        case.simulation.requestSourceRefresh()
        assert len(case.calls) == 1


def test_requested_polarizations_with_different_real_materials_are_not_reused(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch) as case:
        records = case.service.materials._records
        reads = []

        def worker_records(*args, **kwargs):
            rows = deepcopy(records(*args, **kwargs))
            if current_thread() is not main_thread():
                reads.append(True)
                if len(reads) == 2:
                    for row in rows:
                        if row['id'] == 'acceptance-film':
                            row['optical_sets'][0]['n'] = [2.7, 2.7]
            return rows

        monkeypatch.setattr(case.service.materials, '_records', worker_records)
        case.simulation._plot_settings['polarizations'] = ['unpolarized', 's']
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        assert case.simulation._result['_source_refresh_provenance_consistent'] is False
        assert case.simulation._completed_source_refresh_identity is None
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        assert len(case.calls) == 4
        assert case.simulation._completed_source_refresh_identity is not None


def test_failed_calculation_cannot_reuse_an_older_complete_result(app, tmp_path, monkeypatch):
    with spectrum_case(tmp_path, monkeypatch) as case:
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        simulate = case.service.simulate

        def fail(*args, **kwargs):
            raise ValueError('controlled spectrum failure')

        monkeypatch.setattr(case.service, 'simulate', fail)
        case.simulation.requestCalculation()
        assert case.simulation._stale and not case.simulation._busy
        monkeypatch.setattr(case.service, 'simulate', simulate)
        case.simulation.requestSourceRefresh()
        wait_done(case.simulation)
        assert len(case.calls) == 2
        assert case.simulation._next_calculation_id == 3
