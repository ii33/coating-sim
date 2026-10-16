"""Reproducible save-only benchmark; creates and removes a synthetic temp project.

Run from a checkout with PYTHONPATH=. python tests/benchmark_film_design_save.py.
To compare another source checkout against identical application data, prepend
that checkout to PYTHONPATH and pass --application-root /path/to/data-owner.
No elapsed-time threshold is part of the regression suite.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import cProfile
import json
from pathlib import Path
import pstats
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-root", type=Path)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--films", type=int, default=6)
    parser.add_argument("--layers", type=int, default=80)
    parser.add_argument("--samples", type=int, default=2001)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if min(args.iterations, args.films, args.layers) < 1 or args.samples < 2:
        parser.error("iterations/films/layers must be positive; samples must be >= 2")
    import coat_sim.core.data_files as data_files
    if args.application_root is not None:
        # Both versions read precisely the same read-only application catalog.
        data_files.PROJECT_ROOT = args.application_root.resolve()
        data_files.DATA_ROOT = data_files.PROJECT_ROOT / "data"
    from coat_sim.core.film_design import new_design
    from coat_sim.core.project_film_service import ProjectFilmService
    from coat_sim.core.project_film_session import ProjectFilmEditSession
    from coat_sim.core.project_store import ProjectStore
    import coat_sim.core.project_film_service as service_module

    material = {"id": "synthetic", "name": "Synthetic benchmark material",
                "notes": "Synthetic fixture; not production material data",
                "optical_sets": [{"id": "visible", "name": "Synthetic visible",
                                  "kind": "tabulated", "wavelength_unit": "nm",
                                  "wavelength_min": 400.0, "wavelength_max": 700.0,
                                  "wavelengths": [400.0 + 300.0 * i / (args.samples - 1)
                                                  for i in range(args.samples)],
                                  "n": [2.0] * args.samples, "k": [0.0] * args.samples,
                                  "source": "Synthetic benchmark fixture"}]}
    design = new_design("Benchmark")
    design["substrate"].update(material_id="synthetic", optical_set_id="visible")
    design["front_layers"] = [
        {"id": f"layer{i}", "material_id": "synthetic", "optical_set_id": "visible",
         "thickness": {"unit": "nm", "value": 75.0123456789012}, "note": ""}
        for i in range(args.layers)]
    with TemporaryDirectory(prefix="film-design-save-benchmark-") as tmp:
        store = ProjectStore.create(Path(tmp) / "project", "Save benchmark")
        service = ProjectFilmService(store)
        for i in range(args.films):
            candidate = deepcopy(design)
            candidate.update(id=f"design{i}", name=f"film{i}")
            saved = service.create_film(candidate, materials={"synthetic": material} if i == 0 else None)
        editor = ProjectFilmEditSession(service)
        assert editor.request_load_saved_design(saved.document_id)
        timings = []
        profiler = cProfile.Profile()
        for i in range(args.iterations):
            assert editor.set_design_value("name", f"edit{i}")
            start = perf_counter()
            if args.profile:
                profiler.enable()
            assert editor.save_current(), editor.snapshot().error
            if args.profile:
                profiler.disable()
            timings.append(perf_counter() - start)
        assert service.load_document(saved.document_id).design == editor.snapshot().design
        print(json.dumps({"source": service_module.__file__, "global_catalog_error": service.global_catalog_error,
                          "material_count": len(service._project_materials._values),
                          "film_count": args.films, "layer_count": args.layers,
                          "optical_samples": args.samples, "iterations": args.iterations,
                          "profiled": args.profile, "seconds": timings, "median_s": median(timings)}))
        if args.profile:
            pstats.Stats(profiler).sort_stats("cumulative").print_stats(25)


if __name__ == "__main__":
    main()
