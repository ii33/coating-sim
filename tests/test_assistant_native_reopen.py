"""Native owned-process regressions; never attach to a user's assistant runtime."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT/'tests/fixtures/assistant_native_probe.py'
pytestmark = pytest.mark.qt

def run_probe(tmp_path, mode, *, runtime=None, application_root=ROOT):
    state = tmp_path/mode
    state.mkdir()
    runtime = runtime or tmp_path/'private-runtime'
    report = state/'report.json'
    env = os.environ.copy()
    env.update({'COAT_ASSISTANT_RUNTIME_ROOT': str(runtime),
        'QT_QPA_PLATFORM': 'windows' if os.name=='nt' else 'offscreen',
        'QT_QUICK_BACKEND':'software', 'QSG_RHI_BACKEND':'software',
        'PYTHONDONTWRITEBYTECODE':'1', 'APPDATA':str(state/'roaming'),
        'LOCALAPPDATA':str(state/'local'), 'XDG_CACHE_HOME':str(state/'cache')})
    if mode == 'fault_handler_policy':
        env['PYTHONFAULTHANDLER']='1'
        env.pop('COAT_NATIVE_TRACE',None)
    with (state/'native.log').open('wb') as log:
        # Every handle below was created here; no PID scan or shared lock cleanup.
        process = subprocess.Popen([sys.executable,'-B',str(PROBE),str(application_root),str(state),
            str(runtime),str(report),mode],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
            raise AssertionError('Owned native probe timed out: '+str(state/'native.log')) from None
    result = json.loads(report.read_text()) if report.exists() else {}
    assert code == (23 if mode=='crash' else 0), (code, result, str(state/'native.log'))
    return result

def test_native_close_hide_reopen_keeps_assistant_alive(tmp_path):
    result=run_probe(tmp_path,'close_cycle')
    assert result['ok'] and result['quit_on_last'] is False

def test_native_first_user_can_enable_then_close_and_reopen(tmp_path):
    assert run_probe(tmp_path,'first_user')['ok']

@pytest.mark.parametrize('mode',['destroy_chat','destroy_bubble'])
def test_native_destroyed_window_recovers_without_restarting_service(tmp_path,mode):
    assert run_probe(tmp_path,mode)['ok']

def test_native_crash_then_restart_reuses_os_lock_without_clearing(tmp_path):
    runtime=tmp_path/'private-runtime'
    first=run_probe(tmp_path,'crash',runtime=runtime)
    assert (runtime/'assistant.lock').is_file()
    second=run_probe(tmp_path,'close_cycle',runtime=runtime)
    assert second['ok'] and second['generation'] != first['generation']

def test_native_chinese_runtime_and_inherited_fault_handler(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows native handler policy')
    assert run_probe(tmp_path,'fault_handler_policy',runtime=tmp_path/'小刘膜系工程'/'助手运行')['ok']


def test_native_singleton_identifies_real_pid_and_keeps_existing_owner(tmp_path):
    from coat_sim.assistant_process import AssistantProcessClient
    state=tmp_path/'owner'
    state.mkdir()
    runtime=tmp_path/'private-runtime'
    report=state/'report.json'
    env=os.environ.copy()
    env.update({'COAT_ASSISTANT_RUNTIME_ROOT':str(runtime),
                'QT_QPA_PLATFORM':'windows' if os.name=='nt' else 'offscreen',
                'QT_QUICK_BACKEND':'software','QSG_RHI_BACKEND':'software',
                'LOCALAPPDATA':str(state/'local'),'APPDATA':str(state/'roaming')})
    with (state/'native.log').open('wb') as log:
        process=subprocess.Popen([sys.executable,'-B',str(PROBE),str(ROOT),str(state),str(runtime),str(report),'hold'],
                                 cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+30
            while not report.exists() and time.monotonic()<deadline:
                assert process.poll() is None, str(state/'native.log')
                time.sleep(.05)
            assert report.exists(), str(state/'native.log')
            owner=json.loads(report.read_text())
            client=AssistantProcessClient(runtime,application_root=state/'app',timeout=2)
            before=client.ensure_running(timeout=3)
            assert before['pid']==owner['pid']  # Windows venv redirector PID may differ.
            assert run_probe(tmp_path,'duplicate',runtime=runtime)=={}
            after=client.status()
            assert after['pid']==before['pid'] and after['generation']==before['generation']
        finally:
            (state/'stop').write_text('stop')
            process.wait(timeout=35)  # Owned probe also has its own 30s exit watchdog.


def test_native_chinese_installation_source_and_qml_paths(tmp_path):
    source=tmp_path/'工具'/'小刘膜系工程'/'app'
    source.mkdir(parents=True)
    # Source/assets only: never copy, move or install an existing environment.
    shutil.copytree(ROOT/'coat_sim',source/'coat_sim',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    assert run_probe(tmp_path,'close_cycle',application_root=source)['ok']
