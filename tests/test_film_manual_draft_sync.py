"""Manual spectra capture the exact live draft, without loading a saved film."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal

from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from tests.film_structure_fixtures import fixture

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class _Session:
    def __init__(self, design: dict[str, Any]) -> None:
        self.design = deepcopy(design)
        self.design_revision = 1
        self.writes: list[dict[str, Any]] = []

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(design=deepcopy(self.design),
                               design_revision=self.design_revision, path="", dirty=True)

    def replace(self, design: dict[str, Any]) -> None:
        self.design = deepcopy(design)
        self.design_revision += 1

    def set_workflow_configuration(self, scope: str, configuration: dict[str, Any]) -> bool:
        self.writes.append(deepcopy(configuration))
        self.design.setdefault("workflow_settings", {})[scope] = deepcopy(configuration)
        return True


class _Source(QObject):
    changed = Signal()
    optimizationApplied = Signal()

    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session


class _HeldExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.futures: list[Future[Any]] = []

    def submit(self, _function: Any, *args: Any) -> Future[Any]:
        self.calls.append(args)
        future: Future[Any] = Future()
        self.futures.append(future)
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        assert not wait and cancel_futures
        for future in self.futures:
            future.cancel()


@pytest.fixture
def paired(tmp_path: Any, monkeypatch: Any) -> Any:
    service, _library, design = fixture(tmp_path)
    session = _Session(design)
    source = _Source(session)
    controller = FilmSimulationQuickController(service=service, source_controller=source)
    executor = _HeldExecutor()
    monkeypatch.setattr(controller, "_executor", executor)
    # A narrow ownership fixture omits the unrelated project queue/persistence.
    controller.project_context = SimpleNamespace(editor=session, service=service)
    monkeypatch.setattr(service, "load", lambda *_args: pytest.fail("manual calculation loaded a film file"))
    yield controller, session, source, executor
    controller.close()
    controller.deleteLater()
    source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()


def test_manual_calculation_reads_latest_unsaved_same_owner_on_every_start(paired: Any) -> None:
    controller, session, _source, executor = paired
    latest = deepcopy(session.design)
    latest["front_layers"][0]["thickness"]["value"] = 123.0
    session.replace(latest)
    controller.startCalculation()
    assert len(executor.calls) == 1
    assert executor.calls[0][1]["front_layers"][0]["thickness"]["value"] == 123.0
    controller._finish_calculation((controller._active_calculation_token, None, "controlled stop"))
    latest["front_layers"][0]["thickness"]["value"] = 145.0
    session.replace(latest)
    controller.startCalculation()
    assert len(executor.calls) == 2
    assert executor.calls[1][1]["front_layers"][0]["thickness"]["value"] == 145.0


def test_closed_designer_presentation_keeps_project_working_owner(paired: Any) -> None:
    controller, session, _source, executor = paired
    controller.sourceWindowClosed()
    assert not controller._source_alive
    assert controller._source_controller_ref is None
    assert controller._source_session is session
    latest = deepcopy(session.design)
    latest["name"] = "Latest retained project draft"
    session.replace(latest)
    controller.startCalculation()
    assert len(executor.calls) == 1
    assert executor.calls[0][1]["name"] == "Latest retained project draft"
    assert controller.parent() is None  # No launcher or window-open path exists.


def test_failed_source_sync_cannot_fall_back_to_previous_valid_design(paired: Any) -> None:
    controller, session, _source, executor = paired
    invalid = deepcopy(session.design)
    invalid["front_layers"][0]["thickness"]["value"] = -1.0
    session.replace(invalid)
    controller.startCalculation()
    assert executor.calls == []
    assert not controller.busy
    assert "无法同步来源膜系" in str(controller.property("validationError"))
    controller._refresh_validation()
    assert "无法同步来源膜系" in str(controller.property("validationError"))
    valid = deepcopy(session.design)
    valid["front_layers"][0]["thickness"]["value"] = 127.0
    session.replace(valid)
    controller.startCalculation()
    assert len(executor.calls) == 1
    assert executor.calls[0][1]["front_layers"][0]["thickness"]["value"] == 127.0


@pytest.mark.parametrize("blocker", ["rejected_input", "expression_busy"])
def test_real_session_error_or_expression_work_blocks_old_valid_snapshot(
    tmp_path: Any, monkeypatch: Any, blocker: str,
) -> None:
    service, _library, design = fixture(tmp_path)
    session = FilmDesignEditSession(service)
    assert session.apply_optimized_design(design, session.snapshot().design_revision)
    source = _Source(cast(Any, session))
    controller = FilmSimulationQuickController(service=service, source_controller=source)
    executor = _HeldExecutor()
    monkeypatch.setattr(controller, "_executor", executor)
    monkeypatch.setattr(service, "load", lambda *_args: pytest.fail("manual calculation loaded a film file"))
    task = None
    try:
        stable_id = design["front_layers"][0]["id"]
        if blocker == "rejected_input":
            assert not session.set_layer_value("front", stable_id, "thickness.value", -1.0)
            assert session.snapshot().error
        else:
            task = session.request_refresh_expression_templates()
            assert task is not None and session.snapshot().expression_busy
        # The owner deliberately retains the previous legal design while the
        # new input is rejected or expression work is incomplete.
        assert session.snapshot().design["front_layers"][0]["thickness"]["value"] == 100.0
        controller.startCalculation()
        assert executor.calls == []
        assert "无法同步来源膜系" in str(controller.property("validationError"))
        if blocker == "rejected_input":
            assert session.set_layer_value("front", stable_id, "thickness.value", 127.0)
        else:
            assert task is not None
            assert session.complete_expression_task(task, result=[])
        assert not session.snapshot().error and not session.snapshot().expression_busy
        controller.startCalculation()
        assert len(executor.calls) == 1
        expected = 127.0 if blocker == "rejected_input" else 100.0
        assert executor.calls[0][1]["front_layers"][0]["thickness"]["value"] == expected
    finally:
        controller.close()
        controller.deleteLater()
        source.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_explicit_same_id_snapshot_detaches_and_never_writes_project_owner(paired: Any) -> None:
    controller, session, _source, executor = paired
    explicit = deepcopy(session.design)
    explicit["name"] = "Isolated same-ID preview"
    controller.setDesignJson(json.dumps(explicit))
    assert controller._source_session is None
    writes = len(session.writes)
    controller.setParameter("angle_deg", 12.0)
    assert len(session.writes) == writes
    latest = deepcopy(session.design)
    latest["name"] = "Project changed independently"
    session.replace(latest)
    controller.startCalculation()
    assert executor.calls[0][1]["name"] == "Isolated same-ID preview"
    assert session.design["name"] == "Project changed independently"


def test_explicit_constructor_input_does_not_bind_project_working_owner(
    paired: Any, monkeypatch: Any,
) -> None:
    from coat_sim.core.project_compute import ProjectComputeWorkspace

    controller, session, _source, _executor = paired
    compute = SimpleNamespace(register_thaw_callback=lambda _callback: None)
    monkeypatch.setattr(ProjectComputeWorkspace, "for_project", lambda *_args: compute)
    project = SimpleNamespace(editor=session, service=controller.service,
                              host=object(), binding=object())
    explicit = deepcopy(session.design)
    explicit["name"] = "Explicit project-scoped preview"
    preview = FilmSimulationQuickController(
        service=controller.service, design=explicit, project_context=project)
    try:
        assert preview.designName == explicit["name"]
        assert preview._source_session is None
        assert not preview.refreshFromSource()
        writes = len(session.writes)
        preview.setParameter("angle_deg", 12.0)
        assert len(session.writes) == writes
    finally:
        preview.close()
        preview.deleteLater()


def test_independent_projects_with_matching_ids_do_not_cross(paired: Any) -> None:
    controller, session, _source, executor = paired
    foreign = _Session(session.design)
    foreign.design["name"] = "Foreign matching-ID draft"
    assert foreign.design["id"] == session.design["id"]
    foreign_source = _Source(foreign)
    try:
        with pytest.raises(ValueError, match="project editor"):
            FilmSimulationQuickController(service=controller.service,
                                          source_controller=foreign_source,
                                          project_context=controller.project_context)
    finally:
        foreign_source.deleteLater()
    local = deepcopy(session.design)
    local["name"] = "Exact local owner"
    session.replace(local)
    controller.startCalculation()
    assert executor.calls[0][1]["name"] == "Exact local owner"
    assert foreign.design["name"] == "Foreign matching-ID draft"
