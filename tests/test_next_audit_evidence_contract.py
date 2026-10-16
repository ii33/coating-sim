"""Clean-checkout teaching-protocol contracts, offline only."""
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil

import pytest

from tools import benchmark_film_structure as benchmark


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_SHA256 = "647f38a0988c2e8114d7dca909bebf6ba326a37bc84578e2588ceb00c1602b9c"


def test_teaching_protocol_is_byte_preserved_and_uses_tracked_fixture():
    assert benchmark.PROTOCOL_PATH == ROOT / benchmark.PROTOCOL_RELATIVE_PATH
    assert benchmark.PROTOCOL_RELATIVE_PATH.startswith("tests/fixtures/")
    assert hashlib.sha256(benchmark.PROTOCOL_PATH.read_bytes()).hexdigest() == PROTOCOL_SHA256
    protocol = benchmark.load_protocol()
    assert protocol["status"] == "frozen_before_benchmark_runs"
    assert protocol["protocol_id"] == "needle-structure-teaching-benchmarks-20261011-v1"
    assert benchmark.PROTOCOL_RELATIVE_PATH in benchmark.SOURCE_FINGERPRINT_FILES
    assert all(not path.startswith("artifacts/") for path in benchmark.SOURCE_FINGERPRINT_FILES)


def test_protocol_and_source_fingerprint_work_without_artifacts(tmp_path, monkeypatch):
    for relative in benchmark.SOURCE_FINGERPRINT_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path)
    monkeypatch.setattr(benchmark, "PROTOCOL_PATH", tmp_path / benchmark.PROTOCOL_RELATIVE_PATH)
    assert not (tmp_path / "artifacts").exists()
    protocol = benchmark.load_protocol()
    fingerprint = benchmark.source_fingerprint()
    assert len(protocol["cases"]) == 3
    assert fingerprint[benchmark.PROTOCOL_RELATIVE_PATH]["sha256"] == PROTOCOL_SHA256
    assert fingerprint == benchmark.source_fingerprint()
    assert not (tmp_path / "artifacts").exists()
    # An explicit absent path must still fail; do not synthesize a protocol.
    with pytest.raises(FileNotFoundError):
        benchmark.load_protocol(tmp_path / "absent.json")
    assert protocol == deepcopy(benchmark.load_protocol())
