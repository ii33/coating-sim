"""Conditional numerical HR screens, never certified resource budgets.

All lengths are nm. The thickness relaxation assumes globally constant, lossless
indices; point DP only assumes lossless positive propagating admittances at q.
No function in this module constructs a feasible coating or changes a cap.
"""
from __future__ import annotations

from importlib import import_module
import math
from typing import Callable, Protocol, Sequence, cast

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]


class _LPResult(Protocol):
    success: bool
    message: str
    x: Array | None


class _Linprog(Protocol):
    """The public SciPy HiGHS subset actually used here, no untyped propagation."""

    def __call__(self, c: Array, *, A_ub: Array, b_ub: Array,
                 bounds: tuple[float | None, float | None], method: str) -> _LPResult: ...


def normalize_hr_windows(windows: Sequence[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Pointwise strongest union; discrete lines carry zero integral measure."""
    checked = []
    for lo, hi, r in windows:
        if not all(math.isfinite(v) for v in (lo, hi, r)) or lo <= 0 or hi < lo or not 0 <= r <= 1:
            raise ValueError('invalid HR window')
        if hi > lo and r > 0:
            checked.append((lo, hi, r))
    edges = sorted({v for lo, hi, _ in checked for v in (lo, hi)})
    return [(a, b, max(r for lo, hi, r in checked if lo < b and hi > a))
            for a, b in zip(edges[:-1], edges[1:], strict=True)
            if any(lo < b and hi > a for lo, hi, _ in checked)]


def _logcosh(x: float) -> float:
    return x + math.log1p(math.exp(-2 * x)) - math.log(2) if x > 20 else math.log1p(2 * math.sinh(x / 2) ** 2)


def hr_thickness_screen(indices: Sequence[float], windows: Sequence[tuple[float, float, float]],
                        n0: float, ns: float, *, check: Callable[[], None] = lambda: None,
                        on_solve: Callable[[], None] = lambda: None) -> dict:
    """Small finite-background dual LP. Float witness, NOT outward-certified."""
    linprog = cast(_Linprog, import_module('scipy.optimize').linprog)

    n = np.asarray(indices, dtype=float)
    if n.size == 0 or not np.all(np.isfinite(n)) or np.any(n <= 0) or not all(math.isfinite(v) and v > 0 for v in (n0, ns)):
        raise ValueError('finite positive constant indices required')
    bands = normalize_hr_windows(windows)
    if any(r == 1 for _, _, r in bands):
        return dict(value_nm=None, reason='perfect_positive_width_hr_in_ideal_model',
                    no_finite_thickness_in_ideal_model=True)
    mid = float(n.min()) / 2 + float(n.max()) / 2
    aa = np.asarray(sorted({n0, ns, mid, *n.tolist()}))
    delta = n[None, :] - aa[:, None]
    try:
        # Divide by sqrt(a) BEFORE squaring: delta**2 can falsely underflow
        # to zero while the actual coefficient remains representable.
        with np.errstate(over='raise', under='raise', invalid='raise', divide='raise'):
            C = ((delta / np.sqrt(aa[:, None])) * (math.pi / math.sqrt(2))) ** 2
    except FloatingPointError as exc:
        raise ValueError('numerical_unknown_thickness_coefficient_range') from exc
    if np.any((delta != 0) & (C == 0)):
        raise ValueError('numerical_unknown_nonzero_coefficient_underflow')
    budgets: list[float] = []
    for a in aa:
        check()
        steps = .5 * abs(math.log(a) - math.log(n0)) + .5 * abs(math.log(a) - math.log(ns))
        budgets.append(math.fsum((hi - lo) * _logcosh(max(math.log1p(math.sqrt(r)) - .5 * math.log1p(-r) - steps, 0))
                          for lo, hi, r in bands))
    B = np.asarray(budgets)
    if not np.all(np.isfinite(C)) or not np.all(np.isfinite(B)):
        raise ValueError('nonfinite thickness relaxation')
    if np.any(np.all(delta == 0, axis=1) & (B > 0)):
        # B is not outward-certified. A rounding-sized positive value can
        # occur even at an attainable single-layer peak: never call it infeasible.
        raise ValueError('numerical_unknown_zero_budget_comparison')
    scale = float(np.max(C))
    if scale == 0:
        return dict(value_nm=0., reason='zero_available_and_required_budget',
                    no_finite_thickness_in_ideal_model=False)
    try:
        with np.errstate(over='raise', under='raise', invalid='raise', divide='raise'):
            normalized = C / scale
    except FloatingPointError as exc:
        raise ValueError('numerical_unknown_thickness_normalization_range') from exc
    check()
    on_solve()
    solution = linprog(-B, A_ub=normalized.T, b_ub=np.ones(n.size), bounds=(0, None), method='highs')
    check()
    if not solution.success or solution.x is None:
        raise ValueError(f'thickness relaxation solver failed: {solution.message}')
    raw = np.asarray(solution.x, dtype=float)
    if raw.shape != (len(aa),) or not np.all(np.isfinite(raw)):
        raise ValueError('thickness solver returned invalid dual shape or nonfinite values')
    try:
        with np.errstate(over='raise', under='ignore', invalid='raise', divide='raise'):
            y = np.maximum(raw, 0) / scale
            usage = C.T @ y
            if not np.all(np.isfinite(usage)):
                raise ValueError('nonfinite dual material usage')
            y /= max(1., float(np.max(usage))) * (1 + 1e-10)
            usage = C.T @ y
            value = float(B @ y)
    except FloatingPointError as exc:
        raise ValueError('numerical_unknown_thickness_dual_range') from exc
    if not np.all(np.isfinite(y)) or not np.all(np.isfinite(usage)) or not math.isfinite(value) or np.any(usage > 1 + 2e-14):
        raise ValueError('numerical dual witness failed')
    return dict(value_nm=value, reason='conditional_constant_index_dual_screen',
                backgrounds=aa.tolist(), dual_weights=y.tolist(), dual_material_usage=usage.tolist(),
                no_finite_thickness_in_ideal_model=False, interval_certified=False)


def hr_point_layer_screen(indices: Sequence[float], n0: float, ns: float, angle: float,
                          polarization: str, target: float, limit: int, *,
                          check: Callable[[], None] = lambda: None,
                          record_transitions: Callable[[int], None] = lambda count: None) -> dict:
    """First at-most-N budget not excluded at one q; not sufficient for a band."""
    if type(limit) is not int or limit < 0 or polarization not in ('s', 'p', 'unpolarized'):
        raise ValueError('invalid DP controls')
    if not math.isfinite(angle) or not 0 <= angle < 90 or not 0 <= target <= 1:
        raise ValueError('invalid point condition')
    n = np.asarray([n0, *indices, ns], dtype=float)
    if len(n) < 3 or not np.all(np.isfinite(n)) or np.any(n <= 0):
        raise ValueError('finite positive point indices required')
    h = n0 * math.sin(math.radians(angle))
    # Near-critical cancellation is deliberately not hidden under an eps pad.
    if np.any(n - abs(h) <= 1e-10 * np.maximum(n, 1.)):
        raise ValueError('evanescent_or_numerically_near_critical')
    q = np.sqrt((n - h) * (n + h))
    if not np.all(np.isfinite(q)) or np.any(q <= 0):
        raise ValueError('nonfinite_admittance')
    tables = []
    pols = ('s', 'p') if polarization == 'unpolarized' else (polarization,)
    transitions = 0
    for pol in pols:
        logy = np.log(q) if pol == 's' else 2 * np.log(n) - np.log(q)
        w = .5 * np.abs(logy[:, None] - logy[None, :])
        values = [float(w[0, -1])]
        ending = w[0, 1:-1].copy()
        for count in range(1, limit + 1):
            check()
            if count > 1:
                ending = np.max(ending[:, None] + w[1:-1, 1:-1], axis=0)
                transitions += len(indices) ** 2
                record_transitions(len(indices) ** 2)
            values.append(max(values[-1], float(np.max(ending + w[1:-1, -1]))))
        chi = np.asarray(values)
        padded = chi + 64 * np.finfo(float).eps * (np.arange(limit + 1) + 2) * (1 + np.abs(chi))
        tables.append(np.tanh(padded) ** 2)
    ceiling = np.mean(tables, axis=0)
    possible = np.flatnonzero(ceiling >= target) if target < 1 else np.asarray([], dtype=int)
    first = int(possible[0]) if possible.size else None
    return dict(value_layers=first, excluded_through=first - 1 if first is not None else limit,
                search_limit=limit, exceeds_search_window=first is None,
                ceiling_at_search_limit=float(ceiling[-1]), transitions=transitions,
                no_finite_layers_in_ideal_model=target == 1, interval_certified=False)
