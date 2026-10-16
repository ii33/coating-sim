"""Informational elapsed/remaining estimates; never a computation deadline."""
from __future__ import annotations
import time


class RenderProgress:
    def __init__(self, total, callback=None, clock=time.monotonic):
        self.total = int(total)
        self.callback = callback
        self.clock = clock
        self.started = clock()
        self.render_started = None
        self.completed = 0

    def emit(self, stage="rendering"):
        now = self.clock()
        if self.render_started is None:
            self.render_started = now
        remaining = None
        kind = "warming_up"
        if stage == "complete":
            remaining, kind = 0.0, "complete"
        elif stage == "integrating":
            kind = "finalizing"
        elif self.completed:
            remaining = max(0.0, now - self.render_started) / self.completed * (self.total - self.completed)
            kind = "rough" if self.completed < min(4, self.total) else "measured_rate"
        event = {"stage": stage, "completed_wavelengths": self.completed,
                 "total_wavelengths": self.total,
                 "elapsed_s": max(0.0, now - self.started),
                 "estimated_remaining_s": remaining,
                 "fraction": self.completed / self.total if self.total else 0.0,
                 "estimate_kind": kind}
        if self.callback is not None:
            self.callback(event)
        return event

    def advance(self):
        self.completed += 1
        return self.emit()
