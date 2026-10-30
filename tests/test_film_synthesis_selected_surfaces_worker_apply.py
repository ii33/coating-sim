"""Integrated worker, cancel/resume and draft-application checks for side selection."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
import json

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, execute_job, is_synthesis_result,
)
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = pytest.mark.contract


class _SessionService(Service):
    """Add the small editor catalog surface required by FilmDesignSession."""

    def new_design(self):
        return new_design()

    def material_options(self, purpose="film"):
        return [deepcopy(row) for row in self.materials._records()]


def _design_with_stacks(selected):
    env = new_design(design_id="selected-surfaces-worker-apply")
    env["substrate"].update(material_id="glass", optical_set_id="nk",
                            sides="double", mode="coherent")
    env["first_surface"] = "back"
    env["front_layers"] = [{"id": "source-front", "material_id": "low", "optical_set_id": "nk",
        "thickness": {"value": 84.0, "unit": "nm"}, "note": "fixed-front-note",
        "interactive_group_id": 17}]
    env["back_layers"] = [{"id": "source-back", "material_id": "high", "optical_set_id": "nk",
        "thickness": {"value": 63.0, "unit": "nm"}, "note": "fixed-back-note",
        "interactive_group_id": 23}]
    source = validate_design(env)
    request_environment = deepcopy(source)
    for side in selected:
        request_environment[side + "_layers"] = []
    seed = deepcopy(source)
    return source, request_environment, seed


def _execute(service, request, tmp_path, *, cancel_event=None, reports=None):
    frozen = freeze_film_synthesis_request(service, request)
    payload = film_synthesis_request_to_payload("selected-surface-worker", frozen)
    job = build_job_payload(canonical_parameters(None, {"payload": payload}))
    checkpoint_path = tmp_path / "best.json"
    persisted = []
    states = []
    report_callback = (reports if callable(reports) else reports.append
                       if reports is not None else lambda *_args, **_kwargs: None)
    response = execute_job({"payload": job}, cancel_event or Event(),
        report_callback,
        checkpoint_path, lambda path, value: persisted.append((str(path), deepcopy(value))),
        str(tmp_path), search_state_writer=lambda path, value: states.append((str(path), deepcopy(value))))
    assert is_synthesis_result(response)
    return frozen, response, persisted, states


@pytest.mark.parametrize("selected", [("front",), ("back",), ("front", "back")])
def test_full_worker_result_applies_one_draft_transaction_and_preserves_fixed_surface(selected, tmp_path):
    synthesis_service, base = setup_problem(upper=1.0, low=550.0, high=550.0, samples=1)
    source, environment, seed = _design_with_stacks(selected)
    editor_service = _SessionService(synthesis_service.materials._records())
    session = FilmDesignEditSession(editor_service)
    assert session.request_replace_design(source)
    source_snapshot = session.snapshot()
    request = replace(base, environment=environment, seed_designs=(seed,),
        selected_surfaces=selected, warm_start_seed_index=0,
        design_revision=source_snapshot.design_revision,
        max_cycles=2, max_evaluations=12, search_strategy="hybrid")
    frozen, response, _checkpoints, _states = _execute(synthesis_service, request, tmp_path)
    candidate = response["result"]["candidate_design"]
    assert candidate is not None
    assert candidate["first_surface"] == "back"
    fixed = ({"front", "back"} - set(selected)).pop() if len(selected) == 1 else None
    if fixed is not None:
        before = json.dumps(source[fixed + "_layers"], sort_keys=True, separators=(",", ":"))
        after = json.dumps(candidate[fixed + "_layers"], sort_keys=True, separators=(",", ":"))
        assert after == before
    assert session.apply_optimized_design(candidate, source_snapshot.design_revision,
        frozen_material_snapshot=frozen.frozen_material_snapshot)
    applied = session.snapshot()
    assert applied.design_revision == source_snapshot.design_revision + 1
    assert applied.dirty and applied.design["first_surface"] == "back"
    assert applied.design["id"] == source["id"]
    if fixed is not None:
        assert applied.design[fixed + "_layers"] == source[fixed + "_layers"]
    assert session.can_undo_optimized_design(applied.design_revision)
    assert session.undo_optimized_design(applied.design_revision)
    assert session.snapshot().design == source_snapshot.design


def test_real_worker_cancelled_audited_best_can_resume_with_same_surface_contract(tmp_path):
    service, base = setup_problem(upper=0.0001, low=550.0, high=550.0, samples=1)
    selected = ("back",)
    source, environment, seed = _design_with_stacks(selected)
    fixed_front = deepcopy(source["front_layers"])
    cancel = Event()
    reports = []
    def report(item, **_kwargs):
        reports.append(deepcopy(item))
        if item.get("score_scope") == "audited":
            cancel.set()
    request = replace(base, environment=environment, seed_designs=(seed,),
        selected_surfaces=selected, warm_start_seed_index=0,
        max_cycles=5, max_evaluations=40, search_strategy="hybrid")
    frozen, cancelled, persisted, states = _execute(service, request, tmp_path,
        cancel_event=cancel, reports=report)
    assert any(item.get("score_scope") == "audited" for item in reports)
    assert cancelled["result"]["state"] == "cancelled"
    cancelled_design = cancelled["result"]["candidate_design"]
    assert cancelled_design is not None
    assert cancelled_design["front_layers"] == fixed_front
    assert persisted and states

    editor = FilmDesignEditSession(_SessionService(service.materials._records()))
    assert editor.request_replace_design(source)
    base_revision = editor.snapshot().design_revision
    assert editor.apply_optimized_design(cancelled_design, base_revision,
        frozen_material_snapshot=frozen.frozen_material_snapshot)
    assert editor.snapshot().design["front_layers"] == fixed_front

    from coat_sim.core.film_synthesis_continuation import continue_synthesis_request
    checkpoint = cancelled["result"].get("search_checkpoint")
    assert checkpoint is not None
    resumed = continue_synthesis_request(frozen, checkpoint, max_evaluations=48)
    assert resumed.selected_surfaces == selected
    assert resumed.environment["front_layers"] == fixed_front
    _resumed_frozen, resumed_result, _new_checkpoints, _new_states = _execute(
        service, resumed, tmp_path / "resumed")
    assert is_synthesis_result(resumed_result)
    assert resumed_result["result"]["candidate_design"]["front_layers"] == fixed_front
