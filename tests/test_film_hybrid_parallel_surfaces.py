"""Hybrid multicore contracts through real GUI, frozen jobs, CLI and MCP.

No scheduler endpoint is started or contacted. Job tests construct typed specs;
CLI tests use only the real headless --plan path, never a search operation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
import json
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from coat_sim.core import film_performance
from coat_sim.core.capabilities import CapabilityError
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, deserialize_job_payload,
)
from coat_sim.core.mcp_contracts import (
    FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA,
    validate_mcp_arguments,
)
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import (
    SYNTHESIS_SEARCH_DEFAULTS, synthesis_options, synthesis_spec,
)
from tests.film_structure_fixtures import fixture
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene

pytestmark = [pytest.mark.qt, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def _frozen(*, workers=19, batch_size=None):
    service, request = setup_problem(low=550., high=550., samples=1)
    return freeze_film_synthesis_request(service, replace(request,
        search_strategy="hybrid", cpu_workers=workers, parallel_batch_size=batch_size,
        max_working_set_bytes=3 * 1024**3, strict_upper_bound_objective_indices=(0,)))


@pytest.mark.parametrize("value,expected", (
    (None, None), ("", None), ("  ", None), ("auto", None), (" AuTo ", None),
    (19, 19), ("19", 19),
))
def test_parallel_batch_editor_accepts_auto_or_positive_integer(value, expected):
    assert SYNTHESIS_SEARCH_DEFAULTS["parallel_batch_size"] is None
    assert synthesis_options({"parallel_batch_size": value})["parallel_batch_size"] == expected


@pytest.mark.parametrize("value", (
    True, False, 0, "0", -1, "-1", 1.5, "1.5", float("nan"), "NaN",
    float("inf"), "infinity", "9007199254740992", "invalid",
))
def test_parallel_batch_editor_rejects_invalid_values(value):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        synthesis_options({"parallel_batch_size": value})


@pytest.mark.parametrize("workers,expected", ((0, 9), (1, 1), (2, 2), (19, 9)))
def test_auto_batch_freezes_available_request_cap_and_formal_roundtrips(monkeypatch, workers, expected):
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 9)
    frozen = _frozen(workers=workers)
    assert frozen.parallel_batch_size == expected
    envelope = film_synthesis_request_to_payload("hybrid-surfaces", frozen)
    assert set(envelope["request"]) == {item.name for item in fields(FilmSynthesisRequest)}
    # Exercise JSON transport, not merely Python dataclass equality.
    envelope = json.loads(json.dumps(envelope))
    request_id, restored, structures = film_synthesis_request_from_payload(envelope)
    assert request_id == "hybrid-surfaces" and structures == ()
    assert json.loads(json.dumps(film_synthesis_request_to_payload(request_id, restored))) == envelope
    canonical = canonical_parameters(None, {"payload": envelope})
    job = build_job_payload(canonical)
    queued_id, queued, metadata = deserialize_job_payload(job)
    assert queued_id == request_id and metadata == []
    assert json.loads(json.dumps(film_synthesis_request_to_payload(queued_id, queued))) == envelope
    spec = synthesis_spec(None, frozen)
    parsed = JobSpec.parse(json.loads(json.dumps(spec)))
    assert parsed.executor == "film_synthesis" and parsed.inputs == {}
    _, spec_request, _ = deserialize_job_payload(parsed.parameters["payload"])
    assert spec_request == frozen


def test_explicit_batch_is_retained_with_serial_width_and_legacy_auto(monkeypatch):
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 9)
    frozen = _frozen(workers=1, batch_size=19)
    assert frozen.cpu_workers == 1 and frozen.parallel_batch_size == 19
    envelope = film_synthesis_request_to_payload("serial-logical-19", frozen)
    job = build_job_payload(canonical_parameters(None, {"payload": envelope}))
    _, queued, _ = deserialize_job_payload(job)
    assert queued.parallel_batch_size == 19 and queued.cpu_workers == 1
    service, legacy = setup_problem()
    assert freeze_film_synthesis_request(service, legacy).parallel_batch_size is None


def test_real_qml_batch_edit_restore_invalid_correction_preserve_design(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    original = deepcopy(design)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode("synthesis")
        for row in controller.structureMaterialOptions:
            assert controller.setStructureMaterialSelected(row["material_id"], row["optical_set_id"], True)
        assert controller.canStart
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        warnings: list[str] = []
        scene.engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        try:
            scene.click(scene.item("filmOptimizationAlgorithmTab"))
            field = scene.item("filmSynthesisParallelBatchSizeField")
            assert field.isVisible() and field.property("text") == ""
            scene.edit(field, "19")
            assert controller.synthesisOptions["parallel_batch_size"] == 19
            assert controller._build_synthesis_request().parallel_batch_size == 19
            saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
            assert saved["synthesis"]["options"]["parallel_batch_size"] == 19
            scene.edit(field, "2")
            assert controller.synthesisOptions["parallel_batch_size"] == 2
            assert controller.loadOptimizationConfiguration(saved)
            app.processEvents()
            assert controller.synthesisOptions["parallel_batch_size"] == 19
            assert field.property("text") == "19"
            scene.edit(field, "0")
            assert not controller.canStart and controller._pending_edit_error
            assert controller.synthesisOptions["parallel_batch_size"] == 19
            scene.click(field)
            QTest.keyClick(scene.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(scene.window, Qt.Key.Key_Backspace)
            QTest.keyClick(scene.window, Qt.Key.Key_Return)
            QTest.qWait(20)
            assert controller.synthesisOptions["parallel_batch_size"] is None
            assert controller.canStart and not controller._pending_edit_error
            auto = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
            assert auto["synthesis"]["options"]["parallel_batch_size"] is None
            scene.edit(field, "19")
            assert controller.loadOptimizationConfiguration(auto)
            app.processEvents()
            assert field.property("text") == ""
            # Old settings predate the new field and must retain AUTO.
            auto["synthesis"]["options"].pop("parallel_batch_size")
            assert controller.loadOptimizationConfiguration(auto)
            assert controller._build_synthesis_request().parallel_batch_size is None
            assert session.snapshot().design == original
            assert session.snapshot().design_revision == 19
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings
        finally:
            scene.close()


@pytest.mark.parametrize("actual", (9, 2))
def test_summary_reports_actual_processes_not_requested_parallelism(app, tmp_path, actual):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller._synthesis_payload = {"result": {"performance": {
            "backend": "headless_process", "requested_workers": 19,
            "effective_workers": actual, "worker_pids": list(range(1000, 1000 + actual)),
            "submitted_tasks": 11, "completed_tasks": 10, "consumed_tasks": 8,
            "discarded_tasks": 2, "serial_fallback_tasks": 1,
            "search": {"hybrid": {"frozen_parallel_batch_size": 9,
                "parallel_batches": [{"resource_worker_cap": 2}, None, {},
                                     {"resource_worker_cap": True}]}},
        }}}
        summary = controller.synthesisExecutionSummary
        assert "请求进程数：19" in summary
        assert f"实际工作进程数：{actual}" in summary
        assert "实际工作进程数：19" not in summary
        assert f"记录的 PID 数：{actual}" in summary
        assert "冻结逻辑批次：9" in summary and "阶段准入并发上限：2" in summary
        assert "串行：联合厚度精修/采纳" in summary
        assert "按原序采用：8" in summary and "投机未采用：2" in summary


@pytest.mark.parametrize("batches", (None, [], "missing", {"resource_worker_cap": 19}))
def test_summary_tolerates_missing_historical_parallel_diagnostics(app, tmp_path, batches):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller._synthesis_payload = {"result": {"performance": {
            "requested_workers": 19, "effective_workers": None, "worker_pids": None,
            "search": {"hybrid": {"frozen_parallel_batch_size": None, "parallel_batches": batches}},
        }}}
        summary = controller.synthesisExecutionSummary
        assert "实际工作进程数：未知" in summary and "记录的 PID 数：未知" in summary
        assert "冻结逻辑批次：未知" in summary and "阶段准入并发上限：未记录" in summary


@pytest.mark.parametrize("actual,backend", ((2, "headless_process"), (0, "serial")))
def test_summary_does_not_label_idle_ready_workers_as_dispatched(app, tmp_path, actual, backend):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller._synthesis_payload = {"result": {"performance": {
            "backend": backend, "requested_workers": 19, "effective_workers": 9,
            "owned_worker_count": 9, "ready_worker_count": 9,
            "workers_with_submitted_tasks": actual, "worker_pids": list(range(1000, 1009)),
        }}}
        summary = controller.synthesisExecutionSummary
        assert f"实际工作进程数：{actual}" in summary
        assert "实际工作进程数：9" not in summary
        assert "记录的 PID 数：9" in summary


def _plan_cli(path, *args):
    return subprocess.run([sys.executable, "-m", "coat_sim.film_synthesis_cli", "--plan",
        "--request", str(path), *args], cwd=ROOT, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("batch", ("7", "auto"))
def test_real_cli_plan_override_is_frozen_and_never_rewrites_input(tmp_path, batch):
    frozen = _frozen(workers=2, batch_size=19)
    path = tmp_path / "frozen-request.json"
    path.write_text(json.dumps(film_synthesis_request_to_payload("plan-batch", frozen)), encoding="utf-8")
    before = path.read_bytes()
    completed = _plan_cli(path, "--parallel-batch-size", batch)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True and payload["physical_structure_metadata"] == []
    _, planned, _ = film_synthesis_request_from_payload(payload["request"])
    expected = 7 if batch == "7" else min(2, film_performance.available_cpu_workers())
    assert planned.parallel_batch_size == expected
    assert planned.cpu_workers == 2 and planned.objectives == frozen.objectives
    assert planned.strict_upper_bound_objective_indices == (0,)
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid", ("0", "-1", "1.5", "NaN", "true"))
def test_real_cli_rejects_invalid_flag_before_read_or_search(tmp_path, invalid):
    # An absent request proves argparse rejects the option before task admission.
    completed = _plan_cli(tmp_path / "must-not-be-read.json", "--parallel-batch-size", invalid)
    assert completed.returncode == 2
    assert "positive integer or auto" in completed.stderr
    assert completed.stdout == "" and "FileNotFoundError" not in completed.stderr


@pytest.mark.parametrize("mutation", ("ordinary_kind", "unknown_field"))
def test_cli_override_does_not_mask_original_envelope_error(tmp_path, mutation):
    envelope = film_synthesis_request_to_payload("invalid-original", _frozen())
    if mutation == "ordinary_kind":
        envelope["kind"] = "coat_sim.film_optimization_request"
    else:
        envelope["request"]["unrelated_field"] = 1
    path = tmp_path / "invalid-original.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    before = path.read_bytes()
    completed = _plan_cli(path, "--parallel-batch-size", "7")
    assert completed.returncode != 0
    assert not json.loads(completed.stdout)["ok"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("schema", (FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, FILM_SYNTHESIS_SUBMIT_INPUT_SCHEMA))
@pytest.mark.parametrize("value", (None, 1, 19))
def test_mcp_schema_accepts_nullable_positive_integer_without_submit(schema, value):
    envelope = film_synthesis_request_to_payload("mcp-batch", _frozen())
    envelope["request"]["parallel_batch_size"] = value
    arguments = {"payload": envelope}
    assert validate_mcp_arguments(schema, arguments) is arguments


@pytest.mark.parametrize("value", (True, False, 0, -1, 1.5, 7.0, "19", "auto", float("nan")))
def test_mcp_schema_rejects_non_integer_or_nonpositive_batch(value):
    envelope = film_synthesis_request_to_payload("mcp-invalid-batch", _frozen())
    envelope["request"]["parallel_batch_size"] = value
    with pytest.raises(CapabilityError):
        validate_mcp_arguments(FILM_SYNTHESIS_PLAN_INPUT_SCHEMA, {"payload": envelope})


def test_ordinary_v1_request_unknown_field_gates_remain_closed():
    _service, synthesis = setup_problem()
    ordinary = FilmOptimizationRequest(synthesis.environment, 19, (), synthesis.objectives)
    payload = serialize_request(ordinary)
    assert payload["schema_version"] == 1 and "parallel_batch_size" not in payload
    assert not hasattr(deserialize_request(payload), "parallel_batch_size")
    for field in ("parallel_batch_size", "unrelated_field"):
        with pytest.raises(ValueError, match="fields are invalid"):
            deserialize_request({**payload, field: 19})
