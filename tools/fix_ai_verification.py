from pathlib import Path
root=Path(__file__).resolve().parents[1]/'other/ai'
def edit(name, callback):
 p=root/name;p.write_text(callback(p.read_text(encoding='utf-8')),encoding='utf-8',newline='\n')
edit('harness_app/agent/runtime.py', lambda s:s.replace('coat-local-no-auth','harness-local-no-auth').replace('return MediaChatModel(model_name, provider=OpenAIProvider(openai_client=sdk), profile=profile)', '''# SDK 3.24 validates request headers separately from client defaults.
    # Loopback no-auth services need the omission on each request as well.
    settings = {"extra_headers": {"Authorization": Omit()}} if sdk.api_key == "harness-local-no-auth" else None
    return MediaChatModel(model_name, provider=OpenAIProvider(openai_client=sdk), profile=profile, settings=settings)'''))
edit('harness_app/mcp/server.py',lambda s:s.replace('import asyncio\n','import asyncio\nfrom functools import wraps\nimport inspect\n').replace('from mcp.server import MCPServer','from mcp.server import MCPServer\nfrom mcp.server.mcpserver.exceptions import ToolError').replace('for function in (data_files.list_data_files', '''def guarded(function):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def call(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except (ValueError, PermissionError, OSError) as exc:
                raise ToolError(str(exc)) from None
    else:
        @wraps(function)
        def call(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (ValueError, PermissionError, OSError) as exc:
                raise ToolError(str(exc)) from None
    return call


def tool(function):
    mcp.add_tool(guarded(function))
    return function


for function in (data_files.list_data_files''').replace('    mcp.add_tool(function)','    mcp.add_tool(guarded(function))').replace('@mcp.tool()','@tool'))
edit('tests/test_app.py',lambda s:s.replace("'index':0,'text':'','explicit':True", "'selection_index':0,'free_text':'','automatic':False").replace("        received=queue.Queue()", "        received=queue.Queue()\n        stderr_lines=[]\n        threading.Thread(target=lambda: stderr_lines.extend(worker.stderr),daemon=True).start()").replace('error=worker.stderr.read()',"error=''.join(stderr_lines)").replace("self.assertTrue(any(e.get('event')=='decision_requested' for e in events))", "self.assertTrue(any(e.get('event')=='decision_requested' for e in events))\n            self.assertTrue(any(e.get('source')=='execute_workspace' for e in events), 'Delegate must actually run')").replace("        if shots.is_dir():\n", "        self.assertTrue(shots.is_dir(), result.stdout+result.stderr)\n        self.assertGreaterEqual(len(list(shots.glob('*.png'))),5,result.stdout+result.stderr)\n        if shots.is_dir():\n"))
edit('harness_app/ui/launcher.py',lambda s:s.replace('    from PySide6.QtQml import QQmlApplicationEngine', '    from PySide6.QtQuick import QQuickWindow\n    from PySide6.QtQml import QQmlApplicationEngine').replace('        def capture():', '        capture_errors = []\n        def capture_images():').replace('        QTimer.singleShot(250, capture)', '''        def capture():
            try:
                capture_images()
            except Exception as error:
                capture_errors.append(str(error))
                app.exit(2)
        QTimer.singleShot(250, capture)''').replace('        print("GUI smoke test passed:', '''        if capture_errors:
            raise RuntimeError("; ".join(capture_errors))
        print("GUI smoke test passed:'''))
# Skip native taskbar calls on an offscreen platform without native HWNDs.
edit('harness_app/ui/icons.py',lambda s:s.replace('        if native_icon.is_file():','        if native_icon.is_file() and app.platformName() != "offscreen":'))
print('Adjusted loopback SDK requests and made GUI verification fail on render errors')