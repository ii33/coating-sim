from pathlib import Path
import re
import shutil

root = Path(__file__).resolve().parents[1]
app = root / 'other/ai'
pkg = app / 'harness_app'

def edit(relative, callback):
    path = app / relative
    path.write_text(callback(path.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')

def write(relative, text):
    path = app / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')

edit('harness_app/core/workflow_service.py', lambda s: s.replace('ctx.state[', 'ctx.data['))
edit('harness_app/ui/launcher.py', lambda s: s.replace('setContextProperty("settings", settings)', 'setContextProperty("bridge", settings)').replace('settings.saved.connect', 'settings.assistantSettingsSaved.connect').replace('    app.aboutToQuit.connect(settings.shutdown)\n', ''))
edit('harness_app/mcp/server.py', lambda s: s.replace('async def _stop_jobs():', 'def _stop_jobs():').replace('list(_service.engine._active)', 'list(_service.engine._active_tasks)').replace('            await _service.engine.cancel(run_id)', '            _service.engine._active_tasks[run_id].cancel()').replace('        asyncio.run(_stop_jobs())', '        _stop_jobs()').replace('return call(action, path)', 'return call(action)'))

def control(s):
    s = s[:s.index('_ACTIONS =')]
    a = s.index('        bounded = CoatService(self.root)')
    b = s.index('        command =', a)
    s = s[:a] + s[b:]
    return s.replace('(UiControlError, ServiceError)', '(UiControlError, ValueError)')
edit('harness_app/ui/control.py', control)

def desktop(s):
    s = s.replace('from harness_app.core.compute_tasks import ComputeTaskStore', 'from harness_app.core.workflow_service import WorkflowService').replace('ComputeTaskStore(ROOT).snapshot', 'WorkflowService(ROOT).snapshot')
    a = s.index('    @Slot(object, str)\n    def _receive_snapshot')
    b = s.index('    @Slot(str, bool)\n    def confirm', a)
    s = s[:a] + '''    @Slot(object, str)
    def _receive_snapshot(self, snapshot, error):
        self._fetching = False
        if self._closed:
            return
        self._status = error
        labels = {"running": "执行中", "completed": "已完成", "paused": "等待答复",
                  "failed": "失败", "cancelled": "已取消", "interrupted": "已中断"}
        self._jobs = [{**row, "stateLabel": labels.get(row.get("state"), row.get("state", "")),
                       "durationLabel": "节点按检查点推进", "finishLabel": "按实际状态查看"}
                      for row in snapshot.get("jobs", [])]
        self._confirmations = []
        self.changed.emit()

''' + s[b:]
    # Agent decisions and their notifications stay unchanged. The simulation
    # confirmation entry points no longer have a service in the extracted app.
    for start, end in [('    def _release_due(', '    def _tick_decisions('),
                       ('    @Slot(str, bool)\n    def confirm(', '    @Slot(str, str)\n    def _submission_complete(')]:
        if start in s and end in s[s.index(start):]:
            a=s.index(start); b=s.index(end,a)
            s=s[:a]+s[b:]
    s=re.sub(r'            from harness_app.core.api import CoatService\n            return CoatService\(ROOT\)\.[^\n]+', '            raise ValueError("本应用使用工作流检查点，不提供仿真放行。")', s)
    return s.replace('计算队列', '工作流队列')
edit('harness_app/ui/assistant_desktop.py', desktop)

def runtime(s):
    a=s.index('async def _cancel_jobs(')
    b=s.index('# Delegates retain',a)
    s=s[:a]+'''async def _cancel_jobs(client, ledger, session, request, issue_recorder=None):
    # Workflow requests are awaited and cancelled by their MCP request scope.
    # No simulator background jobs belong to this application.
    return


'''+s[b:]
    return s.replace('persona = (PROJECT_ROOT / "agent.md").read_text(encoding="utf-8")', 'persona_path = PROJECT_ROOT / "agent.md"\n    persona = (persona_path if persona_path.is_file() else Path(__file__).parent / "resources/default_persona.md").read_text(encoding="utf-8")')
edit('harness_app/agent/runtime.py', runtime)
edit('harness_app/ui/assistant.py', lambda s: s.replace('"execute_design": "设计助手", "execute_windows": "窗口助手",\n            "execute_simulation": "仿真助手", "execute_correction": "修正板助手"', '"execute_workspace": "文件助手", "execute_workflow": "工作流助手"'))
edit('harness_app/agent/software_manual.py', lambda s: s[:s.index('def capability_context')] + '''def capability_context() -> str:
    return "\\n[本应用能力] 持久对话、data文件、外部MCP、Harness规划与子代理、可信工作流。" \\
           "本应用不包含沉积仿真或几何设计服务。工具先通过project_tools读取精确schema。" \\
           "LPAC沙箱仅Windows可用且需私有runtime就绪；不能退回宿主执行。"
''')
write('harness_app/paths.py', '''"""Source checkout or per-user installed application state directory."""
from pathlib import Path
import os


def app_root():
    override = os.environ.get("AI_HARNESS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    source = Path(__file__).resolve().parent.parent
    if (source / "launcher.py").is_file():
        return source
    return Path.home() / ".ai-harness"


def initialize():
    root = app_root()
    root.mkdir(parents=True, exist_ok=True)
    persona = root / "agent.md"
    if not persona.exists():
        persona.write_bytes((Path(__file__).parent / "agent/resources/default_persona.md").read_bytes())
    return root
''')
# All persistent state follows the same root. Asset/skill locations remain inside
# the package, so installation does not lose offline resources.
for path in pkg.rglob('*.py'):
    if path.name=='paths.py':
        continue
    s=path.read_text(encoding='utf-8')
    old='Path(__file__).resolve().parents[2]'
    if old in s:
        s=s.replace(old,'app_root()')
        marker=s.index('\n',s.index('from __future__ import annotations'))+1 if 'from __future__ import annotations' in s else 0
        s=s[:marker]+'\nfrom harness_app.paths import app_root\n'+s[marker:]
    path.write_text(s,encoding='utf-8',newline='\n')
# Absolute MCP script launching must still add the package installation/source,
# rather than a configurable data directory.
edit('harness_app/mcp/server.py',lambda s:s.replace('sys.path.insert(0, str(app_root()))','sys.path.insert(0, str(Path(__file__).resolve().parents[2]))'))
edit('harness_app/ui/launcher.py', lambda s:s.replace('    configure_qt_dpi_scale()', '    from harness_app.paths import initialize\n    initialize()\n    configure_qt_dpi_scale()'))
edit('harness_app/ui/settings.py',lambda s:s.replace('return f\'"{self._mcp_python()}" "{PROJECT_ROOT / "harness_app" / "mcp" / "server.py"}"\'', 'return f\'"{self._mcp_python()}" -m harness_app.mcp.server\''))
# Preserve original offline assets and attribution; do not ship design module art.
for path in (pkg/'ui/qml').glob('*.qml'):
    s=path.read_text(encoding='utf-8').replace('计算队列','工作流队列').replace('沉积仿真','AI助手')
    path.write_text(s,encoding='utf-8',newline='\n')
for name in ('deposit.png','process.png','mechanic.png','mask.png','source.png','about.png'):
    (pkg/'ui/assets/icons'/name).unlink(missing_ok=True)
# Minimal icon map for the extracted windows.
edit('harness_app/ui/icons.py', lambda s:re.sub(r'WINDOW_ICON_FILES = \{.*?\n\}', 'WINDOW_ICON_FILES = {"launcher": "launcher.png", "settings": "setting.png"}',s,flags=re.S))
edit('harness_app/ui/taskbar.py',lambda s:re.sub(r'MODULE_NAMES = \{.*?\n\}', 'MODULE_NAMES = {"launcher": APP_NAME, "assistant": APP_NAME, "settings": "设置"}',s,flags=re.S))
print('Finalized standalone integration and paths')