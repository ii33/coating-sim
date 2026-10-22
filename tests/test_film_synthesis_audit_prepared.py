"""Source-context errors precede preparation, without bypassing frozen integrity."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from threading import Event
from typing import Any

import pytest

from coat_sim.core import film_optimization_structure as structure
from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_optimization_derivatives import _PreparedMaterialData
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _rehash(payload: dict[str, Any]) -> None:
    value = {key: item for key, item in payload.items() if key != "sha256"}
    payload["sha256"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf8")).hexdigest()


def _active_request():
    service, request = setup_problem(low=520., high=580., samples=3)
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [dict(id="front", material_id="high", optical_set_id="nk",
                                  thickness=dict(value=60., unit="nm"), note="audit contract")]
    design["incident_medium"] = dict(kind="material", material_id="low", optical_set_id="nk")
    payload = service.freeze_design(design, dict(
        wavelength_min_nm=520., wavelength_max_nm=580., samples=3))
    return replace(request, environment=design, frozen_material_snapshot=payload), design, payload


@pytest.fixture
def no_physics(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    preparations: list[int] = []
    original = audit._PreparedStructureEvaluator

    def prepare(*args: Any, **kwargs: Any) -> Any:
        preparations.append(1)
        return original(*args, **kwargs)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invalid source/snapshot must not enter physical evaluation")

    monkeypatch.setattr(audit, "_PreparedStructureEvaluator", prepare)
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", forbidden)
    return preparations


@pytest.mark.parametrize("material", ["glass", "high", "low"])
@pytest.mark.parametrize("failure", ["material_missing", "optical_set_missing", "unknown_k", "invalid_record"])
@pytest.mark.parametrize("repair_digest", [False, True])
def test_active_source_context_precedes_preparation_for_both_digest_states(
    material: str, failure: str, repair_digest: bool, no_physics: list[int],
) -> None:
    request, design, payload = _active_request()
    if failure == "material_missing":
        payload["materials"] = [row for row in payload["materials"] if row["id"] != material]
    else:
        row = next(row for row in payload["materials"] if row["id"] == material)
        if failure == "optical_set_missing":
            row["optical_sets"] = []
        elif failure == "unknown_k":
            row["optical_sets"][0].pop("k")
        else:
            row["optical_sets"][0]["n"] = []
    if repair_digest:
        _rehash(payload)
    with pytest.raises(ValueError) as error:
        audit.audit_frozen_synthesis_candidate(request, design)
    message = str(error.value)
    assert f"material={material}, optical_set=nk" in message
    assert "[520, 580] nm" in message
    if failure == "unknown_k":
        assert "wavelength=520 nm" in message
    assert no_physics == []


@pytest.mark.parametrize("repair_digest", [False, True])
def test_partial_k_domain_has_wavelength_context_before_preparation(
    repair_digest: bool, no_physics: list[int],
) -> None:
    request, design, payload = _active_request()
    optical = next(row for row in payload["materials"] if row["id"] == "glass")["optical_sets"][0]
    optical.clear()
    optical.update(id="nk", name="known restricted k", kind="cauchy",
                   parameters={"A": 1.5, "B": 0., "C": 0.}, wavelength_unit="nm",
                   wavelength_min=450., wavelength_max=700.,
                   k_data={"wavelength_unit": "nm", "wavelength_min": 540., "wavelength_max": 560.,
                           "wavelengths": [540., 560.], "k": [.01, .02]})
    if repair_digest:
        _rehash(payload)
    with pytest.raises(ValueError, match=r"material=glass, optical_set=nk, wavelength=520"):
        audit.audit_frozen_synthesis_candidate(request, design)
    assert no_physics == []


@pytest.mark.parametrize("corruption", ["digest", "missing_digest", "valid_source_tamper"])
def test_valid_sources_still_require_matching_digest_before_any_physics(
    corruption: str, no_physics: list[int],
) -> None:
    request, design, payload = _active_request()
    if corruption == "digest":
        payload["sha256"] = "0" * 64
    elif corruption == "missing_digest":
        payload.pop("sha256")
    else:
        payload["materials"][0]["optical_sets"][0]["n"][0] += .01
    with pytest.raises(ValueError, match="snapshot digest does not match"):
        audit.audit_frozen_synthesis_candidate(request, design)
    assert no_physics == [1]
    proxy = structure.FilmStructureOptimizationRequest(structure.FilmOptimizationRequest(
        request.environment, request.design_revision, (), request.objectives,
        frozen_material_snapshot=payload))
    with pytest.raises(ValueError, match="snapshot digest does not match"):
        structure._PreparedStructureEvaluator(proxy)


def test_valid_audit_prepares_once_and_counts_every_physical_call(monkeypatch: pytest.MonkeyPatch) -> None:
    service, request = setup_problem(low=520., high=580., samples=3)
    request = freeze_film_synthesis_request(service, request)
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [dict(id="front", material_id="high", optical_set_id="nk",
                                  thickness=dict(value=60., unit="nm"), note="audit contract")]
    preparations: list[int] = []
    calls: list[int] = []
    original_prepare = audit._PreparedStructureEvaluator
    original_physical = structure.simulate_with_thickness_jacobian

    def prepare(*args: Any, **kwargs: Any) -> Any:
        preparations.append(1)
        return original_prepare(*args, **kwargs)

    def physical(*args: Any, **kwargs: Any) -> Any:
        assert type(args[1]) is _PreparedMaterialData
        calls.append(1)
        return original_physical(*args, **kwargs)

    monkeypatch.setattr(audit, "_PreparedStructureEvaluator", prepare)
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", physical)
    value = audit.audit_frozen_synthesis_candidate(request, design)
    assert preparations == [1]
    assert len(calls) == value["audit"]["physical_evaluations"] >= 2
    assert value["audit"]["wavelength_points"] >= value["audit"]["unique_condition_points"]
    assert value["score"]["errors"]
    assert not value["audit"]["continuous_certificate"]


def test_cancel_before_first_source_checked_grid_never_prepares(no_physics: list[int]) -> None:
    request, design, _payload = _active_request()
    cancel = Event()

    def progress(_value: Any) -> None:
        cancel.set()

    with pytest.raises(structure._StructureCancelled):
        audit.audit_frozen_synthesis_candidate(request, design, cancel_event=cancel,
                                               progress_callback=progress)
    assert no_physics == []


@pytest.mark.parametrize("mutation", ["remove_k", "restrict_k_domain", "change_n", "candidate_thickness"])
def test_observer_cannot_split_strict_and_prepared_audit_snapshots(mutation: str) -> None:
    request, design, payload = _active_request()
    baseline = audit.audit_frozen_synthesis_candidate(request, design)
    changed: list[str] = []

    def progress(_value: Any) -> None:
        if changed:
            return
        changed.append(mutation)
        optical = next(row for row in payload["materials"] if row["id"] == "glass")["optical_sets"][0]
        if mutation == "remove_k":
            optical.pop("k")
        elif mutation == "restrict_k_domain":
            optical.pop("k")
            optical["k_data"] = dict(wavelength_unit="nm", wavelength_min=540., wavelength_max=560.,
                                     wavelengths=[540., 560.], k=[.01, .02])
        elif mutation == "change_n":
            optical["n"] = [n + .2 for n in optical["n"]]
        else:
            design["front_layers"][0]["thickness"]["value"] += 20.
        _rehash(payload)

    actual = audit.audit_frozen_synthesis_candidate(request, design, progress_callback=progress)
    assert changed == [mutation]
    assert actual == baseline
