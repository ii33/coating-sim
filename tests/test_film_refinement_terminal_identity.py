"""Raw invalid minimax endpoints never borrow accepted-point identity or t."""
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_refinement import refine_synthesis_full_minimax
from tests.test_film_minimax_vector_bounds import _problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.mark.parametrize('endpoint', [(.05, .05, .77), (.07, .02, .88), (.0005, .02, .99)])
@pytest.mark.parametrize('solver_success', [True, False])
def test_invalid_raw_endpoint_and_valid_accepted_iterate_keep_distinct_evidence(
        monkeypatch: pytest.MonkeyPatch, endpoint: tuple[float, float, float],
        solver_success: bool) -> None:
    request, initial, kwargs, calls, archived, _events = _problem(total=7.)
    accepted = np.array([.01, .02, .03])
    raw = np.asarray(endpoint)

    def minimize(_fun: Any, _start: Any, **options: Any) -> SimpleNamespace:
        options['constraints'][0]['fun'](accepted)
        options['callback'](accepted)
        assert calls == [(1., 2.)]
        assert np.all(options['constraints'][0]['fun'](raw) < -999_999.)
        options['callback'](raw)
        assert calls == [(1., 2.)]
        return SimpleNamespace(x=raw, success=solver_success,
            status=0 if solver_success else 9, nit=2, message='finite physically invalid endpoint')

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    terminals: list[dict[str, Any]] = []
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': 1., 'b': 2.}, terminal_callback=terminals.append, **kwargs)
    assert calls == [(1., 2.)] and len(archived) == 1
    assert best['design']['front_layers'][0]['thickness']['value'] == 1.
    assert terminals == [best]
    accepted_hash = physical_design_hash(best['design'])
    raw_design = deepcopy(initial['design'])
    for row, coordinate in zip(raw_design['front_layers'], endpoint[:-1], strict=True):
        row['thickness'] = dict(value=coordinate * 100., unit='nm')
    raw_hash = physical_design_hash(raw_design)
    assert raw_hash != accepted_hash
    assert not detail['success']
    assert detail['termination'] == ('solver_failure' if solver_success else 'iteration_limit')
    assert not detail['terminal_full_grid_epigraph_feasible']
    assert detail['terminal_role'] == 'last_accepted_iterate_or_initial_after_invalid_solver_endpoint'
    assert detail['terminal_physical_hash'] == accepted_hash
    assert detail['optimizer_t'] == .03
    assert detail['terminal_signed_max'] == pytest.approx(.03)
    raw_evidence = detail['solver_terminal']
    assert raw_evidence['role'] == 'actual_solver_endpoint'
    assert raw_evidence['physical_hash'] == raw_hash
    assert raw_evidence['scaled_thickness_coordinates'] == list(endpoint[:-1])
    assert raw_evidence['optimizer_t'] == endpoint[-1]
    assert raw_evidence['signed_max'] is None
    assert not raw_evidence['physical_valid']
    assert not raw_evidence['nominal_pass']
    assert not raw_evidence['local_stationary']
    retained = detail['retained_terminal']
    assert retained['physical_hash'] == accepted_hash
    assert retained['role'] == detail['terminal_role']
    assert retained['physical_valid']
    assert retained['optimizer_t'] == .03
    assert retained['signed_max'] == pytest.approx(.03)
    assert retained['epigraph_feasible']
    assert not retained['local_stationary']
    assert detail['returned_best']['physical_hash'] == accepted_hash
    assert not detail['returned_best']['is_solver_terminal']
    assert detail['returned_best']['role'] == 'best_training_trial'


def test_invalid_endpoint_without_callback_retains_initial_without_inventing_accepted_t(
        monkeypatch: pytest.MonkeyPatch) -> None:
    request, initial, kwargs, calls, archived, _events = _problem(total=7.)
    raw = np.array([.05, .05, .77])
    monkeypatch.setattr('scipy.optimize.minimize',
        lambda *_args, **_kwargs: SimpleNamespace(x=raw, success=True, status=0,
                                                nit=1, message='no valid callback'))
    terminals: list[dict[str, Any]] = []
    best, detail = refine_synthesis_full_minimax(request, initial,
        terminal_callback=terminals.append, **kwargs)
    assert best is initial and terminals == [initial]
    assert not calls and not archived
    assert detail['terminal_physical_hash'] == physical_design_hash(initial['design'])
    assert detail['optimizer_t'] is None
    assert detail['retained_terminal']['optimizer_t'] is None
    assert detail['terminal_signed_max'] == pytest.approx(.07)
    assert detail['solver_terminal']['optimizer_t'] == .77
    assert detail['solver_terminal']['physical_hash'] != detail['terminal_physical_hash']
    assert not detail['solver_terminal']['local_stationary']
    assert not detail['returned_best']['is_solver_terminal']
