"""Run the portable example on CPU, without writing results into the project."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.process_simulator import simulate_process
from coat_sim.core.simulator import Settings


class EmptyCatalog:
    def catalog_records(self):
        return []


def verify(project_file):
    store = ProjectStore.open(project_file)
    plan = load_project_default(store)
    if not plan.configured:
        raise ValueError("Example has no default deposition configuration")
    for row in store.manifest["documents"]:
        store.resolve_document(row["id"])
    graph = plan.graph
    snapshot = plan.film_design_snapshot
    if graph is None or snapshot is None:
        raise ValueError("Example default must include a resolved graph and frozen film")
    parameters = graph["configuration"]["parameters"]
    deposition = simulate_process(Settings(**parameters["settings"]), graph["runtime"],
                                  backend=parameters["backend"])
    service = ProjectFilmService(store, global_material_library=EmptyCatalog())
    film = snapshot["design"]
    optical = service.simulate(film, film["workflow_settings"]["simulation"]["parameters"])
    return {"documents": len(store.manifest["documents"]), "default_ready": plan.configured,
            "deposition": {"grid_shape": list(deposition["field_nm"].shape),
                           "time_samples": deposition["computed_time_steps"],
                           "mean_nm": deposition["mean_nm"], "min_nm": deposition["min_nm"],
                           "max_nm": deposition["max_nm"]},
            "spectrum": {"samples": len(optical["R"]), "min_R": min(optical["R"]),
                         "max_R": max(optical["R"]),
                         "max_energy_residual": max(abs(r + t - 1.) for r, t in zip(optical["R"], optical["T"], strict=True))}}


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "synthetic-demo" / "project.coatproj"
    print(json.dumps(verify(path), ensure_ascii=False, indent=2))
