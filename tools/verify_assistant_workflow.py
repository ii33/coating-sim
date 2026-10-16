"""Opt-in live mascot workflow smoke using the actual production runtime.
Reads the saved provider/key via normal credentials; never prints/persists keys.
Run only with --live after the local deterministic workflow suite passes.
"""
from __future__ import annotations
import argparse, asyncio, io, json, sys, time
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from coat_sim.agent.worker import ProtocolWriter
from coat_sim.agent.storage import AssistantStore

class EvidenceWriter(ProtocolWriter):
    def __init__(self,path):
        super().__init__(io.StringIO())
        self.path=path
        self.events=[]
    def emit(self,event,request_id='',**values):
        super().emit(event,request_id,**values)
        record=json.loads(self.stream.getvalue().splitlines()[-1])
        self.stream.seek(0); self.stream.truncate(0)
        self.events.append(record)
        with self.path.open('a',encoding='utf-8') as out:
            out.write(json.dumps(record,ensure_ascii=False)+'\n')

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not args.live:
        parser.error('Paid provider smoke is opt-in: pass --live only with user authorisation')
    config=json.loads((ROOT/'config/ui_settings.json').read_text(encoding='utf-8'))
    config['_debug_limits']={'max_output_tokens':4096,'request_limit':8,'total_tokens_limit':100000}
    store=AssistantStore()
    session=store.new_session('工作流验收 · 双源三层真实代理')
    request='workflow-live-'+uuid4().hex
    folder=ROOT/'artifacts/debug'/request
    folder.mkdir(parents=True)
    writer=EvidenceWriter(folder/'events.jsonl')
    writer.bind_request(request,session)
    writer.configure_issues(request,config)
    writer.configure_quota(config)
    prompt='''这是已授权的软件工作流验收任务，按明确参数真实创建、计算和导出，不讨论整机机械设计，不委派、不做优化、不另跑脚本。先一次查询create_design_workflow契约，再一次提交结构化方案并根据程序返回结果总结。
方案名“双源三层工作流验收”；说明“仅软件验收未标定”。机器太阳轮120齿、行星轮37齿、模数4mm，盘心距按服务换算；基片高度800mm、盘倾角0度。基片为半径60mm的圆形平面。
源1是点源，机器位置(300,0,0)mm；源2是矩形面源length_mm=30,width_mm=20,angle_deg=15，位置(-200,40,80)mm；两源source_exponent=1,device_type=custom。
修正板1 z_mm=450, outer=[[240,-10],[270,-10],[270,10],[240,10]], holes=[]。修正板2 z_mm=550, outer=[[-160,-20],[-130,-20],[-130,20],[-160,20]],holes=[]。monitors=[]。
工艺3层均为target_mode=mean：第1层source_slot=1,target_nm=2000,rate_nm_s=1,mask_slots=[0]；第2层source_slot=2,target_nm=4000,rate_nm_s=2,mask_slots=[1]；第3层source_slot=1,target_nm=8,rate_nm_s=2,mask_slots=[]。
CPU，grid_size=31,time_steps=720，大盘15rpm，初相0；快速算法fast_cycle=true，残余比例阈值5%；export_pdf=true,export_grid=true。其它设置采用该工具实际默认字段，不猜schema。结束简明报告实际有效参数、膜厚均值/CV、厚薄层算法判定、所有真实文件引用；不能用入队代替成功，也不再次重复提交。'''
    store.add_message(session,'user',prompt,request_id=request)
    import coat_sim.agent.runtime as production
    # Restrict only this disposable test process; no historical design/file tools.
    production.MAIN_TOOLS=frozenset({"create_design_workflow","advance_design_workflow",
        "design_workflow_status","cancel_design_workflow","read_software_manual"})
    production.DEVELOPMENT_TOOLS=frozenset()
    run_task,shutdown_runtime=production.run_task,production.shutdown_runtime
    began=time.perf_counter(); failure=None
    try:
        await asyncio.wait_for(run_task({'config':config,'session_id':session,'request_id':request,
            'prompt':prompt,'attachments':[],'task_mode':'chat','reasoning_strength':'auto'},writer),timeout=240)
    except Exception as exc:
        failure=writer.redact(type(exc).__name__+': '+str(exc))[:1800]
    finally:
        await shutdown_runtime()
    replies=[e.get('text','') for e in writer.events if e['event']=='message']
    if replies:
        store.add_message(session,'assistant',replies[-1],request_id=request)
    terminal=any(e['event']=='completed' for e in writer.events)
    summary={'success':terminal and failure is None,'session_id':session,'request_id':request,
             'elapsed_s':round(time.perf_counter()-began,3),'failure':failure,
             'allowed_tools':sorted(production.MAIN_TOOLS),
             'usage':store.round_usage(request),'reply':replies[-1] if replies else '',
             'events_file':str(folder/'events.jsonl')}
    (folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if not summary['success']: raise SystemExit(1)
if __name__=='__main__': asyncio.run(main())
