"""Real parent-loop handoff ordering with small synthetic-material optical grids."""

from dataclasses import replace
import json

import pytest

from coat_sim.core.film_auto_synthesis import synthesize_film_design
from coat_sim.core.film_synthesis_progress import HybridProgressPolicy
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _accepted_stall(monkeypatch):
    from importlib import import_module

    scipy_optimize = import_module("scipy.optimize")
    original = scipy_optimize.minimize

    def minimize(fun, x0, **kwargs):
        if kwargs.get("method") == "L-BFGS-B":
            fun(x0)
            kwargs["callback"](x0)
            pytest.fail("opt-in one-window stagnation must hand off before next SSE step")
        return original(fun, x0, **kwargs)

    monkeypatch.setattr(scipy_optimize, "minimize", minimize)


def test_real_parent_handoff_preserves_identity_skips_deletion_and_uses_existing_minimax(
    monkeypatch,
):
    _accepted_stall(monkeypatch)
    service, request = setup_problem(upper=0.0, low=450.0, high=700.0, samples=5)
    request = replace(
        request,
        search_strategy="hybrid",
        max_cycles=1,
        max_evaluations=180,
        max_layers=8,
        enable_thickness_reduction=False,
        objective_progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
    )
    result = synthesize_film_design(service, request)
    hybrid = result.performance["search"]["hybrid"]
    handoffs = hybrid.get("objective_progress_handoffs", [])
    assert handoffs, result.termination_reason
    handoff = handoffs[0]
    assert handoff["reason"] == "declared_stagnation"
    assert (
        hybrid["last_full_minimax"]["initial_physical_hash"] == handoff["trigger"]["physical_hash"]
    )
    stages = [r["stage"] for r in hybrid["stages"] if r["cycle"] == handoff["cycle"]]
    assert "deletion_and_same_reference_merge" not in stages
    assert "full_original_slsqp_minimax" in stages
    messages = [r for r in result.trace if r.stage == "objective_progress_handoff"]
    assert (
        messages
        and json.loads(messages[0].message)["trigger"]["physical_hash"]
        == handoff["trigger"]["physical_hash"]
    )
    assert result.state != "failed"


def _request_case(**changes):
    service, request = setup_problem(upper=0.0, low=450.0, high=700.0, samples=5)
    settings = dict(
        search_strategy="hybrid",
        max_cycles=1,
        max_evaluations=180,
        max_layers=8,
        enable_thickness_reduction=False,
        structure_search_seed=0,
        objective_progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
    )
    settings.update(changes)
    return service, replace(request, **settings)


@pytest.mark.parametrize("stop_at", ["hybrid_sse_accepted_iterate", "objective_progress_handoff"])
@pytest.mark.parametrize("kind", ["cancel", "deadline"])
def test_real_parent_stop_at_commit_or_handoff_admits_no_minimax_or_later_optics(
    monkeypatch, stop_at, kind
):
    from threading import Event
    from coat_sim.core import film_auto_synthesis as synthesis
    from coat_sim.core import film_synthesis_refinement as refinement

    _accepted_stall(monkeypatch)
    cancel = Event()
    now = [0.0]
    monkeypatch.setattr(synthesis, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        refinement,
        "refine_synthesis_full_minimax",
        lambda *a, **k: pytest.fail("stop must win before minimax"),
    )
    service, request = _request_case(max_seconds=10.0)
    events = []
    stopped_evaluations = []

    def observe(row):
        events.append(row)
        if row.stage == stop_at:
            stopped_evaluations.append(row.evaluations)
            if kind == "cancel":
                cancel.set()
            else:
                now[0] = 100.0

    result = synthesize_film_design(
        service, request, cancel_event=cancel, progress_callback=observe
    )
    assert stopped_evaluations
    assert result.evaluations == stopped_evaluations[0]
    assert result.candidate_design is not None
    assert result.candidate_provenance["training_complete"]
    assert (
        result.state == "cancelled"
        if kind == "cancel"
        else result.termination_reason == "time_limit"
    )
    tail = events[next(i for i, row in enumerate(events) if row.stage == stop_at) + 1 :]
    assert not any(
        row.stage in ("validation", "hybrid_sse_trial", "deletion", "hybrid_full_minimax_trial")
        for row in tail
    )


def test_real_parent_resource_denied_handoff_keeps_archive_and_no_solver_dispatch(monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis
    from coat_sim.core import film_synthesis_refinement as refinement

    _accepted_stall(monkeypatch)
    original = synthesis._synthesis_working_set_estimate_bytes

    def estimate(request, layers, **kwargs):
        return 1024**3 if kwargs.get("epigraph") else original(request, layers, **kwargs)

    monkeypatch.setattr(synthesis, "_synthesis_working_set_estimate_bytes", estimate)
    monkeypatch.setattr(
        refinement,
        "refine_synthesis_full_minimax",
        lambda *a, **k: pytest.fail("resource denied before minimax"),
    )
    service, request = _request_case(max_working_set_bytes=512 * 1024**2)
    result = synthesize_film_design(service, request)
    hybrid = result.performance["search"]["hybrid"]
    assert hybrid["objective_progress_handoffs"]
    assert "last_full_minimax" not in hybrid
    assert any(row["full_epigraph"] for row in hybrid["resource_skips"])
    assert result.candidate_design is not None and result.state != "failed"
    assert result.performance["search"]["design_archive"]["entries"]
    assert not any(
        row["stage"] in ("deletion_and_same_reference_merge", "terminal_champion_cleanup")
        for row in hybrid["stages"]
    )


def test_null_policy_does_not_add_refiner_keyword_or_change_old_deletion_path(monkeypatch):
    from coat_sim.core import film_synthesis_refinement as refinement

    original = refinement.refine_synthesis_explorer
    seen = []

    def wrapper(*args, **kwargs):
        seen.append(kwargs)
        assert "progress_policy" not in kwargs
        return original(*args, **kwargs)

    monkeypatch.setattr(refinement, "refine_synthesis_explorer", wrapper)
    service, request = _request_case(objective_progress_policy=None)
    result = synthesize_film_design(service, request)
    hybrid = result.performance["search"]["hybrid"]
    assert seen
    assert "objective_progress_handoffs" not in hybrid
    assert any(row["stage"] == "deletion_and_same_reference_merge" for row in hybrid["stages"])
    assert not any(row.stage == "objective_progress_handoff" for row in result.trace)
    assert result.evaluations <= request.max_evaluations
