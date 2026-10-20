"""Owner regressions for editable angle drafts and stable synthesis materials."""
from copy import deepcopy
import json
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlExpression, qmlContext
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_needle import _optical
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tests.test_film_synthesis_gui_review import project_gui as project_gui

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def _edit(window, field, text, *, commit=True):
    field.forceActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for char in text:
        QTest.keyClick(window, Qt.Key(ord(char.upper())))
        QTest.qWait(5)
    if commit:
        QTest.keyClick(window, Qt.Key.Key_Return)
    QTest.qWait(15)


def test_angle_drafts_survive_invalid_intermediate_group_and_equal_nonzero(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        with window_runtime(app, controller) as (window, warnings):
            minimum = window.findChild(QObject, "filmOptimizationAngleMinField")
            maximum = window.findChild(QObject, "filmOptimizationAngleMaxField")
            assert minimum is not None and maximum is not None
            _edit(window, minimum, "12.5")
            assert minimum.property("text") == "12.5"
            assert controller.error and not controller.canStart
            _edit(window, maximum, "12.5")
            assert cast(dict[str, Any], controller.property("selectedObjective"))["angle_min_deg"] == 12.5
            assert cast(dict[str, Any], controller.property("selectedObjective"))["angle_max_deg"] == 12.5
            assert controller._build_request().objectives[0].angles_deg == (12.5,)
            _edit(window, minimum, "15.5")
            _edit(window, maximum, "15.5", commit=False)
            saved_nonzero = controller.exportOptimizationConfiguration()
            path = tmp_path / "nonzero-angle-config.json"
            path.write_text(json.dumps(saved_nonzero), encoding="utf-8")
            assert controller.loadOptimizationConfiguration(json.loads(path.read_text(encoding="utf-8")))
            assert controller._build_request().objectives[0].angles_deg == (15.5,)
            _edit(window, minimum, "1e")
            assert minimum.property("text") == "1e" and controller.error
            with pytest.raises(ValueError):
                controller.exportOptimizationConfiguration()
            window.close()
            QTest.qWait(20)
            assert window.isVisible() and minimum.property("text") == "1e"
            _edit(window, minimum, "0")
            _edit(window, maximum, "0")
            assert controller._build_request().objectives[0].angles_deg == (0.,)
            _edit(window, minimum, "7.5")
            _edit(window, maximum, "7.5", commit=False)
            window.close()
            QTest.qWait(20)
            assert not window.isVisible()
            saved = controller.exportOptimizationConfiguration()
            assert saved["objectives"][0]["angle_min_deg"] == saved["objectives"][0]["angle_max_deg"] == 7.5
            assert not any("ReferenceError" in item or "TypeError" in item for item in warnings), warnings
        with optimizer(service, design, saved) as (restored, _source, _session):
            assert restored._build_request().objectives[0].angles_deg == (7.5,)


def test_material_row_ids_survive_middle_delete_set_change_reorder_and_restore(app, tmp_path):
    service, library, design = fixture(tmp_path)
    profile = library.read("acceptance-film")["record"]
    profile["optical_constants"].append(_optical("film-alternate", 1.4))
    library.write(profile, update=True)
    design["front_layers"] = []
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        ids = [controller.addSynthesisMaterialRow() for _ in range(3)]
        assert all(ids) and len(set(ids)) == 3
        for identity in ids:
            assert controller.setSynthesisRowMaterial(identity, "acceptance-film")
        assert controller.setSynthesisRowOpticalSet(ids[0], "film-set")
        assert controller.setSynthesisRowOpticalSet(ids[2], "film-alternate")
        assert controller.removeSynthesisMaterialRow(ids[1])
        assert [row["id"] for row in cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))] == [ids[0], ids[2]]
        assert not controller.setSynthesisRowOpticalSet(ids[1], "film-set")
        assert controller.moveSynthesisMaterialRow(ids[2], -1)
        rows = cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))
        assert [row["id"] for row in rows] == [ids[2], ids[0]]
        request = controller._build_synthesis_request()
        assert [(ref.material_id, ref.optical_set_id) for ref in request.candidate_materials] == [
            ("acceptance-film", "film-alternate"), ("acceptance-film", "film-set")]
        saved = controller.exportOptimizationConfiguration()
        path = tmp_path / "optimization-config.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["synthesis"]["material_rows"] == [
            {key: row[key] for key in ("id", "material_id", "optical_set_id")} for row in rows]
        assert controller.removeSynthesisMaterialRow(ids[0])
        assert controller.removeSynthesisMaterialRow(ids[2])
        assert controller.synthesisMaterialError and not controller.canStart
        with window_runtime(app, controller) as (_window, warnings):
            assert not any("ReferenceError" in item or "TypeError" in item for item in warnings), warnings
    with optimizer(service, deepcopy(design), saved) as (restored, _source, _session):
        assert [row["id"] for row in cast(list[dict[str, Any]], restored.property("synthesisMaterialRows"))] == [ids[2], ids[0]]
        assert [ref.optical_set_id for ref in restored._build_synthesis_request().candidate_materials] == ["film-alternate", "film-set"]


def test_long_material_names_keep_row_actions_inside_clip_and_nullable_budgets_blank(app, tmp_path):
    service, library, design = fixture(tmp_path)
    profile = library.read("acceptance-film")["record"]
    profile["name"] = ("A deliberately long optical coating material name " * 8)[:120]
    profile["optical_constants"][0]["name"] = ("A deliberately long optical data set name " * 8)[:120]
    library.write(profile, update=True)
    design["front_layers"] = []
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        ids = [controller.addSynthesisMaterialRow() for _ in range(2)]
        for identity in ids:
            assert controller.setSynthesisRowMaterial(identity, "acceptance-film")
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(1100)
            window.setHeight(760)
            QTest.qWait(30)

            def item(name):
                def visit(parent):
                    if parent.objectName() == name:
                        return parent
                    for child in parent.childItems():
                        match = visit(child)
                        if match is not None:
                            return match
                    return None

                found = window.findChild(QObject, name) or visit(window.contentItem())
                assert isinstance(found, QQuickItem), name
                return found

            def click(control):
                parent = control.parentItem()
                while parent is not None:
                    if parent.metaObject().indexOfProperty("contentY") >= 0:
                        content = parent.property("contentItem")
                        if isinstance(content, QQuickItem):
                            point = control.mapToItem(content, QPointF(0, 0))
                            maximum = max(0., float(parent.property("contentHeight")) - parent.height())
                            parent.setProperty("contentY", min(maximum, max(0., point.y() - 15.)))
                    parent = parent.parentItem()
                QTest.qWait(20)
                # Full horizontal hit region must survive every clipped ancestor.
                parent = control.parentItem()
                while parent is not None:
                    if parent.clip():
                        for x in (1., control.width() / 2., control.width() - 1.):
                            mapped = control.mapToItem(parent, QPointF(x, control.height() / 2.))
                            assert 0 <= mapped.x() <= parent.width(), (control.objectName(), mapped, parent.width())
                            assert 0 <= mapped.y() <= parent.height(), (control.objectName(), mapped, parent.height())
                    parent = parent.parentItem()
                position = control.mapToScene(QPointF(control.width() / 2., control.height() / 2.)).toPoint()
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
                QTest.qWait(20)

            click(item("filmOptimizationAlgorithmTab"))
            for key in ("max_cycles", "max_evaluations", "max_layers", "minimum_nonzero_thickness_nm",
                        "max_layer_thickness_nm", "max_total_thickness_nm"):
                assert item("filmSynthesis_" + key + "Field").property("text") == "", key
            click(item("filmSynthesisDown_" + ids[0]))
            assert [row["id"] for row in cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))] == ids[::-1]
            click(item("filmSynthesisUp_" + ids[0]))
            assert [row["id"] for row in cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))] == ids
            click(item("filmSynthesisRemove_" + ids[0]))
            assert [row["id"] for row in cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))] == [ids[1]]
            assert not any("ReferenceError" in text or "TypeError" in text for text in warnings), warnings


@pytest.mark.parametrize("action", ["select", "add", "duplicate", "remove"])
def test_preserved_points_draft_blocks_objective_transition_until_explicit_confirmation(app, tmp_path, action):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller.addObjective()
        controller.selectObjective(0)
        saved = controller.exportOptimizationConfiguration()
        saved["objectives"][0].update(angle_sampling_mode="points", angles_text="0, 15",
                                     angle_min_deg=0., angle_max_deg=15.)
        assert controller.loadOptimizationConfiguration(saved)
        original_id = cast(dict[str, Any], controller.property("selectedObjective"))["id"]
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(1100)
            window.setHeight(760)
            minimum = window.findChild(QObject, "filmOptimizationAngleMinField")
            assert minimum is not None
            _edit(window, minimum, "12")
            if action == "select":
                listing = window.findChild(QObject, "filmOptimizationObjectiveList")
                assert listing is not None
                context = qmlContext(listing)
                assert context is not None
                expression = QQmlExpression(context, listing, "itemAtIndex(1)")
                button = expression.evaluate()
                if isinstance(button, tuple):
                    button = button[0]
            else:
                name = {"add": "Add", "duplicate": "Duplicate", "remove": "Remove"}[action]
                button = window.findChild(QObject, "filmOptimization" + name + "ObjectiveButton")
            assert isinstance(button, QQuickItem)
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                button.mapToScene(QPointF(button.width() / 2., button.height() / 2.)).toPoint())
            QTest.qWait(30)
            assert cast(dict[str, Any], controller.property("selectedObjective"))["id"] == original_id
            assert len(cast(list[dict[str, Any]], controller.property("objectives"))) == 2
            assert minimum.property("text") == "12"
            assert "确认角度范围" in controller.error
            with pytest.raises(ValueError):
                controller.exportOptimizationConfiguration()
            assert controller.confirmObjectiveAngleRange("12", "15", "1")
            controller.selectObjective(1)
            assert controller.exportOptimizationConfiguration()["objectives"][0]["angle_min_deg"] == 12.
            assert controller._build_request(require_variables=False).objectives[0].angles_deg == (12., 13., 14., 15.)
            window.close()
            QTest.qWait(20)
            assert not window.isVisible()
            assert not any("ReferenceError" in item or "TypeError" in item for item in warnings), warnings


def test_export_rejects_registered_invalid_constraint_without_serializing_old_value(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode("synthesis")
        before = deepcopy(session.workflow_configuration("optimization"))
        assert not controller.setSynthesisOption("max_layers", "1e")
        assert controller._pending_edit_error
        with pytest.raises(ValueError):
            controller.exportOptimizationConfiguration()
        controller._persist_configuration()
        assert session.workflow_configuration("optimization") == before
        assert controller._configuration_load_error
        assert controller.setSynthesisOption("max_layers", "5")
        assert controller.exportOptimizationConfiguration()["synthesis"]["options"]["max_layers"] == 5


def test_unknown_k_assumption_configuration_roundtrip_copies_environment_and_seed(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    original = deepcopy(design)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        assert controller.property("synthesisAssumeUnknownKZero") is False
        assert controller.setSynthesisAssumeUnknownKZero(True)
        assert controller.setSynthesisUseCurrentSeed(True)
        saved = controller.exportOptimizationConfiguration()
        path = tmp_path / "unknown-k-assumption-config.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        assert saved["synthesis"]["assume_unknown_k_zero"] is True
        assert controller.setSynthesisAssumeUnknownKZero(False)
        assert controller.loadOptimizationConfiguration(json.loads(path.read_text(encoding="utf-8")))
        request = controller._build_synthesis_request()
        assert request.environment["assume_lossless"] is True
        assert request.environment["use_k"] is True
        assert request.seed_designs[0]["assume_lossless"] is True
        assert request.seed_designs[0]["use_k"] is True
        assert design == original and controller._design["assume_lossless"] == original["assume_lossless"]
        malformed = deepcopy(saved)
        malformed["synthesis"]["assume_unknown_k_zero"] = "yes"
        assert not controller.loadOptimizationConfiguration(malformed)
        assert controller.property("synthesisAssumeUnknownKZero") is True
        legacy = deepcopy(saved)
        del legacy["synthesis"]["assume_unknown_k_zero"]
        assert controller.loadOptimizationConfiguration(legacy)
        assert controller.property("synthesisAssumeUnknownKZero") is False
        assert controller._build_synthesis_request().environment["assume_lossless"] is False


def test_absorption_summary_distinguishes_pool_from_candidate_assumptions(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller._synthesis_payload = {"absorption_assumption": {
            "assume_unknown_k_zero": True,
            "affected_materials": [{"material_id": "pool-only", "optical_set_id": "n-only"}],
            "used_affected_materials": [],
        }}
        summary = str(controller.property("synthesisAbsorptionSummary"))
        assert "pool-only / n-only" in summary and "当前候选未使用" in summary
        controller._synthesis_payload["absorption_assumption"]["used_affected_materials"] = [
            {"material_id": "actual-substrate", "optical_set_id": "n-only"}]
        assert "当前候选实际采用此假设的数据集：actual-substrate / n-only" in str(
            controller.property("synthesisAbsorptionSummary"))


@pytest.mark.parametrize("lock", ["busy", "pending_angle"])
def test_unknown_k_checkbox_keeps_model_value_when_edits_are_locked(app, tmp_path, lock):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(1100)
            window.setHeight(760)
            QTest.qWait(30)

            def click(control):
                assert isinstance(control, QQuickItem)
                parent = control.parentItem()
                while parent is not None:
                    if parent.metaObject().indexOfProperty("contentY") >= 0:
                        content = parent.property("contentItem")
                        if isinstance(content, QQuickItem):
                            point = control.mapToItem(content, QPointF(0, 0))
                            maximum = max(0., float(parent.property("contentHeight")) - parent.height())
                            parent.setProperty("contentY", min(maximum, max(0., point.y() - 15.)))
                    parent = parent.parentItem()
                QTest.qWait(20)
                point = control.mapToScene(QPointF(control.width() / 2., control.height() / 2.)).toPoint()
                assert 0 <= point.x() < window.width() and 0 <= point.y() < window.height()
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(30)

            if lock == "pending_angle":
                minimum = window.findChild(QObject, "filmOptimizationAngleMinField")
                assert minimum is not None
                click(minimum)
                _edit(window, minimum, "1e")
                assert controller._pending_edit_error and controller.error
            click(window.findChild(QObject, "filmOptimizationAlgorithmTab"))
            checkbox = window.findChild(QObject, "filmSynthesisUnknownKZeroField")
            assert isinstance(checkbox, QQuickItem)
            try:
                if lock == "busy":
                    # Controlled controller busy state isolates the native input fence;
                    # independent policy tests exercise the actual scheduler lifecycle.
                    controller._busy = True
                    controller.changed.emit()
                    QTest.qWait(20)
                assert checkbox.property("checked") is False
                assert controller.property("synthesisAssumeUnknownKZero") is False
                click(checkbox)
                assert checkbox.property("checked") is controller.property("synthesisAssumeUnknownKZero") is False
                assert not checkbox.isEnabled()
                # Also exercise the rejected toggled handler itself, including
                # restoration of the binding for later controller/config changes.
                context = qmlContext(checkbox)
                assert context is not None
                rejected_toggle = QQmlExpression(context, checkbox, "toggle(); toggled()")
                rejected_toggle.evaluate()
                assert not rejected_toggle.hasError(), rejected_toggle.error().toString()
                assert checkbox.property("checked") is controller.property("synthesisAssumeUnknownKZero") is False
                if lock == "busy":
                    controller._busy = False
                    controller.changed.emit()
                else:
                    click(window.findChild(QObject, "filmOptimizationTargetsTab"))
                    minimum = window.findChild(QObject, "filmOptimizationAngleMinField")
                    assert minimum is not None
                    click(minimum)
                    _edit(window, minimum, "0")
                    assert not controller._pending_edit_error
                    click(window.findChild(QObject, "filmOptimizationAlgorithmTab"))
                QTest.qWait(20)
                assert checkbox.isEnabled()
                click(checkbox)
                assert checkbox.property("checked") is controller.property("synthesisAssumeUnknownKZero") is True
                assert controller.exportOptimizationConfiguration()["synthesis"]["assume_unknown_k_zero"] is True
                assert controller.setSynthesisAssumeUnknownKZero(False)
                QTest.qWait(20)
                assert checkbox.property("checked") is False
                assert controller.setSynthesisAssumeUnknownKZero(True)
                QTest.qWait(20)
                assert checkbox.property("checked") is True
                saved = controller.exportOptimizationConfiguration()
                saved["synthesis"]["assume_unknown_k_zero"] = False
                assert controller.loadOptimizationConfiguration(saved)
                QTest.qWait(20)
                assert checkbox.property("checked") is False
                assert not any("ReferenceError" in text or "TypeError" in text for text in warnings), warnings
            finally:
                controller._busy = False
                controller.changed.emit()


@pytest.mark.parametrize("progress_available", [True, False])
def test_legacy_checkpoint_null_work_is_viewable_and_inspected_without_fabricated_counts(app, project_gui, progress_available):
    from coat_sim.core.film_synthesis_jobs import validate_checkpoint
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from coat_sim.ui.film_synthesis_quick_adapter import synthesis_result_view
    from tests.test_film_synthesis_execution_review import _checkpoint, _request

    request = _request(ceiling=.001, maximum_nm=200.)
    checkpoint = _checkpoint(request, layer=True)
    if not progress_available:
        checkpoint.pop("progress")
    payload = validate_checkpoint(checkpoint, terminal_state="cancelled")
    original_payload = deepcopy(payload)
    assert payload["work_counters"]["work_counters_complete"] is False
    assert payload["result"]["validation_evaluations"] is None
    assert payload["result"]["audit_physical_evaluations"] is None
    if not progress_available:
        assert payload["result"]["cycles"] is payload["result"]["evaluations"] is None
    view = synthesis_result_view(payload)
    assert view.optimization_result is not None
    assert view.payload == original_payload == payload
    assert view.optimization_result.validation_evaluations == 0  # internal legacy-type placeholder only
    assert view.payload["result"]["validation_evaluations"] is None
    assert view.optimization_result.validation_targets_met is True
    harness, _host, workspace, _store, _library, _saved = project_gui
    _service, source, controllers, _scenes = harness
    original_source = source.session.snapshot()
    controller = FilmOptimizationQuickController(source)
    controllers.append(controller)
    for _ in range(1000):
        if not controller._initial_loading:
            break
        QTest.qWait(10)
    assert not controller._initial_loading
    origin = {"project_id": workspace.binding.project_id, "project_root": str(workspace.binding.root)}
    assert controller.inspectSynthesisQueueResult("legacy-audited-checkpoint", payload, origin=origin), controller.error
    assert controller._synthesis_payload == original_payload
    assert controller.hasCandidate and controller.canSave and not controller.canApply
    assert source.session.snapshot().design == original_source.design
    assert source.session.snapshot().design_revision == original_source.design_revision
    assert "失败评价 未知" in str(controller.property("resultRunMetadataSummary"))
    assert "审计候选评价 未知" in str(controller.property("resultRunMetadataSummary"))
    assert "审计物理调用 未知" in str(controller.property("synthesisAuditSummary"))
    assert "审计波长点 未知" in str(controller.property("synthesisAuditSummary"))
    assert "历史检查点缺少完整累计" in str(controller.property("resultSummary"))
    assert "本次恢复重放单列" in str(controller.property("resultRunMetadataSummary"))
    if not progress_available:
        assert "轮次 未知" in str(controller.property("resultSummary"))
        assert "评价 未知" in str(controller.property("progressStatistics"))
    with window_runtime(app, controller) as (window, warnings):
        window.setWidth(1100)
        window.setHeight(760)
        QTest.qWait(30)
        label = window.findChild(QObject, "filmSynthesisAuditSummary")
        assert isinstance(label, QQuickItem)
        assert "审计物理调用 未知" in str(label.property("text"))
        layers = window.findChild(QObject, "filmSynthesisLayers")
        assert layers is not None and layers.property("count") == 1
        context = qmlContext(layers)
        assert context is not None
        expression = QQmlExpression(context, layers, "itemAt(0)")
        layer_label = expression.evaluate()
        if isinstance(layer_label, tuple):
            layer_label = layer_label[0]
        assert isinstance(layer_label, QQuickItem)
        assert "low" in str(layer_label.property("text")) and "nm" in str(layer_label.property("text"))
        assert not any("ReferenceError" in text or "TypeError" in text for text in warnings), warnings
    assert controller._synthesis_payload == original_payload == payload
