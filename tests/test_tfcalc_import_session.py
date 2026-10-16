"""Synthetic import confirmation and sole-document-owner regressions."""
from copy import deepcopy

import pytest

from coat_sim.core.film_design_session import FilmDesignEditSession, SessionChange
from tests.test_film_design_generator_session import _MemoryService
from tests.test_tfcalc_import import synthetic_tfd
from coat_sim.core.tfcalc_import import build_tfcalc_design, parse_tfcalc_text

pytestmark = pytest.mark.contract


def candidate(session):
    return build_tfcalc_design(parse_tfcalc_text(synthetic_tfd()), {
        "source_table": "primary", "thickness_field": 4, "thickness_unit": "nm",
        "source_order": "substrate_outward", "target_side": "front",
        "reference_wavelength_nm": 550, "preserve_environment_confirmed": True,
        "mapping": {alias: {"material_id": alias, "optical_set_id": alias + "_set"}
                    for alias in ("H", "L")},
    }, session.snapshot().design, session.service.options["film"])


def test_import_is_staged_cancelled_and_confirmed_atomically_as_dirty_new_path():
    session = FilmDesignEditSession(_MemoryService())
    session._path = "synthetic-original.cfilm"
    session.set_design_value("name", "current dirty name")
    before = session.snapshot()
    value = candidate(session)
    changes: list[SessionChange] = []
    session.subscribe(changes.append)
    assert session.request_imported_design(value, before.design_revision)
    staged = session.snapshot()
    assert staged.design == before.design
    assert (staged.path, staged.dirty, staged.design_revision) == (before.path, before.dirty, before.design_revision)
    assert staged.pending_confirmation is not None
    assert staged.pending_confirmation["action"] == "import_tfcalc"
    assert not session.resolve_pending_action(False)
    assert session.snapshot().design == before.design
    assert session.request_imported_design(value, before.design_revision)
    assert session.resolve_pending_action(True)
    result = session.snapshot()
    assert result.design == value
    assert result.path is None and result.dirty
    assert result.design_revision == before.design_revision + 1
    assert result.pending_confirmation is None
    assert sum("design" in change.categories for change in changes) == 1
    assert not session.resolve_pending_action(True)


@pytest.mark.parametrize("mutation", ["physical", "workflow"])
def test_mutations_during_confirmation_reject_candidate_without_reverting_user_edit(mutation):
    session = FilmDesignEditSession(_MemoryService())
    value = candidate(session)
    assert session.request_imported_design(value, session.snapshot().design_revision)
    if mutation == "physical":
        session.set_design_value("name", "new user edit")
    else:
        session.set_workflow_configuration("simulation", {"samples": 17})
    current = session.snapshot()
    assert not session.resolve_pending_action(True)
    assert session.snapshot().design == current.design
    assert session.snapshot().pending_confirmation is None


def test_stale_invalid_duplicate_and_cancel_pending_preserve_document():
    session = FilmDesignEditSession(_MemoryService())
    before = session.snapshot()
    value = candidate(session)
    invalid = deepcopy(value)
    invalid["front_layers"][0]["thickness"]["value"] = 0
    assert not session.request_imported_design(invalid, before.design_revision)
    assert not session.request_imported_design(value, before.design_revision + 1)
    assert session.request_imported_design(value, before.design_revision)
    assert not session.request_imported_design(value, before.design_revision)
    session.cancel_pending_import()
    assert not session.resolve_pending_action(True)
    assert session.snapshot().design == before.design
