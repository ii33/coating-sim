"""Offline fixed-project checkout/dependency orchestration contracts."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools.dependency_manager import project_deploy as deploy

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # CI temporary paths may contain supported platform aliases. Canonicalize
        # the fixture, rather than relaxing production symlink path protection.
        self.parent = Path(self.temp.name).resolve()
        self.directory = self.parent / "new-project"
        self.commands = []
        self.dirty = False
        self.commit = COMMIT
        self.git = shutil.which("git") or sys.executable

    def fake_git(self, git, args, **kwargs):
        self.commands.append(args)
        if args[:2] == ["ls-remote", "--get-url"]:
            return deploy.REPOSITORY_URL + "\n"
        if args[0] == "ls-remote":
            return self.commit + "\trefs/heads/main\n"
        if args[0] == "init":
            (self.directory / ".git").mkdir()
        if args[0] == "checkout":
            (self.directory / "coat_sim").mkdir()
            for source in ROOT.glob("requirements*.txt"):
                shutil.copy2(source, self.directory / source.name)
            (self.directory / "launcher.py").write_text("raise RuntimeError('must never execute')\n")
            (self.directory / "tools").mkdir()
            (self.directory / "tools/run_tests.py").write_text("raise RuntimeError('must never execute')\n")
        if args[0] == "ls-tree":
            return "100644 blob " + COMMIT + "\trequirements.txt\x00"
        if args[0] == "rev-parse":
            return self.commit + "\n"
        if args[0] == "status":
            return " M launcher.py\n" if self.dirty else ""
        return ""

    def prepare(self, **kwargs):
        with patch.object(deploy, "_run_git", side_effect=self.fake_git), patch.object(deploy.shutil, "which", return_value=self.git):
            return deploy.plan_project_deployment(self.directory, python=sys.executable, **kwargs)

    def successful_dependency(self, dependency, confirmed, emit, cancel, proxy):
        return {"schema_version": 1, "status": "success", "operation": "install",
                "target": dependency["target"], "warnings": [], "errors": []}

    def execute(self, plan, **kwargs):
        with patch.object(deploy, "_run_git", side_effect=self.fake_git), patch.object(deploy.manager, "execute_plan", side_effect=self.successful_dependency) as install:
            result = deploy.execute_project_deployment(plan, confirmed=True, **kwargs)
        return result, install

    def test_plan_fixed_repo_and_project_local_environment(self):
        plan = self.prepare()
        self.assertEqual(plan["status"], "ready", plan)
        self.assertEqual(plan["repository"], deploy.REPOSITORY)
        self.assertEqual(plan["source"]["commit"], COMMIT)
        self.assertEqual(plan["dependency_plan"]["target"]["venv"], str(self.directory / ".venv"))
        self.assertEqual(plan["dependency_plan"]["profile"], "runtime")
        argv = plan["dependency_plan"]["actions"][0]["argv"]
        self.assertEqual(argv[-1], str(self.directory / ".venv"))
        self.assertNotIn("--system-site-packages", argv)
        self.assertTrue(all(deploy.REPOSITORY_URL not in arg or arg == deploy.REPOSITORY_URL
                            for command in self.commands for arg in command))
        self.assertIn(["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules", "origin", COMMIT], self.commands)
        self.assertEqual(deploy.read_project_deployment_plan(self.directory), plan)
        self.assertFalse((self.directory / ".venv").exists())

    def test_development_reuses_existing_catalog(self):
        plan = self.prepare(profile="development")
        self.assertEqual(plan["status"], "ready", plan["errors"])
        names = {row["name"] for row in plan["dependency_plan"]["packages"]}
        self.assertTrue({"pytest", "mpmath", "ruff", "mypy"}.issubset(names))
        self.assertFalse({"cx-freeze", "freeze-core", "mitsuba", "drjit", "cupy-cuda12x"} & names)

    def test_existing_directory_preserved_before_git(self):
        self.directory.mkdir()
        keep = self.directory / "keep.txt"
        keep.write_text("unrelated")
        plan = self.prepare()
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("fresh directory", plan["errors"][0])
        self.assertEqual(keep.read_text(), "unrelated")
        self.assertEqual(self.commands, [])

    def test_cancellation_before_directory_creation(self):
        cancel = threading.Event()
        cancel.set()
        plan = self.prepare(cancel_event=cancel)
        self.assertEqual(plan["status"], "cancelled")
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.commands, [])

    def test_no_confirmation_never_installs(self):
        plan = self.prepare()
        with patch.object(deploy.manager, "execute_plan") as install, patch.object(deploy, "_run_git") as git:
            result = deploy.execute_project_deployment(plan)
        self.assertEqual(result["status"], "blocked")
        install.assert_not_called()
        git.assert_not_called()

    def test_install_delegated_and_existing_entrypoints_only_reported(self):
        plan = self.prepare(profile="development")
        result, install = self.execute(plan)
        self.assertEqual(result["status"], "success", result)
        install.assert_called_once()
        self.assertEqual(install.call_args.args[0], plan["dependency_plan"])
        self.assertTrue(install.call_args.args[1])
        self.assertFalse(result["application_started"])
        self.assertEqual({item["purpose"] for item in result["next_commands"]}, {"launch", "quick-tests"})
        self.assertFalse(any(command[0] in ("launcher.py", "tools/run_tests.py") for command in self.commands))
        self.assertTrue((self.directory / ".git/dependency-manager-deploy/result.json").is_file())

    def test_dirty_checkout_never_installs(self):
        plan = self.prepare()
        self.dirty = True
        result, install = self.execute(plan)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("dirty", result["errors"][0])
        install.assert_not_called()

    def test_moved_commit_never_installs(self):
        plan = self.prepare()
        self.commit = "b" * 40
        result, install = self.execute(plan)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("commit changed", result["errors"][0])
        install.assert_not_called()

    def test_changed_requirements_never_installs_even_if_git_mock_clean(self):
        plan = self.prepare()
        (self.directory / "requirements.txt").write_text("unrelated-package==1\n")
        result, install = self.execute(plan)
        self.assertIn("declarations changed", result["errors"][0])
        install.assert_not_called()

    def test_environment_appeared_after_planning_is_preserved(self):
        plan = self.prepare()
        venv = self.directory / ".venv"
        venv.mkdir()
        keep = venv / "keep"
        keep.write_text("existing")
        result, install = self.execute(plan)
        self.assertIn("environment appeared", result["errors"][0])
        self.assertEqual(keep.read_text(), "existing")
        install.assert_not_called()

    def test_altered_plan_fails_stored_plan_comparison(self):
        plan = self.prepare()
        changed = copy.deepcopy(plan)
        changed["dependency_plan"]["target"]["venv"] = str(self.parent / "elsewhere")
        changed["plan_id"] = deploy._plan_id(changed)
        result, install = self.execute(changed)
        self.assertIn("stored plan", result["errors"][0])
        install.assert_not_called()

    def test_authenticated_url_rewrite_is_rejected_without_exposing_token(self):
        with patch.object(deploy, "_run_git", return_value="https://secret:token@other.example/repo.git"), patch.object(deploy.shutil, "which", return_value=self.git):
            plan = deploy.plan_project_deployment(self.directory)
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("URL rewriting", plan["errors"][0])
        self.assertNotIn("secret", json.dumps(plan))
        self.assertNotIn("token@", json.dumps(plan))

    def test_timeout_reported_without_dependency_execution(self):
        with patch.object(deploy, "_run_git", side_effect=TimeoutError("network bound")), patch.object(deploy.shutil, "which", return_value=self.git):
            plan = deploy.plan_project_deployment(self.directory)
        self.assertEqual(plan["status"], "timed_out")
        self.assertNotIn("dependency_plan", plan)

    def test_git_auth_preserved_without_interactive_login_or_pip_configuration(self):
        with patch.dict(os.environ, {"HOME": str(self.parent), "GIT_CONFIG_PARAMETERS": "injected", "PIP_INDEX_URL": "http://unsafe"}):
            env = deploy._git_environment(deploy.ProxyConfig("none"))
        self.assertEqual(env["HOME"], str(self.parent))
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "Never")
        self.assertNotIn("GIT_CONFIG_PARAMETERS", env)
        self.assertNotIn("PIP_INDEX_URL", env)
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)

    def test_invalid_refs_are_rejected_before_git(self):
        for ref in ("-main", "main;echo", "../main", "main\n", "refs/remotes/main", "main.lock"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    deploy._validated_ref(ref)

    def test_annotated_tag_is_peeled_and_ambiguous_ref_is_blocked(self):
        tag = "b" * 40
        result = deploy._resolve_commit(lambda args: f"{tag}\trefs/tags/v1\n{COMMIT}\trefs/tags/v1^{{}}\n", "refs/tags/v1")
        self.assertEqual(result["commit"], COMMIT)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            deploy._resolve_commit(lambda args: f"{tag}\trefs/tags/main\n{COMMIT}\trefs/heads/main\n", "main")

    def test_source_filters_and_symlinks_are_rejected_before_checkout(self):
        with self.assertRaisesRegex(ValueError, "symlinks"):
            deploy._validate_tree(lambda args: "120000 blob " + COMMIT + "\tlink\x00", COMMIT)
        def attributes(args):
            return "100644 blob " + COMMIT + "\t.gitattributes\x00" if args[0] == "ls-tree" else "*.bin filter=custom\n"
        with self.assertRaisesRegex(ValueError, "filters"):
            deploy._validate_tree(attributes, COMMIT)

    def test_changed_git_executable_is_not_run(self):
        plan = self.prepare()
        with patch.object(deploy.shutil, "which", return_value=sys.executable), patch.object(deploy, "_run_git") as git, patch.object(deploy.manager, "execute_plan") as install:
            result = deploy.execute_project_deployment(plan, confirmed=True)
        self.assertIn("Git executable changed", result["errors"][0])
        git.assert_not_called()
        install.assert_not_called()

    def test_runner_never_uses_shell_or_auth_prompts(self):
        with patch.object(deploy.subprocess, "Popen") as popen:
            popen.return_value.returncode = 0
            result = deploy._run_git(self.git, ["status"], cwd=self.parent,
                                     proxy=deploy.ProxyConfig("none"),
                                     cancel_event=threading.Event(), deadline=float("inf"), emit=lambda event: None)
        self.assertEqual(result, "")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        argv = popen.call_args.args[0]
        self.assertIn("http.sslVerify=true", argv)
        self.assertIn("protocol.allow=never", argv)
        self.assertIn("submodule.recurse=false", argv)
        self.assertIn("core.worktree=" + str(self.parent), argv)
        self.assertIn("core.bare=false", argv)
        self.assertTrue(any(arg.startswith("core.hooksPath=") for arg in argv))
        self.assertFalse(any("credential.helper=" in arg for arg in argv))

    def test_runner_cancellation_stops_process_and_returns_actionable_status(self):
        cancel = threading.Event()
        def cancelled_wait(**kwargs):
            cancel.set()
            raise deploy.subprocess.TimeoutExpired("git", kwargs["timeout"])
        with patch.object(deploy.subprocess, "Popen") as popen, patch.object(deploy, "_stop_git", return_value=False) as stop:
            popen.return_value.wait.side_effect = cancelled_wait
            with self.assertRaisesRegex(InterruptedError, "stop confirmed=False"):
                deploy._run_git(self.git, ["fetch"], cwd=self.parent,
                                proxy=deploy.ProxyConfig("none"), cancel_event=cancel,
                                deadline=float("inf"), emit=lambda event: None)
        stop.assert_called_once()

    def test_project_only_target_guard_survives_saved_plan_tampering(self):
        plan = self.prepare()
        plan["dependency_plan"]["target"]["venv"] = str(self.parent / "wrong-environment")
        plan["plan_id"] = deploy._plan_id(plan)
        (self.directory / ".git/dependency-manager-deploy/plan.json").write_text(json.dumps(plan))
        result, install = self.execute(plan)
        self.assertIn("only this checkout", result["errors"][0])
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class CnbDeploymentTests(DeploymentTests):

    def fake_git(self, git, args, **kwargs):
        if args[:2] == ["ls-remote", "--get-url"]:
            self.commands.append(args)
            return args[-1] + "\n"
        return super().fake_git(git, args, **kwargs)

    def test_cnb_plan_pins_configured_source_and_reads_back(self):
        url = "https://cnb.cool/example/project-mirror"
        plan = self.prepare(source="cnb", repository_url=url)
        self.assertEqual(plan["status"], "ready", plan)
        self.assertEqual(plan["source"]["url"], url)
        self.assertEqual(plan["source"]["provider"], "cnb")
        self.assertEqual(deploy.read_project_deployment_plan(self.directory), plan)
        result, install = self.execute(plan)
        self.assertEqual(result["status"], "success", result)
        install.assert_called_once()

    def test_missing_cnb_url_never_creates_destination(self):
        plan = self.prepare(source="cnb")
        self.assertEqual(plan["status"], "blocked")
        self.assertFalse(self.directory.exists())

    def test_private_development_source_requires_explicit_url(self):
        self.assertEqual(deploy.repository_source()["url"], "https://github.com/ii33/coating-sim.git")
        self.assertEqual(deploy.repository_source("github", deploy.LEGACY_REPOSITORY_URL)["url"], deploy.LEGACY_REPOSITORY_URL)
