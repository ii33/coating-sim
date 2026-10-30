"""Independent real-Qt checks for the synthesis and physical-generator GUI.

Only private test windows, synthetic optical data and temporary scheduler roots
are used. Numerical kernels and scheduler admission are not mocked.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QSettings, Qt, QUrl, Slot
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QSignalSpy, QTest
from shiboken6 import isValid

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.materials import default_material_profile
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture as optical_fixture
from tests.test_film_optimization_needle import _optical

pytestmark = [pytest.mark.qt, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[1]


def _wait(predicate, *, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "Timed out waiting for the private test GUI/task"


def _variant(value):
    return value.toVariant() if hasattr(value, "toVariant") else value


class _VisualItemLookup(QObject):
    """Carry object-valued QML selectors through a typed slot.

    Named scene lookup deliberately does not use this bridge: it walks the
    native QQuickItem visual tree. This bridge remains for selectors such as
    Dialog.standardButton() and ListView.itemAtIndex() that return an item
    rather than identifying one by objectName.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.item: QQuickItem | None = None
        self.evidence = ""

    @Slot(QQuickItem, str)
    def capture(self, item, evidence):
        self.item = item
        self.evidence = evidence


def _qt_wrapper_state(value: object) -> tuple[bool, bool | None, str]:
    """Describe a wrapper using Python metadata only, never Qt-object repr."""
    value_type = type(value)
    type_name = f"{value_type.__module__}.{value_type.__qualname__}"
    is_quick_item = isinstance(value, QQuickItem)
    try:
        valid: bool | None = bool(isValid(value))
        validity = str(valid).lower()
    except Exception as exc:
        valid = None
        validity = f"raised:{type(exc).__name__}"
    details = (f"python_type={type_name}; is_QQuickItem={str(is_quick_item).lower()}; "
               f"shiboken_isValid={validity}; wrapper_id=0x{id(value):x}")
    return is_quick_item, valid, details


def _require_live_visual_item(value: object, *, target: str, path: str,
                               source: str) -> QQuickItem:
    is_quick_item, valid, details = _qt_wrapper_state(value)
    context = f"target={target!r}; visual_path={path}; source={source}; {details}"
    if not is_quick_item:
        raise AssertionError(
            f"Non-QQuickItem wrapper encountered during visual lookup; {context}"
        )
    if valid is not True:
        raise AssertionError(
            f"Invalid QQuickItem wrapper encountered during visual lookup; {context}"
        )
    return cast(QQuickItem, value)


def find_named_visual_item(root: QQuickWindow | QQuickItem, name: str) -> QQuickItem:
    """Return one live named item from the native Qt Quick visual tree.

    QML visual parents and QObject parents are different ownership graphs.
    Use QQuickItem.childItems() only, retain wrappers during traversal, prefer
    the unique visible match, and otherwise allow one unique hidden match.
    """
    if isinstance(root, QQuickWindow):
        root_path = "window.contentItem"
        scope = root.contentItem()
    else:
        root_path = "root"
        scope = root
    if not isinstance(scope, QQuickItem) or not isValid(scope):
        is_item, valid, details = _qt_wrapper_state(scope)
        raise AssertionError(
            "QQuickWindow has no live QQuickItem content root; "
            f"target={name!r}; visual_path={root_path}; "
            f"is_QQuickItem={str(is_item).lower()}; shiboken_isValid={valid}; {details}"
        )

    matches = []
    pending = [(scope, root_path)]
    # Keep strong wrapper references, not just ``id()`` values: Shiboken can
    # release a Python wrapper while its QML-owned C++ item remains live, and
    # CPython may then reuse that id for a different child.
    visited = {}
    while pending:
        candidate, path = pending.pop()
        item = _require_live_visual_item(
            candidate, target=name, path=path, source="pending traversal node"
        )
        identity = id(item)
        if identity in visited:
            continue
        visited[identity] = item
        object_name = item.objectName()
        node_path = f"{path}<objectName={object_name!r}>"
        if object_name == name:
            matches.append(item)
        children = item.childItems()
        for index, child in enumerate(children):
            child_path = f"{node_path}/child[{index}]"
            checked = _require_live_visual_item(
                child, target=name, path=child_path,
                source="QQuickItem.childItems() result",
            )
            pending.append((checked, child_path))

    visible = [item for item in matches if item.isVisible()]
    if len(visible) == 1:
        return visible[0]
    if len(visible) > 1:
        raise AssertionError(
            f"Ambiguous visible QQuickItem {name!r}; matches={len(matches)}"
        )
    if len(matches) == 1:
        # Preserve prior behavior for a uniquely named, intentionally hidden
        # item; callers can inspect its visibility explicitly.
        return matches[0]
    if len(matches) > 1:
        raise AssertionError(f"Ambiguous hidden QQuickItem {name!r}; matches={len(matches)}")
    raise AssertionError(f"Missing QQuickItem {name!r}; visible visual matches=0")


class _Scene:
    def __init__(self, controller, filename):
        self.engine = QQmlEngine()
        self._visual_lookup = _VisualItemLookup(self.engine)
        self.engine.rootContext().setContextProperty("_guiReviewVisualLookup", self._visual_lookup)
        ThemeService.instance().attach_engine(self.engine, "Theme")
        self.component = QQmlComponent(
            self.engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml" / filename)))
        window = self.component.createWithInitialProperties({"controller": controller})
        assert isinstance(window, QQuickWindow), [e.toString() for e in self.component.errors()]
        self.window: QQuickWindow = window
        self.window.setWidth(1100)
        self.window.setHeight(760)
        self.window.show()
        self.window.requestActivate()
        _wait(self.window.isActive)
        QTest.qWait(50)

    def item(self, name):
        QCoreApplication.processEvents()
        QTest.qWait(10)
        return find_named_visual_item(self.window, name)

    def visual_item(self, owner, expression, *, description=None):
        # Every object-valued expression uses the typed slot; expression return
        # values carry primitive diagnostics only.
        self._visual_lookup.item = None
        self._visual_lookup.evidence = ""
        query = """(function () {
            const found = (%s);
            const evidence = JSON.stringify({found: found !== null,
                objectName: found ? found.objectName : null,
                visible: found ? found.visible : null});
            _guiReviewVisualLookup.capture(found, evidence);
            return evidence;
        })()""" % expression
        expression_result = self.evaluate(owner, query)
        result = self._visual_lookup.item
        if not isinstance(result, QQuickItem) or not isValid(result):
            # Report primitive evidence only: repr of an unexpected Qt wrapper
            # can itself fail while pytest formats the original lookup failure.
            raise AssertionError(
                f"Missing QQuickItem {description or expression!r}; result_type={type(result).__name__}; "
                f"expression_result_type={type(expression_result).__name__}; "
                f"qml_evidence={self._visual_lookup.evidence}")
        return result

    def evaluate(self, owner, expression):
        value = QQmlExpression(self.engine.rootContext(), owner, expression)
        result = value.evaluate()
        assert not value.hasError(), value.error().toString()
        if isinstance(result, tuple):
            result = result[0]
        return _variant(result)

    def click(self, item):
        # Keep native ancestors and Flickable.contentItem inside QML. Python
        # receives only coordinates, never generic wrappers for those objects.
        self.evaluate(item, """(function () {
            let ancestor = parent;
            while (ancestor !== null) {
                if ("contentY" in ancestor) {
                    const content = ancestor.contentItem;
                    if (!content) throw new Error("Scrollable ancestor has no contentItem");
                    const point = mapToItem(content, 0, 0);
                    const maximum = Math.max(0, ancestor.contentHeight - ancestor.height);
                    ancestor.contentY = Math.min(maximum, Math.max(0, point.y - 15));
                }
                ancestor = ancestor.parent;
            }
            return true;
        })()""")
        QTest.qWait(20)
        assert item.isVisible() and item.isEnabled(), item.objectName()
        overflow_anchor = self.evaluate(item, """(function () {
            const sourceName = objectName;
            let ancestor = parent;
            while (ancestor !== null) {
                if (ancestor.overflowItems !== undefined
                        && ancestor.overflowItems.some(source => source.objectName === sourceName))
                    return String(ancestor.overflowAnchor.objectName);
                ancestor = ancestor.parent;
            }
            return "";
        })()""")
        if overflow_anchor:
            # Overflowed controls intentionally retain visible/enabled state
            # while their original geometry is clipped outside the toolbar.
            # Exercise the actual native More menu, including its project
            # resource carrier, rather than calling the source/controller.
            assert overflow_anchor in ("operationBarOverflowButton", "projectResourceMoreActionsButton")
            self.click(self.item(overflow_anchor))
            self.click(self.item("operationBarOverflow_" + item.objectName()))
            return
        coordinates = json.loads(self.evaluate(item, """(function () {
            const point = mapToItem(null, width / 2, height / 2);
            return JSON.stringify([point.x, point.y]);
        })()"""))
        point = QPointF(*coordinates).toPoint()
        assert 0 <= point.x() < self.window.width() and 0 <= point.y() < self.window.height()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(20)

    def edit(self, item, text):
        self.window.requestActivate()
        _wait(self.window.isActive)
        self.click(item)
        _wait(lambda: bool(item.property("activeFocus")))
        QTest.keyClick(self.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        if str(item.property("text")):
            _wait(lambda: item.property("selectedText") == item.property("text"))
            assert item.property("selectedText") == item.property("text")
        QCoreApplication.processEvents()
        assert item.property("activeFocus"), (item.objectName(), "focus lost after select-all")
        prefix = ""
        for character in str(text):
            assert item.property("activeFocus"), (item.objectName(), prefix, "focus lost before key")
            # Use QtTest's ASCII overload: the key-enum overload supplies
            # lowercase event text even when Shift is included on QWindow.
            modifiers = (Qt.KeyboardModifier.ShiftModifier if character.isupper()
                         else Qt.KeyboardModifier.NoModifier)
            QTest.keyClick(self.window, ord(character), modifiers)
            prefix += character
            QCoreApplication.processEvents()
            assert item.property("activeFocus"), (item.objectName(), prefix, "focus lost after key")
            assert item.property("text") == prefix, (item.objectName(), prefix, item.property("text"))
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        QTest.qWait(20)

    def accept_dialog(self, dialog, standard_button=1024):
        assert dialog is not None and dialog.property("visible")
        button = self.visual_item(dialog, f"standardButton({standard_button})")
        assert isinstance(button, QQuickItem)
        self.click(button)
        _wait(lambda: not dialog.property("visible"))

    def close(self):
        self._visual_lookup.item = None
        self.window.hide()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.engine.deleteLater()
        QCoreApplication.processEvents()


@pytest.fixture
def gui(tmp_path):
    _service, library, _design = optical_fixture(tmp_path)
    high = default_material_profile("Synthetic independent high", id="review-high")
    high.update(category="film", optical_constants=[_optical("review-high-set", 2.1)])
    library.write(high)
    unknown = default_material_profile("Synthetic unknown k", id="review-unknown-k")
    unknown_set = _optical("review-unknown-set", 2.0)
    unknown_set["k"] = None
    unknown.update(category="film", optical_constants=[unknown_set])
    library.write(unknown)
    service = FilmDesignService(tmp_path)
    settings = QSettings(str(tmp_path / "review.ini"), QSettings.Format.IniFormat)
    source = FilmDesignQuickController(service=service, geometry_settings=settings)
    assert source.initializationReady
    assert source.setSubstrateValue("material_id", "acceptance-glass")
    assert source.setSubstrateValue("optical_set_id", "glass-set")
    assert source.setSubstrateValue("sides", "single")
    assert source.currentDesign()["front_layers"] == []
    controllers: list[FilmOptimizationQuickController] = []
    scenes: list[_Scene] = []
    yield service, source, controllers, scenes
    for controller in controllers:
        controller.cancelOptimization()
        controller.shutdown(5000)
    source.markSourceClosed()
    for scene in reversed(scenes):
        scene.close()
    for controller in controllers:
        controller.deleteLater()
    source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    # This client can only address the scheduler inside this test's private root.
    client = SchedulerClient(tmp_path)
    if client.endpoint.exists():
        client.stop()


def _optimizer(gui):
    service, source, controllers, scenes = gui
    controller = FilmOptimizationQuickController(source)
    controllers.append(controller)
    _wait(lambda: not controller._initial_loading)
    scene = _Scene(controller, "FilmOptimizationWindow.qml")
    scenes.append(scene)
    scene.click(scene.item("filmOptimizationAlgorithmTab"))
    scene.click(scene.item("filmOptimizationSearchModeBox"))
    QTest.keyClick(scene.window, Qt.Key.Key_Home)
    target_index = next(i for i, row in enumerate(cast(list[dict[str, Any]], controller.property("searchModeOptions"))) if row["id"] == "synthesis")
    for _index in range(target_index):
        QTest.keyClick(scene.window, Qt.Key.Key_Down)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    _wait(lambda: controller.searchMode == "synthesis")
    for row in list(cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))):
        scene.click(scene.item("filmSynthesisRemove_" + row["id"]))
    scene.click(scene.item("filmSynthesisAddMaterialButton"))
    row_id = cast(list[dict[str, Any]], controller.property("synthesisMaterialRows"))[0]["id"]
    for prefix, selected_id in (("filmSynthesisMaterial_", "acceptance-film"),
                                ("filmSynthesisOpticalSet_", "film-set")):
        picker = scene.item(prefix + row_id)
        model = _variant(picker.property("model"))
        index = next(i for i, option in enumerate(model) if option["id"] == selected_id)
        scene.click(picker)
        QTest.keyClick(scene.window, Qt.Key.Key_Home)
        for _ in range(index):
            QTest.keyClick(scene.window, Qt.Key.Key_Down)
        QTest.keyClick(scene.window, Qt.Key.Key_Return)
        QTest.qWait(20)
    assert any(row["material_id"] == "acceptance-film" and row["selected"] for row in cast(list[dict[str, Any]], controller.property("structureMaterialOptions"))), (
        controller.structureMaterialOptions, getattr(service, "global_catalog_error", ""), service.material_options(purpose="film"))
    for name, value in (("max_cycles", "1"), ("max_evaluations", "12"),
                        ("max_layer_thickness_nm", "200")):
        scene.edit(scene.item("filmSynthesis_" + name + "Field"), value)
    assert cast(dict[str, Any], controller.property("synthesisOptions"))["max_cycles"] == 1
    assert cast(dict[str, Any], controller.property("synthesisOptions"))["max_evaluations"] == 12
    assert not controller.synthesisUseCurrentSeed
    scene.click(scene.item("filmOptimizationTargetsTab"))
    for name, value in (("WavelengthMin", "550"), ("WavelengthMax", "550"),
                        ("LowerBound", "0"), ("UpperBound", "0.1"), ("Tolerance", "0")):
        scene.edit(scene.item("filmOptimization" + name + "Field"), value)
    objective_form = cast(dict[str, Any], controller.property("selectedObjective"))
    assert objective_form["wavelength_min_nm"] == "550"
    assert objective_form["wavelength_max_nm"] == "550"
    assert objective_form["pointwise"]["upper_bound_percent"] == "0.1"
    objective_request = controller._build_synthesis_request().objectives[0]
    assert (objective_request.wavelength_min_nm, objective_request.wavelength_max_nm,
            objective_request.pointwise.upper_bound) == (550., 550., .001)
    assert controller.acceptableErrorPercent == 0.
    assert controller.canStart, (controller.error, controller.statusText, controller._source_stale, controller._active_configuration_error())
    _wait(lambda: cast(QObject, controller.property("computeTarget")).property("canSubmit"))
    return service, source, controller, scene


def _expected_with_synthesis_search_progress(design, controller):
    """Allow only the new validated progress reference; compare every other field."""
    from coat_sim.ui.film_synthesis_search_state import check_reference
    from coat_sim.core.film_synthesis_continuation import validate_search_state
    reference = controller._synthesis_search_reference
    record = controller._synthesis_search_record
    assert reference is not None and record is not None
    snapshot = controller.session.snapshot()
    assert snapshot is not None
    check_reference(controller.service, reference, snapshot)
    assert reference['job_id'] == controller.queueJobId == record['job_id']
    assert reference['spec_fingerprint'] == record['spec_fingerprint']
    validate_search_state(record['request'], dict(record['checkpoint']))
    assert snapshot.design['workflow_settings']['optimization']['synthesis']['search_reference'] == reference
    expected = deepcopy(dict(design))
    expected['workflow_settings']['optimization']['synthesis']['search_reference'] = deepcopy(reference)
    return expected


def test_synthesis_apply_keeps_source_expression_across_cancel_completion_orders(gui, monkeypatch):
    from coat_sim.core.film_optimization import FilmOptimizationResult
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
    from coat_sim.ui.film_synthesis_quick_adapter import SynthesisQuickResult

    service, source, controller, scene = _optimizer(gui)
    symbol_id = source._symbol_stable_ids["H"]
    assert source.setLayerValue("symbol", symbol_id, "note", "user-defined H placeholder")
    controller._load_source(preserve_variables=True)
    source_before = source.session.snapshot()
    source_expression = deepcopy(source_before.design["expression"])
    request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
    editable_snapshot = freeze_synthesis_editable_snapshot(
        service, request.environment, request.frozen_material_snapshot)
    controller._frozen_request = request
    controller._synthesis_editable_snapshot = editable_snapshot
    scene.close()
    gui[3].remove(scene)
    monkeypatch.setattr(controller, "_retain_synthesis_search", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "_retain_cancelled_restart", lambda: None)
    monkeypatch.setattr(controller, "_cancel_candidate_verified", lambda: True)
    monkeypatch.setattr(controller, "_cancel_material_references_unchanged", lambda *_args: True)
    monkeypatch.setattr(controller, "_capture_continuation_postapply", lambda: None)
    monkeypatch.setattr(source, "openSimulationAndCalculate", lambda: "film_simulation")

    def candidate(thickness):
        value = deepcopy(dict(source_before.design))
        value["expression"] = {"text": "", "symbols": {}, "in_sync": False}
        value["front_layers"] = [{
            "id": f"generated_{thickness}", "material_id": "acceptance-film",
            "optical_set_id": "film-set",
            "thickness": {"value": thickness, "unit": "nm"}, "note": "",
        }]
        return service.validate(value)

    def result(state, thickness, revision):
        design = candidate(thickness)
        return FilmOptimizationResult(
            state=state, design_revision=revision, best_design=design,
            best_thickness_nm={design["front_layers"][0]["id"]: float(thickness)},
            max_normalized_error=0.5, weighted_max_normalized_error=0.5,
            weighted_rms_error=0.5, targets_met=False, per_condition_errors=(),
            iterations=1, evaluations=1, message="deterministic event-order fixture",
        )

    def start_attempt(operation_id, job_id):
        current = source.session.snapshot()
        epoch = int(source.pendingEditEpoch)
        controller._frozen_request = request
        controller._synthesis_editable_snapshot = editable_snapshot
        controller._operation_serial = operation_id
        controller._attempt_operation_id = operation_id
        controller._attempt_job_id = job_id
        controller._attempt_session = controller.session
        controller._attempt_source_controller = controller._source_controller
        controller._attempt_source_revision = current.design_revision
        controller._attempt_edit_epoch = epoch
        controller._attempt_search_mode = "synthesis"
        controller._operation_edit_epoch = epoch
        controller._cancel_intent_consumed = False
        return current

    # Worker completion can already carry an optimizer candidate while the
    # later queue envelope says that a cancel marker won. This dispatch must
    # keep the source expression metadata when applying the completed result.
    current = start_attempt(101, "cancel-marker-after-worker-result")
    completed = result("completed", 75.0, current.design_revision)
    cancel_payload = {
        "result": {"state": "cancelled", "candidate_design": deepcopy(completed.best_design),
                   "targets_met": False},
        "review": {"cancel_snapshot": {
            "source": "worker_completed_reply", "replay_performed": False}},
    }
    controller._on_finished(101, SynthesisQuickResult(cancel_payload, completed), [], None)
    applied = source.session.snapshot()
    assert applied.design["expression"] == source_expression
    assert applied.design["expression"]["symbols"]["H"]["note"] == "user-defined H placeholder"
    assert applied.design["front_layers"][0]["thickness"]["value"] == 75.0
    assert applied.design_revision == current.design_revision + 1
    assert source.canUndoOptimizedDesign
    assert source.undoOptimizedDesign()
    assert source.session.snapshot().design == current.design

    # The marker-first path is also deterministic: an accepted cancel applies
    # only the verified cancel candidate, through the same atomic session API.
    current = start_attempt(102, "cancel-marker-before-worker-result")
    cancelled = result("cancelled", 125.0, current.design_revision)
    cancel_payload = {
        "result": {"state": "cancelled", "candidate_design": deepcopy(cancelled.best_design),
                   "targets_met": False},
        "review": {"cancel_snapshot": {
            "source": "worker_completed_reply", "replay_performed": False,
            "candidate_available": True}},
    }
    controller._cancel_intent_accepted = False
    controller._cancel_intent_requested = False
    controller._on_finished(102, SynthesisQuickResult(cancel_payload, cancelled), [], None)
    assert controller._result_application_eligible
    controller._cancel_intent_accepted = True
    controller._cancel_intent_requested = True
    controller._cancel_intent_operation_id = 102
    controller._cancel_intent_job_id = "cancel-marker-before-worker-result"
    assert controller._accept_cancel_intent(), (
        controller._error, controller._progress_text,
        controller._cancel_intent_consumed,
        controller._attempt_source_revision,
        controller.session.snapshot().design_revision,
        controller._attempt_edit_epoch,
        controller._source_edit_epoch(),
    )
    applied = source.session.snapshot()
    assert applied.design["expression"] == source_expression
    assert applied.design["expression"]["symbols"]["H"]["note"] == "user-defined H placeholder"
    assert applied.design["front_layers"][0]["thickness"]["value"] == 125.0
    assert applied.design_revision == current.design_revision + 1
    assert source.canUndoOptimizedDesign
    assert source.undoOptimizedDesign()
    assert source.session.snapshot().design == current.design


def test_scene_keyboard_edit_reactivates_its_window_and_keeps_every_digit(gui):
    _service, _source, controller, scene = _optimizer(gui)
    scene.click(scene.item("filmOptimizationAlgorithmTab"))
    other = QQuickWindow()
    try:
        other.show()
        other.requestActivate()
        _wait(other.isActive)
        assert not scene.window.isActive()
        scene.edit(scene.item("filmSynthesis_max_evaluationsField"), "123")
        assert scene.window.isActive()
        assert controller.synthesisOptions["max_evaluations"] == 123
    finally:
        other.hide()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _preview_sampled_candidate(scene, source, controller):
    before = source.session.snapshot()
    service = source.service
    provider = service.materials
    plotted = deepcopy(controller.plotData)
    preview = QSignalSpy(controller.synthesisPreviewRequested)
    button = scene.item("filmSynthesisPreviewButton")
    assert "采样" in str(button.property("text"))
    scene.click(button)
    assert preview.count() == 1
    assert scene.window.property("synthesisPreviewHighlighted") is True
    status = scene.item("filmSynthesisPlotPreviewStatus")
    assert status.isVisible() and str(status.property("text"))
    assert controller.plotData == plotted
    assert source.service is service and source.service.materials is provider
    assert source.session.snapshot().design == before.design
    assert source.session.snapshot().design_revision == before.design_revision


def test_visual_lookup_diagnostic_distinguishes_wrong_type_from_deleted_qquickitem():
    wrong_type = QObject()
    deleted_item = QQuickItem()
    deleted_item.deleteLater()
    QCoreApplication.sendPostedEvents(deleted_item, QEvent.Type.DeferredDelete)
    assert not isValid(deleted_item)

    with pytest.raises(AssertionError, match="Non-QQuickItem wrapper") as wrong:
        _require_live_visual_item(
            wrong_type, target="wanted", path="contentItem/child[2]", source="probe"
        )
    wrong_message = str(wrong.value)
    assert "target='wanted'" in wrong_message
    assert "visual_path=contentItem/child[2]" in wrong_message
    assert "python_type=PySide6.QtCore.QObject" in wrong_message
    assert "is_QQuickItem=false" in wrong_message
    assert "shiboken_isValid=true" in wrong_message
    assert f"wrapper_id=0x{id(wrong_type):x}" in wrong_message

    with pytest.raises(AssertionError, match="Invalid QQuickItem wrapper") as invalid:
        _require_live_visual_item(
            deleted_item, target="wanted", path="contentItem/child[7]", source="probe"
        )
    invalid_message = str(invalid.value)
    assert "target='wanted'" in invalid_message
    assert "visual_path=contentItem/child[7]" in invalid_message
    assert "python_type=PySide6.QtQuick.QQuickItem" in invalid_message
    assert "is_QQuickItem=true" in invalid_message
    assert "shiboken_isValid=false" in invalid_message
    assert f"wrapper_id=0x{id(deleted_item):x}" in invalid_message


def test_scene_lookup_preserves_visual_ownership_and_strict_item_checks(tmp_path, monkeypatch):
    harness = tmp_path / "Lookup.qml"
    harness.write_text(
        "import QtQuick\nimport QtQuick.Controls\n"
        "Window { property var controller; width: 200; height: 150; visible: true; "
        "Repeater { model: 2; delegate: Item { objectName: 'repeat_' + index } } "
        "Popup { objectName: 'popup'; parent: Overlay.overlay; width: 100; height: 70; "
        "visible: true; contentItem: Item { objectName: 'popup_content'; "
        "Item { objectName: 'popup_button' } } } "
        "Item { objectName: 'hidden_unique'; visible: false } }\n",
        encoding="utf8",
    )
    scene = _Scene(None, str(harness))

    branch = QQuickItem()
    target = QQuickItem()
    hidden = QQuickItem(scene.window.contentItem())
    shown = QQuickItem(scene.window.contentItem())
    nonvisual = QObject(scene.window)
    nonvisual.setObjectName("nonvisual")
    try:
        branch.setParentItem(scene.window.contentItem())
        target.setParentItem(branch)
        target.setObjectName('visual_"quoted"\\target')
        # Visual parenting does not require QObject parenting. Native traversal
        # must find this item without crossing the QObject tree or QML→Python.
        assert target.parent() is None
        assert scene.window.findChildren(QObject, target.objectName()) == []
        with monkeypatch.context() as patch:
            assert scene.item(target.objectName()) is target
            assert target.parent() is None and target.parentItem() is branch
            assert isValid(target)
            patch.setattr(
                scene,
                "evaluate",
                lambda *_args, **_kwargs: pytest.fail(
                    "named visual lookup must use QQuickItem.childItems()"
                ),
            )
            assert scene.item("repeat_0").objectName() == "repeat_0"
            assert scene.item("repeat_1").objectName() == "repeat_1"
            popup_button = scene.item("popup_button")
            assert popup_button.isVisible()
            assert popup_button.parentItem().objectName() == "popup_content"
            assert scene.item("hidden_unique").isVisible() is False
            # A prior success must never mask an absent or nonvisual QObject.
            with pytest.raises(AssertionError, match="absent_after_success"):
                scene.item("absent_after_success")
            for name in ("nonvisual", "popup"):
                with pytest.raises(AssertionError, match=name):
                    scene.item(name)
        # Expression-based selectors still return typed QQuickItems, even if
        # QQmlExpression's primitive result is an unexpected QMetaObject.
        original_evaluate = scene.evaluate
        expression = "(function visit(item) { if (item.objectName === %s) return item; " \
                     "for (let i = 0; i < item.children.length; ++i) { " \
                     "const found = visit(item.children[i]); if (found !== null) return found; } " \
                     "return null; })(contentItem)" % json.dumps(target.objectName())
        with monkeypatch.context() as patch:
            def evaluate_with_unexpected_return(owner, source):
                evidence = original_evaluate(owner, source)
                assert isinstance(evidence, str)
                return nonvisual.metaObject()

            patch.setattr(scene, "evaluate", evaluate_with_unexpected_return)
            assert scene.visual_item(scene.window, expression, description=target.objectName()) is target
        for item in (hidden, shown):
            item.setParent(scene.window.contentItem())
            item.setObjectName("duplicate")
        hidden.setVisible(False)
        shown.setVisible(True)
        assert scene.item("duplicate") is shown
        for name in ("absent", "nonvisual"):
            with pytest.raises(AssertionError, match=name):
                scene.item(name)
    finally:
        target.deleteLater()
        branch.deleteLater()
        scene.close()


def test_scene_click_scrolls_real_flickable_without_python_parent_wrappers(tmp_path, monkeypatch):
    harness = tmp_path / "Click.qml"
    harness.write_text(
        "import QtQuick\nimport QtQuick.Controls\n"
        "Window { property var controller; width: 240; height: 180; "
        "Flickable { objectName: 'scroll'; width: 220; height: 140; contentHeight: 600; "
        "Button { objectName: 'target'; x: 10; y: 450; width: 140; height: 40; "
        "property int clicks: 0; onClicked: clicks += 1 } } }\n",
        encoding="utf8",
    )
    scene = _Scene(None, str(harness))
    try:
        target = scene.item("target")
        scroll = scene.item("scroll")
        assert float(scroll.property("contentY")) == 0.
        # Reproduce the CI's unexpected parentItem conversion while retaining
        # the actual QML ancestry, scrolling and native mouse event delivery.
        with monkeypatch.context() as patch:
            patch.setattr(target, "parentItem", lambda: scroll.metaObject())
            scene.click(target)
        assert target.property("clicks") == 1
        assert float(scroll.property("contentY")) > 0.
        assert isValid(target) and isValid(scroll)
    finally:
        scene.close()


def test_qml_n0_grows_real_single_layer_and_freezes_current_targets_and_materials(gui):
    service, source, controller, scene = _optimizer(gui)
    before = source.session.snapshot()
    assert controller.variables == []
    assert controller.canStart
    scene.click(scene.item("filmOptimizationStartButton"))
    worker = controller._run_coordinator.worker
    attempt_id = controller._operation_serial
    assert worker is not None
    _wait(lambda: bool(controller.queueJobId))
    job_id = controller.queueJobId
    _wait(lambda: not controller.busy, seconds=30.)
    payload = controller._synthesis_payload
    assert payload is not None, controller.error
    assert payload["result"]["state"] == "completed", payload
    assert payload["best_candidate_audit"]["targets_met"] is True
    assert payload["best_candidate_audit"]["continuous_certificate"] is False
    _preview_sampled_candidate(scene, source, controller)
    canvas = scene.item("filmOptimizationComparisonCanvas")
    plotted = _variant(canvas.property("dataSnapshot"))
    assert plotted["wavelength_nm"] == [550.]
    assert plotted["candidate"][0] == pytest.approx(((1.5 - 1.23**2) / (1.5 + 1.23**2))**2, abs=1e-12)
    rendered_objective = scene.visual_item(scene.item("filmSynthesisObjectiveAudit"), "itemAt(0)")
    assert isinstance(rendered_objective, QQuickItem)
    assert "550" in str(rendered_objective.property("text")) and "R" in str(rendered_objective.property("text"))
    rows = controller.synthesisLayers
    assert len(rows) == 1 and rows[0]["material_id"] == "acceptance-film"
    assert rows[0]["thickness_nm"] == pytest.approx(550. / (4. * 1.23), rel=1e-7)
    rendered = scene.visual_item(scene.item("filmSynthesisLayers"), "itemAt(0)")
    assert isinstance(rendered, QQuickItem)
    displayed = str(rendered.property("text"))
    assert ("acceptance-film" in displayed or rows[0]["material_name"] in displayed) and "nm" in displayed, displayed
    applied = source.session.snapshot()
    assert applied.design['front_layers'] == payload['result']['candidate_design']['front_layers']
    assert applied.design_revision == before.design_revision + 1
    assert controller._candidate_applied and source.canUndoOptimizedDesign
    spec_path = service.root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
    frozen = json.loads(spec_path.read_text(encoding="utf-8"))["parameters"]["payload"]["request"]["request"]
    assert frozen["environment"]["front_layers"] == [] and frozen["seed_designs"] == []
    objective = frozen["objectives"][0]
    assert (objective["wavelength_min_nm"], objective["wavelength_max_nm"], objective["pointwise"]["upper_bound"]) == (550., 550., .001)
    assert frozen["candidate_materials"] == [{"material_id": "acceptance-film", "optical_set_id": "film-set"}]
    film = next(row for row in frozen["frozen_material_snapshot"]["materials"] if row["id"] == "acceptance-film")
    assert film["optical_sets"][0]["n"] == [1.23, 1.23]
    assert controller.synthesisCandidateAudit == payload["best_candidate_audit"]
    summary = scene.item("filmSynthesisAuditSummary")
    if not summary.isVisible():
        scene.click(scene.item("filmOptimizationSidebarToggle"))
    scene.click(summary)
    assert str(summary.property("text")) == controller.synthesisAuditSummary
    retained = deepcopy(payload)
    worker.signals.progress.emit(attempt_id - 1, {"stage": "stale-review-event"})
    worker.signals.finished.emit(attempt_id - 1, None, [], {"state": "failed", "message": "stale-review-event"})
    worker.signals.finished.emit(attempt_id, None, [], {"state": "failed", "message": "repeated-review-event"})
    QCoreApplication.processEvents()
    assert controller._synthesis_payload == retained
    assert "review-event" not in controller.error
    confirmation = QSignalSpy(controller.applyConfirmationRequested)
    assert not scene.item("filmOptimizationApplyButton").isEnabled()
    assert confirmation.count() == 0 and not controller.canApply
    assert source.session.snapshot().design_revision == applied.design_revision
    assert source.currentDesign()["front_layers"] == payload["result"]["candidate_design"]["front_layers"]
    source_scene = _Scene(source, "FilmDesignQuick.qml")
    gui[3].append(source_scene)
    source_scene.click(source_scene.item("filmUndoOptimizationButton"))
    _wait(lambda: source.currentDesign()["front_layers"] == [])
    assert source.session.snapshot().design == _expected_with_synthesis_search_progress(before.design, controller)
    assert not source.canUndoOptimizedDesign


def test_qml_real_queue_heartbeat_then_cancel_retains_audited_candidate_facts(gui, monkeypatch):
    service, source, controller, scene = _optimizer(gui)
    # This case is about cancel ownership, not unknown-k policy; its materials
    # have known k, so keep the source's strict assumption unchanged.
    assert controller.setSynthesisAssumeUnknownKZero(False)
    # A synthesis candidate carries a blanked physics expression, but applying
    # it must not discard expression-symbol definitions owned by the source.
    symbol_id = source._symbol_stable_ids['H']
    assert source.setLayerValue('symbol', symbol_id, 'note', 'user-defined H placeholder')
    assert controller.setSynthesisOption("max_cycles", "")
    assert controller.setSynthesisOption("max_evaluations", "")
    assert controller.duplicateSelectedObjective()
    assert controller.setObjectiveField("pointwise.upper_bound_percent", 100.)
    assert controller.setObjectiveField("pointwise.lower_bound_percent", 90.)
    original = source.session.snapshot()
    refreshes = []

    def request_refresh():
        refreshes.append(source.session.snapshot())
        return "film_simulation"

    monkeypatch.setattr(source, "openSimulationAndCalculate", request_refresh)
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId))
    job_id = controller.queueJobId
    client = SchedulerClient(service.root)
    checkpoint = service.root / "artifacts/runtime/scheduler/jobs" / job_id / "best-candidate.json"
    observed: dict[str, Any] = {}

    def have_heartbeat_and_checkpoint():
        try:
            observed.update(client.status(job_id))
        except ValueError as exc:
            if "job_not_found" in str(exc):
                return False  # The displayed provisional ID can precede async admission.
            raise
        return (checkpoint.is_file()
                and observed.get("progress", {}).get("heartbeat_sequence", 0) >= 1
                and observed.get("progress", {}).get("score_scope") == "audited")

    _wait(have_heartbeat_and_checkpoint, seconds=20.)
    assert controller.busy and controller.canCancel
    scene.click(scene.item("filmOptimizationCancelButton"))
    _wait(lambda: not controller.busy, seconds=20.)
    payload = controller._synthesis_payload
    assert payload is not None, controller.error
    assert client.status(job_id)["state"] == "cancelled"
    assert payload["result"]["state"] == "cancelled" and not payload["result"]["targets_met"]
    assert payload["result"]["validation_complete"]
    assert payload["best_candidate_audit"]["available"] is True
    assert payload["best_candidate_audit"]["targets_met"] is False
    assert payload["best_candidate_audit"]["continuous_certificate"] is False
    assert controller.synthesisCandidateAudit == payload["best_candidate_audit"]
    assert len(controller.synthesisLayers) == len(payload["result"]["candidate_design"]["front_layers"])
    assert str(scene.item("filmSynthesisAuditSummary").property("text")) == controller.synthesisAuditSummary
    assert not controller.canApply
    candidate_design = payload["result"]["candidate_design"]
    normalized_original = service.validate(deepcopy(dict(original.design)))
    normalized_candidate = service.validate(deepcopy(dict(candidate_design)))
    after = source.session.snapshot()
    expected_candidate = deepcopy(dict(normalized_candidate))
    expected_candidate["expression"] = deepcopy(original.design["expression"])
    if "workflow_settings" in original.design:
        expected_candidate["workflow_settings"] = deepcopy(original.design["workflow_settings"])
    candidate_changed = expected_candidate != normalized_original
    assert after.design["expression"] == original.design["expression"]
    assert after.design["expression"]["symbols"]["H"]["note"] == "user-defined H placeholder"
    # The numerical candidate blanks expression state before synthesis. When
    # source-owned expression semantics are restored, identical physical
    # designs must not create a fake optimization edit or undo entry.
    if expected_candidate == normalized_original:
        assert after.design == _expected_with_synthesis_search_progress(original.design, controller)
        assert after.design_revision == original.design_revision
        assert after.dirty == original.dirty
        assert not source.canUndoOptimizedDesign
    else:
        assert after.design == _expected_with_synthesis_search_progress(expected_candidate, controller)
        assert after.design_revision == original.design_revision + 1
        assert after.dirty and source.canUndoOptimizedDesign
        source_scene = _Scene(source, "FilmDesignQuick.qml")
        gui[3].append(source_scene)
        expected_undo = _expected_with_synthesis_search_progress(original.design, controller)
        source_scene.click(source_scene.item("filmUndoOptimizationButton"))
        _wait(lambda: source.session.snapshot().design == expected_undo)
        restored = source.session.snapshot()
        assert restored.design_revision == after.design_revision + 1
        assert not source.canUndoOptimizedDesign
    assert len(refreshes) == (1 if candidate_changed else 0)
    if candidate_changed:
        assert refreshes[0].design == after.design
    final_revision = source.session.snapshot().design_revision
    controller.cancelOptimization()
    assert source.session.snapshot().design_revision == final_revision
    assert len(refreshes) == (1 if candidate_changed else 0)


@pytest.mark.parametrize("change", ["revision", "edit_epoch", "source_closed"])
def test_real_synthesis_candidate_cannot_overwrite_a_changed_source(gui, change):
    _service, source, controller, scene = _optimizer(gui)
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId))
    _wait(lambda: not controller.busy, seconds=30.)
    assert controller._synthesis_payload["best_candidate_audit"]["targets_met"] is True
    completed_layers = deepcopy(source.currentDesign()['front_layers'])
    assert len(completed_layers) == 1 and controller._candidate_applied
    if change == "revision":
        assert source.setDesignValue("note", "A real edit after the frozen synthesis")
    elif change == "edit_epoch":
        source.markEditingDirty()
    else:
        source.markSourceClosed()
    current = source.session.snapshot()
    assert not controller.confirmApplyCandidate()
    assert source.session.snapshot().design == current.design
    assert source.session.snapshot().design_revision == current.design_revision
    assert source.currentDesign()["front_layers"] == completed_layers


def _generator(gui):
    _service, source, _controllers, scenes = gui
    scene = _Scene(source, "FilmDesignQuick.qml")
    scenes.append(scene)
    scene.click(scene.item("filmDesignSidebarGenerator"))
    scene.click(scene.item("filmDesignGeneratorMode"))
    QTest.keyClick(scene.window, Qt.Key.Key_End)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    panel = scene.item("filmDesignGeneratorPanel")
    _wait(lambda: panel.property("generatorMode") == "physical")
    form = scene.item("filmPhysicalGeneratorForm")
    scene.click(scene.item("filmPhysicalMechanism"))
    QTest.keyClick(scene.window, Qt.Key.Key_Home)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    _wait(lambda: form.property("mechanism") == "multistack")
    scene.evaluate(form, "setBinding('H', 'review-high', 'review-high-set')")
    scene.evaluate(form, "setBinding('L', 'acceptance-film', 'film-set')")
    scene.evaluate(form, "setBinding('C', 'review-high', 'review-high-set')")
    scene.edit(scene.item("filmPhysicalSection_0_pairs"), "2")
    scene.edit(scene.item("filmPhysicalSection_0_wavelength_nm"), "550")
    return source, scene, form


def test_physical_generator_qml_preview_apply_confirmation_and_undo(gui):
    source, scene, form = _generator(gui)
    original = source.session.snapshot()
    scene.click(scene.item("filmPhysicalPreview"))
    preview = _variant(form.property("previewData"))
    assert preview.get("ok"), form.property("errorText")
    assert preview["mechanism"] == "multistack" and preview["layer_count"] == 4
    assert "candidate_design" not in preview
    prescription = preview["layers_substrate_order"]
    assert len(prescription) == 4
    for row in prescription:
        index = 2.1 if row["material_id"] == "review-high" else 1.23
        assert row["physical_thickness_nm"] == pytest.approx(550. / (4. * index))
    assert source.session.snapshot().design == original.design
    token = str(form.property("previewToken"))
    assert token
    scene.click(scene.item("filmPhysicalApply"))
    confirmation = scene.window.findChild(QObject, "filmPhysicalApplyConfirmation")
    assert confirmation is not None and confirmation.property("visible")
    assert source.session.snapshot().design == original.design
    scene.accept_dialog(confirmation)
    _wait(lambda: source.session.snapshot().design_revision == original.design_revision + 1)
    applied = source.session.snapshot()
    assert applied.dirty and applied.path == original.path
    assert len(applied.design["front_layers"]) == 4
    for layer, row in zip(applied.design["front_layers"], prescription, strict=True):
        assert layer["material_id"] == row["material_id"]
        assert layer["optical_set_id"] == row["optical_set_id"]
        assert layer["thickness"]["unit"] == "nm"
        assert layer["thickness"]["value"] == pytest.approx(row["physical_thickness_nm"])
    assert not source.applyGeneratedDesign(token)
    undo = scene.item("filmDesignGeneratorUndoButton")
    assert source.canUndoGeneratedDesign
    scene.click(undo)
    _wait(lambda: source.currentDesign()["front_layers"] == [])
    restored = source.session.snapshot()
    assert restored.design == original.design and restored.dirty == original.dirty
    assert restored.path == original.path and not source.canUndoGeneratedDesign


@pytest.mark.parametrize("fault", ["out_of_domain", "unknown_k"])
def test_physical_generator_qml_rejects_material_errors_and_invalidates_old_preview(gui, fault):
    source, scene, form = _generator(gui)
    original = source.session.snapshot()
    scene.click(scene.item("filmPhysicalPreview"))
    old_token = str(form.property("previewToken"))
    assert old_token
    if fault == "out_of_domain":
        scene.edit(scene.item("filmPhysicalSection_0_wavelength_nm"), "1000")
    else:
        scene.evaluate(form, "setBinding('H', 'review-unknown-k', 'review-unknown-set')")
    assert not form.property("previewToken")
    assert not source.applyGeneratedDesign(old_token)
    scene.click(scene.item("filmPhysicalPreview"))
    assert not form.property("previewToken")
    assert str(form.property("errorText"))
    assert str(scene.item("filmPhysicalError").property("text")) == str(form.property("errorText"))
    assert source.session.snapshot().design == original.design
    assert source.session.snapshot().design_revision == original.design_revision


@pytest.fixture
def project_gui(tmp_path, monkeypatch):
    from coat_sim.core import material_business

    _service, library, _design = optical_fixture(tmp_path / "synthetic-global")
    inactive = default_material_profile("Inactive exit source", id="review-inactive-exit")
    inactive_set = _optical("inactive-set", 2.8)
    inactive_set["k"] = None
    inactive.update(category="substrate", optical_constants=[inactive_set])
    library.write(inactive)
    later = default_material_profile("Later workflow material", id="review-later-material")
    later.update(category="film", optical_constants=[_optical("later-set", 1.8)])
    library.write(later)
    monkeypatch.setattr(material_business, "application_material_library", lambda: library)
    store = ProjectStore.create(tmp_path / "project", "Independent GUI project")
    host = ProjectSessionHost(lambda root: ProjectStore.open(root / MANIFEST_FILENAME))
    workspace = ProjectFilmWorkspace.for_project(host, host.open_project(store.root).binding)
    service = workspace.service
    store = service.project_store
    design = service.new_design()
    design["substrate"].update(material_id="acceptance-glass", optical_set_id="glass-set", sides="single")
    saved = service.create_film(design, materials={"acceptance-glass": service.materials.get("acceptance-glass")})
    assert workspace.editor.request_load_saved_design(saved.document_id)
    settings = QSettings(str(tmp_path / "project-review.ini"), QSettings.Format.IniFormat)
    source = FilmDesignQuickController(service=service, edit_session=workspace.editor,
        project_context=workspace, geometry_settings=settings)
    controllers: list[FilmOptimizationQuickController] = []
    scenes: list[_Scene] = []
    yield (service, source, controllers, scenes), host, workspace, store, library, saved
    for controller in controllers:
        controller.cancelOptimization()
        controller.shutdown(5000)
    source.markSourceClosed()
    for scene in reversed(scenes):
        scene.close()
    for controller in controllers:
        controller.deleteLater()
    source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    client = SchedulerClient(store.root)
    if client.endpoint.exists():
        client.stop()
    try:
        host.store_for(workspace.binding)
    except ValueError:
        pass
    else:
        assert host.close_project(workspace.binding, prepare_close=lambda *_args: True)


@pytest.mark.parametrize("drift", [False, True])
def test_project_qml_synthesis_new_material_closure_survives_save_or_drift_undo(project_gui, drift, monkeypatch):
    harness, _host, workspace, store, library, saved = project_gui
    service, source, controller, scene = _optimizer(harness)
    # This test isolates the explicit apply/material-closure transaction. The
    # default automatic completion route has independent end-to-end coverage.
    monkeypatch.setattr(controller, '_apply_completed_result', lambda *_args: False)
    original = source.session.snapshot()
    assert original.design["front_layers"] == []
    initial_materials = {row["id"] for row in store.resolve_document(saved.document_id)["materials"]}
    assert "acceptance-film" not in initial_materials
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId))
    _wait(lambda: not controller.busy, seconds=30.)
    assert controller.synthesisCandidateAudit["targets_met"] is True
    assert controller.synthesisLayers[0]["material_id"] == "acceptance-film"
    assert source.session.snapshot().design == _expected_with_synthesis_search_progress(original.design, controller)
    if drift:
        changed = default_material_profile("Same ID, changed global optics", id="acceptance-film")
        changed.update(category="film", optical_constants=[_optical("film-set", 2.3)])
        library.write(changed, update=True)
        service.refresh_materials()
        assert service.catalog_material("acceptance-film")["optical_sets"][0]["n"] == [2.3, 2.3]
    _preview_sampled_candidate(scene, source, controller)
    applied_signal = QSignalSpy(source.optimizationApplied)
    scene.click(scene.item("filmOptimizationApplyButton"))
    dialog = scene.window.findChild(QObject, "filmOptimizationApplyConfirmation")
    scene.accept_dialog(dialog, 16384)
    accepted = applied_signal.count() == 1
    if not accepted:
        assert drift and (controller.error or source.session.snapshot().error)
        assert source.session.snapshot().design == _expected_with_synthesis_search_progress(original.design, controller)
        return
    applied = source.session.snapshot()
    assert len(applied.design["front_layers"]) == 1 and applied.path == original.path
    assert applied.design["id"] == original.design["id"]
    assert service.materials.get("acceptance-film")["optical_sets"][0]["n"] == [1.23, 1.23]
    assert applied.design["front_layers"][0]["thickness"]["value"] == pytest.approx(550. / (4. * 1.23), rel=1e-7)
    if drift:
        configuration = source.session.workflow_configuration("optimization")
        configuration["structure_materials"] = [{"material_id": "review-later-material", "optical_set_id": "later-set"}]
        assert source.session.set_workflow_configuration("optimization", configuration)
        assert source.canUndoOptimizedDesign and source.undoOptimizedDesign()
        restored = source.session.snapshot()
        expected = deepcopy(dict(original.design))
        expected["workflow_settings"]["optimization"] = configuration
        assert restored.design == expected and restored.path == original.path
        assert service.materials.get("review-later-material")["optical_sets"][0]["n"] == [1.8, 1.8]
        assert not source.canUndoOptimizedDesign
        assert source.saveCurrent(), source.session.snapshot().error
        persisted = ProjectStore.open(store.project_file).resolve_document(saved.document_id)
        later = next(row for row in persisted["materials"] if row["id"] == "review-later-material")
        assert later["optical_sets"][0]["n"] == [1.8, 1.8]
    else:
        assert source.saveCurrent(), source.session.snapshot().error
        persisted = ProjectStore.open(store.project_file).resolve_document(saved.document_id)
        retained = next(row for row in persisted["materials"] if row["id"] == "acceptance-film")
        assert retained["optical_sets"][0]["n"] == [1.23, 1.23]
        persisted_design = next(row["data"] for row in persisted["documents"] if row["id"] == saved.document_id)
        assert persisted_design["front_layers"] == applied.design["front_layers"]
        assert not source.session.snapshot().dirty
    assert workspace.binding.root == store.root


def test_project_close_reopen_rejects_old_synthesis_and_physical_preview(project_gui):
    harness, host, workspace, store, _library, saved = project_gui
    _service, source, controller, scene = _optimizer(harness)
    # The optical fixture has known k. Exercise project-generation fencing
    # without the new unknown-k=0 policy blocking the physical preview setup.
    assert controller.setSynthesisAssumeUnknownKZero(False)
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId))
    _wait(lambda: not controller.busy, seconds=30.)
    assert controller.synthesisCandidateAudit["targets_met"] is True
    before = source.session.snapshot()
    physical_inputs = {"mechanism": "composite", "name": "Project preview before close",
        "bindings": [{"role": "L", "material_id": "acceptance-film", "optical_set_id": "film-set"}],
        "sections": [{"type": "layer", "role": "L", "wavelength_nm": 550., "optical_thickness_waves": .25}]}
    physical_preview = source.previewPhysicalStructure(physical_inputs)
    assert physical_preview["ok"], physical_preview
    binding = workspace.binding
    assert host.close_project(binding, prepare_close=lambda *_args: True)
    reopened = host.open_project(store.root).binding
    assert reopened != binding
    assert not source.applyGeneratedDesign(physical_preview["preview_token"])
    assert not controller.previewSynthesisCandidate()
    assert not controller.confirmApplyCandidate()
    assert source.session.snapshot().design == before.design
    assert source.session.snapshot().design_revision == before.design_revision
    preview = source.previewPhysicalStructure({"mechanism": "composite", "name": "Stale source",
        "bindings": [{"role": "L", "material_id": "acceptance-film", "optical_set_id": "film-set"}],
        "sections": [{"type": "layer", "role": "L", "wavelength_nm": 550., "optical_thickness_waves": .25}]})
    assert preview["ok"] is False and preview["error"]
    persisted = ProjectStore.open(store.project_file).resolve_document(saved.document_id)
    persisted_design = next(row["data"] for row in persisted["documents"] if row["id"] == saved.document_id)
    assert persisted_design["front_layers"] == []
    assert host.close_project(reopened, prepare_close=lambda *_args: True)


def test_physical_generator_same_provider_optical_drift_rejects_application(gui):
    service, _source, _controllers, _scenes = gui
    source, scene, form = _generator(gui)
    original = source.session.snapshot()
    scene.click(scene.item("filmPhysicalPreview"))
    assert form.property("previewToken")
    provider = service.materials
    changed = default_material_profile("Same provider, changed n", id="review-high")
    changed.update(category="film", optical_constants=[_optical("review-high-set", 3.1)])
    MaterialLibrary(service.root).write(changed, update=True)
    assert service.materials is provider
    scene.click(scene.item("filmPhysicalApply"))
    confirmation = scene.window.findChild(QObject, "filmPhysicalApplyConfirmation")
    assert confirmation is not None and confirmation.property("visible")
    scene.accept_dialog(confirmation)
    assert not form.property("previewToken") and str(form.property("errorText"))
    assert source.session.snapshot().design == original.design
    assert source.session.snapshot().design_revision == original.design_revision


def test_physical_generator_coupled_cavity_fields_build_actual_independent_sections(gui):
    source, scene, form = _generator(gui)
    scene.click(scene.item("filmPhysicalMechanism"))
    QTest.keyClick(scene.window, Qt.Key.Key_Home)
    QTest.keyClick(scene.window, Qt.Key.Key_Down)
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    _wait(lambda: form.property("mechanism") == "coupled_cavity")
    scene.edit(scene.item("filmPhysicalSection_0_substrate_mirror_pairs"), "1")
    scene.edit(scene.item("filmPhysicalSection_0_incident_mirror_pairs"), "2")
    scene.click(scene.item("filmPhysicalSection_0_addCavity"))
    scene.edit(scene.item("filmPhysicalSection_0_coupling_0_pairs"), "1")
    scene.edit(scene.item("filmPhysicalSection_0_cavity_1_optical_thickness_waves"), "0.4")
    section = _variant(form.property("sections"))[0]
    assert float(section["substrate_mirror"]["pairs"]) == 1
    assert float(section["incident_mirror"]["pairs"]) == 2
    assert float(section["coupling_mirrors"][0]["pairs"]) == 1
    assert float(section["cavities"][0]["optical_thickness_waves"]) == .5
    assert float(section["cavities"][1]["optical_thickness_waves"]) == .4
    original = source.session.snapshot()
    scene.click(scene.item("filmPhysicalPreview"))
    preview = _variant(form.property("previewData"))
    assert preview.get("ok"), form.property("errorText")
    assert preview["layer_count"] == 10
    assert all(row["physical_thickness_nm"] > 0 for row in preview["layers_substrate_order"])
    assert source.session.snapshot().design == original.design


def test_project_synthesis_apply_retains_inactive_source_absent_from_active_task_snapshot(project_gui, monkeypatch):
    harness, _host, _workspace, store, _library, saved = project_gui
    _service, source, _controllers, _scenes = harness
    assert source.setMediumValue("exit_medium", "kind", "material")
    assert source.setMediumValue("exit_medium", "material_id", "review-inactive-exit")
    assert source.setMediumValue("exit_medium", "optical_set_id", "inactive-set")
    assert source.saveCurrent(), source.session.snapshot().error
    service, source, controller, scene = _optimizer(harness)
    # Exercise explicit apply of the inactive editable closure, not scheduling
    # of automatic completion, which is covered by the default UI workflow.
    monkeypatch.setattr(controller, '_apply_completed_result', lambda *_args: False)
    original = source.session.snapshot()
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId))
    _wait(lambda: not controller.busy, seconds=30.)
    assert controller.synthesisCandidateAudit["targets_met"] is True
    frozen = controller._frozen_request.frozen_material_snapshot
    assert "review-inactive-exit" not in {row["id"] for row in frozen["materials"]}
    assert controller._synthesis_payload["result"]["candidate_design"]["exit_medium"] == original.design["exit_medium"]
    applied_signal = QSignalSpy(source.optimizationApplied)
    scene.click(scene.item("filmOptimizationApplyButton"))
    scene.accept_dialog(scene.window.findChild(QObject, "filmOptimizationApplyConfirmation"), 16384)
    assert applied_signal.count() == 1, (controller.error, source.session.snapshot().error)
    assert source.currentDesign()["exit_medium"] == original.design["exit_medium"]
    retained = service.materials.get("review-inactive-exit")["optical_sets"][0]
    assert retained["n"] == [2.8, 2.8] and retained["k"] is None
    assert source.saveCurrent(), source.session.snapshot().error
    persisted = ProjectStore.open(store.project_file).resolve_document(saved.document_id)
    inactive = next(row for row in persisted["materials"] if row["id"] == "review-inactive-exit")
    assert inactive["optical_sets"][0]["k"] is None
    assert inactive["optical_sets"][0]["n"] == [2.8, 2.8]
