"""Independent physical and native-Qt checks for explicit unknown-only k=0."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
from coat_sim.core.film_design_service import FilmDesignService, simulate_frozen
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
    film_synthesis_result_to_payload,
)
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from tests.test_film_auto_synthesis import Service, setup_problem
from tests.test_film_optimization_needle import _optical
from tests.test_film_synthesis_gui_review import _Scene, _wait
from tests.test_film_synthesis_gui_usability_review import _open_optimizer, _type_number

pytestmark = pytest.mark.contract
ZN = "builtin_research_ZnS_substrate"
ZS = "ri_zns_debenham_n"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _assumed_request(request):
    environment = deepcopy(dict(request.environment))
    environment["assume_lossless"] = True
    return replace(request, environment=environment)


def _affected(value):
    return {(row["material_id"], row["optical_set_id"]) for row in value}


@pytest.mark.algorithm
def test_default_strict_and_global_use_k_false_cannot_silently_bypass_unknown_k():
    service, request = setup_problem(upper=1., low=550., high=550., samples=1, materials=("low",))
    service.materials.records["glass"]["optical_sets"][0]["k"] = None
    original = _digest(service.materials.records)
    assert request.environment["assume_lossless"] is False
    with pytest.raises(ValueError, match="material=glass"):
        freeze_film_synthesis_request(service, request)
    environment = deepcopy(dict(_assumed_request(request).environment))
    environment["use_k"] = False
    with pytest.raises(ValueError, match="use_k"):
        freeze_film_synthesis_request(service, replace(request, environment=environment))
    assert _digest(service.materials.records) == original


@pytest.mark.algorithm
def test_unknown_only_zero_keeps_raw_records_and_known_absorbing_layer_in_actual_tmm():
    service, request = setup_problem(upper=1., low=550., high=550., samples=1, materials=("low",))
    service.materials.records["glass"]["optical_sets"][0]["k"] = None
    service.materials.records["low"]["optical_sets"][0]["k"] = [.12] * 3
    request = replace(request, objectives=(replace(request.objectives[0], metric="T"),))
    original = _digest(service.materials.records)
    frozen = freeze_film_synthesis_request(service, _assumed_request(request))
    assert frozen.frozen_material_snapshot is not None
    raw_materials = deepcopy(frozen.frozen_material_snapshot["materials"])
    raw_by_id = {row["id"]: row for row in raw_materials}
    assert raw_by_id["glass"]["optical_sets"][0]["k"] is None
    assert raw_by_id["low"]["optical_sets"][0]["k"] == [.12] * 3
    candidate = deepcopy(dict(frozen.environment))
    candidate["front_layers"] = [{"id": "known-absorbing-film", "material_id": "low",
        "optical_set_id": "nk", "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    actual = audit_frozen_synthesis_candidate(frozen, candidate)
    assumed_T = actual["score"]["errors"][0].actual
    preview = service.freeze_design(candidate, {"wavelength_min_nm": 550., "wavelength_max_nm": 550., "samples": 1})
    preview_response = simulate_frozen(".", preview)
    assert preview_response["T"][0] == pytest.approx(assumed_T, abs=2e-13)
    # Independent control replaces only the absent substrate source by explicit
    # k=0 and uses the ordinary strict optical path. The film remains lossy.
    reference_records = deepcopy(list(service.materials.records.values()))
    next(row for row in reference_records if row["id"] == "glass")["optical_sets"][0]["k"] = [0.] * 3
    reference = Service(reference_records)
    explicit = freeze_film_synthesis_request(reference, request)
    explicit_candidate = deepcopy(candidate)
    explicit_candidate["assume_lossless"] = False
    expected = audit_frozen_synthesis_candidate(explicit, explicit_candidate)
    assert assumed_T == pytest.approx(expected["score"]["errors"][0].actual, abs=2e-13)
    next(row for row in reference_records if row["id"] == "low")["optical_sets"][0]["k"] = [0.] * 3
    all_lossless = freeze_film_synthesis_request(Service(reference_records), request)
    wrong = audit_frozen_synthesis_candidate(all_lossless, explicit_candidate)
    assert abs(assumed_T - wrong["score"]["errors"][0].actual) > .02
    assumption = actual["audit"]["absorption_assumption"]
    assert assumption["policy"] == "unknown_k_zero"
    assert _affected(assumption["affected_materials"]) == {("glass", "nk")}
    assert assumption["known_k_preserved"] is True
    assert assumption["source_records_unchanged"] is True
    assert assumption["measured_absorption_validated"] is False
    assert frozen.frozen_material_snapshot["materials"] == raw_materials
    assert _digest(service.materials.records) == original


@pytest.mark.algorithm
@pytest.mark.parametrize("failure", ["n_domain", "known_k_domain", "negative_n"])
def test_unknown_only_policy_keeps_index_and_known_k_domain_errors(failure):
    service, request = setup_problem(low=520., high=580., samples=3, materials=("low",))
    optical = service.materials.records["glass"]["optical_sets"][0]
    optical["k"] = None
    if failure == "n_domain":
        optical.update(wavelength_min=540., wavelength_max=560., wavelengths=[540., 550., 560.])
    elif failure == "known_k_domain":
        optical.clear()
        optical.update(id="nk", name="Known bounded k", kind="cauchy",
            parameters={"A": 1.5, "B": 0., "C": 0.}, wavelength_unit="nm",
            wavelength_min=450., wavelength_max=700.,
            k_data={"wavelength_unit": "nm", "wavelength_min": 540., "wavelength_max": 560.,
                "wavelengths": [540., 560.], "k": [.01, .02]})
    else:
        optical["n"] = [-1.] * 3
    with pytest.raises(ValueError):
        freeze_film_synthesis_request(service, _assumed_request(request))


@pytest.mark.algorithm
def test_offline_request_result_audit_and_candidate_file_preserve_conditional_policy(tmp_path):
    service, request = setup_problem(upper=.05, low=550., high=550., samples=1, materials=("low",))
    for identifier in ("glass", "low"):
        service.materials.records[identifier]["optical_sets"][0]["k"] = None
    original = _digest(service.materials.records)
    frozen = freeze_film_synthesis_request(service, _assumed_request(request))
    raw = film_synthesis_request_to_payload("unknown-k-independent", frozen)
    request_id, parsed, structures = film_synthesis_request_from_payload(json.loads(json.dumps(raw)))
    assert structures == ()
    assert parsed.environment["assume_lossless"] is True and parsed.environment["use_k"] is True
    assert parsed.frozen_material_snapshot == frozen.frozen_material_snapshot
    result = synthesize_film_design(None, parsed)
    assert result.state == "completed" and result.candidate_design is not None
    encoded = film_synthesis_result_to_payload(request_id, result)
    assumption = encoded["absorption_assumption"]
    assert assumption["policy"] == "unknown_k_zero"
    assert _affected(assumption["affected_materials"]) == {("glass", "nk"), ("low", "nk")}
    assert _affected(assumption["used_affected_materials"]) == {("glass", "nk")}
    assert assumption["conditional_on_assumption"] is True
    assert encoded["review"]["absorption_assumption"] == assumption
    assert encoded["best_candidate_audit"]["absorption_assumption"] == assumption
    assert result.validation_score is not None
    audit = result.validation_score["audit"]["absorption_assumption"]
    assert audit["scope"] == "candidate_active_materials"
    assert _affected(audit["affected_materials"]) == {("glass", "nk")}
    assert audit["known_k_preserved"] and audit["source_records_unchanged"]
    path = tmp_path / "explicit-assumption.cfilm"
    files = FilmDesignService(tmp_path)
    files.save(path, result.candidate_design)
    loaded = files.load(path)
    assert loaded["assume_lossless"] is True and loaded["use_k"] is True
    assert result.candidate_design["front_layers"] == loaded["front_layers"] == []
    strict_environment = deepcopy(dict(frozen.environment))
    strict_environment["assume_lossless"] = False
    strict = replace(frozen, environment=strict_environment, frozen_material_snapshot=None)
    with pytest.raises(ValueError, match="material=glass"):
        freeze_film_synthesis_request(service, strict)
    assert _digest(service.materials.records) == original


@pytest.mark.algorithm
def test_unknown_pool_unused_does_not_claim_candidate_absorption_assumption():
    service, request = setup_problem(upper=.05, low=550., high=550., samples=1, materials=("low",))
    service.materials.records["low"]["optical_sets"][0]["k"] = None
    original = _digest(service.materials.records)
    frozen = freeze_film_synthesis_request(service, _assumed_request(request))
    result = synthesize_film_design(None, frozen)
    assert result.state == "completed" and result.candidate_design is not None
    assert result.candidate_design["front_layers"] == []
    payload = film_synthesis_result_to_payload("unused-unknown-pool-independent", result)
    assumption = payload["absorption_assumption"]
    assert assumption["policy"] == "unknown_k_zero"
    assert _affected(assumption["affected_materials"]) == {("low", "nk")}
    assert assumption["used_affected_materials"] == []
    assert assumption["conditional_on_assumption"] is False
    assert "using source k" in assumption["claim"]
    assert payload["review"]["absorption_assumption"] == assumption
    assert payload["best_candidate_audit"]["absorption_assumption"] == assumption
    assert result.validation_score is not None
    candidate_fact = result.validation_score["audit"]["absorption_assumption"]
    assert candidate_fact["affected_materials"] == candidate_fact["used_affected_materials"] == []
    assert candidate_fact["conditional_on_assumption"] is False
    annotation = result.candidate_design.get("note", "") + result.candidate_design.get("summary", "")
    assert "未知 k 按 0" not in annotation
    assert _digest(service.materials.records) == original


@pytest.fixture
def unknown_k_gui(tmp_path):
    library = MaterialLibrary(tmp_path)
    original = _digest(library.read(ZN)["record"])
    optical = _optical("film-set", 1.23)
    optical.update(wavelength_min=450., wavelength_max=1000., wavelengths=[450., 1000.])
    record = default_material_profile("Explicit synthetic policy-test film", id="acceptance-film")
    record.update(category="film", optical_constants=[optical])
    library.write(record)
    service = FilmDesignService(tmp_path)
    source = FilmDesignQuickController(service=service,
        geometry_settings=QSettings(str(tmp_path/"unknown-k.ini"), QSettings.Format.IniFormat))
    assert source.initializationReady
    assert source.setSubstrateValue("material_id", ZN)
    assert source.setSubstrateValue("optical_set_id", ZS)
    assert source.setSubstrateValue("sides", "single")
    controllers: list[FilmOptimizationQuickController] = []
    scenes: list[_Scene] = []
    yield (service, source, controllers, scenes), library, original
    for controller in controllers:
        controller.cancelOptimization()
        controller.shutdown(5000)
    source.markSourceClosed()
    for scene in reversed(scenes):
        scene.close()
    for controller in controllers:
        controller.deleteLater()
    source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    client = SchedulerClient(tmp_path)
    if client.endpoint.exists():
        client.stop()


@pytest.mark.qt
def test_native_unknown_k_checkbox_starts_real_ZnS_and_off_restores_strict(unknown_k_gui, tmp_path):
    gui, library, original_record = unknown_k_gui
    service, source, controller, scene = _open_optimizer(gui)
    assert controller.synthesisAssumeUnknownKZero is True
    assert controller.setSynthesisAssumeUnknownKZero(False)  # Explicit strict start; new default is covered separately.
    scene.click(scene.item("filmOptimizationTargetsTab"))
    for name, value in (("WavelengthMax", "900"), ("WavelengthMin", "600"),
                        ("WavelengthStep", "100"), ("UpperBound", "100"), ("Tolerance", "0.1")):
        scene.edit(scene.item("filmOptimization" + name + "Field"), value)
    _type_number(scene, "filmOptimizationAngleMinField", "0", "enter")
    _type_number(scene, "filmOptimizationAngleMaxField", "0", "focus")
    scene.click(scene.item("filmOptimizationAlgorithmTab"))
    checkbox = scene.item("filmSynthesisUnknownKZeroField")
    assert checkbox.property("checked") is False
    assert controller.property("synthesisAssumeUnknownKZero") is False
    jobs = service.root / "artifacts/runtime/scheduler/jobs"
    before_jobs = set(jobs.glob("*/spec.json"))
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.property("error")))
    assert ZN in str(controller.property("error")) and ZS in str(controller.property("error"))
    assert set(jobs.glob("*/spec.json")) == before_jobs
    scene.click(checkbox)
    assert controller.property("synthesisAssumeUnknownKZero") is True
    assert controller.exportOptimizationConfiguration()["synthesis"]["assume_unknown_k_zero"] is True
    typed = controller._build_synthesis_request()
    assert typed.environment["assume_lossless"] is True and typed.environment["use_k"] is True
    assert source.currentDesign()["assume_lossless"] is False
    assert source.currentDesign()["use_k"] is True
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.property("queueJobId")))
    job_id = str(controller.property("queueJobId"))
    _wait(lambda: not controller.property("busy"), seconds=30.)
    payload = controller._synthesis_payload
    assert payload is not None and payload["result"]["state"] == "completed"
    assumption = payload["absorption_assumption"]
    assert assumption["policy"] == "unknown_k_zero"
    assert (ZN, ZS) in _affected(assumption["used_affected_materials"])
    assert assumption["known_k_preserved"] and assumption["source_records_unchanged"]
    assert assumption["measured_absorption_validated"] is False
    label = scene.item("filmSynthesisAbsorptionSummary")
    if not label.isVisible():
        scene.click(scene.item("filmOptimizationSidebarToggle"))
    scene.click(label)
    assert label.isVisible() and str(label.property("text"))
    assert "k" in str(label.property("text"))
    spec = json.loads((jobs/job_id/"spec.json").read_text(encoding="utf-8"))["parameters"]["payload"]["request"]["request"]
    assert spec["environment"]["assume_lossless"] is True and spec["environment"]["use_k"] is True
    assert spec["environment"]["front_layers"] == spec["environment"]["back_layers"] == []
    assert spec["objectives"][0]["angles_deg"] == [0.]
    assert spec["candidate_materials"] == [{"material_id": "acceptance-film", "optical_set_id": "film-set"}]
    assert spec["seed_designs"] == []
    saved_path = tmp_path / "assumed-candidate.cfilm"
    assert controller.saveCandidatePath(str(saved_path)), controller.property("error")
    saved = service.load(saved_path)
    assert saved["assume_lossless"] is True and saved["use_k"] is True
    annotation = str(saved.get("note", "")) + str(saved.get("summary", ""))
    assert ZN in annotation and ZS in annotation and "未知 k 按 0" in annotation
    assert _digest(library.read(ZN)["record"]) == original_record
    assert controller._candidate_applied
    assert source.currentDesign()["assume_lossless"] is True
    assert source.currentDesign()["front_layers"] == payload["result"]["candidate_design"]["front_layers"]
    scene.click(scene.item("filmSynthesisUnknownKZeroField"))
    assert controller.property("synthesisAssumeUnknownKZero") is False
    assert controller._build_synthesis_request().environment["assume_lossless"] is False
    # The strict next request must not rewrite the already-applied candidate's
    # explicit absorption assumption or its original frozen evidence.
    assert source.currentDesign()["assume_lossless"] is True
    before_jobs = set(jobs.glob("*/spec.json"))
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: ZN in str(controller.property("error")))
    assert not controller.property("busy")
    assert set(jobs.glob("*/spec.json")) == before_jobs
    assert _digest(library.read(ZN)["record"]) == original_record


@pytest.mark.algorithm
def test_automatic_physical_seed_canonical_job_roundtrip_keeps_unknown_k_policy():
    from coat_sim.core.film_physical_structures import (
        FilmOpticalLayerSpec, FilmStructureMaterialBinding, FilmStructureSpec,
        generate_physical_structure, structure_spec_to_mapping,
    )
    from coat_sim.core.film_synthesis_jobs import (
        build_job_payload, canonical_parameters, deserialize_job_payload,
    )
    service, request = setup_problem(upper=1., low=550., high=550., samples=1, materials=("low",))
    service.materials.records["low"]["optical_sets"][0]["k"] = None
    original = _digest(service.materials.records)
    frozen = freeze_film_synthesis_request(service, _assumed_request(request))
    spec = FilmStructureSpec("composite", (FilmStructureMaterialBinding("L", "low", "nk"),),
        (FilmOpticalLayerSpec("L", 550.),), name="Explicitly assumed physical seed")
    assert frozen.frozen_material_snapshot is not None
    with pytest.raises(ValueError):
        generate_physical_structure(frozen.environment, spec, frozen.frozen_material_snapshot)
    generated = generate_physical_structure(frozen.environment, spec,
        frozen.frozen_material_snapshot, assume_unknown_k_zero=True)
    assert generated.candidate_design["assume_lossless"] is True
    assumption = generated.metadata["absorption_assumption"]
    assert assumption["policy"] == "unknown_k_zero"
    assert _affected(assumption["affected_materials"]) == {("low", "nk")}
    payload = film_synthesis_request_to_payload("assumed-structure-independent", frozen,
        (structure_spec_to_mapping(spec),))
    canonical = canonical_parameters(None, {"payload": payload})
    job = build_job_payload(canonical)
    request_id, restored, metadata = deserialize_job_payload(json.loads(json.dumps(job)))
    assert request_id == "assumed-structure-independent"
    assert len(restored.seed_designs) == 1
    seed = restored.seed_designs[0]
    assert seed["assume_lossless"] is True and seed["use_k"] is True
    assert seed["front_layers"] == generated.candidate_design["front_layers"]
    assert seed["front_layers"][0]["optical_set_id"] == "nk"
    assert restored.frozen_material_snapshot is not None
    optical = next(row for row in restored.frozen_material_snapshot["materials"] if row["id"] == "low")["optical_sets"][0]
    assert optical["k"] is None
    assert metadata[0]["metadata"]["absorption_assumption"] == assumption
    assert _digest(service.materials.records) == original
