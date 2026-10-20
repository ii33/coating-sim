"""Contract checks preventing misleading benchmark speedup comparisons."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from typing import Any


def load_harness():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'benchmark_film_synthesis_parallel.py'
    spec = importlib.util.spec_from_file_location('synthesis_parallel_benchmark', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_semantics_include_ordered_rank_prescription_and_audit_but_not_execution_time():
    harness = load_harness()
    baseline: dict[str, Any] = {'state': 'completed', 'candidate_design': {'front_layers': [{'id': 'a', 'd': 100.}]},
        'trace': [{'stage': 'candidate', 'material_id': 'a'}, {'stage': 'candidate', 'material_id': 'b'}],
        'validation_score': {'max_error': .1, 'audit': {'dense_points': 17}},
        'performance': {'cpu_workers': 1, 'ipc_seconds': 0.}}
    timed = deepcopy(baseline)
    timed['performance'] = {'cpu_workers': 8, 'ipc_seconds': 3.}
    assert harness.semantic_result(timed) == harness.semantic_result(baseline)
    for field, value in [('trace', list(reversed(baseline['trace']))),
                         ('candidate_design', {'front_layers': [{'id': 'a', 'd': 101.}]}),
                         ('validation_score', {'max_error': .1, 'audit': {'dense_points': 33}})]:
        changed = deepcopy(baseline)
        changed[field] = value
        assert harness.semantic_result(changed) != harness.semantic_result(baseline)


def test_native_memory_sampler_observes_owned_current_process():
    harness = load_harness()
    records, method = harness.process_memory(os.getpid())
    if records:
        own = next(row for row in records if row['pid'] == os.getpid())
        assert own['rss'] > 0
    else:
        assert 'unavailable' in method


def test_fixed_cycle_throughput_is_comparable_without_claiming_target_attainment():
    harness = load_harness()
    exhausted = {'state': 'not_found', 'termination_reason': 'cycle_limit', 'targets_met': False}
    for name in ('throughput', 'multilayer'):
        assert harness.case_acceptance_passed(name, exhausted)
        assert not harness.case_acceptance_passed(name, exhausted, partial=True)
        for rejected in (
            {'state': 'failed', 'termination_reason': 'numerical_failure: invalid response'},
            {'state': 'cancelled', 'termination_reason': 'cancelled'},
            {'state': 'not_found', 'termination_reason': 'search_exhausted'},
            {'state': 'not_found', 'termination_reason': 'evaluation_limit'},
        ):
            assert not harness.case_acceptance_passed(name, rejected)
        assert harness.case_acceptance_passed(name, {'state': 'completed', 'targets_met': True})
    for name in ('ar', 'smoke'):
        assert not harness.case_acceptance_passed(name, exhausted)
        assert not harness.case_acceptance_passed(name, {'state': 'completed', 'targets_met': False})
        assert harness.case_acceptance_passed(name, {'state': 'completed', 'targets_met': True})
