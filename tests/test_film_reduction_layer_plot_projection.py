"""Display projection never substitutes unaudited trial data for the incumbent."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core.film_optimization import FilmOptimizationProgress
from coat_sim.scheduler.film_reduction_live import build_live_frame
from tests.test_film_reduction_ui import audited, finish
from tests.test_film_optimization_cancel_apply_review import app as app, review as review

pytestmark = pytest.mark.qt


def test_reduction_projection_expands_scale_and_resets_with_task(review):
    request, result = audited(review)
    controller = review.controller
    assert controller.setSearchMode('reduction')
    frame = build_live_frame(job_id='plot-projection', input_digest='a' * 64,
        sequence=0, request=request, source_design=request.base_request.design_snapshot,
        best_design=result.best_design, baseline_metrics=result.metrics['baseline'],
        candidate_metrics=result.metrics['candidate'], stage='审核完成', state='running',
        iteration=0, evaluations=1)
    assert len(frame['layers']) == 1

    def incumbent(sequence, value):
        item = deepcopy(frame)
        item['sequence'] = sequence
        row = item['layers'][0]
        row['best_nm'] = value
        row['best_deleted'] = False
        row['delta_nm'] = value - row['original_nm']
        item['candidate']['layer_count'] = 1
        item['candidate']['total_thickness_nm'] = value
        item['candidate']['min_thickness_nm'] = value
        return item

    controller._busy = True
    controller._active_operation_id = 42
    controller._attempt_search_mode = 'reduction'
    controller._attempt_job_id = 'plot-projection'
    original = frame['layers'][0]['original_nm']

    def accept(item):
        controller._on_progress_for_operation(42, FilmOptimizationProgress(
            'running', 0.1, 1, 1, 0.0, reduction_feedback_snapshot=item))

    accept(incumbent(1, original + 20))
    row = controller.reductionFeedbackLayers[0]
    assert row['original_nm'] == original and row['best_nm'] == original + 20
    assert 'current_nm' not in row
    assert controller.reductionFeedbackBarScaleNm == original + 20
    accept(incumbent(2, original + 10))
    assert controller.reductionFeedbackBarScaleNm == original + 20
    assert controller.reductionFeedbackLayers[0]['best_ratio'] == (original + 10) / (original + 20)
    # Wrong-job or older snapshots never replace the accepted frame or scale.
    stale = incumbent(3, original + 100)
    stale['job_id'] = 'different-job'
    accept(stale)
    accept(incumbent(1, original + 100))
    assert controller.reductionFeedbackBarScaleNm == original + 20
    assert controller.reductionFeedbackLayers[0]['best_nm'] == original + 10
    # Exercise the actual cancellation completion path; it must not erase the
    # latest independently audited incumbent, even though it is not applied.
    controller._busy = False
    cancelled = replace(result, base_result=replace(result.base_result,
        state='cancelled', termination_reason='cancelled'), is_final=False)
    finish(review, request, cancelled)
    assert controller.hasReductionFeedback
    assert controller.reductionFeedbackLayers[0]['best_nm'] == original + 10
    assert controller.reductionFeedbackBarScaleNm == original + 20
    # Switching tasks clears the preceding frame.
    assert controller.setSearchMode('fixed_topology')
    assert not controller.hasReductionFeedback
    assert controller.reductionFeedbackLayers == []
    assert controller.reductionFeedbackBarScaleNm == 0
