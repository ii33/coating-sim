"""Real disk/process checks for bounded, project-local diagnostics."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from coat_sim.project_logging import CHANNELS, LOG_BACKUPS, MAX_LOG_BYTES, ProjectLog, log_path

pytestmark = pytest.mark.contract


def test_log_rotation_bounds_bytes_names_and_preserves_utf8(tmp_path):
    stream=ProjectLog('assistant',tmp_path,max_bytes=256)
    for index in range(30):
        stream.write(f'{index:02d} 中文诊断 ' + 'x'*80 + '\n')
    stream.write('过长输出'*500)
    files=sorted((tmp_path/'artifacts/logs').glob('*.log*'))
    assert len(files)==3
    assert {path.name for path in files}=={'assistant.log','assistant.log.1','assistant.log.2'}
    assert all(path.stat().st_size<=256 for path in files)
    assert 'truncated' in stream.path.read_text(encoding='utf-8')
    for path in files:
        path.read_text(encoding='utf-8')
    assert LOG_BACKUPS==2 and MAX_LOG_BYTES==2*1024*1024
    assert len(CHANNELS)*MAX_LOG_BYTES*(LOG_BACKUPS+1)==54*1024*1024


def test_existing_oversized_log_is_bounded_before_rotation(tmp_path):
    path=log_path('desktop',tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'old'*1000)
    ProjectLog('desktop',tmp_path,max_bytes=128).write('new launch\n')
    assert path.read_text()=='new launch\n'
    assert path.with_name('desktop.log.1').stat().st_size==128


def test_client_failures_persist_without_console_in_installation_not_runtime(tmp_path):
    from coat_sim.assistant_process import AssistantProcessClient
    client=AssistantProcessClient(tmp_path/'shared-runtime',application_root=tmp_path/'app')
    with patch('sys.stderr',None):
        client.trace('assistant_registration_failed',error=OSError('connection refused token=private-value'))
    path=log_path('assistant-client',tmp_path/'app')
    row=json.loads(path.read_text(encoding='utf-8'))
    assert row['pid']==os.getpid() and row['timestamp'].endswith('Z')
    assert row['event']=='assistant_registration_failed' and row['error_type']=='OSError'
    assert 'private-value' not in path.read_text()
    assert not (tmp_path/'shared-runtime/artifacts/logs').exists()


def test_multiprocess_writers_rotate_without_interleaving_or_growth(tmp_path):
    script='''import json,sys
from coat_sim.project_logging import ProjectLog
stream=ProjectLog('assistant',sys.argv[1],max_bytes=1024)
for index in range(200):
    stream.write(json.dumps({'writer':sys.argv[2],'index':index,'text':'中文诊断'})+'\\n')
'''
    children=[]
    try:
        for index in range(4):
            children.append(subprocess.Popen([sys.executable,'-B','-c',script,str(tmp_path),str(index)],
                cwd=Path(__file__).resolve().parents[1],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE))
        for child in children:
            _,error=child.communicate(timeout=20)
            assert child.returncode==0,error
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()  # Only handles created in this test.
                child.wait(timeout=5)
    files=list((tmp_path/'artifacts/logs').glob('*.log*'))
    assert len(files)==3 and all(path.stat().st_size<=1024 for path in files)
    for path in files:
        assert all(json.loads(line)['text']=='中文诊断' for line in path.read_text(encoding='utf-8').splitlines())


def test_detached_entry_retains_protocol_stdout_and_captures_import_failure(tmp_path,monkeypatch):
    from coat_sim import logged_process
    original=(sys.stdout,sys.stderr,sys.__stdout__,sys.__stderr__)
    protocol=io.StringIO()
    def fail(module,**_kwargs):
        assert module=='coat_sim.agent.worker' and sys.argv==[module,'argument']
        print('{"event":"ready"}')
        print('before import failure',file=sys.stderr)
        raise ImportError('owned bootstrap failure')
    monkeypatch.setattr(logged_process.runpy,'run_module',fail)
    monkeypatch.setattr(sys,'argv',['logged','--channel','assistant-worker','--project-root',str(tmp_path),
        '--stderr-only','--module','coat_sim.agent.worker','--','argument'])
    try:
        sys.stdout=protocol
        with pytest.raises(SystemExit) as error:
            logged_process.main()
        assert error.value.code==1
    finally:
        for name,value in zip(("stdout","stderr","__stdout__","__stderr__"),original,strict=True):
            setattr(sys,name,value)
    assert json.loads(protocol.getvalue())=={'event':'ready'}
    assert 'ImportError: owned bootstrap failure' in log_path('assistant-worker',tmp_path).read_text()


def test_unknown_channels_cannot_create_unbounded_files_or_escape_root(tmp_path):
    with pytest.raises(ValueError):
        ProjectLog('../escape',tmp_path)
    assert not list(tmp_path.iterdir())


def test_log_io_failure_does_not_replace_business_result(tmp_path,monkeypatch):
    stream=ProjectLog('assistant',tmp_path)
    monkeypatch.setattr(Path,'open',lambda *_a,**_k: (_ for _ in ()).throw(OSError('disk full')))
    assert stream.write('lost diagnostic')==len('lost diagnostic')


def test_real_logged_worker_keeps_json_protocol_and_private_log(tmp_path):
    from coat_sim.project_logging import logged_module_command
    command=logged_module_command('coat_sim.agent.worker',channel='assistant-worker',
        project_root=tmp_path,stderr_only=True)
    environment=os.environ.copy()
    environment.update({'LOCALAPPDATA':str(tmp_path/'local'),'APPDATA':str(tmp_path/'roaming')})
    child=subprocess.run(command,input=b'{"op":"compact","request_id":"bad request"}\n{"op":"shutdown"}\n',
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=Path(__file__).resolve().parents[1],
        env=environment,timeout=20)
    assert child.returncode==0,child.stderr
    replies=[json.loads(line) for line in child.stdout.decode('utf-8').splitlines()]
    assert replies and replies[0]['event']=='context_compaction'
    assert child.stderr==b''
    row=json.loads(log_path('assistant-worker',tmp_path).read_text(encoding='utf-8'))
    assert row['event']=='child_process_start' and row['pid']!=os.getpid()


def test_logged_child_has_registered_frozen_role(monkeypatch,tmp_path):
    from coat_sim import process_entry
    from coat_sim.project_logging import logged_module_command
    monkeypatch.setattr(process_entry,'is_frozen',lambda:True)
    command=logged_module_command('coat_sim.ui.project_runtime','--project','owned.coatproj',
        channel='project',project_root=tmp_path)
    assert command[:3]==[sys.executable,'--coat-worker','logged-process']
    assert 'coat_sim.ui.project_runtime' in command
    assert process_entry._ROLE_MODULES['logged-process']=='coat_sim.logged_process'



def test_unwritable_log_directory_does_not_prevent_stream_installation(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'mkdir',lambda *_a,**_k: (_ for _ in ()).throw(OSError('read only')))
    assert ProjectLog('desktop',tmp_path).write('startup still proceeds')==len('startup still proceeds')


def test_bootstrap_handoff_already_has_header_in_bounded_project_log(tmp_path,monkeypatch):
    import runpy
    import coat_sim.process_entry as entry
    namespace=runpy.run_path(str(Path(__file__).resolve().parents[1]/'tools/launch_desktop.pyw'))
    main=namespace['main']
    monkeypatch.setattr(entry,'consume_qa_start_slot',lambda:True)
    monkeypatch.setitem(main.__globals__,'PROJECT_ROOT',tmp_path)
    monkeypatch.setitem(main.__globals__,'_restart_with_desktop_host',lambda:True)
    original=(sys.stdout,sys.stderr,sys.__stdout__,sys.__stderr__)
    original_path=list(sys.path)
    try:
        assert main()==0
    finally:
        sys.path[:]=original_path
        for name,value in zip(('stdout','stderr','__stdout__','__stderr__'),original,strict=True):
            setattr(sys,name,value)
    text=log_path('desktop',tmp_path).read_text(encoding='utf-8')
    assert 'Desktop bootstrap:' in text and 'PID: '+str(os.getpid()) in text
    assert 'handed off' in text
