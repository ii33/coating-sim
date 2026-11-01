"""Public, unseeded four-window request construction; no optimization run."""
import hashlib
import json
import sys

from coat_sim.core.data_files import PROJECT_ROOT
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.materials import builtin_material_profiles
from tools.run_film_synthesis_acceptance import public_four_window_case, sha256_json


def test_public_four_window_request_is_reproducible_from_packaged_catalog():
    service, request, provenance = public_four_window_case()
    env = request.environment
    assert env["front_layers"] == env["back_layers"] == []
    assert env["layer_order"] == "substrate_outward"
    assert env["substrate"] == {
        "material_id": "film_substrate_cdgm_hk9l", "optical_set_id": "cdgm_hk9l_sellmeier_nm",
        "thickness": {"value": 3.0, "unit": "mm"}, "sides": "double", "mode": "auto"}
    air = {"kind": "material", "material_id": "builtin_ideal_air", "optical_set_id": "ideal_air_n1_k0"}
    assert env["incident_medium"] == env["exit_medium"] == air
    assert env["first_surface"] == "front"
    assert env["use_k"] is True and env["assume_lossless"] is True
    assert request.selected_surfaces == ("front",)
    assert request.search_until_target is True
    assert request.seed_designs == () and request.search_checkpoint is None and request.warm_start_seed_index is None
    for field in ("minimum_nonzero_thickness_nm", "max_layer_thickness_nm", "max_total_thickness_nm",
                  "max_layers", "max_cycles", "max_evaluations", "max_seconds", "max_working_set_bytes"):
        assert getattr(request, field) is None
    assert [(r.material_id, r.optical_set_id) for r in request.candidate_materials] == [
        ("builtin_research_SiO2_film", "ri_sio2_rodriguez_de_marcos"),
        ("builtin_research_TiO2_film", "rii_tio2_franta")]
    assert [(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples,
             o.pointwise.lower_bound, o.pointwise.upper_bound) for o in request.objectives] == [
        (400., 500., 101, .8, None), (510., 550., 41, None, .0065),
        (560., 700., 141, .8, None), (1040., 1080., 41, None, .0065)]
    for objective in request.objectives:
        assert objective.metric == "T" and objective.angles_deg == (0.,)
        assert objective.polarization == "unpolarized" and objective.pointwise.enabled
        assert not objective.pointwise.strict_lower and not objective.pointwise.strict_upper
        assert not objective.mean.enabled

    frozen = freeze_film_synthesis_request(service, request)
    assert frozen.frozen_material_snapshot is not None
    records = {r["id"]: r for r in frozen.frozen_material_snapshot["materials"]}
    profiles = {r["id"]: r for r in builtin_material_profiles()}
    for entry in provenance["material_optical_set_ids"]:
        material_id, set_id = entry["material_id"], entry["optical_set_id"]
        group = next(g for g in profiles[material_id]["optical_constants"] if g["id"] == set_id)
        assert records[material_id]["optical_sets"] == [group]
        assert provenance["optical_group_sha256"][material_id + "/" + set_id] == sha256_json(group)
    for material_id in ("builtin_research_TiO2_film", "builtin_research_SiO2_film"):
        k = records[material_id]["optical_sets"][0]["k"]
        assert k and max(k) > 0  # Known absorption was not replaced by zero.
    assert "k" not in records["film_substrate_cdgm_hk9l"]["optical_sets"][0]
    for relative, digest in provenance["repository_source_sha256"].items():
        assert hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest() == digest
    service2, request2, provenance2 = public_four_window_case()
    assert provenance2 == provenance
    assert sha256_json(freeze_film_synthesis_request(service2, request2)) == sha256_json(frozen)


def test_explicit_cli_scenario_dispatch_without_running_optimization(tmp_path, monkeypatch):
    from tools import run_film_synthesis_acceptance as runner

    observed = []
    def fake_run_case(name, label, service, request, output_dir, cancel_event,
                      heartbeat_state, events_file, repo_root):
        observed.append((name, label, request))
        return {"summary": {"state": "not_run_test_stub"}}

    out = tmp_path / "acceptance"
    monkeypatch.setattr(runner, "run_case", fake_run_case)
    monkeypatch.setattr(sys, "argv", ["run_film_synthesis_acceptance.py", "--repo-root", str(PROJECT_ROOT),
                                    "--out", str(out), "--scenario", "public-four-window"])
    assert runner.main() == 0
    assert len(observed) == 1 and observed[0][0] == "public_four_window_from_zero"
    assert observed[0][2].seed_designs == () and observed[0][2].search_checkpoint is None
    report = json.loads((out / "run.json").read_text())
    assert report["results"][0]["summary"]["state"] == "not_run_test_stub"
    for name in ("rejection_priors", "local_restarts", "refinement", "continuation", "physics"):
        relative = f"coat_sim/core/film_synthesis_{name}.py"
        assert report["implementation_sha256"][relative] == hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
    assert (out / "public_four_window_provenance.json").is_file()
