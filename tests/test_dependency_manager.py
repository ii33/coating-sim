"""Offline contracts for the independent tool; never installs target packages."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.dependency_manager import manager, runtime
from tools.dependency_manager.mirrors import ProxyConfig

ROOT = Path(__file__).resolve().parents[1]


class ManagerTests(unittest.TestCase):
    def test_tool_build_catalog_is_a_separate_scope(self):
        rows = manager.build_catalog(ROOT)
        self.assertEqual({row["name"] for row in rows}, {"pyinstaller"})
        self.assertTrue(all(row["group"] == "build" and row["scope"] == "separate-build-venv" for row in rows))
        self.assertNotIn("build", manager.PROFILES)
        _, runtime_rows = manager.catalog(ROOT)
        self.assertFalse({"pyinstaller", "cx-freeze", "freeze-core", "patchelf"}.intersection(row["name"] for row in runtime_rows))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows TEMP may use an 8.3 alias or an inherited linked ancestor.
        # Identity fixtures must match the canonical paths emitted by the planner;
        # links deliberately introduced below this root remain separate test cases.
        self.root = Path(self.temp.name).resolve()
        (self.root / "requirements.txt").write_text(
            "numpy>=1.24\nscipy>=1.16,<2\nPillow>=10\nPySide6-Essentials>=6.8,<7\npydantic-ai-slim[openai,mcp]==2.51.0\nkeyring>=25,<26\n"
        )
        (self.root / "requirements-test.txt").write_text(
            "pytest>=9.1,<10\nmpmath>=1.3,<2\nmitsuba==3.9.0\ndrjit==1.4.0\n"
        )
        (self.root / "requirements-analysis.txt").write_text(
            "-r requirements.txt\n-r requirements-test.txt\n"
            "-r requirements-analysis-lock.txt\nruff==0.17.0\nmypy==2.4.0\n"
        )
        (self.root / "requirements-sandbox.txt").write_text(
            "pygal==3.0.5\nzipp==4.1.0\n"
        )
        self.venv = self.root / ".venv"
        self.proxy = ProxyConfig("none")

    def fake_venv(self, inventory=None):
        interpreter = self.venv / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("fixture")
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        data = {
            "prefix": str(self.venv),
            "base_prefix": str(self.root / "base"),
            "executable": str(interpreter),
            "python": "3.12.14",
            "platform": sys.platform,
            "machine": "x86_64",
            "implementation": "CPython",
            "has_venv": True,
            "has_ensurepip": True,
            "inventory": inventory or {},
            "checks": {},
            "dependency_issues": {},
            "pip": "25.0",
        }
        return data

    def test_real_project_catalog_covers_exact_runtime_requirements(self):
        unused, rows = manager.catalog(ROOT, ("desktop",))
        self.assertEqual(manager.catalog(ROOT, ("desktop",))[0], ["desktop"])
        self.assertEqual(len(rows), 15)
        self.assertEqual(
            {r["requirement"] for r in rows},
            {
                r["requirement"]
                for r in manager._read_requirements(ROOT / "requirements.txt")
            },
        )

    def test_development_profile_adds_only_direct_test_and_static_tools(self):
        _, runtime = manager.catalog(self.root, ("desktop",), profile="runtime")
        _, development = manager.catalog(
            self.root, ("desktop",), profile="development"
        )
        runtime_names = {row["name"] for row in runtime}
        development_by_name = {row["name"]: row for row in development}
        self.assertEqual(
            set(development_by_name) - runtime_names,
            {"pytest", "mpmath", "ruff", "mypy"},
        )
        self.assertEqual(development_by_name["pytest"]["group"], "development-test")
        self.assertEqual(development_by_name["ruff"]["group"], "static-tooling")
        self.assertEqual(development_by_name["ruff"]["source"], "requirements-analysis.txt")
        self.assertNotIn("mitsuba", development_by_name)
        self.assertNotIn("drjit", development_by_name)
        self.assertNotIn("cupy-cuda12x", development_by_name)
        self.assertNotIn("torch-directml", development_by_name)

    def test_development_profile_does_not_select_cpu_or_render_features(self):
        _, rows = manager.catalog(
            self.root, ("sandbox",), backend="cpu", profile="development"
        )
        names = {row["name"] for row in rows}
        self.assertEqual(names, {"pygal", "zipp", "pytest", "mpmath", "ruff", "mypy"})
        self.assertNotIn("numpy", names)
        self.assertNotIn("mitsuba", names)

    def test_analysis_tool_reader_fails_closed_on_unexpected_includes_and_options(self):
        path = self.root / "requirements-analysis.txt"
        for content in (
            "-r requirements.txt\n-r requirements-test.txt\n"
            "-r requirements-analysis-lock.txt\n-r extra.txt\n"
            "ruff==0.17.0\nmypy==2.4.0\n",
            "-r requirements.txt\n-r requirements-test.txt\n"
            "-r requirements-analysis-lock.txt\n--trusted-host invalid\n"
            "ruff==0.17.0\nmypy==2.4.0\n",
            "-r requirements.txt\n-r requirements-test.txt\n"
            "-r requirements-analysis-lock.txt\nruff @ https://bad.example/ruff.whl\n"
            "mypy==2.4.0\n",
        ):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    manager.catalog(self.root, profile="development")

    def test_group_partition_renderer_pair_and_test_not_in_desktop(self):
        unused, rows = manager.catalog(self.root, ("appearance",))
        self.assertEqual(
            {r["name"] for r in rows}, {"numpy", "scipy", "pillow", "mitsuba", "drjit"}
        )
        self.assertEqual(
            {r["name"] for r in manager.catalog(self.root, ("test",))[1]},
            {"numpy", "scipy", "pillow", "pytest", "mpmath"},
        )

    def test_missing_environment_is_inspectable_without_qt_or_pip(self):
        result = manager.inspect_environment(self.root)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["target"]["exists"])
        self.assertTrue(all(r["status"] == "missing" for r in result["packages"]))

    def test_system_target_is_rejected(self):
        self.venv.mkdir()
        result = manager.inspect_environment(self.root)
        self.assertIn("non-venv", result["errors"][0])

    def test_target_identity_must_match(self):
        data = self.fake_venv()
        data["prefix"] = data["base_prefix"]
        with patch.object(manager, "probe_python", return_value=data):
            self.assertFalse(manager.inspect_environment(self.root)["target"]["valid"])

    def test_system_site_packages_rejected_before_probe(self):
        self.fake_venv()
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = true")
        with patch.object(manager, "probe_python") as probe:
            self.assertTrue(manager.inspect_environment(self.root)["errors"])
            probe.assert_not_called()

    def test_symlink_target_rejected(self):
        other = self.root / "other"
        other.mkdir()
        self.venv.symlink_to(other, target_is_directory=True)
        self.assertTrue(manager.inspect_environment(self.root)["errors"])

    def test_pin_conflict_even_when_package_is_present(self):
        data = self.fake_venv({"pygal": "3.1.3", "zipp": "4.1.1"})
        with patch.object(manager, "probe_python", return_value=data):
            rows = manager.inspect_environment(self.root, groups=("sandbox",))[
                "packages"
            ]
        self.assertEqual([row["status"] for row in rows], ["conflict", "conflict"])

    def test_new_venv_must_match_selected_bootstrap_identity(self):
        data = self.fake_venv()
        plan = {
            "root": str(self.root),
            "target": {
                "venv": str(self.venv),
                "exists": False,
                "valid": False,
                "parent_identity": {
                    "path": str(self.root),
                    "device": self.root.stat().st_dev,
                    "inode": self.root.stat().st_ino,
                },
            },
            "groups": ["cpu"],
            "backend": "cpu",
            "bootstrap_identity": {
                "base_prefix": str(self.root / "expected"),
                "python": "3.13.16",
                "platform": "win32",
                "machine": "AMD64",
            },
        }
        with (
            patch.object(manager, "probe_python", return_value=data),
            self.assertRaisesRegex(ValueError, "bootstrap"),
        ):
            manager._verify_mutation_target(plan)

    def test_old_target_pip_is_blocked_before_dry_run(self):
        data = self.fake_venv()
        data["pip"] = "21.2.4"
        with patch.object(manager, "probe_python", return_value=data):
            plan = manager.plan_environment(self.root, proxy=self.proxy)
        self.assertEqual(plan["status"], "blocked")
        self.assertTrue(any("22.2" in error for error in plan["errors"]))

    def test_numeric_fallback_padding_and_bounds(self):
        self.assertTrue(manager._satisfies("3.0", "==3.0.0"))
        self.assertTrue(manager._satisfies("1.25", ">=1.24,<2"))
        self.assertFalse(manager._satisfies("3.1.3", "==3.0.5"))
        self.assertFalse(manager._satisfies("1.2rc1", ">=1.2"))

    def test_declared_extra_dependency_issue_marks_root_conflict(self):
        data = self.fake_venv({"pydantic-ai-slim": "2.51.0"})
        data["dependency_issues"] = {
            "pydantic-ai-slim": [
                {"requirement": "openai>=2", "installed_version": None}
            ]
        }
        with patch.object(manager, "probe_python", return_value=data):
            row = next(
                r
                for r in manager.inspect_environment(self.root, groups=("assistant",))[
                    "packages"
                ]
                if r["name"] == "pydantic-ai-slim"
            )
        self.assertEqual(row["status"], "conflict")

    def test_unsafe_requirement_options_or_urls_rejected(self):
        for text in (
            "--trusted-host foo",
            "pkg @ https://bad.example/a.whl",
            "numpy>=1; python_version>'3'",
        ):
            with self.subTest(text=text):
                (self.root / "requirements.txt").write_text(text)
                with self.assertRaises(ValueError):
                    manager.catalog(self.root)

    def test_unknown_group_backend_mirror_and_action_rejected(self):
        for kwargs in (
            {"groups": ("unknown",)},
            {"backend": "custom"},
            {"profile": "all"},
            {"mirror": "https://evil"},
            {"action": "uninstall"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                manager.plan_environment(self.root, **kwargs)

    def test_missing_environment_plan_requires_explicit_normal_python(self):
        plan = manager.plan_environment(self.root, proxy=self.proxy)
        self.assertEqual(plan["status"], "blocked")
        with patch.object(
            manager,
            "probe_python",
            return_value={"has_venv": False, "has_ensurepip": False},
        ):
            plan = manager.plan_environment(
                self.root, python="frozen.exe", proxy=self.proxy
            )
        self.assertEqual(plan["status"], "blocked")

    def test_existing_normal_bootstrap_plan_has_preflight_before_install(self):
        plan = manager.plan_environment(
            self.root, groups=("cpu",), python=sys.executable, proxy=self.proxy
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(
            [a["kind"] for a in plan["actions"]],
            ["create_venv", "resolve_wheels", "pip_install"],
        )
        self.assertIn("--dry-run", plan["actions"][1]["argv"])
        self.assertNotIn("--trusted-host", str(plan))
        self.assertNotIn("--proxy", str(plan["actions"]))

    def test_install_versus_repair_conflicts(self):
        data = self.fake_venv({"pygal": "3.1.3", "zipp": "4.1.0"})
        with patch.object(manager, "probe_python", return_value=data):
            install = manager.plan_environment(
                self.root, groups=("sandbox",), action="install", proxy=self.proxy
            )
            repair = manager.plan_environment(
                self.root, groups=("sandbox",), action="repair", proxy=self.proxy
            )
        self.assertFalse(install["actions"])
        self.assertIn("pygal==3.0.5", repair["actions"][-1]["argv"])
        self.assertNotIn("zipp==4.1.0", repair["actions"][-1]["argv"])

    def test_missing_pip_only_repair_uses_ensurepip(self):
        data = self.fake_venv()
        data["pip"] = None
        with patch.object(manager, "probe_python", return_value=data):
            repair = manager.plan_environment(
                self.root, action="repair", proxy=self.proxy
            )
            install = manager.plan_environment(
                self.root, action="install", proxy=self.proxy
            )
        self.assertEqual(repair["actions"][0]["kind"], "ensurepip")
        self.assertEqual(install["status"], "blocked")

    def test_optional_backend_compatibility_blocks_manual_vendor_profiles(self):
        for backend in ("directml", "rocm", "xpu"):
            with self.subTest(backend=backend):
                self.assertTrue(
                    manager.inspect_environment(self.root, backend=backend)["errors"]
                )

    def test_execute_requires_confirmation_and_rejects_tampering(self):
        plan = manager.plan_environment(
            self.root, groups=("cpu",), python=sys.executable, proxy=self.proxy
        )
        with patch.object(manager, "run_command") as run:
            self.assertEqual(
                manager.execute_plan(plan, proxy=self.proxy)["status"], "blocked"
            )
            altered = copy.deepcopy(plan)
            altered["actions"][0]["argv"] = ["touch", "/bad"]
            self.assertEqual(
                manager.execute_plan(altered, True, proxy=self.proxy)["status"],
                "blocked",
            )
            run.assert_not_called()

    def test_execute_verifies_post_install_not_exit_alone(self):
        data = self.fake_venv()
        with (
            patch.object(manager, "probe_python", return_value=data),
            patch.object(manager, "run_command", return_value=0),
        ):
            plan = manager.plan_environment(
                self.root, groups=("cpu",), proxy=self.proxy
            )
            result = manager.execute_plan(plan, True, proxy=self.proxy)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(list(self.root.glob("*.lock")))

    def test_execute_failed_preflight_never_installs(self):
        data = self.fake_venv()
        with (
            patch.object(manager, "probe_python", return_value=data),
            patch.object(manager, "run_command", return_value=1) as run,
        ):
            plan = manager.plan_environment(
                self.root, groups=("cpu",), proxy=self.proxy
            )
            result = manager.execute_plan(plan, True, proxy=self.proxy)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(run.call_count, 1)

    def test_development_profile_is_preserved_through_plan_revalidation(self):
        data = self.fake_venv()
        with (
            patch.object(manager, "probe_python", return_value=data),
            patch.object(manager, "run_command", return_value=1) as run,
        ):
            plan = manager.plan_environment(
                self.root,
                groups=("cpu",),
                profile="development",
                proxy=self.proxy,
            )
            result = manager.execute_plan(plan, True, proxy=self.proxy)
        self.assertEqual(plan["profile"], "development")
        self.assertEqual(result["profile"], "development")
        self.assertEqual(run.call_count, 1)

    def test_concurrent_lock_preserved_and_no_mutation(self):
        data = self.fake_venv()
        lock = self.root / ".venv.dependency-manager.lock"
        lock.write_text("other")
        with (
            patch.object(manager, "probe_python", return_value=data),
            patch.object(manager, "run_command") as run,
        ):
            plan = manager.plan_environment(self.root, proxy=self.proxy)
            self.assertEqual(
                manager.execute_plan(plan, True, proxy=self.proxy)["status"], "failed"
            )
            run.assert_not_called()
        self.assertEqual(lock.read_text(), "other")

    def test_subprocess_argv_only_and_stream_redaction(self):
        with self.assertRaises(ValueError):
            manager.run_command("echo bad", lambda event: None, threading.Event())
        events: list[dict] = []
        code = manager.run_command(
            [
                sys.executable,
                "-c",
                "print('https://user:SECRET@example.test/a?token=TOKEN')",
            ],
            events.append,
            threading.Event(),
            self.proxy,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("SECRET", str(events))
        self.assertNotIn("TOKEN", str(events))

    def test_cancellation_stops_child_process(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            code = manager.run_command(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                lambda event: None,
                cancelled,
                self.proxy,
                timeout=1,
            )
            self.assertEqual(code, 130)
        finally:
            timer.cancel()

    def test_windows_taskkill_timeout_still_kills_main_process_and_warns(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = io.StringIO("")
                self.pid = 123
                self.killed = False

            def poll(self):
                return -9 if self.killed else None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return -9

        process = FakeProcess()
        cancel = threading.Event()
        timer = threading.Timer(0.02, cancel.set)
        events: list[dict] = []
        timer.start()
        try:
            fake_os = SimpleNamespace(
                name="nt", environ={"SystemRoot": "C:/Windows"}, devnull=os.devnull
            )
            with (
                patch.object(manager, "os", fake_os),
                patch.object(manager.subprocess, "Popen", return_value=process),
                patch.object(
                    manager.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("taskkill", 10),
                ),
            ):
                self.assertEqual(
                    manager.run_command(
                        ["python", "fixture"], events.append, cancel, self.proxy
                    ),
                    130,
                )
            self.assertTrue(process.killed)
            self.assertIn("descendants may still", str(events))
        finally:
            timer.cancel()

    def test_child_environment_ignores_pip_config_and_secrets_in_args(self):
        with patch.dict(
            os.environ,
            {
                "PIP_TRUSTED_HOST": "evil",
                "PIP_EXTRA_INDEX_URL": "https://evil",
                "PYTHONPATH": "bad",
            },
        ):
            env = manager.clean_environment(
                ProxyConfig("http", "http://user:secret@localhost:10808")
            )
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertNotIn("PIP_TRUSTED_HOST", env)
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertIn("secret", env["HTTPS_PROXY"])

    def test_stdlib_cli_inspection_works_with_site_packages_disabled(self):
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                str(ROOT / "dependency_manager.py"),
                "inspect",
                "--project",
                str(self.root),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema_version"], 1)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.registry_patch = patch.object(
            runtime, "_registered_python_paths", return_value=[]
        )
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.directory = self.home / "managed"
        self.proxy = ProxyConfig("none")

    def plan(self, **kwargs):
        return runtime.plan_runtime(
            self.directory,
            proxy=self.proxy,
            platform_name="win32",
            machine="AMD64",
            **kwargs,
        )

    def test_fixed_manifest_official_hashes_and_architecture(self):
        manifest = runtime.runtime_manifest()
        self.assertEqual(manifest["version"], "3.13.16")
        for arch in ("AMD64", "x86", "ARM64"):
            plan = runtime.plan_runtime(
                self.directory, proxy=self.proxy, platform_name="win32", machine=arch
            )
            self.assertEqual(plan["status"], "ready", plan["errors"])
            self.assertEqual(len(plan["artifact"]["sha256"]), 64)

    def test_linux_unknown_arch_and_directml_are_blocked_before_download(self):
        for kwargs in (
            {"platform_name": "linux", "machine": "AMD64"},
            {"platform_name": "win32", "machine": "sparc"},
            {"platform_name": "win32", "machine": "AMD64", "backend": "directml"},
        ):
            self.assertEqual(
                runtime.plan_runtime(self.directory, proxy=self.proxy, **kwargs)[
                    "status"
                ],
                "blocked",
            )

    def test_runtime_path_collision_and_outside_home_rejected(self):
        self.directory.mkdir()
        self.assertEqual(self.plan()["status"], "blocked")
        self.assertEqual(
            runtime.plan_runtime(
                "/usr/system-python",
                proxy=self.proxy,
                platform_name="win32",
                machine="AMD64",
            )["status"],
            "blocked",
        )

    def test_runtime_dotdot_and_symlink_ancestor_are_blocked(self):
        escaped = self.home / ".." / "outside" / "python"
        self.assertEqual(
            runtime.plan_runtime(
                escaped, proxy=self.proxy, platform_name="win32", machine="AMD64"
            )["status"],
            "blocked",
        )
        actual = self.home / "actual"
        actual.mkdir()
        link = self.home / "link"
        link.symlink_to(actual, target_is_directory=True)
        self.assertEqual(
            runtime.plan_runtime(
                link / "python",
                proxy=self.proxy,
                platform_name="win32",
                machine="AMD64",
            )["status"],
            "blocked",
        )

    def test_official_redirect_rejects_userinfo_ports_queries_and_external_host(self):
        handler = runtime._OfficialRedirect()
        request = runtime.Request("https://www.python.org/source")
        for url in (
            "https://:pw@www.python.org/file",
            "https://www.python.org:8443/file",
            "https://www.python.org/file?token=private",
            "https://evil.example/file",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)

    def test_installer_flags_no_global_side_effects(self):
        args = runtime.installer_arguments("installer.exe", self.directory)
        for flag in (
            "InstallAllUsers=0",
            "PrependPath=0",
            "AppendPath=0",
            "Include_launcher=0",
            "AssociateFiles=0",
            "Shortcuts=0",
            "Include_pip=1",
        ):
            self.assertIn(flag, args)
        self.assertNotIn("runas", str(args))
        self.assertNotIn("Include_tcltk=1", args)

    def test_download_hash_verified_before_execution(self):
        content = b"fixture official installer"
        artifact = {
            "url": "https://www.python.org/fixture",
            "sha256": runtime.hashlib.sha256(content).hexdigest(),
        }
        response = io.BytesIO(content)
        with patch.object(runtime, "build_opener") as opener:
            opener.return_value.open.return_value = response
            destination = self.home / "download.exe"
            self.assertEqual(
                runtime.download_runtime(
                    artifact,
                    destination,
                    self.proxy,
                    threading.Event(),
                    lambda event: None,
                ),
                len(content),
            )
        self.assertEqual(destination.read_bytes(), content)

    def test_hash_mismatch_cancellation_size_and_network_error_clean_download(self):
        for reason in ("hash", "cancel", "size", "network"):
            with self.subTest(reason=reason):
                artifact = {"url": "https://www.python.org/fixture", "sha256": "0" * 64}
                cancelled = threading.Event()
                if reason == "cancel":
                    cancelled.set()
                destination = self.home / (reason + ".exe")
                with patch.object(runtime, "build_opener") as opener:
                    if reason == "network":
                        opener.return_value.open.side_effect = OSError("offline")
                    else:
                        opener.return_value.open.return_value = io.BytesIO(b"x" * 10)
                    with self.assertRaises((ValueError, InterruptedError, OSError)):
                        runtime.download_runtime(
                            artifact,
                            destination,
                            self.proxy,
                            cancelled,
                            lambda event: None,
                            max_bytes=2 if reason == "size" else 100,
                        )
                self.assertFalse(destination.exists())

    def test_existing_registered_python_prevents_maintenance_of_other_environment(self):
        with patch.object(
            runtime,
            "_registered_python_paths",
            return_value=[str(self.home / "other-python")],
        ):
            self.assertEqual(self.plan()["status"], "blocked")

    def test_runtime_download_does_not_require_global_vc_dll(self):
        import ctypes

        plan = self.plan()
        with (
            patch.object(runtime, "plan_runtime", return_value=plan),
            patch.dict(os.environ, {"SystemRoot": str(self.home)}),
            patch.object(ctypes, "WinDLL", create=True) as loader,
            patch.object(runtime, "_windows_file_version", return_value=(10, 0, 10240, 0)),
            patch.object(runtime, "download_runtime") as download,
            patch.object(runtime, "run_command", return_value=1603) as install,
        ):
            result = runtime.execute_runtime(plan, True, proxy=self.proxy)
            self.assertEqual(result["status"], "failed")  # Synthetic installer exit, no real execution.
            self.assertIn("1603", result["errors"][0])
            download.assert_called_once()
            install.assert_called_once()
            loader.assert_called_once_with(str(self.home / "System32/ucrtbase.dll"))
        self.assertFalse((self.home / "System32/vcruntime140.dll").exists())

    def test_missing_or_old_ucrt_blocks_before_download_or_install(self):
        import ctypes

        plan = self.plan()
        for version in ((9, 0, 20000, 0), (10, 0, 10136, 99)):
            with (
                self.subTest(version=version),
                patch.object(runtime, "plan_runtime", return_value=plan),
                patch.dict(os.environ, {"SystemRoot": str(self.home)}),
                patch.object(ctypes, "WinDLL", create=True),
                patch.object(runtime, "_windows_file_version", return_value=version),
                patch.object(runtime, "download_runtime") as download,
                patch.object(runtime, "run_command") as install,
            ):
                result = runtime.execute_runtime(plan, True, proxy=self.proxy)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("UCRT", result["errors"][0])
                download.assert_not_called()
                install.assert_not_called()
        with (
            patch.object(runtime, "plan_runtime", return_value=plan),
            patch.dict(os.environ, {"SystemRoot": str(self.home)}),
            patch.object(ctypes, "WinDLL", create=True, side_effect=OSError("missing UCRT")),
            patch.object(runtime, "download_runtime") as download,
            patch.object(runtime, "run_command") as install,
        ):
            self.assertEqual(runtime.execute_runtime(plan, True, proxy=self.proxy)["status"], "blocked")
            download.assert_not_called()
            install.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Real UCRT version API requires Windows")
    def test_native_windows_system_ucrt_readiness_is_verified(self):
        runtime._verify_system_ucrt()  # Official Windows source CI, read-only, no installer.

    def test_windows_fixed_version_api_metadata_validation(self):
        import ctypes

        fixed = (ctypes.c_uint32 * 13)(0xFEEF04BD, 0, 0x000A0000, (10137 << 16), *([0] * 9))

        def query(buffer, root, address, length):
            self.assertEqual(root, "\\")
            ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(fixed)
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents.value = ctypes.sizeof(fixed)
            return 1

        api = SimpleNamespace(GetFileVersionInfoSizeW=Mock(return_value=64),
                              GetFileVersionInfoW=Mock(return_value=1),
                              VerQueryValueW=Mock(side_effect=query))
        with (
            patch.dict(os.environ, {"SystemRoot": str(self.home)}),
            patch.object(ctypes, "WinDLL", create=True, return_value=api),
        ):
            self.assertEqual(runtime._windows_file_version(self.home / "ucrtbase.dll"), (10, 0, 10137, 0))
            fixed[0] = 0
            with self.assertRaisesRegex(OSError, "invalid"):
                runtime._windows_file_version(self.home / "ucrtbase.dll")
            api.GetFileVersionInfoSizeW.return_value = 0
            with self.assertRaisesRegex(OSError, "unavailable"):
                runtime._windows_file_version(self.home / "ucrtbase.dll")

    def test_runtime_execute_requires_confirmation_and_never_cross_platform(self):
        plan = self.plan()
        # Exercise the unsupported execution host explicitly, even on a Windows
        # runner. The test must never reach a real downloader or installer.
        with (
            patch.object(runtime.sys, "platform", "linux"),
            patch.object(runtime, "download_runtime") as download,
            patch.object(runtime, "run_command") as install,
        ):
            self.assertEqual(
                runtime.execute_runtime(plan, False, proxy=self.proxy)["status"],
                "blocked",
            )
            self.assertEqual(
                runtime.execute_runtime(plan, True, proxy=self.proxy)["status"],
                "blocked",
            )
            download.assert_not_called()
            install.assert_not_called()


class FixturePathTests(unittest.TestCase):
    def test_inherited_temp_alias_does_not_mask_identity_or_manifest_checks(self):
        # Reproduce a noncanonical inherited TEMP on every host, without mocking
        # resolve() or disabling production symlink/ancestor checks.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            actual = root / "actual-temp"
            actual.mkdir()
            alias = root / "temp-alias"
            alias.symlink_to(actual, target_is_directory=True)
            with patch.object(tempfile, "tempdir", str(alias)):
                for case_type, method in (
                    (ManagerTests, "test_new_venv_must_match_selected_bootstrap_identity"),
                    (RuntimeTests, "test_fixed_manifest_official_hashes_and_architecture"),
                ):
                    case = case_type(method)
                    try:
                        case.setUp()
                        getattr(case, method)()
                    finally:
                        case.doCleanups()


if __name__ == "__main__":
    unittest.main()
