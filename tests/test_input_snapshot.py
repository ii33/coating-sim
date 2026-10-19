"""Offline regressions for captured design graphs and pending board candidates."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from coat_sim.core.api import CoatService, ServiceError
from coat_sim.core.input_snapshot import (candidate_snapshot,
                                         canonical_json_bytes, freeze_process,
                                         runtime_from_snapshot)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(root, *, mask_count=2, shared_masks=False):
    source = {"schema_version": 2, "kind": "point", "device_type": "custom",
              "source_exponent": 1.0}
    substrate = {"schema_version": 1, "name": "Test substrate",
                 "outline": {"kind": "circle", "radius_mm": 10.0},
                 "surface": {"kind": "flat"}}
    masks = [{"z_mm": 50.0, "outer": [[-5., -5.], [-1., -5.], [-1., 5.], [-5., 5.]],
              "holes": [[[-4., -1.], [-2., -1.], [-2., 1.], [-4., 1.]]]},
             {"z_mm": 60.0, "outer": [[1., -5.], [5., -5.], [5., 5.], [1., 5.]],
              "holes": []}]
    write_json(root / "parts" / "source.csrc", source)
    write_json(root / "parts" / "substrate.csub", substrate)
    for index, mask in enumerate(masks[:mask_count]):
        write_json(root / "parts" / f"mask{index}.cmsk", mask)
    machine = {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0.,
               "planet_offset_mm": 0., "planet_tilt_deg": 0., "substrate_z_mm": 100.,
               "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 0.,
               "sources": [{"file": "../parts/source.csrc", "x_mm": 0., "y_mm": 0., "z_mm": 0.},
                           {"file": "../parts/source.csrc", "x_mm": 10., "y_mm": 1., "z_mm": 2.}],
               "masks": [{"file": f"../parts/mask{0 if shared_masks else index}.cmsk"}
                         for index in range(mask_count)],
               "monitors": [{"name": "QCM", "kind": "crystal", "x_mm": 20.,
                             "y_mm": 5., "z_mm": 90.}]}
    write_json(root / "machines" / "machine.cmac", machine)
    recipe = {"schema_version": 6, "name": "Snapshot test",
              "machine_file": "machines/machine.cmac", "substrate_file": "parts/substrate.csub",
              "steps": [{"source_slot": 1, "target_nm": 1., "rate_nm_s": 1., "target_mode": "mean"},
                        {"source_slot": 2, "target_nm": 2., "rate_nm_s": 2., "target_mode": "monitor",
                         "monitor_slot": 1, "mask_slots": []}]}
    write_json(root / "recipe.cproc", recipe)
    return CoatService(root), recipe


def record(snapshot, kind, index=0):
    return [item for item in snapshot["files"] if item["kind"] == kind][index]


def rehash(item):
    item["sha256"] = hashlib.sha256(canonical_json_bytes(item["data"])).hexdigest()


def without_references(value):
    if isinstance(value, dict):
        return {key: without_references(item) for key, item in value.items()
                if key not in {"file", "machine_file", "substrate_file", "machine_path", "substrate_path"}}
    if isinstance(value, list):
        return [without_references(item) for item in value]
    return value


class InputSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service, self.recipe = fixture(self.root)

    def freeze(self):
        return freeze_process(self.service, self.recipe, self.root)

    def test_complete_typed_graph_matches_existing_resolver_and_writes_nothing(self):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        expected = self.service.load("process", "recipe.cproc").runtime
        snapshot = self.freeze()
        self.assertEqual(snapshot["kind"], "coat_sim.input_snapshot")
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["process_path"], "designs/process.cproc")
        self.assertEqual([item["kind"] for item in snapshot["files"]],
                         ["process", "machine", "substrate", "source", "mask", "mask"])
        self.assertEqual(without_references(snapshot["runtime"]), without_references(expected))
        self.assertEqual(snapshot["runtime"]["steps"][0]["mask_slots"], [0, 1])
        for item in snapshot["files"]:
            self.assertTrue(item["path"].startswith("designs/"))
            self.assertEqual(item["sha256"], hashlib.sha256(json.dumps(
                item["data"], ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest())
        machine = record(snapshot, "machine")["data"]
        self.assertEqual(machine["sources"][0]["file"], machine["sources"][1]["file"])
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes()
                                 for path in self.root.rglob("*") if path.is_file()})
        self.assertFalse((self.root / "data" / "tmp").exists())

    def test_reads_fresh_dependencies_and_detaches_all_caller_mutations(self):
        stale = self.service.load("process", "recipe.cproc").runtime
        source_path = self.root / "parts" / "source.csrc"
        source = json.loads(source_path.read_text())
        source["source_exponent"] = 7.0
        write_json(source_path, source)
        snapshot = self.freeze()
        self.assertEqual(stale["machine"]["sources"][0]["profile"]["source_exponent"], 1.)
        self.assertEqual(snapshot["runtime"]["machine"]["sources"][0]["profile"]["source_exponent"], 7.)
        self.recipe["steps"][0]["target_nm"] = 88.
        source_path.unlink()
        snapshot["runtime"]["steps"][0]["target_nm"] = 99.
        snapshot["runtime"]["machine"]["sources"][0]["profile"]["source_exponent"] = 3.
        rebuilt = runtime_from_snapshot(snapshot)
        self.assertEqual(rebuilt["profile"]["steps"][0]["target_nm"], 1.)
        self.assertEqual(rebuilt["machine"]["sources"][0]["profile"]["source_exponent"], 7.)
        rebuilt["machine"]["sources"][0]["profile"]["source_exponent"] = 4.
        self.assertEqual(record(snapshot, "source")["data"]["source_exponent"], 7.)
        self.assertEqual(rebuilt["machine"]["sources"][1]["profile"]["source_exponent"], 7.)

    def test_each_file_read_once_even_if_it_changes_during_capture(self):
        source_path = self.root / "parts" / "source.csrc"
        original_open = Path.open
        reads = []

        def changing_open(path, mode="r", *args, **kwargs):
            if mode == "rb":
                reads.append(path)
                with original_open(path, mode, *args, **kwargs) as stream:
                    content = stream.read()
                if path == source_path:
                    replacement = json.loads(content)
                    replacement["source_exponent"] = 15.
                    with original_open(path, "w", encoding="utf-8") as stream:
                        json.dump(replacement, stream)
                    machine_path = self.root / "machines" / "machine.cmac"
                    with original_open(machine_path, encoding="utf-8") as stream:
                        machine = json.load(stream)
                    machine["sources"][0]["x_mm"] = 123.
                    with original_open(machine_path, "w", encoding="utf-8") as stream:
                        json.dump(machine, stream)
                return io.BytesIO(content)
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", changing_open):
            snapshot = self.freeze()
        self.assertEqual(len(reads), len(set(reads)))
        self.assertEqual(reads.count(source_path), 1)
        self.assertEqual(snapshot["runtime"]["machine"]["sources"][0]["x_mm"], 0.)
        self.assertEqual(snapshot["runtime"]["machine"]["sources"][0]["profile"]["source_exponent"], 1.)
        self.assertEqual(record(snapshot, "source")["data"]["source_exponent"], 1.)
        self.assertEqual(json.loads(source_path.read_text())["source_exponent"], 15.)

    def test_reopens_with_no_filesystem_access_and_ignores_cached_runtime_or_provenance(self):
        snapshot = self.freeze()
        expected = deepcopy(snapshot["runtime"])
        snapshot["runtime"] = {"machine_path": "/etc/passwd", "machine": {"bad": True}}
        snapshot["parameters"] = {"backend": "CPU", "grid_size": 21}
        for item in snapshot["files"]:
            item["original_path"] = "/missing/original/file.json"
        with patch.object(Path, "open", side_effect=AssertionError("Unexpected filesystem access")):
            self.assertEqual(runtime_from_snapshot(snapshot), expected)

    def test_restricts_outside_root_dependency_and_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "source.csrc"
            write_json(external, {"schema_version": 2, "kind": "point"})
            machine_path = self.root / "machines" / "machine.cmac"
            machine = json.loads(machine_path.read_text())
            machine["sources"][0]["file"] = str(external)
            write_json(machine_path, machine)
            with self.assertRaises(ServiceError) as error:
                self.freeze()
            self.assertEqual(error.exception.code, "path_outside_root")
            (self.root / "link.csrc").symlink_to(external)
            machine["sources"][0]["file"] = "../link.csrc"
            write_json(machine_path, machine)
            with self.assertRaises(ServiceError):
                self.freeze()

    def test_rejects_non_json_files_before_reading(self):
        self.recipe["machine_file"] = "run.py"
        with patch.object(Path, "open", side_effect=AssertionError("Unexpected read")):
            with self.assertRaisesRegex(ValueError, r"\.cmac"):
                self.freeze()

    def test_rejects_duplicate_json_keys_and_invalid_dependency(self):
        source_path = self.root / "parts" / "source.csrc"
        source_path.write_text('{"schema_version":2,"kind":"point","kind":"disk","radius_mm":2}')
        with self.assertRaisesRegex(ValueError, "重复"):
            self.freeze()
        write_json(source_path, {"kind": "point"})
        with self.assertRaisesRegex(ValueError, "第 2 版"):
            self.freeze()
        source_path.unlink()
        with self.assertRaises(OSError):
            self.freeze()

    def test_rejects_corruption_and_wrong_typed_graph(self):
        good = self.freeze()
        bad = deepcopy(good)
        record(bad, "source")["data"]["source_exponent"] = 2.
        with self.assertRaisesRegex(ValueError, "校验和"):
            runtime_from_snapshot(bad)
        bad = deepcopy(good)
        record(bad, "machine")["kind"] = "process"
        with self.assertRaises(ValueError):
            runtime_from_snapshot(bad)
        bad = deepcopy(good)
        bad["files"] = [item for item in bad["files"] if item["kind"] != "source"]
        with self.assertRaisesRegex(ValueError, "缺少"):
            runtime_from_snapshot(bad)
        bad = deepcopy(good)
        extra = deepcopy(record(bad, "source"))
        extra["path"] = "designs/unused.csrc"
        bad["files"].append(extra)
        with self.assertRaisesRegex(ValueError, "未被工艺引用"):
            runtime_from_snapshot(bad)
        bad = deepcopy(good)
        bad["files"].append(deepcopy(bad["files"][-1]))
        with self.assertRaisesRegex(ValueError, "重复路径"):
            runtime_from_snapshot(bad)

    def test_rejects_unsafe_graph_paths_and_references(self):
        good = self.freeze()
        for value in ("../source.csrc", "/etc/source.csrc", "C:/source.csrc", "\\\\server\\source.csrc",
                      "source.py", "./source.csrc", "nested/../source.csrc", "NUL.json"):
            with self.subTest(reference=value):
                bad = deepcopy(good)
                item = record(bad, "machine")
                item["data"]["sources"][0]["file"] = value
                rehash(item)
                with self.assertRaises(ValueError):
                    runtime_from_snapshot(bad)
        for value in ("../process.cproc", "outside/process.cproc", "designs/../process.cproc",
                      "/designs/process.cproc", "designs/p.py", "designs/P.json/"):
            with self.subTest(path=value):
                bad = deepcopy(good)
                bad["process_path"] = value
                with self.assertRaises(ValueError):
                    runtime_from_snapshot(bad)

    def test_rejects_invalid_slots_and_board_heights_using_captured_graph(self):
        good = self.freeze()
        for field, value in (("source_slot", 3), ("mask_slots", [1, 1]), ("monitor_slot", 2)):
            with self.subTest(field=field):
                bad = deepcopy(good)
                item = record(bad, "process")
                item["data"]["steps"][1][field] = value
                rehash(item)
                with self.assertRaises(ValueError):
                    runtime_from_snapshot(bad)
        bad = deepcopy(good)
        item = record(bad, "mask")
        item["data"]["z_mm"] = 100.
        rehash(item)
        with self.assertRaisesRegex(ValueError, "低于"):
            runtime_from_snapshot(bad)

    def test_maximum_unique_dependency_graph_is_supported(self):
        machine_path = self.root / "machines" / "machine.cmac"
        machine = json.loads(machine_path.read_text())
        machine["sources"] = []
        for index in range(64):
            write_json(self.root / "parts" / f"source{index}.csrc",
                       {"schema_version": 2, "kind": "point", "source_exponent": index % 20})
            machine["sources"].append({"file": f"../parts/source{index}.csrc", "x_mm": index, "y_mm": 0.})
        write_json(machine_path, machine)
        snapshot = self.freeze()
        self.assertEqual(len(snapshot["files"]), 64 + len(machine["masks"]) + 3)
        self.assertEqual(len(snapshot["runtime"]["machine"]["sources"]), 64)


class CandidateSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service, self.recipe = fixture(self.root)
        self.snapshot = freeze_process(self.service, self.recipe, self.root)

    def test_replaces_only_selected_board_and_preserves_fixed_holes(self):
        original = deepcopy(self.snapshot)
        candidate = deepcopy(self.snapshot["runtime"]["machine"]["masks"][1]["profile"])
        candidate["outer"][0][0] = 2.
        steps = deepcopy(self.snapshot["runtime"]["steps"])
        steps[1]["mask_slots"] = [0, 1]
        result = candidate_snapshot(self.snapshot, {1: candidate}, steps)
        self.assertEqual(self.snapshot, original)
        self.assertEqual(record(result, "mask"), record(original, "mask"))
        self.assertEqual(result["runtime"]["machine"]["masks"][0]["profile"],
                         original["runtime"]["machine"]["masks"][0]["profile"])
        self.assertTrue(result["runtime"]["machine"]["masks"][0]["profile"]["holes"])
        self.assertEqual(result["runtime"]["machine"]["masks"][1]["profile"], candidate)
        self.assertEqual(result["runtime"]["steps"][1]["mask_slots"], [0, 1])
        candidate["outer"][0][0] = 99.
        steps[1]["mask_slots"] = []
        self.assertEqual(result["runtime"]["machine"]["masks"][1]["profile"]["outer"][0][0], 2.)
        self.assertEqual(result["runtime"]["steps"][1]["mask_slots"], [0, 1])
        self.assertFalse((self.root / "data" / "tmp").exists())

    def test_shared_mask_file_does_not_change_unselected_slot(self):
        service, recipe = fixture(self.root, shared_masks=True)
        original = freeze_process(service, recipe, self.root)
        candidate = deepcopy(original["runtime"]["machine"]["masks"][0]["profile"])
        candidate["holes"] = []
        result = candidate_snapshot(original, {0: candidate}, original["runtime"]["steps"])
        self.assertEqual(len([item for item in result["files"] if item["kind"] == "mask"]), 2)
        self.assertEqual(result["runtime"]["machine"]["masks"][0]["profile"]["holes"], [])
        self.assertEqual(result["runtime"]["machine"]["masks"][1]["profile"],
                         original["runtime"]["machine"]["masks"][1]["profile"])
        self.assertEqual(record(result, "mask"), record(original, "mask"))

    def test_adds_new_mask_to_empty_machine_without_writes(self):
        service, recipe = fixture(self.root, mask_count=0)
        original = freeze_process(service, recipe, self.root)
        steps = deepcopy(original["runtime"]["steps"])
        steps[0]["mask_slots"] = [0]
        candidate = record(self.snapshot, "mask")["data"]
        result = candidate_snapshot(original, {0: candidate}, steps)
        self.assertEqual(len(original["runtime"]["machine"]["masks"]), 0)
        self.assertEqual(len(result["runtime"]["machine"]["masks"]), 1)
        self.assertEqual(result["runtime"]["steps"][0]["mask_slots"], [0])
        with self.assertRaisesRegex(ValueError, "依次"):
            candidate_snapshot(original, {1: candidate}, steps)

    def test_repeated_candidates_remove_orphan_masks_and_reject_invalid_input(self):
        candidate = record(self.snapshot, "mask")["data"]
        first = candidate_snapshot(self.snapshot, {0: candidate}, self.snapshot["runtime"]["steps"])
        second = candidate_snapshot(first, {0: candidate}, first["runtime"]["steps"])
        self.assertEqual(len(second["files"]), len(self.snapshot["files"]))
        self.assertEqual(runtime_from_snapshot(first)["machine"]["masks"][0]["profile"],
                         runtime_from_snapshot(second)["machine"]["masks"][0]["profile"])
        appended = candidate_snapshot(self.snapshot, {2: candidate}, self.snapshot["runtime"]["steps"])
        self.assertEqual(len(appended["runtime"]["machine"]["masks"]), 3)
        invalid_candidates: list[Any] = [{3: candidate}, {-1: candidate}, {True: candidate}, {"0": candidate}, []]
        for candidates in invalid_candidates:
            with self.subTest(candidates=candidates):
                with self.assertRaises(ValueError):
                    candidate_snapshot(self.snapshot, candidates, self.snapshot["runtime"]["steps"])


if __name__ == "__main__":
    unittest.main()
