"""Adversarial representation-cap tests for front-only mathematical needles."""
from copy import deepcopy

import pytest

from coat_sim.core.film_design import MAX_LAYERS, validate_design
from coat_sim.core.film_synthesis_batch_needle import scan_batch_needles
from tests.test_film_synthesis_batch_needle import _problem, _row

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize('front_count', [0, 1])
def test_full_representation_cap_reports_skips_without_touching_preserved_back_rows(front_count):
    request, design, frozen = _problem()
    design['front_layers'] = design['front_layers'][:front_count]
    design['back_layers'] = [_row(f'back-{i}', thickness=1.)
                            for i in range(MAX_LAYERS - front_count)]
    validate_design(design)
    before = deepcopy(design)
    def evaluate(*args, **kwargs):
        raise AssertionError('a representation-oversized probe must not reach physical evaluation')
    scan = scan_batch_needles(design, frozen, request.candidate_materials[:1],
        evaluate=evaluate, position_samples=1, max_probes=1)
    assert not scan.complete and scan.stop_reason == 'restricted_or_resource_skips'
    assert scan.skipped_probe_count == (3 if front_count else 1)
    assert not scan.proposals and not scan.selected
    assert scan.probe_count == 0
    assert design == before


def test_one_remaining_global_row_allows_interfaces_but_not_two_insertions_or_host_split():
    request, design, frozen = _problem()
    design['back_layers'] = [_row(f'back-{i}', thickness=1.) for i in range(MAX_LAYERS - 2)]
    before_back = deepcopy(design['back_layers'])
    calls = []
    def evaluate(candidate, *, variable_ids=(), zero_ids=()):
        assert len(candidate['front_layers']) + len(candidate['back_layers']) == MAX_LAYERS
        assert candidate['back_layers'] == before_back
        assert len(zero_ids) == 1
        calls.append(tuple(variable_ids))
        return dict(gradient_by_layer_id={identifier: -1. for identifier in variable_ids})
    scan = scan_batch_needles(design, frozen, request.candidate_materials[:1],
        evaluate=evaluate, position_samples=1, valley_refinement_rounds=0, max_needles=2)
    assert scan.probe_count == len(calls) == 2
    assert scan.skipped_probe_count == 1
    assert len(scan.selected) == 1
    assert all(proposal.site['kind'] == 'interface' for proposal in scan.proposals)
    assert not scan.complete
    assert design['back_layers'] == before_back
