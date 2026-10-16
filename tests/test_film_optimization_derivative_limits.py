"""Analytic film paths retain allocation failures, without a condition quota."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.core import film_optimization_derivatives as derivatives
from tests.film_structure_fixtures import fixture

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


class _ReachedVariableValidation(Exception):
    pass


def test_analytic_request_crosses_old_condition_limit_without_allocating(
    tmp_path: Any, monkeypatch: Any,
) -> None:
    service, _library, design = fixture(tmp_path)
    settings = {"wavelength_min_nm": 400.0, "wavelength_max_nm": 700.0,
                "samples": 250001, "angle_deg": 0.0,
                "polarization": "unpolarized"}
    frozen = service.freeze_design(design)
    # Four media would previously exceed 1,000,000 conditions. No wavelength
    # or Jacobian array is allocated, and the next validation is a sentinel.
    monkeypatch.setattr(derivatives.np, "linspace",
                        lambda *_args: SimpleNamespace(size=250001))

    def reached(_variable: Any) -> Any:
        raise _ReachedVariableValidation

    monkeypatch.setattr(derivatives, "_variable_fields", reached)
    with pytest.raises(_ReachedVariableValidation):
        derivatives.simulate_with_thickness_jacobian(
            design, frozen, [object()], settings)


def test_analytic_request_keeps_real_allocation_error(
    tmp_path: Any, monkeypatch: Any,
) -> None:
    service, _library, design = fixture(tmp_path)
    frozen = service.freeze_design(design)

    def allocation_failure(*_args: Any) -> Any:
        raise MemoryError("controlled wavelength allocation failure")

    monkeypatch.setattr(derivatives.np, "linspace", allocation_failure)
    with pytest.raises(MemoryError, match="controlled wavelength allocation failure"):
        derivatives.simulate_with_thickness_jacobian(
            design, frozen, [], {"samples": 250001})


def test_failed_analytic_candidate_keeps_full_multi_angle_penalty_shape(
    monkeypatch: Any,
) -> None:
    """Removing the quota must retain the residual-size statistic used on failure."""
    from dataclasses import replace

    import numpy as np
    from importlib import import_module

    scipy_optimize = import_module("scipy.optimize")

    from coat_sim.core.film_optimization import optimize_film_design
    from tests.test_film_optimization_cache_chunks import MemoryService, make_request

    calls: list[float] = []

    def derivative(candidate: Any, _materials: Any, _variables: Any,
                   settings: Any) -> dict[str, Any]:
        fraction = (candidate["front_layers"][0]["thickness"]["value"] - 50.0) / 100.0
        calls.append(fraction)
        waves = np.linspace(settings["wavelength_min_nm"],
                            settings["wavelength_max_nm"], settings["samples"])
        jacobian = np.full((len(waves), 1), np.nan if fraction < 0.75 else 0.001)
        return {"wavelength_nm": waves, "R": np.full(len(waves), 0.8),
                "jacobian_R": jacobian}

    def least_squares(fun: Any, _x0: Any, *, jac: Any, callback: Any,
                      **_kwargs: Any) -> Any:
        del callback
        point = np.array([0.5])
        residual = fun(point)
        jacobian = jac(point)
        assert residual.shape == (6,)  # three wavelengths times both angles
        assert jacobian.shape == (6, 1)
        assert np.all(residual == 1e6)
        assert np.all(jacobian == 0)
        return SimpleNamespace(success=False, status=0)

    monkeypatch.setattr(derivatives, "simulate_with_thickness_jacobian", derivative)
    monkeypatch.setattr(scipy_optimize, "least_squares", least_squares)
    request = make_request(samples=3, target=1.0, solver="trf", max_evaluations=5,
                           thickness=150.0, high=150.0)
    request = replace(request, objectives=(replace(request.objectives[0],
                                                 angles_deg=(0.0, 30.0)),))
    result = optimize_film_design(MemoryService(), request)
    assert calls == [1.0, 1.0, 0.5]
    assert result.evaluations == 3
    assert result.failed_evaluations == 1
    assert result.best_thickness_nm["memory-layer"] == pytest.approx(150.0)
