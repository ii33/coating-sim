"""Cancellation arriving from a render must prevent completion publication."""
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from coat_sim.core.appearance_execution import execute_appearance
from tests.test_appearance_result_contract import _job


class AppearanceCancelPublicationTests(unittest.TestCase):
    def test_cancel_after_render_does_not_publish_partial_files_as_success(self):
        # Asset rebuild and ray tracing have separate integration coverage.
        # Inject cancellation at their return boundary using only a fresh root.
        with tempfile.TemporaryDirectory(prefix="appearance-cancel-publication-") as directory:
            root = Path(directory).resolve()
            job_id = "b" * 32
            envelope = {"schema": "coat_sim.appearance_frozen_request", "schema_version": 1,
                        "root": str(root), "request": {"display_exposure": 0.01},
                        "context": {}, "context_sha256": "a" * 64,
                        "job": _job(), "job_sha256": "c" * 64}
            from coat_sim.core.appearance_result import _sha256_json
            envelope["context_sha256"] = _sha256_json(envelope["context"])
            envelope["job_sha256"] = _sha256_json(envelope["job"])
            cancel = Event()
            output = root / "data/tmp/appearance" / job_id / "render"

            def interrupted_render(job, target, **kwargs):
                self.assertIs(kwargs["cancel_event"], cancel)
                self.assertEqual(target, output)
                target.mkdir()
                (target / "radiance_spectrum.npy").write_bytes(b"partial frame")
                cancel.set()
                return {}

            with patch("coat_sim.core.appearance_compute.build_frozen_request", return_value=envelope), \
                    patch("coat_sim.scheduler.store.SchedulerStore.status", return_value={
                        "kind": "appearance_render", "input_digest": "d" * 64}), \
                    patch("coat_sim.appearance.render.render_job", side_effect=interrupted_render):
                with self.assertRaises(InterruptedError):
                    execute_appearance(root, job_id, {"frozen": envelope}, cancel, lambda _: None)
            self.assertTrue((output / "radiance_spectrum.npy").exists())
            self.assertFalse((root / "artifacts/runtime/scheduler/jobs" / job_id /
                              "appearance-result.json").exists())


if __name__ == "__main__":
    unittest.main()
