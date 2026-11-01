"""Classify session pauses separately from demonstrated search completion.

This is an outer presentation contract, never a local convergence criterion or
proof that all continuous film designs are infeasible.
"""
from __future__ import annotations

from typing import Any, Mapping


def until_target_summary(*, state: str, reason: str, goal_met: bool,
                         hybrid: Mapping[str, Any], has_local_optimum: bool,
                         checkpoint_available: bool) -> dict[str, Any]:
    if type(state) is not str or type(reason) is not str or type(goal_met) is not bool:
        raise ValueError('search status requires typed state, reason and goal evidence')
    session = hybrid.get('continuation_session', {})
    exhausted = (reason == 'continuation_frontier_exhausted'
        and isinstance(session, Mapping) and session.get('frontier_exhausted') is True
        and hybrid.get('resource_limited') is False)
    if state == 'cancelled':
        status = 'cancelled'
    elif reason in {'resource_limit', 'continuation_resource_limit', 'local_session_resource_guard'}:
        status = 'blocked_resource'
    elif reason == 'no_admissible_search_step':
        status = 'blocked_search'
    elif reason in {'local_session_solver_failure', 'local_session_terminal_evidence_unavailable',
                    'local_session_iteration_limit', 'local_session_stage_budget'}:
        status = 'interrupted_error'
    elif state == 'failed':
        status = 'interrupted_error'
    elif goal_met:
        status = 'targets_met'
    elif exhausted:
        status = 'declared_frontier_exhausted'
    elif reason.startswith('local_session_') and has_local_optimum:
        status = 'paused_local'
    elif reason == 'continuation_quantum_no_new_candidate':
        status = 'paused_no_new_candidate'
    else:
        status = 'paused_unresolved'
    return dict(enabled=True, status=status,
        search_complete=status in {'targets_met', 'declared_frontier_exhausted'},
        goal_met=goal_met, declared_frontier_exhausted=bool(exhausted),
        global_infeasibility_proven=False,
        continuation_checkpoint_available=checkpoint_available, termination_reason=reason)
