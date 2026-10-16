"""Bounded, derivative-free solvers for normalized film parameters.

The optimization service owns candidate archiving, exact lexicographic scoring,
evaluation budgets, and cancellation.  This module only supplies scalar
proposals to the caller's ``evaluate`` function and reports completed solver
iterations.  It deliberately does not implement the legacy baseline search.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np


Objective = Callable[[np.ndarray], float]


class SolverStop(Exception):
    """Signal that the outer evaluation budget or cancellation stopped a run.

    ``run_solver`` catches this control-flow exception and returns its reason
    unchanged in :class:`SolverOutcome`.  This keeps SciPy from interpreting
    cancellation as an objective failure while giving the caller a partial
    outcome.
    """

    def __init__(self, reason: str = "stopped") -> None:
        self.reason = str(reason) or "stopped"
        super().__init__(self.reason)


@dataclass(frozen=True)
class SolverOutcome:
    """Completed outer iterations and why the solver returned."""

    iterations: int
    reason: str


def _vectors(
    initial: Sequence[float], lower: Sequence[float], upper: Sequence[float]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x0 = np.asarray(initial, dtype=float)
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    if x0.ndim != 1 or lo.ndim != 1 or hi.ndim != 1:
        raise ValueError("initial, lower, and upper must be one-dimensional")
    if x0.size == 0 or lo.shape != x0.shape or hi.shape != x0.shape:
        raise ValueError("initial, lower, and upper must have the same nonzero size")
    if not (np.all(np.isfinite(x0)) and np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        raise ValueError("initial values and bounds must be finite")
    if np.any(lo > hi):
        raise ValueError("lower bounds must not exceed upper bounds")
    return np.clip(x0, lo, hi), lo, hi


def _scalar_objective(evaluate: Objective, lower: np.ndarray, upper: np.ndarray):
    def objective(point: Sequence[float]) -> float:
        bounded = np.clip(np.asarray(point, dtype=float), lower, upper)
        return float(evaluate(bounded.copy()))

    return objective


def _bounded_powell(
    objective: Objective,
    initial: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    iteration_budget: int,
    min_step_fraction: float,
    initial_step_fraction: float,
    on_iteration: Callable[[], None],
):
    from scipy.optimize import minimize

    local_iterations = 0

    def callback(_point: np.ndarray) -> bool:
        nonlocal local_iterations
        local_iterations += 1
        on_iteration()
        return local_iterations >= iteration_budget

    step = max(float(initial_step_fraction), float(min_step_fraction), 1e-12)
    result = minimize(
        objective,
        initial,
        method="Powell",
        bounds=list(zip(lower, upper)),
        callback=callback,
        options={
            "maxiter": int(iteration_budget),
            "xtol": max(float(min_step_fraction), 1e-12),
            "ftol": max(float(min_step_fraction), 1e-12),
            "direc": np.eye(initial.size, dtype=float) * step,
        },
    )
    reason = "iteration_limit" if local_iterations >= iteration_budget else (
        "converged" if bool(result.success) else "solver_stopped"
    )
    return local_iterations, reason


def _bounded_nelder_mead(
    objective: Objective,
    initial: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    iteration_budget: int,
    min_step_fraction: float,
    initial_step_fraction: float,
    on_iteration: Callable[[], None],
) -> tuple[int, str]:
    from scipy.optimize import minimize

    local_iterations = 0
    simplex = np.tile(initial, (initial.size + 1, 1))
    for axis, width in enumerate(upper - lower):
        delta = min(max(float(initial_step_fraction), float(min_step_fraction)), width)
        if delta <= 0.0:
            continue
        if initial[axis] + delta <= upper[axis]:
            simplex[axis + 1, axis] += delta
        else:
            simplex[axis + 1, axis] = max(lower[axis], initial[axis] - delta)

    def callback(_point: np.ndarray) -> bool:
        nonlocal local_iterations
        local_iterations += 1
        on_iteration()
        return local_iterations >= iteration_budget

    result = minimize(
        objective,
        initial,
        method="Nelder-Mead",
        bounds=list(zip(lower, upper)),
        callback=callback,
        options={
            "maxiter": int(iteration_budget),
            "xatol": max(float(min_step_fraction), 1e-12),
            "fatol": max(float(min_step_fraction), 1e-12),
            "initial_simplex": simplex,
        },
    )
    reason = "iteration_limit" if local_iterations >= iteration_budget else (
        "converged" if bool(result.success) else "solver_stopped"
    )
    return local_iterations, reason


def _differential_evolution(
    objective: Objective,
    initial: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    iteration_budget: int,
    seed: int,
    population_size: int,
    min_step_fraction: float,
    on_iteration: Callable[[], None],
) -> tuple[int, str]:
    from scipy.optimize import differential_evolution

    local_iterations = 0
    rng = np.random.default_rng(seed)
    # SciPy requires an explicit initial population to contain at least five
    # members.  The default contract size is 12; clamp smaller requests to the
    # library's minimum while keeping the requested size for normal inputs.
    actual_population_size = max(5, int(population_size))
    spans = upper - lower
    population = lower + rng.random((actual_population_size, initial.size)) * spans
    population[0] = initial

    def callback(_point: np.ndarray, _convergence: float) -> bool:
        nonlocal local_iterations
        local_iterations += 1
        on_iteration()
        return local_iterations >= iteration_budget

    result = differential_evolution(
        objective,
        bounds=list(zip(lower, upper)),
        strategy="best1bin",
        maxiter=int(iteration_budget),
        popsize=actual_population_size,
        tol=max(float(min_step_fraction), 1e-12),
        atol=0.0,
        mutation=(0.5, 1.0),
        recombination=0.7,
        seed=seed,
        callback=callback,
        init=population,
        polish=False,
        updating="immediate",
        workers=1,
    )
    reason = "iteration_limit" if local_iterations >= iteration_budget else (
        "converged" if bool(result.success) else "solver_stopped"
    )
    return local_iterations, reason


def _simulated_annealing(
    objective: Objective,
    initial: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    iteration_budget: int,
    seed: int,
    initial_step_fraction: float,
    min_step_fraction: float,
    on_iteration: Callable[[], None],
) -> tuple[int, str]:
    rng = np.random.default_rng(seed)
    current = initial.copy()
    current_value = objective(current)
    spans = upper - lower
    local_iterations = 0
    base_temperature = max(abs(current_value), 1.0) * 0.1

    for index in range(iteration_budget):
        step = max(
            float(min_step_fraction),
            float(initial_step_fraction) * (0.95 ** index),
        )
        proposal = current + rng.normal(0.0, step, size=current.size)
        # Reflect at each bound so proposals remain in range without piling up
        # on a boundary through repeated clipping.
        for axis, span in enumerate(spans):
            if span <= 0.0:
                proposal[axis] = lower[axis]
                continue
            offset = (proposal[axis] - lower[axis]) % (2.0 * span)
            proposal[axis] = lower[axis] + (offset if offset <= span else 2.0 * span - offset)

        proposal_value = objective(proposal)
        temperature = base_temperature * (0.01 ** (index / max(iteration_budget, 1)))
        delta = proposal_value - current_value
        if delta <= 0.0 or rng.random() < np.exp(-delta / max(temperature, 1e-12)):
            current = proposal
            current_value = proposal_value
        local_iterations += 1
        on_iteration()

    return local_iterations, "iteration_limit"


def run_solver(
    method: str,
    initial: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
    evaluate: Objective,
    *,
    max_iterations: int,
    seed: int,
    multistart_count: int,
    population_size: int,
    initial_step_fraction: float,
    min_step_fraction: float,
) -> SolverOutcome:
    """Run one bounded derivative-free method over normalized parameters.

    Each candidate is clipped to the supplied bounds and passed to ``evaluate``
    as a fresh NumPy array. ``max_iterations`` counts Powell/Nelder-Mead outer
    iterations, differential-evolution generations, simulated-annealing
    proposals, or the total Powell iterations across all multistarts. An
    outer ``SolverStop`` is caught and returned with its original reason.
    """

    supported = {
        "powell",
        "nelder_mead",
        "multistart",
        "differential_evolution",
        "simulated_annealing",
    }
    if method not in supported:
        raise ValueError(f"unsupported derivative-free solver: {method!r}")

    x0, lo, hi = _vectors(initial, lower, upper)
    budget = max(0, int(max_iterations))
    completed_iterations = 0

    def count_iteration() -> None:
        nonlocal completed_iterations
        completed_iterations += 1

    if budget == 0:
        return SolverOutcome(iterations=0, reason="iteration_limit")

    objective = _scalar_objective(evaluate, lo, hi)
    try:
        if method == "powell":
            iterations, reason = _bounded_powell(
                objective,
                x0,
                lo,
                hi,
                iteration_budget=budget,
                min_step_fraction=min_step_fraction,
                initial_step_fraction=initial_step_fraction,
                on_iteration=count_iteration,
            )
        elif method == "nelder_mead":
            iterations, reason = _bounded_nelder_mead(
                objective,
                x0,
                lo,
                hi,
                iteration_budget=budget,
                min_step_fraction=min_step_fraction,
                initial_step_fraction=initial_step_fraction,
                on_iteration=count_iteration,
            )
        elif method == "differential_evolution":
            iterations, reason = _differential_evolution(
                objective,
                x0,
                lo,
                hi,
                iteration_budget=budget,
                seed=int(seed),
                population_size=int(population_size),
                min_step_fraction=min_step_fraction,
                on_iteration=count_iteration,
            )
        elif method == "simulated_annealing":
            iterations, reason = _simulated_annealing(
                objective,
                x0,
                lo,
                hi,
                iteration_budget=budget,
                seed=int(seed),
                initial_step_fraction=initial_step_fraction,
                min_step_fraction=min_step_fraction,
                on_iteration=count_iteration,
            )
        else:  # multistart
            rng = np.random.default_rng(seed)
            starts = [x0]
            for _ in range(max(1, int(multistart_count)) - 1):
                starts.append(lo + rng.random(x0.size) * (hi - lo))

            all_converged = True
            reason = "converged"
            for index, start in enumerate(starts):
                remaining = budget - completed_iterations
                if remaining <= 0:
                    reason = "iteration_limit"
                    break
                starts_left = len(starts) - index
                allocation = max(1, (remaining + starts_left - 1) // starts_left)
                local_count, local_reason = _bounded_powell(
                    objective,
                    start,
                    lo,
                    hi,
                    iteration_budget=allocation,
                    min_step_fraction=min_step_fraction,
                    initial_step_fraction=initial_step_fraction,
                    on_iteration=count_iteration,
                )
                all_converged = all_converged and local_reason == "converged"
                if completed_iterations >= budget:
                    reason = "iteration_limit"
                    break
            else:
                reason = "converged" if all_converged else "solver_stopped"
    except SolverStop as stop:
        return SolverOutcome(
            iterations=min(completed_iterations, budget),
            reason=stop.reason,
        )

    return SolverOutcome(
        iterations=min(completed_iterations, budget),
        reason=reason,
    )


__all__ = ["SolverOutcome", "SolverStop", "run_solver"]
