"""Read-only Windows binding tests, using inert files, not executable runtimes.

Run directly with stdlib unittest, or collect with pytest. Fixture base pins are
local test constants, never an injectable trust option in the public loader.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from coat_sim import compute_runtime_manifest as runtime


def sha(data):
    return hashlib.sha256(data).hexdigest()


def base_digest(files):
    return sha("".join(f"{name}\0{record['sha256']}\0{record['size']}\n"
                       for name, record in sorted(files.items())).encode())


CPU = {"numpy": "2.2.6", "scipy": "1.16.0", "matplotlib": "3.10.0", "pillow": "11.3.0",
       "pydantic": "2.12.0"}
APP = dict(CPU, **{"pyside6-essentials": "6.8.3", "pyside6-addons": "6.8.3", "mcp": "2.0.0",
                  "socksio": "1.0.0", "keyring": "25.6.0", "ezdxf": "1.4.4", "openpyxl": "3.1.5",
                  "pypdf": "6.21.0", "pyyaml": "6.0.3", "pydantic-ai-slim": "2.51.0",
                  "pydantic-ai-harness": "0.36.0", "fastmcp-slim": "4.0.10"})


class RuntimeFixture:
    def __init__(self, root, *, optional=True, cuda="cuda12"):
        self.root = root
        self.base = root / ".runtime/python"
        self.base.mkdir(parents=True)
        self.base_files = {"python.exe": b"MZ test CPython, never executable",
                           "python312.dll": b"inert dll",
                           "Lib/venv/scripts/nt/python.exe": b"MZ inert redirector"}
        self.files = {}
        for name, content in self.base_files.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.files[name] = {"sha256": sha(content), "size": len(content)}
        self.marker: dict[str, Any] = {"schema_version": 1, "version": runtime.BASE_VERSION,
                       "package_sha256": runtime.BASE_PACKAGE_SHA256, "files": self.files}
        self.write_marker()
        digest = base_digest(self.files)
        self.pins = patch.multiple(runtime, BASE_INVENTORY_SHA256=digest, BASE_FILE_COUNT=len(self.files))
        self.pins.start()
        self.data: dict[str, Any] = {"schema_version": 1, "platform": "win32", "architecture": "x86_64",
                     "base": {"root": ".runtime/python", "python": ".runtime/python/python.exe",
                              "version": runtime.BASE_VERSION, "inventory_sha256": digest},
                     "environments": {}, "roles": {"app": "app", "deposition_cpu": "app"}}
        self.add_environment("app", ".venv", "cpu", APP)
        if optional:
            self.add_environment("deposition_directml", ".runtime/envs/deposition-directml", "directml",
                                 dict(CPU, **{"torch-directml": "0.2.5.dev240914", "torch": "2.4.1",
                                              "torchvision": "0.19.1"}))
            self.add_environment("deposition_cuda", ".runtime/envs/deposition-cuda", cuda,
                                 dict(CPU, **{f"cupy-{cuda}x": "14.2.0",
                                              "cuda-toolkit": "12.1.1" if cuda == "cuda12" else "13.0.0"}))
            self.add_environment("appearance", ".runtime/envs/appearance", "cpu",
                                 dict(CPU, mitsuba="3.9.0", drjit="1.4.0"))
        self.write()

    def write_marker(self):
        (self.base / ".coating-python.json").write_text(json.dumps(self.marker), encoding="utf-8")

    def add_environment(self, name, relative, backend, packages):
        directory = self.root / relative
        (directory / "Scripts").mkdir(parents=True)
        python = directory / "Scripts/python.exe"
        python.write_bytes(self.base_files["Lib/venv/scripts/nt/python.exe"])
        config = directory / "pyvenv.cfg"
        config.write_text(f"home = {self.base}\ninclude-system-site-packages = false\n"
                          f"version = {runtime.BASE_VERSION}\nexecutable = {self.base / 'python.exe'}\n",
                          encoding="utf-8")
        record = {"root": relative, "python": relative + "/Scripts/python.exe", "backend": backend,
                  "python_sha256": sha(python.read_bytes()), "pyvenv_cfg_sha256": sha(config.read_bytes()),
                  "packages": dict(packages), "packages_sha256": runtime.package_inventory_digest(packages)}
        self.data["environments"][name] = record
        self.data["roles"][name] = name
        self.write_metadata(name)

    def write_metadata(self, name):
        record = self.data["environments"][name]
        site = self.root / record["root"] / "Lib/site-packages"
        site.mkdir(parents=True, exist_ok=True)
        for old in site.glob("*.dist-info/METADATA"):
            old.unlink()
            old.parent.rmdir()
        for package, version in record["packages"].items():
            metadata = site / f"{package.replace('-', '_')}-{version}.dist-info/METADATA"
            metadata.parent.mkdir()
            metadata.write_text(f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n\n", encoding="utf-8")
        record["packages_sha256"] = runtime.package_inventory_digest(record["packages"])

    def refresh_config(self, name, transform):
        record = self.data["environments"][name]
        path = self.root / record["root"] / "pyvenv.cfg"
        path.write_text(transform(path.read_text()), encoding="utf-8")
        record["pyvenv_cfg_sha256"] = sha(path.read_bytes())
        self.write()

    def write(self):
        (self.root / runtime.MANIFEST_PATH).write_text(json.dumps(self.data), encoding="utf-8")

    def close(self):
        self.pins.stop()


class ComputeRuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.fixture = RuntimeFixture(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.fixture.close)

    def load(self):
        return runtime.load_compute_runtime_manifest(self.root)

    def invalid(self):
        with self.assertRaises(runtime.RuntimeManifestError) as caught:
            self.load()
        self.assertNotIsInstance(caught.exception, runtime.RuntimeBindingMissing)

    def test_all_bindings_and_cpu_alias_validate(self):
        value = self.load()
        for role, environment in value.roles.items():
            self.assertEqual(value.select_python(role), self.root / self.fixture.data["environments"][environment]["python"])
        self.assertEqual(value.select_python("app"), value.select_python("deposition_cpu"))
        self.assertEqual(len(value.environments), 4)

    def test_cuda13_has_separate_valid_binding(self):
        record = self.fixture.data["environments"]["deposition_cuda"]
        record["backend"] = "cuda13"
        record["packages"].pop("cupy-cuda12x")
        record["packages"].update({"cupy-cuda13x": "14.2.0", "cuda-toolkit": "13.0.0"})
        self.fixture.write_metadata("deposition_cuda")
        self.fixture.write()
        self.assertEqual(self.load().environments["deposition_cuda"].backend, "cuda13")

    def test_manifest_and_binding_results_are_immutable(self):
        value = self.load()
        with self.assertRaises(TypeError):
            value.roles["appearance"] = "app"
        with self.assertRaises(TypeError):
            value.environments["app"].packages["numpy"] = "0"

    def test_selection_never_executes_or_imports_target_packages(self):
        import builtins
        import subprocess
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.split(".")[0] in {"torch", "torch_directml", "cupy", "mitsuba", "drjit", "numpy"}:
                self.fail("Target package import: " + name)
            return original(name, *args, **kwargs)
        with patch.object(builtins, "__import__", guarded), patch.object(subprocess, "Popen", side_effect=AssertionError("execute")), \
                patch.object(os, "system", side_effect=AssertionError("execute")):
            self.assertTrue(runtime.select_runtime_python(self.root, "appearance").is_file())

    def test_missing_manifest_is_distinct(self):
        (self.root / runtime.MANIFEST_PATH).unlink()
        with self.assertRaises(runtime.RuntimeBindingMissing):
            self.load()

    def test_operation_or_recovery_lock_blocks_old_and_missing_binding(self):
        lock = self.root / ".runtime/compute-runtime-install.lock"
        lock.write_text('{"pid":1234}')
        for manifest_present in (True, False):
            with self.subTest(manifest_present=manifest_present):
                if not manifest_present:
                    (self.root / runtime.MANIFEST_PATH).unlink()
                with self.assertRaises(runtime.RuntimeManifestError) as caught:
                    self.load()
                self.assertNotIsInstance(caught.exception, runtime.RuntimeBindingMissing)
                self.assertIn("fresh runtime-roles-plan", str(caught.exception))

    def test_lock_acquired_during_publication_gap_never_looks_like_missing_manifest(self):
        manifest = self.root / runtime.MANIFEST_PATH
        original = os.path.lexists
        injected = False
        def concurrent(path):
            nonlocal injected
            if Path(path) == manifest and not injected:
                injected = True
                (self.root / ".runtime/compute-runtime-install.lock").write_text("writer")
                manifest.unlink()
                return False
            return original(path)
        with patch.object(os.path, "lexists", concurrent), self.assertRaises(runtime.RuntimeManifestError) as caught:
            self.load()
        self.assertNotIsInstance(caught.exception, runtime.RuntimeBindingMissing)
        self.assertIn("fresh runtime-roles-plan", str(caught.exception))

    def test_lock_acquired_during_validation_blocks_return_of_old_bindings(self):
        original = runtime._packages
        def concurrent(root, environment, data):
            result = original(root, environment, data)
            if environment == "appearance":
                (self.root / ".runtime/compute-runtime-install.lock").write_text("writer")
            return result
        with patch.object(runtime, "_packages", concurrent), self.assertRaises(runtime.RuntimeManifestError):
            self.load()

    def test_absent_optional_bindings_require_explicit_deposition_fallback(self):
        for name in ("appearance", "deposition_cuda", "deposition_directml"):
            self.fixture.data["environments"].pop(name)
            self.fixture.data["roles"].pop(name)
        self.fixture.write()
        value = self.load()
        for role in ("appearance", "deposition_cuda", "deposition_directml"):
            with self.assertRaises(runtime.RuntimeBindingMissing):
                value.select_python(role)
        for role in ("deposition_cuda", "deposition_directml"):
            self.assertEqual(value.select_python(role, allow_cpu_fallback=True), value.select_python("app"))
        with self.assertRaises(runtime.RuntimeBindingMissing):
            value.select_python("appearance", allow_cpu_fallback=True)

    def test_no_external_python_or_unknown_role_fallback(self):
        with self.assertRaises(runtime.RuntimeManifestError):
            self.load().select_python("external", allow_cpu_fallback=True)

    def test_invalid_api_input_fails_with_manifest_errors(self):
        invalid_roots: list[Any] = ["a\0b", None, 123]
        for root in invalid_roots:
            with self.subTest(root=root), self.assertRaises(runtime.RuntimeManifestError):
                runtime.load_compute_runtime_manifest(root)
        value = self.load()
        invalid_roles: list[Any] = [None, [], 123]
        for role in invalid_roles:
            with self.subTest(role=role), self.assertRaises(runtime.RuntimeManifestError):
                value.select_python(role)
        with self.assertRaises(runtime.RuntimeManifestError):
            value.select_python("app", allow_cpu_fallback="false")

    def test_invalid_optional_binding_stops_app_selection(self):
        self.fixture.data["environments"]["appearance"]["python_sha256"] = "f" * 64
        self.fixture.write()
        with self.assertRaises(runtime.RuntimeManifestError):
            runtime.select_runtime_python(self.root, "app", allow_cpu_fallback=True)

    def test_unsupported_top_level_fields_and_platforms(self):
        original = deepcopy(self.fixture.data)
        mutations = [lambda d: d.update(extra=True), lambda d: d.update(schema_version=True),
                     lambda d: d.update(schema_version=2), lambda d: d.update(platform="linux"),
                     lambda d: d.update(architecture="arm64"), lambda d: d.pop("base"),
                     lambda d: d.update(environments=[]), lambda d: d.update(roles=[])]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.fixture.data = deepcopy(original)
                mutate(self.fixture.data)
                self.fixture.write()
                self.invalid()

    def test_wrong_role_bindings_reject_alias_and_unknowns(self):
        original = deepcopy(self.fixture.data)
        for role, value in (("appearance", "app"), ("deposition_cpu", "deposition_cuda"), ("unknown", "app")):
            with self.subTest(role=role):
                self.fixture.data = deepcopy(original)
                self.fixture.data["roles"][role] = value
                self.fixture.write()
                self.invalid()

    def test_optional_role_pair_mismatch_is_invalid(self):
        self.fixture.data["roles"].pop("appearance")
        self.fixture.write()
        self.invalid()

    def test_app_and_cpu_alias_are_required(self):
        self.fixture.data["roles"].pop("deposition_cpu")
        self.fixture.write()
        self.invalid()

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / runtime.MANIFEST_PATH
        path.write_text('{"schema_version":1,"schema_version":1}')
        self.invalid()

    def test_malformed_unicode_json_and_nonfinite_values_are_rejected(self):
        path = self.root / runtime.MANIFEST_PATH
        for raw in (b"{", b"\xff", b'{"a":NaN}', b'{"a":Infinity}'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.invalid()

    def test_base_marker_must_match_pins(self):
        original = deepcopy(self.fixture.marker)
        for field, value in (("schema_version", True), ("version", "3.13.0"),
                             ("package_sha256", "f" * 64), ("files", {})):
            with self.subTest(field=field):
                self.fixture.marker = deepcopy(original)
                self.fixture.marker[field] = value
                self.fixture.write_marker()
                self.invalid()

    def test_base_inventory_content_change_is_untrusted_even_with_updated_marker(self):
        path = self.fixture.base / "python.exe"
        path.write_bytes(b"untrusted replacement")
        self.fixture.files["python.exe"] = {"sha256": sha(path.read_bytes()), "size": path.stat().st_size}
        self.fixture.write_marker()
        self.invalid()

    def test_base_file_tamper_and_missing_files_are_rejected(self):
        path = self.fixture.base / "python312.dll"
        path.write_bytes(b"tampered")
        self.invalid()
        path.unlink()
        self.invalid()

    def test_unowned_base_file_rejected_but_cache_allowed(self):
        cache = self.fixture.base / "Lib/__pycache__/module.cpython-312.pyc"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"inert cache")
        self.load()
        (self.fixture.base / "Lib/extra.py").write_text("raise RuntimeError('not executed')")
        self.invalid()

    def test_unsafe_base_inventory_paths(self):
        original = deepcopy(self.fixture.files)
        paths = ("../outside", "/absolute", "C:/python.exe", "C:relative", "file:stream", "a\\b",
                 "a//b", "./a", "a/../b", "a/", "CON.txt", "COM1", "LPT².log", "nul",
                 "CON .txt", "a.", "a ", "a\x00b", "a\nb", "a?b", "a|b", "e\u0301.txt")
        for name in paths:
            with self.subTest(name=name):
                self.fixture.marker["files"] = deepcopy(original)
                self.fixture.marker["files"][name] = self.fixture.marker["files"].pop("python312.dll")
                self.fixture.write_marker()
                self.invalid()

    def test_duplicate_windows_inventory_names_rejected(self):
        self.fixture.marker["files"]["PYTHON.EXE"] = self.fixture.marker["files"].pop("python312.dll")
        self.fixture.write_marker()
        self.invalid()

    def test_env_manifest_paths_are_fixed_not_path_hints(self):
        record = self.fixture.data["environments"]["app"]
        for field in ("root", "python"):
            original = record[field]
            for value in ("/usr/bin/python", "../python", "C:/python.exe", ".venv/Scripts/python.exe:stream"):
                with self.subTest(field=field, value=value):
                    record[field] = value
                    self.fixture.write()
                    self.invalid()
            record[field] = original

    def test_env_wrong_backend_and_unknown_fields_rejected(self):
        record = self.fixture.data["environments"]["app"]
        invalid_backends: list[Any] = ["cuda12", [], None]
        for value in invalid_backends:
            with self.subTest(value=value):
                record["backend"] = value
                self.fixture.write()
                self.invalid()
        record["backend"] = "cpu"
        record["ready"] = True
        self.fixture.write()
        self.invalid()

    def test_environment_interpreter_and_config_hashes_are_required(self):
        record = self.fixture.data["environments"]["app"]
        for field in ("python_sha256", "pyvenv_cfg_sha256", "packages_sha256"):
            original = record[field]
            for value in ("f" * 64, "A" * 64, "x", None):
                with self.subTest(field=field, value=value):
                    record[field] = value
                    self.fixture.write()
                    self.invalid()
            record[field] = original

    def test_config_external_base_and_system_site_rejected_with_matching_hash(self):
        record = self.fixture.data["environments"]["app"]
        path = self.root / record["root"] / "pyvenv.cfg"
        original = path.read_text()
        transforms = [lambda s: s.replace("false", "true"), lambda s: s.replace("3.12.10", "3.13.0"),
                      lambda s: s.replace(str(self.fixture.base), str(self.root / "outside")),
                      lambda s: s + "home = /external\n", lambda s: s.replace("executable =", "other ="),
                      lambda s: s.replace("home =", "#home =")]
        for transform in transforms:
            with self.subTest(transform=transform):
                path.write_text(transform(original))
                record["pyvenv_cfg_sha256"] = sha(path.read_bytes())
                self.fixture.write()
                self.invalid()

    def test_env_file_mutation_is_not_hidden_by_recorded_package_inventory(self):
        python = self.root / self.fixture.data["environments"]["app"]["python"]
        python.write_bytes(b"replacement")
        self.invalid()

    def test_rehashed_arbitrary_interpreter_is_not_blessed_by_manifest(self):
        record = self.fixture.data["environments"]["app"]
        python = self.root / record["python"]
        python.write_bytes(b"arbitrary executable")
        record["python_sha256"] = sha(python.read_bytes())
        self.fixture.write()
        self.invalid()

    def test_stale_manifest_after_app_repair_then_dml_failure_is_rejected(self):
        # Simulate an installer updating app, then stopping before the atomic
        # manifest write because DirectML failed. The previous JSON stays, but
        # does NOT remain a valid binding for partially updated environments.
        self.load()
        manifest = self.root / runtime.MANIFEST_PATH
        before = manifest.read_bytes()
        metadata = next((self.root / ".venv/Lib/site-packages").glob("numpy-*.dist-info/METADATA"))
        metadata.write_text("Name: numpy\nVersion: 2.2.7\n")
        self.assertEqual(manifest.read_bytes(), before)
        with self.assertRaises(runtime.RuntimeManifestError):
            runtime.select_runtime_python(self.root, "deposition_directml", allow_cpu_fallback=True)
        self.assertEqual(manifest.read_bytes(), before)

    def test_missing_required_packages(self):
        record = self.fixture.data["environments"]["app"]
        for name in tuple(APP):
            with self.subTest(name=name):
                version = record["packages"].pop(name)
                self.fixture.write_metadata("app")
                self.fixture.write()
                self.invalid()
                record["packages"][name] = version

    def test_minimum_ceiling_and_exact_package_constraints(self):
        record = self.fixture.data["environments"]["app"]
        for name, version in (("numpy", "1.23.9"), ("scipy", "2.0"), ("pydantic", "2.12rc1"),
                              ("pyside6-essentials", "7.0.0"), ("pydantic-ai-slim", "2.51.1")):
            with self.subTest(name=name, version=version):
                original = record["packages"][name]
                record["packages"][name] = version
                self.fixture.write_metadata("app")
                self.fixture.write()
                self.invalid()
                record["packages"][name] = original

    def test_legitimate_transitive_packages_allowed_and_names_normalized(self):
        record = self.fixture.data["environments"]["app"]
        record["packages"]["ordinary-transitive"] = "1.0.post1"
        self.fixture.write_metadata("app")
        metadata = self.root / record["root"] / "Lib/site-packages/ordinary_transitive-1.0.post1.dist-info/METADATA"
        metadata.write_text("Name: Ordinary.._Transitive\nVersion: 1.0.post1\n")
        self.fixture.write()
        self.assertIn("ordinary-transitive", self.load().environments["app"].packages)

    def test_existing_app_unrelated_gpu_packages_are_preserved_not_imported(self):
        record = self.fixture.data["environments"]["app"]
        record["packages"].update({"torch": "2.4.1", "torch-directml": "0.2.5.dev240914",
                                   "cupy-cuda12x": "14.2.0"})
        self.fixture.write_metadata("app")
        self.fixture.write()
        value = self.load()
        self.assertEqual(value.select_python("deposition_cpu"), value.select_python("app"))
        self.assertIn("torch-directml", value.environments["app"].packages)

    def test_noncanonical_manifest_package_names_rejected(self):
        record = self.fixture.data["environments"]["app"]
        record["packages"]["PySide6_Essentials"] = record["packages"].pop("pyside6-essentials")
        record["packages_sha256"] = runtime.package_inventory_digest(record["packages"])
        self.fixture.write()
        self.invalid()

    def test_recorded_inventory_must_match_actual_metadata(self):
        record = self.fixture.data["environments"]["app"]
        record["packages"]["numpy"] = "2.2.7"
        record["packages_sha256"] = runtime.package_inventory_digest(record["packages"])
        self.fixture.write()
        self.invalid()

    def test_unrecorded_and_duplicate_distributions_rejected(self):
        site = self.root / ".venv/Lib/site-packages"
        metadata = site / "unrecorded-1.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text("Name: unrecorded\nVersion: 1\n")
        self.invalid()
        metadata.write_text("Name: numpy\nVersion: 2.2.6\n")
        self.invalid()

    def test_legacy_linked_package_metadata_rejected(self):
        (self.root / ".venv/Lib/site-packages/external.egg-link").write_text("/external")
        self.invalid()

    def test_metadata_repeated_or_missing_identity_rejected(self):
        metadata = next((self.root / ".venv/Lib/site-packages").glob("numpy-*.dist-info/METADATA"))
        for value in ("Name: numpy\n", "Name: numpy\nVersion: 2.2.6\nName: other\n",
                      "Name: numpy\nVersion: 2.2.6\nVersion: 2.2.7\n"):
            with self.subTest(value=value):
                metadata.write_text(value)
                self.invalid()

    def test_gpu_distribution_conflicts_rejected(self):
        cases = (("deposition_directml", "cupy-cuda12x", "14.2.0"),
                 ("deposition_directml", "cupy-cuda102", "12.0.0"),
                 ("deposition_cuda", "cupy-cuda13x", "14.2.0"),
                 ("deposition_cuda", "torch-directml", "0.2.5.dev240914"))
        for environment, name, version in cases:
            with self.subTest(environment=environment):
                record = self.fixture.data["environments"][environment]
                record["packages"][name] = version
                self.fixture.write_metadata(environment)
                self.fixture.write()
                self.invalid()
                record["packages"].pop(name)
                self.fixture.write_metadata(environment)

    def test_gpu_and_appearance_exact_pins_enforced(self):
        for environment, name in (("appearance", "mitsuba"), ("appearance", "drjit"),
                                  ("deposition_directml", "torch-directml"),
                                  ("deposition_cuda", "cuda-toolkit"), ("deposition_cuda", "cupy-cuda12x")):
            with self.subTest(environment=environment, name=name):
                record = self.fixture.data["environments"][environment]
                original = record["packages"][name]
                record["packages"][name] = "99.0"
                self.fixture.write_metadata(environment)
                self.fixture.write()
                self.invalid()
                record["packages"][name] = original
                self.fixture.write_metadata(environment)

    def test_symlinked_manifest_and_dangling_manifest_are_invalid(self):
        path = self.root / runtime.MANIFEST_PATH
        other = self.root / "manifest.json"
        path.rename(other)
        path.symlink_to(other)
        self.invalid()
        other.unlink()
        self.invalid()

    def test_symlinked_environment_and_metadata_are_invalid(self):
        source = self.root / ".venv/Lib/site-packages"
        target = self.root / "other-site"
        source.rename(target)
        source.symlink_to(target, target_is_directory=True)
        self.invalid()

    def test_symlinked_root_and_parent_are_invalid(self):
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(runtime.RuntimeManifestError):
            runtime.load_compute_runtime_manifest(link)

    def test_windows_reparse_flag_is_rejected_without_windows(self):
        real_lstat = Path.lstat
        from types import SimpleNamespace
        target = self.root / ".venv/Scripts/python.exe"
        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", lstat):
            self.invalid()

    def test_broken_runtime_container_is_invalid_not_missing(self):
        import shutil
        shutil.rmtree(self.root / ".runtime")
        (self.root / ".runtime").write_text("not a directory")
        self.invalid()

    def test_manifest_size_bound(self):
        (self.root / runtime.MANIFEST_PATH).write_bytes(b" " * (4 * 1024 * 1024 + 1))
        self.invalid()

    def test_digest_is_stable_order_sensitive_to_content_not_map_order(self):
        self.assertEqual(runtime.package_inventory_digest({"b": "2", "a": "1"}),
                         sha(b"a\x001\nb\x002\n"))
        self.assertNotEqual(runtime.package_inventory_digest({"a": "1"}),
                            runtime.package_inventory_digest({"a": "2"}))


if __name__ == "__main__":
    unittest.main()
