"""Controlled full-result/CSV/cancel regression, in a fresh test directory."""
import csv
import hashlib
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions
from tests.film_performance_fixtures import fixture


def test_50001_by_18_complete_csv_and_cooperative_cancel(tmp_path):
    service, design, frozen = fixture(layers=18)
    settings = dict(wavelength_min_nm=400., wavelength_max_nm=800., samples=50001,
                    angle_deg=37., polarization="unpolarized")
    expected = service.simulate(design, settings)
    backend = FrozenFilmEvaluator(frozen, settings, options=PerformanceOptions(backend="cpu"))
    actual = backend.evaluate_thickness_batch([], np.empty((1, 0)))
    for key in ("R", "T", "A"):
        assert len(expected[key]) == 50001
        np.testing.assert_allclose(actual[key][0], expected[key], atol=2e-12, rtol=2e-12)
    np.testing.assert_array_equal(actual["wavelength_nm"], expected["wavelength_nm"])
    root = tmp_path.resolve()
    def isolated_path(value):
        target = Path(value).resolve()
        assert target.is_relative_to(root)
        return target
    # Only replace the root resolver of this in-memory fixture, never export IO.
    service.paths = SimpleNamespace(_path=isolated_path)
    target = root / "complete-50001.csv"
    service.export_csv(target, expected)
    with target.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        count = 0
        for row in reader:
            count += 1
            if count == 1:
                assert row["evaluation_report_json"]
        assert count == 50001
        assert float(row["wavelength_nm"]) == 800.
        assert float(row["R"]) == expected["R"][-1]
    original_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    class CancelDuringWork:
        def __init__(self): self.checks = 0
        def is_set(self):
            self.checks += 1
            return self.checks >= 8
    with pytest.raises(InterruptedError):
        service.simulate(design, settings, cancel_event=CancelDuringWork())
    with pytest.raises(InterruptedError):
        backend.evaluate_thickness_batch([], np.empty((1, 0)), cancel=CancelDuringWork())
    assert hashlib.sha256(target.read_bytes()).hexdigest() == original_hash
