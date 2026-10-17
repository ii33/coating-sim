"""Real QML menu gesture -> authenticated native process -> foreground HWND."""
from pathlib import Path
import ctypes,json,os,subprocess,sys,time
from ctypes import wintypes
from PySide6.QtCore import QObject,QPointF,QMetaObject
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from coat_sim.ui.control import UiControlClient
from coat_sim.assistant_process import LIFECYCLE_OPERATIONS

def run(host,root,state):
    if os.name!='nt':return {'activation_cases':[],'native_windows_only':True}
    exe=Path(sys.executable).with_name('pythonw.exe')
    child=subprocess.Popen([str(exe),'-E','-s','-X','utf8',str(root/'tests/fixtures/assistant_activation_target.py'),str(root),str(state)],cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    transport=None
    try:
        deadline=time.monotonic()+12
        while not (state/'target-ready.json').exists():
            assert child.poll() is None,'Native target exited before readiness'
            assert time.monotonic()<deadline,'Target readiness timeout'
            QTest.qWait(25)
        target=json.loads((state/'target-ready.json').read_text());endpoint=target['endpoint']
        host.registry.register_client(endpoint,generation=target['generation'],process_start=target['process_start'])
        rows=host.registry.list_windows();assert len(rows)==2 and rows[0]['id']!=rows[1]['id']
        host.controller.desktop.set_external_windows(rows)
        transport=UiControlClient(endpoint['root'],endpoint['pid'],initial_endpoint=endpoint,operations=LIFECYCLE_OPERATIONS)
        u=ctypes.WinDLL('user32',use_last_error=True);u.GetForegroundWindow.restype=wintypes.HWND
        u.GetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int];u.GetWindowLongW.restype=ctypes.c_long
        def click(x,y,right=False):
            u.SetCursorPos(round(x),round(y));u.mouse_event(8 if right else 2,0,0,0,0);u.mouse_event(16 if right else 4,0,0,0,0)
        bubble=host.controller._bubble
        menu=bubble.findChild(QObject,'assistantContextMenu');submenu=bubble.findChild(QObject,'assistantWindowListMenu')
        assert menu is not None and submenu is not None
        results=[]
        for case,key in (('normal','project-a'),('minimized','project-b'),('occluded','project-a')):
            row=next(r for r in rows if r['project_id']==key);target_window=next(r for r in target['windows'] if r['project_id']==key);hwnd=target_window['hwnd'];flags=u.GetWindowLongW(hwnd,-20)
            if case in ('minimized','occluded'):
                transport.request('window_action',project_id=key,action='minimize' if case=='minimized' else 'cover')
            was_minimized=bool(u.IsIconic(hwnd))
            click(bubble.x()+bubble.width()/2,bubble.y()+bubble.height()/2,True);QTest.qWait(120)
            assert menu.property('opened'),'Root menu did not open'
            QMetaObject.invokeMethod(submenu,'open');QTest.qWait(120)
            item=bubble.findChild(QQuickItem,'assistantWindowListItem_'+row['id']);assert item is not None
            point=item.mapToGlobal(QPointF(item.width()*.65,item.height()/2))
            foreground_before=int(u.GetForegroundWindow());click(point.x(),point.y())
            deadline=time.monotonic()+3
            while int(u.GetForegroundWindow())!=hwnd and time.monotonic()<deadline:QTest.qWait(25)
            foreground=int(u.GetForegroundWindow());QTest.qWait(100)
            sample={'case':case,'project':key,'targetHwnd':hwnd,'foregroundBefore':foreground_before,'foregroundAfter':foreground,'matched':foreground==hwnd,'stableForeground':int(u.GetForegroundWindow())==hwnd,'wasMinimized':was_minimized,'restored':not bool(u.IsIconic(hwnd)),'flagsPreserved':flags==u.GetWindowLongW(hwnd,-20),'targetPid':endpoint['pid']}
            results.append(sample)
            (state/'activation-cases.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
            assert sample['matched'] and sample['stableForeground'],sample
            assert sample['restored'] and sample['flagsPreserved'],sample
            if case=='minimized':assert was_minimized
        local = host.controller._chat
        for case in ('local-normal', 'local-minimized', 'local-occluded'):
            host.controller.show_chat()
            if case == 'local-minimized': local.showMinimized()
            elif case == 'local-occluded':
                transport.request('window_action', project_id='project-a', action='cover')
            QTest.qWait(80)
            host.controller.desktop.refreshWindows()
            identity = next(identity for identity, window in host.controller.desktop._window_refs.items() if window is local)
            hwnd = int(local.winId()); flags = u.GetWindowLongW(hwnd, -20)
            was_minimized = bool(u.IsIconic(hwnd))
            click(bubble.x()+bubble.width()/2,bubble.y()+bubble.height()/2,True);QTest.qWait(120)
            QMetaObject.invokeMethod(submenu, 'open');QTest.qWait(120)
            item=bubble.findChild(QQuickItem, 'assistantWindowListItem_'+identity);assert item is not None
            point=item.mapToGlobal(QPointF(item.width()*.65,item.height()/2))
            foreground_before=int(u.GetForegroundWindow());click(point.x(),point.y())
            deadline=time.monotonic()+3
            while int(u.GetForegroundWindow())!=hwnd and time.monotonic()<deadline:QTest.qWait(25)
            foreground=int(u.GetForegroundWindow());QTest.qWait(100)
            sample={'case':case,'targetHwnd':hwnd,'foregroundBefore':foreground_before,'foregroundAfter':foreground,'matched':foreground==hwnd,'stableForeground':int(u.GetForegroundWindow())==hwnd,'wasMinimized':was_minimized,'restored':not bool(u.IsIconic(hwnd)),'flagsPreserved':flags==u.GetWindowLongW(hwnd,-20)}
            results.append(sample);(state/'activation-cases.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
            assert sample['matched'] and sample['stableForeground'] and sample['restored'] and sample['flagsPreserved'],sample
            if case == 'local-minimized': assert was_minimized
        after=transport.request('list_windows')['windows']
        assert [(r['project_id'],r['instance_id'],r['hwnd']) for r in after]==[(r['project_id'],r['instance_id'],r['hwnd']) for r in target['windows']]
        requests=json.loads((state/'target-requests.json').read_text())
        assert [r['project_id'] for r in requests]==['project-a','project-b','project-a']
        return {'activation_cases':results,'exact_instances_reused':True,'project_routes_correct':True}
    finally:
        if transport is not None:
            transport.request('window_action',action='shutdown')
        child.wait(timeout=12)
