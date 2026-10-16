"""Offline status/source contracts, including a real disposable Git checkout."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.dependency_manager import cli, project_status as status, project_deploy as deploy


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "coat_sim").mkdir()
        (self.root / "coat_sim/version.txt").write_text("dev.20261016.01\n")
        self.environment = {"status": "success", "target": {"valid": False}, "packages": []}
        self.probe = patch.object(status.manager, "inspect_environment", return_value=self.environment)
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def test_archive_has_version_but_no_invented_git_provenance(self):
        with patch.object(deploy, "_run_git") as git:
            result = status.check_project(self.root)
        self.assertEqual(result["internal_version"], "dev.20261016.01")
        for key in ("commit", "ref", "dirty"):
            self.assertIsNone(result[key])
        self.assertEqual(result["provenance"], "unknown")
        self.assertEqual(result["environment"], self.environment)
        git.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "Git required for disposable checkout")
    def test_real_git_dirty_check_changes_no_index_or_files(self):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()
        git("init", "--quiet", "--template=")
        git("add", "coat_sim/version.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        git("remote", "add", "origin", "https://cnb.cool/example/mirror")
        before = (self.root / ".git/index").read_bytes()
        result = status.check_project(self.root)
        self.assertEqual(result["commit"], git("rev-parse", "HEAD"))
        self.assertFalse(result["dirty"])
        self.assertEqual(result["source"]["provider"], "cnb")
        (self.root / "user-work.txt").write_text("preserve")
        self.assertTrue(status.check_project(self.root)["dirty"])
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        self.assertEqual((self.root / "user-work.txt").read_text(), "preserve")

    def test_offline_and_timeout_keep_local_version(self):
        for error, expected in ((ValueError("network unavailable"), "failed"),
                                (TimeoutError("timeout"), "timed_out"),
                                (InterruptedError("cancelled"), "cancelled")):
            with patch.object(status.shutil, "which", return_value="git"), patch.object(deploy, "_run_git", side_effect=error):
                result = status.check_project(self.root, check_remote=True)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["comparison"], "unknown")
            self.assertEqual(result["internal_version"], "dev.20261016.01")

    def test_remote_equality_and_difference_are_not_ahead_behind(self):
        (self.root / ".git").mkdir()
        def git(executable, args, **kwargs):
            if args[:2] == ["rev-parse", "--verify"]:
                return "a" * 40
            if args[:2] == ["ls-remote", "--get-url"]:
                return deploy.REPOSITORY_URL
            if args[0] == "ls-remote":
                return "b" * 40 + "\trefs/heads/main\n"
            return ""
        with patch.object(status.shutil, "which", return_value="git"), patch.object(deploy, "_run_git", side_effect=git):
            result = status.check_project(self.root, check_remote=True)
        self.assertEqual(result["comparison"], "different")
        self.assertEqual(result["remote"]["commit"], "b" * 40)

    def test_source_validation_rejects_credentials_and_non_project_github(self):
        for source, url in (("cnb", None), ("cnb", "https://token@cnb.cool/a/b"),
                            ("cnb", "https://cnb.cool/a/b?token=secret"),
                            ("cnb", "http://cnb.cool/a/b"), ("cnb", "https://evil.test/a/b"),
                            ("github", "https://github.com/other/project.git")):
            with self.assertRaises(ValueError):
                deploy.repository_source(source, url)
        self.assertEqual(deploy.repository_source("cnb", "https://cnb.cool/group/project.git")["provider"], "cnb")

    def test_cli_check_is_offline_by_default(self):
        with patch.object(cli, "check_project", return_value={"operation": "project_check", "status": "success"}) as check, patch.object(cli.signal, "signal"), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["project-check", "--json"]), 0)
        self.assertFalse(check.call_args.kwargs["check_remote"])
        self.assertEqual(json.loads(output.getvalue())["status"], "success")

class ManagedGitIntegrationTests(StatusTests):
    def test_managed_git_preferred_without_path_changes(self):
        import os
        original_path = os.environ.get("PATH")
        with patch.object(status, "find_managed_git", return_value="/private/cmd/git.exe"), patch.object(status.shutil, "which") as system_git, patch.object(deploy, "_run_git") as run:
            run.side_effect = [deploy.REPOSITORY_URL, "a" * 40 + "\trefs/heads/main\n"]
            result = status.check_project(self.root, check_remote=True)
        self.assertEqual(result["status"], "success")
        system_git.assert_not_called()
        self.assertEqual(run.call_args.args[0], "/private/cmd/git.exe")
        self.assertEqual(os.environ.get("PATH"), original_path)

class FilterSafetyTests(StatusTests):
    @unittest.skipUnless(shutil.which("git"), "Git required for real filter regression")
    def test_readonly_status_never_executes_clean_or_process_filters(self):
        import os
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()
        git("init", "--quiet", "--template=")
        target = self.root / "filtered.txt"
        target.write_text("first\n")
        (self.root / ".gitattributes").write_text("filtered.txt filter=custom\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        original_index = (self.root / ".git/index").read_bytes()
        original_stat = target.stat()
        target.write_text("other\n")  # Same size defeats a size-only dirty shortcut.
        os.utime(target, (original_stat.st_atime + 2, original_stat.st_mtime + 2))
        for kind in ("clean", "process"):
            with self.subTest(filter=kind):
                git("config", "filter.custom." + kind, "echo ran > SENTINEL; cat")
                git("config", "filter.custom.required", "true")
                config = (self.root / ".git/config").read_bytes()
                result = status.check_project(self.root)
                self.assertEqual(result["status"], "success", result)
                self.assertTrue(result["dirty"])
                self.assertFalse((self.root / "SENTINEL").exists())
                self.assertEqual((self.root / ".git/index").read_bytes(), original_index)
                self.assertEqual((self.root / ".git/config").read_bytes(), config)
                self.assertEqual(target.read_text(), "other\n")

class SubmoduleSafetyTests(StatusTests):
    def test_git_environment_disables_system_attributes(self):
        import os
        with patch.dict(os.environ, {"GIT_ATTR_NOSYSTEM": "0"}):
            self.assertEqual(deploy._git_environment(status.ProxyConfig("none"))["GIT_ATTR_NOSYSTEM"], "1")

    @unittest.skipUnless(shutil.which("git"), "Git required for real gitlink regression")
    def test_readonly_status_does_not_enter_submodule_filters(self):
        import os
        def git(directory, *args):
            return subprocess.check_output(["git", *args], cwd=directory, text=True, stderr=subprocess.DEVNULL).strip()
        git(self.root, "init", "--quiet", "--template=")
        sub = self.root / "sub"
        sub.mkdir()
        git(sub, "init", "--quiet", "--template=")
        target = sub / "f"
        target.write_text("first")
        (sub / ".gitattributes").write_text("f filter=custom\n")
        git(sub, "add", ".")
        git(sub, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "sub fixture")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "parent gitlink")
        git(sub, "config", "filter.custom.clean", "echo ran > SENTINEL; cat")
        before = {p: p.read_bytes() for p in (self.root / ".git/index", sub / ".git/index", sub / ".git/config")}
        stamp = target.stat()
        target.write_text("other")
        os.utime(target, (stamp.st_atime + 2, stamp.st_mtime + 2))
        result = status.check_project(self.root)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["submodule_dirty"], "not_checked")
        self.assertFalse(result["dirty"])  # Only the parent worktree is inspected.
        self.assertFalse((sub / "SENTINEL").exists())
        self.assertIn("Submodule worktrees are not inspected", " ".join(result["warnings"]))
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
