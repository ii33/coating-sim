"""Graceful stage slicing retains real probes but never suppresses cancellation."""
from typing import Any

import pytest

from coat_sim.core.film_synthesis_batch_needle import scan_batch_needles, build_batch_needle_design
from tests.test_film_synthesis_batch_needle import _problem, _valley_evaluator

pytestmark = pytest.mark.algorithm


def test_wall_slice_keeps_completed_negative_proposals_and_allows_later_joint_build():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, request.candidate_materials[:1],
        evaluate=_valley_evaluator(calls), gradient=lambda value, ids: [-1.] * len(ids),
        position_samples=9, max_needles=2, stop_check=lambda: len(calls) >= 2)
    assert scan.probe_count == 2 and len(scan.selected) == 2
    assert not scan.complete and scan.stop_reason == 'stage_budget'
    build = build_batch_needle_design(design, frozen, scan.selected, seed_thickness_nm=.01)
    assert len(build.needle_ids) == 2 and len(calls) == 2


def test_stop_before_first_probe_is_partial_not_stationary_or_failure():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), stop_check=lambda: True)
    assert not scan.complete and scan.stop_reason == 'stage_budget'
    assert scan.probe_count == 0 and scan.selected == () and calls == []


def test_global_cancellation_exception_is_not_converted_to_local_partial_success():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    def cancelled():
        if len(calls) >= 2:
            raise InterruptedError('user cancellation')
        return False
    with pytest.raises(InterruptedError, match='user cancellation'):
        scan_batch_needles(design, frozen, request.candidate_materials,
            evaluate=_valley_evaluator(calls), stop_check=cancelled)
    assert len(calls) == 2


def test_invalid_stop_hook_rejected_before_optical_call():
    request, design, frozen = _problem()
    invalid: Any = False
    with pytest.raises(ValueError, match='stop_check'):
        scan_batch_needles(design, frozen, request.candidate_materials,
            evaluate=lambda *args, **kwargs: pytest.fail('no optical call'), stop_check=invalid)
