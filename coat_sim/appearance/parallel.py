"""Bounded, spawned CPU processes for independent exact spectral samples."""
from __future__ import annotations

import multiprocessing
import time
from numbers import Integral

_DATA = None
_MI = None


def validate_workers(workers: int) -> int:
    if isinstance(workers, bool) or not isinstance(workers, Integral) or not 1 <= workers <= 64:
        raise ValueError("appearance workers must be an integer between 1 and 64")
    return int(workers)


def _initialize(data):
    # Keep initializer infallible: failed runtime imports belong to tasks so
    # Pool propagates them instead of endlessly respawning failed initializers.
    global _DATA, _MI
    _DATA, _MI = data, None


def _render_wave(index):
    global _MI
    from .render import _load_mitsuba, _render_one_wave
    if _MI is None:
        _MI = _load_mitsuba()
        import drjit as dr
        dr.set_thread_count(1)
    return _render_one_wave(_MI, _DATA, index)


def iter_parallel_waves(data, workers: int, cancel_event=None):
    """Yield bounded results as ready; caller must close on early abandonment."""
    workers = min(validate_workers(workers), len(data["wavelength_nm"]))
    if workers < 1:
        raise ValueError("appearance wavelength list cannot be empty")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("appearance render cancelled before worker startup")
    pool = multiprocessing.get_context("spawn").Pool(
        processes=workers, initializer=_initialize, initargs=(data,))
    # Pool replaces a crashed process but cannot recover its lost AsyncResult.
    # Retain our original process handles to fail instead of waiting forever.
    owned_processes = tuple(pool._pool)
    finished = False
    try:
        pending = {}
        next_index = 0
        count = len(data["wavelength_nm"])
        while next_index < count or pending:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("appearance render cancelled while awaiting wavelength")
            if any(process.exitcode is not None for process in owned_processes):
                raise RuntimeError("appearance wavelength worker exited unexpectedly")
            while next_index < count and len(pending) < workers:
                pending[next_index] = pool.apply_async(_render_wave, (next_index,))
                next_index += 1
            ready = [index for index, task in pending.items() if task.ready()]
            if not ready:
                time.sleep(0.025)
                continue
            for index in ready:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("appearance render cancelled at wavelength boundary")
                result = pending.pop(index).get()
                if result[0] != index:
                    raise RuntimeError("appearance worker returned mismatched wavelength index")
                yield result
        finished = True
    finally:
        if finished:
            pool.close()
        else:
            pool.terminate()
        pool.join()
