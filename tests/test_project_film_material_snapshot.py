"""Focused contracts for project-owned optical material snapshots."""

from typing import Any, cast

import pytest

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_film_service import ProjectOpticalSnapshot


def _optical_set(identity: str, *, n: list[float], k: list[float] | None) -> dict:
    return {
        "id": identity,
        "name": f"Synthetic {identity}",
        "kind": "tabulated",
        "wavelength_unit": "nm",
        "wavelength_min": 400.0,
        "wavelength_max": 700.0,
        "wavelengths": [400.0, 550.0, 700.0],
        "n": n,
        "k": k,
        "source": "Synthetic unit-test values; not production data",
    }


def _material(identity: str, *, n: list[float] | None = None,
              k: list[float] | None = None) -> dict:
    return {
        "id": identity,
        "name": f"Synthetic material {identity}",
        "optical_sets": [
            _optical_set("visible", n=n or [1.4, 1.5, 1.6], k=k),
        ],
    }


def test_snapshot_detaches_input_and_returned_values() -> None:
    original = _material("project-glass", k=[0.01, 0.02, 0.03])
    snapshot = ProjectOpticalSnapshot([original])

    original["optical_sets"][0]["n"][1] = 8.0
    original["optical_sets"][0]["k"][1] = 9.0
    original["name"] = "Mutated source"
    assert snapshot.evaluate("project-glass", "visible", 550.0) == complex(1.5, 0.02)

    returned = snapshot.get("project-glass")
    returned["optical_sets"][0]["n"][1] = 7.0
    returned["optical_sets"].clear()
    returned["name"] = "Mutated return"
    option = snapshot.options()[0]
    option["optical_sets"][0]["k"][1] = 6.0
    option["readonly"] = False

    fresh = snapshot.get("project-glass")
    assert fresh["name"] == "Synthetic material project-glass"
    assert fresh["optical_sets"][0]["n"] == [1.4, 1.5, 1.6]
    assert fresh["optical_sets"][0]["k"] == [0.01, 0.02, 0.03]
    assert snapshot.options()[0]["readonly"] is True


def test_same_material_id_can_have_different_values_in_separate_snapshots() -> None:
    left = ProjectOpticalSnapshot([_material("shared-id", n=[1.4, 1.5, 1.6])])
    right = ProjectOpticalSnapshot([_material("shared-id", n=[2.0, 2.1, 2.2])])

    assert left.evaluate("shared-id", "visible", 550.0, assume_lossless=True) == complex(1.5, 0.0)
    assert right.evaluate("shared-id", "visible", 550.0, assume_lossless=True) == complex(2.1, 0.0)


@pytest.mark.parametrize(
    "records",
    [
        [_material("duplicate"), _material("duplicate")],
        [_material("Glass"), _material("glass")],
    ],
)
def test_duplicate_or_case_conflicting_ids_are_rejected(records: list[dict]) -> None:
    with pytest.raises(ProjectError) as error:
        ProjectOpticalSnapshot(records)

    assert error.value.code == "dependency_conflict"


def test_missing_material_and_optical_set_have_stable_project_errors() -> None:
    snapshot = ProjectOpticalSnapshot([_material("available")])

    with pytest.raises(ProjectError) as missing_material:
        snapshot.get("absent")
    assert missing_material.value.code == "missing_dependency"
    with pytest.raises(ProjectError) as missing_set:
        snapshot.evaluate("available", "absent-set", 550.0)
    assert missing_set.value.code == "missing_dependency"


def test_roleless_project_records_are_readonly_options_for_every_picker() -> None:
    snapshot = ProjectOpticalSnapshot([_material("one"), _material("two")])

    options = snapshot.options_many(["film", "substrate", "medium"])

    assert set(options) == {"film", "substrate", "medium"}
    for rows in options.values():
        assert {row["id"] for row in rows} == {"one", "two"}
        assert all(row["readonly"] is True for row in rows)
        assert all("roles" not in row for row in rows)


def test_invalid_picker_purpose_is_rejected() -> None:
    snapshot = ProjectOpticalSnapshot([_material("one")])

    with pytest.raises(ValueError, match="film/substrate/medium"):
        snapshot.options("optics")
    with pytest.raises(ValueError, match="film/substrate/medium"):
        snapshot.options_many(["film", "optics"])


def test_existing_tabulated_evaluation_interpolates_n_and_k() -> None:
    snapshot = ProjectOpticalSnapshot([
        _material("measured", n=[1.4, 1.6, 1.8], k=[0.0, 0.1, 0.2]),
    ])

    assert snapshot.evaluate("measured", "visible", 475.0) == complex(1.5, 0.05)
    assert snapshot.evaluate("measured", "visible", 700.0) == complex(1.8, 0.2)
    with pytest.raises(ValueError):
        snapshot.evaluate("measured", "visible", 701.0)


def test_unknown_k_needs_explicit_lossless_assumption() -> None:
    snapshot = ProjectOpticalSnapshot([_material("unknown-k")])

    with pytest.raises(ValueError, match="explicit lossless assumption"):
        snapshot.evaluate("unknown-k", "visible", 550.0)
    assert snapshot.evaluate("unknown-k", "visible", 550.0, assume_lossless=True) == complex(1.5, 0.0)
    with pytest.raises(ValueError, match="explicit bool"):
        snapshot.evaluate("unknown-k", "visible", 550.0, assume_lossless=cast(Any, 1))


def test_explicit_picker_usage_filters_one_material_to_its_assigned_role() -> None:
    snapshot = ProjectOpticalSnapshot(
        [_material("film-only")], usage={"film-only": {"film"}},
    )

    options = snapshot.options_many(["film", "substrate", "medium"])

    assert [row["id"] for row in options["film"]] == ["film-only"]
    assert options["film"][0]["readonly"] is True
    assert options["substrate"] == []
    assert options["medium"] == []


def test_picker_usage_with_unknown_material_identity_is_rejected() -> None:
    with pytest.raises(ProjectError) as error:
        ProjectOpticalSnapshot(
            [_material("known")], usage={"unknown": {"film"}},
        )

    assert error.value.code == "dependency_conflict"
