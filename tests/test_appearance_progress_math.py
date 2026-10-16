"""Deterministic elapsed/ETA coverage; no renderer, sleeps, or deadlines."""
from __future__ import annotations

import unittest

from coat_sim.appearance.progress import RenderProgress


class _Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class AppearanceProgressTests(unittest.TestCase):
    def test_initial_event_has_unknown_remaining(self):
        clock = _Clock(10.0)
        events = []
        progress = RenderProgress(8, events.append, clock=clock)
        clock.now = 12.0

        event = progress.emit()

        self.assertEqual(event["stage"], "rendering")
        self.assertEqual(event["completed_wavelengths"], 0)
        self.assertEqual(event["total_wavelengths"], 8)
        self.assertEqual(event["elapsed_s"], 2.0)
        self.assertEqual(event["fraction"], 0.0)
        self.assertIsNone(event["estimated_remaining_s"])
        self.assertEqual(event["estimate_kind"], "warming_up")
        self.assertEqual(events, [event])

    def test_rough_and_measured_rates_exclude_runtime_startup(self):
        clock = _Clock()
        events = []
        progress = RenderProgress(8, events.append, clock=clock)
        clock.now = 5.0
        progress.emit()

        for completed, now, remaining in ((1, 9.0, 28.0), (2, 13.0, 24.0),
                                          (3, 17.0, 20.0), (4, 21.0, 16.0)):
            with self.subTest(completed=completed):
                clock.now = now
                event = progress.advance()
                self.assertEqual(event["completed_wavelengths"], completed)
                self.assertEqual(event["elapsed_s"], now)
                self.assertEqual(event["fraction"], completed / 8)
                self.assertEqual(event["estimated_remaining_s"], remaining)
                self.assertEqual(event["estimate_kind"],
                                 "rough" if completed < 4 else "measured_rate")

        self.assertEqual([event["completed_wavelengths"] for event in events],
                         [0, 1, 2, 3, 4])

    def test_short_job_switches_to_measured_rate_at_last_wavelength(self):
        clock = _Clock()
        progress = RenderProgress(2, clock=clock)
        progress.emit()
        clock.now = 2.0
        self.assertEqual(progress.advance()["estimate_kind"], "rough")
        clock.now = 4.0

        event = progress.advance()

        self.assertEqual(event["estimate_kind"], "measured_rate")
        self.assertEqual(event["estimated_remaining_s"], 0.0)
        self.assertEqual(event["fraction"], 1.0)

    def test_integration_keeps_its_remaining_time_unknown(self):
        clock = _Clock()
        progress = RenderProgress(2, clock=clock)
        progress.emit()
        clock.now = 2.0
        progress.advance()
        clock.now = 4.0
        progress.advance()
        clock.now = 10.0

        event = progress.emit("integrating")

        self.assertEqual(event["completed_wavelengths"], 2)
        self.assertEqual(event["fraction"], 1.0)
        self.assertEqual(event["elapsed_s"], 10.0)
        self.assertIsNone(event["estimated_remaining_s"])
        self.assertEqual(event["estimate_kind"], "finalizing")

    def test_complete_has_zero_remaining_and_includes_finalization_elapsed(self):
        clock = _Clock()
        progress = RenderProgress(2, clock=clock)
        progress.emit()
        clock.now = 2.0
        progress.advance()
        clock.now = 4.0
        progress.advance()
        clock.now = 9.0

        event = progress.emit("complete")

        self.assertEqual(event["stage"], "complete")
        self.assertEqual(event["estimate_kind"], "complete")
        self.assertEqual(event["estimated_remaining_s"], 0.0)
        self.assertEqual(event["elapsed_s"], 9.0)
        self.assertEqual(event["fraction"], 1.0)

    def test_elapsed_time_never_turns_into_a_computation_timeout(self):
        clock = _Clock()
        progress = RenderProgress(2, clock=clock)
        progress.emit()
        clock.now = 1_000_000_000.0

        first = progress.advance()

        self.assertEqual(first["completed_wavelengths"], 1)
        self.assertEqual(first["elapsed_s"], 1_000_000_000.0)
        self.assertEqual(first["estimated_remaining_s"], 1_000_000_000.0)
        clock.now = 2_000_000_000.0
        progress.advance()
        complete = progress.emit("complete")
        self.assertEqual(complete["completed_wavelengths"], 2)
        self.assertEqual(complete["estimated_remaining_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
