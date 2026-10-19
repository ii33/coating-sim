"""Independent regressions for catalog provenance and solver boundaries."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from pydantic import TypeAdapter

from coat_sim.core.api import CoatService
from coat_sim.core.input_snapshot import canonical_json_bytes, freeze_process, runtime_from_snapshot
from coat_sim.core.material_physics import evaluate_source_thermochemistry, reference_material_data
from coat_sim.core.material_research import research_material_records, validate_research_data
from coat_sim.core.materials import builtin_material_profiles, default_material_profile, validate_material_profile, source_bindable
from coat_sim.core.schemas import SourceDesign, design_data
from coat_sim.core.simulation_assets import export_simulation_asset, read_simulation_asset, save_simulation_input_asset
from coat_sim.core.simulator import Settings
from coat_sim.core.source_profiles import effective_source_exponent, source_samples, validate_source_profile
from tests.test_input_snapshot import fixture, rehash


def films():
    return [row for row in builtin_material_profiles() if source_bindable(row)]


class ResearchCatalogIndependentReview(unittest.TestCase):
    def test_expected_material_roles_are_separate_records(self):
        records = research_material_records()
        self.assertEqual(len(records), 18)
        self.assertEqual(sum("film" in row["roles"] for row in records), 13)
        self.assertEqual(sum("substrate" in row["roles"] for row in records), 5)
        self.assertEqual(sum("boat" in row["roles"] for row in records), 0)
        byid = {row["id"]: row for row in records}
        for formula in ("ZnS", "Ge"):
            self.assertNotEqual(byid[formula + "_film"]["roles"],
                                byid[formula + "_substrate"]["roles"])

    def test_response_contract_evidence_is_self_contained(self):
        def references(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "source_ref":
                        yield item
                    elif key == "evidence_refs":
                        yield from item
                    else:
                        yield from references(item)
            elif isinstance(value, list):
                for item in value:
                    yield from references(item)
        for record in research_material_records():
            self.assertTrue(record["provenance_input_files"])
            for source in record["provenance_input_files"].values():
                self.assertRegex(source["sha256"], r"^[a-f0-9]{64}$")
                self.assertGreater(source["bytes"], 0)
            contract = record.get("response_contract")
            if contract is None:
                continue
            for reference in references(contract):
                with self.subTest(material=record["id"], reference=reference):
                    self.assertIn(reference, record.get("resolved_evidence", {}))
                    self.assertTrue(record["resolved_evidence"][reference])

    def test_every_property_has_resolved_source_and_unknown_stays_null(self):
        statuses = set()
        for record in research_material_records():
            sources = {source["id"] for source in record["sources"]}
            for prop in record["properties"]:
                with self.subTest(record=record["id"], prop=prop["id"]):
                    statuses.add(prop["status"])
                    self.assertTrue(set(prop["source_ids"]).issubset(sources))
                    self.assertTrue(prop["conditions"])
                    if prop["status"] == "unknown":
                        self.assertIsNone(prop["value"])
                    elif prop["status"] in ("conditional", "verified"):
                        self.assertTrue(prop["source_ids"])
        self.assertTrue({"unknown", "quarantined", "conditional"}.issubset(statuses))

    def test_research_provenance_and_identity_are_exact_detached_json(self):
        canonical = films()[1]["research_data"]
        for mutate in (
            lambda row: row["sources"][0].update(url="https://example.invalid/edited"),
            lambda row: row["properties"][0].update(value=0),
            lambda row: row["properties"][0].update(status="verified"),
            lambda row: row["properties"][0].update(source_ids=[]),
            lambda row: row.update(dataset_id="new-unapproved-version"),
            lambda row: row.update(schema_version=True),
        ):
            changed = deepcopy(canonical)
            mutate(changed)
            with self.assertRaises(ValueError):
                validate_research_data(changed)
        returned = validate_research_data(canonical)
        returned["properties"][0]["source_ids"].append("edited")
        self.assertNotEqual(returned, canonical)
        self.assertEqual(validate_research_data(canonical), canonical)

    def test_other_researched_material_cannot_borrow_zns_physics(self):
        researched = [material for material in builtin_material_profiles()
                      if material.get("research_data") and
                      material["research_data"]["id"] not in ("ZnS", "ZnS_film")]
        expected = {record["id"] for record in research_material_records()
                    if record["id"] not in ("ZnS", "ZnS_film")}
        self.assertEqual({material["research_data"]["id"] for material in researched}, expected)
        for material in researched:
            material["reference_data"] = reference_material_data()
            with self.subTest(material=material["research_data"]["id"]):
                with self.assertRaises(ValueError):
                    validate_material_profile(material)
                with self.assertRaises(ValueError):
                    validate_source_profile({"kind": "point", "material": material,
                        "thermochemistry": {"model_id": "zns_ri8301_gas_v1", "temperature_K": 1200}})
                with self.assertRaises(ValueError):
                    evaluate_source_thermochemistry({"kind": "point", "material": material,
                        "thermochemistry": {"model_id": "zns_ri8301_gas_v1", "temperature_K": 1200}})

    def test_all_nonfilm_roles_rejected_for_every_device_and_uniform_model(self):
        for material in builtin_material_profiles():
            if "film" in material["roles"]:
                continue
            for device in ("custom", "crucible", "resistance_boat", "electron_beam"):
                for emission in ({"kind": "uniform"}, None):
                    with self.subTest(material=material["id"], device=device, emission=emission):
                        source = {"kind": "point", "device_type": device, "material": material,
                                  "emission_model": emission}
                        with self.assertRaises(ValueError):
                            validate_source_profile(source)
                        with self.assertRaises(ValueError):
                            TypeAdapter(SourceDesign).validate_python(source)

    def test_every_film_has_lossless_typed_roundtrip_without_implicit_physics(self):
        for material in films():
            for device in ("custom", "resistance_boat", "electron_beam"):
                with self.subTest(material=material["id"], device=device):
                    normalized = validate_source_profile({"kind": "point", "device_type": device,
                                                          "material": material})
                    typed = TypeAdapter(SourceDesign).validate_python(normalized)
                    self.assertEqual(design_data("source", json.loads(typed.model_dump_json())), normalized)
                    self.assertNotIn("thermochemistry", normalized)

    def test_research_numbers_do_not_change_spatial_source_or_process_rate(self):
        settings = Settings(grid_size=21, time_steps=4, carrier_rpm=0)
        for material in films():
            stripped = deepcopy(material)
            for key in ("research_data", "roles", "reference_data"):
                stripped.pop(key, None)
            for device in ("resistance_boat", "electron_beam"):
                source = {"kind": "disk", "radius_mm": 12, "device_type": device, "material": material}
                baseline = dict(source, material=stripped)
                for actual, expected in zip(source_samples(settings, source), source_samples(settings, baseline)):
                    np.testing.assert_array_equal(actual, expected)
                self.assertEqual(effective_source_exponent(settings, source),
                                 effective_source_exponent(settings, baseline))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, recipe = fixture(root, mask_count=0)
            before = service.simulate_recipe(settings, process_path=root / "recipe.cproc", backend="CPU")
            material = next(row for row in films() if row["research_data"]["id"] == "ITO_film")
            service.save("source", root / "parts/source.csrc", {"kind": "point", "material": material}, overwrite=True)
            after = service.simulate_recipe(settings, process_path=root / "recipe.cproc", backend="CPU")
            np.testing.assert_array_equal(before["field_nm"], after["field_nm"])
            self.assertEqual(before["step_results"], after["step_results"])

    def test_legacy_user_records_need_no_research_or_roles(self):
        original = default_material_profile("Custom assumptions", id="legacy")
        for device in ("custom", "resistance_boat", "electron_beam"):
            source = validate_source_profile({"kind": "point", "device_type": device, "material": original})
            self.assertEqual(design_data("source", source), source)
            self.assertNotIn("research_data", source["material"])
            self.assertNotIn("roles", source["material"])

    def test_immutable_assets_reject_rehashed_evidence_edits_and_export_full_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, recipe = fixture(root, mask_count=0)
            material = next(row for row in films() if row["research_data"]["id"] == "ITO_film")
            service.save("source", root / "parts/source.csrc", {"kind": "point", "material": material}, overwrite=True)
            snapshot = freeze_process(service, recipe, root)
            original_bytes = canonical_json_bytes(snapshot)
            changed = deepcopy(snapshot)
            record = next(row for row in changed["files"] if row["kind"] == "source")
            record["data"]["material"]["research_data"]["properties"][0]["status"] = "verified"
            rehash(record)
            with self.assertRaises(ValueError):
                runtime_from_snapshot(changed)
            asset = save_simulation_input_asset(root, name="Full evidence review", settings={},
                input_snapshot=snapshot, confirmed=True)["asset_path"]
            source = next(row for row in read_simulation_asset(asset, detail="full")["designs"] if row["kind"] == "source")
            (root / "parts/source.csrc").unlink()
            for label, members in (("source-only", [source["path"]]), ("full-process", None)):
                exported = export_simulation_asset(asset, root / label, members=members)
                source_path = next(row["path"] for row in exported["designs"] if row["kind"] == "source")
                self.assertEqual(CoatService(root / label).load("source", source_path).data["material"], material)
            self.assertEqual(canonical_json_bytes(snapshot), original_bytes)


class ResearchQueueIndependentReview(unittest.TestCase):
    def test_real_queue_preserves_research_after_source_deletion(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QUrl
        from PySide6.QtWidgets import QApplication
        from coat_sim.core.compute_tasks import ComputeTaskStore
        from coat_sim.ui.main import MainBridge
        from tests.scheduler_queue_support import SchedulerHarness
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _ = fixture(root, mask_count=0)
            material = next(row for row in films() if row["research_data"]["id"] == "ITO_film")
            source_path = root / "parts/source.csrc"
            service.save("source", source_path, {"kind": "point", "material": material}, overwrite=True)
            store = ComputeTaskStore(root)
            harness = SchedulerHarness(root)
            gate = root / "queue-gate"
            gate.mkdir()
            bridge = None
            with patch("coat_sim.core.data_files.PROJECT_ROOT", root), \
                    patch("coat_sim.core.data_files.DATA_ROOT", root / "data"), \
                    patch("coat_sim.ui.main.SchedulerClient", side_effect=harness.client), \
                    harness.gated_workers(gate):
                bridge = MainBridge(service)
                bridge.chooseFile("process", QUrl.fromLocalFile(str(root / "recipe.cproc")))
                bridge.setDevice("CPU")
                bridge._values.update(grid_size="21", time_steps="4")
                bridge._time_basis = "time_steps"
                bridge._fast_cycle = False
                try:
                    bridge.calculate()
                    queue_id = bridge._cancel_event.job_id
                    gate_deadline = time.monotonic() + 10
                    started = gate / "started-21"
                    while not started.exists() and time.monotonic() < gate_deadline:
                        harness.tick()
                        app.processEvents()
                        time.sleep(.01)
                    self.assertTrue(started.exists(), "real scheduler worker did not reach its gate")
                    self.assertEqual(harness.client().status(queue_id)["state"], "running")
                    source_path.unlink()
                    (gate / "release").touch()
                    deadline = time.monotonic() + 12
                    while bridge.busy and time.monotonic() < deadline:
                        harness.tick()
                        app.processEvents()
                        time.sleep(.01)
                    app.processEvents()
                    self.assertFalse(bridge.busy, bridge.status)
                    self.assertTrue(bridge.hasResult, bridge.status)
                finally:
                    (gate / "release").touch()
                    if bridge.busy:
                        bridge.cancel()
                        deadline = time.monotonic() + 12
                        while bridge.busy and time.monotonic() < deadline:
                            harness.tick()
                            app.processEvents()
                            time.sleep(.01)
                    bridge.detach_observers()
                    harness.close()
            job = store.snapshot()["jobs"][0]
            self.assertEqual(job["state"], "succeeded", job)
            self.assertTrue(job["result"]["asset"]["run_id"])
            frozen_spec = json.loads((root / "artifacts/runtime/scheduler/jobs" /
                                      job["job_id"] / "spec.json").read_text())
            self.assertEqual(
                runtime_from_snapshot(frozen_spec["parameters"]["process_path"])
                ["machine"]["sources"][0]["profile"]["material"], material,
            )
            bundle = service.load_simulation_asset(job["result"]["asset"]["temporary_bundle_path"])
            self.assertEqual(runtime_from_snapshot(bundle["input_snapshot"])["machine"]["sources"][0]["profile"]["material"], material)


if __name__ == "__main__":
    unittest.main()
