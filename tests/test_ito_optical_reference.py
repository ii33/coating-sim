"""Pinned author-supplied ITO table, licensing and append-only routing."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, ClassVar
import unittest

from coat_sim.core.material_research import research_material_records
from coat_sim.core.materials import builtin_material_profiles, material_completeness
from coat_sim.core.optical_constants import evaluate_optical_set, validate_optical_set
from tools.import_optical_catalog import append_package_catalog, validate_package_catalog


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data/optical_catalog_sources/rii_ito_minenkov_20261018_v1"
SET_ID = "ri_ito_minenkov_glass_2024"
COMMIT = "c5c2f188e848453def5970e347399d653df2ffc2"


def read_json(name):
    return json.loads((ARCHIVE / name).read_text(encoding="utf-8"))


def canonical_hash(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ItoOpticalReferenceTests(unittest.TestCase):
    package: ClassVar[dict[str, Any]]
    provenance: ClassVar[dict[str, Any]]
    record: ClassVar[dict[str, Any]]
    catalog: ClassVar[dict[str, Any]]
    group: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls):
        cls.package = read_json("package.json")
        cls.provenance = read_json("provenance.json")
        cls.record = read_json("import_record.json")
        cls.catalog = json.loads((ROOT / "data/optical_catalog.json").read_text(encoding="utf-8"))
        cls.group = cls.package["materials"][0]["optical_constants"][0]

    def test_exact_raw_bytes_license_and_fixed_commit(self):
        raw = (ARCHIVE / "raw/Minenkov-glass.yml").read_bytes()
        license_bytes = (ARCHIVE / "refractiveindex_LICENSE").read_bytes()
        self.assertEqual(len(raw), 23346)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "2861e8774e0d8a95f485ea56721a70d239bf43ff7e11f48c1f60b12a551fad78")
        self.assertEqual(hashlib.sha256(license_bytes).hexdigest(),
                         "36ffd9dc085d529a7e60e1276d73ae5a030b020313e6c5408593a6ae2af39673")
        self.assertIn(b"CC0 1.0 Universal", license_bytes)
        self.assertIn(b"copyright and related rights waived via CC0 1.0", raw)
        self.assertEqual(self.provenance["source_commit"], COMMIT)
        self.assertIn(COMMIT, self.provenance["source_url"])
        self.assertNotIn("/master/", self.provenance["source_url"])
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        self.assertEqual(blob, self.provenance["source_git_blob_sha1"])

    def test_all_numeric_rows_match_original_without_extra_samples(self):
        text = (ARCHIVE / "raw/Minenkov-glass.yml").read_text(encoding="utf-8")
        pattern = r"^\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s*$"
        rows = [tuple(map(float, match)) for match in re.findall(pattern, text, re.MULTILINE)]
        self.assertEqual(len(rows), 706)
        self.assertEqual(list(zip(self.group["wavelengths"], self.group["n"],
                                  self.group["k"], strict=True)), rows)
        self.assertEqual(rows[0], (0.19146, 2.04232, 0.59569))
        self.assertEqual(rows[-1], (1.68869, 0.23337, 1.77193))
        validate_optical_set(self.group)

    def test_exact_role_routing_and_no_default_metadata(self):
        summary = validate_package_catalog(self.package)
        self.assertEqual(summary["material_count"], 1)
        self.assertEqual(summary["group_count"], 1)
        row = self.package["materials"][0]
        self.assertEqual(row["material_id"], "ITO_film")
        self.assertEqual(row["builtin_material_id"], "builtin_research_ITO_film")
        self.assertEqual(row["category"], "film")
        self.assertEqual(self.group["id"], SET_ID)
        self.assertFalse(self.provenance["adaptation"]["automatic_default_selection"])
        current = next(m for m in self.catalog["materials"] if m["material_id"] == "ITO_film")
        self.assertEqual(current, row)

    def test_table_domain_is_not_a_measurement_domain_claim(self):
        self.assertEqual(self.provenance["declared_table_range_um"], [0.19146, 1.68869])
        self.assertIsNone(self.provenance["independently_verified_measurement_range_um"])
        self.assertIsNone(self.provenance["independently_verified_fit_range_um"])
        self.assertIn("joint_ellipsometry_transmission", self.provenance["representation"])
        self.assertEqual(self.provenance["conditions"]["film_thickness_nm"], 110.0)
        self.assertEqual(self.provenance["conditions"]["substrate"], "XY15S glass")
        self.assertIsNone(self.provenance["conditions"]["deposition_temperature_C"])
        self.assertIsNone(self.provenance["conditions"]["deposition_atmosphere"])
        self.assertIn("未核定", self.group["notes"])

    def test_endpoints_and_domain_rejection(self):
        self.assertEqual(evaluate_optical_set(self.group, 0.19146, "um"), (2.04232, 0.59569))
        self.assertEqual(evaluate_optical_set(self.group, 1688.69, "nm"), (0.23337, 1.77193))
        for wave in (191.45, 1688.70):
            with self.subTest(wave=wave), self.assertRaises(ValueError):
                evaluate_optical_set(self.group, wave, "nm")
        for wave in (380.0, 400.0, 780.0):
            n, k = evaluate_optical_set(self.group, wave, "nm")
            self.assertGreater(n, 0)
            self.assertGreaterEqual(k, 0)

    def test_existing_catalog_rows_and_root_metadata_preserved(self):
        rows = [row for row in self.catalog["materials"] if row["material_id"] != "ITO_film"]
        metadata = {key: value for key, value in self.catalog.items()
                    if key not in ("materials", "datasets")}
        self.assertEqual(canonical_hash(rows), self.record["preserved_material_rows_sha256"])
        self.assertEqual(canonical_hash(metadata), self.record["preserved_root_metadata_sha256"])

    def test_reimport_noop_and_conflicting_same_id_rejected(self):
        self.assertEqual(append_package_catalog(self.catalog, self.package), self.catalog)
        changed = deepcopy(self.package)
        changed["materials"][0]["optical_constants"][0]["n"][0] += 0.1
        with self.assertRaisesRegex(ValueError, "conflicts"):
            append_package_catalog(self.catalog, changed)

    def test_research_snapshot_and_source_defaults_remain_uncalibrated(self):
        material = next(row for row in builtin_material_profiles() if row["id"] == "builtin_research_ITO_film")
        original_research = next(row for row in research_material_records() if row["id"] == "ITO_film")
        self.assertEqual(material["research_data"], original_research)
        self.assertEqual(material["optical_constants"], [validate_optical_set(self.group)])
        completeness = material_completeness(material)
        self.assertFalse(completeness["thermodynamic_rate_available"])
        for model in completeness["models"].values():
            self.assertFalse(model["calibration_declared"])


if __name__ == "__main__":
    unittest.main()
