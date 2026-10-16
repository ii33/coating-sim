"""Mocked scheduling coverage for parallel appearance wavelengths.

These tests deliberately replace the spawned Pool and Mitsuba boundary. They
exercise parent-side scheduling and cleanup only; no renderer/GPU validation is
implied.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from threading import Event
from unittest.mock import patch

import numpy as np

from coat_sim.appearance import parallel


class _AsyncResult:
    def __init__(self, pool, index, ready_after=0, error=None):
        self.pool = pool
        self.index = index
        self.ready_after = ready_after
        self.error = error
        self.polls = 0
        self.completed = False

    def ready(self):
        self.polls += 1
        return self.polls > self.ready_after

    def get(self):
        self.completed = True
        self.pool.active -= 1
        if self.error is not None:
            raise self.error
        base_seed = self.pool.initargs[0]["resolution"]["seed"]
        return (self.index, np.full((1, 1), self.index, dtype=np.float32),
                float(self.index), base_seed + self.index)


class _FakePool:
    def __init__(self, *, ready_after=None, errors=None, process_exitcodes=()):
        self.ready_after = ready_after or {}
        self.errors = errors or {}
        self.initargs = None
        self.submitted = []
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.terminated = False
        self.joined = False
        # The implementation checks owned workers for unexpected process exits.
        self._pool = [type("FakeProcess", (), {"exitcode": code})()
                      for code in process_exitcodes]

    def apply_async(self, function, args):
        index = args[0]
        self.submitted.append(index)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        return _AsyncResult(self, index, self.ready_after.get(index, 0),
                            self.errors.get(index))

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True

    def join(self):
        self.joined = True


class _FakeContext:
    def __init__(self, pool):
        self.pool = pool
        self.kwargs = None

    def Pool(self, **kwargs):
        self.kwargs = kwargs
        self.pool.initargs = kwargs["initargs"]
        return self.pool


def _data(count=4):
    return {"wavelength_nm": np.arange(500.0, 500.0 + count),
            "resolution": {"seed": 71}}


class AppearanceParallelTests(unittest.TestCase):
    def _install_pool(self, pool):
        context = _FakeContext(pool)
        patcher = patch.object(parallel.multiprocessing, "get_context",
                               return_value=context)
        patcher.start()
        self.addCleanup(patcher.stop)
        return context

    def test_results_are_yielded_as_ready_with_bounded_submissions(self):
        pool = _FakePool(ready_after={0: 2})
        context = self._install_pool(pool)

        results = list(parallel.iter_parallel_waves(_data(), workers=2))

        self.assertEqual([row[0] for row in results], [1, 2, 0, 3])
        self.assertEqual([row[3] for row in results], [72, 73, 71, 74])
        self.assertEqual(pool.submitted, [0, 1, 2, 3])
        self.assertLessEqual(pool.max_active, 2)
        self.assertEqual(context.kwargs["processes"], 2)
        self.assertEqual(context.kwargs["initializer"], parallel._initialize)
        self.assertTrue(pool.closed)
        self.assertFalse(pool.terminated)
        self.assertTrue(pool.joined)

    def test_cancel_after_a_result_terminates_and_joins_pool(self):
        pool = _FakePool()
        self._install_pool(pool)
        cancel = Event()
        results = parallel.iter_parallel_waves(_data(), workers=2,
                                               cancel_event=cancel)

        self.assertEqual(next(results)[0], 0)
        cancel.set()
        with self.assertRaises(InterruptedError):
            next(results)

        self.assertTrue(pool.terminated)
        self.assertFalse(pool.closed)
        self.assertTrue(pool.joined)

    def test_worker_exception_terminates_and_joins_pool(self):
        pool = _FakePool(errors={1: RuntimeError("synthetic worker failure")})
        self._install_pool(pool)
        results = parallel.iter_parallel_waves(_data(), workers=2)

        # One already-completed result can be observed before the next future
        # raises; render_job must not publish a completed frame in that case.
        self.assertEqual(next(results)[0], 0)
        with self.assertRaisesRegex(RuntimeError, "synthetic worker failure"):
            next(results)

        self.assertTrue(pool.terminated)
        self.assertFalse(pool.closed)
        self.assertTrue(pool.joined)

    def test_worker_exit_is_reported_and_pool_is_terminated(self):
        pool = _FakePool(process_exitcodes=(17,))
        self._install_pool(pool)
        results = parallel.iter_parallel_waves(_data(), workers=2)

        with self.assertRaisesRegex(RuntimeError, "worker.*exit|exit.*worker"):
            next(results)

        self.assertTrue(pool.terminated)
        self.assertFalse(pool.closed)
        self.assertTrue(pool.joined)

    def test_worker_count_validation(self):
        for invalid in (0, -1, 65, True, 1.5, "2", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parallel.validate_workers(invalid)
        self.assertEqual(parallel.validate_workers(1), 1)
        self.assertEqual(parallel.validate_workers(64), 64)

    def test_worker_failure_does_not_publish_complete_render_artifacts(self):
        from coat_sim.appearance.render import render_job
        from tests.test_appearance_result_contract import _job

        with tempfile.TemporaryDirectory(prefix="appearance-parallel-failure-") as directory:
            output = Path(directory) / "render"

            def fail_after_first_wave(data, workers, cancel_event=None):
                yield (0, np.ones((2, 2), dtype=np.float32), 0.0, 3)
                raise RuntimeError("synthetic worker failure")

            with patch("coat_sim.appearance.render._load_mitsuba", return_value=object()), \
                    patch("coat_sim.appearance.parallel.iter_parallel_waves",
                          side_effect=fail_after_first_wave):
                with self.assertRaisesRegex(RuntimeError, "synthetic worker failure"):
                    render_job(_job(), output, workers=2)

            self.assertTrue((output / "radiance_spectrum.npy").exists())
            for filename in ("manifest.json", "xyz.npy", "linear_srgb.npy", "display.png"):
                self.assertFalse((output / filename).exists(), filename)

    def test_cli_passes_explicit_worker_count_to_render_job(self):
        from coat_sim.appearance import cli

        class _MMap:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Spectrum:
            _mmap = _MMap()

        result = {"radiance_spectrum": _Spectrum(),
                  "manifest": {"input_sha256": "a" * 64,
                               "film_rgb_equality_max_abs": 0.0}}
        with tempfile.TemporaryDirectory(prefix="appearance-cli-") as directory:
            job_path = Path(directory) / "job.json"
            job_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            output = Path(directory) / "output"
            stdout = io.StringIO()
            with patch("coat_sim.appearance.cli.render_job", return_value=result) as renderer, \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([str(job_path), str(output), "--workers", "3"]), 0)

        self.assertEqual(renderer.call_args.kwargs["workers"], 3)
        self.assertTrue(result["radiance_spectrum"]._mmap.closed)
        self.assertIn('"input_sha256"', stdout.getvalue())


class AppearanceRenderLifecycleTests(unittest.TestCase):
    @staticmethod
    def _waves(*args, **kwargs):
        for index in range(2):
            yield (index, np.ones((2, 2), dtype=np.float32), 0.0, 3 + index)

    @contextlib.contextmanager
    def _mock_runtime(self, *, waves=None, cmf=None):
        """Use real output mmaps but no renderer or spawned subprocesses."""
        mmaps = []
        original_open_memmap = np.lib.format.open_memmap

        def capture_memmap(*args, **kwargs):
            spectrum = original_open_memmap(*args, **kwargs)
            mmaps.append(spectrum)
            return spectrum

        wave_results = waves if waves is not None else self._waves
        try:
            with patch("coat_sim.appearance.render._load_mitsuba", return_value=object()), \
                    patch("coat_sim.appearance.render._serial_wave_results",
                          side_effect=wave_results), \
                    patch("coat_sim.appearance.parallel.iter_parallel_waves",
                          side_effect=wave_results), \
                    patch("coat_sim.appearance.render.np.lib.format.open_memmap",
                          side_effect=capture_memmap), \
                    patch("coat_sim.appearance.render._cmf_table", side_effect=cmf,
                          return_value=np.ones((2, 3), dtype=np.float64)) as cmf_mock:
                yield mmaps, cmf_mock
        finally:
            # Keep failed assertions safe on Windows, where an open mmap can
            # otherwise prevent TemporaryDirectory from removing the output.
            for spectrum in mmaps:
                spectrum._mmap.close()

    def _assert_failed_output(self, output, mmaps):
        self.assertEqual(len(mmaps), 1)
        self.assertTrue(mmaps[0]._mmap.closed)
        self.assertTrue((output / "radiance_spectrum.npy").exists())
        self.assertFalse((output / "manifest.json").exists())

    def test_cancel_on_result_exhaustion_closes_spectrum_before_integration(self):
        from coat_sim.appearance.render import render_job
        from tests.test_appearance_result_contract import _job

        for workers in (1, 2):
            with self.subTest(workers=workers), \
                    tempfile.TemporaryDirectory(prefix="appearance-exhaustion-cancel-") as directory:
                cancel = Event()
                output = Path(directory) / "render"

                def cancel_after_last_wave(*args, **kwargs):
                    yield from self._waves()
                    cancel.set()

                with self._mock_runtime(waves=cancel_after_last_wave) as (mmaps, cmf_mock):
                    with self.assertRaisesRegex(InterruptedError, "before result integration"):
                        render_job(_job(), output, workers=workers, cancel_event=cancel)
                    self._assert_failed_output(output, mmaps)
                    cmf_mock.assert_not_called()

    def test_cancel_during_cmf_closes_spectrum_without_completion_publication(self):
        from coat_sim.appearance.render import render_job
        from tests.test_appearance_result_contract import _job

        for workers in (1, 2):
            with self.subTest(workers=workers), \
                    tempfile.TemporaryDirectory(prefix="appearance-cmf-cancel-") as directory:
                cancel = Event()
                output = Path(directory) / "render"

                def cancel_during_cmf(wavelengths):
                    cancel.set()
                    return np.ones((wavelengths.size, 3), dtype=np.float64)

                with self._mock_runtime(cmf=cancel_during_cmf) as (mmaps, cmf_mock):
                    with self.assertRaisesRegex(InterruptedError, "after color matching"):
                        render_job(_job(), output, workers=workers, cancel_event=cancel)
                    self._assert_failed_output(output, mmaps)
                    cmf_mock.assert_called_once()
                    for filename in ("xyz.npy", "linear_srgb.npy", "display.png"):
                        self.assertFalse((output / filename).exists(), filename)

    def test_progress_callback_cancellation_blocks_completion_publication(self):
        from coat_sim.appearance.render import render_job
        from tests.test_appearance_result_contract import _job

        for workers in (1, 2):
            for cancel_stage in ("integrating", "complete"):
                with self.subTest(workers=workers, cancel_stage=cancel_stage), \
                        tempfile.TemporaryDirectory(prefix="appearance-progress-cancel-") as directory:
                    cancel = Event()
                    output = Path(directory) / "render"
                    stages = []

                    def on_progress(event):
                        stages.append(event["stage"])
                        if event["stage"] == cancel_stage:
                            cancel.set()

                    with self._mock_runtime() as (mmaps, cmf_mock):
                        with self.assertRaises(InterruptedError):
                            render_job(_job(), output, workers=workers, cancel_event=cancel,
                                       progress_callback=on_progress)
                        self.assertIn(cancel_stage, stages)
                        self._assert_failed_output(output, mmaps)
                        if cancel_stage == "integrating":
                            cmf_mock.assert_not_called()

    def test_post_wave_cmf_failure_closes_spectrum(self):
        from coat_sim.appearance.render import render_job
        from tests.test_appearance_result_contract import _job

        for workers in (1, 2):
            with self.subTest(workers=workers), \
                    tempfile.TemporaryDirectory(prefix="appearance-cmf-failure-") as directory:
                output = Path(directory) / "render"
                with self._mock_runtime(cmf=RuntimeError("synthetic CMF failure")) as (mmaps, _):
                    with self.assertRaisesRegex(RuntimeError, "synthetic CMF failure"):
                        render_job(_job(), output, workers=workers)
                    self._assert_failed_output(output, mmaps)


if __name__ == "__main__":
    unittest.main()
