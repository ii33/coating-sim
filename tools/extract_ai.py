"""One-time, explicit source extraction used to create other/ai.

This copies source/resources only, never configuration, credentials or history.
"""
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "other" / "ai"
PACKAGE = DEST / "harness_app"


def write(relative, text):
    path = DEST / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def transform(text):
    return (text.replace("coat_sim", "harness_app")
            .replace("coat-sim", "ai-harness")
            .replace("COAT_", "HARNESS_")
            .replace("coat_primary_module", "harness_primary_module"))


def copy_file(relative):
    source = ROOT / "coat_sim" / relative
    write("harness_app/" + relative, transform(source.read_text(encoding="utf-8-sig")))


def replace(relative, before, after):
    path = DEST / relative
    text = path.read_text(encoding="utf-8")
    if before not in text:
        raise RuntimeError(f"Extraction source changed: {relative}: {before[:80]}")
    path.write_text(text.replace(before, after), encoding="utf-8", newline="\n")


def main():
    if PACKAGE.exists():
        raise RuntimeError("other/ai/harness_app already exists; do not overwrite an extracted app")
    for source in sorted((ROOT / "coat_sim/agent").glob("*.py")):
        copy_file("agent/" + source.name)
    for name in ("assistant", "assistant_desktop", "assistant_artifacts", "assistant_rich",
                 "assistant_stream_render", "assistant_render", "assistant_credentials",
                 "assistant_debug", "settings", "model_probe", "mcp_endpoint_process",
                 "token_format", "fonts", "icons", "taskbar", "preview_cursor"):
        copy_file(f"ui/{name}.py")
    for name in ("AppWindow", "WindowChrome", "SettingsWindow", "AssistantBubble",
                 "AssistantChatWindow", "AssistantDecisionWindow", "AssistantNotification",
                 "ComputeQueueWindow"):
        copy_file(f"ui/qml/{name}.qml")
    copy_file("workflow.py")
    copy_file("core/data_files.py")
    copy_file("mcp/desktop_endpoint.py")
    # Debug transport is reusable; the simulator's WindowRegistry is not.
    control = transform((ROOT / "coat_sim/ui/control.py").read_text(encoding="utf-8-sig"))
    control = control[:control.index("class WindowRegistry")]
    control = re.sub(r"from harness_app.core.api import .*\n", "", control)
    control = re.sub(r"WINDOW_TYPES = .*?\nOPERATIONS", 'WINDOW_TYPES = ("assistant", "settings")\nOPERATIONS', control, flags=re.S)
    write("harness_app/ui/control.py", control)
    for relative in ("__init__.py", "ui/__init__.py", "core/__init__.py", "mcp/__init__.py"):
        write("harness_app/" + relative, '"""Independent AI Harness application."""\n')
    for folder in ("fonts", "icons"):
        shutil.copytree(ROOT / "coat_sim/ui/assets" / folder, PACKAGE / "ui/assets" / folder)
    for name in ("assistant.png", "bubble_pop.wav"):
        shutil.copy2(ROOT / "coat_sim/ui/assets" / name, PACKAGE / "ui/assets" / name)
    shutil.copy2(ROOT / "coat_sim/ui/assets/bubble_pop_SOURCE.txt", PACKAGE / "ui/assets/bubble_pop_SOURCE.txt")
    # Same runtime, with a general-purpose workspace and workflow tool surface.
    path = DEST / "harness_app/agent/runtime.py"
    runtime = path.read_text(encoding="utf-8")
    start = runtime.index("READ_TOOLS = frozenset({")
    end = runtime.index("_BUSINESS_SERVER:", start)
    runtime = runtime[:start] + '''READ_TOOLS = frozenset({
    "list_data_files", "read_data_file", "read_software_manual", "python_sandbox_status",
    "list_workflows", "workflow_status", "workflow_runs",
})
DATA_WRITE_TOOLS = frozenset({"write_data_file", "make_data_directory", "move_data_path", "delete_data_path", "run_python_script"})
WORKFLOW_TOOLS = frozenset({"run_workflow", "resume_workflow", "cancel_workflow"})
MAIN_TOOLS = READ_TOOLS | DATA_WRITE_TOOLS | WORKFLOW_TOOLS
SUBMIT_TOOLS = frozenset()
DEVELOPMENT_TOOLS = frozenset({"developer_status", "developer_checks", "developer_persona",
    "developer_access", "developer_files", "developer_report_issue", "developer_issue_reports"})
CATALOG_TOOLS = frozenset({"project_tools"})
POLL_TOOLS = frozenset({"workflow_status"})
''' + runtime[end:]
    start = runtime.index("# Each delegation receives")
    end = runtime.index("async def compact_context", start)
    runtime = runtime[:start] + '''# Delegates retain the original Harness execution/persistence boundaries.
EXECUTION_TOOLS = {"workspace": READ_TOOLS | DATA_WRITE_TOOLS,
                   "workflow": READ_TOOLS | WORKFLOW_TOOLS}
EXECUTION_DESCRIPTIONS = {"workspace": "完成用户授权的数据文件与沙箱任务。",
                          "workflow": "执行、核对及恢复用户授权的工作流。"}


''' + runtime[end:]
    start = runtime.index('    background_notification = command.get("background_notification")')
    end = runtime.index("    history, recovery = await _restore_history", start)
    runtime = runtime[:start] + "    background_context = None\n" + runtime[end:]
    start = runtime.index("    instructions = (persona + capability_context()")
    end = runtime.index("    if external.configs:", start)
    runtime = runtime[:start] + '''    instructions = (persona + capability_context() + "\\n" + mode_instructions
        + "\\n模式不扩大用户授权。内置动作通过project_tools发现、project_action执行。"
        "工作空间为本应用data；外部附件先由用户选择导入副本。覆盖、删除及外部发送须遵循用户授权。"
        "工作流通过list_workflows列出固定定义，run_workflow执行，workflow_status核对，用户要求继续时resume_workflow。"
        "waiting不是成功；同run_id重试必须保持相同输入，未知效果先读状态，不能重复写入。"
        "使用run_python_script前先读python_sandbox_status；隔离不可用时明确说明，不降级为宿主执行。"
        "工具结果由本地archive保存，需要细节时read_archived_tool_result分页读取；不要重复查相同目录。"
        "新用户插话作为当前任务的新约束；只有明确停止才取消任务。历史计划不构成继续授权。"
        "关键条件用ask_user_question合并提问，2–3个选项、推荐项第一；重要条件必须等待真人答复。"
        "optional倒计时仅用于普通偏好，不用于权限、覆盖或重要目标；默认选项不冒充真人授权。"
        "开发工具仅在本地开发设置和源码门禁允许时开放。人设不改变权限或凭据边界。"
        "委派只传必要目标和结果引用，返回已执行动作、真实状态、文件位置及未解决事项。"
        "不编造执行结果或预计Token费用。"
        + language_instructions(config) + tone_instructions(config) + issue_instructions(config))
''' + runtime[end:]
    runtime = runtime.replace('include=["coat-mcp", "film-stack", "result-visualization"]',
                              'include=["workspace", "workflows"]')
    start = runtime.index('                    capability_context() + "\\n你是板娘的领域执行助手。')
    end = runtime.index('                    tools=[read_archived_tool_result]', start)
    runtime = runtime[:start] + '''                    capability_context() + "\\n你是AI助手的执行助手。仅完成已委派且用户授权的完整子任务。"
                    "先读取必要数据和状态；保留原文件，未知效果先核对，不重复写入。"
                    "工作流waiting时返回待决状态，不自行替用户回答。沙箱不可用时不得在宿主执行。"
                    "返回简短结果、路径、run_id及剩余事项。\\n用户当前要求：\\n"
                    + command["prompt"] + language_instructions(config) + issue_instructions(config)),
''' + runtime[end:]
    start = runtime.index("        async def project_tools(")
    end = runtime.index("        async def project_action(", start)
    runtime = runtime[:start] + '''        async def project_tools(tool_name: str = "", tool_names: list[str] | None = None) -> dict[str, Any]:
            """List tools or read up to 12 exact schemas in one step."""
            catalog = await gateway.catalog(([tool_name] if tool_name else []) + (tool_names or []))
            if tool_name and not tool_names:
                values = catalog.get("tools", [])
                return values[0] if values else {"ok": False, "error": "工具不在本轮目录中。"}
            catalog["development_enabled"] = development_enabled(config)
            return catalog

''' + runtime[end:]
    runtime = runtime.replace("明确要求设计、保存或计算", "明确要求文件操作或工作流")
    runtime = runtime.replace("工程分析", "数据分析")
    path.write_text(runtime, encoding="utf-8", newline="\n")
    replace("harness_app/agent/tool_dispatch.py", 'tools.get("create_default_workflow")', 'tools.get("run_workflow")')
    replace("harness_app/agent/tool_dispatch.py", '["input_schema", "prepare_temporary_design", "submit_process_recipe"]', '["list_workflows", "run_workflow", "workflow_status"]')
    replace("harness_app/ui/settings.py", 'source.get("assistant_enabled") is True', 'source.get("assistant_enabled", True) is True')
    replace("harness_app/ui/taskbar.py", 'APP_NAME = "沉积仿真"', 'APP_NAME = "AI助手"')
    # New credentials use their own OS-keyring namespace.
    for file in ("harness_app/ui/settings.py", "harness_app/ui/assistant_credentials.py"):
        text = (DEST / file).read_text(encoding="utf-8")
        text = text.replace("coat-model-cache-v1", "harness-model-cache-v1")
        (DEST / file).write_text(text, encoding="utf-8", newline="\n")
    # General data tools have no design-model concept.
    path = DEST / "harness_app/core/data_files.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("DESIGN_FOLDERS =")
    end = text.index("def data_path", start)
    text = text[:start] + 'MAX_FILE_BYTES = 32 * 1024 * 1024\n\n' + text[end:]
    path.write_text(text, encoding="utf-8", newline="\n")
    print("Extracted sources and offline UI resources to", DEST)


if __name__ == "__main__":
    main()
