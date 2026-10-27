"""Real-session persistence and live-repair bridge contracts."""
from copy import deepcopy
from dataclasses import replace
import json
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication

from coat_sim.core import film_automatic_repair_jobs as live_jobs
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.film_optimization import FilmThicknessVariable, optimize_film_design
from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture
from tests.test_film_automatic_repair_live_display import (
    app as app,
    live_gui as live_gui,
    start_live,
    thin_value,
)
from tests.test_film_manufacturing_quick_controller import _readable_offscreen_font
from tests.test_film_synthesis_gui_review import _Scene

pytestmark = pytest.mark.qt


def make_owner(tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = [dict(id="bridge-layer", material_id="acceptance-film", optical_set_id="film-set",
        thickness=dict(value=7.0, unit="nm"), note="synthetic owner bridge probe")]
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    assert owner.setObjectiveField("wavelength_min_nm", 550.0)
    assert owner.setObjectiveField("wavelength_max_nm", 550.0)
    return service, session, source, owner


def test_real_save_close_reopen_and_nonphysical_table_fence(app, tmp_path):
    service, session, source, owner = make_owner(tmp_path)
    panel = owner.manufacturingController
    reopened = None
    try:
        assert panel.prepare(), panel.error
        baseline_signature = deepcopy(panel._prepared_signature)
        epoch = source.pendingEditEpoch
        revision = session.snapshot().design_revision
        panel._context = dict(session=session, source=source, revision=revision, epoch=epoch,
            draft_epoch=panel._draft_epoch, project_compute=None, project_operation=None,
            project_root=str(service.root.resolve()))
        sensitivity = owner.sensitivityController
        assert sensitivity.prepare(), sensitivity.error
        primary_request = owner._build_request(require_variables=False, fixed_topology=False)
        marker = optimize_film_design(service, replace(primary_request,
            variables=(FilmThicknessVariable("front", "bridge-layer", 0., None),),
            max_iterations=1, max_evaluations=8))
        owner._result = marker  # A real unapplied primary proposal must survive table edits.
        edits = []
        sensitivity.tableConfigurationEdited.connect(lambda: edits.append(True))
        assert sensitivity.setTableDefault("uncertainty_unit", "absolute_nm")
        assert sensitivity.setTableDefault("uncertainty_kind", "stddev")
        assert sensitivity.setTableDefault("uncertainty_text", "0.25")
        count = len(edits)
        assert sensitivity.setTableDefault("uncertainty_text", "0.25")
        assert len(edits) == count == 3
        table = sensitivity.exportTableConfiguration()
        assert session.workflow_configuration("optimization")["sensitivity_table"] == table
        assert not panel._stale and panel._context_is_current()
        assert panel._signature(panel._build_base_request()) == baseline_signature
        assert source.pendingEditEpoch == epoch and session.snapshot().design_revision == revision
        assert owner._result is marker
        document = tmp_path / "bridge-reopen.cfilm"
        assert session.save_file(str(document)), session.snapshot().error
        saved = json.loads(document.read_text(encoding="utf-8"))
        assert saved["workflow_settings"]["optimization"]["sensitivity_table"] == table
        owner.detach_observers()
        new_session = FilmDesignEditSession(service)
        assert new_session.request_open_file(str(document)), new_session.snapshot().error
        new_source = FilmDesignQuickController(service=service, edit_session=new_session)
        reopened = FilmOptimizationQuickController(new_source)
        assert reopened._sensitivity_controller is None
        assert reopened.exportOptimizationConfiguration()["sensitivity_table"] == table
        restored = cast(FilmSensitivityQuickController, reopened.sensitivityController)
        assert restored.exportTableConfiguration() == table
        restored_edits = []
        restored.tableConfigurationEdited.connect(lambda: restored_edits.append(True))
        configuration = reopened.exportOptimizationConfiguration()
        configuration["sensitivity_table"]["defaults"]["uncertainty_text"] = "0.75"
        assert reopened.loadOptimizationConfiguration(configuration), reopened.error
        assert not restored_edits  # load is not a new user edit and must not persist recursively.
        restored_defaults = restored.exportTableConfiguration()["defaults"]
        assert isinstance(restored_defaults, dict)
        assert restored_defaults["uncertainty_text"] == "0.75"
        assert restored.setTableDefault("uncertainty_text", "0.5")
        assert len(restored_edits) == 1
        assert new_session.workflow_configuration("optimization")["sensitivity_table"]["defaults"]["uncertainty_text"] == "0.5"
        panel2 = cast(FilmManufacturingQuickController, reopened.manufacturingController)
        assert panel2.prepare()
        assert reopened.setObjectiveField("pointwise.upper_bound_percent", 0.2)
        assert panel2._stale  # Actual optical criteria changes still invalidate repair.
        assert app is not None
    finally:
        if reopened is not None:
            reopened.detach_observers()
        owner.detach_observers()
        QCoreApplication.processEvents()


def test_apply_migrates_only_latest_table_field_without_mutating_frozen_candidate(app, tmp_path, monkeypatch):
    _service, session, source, owner = make_owner(tmp_path)
    try:
        sensitivity = owner.sensitivityController
        assert sensitivity.setTableDefault("uncertainty_text", "0.4")
        latest = session.workflow_configuration("optimization")["sensitivity_table"]
        candidate = deepcopy(session.snapshot().design)
        candidate["workflow_settings"]["optimization"]["sensitivity_table"]["defaults"]["uncertainty_text"] = "old"
        candidate["workflow_settings"]["optimization"]["frozen_marker"] = "must remain frozen"
        original = deepcopy(candidate)
        applied = []
        monkeypatch.setattr(owner, "_manufacturing_can_apply", lambda *args, **kwargs: True)
        def capture_rejected_application(design, *_args):
            applied.append(deepcopy(design))
            return False
        monkeypatch.setattr(source, "applySynthesizedDesign", capture_rejected_application)
        assert not owner._apply_manufacturing_candidate({},
            dict(revision=session.snapshot().design_revision, epoch=source.pendingEditEpoch, editable_snapshot={}),
            dict(design=candidate))
        assert len(applied) == 1
        optimization = applied[0]["workflow_settings"]["optimization"]
        assert optimization["sensitivity_table"] == latest
        assert optimization["frozen_marker"] == "must remain frozen"
        assert candidate == original
        assert app is not None
    finally:
        owner.detach_observers()
        QCoreApplication.processEvents()


def test_running_repair_keeps_edited_table_and_uses_cheap_validated_readiness(live_gui, monkeypatch):
    h = live_gui
    sensitivity = h.owner.sensitivityController
    assert sensitivity.setTableDefault("uncertainty_text", "0.1")
    original_revision = h.session.snapshot().design_revision
    original_epoch = h.source.pendingEditEpoch
    changed: list[bool] = []

    def edit_then_cancel(frame):
        best = frame.get("champion") if frame.get("stage") == "repair_live" else None
        if best is None or changed:
            return
        assert thin_value(best["design"]) == 7.5
        assert sensitivity.setTableDefault("uncertainty_text", "0.4")
        assert h.session.snapshot().design_revision == original_revision
        assert h.source.pendingEditEpoch == original_epoch
        assert not h.panel._stale and h.panel._context_is_current()
        assert h.panel._signature(h.panel._build_base_request()) == h.panel._context["signature"]
        original = live_jobs.applicable_improvement
        parsed = []
        def counted(*args, **kwargs):
            parsed.append(True)
            return original(*args, **kwargs)
        with monkeypatch.context() as reads:
            reads.setattr(live_jobs, "applicable_improvement", counted)
            for _ in range(10):
                assert h.panel.canApply
                assert h.panel.selectedRouteWarning
            assert not parsed, "table edits or property reads reparsed already-validated optical evidence"
        changed.append(True)
        h.panel.cancel()

    h.client.after_frame = edit_then_cancel
    start_live(h)
    h.pool.run()
    assert changed and not h.panel.error, h.panel.error
    assert h.panel._applied
    assert thin_value(h.session.snapshot().design) == 7.5
    assert h.session.snapshot().design_revision == original_revision + 1
    saved = h.session.workflow_configuration("optimization")["sensitivity_table"]
    assert saved["defaults"]["uncertainty_text"] == "0.4"
    assert sensitivity.exportTableConfiguration()["defaults"]["uncertainty_text"] == "0.4"


@pytest.mark.parametrize("scale", [1.0, 1.4])
def test_busy_repair_footer_preserves_full_button_text_and_window_bounds(live_gui, scale):
    h = live_gui
    theme = ThemeService.instance()
    previous_mode = theme._mode
    previous_font_scale = theme._font_scale
    previous_density_scale = theme._density_scale
    scene = None
    try:
        with _readable_offscreen_font():
            theme.configure(mode="light", font_scale=scale, density_scale=1.0)
            scene = _Scene(h.owner, "FilmOptimizationWindow.qml")
            scene.click(scene.item("filmManufacturingOpenButton"))
            start_live(h)
            for name in ("filmManufacturingSave", "filmManufacturingRun",
                         "filmManufacturingCancel", "filmManufacturingClose"):
                button = scene.item(name)
                geometry = json.loads(scene.evaluate(button, """JSON.stringify({
                    width: width, implicitWidth: implicitWidth,
                    contentWidth: contentItem.width, textWidth: contentItem.contentWidth,
                    textTruncated: contentItem.truncated, right: x + width,
                    footerWidth: parent.width, windowRight: mapToItem(null, width, 0).x
                })"""))
                assert not geometry["textTruncated"], (name, scale, geometry)
                assert geometry["contentWidth"] + 0.5 >= geometry["textWidth"], (name, scale, geometry)
                assert geometry["width"] + 0.5 >= geometry["implicitWidth"], (name, scale, geometry)
                assert geometry["right"] <= geometry["footerWidth"] + 0.5, (name, scale, geometry)
                assert geometry["windowRight"] <= scene.window.width(), (name, scale, geometry)
            h.client.after_frame = lambda _frame: h.panel.cancel()
            h.pool.run()
            assert not h.panel.error, h.panel.error
    finally:
        if scene is not None:
            scene.close()
        theme.configure(mode=previous_mode, font_scale=previous_font_scale, density_scale=previous_density_scale)


def seed_table_configuration(owner, *, opened):
    configuration = owner.exportOptimizationConfiguration()
    configuration["sensitivity_table"] = dict(version=1, defaults=dict(
        bias_text="", bias_unit="", uncertainty_kind="stddev", uncertainty_text="0.25",
        uncertainty_unit="absolute_nm"), layers=[dict(layer_id="bridge-layer", enabled=True,
        override=False, values={})], correlation_text="", correlation_layer_ids=None,
        correlation_invalidated=False, uncorrelated=False, include_finite_bias=False)
    assert owner.loadOptimizationConfiguration(configuration), owner.error
    controller = None
    if opened:
        controller = cast(FilmSensitivityQuickController, owner.sensitivityController)
        assert controller.prepare(), controller.error
    owner._persist_configuration()
    return controller


def assert_rejected_configuration_is_atomic(owner, session, source, incoming, controller):
    before = deepcopy(owner.exportOptimizationConfiguration())
    cache = deepcopy(owner._sensitivity_table_configuration)
    persisted = session.workflow_configuration("optimization")
    revision, epoch = session.snapshot().design_revision, source.pendingEditEpoch
    result = owner._result
    table_state = None if controller is None else (
        deepcopy(controller.exportTableConfiguration()), deepcopy(controller._table_rows),
        deepcopy(controller._prepared_signature))
    assert not owner.loadOptimizationConfiguration(incoming)
    assert owner.exportOptimizationConfiguration() == before
    assert owner._sensitivity_table_configuration == cache
    assert session.workflow_configuration("optimization") == persisted
    assert session.snapshot().design_revision == revision and source.pendingEditEpoch == epoch
    assert owner._result is result
    assert owner.error
    if controller is None:
        assert owner._sensitivity_controller is None
    else:
        assert owner._sensitivity_controller is controller
        assert (controller.exportTableConfiguration(), controller._table_rows,
                controller._prepared_signature) == table_state


@pytest.mark.parametrize("opened", [False, True])
@pytest.mark.parametrize("whole_configuration", [False, True])
@pytest.mark.parametrize("corruption", ["version", "duplicate_layer_id"])
def test_nested_table_schema_rejection_preserves_open_or_unopened_owner_atomically(
    app, tmp_path, opened, whole_configuration, corruption,
):
    _service, session, source, owner = make_owner(tmp_path)
    try:
        controller = seed_table_configuration(owner, opened=opened)
        incoming = deepcopy(owner.exportOptimizationConfiguration())
        if corruption == "version":
            incoming["sensitivity_table"]["version"] = 2
        else:
            incoming["sensitivity_table"]["layers"] *= 2
        if whole_configuration:
            incoming["objectives"][0]["name"] = "must not replace the old objective"
            incoming["solver"]["max_iterations"] += 1
        assert_rejected_configuration_is_atomic(owner, session, source, incoming, controller)
    finally:
        owner.detach_observers()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("opened", [False, True])
@pytest.mark.parametrize("corruption", ["solver", "search_reference"])
def test_valid_nested_table_cannot_commit_before_all_outer_validation(
    app, tmp_path, monkeypatch, opened, corruption,
):
    _service, session, source, owner = make_owner(tmp_path)
    try:
        controller = seed_table_configuration(owner, opened=opened)
        incoming = deepcopy(owner.exportOptimizationConfiguration())
        incoming["sensitivity_table"]["defaults"]["uncertainty_text"] = "-"  # A legitimate draft.
        if corruption == "solver":
            incoming["solver"]["method"] = "invalid solver"
        else:
            incoming["synthesis"]["search_reference"] = []
        loads = []
        if controller is not None:
            original = controller.loadTableConfiguration
            def record_load(configuration):
                loads.append(True)
                return original(configuration)
            monkeypatch.setattr(controller, "loadTableConfiguration", record_load)
        assert_rejected_configuration_is_atomic(owner, session, source, incoming, controller)
        assert not loads
    finally:
        owner.detach_observers()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("state", ["_busy", "_closed"])
@pytest.mark.parametrize("whole_configuration", [False, True])
def test_existing_table_runtime_rejection_is_not_bypassed_or_reported_success(
    app, tmp_path, monkeypatch, state, whole_configuration,
):
    _service, session, source, owner = make_owner(tmp_path)
    controller = None
    try:
        controller = seed_table_configuration(owner, opened=True)
        assert controller is not None
        incoming = deepcopy(owner.exportOptimizationConfiguration())
        incoming["sensitivity_table"]["defaults"]["uncertainty_text"] = "0.75"
        if whole_configuration:
            incoming["objectives"][0]["name"] = "must not commit while the table is unavailable"
        setattr(controller, state, True)
        def no_replacement_controller(*_args, **_kwargs):
            raise AssertionError("A new controller must not bypass the existing runtime admission gate")
        monkeypatch.setattr("coat_sim.ui.film_optimization_quick_controller.FilmSensitivityQuickController",
                            no_replacement_controller)
        assert_rejected_configuration_is_atomic(owner, session, source, incoming, controller)
    finally:
        if controller is not None:
            setattr(controller, state, False)
        owner.detach_observers()
        QCoreApplication.processEvents()

