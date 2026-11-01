"""Whole-domain preview is detached, bounded and never scoring evidence."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import threading
import time

import numpy as np
import pytest

from coat_sim.core import film_full_domain_preview as preview
from coat_sim.scheduler.film_display import _selection
from tests.film_performance_fixtures import fixture
from tests.test_film_candidate_display import harness, objective, record


FULL = {"objective_index": 0, "angle_deg": 0.0, "metric": "R", "spectrum_scope": "full_domain"}


def redigest(snapshot):
    snapshot["sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in snapshot.items() if k != "sha256"},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def until(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("preview did not complete")


def test_true_forward_simulation_on_gap_frozen_sources_and_context():
    service, design, frozen = fixture(2)
    objectives = (
        replace(objective(), angles_deg=(0.0, 30.0), polarization="p"),
        replace(objective(), wavelength_min_nm=700.0, wavelength_max_nm=800.0, metric="T"),
    )
    result = preview.full_domain_spectrum(frozen, design, objectives, dict(FULL, angle_deg=30.0))
    assert (
        len(result["actual"]) == 1024
        and result["wavelength_nm"][0] == 500
        and result["wavelength_nm"][-1] == 800
    )
    direct = service.simulate(
        design,
        dict(
            wavelength_min_nm=500.0,
            wavelength_max_nm=800.0,
            samples=1024,
            angle_deg=30.0,
            polarization="p",
            extrapolation_policy="error",
        ),
    )
    assert np.allclose(result["actual"], direct["R"])
    assert result["actual"][512] == pytest.approx(direct["R"][512])  # real gap simulation
    selected_t = preview.full_domain_spectrum(
        frozen, design, objectives, dict(FULL, objective_index=1, metric="T")
    )
    direct_t = service.simulate(
        design,
        dict(
            wavelength_min_nm=500.0,
            wavelength_max_nm=800.0,
            samples=1024,
            angle_deg=0.0,
            polarization="unpolarized",
            extrapolation_policy="error",
        ),
    )
    assert np.allclose(selected_t["actual"], direct_t["T"])
    assert not np.allclose(selected_t["actual"], result["actual"])
    with pytest.raises(ValueError):
        preview.full_domain_spectrum(dict(frozen, sha256="x"), design, objectives, FULL)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        preview.full_domain_spectrum(frozen, design, objectives, FULL, cancel_event=cancelled)


def test_source_outside_envelope_and_unknown_k_fail_closed():
    _, design, frozen = fixture(1)
    objectives = (
        objective(),
        replace(objective(), wavelength_min_nm=950.0, wavelength_max_nm=1000.0),
    )
    with pytest.raises(ValueError):
        preview.full_domain_spectrum(frozen, design, objectives, FULL)
    broken = deepcopy(frozen)
    broken["materials"][0]["optical_sets"][0]["k"] = None
    redigest(broken)
    with pytest.raises(ValueError, match="unknown k"):
        preview.full_domain_spectrum(broken, design, (objective(),), FULL)
    design["assume_lossless"] = True
    assert len(preview.full_domain_spectrum(broken, design, (objective(),), FULL)["actual"]) == 1024


@pytest.mark.parametrize(
    "extra",
    [
        {"spectrum_scope": "objective"},
        {"spectrum_scope": None},
        {"unexpected": 1},
        {"spectrum_scope": "full_domain", "x": 1},
    ],
)
def test_selection_rejects_unknown_fields_and_values(extra):
    with pytest.raises(ValueError):
        _selection({"objective_index": 0, "angle_deg": 0.0, "metric": "R", **extra})


def test_latest_only_cache_cancel_and_close(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def compute(snapshot, design, objectives, selection, *, cancel_event):
        calls.append(design["name"])
        if design["name"] == "old":
            started.set()
            release.wait(2)
            if cancel_event.is_set():
                raise InterruptedError()
        return {"wave": design["name"]}

    monkeypatch.setattr(preview, "full_domain_spectrum", compute)
    worker = preview.LatestPreview({}, (objective(),))
    key1 = ("ctx", 1, 0, 0.0, "R", ("old",))
    key2 = ("ctx", 1, 0, 0.0, "R", ("new",))
    worker.request(key1, FULL, {"old": {"name": "old"}})
    assert started.wait(2)
    worker.request(key2, FULL, {"new": {"name": "new"}})
    release.set()
    until(lambda: worker.get(key2, "new") is not None)
    assert worker.get(key1, "old") is None and calls == ["old", "new"]
    # New current plus unchanged best keeps completed best without resimulation.
    key3 = ("ctx", 1, 0, 0.0, "R", ("new", "next"))
    worker.request(key3, FULL, {"new": {"name": "new"}, "next": {"name": "next"}})
    until(lambda: worker.get(key3, "next") is not None)
    assert calls == ["old", "new", "next"]
    worker.close()
    worker.thread.join(2)
    assert not worker.thread.is_alive() and worker.get(key3, "next") is None


def test_best_captured_before_scope_switch_and_terminal_cancels(monkeypatch):
    observer, packets, clock, control = harness()
    observer._frozen_snapshot = {}
    calls = []

    def compute(snapshot, design, objectives, selection, *, cancel_event):
        calls.append(design["front_layers"][0]["thickness"]["value"])
        return {
            "wavelength_nm": [500.0, 600.0],
            "actual": [0.1, 0.2],
            "indices": [0, 1],
            "original_point_count": 2,
        }

    monkeypatch.setattr(preview, "full_domain_spectrum", compute)
    record(observer, best=True)
    assert calls == [] and observer._designs["best"] is not None
    control["spectrum_scope"] = "full_domain"
    clock[0] = 1.0
    observer.refresh()
    until(lambda: bool(calls))
    clock[0] = 2.0
    observer.refresh()
    assert packets[-1]["best"]["availability"] == "available"
    assert packets[-1]["best"]["statistics"]["original_point_count"] == 3
    worker = observer._preview
    observer.finish(state="completed")
    worker.thread.join(2)
    assert not worker.thread.is_alive() and packets[-1]["terminal"]
    assert packets[-1]["best"]["spectrum"] is None


def test_slow_best_not_starved_by_current_churn_but_control_change_cancels(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls, cancelled = [], []

    def compute(snapshot, design, objectives, selection, *, cancel_event):
        name = design["name"]
        calls.append(name)
        if name == "best":
            started.set()
            release.wait(3)
            cancelled.append(cancel_event.is_set())
            if cancel_event.is_set():
                raise InterruptedError()
        return {"name": name}

    monkeypatch.setattr(preview, "full_domain_spectrum", compute)
    worker = preview.LatestPreview({}, (objective(),))

    def key(seq, current):
        return ("context", seq, 0, 0.0, "R", ("best", current))

    worker.request(key(1, "a"), FULL, {"best": {"name": "best"}, "a": {"name": "a"}})
    assert started.wait(2)
    for current in "bcdef":
        worker.request(
            key(1, current), FULL, {"best": {"name": "best"}, current: {"name": current}}
        )
    release.set()
    until(lambda: worker.get(key(1, "f"), "best") is not None)
    until(lambda: worker.get(key(1, "f"), "f") is not None)
    assert cancelled == [False] and calls == ["best", "f"]
    worker.close()
    worker.thread.join(2)

    started.clear()
    release.clear()
    calls.clear()
    cancelled.clear()
    worker = preview.LatestPreview({}, (objective(),))
    worker.request(key(1, "a"), FULL, {"best": {"name": "best"}})
    assert started.wait(2)
    worker.request(key(2, "z"), FULL, {"z": {"name": "z"}})
    release.set()
    until(lambda: worker.get(key(2, "z"), "z") is not None)
    assert cancelled == [True] and worker.get(key(1, "a"), "best") is None
    worker.close()
    worker.thread.join(2)


def test_unavailable_reason_and_off_do_not_affect_scoring(monkeypatch):
    observer, packets, clock, control = harness()
    observer._frozen_snapshot = {}

    def fail(*args, **kwargs):
        raise ValueError("source out of range\nmissing wavelength")

    monkeypatch.setattr(preview, "full_domain_spectrum", fail)
    record(observer, best=True)
    assert packets[-1]["best"]["nominal_pass"]
    control["spectrum_scope"] = "full_domain"
    clock[0] = 1.0
    observer.refresh()
    until(lambda: observer._preview and bool(observer._preview.results))
    clock[0] = 2.0
    observer.refresh()
    assert packets[-1]["best"]["availability"] == "unavailable"
    assert packets[-1]["best"]["unavailable_reason"] == "source out of range missing wavelength"
    assert packets[-1]["best"]["nominal_pass"]
    worker = observer._preview
    control["enabled"] = False
    clock[0] = 2.1
    observer.refresh()  # cancel even inside the publication gate
    assert worker.key is None and worker.results == {}
    control["enabled"] = True
    clock[0] = 3.0
    observer.refresh()
    assert observer._preview is worker  # rapid toggles never start a second worker
    observer.finish()
    worker.thread.join(2)
    assert not worker.thread.is_alive()


def test_owned_transport_full_scope_and_cancel_marker(tmp_path):
    from types import SimpleNamespace
    from coat_sim.scheduler.film_display import FilmDisplayRegistry, WorkerDisplaySession
    from tests.test_film_display_transport import row, control
    from tests.test_film_candidate_display import rows, coverage

    item = row(enabled=True)
    directory = tmp_path / "artifacts/runtime/scheduler/jobs" / item["job_id"]
    directory.mkdir(parents=True)
    registry = FilmDisplayRegistry(tmp_path)
    registry.start(item, directory)
    clock = [0.0]
    marker = tmp_path / "cancel"
    _, design, frozen = fixture(1)
    session = WorkerDisplaySession(
        directory,
        item["job_id"],
        item["input_digest"],
        item["film_display_config"],
        clock=lambda: clock[0],
        cancel_marker=marker,
    )
    obs = session.observer_for_request(
        SimpleNamespace(objectives=(objective(),), frozen_material_snapshot=frozen)
    )
    target = objective()
    obs.record(
        design,
        weighted_rms_error=0.0,
        max_normalized_error=0.0,
        condition_rows=rows(target),
        raw_coverage=coverage(target),
        physical_thickness_nm=[65.0],
        is_best=True,
    )
    assert registry.poll(item, directory)
    control(registry, item, enabled=True, selection=FULL)
    clock[0] = 1.0
    obs.refresh()
    assert registry.poll(item, directory)
    assert registry.attach_job(item)["film_display"]["display"]["best"]["availability"] == "waiting"
    until(lambda: obs._preview and bool(obs._preview.results))
    clock[0] = 2.0
    obs.refresh()
    assert registry.poll(item, directory)
    slot = registry.attach_job(item)["film_display"]["display"]["best"]
    assert slot["availability"] == "available" and len(slot["spectrum"]["actual"]) == 1024
    assert session.preview_control_is_current(1, FULL)
    marker.touch()
    assert not session.preview_control_is_current(1, FULL)
    assert not session.control()["enabled"]
    worker = obs._preview
    session.finish("cancelled")
    worker.thread.join(2)
    assert not worker.thread.is_alive()


def test_slow_current_publishes_previous_matching_summary_during_churn(monkeypatch):
    observer, packets, clock, control = harness()
    observer._frozen_snapshot = {}
    control["spectrum_scope"] = "full_domain"
    started, release, hold_next = threading.Event(), threading.Event(), threading.Event()

    def slow(snapshot, design, objectives, selection, *, cancel_event):
        thickness = design["front_layers"][0]["thickness"]["value"]
        if thickness == 90.0:
            started.set()
            release.wait(3)
        elif thickness != 100.0:
            hold_next.wait(3)
        if cancel_event.is_set():
            raise InterruptedError()
        return {
            "wavelength_nm": [500.0, 600.0],
            "actual": [thickness / 1000] * 2,
            "indices": [0, 1],
            "original_point_count": 2,
        }

    monkeypatch.setattr(preview, "full_domain_spectrum", slow)
    record(observer, 100.0, best=True)
    until(lambda: bool(observer._preview.results))
    clock[0] = 1.0
    record(observer, 90.0)
    old_hash = observer._current[0]["physical_hash"]
    assert started.wait(2)
    clock[0] = 2.0
    record(observer, 80.0)
    clock[0] = 3.0
    record(observer, 70.0)
    release.set()
    until(
        lambda: (
            observer._preview.previous_current(observer._preview.key) is not None
            and observer._preview.previous_current(observer._preview.key)[0] == old_hash
        )
    )
    clock[0] = 4.0
    record(observer, 60.0)
    shown = packets[-1]["current"]
    assert shown["preview_delayed"] is True
    assert shown["physical_hash"] == old_hash
    assert shown["total_thickness_nm"] == 90.0
    assert shown["spectrum"]["actual"] == [0.09, 0.09]
    assert shown["physical_hash"] != observer._current[0]["physical_hash"]
    control["enabled"] = False
    clock[0] = 4.1
    observer.refresh()
    assert observer._preview.previous_current(observer._preview.key) is None
    hold_next.set()
    worker = observer._preview
    observer.finish()
    worker.thread.join(2)
    assert not worker.thread.is_alive()


def test_full_spectrum_is_actually_reduced_to_packet_budget(monkeypatch):
    from coat_sim.core import film_candidate_display as display

    observer, packets, clock, control = harness()
    observer._frozen_snapshot = {}
    control["spectrum_scope"] = "full_domain"
    monkeypatch.setattr(display, "MAX_DISPLAY_BYTES", 14000)

    def large(snapshot, design, objectives, selection, *, cancel_event):
        waves = np.linspace(500.0, 600.0, 1024).tolist()
        actual = [0.12345678901234567] * 1024
        return {
            "wavelength_nm": waves,
            "actual": actual,
            "indices": list(range(1024)),
            "original_point_count": 1024,
        }

    monkeypatch.setattr(preview, "full_domain_spectrum", large)
    record(observer, best=True)
    until(lambda: bool(observer._preview.results))
    clock[0] = 1.0
    observer.refresh()
    packet = packets[-1]
    for role in ("current", "best"):
        spectrum = packet[role]["spectrum"]
        assert spectrum is not None and 2 <= len(spectrum["actual"]) < 1024
        assert spectrum["indices"][0] == 0 and spectrum["indices"][-1] == 1023
        assert spectrum["original_point_count"] == 1024
    assert len(json.dumps(packet, separators=(",", ":")).encode()) <= 14000 - 1024
    worker = observer._preview
    observer.finish()
    worker.thread.join(2)
