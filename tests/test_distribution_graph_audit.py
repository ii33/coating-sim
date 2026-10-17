"""Synthetic, state-free tests for the read-only distribution audit helper."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "tools/audit_distribution_graph.py"
SPEC = importlib.util.spec_from_file_location("distribution_graph_audit", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Distribution audit helper cannot be loaded")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)



class DistributionGraphAuditTest(unittest.TestCase):
    def write(self, root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_relative_import_module_registration_and_optional_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root, "coat_sim/__init__.py", "")
            self.write(root, "coat_sim/ui/__init__.py", "")
            self.write(root, "coat_sim/ui/view.py", "from .. import helper\ntry:\n import cupy\nexcept ImportError:\n pass\nROLE='coat_sim.helper'\n")
            self.write(root, "coat_sim/helper.py", "")
            graph = audit.collect_graph(root)
            edges = graph["edges"]
            self.assertTrue(any(e["kind"] == "python_from_submodule" and e["target"] == "coat_sim/helper.py" for e in edges))
            self.assertTrue(any(e["kind"] == "module_literal_candidate_not_execution" and e["target"] == "coat_sim/helper.py" for e in edges))
            cupy = next(d for d in graph["external_imports"] if d["import_root"] == "cupy")
            self.assertTrue(cupy["references"][0]["inside_try"])
            self.assertFalse(cupy["explicitly_declared"])
            self.assertEqual(graph["parse_failures"], [])

    def test_qml_literal_import_component_and_state_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root, "coat_sim/ui/qml/View.qml", 'import QtQuick\nimport "controls" as Controls\nControls.Button { source: "../assets/a.png" }\n')
            self.write(root, "coat_sim/ui/qml/controls/Button.qml", 'import QtQuick\nItem {}\n')
            self.write(root, "coat_sim/ui/assets/a.png", "synthetic-pixels")
            self.write(root, "coat_sim/ui/assets/session.sqlite3", "DO NOT READ")
            self.write(root, "coat_sim/ui/qml/__pycache__/private.qml", "DO NOT READ")
            graph = audit.collect_graph(root)
            names = {r["path"] for r in graph["resources"]}
            self.assertNotIn("coat_sim/ui/assets/session.sqlite3", names)
            self.assertNotIn("coat_sim/ui/qml/__pycache__/private.qml", names)
            self.assertTrue(any(e["kind"] == "qml_component_instantiation" for e in graph["edges"]))
            self.assertTrue(any(e["kind"] == "qml_resource_literal" and e["target"].endswith("a.png") for e in graph["edges"]))
            self.assertEqual(graph["qml_plugin_imports"][0]["module"], "QtQuick")

    def test_archive_member_and_extracted_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "validation.zip"
            value = b"source byte evidence\n"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("data/NOTICE.txt", value)
            manifest = [{"path": "data/NOTICE.txt", "size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}]
            result = audit.validate_archive(archive, manifest, root / "extracted")
            self.assertTrue(result["member_bytes_and_extracted_bytes_verified"])
            self.assertEqual((root / "extracted/data/NOTICE.txt").read_bytes(), value)

    def test_archive_rejects_traversal_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = b"x"
            for name, correct in (("../outside", True), ("C:/outside", True), ("safe", False)):
                archive = root / "unsafe.zip"
                with zipfile.ZipFile(archive, "w") as stream:
                    stream.writestr(name, value)
                manifest = [{"path": name, "size_bytes": 1, "sha256": hashlib.sha256(value).hexdigest() if correct else "bad"}]
                with self.assertRaises(ValueError):
                    audit.validate_archive(archive, manifest, root / "extracted")
                self.assertFalse((root / "extracted").exists())

    def test_archive_repeat_verifies_existing_extraction_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "validation.zip"
            value = b"source"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("safe", value)
            manifest = [{"path": "safe", "size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}]
            audit.validate_archive(archive, manifest, root / "extracted")
            audit.validate_archive(archive, manifest, root / "extracted")
            (root / "extracted/safe").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                audit.validate_archive(archive, manifest, root / "extracted")
            self.assertEqual((root / "extracted/safe").read_bytes(), b"changed")

    def test_qml_registration_and_documentation_resource_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root, "coat_sim/ui/preview.py", 'qmlRegisterType(Canvas, "CoatUi", 1, 0, "Canvas")\n')
            self.write(root, "coat_sim/ui/qml/View.qml", 'import CoatUi 1.0\nCanvas {}\n')
            self.write(root, "coat_sim/ui/assets/LICENSE", "license")
            self.write(root, "docs/page.html", '<a href="../coat_sim/ui/assets/LICENSE">notice</a>')
            graph = audit.collect_graph(root)
            plugin = graph["qml_plugin_imports"][0]
            self.assertEqual(plugin["python_registration_evidence"][0]["qml_type"], "Canvas")
            self.assertTrue(any(e["kind"] == "documentation_resource_link" for e in graph["edges"]))

    def test_fork_scripts_never_executed_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root, audit.FORK_SCRIPTS[0], 'raise RuntimeError("MUST NOT EXECUTE")\n')
            graph = audit.collect_graph(root)
            candidate = graph["fork_scripts"][0]
            self.assertTrue(candidate["present"])
            self.assertFalse(candidate["executed"])
            self.assertTrue((root / candidate["path"]).is_file())
            self.assertIn("UNKNOWN", candidate["ownership"])

    def archive(self, root, names):
        archive = root / "validation.zip"
        manifest = []
        with zipfile.ZipFile(archive, "w") as stream:
            for name in names:
                value = b"source"
                stream.writestr(name, value)
                manifest.append({"path": name, "size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()})
        return archive, manifest

    def test_archive_rejects_noncanonical_or_windows_stream_paths_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("./safe", "safe//file", "safe/./file", "data/file:stream", "safe/", "",
                         "safe/file?", "safe/file*", "safe/file<", "safe/file>", 'safe/file"',
                         "safe/file|", "safe/file.", "safe/file ", "safe/dir./file", "safe/dir /file",
                         "safe/CON", "safe/nul.txt", "safe/COM1", "safe/lpt9.txt", "safe/control\x01"):
                with self.subTest(name=name):
                    archive, manifest = self.archive(root, [name])
                    with self.assertRaises(ValueError):
                        audit.validate_archive(archive, manifest, root / "extracted")
                    self.assertFalse((root / "extracted").exists())

    def test_archive_accepts_legal_nested_and_unicode_file_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["docs/portable-bundle.md", "data/资料/NOTICE.txt", "launcher.py"]
            archive, manifest = self.archive(root, names)
            result = audit.validate_archive(archive, manifest, root / "extracted")
            self.assertEqual(result["member_count"], 3)
            self.assertTrue(result["canonical_file_names_verified"])

    def test_archive_rejects_case_and_file_directory_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for names in (["file", "FILE"], ["file", "file/child"], ["File", "file/child"]):
                with self.subTest(names=names):
                    archive, manifest = self.archive(root, names)
                    with self.assertRaises(ValueError):
                        audit.validate_archive(archive, manifest, root / "extracted")
                    self.assertFalse((root / "extracted").exists())

    def test_archive_rejects_existing_file_directory_and_root_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, manifest = self.archive(root, ["safe"])
            outside = root / "outside"
            outside.mkdir()
            (outside / "safe").write_bytes(b"source")
            file_link = root / "file-link"
            file_link.mkdir()
            (file_link / "safe").symlink_to(outside / "safe")
            directory_link = root / "directory-link"
            directory_link.mkdir()
            (directory_link / "linked").symlink_to(outside, target_is_directory=True)
            root_link = root / "root-link"
            root_link.symlink_to(outside, target_is_directory=True)
            for extraction in (file_link, directory_link, root_link, root_link / "new-extraction"):
                with self.subTest(extraction=extraction):
                    with self.assertRaises(ValueError):
                        audit.validate_archive(archive, manifest, extraction)
            self.assertEqual((outside / "safe").read_bytes(), b"source")
            self.assertFalse((outside / "new-extraction").exists())

    def test_archive_rejects_zip_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "linked.zip"
            value = b"outside"
            member = zipfile.ZipInfo("safe")
            member.create_system = 3
            member.external_attr = 0o120777 << 16
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr(member, value)
            manifest = [{"path": "safe", "size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}]
            with self.assertRaises(ValueError):
                audit.validate_archive(archive, manifest, root / "extracted")
            self.assertFalse((root / "extracted").exists())

    def documentation_layout(self, root):
        cie = "data/film_design/cie-illuminants-20261006/"
        contents = {name: "guide" for name in audit.DOCUMENTATION_FILES}
        contents["README.md"] = "\n".join(f"[guide]({name})" for name in (
            "docs/portable-bundle.md", "docs/portable-assets.md", "docs/installation.md"))
        contents["docs/algorithms.html"] = (
            '<section id="cie"><a href="../' + cie + 'NOTICE_DATA.md">notice</a>'
            '<a href="../' + cie + 'LICENSE_DATA_CC_BY_SA_4_0.txt">license</a></section>')
        contents["docs/software_manual.html"] = '<a href="portable-bundle.md">guide</a><section id="workflow"></section><a href="#workflow">workflow</a>'
        contents["coat_sim/agent/resources/software_manual.md"] = "Complete steps: docs/portable-bundle.md"
        contents[cie + "NOTICE_DATA.md"] = "data notice"
        contents[cie + "LICENSE_DATA_CC_BY_SA_4_0.txt"] = "data license"
        for name, text in contents.items():
            self.write(root, name, text)
        return [{"path": name, "size_bytes": (root / name).stat().st_size, "sha256": audit.digest(root / name)} for name in contents]

    def test_documentation_required_license_guide_links_and_manifest_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.documentation_layout(root)
            result = audit.documentation_validation(root, manifest, root)
            self.assertTrue(result["passed"])
            self.assertTrue(result["all_local_references_closed"])
            self.assertEqual(len(result["required_document_links"]), 6)
            self.assertEqual(len(result["license_notice_inventory"]), 2)
            self.assertFalse(result["runtime_third_party_license_completeness_verified"])

    def test_documentation_missing_required_link_target_and_bad_anchor_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.documentation_layout(root)
            manifest = [record for record in manifest if record["path"] != "docs/portable-bundle.md"]
            path = root / "docs/software_manual.html"
            path.write_text(path.read_text().replace("#workflow", "#missing"))
            for record in manifest:
                if record["path"] == "docs/software_manual.html":
                    record.update(size_bytes=path.stat().st_size, sha256=audit.digest(path))
            result = audit.documentation_validation(root, manifest, root)
            self.assertFalse(result["passed"])
            self.assertIn("docs/portable-bundle.md", result["missing_documents"])
            self.assertTrue(any(row["status"] == "missing_html_anchor" for row in result["unresolved_local_references"]))

    def test_documentation_remote_render_dependency_fails_but_external_reference_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.documentation_layout(root)
            path = root / "docs/algorithms.html"
            path.write_text(path.read_text() + '<a href="https://example.invalid/reference">citation</a>')
            for record in manifest:
                if record["path"] == "docs/algorithms.html":
                    record.update(size_bytes=path.stat().st_size, sha256=audit.digest(path))
            self.assertTrue(audit.documentation_validation(root, manifest, root)["passed"])
            path.write_text(path.read_text() + '<script src="https://example.invalid/script.js"></script>')
            result = audit.documentation_validation(root, manifest, root)
            self.assertFalse(result["passed"])
            self.assertEqual(len(result["remote_html_render_dependencies"]), 1)
            self.assertFalse(result["external_urls_requested"])

    def test_dependency_inventory_includes_remote_service_declarations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root, "requirements-remote-service.txt", "numpy>=1.24\npydantic>=2.12,<3\n")
            declarations = audit.declared_dependencies(root)
            self.assertEqual([row["distribution"] for row in declarations], ["numpy", "pydantic"])
            self.assertEqual(declarations[0]["source"], "requirements-remote-service.txt")

    def test_reference_manifest_preserves_byte_mismatch_even_if_crlf_equivalent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = b"row\r\nnext\r\n"
            sample = root / "data/reference/sample.csv"
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(b"row\nnext\n")
            self.write(root, "data/reference/MANIFEST.json", __import__("json").dumps({"sample.csv": {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}}))
            result = audit.reference_manifest_validation(root, "data/reference/MANIFEST.json")
            self.assertFalse(result["passed"])
            self.assertTrue(result["mismatches"][0]["lf_to_crlf_only_would_match_recorded_bytes"])
            self.assertFalse(result["data_or_manifest_modified"])
            (root / "data/reference/sample.csv").write_bytes(value)
            self.assertTrue(audit.reference_manifest_validation(root, "data/reference/MANIFEST.json")["passed"])


    def test_repository_cie_manifest_preserves_all_original_bytes(self):
        root = SCRIPT.parents[1]
        result = audit.reference_manifest_validation(
            root, "data/film_design/cie-illuminants-20261006/MANIFEST_SHA256.json")
        self.assertEqual(result["file_count"], 27)
        self.assertTrue(result["passed"], result["mismatches"])

    def test_repository_attributes_disable_text_conversion_for_exact_cie_files_only(self):
        root = SCRIPT.parents[1]
        rules = [line.split() for line in (root / ".gitattributes").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        cie_rules = [rule for rule in rules
                     if rule[0].startswith("/data/film_design/cie-illuminants-20261006/")]
        manifest_path = root / "data/film_design/cie-illuminants-20261006/MANIFEST_SHA256.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_members = {
            "/data/film_design/cie-illuminants-20261006/" + name
            for name in manifest
        }
        self.assertEqual(len(manifest_members), 27)
        self.assertEqual({rule[0] for rule in cie_rules}, manifest_members)
        self.assertEqual(len(cie_rules), len(manifest_members))
        crlf_members = {
            "/data/film_design/cie-illuminants-20261006/" + name
            for name in (
                "data/A_1nm.csv", "data/D50_1nm.csv", "data/D65_1nm.csv",
                "data/E_1nm.csv", "data/V_1nm.csv", "data/XYZ1931_2_1nm.csv",
                "upstream/CIE_sle_photopic.csv",
                "upstream/CIE_sle_photopic.csv_metadata.json",
                "upstream/CIE_std_illum_A_1nm.csv",
                "upstream/CIE_std_illum_D50.csv",
                "upstream/CIE_std_illum_D50.csv_metadata.json",
                "upstream/CIE_std_illum_D65.csv",
                "upstream/CIE_xyz_1931_2deg.csv",
                "upstream/CIE_xyz_1931_2deg.csv_metadata.json",
            )
        }
        self.assertEqual(len(crlf_members), 14)
        for rule in cie_rules:
            pattern, *attributes = rule
            self.assertTrue(pattern.startswith("/data/film_design/cie-illuminants-20261006/"))
            self.assertFalse(any(char in pattern for char in "*?[]"))
            if pattern in crlf_members:
                self.assertEqual(attributes, ["-text", "whitespace=cr-at-eol"])
                self.assertIn(b"\r\n", (root / pattern.lstrip("/")).read_bytes())
            else:
                self.assertEqual(attributes, ["-text"])
                data = (root / pattern.lstrip("/")).read_bytes()
                self.assertNotIn(b"\r", data)
                self.assertIn(b"\n", data)
        result = audit.reference_manifest_validation(
            root, "data/film_design/cie-illuminants-20261006/MANIFEST_SHA256.json")
        self.assertTrue(result["passed"], result["mismatches"])



if __name__ == "__main__":
    unittest.main()
