"""Thin project deployment CLI routing; no network or package installation."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from tools.dependency_manager import cli


class ProjectCliTests(unittest.TestCase):
    def plan(self):
        return {
            "schema_version": 1, "operation": "project_deploy_plan", "status": "ready",
            "plan_id": "reviewed-plan", "directory": "/selected/project", "profile": "development",
            "source": {"url": "https://github.com/ii33/deposition-sim.git",
                       "commit": "a" * 40, "requested_ref": "main"},
            "dependency_plan": {
                "plan_id": "nested-environment-plan", "profile": "development",
                "target": {"venv": "/selected/project/.venv", "interpreter": "/selected/project/.venv/bin/python"},
                "packages": [{"name": "pytest", "requirement": "pytest>=8", "status": "missing"}],
                "actions": [{"kind": "create_venv", "argv": ["/normal/python", "-I", "-m", "venv", "/selected/project/.venv"]}],
            },
            "warnings": [], "errors": [],
        }

    def run_cli(self, args):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(cli.signal, "signal"):
            code = cli.main(args)
        return code, output.getvalue()

    def test_plan_passes_profile_groups_sources_and_explicit_interpreter(self):
        with patch.object(cli, "plan_project_deployment", return_value=self.plan()) as prepare:
            code, output = self.run_cli([
                "project-plan", "--deployment-dir", "/selected/project", "--ref", "refs/tags/v1",
                "--profile", "development", "--groups", "cpu", "--mirror", "tuna",
                "--python", "/normal/python", "--proxy-mode", "none", "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["plan_id"], "reviewed-plan")
        self.assertEqual(prepare.call_args.args[:7],
                         ("/selected/project", "refs/tags/v1", ("cpu",), "cpu", "development", "tuna", "/normal/python"))
        self.assertEqual(prepare.call_args.kwargs["timeout"], 300)

    def test_execute_loads_saved_plan_without_preparing_again(self):
        plan = self.plan()
        result = {"operation": "project_deploy", "status": "success", "warnings": [], "errors": []}
        with patch.object(cli, "read_project_deployment_plan", return_value=plan) as load, patch.object(cli, "execute_project_deployment", return_value=result) as execute, patch.object(cli, "plan_project_deployment") as prepare:
            code, output = self.run_cli(["project-deploy", "--deployment-dir", "/selected/project", "--plan-id", "reviewed-plan", "--yes", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "success")
        load.assert_called_once_with("/selected/project")
        prepare.assert_not_called()
        self.assertIs(execute.call_args.args[0], plan)
        self.assertEqual(execute.call_args.args[0]["profile"], "development")
        self.assertTrue(execute.call_args.args[1])

    def test_missing_or_wrong_approval_never_confirms(self):
        cases: tuple[list[str], ...] = ([], ["--yes"], ["--plan-id", "reviewed-plan"], ["--yes", "--plan-id", "wrong-plan"])
        for approval in cases:
            with self.subTest(approval=approval), patch.object(cli, "read_project_deployment_plan", return_value=self.plan()), patch.object(cli, "execute_project_deployment", return_value={"operation": "project_deploy", "status": "blocked", "errors": [], "warnings": []}) as execute:
                code, _output = self.run_cli(["project-deploy", "--deployment-dir", "/selected/project", "--json", *approval])
                self.assertEqual(code, 2)
                self.assertFalse(execute.call_args.args[1])

    def test_explicit_directory_is_required_before_any_source_operation(self):
        with patch.object(cli, "plan_project_deployment") as prepare, patch.object(cli, "read_project_deployment_plan") as load:
            code, output = self.run_cli(["project-plan", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("--deployment-dir", json.loads(output)["errors"][0])
        prepare.assert_not_called()
        load.assert_not_called()

    def test_arbitrary_environment_override_is_rejected(self):
        with patch.object(cli, "plan_project_deployment") as prepare:
            code, output = self.run_cli(["project-plan", "--deployment-dir", "/selected/project", "--venv", "/other/environment", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("checkout/.venv", json.loads(output)["errors"][0])
        prepare.assert_not_called()

    def test_human_plan_displays_commit_target_actions_and_outer_approval_id(self):
        with patch.object(cli, "plan_project_deployment", return_value=self.plan()):
            code, output = self.run_cli(["project-plan", "--deployment-dir", "/selected/project"])
        self.assertEqual(code, 0)
        self.assertIn("获取本项目", output)
        self.assertIn("a" * 40, output)
        self.assertIn("/selected/project/.venv", output)
        self.assertIn("create_venv", output)
        self.assertIn("plan_id: reviewed-plan", output)
        self.assertNotIn("plan_id: nested-environment-plan", output)

    def test_events_keep_source_phase_and_single_final_result(self):
        def prepare(*args, **kwargs):
            kwargs["emit"]({"event": "phase", "phase": "fetch-pinned-project"})
            return self.plan()
        with patch.object(cli, "plan_project_deployment", side_effect=prepare):
            code, output = self.run_cli(["project-plan", "--deployment-dir", "/selected/project", "--events"])
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["phase"], "fetch-pinned-project")
        self.assertEqual(rows[-1]["event"], "result")
        self.assertEqual(sum(row.get("event") == "result" for row in rows), 1)

    def test_profile_enabled_existing_inspection_is_preserved(self):
        with patch.object(cli, "inspect_environment", return_value={"operation": "inspect", "status": "success", "errors": [], "warnings": []}) as inspect:
            code, _output = self.run_cli(["inspect", "--profile", "development", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(inspect.call_args.args[-1], "development")

    def test_generic_build_operations_are_not_exposed(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(["build-plan"])


if __name__ == "__main__":
    unittest.main()

class LaunchCliTests(ProjectCliTests):

    def test_launch_creation_needs_reviewed_current_plan(self):
        from tools.dependency_manager import launchers
        plan = {"operation": "launch_entries", "status": "ready", "plan_id": "current"}
        with patch.object(launchers, "plan_launch_entries", return_value=plan) as prepare, patch.object(launchers, "execute_launch_entries", return_value={"operation": "launch_entries", "status": "success"}) as execute:
            code, _ = self.run_cli(["launch-create", "--project", "/project", "--shortcut", "custom", "--shortcut-dir", "/links", "--launch-name", "My Coating", "--plan-id", "current", "--yes", "--json"])
        self.assertEqual(code, 0)
        prepare.assert_called_once_with("/project", shortcut="custom", output_dir="/links", name="My Coating")
        execute.assert_called_once_with(plan, confirmed=True)
        with patch.object(launchers, "plan_launch_entries", return_value=plan), patch.object(launchers, "execute_launch_entries", return_value={"operation": "launch_entries", "status": "blocked"}) as execute:
            self.run_cli(["launch-create", "--yes", "--json"])
        execute.assert_called_once_with(plan, confirmed=False)

class GitRuntimeCliTests(ProjectCliTests):
    def test_private_git_install_uses_reviewed_plan_and_success_exit(self):
        from tools.dependency_manager import git_runtime
        plan = {"operation": "git_runtime_plan", "status": "ready", "plan_id": "git-plan"}
        with patch.object(git_runtime, "plan_git_runtime", return_value=plan) as prepare, patch.object(git_runtime, "execute_git_runtime", return_value={"operation": "git_runtime_install", "status": "completed", "git": "/private/cmd/git.exe"}) as execute:
            code, output = self.run_cli(["git-install", "--git-dir", "/private", "--plan-id", "git-plan", "--yes", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["git"], "/private/cmd/git.exe")
        self.assertEqual(prepare.call_args.args[0], "/private")
        self.assertTrue(execute.call_args.args[1])
        with patch.object(git_runtime, "plan_git_runtime", return_value=plan), patch.object(git_runtime, "execute_git_runtime", return_value={"operation": "git_runtime_install", "status": "failed"}) as execute:
            self.run_cli(["git-install", "--yes", "--json"])
        self.assertFalse(execute.call_args.args[1])
