"""Standalone design files accept only canonical short typed suffixes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.api import CoatService, ServiceError
from coat_sim.core.design_files import (
    DESIGN_EXTENSIONS,
    DESIGN_SUFFIXES,
    LEGACY_DESIGN_EXTENSIONS,
    default_design_path,
    design_kind_from_path,
    validate_design_member_path,
    validate_design_path,
)
from coat_sim.core.input_snapshot import runtime_from_snapshot
from coat_sim.core import simulation_assets as assets
from tests.test_portable_asset_bundle import fixture_snapshot


class TypedDesignExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = CoatService(self.root)

    def _canonical_graph(self):
        rows = {row["kind"]: deepcopy(row["data"])
                for row in fixture_snapshot()["files"]}
        rows["machine"]["sources"][0]["file"] = "source.csrc"
        rows["machine"]["masks"][0]["file"] = "mask.cmsk"
        rows["process"]["machine_file"] = "machine.cmac"
        rows["process"]["substrate_file"] = "substrate.csub"
        (self.root / "designs").mkdir()
        paths = {kind: self.root / "designs" / (kind + extension)
                 for kind, extension in DESIGN_EXTENSIONS.items()
                 if kind != "film"}
        normalized = {}
        for kind in ("source", "substrate", "mask", "machine", "process"):
            normalized[kind] = self.service.save(kind, paths[kind], rows[kind]).data
        return normalized, paths

    def test_only_canonical_short_extensions_are_standalone_design_paths(self):
        self.assertEqual(DESIGN_EXTENSIONS, {
            "source": ".csrc", "substrate": ".csub", "mask": ".cmsk",
            "machine": ".cmac", "process": ".cproc", "film": ".cfilm",
        })
        self.assertEqual(DESIGN_SUFFIXES, frozenset(DESIGN_EXTENSIONS.values()))
        for kind, extension in DESIGN_EXTENSIONS.items():
            with self.subTest(kind=kind):
                self.assertEqual(default_design_path(kind, "new").name, "new" + extension)
                self.assertEqual(validate_design_path(kind, "saved" + extension).suffix, extension)
                self.assertEqual(design_kind_from_path("saved" + extension), kind)
                for suffix in (*LEGACY_DESIGN_EXTENSIONS.values(), ".json"):
                    with self.subTest(kind=kind, suffix=suffix):
                        with self.assertRaises(ValueError):
                            validate_design_path(kind, "old" + suffix)
                        with self.assertRaises(ValueError):
                            default_design_path(kind, "old" + suffix)
                        self.assertIsNone(design_kind_from_path("old" + suffix))
                for other, other_extension in DESIGN_EXTENSIONS.items():
                    if other != kind:
                        with self.assertRaises(ValueError):
                            validate_design_path(kind, "wrong" + other_extension)
        # Internal snapshots and .coatasset design members use the same suffixes.
        self.assertNotIn(".json", DESIGN_SUFFIXES)
        self.assertNotIn(".coatsource", DESIGN_SUFFIXES)
        self.assertNotIn(".coatasset", DESIGN_SUFFIXES)
        self.assertIsNone(design_kind_from_path("bundle.coatasset"))

    def test_all_five_deposition_designs_round_trip_with_canonical_extensions(self):
        rows, paths = self._canonical_graph()
        for kind in ("source", "substrate", "mask", "machine", "process"):
            with self.subTest(kind=kind):
                self.assertEqual(paths[kind].suffix, DESIGN_EXTENSIONS[kind])
                loaded = self.service.load(kind, paths[kind])
                self.assertTrue(loaded.valid, loaded.issues)
                self.assertEqual(loaded.data, rows[kind])
                self.assertTrue(paths[kind].read_bytes().startswith(b"{"))

    def test_old_paths_are_rejected_without_reading_or_changing_existing_files(self):
        rows = {row["kind"]: row["data"] for row in fixture_snapshot()["files"]}
        for kind, suffix in (
            *((kind, suffix) for kind, suffix in LEGACY_DESIGN_EXTENSIONS.items()),
            *((kind, ".json") for kind in DESIGN_EXTENSIONS if kind != "film"),
        ):
            with self.subTest(kind=kind, suffix=suffix):
                target = self.root / (kind + suffix)
                original = json.dumps(rows[kind], separators=(",", ":")).encode("utf-8")
                target.write_bytes(original)
                with self.assertRaises((ServiceError, ValueError)):
                    self.service.load(kind, target)
                with self.assertRaises((ServiceError, ValueError)):
                    self.service.save(kind, target, rows[kind], overwrite=True)
                self.assertEqual(target.read_bytes(), original)

    def test_wrong_suffix_or_payload_is_rejected_without_replacing_a_file(self):
        rows, _ = self._canonical_graph()
        source_path = self.root / "designs" / "source.csrc"
        original = source_path.read_bytes()
        with self.assertRaises((ServiceError, ValueError)):
            self.service.save("source", self.root / "source.cmsk", rows["source"])
        with self.assertRaises((ServiceError, ValueError)):
            self.service.save("source", source_path, rows["substrate"], overwrite=True)
        self.assertFalse((self.root / "source.cmsk").exists())
        self.assertEqual(source_path.read_bytes(), original)

    def test_asset_snapshot_keeps_its_internal_member_contract(self):
        portable = assets.portable_input_snapshot(fixture_snapshot())
        self.assertTrue(all(Path(row["path"]).suffix == DESIGN_EXTENSIONS[row["kind"]]
                            for row in portable["files"]))
        runtime_from_snapshot(portable)

    def test_old_internal_design_member_suffixes_are_rejected(self):
        for suffix in (".json", ".coatsource", ".coatmask"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    validate_design_member_path("source", "designs/source" + suffix)
                snapshot = fixture_snapshot()
                snapshot["files"][2]["path"] = "designs/source" + suffix
                with self.assertRaises(ValueError):
                    assets.portable_input_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
