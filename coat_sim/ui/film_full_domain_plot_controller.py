"""Latest-only, cancellable formal spectrum preview; never result authority."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Lock
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot, Qt


class FilmFullDomainPlotController(QObject):
    changed = Signal()
    _completed = Signal(int, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-full-domain")
        self._completed.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._generation = 0
        self._key = None
        self._input_key = None
        self._inputs: Any = None
        self._pending = None
        self._active_cancel = None
        self._inflight = False
        self._closed = False
        self.data = {}
        self.status = ""

    def request(self, key, frozen_snapshot, original_design, candidate_design, objectives, selection):
        """Replace pending work, keeping at most one active and one pending request."""
        if self._closed or key == self._key:
            return
        input_key = (id(frozen_snapshot), id(original_design), id(candidate_design), id(objectives))
        if input_key != self._input_key:
            # The request/result are logically frozen, but their nested mappings
            # are mutable. Detach once on enqueue, before another producer can
            # change them; subsequent angle/metric switches reuse this snapshot.
            self._inputs = deepcopy((frozen_snapshot, original_design, candidate_design, objectives))
            self._input_key = input_key
        frozen_snapshot, original_design, candidate_design, objectives = self._inputs
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._key = key
            if self._active_cancel is not None:
                self._active_cancel.set()
            self._pending = (generation, frozen_snapshot, original_design, candidate_design,
                             objectives, dict(selection))
            start = not self._inflight
            self._inflight = True
        self.data = {}
        self.status = "正在后台计算正式全域光谱；不改变优化验收。"
        self.changed.emit()
        if start:
            self._executor.submit(self._work)

    def clear(self):
        with self._lock:
            self._generation += 1
            self._key = self._pending = None
            self._input_key = self._inputs = None
            if self._active_cancel is not None:
                self._active_cancel.set()
        had_state = bool(self.data or self.status)
        self.data, self.status = {}, ""
        if had_state:
            self.changed.emit()

    def _work(self):
        from coat_sim.core.film_full_domain_preview import full_domain_spectrum
        while True:
            with self._lock:
                pending, self._pending = self._pending, None
                if self._closed or pending is None:
                    self._inflight = False
                    return
                cancel = self._active_cancel = Event()
            generation, frozen, original, candidate, objectives, selection = pending
            result, error = {}, ""
            try:
                baseline = full_domain_spectrum(frozen, original, objectives, selection, cancel_event=cancel)
                if cancel.is_set():
                    raise InterruptedError("preview superseded")
                best = full_domain_spectrum(frozen, candidate, objectives, selection, cancel_event=cancel)
                if baseline["wavelength_nm"] != best["wavelength_nm"]:
                    raise ValueError("全域光谱采样网格不一致")
                result = dict(full_domain=True, wavelength_nm=best["wavelength_nm"],
                              baseline=baseline["actual"], candidate=best["actual"],
                              metric=selection["metric"])
            except InterruptedError:
                continue
            except Exception as exc:
                error = f"正式全域光谱不可用：{exc}"
            try:
                self._completed.emit(generation, result, error)
            except RuntimeError:
                return

    @Slot(int, object, str)
    def _accept(self, generation, result, error):
        if self._closed or generation != self._generation:
            return
        self.data = result
        self.status = error or "正式全域光谱为独立显示采样；不改变目标验收。"
        self.changed.emit()

    def shutdown(self):
        if self._closed:
            return
        with self._lock:
            self._closed = True
        self.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
