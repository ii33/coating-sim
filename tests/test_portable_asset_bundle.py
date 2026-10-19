"""Portable .coatasset inputs/results and defensive import/export contracts."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

from coat_sim.core.api import CoatService
from coat_sim.core import simulation_assets as assets


def fixture_snapshot():
    source = {"schema_version": 2, "kind": "point"}
    mask = {"z_mm": 500.0, "outer": [[0, 0], [1, 0], [0, 1]], "holes": []}
    machine = {"schema_version": 5, "carrier_x_mm": 0, "carrier_y_mm": 0,
        "planet_offset_mm": 312, "planet_tilt_deg": 0, "substrate_z_mm": 1000,
        "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 4,
        "sources": [{"file": "source.csrc", "x_mm": 300, "y_mm": 0, "z_mm": 0}],
        "masks": [{"file": "mask.cmsk"}], "monitors": []}
    substrate = {"schema_version": 1, "name": "test substrate",
        "outline": {"kind": "circle", "radius_mm": 15}, "surface": {"kind": "flat"}}
    process = {"schema_version": 6, "name": "recipe", "machine_file": "machine.cmac",
        "substrate_file": "substrate.csub", "steps": [{"source_slot": 1, "target_nm": 10,
        "rate_nm_s": 1, "target_mode": "mean"}]}
    rows = []
    for kind, data in [("process", process), ("machine", machine), ("source", source),
                       ("mask", mask), ("substrate", substrate)]:
        rows.append({"kind": kind, "path": f"designs/{kind}{assets.design_extension(kind)}", "data": data,
            "sha256": hashlib.sha256(assets._json_bytes(data)).hexdigest(),
            "original_path": "/nonexistent/private/never-read.json"})
    return {"kind": "coat_sim.input_snapshot", "version": 1,
            "process_path": "designs/process.cproc", "files": rows,
            "runtime": {"never": "/nonexistent/private/never-read.json"},
            "parameters": {"backend": "CPU", "fast_cycle": False, "secret": "excluded"}}


class PortableAssetBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def save(self, **kwargs):
        return assets.save_simulation_input_asset(self.root, name="fixture", settings={},
            input_snapshot=fixture_snapshot(), confirmed=True, **kwargs)["asset_path"]

    def rewrite(self, path, edit, info_edit=None):
        with zipfile.ZipFile(path) as archive:
            content = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(content["manifest.json"])
        edit(content, manifest)
        content["manifest.json"] = assets._json_bytes(manifest)
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in content.items():
                info = zipfile.ZipInfo(name)
                if info_edit:
                    info_edit(info)
                archive.writestr(info, value)

    def test_inputs_only_read_reconstruct_and_export_after_move(self):
        path = self.save()
        content = assets.read_simulation_asset(path, detail="full", include_arrays=True)
        self.assertFalse(content["has_results"])
        self.assertEqual(content["arrays"], {})
        self.assertEqual({row["kind"] for row in content["designs"]}, assets.DESIGN_KINDS)
        with zipfile.ZipFile(path) as archive:
            config = archive.read("configuration.json")
            self.assertNotIn(b"never-read", config)
            self.assertNotIn(b"excluded", config)
            self.assertTrue(all("role" in row and "sha256" in row for row in content["members"]))
        other = self.root / "moved" / "independent.coatasset"
        other.parent.mkdir()
        shutil.move(path, other)
        process = next(row["path"] for row in content["designs"] if row["kind"] == "process")
        exported = assets.export_simulation_asset(other, self.root / "extracted", members=[process])
        self.assertEqual(len(exported["designs"]), 5)
        process_path = next(row["path"] for row in exported["designs"] if row["kind"] == "process")
        loaded = CoatService(self.root).load("process", process_path)
        self.assertTrue(loaded.valid, loaded.errors)
        self.assertEqual(loaded.data["name"], "recipe")

    def test_export_leaf_does_not_export_unrelated_designs(self):
        path = self.save()
        content = assets.read_simulation_asset(path, detail="designs")
        source = next(row["path"] for row in content["designs"] if row["kind"] == "source")
        exported = assets.export_simulation_asset(path, self.root / "source", members=[source])
        self.assertEqual(len(exported["files"]), 1)
        self.assertEqual(exported["designs"][0]["kind"], "source")

    def test_collision_preflight_does_not_partially_publish(self):
        path = self.save()
        content = assets.read_simulation_asset(path, detail="full")
        process = next(row["path"] for row in content["designs"] if row["kind"] == "process")
        output = self.root / "export"
        conflict = output / content["designs"][-1]["path"]
        conflict.parent.mkdir(parents=True)
        conflict.write_text("existing")
        plan = assets.plan_simulation_asset_export(path, output, members=[process])
        self.assertEqual(plan["collisions"], [str(conflict)])
        with self.assertRaises(FileExistsError):
            assets.export_simulation_asset(path, output, members=[process])
        self.assertEqual([p for p in output.rglob("*") if p.is_file()], [conflict])
        self.assertEqual(conflict.read_text(), "existing")
        with self.assertRaisesRegex(ValueError, "类型"):
            assets.export_simulation_asset(path, output, members=[process], overwrite=True)
        prior = deepcopy(content["designs"][-1]["data"])
        prior["description"] = "existing same-kind design"
        conflict.write_text(json.dumps(prior))
        assets.export_simulation_asset(path, output, members=[process], overwrite=True)
        self.assertNotIn("existing same-kind design", conflict.read_text())

    def test_missing_dependencies_cannot_be_exported_as_portable(self):
        path = self.save()
        content = assets.read_simulation_asset(path, detail="designs")
        process = next(row["path"] for row in content["designs"] if row["kind"] == "process")
        with self.assertRaisesRegex(ValueError, "依赖"):
            assets.export_simulation_asset(path, self.root / "export", members=[process], include_dependencies=False)

    def test_export_rejects_symlink_parent(self):
        path = self.save()
        output = self.root / "export"
        output.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (output / "designs").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            assets.export_simulation_asset(path, output)
        self.assertFalse(list(elsewhere.iterdir()))

    def test_save_confirmation_and_overwrite(self):
        with self.assertRaises(ValueError):
            assets.save_simulation_input_asset(self.root, name="fixture", settings={},
                input_snapshot=fixture_snapshot(), confirmed=False)
        self.save()
        with self.assertRaises(FileExistsError):
            self.save()
        self.save(overwrite=True)

    def test_frozen_data_hash_and_no_original_file_reads(self):
        snapshot = fixture_snapshot()
        frozen = assets.portable_input_snapshot(snapshot)
        self.assertEqual(len(frozen["files"]), 5)
        snapshot["files"][0]["data"]["name"] = "tampered"
        with self.assertRaisesRegex(ValueError, "校验"):
            assets.portable_input_snapshot(snapshot)
        self.assertEqual(frozen["files"][0]["data"]["name"], "recipe")

    def test_reject_arbitrary_secret_or_code_design(self):
        for modification in (lambda row: row.update(kind="python"),
                             lambda row: row["data"].update(api_key="never include"),
                             lambda row: row.update(path="designs/run.py")):
            snapshot = fixture_snapshot()
            row = snapshot["files"][2]
            modification(row)
            row["sha256"] = hashlib.sha256(assets._json_bytes(row["data"])).hexdigest()
            with self.assertRaises(ValueError):
                assets.portable_input_snapshot(snapshot)

    def test_old_frozen_file_contract_is_rejected_without_reading_files(self):
        snapshot = fixture_snapshot()
        directory = self.root / "frozen"
        directory.mkdir()
        for row in snapshot["files"]:
            target = directory / Path(row["path"]).name
            target.write_bytes(assets._json_bytes(row["data"]))
        for legacy in (
            {"directory": str(directory), "process_path": str(directory / "process.cproc")},
            {"manifest_file": str(directory / "manifest.json"),
             "files": [{"kind": row["kind"], "snapshot_path": str(directory / Path(row["path"]).name)}
                       for row in snapshot["files"]]},
        ):
            with self.assertRaisesRegex(ValueError, "in-memory typed"):
                assets.portable_input_snapshot(legacy)

    def test_old_frozen_dependency_cannot_escape_or_use_original(self):
        directory = self.root / "frozen"
        directory.mkdir()
        secret = self.root / "secret.json"
        secret.write_text('{"schema_version":2,"kind":"point"}')
        snapshot = {"directory": str(directory), "source_path": str(secret)}
        with self.assertRaisesRegex(ValueError, "in-memory typed"):
            assets.portable_input_snapshot(snapshot)

    def test_legacy_v1_is_rejected_without_modifying_bundle(self):
        result = {"field": np.ones((2, 2)), "x": np.arange(2.), "y": np.arange(2.)}
        with patch("coat_sim.core.result_visualization.render_result_visualizations", return_value={"files": []}):
            receipt = assets.prepare_simulation_asset(self.root, result=result, settings={}, machine=None,
                process=None, elapsed_s=1, requested_device="CPU")
        path = assets.save_simulation_asset(self.root, receipt["run_id"], "legacy", confirmed=True)["asset_path"]
        def legacy(content, manifest):
            manifest["schema_version"] = 1
        self.rewrite(path, legacy)
        original = Path(path).read_bytes()
        with self.assertRaises(ValueError):
            assets.read_simulation_asset(path, include_arrays=True)
        self.assertEqual(Path(path).read_bytes(), original)

    def test_bad_checksums_and_typed_roles_rejected(self):
        for mutation in (lambda content, manifest: content.update({"summary.json": b"{}"}),
                         lambda content, manifest: manifest["entries"][0].update(role="script"),
                         lambda content, manifest: manifest["entries"].append(dict(manifest["entries"][0]))):
            path = self.save(overwrite=True)
            self.rewrite(path, mutation)
            with self.assertRaises(ValueError):
                assets.read_simulation_asset(path)

    def test_zip_traversal_aliases_code_symlinks_rejected(self):
        for member in ("../escape.json", "/tmp/escape.json", "designs/../escape.json", "designs\\escape.json",
                       "designs/C:evil.json", "designs/a//b.json", "designs/CON.json", "script.py"):
            path = self.save(overwrite=True)
            def append(content, manifest):
                payload = b"{}"
                content[member] = payload
                manifest["entries"].append({"path": member, "role": "design", "kind": "source", "size": 2,
                    "sha256": hashlib.sha256(payload).hexdigest()})
            self.rewrite(path, append)
            with self.subTest(member=member), self.assertRaises(ValueError):
                assets.read_simulation_asset(path)
        path = self.save(overwrite=True)
        self.rewrite(path, lambda c, m: None,
            lambda info: setattr(info, "external_attr", (stat.S_IFLNK | 0o777) << 16))
        with self.assertRaisesRegex(ValueError, "链接"):
            assets.read_simulation_asset(path)

    def test_nested_npz_rejects_object_arrays_and_unknown_code_without_loading(self):
        result = {"field": np.ones((2, 2)), "x": np.arange(2.), "y": np.arange(2.)}
        with patch("coat_sim.core.result_visualization.render_result_visualizations", return_value={"files": []}):
            receipt = assets.prepare_simulation_asset(self.root, result=result, settings={}, machine=None,
                process=None, elapsed_s=1, requested_device="CPU")
        for unsafe in ("object", "code"):
            path = assets.save_simulation_asset(self.root, receipt["run_id"], "numeric", confirmed=True,
                overwrite=True)["asset_path"]
            payload = io.BytesIO()
            field = np.full((2, 2), "payload", dtype=object) if unsafe == "object" else result["field"]
            np.savez(payload, field=field, x=result["x"], y=result["y"])
            if unsafe == "code":
                with zipfile.ZipFile(payload, "a") as nested:
                    nested.writestr("run.py", "raise RuntimeError('must never execute')")
            def corrupt(content, manifest):
                content["result.npz"] = payload.getvalue()
                entry = next(row for row in manifest["entries"] if row["path"] == "result.npz")
                entry.update(size=len(content["result.npz"]),
                             sha256=hashlib.sha256(content["result.npz"]).hexdigest())
            self.rewrite(path, corrupt)
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                assets.read_simulation_asset(path, include_arrays=False)

    def test_nonportable_duplicate_json_is_rejected(self):
        path = self.save()
        def corrupt(content, manifest):
            content["summary.json"] = b'{"run_id":"first","run_id":"second"}'
            row = next(row for row in manifest["entries"] if row["path"] == "summary.json")
            row.update(size=len(content["summary.json"]), sha256=hashlib.sha256(content["summary.json"]).hexdigest())
        self.rewrite(path, corrupt)
        with self.assertRaisesRegex(ValueError, "重复"):
            assets.read_simulation_asset(path)

    def test_portable_refs_checked_even_if_checksums_are_repaired(self):
        path = self.save()
        def corrupt(content, manifest):
            name = next(row["path"] for row in manifest["entries"] if row.get("kind") == "process")
            data = json.loads(content[name])
            data["machine_file"] = "../../../secret.json"
            content[name] = assets._json_bytes(data)
            entry = next(row for row in manifest["entries"] if row["path"] == name)
            entry.update(size=len(content[name]), sha256=hashlib.sha256(content[name]).hexdigest())
        self.rewrite(path, corrupt)
        with self.assertRaises(ValueError):
            assets.read_simulation_asset(path)


if __name__ == "__main__":
    unittest.main()
