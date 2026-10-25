"""The removed process sensitivity interface has no discovery or dispatch route."""
from copy import deepcopy

import pytest


def test_removed_process_tools_are_not_published_or_dispatchable():
    from coat_sim.core import mcp_contracts as contracts
    from coat_sim.mcp import server
    from coat_sim.agent import runtime, tool_dispatch
    for name in ('plan_film_sensitivity', 'submit_film_sensitivity'):
        assert not hasattr(server, name)
        assert name not in runtime.MAIN_TOOLS
        assert name not in runtime.SUBMIT_TOOLS
        assert name not in runtime.EXECUTION_TOOLS['simulation']
        assert name not in tool_dispatch.STRONG_IDEMPOTENT_TOOLS
    assert not hasattr(contracts, 'FILM_SENSITIVITY_PLAN_INPUT_SCHEMA')
    assert not hasattr(contracts, 'FILM_SENSITIVITY_SUBMIT_INPUT_SCHEMA')
    kinds = contracts.FILM_SYNTHESIS_ESTIMATE_INPUT_SCHEMA['properties']['kind']['enum']
    assert 'film_sensitivity' not in kinds
    assert 'process' in kinds
    assert 'film_ideal_sensitivity' in kinds and 'film_thickness_error_table' in kinds


@pytest.mark.parametrize(('field', 'value'), [
    ('optimization_policy', 'nominal_preserving'),
    ('error_amplitude_semantics', 'standard_deviation'),
    ('errors', []), ('error_model', 'independent'),
    ('robustness_objective', 'linear_sensitivity_norm'), ('scenarios', []),
    ('operation', 'optimize'), ('policy', 'strict_nominal'), ('budgets', []),
])
def test_ordinary_optimization_schema2_rejects_sensitivity_fields(field, value):
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
    from tests.test_film_ideal_sensitivity import problem
    _service, request = problem()
    ordinary = FilmOptimizationRequest(request.design_snapshot, request.design_revision,
        request.variables, request.objectives)
    payload = serialize_request(ordinary)
    assert payload['schema_version'] == 2
    restored = deserialize_request(payload)
    assert isinstance(restored, FilmOptimizationRequest)
    assert serialize_request(restored) == payload
    changed = deepcopy(payload)
    changed[field] = value
    with pytest.raises(ValueError, match='fields'):
        deserialize_request(changed)
