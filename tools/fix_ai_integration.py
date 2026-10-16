from pathlib import Path
root=Path(__file__).resolve().parents[1]/'other/ai'
def edit(name, callback):
 p=root/name; p.write_text(callback(p.read_text(encoding='utf-8')),encoding='utf-8',newline='\n')
edit('harness_app/agent/runtime.py',lambda s:s.replace('async def _cancel_jobs(client, ledger, session, request, issue_recorder=None):','async def _cancel_jobs(client, ledger, session, request, gui_calculations=None, issue_recorder=None):'))
edit('harness_app/ui/control.py',lambda s:s.replace('"code": exc.code,','"code": getattr(exc, "code", "invalid_arguments"),'))
edit('harness_app/agent/external_mcp.py',lambda s:s.replace('"deposition-sim"','"ai-harness"').replace('[str(PROJECT_ROOT / "harness_app" / "mcp" / "server.py")]', '["-m", "harness_app.mcp.server"]'))
edit('harness_app/mcp/server.py',lambda s:s.replace('from harness_app.paths import app_root\n','').replace('from mcp.server import MCPServer','from harness_app.paths import app_root\nfrom mcp.server import MCPServer'))
edit('harness_app/ui/launcher.py',lambda s:s.replace('    if args.smoke_test:\n        assistant.desktop.showQueue()', '''    if args.smoke_test:
        assistant.desktop.showQueue()
        assistant.desktop.notify("Smoke notification", sound=False)
        assistant.desktop._receive_decision({"decision_id": "smoke", "request_id": "smoke",
            "question": "Smoke decision", "options": [{"label": "Continue", "description": "Proceed"},
            {"label": "Stop", "description": "Pause"}], "optional": False})
        def capture():
            from harness_app.paths import app_root
            output = app_root() / "artifacts/verification/screenshots"
            output.mkdir(parents=True, exist_ok=True)
            for name, item in (("settings", window), ("chat", assistant._chat),
                               ("bubble", assistant._bubble), ("queue", assistant.desktop._queue_window),
                               ("decision", assistant.desktop._decision_window)):
                if item is not None:
                    image = item.grabWindow()
                    if image.isNull() or not image.save(str(output / (name + ".png"))):
                        raise RuntimeError("Could not capture " + name)
        QTimer.singleShot(250, capture)''').replace('QTimer.singleShot(400, app.quit)','QTimer.singleShot(800, app.quit)'))
# Resolve scientific-plot TOC to the existing copied section identity.
p=root/'docs/algorithms.html'; s=p.read_text(encoding='utf-8')
import re
ids=re.findall(r'<section id="([^"]+)"',s)
plot=next(i for i in ids if i not in ('sandbox-visualization','workflow-host'))
s=s.replace('#sandbox-scientific-plots','#'+plot)
p.write_text(s,encoding='utf-8',newline='\n')
print('Fixed cancellation, MCP export and GUI verification')