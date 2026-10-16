"""Stage-based effect receipts for the shared capability entrypoint."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coat_sim.core.api import ServiceError
from coat_sim.core.capabilities import CapabilityError, CapabilityService
from coat_sim.core.film_design import new_design


_TEMP_BASE = Path(tempfile.gettempdir())


class _EffectFixture:
    """Filesystem and domain fixture confined to one fresh temporary root."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.validation_calls = 0
        self.save_calls = 0
        self.simulation_calls = 0
        self.csv_calls = 0
        self.save_error = None
        self.result = {
            "settings": {"samples": 2},
            "provenance": [],
            "wavelength_nm": [400.0, 500.0],
            "R": [0.1, 0.2],
            "T": [0.8, 0.7],
            "A": [0.1, 0.1],
        }

    def _path(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise ServiceError("path_outside_root", "outside project root")
        return path

    def validate_film_design(self, data):
        self.validation_calls += 1
        return deepcopy(data)

    def save_film_design(self, path, data, *, overwrite=False):
        self.save_calls += 1
        if self.save_error is not None:
            raise self.save_error
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("saved", encoding="utf-8")
        return {"path": str(target)}

    def simulate_film_design(self, data, settings=None):
        self.simulation_calls += 1
        return {"design": deepcopy(data), **deepcopy(self.result)}

    def export_film_csv(self, path, result, *, overwrite=False):
        self.csv_calls += 1
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("wavelength_nm,R,T,A\n400,0.1,0.8,0.1\n", encoding="utf-8")
        return {"path": str(target)}


class CapabilityEffectStageTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="capability-effect-", dir=str(_TEMP_BASE))
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self.service = _EffectFixture(self.root)
        self.capabilities = CapabilityService(self.service)
        self.design = new_design()

    def assert_error(self, operation, arguments, *, code, effect):
        with self.assertRaises(CapabilityError) as raised:
            self.capabilities.call(operation, arguments)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.effect, effect)
        return raised.exception

    def test_strict_arguments_and_save_preflight_rejections_report_none_without_writes(self):
        self.assert_error("save_film_design", {
            "path": "data/design.cfilm", "data": self.design, "overwrite": 1,
        }, code="invalid_arguments", effect="none")
        self.assert_error("save_film_design", {
            "path": "../escape.cfilm", "data": self.design,
        }, code="path_outside_root", effect="none")
        self.assert_error("save_film_design", {
            "path": "data/design.json", "data": self.design,
        }, code="invalid_request", effect="none")

        self.assert_error("save_film_design", {
            "path": "data/design.cfilm", "data": {"invalid": True},
        }, code="invalid_request", effect="none")

        self.assertEqual(self.service.save_calls, 0)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_simulation_preflight_refusal_reports_none_without_calculation_or_artifacts(self):
        self.assert_error("simulate_film_design", {
            "data": self.design, "output_path": "data/outside-results.json",
        }, code="invalid_request", effect="none")
        self.assertEqual(self.service.validation_calls, 0)
        self.assertEqual(self.service.simulation_calls, 0)
        self.assertEqual(self.service.csv_calls, 0)
        self.assertEqual(list(self.root.rglob("*")), [])

        existing_csv = self.root / "data" / "results" / "existing.csv"
        existing_csv.parent.mkdir(parents=True)
        existing_csv.write_text("existing", encoding="utf-8")
        self.assert_error("simulate_film_design", {
            "data": self.design, "output_path": "data/results/existing.json",
        }, code="invalid_request", effect="none")
        self.assertEqual(self.service.simulation_calls, 0)
        self.assertEqual(self.service.csv_calls, 0)

        self.assert_error("simulate_film_design", {
            "data": {"invalid": True}, "output_path": "data/results/fresh.json",
        }, code="invalid_request", effect="none")
        self.assertEqual(self.service.simulation_calls, 0)
        self.assertEqual(self.service.csv_calls, 0)
        self.assertFalse((self.root / "data" / "results" / "fresh.csv").exists())
        self.assertFalse((self.root / "data" / "results" / "fresh.json").exists())

    def test_same_path_error_code_after_save_dispatch_remains_unknown(self):
        self.service.save_error = ServiceError("path_outside_root", "late path error")

        self.assert_error("save_film_design", {
            "path": "data/designs/late.cfilm", "data": self.design,
        }, code="path_outside_root", effect="unknown")
        self.assertEqual(self.service.save_calls, 1)

    def test_csv_published_before_json_failure_remains_unknown(self):
        def fail_json(path, payload, *, overwrite):
            raise OSError("fixture JSON publication failure")

        capabilities = CapabilityService(self.service, film_json_publisher=fail_json)
        self.assert_error_from(capabilities, "simulate_film_design", {
            "data": self.design, "output_path": "data/results/partial.json",
        }, code="invalid_request", effect="unknown")

        self.assertEqual(self.service.simulation_calls, 1)
        self.assertEqual(self.service.csv_calls, 1)
        self.assertTrue((self.root / "data" / "results" / "partial.csv").is_file())
        self.assertFalse((self.root / "data" / "results" / "partial.json").exists())

    def test_default_simulation_path_uses_one_uuid_for_preflight_and_dispatch(self):
        capabilities = CapabilityService(self.service)
        with patch("coat_sim.core.film_result_service.uuid4") as mocked_uuid:
            mocked_uuid.return_value.hex = "single-target"
            reply = capabilities.call("simulate_film_design", {"data": self.design})

        expected = self.root / "data" / "results" / "film_single-target.json"
        self.assertEqual(Path(reply["artifact_paths"]["json"]), expected)
        mocked_uuid.assert_called_once_with()
        self.assertTrue(expected.is_file())
        self.assertTrue(expected.with_suffix(".csv").is_file())

    def assert_error_from(self, capabilities, operation, arguments, *, code, effect):
        with self.assertRaises(CapabilityError) as raised:
            capabilities.call(operation, arguments)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.effect, effect)
        return raised.exception


if __name__ == "__main__":
    unittest.main()
