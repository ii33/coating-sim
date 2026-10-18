"""Small real CPU integrations; writes evidence only in the independent workspace."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import asdict

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / 'tests'))
from test_process_optimization_modes import fixture, options
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.worker import load_result
from coat_sim.core.simulator import Settings

EVIDENCE = WORKSPACE / 'artifacts/scheduler-validation'

def wait(client, job_id):
    deadline = time.monotonic()+180
    while time.monotonic()<deadline:
        job = client.status(job_id)
        if job['state'] in ('succeeded','failed','cancelled'): return job
        time.sleep(.1)
    raise AssertionError('real test timeout: '+job_id)

def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    root = EVIDENCE / ('fixture-'+str(int(time.time())))
    root.mkdir()
    service, path = fixture(root, mask_count=1, source_count=1)
    client = SchedulerClient(root)
    evidence = {'root': str(root), 'backend': 'CPU', 'grid_size': 21,
                'requested_time_steps': 80, 'requested_nominal_dt_s': 1/80, 'checks': []}
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.json')}
    settings = Settings(grid_size=21, time_steps=80, carrier_rpm=1.)
    parameters = {'settings': asdict(settings), 'process_path': str(path), 'backend':'CPU',
                  'fast_cycle':False, 'algorithm':'fast', 'algorithm_options':{}}
    try:
        job = client.submit(freeze_spec(service,'process',parameters), job_id='real-deposition')
        job = wait(client,job['job_id'])
        assert job['state']=='succeeded', job
        result = load_result(client.result(job['job_id']), client)
        import numpy as np
        assert np.isfinite(result['field_nm']).any()
        actual_dt = [layer['time_step_s'] for layer in result['step_results']]
        assert actual_dt and max(actual_dt) <= .05, actual_dt
        evidence['actual_deposition_dt_s'] = actual_dt
        evidence['checks'].append({'name':'real_deposition','job':job,'field_mean_nm':float(np.nanmean(result['field_nm']))})
        optimization_options = options()
        optimization_options['mask_bounds'][0]['z_mm'] = 50.
        optimized = client.submit(freeze_spec(service,'mask_optimization',
                    {'settings':asdict(settings),'process_path':str(path),'options':optimization_options}),job_id='real-mask-optimization')
        optimized = wait(client,optimized['job_id'])
        assert optimized['state']=='succeeded', optimized
        assert optimized['result']['pending_masks'], optimized
        optimized_result = load_result(client.result(optimized['job_id']), client)
        actual_optimization_dt = [layer['time_step_s'] for layer in optimized_result['step_results']]
        assert actual_optimization_dt and max(actual_optimization_dt) <= .05, actual_optimization_dt
        evidence['actual_optimization_verification_dt_s'] = actual_optimization_dt
        evidence['checks'].append({'name':'real_mask_optimization','job':optimized})
        assert before == {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.json')}
        evidence['checks'].append({'name':'source_files_unchanged','sha256':before})

        # Real result-save failure, followed by healthy work, rather than only a probe injection.
        failure_id='0123456789abcdef0123456789abcdef'
        output=root/'data/tmp/simulations'/failure_id
        output.mkdir(parents=True)
        (output/'result.npz').write_bytes(b'injected invalid pre-existing archive')
        failed = client.submit(freeze_spec(service,'process',parameters),job_id=failure_id)
        failed = wait(client,failed['job_id'])
        assert failed['state']=='failed' and failed['error_code']=='result_save_failed', failed
        evidence['checks'].append({'name':'real_result_save_failure','job':failed})

        # Execute the real GUI calculate and launcher shutdown paths in a client process.
        gui_code = '''import os,json,sys,time
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import QTimer
from coat_sim.core.api import CoatService
from coat_sim.scheduler import SchedulerClient
from coat_sim.ui.main import MainBridge
from coat_sim.ui.launcher import LauncherBridge
app=QGuiApplication([])
bridge=MainBridge(CoatService(sys.argv[1]))
bridge._paths['process']=sys.argv[2]
bridge._values['grid_size']='21'
bridge._values['time_step_s']='0.025'
bridge._device='CPU'
bridge.calculate()
ids=[event.job_id for event in bridge._job_cancel_events.values()]
assert ids,bridge.status
# A local observer ID is provisional until the service has durably admitted it.
client=SchedulerClient(sys.argv[1])
deadline=time.monotonic()+30
admitted=set()
while not set(ids).issubset(admitted) and time.monotonic()<deadline:
    app.processEvents()
    admitted={row['job_id'] for row in client.snapshot()['jobs']}
    time.sleep(.01)
assert set(ids).issubset(admitted),('admission timed out',ids,bridge.status)
class Harness:
    _closing=False
    _main_bridge=bridge
    control_registry=control_server=_queue_view=assistant_controller=None
    _child_timer=QTimer()
    _shutdown_timer=QTimer()
    _windows=[]
    _children=[]
    def _finish_shutdown(self):pass
LauncherBridge.shutdown(Harness())
print(json.dumps({'jobs':ids,'detached':not bridge.busy}),flush=True)
'''
        gui = subprocess.run([sys.executable,'-c',gui_code,str(root),str(path)],cwd=WORKSPACE,
                             capture_output=True,text=True,timeout=90)
        assert gui.returncode==0,gui.stderr+'\n'+gui.stdout
        gui_response = json.loads(gui.stdout.strip().splitlines()[-1])
        for job_id in gui_response['jobs']:
            state=wait(client,job_id)
            assert state['state']=='succeeded',state
        evidence['checks'].append({'name':'gui_calculate_shutdown_client_exit','response':gui_response,'stderr':gui.stderr})

        # MCP status/result and independent CLI status read the same committed job.
        import coat_sim.mcp.server as mcp
        mcp.service=service
        mcp_state=mcp.calculation_job_status(job['job_id'])
        assert mcp_state['state']==client.status(job['job_id'])['state']
        assert mcp.calculation_job_result(job['job_id'])['artifact_id']==job['result']['artifact_id']
        mcp._stop_jobs()
        cli=subprocess.run([sys.executable,'-m','coat_sim.scheduler','--root',str(root),'status',job['job_id']],
                           cwd=WORKSPACE,capture_output=True,text=True,timeout=60)
        assert cli.returncode==0,cli.stderr
        assert json.loads(cli.stdout)['state']==mcp_state['state']
        evidence['checks'].append({'name':'MCP_GUI_CLI_shared_state','state':mcp_state['state']})
        evidence['status']='passed'
    except BaseException as exc:
        evidence.update(status='failed',error=str(exc))
        raise
    finally:
        (EVIDENCE/'real-checks.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        try: client.stop()
        except Exception as exc: print('cleanup',exc)
    print(json.dumps({'status':evidence['status'],'checks':len(evidence['checks']),'evidence':str(EVIDENCE/'real-checks.json')}))

if __name__=='__main__':main()
