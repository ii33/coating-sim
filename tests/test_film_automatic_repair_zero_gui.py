"""Zero-layer repair projections and real QML over production optical evidence."""
from copy import deepcopy
import json
from math import fsum
from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QColor
from PySide6.QtTest import QSignalSpy
import shiboken6

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.scheduler import SchedulerClient
from coat_sim.ui.compute_target import ComputeTarget
from coat_sim.ui.film_manufacturing_quick_adapter import layer_change_rows, layer_total_nm
from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.film_structure_fixtures import fixture
from tests.test_film_automatic_repair_live_display import (
    app as app,
    live_gui as live_gui,
    show_repair_algorithm,
    start_live,
)
from tests.test_film_manufacturing_quick_controller import _readable_offscreen_font
from tests.test_film_synthesis_gui_review import _Scene, _wait
from tests.test_scheduler_service import stop_service

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def total(design):
    assert all(row["thickness"]["unit"] == "nm" for row in design["front_layers"])
    return fsum(row["thickness"]["value"] for row in design["front_layers"])


def test_zero_preview_and_canonical_removal_keep_ids_lineage_and_physical_totals():
    layers = [dict(id=key, material_id="same", thickness_nm=value, origins=[key])
              for key, value in (("left", 100.), ("thin", 7.), ("right", 60.))]
    source = dict(layers=layers)
    preview = deepcopy(source)
    preview["layers"][1]["thickness_nm"] = 0.
    best = dict(layers=[dict(deepcopy(layers[0]), thickness_nm=160., origins=["left", "right"])])
    rows = {row["id"]: row for row in layer_change_rows(source, preview, best, bar_scale_nm=160.)}
    assert layer_total_nm(preview) == layer_total_nm(best) == 160.
    assert rows["thin"]["current_nm"] == rows["thin"]["current_ratio"] == 0.
    assert "待核验" in rows["thin"]["status"]
    assert rows["thin"]["best_deleted"] and rows["thin"]["best_ratio"] == 0.
    assert "已删除" in rows["thin"]["status"]
    assert rows["left"]["best_nm"] == 160. and rows["left"]["best_ratio"] == 1.
    assert rows["left"]["best_origins"] == ["left", "right"]
    assert "合并/拆分至" in rows["right"]["status"]
    qw = dict(design=dict(front_layers=[dict(id="qw", thickness=dict(value=.25, unit="QW"))]))
    assert layer_total_nm(qw) is None  # Unconverted optical thickness must never be added as nm.
    assert layer_total_nm(dict(layers=[])) == 0.


def test_live_scale_only_expands_and_next_run_resets_to_new_source(live_gui):
    h = live_gui
    scales = []
    def frontier(source):
        for name, left, thin in (("expand", 160., 7.5), ("shrink", 80., 7.9)):
            candidate = deepcopy(source)
            candidate["front_layers"][0]["thickness"]["value"] = left
            candidate["front_layers"][1]["thickness"]["value"] = thin
            yield name, candidate, {row["id"]: [row["id"]] for row in source["front_layers"]}
    h.frontier = frontier
    def inspect(frame):
        current = frame.get("current") if frame.get("stage") == "repair_live" else None
        if current is None or current["evidence_scope"] != "training":
            return
        if current["operator"] in {"expand", "shrink"}:
            scale = h.panel.liveBarScaleNm
            totals = h.panel.liveTotals
            assert scale == 160.
            assert totals["current_nm"] == total(current["design"])
            assert totals["current_hash"] == current["physical_hash"]
            assert totals["seq"] == frame["seq"]
            scales.append((current["operator"], scale))
            if current["operator"] == "shrink":
                h.panel.cancel()
    h.client.after_frame = inspect
    start_live(h)
    h.pool.run()
    assert not h.panel.error, h.panel.error
    assert {name for name, _scale in scales} == {"expand", "shrink"}
    # A new run must not inherit a discarded large trial's scale.
    assert h.panel.prepare(), h.panel.error
    source_max = max(row["thickness"]["value"] for row in h.session.snapshot().design["front_layers"])
    assert source_max < 160.
    h.frontier = lambda _source: iter(())
    h.client.after_frame = lambda _frame: h.panel.cancel()
    start_live(h)
    assert h.panel.liveBarScaleNm == 0.
    h.pool.run()
    assert not h.panel.error, h.panel.error
    assert h.panel.liveBarScaleNm == source_max


def contrast(foreground, background):
    def luminance(color):
        channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in color]
        return sum(a * b for a, b in zip(channels, (.2126, .7152, .0722), strict=True))
    first, second = sorted((luminance(foreground), luminance(background)))
    return (second + .05) / (first + .05)


@pytest.mark.parametrize("mode,scale,width", [
    ("light", 1.0, 1100), ("dark", 1.0, 760),
    ("light", 1.4, 760), ("dark", 1.4, 1100),
])
def test_real_qml_zero_trial_cancel_shows_applied_champion_totals_bars_and_undo(
    live_gui, tmp_path, mode, scale, width,
):
    h = live_gui
    assert h.owner.setSearchMode("automatic_repair"), h.owner.error
    before = deepcopy(h.session.snapshot().design)
    revision = h.session.snapshot().design_revision
    applied = QSignalSpy(h.source.optimizationApplied)
    theme = ThemeService.instance()
    previous = (theme._mode, theme._font_scale, theme._density_scale)
    captured: list[str] = []
    scene = None
    def frontier(source):
        for name, value in (("partial", 7.5), ("collapse_thin_layer:thin", 0.)):
            candidate = deepcopy(source)
            candidate["front_layers"][1]["thickness"]["value"] = value
            yield name, candidate, {row["id"]: [row["id"]] for row in source["front_layers"]}
    h.frontier = frontier
    try:
        with _readable_offscreen_font():
            theme.configure(mode=mode, font_scale=scale, density_scale=1.0)
            scene = _Scene(h.owner, "FilmOptimizationWindow.qml")
            scene.window.setWidth(width)
            scene.window.setHeight(950)
            show_repair_algorithm(scene, h.owner)
            def inspect(frame):
                current = frame.get("current") if frame.get("stage") == "repair_live" else None
                if (current is None or current["operator"] != "collapse_thin_layer:thin"
                        or current["evidence_scope"] != "training" or captured):
                    return
                assert current["layers"][1]["thickness_nm"] == 0.
                assert frame["champion"] is not None
                assert h.panel.liveTotals["current_nm"] == 160.
                assert h.panel.liveTotals["best_nm"] == 167.5
                assert h.panel.liveTotals["current_hash"] != h.panel.liveTotals["best_hash"]
                rows = {row["id"]: row for row in h.panel.liveLayerRows}
                assert rows["thin"]["current_ratio"] == 0.
                assert "待核验" in rows["thin"]["status"]
                assert scene is not None
                bar = scene.item("filmRepairLiveBar_thin")
                assert bar.width() == 0.
                for key, row in rows.items():
                    background = scene.item("filmRepairLiveBar_" + key)
                    row_item = scene.item("filmRepairLiveLayer_" + key)
                    assert background.width() == pytest.approx(row_item.width() * row["current_ratio"], abs=.5)
                    assert row["current_ratio"] == pytest.approx((row["current_nm"] or 0.) / h.panel.liveBarScaleNm)
                    assert float(scene.evaluate(row_item, "mapToItem(null,width,0).x")) <= scene.window.width() - 16
                values = scene.item("filmRepairLiveValues_left")
                geometry = json.loads(scene.evaluate(values,
                    "JSON.stringify({width:width,paintedWidth:paintedWidth,height:height,paintedHeight:paintedHeight})"))
                assert geometry["width"] + .5 >= geometry["paintedWidth"]
                assert geometry["height"] + .5 >= geometry["paintedHeight"]
                foreground = cast(QColor, values.property("color"))
                surface = QColor(str(theme.current_snapshot["colorSurface"]))
                accent = QColor(str(theme.current_snapshot["colorAccent"]))
                mixed = [a * .12 + b * .88 for a, b in zip(
                    (accent.redF(), accent.greenF(), accent.blueF()),
                    (surface.redF(), surface.greenF(), surface.blueF()), strict=True)]
                assert contrast((foreground.redF(), foreground.greenF(), foreground.blueF()), mixed) >= 4.5
                QCoreApplication.processEvents()
                assert scene.window.grabWindow().save(str(tmp_path / f"zero-live-{mode}-{scale}-{width}.png"))
                captured.append(current["physical_hash"])
                scene.click(scene.item("filmOptimizationCancelButton"))
            h.client.after_frame = inspect
            start_live(h)
            h.pool.run()
            assert captured and not h.panel.error, h.panel.error
            assert h.panel._applied and applied.count() == 1
            assert h.session.snapshot().design_revision == revision + 1
            best = h.panel._applied_snapshot
            assert best is not None and total(best["design"]) == 167.5
            assert h.panel.liveTotals["mode"] == "applied"
            assert h.panel.liveTotals["current_nm"] == h.panel.liveTotals["best_nm"] == 167.5
            assert h.panel.liveTotals["current_hash"] == h.panel.liveTotals["best_hash"] == best["physical_hash"]
            rows = {row["id"]: row for row in h.panel.liveLayerRows}
            assert rows["thin"]["current_nm"] == rows["thin"]["best_nm"] == 7.5
            assert rows["thin"]["current_ratio"] == rows["thin"]["best_ratio"]
            assert captured[0] != best["physical_hash"]  # Unreviewed zero never becomes the applied display.
            assert "167.5 nm" in scene.item("filmRepairLiveTotal").property("text")
            assert h.source.undoOptimizedDesign() and h.session.snapshot().design == before
    finally:
        if scene is not None:
            scene.close()
        theme.configure(mode=previous[0], font_scale=previous[1], density_scale=previous[2])


def test_real_zero_champion_is_canonical_deleted_merged_and_manual_apply_is_one_transaction(live_gui):
    h = live_gui
    before = deepcopy(h.session.snapshot().design)
    revision = h.session.snapshot().design_revision
    def frontier(source):
        candidate = deepcopy(source)
        candidate["front_layers"][1]["thickness"]["value"] = 0.
        yield "collapse_thin_layer:thin", candidate, {row["id"]: [row["id"]] for row in source["front_layers"]}
    h.frontier = frontier
    start_live(h)
    h.pool.run()
    assert not h.panel.error, h.panel.error
    assert h.panel.canApply and not h.panel._applied
    best = h.panel._selected_snapshot()
    assert best is not None and len(best["design"]["front_layers"]) == 1
    assert total(best["design"]) == 160.
    assert best["layers"][0]["origins"] == ["left", "right"]
    assert h.panel.liveTotals["current_nm"] == h.panel.liveTotals["best_nm"] == 160.
    assert h.panel.liveTotals["mode"] == "terminal_best"
    rows = {row["id"]: row for row in h.panel.liveLayerRows}
    assert rows["thin"]["current_deleted"] and rows["thin"]["current_ratio"] == 0.
    assert "已删除" in rows["thin"]["status"]
    assert "合并/拆分至" in rows["right"]["status"]
    assert h.panel.prepareApplication()
    assert h.panel.applyPreparedCandidate(h.panel.pendingApplicationHash), h.panel.error
    assert h.session.snapshot().design_revision == revision + 1
    assert len(h.session.snapshot().design["front_layers"]) == 1
    assert {row["layer_id"] for row in h.owner.variables} == {"left"}
    assert h.source.undoOptimizedDesign() and h.session.snapshot().design == before


def test_actual_http_zero_champion_cancel_applies_once_and_synchronizes_display(app, tmp_path, monkeypatch):
    """Real HTTP/worker/SLSQP; only an existing native hard bound is a fixture.

    A physical upper bound of 7 nm is lower than this repair goal's 8 nm floor.
    Positive local repair can improve to 7; zero-directed repair can remove it.
    Coalescing may skip raw zero trials, which are tested separately above.
    """
    original_request_init = FilmSynthesisRequest.__init__
    def native_bound_input(self, *args, **kwargs):
        kwargs.setdefault("max_layer_thickness_nm", 7.)
        original_request_init(self, *args, **kwargs)
    monkeypatch.setattr(FilmSynthesisRequest, "__init__", native_bound_input)
    service, _library, design = fixture(tmp_path)
    design["front_layers"][0]["thickness"]["value"] = .5
    session = FilmDesignEditSession(service)
    assert session.request_replace_design(design)
    source = FilmDesignQuickController(service=service, edit_session=session)
    owner = FilmOptimizationQuickController(source)
    panel = cast(FilmManufacturingQuickController, owner.manufacturingController)
    client = SchedulerClient(tmp_path)
    panel.client_factory = lambda _root: client
    scene = _Scene(owner, "FilmOptimizationWindow.qml")
    cancelled: list[str] = []
    observed: list[float] = []
    try:
        assert owner.setObjectiveField("wavelength_min_nm", 520.)
        assert owner.setObjectiveField("wavelength_max_nm", 580.)
        assert owner.setObjectiveField("pointwise.upper_bound_percent", 100.)
        assert panel.prepare(), panel.error
        assert owner.setSearchMode("automatic_repair"), owner.error
        before = deepcopy(session.snapshot().design)
        revision = session.snapshot().design_revision
        applied = QSignalSpy(source.optimizationApplied)
        show_repair_algorithm(scene, owner)
        def inspect():
            frame = panel._live_frame
            if frame is None or frame.get("champion") is None:
                return
            best = frame["champion"]
            best_total = total(best["design"])
            observed.append(best_total)
            if panel.busy and best_total == 0. and not cancelled:
                displayed = cast(dict, panel.property("liveTotals"))
                assert displayed["best_nm"] == 0.
                assert displayed["best_hash"] == best["physical_hash"]
                cancelled.append(best["physical_hash"])
                scene.click(scene.item("filmOptimizationCancelButton"))
        panel.changed.connect(inspect)
        start_live(SimpleNamespace(panel=panel))
        _wait(lambda: not panel.busy, seconds=45.)
        assert not panel.error, panel.error
        assert cancelled and panel._applied, dict(observed=observed, status=panel.statusText)
        payload = panel._payload
        assert payload is not None
        assert payload["request"]["synthesis"]["request"]["max_layer_thickness_nm"] == 7.
        audited = [event for event in payload["result"]["trace"] if event.get("kind") == "audited_candidate"]
        assert any(event["operator"] == "compensate_thin_layers" and event["assessment"]["thickness_complete"] is False
                   and event["assessment"]["applicable_improvement"] for event in audited)
        assert any(event["operator"].startswith("collapse_thin_layer:") and event["assessment"]["thickness_complete"] is True
                   and event["assessment"]["applicable_improvement"] for event in audited)
        assert panel._stop_reason == "user_cancel"
        assert session.snapshot().design_revision == revision + 1 and applied.count() == 1
        assert session.snapshot().design["front_layers"] == []
        totals = cast(dict, panel.property("liveTotals"))
        assert totals["mode"] == "applied"
        assert totals["current_nm"] == totals["best_nm"] == 0.
        assert totals["current_hash"] == totals["best_hash"] == cancelled[0]
        rows = cast(list[dict], panel.property("liveLayerRows"))
        assert len(rows) == 1 and rows[0]["current_deleted"] and rows[0]["best_deleted"]
        assert rows[0]["current_ratio"] == rows[0]["best_ratio"] == 0.
        assert "已删除" in rows[0]["status"]
        plot = cast(dict, owner.property("plotData"))
        expected = service.simulate(session.snapshot().design, dict(
            wavelength_min_nm=plot["wavelength_nm"][0], wavelength_max_nm=plot["wavelength_nm"][-1],
            samples=len(plot["wavelength_nm"])))
        assert plot["candidate"] == pytest.approx(expected["R"])
        assert source.undoOptimizedDesign() and session.snapshot().design == before
    finally:
        scene.close()
        owner.shutdown(1000)
        source.markSourceClosed()
        cast(ComputeTarget, owner.computeTarget)._executor.shutdown(wait=True, cancel_futures=True)
        if (client.root / "artifacts/runtime/scheduler/endpoint.json").exists():
            stop_service(client)
        shiboken6.delete(owner)
        shiboken6.delete(source)


def test_pinned_manual_confirmation_keeps_display_frame_and_applied_hash(live_gui):
    h = live_gui
    h.proposals = [7.5, 7.9, 7.95]
    pinned: list[tuple[str, int, str]] = []
    def inspect(frame):
        current = frame.get("current") if frame.get("stage") == "repair_live" else None
        if current is None:
            return
        if frame.get("champion") is not None and not pinned:
            assert h.panel.prepareApplication()
            pinned.append((h.panel.pendingApplicationHash, h.panel.liveTotals["seq"], h.panel.liveTotals["current_hash"]))
        if current["layers"][1]["thickness_nm"] == 7.95 and not h.panel._applied:
            assert h.panel.liveTotals["seq"] == pinned[0][1]
            assert h.panel.liveTotals["current_hash"] == pinned[0][2]
            assert h.panel.liveTotals["best_hash"] == pinned[0][0]
            assert h.panel.applyPreparedCandidate(pinned[0][0]), h.panel.error
    h.client.after_frame = inspect
    start_live(h)
    h.pool.run()
    assert not h.panel.error and h.panel._applied, h.panel.error
    assert h.panel.liveTotals["current_nm"] == h.panel.liveTotals["best_nm"] == 167.5
    assert h.panel.liveTotals["current_hash"] == pinned[0][0]


def test_cancel_without_improvement_replaces_trial_display_with_unchanged_complete_source(live_gui):
    h = live_gui
    before = deepcopy(h.session.snapshot().design)
    revision = h.session.snapshot().design_revision
    h.proposals = [6.]
    cancelled: list[bool] = []
    def inspect(frame):
        current = frame.get("current") if frame.get("stage") == "repair_live" else None
        if current is not None and current["operator"] == "controlled_partial_0" and not cancelled:
            assert h.panel.liveTotals["current_nm"] == 166.
            cancelled.append(True)
            h.panel.cancel()
    h.client.after_frame = inspect
    start_live(h)
    h.pool.run()
    assert cancelled
    assert not h.panel.error and not h.panel._applied, h.panel.error
    assert h.session.snapshot().design == before and h.session.snapshot().design_revision == revision
    assert h.panel.liveTotals["mode"] == "source_unchanged"
    assert h.panel.liveTotals["current_nm"] == 167.
    assert h.panel.liveTotals["current_hash"] == h.panel._payload["result"]["source"]["physical_hash"]
    assert {row["id"]: row["current_nm"] for row in h.panel.liveLayerRows} == {"left": 100., "thin": 7., "right": 60.}
