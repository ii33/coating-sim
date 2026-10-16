"""Retired build guards and historical audit evidence; no bundler or network."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import hashlib

from tools import build_dependency_manager as build
from tools.dependency_manager import package_audit


class BuildContractTests(unittest.TestCase):
    def test_packaging_entry_is_retired(self):
        with patch.object(build.subprocess, "run") as run:
            self.assertEqual(build.main(["--dry-run"]), 2)
            self.assertEqual(build.main(["--test-build"]), 2)
            self.assertEqual(build.legacy_cxfreeze_validation(["--test-build"]), 2)
            run.assert_not_called()

    def test_notice_must_exist_and_be_nonempty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            empty = path / "empty"
            empty.touch()
            with self.assertRaisesRegex(RuntimeError, "Required licence"):
                build.copy_required([path / "missing", empty], path / "notice")
            good = path / "good"
            good.write_text("complete notice", encoding="utf-8")
            self.assertEqual(build.copy_required([good], path / "notice"), good)
            self.assertEqual((path / "notice").read_text(), "complete notice")

    def test_generated_startup_license_required(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "startup licence"):
                build.verify_payload(Path(directory))

    def test_build_tool_must_not_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "frozen_application_license.txt").write_text("MIT")
            (path / "patchelf").write_text("build-only")
            with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                build.verify_payload(path)

    def test_bundled_doctor_not_only_an_import(self):
        with patch.object(build.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({"status": "success", "frozen": False})
            with self.assertRaisesRegex(RuntimeError, "independent runtime"):
                build.bundled_doctor(Path("bundle"))
            run.return_value.stdout = json.dumps({"status": "success", "frozen": True, "tk_version": 8.6})
            self.assertEqual(build.bundled_doctor(Path("bundle"))["tk_version"], 8.6)
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_windows_missing_notice_blocks_before_bundler(self):
        with patch.object(build.sys, "platform", "win32"), patch.object(build.release_policy.__globals__["json"], "loads", return_value={"schema_version": 1, "platforms": {"win32": {"redistribution_approved": True, "evidence_files": ["README.md"]}}}), patch.object(build.subprocess, "run") as run:
            self.assertEqual(build.main([]), 2)
            run.assert_not_called()

    def test_unreviewed_platform_policy_blocks_binary_publication(self):
        with patch.object(build.sys, "platform", "darwin"), patch.object(build.subprocess, "run") as run:
            self.assertEqual(build.main([]), 2)
            run.assert_not_called()

    def test_approval_needs_archived_evidence(self):
        with patch.object(build.json, "loads", return_value={"schema_version": 1, "platforms": {build.sys.platform: {"redistribution_approved": True, "evidence_files": []}}}):
            with self.assertRaisesRegex(RuntimeError, "archived evidence"):
                build.release_policy()

    def test_approval_cannot_leave_requirement_false(self):
        policy = {"schema_version": 1, "platforms": {build.sys.platform: {
            "redistribution_approved": True, "evidence_files": ["README.md"],
            "requirements": {"rights_review": False},
        }}}
        with patch.object(build.json, "loads", return_value=policy):
            with self.assertRaisesRegex(RuntimeError, "unmet explicit"):
                build.release_policy()

    def test_approval_evidence_bytes_are_bound(self):
        policy = {"schema_version": 1, "platforms": {build.sys.platform: {
            "redistribution_approved": True, "evidence_files": ["README.md"],
            "evidence_sha256": {"README.md": "0" * 64},
        }}}
        with patch.object(build.json, "loads", return_value=policy):
            with self.assertRaisesRegex(RuntimeError, "evidence hash"):
                build.release_policy()


class NativePayloadTests(unittest.TestCase):
    def fixture(self, root):
        data = b"\x7fELFsynthetic-not-executable"
        path = root / "program"
        path.write_bytes(data)
        profile = {
            "native_files": [{"path": "program", "sha256": hashlib.sha256(data).hexdigest(), "needed": ["libc.so.6"]}],
            "direct_system_dependencies": ["libc.so.6"], "components": [],
            "bundled_runtime": {"tool_python": "3.12.15", "tk_version": 8.6},
        }
        return path, profile

    def test_exact_native_profile_and_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, profile = self.fixture(root)
            report = package_audit.audit_linux_payload(root, {"tool_python": "3.12.15 (build details)", "tk_version": 8.6}, profile)
            self.assertEqual(report["native_files_verified"], 1)
            with self.assertRaisesRegex(RuntimeError, "runtime differs"):
                package_audit.audit_linux_payload(root, {"tool_python": "3.13.16", "tk_version": 8.6}, profile)

    def test_changed_missing_and_unknown_native_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, profile = self.fixture(root)
            path.write_bytes(path.read_bytes() + b"change")
            with self.assertRaisesRegex(RuntimeError, "hash differs"):
                package_audit.audit_linux_payload(root, profile=profile)
            path.unlink()
            with self.assertRaisesRegex(RuntimeError, "missing"):
                package_audit.audit_linux_payload(root, profile=profile)
            self.fixture(root)
            (root / "innocent-name").write_bytes(b"\x7fELFunreviewed")
            with self.assertRaisesRegex(RuntimeError, "unreviewed"):
                package_audit.audit_linux_payload(root, profile=profile)

    def test_unreviewed_os_import_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, profile = self.fixture(root)
            profile["direct_system_dependencies"] = []
            with self.assertRaisesRegex(RuntimeError, "system dependency"):
                package_audit.audit_linux_payload(root, profile=profile)

    def test_zip_cannot_hide_gpl_or_build_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for member in ("_gdbm.cpython-312.so", "lib/libreadline.so.6", "tools/dependency_manager/package_audit.pyc", "pip/__init__.pyc"):
                with self.subTest(member=member):
                    with zipfile.ZipFile(root / "library.zip", "w") as archive:
                        archive.writestr(member, b"synthetic")
                    with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                        package_audit.audit_file_names(root)

    def test_relocation_verifies_inputs_and_only_removes_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "fontconfig"
            removed = root / "uuid"
            consumer.write_bytes(b"fontconfig")
            removed.write_bytes(b"uuid")
            profile = {"system_libuuid_transition": {
                "consumer": "fontconfig", "remove": "uuid",
                "original_consumer_sha256": package_audit.sha256(consumer),
                "original_removed_sha256": package_audit.sha256(removed),
            }}
            with patch.object(package_audit.shutil, "which", return_value="/build-only/patchelf"), patch.object(package_audit.subprocess, "run", side_effect=package_audit.subprocess.TimeoutExpired("patchelf", 30)):
                with self.assertRaises(package_audit.subprocess.TimeoutExpired):
                    package_audit.prepare_linux_payload(root, profile)
            self.assertTrue(removed.exists())
            with patch.object(package_audit.shutil, "which", return_value="/build-only/patchelf"), patch.object(package_audit.subprocess, "run") as run:
                package_audit.prepare_linux_payload(root, profile)
                self.assertEqual(run.call_args.args[0], ["/build-only/patchelf", "--replace-needed", "uuid", "libuuid.so.1", str(consumer)])
            self.assertFalse(removed.exists())

    def test_windows_only_fixed_official_cpython_dlls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for name in ("vcruntime140.dll", "vcruntime140_1.dll"):
                path = root / name
                path.write_bytes(name.encode())
                files[name] = {"sha256": package_audit.sha256(path)}
            provenance = root / "provenance.json"
            provenance.write_text(json.dumps({"schema_version": 1, "cpython_runtime_files": files}))
            self.assertEqual({path.name for path in package_audit.windows_runtime_sources(root, provenance)}, set(files))
            (root / "vcruntime140.dll").write_bytes(b"different-redist")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                package_audit.windows_runtime_sources(root, provenance)
            files["msvcp140.dll"] = {"sha256": "0" * 64}
            provenance.write_text(json.dumps({"schema_version": 1, "cpython_runtime_files": files}))
            with self.assertRaisesRegex(RuntimeError, "Unreviewed Windows"):
                package_audit.windows_runtime_sources(root, provenance)


if __name__ == "__main__":
    unittest.main()
