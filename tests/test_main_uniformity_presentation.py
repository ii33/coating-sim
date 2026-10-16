"""Read-only uniformity labels, numeric definitions, and bridge state contract."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import qt_test_guard  # noqa: F401 -- isolate user settings before Qt imports
import numpy as np

from PySide6.QtCore import QUrl
from coat_sim.core.api import CoatService
from coat_sim.core.process_simulator import _field_metrics
from coat_sim.ui.main import MainBridge
from coat_sim.ui.uniformity_presentation import format_uniformity_rows


def rows_by_key(result):
    return {row["key"]: row for row in format_uniformity_rows(result)}


class UniformityPresentationTests(unittest.TestCase):
    def test_existing_population_cv_is_same_domain_and_rms_inverts_unrounded_statistics(self):
        field = np.array([[1., 2., np.nan], [3., np.inf, -np.inf]])
        result = _field_metrics(field)
        before = deepcopy(result)
        valid = field[np.isfinite(field)]
        self.assertEqual(result["mean_nm"], float(valid.mean()))
        self.assertEqual(result["cv_percent"], float(valid.std(ddof=0)) / valid.mean() * 100)
        recovered = result["mean_nm"] * (result["cv_percent"] / 100)
        self.assertAlmostEqual(recovered, float(valid.std(ddof=0)), places=15)
        self.assertNotAlmostEqual(recovered, float(np.sqrt(np.mean(valid ** 2))), places=3)
        rows = rows_by_key(result)
        self.assertEqual(rows["pv_nm"]["value"], "2 nm")
        self.assertEqual(rows["pv_percent"]["value"], "100 %")
        self.assertEqual(rows["rms_nm"]["value"], "0.816 nm")
        self.assertEqual(rows["rms_percent"]["value"], "40.825 %")
        self.assertEqual(result, before)

    def test_bilingual_labels_units_and_no_machine_or_surface_rows(self):
        rows = format_uniformity_rows({"mean_nm": 100, "min_nm": 98, "max_nm": 102,
                                      "pv_nm": 4, "pv_percent": 4, "cv_percent": 1.25,
                                      "device": "GPU", "elapsed_s": 100,
                                      "surface_pv_nm": 999, "rms_nm": 999})
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(" (" in row["label"] and row["label"].endswith("：") for row in rows))
        self.assertEqual(rows[-2]["label"], "均方根偏差 (RMS)：")
        self.assertEqual(rows[-2]["value"], "1.25 nm")
        self.assertEqual(rows[-1]["value"], "1.25 %")
        self.assertNotIn("GPU", str(rows))
        self.assertNotIn("999", str(rows))

    def test_missing_statistics_are_not_reconstructed_from_field_or_zeroed(self):
        self.assertEqual(format_uniformity_rows(None), [])
        rows = rows_by_key({"field_nm": [[1, 2, 3]], "pv_nm": 2})
        self.assertEqual(rows["pv_nm"]["value"], "2 nm")
        for key in ("mean_nm", "min_nm", "max_nm", "pv_percent", "rms_nm", "rms_percent"):
            self.assertEqual(rows[key]["value"], "未提供")

    def test_statistics_display_never_reads_or_resamples_the_grid(self):
        class UnreadableGrid:
            def __array__(self, *args, **kwargs):
                raise AssertionError("display attempted to recompute grid statistics")
            def __iter__(self):
                raise AssertionError("display attempted to traverse the grid")
        result = {"field_nm": UnreadableGrid(), "mean_nm": 100., "cv_percent": 1.25}
        self.assertEqual(rows_by_key(result)["rms_nm"]["value"], "1.25 nm")

    def test_rms_requires_positive_mean_and_nonnegative_finite_cv(self):
        cases = [(0, 0), (-1, 5), (np.nan, 5), (np.inf, 5), (100, np.nan),
                 (100, np.inf), (100, -1), (100, None), (None, 1),
                 (True, 1), (100, False), ("100", 1), (100, "1")]
        for mean, cv in cases:
            with self.subTest(mean=mean, cv=cv):
                rows = rows_by_key({"mean_nm": mean, "cv_percent": cv})
                self.assertEqual(rows["rms_nm"]["value"], "未提供")
                self.assertEqual(rows["rms_percent"]["value"], "未提供")

    def test_uniform_field_has_zero_deviation_not_zero_thickness(self):
        rows = rows_by_key(_field_metrics(np.full((2, 3), 200.)))
        self.assertEqual(rows["mean_nm"]["value"], "200 nm")
        self.assertEqual(rows["rms_nm"]["value"], "0 nm")
        self.assertEqual(rows["rms_percent"]["value"], "0 %")
        self.assertEqual(rows_by_key({"pv_nm": -0.0})["pv_nm"]["value"], "0 nm")

    def test_invalid_pv_is_not_inferred_and_tiny_nonzero_display_is_scientific(self):
        rows = rows_by_key({"mean_nm": 1., "cv_percent": 0.00001,
                            "pv_nm": np.inf, "pv_percent": -1,
                            "min_nm": 0.5, "max_nm": 1.5})
        self.assertEqual(rows["pv_nm"]["value"], "未提供")
        self.assertEqual(rows["pv_percent"]["value"], "未提供")
        self.assertEqual(rows["rms_nm"]["value"], "1e-07 nm")
        self.assertEqual(rows["rms_percent"]["value"], "1e-05 %")


class UniformityBridgeStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bridge = MainBridge(CoatService(root=Path(self.directory.name), restrict_to_root=False))
        self.addCleanup(self.stop_owned_probe_and_scheduler)

    def stop_owned_probe_and_scheduler(self):
        self.bridge.detach_observers()
        target = self.bridge._compute_target
        target._poll.stop()
        target._executor.shutdown(wait=True, cancel_futures=True)
        root = Path(self.directory.name)
        if (root / "artifacts/runtime/scheduler/endpoint.json").is_file():
            from coat_sim.scheduler import SchedulerClient
            from tests.test_scheduler_service import stop_service
            stop_service(SchedulerClient(root))

    def test_initial_loading_failure_and_cancel_states_are_distinct(self):
        bridge = self.bridge
        self.assertEqual(bridge.uniformityRows, [])
        self.assertIn("计算完成后", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "info")
        bridge._busy = True
        self.assertIn("正在计算", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "busy")
        bridge._finish(bridge._job_id, None, 0, ValueError("网格无效"))
        self.assertIn("计算失败：网格无效", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "error")
        bridge._finish(bridge._job_id, None, 0, InterruptedError())
        self.assertIn("计算已取消", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "info")

    def test_old_result_retained_while_metrics_log_is_cleared_or_attempt_fails(self):
        bridge = self.bridge
        result = {"mean_nm": 100., "cv_percent": 1.25, "pv_nm": 4., "pv_percent": 4.}
        bridge._result = result
        expected = bridge.uniformityRows
        bridge._metrics = []  # Existing queue submission clears log summaries.
        bridge._busy = True
        self.assertEqual(bridge.uniformityRows, expected)
        self.assertIn("上次有效结果", bridge.uniformityStateText)
        bridge._finish(bridge._job_id, None, 0, ValueError("计算中断"))
        self.assertIs(bridge._result, result)
        self.assertEqual(bridge.uniformityRows, expected)
        self.assertIn("上次结果", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "error")
        bridge._status = "已选择工艺积分。"  # Result failure remains contextual.
        self.assertIn("计算失败：计算中断", bridge.uniformityStateText)

    def test_late_result_error_does_not_override_current_state(self):
        bridge = self.bridge
        bridge._job_id = 10
        bridge._uniformity_result_notice = "当前任务有效"
        bridge._finish(9, None, 0, ValueError("过期任务"))
        self.assertEqual(bridge.uniformityStateText, "当前任务有效")

    def test_submission_and_asset_read_failure_preserve_valid_rows(self):
        bridge = self.bridge
        bridge._result = {"mean_nm": 10., "cv_percent": 2.}
        rows = bridge.uniformityRows
        bridge._submission_failed(None, None, OSError("队列服务失败"))
        self.assertEqual(bridge.uniformityRows, rows)
        self.assertIn("队列服务失败", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "error")
        bridge.openAsset(QUrl.fromLocalFile(str(Path(self.directory.name) / "missing.coatasset")))
        self.assertEqual(bridge.uniformityRows, rows)
        self.assertIn("资产包读取失败", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "error")

    def test_invalid_fast_threshold_has_typed_failure_without_clearing_result(self):
        bridge = self.bridge
        bridge._result = {"mean_nm": 10., "cv_percent": 2.}
        rows = bridge.uniformityRows
        bridge._fast_threshold_invalid = True
        bridge.calculate()
        self.assertFalse(bridge.busy)
        self.assertEqual(bridge.uniformityRows, rows)
        self.assertIn("残余时长阈值", bridge.uniformityStateText)
        self.assertEqual(bridge.uniformityStateSeverity, "error")


class UniformityQueueAssetTests(unittest.TestCase):
    # The existing peer runs real isolated CPU workers with typed design files,
    # while avoiding a shared daemon, remote computer or network request.
    from tests.test_desktop_compute_queue import QueueTests as _QueueSupport
    setUp = _QueueSupport.setUp
    tearDown = _QueueSupport.tearDown
    spin = _QueueSupport.spin
    bridge = _QueueSupport.bridge

    def test_queue_asset_load_preserves_uniformity_and_can_calculate_again(self):
        initial = self.bridge()
        initial.calculate()
        self.spin(lambda: not initial.busy, timeout=20)
        self.assertTrue(initial.hasResult, initial.status)
        job = self.store.snapshot()["jobs"][0]
        path = self.store.result_asset_path(job["job_id"])
        original_stamp = path.stat().st_mtime_ns
        loaded = self.service.load_simulation_asset(path)
        reopened = MainBridge(self.service)
        self.bridges.append(reopened)
        reopened._apply_loaded_asset(loaded, str(path), queue_result_copy=True)
        self.assertTrue(reopened.hasResult, reopened.status)
        self.assertFalse(reopened._result_view_only)
        self.assertEqual(reopened.uniformityRows, initial.uniformityRows)
        self.assertEqual(reopened.uniformityStateText, "")
        self.assertEqual(path.stat().st_mtime_ns, original_stamp)
        previous_rows = reopened.uniformityRows
        reopened.calculate()
        self.assertTrue(reopened.busy, reopened.status)
        self.assertEqual(reopened.uniformityRows, previous_rows)
        self.assertEqual(reopened.uniformityStateSeverity, "busy")
        self.spin(lambda: not reopened.busy, timeout=20)
        self.assertTrue(reopened.hasResult, reopened.status)
        self.assertEqual(reopened.uniformityRows, previous_rows)
        self.assertEqual(reopened.uniformityStateText, "")
        self.assertEqual(reopened.uniformityStateSeverity, "info")
        self.assertEqual(len(self.store.snapshot()["jobs"]), 2)
        self.assertTrue(all(row["state"] == "succeeded" for row in self.store.snapshot()["jobs"]))
        self.assertEqual(path.stat().st_mtime_ns, original_stamp)


if __name__ == "__main__":
    unittest.main()
