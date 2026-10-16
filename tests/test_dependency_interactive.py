"""Terminal flow contracts: all external operations are mocked."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.dependency_manager import interactive as subject
from tools.dependency_manager.cli import parser


@pytest.fixture
def flow(monkeypatch, tmp_path):
    root = tmp_path / "install"
    identity = {"executable": "/python", "python": "3.12.9", "implementation": "CPython"}
    calls = {}
    values = {
        "discover_python": {
            "status": "ready",
            "selected_python": "/python",
            "candidates": [{"path": "/python", "identity": identity}],
        },
        "discover_git": "/git",
        "recommend_environment": {"status": "success", "selected_backend": "cpu"},
        "plan_project_deployment": {"status": "ready", "plan_id": "source"},
        "plan_environment": {"status": "ready", "plan_id": "dependencies"},
        "execute_project_deployment": {"status": "success"},
        "execute_plan": {"status": "success"},
        "plan_runtime": {"status": "ready", "plan_id": "python"},
        "execute_runtime": {"status": "success", "python": "/python"},
        "plan_git_runtime": {"status": "ready", "plan_id": "git"},
        "execute_git_runtime": {"status": "completed", "git": "/git"},
        "inspect_environment": {"status": "success", "target": identity},
        "plan_launch_entries": {"status": "ready"},
        "execute_launch_entries": {"status": "success"},
    }
    for name, value in values.items():
        calls[name] = MagicMock(return_value=value)
        monkeypatch.setattr(subject, name, calls[name])

    def run(answers):
        responses = iter(answers)
        return subject.run_interactive(
            root, input_fn=lambda prompt: next(responses), output=lambda text: None
        )

    return root, calls, run


def make_project(path):
    path.mkdir(parents=True)
    (path / "requirements.txt").write_text("numpy\n")
    (path / "launcher.py").write_text("")
    (path / "coat_sim").mkdir()


def test_default_entry_is_terminal_without_tk():
    assert parser().parse_args([]).operation == "interactive"
    assert "tkinter" not in Path(subject.__file__).read_text(encoding="utf-8")


def test_default_cnb_full_runtime_one_install_confirmation(flow):
    root, calls, run = flow
    assert run(["", "", "", "", "", "y"]) == 0
    plan = calls["plan_project_deployment"].call_args
    assert plan.args == (str(root / "app"),)
    assert plan.kwargs["source"] == "cnb"
    assert plan.kwargs["repository_url"] == subject.CNB_URL
    assert plan.kwargs["groups"] == ("desktop", "appearance")
    assert plan.kwargs["profile"] == "runtime"
    assert plan.kwargs["mirror"] == "pypi"
    calls["execute_project_deployment"].assert_called_once()


def test_github_acceleration_and_development_are_independent(flow):
    root, calls, run = flow
    assert run(["", "2", "y", "y", "", "y"]) == 0
    plan = calls["plan_project_deployment"].call_args.kwargs
    assert (plan["source"], plan["repository_url"], plan["mirror"], plan["profile"]) == (
        "github",
        None,
        "tuna",
        "development",
    )


def test_python_first_preserves_runtime_and_clones_only_new_app(flow):
    root, calls, run = flow
    runtime = root / ".runtime" / "python"
    runtime.mkdir(parents=True)
    sentinel = runtime / "preserve"
    sentinel.write_text("owned runtime")
    assert run(["", "", "", "", "", "y"]) == 0
    assert sentinel.read_text(encoding="utf-8") == "owned runtime"
    assert calls["plan_project_deployment"].call_args.args[0] == str(root / "app")


@pytest.mark.parametrize("at_root", [True, False])
def test_existing_project_installs_dependencies_without_source_overwrite(flow, at_root):
    root, calls, run = flow
    make_project(root if at_root else root / "app")
    assert run(["", "", "", "", "", "y"]) == 0
    calls["plan_project_deployment"].assert_not_called()
    calls["execute_plan"].assert_called_once()


def test_unrelated_app_is_rejected_without_discovery_or_writes(flow):
    root, calls, run = flow
    (root / "app").mkdir(parents=True)
    assert run([""]) == 2
    calls["discover_python"].assert_not_called()


def test_declined_plan_never_installs_dependencies(flow):
    root, calls, run = flow
    assert run(["", "", "", "", "", ""]) == 130
    calls["execute_project_deployment"].assert_not_called()


def test_network_failure_never_executes_plan(flow):
    root, calls, run = flow
    calls["plan_project_deployment"].return_value = {"status": "failed", "errors": ["offline"]}
    assert run(["", "", "", "", ""]) == 2
    calls["execute_project_deployment"].assert_not_called()


def test_python_install_confirmation_and_project_boundary(flow):
    root, calls, run = flow
    assert run(["", "", "", "", "y", "y"]) == 0
    assert calls["plan_runtime"].call_args.kwargs["project"] == root
    calls["execute_runtime"].assert_called_once()


def test_no_git_installs_only_after_explicit_tool_plan_confirmation(flow):
    root, calls, run = flow
    calls["discover_git"].return_value = None
    assert run(["", "", "", "", "", "y", "y"]) == 0
    assert calls["plan_git_runtime"].call_args.kwargs["project"] == root
    calls["execute_git_runtime"].assert_called_once()


def test_ctrl_c_returns_cancelled_without_installing(flow):
    root, calls, run = flow

    def interrupt(prompt):
        raise KeyboardInterrupt

    assert subject.run_interactive(root, input_fn=interrupt, output=lambda text: None) == 130
    calls["execute_project_deployment"].assert_not_called()


def test_missing_python_never_starts_source_or_dependency_install(flow):
    root, calls, run = flow
    calls["discover_python"].return_value = {
        "status": "missing",
        "selected_python": None,
        "candidates": [],
    }
    assert run(["", "", "", "", ""]) == 2
    calls["plan_project_deployment"].assert_not_called()
    calls["execute_plan"].assert_not_called()


def test_ctrl_c_during_execution_sets_core_cancellation_event(flow):
    import signal

    root, calls, run = flow
    previous = signal.getsignal(signal.SIGINT)

    def cancelled(*args, **kwargs):
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert kwargs["cancel_event"].is_set()
        return {"status": "cancelled"}

    calls["execute_project_deployment"].side_effect = cancelled
    assert run(["", "", "", "", "", "y"]) == 130
    assert signal.getsignal(signal.SIGINT) == previous


def test_empty_existing_install_root_is_valid(flow):
    root, calls, run = flow
    root.mkdir()
    assert run(["", "", "", "", "", "y"]) == 0
    assert calls["plan_project_deployment"].call_args.args[0] == str(root / "app")


def test_existing_venv_uses_compatible_compute_recommendation_without_python_replacement(flow):
    root, calls, run = flow
    make_project(root / "app")
    (root / "app" / ".venv").mkdir()
    calls["recommend_environment"].return_value = {
        "status": "success",
        "selected_backend": "cuda12",
        "backend_usable": {"cuda12": True},
    }
    assert run(["", "", "", "", "y"]) == 0
    plan = calls["plan_environment"].call_args.args
    assert plan[3] == "cuda12" and plan[6] is None
    calls["discover_python"].assert_not_called()


def test_launch_script_creation_is_included_and_existing_files_are_preserved(flow):
    root, calls, run = flow
    calls["plan_launch_entries"].return_value = {
        "status": "blocked",
        "errors": ["existing script preserved"],
    }
    assert run(["", "", "", "", "", "y"]) == 0
    calls["execute_launch_entries"].assert_not_called()
    calls["plan_launch_entries"].assert_called_once_with(root / "app", name="launch-coating")


def test_human_output_is_concise_and_json_details_are_project_local(flow):
    root, calls, run = flow
    answers = iter(["", "", "", "", "", "y"])
    lines: list[str] = []
    assert (
        subject.run_interactive(root, input_fn=lambda prompt: next(answers), output=lines.append)
        == 0
    )
    assert not any(line.startswith("{\n") for line in lines)
    assert list((root / ".dependency-manager" / "logs").glob("installer-*.json"))
    assert any("启动脚本" in line for line in lines)


def test_post_install_verifies_actual_target_and_does_not_reinstall_on_gpu_failure(flow):
    root, calls, run = flow
    actual = {
        "valid": True,
        "executable": str(root / "app" / ".venv" / "bin" / "python"),
        "python": "3.12.9",
    }
    calls["inspect_environment"].return_value = {"status": "success", "target": actual}
    calls["recommend_environment"].side_effect = [
        {"status": "success", "selected_backend": "cuda12"},
        {"status": "success", "selected_backend": "cuda12", "backend_usable": {"cuda12": False}},
    ]
    assert run(["", "", "", "", "", "y"]) == 0
    assert calls["recommend_environment"].call_args.args == (actual,)
    assert calls["recommend_environment"].call_args.kwargs["manual_backend"] == "cuda12"
    calls["execute_project_deployment"].assert_called_once()


@pytest.mark.parametrize("approve_git", [False, True])
def test_real_windows_git_plan_source_string_is_displayed_without_crashing(flow, approve_git):
    # This is the real read-only plan, including its vendor source string,
    # official artifact/hash/license and parent identity. Never download Git.
    from tools.dependency_manager.git_runtime import plan_git_runtime

    root, calls, run = flow
    plan = plan_git_runtime(project=root, platform_name="win32", machine="AMD64")
    assert plan["status"] == "ready"
    assert isinstance(plan["source"], str)
    calls["discover_git"].return_value = None
    calls["plan_git_runtime"].return_value = plan
    answers = iter(["", "", "", "", "", "y" if approve_git else "", "y"])
    lines: list[str] = []
    code = subject.run_interactive(root, input_fn=lambda prompt: next(answers), output=lines.append)
    assert code == (0 if approve_git else 130)
    assert "工具来源：" + plan["source"] in lines
    assert any(plan["artifact"]["sha256"] in line for line in lines)
    assert any(plan["license"] in line for line in lines)
    assert calls["execute_git_runtime"].call_count == int(approve_git)
    if approve_git:
        assert calls["execute_git_runtime"].call_args.args[0] == plan
        calls["execute_project_deployment"].assert_called_once()
    else:
        calls["execute_project_deployment"].assert_not_called()


def test_project_source_mapping_still_displays_commit(flow):
    root, calls, run = flow
    source = {"url": subject.CNB_URL, "commit": "a" * 40}
    calls["plan_project_deployment"].return_value = {"status": "ready", "source": source}
    answers = iter(["", "", "", "", "", "y"])
    lines: list[str] = []
    assert (
        subject.run_interactive(root, input_fn=lambda prompt: next(answers), output=lines.append)
        == 0
    )
    assert f"源码：{source['url']} @ {source['commit']}" in lines


def test_optional_display_fields_do_not_assume_mapping_or_list(flow):
    root, calls, run = flow
    calls["recommend_environment"].return_value = {
        "status": "success",
        "selected_backend": "cpu",
        "python": "/python",
        "visible_gpus": ["unclassified device", {"name": "example GPU"}],
        "errors": None,
        "warnings": "single warning",
    }
    calls["plan_project_deployment"].return_value = {
        "status": "blocked",
        "dependency_plan": "not a nested plan",
        "target": "/target",
        "artifact": "not an artifact mapping",
        "actions": None,
        "packages": None,
        "errors": "blocked reason",
        "warnings": None,
    }
    answers = iter(["", "", "", "", ""])
    lines: list[str] = []
    assert (
        subject.run_interactive(root, input_fn=lambda prompt: next(answers), output=lines.append)
        == 2
    )
    assert "single warning" in lines and "blocked reason" in lines
    assert any("unclassified device" in line and "example GPU" in line for line in lines)
    calls["execute_project_deployment"].assert_not_called()
