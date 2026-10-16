"""User-authorized integration verification; no external model/API billing.

Run with the project Python. Creates unique owned designs and retained evidence;
never overwrites user designs or the production environment-issues ledger.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coat_sim.workflow import (Workflow, WorkflowNode, WorkflowEngine,
                               SQLiteCheckpointStore, StepResult, WorkflowError,
                               pydantic_agent_node)
from coat_sim.core.design_workflow import DesignWorkflowSpec, design_pipeline, prepare_design_bundle
from coat_sim.core.data_files import data_path

IDENTITY = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
EVIDENCE = ROOT / "artifacts" / "workflow-verification" / IDENTITY
EVIDENCE.mkdir(parents=True, exist_ok=False)
CASES = []
DETAILS = {"external_model_calls": 0, "identity": IDENTITY, "requirements": [
    "双源不同高度、双修正板、晶控/光控监测、三层平均/中心/监测基准、快速周期与精确薄层、报告/网格交付",
    "部分失败后恢复，不重复生成设计/提交计算，用户修改后的文件不被覆盖",
    "程序/Agent 节点混合、分支循环、外部输入、取消、崩溃恢复、跨执行器互斥",
    "在真实 LPAC 沙箱中使用实际结果网格生成热图、等值线、径向剖面",
    "开发报告实际持久化、敏感内容脱敏、发行门禁、错误分类"], "cases": CASES}


async def case(name, operation):
    started = time.perf_counter()
    try:
        detail = await operation()
        CASES.append({"name": name, "status": "passed", "elapsed_s": time.perf_counter() - started,
                      "detail": detail})
        print("PASS " + name, flush=True)
    except Exception:
        CASES.append({"name": name, "status": "failed", "elapsed_s": time.perf_counter() - started,
                      "error": traceback.format_exc()})
        print("FAIL " + name + "\n" + CASES[-1]["error"], flush=True)


def engine(name):
    return WorkflowEngine(SQLiteCheckpointStore(EVIDENCE / (name + ".sqlite3")))


async def engine_recovery():
    runner = engine("recovery")
    fail = True
    effects = {}
    keys = []
    async def operation(context):
        nonlocal fail
        keys.append(context.idempotency_key)
        effects.setdefault(context.idempotency_key, {"one_external_effect": True})
        context.data["partial"] = object() if fail else "complete"
        if fail:
            fail = False
            raise RuntimeError("workflow-verification injected interrupted node")
        return StepResult(output="saved")
    workflow = Workflow("verification.recovery", "1", "save", [WorkflowNode("save", operation)])
    try:
        await runner.run(workflow, {"same": True}, run_id="recover")
        raise AssertionError("expected injected node failure")
    except RuntimeError:
        pass
    failed = runner.get("recover")
    assert failed.status == "failed" and failed.data == {} and failed.outputs == {}
    resumed = await engine("recovery").run(workflow, {"same": True}, run_id="recover")
    assert resumed.status == "completed" and resumed.data == {"partial": "complete"}
    assert keys[0] == keys[1] and len(effects) == 1
    events = runner.events("recover")
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    return {"effect_count": len(effects), "keys": keys, "events": runner.events("recover")}


async def engine_loop_pause():
    runner = engine("loop")
    keys = []
    def loop(context):
        keys.append(context.idempotency_key)
        n = context.data.get("n", 0) + 1
        return StepResult(output=n, update={"n": n}, next_node="loop" if n < 3 else "confirm")
    def confirm(context):
        if context.resume_data is None:
            return StepResult(output="proposed", wait_for_input="approve")
        assert context.resume_data == {"approved": True}
        return StepResult(output="approved")
    workflow = Workflow("verification.pause", "1", "loop", [WorkflowNode("loop", loop), WorkflowNode("confirm", confirm)])
    paused = await runner.run(workflow, {}, run_id="pause")
    assert paused.status == "paused" and paused.data["n"] == 3 and len(set(keys)) == 3
    assert (await runner.run(workflow, {}, run_id="pause")).status == "paused"
    try:
        await runner.resume(workflow, "pause")
        raise AssertionError("must require external response")
    except WorkflowError:
        pass
    done = await runner.resume(workflow, "pause", response={"approved": True})
    assert done.status == "completed" and done.outputs["confirm"] == "approved"
    try:
        await runner.run(workflow, {"changed": True}, run_id="pause")
        raise AssertionError("input conflict must fail")
    except WorkflowError:
        pass
    return {"loop_keys": keys, "step_count": done.step_count}


async def engine_lease_cancel():
    runner = engine("lease")
    other = engine("lease")
    entered = asyncio.Event()
    async def block(context):
        entered.set()
        await asyncio.Event().wait()
    workflow = Workflow("verification.cancel", "1", "wait", [WorkflowNode("wait", block)])
    task = asyncio.create_task(runner.run(workflow, {}, run_id="cancel"))
    await entered.wait()
    try:
        await other.run(workflow, {}, run_id="cancel")
        raise AssertionError("second executor must not duplicate active effects")
    except WorkflowError as exc:
        busy = str(exc)
    assert await runner.cancel("cancel")
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert runner.get("cancel").status == "cancelled"
    assert (await other.run(workflow, {}, run_id="cancel")).status == "cancelled"
    try:
        await other.resume(workflow, "cancel", response={})
        raise AssertionError("explicit cancellation must be terminal")
    except WorkflowError:
        pass
    return {"busy_error": busy, "terminal": runner.get("cancel").status}


async def engine_interruption():
    runner = engine("interrupt")
    entered = asyncio.Event()
    interrupted = True
    async def operation(context):
        nonlocal interrupted
        if interrupted:
            interrupted = False
            entered.set()
            await asyncio.Event().wait()
        return "resumed"
    workflow = Workflow("verification.interrupt", "1", "node", [WorkflowNode("node", operation)])
    task = asyncio.create_task(runner.run(workflow, {}, run_id="interrupt"))
    await entered.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert runner.get("interrupt").status == "interrupted"
    done = await engine("interrupt").run(workflow, {}, run_id="interrupt")
    assert done.status == "completed" and done.outputs["node"] == "resumed"
    return {"events": runner.events("interrupt")}


async def cancellation_swallowed_by_node():
    runner = engine("swallowed")
    entered = asyncio.Event()
    def after(context):
        raise AssertionError("cancelled handler must not advance")
    async def suppress(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return StepResult(output="incorrect completion", update={"bad": True})
    workflow = Workflow("verification.swallowed", "1", "handler", [WorkflowNode("handler", suppress, "after"), WorkflowNode("after", after)])
    task = asyncio.create_task(runner.run(workflow, {}, run_id="swallowed"))
    await entered.wait()
    await runner.cancel("swallowed")
    try:
        await task
    except asyncio.CancelledError:
        pass
    saved = runner.get("swallowed")
    assert saved.status == "cancelled" and saved.outputs == {} and saved.data == {}
    assert "swallowed" not in runner._cancel_requests
    return {"state": saved.snapshot()}


async def same_store_stale_lease():
    store = SQLiteCheckpointStore(EVIDENCE / "stale-lease.sqlite3")
    first, second = WorkflowEngine(store), WorkflowEngine(store)
    started = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]
    calls = 0
    async def operation(context):
        nonlocal calls
        index = calls
        calls += 1
        started[index].set()
        await finish[index].wait()
        return "stale" if index == 0 else "winner"
    workflow = Workflow("verification.stale_lease", "1", "node", [WorkflowNode("node", operation)])
    task_first = asyncio.create_task(first.run(workflow, {}, run_id="stale"))
    await started[0].wait()
    # Inject lease expiry only in this isolated test database to exercise
    # a stale executor after a new owner claims the same store instance.
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE workflow_leases SET expires_at=0 WHERE run_id='stale'")
    task_second = asyncio.create_task(second.run(workflow, {}, run_id="stale"))
    await started[1].wait()
    finish[0].set()
    try:
        await task_first
        raise AssertionError("stale writer must not borrow new owner token")
    except WorkflowError:
        pass
    finish[1].set()
    winner = await task_second
    assert winner.status == "completed" and winner.outputs["node"] == "winner"
    assert store.get("stale").outputs["node"] == "winner"
    return {"state": winner.snapshot(), "executors": 2}


async def engine_separate_process_lease():
    runner = engine("process-lease")
    started = asyncio.Event()
    async def operation(context):
        started.set()
        await asyncio.Event().wait()
    workflow = Workflow("verification.process", "1", "node", [WorkflowNode("node", operation)])
    task = asyncio.create_task(runner.run(workflow, {}, run_id="process"))
    await started.wait()
    child = EVIDENCE / "lease_child.py"
    marker = EVIDENCE / "child_effect.txt"
    child.write_text('''import asyncio,json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom coat_sim.workflow import *\ndef effect(ctx):\n Path(sys.argv[3]).write_text("created",encoding="utf-8")\n return "child"\nasync def main():\n runner=WorkflowEngine(SQLiteCheckpointStore(sys.argv[2]))\n workflow=Workflow("verification.process","1","node",[WorkflowNode("node",effect)])\n try:\n  result=await runner.run(workflow,{},run_id="process")\n  print(json.dumps({"status":result.status}))\n except WorkflowError as error:\n  print(json.dumps({"busy":True,"error":str(error)},ensure_ascii=False))\nasyncio.run(main())\n''', encoding="utf-8")
    process = await asyncio.create_subprocess_exec(sys.executable, "-X", "utf8", str(child), str(ROOT),
                    str(runner.store.path), str(marker), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        reply = json.loads(stdout.decode("utf-8"))
        assert reply.get("busy") and not marker.exists(), reply
        return {"child_process_returncode": process.returncode, "reply": reply, "duplicate_effect": False}
    finally:
        await runner.cancel("process")
        try:
            await task
        except asyncio.CancelledError:
            pass


async def actual_mcp_cancel_no_restart():
    from coat_sim.mcp import server
    specification = complex_spec()
    specification["fast_cycle"] = False
    specification["settings"].update(time_steps=5000, grid_size=101)
    specification.update(export_pdf=False, export_grid=False)
    request = "workflow-cancel-verification-" + IDENTITY
    spec = DesignWorkflowSpec.model_validate(specification)
    reply = await server.create_design_workflow(request, spec)
    assert reply["workflow_status"] == "paused", reply
    cancel = await server.cancel_design_workflow(reply["workflow_run_id"])
    assert cancel["cancelled"], cancel
    job = await server.wait_calculation_job(reply["job_id"], 30)
    assert job["state"] == "cancelled", job
    repeat = await server.create_design_workflow(request, spec)
    assert repeat["workflow_status"] == "cancelled" and repeat["job_id"] == reply["job_id"]
    try:
        await server.advance_design_workflow(reply["workflow_run_id"])
        raise AssertionError("cancelled job must not restart on advance")
    except Exception as exc:
        assert "取消" in str(exc)
    return {"cancel": cancel, "terminal_job": job, "repeat": repeat}


async def runtime_auto_advance_states():
    from coat_sim.agent.runtime import BudgetAndTools
    cases = []
    for mode in ("completed", "failed_response", "failed_request"):
        calls, artifacts, observations, issues = [], [], [], []
        class Client:
            async def call_tool_mcp(self, name, arguments):
                calls.append({"name": name, "arguments": arguments})
                if name == "calculation_job_result":
                    value = {"run_id": "verified-run", "result_file": "test.npz"}
                elif name == "read_calculation_result":
                    value = {"run_id": "verified-run", "field_unit": "nm", "field_shape": [31,31],
                             "settings": {"sun_teeth": 48}, "metadata": {"mean_nm": 265}, "elapsed_s": 0.1}
                elif name == "advance_design_workflow":
                    if mode == "failed_request":
                        raise OSError("workflow-verification injected export request error")
                    value = {"workflow_run_id": "design-test", "workflow_status": "completed" if mode == "completed" else "failed",
                             "files": [{"reference": "data/tmp/workflow-verification-report.pdf"}] if mode == "completed" else [],
                             "error": None if mode == "completed" else "workflow-verification injected export stage error"}
                else:
                    raise AssertionError(name)
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(value))],
                                       structured_content=value, data=value, is_error=False)
        runtime = BudgetAndTools.__new__(BudgetAndTools)
        runtime.client = Client()
        runtime.writer = SimpleNamespace(redact=lambda value: value)
        runtime.ledger = SimpleNamespace(update_job=lambda value: None)
        runtime.progress = lambda *args: None
        runtime._observe_tool = lambda *args: observations.append(args)
        runtime._record_issue = lambda *args, **kwargs: issues.append((args, kwargs))
        runtime._upsert_artifact = lambda artifact: artifacts.append(artifact)
        result = await runtime._complete_default_workflow({"workflow_run_id": "design-test", "job_id": "verified-job",
                    "state": "succeeded", "job": {"job_id": "verified-job", "state": "succeeded"}})
        assert result["simulation"]["ok"] is True, result
        assert result["ok"] is (mode == "completed"), result
        assert len(artifacts) == (1 if mode == "completed" else 0)
        assert [call["name"] for call in calls] == ["calculation_job_result", "read_calculation_result", "advance_design_workflow"]
        assert not any("submit" in call["name"] for call in calls)
        cases.append({"mode": mode, "result": result, "calls": calls, "artifacts": artifacts})
    return {"transport": "deterministic MCP response fixture; no network/model calls", "cases": cases}


async def engine_invalid_boundaries():
    runner = engine("boundaries")
    def invalid(context):
        context.data["incomplete"] = True
        return StepResult(output="bad", next_node="missing")
    workflow = Workflow("verification.invalid", "1", "node", [WorkflowNode("node", invalid)])
    try:
        await runner.run(workflow, {}, run_id="invalid")
        raise AssertionError("invalid transition must fail")
    except WorkflowError:
        pass
    assert runner.get("invalid").data == {} and runner.get("invalid").outputs == {}
    forever = Workflow("verification.limit", "1", "again", [WorkflowNode("again", lambda ctx: StepResult(next_node="again"))], max_steps=3)
    try:
        await runner.run(forever, {}, run_id="limit")
        raise AssertionError("loop budget must stop")
    except WorkflowError:
        pass
    assert runner.get("limit").attempts["again"] == 3
    return {"invalid": runner.get("invalid").snapshot(), "budget": runner.get("limit").snapshot()}


async def agent_program_nodes():
    from pydantic import BaseModel
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    class Plan(BaseModel):
        approved: bool
        name: str
    agent = Agent(TestModel(custom_output_args={"approved": True, "name": "workflow verification"}), output_type=Plan)
    node = pydantic_agent_node("plan", agent, lambda ctx: "Produce a typed plan, revision " + str(ctx.data.get("visits", 0)),
                               next_node="validate", skills=["coat-mcp"])
    history_lengths = []
    def validate(context):
        Plan.model_validate(context.outputs["plan"])
        history_lengths.append(len(json.loads(context.agent_context("plan")["message_history_json"])))
        visit = context.data.get("visits", 0) + 1
        return StepResult(output="validated", update={"visits": visit}, next_node="plan" if visit < 2 else None)
    runner = engine("agent")
    done = await runner.run(Workflow("verification.agent", "1", "plan", [node, WorkflowNode("validate", validate)]), {})
    assert done.status == "completed" and history_lengths[1] > history_lengths[0]
    return {"model": "PydanticAI TestModel (deterministic, no network)", "history_lengths": history_lengths,
            "events": runner.events(done.run_id)}


def complex_spec():
    return {
        "name": "工作流集成验证 " + IDENTITY,
        "description": "用户授权的工作流验证数据；用于软件链路验证，非设备标定或生产方案。",
        "sources": [{"design": {"schema_version": 2, "kind": "point", "source_exponent": 1.2}, "x_mm": 170, "y_mm": 0, "z_mm": 0},
                    {"design": {"schema_version": 2, "kind": "rectangle", "length_mm": 25, "width_mm": 15, "angle_deg": 20}, "x_mm": -160, "y_mm": 30, "z_mm": 40}],
        "masks": [{"z_mm": 240, "outer": [[10,-90],[30,-90],[30,90],[10,90]], "holes": []},
                  {"z_mm": 400, "outer": [[-100,-30],[-60,-30],[-60,15],[-100,15]], "holes": []}],
        "substrate": {"schema_version": 1, "name": "验证平面圆片", "outline": {"kind": "circle", "radius_mm": 60}, "surface": {"kind": "flat"}},
        "machine": {"sun_teeth": 48, "planet_teeth": 48, "gear_module_mm": 2.5, "substrate_z_mm": 700, "planet_tilt_deg": 8},
        "monitors": [{"name": "验证晶控", "kind": "crystal", "x_mm": 20, "y_mm": -40, "z_mm": 700},
                     {"name": "验证光控", "kind": "optical", "x_mm": 30, "y_mm": 50, "z_mm": 650}],
        "steps": [{"source_slot": 1, "target_nm": 200, "rate_nm_s": 0.5, "target_mode": "mean", "mask_slots": [0]},
                  {"source_slot": 2, "target_nm": 60, "rate_nm_s": 0.2, "target_mode": "monitor", "monitor_slot": 1, "mask_slots": [1]},
                  {"source_slot": 1, "target_nm": 5, "rate_nm_s": 20, "target_mode": "center", "mask_slots": []}],
        "settings": {"carrier_rpm": 15, "carrier_phase_deg": 13, "planet_phase_deg": 27, "grid_size": 31, "time_steps": 360},
        "backend": "CPU", "fast_cycle": True, "fast_cycle_residual_percent": 5,
        "export_pdf": True, "export_grid": True}


async def real_pipeline():
    from coat_sim.mcp import server
    spec = DesignWorkflowSpec.model_validate(complex_spec())
    request = "workflow-verification-" + IDENTITY
    reply = await server.create_design_workflow(request, spec)
    job_id = reply["job_id"]
    deadline = time.monotonic() + 180
    while reply["workflow_status"] != "completed":
        job = await server.wait_calculation_job(job_id, 10)
        if job["state"] in ("failed", "cancelled"):
            raise AssertionError(job)
        reply = await server.advance_design_workflow(reply["workflow_run_id"])
        if time.monotonic() > deadline:
            raise TimeoutError("workflow integration deadline")
    result = reply["result"]
    assert result["settings"]["sun_teeth"] == 48
    assert result["settings"]["planet_teeth"] == 48
    assert result["settings"]["planet_offset_mm"] == 120
    assert result["settings"]["substrate_z_mm"] == 700
    assert len(result["machine"]["sources"]) == 2 and result["machine"]["sources"][1]["z_mm"] == 40
    assert len(result["process"]["steps"]) == 3
    paths = [Path(info["path"]) for info in reply["files"]]
    assert len(paths) == 9 and len(set(paths)) == 9 and all(path.is_file() for path in paths)
    grid_path = next(path for path in paths if path.name == "grid.json")
    grid = json.loads(grid_path.read_text(encoding="utf-8"))
    finite = [value for row in grid["field"] for value in row if value is not None]
    assert len(finite) > 100 and all(math.isfinite(value) and value >= 0 for value in finite)
    original_events = server.design_workflow_status(reply["workflow_run_id"])["events"]
    repeat = await server.create_design_workflow(request, spec)
    assert repeat["job_id"] == job_id and repeat["run_id"] == reply["run_id"]
    assert server.find_calculation_job(request)["job"]["job_id"] == job_id
    assert server.design_workflow_status(reply["workflow_run_id"])["events"] == original_events
    changed = spec.model_copy(update={"name": "changed"})
    try:
        await server.create_design_workflow(request, changed)
        raise AssertionError("changed input must conflict")
    except Exception as exc:
        assert "相同 run_id" in str(exc) or "conflict" in str(exc)
    DETAILS["real_workflow"] = reply
    DETAILS["grid_path"] = str(grid_path)
    return {"files": reply["files"], "job_id": job_id, "run_id": reply["run_id"],
            "effective_machine": {key: result["settings"][key] for key in ("sun_teeth", "planet_teeth", "planet_offset_mm", "substrate_z_mm")},
            "events": original_events, "finite_grid_points": len(finite)}


async def partial_export_recovery():
    from coat_sim.mcp import server
    recorded = DETAILS["real_workflow"]
    request = "workflow-export-recovery-" + IDENTITY
    spec = DesignWorkflowSpec.model_validate(complex_spec()).model_dump(mode="json", exclude_none=True)
    fail = True
    submits = 0
    def reused_submit(settings, path, **options):
        nonlocal submits
        submits += 1
        return deepcopy(recorded["job"])
    def injected_pdf(*args, **kwargs):
        nonlocal fail
        if fail:
            fail = False
            raise OSError("workflow-verification injected export interruption")
        return server.export_pdf(*args, **kwargs)
    pipeline = design_pipeline(server.service, submit=reused_submit, status=server.calculation_job_status,
                               read_result=server.read_calculation_result, export_pdf=injected_pdf)
    runner = engine("export")
    try:
        await runner.run(pipeline, {"client_request_id": request, "specification": spec}, run_id="export")
        raise AssertionError("expect export failure")
    except OSError:
        pass
    failed = runner.get("export")
    assert failed.status == "failed" and failed.current_node == "export" and submits == 1
    grid_path = data_path("results/workflow-output/" + hashlib.sha256(request.encode()).hexdigest()[:32] + "/grid.json", root_allowed=False)
    original_hash = hashlib.sha256(grid_path.read_bytes()).hexdigest()
    original_mtime = grid_path.stat().st_mtime_ns
    done = await engine("export").run(pipeline, {"client_request_id": request, "specification": spec}, run_id="export")
    assert done.status == "completed" and submits == 1
    assert grid_path.stat().st_mtime_ns == original_mtime and hashlib.sha256(grid_path.read_bytes()).hexdigest() == original_hash
    # Evidence is clear: calculation callback reused the already-successful real
    # job; this case exercises export recovery rather than a second simulation.
    return {"submitted_callback_count": submits, "reused_real_job": recorded["job_id"],
            "grid_reused_without_write": True, "events": runner.events("export"), "files": done.outputs["export"]["files"]}


async def invalid_contract_and_changed_file():
    from coat_sim.mcp import server
    spec = complex_spec()
    invalid = deepcopy(spec)
    invalid["steps"][0]["source_slot"] = 10
    try:
        DesignWorkflowSpec.model_validate(invalid)
        raise AssertionError("invalid source slot must fail before writes")
    except ValueError:
        pass
    request = "workflow-file-check-" + IDENTITY
    manifest = prepare_design_bundle(server.service, request, spec)
    path = Path(manifest["paths"]["sources"][0])
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["source_exponent"] = 2.4
    server.service.save("source", path, changed, overwrite=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        prepare_design_bundle(server.service, request, spec)
        raise AssertionError("modified owned file must stop recovery")
    except Exception as exc:
        assert "修改" in str(exc)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    return {"modified_file_preserved": str(path), "invalid_slot_rejected": True}


async def visualization_regression():
    # Private runtime owns Pygal; host does not need to install sandbox packages.
    packages = ROOT / "artifacts" / "sandbox-runtime" / "venv" / "Lib" / "site-packages"
    with patch.object(sys, "path", [str(packages), *sys.path]):
        from coat_sim.agent import sandbox_helpers as plot
    svg_count = 0
    for x, y, values in [([0,1,2],[0,1],[[1,None,3],[2,4,5]]),
                         ([2,1,0],[1,0],[[5,4,2],[3,None,1]]),
                         ([0,1],[0,1],[[7,7],[7,7]])]:
        chart = plot.heatmap(x, y, values, title="verification < &")
        svg = chart.render().decode() if isinstance(chart.render(), bytes) else chart.render()
        root = ET.fromstring(svg)
        assert not any(element.tag.rsplit("}",1)[-1] == "script" for element in root.iter())
        assert "nan" not in svg.lower() and "infinity" not in svg.lower()
        svg_count += 1
    for level in (float("nan"), float("inf")):
        try:
            plot.contour([0,1],[0,1],[[0,1],[1,0]], levels=[level])
            raise AssertionError("nonfinite contour levels must fail")
        except ValueError:
            pass
    saddle = plot.contour([0,1],[0,1],[[0,1],[1,0]], levels=[0.5])
    assert "svg" in str(saddle.render())
    rows = plot.radial_profile([-1,0,1],[-1,0,1],[[None,2,None],[3,4,5],[None,6,None]], bin_width=0.5)
    assert sum(row["count"] for row in rows) == 5
    profile = plot.profile_chart(rows)
    ET.fromstring(profile.render())
    DETAILS["local_plot_regression"] = {"heatmaps": svg_count, "radial_count": sum(row["count"] for row in rows)}
    return DETAILS["local_plot_regression"]


async def sandbox_real_grid():
    from coat_sim.mcp import server
    grid_path = DETAILS["grid_path"]
    code = '''import json\nfrom pathlib import Path\nimport coat_plot as plot\ngrid=json.loads(Path(next(iter(INPUT_FILES.values()))).read_text(encoding="utf-8"))\nplot.save_chart(plot.heatmap(grid["x"],grid["y"],grid["field"],title="Real workflow thickness"),"heatmap.svg")\nplot.save_chart(plot.contour(grid["x"],grid["y"],grid["field"],levels=8,title="Real workflow contours"),"contour.svg")\nrows=plot.radial_profile(grid["x"],grid["y"],grid["field"],bin_width=5)\nplot.save_chart(plot.profile_chart(rows,title="Real workflow radial thickness"),"radial.svg")\nPath(OUTPUT_DIR,"radial.json").write_text(json.dumps(rows,allow_nan=False),encoding="utf-8")\nprint(json.dumps({"bins":len(rows),"count":sum(row["count"] for row in rows)}))\n'''
    result = await server.run_python_script(code, files=[grid_path], timeout_s=60)
    assert result.get("exit_code") == 0 and not result.get("timed_out"), result
    assert len(result.get("files", [])) == 4, result
    for info in result["files"]:
        path = Path(info["path"])
        assert path.is_file()
        if path.suffix == ".svg":
            xml = ET.fromstring(path.read_text(encoding="utf-8"))
            assert not any(node.tag.rsplit("}",1)[-1] == "script" for node in xml.iter())
    return result


async def development_reports():
    from coat_sim.agent import environment_issues as issues
    from coat_sim.mcp import server
    prefix = "tmp/workflow-verification-" + IDENTITY
    original_data_path = issues.data_path
    def isolated_path(value, **kwargs):
        if not Path(value).is_absolute() and str(value).startswith(("tmp/", "data/tmp")):
            value = prefix + "/" + Path(value).name
        return original_data_path(value, **kwargs)
    with patch.dict(os.environ, {"COAT_DEVELOPMENT": "1"}), patch.object(issues, "data_path", isolated_path), \
            patch.object(issues, "LOG_BASE", "data/" + prefix + "/ENVIRONMENT_ISSUES"):
        result = server.developer_report_issue("工作流验证注入异常", "workflow-verification intentional failure; Authorization: Bearer sk-test-secret-verification",
                                                stage="workflow_verification", request_id=IDENTITY,
                                                recovery="验证记录通道，未声称生产环境发生问题")
        assert result.get("recorded") and result.get("reports_saved"), result
        reports = server.developer_issue_reports(refresh=True, limit=20)
        paths = [Path(item["path"]) for item in reports.get("files", [])]
        assert paths and all(path.is_file() for path in paths), reports
        for path in paths:
            if path.suffix in (".md", ".json"):
                assert "sk-test-secret-verification" not in path.read_text(encoding="utf-8")
        with patch.dict(os.environ, {"COAT_RELEASE_BUILD": "1"}):
            try:
                server.developer_issue_reports()
                raise AssertionError("release mode must refuse developer tools")
            except Exception as exc:
                assert "禁止" in str(exc) or "未启用" in str(exc)
        return {"report": result, "readback": reports, "isolated_ledger": True}


async def main():
    selected = None
    if "--only" in sys.argv:
        selected = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    for name, operation in [
        ("engine_failed_node_transaction_and_restart_idempotency", engine_recovery),
        ("engine_loop_and_external_input", engine_loop_pause),
        ("engine_cross_executor_lease_and_terminal_cancel", engine_lease_cancel),
        ("engine_interrupted_run_restores", engine_interruption),
        ("explicit_cancel_survives_handler_suppression", cancellation_swallowed_by_node),
        ("same_store_expired_lease_cannot_borrow_new_token", same_store_stale_lease),
        ("engine_separate_process_lease", engine_separate_process_lease),
        ("engine_invalid_transition_and_loop_limit", engine_invalid_boundaries),
        ("typed_agent_program_nodes_persist_context", agent_program_nodes),
        ("actual_mcp_multi_source_mask_monitor_recipe_export", real_pipeline),
        ("partial_export_retry_reuses_real_result_and_existing_grid", partial_export_recovery),
        ("contract_rejection_and_modified_owned_file_preserved", invalid_contract_and_changed_file),
        ("scientific_plot_regression", visualization_regression),
        ("actual_lpac_result_grid_visualization", sandbox_real_grid),
        ("development_report_persistence_redaction_release_gate", development_reports),
        ("actual_mcp_cancel_no_restart", actual_mcp_cancel_no_restart),
        ("runtime_auto_advance_states", runtime_auto_advance_states),
    ]:
        if selected is None or name in selected:
            await case(name, operation)
    DETAILS["passed"] = sum(item["status"] == "passed" for item in CASES)
    DETAILS["failed"] = sum(item["status"] == "failed" for item in CASES)
    path = EVIDENCE / "evidence.json"
    path.write_text(json.dumps(DETAILS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"passed": DETAILS["passed"], "failed": DETAILS["failed"], "evidence": str(path)}, ensure_ascii=False), flush=True)
    return bool(DETAILS["failed"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
