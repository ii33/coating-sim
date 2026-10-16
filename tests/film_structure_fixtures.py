"""Synthetic inputs for queue/UI contracts, rebuilt from tracked needle fixtures.

The original ``test_film_optimization_independent_acceptance`` is absent from
the baseline and its history. This module supplies input data only; it does
not restore that file's unknown tests or provide an optimization oracle.
"""
from copy import deepcopy

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_optimization import FilmOptimizationRequest, FilmThicknessVariable
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from tests.test_film_optimization_needle import _optical


def fixture(root):
    """Use the existing needle test's constant n=1.5 glass and n=1.23 film."""
    library = MaterialLibrary(root)
    for identifier, category, optical in (
        ("acceptance-glass", "substrate", _optical("glass-set", 1.5)),
        ("acceptance-film", "film", _optical("film-set", 1.23)),
    ):
        record = default_material_profile("Synthetic " + identifier, id=identifier)
        record.update(category=category, optical_constants=[optical])
        library.write(record)
    service = FilmDesignService(root)
    design = service.new_design()
    design["substrate"].update(material_id="acceptance-glass", optical_set_id="glass-set")
    design["front_layers"] = [{
        "id": "existing-stable-layer", "material_id": "acceptance-film",
        "optical_set_id": "film-set", "thickness": {"value": 100.0, "unit": "nm"},
        "note": "Synthetic queue/UI fixture; not restored independent acceptance data",
    }]
    return service, library, design


def request(design, objectives, *, bounds, max_iterations, max_evaluations):
    layer_id = design["front_layers"][0]["id"]
    return FilmOptimizationRequest(
        design_snapshot=deepcopy(design), design_revision=19,
        variables=(FilmThicknessVariable("front", layer_id, *bounds),),
        objectives=tuple(objectives), max_iterations=max_iterations,
        max_evaluations=max_evaluations,
    )
