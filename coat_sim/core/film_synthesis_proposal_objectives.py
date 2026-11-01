"""Reflective starting-structure hints for transmission rejection windows.

These private objectives are ONLY for constructing structural proposals.
They are not equivalent optical constraints in absorbing systems. Scoring,
gradients, local refinement and independent audit must use the original request.
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, TypedDict

from .film_objective_criteria import FilmObjectiveConstraint

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest


PROPOSAL_OBJECTIVE_POLICY = "transmission_rejection_reflective_seed_v1"


class ProposalObjectiveHint(TypedDict):
    objective_index: int
    original_metric: str
    original_upper_bound: float
    proposal_metric: str
    proposal_lower_bound: float
    acceptance_equivalence: bool


def reflective_proposal_request(
    request: FilmSynthesisRequest,
) -> tuple[FilmSynthesisRequest, tuple[ProposalObjectiveHint, ...]]:
    """Keep target indices; replace only positive-weight pointwise T ceilings.

    The 0.5 ceiling is a structural-family selection convention, not a physical
    feasibility threshold. Original pass windows and all source optics remain
    intact. No original object is mutated and no optical work is performed.
    """
    objectives = []
    hints: list[ProposalObjectiveHint] = []
    for index, objective in enumerate(request.objectives):
        bound = objective.pointwise
        upper = bound.upper_bound
        if (objective.metric == "T" and objective.weight > 0 and bound.enabled
                and bound.mode == "range" and upper is not None and 0 <= upper <= .5
                and (bound.lower_bound is None or bound.lower_bound <= 0)):
            lower = 1.0 - upper
            objectives.append(replace(objective, metric="R",
                pointwise=FilmObjectiveConstraint(lower_bound=lower, upper_bound=None,
                    strict_lower=bound.strict_upper),
                mean=FilmObjectiveConstraint(enabled=False)))
            hints.append(ProposalObjectiveHint(objective_index=index,
                original_metric="T", original_upper_bound=upper,
                proposal_metric="R", proposal_lower_bound=lower,
                acceptance_equivalence=False))
        else:
            objectives.append(objective)
    if not hints:
        return request, ()
    return replace(request, objectives=tuple(objectives)), tuple(hints)
